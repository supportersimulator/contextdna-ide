"""Tests for chain_requirements — gate infrastructure."""
import pytest
from memory.chain_requirements import (
    CommandRequirements, GateResult, RuntimeContext, check_requirements,
)


def test_gate_result_enum():
    assert GateResult.PROCEED.value == "proceed"
    assert GateResult.DEGRADED.value == "degraded"
    assert GateResult.BLOCKED.value == "blocked"


def test_command_requirements_defaults():
    reqs = CommandRequirements()
    assert reqs.min_llms == 0
    assert reqs.needs_state is False
    assert reqs.needs_evidence is False
    assert reqs.needs_git is False
    assert reqs.preconditions == []
    assert reqs.recommended_llms == 0


def test_runtime_context_minimal():
    ctx = RuntimeContext(healthy_llms=[], state=None, evidence=None,
                         git_available=False, git_root=None)
    assert ctx.healthy_llms == []


def test_check_requirements_proceed():
    reqs = CommandRequirements(min_llms=0)
    ctx = RuntimeContext(healthy_llms=[], state=None, evidence=None,
                         git_available=False, git_root=None)
    result = check_requirements(reqs, ctx)
    assert result.gate == GateResult.PROCEED


def test_check_requirements_blocked_llms():
    reqs = CommandRequirements(min_llms=2)
    ctx = RuntimeContext(healthy_llms=["one"], state=None, evidence=None,
                         git_available=False, git_root=None)
    result = check_requirements(reqs, ctx)
    assert result.gate == GateResult.BLOCKED
    assert "LLM" in result.reason


def test_check_requirements_degraded():
    reqs = CommandRequirements(min_llms=1, recommended_llms=3)
    ctx = RuntimeContext(healthy_llms=["one"], state=None, evidence=None,
                         git_available=True, git_root="/tmp")
    result = check_requirements(reqs, ctx)
    assert result.gate == GateResult.DEGRADED
    assert len(result.notes) > 0


def test_check_requirements_blocked_git():
    reqs = CommandRequirements(needs_git=True)
    ctx = RuntimeContext(healthy_llms=[], state=None, evidence=None,
                         git_available=False, git_root=None)
    result = check_requirements(reqs, ctx)
    assert result.gate == GateResult.BLOCKED
    assert "git" in result.reason.lower()


def test_check_requirements_blocked_state():
    reqs = CommandRequirements(needs_state=True)
    ctx = RuntimeContext(healthy_llms=[], state=None, evidence=None,
                         git_available=False, git_root=None)
    result = check_requirements(reqs, ctx)
    assert result.gate == GateResult.BLOCKED


def test_check_requirements_blocked_evidence():
    reqs = CommandRequirements(needs_evidence=True)
    ctx = RuntimeContext(healthy_llms=[], state=None, evidence=None,
                         git_available=False, git_root=None)
    result = check_requirements(reqs, ctx)
    assert result.gate == GateResult.BLOCKED
