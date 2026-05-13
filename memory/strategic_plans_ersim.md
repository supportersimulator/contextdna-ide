# Strategic Plans — ER Simulator

> **Access:** Atlas (direct), GPT-4.1 (via `strategic_analyst.py`), Qwen3-4B (read-only)
> **Maintainers:** Atlas, Claude Opus 4.6, GPT-4.1, Qwen3-4B
> **Last updated**: 2026-02-23 19:01 UTC by gpt-4.1

---

## 1. Active Threads

### 1.1 Monolith-to-Module Migration (ER Sim Builder)
**Status:** STALLED  
**Key Files:**  
- `simulator-core/er-sim-monitor/temp-check/Code.js`
- `simulator-core/er-sim-monitor/temp-restore/Ultimate_Categorization_Tool_Complete.js`
- `simulator-core/er-sim-monitor/temp-restore/Multi_Step_Cache_Enrichment.js`
- `simulator-core/er-sim-monitor/temp-restore/ATSR_Title_Generator_Feature.js`
**Summary:**  
Migration abandoned mid-transition; no recent commits. UI-dependent logic blocks automated testing. Assign owner, set milestone, or formally sunset to prevent codebase rot.

---

### 1.2 AI Category Suggestion Approval & Smart Duplicate Detection
**Status:** ACTIVE  
**Key Files:**  
- `simulator-core/er-sim-monitor/temp-restore/Ultimate_Categorization_Tool_Complete.js`
- `simulator-core/er-sim-monitor/testing/QUALITY_ANALYSIS_SUMMARY.md`
**Summary:**  
Approve/reject workflow live; only exact hash matches blocked. Semantic/medical near-duplicate detection not implemented. Next: add semantic similarity.

---

### 1.3 Voice AI Stack (LiveKit/Kyutai TTS/ECS)
**Status:** ACTIVE  
**Key Files:**  
- `ersim-voice-stack/`
- `backend/sim/prompts.py`
- `docs/aws-ec2-ec2-ecs.md`
**Summary:**  
Self-hosted, scalable voice stack. Streaming TTS and local LLM proxy not implemented. TURN/NAT traversal and TLS termination require more testing.

---

### 1.4 Web App (Next.js / Django / Supabase)
**Status:** ACTIVE  
**Key Files:**  
- `web-app/`
- `backend/`
**Summary:**  
Next.js frontend, Supabase auth, Django backend, scenario API clients. JWT in localStorage—must move to httpOnly cookies + CSRF. Security hardening required. Scenario data flows: Apps Script → Django → Next.js.

---

## 2. Dead Code & Debt

1. **Chatterbox TTS:** Remove all references in `ersim-voice-stack/`.
2. **Fargate LiveKit Server configs:** Remove ECS taskdefs/scripts for LiveKit on Fargate.
3. **ALB configs for voice stack:** Only NLB is valid for UDP/WebRTC.
4. **Old TTS adapters:** Remove unused REST wrappers (Fish Speech, Zonos, etc.).
5. **OpenAI Realtime pipeline:** Remains as fallback; ensure new infra uses LiveKit.
6. **Legacy/duplicate functions in Apps Script monolith:** E.g., multiple `getRecommendedFields`, legacy ATSR save.
7. **Modular extracts not referenced:** Clarify/remove partial/test-only files in `temp-restore/`.

---

## 3. Blind Spots

1. **Security — API Keys in Apps Script:** Keys in Settings/PropertiesService need rotation and access control.
2. **JWT in web-app:** Stored in localStorage; must move to httpOnly cookies + CSRF.
3. **Testing Gaps:** No automated regression for Apps Script; UI-dependent functions untested.
4. **Duplicate Detection (Medical Nuance):** Only exact hash matches blocked; semantic/medical near-duplicates not detected.
5. **Service Discovery Drift:** Cloud Map DNS for ECS containers must be kept in sync.
6. **OpenAI API retry/backoff:** Apps Script API calls lack robust retry/backoff.
7. **Migration drift:** Abandoned code paths and partial modules risk codebase rot.

---

## 4. Next Priorities

1. **Persist and document all Surgery Team phases (A/D/C/E)** — update strategic_plans_ersim.md and learnings.
2. **Unblock Monolith-to-Module Migration** — assign owner, set milestone, or sunset abandoned code.
3. **Implement Smart Duplicate Detection** — semantic similarity + medical nuance.
4. **Audit and clarify all migration code paths** — remove or wire up partial modules in `temp-restore/`.
5. **Streaming TTS & Local LLM Proxy** — lower latency, more robust voice stack.
6. **Harden web-app security** — httpOnly cookies, CSRF, API key rotation.
7. **Service Discovery & TLS Hardening** — Cloud Map DNS sync, TLS termination.

---

## 5. Archived

1. **Dynamic Header Resolution** — batch/categorization/pathway functions use header cache [DONE 2026-02-15]
2. **Crash-Proof Batch Processing** — output sheet is source of truth [DONE 2026-02-15]
3. **5-Layer Safety Validation** — pathway changes require multi-layer validation [DONE 2026-02-14]
4. **Prompt Sharing Between Django and Agent** — unified simulation prompt [DONE 2026-02-14]
5. **Separation of Concerns in Scenario Pipeline** — categories/pathways managed independently [DONE 2026-02-14]