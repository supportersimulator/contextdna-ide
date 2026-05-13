"""Lock-order deadlock detector.

Detects classic A->B vs B->A acquisition inversions by tracking per-thread
lock acquisition stacks. Drop-in wrapper around `threading.RLock` / `Lock`.

Design constraints (from granularity_upgrade_plan.md, Phase B):
- Zero behavior change when not enabled. Importing this module is free.
- ZSF: every detected inversion increments a counter; no silent pass.
- Bounded memory: per-thread stack capped at MAX_STACK_DEPTH.
- Cycle detection across N>=2 threads is intentionally out of scope; we
  only catch A->B / B->A pairwise inversions, which cover ~95% of real
  Python deadlocks (see WAL+RLock survey notes).

Enable in code:

    from deadlock_detector import TrackedRLock, get_counters

    self._locks = {
        "learnings":         TrackedRLock("learnings"),
        "negative_patterns": TrackedRLock("negative_patterns"),
    }

Counters surface via `get_counters()`; wire into `/health.locks` exporter.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger("memory.deadlock_detector")

# Bounded — a thread holding > 16 locks is a bug independent of order.
MAX_STACK_DEPTH = 16

# Per-thread lock-acquisition stack. Each entry is the lock name string.
_thread_local = threading.local()


def _stack() -> List[str]:
    s = getattr(_thread_local, "stack", None)
    if s is None:
        s = []
        _thread_local.stack = s
    return s


# Observed pair-ordering: maps (held_name, acquired_name) -> count.
# A canonical ordering is fixed on first observation. Any subsequent
# acquisition of the reverse pair is an inversion.
_canonical_order: Dict[tuple, int] = {}
_canonical_lock = threading.Lock()

# ZSF counters.
_counters: Counter[str] = Counter()
# Per-pair inversion counts. Key: "A<->B" (sorted), value: int.
_inversion_pairs: Counter[str] = Counter()
_counter_lock = threading.Lock()


def _bump(key: str, delta: int = 1) -> None:
    with _counter_lock:
        _counters[key] += delta


def _bump_pair(a: str, b: str) -> None:
    # Sorted key so "A<->B" and "B<->A" collapse to one bucket.
    lo, hi = sorted((a, b))
    pair_key = f"{lo}<->{hi}"
    with _counter_lock:
        _inversion_pairs[pair_key] += 1


def get_counters() -> Dict[str, int]:
    """Snapshot of all detector counters. ZSF surface."""
    with _counter_lock:
        out: Dict[str, int] = {
            "deadlock_acquisitions_total":  _counters["acquisitions"],
            "deadlock_inversions_total":    _counters["inversions"],
            "deadlock_recursion_depth_max": _counters["max_depth"],
            "deadlock_stack_overflow_total": _counters["stack_overflow"],
        }
        for pair_key, count in _inversion_pairs.items():
            # e.g. "deadlock_inversions_total{pair=learnings<->negative_patterns}"
            out[f"deadlock_inversions_total[{pair_key}]"] = count
        return out


def reset_counters() -> None:
    """Test helper. Not for production use."""
    with _counter_lock:
        _counters.clear()
        _inversion_pairs.clear()
    with _canonical_lock:
        _canonical_order.clear()


def _record_acquisition(name: str) -> None:
    stack = _stack()

    # ZSF: a thread holding > MAX_STACK_DEPTH locks is a bug — log + count
    # but do not raise (we are not the gatekeeper for correctness).
    if len(stack) >= MAX_STACK_DEPTH:
        _bump("stack_overflow")
        logger.warning(
            "deadlock_detector: stack depth %d exceeded for thread %s; "
            "tracking dropped for this acquisition",
            len(stack), threading.current_thread().name,
        )
        return

    # Track max depth observed.
    with _counter_lock:
        if len(stack) + 1 > _counters["max_depth"]:
            _counters["max_depth"] = len(stack) + 1

    # Validate ordering against every lock already held.
    for held in stack:
        if held == name:
            # Reentrant — fine for RLock.
            continue
        with _canonical_lock:
            forward = (held, name)
            reverse = (name, held)
            if forward in _canonical_order:
                _canonical_order[forward] += 1
            elif reverse in _canonical_order:
                # Inversion detected.
                _bump("inversions")
                _bump_pair(held, name)
                logger.error(
                    "deadlock_detector: ORDER INVERSION thread=%s held=%s "
                    "acquiring=%s canonical=%s->%s",
                    threading.current_thread().name, held, name,
                    name, held,
                )
            else:
                # First observation establishes canonical order.
                _canonical_order[forward] = 1

    stack.append(name)
    _bump("acquisitions")


def _record_release(name: str) -> None:
    stack = _stack()
    # Release the topmost matching entry (RLock release semantics).
    for i in range(len(stack) - 1, -1, -1):
        if stack[i] == name:
            stack.pop(i)
            return
    # Release without acquisition — usually a bug in the caller, but we
    # don't raise here; just count it.
    _bump("release_without_acquire")


class TrackedRLock:
    """Drop-in replacement for `threading.RLock` with order tracking.

    Behavior is identical to RLock except that acquire/release update the
    per-thread stack and the canonical-order map. Imports cleanly and is
    a no-op if you never instantiate one — module import alone changes
    nothing.
    """

    __slots__ = ("_lock", "_name")

    def __init__(self, name: str) -> None:
        if not name:
            raise ValueError("TrackedRLock requires a non-empty name")
        self._lock = threading.RLock()
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if timeout == -1:
            acquired = self._lock.acquire(blocking)
        else:
            acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            _record_acquisition(self._name)
        return acquired

    def release(self) -> None:
        _record_release(self._name)
        self._lock.release()

    def __enter__(self) -> "TrackedRLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


# --------------------------------------------------------------------------- #
# Self-check entry point. `python deadlock_detector.py` runs a tiny demo that
# triggers a deliberate inversion so an operator can see it works end-to-end.
# --------------------------------------------------------------------------- #


def _self_check() -> Dict[str, int]:
    """Trigger one inversion deterministically; return counter snapshot."""
    reset_counters()
    a = TrackedRLock("learnings")
    b = TrackedRLock("negative_patterns")

    # Establish canonical order: a -> b.
    with a:
        with b:
            pass

    # Force an inversion: b -> a.
    with b:
        with a:
            pass

    return get_counters()


if __name__ == "__main__":
    snap = _self_check()
    print("deadlock_detector self-check counters:")
    for k, v in sorted(snap.items()):
        print(f"  {k} = {v}")
    assert snap.get("deadlock_inversions_total", 0) >= 1, (
        "self-check failed: expected >=1 inversion"
    )
    print("OK: detector tripped on deliberate inversion")
