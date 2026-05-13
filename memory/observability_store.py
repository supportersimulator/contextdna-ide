"""
observability_store — session/workflow audit trail.

A lightweight reimplementation of the legacy
`memory/observability_store.py` (see `memory.bak.20260426/observability_store.py`,
~4155 LOC) reduced to the *caller contract* actually exercised across the live
codebase.  Synaptic flagged the missing module as the #1 still-broken
dependency: 14+ references resolve `from memory.observability_store import ...`
that throw `ModuleNotFoundError` and prevent the session historian, mutual
heartbeat, agent service, lite scheduler, session gold passes and
surgery-team CLI from recording audit trail events — so Synaptic cannot
diagnose failures and webhook injection cannot promote evidence.

Caller contract (discovered from grep, see module-level docstring at bottom):

    Factory / lifecycle
      - get_observability_store()              -> singleton, thread-safe
      - ObservabilityStore(mode="auto")         -> "lite" | "heavy" | "auto"
      - store.close()
      - store._sqlite_conn                      (raw sqlite3.Connection,
                                                 exposed for power callers)
      - store.mode                              ("lite" | "heavy")

    Event recording (used by lite_scheduler, session_historian,
                     session_gold_passes, persistent_hook_structure)
      - record_event(event_type, payload, session_id=None, success=True,
                     details=None)              # NEW: Synaptic JSON-RPC surface
      - record_outcome_event(session_id, outcome_type, success, reward, notes)
      - record_direct_claim_outcome(claim_id, success, reward, source, notes)
      - record_dependency_status(dependency_name, status, latency_ms=None,
                                 reason_code=None, details=None)
      - record_task_run(task_name, run_type, status, duration_ms,
                        budget_ms=None, details=None, error=None)
      - record_recovery_attempt(service_name, success, duration_ms, message)
      - quarantine_item(item_id, item_type, promotion_rules, notes)

    Query (used by surgery-team and agent_service)
      - get_events(event_type=None, session_id=None, limit=100)
      - get_session_trace(session_id, limit=500)
      - query_events(filters, limit=200)         # Synaptic JSON-RPC surface
      - get_recent_failures(hours=24, limit=50)
      - get_health()                              # JSON-RPC surface
      - get_claims_by_status(status, limit=20)
      - get_frequent_negative_patterns(min_frequency=2)
      - get_recent_task_runs(limit=20)
      - get_recent_dependency_events(limit=20)
      - get_dependency_summary(hours=24)

Storage strategy
================
SQLite-first (always on, works in `lite` mode out of the box, no Postgres
dep required to import this module).  Optional PostgreSQL backend toggled
via the env var `OBSERVABILITY_BACKEND={sqlite|postgres|auto}` (default
`auto`: SQLite primary, PostgreSQL mirror if `psycopg2` is importable AND
`OBSERVABILITY_PG_DSN` is set).  A failure to attach the Postgres mirror
NEVER aborts a write — the SQLite write succeeds and a ZSF counter is
bumped (`observability.pg.attach_errors`).

Triple-fallback ladder (ZSF — no silent failure):

    1. SQLite write (default).  If the sqlite file is corrupted at startup
       it is rotated to `<path>.corrupted_<ts>` (matching the backup's
       2026-02-07 P1.2 fix).
    2. PostgreSQL mirror — best-effort.  Errors recorded in
       `observability.pg.write_errors`, never raised.
    3. In-memory ring buffer (10k events) — last-resort fallback if BOTH
       SQLite AND Postgres are unavailable.  Errors are recorded in
       `observability.ringbuf.flushes` and surfaced via `get_health()`.

Every code path increments a counter; counters are reported by
`get_health()` so the multi-fleet `/health` endpoint can observe degraded
state.  No `except Exception: pass` — the ZSF invariant in
/CLAUDE.md is enforced.

JSON-RPC API
============
See `configs/observability-store.yaml` for the JSON-RPC method descriptors
that this module backs.  The four canonical surface methods are
`record_event`, `query_events`, `get_health`, `get_recent_failures`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

# ---------------------------------------------------------------------------
# Module-level logger.  Falls back to a noop when stdlib logging is misconfigured
# (this module is imported very early in some bootstraps).
# ---------------------------------------------------------------------------
try:  # pragma: no cover — pure import guard
    import logging

    logger = logging.getLogger("memory.observability_store")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
except Exception:  # noqa: BLE001 — ZSF: surface, but do not raise from import
    class _NoopLogger:
        def __getattr__(self, _name: str) -> Callable[..., None]:
            return lambda *_, **__: None

    logger = _NoopLogger()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------
_DEFAULT_DB_DIR = Path(
    os.environ.get(
        "OBSERVABILITY_DB_DIR",
        str(Path.home() / ".claude" / "projects" / "contextdna-ide-oss"),
    )
).expanduser()

SQLITE_DB_PATH: Path = _DEFAULT_DB_DIR / ".observability.db"
RING_BUFFER_SIZE: int = int(os.environ.get("OBSERVABILITY_RINGBUF_SIZE", "10000"))

# ---------------------------------------------------------------------------
# Schema constant — exported so tooling, migrations and the YAML config can
# reference the canonical DDL.  Schema version is encoded in the trailing
# metadata table so downstream consumers can detect drift.
# ---------------------------------------------------------------------------
OBSERVABILITY_SCHEMA_VERSION = "1.0.0"

OBSERVABILITY_SCHEMA = """
PRAGMA foreign_keys=ON;

-- Generic event log — JSON-RPC `record_event` lands here.
CREATE TABLE IF NOT EXISTS observability_events (
    event_id        TEXT PRIMARY KEY,
    timestamp_utc   TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    session_id      TEXT,
    success         INTEGER NOT NULL DEFAULT 1,
    payload_json    TEXT NOT NULL,
    details_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_observability_events_time
    ON observability_events(timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_observability_events_type
    ON observability_events(event_type, timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_observability_events_session
    ON observability_events(session_id, timestamp_utc DESC);

-- Outcome events (record_outcome_event)
CREATE TABLE IF NOT EXISTS outcome_event (
    rowid_alias       INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc     TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    outcome_type      TEXT NOT NULL,
    success           INTEGER NOT NULL,
    reward            REAL NOT NULL DEFAULT 0.0,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcome_event_session
    ON outcome_event(session_id, timestamp_utc DESC);

-- Claim table — used by lite_scheduler grade upgrades & surgery-team
CREATE TABLE IF NOT EXISTS claim (
    claim_id        TEXT PRIMARY KEY,
    timestamp_utc   TEXT NOT NULL,
    statement       TEXT NOT NULL,
    evidence_grade  TEXT NOT NULL DEFAULT 'anecdotal',
    status          TEXT NOT NULL DEFAULT 'active',
    source          TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_claim_status ON claim(status);

-- Direct claim outcomes (record_direct_claim_outcome)
CREATE TABLE IF NOT EXISTS direct_claim_outcome (
    rowid_alias     INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id        TEXT NOT NULL,
    timestamp_utc   TEXT NOT NULL,
    success         INTEGER NOT NULL,
    reward          REAL NOT NULL DEFAULT 0.0,
    source          TEXT,
    notes           TEXT,
    FOREIGN KEY (claim_id) REFERENCES claim(claim_id)
);
CREATE INDEX IF NOT EXISTS idx_dco_claim
    ON direct_claim_outcome(claim_id, timestamp_utc DESC);

-- Dependency status events (record_dependency_status)
CREATE TABLE IF NOT EXISTS dependency_status_event (
    dep_event_id      TEXT PRIMARY KEY,
    timestamp_utc     TEXT NOT NULL,
    dependency_name   TEXT NOT NULL,
    status            TEXT NOT NULL,
    latency_ms        INTEGER,
    reason_code       TEXT,
    details_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_dep_status_time
    ON dependency_status_event(timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_dep_status_dep
    ON dependency_status_event(dependency_name, timestamp_utc DESC);

-- Task run events (record_task_run)
CREATE TABLE IF NOT EXISTS task_run_event (
    task_run_id       TEXT PRIMARY KEY,
    timestamp_utc     TEXT NOT NULL,
    task_name         TEXT NOT NULL,
    run_type          TEXT NOT NULL,
    status            TEXT NOT NULL,
    duration_ms       INTEGER NOT NULL,
    budget_ms         INTEGER,
    details_json      TEXT,
    error_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_run_time
    ON task_run_event(timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_task_run_task
    ON task_run_event(task_name, timestamp_utc DESC);

-- Recovery attempts (record_recovery_attempt)
CREATE TABLE IF NOT EXISTS recovery_attempt (
    recovery_id     TEXT PRIMARY KEY,
    timestamp_utc   TEXT NOT NULL,
    service_name    TEXT NOT NULL,
    success         INTEGER NOT NULL,
    duration_ms     INTEGER,
    message         TEXT
);

-- Negative-pattern frequency rollup (get_frequent_negative_patterns)
CREATE TABLE IF NOT EXISTS negative_pattern (
    pattern_key     TEXT PRIMARY KEY,
    frequency       INTEGER NOT NULL DEFAULT 1,
    description     TEXT,
    last_seen_utc   TEXT NOT NULL
);

-- Quarantined items (quarantine_item)
CREATE TABLE IF NOT EXISTS quarantine_item (
    item_id             TEXT PRIMARY KEY,
    timestamp_utc       TEXT NOT NULL,
    item_type           TEXT NOT NULL,
    promotion_rules_json TEXT NOT NULL,
    notes               TEXT
);

-- Schema metadata
CREATE TABLE IF NOT EXISTS observability_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# ZSF counter taxonomy
# ---------------------------------------------------------------------------
# Counters live in-process (per ObservabilityStore instance) and are reported
# verbatim by `get_health()` so multi-fleet `/health` and the gains-gate can
# see degraded states without crashing.  Every code path that can fail
# increments a named counter; no `except: pass` (CLAUDE.md ZSF invariant).
_COUNTER_NAMES = (
    "observability.events.recorded",
    "observability.events.errors",
    "observability.sqlite.write_errors",
    "observability.sqlite.read_errors",
    "observability.sqlite.corruption_recoveries",
    "observability.pg.attach_errors",
    "observability.pg.write_errors",
    "observability.ringbuf.flushes",
    "observability.ringbuf.depth",
    "observability.serialization_errors",
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_dumps(obj: Any) -> str:
    """Serialize to JSON, never raising.  Falls back to a repr() string."""
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.warning("observability serialization failed: %s", e)
        return json.dumps({"_unserializable": repr(obj)[:1000]})


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection — independent of memory.db_utils so
    this module is importable in `migrate3` even before the rest of the
    memory package lands."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    # WAL + reasonable defaults.  These are best-effort PRAGMAs — failure
    # here is non-fatal (the connection still works in rollback-journal mode).
    for pragma in ("PRAGMA journal_mode=WAL",
                   "PRAGMA synchronous=NORMAL",
                   "PRAGMA temp_store=MEMORY"):
        try:
            conn.execute(pragma)
        except sqlite3.DatabaseError as e:
            logger.debug("PRAGMA failed (%s): %s", pragma, e)
    return conn


# ---------------------------------------------------------------------------
# ObservabilityStore
# ---------------------------------------------------------------------------
class ObservabilityStore:
    """Unified observability persistence.

    SQLite is always the primary backend.  PostgreSQL is opt-in and acts as a
    mirror (writes are best-effort; the SQLite write is the source of truth
    for the JSON-RPC API).  A bounded in-memory ring buffer is the
    last-resort fallback when both SQL backends are unavailable.
    """

    def __init__(self, mode: str = "auto", db_path: Optional[Path] = None):
        self.mode = self._resolve_mode(mode)
        self.db_path = Path(db_path) if db_path else SQLITE_DB_PATH
        self._sqlite_conn: Optional[sqlite3.Connection] = None
        self._pg_conn: Any = None
        self._write_lock = threading.Lock()
        self._ringbuf: Deque[Dict[str, Any]] = deque(maxlen=RING_BUFFER_SIZE)
        self._counters: Dict[str, int] = {name: 0 for name in _COUNTER_NAMES}
        self._started_at_utc = _utc_iso()

        self._init_sqlite()
        if self._backend_includes_postgres():
            self._init_postgres()
        self._write_meta()

    # ------------------------------------------------------------------
    # Lifecycle / init
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_mode(mode: str) -> str:
        if mode in {"lite", "heavy"}:
            return mode
        # auto: honour an explicit env hint, otherwise default to lite
        env_mode = os.environ.get("OBSERVABILITY_MODE", "lite").lower()
        return env_mode if env_mode in {"lite", "heavy"} else "lite"

    @staticmethod
    def _backend_includes_postgres() -> bool:
        backend = os.environ.get("OBSERVABILITY_BACKEND", "auto").lower()
        if backend == "sqlite":
            return False
        if backend == "postgres":
            return True
        # auto: only if a DSN is configured
        return bool(os.environ.get("OBSERVABILITY_PG_DSN"))

    def _init_sqlite(self) -> None:
        try:
            self._sqlite_conn = _connect_sqlite(self.db_path)
            # Integrity probe — catches "file is not a database" exactly
            # like the backup did (2026-02-07 P1.2 fix).
            self._sqlite_conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.corruption_recoveries")
            logger.warning(
                "Corrupted observability DB (%s) — rotating and recreating",
                e,
            )
            try:
                if self._sqlite_conn is not None:
                    self._sqlite_conn.close()
            except sqlite3.Error as close_err:
                logger.debug("close-during-recovery failed: %s", close_err)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            corrupted = self.db_path.with_suffix(f".db.corrupted_{ts}")
            try:
                self.db_path.rename(corrupted)
            except OSError as rename_err:
                logger.warning("rename of corrupted DB failed: %s", rename_err)
                try:
                    self.db_path.unlink(missing_ok=True)
                except OSError as unlink_err:
                    logger.warning(
                        "unlink of corrupted DB failed: %s", unlink_err
                    )
            for suffix in ("-wal", "-shm"):
                aux = self.db_path.with_name(self.db_path.name + suffix)
                try:
                    aux.unlink(missing_ok=True)
                except OSError as aux_err:
                    logger.debug("aux cleanup (%s) failed: %s", aux, aux_err)
            self._sqlite_conn = _connect_sqlite(self.db_path)

        assert self._sqlite_conn is not None
        self._sqlite_conn.executescript(OBSERVABILITY_SCHEMA)
        self._sqlite_conn.commit()

    def _init_postgres(self) -> None:
        dsn = os.environ.get("OBSERVABILITY_PG_DSN")
        if not dsn:
            self._bump("observability.pg.attach_errors")
            logger.info("OBSERVABILITY_PG_DSN unset — Postgres mirror disabled")
            return
        try:
            import psycopg2  # type: ignore[import-not-found]

            self._pg_conn = psycopg2.connect(dsn)
            with self._pg_conn:
                with self._pg_conn.cursor() as cur:
                    # Mirror the canonical events table only — the rich
                    # tables stay SQLite-local (typed for the local agent).
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS observability_events (
                            event_id      TEXT PRIMARY KEY,
                            timestamp_utc TIMESTAMPTZ NOT NULL,
                            event_type    TEXT NOT NULL,
                            session_id    TEXT,
                            success       BOOLEAN NOT NULL DEFAULT TRUE,
                            payload       JSONB NOT NULL,
                            details       JSONB
                        );
                        CREATE INDEX IF NOT EXISTS
                            idx_pg_obs_events_time
                            ON observability_events(timestamp_utc DESC);
                        """
                    )
        except ImportError:
            self._bump("observability.pg.attach_errors")
            logger.info("psycopg2 unavailable — Postgres mirror disabled")
            self._pg_conn = None
        except Exception as e:  # noqa: BLE001 — surface AND count
            self._bump("observability.pg.attach_errors")
            logger.warning("Postgres mirror init failed: %s", e)
            self._pg_conn = None

    def _write_meta(self) -> None:
        if self._sqlite_conn is None:
            return
        try:
            with self._write_lock:
                self._sqlite_conn.execute(
                    "INSERT OR REPLACE INTO observability_meta(key,value) "
                    "VALUES (?,?)",
                    ("schema_version", OBSERVABILITY_SCHEMA_VERSION),
                )
                self._sqlite_conn.execute(
                    "INSERT OR REPLACE INTO observability_meta(key,value) "
                    "VALUES (?,?)",
                    ("started_at_utc", self._started_at_utc),
                )
                self._sqlite_conn.commit()
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.write_errors")
            logger.warning("meta write failed: %s", e)

    # ------------------------------------------------------------------
    # Counter helpers (ZSF surface)
    # ------------------------------------------------------------------
    def _bump(self, name: str, n: int = 1) -> None:
        # `dict.get` with default keeps the counter dict tolerant of new names
        # introduced by future code paths.  No raise, no silent pass.
        self._counters[name] = self._counters.get(name, 0) + n

    # ------------------------------------------------------------------
    # Internal write helpers
    # ------------------------------------------------------------------
    def _sqlite_exec(self, sql: str, params: tuple = ()) -> bool:
        """Execute a single statement under the write lock.  Returns True on
        success, False on error (which is ALSO counted)."""
        if self._sqlite_conn is None:
            self._bump("observability.sqlite.write_errors")
            return False
        try:
            with self._write_lock:
                self._sqlite_conn.execute(sql, params)
                self._sqlite_conn.commit()
            return True
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.write_errors")
            logger.warning("sqlite write failed: %s — sql=%s", e, sql[:120])
            return False

    def _pg_mirror(self, event_id: str, timestamp_utc: str, event_type: str,
                   session_id: Optional[str], success: bool,
                   payload_json: str, details_json: Optional[str]) -> None:
        if self._pg_conn is None:
            return
        try:
            with self._pg_conn:
                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO observability_events "
                        "(event_id, timestamp_utc, event_type, session_id, "
                        " success, payload, details) "
                        "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) "
                        "ON CONFLICT (event_id) DO NOTHING",
                        (event_id, timestamp_utc, event_type, session_id,
                         success, payload_json, details_json),
                    )
        except Exception as e:  # noqa: BLE001 — surface AND count
            self._bump("observability.pg.write_errors")
            logger.warning("postgres mirror write failed: %s", e)

    def _ringbuf_append(self, record: Dict[str, Any]) -> None:
        self._ringbuf.append(record)
        self._counters["observability.ringbuf.depth"] = len(self._ringbuf)
        self._bump("observability.ringbuf.flushes")

    # ------------------------------------------------------------------
    # Public API — event recording
    # ------------------------------------------------------------------
    def record_event(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        success: bool = True,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Canonical JSON-RPC `record_event` surface.

        Returns the generated event_id (also useful for tests).
        """
        event_id = f"ev_{uuid.uuid4().hex[:16]}"
        timestamp_utc = _utc_iso()
        payload_json = _safe_json_dumps(payload or {})
        details_json = _safe_json_dumps(details) if details is not None else None
        # serialization guard
        if payload_json.startswith('{"_unserializable"'):
            self._bump("observability.serialization_errors")

        ok = self._sqlite_exec(
            "INSERT INTO observability_events "
            "(event_id, timestamp_utc, event_type, session_id, success, "
            " payload_json, details_json) VALUES (?,?,?,?,?,?,?)",
            (event_id, timestamp_utc, event_type, session_id,
             1 if success else 0, payload_json, details_json),
        )
        if ok:
            self._bump("observability.events.recorded")
            self._pg_mirror(event_id, timestamp_utc, event_type, session_id,
                            success, payload_json, details_json)
        else:
            self._bump("observability.events.errors")
            self._ringbuf_append({
                "event_id": event_id,
                "timestamp_utc": timestamp_utc,
                "event_type": event_type,
                "session_id": session_id,
                "success": success,
                "payload_json": payload_json,
                "details_json": details_json,
            })
        return event_id

    def record_outcome_event(
        self,
        session_id: str,
        outcome_type: str,
        success: bool,
        reward: float = 0.0,
        notes: str = "",
    ) -> int:
        """Legacy caller (session_gold_passes / lite_scheduler)."""
        ok = self._sqlite_exec(
            "INSERT INTO outcome_event "
            "(timestamp_utc, session_id, outcome_type, success, reward, notes) "
            "VALUES (?,?,?,?,?,?)",
            (_utc_iso(), session_id, outcome_type,
             1 if success else 0, float(reward), notes or ""),
        )
        if ok:
            self._bump("observability.events.recorded")
        else:
            self._bump("observability.events.errors")
            self._ringbuf_append({
                "kind": "outcome_event",
                "session_id": session_id,
                "outcome_type": outcome_type,
                "success": success,
                "reward": reward,
                "notes": notes,
            })
        # Mirror into the generic event log so JSON-RPC consumers see it.
        self.record_event(
            event_type=f"outcome.{outcome_type}",
            payload={"reward": float(reward), "notes": notes},
            session_id=session_id,
            success=success,
        )
        return 1 if ok else 0

    def record_direct_claim_outcome(
        self,
        claim_id: str,
        success: bool,
        reward: float = 0.0,
        source: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> bool:
        """Legacy caller (session_gold_passes pass 13 evidence audit)."""
        # Ensure the parent claim row exists (FK).  If it doesn't, create a
        # synthetic placeholder so we never lose the outcome (ZSF).
        if self._sqlite_conn is not None:
            try:
                row = self._sqlite_conn.execute(
                    "SELECT claim_id FROM claim WHERE claim_id=?",
                    (claim_id,),
                ).fetchone()
                if row is None:
                    self._sqlite_exec(
                        "INSERT INTO claim "
                        "(claim_id, timestamp_utc, statement, "
                        " evidence_grade, status, source) "
                        "VALUES (?,?,?,?,?,?)",
                        (claim_id, _utc_iso(),
                         f"synthetic placeholder for {claim_id}",
                         "anecdotal", "active", source or "auto"),
                    )
            except sqlite3.DatabaseError as e:
                self._bump("observability.sqlite.read_errors")
                logger.warning("claim lookup failed: %s", e)

        ok = self._sqlite_exec(
            "INSERT INTO direct_claim_outcome "
            "(claim_id, timestamp_utc, success, reward, source, notes) "
            "VALUES (?,?,?,?,?,?)",
            (claim_id, _utc_iso(), 1 if success else 0,
             float(reward), source, notes),
        )
        if ok:
            self._bump("observability.events.recorded")
        else:
            self._bump("observability.events.errors")
        return ok

    def record_dependency_status(
        self,
        dependency_name: str,
        status: str,
        latency_ms: Optional[int] = None,
        reason_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        event_id = f"dep_{uuid.uuid4().hex[:16]}"
        ok = self._sqlite_exec(
            "INSERT INTO dependency_status_event "
            "(dep_event_id, timestamp_utc, dependency_name, status, "
            " latency_ms, reason_code, details_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (event_id, _utc_iso(), dependency_name, status,
             latency_ms, reason_code,
             _safe_json_dumps(details) if details is not None else None),
        )
        if ok:
            self._bump("observability.events.recorded")
        else:
            self._bump("observability.events.errors")
        return event_id

    def record_task_run(
        self,
        task_name: str,
        run_type: str,
        status: str,
        duration_ms: int,
        budget_ms: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        event_id = f"tr_{uuid.uuid4().hex[:16]}"
        ok = self._sqlite_exec(
            "INSERT INTO task_run_event "
            "(task_run_id, timestamp_utc, task_name, run_type, status, "
            " duration_ms, budget_ms, details_json, error_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id, _utc_iso(), task_name, run_type, status,
             int(duration_ms), budget_ms,
             _safe_json_dumps(details) if details is not None else None,
             _safe_json_dumps(error) if error is not None else None),
        )
        if ok:
            self._bump("observability.events.recorded")
        else:
            self._bump("observability.events.errors")
        return event_id

    def record_recovery_attempt(
        self,
        service_name: str,
        success: bool,
        duration_ms: Optional[int] = None,
        message: Optional[str] = None,
    ) -> str:
        event_id = f"rec_{uuid.uuid4().hex[:16]}"
        ok = self._sqlite_exec(
            "INSERT INTO recovery_attempt "
            "(recovery_id, timestamp_utc, service_name, success, "
            " duration_ms, message) VALUES (?,?,?,?,?,?)",
            (event_id, _utc_iso(), service_name,
             1 if success else 0, duration_ms, message),
        )
        if ok:
            self._bump("observability.events.recorded")
        else:
            self._bump("observability.events.errors")
        return event_id

    def quarantine_item(
        self,
        item_id: str,
        item_type: str,
        promotion_rules: Dict[str, Any],
        notes: Optional[str] = None,
    ) -> bool:
        ok = self._sqlite_exec(
            "INSERT OR REPLACE INTO quarantine_item "
            "(item_id, timestamp_utc, item_type, promotion_rules_json, notes) "
            "VALUES (?,?,?,?,?)",
            (item_id, _utc_iso(), item_type,
             _safe_json_dumps(promotion_rules or {}), notes),
        )
        if ok:
            self._bump("observability.events.recorded")
        else:
            self._bump("observability.events.errors")
        return ok

    # ------------------------------------------------------------------
    # Public API — queries
    # ------------------------------------------------------------------
    def _rows_to_dicts(self, rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
        return [dict(r) for r in rows]

    def get_events(
        self,
        event_type: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if self._sqlite_conn is None:
            return []
        sql = "SELECT * FROM observability_events WHERE 1=1"
        params: List[Any] = []
        if event_type is not None:
            sql += " AND event_type=?"
            params.append(event_type)
        if session_id is not None:
            sql += " AND session_id=?"
            params.append(session_id)
        sql += " ORDER BY timestamp_utc DESC LIMIT ?"
        params.append(int(limit))
        try:
            rows = self._sqlite_conn.execute(sql, params).fetchall()
            return self._rows_to_dicts(rows)
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.read_errors")
            logger.warning("get_events failed: %s", e)
            return []

    def query_events(
        self,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """JSON-RPC `query_events` surface.

        Filters: event_type, session_id, success, since_utc (ISO8601).
        """
        filters = filters or {}
        if self._sqlite_conn is None:
            return []
        clauses = ["1=1"]
        params: List[Any] = []
        if "event_type" in filters:
            clauses.append("event_type=?")
            params.append(filters["event_type"])
        if "session_id" in filters:
            clauses.append("session_id=?")
            params.append(filters["session_id"])
        if "success" in filters:
            clauses.append("success=?")
            params.append(1 if filters["success"] else 0)
        if "since_utc" in filters:
            clauses.append("timestamp_utc >= ?")
            params.append(filters["since_utc"])
        sql = (
            "SELECT * FROM observability_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY timestamp_utc DESC LIMIT ?"
        )
        params.append(int(limit))
        try:
            rows = self._sqlite_conn.execute(sql, params).fetchall()
            return self._rows_to_dicts(rows)
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.read_errors")
            logger.warning("query_events failed: %s", e)
            return []

    def get_session_trace(
        self, session_id: str, limit: int = 500
    ) -> List[Dict[str, Any]]:
        if self._sqlite_conn is None:
            return []
        try:
            rows = self._sqlite_conn.execute(
                "SELECT * FROM observability_events WHERE session_id=? "
                "ORDER BY timestamp_utc ASC LIMIT ?",
                (session_id, int(limit)),
            ).fetchall()
            return self._rows_to_dicts(rows)
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.read_errors")
            logger.warning("get_session_trace failed: %s", e)
            return []

    def get_recent_failures(
        self, hours: int = 24, limit: int = 50
    ) -> List[Dict[str, Any]]:
        if self._sqlite_conn is None:
            return []
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(hours=hours)).isoformat()
        try:
            rows = self._sqlite_conn.execute(
                "SELECT * FROM observability_events "
                "WHERE success=0 AND timestamp_utc >= ? "
                "ORDER BY timestamp_utc DESC LIMIT ?",
                (cutoff, int(limit)),
            ).fetchall()
            return self._rows_to_dicts(rows)
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.read_errors")
            logger.warning("get_recent_failures failed: %s", e)
            return []

    def get_claims_by_status(
        self, status: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        if self._sqlite_conn is None:
            return []
        try:
            rows = self._sqlite_conn.execute(
                "SELECT * FROM claim WHERE status=? "
                "ORDER BY timestamp_utc DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
            return self._rows_to_dicts(rows)
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.read_errors")
            logger.warning("get_claims_by_status failed: %s", e)
            return []

    def get_frequent_negative_patterns(
        self, min_frequency: int = 2
    ) -> List[Dict[str, Any]]:
        if self._sqlite_conn is None:
            return []
        try:
            rows = self._sqlite_conn.execute(
                "SELECT pattern_key, frequency, description, last_seen_utc "
                "FROM negative_pattern WHERE frequency >= ? "
                "ORDER BY frequency DESC, last_seen_utc DESC",
                (int(min_frequency),),
            ).fetchall()
            return self._rows_to_dicts(rows)
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.read_errors")
            logger.warning("get_frequent_negative_patterns failed: %s", e)
            return []

    def get_recent_task_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        if self._sqlite_conn is None:
            return []
        try:
            rows = self._sqlite_conn.execute(
                "SELECT * FROM task_run_event "
                "ORDER BY timestamp_utc DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return self._rows_to_dicts(rows)
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.read_errors")
            logger.warning("get_recent_task_runs failed: %s", e)
            return []

    def get_recent_dependency_events(
        self, limit: int = 20
    ) -> List[Dict[str, Any]]:
        if self._sqlite_conn is None:
            return []
        try:
            rows = self._sqlite_conn.execute(
                "SELECT * FROM dependency_status_event "
                "ORDER BY timestamp_utc DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return self._rows_to_dicts(rows)
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.read_errors")
            logger.warning("get_recent_dependency_events failed: %s", e)
            return []

    def get_dependency_summary(self, hours: int = 24) -> Dict[str, Any]:
        if self._sqlite_conn is None:
            return {}
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(hours=hours)).isoformat()
        try:
            rows = self._sqlite_conn.execute(
                "SELECT dependency_name, status, timestamp_utc "
                "FROM dependency_status_event WHERE timestamp_utc >= ? "
                "ORDER BY timestamp_utc ASC",
                (cutoff,),
            ).fetchall()
        except sqlite3.DatabaseError as e:
            self._bump("observability.sqlite.read_errors")
            logger.warning("get_dependency_summary failed: %s", e)
            return {}
        summary: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            name = row["dependency_name"]
            entry = summary.setdefault(name, {
                "total_checks": 0,
                "healthy_count": 0,
                "last_status": None,
                "last_check": None,
            })
            entry["total_checks"] += 1
            if row["status"] == "healthy":
                entry["healthy_count"] += 1
            entry["last_status"] = row["status"]
            entry["last_check"] = row["timestamp_utc"]
        for entry in summary.values():
            entry["healthy_pct"] = (
                100.0 * entry["healthy_count"] / entry["total_checks"]
                if entry["total_checks"] else 0.0
            )
        return summary

    # ------------------------------------------------------------------
    # Public API — health
    # ------------------------------------------------------------------
    def get_health(self) -> Dict[str, Any]:
        """JSON-RPC `get_health` surface.

        Returns a dict with status (`ok`|`degraded`|`down`), backend mix,
        counter taxonomy, ring-buffer depth and schema version.
        """
        sqlite_ok = self._sqlite_conn is not None
        pg_attached = self._pg_conn is not None
        ringbuf_depth = len(self._ringbuf)

        # Status logic: ok if SQLite is up AND ring buffer depth is low.
        if not sqlite_ok:
            status = "down"
        elif ringbuf_depth > 0 or self._counters.get(
            "observability.sqlite.write_errors", 0
        ) > 0:
            status = "degraded"
        else:
            status = "ok"

        return {
            "status": status,
            "schema_version": OBSERVABILITY_SCHEMA_VERSION,
            "started_at_utc": self._started_at_utc,
            "mode": self.mode,
            "backend": {
                "sqlite": {
                    "attached": sqlite_ok,
                    "path": str(self.db_path),
                },
                "postgres": {
                    "attached": pg_attached,
                    "dsn_set": bool(os.environ.get("OBSERVABILITY_PG_DSN")),
                },
                "ringbuf": {
                    "depth": ringbuf_depth,
                    "capacity": RING_BUFFER_SIZE,
                },
            },
            "counters": dict(self._counters),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        try:
            if self._sqlite_conn is not None:
                self._sqlite_conn.close()
        except sqlite3.Error as e:
            logger.warning("sqlite close failed: %s", e)
        try:
            if self._pg_conn is not None:
                self._pg_conn.close()
        except Exception as e:  # noqa: BLE001 — psycopg2 errors are class hierarchy
            logger.warning("postgres close failed: %s", e)


# ---------------------------------------------------------------------------
# Module-level convenience surface (JSON-RPC compatible)
# ---------------------------------------------------------------------------
_store_instance: Optional[ObservabilityStore] = None
_store_lock = threading.Lock()


def get_observability_store() -> ObservabilityStore:
    """Thread-safe singleton accessor.  All callers should prefer this over
    instantiating ObservabilityStore directly so that the SQLite connection
    is reused (sqlite3 connections are not cheap on macOS APFS)."""
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = ObservabilityStore()
    return _store_instance


def record_event(
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    success: bool = True,
    details: Optional[Dict[str, Any]] = None,
) -> str:
    return get_observability_store().record_event(
        event_type=event_type,
        payload=payload,
        session_id=session_id,
        success=success,
        details=details,
    )


def get_events(
    event_type: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    return get_observability_store().get_events(
        event_type=event_type, session_id=session_id, limit=limit
    )


def query_events(
    filters: Optional[Dict[str, Any]] = None, limit: int = 200
) -> List[Dict[str, Any]]:
    return get_observability_store().query_events(filters=filters, limit=limit)


def get_session_trace(
    session_id: str, limit: int = 500
) -> List[Dict[str, Any]]:
    return get_observability_store().get_session_trace(
        session_id=session_id, limit=limit
    )


def get_recent_failures(
    hours: int = 24, limit: int = 50
) -> List[Dict[str, Any]]:
    return get_observability_store().get_recent_failures(
        hours=hours, limit=limit
    )


def get_health() -> Dict[str, Any]:
    return get_observability_store().get_health()


__all__ = [
    "ObservabilityStore",
    "OBSERVABILITY_SCHEMA",
    "OBSERVABILITY_SCHEMA_VERSION",
    "SQLITE_DB_PATH",
    "get_observability_store",
    "record_event",
    "get_events",
    "query_events",
    "get_session_trace",
    "get_recent_failures",
    "get_health",
]


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover — manual smoke
    import sys

    store = get_observability_store()
    if len(sys.argv) < 2:
        print(f"observability_store schema={OBSERVABILITY_SCHEMA_VERSION}")
        print(f"db_path={store.db_path}")
        print(json.dumps(store.get_health(), indent=2))
        sys.exit(0)
    cmd = sys.argv[1].lower()
    if cmd == "smoke":
        eid = store.record_event(
            event_type="smoke.test",
            payload={"ts": time.time()},
            session_id="cli-smoke",
            success=True,
        )
        print(f"recorded {eid}")
        print(json.dumps(store.get_events(limit=3), indent=2, default=str))
    elif cmd == "health":
        print(json.dumps(store.get_health(), indent=2))
    else:
        print(f"unknown command: {cmd}")
        sys.exit(2)
