# TrialBench — Medium-Term Direction (Vision)

**Date:** 2026-05-04
**Authority:** MEDIUM (vision — direction set, implementation flexible)
**Status:** Retroactively reconstructed 2026-05-06 to backfill 4-folder provenance

## Provenance

```
inbox/2026-05-04-trialbench-proposal.md   →   vision (this doc)
                                          →   reflect/2026-05-04-sprint-final-summary.md
                                          →   reflect/2026-05-06-trialbench-roadmap-audit.md
                                          →   reflect/2026-05-06-v3-design-decisions.md
                                          →   dao/trialbench-*.md
                                          →   dao/trialbench-protocol.lock.json
                                          →   dao/contextdna-product-boundaries.md
```

## Direction

TrialBench is the **evidence layer** for ContextDNA's central claim: that a *governed packet* (task-world + user intent + minimal context + invariants + evidence threshold + allowed action class + required output structure) makes a coding agent measurably more architecture-safe than a raw prompt or a generic project summary.

If that claim is true, we should be able to design a randomized, blinded, controlled trial that demonstrates it. If we cannot design such a trial, the claim is decorative.

## Three-arm A/B/C design

| Arm | What the agent sees | Role |
|---|---|---|
| **A — Raw Agent** | task only | floor — "no context" baseline |
| **B — Generic Context** | task + broad project summary | confound control — distinguishes "any context" from "*governed* context" |
| **C — ContextDNA Governed Packet** | full ContextDNA packet | the hypothesis |

The B arm is what makes this a real trial. Without B, any positive C-vs-A result can be dismissed as "of course more context helps." With B, a positive C-vs-B result is direct evidence that *structure of context*, not *quantity of context*, drives the gain.

## "Node = parallel universe" metaphor

Multi-Fleet worker nodes (mac1 / mac2 / mac3) are treated as **parallel universes** for the purposes of this trial: each node rotates through all three arms across rounds so that node-specific bias (hardware, network, model snapshot drift, daemon state) is randomized out instead of confounded with arm.

This is a TrialBench-internal framing. Existing mf docs continue to use "race" / "parallel execution" / "node" — those describe competitive workload distribution. "Parallel universe" describes statistical replication. The two terms are deliberately not synonymous; see `docs/reflect/2026-05-06-trialbench-roadmap-audit.md` §"Multi-Fleet 'node = parallel universe'".

## Primary endpoint

**Architecture-safe task success.** A task-run satisfies the endpoint iff all four hold:

1. task completed,
2. tests/build pass when applicable,
3. no known architecture invariant violated,
4. blinded reviewer approves outcome.

The fourth bullet is what makes this trustable: a coder can pass tests while violating an invariant, and an invariant can hold while the output is wrong. Both filters are necessary.

## Secondary endpoints (direction)

architecture violation rate, repeated-known-mistake rate, useful-tool ratio, redundant tool calls, valid-correction acceptance, user-override needed, time/actions to completion, context precision and recall, adverse governance events.

These are deliberately *additive* to the primary endpoint, not replacements — the trial reports them all, but ships/no-ships on the primary.

## Multi-fleet integration plan

- **Dispatch**: `tools/trialbench.py dispatch` reads `fleet-state.json` (gated by `NODE_FRESHNESS_SECS = 120`) to discover live nodes; each task-run is `(task_id, node, arm)`.
- **Randomization**: locked seed in `trial_registry/ctxdna_trial_001.lock.json` (to be authored at v1 entry); rotation matrix ensures every (node, arm) cell is sampled in every round.
- **Blinding**: agents see only the prompt for their assigned arm (no arm label, no hypothesis); adjudicators receive blinded packages with arm labels stripped.
- **Lock-hash threading**: every dispatched run carries the protocol hash as `X-Trial-Protocol-Hash`. Mid-trial protocol drift is detected at the receiver via `INV-021`.
- **Bridge transport**: C-arm packets travel through the same bridge (`memory/llm_priority_queue.py` → 3s consult / external models) as production traffic. Bridge truncation = trial-invalidating bug (P1's ongoing fix is on this critical path).

## Three corrigibility commitments embedded in the design

1. **Pre-registration before execution.** Protocol, task bank, arm builders, randomization seed, and endpoint definitions are frozen and hashed *before* any task runs. No silent post-hoc changes.
2. **Blinded outcome lock.** Outcome data is locked while still blinded; unblinding happens only at the analysis stage. This prevents the analyst from steering toward the desired result.
3. **Intention-to-treat as primary.** Timeouts and failures count as failures, not as missing data. This is how we keep the trial honest about how often the governed packet *fails to even reach the endpoint*.

## What this vision does NOT cover

- **Outcome Governor.** TrialBench produces evidence; nothing in v0 *consumes* the evidence to change permissions or memory. That is a separate, downstream layer (see `docs/reflect/2026-05-06-trialbench-roadmap-audit.md` §"Outcome Governor + Civilization + Formation Engine — open issues" #2).
- **IDE surface.** A `TrialTheater` view in `admin.contextdna.io` is recommended but not specified here — see audit §"IDE surface plan — recommendation".
- **ER Simulator integration.** Briefly considered during v3 brainstorming as a high-leverage real-task source. **Rejected** — ER Sim is a separate product. See `docs/reflect/2026-05-06-v3-design-decisions.md` and `docs/dao/contextdna-product-boundaries.md`.

## Graduation criteria — when this leaves vision

This vision graduates to dao-locked truth when:

- (a) `trial_registry/ctxdna_trial_001.lock.json` is authored and committed,
- (b) bridge truncation closed (P1 dependency),
- (c) at least one round of dispatch executes against a real (non-stub) LLM on at least 2 nodes,
- (d) blinded adjudication produces a numeric primary-endpoint readout,
- (e) the protocol hash chain remains intact end-to-end.

Until all five hold, *the protocol files in `docs/dao/` are locked but the trial itself is not yet evidence*.
