"""RACE R5 — Redis health regression tests.

Guards ``FleetNerveNATS._build_redis_health`` and the Redis-related lines
in the Prometheus ``/metrics`` renderer.

Many subsystems (cache, llm health, webhook S2/S6 cache, gains-gate)
depend on Redis; when Redis is down they fail silently. These tests
prove:

  - Redis up → status "ok" + ping_ms numeric
  - Redis down → status "down" + last_error populated
  - /health endpoint returns 200 even when Redis is down (ZSF invariant)
  - Prometheus /metrics emits ``fleet_nerve_redis_ping_ms`` and
    ``fleet_nerve_redis_status`` gauges in both states

Like ``test_health_observability.py``, we bind the helper off the real
class so the production code is exercised, but hand it a minimal holder
instead of standing up a full FleetNerveNATS (which needs NATS,
asyncio, etc.).
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Lightweight holder standing in for FleetNerveNATS ───────────────


def _make_holder(tmp_path: Path):
    """Return a SimpleNamespace wired with the attributes _build_redis_health
    and _build_prometheus_metrics read off ``self``.
    """
    holder = SimpleNamespace()
    holder.node_id = "test-node"
    holder.nc = None
    holder.js = None
    holder._loop = None
    holder._peers = {}
    holder._stats = {
        "received": 0,
        "sent": 0,
        "broadcasts": 0,
        "errors": 0,
        "gates_blocked": 0,
        "validation_rejected": 0,
        "p0_discord_sends": 0,
        "ack_handler_errors": 0,
        "ack_send_errors": 0,
        "repair_gate_health_errors": 0,
        "peer_failures_detected_via_callback": 0,
        "peer_failures_detected_via_no_responders": 0,
        "heartbeats_skipped_due_to_traffic": 0,
        "bytes_sent": 0,
        "bytes_received": 0,
        "large_msg_count": 0,
        "size_record_errors": 0,
        "started_at": time.time() - 60,
    }
    holder._recent_errors = deque(maxlen=50)
    holder._counter_snapshots = []
    holder._last_counter_snapshot_ts = 0.0
    holder._delegation_queue_dir = tmp_path / "delegations"
    # _compute_msg_size_p95 reads this; supply an empty deque so the
    # Prometheus renderer doesn't crash when we test its happy path.
    holder._msg_size_window = deque(maxlen=512)
    return holder


def _bind(method_name: str):
    try:
        from tools.fleet_nerve_nats import FleetNerveNATS
    except ImportError as e:
        pytest.skip(f"fleet_nerve_nats import unavailable in test env: {e}")
    return getattr(FleetNerveNATS, method_name)


# ── happy path: Redis up ────────────────────────────────────────────


def test_redis_health_ok_returns_numeric_ping(tmp_path):
    """When redis-py.ping() succeeds we report status=ok with a numeric
    ping_ms and a populated info section. last_error must be None.
    """
    build = _bind("_build_redis_health")
    holder = _make_holder(tmp_path)

    fake_client = MagicMock()
    fake_client.ping.return_value = True
    fake_client.info.side_effect = lambda section=None: {
        "memory": {"used_memory": 12 * 1024 * 1024},
        "clients": {"connected_clients": 5},
    }[section]

    fake_redis_module = MagicMock()
    fake_redis_module.Redis.return_value = fake_client

    with patch.dict("sys.modules", {"redis": fake_redis_module}):
        result = build(holder)

    assert result["status"] == "ok"
    assert isinstance(result["ping_ms"], int)
    assert result["ping_ms"] >= 0
    assert result["memory_used_mb"] == 12
    assert result["connected_clients"] == 5
    assert result["last_error"] is None


def test_redis_health_uses_socket_timeout(tmp_path):
    """Verify the redis client is constructed with a 1s socket_timeout
    so /health can never block on an unreachable Redis broker.
    """
    build = _bind("_build_redis_health")
    holder = _make_holder(tmp_path)

    fake_client = MagicMock()
    fake_client.ping.return_value = True
    fake_client.info.return_value = {}

    fake_redis_module = MagicMock()
    fake_redis_module.Redis.return_value = fake_client

    with patch.dict("sys.modules", {"redis": fake_redis_module}):
        build(holder)

    kwargs = fake_redis_module.Redis.call_args.kwargs
    assert kwargs.get("socket_timeout") == 1.0
    assert kwargs.get("socket_connect_timeout") == 1.0


# ── failure path: Redis down ────────────────────────────────────────


def test_redis_health_down_when_ping_raises(tmp_path):
    """When redis-py raises (broker unreachable), report status=down
    with last_error populated. Must NOT propagate the exception — /health
    must still return 200.
    """
    build = _bind("_build_redis_health")
    holder = _make_holder(tmp_path)

    fake_client = MagicMock()
    fake_client.ping.side_effect = ConnectionError("Connection refused")

    fake_redis_module = MagicMock()
    fake_redis_module.Redis.return_value = fake_client

    with patch.dict("sys.modules", {"redis": fake_redis_module}):
        result = build(holder)

    assert result["status"] == "down"
    assert result["ping_ms"] is None
    assert result["last_error"] is not None
    assert "Connection refused" in result["last_error"]
    # ZSF: error counter must increment so failures are observable.
    assert holder._stats.get("redis_health_errors", 0) >= 1


def test_redis_health_down_when_redis_py_missing(tmp_path):
    """If redis-py isn't installed, return status=down with a
    descriptive last_error rather than crashing.
    """
    build = _bind("_build_redis_health")
    holder = _make_holder(tmp_path)

    # Stub the import to raise. We use a finder that pretends redis isn't
    # installed by injecting a None entry into sys.modules — Python's
    # import machinery treats that as ImportError on next ``import``.
    import sys
    saved = sys.modules.pop("redis", None)
    sys.modules["redis"] = None  # type: ignore[assignment]
    try:
        result = build(holder)
    finally:
        if saved is not None:
            sys.modules["redis"] = saved
        else:
            sys.modules.pop("redis", None)

    assert result["status"] == "down"
    assert result["last_error"] is not None
    assert "redis-py-not-installed" in result["last_error"]


def test_redis_health_endpoint_returns_200_when_redis_down(tmp_path):
    """Behavioural guarantee: /health must remain 200 even when
    _build_redis_health reports down. The /health handler in
    fleet_nerve_nats.py serializes _build_health() unconditionally —
    we prove that path stays JSON-serializable + non-throwing when the
    Redis section is in a failure state.
    """
    import json

    build_redis = _bind("_build_redis_health")
    holder = _make_holder(tmp_path)

    fake_client = MagicMock()
    fake_client.ping.side_effect = OSError("no route to host")
    fake_redis_module = MagicMock()
    fake_redis_module.Redis.return_value = fake_client

    with patch.dict("sys.modules", {"redis": fake_redis_module}):
        snapshot = build_redis(holder)

    # The actual /health handler does:
    #     data = json.dumps(daemon._build_health()).encode()
    # which never raises here because the redis_health key is just a
    # dict of primitives. Prove the snapshot is JSON-serializable.
    encoded = json.dumps({"redis_health": snapshot})
    parsed = json.loads(encoded)
    assert parsed["redis_health"]["status"] == "down"
    assert parsed["redis_health"]["last_error"] is not None


# ── caching ─────────────────────────────────────────────────────────


def test_redis_health_caches_within_5s(tmp_path):
    """Repeated calls within the 5s window must reuse the cached result
    so /health and /metrics together don't open two sockets per scrape.
    """
    build = _bind("_build_redis_health")
    holder = _make_holder(tmp_path)

    fake_client = MagicMock()
    fake_client.ping.return_value = True
    fake_client.info.return_value = {}
    fake_redis_module = MagicMock()
    fake_redis_module.Redis.return_value = fake_client

    with patch.dict("sys.modules", {"redis": fake_redis_module}):
        first = build(holder)
        second = build(holder)

    assert first is second  # same cached dict object
    # Redis() was constructed exactly once.
    assert fake_redis_module.Redis.call_count == 1


# ── Prometheus integration ──────────────────────────────────────────


def _wire_metrics_helpers(holder):
    """Bind the real instance methods the Prometheus renderer needs onto
    a SimpleNamespace holder so the renderer can call them as ``self.foo()``.
    """
    from tools.fleet_nerve_nats import FleetNerveNATS

    holder._build_redis_health = lambda: FleetNerveNATS._build_redis_health(holder)
    holder._compute_msg_size_p95 = lambda: 0
    holder._build_delegation_queue = lambda: {"depth": 0}


def test_metrics_emits_redis_gauges_when_ok(tmp_path):
    """/metrics must surface fleet_nerve_redis_ping_ms and
    fleet_nerve_redis_status gauges so Prometheus dashboards can alert
    on Redis health without parsing JSON.
    """
    build_metrics = _bind("_build_prometheus_metrics")
    holder = _make_holder(tmp_path)
    _wire_metrics_helpers(holder)

    # Pre-seed the cache so the metrics path doesn't hit the import
    # plumbing; this exercises the gauge-emit branch deterministically.
    holder._redis_health_cache = {
        "status": "ok",
        "ping_ms": 2,
        "memory_used_mb": 12,
        "connected_clients": 5,
        "last_error": None,
    }
    holder._redis_health_cache_at = time.time()

    text = build_metrics(holder)

    assert "fleet_nerve_redis_ping_ms" in text
    assert 'fleet_nerve_redis_status{node="test-node",status="ok"} 1' in text
    # The ping gauge line should carry the cached integer.
    assert 'fleet_nerve_redis_ping_ms{node="test-node"} 2' in text


def test_metrics_emits_redis_down_gauge(tmp_path):
    """When Redis is down the status gauge must read 0 and ping must be
    -1 (sentinel for "no measurement") so PromQL can filter it out.
    """
    build_metrics = _bind("_build_prometheus_metrics")
    holder = _make_holder(tmp_path)
    _wire_metrics_helpers(holder)

    holder._redis_health_cache = {
        "status": "down",
        "ping_ms": None,
        "memory_used_mb": None,
        "connected_clients": None,
        "last_error": "Connection refused",
    }
    holder._redis_health_cache_at = time.time()

    text = build_metrics(holder)

    assert 'fleet_nerve_redis_status{node="test-node",status="down"} 0' in text
    assert 'fleet_nerve_redis_ping_ms{node="test-node"} -1' in text
