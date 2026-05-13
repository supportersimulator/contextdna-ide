"""Chain telemetry — execution recording, pattern detection, evidence grades.

Matches macbook1's orchestration layer design (63ca4eab), Section 4.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class EvidenceGrade(Enum):
    ANECDOTE = "anecdote"
    CORRELATION = "correlation"
    CASE_SERIES = "case_series"
    COHORT = "cohort"

    @classmethod
    def for_observations(cls, count: int, frequency: float) -> EvidenceGrade:
        if count >= 50 and frequency >= 0.95:
            return cls.COHORT
        if count >= 20 and frequency >= 0.85:
            return cls.CASE_SERIES
        if count >= 5 and frequency >= 0.70:
            return cls.CORRELATION
        return cls.ANECDOTE


@dataclass
class ExecutionRecord:
    chain_id: str
    execution_id: str
    segments_run: list[str]
    segments_skipped: list[str]
    order_digest: str
    success: bool
    failed_segment: str | None
    duration_ms: float
    duration_by_segment: dict[str, float]
    project_id: str
    timestamp: str

    @classmethod
    def create(cls, chain_id, segments_run, segments_skipped, success,
               duration_ms, duration_by_segment, project_id, failed_segment=None):
        order_digest = hashlib.sha256(",".join(segments_run).encode()).hexdigest()[:12]
        return cls(
            chain_id=chain_id, execution_id=str(uuid.uuid4()),
            segments_run=segments_run, segments_skipped=segments_skipped,
            order_digest=order_digest, success=success, failed_segment=failed_segment,
            duration_ms=duration_ms, duration_by_segment=duration_by_segment,
            project_id=project_id, timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, data: str) -> ExecutionRecord:
        return cls(**json.loads(data))


@dataclass
class DetectedPattern:
    order_digest: str
    segments: list[str]
    frequency: float
    observations: int
    grade: EvidenceGrade


class ChainTelemetry:
    def __init__(self, backend="memory", min_observations=5, min_frequency=0.70):
        self._backend = backend
        self._min_observations = min_observations
        self._min_frequency = min_frequency
        self._records: dict[str, list[ExecutionRecord]] = {}

    def record(self, rec: ExecutionRecord) -> None:
        if rec.chain_id not in self._records:
            self._records[rec.chain_id] = []
        self._records[rec.chain_id].append(rec)

    def recent_executions(self, chain_id: str, limit: int = 50) -> list[ExecutionRecord]:
        return self._records.get(chain_id, [])[-limit:]

    def detect_patterns(self, chain_id: str) -> list[DetectedPattern]:
        records = self._records.get(chain_id, [])
        if len(records) < self._min_observations:
            return []
        counter = Counter(r.order_digest for r in records)
        total = len(records)
        patterns = []
        for digest, count in counter.most_common():
            freq = count / total
            if freq < self._min_frequency or count < self._min_observations:
                continue
            rep = next(r for r in records if r.order_digest == digest)
            grade = EvidenceGrade.for_observations(count, freq)
            patterns.append(DetectedPattern(
                order_digest=digest, segments=rep.segments_run,
                frequency=freq, observations=count, grade=grade,
            ))
        return patterns
