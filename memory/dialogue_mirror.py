"""Dialogue Mirror — Synaptic's eyes and ears.

Minimal viable implementation that reads from the existing
``~/.context-dna/.dialogue_mirror.db`` SQLite store (already populated
by upstream writers in ``persistent_hook_structure.py``).

Contract reverse-engineered from callers:
- ``synaptic_service_hub.get_dialogue_context`` -> ``mirror.get_context_for_synaptic(max_messages, max_age_hours)``
  expects a dict shaped like ``{"dialogue_context": [...], "sources": [...], "time_range": {...}}``.
- ``persistent_hook_structure`` (rich-context build + short-prompt bypass) calls
  ``mirror_aaron_message(session_id, content, source, project=None)`` and reads
  ``DialogueMirror().get_sentiment_summary(hours_back=...)``.
- ``lite_scheduler._run_user_sentiment`` calls
  ``get_dialogue_mirror().analyze_user_sentiment(hours_back=...)``.
- Module-level imports requested elsewhere: ``DialogueMirror``, ``DialogueSource``,
  ``get_dialogue_mirror``, ``mirror_aaron_message``.

Also exposes the task-spec helpers ``get_recent_dialogue``,
``get_dialogue_for_query``, ``get_dialogue_context`` plus a ``DialogueEntry``
TypedDict for callers that want a strict schema.

ZSF: no exception ever leaves a public call. Each public method increments
a counter (in-memory + persisted to a JSON file) so failures are observable
via ``get_health()`` instead of being silently swallowed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

CONTEXT_DNA_DIR = Path(
    os.environ.get("CONTEXT_DNA_DIR", str(Path.home() / ".context-dna"))
)
LEGACY_DB_PATH = CONTEXT_DNA_DIR / ".dialogue_mirror.db"
COUNTERS_PATH = CONTEXT_DNA_DIR / ".dialogue_mirror_counters.json"

# Sentiment / intent vocab (rule-based, mirrors lite_scheduler expectations)
_NEG_PATTERNS = (
    "frustrat", "annoy", "broken", "doesn't work", "wtf", "stuck", "again",
    "wrong", "wrong way", "stop", "no, ", "no ", "bug", "fail", "crash",
)
_POS_PATTERNS = (
    "great", "love", "perfect", "nice", "ship", "works", "thanks", "good job",
)
_INTENT_KEYWORDS = {
    "debug": ("debug", "fix", "broken", "bug", "trace", "error"),
    "build": ("build", "implement", "add", "create", "scaffold", "write"),
    "deploy": ("deploy", "ship", "release", "push", "merge"),
    "refactor": ("refactor", "clean", "simplify", "consolidate", "rename"),
    "review": ("review", "audit", "scan", "check"),
}


class DialogueSource(str, Enum):
    """IDE / origin tags. Stored as plain strings in the ``source`` column."""

    VSCODE = "vscode"
    CURSOR = "cursor"
    WINDSURF = "windsurf"
    PYCHARM = "pycharm"
    FLEET = "fleet"
    UNKNOWN = "unknown"


class DialogueEntry(TypedDict, total=False):
    """Public schema for a single mirrored dialogue exchange."""

    ts: str
    speaker: str
    text: str
    session_id: str
    source: str
    project: str
    role: str  # alias of ``speaker`` for hub callers
    content: str  # alias of ``text`` for hub callers


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_to_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Accept both naive (legacy rows) and aware ISO strings.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _has_table(db_path: Path, table: str) -> bool:
    """Return True iff ``db_path`` exists and contains ``table``."""
    try:
        if not db_path.exists() or db_path.stat().st_size == 0:
            return False
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _resolve_db_path() -> Path:
    """Pick the live store, preferring unified DB *only* when its tables exist.

    Fallback chain (in order):
    1. Unified DB with ``dlg_dialogue_messages`` populated  -> unified
    2. Legacy ``~/.context-dna/.dialogue_mirror.db``        -> legacy
    3. Legacy path even if missing (writes will create it)  -> legacy
    """
    try:
        from memory.db_utils import get_unified_db_path  # type: ignore

        unified = Path(get_unified_db_path(LEGACY_DB_PATH))
        if unified != LEGACY_DB_PATH and _has_table(unified, "dlg_dialogue_messages"):
            return unified
    except Exception:
        pass
    return LEGACY_DB_PATH


def _resolve_table(table_name: str, db_path: Optional[Path] = None) -> str:
    """Resolve the actual table name.

    Uses the unified prefix only when ``db_path`` points at the unified DB.
    Otherwise returns the bare legacy name regardless of what ``db_utils``
    would prefer (avoids the 0-byte unified-stub trap).
    """
    try:
        from memory.db_utils import UNIFIED_DB_PATH, unified_table  # type: ignore

        if db_path is not None and Path(db_path) == Path(UNIFIED_DB_PATH):
            return unified_table(".dialogue_mirror.db", table_name)
    except Exception:
        pass
    return table_name


# ---------------------------------------------------------------------------
# DialogueMirror
# ---------------------------------------------------------------------------


@dataclass
class _Counters:
    """In-memory counters; persisted lazily so failures stay observable."""

    reads: int = 0
    writes: int = 0
    errors: int = 0
    last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reads": self.reads,
            "writes": self.writes,
            "errors": self.errors,
            "last_error": self.last_error,
            "ts": _now_iso(),
        }


class DialogueMirror:
    """Read-mostly view over ``~/.context-dna/.dialogue_mirror.db``.

    Writes are append-only and best-effort. Never raises out — every failure
    increments ``_counters.errors`` and degrades to an empty/neutral return so
    callers (Synaptic hub, lite_scheduler, hook injector) keep flowing.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else _resolve_db_path()
        self._lock = threading.Lock()
        self._counters = _Counters()
        # Ensure the parent dir exists so the first write doesn't crash.
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("dialogue_mirror: parent dir create failed: %s", exc)

    # -- internal -----------------------------------------------------------

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            if not self.db_path.exists():
                return None
            conn = sqlite3.connect(str(self.db_path), timeout=2.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as exc:
            self._record_error(f"connect: {exc}")
            return None

    def _record_error(self, msg: str) -> None:
        self._counters.errors += 1
        self._counters.last_error = msg[:200]
        logger.warning("dialogue_mirror error: %s", msg)
        # Persist counters opportunistically; ignore I/O failure.
        try:
            COUNTERS_PATH.write_text(json.dumps(self._counters.to_dict()))
        except OSError:
            pass

    def _table(self, name: str) -> str:
        return _resolve_table(name, self.db_path)

    def _row_to_entry(self, row: sqlite3.Row) -> DialogueEntry:
        text = row["content"] if "content" in row.keys() else ""
        speaker = row["role"] if "role" in row.keys() else "unknown"
        ts = row["timestamp"] if "timestamp" in row.keys() else ""
        sid = row["session_id"] if "session_id" in row.keys() else ""
        source = row["source"] if "source" in row.keys() else ""
        try:
            project = row["project_context"] if "project_context" in row.keys() else ""
        except IndexError:
            project = ""
        entry: DialogueEntry = {
            "ts": ts or "",
            "speaker": speaker or "unknown",
            "text": text or "",
            "session_id": sid or "",
            "source": source or "",
            "project": project or "",
            # Hub-friendly aliases:
            "role": speaker or "unknown",
            "content": text or "",
        }
        return entry

    # -- public read API ----------------------------------------------------

    def get_recent_messages(
        self,
        max_messages: int = 50,
        max_age_hours: int = 24,
        roles: Optional[List[str]] = None,
    ) -> List[DialogueEntry]:
        """Return most-recent mirrored exchanges, newest-first."""
        self._counters.reads += 1
        conn = self._connect()
        if conn is None:
            return []
        try:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(
                hours=max(1, int(max_age_hours))
            )
            cutoff = cutoff_dt.isoformat()
            tbl = self._table("dialogue_messages")
            params: List[Any] = [cutoff]
            sql = (
                f"SELECT session_id, role, content, timestamp, source, project_context "
                f"FROM {tbl} WHERE timestamp >= ?"
            )
            if roles:
                placeholders = ",".join("?" * len(roles))
                sql += f" AND role IN ({placeholders})"
                params.extend(roles)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(max(1, int(max_messages)))
            rows = conn.execute(sql, params).fetchall()
            # If the time window is empty (e.g. stale DB), fall back to
            # unconditional newest rows so Synaptic still gets *something*.
            if not rows:
                rows = conn.execute(
                    f"SELECT session_id, role, content, timestamp, source, project_context "
                    f"FROM {tbl} ORDER BY timestamp DESC LIMIT ?",
                    (max(1, int(max_messages)),),
                ).fetchall()
            return [self._row_to_entry(r) for r in rows]
        except sqlite3.Error as exc:
            self._record_error(f"get_recent_messages: {exc}")
            return []
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def get_context_for_synaptic(
        self, max_messages: int = 50, max_age_hours: int = 24
    ) -> Dict[str, Any]:
        """Shape the hub's ``get_dialogue_context`` expects.

        Returned dict contains:
        - ``dialogue_context``: list of dicts with role/content/timestamp/source.
        - ``sources``: distinct ``source`` values seen in the window.
        - ``time_range``: ``{"from": iso, "to": iso}``.
        """
        msgs = self.get_recent_messages(
            max_messages=max_messages, max_age_hours=max_age_hours
        )
        # Newest-first from DB; Synaptic reasoning usually expects oldest-first.
        ordered = list(reversed(msgs))
        sources = sorted({m.get("source", "") for m in ordered if m.get("source")})
        if ordered:
            time_range = {
                "from": ordered[0].get("ts", ""),
                "to": ordered[-1].get("ts", ""),
            }
        else:
            time_range = {"from": "", "to": ""}
        return {
            "dialogue_context": ordered,
            "sources": sources,
            "time_range": time_range,
            "message_count": len(ordered),
        }

    def get_dialogue_for_query(
        self, query: str, limit: int = 10, max_age_hours: int = 24 * 30
    ) -> List[DialogueEntry]:
        """Substring match across ``content``. Falls back to recent if empty."""
        self._counters.reads += 1
        conn = self._connect()
        if conn is None:
            return []
        try:
            tbl = self._table("dialogue_messages")
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=max(1, int(max_age_hours)))
            ).isoformat()
            # Tokenise into simple words; AND across tokens via multiple LIKEs.
            tokens = [t for t in re.findall(r"[A-Za-z0-9_]+", query or "") if len(t) > 2]
            if not tokens:
                # Empty/short query -> just return recent rows.
                return self.get_recent_messages(
                    max_messages=limit, max_age_hours=max_age_hours
                )
            where = " AND ".join(["content LIKE ?"] * len(tokens))
            params: List[Any] = [f"%{t}%" for t in tokens]
            params.append(cutoff)
            params.append(max(1, int(limit)))
            sql = (
                f"SELECT session_id, role, content, timestamp, source, project_context "
                f"FROM {tbl} WHERE {where} AND timestamp >= ? "
                f"ORDER BY timestamp DESC LIMIT ?"
            )
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_entry(r) for r in rows]
        except sqlite3.Error as exc:
            self._record_error(f"get_dialogue_for_query: {exc}")
            return []
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def get_sentiment_summary(self, hours_back: int = 2) -> str:
        """Tiny rule-based sentiment string (used by S6 dialogue context)."""
        result = self.analyze_user_sentiment(hours_back=hours_back)
        if result.get("message_count", 0) == 0:
            return ""
        return (
            f"sentiment={result['sentiment']} intent={result['intent']} "
            f"energy={result['energy']} msgs={result['message_count']}"
        )

    def analyze_user_sentiment(self, hours_back: int = 2) -> Dict[str, Any]:
        """Rule-based sentiment scan over Aaron's recent messages.

        Returns the dict ``lite_scheduler._run_user_sentiment`` expects:
        ``{sentiment, intent, energy, message_count}``.
        """
        msgs = self.get_recent_messages(
            max_messages=20, max_age_hours=hours_back, roles=["aaron", "user"]
        )
        if not msgs:
            return {
                "sentiment": "neutral",
                "intent": "unknown",
                "energy": "medium",
                "message_count": 0,
            }
        joined = " ".join((m.get("text") or "").lower() for m in msgs)
        neg = sum(joined.count(p) for p in _NEG_PATTERNS)
        pos = sum(joined.count(p) for p in _POS_PATTERNS)
        if neg > pos + 1:
            sentiment = "frustrated"
        elif pos > neg + 1:
            sentiment = "satisfied"
        else:
            sentiment = "neutral"
        intent = "unknown"
        for label, keys in _INTENT_KEYWORDS.items():
            if any(k in joined for k in keys):
                intent = label
                break
        # Energy = density of !/?/CAPS in latest message.
        last = msgs[0].get("text", "") if msgs else ""
        bang = last.count("!") + last.count("?")
        caps_ratio = (
            sum(1 for ch in last if ch.isupper()) / max(1, len(last)) if last else 0
        )
        if bang >= 2 or caps_ratio > 0.3:
            energy = "high"
        elif len(last) < 20:
            energy = "low"
        else:
            energy = "medium"
        return {
            "sentiment": sentiment,
            "intent": intent,
            "energy": energy,
            "message_count": len(msgs),
        }

    # -- public write API ---------------------------------------------------

    def mirror_message(
        self,
        session_id: str,
        role: str,
        content: str,
        source: str = "vscode",
        project: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Append a single mirrored exchange. Best-effort; never raises."""
        if not content:
            return False
        self._counters.writes += 1
        try:
            with self._lock:
                conn = sqlite3.connect(str(self.db_path), timeout=2.0)
                try:
                    self._ensure_tables(conn)
                    msg_tbl = self._table("dialogue_messages")
                    thr_tbl = self._table("dialogue_threads")
                    now = _now_iso()
                    msg_id = f"{int(time.time() * 1000)}-{session_id[:8]}-{role[:6]}"
                    conn.execute(
                        f"INSERT OR IGNORE INTO {msg_tbl} "
                        f"(id, session_id, role, content, timestamp, source, "
                        f"project_context, file_context, metadata) "
                        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            msg_id,
                            session_id or "default-session",
                            role,
                            content,
                            now,
                            source or DialogueSource.UNKNOWN.value,
                            project or "",
                            "",
                            json.dumps(metadata or {}),
                        ),
                    )
                    # Upsert thread.
                    conn.execute(
                        f"INSERT INTO {thr_tbl} "
                        f"(session_id, project, started_at, last_activity, message_count) "
                        f"VALUES (?, ?, ?, ?, 1) "
                        f"ON CONFLICT(session_id) DO UPDATE SET "
                        f"last_activity = excluded.last_activity, "
                        f"message_count = message_count + 1",
                        (session_id or "default-session", project or "", now, now),
                    )
                    conn.commit()
                    return True
                finally:
                    conn.close()
        except sqlite3.Error as exc:
            self._record_error(f"mirror_message: {exc}")
            return False

    def _ensure_tables(self, conn: sqlite3.Connection) -> None:
        """Create legacy schema if the DB is brand new (unified DB owns its own)."""
        if str(self.db_path).endswith("contextdna_unified.db"):
            return
        msg_tbl = self._table("dialogue_messages")
        thr_tbl = self._table("dialogue_threads")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {thr_tbl} ("
            "session_id TEXT PRIMARY KEY, project TEXT, started_at TEXT, "
            "last_activity TEXT, message_count INTEGER DEFAULT 0)"
        )
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {msg_tbl} ("
            "id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
            "timestamp TEXT, source TEXT, project_context TEXT, file_context TEXT, "
            "metadata TEXT)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{msg_tbl}_ts ON {msg_tbl}(timestamp DESC)"
        )

    # -- health -------------------------------------------------------------

    def get_health(self) -> Dict[str, Any]:
        return {
            "db_path": str(self.db_path),
            "db_exists": self.db_path.exists(),
            **self._counters.to_dict(),
        }


# ---------------------------------------------------------------------------
# Module-level helpers (the surface area callers actually import)
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_singleton: Optional[DialogueMirror] = None


def get_dialogue_mirror() -> DialogueMirror:
    """Return a process-wide singleton."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = DialogueMirror()
    return _singleton


def mirror_aaron_message(
    session_id: str,
    content: str,
    source: str = "vscode",
    project: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Mirror an incoming Aaron prompt (called from injection hook)."""
    try:
        return get_dialogue_mirror().mirror_message(
            session_id=session_id,
            role="aaron",
            content=content,
            source=source,
            project=project,
            metadata=metadata,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("mirror_aaron_message swallowed: %s", exc)
        return False


def mirror_atlas_message(
    session_id: str,
    content: str,
    source: str = "atlas",
    project: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Symmetric helper for Atlas replies (used by future writers)."""
    try:
        return get_dialogue_mirror().mirror_message(
            session_id=session_id,
            role="atlas",
            content=content,
            source=source,
            project=project,
            metadata=metadata,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("mirror_atlas_message swallowed: %s", exc)
        return False


def get_recent_dialogue(
    limit: int = 20, since_iso: Optional[str] = None
) -> List[DialogueEntry]:
    """Task-spec helper. Returns recent exchanges as DialogueEntry list."""
    hours = 24
    if since_iso:
        since_dt = _iso_to_dt(since_iso)
        if since_dt:
            delta = datetime.now(timezone.utc) - since_dt
            hours = max(1, int(delta.total_seconds() // 3600) + 1)
    return get_dialogue_mirror().get_recent_messages(
        max_messages=limit, max_age_hours=hours
    )


def get_dialogue_for_query(query: str, limit: int = 10) -> List[DialogueEntry]:
    """Task-spec helper. Keyword/substring search across content."""
    return get_dialogue_mirror().get_dialogue_for_query(query=query, limit=limit)


def get_dialogue_context(topic: str, limit: int = 10) -> Dict[str, Any]:
    """Task-spec helper. Returns the hub-shaped context dict for ``topic``.

    Strategy:
    - If ``topic`` has non-trivial tokens, run substring search and reshape
      to the hub schema (``dialogue_context``/``sources``/``time_range``).
    - Otherwise fall back to the rolling 24h window.
    """
    mirror = get_dialogue_mirror()
    tokens = [t for t in re.findall(r"[A-Za-z0-9_]+", topic or "") if len(t) > 2]
    if not tokens:
        return mirror.get_context_for_synaptic(max_messages=limit, max_age_hours=24)
    matches = mirror.get_dialogue_for_query(query=topic, limit=limit)
    ordered = list(reversed(matches))
    sources = sorted({m.get("source", "") for m in ordered if m.get("source")})
    time_range = (
        {"from": ordered[0].get("ts", ""), "to": ordered[-1].get("ts", "")}
        if ordered
        else {"from": "", "to": ""}
    )
    return {
        "dialogue_context": ordered,
        "sources": sources,
        "time_range": time_range,
        "message_count": len(ordered),
        "topic": topic,
    }


__all__ = [
    "DialogueMirror",
    "DialogueSource",
    "DialogueEntry",
    "get_dialogue_mirror",
    "mirror_aaron_message",
    "mirror_atlas_message",
    "get_recent_dialogue",
    "get_dialogue_for_query",
    "get_dialogue_context",
]
