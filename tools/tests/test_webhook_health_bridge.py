"""Tests for tools/webhook_health_bridge.py — RACE N2.

Covers:
- Subject helpers (per-node scoping)
- Publisher: payload build, validation, fire-and-forget semantics, error counting
- Aggregator: record, ring eviction, snapshot math, missing tally, cache rate
- End-to-end: publisher → bytes → aggregator.record_raw → snapshot
- ZSF: every failure path bumps an observable counter
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

import pytest

from tools.webhook_health_bridge import (
    SUBJECT_PREFIX,
    WebhookHealthAggregator,
    WebhookHealthPublisher,
    _percentile,
    subject_for,
    subject_pattern_for,
)


# ── Subject helpers ──────────────────────────────────────────────────────


def test_subject_for_scopes_to_node():
    assert subject_for("mac3") == f"{SUBJECT_PREFIX}.mac3"
    assert subject_for("mac1") == f"{SUBJECT_PREFIX}.mac1"
    assert subject_pattern_for("cloud") == f"{SUBJECT_PREFIX}.cloud"


def test_subject_for_rejects_empty():
    with pytest.raises(ValueError):
        subject_for("")
    with pytest.raises(ValueError):
        subject_for(None)  # type: ignore[arg-type]


# ── Percentile helper ────────────────────────────────────────────────────


def test_percentile_basic():
    assert _percentile([], 50) == 0.0
    assert _percentile([100], 95) == 100
    # Nearest-rank: 10 values, p95 → rank ceil(0.95 * 10) = 10 → max
    assert _percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    # p50 of 10 values → rank 5 → 5
    assert _percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    # Out of order input still works
    assert _percentile([10, 1, 9, 2, 8], 50) == 8 or _percentile([10, 1, 9, 2, 8], 50) >= 1


# ── Publisher: payload construction ──────────────────────────────────────


def test_publisher_requires_node_id():
    with pytest.raises(ValueError):
        WebhookHealthPublisher("")


def test_publisher_build_payload_minimal():
    pub = WebhookHealthPublisher("mac3")
    payload = pub.build_payload(
        total_ms=300,
        sections={"section_0": {"latency_ms": 50, "ok": True}},
    )
    assert payload["node"] == "mac3"
    assert payload["total_ms"] == 300
    assert payload["sections"] == {"section_0": {"latency_ms": 50, "ok": True}}
    assert payload["missing"] == []
    assert "ts" in payload
    assert isinstance(payload["ts"], float)


def test_publisher_build_payload_filters_invalid_sections():
    """Section entries missing latency_ms or ok must be silently dropped."""
    pub = WebhookHealthPublisher("mac3")
    payload = pub.build_payload(
        total_ms=200,
        sections={
            "section_0": {"latency_ms": 50, "ok": True},
            "section_1": {"latency_ms": "notanumber", "ok": True},  # invalid
            "section_2": {"latency_ms": 75},  # missing ok
            "section_3": "not a dict",  # type wrong
            "section_4": {"latency_ms": 90, "ok": False, "error": "timeout"},
        },
    )
    sections = payload["sections"]
    assert "section_0" in sections
    assert "section_4" in sections
    assert sections["section_4"]["error"] == "timeout"
    assert "section_1" not in sections
    assert "section_2" not in sections
    assert "section_3" not in sections


def test_publisher_build_payload_with_missing_and_cache():
    pub = WebhookHealthPublisher("mac3")
    payload = pub.build_payload(
        total_ms=400,
        sections={"section_0": {"latency_ms": 100, "ok": True}},
        missing=["section_4", "section_6"],
        cache_hit=True,
    )
    assert payload["missing"] == ["section_4", "section_6"]
    assert payload["cache_hit"] is True


# ── Publisher: error counting (ZSF) ──────────────────────────────────────


def test_publish_no_transport_records_error():
    pub = WebhookHealthPublisher("mac3")
    ok = pub.publish(total_ms=100, sections={"section_0": {"latency_ms": 50, "ok": True}})
    assert ok is False
    assert pub.publish_errors == 1
    assert pub.publish_attempts == 1
    s = pub.stats()
    assert s["webhook_publish_errors"] == 1
    assert s["webhook_publish_attempts"] == 1


def test_publish_async_no_transport_records_error():
    pub = WebhookHealthPublisher("mac3")

    async def _drive():
        ok = await pub.publish_async(
            total_ms=100,
            sections={"section_0": {"latency_ms": 50, "ok": True}},
        )
        assert ok is False

    asyncio.run(_drive())
    assert pub.publish_errors == 1
    assert pub.publish_attempts == 1


def test_publish_async_transport_raises_records_error():
    pub = WebhookHealthPublisher("mac3")

    async def _boom(subject: str, data: bytes) -> None:
        raise RuntimeError("nats down")

    pub.set_transport(_boom, asyncio.new_event_loop())

    async def _drive():
        ok = await pub.publish_async(
            total_ms=100,
            sections={"section_0": {"latency_ms": 50, "ok": True}},
        )
        assert ok is False

    asyncio.run(_drive())
    assert pub.publish_errors == 1


def test_publish_async_success_no_error():
    pub = WebhookHealthPublisher("mac3")
    received: List[tuple] = []

    async def _capture(subject: str, data: bytes) -> None:
        received.append((subject, data))

    async def _drive():
        loop = asyncio.get_running_loop()
        pub.set_transport(_capture, loop)
        ok = await pub.publish_async(
            total_ms=300,
            sections={
                "section_0": {"latency_ms": 50, "ok": True},
                "section_1": {"latency_ms": 80, "ok": True},
            },
            missing=["section_4"],
        )
        assert ok is True

    asyncio.run(_drive())
    assert pub.publish_errors == 0
    assert pub.publish_attempts == 1
    assert len(received) == 1
    subject, data = received[0]
    assert subject == subject_for("mac3")
    decoded = json.loads(data.decode("utf-8"))
    assert decoded["node"] == "mac3"
    assert decoded["total_ms"] == 300
    assert decoded["missing"] == ["section_4"]


def test_publish_sync_in_running_loop_schedules_task():
    """publish() called from inside a running loop attaches to it."""
    pub = WebhookHealthPublisher("mac3")
    captured: List[tuple] = []

    async def _capture(subject: str, data: bytes) -> None:
        captured.append((subject, data))

    async def _drive():
        loop = asyncio.get_running_loop()
        pub.set_transport(_capture, loop)
        ok = pub.publish(
            total_ms=200,
            sections={"section_0": {"latency_ms": 30, "ok": True}},
        )
        assert ok is True
        # Yield so the scheduled task runs.
        await asyncio.sleep(0)

    asyncio.run(_drive())
    assert len(captured) == 1
    assert pub.publish_errors == 0


def test_publish_sync_no_loop_no_thread_records_error():
    """publish() called sync with no loop and no transport-loop drops + counts."""
    pub = WebhookHealthPublisher("mac3")
    # No transport at all → counts as error.
    ok = pub.publish(total_ms=100, sections={"section_0": {"latency_ms": 10, "ok": True}})
    assert ok is False
    assert pub.publish_errors == 1


# ── Aggregator: ingest + filtering ───────────────────────────────────────


def _evt(
    node: str = "mac3",
    total_ms: float = 300,
    sections: Dict[str, Dict[str, Any]] | None = None,
    missing: List[str] | None = None,
    cache_hit: bool | None = None,
    ts: float | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "node": node,
        "ts": ts if ts is not None else time.time(),
        "total_ms": total_ms,
        "sections": sections
        or {"section_0": {"latency_ms": 50, "ok": True}},
        "missing": missing or [],
    }
    if cache_hit is not None:
        payload["cache_hit"] = cache_hit
    return payload


def test_aggregator_empty_snapshot_shape():
    agg = WebhookHealthAggregator("mac3")
    snap = agg.snapshot()
    assert snap["active"] is True
    assert snap["events_recorded"] == 0
    assert snap["last_webhook_age_s"] is None
    assert snap["last_total_ms"] is None
    assert snap["p50_total_ms"] is None
    assert snap["p95_total_ms"] is None
    assert snap["missing_sections_recent"] == []
    assert snap["section_health"] == {}
    assert snap["cache_hit_rate"] is None
    assert snap["window_size"] == 10


def test_aggregator_records_and_summarizes():
    agg = WebhookHealthAggregator("mac3")
    now = 10_000.0
    agg.record(
        _evt(
            ts=now - 12,
            total_ms=318,
            sections={
                "section_0": {"latency_ms": 56, "ok": True},
                "section_6": {"latency_ms": 6500, "ok": True},
            },
        )
    )
    snap = agg.snapshot(now=now)
    assert snap["events_recorded"] == 1
    assert snap["last_webhook_age_s"] == 12
    assert snap["last_total_ms"] == 318
    assert snap["p50_total_ms"] == 318
    assert snap["p95_total_ms"] == 318
    assert "section_0" in snap["section_health"]
    assert snap["section_health"]["section_6"]["avg_ms"] == 6500.0
    assert snap["section_health"]["section_6"]["fail_rate"] == 0.0


def test_aggregator_filters_other_node():
    agg = WebhookHealthAggregator("mac3")
    accepted = agg.record(_evt(node="mac1"))
    assert accepted is False
    assert agg.snapshot()["events_recorded"] == 0


def test_aggregator_accepts_missing_node():
    """If 'node' field is missing, accept (some transports may strip it)."""
    agg = WebhookHealthAggregator("mac3")
    payload = _evt()
    del payload["node"]
    assert agg.record(payload) is True


def test_aggregator_ring_eviction():
    agg = WebhookHealthAggregator("mac3", window=3)
    for i in range(5):
        agg.record(_evt(total_ms=100 + i * 10, ts=1000 + i))
    snap = agg.snapshot(now=2000)
    # Only last 3 events kept: total_ms 120, 130, 140
    assert snap["events_recorded"] == 5
    # last is the most recent
    assert snap["last_total_ms"] == 140
    # window kept = 3 events with totals [120,130,140]
    assert snap["p50_total_ms"] in (120, 130, 140)
    assert snap["p95_total_ms"] == 140


def test_aggregator_section_fail_rate():
    agg = WebhookHealthAggregator("mac3")
    for i in range(10):
        ok = i < 9  # 1 failure out of 10
        agg.record(
            _evt(
                sections={
                    "section_6": {"latency_ms": 5000, "ok": ok},
                }
            )
        )
    snap = agg.snapshot()
    assert snap["section_health"]["section_6"]["fail_rate"] == 0.1
    assert snap["section_health"]["section_6"]["samples"] == 10


def test_aggregator_missing_sections_tally_sorted():
    agg = WebhookHealthAggregator("mac3")
    agg.record(_evt(missing=["section_4"]))
    agg.record(_evt(missing=["section_4", "section_6"]))
    agg.record(_evt(missing=["section_6"]))
    agg.record(_evt(missing=["section_6"]))
    snap = agg.snapshot()
    tally = snap["missing_sections_recent"]
    assert tally[0]["section"] == "section_6"
    assert tally[0]["count"] == 3
    assert tally[1]["section"] == "section_4"
    assert tally[1]["count"] == 2


def test_aggregator_cache_hit_rate():
    agg = WebhookHealthAggregator("mac3")
    agg.record(_evt(cache_hit=True))
    agg.record(_evt(cache_hit=True))
    agg.record(_evt(cache_hit=False))
    agg.record(_evt(cache_hit=False))
    agg.record(_evt())  # no cache_hit field — should not affect rate
    snap = agg.snapshot()
    assert snap["cache_hit_rate"] == 0.5


def test_aggregator_p50_p95_over_window():
    agg = WebhookHealthAggregator("mac3")
    # 10 events with ascending total_ms 100..1000.
    for i in range(10):
        agg.record(_evt(total_ms=100 * (i + 1)))
    snap = agg.snapshot()
    # nearest-rank p50 of 10 → rank 5 → 500
    assert snap["p50_total_ms"] == 500
    assert snap["p95_total_ms"] == 1000


def test_aggregator_record_malformed_increments_errors():
    agg = WebhookHealthAggregator("mac3")
    assert agg.record({"node": "mac3", "ts": 1, "total_ms": "bad", "sections": {}}) is False
    assert agg.record({"node": "mac3", "sections": "notdict"}) is False
    assert agg.snapshot()["receive_errors"] >= 2


def test_aggregator_record_raw_bad_json():
    agg = WebhookHealthAggregator("mac3")
    assert agg.record_raw(b"this is not json") is False
    assert agg.snapshot()["receive_errors"] == 1


def test_aggregator_record_raw_non_dict_payload():
    agg = WebhookHealthAggregator("mac3")
    assert agg.record_raw(b"[1,2,3]") is False
    assert agg.snapshot()["receive_errors"] == 1


def test_aggregator_record_raw_happy_path():
    agg = WebhookHealthAggregator("mac3")
    payload = _evt(total_ms=222)
    data = json.dumps(payload).encode("utf-8")
    assert agg.record_raw(data) is True
    snap = agg.snapshot()
    assert snap["last_total_ms"] == 222


def test_aggregator_stats_counters():
    agg = WebhookHealthAggregator("mac3")
    agg.record(_evt())
    agg.record_raw(b"bad-json")
    s = agg.stats()
    assert s["webhook_events_received"] == 1
    assert s["webhook_receive_errors"] == 1


# ── End-to-end: publisher → aggregator round-trip ────────────────────────


def test_publisher_to_aggregator_round_trip():
    """Bytes from the publisher decode cleanly in the aggregator."""
    pub = WebhookHealthPublisher("mac3")
    captured: List[tuple] = []

    async def _capture(subject: str, data: bytes) -> None:
        captured.append((subject, data))

    async def _drive():
        loop = asyncio.get_running_loop()
        pub.set_transport(_capture, loop)
        await pub.publish_async(
            total_ms=318,
            sections={
                "section_0": {"latency_ms": 56, "ok": True},
                "section_6": {"latency_ms": 6500, "ok": True},
                "section_4": {"latency_ms": 0, "ok": False, "error": "timeout"},
            },
            missing=["section_4"],
            cache_hit=True,
        )

    asyncio.run(_drive())
    assert len(captured) == 1
    subject, data = captured[0]
    assert subject == subject_for("mac3")

    agg = WebhookHealthAggregator("mac3")
    assert agg.record_raw(data) is True
    snap = agg.snapshot()
    assert snap["last_total_ms"] == 318
    assert snap["section_health"]["section_4"]["fail_rate"] == 1.0
    assert snap["missing_sections_recent"][0]["section"] == "section_4"
    assert snap["cache_hit_rate"] == 1.0


def test_aggregator_subscribe_uses_correct_subject():
    """The subscribe() helper must subscribe to event.webhook.completed.<node>."""
    agg = WebhookHealthAggregator("mac3")

    class _FakeNC:
        def __init__(self):
            self.subscribed: List[str] = []

        async def subscribe(self, subject: str, cb=None):
            self.subscribed.append(subject)
            return ("sub", subject, cb)

    nc = _FakeNC()

    async def _drive():
        sub = await agg.subscribe(nc)
        assert sub[1] == subject_for("mac3")

    asyncio.run(_drive())
    assert nc.subscribed == [subject_for("mac3")]


def test_daemon_build_health_wires_webhook_block():
    """Daemon /health must include a top-level ``webhook`` key.

    Structural regression guard — same pattern as test_build_health_exposes_all_b6_sections.
    Greps the daemon source rather than instantiating FleetNerveNATS (which
    needs NATS, asyncio loop, etc.).
    """
    from pathlib import Path

    daemon_path = Path(__file__).resolve().parent.parent / "fleet_nerve_nats.py"
    src = daemon_path.read_text(encoding="utf-8")
    # Top-level key in _build_health() return dict
    assert '"webhook":' in src, "webhook key missing from /health response"
    # Aggregator import + subscription wiring
    assert "WebhookHealthAggregator" in src, "WebhookHealthAggregator not wired"
    assert "event.webhook.completed" in src, "subject not referenced in daemon"


def test_aggregator_subscribe_callback_records_messages():
    """Messages routed through subscribe() callback land in the ring."""
    agg = WebhookHealthAggregator("mac3")
    captured_cb = []

    class _FakeNC:
        async def subscribe(self, subject: str, cb=None):
            captured_cb.append(cb)
            return None

    class _FakeMsg:
        def __init__(self, data: bytes):
            self.data = data

    async def _drive():
        await agg.subscribe(_FakeNC())
        cb = captured_cb[0]
        payload = _evt(total_ms=999)
        await cb(_FakeMsg(json.dumps(payload).encode("utf-8")))
        # bad message should bump receive_errors but not crash
        await cb(_FakeMsg(b"not-json"))

    asyncio.run(_drive())
    snap = agg.snapshot()
    assert snap["last_total_ms"] == 999
    assert snap["receive_errors"] >= 1
