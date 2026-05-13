# Corrigibility Loop Algorithm (DAO — locked)

**Authority**: L4 DAO (highest, peer of `contextdna-product-boundaries.md`).
**Status**: Locked truth. Override only by explicit Aaron decree.
**Origin**: Wave C C3 stress-test, 2026-05-06. Empirically validated against ~dozen consensus rounds in current Atlas session.

---

## Why This Exists

The 3-Surgeons consensus loop is **not self-validating**. Surgeons confabulate. Specifically:

- **Neurologist (Qwen3-4B local)** confabulates fake file paths, fake commit IDs (e.g., "P1 fix #147"), and — without explicit code-context anchoring — drops into medical-domain answers (Cycle 10 finding).
- **Cardiologist (DeepSeek-chat / GPT-4.1-mini)** confabulates plausible-looking architectural narratives with high prose fluency.
- **Consensus** averages confidence; it does NOT verify specifics against ground truth.

Without an active testing loop driven by Atlas, the consensus surface degrades into theatre.

This document defines the **invariant** procedure that turns 3s consensus into an actually-corrigible signal.

---

## The Algorithm

```
INPUT: a claim C about the codebase or system state.

1. Atlas runs `3s consensus "<C>"`.
2. Capture cardio + neuro vote (agree/disagree/uncertain) + confidence (0.0-1.0).
3. If both AGREE high confidence (≥0.85):
   → spot-check 1 specific via grep/file-read before accepting.
4. If they DISAGREE:
   → drill the dissenting side: `3s ask-<side> "what specifically?"`.
5. Test EVERY specific claim (file path, line number, function name,
   commit hash, error string) against actual code:
   → `ls <path>`, `grep -n <pattern> <file>`, `git log --oneline | grep <hash>`,
     or read the file directly.
6. If a specific is wrong, do NOT immediately discard the dissent. Ask:
   → "Is there underlying validity?" (schema-drift, init-ordering,
     missing-injection, race, etc. — abstract pattern survives even when
     concrete coordinates are fabricated.)
7. Decision:
   → underlying validity present → fix THAT, not the named specific.
   → no underlying validity → REJECT dissent, log as confabulation.
8. Re-run `3s consensus` on the patched/clarified state.
9. Loop until either:
   → both surgeons ≥0.85 agree, OR
   → cardio explicitly says "NO REMAINING DEFECTS FOUND".
```

---

## Invariance Verdict

**The loop is CONDITIONAL-invariant, not unconditionally invariant.**

It is invariant **iff** Atlas executes step 5 (test every specific) on every cycle.

It BREAKS — silently — under at least these conditions:

| Failure mode | Trigger | Outcome |
|---|---|---|
| **A. Faith mode** | Atlas accepts surgeon dissent without grep-testing. | Loop diverges, chases ghost defects, wastes cycles on non-existent files. |
| **B. Anchor loss** | A 3s call omits "Software dev: Python superrepo …" framing. | Neuro answers in medical domain (concrete=construction, defects=neurological). Output looks confident, is content-irrelevant. |
| **C. Authority inversion** | Aaron over-rides surgeon consensus (e.g., ER Sim domain correction). | Atlas accepts immediately — by design. This is a feature, not a break, but it bypasses the loop. |
| **D. Specific-pattern collapse** | Both surgeons agree on a fake specific (e.g., file that doesn't exist). | Step 3 spot-check is the only catch. If skipped, fake travels downstream. |
| **E. Underlying-validity over-charity** | Atlas extracts "underlying" pattern from EVERY rejected specific. | Loop never terminates; every confabulation becomes "well, schema-drift IS a thing". |
| **F. Cost-cap exhaustion** | Repeated re-consensus runs hit the cost cap. | Loop truncates without convergence; Atlas ships on partial signal. |
| **G. Path divergence** | 3s plugin source exists in repo + marketplace + cache; testing wrong copy. | Surgeon "fixes" don't reach the loaded plugin. Verified via `which 3s` → cache path. |

**Therefore**: the loop's invariance lives in Atlas's discipline, not in the surgeons.

---

## Maximal-Invariance Enhancements (proposed)

Three concrete, bounded enhancements that would convert CONDITIONAL → STRUCTURAL invariance:

### 1. Auto-anchor (highest leverage)
Every `3s ask-local` and `3s consensus` call from this stack auto-prepends:

> `Software dev: Python superrepo at /Users/aarontjomsland/dev/er-simulator-superrepo. NOT medical, NOT construction. If you cannot answer in code-domain, say so.`

Closes failure mode B (anchor loss). Empirically: Scenario 3 produced a construction-defect answer; Scenario 1+2 produced code-shaped (still-confabulated) answers. Anchoring eliminates the worst confabulation class.

### 2. Auto-grep on specifics
When a surgeon names a specific (regex-detected: `path/like/this`, `line \d+`, commit hash `[a-f0-9]{7,}`, function name `\w+\(\)`), the bridge auto-runs `ls`/`grep`/`git log` and **annotates the surgeon's response** with `[VERIFIED]` or `[UNVERIFIED — file not found]` before returning to Atlas.

Closes failure mode D (specific-pattern collapse). Forces step 5 to fire even if Atlas forgets.

### 3. Auto-cite provenance tag
Every consensus result returns a `provenance:` field listing what was tested:

```
provenance:
  - tested_via: grep
    target: "scripts/3s-brainstorm.sh:217"
    result: "no race-condition primitives found"
  - tested_via: file-read
    target: "superrepo/config/settings.py"
    result: "FILE NOT FOUND"
```

Closes failure mode A (faith mode). Atlas cannot accept on faith if provenance is empty.

---

## Empirical Stress-Test Evidence (2026-05-06)

Reproduced confabulation in all 3 scenarios:

| Scenario | Anchoring | Output | Real? |
|---|---|---|---|
| 1 | Software dev + superrepo | `superrepo/core/models` ↔ `superrepo/utils/config` circular import | **NO** — `superrepo/` directory does not exist |
| 2 | Software dev + 5 commits + paths | `superrepo/requirements/core.txt`, `superrepo/config/settings.py`, commits `a3f91e2`, `bd7c3a1` | **NO** — files don't exist, commits don't exist |
| 3 | None | "Concrete defect = low compressive strength in construction concrete" | **NO** — wrong domain entirely |

Adversarial consensus tests:
- Self-referential "loop is invariant under all conditions" → **both disagree** (cardio 0.20, neuro 0.30). Healthy.
- Specific real-looking "line 217 race condition" → **both uncertain** (cardio 0.00, neuro 0.10). Line 217 is a printf, no race. Surgeons correctly refused to confirm without evidence.

Conclusion: **adversarial consensus** (challenge-style claims) is more robust than **generative ask-local** (open-ended specifics). Confabulation risk concentrates in `ask-local` step 4 of the algorithm.

---

## Locked. Do not modify without:
1. Aaron explicit decree, OR
2. New empirical evidence overriding the failure-mode table.
