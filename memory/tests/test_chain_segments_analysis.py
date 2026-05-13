"""Tests for analysis chain segments — risk-scan and contradiction-scan."""
import os
from pathlib import Path
import pytest
from memory.chain_engine import SEGMENT_REGISTRY
from memory.chain_requirements import RuntimeContext
import memory.chain_segments_analysis
from memory.chain_segments_analysis import _scan_text_for_risk


REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)


def _ctx(git=True):
    return RuntimeContext(
        healthy_llms=["test-llm"],
        state="mock", evidence="mock",
        git_available=git,
        git_root=REPO_ROOT if git else None,
    )


class TestRiskScan:
    def test_registered(self):
        assert "risk-scan" in SEGMENT_REGISTRY

    def test_low_risk_topic(self):
        # Use git=False to test topic classification only (not working tree state)
        result = SEGMENT_REGISTRY["risk-scan"].fn(_ctx(git=False), {"topic": "fix typo in readme"})
        assert result["risk_level"] == "low"

    def test_high_risk_auth_topic(self):
        result = SEGMENT_REGISTRY["risk-scan"].fn(
            _ctx(), {"topic": "modify auth token validation"})
        assert result["risk_level"] == "high"
        assert len(result["risk_signals"]) > 0

    def test_medium_risk_config_topic(self):
        result = SEGMENT_REGISTRY["risk-scan"].fn(
            _ctx(), {"topic": "update yaml config for api endpoint"})
        assert result["risk_level"] in ("medium", "high")

    def test_escalation_mode_mapping(self):
        # Use git=False to test topic classification only (not working tree state)
        result = SEGMENT_REGISTRY["risk-scan"].fn(_ctx(git=False), {"topic": "fix typo"})
        assert result["risk_escalation_mode"] == "Light"

    def test_high_risk_production_topic(self):
        result = SEGMENT_REGISTRY["risk-scan"].fn(
            _ctx(), {"topic": "deploy to production database migration"})
        assert result["risk_level"] == "high"

    def test_no_git_still_works(self):
        result = SEGMENT_REGISTRY["risk-scan"].fn(
            _ctx(git=False), {"topic": "some work"})
        assert "risk_level" in result
        assert result["risk_files_changed"] == 0


class TestScanTextHelper:
    def test_high_signal(self):
        level, signals = _scan_text_for_risk("update auth password handler")
        assert level == "high"

    def test_low_signal(self):
        level, signals = _scan_text_for_risk("fix readme typo")
        assert level == "low"
        assert len(signals) == 0

    def test_medium_signal(self):
        level, signals = _scan_text_for_risk("update config yaml")
        assert level == "medium"


class TestContradictionScan:
    def test_registered(self):
        assert "contradiction-scan" in SEGMENT_REGISTRY

    def test_empty_topic(self):
        result = SEGMENT_REGISTRY["contradiction-scan"].fn(_ctx(), {"topic": ""})
        assert result["contradiction_aligned"] is True
        assert result["contradiction_docs_checked"] == 0

    def test_scans_docs_directory(self):
        result = SEGMENT_REGISTRY["contradiction-scan"].fn(
            _ctx(), {"topic": "chain orchestration foundation design"})
        assert result["contradiction_docs_checked"] > 0

    def test_returns_structure(self):
        result = SEGMENT_REGISTRY["contradiction-scan"].fn(
            _ctx(), {"topic": "some analysis topic"})
        assert "contradictions" in result
        assert "contradiction_aligned" in result
        assert isinstance(result["contradictions"], list)

    def test_blocked_without_git(self):
        # contradiction-scan requires git, so check requirements
        seg = SEGMENT_REGISTRY["contradiction-scan"]
        assert seg.requires.needs_git is True
