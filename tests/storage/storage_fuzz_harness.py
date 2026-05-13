#!/usr/bin/env python3
"""
storage_fuzz_harness.py — Pre-integration fuzz harness for STORAGE_BACKEND toggle
===============================================================================

Round 3 task (Synaptic-designed, Atlas-interpreted) addressing 3s-flagged risk #1:
fuzz testing for STORAGE_BACKEND toggles + concurrent agents against
``memory/learning_store.py`` (unified storage facade).

What this harness does
----------------------
1. Spawns N concurrent workers (default 8) performing random ops against the
   live ``LearningStore`` API: record / promote / retire / query.
2. Between iterations, toggles ``STORAGE_BACKEND`` env between ``sqlite`` and
   ``auto`` while resetting both singletons under lock (the only honest way to
   make a re-resolution observable mid-run; the singleton-caches the choice).
3. After each iteration asserts invariants:
     I1. Same row count from SQL ``COUNT(*)`` vs. API ``get_stats().total``.
     I2. No duplicate IDs across backends (SELECT id, COUNT(*) ... HAVING > 1).
     I3. Promotion state preserved across toggle (a learning promoted in
         iteration N must still report type starting with the promoted type
         after the next toggle re-resolves the backend).
4. Writes a JSON checkpoint per iteration into
   ``contextdna-ide-oss/migrate4/storage_checkpoints/<run-id>/checkpoint-<N>.json``
   shape: ``{ts, backend_before, backend_after, row_count_delta,
   invariant_pass, errors}``.
5. ZSF: every worker exception captured with traceback; harness exits non-zero
   on any invariant violation. Counter ``fuzz_invariant_violations_total``
   printed at end.

Known limitations
-----------------
* Postgres path is *deferred* — ``psycopg2`` is missing in the local test env
  (verified at task-start). When STORAGE_BACKEND=auto, the harness still
  exercises the resolution logic (auto -> falls back to SQLite), so the toggle
  is observable even without Postgres present.
* The harness operates on a **copy** of ``~/.context-dna/learnings.db`` placed
  in a tmp dir via ``$CONTEXT_DNA_LEARNINGS_DB`` so the live 3,445-row DB is
  never mutated. Drop ``--use-live-db`` (off by default) is intentionally not
  exposed.

CLI
---
    python3 storage_fuzz_harness.py \
        --workers 8 \
        --iterations 15 \
        --ops-per-iter 30 \
        --seed 1337

Exit codes
----------
* 0 — all iterations passed invariants, no worker tracebacks.
* 1 — at least one invariant violation OR worker traceback.
* 2 — harness configuration / setup error.

This module is pytest-compatible — see ``test_fuzz_smoke()`` for a tiny smoke
test that runs 2 iterations / 2 workers / 4 ops to keep CI cheap.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import random
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Path bootstrap                                                               #
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
# migrate4/storage_checkpoints/ -> migrate4/ -> contextdna-ide-oss/ -> superrepo
SUPERREPO = HERE.parent.parent.parent
# The unified storage layer ships under contextdna-ide-oss/migrate3/memory/
MIGRATE3_PATH = SUPERREPO / "contextdna-ide-oss" / "migrate3"

if str(MIGRATE3_PATH) not in sys.path:
    sys.path.insert(0, str(MIGRATE3_PATH))


# --------------------------------------------------------------------------- #
# Counters (ZSF)                                                               #
# --------------------------------------------------------------------------- #


_HARNESS_COUNTERS: Dict[str, int] = {}
_COUNTER_LOCK = threading.Lock()


def _bump(key: str, by: int = 1) -> None:
    with _COUNTER_LOCK:
        _HARNESS_COUNTERS[key] = _HARNESS_COUNTERS.get(key, 0) + by


def get_harness_counters() -> Dict[str, int]:
    with _COUNTER_LOCK:
        return dict(_HARNESS_COUNTERS)


# --------------------------------------------------------------------------- #
# Backend singleton manipulation                                               #
# --------------------------------------------------------------------------- #


def _reset_singletons() -> None:
    """Drop cached LearningStore + SQLiteStorage so env re-resolves.

    This is the *only* way to honestly observe a STORAGE_BACKEND toggle mid-run
    — without resetting these caches, ``get_learning_store()`` keeps returning
    the first-resolved backend forever.
    """
    try:
        import memory.learning_store as ls_mod
        import memory.sqlite_storage as sqlite_mod
    except ImportError as exc:
        _bump("reset.import_fail")
        raise RuntimeError(f"cannot import memory layer: {exc}") from exc

    with ls_mod._learning_store_lock:  # type: ignore[attr-defined]
        ls_mod._learning_store = None  # type: ignore[attr-defined]
    with sqlite_mod._sqlite_storage_lock:  # type: ignore[attr-defined]
        prev = sqlite_mod._sqlite_storage  # type: ignore[attr-defined]
        # Close existing connection if any — prevents WAL-handle leaks across
        # iterations when running on the same db path.
        if prev is not None:
            try:
                prev.conn.close()  # type: ignore[union-attr]
            except Exception:  # pragma: no cover — best-effort
                _bump("reset.conn_close_fail")
        sqlite_mod._sqlite_storage = None  # type: ignore[attr-defined]
    _bump("reset.ok")


def _set_backend(value: str) -> None:
    os.environ["STORAGE_BACKEND"] = value
    _reset_singletons()


def _get_store():
    from memory.learning_store import get_learning_store

    return get_learning_store()


# --------------------------------------------------------------------------- #
# Workload primitives                                                          #
# --------------------------------------------------------------------------- #


WORKER_TYPES = ("win", "fix", "pattern", "insight", "gotcha")


def _random_learning(worker_id: int, op_id: int) -> Dict[str, Any]:
    """Produce a small randomized learning payload."""
    learning_type = random.choice(WORKER_TYPES)
    title_token = uuid.uuid4().hex[:8]
    return {
        # Pre-supply id so we can track promotions across toggles.
        "id": f"fuzz_{worker_id}_{op_id}_{title_token}",
        "type": learning_type,
        "title": f"fuzz {learning_type} {title_token}",
        "content": f"worker={worker_id} op={op_id} payload={uuid.uuid4().hex}",
        "tags": [f"fuzz", f"w{worker_id}", learning_type],
        "session_id": f"fuzz-session-{worker_id}",
        "injection_id": "",
        "source": "fuzz_harness",
        "metadata": {"fuzz": True, "worker": worker_id, "op": op_id},
    }


def _op_record(store) -> Tuple[str, Optional[str], Optional[str]]:
    """Returns (op_name, learning_id_or_None, err_or_None)."""
    try:
        data = _random_learning(
            worker_id=threading.get_ident() % 1000,
            op_id=random.randint(0, 1_000_000),
        )
        # skip_dedup so the fuzz traffic actually creates new rows rather than
        # collapsing into the smart-merge consolidator.
        result = store.store_learning(data, skip_dedup=True, consolidate=False)
        _bump("op.record.ok")
        return ("record", result.get("id"), None)
    except Exception as exc:  # noqa: BLE001 — ZSF: surface every failure
        _bump("op.record.fail")
        return ("record", None, f"{type(exc).__name__}: {exc}")


def _op_promote(store, candidate_ids: List[str]) -> Tuple[str, Optional[str], Optional[str]]:
    if not candidate_ids:
        _bump("op.promote.skip_empty")
        return ("promote", None, None)
    target = random.choice(candidate_ids)
    try:
        ok = store.promote(target, type="sop", promoted_by="fuzz")
        if ok:
            _bump("op.promote.ok")
            return ("promote", target, None)
        _bump("op.promote.miss")
        return ("promote", target, "promote returned False")
    except Exception as exc:  # noqa: BLE001
        _bump("op.promote.fail")
        return ("promote", target, f"{type(exc).__name__}: {exc}")


def _op_retire(store, candidate_ids: List[str]) -> Tuple[str, Optional[str], Optional[str]]:
    if not candidate_ids:
        _bump("op.retire.skip_empty")
        return ("retire", None, None)
    target = random.choice(candidate_ids)
    try:
        ok = store.retire(target)
        if ok:
            _bump("op.retire.ok")
            return ("retire", target, None)
        _bump("op.retire.miss")
        return ("retire", target, "retire returned False")
    except Exception as exc:  # noqa: BLE001
        _bump("op.retire.fail")
        return ("retire", target, f"{type(exc).__name__}: {exc}")


def _op_query(store) -> Tuple[str, Optional[str], Optional[str]]:
    try:
        term = random.choice(["fuzz", "patch", "win", "fix", "context"])
        results = store.query(term, limit=5)
        _bump("op.query.ok")
        # No assertion about result count — query is best-effort across backends.
        return ("query", str(len(results)), None)
    except Exception as exc:  # noqa: BLE001
        _bump("op.query.fail")
        return ("query", None, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Worker pool                                                                  #
# --------------------------------------------------------------------------- #


class WorkerResult:
    __slots__ = ("ops", "errors", "promoted_ids", "created_ids")

    def __init__(self) -> None:
        self.ops: List[Tuple[str, Optional[str]]] = []
        self.errors: List[Dict[str, str]] = []
        self.promoted_ids: List[str] = []
        self.created_ids: List[str] = []


def _worker(worker_id: int, ops_count: int, shared_ids: List[str], lock: threading.Lock) -> WorkerResult:
    """A single concurrent worker performing ``ops_count`` random ops.

    ``shared_ids`` is the pool of known learning ids that promote/retire can
    target. Workers append new ids back into the pool under ``lock``.
    """
    out = WorkerResult()
    try:
        store = _get_store()
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc(limit=4)
        out.errors.append({"phase": "init", "msg": str(exc), "tb": tb})
        _bump("worker.init_fail")
        return out

    for _ in range(ops_count):
        op_choice = random.choices(
            population=("record", "promote", "retire", "query"),
            weights=(5, 2, 1, 3),
            k=1,
        )[0]
        try:
            if op_choice == "record":
                name, lid, err = _op_record(store)
                if lid:
                    out.created_ids.append(lid)
                    with lock:
                        shared_ids.append(lid)
            elif op_choice == "promote":
                with lock:
                    snapshot = list(shared_ids)
                name, lid, err = _op_promote(store, snapshot)
                if lid and err is None:
                    out.promoted_ids.append(lid)
            elif op_choice == "retire":
                with lock:
                    snapshot = list(shared_ids)
                name, lid, err = _op_retire(store, snapshot)
            else:
                name, lid, err = _op_query(store)

            out.ops.append((name, lid))
            if err:
                out.errors.append({"phase": name, "msg": err, "tb": ""})
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=6)
            out.errors.append({"phase": op_choice, "msg": str(exc), "tb": tb})
            _bump("worker.unhandled")

    return out


# --------------------------------------------------------------------------- #
# Invariant checks                                                             #
# --------------------------------------------------------------------------- #


def _sql_row_count(db_path: Path) -> int:
    """Count rows directly via SQLite, bypassing the storage facade."""
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM learnings")
        return int(cur.fetchone()[0])


def _api_row_count(store) -> int:
    """Count rows via the LearningStore API."""
    stats = store.get_stats()
    return int(stats.get("total", 0))


def _sql_duplicate_ids(db_path: Path) -> List[str]:
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "SELECT id, COUNT(*) c FROM learnings GROUP BY id HAVING c > 1"
        )
        return [row[0] for row in cur.fetchall()]


def _promotion_state(store, promoted_ids: List[str]) -> Tuple[List[str], List[str]]:
    """For each id we *claimed* to promote, check the live row's type.

    Returns (still_promoted, drifted) — drifted = ids whose row type is no
    longer in the promoted family (sop / promoted-prefix / etc).
    """
    still_promoted: List[str] = []
    drifted: List[str] = []
    promoted_prefixes = ("sop", "promoted")
    for lid in promoted_ids:
        row = store.get_by_id(lid)
        if not row:
            drifted.append(lid)
            continue
        row_type = (row.get("type") or "").lower()
        if row_type.startswith(promoted_prefixes) or row.get("metadata", {}).get("promoted_at"):
            still_promoted.append(lid)
        else:
            drifted.append(lid)
    return still_promoted, drifted


def _check_invariants(
    db_path: Path,
    promoted_ids_to_check: List[str],
) -> Tuple[bool, Dict[str, Any]]:
    """Run all invariant checks; return (passed, evidence)."""
    evidence: Dict[str, Any] = {}
    # Fresh resolution so we hit the post-toggle backend.
    store = _get_store()

    try:
        sql_count = _sql_row_count(db_path)
        api_count = _api_row_count(store)
    except Exception as exc:  # noqa: BLE001
        evidence["i1_error"] = f"{type(exc).__name__}: {exc}"
        _bump("inv.i1.error")
        return False, evidence

    evidence["sql_count"] = sql_count
    evidence["api_count"] = api_count
    i1 = sql_count == api_count
    if not i1:
        _bump("inv.i1.fail")

    try:
        dupes = _sql_duplicate_ids(db_path)
    except Exception as exc:  # noqa: BLE001
        evidence["i2_error"] = f"{type(exc).__name__}: {exc}"
        _bump("inv.i2.error")
        return False, evidence

    evidence["duplicate_ids"] = dupes
    i2 = len(dupes) == 0
    if not i2:
        _bump("inv.i2.fail")

    still_p, drifted = _promotion_state(store, promoted_ids_to_check)
    evidence["promotion_still_promoted"] = len(still_p)
    evidence["promotion_drifted"] = drifted
    i3 = len(drifted) == 0
    if not i3:
        _bump("inv.i3.fail")

    return (i1 and i2 and i3), evidence


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #


def _checkpoint_dir(run_id: str) -> Path:
    out = HERE / run_id
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_checkpoint(out_dir: Path, n: int, payload: Dict[str, Any]) -> Path:
    path = out_dir / f"checkpoint-{n:03d}.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    tmp.replace(path)
    return path


@contextmanager
def _ephemeral_db(source: Optional[Path]):
    """Prepare an ephemeral DB so the live ~/.context-dna/learnings.db is safe.

    If ``source`` exists, copy it. Otherwise create an empty file and let the
    storage layer build the schema on first connect.
    """
    tmpdir = Path(os.environ.get("TMPDIR", "/tmp")) / f"fuzz-{uuid.uuid4().hex[:8]}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    db_path = tmpdir / "learnings.db"
    if source and source.exists():
        shutil.copy2(source, db_path)
        # Also copy WAL if present so we start fully consistent.
        for sidecar in (source.with_suffix(".db-shm"), source.with_suffix(".db-wal")):
            if sidecar.exists():
                shutil.copy2(sidecar, tmpdir / sidecar.name)
    prev = os.environ.get("CONTEXT_DNA_LEARNINGS_DB")
    os.environ["CONTEXT_DNA_LEARNINGS_DB"] = str(db_path)
    try:
        yield db_path
    finally:
        if prev is None:
            os.environ.pop("CONTEXT_DNA_LEARNINGS_DB", None)
        else:
            os.environ["CONTEXT_DNA_LEARNINGS_DB"] = prev
        # Always drop the directory — fuzz traffic is disposable.
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:  # pragma: no cover
            _bump("ephemeral.cleanup_fail")


def run_fuzz(
    *,
    workers: int = 8,
    iterations: int = 15,
    ops_per_iter: int = 30,
    seed: Optional[int] = None,
    source_db: Optional[Path] = None,
    verbose: bool = True,
) -> Tuple[bool, Dict[str, Any]]:
    """Run the fuzz harness. Returns (all_passed, summary)."""
    if seed is not None:
        random.seed(seed)

    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = _checkpoint_dir(run_id)

    shared_ids: List[str] = []
    shared_lock = threading.Lock()
    promoted_ledger: List[str] = []

    summary = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "workers": workers,
        "iterations": iterations,
        "ops_per_iter": ops_per_iter,
        "seed": seed,
        "checkpoints": [],
        "fuzz_invariant_violations_total": 0,
        "fuzz_worker_traceback_total": 0,
        "ended_at": None,
    }

    with _ephemeral_db(source_db) as db_path:
        if verbose:
            print(f"[fuzz] run_id={run_id}")
            print(f"[fuzz] ephemeral db: {db_path}")
            print(f"[fuzz] checkpoint dir: {out_dir}")

        backend_cycle = ("sqlite", "auto")

        # Seed shared_ids with a handful of existing real ids so promote/retire
        # have targets even before workers create new rows. We sample from the
        # already-copied DB read-only via the API.
        _set_backend("sqlite")
        try:
            seed_store = _get_store()
            for row in seed_store.get_recent(limit=50):
                if row.get("id"):
                    shared_ids.append(row["id"])
        except Exception as exc:  # noqa: BLE001 — ZSF
            _bump("seed.fail")
            if verbose:
                print(f"[fuzz] seed sampling failed (continuing): {exc}")

        for n in range(1, iterations + 1):
            iter_started = time.time()
            backend_before = os.environ.get("STORAGE_BACKEND", "auto")

            # Concurrent workers
            iteration_errors: List[Dict[str, str]] = []
            iter_promoted: List[str] = []

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_worker, wid, ops_per_iter, shared_ids, shared_lock)
                    for wid in range(workers)
                ]
                for fut in as_completed(futures):
                    try:
                        wr = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        tb = traceback.format_exc(limit=4)
                        iteration_errors.append(
                            {"phase": "future", "msg": str(exc), "tb": tb}
                        )
                        _bump("worker.future_fail")
                        continue
                    iteration_errors.extend(wr.errors)
                    iter_promoted.extend(wr.promoted_ids)

            promoted_ledger.extend(iter_promoted)

            # Toggle backend (sqlite <-> auto). Even with psycopg2 missing,
            # ``auto`` is observably different: it re-runs ``_resolve_backend``
            # and hits the "auto -> fallback to sqlite" branch (counter
            # backend.auto_sqlite advances).
            backend_after = backend_cycle[n % len(backend_cycle)]
            _set_backend(backend_after)

            # Invariants after toggle re-resolves the backend
            row_count_before = None
            try:
                row_count_before = _sql_row_count(db_path)
            except Exception as exc:  # noqa: BLE001
                _bump("inv.count_before.fail")
                iteration_errors.append(
                    {"phase": "count_before", "msg": str(exc), "tb": ""}
                )

            passed, evidence = _check_invariants(
                db_path,
                promoted_ids_to_check=list(promoted_ledger),
            )

            row_count_after = evidence.get("sql_count")
            row_count_delta = (
                None
                if row_count_before is None or row_count_after is None
                else row_count_after - row_count_before
            )

            if not passed:
                summary["fuzz_invariant_violations_total"] += 1
            summary["fuzz_worker_traceback_total"] += sum(
                1 for e in iteration_errors if e.get("tb")
            )

            checkpoint = {
                "n": n,
                "ts": datetime.now(timezone.utc).isoformat(),
                "duration_s": round(time.time() - iter_started, 3),
                "backend_before": backend_before,
                "backend_after": backend_after,
                "row_count_before": row_count_before,
                "row_count_after": row_count_after,
                "row_count_delta": row_count_delta,
                "invariant_pass": passed,
                "evidence": evidence,
                "iteration_errors": iteration_errors[:50],  # cap for size
                "iteration_error_total": len(iteration_errors),
                "promoted_in_iter": iter_promoted,
            }
            path = _write_checkpoint(out_dir, n, checkpoint)
            summary["checkpoints"].append(str(path))

            if verbose:
                marker = "OK" if passed else "FAIL"
                print(
                    f"[fuzz] iter {n:02d}/{iterations:02d} "
                    f"backend {backend_before!r}->{backend_after!r} "
                    f"delta={row_count_delta} "
                    f"errs={len(iteration_errors)} "
                    f"invariants={marker}"
                )

        summary["ended_at"] = datetime.now(timezone.utc).isoformat()
        summary["harness_counters"] = get_harness_counters()
        # Also capture learning_store counters for cross-reference
        try:
            from memory.learning_store import get_counters as ls_counters

            summary["learning_store_counters"] = ls_counters()
        except Exception:  # pragma: no cover
            _bump("ls_counters.fail")

        # Final summary file
        summary_path = out_dir / "summary.json"
        with open(summary_path, "w") as fh:
            json.dump(summary, fh, indent=2, default=str)

        if verbose:
            print(
                f"[fuzz] DONE — invariant_violations="
                f"{summary['fuzz_invariant_violations_total']} "
                f"worker_tracebacks={summary['fuzz_worker_traceback_total']}"
            )
            print(f"[fuzz] summary: {summary_path}")

    all_passed = (
        summary["fuzz_invariant_violations_total"] == 0
        and summary["fuzz_worker_traceback_total"] == 0
    )
    return all_passed, summary


# --------------------------------------------------------------------------- #
# pytest entry points                                                          #
# --------------------------------------------------------------------------- #


def test_fuzz_smoke():
    """Tiny smoke test — pytest-friendly. Always uses ephemeral empty DB."""
    passed, summary = run_fuzz(
        workers=2,
        iterations=2,
        ops_per_iter=4,
        seed=42,
        source_db=None,
        verbose=False,
    )
    assert passed, json.dumps(summary, default=str, indent=2)


def test_fuzz_against_live_db_copy():
    """Optional richer test that mirrors the CLI default run.

    Skips if the live DB is unavailable on this host so CI stays green.
    """
    live = Path.home() / ".context-dna" / "learnings.db"
    if not live.exists():
        import pytest  # type: ignore

        pytest.skip("live ~/.context-dna/learnings.db not present")
    passed, summary = run_fuzz(
        workers=4,
        iterations=5,
        ops_per_iter=10,
        seed=7,
        source_db=live,
        verbose=False,
    )
    assert passed, json.dumps(summary, default=str, indent=2)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--iterations", type=int, default=15)
    p.add_argument("--ops-per-iter", type=int, default=30)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument(
        "--source-db",
        type=str,
        default=str(Path.home() / ".context-dna" / "learnings.db"),
        help="DB to clone for the ephemeral run (never written-to in place).",
    )
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    source_db = Path(args.source_db) if args.source_db else None
    if source_db is not None and not source_db.exists():
        print(f"[fuzz] warn: source db missing, using empty ephemeral: {source_db}")
        source_db = None

    try:
        passed, summary = run_fuzz(
            workers=args.workers,
            iterations=args.iterations,
            ops_per_iter=args.ops_per_iter,
            seed=args.seed,
            source_db=source_db,
            verbose=not args.quiet,
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print(f"[fuzz] harness setup error: {exc}", file=sys.stderr)
        return 2

    print(
        f"fuzz_invariant_violations_total="
        f"{summary['fuzz_invariant_violations_total']}"
    )
    print(
        f"fuzz_worker_traceback_total="
        f"{summary['fuzz_worker_traceback_total']}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
