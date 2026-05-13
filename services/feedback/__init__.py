"""Operator feedback service for the ContextDNA mothership.

Captures user-driven friction reports (bugs, confusion, unmet expectations,
success stories, feature requests, panel requests) and writes them to the
evidence ledger so the subconscious can promote / retire patterns based on
real-world signal.

Public surface
--------------
- :class:`FeedbackKind` — enum of accepted ``kind`` values (six total).
- :class:`Severity` — enum of accepted ``severity`` values (info / warning / critical).
- :class:`FeedbackRecord` — immutable dataclass returned from :func:`record_feedback`.
- :func:`record_feedback` — primary entry point. Writes a ``feedback.operator_reported``
  payload as an ``EvidenceKind.AUDIT`` row in the SQLite evidence ledger
  (``memory/evidence_ledger.py``). Falls back to ``/tmp/feedback-fallback.jsonl``
  when Postgres / SQLite is unavailable so the report survives.
- :func:`stats` — ZSF observability. Returns counter snapshot for ``/health``.
- :func:`list_recent` — convenience reader for ``context-dna-ide feedback list``.

The handler itself is in :mod:`.handler`; the operator CLI in :mod:`.cli`.
"""
from __future__ import annotations

from .handler import (
    COUNTERS,
    FALLBACK_PATH,
    FeedbackKind,
    FeedbackRecord,
    FeedbackError,
    Severity,
    list_recent,
    record_feedback,
    stats,
)

__all__ = [
    "COUNTERS",
    "FALLBACK_PATH",
    "FeedbackError",
    "FeedbackKind",
    "FeedbackRecord",
    "Severity",
    "list_recent",
    "record_feedback",
    "stats",
]
