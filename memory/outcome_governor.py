"""
Outcome Governor — TrialBench evidence -> Outcome ledger handoff (v0 stub).

Closes audit gap #1 (P2 roadmap audit, 2026-05-06):
TrialBench produces evidence; nothing in the superrepo consumes it. This module
ingests TrialBench outcomes and writes them to an append-only JSONL ledger that
downstream code (Permission Governor, Memory Promotion, SOP update) can read.

Proof loop (from TS scaffold docs/02-outcome-measurement.md):
    Context -> Behavior -> Outcome -> Score -> Permission -> Memory/SOP update

This module covers the Outcome -> Score -> Ledger leg. Score-derivation here is
heuristic (tests_pass, no_invariant_violation, primary_endpoint_success). Real
human-blinded scoring is v2+; the heuristic preserves that caveat in record
metadata so future code can re-score without losing provenance.

ZSF (Zero Silent Failures):
  - every parse failure logs + skips (does not crash)
  - module-level counter dict ``INGEST_ERRORS`` tracks error categories so a
    daemon can scrape it (no Prometheus dep here — stdlib only)
  - JSONL writes are protected by ``fcntl.flock`` so concurrent ingests don't
    interleave lines

Strict allowlist: only reads from ``artifacts/trialbench/<trial_id>/``.

stdlib only (no scipy / pandas / requests).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import fcntl
import json
import logging
import os
import pathlib
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_THIS_FILE = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parent.parent  # memory/.. -> superrepo

LEDGER_PATH: pathlib.Path = _REPO_ROOT / "memory" / "outcome_ledger.jsonl"
TRIALBENCH_ROOT: pathlib.Path = _REPO_ROOT / "artifacts" / "trialbench"

# Canonical arm IDs (mirrors tools/trialbench_score.py KNOWN_ARMS / ARM_ALIASES).
KNOWN_ARMS = ("A_raw", "B_generic_context", "C_contextdna_packet")
ARM_ALIASES = {
    "C_governed": "C_contextdna_packet",  # N2 dispatcher emits C_governed
}

# Outcome categories
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_TIMEOUT = "timeout"

# ZSF: module-level error counters (daemon-scrapable; not Prometheus).
INGEST_ERRORS: dict[str, int] = {
    "outcome_ledger_ingest_errors": 0,
    "missing_artifact_dir": 0,
    "scored_parse_failure": 0,
    "run_parse_failure": 0,
    "ledger_write_failure": 0,
    "skipped_unknown_arm": 0,
}

logger = logging.getLogger("memory.outcome_governor")
if not logger.handlers:  # avoid duplicate handlers on re-import
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s outcome_governor %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class OutcomeRecord:
    """Single trialbench outcome, normalized for ledger storage."""

    task_id: str
    arm: str
    node: str
    outcome: str  # success / failure / timeout
    score: float  # 0.0 - 1.0 (heuristic v0)
    evidence_refs: list[str] = field(default_factory=list)
    timestamp: str = ""  # ISO8601, ideally the trial run's finished_at
    trial_id: str = ""
    score_method: str = "heuristic_v0"  # caveat: real blinded scoring is v2+

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LedgerEntry:
    """Wraps an OutcomeRecord with sequence_id + write_timestamp."""

    sequence_id: int
    write_timestamp: str
    outcome_record: OutcomeRecord

    def to_json(self) -> dict[str, Any]:
        return {
            "sequence_id": self.sequence_id,
            "write_timestamp": self.write_timestamp,
            "outcome_record": self.outcome_record.to_json(),
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "LedgerEntry":
        rec = obj.get("outcome_record") or {}
        return cls(
            sequence_id=int(obj.get("sequence_id", 0)),
            write_timestamp=str(obj.get("write_timestamp", "")),
            outcome_record=OutcomeRecord(
                task_id=str(rec.get("task_id", "")),
                arm=str(rec.get("arm", "")),
                node=str(rec.get("node", "")),
                outcome=str(rec.get("outcome", "")),
                score=float(rec.get("score", 0.0)),
                evidence_refs=list(rec.get("evidence_refs") or []),
                timestamp=str(rec.get("timestamp", "")),
                trial_id=str(rec.get("trial_id", "")),
                score_method=str(rec.get("score_method", "heuristic_v0")),
            ),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _bump(counter: str) -> None:
    INGEST_ERRORS[counter] = INGEST_ERRORS.get(counter, 0) + 1
    INGEST_ERRORS["outcome_ledger_ingest_errors"] = (
        INGEST_ERRORS.get("outcome_ledger_ingest_errors", 0) + 1
    )


def _normalize_arm(arm: str) -> str:
    return ARM_ALIASES.get(arm, arm)


def _safe_trial_dir(trial_id: str) -> pathlib.Path | None:
    """Strict allowlist: only ``artifacts/trialbench/<trial_id>/``.

    Rejects path traversal (``..``, absolute, anything containing ``/``).
    """
    if not trial_id or "/" in trial_id or ".." in trial_id or trial_id.startswith("."):
        logger.error("rejected trial_id=%r (path traversal guard)", trial_id)
        _bump("missing_artifact_dir")
        return None
    p = TRIALBENCH_ROOT / trial_id
    if not p.is_dir():
        logger.error("trial dir missing: %s", p)
        _bump("missing_artifact_dir")
        return None
    return p


def _derive_outcome_and_score(run: dict[str, Any]) -> tuple[str, float]:
    """Heuristic outcome + score from a scored or raw run dict.

    Score components (each in [0,1], averaged):
      - tests_pass
      - no_invariant_violation
      - primary_endpoint_success
      - task_complete
      - blinded_reviewer_approved (synthetic ok for v0)

    If timed_out -> OUTCOME_TIMEOUT, score 0.0.
    If exit_code present and != 0 -> OUTCOME_FAILURE.
    Else outcome by majority of components.
    """

    if bool(run.get("timed_out", False)):
        return OUTCOME_TIMEOUT, 0.0

    exit_code = run.get("exit_code")
    http_status = run.get("http_status")
    if exit_code is not None:
        try:
            if int(exit_code) != 0:
                return OUTCOME_FAILURE, 0.0
        except (TypeError, ValueError):
            pass
    if http_status is not None:
        try:
            hs = int(http_status)
            if hs >= 500:
                return OUTCOME_FAILURE, 0.0
        except (TypeError, ValueError):
            pass

    # Score components — only count those that are explicitly present as bools.
    components: list[float] = []
    for key in (
        "tests_pass",
        "no_invariant_violation",
        "primary_endpoint_success",
        "task_complete",
        "blinded_reviewer_approved",
    ):
        if key in run and isinstance(run[key], bool):
            components.append(1.0 if run[key] else 0.0)

    if not components:
        # Raw run with no scorer fields — treat exit_code 0 as success @ 0.5
        # (low confidence: heuristic only, no eval signal).
        return OUTCOME_SUCCESS if exit_code == 0 else OUTCOME_FAILURE, 0.5

    score = sum(components) / len(components)
    outcome = OUTCOME_SUCCESS if score >= 0.5 else OUTCOME_FAILURE
    return outcome, round(score, 4)


def _record_from_scored(
    scored: dict[str, Any], trial_id: str, evidence_path: pathlib.Path
) -> OutcomeRecord | None:
    """Build OutcomeRecord from a scored_outcomes.json entry."""
    arm_raw = str(scored.get("arm") or "")
    arm = _normalize_arm(arm_raw)
    if arm not in KNOWN_ARMS:
        logger.warning("skip unknown arm=%r in trial=%s", arm_raw, trial_id)
        _bump("skipped_unknown_arm")
        return None

    task_id = str(scored.get("task_id") or "")
    node = str(scored.get("node_id") or scored.get("node") or "")
    if not task_id or not node:
        logger.warning("skip scored entry missing task_id/node: %r", scored)
        _bump("scored_parse_failure")
        return None

    outcome, score = _derive_outcome_and_score(scored)
    return OutcomeRecord(
        task_id=task_id,
        arm=arm,
        node=node,
        outcome=outcome,
        score=score,
        evidence_refs=[str(evidence_path), str(scored.get("source_file") or "")],
        timestamp=_now_iso(),  # scored_outcomes has no per-run timestamp
        trial_id=trial_id,
        score_method="heuristic_v0_scored",
    )


def _record_from_run(
    run: dict[str, Any], trial_id: str, evidence_path: pathlib.Path
) -> OutcomeRecord | None:
    """Build OutcomeRecord from a raw run_NNN.json (fallback path)."""
    arm_raw = str(run.get("arm") or "")
    arm = _normalize_arm(arm_raw)
    if arm not in KNOWN_ARMS:
        logger.warning("skip unknown arm=%r in run=%s", arm_raw, evidence_path.name)
        _bump("skipped_unknown_arm")
        return None

    task_id = str(run.get("task_id") or "")
    node = str(run.get("node") or run.get("node_id") or "")
    if not task_id or not node:
        logger.warning("skip raw run missing task_id/node: %s", evidence_path)
        _bump("run_parse_failure")
        return None

    outcome, score = _derive_outcome_and_score(run)
    finished_at = str(run.get("finished_at") or run.get("started_at") or _now_iso())
    return OutcomeRecord(
        task_id=task_id,
        arm=arm,
        node=node,
        outcome=outcome,
        score=score,
        evidence_refs=[str(evidence_path)],
        timestamp=finished_at,
        trial_id=trial_id,
        score_method="heuristic_v0_raw",
    )


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

def _iter_ledger_lines() -> Iterator[dict[str, Any]]:
    """Yield parsed ledger entries (dicts). Skips malformed lines (logs + counter)."""
    if not LEDGER_PATH.exists():
        return
    try:
        with LEDGER_PATH.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    logger.error("ledger line %d malformed: %s", lineno, e)
                    _bump("ledger_write_failure")
    except OSError as e:
        logger.error("ledger read failure: %s", e)
        _bump("ledger_write_failure")


def _existing_triples() -> tuple[set[tuple[str, str, str]], int]:
    """Return (set of (task_id, arm, trial_id), max_sequence_id) for idempotency."""
    triples: set[tuple[str, str, str]] = set()
    max_seq = 0
    for obj in _iter_ledger_lines():
        try:
            rec = obj.get("outcome_record") or {}
            triple = (
                str(rec.get("task_id", "")),
                str(rec.get("arm", "")),
                str(rec.get("trial_id", "")),
            )
            if triple != ("", "", ""):
                triples.add(triple)
            seq = int(obj.get("sequence_id", 0))
            if seq > max_seq:
                max_seq = seq
        except (TypeError, ValueError):
            continue
    return triples, max_seq


def _append_locked(entries: Iterable[LedgerEntry]) -> int:
    """Append entries to LEDGER_PATH under fcntl.flock. Returns count written."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        # Open in append mode; lock for the duration of the write.
        with LEDGER_PATH.open("a", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError as e:
                logger.error("flock failure (continuing unlocked): %s", e)
                _bump("ledger_write_failure")
            try:
                for entry in entries:
                    f.write(json.dumps(entry.to_json(), separators=(",", ":")) + "\n")
                    count += 1
                f.flush()
                os.fsync(f.fileno())
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except OSError as e:
        logger.error("ledger append failure: %s", e)
        _bump("ledger_write_failure")
    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest_trial(trial_id: str) -> int:
    """Ingest a trialbench trial's outcomes into the ledger.

    Reads ``artifacts/trialbench/<trial_id>/scored_outcomes.json`` first.
    Falls back to per-run ``run_NNN.json`` if scored_outcomes is missing.
    Skips runs already in ledger (idempotent on (task_id, arm, trial_id)).
    Returns count of NEW records appended.
    """
    trial_dir = _safe_trial_dir(trial_id)
    if trial_dir is None:
        return 0

    new_records: list[OutcomeRecord] = []
    seen, max_seq = _existing_triples()

    scored_path = trial_dir / "scored_outcomes.json"
    used_scored = False
    if scored_path.is_file():
        try:
            with scored_path.open("r", encoding="utf-8") as f:
                scored_list = json.load(f)
            if not isinstance(scored_list, list):
                raise ValueError(f"scored_outcomes.json is not a list: {type(scored_list)}")
            used_scored = True
            for entry in scored_list:
                if not isinstance(entry, dict):
                    logger.warning("non-dict scored entry skipped: %r", entry)
                    _bump("scored_parse_failure")
                    continue
                rec = _record_from_scored(entry, trial_id, scored_path)
                if rec is None:
                    continue
                triple = (rec.task_id, rec.arm, rec.trial_id)
                if triple in seen:
                    continue
                seen.add(triple)
                new_records.append(rec)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            logger.error("scored_outcomes.json parse failure: %s", e)
            _bump("scored_parse_failure")
            used_scored = False

    if not used_scored:
        # Fallback: walk run_*.json files
        for run_path in sorted(trial_dir.glob("run_*.json")):
            try:
                with run_path.open("r", encoding="utf-8") as f:
                    run = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.error("run parse failure %s: %s", run_path, e)
                _bump("run_parse_failure")
                continue
            if not isinstance(run, dict):
                _bump("run_parse_failure")
                continue
            rec = _record_from_run(run, trial_id, run_path)
            if rec is None:
                continue
            triple = (rec.task_id, rec.arm, rec.trial_id)
            if triple in seen:
                continue
            seen.add(triple)
            new_records.append(rec)

    if not new_records:
        return 0

    entries: list[LedgerEntry] = []
    write_ts = _now_iso()
    for i, rec in enumerate(new_records, start=1):
        entries.append(
            LedgerEntry(
                sequence_id=max_seq + i,
                write_timestamp=write_ts,
                outcome_record=rec,
            )
        )
    written = _append_locked(entries)
    return written


def list_outcomes(limit: int = 50, arm: str | None = None) -> list[LedgerEntry]:
    """Return up to ``limit`` most-recent ledger entries, optionally filtered by arm."""
    if limit <= 0:
        return []
    arm_norm = _normalize_arm(arm) if arm else None
    entries: list[LedgerEntry] = []
    for obj in _iter_ledger_lines():
        try:
            entry = LedgerEntry.from_json(obj)
        except (TypeError, ValueError) as e:
            logger.warning("ledger entry decode failure: %s", e)
            _bump("ledger_write_failure")
            continue
        if arm_norm and entry.outcome_record.arm != arm_norm:
            continue
        entries.append(entry)
    # Most-recent first by sequence_id (stable since sequence_id is monotonic).
    entries.sort(key=lambda e: e.sequence_id, reverse=True)
    return entries[:limit]


def summarize_recent(window_hours: int = 24) -> dict[str, Any]:
    """Aggregate per-arm success rate + count over the last ``window_hours``.

    Uses ledger ``write_timestamp`` (not record.timestamp) — write_timestamp
    is the moment the governor learned of the outcome, which is the relevant
    time for permission/memory-update decisions.
    """
    if window_hours <= 0:
        return {"window_hours": window_hours, "arms": {}, "total": 0}

    cutoff = _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(hours=window_hours)
    per_arm: dict[str, dict[str, Any]] = {a: {"count": 0, "success": 0, "scores": []} for a in KNOWN_ARMS}
    total = 0

    for obj in _iter_ledger_lines():
        try:
            entry = LedgerEntry.from_json(obj)
        except (TypeError, ValueError):
            continue
        try:
            ts = _dt.datetime.fromisoformat(entry.write_timestamp)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        rec = entry.outcome_record
        bucket = per_arm.setdefault(
            rec.arm, {"count": 0, "success": 0, "scores": []}
        )
        bucket["count"] += 1
        bucket["scores"].append(rec.score)
        if rec.outcome == OUTCOME_SUCCESS:
            bucket["success"] += 1
        total += 1

    arms_out: dict[str, dict[str, Any]] = {}
    for arm, b in per_arm.items():
        c = b["count"]
        scores = b["scores"]
        arms_out[arm] = {
            "count": c,
            "success": b["success"],
            "success_rate": (b["success"] / c) if c else 0.0,
            "avg_score": (sum(scores) / len(scores)) if scores else 0.0,
        }

    return {
        "window_hours": window_hours,
        "total": total,
        "arms": arms_out,
        "ledger_path": str(LEDGER_PATH),
        "ingest_errors": dict(INGEST_ERRORS),
        "score_method": "heuristic_v0 (real blinded scoring is v2+)",
    }


__all__ = [
    "OutcomeRecord",
    "LedgerEntry",
    "ingest_trial",
    "list_outcomes",
    "summarize_recent",
    "LEDGER_PATH",
    "INGEST_ERRORS",
    "KNOWN_ARMS",
    "ARM_ALIASES",
    "OUTCOME_SUCCESS",
    "OUTCOME_FAILURE",
    "OUTCOME_TIMEOUT",
]
