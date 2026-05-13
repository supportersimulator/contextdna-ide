"""Evidence Ledger v1 — generalized, content-addressed evidence registry.

Purpose
-------
Lifts the design of v6 ``experiment_evidence.py`` (sha256 + git-rev-parse +
ProductBridge.fleet_to_memory pattern) into a **generalized** Evidence Ledger
that fills the TrialBench v1 missing trial-registry gap (per R4 audit).

This module is the canonical *kind-agnostic* registry for evidence records:
experiments, competitions, trials, decisions, audits — anything we want to
content-address, immutably store, and DAG-link via parent_ids. It is **not**
a replacement for `memory/outcome_ledger.jsonl` (Q3) or
`multi-fleet/multifleet/evidence_ledger.py` (the fleet hash-chain ledger);
it **bridges** to both:

  - `from_outcome_ledger()` ingests existing Q3 records as kind="outcome".
  - `to_brain(record)` writes a brain memory entry keyed by record_id.

Design (Constitutional Physics)
-------------------------------
- **Determinism**: record_id = sha256 of canonical-JSON content (sorted keys,
  ISO timestamp normalized, parent_ids as sorted list). Same content -> same
  record_id, every time, every machine.
- **Idempotence**: writing the same content twice is a no-op (dedupe counter
  bumps); record() always returns the canonical record.
- **No discovery at write**: we never call out to git or read files at record
  time *unless* the caller passes a context dict with those values already
  resolved. (We DO offer a helper `current_git_rev()` but it is opt-in.)
- **Evidence over confidence**: records are immutable; there is no UPDATE
  path. To revise, write a new record with parent_ids=[old_id].
- **Reversibility**: revertible in one git revert + delete `evidence_ledger.db`.

Storage
-------
SQLite at ``memory/evidence_ledger.db`` (single table, idempotent schema,
WAL mode). Bridges out to context_dna brain (best-effort) and Q3 ledger (one
shot backfill, idempotent).

ZSF (Zero Silent Failures)
--------------------------
Module-level counters (daemon-scrapable, no Prometheus dep):

  - ``evidence_records_total``       — successful new records
  - ``evidence_dedupe_hits_total``   — same-content writes (no-op)
  - ``evidence_write_errors_total``  — write failures (logged, NOT swallowed)
  - ``evidence_brain_bridge_errors_total`` — brain-bridge failures (logged)
  - ``evidence_outcome_backfill_total`` — outcomes ingested via backfill

Contract: every error path increments a counter AND logs with full context;
no bare ``except: pass``. Write failures re-raise after counter+log.

Public API
----------
  - ``EvidenceRecord`` dataclass
  - ``EvidenceLedger`` class
      .record(content, kind, parent_ids=None) -> EvidenceRecord
      .query(kind=None, since=None) -> list[EvidenceRecord]
      .get(record_id) -> EvidenceRecord | None
      .lineage(record_id) -> list[EvidenceRecord]
      .to_brain(record) -> bool
      .from_outcome_ledger() -> int
      .redact_record(record_id, reason, actor, redacted_marker="[REDACTED]") -> dict
      .verify_chain() -> dict
  - ``EvidenceKind`` enum (experiment / competition / trial / decision / audit / outcome / redaction)
  - ``EvidenceLedgerError`` (raised by redact_record on missing target)

stdlib only.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import hashlib
import json
import logging
import pathlib
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_THIS_FILE = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parent.parent  # memory/.. -> superrepo

DB_PATH: pathlib.Path = _REPO_ROOT / "memory" / "evidence_ledger.db"
SCHEMA_VERSION = "v1"


class EvidenceKind(str, enum.Enum):
    EXPERIMENT = "experiment"
    COMPETITION = "competition"
    TRIAL = "trial"
    DECISION = "decision"
    AUDIT = "audit"
    OUTCOME = "outcome"  # backfill from Q3 outcome_ledger
    REDACTION = "redaction"  # tombstone marker for post-hoc redaction (W1.b)
    MISSION_MEMENTO = "mission_memento"  # OpenPath Phase-13 GG5 — Mission Memento

    @classmethod
    def coerce(cls, value: Any) -> "EvidenceKind":
        """Accept str or enum; raise on unknown value (NOT silent)."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError as e:
                # Surface the bad kind explicitly — callers get full context.
                raise ValueError(
                    f"unknown EvidenceKind {value!r}; "
                    f"valid: {[k.value for k in cls]}"
                ) from e
        raise TypeError(f"EvidenceKind expects str or EvidenceKind, got {type(value)!r}")


# ZSF: module-level error counters (daemon-scrapable)
COUNTERS: dict[str, int] = {
    "evidence_records_total": 0,
    "evidence_dedupe_hits_total": 0,
    "evidence_write_errors_total": 0,
    "evidence_brain_bridge_errors_total": 0,
    "evidence_outcome_backfill_total": 0,
    "evidence_query_errors_total": 0,
    "evidence_lineage_errors_total": 0,
    # W1.b — post-hoc REDACT tombstone API
    "ledger_redact_ok_total": 0,
    "ledger_redact_errors_total": 0,
    "ledger_redact_target_missing_total": 0,
    # verify_chain() observability
    "evidence_chain_verify_ok_total": 0,
    "evidence_chain_verify_mismatch_total": 0,
    # GG5 — Mission Memento writer (OpenPath Phase-13)
    "mission_memento_records_total": 0,
    "mission_memento_invalid_total": 0,
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class EvidenceLedgerError(Exception):
    """Raised by EvidenceLedger when an operation cannot proceed.

    Examples:
      * ``redact_record(record_id=...)`` invoked with an unknown record_id
        — caller cannot tombstone what is not in the ledger.
      * Caller-supplied marker is the empty string (would silently zero out
        the target payload with an indistinguishable value).

    Distinct from ``ValueError`` / ``TypeError`` (input shape) and
    ``sqlite3.Error`` (storage) so callers can branch on intent.
    """


def _bump(counter: str, n: int = 1) -> None:
    COUNTERS[counter] = COUNTERS.get(counter, 0) + n


logger = logging.getLogger("memory.evidence_ledger")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s evidence_ledger %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# Per-DB write locks (process-local; SQLite handles inter-process via WAL)
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _get_write_lock(db_path: str) -> threading.Lock:
    resolved = str(pathlib.Path(db_path).resolve())
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[resolved] = lock
        return lock


# ---------------------------------------------------------------------------
# Helpers (deterministic — no I/O at write time unless explicitly invoked)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """ISO-8601 UTC timestamp, second precision (deterministic across machines)."""
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")


def current_git_rev(cwd: pathlib.Path | str | None = None) -> str | None:
    """Return current git HEAD or None (opt-in helper, NOT auto-called).

    Callers that want git provenance should resolve it once and pass into
    ``record(content={"git_rev": rev, ...})`` — this keeps record() pure /
    deterministic. We expose the helper so callers don't reinvent it.
    """
    cwd = pathlib.Path(cwd) if cwd else _REPO_ROOT
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip() or None
    except (subprocess.SubprocessError, OSError, FileNotFoundError) as e:
        logger.debug("current_git_rev unavailable: %s", e)
        return None


def _canonical_content(
    content: dict[str, Any],
    kind: str,
    parent_ids: tuple[str, ...],
    schema_version: str,
) -> str:
    """Canonical JSON for hashing.

    Sort keys, separators with no whitespace, parent_ids as sorted tuple.
    Content itself is sorted recursively via sort_keys=True.
    """
    canonical = {
        "kind": kind,
        "schema_version": schema_version,
        "parent_ids": list(parent_ids),  # already sorted by caller
        "content": content,
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)


def _hash_content(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceRecord:
    """Immutable evidence record.

    record_id = sha256 of canonical(content+kind+parent_ids+schema_version).
    parent_ids is a frozenset for DAG semantics; canonicalization sorts.

    Redaction metadata (W1.b)
    -------------------------
    Records that have been post-hoc redacted via :meth:`EvidenceLedger.redact_record`
    carry the tombstone's record_id in ``redacted_by_record_id`` and an ISO-8601
    timestamp in ``redacted_at``. ``content`` for a redacted record is rendered
    as ``{"__redacted__": "<marker>", ...}`` (NOT the original payload).
    The ``record_id`` (= sha256 of original canonical content) is unchanged.
    """

    record_id: str
    content: dict[str, Any]
    kind: str
    parent_ids: frozenset[str]
    created_at: str
    schema_version: str = SCHEMA_VERSION
    git_rev: str | None = None  # convenience field; if present in content, mirrored
    redacted_by_record_id: str | None = None
    redacted_at: str | None = None

    @property
    def is_redacted(self) -> bool:
        return bool(self.redacted_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "content": self.content,
            "kind": self.kind,
            "parent_ids": sorted(self.parent_ids),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "git_rev": self.git_rev,
            "redacted_by_record_id": self.redacted_by_record_id,
            "redacted_at": self.redacted_at,
        }


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

class EvidenceLedger:
    """Content-addressed Evidence Ledger backed by SQLite.

    Idempotent: writing the same (content, kind, parent_ids) twice is a no-op
    (returns the existing record, bumps dedupe counter). Records are
    immutable — there is no update API. To revise, record a new record with
    ``parent_ids=[old_id]``.

    Bridges
    -------
    - to_brain(record): writes a brain memory entry via context_dna.brain
      (best-effort; bridge errors counter-tracked, NOT silenced).
    - from_outcome_ledger(): one-shot idempotent backfill of Q3 entries.
    """

    def __init__(self, db_path: str | pathlib.Path | None = None):
        self._db_path = str(pathlib.Path(db_path) if db_path else DB_PATH)
        pathlib.Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = _get_write_lock(self._db_path)
        self._init_db()

    # -- schema --------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._write_lock:
            conn = self._conn()
            try:
                # One canonical table; second table for parent edges (DAG).
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_records (
                        record_id       TEXT PRIMARY KEY,
                        content_json    TEXT NOT NULL,
                        kind            TEXT NOT NULL,
                        created_at      TEXT NOT NULL,
                        schema_version  TEXT NOT NULL,
                        git_rev         TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_parents (
                        child_id   TEXT NOT NULL,
                        parent_id  TEXT NOT NULL,
                        PRIMARY KEY (child_id, parent_id),
                        FOREIGN KEY (child_id)  REFERENCES evidence_records(record_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_kind "
                    "ON evidence_records(kind)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_created "
                    "ON evidence_records(created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_parents_parent "
                    "ON evidence_parents(parent_id)"
                )
                # W1.b — post-hoc redaction columns. Added via ALTER TABLE so
                # existing v1 DBs migrate forward without losing rows. Both
                # NULL by default; only the redact_record() path populates them.
                self._migrate_add_column_if_missing(
                    conn, "evidence_records", "redacted_by_record_id", "TEXT"
                )
                self._migrate_add_column_if_missing(
                    conn, "evidence_records", "redacted_at", "TEXT"
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _migrate_add_column_if_missing(
        conn: sqlite3.Connection, table: str, column: str, coltype: str
    ) -> None:
        """Idempotent ALTER TABLE ... ADD COLUMN. Safe to run on every open.

        SQLite has no ``ADD COLUMN IF NOT EXISTS``; we read PRAGMA table_info
        and only add when the column is absent. Failures bubble up — the
        ledger refuses to operate on a half-migrated schema (ZSF).
        """
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {r["name"] for r in rows}
        if column in existing:
            return
        # NB: column / coltype are constants in our caller; no SQL injection
        # surface, but keep parameterized identifiers out of f-strings in any
        # future caller that takes user input.
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"
        )
        logger.info("evidence_ledger schema migrated: added %s.%s", table, column)

    # -- write ---------------------------------------------------------------

    def record(
        self,
        content: dict[str, Any],
        kind: str | EvidenceKind,
        parent_ids: Iterable[str] | None = None,
    ) -> EvidenceRecord:
        """Record (or re-fetch) a content-addressed evidence record.

        Idempotent: same (content, kind, parent_ids) -> same record_id ->
        no DB write, dedupe counter bumps.

        Raises:
            ValueError: kind unknown.
            TypeError: content not a dict.
            sqlite3.Error: re-raised after counter+log on write failure.
        """
        if not isinstance(content, dict):
            raise TypeError(f"content must be dict, got {type(content).__name__}")

        kind_enum = EvidenceKind.coerce(kind)
        kind_str = kind_enum.value

        # Canonicalize parent_ids -> sorted tuple of strings (DAG-stable hash)
        if parent_ids is None:
            parents_tuple: tuple[str, ...] = ()
        else:
            parents_tuple = tuple(sorted({str(p) for p in parent_ids if p}))

        canonical = _canonical_content(
            content=content,
            kind=kind_str,
            parent_ids=parents_tuple,
            schema_version=SCHEMA_VERSION,
        )
        record_id = _hash_content(canonical)
        git_rev = content.get("git_rev") if isinstance(content.get("git_rev"), str) else None

        with self._write_lock:
            conn = self._conn()
            try:
                # Idempotency check: same record_id already present -> no-op.
                row = conn.execute(
                    "SELECT record_id, content_json, kind, created_at, "
                    "schema_version, git_rev, redacted_by_record_id, redacted_at "
                    "FROM evidence_records WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                if row is not None:
                    _bump("evidence_dedupe_hits_total")
                    return self._row_to_record(row, conn)

                created_at = _now_iso()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO evidence_records "
                        "(record_id, content_json, kind, created_at, schema_version, git_rev) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            record_id,
                            json.dumps(content, sort_keys=True, default=str),
                            kind_str,
                            created_at,
                            SCHEMA_VERSION,
                            git_rev,
                        ),
                    )
                    for parent_id in parents_tuple:
                        conn.execute(
                            "INSERT OR IGNORE INTO evidence_parents "
                            "(child_id, parent_id) VALUES (?, ?)",
                            (record_id, parent_id),
                        )
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    _bump("evidence_write_errors_total")
                    logger.error(
                        "evidence write failure record_id=%s kind=%s err=%s",
                        record_id[:12], kind_str, e,
                    )
                    raise

                _bump("evidence_records_total")
                logger.info(
                    "evidence recorded id=%s kind=%s parents=%d",
                    record_id[:12], kind_str, len(parents_tuple),
                )
                return EvidenceRecord(
                    record_id=record_id,
                    content=content,
                    kind=kind_str,
                    parent_ids=frozenset(parents_tuple),
                    created_at=created_at,
                    schema_version=SCHEMA_VERSION,
                    git_rev=git_rev,
                )
            finally:
                conn.close()

    # -- read ----------------------------------------------------------------

    def get(self, record_id: str) -> EvidenceRecord | None:
        if not record_id:
            return None
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT record_id, content_json, kind, created_at, "
                "schema_version, git_rev, redacted_by_record_id, redacted_at "
                "FROM evidence_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            return self._row_to_record(row, conn) if row else None
        except sqlite3.Error as e:
            _bump("evidence_query_errors_total")
            logger.error("evidence get failure record_id=%s err=%s", record_id[:12], e)
            raise
        finally:
            conn.close()

    def query(
        self,
        kind: str | EvidenceKind | None = None,
        since: str | None = None,
    ) -> list[EvidenceRecord]:
        """Query records, newest first. ``since`` is ISO-8601 UTC string."""
        conn = self._conn()
        try:
            sql = (
                "SELECT record_id, content_json, kind, created_at, "
                "schema_version, git_rev, redacted_by_record_id, redacted_at "
                "FROM evidence_records"
            )
            conditions: list[str] = []
            params: list[Any] = []
            if kind is not None:
                kind_str = EvidenceKind.coerce(kind).value
                conditions.append("kind = ?")
                params.append(kind_str)
            if since:
                conditions.append("created_at >= ?")
                params.append(since)
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC, record_id DESC"
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_record(r, conn) for r in rows]
        except sqlite3.Error as e:
            _bump("evidence_query_errors_total")
            logger.error("evidence query failure kind=%s err=%s", kind, e)
            raise
        finally:
            conn.close()

    def lineage(self, record_id: str) -> list[EvidenceRecord]:
        """Walk parent_ids transitively. Returns ancestors (oldest-first).

        Cycle-safe via visited set. Self-records included if you walk from a
        node to itself via parent edges (shouldn't happen w/ content-address).
        """
        if not record_id:
            return []
        conn = self._conn()
        try:
            visited: set[str] = set()
            ordered: list[EvidenceRecord] = []
            frontier: list[str] = [record_id]
            while frontier:
                cur = frontier.pop(0)
                if cur in visited:
                    continue
                visited.add(cur)
                row = conn.execute(
                    "SELECT record_id, content_json, kind, created_at, "
                    "schema_version, git_rev, redacted_by_record_id, redacted_at "
                    "FROM evidence_records WHERE record_id = ?",
                    (cur,),
                ).fetchone()
                if row is None:
                    continue
                rec = self._row_to_record(row, conn)
                if cur != record_id:  # exclude root from lineage list
                    ordered.append(rec)
                parent_rows = conn.execute(
                    "SELECT parent_id FROM evidence_parents WHERE child_id = ?",
                    (cur,),
                ).fetchall()
                for pr in parent_rows:
                    pid = pr["parent_id"]
                    if pid not in visited:
                        frontier.append(pid)
            # Oldest first
            ordered.sort(key=lambda r: r.created_at)
            return ordered
        except sqlite3.Error as e:
            _bump("evidence_lineage_errors_total")
            logger.error("evidence lineage failure id=%s err=%s", record_id[:12], e)
            raise
        finally:
            conn.close()

    # -- bridges -------------------------------------------------------------

    def to_brain(self, record: EvidenceRecord) -> bool:
        """Write a brain memory entry keyed by ``record.record_id``.

        Best-effort bridge. Returns True on success, False if brain
        is unavailable or the write failed (counter bumped, error logged).
        Does NOT raise — brain availability varies by environment.
        """
        try:
            # Lazy import — context_dna.brain may not be importable in all envs.
            from context_dna import brain as _brain  # type: ignore
        except ImportError as e:
            _bump("evidence_brain_bridge_errors_total")
            logger.warning(
                "to_brain: context_dna.brain unavailable (%s); "
                "record_id=%s NOT bridged",
                e, record.record_id[:12],
            )
            return False

        # Build a compact "task / details" payload the brain.win API expects.
        task = f"evidence:{record.kind}:{record.record_id[:16]}"
        details = json.dumps(
            {
                "record_id": record.record_id,
                "kind": record.kind,
                "created_at": record.created_at,
                "git_rev": record.git_rev,
                "parent_ids": sorted(record.parent_ids),
                # Trim content for brain memory (full record stays in SQLite).
                "content_summary": {
                    k: v for k, v in list(record.content.items())[:6]
                },
            },
            sort_keys=True,
            default=str,
        )
        try:
            ok = bool(_brain.win(task=task, details=details, area="evidence_ledger"))
            if not ok:
                _bump("evidence_brain_bridge_errors_total")
                logger.warning("brain.win returned False for %s", record.record_id[:12])
            return ok
        except Exception as e:  # brain has many backends; surface explicitly
            _bump("evidence_brain_bridge_errors_total")
            logger.error(
                "to_brain failure record_id=%s err=%s",
                record.record_id[:12], e,
            )
            return False

    def from_outcome_ledger(
        self,
        ledger_path: pathlib.Path | str | None = None,
    ) -> int:
        """One-shot idempotent backfill of Q3 outcome_ledger.jsonl entries.

        Each ledger entry becomes an ``EvidenceRecord(kind="outcome")``.
        Idempotent because record_id is content-addressed: re-running over
        the same ledger produces zero NEW records (dedupe hits only).

        Returns: count of NEW records written this call.
        """
        # Lazy-import the governor so tests / consumers w/o trialbench env
        # can still use the ledger.
        try:
            from memory import outcome_governor as og  # type: ignore
        except ImportError as e:
            logger.warning("from_outcome_ledger: outcome_governor unavailable: %s", e)
            return 0

        path = pathlib.Path(ledger_path) if ledger_path else og.LEDGER_PATH
        if not path.is_file():
            logger.info("from_outcome_ledger: no ledger at %s — nothing to backfill", path)
            return 0

        new_count = 0
        try:
            with path.open("r", encoding="utf-8") as f:
                for lineno, raw in enumerate(f, start=1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            "from_outcome_ledger line %d malformed: %s", lineno, e
                        )
                        continue
                    rec_dict = obj.get("outcome_record") or {}
                    if not rec_dict:
                        continue
                    # Build content; record_id will dedupe via sha256 of content.
                    content = {
                        "task_id": rec_dict.get("task_id"),
                        "arm": rec_dict.get("arm"),
                        "node": rec_dict.get("node"),
                        "outcome": rec_dict.get("outcome"),
                        "score": rec_dict.get("score"),
                        "evidence_refs": list(rec_dict.get("evidence_refs") or []),
                        "timestamp": rec_dict.get("timestamp"),
                        "trial_id": rec_dict.get("trial_id"),
                        "score_method": rec_dict.get("score_method"),
                        "source": "outcome_ledger.jsonl",
                        "sequence_id": obj.get("sequence_id"),
                        "write_timestamp": obj.get("write_timestamp"),
                    }
                    before = COUNTERS["evidence_records_total"]
                    self.record(content=content, kind=EvidenceKind.OUTCOME)
                    if COUNTERS["evidence_records_total"] > before:
                        new_count += 1
        except OSError as e:
            logger.error("from_outcome_ledger read failure %s: %s", path, e)
            return new_count

        _bump("evidence_outcome_backfill_total", new_count)
        logger.info(
            "from_outcome_ledger: backfilled %d new records (idempotent dedupe applied)",
            new_count,
        )
        return new_count

    # -- redaction (W1.b) ----------------------------------------------------

    def redact_record(
        self,
        record_id: str,
        reason: str,
        actor: str,
        redacted_marker: str = "[REDACTED]",
    ) -> dict[str, Any]:
        """Post-hoc surgical redaction of a record's payload (W1.b).

        Behaviour
        ---------
        1. Look up ``record_id``. If absent, ``EvidenceLedgerError`` raised
           and ``ledger_redact_target_missing_total`` is bumped.
        2. Write a NEW evidence record (the *tombstone*) with
           ``kind="redaction"``, content::

               {
                 "subject":       <target record_id>,
                 "actor":         <actor>,
                 "reason":        <reason>,
                 "redacted_at":   <ISO-8601 UTC>,
                 "marker":        <redacted_marker>,
               }

           and ``parent_ids=[<target record_id>]`` so the DAG ties tombstone
           back to its target.
        3. Mutate the target's ``content_json`` column to the literal
           ``redacted_marker`` STRING (not JSON-wrapped — readers MUST tolerate
           a non-JSON content_json after redaction, which the ``get()`` /
           ``query()`` paths now do via the ``content`` field rendering as
           ``{"__redacted__": "<marker>"}`` for backward-compatible dict shape).
        4. Set ``redacted_by_record_id`` to the tombstone's record_id and
           ``redacted_at`` to the same ISO timestamp on the target row.
        5. The target's ``record_id`` (= sha256 of original canonical content)
           is **never** mutated. Chain integrity = the target's record_id is
           still the cryptographic attestation of the original payload, which
           Aaron / a backup may still hold offline.

        Idempotence policy
        ------------------
        Calling ``redact_record`` on a *target that is already redacted* is
        treated as a **no-op** that returns the existing tombstone metadata
        (``already_redacted=True``). This avoids creating infinite tombstone
        chains when the IDE re-clicks the Redact button. Counter
        ``ledger_redact_ok_total`` does **not** double-count.

        Returns
        -------
        dict with keys::

            tombstone_record_id : str    # sha256 of the new tombstone record
            redacted_target     : str    # the input record_id
            redacted_at         : str    # ISO-8601 UTC
            already_redacted    : bool   # True iff this call was a no-op

        Raises
        ------
        EvidenceLedgerError
            target ``record_id`` not in ledger; or ``redacted_marker`` empty.
        ValueError
            ``reason`` / ``actor`` empty after strip.
        sqlite3.Error
            re-raised after counter+log bump (ZSF).
        """
        # Input validation — fail fast before any DB I/O.
        if not isinstance(record_id, str) or not record_id.strip():
            _bump("ledger_redact_errors_total")
            raise ValueError("redact_record: record_id required (non-empty string)")
        if not isinstance(reason, str) or not reason.strip():
            _bump("ledger_redact_errors_total")
            raise ValueError("redact_record: reason required (non-empty string)")
        if not isinstance(actor, str) or not actor.strip():
            _bump("ledger_redact_errors_total")
            raise ValueError("redact_record: actor required (non-empty string)")
        if not isinstance(redacted_marker, str) or not redacted_marker:
            _bump("ledger_redact_errors_total")
            raise EvidenceLedgerError(
                "redact_record: redacted_marker must be a non-empty string"
            )

        target_id = record_id.strip()
        reason_norm = reason.strip()
        actor_norm = actor.strip()

        # Phase 1 — target existence + idempotence pre-check (read-only).
        with self._write_lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT record_id, content_json, kind, created_at, "
                    "schema_version, git_rev, redacted_by_record_id, redacted_at "
                    "FROM evidence_records WHERE record_id = ?",
                    (target_id,),
                ).fetchone()
                if row is None:
                    _bump("ledger_redact_target_missing_total")
                    _bump("ledger_redact_errors_total")
                    raise EvidenceLedgerError(
                        f"redact_record: target record_id {target_id!r} not in ledger"
                    )

                already_tombstone = row["redacted_by_record_id"]
                already_at = row["redacted_at"]
                if already_tombstone:
                    # Idempotent path — never create a 2nd tombstone for the
                    # same target. Look up the existing tombstone metadata
                    # and return it. We do NOT bump ledger_redact_ok_total
                    # so the counter measures unique tombstones, not clicks.
                    logger.info(
                        "redact_record: target %s already redacted by %s "
                        "(idempotent no-op)",
                        target_id[:12], str(already_tombstone)[:12],
                    )
                    return {
                        "tombstone_record_id": str(already_tombstone),
                        "redacted_target": target_id,
                        "redacted_at": str(already_at) if already_at else "",
                        "already_redacted": True,
                    }
            except sqlite3.Error as e:
                conn.rollback()
                _bump("ledger_redact_errors_total")
                logger.error(
                    "redact_record lookup failure id=%s err=%s",
                    target_id[:12], e,
                )
                raise
            finally:
                conn.close()

        # Phase 2 — write the tombstone via canonical record() API.
        # This grabs its own connection / lock, which is fine: tombstone
        # creation is independent of the target mutation.
        redacted_at_iso = _now_iso()
        tombstone_content = {
            "subject": target_id,
            "actor": actor_norm,
            "reason": reason_norm,
            "redacted_at": redacted_at_iso,
            "marker": redacted_marker,
        }
        try:
            tombstone = self.record(
                content=tombstone_content,
                kind=EvidenceKind.REDACTION,
                parent_ids=[target_id],
            )
        except (sqlite3.Error, ValueError, TypeError) as e:
            _bump("ledger_redact_errors_total")
            logger.error(
                "redact_record tombstone write failure target=%s err=%s",
                target_id[:12], e,
            )
            raise

        # Phase 3 — mutate the target row. content_json becomes the literal
        # marker string (NOT JSON-wrapped); _row_to_record handles the
        # post-redact shape transparently.
        with self._write_lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE evidence_records "
                    "SET content_json = ?, "
                    "    redacted_by_record_id = ?, "
                    "    redacted_at = ? "
                    "WHERE record_id = ?",
                    (
                        redacted_marker,
                        tombstone.record_id,
                        redacted_at_iso,
                        target_id,
                    ),
                )
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                _bump("ledger_redact_errors_total")
                logger.error(
                    "redact_record target mutation failure id=%s err=%s",
                    target_id[:12], e,
                )
                raise
            finally:
                conn.close()

        _bump("ledger_redact_ok_total")
        logger.info(
            "redact_record OK target=%s tombstone=%s actor=%s reason=%s",
            target_id[:12], tombstone.record_id[:12], actor_norm, reason_norm[:60],
        )
        return {
            "tombstone_record_id": tombstone.record_id,
            "redacted_target": target_id,
            "redacted_at": redacted_at_iso,
            "already_redacted": False,
        }

    # -- chain verification --------------------------------------------------

    def verify_chain(self) -> dict[str, Any]:
        """Re-derive sha256 of every non-redacted record's content_json and
        compare it against the stored ``record_id``.

        Why this is meaningful here
        ---------------------------
        Every record's ``record_id`` IS the sha256 of its canonical
        ``{kind, schema_version, parent_ids, content}`` payload. So this
        function recomputes the canonical hash and asserts ``record_id ==
        sha256(canonical)``. A mismatch means either:
          * disk corruption,
          * a non-redact write path mutated content_json (would be a bug),
          * the redact path mishandled a row (the ZSF guard against #2).

        Redacted records (``content_json`` == marker string, ``redacted_at``
        IS NOT NULL) are skipped from sha256 recomputation but still verified
        structurally: their ``redacted_by_record_id`` MUST resolve to a real
        ``kind="redaction"`` row whose content.subject equals the target.

        Returns
        -------
        dict::

            {
              "ok":                bool,
              "checked":           int,   # rows examined
              "recomputed":        int,   # non-redacted rows hash-verified
              "redacted":          int,   # rows skipped from re-hash
              "mismatches":        list[str],  # record_ids that failed
              "missing_tombstone": list[str],  # redacted rows w/ broken DAG
            }
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT record_id, content_json, kind, schema_version, "
                "redacted_by_record_id, redacted_at "
                "FROM evidence_records"
            ).fetchall()
            mismatches: list[str] = []
            missing_tombstone: list[str] = []
            checked = 0
            recomputed = 0
            redacted = 0
            for r in rows:
                checked += 1
                rec_id = r["record_id"]
                if r["redacted_at"]:
                    redacted += 1
                    tomb_id = r["redacted_by_record_id"]
                    if not tomb_id:
                        missing_tombstone.append(rec_id)
                        continue
                    tomb_row = conn.execute(
                        "SELECT record_id, content_json, kind "
                        "FROM evidence_records WHERE record_id = ?",
                        (tomb_id,),
                    ).fetchone()
                    if (
                        tomb_row is None
                        or tomb_row["kind"] != EvidenceKind.REDACTION.value
                    ):
                        missing_tombstone.append(rec_id)
                        continue
                    # Tombstone content must reference the target via subject.
                    try:
                        tomb_content = json.loads(tomb_row["content_json"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        missing_tombstone.append(rec_id)
                        continue
                    if tomb_content.get("subject") != rec_id:
                        missing_tombstone.append(rec_id)
                    continue
                # Non-redacted: re-derive sha256 of canonical content.
                try:
                    content = json.loads(r["content_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    mismatches.append(rec_id)
                    continue
                # Reconstruct parent_ids in canonical (sorted) order.
                parent_rows = conn.execute(
                    "SELECT parent_id FROM evidence_parents WHERE child_id = ?",
                    (rec_id,),
                ).fetchall()
                parents_tuple = tuple(sorted(p["parent_id"] for p in parent_rows))
                canonical = _canonical_content(
                    content=content,
                    kind=r["kind"],
                    parent_ids=parents_tuple,
                    schema_version=r["schema_version"],
                )
                derived = _hash_content(canonical)
                if derived != rec_id:
                    mismatches.append(rec_id)
                else:
                    recomputed += 1
            ok = not mismatches and not missing_tombstone
            if ok:
                _bump("evidence_chain_verify_ok_total")
            else:
                _bump("evidence_chain_verify_mismatch_total")
                logger.error(
                    "verify_chain: mismatches=%d missing_tombstone=%d",
                    len(mismatches), len(missing_tombstone),
                )
            return {
                "ok": ok,
                "checked": checked,
                "recomputed": recomputed,
                "redacted": redacted,
                "mismatches": mismatches,
                "missing_tombstone": missing_tombstone,
            }
        except sqlite3.Error as e:
            _bump("evidence_chain_verify_mismatch_total")
            logger.error("verify_chain failure: %s", e)
            raise
        finally:
            conn.close()

    # -- internal helpers ----------------------------------------------------

    @staticmethod
    def _row_redaction_metadata(row: sqlite3.Row) -> dict[str, Any] | None:
        """Return {redacted_at, redacted_by_record_id} when row is redacted, else None.

        Robust against rows from older (pre-migration) connections where the
        redaction columns may not be present.
        """
        try:
            keys = set(row.keys())
        except Exception:  # row missing keys() (some sqlite3 row types)
            return None
        if "redacted_at" not in keys:
            return None
        if not row["redacted_at"]:
            return None
        return {
            "redacted_at": row["redacted_at"],
            "redacted_by_record_id": row["redacted_by_record_id"]
            if "redacted_by_record_id" in keys
            else None,
        }

    def _row_to_record(
        self,
        row: sqlite3.Row,
        conn: sqlite3.Connection,
    ) -> EvidenceRecord:
        raw_content = row["content_json"]
        # W1.b — post-hoc redacted rows store the literal marker string in
        # content_json (NOT JSON). Render them as a stable dict so callers
        # that expect ``record.content`` to be a dict don't break, and
        # downstream consumers can detect redaction via the ``__redacted__``
        # sentinel key.
        try:
            content = json.loads(raw_content) if raw_content else {}
        except (json.JSONDecodeError, TypeError):
            redacted_columns = self._row_redaction_metadata(row)
            if redacted_columns is not None:
                content = {
                    "__redacted__": raw_content,
                    "redacted_at": redacted_columns.get("redacted_at"),
                    "redacted_by_record_id": redacted_columns.get(
                        "redacted_by_record_id"
                    ),
                }
            else:
                _bump("evidence_query_errors_total")
                logger.error(
                    "evidence row decode failure record_id=%s",
                    row["record_id"][:12],
                )
                raise
        parent_rows = conn.execute(
            "SELECT parent_id FROM evidence_parents WHERE child_id = ?",
            (row["record_id"],),
        ).fetchall()
        parents = frozenset(pr["parent_id"] for pr in parent_rows)
        meta = self._row_redaction_metadata(row)
        return EvidenceRecord(
            record_id=row["record_id"],
            content=content,
            kind=row["kind"],
            parent_ids=parents,
            created_at=row["created_at"],
            schema_version=row["schema_version"],
            git_rev=row["git_rev"],
            redacted_by_record_id=(meta or {}).get("redacted_by_record_id"),
            redacted_at=(meta or {}).get("redacted_at"),
        )

    # -- diagnostics ---------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0]
            by_kind_rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM evidence_records GROUP BY kind"
            ).fetchall()
            by_kind = {r["kind"]: r["n"] for r in by_kind_rows}
            edge_count = conn.execute("SELECT COUNT(*) FROM evidence_parents").fetchone()[0]
        finally:
            conn.close()
        return {
            "db_path": self._db_path,
            "schema_version": SCHEMA_VERSION,
            "total_records": total,
            "by_kind": by_kind,
            "dag_edges": edge_count,
            "counters": dict(COUNTERS),
        }


# ---------------------------------------------------------------------------
# Mission Memento writer (OpenPath Phase-13 GG5 — additive only)
# ---------------------------------------------------------------------------

# Required keys in the Mission Memento content payload. Validated at write
# time so partially-formed mementos cannot enter the ledger silently. The
# schema mirrors the OpenPath spec:
#
#   - mission_id   : str  — stable identifier from the Mission Envelope
#   - goal         : str  — one-sentence user_goal copied from the envelope
#   - decision     : str  — router decision name ("execute_and_log",
#                            "chief_review", "chief_plus_surgeons",
#                            "execute_with_post_verification", "ask_user",
#                            "propose_envelope", "deny", "execute", ...)
#   - actions_taken: list — list of action names actually dispatched
#   - evidence_ids : list — EvidenceRecord.record_id values for any
#                            sub-records produced during dispatch
#   - final_state  : str  — terminal state ("succeeded", "rejected",
#                            "in_review", "expired", "rolled_back")
#   - unresolved   : list — open follow-ups (case_ids, missing approvals)
MISSION_MEMENTO_REQUIRED_KEYS: tuple[str, ...] = (
    "mission_id",
    "goal",
    "decision",
    "actions_taken",
    "evidence_ids",
    "final_state",
    "unresolved",
)


def record_mission_memento(
    ledger: "EvidenceLedger",
    *,
    mission_id: str,
    goal: str,
    decision: str,
    actions_taken: list[str] | tuple[str, ...] | None = None,
    evidence_ids: list[str] | tuple[str, ...] | None = None,
    final_state: str,
    unresolved: list[str] | tuple[str, ...] | None = None,
    parent_ids: Iterable[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> EvidenceRecord:
    """Record a Mission Memento (kind=mission_memento).

    Additive writer for the OpenPath Phase-13 model. Wraps the existing
    ``EvidenceLedger.record`` path — it does not modify any other writer or
    record kind. The full schema is documented in
    ``docs/MISSION_ENVELOPE_SPEC.md`` and ``docs/OPEN_PATH_AUTONOMY.md``.

    Args:
        ledger: An ``EvidenceLedger`` instance.
        mission_id: Stable Mission Envelope id (UUID4 hex from GG1 builder).
        goal: One-sentence ``user_goal`` from the envelope.
        decision: Router decision name. Free-form string but should match the
            ``route_action`` decision vocabulary.
        actions_taken: List of action names actually dispatched. Empty list
            allowed (e.g. ``decision="propose_envelope"``).
        evidence_ids: EvidenceRecord ids for sub-records produced during
            dispatch (chief decision, surgeon verdicts, etc).
        final_state: Terminal state literal — see the module-level docstring
            for the canonical vocabulary.
        unresolved: Open follow-ups (Arbiter case_ids, missing approvals).
        parent_ids: Optional parent record ids — preserved by the underlying
            ``record()`` for chain integrity. Passing a parent maintains the
            DAG link so ``ledger.lineage(memento.record_id)`` walks back.
        extra: Optional additional content fields. Merged into the canonical
            payload; collisions with required keys raise ValueError.

    Returns:
        The created (or deduped) EvidenceRecord.

    Raises:
        ValueError: required key missing/empty, or ``extra`` collides with
            a required key. Counter ``mission_memento_invalid_total`` bumps.
        TypeError: ``ledger`` is not an EvidenceLedger.
    """
    if not isinstance(ledger, EvidenceLedger):
        raise TypeError(
            f"ledger must be EvidenceLedger, got {type(ledger).__name__}"
        )

    # Validate list-typed fields BEFORE normalization — the canonical-JSON
    # hasher will accept any iterable, but the spec requires ``list`` (or
    # tuple, which we coerce). A bare string is iterable of chars and would
    # silently coerce; reject it explicitly. ZSF: surface bad input, never
    # normalize it away.
    list_fields = {
        "actions_taken": actions_taken,
        "evidence_ids": evidence_ids,
        "unresolved": unresolved,
    }
    for key, raw in list_fields.items():
        if raw is None:
            continue
        if not isinstance(raw, (list, tuple)):
            _bump("mission_memento_invalid_total")
            raise ValueError(
                f"mission_memento field {key!r} must be list or tuple, "
                f"got {type(raw).__name__}"
            )

    # Validate string-typed required fields.
    string_fields = {
        "mission_id": mission_id,
        "goal": goal,
        "decision": decision,
        "final_state": final_state,
    }
    for key, raw in string_fields.items():
        if not isinstance(raw, str) or not raw.strip():
            _bump("mission_memento_invalid_total")
            raise ValueError(
                f"mission_memento field {key!r} must be non-empty str"
            )

    # Build the canonical content payload with stable key order. Lists are
    # normalized to plain ``list`` for canonical-JSON hashing.
    payload: dict[str, Any] = {
        "mission_id": mission_id,
        "goal": goal,
        "decision": decision,
        "actions_taken": list(actions_taken or []),
        "evidence_ids": list(evidence_ids or []),
        "final_state": final_state,
        "unresolved": list(unresolved or []),
    }

    # Merge extras AFTER validation so callers cannot stomp required fields.
    if extra:
        collisions = set(extra.keys()) & set(MISSION_MEMENTO_REQUIRED_KEYS)
        if collisions:
            _bump("mission_memento_invalid_total")
            raise ValueError(
                f"mission_memento extra collides with required keys: "
                f"{sorted(collisions)}"
            )
        payload.update(extra)

    # Snapshot the dedupe counter BEFORE writing so we can tell whether the
    # underlying record() call bumped dedupe (idempotent re-write) or wrote
    # a new row. This keeps mission_memento_records_total a true new-write
    # count (matching evidence_records_total semantics for this kind).
    dedupe_before = COUNTERS.get("evidence_dedupe_hits_total", 0)
    record = ledger.record(
        content=payload,
        kind=EvidenceKind.MISSION_MEMENTO,
        parent_ids=parent_ids,
    )
    dedupe_after = COUNTERS.get("evidence_dedupe_hits_total", 0)
    if dedupe_after == dedupe_before:
        # Not a dedupe — first time we've seen this canonical content.
        _bump("mission_memento_records_total")
    return record


# ---------------------------------------------------------------------------
# Module __all__
# ---------------------------------------------------------------------------

__all__ = [
    "EvidenceRecord",
    "EvidenceLedger",
    "EvidenceLedgerError",
    "EvidenceKind",
    "DB_PATH",
    "SCHEMA_VERSION",
    "COUNTERS",
    "current_git_rev",
    "MISSION_MEMENTO_REQUIRED_KEYS",
    "record_mission_memento",
]
