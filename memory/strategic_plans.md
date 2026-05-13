# Strategic Plans — ContextDNA Infrastructure

> **Access:** Atlas (direct), GPT-4.1 (via `strategic_analyst.py`), Qwen3-4B (read-only)
> **Maintainers:** Atlas, Claude Opus 4.6, GPT-4.1, Qwen3-4B
> **Last updated**: 2026-02-23 19:01 UTC by gpt-4.1
> **Scope:** ContextDNA, memory infrastructure, webhook pipeline, scheduler, LLM team
> **See also:** `strategic_plans_ersim.md` for ER Simulator plans

---

## 1. Active Threads

### 1.1 Unified Resource Arbitration (LLM/Scheduler/GPU)
**Status:** ACTIVE  
**Key Files:**  
- `memory/llm_priority_queue.py`
- `memory/lite_scheduler.py`
- `context-dna/src/context_dna/brain.py`
**Summary:**  
Infra stabilization and arbitration code landed. Scheduler fixed (39 jobs LITE mode, 2026-02-16). 3 worst jobs fixed (cursor_refresh, injection_health, watchdog_failsafe — was 4,250 failures/day → <100). Cross-resource throttling/preemption incomplete. GPU lock has stale-PID auto-steal. Hybrid routing live (local-first, external fallback).

---

### 1.2 Webhook Pipeline & SOP Injection Architecture
**Status:** ACTIVE  
**Key Files:**  
- `memory/persistent_hook_structure.py`
- `memory/anticipation_engine.py`
- `memory/redis_cache.py`
- `scripts/auto-memory-query.sh`
- `memory/SOP_WEBHOOK_INJECTION_ARCHITECTURE.md`
**Summary:**  
All 9 sections implemented and operational. S2 (professor), S6 (Synaptic→Atlas), S8 (Synaptic→Aaron) use pre-compute via anticipation engine. S5 (protocol), S7 (full library) use reasoning chains. S0 (safety) runs critical findings + scheduler health. S10 (strategic) via GPT-4.1 analyst. Overflow protection: progressive strip S7→S4→S3→S1→S5, never strip S0/S2/S6/S8/S10. ≤5 word gate at all entry points.

---

### 1.3 Surgery Team Phases — Documentation & Persistence
**Status:** ACTIVE  
**Key Files:**  
- `memory/strategic_analyst.py`
- `memory/session_historian.py`
**Summary:**  
Phases A/C/D/E not session-persistent post-Phase B; risk of lost learnings. Immediate: persist/document all phases, ensure historian and plan sync, recoverability for phase transitions.

---

### 1.4 Panel Protocol v1 & IDE Ecosystem
**Status:** ACTIVE  
**Key Files:**  
- `admin.contextdna.io/lib/ide/panels/`
- `admin.contextdna.io/lib/ide/panelRegistry.ts`
- `admin.contextdna.io/lib/ide/eventBus.ts`
**Summary:**  
Panel manifest, sandboxing, extension loader operational. Security/permission gating, integration providers, and security review remain open.

---

### 1.5 Workspace Scoping & Reasoning Chains (Qwen3-4B Butler)
**Status:** ACTIVE  
**Key Files:**  
- `memory/strategic_analyst.py`
- `context-dna/src/context_dna/brain.py`
- `memory/session_historian.py`
**Summary:**  
Qwen3-4B butler integration live. Resource impact and stability not fully assessed.

---

## 2. Architecture Map

- **Webhook Engine:** `memory/persistent_hook_structure.py`
- **Scheduler:** `memory/lite_scheduler.py`
- **LLM Priority Queue:** `memory/llm_priority_queue.py`
- **Anticipation Engine:** `memory/anticipation_engine.py`
- **Strategic Analyst:** `memory/strategic_analyst.py`
- **Session Historian:** `memory/session_historian.py`
- **Redis Cache:** `memory/redis_cache.py`
- **Professor:** `memory/professor.py`
- **Brain:** `context-dna/src/context_dna/brain.py`
- **Butler Watchdog:** `scripts/butler-watchdog.sh`
- **Synaptic:** `memory/synaptic_chat_server.py`, `memory/synaptic_service_hub.py`

---

## 3. Dead Code & Debt

1. **Legacy xbar plugin:** `context-dna/core/clients/xbar/context-dna.1m.py`
2. **Unused CLI entry points:** `pattern_manager.py`, `bugfix_sop_enhancer.py`
3. **Barrel exports:** `admin.contextdna.io/lib/ide/panels/index.ts`
4. **20 Logos agents:** `memory/agents/`
5. **220 raw sqlite3.connect calls:** Should use `db_utils.connect_wal()`
6. **agent_service FD leak:** `infra/agent_service/`, `services/agent_service.py` — monitor post-fix

---

## 4. Blind Spots

1. **Scheduler coordinator**: launchd KeepAlive auto-recovers, but singleton PID guard can stall if PID file stale. Monitor.
2. **Unified resource starvation:** B4 cache awareness partial; full arbitration not landed.
3. **Session historian DB**: `.session_history.db` was empty (0 bytes) on 2/24 — gold extraction may not be persisting. Investigate.
4. **GPU lock orchestration:** Redis fallback fragile, fairness missing.
5. **SQLite concurrency:** Singleton fix helps, but `.observability.db` risk persists.
6. **IDE integration providers:** Stubs; security review needed.
7. **SQL LIMIT removals:** Batch size increases risk unbounded fetches.
8. **PG catalog corruption**: Happened 2/23 — direct pg_attribute row deletion (not ALTER TABLE). 3 tables affected. Rebuilt 2/24 via DROP+CREATE. GORM AutoMigrate recovered. Record as SOP: never directly edit pg_catalog.
9. **Acontext data volatility**: All tables had 0 rows when rebuilt. No persistent user data stored — purely ephemeral session/task tracking.

---

## 5. Connections

1. **Resource contention:** Scheduler, LLM queue, GPU lock—systemic arbitration gap.
2. **Webhook reliability:** Overflow, stale anticipation cache, and quality degradation persist.
3. **Monitoring fragility:** Health check false positives and FD leaks expose observability gaps.
4. **Session recovery:** Improvements reduce context loss, but phase-level documentation lags.
5. **Workspace scoping:** Qwen3-4B butler—potential for improved reasoning chains.
6. **Strategic plan drift:** Plan updates outpace implementation, especially for SOPs and phase persistence.

---

## 6. Next Priorities (updated 2026-03-05)

### 6.1 Phase 2 Action Items (Aaron-directed, 2026-03-05)

**IMMEDIATE:**
1. **POST /api/mode security fix** — `agent_service.py:4695` and `:4733` bypass mode_authority. Wire through `mode_authority.set_mode()`. 3-surgeon consensus confirmed. Small fix, high value.
2. **FLC monitoring skills** — Cardiologist (GPT-4.1-mini) to receive 3 new skills: semantic drift detection, token truncation verification, cold-start audit. Cross-exam in progress.

**SHORT-TERM:**
3. **LPC vector implementation** — Model names + inference backends as dynamic secrets (env vars). See `docs/future_upgrades.md` Section 5.
4. **SQLite concurrency migration** — ~269 raw sqlite3.connect calls → db_utils. Batch 1 done, 3 remaining.
5. **V12 Action Fragmentation** — 85% done, wire atlas-ops.sh + scheduler as gate.

**FUTURE UPGRADES (documented in `docs/future_upgrades.md`):**
6. Multi-machine / distributed operation (Section 6)
7. Multi-user auth & identity — OAuth/LDAP (Section 7)
8. Safety standards — "freedom via constraint" (Section 8)
9. IDE / UI / UX vision — Electron hybrid recommended by 3-surgeon cross-exam (Section 9)
10. FLC monitoring — hold-for-strategy items (Section 10)

### 6.2 Cross-Exam Results Archive (2026-03-05)

Results in `/tmp/atlas-agent-results/` (volatile — key findings summarized here):

| Cross-Exam | File | Key Finding |
|---|---|---|
| POST /api/mode security | cross_exam_1772723728.json | Bypass confirmed at agent_service.py:4695,:4733. All 3 surgeons: wire through mode_authority. |
| UI/UX + Safety | cross_exam_1772723995.json | Electron hybrid recommended. "Freedom via constraint" safety model. Flutter/Qt raised as alternatives. |
| FLC skills | cross_exam_1772769548.json | 3 skills designed. Both surgeons: thresholds arbitrary, 50-sample underpowered. Implement observation-only first, calibrate from data. |

### 6.3 Prior Priorities (Phase C, 2026-02-23)

1. Persist and document all Surgery Team phases (A/C/D/E)
2. Audit 9 sections of SOP_WEBHOOK_INJECTION_ARCHITECTURE.md
3. Unblock scheduler arbitration
4. Panel Protocol v1 — finalize SDK, security review
5. Workspace scoping, reasoning chain refinement

---

## 7. Archived

1. **Phase A: Clean Slate + Resource Mastery** — stale findings purge (1064), external call caching, resource arbitration [DONE 2026-02-23]
2. **Phase B: Infrastructure Stabilization** — FD leak fix, resource arbitrator, health checks, cache wiring, thinking mode SSoT [DONE 2026-02-23]
3. **Autonomous A/B Testing** — 3-surgeon consensus, auto-revert [DONE 2026-02-22]
4. **GPU Lock resilience** — Redis fallback, cross-process yielding [DONE 2026-02-18]
5. **Surgery Team of 3** — hybrid LLM operational [DONE 2026-02-18]
6. **S10 Strategic Analyst** — pre-compute, hourly rewrite [DONE 2026-02-18]
7. **Deep Scan Pipeline** — codebase review, 10 batches [DONE 2026-02-17]
8. **Overflow Protection** — progressive strip order [DONE 2026-02-17]
9. **Injection depth cycling** — token savings [DONE 2026-02-17]
10. **Session historian DB integrity** — pre-checks [DONE 2026-02-16]
11. **Evidence grade ladder** — UP-only logic [DONE 2026-02-16]
12. **Atomic writes and install locks** — critical ops [DONE 2026-02-16]
13. **Comprehensive logging** — major workflows [DONE 2026-02-16]