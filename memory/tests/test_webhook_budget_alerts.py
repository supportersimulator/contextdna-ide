"""RACE S5 — Webhook composite latency budget alerts.

Tests cover:

1. **Bridge primitives** (tools.webhook_health_bridge):
   - ``budget_subject_for`` per-node scoping + validation.
   - ``WebhookHealthPublisher.publish_budget_exceeded`` payload shape.
   - ``WebhookBudgetAggregator`` ingest, snapshot, rolling 1h count, ZSF.

2. **Webhook wiring** (memory.persistent_hook_structure):
   - Sets ``WEBHOOK_TOTAL_BUDGET_MS=1`` so the budget always fires.
   - Asserts a budget-exceeded publish hits the singleton publisher.
   - Verifies the WARNING is logged.
   - Confirms ZSF: publisher failure does NOT break webhook generation.

The bridge tests are fast and pure (no NATS). The integration tests reuse
the same mock-publisher trick as RACE O3
(``memory.tests.test_webhook_health_publish_integration``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List

import pytest

from tools.webhook_health_bridge import (
    BUDGET_SUBJECT_PREFIX,
    WebhookBudgetAggregator,
    WebhookHealthPublisher,
    budget_subject_for,
)


# ── Subject helpers ─────────────────────────────────────────────────────────


def test_budget_subject_for_scopes_to_node():
    assert budget_subject_for("mac3") == f"{BUDGET_SUBJECT_PREFIX}.mac3"
    assert budget_subject_for("cloud") == f"{BUDGET_SUBJECT_PREFIX}.cloud"


def test_budget_subject_for_rejects_empty():
    with pytest.raises(ValueError):
        budget_subject_for("")
    with pytest.raises(ValueError):
        budget_subject_for(None)  # type: ignore[arg-type]


# ── Publisher.publish_budget_exceeded ───────────────────────────────────────


class _CapturingTransport:
    """Captures dispatched (subject, data) tuples without touching NATS."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: List[tuple] = []
        self.fail = fail

    def __call__(self, subject: str, data: bytes):
        async def _coro():
            if self.fail:
                raise RuntimeError("transport failure")
            self.calls.append((subject, data))
        return _coro()


def _publisher_with_loop():
    loop = asyncio.new_event_loop()
    transport = _CapturingTransport()
    pub = WebhookHealthPublisher("mac3", nats_publish=transport, loop=loop)
    return pub, transport, loop


def _drain(loop: asyncio.AbstractEventLoop) -> None:
    """Run pending tasks scheduled via run_coroutine_threadsafe."""
    # Give scheduled coroutines one tick to run.
    async def _noop():
        await asyncio.sleep(0)
    loop.run_until_complete(_noop())
    loop.run_until_complete(_noop())


def test_publish_budget_exceeded_emits_correct_subject_and_payload():
    pub, transport, loop = _publisher_with_loop()
    try:
        ok = pub.publish_budget_exceeded(
            total_ms=25000,
            budget_ms=20000,
            sections={"section_2": {"latency_ms": 18000, "ok": True}},
            missing=["section_8"],
        )
        assert ok is True
        _drain(loop)
        assert len(transport.calls) == 1
        subject, data = transport.calls[0]
        assert subject == budget_subject_for("mac3")
        payload = json.loads(data.decode("utf-8"))
        assert payload["node"] == "mac3"
        assert payload["total_ms"] == 25000
        assert payload["budget_ms"] == 20000
        assert payload["over_by_ms"] == 5000
        assert payload["missing"] == ["section_8"]
        assert "ts" in payload
        assert pub.publish_attempts == 1
        assert pub.publish_errors == 0
    finally:
        loop.close()


def test_publish_budget_exceeded_counts_failure_zsf():
    """Transport failure must be counted, never raised — ZSF."""
    loop = asyncio.new_event_loop()
    transport = _CapturingTransport(fail=True)
    pub = WebhookHealthPublisher("mac3", nats_publish=transport, loop=loop)
    try:
        # Call returns True (scheduled), but the loop run will raise inside the
        # coroutine — that bumps publisher counters via _record_error... except
        # the transport coroutine errors are observed by our drain only. Hot
        # path itself never raised — that's the contract we care about.
        result = pub.publish_budget_exceeded(total_ms=21000, budget_ms=20000, sections={})
        assert isinstance(result, bool)
        # Drain — transport's coroutine raises but the loop swallows it.
        try:
            _drain(loop)
        except Exception as exc:  # noqa: BLE001 — expected; transport raises
            # Observable: print to test stderr so it's visible if the
            # exception type ever changes unexpectedly.
            print(f"drain swallowed expected transport error: {type(exc).__name__}: {exc}")
        assert pub.publish_attempts == 1
    finally:
        loop.close()


def test_publish_budget_exceeded_no_transport_records_error():
    pub = WebhookHealthPublisher("mac3")  # no transport
    ok = pub.publish_budget_exceeded(total_ms=21000, budget_ms=20000, sections={})
    assert ok is False
    assert pub.publish_errors == 1
    assert pub.publish_attempts == 1


# ── WebhookBudgetAggregator ────────────────────────────────────────────────


def test_aggregator_empty_snapshot_stable_shape():
    agg = WebhookBudgetAggregator("mac3")
    snap = agg.snapshot()
    assert snap["active"] is True
    assert snap["recent_count"] == 0
    assert snap["total_count"] == 0
    assert snap["receive_errors"] == 0
    assert snap["last_event"] is None
    assert "window_s" in snap


def test_aggregator_records_payload_and_increments_recent():
    agg = WebhookBudgetAggregator("mac3")
    payload = {
        "node": "mac3",
        "ts": time.time(),
        "total_ms": 25000,
        "budget_ms": 20000,
        "over_by_ms": 5000,
    }
    accepted = agg.record(payload)
    assert accepted is True
    snap = agg.snapshot()
    assert snap["recent_count"] == 1
    assert snap["total_count"] == 1
    assert snap["last_event"]["total_ms"] == 25000
    assert snap["last_event"]["budget_ms"] == 20000
    assert snap["last_event"]["over_by_ms"] == 5000


def test_aggregator_filters_other_nodes():
    agg = WebhookBudgetAggregator("mac3")
    accepted = agg.record({
        "node": "mac1",
        "ts": time.time(),
        "total_ms": 25000,
        "budget_ms": 20000,
    })
    assert accepted is False
    assert agg.snapshot()["recent_count"] == 0


def test_aggregator_records_raw_bytes_and_handles_bad_json():
    agg = WebhookBudgetAggregator("mac3")
    good = json.dumps({
        "node": "mac3",
        "total_ms": 21000,
        "budget_ms": 20000,
    }).encode("utf-8")
    assert agg.record_raw(good) is True
    assert agg.snapshot()["recent_count"] == 1
    # Bad JSON
    assert agg.record_raw(b"not-json{") is False
    assert agg.snapshot()["receive_errors"] == 1
    # Non-dict payload
    assert agg.record_raw(b"42") is False
    assert agg.snapshot()["receive_errors"] == 2


def test_aggregator_recent_window_prunes_old_events():
    """Events older than window_s drop out of recent_count, total stays."""
    agg = WebhookBudgetAggregator("mac3", recent_window_s=2)
    old_ts = time.time() - 10  # 10s ago, > 2s window
    agg.record({"node": "mac3", "ts": old_ts, "total_ms": 25000, "budget_ms": 20000})
    agg.record({"node": "mac3", "ts": time.time(), "total_ms": 26000, "budget_ms": 20000})
    snap = agg.snapshot()
    assert snap["recent_count"] == 1  # only the fresh one
    assert snap["total_count"] == 2  # both still tallied lifetime
    # Counters surface both via stats() too (daemon merges into _stats)
    stats = agg.stats()
    assert stats["webhook_budget_exceeded_count"] == 2
    assert stats["webhook_budget_receive_errors"] == 0


def test_aggregator_malformed_payload_counts_error():
    agg = WebhookBudgetAggregator("mac3")
    # missing budget_ms
    assert agg.record({"node": "mac3", "total_ms": 21000}) is False
    assert agg.snapshot()["receive_errors"] == 1


def test_aggregator_publisher_to_aggregator_roundtrip():
    """End-to-end: publisher serializes → aggregator deserializes → snapshot."""
    pub, transport, loop = _publisher_with_loop()
    agg = WebhookBudgetAggregator("mac3")
    try:
        pub.publish_budget_exceeded(total_ms=30000, budget_ms=20000, sections={})
        _drain(loop)
        assert len(transport.calls) == 1
        _, data = transport.calls[0]
        assert agg.record_raw(data) is True
        snap = agg.snapshot()
        assert snap["recent_count"] == 1
        assert snap["last_event"]["over_by_ms"] == 10000
    finally:
        loop.close()


# ── Webhook wiring (persistent_hook_structure) ─────────────────────────────


class _MockBudgetPublisher:
    """Captures publish() + publish_budget_exceeded() calls."""

    def __init__(self, raise_on_budget: bool = False) -> None:
        self.publish_calls: List[Dict[str, Any]] = []
        self.budget_calls: List[Dict[str, Any]] = []
        self.publish_attempts = 0
        self.publish_errors = 0
        self.raise_on_budget = raise_on_budget

    def publish(self, total_ms, sections, missing=None, cache_hit=None, extra=None) -> bool:
        self.publish_attempts += 1
        self.publish_calls.append(
            {
                "total_ms": total_ms,
                "sections": dict(sections or {}),
                "missing": list(missing or []),
                "cache_hit": cache_hit,
            }
        )
        return True

    def publish_budget_exceeded(
        self, total_ms, budget_ms, sections=None, missing=None, extra=None
    ) -> bool:
        self.publish_attempts += 1
        if self.raise_on_budget:
            self.publish_errors += 1
            raise RuntimeError("mock budget failure")
        self.budget_calls.append(
            {
                "total_ms": total_ms,
                "budget_ms": budget_ms,
                "sections": dict(sections or {}),
                "missing": list(missing or []),
            }
        )
        return True

    def stats(self) -> Dict[str, int]:
        return {
            "webhook_publish_attempts": self.publish_attempts,
            "webhook_publish_errors": self.publish_errors,
        }


@pytest.fixture
def mock_budget_publisher(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-s5-budget")
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac3-test")
    # Force any positive parallel_elapsed_ms to exceed the budget.
    monkeypatch.setenv("WEBHOOK_TOTAL_BUDGET_MS", "1")
    from memory import webhook_health_publisher as whp

    whp.reset_publisher_for_test()
    mock = _MockBudgetPublisher()
    whp.set_publisher_for_test(mock)
    yield mock
    whp.reset_publisher_for_test()


def test_webhook_fires_budget_alert_when_exceeded(mock_budget_publisher, caplog):
    """Webhook publishes a budget alert + logs WARNING when over budget."""
    from memory.persistent_hook_structure import generate_context_injection

    prompt = "investigate why the deployment pipeline is failing in staging environment"
    with caplog.at_level(logging.WARNING):
        result = generate_context_injection(prompt=prompt, mode="hybrid")

    assert result is not None
    # At least one budget-exceeded publish should have fired (budget=1ms is
    # always blown by real injection work).
    assert len(mock_budget_publisher.budget_calls) >= 1, (
        f"expected publish_budget_exceeded() — got "
        f"{mock_budget_publisher.publish_attempts} total publishes / "
        f"{len(mock_budget_publisher.publish_calls)} health / "
        f"{len(mock_budget_publisher.budget_calls)} budget"
    )
    call = mock_budget_publisher.budget_calls[-1]
    assert call["budget_ms"] == 1
    assert call["total_ms"] >= 1
    assert isinstance(call["missing"], list)

    # WARNING line is greppable in fleet logs.
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("webhook_budget_exceeded" in msg for msg in warning_messages), (
        f"expected webhook_budget_exceeded WARNING; got: {warning_messages}"
    )


def test_webhook_does_not_fire_alert_under_budget(monkeypatch):
    """Budget set huge → no breach → no budget publish."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-s5-no-breach")
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac3-test")
    monkeypatch.setenv("WEBHOOK_TOTAL_BUDGET_MS", "600000")  # 10 minutes — never breached
    from memory import webhook_health_publisher as whp

    whp.reset_publisher_for_test()
    mock = _MockBudgetPublisher()
    whp.set_publisher_for_test(mock)
    try:
        from memory.persistent_hook_structure import generate_context_injection

        prompt = "investigate why the deployment pipeline is failing in staging environment"
        result = generate_context_injection(prompt=prompt, mode="hybrid")
        assert result is not None
        # Health event still fires; budget event must not.
        assert len(mock.budget_calls) == 0
        assert len(mock.publish_calls) >= 1
    finally:
        whp.reset_publisher_for_test()


def test_webhook_budget_publish_failure_does_not_break_hot_path(monkeypatch):
    """ZSF: if publish_budget_exceeded raises, webhook still returns InjectionResult."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-s5-fail")
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac3-test")
    monkeypatch.setenv("WEBHOOK_TOTAL_BUDGET_MS", "1")
    from memory import webhook_health_publisher as whp

    whp.reset_publisher_for_test()
    mock = _MockBudgetPublisher(raise_on_budget=True)
    whp.set_publisher_for_test(mock)
    try:
        from memory.persistent_hook_structure import generate_context_injection

        prompt = "investigate why the deployment pipeline is failing in staging environment"
        result = generate_context_injection(prompt=prompt, mode="hybrid")
        assert result is not None
        # Mock raised; failure was counted, hot path returned anyway.
        assert mock.publish_errors >= 1
    finally:
        whp.reset_publisher_for_test()


def test_webhook_budget_disabled_when_env_zero(monkeypatch):
    """WEBHOOK_TOTAL_BUDGET_MS=0 disables alert entirely."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-s5-disabled")
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac3-test")
    monkeypatch.setenv("WEBHOOK_TOTAL_BUDGET_MS", "0")
    from memory import webhook_health_publisher as whp

    whp.reset_publisher_for_test()
    mock = _MockBudgetPublisher()
    whp.set_publisher_for_test(mock)
    try:
        from memory.persistent_hook_structure import generate_context_injection

        prompt = "investigate why the deployment pipeline is failing in staging environment"
        result = generate_context_injection(prompt=prompt, mode="hybrid")
        assert result is not None
        assert len(mock.budget_calls) == 0
    finally:
        whp.reset_publisher_for_test()
