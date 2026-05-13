"""RACE Q4 — NATS bandwidth + message-size telemetry tests.

Covers the new ``_record_msg_size`` / ``_compute_msg_size_p95`` helpers and
their wiring into ``_build_health`` + ``_build_prometheus_metrics``.

The helpers are pure (no asyncio / NATS), so we exercise them off a minimal
SimpleNamespace holder that stands in for ``FleetNerveNATS`` — same pattern
as ``test_health_observability.py`` (RACE B6).
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest


def _bind(method_name: str):
    try:
        from tools.fleet_nerve_nats import FleetNerveNATS
    except ImportError as e:
        pytest.skip(f"fleet_nerve_nats import unavailable: {e}")
    return getattr(FleetNerveNATS, method_name)


def _constants():
    try:
        from tools import fleet_nerve_nats as mod
    except ImportError as e:
        pytest.skip(f"fleet_nerve_nats import unavailable: {e}")
    return mod


def _make_holder(tmp_path: Path | None = None):
    """Minimal stand-in for FleetNerveNATS exercising Q4 helpers."""
    mod = _constants()
    holder = SimpleNamespace()
    holder.node_id = "test-node"
    holder._stats = {
        "received": 0,
        "sent": 0,
        "broadcasts": 0,
        "errors": 0,
        "bytes_sent": 0,
        "bytes_received": 0,
        "largest_msg_bytes": 0,
        "large_msg_count": 0,
    }
    holder._msg_size_window = deque(maxlen=mod.MSG_SIZE_WINDOW)
    if tmp_path is not None:
        holder._delegation_queue_dir = tmp_path / "delegations"
        holder._build_delegation_queue = lambda: {"depth": 0, "status": "empty"}
        # _build_prometheus_metrics calls self._compute_msg_size_p95() — bind
        # the real helper so SimpleNamespace doesn't 500 the metrics output.
        compute = _bind("_compute_msg_size_p95")
        holder._compute_msg_size_p95 = lambda: compute(holder)
    return holder


# ── _record_msg_size ────────────────────────────────────────────────


def test_record_msg_size_increments_sent_counter():
    record = _bind("_record_msg_size")
    holder = _make_holder()
    record(holder, "sent", 100)
    record(holder, "sent", 250)
    assert holder._stats["bytes_sent"] == 350
    assert holder._stats["bytes_received"] == 0
    assert holder._stats["largest_msg_bytes"] == 250
    assert list(holder._msg_size_window) == [100, 250]


def test_record_msg_size_increments_received_counter():
    record = _bind("_record_msg_size")
    holder = _make_holder()
    record(holder, "received", 64)
    record(holder, "received", 1024)
    assert holder._stats["bytes_received"] == 1088
    assert holder._stats["bytes_sent"] == 0
    assert holder._stats["largest_msg_bytes"] == 1024


def test_record_msg_size_tracks_high_water_mark():
    record = _bind("_record_msg_size")
    holder = _make_holder()
    for n in (10, 500, 50, 800, 200):
        record(holder, "sent", n)
    # Largest must be a high-water gauge — never decreases on smaller writes.
    assert holder._stats["largest_msg_bytes"] == 800


def test_record_msg_size_warns_and_counts_large_message(caplog):
    record = _bind("_record_msg_size")
    mod = _constants()
    holder = _make_holder()
    big = mod.MSG_SIZE_WARN_BYTES + 1
    with caplog.at_level(logging.WARNING, logger="fleet-nerve"):
        record(holder, "sent", big)
    assert holder._stats["large_msg_count"] == 1
    # Smaller message must NOT trigger warning bookkeeping.
    record(holder, "sent", mod.MSG_SIZE_WARN_BYTES - 1)
    assert holder._stats["large_msg_count"] == 1


def test_record_msg_size_rejects_bogus_input():
    record = _bind("_record_msg_size")
    holder = _make_holder()
    record(holder, "sent", "not-a-number")
    record(holder, "sent", -5)
    # Bogus inputs must be observable, not silent.
    assert holder._stats.get("size_record_errors", 0) == 2
    assert holder._stats["bytes_sent"] == 0
    assert holder._stats["largest_msg_bytes"] == 0


def test_record_msg_size_window_is_bounded():
    record = _bind("_record_msg_size")
    mod = _constants()
    holder = _make_holder()
    for i in range(mod.MSG_SIZE_WINDOW + 50):
        record(holder, "sent", i + 1)
    # Rolling window must cap at MSG_SIZE_WINDOW even under sustained load.
    assert len(holder._msg_size_window) == mod.MSG_SIZE_WINDOW


# ── _compute_msg_size_p95 ───────────────────────────────────────────


def test_compute_msg_size_p95_empty_window_returns_zero():
    p95 = _bind("_compute_msg_size_p95")
    holder = _make_holder()
    assert p95(holder) == 0


def test_compute_msg_size_p95_known_distribution():
    record = _bind("_record_msg_size")
    p95 = _bind("_compute_msg_size_p95")
    holder = _make_holder()
    # Insert sizes 1..100; nearest-rank p95 over 100 samples → index 94 → 95.
    for n in range(1, 101):
        record(holder, "sent", n)
    assert p95(holder) == 95


def test_compute_msg_size_p95_single_sample():
    record = _bind("_record_msg_size")
    p95 = _bind("_compute_msg_size_p95")
    holder = _make_holder()
    record(holder, "sent", 42)
    assert p95(holder) == 42


# ── prometheus /metrics surface ─────────────────────────────────────


def test_prometheus_emits_q4_bandwidth_metrics(tmp_path):
    build = _bind("_build_prometheus_metrics")
    record = _bind("_record_msg_size")
    holder = _make_holder(tmp_path)
    record(holder, "sent", 200)
    record(holder, "received", 800)
    text = build(holder)
    assert 'fleet_nerve_bytes_sent{node="test-node"} 200' in text
    assert 'fleet_nerve_bytes_received{node="test-node"} 800' in text
    assert 'fleet_nerve_largest_msg_bytes{node="test-node"} 800' in text
    assert "# TYPE fleet_nerve_largest_msg_bytes gauge" in text
    assert "# TYPE fleet_nerve_msg_size_p95_bytes gauge" in text
    assert 'fleet_nerve_msg_size_p95_bytes{node="test-node"}' in text


def test_prometheus_large_msg_count_surfaces(tmp_path):
    build = _bind("_build_prometheus_metrics")
    record = _bind("_record_msg_size")
    mod = _constants()
    holder = _make_holder(tmp_path)
    record(holder, "sent", mod.MSG_SIZE_WARN_BYTES + 100)
    record(holder, "sent", mod.MSG_SIZE_WARN_BYTES + 200)
    text = build(holder)
    assert 'fleet_nerve_large_msg_count{node="test-node"} 2' in text


# ── source-level wiring guard (regressions in send paths) ───────────


def test_send_paths_record_msg_size_in_source():
    """Structural guard: every NATS publish path must call _record_msg_size.

    If a future refactor adds a new ``self.nc.publish(...)`` site without
    recording bytes, /metrics silently under-reports bandwidth — exactly
    the failure mode RACE Q4 exists to prevent. This test reads the source
    and asserts the helper appears alongside the three known publish paths.
    """
    module_path = (
        Path(__file__).resolve().parent.parent / "fleet_nerve_nats.py"
    )
    src = module_path.read_text(encoding="utf-8")
    # Helper must exist
    assert "def _record_msg_size" in src
    # Each known send path must invoke the helper
    assert src.count('self._record_msg_size("sent"') >= 3, (
        "Expected ≥3 'sent' size-recording sites (send, _send_broadcast, _send_p1_nats)"
    )
    assert 'self._record_msg_size("received"' in src, (
        "Inbound _handle_message must record received bytes"
    )


def test_health_stats_exposes_p95_key():
    """_build_health must overlay msg_size_p95_bytes onto its stats dict."""
    module_path = (
        Path(__file__).resolve().parent.parent / "fleet_nerve_nats.py"
    )
    src = module_path.read_text(encoding="utf-8")
    assert '"msg_size_p95_bytes"' in src, (
        "msg_size_p95_bytes must appear in /health.stats overlay"
    )
