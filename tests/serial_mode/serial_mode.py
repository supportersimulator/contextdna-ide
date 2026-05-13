"""SERIAL_MODE — test-only synchronous execution toggle.

Purpose
-------
Bootstrap-verify, CI smoke tests, and bug-report reproducers need a way to
sidestep async queues, semaphores, and background workers so that a run is
fully deterministic and step-debuggable.  Setting the environment variable
``SERIAL_MODE=1`` flips three switches at once:

1. ``@serial_safe`` decorated coroutines execute synchronously via
   ``asyncio.run`` instead of being awaited by the caller's loop.
2. ``@bypass_queue_in_serial`` decorated call sites that would normally
   hand work to the LLM priority queue instead receive a deterministic
   response from :func:`_serial_llm_response` (prompt-hash keyed).
3. Semaphores produced by ``anticipation_backpressure.py`` (Agent 4's
   work) are expected to become no-ops — see the *Backpressure note*
   section below.  This module does not import that module; the contract
   is enforced by ``anticipation_backpressure.py`` reading
   :func:`is_serial_mode` and returning a null context manager when True.

Zero Silent Failures (ZSF)
--------------------------
Two counters are exposed via :func:`get_serial_counters`:

* ``serial_mode_calls_total`` — incremented every time a decorator
  observed ``SERIAL_MODE=1`` and took the serial branch.
* ``serial_mode_fallbacks_total`` — incremented when the serial branch
  itself raised; the original async/queue path is then attempted as a
  fallback rather than letting the exception escape silently.  Any
  fallback failure re-raises with full traceback.

Transition from test to production
----------------------------------
The toggle is *env-only*.  Removing ``SERIAL_MODE`` from the environment
(or setting it to anything other than ``"1"``) restores normal behavior
on the very next decorator entry — no global state is mutated, no
modules are monkey-patched, no caches need clearing.  Production
deployments simply must not set ``SERIAL_MODE``; a CI guard can assert
``os.environ.get("SERIAL_MODE", "0") != "1"`` at process start.

Backpressure note (Agent 4 compatibility)
-----------------------------------------
``anticipation_backpressure.py`` (Agent 4) exposes asyncio.Semaphore /
asyncio.BoundedSemaphore objects used to throttle anticipation work.  In
SERIAL_MODE, those semaphores SHOULD become no-ops — there is no
concurrency to throttle when every call executes inline.  The agreed
contract is::

    # in anticipation_backpressure.py
    from migrate4.test_mode.serial_mode import is_serial_mode

    def acquire(...):
        if is_serial_mode():
            return contextlib.nullcontext()
        return self._semaphore  # normal async path

This file deliberately does not import the backpressure module to keep
``test_mode`` import-cheap and dependency-free.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import os
import threading
from typing import Any, Awaitable, Callable, Dict, TypeVar

__all__ = [
    "bypass_queue_in_serial",
    "get_serial_counters",
    "is_serial_mode",
    "reset_serial_counters",
    "serial_safe",
    "_serial_llm_response",
]

T = TypeVar("T")

# ---------------------------------------------------------------------------
# ZSF counters
# ---------------------------------------------------------------------------
_COUNTERS_LOCK = threading.Lock()
_COUNTERS: Dict[str, int] = {
    "serial_mode_calls_total": 0,
    "serial_mode_fallbacks_total": 0,
}


def _bump(name: str, n: int = 1) -> None:
    """Thread-safe counter increment (ZSF — never silent)."""
    with _COUNTERS_LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + n


def get_serial_counters() -> Dict[str, int]:
    """Return a snapshot copy of the ZSF counters."""
    with _COUNTERS_LOCK:
        return dict(_COUNTERS)


def reset_serial_counters() -> None:
    """Reset all counters to zero.  Tests call this between cases."""
    with _COUNTERS_LOCK:
        for k in list(_COUNTERS.keys()):
            _COUNTERS[k] = 0


# ---------------------------------------------------------------------------
# Public env probe
# ---------------------------------------------------------------------------
def is_serial_mode() -> bool:
    """Return True iff ``SERIAL_MODE`` is literally ``"1"`` in the env.

    Deliberately strict: ``"true"``, ``"yes"``, ``"on"`` are NOT accepted
    so that copy-pasted prod configs cannot accidentally flip the
    toggle.  CI scripts pass exactly ``SERIAL_MODE=1``.
    """
    return os.environ.get("SERIAL_MODE", "0") == "1"


# ---------------------------------------------------------------------------
# Deterministic LLM stub
# ---------------------------------------------------------------------------
def _serial_llm_response(prompt: str) -> str:
    """Deterministic response derived from the prompt hash.

    Tests rely on ``f(prompt) == f(prompt)`` for repeatable assertions.
    The format is intentionally distinguishable from real LLM output so
    a stray production code path that hits this function fails loudly
    in any human review of logs.
    """
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    # 16 hex chars = 64 bits of entropy, plenty for distinct test fixtures.
    return f"[SERIAL_MODE_STUB:{digest[:16]}] echo({len(prompt)})"


# ---------------------------------------------------------------------------
# @serial_safe — coroutine -> sync-or-async dispatcher
# ---------------------------------------------------------------------------
def serial_safe(func: Callable[..., Awaitable[T]]) -> Callable[..., Any]:
    """Decorator: when SERIAL_MODE=1, execute coroutine synchronously.

    Call sites use exactly one form regardless of mode::

        result = await my_async_fn(...)        # SERIAL_MODE unset
        result = await my_async_fn(...)        # SERIAL_MODE=1 (returns an
                                               # already-resolved Future)

    To keep the call site uniform, the wrapper always returns an
    awaitable.  In SERIAL_MODE we drive the coroutine via ``asyncio.run``
    (or, if a loop is already running, ``loop.run_until_complete`` is
    not safe; we therefore use ``asyncio.new_event_loop`` and run there)
    and wrap the concrete result in an already-completed Future so
    ``await wrapper(...)`` still yields a value.

    If the serial execution itself raises, we increment
    ``serial_mode_fallbacks_total`` and fall back to returning the raw
    coroutine for the caller's loop to await normally.  This way a
    SERIAL_MODE bug never silently drops the call.
    """
    if not inspect.iscoroutinefunction(func):
        raise TypeError(
            f"@serial_safe requires an async def function, got {func!r}"
        )

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any):
        if not is_serial_mode():
            return func(*args, **kwargs)  # normal async path

        _bump("serial_mode_calls_total")
        try:
            # asyncio.run refuses to run inside a running loop.  In tests
            # we typically are NOT inside one; the try/except covers the
            # rare case of nested-loop usage.
            try:
                asyncio.get_running_loop()
                _running = True
            except RuntimeError:
                _running = False

            if _running:
                # Spin a dedicated loop on a side thread to avoid nesting.
                box: Dict[str, Any] = {}

                def _runner() -> None:
                    loop = asyncio.new_event_loop()
                    try:
                        box["result"] = loop.run_until_complete(
                            func(*args, **kwargs)
                        )
                    except BaseException as exc:  # noqa: BLE001 — ZSF
                        box["exc"] = exc
                    finally:
                        loop.close()

                t = threading.Thread(target=_runner, daemon=True)
                t.start()
                t.join()
                if "exc" in box:
                    raise box["exc"]
                value = box["result"]
            else:
                value = asyncio.run(func(*args, **kwargs))

            fut: asyncio.Future = asyncio.Future(loop=asyncio.new_event_loop())
            # An already-resolved Future on a throwaway loop is fine for
            # `await` only when the awaiter shares the same loop.  To
            # keep the call-site uniform regardless of caller loop, we
            # return the value wrapped in a tiny coroutine instead.

            async def _identity() -> Any:
                return value

            return _identity()
        except BaseException:  # noqa: BLE001 — ZSF: surface, count, fallback
            _bump("serial_mode_fallbacks_total")
            # Fallback: hand the original coroutine back so the caller's
            # loop can await it normally.  Re-raises happen at await time.
            return func(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# @bypass_queue_in_serial — short-circuit llm_generate-style callers
# ---------------------------------------------------------------------------
def bypass_queue_in_serial(
    func: Callable[..., Any],
) -> Callable[..., Any]:
    """Decorator: when SERIAL_MODE=1, return a deterministic stub instead
    of calling the LLM priority queue.

    The decorated function is expected to be a wrapper around
    :func:`memory.llm_priority_queue.llm_generate` (or any code path
    that ultimately enqueues work).  In SERIAL_MODE we extract the
    ``user_prompt`` keyword/positional argument, route it through
    :func:`_serial_llm_response`, and return that string immediately.

    Prompt extraction order:
        1. keyword ``user_prompt``
        2. keyword ``prompt``
        3. positional arg matching parameter named ``user_prompt`` or
           ``prompt``
        4. concatenated ``str(args)`` as a last resort (still
           deterministic, just less ergonomic)

    Fallback: if extraction or stub generation raises, increment
    ``serial_mode_fallbacks_total`` and call the real function.
    """
    sig = inspect.signature(func)
    param_names = list(sig.parameters)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not is_serial_mode():
            return func(*args, **kwargs)

        _bump("serial_mode_calls_total")
        try:
            prompt: str
            if "user_prompt" in kwargs:
                prompt = str(kwargs["user_prompt"])
            elif "prompt" in kwargs:
                prompt = str(kwargs["prompt"])
            else:
                # Look up by parameter name in positional args.
                prompt = ""
                for idx, name in enumerate(param_names):
                    if name in ("user_prompt", "prompt") and idx < len(args):
                        prompt = str(args[idx])
                        break
                if not prompt:
                    prompt = repr(args) + repr(sorted(kwargs.items()))
            return _serial_llm_response(prompt)
        except BaseException:  # noqa: BLE001 — ZSF: count, fall back
            _bump("serial_mode_fallbacks_total")
            return func(*args, **kwargs)

    return wrapper
