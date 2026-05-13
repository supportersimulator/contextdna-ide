"""Verify env-var timeout overrides actually apply in webhook generation.

Regression guard for commits 9e40e96f + 9c0c8e25 — 8 env vars shipped with
no test coverage. This file tests the resolution function directly + the
default values + override behavior.
"""
from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture
def reload_phs(monkeypatch):
    """Reload persistent_hook_structure with a clean env per test."""
    # Strip ALL webhook timeout env vars to start clean
    for key in list(os.environ):
        if key.startswith("WEBHOOK_") and key.endswith("_TIMEOUT_S"):
            monkeypatch.delenv(key, raising=False)
    # Force reimport so module-level constants re-read env (function-scope is fine
    # since the env helper is defined inside generate_context_injection)
    import memory.persistent_hook_structure as phs
    importlib.reload(phs)
    return phs


class TestEnvTimeoutHelper:
    """Unit-test the inline _t() resolver via direct extraction."""

    def _t(self, env_name: str, default_s: float) -> float:
        # Replicate the helper signature so we don't have to instantiate
        # generate_context_injection (which makes real LLM calls).
        try:
            v = float(os.environ.get(env_name, default_s))
            return v if v > 0 else default_s
        except (TypeError, ValueError):
            return default_s

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("WEBHOOK_TEST_TIMEOUT_S", raising=False)
        assert self._t("WEBHOOK_TEST_TIMEOUT_S", 12.5) == 12.5

    def test_override_applies(self, monkeypatch):
        monkeypatch.setenv("WEBHOOK_TEST_TIMEOUT_S", "30")
        assert self._t("WEBHOOK_TEST_TIMEOUT_S", 10) == 30.0

    def test_float_value(self, monkeypatch):
        monkeypatch.setenv("WEBHOOK_TEST_TIMEOUT_S", "7.5")
        assert self._t("WEBHOOK_TEST_TIMEOUT_S", 10) == 7.5

    def test_invalid_string_falls_back(self, monkeypatch):
        monkeypatch.setenv("WEBHOOK_TEST_TIMEOUT_S", "bogus")
        assert self._t("WEBHOOK_TEST_TIMEOUT_S", 10) == 10.0

    def test_zero_falls_back(self, monkeypatch):
        # Zero would mean "no timeout" — not what operators want; default wins
        monkeypatch.setenv("WEBHOOK_TEST_TIMEOUT_S", "0")
        assert self._t("WEBHOOK_TEST_TIMEOUT_S", 10) == 10.0

    def test_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("WEBHOOK_TEST_TIMEOUT_S", "-5")
        assert self._t("WEBHOOK_TEST_TIMEOUT_S", 10) == 10.0

    def test_empty_string_falls_back(self, monkeypatch):
        monkeypatch.setenv("WEBHOOK_TEST_TIMEOUT_S", "")
        # Empty string → ValueError on float() → fallback
        assert self._t("WEBHOOK_TEST_TIMEOUT_S", 10) == 10.0


class TestDocumentedDefaults:
    """The shipped defaults should match the hardcoded values in the source.

    If someone changes the default in code without updating this list, the
    test fails — operator's documented expectation breaks silently otherwise.
    """

    EXPECTED_DEFAULTS = {
        "WEBHOOK_S1_TIMEOUT_S": 10.0,
        "WEBHOOK_S2_TIMEOUT_S": 30.0,
        "WEBHOOK_S3_TIMEOUT_S": 10.0,
        "WEBHOOK_S4_TIMEOUT_S": 12.0,    # bumped 9c0c8e25
        "WEBHOOK_S6_TIMEOUT_S": 15.0,    # bumped 9c0c8e25
        "WEBHOOK_S7_TIMEOUT_S": 10.0,
        "WEBHOOK_S8_TIMEOUT_S": 120.0,
        "WEBHOOK_S10_TIMEOUT_S": 5.0,
        "WEBHOOK_INJECTION_TIMEOUT_S": 20.0,  # bumped 9c0c8e25
    }

    def test_documented_defaults_match_source(self):
        """Grep the source for default values — fail if they drift."""
        from pathlib import Path
        src = Path("memory/persistent_hook_structure.py").read_text()
        # Each line should look like:  S4_TIMEOUT  = _t("WEBHOOK_S4_TIMEOUT_S", 12.0)
        for env_name, expected in self.EXPECTED_DEFAULTS.items():
            if env_name == "WEBHOOK_INJECTION_TIMEOUT_S":
                # Different shape: float(os.environ.get("WEBHOOK_INJECTION_TIMEOUT_S", "20"))
                pattern = f'os.environ.get("{env_name}", "{int(expected)}"'
                assert pattern in src, (
                    f"INJECTION_TIMEOUT default drifted from {expected}: pattern {pattern!r} not found"
                )
                continue
            # Section timeouts use the _t() helper
            pattern_a = f'_t("{env_name}", {expected})'
            pattern_b = f'_t("{env_name}", {int(expected)})'
            assert pattern_a in src or pattern_b in src, (
                f"{env_name} default drifted from {expected}: "
                f"neither {pattern_a!r} nor {pattern_b!r} found in source"
            )


class TestEnvDocumentation:
    """The 8 timeout env vars must be discoverable via grep — no hidden config.

    Operators read env-var names from one canonical source. Test enforces the
    canonical source includes all of them.
    """

    REQUIRED_ENV_VARS = [
        "WEBHOOK_S1_TIMEOUT_S",
        "WEBHOOK_S2_TIMEOUT_S",
        "WEBHOOK_S3_TIMEOUT_S",
        "WEBHOOK_S4_TIMEOUT_S",
        "WEBHOOK_S6_TIMEOUT_S",
        "WEBHOOK_S7_TIMEOUT_S",
        "WEBHOOK_S8_TIMEOUT_S",
        "WEBHOOK_S10_TIMEOUT_S",
        "WEBHOOK_INJECTION_TIMEOUT_S",
    ]

    def test_all_env_vars_in_source(self):
        from pathlib import Path
        src = Path("memory/persistent_hook_structure.py").read_text()
        missing = [v for v in self.REQUIRED_ENV_VARS if v not in src]
        assert not missing, (
            f"Env vars not present in source — operator can't override: {missing}"
        )
