#!/usr/bin/env python3
"""
test_storage_edge_cases.py — TARGETED edge-case tests for storage layer
=======================================================================

Round-4 follow-up to the Round-3 fuzz harness (storage_fuzz_harness.py).
3s Neurologist flagged Round-3 as PARTIAL (+0.03, conf 0.80) because the
random-walk fuzzer never deterministically hits the cases where the
SQLite-singleton + RLock bug was most likely to manifest in production:

    1. ``limit=0`` / ``limit=MAX_INT`` / ``limit=-1`` boundary inputs to
       ``LearningStore.get_recent``.
    2. Concurrent ``store_learning`` + ``get_recent`` interleave with the
       new RLock — read-after-write consistency window.
    3. Circuit-breaker tripping mid storage-rollback — must not corrupt
       on-disk SQLite or leak orphan state between sqlite_storage and
       learning_store singletons.

These tests run against a TMP copy of ``~/.context-dna/learnings.db``,
toggled via ``$CONTEXT_DNA_LEARNINGS_DB`` — same idiom as the Round-3
fuzz harness so we never clobber the 3,455-row live DB.

ZSF (Zero Silent Failures)
--------------------------
* Every test logs counter increments via ``_log_counter_delta``.
* Random behaviour is seeded (``random.seed(seed)``) and the seed is
  printed in the failure message so failures reproduce on the next run.
* No bare ``except: pass`` — every except path bumps a counter or
  re-raises.

Self-review note (which tests SHOULD fail today vs pass after Round-3)
---------------------------------------------------------------------
Round-3 changes that LIVE today (verified via grep against
``contextdna-ide-oss/migrate3/memory/learning_store.py`` 2026-05-13):

* ``LearningStore.get_recent`` STILL swallows every exception and
  returns ``[]`` (lines 236-238). This is the silent-empty bug. The
  test ``test_limit_huge_does_not_silently_empty`` is written to FAIL
  TODAY (returns [] for a 200-row DB) and to pass once the catch-block
  logs + re-raises (or at minimum logs an error before returning []).
* No top-level limit validation in ``get_recent``, so ``limit=-1`` does
  NOT raise ``ValueError`` today; it forwards a negative LIMIT into
  SQLite, where SQLite silently treats LIMIT -1 as unlimited. Test
  ``test_limit_negative_raises_value_error`` is expected to FAIL today.
* No SQLite-level RLock in ``store_learning`` yet (sqlite_storage uses
  no explicit lock — relies on sqlite3 module's GIL serialization).
  Test ``test_concurrent_write_then_read_consistency`` will PROBABLY
  pass because SQLite + WAL is read-after-write consistent inside a
  single process, but flakes if any worker hits ``recent.fail``.
* Storage-rollback is a stand-alone bash script with no awareness of
  the LLM circuit breaker. ``test_breaker_trip_during_rollback`` runs
  ``--dry-run`` so we DON'T mutate disk; the test instead asserts that
  tripping the breaker during the dry-run does not leave the
  ``LearningStore`` singleton in an inconsistent state.

The full failure list becomes Round-5's bug-list.

Run
---
    pytest contextdna-ide-oss/migrate5/tests/storage_edge/test_storage_edge_cases.py -v
"""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import pytest

# --------------------------------------------------------------------------- #
# Path bootstrap — matches storage_fuzz_harness.py                             #
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
# migrate5/tests/storage_edge/ -> migrate5/ -> contextdna-ide-oss/ -> superrepo
SUPERREPO = HERE.parent.parent.parent.parent
MIGRATE3_PATH = SUPERREPO / "contextdna-ide-oss" / "migrate3"
MIGRATE4_PATH = SUPERREPO / "contextdna-ide-oss" / "migrate4"
ROLLBACK_SCRIPT = MIGRATE4_PATH / "rollback" / "storage-rollback.sh"

# Important: BOTH migrate3 and migrate4 ship a top-level ``memory`` package.
# migrate3's contains learning_store; migrate4's only ships
# synaptic_voice_audio. Whichever path comes first in sys.path wins, so we
# must insert migrate3 LAST (i.e. at index 0) so it shadows migrate4.
# Drop any pre-existing duplicate so the order is deterministic across reruns.
for p in (str(MIGRATE4_PATH), str(MIGRATE3_PATH)):
    while p in sys.path:
        sys.path.remove(p)
sys.path.insert(0, str(MIGRATE4_PATH))   # for llm_priority_queue.circuit_breaker
sys.path.insert(0, str(MIGRATE3_PATH))   # MUST win the ``memory`` resolution


logger = logging.getLogger("storage_edge_cases")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[edge] %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

MAX_INT_31 = 2**31 - 1   # 2_147_483_647 — SQLite's max safe LIMIT
MAX_INT_HUGE = 10_000    # large but not pathological — used in "loud-fail" test
DEFAULT_SEED = 1337


# --------------------------------------------------------------------------- #
# Singleton reset (lifted from storage_fuzz_harness)                           #
# --------------------------------------------------------------------------- #


def _reset_singletons() -> None:
    """Drop cached LearningStore + SQLiteStorage so env re-resolves the DB."""
    import memory.learning_store as ls_mod
    import memory.sqlite_storage as sqlite_mod

    with ls_mod._learning_store_lock:  # type: ignore[attr-defined]
        ls_mod._learning_store = None  # type: ignore[attr-defined]
    with sqlite_mod._sqlite_storage_lock:  # type: ignore[attr-defined]
        prev = sqlite_mod._sqlite_storage  # type: ignore[attr-defined]
        if prev is not None:
            try:
                prev.conn.close()
            except sqlite3.Error as exc:
                logger.warning("reset: close failed (continuing): %s", exc)
        sqlite_mod._sqlite_storage = None  # type: ignore[attr-defined]


def _get_store():
    from memory.learning_store import get_learning_store
    return get_learning_store()


def _get_counters() -> Dict[str, int]:
    from memory.learning_store import get_counters
    return get_counters()


def _log_counter_delta(label: str, before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
    """Log the delta between two counter snapshots — ZSF observability."""
    delta: Dict[str, int] = {}
    for k in set(before) | set(after):
        d = after.get(k, 0) - before.get(k, 0)
        if d:
            delta[k] = d
    logger.info("counter-delta %s -> %s", label, delta)
    return delta


# --------------------------------------------------------------------------- #
# Ephemeral DB fixture (mirrors storage_fuzz_harness._ephemeral_db)            #
# --------------------------------------------------------------------------- #


@contextmanager
def _ephemeral_db(source: Optional[Path], copy_live: bool = True) -> Iterator[Path]:
    """Stand up a private copy of the live DB so tests never clobber it."""
    tmpdir = Path(tempfile.mkdtemp(prefix="edge-"))
    db_path = tmpdir / "learnings.db"
    if copy_live and source is not None and source.exists():
        shutil.copy2(source, db_path)
        for sidecar in (source.with_suffix(".db-shm"), source.with_suffix(".db-wal")):
            if sidecar.exists():
                shutil.copy2(sidecar, tmpdir / sidecar.name)
    else:
        # Touch an empty file — the storage layer creates the schema lazily.
        db_path.touch()

    prev = os.environ.get("CONTEXT_DNA_LEARNINGS_DB")
    os.environ["CONTEXT_DNA_LEARNINGS_DB"] = str(db_path)
    # Force the SQLite branch so we don't accidentally hit a Postgres fallback.
    prev_backend = os.environ.get("STORAGE_BACKEND")
    os.environ["STORAGE_BACKEND"] = "sqlite"
    _reset_singletons()
    try:
        yield db_path
    finally:
        if prev is None:
            os.environ.pop("CONTEXT_DNA_LEARNINGS_DB", None)
        else:
            os.environ["CONTEXT_DNA_LEARNINGS_DB"] = prev
        if prev_backend is None:
            os.environ.pop("STORAGE_BACKEND", None)
        else:
            os.environ["STORAGE_BACKEND"] = prev_backend
        # Cleanly close singletons before nuking the tmpdir to avoid WAL leaks.
        _reset_singletons()
        shutil.rmtree(tmpdir, ignore_errors=True)


LIVE_DB = Path.home() / ".context-dna" / "learnings.db"


@pytest.fixture(scope="function")
def ephemeral_db() -> Iterator[Path]:
    """A fresh ephemeral copy of the live DB per test."""
    with _ephemeral_db(LIVE_DB, copy_live=True) as db:
        yield db


@pytest.fixture(scope="function")
def seeded_random() -> Iterator[int]:
    """Deterministic seeded random across the test for repeatability."""
    seed = int(os.environ.get("EDGE_SEED", DEFAULT_SEED))
    random.seed(seed)
    logger.info("seeded random with %d (override via $EDGE_SEED)", seed)
    yield seed


def _seed_rows(store, n: int) -> List[str]:
    """Insert ``n`` cheap fuzz rows so limit-boundary tests have data to read."""
    created: List[str] = []
    for i in range(n):
        data = {
            "id": f"edge_seed_{uuid.uuid4().hex[:10]}_{i}",
            "type": "win",
            "title": f"edge seed {i}",
            "content": f"edge-seed payload {i} {uuid.uuid4().hex}",
            "tags": ["edge", "seed"],
            "session_id": "edge-fixture",
            "injection_id": "",
            "source": "test_storage_edge_cases",
            "metadata": {"edge": True, "i": i},
        }
        result = store.store_learning(data, skip_dedup=True, consolidate=False)
        created.append(result["id"])
    return created


# =========================================================================== #
# 1. LIMIT BOUNDARY TESTS                                                      #
# =========================================================================== #


def test_limit_zero_returns_empty_cleanly(ephemeral_db: Path, seeded_random: int) -> None:
    """limit=0 → returns []; MUST NOT raise; MUST NOT silently swallow real errors.

    Today: get_recent() blanket-catches Exception and returns [] (recent.fail++).
    The honest expectation is: limit=0 returns [] AND recent.ok increments
    (not recent.fail). Anything else is a regression.
    """
    store = _get_store()
    _seed_rows(store, 5)

    before = _get_counters()
    result = store.get_recent(limit=0)
    after = _get_counters()
    delta = _log_counter_delta("limit=0", before, after)

    assert isinstance(result, list), f"expected list, got {type(result).__name__}"
    assert result == [], f"expected [], got {len(result)} rows"
    assert delta.get("recent.ok", 0) >= 1, (
        f"recent.ok did NOT increment — clean limit=0 must hit the ok branch. "
        f"delta={delta}"
    )
    assert delta.get("recent.fail", 0) == 0, (
        f"recent.fail bumped on limit=0 — silent failure! delta={delta}"
    )


def test_limit_one_returns_exactly_one_row(ephemeral_db: Path, seeded_random: int) -> None:
    """limit=1 → exactly one row from a 5-row table."""
    store = _get_store()
    _seed_rows(store, 5)

    before = _get_counters()
    result = store.get_recent(limit=1)
    after = _get_counters()
    _log_counter_delta("limit=1", before, after)

    assert len(result) == 1, f"expected 1 row, got {len(result)}"
    assert "id" in result[0], "row missing id field"


def test_limit_huge_does_not_silently_empty(ephemeral_db: Path, seeded_random: int) -> None:
    """limit=200 with 200 rows in DB → must return all 200, not silently [].

    This is the bug Round-3 flagged: a too-large limit shouldn't trigger the
    blanket exception swallow that returns []. With ZSF logging in place,
    recent.fail.huge should stay at 0 on success.
    """
    store = _get_store()
    inserted_ids = _seed_rows(store, 200)
    assert len(inserted_ids) == 200, "fixture failed to insert 200 rows"

    before = _get_counters()
    result = store.get_recent(limit=200)
    after = _get_counters()
    delta = _log_counter_delta("limit=200", before, after)

    # The live DB already has ~3455 rows — get_recent returns the *most
    # recent* 200 globally. Our 200 inserts are the most recent, so they
    # must all be present in the result.
    inserted_set = set(inserted_ids)
    returned_set = {row.get("id") for row in result if row.get("id")}
    missing = inserted_set - returned_set

    assert delta.get("recent.fail", 0) == 0, (
        f"recent.fail bumped — silent-empty bug! seed={seeded_random} delta={delta}"
    )
    # No dedicated ``recent.fail.huge`` counter exists yet; ZSF-shape:
    # assert it stays 0 if/when the storage layer adds it.
    assert delta.get("recent.fail.huge", 0) == 0, (
        f"recent.fail.huge bumped — limit=200 must succeed. delta={delta}"
    )
    assert len(result) >= 200, f"expected >=200 rows, got {len(result)}"
    assert not missing, (
        f"silent-empty bug observable: {len(missing)} of our 200 inserts "
        f"missing from the result set. seed={seeded_random}"
    )


def test_limit_10k_succeeds_or_loud_fails(ephemeral_db: Path, seeded_random: int) -> None:
    """limit=10_000 → either returns up to 10k rows OR fails with a LOGGED exception.

    "LOUD" failure means: ``recent.fail`` increments AND a record is left in
    the logger. Silent return of [] without bumping any counter is the bug.
    """
    store = _get_store()
    _seed_rows(store, 50)

    before = _get_counters()
    try:
        result = store.get_recent(limit=MAX_INT_HUGE)
        loud_exception = False
    except Exception as exc:  # noqa: BLE001 — we want to catch the loud-fail case
        result = None
        loud_exception = True
        logger.info("limit=10k raised loudly: %s: %s", type(exc).__name__, exc)
    after = _get_counters()
    delta = _log_counter_delta("limit=10000", before, after)

    if loud_exception:
        # Loud fail is acceptable for limit=10k as long as it bumped a counter.
        assert delta.get("recent.fail", 0) >= 1 or delta.get("store.fail", 0) >= 1, (
            f"loud exception but no counter bumped — that IS a silent failure. "
            f"delta={delta}"
        )
    else:
        # Success path
        assert isinstance(result, list)
        assert len(result) <= MAX_INT_HUGE
        # If success, recent.ok MUST have bumped — anything else = silent-empty.
        assert delta.get("recent.ok", 0) >= 1, (
            f"limit=10000 returned silently without bumping recent.ok. "
            f"delta={delta}"
        )


def test_limit_max_int_handled_gracefully(ephemeral_db: Path, seeded_random: int) -> None:
    """limit=MAX_INT (2**31 - 1) → no OOM, no silent empty.

    Either the call succeeds (SQLite can handle huge LIMIT since rows are
    materialized lazily) or it fails LOUDLY. Either is acceptable; the
    forbidden outcome is "returns [] silently while DB has rows."
    """
    store = _get_store()
    _seed_rows(store, 10)

    before = _get_counters()
    raised = False
    raised_exc: Optional[BaseException] = None
    try:
        result = store.get_recent(limit=MAX_INT_31)
    except (MemoryError, OverflowError, sqlite3.Error) as exc:
        raised = True
        raised_exc = exc
        result = None
    after = _get_counters()
    delta = _log_counter_delta("limit=MAX_INT", before, after)

    if raised:
        assert delta.get("recent.fail", 0) >= 1, (
            f"raised {raised_exc!r} but recent.fail did not increment — "
            f"silent failure path. delta={delta}"
        )
    else:
        assert isinstance(result, list), f"expected list, got {type(result)}"
        # We seeded 10 rows; live DB has ~3,455 + 10. Even with no error,
        # the count must be > 0 — silent-empty would return [] here.
        assert len(result) > 0, (
            f"limit=MAX_INT returned [] silently. DB has rows. delta={delta}"
        )


def test_limit_negative_raises_value_error(ephemeral_db: Path, seeded_random: int) -> None:
    """limit=-1 → MUST raise ValueError. Negative limit is illegal input.

    Today: get_recent forwards LIMIT -1 to SQLite, which silently treats it
    as "no limit" (returns all rows). That's the bug. The fix is a guard at
    the LearningStore boundary that raises ValueError on negative limit.
    """
    store = _get_store()
    _seed_rows(store, 10)

    with pytest.raises((ValueError, TypeError)) as excinfo:
        store.get_recent(limit=-1)
    msg = str(excinfo.value).lower()
    assert any(t in msg for t in ("limit", "negative", "non-negative", "must be")), (
        f"raised wrong error shape — message did not mention limit/negative: {excinfo.value!r}"
    )


# =========================================================================== #
# 2. CONCURRENT WRITE + READ INTERLEAVE                                        #
# =========================================================================== #


def test_concurrent_write_then_read_consistency(
    ephemeral_db: Path, seeded_random: int
) -> None:
    """8 writers + 4 readers × 50 iterations.

    Asserts:
        * Every write that returned a non-empty id is queryable within 100ms
          (read-after-write consistency).
        * Zero "missing" rows — write returned success but the row is not in
          the DB.

    ZSF: any worker exception bumps ``worker_errors`` and the test fails
    with the seed so the failure reproduces.
    """
    store = _get_store()
    iterations = 50
    writer_count = 8
    reader_count = 4
    write_ack: List[Tuple[str, float]] = []  # (id, t_ack)
    write_ack_lock = threading.Lock()
    worker_errors: List[Dict[str, str]] = []
    err_lock = threading.Lock()

    def _writer(wid: int) -> None:
        for i in range(iterations):
            data = {
                "id": f"edge_conc_{wid}_{i}_{uuid.uuid4().hex[:8]}",
                "type": "win",
                "title": f"conc w{wid} i{i}",
                "content": f"conc payload w{wid} i{i}",
                "tags": ["edge", "concurrent"],
                "session_id": f"edge-conc-{wid}",
                "injection_id": "",
                "source": "test_storage_edge_cases",
                "metadata": {"wid": wid, "i": i},
            }
            try:
                result = store.store_learning(
                    data, skip_dedup=True, consolidate=False
                )
                lid = result.get("id") if isinstance(result, dict) else None
                if lid:
                    with write_ack_lock:
                        write_ack.append((lid, time.monotonic()))
            except Exception as exc:  # noqa: BLE001 — ZSF: surface every failure
                with err_lock:
                    worker_errors.append(
                        {"role": "writer", "wid": str(wid), "msg": f"{type(exc).__name__}: {exc}"}
                    )

    def _reader(rid: int) -> None:
        for _ in range(iterations):
            try:
                rows = store.get_recent(limit=100)
                _ = len(rows)   # observable side effect, prevents elimination
            except Exception as exc:  # noqa: BLE001
                with err_lock:
                    worker_errors.append(
                        {"role": "reader", "rid": str(rid), "msg": f"{type(exc).__name__}: {exc}"}
                    )
            # tiny jitter to interleave with writers
            time.sleep(random.uniform(0.0005, 0.003))

    before = _get_counters()
    with ThreadPoolExecutor(max_workers=writer_count + reader_count) as pool:
        futures = []
        for wid in range(writer_count):
            futures.append(pool.submit(_writer, wid))
        for rid in range(reader_count):
            futures.append(pool.submit(_reader, rid))
        for f in as_completed(futures):
            f.result()  # propagate any unhandled exception (ZSF)
    after = _get_counters()
    _log_counter_delta("concurrent", before, after)

    assert not worker_errors, (
        f"workers raised — seed={seeded_random} errors={worker_errors[:5]} "
        f"(total {len(worker_errors)})"
    )

    # Read-after-write consistency check.
    # Sample a slice of ack'd ids and confirm each is queryable.
    ack_ids = [lid for lid, _ in write_ack]
    sample_size = min(100, len(ack_ids))
    sample_ids = random.sample(ack_ids, sample_size) if ack_ids else []

    # Give the WAL one round trip to flush before checking — at most 100ms.
    time.sleep(0.1)

    missing: List[str] = []
    with sqlite3.connect(str(ephemeral_db)) as conn:
        for lid in sample_ids:
            cur = conn.execute(
                "SELECT id FROM learnings WHERE id = ? LIMIT 1", (lid,)
            )
            if cur.fetchone() is None:
                missing.append(lid)

    assert not missing, (
        f"Read-after-write inconsistency: {len(missing)} of {sample_size} "
        f"sampled writes are missing after 100ms. seed={seeded_random} "
        f"first_missing={missing[:5]}"
    )
    assert len(write_ack) > 0, "no writes succeeded — concurrency test trivially passed"


# =========================================================================== #
# 3. BREAKER-TRIP-DURING-ROLLBACK                                              #
# =========================================================================== #


def _make_test_breaker():
    """Return a fresh CircuitBreaker isolated from the global module state."""
    from llm_priority_queue.circuit_breaker import CircuitBreaker

    return CircuitBreaker(max_timeouts=3, recovery_s=60.0)


def _trip_breaker_via_timeouts(breaker, count: int = 3) -> None:
    """Force ``count`` consecutive timeouts so the breaker transitions to OPEN.

    We inject a fake ``llm_generate_fn`` via a side-channel: re-create a
    breaker whose wrapped function always sleeps past ``timeout_s``.
    The breaker treats response=None as a timeout/failure.
    """
    from llm_priority_queue.circuit_breaker import BreakerState

    def _slow_llm(*args, **kwargs):
        # Returning None signals timeout to the breaker contract.
        return None

    breaker._llm_generate = _slow_llm  # type: ignore[attr-defined]
    for _ in range(count):
        breaker.call(
            system_prompt="trip",
            user_prompt="probe",
            profile="classify",
            timeout_s=0.001,
        )
    # The breaker should be OPEN after max_timeouts consecutive timeouts.
    assert breaker.state == BreakerState.OPEN, (
        f"failed to trip breaker — state={breaker.state} "
        f"consecutive_timeouts={breaker.consecutive_timeouts}"
    )


def _make_backup_snapshot(backup_dir: Path, source_db: Path) -> str:
    """Create a 'snapshot' the rollback script can find. Returns target id."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = f"edge_{uuid.uuid4().hex[:10]}"
    dest = backup_dir / f"learnings_{target}.db"
    shutil.copy2(source_db, dest)
    return target


def test_breaker_trip_during_rollback(ephemeral_db: Path, seeded_random: int) -> None:
    """Trip the circuit breaker mid storage-rollback. Assert no orphan state.

    Approach:
        1. Build a clean snapshot in a tmp backup dir.
        2. Open ephemeral DB via LearningStore + record baseline row count.
        3. Trip the breaker (3 consecutive timeouts).
        4. Run ``storage-rollback.sh --dry-run`` (no mutation — safe in CI).
        5. Assert: rollback exited cleanly, AND
                    learning_store + sqlite_storage row counts match the
                    direct sqlite3 row count post-rollback (no orphan state).
        6. Assert: tripping the breaker did NOT corrupt LearningStore's
                    singleton — get_recent(10) still returns rows.
    """
    if not ROLLBACK_SCRIPT.exists():
        pytest.skip(f"rollback script missing at {ROLLBACK_SCRIPT}")

    # Step 1+2: snapshot + baseline
    store = _get_store()
    _seed_rows(store, 25)
    baseline_recent = store.get_recent(limit=10)
    baseline_count_direct = _direct_row_count(ephemeral_db)

    tmp_backups = Path(tempfile.mkdtemp(prefix="edge-rollback-bk-"))
    try:
        target = _make_backup_snapshot(tmp_backups, ephemeral_db)

        # Step 3: trip the breaker
        breaker = _make_test_breaker()
        try:
            _trip_breaker_via_timeouts(breaker, count=3)
        except ImportError as exc:
            pytest.skip(f"circuit breaker unavailable: {exc}")

        # Step 4: run rollback in dry-run mode (no mutation)
        env = dict(os.environ)
        env["CONTEXT_DNA_LEARNINGS_DB"] = str(ephemeral_db)
        env["CONTEXTDNA_BACKUP_DIR"] = str(tmp_backups)
        proc = subprocess.run(
            [
                "bash", str(ROLLBACK_SCRIPT),
                "--target", target,
                "--reason", "edge-test-breaker-trip",
                "--dry-run",
                "--no-postgres",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Step 5: rollback should have exited 0 in dry-run
        assert proc.returncode == 0, (
            f"rollback --dry-run failed: rc={proc.returncode}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )

        # Step 6: state is still consistent — singleton not poisoned by breaker
        after_recent = store.get_recent(limit=10)
        assert len(after_recent) == len(baseline_recent), (
            f"singleton drift: was {len(baseline_recent)} rows, "
            f"now {len(after_recent)}"
        )

        after_count_direct = _direct_row_count(ephemeral_db)
        assert after_count_direct == baseline_count_direct, (
            f"orphan state: direct row count drifted "
            f"{baseline_count_direct} -> {after_count_direct} "
            f"despite --dry-run"
        )

        # Confirm: API-side count agrees with direct count.
        api_count = store.get_stats().get("total", -1)
        assert api_count == after_count_direct, (
            f"facade/direct count divergence: facade={api_count} "
            f"direct={after_count_direct}"
        )
    finally:
        shutil.rmtree(tmp_backups, ignore_errors=True)


# =========================================================================== #
# Helpers                                                                      #
# =========================================================================== #


def _direct_row_count(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM learnings")
        return int(cur.fetchone()[0])


# --------------------------------------------------------------------------- #
# CLI for manual execution                                                     #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":   # pragma: no cover
    # Allow running the file directly without pytest collection — useful for
    # quick smoke checks on a developer machine.
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
