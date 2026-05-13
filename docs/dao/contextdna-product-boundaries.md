# ContextDNA Product Boundaries — LOCKED

**Authority:** HIGHEST (dao — invariant per Aaron's direct correction, 2026-05-06)
**Status:** LOCKED. Do not mutate without an explicit Aaron-signed superseding dao entry.

## Provenance

```
inbox/2026-05-04-trialbench-proposal.md
  →  vision/2026-05-04-trialbench-vision.md
  →  reflect/2026-05-04-sprint-final-summary.md
  →  reflect/2026-05-06-trialbench-roadmap-audit.md
  →  reflect/2026-05-06-v3-design-decisions.md   ← evidence for this lock
  →  dao (this doc)
```

This dao entry is *not* a TrialBench artifact. It is a separate, additive lock created during the v3 batch to record a product-strategy boundary that emerged when the 3-Surgeons quorum proposed an integration Aaron rejected as out-of-scope. See `docs/reflect/2026-05-06-v3-design-decisions.md` for the corrigibility moment evidence.

## Locked truths

### 1. ER Simulator is a separate project

**ER Simulator** (`simulator-core/er-sim-monitor/`, `er_simulator_app/`, etc.) is a **separate product** from the ContextDNA stack. It happens to live in the same superrepo for operational convenience; it is not part of the ContextDNA IDE, ContextDNA TrialBench, or ContextDNA Outcome Governor.

ER Simulator characteristics that make it categorically distinct:

- Different user base (medical-simulation users, not developer-tool users)
- Different revenue model
- Different liability surface (medical-accuracy non-negotiable; cf. CLAUDE.md "ER Simulator" section)
- Different invariant set (39 canonical `_ecg`-suffixed waveforms, `<1% CPU`, 8/8 guard)
- Different domain (clinical scenarios, not coding agents)

### 2. ContextDNA IDE is the revenue product (within the ContextDNA stack)

Within the ContextDNA stack, the **ContextDNA IDE** is the revenue product. Multi-Fleet and 3-Surgeons are FREE/open. TrialBench is the evidence layer for the IDE's central claim. Outcome Governor / Civilization Runtime / Formation Engine are downstream layers of the IDE, not separate products.

### 3. Do not blend ER Simulator with ContextDNA

Specifically forbidden as default behavior, absent a future Aaron-signed superseding lock:

- ❌ Wiring ER Simulator event streams into TrialBench task generation
- ❌ Using ER Simulator's invariant guard as TrialBench's validation criteria
- ❌ Surfacing ER Simulator UI inside the ContextDNA IDE
- ❌ Routing ER Simulator audio/waveform data through ContextDNA evidence ledgers
- ❌ Citing ER Simulator's medical-accuracy posture as evidence of ContextDNA IDE quality
- ❌ Counting ER Simulator engagement metrics in ContextDNA IDE KPIs

### 4. What is allowed

- ✅ Both projects living in the same superrepo
- ✅ Sharing low-level *infra* utilities (e.g., shared logging, shared `.venv`, shared fleet daemon for ops only)
- ✅ Separate documentation trails (ER Sim docs do not pollute ContextDNA dao; ContextDNA dao does not assume ER Sim primitives)
- ✅ Independent release cadences
- ✅ **Shared AWS EC2 (login + user-management plane only) — budget-pragmatic reuse, NOT architectural coupling.** Aaron explicitly avoided spinning up a new EC2 for ContextDNA IDE remote-login until budget allows. The reuse is bounded to auth/user-mgmt; ER Sim's medical assets must not be touched, overridden, or polluted. When budget allows, ContextDNA IDE migrates to its own EC2 and this shared surface goes away.
- ✅ **ER Sim as a *target project* the ContextDNA IDE will eventually be used to build** — same relationship the IDE will have with any external repo a developer opens. May surface as a Dockview panel/tab labeled clearly as a distinct workspace. This is "IDE-targets-project," NOT "IDE-embeds-project."

### 4a. EC2-share guardrails (Aaron 2026-05-06 clarification)

The shared EC2 instance for login/user-management is the ONLY infrastructure piece both projects touch today. Rules:

- ContextDNA IDE auth code MUST NOT modify ER Simulator tables, secrets, IAM roles, or routes
- ER Simulator routes/handlers MUST NOT be aware of ContextDNA IDE callers
- Any code path that grew out of "we already had EC2 for ER Sim, let's piggyback" must be tagged with `# EC2-shared: budget-pragmatic, migrate when funded`
- When ContextDNA IDE moves to its own EC2: tag becomes the cleanup checklist; nothing else carries over

### 5. How to override this lock

This lock is overridable, but only by:

1. An explicit Aaron decision (chat-message or commit-signed) stating the boundary is being relaxed for a *specific, scoped* purpose.
2. A new dao entry that supersedes this one, with provenance trail showing what changed and why.
3. Updates to `memory/north_star.py` reflecting the change, since rank ordering would shift.

Until all three hold, this lock is invariant.

## Why this lock exists (one paragraph)

During v3 brainstorming, both 3-Surgeons (Cardiologist + Neurologist) independently converged with weighted confidence +1.00 on a recommendation to wire ER Simulator scenarios into TrialBench. The mechanical leverage was real — ER Sim's 39 canonical waveforms and phase-based scenarios are genuinely the most authentic real-world task source in the superrepo. But the surgeons could not see what Aaron sees: that ER Sim is a separate product with a separate user base, separate liability surface, and separate revenue model. Blending them would have entangled two businesses for one technical convenience. Aaron caught the violation; Atlas accepted the correction without defending the consensus; this lock is the durable artifact so future agents do not re-walk the same loop.

## Aaron 2026-05-06 follow-up clarification

Atlas's original lock (above) was correct in spirit but too binary. Aaron clarified the actual relationship more precisely:

> "The only aspect of the ER Simulator that can potentially be considered an actual part of ContextDNA IDE is the EC2 aws for the login and user etc because i didn't want to spend more money yet on new EC2 etc for remote login etc until we have a budget for it- so we reused just a little of the EC2 that was the ER Simulator aws location but we were very careful not to override or ruin any of the ER Simulator project along the way. The ER Simulator project is meant to be one of the first actual projects the ContextDNA IDE will be used to build (perhaps it can have it's own panel or something within the dockview etc or something (not important- just meant to ensure it is actually separate like working on a distinct repo etc)"

This clarification adds two specific rules to the lock (captured in §4 + §4a):

1. **Shared EC2 = budget-pragmatic, not architectural** — the auth/user-mgmt EC2 surface is the lone exception; cleanup-tagged for eventual migration.
2. **ER Sim is a *target* of the IDE, not an *embedding* in it** — same relationship as any external repo. May get its own Dockview panel as a distinct workspace, but architecturally separate.

Net effect: the §3 forbidden list still holds. §4 gains two explicit allowances. ER Sim and ContextDNA stack remain categorically separate products.

## Cross-refs

- Evidence: `docs/reflect/2026-05-06-v3-design-decisions.md`
- Public-repo policy: user memory `project_public_repo_org.md` (multifleet + 3-surgeons FREE; ContextDNA IDE = revenue product)
- ER Simulator invariants: `CLAUDE.md` §"ER SIMULATOR" + `CLAUDE-reference.md`
- v3 design doc: `docs/plans/2026-05-06-v3-priorities-design.md`
- TrialBench locked surface (untouched by this lock): `docs/dao/trialbench-*.md` + `docs/dao/trialbench-protocol.lock.json`
