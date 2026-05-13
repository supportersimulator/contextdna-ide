"""
SQLite connection utilities — WAL enforcement + proper lifecycle.

Usage patterns:
    # Pattern 1: Temporary connection (one-off queries)
    conn = connect_wal(db_path)
    try:
        result = conn.execute("SELECT ...").fetchone()
    finally:
        conn.close()

    # Pattern 2: Persistent connection (singleton/long-lived)
    self.conn = connect_wal(db_path, check_same_thread=False)
    # Keep open for object lifetime

NEVER use: `with sqlite3.connect() as conn:` — context manager does NOT close.
"""

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

# Write operations that should be blocked during freeze
_WRITE_PREFIXES = ('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER')


def _is_write_sql(sql: str) -> bool:
    """Check if SQL statement is a write operation."""
    stripped = sql.strip().upper()
    return stripped.startswith(_WRITE_PREFIXES)


def _check_write_freeze() -> None:
    """Check write freeze, raise WriteFrozenError if active.

    Fail-open: if import or Redis fails, does NOT block writes.
    """
    try:
        from memory.write_freeze import get_write_freeze_guard, _write_freeze_enabled
        if not _write_freeze_enabled:
            return
        guard = get_write_freeze_guard()
        guard.check_or_raise()
    except ImportError:
        pass  # write_freeze module not available — fail open
    except Exception as e:
        # Import WriteFrozenError to re-raise it specifically
        try:
            from memory.write_freeze import WriteFrozenError
            if isinstance(e, WriteFrozenError):
                raise
        except ImportError:
            pass
        # All other exceptions: fail open
        logger.debug(f"Write freeze check error (failing open): {e}")


def connect_wal(
    db_path: Union[str, Path],
    timeout: int = 5,
    check_same_thread: bool = True,
    row_factory=sqlite3.Row,
    isolation_level: Optional[str] = "",
) -> sqlite3.Connection:
    """Create SQLite connection with WAL mode and safe defaults.

    Args:
        db_path: Path to database file
        timeout: Lock wait timeout in seconds (maps to busy_timeout)
        check_same_thread: False for multi-threaded singletons
        row_factory: Row factory (default: sqlite3.Row for dict-like access)
        isolation_level: "" for deferred transactions (sqlite3 default), None for autocommit

    Returns:
        Configured sqlite3.Connection. Caller MUST close explicitly.
    """
    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=check_same_thread,
        isolation_level=isolation_level,
        timeout=timeout,
    )
    if row_factory:
        conn.row_factory = row_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={timeout * 1000}")
    return conn


@contextmanager
def safe_conn(
    db_path: Union[str, Path],
    timeout: int = 5,
    row_factory=sqlite3.Row,
    isolation_level: Optional[str] = "",
):
    """Context manager that properly closes the connection.

    Usage:
        with safe_conn(db_path) as conn:
            conn.execute("SELECT ...")

    Unlike `with sqlite3.connect() as conn:`, this ACTUALLY closes on exit.
    """
    conn = connect_wal(
        db_path,
        timeout=timeout,
        row_factory=row_factory,
        isolation_level=isolation_level,
    )
    try:
        yield conn
        # Check write freeze before committing changes
        _check_write_freeze()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class ThreadSafeConnection:
    """Thread-safe SQLite connection wrapper for singleton patterns.

    Usage:
        self.db = ThreadSafeConnection(db_path)
        with self.db.lock:
            self.db.conn.execute("INSERT ...")
    """

    def __init__(self, db_path: Union[str, Path], **kwargs):
        self.lock = threading.Lock()
        self.conn = connect_wal(
            db_path,
            check_same_thread=False,
            **kwargs,
        )

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        if _is_write_sql(sql):
            _check_write_freeze()
        with self.lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, params_seq) -> sqlite3.Cursor:
        if _is_write_sql(sql):
            _check_write_freeze()
        with self.lock:
            return self.conn.executemany(sql, params_seq)

    def fetchone(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self):
        with self.lock:
            self.conn.close()


# ============================================================================
# Unified Database Helpers
# ============================================================================

# Central path to the unified SQLite DB (consolidated from 13 individual DBs)
UNIFIED_DB_PATH = Path(
    os.environ.get("CONTEXT_DNA_DIR", Path.home() / ".context-dna")
) / "contextdna_unified.db"

# Prefix mapping: old DB basename -> table prefix in unified DB
# Must match scripts/consolidate-sqlite.py exactly
_UNIFIED_PREFIX_MAP = {
    "learnings.db": "lrn",
    ".dialogue_mirror.db": "dlg",
    ".synaptic_personality.db": "syn_pers",
    ".synaptic_evolution.db": "syn_evo",
    ".synaptic_evolution_tracking.db": "syn_trk",
    ".synaptic_patterns.db": "syn_pat",
    ".synaptic_chat.db": "syn_chat",
    "contextdna_tasks.db": "task",
    "ghostscan.db": "ghost",
    ".outcome_tracking.db": "out",
    ".failure_patterns.db": "fail",
    ".strategic_plans.db": "plan",
    ".hindsight_validator.db": "hind",
}


def unified_db_available() -> bool:
    """Check if the unified database exists."""
    return UNIFIED_DB_PATH.exists()


def get_unified_db_path(legacy_db_path: Union[str, Path]) -> Path:
    """Resolve a legacy DB path to the unified DB if available.

    If the unified DB exists and the legacy DB is one of the 13 consolidated,
    returns the unified path. Otherwise returns the original legacy path.

    Args:
        legacy_db_path: Original path to a specific .db file

    Returns:
        Path to either the unified DB or the original legacy DB
    """
    legacy = Path(legacy_db_path)
    db_name = legacy.name

    if db_name in _UNIFIED_PREFIX_MAP and UNIFIED_DB_PATH.exists():
        return UNIFIED_DB_PATH

    return legacy


def get_table_prefix(legacy_db_path: Union[str, Path]) -> str:
    """Get the table prefix for a legacy DB in the unified database.

    Args:
        legacy_db_path: Original path to a specific .db file

    Returns:
        Prefix string (e.g., "lrn", "dlg") or empty string if not consolidated
    """
    db_name = Path(legacy_db_path).name
    return _UNIFIED_PREFIX_MAP.get(db_name, "")


def unified_table(legacy_db_path: Union[str, Path], table_name: str) -> str:
    """Get the actual table name, applying unified prefix if applicable.

    Usage:
        table = unified_table(".dialogue_mirror.db", "dialogue_threads")
        # Returns "dlg_dialogue_threads" if unified DB exists
        # Returns "dialogue_threads" if using legacy DB

    Args:
        legacy_db_path: Original .db filename (basename or full path)
        table_name: Original table name without prefix

    Returns:
        Prefixed table name if unified DB in use, else original name
    """
    db_name = Path(legacy_db_path).name
    prefix = _UNIFIED_PREFIX_MAP.get(db_name, "")

    if prefix and UNIFIED_DB_PATH.exists():
        return f"{prefix}_{table_name}"
    return table_name
