"""Tests — OpenAI is optional, DeepSeek is primary (cutover 2026-04-18).

These tests pin the new behaviour for three scenarios:

1. OpenAI Python package is missing → _external_fallback still succeeds
   via DeepSeek and _call_openai_fallback returns None (no ImportError).

2. Context_DNA_OPENAI absent, Context_DNA_Deep_Seek present → A/B framework
   and surgery-team remote query auto-select DeepSeek without raising.

3. Both keys absent → code degrades gracefully (soft failure with clear
   error string), never a raw stack trace.

All external network calls are mocked. No real API keys required.
"""
from __future__ import annotations

import builtins
import importlib
import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

# Ensure repo root is on sys.path when pytest runs from unusual cwd
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from memory import llm_priority_queue as lpq  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _no_redis_cache():
    """Patch redis.Redis so .get returns None (cache miss) and .setex no-ops."""
    fake = MagicMock()
    fake.get.return_value = None
    fake.setex.return_value = True
    with patch("redis.Redis", return_value=fake, create=True):
        yield fake


@contextmanager
def _scrub_env(*names):
    """Temporarily remove env vars and restore them at exit."""
    saved = {n: os.environ.pop(n, None) for n in names}
    try:
        yield
    finally:
        for n, v in saved.items():
            if v is not None:
                os.environ[n] = v


def _fake_ds_result(content: str = "deepseek-optional-ok"):
    return {
        "content": content,
        "cost_estimate": 0.000123,
        "latency_ms": 42,
        "model": "deepseek-chat",
    }


@contextmanager
def _block_openai_import():
    """Simulate the openai package NOT being installed.

    Any `import openai` (or `from openai import ...`) raises ImportError
    inside this context. Also clears the cached module so the import
    machinery goes through __import__ again.
    """
    orig_import = builtins.__import__
    saved_mod = sys.modules.pop("openai", None)

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "openai" or name.startswith("openai."):
            raise ImportError("openai not installed (simulated)")
        return orig_import(name, globals, locals, fromlist, level)

    with patch.object(builtins, "__import__", fake_import):
        try:
            yield
        finally:
            if saved_mod is not None:
                sys.modules["openai"] = saved_mod


# ---------------------------------------------------------------------------
# 1. Missing openai package → external_fallback still works via DeepSeek
# ---------------------------------------------------------------------------

class ExternalFallbackWithoutOpenAITests(unittest.TestCase):
    """DeepSeek succeeds even when the openai SDK isn't importable."""

    def setUp(self):
        self._saved_provider = os.environ.pop(lpq._EXTERNAL_PROVIDER_ENV, None)

    def tearDown(self):
        if self._saved_provider is not None:
            os.environ[lpq._EXTERNAL_PROVIDER_ENV] = self._saved_provider
        else:
            os.environ.pop(lpq._EXTERNAL_PROVIDER_ENV, None)

    def test_deepseek_only_mode_no_openai_package(self):
        os.environ[lpq._EXTERNAL_PROVIDER_ENV] = "deepseek"

        with _block_openai_import(), \
             patch.object(lpq, "_call_deepseek", return_value=_fake_ds_result()) as m_ds, \
             patch.object(lpq, "_call_openai_fallback") as m_oai, \
             patch.object(lpq, "_track_external_cost"), \
             patch.object(lpq, "_log_fallback_event"), \
             _no_redis_cache():
            out = lpq._external_fallback("sys", "user", "deep", caller="no-openai")

        self.assertEqual(out, "deepseek-optional-ok")
        m_ds.assert_called_once()
        m_oai.assert_not_called()

    def test_deepseek_first_openai_fallback_skips_openai_on_ds_success(self):
        """Default mode — DS success means OpenAI never gets called at all,
        so a missing openai package has zero blast radius."""
        os.environ[lpq._EXTERNAL_PROVIDER_ENV] = "deepseek-first-openai-fallback"

        with _block_openai_import(), \
             patch.object(lpq, "_call_deepseek", return_value=_fake_ds_result()), \
             patch.object(lpq, "_call_openai_fallback") as m_oai, \
             patch.object(lpq, "_track_external_cost"), \
             patch.object(lpq, "_log_fallback_event"), \
             _no_redis_cache():
            out = lpq._external_fallback("sys", "user", "deep", caller="default-mode")

        self.assertEqual(out, "deepseek-optional-ok")
        m_oai.assert_not_called()

    def test_call_openai_fallback_returns_none_when_sdk_missing(self):
        """Direct test: _call_openai_fallback itself must not raise when the
        openai SDK is absent — it returns None so _external_fallback can
        log + return None to the caller cleanly."""
        with _block_openai_import(), _no_redis_cache():
            out = lpq._call_openai_fallback("sys", "user", "deep", caller="sdk-missing")
        self.assertIsNone(out)


# ---------------------------------------------------------------------------
# 2. Missing OPENAI key + present DEEPSEEK key → auto-selects DeepSeek
# ---------------------------------------------------------------------------

class AbAutonomousProviderAutoSelectTests(unittest.TestCase):
    """memory.ab_autonomous._resolve_llm_provider must pick DeepSeek when
    LLM_PROVIDER is unset and a DeepSeek key is present."""

    def test_deepseek_key_only_selects_deepseek(self):
        scrub = ("LLM_PROVIDER", "Context_DNA_OPENAI", "OPENAI_API_KEY")
        with _scrub_env(*scrub), \
             patch.dict(os.environ, {"Context_DNA_Deep_Seek": "ds-test-key-123"}):
            import memory.ab_autonomous as ab
            importlib.reload(ab)
            cfg = ab._resolve_llm_provider()
        self.assertEqual(cfg["provider"], "deepseek")
        self.assertEqual(cfg["model"], "deepseek-chat")
        self.assertEqual(cfg["api_key"], "ds-test-key-123")
        self.assertEqual(cfg["base_url"], "https://api.deepseek.com/v1")

    def test_openai_key_only_falls_back_to_openai(self):
        """If user configured only OpenAI (legacy), we still return an
        OpenAI config rather than failing."""
        scrub = ("LLM_PROVIDER", "Context_DNA_Deep_Seek", "Context_DNA_Deepseek",
                 "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
        with _scrub_env(*scrub), \
             patch.dict(os.environ, {"Context_DNA_OPENAI": "sk-test-openai"}), \
             patch("subprocess.run") as m_sp:
            # Prevent keychain lookup from finding a DS key on a dev box
            m_sp.return_value = MagicMock(returncode=1, stdout="", stderr="")
            import memory.ab_autonomous as ab
            importlib.reload(ab)
            cfg = ab._resolve_llm_provider()
        self.assertEqual(cfg["provider"], "openai")
        self.assertEqual(cfg["model"], "gpt-4.1-mini")
        self.assertEqual(cfg["api_key"], "sk-test-openai")


# ---------------------------------------------------------------------------
# 3. Both keys absent → graceful failure (no stack trace, clear message)
# ---------------------------------------------------------------------------

class GracefulFailureTests(unittest.TestCase):
    """With no external LLM keys at all, callers get a clean soft-fail."""

    def test_resolve_provider_returns_empty_key_not_raises(self):
        scrub = (
            "LLM_PROVIDER", "Context_DNA_Deep_Seek", "Context_DNA_Deepseek",
            "DEEPSEEK_API_KEY", "Context_DNA_OPENAI", "OPENAI_API_KEY",
        )
        with _scrub_env(*scrub), patch("subprocess.run") as m_sp:
            m_sp.return_value = MagicMock(returncode=1, stdout="", stderr="")
            import memory.ab_autonomous as ab
            importlib.reload(ab)
            cfg = ab._resolve_llm_provider()
        # Does not raise; api_key is empty so callers skip cleanly
        self.assertEqual(cfg["api_key"], "")
        # Still returns a valid provider config (DeepSeek shape, ready to
        # work as soon as a key is added)
        self.assertEqual(cfg["provider"], "deepseek")

    def test_get_openai_client_returns_none_when_sdk_missing(self):
        import memory.ab_autonomous as ab
        importlib.reload(ab)
        with _block_openai_import():
            client = ab._get_openai_client(api_key="whatever", base_url=None)
        self.assertIsNone(client)

    def test_external_fallback_logs_and_returns_none_on_total_failure(self):
        """DS missing SDK + OpenAI missing SDK + keys missing = None, not
        a raw exception. Caller logs the soft failure string."""
        os.environ[lpq._EXTERNAL_PROVIDER_ENV] = "deepseek-first-openai-fallback"
        try:
            with _block_openai_import(), \
                 patch.object(lpq, "_load_deepseek_api_key", return_value=None), \
                 patch.object(lpq, "_track_external_cost"), \
                 patch.object(lpq, "_log_fallback_event"), \
                 _no_redis_cache():
                out = lpq._external_fallback("sys", "user", "deep", caller="total-fail")
            self.assertIsNone(out)
        finally:
            os.environ.pop(lpq._EXTERNAL_PROVIDER_ENV, None)


if __name__ == "__main__":
    unittest.main()
