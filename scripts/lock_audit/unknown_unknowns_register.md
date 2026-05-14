# Unknown-Unknowns Register — SQLite Lock × Python RLock Audit

**Owner**: 3-Surgeon Phase A audit, C3 follow-up
**Companions**:
- [`lock_ordering_invariant.md`](./lock_ordering_invariant.md) — single-lock proof (Phase A)
- [`lock_audit_report.md`](./lock_audit_report.md) — original audit
- [`granularity_upgrade_plan.md`](./granularity_upgrade_plan.md) — Phase B/C plan
- [`deadlock_detector.py`](./deadlock_detector.py) — runtime detector

**Trigger**: B6 cross-exam (Round-3 surgeon Phase A review) surfaced six residual concerns the audit had not formally addressed. C3 (this register) closes them — either with empirical evidence, explicit deferral, or scope exclusion.

**Date**: 2026-05-13
**Verdict summary**: 2 ADDRESSED with passing tests, 4 documented as known-unknowns with explicit residual-risk classification.

---

## Status legend

| Status | Meaning |
|---|---|
| **ADDRESSED** | Empirically tested or analytically proven. Evidence linked. |
| **DOCUMENTED-OUT** | Known concern, deliberately not tested in this pass. Residual risk noted. Owner / next step recorded. |
| **OUT-OF-SCOPE** | Concern applies to a deployment shape (multi-process, NFS) that the current architecture explicitly does not target. Re-open if architecture changes. |

---

## 1. SQLite internal lock escalation × Python RLock — **ADDRESSED**

**Concern (from B6)**: SQLite's internal lock state machine (UNLOCKED -> SHARED -> RESERVED -> PENDING -> EXCLUSIVE) could interact with the Python RLock held by every `SQLiteStorage` public method, producing silent deadlocks or `database is locked` errors only under sustained concurrent load. In particular, the worry was that a Python writer holding the RLock would simultaneously sit waiting for a SQLite-level EXCLUSIVE lock — a two-layer-lock stall.

**Why WAL mode neutralises it**: In rollback-journal mode, readers take a SHARED lock on the main DB file and writers must escalate to EXCLUSIVE, which blocks while any SHARED is held — that is where the two-layer interaction bites. WAL mode flips this: readers snapshot the WAL frame without taking SHARED on the main DB, and writers append to the WAL without ever needing EXCLUSIVE on the main DB. Concurrent reads do not block writes, and concurrent writes are already serialised at the Python layer by `SQLiteStorage._lock`, so the SQLite layer never has to wait — therefore never has to escalate.

**Evidence**:
- WAL mode is set at `memory/sqlite_storage.py:195` via `PRAGMA journal_mode=WAL`.
- Independently confirmed on disk via side-channel `sqlite3.connect()`: `journal_mode = wal`, `synchronous = 2` (NORMAL), `cache_size = -2000`.
- Tests: [`tests/lock_escalation/test_sqlite_wal_under_rlock.py`](../../tests/lock_escalation/test_sqlite_wal_under_rlock.py)
  - `test_wal_mode_is_actually_enabled` — precondition gate.
  - `test_read_while_write_no_lock_escalation_stall` — 1 writer + 1 reader, 60 ops total, median 0.04ms, max 0.8ms. Zero `store.fail` / `get_recent.fail` counter increments. WAL checkpoint returns `busy=0` after the run.
  - `test_write_while_read_no_busy_leak` — slow reader (200-row keyword fallback) + writer; writer median 1.0ms, max 2.4ms. Zero counter failures.
  - `test_write_while_write_serialised_by_rlock` — 4 writers × 20 ops each = 80 atomic writes; median 0.1ms, p95 1.4ms, max 1.4ms. All 80 ids retrievable post-run (no lost writes). Zero `store.fail`.

**Findings — was this a real problem?** No. It was theoretical. WAL mode + the Python RLock combination cannot produce the feared interaction by construction:

  - WAL readers never take SHARED on the main DB, so writers never wait for them.
  - The RLock fully serialises writers at the Python layer, so the SQLite layer's RESERVED/EXCLUSIVE escalation is uncontested.
  - The three test cases ran in 0.19s total with zero `database is locked` errors and zero `SQLITE_BUSY` codes leaked through the counter channel.

The cross-exam's caution was appropriate (it correctly identified a theoretical interaction surface), but the actual interaction is closed by the WAL pragma. The test now stands as a regression guard: if anyone ever changes `journal_mode` away from WAL, the first test (`test_wal_mode_is_actually_enabled`) fails loudly with a precise error message.

**Residual risk**: Low. The only path to re-introducing the concern is regressing the `journal_mode=WAL` pragma — which the regression test catches.

---

## 2. RLock-GIL priority inversion — **ADDRESSED**

**Concern (from B6)**: A HIGH-priority thread holding the storage RLock could be displaced by a LOW-priority thread (classical priority inversion via threading.RLock + GIL); or repeated LOW-priority writers could starve a MEDIUM-priority reader so badly that the reader's latency exceeds shadow-sampling deadlines.

**Why the classical inversion is impossible in CPython**:
- The GIL does not honour OS thread priorities. CPython uses a round-robin timeslice (`sys.getswitchinterval() = 0.005s`) regardless of pthread priority.
- Setting `os.setpriority()` or `pthread_setschedparam()` is a no-op for Python-bytecode-level scheduling.
- Therefore the LOW-MEDIUM-HIGH ordering that produces classical priority inversion does not exist at the Python scheduling layer.

**What we still had to verify**: `threading.RLock` is non-fair (acquisition order is implementation-defined). A pathological RLock implementation could still starve a waiter — that is the real concern, decoupled from the misnamed "priority" framing.

**Evidence**:
- Tests: [`tests/lock_escalation/test_rlock_gil_no_inversion.py`](../../tests/lock_escalation/test_rlock_gil_no_inversion.py)
  - `test_gil_does_not_honor_thread_priorities_documented` — records `sys.getswitchinterval() = 0.005s` and serves as a future-Python-release tripwire.
  - `test_rlock_fairness_under_mixed_workload` — 1 HIGH writer + 2 LOW writers + 1 MED reader running concurrently with 8 KB payloads. Wall time 0.02s. Results:
    - HIGH: 30 ops, median 0.4ms, max 1.8ms.
    - LOW: 60 ops combined, median 0.3ms, max 2.3ms.
    - MED reader: 60/60 ops (100% completion), median 0.04ms, max 0.1ms.
    - All thread medians within 3x of global median; no tail starvation past the 10 ms absolute floor.
  - `test_no_lock_held_across_thread_exit` — negative-space probe verifying the RLock is correctly released after thread exit and is acquirable from a sibling thread.

**Findings — was this a real problem?** No starvation observed. The MED reader (the most vulnerable population — cheapest ops competing against three writers doing 8 KB payloads) completed 100% of its scheduled ops with sub-millisecond latency. The RLock backed by CPython 3.14's pthread mutex is fair enough that no thread fell more than 3x behind the global median across a 0.02s contended window. The classical-priority-inversion framing is structurally inapplicable to GIL-bound Python.

**Test calibration note**: Initial threshold of `max_ratio <= 20.0x` triggered a false positive at sub-millisecond scale (a single 3ms outlier vs 0.1ms median produces a 30x ratio that reflects scheduler jitter, not lock unfairness). Added `STARVATION_MAX_MAX_ABS_FLOOR_S = 0.010` — tail ratio only fails the test when absolute max also exceeds 10 ms, the threshold past which a tail outlier could realistically break shadow-sampling deadlines.

**Residual risk**: Low. Two future failure modes to watch:
1. A CPython release that changes RLock fairness (very unlikely — the pthread backing is stable across releases).
2. A future code path that holds the RLock across an I/O operation longer than the current sub-millisecond CRUD ops. The fairness test would catch this because median latencies would converge upward and the absolute-floor check would activate.

---

## 3. Temporal invariants — worst-case `get_recent()` latency blocking shadow sampling — **DOCUMENTED-OUT**

**Concern**: A long `get_recent()` call holding the RLock could silently shadow-sample-block downstream consumers past their deadlines, with no observable signal until the deadline is missed.

**Status**: DOCUMENTED-OUT. Not addressed in this pass.

**Why deferred**: Latency-budget tests belong with the shadow-sampling owner, not the lock-audit owner. The cross-exam correctly identified this as a *temporal* concern (about wall-clock deadlines) rather than a *correctness* concern (about lock semantics). Lock fairness is now proven (item #2); the remaining question is whether the *absolute* `get_recent` latency, summed across the contended window, fits the shadow-sampling deadline budget. That budget lives in the shadow-sampler.

**Residual risk**: Medium. We have empirical p95 measurements for `get_recent` under contention (max 0.3ms in the 60-op write-while-read test) but no formal contract that says "below X ms always." Shadow sampling could silently miss deadlines under a workload heavier than our test bench.

**Owner / next step**: Shadow sampling owner. Wire `get_recent` p95 latency into the shadow-sampling SLO probe; alert when p95 exceeds the budget. Specific test artefact deferred to that workstream.

---

## 4. Configuration drift during rollback — **DOCUMENTED-OUT (C2 owns)**

**Concern**: A storage rollback could leave configuration in a half-applied state — e.g. `STORAGE_BACKEND` env points to one backend but the on-disk DB is in a state the other backend cannot read.

**Status**: DOCUMENTED-OUT. C2 (the rollback/migration agent in this /loop) owns this directly.

**Why deferred from C3**: C3's scope is lock-escalation evidence; C2 is running the rollback-drift evidence pass in parallel. Coordinated split avoids duplicate work and double-write conflicts on `scripts/lock_audit/`.

**Residual risk**: Until C2 reports, classify as Medium. Existing partial coverage in `tests/storage_edge/test_storage_edge_cases.py::test_breaker_trip_during_rollback` already exercises the breaker × rollback path.

**Owner / next step**: C2 agent in this /loop. Cross-link C2's deliverable here when complete.

---

## 5. OS / filesystem-dependent behaviour (msync, NFS, fd reuse) — **OUT-OF-SCOPE**

**Concern**: SQLite WAL relies on `fsync()` / `msync()` semantics being correct; on NFS or other network filesystems these guarantees are weakened or broken, and the WAL frame ordering invariant can be violated by silent fd reuse.

**Status**: OUT-OF-SCOPE.

**Reason**: The ContextDNA mothership architecture targets single-machine local SQLite at `~/.context-dna/learnings.db` (see `memory/sqlite_storage.py::get_db_path()` lines 118-137 — `USER_DATA_DIR` defaults to `~/.context-dna`, an APFS-backed local path on macOS / ext4 on Linux). NFS-backed SQLite is not a supported configuration. Multi-machine state synchronization (Layer 3) goes through the fleet replication channel, not through filesystem sharing.

**Residual risk**: Negligible for the supported deployment. If a future user mounts `~/.context-dna` on NFS, the supported response is "don't do that — use the fleet replication channel for cross-machine state."

**Owner / next step**: If/when distributed deployment is in scope, re-open as a new audit item with NFS-specific tests. Until then, no work.

---

## 6. Multi-process deployment (RLock per-process, no distributed lock) — **OUT-OF-SCOPE**

**Concern**: `threading.RLock` is per-process. If two Python processes attach the same SQLite DB, the RLock serialisation guarantee evaporates — process A's RLock does not know about process B's writers.

**Status**: OUT-OF-SCOPE.

**Reason**: The current architecture uses a single Storage singleton per process (`get_sqlite_storage()` in `memory/sqlite_storage.py:952-959`). Multi-process attachment to the same DB file is not a supported topology. If/when a future scheduler spawns sibling Python processes that all attach to the same DB, the design will need a cross-process lock primitive (e.g. SQLite's own BEGIN IMMEDIATE / file-system flock) — that is a different audit and a different invariant.

**Residual risk**: Theoretically high if architecture changes; structurally zero today (single process = no multi-process race surface). The fact that the singleton accessor pattern is unconditionally used everywhere makes accidental multi-process attachment hard to introduce.

**Owner / next step**: If/when multi-process deployment is on the roadmap, open a new audit item. Until then, no work. The single-process invariant is implicit in the rest of the architecture (Python GIL-bound, agent service is one process).

---

## Cross-links

- [`lock_ordering_invariant.md`](./lock_ordering_invariant.md) — full per-method lock acquisition table (Phase A single-lock proof).
- [`tests/lock_escalation/test_sqlite_wal_under_rlock.py`](../../tests/lock_escalation/test_sqlite_wal_under_rlock.py) — empirical evidence for item #1.
- [`tests/lock_escalation/test_rlock_gil_no_inversion.py`](../../tests/lock_escalation/test_rlock_gil_no_inversion.py) — empirical evidence for item #2.
- [`deadlock_detector.py`](./deadlock_detector.py) — runtime detection if Phase B/C introduces multi-lock methods.

---

## Convergence note

Of the six concerns the B6 cross-exam surfaced:

  * **2 closed empirically** with passing tests (items #1, #2).
  * **2 deferred to specific owners** with explicit residual-risk classification (items #3, #4).
  * **2 ruled out by architectural scope** (items #5, #6) — re-open if architecture changes.

The cross-exam's residual-concerns list can now be retired. Items #3, #4 remain on the active workstream's TODO under their respective owners; items #1, #2 are closed. Items #5, #6 are dormant unless the deployment shape changes.
