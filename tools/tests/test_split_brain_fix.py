"""
TT4 / R4 — Tests for the mac1-class split-brain detector + recovery.

Reproduces the canonical mac1 syndrome:
  - `nc.publish('fleet.heartbeat', ...)` returns success every tick
  - `_peers` stays at {} because no peer heartbeat lands
  - JetStream cluster still sees the node (peers_jetstream_only ticks up)

Without R4: this state is silent — only the union in `/health.peers`
hides it visually. R4 detects it and forces a `nc.close()` so the
connect-retry loop rebinds subs.

Tests:
  * Healthy node never trips the detector (peers visible → silent_ticks=0).
  * Silent-isolation for `threshold_ticks` consecutive ticks fires the
    detector and increments `split_brain_detected_total` +
    `split_brain_reconnect_total`.
  * Reconnect path closes the existing client and sets `self.nc = None`
    (the connect-retry loop's contract).
  * Single-node deploys (no expected peers) never fire — minimal-fix scope.

Pure-Python: no NATS, no event loop, no daemon — patches the publish
loop's split-brain hook onto a stub.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _new_nerve(peers_config=None, node_id="mac1"):
    """Build a FleetNerveNATS without running __init__."""
    with patch("tools.fleet_nerve_nats.nats"):
        from tools.fleet_nerve_nats import FleetNerveNATS
        nerve = FleetNerveNATS.__new__(FleetNerveNATS)
        nerve.node_id = node_id
        nerve._peers = {}
        nerve._peers_config = peers_config if peers_config is not None else {
            "mac1": {"ip": "10.0.0.1"},
            "mac2": {"ip": "10.0.0.2"},
            "mac3": {"ip": "10.0.0.3"},
        }
        nerve._stats = {
            "split_brain_detected_total": 0,
            "split_brain_reconnect_total": 0,
            "split_brain_reconnect_errors": 0,
        }
        nerve._split_brain_silent_ticks = 0
        nerve._split_brain_threshold_ticks = 3
        # Stub the NATS client with a close() coroutine.
        nerve.nc = SimpleNamespace(close=AsyncMock())
        return nerve


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ══════════════════════════════════════════════════════════════
# Healthy node never trips the detector
# ══════════════════════════════════════════════════════════════


class TestHealthyNode:
    def test_visible_peers_keep_silent_ticks_zero(self):
        nerve = _new_nerve()
        # Simulate a peer heartbeat landing in _peers.
        nerve._peers["mac2"] = {"_received_at": 1.0}
        for _ in range(10):
            _run(nerve._maybe_split_brain_recover())
        assert nerve._split_brain_silent_ticks == 0
        assert nerve._stats["split_brain_detected_total"] == 0
        assert nerve._stats["split_brain_reconnect_total"] == 0

    def test_partial_visibility_still_counts_as_healthy(self):
        nerve = _new_nerve()
        # Only one out of two expected peers visible — still > 0 peers,
        # so we are NOT silent-isolated.
        nerve._peers["mac3"] = {"_received_at": 1.0}
        for _ in range(10):
            _run(nerve._maybe_split_brain_recover())
        assert nerve._stats["split_brain_detected_total"] == 0


# ══════════════════════════════════════════════════════════════
# Silent-isolation triggers the detector
# ══════════════════════════════════════════════════════════════


class TestSilentIsolation:
    def test_threshold_fires_detector_and_forces_reconnect(self):
        nerve = _new_nerve()
        # _peers is empty + we have expected peers → silent-isolation.
        # Tick exactly `threshold_ticks` times.
        for _ in range(nerve._split_brain_threshold_ticks):
            _run(nerve._maybe_split_brain_recover())
        assert nerve._stats["split_brain_detected_total"] == 1
        assert nerve._stats["split_brain_reconnect_total"] == 1
        # nc was closed and replaced with None per the connect-retry contract.
        assert nerve.nc is None

    def test_below_threshold_does_not_fire(self):
        nerve = _new_nerve()
        nerve._split_brain_threshold_ticks = 10
        for _ in range(3):
            _run(nerve._maybe_split_brain_recover())
        assert nerve._stats["split_brain_detected_total"] == 0
        assert nerve._split_brain_silent_ticks == 3

    def test_peer_arrival_resets_silent_ticks(self):
        nerve = _new_nerve()
        # Build up some silent ticks first.
        _run(nerve._maybe_split_brain_recover())
        _run(nerve._maybe_split_brain_recover())
        assert nerve._split_brain_silent_ticks == 2
        # A peer heartbeat arrives → reset.
        nerve._peers["mac2"] = {"_received_at": 1.0}
        _run(nerve._maybe_split_brain_recover())
        assert nerve._split_brain_silent_ticks == 0


# ══════════════════════════════════════════════════════════════
# Minimal-fix scope: single-node deploys never fire
# ══════════════════════════════════════════════════════════════


class TestSingleNodeDeploy:
    def test_no_expected_peers_never_fires(self):
        # Only self in peers_config → no peers expected → detector noop.
        nerve = _new_nerve(peers_config={"mac1": {"ip": "10.0.0.1"}}, node_id="mac1")
        for _ in range(20):
            _run(nerve._maybe_split_brain_recover())
        assert nerve._stats["split_brain_detected_total"] == 0
        assert nerve._stats["split_brain_reconnect_total"] == 0
        assert nerve._split_brain_silent_ticks == 0


# ══════════════════════════════════════════════════════════════
# Reconnect-path observability — failures surface, not swallow
# ══════════════════════════════════════════════════════════════


class TestReconnectErrorPath:
    def test_close_failure_bumps_reconnect_errors(self):
        nerve = _new_nerve()
        # Make nc.close raise — the daemon must keep going but record the
        # failure in `split_brain_reconnect_errors`.
        async def _boom():
            raise RuntimeError("simulated close failure")
        nerve.nc.close = _boom
        for _ in range(nerve._split_brain_threshold_ticks):
            _run(nerve._maybe_split_brain_recover())
        assert nerve._stats["split_brain_detected_total"] == 1
        assert nerve._stats["split_brain_reconnect_errors"] >= 1


# ══════════════════════════════════════════════════════════════
# /health.zsf_counters surfaces the split_brain block
# ══════════════════════════════════════════════════════════════


class TestZsfCountersSurfacing:
    def test_split_brain_block_present_with_thresholds(self):
        nerve = _new_nerve()
        nerve._cascade_skipped_infeasible = {}
        out = nerve._build_zsf_counters()
        sb = out["split_brain"]
        assert "detected_total" in sb
        assert "reconnect_total" in sb
        assert "reconnect_errors" in sb
        assert sb["threshold_ticks"] == nerve._split_brain_threshold_ticks
        assert sb["silent_ticks_current"] == 0
