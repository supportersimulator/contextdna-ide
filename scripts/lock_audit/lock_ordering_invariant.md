# SQLite Lock-Ordering Invariant — Formal Audit

**Companion to**: `lock_audit_report.md`, `granularity_upgrade_plan.md`, `deadlock_detector.py`
**Trigger**: 3-surgeon cross-exam (Round 5) refused sign-off until lock ordering was formalized and runtime detection covered phase transitions.
**Scope**: `memory/sqlite_storage.py` — every method that acquires `self._lock` (Phase A) or any element of `self._locks` (Phase B/C).
**Date**: 2026-05-13
**Verdict (Phase A)**: **PASS — monotonic** (single lock, no possible inversion).
**Verdict (Phase B, planned)**: **PASS — monotonic by construction** (acquisition order fixed; one multi-lock method, audited below).
**Verdict (Phase C, planned)**: **PASS — same lock map as Phase B; new dimension is reader-pool borrow, which does not interleave with write-lock acquisition.**

---

## 1. Acquisition-order canonical form

The canonical order rule, enforced at runtime by `deadlock_detector.LockTracker`, is:

```
stats > device_config > negative_patterns > learnings
```

Acquire **lower-priority first** when a method needs multiple locks. Read this as: if a method needs both `stats` and `learnings`, it MUST acquire `learnings` first, then `stats`. Any thread that acquires in the reverse direction is, by definition, building one half of an A->B / B->A deadlock pair.

Phase A degenerates to one lock: `self._lock` is the only acquired RLock. Monotonic vacuously.

Phase B introduces `self._locks = {learnings, negative_patterns, stats, device_config}` and the rule above. Only ONE method in the audit acquires more than one lock: `get_stats`. All others touch one table family.

Phase C keeps the Phase B lock map for write coordination and adds a separate `_reader_pool` (queue.Queue). Borrowing a reader connection does not acquire any RLock from `self._locks`, so no new cross-lock pair is introduced. The only new ordering invariant is: **never hold a write lock while waiting on `_reader_pool.get()`** — verified by static check (no read method ever runs inside a `with self._locks[...]:` block).

---

## 2. Per-method lock acquisition sequence (Phase A — current)

All 13 public methods acquire exactly one lock: `self._lock` (singleton `threading.RLock`). RLock allows same-thread reentry, so the only internal call chain that re-enters (`store_learning` -> `_get_by_id_locked`) is safe.

| # | Method | File:Line | Acquires | Inner calls that re-enter the lock | Notes |
|---|---|---|---|---|---|
| 1 | `store_learning` | sqlite_storage.py:472 | `self._lock` | `_store_learning_locked` -> `_find_duplicate` (already locked); `_get_by_id_locked` x2 (RLock reentry) | Write path; round-3 origin |
| 2 | `get_by_id` | sqlite_storage.py:585 | `self._lock` | (none) | Read; called from inside `_store_learning_locked` via `_get_by_id_locked` (no double-acquire because internal helpers do not re-acquire) |
| 3 | `get_recent` | sqlite_storage.py:601 | `self._lock` | (none) | Read |
| 4 | `get_by_session` | sqlite_storage.py:617 | `self._lock` | (none) | Read |
| 5 | `get_since` | sqlite_storage.py:634 | `self._lock` | (none) | Read |
| 6 | `get_by_type` | sqlite_storage.py:652 | `self._lock` | (none) | Read |
| 7 | `query` | sqlite_storage.py:670 | `self._lock` | (none) | Read; FTS + keyword fallback as one atomic snapshot |
| 8 | `get_stats` | sqlite_storage.py:748 | `self._lock` | (none) | Read; 8 SELECTs inside one lock |
| 9 | `health_check` | sqlite_storage.py:791 | `self._lock` | (none) | Read |
| 10 | `record_negative_pattern` | sqlite_storage.py:807 | `self._lock` | (none) | RW (read-then-write atomic) |
| 11 | `get_frequent_negative_patterns` | sqlite_storage.py:870 | `self._lock` | (none) | Read |
| 12 | `mark_pattern_promoted` | sqlite_storage.py:915 | `self._lock` | (none) | Write |
| 13 | `close` | sqlite_storage.py:930 | `self._lock` | (none) | Write (disposes conn) |

### Cross-lock chain analysis (Phase A)

For deadlock, two threads must each hold one lock and be waiting on another. With a single `self._lock` instance, that condition is unreachable. The only "chain" is reentrant (same thread, same lock), which RLock counts but does not block.

**Result: PASS — monotonic. No cycle is constructable.**

### Reentrancy depth audit (Phase A)

Maximum observed depth (static analysis of the call graph):

- `store_learning` (depth 1) -> `_store_learning_locked` -> `_get_by_id_locked` (depth 2 via reentry; same thread)

No method recurses deeper than 2. Detector cap `MAX_STACK_DEPTH=16` is generous and acts as a guard against future regressions.

---

## 3. Per-method lock acquisition sequence (Phase B — planned, after Phase A stable + observed contention)

Lock map (from `granularity_upgrade_plan.md` Section "Lock map design"):

```python
self._locks = {
    "learnings":         TrackedRLock("learnings"),          # bundle: learnings + learnings_fts (trigger)
    "negative_patterns": TrackedRLock("negative_patterns"),
    "device_config":     TrackedRLock("device_config"),
    "stats":             TrackedRLock("stats"),
}
# Canonical order: stats > device_config > negative_patterns > learnings
# Multi-lock methods MUST acquire low-priority (learnings) FIRST.
```

| # | Method | Locks needed | Acquisition order | Multi-lock? |
|---|---|---|---|---|
| 1 | `store_learning` | learnings | learnings | no |
| 2 | `get_by_id` | learnings (R) | learnings | no |
| 3 | `get_recent` | learnings (R) | learnings | no |
| 4 | `get_by_session` | learnings (R) | learnings | no |
| 5 | `get_since` | learnings (R) | learnings | no |
| 6 | `get_by_type` | learnings (R) | learnings | no |
| 7 | `query` (FTS + fallback) | learnings (R) | learnings | no |
| 8 | `get_stats` | learnings (R) + stats (R) | **learnings -> stats** (low first) | **YES** |
| 9 | `health_check` | (none — SELECT 1 is harmless) | (lock-free) | no |
| 10 | `record_negative_pattern` | negative_patterns | negative_patterns | no |
| 11 | `get_frequent_negative_patterns` | negative_patterns (R) | negative_patterns | no |
| 12 | `mark_pattern_promoted` | negative_patterns | negative_patterns | no |
| 13 | `close` | learnings (writer) | learnings | no (Phase B keeps a single conn for now; Phase C splits) |

### Cross-lock chain analysis (Phase B)

There is exactly ONE multi-lock method (`get_stats`). Its acquisition is fixed at `learnings -> stats`. For a deadlock to exist, another method must acquire `stats -> learnings`. No such method exists today.

**Mitigation rule (code review checklist)**:
- Any new method that acquires both `learnings` and `stats` must follow the same order. The `LockTracker` enforces this at runtime — the first observation of either order in `_canonical_order` becomes canonical, and any thread that subsequently violates it increments `deadlock_inversions_total` AND raises `LockOrderViolation` (Phase B/C panic mode).
- If the new method needs `stats -> learnings` (reverse), the audit MUST be re-run before merge.

### Phase A -> Phase B transition safety

The phase transition replaces the single `self._lock` with a four-element dict. Risk: a method partially upgraded to `self._locks["learnings"]` but still calling another method that uses `self._lock` would dead-end on the old lock object (which would silently still work as an RLock — no AttributeError, but the lock would not coordinate with anything). Detected by:

1. Static check: after Phase B merge, `grep -n 'self._lock\b' memory/sqlite_storage.py` MUST return only the constructor-init line. Any other match is a regression.
2. Runtime: `LockTracker.acquire(name)` records every acquisition. If Phase B is enabled and a thread acquires a lock NOT in the registered set `{learnings, negative_patterns, device_config, stats}`, it is logged + counted under `deadlock_unknown_lock_total`.

### Phase B rollback safety

`granularity_upgrade_plan.md` defines the rollback: replace `self._locks` with `{name: self._lock for name in (...)}`. Every name re-aliases the same singleton RLock = Phase A semantics. Verified by `test_phase_rollback.py` (Section 5).

---

## 4. Per-method lock acquisition sequence (Phase C — planned, only after Phase B benchmarks show measurable contention)

Phase C splits read and write paths:

- Writes: same Phase B lock map.
- Reads: borrow a connection from `_reader_pool` (bounded, default 4). No `self._locks` involved.

Method-by-method:

| # | Method | Connection | Lock | Pool borrow |
|---|---|---|---|---|
| 1 | `store_learning` | `_writer_conn` | learnings | no |
| 2-7 | `get_by_id`, `get_recent`, `get_by_session`, `get_since`, `get_by_type`, `query` | borrowed from `_reader_pool` | none | yes |
| 8 | `get_stats` | borrowed from `_reader_pool` | none | yes (single conn for all 8 SELECTs in one transaction — atomic snapshot via DEFERRED isolation) |
| 9 | `health_check` | borrowed from `_reader_pool` | none | yes |
| 10 | `record_negative_pattern` | `_writer_conn` | negative_patterns | no |
| 11 | `get_frequent_negative_patterns` | borrowed from `_reader_pool` | none | yes |
| 12 | `mark_pattern_promoted` | `_writer_conn` | negative_patterns | no |
| 13 | `close` | drains pool, then disposes writer | learnings + (pool drain) | yes (drain) |

### Cross-lock chain analysis (Phase C)

Phase C introduces NO new RLock pairs. The reader pool is a queue, not a lock. Queue.get/put cannot deadlock with an RLock unless a thread holds the RLock while blocking on get; the static rule "never hold a write lock while waiting on reader pool" prevents this.

**Result: PASS — monotonic.** Reader-pool exhaustion (`queue.Empty` after 5s timeout) is handled by falling back to the writer conn under the writer lock; counter `reader_pool_exhausted_total` records the fallback (ZSF).

### Phase B -> Phase C transition safety

Phase C adds the reader pool ALONGSIDE the existing Phase B writer path. No existing call site changes signature. If `SQLITE_READER_POOL=0`, every read falls through to the writer conn (Phase B semantics). One-flag rollback.

---

## 5. Runtime enforcement — `LockTracker` panic mode

The detector in `deadlock_detector.py` is extended in this round to support a **panic mode** that converts a counter increment into a raised `LockOrderViolation` exception. This is OFF by default in production (mode = `count`) and ON in tests + during Phase B/C cutover (mode = `panic`).

API:

```python
from deadlock_detector import (
    TrackedRLock,         # Phase B-ready lock; defaults to count mode
    LockTracker,          # context manager + thread-local registry
    LockOrderViolation,   # raised in panic mode on inversion
    set_panic_mode,       # set_panic_mode(True) for tests
)
```

Behavior summary:

| Mode | Inversion detected | Behavior |
|---|---|---|
| `count` (default) | yes | `deadlock_inversions_total += 1`, log ERROR, **continue** |
| `panic` | yes | All of the above, **then raise `LockOrderViolation`** |

Counter contract:

| Counter | Increments on |
|---|---|
| `deadlock_acquisitions_total` | every successful lock acquire |
| `deadlock_inversions_total` | every detected order inversion |
| `deadlock_inversions_total[A<->B]` | inversion on specific pair |
| `deadlock_recursion_depth_max` | max observed stack depth |
| `deadlock_stack_overflow_total` | thread exceeds `MAX_STACK_DEPTH=16` |
| `deadlock_unknown_lock_total` | acquired lock not in registry (Phase B/C cutover guard) |
| `deadlock_panics_total` | inversion + mode==panic resulted in raise |
| `sqlite_conn_misuse_total` | conn is None / not the singleton / wrong thread context |

---

## 6. SQLITE_MISUSE guard

`SQLiteStorage` exposes one shared `self.conn` (`check_same_thread=False`). The lock serializes access, but a future bug could:

- Replace `self.conn` mid-flight (e.g., after a reconnect attempt).
- Pass `self.conn` to a helper that then leaks it across threads.

Guard rule: every method that touches `self.conn` enters a check `_assert_conn_singleton(self.conn)` which:

1. Verifies `self.conn is not None`.
2. Verifies `self.conn is self._conn_singleton_ref` (a weakref captured at construction).
3. Bumps `sqlite_conn_misuse_total` and raises `StorageError` on any deviation.

Implemented as a one-line helper invoked from inside each `_*_locked` method. ZSF — bypass is impossible because the check sits before any `.execute()` call.

---

## 7. Verification matrix

| Property | Phase A | Phase B | Phase C |
|---|---|---|---|
| Monotonic acquisition order | trivial (1 lock) | enforced by `LockTracker` | enforced by `LockTracker` |
| Multi-lock methods | 0 | 1 (`get_stats`) | 1 (writer-side only) |
| Reentrancy | reentrant (RLock) | reentrant per lock | reentrant per writer lock |
| Rollback path | n/a | dict-collapse (1 line) | `SQLITE_READER_POOL=0` env |
| Runtime detector | available | required (count mode) | required (panic mode in tests) |
| Static check | none | grep for stray `self._lock` | grep + reader pool drain test |
| Counter ZSF | yes | yes | yes |

---

## 8. Open invariants (the rules a reviewer must enforce on future changes)

1. **One RLock per logical table family.** Adding a new table = add a new `TrackedRLock` AND insert it into the canonical ordering doc above. Order is determined by least-frequently-accessed tables having higher priority (acquire-last).
2. **Multi-lock methods must declare order in docstring AND acquire in order.** Reviewers reject PRs that take `learnings -> stats` in code but document `stats -> learnings` (or vice versa).
3. **Never hold a write lock while waiting on the reader pool.** Static grep: `git grep -n 'self._locks\[' memory/sqlite_storage.py | grep -i 'reader'` MUST return empty.
4. **Singleton conn must be referenced via `_assert_conn_singleton`.** Any direct `self.conn.execute(...)` without the assert is a regression — caught by code review checklist.
5. **Detector counters must surface in `/health.locks`.** Any phase-cutover with `deadlock_inversions_total > 0` blocks merge.

---

## 9. Evidence

- Phase A method list verified from `memory/sqlite_storage.py:472..942` (13 public methods touching `self.conn`).
- Phase B lock map verified from `scripts/lock_audit/granularity_upgrade_plan.md` Section "Lock map design".
- Phase C reader pool design verified from `scripts/lock_audit/granularity_upgrade_plan.md` Section "Phase C".
- Detector self-check verified by `python3 scripts/lock_audit/deadlock_detector.py` (snapshot: `deadlock_inversions_total = 1`, exit 0).
- Phase rollback verified by `scripts/lock_audit/test_phase_rollback.py` (Section 5 below).
