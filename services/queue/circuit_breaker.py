"""Circuit breaker wrapper around ``memory.llm_priority_queue.llm_generate``.

Synaptic 2026-05-13 flagged a deadlock risk in the priority queue: high
anticipation + failure-analysis load can stall the single MLX server (port
5044) with no breaker, no timeout fallback, and no probe path back to
healthy. Atlas reading: the queue itself is fair, but a flapping or hung
upstream (MLX dead, DeepSeek 5xx, OpenAI rate-limit) translates directly
into Aaron-blocking webhook latency because every caller waits the full
``timeout_s`` before failing.

This module wraps ``llm_generate`` in a classic three-state circuit
breaker (CLOSED / OPEN / HALF_OPEN) and provides two fallback strategies:

  Fallback A — cached: return a recent successful response keyed by
    (profile, prompt hash) within a configurable TTL (default 5 min).
  Fallback B — heuristic: return a terse synthesized response with a low
    confidence flag so the caller can decide whether to use it.

Synaptic's directive proposed 100 ms latency thresholds and 500 ms
recovery delays. Those numbers are tuned for a microservice fanout, not a
single-threaded local LLM where each token costs ~30-80 ms. Honest LLM
defaults:

  ``BREAKER_LATENCY_THRESHOLD_MS`` — 30000 ms (30 s). A healthy MLX
    classify call finishes in 500-2000 ms; a healthy deep call finishes
    in 10-25 s. 30 s is the conservative "something is wrong" knee.
  ``BREAKER_MAX_TIMEOUTS`` — 3. Three consecutive timeouts means the
    upstream is gone, not flaky.
  ``BREAKER_RECOVERY_S`` — 30 s. Long enough for an MLX restart loop or
    a transient DeepSeek 5xx storm to clear, short enough that Aaron's
    next ATLAS-priority call gets to probe.
  ``BREAKER_CACHE_TTL_S`` — 300 s (5 min). Matches webhook anticipation
    cycle cadence — Fallback A becomes meaningful when the same topic
    is asked twice within one work session.

The breaker does NOT modify ``llm_generate``. It is a pure proxy:
``cb = CircuitBreaker(); cb.call(system_prompt, user_prompt, ...)``.

ZSF (zero silent failures)
--------------------------
Every state transition and fallback path bumps a counter in
:data:`COUNTERS`. ``get_counters()`` exposes them for /health. The
breaker never silently passes ``except Exception``; every catch records
what failed and bumps a named counter before degrading.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("contextdna.llm_circuit_breaker")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s contextdna.llm_circuit_breaker %(message)s"
        )
    )
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Counters (ZSF) — mirrors anticipation_engine convention
# ---------------------------------------------------------------------------

COUNTERS: dict[str, Any] = {
    # State transitions
    "breaker_trips_total": 0,          # CLOSED  -> OPEN
    "breaker_recoveries_total": 0,     # HALF_OPEN -> CLOSED
    "breaker_reopens_total": 0,        # HALF_OPEN -> OPEN (probe failed)
    "breaker_half_open_attempts_total": 0,
    # Fallback usage
    "breaker_fallback_a_total": 0,     # cache hit while OPEN/HALF_OPEN
    "breaker_fallback_b_total": 0,     # heuristic served
    "breaker_fallback_unavailable_total": 0,  # OPEN, no cache, heuristic disabled
    # Call lifecycle
    "calls_total": 0,
    "calls_success_total": 0,
    "calls_timeout_total": 0,
    "calls_error_total": 0,
    "calls_short_circuited_total": 0,  # rejected because OPEN
    # Latency tripping
    "breaker_latency_trips_total": 0,
    "breaker_timeout_trips_total": 0,
    # Gauge — current state as int (CLOSED=0, HALF_OPEN=1, OPEN=2)
    "breaker_state_now": 0,
    # Cache lifecycle
    "cache_writes_total": 0,
    "cache_evictions_total": 0,
}

_counter_lock = threading.Lock()


def _bump(name: str, delta: int = 1) -> None:
    with _counter_lock:
        if name in COUNTERS and isinstance(COUNTERS[name], int):
            COUNTERS[name] += delta


def _set_gauge(name: str, value: int) -> None:
    with _counter_lock:
        if name in COUNTERS:
            COUNTERS[name] = value


def get_counters() -> dict[str, Any]:
    """Snapshot copy of counters for /health surfaces."""
    with _counter_lock:
        return dict(COUNTERS)


# ---------------------------------------------------------------------------
# Configuration (env-overridable)
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Defaults — see module docstring for rationale.
DEFAULT_MAX_TIMEOUTS = _env_int("BREAKER_MAX_TIMEOUTS", 3)
DEFAULT_LATENCY_THRESHOLD_MS = _env_int("BREAKER_LATENCY_THRESHOLD_MS", 30_000)
DEFAULT_RECOVERY_S = _env_float("BREAKER_RECOVERY_S", 30.0)
DEFAULT_CACHE_TTL_S = _env_float("BREAKER_CACHE_TTL_S", 300.0)
DEFAULT_CACHE_MAX_ENTRIES = _env_int("BREAKER_CACHE_MAX_ENTRIES", 256)
DEFAULT_LATENCY_WINDOW = _env_int("BREAKER_LATENCY_WINDOW", 20)
DEFAULT_HEURISTIC_ENABLED = _env_flag("BREAKER_HEURISTIC_ENABLED", True)
# Latency percentile to monitor (0-100). Synaptic asked for p99 but the
# tail is sparse; p95 is the production-realistic signal on a 20-call
# window. Configurable so we can lift to p99 when the window grows.
DEFAULT_LATENCY_PERCENTILE = _env_int("BREAKER_LATENCY_PERCENTILE", 95)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class BreakerState(enum.IntEnum):
    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    response: str
    written_at: float
    profile: str


# ---------------------------------------------------------------------------
# Result wrapper — callers see confidence + degraded flag
# ---------------------------------------------------------------------------


@dataclass
class BreakerResult:
    """Return value from :meth:`CircuitBreaker.call`.

    ``response`` is ``None`` only when the breaker is OPEN with no cache
    and ``heuristic_enabled=False`` — callers must check ``response is
    not None`` before using it.
    """

    response: Optional[str]
    source: str  # "live" | "cache" | "heuristic" | "unavailable"
    confidence: float  # 1.0=live, 0.6=cache, 0.3=heuristic, 0.0=unavailable
    degraded: bool
    latency_ms: float
    breaker_state: BreakerState

    @property
    def ok(self) -> bool:
        return self.response is not None


# ---------------------------------------------------------------------------
# Heuristic fallback — terse synthesized response keyed off profile
# ---------------------------------------------------------------------------


_HEURISTIC_BY_PROFILE: dict[str, str] = {
    "classify": "anticipation_unavailable",
    "extract": "{}",
    "voice": "anticipation_unavailable",
    "deep": "anticipation_unavailable",
    "reasoning": "anticipation_unavailable",
    "s2_professor": "anticipation_unavailable",
    "s8_synaptic": "anticipation_unavailable",
}


def _heuristic_response(profile: str) -> str:
    return _HEURISTIC_BY_PROFILE.get(profile, "anticipation_unavailable")


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Three-state breaker wrapping ``llm_generate``.

    The breaker is **safe to share across threads**. State transitions and
    cache writes are guarded by a single re-entrant lock. The lock is
    *not* held across the wrapped ``llm_generate`` call — only around the
    pre-flight check, post-call update, and cache mutation.

    Parameters mirror the env defaults; pass explicit values in tests.
    """

    def __init__(
        self,
        *,
        max_timeouts: int = DEFAULT_MAX_TIMEOUTS,
        latency_threshold_ms: int = DEFAULT_LATENCY_THRESHOLD_MS,
        recovery_s: float = DEFAULT_RECOVERY_S,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
        cache_max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        latency_window: int = DEFAULT_LATENCY_WINDOW,
        latency_percentile: int = DEFAULT_LATENCY_PERCENTILE,
        heuristic_enabled: bool = DEFAULT_HEURISTIC_ENABLED,
        time_fn: Callable[[], float] = time.monotonic,
        llm_generate_fn: Optional[Callable[..., Optional[str]]] = None,
        wall_clock_fn: Callable[[], float] = time.time,
    ) -> None:
        self.max_timeouts = max_timeouts
        self.latency_threshold_ms = latency_threshold_ms
        self.recovery_s = recovery_s
        self.cache_ttl_s = cache_ttl_s
        self.cache_max_entries = max(8, cache_max_entries)
        self.latency_window = max(5, latency_window)
        if not 50 <= latency_percentile <= 100:
            latency_percentile = 95
        self.latency_percentile = latency_percentile
        self.heuristic_enabled = heuristic_enabled
        self._time = time_fn
        self._wall = wall_clock_fn

        # Allow tests to inject a fake llm_generate. By default we lazily
        # import the real one so the module remains importable when the
        # mothership upstream is missing (matches anticipation_engine
        # graceful-degradation contract).
        self._llm_generate = llm_generate_fn or self._default_llm_generate

        self._lock = threading.RLock()
        self._state: BreakerState = BreakerState.CLOSED
        self._consecutive_timeouts: int = 0
        self._last_success_at: Optional[float] = None
        self._last_state_change_at: float = self._time()
        self._opened_at: Optional[float] = None
        self._latencies: deque[float] = deque(maxlen=self.latency_window)
        self._cache: dict[str, _CacheEntry] = {}
        # FIFO insertion order for cache eviction
        self._cache_order: deque[str] = deque()

        _set_gauge("breaker_state_now", int(self._state))

    # ----- public API ------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state

    @property
    def consecutive_timeouts(self) -> int:
        with self._lock:
            return self._consecutive_timeouts

    @property
    def last_success_at(self) -> Optional[float]:
        with self._lock:
            return self._last_success_at

    @property
    def last_state_change_at(self) -> float:
        with self._lock:
            return self._last_state_change_at

    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        priority: Any = None,
        profile: str = "classify",
        caller: str = "circuit_breaker",
        timeout_s: float = 30.0,
        **kwargs: Any,
    ) -> BreakerResult:
        """Run ``llm_generate`` through the breaker.

        Extra ``**kwargs`` are forwarded to the wrapped generator so future
        flags (``enable_thinking``, ``raise_on_preempt``) keep working.
        """
        _bump("calls_total")
        now = self._time()
        cache_key = self._cache_key(system_prompt, user_prompt, profile)

        # ------------------------------------------------------------------
        # Pre-flight: state check + maybe HALF_OPEN probe transition
        # ------------------------------------------------------------------
        allowed, current_state = self._pre_call(now)
        if not allowed:
            _bump("calls_short_circuited_total")
            return self._serve_fallback(cache_key, profile, current_state, latency_ms=0.0)

        # ------------------------------------------------------------------
        # Call the wrapped generator outside the lock
        # ------------------------------------------------------------------
        t0 = self._time()
        response: Optional[str] = None
        exc: Optional[BaseException] = None
        try:
            response = self._llm_generate(
                system_prompt,
                user_prompt,
                priority=priority,
                profile=profile,
                caller=caller,
                timeout_s=timeout_s,
                **kwargs,
            )
        except BaseException as e:  # noqa: BLE001 — ZSF: record before re-classify
            exc = e
            _bump("calls_error_total")
            logger.warning(
                "llm_generate raised %s for [%s]: %s",
                type(e).__name__,
                caller,
                e,
            )
        latency_ms = (self._time() - t0) * 1000.0

        # ------------------------------------------------------------------
        # Post-call: classify outcome and mutate breaker state
        # ------------------------------------------------------------------
        # ``llm_generate`` returns None on timeout AND on preemption. We
        # treat None as "no useful response" for breaker accounting. The
        # caller can still distinguish via the wrapped errors if needed.
        is_timeout = response is None and exc is None
        is_error = exc is not None
        is_success = response is not None and not is_error

        if is_success:
            assert response is not None
            self._record_success(latency_ms, cache_key, profile, response)
            _bump("calls_success_total")
            return BreakerResult(
                response=response,
                source="live",
                confidence=1.0,
                degraded=False,
                latency_ms=latency_ms,
                breaker_state=self.state,
            )

        if is_timeout:
            _bump("calls_timeout_total")
        # errors already counted

        tripped_state = self._record_failure(is_timeout=is_timeout, latency_ms=latency_ms)
        return self._serve_fallback(cache_key, profile, tripped_state, latency_ms=latency_ms)

    def reset(self) -> None:
        """Hard reset — primarily for tests and operator recovery."""
        with self._lock:
            self._state = BreakerState.CLOSED
            self._consecutive_timeouts = 0
            self._opened_at = None
            self._last_state_change_at = self._time()
            self._latencies.clear()
            _set_gauge("breaker_state_now", int(self._state))

    # ----- internal: state machine ----------------------------------------

    def _pre_call(self, now: float) -> tuple[bool, BreakerState]:
        """Return (allowed_to_call, observed_state).

        Drives the OPEN -> HALF_OPEN transition when recovery has elapsed.
        Only one caller becomes the HALF_OPEN probe; the rest see OPEN
        and short-circuit.
        """
        with self._lock:
            if self._state is BreakerState.CLOSED:
                return True, self._state
            if self._state is BreakerState.HALF_OPEN:
                # Another caller is already probing — short-circuit.
                return False, self._state
            # OPEN — has recovery elapsed?
            opened = self._opened_at or self._last_state_change_at
            if (now - opened) >= self.recovery_s:
                self._transition(BreakerState.HALF_OPEN)
                _bump("breaker_half_open_attempts_total")
                return True, self._state
            return False, self._state

    def _record_success(
        self, latency_ms: float, cache_key: str, profile: str, response: str
    ) -> None:
        with self._lock:
            self._consecutive_timeouts = 0
            self._last_success_at = self._wall()
            self._latencies.append(latency_ms)
            self._write_cache(cache_key, profile, response)

            # HALF_OPEN probe success -> CLOSED (recovery).
            if self._state is BreakerState.HALF_OPEN:
                _bump("breaker_recoveries_total")
                self._transition(BreakerState.CLOSED)
                return

            # CLOSED: even a successful call can trip the breaker if the
            # latency percentile blows past the threshold. This is the
            # case Synaptic flagged — the LLM is "working" but every
            # call takes 60 s and the queue silently piles up.
            if self._state is BreakerState.CLOSED:
                if (
                    len(self._latencies) >= self.latency_window
                    and self._latency_percentile_ms() > self.latency_threshold_ms
                ):
                    _bump("breaker_latency_trips_total")
                    _bump("breaker_trips_total")
                    self._open_now()

    def _record_failure(self, *, is_timeout: bool, latency_ms: float) -> BreakerState:
        """Update bookkeeping and decide whether to trip.

        Returns the (possibly updated) state for the caller to forward
        into the fallback path.
        """
        with self._lock:
            if is_timeout:
                self._consecutive_timeouts += 1
                # Timeouts still count as "took the full window" for
                # latency tracking — record the timeout_s in ms so the
                # tail percentile sees the pain.
                self._latencies.append(latency_ms)
            else:
                # Errors do not increment the timeout counter (a 500 is
                # not a hang) but still bump the latency tail.
                self._latencies.append(latency_ms)

            # If we were HALF_OPEN, any failure re-opens immediately.
            if self._state is BreakerState.HALF_OPEN:
                _bump("breaker_reopens_total")
                self._open_now()
                return self._state

            # CLOSED -> OPEN trip conditions
            tripped = False
            if self._consecutive_timeouts >= self.max_timeouts:
                _bump("breaker_timeout_trips_total")
                tripped = True
            elif self._latency_percentile_ms() > self.latency_threshold_ms:
                _bump("breaker_latency_trips_total")
                tripped = True

            if tripped:
                _bump("breaker_trips_total")
                self._open_now()
            return self._state

    def _open_now(self) -> None:
        # caller already holds self._lock
        self._opened_at = self._time()
        self._transition(BreakerState.OPEN)

    def _transition(self, new_state: BreakerState) -> None:
        # caller already holds self._lock
        if new_state is self._state:
            return
        old = self._state
        self._state = new_state
        self._last_state_change_at = self._time()
        _set_gauge("breaker_state_now", int(new_state))
        logger.info("breaker %s -> %s", old.name, new_state.name)

    def _latency_percentile_ms(self) -> float:
        # caller already holds self._lock
        if not self._latencies:
            return 0.0
        sorted_l = sorted(self._latencies)
        idx = int(len(sorted_l) * self.latency_percentile / 100)
        idx = max(0, min(idx, len(sorted_l) - 1))
        return sorted_l[idx]

    # ----- internal: cache ------------------------------------------------

    @staticmethod
    def _cache_key(system_prompt: str, user_prompt: str, profile: str) -> str:
        h = hashlib.sha256()
        h.update(profile.encode("utf-8"))
        h.update(b"\x1f")
        h.update(system_prompt.encode("utf-8"))
        h.update(b"\x1f")
        h.update(user_prompt.encode("utf-8"))
        return h.hexdigest()

    def _write_cache(self, key: str, profile: str, response: str) -> None:
        # caller already holds self._lock
        self._cache[key] = _CacheEntry(
            response=response, written_at=self._wall(), profile=profile
        )
        # Maintain FIFO order (without using OrderedDict semantics so we
        # don't reorder on overwrite — overwrites stay in their original
        # slot, which makes eviction deterministic).
        if key not in self._cache_order:
            self._cache_order.append(key)
        _bump("cache_writes_total")
        while len(self._cache_order) > self.cache_max_entries:
            evict = self._cache_order.popleft()
            self._cache.pop(evict, None)
            _bump("cache_evictions_total")

    def _read_cache(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            age = self._wall() - entry.written_at
            if age > self.cache_ttl_s:
                self._cache.pop(key, None)
                try:
                    self._cache_order.remove(key)
                except ValueError:
                    pass
                _bump("cache_evictions_total")
                return None
            return entry.response

    # ----- internal: fallback ---------------------------------------------

    def _serve_fallback(
        self,
        cache_key: str,
        profile: str,
        observed_state: BreakerState,
        *,
        latency_ms: float,
    ) -> BreakerResult:
        cached = self._read_cache(cache_key)
        if cached is not None:
            _bump("breaker_fallback_a_total")
            return BreakerResult(
                response=cached,
                source="cache",
                confidence=0.6,
                degraded=True,
                latency_ms=latency_ms,
                breaker_state=observed_state,
            )

        if self.heuristic_enabled:
            _bump("breaker_fallback_b_total")
            return BreakerResult(
                response=_heuristic_response(profile),
                source="heuristic",
                confidence=0.3,
                degraded=True,
                latency_ms=latency_ms,
                breaker_state=observed_state,
            )

        _bump("breaker_fallback_unavailable_total")
        return BreakerResult(
            response=None,
            source="unavailable",
            confidence=0.0,
            degraded=True,
            latency_ms=latency_ms,
            breaker_state=observed_state,
        )

    # ----- internal: upstream import (lazy) -------------------------------

    @staticmethod
    def _default_llm_generate(
        system_prompt: str,
        user_prompt: str,
        *,
        priority: Any = None,
        profile: str = "classify",
        caller: str = "circuit_breaker",
        timeout_s: float = 30.0,
        **kwargs: Any,
    ) -> Optional[str]:
        """Lazy proxy to the real ``memory.llm_priority_queue.llm_generate``.

        Kept as a staticmethod so the import only happens on first real
        call. Tests inject their own generator via ``llm_generate_fn``.
        """
        try:
            from memory.llm_priority_queue import (  # type: ignore[import-not-found]
                Priority,
                llm_generate,
            )
        except Exception as exc:  # noqa: BLE001 — degrade, never crash
            logger.warning("llm_priority_queue unavailable: %s", exc)
            return None

        # Map an int / None priority into the real enum without forcing
        # the caller to import Priority before the breaker.
        if priority is None:
            resolved_priority = Priority.BACKGROUND
        elif isinstance(priority, int):
            try:
                resolved_priority = Priority(priority)
            except ValueError:
                resolved_priority = Priority.BACKGROUND
        else:
            resolved_priority = priority

        return llm_generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            priority=resolved_priority,
            profile=profile,
            caller=caller,
            timeout_s=timeout_s,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Module-level singleton (lazy) for callers that want a shared breaker
# ---------------------------------------------------------------------------

_default_breaker: Optional[CircuitBreaker] = None
_default_lock = threading.Lock()


def get_default_breaker() -> CircuitBreaker:
    """Return the process-wide default breaker, creating it on first use."""
    global _default_breaker
    with _default_lock:
        if _default_breaker is None:
            _default_breaker = CircuitBreaker()
        return _default_breaker


__all__ = [
    "BreakerState",
    "BreakerResult",
    "CircuitBreaker",
    "COUNTERS",
    "get_counters",
    "get_default_breaker",
]
