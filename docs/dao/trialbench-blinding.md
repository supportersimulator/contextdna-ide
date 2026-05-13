# Blinding Plan

**Authority:** HIGHEST (dao — invariant)

## Provenance

```
inbox/2026-05-04-trialbench-proposal.md
  →  vision/2026-05-04-trialbench-vision.md
  →  reflect/2026-05-06-trialbench-roadmap-audit.md
  →  reflect/trialbench-retroactive-audit.md
  →  dao (this doc)
```



## Agent-side blinding
Agents receive assigned inputs without condition labels and without being told the hypothesis.

## Adjudicator-side blinding
Run packages remove or mask:
- arm label,
- node identifier,
- ContextDNA packet source labels,
- experiment hypotheses,
- randomization metadata.

## Delayed unblinding
The adjudication table is locked before arm labels are reattached.

## Limitations
The agent may infer that it received more structured context. This is expected and cannot be fully blinded. The critical blind is outcome adjudication.
