#!/usr/bin/env python3
"""test_phase_rollback.py — Phase A -> Phase B -> rollback -> Phase A.

Round-5 deliverable for the lock-ordering invariant work. Simulates the
phased lock-granularity upgrade end-to-end:

    Phase A (single RLock, today)
        |
        v  upgrade
    Phase B (per-table TrackedRLock map, panic mode ON)
        |
        v  induce a deliberate order inversion
    Phase B detects + panics on the inverted thread
        |
        v  ROLLBACK
    Phase A (single RLock restored) — verify ledger consistency

Ledger consistency property: every successful `store_learning` call
recorded a row, and every row matches what the test inserted (no torn
rows, no duplicates, no lost writes). We assert this BEFORE the rollback
and AFTER the rollback — the row set MUST be byte-equal.

Counters asserted:
    deadlock_inversions_total >= 1     (Phase B detected the bad chain)
    deadlock_panics_total >= 1         (Phase B panicked the bad chain)
    sqlite_conn_misuse_total == 0      (no misuse occurred)

Run:
    python3 scripts/lock_audit/test_phase_rollback.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
BUILD_ROOT = HERE.parent.parent  # /tmp/cdna-mothership-build
sys.path.insert(0, str(BUILD_ROOT))
sys.path.insert(0, str(HERE))

from deadlock_detector import (  # noqa: E402
    LockOrderViolation,
    LockTracker,
    TrackedRLock,
    assert_conn_singleton,
    get_counters,
    register_lock_set,
    reset_counters,
    seed_canonical_order,
    set_panic_mode,
)


# --------------------------------------------------------------------------- #
# Phase-A & Phase-B storage shims (mirror the structure of SQLiteStorage)
# --------------------------------------------------------------------------- #


class PhaseAStorage:
    """Mirrors today's sqlite_storage.py: one RLock, one conn."""

    def __init__(self, db_path: Path) -> None:
        import sqlite3
        self.db_path = db_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn_singleton_ref = self.conn
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS ledger ("
            "id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )

    def write(self, n: int, payload: str) -> None:
        with self._lock:
            assert_conn_singleton(self.conn, self._conn_singleton_ref)
            self.conn.execute(
                "INSERT INTO ledger (id, payload) VALUES (?, ?)", (n, payload)
            )

    def read_all(self) -> List[Tuple[int, str]]:
        with self._lock:
            assert_conn_singleton(self.conn, self._conn_singleton_ref)
            return self.conn.execute(
                "SELECT id, payload FROM ledger ORDER BY id"
            ).fetchall()

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class PhaseBStorage:
    """Per-table lock map. Two tables, two locks: 'learnings', 'stats'.

    Canonical order: learnings -> stats (acquire learnings first).
    Phase B install registers locks AND seeds canonical order so the
    detector trips on day one, not after the first 'good' observation.
    """

    LOCK_NAMES = ("learnings", "stats")

    def __init__(self, db_path: Path) -> None:
        import sqlite3
        self.db_path = db_path
        self.conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn_singleton_ref = self.conn
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS ledger ("
            "id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self._locks: Dict[str, TrackedRLock] = {
            name: TrackedRLock(name) for name in self.LOCK_NAMES
        }
        register_lock_set(list(self.LOCK_NAMES))
        seed_canonical_order([("learnings", "stats")])

    def write(self, n: int, payload: str) -> None:
        # Phase B write: only touches learnings.
        with self._locks["learnings"]:
            assert_conn_singleton(self.conn, self._conn_singleton_ref)
            self.conn.execute(
                "INSERT INTO ledger (id, payload) VALUES (?, ?)", (n, payload)
            )

    def read_all(self) -> List[Tuple[int, str]]:
        # Phase B read: only touches learnings.
        with self._locks["learnings"]:
            assert_conn_singleton(self.conn, self._conn_singleton_ref)
            return self.conn.execute(
                "SELECT id, payload FROM ledger ORDER BY id"
            ).fetchall()

    def stats_then_learnings(self) -> None:
        """DELIBERATE INVERSION: takes stats first, then learnings.

        This is the chain Phase B must catch. Real `get_stats` should
        acquire learnings first, then stats. A buggy refactor that
        reverses the order is the canonical deadlock scenario.
        """
        with self._locks["stats"]:
            with self._locks["learnings"]:
                pass  # would do work here in a real method

    def learnings_then_stats(self) -> None:
        """Correct order — establishes the canonical pair under load."""
        with self._locks["learnings"]:
            with self._locks["stats"]:
                pass

    def close(self) -> None:
        self.conn.close()


# --------------------------------------------------------------------------- #
# Test orchestration
# --------------------------------------------------------------------------- #


def _seed_phase_a(storage: PhaseAStorage, rows: int = 50) -> List[Tuple[int, str]]:
    """Write N rows; return the expected ledger state."""
    expected: List[Tuple[int, str]] = []
    for i in range(rows):
        payload = f"row-{i:04d}"
        storage.write(i, payload)
        expected.append((i, payload))
    return expected


def _concurrent_writes(storage, ids: range, threads: int = 4) -> None:
    """Concurrent writers — sanity check that the lock actually serializes."""
    chunks = [list(ids)[t::threads] for t in range(threads)]
    errors: List[Exception] = []
    err_lock = threading.Lock()

    def worker(my_ids: List[int]) -> None:
        for n in my_ids:
            try:
                storage.write(n, f"row-{n:04d}")
            except Exception as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)

    workers = [threading.Thread(target=worker, args=(c,)) for c in chunks]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert not errors, f"unexpected write errors: {errors[:3]}"


def main() -> int:
    print("=" * 70)
    print("test_phase_rollback — Phase A -> B -> rollback -> A")
    print("=" * 70)

    reset_counters()
    set_panic_mode(False)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "phase.db"

        # ---- Phase A: single RLock, baseline write set -------------------- #
        print("\n[Phase A] open + write 50 rows + concurrent burst of 20 more")
        a = PhaseAStorage(db_path)
        try:
            expected = _seed_phase_a(a, rows=50)
            _concurrent_writes(a, range(50, 70), threads=4)
            expected.extend((i, f"row-{i:04d}") for i in range(50, 70))
            before = a.read_all()
            assert before == expected, (
                f"Phase A ledger mismatch: got {len(before)} rows, "
                f"expected {len(expected)}"
            )
            print(f"  Phase A ledger has {len(before)} rows (consistent)")
        finally:
            a.close()

        # ---- Phase B: per-table TrackedRLock, panic mode ON -------------- #
        print("\n[Phase B] upgrade lock granularity, panic mode ON")
        set_panic_mode(True)
        b = PhaseBStorage(db_path)
        try:
            # First, do correct-order acquisitions to seed canonical order.
            for _ in range(5):
                b.learnings_then_stats()

            # Concurrent writes through Phase B should still succeed.
            _concurrent_writes(b, range(70, 90), threads=4)
            expected.extend((i, f"row-{i:04d}") for i in range(70, 90))
            mid = b.read_all()
            assert mid == expected, (
                f"Phase B ledger mismatch: got {len(mid)} rows, "
                f"expected {len(expected)}"
            )
            print(f"  Phase B ledger has {len(mid)} rows (consistent)")

            # Now induce the inversion. Panic mode should raise.
            print("\n[Phase B] inducing deliberate stats->learnings inversion")
            panic_caught = False
            try:
                b.stats_then_learnings()
            except LockOrderViolation as exc:
                panic_caught = True
                print(f"  LockOrderViolation raised: {exc}")
            assert panic_caught, "panic mode failed to raise on inversion"

            # Verify counters reflect the detection + panic.
            snap = get_counters()
            assert snap.get("deadlock_inversions_total", 0) >= 1, (
                "expected at least one detected inversion"
            )
            assert snap.get("deadlock_panics_total", 0) >= 1, (
                "expected at least one panic"
            )
            assert snap.get("sqlite_conn_misuse_total", 0) == 0, (
                f"unexpected sqlite_conn_misuse: {snap['sqlite_conn_misuse_total']}"
            )
            print(f"  counters: inversions={snap['deadlock_inversions_total']} "
                  f"panics={snap['deadlock_panics_total']} "
                  f"misuse={snap['sqlite_conn_misuse_total']}")
        finally:
            b.close()
            set_panic_mode(False)

        # ---- Rollback: collapse back to Phase A --------------------------- #
        print("\n[Rollback] collapse Phase B -> Phase A (single RLock)")
        a2 = PhaseAStorage(db_path)
        try:
            after = a2.read_all()
            assert after == expected, (
                f"Rollback ledger mismatch: got {len(after)} rows, "
                f"expected {len(expected)}"
            )
            # Verify Phase A still works post-rollback.
            _concurrent_writes(a2, range(90, 100), threads=4)
            expected.extend((i, f"row-{i:04d}") for i in range(90, 100))
            final = a2.read_all()
            assert final == expected, (
                f"Post-rollback writes lost: got {len(final)}, "
                f"expected {len(expected)}"
            )
            print(f"  Post-rollback ledger has {len(final)} rows (consistent)")
        finally:
            a2.close()

    print("\n" + "=" * 70)
    print("PASS — Phase A -> B -> rollback -> A ledger consistent end-to-end")
    print("=" * 70)
    return 0


# --------------------------------------------------------------------------- #
# pytest-compatible smoke test
# --------------------------------------------------------------------------- #


def test_phase_rollback_smoke() -> None:
    rc = main()
    assert rc == 0


def test_locktracker_replay() -> None:
    """Offline LockTracker replay should flag the inversion deterministically."""
    reset_counters()
    set_panic_mode(False)
    register_lock_set(["learnings", "stats"])
    tracker = LockTracker([("learnings", "stats")])
    tracker.start()

    a = TrackedRLock("learnings")
    b = TrackedRLock("stats")

    # Correct order.
    with a:
        with b:
            tracker.record("learnings", "acquire")
            tracker.record("stats", "acquire")
            tracker.record("stats", "release")
            tracker.record("learnings", "release")

    # Now feed the tracker a wrong-order trace (without actually deadlocking).
    tracker.record("stats", "acquire")
    tracker.record("learnings", "acquire")  # violation
    tracker.record("learnings", "release")
    tracker.record("stats", "release")
    result = tracker.analyze()
    assert not result.ok, "tracker should have flagged the inversion"
    assert any(
        violation[1] == "stats" and violation[2] == "learnings"
        for violation in result.violations
    ), f"expected stats->learnings violation, got {result.violations}"
    tracker.stop()


def test_sqlite_conn_misuse_counter() -> None:
    """assert_conn_singleton bumps the counter and raises on mismatch."""
    from deadlock_detector import SQLiteMisuse
    reset_counters()
    sentinel = object()
    other = object()

    # Wrong identity.
    try:
        assert_conn_singleton(other, sentinel)
    except SQLiteMisuse:
        pass
    else:
        raise AssertionError("expected SQLiteMisuse on identity mismatch")

    # None conn.
    try:
        assert_conn_singleton(None, sentinel)
    except SQLiteMisuse:
        pass
    else:
        raise AssertionError("expected SQLiteMisuse on None conn")

    snap = get_counters()
    assert snap["sqlite_conn_misuse_total"] >= 2, (
        f"expected sqlite_conn_misuse_total >= 2, got "
        f"{snap['sqlite_conn_misuse_total']}"
    )


if __name__ == "__main__":
    try:
        rc = main()
        # Also run the unit tests in __main__.
        test_locktracker_replay()
        test_sqlite_conn_misuse_counter()
        print("\nLockTracker replay OK")
        print("sqlite_conn_misuse counter OK")
        sys.exit(rc)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
