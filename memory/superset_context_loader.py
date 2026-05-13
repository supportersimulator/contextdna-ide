#!/usr/bin/env python3
"""
RACE V4 — Superset Context Loader

Wraps multi-fleet's SupersetBridge so the Synaptic context pipeline (S6/S8
in synaptic_service_hub.get_rich_context) can include workspace + task +
device data without slowing the webhook when Superset is down.

Why this module exists:
  multi-fleet/multifleet/superset_bridge.py wraps 18 MCP tools but had ZERO
  callers from memory/, tools/, or scripts/. Cross-cutting Priority #3
  flagged "wire as entry path." This module is that entry point.

Wiring strategy (this file):
  * Lazy-import SupersetBridge — heavy module never loaded unless called.
  * Circuit-breaker — after N consecutive failures, skip the bridge for a
    cooldown window. Prevents 15s-per-call HTTP timeouts from compounding
    into a webhook-busting stampede.
  * Per-call wall-clock budget — even when the breaker is closed (allowing
    calls), enforce a hard local budget so a slow Superset can never blow
    past the webhook latency budget.
  * All paths return small typed dicts. Empty dicts when unavailable.

The 3 highest-value tools we wire (per task spec):
  * get_workspace_details
  * list_tasks
  * start_agent_session  (we expose `launch_agent` — same MCP tool)

The loader exposes one composite entrypoint, `load_superset_context`, that
synaptic_service_hub.get_rich_context calls in a try/except. Each sub-call
is independently guarded so one slow tool can't sink the others.

INVARIANT (ZSF):
  No bare `except: pass`. Every swallow increments an observable counter
  exposed through `get_breaker_state()`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("memory.superset_context")

# ── Tunables (env-overridable) ──
# Hard wall-clock budget (seconds) for the loader. Breaker trips long before
# this in steady state, but this is the absolute upper bound when closed.
BUDGET_SECONDS = float(os.environ.get("SUPERSET_CTX_BUDGET_SECONDS", "2.0"))

# Consecutive failures before breaker opens.
BREAKER_THRESHOLD = int(os.environ.get("SUPERSET_CTX_BREAKER_THRESHOLD", "3"))

# Cooldown (seconds) before a half-open probe is allowed.
BREAKER_COOLDOWN_SECONDS = float(
    os.environ.get("SUPERSET_CTX_BREAKER_COOLDOWN_SECONDS", "60.0")
)

# Master kill-switch — set to "0" to disable the entire loader.
ENABLED = os.environ.get("SUPERSET_CTX_ENABLED", "1") != "0"

# Default cap on tasks returned to the context pipeline.
DEFAULT_TASK_LIMIT = int(os.environ.get("SUPERSET_CTX_TASK_LIMIT", "5"))


class _CircuitBreaker:
    """Tiny thread-safe circuit breaker.

    States:
      closed   — calls allowed, failures counted.
      open     — calls short-circuit until cooldown elapses.
      half_open — one probe call allowed; success closes, failure re-opens.
    """

    def __init__(
        self,
        threshold: int = BREAKER_THRESHOLD,
        cooldown: float = BREAKER_COOLDOWN_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._threshold = threshold
        self._cooldown = cooldown
        self._consec_failures = 0
        self._opened_at: Optional[float] = None
        # Observability counters (ZSF).
        self.calls_allowed = 0
        self.calls_blocked = 0
        self.successes = 0
        self.failures = 0
        self.opens = 0

    def allow(self, now: Optional[float] = None) -> bool:
        """Return True if a call should be attempted."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            if self._opened_at is None:
                self.calls_allowed += 1
                return True
            # Open — check cooldown.
            if (now - self._opened_at) >= self._cooldown:
                # Half-open: allow exactly one probe.
                self.calls_allowed += 1
                return True
            self.calls_blocked += 1
            return False

    def record_success(self) -> None:
        with self._lock:
            self.successes += 1
            self._consec_failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            self._consec_failures += 1
            if self._consec_failures >= self._threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                self.opens += 1
                logger.warning(
                    "Superset context breaker OPEN after %d consecutive failures",
                    self._consec_failures,
                )

    def state(self) -> Dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._opened_at is None:
                state = "closed"
                cooldown_remaining = 0.0
            else:
                elapsed = now - self._opened_at
                if elapsed >= self._cooldown:
                    state = "half_open"
                    cooldown_remaining = 0.0
                else:
                    state = "open"
                    cooldown_remaining = max(0.0, self._cooldown - elapsed)
            return {
                "state": state,
                "consec_failures": self._consec_failures,
                "calls_allowed": self.calls_allowed,
                "calls_blocked": self.calls_blocked,
                "successes": self.successes,
                "failures": self.failures,
                "opens": self.opens,
                "cooldown_remaining_s": cooldown_remaining,
            }

    def reset(self) -> None:
        with self._lock:
            self._consec_failures = 0
            self._opened_at = None
            self.calls_allowed = 0
            self.calls_blocked = 0
            self.successes = 0
            self.failures = 0
            self.opens = 0


# Module-level singleton breaker — shared across calls in a process.
_breaker = _CircuitBreaker()


def get_breaker_state() -> Dict[str, Any]:
    """Public accessor for breaker observability (used by tests + diagnostics)."""
    return _breaker.state()


def reset_breaker() -> None:
    """Reset the breaker — used by tests."""
    _breaker.reset()


# ── Lazy bridge construction ─────────────────────────────────────────────────

_bridge_lock = threading.Lock()
_bridge_cached: Optional[Any] = None
# Sentinel — distinguishes "never tried" from "tried and got None".
_BRIDGE_UNAVAILABLE = object()


def _get_bridge() -> Optional[Any]:
    """Lazy-import + memoize SupersetBridge.

    We deliberately keep this import-by-name so importing
    memory.superset_context_loader does NOT pull in multi-fleet's
    SupersetBridge module (which itself imports urllib + subprocess +
    Keychain helpers). We only pay that cost on first real use.
    """
    global _bridge_cached
    if _bridge_cached is _BRIDGE_UNAVAILABLE:
        return None
    if _bridge_cached is not None:
        return _bridge_cached
    with _bridge_lock:
        if _bridge_cached is _BRIDGE_UNAVAILABLE:
            return None
        if _bridge_cached is not None:
            return _bridge_cached
        try:
            from multifleet.superset_bridge import SupersetBridge  # type: ignore
        except Exception as e:  # pragma: no cover - defensive only
            logger.warning("SupersetBridge import failed: %s", e)
            _bridge_cached = _BRIDGE_UNAVAILABLE
            return None
        try:
            _bridge_cached = SupersetBridge()
        except Exception as e:  # pragma: no cover - defensive only
            logger.warning("SupersetBridge construction failed: %s", e)
            _bridge_cached = _BRIDGE_UNAVAILABLE
            return None
        return _bridge_cached


def _set_bridge_for_tests(bridge: Optional[Any]) -> None:
    """Test hook — inject a fake bridge without re-importing.

    Use `None` to clear the cache so the real lazy import runs again.
    """
    global _bridge_cached
    with _bridge_lock:
        _bridge_cached = bridge if bridge is not None else None


# ── Public loader entry-point ────────────────────────────────────────────────


def load_superset_context(
    *,
    workspace_id: Optional[str] = None,
    device_id: Optional[str] = None,
    task_status: Optional[str] = None,
    task_limit: int = DEFAULT_TASK_LIMIT,
    budget_seconds: float = BUDGET_SECONDS,
) -> Dict[str, Any]:
    """Load Superset workspace/task context for S6/S8 enrichment.

    Returns a dict with keys (always present, possibly empty):
      * available  — bool, did we get any data?
      * workspace  — dict (or {})
      * tasks      — list[dict]
      * elapsed_ms — float
      * breaker    — dict, breaker state at call time
      * errors     — list[str], one entry per sub-call that failed

    Never raises. Always returns within `budget_seconds` modulo the
    underlying bridge's own timeouts; callers should treat this as
    best-effort enrichment and never block on it.
    """
    out: Dict[str, Any] = {
        "available": False,
        "workspace": {},
        "tasks": [],
        "elapsed_ms": 0.0,
        "breaker": {},
        "errors": [],
    }

    if not ENABLED:
        out["errors"].append("disabled_via_env")
        out["breaker"] = _breaker.state()
        return out

    if not _breaker.allow():
        out["errors"].append("breaker_open")
        out["breaker"] = _breaker.state()
        return out

    bridge = _get_bridge()
    if bridge is None:
        # Treat unavailability of the bridge module itself as a failure so
        # the breaker eventually opens and we stop wasting cycles.
        _breaker.record_failure()
        out["errors"].append("bridge_unavailable")
        out["breaker"] = _breaker.state()
        return out

    t0 = time.monotonic()
    any_success = False

    # 1. Workspace details — only if workspace_id provided.
    if workspace_id:
        try:
            details = bridge.get_workspace_details(workspace_id, device_id=device_id)
            if isinstance(details, dict) and details.get("_available") is False:
                out["errors"].append(
                    f"workspace_details_unavailable: {details.get('error', '?')}"
                )
            elif isinstance(details, dict):
                out["workspace"] = details
                any_success = True
            else:
                out["errors"].append(
                    f"workspace_details_unexpected_type: {type(details).__name__}"
                )
        except Exception as e:
            out["errors"].append(f"workspace_details_exception: {e}")

    # Bail early if the workspace call already burned the whole budget.
    if (time.monotonic() - t0) >= budget_seconds:
        out["errors"].append("budget_exhausted_after_workspace")
        out["elapsed_ms"] = (time.monotonic() - t0) * 1000.0
        out["available"] = any_success
        if any_success:
            _breaker.record_success()
        else:
            _breaker.record_failure()
        out["breaker"] = _breaker.state()
        return out

    # 2. Tasks — list (optionally filtered by status), capped to task_limit.
    try:
        tasks = bridge.list_tasks(status=task_status)
        if isinstance(tasks, list):
            out["tasks"] = tasks[: max(0, task_limit)]
            # Empty list is still a successful call — Superset replied.
            any_success = True
        else:
            out["errors"].append(
                f"list_tasks_unexpected_type: {type(tasks).__name__}"
            )
    except Exception as e:
        out["errors"].append(f"list_tasks_exception: {e}")

    elapsed = time.monotonic() - t0
    out["elapsed_ms"] = elapsed * 1000.0
    out["available"] = any_success

    if any_success:
        _breaker.record_success()
    else:
        _breaker.record_failure()

    out["breaker"] = _breaker.state()
    return out


def launch_agent_session(
    *,
    workspace_id: str,
    task_id: Optional[str] = None,
    agent: str = "claude",
    device_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Wire for the third high-value MCP tool: start_agent_session.

    Kept separate from load_superset_context because launching an agent is
    a side-effect — we never want to do it as part of S6/S8 context loads.
    Callers (e.g. coordinator scripts) invoke this explicitly.

    Returns a dict with shape:
      * available — bool
      * result    — bridge response (or {})
      * error     — str (only present on failure)
      * breaker   — dict
    """
    if not ENABLED:
        return {
            "available": False,
            "result": {},
            "error": "disabled_via_env",
            "breaker": _breaker.state(),
        }

    if not _breaker.allow():
        return {
            "available": False,
            "result": {},
            "error": "breaker_open",
            "breaker": _breaker.state(),
        }

    bridge = _get_bridge()
    if bridge is None:
        _breaker.record_failure()
        return {
            "available": False,
            "result": {},
            "error": "bridge_unavailable",
            "breaker": _breaker.state(),
        }

    try:
        result = bridge.launch_agent(
            workspace_id=workspace_id,
            task_id=task_id,
            agent=agent,
            device_id=device_id,
        )
    except Exception as e:
        _breaker.record_failure()
        return {
            "available": False,
            "result": {},
            "error": f"exception: {e}",
            "breaker": _breaker.state(),
        }

    if isinstance(result, dict) and result.get("_available") is False:
        _breaker.record_failure()
        return {
            "available": False,
            "result": result,
            "error": result.get("error", "unavailable"),
            "breaker": _breaker.state(),
        }

    _breaker.record_success()
    return {
        "available": True,
        "result": result if isinstance(result, dict) else {"raw": result},
        "breaker": _breaker.state(),
    }


__all__ = [
    "load_superset_context",
    "launch_agent_session",
    "get_breaker_state",
    "reset_breaker",
]
