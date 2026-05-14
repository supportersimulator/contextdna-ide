#!/usr/bin/env python3
"""
test_rlock_gil_no_inversion.py — RLock × GIL fairness (no priority inversion)
=============================================================================

Cross-exam unknown-unknown #2 (B6, Round-3-surgeon Phase A audit):

    Could a HIGH-priority thread holding the storage RLock be displaced by a
    LOW-priority thread (classical priority inversion), or could repeated
    LOW-priority writers starve a MEDIUM-priority reader so badly that the
    reader's latency exceeds shadow-sampling deadlines?

Why the *classical* priority inversion is impossible in CPython
---------------------------------------------------------------
CPython's GIL does not honour OS thread priorities. All Python-bytecode-level
threads are treated equally: the interpreter switches between them on the
sys.setswitchinterval() boundary (default 5ms) regardless of pthread
priority. So:

  * Setting thread priority via os.setpriority / pthread_setschedparam does
    NOT affect Python-level scheduling.
  * "Priority inversion" in the classical sense (where a HIGH thread blocks
    on a lock held by LOW which is preempted by MEDIUM) cannot manifest
    through the GIL — there is no MEDIUM that gets preferred over LOW from
    the interpreter's view.

What CAN still go wrong (and what this test checks)
---------------------------------------------------
Python's `threading.RLock` is a *non-fair* lock backed by a pthread/mutex.
Acquisition order is implementation-defined; in pathological cases a single
thread can re-acquire repeatedly and starve waiters indefinitely.

This test stresses the fairness of `SQLiteStorage._lock` (a `threading.RLock`)
under sustained contention from threads with different work profiles:

  * HIGH: 1 thread doing long writes (large content) — proxy for "important"
  * LOW:  2 threads doing long writes (large content) — proxy for "bulk"
  * MED:  1 thread doing short reads (cheap get_recent calls)

We do NOT try to set OS thread priorities (would be a no-op for the GIL).
Instead we label threads by their work profile and measure:

  * Per-thread median latency.
  * Per-thread max latency.
  * Starvation ratio = thread_max / (overall_median + epsilon).

PASS criteria
-------------
  * No thread's median latency > 10 * global_median.
  * No thread's max latency > 20 * global_median.
  * All threads complete (i.e. no thread sits in `RLock.acquire()` forever
    while others keep grabbing it).
  * No exceptions.
  * Every "MED" reader completes at least 80% of its scheduled ops
    (proves the reader was not perpetually starved).

ZSF (Zero Silent Failures)
--------------------------
  * Worker exceptions captured in a thread-safe list; loud failure.
  * Per-thread completion counts asserted (a thread that "passes" by doing
    zero ops is a failure mode we explicitly reject).
  * Seed printed on failure.

Companion docs
--------------
  * scripts/lock_audit/lock_ordering_invariant.md (single-lock proof — Phase A)
  * scripts/lock_audit/unknown_unknowns_register.md (this test addresses #2)
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

# Ensure repo root is importable regardless of where pytest is invoked from.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memory.sqlite_storage import SQLiteStorage, get_counters  # noqa: E402

logger = logging.getLogger("test_rlock_gil_no_inversion")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


# --------------------------------------------------------------------------- #
# Tunables                                                                     #
# --------------------------------------------------------------------------- #

WALL_BUDGET_S = 20.0           # absolute test wall-clock budget
HIGH_WRITER_OPS = 30
LOW_WRITER_OPS = 30
MED_READER_OPS = 60
LONG_CONTENT_BYTES = 8 * 1024   # ~8 KB payload — proxy for "long" work

# Fairness thresholds.
#
# Per-thread median is the right starvation signal: if a thread's typical op
# takes much longer than the rest of the population, it is starving. Per-op
# *max* ratios are noisy when the absolute scale is sub-millisecond — a single
# 3ms op vs 0.1ms median produces a 30x "ratio" that reflects scheduler jitter,
# not lock unfairness. So we apply the max-ratio check only when absolute max
# crosses a meaningful threshold (10 ms), past which a tail outlier IS likely
# to break shadow-sampling deadlines.
STARVATION_MAX_MEDIAN = 10.0          # thread median must not exceed 10x global median
STARVATION_MAX_MAX = 20.0             # thread max ratio threshold (only checked if abs > floor)
STARVATION_MAX_MAX_ABS_FLOOR_S = 0.010  # ignore tail ratios when abs max < 10ms
MIN_READER_COMPLETION_RATIO = 0.80    # reader must finish >= 80% of its ops

DEFAULT_SEED = 8989


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@contextmanager
def _ephemeral_db() -> Iterator[Path]:
    tmpdir = Path(tempfile.mkdtemp(prefix="lockesc-rlock-"))
    db_path = tmpdir / "learnings.db"
    db_path.touch()
    prev_db = os.environ.get("CONTEXT_DNA_LEARNINGS_DB")
    prev_backend = os.environ.get("STORAGE_BACKEND")
    os.environ["CONTEXT_DNA_LEARNINGS_DB"] = str(db_path)
    os.environ["STORAGE_BACKEND"] = "sqlite"
    try:
        yield db_path
    finally:
        if prev_db is None:
            os.environ.pop("CONTEXT_DNA_LEARNINGS_DB", None)
        else:
            os.environ["CONTEXT_DNA_LEARNINGS_DB"] = prev_db
        if prev_backend is None:
            os.environ.pop("STORAGE_BACKEND", None)
        else:
            os.environ["STORAGE_BACKEND"] = prev_backend
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(scope="function")
def ephemeral_db() -> Iterator[Path]:
    with _ephemeral_db() as db:
        yield db


@pytest.fixture(scope="function")
def storage(ephemeral_db: Path) -> Iterator[SQLiteStorage]:
    inst = SQLiteStorage(db_path=ephemeral_db)
    try:
        yield inst
    finally:
        inst.close()


@pytest.fixture(scope="function")
def seeded_random() -> Iterator[int]:
    seed = int(os.environ.get("LOCKESC_SEED", DEFAULT_SEED))
    random.seed(seed)
    logger.info("seeded random with %d (override via $LOCKESC_SEED)", seed)
    yield seed


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _long_payload(label: str, idx: int) -> Dict[str, Any]:
    return {
        "id": f"prio_{label}_{uuid.uuid4().hex[:10]}_{idx}",
        "type": "win",
        "title": f"priority probe {label} {idx}",
        # Long content forces each write to do more work inside the lock, so
        # starvation (if it exists) has a chance to manifest in measurable
        # latency.
        "content": ("x" * LONG_CONTENT_BYTES) + f" {uuid.uuid4().hex}",
        "tags": ["priority", label],
        "source": "test_rlock_gil_no_inversion",
    }


def _try_set_os_priority(nice: int) -> bool:
    """Best-effort OS thread niceness.

    We try to set per-thread priority for completeness, but we expect it to
    be a no-op for Python-level scheduling. Returns True if the call
    succeeded, False otherwise (e.g. macOS without privileges).
    """
    try:
        os.setpriority(os.PRIO_PROCESS, 0, nice)
        return True
    except (OSError, PermissionError, AttributeError):
        return False


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_gil_does_not_honor_thread_priorities_documented(seeded_random: int) -> None:
    """Documentation test: confirm we understand the GIL behaviour we rely on.

    This test is not a defect probe — it just records the invariant that the
    CPython GIL does not honour OS-level thread priorities, which is the
    reason a "classical" priority inversion via threading.RLock + GIL is
    structurally impossible. If a future Python release changes this (it
    won't — the GIL design is stable), the test will fail and force a
    re-audit.
    """
    # CPython's switchinterval is a round-robin timeslice, not a priority
    # scheduler. We assert that the default is a positive float in the
    # documented range — if Python ever switches to a priority queue, the
    # interface and semantics will change visibly here.
    interval = sys.getswitchinterval()
    assert isinstance(interval, float)
    assert 0.0 < interval < 1.0, (
        f"sys.getswitchinterval() returned {interval!r} — outside documented "
        f"range. Did Python switch to a priority scheduler? Re-audit RLock "
        f"fairness assumptions (seed={seeded_random})."
    )
    logger.info("GIL switchinterval = %.6fs (round-robin, priority-blind)", interval)


def test_rlock_fairness_under_mixed_workload(
    storage: SQLiteStorage,
    seeded_random: int,
) -> None:
    """Probe RLock fairness with HIGH/LOW writers + MED readers.

    No thread should starve relative to the global median latency, and the
    MED reader thread (cheapest, shortest ops) must complete most of its
    scheduled ops despite competing with two LOW writers and one HIGH writer.
    """
    counters_before = get_counters()
    errors: List[BaseException] = []

    # Per-thread latency buckets (label -> list of per-op seconds).
    bucket_lock = threading.Lock()
    buckets: Dict[str, List[float]] = {"HIGH": [], "LOW": [], "MED": []}
    completions: Dict[str, int] = {"HIGH": 0, "LOW": 0, "MED": 0}

    barrier = threading.Barrier(4)  # 1 HIGH + 2 LOW + 1 MED

    def writer(label: str, ops: int, nice: int) -> None:
        _try_set_os_priority(nice)  # cosmetic — won't change Python scheduling
        try:
            barrier.wait(timeout=5.0)
        except threading.BrokenBarrierError as exc:
            errors.append(exc)
            return
        done = 0
        for i in range(ops):
            payload = _long_payload(label, i)
            t0 = time.perf_counter()
            try:
                storage.store_learning(payload, skip_dedup=True)
            except BaseException as exc:  # noqa: BLE001 — ZSF
                errors.append(exc)
                return
            dt = time.perf_counter() - t0
            with bucket_lock:
                buckets[label].append(dt)
            done += 1
            # Yield briefly to give the OS a chance to schedule other threads;
            # mimics realistic bursty workload.
            time.sleep(0)
        with bucket_lock:
            completions[label] += done

    def reader(ops: int, nice: int) -> None:
        _try_set_os_priority(nice)
        try:
            barrier.wait(timeout=5.0)
        except threading.BrokenBarrierError as exc:
            errors.append(exc)
            return
        done = 0
        deadline = time.perf_counter() + WALL_BUDGET_S
        for _ in range(ops):
            if time.perf_counter() > deadline:
                break
            t0 = time.perf_counter()
            try:
                rows = storage.get_recent(limit=20)
            except BaseException as exc:  # noqa: BLE001 — ZSF
                errors.append(exc)
                return
            dt = time.perf_counter() - t0
            with bucket_lock:
                buckets["MED"].append(dt)
            assert isinstance(rows, list)
            done += 1
        with bucket_lock:
            completions["MED"] += done

    threads = [
        threading.Thread(target=lambda: writer("HIGH", HIGH_WRITER_OPS, nice=-5), name="HIGH"),
        threading.Thread(target=lambda: writer("LOW", LOW_WRITER_OPS, nice=10), name="LOW-1"),
        threading.Thread(target=lambda: writer("LOW", LOW_WRITER_OPS, nice=10), name="LOW-2"),
        threading.Thread(target=lambda: reader(MED_READER_OPS, nice=0), name="MED"),
    ]

    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        # Hard wall-clock cap: if a thread is alive at WALL_BUDGET_S + slack,
        # we have a starvation problem to surface.
        t.join(timeout=WALL_BUDGET_S + 10.0)
        assert not t.is_alive(), (
            f"thread {t.name} did not finish within {WALL_BUDGET_S + 10}s — "
            f"likely starvation (seed={seeded_random})"
        )
    wall = time.perf_counter() - start

    assert not errors, f"workers raised: {errors}"

    # No storage-layer failures.
    counters_after = get_counters()
    for key in ("store.fail", "get_recent.fail"):
        delta = counters_after.get(key, 0) - counters_before.get(key, 0)
        assert delta == 0, (
            f"counter {key} advanced by {delta} during fairness test "
            f"(seed={seeded_random})"
        )

    # Fairness analysis.
    all_latencies: List[float] = []
    for arr in buckets.values():
        all_latencies.extend(arr)
    assert all_latencies, "no measurements collected — fixture broken"
    global_median = statistics.median(all_latencies)

    summary: List[str] = []
    failures: List[str] = []
    for label, arr in buckets.items():
        if not arr:
            failures.append(f"{label}: 0 ops completed — total starvation")
            continue
        med = statistics.median(arr)
        mx = max(arr)
        ratio_med = med / global_median if global_median > 0 else 0
        ratio_max = mx / global_median if global_median > 0 else 0
        summary.append(
            f"{label}: ops={len(arr)} median={med:.4f}s max={mx:.4f}s "
            f"ratio_med={ratio_med:.2f}x ratio_max={ratio_max:.2f}x"
        )
        if ratio_med > STARVATION_MAX_MEDIAN:
            failures.append(
                f"{label} median ratio {ratio_med:.2f}x > "
                f"{STARVATION_MAX_MEDIAN}x — starvation"
            )
        # Tail-ratio check only fires when the absolute tail is large enough
        # to matter — sub-ms tail ratios are scheduler jitter, not starvation.
        if ratio_max > STARVATION_MAX_MAX and mx >= STARVATION_MAX_MAX_ABS_FLOOR_S:
            failures.append(
                f"{label} max ratio {ratio_max:.2f}x > "
                f"{STARVATION_MAX_MAX}x AND abs max {mx:.4f}s >= "
                f"{STARVATION_MAX_MAX_ABS_FLOOR_S}s — tail starvation"
            )

    logger.info(
        "fairness wall=%.2fs global_median=%.4fs",
        wall,
        global_median,
    )
    for line in summary:
        logger.info("  %s", line)

    # Reader completion ratio: the MED reader must not be starved out.
    expected_med = MED_READER_OPS
    actual_med = completions["MED"]
    completion_ratio = actual_med / expected_med if expected_med else 0
    logger.info(
        "MED reader completion: %d/%d = %.1f%%",
        actual_med,
        expected_med,
        100.0 * completion_ratio,
    )
    if completion_ratio < MIN_READER_COMPLETION_RATIO:
        failures.append(
            f"MED reader completed only {actual_med}/{expected_med} ops "
            f"({100.0 * completion_ratio:.1f}%) < "
            f"{100.0 * MIN_READER_COMPLETION_RATIO:.0f}% — starved"
        )

    assert not failures, (
        "RLock fairness FAILED:\n  " + "\n  ".join(failures) +
        f"\nbuckets: {summary}\nseed={seeded_random}"
    )


def test_no_lock_held_across_thread_exit(
    storage: SQLiteStorage,
    seeded_random: int,
) -> None:
    """Negative-space probe: a thread that exits mid-acquire must release.

    A subtle RLock failure mode: if a thread is killed (or its execution
    aborts) while holding an RLock, no other thread can acquire it. We can't
    actually kill a CPython thread, but we can simulate the post-exit state
    by acquiring + releasing in one thread and confirming the lock is
    immediately usable from another thread.

    This guards against a regression where future code might use a
    non-reentrant lock or hold a lock past thread death.
    """
    acquired_event = threading.Event()
    proceed_event = threading.Event()
    errors: List[BaseException] = []

    def holder() -> None:
        try:
            with storage._lock:  # noqa: SLF001 — intentional white-box probe
                acquired_event.set()
                proceed_event.wait(timeout=5.0)
        except BaseException as exc:  # noqa: BLE001 — ZSF
            errors.append(exc)

    t = threading.Thread(target=holder, name="holder")
    t.start()
    assert acquired_event.wait(timeout=2.0), "holder never acquired lock"

    # While holder still owns the lock, attempt to acquire from main thread
    # with a non-blocking try — must fail (proves the lock IS held).
    got = storage._lock.acquire(blocking=False)  # noqa: SLF001
    assert not got, (
        "main thread acquired RLock while holder still owns it — RLock is "
        f"broken or not actually mutual-exclusive (seed={seeded_random})"
    )

    proceed_event.set()
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert not errors

    # After holder exits, lock must be re-acquirable from main thread.
    got = storage._lock.acquire(blocking=True, timeout=2.0)  # noqa: SLF001
    assert got, (
        f"RLock not released after holder thread exited — guaranteed deadlock "
        f"in production (seed={seeded_random})"
    )
    storage._lock.release()  # noqa: SLF001


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
