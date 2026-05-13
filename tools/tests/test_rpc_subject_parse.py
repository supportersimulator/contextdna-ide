"""RACE AH — regression test for `_handle_rpc` subject parsing.

Latent bug (race/y-rpc-mac1-diagnosis.md, latent since 1e63b33c):

    method = subject_parts[-1] if len(subject_parts) >= 4 else "status"

`rpc.mac1.health` splits to 3 parts, NOT 4 — so every method silently
fell through to ``"status"``. Daemon then returned status-shape
(``node_id``, snake_case) instead of health-shape (``nodeId``,
camelCase) — masking the daemon's true reply path behind a co-responder
race with discord-bridge.

This test runs ``_handle_rpc`` against synthetic ``rpc.<node>.<method>``
subjects and asserts the dispatched method matches the subject token,
not the fall-through default.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _bind(method_name: str):
    try:
        from tools.fleet_nerve_nats import FleetNerveNATS
    except ImportError as e:
        pytest.skip(f"fleet_nerve_nats import unavailable in test env: {e}")
    return getattr(FleetNerveNATS, method_name)


class _RecordingNC:
    """Minimal stand-in for ``nats.NATS`` — captures publishes."""

    def __init__(self):
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


def _make_holder():
    """Holder wired with the attributes ``_handle_rpc`` reads.

    We stub the four ``_build_*`` helpers + struggle counters so the
    method-dispatch path is observable without standing up a full
    FleetNerveNATS instance (NATS, asyncio loop, JetStream, ...).
    """
    nc = _RecordingNC()
    holder = SimpleNamespace()
    holder.node_id = "mac-test"
    holder.nc = nc
    # Distinct sentinel reply per method so we can assert which builder ran.
    holder._build_rpc_status = MagicMock(return_value={"_called": "status"})
    holder._build_health = MagicMock(return_value={"_called": "health"})
    holder._recent_self_commits = MagicMock(return_value=["sha-a", "sha-b"])
    holder._read_current_tracks = MagicMock(return_value=[{"track": "t1"}])
    holder._struggle_record_success = MagicMock()
    holder._struggle_record_failure = MagicMock()
    return holder


def _run_dispatch(holder, subject: str, reply: str = "_INBOX.test"):
    handler = _bind("_handle_rpc")
    msg = SimpleNamespace(subject=subject, reply=reply, data=b"ping")
    asyncio.run(handler(holder, msg))


def _last_payload(holder) -> dict:
    assert holder.nc.published, "handler never published a reply"
    subj, payload = holder.nc.published[-1]
    return json.loads(payload.decode())


# ── Core regression: rpc.<node>.health must reach _build_health ──────


def test_health_subject_dispatches_to_build_health():
    """`rpc.mac1.health` must invoke ``_build_health`` (camelCase shape).

    Pre-fix: subject_parts == ['rpc','mac1','health'] (len 3), the
    `>= 4` guard fell through to "status" → wrong builder, wrong shape.
    Post-fix: index 2 ('health') routes to ``_build_health``.
    """
    holder = _make_holder()
    _run_dispatch(holder, "rpc.mac1.health")

    holder._build_health.assert_called_once()
    holder._build_rpc_status.assert_not_called()
    payload = _last_payload(holder)
    assert payload == {"_called": "health"}, (
        f"health subject must reach _build_health, got: {payload!r}"
    )
    # The success-path should bump the consecutive-success counter.
    holder._struggle_record_success.assert_called_with(
        "consecutive_failed_health_replies",
    )


def test_status_subject_dispatches_to_build_rpc_status():
    """`rpc.mac2.status` must still route to ``_build_rpc_status``."""
    holder = _make_holder()
    _run_dispatch(holder, "rpc.mac2.status")

    holder._build_rpc_status.assert_called_once()
    holder._build_health.assert_not_called()
    assert _last_payload(holder) == {"_called": "status"}


def test_commits_subject_dispatches_to_recent_commits():
    """`rpc.mac3.commits` must produce ``{"commits": [...]}``."""
    holder = _make_holder()
    _run_dispatch(holder, "rpc.mac3.commits")

    holder._recent_self_commits.assert_called_once()
    payload = _last_payload(holder)
    assert payload == {"commits": ["sha-a", "sha-b"]}


def test_tracks_subject_dispatches_to_current_tracks():
    """`rpc.mac1.tracks` must produce ``{"tracks": [...]}``."""
    holder = _make_holder()
    _run_dispatch(holder, "rpc.mac1.tracks")

    holder._read_current_tracks.assert_called_once()
    payload = _last_payload(holder)
    assert payload == {"tracks": [{"track": "t1"}]}


def test_unknown_method_returns_error_envelope():
    """`rpc.mac1.bogus` must surface as a structured error, not 'status'."""
    holder = _make_holder()
    _run_dispatch(holder, "rpc.mac1.bogus")

    holder._build_rpc_status.assert_not_called()
    holder._build_health.assert_not_called()
    payload = _last_payload(holder)
    assert "error" in payload
    assert "bogus" in payload["error"]
    assert payload["valid"] == ["status", "health", "commits", "tracks"]


def test_short_subject_falls_through_to_status():
    """A malformed (too-short) subject keeps the safe ``status`` default."""
    holder = _make_holder()
    _run_dispatch(holder, "rpc.malformed")  # only 2 parts

    holder._build_rpc_status.assert_called_once()
    holder._build_health.assert_not_called()


def test_no_reply_subject_skips_publish():
    """Without `msg.reply`, the handler must not publish anything."""
    holder = _make_holder()
    handler = _bind("_handle_rpc")
    msg = SimpleNamespace(subject="rpc.mac1.health", reply=None, data=b"ping")
    asyncio.run(handler(holder, msg))

    assert holder.nc.published == [], "no reply subject => no publish"
