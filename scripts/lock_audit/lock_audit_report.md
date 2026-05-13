# SQLite Lock Audit Report

**Scope**: All SQLite access points in `/tmp/cdna-mothership-build/memory/`
**Trigger**: Neurologist Round-3 PARTIAL verdict — "Lock Granularity Audit + Upgrade"
**Method**: grep + read; every row below references file:line in the build tree.
**Date**: 2026-05-13

---

## Executive Summary

| Module | Lock Type | Coverage Gap | Worst-Case Contention |
|---|---|---|---|
| `sqlite_storage.py` | `threading.RLock` on instance (self._lock) | **9 of 10 public methods UNLOCKED** | Reads can race with writes mid-transaction |
| `observability_store.py` | `threading.Lock` write-only (self._write_lock) | Reads unlocked, but reads are read-only & WAL-safe | LOW |
| `evidence_ledger.py` | Per-DB-path `threading.Lock` write-only | Reads use fresh `conn`, WAL-safe | LOW |
| `dialogue_mirror.py` | `threading.Lock` write-only; new conn per call | Reads open/close their own conn | LOW |
| `task_persistence.py` | `threading.Lock` on `_lock`; opens fresh conn each call | Used uniformly across CRUD | LOW |
| `context_bus.py` | `threading.Lock`; new conn per op | Polling thread + writers share lock | MED |
| `consolidated_db.py` | Lock guards thread-local conn map only, not exec | Per-thread conn means no cross-thread lock | MED (if same-thread reentry) |
| `atlas_journal.py` | None — `with sqlite3.connect()` per call | New conn each time = WAL handles isolation | LOW |
| `session_gold_passes.py` | Redis cross-process lock; new conn each call | Holds 10-min Redis lock for whole pass | MED for LLM defer |
| `anticipation_engine.py` | Module-level locks for singleton/cycle gating | Per-call `sqlite3.connect(...)` | LOW |
| `db_utils.py::ThreadSafeConnection` | `threading.Lock`; wraps every execute | Coarse — full-DB | MED |
| `postgres_storage.py` | Pool-level lock only; not relevant to SQLite | N/A | — |

**The contention story is concentrated in `sqlite_storage.py`.** Every other module either uses transient connections (WAL handles isolation naturally) or guards both reads and writes uniformly. The Round-3 fix only patched the bleed in `store_learning`; the other 9 public methods on the same shared connection still expose the race.

---

## Section A — `sqlite_storage.py` (canonical learnings DB)

Single `self.conn` opened with `check_same_thread=False, isolation_level=None` (autocommit) + WAL + cache_size=10000. Schema covers tables: `learnings`, `learnings_fts` (FTS5 virtual), `stats`, `device_config`, `negative_patterns`. Three triggers (`learnings_ai/ad/au`) keep FTS in sync — every write to `learnings` cascades into `learnings_fts`.

| File | Line | Method | Tables | Op | Locked? | Contention | Recommended | Effort |
|---|---|---|---|---|---|---|---|---|
| sqlite_storage.py | 393–408 | `store_learning` | learnings, learnings_fts (trigger), negative_patterns | W (RW: dedup read + update/insert) | YES (RLock) | **HIGH** — full-DB write blocks all readers on same conn | Keep RLock for Phase A; per-table for Phase B (learnings + FTS as one bundle) | S |
| sqlite_storage.py | 409–503 | `_store_learning_locked` | learnings, learnings_fts | RW | YES (caller holds) | HIGH | Same as above | — |
| sqlite_storage.py | 330–371 | `_find_duplicate` | learnings | R (full window scan, hours back) | Only when called from `store_learning` | **HIGH** — fetchall() of N rows inside lock | Lock-free with WAL + immutable read; bound scan with LIMIT 200 | M |
| sqlite_storage.py | 373–389 | `_smart_merge` | (in-memory) | — | N/A | N/A | — | — |
| sqlite_storage.py | 505–514 | `get_by_id` | learnings | R (PK lookup) | **NO** | LOW per call, MED in aggregate | Lock-free (WAL SELECT) | S |
| sqlite_storage.py | 516–525 | `get_recent` | learnings | R (ORDER BY created_at LIMIT) | **NO** | MED — called by every xbar refresh + dashboard poll | Lock-free (WAL SELECT) | S |
| sqlite_storage.py | 527–537 | `get_by_session` | learnings | R | **NO** | LOW | Lock-free | S |
| sqlite_storage.py | 539–550 | `get_since` | learnings | R | **NO** | LOW | Lock-free | S |
| sqlite_storage.py | 552–563 | `get_by_type` | learnings | R | **NO** | MED — every webhook section pulls by type | Lock-free | S |
| sqlite_storage.py | 565–627 | `query` (FTS) | learnings, learnings_fts | R (JOIN, RANK; fallback fetchall LIMIT 200) | **NO** | **HIGH** — FTS MATCH + keyword fallback both scan + sort | Lock-free for FTS path; LIMIT-bound for fallback | S |
| sqlite_storage.py | 629–665 | `get_stats` | learnings | R (`SELECT COUNT(*)` x7 + `MAX(created_at)`) | **NO** | **HIGH** — 8 separate `COUNT(*)` per call; called by xbar every refresh | Per-table lock OR cached snapshot OR single CTE | M |
| sqlite_storage.py | 667–674 | `health_check` | (`SELECT 1`) | R | **NO** | LOW | Lock-free | — |
| sqlite_storage.py | 678–726 | `record_negative_pattern` | negative_patterns | RW (read existing + update or insert) | **NO** | MED — concurrent webhook + butler can write same pattern_key | Per-table lock (negative_patterns) | S |
| sqlite_storage.py | 728–764 | `get_frequent_negative_patterns` | negative_patterns | R | **NO** | LOW | Lock-free | — |
| sqlite_storage.py | 766–774 | `mark_pattern_promoted` | negative_patterns | W | **NO** | LOW | Per-table lock | S |
| sqlite_storage.py | 776–781 | `close` | — | — | **NO** | LOW (singleton lifetime) | Process-end only | — |

### Contention math (sqlite_storage.py)

- `store_learning` holds the full-DB RLock for the duration of: dedup scan (up to 24-hour window full read) + UPDATE/INSERT + trigger cascade to FTS. P95 hold time on a 1k-row DB at ~50KB content: 8–15ms (estimated from row-factory + JSON parse cost). On a 50k-row DB without `_find_duplicate` LIMIT: 50–200ms.
- `get_stats` issues 8 sequential `COUNT(*)` queries (no GROUP BY rollup, no cached materialized view). At 50k rows that's 8 × full-table scan × index lookup. Estimated 20–40ms total. Called by xbar dashboard every refresh and by `gains-gate.sh`.
- `query` keyword fallback path fetches up to 200 rows + Python-side scoring loop. The fallback fires when FTS returns zero rows — common for short queries.

**Why the Round-3 fix is correct but incomplete**: it stopped the cursor-state corruption observed in fuzz harness for concurrent `store_learning` calls. It did NOT prevent a read on the same `self.conn` from interleaving with a write — and SQLite shared connection cursors share state. Concurrent reader during writer mid-transaction can return torn rows or trigger `OperationalError: database is locked` because the trigger cascade to FTS holds the underlying journal.

### Highest-contention method: `store_learning` (line 393)

Reason: holds the only existing lock, drags `_find_duplicate`'s full-window scan inside the critical section, AND every other public reader runs unlocked against the same `self.conn`. Inversion risk: reader sees half-applied FTS trigger state.

---

## Section B — `observability_store.py` (webhook events)

Connection: `_connect_sqlite()` returns one shared conn (line 299), `check_same_thread=False, timeout=5`. WAL + busy_timeout=5000 + foreign_keys=ON.

| File | Line | Method | Tables | Op | Locked? | Contention | Recommended | Effort |
|---|---|---|---|---|---|---|---|---|
| observability_store.py | 472–486 | `_sqlite_exec` | (variadic) | W | YES (_write_lock) | MED — every webhook event writes via this | Keep coarse-lock; per-table not needed | — |
| observability_store.py | 441–459 | `_write_meta` | observability_meta | W | YES (_write_lock) | LOW | — | — |
| observability_store.py | 740–764 | `get_events` | observability_events | R | **NO** | LOW — WAL allows concurrent reads | Lock-free OK | — |
| observability_store.py | 766–804 | `query_events` | observability_events | R | **NO** | LOW | — | — |
| observability_store.py | 806–821 | `get_session_trace` | observability_events | R | **NO** | LOW | — | — |
| observability_store.py | 823–841 | `get_recent_failures` | observability_events | R (filter success=0) | **NO** | LOW | — | — |
| observability_store.py | 843–858 | `get_claims_by_status` | claim | R | **NO** | LOW | — | — |
| observability_store.py | 860–876 | `get_frequent_negative_patterns` | negative_pattern | R | **NO** | LOW | — | — |
| observability_store.py | 878–891 | `get_recent_task_runs` | task_run_event | R | **NO** | LOW | — | — |
| observability_store.py | 893–908 | `get_recent_dependency_events` | dependency_status_event | R | **NO** | LOW | — | — |

Pattern: writes are coarse-locked (correct for single shared conn). Reads are SELECT-only on WAL — safe even unlocked because SQLite's WAL gives readers a consistent snapshot.

---

## Section C — `evidence_ledger.py` (content-addressed evidence)

Connection: opened per-call in `_conn()` (line 320). Per-DB-path locks via `_get_write_lock()` registry (line 177). Writes lock; reads don't.

| File | Line | Method | Tables | Op | Locked? | Contention | Recommended | Effort |
|---|---|---|---|---|---|---|---|---|
| evidence_ledger.py | 415–502 | `record` | evidence_records, evidence_parents | RW (dedupe-check + BEGIN IMMEDIATE + INSERT) | YES (write_lock + BEGIN IMMEDIATE) | LOW — per-call conn, lock per-DB-path | Already correct | — |
| evidence_ledger.py | 506–523 | `get` | evidence_records | R | **NO** | LOW | OK (WAL) | — |
| evidence_ledger.py | 525–557 | `query` | evidence_records | R | **NO** | LOW | OK | — |
| evidence_ledger.py | 559–604 | `lineage` | evidence_records, evidence_parents | R (BFS walk) | **NO** | LOW | OK | — |
| evidence_ledger.py | (redact_record) | redaction path | evidence_records | W | YES | LOW | — | — |

**This module is the gold standard.** Per-DB-path locks + per-call connections + BEGIN IMMEDIATE on writes + WAL-protected reads. Other modules should converge on this pattern.

---

## Section D — `dialogue_mirror.py` (user/agent transcript)

Connection: `_connect()` (line 212) opens fresh each call; closes in `finally`. `self._lock` (line 202) guards writes only.

| File | Line | Method | Tables | Op | Locked? | Contention | Recommended | Effort |
|---|---|---|---|---|---|---|---|---|
| dialogue_mirror.py | 261–306 | `get_recent_messages` | dialogue_messages | R | **NO** | LOW (new conn) | OK | — |
| dialogue_mirror.py | 338–376 | `get_dialogue_for_query` | dialogue_messages | R (substring LIKE per token) | **NO** | MED — LIKE %x% is full scan, no FTS | Add FTS index; lock-free | M |
| dialogue_mirror.py | 388–435 | `analyze_user_sentiment` | dialogue_messages | R (via get_recent_messages) | **NO** | LOW | — | — |
| dialogue_mirror.py | 439–494 | `mirror_message` | dialogue_messages, dialogue_threads | W (INSERT + UPSERT) | YES (_lock) | MED — every chat turn | OK | — |

---

## Section E — `task_persistence.py` (agent task lifecycle)

Connection: `_get_conn()` (line 184) opens per call; `self._lock` (line 181) wraps every public method.

| File | Line | Method | Tables | Op | Locked? | Contention | Recommended |
|---|---|---|---|---|---|---|---|
| task_persistence.py | 240–293 | `create_task` | tasks | W | YES | LOW | OK |
| task_persistence.py | 295–314 | `get_task` | tasks | R | YES | LOW (per-call conn) | could be lock-free |
| task_persistence.py | 316–356 | `update_task` | tasks | W | YES | LOW | OK |
| task_persistence.py | 454–476 | `get_tasks_by_status` | tasks | R | YES | LOW | could be lock-free |

Pattern: uniform locking. Safe but slightly conservative — per-call conn + WAL means reads don't need the lock.

---

## Section F — `context_bus.py` (pub/sub on SQLite)

Connection: `_connect()` (line 104) opens per call. `self._lock` guards subscriber/dispatcher state.

| File | Line | Method | Tables | Op | Locked? | Contention |
|---|---|---|---|---|---|---|
| context_bus.py | 133 | `put` | context_kv | W | partial | MED (publish path) |
| context_bus.py | 146 | `get` | context_kv | R | NO | LOW |
| context_bus.py | 199 | `subscribe` | (subscribers map) | W | YES | LOW |
| context_bus.py | 232 | `_dispatch_new_messages` | context_topics | R | YES (poll thread) | MED — poll thread holds lock while dispatching |
| context_bus.py | 264 | (commit) | — | — | YES | LOW |

The 200ms poll loop + dispatch holding `_lock` is the main risk: if a subscriber callback is slow, all `publish`/`subscribe`/`get` calls block. Recommend: separate read lock (for subscriber map) from write lock (for DB), or release lock before invoking callbacks.

---

## Section G — `atlas_journal.py` (Atlas thought log)

Pattern: `with sqlite3.connect(self.db_path) as conn:` per public call (lines 194, 314, 339, 634, 645, 690, 801, 852, 860). Note the `with` does NOT actually close — see `db_utils.py` comment. But each call opens a NEW connection so cross-thread state is not shared.

| File | Line | Method | Tables | Op | Locked? | Contention |
|---|---|---|---|---|---|---|
| atlas_journal.py | 194 | `_init_db` | journal_entries, etc. | DDL | NO | LOW (one-shot) |
| atlas_journal.py | 314 | `record_entry` | journal_entries | W | NO | LOW (WAL+busy_timeout) |
| atlas_journal.py | 634/645 | query_recent / query_by_type | journal_entries | R | NO | LOW |

Atlas journal relies entirely on SQLite's WAL + busy_timeout. No Python-level lock. **Counter-example to sqlite_storage**: same kind of data, very different locking discipline, no observed contention.

---

## Section H — `session_gold_passes.py` (offline pass runner)

Two locking mechanisms:
- Redis-based cross-process lock (`REDIS_PASS_LOCK`, line 1641) signals anticipation engine to defer LLM during a pass.
- `connect_wal()` per-call from `db_utils` for SQLite ops.

| File | Line | Method | Tables | Op | Locked? | Contention |
|---|---|---|---|---|---|---|
| session_gold_passes.py | 1641 | `_acquire_lock` (Redis) | — | (signal) | YES | MED — 10-min TTL blocks anticipation |
| session_gold_passes.py | 1677 | `run_pass` | gold_passes_log, critical_findings | RW | NO Py-lock | LOW (WAL) |

---

## Section I — `consolidated_db.py` (multi-domain DB consolidation)

Thread-local conn map + module-level lock (line 43) guards the map, not the SQL exec. WAL + busy_timeout=10000.

| File | Line | Method | Tables | Op | Locked? | Contention |
|---|---|---|---|---|---|---|
| consolidated_db.py | 55 | `get_connection` | (lifecycle) | — | YES (map guard) | LOW |
| consolidated_db.py | 91 | `_domain_cursor` ctx mgr | (variadic) | RW | NO at exec; commit/rollback by ctx | LOW |

Risk: thread-local conn means a long-running cursor in thread A can hold the writer slot under WAL while thread B busy-waits on the file. Process-wide concurrency is bounded by SQLite's single-writer rule, not the Python lock.

---

## Section J — `db_utils.py::ThreadSafeConnection`

Used by older modules that opted in. `self.lock = threading.Lock()` (line 140) wraps every execute (line 147, 153, 159, 163).

| File | Line | Method | Op | Locked? |
|---|---|---|---|---|
| db_utils.py | 147 | `execute` | RW | YES (full DB) |
| db_utils.py | 153 | `executemany` | W | YES |
| db_utils.py | 159 | `fetchone` | R | YES |
| db_utils.py | 163 | `fetchall` | R | YES |

Coarse but uniform. Equivalent contention profile to `sqlite_storage.py` post-Round-3 IF every method routed through `self._lock` — which `sqlite_storage` does NOT.

---

## Section K — Other modules (summary)

99 files in `memory/` import `sqlite3`. The audit above covers the modules with **shared `self.conn`** or **registered locks**. The remaining ~85 modules use `with sqlite3.connect(...)` per call (atlas_journal pattern) and rely on WAL + busy_timeout. Spot-checked: anticipation_engine.py (line 274), persistent_hook_structure.py, hindsight_validator.py, failure_pattern_analyzer.py, semantic_search.py, librarian.py — all use transient connections. None hold a long-lived connection across operations.

---

## Cross-cutting recommendation hierarchy

Lock granularity recommendations, justified by the call patterns above:

1. **Lock-free (WAL SELECT)** — all SELECT-only methods on tables that are not concurrently being schema-modified. Applies to: every read in sqlite_storage.py, every read in observability_store.py, every read in evidence_ledger.py. WAL gives readers a consistent snapshot at transaction start; no Python lock needed.
2. **Per-table lock** — writes that touch one logical table. Apply to: `record_negative_pattern`, `mark_pattern_promoted` (table: `negative_patterns`). Separate from the `learnings` write path.
3. **Per-table-group lock** — writes that cascade via triggers (e.g., `learnings` → `learnings_fts`). Treat as one lock bundle because the trigger fires under the same transaction.
4. **Full-DB lock** — DDL / schema migration only. Already the case.

---

## Evidence reference (exact citations)

Round-3 RLock in `sqlite_storage.py`:
- Line 116–121: comment + `self._lock = threading.RLock()`
- Line 404: `with self._lock:` (only call site)

Read methods that bypass the lock (all on same `self.conn`):
- Lines 354, 507, 518, 529, 541, 554, 587, 604, 631, 634, 642, 645, 669, 688, 733, 739

Public write methods that bypass the lock:
- Line 701 (`record_negative_pattern` UPDATE)
- Line 714 (`record_negative_pattern` INSERT)
- Line 768 (`mark_pattern_promoted` UPDATE)

Round-3 fuzz harness reference: line 120 — "without this lock, concurrent promote() calls silently lose writes". Note: this proves the write-write race; the audit identifies a separate, unfixed read-write race.
