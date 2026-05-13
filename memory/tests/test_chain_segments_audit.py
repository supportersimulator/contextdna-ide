"""Tests for deep-audit chain segments — 5-phase pipeline decomposition."""
from pathlib import Path
import pytest
from memory.chain_engine import SEGMENT_REGISTRY, ChainExecutor, clear_registry
from memory.chain_requirements import RuntimeContext, CommandRequirements
import memory.chain_segments_audit


REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)


def _ctx(llms=None, git=True):
    return RuntimeContext(
        healthy_llms=llms or ["test-llm"],
        state="mock", evidence="mock",
        git_available=git,
        git_root=REPO_ROOT if git else None,
    )


class TestAuditDiscover:
    def test_registered(self):
        assert "audit-discover" in SEGMENT_REGISTRY

    def test_discovers_docs(self):
        result = SEGMENT_REGISTRY["audit-discover"].fn(
            _ctx(), {"topic": "chain orchestration"})
        assert result["audit_docs_discovered"] > 0
        assert isinstance(result["audit_docs_selected"], list)
        assert len(result["audit_docs_selected"]) <= 10

    def test_relevance_scoring(self):
        result = SEGMENT_REGISTRY["audit-discover"].fn(
            _ctx(), {"topic": "orchestration implementation"})
        selected = result["audit_docs_selected"]
        if len(selected) > 1:
            # First should have >= relevance score of last
            assert selected[0]["relevance_score"] >= selected[-1]["relevance_score"]

    def test_empty_topic(self):
        result = SEGMENT_REGISTRY["audit-discover"].fn(_ctx(), {"topic": ""})
        assert isinstance(result["audit_docs_selected"], list)


class TestAuditRead:
    def test_registered(self):
        assert "audit-read" in SEGMENT_REGISTRY

    def test_reads_discovered_docs(self):
        # First discover, then read
        discover_result = SEGMENT_REGISTRY["audit-discover"].fn(
            _ctx(), {"topic": "chain orchestration"})
        read_result = SEGMENT_REGISTRY["audit-read"].fn(
            _ctx(), discover_result)
        assert read_result["audit_docs_read"] > 0
        assert read_result["audit_total_chars"] > 0

    def test_empty_selection(self):
        result = SEGMENT_REGISTRY["audit-read"].fn(
            _ctx(), {"audit_docs_selected": []})
        assert result["audit_docs_read"] == 0


class TestAuditExtract:
    def test_registered(self):
        assert "audit-extract" in SEGMENT_REGISTRY

    def test_requires_llm(self):
        seg = SEGMENT_REGISTRY["audit-extract"]
        assert seg.requires.min_llms == 1

    def test_test_mode(self):
        result = SEGMENT_REGISTRY["audit-extract"].fn(
            _ctx(), {"_test_mode": True, "audit_doc_contents": {"test.md": "content"}})
        assert result["audit_extract_mode"] == "test"
        assert len(result["audit_features"]) > 0
        assert result["audit_features"][0]["status"] == "PLANNED"


class TestAuditCrosscheck:
    def test_registered(self):
        assert "audit-crosscheck" in SEGMENT_REGISTRY

    def test_test_mode(self):
        features = [{"name": "feat-1", "status": "PLANNED"}]
        result = SEGMENT_REGISTRY["audit-crosscheck"].fn(
            _ctx(), {"_test_mode": True, "audit_features": features})
        assert result["audit_crosscheck_mode"] == "test"
        assert len(result["audit_verdicts"]) == 1
        assert result["audit_verdicts"][0]["verdict"] == "NOT_BUILT"

    def test_empty_features(self):
        result = SEGMENT_REGISTRY["audit-crosscheck"].fn(
            _ctx(), {"audit_features": []})
        assert result["audit_verdicts"] == []


class TestAuditReport:
    def test_registered(self):
        assert "audit-report" in SEGMENT_REGISTRY

    def test_produces_summary(self):
        data = {
            "audit_features": [
                {"name": "f1", "status": "PLANNED"},
                {"name": "f2", "status": "PARTIAL"},
            ],
            "audit_verdicts": [
                {"feature": "f1", "verdict": "NOT_BUILT"},
                {"feature": "f2", "verdict": "PARTIALLY_BUILT"},
            ],
            "audit_ab_candidates": [{"feature": "f1"}],
            "audit_docs_read": 5,
            "audit_total_chars": 10000,
        }
        result = SEGMENT_REGISTRY["audit-report"].fn(_ctx(), data)
        assert result["audit_complete"] is True
        summary = result["audit_summary"]
        assert summary["features_found"] == 2
        assert summary["ab_candidates_count"] == 1
        assert summary["docs_read"] == 5

    def test_no_requirements(self):
        seg = SEGMENT_REGISTRY["audit-report"]
        assert seg.requires.min_llms == 0


class TestAuditChainEndToEnd:
    def test_full_pipeline_test_mode(self):
        """Run all 5 audit segments as a chain in test mode."""
        ctx = _ctx(llms=["test-llm"])
        data = {"topic": "chain orchestration", "_test_mode": True}

        # Phase 1: Discover
        r1 = SEGMENT_REGISTRY["audit-discover"].fn(ctx, data)
        data.update(r1)

        # Phase 2: Read
        r2 = SEGMENT_REGISTRY["audit-read"].fn(ctx, data)
        data.update(r2)

        # Phase 3: Extract (test mode)
        r3 = SEGMENT_REGISTRY["audit-extract"].fn(ctx, data)
        data.update(r3)

        # Phase 4: Crosscheck (test mode)
        r4 = SEGMENT_REGISTRY["audit-crosscheck"].fn(ctx, data)
        data.update(r4)

        # Phase 5: Report
        r5 = SEGMENT_REGISTRY["audit-report"].fn(ctx, data)
        data.update(r5)

        assert data["audit_complete"] is True
        assert data["audit_docs_read"] > 0
        assert len(data["audit_features"]) > 0
        assert data["audit_summary"]["features_found"] > 0
