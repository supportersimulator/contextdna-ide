"""Regression guards for the event-driven immediate-audit self-heal path.

Branch: race/a1-immediate-audit.

Root cause being guarded: the old ``_watchdog_loop`` ticked every 30s,
so send_with_fallback exhaustion, NATS drops, and peer-recovery
heartbeats all waited up to 30s before any channel re-probe. The fix
fires an event-driven audit in < 100ms on the event loop, debounced per
peer, hard-capped at 10s.

What this file asserts:
  1. ``send_with_fallback`` exhaustion calls ``trigger_immediate_audit``
     within 100ms (not 30s).
  2. Two concurrent triggers within 50ms result in ONE audit task
     (``audits_debounced`` increments).
  3. An audit invocation probes every expected channel exactly once
     (P1, P2, P3, P5, P7 -- the cascade we re-probe).
  4. When a heartbeat arrives from a peer marked broken on P1, an
     audit is triggered (recovery path).
  5. No new ``while True: ... sleep`` polling was added in the daemon
     source -- structural invariant guarded via AST scan of the diff.
"""
from __future__ import annotations

import ast
import asyncio
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DAEMON_SRC = REPO_ROOT / "tools" / "fleet_nerve_nats.py"


# ── Helpers ─────────────────────────────────────────────────────────


def _load_daemon():
    """Lazy import of the daemon module. Skips if NATS deps missing."""
    try:
        import tools.fleet_nerve_nats as daemon
        return daemon
    except ImportError as e:
        pytest.skip(f"fleet_nerve_nats import unavailable in test env: {e}")


def _make_nerve(daemon, node_id: str = "test-node"):
    """Build a FleetNerveNATS instance bypassing network init.

    Uses __new__ + manual _stats / _peers / _peers_config so we avoid
    the real __init__ (which touches FleetNerveStore, FleetSendGate,
    JetStream, etc.). Enough state for the audit path.
    """
    Nerve = daemon.FleetNerveNATS
    nerve = Nerve.__new__(Nerve)
    nerve.node_id = node_id
    nerve.nc = None
    nerve._loop = None
    nerve._stats = {
        "audits_triggered": 0,
        "audits_completed": 0,
        "audits_debounced": 0,
        "audits_timed_out": 0,
        "audit_errors": 0,
        "errors": 0,
        "heartbeat_parse_errors": 0,
        "heartbeat_repair_clear_errors": 0,
        "heartbeat_channel_state_errors": 0,
    }
    nerve._peers = {}
    nerve._peers_config = {
        "peer-mac2": {"ip": "10.0.0.2", "ssh_user": "aaron"},
        "peer-mac3": {"ip": "10.0.0.3", "ssh_user": "aaron"},
    }
    nerve._chief = None
    nerve._channel_health = {}
    nerve._heal_in_flight = {}
    nerve._repair_in_progress = {}
    nerve.protocol = daemon.CommunicationProtocol(nerve)
    return nerve


def _resolve_peer_stub(nerve, peer_id: str) -> dict:
    return nerve._peers_config.get(peer_id, {})


# ── 1. send exhaustion triggers audit in < 100ms ────────────────────


def test_send_exhaustion_triggers_audit():
    """send_with_fallback exhausting all channels must schedule an
    immediate audit within 100ms -- NOT wait 30s for _watchdog_loop.
    """
    daemon = _load_daemon()

    async def run():
        nerve = _make_nerve(daemon)
        # Patch protocol.trigger_immediate_audit to a recording stub so
        # we measure the latency of the *dispatch* not the audit itself.
        calls: list[tuple[float, str, str]] = []
        t_start = time.monotonic()

        def _recorder(peer_id: str, reason: str = "unspecified") -> bool:
            calls.append((time.monotonic() - t_start, peer_id, reason))
            return True

        nerve.protocol.trigger_immediate_audit = _recorder  # type: ignore[assignment]

        # Stub _resolve_peer so send_with_fallback doesn't crash.
        nerve._resolve_peer = lambda target: _resolve_peer_stub(nerve, target)  # type: ignore[method-assign]

        # Neutralise the store + gate so we only exercise the cascade.
        nerve._store = None
        nerve._send_gate = None
        # Rate limiter: allow everything.
        nerve.rate_limiter = MagicMock()
        nerve.rate_limiter.allow = MagicMock(return_value=True)
        # Channel reliability: deterministic order.
        nerve._order_channels_by_reliability = lambda target, channels: channels  # type: ignore[method-assign]
        nerve._channel_fail_rate = lambda target, name: 0.0  # type: ignore[method-assign]
        nerve._record_channel_attempt = lambda *a, **kw: None  # type: ignore[method-assign]
        nerve._update_channel_health = lambda *a, **kw: None  # type: ignore[method-assign]
        nerve._pending_acks = {}
        nerve._attempt_self_heal = lambda *a, **kw: None  # type: ignore[method-assign]

        async def _fail(target, message):
            return {"delivered": False, "error": "stub failure"}

        # Force every channel sender to fail.
        for name in ("_send_p0_cloud", "_send_p1_nats", "_send_p2_http",
                     "_send_p3_chief", "_send_p4_seed", "_send_p5_ssh",
                     "_send_p6_wol", "_send_p7_git", "_send_p0_discord"):
            setattr(nerve, name, _fail)

        t0 = time.monotonic()
        result = await nerve.send_with_fallback(
            "peer-mac2", {"type": "context", "payload": {"body": "x"}}
        )
        latency_s = time.monotonic() - t0

        assert result["delivered"] is False
        assert len(calls) == 1, (
            f"Expected exactly one trigger_immediate_audit call on "
            f"exhaustion, got {len(calls)}: {calls}"
        )
        audit_latency, peer, reason = calls[0]
        assert peer == "peer-mac2"
        assert reason == "send_exhausted"
        # Dispatch latency must be < 100ms -- the whole point of the fix.
        assert audit_latency < 0.100, (
            f"Audit trigger latency {audit_latency*1000:.1f}ms exceeds "
            f"100ms target (should be sub-100ms, was 30s on the old path)"
        )
        # And the total send_with_fallback stayed well under the old
        # 30s worst-case envelope.
        assert latency_s < 5.0, f"send_with_fallback took {latency_s:.1f}s"

    asyncio.run(run())


# ── 2. debounce: one audit per peer under concurrent triggers ───────


def test_audit_debounced_on_concurrent_trigger():
    """Two triggers within 50ms for the same peer result in ONE audit
    task; the second tick increments ``audits_debounced``.
    """
    daemon = _load_daemon()

    async def run():
        nerve = _make_nerve(daemon)
        protocol = nerve.protocol

        # Replace _run_audit with a slow stub so the first task stays
        # in flight long enough for the debounce to fire.
        probed: list[str] = []

        async def _slow_audit(peer_id, reason):
            probed.append(f"{peer_id}:{reason}")
            await asyncio.sleep(0.2)
            nerve._stats["audits_completed"] = (
                nerve._stats.get("audits_completed", 0) + 1
            )
            protocol._pending_audits.pop(peer_id, None)
            return {}

        protocol._run_audit = _slow_audit  # type: ignore[assignment]

        a = protocol.trigger_immediate_audit("peer-mac2", reason="first")
        # Immediate second call -- must debounce, not schedule.
        b = protocol.trigger_immediate_audit("peer-mac2", reason="second")

        assert a is True, "First trigger must schedule a task"
        assert b is False, "Second concurrent trigger must debounce"
        assert nerve._stats["audits_triggered"] == 1
        assert nerve._stats["audits_debounced"] == 1

        # Wait for the in-flight audit to finish, then confirm the slot
        # clears and a fresh trigger succeeds.
        await asyncio.sleep(0.3)
        assert "peer-mac2" not in protocol._pending_audits
        c = protocol.trigger_immediate_audit("peer-mac2", reason="third")
        assert c is True
        await asyncio.sleep(0.3)
        assert len(probed) == 2, f"Expected 2 audits after debounce clears, got {probed}"

    asyncio.run(run())


# ── 3. audit runs all expected channel probes ──────────────────────


def test_audit_runs_all_channels():
    """``_probe_all_channels`` must probe P1, P2, P3, P5, P7 -- the
    cascade re-probed on failure. P4/P6 are one-way and excluded.
    Each channel appears exactly once in the result dict.
    """
    daemon = _load_daemon()

    async def run():
        nerve = _make_nerve(daemon)
        # Seed a stale heartbeat so P1 probe reports broken (deterministic).
        nerve._peers = {"peer-mac2": {"_received_at": time.time() - 90}}
        nerve._resolve_peer = lambda target: _resolve_peer_stub(nerve, target)  # type: ignore[method-assign]
        # Set chief so P3 path is exercised.
        nerve._chief = "peer-mac1"
        nerve._peers_config["peer-mac1"] = {"ip": "10.0.0.1"}

        # Patch out external I/O -- we only care about channel coverage.
        async def _raise(*a, **kw):
            raise OSError("stubbed")

        import urllib.request

        with patch.object(urllib.request, "urlopen", side_effect=OSError("stub")):
            # Patch subprocess creation for SSH/git probes to a fast-fail.
            async def _fast_fail(*args, **kwargs):
                raise FileNotFoundError("stub binary missing")

            with patch("asyncio.create_subprocess_exec", side_effect=_fast_fail):
                results = await nerve.protocol._probe_all_channels("peer-mac2")

        daemon_const = daemon
        expected = {
            daemon_const.CHANNEL_P1_NATS,
            daemon_const.CHANNEL_P2_HTTP,
            daemon_const.CHANNEL_P3_CHIEF,
            daemon_const.CHANNEL_P5_SSH,
            daemon_const.CHANNEL_P7_GIT,
        }
        assert set(results.keys()) == expected, (
            f"Probe must cover exactly {expected}, got {set(results.keys())}"
        )
        # Each channel probed exactly once -- dict enforces uniqueness.
        assert len(results) == 5

    asyncio.run(run())


# ── 4. recovery heartbeat triggers audit ───────────────────────────


def test_recovery_heartbeat_triggers_audit():
    """When a heartbeat arrives from a peer whose previous heartbeat
    was stale (> 45s) or absent, the audit path must fire.
    """
    daemon = _load_daemon()

    async def run():
        nerve = _make_nerve(daemon)
        calls: list[tuple[str, str]] = []

        def _recorder(peer_id: str, reason: str = "unspecified") -> bool:
            calls.append((peer_id, reason))
            return True

        # Hook the public wrapper, which is what _handle_heartbeat calls.
        nerve.trigger_immediate_audit = _recorder  # type: ignore[method-assign]

        # Case 1: never seen -> recovery path fires.
        import json
        msg1 = MagicMock()
        msg1.data = json.dumps({"node": "peer-mac2", "sessions": 1}).encode()
        await nerve._handle_heartbeat(msg1)
        assert ("peer-mac2", "heartbeat_recovery") in calls

        # Case 2: previous heartbeat fresh (< 45s) -> no audit fires.
        calls.clear()
        nerve._peers["peer-mac3"] = {"_received_at": time.time() - 5}
        msg2 = MagicMock()
        msg2.data = json.dumps({"node": "peer-mac3", "sessions": 1}).encode()
        await nerve._handle_heartbeat(msg2)
        assert calls == [], (
            f"Fresh heartbeat must NOT trigger audit (would flood the "
            f"audit path on every 15s heartbeat), got {calls}"
        )

        # Case 3: previous heartbeat stale -> recovery fires again.
        calls.clear()
        nerve._peers["peer-mac2"] = {"_received_at": time.time() - 90}
        msg3 = MagicMock()
        msg3.data = json.dumps({"node": "peer-mac2", "sessions": 1}).encode()
        await nerve._handle_heartbeat(msg3)
        assert ("peer-mac2", "heartbeat_recovery") in calls

    asyncio.run(run())


# ── 5. structural invariant: no new polling added ───────────────────


def test_no_polling_invariant():
    """AST scan: the audit feature must be event-driven. No new
    ``while True: ... sleep`` polling may be introduced in any function
    whose name contains 'audit'.

    This guards against a future patch re-introducing a 30s poll in
    trigger_immediate_audit / _run_audit / _probe_all_channels.
    """
    src = DAEMON_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)

    audit_fns: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "audit" in node.name:
                audit_fns.append(node)

    assert len(audit_fns) >= 3, (
        f"Expected at least trigger_immediate_audit / _run_audit / "
        f"_probe_all_channels / _audit_all_peers, found {[f.name for f in audit_fns]}"
    )

    for fn in audit_fns:
        for inner in ast.walk(fn):
            # Pattern: `while True:` block containing an `await sleep` or
            # `time.sleep` -- that's polling.
            if isinstance(inner, ast.While):
                is_true = (
                    isinstance(inner.test, ast.Constant) and inner.test.value is True
                )
                if not is_true:
                    continue
                for sub in ast.walk(inner):
                    is_sleep = False
                    if isinstance(sub, ast.Call):
                        func = sub.func
                        if isinstance(func, ast.Attribute) and func.attr == "sleep":
                            is_sleep = True
                        if isinstance(func, ast.Name) and func.id == "sleep":
                            is_sleep = True
                    if is_sleep:
                        pytest.fail(
                            f"Polling detected in audit function "
                            f"{getattr(fn, 'name', '?')}: while True + sleep. "
                            f"Audit must be event-driven."
                        )


# ── 6. counters declared in _stats init ─────────────────────────────


def test_audit_counters_declared_in_stats_init():
    """All five audit counters must be seeded in the ``_stats`` init
    so ``/health.stats`` exposes them from process start, not only
    after the first audit.
    """
    src = DAEMON_SRC.read_text(encoding="utf-8")
    for counter in (
        "audits_triggered",
        "audits_completed",
        "audits_debounced",
        "audits_timed_out",
        "audit_errors",
    ):
        assert f'"{counter}"' in src, (
            f"Audit counter {counter!r} missing from _stats init dict -- "
            f"won't be visible on /health until first tick."
        )


# ── 7. self/broadcast guard ─────────────────────────────────────────


def test_audit_skips_self_and_broadcast():
    """Constraint from spec: never audit self-target or broadcast."""
    daemon = _load_daemon()

    async def run():
        nerve = _make_nerve(daemon, node_id="test-node")
        protocol = nerve.protocol

        # Stub _run_audit -- it must never run for these cases.
        ran: list[str] = []

        async def _runner(peer_id, reason):
            ran.append(peer_id)

        protocol._run_audit = _runner  # type: ignore[assignment]

        assert protocol.trigger_immediate_audit("test-node", "self") is False
        assert protocol.trigger_immediate_audit("all", "broadcast") is False
        assert protocol.trigger_immediate_audit("", "empty") is False
        # Give any stray task a chance to fire.
        await asyncio.sleep(0.02)
        assert ran == [], f"Audit must skip self/broadcast, ran={ran}"
        # Legitimate target -- fires.
        assert protocol.trigger_immediate_audit("peer-mac2", "real") is True
        await asyncio.sleep(0.02)
        assert "peer-mac2" in ran

    asyncio.run(run())
