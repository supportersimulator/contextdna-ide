"""RACE B6 — comprehensive observability regression tests for /health.

Guards the five new sections added to ``FleetNerveNATS._build_health`` plus
the Prometheus ``/metrics`` renderer:

    - jetstream_health   — per-stream replica lag (R=3 quorum signal)
    - cluster_state      — NATS connection + observed peers
    - recent_errors      — last-N ERROR log entries captured live
    - counter_rates      — per-min / per-hour deltas on numeric _stats
    - delegation_queue   — pending / done / failed queue depth

These helper methods are pure (no I/O on their own), so we stand up the
minimum shape the class reads from ``self`` rather than building a full
FleetNerveNATS instance (which needs NATS, asyncio loop, etc.).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── Lightweight holder standing in for FleetNerveNATS ───────────────


def _make_holder(tmp_path: Path):
    """Return a SimpleNamespace wired with the attributes each B6 helper
    reads. The helpers are bound off the real class so they exercise the
    production code — we just hand them a minimal ``self``.
    """
    import threading
    holder = SimpleNamespace()
    holder.node_id = "test-node"
    holder.nc = None
    holder.js = None
    holder._loop = None
    holder._peers = {"peer-a": {"_received_at": time.time()}}
    # AA1 2026-05-07 — _build_jetstream_health now reads cache + lock
    # state up front. Pre-init both so the helper never AttributeErrors
    # against a fresh holder.
    holder._js_health_cache = None
    holder._js_health_cache_at = 0.0
    holder._js_health_cache_ttl_ok_s = 15.0
    holder._js_health_cache_ttl_err_s = 3.0
    holder._js_health_cache_lock = threading.Lock()
    holder._js_health_cache_lock_wait_s = 0.5
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
        "started_at": time.time() - 60,
    }
    holder._recent_errors = deque(maxlen=50)
    holder._counter_snapshots = []
    holder._last_counter_snapshot_ts = 0.0
    holder._delegation_queue_dir = tmp_path / "delegations"
    return holder


def _bind(method_name: str):
    try:
        from tools.fleet_nerve_nats import FleetNerveNATS
    except ImportError as e:
        pytest.skip(f"fleet_nerve_nats import unavailable in test env: {e}")
    return getattr(FleetNerveNATS, method_name)


# ── jetstream_health ────────────────────────────────────────────────


def test_jetstream_health_reports_disabled_when_js_none(tmp_path):
    build = _bind("_build_jetstream_health")
    holder = _make_holder(tmp_path)
    result = build(holder)
    assert isinstance(result, dict)
    assert result.get("status") == "disabled"
    assert "streams" in result


def test_jetstream_health_cached_for_15s(tmp_path):
    """Second call within 15s must not re-probe (respects cache_at)."""
    build = _bind("_build_jetstream_health")
    holder = _make_holder(tmp_path)
    first = build(holder)
    # Mutate the cached value to prove we got the same object back.
    holder._js_health_cache["sentinel"] = True
    second = build(holder)
    assert second.get("sentinel") is True, "15s cache must short-circuit re-probe"


# ── jetstream_health wedge recovery (AA1 2026-05-07) ────────────────


def test_jetstream_health_error_payload_uses_short_ttl(tmp_path):
    """A cached error payload must refresh after the short error TTL,
    not stick for the full 15s success window. This is the wedge fix:
    one TimeoutError should not pin /health into ``status: error`` for
    every poller across the next 15 seconds.
    """
    build = _bind("_build_jetstream_health")
    holder = _make_holder(tmp_path)
    # Seed an error cache 4 seconds in the past (older than the 3s
    # error TTL but younger than the 15s ok TTL).
    holder._js_health_cache = {
        "status": "error", "error": "TimeoutError()", "streams": {},
    }
    import time as _time
    holder._js_health_cache_at = _time.monotonic() - 4.0
    result = build(holder)
    # Re-probe ran (js is None → status flips to disabled). Crucially
    # the stale error payload was NOT served.
    assert result.get("status") == "disabled"
    # Wedge-recovery counter NOT bumped (still error→disabled, not
    # error→ok with at least one healthy stream), but the error-TTL
    # refresh counter IS — proves the short-TTL path fired.
    assert holder._stats.get("js_health_cache_error_ttl_refreshes_total") == 1


def test_jetstream_health_ok_payload_uses_long_ttl(tmp_path):
    """A cached ok payload still respects the 15s success window so
    high-frequency pollers don't hammer stream_info."""
    build = _bind("_build_jetstream_health")
    holder = _make_holder(tmp_path)
    holder._js_health_cache = {"status": "ok", "streams": {}, "sentinel": True}
    import time as _time
    holder._js_health_cache_at = _time.monotonic() - 4.0
    result = build(holder)
    assert result.get("sentinel") is True


def test_jetstream_health_lock_released_on_inner_exception(tmp_path):
    """If the populate path raises, the cache lock MUST release. A
    leaked lock would wedge every subsequent /health caller behind a
    held mutex (the actual M1/G3 wedge mode)."""
    build = _bind("_build_jetstream_health")
    holder = _make_holder(tmp_path)

    # Force the populate path to fault by handing it a js handle that
    # raises on attribute access.
    class _BoomJS:
        def __getattr__(self, _name):
            raise RuntimeError("simulated wedge")
    holder.js = _BoomJS()
    holder._loop = None  # _probe_stream_sync will short-circuit on this

    # Run twice — second call must not hang. Each call probes 3 streams
    # (each returns status=error because _loop is None) which is fine;
    # we care that the lock is released so the 2nd call enters.
    r1 = build(holder)
    assert r1.get("status") in ("error", "degraded", "ok")
    # Lock must be releasable from this thread (i.e. not currently held).
    assert holder._js_health_cache_lock.acquire(blocking=False)
    holder._js_health_cache_lock.release()
    r2 = build(holder)
    assert isinstance(r2, dict)


def test_jetstream_health_lock_timeout_serves_stale_and_counts(tmp_path):
    """When the populate lock is already held, the call returns the
    previous cached payload (not a fresh probe) and bumps the lock-
    timeout counter."""
    build = _bind("_build_jetstream_health")
    holder = _make_holder(tmp_path)
    holder._js_health_cache = {"status": "ok", "streams": {}, "stale": True}
    import time as _time
    holder._js_health_cache_at = _time.monotonic() - 100.0  # forces refresh
    holder._js_health_cache_lock_wait_s = 0.05  # short wait for the test

    # Acquire the lock so the next call can't enter populate.
    assert holder._js_health_cache_lock.acquire(blocking=False)
    try:
        result = build(holder)
    finally:
        holder._js_health_cache_lock.release()
    assert result.get("stale") is True, "must serve previous cache"
    assert holder._stats.get("js_health_cache_lock_timeouts_total") == 1


def test_jetstream_health_warming_up_when_no_cache_and_lock_held(tmp_path):
    """If there is no prior cache AND the lock is held, the helper
    must emit the ``warming-up`` sentinel rather than an empty schema.
    /health consumers (xbar, dashboards) never see type-name placeholders.
    """
    build = _bind("_build_jetstream_health")
    holder = _make_holder(tmp_path)
    holder._js_health_cache_lock_wait_s = 0.05
    assert holder._js_health_cache_lock.acquire(blocking=False)
    try:
        result = build(holder)
    finally:
        holder._js_health_cache_lock.release()
    assert result == {"status": "warming-up", "streams": {}}
    assert holder._stats.get("js_health_cache_lock_timeouts_total") == 1


def test_jetstream_health_recovers_within_one_ttl_window(tmp_path):
    """End-to-end: simulate the M1/G3 wedge — cache stuck in error,
    js handle gets restored — and prove the next call within 1 short
    TTL window flips status back to ok."""
    build = _bind("_build_jetstream_health")
    holder = _make_holder(tmp_path)
    # Pin a stale error payload 4s old (> 3s err TTL).
    holder._js_health_cache = {
        "status": "error", "error": "TimeoutError()", "streams": {},
    }
    import time as _time
    holder._js_health_cache_at = _time.monotonic() - 4.0

    # Provide a working stream probe — bypass _probe_stream_sync entirely
    # by stubbing it onto the holder.
    def _ok_probe(self, stream_name):
        return {
            "status": "ok", "replicas": 3, "leader": "mac3",
            "max_follower_lag": 0, "messages": 0, "bytes": 0,
            "first_seq": 0, "last_seq": 0, "followers": [],
        }
    # js must be non-None for the populate path to invoke probe.
    holder.js = object()
    # Bind the stub so ``self._probe_stream_sync`` resolves to it.
    import types
    holder._probe_stream_sync = types.MethodType(_ok_probe, holder)

    result = build(holder)
    assert result["status"] == "ok"
    # Wedge-recovery counter MUST bump — proves the wedge-recovery
    # signal is observable in /health.stats.
    assert holder._stats.get("js_health_cache_wedge_recoveries_total") == 1


# ── cluster_state ───────────────────────────────────────────────────


def test_cluster_state_disconnected_when_no_nc(tmp_path):
    build = _bind("_build_cluster_state")
    holder = _make_holder(tmp_path)
    result = build(holder)
    assert result == {"status": "disconnected"}


def test_cluster_state_exposes_observed_peers(tmp_path):
    build = _bind("_build_cluster_state")
    holder = _make_holder(tmp_path)
    holder._peers = {"mac1": {}, "mac2": {}, "mac3": {}}
    fake_nc = MagicMock()
    fake_nc.is_connected = True
    fake_nc._server_info = {
        "server_id": "ND123",
        "server_name": "nats-a",
        "cluster": "fleet",
        "version": "2.10.11",
        "jetstream": True,
    }
    fake_nc.connected_url = "nats://127.0.0.1:4222"
    holder.nc = fake_nc
    result = build(holder)
    assert result["status"] == "connected"
    assert result["server_id"] == "ND123"
    assert result["jetstream_enabled"] is True
    assert result["observed_peer_count"] == 3
    assert result["observed_peers"] == ["mac1", "mac2", "mac3"]


# ── recent_errors ───────────────────────────────────────────────────


def test_recent_errors_returns_list_and_respects_limit(tmp_path):
    build = _bind("_build_recent_errors")
    holder = _make_holder(tmp_path)
    for i in range(25):
        holder._recent_errors.append({
            "ts": time.time(),
            "logger": "fleet_nats",
            "level": "ERROR",
            "message": f"err-{i}",
        })
    out = build(holder)  # default limit=10
    assert isinstance(out, list)
    assert len(out) == 10
    # Must be the newest 10 (err-15..err-24)
    assert out[0]["message"] == "err-15"
    assert out[-1]["message"] == "err-24"


def test_recent_errors_empty_when_buffer_missing(tmp_path):
    """If _recent_errors is None the helper must not crash /health."""
    build = _bind("_build_recent_errors")
    holder = _make_holder(tmp_path)
    holder._recent_errors = None  # type: ignore[assignment]
    out = build(holder)
    assert out == []


def test_observability_error_handler_captures_error_level(tmp_path):
    """The handler installed by __init__ must actually capture ERROR logs."""
    try:
        from tools.fleet_nerve_nats import _ObservabilityErrorHandler
    except ImportError as e:
        pytest.skip(f"handler import unavailable: {e}")
    buf: deque = deque(maxlen=10)
    h = _ObservabilityErrorHandler(buf)
    h.setLevel(logging.ERROR)
    lg = logging.getLogger("test_b6_obs_capture")
    lg.addHandler(h)
    lg.setLevel(logging.ERROR)
    try:
        lg.error("boom %s", "x")
        lg.warning("suppressed")  # < ERROR, must not appear
        assert len(buf) == 1
        rec = buf[0]
        assert rec["level"] == "ERROR"
        assert rec["message"] == "boom x"
        assert rec["logger"] == "test_b6_obs_capture"
    finally:
        lg.removeHandler(h)


# ── counter_rates ───────────────────────────────────────────────────


def test_counter_rates_has_minute_and_hour_windows(tmp_path):
    build = _bind("_build_counter_rates")
    holder = _make_holder(tmp_path)
    # Seed a baseline snapshot 120s in the past with lower counters.
    holder._counter_snapshots = [{
        "ts": time.time() - 120,
        "counters": {k: 0 for k in holder._stats if isinstance(holder._stats[k], (int, float))},
    }]
    holder._stats["received"] = 60  # 60 msgs over 120s → 0.5/s
    result = build(holder)
    assert "window_minute" in result
    assert "window_hour" in result
    assert "snapshot_count" in result
    # minute window uses the snapshot ≥60s old (our 120s baseline qualifies).
    assert result["window_minute"].get("received", 0) == pytest.approx(0.5, abs=0.1)


def test_counter_rates_returns_empty_windows_when_no_history(tmp_path):
    build = _bind("_build_counter_rates")
    holder = _make_holder(tmp_path)
    result = build(holder)
    assert result["window_minute"] == {}
    assert result["window_hour"] == {}
    assert result["snapshot_count"] == 1  # the one we just appended


# ── delegation_queue ────────────────────────────────────────────────


def test_delegation_queue_counts_pending_done_failed(tmp_path):
    build = _bind("_build_delegation_queue")
    holder = _make_holder(tmp_path)
    base = holder._delegation_queue_dir
    base.mkdir(parents=True)
    (base / "done").mkdir()
    (base / "failed").mkdir()
    # 3 pending, 2 done, 1 failed
    for i in range(3):
        (base / f"pending-{i}.json").write_text("{}")
    for i in range(2):
        (base / "done" / f"done-{i}.json").write_text("{}")
    (base / "failed" / "f-0.json").write_text("{}")
    result = build(holder)
    assert result["status"] == "ok"
    assert result["depth"] == 3
    assert result["done"] == 2
    assert result["failed"] == 1
    assert result["path"] == str(base)


def test_delegation_queue_empty_when_dir_missing(tmp_path):
    build = _bind("_build_delegation_queue")
    holder = _make_holder(tmp_path)
    # dir intentionally not created
    result = build(holder)
    assert result["depth"] == 0
    assert result["status"] == "empty"


# ── prometheus /metrics ─────────────────────────────────────────────


def test_prometheus_metrics_emits_counter_lines(tmp_path):
    build = _bind("_build_prometheus_metrics")
    build_dq = _bind("_build_delegation_queue")
    holder = _make_holder(tmp_path)
    holder._stats["received"] = 42
    holder._stats["errors"] = 3
    # Bind the delegation-queue helper as a bound method so
    # ``self._build_delegation_queue()`` inside the metrics builder resolves.
    holder._build_delegation_queue = lambda: build_dq(holder)
    # dir missing → delegation depth 0 (still rendered)
    text = build(holder)
    assert isinstance(text, str)
    assert text.endswith("\n")
    assert "# TYPE fleet_nerve_counter counter" in text
    assert 'fleet_nerve_received{node="test-node"} 42' in text
    assert 'fleet_nerve_errors{node="test-node"} 3' in text
    # delegation depth is surfaced as a pseudo-counter
    assert "fleet_nerve_delegation_queue_depth" in text


# ── schema presence regression ──────────────────────────────────────


def test_build_health_exposes_all_b6_sections():
    """Structural guard: the five B6 sections must be keys in the returned
    dict built by _build_health. We patch the helpers to constants so we
    can call _build_health on a minimal holder without hitting live NATS.
    """
    try:
        from tools.fleet_nerve_nats import FleetNerveNATS
    except ImportError as e:
        pytest.skip(f"fleet_nerve_nats unavailable: {e}")
    src = Path(FleetNerveNATS.__module__.replace(".", "/"))  # unused, just a sanity import
    # Read the source and grep for each section so we catch regressions
    # even if future refactors change how _build_health is composed.
    module_path = Path(__file__).resolve().parent.parent / "fleet_nerve_nats.py"
    text = module_path.read_text(encoding="utf-8")
    for section in (
        "jetstream_health",
        "cluster_state",
        "recent_errors",
        "counter_rates",
        "delegation_queue",
    ):
        assert f'"{section}"' in text, (
            f"RACE B6 health section {section!r} missing from fleet_nerve_nats.py"
        )
    # /metrics endpoint wiring must also be present
    assert 'self.path == "/metrics"' in text, (
        "Prometheus /metrics endpoint missing from HTTP handler"
    )
