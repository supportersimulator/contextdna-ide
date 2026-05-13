#!/usr/bin/env python3
"""fleet-inbox-watch.py — event-driven replacement for fleet-inbox-watch.sh.

Replaces the 15s HTTP poll to chief with a NATS subscription on
`fleet.<node>.>` plus a JetStream durable consumer for offline catchup.

Behavior parity with the shell version:
  - On each incoming message, appends to /tmp/fleet-inbox-<node>.txt
  - Writes the latest batch to /tmp/fleet-inbox-NEW sentinel
  - Colored stdout for live terminal display

Wins:
  - 0s latency (push) vs 15s poll
  - Zero CPU burn when idle (epoll/kqueue under nats-py)
  - Offline-catchup via JetStream durable consumer (better than HTTP poll)
  - No CHIEF_URL coupling — any peer can publish

Usage:
  ./scripts/fleet-inbox-watch.py             # auto-detect node from hostname
  ./scripts/fleet-inbox-watch.py mac2        # explicit node id
  NATS_URL=nats://custom:4222 ./fleet-inbox-watch.py

LaunchAgent deploy: scripts/fleet-inbox-watch.plist.example (update Program path).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any, Optional

import nats
from nats.errors import TimeoutError as NATSTimeoutError

logging.basicConfig(
    level=os.environ.get("FLEET_INBOX_LOG_LEVEL", "INFO"),
    format="%(asctime)s [fleet-inbox] %(message)s",
)
log = logging.getLogger("fleet-inbox")

RED = "\033[0;31m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
RESET = "\033[0m"


def detect_node() -> str:
    """Resolve node id via the single source of truth (multifleet.fleet_config).

    Priority: ``MULTIFLEET_NODE_ID`` env > IP match against
    ``.multifleet/config.json`` > hostname.  OSS adopters whose nodes aren't
    named mac1/mac2/mac3 only need to populate the config file — no code edit
    needed.  Falls back to bare hostname when fleet_config isn't importable
    (e.g. this script is run from outside the repo).
    """
    env = os.environ.get("MULTIFLEET_NODE_ID")
    if env:
        return env
    try:
        # Make the package importable when running from any cwd inside the repo.
        repo_root = Path(__file__).resolve().parent.parent
        for candidate in (repo_root / "multi-fleet", repo_root):
            p = str(candidate)
            if p not in sys.path:
                sys.path.insert(0, p)
        from multifleet.fleet_config import get_node_id as _resolve  # type: ignore
        return _resolve()
    except Exception:
        return (socket.gethostname() or "").split(".")[0].lower()


def write_inbox_entry(inbox_log: Path, sentinel: Path, envelope: dict) -> None:
    """Append a message to the inbox log + write the sentinel."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "",
        "══════════════════════════════════",
        f"  Fleet Inbox — {ts}",
        "══════════════════════════════════",
        f"  From:     {envelope.get('from', '?')}",
        f"  Subject:  {envelope.get('subject', '(none)')}",
        f"  Priority: {envelope.get('priority', 'normal')}",
        f"  Sent:     {envelope.get('timestamp', envelope.get('sent_at', '?'))}",
        f"  Body:     {envelope.get('body', '')}",
        "",
    ]
    with inbox_log.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    sentinel.write_text(json.dumps({"count": 1, "messages": [envelope]}))
    # Colored terminal preview
    pri = str(envelope.get("priority", "normal")).upper()
    pri_color = RED if pri == "HIGH" else YELLOW if pri == "NORMAL" else CYAN
    sys.stdout.write(
        f"\n{BOLD}{YELLOW}╔══════════════════════════════════╗{RESET}\n"
        f"{BOLD}{YELLOW}║  Fleet Inbox — 1 new message      ║{RESET}\n"
        f"{BOLD}{YELLOW}╚══════════════════════════════════╝{RESET}\n"
        f"  {pri_color}[{pri}]{RESET} From {envelope.get('from', '?')}: "
        f"{envelope.get('subject', '')}\n  {envelope.get('body', '')}\n\n"
    )
    sys.stdout.flush()


async def run(node: str, nats_url: str, inbox_log: Path, sentinel: Path) -> None:
    """Subscribe to fleet.<node>.> and pipe each message into the inbox log."""
    async def _on_disconnect() -> None:
        log.warning("NATS disconnected; auto-reconnecting")

    async def _on_reconnect() -> None:
        log.info("NATS reconnected")

    async def _on_error(e: Exception) -> None:
        log.warning(f"NATS error: {e}")

    nc = await nats.connect(
        nats_url,
        reconnect_time_wait=2,
        max_reconnect_attempts=-1,
        disconnected_cb=_on_disconnect,
        reconnected_cb=_on_reconnect,
        error_cb=_on_error,
    )
    log.info(f"connected to {nats_url}; subscribing to fleet.{node}.>")

    # Optional JetStream durable consumer — gives offline replay on reconnect.
    # Degrades silently if JetStream not configured on the server.
    js_sub = None
    try:
        from multifleet.jetstream import (
            ensure_streams,
            create_durable_consumer,
            durable_consumer_name,
            replay_missed,
            STREAM_MESSAGES,
        )
        js = await ensure_streams(nc)
        if js is not None:
            durable = durable_consumer_name(STREAM_MESSAGES, f"{node}_inbox_watch")
            js_sub = await create_durable_consumer(
                js, STREAM_MESSAGES, durable, f"fleet.{node}.>", deliver_policy="all",
            )

            async def _replay_handler(msg: Any) -> None:
                try:
                    write_inbox_entry(inbox_log, sentinel, json.loads(msg.data))
                except Exception as e:
                    log.warning(f"replay handler error: {e}")

            replayed = await replay_missed(js_sub, _replay_handler)
            if replayed:
                log.info(f"replayed {replayed} missed message(s) from JetStream")
    except ImportError:
        log.debug("multifleet.jetstream unavailable — core pub/sub only")
    except Exception as e:
        log.warning(f"jetstream setup skipped: {e}")

    async def on_msg(msg: Any) -> None:
        try:
            envelope = json.loads(msg.data)
        except Exception as e:
            log.warning(f"skipping non-JSON message on {msg.subject}: {e}")
            return
        write_inbox_entry(inbox_log, sentinel, envelope)

    await nc.subscribe(f"fleet.{node}.>", cb=on_msg)
    log.info("watching — no polling, event-driven push")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down")
    if js_sub is not None:
        try:
            await js_sub.unsubscribe()
        except Exception:
            pass
    await nc.drain()


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    node = argv[0] if argv and not argv[0].startswith("-") else detect_node()
    nats_url = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
    inbox_log = Path(f"/tmp/fleet-inbox-{node}.txt")
    sentinel = Path("/tmp/fleet-inbox-NEW")
    log.info(f"node={node} nats={nats_url} inbox={inbox_log}")
    try:
        asyncio.run(run(node, nats_url, inbox_log, sentinel))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
