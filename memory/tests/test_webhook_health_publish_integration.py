"""RACE O3 — verify generate_context_injection publishes a health event.

Tests that the wiring inside ``memory.persistent_hook_structure`` invokes the
:class:`tools.webhook_health_bridge.WebhookHealthPublisher` singleton with the
expected payload shape (sections / missing / cache_hit / total_ms).

Strategy:
- Inject a *mock* publisher via ``memory.webhook_health_publisher.set_publisher_for_test``.
- Drive ``generate_context_injection`` with a prompt that EXCEEDS the
  5-word bypass so the full path runs.
- Capture the publish call args and validate shape.
- Confirm fire-and-forget semantics: even if the mock raises, the webhook
  still returns an InjectionResult (ZSF — never blocks hot path).
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Dict, List

import pytest


# ── Mock publisher ──────────────────────────────────────────────────────────


class _MockPublisher:
    """Captures publish() calls for assertions. Mimics the real shape only."""

    def __init__(self, raise_on_publish: bool = False) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.publish_attempts = 0
        self.publish_errors = 0
        self.raise_on_publish = raise_on_publish

    def publish(
        self,
        total_ms,
        sections,
        missing=None,
        cache_hit=None,
        extra=None,
    ) -> bool:
        self.publish_attempts += 1
        if self.raise_on_publish:
            self.publish_errors += 1
            raise RuntimeError("mock failure")
        self.calls.append(
            {
                "total_ms": total_ms,
                "sections": dict(sections or {}),
                "missing": list(missing or []),
                "cache_hit": cache_hit,
                "extra": dict(extra or {}),
            }
        )
        return True

    def stats(self) -> Dict[str, int]:
        return {
            "webhook_publish_attempts": self.publish_attempts,
            "webhook_publish_errors": self.publish_errors,
        }


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_publisher(monkeypatch):
    """Install a mock publisher that the wiring will pick up."""
    # Mute external services that the webhook touches in non-trivial ways —
    # we don't need real LLM/Redis/NATS for the publish-call assertion.
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-o3-publish")
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac3-test")
    # Ensure webhook health publisher module is fresh — clear any prior mock.
    from memory import webhook_health_publisher as whp

    whp.reset_publisher_for_test()
    mock = _MockPublisher()
    whp.set_publisher_for_test(mock)
    yield mock
    whp.reset_publisher_for_test()


# ── Tests ───────────────────────────────────────────────────────────────────


def test_generate_context_injection_publishes_health_event(mock_publisher):
    """End-to-end: webhook generation triggers publish() exactly once with shape."""
    from memory.persistent_hook_structure import generate_context_injection

    # Long-enough prompt to bypass the ≤5-word fast-path (which returns early
    # without any section work and skips the publish entirely — that's correct
    # behavior, but not what we want to exercise here).
    prompt = "investigate why the deployment pipeline is failing in staging environment"
    result = generate_context_injection(prompt=prompt, mode="hybrid")

    # The injection must complete normally.
    assert result is not None
    assert hasattr(result, "generation_time_ms")

    # And it must have invoked publish() at least once.
    assert len(mock_publisher.calls) >= 1, (
        f"expected publish() to be called, got {mock_publisher.publish_attempts} attempts "
        f"and {mock_publisher.publish_errors} errors"
    )

    call = mock_publisher.calls[-1]

    # total_ms is an integer matching the injection result.
    assert isinstance(call["total_ms"], int)
    assert call["total_ms"] >= 0
    assert call["total_ms"] == int(result.generation_time_ms)

    # sections is a dict keyed by "section_X" with {latency_ms, ok} entries.
    assert isinstance(call["sections"], dict)
    for sec_key, info in call["sections"].items():
        assert sec_key.startswith("section_"), f"bad key: {sec_key}"
        assert isinstance(info, dict)
        assert "latency_ms" in info
        assert "ok" in info
        assert isinstance(info["ok"], bool)
        assert isinstance(info["latency_ms"], int)

    # missing is a list of section keys (subset of sections that returned empty).
    assert isinstance(call["missing"], list)
    for m in call["missing"]:
        assert m.startswith("section_")
        # Missing sections must also be present in sections with ok=False.
        # (Only when the section was actually attempted; "never attempted"
        # sections are simply omitted from both dicts.)
        if m in call["sections"]:
            assert call["sections"][m]["ok"] is False

    # cache_hit may be True/False — must always be a bool (not None) in the
    # publish call because the wiring computes it explicitly.
    assert isinstance(call["cache_hit"], bool)


def test_publish_failure_does_not_break_webhook(monkeypatch):
    """If publish() raises, generate_context_injection still returns normally."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-o3-failure")
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac3-test")

    from memory import webhook_health_publisher as whp

    whp.reset_publisher_for_test()
    mock = _MockPublisher(raise_on_publish=True)
    whp.set_publisher_for_test(mock)
    try:
        from memory.persistent_hook_structure import generate_context_injection

        prompt = "investigate why the deployment pipeline is failing in staging environment"
        result = generate_context_injection(prompt=prompt, mode="hybrid")

        # Webhook returns despite mock raising.
        assert result is not None
        # Publisher attempted exactly one publish — and recorded the failure.
        assert mock.publish_attempts == 1
        assert mock.publish_errors == 1
    finally:
        whp.reset_publisher_for_test()


def test_short_prompt_bypass_does_not_publish(monkeypatch):
    """≤5-word prompts skip injection entirely — no publish either.

    This is correct behavior (no section work happened, nothing meaningful to
    report). Documenting it as a contract so the wiring doesn't silently
    publish empty/zero events for every short prompt.
    """
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-o3-bypass")
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac3-test")

    from memory import webhook_health_publisher as whp

    whp.reset_publisher_for_test()
    mock = _MockPublisher()
    whp.set_publisher_for_test(mock)
    try:
        from memory.persistent_hook_structure import generate_context_injection

        result = generate_context_injection(prompt="ok thanks", mode="hybrid")
        assert result is not None
        # Short prompt path returns early before reaching the publish wire.
        assert mock.publish_attempts == 0
    finally:
        whp.reset_publisher_for_test()


# ── Singleton sanity checks ─────────────────────────────────────────────────


def test_get_publisher_returns_singleton(monkeypatch):
    """Repeated calls to get_publisher() return the same instance."""
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac3-test")
    from memory import webhook_health_publisher as whp

    whp.reset_publisher_for_test()
    try:
        a = whp.get_publisher()
        b = whp.get_publisher()
        assert a is b
    finally:
        whp.reset_publisher_for_test()


def test_get_publisher_has_stats(monkeypatch):
    """Singleton exposes the stats() interface for daemon /health merge."""
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac3-test")
    from memory import webhook_health_publisher as whp

    whp.reset_publisher_for_test()
    try:
        pub = whp.get_publisher()
        stats = pub.stats()
        assert "webhook_publish_attempts" in stats
        assert "webhook_publish_errors" in stats
    finally:
        whp.reset_publisher_for_test()
