"""Controlled-failure test for memory/webhook_health_publisher.py ZSF compliance.

Per Round 8 finding (mac1, commit 171915da): race/o3's ZSF claim was
technically false because _build_async_publish raised inside a background
asyncio coroutine and _dispatch only saw schedule-success without seeing
the actual failure. Round 8 fixed this by passing publisher_ref into
_build_async_publish so failures bump publish_errors via _record_error.

This test PROVES the fix and guards against regression.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest


def test_async_publish_failure_increments_publish_errors():
    """Controlled-failure proof for Round 8 ZSF fix.

    Builds a publisher with a transport that fails in the background loop.
    Pre-Round-8 the failure was swallowed silently. Round 8 wires
    _record_error("background-publish", exc) so publish_errors increments.

    Test fails if Round 8 reverts.
    """
    try:
        from memory.webhook_health_publisher import _build_async_publish
        from tools.webhook_health_bridge import WebhookHealthPublisher
    except ImportError:
        pytest.skip("publisher modules not importable in this env")

    pub = WebhookHealthPublisher(node_id="test-node")
    if not hasattr(pub, "publish_errors"):
        pytest.skip("publisher has no publish_errors counter (pre-Round-8)")

    # Async publish callable pointing at unreachable port — guaranteed failure.
    # publisher_ref is a CALLABLE returning the publisher (per Round 8 contract).
    publish_callable = _build_async_publish(
        nats_url="nats://127.0.0.1:1",  # port 1 = unreachable
        publisher_ref=lambda: pub,
    )

    # Background loop in dedicated thread so the publish coroutine has somewhere
    # to actually run + raise.
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    try:
        starting_errors = pub.publish_errors
        pub.set_transport(publish_callable, loop)
        pub.publish(total_ms=100, sections={"section_0": {"latency_ms": 10, "ok": True}})
        # Wait for background failure to record
        for _ in range(50):  # 5s budget
            if pub.publish_errors > starting_errors:
                break
            time.sleep(0.1)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()

    assert pub.publish_errors > starting_errors, (
        f"ZSF regression: publish_errors stayed at {starting_errors} after "
        f"controlled async-publish failure. Round 8 fix (commit 171915da) "
        f"may have been reverted."
    )


def test_zsf_publisher_has_publish_errors_counter():
    """Round 8 invariant: publisher exposes publish_errors counter.

    If a future refactor removes this counter, ZSF compliance for the
    background-publish path becomes unobservable again. This guard fails
    loudly so the regression is caught at PR time.
    """
    try:
        from tools.webhook_health_bridge import WebhookHealthPublisher
    except ImportError:
        pytest.skip("publisher not importable")
    pub = WebhookHealthPublisher(node_id="test-node")
    assert hasattr(pub, "publish_errors"), (
        "ZSF requires publish_errors counter — Round 8 invariant (commit 171915da)"
    )
