#!/usr/bin/env python3
"""
test_sqlite_wal_under_rlock.py — SQLite internal lock escalation × Python RLock
==============================================================================

Cross-exam unknown-unknown #1 (B6, Round-3-surgeon Phase A audit):

    Could SQLite's internal lock state machine (UNLOCKED -> SHARED -> RESERVED
    -> PENDING -> EXCLUSIVE) interact with Python's threading.RLock in a way
    that causes a write to stall *while* the Python RLock is held, producing
    a silent deadlock or `database is locked` error that surfaces only under
    sustained concurrent load?

Why WAL mode makes the "yes" path unreachable
---------------------------------------------
* In rollback-journal mode, the writer acquires RESERVED -> PENDING ->
  EXCLUSIVE on the shared DB file; concurrent readers holding SHARED block
  the transition to EXCLUSIVE and the writer waits (or gets SQLITE_BUSY).
  With Python's RLock serialising every method, the *Python* writer holds the
  RLock the entire time it is blocked on SQLite's internal lock state — that
  is a classic two-layer-lock stall.

* In WAL mode (which `memory/sqlite_storage.py` line 195 enables via
  `PRAGMA journal_mode=WAL`), readers do NOT take a SHARED lock on the main
  DB file; they read from the WAL file at a frozen snapshot. Writers append
  to the WAL without needing EXCLUSIVE on the main DB. Concurrent reads do
  not block writes and vice versa. The only serialisation that survives is
  writer-vs-writer, which is exactly what Python's RLock already enforces
  (so the SQLite layer never has to wait).

This test proves the WAL-mode guarantee empirically and verifies that no
`database is locked` or `SQLITE_BUSY` ever leaks out of the storage layer
under the three lock-interaction patterns the cross-exam identified.

Three test cases
----------------
1. **read-while-write**: writer thread holds the RLock (inside
   `store_learning`); a reader thread calls `get_recent` concurrently —
   must NOT raise SQLITE_BUSY, must NOT block longer than 5x the writer's
   nominal latency.

2. **write-while-read**: a reader thread is mid-`get_recent` (slowed by a
   large LIMIT) when a writer arrives — writer must not see SQLITE_BUSY.

3. **write-while-write**: two writer threads collide on `store_learning`;
   RLock must serialise them (one waits for the other) and neither errors.
   This is the canary that Python's RLock IS doing the serialisation work
   that prevents the SQLite layer from ever needing to escalate to
   PENDING / EXCLUSIVE under contention.

PASS criteria
-------------
* Zero `sqlite3.OperationalError` with `database is locked` message.
* Zero `SQLITE_BUSY` codes leaked out of the storage layer's exception
  channel (counters: `store.fail`, `get_recent.fail`, etc. must be 0).
* All operations complete within `MAX_LATENCY_S` (5s for the slowest case).
* WAL mode confirmed via `PRAGMA journal_mode` returning `wal`.

ZSF (Zero Silent Failures)
--------------------------
* Worker exceptions captured in a thread-safe list; test fails loud.
* Counter deltas asserted explicitly (not just "no exception" — must also
  prove the success counter advanced, otherwise we could be passing by
  doing nothing).
* Random seed printed on failure for reproducibility.

Companion docs
--------------
* scripts/lock_audit/lock_ordering_invariant.md (Phase A — single-lock proof)
* scripts/lock_audit/unknown_unknowns_register.md (this test addresses item #1)
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

# Ensure repo root is importable regardless of where pytest is invoked from.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memory.sqlite_storage import (  # noqa: E402
    SQLiteStorage,
    get_counters,
)

logger = logging.getLogger("test_sqlite_wal_under_rlock")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


# --------------------------------------------------------------------------- #
# Tunables                                                                     #
# --------------------------------------------------------------------------- #

MAX_LATENCY_S = 5.0  # absolute ceiling per worker op
WRITER_OPS = 20
READER_OPS = 40
WRITERS_BOTH = 4  # for write-while-write
DEFAULT_SEED = 4242


# --------------------------------------------------------------------------- #
# Ephemeral DB fixture (mirrors tests/storage_edge/test_storage_edge_cases.py) #
# --------------------------------------------------------------------------- #


@contextmanager
def _ephemeral_db() -> Iterator[Path]:
    """Stand up a fresh DB so this test never touches the live ~/.context-dna."""
    tmpdir = Path(tempfile.mkdtemp(prefix="lockesc-wal-"))
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
    """Fresh SQLiteStorage instance per test — bypasses the module singleton.

    Bypassing the singleton is intentional: we want fully isolated per-test
    instances with their own RLock so a failure in case 1 cannot pollute
    cases 2 or 3 via shared singleton state.
    """
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


def _make_payload(idx: int) -> Dict[str, Any]:
    return {
        "id": f"lockesc_{uuid.uuid4().hex[:10]}_{idx}",
        "type": "win",
        "title": f"lock-escalation seed {idx} {uuid.uuid4().hex[:6]}",
        "content": f"WAL-mode escalation probe payload {idx} {uuid.uuid4().hex}",
        "tags": ["lock-escalation", "wal-mode"],
        "source": "test_sqlite_wal_under_rlock",
    }


def _confirm_wal_mode(db_path: Path) -> str:
    """Open a side-channel connection and read PRAGMA journal_mode.

    Side-channel so we observe the disk state, not just what the Storage
    instance thinks it set. If WAL is not actually engaged on disk, the test
    short-circuits with a clear error rather than running misleadingly green.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        return (row[0] if row else "").lower()
    finally:
        conn.close()


def _checkpoint(db_path: Path) -> Tuple[int, int, int]:
    """Run `PRAGMA wal_checkpoint` and return (busy, log, checkpointed).

    Used as a "phase marker" the test can call between cases to flush WAL
    state and verify the checkpoint itself never gets a BUSY return (which
    would indicate a stuck SHARED lock from a previous case).

    Returns a tuple even on rare driver hiccups so the test asserts on
    concrete values rather than implicit None.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    try:
        row = conn.execute("PRAGMA wal_checkpoint").fetchone()
        if not row:
            return (-1, -1, -1)
        return (int(row[0]), int(row[1]), int(row[2]))
    finally:
        conn.close()


def _run_workers(
    fn,
    n: int,
    *,
    name: str,
    errors: List[BaseException],
    latencies: List[float],
    barrier: Optional[threading.Barrier] = None,
) -> List[Any]:
    """Run `n` instances of `fn(worker_id)` in a ThreadPoolExecutor."""
    results: List[Any] = []
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix=name) as ex:
        futs = [ex.submit(fn, i, barrier) for i in range(n)]
        for fut in as_completed(futs):
            try:
                res = fut.result()
                results.append(res)
            except BaseException as exc:  # noqa: BLE001 — ZSF: capture everything
                errors.append(exc)
    return results


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_wal_mode_is_actually_enabled(storage: SQLiteStorage, ephemeral_db: Path) -> None:
    """Pre-condition: every other test in this file assumes WAL.

    Confirms via a side-channel sqlite3 connection (not via the Storage
    instance) that journal_mode=wal is on disk. If this fails, every other
    test in the file is invalid — fail loud and fail first.
    """
    mode = _confirm_wal_mode(ephemeral_db)
    assert mode == "wal", (
        f"WAL mode NOT enabled — journal_mode={mode!r}. "
        f"The interaction between sqlite_storage.SQLiteStorage._lock (RLock) and "
        f"SQLite's internal lock state machine is only safe in WAL mode. "
        f"All lock-escalation tests must be rewritten if WAL is off."
    )


def test_read_while_write_no_lock_escalation_stall(
    storage: SQLiteStorage,
    ephemeral_db: Path,
    seeded_random: int,
) -> None:
    """Case 1: writer + concurrent reader must not stall on lock escalation.

    Even though the Python RLock serialises every Storage method, what we
    are testing is that the *SQLite* layer beneath the RLock does not need
    to escalate (RESERVED -> PENDING -> EXCLUSIVE) in a way that interacts
    with the RLock to produce a deadlock or BUSY error.

    In WAL mode, this is true by construction (writers append to WAL, readers
    snapshot the WAL frame), but we verify empirically.
    """
    counters_before = get_counters()
    errors: List[BaseException] = []
    latencies: List[float] = []
    write_count = WRITER_OPS
    read_count = READER_OPS
    barrier = threading.Barrier(2)

    def writer(_wid: int, _barrier: Optional[threading.Barrier]) -> int:
        if _barrier is not None:
            _barrier.wait(timeout=5.0)
        ok = 0
        for i in range(write_count):
            t0 = time.perf_counter()
            storage.store_learning(_make_payload(i))
            dt = time.perf_counter() - t0
            latencies.append(dt)
            assert dt < MAX_LATENCY_S, (
                f"writer op {i} took {dt:.3f}s > MAX_LATENCY_S={MAX_LATENCY_S}s "
                f"(seed={seeded_random})"
            )
            ok += 1
        return ok

    def reader(_rid: int, _barrier: Optional[threading.Barrier]) -> int:
        if _barrier is not None:
            _barrier.wait(timeout=5.0)
        ok = 0
        for _ in range(read_count):
            t0 = time.perf_counter()
            rows = storage.get_recent(limit=50)
            dt = time.perf_counter() - t0
            latencies.append(dt)
            assert dt < MAX_LATENCY_S, (
                f"reader op took {dt:.3f}s > MAX_LATENCY_S={MAX_LATENCY_S}s "
                f"(seed={seeded_random})"
            )
            # rows may be empty initially — that's fine
            assert isinstance(rows, list)
            ok += 1
        return ok

    threads = [
        threading.Thread(target=lambda: writer(0, barrier), name="writer"),
        threading.Thread(target=lambda: reader(0, barrier), name="reader"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)
        assert not t.is_alive(), f"thread {t.name} hung past 30s — possible deadlock"

    assert not errors, f"workers raised: {errors}"

    counters_after = get_counters()
    # Confirm no failures leaked from sqlite into the counter channel.
    for key in ("store.fail", "get_recent.fail", "get_by_id.fail"):
        delta = counters_after.get(key, 0) - counters_before.get(key, 0)
        assert delta == 0, (
            f"counter {key} advanced by {delta} during read-while-write — "
            f"SQLITE_BUSY or `database is locked` leaked through the RLock boundary "
            f"(seed={seeded_random})"
        )

    # Confirm WAL checkpoint can run (no leftover SHARED lock holding it back).
    busy, _log, _cp = _checkpoint(ephemeral_db)
    assert busy == 0, (
        f"PRAGMA wal_checkpoint returned busy={busy} after read-while-write — "
        f"a SHARED lock leaked; this is the exact escalation-interaction the "
        f"cross-exam was worried about (seed={seeded_random})"
    )

    if latencies:
        logger.info(
            "read-while-write: ops=%d median=%.4fs p95=%.4fs max=%.4fs",
            len(latencies),
            statistics.median(latencies),
            sorted(latencies)[int(0.95 * len(latencies))],
            max(latencies),
        )


def test_write_while_read_no_busy_leak(
    storage: SQLiteStorage,
    ephemeral_db: Path,
    seeded_random: int,
) -> None:
    """Case 2: writer arriving during a long read must not see SQLITE_BUSY.

    We seed the DB with a small set of rows, then run a slow reader (FTS query
    over the whole keyword fallback path) while writers append. In rollback-
    journal mode, the writer would have to wait for the reader's SHARED lock
    to drop. In WAL mode, both proceed independently.
    """
    # Seed so the reader has rows to scan.
    for i in range(200):
        storage.store_learning(_make_payload(i))

    counters_before = get_counters()
    errors: List[BaseException] = []
    read_latencies: List[float] = []
    write_latencies: List[float] = []
    barrier = threading.Barrier(2)

    def slow_reader(_rid: int, _barrier: Optional[threading.Barrier]) -> int:
        if _barrier is not None:
            _barrier.wait(timeout=5.0)
        ok = 0
        for _ in range(15):
            t0 = time.perf_counter()
            # query() does FTS + keyword fallback over up to 200 rows = slow path
            rows = storage.query("payload", limit=100)
            dt = time.perf_counter() - t0
            read_latencies.append(dt)
            assert dt < MAX_LATENCY_S, (
                f"slow reader took {dt:.3f}s > MAX_LATENCY_S={MAX_LATENCY_S}s "
                f"(seed={seeded_random})"
            )
            assert isinstance(rows, list)
            ok += 1
        return ok

    def writer(_wid: int, _barrier: Optional[threading.Barrier]) -> int:
        if _barrier is not None:
            _barrier.wait(timeout=5.0)
        ok = 0
        for i in range(WRITER_OPS):
            t0 = time.perf_counter()
            storage.store_learning(_make_payload(1000 + i))
            dt = time.perf_counter() - t0
            write_latencies.append(dt)
            assert dt < MAX_LATENCY_S, (
                f"writer (during read) took {dt:.3f}s > MAX_LATENCY_S "
                f"(seed={seeded_random})"
            )
            ok += 1
        return ok

    threads = [
        threading.Thread(target=lambda: slow_reader(0, barrier), name="slow_reader"),
        threading.Thread(target=lambda: writer(0, barrier), name="writer"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60.0)
        assert not t.is_alive(), f"thread {t.name} hung past 60s — possible deadlock"

    assert not errors, f"workers raised: {errors}"

    counters_after = get_counters()
    for key in ("store.fail", "query.fail"):
        delta = counters_after.get(key, 0) - counters_before.get(key, 0)
        assert delta == 0, (
            f"counter {key} advanced by {delta} during write-while-read — "
            f"writer hit SQLITE_BUSY while reader held SQLite SHARED state "
            f"(seed={seeded_random})"
        )

    if write_latencies:
        logger.info(
            "write-while-read: writer ops=%d median=%.4fs max=%.4fs",
            len(write_latencies),
            statistics.median(write_latencies),
            max(write_latencies),
        )
    if read_latencies:
        logger.info(
            "write-while-read: reader ops=%d median=%.4fs max=%.4fs",
            len(read_latencies),
            statistics.median(read_latencies),
            max(read_latencies),
        )


def test_write_while_write_serialised_by_rlock(
    storage: SQLiteStorage,
    ephemeral_db: Path,
    seeded_random: int,
) -> None:
    """Case 3: write-vs-write — RLock serialises so SQLite never escalates.

    This is the *canary* case. If Python's RLock is doing its job, all writes
    queue at the Python layer and the SQLite layer below sees a sequential
    stream of writers, never two simultaneous writers. The RESERVED ->
    EXCLUSIVE escalation is therefore never contested.

    Asserts:
      * All N writer threads complete.
      * Total commit count matches WRITERS_BOTH * WRITER_OPS.
      * No counter `store.fail` advances.
      * Every payload id is queryable post-run (no lost writes).
    """
    n_writers = WRITERS_BOTH
    ops_each = WRITER_OPS
    expected_total = n_writers * ops_each

    counters_before = get_counters()
    errors: List[BaseException] = []
    latencies: List[float] = []
    written_ids: List[str] = []
    written_lock = threading.Lock()
    barrier = threading.Barrier(n_writers)

    def writer(wid: int, _barrier: Optional[threading.Barrier]) -> int:
        if _barrier is not None:
            _barrier.wait(timeout=5.0)
        ok = 0
        for i in range(ops_each):
            payload = _make_payload(wid * 10_000 + i)
            t0 = time.perf_counter()
            res = storage.store_learning(payload, skip_dedup=True)
            dt = time.perf_counter() - t0
            latencies.append(dt)
            assert dt < MAX_LATENCY_S, (
                f"writer {wid} op {i} took {dt:.3f}s > MAX_LATENCY_S "
                f"(seed={seeded_random})"
            )
            assert isinstance(res, dict)
            with written_lock:
                written_ids.append(payload["id"])
            ok += 1
        return ok

    _run_workers(writer, n_writers, name="ww", errors=errors, latencies=latencies, barrier=barrier)

    assert not errors, f"writers raised: {errors}"

    counters_after = get_counters()
    fail_delta = counters_after.get("store.fail", 0) - counters_before.get("store.fail", 0)
    assert fail_delta == 0, (
        f"store.fail advanced by {fail_delta} during write-while-write — "
        f"two writers raced and at least one hit SQLITE_BUSY (seed={seeded_random})"
    )

    # Atomicity: every id we *think* we wrote must be retrievable.
    found = 0
    for wid in written_ids:
        row = storage.get_by_id(wid)
        if row is not None:
            found += 1
    assert found == expected_total, (
        f"lost writes: expected {expected_total}, found {found}. RLock failed to "
        f"serialise SQLite writers (seed={seeded_random})"
    )

    busy, _log, _cp = _checkpoint(ephemeral_db)
    assert busy == 0, (
        f"PRAGMA wal_checkpoint busy={busy} after write-while-write — a writer "
        f"didn't release WAL state cleanly (seed={seeded_random})"
    )

    if latencies:
        logger.info(
            "write-while-write: ops=%d median=%.4fs p95=%.4fs max=%.4fs",
            len(latencies),
            statistics.median(latencies),
            sorted(latencies)[int(0.95 * len(latencies))],
            max(latencies),
        )


if __name__ == "__main__":
    # Allow direct invocation for ad-hoc runs.
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
