"""Tests for chain_consultation."""
import pytest
from memory.chain_consultation import (
    ChainConsultation, CommunityPreset, should_consult, META_REVIEW_REQS,
)


def test_should_consult_at_cadence():
    assert should_consult(total_executions=20, last_consultation_at=0, cadence=20)
    assert should_consult(total_executions=40, last_consultation_at=20, cadence=20)


def test_should_not_consult_below_cadence():
    assert not should_consult(total_executions=15, last_consultation_at=0, cadence=20)
    assert not should_consult(total_executions=25, last_consultation_at=20, cadence=20)


def test_community_preset_to_yaml():
    preset = CommunityPreset(name="python-web",
        segments=["pre-flight", "risk-scan", "execute", "verify"],
        evidence_grade="correlation", observations=15,
        consensus_score=0.85, description="Optimized for Python web apps")
    y = preset.to_yaml()
    assert "python-web" in y
    assert "pre-flight" in y


def test_community_preset_roundtrip():
    preset = CommunityPreset(name="roundtrip-test", segments=["a", "b", "c"],
        evidence_grade="case_series", observations=30,
        consensus_score=0.90, description="Test preset")
    y = preset.to_yaml()
    restored = CommunityPreset.from_yaml(y)
    assert restored.name == preset.name
    assert restored.segments == preset.segments
    assert restored.observations == preset.observations
    assert restored.consensus_score == preset.consensus_score


def test_consultation_build_context():
    c = ChainConsultation()
    context = c.build_consultation_context(
        segments=["a", "b", "c"],
        presets={"full-3s": ["a", "b", "c"]},
        recent_failures=["b failed at step 2"])
    assert "a" in context
    assert "full-3s" in context
    assert "b failed" in context


def test_consultation_mark_consulted():
    c = ChainConsultation()
    assert c.last_consultation_at == 0
    c.mark_consulted(at_execution=20)
    assert c.last_consultation_at == 20


def test_meta_review_requirements():
    assert META_REVIEW_REQS.min_llms == 2
    assert META_REVIEW_REQS.needs_state is True
    assert META_REVIEW_REQS.needs_evidence is True
    assert META_REVIEW_REQS.recommended_llms == 3
