"""Gate infrastructure for chain orchestration.

Matches macbook1's capability-adaptive command parity design (966d85a8).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GateResult(Enum):
    PROCEED = "proceed"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass
class CommandRequirements:
    """What a segment/command needs to run."""
    min_llms: int = 0
    needs_state: bool = False
    needs_evidence: bool = False
    needs_git: bool = False
    preconditions: list[str] = field(default_factory=list)
    recommended_llms: int = 0


@dataclass
class CheckResult:
    """Result of a requirements check."""
    gate: GateResult
    reason: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class RuntimeContext:
    """Detected infrastructure state."""
    healthy_llms: list[str]
    state: Any  # StateBackend or None
    evidence: Any  # EvidenceStore or None
    git_available: bool
    git_root: str | None


def check_requirements(reqs: CommandRequirements, ctx: RuntimeContext) -> CheckResult:
    """Check if runtime context satisfies requirements.

    Returns BLOCKED on hard failures, DEGRADED when functional but reduced,
    PROCEED when all requirements fully met.
    """
    notes: list[str] = []

    # Hard blocks
    if reqs.min_llms > len(ctx.healthy_llms):
        return CheckResult(
            gate=GateResult.BLOCKED,
            reason=f"Requires {reqs.min_llms} LLM(s), only {len(ctx.healthy_llms)} available",
        )
    if reqs.needs_state and ctx.state is None:
        return CheckResult(gate=GateResult.BLOCKED, reason="State backend required but unavailable")
    if reqs.needs_evidence and ctx.evidence is None:
        return CheckResult(gate=GateResult.BLOCKED, reason="Evidence store required but unavailable")
    if reqs.needs_git and not ctx.git_available:
        return CheckResult(gate=GateResult.BLOCKED, reason="Git repository required but not detected")

    # Degradation checks
    if reqs.recommended_llms > len(ctx.healthy_llms) >= reqs.min_llms:
        notes.append(
            f"Running with {len(ctx.healthy_llms)} surgeon(s) "
            f"({reqs.recommended_llms} recommended)"
        )

    if notes:
        return CheckResult(gate=GateResult.DEGRADED, notes=notes)

    return CheckResult(gate=GateResult.PROCEED)
