"""
RACE AB1 — Tests for brainstorming-skill auto-3s consult wiring.

Verifies that creative-class prompts (≥6 words) entering the brainstorming
skill path automatically trigger a 3-Surgeons consult, with the resulting
verdict prepended to S6 output as `[3S_VERDICT: <summary>]`.

Behavior under test:
- Creative prompt (≥6 words)            → auto_consult IS called, verdict line emitted
- Read-only prompt                      → auto_consult NOT called
- Writing-plans prompt                  → auto_consult NOT called (only brainstorming triggers it)
- Short creative prompt (<6 words)      → auto_consult bridge SKIPS
- 3s timeout / error                    → graceful fallback (no verdict line)
- ZSF: failure increments error counter, never raises

We mock out:
  * memory.surgery_bridge.auto_consult internals (the subprocess call) when
    we want deterministic responses
  * the LLM path in generate_deep_s6 (already covered in test_s6_skill_wiring;
    we only assert the verdict line, not the LLM body)
"""

from __future__ import annotations

import sys
import types

import pytest


# =========================================================================
# Helper: fresh import + patch surface for surgery_bridge / synaptic_deep_voice
# =========================================================================

@pytest.fixture
def fresh_bridge(monkeypatch):
    """Reset auto_consult counters and surgery_bridge module state per-test."""
    import memory.surgery_bridge as sb
    # Reset counters so each test sees a clean slate.
    with sb._auto_consult_lock:
        for k in sb._AUTO_CONSULT_COUNTERS:
            sb._AUTO_CONSULT_COUNTERS[k] = 0
    return sb


@pytest.fixture
def mocked_s6_pathway(monkeypatch):
    """Mock the S6 LLM path + cache so we can exercise classifier+auto_consult.

    Returns the synaptic_deep_voice module with stubs in place for everything
    that touches I/O EXCEPT the AB1 auto-3s wiring (that's the SUT).
    """
    # Disable Redis cache to force the LLM path deterministically.
    monkeypatch.setenv("S6_CACHE_ENABLED", "0")

    import memory.synaptic_deep_voice as dv

    monkeypatch.setattr(dv, "_get_personality_context", lambda: "stub-personality")
    monkeypatch.setattr(dv, "_get_pattern_context_for_task", lambda p: "stub-patterns")
    monkeypatch.setattr(dv, "_get_evolution_context", lambda: "stub-evo")
    monkeypatch.setattr(dv, "_get_wisdom_for_task", lambda p: "stub-wisdom")

    fake_llm = types.ModuleType("memory.llm_priority_queue")

    class Priority:
        AARON = "AARON"
        ATLAS = "ATLAS"
        EXTERNAL = "EXTERNAL"
        BACKGROUND = "BACKGROUND"

    def llm_generate(**kwargs):
        return "deep-guidance-body-from-mocked-llm-call"

    fake_llm.Priority = Priority
    fake_llm.llm_generate = llm_generate
    monkeypatch.setitem(sys.modules, "memory.llm_priority_queue", fake_llm)

    return dv


# =========================================================================
# auto_consult — direct tests at the bridge level
# =========================================================================

def test_auto_consult_skips_short_prompts(fresh_bridge):
    """Prompts with <6 words must skip — not enough context for surgeons."""
    result = fresh_bridge.auto_consult("add X feature")  # 3 words
    assert result["status"] == "skipped"
    assert result["summary"] == ""
    assert "too short" in result["reason"].lower()
    counters = fresh_bridge.get_auto_consult_counters()
    # Skip still counts as a call (visibility), but no success/error.
    assert counters["calls"] == 1
    assert counters["successes"] == 0
    assert counters["errors"] == 0


def test_auto_consult_calls_consult_for_creative_prompt(monkeypatch, fresh_bridge):
    """≥6 words → bridge dispatches to consult command via _call_with_fallback."""
    captured = {}

    def fake_call(command, topic, **kwargs):
        captured["command"] = command
        captured["topic"] = topic
        return {
            "status": "ok",
            "output": {
                "cardiologist_report": "Cardiologist says: design looks sound, watch error paths.",
                "neurologist_report": "Neurologist says: corrigibility-ok, no red flags.",
            },
            "path": "subprocess",
        }

    monkeypatch.setattr(fresh_bridge, "_call_with_fallback", fake_call)

    result = fresh_bridge.auto_consult(
        "let's add an auto-3s consult to the brainstorming skill"
    )
    assert captured["command"] == "consult"
    assert "auto-3s" in captured["topic"]
    assert result["status"] == "ok"
    assert "Cardiologist says" in result["summary"] or "Neurologist says" in result["summary"]
    counters = fresh_bridge.get_auto_consult_counters()
    assert counters["calls"] == 1
    assert counters["successes"] == 1
    assert counters["errors"] == 0


def test_auto_consult_handles_timeout_gracefully(monkeypatch, fresh_bridge):
    """Subprocess timeout from underlying _call_with_fallback → status=error, ZSF counter ++."""

    def fake_call(command, topic, **kwargs):
        return {"status": "error", "output": "timeout", "path": "subprocess"}

    monkeypatch.setattr(fresh_bridge, "_call_with_fallback", fake_call)

    result = fresh_bridge.auto_consult(
        "build a fleet status dashboard with NATS subscriptions"
    )
    assert result["status"] == "error"
    assert result["summary"] == ""
    # No raise. Counter incremented for timeouts specifically.
    counters = fresh_bridge.get_auto_consult_counters()
    assert counters["calls"] == 1
    assert counters["timeouts"] == 1
    assert counters["successes"] == 0


def test_auto_consult_handles_unexpected_raise(monkeypatch, fresh_bridge):
    """If _call_with_fallback raises, auto_consult MUST NOT propagate."""

    def fake_call(command, topic, **kwargs):
        raise RuntimeError("simulated bridge crash")

    monkeypatch.setattr(fresh_bridge, "_call_with_fallback", fake_call)

    # Must not raise — ZSF invariant.
    result = fresh_bridge.auto_consult(
        "create a new component for inbox triage workflow"
    )
    assert result["status"] == "error"
    assert result["summary"] == ""
    assert "simulated bridge crash" in result["reason"]
    counters = fresh_bridge.get_auto_consult_counters()
    assert counters["errors"] == 1


def test_auto_consult_handles_empty_output(monkeypatch, fresh_bridge):
    """Surgeons return ok but empty — degraded backend → counted as error."""

    def fake_call(command, topic, **kwargs):
        return {"status": "ok", "output": {}, "path": "subprocess"}

    monkeypatch.setattr(fresh_bridge, "_call_with_fallback", fake_call)

    result = fresh_bridge.auto_consult(
        "fix Y bug in webhook injection latency tail"
    )
    assert result["status"] == "error"
    assert "empty consult output" in result["reason"]
    counters = fresh_bridge.get_auto_consult_counters()
    assert counters["errors"] == 1


def test_auto_consult_summary_truncates_long_output(monkeypatch, fresh_bridge):
    """Verdict summary must be a single line ≤400 chars for [3S_VERDICT: …]."""
    long_text = "A" * 1000

    def fake_call(command, topic, **kwargs):
        return {
            "status": "ok",
            "output": {"summary": long_text + "\nmultiline\nsecond-line"},
            "path": "subprocess",
        }

    monkeypatch.setattr(fresh_bridge, "_call_with_fallback", fake_call)

    result = fresh_bridge.auto_consult(
        "design new pattern for cross-session evidence promotion"
    )
    assert result["status"] == "ok"
    assert len(result["summary"]) <= 400
    assert "\n" not in result["summary"]


# =========================================================================
# maybe_auto_consult_3s — gating tests at the synaptic_deep_voice level
# =========================================================================

def test_maybe_auto_consult_skipped_for_non_brainstorming(monkeypatch):
    """Read-only / writing-plans / None classification → no 3s call."""
    import memory.synaptic_deep_voice as dv
    import memory.surgery_bridge as sb

    called = {"n": 0}

    def spy(*args, **kwargs):
        called["n"] += 1
        return {"status": "ok", "summary": "should not be reached"}

    monkeypatch.setattr(sb, "auto_consult", spy)

    assert dv.maybe_auto_consult_3s("show fleet status", None) == ""
    assert dv.maybe_auto_consult_3s(
        "implement plan from spec.md", "writing-plans"
    ) == ""
    assert called["n"] == 0


def test_maybe_auto_consult_called_for_brainstorming(monkeypatch):
    """skill='brainstorming' → auto_consult invoked, verdict line returned."""
    import memory.synaptic_deep_voice as dv
    import memory.surgery_bridge as sb

    captured = {}

    def spy(prompt, depth="light", min_words=6, **kwargs):
        captured["prompt"] = prompt
        captured["depth"] = depth
        captured["min_words"] = min_words
        return {
            "status": "ok",
            "summary": "design ok, recommend test coverage on retry path",
            "depth": depth,
        }

    monkeypatch.setattr(sb, "auto_consult", spy)

    line = dv.maybe_auto_consult_3s(
        "let's add an auto-3s consult brainstorming integration",
        "brainstorming",
    )
    assert line.startswith("[3S_VERDICT:")
    assert "design ok" in line
    assert captured["depth"] == "light"
    assert captured["min_words"] == 6


def test_maybe_auto_consult_disabled_via_env(monkeypatch):
    """AUTO_3S_CONSULT_ENABLED=0 → no 3s call, empty line."""
    import memory.synaptic_deep_voice as dv
    import memory.surgery_bridge as sb

    monkeypatch.setenv("AUTO_3S_CONSULT_ENABLED", "0")
    called = {"n": 0}

    def spy(*args, **kwargs):
        called["n"] += 1
        return {"status": "ok", "summary": "x"}

    monkeypatch.setattr(sb, "auto_consult", spy)

    assert dv.maybe_auto_consult_3s(
        "build a brand new fleet dashboard component",
        "brainstorming",
    ) == ""
    assert called["n"] == 0


def test_maybe_auto_consult_swallows_skipped_status(monkeypatch):
    """auto_consult returns 'skipped' (short prompt) → no verdict line."""
    import memory.synaptic_deep_voice as dv
    import memory.surgery_bridge as sb

    def spy(*args, **kwargs):
        return {"status": "skipped", "summary": "", "reason": "prompt too short"}

    monkeypatch.setattr(sb, "auto_consult", spy)

    assert dv.maybe_auto_consult_3s("add X", "brainstorming") == ""


def test_maybe_auto_consult_swallows_errors(monkeypatch):
    """auto_consult error/timeout → no verdict line, no raise."""
    import memory.synaptic_deep_voice as dv
    import memory.surgery_bridge as sb

    def spy(*args, **kwargs):
        return {"status": "error", "summary": "", "reason": "timeout"}

    monkeypatch.setattr(sb, "auto_consult", spy)

    line = dv.maybe_auto_consult_3s(
        "build a new feature for atlas inbox triage",
        "brainstorming",
    )
    assert line == ""


def test_maybe_auto_consult_total_against_unexpected_raise(monkeypatch):
    """Defense in depth: even if auto_consult raises, we return ''."""
    import memory.synaptic_deep_voice as dv
    import memory.surgery_bridge as sb

    def explode(*args, **kwargs):
        raise RuntimeError("deeper bridge crash")

    monkeypatch.setattr(sb, "auto_consult", explode)

    # Must not raise — ZSF invariant.
    line = dv.maybe_auto_consult_3s(
        "design a multi-fleet coordinator with NATS pub/sub",
        "brainstorming",
    )
    assert line == ""


# =========================================================================
# generate_deep_s6 — end-to-end: verdict line lands in S6 output
# =========================================================================

def test_s6_emits_verdict_line_for_creative_prompt(monkeypatch, mocked_s6_pathway):
    """Full path: creative prompt → [SUGGESTED_SKILL: brainstorming] + [3S_VERDICT: …] + body."""
    import memory.surgery_bridge as sb

    def spy(prompt, depth="light", min_words=6, **kwargs):
        return {
            "status": "ok",
            "summary": "scope is bounded, proceed; add timeout in caller",
            "depth": depth,
        }

    monkeypatch.setattr(sb, "auto_consult", spy)

    out = mocked_s6_pathway.generate_deep_s6(
        "let's add an auto-3s consult brainstorming integration",
        session_id="s-1",
    )
    assert out is not None
    lines = out.splitlines()
    assert lines[0] == "[SUGGESTED_SKILL: brainstorming]"
    assert lines[1].startswith("[3S_VERDICT:")
    assert "scope is bounded" in lines[1]
    # LLM body still preserved below the headers.
    assert "deep-guidance-body-from-mocked-llm-call" in out


def test_s6_omits_verdict_line_for_read_only_prompt(monkeypatch, mocked_s6_pathway):
    """Read-only prompt → no skill line, no verdict line. auto_consult never called."""
    import memory.surgery_bridge as sb

    called = {"n": 0}

    def spy(*args, **kwargs):
        called["n"] += 1
        return {"status": "ok", "summary": "should not appear"}

    monkeypatch.setattr(sb, "auto_consult", spy)

    out = mocked_s6_pathway.generate_deep_s6("show fleet status", session_id="s-1")
    assert out is not None
    assert "[SUGGESTED_SKILL:" not in out
    assert "[3S_VERDICT:" not in out
    assert called["n"] == 0


def test_s6_omits_verdict_line_for_writing_plans_prompt(monkeypatch, mocked_s6_pathway):
    """writing-plans skill route → skill line emitted, but NO 3s verdict."""
    import memory.surgery_bridge as sb

    called = {"n": 0}

    def spy(*args, **kwargs):
        called["n"] += 1
        return {"status": "ok", "summary": "should not appear"}

    monkeypatch.setattr(sb, "auto_consult", spy)

    out = mocked_s6_pathway.generate_deep_s6(
        "implement plan from spec.md",
        session_id="s-1",
    )
    assert out is not None
    assert out.startswith("[SUGGESTED_SKILL: writing-plans]")
    assert "[3S_VERDICT:" not in out
    assert called["n"] == 0


def test_s6_3s_failure_does_not_block_s6(monkeypatch, mocked_s6_pathway):
    """3s consult error MUST NOT block S6 generation. Skill line still emitted."""
    import memory.surgery_bridge as sb

    def spy(*args, **kwargs):
        return {"status": "error", "summary": "", "reason": "timeout"}

    monkeypatch.setattr(sb, "auto_consult", spy)

    out = mocked_s6_pathway.generate_deep_s6(
        "build a new fleet dashboard component for race AB1",
        session_id="s-1",
    )
    assert out is not None
    # Skill line still present (V2 contract preserved).
    assert out.startswith("[SUGGESTED_SKILL: brainstorming]")
    # No verdict line because 3s failed.
    assert "[3S_VERDICT:" not in out
    # LLM body still flowed.
    assert "deep-guidance-body-from-mocked-llm-call" in out


def test_prepend_skill_line_idempotent_with_verdict(mocked_s6_pathway):
    """Re-prepend must not stack [SUGGESTED_SKILL:] or [3S_VERDICT:] headers."""
    body_with_headers = (
        "[SUGGESTED_SKILL: brainstorming]\n"
        "[3S_VERDICT: stale verdict]\n"
        "stale-body-text"
    )
    out = mocked_s6_pathway._prepend_skill_line(
        body_with_headers,
        "[SUGGESTED_SKILL: brainstorming]",
        "[3S_VERDICT: fresh verdict]",
    )
    # Exactly one of each header.
    assert out.count("[SUGGESTED_SKILL:") == 1
    assert out.count("[3S_VERDICT:") == 1
    assert "fresh verdict" in out
    assert "stale verdict" not in out
    assert "stale-body-text" in out


def test_prepend_skill_line_empty_verdict_only_emits_skill(mocked_s6_pathway):
    """Empty verdict_line → only skill line, no [3S_VERDICT:] header."""
    out = mocked_s6_pathway._prepend_skill_line(
        "body-text", "[SUGGESTED_SKILL: brainstorming]", ""
    )
    assert out.startswith("[SUGGESTED_SKILL: brainstorming]")
    assert "[3S_VERDICT:" not in out
    assert "body-text" in out
