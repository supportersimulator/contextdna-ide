"""Tests for review chain segments — plan-review and pre-impl."""
from pathlib import Path
import pytest
from memory.chain_engine import SEGMENT_REGISTRY
from memory.chain_requirements import RuntimeContext
import memory.chain_segments_review


REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)


def _ctx(git=True):
    return RuntimeContext(
        healthy_llms=["test-llm"],
        state="mock", evidence="mock",
        git_available=git,
        git_root=REPO_ROOT if git else None,
    )


class TestPlanReview:
    def test_registered(self):
        assert "plan-review" in SEGMENT_REGISTRY

    def test_requires_git(self):
        assert SEGMENT_REGISTRY["plan-review"].requires.needs_git is True

    def test_aligned_verdict(self):
        result = SEGMENT_REGISTRY["plan-review"].fn(
            _ctx(), {"topic": "add chain telemetry recording"})
        assert result["plan_review_verdict"] in (
            "ALIGNED", "MISALIGNED", "MISSING_PREREQUISITE", "INSUFFICIENT_CONTEXT")
        assert "plan_review_recommendation" in result

    def test_empty_topic_insufficient(self):
        result = SEGMENT_REGISTRY["plan-review"].fn(_ctx(), {"topic": ""})
        assert result["plan_review_verdict"] == "INSUFFICIENT_CONTEXT"

    def test_strategic_context_collected(self):
        result = SEGMENT_REGISTRY["plan-review"].fn(
            _ctx(), {"topic": "orchestration chain segments"})
        ctx = result["plan_review_strategic_context"]
        # At minimum, inbox/ should exist
        assert any("inbox" in k for k in ctx.keys())

    def test_large_scope_detected(self):
        result = SEGMENT_REGISTRY["plan-review"].fn(
            _ctx(), {"topic": "complete architecture redesign of the system"})
        signals = result["plan_review_misalignment_signals"]
        assert any("scope" in s.lower() for s in signals)

    def test_with_plan_content(self):
        result = SEGMENT_REGISTRY["plan-review"].fn(
            _ctx(), {
                "topic": "add logging",
                "plan_content": "This depends on the new config system being ready first",
            })
        signals = result["plan_review_alignment_signals"]
        assert any("prerequisite" in s.lower() for s in signals)

    def test_returns_folder_doc_counts(self):
        result = SEGMENT_REGISTRY["plan-review"].fn(
            _ctx(), {"topic": "chain segments"})
        for folder_info in result["plan_review_strategic_context"].values():
            assert "doc_count" in folder_info
            assert "purpose" in folder_info


class TestPreImpl:
    def test_registered(self):
        assert "pre-impl" in SEGMENT_REGISTRY

    def test_clear_to_proceed(self):
        result = SEGMENT_REGISTRY["pre-impl"].fn(
            _ctx(), {"plan_review_verdict": "ALIGNED", "risk_level": "low"})
        assert result["pre_impl_proceed"] is True
        assert result["pre_impl_summary"] == "Clear to proceed"

    def test_blocked_by_misalignment(self):
        result = SEGMENT_REGISTRY["pre-impl"].fn(
            _ctx(), {"plan_review_verdict": "MISALIGNED"})
        assert result["pre_impl_proceed"] is False
        assert len(result["pre_impl_blockers"]) > 0

    def test_blocked_by_contradictions(self):
        result = SEGMENT_REGISTRY["pre-impl"].fn(
            _ctx(), {"contradictions": [{"file": "test.md", "kind": "prohibition"}]})
        assert result["pre_impl_proceed"] is False

    def test_blocked_by_gains_gate(self):
        result = SEGMENT_REGISTRY["pre-impl"].fn(
            _ctx(), {"gains_gate_pass": False})
        assert result["pre_impl_proceed"] is False

    def test_warning_on_high_risk(self):
        result = SEGMENT_REGISTRY["pre-impl"].fn(
            _ctx(), {"risk_level": "high"})
        assert result["pre_impl_proceed"] is True  # Warning, not blocker
        assert len(result["pre_impl_warnings"]) > 0

    def test_no_requirements(self):
        seg = SEGMENT_REGISTRY["pre-impl"]
        assert seg.requires.min_llms == 0
        assert seg.requires.needs_git is False
