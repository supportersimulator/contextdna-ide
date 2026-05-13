"""Tests for LLM priority queue migration — agent-ds-callers.

Verifies that:
  1. memory.chain_segments_audit._call_llm routes via llm_priority_queue.llm_generate
  2. memory.recovery_agent.LLMAdvisor._query_openai routes via llm_priority_queue.llm_generate

These tests mock llm_priority_queue.llm_generate so they never touch
the real LLM, DeepSeek, or OpenAI APIs.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# chain_segments_audit._call_llm
# ---------------------------------------------------------------------------

class TestChainSegmentsAuditCallLlm:
    def test_call_llm_routes_via_priority_queue(self):
        """_call_llm must delegate to llm_priority_queue.llm_generate."""
        import memory.chain_segments_audit as mod

        with patch("memory.llm_priority_queue.llm_generate") as mock_gen:
            mock_gen.return_value = '{"features": []}'

            result = mod._call_llm(ctx=None, prompt="extract features please")

            assert result == '{"features": []}'
            assert mock_gen.called, "llm_generate must be called"

            # Keyword args — verify the shape
            kwargs = mock_gen.call_args.kwargs
            assert kwargs.get("user_prompt") == "extract features please"
            # Priority should be BACKGROUND (P4) — background batch work
            from memory.llm_priority_queue import Priority
            assert kwargs.get("priority") == Priority.BACKGROUND
            # Caller identifier present for telemetry
            assert "chain_segments_audit" in kwargs.get("caller", "")

    def test_call_llm_raises_on_empty_result(self):
        """Empty/None result from priority queue must raise RuntimeError."""
        import memory.chain_segments_audit as mod

        with patch("memory.llm_priority_queue.llm_generate") as mock_gen:
            mock_gen.return_value = None

            with pytest.raises(RuntimeError):
                mod._call_llm(ctx=None, prompt="anything")

    def test_call_llm_no_direct_openai_or_deepseek(self):
        """After migration, urllib must not be invoked for OpenAI/DeepSeek."""
        import memory.chain_segments_audit as mod

        with patch("memory.llm_priority_queue.llm_generate") as mock_gen, \
                patch("urllib.request.urlopen") as mock_urlopen:
            mock_gen.return_value = "{}"

            mod._call_llm(ctx=None, prompt="x")

            # Priority queue was used; urllib.request.urlopen was NOT invoked
            # by the audit module itself (the queue may use other transports).
            assert mock_gen.called
            assert not mock_urlopen.called


# ---------------------------------------------------------------------------
# recovery_agent.LLMAdvisor._query_openai
# ---------------------------------------------------------------------------

class TestRecoveryAgentQueryOpenAi:
    def test_query_openai_routes_via_priority_queue(self):
        """LLMAdvisor._query_openai must delegate to llm_generate."""
        from memory.recovery_agent import LLMAdvisor, LLMConfig, LLMProvider

        cfg = LLMConfig(
            provider=LLMProvider.OPENAI,
            openai_api_key="sk-test-fake-key",
        )
        advisor = LLMAdvisor(cfg)

        with patch("memory.llm_priority_queue.llm_generate") as mock_gen:
            mock_gen.return_value = '{"analysis": "docker stopped", "confidence": 0.8}'

            result = advisor._query_openai("diagnose docker failure")

            assert result == '{"analysis": "docker stopped", "confidence": 0.8}'
            assert mock_gen.called

            kwargs = mock_gen.call_args.kwargs
            assert kwargs.get("user_prompt") == "diagnose docker failure"

            from memory.llm_priority_queue import Priority
            assert kwargs.get("priority") == Priority.BACKGROUND
            assert "recovery_agent" in kwargs.get("caller", "")

    def test_query_openai_no_direct_http(self):
        """After migration, _query_openai must not open a connection to api.openai.com."""
        from memory.recovery_agent import LLMAdvisor, LLMConfig, LLMProvider

        cfg = LLMConfig(
            provider=LLMProvider.OPENAI,
            openai_api_key="sk-test-fake-key",
        )
        advisor = LLMAdvisor(cfg)

        with patch("memory.llm_priority_queue.llm_generate") as mock_gen, \
                patch("urllib.request.urlopen") as mock_urlopen:
            mock_gen.return_value = "ok"

            advisor._query_openai("prompt")

            assert mock_gen.called
            assert not mock_urlopen.called, "urlopen must not be invoked after migration"

    def test_query_openai_returns_none_on_queue_failure(self):
        """If priority queue returns None, the advisor returns None (not crash)."""
        from memory.recovery_agent import LLMAdvisor, LLMConfig, LLMProvider

        cfg = LLMConfig(
            provider=LLMProvider.OPENAI,
            openai_api_key="sk-test-fake-key",
        )
        advisor = LLMAdvisor(cfg)

        with patch("memory.llm_priority_queue.llm_generate") as mock_gen:
            mock_gen.return_value = None

            result = advisor._query_openai("prompt")

            assert result is None
