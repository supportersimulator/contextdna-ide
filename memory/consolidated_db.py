"""Consolidated SQLite connection helper.

Single source of truth for the unified ``memory/contextdna.db`` produced by
``scripts/migrate-sqlite.py``. Domain helpers (``fleet_db``, ``surgeon_db``,
``evidence_db``, ``synaptic_db``, ``webhook_db``) all share one connection
opened in WAL mode with foreign keys ON and a generous busy timeout.

Backwards-compat shim: legacy modules that imported per-domain DB paths can
call ``legacy_path_redirect(old_path)`` which emits a ``DeprecationWarning``
and returns the consolidated DB path. Existing callers using
``sqlite3.connect(...)`` keep working — the path just points at the new
unified DB.

Stdlib only — no SQLAlchemy.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# Resolved relative to the repo root (this file lives in ``memory/``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = _REPO_ROOT / "memory" / "contextdna.db"


# Domain -> table prefix. Must stay in sync with scripts/migrate-sqlite.py.
DOMAIN_PREFIXES: dict[str, str] = {
    "fleet": "fleet_",
    "surgeon": "surgeon_",
    "evidence": "evidence_",
    "synaptic": "synaptic_",
    "webhook": "webhook_",
}


# Module-level singleton so all helpers share one connection per thread.
_lock = threading.Lock()
_connections: dict[int, sqlite3.Connection] = {}


def _db_path() -> Path:
    """Resolve the DB path. ``CONTEXTDNA_DB_PATH`` env var wins (tests)."""
    env = os.environ.get("CONTEXTDNA_DB_PATH")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


def get_connection() -> sqlite3.Connection:
    """Return a thread-local connection to the consolidated DB.

    First call per-thread opens the file with WAL, foreign keys, busy_timeout.
    """
    tid = threading.get_ident()
    with _lock:
        conn = _connections.get(tid)
        if conn is not None:
            return conn
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        _connections[tid] = conn
        return conn


def close_all() -> None:
    """Close every cached thread-local connection. Test helper."""
    with _lock:
        for c in _connections.values():
            try:
                c.close()
            except sqlite3.Error:
                pass
        _connections.clear()


# ── Domain helpers ─────────────────────────────────────────────────────────


@contextmanager
def _domain_cursor(domain: str) -> Iterator[sqlite3.Cursor]:
    """Yield a cursor scoped to a domain. Validates domain is registered."""
    if domain not in DOMAIN_PREFIXES:
        raise ValueError(f"unknown domain {domain!r}; valid: {list(DOMAIN_PREFIXES)}")
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def fleet_db() -> "_DomainHandle":
    """Cursor handle for fleet_* tables."""
    return _DomainHandle("fleet")


def surgeon_db() -> "_DomainHandle":
    """Cursor handle for surgeon_* tables."""
    return _DomainHandle("surgeon")


def evidence_db() -> "_DomainHandle":
    """Cursor handle for evidence_* tables."""
    return _DomainHandle("evidence")


def synaptic_db() -> "_DomainHandle":
    """Cursor handle for synaptic_* tables."""
    return _DomainHandle("synaptic")


def webhook_db() -> "_DomainHandle":
    """Cursor handle for webhook_* tables."""
    return _DomainHandle("webhook")


class _DomainHandle:
    """Thin wrapper that exposes ``cursor()`` and ``prefix`` for a domain.

    Use as a context manager for transactional scope, or call ``cursor()``
    directly for ad-hoc queries against the shared connection.
    """

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.prefix = DOMAIN_PREFIXES[domain]

    def cursor(self) -> sqlite3.Cursor:
        return get_connection().cursor()

    def table(self, suffix: str) -> str:
        """Compose a fully-prefixed table name. Convenience for callers."""
        return f"{self.prefix}{suffix}"

    def __enter__(self):
        self._ctx = _domain_cursor(self.domain)
        return self._ctx.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self._ctx.__exit__(exc_type, exc, tb)


# ── Backwards-compat redirect ──────────────────────────────────────────────


_LEGACY_PATHS: dict[str, str] = {
    str(_REPO_ROOT / "memory" / ".hindsight_validator.db"): "surgeon",
    str(_REPO_ROOT / "memory" / ".strategic_plans.db"): "synaptic",
    str(_REPO_ROOT / "memory" / ".outcome_tracking.db"): "evidence",
    str(_REPO_ROOT / "memory" / ".failure_patterns.db"): "surgeon",
    str(_REPO_ROOT / "memory" / ".observability.db"): "webhook",
    str(
        _REPO_ROOT / "multi-fleet" / ".contextdna" / "local" / "supervisor.db"
    ): "fleet",
}


def legacy_path_redirect(old_path: str | Path) -> Path:
    """Redirect any legacy DB path to the consolidated DB.

    Emits a ``DeprecationWarning`` so callers see the migration nudge in
    their logs. Returns the consolidated DB path so existing code that does
    ``sqlite3.connect(legacy_path_redirect(...))`` keeps working.
    """
    key = str(Path(old_path).resolve()) if Path(old_path).exists() else str(old_path)
    domain = _LEGACY_PATHS.get(key) or _LEGACY_PATHS.get(str(old_path))
    if domain is not None:
        warnings.warn(
            f"{old_path} is deprecated; use memory/consolidated_db.{domain}_db() "
            f"or set CONTEXTDNA_DB_PATH to the consolidated DB.",
            DeprecationWarning,
            stacklevel=2,
        )
    return _db_path()


__all__ = [
    "DEFAULT_DB_PATH",
    "DOMAIN_PREFIXES",
    "get_connection",
    "close_all",
    "fleet_db",
    "surgeon_db",
    "evidence_db",
    "synaptic_db",
    "webhook_db",
    "legacy_path_redirect",
]
