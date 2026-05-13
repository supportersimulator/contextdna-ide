"""Tests for chain_telemetry — execution recording, pattern detection, evidence grades."""
import pytest
from memory.chain_telemetry import (
    EvidenceGrade, ExecutionRecord, ChainTelemetry, DetectedPattern,
)


def test_evidence_grade_thresholds():
    assert EvidenceGrade.for_observations(3, 0.9) == EvidenceGrade.ANECDOTE
    assert EvidenceGrade.for_observations(10, 0.75) == EvidenceGrade.CORRELATION
    assert EvidenceGrade.for_observations(30, 0.90) == EvidenceGrade.CASE_SERIES
    assert EvidenceGrade.for_observations(60, 0.96) == EvidenceGrade.COHORT


def test_evidence_grade_low_frequency():
    assert EvidenceGrade.for_observations(25, 0.60) == EvidenceGrade.ANECDOTE


def test_execution_record_create():
    rec = ExecutionRecord.create(
        chain_id="full-3s", segments_run=["pre-flight", "execute", "verify"],
        segments_skipped=["doc-flow"], success=True, duration_ms=1234.5,
        duration_by_segment={"pre-flight": 100, "execute": 900, "verify": 234.5},
        project_id="test-project",
    )
    assert rec.chain_id == "full-3s"
    assert rec.execution_id
    assert rec.order_digest
    assert rec.success is True
    assert rec.failed_segment is None
    assert rec.timestamp


def test_execution_record_serialization():
    rec = ExecutionRecord.create(
        chain_id="test", segments_run=["a", "b"], segments_skipped=[],
        success=True, duration_ms=100,
        duration_by_segment={"a": 50, "b": 50}, project_id="proj",
    )
    j = rec.to_json()
    restored = ExecutionRecord.from_json(j)
    assert restored.chain_id == rec.chain_id
    assert restored.execution_id == rec.execution_id
    assert restored.order_digest == rec.order_digest


def test_chain_telemetry_record_and_retrieve():
    ct = ChainTelemetry(backend="memory")
    rec = ExecutionRecord.create(
        chain_id="test-chain", segments_run=["a", "b"], segments_skipped=[],
        success=True, duration_ms=100,
        duration_by_segment={"a": 50, "b": 50}, project_id="proj",
    )
    ct.record(rec)
    recent = ct.recent_executions("test-chain")
    assert len(recent) == 1
    assert recent[0].execution_id == rec.execution_id


def test_chain_telemetry_detect_patterns():
    ct = ChainTelemetry(backend="memory")
    for _ in range(10):
        rec = ExecutionRecord.create(
            chain_id="detect-me", segments_run=["a", "b", "c"],
            segments_skipped=[], success=True, duration_ms=100,
            duration_by_segment={"a": 30, "b": 40, "c": 30}, project_id="proj",
        )
        ct.record(rec)
    patterns = ct.detect_patterns("detect-me")
    assert len(patterns) >= 1
    assert patterns[0].frequency >= 0.70
    assert patterns[0].grade == EvidenceGrade.CORRELATION


def test_chain_telemetry_no_pattern_below_threshold():
    ct = ChainTelemetry(backend="memory")
    for _ in range(3):
        rec = ExecutionRecord.create(
            chain_id="too-few", segments_run=["x"], segments_skipped=[],
            success=True, duration_ms=50,
            duration_by_segment={"x": 50}, project_id="proj",
        )
        ct.record(rec)
    assert len(ct.detect_patterns("too-few")) == 0


def test_detected_pattern_dataclass():
    dp = DetectedPattern(
        order_digest="abc123", segments=["a", "b"],
        frequency=0.85, observations=25, grade=EvidenceGrade.CASE_SERIES,
    )
    assert dp.grade == EvidenceGrade.CASE_SERIES
