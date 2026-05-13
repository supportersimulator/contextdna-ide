"""Tests for core chain segments — pre-flight, verify, gains-gate."""
import pytest
from memory.chain_engine import SEGMENT_REGISTRY, clear_registry
from memory.chain_requirements import RuntimeContext, CommandRequirements
# Import to trigger registration
import memory.chain_segments_core


@pytest.fixture(autouse=True)
def preserve_registry():
    """Don't clear core segments — they register on import."""
    yield


def _ctx(llms=None, state="mock", git=True):
    return RuntimeContext(
        healthy_llms=llms if llms is not None else ["test-llm"],
        state=state, evidence=state,
        git_available=git,
        git_root="$HOME/dev/er-simulator-superrepo" if git else None,
    )


class TestPreFlight:
    def test_registered(self):
        assert "pre-flight" in SEGMENT_REGISTRY

    def test_returns_checks(self):
        result = SEGMENT_REGISTRY["pre-flight"].fn(_ctx(), {})
        assert "preflight_checks" in result
        assert "preflight_ok" in result
        assert isinstance(result["preflight_checks"], list)

    def test_detects_llms(self):
        result = SEGMENT_REGISTRY["pre-flight"].fn(_ctx(llms=["a", "b"]), {})
        assert result["llm_count"] == 2

    def test_no_llms(self):
        result = SEGMENT_REGISTRY["pre-flight"].fn(_ctx(llms=[]), {})
        assert result["llm_count"] == 0

    def test_no_git(self):
        result = SEGMENT_REGISTRY["pre-flight"].fn(_ctx(git=False), {})
        assert result["git_clean"] is False

    def test_state_type_memory(self):
        result = SEGMENT_REGISTRY["pre-flight"].fn(_ctx(state="memory"), {})
        assert result["state_type"] == "memory"

    def test_state_type_none(self):
        result = SEGMENT_REGISTRY["pre-flight"].fn(_ctx(state=None), {})
        assert result["state_type"] == "none"


class TestVerify:
    def test_registered(self):
        assert "verify" in SEGMENT_REGISTRY

    def test_clean_verify(self):
        result = SEGMENT_REGISTRY["verify"].fn(_ctx(), {})
        assert "verified" in result
        assert "verify_issues" in result

    def test_reports_chain_errors(self):
        result = SEGMENT_REGISTRY["verify"].fn(_ctx(), {"errors": ["e1", "e2"]})
        assert not result["verified"]
        assert any("error" in i.lower() for i in result["verify_issues"])

    def test_passes_topic(self):
        result = SEGMENT_REGISTRY["verify"].fn(_ctx(), {"topic": "auth fix"})
        assert result["verify_topic"] == "auth fix"

    def test_no_git_still_works(self):
        result = SEGMENT_REGISTRY["verify"].fn(_ctx(git=False), {})
        assert "verified" in result


class TestGainsGate:
    def test_registered(self):
        assert "gains-gate" in SEGMENT_REGISTRY

    def test_all_pass(self):
        result = SEGMENT_REGISTRY["gains-gate"].fn(_ctx(), {"preflight_ok": True})
        assert result["gains_gate_pass"] is True
        assert result["gains_gate_critical_failures"] == 0

    def test_fails_without_llms(self):
        result = SEGMENT_REGISTRY["gains-gate"].fn(_ctx(llms=[]), {"preflight_ok": True})
        assert result["gains_gate_pass"] is False
        assert result["gains_gate_critical_failures"] > 0

    def test_fails_without_state(self):
        result = SEGMENT_REGISTRY["gains-gate"].fn(_ctx(state=None), {"preflight_ok": True})
        assert result["gains_gate_pass"] is False

    def test_preflight_failure_cascades(self):
        result = SEGMENT_REGISTRY["gains-gate"].fn(_ctx(), {"preflight_ok": False})
        assert result["gains_gate_pass"] is False

    def test_returns_check_details(self):
        result = SEGMENT_REGISTRY["gains-gate"].fn(_ctx(), {})
        assert len(result["gains_gate_checks"]) == 4
        for check in result["gains_gate_checks"]:
            assert "name" in check
            assert "pass" in check
            assert "detail" in check
