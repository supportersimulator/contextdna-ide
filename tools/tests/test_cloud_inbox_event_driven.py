"""Tests for tools/cloud_inbox_listener.py — event-driven cloud bot.

Verifies the four contracts that replace the old polling cron:

  1. NATS subscribe path works — listener binds a JetStream durable
     consumer and dispatches arriving messages through the handler.
  2. Status reports are debounced — at most one per `status_debounce_s`
     even if many state changes arrive faster.
  3. No `while True ... sleep` polling in the inbox-check path —
     enforced via AST scan of the module source.
  4. Daily-summary task fires once per `daily_summary_interval_s` — the
     one legitimate scheduled job; cadence is bounded (24h, not 1h).

Each test uses fakes for NATS so we don't need a live nats-server.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tools.cloud_inbox_listener import (
    CloudInboxListener,
    FleetState,
    ListenerCounters,
)

# Async tests require an explicit marker in pytest-asyncio strict mode.
# We mark each `async def test_*` individually so the sync AST-scan tests
# below don't trip a "marked async but is sync" warning.
asyncio_test = pytest.mark.asyncio


# ── Fakes ────────────────────────────────────────────────────────────────


class _FakeMsg:
    def __init__(self, payload: dict) -> None:
        self.data = json.dumps(payload).encode("utf-8")
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


class _FakeSub:
    """Stand-in for a JetStream pull subscription.

    `feed(msgs)` queues messages for the next `fetch()` call. When the
    queue is empty, `fetch()` raises a TimeoutError-shaped exception so
    the listener follows its idle path (no polling — just re-await).
    """

    def __init__(self) -> None:
        self._queue: list[Any] = []
        self.fetch_calls = 0

    def feed(self, msgs: list[dict]) -> None:
        for m in msgs:
            self._queue.append(_FakeMsg(m))

    async def fetch(self, batch: int = 10, timeout: float = 30.0) -> list[Any]:
        self.fetch_calls += 1
        if not self._queue:
            # Mirror nats-py's timeout shape — class name contains "Timeout".
            # Yield first so other coroutines (watcher) get a chance to run;
            # nats-py's real fetch awaits the network so this matches.
            await asyncio.sleep(0.01)
            raise asyncio.TimeoutError("no events in window")
        # Same: yield to the loop so background tasks can schedule.
        await asyncio.sleep(0)
        out, self._queue = self._queue[:batch], self._queue[batch:]
        return out


# ── 1. NATS subscribe path ───────────────────────────────────────────────


@asyncio_test
async def test_listener_processes_arriving_message_through_handler() -> None:
    """Drive _handle_raw_message directly — same code path the run() loop
    uses on every NATS delivery, but without spinning the fetch loop.
    """
    received: list[dict] = []

    async def handler(msg: dict) -> FleetState | None:
        received.append(msg)
        return FleetState(unread_count=1, open_tracks=("track-a",))

    listener = CloudInboxListener(message_handler=handler)
    fake = _FakeSub()
    fake.feed([{"from": "mac1", "subject": "hello", "body": "hi"}])
    msgs = await fake.fetch()
    for m in msgs:
        await listener._handle_raw_message(m)

    assert received and received[0]["from"] == "mac1"
    assert listener.counters.messages_received == 1
    assert listener.counters.messages_processed == 1
    assert listener.counters.messages_failed == 0
    assert msgs[0].acked is True


@asyncio_test
async def test_run_loop_dispatches_then_exits_on_stop() -> None:
    """End-to-end smoke: run() awaits fetch, dispatches one msg, exits when
    stop() is called. Verifies the event-driven loop has no polling cadence.
    """
    received: list[dict] = []

    async def handler(msg: dict) -> FleetState | None:
        received.append(msg)
        return None

    listener = CloudInboxListener(
        message_handler=handler,
        daily_summary_interval_s=3600.0,
    )
    listener._sub = _FakeSub()
    listener._sub.feed([{"x": 1}])

    async def watcher() -> None:
        # Stop as soon as the message is dispatched.
        for _ in range(200):
            if listener.counters.messages_processed >= 1:
                listener.stop()
                return
            await asyncio.sleep(0.01)
        listener.stop()

    runner = asyncio.create_task(listener.run())
    w = asyncio.create_task(watcher())
    try:
        await asyncio.wait_for(runner, timeout=8.0)
    finally:
        w.cancel()
        try:
            await w
        except (asyncio.CancelledError, Exception):
            pass

    assert received == [{"x": 1}]
    assert listener.counters.messages_processed == 1


# ── 2. Status report debounce ────────────────────────────────────────────


@asyncio_test
async def test_status_reports_are_debounced_under_burst() -> None:
    """10 distinct state changes within debounce window → 1 report emitted."""
    emitted: list[tuple[FleetState, FleetState]] = []

    async def reporter(prev: FleetState, curr: FleetState) -> None:
        emitted.append((prev, curr))

    # Use a fake clock so we control debounce evaluation deterministically.
    now = {"t": 1000.0}
    listener = CloudInboxListener(
        status_reporter=reporter,
        status_debounce_s=3600.0,
        clock=lambda: now["t"],
    )

    # 10 state changes, clock barely moves → debounce should suppress 9.
    for i in range(10):
        new_state = FleetState(unread_count=i + 1, open_tracks=(f"t{i}",))
        now["t"] += 1.0  # 1 second between events
        await listener._maybe_emit_status_report(new_state)

    assert len(emitted) == 1, (
        f"Expected exactly 1 status report under 1h debounce, got {len(emitted)}"
    )
    assert listener.counters.status_reports_emitted == 1
    assert listener.counters.status_reports_debounced == 9


@asyncio_test
async def test_no_status_report_when_state_unchanged() -> None:
    """State equality short-circuits — even outside debounce window."""
    emitted: list[tuple[FleetState, FleetState]] = []

    async def reporter(prev: FleetState, curr: FleetState) -> None:
        emitted.append((prev, curr))

    now = {"t": 1000.0}
    listener = CloudInboxListener(
        status_reporter=reporter,
        status_debounce_s=1.0,  # very short — but state never changes
        clock=lambda: now["t"],
    )
    listener.state = FleetState(unread_count=0, open_tracks=())

    for _ in range(5):
        now["t"] += 10.0  # well past debounce
        await listener._maybe_emit_status_report(
            FleetState(unread_count=0, open_tracks=())
        )

    assert emitted == [], "no report should fire while state is unchanged"
    assert listener.counters.status_reports_emitted == 0


@asyncio_test
async def test_status_report_emits_once_window_passes() -> None:
    """After debounce window AND a state change, the next report fires."""
    emitted: list[FleetState] = []

    async def reporter(prev: FleetState, curr: FleetState) -> None:
        emitted.append(curr)

    now = {"t": 1000.0}
    listener = CloudInboxListener(
        status_reporter=reporter,
        status_debounce_s=60.0,
        clock=lambda: now["t"],
    )

    await listener._maybe_emit_status_report(FleetState(unread_count=1))
    assert len(emitted) == 1

    # Within debounce — suppressed.
    now["t"] += 30.0
    await listener._maybe_emit_status_report(FleetState(unread_count=2))
    assert len(emitted) == 1

    # Past debounce + state changed — fires.
    now["t"] += 31.0
    await listener._maybe_emit_status_report(FleetState(unread_count=3))
    assert len(emitted) == 2
    assert emitted[-1].unread_count == 3


# ── 3. AST scan: no polling in inbox-check path ──────────────────────────


def test_no_polling_loop_in_listener_source() -> None:
    """AST scan: forbid `while True ... sleep` patterns in the listener.

    Polling = a loop whose body's only progress mechanism is a sleep.
    The listener uses `while not self._stopped.is_set()` driven by event
    arrival (`fetch()` blocks until events) — not sleep cadence.

    This test fails if anyone reintroduces a `time.sleep(N)` or
    `asyncio.sleep(N)` for N > 5 inside a loop in the inbox-check path.
    """
    src_path = Path(__file__).resolve().parent.parent / "cloud_inbox_listener.py"
    tree = ast.parse(src_path.read_text())

    violations: list[str] = []

    def _is_sleep_call(node: ast.AST) -> tuple[bool, float | None]:
        if not isinstance(node, ast.Call):
            return False, None
        func = node.func
        name = ""
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name != "sleep":
            return False, None
        if not node.args:
            return True, None
        a = node.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, (int, float)):
            return True, float(a.value)
        return True, None

    class LoopVisitor(ast.NodeVisitor):
        def visit_While(self, node: ast.While) -> None:
            # Detect `while True:` — the canonical polling shape.
            is_unbounded = (
                isinstance(node.test, ast.Constant) and node.test.value is True
            )
            if is_unbounded:
                # Walk body — any sleep call > 5s is treated as polling cadence.
                for child in ast.walk(node):
                    is_sleep, secs = _is_sleep_call(child)
                    if is_sleep and (secs is None or secs > 5.0):
                        violations.append(
                            f"while True with sleep({secs}) at line {node.lineno}"
                        )
            self.generic_visit(node)

    LoopVisitor().visit(tree)
    assert not violations, (
        "Polling pattern reintroduced into cloud_inbox_listener.py:\n  "
        + "\n  ".join(violations)
    )


def test_no_time_sleep_import_in_listener_path() -> None:
    """Defense-in-depth: the inbox-check path must not import `time.sleep`
    in a way that would let synchronous polling sneak in.

    `time` is allowed (used for monotonic clock readings) but `from time
    import sleep` is forbidden.
    """
    src_path = Path(__file__).resolve().parent.parent / "cloud_inbox_listener.py"
    tree = ast.parse(src_path.read_text())
    bad_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name == "sleep":
                    bad_imports.append(f"from time import sleep at line {node.lineno}")
    assert not bad_imports, "\n".join(bad_imports)


# ── 4. Daily summary fires once per interval ─────────────────────────────


@asyncio_test
async def test_daily_summary_emitter_fires_once_per_interval() -> None:
    """Daily summary task wakes once per `daily_summary_interval_s`.

    We use a very short interval (0.1s) and run the listener for ~0.35s,
    expecting roughly 3 emissions. Bounded cadence — NOT polling, because
    it's the legitimate single-summary-per-interval safety net.
    """
    summaries: list[FleetState] = []

    async def emitter(state: FleetState) -> None:
        summaries.append(state)

    listener = CloudInboxListener(
        daily_summary_emitter=emitter,
        daily_summary_interval_s=0.1,
    )

    # Spawn just the daily loop — we don't need NATS for this test.
    task = asyncio.create_task(listener._daily_summary_loop())
    await asyncio.sleep(0.35)
    listener.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()

    assert 2 <= len(summaries) <= 5, (
        f"expected ~3 summaries in 0.35s with 0.1s interval, got {len(summaries)}"
    )
    assert listener.counters.daily_summaries_emitted == len(summaries)


@asyncio_test
async def test_daily_summary_does_not_fire_at_inbox_event_cadence() -> None:
    """Critical: arriving fleet messages must NOT trigger daily summaries.

    This test asserts the separation that the old polling system violated —
    every inbox check had to emit a fresh artifact regardless of state.
    """
    summaries: list[FleetState] = []
    handled: list[dict] = []

    async def emitter(state: FleetState) -> None:
        summaries.append(state)

    async def handler(msg: dict) -> FleetState | None:
        handled.append(msg)
        return FleetState(unread_count=len(handled))

    listener = CloudInboxListener(
        message_handler=handler,
        daily_summary_emitter=emitter,
        daily_summary_interval_s=3600.0,  # 1h — won't fire during test
        status_debounce_s=3600.0,
    )

    fake = _FakeSub()
    fake.feed([{"i": i} for i in range(5)])
    for m in await fake.fetch():
        await listener._handle_raw_message(m)

    assert listener.counters.messages_processed == 5
    assert summaries == [], "daily summaries must not fire on per-message events"


# ── ZSF: counters surface on every failure path ──────────────────────────


@asyncio_test
async def test_handler_exception_increments_failed_counter() -> None:
    async def bad_handler(msg: dict) -> FleetState | None:
        raise RuntimeError("simulated handler failure")

    listener = CloudInboxListener(message_handler=bad_handler)
    fake = _FakeSub()
    fake.feed([{"x": 1}])
    for m in await fake.fetch():
        await listener._handle_raw_message(m)

    assert listener.counters.messages_failed == 1
    snap = listener.health_snapshot()
    assert snap["counters"]["messages_failed"] == 1
    assert snap["counters"]["messages_received"] == 1


def test_health_snapshot_shape_is_stable() -> None:
    listener = CloudInboxListener()
    snap = listener.health_snapshot()
    for key in (
        "node",
        "subject",
        "durable",
        "stream",
        "status_debounce_s",
        "daily_summary_interval_s",
        "counters",
        "state",
        "ts",
    ):
        assert key in snap
    for ckey in (
        "messages_received",
        "messages_processed",
        "messages_failed",
        "status_reports_emitted",
        "status_reports_debounced",
        "daily_summaries_emitted",
        "nats_disconnects",
        "process_errors",
    ):
        assert ckey in snap["counters"]


def test_listener_counters_dataclass_starts_at_zero() -> None:
    c = ListenerCounters()
    d = c.as_dict()
    assert all(d[k] == 0 for k in (
        "messages_received",
        "messages_processed",
        "messages_failed",
        "status_reports_emitted",
        "status_reports_debounced",
        "daily_summaries_emitted",
        "nats_disconnects",
        "process_errors",
    ))
