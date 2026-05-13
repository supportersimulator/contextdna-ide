"""RACE A3 — strict P0→P7 cascade order vs reliability-reordered.

Guards the FLEET_CHANNEL_ORDER invariant: "notices when needing to use next
most optimal fast and reliable channel IN ORDER" (Aaron's spec). Channel order
must be deterministic (strict mode, default) unless explicitly opted into
reliability-reordering via the env flag.

Constraints:
- No real NATS — all channel senders are mocked.
- Tests only exercise the in-process cascade logic in send_with_fallback.
- Covers: default mode, strict preserves order despite reliability history,
  strict still skips known-broken channels, reliability mode reorders,
  /health exposes the cascade mode.
"""
from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import patch

import pytest


def _load_daemon_class():
    """Import FleetNerveNATS lazily — skip gracefully if nats-py missing."""
    try:
        from tools.fleet_nerve_nats import FleetNerveNATS
    except ImportError as e:
        pytest.skip(f"fleet_nerve_nats import unavailable: {e}")
    return FleetNerveNATS


def _make_daemon(env_mode: str | None = None):
    """Construct a FleetNerveNATS with env-configured mode, minus NATS.

    We patch only the env var; the daemon __init__ avoids any NATS handshake
    (connection happens in async connect(), which we never call).
    """
    FleetNerveNATS = _load_daemon_class()
    env = dict(os.environ)
    if env_mode is None:
        env.pop("FLEET_CHANNEL_ORDER", None)
    else:
        env["FLEET_CHANNEL_ORDER"] = env_mode
    with patch.dict(os.environ, env, clear=True):
        daemon = FleetNerveNATS(node_id="test-node", nats_url="nats://127.0.0.1:4222")
    return daemon


def _install_channel_mocks(daemon, attempts: list, delivering: str | None):
    """Replace every _send_pN_* method with a mock that records the call order.

    ``attempts`` accumulates (channel_name, target) tuples in call order.
    If ``delivering == channel_name`` that mock returns delivered=True; all
    others return delivered=False. Set delivering=None to fail every channel
    (cascade exhausts).
    """
    channel_to_attr = {
        "P0_cloud":   "_send_p0_cloud",
        "P1_nats":    "_send_p1_nats",
        "P2_http":    "_send_p2_http",
        "P3_chief":   "_send_p3_chief",
        "P4_seed":    "_send_p4_seed",
        "P5_ssh":     "_send_p5_ssh",
        "P6_wol":     "_send_p6_wol",
        "P7_git":     "_send_p7_git",
        "P0_discord": "_send_p0_discord",
    }

    def make_mock(ch_name: str):
        async def _mock(target, message):
            attempts.append((ch_name, target))
            if delivering == ch_name:
                return {"delivered": True, "method": f"mock_{ch_name}"}
            return {"delivered": False, "error": "mock_not_delivered"}
        return _mock

    for ch, attr in channel_to_attr.items():
        setattr(daemon, attr, make_mock(ch))


def _disable_side_effects(daemon):
    """Neuter gate, rate-limiter, store, and self-heal side-effects.

    Keeps the cascade pure: only the mocked channel senders matter.
    """
    # Gate — always pass
    daemon._send_gate = None
    daemon._store = None
    # Rate limiter — always allow
    daemon.rate_limiter.allow = lambda peer, msg_type: True
    # No self-heal side-effect from P3+ success
    daemon._attempt_self_heal = lambda *a, **kw: None


# ── tests ────────────────────────────────────────────────────────────


def test_default_is_strict():
    """No env var → mode is 'strict'."""
    daemon = _make_daemon(env_mode=None)
    assert daemon._channel_order_mode == "strict"


def test_strict_mode_preserves_order_despite_reliability():
    """Peer history strongly favours P5, but strict must still try P1→...→P5.

    Setup: P3 has 0% success history, P5 has 100% + recent bonus.
    Reliability mode would put P5 first. Strict must hit P1 → P2 → P3 → P4 → P5.
    """
    daemon = _make_daemon(env_mode="strict")
    _disable_side_effects(daemon)

    target = "peer-mac2"
    # Poison P3 reliability, boost P5
    daemon._channel_success_rate[target] = {
        "P3_chief": {"ok": 0, "fail": 50, "last_ok_ts": 0.0},
        "P5_ssh":   {"ok": 100, "fail": 0, "last_ok_ts": time.time()},
    }

    attempts: list = []
    # Let P5 deliver so cascade stops at expected depth
    _install_channel_mocks(daemon, attempts, delivering="P5_ssh")

    result = asyncio.run(daemon.send_with_fallback(target, {"type": "context"}))

    # Must be tried in static order up to P5
    names = [a[0] for a in attempts]
    assert names == ["P1_nats", "P2_http", "P3_chief", "P4_seed", "P5_ssh"], (
        f"strict mode violated P0→P7 order: {names}"
    )
    assert result["delivered"] is True
    assert result["channel"] == "P5_ssh"


def test_strict_mode_skips_known_broken_continues_order():
    """Strict mode still skips broken channels — but order of the rest holds.

    Mark P3 broken (fail_rate=1.0 → channel-health broken). Cascade should go
    P1 → P2 → [P3 attempted, fails fast] → P4 → ... with relative order
    preserved. In current implementation channels aren't pre-filtered; the
    skip is enforced by the fail-fast timeout half (fail_rate>0.5) and the
    broken channel health record. This test asserts the strict invariant:
    *relative order between healthy channels is never swapped.*
    """
    daemon = _make_daemon(env_mode="strict")
    _disable_side_effects(daemon)

    target = "peer-mac3"
    # P3 is "broken" — fail_rate 100% so base rate 0 + fail_penalty caps out.
    # In reliability mode this would sink P3 to the bottom. Strict keeps it
    # at position 3 (then its mock returns not-delivered and cascade moves on).
    daemon._channel_success_rate[target] = {
        "P3_chief": {"ok": 0, "fail": 200, "last_ok_ts": 0.0},
    }

    attempts: list = []
    # P4 delivers — so we observe P3 was attempted-then-skipped (not hoisted).
    _install_channel_mocks(daemon, attempts, delivering="P4_seed")

    result = asyncio.run(daemon.send_with_fallback(target, {"type": "context"}))

    names = [a[0] for a in attempts]
    # Core strict invariant: P3 appears at index 2, BEFORE P4, even though
    # its reliability score would demote it. Completeness (skip) is orthogonal.
    assert names == ["P1_nats", "P2_http", "P3_chief", "P4_seed"], (
        f"strict mode reordered or skipped out of sequence: {names}"
    )
    assert result["channel"] == "P4_seed"


def test_reliability_mode_reorders():
    """Reliability mode: P5 with 100% + recent bonus beats P3 with 0%.

    Same fixture as the strict test but mode=reliability — expect P5 tried
    BEFORE P3.
    """
    daemon = _make_daemon(env_mode="reliability")
    _disable_side_effects(daemon)

    target = "peer-mac2"
    daemon._channel_success_rate[target] = {
        "P3_chief": {"ok": 0, "fail": 50, "last_ok_ts": 0.0},
        "P5_ssh":   {"ok": 100, "fail": 0, "last_ok_ts": time.time()},
    }

    attempts: list = []
    # Let P5 deliver so cascade stops once the reordered winner fires.
    _install_channel_mocks(daemon, attempts, delivering="P5_ssh")

    result = asyncio.run(daemon.send_with_fallback(target, {"type": "context"}))

    names = [a[0] for a in attempts]
    # P5 must appear before P3 in call order (reliability reorder)
    assert "P5_ssh" in names and "P3_chief" not in names[: names.index("P5_ssh")], (
        f"reliability mode did not promote P5 over P3: {names}"
    )
    assert result["delivered"] is True
    assert result["channel"] == "P5_ssh"


def test_health_exposes_cascade_mode():
    """/health payload must include cascade.mode so operators can audit."""
    daemon = _make_daemon(env_mode="strict")
    health = daemon._build_health()
    assert "cascade" in health, "health missing cascade section"
    assert health["cascade"]["mode"] == "strict"

    daemon2 = _make_daemon(env_mode="reliability")
    health2 = daemon2._build_health()
    assert health2["cascade"]["mode"] == "reliability"


def test_invalid_env_value_defaults_to_strict():
    """Typos like FLEET_CHANNEL_ORDER=fast must not silently enable anything."""
    daemon = _make_daemon(env_mode="fast")  # invalid
    assert daemon._channel_order_mode == "strict", (
        "invalid FLEET_CHANNEL_ORDER must default to strict, not silently accepted"
    )
