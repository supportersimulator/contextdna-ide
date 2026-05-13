# SQLite Lock Granularity — Phased Upgrade Plan

**Companion to**: `lock_audit_report.md`
**Owner of risk**: Concurrent webhook ingestion + xbar dashboard polling + butler background passes all hit `sqlite_storage.py` on the same `self.conn`.

---

## Phase A — Keep full-DB RLock, close the read-write race (Effort: S, ~2h)

**Goal**: stop the race the audit found between `store_learning` (locked) and `get_recent`/`query`/`get_stats` (unlocked) on the same connection.

### Changes

1. In `sqlite_storage.py`, wrap every method that touches `self.conn` with `with self._lock:`. List:
   - `get_by_id` (505)
   - `get_recent` (516)
   - `get_by_session` (527)
   - `get_since` (539)
   - `get_by_type` (552)
   - `query` (565)
   - `get_stats` (629)
   - `health_check` (667)
   - `record_negative_pattern` (678)
   - `get_frequent_negative_patterns` (728)
   - `mark_pattern_promoted` (766)
2. Reentrancy is already covered (`RLock`, not `Lock`), so `store_learning` → `get_by_id` works.
3. Bound `_find_duplicate` (line 354) to `LIMIT 200` so the dedup scan can never starve readers behind the writer.
4. Bound `query` keyword fallback to `LIMIT 100` (currently 200) and add early-exit once `scored` reaches `limit`.

### Validation

- Re-run the Round-3 storage fuzz harness (cited at line 120) — should still pass.
- Add a fuzz that interleaves `store_learning` (write) with `query` (read). Expect: zero "torn row" / "FTS rowid out of range" errors.
- Track `recent.fail.huge` counter in `learning_store.py` (already wired at line 241) — should drop to zero in Phase A.

### Risk

- **New deadlock class**: none. RLock is reentrant; any call chain that re-enters `self._lock` is already safe.
- **Latency increase**: yes — readers now serialize behind writers. Estimated +15ms p95 for `get_stats` during webhook bursts. Acceptable per current SLO (`/health` p95 < 100ms).

### Rollback

Single feature flag: `SQLITE_FULL_LOCK_EVERYTHING=0`. If p95 regresses on real traffic, revert the `with self._lock:` decorators. Old behavior is exactly today's behavior.

---

## Phase B — Per-table locks (Effort: M, ~1 week)

**Goal**: let reads on `negative_patterns`, `device_config`, `stats` proceed in parallel with writes to `learnings`. Justified by the audit: `record_negative_pattern` touches a different table than `store_learning`; there is no reason for them to serialize.

### Lock map design

```python
self._locks: Dict[str, threading.RLock] = {
    "learnings":         threading.RLock(),   # bundle: learnings + learnings_fts (trigger)
    "negative_patterns": threading.RLock(),
    "device_config":     threading.RLock(),
    "stats":             threading.RLock(),
}

# Acquisition order (ALWAYS this order to prevent A→B vs B→A deadlock):
#   stats > device_config > negative_patterns > learnings
# If a method needs two locks, acquire low-priority first.
```

### Method → lock(s) mapping

| Method | Lock(s) needed |
|---|---|
| `store_learning` | learnings |
| `_find_duplicate` (called from store_learning) | (inherits) |
| `get_by_id`, `get_recent`, `get_by_session`, `get_since`, `get_by_type` | learnings (READ) — but per Phase C, none |
| `query` (FTS path) | learnings |
| `get_stats` | learnings + stats (acquire stats first) |
| `record_negative_pattern` | negative_patterns |
| `get_frequent_negative_patterns` | negative_patterns |
| `mark_pattern_promoted` | negative_patterns |
| `link_to_backend` (device.json — not SQLite) | n/a |

### New deadlock classes introduced

1. **Two-lock methods** (`get_stats` needs learnings + stats). Mitigated by hard acquisition-order rule.
2. **Future helpers** that touch multiple tables in one transaction. Mitigated by enforcing `_locks` order in code review + `deadlock_detector.py` (see below).
3. **Reentrant write** (`store_learning` calls `get_by_id` inside its lock). RLock keeps this safe within one logical lock; cross-lock reentry must be flagged.

### Detection

- Wire `deadlock_detector.py` (this delivery) into a debug-mode wrapper around `RLock.acquire`. Toggle via `SQLITE_LOCK_DEBUG=1`. In production: off, zero overhead.
- ZSF counter `deadlock_inversions_total{a=X, b=Y}` increments any time a thread acquires A→B in the wrong order. Surface in `/health.locks`.

### Rollback

- Replace the `_locks` dict with `{name: self._lock for name in ...}` — every name maps to the same RLock = Phase A behavior. One-line revert.
- Cost: 30 seconds, zero data risk.

---

## Phase C — WAL read-uncommitted SELECT for read-heavy paths (Effort: L, ~3 weeks)

**Goal**: reads do not block writes and writes do not block reads, *measured*. Today's `journal_mode=WAL` + `synchronous=NORMAL` already gives concurrent reads + writes at the SQLite layer — but every method goes through `self.conn` which is one Python-side resource. Phase C separates read and write paths.

### Design

Maintain **two** connection pools per `SQLiteStorage` instance:

```python
self._writer_conn = sqlite3.connect(..., isolation_level=None)
self._reader_pool = queue.Queue()   # bounded N=4 readers
for _ in range(4):
    rc = sqlite3.connect(..., isolation_level="DEFERRED")
    rc.execute("PRAGMA read_uncommitted=1")  # WAL semantics — safe in SQLite WAL mode
    self._reader_pool.put(rc)
```

- Writes go through `self._writer_conn` (held under `self._locks["learnings"]`).
- Reads borrow from `self._reader_pool` for the duration of the SELECT; no Python lock.
- `read_uncommitted=1` in WAL mode means readers see committed writes immediately — there is no "dirty read" because WAL writers can't expose uncommitted state.
- Reader pool size = `max(4, NUM_CORES // 2)`, configurable via `SQLITE_READER_POOL`.

### Benchmark methodology

Required before promoting Phase C to default:

1. **Baseline**: Phase A enabled, single-conn. Measure p50/p95/p99 latency for:
   - `get_recent(20)` × 1000 calls
   - `get_recent(200)` × 1000 calls
   - `query("import error")` × 500 calls
   - `get_stats()` × 500 calls
   - **Concurrent**: 4 threads, each doing 250 of the above, while 1 writer thread runs `store_learning` at 10 Hz.
2. **Phase C**: same workload, same counts.
3. **Pass criteria**: Phase C p95 < 0.7 × Phase A p95 for read paths; write p95 unchanged or better.
4. **Failure mode**: if pool exhaustion occurs (queue.Empty after timeout), bump `reader_pool_exhausted_total` counter and fall through to writer conn under the lock. ZSF.

### New deadlock classes introduced

1. **Reader pool starvation** — long-running query holds reader conn forever. Mitigated by per-borrow timeout (5s).
2. **Connection leak on exception** — must be wrapped in `try/finally` to return conn to pool. Mitigated by context manager helper `@_borrow_reader`.
3. **Schema migration during runtime** — DDL needs all connections drained. Mitigated by `_freeze()` method that pulls all readers from the pool before issuing DDL.

### Rollback

- `SQLITE_READER_POOL=0` collapses pool size to zero; every read falls through to the writer conn under the lock = Phase A behavior.
- The reader pool only adds; it never replaces the existing path. Falling back is graceful.

---

## Phase ordering rationale

| Phase | When to ship | Trigger |
|---|---|---|
| A | Now (next merge) | Audit found unlocked reads on shared `self.conn` — already a real race |
| B | After 2 weeks Phase A stable | If `recent.fail.*` counter shows non-zero contention residue |
| C | After Phase B + benchmark proves contention is measurable | Only if dashboard latency is observed > SLO under load |

**Do not skip phases.** Each phase is independently revertable; combined phase jumps make rollback nontrivial.

---

## Risk register

| Risk | Mitigation | Detection |
|---|---|---|
| Phase B introduces A→B / B→A deadlock | Hard acquisition-order rule; `deadlock_detector.py` in debug | `deadlock_inversions_total > 0` |
| Phase C reader sees stale snapshot | WAL read_uncommitted in WAL mode = bounded staleness (< 100ms typical) | Add `staleness_ms` to test harness |
| Phase A causes p95 regression > SLO | Feature flag + revert | `/health.webhook.p95_ms` > 100 |
| Lock acquisition order drift over time | Static check: any new method touching `self._locks` must declare its acquisition order in a docstring; CI grep + reviewer checklist | CI gate |
| RLock recursion blowup (writer calls reader calls writer) | RLock allows reentry on same thread; depth-limit at 5 in detector | `deadlock_recursion_depth_total` counter |

---

## Open questions for review

1. Phase B per-table locks: should `learnings_fts` share the `learnings` lock (current plan) or get its own? Argument for sharing: trigger cascades inside the same transaction. Argument for separate: bulk FTS rebuilds could proceed in parallel with writes. **Recommendation**: share. Cost of getting it wrong is higher than the win.
2. Phase C reader pool size: 4 is a guess. Benchmark must drive the choice. If most reads are < 1ms (PK lookup), even 2 is plenty.
3. Should Phase A apply to `dialogue_mirror`, `task_persistence`, `context_bus` too? Audit says NO — those modules already use per-call connections + per-call locks. The fix is specific to `sqlite_storage.py`'s shared-conn pattern.

---

## Files touched per phase

| Phase | Files | Lines changed (estimate) |
|---|---|---|
| A | `sqlite_storage.py` only | ~30 (one decorator per method + 1 LIMIT change) |
| B | `sqlite_storage.py`, new `lock_registry.py` | ~150 |
| C | `sqlite_storage.py`, new `reader_pool.py`, `tests/test_reader_pool.py` | ~400 |
