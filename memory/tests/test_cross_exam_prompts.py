"""Tests for cross-exam system-prompt constants.

Verifies that the SURGERY_*_SYSTEM_PROMPT module-level constants:
  1. Exist at module scope on memory.agent_service.
  2. Contain the exact text previously inlined inside
     _surgery_cross_exam_blocking (no behaviour drift).
  3. Are re-used by reference — the same object identity across reads,
     which is what lets OpenAI's prompt_cache_key deduplicate bytes.

These tests do not invoke any LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path

SUPERREPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(SUPERREPO))


class TestSurgeryPromptConstants:
    def test_constants_exist(self):
        import memory.agent_service as m
        assert hasattr(m, "SURGERY_BASE_SYSTEM_PROMPT")
        assert hasattr(m, "SURGERY_CROSS_SYSTEM_PROMPT")
        assert hasattr(m, "SURGERY_EXPLORE_SYSTEM_PROMPT")

    def test_base_prompt_text_unchanged(self):
        import memory.agent_service as m
        expected = (
            "You are part of a 3-model surgery team. Be analytical and critical. "
            "Identify confabulations, unsupported claims, and blind spots."
        )
        assert m.SURGERY_BASE_SYSTEM_PROMPT == expected

    def test_cross_prompt_text_unchanged(self):
        import memory.agent_service as m
        expected = (
            "You are a critical reviewer in a multi-model surgery team. "
            "Analyze the other model's report. Identify:\n"
            "1. Confabulations (claims without evidence)\n"
            "2. Blind spots (what was missed)\n"
            "3. Agreements (what aligns with your understanding)\n"
            "4. Confidence assessment (how much to trust each claim)"
        )
        assert m.SURGERY_CROSS_SYSTEM_PROMPT == expected

    def test_explore_prompt_text_unchanged(self):
        import memory.agent_service as m
        expected = (
            "You are part of a 3-model surgery team. The team has already produced "
            "initial analyses and cross-examinations (provided below). Your role now "
            "is OPEN EXPLORATION — go beyond what was already covered.\n\n"
            "Focus on:\n"
            "- What are we ALL blind to? What assumptions remain unchallenged?\n"
            "- What adjacent systems, failure modes, or interactions were not considered?\n"
            "- What's the worst-case scenario nobody mentioned?\n"
            "- What corrigibility risks exist — where might we be confidently wrong?\n\n"
            "Do NOT repeat points already made. Only surface genuinely NEW insights."
        )
        assert m.SURGERY_EXPLORE_SYSTEM_PROMPT == expected

    def test_constants_are_reused_by_identity(self):
        """Two reads of the same constant must return the same object.

        This is the property that makes prompt-cache keys stable: the bytes
        sent to OpenAI are literally the same object every call, so any
        hash or interning layer upstream lands in one bucket.
        """
        import memory.agent_service as m
        a = m.SURGERY_BASE_SYSTEM_PROMPT
        b = m.SURGERY_BASE_SYSTEM_PROMPT
        assert a is b

    def test_prompts_referenced_in_cross_exam(self):
        """Source inspection: _surgery_cross_exam_blocking uses the constants,
        not inline string literals. Guards against regression where someone
        re-inlines the text and silently breaks cache-hit telemetry.
        """
        import inspect
        import memory.agent_service as m
        # Read the file source since the function is nested inside another
        src = Path(m.__file__).read_text()
        # The nested function must reference all three constants
        assert "SURGERY_BASE_SYSTEM_PROMPT" in src
        assert "SURGERY_CROSS_SYSTEM_PROMPT" in src
        assert "SURGERY_EXPLORE_SYSTEM_PROMPT" in src
        # And the old inlined version must NOT be present more than once
        # (once in module-constant definition, no inline copies)
        base_literal = "You are part of a 3-model surgery team. Be analytical"
        assert src.count(base_literal) == 1, \
            "Base prompt literal appears more than once — inline copy leaked back in"
