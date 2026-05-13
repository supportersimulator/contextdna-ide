"""
RACE V2 — Tests for S6 superpowers skill wiring.

Verifies that `generate_deep_s6` (and the underlying classifier) emits
a structured `[SUGGESTED_SKILL: <skill>]` pointer line at the top of S6
output for the right task classes, and stays silent for read-only queries.

Per docs/reflect/2026-04-26-cross-cutting-priorities.md Priority #3:
- creative / new feature / behavior-change prompts -> brainstorming
- multi-step / spec-driven prompts                 -> writing-plans
- read-only / status / diagnostic prompts          -> no suggestion

The classifier itself is pure (no I/O), so we test it directly. We also
exercise `generate_deep_s6` end-to-end with the LLM mocked — that proves
the pointer line is actually surfaced through the live path, not just
the helper.
"""

import sys
import types

import pytest


# =========================================================================
# Direct classifier tests — pure, no fixtures needed
# =========================================================================

def test_classify_brainstorming_lets_add_feature():
    from memory.synaptic_deep_voice import classify_task_for_skill
    assert classify_task_for_skill("let's add X feature") == "brainstorming"


def test_classify_brainstorming_build_verb():
    from memory.synaptic_deep_voice import classify_task_for_skill
    assert classify_task_for_skill("build a fleet status dashboard") == "brainstorming"


def test_classify_brainstorming_fix_verb():
    from memory.synaptic_deep_voice import classify_task_for_skill
    assert classify_task_for_skill("fix Y bug in webhook injection") == "brainstorming"


def test_classify_brainstorming_modify_behavior():
    from memory.synaptic_deep_voice import classify_task_for_skill
    assert (
        classify_task_for_skill("modify behavior of S2 cache eviction")
        == "brainstorming"
    )


def test_classify_writing_plans_implement_from_spec():
    from memory.synaptic_deep_voice import classify_task_for_skill
    # "implement" is a creative verb but presence of "spec.md" + "plan"
    # routes this to writing-plans (plan signal beats creative signal).
    assert (
        classify_task_for_skill("implement plan from spec.md")
        == "writing-plans"
    )


def test_classify_writing_plans_multistep_requirements():
    from memory.synaptic_deep_voice import classify_task_for_skill
    assert (
        classify_task_for_skill("execute multi-step requirements for race v2")
        == "writing-plans"
    )


def test_classify_writing_plans_phase_milestone():
    from memory.synaptic_deep_voice import classify_task_for_skill
    assert (
        classify_task_for_skill("walk through phase 2 milestones in the roadmap")
        == "writing-plans"
    )


def test_classify_read_only_show_fleet_status():
    from memory.synaptic_deep_voice import classify_task_for_skill
    assert classify_task_for_skill("show fleet status") is None


def test_classify_read_only_what_is():
    from memory.synaptic_deep_voice import classify_task_for_skill
    assert classify_task_for_skill("what is the current webhook latency") is None


def test_classify_read_only_check_health():
    from memory.synaptic_deep_voice import classify_task_for_skill
    assert classify_task_for_skill("check health of NATS daemon") is None


def test_classify_empty_prompt():
    from memory.synaptic_deep_voice import classify_task_for_skill
    assert classify_task_for_skill("") is None
    assert classify_task_for_skill("   ") is None
    assert classify_task_for_skill(None) is None  # type: ignore[arg-type]


def test_classify_unknown_prompt_returns_none():
    from memory.synaptic_deep_voice import classify_task_for_skill
    # Prose with no creative/plan/read-only verbs at all.
    assert classify_task_for_skill("the sky is blue today") is None


# =========================================================================
# format_skill_suggestion — pure formatter
# =========================================================================

def test_format_skill_suggestion_brainstorming():
    from memory.synaptic_deep_voice import format_skill_suggestion
    assert (
        format_skill_suggestion("brainstorming")
        == "[SUGGESTED_SKILL: brainstorming]"
    )


def test_format_skill_suggestion_writing_plans():
    from memory.synaptic_deep_voice import format_skill_suggestion
    assert (
        format_skill_suggestion("writing-plans")
        == "[SUGGESTED_SKILL: writing-plans]"
    )


def test_format_skill_suggestion_none():
    from memory.synaptic_deep_voice import format_skill_suggestion
    assert format_skill_suggestion(None) == ""
    assert format_skill_suggestion("") == ""


# =========================================================================
# Integration: generate_deep_s6 prepends the pointer line
# =========================================================================
# We mock out everything S6 touches except the classifier:
#   - context helpers return non-empty stubs (so context_parts is truthy)
#   - llm_generate returns a fixed body
#   - the Redis cache is disabled via env so we hit the LLM path
#
# This verifies the wiring threads classifier -> S6 output end-to-end.

@pytest.fixture
def mocked_s6(monkeypatch):
    # Disable cache to force the LLM path deterministically.
    monkeypatch.setenv("S6_CACHE_ENABLED", "0")

    import memory.synaptic_deep_voice as dv

    monkeypatch.setattr(dv, "_get_personality_context", lambda: "stub-personality")
    monkeypatch.setattr(dv, "_get_pattern_context_for_task", lambda p: "stub-patterns")
    monkeypatch.setattr(dv, "_get_evolution_context", lambda: "stub-evo")
    monkeypatch.setattr(dv, "_get_wisdom_for_task", lambda p: "stub-wisdom")

    # Replace the LLM with a deterministic body — we only care that the
    # skill line lands ABOVE this body in the returned string.
    fake_llm = types.ModuleType("memory.llm_priority_queue")

    class Priority:
        AARON = "AARON"
        ATLAS = "ATLAS"
        EXTERNAL = "EXTERNAL"
        BACKGROUND = "BACKGROUND"

    def llm_generate(**kwargs):
        # Must be >20 chars — generate_deep_s6 drops shorter results.
        return "deep-guidance-body-from-mocked-llm-call"

    fake_llm.Priority = Priority
    fake_llm.llm_generate = llm_generate
    monkeypatch.setitem(sys.modules, "memory.llm_priority_queue", fake_llm)

    return dv


def test_s6_prepends_brainstorming_for_creative_prompt(mocked_s6):
    out = mocked_s6.generate_deep_s6("let's add X feature", session_id="sess-1")
    assert out is not None
    assert out.startswith("[SUGGESTED_SKILL: brainstorming]")
    # Body still present after the pointer line.
    assert "deep-guidance-body-from-mocked-llm-call" in out


def test_s6_prepends_writing_plans_for_spec_prompt(mocked_s6):
    out = mocked_s6.generate_deep_s6(
        "implement plan from spec.md", session_id="sess-1"
    )
    assert out is not None
    assert out.startswith("[SUGGESTED_SKILL: writing-plans]")
    assert "deep-guidance-body-from-mocked-llm-call" in out


def test_s6_omits_skill_line_for_read_only_prompt(mocked_s6):
    out = mocked_s6.generate_deep_s6("show fleet status", session_id="sess-1")
    assert out is not None
    assert "[SUGGESTED_SKILL:" not in out
    assert "deep-guidance-body-from-mocked-llm-call" in out


def test_s6_skill_line_is_idempotent_when_body_already_has_one(mocked_s6):
    """Re-prepending must not stack multiple [SUGGESTED_SKILL: …] headers."""
    body_with_header = "[SUGGESTED_SKILL: brainstorming]\nstale-body-text"
    out = mocked_s6._prepend_skill_line(
        body_with_header, "[SUGGESTED_SKILL: writing-plans]"
    )
    # Exactly one [SUGGESTED_SKILL: header, and it's the new one.
    assert out.count("[SUGGESTED_SKILL:") == 1
    assert out.startswith("[SUGGESTED_SKILL: writing-plans]")
    assert "stale-body-text" in out


def test_s6_prepend_with_empty_skill_returns_body_unchanged(mocked_s6):
    body = "deep-guidance-body-from-mocked-llm-call"
    assert mocked_s6._prepend_skill_line(body, "") == body
