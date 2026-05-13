"""Shared-semaphore backpressure for anticipation_engine + failure_pattern_analyzer.

Why this exists
---------------
Synaptic risk audit (2026-05-13):
    :mod:`memory.anticipation_engine` (18 active import sites) and
    :mod:`memory.failure_pattern_analyzer` (6 active import sites) both
    queue work onto :func:`memory.llm_priority_queue.llm_generate` with
    **no shared throttling**. A failed deployment fires many failure
    analysis calls simultaneously — the priority queue's worker pool
    (default 1-2 workers) gets stampeded by ``BACKGROUND``-priority
    anticipation traffic, starving ``ATLAS`` diagnostics and even
    delaying ``AARON``-priority foreground prompts through head-of-line
    contention.

This module is the **shared backpressure manager** both predictive layer
modules route their LLM submissions through. It does NOT replace the
priority queue — it sits in front of it and limits concurrency at the
*caller* tier so the queue never sees more than ``slots_now`` outstanding
predictive submissions at once.

Design contract
---------------
* Module-level :class:`asyncio.Semaphore` with default **2 slots**.
* :func:`submit_under_backpressure` — async context manager that acquires
  a slot, calls ``llm_generate`` via the optional
  :mod:`contextdna_ide_oss.migrate4.backpressure.circuit_breaker` shim
  when available (falls back to the raw priority queue otherwise), and
  releases the slot.
* :func:`submit_under_backpressure_sync` — sync wrapper for the many
  legacy call sites that run outside an event loop.
* **Dynamic resize with hysteresis** (deliberately *not* Synaptic's 5-level
  ladder — see ``_maybe_resize`` docstring): if 5 consecutive submissions
  have median wait > ``RESIZE_DOWN_WAIT_S`` (default 30 s) we drop from
  2 slots to 1 to shed load. If 10 consecutive submissions wait less than
  ``RESIZE_UP_WAIT_S`` (default 1 s) we restore to 2. Binary up/down with
  cooldown.
* **ZSF counters** — every code path bumps a counter; nothing fails
  silently. ``get_counters()`` returns a snapshot for ``/health``.

Configuration (env)
-------------------
* ``CONTEXTDNA_BACKPRESSURE_INITIAL_SLOTS`` — initial slot count
  (default ``2``; valid range ``1..8``).
* ``CONTEXTDNA_BACKPRESSURE_MAX_SLOTS`` — ceiling for resize-up
  (default ``2``).
* ``CONTEXTDNA_BACKPRESSURE_MIN_SLOTS`` — floor for resize-down
  (default ``1``).
* ``BACKPRESSURE_ACQUIRE_TIMEOUT_S`` — semaphore acquire timeout
  in seconds (default ``60``).
* ``CONTEXTDNA_BACKPRESSURE_RESIZE_DOWN_WAIT_S`` — median wait above which
  we shed load (default ``30``).
* ``CONTEXTDNA_BACKPRESSURE_RESIZE_UP_WAIT_S`` — median wait below which
  we restore (default ``1``).
* ``CONTEXTDNA_BACKPRESSURE_RESIZE_COOLDOWN_S`` — minimum seconds between
  resize events (default ``60``).
* ``CONTEXTDNA_BACKPRESSURE_DISABLED`` — set to ``1`` to bypass entirely
  (still bumps a ``backpressure_bypass_total`` counter).

Counters
--------
* ``backpressure_acquired_total`` — slot acquired
* ``backpressure_released_total`` — slot released
* ``backpressure_acquire_timeout_total`` — slot acquire timed out
* ``backpressure_resized_down_total`` — slots dropped
* ``backpressure_resized_up_total`` — slots restored
* ``backpressure_llm_errors_total`` — wrapped ``llm_generate`` raised
* ``backpressure_llm_returned_none_total`` — wrapped call returned ``None``
* ``backpressure_circuit_breaker_used_total`` — circuit breaker shim engaged
* ``backpressure_bypass_total`` — disabled-flag bypass
* ``backpressure_slots_now`` — gauge of current slot count
"""
from __future__ import annotations

import asyncio
import logging
import os
import statistics
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

logger = logging.getLogger("contextdna.backpressure")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s contextdna.backpressure %(message)s"
        )
    )
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Configuration (env-overridable, sane defaults)
# ---------------------------------------------------------------------------


def _clamp_int(value: int, lo: int, hi: int) -> int:
    """Clamp helper; treats invalid values as the lower bound to fail safe."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return lo
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


_INITIAL_SLOTS = _clamp_int(
    os.environ.get("CONTEXTDNA_BACKPRESSURE_INITIAL_SLOTS", "2"), 1, 8
)
_MAX_SLOTS = _clamp_int(
    os.environ.get("CONTEXTDNA_BACKPRESSURE_MAX_SLOTS", "2"), _INITIAL_SLOTS, 8
)
_MIN_SLOTS = _clamp_int(
    os.environ.get("CONTEXTDNA_BACKPRESSURE_MIN_SLOTS", "1"), 1, _INITIAL_SLOTS
)
BACKPRESSURE_ACQUIRE_TIMEOUT_S = float(
    os.environ.get("BACKPRESSURE_ACQUIRE_TIMEOUT_S", "60.0")
)
_RESIZE_DOWN_WAIT_S = float(
    os.environ.get("CONTEXTDNA_BACKPRESSURE_RESIZE_DOWN_WAIT_S", "30.0")
)
_RESIZE_UP_WAIT_S = float(
    os.environ.get("CONTEXTDNA_BACKPRESSURE_RESIZE_UP_WAIT_S", "1.0")
)
_RESIZE_COOLDOWN_S = float(
    os.environ.get("CONTEXTDNA_BACKPRESSURE_RESIZE_COOLDOWN_S", "60.0")
)
_DISABLED = os.environ.get("CONTEXTDNA_BACKPRESSURE_DISABLED", "0") == "1"

# Rolling windows for hysteresis
_DOWN_WINDOW = 5   # consecutive samples above _RESIZE_DOWN_WAIT_S → shed
_UP_WINDOW = 10    # consecutive samples below _RESIZE_UP_WAIT_S → restore


# ---------------------------------------------------------------------------
# Counters (ZSF)
# ---------------------------------------------------------------------------

COUNTERS: dict[str, Any] = {
    "backpressure_acquired_total": 0,
    "backpressure_released_total": 0,
    "backpressure_acquire_timeout_total": 0,
    "backpressure_resized_down_total": 0,
    "backpressure_resized_up_total": 0,
    "backpressure_llm_errors_total": 0,
    "backpressure_llm_returned_none_total": 0,
    "backpressure_circuit_breaker_used_total": 0,
    "backpressure_bypass_total": 0,
    "backpressure_slots_now": _INITIAL_SLOTS,  # gauge
    "backpressure_wait_seconds_total": 0.0,    # cumulative
    "backpressure_max_wait_seconds": 0.0,      # high watermark
}

_counter_lock = threading.Lock()


def _bump(name: str, delta: int = 1) -> None:
    with _counter_lock:
        if name in COUNTERS and isinstance(COUNTERS[name], (int, float)):
            COUNTERS[name] += delta


def _set(name: str, value: Any) -> None:
    with _counter_lock:
        COUNTERS[name] = value


def _max(name: str, value: float) -> None:
    with _counter_lock:
        cur = COUNTERS.get(name, 0.0)
        if value > cur:
            COUNTERS[name] = value


def get_counters() -> dict[str, Any]:
    """Snapshot copy of counters for ``/health`` surfaces."""
    with _counter_lock:
        return dict(COUNTERS)


def reset_counters_for_tests() -> None:
    """Test-only — restores fresh counters and the initial slot count.

    Never call from production code paths; the only legitimate callers are
    pytest fixtures.
    """
    with _counter_lock:
        for k in COUNTERS:
            if isinstance(COUNTERS[k], float):
                COUNTERS[k] = 0.0
            else:
                COUNTERS[k] = 0
        COUNTERS["backpressure_slots_now"] = _INITIAL_SLOTS


# ---------------------------------------------------------------------------
# Module-level semaphore + resize state (singleton)
# ---------------------------------------------------------------------------


class _SemaphoreState:
    """Singleton state for the shared semaphore.

    We don't create the :class:`asyncio.Semaphore` at module import — the
    semaphore is bound to whichever event loop first calls
    :func:`get_anticipation_semaphore`. Lazy construction lets the same
    process safely host multiple loops (e.g., pytest's per-test loop)
    without binding the singleton to a dead loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Per-loop bound semaphores. Most processes have exactly one event
        # loop, so this dict typically has one entry; in tests we may see
        # several. Keyed by ``id(loop)``.
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        self._loops: dict[int, asyncio.AbstractEventLoop] = {}
        self._current_slots = _INITIAL_SLOTS
        # Sliding window of recent wait samples (seconds)
        self._waits: deque[float] = deque(maxlen=max(_DOWN_WINDOW, _UP_WINDOW))
        self._last_resize_at = 0.0


_state = _SemaphoreState()


def get_anticipation_semaphore(
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> asyncio.Semaphore:
    """Return the shared semaphore bound to *loop* (or the running loop).

    Idempotent — the same loop always gets the same semaphore. If the
    loop has been closed since the previous call, a fresh semaphore is
    minted (the stale entry is evicted). Thread-safe.
    """
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — caller is sync. Use a private background
            # loop dedicated to backpressure so we always return *some*
            # semaphore. Callers should normally use
            # :func:`submit_under_backpressure_sync` instead.
            loop = _get_or_create_sync_loop()

    key = id(loop)
    with _state._lock:
        sem = _state._semaphores.get(key)
        if sem is not None and not loop.is_closed():
            return sem
        # Evict stale or closed loops
        if sem is not None and loop.is_closed():
            _state._semaphores.pop(key, None)
            _state._loops.pop(key, None)
        sem = asyncio.Semaphore(_state._current_slots)
        _state._semaphores[key] = sem
        _state._loops[key] = loop
        _set("backpressure_slots_now", _state._current_slots)
        return sem


# ---------------------------------------------------------------------------
# Sync loop for non-async callers
# ---------------------------------------------------------------------------

_sync_loop: Optional[asyncio.AbstractEventLoop] = None
_sync_loop_lock = threading.Lock()
_sync_loop_thread: Optional[threading.Thread] = None


def _get_or_create_sync_loop() -> asyncio.AbstractEventLoop:
    """Background event loop for sync callers.

    A long-lived daemon thread runs an event loop forever. Sync callers
    submit coroutines via :func:`asyncio.run_coroutine_threadsafe`. We
    do **not** use :func:`asyncio.run` per-call because that would create
    a fresh loop (and a fresh semaphore) each invocation, defeating the
    point of shared backpressure.
    """
    global _sync_loop, _sync_loop_thread
    with _sync_loop_lock:
        if _sync_loop is not None and not _sync_loop.is_closed():
            return _sync_loop

        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            except Exception:  # noqa: BLE001
                logger.exception("backpressure sync loop crashed")

        t = threading.Thread(target=_run, name="backpressure-sync-loop", daemon=True)
        t.start()
        _sync_loop = loop
        _sync_loop_thread = t
        return loop


# ---------------------------------------------------------------------------
# Resize logic (binary up/down with hysteresis + cooldown)
# ---------------------------------------------------------------------------


def _resize_to(new_slots: int) -> None:
    """Rebuild the per-loop semaphores to *new_slots*.

    asyncio.Semaphore has no public resize API; we replace the singleton.
    Any coroutines currently awaiting the old semaphore stay parked on it
    and complete normally — the resize only affects *new* acquirers. This
    is intentional: yanking an in-flight acquirer would violate ZSF.
    """
    with _state._lock:
        old = _state._current_slots
        if new_slots == old:
            return
        _state._current_slots = new_slots
        # Rebuild semaphores for all known loops
        rebuilt: dict[int, asyncio.Semaphore] = {}
        for key, loop in list(_state._loops.items()):
            if loop.is_closed():
                continue
            rebuilt[key] = asyncio.Semaphore(new_slots)
        _state._semaphores = rebuilt
        _set("backpressure_slots_now", new_slots)
    if new_slots < old:
        _bump("backpressure_resized_down_total")
        logger.info(
            "backpressure resized DOWN: %d -> %d slots (shed load)", old, new_slots
        )
    else:
        _bump("backpressure_resized_up_total")
        logger.info(
            "backpressure resized UP: %d -> %d slots (restore)", old, new_slots
        )


def _maybe_resize(wait_s: float) -> None:
    """Hysteresis-based resize.

    Synaptic asked for a "5-level dynamic adjustment with 10 ms latency".
    We deliberately push back: 5 levels of ladder oscillation across a
    2-slot semaphore is theatre — there are only two interesting states
    (saturated, healthy). Honesty over Synaptic-fidelity:

      * 5 consecutive samples with median wait > 30 s → drop to ``_MIN_SLOTS``
      * 10 consecutive samples with median wait < 1 s → restore to ``_MAX_SLOTS``
      * 60 s cooldown between resize events (prevents flap)

    All resize decisions log at ``INFO`` so operators see why slots moved.
    """
    with _state._lock:
        _state._waits.append(wait_s)
        # Throttle: never resize more often than _RESIZE_COOLDOWN_S
        if time.monotonic() - _state._last_resize_at < _RESIZE_COOLDOWN_S:
            return
        current = _state._current_slots
        waits = list(_state._waits)

    # Down: median of last DOWN_WINDOW samples above threshold
    if len(waits) >= _DOWN_WINDOW and current > _MIN_SLOTS:
        tail = waits[-_DOWN_WINDOW:]
        if statistics.median(tail) > _RESIZE_DOWN_WAIT_S:
            _resize_to(_MIN_SLOTS)
            with _state._lock:
                _state._last_resize_at = time.monotonic()
                _state._waits.clear()
            return

    # Up: median of last UP_WINDOW samples below threshold
    if len(waits) >= _UP_WINDOW and current < _MAX_SLOTS:
        tail = waits[-_UP_WINDOW:]
        if statistics.median(tail) < _RESIZE_UP_WAIT_S:
            _resize_to(_MAX_SLOTS)
            with _state._lock:
                _state._last_resize_at = time.monotonic()
                _state._waits.clear()
            return


# ---------------------------------------------------------------------------
# Circuit breaker shim (optional)
# ---------------------------------------------------------------------------


def _try_import_circuit_breaker() -> Optional[Callable[..., Any]]:
    """Return ``circuit_breaker.guarded_llm_generate`` if available.

    The companion ``circuit_breaker.py`` module is part of the same
    migrate4 milestone but may not be present at module import time. We
    probe lazily on every call so a later install of the shim takes
    effect without restart.
    """
    try:
        from contextdna_ide_oss.migrate4.backpressure import (  # type: ignore[import-not-found]
            circuit_breaker,
        )

        if hasattr(circuit_breaker, "guarded_llm_generate"):
            return circuit_breaker.guarded_llm_generate  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — probe gracefully
        pass
    return None


def _try_import_llm_generate() -> Optional[Callable[..., Any]]:
    """Direct import of :func:`memory.llm_priority_queue.llm_generate`.

    Falls back gracefully when the priority queue is unavailable (e.g.,
    in OSS-IDE installs that haven't wired the mothership yet).
    """
    try:
        from memory.llm_priority_queue import llm_generate  # type: ignore

        return llm_generate
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Public API — async context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def submit_under_backpressure(
    priority: Any,
    prompt_data: dict[str, Any],
    *,
    timeout_s: Optional[float] = None,
):
    """Acquire a slot, run ``llm_generate``, release. Yields the result.

    Use::

        async with submit_under_backpressure(Priority.BACKGROUND, {
            "system_prompt": "...",
            "user_prompt":   "...",
            "profile":       "classify",
            "caller":        "anticipation_engine",
        }) as result:
            ...  # result is the LLM string, or None on timeout/error

    Returns ``None`` (still yielded — the ``async with`` body always runs)
    on any of:
      * semaphore acquire timeout > :data:`BACKPRESSURE_ACQUIRE_TIMEOUT_S`
      * wrapped ``llm_generate`` raised
      * wrapped ``llm_generate`` returned ``None`` (queue timeout / preempt)
      * disabled via ``CONTEXTDNA_BACKPRESSURE_DISABLED=1``

    *priority* and *prompt_data* are passed through to ``llm_generate``;
    we do not introspect them. *timeout_s* overrides the semaphore-acquire
    timeout for this single call (the LLM call's own timeout is separate
    and lives inside ``prompt_data`` as ``timeout_s``).
    """
    # Disabled flag: bypass entirely but still record a counter
    if _DISABLED:
        _bump("backpressure_bypass_total")
        result = await _invoke_llm(priority, prompt_data)
        yield result
        return

    acquire_timeout = (
        float(timeout_s) if timeout_s is not None else BACKPRESSURE_ACQUIRE_TIMEOUT_S
    )
    sem = get_anticipation_semaphore()
    wait_start = time.monotonic()
    acquired = False
    try:
        try:
            await asyncio.wait_for(sem.acquire(), timeout=acquire_timeout)
            acquired = True
        except asyncio.TimeoutError:
            wait_s = time.monotonic() - wait_start
            _bump("backpressure_acquire_timeout_total")
            _max("backpressure_max_wait_seconds", wait_s)
            logger.warning(
                "backpressure acquire timed out after %.2fs (slots=%d)",
                wait_s, _state._current_slots,
            )
            yield None
            return

        wait_s = time.monotonic() - wait_start
        _bump("backpressure_acquired_total")
        with _counter_lock:
            COUNTERS["backpressure_wait_seconds_total"] += wait_s
        _max("backpressure_max_wait_seconds", wait_s)
        _maybe_resize(wait_s)

        result = await _invoke_llm(priority, prompt_data)
        yield result
    finally:
        if acquired:
            try:
                sem.release()
                _bump("backpressure_released_total")
            except Exception:  # noqa: BLE001 — release should never raise
                logger.exception("backpressure semaphore release failed")


async def _invoke_llm(priority: Any, prompt_data: dict[str, Any]) -> Optional[str]:
    """Route through circuit breaker if present, else raw ``llm_generate``.

    ``llm_generate`` is a blocking sync function; we run it in a thread
    so the event loop stays responsive while the LLM works.
    """
    guarded = _try_import_circuit_breaker()
    if guarded is not None:
        _bump("backpressure_circuit_breaker_used_total")
        target = guarded
    else:
        target = _try_import_llm_generate()
        if target is None:
            _bump("backpressure_llm_errors_total")
            logger.debug("llm_generate unavailable — backpressure returned None")
            return None

    call_kwargs = dict(prompt_data)
    call_kwargs["priority"] = priority
    try:
        result = await asyncio.to_thread(_call_blocking, target, call_kwargs)
    except Exception as exc:  # noqa: BLE001
        _bump("backpressure_llm_errors_total")
        logger.warning("backpressure wrapped llm_generate raised: %s", exc)
        return None

    if result is None:
        _bump("backpressure_llm_returned_none_total")
    return result


def _call_blocking(target: Callable[..., Any], kwargs: dict[str, Any]) -> Optional[str]:
    """Tiny shim so ``asyncio.to_thread`` has a non-lambda callable."""
    return target(**kwargs)


# ---------------------------------------------------------------------------
# Public API — sync wrapper
# ---------------------------------------------------------------------------


def submit_under_backpressure_sync(
    priority: Any,
    prompt_data: dict[str, Any],
    *,
    timeout_s: Optional[float] = None,
) -> Optional[str]:
    """Sync wrapper for callers outside an event loop.

    Submits to the dedicated backpressure event loop (started on demand)
    and blocks until the coroutine completes. Returns the same value the
    async API would yield.

    Threadsafe — many threads may call this concurrently; the shared
    semaphore (bound to the backpressure loop) serializes them just like
    async callers.
    """
    if _DISABLED:
        _bump("backpressure_bypass_total")
        target = _try_import_circuit_breaker() or _try_import_llm_generate()
        if target is None:
            _bump("backpressure_llm_errors_total")
            return None
        call_kwargs = dict(prompt_data)
        call_kwargs["priority"] = priority
        try:
            return target(**call_kwargs)
        except Exception as exc:  # noqa: BLE001
            _bump("backpressure_llm_errors_total")
            logger.warning("backpressure sync bypass llm_generate raised: %s", exc)
            return None

    loop = _get_or_create_sync_loop()

    async def _run() -> Optional[str]:
        async with submit_under_backpressure(
            priority, prompt_data, timeout_s=timeout_s
        ) as result:
            return result

    fut = asyncio.run_coroutine_threadsafe(_run(), loop)
    # Block until done. We give it acquire_timeout + LLM timeout headroom
    # so we never out-wait the inner timeouts.
    inner_llm_timeout = float(prompt_data.get("timeout_s") or 120.0)
    overall = (
        (float(timeout_s) if timeout_s is not None else BACKPRESSURE_ACQUIRE_TIMEOUT_S)
        + inner_llm_timeout
        + 5.0
    )
    try:
        return fut.result(timeout=overall)
    except Exception as exc:  # noqa: BLE001
        _bump("backpressure_llm_errors_total")
        logger.warning("backpressure sync wrapper raised: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


def current_slot_count() -> int:
    """Current semaphore slot count (gauge). Reads under lock."""
    with _state._lock:
        return _state._current_slots


def median_recent_wait_seconds() -> Optional[float]:
    """Median wait time over the current sample window, or ``None`` if empty."""
    with _state._lock:
        if not _state._waits:
            return None
        return float(statistics.median(_state._waits))


__all__ = [
    "BACKPRESSURE_ACQUIRE_TIMEOUT_S",
    "COUNTERS",
    "current_slot_count",
    "get_anticipation_semaphore",
    "get_counters",
    "median_recent_wait_seconds",
    "reset_counters_for_tests",
    "submit_under_backpressure",
    "submit_under_backpressure_sync",
]
