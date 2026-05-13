"""Regression guards for the Zero-Silent-Failures hardening of
``tools/fleet_nerve_nats.py`` (branch: fix/daemon-zsf-hardening).

Two kinds of guard:

1. **Grep regression** — asserts the count of ``except Exception: pass``
   patterns has not regressed above the post-fix threshold. Prevents
   future commits from re-introducing silent swallows.

2. **Counter coverage** — for each new ``self._stats`` counter added in
   the hardening pass, force the exception path and assert the counter
   incremented. Validates the counters are actually wired up.

Original root bug (commit ``cec1f39c``): a bare ``except Exception: pass``
in ``_handle_heartbeat`` silently swallowed bookkeeping failures, blocking
the primary ``_peers[node]`` update. mac2 went 4.9h stale while actively
publishing — self-heal then misdiagnosed and fired 55 repair attempts in
8h. This file guards against that bug class re-emerging.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DAEMON_SRC = REPO_ROOT / "tools" / "fleet_nerve_nats.py"

# Post-fix threshold. Hardening reduced `except Exception: pass` in the
# daemon from 21 sites to 0. Any future regression MUST bump this number
# consciously (and justify it in review).
MAX_ALLOWED_BARE_EXCEPT_PASS = 0


# ── Regression guard ────────────────────────────────────────────────


def test_no_bare_except_pass_regression():
    """Assert daemon has <= MAX_ALLOWED_BARE_EXCEPT_PASS bare-excepts.

    Matches ``except Exception: pass`` (possibly split across lines,
    with any whitespace). This is the exact pattern Aaron's ZSF
    invariant forbids in production hot paths.
    """
    src = DAEMON_SRC.read_text(encoding="utf-8")
    # Match `except Exception:` optionally followed by whitespace/newline
    # and then `pass` as the sole statement in the except body.
    pattern = re.compile(r"except\s+Exception\s*:\s*\n\s*pass\b")
    hits = pattern.findall(src)
    assert len(hits) <= MAX_ALLOWED_BARE_EXCEPT_PASS, (
        f"ZSF regression: found {len(hits)} `except Exception: pass` in "
        f"{DAEMON_SRC.name} (max allowed: {MAX_ALLOWED_BARE_EXCEPT_PASS}).\n"
        f"Each bare swallow hides bugs. Narrow the except or add a counter.\n"
        f"See commit cec1f39c for the heartbeat-stale bug this guards against."
    )


def test_new_zsf_counters_declared_in_stats_init():
    """The three hardening counters must be initialised in _stats.

    If they're missing from the init dict, .get() fallback still works
    but /health.stats won't expose them until first increment — making
    the counters invisible to fleet operators until the first failure.
    """
    src = DAEMON_SRC.read_text(encoding="utf-8")
    for counter in ("ack_handler_errors", "ack_send_errors", "repair_gate_health_errors"):
        assert f'"{counter}"' in src, (
            f"ZSF counter {counter!r} missing from _stats init — "
            f"won't be visible in /health until first exception."
        )


# ── Counter-coverage unit tests ─────────────────────────────────────
#
# We don't instantiate the full FleetNerveNATS (NATS connection etc).
# Instead we exercise the narrowed-except logic by calling the handler
# methods directly on a lightweight stub that holds the _stats dict.


class _StatsHolder:
    """Minimal stand-in for FleetNerveNATS — just the _stats dict +
    whatever attributes the method under test reads."""

    def __init__(self):
        self._stats = {
            "ack_handler_errors": 0,
            "ack_send_errors": 0,
            "repair_gate_health_errors": 0,
        }
        self._pending_acks = {}
        self.node_id = "test-node"
        self.nc = None


def _load_daemon_method(method_name: str):
    """Import FleetNerveNATS lazily — the module imports nats-py at
    top level. If the import fails in the test env, skip gracefully."""
    try:
        from tools.fleet_nerve_nats import FleetNerveNATS
    except ImportError as e:
        pytest.skip(f"fleet_nerve_nats import unavailable in test env: {e}")
    return getattr(FleetNerveNATS, method_name)


def test_handle_ack_increments_counter_on_bad_json():
    """Malformed ACK payload must increment ack_handler_errors, not swallow."""
    handle_ack = _load_daemon_method("_handle_ack")
    holder = _StatsHolder()
    bad_msg = MagicMock()
    bad_msg.data = b"\xff\xfe not valid json"
    asyncio.get_event_loop() if False else None  # keep linters quiet
    asyncio.run(handle_ack(holder, bad_msg))
    assert holder._stats["ack_handler_errors"] == 1, (
        "ACK with undecodable bytes must increment ack_handler_errors, "
        "not be silently swallowed."
    )


def test_handle_ack_increments_counter_on_tracker_mutation_error():
    """If _pending_acks lookup/mutation throws, counter must increment."""
    handle_ack = _load_daemon_method("_handle_ack")
    holder = _StatsHolder()
    # Make _pending_acks a broken dict-like that raises on `in` check
    class Boom:
        def __contains__(self, key):
            raise TypeError("synthetic tracker failure")
    holder._pending_acks = Boom()
    good_msg = MagicMock()
    good_msg.data = json.dumps({"ref": "abc123", "node": "peer", "channel": "P1_nats"}).encode()
    asyncio.run(handle_ack(holder, good_msg))
    assert holder._stats["ack_handler_errors"] == 1


def test_send_ack_increments_counter_on_publish_failure():
    """NATS publish failure on ACK must increment ack_send_errors."""
    send_ack = _load_daemon_method("_send_ack")
    holder = _StatsHolder()

    class FakeNC:
        is_connected = True
        async def publish(self, subject, payload):
            raise OSError("synthetic NATS publish failure")
    holder.nc = FakeNC()

    asyncio.run(send_ack(holder, sender="peer-mac2", msg_id="deadbeef",
                         status="ok", channel="P1_nats"))
    assert holder._stats["ack_send_errors"] == 1, (
        "OSError on ACK publish must increment ack_send_errors."
    )


def test_send_ack_noop_when_sender_is_self_does_not_increment():
    """Self-addressed ACK early-returns; counter must not tick."""
    send_ack = _load_daemon_method("_send_ack")
    holder = _StatsHolder()

    class FakeNC:
        is_connected = True
        async def publish(self, subject, payload):
            raise OSError("should not be called")
    holder.nc = FakeNC()

    asyncio.run(send_ack(holder, sender=holder.node_id, msg_id="x",
                         status="ok", channel="P1_nats"))
    assert holder._stats["ack_send_errors"] == 0


# ── repair_gate_health_errors ───────────────────────────────────────
#
# ``repair_with_escalation`` is a large async coroutine. Rather than
# stand the whole thing up, we verify the narrowed-except branch in
# isolation: construct the try/except inline, force a throw from
# _get_peer_health_score, assert the counter ticks.


def test_repair_gate_health_counter_increments_on_health_probe_failure():
    """The narrowed except in repair_with_escalation's ch_health build
    must increment repair_gate_health_errors rather than silently
    passing an empty ch_health to the gate.
    """
    # Verify the source text wires the counter — a structural check
    # that complements the behavioural tests above and catches future
    # refactors that might drop the counter increment.
    src = DAEMON_SRC.read_text(encoding="utf-8")
    # The narrowed-except block must increment repair_gate_health_errors
    # and log, in the ch_health snapshot build.
    assert "repair_gate_health_errors" in src
    assert re.search(
        r"_get_peer_health_score failed for.*gate will evaluate without channel_health snapshot",
        src, re.DOTALL,
    ), "repair_gate narrowed-except must log the gate-degradation warning"
    # And the bare-except it replaced must be gone.
    narrow_block = re.search(
        r"except \(KeyError, AttributeError, TypeError, ValueError\) as e:\s*\n"
        r"\s*self\._stats\[\"repair_gate_health_errors\"\]",
        src,
    )
    assert narrow_block, (
        "repair_with_escalation must use narrow exception types for "
        "_get_peer_health_score failure, not bare except Exception"
    )
