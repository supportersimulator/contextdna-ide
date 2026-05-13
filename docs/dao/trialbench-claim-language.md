# TrialBench Claim Language — LOCKED

**Authority:** HIGHEST (dao — invariant)
**Status:** LOCKED. The verbatim caveat block below MUST appear in every TrialBench manuscript. Modifications require an explicit Aaron-signed superseding dao entry.

## Provenance

```
inbox/2026-05-04-trialbench-proposal.md
  →  vision/2026-05-04-trialbench-vision.md
  →  reflect/2026-05-04-sprint-final-summary.md
  →  reflect/2026-05-06-trialbench-roadmap-audit.md
  →  reflect/2026-05-06-v3-design-decisions.md
  →  D-batch gap analysis 2026-05-06 (D5 lifted from per-file string)
  →  dao (this doc)
```

## Why this lock exists

D5's gap analysis on the original 396KB Metis-and-TrialBench chat (2026-05-06) verified that:
- chat L569 mentioned "30 minimum" runs without a power calculation (corrigibility-flagged)
- chat repeatedly stressed "do NOT claim FDA-grade — only EBM principles"
- the v0 implementation preserved this caveat at `tools/trialbench_score.py:84-90` as the constant `CAVEAT_BLOCK_VERBATIM`
- per-file persistence is fragile; one misguided edit could shed the honest framing

Lifting the caveat to a dao entry makes the claim language **invariant across implementations** — any future scorer rewrite, manuscript template change, or AI-IDE-agent edit must reference this dao truth.

## The verbatim caveat (CANONICAL)

The following text MUST appear, unchanged, in every TrialBench manuscript:

```
This trial uses synthetic blinded-reviewer scoring (no human adjudication).
Results demonstrate pipeline functionality, not ContextDNA efficacy. Real
efficacy claims require human-blinded adjudication and pre-registered
replication. Do NOT cite as 'FDA-grade' or any regulatory standard — only
Evidence-Based Medicine *principles*.
```

## Implementation contract

- `tools/trialbench_score.py` exports `CAVEAT_BLOCK_VERBATIM` (string constant)
- Manuscript generator unconditionally renders the constant in §5
- When `synthetic_reviewer_used=True` (any run with no human reviewer), an additional `synthetic_reviewer_used=YES` line is auto-appended
- Tests verify the string is unchanged (golden-master snapshot in `tools/test_trialbench_score.py::test_manuscript_contains_caveat_and_endpoint`)

## Enforcement

This dao entry is the canonical source. If the implementation drifts:
- The string constant in `tools/trialbench_score.py` is wrong
- Tests should fail
- The fix is to restore the constant from this dao doc, NOT to update this doc

The dao text is authoritative. The Python constant is a mirror.

## Forbidden manuscript phrases

These phrases MUST NOT appear in any TrialBench manuscript without preceding human peer-review:

- ❌ "FDA-grade"
- ❌ "FDA-equivalent"
- ❌ "regulatory-compliant"
- ❌ "clinical-grade evidence"
- ❌ "publication-ready"
- ❌ "statistically significant" (only when n is below the SAP-prespecified power threshold)

## How to override

Same as `contextdna-product-boundaries.md`:
1. Explicit Aaron decision (chat-message or commit-signed) stating the boundary is being relaxed for a *specific, scoped* purpose
2. New superseding dao entry with provenance trail showing what changed and why
3. Update the corresponding test in `tools/test_trialbench_score.py`

Until all three hold, this lock is invariant.

## Cross-refs

- Implementation: `tools/trialbench_score.py:84-90` (`CAVEAT_BLOCK_VERBATIM`)
- Original chat residue: `~/Downloads/Context-DNA-Metis-and-TrialBench__chat.md` line ~11154 (Aaron's FDA/EBM rigor question), L11992 ("EBM principles, not regulatory")
- D5 corrigibility analysis: `/tmp/d5-gap-analysis.md`
- Sister dao: `docs/dao/contextdna-product-boundaries.md`
- Honest framing principle: `docs/dao/corrigibility-loop-algorithm.md`
