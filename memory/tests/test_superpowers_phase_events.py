"""RACE V1 — superpowers phase events on the fleet NATS bus.

Covers the cross-process bridge added so peer fleet nodes can gate on each
other's BLOCKING / EXECUTION superpowers phases. Three contracts under test:

1. ``SuperpowersAdapter.update_phase`` triggers a NATS publish to
   ``event.superpowers.phase.<node_id>`` carrying the documented payload
   shape (phase, prev_phase, ts, node_id, optional session_id/skill_chain).

2. Zero Silent Failures — a publish failure increments the
   ``superpowers_phase_publish_errors`` counter and is logged, never
   swallowed.

3. The daemon-side ``SuperpowersPhaseAggregator`` populates a per-node
   ``fleet_phase_states`` mapping such that other nodes can poll the daemon
   ``/health`` endpoint (or subscribe directly) and gate on a peer's phase.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import pytest

# multi-fleet sources are not on the default sys.path; mirror what the
# repository's pytest invocation does for the matching multi-fleet tests.
_MF_ROOT = Path(__file__).resolve().parents[2] / "multi-fleet"
if str(_MF_ROOT) not in sys.path:
    sys.path.insert(0, str(_MF_ROOT))

from multifleet.event_bus import IDEEventBus
from multifleet.superpowers_adapter import (
    SuperpowersAdapter,
    get_phase_publisher,
    reset_phase_publisher_for_tests,
    set_phase_publisher_transport,
)
from multifleet.superpowers_phase_bridge import (
    SUBJECT_PREFIX,
    SuperpowersPhaseAggregator,
    SuperpowersPhasePublisher,
    subject_for,
    subject_pattern_all,
)


# ── shared fixtures ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singletons():
    IDEEventBus.reset()
    reset_phase_publisher_for_tests()
    yield
    IDEEventBus.reset()
    reset_phase_publisher_for_tests()


@pytest.fixture
def bus():
    b = IDEEventBus(ring_size=50, namespace_ring_size=20)
    IDEEventBus._instance = b
    return b


class _FakeNATS:
    """Minimal in-memory NATS stand-in.

    Captures every (subject, payload) the publisher emits so tests can
    assert on the wire format without touching real networking. Subscribers
    are simple list filters keyed by exact subject prefix match before the
    final wildcard segment; the only pattern we need to support is
    ``event.superpowers.phase.*``.
    """

    def __init__(self):
        self.published: list[tuple[str, bytes]] = []
        self._handlers: list[tuple[str, object]] = []  # (subject, cb)

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))
        # Dispatch to wildcard subscribers for the aggregator path.
        for sub_subject, cb in list(self._handlers):
            if _matches(sub_subject, subject):
                msg = _FakeMsg(subject=subject, data=data)
                await cb(msg)

    async def subscribe(self, subject: str, cb):
        self._handlers.append((subject, cb))
        return _FakeSub(subject)


class _FakeMsg:
    def __init__(self, subject: str, data: bytes):
        self.subject = subject
        self.data = data


class _FakeSub:
    def __init__(self, subject: str):
        self.subject = subject


def _matches(pattern: str, subject: str) -> bool:
    """Match NATS-style ``a.b.*`` patterns against a concrete subject."""
    p_parts = pattern.split(".")
    s_parts = subject.split(".")
    if len(p_parts) != len(s_parts):
        return False
    for p, s in zip(p_parts, s_parts):
        if p == "*":
            continue
        if p != s:
            return False
    return True


# ════════════════════════════════════════════════════════════════════════
# 1. Phase change emits NATS publish
# ════════════════════════════════════════════════════════════════════════


class TestPhaseChangeEmitsPublish:
    @pytest.mark.asyncio
    async def test_update_phase_emits_nats_publish(self, bus):
        nats = _FakeNATS()
        loop = asyncio.get_running_loop()
        set_phase_publisher_transport(nats.publish, loop)

        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming")

        # Allow scheduled publish task to run.
        await asyncio.sleep(0.05)

        assert len(nats.published) == 1
        subject, data = nats.published[0]
        assert subject.startswith(SUBJECT_PREFIX)
        payload = json.loads(data.decode("utf-8"))
        assert payload["phase"] == "brainstorming"
        assert payload["prev_phase"] == "unknown"
        assert isinstance(payload["ts"], (int, float))
        assert payload["node_id"]  # adapter must self-tag

    @pytest.mark.asyncio
    async def test_phase_chain_publishes_each_transition(self, bus):
        nats = _FakeNATS()
        loop = asyncio.get_running_loop()
        set_phase_publisher_transport(nats.publish, loop)

        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming")
        adapter.update_phase("writing-plans")
        adapter.update_phase("executing-plans")
        await asyncio.sleep(0.05)

        assert len(nats.published) == 3
        phases = [json.loads(d.decode())["phase"] for _, d in nats.published]
        assert phases == ["brainstorming", "writing-plans", "executing-plans"]

    @pytest.mark.asyncio
    async def test_same_phase_repeat_does_not_publish(self, bus):
        nats = _FakeNATS()
        loop = asyncio.get_running_loop()
        set_phase_publisher_transport(nats.publish, loop)

        adapter = SuperpowersAdapter()
        adapter.update_phase("executing-plans")
        adapter.update_phase("executing-plans")  # idempotent
        await asyncio.sleep(0.05)
        assert len(nats.published) == 1

    @pytest.mark.asyncio
    async def test_session_id_and_skill_chain_propagate(self, bus):
        nats = _FakeNATS()
        loop = asyncio.get_running_loop()
        set_phase_publisher_transport(nats.publish, loop)

        adapter = SuperpowersAdapter()
        adapter.update_phase(
            "executing-plans",
            session_id="sess-42",
            skill_chain=["brainstorming", "writing-plans"],
        )
        await asyncio.sleep(0.05)

        payload = json.loads(nats.published[0][1].decode("utf-8"))
        assert payload["session_id"] == "sess-42"
        assert payload["skill_chain"] == ["brainstorming", "writing-plans"]

    @pytest.mark.asyncio
    async def test_publisher_subject_matches_node_id(self, bus):
        nats = _FakeNATS()
        loop = asyncio.get_running_loop()
        set_phase_publisher_transport(nats.publish, loop)

        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming")
        await asyncio.sleep(0.05)

        publisher = get_phase_publisher()
        subject, _ = nats.published[0]
        assert subject == subject_for(publisher.node_id)


# ════════════════════════════════════════════════════════════════════════
# 2. ZSF — failure counted, not silent
# ════════════════════════════════════════════════════════════════════════


class TestZeroSilentFailures:
    @pytest.mark.asyncio
    async def test_publish_failure_counted(self, bus, caplog):
        async def boom(_subject, _data):
            raise RuntimeError("nats-down")

        loop = asyncio.get_running_loop()
        set_phase_publisher_transport(boom, loop)
        publisher = get_phase_publisher()

        with caplog.at_level(logging.WARNING):
            ok = await publisher.publish_async("brainstorming", "unknown")
            assert ok is False

        # ZSF: error counter incremented, attempt counter incremented.
        stats = publisher.stats()
        assert stats["superpowers_phase_publish_errors"] == 1
        assert stats["superpowers_phase_publish_attempts"] == 1
        assert any(
            "superpowers phase publish failed" in r.getMessage()
            for r in caplog.records
        )

    def test_publish_with_no_transport_counts_error(self):
        # Fresh publisher, no transport — single sync call must increment err.
        publisher = SuperpowersPhasePublisher(node_id="mac1")
        ok = publisher.publish("brainstorming", "unknown")
        assert ok is False
        stats = publisher.stats()
        assert stats["superpowers_phase_publish_errors"] == 1
        assert stats["superpowers_phase_publish_attempts"] == 1

    @pytest.mark.asyncio
    async def test_adapter_phase_change_continues_when_nats_dead(self, bus):
        async def boom(_subject, _data):
            raise RuntimeError("nats-down")

        loop = asyncio.get_running_loop()
        set_phase_publisher_transport(boom, loop)

        adapter = SuperpowersAdapter()
        adapter.update_phase("executing-plans")  # must NOT raise
        await asyncio.sleep(0.05)

        # Adapter state still advanced even though NATS is broken.
        assert adapter._current_phase == "executing-plans"
        ok, _reason = adapter.can_dispatch_race()
        assert ok is True

        # And the failure surfaced as an observable counter.
        stats = get_phase_publisher().stats()
        assert stats["superpowers_phase_publish_errors"] >= 1

    def test_aggregator_records_bad_json_as_receive_error(self):
        agg = SuperpowersPhaseAggregator(node_id="mac1")
        ok = agg.record_raw(b"not-json{{")
        assert ok is False
        assert agg.stats()["superpowers_phase_receive_errors"] == 1

    def test_aggregator_rejects_missing_required_keys(self):
        agg = SuperpowersPhaseAggregator(node_id="mac1")
        # Missing node_id.
        assert agg.record({"phase": "executing-plans"}) is False
        # Missing phase.
        assert agg.record({"node_id": "mac2"}) is False
        assert agg.stats()["superpowers_phase_receive_errors"] == 2


# ════════════════════════════════════════════════════════════════════════
# 3. Subscriber populates fleet_phase_states correctly
# ════════════════════════════════════════════════════════════════════════


class TestAggregatorPopulatesFleetPhaseStates:
    @pytest.mark.asyncio
    async def test_aggregator_records_published_event(self, bus):
        nats = _FakeNATS()
        agg = SuperpowersPhaseAggregator(node_id="mac3")
        await agg.subscribe(nats)

        publisher = SuperpowersPhasePublisher(
            node_id="mac2", nats_publish=nats.publish
        )
        await publisher.publish_async(
            "executing-plans", "writing-plans",
            session_id="sess-1",
            skill_chain=["writing-plans"],
        )

        snap = agg.snapshot()
        assert "mac2" in snap
        assert snap["mac2"]["phase"] == "executing-plans"
        assert snap["mac2"]["prev_phase"] == "writing-plans"
        assert snap["mac2"]["session_id"] == "sess-1"
        assert snap["mac2"]["skill_chain"] == ["writing-plans"]
        assert snap["mac2"]["age_s"] >= 0

    @pytest.mark.asyncio
    async def test_multiple_nodes_tracked_independently(self, bus):
        nats = _FakeNATS()
        agg = SuperpowersPhaseAggregator(node_id="mac3")
        await agg.subscribe(nats)

        # Three nodes report different phases.
        for node, phase in [
            ("mac1", "brainstorming"),
            ("mac2", "executing-plans"),
            ("mac3", "writing-plans"),
        ]:
            pub = SuperpowersPhasePublisher(node_id=node, nats_publish=nats.publish)
            await pub.publish_async(phase, "unknown")

        snap = agg.snapshot()
        assert set(snap.keys()) == {"mac1", "mac2", "mac3"}
        assert snap["mac1"]["phase"] == "brainstorming"
        assert snap["mac2"]["phase"] == "executing-plans"
        assert snap["mac3"]["phase"] == "writing-plans"

    @pytest.mark.asyncio
    async def test_latest_event_supersedes_older(self, bus):
        nats = _FakeNATS()
        agg = SuperpowersPhaseAggregator(node_id="mac3")
        await agg.subscribe(nats)

        pub = SuperpowersPhasePublisher(node_id="mac2", nats_publish=nats.publish)
        await pub.publish_async("brainstorming", "unknown", ts=100.0)
        await pub.publish_async("writing-plans", "brainstorming", ts=200.0)
        await pub.publish_async("executing-plans", "writing-plans", ts=300.0)

        snap = agg.snapshot()
        assert snap["mac2"]["phase"] == "executing-plans"
        # History keeps the older entries for forensics.
        history = agg.history_for("mac2")
        phases = [e["phase"] for e in history]
        assert phases == ["brainstorming", "writing-plans", "executing-plans"]

    @pytest.mark.asyncio
    async def test_subscribe_uses_wildcard_subject(self, bus):
        """Aggregator must subscribe to every node — not just its own."""
        nats = _FakeNATS()
        agg = SuperpowersPhaseAggregator(node_id="mac3")
        await agg.subscribe(nats)
        assert any(
            sub_subject == subject_pattern_all()
            for sub_subject, _ in nats._handlers
        )

    @pytest.mark.asyncio
    async def test_age_s_increases_with_time(self, bus):
        agg = SuperpowersPhaseAggregator(node_id="mac3")
        agg.record({
            "node_id": "mac2",
            "phase": "brainstorming",
            "prev_phase": "unknown",
            "ts": time.time() - 5.0,
        })
        snap = agg.snapshot()
        assert snap["mac2"]["age_s"] >= 4

    @pytest.mark.asyncio
    async def test_end_to_end_adapter_to_aggregator(self, bus):
        """One node's adapter publishes; another node's aggregator sees the phase.

        This is the scenario the task spec asks for: mac2's BLOCKING phase
        becomes visible to mac1/mac3 so they can gate on it.
        """
        nats = _FakeNATS()
        loop = asyncio.get_running_loop()

        # mac3's daemon side: subscribe to the wildcard subject.
        agg = SuperpowersPhaseAggregator(node_id="mac3")
        await agg.subscribe(nats)

        # mac2's adapter side: wire publisher to NATS, change phase.
        set_phase_publisher_transport(nats.publish, loop)
        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming")  # BLOCKING
        await asyncio.sleep(0.05)

        snap = agg.snapshot()
        publisher_node = get_phase_publisher().node_id
        assert publisher_node in snap
        assert snap[publisher_node]["phase"] == "brainstorming"
        assert snap[publisher_node]["prev_phase"] == "unknown"

        # A peer reading this snapshot can now decide: BLOCKING means hold.
        peer_phase = snap[publisher_node]["phase"]
        assert peer_phase in SuperpowersAdapter.BLOCKING_PHASES


# ── pytest-asyncio config (avoid requiring a project-wide pytest.ini) ──

def pytest_collection_modifyitems(config, items):
    """Ensure every async test gets the asyncio marker.

    Some forks of pytest-asyncio require either auto mode or explicit
    marking. Marking here keeps the test file self-contained.
    """
    for item in items:
        if asyncio.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio)
