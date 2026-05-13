#!/usr/bin/env python3
"""
Learning Store — Unified facade over sqlite_storage and postgres_storage
========================================================================

High-level CRUD over the ``learnings`` table. Routes every read/write through
whichever backend is configured via the ``STORAGE_BACKEND`` env var:

    STORAGE_BACKEND=sqlite     # explicit SQLite (default if neither chosen)
    STORAGE_BACKEND=postgres   # explicit PostgreSQL (requires psycopg2)
    STORAGE_BACKEND=auto       # prefer Postgres, fall back to SQLite

Public API (the contract every consumer relies on):

    LearningStore                     # class
    get_learning_store()              # singleton accessor
    record_learning(...)              # convenience: store + return id
    get_learnings(...)                # combined recent/session/type filter
    promote(id, **fields)             # mark learning as promoted
    retire(id)                        # soft-delete (sets type='retired')
    build_learning_data(...)          # legacy helper used by the webhook

Backends are loaded lazily — importing this module never touches a database.

ZSF:
    Every public method increments a counter via :func:`get_counters`. A
    silent failure here would break observability for every webhook write.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger("contextdna.learning_store")


# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #


def _selected_backend() -> str:
    raw = os.environ.get("STORAGE_BACKEND", "auto").lower().strip()
    if raw not in {"sqlite", "postgres", "auto"}:
        return "auto"
    return raw


MEMORY_DIR = Path(__file__).resolve().parent
LEARNING_HISTORY_FILE = MEMORY_DIR / ".learning_history.json"  # legacy JSON backup
MAX_HISTORY = 100


# --------------------------------------------------------------------------- #
# Counters (ZSF)                                                               #
# --------------------------------------------------------------------------- #


_COUNTERS: Counter[str] = Counter()
_COUNTER_LOCK = threading.Lock()


def _bump(key: str) -> None:
    with _COUNTER_LOCK:
        _COUNTERS[key] += 1


def get_counters() -> Dict[str, int]:
    with _COUNTER_LOCK:
        return dict(_COUNTERS)


# --------------------------------------------------------------------------- #
# Backend resolution                                                           #
# --------------------------------------------------------------------------- #


def _load_sqlite():
    try:
        from memory.sqlite_storage import get_sqlite_storage  # type: ignore
        return get_sqlite_storage()
    except ImportError:
        try:
            from sqlite_storage import get_sqlite_storage  # type: ignore
            return get_sqlite_storage()
        except ImportError:
            _bump("backend.sqlite_missing")
            return None


def _load_postgres():
    try:
        from memory.postgres_storage import (  # type: ignore
            get_postgres_storage,
            BackendUnavailable,
        )
    except ImportError:
        try:
            from postgres_storage import (  # type: ignore
                get_postgres_storage,
                BackendUnavailable,
            )
        except ImportError:
            _bump("backend.postgres_missing")
            return None
    try:
        storage = get_postgres_storage()
        if not storage.health_check():
            _bump("backend.postgres_unhealthy")
            return None
        return storage
    except BackendUnavailable:
        _bump("backend.postgres_unavailable")
        return None


def _resolve_backend():
    """Return the active backend object based on STORAGE_BACKEND env."""
    choice = _selected_backend()
    if choice == "postgres":
        backend = _load_postgres()
        if backend is None:
            _bump("backend.postgres_forced_fail")
            logger.warning(
                "STORAGE_BACKEND=postgres but backend unavailable — using SQLite"
            )
            return _load_sqlite()
        _bump("backend.postgres_used")
        return backend
    if choice == "sqlite":
        _bump("backend.sqlite_used")
        return _load_sqlite()
    # auto: prefer postgres, fall back to sqlite
    backend = _load_postgres()
    if backend is not None:
        _bump("backend.auto_postgres")
        return backend
    _bump("backend.auto_sqlite")
    return _load_sqlite()


# --------------------------------------------------------------------------- #
# Unified store                                                                #
# --------------------------------------------------------------------------- #


class LearningStore:
    """Unified high-level CRUD over the active storage backend."""

    def __init__(self) -> None:
        self._backend = _resolve_backend()
        self._ensure_json_backup()

    # -- JSON backup (legacy compat) ------------------------------------- #

    def _ensure_json_backup(self) -> None:
        if not LEARNING_HISTORY_FILE.exists():
            try:
                LEARNING_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(LEARNING_HISTORY_FILE, "w") as fh:
                    json.dump([], fh)
            except OSError:
                _bump("json_backup.init_fail")

    def _read_json_backup(self) -> List[Dict[str, Any]]:
        try:
            with open(LEARNING_HISTORY_FILE, "r") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        except OSError:
            _bump("json_backup.read_fail")
            return []

    def _write_json_backup(self, history: List[Dict[str, Any]]) -> None:
        try:
            tmp = LEARNING_HISTORY_FILE.with_suffix(".tmp")
            with open(tmp, "w") as fh:
                json.dump(history, fh, indent=2, default=str)
            tmp.replace(LEARNING_HISTORY_FILE)
        except OSError:
            _bump("json_backup.write_fail")

    def _sync_to_json(self, learning: Dict[str, Any]) -> None:
        history = self._read_json_backup() or []
        for idx, existing in enumerate(history):
            if existing.get("id") == learning.get("id"):
                history[idx] = learning
                break
        else:
            history.insert(0, learning)
            history = history[:MAX_HISTORY]
        self._write_json_backup(history)

    # -- CRUD ------------------------------------------------------------ #

    def store_learning(
        self,
        learning_data: Dict[str, Any],
        skip_dedup: bool = False,
        consolidate: bool = True,
    ) -> Dict[str, Any]:
        if self._backend is None:
            _bump("store.no_backend")
            raise RuntimeError("No storage backend available")
        try:
            result = self._backend.store_learning(
                learning_data,
                skip_dedup=skip_dedup,
                consolidate=consolidate,
            )
            self._sync_to_json(result)
            _bump("store.ok")
            return result
        except Exception as exc:
            _bump("store.fail")
            logger.error("LearningStore.store_learning failed: %s", exc)
            raise

    def get_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        if self._backend is None:
            _bump("recent.no_backend")
            return self._read_json_backup()[:limit]
        try:
            results = self._backend.get_recent(limit)
            _bump("recent.ok")
            return results
        except Exception:
            _bump("recent.fail")
            return []

    def get_by_session(self, session_id: str) -> List[Dict[str, Any]]:
        if self._backend is None:
            _bump("by_session.no_backend")
            return [
                l for l in self._read_json_backup()
                if l.get("session_id") == session_id
            ]
        try:
            results = self._backend.get_by_session(session_id)
            _bump("by_session.ok")
            return results
        except Exception:
            _bump("by_session.fail")
            return []

    def get_since(self, timestamp: str, limit: int = 20) -> List[Dict[str, Any]]:
        if self._backend is None:
            _bump("since.no_backend")
            return []
        try:
            results = self._backend.get_since(timestamp, limit)
            _bump("since.ok")
            return results
        except Exception:
            _bump("since.fail")
            return []

    def get_by_type(self, learning_type: str, limit: int = 20) -> List[Dict[str, Any]]:
        if self._backend is None:
            _bump("by_type.no_backend")
            return [
                l for l in self._read_json_backup()
                if l.get("type") == learning_type
            ][:limit]
        try:
            results = self._backend.get_by_type(learning_type, limit)
            _bump("by_type.ok")
            return results
        except Exception:
            _bump("by_type.fail")
            return []

    def get_by_id(self, learning_id: str) -> Optional[Dict[str, Any]]:
        if self._backend is None:
            _bump("by_id.no_backend")
            for l in self._read_json_backup():
                if l.get("id") == learning_id:
                    return l
            return None
        try:
            row = self._backend.get_by_id(learning_id)
            _bump("by_id.ok")
            if row is None:
                return None
            # SQLite returns a Row, Postgres returns a dict — normalize.
            if hasattr(self._backend, "_row_to_dict") and not isinstance(row, dict):
                return self._backend._row_to_dict(row)
            return row
        except Exception:
            _bump("by_id.fail")
            return None

    def get_stats(self) -> Dict[str, Any]:
        if self._backend is None:
            _bump("stats.no_backend")
            history = self._read_json_backup()
            return {
                "total": len(history),
                "wins": sum(1 for l in history if l.get("type") == "win"),
                "fixes": sum(1 for l in history if l.get("type") == "fix"),
                "patterns": sum(1 for l in history if l.get("type") == "pattern"),
                "backend": "json",
            }
        try:
            stats = self._backend.get_stats()
            stats["backend"] = self._backend.__class__.__name__
            _bump("stats.ok")
            return stats
        except Exception:
            _bump("stats.fail")
            return {"total": 0, "backend": "error"}

    def query(self, search: str, limit: int = 10) -> List[Dict[str, Any]]:
        if self._backend is None:
            _bump("query.no_backend")
            return []
        try:
            results = self._backend.query(search, limit=limit)
            _bump("query.ok")
            return results
        except Exception:
            _bump("query.fail")
            return []

    # -- mutation: promote / retire -------------------------------------- #

    def promote(self, learning_id: str, **fields: Any) -> bool:
        """Mark a learning as promoted (e.g. to SOP).

        ``fields`` is merged into the learning's metadata dict. Reuses
        ``store_learning`` for the underlying upsert.
        """
        existing = self.get_by_id(learning_id)
        if not existing:
            _bump("promote.not_found")
            return False
        metadata = existing.get("metadata") or {}
        metadata.update(fields)
        metadata.setdefault("promoted_at", datetime.now(timezone.utc).isoformat())
        existing["metadata"] = metadata
        existing["type"] = fields.get("type", "sop")
        try:
            self.store_learning(existing, skip_dedup=True, consolidate=False)
            _bump("promote.ok")
            return True
        except Exception:
            _bump("promote.fail")
            return False

    def retire(self, learning_id: str) -> bool:
        """Soft-delete a learning by retagging it as ``retired``."""
        existing = self.get_by_id(learning_id)
        if not existing:
            _bump("retire.not_found")
            return False
        existing["type"] = "retired"
        metadata = existing.get("metadata") or {}
        metadata["retired_at"] = datetime.now(timezone.utc).isoformat()
        existing["metadata"] = metadata
        try:
            self.store_learning(existing, skip_dedup=True, consolidate=False)
            _bump("retire.ok")
            return True
        except Exception:
            _bump("retire.fail")
            return False

    def associate_with_injection(self, learning_id: str, injection_id: str) -> bool:
        existing = self.get_by_id(learning_id)
        if not existing:
            _bump("associate.not_found")
            return False
        existing["injection_id"] = injection_id
        try:
            self.store_learning(existing, skip_dedup=True, consolidate=False)
            _bump("associate.ok")
            return True
        except Exception:
            _bump("associate.fail")
            return False

    # -- introspection --------------------------------------------------- #

    def backend_name(self) -> str:
        if self._backend is None:
            return "none"
        return self._backend.__class__.__name__


# --------------------------------------------------------------------------- #
# Singleton accessor                                                           #
# --------------------------------------------------------------------------- #

_learning_store: Optional[LearningStore] = None
_learning_store_lock = threading.Lock()


def get_learning_store() -> LearningStore:
    """Return the singleton :class:`LearningStore` instance."""
    global _learning_store
    if _learning_store is None:
        with _learning_store_lock:
            if _learning_store is None:
                _learning_store = LearningStore()
    return _learning_store


# --------------------------------------------------------------------------- #
# Module-level convenience API (the contract Synaptic asked for)               #
# --------------------------------------------------------------------------- #


def record_learning(
    learning_type: str,
    title: str,
    content: str,
    tags: Optional[List[str]] = None,
    session_id: str = "",
    injection_id: str = "",
    source: str = "manual",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Convenience helper — build + store + return the learning id."""
    data = build_learning_data(
        learning_type=learning_type,
        title=title,
        content=content,
        tags=tags,
        session_id=session_id,
        injection_id=injection_id,
        source=source,
        metadata=metadata,
    )
    stored = get_learning_store().store_learning(data)
    return stored.get("id", "")


def get_learnings(
    *,
    session_id: Optional[str] = None,
    learning_type: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Filterable read — used by webhook / dashboard / xbar."""
    store = get_learning_store()
    if session_id:
        return store.get_by_session(session_id)[:limit]
    if learning_type:
        return store.get_by_type(learning_type, limit=limit)
    if since:
        return store.get_since(since, limit=limit)
    return store.get_recent(limit=limit)


def promote(learning_id: str, **fields: Any) -> bool:
    return get_learning_store().promote(learning_id, **fields)


def retire(learning_id: str) -> bool:
    return get_learning_store().retire(learning_id)


def build_learning_data(
    learning_type: str,
    title: str,
    content: str,
    tags: Optional[List[str]] = None,
    session_id: str = "",
    injection_id: str = "",
    source: str = "manual",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "type": learning_type,
        "title": title,
        "content": content,
        "tags": tags or [],
        "session_id": session_id,
        "injection_id": injection_id,
        "source": source,
        "metadata": metadata or {},
        "timestamp": datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z",
    }


if __name__ == "__main__":
    store = get_learning_store()
    print(f"backend:  {store.backend_name()}")
    print(f"stats:    {store.get_stats()}")
    print(f"counters: {get_counters()}")
