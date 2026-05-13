"""Webhook health bridge — surface webhook generation health on daemon /health.

RACE N2 — Cross-process bridge via NATS pub/sub.

Webhook + daemon are separate processes. Daemon /health was blind to webhook
generation status. This module is the bridge:

- ``WebhookHealthPublisher`` is used inside the webhook generator. After every
  successful (or partial) generation, the webhook calls
  :meth:`WebhookHealthPublisher.publish` with timing + per-section status. The
  call is fire-and-forget — webhook code never blocks waiting on NATS, never
  raises into the hot path. Failures bump ``webhook_publish_errors`` so the
  daemon can surface them later (ZSF — no silent failures).

- ``WebhookHealthAggregator`` is owned by the daemon. It subscribes to
  ``event.webhook.completed.<node>`` (this node only — not cross-node), keeps
  the last ``WINDOW`` events in a deque, and computes aggregate statistics:
  last age, total_ms p50/p95, per-section avg_ms + fail_rate, recent missing
  sections, cache hit rate. The :meth:`WebhookHealthAggregator.snapshot` method
  returns the dict the daemon embeds at ``/health.webhook``.

Subject convention::

    event.webhook.completed.<node_id>

Per-node so subscribers can scope to the local node only. Daemon mac3 only
cares about its own webhook process — cross-node webhook events from other
peers are orthogonal.

Payload shape (minimum required keys)::

    {
        "node": "mac3",
        "ts": 1714000000.123,
        "total_ms": 318,
        "sections": {
            "section_0": {"latency_ms": 56, "ok": true},
            "section_1": {"latency_ms": 89, "ok": true},
            ...
        },
        "missing": ["section_4"],          # optional, sections that failed
        "cache_hit": true                  # optional (M3 cache integration)
    }

Both classes are independently testable and have no NATS dependency at
construction time. Async transport is injected by the caller, so the daemon
plugs in its existing ``self.nc`` connection while tests can drive everything
with plain dicts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import threading
import time
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ── Subject helpers ──────────────────────────────────────────────────────

SUBJECT_PREFIX = "event.webhook.completed"
BUDGET_SUBJECT_PREFIX = "event.webhook.budget_exceeded"


def subject_for(node_id: str) -> str:
    """Return NATS subject for publishing/subscribing to this node's events."""
    if not node_id or not isinstance(node_id, str):
        raise ValueError(f"node_id must be a non-empty string, got {node_id!r}")
    return f"{SUBJECT_PREFIX}.{node_id}"


def subject_pattern_for(node_id: str) -> str:
    """Same as :func:`subject_for` — explicit single-node pattern (no wildcard)."""
    return subject_for(node_id)


def budget_subject_for(node_id: str) -> str:
    """Return NATS subject for publishing/subscribing to budget-exceeded alerts.

    RACE S5 — Webhook composite latency budget alerts. Per-node so the daemon
    only counts its own webhook regressions, not cross-node noise.
    """
    if not node_id or not isinstance(node_id, str):
        raise ValueError(f"node_id must be a non-empty string, got {node_id!r}")
    return f"{BUDGET_SUBJECT_PREFIX}.{node_id}"


# ── Publisher (used by webhook generator) ────────────────────────────────


class WebhookHealthPublisher:
    """Fire-and-forget publisher for webhook completion events.

    Webhook code calls :meth:`publish` after every generation attempt. The call
    NEVER blocks the webhook hot path:

    - If the supplied async publish callable is None or fails, the event is
      dropped and ``webhook_publish_errors`` is incremented.
    - If called from sync code (the typical webhook handler), the publish runs
      via ``asyncio.run_coroutine_threadsafe`` on the supplied loop, or is
      scheduled on a one-shot asyncio loop in a background thread.

    This is deliberately thin — the goal is observability, not durability.
    Lost events are acceptable; corrupt events are not.

    Attributes
    ----------
    publish_errors : int
        Monotonic counter of publish failures.
    publish_attempts : int
        Monotonic counter of publish attempts (success + failure).
    """

    def __init__(
        self,
        node_id: str,
        nats_publish: Optional[Callable[[str, bytes], Awaitable[None]]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if not node_id:
            raise ValueError("node_id required")
        self.node_id = node_id
        self._nats_publish = nats_publish
        self._loop = loop
        self.publish_errors = 0
        self.publish_attempts = 0
        self._lock = threading.Lock()

    def set_transport(
        self,
        nats_publish: Callable[[str, bytes], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Wire up async transport after construction (daemon-friendly)."""
        self._nats_publish = nats_publish
        self._loop = loop

    def build_payload(
        self,
        total_ms: int,
        sections: Dict[str, Dict[str, Any]],
        missing: Optional[Iterable[str]] = None,
        cache_hit: Optional[bool] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Construct a payload dict matching the contract.

        Validates section entries — each entry must have ``latency_ms`` (int)
        and ``ok`` (bool). Invalid entries are dropped (counted as a publish
        error so it's still observable).
        """
        clean_sections: Dict[str, Dict[str, Any]] = {}
        for name, info in (sections or {}).items():
            if not isinstance(info, dict):
                continue
            latency = info.get("latency_ms")
            ok = info.get("ok")
            if not isinstance(latency, (int, float)) or not isinstance(ok, bool):
                continue
            entry: Dict[str, Any] = {
                "latency_ms": int(latency),
                "ok": bool(ok),
            }
            # Carry through optional fields the webhook may add.
            for k in ("error", "cache_hit"):
                if k in info:
                    entry[k] = info[k]
            clean_sections[str(name)] = entry

        payload: Dict[str, Any] = {
            "node": self.node_id,
            "ts": time.time(),
            "total_ms": int(total_ms),
            "sections": clean_sections,
            "missing": list(missing or []),
        }
        if cache_hit is not None:
            payload["cache_hit"] = bool(cache_hit)
        if extra:
            for k, v in extra.items():
                if k not in payload:
                    payload[k] = v
        return payload

    def publish(
        self,
        total_ms: int,
        sections: Dict[str, Dict[str, Any]],
        missing: Optional[Iterable[str]] = None,
        cache_hit: Optional[bool] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Fire-and-forget publish. Returns True if scheduled, False on error.

        Webhook code calls this from its sync hot path. Never raises.
        """
        with self._lock:
            self.publish_attempts += 1
        try:
            payload = self.build_payload(
                total_ms=total_ms,
                sections=sections,
                missing=missing,
                cache_hit=cache_hit,
                extra=extra,
            )
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            return self._dispatch(subject_for(self.node_id), data)
        except Exception as exc:  # noqa: BLE001 — fire-and-forget, must not raise
            self._record_error("build/serialize", exc)
            return False

    async def publish_async(
        self,
        total_ms: int,
        sections: Dict[str, Dict[str, Any]],
        missing: Optional[Iterable[str]] = None,
        cache_hit: Optional[bool] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Async variant — usable directly from inside an event loop.

        Used by tests and by code already running on an asyncio loop. Never
        raises; logs and counts errors.
        """
        with self._lock:
            self.publish_attempts += 1
        if self._nats_publish is None:
            self._record_error("no-transport", None)
            return False
        try:
            payload = self.build_payload(
                total_ms=total_ms,
                sections=sections,
                missing=missing,
                cache_hit=cache_hit,
                extra=extra,
            )
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            await self._nats_publish(subject_for(self.node_id), data)
            return True
        except Exception as exc:  # noqa: BLE001 — fire-and-forget, never raise
            self._record_error("publish-async", exc)
            return False

    # ── internals ────────────────────────────────────────────────────────

    def _dispatch(self, subject: str, data: bytes) -> bool:
        """Schedule the async publish onto the supplied event loop.

        Three paths:

        1. ``loop`` supplied + transport supplied → schedule on it via
           ``run_coroutine_threadsafe`` (safe from any thread).
        2. No loop but already inside a running loop → use ``ensure_future``.
        3. No transport → record error.
        """
        if self._nats_publish is None:
            self._record_error("no-transport", None)
            return False

        coro = self._nats_publish(subject, data)
        try:
            if self._loop is not None and not self._loop.is_closed():
                asyncio.run_coroutine_threadsafe(coro, self._loop)
                return True
            # Try to attach to a currently-running loop.
            try:
                running = asyncio.get_running_loop()
                running.create_task(coro)
                return True
            except RuntimeError:
                # No running loop in this thread — drop, count.
                # Close the never-awaited coroutine to silence warnings.
                coro.close()
                self._record_error("no-loop", None)
                return False
        except Exception as exc:  # noqa: BLE001 — fire-and-forget
            try:
                coro.close()
            except Exception:  # noqa: BLE001
                pass
            self._record_error("dispatch", exc)
            return False

    def _record_error(self, where: str, exc: Optional[BaseException]) -> None:
        with self._lock:
            self.publish_errors += 1
        # ZSF: every failure observable in logs. Don't spam — one line, no trace.
        if exc is not None:
            logger.warning(
                "webhook health publish failed (%s): %s: %s",
                where,
                type(exc).__name__,
                exc,
            )
        else:
            logger.warning("webhook health publish failed (%s)", where)

    def publish_budget_exceeded(
        self,
        total_ms: int,
        budget_ms: int,
        sections: Optional[Dict[str, Dict[str, Any]]] = None,
        missing: Optional[Iterable[str]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """RACE S5 — Fire-and-forget alert when total latency exceeded budget.

        Publishes to ``event.webhook.budget_exceeded.<node_id>``. The daemon
        subscribes and surfaces a rolling 1h count under
        ``/health.webhook.budget_exceeded_recent``.

        Counted in ``publish_attempts`` / ``publish_errors`` like the regular
        publish path so failure modes stay observable.
        """
        with self._lock:
            self.publish_attempts += 1
        try:
            payload: Dict[str, Any] = {
                "node": self.node_id,
                "ts": time.time(),
                "total_ms": int(total_ms),
                "budget_ms": int(budget_ms),
                "over_by_ms": int(total_ms) - int(budget_ms),
                "sections": dict(sections or {}),
                "missing": list(missing or []),
            }
            if extra:
                for k, v in extra.items():
                    if k not in payload:
                        payload[k] = v
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            return self._dispatch(budget_subject_for(self.node_id), data)
        except Exception as exc:  # noqa: BLE001 — fire-and-forget, must not raise
            self._record_error("budget-build/serialize", exc)
            return False

    def stats(self) -> Dict[str, int]:
        """Return monotonic counters for daemon /health stats merge."""
        with self._lock:
            return {
                "webhook_publish_attempts": self.publish_attempts,
                "webhook_publish_errors": self.publish_errors,
            }


# ── Aggregator (used by daemon) ──────────────────────────────────────────


class WebhookHealthAggregator:
    """Collects the last ``WINDOW`` webhook completion events and summarizes them.

    Subscription wiring happens in the daemon — call :meth:`record` from the
    NATS message handler. Tests can call :meth:`record` directly.

    The aggregator is intentionally simple — it does NOT decode bytes. Bytes
    are decoded once in :meth:`record_raw`, which delegates to :meth:`record`.

    All state is in-memory; restart loses history (acceptable — webhook fires
    every few seconds anyway). Thread-safe via a single lock.
    """

    WINDOW = 10  # rolling window size

    def __init__(self, node_id: str, window: int = WINDOW) -> None:
        self.node_id = node_id
        self._window = max(1, int(window))
        self._events: Deque[Dict[str, Any]] = deque(maxlen=self._window)
        self._lock = threading.Lock()
        self._receive_errors = 0
        self._received = 0

    # ── ingest ───────────────────────────────────────────────────────────

    def record_raw(self, data: bytes) -> bool:
        """Decode + record a NATS message body. Never raises."""
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — bad JSON observable, not fatal
            self._receive_errors += 1
            logger.warning(
                "webhook health receive: bad JSON (%s): %s", type(exc).__name__, exc
            )
            return False
        if not isinstance(payload, dict):
            self._receive_errors += 1
            logger.warning("webhook health receive: payload not dict")
            return False
        return self.record(payload)

    def record(self, payload: Dict[str, Any]) -> bool:
        """Record a structured event. Returns True if accepted."""
        # Filter to this node — we only aggregate local webhook health.
        node = payload.get("node")
        if node and node != self.node_id:
            return False
        # Required: total_ms (int|float), sections (dict).
        total_ms = payload.get("total_ms")
        sections = payload.get("sections")
        if not isinstance(total_ms, (int, float)) or not isinstance(sections, dict):
            self._receive_errors += 1
            logger.warning("webhook health receive: malformed payload (keys=%s)", list(payload.keys()))
            return False
        ts = payload.get("ts")
        if not isinstance(ts, (int, float)):
            ts = time.time()
        missing = payload.get("missing") or []
        if not isinstance(missing, list):
            missing = []
        cache_hit = payload.get("cache_hit")
        normalized = {
            "ts": float(ts),
            "total_ms": float(total_ms),
            "sections": {
                str(k): {
                    "latency_ms": float(v.get("latency_ms", 0))
                    if isinstance(v, dict) else 0.0,
                    "ok": bool(v.get("ok", False))
                    if isinstance(v, dict) else False,
                }
                for k, v in sections.items()
                if isinstance(v, dict)
            },
            "missing": [str(m) for m in missing],
            "cache_hit": cache_hit if isinstance(cache_hit, bool) else None,
        }
        with self._lock:
            self._events.append(normalized)
            self._received += 1
        return True

    # ── snapshot ─────────────────────────────────────────────────────────

    def snapshot(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Return the dict to embed at /health.webhook.

        Always returns a dict with stable shape, even when no events recorded.
        """
        if now is None:
            now = time.time()
        with self._lock:
            events: List[Dict[str, Any]] = list(self._events)
            received = self._received
            errors = self._receive_errors

        if not events:
            return {
                "active": True,
                "events_recorded": 0,
                "receive_errors": errors,
                "last_webhook_age_s": None,
                "last_total_ms": None,
                "p50_total_ms": None,
                "p95_total_ms": None,
                "missing_sections_recent": [],
                "section_health": {},
                "cache_hit_rate": None,
                "window_size": self._window,
            }

        latest = events[-1]
        totals = [e["total_ms"] for e in events]

        # Section health: avg latency + fail rate across the window.
        section_stats: Dict[str, Dict[str, float]] = {}
        section_counts: Dict[str, Dict[str, float]] = {}
        for e in events:
            for name, info in e["sections"].items():
                bucket = section_counts.setdefault(
                    name, {"latency_sum": 0.0, "n": 0, "fails": 0}
                )
                bucket["latency_sum"] += info["latency_ms"]
                bucket["n"] += 1
                if not info["ok"]:
                    bucket["fails"] += 1
        for name, bucket in section_counts.items():
            n = bucket["n"]
            section_stats[name] = {
                "avg_ms": round(bucket["latency_sum"] / n, 2) if n else 0.0,
                "fail_rate": round(bucket["fails"] / n, 3) if n else 0.0,
                "samples": int(n),
            }

        # Missing sections — flatten + tally + sort by frequency desc.
        missing_tally: Dict[str, int] = {}
        for e in events:
            for m in e["missing"]:
                missing_tally[m] = missing_tally.get(m, 0) + 1
        missing_sorted = [
            {"section": k, "count": v}
            for k, v in sorted(missing_tally.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

        # Cache hit rate — from events that supplied cache_hit.
        cache_events = [e for e in events if e["cache_hit"] is not None]
        cache_rate: Optional[float]
        if cache_events:
            hits = sum(1 for e in cache_events if e["cache_hit"])
            cache_rate = round(hits / len(cache_events), 3)
        else:
            cache_rate = None

        return {
            "active": True,
            "events_recorded": received,
            "receive_errors": errors,
            "last_webhook_age_s": max(0, int(now - latest["ts"])),
            "last_total_ms": int(latest["total_ms"]),
            "p50_total_ms": int(_percentile(totals, 50)),
            "p95_total_ms": int(_percentile(totals, 95)),
            "missing_sections_recent": missing_sorted,
            "section_health": section_stats,
            "cache_hit_rate": cache_rate,
            "window_size": self._window,
        }

    # ── lifecycle helpers ────────────────────────────────────────────────

    async def subscribe(self, nc: Any) -> Any:
        """Subscribe on the supplied NATS connection. Returns the subscription.

        Daemon calls this once at startup with ``self.nc``. The aggregator only
        subscribes to its own node's subject so it doesn't drown in cross-node
        events.
        """
        async def _cb(msg: Any) -> None:
            try:
                self.record_raw(msg.data)
            except Exception as exc:  # noqa: BLE001 — receive must not crash callback
                logger.warning(
                    "webhook health subscribe callback error: %s: %s",
                    type(exc).__name__, exc,
                )
                self._receive_errors += 1

        return await nc.subscribe(subject_for(self.node_id), cb=_cb)

    def stats(self) -> Dict[str, int]:
        """Counters for daemon-level visibility."""
        with self._lock:
            return {
                "webhook_events_received": self._received,
                "webhook_receive_errors": self._receive_errors,
            }


# ── Budget aggregator (RACE S5) ──────────────────────────────────────────


class WebhookBudgetAggregator:
    """Tracks ``event.webhook.budget_exceeded.<node>`` events for /health.

    RACE S5 — Webhook composite latency budget alerts. Webhook publishes one
    event per generation that exceeds ``WEBHOOK_TOTAL_BUDGET_MS``. Daemon
    subscribes and surfaces:

    - ``budget_exceeded_count`` — monotonic since daemon start.
    - ``budget_exceeded_recent`` — count in the last hour (rolling).
    - ``last_event`` — most recent breach (ts/total_ms/budget_ms/over_by_ms).

    All state in-memory; restart loses history (acceptable — webhook fires
    every few seconds anyway). Thread-safe via single lock.
    """

    RECENT_WINDOW_S = 3600  # 1 hour

    def __init__(self, node_id: str, recent_window_s: int = RECENT_WINDOW_S) -> None:
        self.node_id = node_id
        self._recent_window_s = max(1, int(recent_window_s))
        # We only need timestamps + last event metadata to compute counts.
        self._timestamps: Deque[float] = deque()
        self._last_event: Optional[Dict[str, Any]] = None
        self._total_count = 0
        self._receive_errors = 0
        self._lock = threading.Lock()

    # ── ingest ───────────────────────────────────────────────────────────

    def record_raw(self, data: bytes) -> bool:
        """Decode + record a NATS message body. Never raises."""
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._receive_errors += 1
            logger.warning(
                "webhook budget receive: bad JSON (%s): %s", type(exc).__name__, exc
            )
            return False
        if not isinstance(payload, dict):
            with self._lock:
                self._receive_errors += 1
            logger.warning("webhook budget receive: payload not dict")
            return False
        return self.record(payload)

    def record(self, payload: Dict[str, Any]) -> bool:
        """Record a structured budget-exceeded event."""
        node = payload.get("node")
        if node and node != self.node_id:
            return False
        total_ms = payload.get("total_ms")
        budget_ms = payload.get("budget_ms")
        if not isinstance(total_ms, (int, float)) or not isinstance(budget_ms, (int, float)):
            with self._lock:
                self._receive_errors += 1
            logger.warning(
                "webhook budget receive: malformed payload (keys=%s)", list(payload.keys())
            )
            return False
        ts = payload.get("ts")
        if not isinstance(ts, (int, float)):
            ts = time.time()
        normalized = {
            "ts": float(ts),
            "total_ms": int(total_ms),
            "budget_ms": int(budget_ms),
            "over_by_ms": int(payload.get("over_by_ms", int(total_ms) - int(budget_ms))),
        }
        with self._lock:
            self._timestamps.append(float(ts))
            self._last_event = normalized
            self._total_count += 1
            self._prune_locked(now=time.time())
        return True

    def _prune_locked(self, *, now: float) -> None:
        cutoff = now - self._recent_window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    # ── snapshot ─────────────────────────────────────────────────────────

    def recent_count(self, *, now: Optional[float] = None) -> int:
        """Return number of breaches inside the rolling window."""
        if now is None:
            now = time.time()
        with self._lock:
            self._prune_locked(now=now)
            return len(self._timestamps)

    def snapshot(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Return dict for daemon /health.webhook.budget_exceeded_recent."""
        if now is None:
            now = time.time()
        with self._lock:
            self._prune_locked(now=now)
            recent = len(self._timestamps)
            total = self._total_count
            errors = self._receive_errors
            last = dict(self._last_event) if self._last_event else None
        return {
            "active": True,
            "window_s": self._recent_window_s,
            "recent_count": recent,
            "total_count": total,
            "receive_errors": errors,
            "last_event": last,
        }

    # ── lifecycle helpers ────────────────────────────────────────────────

    async def subscribe(self, nc: Any) -> Any:
        """Subscribe to budget alerts on the supplied NATS connection."""
        async def _cb(msg: Any) -> None:
            try:
                self.record_raw(msg.data)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "webhook budget subscribe callback error: %s: %s",
                    type(exc).__name__, exc,
                )
                with self._lock:
                    self._receive_errors += 1

        return await nc.subscribe(budget_subject_for(self.node_id), cb=_cb)

    def stats(self) -> Dict[str, int]:
        """Counters for daemon-level visibility (merged into _stats)."""
        with self._lock:
            return {
                "webhook_budget_exceeded_count": self._total_count,
                "webhook_budget_receive_errors": self._receive_errors,
            }


# ── Helpers ──────────────────────────────────────────────────────────────


def _percentile(values: List[float], pct: float) -> float:
    """Compute pct percentile (0-100) using nearest-rank — small-sample friendly.

    For the rolling window of N=10 we don't need scipy. Nearest-rank avoids
    interpolation surprises on tiny samples and matches what people expect
    when looking at "p95 over 10 events".
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    # Nearest-rank: rank = ceil(pct/100 * N)
    import math
    rank = max(1, math.ceil((pct / 100.0) * len(ordered)))
    rank = min(rank, len(ordered))
    return ordered[rank - 1]


__all__ = [
    "SUBJECT_PREFIX",
    "BUDGET_SUBJECT_PREFIX",
    "subject_for",
    "subject_pattern_for",
    "budget_subject_for",
    "WebhookHealthPublisher",
    "WebhookHealthAggregator",
    "WebhookBudgetAggregator",
]
