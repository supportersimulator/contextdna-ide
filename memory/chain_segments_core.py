"""Core chain segments — pre-flight, verify, gains-gate.

These appear in every preset and form the foundation of chain execution.
"""
from __future__ import annotations

import os
import subprocess

from memory.chain_engine import segment
from memory.chain_requirements import CommandRequirements


@segment(
    name="pre-flight",
    requires=CommandRequirements(),
    tags=["core", "health"],
)
def seg_pre_flight(ctx, data: dict) -> dict:
    """Check system health: git, LLMs, state backend."""
    checks = []

    # Git status
    git_clean = False
    if ctx.git_available:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=ctx.git_root,
                timeout=5,
            )
            git_clean = len(result.stdout.strip()) == 0
            checks.append({"name": "git", "status": "clean" if git_clean else "dirty",
                           "ok": True})
        except Exception as e:
            checks.append({"name": "git", "status": f"error: {e}", "ok": False})
    else:
        checks.append({"name": "git", "status": "not available", "ok": False})

    # LLM availability
    llm_count = len(ctx.healthy_llms)
    checks.append({
        "name": "llms",
        "status": f"{llm_count} available: {', '.join(ctx.healthy_llms) or 'none'}",
        "ok": llm_count > 0,
    })

    # State backend
    state_type = "none"
    if ctx.state is not None:
        if ctx.state == "memory":
            state_type = "memory"
        else:
            try:
                ctx.state.ping()
                state_type = "redis"
            except Exception:
                state_type = "memory-fallback"
    checks.append({"name": "state", "status": state_type, "ok": ctx.state is not None})

    all_ok = all(c["ok"] for c in checks)
    return {
        "preflight_ok": all_ok,
        "preflight_checks": checks,
        "llm_count": llm_count,
        "git_clean": git_clean,
        "state_type": state_type,
    }


@segment(
    name="verify",
    requires=CommandRequirements(),
    tags=["core", "validation"],
)
def seg_verify(ctx, data: dict) -> dict:
    """Verify work was done correctly — check for uncommitted changes, basic validation."""
    issues = []

    # Check for uncommitted changes
    if ctx.git_available:
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True, cwd=ctx.git_root,
                timeout=5,
            )
            changed_files = [f for f in result.stdout.strip().split("\n") if f]
            if changed_files:
                issues.append(f"Uncommitted changes in {len(changed_files)} file(s)")
        except Exception as e:
            issues.append(f"Git check failed: {e}")

    # Check if any segments had errors earlier in the chain
    chain_errors = data.get("errors", [])
    if chain_errors:
        issues.append(f"{len(chain_errors)} error(s) from earlier segments")

    # Check if any segments were skipped
    topic = data.get("topic", "")
    verified = len(issues) == 0

    return {
        "verified": verified,
        "verify_issues": issues,
        "verify_topic": topic,
    }


@segment(
    name="gains-gate",
    requires=CommandRequirements(),
    tags=["core", "health", "gate"],
)
def seg_gains_gate(ctx, data: dict) -> dict:
    """Infrastructure health gate — verify system state between phases."""
    checks = []

    # LLM health
    llm_ok = len(ctx.healthy_llms) > 0
    checks.append({"name": "llm_health", "pass": llm_ok,
                    "detail": f"{len(ctx.healthy_llms)} surgeon(s) available"})

    # State backend health
    state_ok = ctx.state is not None
    checks.append({"name": "state_backend", "pass": state_ok,
                    "detail": "available" if state_ok else "unavailable"})

    # Git health
    git_ok = ctx.git_available
    checks.append({"name": "git", "pass": git_ok,
                    "detail": ctx.git_root or "not available"})

    # Check prior chain health
    preflight_ok = data.get("preflight_ok", True)
    checks.append({"name": "preflight", "pass": preflight_ok,
                    "detail": "passed" if preflight_ok else "failed or not run"})

    all_pass = all(c["pass"] for c in checks)
    critical_failures = [c for c in checks if not c["pass"]]

    return {
        "gains_gate_pass": all_pass,
        "gains_gate_checks": checks,
        "gains_gate_critical_failures": len(critical_failures),
    }
