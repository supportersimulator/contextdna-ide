"""Operator feedback handler — writes user-reported friction into the evidence ledger.

Why this exists
---------------
Synaptic diagnosis (2026-05-13): the mothership has no mechanism for operators
to report issues back into the evidence ledger. Without that loop the
subconscious cannot learn from real-world friction — patterns get promoted
and retired purely on internal signals (autopsies, gold passes), never on
operator pain. This module closes that gap.

Design contract
---------------
- ONE entry point: :func:`record_feedback`. Returns a :class:`FeedbackRecord`
  on success or raises :class:`FeedbackError` (after the fallback path has
  been attempted, so the report survives even on raise).
- The evidence ledger (``memory.evidence_ledger.EvidenceLedger``) is the
  canonical sink. It is SQLite-backed in this repo despite Synaptic's
  "Postgres" framing — the IDE-OSS docs call it ``evidence``, the
  implementation is SQLite (``memory/evidence_ledger.db``). We treat the
  ledger as an opaque dependency: import lazily, never assume reachability.
- Payload uses ``EvidenceKind.AUDIT`` (the real enum) with
  ``content.event_type = "feedback.operator_reported"``. ``audit`` is the
  closest existing kind for "operator says X happened" — feedback is a
  human-attested observation, distinct from ``trial`` (auto-collected) or
  ``outcome`` (autopsy-derived).

ZSF (zero silent failures)
--------------------------
- :data:`COUNTERS` tracks every code path:
  * ``feedback_records_total`` — successful ledger writes
  * ``feedback_records_total_by_kind`` — per-``FeedbackKind`` counter dict
  * ``feedback_records_total_by_severity`` — per-:class:`Severity` counter dict
  * ``feedback_record_errors_total`` — ledger write failures (then fallback ran)
  * ``feedback_fallback_writes_total`` — successful fallback appends
  * ``feedback_fallback_errors_total`` — fallback ALSO failed (true silent-loss
    risk — caller MUST surface this through /health or stderr)
- Fallback file: ``/tmp/feedback-fallback.jsonl`` (one record per line). A
  later reaper can replay these into the ledger when Postgres/SQLite is back.
- Failures NEVER swallow the exception silently. They bump a counter, log at
  WARNING, write to fallback, then re-raise as :class:`FeedbackError` with
  the original cause chained.

Auto-capture
------------
- ``mothership_version``: ``CONTEXTDNA_VERSION`` env or ``"unknown"``.
- ``profile``: ``CONTEXTDNA_PROFILE`` env (``heavy`` / ``lite``) or ``"unknown"``.
- ``active_panels``: ``CONTEXTDNA_ACTIVE_PANELS`` env, comma-split.
- ``node_id``: ``MULTIFLEET_NODE_ID`` / ``CONTEXTDNA_NODE_ID`` env, then
  ``socket.gethostname()``.
- ``recent_logs``: last 100 lines of any file in ``logs/`` whose mtime is
  within ``RECENT_LOG_WINDOW_S`` (default 1h) of the report time, when
  ``include_logs=True`` (default for ``bug`` / ``confusion`` /
  ``unmet_expectation``; opt-out for ``success_story`` / ``feature_request``
  / ``panel_request``).
- ``stack_trace``: only populated when the caller passes one explicitly. We
  do NOT walk the call stack on our own — operator feedback often arrives
  hours after the failure and a synthetic traceback would be misleading.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import json
import logging
import os
import pathlib
import socket
import sys
import threading
import traceback
from typing import Any

logger = logging.getLogger("contextdna.feedback.handler")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s contextdna.feedback %(message)s")
    )
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FeedbackKind(str, enum.Enum):
    """Taxonomy of operator feedback. See README for the rationale.

    Six kinds, chosen so the subconscious can route each one to a different
    learning signal:

    - ``bug`` — something broke. Routes to autopsy intake.
    - ``confusion`` — operator could not figure out how to do X. Routes to
      docs / S2 Professor Wisdom revision.
    - ``unmet_expectation`` — system worked as designed but the design is
      wrong. Routes to S5 Protocol / SOP revision.
    - ``success_story`` — operator wants to celebrate a win. Routes to
      pattern-promotion (this is positive reinforcement signal — without it
      the system only learns from failure).
    - ``feature_request`` — operator wants new capability. Routes to plan
      generation (``docs/plans/``).
    - ``panel_request`` — operator wants a new IDE panel. Routes to
      ``contextdna-ide-oss`` panel pipeline.
    """

    BUG = "bug"
    CONFUSION = "confusion"
    UNMET_EXPECTATION = "unmet_expectation"
    SUCCESS_STORY = "success_story"
    FEATURE_REQUEST = "feature_request"
    PANEL_REQUEST = "panel_request"

    @classmethod
    def coerce(cls, value: Any) -> "FeedbackKind":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError as e:
                raise ValueError(
                    f"unknown FeedbackKind {value!r}; "
                    f"valid: {[k.value for k in cls]}"
                ) from e
        raise TypeError(
            f"FeedbackKind expects str or FeedbackKind, got {type(value)!r}"
        )


class Severity(str, enum.Enum):
    """Operator-asserted severity. Routed to gains-gate prioritization."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @classmethod
    def coerce(cls, value: Any) -> "Severity":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.lower())
            except ValueError as e:
                raise ValueError(
                    f"unknown Severity {value!r}; "
                    f"valid: {[s.value for s in cls]}"
                ) from e
        raise TypeError(
            f"Severity expects str or Severity, got {type(value)!r}"
        )


# Default: only "bad-news" kinds auto-capture logs. Operators reporting a
# success story typically don't want their log noise serialized into the
# ledger — they explicitly opt in via ``include_logs=True``.
_DEFAULT_INCLUDE_LOGS_FOR: frozenset[FeedbackKind] = frozenset(
    {
        FeedbackKind.BUG,
        FeedbackKind.CONFUSION,
        FeedbackKind.UNMET_EXPECTATION,
    }
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FeedbackError(Exception):
    """Raised when both ledger write AND fallback write fail.

    If only the ledger write failed but the fallback succeeded,
    :func:`record_feedback` returns the :class:`FeedbackRecord` with
    ``persisted_to="fallback"`` — no exception. ``FeedbackError`` means the
    operator's report is now at risk of being lost (the caller / CLI MUST
    surface this on stderr).
    """


# ---------------------------------------------------------------------------
# ZSF counters
# ---------------------------------------------------------------------------


COUNTERS: dict[str, Any] = {
    "feedback_records_total": 0,
    "feedback_records_total_by_kind": {k.value: 0 for k in FeedbackKind},
    "feedback_records_total_by_severity": {s.value: 0 for s in Severity},
    "feedback_record_errors_total": 0,
    "feedback_fallback_writes_total": 0,
    "feedback_fallback_errors_total": 0,
}
_COUNTERS_LOCK = threading.Lock()


def _bump(counter: str, n: int = 1) -> None:
    with _COUNTERS_LOCK:
        COUNTERS[counter] = COUNTERS.get(counter, 0) + n


def _bump_dict(counter: str, key: str, n: int = 1) -> None:
    with _COUNTERS_LOCK:
        bucket = COUNTERS.setdefault(counter, {})
        bucket[key] = bucket.get(key, 0) + n


def stats() -> dict[str, Any]:
    """Snapshot of feedback counters for ``/health`` exposure (ZSF)."""
    with _COUNTERS_LOCK:
        # Deep-copy the per-kind / per-severity dicts so the caller can't
        # mutate live counters.
        snap = dict(COUNTERS)
        snap["feedback_records_total_by_kind"] = dict(
            snap.get("feedback_records_total_by_kind", {})
        )
        snap["feedback_records_total_by_severity"] = dict(
            snap.get("feedback_records_total_by_severity", {})
        )
        return snap


# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

_THIS_FILE = pathlib.Path(__file__).resolve()
# migrate2/services/feedback/ -> .../contextdna-ide-oss -> superrepo root
_REPO_ROOT: pathlib.Path = _THIS_FILE.parents[4]

FALLBACK_PATH: pathlib.Path = pathlib.Path(
    os.environ.get("CONTEXTDNA_FEEDBACK_FALLBACK", "/tmp/feedback-fallback.jsonl")
)
RECENT_LOG_WINDOW_S: int = int(os.environ.get("CONTEXTDNA_FEEDBACK_LOG_WINDOW_S", "3600"))
MAX_LOG_LINES: int = 100
MAX_LOG_FILES: int = 8  # cap the breadth so a misconfigured logs/ dir doesn't OOM
MAX_BODY_BYTES: int = 32 * 1024  # 32 KiB per body — keeps ledger row sane

EVENT_TYPE: str = "feedback.operator_reported"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FeedbackRecord:
    """Immutable receipt for a recorded feedback report.

    ``record_id`` is None when the ledger write failed AND we wrote to the
    fallback file (``persisted_to == "fallback"``). It is the SHA-256 evidence
    record_id when the ledger accepted the write (``persisted_to == "ledger"``).
    """

    client_id: str
    kind: str
    title: str
    severity: str
    created_at: str
    persisted_to: str  # "ledger" | "fallback"
    record_id: str | None
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Auto-capture helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")


def _resolve_node_id() -> str:
    for env_key in ("MULTIFLEET_NODE_ID", "CONTEXTDNA_NODE_ID"):
        v = os.environ.get(env_key)
        if v:
            return v
    try:
        return socket.gethostname().split(".")[0] or "unknown"
    except OSError:
        return "unknown"


def _resolve_active_panels() -> list[str]:
    raw = os.environ.get("CONTEXTDNA_ACTIVE_PANELS", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _resolve_profile() -> str:
    return os.environ.get("CONTEXTDNA_PROFILE", "unknown") or "unknown"


def _resolve_version() -> str:
    return os.environ.get("CONTEXTDNA_VERSION", "unknown") or "unknown"


def _tail_recent_logs() -> list[dict[str, Any]]:
    """Best-effort tail of recent log files. Never raises.

    Walks ``<repo>/logs/`` (one level deep) and returns up to ``MAX_LOG_FILES``
    entries, each holding the last ``MAX_LOG_LINES`` lines of files whose
    mtime is within ``RECENT_LOG_WINDOW_S`` of now. Empty list on any error.
    """
    out: list[dict[str, Any]] = []
    logs_dir = _REPO_ROOT / "logs"
    if not logs_dir.is_dir():
        return out
    try:
        cutoff = _dt.datetime.now(tz=_dt.timezone.utc).timestamp() - RECENT_LOG_WINDOW_S
        candidates: list[tuple[float, pathlib.Path]] = []
        for child in logs_dir.iterdir():
            if not child.is_file():
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            candidates.append((mtime, child))
        # Newest first; cap to MAX_LOG_FILES.
        candidates.sort(reverse=True)
        for _mtime, path in candidates[:MAX_LOG_FILES]:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    # Pull whole file but only keep the tail — small files
                    # in logs/ are the norm. For very large logs we trade a
                    # bit of memory for simplicity here.
                    lines = fh.readlines()[-MAX_LOG_LINES:]
                out.append({"path": str(path.relative_to(_REPO_ROOT)), "tail": lines})
            except (OSError, UnicodeError) as exc:
                logger.debug("feedback log tail failed for %s: %s", path, exc)
                continue
    except OSError as exc:
        logger.debug("feedback log scan failed: %s", exc)
    return out


def _build_payload(
    client_id: str,
    kind: FeedbackKind,
    title: str,
    body: str,
    severity: Severity,
    artifacts: list[dict] | None,
    stack_trace: str | None,
    include_logs: bool,
    created_at: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_type": EVENT_TYPE,
        "client_id": client_id,
        "kind": kind.value,
        "title": title,
        "body": body,
        "severity": severity.value,
        "created_at": created_at,
        "node_id": _resolve_node_id(),
        "mothership_version": _resolve_version(),
        "profile": _resolve_profile(),
        "active_panels": _resolve_active_panels(),
        "artifacts": list(artifacts or []),
    }
    if stack_trace:
        payload["stack_trace"] = stack_trace
    if include_logs:
        payload["recent_logs"] = _tail_recent_logs()
    return payload


# ---------------------------------------------------------------------------
# Ledger / fallback writers
# ---------------------------------------------------------------------------


def _try_write_to_ledger(payload: dict[str, Any]) -> str:
    """Attempt to write payload as ``EvidenceKind.AUDIT`` row.

    Returns the SHA-256 evidence record_id on success. Raises on any failure
    so the caller can route to the fallback path. NEVER swallows.
    """
    # Lazy import — keeps this module importable in environments that don't
    # have the mothership's memory package on sys.path (CI tests, OSS
    # consumers running the CLI on a fresh checkout).
    try:
        from memory.evidence_ledger import EvidenceKind, EvidenceLedger
    except ImportError as exc:
        raise FeedbackError(
            "evidence ledger not importable (memory/evidence_ledger.py missing); "
            "falling back to JSONL"
        ) from exc

    ledger = EvidenceLedger()
    record = ledger.record(content=payload, kind=EvidenceKind.AUDIT)
    return record.record_id


def _write_to_fallback(payload: dict[str, Any]) -> None:
    """Append payload as JSONL to :data:`FALLBACK_PATH`.

    Raises on failure so the caller bumps the right counter.
    """
    FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, default=str)
    # ``a`` mode is atomic on POSIX for small writes — fine for one JSONL row.
    with FALLBACK_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def record_feedback(
    client_id: str,
    kind: str | FeedbackKind,
    title: str,
    body: str,
    severity: str | Severity = Severity.INFO,
    artifacts: list[dict] | None = None,
    *,
    stack_trace: str | None = None,
    include_logs: bool | None = None,
) -> FeedbackRecord:
    """Record an operator feedback report to the evidence ledger.

    Parameters
    ----------
    client_id:
        Stable identifier for the reporting client (e.g. ``"aaron@m1"``,
        ``"cli:atlas"``). Used by the subconscious to weight signals from
        known-reliable reporters.
    kind:
        One of :class:`FeedbackKind` (``bug`` / ``confusion`` /
        ``unmet_expectation`` / ``success_story`` / ``feature_request`` /
        ``panel_request``). Strings are coerced.
    title:
        Short summary (≤ 200 chars recommended). Becomes the human-readable
        index for ``context-dna-ide feedback list``.
    body:
        Free-form description. Capped at ``MAX_BODY_BYTES`` (32 KiB) — overrun
        is truncated with a ``[…truncated N bytes]`` marker so the writer
        never silently drops content.
    severity:
        ``info`` / ``warning`` / ``critical``. Drives prioritization in the
        gains-gate and S3 Awareness surfacing.
    artifacts:
        Optional list of dicts describing attached files / URLs / IDs. The
        dict shape is open — typically ``{"path": "...", "sha256": "...",
        "size": N}`` or ``{"url": "..."}``.
    stack_trace:
        Optional pre-captured traceback. We do NOT walk the stack — operator
        feedback usually arrives after the fact and a synthetic traceback
        would be misleading. Pass one explicitly if you have it.
    include_logs:
        Override the default log-capture behavior. ``None`` (default) means
        "auto" — capture for ``bug`` / ``confusion`` / ``unmet_expectation``,
        skip for the positive / aspirational kinds.

    Returns
    -------
    FeedbackRecord
        Always returned on success-or-fallback. Inspect ``persisted_to`` to
        see whether the ledger or the JSONL fallback received the row.

    Raises
    ------
    ValueError
        Bad ``kind`` / ``severity`` / empty ``client_id`` / empty ``title``.
    TypeError
        ``artifacts`` not a list of dicts.
    FeedbackError
        Both ledger AND fallback writes failed. The operator's report is at
        risk; the caller MUST surface this on stderr / through /health.
    """
    # ── input validation (loud, never silent) ──
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id must be a non-empty string")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    if not isinstance(body, str):
        raise TypeError(f"body must be str, got {type(body).__name__}")
    if artifacts is not None:
        if not isinstance(artifacts, list) or not all(
            isinstance(a, dict) for a in artifacts
        ):
            raise TypeError("artifacts must be list[dict] or None")

    kind_enum = FeedbackKind.coerce(kind)
    sev_enum = Severity.coerce(severity)
    body_bytes = body.encode("utf-8")
    if len(body_bytes) > MAX_BODY_BYTES:
        truncated = body_bytes[:MAX_BODY_BYTES].decode("utf-8", errors="ignore")
        body = (
            truncated
            + f"\n[…truncated {len(body_bytes) - MAX_BODY_BYTES} bytes]"
        )

    if include_logs is None:
        include_logs = kind_enum in _DEFAULT_INCLUDE_LOGS_FOR

    created_at = _now_iso()
    payload = _build_payload(
        client_id=client_id.strip(),
        kind=kind_enum,
        title=title.strip(),
        body=body,
        severity=sev_enum,
        artifacts=artifacts,
        stack_trace=stack_trace,
        include_logs=include_logs,
        created_at=created_at,
    )

    # ── primary write: evidence ledger ──
    record_id: str | None = None
    persisted_to = "ledger"
    ledger_err: Exception | None = None
    try:
        record_id = _try_write_to_ledger(payload)
    except Exception as exc:  # noqa: BLE001 — record-and-continue at edge
        ledger_err = exc
        _bump("feedback_record_errors_total")
        logger.warning(
            "feedback ledger write failed (kind=%s severity=%s): %s: %s",
            kind_enum.value,
            sev_enum.value,
            type(exc).__name__,
            exc,
        )
        # ── fallback path ──
        try:
            _write_to_fallback({**payload, "_ledger_error": repr(exc)})
            _bump("feedback_fallback_writes_total")
            persisted_to = "fallback"
        except Exception as fb_exc:  # noqa: BLE001 — last-ditch path
            _bump("feedback_fallback_errors_total")
            logger.error(
                "feedback FALLBACK ALSO FAILED — report at risk "
                "(ledger=%s fallback=%s):\n%s",
                type(exc).__name__,
                type(fb_exc).__name__,
                "".join(traceback.format_exception(fb_exc)),
            )
            raise FeedbackError(
                f"both ledger write ({type(exc).__name__}) and fallback write "
                f"({type(fb_exc).__name__}) failed — operator report at risk"
            ) from fb_exc

    # ── counter bookkeeping (only on successful persistence) ──
    _bump("feedback_records_total")
    _bump_dict("feedback_records_total_by_kind", kind_enum.value)
    _bump_dict("feedback_records_total_by_severity", sev_enum.value)

    if ledger_err is None:
        logger.info(
            "feedback recorded kind=%s severity=%s id=%s",
            kind_enum.value,
            sev_enum.value,
            (record_id or "")[:12],
        )
    else:
        logger.info(
            "feedback recorded via FALLBACK kind=%s severity=%s "
            "(ledger=%s — replay later)",
            kind_enum.value,
            sev_enum.value,
            type(ledger_err).__name__,
        )

    return FeedbackRecord(
        client_id=payload["client_id"],
        kind=kind_enum.value,
        title=payload["title"],
        severity=sev_enum.value,
        created_at=created_at,
        persisted_to=persisted_to,
        record_id=record_id,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Reader for `context-dna-ide feedback list`
# ---------------------------------------------------------------------------


def list_recent(limit: int = 20) -> list[dict[str, Any]]:
    """Return up to ``limit`` recent feedback rows (newest first).

    Reads from the ledger when importable; otherwise falls back to tailing
    :data:`FALLBACK_PATH`. Best-effort — never raises. Each row is a dict
    with the canonical payload fields.
    """
    rows: list[dict[str, Any]] = []
    try:
        from memory.evidence_ledger import EvidenceKind, EvidenceLedger
    except ImportError:
        EvidenceLedger = None  # type: ignore[assignment]
        EvidenceKind = None  # type: ignore[assignment]

    if EvidenceLedger is not None and EvidenceKind is not None:
        try:
            ledger = EvidenceLedger()
            # The EvidenceLedger doesn't expose a public "list by kind/event"
            # method in the current minimal surface — fall through to direct
            # SQL on the SQLite file. We deliberately use the public ``_conn``
            # via a fresh ledger instance (read-only query).
            conn = ledger._conn()
            try:
                cursor = conn.execute(
                    "SELECT content_json, created_at FROM evidence_records "
                    "WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
                    (EvidenceKind.AUDIT.value, max(1, int(limit) * 4)),
                )
                for row in cursor:
                    try:
                        payload = json.loads(row["content_json"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("event_type") != EVENT_TYPE:
                        continue
                    rows.append(payload)
                    if len(rows) >= limit:
                        break
            finally:
                conn.close()
            return rows
        except Exception as exc:  # noqa: BLE001 — never raise from reader
            logger.debug("feedback list_recent ledger read failed: %s", exc)

    # Fallback file tail
    if FALLBACK_PATH.is_file():
        try:
            with FALLBACK_PATH.open("r", encoding="utf-8") as fh:
                tail = fh.readlines()[-max(1, limit) :]
            for line in reversed(tail):
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
        except OSError as exc:
            logger.debug("feedback list_recent fallback read failed: %s", exc)
    return rows


__all__ = [
    "COUNTERS",
    "EVENT_TYPE",
    "FALLBACK_PATH",
    "FeedbackError",
    "FeedbackKind",
    "FeedbackRecord",
    "Severity",
    "list_recent",
    "record_feedback",
    "stats",
]
