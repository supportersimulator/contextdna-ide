#!/usr/bin/env python3
"""
SQLite Storage Backend — Unified Storage Layer (SQLite half)
============================================================

Thin wrapper around SQLite providing the contract every Context DNA consumer
relies on. The public surface is preserved verbatim from the legacy module so
existing imports keep working:

    from memory.sqlite_storage import get_sqlite_storage, SQLiteStorage

Database location (configurable, in priority order):
    1. $CONTEXT_DNA_LEARNINGS_DB  — explicit override (used by tests, sidecars)
    2. $CONTEXT_DNA_DIR/learnings.db
    3. ~/.context-dna/learnings.db  (default user-data location)
    4. <module-dir>/.context-dna.db (repo-local fallback when home unavailable)

Hard-coded path callers that lived on the SQLite backend (xbar plugin, helper
agent, dashboard) continue to point at ~/.context-dna/learnings.db; this module
keeps that path canonical.

ZSF compliance:
    - Every method increments a counter on success/failure (no silent except).
    - Connection errors surface as ``StorageError`` with the cause attached.
    - ``health_check`` returns False instead of raising, but the counter still
      records the failure.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #

USER_DATA_DIR = Path(
    os.environ.get("CONTEXT_DNA_DIR", str(Path.home() / ".context-dna"))
)
MEMORY_DIR = Path(__file__).resolve().parent


def get_db_path() -> Path:
    """Resolve the SQLite database path.

    Order:
        1. $CONTEXT_DNA_LEARNINGS_DB  (explicit override)
        2. $CONTEXT_DNA_DIR/learnings.db
        3. ~/.context-dna/learnings.db
        4. <module-dir>/.context-dna.db (repo-local fallback)
    """
    explicit = os.environ.get("CONTEXT_DNA_LEARNINGS_DB", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    user_db = USER_DATA_DIR / "learnings.db"
    try:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        return user_db
    except OSError:
        return MEMORY_DIR / ".context-dna.db"


DB_PATH = get_db_path()


# --------------------------------------------------------------------------- #
# Errors + counters (ZSF)                                                      #
# --------------------------------------------------------------------------- #


class StorageError(RuntimeError):
    """Raised when a SQLite operation fails after exhausting recovery."""


# Module-level per-method counter; readable via :func:`get_counters`.
_COUNTERS: Counter[str] = Counter()
_COUNTER_LOCK = threading.Lock()


def _bump(key: str) -> None:
    with _COUNTER_LOCK:
        _COUNTERS[key] += 1


def get_counters() -> Dict[str, int]:
    """Return a snapshot of operation counters (success + failure)."""
    with _COUNTER_LOCK:
        return dict(_COUNTERS)


# --------------------------------------------------------------------------- #
# Storage class                                                                #
# --------------------------------------------------------------------------- #


class SQLiteStorage:
    """SQLite storage with FTS5 search, smart dedup, and ZSF counters."""

    SCHEMA_VERSION = 3

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None,  # autocommit
            )
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA cache_size=10000")
        except sqlite3.Error as exc:
            _bump("connect.fail")
            raise StorageError(f"SQLite connect failed: {exc}") from exc

        self._device_id = self._get_or_create_device_id()
        self._init_schema()
        _bump("connect.ok")

    # -- schema ---------------------------------------------------------- #

    def _init_schema(self) -> None:
        cur = self.conn
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS learnings (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    session_id TEXT DEFAULT '',
                    injection_id TEXT DEFAULT '',
                    source TEXT DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    merge_count INTEGER DEFAULT 1,
                    workspace_id TEXT NOT NULL DEFAULT ''
                )
                """
            )

            cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS learnings_fts USING fts5(
                    title, content, tags,
                    content=learnings, content_rowid=rowid
                )
                """
            )

            for trigger in (
                """
                CREATE TRIGGER IF NOT EXISTS learnings_ai AFTER INSERT ON learnings BEGIN
                    INSERT INTO learnings_fts(rowid, title, content, tags)
                    VALUES (new.rowid, new.title, new.content, new.tags);
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS learnings_ad AFTER DELETE ON learnings BEGIN
                    INSERT INTO learnings_fts(learnings_fts, rowid, title, content, tags)
                    VALUES ('delete', old.rowid, old.title, old.content, old.tags);
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS learnings_au AFTER UPDATE ON learnings BEGIN
                    INSERT INTO learnings_fts(learnings_fts, rowid, title, content, tags)
                    VALUES ('delete', old.rowid, old.title, old.content, old.tags);
                    INSERT INTO learnings_fts(rowid, title, content, tags)
                    VALUES (new.rowid, new.title, new.content, new.tags);
                END
                """,
            ):
                cur.execute(trigger)

            cur.execute("CREATE INDEX IF NOT EXISTS idx_learnings_type ON learnings(type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_learnings_created ON learnings(created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_learnings_session ON learnings(session_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_learnings_workspace ON learnings(workspace_id)")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS stats (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "INSERT OR IGNORE INTO stats (key, value) VALUES ('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS device_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS negative_patterns (
                    pattern_key TEXT PRIMARY KEY,
                    description TEXT,
                    goal TEXT DEFAULT '',
                    first_seen TEXT,
                    last_seen TEXT,
                    frequency INTEGER DEFAULT 1,
                    promoted_to_sop INTEGER DEFAULT 0,
                    context_samples TEXT DEFAULT '[]'
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_neg_goal ON negative_patterns(goal)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_neg_freq ON negative_patterns(frequency DESC)")
            _bump("schema.ok")
        except sqlite3.Error as exc:
            _bump("schema.fail")
            raise StorageError(f"Schema init failed: {exc}") from exc

    # -- device identity ------------------------------------------------- #

    def _get_or_create_device_id(self) -> str:
        device_file = USER_DATA_DIR / "device.json"
        if device_file.exists():
            try:
                with open(device_file, "r") as fh:
                    data = json.load(fh)
                if "device_id" in data:
                    return data["device_id"]
            except (json.JSONDecodeError, OSError):
                _bump("device.read_fail")

        device_id = str(uuid.uuid4())
        try:
            device_file.parent.mkdir(parents=True, exist_ok=True)
            with open(device_file, "w") as fh:
                json.dump(
                    {
                        "device_id": device_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "linked_to_backend": False,
                    },
                    fh,
                    indent=2,
                )
            _bump("device.created")
        except OSError:
            _bump("device.write_fail")
        return device_id

    @property
    def device_id(self) -> str:
        return self._device_id

    def get_device_info(self) -> Dict[str, Any]:
        device_file = USER_DATA_DIR / "device.json"
        if device_file.exists():
            try:
                with open(device_file, "r") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                _bump("device.info_fail")
        return {"device_id": self._device_id, "linked_to_backend": False}

    def link_to_backend(self, user_email: str, device_token: str) -> bool:
        device_file = USER_DATA_DIR / "device.json"
        try:
            data = self.get_device_info()
            data["user_email"] = user_email
            data["device_token"] = device_token
            data["linked_to_backend"] = True
            data["linked_at"] = datetime.now(timezone.utc).isoformat()
            with open(device_file, "w") as fh:
                json.dump(data, fh, indent=2)
            _bump("device.linked")
            return True
        except OSError:
            _bump("device.link_fail")
            return False

    # -- internal helpers ------------------------------------------------ #

    def _row_to_dict(self, row: Optional[sqlite3.Row]) -> Dict[str, Any]:
        if not row:
            return {}
        return {
            "id": row["id"],
            "type": row["type"],
            "title": row["title"],
            "content": row["content"],
            "tags": json.loads(row["tags"]) if row["tags"] and row["tags"].strip() else [],
            "session_id": row["session_id"],
            "injection_id": row["injection_id"],
            "source": row["source"],
            "timestamp": row["created_at"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] and row["metadata"].strip() else {},
            "_merge_count": row["merge_count"],
            "workspace_id": row["workspace_id"],
        }

    def _find_duplicate(
        self,
        new_learning: Dict[str, Any],
        time_window_hours: int = 24,
        workspace_id: str = "",
    ) -> Optional[sqlite3.Row]:
        new_title = (new_learning.get("title") or "").lower().strip()
        if not new_title:
            return None

        def core(t: str) -> str:
            c = t.lower().strip()
            for pat in (
                r"^\[process sop\]\s*",
                r"^\[bug-fix sop\]\s*",
                r"^agent success:\s*",
                r"^fix:\s*",
            ):
                c = re.sub(pat, "", c)
            return c.strip()

        new_core = core(new_title)
        new_words = set(new_title.split())
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=time_window_hours)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM learnings WHERE created_at >= ? AND workspace_id = ? "
            "ORDER BY created_at DESC",
            (cutoff, workspace_id),
        ).fetchall()

        for row in rows:
            existing_title = (row["title"] or "").lower().strip()
            existing_words = set(existing_title.split())
            if new_words and existing_words:
                overlap = len(new_words & existing_words)
                ratio = overlap / max(len(new_words), len(existing_words))
                if ratio >= 0.8:
                    return row
            existing_core = core(existing_title)
            if new_core and existing_core and new_core == existing_core:
                return row
        return None

    def _smart_merge(self, existing: sqlite3.Row, new: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(existing)
        new_title = new.get("title") or ""
        if len(new_title) > len(existing["title"] or ""):
            merged["title"] = new_title

        new_content = new.get("content") or ""
        existing_content = existing["content"] or ""
        if len(new_content) > len(existing_content):
            merged["content"] = new_content

        existing_tags = json.loads(existing["tags"] or "[]")
        new_tags = new.get("tags") or []
        merged["tags"] = json.dumps(list(set(existing_tags) | set(new_tags)))
        merged["merge_count"] = (existing["merge_count"] or 1) + 1
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        return merged

    # -- public CRUD ----------------------------------------------------- #

    def store_learning(
        self,
        learning_data: Dict[str, Any],
        skip_dedup: bool = False,
        consolidate: bool = True,
        workspace_id: str = "",
    ) -> Dict[str, Any]:
        try:
            if not skip_dedup:
                dup = self._find_duplicate(learning_data, workspace_id=workspace_id)
                if dup is not None:
                    MAX_MERGE = 3
                    if consolidate and (dup["merge_count"] or 1) < MAX_MERGE:
                        merged = self._smart_merge(dup, learning_data)
                        self.conn.execute(
                            "UPDATE learnings SET title=?, content=?, tags=?, "
                            "merge_count=?, updated_at=? WHERE id=?",
                            (
                                merged["title"],
                                merged["content"],
                                merged["tags"],
                                merged["merge_count"],
                                merged["updated_at"],
                                dup["id"],
                            ),
                        )
                        result = self._row_to_dict(self.get_by_id(dup["id"]))
                        result["_consolidated"] = True
                        _bump("store.consolidated")
                        return result
                    result = self._row_to_dict(dup)
                    result["_duplicate"] = True
                    _bump("store.duplicate")
                    return result

            learning_id = learning_data.get("id") or (
                f"learn_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
            )
            now = datetime.now(timezone.utc).isoformat()
            # Upsert: skip_dedup callers (promote/retire/associate) rely on
            # re-saving an existing row by id without colliding on PK.
            existing_for_upsert = self.get_by_id(learning_id) if skip_dedup else None
            if existing_for_upsert is not None:
                self.conn.execute(
                    """
                    UPDATE learnings SET
                        type=?, title=?, content=?, tags=?,
                        session_id=?, injection_id=?, source=?,
                        updated_at=?, metadata=?, workspace_id=?
                    WHERE id=?
                    """,
                    (
                        learning_data.get("type", existing_for_upsert["type"]),
                        learning_data.get("title", existing_for_upsert["title"]),
                        learning_data.get("content", existing_for_upsert["content"]),
                        json.dumps(learning_data.get("tags", [])),
                        learning_data.get("session_id", existing_for_upsert["session_id"]),
                        learning_data.get("injection_id", existing_for_upsert["injection_id"]),
                        learning_data.get("source", existing_for_upsert["source"]),
                        now,
                        json.dumps(learning_data.get("metadata", {})),
                        workspace_id or existing_for_upsert["workspace_id"],
                        learning_id,
                    ),
                )
                _bump("store.upsert")
            else:
                self.conn.execute(
                    """
                    INSERT INTO learnings
                    (id, type, title, content, tags, session_id, injection_id, source,
                     created_at, updated_at, metadata, merge_count, workspace_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
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
                        learning_data.get("timestamp") or now,
                        now,
                        json.dumps(learning_data.get("metadata", {})),
                        workspace_id,
                    ),
                )
                _bump("store.ok")
            stored = self.get_by_id(learning_id)
            return self._row_to_dict(stored) if stored else learning_data
        except sqlite3.Error as exc:
            _bump("store.fail")
            raise StorageError(f"store_learning failed: {exc}") from exc

    def get_by_id(self, learning_id: str) -> Optional[sqlite3.Row]:
        try:
            row = self.conn.execute(
                "SELECT * FROM learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            _bump("get_by_id.ok")
            return row
        except sqlite3.Error:
            _bump("get_by_id.fail")
            return None

    def get_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM learnings ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            _bump("get_recent.ok")
            return [self._row_to_dict(r) for r in rows]
        except sqlite3.Error:
            _bump("get_recent.fail")
            return []

    def get_by_session(self, session_id: str) -> List[Dict[str, Any]]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM learnings WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
            _bump("get_by_session.ok")
            return [self._row_to_dict(r) for r in rows]
        except sqlite3.Error:
            _bump("get_by_session.fail")
            return []

    def get_since(self, timestamp: str, limit: int = 20) -> List[Dict[str, Any]]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM learnings WHERE created_at >= ? "
                "ORDER BY created_at DESC LIMIT ?",
                (timestamp, limit),
            ).fetchall()
            _bump("get_since.ok")
            return [self._row_to_dict(r) for r in rows]
        except sqlite3.Error:
            _bump("get_since.fail")
            return []

    def get_by_type(self, learning_type: str, limit: int = 20) -> List[Dict[str, Any]]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM learnings WHERE type = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (learning_type, limit),
            ).fetchall()
            _bump("get_by_type.ok")
            return [self._row_to_dict(r) for r in rows]
        except sqlite3.Error:
            _bump("get_by_type.fail")
            return []

    def query(
        self,
        search: str,
        limit: int = 10,
        workspace_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """FTS5 OR-matching with keyword fallback. Workspace-aware."""
        stop = {
            "the", "a", "an", "is", "are", "was", "were", "be", "to", "of",
            "in", "for", "on", "with", "at", "by", "from", "it", "this",
            "that", "and", "or", "but", "not",
        }
        words = [w for w in search.lower().split() if w not in stop and len(w) > 1]
        if not words:
            words = search.lower().split()

        ws_clause = " AND l.workspace_id = ?" if workspace_id is not None else ""
        ws_params = [workspace_id] if workspace_id is not None else []

        if words:
            fts_terms = " OR ".join(w.replace('"', '""') for w in words[:8])
            try:
                rows = self.conn.execute(
                    f"SELECT l.* FROM learnings l "
                    f"JOIN learnings_fts fts ON l.rowid = fts.rowid "
                    f"WHERE learnings_fts MATCH ? "
                    f"AND COALESCE(l.type, '') != 'prompt_leak'"
                    f"{ws_clause} ORDER BY rank LIMIT ?",
                    (fts_terms, *ws_params, limit),
                ).fetchall()
                if rows:
                    _bump("query.fts_ok")
                    return [self._row_to_dict(r) for r in rows]
            except sqlite3.Error:
                _bump("query.fts_fail")

        # Keyword scoring fallback
        try:
            ws_bare = " AND workspace_id = ?" if workspace_id is not None else ""
            all_rows = self.conn.execute(
                f"SELECT * FROM learnings WHERE COALESCE(type, '') != 'prompt_leak'"
                f"{ws_bare} ORDER BY created_at DESC LIMIT 200",
                (*ws_params,),
            ).fetchall()
            query_words = set(words) if words else set(search.lower().split())
            scored: List[tuple] = []
            for row in all_rows:
                title = (row["title"] or "").lower()
                content = (row["content"] or "").lower()
                tags_str = (row["tags"] or "[]").lower()
                text_words = set((title + " " + content + " " + tags_str).split())
                if query_words and text_words:
                    overlap = len(query_words & text_words)
                    if overlap:
                        ratio = overlap / len(query_words)
                        boost = 1.5 if any(w in title for w in query_words) else 1.0
                        scored.append((ratio * boost, row))
            scored.sort(key=lambda x: x[0], reverse=True)
            _bump("query.keyword_ok")
            return [self._row_to_dict(r) for _, r in scored[:limit]]
        except sqlite3.Error:
            _bump("query.fail")
            return []

    def get_stats(self) -> Dict[str, Any]:
        try:
            total = self.conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
            by_type: Dict[str, int] = {}
            for lt in ("win", "fix", "pattern", "insight", "gotcha", "sop"):
                count = self.conn.execute(
                    "SELECT COUNT(*) FROM learnings WHERE type = ?", (lt,)
                ).fetchone()[0]
                if count:
                    by_type[lt] = count
            today = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            today_count = self.conn.execute(
                "SELECT COUNT(*) FROM learnings WHERE created_at >= ?", (today,)
            ).fetchone()[0]
            last = self.conn.execute(
                "SELECT created_at FROM learnings ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            _bump("stats.ok")
            return {
                "total": total,
                "wins": by_type.get("win", 0),
                "fixes": by_type.get("fix", 0),
                "patterns": by_type.get("pattern", 0),
                "by_type": by_type,
                "today": today_count,
                "last_capture": last[0] if last else None,
                "db_size_kb": (
                    round(self.db_path.stat().st_size / 1024, 2)
                    if self.db_path.exists()
                    else 0
                ),
            }
        except sqlite3.Error:
            _bump("stats.fail")
            return {"total": 0, "wins": 0, "fixes": 0, "patterns": 0, "by_type": {}}

    def health_check(self) -> bool:
        try:
            self.conn.execute("SELECT 1").fetchone()
            _bump("health.ok")
            return True
        except (sqlite3.Error, AttributeError):
            _bump("health.fail")
            return False

    # -- negative patterns ----------------------------------------------- #

    def record_negative_pattern(
        self,
        pattern_key: str,
        context: str,
        goal: str = "",
        description: str = "",
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        snippet = (context or "")[:200]
        try:
            existing = self.conn.execute(
                "SELECT frequency, context_samples FROM negative_patterns "
                "WHERE pattern_key = ?",
                (pattern_key,),
            ).fetchone()
            if existing:
                freq = existing[0] + 1
                try:
                    samples = json.loads(existing[1] or "[]")
                except (json.JSONDecodeError, TypeError):
                    samples = []
                if len(samples) < 3 and snippet:
                    samples.append(snippet)
                self.conn.execute(
                    """
                    UPDATE negative_patterns
                    SET frequency=?, last_seen=?, context_samples=?,
                        goal = CASE WHEN goal='' AND ?<>'' THEN ? ELSE goal END,
                        description = CASE WHEN description IS NULL OR description='' THEN ? ELSE description END
                    WHERE pattern_key=?
                    """,
                    (freq, now, json.dumps(samples), goal, goal, description or pattern_key, pattern_key),
                )
                _bump("negpat.update")
                return freq
            samples = [snippet] if snippet else []
            self.conn.execute(
                """
                INSERT INTO negative_patterns
                (pattern_key, description, goal, first_seen, last_seen, frequency, context_samples)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (pattern_key, description or pattern_key, goal, now, now, json.dumps(samples)),
            )
            _bump("negpat.insert")
            return 1
        except sqlite3.Error as exc:
            _bump("negpat.fail")
            raise StorageError(f"record_negative_pattern failed: {exc}") from exc

    def get_frequent_negative_patterns(
        self, min_frequency: int = 3, goal: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        try:
            if goal:
                rows = self.conn.execute(
                    "SELECT * FROM negative_patterns WHERE frequency >= ? AND goal = ? "
                    "ORDER BY frequency DESC",
                    (min_frequency, goal),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM negative_patterns WHERE frequency >= ? "
                    "ORDER BY frequency DESC",
                    (min_frequency,),
                ).fetchall()
            _bump("negpat.list_ok")
            results: List[Dict[str, Any]] = []
            for row in rows:
                try:
                    samples = json.loads(row["context_samples"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    samples = []
                results.append({
                    "pattern_key": row["pattern_key"],
                    "description": row["description"],
                    "goal": row["goal"],
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                    "frequency": row["frequency"],
                    "promoted_to_sop": bool(row["promoted_to_sop"]),
                    "context_samples": samples,
                })
            return results
        except sqlite3.Error:
            _bump("negpat.list_fail")
            return []

    def mark_pattern_promoted(self, pattern_key: str) -> None:
        try:
            self.conn.execute(
                "UPDATE negative_patterns SET promoted_to_sop = 1 WHERE pattern_key = ?",
                (pattern_key,),
            )
            _bump("negpat.promoted")
        except sqlite3.Error:
            _bump("negpat.promote_fail")

    def close(self) -> None:
        try:
            self.conn.close()
            _bump("close.ok")
        except sqlite3.Error:
            _bump("close.fail")


# --------------------------------------------------------------------------- #
# Singleton accessor                                                           #
# --------------------------------------------------------------------------- #

_sqlite_storage: Optional[SQLiteStorage] = None
_sqlite_storage_lock = threading.Lock()


def get_sqlite_storage() -> SQLiteStorage:
    """Return a thread-safe singleton instance of :class:`SQLiteStorage`."""
    global _sqlite_storage
    if _sqlite_storage is None:
        with _sqlite_storage_lock:
            if _sqlite_storage is None:
                _sqlite_storage = SQLiteStorage()
    return _sqlite_storage


if __name__ == "__main__":
    storage = get_sqlite_storage()
    print(f"db_path: {storage.db_path}")
    print(f"health:  {storage.health_check()}")
    print(f"stats:   {storage.get_stats()}")
    print(f"counters:{get_counters()}")
