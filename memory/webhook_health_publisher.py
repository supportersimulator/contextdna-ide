"""Singleton WebhookHealthPublisher wiring for the webhook generation path.

RACE O3 — Wires :class:`tools.webhook_health_bridge.WebhookHealthPublisher`
into ``memory.persistent_hook_structure.generate_context_injection`` so that
the daemon's ``/health.webhook`` endpoint actually receives data.

Design constraints:

- **Fire-and-forget** — webhook hot path must NEVER block on NATS.
- **ZSF (zero silent failures)** — every error path increments the publisher's
  ``publish_errors`` counter. The counter is observable via daemon /health
  (publisher.stats()).
- **Lazy** — NATS connection is opened on first publish, on a background
  thread/loop. If the daemon process is on the SAME host, the bridge attaches
  to the local NATS broker (``nats://127.0.0.1:4222`` by default).
- **Per-process singleton** — webhook generation may be called many times in
  one Python process. We construct exactly one publisher.
- **Graceful degrade** — if ``nats-py`` is not installed, the publisher still
  returns an instance, just with no transport. ``publish_errors`` increments
  on each call so the gap is observable, but injection never fails.

The webhook caller imports :func:`get_publisher` and invokes
:meth:`WebhookHealthPublisher.publish` after generation completes. Tests can
inject a mock publisher via :func:`set_publisher_for_test`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
from typing import Optional

logger = logging.getLogger(__name__)


_lock = threading.Lock()  # protects _publisher_singleton
_loop_lock = threading.Lock()  # protects _loop / _loop_thread (independent)
_publisher_singleton = None  # Optional[WebhookHealthPublisher]
_loop_thread: Optional[threading.Thread] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


# ── Configuration ───────────────────────────────────────────────────────────


def _resolve_node_id() -> str:
    """Resolve node_id without raising. Mirrors fleet_config priority order.

    The webhook hot path may run in a Claude Code / MCP process that does
    NOT have ``multi-fleet/`` on ``sys.path``. The original implementation
    fell straight through to ``socket.gethostname()`` — silently
    publishing to ``event.webhook.completed.<hostname>`` instead of the
    ``event.webhook.completed.mac1`` the daemon subscribes to. Symptom:
    /health.events_recorded stays at 0 forever (observed 2026-04-26).

    Resolution order:
      1. ``MULTIFLEET_NODE_ID`` env (fast path).
      2. ``multifleet.fleet_config.get_node_id`` if importable.
      3. ``.multifleet/config.json`` — match this host's hostname /
         primary IPs against ``nodes.<id>.mdns_name`` /
         ``nodes.<id>.{host,lan_ip,tailscale_ip}``.
      4. Fallback to short hostname (last resort, prone to subject-name
         drift across the fleet).
    """
    env = os.environ.get("MULTIFLEET_NODE_ID")
    if env:
        return env
    try:
        from multifleet.fleet_config import get_node_id  # type: ignore

        node_id = get_node_id()
        if node_id:
            return node_id
    except Exception as exc:  # noqa: BLE001 — never raise from helper
        logger.debug("webhook_health_publisher: fleet_config get_node_id failed: %s", exc)

    cfg_match = _resolve_node_id_from_config()
    if cfg_match:
        return cfg_match

    return socket.gethostname().split(".")[0] or "unknown"


def _resolve_node_id_from_config() -> Optional[str]:
    """Walk up from this file looking for .multifleet/config.json and
    match the local host against any ``nodes`` entry. Returns the node
    id on a match, None otherwise. Never raises.
    """
    import json
    from pathlib import Path
    try:
        here = Path(__file__).resolve().parent
    except Exception:
        return None
    cfg_path: Optional[Path] = None
    for parent in [here, *here.parents]:
        candidate = parent / ".multifleet" / "config.json"
        if candidate.is_file():
            cfg_path = candidate
            break
    if cfg_path is None:
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("webhook_health_publisher: cfg read failed: %s", exc)
        return None
    nodes = cfg.get("nodes", {})
    if not isinstance(nodes, dict):
        return None

    # Collect this host's identifiers — hostname (full + short), and
    # any IPs we can resolve cheaply without dragging in netifaces.
    host_full = socket.gethostname() or ""
    host_short = host_full.split(".")[0] if host_full else ""
    host_ids = {host_full.lower(), host_short.lower()}
    try:
        host_ids.add(socket.gethostbyname(host_full).lower())
    except Exception:
        pass

    for node_id, node_cfg in nodes.items():
        if not isinstance(node_cfg, dict):
            continue
        candidates = {
            str(node_cfg.get("mdns_name", "")).lower(),
            str(node_cfg.get("host", "")).lower(),
            str(node_cfg.get("lan_ip", "")).lower(),
            str(node_cfg.get("tailscale_ip", "")).lower(),
        }
        # Allow mdns "aarons-macbook-pro.local" to match host
        # "aarons-mbp" only when the bare prefix coincides; otherwise
        # require an exact match. Avoids accidentally promoting a
        # peer's id if hostnames are similar.
        for cand in candidates:
            if cand and cand in host_ids:
                return node_id
            short = cand.split(".")[0] if cand else ""
            if short and short in host_ids:
                return node_id
    return None


def _resolve_nats_url() -> str:
    """Resolve NATS URL. Env > loopback default."""
    return os.environ.get("NATS_URL") or "nats://127.0.0.1:4222"


# ── Background loop (single shared) ─────────────────────────────────────────


def _ensure_background_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Spin up a single asyncio loop on a daemon thread.

    The publisher schedules coroutines onto this loop via
    ``run_coroutine_threadsafe``. Reuses one loop for the whole process.
    """
    global _loop_thread, _loop

    if _loop is not None and not _loop.is_closed():
        return _loop

    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop

        ready = threading.Event()
        loop_holder: dict = {}

        def _run() -> None:
            try:
                lp = asyncio.new_event_loop()
                loop_holder["loop"] = lp
                asyncio.set_event_loop(lp)
                ready.set()
                lp.run_forever()
            except Exception as exc:  # noqa: BLE001 — never let thread die silently
                logger.warning(
                    "webhook_health_publisher: background loop crashed: %s: %s",
                    type(exc).__name__, exc,
                )
                ready.set()

        t = threading.Thread(
            target=_run,
            name="webhook-health-publisher-loop",
            daemon=True,
        )
        t.start()
        # Wait briefly for loop creation; never block forever.
        ready.wait(timeout=2.0)
        _loop_thread = t
        _loop = loop_holder.get("loop")
        return _loop


# ── NATS transport (lazy) ───────────────────────────────────────────────────


_nats_client = None  # nats.aio.client.Client | None
_nats_connecting = False
# Y4 cleanup (2026-05-07): a second concurrent _get_or_connect_nats call returns
# None *intentionally* (we don't want N parallel TCP connects fighting). The
# caller used to surface this as RuntimeError("nats-unavailable") which got
# logged identically to a real transport failure — earlier in this session that
# misled Atlas into thinking the webhook was broken. We now raise
# NatsConcurrentDropOK so the caller can distinguish "intentional drop" from
# "real failure" and bump a separate counter.
_nats_concurrent_drops = 0
_nats_concurrent_drops_lock = threading.Lock()


class NatsConcurrentDropOK(Exception):
    """Raised when a publish overlaps an in-flight NATS connect attempt.

    BY DESIGN — only one connect runs at a time. Subsequent overlapping
    publishes are intentionally dropped. Distinct exception class so callers
    can route this to a different counter / log level than real transport
    failures.
    """


def _bump_concurrent_drop() -> int:
    global _nats_concurrent_drops
    with _nats_concurrent_drops_lock:
        _nats_concurrent_drops += 1
        return _nats_concurrent_drops


def get_concurrent_drops_total() -> int:
    """Return the monotonic concurrent-drop counter (observability)."""
    with _nats_concurrent_drops_lock:
        return _nats_concurrent_drops


def reset_concurrent_drops_for_test() -> None:
    """Tests only — reset the concurrent-drop counter."""
    global _nats_concurrent_drops
    with _nats_concurrent_drops_lock:
        _nats_concurrent_drops = 0


async def _get_or_connect_nats(nats_url: str):
    """Async — return a connected NATS client or None on failure.

    Raises :class:`NatsConcurrentDropOK` when a concurrent connect is in
    flight (intentional drop, not a real error). Returns ``None`` for real
    transport failures (nats-py missing, connect timed out, etc).
    """
    global _nats_client, _nats_connecting

    if _nats_client is not None and not _nats_client.is_closed:
        return _nats_client

    if _nats_connecting:
        # Another publish is already opening the connection — intentional drop.
        # Distinct exception so callers don't conflate this with "transport
        # broken". Counter is separate from publish_errors.
        _bump_concurrent_drop()
        raise NatsConcurrentDropOK("concurrent-connect-in-flight")
    _nats_connecting = True
    try:
        try:
            import nats  # type: ignore
        except ImportError:
            logger.debug("webhook_health_publisher: nats-py not installed")
            return None
        try:
            client = await asyncio.wait_for(
                nats.connect(nats_url, name="webhook-health-publisher"),
                timeout=2.0,
            )
            _nats_client = client
            return client
        except Exception as exc:  # noqa: BLE001 — connection failures observable via counter
            logger.debug(
                "webhook_health_publisher: NATS connect failed (%s): %s",
                type(exc).__name__, exc,
            )
            return None
    finally:
        _nats_connecting = False


def _build_async_publish(nats_url: str, publisher_ref=None):
    """Return an async publish callable bound to a lazy NATS client.

    ``publisher_ref`` is a callable returning the
    ``WebhookHealthPublisher`` so the inner background task can bump its
    ``publish_errors`` counter on transport failure (ZSF — race/o3
    originally swallowed background-task exceptions, leaving
    ``events_recorded=0`` with no observable error).
    """

    async def _publish(subject: str, data: bytes) -> None:
        try:
            client = await _get_or_connect_nats(nats_url)
            if client is None:
                raise RuntimeError("nats-unavailable")
            await client.publish(subject, data)
        except NatsConcurrentDropOK as exc:
            # Intentional drop — log at DEBUG, do NOT bump publish_errors.
            # Counter already bumped inside _get_or_connect_nats. Re-raise
            # so the CLI / caller can distinguish from a real failure.
            logger.debug(
                "webhook_health_publisher: concurrent-drop-ok (%s)", exc,
            )
            raise
        except Exception as exc:  # noqa: BLE001 — record-and-re-raise at edge
            logger.debug(
                "webhook_health_publisher: background publish failed (%s): %s",
                type(exc).__name__, exc,
            )
            try:
                pub = publisher_ref() if publisher_ref is not None else None
                if pub is not None and hasattr(pub, "_record_error"):
                    pub._record_error("background-publish", exc)
                elif pub is not None:
                    pub.publish_errors += 1
            except Exception:  # noqa: BLE001
                pass
            raise

    return _publish


# ── Singleton accessor ──────────────────────────────────────────────────────


def get_publisher():
    """Return the per-process singleton publisher (lazy-built).

    Always returns an instance — never None. If transport setup fails the
    publisher stays with no transport; ``publish()`` calls will increment
    ``publish_errors`` so the gap is observable.
    """
    global _publisher_singleton
    if _publisher_singleton is not None:
        return _publisher_singleton

    with _lock:
        if _publisher_singleton is not None:
            return _publisher_singleton

        try:
            from tools.webhook_health_bridge import WebhookHealthPublisher
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "webhook_health_publisher: bridge import failed: %s: %s",
                type(exc).__name__, exc,
            )
            # Build a minimal stub so callers don't have to None-check.
            class _NoOp:
                publish_errors = 0
                publish_attempts = 0

                def publish(self, *_a, **_k):  # noqa: D401
                    self.publish_attempts += 1
                    self.publish_errors += 1
                    return False

                def stats(self):  # noqa: D401
                    return {
                        "webhook_publish_attempts": self.publish_attempts,
                        "webhook_publish_errors": self.publish_errors,
                    }

            _publisher_singleton = _NoOp()
            return _publisher_singleton

        node_id = _resolve_node_id()
        nats_url = _resolve_nats_url()

        pub = WebhookHealthPublisher(node_id=node_id)

        # Wire transport — best-effort. If anything fails, publisher exists
        # without transport (ZSF: stays observable via counters). The
        # ``publisher_ref`` lambda gives the background-task error path
        # access to the publisher's counter (see _build_async_publish).
        try:
            loop = _ensure_background_loop()
            if loop is not None:
                pub.set_transport(
                    _build_async_publish(nats_url, publisher_ref=lambda: pub),
                    loop,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "webhook_health_publisher: transport wire failed: %s: %s",
                type(exc).__name__, exc,
            )

        _publisher_singleton = pub
        return _publisher_singleton


# ── Test hooks ──────────────────────────────────────────────────────────────


def set_publisher_for_test(pub) -> None:
    """Inject a mock publisher (tests only)."""
    global _publisher_singleton
    _publisher_singleton = pub


def reset_publisher_for_test() -> None:
    """Forget the cached publisher so the next call rebuilds (tests only)."""
    global _publisher_singleton
    _publisher_singleton = None


__all__ = [
    "get_publisher",
    "set_publisher_for_test",
    "reset_publisher_for_test",
    "NatsConcurrentDropOK",
    "get_concurrent_drops_total",
    "reset_concurrent_drops_for_test",
]


# ── CLI entry point ─────────────────────────────────────────────────────────
#
# Webhook dead-air root cause (audit 2026-05-04-A1): the publisher is wired
# only into ``persistent_hook_structure.generate_context_injection`` which is
# called exclusively by ``scripts/auto-memory-query.sh`` when INJECTION_MODE
# != "layered". Production runs in "layered" mode (default, no
# ``.context_injection_mode`` file present) so the publish hook never fires
# and ``/health.webhook.events_recorded`` is stuck at 0.
#
# Until the layered hook code path is taught to publish, operators (and
# follow-up agents) need a one-shot way to publish a synthetic completion
# event. This CLI is that lever — explicitly out-of-band, never imported by
# the hot path.
#
#     python3 -m memory.webhook_health_publisher publish \
#         --total-ms 250 --section section_0:50:ok --section section_4:120:fail
#
# All errors increment the publisher's counters (ZSF — observable via
# /health._stats.webhook_publish_errors). Exit code is 0 on dispatched, 2
# on transport-missing, 1 on argparse / CLI usage error.


def _cli_publish(argv: Optional[list] = None) -> int:
    """Out-of-band publish helper. Returns process exit code.

    Section flag format: ``--section <name>:<latency_ms>:<ok|fail>``. Repeat
    the flag for multiple sections. ``--missing <name>`` repeats. The publish
    is fire-and-forget through the singleton publisher; we sleep briefly
    after dispatch so the background loop's coroutine has time to send
    before the process exits.
    """
    import argparse
    import time as _time

    parser = argparse.ArgumentParser(
        prog="memory.webhook_health_publisher",
        description="Out-of-band webhook health publish (RACE N2 producer side).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_pub = sub.add_parser("publish", help="Publish a webhook completion event.")
    p_pub.add_argument("--total-ms", type=int, required=True)
    p_pub.add_argument(
        "--section",
        action="append",
        default=[],
        help="name:latency_ms:ok|fail (repeatable)",
    )
    p_pub.add_argument("--missing", action="append", default=[])
    p_pub.add_argument(
        "--cache-hit",
        choices=("true", "false"),
        default=None,
    )
    p_pub.add_argument(
        "--wait-s",
        type=float,
        default=1.0,
        help="seconds to wait after dispatch so background publish completes",
    )
    args = parser.parse_args(argv)

    sections: dict = {}
    for raw in args.section:
        try:
            name, lat, status = raw.split(":", 2)
            sections[name.strip()] = {
                "latency_ms": int(lat),
                "ok": status.strip().lower() in ("ok", "true", "1"),
            }
        except Exception as exc:  # noqa: BLE001 — surface parse errors
            print(f"bad --section value {raw!r}: {exc}", flush=True)
            return 1

    cache_hit: Optional[bool]
    if args.cache_hit is None:
        cache_hit = None
    else:
        cache_hit = args.cache_hit == "true"

    pub = get_publisher()
    drops_before = get_concurrent_drops_total()
    ok = pub.publish(
        total_ms=args.total_ms,
        sections=sections,
        missing=args.missing,
        cache_hit=cache_hit,
    )
    # Give the background loop a beat to deliver before process exit.
    if args.wait_s > 0:
        _time.sleep(args.wait_s)

    stats = pub.stats() if hasattr(pub, "stats") else {}
    drops_after = get_concurrent_drops_total()
    drops_delta = drops_after - drops_before
    # Distinguish "concurrent-drop-ok" (intentional, ZSF — bumps a separate
    # counter, not publish_errors) from real transport failures.
    print(
        f"dispatched={ok} attempts={stats.get('webhook_publish_attempts')} "
        f"errors={stats.get('webhook_publish_errors')} "
        f"concurrent_drops_total={drops_after} concurrent_drops_delta={drops_delta}",
        flush=True,
    )
    if drops_delta > 0 and not ok:
        # Drop is by design — emit a distinct marker so log filters can
        # ignore it instead of treating it as "nats-unavailable".
        print("concurrent-drop-ok", flush=True)
    if ok:
        return 0
    # Transport not wired (no nats-py, no daemon) — distinct exit code so
    # operators can tell "tried but failed" from "argparse rejected".
    return 2


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    import sys as _sys

    _sys.exit(_cli_publish(_sys.argv[1:]))
