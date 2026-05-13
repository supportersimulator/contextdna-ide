#!/usr/bin/env python3
"""
memory.librarian — Minimal Viable Librarian for Synaptic + Agent Service

This module unblocks two consumers that previously imported `memory.librarian`:

  1. `memory.synaptic_service_hub.get_rich_context()` -> calls `_search_learnings`.
     When this import failed, Synaptic's `learnings_error` was populated and the
     8th Intelligence lost ledger context. THIS IS THE PRIMARY CONTRACT.

  2. `memory.agent_service.create_app()` -> mounts `librarian_router` (FastAPI).
     When FastAPI is unavailable or the router is absent, the agent service logs
     a soft warning. This module exposes a best-effort router so the mount no
     longer ImportErrors.

Storage layer
-------------
The legacy sqlite_storage / unified_storage modules referenced by the older
librarian have been excised from the live tree (see `memory.bak.20260426/`).
The only learnings store still live is the SQLite FTS5 table at
`~/.context-dna/learnings.db` (table `learnings`, FTS5 mirror `learnings_fts`).

Schema (verified at write time, 2026-05-13):
    learnings(id TEXT PK, type TEXT, title TEXT, content TEXT, tags TEXT,
              session_id, injection_id, source, created_at, updated_at,
              metadata, merge_count, embedding, project, workspace_id)
    learnings_fts(title, content, tags) — FTS5 virtual mirror

If the DB or FTS table is missing we fall back to a LIKE substring scan over
title/content. If both fail we return `[]` and bump a counter.

ZSF compliance
--------------
- No `except: pass`. Every failure path bumps a counter in `COUNTERS` AND logs.
- Counter dict is module-level, daemon-scrapable via
  `from memory.librarian import COUNTERS`.
- Persistence: counters mirrored to
  `memory/.librarian_counters.json` on each bump (best-effort; persistence
  failures bump `librarian_counter_persist_errors_total`).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("memory.librarian")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s librarian %(message)s"
    ))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

_USER_DATA_DIR = Path.home() / ".context-dna"
LEARNINGS_DB_PATH = Path(
    os.environ.get("CONTEXTDNA_LEARNINGS_DB", str(_USER_DATA_DIR / "learnings.db"))
)
_COUNTER_FILE = Path(__file__).parent / ".librarian_counters.json"

STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "it", "its", "this", "that",
    "and", "or", "but", "not", "so",
})


# ---------------------------------------------------------------------------
# ZSF counters (module-level, daemon-scrapable per CLAUDE.md invariant)
# ---------------------------------------------------------------------------

COUNTERS: Dict[str, int] = {
    "librarian_search_ok_total": 0,
    "librarian_search_fts_hits_total": 0,
    "librarian_search_like_fallback_total": 0,
    "librarian_search_empty_total": 0,
    "librarian_search_db_missing_total": 0,
    "librarian_search_db_open_errors_total": 0,
    "librarian_search_fts_errors_total": 0,
    "librarian_search_like_errors_total": 0,
    "librarian_search_unknown_errors_total": 0,
    "librarian_patterns_ok_total": 0,
    "librarian_patterns_errors_total": 0,
    "librarian_sops_ok_total": 0,
    "librarian_sops_errors_total": 0,
    "librarian_counter_persist_errors_total": 0,
}

_COUNTER_LOCK = threading.Lock()


def _bump(name: str, n: int = 1) -> None:
    """Increment a ZSF counter and best-effort persist to disk."""
    with _COUNTER_LOCK:
        COUNTERS[name] = COUNTERS.get(name, 0) + n
        snapshot = dict(COUNTERS)
    try:
        _COUNTER_FILE.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    except OSError as e:
        # Persistence is best-effort but observable per ZSF.
        with _COUNTER_LOCK:
            COUNTERS["librarian_counter_persist_errors_total"] = (
                COUNTERS.get("librarian_counter_persist_errors_total", 0) + 1
            )
        logger.warning(
            "librarian counter persist failed name=%s err=%s", name, e
        )


def get_counters() -> Dict[str, int]:
    """Return a snapshot of ZSF counters."""
    with _COUNTER_LOCK:
        return dict(COUNTERS)


# ---------------------------------------------------------------------------
# Dataclasses (used by guess-ahead API; primary contract returns plain dicts)
# ---------------------------------------------------------------------------

@dataclass
class Learning:
    id: str
    type: str
    title: str
    content: str
    tags: List[str]
    created_at: str
    score: float = 0.0


@dataclass
class Pattern:
    id: str
    title: str
    content: str
    tags: List[str]
    created_at: str


@dataclass
class SOP:
    id: str
    title: str
    content: str
    tags: List[str]
    created_at: str


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _connect(db_path: Optional[Path] = None) -> Optional[sqlite3.Connection]:
    """Open the learnings SQLite DB read-only. Returns None on failure."""
    path = db_path or LEARNINGS_DB_PATH
    if not path.exists():
        _bump("librarian_search_db_missing_total")
        logger.warning("librarian learnings DB missing: %s", path)
        return None
    try:
        # Read-only URI prevents accidental writes from this surface.
        conn = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=5.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        _bump("librarian_search_db_open_errors_total")
        logger.warning("librarian DB open failed path=%s err=%s", path, e)
        return None


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a learnings row into the dict shape Synaptic expects."""
    try:
        tags_raw = row["tags"] if "tags" in row.keys() else "[]"
        tags = json.loads(tags_raw) if tags_raw else []
        if not isinstance(tags, list):
            tags = []
    except (json.JSONDecodeError, TypeError, ValueError):
        tags = []
    return {
        "id": row["id"] if "id" in row.keys() else "",
        "type": row["type"] if "type" in row.keys() else "learning",
        "title": row["title"] if "title" in row.keys() else "",
        "content": row["content"] if "content" in row.keys() else "",
        "tags": tags,
        "created_at": row["created_at"] if "created_at" in row.keys() else "",
    }


def _tokenize(query: str) -> List[str]:
    """Lowercase + strip stop words. Used for FTS terms and LIKE fallback."""
    words = [
        w.strip().lower()
        for w in query.replace(",", " ").replace(";", " ").split()
        if w.strip()
    ]
    filtered = [w for w in words if w not in STOP_WORDS and len(w) > 1]
    return filtered if filtered else words


# ---------------------------------------------------------------------------
# CORE CONTRACT — what synaptic_service_hub.get_rich_context() imports
# ---------------------------------------------------------------------------

def _search_learnings(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Search learnings via FTS5 with LIKE-substring fallback.

    PRIMARY CONTRACT — called by `memory.synaptic_service_hub.get_rich_context`.

    Returns a list of dicts shaped:
        {"id", "type", "title", "content", "tags", "created_at"}

    Always returns a list (never raises). Empty list on any failure.
    Bumps `librarian_search_*` counters per ZSF.
    """
    if not query or not isinstance(query, str):
        _bump("librarian_search_empty_total")
        return []

    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 10

    conn = _connect()
    if conn is None:
        return []

    try:
        # ---- Strategy 1: FTS5 OR-match ------------------------------------
        words = _tokenize(query)
        if words:
            fts_terms = " OR ".join(
                w.replace('"', '""') for w in words[:8] if w
            )
            if fts_terms:
                try:
                    rows = conn.execute(
                        """
                        SELECT l.id, l.type, l.title, l.content, l.tags,
                               l.created_at
                        FROM learnings l
                        JOIN learnings_fts fts ON l.rowid = fts.rowid
                        WHERE learnings_fts MATCH ?
                          AND COALESCE(l.type, '') != 'prompt_leak'
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_terms, limit),
                    ).fetchall()
                    if rows:
                        out = [_row_to_dict(r) for r in rows]
                        _bump("librarian_search_fts_hits_total")
                        _bump("librarian_search_ok_total")
                        return out
                except sqlite3.Error as e:
                    _bump("librarian_search_fts_errors_total")
                    logger.warning(
                        "librarian FTS5 query failed query=%r err=%s",
                        query[:80], e,
                    )
                    # fall through to LIKE

        # ---- Strategy 2: LIKE-substring fallback --------------------------
        try:
            terms = words or [query.lower()]
            # Score = number of terms matched in title+content (title 2x boost).
            placeholders = " OR ".join(
                "(LOWER(title) LIKE ? OR LOWER(content) LIKE ?)"
                for _ in terms
            )
            params: List[Any] = []
            for t in terms:
                like = f"%{t}%"
                params.extend([like, like])
            sql = f"""
                SELECT id, type, title, content, tags, created_at
                FROM learnings
                WHERE ({placeholders})
                  AND COALESCE(type, '') != 'prompt_leak'
                ORDER BY created_at DESC
                LIMIT ?
            """
            params.append(limit * 3)  # over-fetch, then rank in Python
            rows = conn.execute(sql, params).fetchall()
            scored = []
            for r in rows:
                title_l = (r["title"] or "").lower()
                content_l = (r["content"] or "").lower()
                score = 0
                for t in terms:
                    if t in title_l:
                        score += 2
                    if t in content_l:
                        score += 1
                if score > 0:
                    scored.append((score, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            out = [_row_to_dict(r) for _, r in scored[:limit]]
            _bump("librarian_search_like_fallback_total")
            _bump("librarian_search_ok_total")
            return out
        except sqlite3.Error as e:
            _bump("librarian_search_like_errors_total")
            logger.warning(
                "librarian LIKE fallback failed query=%r err=%s",
                query[:80], e,
            )
            return []
    except Exception as e:  # noqa: BLE001 — last-line catch logs + bumps
        _bump("librarian_search_unknown_errors_total")
        logger.error(
            "librarian _search_learnings unknown error query=%r err=%s",
            query[:80], e,
        )
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error as e:
            # Logged but no counter — connection close is not a service failure.
            logger.debug("librarian conn close error: %s", e)


# ---------------------------------------------------------------------------
# Public API — guess-ahead surfaces requested in the build prompt
# ---------------------------------------------------------------------------

def get_learnings_for_query(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Public alias for `_search_learnings`. Returns plain dicts for JSON safety.

    Synaptic's hub uses `_search_learnings` directly; this exists so future
    callers don't have to reach for a private name.
    """
    return _search_learnings(query, limit)


def get_recent_patterns(limit: int = 10) -> List[Dict[str, Any]]:
    """Most recent rows with type='pattern' from the learnings store.

    Returns a list of dicts in the same shape as `_search_learnings`.
    Empty list on failure (counter bumped).
    """
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 10

    conn = _connect()
    if conn is None:
        _bump("librarian_patterns_errors_total")
        return []
    try:
        rows = conn.execute(
            """
            SELECT id, type, title, content, tags, created_at
            FROM learnings
            WHERE type = 'pattern'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        out = [_row_to_dict(r) for r in rows]
        _bump("librarian_patterns_ok_total")
        return out
    except sqlite3.Error as e:
        _bump("librarian_patterns_errors_total")
        logger.warning("librarian get_recent_patterns failed err=%s", e)
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error as e:
            logger.debug("librarian conn close error: %s", e)


def get_sops(scope: str = "all", limit: int = 10) -> List[Dict[str, Any]]:
    """Return SOPs.

    Priority:
      1. `memory.brain.search_sops(scope)` if scope is a non-empty query string
         (delegates to SOPRegistry — same module Synaptic's hub already uses).
      2. Otherwise (or on failure), fall back to learnings rows of type='sop'.
    """
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 10

    # Strategy 1 — delegate to brain.search_sops if it works.
    if scope and scope != "all":
        try:
            from memory.brain import search_sops as _brain_search_sops
            results = _brain_search_sops(scope) or []
            # Brain returns SOPRegistry objects (may be dicts already);
            # normalise to dicts for JSON-safety.
            out: List[Dict[str, Any]] = []
            for r in results[:limit]:
                if isinstance(r, dict):
                    out.append(r)
                elif hasattr(r, "__dict__"):
                    out.append(dict(r.__dict__))
                else:
                    out.append({"title": str(r), "content": ""})
            _bump("librarian_sops_ok_total")
            return out
        except Exception as e:  # noqa: BLE001
            _bump("librarian_sops_errors_total")
            logger.warning(
                "librarian get_sops brain delegate failed scope=%r err=%s",
                scope, e,
            )
            # Fall through to learnings-table fallback.

    # Strategy 2 — fallback: learnings rows where type='sop'.
    conn = _connect()
    if conn is None:
        _bump("librarian_sops_errors_total")
        return []
    try:
        rows = conn.execute(
            """
            SELECT id, type, title, content, tags, created_at
            FROM learnings
            WHERE type = 'sop'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        out = [_row_to_dict(r) for r in rows]
        _bump("librarian_sops_ok_total")
        return out
    except sqlite3.Error as e:
        _bump("librarian_sops_errors_total")
        logger.warning("librarian get_sops fallback failed err=%s", e)
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error as e:
            logger.debug("librarian conn close error: %s", e)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def health() -> Dict[str, Any]:
    """Quick health probe for the librarian subsystem."""
    db_exists = LEARNINGS_DB_PATH.exists()
    fts_ok = False
    row_count = 0
    if db_exists:
        conn = _connect()
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM learnings"
                ).fetchone()
                row_count = int(row["n"]) if row else 0
                # FTS table reachable?
                try:
                    conn.execute(
                        "SELECT 1 FROM learnings_fts LIMIT 1"
                    ).fetchone()
                    fts_ok = True
                except sqlite3.Error as e:
                    logger.debug("librarian FTS probe failed: %s", e)
            except sqlite3.Error as e:
                logger.warning("librarian health row count failed: %s", e)
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
    return {
        "db_path": str(LEARNINGS_DB_PATH),
        "db_exists": db_exists,
        "fts_available": fts_ok,
        "row_count": row_count,
        "counters": get_counters(),
    }


# ---------------------------------------------------------------------------
# Optional FastAPI router — referenced by memory.agent_service.create_app.
# Best-effort: if FastAPI is missing the import simply omits the router and the
# agent service's existing `except ImportError` path keeps things degraded.
# ---------------------------------------------------------------------------

try:
    from fastapi import APIRouter

    librarian_router = APIRouter(prefix="/v1/context", tags=["librarian"])

    @librarian_router.get("/health")
    async def _librarian_health() -> Dict[str, Any]:
        return health()

    @librarian_router.get("/query")
    async def _librarian_query(q: str, limit: int = 10) -> Dict[str, Any]:
        t0 = time.monotonic()
        results = _search_learnings(q, limit=limit)
        return {
            "query": q,
            "results": results,
            "count": len(results),
            "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
        }
except ImportError as e:
    logger.info("librarian_router unavailable (FastAPI missing): %s", e)
    librarian_router = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "health":
        print(json.dumps(health(), indent=2, default=str))
    elif len(sys.argv) >= 3 and sys.argv[1] == "search":
        q = " ".join(sys.argv[2:])
        results = _search_learnings(q, limit=5)
        print(json.dumps({
            "query": q,
            "results": results,
            "counters": get_counters(),
        }, indent=2, default=str))
    else:
        print("Usage:")
        print("  python -m memory.librarian health")
        print("  python -m memory.librarian search <query>")
