"""RACE R3 — JetStream replay-on-reconnect wiring tests.

Verifies:
1. ``_ensure_durable_consumers`` creates one durable per FLEET_MESSAGES /
   FLEET_EVENTS, named via ``durable_consumer_name(stream, node_id)``.
2. ``_replay_missed_on_reconnect`` calls ``replay_missed`` on every
   established durable subscription and routes each message through the
   normal ``_handle_message`` path.
3. The ``messages_replayed_on_reconnect`` counter increments by the total
   delivered count across both streams.
4. ``_on_reconnect`` invokes ``_replay_missed_on_reconnect`` after the
   re-subscribe step (so a NATS disconnect → reconnect cycle drains the
   missed-message queue).
5. ZSF: failures during replay increment ``replay_on_reconnect_errors``
   instead of crashing the reconnect handler.

These tests avoid spinning up nats-server by mocking the JetStream context
and the durable subscription objects. ``replay_missed`` is the real
implementation from ``multifleet.jetstream`` so the contract between
daemon and module is exercised end-to-end.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FLEET_ROOT = REPO_ROOT / "multi-fleet"
for p in (REPO_ROOT, FLEET_ROOT):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


# ── Fixtures ────────────────────────────────────────────────────────


def _fake_jetstream_msg(payload: bytes, subject: str = "fleet.test.message"):
    """Mimic a nats.js.api.Msg enough for replay_missed + _handle_message."""
    m = MagicMock()
    m.data = payload
    m.subject = subject
    m.reply = ""
    m.ack = AsyncMock()
    m.nak = AsyncMock()
    return m


def _make_daemon(node_id: str = "mac-test"):
    """Construct a FleetNerveNATS without touching the network.

    Many subsystems (event bus, webhook bridge, peer config) lazy-init
    on the first call, so a bare construction is safe.
    """
    from tools.fleet_nerve_nats import FleetNerveNATS

    d = FleetNerveNATS(node_id=node_id, nats_url="nats://127.0.0.1:4222")
    return d


# ── 1. Durable consumer creation ────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_durable_consumers_creates_per_stream():
    """Daemon registers exactly one durable per stream with race/n3 names."""
    from multifleet import jetstream as js_mod

    d = _make_daemon("mac-r3a")
    d.js = MagicMock()  # JetStream context placeholder

    fake_msgs_sub = MagicMock(name="messages_sub")
    fake_events_sub = MagicMock(name="events_sub")

    created: list[tuple[str, str, str]] = []

    async def _fake_create(js, *, stream, durable_name, subject, **_kw):
        created.append((stream, durable_name, subject))
        return fake_msgs_sub if stream == js_mod.STREAM_MESSAGES else fake_events_sub

    with patch("multifleet.jetstream.create_durable_consumer", side_effect=_fake_create):
        await d._ensure_durable_consumers()

    # One consumer per stream, no duplicates.
    streams = {entry[0] for entry in created}
    assert streams == {js_mod.STREAM_MESSAGES, js_mod.STREAM_EVENTS}

    # Race/N3 naming pattern: {stream}_{node_id}
    for stream, durable, _subject in created:
        assert durable == js_mod.durable_consumer_name(stream, "mac-r3a")

    # Sub map populated with the right objects.
    assert d._durable_subs[js_mod.STREAM_MESSAGES] is fake_msgs_sub
    assert d._durable_subs[js_mod.STREAM_EVENTS] is fake_events_sub


@pytest.mark.asyncio
async def test_ensure_durable_consumers_idempotent():
    """Second call must not re-create existing consumers."""
    from multifleet import jetstream as js_mod

    d = _make_daemon("mac-r3b")
    d.js = MagicMock()
    sub = MagicMock(name="cached_sub")
    d._durable_subs[js_mod.STREAM_MESSAGES] = sub
    d._durable_subs[js_mod.STREAM_EVENTS] = sub

    with patch("multifleet.jetstream.create_durable_consumer") as mocked:
        await d._ensure_durable_consumers()
        mocked.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_durable_consumers_noop_without_jetstream():
    """No JS context → quietly skip, no errors, no map mutation."""
    d = _make_daemon("mac-r3c")
    d.js = None
    await d._ensure_durable_consumers()
    assert d._durable_subs == {}


# ── 2. Replay routing through _handle_message ──────────────────────


@pytest.mark.asyncio
async def test_replay_missed_routes_through_handle_message():
    """Each replayed message reaches _handle_message and is acked."""
    from multifleet import jetstream as js_mod

    d = _make_daemon("mac-r3d")
    d.js = MagicMock()

    # Pre-seed two streams with N missed messages each.
    msgs_payload = [_fake_jetstream_msg(b'{"i":%d}' % i) for i in range(3)]
    events_payload = [_fake_jetstream_msg(b'{"e":%d}' % i, subject="event.x") for i in range(2)]

    sub_msgs = MagicMock()
    sub_msgs.fetch = AsyncMock(side_effect=[msgs_payload, []])
    sub_events = MagicMock()
    sub_events.fetch = AsyncMock(side_effect=[events_payload, []])

    d._durable_subs = {
        js_mod.STREAM_MESSAGES: sub_msgs,
        js_mod.STREAM_EVENTS: sub_events,
    }

    handled: list[object] = []

    async def _capture(msg):
        handled.append(msg)

    d._handle_message = _capture  # type: ignore[assignment]

    total = await d._replay_missed_on_reconnect()

    # All 5 messages reached _handle_message.
    assert total == 5
    assert len(handled) == 5
    # All acked by replay_missed (handler did not raise).
    for m in msgs_payload + events_payload:
        m.ack.assert_awaited_once()
        m.nak.assert_not_called()


@pytest.mark.asyncio
async def test_replay_increments_messages_replayed_counter():
    """messages_replayed_on_reconnect must reflect total delivered count."""
    from multifleet import jetstream as js_mod

    d = _make_daemon("mac-r3e")
    d.js = MagicMock()

    msgs = [_fake_jetstream_msg(b'{"i":%d}' % i) for i in range(4)]
    sub = MagicMock()
    sub.fetch = AsyncMock(side_effect=[msgs, []])
    d._durable_subs = {js_mod.STREAM_MESSAGES: sub}

    d._handle_message = AsyncMock()

    before = d._stats.get("messages_replayed_on_reconnect", 0)
    runs_before = d._stats.get("replay_on_reconnect_runs", 0)

    total = await d._replay_missed_on_reconnect()

    assert total == 4
    assert d._stats["messages_replayed_on_reconnect"] == before + 4
    assert d._stats["replay_on_reconnect_runs"] == runs_before + 1


@pytest.mark.asyncio
async def test_replay_zero_when_no_missed_messages():
    """Empty fetch → counter not bumped, no error."""
    from multifleet import jetstream as js_mod

    d = _make_daemon("mac-r3f")
    d.js = MagicMock()
    sub = MagicMock()
    sub.fetch = AsyncMock(return_value=[])
    d._durable_subs = {js_mod.STREAM_MESSAGES: sub}
    d._handle_message = AsyncMock()

    before = d._stats.get("messages_replayed_on_reconnect", 0)
    total = await d._replay_missed_on_reconnect()
    assert total == 0
    assert d._stats["messages_replayed_on_reconnect"] == before


# ── 3. Reconnect cycle wiring ──────────────────────────────────────


@pytest.mark.asyncio
async def test_on_reconnect_triggers_replay():
    """Simulate a NATS disconnect→reconnect; replay must run."""
    d = _make_daemon("mac-r3g")
    d.js = MagicMock()
    # Stub the resubscribe path so we don't need real NATS.
    d._subscribe_all = AsyncMock()

    called = {"n": 0}

    async def _replay_stub():
        called["n"] += 1
        return 0

    d._replay_missed_on_reconnect = _replay_stub  # type: ignore[assignment]

    # _on_reconnect simulates the disconnect→reconnect callback nats-py
    # invokes after the underlying TCP/WS link is re-established.
    await d._on_reconnect()
    assert called["n"] == 1
    d._subscribe_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_reconnect_counts_replay_failure():
    """ZSF: replay exception must increment counter, not crash reconnect."""
    d = _make_daemon("mac-r3h")
    d.js = MagicMock()
    d._subscribe_all = AsyncMock()

    async def _broken():
        raise RuntimeError("simulated replay failure")

    d._replay_missed_on_reconnect = _broken  # type: ignore[assignment]

    before = d._stats.get("replay_on_reconnect_errors", 0)
    # Must NOT raise.
    await d._on_reconnect()
    assert d._stats["replay_on_reconnect_errors"] == before + 1


# ── 4. Real replay_missed contract (no daemon) ─────────────────────


@pytest.mark.asyncio
async def test_real_replay_missed_drains_and_acks():
    """Sanity guard the daemon depends on the real replay_missed shape."""
    from multifleet.jetstream import replay_missed

    msgs = [_fake_jetstream_msg(b'{"x":1}'), _fake_jetstream_msg(b'{"x":2}')]
    sub = MagicMock()
    sub.fetch = AsyncMock(side_effect=[msgs, []])

    handled: list[object] = []

    async def _h(msg):
        handled.append(msg)

    delivered = await replay_missed(sub, _h)
    assert delivered == 2
    assert len(handled) == 2
    for m in msgs:
        m.ack.assert_awaited_once()


# ── 5. Counter stat surface (regression) ───────────────────────────


def test_replay_counters_declared_in_stats():
    """Counters must exist in _stats at construction (visible in /health)."""
    d = _make_daemon("mac-r3i")
    for k in (
        "messages_replayed_on_reconnect",
        "replay_on_reconnect_runs",
        "replay_on_reconnect_errors",
    ):
        assert k in d._stats, f"_stats missing R3 counter {k!r}"
        assert d._stats[k] == 0


# ── 6. Full pre-seed → disconnect → reconnect cycle ────────────────


@pytest.mark.asyncio
async def test_full_disconnect_reconnect_cycle_replays_all():
    """End-to-end: pre-seed N missed messages, run reconnect, verify all
    routed via _handle_message and counter incremented exactly once.
    """
    from multifleet import jetstream as js_mod

    d = _make_daemon("mac-r3j")
    d.js = MagicMock()
    d._subscribe_all = AsyncMock()

    N = 7
    msgs = [_fake_jetstream_msg(b'{"i":%d}' % i) for i in range(N)]
    sub_msgs = MagicMock()
    sub_msgs.fetch = AsyncMock(side_effect=[msgs, []])
    sub_events = MagicMock()
    sub_events.fetch = AsyncMock(return_value=[])
    d._durable_subs = {
        js_mod.STREAM_MESSAGES: sub_msgs,
        js_mod.STREAM_EVENTS: sub_events,
    }

    seen = []

    async def _capture(msg):
        seen.append(msg)

    d._handle_message = _capture  # type: ignore[assignment]

    # Simulate disconnect → reconnect cycle.
    await d._on_disconnect()  # increments errors etc, must not raise
    await d._on_reconnect()

    assert len(seen) == N
    assert d._stats["messages_replayed_on_reconnect"] == N
    assert d._stats["replay_on_reconnect_runs"] == 1
    assert d._stats["replay_on_reconnect_errors"] == 0
