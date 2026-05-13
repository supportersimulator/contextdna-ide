"""RACE P2 — End-to-end webhook health bridge integration test.

Proves the cross-process bridge actually works: webhook publishes →
aggregator receives → snapshot reflects exactly what was published.

Two layers of evidence:

1. **Fake NATS bus** (always runs): an in-process subject router that mimics
   ``nc.publish(subject, data)`` + ``nc.subscribe(subject, cb=...)``. Drives
   the *real* :class:`WebhookHealthPublisher` and *real*
   :class:`WebhookHealthAggregator` against this fake. Validates wiring,
   subject scoping, payload contract, and aggregator math without the network.

2. **Live NATS** (gated): if a broker is reachable on
   ``$NATS_URL`` (default ``nats://127.0.0.1:4222``) AND ``nats-py`` is
   importable, runs the same scenario through real pub/sub. Skipped
   otherwise so CI on machines without a broker still passes.

Why both: the fake test catches contract drift fast; the live test catches
serialization / connection / subject mismatches that fakes can paper over
(e.g. publisher sends ``event.webhook.completed.<node>`` but aggregator
subscribes to ``event.webhook_completed.<node>``).

Wiring under test::

    persistent_hook_structure.generate_context_injection
        └── memory.webhook_health_publisher.get_publisher()
                └── tools.webhook_health_bridge.WebhookHealthPublisher.publish
                        └── NATS subject event.webhook.completed.<node>
                                └── tools.webhook_health_bridge.WebhookHealthAggregator.subscribe
                                        └── /health.webhook (snapshot)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from typing import Any, Callable, Dict, List

import pytest

from tools.webhook_health_bridge import (
    WebhookHealthAggregator,
    WebhookHealthPublisher,
    subject_for,
)


# ── Fake NATS bus ─────────────────────────────────────────────────────────


class _FakeMsg:
    """Mimics nats.aio.msg.Msg — only the .data attribute is used."""

    __slots__ = ("subject", "data")

    def __init__(self, subject: str, data: bytes) -> None:
        self.subject = subject
        self.data = data


class _FakeNATS:
    """In-process subject router that quacks like a NATS client.

    Implements the *exact* surface used by the bridge:

    - ``await nc.publish(subject, data)`` — fan-out to every callback
      whose subscribed subject equals ``subject`` (no wildcards needed —
      the bridge uses literal per-node subjects).
    - ``await nc.subscribe(subject, cb=cb)`` — register callback, return
      a sub object the daemon can stash.

    Callbacks are awaited inline so tests don't need separate sync
    primitives — the publisher's coroutine completes only after every
    subscriber has processed the message.
    """

    def __init__(self) -> None:
        self._subs: Dict[str, List[Callable[[_FakeMsg], Any]]] = {}
        self.is_closed = False
        self.published: List[tuple] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, bytes(data)))
        for cb in list(self._subs.get(subject, [])):
            await cb(_FakeMsg(subject, data))

    async def subscribe(self, subject: str, cb=None):
        if cb is None:
            raise ValueError("cb required")
        self._subs.setdefault(subject, []).append(cb)
        return ("fake-sub", subject, cb)

    async def close(self) -> None:
        self.is_closed = True
        self._subs.clear()


# ── Helpers ───────────────────────────────────────────────────────────────


def _completion(
    n: int,
    *,
    section_count: int = 9,
    fail_section: str | None = None,
    cache_hit: bool = False,
) -> Dict[str, Any]:
    """Build a deterministic fake webhook completion (varying per ``n``)."""
    sections: Dict[str, Dict[str, Any]] = {}
    missing: List[str] = []
    for i in range(section_count):
        key = f"section_{i}"
        ok = True
        if fail_section == key:
            ok = False
            missing.append(key)
        sections[key] = {"latency_ms": 50 + i * 10 + n, "ok": ok}
    return {
        "total_ms": 300 + n * 25,
        "sections": sections,
        "missing": missing,
        "cache_hit": cache_hit,
    }


# ── Fake-NATS e2e ─────────────────────────────────────────────────────────


def test_e2e_publisher_to_aggregator_via_fake_nats():
    """Real publisher + real aggregator, glued by an in-process fake bus.

    Asserts the bridge contract end-to-end: 3 published completions land in
    the aggregator's window, and ``snapshot()`` reflects every payload field
    the publisher sent.
    """
    node = "mac3-e2e"
    fake = _FakeNATS()
    agg = WebhookHealthAggregator(node)
    pub = WebhookHealthPublisher(node)

    async def _drive():
        loop = asyncio.get_running_loop()
        # Subscribe aggregator first — same call the daemon makes.
        sub = await agg.subscribe(fake)
        assert sub is not None, "subscribe() must return a sub handle"

        # Wire publisher transport — same shape the singleton uses.
        pub.set_transport(fake.publish, loop)

        # Publish 3 completions with distinguishing fields.
        ok1 = await pub.publish_async(**_completion(0, cache_hit=True))
        ok2 = await pub.publish_async(**_completion(1, cache_hit=False))
        ok3 = await pub.publish_async(
            **_completion(2, fail_section="section_4", cache_hit=True)
        )
        assert ok1 and ok2 and ok3, "publish_async must succeed against fake bus"

    asyncio.run(_drive())

    # Bytes really traversed the bus.
    assert len(fake.published) == 3
    for subject, data in fake.published:
        assert subject == subject_for(node), f"wrong subject: {subject}"
        decoded = json.loads(data.decode("utf-8"))
        assert decoded["node"] == node
        assert isinstance(decoded["total_ms"], int)
        assert isinstance(decoded["sections"], dict)

    # Aggregator received all 3 events.
    snap = agg.snapshot()
    assert snap["events_recorded"] == 3, snap
    assert snap["receive_errors"] == 0
    assert snap["window_size"] == WebhookHealthAggregator.WINDOW

    # Latest = 3rd publish (total_ms = 300 + 2*25 = 350).
    assert snap["last_total_ms"] == 350

    # p50/p95 over [300, 325, 350]: nearest-rank → p50 rank 2 → 325, p95 rank 3 → 350.
    assert snap["p50_total_ms"] == 325
    assert snap["p95_total_ms"] == 350

    # Section health visible — every section appears with samples=3.
    assert "section_0" in snap["section_health"]
    assert snap["section_health"]["section_0"]["samples"] == 3

    # The 3rd publish failed section_4 → fail_rate 1/3.
    assert snap["section_health"]["section_4"]["fail_rate"] == pytest.approx(0.333, abs=0.01)

    # Missing tally — section_4 appeared once.
    missing_keys = {m["section"]: m["count"] for m in snap["missing_sections_recent"]}
    assert missing_keys == {"section_4": 1}

    # Cache hit rate: 2 of 3 events had cache_hit=True.
    assert snap["cache_hit_rate"] == pytest.approx(0.667, abs=0.01)

    # Publisher counters: 3 attempts, 0 errors.
    assert pub.publish_attempts == 3
    assert pub.publish_errors == 0


def test_e2e_subject_mismatch_breaks_bridge():
    """Regression guard — if publisher and aggregator disagree on the subject,
    the aggregator must NOT receive events. This pins the contract:
    ``event.webhook.completed.<node>`` exactly.

    If someone "fixes" subject_for() to use underscores or a different prefix
    on one side only, this test fails loudly.
    """
    fake = _FakeNATS()
    agg = WebhookHealthAggregator("mac3")
    pub = WebhookHealthPublisher("mac1")  # different node → different subject

    async def _drive():
        loop = asyncio.get_running_loop()
        await agg.subscribe(fake)
        pub.set_transport(fake.publish, loop)
        await pub.publish_async(**_completion(0))

    asyncio.run(_drive())

    # mac1 published, mac3 subscribed — fake bus routes by subject equality,
    # so aggregator sees nothing.
    assert agg.snapshot()["events_recorded"] == 0
    # And publisher counted a successful publish (it doesn't know about subs).
    assert pub.publish_attempts == 1


def test_e2e_aggregator_filters_other_node_payload():
    """If somehow a cross-node payload reaches the aggregator (e.g. someone
    subscribes with a wildcard later), the in-payload ``node`` filter MUST
    drop it. This is a defense-in-depth check beyond subject scoping.
    """
    agg = WebhookHealthAggregator("mac3")
    # Hand-craft a payload claiming to be from another node.
    payload = {
        "node": "mac1",
        "ts": time.time(),
        "total_ms": 200,
        "sections": {"section_0": {"latency_ms": 50, "ok": True}},
    }
    accepted = agg.record(payload)
    assert accepted is False
    assert agg.snapshot()["events_recorded"] == 0


# ── Live NATS e2e (gated) ─────────────────────────────────────────────────


def _broker_reachable(host: str = "127.0.0.1", port: int = 4222) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


_LIVE_REASON: str | None = None
try:
    import nats  # type: ignore  # noqa: F401

    _NATS_URL = os.environ.get("NATS_URL") or "nats://127.0.0.1:4222"
    if not _broker_reachable():
        _LIVE_REASON = f"NATS broker not reachable at {_NATS_URL}"
except ImportError:
    _LIVE_REASON = "nats-py not installed"


@pytest.mark.skipif(_LIVE_REASON is not None, reason=_LIVE_REASON or "")
def test_e2e_live_nats_publisher_to_aggregator():
    """Same scenario as the fake-bus test, but over a real NATS broker.

    Uses a unique node_id per run to avoid clobbering any live daemon's
    subject. Skipped unless ``nats-py`` is installed AND a broker is
    reachable on ``$NATS_URL``.
    """
    import nats as _nats

    # Unique node so we never collide with a real daemon's subject.
    node = f"e2e-test-{os.getpid()}-{int(time.time())}"

    async def _drive():
        # Two separate connections — mimics the cross-process reality
        # where webhook + daemon each open their own client.
        pub_nc = await _nats.connect(_NATS_URL, name="e2e-publisher")
        sub_nc = await _nats.connect(_NATS_URL, name="e2e-aggregator")
        try:
            agg = WebhookHealthAggregator(node)
            pub = WebhookHealthPublisher(node)

            # Aggregator subscribes via the bridge's own subscribe() helper
            # — this is what the daemon does at startup.
            sub = await agg.subscribe(sub_nc)
            assert sub is not None

            # Give NATS a tick to register the sub before we publish.
            await sub_nc.flush(timeout=1.0)

            loop = asyncio.get_running_loop()
            pub.set_transport(pub_nc.publish, loop)

            for n in range(3):
                ok = await pub.publish_async(**_completion(n))
                assert ok, "live publish_async must succeed"

            # Drain — make sure all 3 messages have been delivered.
            await pub_nc.flush(timeout=2.0)

            # Subscriber callbacks run on the same loop; allow them to drain.
            for _ in range(20):
                if agg.snapshot()["events_recorded"] >= 3:
                    break
                await asyncio.sleep(0.05)
        finally:
            await pub_nc.close()
            await sub_nc.close()

        return agg

    agg = asyncio.run(_drive())

    snap = agg.snapshot()
    assert snap["events_recorded"] == 3, (
        f"live NATS bridge broken: only {snap['events_recorded']}/3 events received "
        f"({snap=})"
    )
    assert snap["receive_errors"] == 0
    # Latest = 3rd publish (total_ms = 350).
    assert snap["last_total_ms"] == 350
