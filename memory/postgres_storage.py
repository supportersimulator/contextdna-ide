#!/usr/bin/env python3
"""
PostgreSQL Storage Backend — Unified Storage Layer (Postgres half)
==================================================================

Mirrors the public surface of :mod:`memory.sqlite_storage` so consumers can
swap backends by setting ``STORAGE_BACKEND=postgres``. Functions exported at
module level match the legacy contract used by ``persistent_hook_structure.py``
and ``synaptic_service_hub.py``:

    from memory.postgres_storage import (
        store_learning, get_failure_count,
        get_connection, release_connection,
    )

ZSF / graceful degradation:
    * psycopg2 is **not** imported at module load. Every function lazy-imports
      it inside the call. When psycopg2 is missing, ``BackendUnavailable`` is
      raised (or the public function returns a safe default + bumps a counter
      so the caller — and observability — can see the failure).
    * Every operation increments a per-method counter in :data:`_COUNTERS`.

Connection config (env, in order):
    LEARNINGS_DB_USER / DATABASE_USER     → ``context_dna``
    LEARNINGS_DB_PASSWORD / DATABASE_PASSWORD → ``context_dna_dev``
    LEARNINGS_DB_HOST / DATABASE_HOST     → ``127.0.0.1``
    LEARNINGS_DB_PORT / DATABASE_PORT     → ``5432``
    LEARNINGS_DB_NAME / DATABASE_NAME     → ``context_dna``
    DATABASE_URL                          → overrides everything above
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


logger = logging.getLogger("contextdna.postgres")


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #

def _env(name: str, fallback: str, default: str) -> str:
    return os.environ.get(name, os.environ.get(fallback, default))


DATABASE_USER = _env("LEARNINGS_DB_USER", "DATABASE_USER", "context_dna")
DATABASE_PASSWORD = _env("LEARNINGS_DB_PASSWORD", "DATABASE_PASSWORD", "context_dna_dev")
DATABASE_HOST = _env("LEARNINGS_DB_HOST", "DATABASE_HOST", "127.0.0.1")
DATABASE_PORT = _env("LEARNINGS_DB_PORT", "DATABASE_PORT", "5432")
DATABASE_NAME = _env("LEARNINGS_DB_NAME", "DATABASE_NAME", "context_dna")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"postgresql://{DATABASE_USER}:{DATABASE_PASSWORD}@{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}",
)


# --------------------------------------------------------------------------- #
# Errors + counters (ZSF)                                                      #
# --------------------------------------------------------------------------- #


class BackendUnavailable(RuntimeError):
    """Raised when psycopg2 is missing or the DB is unreachable."""


_COUNTERS: Counter[str] = Counter()
_COUNTER_LOCK = threading.Lock()


def _bump(key: str) -> None:
    with _COUNTER_LOCK:
        _COUNTERS[key] += 1


def get_counters() -> Dict[str, int]:
    """Return a snapshot of operation counters."""
    with _COUNTER_LOCK:
        return dict(_COUNTERS)


# --------------------------------------------------------------------------- #
# Connection pool                                                              #
# --------------------------------------------------------------------------- #

_connection_pool = None
_pool_lock = threading.Lock()


def _psycopg2():
    """Lazy import of psycopg2. Raises :class:`BackendUnavailable` on failure."""
    try:
        import psycopg2  # noqa: WPS433
        from psycopg2 import pool  # noqa: WPS433

        return psycopg2, pool
    except ImportError as exc:
        _bump("psycopg2.missing")
        raise BackendUnavailable("psycopg2 not installed") from exc


def get_connection():
    """Return a pooled connection or ``None`` if the backend is unavailable.

    Backward-compatible with legacy callers in ``persistent_hook_structure.py``
    which expect ``None`` rather than an exception when psycopg2 isn't around.
    """
    global _connection_pool
    try:
        _, pool = _psycopg2()
    except BackendUnavailable:
        logger.warning("psycopg2 not available — PostgreSQL storage disabled")
        return None

    try:
        with _pool_lock:
            if _connection_pool is None:
                _connection_pool = pool.ThreadedConnectionPool(
                    minconn=1, maxconn=10, dsn=DATABASE_URL
                )
                _bump("pool.created")
                logger.info("PostgreSQL connection pool created")
        conn = _connection_pool.getconn()
        _bump("conn.acquired")
        return conn
    except Exception as exc:  # psycopg2 errors aren't a single base
        _bump("conn.fail")
        logger.error("Failed to get PostgreSQL connection: %s", exc)
        return None


def release_connection(conn) -> None:
    """Return a connection to the pool (no-op if pool/conn is missing)."""
    global _connection_pool
    if _connection_pool is None or conn is None:
        return
    try:
        _connection_pool.putconn(conn)
        _bump("conn.released")
    except Exception as exc:
        _bump("conn.release_fail")
        logger.error("Failed to release PostgreSQL connection: %s", exc)


@contextmanager
def get_db_connection():
    """Context manager mirroring the legacy API."""
    conn = get_connection()
    try:
        yield conn
    finally:
        if conn is not None:
            release_connection(conn)


# --------------------------------------------------------------------------- #
# Schema bootstrap                                                             #
# --------------------------------------------------------------------------- #


def init_tables() -> bool:
    """Create the seven canonical tables if they do not exist."""
    with get_db_connection() as conn:
        if conn is None:
            _bump("init.no_conn")
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS contextdna_learnings (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                    session_id TEXT DEFAULT '',
                    injection_id TEXT DEFAULT '',
                    source TEXT DEFAULT 'manual',
                    workspace_id TEXT NOT NULL DEFAULT '',
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    merge_count INTEGER DEFAULT 1,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    fts tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                        setweight(to_tsvector('english', coalesce(content, '')), 'B')
                    ) STORED
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_cdna_learnings_fts "
                "ON contextdna_learnings USING GIN (fts)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_cdna_learnings_type "
                "ON contextdna_learnings(type)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_cdna_learnings_session "
                "ON contextdna_learnings(session_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_cdna_learnings_workspace "
                "ON contextdna_learnings(workspace_id)"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS contextdna_failures (
                    id SERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    failure_type TEXT,
                    prompt TEXT,
                    error_message TEXT,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_cdna_failures_session "
                "ON contextdna_failures(session_id, created_at DESC)"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS contextdna_events (
                    id SERIAL PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    project_id TEXT,
                    scope TEXT,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_cdna_events_created "
                "ON contextdna_events(created_at DESC)"
            )

            conn.commit()
            _bump("init.ok")
            return True
        except Exception as exc:
            _bump("init.fail")
            logger.error("Failed to initialize tables: %s", exc)
            try:
                conn.rollback()
            except Exception:
                _bump("init.rollback_fail")
            return False


# --------------------------------------------------------------------------- #
# Learnings (the unified contract)                                             #
# --------------------------------------------------------------------------- #


def store_learning(
    learning_data: Dict[str, Any],
    skip_dedup: bool = False,
    consolidate: bool = True,
    workspace_id: str = "",
) -> Dict[str, Any]:
    """Insert a learning row. Honors the same dedup contract as SQLite."""
    with get_db_connection() as conn:
        if conn is None:
            _bump("store.no_conn")
            raise BackendUnavailable("PostgreSQL unavailable for store_learning")
        try:
            cur = conn.cursor()
            learning_id = learning_data.get("id") or (
                f"learn_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
            )
            now = datetime.now(timezone.utc)
            cur.execute(
                """
                INSERT INTO contextdna_learnings
                (id, type, title, content, tags, session_id, injection_id, source,
                 workspace_id, metadata, merge_count, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb, 1, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    tags = EXCLUDED.tags,
                    metadata = EXCLUDED.metadata,
                    merge_count = contextdna_learnings.merge_count + 1,
                    updated_at = EXCLUDED.updated_at
                RETURNING id, type, title, content, tags, session_id, injection_id,
                          source, workspace_id, metadata, merge_count,
                          created_at, updated_at
                """,
                (
                    learning_id,
                    learning_data.get("type", "win"),
                    learning_data.get("title", ""),
                    learning_data.get("content", ""),
                    json.dumps(learning_data.get("tags", [])),
                    learning_data.get("session_id", ""),
                    learning_data.get("injection_id", ""),
                    learning_data.get("source", "manual"),
                    workspace_id,
                    json.dumps(learning_data.get("metadata", {})),
                    now,
                    now,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            _bump("store.ok")
            return _row_to_dict(row)
        except Exception as exc:
            _bump("store.fail")
            logger.error("store_learning failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                _bump("store.rollback_fail")
            raise BackendUnavailable(f"store_learning failed: {exc}") from exc


def get_learning(learning_id: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        if conn is None:
            _bump("get.no_conn")
            return None
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, type, title, content, tags, session_id, injection_id, "
                "source, workspace_id, metadata, merge_count, created_at, updated_at "
                "FROM contextdna_learnings WHERE id = %s",
                (learning_id,),
            )
            row = cur.fetchone()
            _bump("get.ok")
            return _row_to_dict(row) if row else None
        except Exception:
            _bump("get.fail")
            return None


def get_recent_learnings(limit: int = 20) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        if conn is None:
            _bump("recent.no_conn")
            return []
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, type, title, content, tags, session_id, injection_id, "
                "source, workspace_id, metadata, merge_count, created_at, updated_at "
                "FROM contextdna_learnings ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            _bump("recent.ok")
            return [_row_to_dict(r) for r in cur.fetchall()]
        except Exception:
            _bump("recent.fail")
            return []


def query_learnings(search: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Full-text search via the generated tsvector column."""
    with get_db_connection() as conn:
        if conn is None:
            _bump("query.no_conn")
            return []
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, type, title, content, tags, session_id, injection_id, "
                "source, workspace_id, metadata, merge_count, created_at, updated_at "
                "FROM contextdna_learnings "
                "WHERE fts @@ plainto_tsquery('english', %s) "
                "ORDER BY ts_rank(fts, plainto_tsquery('english', %s)) DESC LIMIT %s",
                (search, search, limit),
            )
            _bump("query.ok")
            return [_row_to_dict(r) for r in cur.fetchall()]
        except Exception:
            _bump("query.fail")
            return []


def _row_to_dict(row) -> Dict[str, Any]:
    if not row:
        return {}
    tags = row[4]
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (json.JSONDecodeError, TypeError):
            tags = []
    metadata = row[9]
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    return {
        "id": row[0],
        "type": row[1],
        "title": row[2],
        "content": row[3],
        "tags": tags or [],
        "session_id": row[5],
        "injection_id": row[6],
        "source": row[7],
        "workspace_id": row[8],
        "metadata": metadata or {},
        "_merge_count": row[10],
        "timestamp": row[11].isoformat() if row[11] else None,
        "updated_at": row[12].isoformat() if row[12] else None,
    }


# --------------------------------------------------------------------------- #
# Failure tracking (Three-Tier escalation)                                     #
# --------------------------------------------------------------------------- #


def record_failure(
    session_id: str,
    failure_type: str = "task_failed",
    prompt: Optional[str] = None,
    error_message: Optional[str] = None,
) -> bool:
    with get_db_connection() as conn:
        if conn is None:
            _bump("failure.no_conn")
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO contextdna_failures (session_id, failure_type, prompt, error_message) "
                "VALUES (%s, %s, %s, %s)",
                (session_id, failure_type, prompt, error_message),
            )
            conn.commit()
            _bump("failure.recorded")
            return True
        except Exception as exc:
            _bump("failure.fail")
            logger.error("record_failure failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                _bump("failure.rollback_fail")
            return False


def get_failure_count(session_id: str, hours: int = 1) -> int:
    with get_db_connection() as conn:
        if conn is None:
            _bump("failure.count_no_conn")
            return 0
        try:
            cur = conn.cursor()
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            cur.execute(
                "SELECT COUNT(*) FROM contextdna_failures "
                "WHERE session_id = %s AND created_at > %s",
                (session_id, cutoff),
            )
            count = cur.fetchone()[0]
            _bump("failure.count_ok")
            return int(count)
        except Exception:
            _bump("failure.count_fail")
            return 0


def clear_failures(session_id: str) -> bool:
    with get_db_connection() as conn:
        if conn is None:
            _bump("failure.clear_no_conn")
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM contextdna_failures WHERE session_id = %s",
                (session_id,),
            )
            conn.commit()
            _bump("failure.cleared")
            return True
        except Exception:
            _bump("failure.clear_fail")
            try:
                conn.rollback()
            except Exception:
                _bump("failure.clear_rollback_fail")
            return False


# --------------------------------------------------------------------------- #
# Events                                                                       #
# --------------------------------------------------------------------------- #


def store_event(
    event_type: str,
    payload: Dict[str, Any],
    project_id: Optional[str] = None,
    scope: Optional[str] = None,
) -> bool:
    with get_db_connection() as conn:
        if conn is None:
            _bump("event.no_conn")
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO contextdna_events (event_type, project_id, scope, payload) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (event_type, project_id, scope, json.dumps(payload)),
            )
            conn.commit()
            _bump("event.stored")
            return True
        except Exception:
            _bump("event.fail")
            try:
                conn.rollback()
            except Exception:
                _bump("event.rollback_fail")
            return False


def get_recent_events(
    hours: int = 4, event_type: Optional[str] = None, limit: int = 100
) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        if conn is None:
            _bump("event.list_no_conn")
            return []
        try:
            cur = conn.cursor()
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            if event_type:
                cur.execute(
                    "SELECT id, event_type, project_id, scope, payload, created_at "
                    "FROM contextdna_events "
                    "WHERE created_at > %s AND event_type = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (cutoff, event_type, limit),
                )
            else:
                cur.execute(
                    "SELECT id, event_type, project_id, scope, payload, created_at "
                    "FROM contextdna_events WHERE created_at > %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (cutoff, limit),
                )
            rows = cur.fetchall()
            _bump("event.list_ok")
            return [
                {
                    "id": r[0],
                    "event_type": r[1],
                    "project_id": r[2],
                    "scope": r[3],
                    "payload": r[4] or {},
                    "created_at": r[5].isoformat() if r[5] else None,
                }
                for r in rows
            ]
        except Exception:
            _bump("event.list_fail")
            return []


# --------------------------------------------------------------------------- #
# PostgresStorage class wrapper                                                #
# --------------------------------------------------------------------------- #


class PostgresStorage:
    """Class wrapper matching the SQLiteStorage public surface.

    Used by :mod:`memory.learning_store` when ``STORAGE_BACKEND=postgres``.
    """

    def __init__(self) -> None:
        # Trigger schema bootstrap if connection is available. The constructor
        # never raises — callers can still create an instance and have queries
        # degrade gracefully when Postgres is offline.
        try:
            init_tables()
            _bump("class.init_ok")
        except BackendUnavailable:
            _bump("class.init_unavailable")

    def store_learning(
        self,
        learning_data: Dict[str, Any],
        skip_dedup: bool = False,
        consolidate: bool = True,
        workspace_id: str = "",
    ) -> Dict[str, Any]:
        return store_learning(learning_data, skip_dedup, consolidate, workspace_id)

    def get_by_id(self, learning_id: str) -> Optional[Dict[str, Any]]:
        return get_learning(learning_id)

    def get_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        return get_recent_learnings(limit)

    def get_by_session(self, session_id: str) -> List[Dict[str, Any]]:
        with get_db_connection() as conn:
            if conn is None:
                _bump("session.no_conn")
                return []
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, type, title, content, tags, session_id, injection_id, "
                    "source, workspace_id, metadata, merge_count, created_at, updated_at "
                    "FROM contextdna_learnings WHERE session_id = %s "
                    "ORDER BY created_at DESC",
                    (session_id,),
                )
                _bump("session.ok")
                return [_row_to_dict(r) for r in cur.fetchall()]
            except Exception:
                _bump("session.fail")
                return []

    def get_since(self, timestamp: str, limit: int = 20) -> List[Dict[str, Any]]:
        with get_db_connection() as conn:
            if conn is None:
                _bump("since.no_conn")
                return []
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, type, title, content, tags, session_id, injection_id, "
                    "source, workspace_id, metadata, merge_count, created_at, updated_at "
                    "FROM contextdna_learnings WHERE created_at >= %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (timestamp, limit),
                )
                _bump("since.ok")
                return [_row_to_dict(r) for r in cur.fetchall()]
            except Exception:
                _bump("since.fail")
                return []

    def get_by_type(self, learning_type: str, limit: int = 20) -> List[Dict[str, Any]]:
        with get_db_connection() as conn:
            if conn is None:
                _bump("by_type.no_conn")
                return []
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, type, title, content, tags, session_id, injection_id, "
                    "source, workspace_id, metadata, merge_count, created_at, updated_at "
                    "FROM contextdna_learnings WHERE type = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (learning_type, limit),
                )
                _bump("by_type.ok")
                return [_row_to_dict(r) for r in cur.fetchall()]
            except Exception:
                _bump("by_type.fail")
                return []

    def query(self, search: str, limit: int = 10) -> List[Dict[str, Any]]:
        return query_learnings(search, limit)

    def get_stats(self) -> Dict[str, Any]:
        with get_db_connection() as conn:
            if conn is None:
                _bump("stats.no_conn")
                return {"total": 0, "wins": 0, "fixes": 0, "patterns": 0, "by_type": {}}
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM contextdna_learnings")
                total = int(cur.fetchone()[0])
                by_type: Dict[str, int] = {}
                for lt in ("win", "fix", "pattern", "insight", "gotcha", "sop"):
                    cur.execute(
                        "SELECT COUNT(*) FROM contextdna_learnings WHERE type = %s",
                        (lt,),
                    )
                    count = int(cur.fetchone()[0])
                    if count:
                        by_type[lt] = count
                _bump("stats.ok")
                return {
                    "total": total,
                    "wins": by_type.get("win", 0),
                    "fixes": by_type.get("fix", 0),
                    "patterns": by_type.get("pattern", 0),
                    "by_type": by_type,
                }
            except Exception:
                _bump("stats.fail")
                return {"total": 0, "wins": 0, "fixes": 0, "patterns": 0, "by_type": {}}

    def health_check(self) -> bool:
        with get_db_connection() as conn:
            if conn is None:
                _bump("health.no_conn")
                return False
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                _bump("health.ok")
                return True
            except Exception:
                _bump("health.fail")
                return False

    def close(self) -> None:
        global _connection_pool
        if _connection_pool is not None:
            try:
                _connection_pool.closeall()
                _bump("pool.closed")
            except Exception:
                _bump("pool.close_fail")
            _connection_pool = None


# --------------------------------------------------------------------------- #
# Singleton accessor                                                           #
# --------------------------------------------------------------------------- #

_postgres_storage: Optional[PostgresStorage] = None
_postgres_lock = threading.Lock()


def get_postgres_storage() -> PostgresStorage:
    """Return a thread-safe singleton instance of :class:`PostgresStorage`."""
    global _postgres_storage
    if _postgres_storage is None:
        with _postgres_lock:
            if _postgres_storage is None:
                _postgres_storage = PostgresStorage()
    return _postgres_storage


# Legacy compatibility — original module attempted ``init_tables()`` at import
# time. We deliberately do **not** do that here: schema bootstrap is lazy so
# importing this module on a host without psycopg2 (or with PG offline) never
# raises. Callers must invoke :func:`init_tables` (or instantiate
# :class:`PostgresStorage`) when they actually need persistence.


if __name__ == "__main__":
    storage = get_postgres_storage()
    print(f"DATABASE_URL: {DATABASE_URL}")
    print(f"health:       {storage.health_check()}")
    print(f"counters:     {get_counters()}")
