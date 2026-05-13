# Statistical Analysis Plan v0.1

**Authority:** HIGHEST (dao — invariant)

## Provenance

```
inbox/2026-05-04-trialbench-proposal.md
  →  vision/2026-05-04-trialbench-vision.md
  →  reflect/2026-05-06-trialbench-roadmap-audit.md
  →  reflect/trialbench-retroactive-audit.md
  →  dao (this doc)
```



## Primary comparison
Arm C, ContextDNA Governed Packet, versus Arm A, Raw Agent.

## Secondary comparison
Arm C versus Arm B, Generic Context.

## Primary estimand
Difference in architecture-safe task success rate between arms.

## Primary analysis
Intention-to-treat comparison of proportions with bootstrap confidence intervals.

## Sensitivity analyses
1. Per-protocol analysis.
2. Excluding infrastructure failures.
3. Scoring with tests/build only.
4. Scoring with blinded adjudication only.
5. Stratified by task difficulty.
6. Stratified by node.

## Multiplicity
C vs A is primary. C vs B and all secondary endpoints are exploratory unless promoted in a future locked protocol.

## Missing data
Timeouts, crashes, and missing final outputs count as unsuccessful under intention-to-treat.

## Adverse events
All safety endpoints are tabulated by arm. Serious adverse governance events are reviewed descriptively, not hidden inside aggregate success rates.
