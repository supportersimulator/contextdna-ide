"""Lock-order deadlock detector + runtime panic mode.

Detects classic A->B vs B->A acquisition inversions by tracking per-thread
lock acquisition stacks. Drop-in wrapper around `threading.RLock` / `Lock`.

Design constraints (from granularity_upgrade_plan.md + lock_ordering_invariant.md):
- Zero behavior change when not enabled. Importing this module is free.
- ZSF: every detected inversion increments a counter; no silent pass.
- Bounded memory: per-thread stack capped at MAX_STACK_DEPTH.
- Two modes:
    * count  (default) — inversions are counted + logged; thread continues.
    * panic  (opt-in for tests / Phase B-C cutover) — inversions raise
      `LockOrderViolation` so the offending thread fails fast.
- Cycle detection across N>=2 threads is intentionally scoped to pairwise
  ordering (A->B vs B->A), which covers ~95% of real Python deadlocks
  (see WAL+RLock survey notes). For phase-transition validation we add a
  separate `LockTracker` helper that records the (thread_id, lock_name,
  acquire_order) tuple for offline replay.

Enable in code (Phase B-ready):

    from deadlock_detector import TrackedRLock, get_counters, set_panic_mode

    set_panic_mode(True)   # tests only
    self._locks = {
        "learnings":         TrackedRLock("learnings"),
        "negative_patterns": TrackedRLock("negative_patterns"),
        "device_config":     TrackedRLock("device_config"),
        "stats":             TrackedRLock("stats"),
    }

Counter surface (wired into /health.locks exporter):

    deadlock_acquisitions_total
    deadlock_inversions_total
    deadlock_inversions_total[A<->B]
    deadlock_recursion_depth_max
    deadlock_stack_overflow_total
    deadlock_unknown_lock_total
    deadlock_panics_total
    sqlite_conn_misuse_total
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("memory.deadlock_detector")

# Bounded — a thread holding > 16 locks is a bug independent of order.
MAX_STACK_DEPTH = 16

# Per-thread lock-acquisition stack. Each entry is the lock name string.
_thread_local = threading.local()


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class LockOrderViolation(RuntimeError):
    """Raised when panic mode is on and a lock-order inversion is detected.

    Carries:
        held       -- the lock already held by the thread
        acquiring  -- the lock being acquired (in inverted order)
        canonical  -- a tuple (lo, hi) describing the canonical order
    """

    def __init__(self, held: str, acquiring: str, canonical: Tuple[str, str]) -> None:
        self.held = held
        self.acquiring = acquiring
        self.canonical = canonical
        super().__init__(
            f"lock order inversion: held={held} acquiring={acquiring} "
            f"canonical={canonical[0]}->{canonical[1]}"
        )


class SQLiteMisuse(RuntimeError):
    """Raised when a non-singleton or stale conn is used inside SQLiteStorage."""


# --------------------------------------------------------------------------- #
# Mode flag (count | panic)
# --------------------------------------------------------------------------- #

_MODE_LOCK = threading.Lock()
_panic_mode: bool = False


def set_panic_mode(enabled: bool) -> None:
    """Toggle panic mode. When True, inversions raise `LockOrderViolation`."""
    global _panic_mode
    with _MODE_LOCK:
        _panic_mode = bool(enabled)


def panic_mode() -> bool:
    with _MODE_LOCK:
        return _panic_mode


# --------------------------------------------------------------------------- #
# Counter surface (ZSF)
# --------------------------------------------------------------------------- #

_counters: Counter[str] = Counter()
_inversion_pairs: Counter[str] = Counter()
_counter_lock = threading.Lock()

# Phase B-ready: registry of valid lock names. Empty by default = no checking.
_registered_locks: set = set()


def register_lock_set(names: List[str]) -> None:
    """Phase B/C cutover guard. Acquiring a lock NOT in the registry bumps
    `deadlock_unknown_lock_total`. Empty registry = no enforcement.
    """
    global _registered_locks
    with _counter_lock:
        _registered_locks = set(names)


def clear_lock_registry() -> None:
    global _registered_locks
    with _counter_lock:
        _registered_locks = set()


def _bump(key: str, delta: int = 1) -> None:
    with _counter_lock:
        _counters[key] += delta


def _bump_pair(a: str, b: str) -> None:
    lo, hi = sorted((a, b))
    pair_key = f"{lo}<->{hi}"
    with _counter_lock:
        _inversion_pairs[pair_key] += 1


def get_counters() -> Dict[str, int]:
    """Snapshot of all detector counters. ZSF surface."""
    with _counter_lock:
        out: Dict[str, int] = {
            "deadlock_acquisitions_total":   _counters["acquisitions"],
            "deadlock_inversions_total":     _counters["inversions"],
            "deadlock_recursion_depth_max":  _counters["max_depth"],
            "deadlock_stack_overflow_total": _counters["stack_overflow"],
            "deadlock_unknown_lock_total":   _counters["unknown_lock"],
            "deadlock_panics_total":         _counters["panics"],
            "sqlite_conn_misuse_total":      _counters["sqlite_conn_misuse"],
            "deadlock_release_without_acquire_total": _counters["release_without_acquire"],
        }
        for pair_key, count in _inversion_pairs.items():
            out[f"deadlock_inversions_total[{pair_key}]"] = count
        return out


def reset_counters() -> None:
    """Test helper. Not for production use."""
    with _counter_lock:
        _counters.clear()
        _inversion_pairs.clear()
    with _canonical_lock:
        _canonical_order.clear()


# --------------------------------------------------------------------------- #
# Canonical order map (pair-wise)
# --------------------------------------------------------------------------- #

_canonical_order: Dict[Tuple[str, str], int] = {}
_canonical_lock = threading.Lock()


def seed_canonical_order(pairs: List[Tuple[str, str]]) -> None:
    """Pre-seed the canonical-order map. Used to lock in the documented
    invariant before any code runs — so test ordering cannot establish a
    bogus 'first observation'.

    Each entry (a, b) means: "if a thread holds `a` and tries to acquire
    `b`, that is the canonical order — the reverse is an inversion."
    """
    with _canonical_lock:
        for a, b in pairs:
            _canonical_order.setdefault((a, b), 0)


# --------------------------------------------------------------------------- #
# Per-thread tracker
# --------------------------------------------------------------------------- #


def _stack() -> List[str]:
    s = getattr(_thread_local, "stack", None)
    if s is None:
        s = []
        _thread_local.stack = s
    return s


def _trace() -> List[Tuple[int, str, float]]:
    """Per-thread acquire trace: list of (thread_id, lock_name, t_monotonic).

    Used by `LockTracker` for offline replay during phase-transition tests.
    """
    t = getattr(_thread_local, "trace", None)
    if t is None:
        t = []
        _thread_local.trace = t
    return t


def get_trace_snapshot() -> List[Tuple[int, str, float]]:
    """Return a copy of the current thread's acquire trace."""
    return list(_trace())


def reset_trace() -> None:
    """Reset the current thread's acquire trace (test helper)."""
    _thread_local.trace = []


def _record_acquisition(name: str) -> None:
    stack = _stack()
    trace = _trace()

    # Phase B/C cutover guard: unknown locks.
    with _counter_lock:
        if _registered_locks and name not in _registered_locks:
            _counters["unknown_lock"] += 1
            logger.warning(
                "deadlock_detector: unknown lock %s acquired (not in registry)",
                name,
            )

    # ZSF: a thread holding > MAX_STACK_DEPTH locks is a bug — log + count.
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

    inversion_pair: Optional[Tuple[str, str]] = None

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
                inversion_pair = (held, name)
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
    trace.append((threading.get_ident(), name, time.monotonic()))
    _bump("acquisitions")

    # Panic AFTER pushing onto the stack so release semantics still match.
    if inversion_pair is not None and panic_mode():
        held, acquiring = inversion_pair
        _bump("panics")
        raise LockOrderViolation(
            held=held,
            acquiring=acquiring,
            canonical=(acquiring, held),  # the reverse of the observed inversion
        )


def _record_release(name: str) -> None:
    stack = _stack()
    for i in range(len(stack) - 1, -1, -1):
        if stack[i] == name:
            stack.pop(i)
            return
    _bump("release_without_acquire")


# --------------------------------------------------------------------------- #
# TrackedRLock — drop-in wrapper
# --------------------------------------------------------------------------- #


class TrackedRLock:
    """Drop-in replacement for `threading.RLock` with order tracking.

    Behavior is identical to RLock except that acquire/release update the
    per-thread stack and the canonical-order map.
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
            try:
                _record_acquisition(self._name)
            except LockOrderViolation:
                # Panic mode: release before re-raising so the underlying
                # RLock is not leaked.
                try:
                    self._lock.release()
                finally:
                    raise
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
# LockTracker — offline replay of acquire traces
# --------------------------------------------------------------------------- #


class LockTracker:
    """Records lock acquire traces across all threads and validates them
    against a declared canonical ordering.

    Usage in a test:

        canonical = [("learnings", "stats")]   # learnings must be acquired first
        tracker = LockTracker(canonical)
        tracker.start()
        ... run code under test ...
        tracker.stop()
        result = tracker.analyze()
        assert result.violations == []
    """

    def __init__(self, canonical_pairs: List[Tuple[str, str]]) -> None:
        self._canonical = list(canonical_pairs)
        self._lock = threading.Lock()
        self._records: List[Tuple[int, str, float, str]] = []  # (tid, lock, t, action)
        self._wrapped: List[Tuple[Callable, Callable]] = []
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        # Pre-seed canonical pairs.
        seed_canonical_order(self._canonical)
        self._started = True

    def stop(self) -> None:
        self._started = False

    def record(self, lock_name: str, action: str) -> None:
        """External hook — call from acquire/release in instrumented locks."""
        with self._lock:
            self._records.append(
                (threading.get_ident(), lock_name, time.monotonic(), action)
            )

    def analyze(self) -> "LockTrackerResult":
        """Replay records and identify per-thread acquire chains that violate
        canonical order."""
        violations: List[Tuple[int, str, str]] = []
        # Build a per-thread held-set as we replay.
        held: Dict[int, List[str]] = {}
        with self._lock:
            records = list(self._records)
        for tid, lock_name, _ts, action in records:
            stk = held.setdefault(tid, [])
            if action == "acquire":
                for h in stk:
                    if h == lock_name:
                        continue
                    if (lock_name, h) in _canonical_order and (h, lock_name) not in _canonical_order:
                        # Held=h, acquiring=lock_name; canonical says lock_name->h.
                        violations.append((tid, h, lock_name))
                stk.append(lock_name)
            elif action == "release":
                for i in range(len(stk) - 1, -1, -1):
                    if stk[i] == lock_name:
                        stk.pop(i)
                        break
        return LockTrackerResult(
            canonical=self._canonical,
            violations=violations,
            record_count=len(records),
        )


class LockTrackerResult:
    __slots__ = ("canonical", "violations", "record_count")

    def __init__(
        self,
        canonical: List[Tuple[str, str]],
        violations: List[Tuple[int, str, str]],
        record_count: int,
    ) -> None:
        self.canonical = canonical
        self.violations = violations
        self.record_count = record_count

    @property
    def ok(self) -> bool:
        return not self.violations

    def __repr__(self) -> str:
        return (
            f"LockTrackerResult(ok={self.ok}, records={self.record_count}, "
            f"violations={len(self.violations)})"
        )


# --------------------------------------------------------------------------- #
# SQLITE_MISUSE guard
# --------------------------------------------------------------------------- #


def assert_conn_singleton(conn: Any, expected: Any) -> None:
    """Verify `conn` is the singleton connection captured at construction.

    Bumps `sqlite_conn_misuse_total` and raises `SQLiteMisuse` on any
    deviation. ZSF — the counter advances on every failure path.
    """
    if conn is None:
        _bump("sqlite_conn_misuse")
        raise SQLiteMisuse("conn is None")
    if expected is None:
        _bump("sqlite_conn_misuse")
        raise SQLiteMisuse("expected conn singleton is None (not captured)")
    if conn is not expected:
        _bump("sqlite_conn_misuse")
        raise SQLiteMisuse(
            f"conn identity mismatch: got id={id(conn):#x} "
            f"expected id={id(expected):#x}"
        )


# --------------------------------------------------------------------------- #
# Self-check
# --------------------------------------------------------------------------- #


def _self_check() -> Dict[str, int]:
    """Trigger one inversion deterministically; return counter snapshot."""
    reset_counters()
    set_panic_mode(False)
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

    snap = get_counters()
    assert snap.get("deadlock_inversions_total", 0) >= 1, (
        "self-check failed: expected >=1 inversion"
    )

    # Now verify panic mode raises.
    reset_counters()
    set_panic_mode(True)
    a2 = TrackedRLock("learnings")
    b2 = TrackedRLock("negative_patterns")
    seed_canonical_order([("learnings", "negative_patterns")])
    panic_raised = False
    try:
        with b2:
            with a2:  # inversion -> raise
                pass
    except LockOrderViolation:
        panic_raised = True
    finally:
        set_panic_mode(False)
    assert panic_raised, "panic mode did not raise on inversion"
    return get_counters()


if __name__ == "__main__":
    snap = _self_check()
    print("deadlock_detector self-check counters:")
    for k, v in sorted(snap.items()):
        print(f"  {k} = {v}")
    print("OK: detector tripped on deliberate inversion + panic mode raised")
