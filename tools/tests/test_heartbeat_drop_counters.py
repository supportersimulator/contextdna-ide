"""
TT4 / M2 — Tests for heartbeat-drop diagnostic counters.

Feeds bogus heartbeats through `FleetNerveNATS._handle_heartbeat` and asserts
the per-cause counters increment as the plan specifies:

  - heartbeat_received_total              (valid receive)
  - heartbeat_dropped_self_total          (self-heartbeat reflection)
  - heartbeat_dropped_unknown_node_total  (node == "unknown")
  - heartbeat_dropped_no_peer_cfg_total   (sender not in peers config)
  - heartbeat_dropped_stale_ts_total      (timestamp > 60s in the past)
  - heartbeat_parse_errors                (existing — non-JSON payload)
  - heartbeat_quorum_loss_events_total    (lost majority for >threshold s)

Also asserts the per-peer drop breakdown surfaces under
`_build_zsf_counters()['heartbeat_drops']['per_peer']` so dashboards see
which peer is producing bogus heartbeats.

Pure-Python: no NATS, no event loop, no daemon — patches the heartbeat
handler onto a stub instance.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _new_nerve(peers_config=None, node_id="mac2"):
    """Build a FleetNerveNATS without running __init__."""
    with patch("tools.fleet_nerve_nats.nats"):
        from tools.fleet_nerve_nats import FleetNerveNATS
        nerve = FleetNerveNATS.__new__(FleetNerveNATS)
        nerve.node_id = node_id
        nerve._peers = {}
        nerve._peers_config = peers_config or {
            "mac1": {"ip": "10.0.0.1"},
            "mac2": {"ip": "10.0.0.2"},
            "mac3": {"ip": "10.0.0.3"},
        }
        nerve._stats = {
            "heartbeat_received_total": 0,
            "heartbeat_dropped_self_total": 0,
            "heartbeat_dropped_unknown_node_total": 0,
            "heartbeat_dropped_no_peer_cfg_total": 0,
            "heartbeat_dropped_stale_ts_total": 0,
            "heartbeat_quorum_loss_events_total": 0,
            "heartbeat_parse_errors": 0,
        }
        nerve._heartbeat_drops_by_peer = {}
        nerve._last_known_peer_count = 1
        nerve._split_brain_silent_ticks = 0
        nerve._split_brain_threshold_ticks = 6
        nerve._quorum_loss_since = None
        nerve._quorum_loss_threshold_s = 5.0
        nerve._cascade_skipped_infeasible = {}
        # Disable secondary bookkeeping the handler touches.
        nerve.protocol = None
        nerve._state_sync = None
        nerve._repair_in_progress = {}
        return nerve


def _hb_msg(payload):
    """Wrap a dict as a NATS-msg-like object with `.data` (bytes)."""
    if isinstance(payload, (dict, list)):
        body = json.dumps(payload).encode()
    elif isinstance(payload, str):
        body = payload.encode()
    elif isinstance(payload, bytes):
        body = payload
    else:
        raise TypeError(type(payload))
    return SimpleNamespace(data=body)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ══════════════════════════════════════════════════════════════
# Per-cause drop counters
# ══════════════════════════════════════════════════════════════


class TestHeartbeatDropCounters:
    def test_valid_heartbeat_increments_received_total(self):
        nerve = _new_nerve()
        msg = _hb_msg({"node": "mac1", "timestamp": time.time()})
        _run(nerve._handle_heartbeat(msg))
        assert nerve._stats["heartbeat_received_total"] == 1
        assert "mac1" in nerve._peers

    def test_self_heartbeat_drops_with_named_counter(self):
        nerve = _new_nerve(node_id="mac2")
        msg = _hb_msg({"node": "mac2", "timestamp": time.time()})
        _run(nerve._handle_heartbeat(msg))
        assert nerve._stats["heartbeat_dropped_self_total"] == 1
        # Did NOT bump received-total (drop is before counted-as-received).
        assert nerve._stats["heartbeat_received_total"] == 0
        assert "mac2" not in nerve._peers

    def test_unknown_node_drops_with_named_counter(self):
        nerve = _new_nerve()
        # No "node" field — defaults to "unknown".
        msg = _hb_msg({"timestamp": time.time()})
        _run(nerve._handle_heartbeat(msg))
        assert nerve._stats["heartbeat_dropped_unknown_node_total"] == 1
        # Per-peer breakdown captured the drop under the "unknown" key.
        assert nerve._heartbeat_drops_by_peer["unknown"]["unknown_node"] == 1

    def test_no_peer_cfg_drops_unknown_sender(self):
        nerve = _new_nerve()
        msg = _hb_msg({"node": "lab-rat-99", "timestamp": time.time()})
        _run(nerve._handle_heartbeat(msg))
        assert nerve._stats["heartbeat_dropped_no_peer_cfg_total"] == 1
        assert nerve._heartbeat_drops_by_peer["lab-rat-99"]["no_peer_cfg"] == 1
        # Sender stays out of _peers (no fake topology pollution).
        assert "lab-rat-99" not in nerve._peers

    def test_stale_timestamp_drops_old_heartbeat(self):
        nerve = _new_nerve()
        stale = time.time() - 600.0  # 10 min ago
        msg = _hb_msg({"node": "mac1", "timestamp": stale})
        _run(nerve._handle_heartbeat(msg))
        assert nerve._stats["heartbeat_dropped_stale_ts_total"] == 1
        assert nerve._heartbeat_drops_by_peer["mac1"]["stale_ts"] == 1
        # Did NOT update _peers with the stale row.
        assert "mac1" not in nerve._peers

    def test_parse_error_bumps_existing_counter(self):
        nerve = _new_nerve()
        msg = _hb_msg(b"not valid json {")
        _run(nerve._handle_heartbeat(msg))
        assert nerve._stats["heartbeat_parse_errors"] == 1
        assert nerve._stats["heartbeat_received_total"] == 0


# ══════════════════════════════════════════════════════════════
# Per-peer breakdown surfaced under /health.zsf_counters
# ══════════════════════════════════════════════════════════════


class TestZsfCountersSurfacing:
    def test_zsf_counters_includes_heartbeat_drops_block(self):
        nerve = _new_nerve()
        # Trigger three distinct drop reasons.
        _run(nerve._handle_heartbeat(_hb_msg({"node": "lab-rat", "timestamp": time.time()})))  # no_peer_cfg
        _run(nerve._handle_heartbeat(_hb_msg({"node": "mac1", "timestamp": time.time() - 600})))  # stale_ts
        _run(nerve._handle_heartbeat(_hb_msg({"timestamp": time.time()})))  # unknown_node
        out = nerve._build_zsf_counters()
        hb = out["heartbeat_drops"]
        assert hb["dropped_no_peer_cfg_total"] == 1
        assert hb["dropped_stale_ts_total"] == 1
        assert hb["dropped_unknown_node_total"] == 1
        # Per-peer breakdown surfaces *which* peer is the troublemaker.
        assert hb["per_peer"]["lab-rat"]["no_peer_cfg"] == 1
        assert hb["per_peer"]["mac1"]["stale_ts"] == 1
        assert hb["per_peer"]["unknown"]["unknown_node"] == 1


# ══════════════════════════════════════════════════════════════
# Quorum-loss event counter (>threshold seconds w/o majority)
# ══════════════════════════════════════════════════════════════


class TestQuorumLossCounter:
    def test_quorum_loss_no_event_when_healthy(self):
        nerve = _new_nerve()
        # Seed mac1 + mac3 as visible (majority of {mac1,mac3} + self = 3/3).
        now = time.time()
        nerve._peers["mac1"] = {"_received_at": now}
        nerve._peers["mac3"] = {"_received_at": now}
        nerve._check_quorum_loss()
        assert nerve._stats["heartbeat_quorum_loss_events_total"] == 0
        assert nerve._quorum_loss_since is None

    def test_quorum_loss_fires_after_threshold(self):
        nerve = _new_nerve()
        # Tighten threshold so the test isn't slow.
        nerve._quorum_loss_threshold_s = 0.0  # fire immediately on second tick
        # No peers visible → we have only "self" out of 3-node fleet → no majority.
        nerve._check_quorum_loss()  # arms the timer
        assert nerve._quorum_loss_since is not None
        # Second tick crosses threshold (0.0) → event bumps.
        nerve._check_quorum_loss()
        assert nerve._stats["heartbeat_quorum_loss_events_total"] >= 1
