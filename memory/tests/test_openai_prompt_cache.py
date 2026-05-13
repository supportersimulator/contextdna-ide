"""Tests for OpenAI prompt_cache_key on the external-fallback path.

Verifies that:
  1. _openai_prompt_cache_key produces a stable 16-char hex key.
  2. The same system prompt + profile yields the same key across calls.
  3. Different system prompts or profiles yield different keys.
  4. _call_openai_fallback forwards the derived key to gateway.infer as
     ``prompt_cache_key`` so OpenAI groups repeated calls into one cache bucket.
  5. OpenAIProvider.infer passes prompt_cache_key into chat.completions.create.

These tests mock the gateway / OpenAI client so no real API calls happen.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make memory/ and context-dna/ importable
SUPERREPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(SUPERREPO))
sys.path.insert(0, str(SUPERREPO / "context-dna"))


# ---------------------------------------------------------------------------
# _openai_prompt_cache_key — pure helper
# ---------------------------------------------------------------------------

class TestPromptCacheKey:
    def test_stable_and_hex_16(self):
        from memory.llm_priority_queue import _openai_prompt_cache_key
        k1 = _openai_prompt_cache_key("You are a helpful assistant.", "classify")
        k2 = _openai_prompt_cache_key("You are a helpful assistant.", "classify")
        assert k1 == k2
        assert len(k1) == 16
        # hex chars only
        int(k1, 16)

    def test_different_system_prompt_changes_key(self):
        from memory.llm_priority_queue import _openai_prompt_cache_key
        a = _openai_prompt_cache_key("prompt A", "classify")
        b = _openai_prompt_cache_key("prompt B", "classify")
        assert a != b

    def test_different_profile_changes_key(self):
        from memory.llm_priority_queue import _openai_prompt_cache_key
        a = _openai_prompt_cache_key("same prompt", "classify")
        b = _openai_prompt_cache_key("same prompt", "deep")
        assert a != b


# ---------------------------------------------------------------------------
# _call_openai_fallback — forwards cache key to gateway.infer
# ---------------------------------------------------------------------------

class TestCallOpenAIFallbackForwardsCacheKey:
    def test_cache_key_passed_to_gateway(self):
        """_call_openai_fallback must pass prompt_cache_key to gateway.infer."""
        import memory.llm_priority_queue as mod

        # Build a fake gateway whose .infer returns a usable LLMResponse-ish object
        fake_response = MagicMock()
        fake_response.error = None
        fake_response.content = "hello"
        fake_response.cost_usd = 0.0
        fake_response.latency_ms = 42
        fake_response.model_id = "gpt-4o-mini"

        fake_gateway = MagicMock()
        fake_gateway.infer.return_value = fake_response
        # Needed by the static_fallback_message comparison
        fake_gateway.config.static_fallback_message = "__never_equal__"

        # Patch get_gateway where it is looked up (inside the function body, via sys.path)
        with patch.dict(sys.modules):  # isolate sys.modules mutations
            import local_llm.llm_gateway as gw_mod
            with patch.object(gw_mod, "get_gateway", return_value=fake_gateway):
                result = mod._call_openai_fallback(
                    system_prompt="You are Atlas.",
                    user_prompt="Summarise.",
                    profile="classify",
                    caller="unit_test",
                )

        assert result is not None
        assert result["content"] == "hello"

        # Verify cache key was forwarded
        kwargs = fake_gateway.infer.call_args.kwargs
        assert "prompt_cache_key" in kwargs, \
            "prompt_cache_key must be passed to gateway.infer"

        expected = mod._openai_prompt_cache_key("You are Atlas.", "classify")
        assert kwargs["prompt_cache_key"] == expected


# ---------------------------------------------------------------------------
# OpenAIProvider.infer — forwards key into chat.completions.create
# ---------------------------------------------------------------------------

class TestOpenAIProviderForwardsCacheKey:
    def _build_provider(self):
        from local_llm.providers.openai_provider import OpenAIProvider
        from local_llm.providers.base import ProviderConfig

        cfg = ProviderConfig(
            provider_type="openai",
            model_id="gpt-4o-mini",
            api_key="sk-test-" + "x" * 20,
            max_tokens=100,
            temperature=0.7,
        )
        return OpenAIProvider(cfg)

    def test_cache_key_forwarded_to_openai_create(self):
        provider = self._build_provider()

        # Build a fake OpenAI client whose chat.completions.create records kwargs
        fake_client = MagicMock()
        fake_message = MagicMock()
        fake_message.content = "ok"
        fake_choice = MagicMock()
        fake_choice.message = fake_message
        fake_usage = MagicMock()
        fake_usage.prompt_tokens = 10
        fake_usage.completion_tokens = 5
        fake_resp = MagicMock()
        fake_resp.choices = [fake_choice]
        fake_resp.usage = fake_usage
        fake_resp.id = "resp-1"
        fake_client.chat.completions.create.return_value = fake_resp

        with patch.object(provider, "_get_client", return_value=fake_client):
            resp = provider.infer(
                prompt="hi",
                system="You are Atlas.",
                prompt_cache_key="abc123",
            )

        assert resp.content == "ok"
        kwargs = fake_client.chat.completions.create.call_args.kwargs
        assert kwargs.get("prompt_cache_key") == "abc123"

    def test_cache_key_omitted_when_not_supplied(self):
        """No prompt_cache_key → param is absent (don't break old SDKs)."""
        provider = self._build_provider()

        fake_client = MagicMock()
        fake_message = MagicMock()
        fake_message.content = "ok"
        fake_choice = MagicMock()
        fake_choice.message = fake_message
        fake_usage = MagicMock()
        fake_usage.prompt_tokens = 10
        fake_usage.completion_tokens = 5
        fake_resp = MagicMock()
        fake_resp.choices = [fake_choice]
        fake_resp.usage = fake_usage
        fake_resp.id = "resp-1"
        fake_client.chat.completions.create.return_value = fake_resp

        with patch.object(provider, "_get_client", return_value=fake_client):
            provider.infer(prompt="hi", system="sys")

        kwargs = fake_client.chat.completions.create.call_args.kwargs
        assert "prompt_cache_key" not in kwargs
