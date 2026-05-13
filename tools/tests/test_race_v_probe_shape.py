"""RACE AH/C — shape validation in scripts/race-v-live-cluster-smoke.py.

Diagnosis (race/y-rpc-mac1-diagnosis.md): the live-cluster smoke probe
treated ANY 2s reply on `rpc.<peer>.health` as PASS — including the
discord-bridge defaults stub::

    {"nodeId":"<peer>","uptime_s":0,"git_commit":"?", ...}

A real daemon `_build_health()` produces ``nodeId`` (camelCase),
positive ``uptime_s``, and ``git.commit`` populated. Stubs and
status-shape replies (post-AH/A bug, ``node_id`` snake_case) must now
register as DEGRADED with a ``shape_violations`` list explaining why.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "race-v-live-cluster-smoke.py"


def _load_probe_module():
    """Import scripts/race-v-live-cluster-smoke.py as a module.

    Hyphens forbid normal `import`; use the spec loader. Skips cleanly
    when nats-py isn't installed (probe imports `nats` at top level).
    """
    if not SCRIPT.exists():
        pytest.skip(f"probe script missing at {SCRIPT}")
    spec = importlib.util.spec_from_file_location("race_v_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ImportError as e:
        pytest.skip(f"probe module deps unavailable: {e}")
    return mod


def _fake_nc(reply_payload: bytes):
    """Mock `nats.NATS` returning ``reply_payload`` from request()."""
    nc = SimpleNamespace()
    msg = SimpleNamespace(data=reply_payload)
    nc.request = AsyncMock(return_value=msg)
    return nc


def _run_probe(peer: str, payload: bytes) -> dict:
    mod = _load_probe_module()
    nc = _fake_nc(payload)
    return asyncio.run(mod.check_peer_ping(nc, peer))


# ── Healthy path ───────────────────────────────────────────────────


def test_real_daemon_health_shape_passes():
    """Real ``_build_health`` payload ⇒ PASS, no shape_violations."""
    payload = json.dumps({
        "nodeId": "mac1",
        "status": "ok",
        "transport": "nats",
        "uptime_s": 7200,
        "git": {"commit": "deadbeef1234"},
        "peers": {},
    }).encode()

    result = _run_probe("mac1", payload)

    assert result["status"] == "PASS", result
    assert "shape_violations" not in result


# ── Stub-detection (the AH/B regression guard at probe layer) ──────


def test_discord_bridge_defaults_stub_marked_degraded():
    """The exact stub captured in race/y must register as DEGRADED.

    Pre-fix this returned PASS — masking mac1's daemon silence behind
    the bridge's fake-OK reply.
    """
    payload = json.dumps({
        "nodeId": "mac1",
        "uptime_s": 0,
        "git_commit": "?",
        "lastSeen_per_peer": {},
        "channel_health": {},
        "responder": "discord-bridge",
    }).encode()

    result = _run_probe("mac1", payload)

    assert result["status"] == "DEGRADED", result
    violations = result.get("shape_violations", [])
    assert any("uptime_s <= 0" in v for v in violations), violations
    assert any("git_commit" in v for v in violations), violations
    assert any("discord-bridge" in v for v in violations), violations


# ── Status-shape detection (the AH/A regression guard at probe layer) ─


def test_status_shape_reply_marked_degraded():
    """`_build_rpc_status` payload (snake_case `node_id`) must DEGRADE.

    This is what the daemon ACTUALLY returned pre-AH/A: subject parse
    fell through to "status" so health probes got status-shape.
    """
    payload = json.dumps({
        "node_id": "mac1",   # snake_case — wrong shape for health probe
        "status": "ok",
        "transport": "nats",
        "uptime_s": 7200,
        "git_head": "abc1234",
    }).encode()

    result = _run_probe("mac1", payload)

    assert result["status"] == "DEGRADED", result
    violations = result.get("shape_violations", [])
    assert any("status-shape" in v for v in violations), violations


# ── Other failure modes ────────────────────────────────────────────


def test_node_id_mismatch_marked_degraded():
    """Reply for the wrong node ⇒ DEGRADED."""
    payload = json.dumps({
        "nodeId": "mac9",   # asked for mac1, got mac9
        "uptime_s": 100,
        "git": {"commit": "abc"},
    }).encode()

    result = _run_probe("mac1", payload)

    assert result["status"] == "DEGRADED"
    violations = result.get("shape_violations", [])
    assert any("nodeId mismatch" in v for v in violations), violations


def test_non_json_reply_marked_degraded():
    """Garbage bytes ⇒ DEGRADED, not FAIL — server replied, just badly."""
    result = _run_probe("mac1", b"not-json-at-all")

    assert result["status"] == "DEGRADED"
    violations = result.get("shape_violations", [])
    assert any("not JSON" in v for v in violations), violations


def test_missing_uptime_marked_degraded():
    """Reply without ``uptime_s`` ⇒ DEGRADED."""
    payload = json.dumps({
        "nodeId": "mac1",
        "git": {"commit": "abc"},
        # uptime_s missing entirely
    }).encode()

    result = _run_probe("mac1", payload)

    assert result["status"] == "DEGRADED"
    assert any("uptime_s" in v for v in result["shape_violations"])
