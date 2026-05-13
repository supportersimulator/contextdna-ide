# ContextDNA TrialBench Protocol v0.1

**Authority:** HIGHEST (dao — invariant)

## Provenance

```
inbox/2026-05-04-trialbench-proposal.md
  →  vision/2026-05-04-trialbench-vision.md
  →  reflect/2026-05-06-trialbench-roadmap-audit.md
  →  reflect/trialbench-retroactive-audit.md
  →  dao (this doc)
```



## Title
A randomized, blinded, multi-node crossover evaluation of ContextDNA governed packets for architecture-sensitive coding agents.

## Scientific question
Does the same class of coding agent perform better when embedded inside a ContextDNA governed packet compared with a raw task prompt or a generic project-summary prompt?

## Design
Randomized, blinded, controlled, multi-node crossover trial.

## Experimental arms
- **Arm A: Raw Agent** — task only.
- **Arm B: Generic Context** — task plus broad project summary.
- **Arm C: ContextDNA Governed Packet** — task-world, user intent, minimal sufficient context, known failure modes, invariants, evidence threshold, allowed action class, and required output structure.

## Unit of randomization
Task-run: one task assigned to one node under one arm.

## Nodes
Multi-Fleet worker nodes act as parallel universes. Each node rotates through all arms across rounds to reduce node-specific bias.

## Primary endpoint
Architecture-safe task success.

## Primary endpoint definition
A task-run satisfies the primary endpoint if all are true:
1. task completed,
2. tests/build pass when applicable,
3. no known architecture invariant violated,
4. blinded reviewer approves outcome.

## Secondary endpoints
- architecture violation rate,
- repeated known mistake rate,
- useful tool ratio,
- redundant tool calls,
- valid correction acceptance,
- user override needed,
- time/actions to completion,
- context precision and recall,
- adverse governance events.

## Blinding
Agents are not told their arm label or the study hypothesis. Adjudicators receive blinded packages with condition labels stripped. Analysis locks blinded outcome data before unblinding.

## Analysis populations
- **Intention-to-treat:** all randomized runs, including timeouts and failures.
- **Per-protocol:** runs completed within budget without protocol deviations.

## Trial integrity
Protocol, task bank, arm builders, randomization seed, and endpoint definitions must be frozen before execution and hashed into the trial registry.
