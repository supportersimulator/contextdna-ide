"""Y4 cleanup (2026-05-07) — verify concurrent-drop ZSF refinement.

Earlier this session, the publisher's "drop overlapping connect attempt" path
raised ``RuntimeError("nats-unavailable")`` and the log line was identical to
a real transport failure. That misled Atlas into believing the webhook was
broken.

Refinement:
  - Distinct exception class :class:`NatsConcurrentDropOK`.
  - Separate counter ``_nats_concurrent_drops`` (NOT ``publish_errors``).
  - Log at DEBUG, not ERROR/WARNING.
  - Caller can distinguish intentional drop from real failure.

This test spawns multiple concurrent ``_get_or_connect_nats`` calls and
verifies:
  1. Exactly one connect attempt proceeds; the others raise
     :class:`NatsConcurrentDropOK`.
  2. The concurrent-drops counter advances by N-1.
  3. ``publish_errors`` does NOT advance for the dropped calls (those are
     ZSF observable but not "errors").
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from memory import webhook_health_publisher as whp


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset module globals between tests so each runs in isolation."""
    whp.reset_concurrent_drops_for_test()
    # Force the connect-in-flight flag off in case a previous test left it on.
    whp._nats_connecting = False
    whp._nats_client = None
    yield
    whp.reset_concurrent_drops_for_test()
    whp._nats_connecting = False
    whp._nats_client = None


def test_concurrent_drops_use_distinct_exception(monkeypatch):
    """When 5 coroutines race the connect, 1 attempts and 4 raise NatsConcurrentDropOK.

    We monkeypatch ``nats.connect`` with a slow stub so the connecting
    coroutine actually holds the ``_nats_connecting`` flag long enough for
    the other 4 to see it.
    """

    # Stub nats.connect — return a fake client after a small delay so the
    # other 4 calls have a window in which `_nats_connecting=True`.
    class _FakeClient:
        is_closed = False

        async def publish(self, subject, data):  # pragma: no cover
            return None

    async def _slow_connect(*_a, **_kw):
        await asyncio.sleep(0.05)
        return _FakeClient()

    fake_nats = type("FakeNats", (), {"connect": staticmethod(_slow_connect)})

    import sys
    monkeypatch.setitem(sys.modules, "nats", fake_nats)

    async def _race():
        coros = [whp._get_or_connect_nats("nats://stub") for _ in range(5)]
        return await asyncio.gather(*coros, return_exceptions=True)

    results = asyncio.run(_race())

    # Exactly one should succeed (return a fake client), the rest should
    # raise NatsConcurrentDropOK. Because asyncio.gather schedules them
    # cooperatively on a single loop, the first to hit the `if
    # _nats_connecting` check sees False and proceeds; the others see True
    # and raise.
    successes = [r for r in results if isinstance(r, _FakeClient)]
    drops = [r for r in results if isinstance(r, whp.NatsConcurrentDropOK)]
    other_errors = [
        r for r in results
        if not isinstance(r, _FakeClient)
        and not isinstance(r, whp.NatsConcurrentDropOK)
    ]

    assert len(successes) == 1, f"expected 1 success, got {len(successes)}: {results}"
    assert len(drops) == 4, f"expected 4 concurrent-drops, got {len(drops)}: {results}"
    assert other_errors == [], f"unexpected other errors: {other_errors}"

    # Counter should reflect exactly 4 drops.
    assert whp.get_concurrent_drops_total() == 4


def test_concurrent_drop_does_not_bump_publish_errors(monkeypatch):
    """The concurrent-drop path bumps the dedicated counter, NOT publish_errors.

    Drives a publisher with a wired async transport built via
    ``_build_async_publish``, schedules N concurrent dispatches, and asserts:
      - publish_errors == 0 for the dropped ones (drops are not "errors").
      - concurrent_drops_total == N-1.
    """
    # Stub nats.connect so the first call holds the connect flag.
    class _FakeClient:
        is_closed = False
        publish_calls = 0

        async def publish(self, subject, data):
            type(self).publish_calls += 1

    async def _slow_connect(*_a, **_kw):
        await asyncio.sleep(0.05)
        return _FakeClient()

    fake_nats = type("FakeNats", (), {"connect": staticmethod(_slow_connect)})
    import sys
    monkeypatch.setitem(sys.modules, "nats", fake_nats)

    # Build a real publisher and wire it with the real _build_async_publish
    # (so the concurrent-drop path is exercised end-to-end).
    from tools.webhook_health_bridge import WebhookHealthPublisher

    pub = WebhookHealthPublisher(node_id="test-node")
    publish_callable = whp._build_async_publish(
        "nats://stub", publisher_ref=lambda: pub,
    )

    # We need a real loop the publisher can schedule onto.
    loop_holder = {}
    ready = threading.Event()

    def _run_loop():
        lp = asyncio.new_event_loop()
        loop_holder["loop"] = lp
        asyncio.set_event_loop(lp)
        ready.set()
        lp.run_forever()

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()
    ready.wait(timeout=2.0)
    loop = loop_holder["loop"]
    pub.set_transport(publish_callable, loop)

    # Schedule 5 publishes simultaneously. The publisher's _dispatch path
    # calls run_coroutine_threadsafe, which means each lands as a task on
    # the same loop. We can then wait for them all to complete.
    futures = []
    for _ in range(5):
        # Direct call into the dispatch path — sync, returns once scheduled.
        coro = publish_callable("event.webhook.completed.test-node", b"{}")
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        futures.append(fut)

    # Collect results — 1 should complete cleanly, 4 should raise
    # NatsConcurrentDropOK (re-raised by _publish so the loop sees it).
    successes = 0
    drops = 0
    other = 0
    for fut in futures:
        try:
            fut.result(timeout=2.0)
            successes += 1
        except whp.NatsConcurrentDropOK:
            drops += 1
        except Exception:  # noqa: BLE001 — tally unexpected
            other += 1

    loop.call_soon_threadsafe(loop.stop)

    assert successes == 1, f"expected 1 success, got {successes}"
    assert drops == 4, f"expected 4 concurrent-drops, got {drops}"
    assert other == 0, f"unexpected other failures: {other}"

    # publish_errors must NOT be bumped by the drops.
    assert pub.publish_errors == 0, (
        f"concurrent drops should not bump publish_errors, "
        f"got {pub.publish_errors}"
    )

    # Concurrent-drop counter must reflect the 4 drops.
    assert whp.get_concurrent_drops_total() == 4
