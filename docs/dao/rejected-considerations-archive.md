# REJECTED-PER-DAO Considerations Archive

**Authority**: dao-supplemental — NOT a lock; reference archive for revisit decisions
**Status**: append-only ledger of items rejected per current dao locks, with explicit revisit-conditions
**Purpose**: Aaron's directive 2026-05-06 — "anything rejected per dao, document to revisit later"

## How to use this doc

Each item below was rejected in the D-batch gap analysis (5 agents reviewing the 396KB Metis-and-TrialBench chat against shipped reality + Aaron-locked dao truths).

Rejection is **not permanent**. Each item has a `revisit_when` field listing the conditions under which the item could be reconsidered. When those conditions hold, a future Aaron decision can:
1. Update or supersede the relevant dao lock
2. Add the item back to the active backlog
3. Reference this archive entry's `id` in the new plan

## Sources

- D1-D5 gap analysis: `/tmp/d{1-5}-gap-analysis.md`
- Original chat: `~/Downloads/Context-DNA-Metis-and-TrialBench__chat.md` (12251 lines)
- Locked truths that drove rejection:
  - `docs/dao/contextdna-product-boundaries.md` (ER-Sim separation, IDE = revenue)
  - `docs/dao/corrigibility-loop-algorithm.md` (3s confab pattern documented)
  - `docs/dao/trialbench-claim-language.md` (no FDA claims)
  - `docs/dao/trialbench-protocol.lock.json` (locked v0 protocol)

---

## Archive entries

### R1. TS sibling repo `contextdna-outcome-governor`

- **Source**: chat L832, L875-877 (D2)
- **What chat proposed**: Standalone TypeScript repo with `src/{ledger,scoring,teaching,governor,fidelity,schemas}/`, `packages/{adapters,api,dashboard}/`, full monorepo
- **Why rejected**: Splits product surface; conflicts with dao §2 (IDE = THE revenue product). Atlas shipped Python-in-superrepo at `memory/{outcome,permission}_governor.py` which supersedes the TS sketch
- **revisit_when**: ContextDNA IDE has shipped to first 100 paying customers AND a non-Python language adapter (e.g., for embedded clients) has measurable demand AND there's budget for a TS team

### R2. Standalone repo `contextdna-teaching-governor`

- **Source**: chat L1218-1259 (D1)
- **What chat proposed**: Separate TypeScript monorepo with 7 dashboard panels, 6-level self-government taxonomy
- **Why rejected**: Same as R1 — violates dao §2. Already realized as compressed Python module per Permission Governor (R3 commit acf18535 + 0a9ceb5a)
- **revisit_when**: Same as R1 + Permission Governor ledger has >10,000 entries with quantified influence-tier transitions

### R3. Multi-IDE adapter scaffolding

- **Source**: chat L1259 (D1)
- **What chat proposed**: Adapters for `claude-code`, `opencode`, `github`, `local-llm`
- **Why rejected**: Dilutes IDE-as-primary-surface narrative; ContextDNA IDE is the proprietary revenue product. Multi-IDE adapters open-source the value
- **revisit_when**: ContextDNA IDE has clear product-market fit AND market signal that customers want OUR governor running INSIDE their other IDEs (likely never; more probable that ContextDNA IDE absorbs them)

### R4. 3-Surgeons influence-weighted voting (`decideFromWeightedVotes`)

- **Source**: chat L1208-1217 (D2)
- **What chat proposed**: Per-domain influence weights modify surgeon vote contributions
- **Why rejected — strongest reject**: DIRECTLY conflicts with `corrigibility-loop-algorithm.md`. Surgeons confabulate (Cycle 10 + multiple Wave verifications); weighting amplifies bad signal. Counter-position skill exists *because* trust-on-faith is the failure mode
- **revisit_when**: 3-distinct-LLM Constitutional Physics #5 is HARDWARE-real (mac3 MLX live, not DeepSeek-fallback) AND a longitudinal calibration dataset (>1000 consensus calls with verified outcomes) shows weighting does NOT amplify confabulations

### R5. governedWriteFile / governedPromoteMemory wrappers

- **Source**: chat L1631-1645 (D2)
- **What chat proposed**: Agents must NOT have direct write access; every privileged op wrapped
- **Why rejected**: Conflicts with CLAUDE.md "FULL PERMISSIONS" — Atlas-as-developer needs direct write for productive work. Rate-of-progress argument
- **revisit_when**: ContextDNA IDE serves ≥100 untrusted-developer agents AND there's evidence that one bad action damaged shared state (Atlas alone, in-house, doesn't trigger this)

### R6. 10-service-orders taxonomy (Sovereignty Kernel → Wisdom Pattern Keeper)

- **Source**: chat L5300-5800 (D3)
- **What chat proposed**: 10 named orders: Sovereignty Kernel, Ministry Circuit, Adjudication Court, Metacognitive Arbiter, Constellation Council, Node Sovereign, Evolution Lab, Emergency Order, Seraphic Helper, Wisdom Pattern Keeper
- **Why rejected**: Supersedes 3-Surgeons (which IS empirically validated by this session) without empirical replacement. Conflicts with token-conservation (CLAUDE.md). Aesthetic, not load-bearing
- **revisit_when**: 3-Surgeons is empirically demonstrated insufficient (e.g., consensus repeatedly wrong on cross-validated outcome data) AND a smaller order-set (3-5) outperforms 3-Surgeons on the same TrialBench benchmark

### R7. 100-phase roadmap as normative

- **Source**: chat L5900-7400 (D3) + roadmap doc in TS scaffold
- **What chat proposed**: 10 phases × 10 sub-items = 100 numbered tasks treated as plan
- **Why rejected**: Importing creates 100 ghost-tasks competing with measured priorities. The roadmap is brainstorm-fodder, not commitment. roadmap-audit (P2) already noted this
- **revisit_when**: Aaron explicitly asks for the 100-phase plan as a Q-by-Q roadmap (in which case decompose into 4 quarterly OKR sets, not 100 tasks)

### R8. "Local Universe / Civilization" framing

- **Source**: chat L5050-5400 (D3)
- **What chat proposed**: Multi-layer civilization model with "ContextDNA Ecosystem" as the Local Universe layer
- **Why rejected**: Re-blends what `contextdna-product-boundaries.md` §2 explicitly separates (IDE = revenue, mf+3s = FREE/open). Civilization framing dissolves the boundary
- **revisit_when**: dao §2 is superseded by an Aaron-signed entry merging the products

### R9. Agents-teaching-agents recursive formation

- **Source**: chat L9410, 9756-9779 (D4)
- **What chat proposed**: Agent-to-agent peer pedagogy where teaching others ascends the teacher
- **Why rejected**: Value-drift risk. The bounded version (one Atlas teaches via cross-exam) is already in 3-Surgeon protocol. Recursive teaching opens drift surface area without empirical bound
- **revisit_when**: There's a measurement framework that catches value-drift in <1 cross-exam round AND a sandbox for the recursive variant

### R10. TasteCircuit / Beauty as runtime score

- **Source**: chat L9410-9540 (D4)
- **What chat proposed**: Runtime "felt gracefulness: 0.88" score added to evaluation
- **Why rejected — second-strongest reject**: "Felt gracefulness" is unfalsifiable LLM self-rating. Violates Constitutional Physics #3 (Evidence Over Confidence). The `simplify` + `architectural-gate` skills already cover the falsifiable aspects
- **revisit_when**: A measurement framework exists where "beauty" correlates with later-validated outcome quality (none currently does — even human aesthetics correlate weakly with code quality)

### R11. 10-level Ascension expanding PermissionTier

- **Source**: chat L9170-9220 (D4)
- **What chat proposed**: 10 ascension levels (Raw → Wisdom Pattern Contributor)
- **Why rejected**: 4-tier PermissionTier (advisory/trusted/lead/restricted) shipped in `memory/permission_governor.py` is sufficient for a 5-agent fleet. 10-level over-provisions
- **revisit_when**: Fleet expands to >50 agents AND outcome-ledger evidence shows the 4 tiers are too coarse-grained to differentiate empirically

### R12. ER Simulator seeding TrialBench tasks/Arm B

- **Source**: chat (proposed by Cardio+Neuro convergence in v3 brainstorm)
- **What chat proposed** (and 3s converged on): wire ER Sim's 39 waveforms + phase scenarios into TrialBench's task_bank
- **Why rejected**: Aaron's direct correction during v3 — ER Sim is a separate product (`docs/dao/contextdna-product-boundaries.md` §1+§3). Atlas accepted correction immediately; this is the canonical corrigibility-moment evidence
- **revisit_when**: ER Sim is sold/spun-off as a distinct entity (in which case ContextDNA IDE could legitimately consume its public API as a third-party adapter)

### R13. TS scaffold reconstruction in parallel to Python

- **Source**: chat throughout (~20 file paths cited)
- **What chat proposed**: Build out the TS scaffold's 30+ files alongside our Python ship
- **Why rejected**: Sketch not contract. The TS scaffold was a brainstorm, not a spec. Our Python ship IS the canonical surface. Building in parallel would burn cycles for a code path that may never run
- **revisit_when**: Same as R1+R3 (multi-language adapter clearly demanded by paying customers)

### R14. Pre-baked "68% regression reduction" investor metrics

- **Source**: chat L2355 (D1)
- **What chat proposed**: Use the 68% number for investor demos
- **Why rejected**: Confabulated. Only TrialBench output is normative for any external claim. `claim-language.md` dao entry locks this
- **revisit_when**: Real TrialBench n≥30 + human-blinded adjudication produces the 68% (or any) number with bootstrap CI

### R15. Shedding CAVEAT_BLOCK_VERBATIM

- **Source**: D5 finding
- **What was at risk**: per-file persistence of the FDA-grade caveat could be lost via a single edit
- **Why rejected**: Honest framing must persist. Per `docs/dao/trialbench-claim-language.md` (now locked), the caveat is dao truth, not implementation detail
- **revisit_when**: Real human-blinded adjudication AND IRB or equivalent approves a different caveat shape

---

### R16. RiskLane-replaces-PermissionTier (single-axis governance)

- **Source**: MavKa diff doc `~/Downloads/OpenPath_Context-DNA-MavKa.md` L2589-2627 + `route_action()` stub L2921+
- **What chat proposed**: `route_action(action, envelope, context)` short-circuits gate decisions to RiskLane only (`if assessment.lane <= 1: return "execute"`), bypassing agent-trust dimension entirely
- **Why rejected**: E3 verdict — Risk Lane and `PermissionTier` are orthogonal axes (action-axis vs agent-axis). Conflating them silently drops the agent-trust signal that `permission_governor.py` derives from outcome evidence. Cross-node 3s confirmed: mac2 weighted +1.00, mac1 chief +1.00 on the orthogonality interpretation specifically — the replacement interpretation is REJECT
- **revisit_when**: A measurement framework demonstrates that a single-axis gate (lane-only OR tier-only) outperforms the dual-axis AND-gate on the same TrialBench benchmark (with bootstrap CI showing the win is not noise). Until then, both axes apply to every action

### R17. Lane-0 read-only doorway as standalone product

- **Source**: MavKa diff doc L595-648 (read-mostly Telegram doorway proposal)
- **What chat proposed**: `/status /today /memory /nodes /ask-chief /plan` exposed via Telegram bot as separate revenue surface
- **Why deferred (not rejected — RESERVE-condition-gated)**: E1+E2+E3 agreed the Lane-0 read-only concept is sound IF routed THROUGH ContextDNA IDE (channel-agnostic), not as standalone repo. Cross-node 3s green (mac2 +0.74, mac1 chief +1.00). But shipping it now would (a) split product surface per dao §2 (R1/R13 pattern), (b) precede IDE multi-tenant which doesn't exist yet
- **revisit_when**: ContextDNA IDE ships multi-tenant (i.e., serves 2+ users with isolated workspaces) AND there's customer signal that remote read-only access to fleet state from a non-IDE surface would close a deal. At that point: ship as IDE-mediated channel-agnostic read-only API (NOT as separate Telegram bot)

---

## Forbidden re-introductions

A future agent (Atlas or otherwise) attempting to ship any of R1-R17 without:
1. An Aaron-signed superseding dao entry, AND
2. The corresponding `revisit_when` condition demonstrably met,

is **violating the dao** and should self-block via the corrigibility-loop algorithm step 1 (counter-position before action).

The corrigibility-loop algorithm is itself locked at `docs/dao/corrigibility-loop-algorithm.md`.

## Audit trail

| Date | Author | Change |
|---|---|---|
| 2026-05-06 | Atlas (Claude Opus 4.7) | Initial archive — 15 D-batch REJECT-PER-DAO entries |
| 2026-05-08 | Atlas (Claude Opus 4.7) | E-batch additions: R16 RiskLane-replaces-PermissionTier + R17 Lane-0 doorway RESERVE — both backed by cross-node 3s verdict (mac2 + mac1 chief converged) |

## How to revisit

To resurrect an item:
1. Cite the entry's R-number (R1-R15) in the proposing dao entry
2. Demonstrate the `revisit_when` condition with evidence (commit hashes, metric snapshots, dataset sizes)
3. Show the Aaron decision that authorizes the supersede
4. Update this archive's audit trail with the resurrection event
