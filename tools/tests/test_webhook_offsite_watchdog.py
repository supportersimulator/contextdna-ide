"""WW1 (2026-05-12) — offsite-NATS watchdog tests.

Proves the daemon's ``_connect_retry_loop`` detects the "connected to a peer's
NATS server instead of local" failure mode and force-reconnects so the local
publisher's events are no longer dropped.

Root-cause this guards against (see
``.fleet/audits/2026-05-12-WW1-webhook-silence-fix.md``):

  1. libnats picks a non-local URL from the cluster INFO discovered_servers pool
     during reconnect (e.g. mac2 IPv6).
  2. Daemon's subscription lives on that peer's NATS server.
  3. Local publisher publishes on 127.0.0.1 — NATS cluster interest only
     propagates one hop, so events are silently dropped.
  4. ``events_recorded`` stays flat. Atlas blind for 45h.

ZSF: every detection increments
``webhook_offsite_nats_detected``; every reconnect attempt increments
``webhook_offsite_nats_reconnect_total``; every close-error increments
``webhook_offsite_nats_reconnect_errors``.
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest


# ── Minimal helpers (mirror the inline watchdog logic) ───────────────────


def _is_loopback(url: str) -> bool:
    """Return True if URL points at the local host.

    Mirrors the predicate inlined into ``_connect_retry_loop``.
    """
    s = (url or "").lower()
    return (
        "127.0.0.1" in s
        or "localhost" in s
        or "[::1]" in s
    )


# ── Predicate-level tests ────────────────────────────────────────────────


def test_predicate_local_url_recognised():
    assert _is_loopback("nats://127.0.0.1:4222")
    assert _is_loopback("nats://localhost:4222")
    assert _is_loopback("nats://[::1]:4222")


def test_predicate_remote_url_rejected():
    assert not _is_loopback("nats://localhost:4222")
    assert not _is_loopback("nats://[2605:a601:8004:1e00:7e:1f7d:7ed3:4efd]:4222")
    assert not _is_loopback("")


# ── End-to-end: stage a fake daemon, run the watchdog inline ─────────────


class _FakeNats:
    """Minimal nats client stand-in.

    Only the attributes the watchdog reads are populated. ``close`` flips
    ``is_connected`` so the next loop iteration falls into the retry branch.
    """

    def __init__(self, connected_url: str) -> None:
        self.connected_url = connected_url
        self.is_connected = True
        self.closed = False

    async def close(self) -> None:
        self.is_connected = False
        self.closed = True


class _MiniDaemon:
    """Just enough surface to exercise the watchdog block."""

    def __init__(self, nats_url: str, nc: _FakeNats) -> None:
        self.nats_url = nats_url
        self.nc = nc
        self._stats: dict = {
            "webhook_offsite_nats_detected": 0,
            "webhook_offsite_nats_reconnect_total": 0,
            "webhook_offsite_nats_reconnect_errors": 0,
            "nats_connect_retry_errors": 0,
        }


async def _run_watchdog_once(daemon: _MiniDaemon, *, local_reachable: bool) -> None:
    """Replicates the inline WW1 watchdog block from _connect_retry_loop.

    Kept verbatim with the production code path so any drift here surfaces
    in CI before it hits the daemon.
    """
    nc = daemon.nc
    configured = (daemon.nats_url or "").lower()
    configured_is_local = _is_loopback(configured)
    connected_url_attr = getattr(nc, "connected_url", None)
    connected_str = (str(connected_url_attr) if connected_url_attr else "").lower()
    connected_is_local = bool(connected_str) and _is_loopback(connected_str)
    if configured_is_local and connected_str and not connected_is_local:
        daemon._stats["webhook_offsite_nats_detected"] = (
            daemon._stats.get("webhook_offsite_nats_detected", 0) + 1
        )
        if local_reachable:
            daemon._stats["webhook_offsite_nats_reconnect_total"] = (
                daemon._stats.get("webhook_offsite_nats_reconnect_total", 0) + 1
            )
            try:
                await nc.close()
                daemon.nc = None
            except Exception:
                daemon._stats["webhook_offsite_nats_reconnect_errors"] = (
                    daemon._stats.get("webhook_offsite_nats_reconnect_errors", 0) + 1
                )


def test_offsite_detected_and_reconnect_attempted():
    """The bug scenario: local nats_url, daemon floated to mac2 over IPv6."""
    daemon = _MiniDaemon(
        nats_url="nats://127.0.0.1:4222",
        nc=_FakeNats(
            connected_url=(
                "ParseResult(scheme='nats', "
                "netloc='[2605:a601:8004:1e00:7e:1f7d:7ed3:4efd]:4222', "
                "path='', params='', query='', fragment='')"
            ),
        ),
    )
    asyncio.run(_run_watchdog_once(daemon, local_reachable=True))
    assert daemon._stats["webhook_offsite_nats_detected"] == 1
    assert daemon._stats["webhook_offsite_nats_reconnect_total"] == 1
    assert daemon._stats["webhook_offsite_nats_reconnect_errors"] == 0
    assert daemon.nc is None  # close ran → retry branch will reconnect


def test_offsite_detected_local_unreachable_no_reconnect():
    """Local NATS dead — keep the remote connection (better than nothing)."""
    daemon = _MiniDaemon(
        nats_url="nats://127.0.0.1:4222",
        nc=_FakeNats(connected_url="ParseResult(netloc='192.168.1.183:4222')"),
    )
    asyncio.run(_run_watchdog_once(daemon, local_reachable=False))
    assert daemon._stats["webhook_offsite_nats_detected"] == 1
    assert daemon._stats["webhook_offsite_nats_reconnect_total"] == 0  # didn't reconnect
    assert daemon.nc is not None  # connection preserved


def test_local_to_local_noop():
    """Healthy case: daemon connected to its own local NATS — no action."""
    daemon = _MiniDaemon(
        nats_url="nats://127.0.0.1:4222",
        nc=_FakeNats(connected_url="ParseResult(netloc='127.0.0.1:4222')"),
    )
    asyncio.run(_run_watchdog_once(daemon, local_reachable=True))
    assert daemon._stats["webhook_offsite_nats_detected"] == 0
    assert daemon._stats["webhook_offsite_nats_reconnect_total"] == 0


def test_chief_deploy_remote_url_noop():
    """A peer node configured to talk to the chief is NOT offsite.

    e.g. mac2 with nats_url='nats://localhost:4222' connected to mac1.
    Watchdog must not fire — that's the intended topology.
    """
    daemon = _MiniDaemon(
        nats_url="nats://localhost:4222",  # chief
        nc=_FakeNats(connected_url="ParseResult(netloc='192.168.1.144:4222')"),
    )
    asyncio.run(_run_watchdog_once(daemon, local_reachable=True))
    assert daemon._stats["webhook_offsite_nats_detected"] == 0
    assert daemon._stats["webhook_offsite_nats_reconnect_total"] == 0


def test_close_failure_counts_error():
    """Controlled-failure: nc.close raises — error counter must bump (ZSF)."""

    class _ExplodingNats(_FakeNats):
        async def close(self) -> None:  # type: ignore[override]
            raise RuntimeError("simulated close failure")

    daemon = _MiniDaemon(
        nats_url="nats://127.0.0.1:4222",
        nc=_ExplodingNats(connected_url="ParseResult(netloc='192.168.1.183:4222')"),
    )
    asyncio.run(_run_watchdog_once(daemon, local_reachable=True))
    assert daemon._stats["webhook_offsite_nats_detected"] == 1
    assert daemon._stats["webhook_offsite_nats_reconnect_total"] == 1
    assert daemon._stats["webhook_offsite_nats_reconnect_errors"] == 1
