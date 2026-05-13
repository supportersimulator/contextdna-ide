# Randomization Plan

**Authority:** HIGHEST (dao — invariant)

## Provenance

```
inbox/2026-05-04-trialbench-proposal.md
  →  vision/2026-05-04-trialbench-vision.md
  →  reflect/2026-05-06-trialbench-roadmap-audit.md
  →  reflect/trialbench-retroactive-audit.md
  →  dao (this doc)
```



## Design
Stratified rotating crossover assignment.

## Strata
- task family,
- task difficulty,
- node,
- model when applicable.

## Seed
The seed must be frozen in `trial_registry/ctxdna_trial_001.lock.json` before trial execution.

## Allocation concealment
Worker nodes receive only the concrete input packet, not the arm label.
