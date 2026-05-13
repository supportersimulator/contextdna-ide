"""RACE S3 — NATS connection-pool health regression tests.

Guards ``FleetNerveNATS._build_nats_connection_health`` and the
NATS-conn lines in the Prometheus ``/metrics`` renderer.

When the nats-py client leaks subscriptions, queues outbound bytes, or
flaps reconnects, there is no observable signal until things break.
These tests prove:

  - Connected client → status "ok", client_status "connected", counters
    sourced from ``nc.stats`` and ``len(nc._subs)``.
  - State derivation: closed > draining > reconnecting > connected.
  - ``nc is None`` (daemon never connected) → status "ok",
    client_status "closed", zero counters (stable shape, no crash).
  - Broken ``nc`` (raises on attr access) → status "error" + error
    string + ``nats_conn_health_errors`` counter incremented (ZSF).
  - Prometheus emits the six declared metrics with correct types.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── helpers ─────────────────────────────────────────────────────────


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


def _make_holder(nc=None, tmp_path: Path | None = None):
    """Minimal stand-in for FleetNerveNATS exercising S3 helpers."""
    holder = SimpleNamespace()
    holder.node_id = "test-node"
    holder.nc = nc
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
    if tmp_path is not None:
        holder._delegation_queue_dir = tmp_path / "delegations"
        holder._build_delegation_queue = lambda: {"depth": 0, "status": "empty"}
        # Bind real msg_size_p95 + redis health + nats conn health helpers so
        # the prometheus path doesn't blow up looking for them.
        mod = _constants()
        holder._msg_size_window = deque(maxlen=mod.MSG_SIZE_WINDOW)
        compute_p95 = _bind("_compute_msg_size_p95")
        holder._compute_msg_size_p95 = lambda: compute_p95(holder)
        # Redis: minimal stub returning a stable down shape so the renderer
        # can still iterate. We're testing nats-conn lines, not Redis.
        holder._build_redis_health = lambda: {
            "status": "down",
            "ping_ms": None,
            "memory_used_mb": None,
            "connected_clients": None,
            "last_error": "stub",
        }
        build_nch = _bind("_build_nats_connection_health")
        holder._build_nats_connection_health = lambda: build_nch(holder)
    return holder


def _fake_nc(
    *,
    is_connected=True,
    is_reconnecting=False,
    is_closed=False,
    is_draining=False,
    stats=None,
    subs=None,
    pending_data_size=0,
    max_payload=1048576,
):
    nc = SimpleNamespace()
    nc.is_connected = is_connected
    nc.is_reconnecting = is_reconnecting
    nc.is_closed = is_closed
    nc.is_draining = is_draining
    nc.stats = stats if stats is not None else {
        "in_msgs": 0,
        "out_msgs": 0,
        "in_bytes": 0,
        "out_bytes": 0,
        "reconnects": 0,
        "errors_received": 0,
    }
    nc._subs = subs if subs is not None else {}
    nc.pending_data_size = pending_data_size
    nc.max_payload = max_payload
    return nc


# ── _build_nats_connection_health — happy path ──────────────────────


def test_connected_client_reports_full_counters():
    build = _bind("_build_nats_connection_health")
    nc = _fake_nc(
        is_connected=True,
        stats={
            "in_msgs": 12345,
            "out_msgs": 6789,
            "in_bytes": 1_000_000,
            "out_bytes": 500_000,
            "reconnects": 2,
            "errors_received": 1,
        },
        subs={i: object() for i in range(8)},
        pending_data_size=0,
        max_payload=1048576,
    )
    holder = _make_holder(nc=nc)
    out = build(holder)
    assert out["status"] == "ok"
    assert out["client_status"] == "connected"
    assert out["in_msgs_total"] == 12345
    assert out["out_msgs_total"] == 6789
    assert out["in_bytes_total"] == 1_000_000
    assert out["out_bytes_total"] == 500_000
    assert out["subscription_count"] == 8
    assert out["pending_data_bytes"] == 0
    assert out["reconnects"] == 2
    assert out["errors_received"] == 1
    assert out["max_payload_bytes"] == 1048576


def test_pending_buffer_growth_visible():
    """Back-pressure signal: pending_data_bytes must surface when the
    outbound write buffer has unflushed bytes."""
    build = _bind("_build_nats_connection_health")
    nc = _fake_nc(pending_data_size=4096)
    holder = _make_holder(nc=nc)
    out = build(holder)
    assert out["pending_data_bytes"] == 4096


# ── _build_nats_connection_health — state derivation ────────────────


def test_state_priority_closed_beats_others():
    build = _bind("_build_nats_connection_health")
    nc = _fake_nc(
        is_connected=True, is_reconnecting=True, is_draining=True, is_closed=True,
    )
    holder = _make_holder(nc=nc)
    assert build(holder)["client_status"] == "closed"


def test_state_draining_beats_reconnecting_and_connected():
    build = _bind("_build_nats_connection_health")
    nc = _fake_nc(
        is_connected=True, is_reconnecting=True, is_draining=True, is_closed=False,
    )
    holder = _make_holder(nc=nc)
    assert build(holder)["client_status"] == "draining"


def test_state_reconnecting_beats_connected():
    build = _bind("_build_nats_connection_health")
    nc = _fake_nc(is_connected=True, is_reconnecting=True)
    holder = _make_holder(nc=nc)
    assert build(holder)["client_status"] == "reconnecting"


def test_state_disconnected_when_no_flag_set():
    build = _bind("_build_nats_connection_health")
    nc = _fake_nc(is_connected=False)
    holder = _make_holder(nc=nc)
    assert build(holder)["client_status"] == "disconnected"


# ── _build_nats_connection_health — defensive paths ─────────────────


def test_nc_none_returns_stable_shape():
    """Daemon never connected → still return all keys with zeros."""
    build = _bind("_build_nats_connection_health")
    holder = _make_holder(nc=None)
    out = build(holder)
    assert out["status"] == "ok"  # the helper itself didn't fail
    assert out["client_status"] == "closed"
    # Stable shape — every documented key present.
    for k in (
        "in_msgs_total", "out_msgs_total", "in_bytes_total", "out_bytes_total",
        "subscription_count", "pending_data_bytes", "reconnects",
        "errors_received", "max_payload_bytes",
    ):
        assert k in out, f"missing key {k}"
        assert out[k] == 0


def test_zsf_broken_nc_does_not_crash():
    """ZSF invariant: a malicious/buggy nc that raises on attribute
    access must NOT propagate; result is observable as status=error
    AND the failure counter increments."""
    build = _bind("_build_nats_connection_health")

    class BadNC:
        @property
        def is_closed(self):
            raise RuntimeError("simulated broken client")

        @property
        def is_draining(self):
            raise RuntimeError("simulated broken client")

        @property
        def is_reconnecting(self):
            raise RuntimeError("simulated broken client")

        @property
        def is_connected(self):
            raise RuntimeError("simulated broken client")

    holder = _make_holder(nc=BadNC())
    out = build(holder)
    assert out["status"] == "error"
    assert "simulated broken client" in out["error"]
    # Counter must increment so dashboards can alert on the regression.
    assert holder._stats.get("nats_conn_health_errors", 0) == 1


def test_missing_stats_dict_yields_zeros():
    """nats-py rename / older client → counters default to 0, no crash."""
    build = _bind("_build_nats_connection_health")
    nc = _fake_nc()
    nc.stats = None  # simulate missing/renamed attribute
    holder = _make_holder(nc=nc)
    out = build(holder)
    assert out["status"] == "ok"
    assert out["in_msgs_total"] == 0
    assert out["out_msgs_total"] == 0
    assert out["reconnects"] == 0


def test_missing_subs_attr_does_not_crash():
    build = _bind("_build_nats_connection_health")
    nc = _fake_nc()
    delattr(nc, "_subs")
    holder = _make_holder(nc=nc)
    out = build(holder)
    assert out["status"] == "ok"
    assert out["subscription_count"] == 0


# ── Prometheus /metrics surface ─────────────────────────────────────


def test_prometheus_emits_s3_nats_conn_metrics(tmp_path):
    build_metrics = _bind("_build_prometheus_metrics")
    nc = _fake_nc(
        is_connected=True,
        stats={
            "in_msgs": 100, "out_msgs": 50, "in_bytes": 0, "out_bytes": 0,
            "reconnects": 3, "errors_received": 0,
        },
        subs={"a": 1, "b": 2, "c": 3},
        pending_data_size=2048,
        max_payload=1048576,
    )
    holder = _make_holder(nc=nc, tmp_path=tmp_path)
    text = build_metrics(holder)
    # The three metrics required by the spec.
    assert 'fleet_nerve_nats_in_msgs_total{node="test-node"} 100' in text
    assert 'fleet_nerve_nats_subs_active{node="test-node"} 3' in text
    assert 'fleet_nerve_nats_reconnects_total{node="test-node"} 3' in text
    # Plus the additional gauges/counters we expose for full visibility.
    assert 'fleet_nerve_nats_out_msgs_total{node="test-node"} 50' in text
    assert 'fleet_nerve_nats_pending_bytes{node="test-node"} 2048' in text
    assert 'fleet_nerve_nats_max_payload_bytes{node="test-node"} 1048576' in text
    # TYPE declarations are required for Prometheus tooling.
    assert "# TYPE fleet_nerve_nats_in_msgs_total counter" in text
    assert "# TYPE fleet_nerve_nats_subs_active gauge" in text
    assert "# TYPE fleet_nerve_nats_reconnects_total counter" in text
    assert "# TYPE fleet_nerve_nats_pending_bytes gauge" in text


def test_prometheus_emits_zeros_when_nc_none(tmp_path):
    """Even with no NATS connection, the metrics must render a 0 line —
    a missing series breaks dashboards just as badly as a wrong value."""
    build_metrics = _bind("_build_prometheus_metrics")
    holder = _make_holder(nc=None, tmp_path=tmp_path)
    text = build_metrics(holder)
    assert 'fleet_nerve_nats_in_msgs_total{node="test-node"} 0' in text
    assert 'fleet_nerve_nats_subs_active{node="test-node"} 0' in text
    assert 'fleet_nerve_nats_reconnects_total{node="test-node"} 0' in text


def test_prometheus_zsf_swallows_helper_failure(tmp_path):
    """If the conn-health helper itself raises, /metrics must still
    return text (with a comment marker), not a 500."""
    build_metrics = _bind("_build_prometheus_metrics")
    holder = _make_holder(nc=None, tmp_path=tmp_path)

    def _boom():
        raise RuntimeError("helper exploded")

    holder._build_nats_connection_health = _boom
    text = build_metrics(holder)
    assert "nats-conn-metrics-error" in text
    assert "helper exploded" in text


# ── /health wiring guard (regression) ───────────────────────────────


def test_build_health_wires_nats_connection_health():
    """Source-level guard: if a future refactor drops the wiring,
    /health will silently lose the section. Fail loud at test time."""
    module_path = (
        Path(__file__).resolve().parent.parent / "fleet_nerve_nats.py"
    )
    src = module_path.read_text(encoding="utf-8")
    assert "def _build_nats_connection_health" in src
    assert '"nats_connection_health": self._build_nats_connection_health()' in src
