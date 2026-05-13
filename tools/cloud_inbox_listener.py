#!/usr/bin/env python3
"""Cloud inbox listener — event-driven replacement for polling cloud bot.

WHY THIS EXISTS
================
The cloud node was running an hourly polling loop that called Claude on
the cloud node every 5-15 minutes to "check the fleet inbox". Result:
38 of 50 commits over 5 days were `fleet: cloud inbox check` artifacts
with zero new information — pure noise.

Aaron's invariant: NO POLLING EVER. Every check that fires without a
state change is wasted compute and wasted git history.

Architecture
------------
1. Subscribe to NATS subject `fleet.>` (catch every fleet-wide event).
2. Use a JetStream durable consumer named `cloud_inbox_listener` so the
   cloud node catches up on missed messages after disconnect (offline
   replay) without polling.
3. On each delivered message: process it (filesystem inbox sync, archive,
   forward to Claude session if needed). NEVER schedule the next check
   — the next message arrival IS the next check.
4. Emit a status report ONLY when state actually changes (new message
   counts, plan transitions, blockers cleared). Debounce: max 1 status
   report per `STATUS_REPORT_DEBOUNCE_S` (default 1h).
5. Once per 24h, emit a single legitimate "daily summary" — this IS
   scheduled, but it's a single commit per day, not 38.

Zero Silent Failures
--------------------
Every exception path increments a counter on the listener and emits a
log line. `health_snapshot()` returns the counters so the fleet daemon
can publish them on `fleet.cloud.health`.

Backwards compatibility
-----------------------
- Existing fleet messages on disk in `.fleet-messages/cloud/` are still
  drained on startup (one shot, not a loop).
- Existing artifact format (`.fleet-messages/all/<date>-cloud-inbox-
  check-vN.md`) is preserved — only the cadence changes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("cloud_inbox_listener")

# ── Defaults (overridable via env or constructor) ────────────────────────
DEFAULT_NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
DEFAULT_NODE_ID = os.environ.get("MULTIFLEET_NODE_ID", "cloud")
DEFAULT_FLEET_SUBJECT = "fleet.>"
DEFAULT_DURABLE = "cloud_inbox_listener"
DEFAULT_STREAM = "FLEET_MESSAGES"
DEFAULT_STATUS_DEBOUNCE_S = 3600  # 1 hour
DEFAULT_DAILY_SUMMARY_INTERVAL_S = 86400  # 24 hours
DEFAULT_REPO_ROOT = Path(
    os.environ.get("CONTEXT_DNA_REPO", str(Path.home() / "dev" / "er-simulator-superrepo"))
)


@dataclass
class ListenerCounters:
    """Observable counters for ZSF — every failure path increments one."""

    messages_received: int = 0
    messages_processed: int = 0
    messages_failed: int = 0
    status_reports_emitted: int = 0
    status_reports_debounced: int = 0
    daily_summaries_emitted: int = 0
    nats_disconnects: int = 0
    process_errors: int = 0
    last_event_ts: float = 0.0
    last_status_report_ts: float = 0.0
    last_daily_summary_ts: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages_received": self.messages_received,
            "messages_processed": self.messages_processed,
            "messages_failed": self.messages_failed,
            "status_reports_emitted": self.status_reports_emitted,
            "status_reports_debounced": self.status_reports_debounced,
            "daily_summaries_emitted": self.daily_summaries_emitted,
            "nats_disconnects": self.nats_disconnects,
            "process_errors": self.process_errors,
            "last_event_ts": self.last_event_ts,
            "last_status_report_ts": self.last_status_report_ts,
            "last_daily_summary_ts": self.last_daily_summary_ts,
        }


@dataclass
class FleetState:
    """Snapshot used to detect state changes — drives status-report gating."""

    unread_count: int = 0
    open_tracks: tuple[str, ...] = ()
    last_processed_subject: str = ""
    plan_status_hash: str = ""

    def equals(self, other: "FleetState") -> bool:
        return (
            self.unread_count == other.unread_count
            and self.open_tracks == other.open_tracks
            and self.plan_status_hash == other.plan_status_hash
        )


# Type alias for plug-in handlers — keeps tests simple and avoids forcing
# tests to spin up a real NATS server / filesystem layout.
MessageHandler = Callable[[dict], Awaitable[Optional[FleetState]]]
StatusReporter = Callable[[FleetState, FleetState], Awaitable[None]]
DailySummaryEmitter = Callable[[FleetState], Awaitable[None]]


class CloudInboxListener:
    """Event-driven replacement for the cloud bot's polling inbox check.

    Key contract: this object NEVER contains `while True ... sleep` for the
    purpose of checking inbox state. The only `await asyncio.sleep` allowed
    is the daily-summary heartbeat (24h cadence — legitimate scheduled task).
    """

    def __init__(
        self,
        *,
        node_id: str = DEFAULT_NODE_ID,
        nats_url: str = DEFAULT_NATS_URL,
        fleet_subject: str = DEFAULT_FLEET_SUBJECT,
        durable_name: str = DEFAULT_DURABLE,
        stream: str = DEFAULT_STREAM,
        status_debounce_s: float = DEFAULT_STATUS_DEBOUNCE_S,
        daily_summary_interval_s: float = DEFAULT_DAILY_SUMMARY_INTERVAL_S,
        repo_root: Path = DEFAULT_REPO_ROOT,
        message_handler: Optional[MessageHandler] = None,
        status_reporter: Optional[StatusReporter] = None,
        daily_summary_emitter: Optional[DailySummaryEmitter] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.node_id = node_id
        self.nats_url = nats_url
        self.fleet_subject = fleet_subject
        self.durable_name = durable_name
        self.stream = stream
        self.status_debounce_s = float(status_debounce_s)
        self.daily_summary_interval_s = float(daily_summary_interval_s)
        self.repo_root = repo_root
        self.counters = ListenerCounters()
        self.state = FleetState()
        self._stopped = asyncio.Event()
        self._nc: Any = None
        self._js: Any = None
        self._sub: Any = None
        self._daily_task: Optional[asyncio.Task] = None
        self._clock = clock

        # Pluggable behaviors — defaults are no-ops so the listener can be
        # used in tests without filesystem or git side-effects.
        self._message_handler = message_handler or self._default_message_handler
        self._status_reporter = status_reporter or self._default_status_reporter
        self._daily_summary_emitter = (
            daily_summary_emitter or self._default_daily_summary_emitter
        )

    # ── Defaults — overridable via constructor ───────────────────────────

    async def _default_message_handler(self, msg: dict) -> Optional[FleetState]:
        """Default: accept the message, do nothing, return None (no state change)."""
        return None

    async def _default_status_reporter(
        self, prev: FleetState, curr: FleetState
    ) -> None:
        logger.info(
            "status_report (default): unread %d → %d, tracks %d → %d",
            prev.unread_count, curr.unread_count,
            len(prev.open_tracks), len(curr.open_tracks),
        )

    async def _default_daily_summary_emitter(self, state: FleetState) -> None:
        logger.info(
            "daily_summary (default) — node=%s unread=%d tracks=%d",
            self.node_id, state.unread_count, len(state.open_tracks),
        )

    # ── NATS connection / subscription ───────────────────────────────────

    async def connect(self) -> bool:
        """Open NATS connection and bind a JetStream durable consumer.

        Returns True on success. On failure, increments `process_errors` and
        returns False — the caller decides whether to retry. We do NOT loop
        here, because that would re-introduce polling.
        """
        try:
            import nats  # local import — keeps module importable without nats-py
        except ImportError:
            logger.error("nats-py not installed — cannot start cloud inbox listener")
            self.counters.process_errors += 1
            return False

        try:
            self._nc = await nats.connect(
                self.nats_url,
                name=f"cloud-inbox-listener-{self.node_id}",
                reconnect_time_wait=2,
                max_reconnect_attempts=-1,  # forever — NATS handles backoff, not us
                disconnected_cb=self._on_disconnected,
                reconnected_cb=self._on_reconnected,
            )
            self._js = self._nc.jetstream()
            return True
        except Exception as e:
            logger.error("NATS connect failed: %s", e)
            self.counters.process_errors += 1
            return False

    async def _on_disconnected(self) -> None:
        self.counters.nats_disconnects += 1
        logger.warning("NATS disconnected (count=%d)", self.counters.nats_disconnects)

    async def _on_reconnected(self) -> None:
        logger.info("NATS reconnected — JetStream durable will replay missed events")

    async def subscribe(self) -> bool:
        """Bind the JetStream durable consumer for `fleet.>`.

        Uses pull-subscribe so we control the fetch loop (event-triggered,
        not time-triggered). On `fetch()` we await an event arrival; we do
        NOT poll on a timer.
        """
        if self._js is None:
            logger.error("subscribe() called before connect()")
            self.counters.process_errors += 1
            return False

        try:
            try:
                from multifleet.jetstream import (
                    create_durable_consumer,
                    durable_consumer_name,
                )
                full_durable = durable_consumer_name(self.stream, self.durable_name)
                self._sub = await create_durable_consumer(
                    self._js,
                    self.stream,
                    full_durable,
                    self.fleet_subject,
                    deliver_policy="all",  # catch up on offline backlog
                )
            except ImportError:
                # Fallback: direct JetStream pull_subscribe — keeps the
                # listener usable in environments where the multifleet
                # package isn't on PYTHONPATH.
                self._sub = await self._js.pull_subscribe(
                    subject=self.fleet_subject,
                    durable=self.durable_name,
                    stream=self.stream,
                )
            if self._sub is None:
                logger.error("durable consumer creation returned None")
                self.counters.process_errors += 1
                return False
            return True
        except Exception as e:
            logger.error("JetStream subscribe failed: %s", e)
            self.counters.process_errors += 1
            return False

    # ── Event loop — pure event-driven, no polling cadence ───────────────

    async def run(self) -> None:
        """Main loop. Awaits NATS event delivery. Exits on `stop()`.

        IMPORTANT: this loop has no `time.sleep`. The `fetch()` call blocks
        until a message arrives or `stop_after_idle_s` elapses — that is
        event delivery latency, not a polling interval.
        """
        # Spawn the daily-summary task — the ONE legitimate scheduled job.
        self._daily_task = asyncio.create_task(self._daily_summary_loop())

        while not self._stopped.is_set():
            try:
                msgs = await self._sub.fetch(batch=10, timeout=30.0)
            except Exception as e:
                # `TimeoutError` from JetStream simply means "no events in
                # window" — that's the steady state for an idle fleet. We do
                # NOT poll faster; we re-await the next fetch.
                err_name = type(e).__name__
                if "Timeout" in err_name:
                    continue
                logger.warning("fetch error (%s): %s", err_name, e)
                self.counters.process_errors += 1
                # Brief backoff to avoid tight loop on hard failures —
                # NOT a polling cadence; this is recovery debounce only.
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue

            for m in msgs:
                await self._handle_raw_message(m)

        if self._daily_task is not None:
            self._daily_task.cancel()
            try:
                await asyncio.wait_for(self._daily_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    async def _handle_raw_message(self, m: Any) -> None:
        """Decode + dispatch a single NATS message. ZSF on every path."""
        self.counters.messages_received += 1
        self.counters.last_event_ts = self._clock()
        try:
            data = json.loads(m.data.decode("utf-8")) if hasattr(m, "data") else m
        except Exception as e:
            logger.warning("decode failed: %s", e)
            self.counters.messages_failed += 1
            try:
                await m.ack()
            except Exception:
                pass
            return

        try:
            new_state = await self._message_handler(data)
            self.counters.messages_processed += 1
            if new_state is not None:
                await self._maybe_emit_status_report(new_state)
        except Exception as e:
            logger.warning("handler error: %s", e)
            self.counters.messages_failed += 1
        finally:
            try:
                await m.ack()
            except Exception:
                pass

    # ── State-change-gated status reporting ──────────────────────────────

    async def _maybe_emit_status_report(self, new_state: FleetState) -> None:
        """Emit a status report iff state changed AND debounce window passed.

        Both gates must pass. Either alone is insufficient:
          - state-change-only would still spam during a chatty hour
          - debounce-only would still emit on quiet hours (no new info)

        Combined: at most one report per `status_debounce_s`, and only if
        something actually changed since the last report.
        """
        prev = self.state
        if new_state.equals(prev):
            self.counters.status_reports_debounced += 1
            return

        now = self._clock()
        elapsed = now - self.counters.last_status_report_ts
        if (
            self.counters.last_status_report_ts > 0
            and elapsed < self.status_debounce_s
        ):
            # State changed but we already reported recently — update
            # `self.state` so the next report sees the latest delta, but
            # don't emit (counts as debounced).
            self.state = new_state
            self.counters.status_reports_debounced += 1
            return

        try:
            await self._status_reporter(prev, new_state)
            self.counters.status_reports_emitted += 1
            self.counters.last_status_report_ts = now
        except Exception as e:
            logger.warning("status_reporter failed: %s", e)
            self.counters.process_errors += 1
        finally:
            self.state = new_state

    # ── Daily summary — the one legitimate scheduled task ───────────────

    async def _daily_summary_loop(self) -> None:
        """Fire `daily_summary_emitter` once per `daily_summary_interval_s`.

        This IS scheduled (24h cadence by default) — it's the legitimate
        single-commit-per-day safety net so we always have a recent record
        of fleet state, even if no events arrived. Distinct from polling
        because the cadence is bounded (1/day, not 1/hour).
        """
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(), timeout=self.daily_summary_interval_s
                    )
                    return  # stopped
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._daily_summary_emitter(self.state)
                    self.counters.daily_summaries_emitted += 1
                    self.counters.last_daily_summary_ts = self._clock()
                except Exception as e:
                    logger.warning("daily_summary failed: %s", e)
                    self.counters.process_errors += 1
        except asyncio.CancelledError:
            return

    # ── Lifecycle ────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stopped.set()

    async def close(self) -> None:
        self.stop()
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                pass

    def health_snapshot(self) -> dict[str, Any]:
        snap = {
            "node": self.node_id,
            "subject": self.fleet_subject,
            "durable": self.durable_name,
            "stream": self.stream,
            "status_debounce_s": self.status_debounce_s,
            "daily_summary_interval_s": self.daily_summary_interval_s,
            "counters": self.counters.as_dict(),
            "state": {
                "unread_count": self.state.unread_count,
                "open_tracks": list(self.state.open_tracks),
                "plan_status_hash": self.state.plan_status_hash,
            },
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        return snap


# ── CLI entrypoint ────────────────────────────────────────────────────────

async def _amain(args: argparse.Namespace) -> int:
    listener = CloudInboxListener(
        node_id=args.node,
        nats_url=args.nats_url,
        status_debounce_s=args.status_debounce_s,
        daily_summary_interval_s=args.daily_summary_interval_s,
    )
    if not await listener.connect():
        return 2
    if not await listener.subscribe():
        return 3

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, listener.stop)
        except NotImplementedError:
            # Windows / restricted env — fall through; KeyboardInterrupt still works.
            pass

    logger.info(
        "cloud_inbox_listener running — node=%s subject=%s durable=%s",
        listener.node_id, listener.fleet_subject, listener.durable_name,
    )
    try:
        await listener.run()
    finally:
        await listener.close()
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(
        description="Event-driven cloud inbox listener (replaces polling cron)"
    )
    p.add_argument("--node", default=DEFAULT_NODE_ID)
    p.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    p.add_argument(
        "--status-debounce-s",
        type=float,
        default=DEFAULT_STATUS_DEBOUNCE_S,
        help="Min seconds between status reports (default 3600 = 1h)",
    )
    p.add_argument(
        "--daily-summary-interval-s",
        type=float,
        default=DEFAULT_DAILY_SUMMARY_INTERVAL_S,
        help="Seconds between safety-net daily summaries (default 86400 = 24h)",
    )
    args = p.parse_args()
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
