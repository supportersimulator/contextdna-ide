# TrialBench — Thesis

**Status:** v0 demo locked (2026-05-06).
**Lock:** [`trial_registry/ctxdna_trial_001.lock.json`](../../trial_registry/ctxdna_trial_001.lock.json)
**Demo:** `bash scripts/demo-trialbench.sh [--dry-run]`
**Author:** Atlas (M2 scaffold). Pending Aaron countersign.

---

## What TrialBench is

TrialBench is the **evidence layer** of Context DNA. Where the rest of the
stack moves agents (Multi-Fleet), debates with them (3-Surgeons), and watches
them (the IDE), TrialBench is the part that *measures whether any of it makes
the agents better*. It is a randomized, blinded, multi-node crossover trial —
the same statistical machinery a clinical trial uses, applied to coding agents.

The substance lives in `docs/dao/`:

- `trialbench-protocol.md` — three arms (`A_raw`, `B_generic_context`,
  `C_governed`), one task per node per arm per round, primary endpoint
  *architecture-safe task success*.
- `trialbench-randomization.md` — stratified rotating crossover, seed locked
  in the trial registry.
- `trialbench-sap.md` — pre-registered statistical analysis: ITT comparison
  of proportions with bootstrap CI, sensitivity analyses listed up front.
- `trialbench-blinding.md` — agents and adjudicators don't see the arm label.
- `trialbench-endpoints.md` / `trialbench-adjudication.md` — exact definitions
  of "success" and exactly when a human reviewer must override the heuristic.

The whole bundle is hashed (`trial_protocol_hash` =
`6c72130f331d451cfe54a4bc60edc79d75cdd744da011338bdb4c6f5bc4721f6`) and that
hash rides on every dispatch as the `X-Trial-Protocol-Hash` header. INV-021
(`memory/invariants.py`) refuses any trial-class action proposal that doesn't
carry it. That's the chain of custody.

---

## Where TrialBench fits in Aaron's big picture

The locked sequence in `memory/north_star.py` is:

> Multi-Fleet → 3-Surgeons → Context-DNA-IDE → full-local-ops → ER Simulator.

TrialBench is the **proof loop** that runs through all five.

| North-star rank | TrialBench role |
|---|---|
| 1. Multi-Fleet | Randomization unit: each fleet node = one parallel arm assignment per round. The fleet dispatcher (`tools/trialbench.py`) round-robins task×arm assignments across whatever nodes `fleet-state.json` says are fresh. |
| 2. 3-Surgeons | Adjudicators: blinded packets go to the surgeon trio for outcome scoring once an adjudicator UI ships. v0 uses a synthetic reviewer and surfaces that fact in every report. |
| 3. Context-DNA-IDE | Surface: TrialTheater (recommended in `docs/reflect/2026-05-06-trialbench-roadmap-audit.md`) is the user-facing dashboard — live arm grid, dispersion CI strip, blinded adjudication queue. This is what Aaron clicks on. |
| 4. Full-local-ops | Consumer: `memory/outcome_governor.py` ingests scored outcomes into `memory/outcome_ledger.jsonl`, an append-only fcntl-locked JSONL. The ledger is what the gold-mining and SOP-update loops read to decide whether to promote a memory or change a permission. **Without the ledger, scores are decorative; with it, scores become governance.** |
| 5. ER Simulator | The eventual *real-world test bed*. ER Simulator's medical-accuracy invariants are the gold standard for what `architecture-safe task success` means in a domain Aaron actually shipped. |

The revenue path is the same path. Multi-Fleet reliability sells the fleet
SDK. 3-Surgeons sells the consensus tool. The IDE sells the visibility.
TrialBench is what makes the visibility *credible* — when the IDE shows
"Arm C wins by 23 points, CI [12, 31]", buyers can trust it because the
protocol was hashed and frozen before the trial ran.

---

## Why locked + reproducible matters

Constitutional Physics #1 says **same inputs → same output**. Constitutional
Physics #4 says **outcomes = truth**. TrialBench is the join of those two.

If the protocol drifts mid-trial — a task swapped, a scorer threshold bumped,
a randomization seed re-rolled because the first batch looked unflattering —
then the trial measured whatever the experimenter wanted it to measure, and
the score is noise. This is the failure mode every honest clinical trial
guards against and every benchmark eventually slides into. We pay the cost of
a frozen lockfile up front so the score has bite.

The lock is enforced three ways:

1. **Protocol hash** (`docs/dao/trialbench-protocol.lock.json`) — sha256 over
   the seven dao files. Every dispatch carries the hash; INV-021 in
   `memory/invariants.py` rejects any action proposal that doesn't.
2. **Trial registry** (`trial_registry/ctxdna_trial_001.lock.json`) — frozen
   task selection, frozen RNG seed, frozen node intent, frozen arm definitions.
   `locked: true` and `approved_by` baked in.
3. **Append-only outcome ledger** (`memory/outcome_ledger.jsonl`) — fcntl-locked
   writes, idempotent on `(task_id, arm, trial_id)`. You can audit every score
   that ever flowed into a permission change.

The synthetic-reviewer flag is the canary. The v0 scorer falls back to a
heuristic when no human adjudication is available, and `synthetic_reviewer_used`
must be `true` on the report. The demo script's invariant gate fails loud if
that flag goes missing — i.e., if a future change quietly hides "we faked the
human review" inside an aggregate.

---

## v0 vs the roadmap

### What v0 ships

- 5 frozen tasks (`arch_001, _002, _004, _005, _007`) — chosen from the 10-task
  bank to span 3 task families × 2 difficulty levels. Cheap (each is a single
  Anthropic Messages dispatch, max 1024 output tokens).
- 2 arms exercised: `A_raw` (control) and `C_governed` (Context-DNA packet from
  `tools/trialbench_packet.py`). 10 dispatches per trial.
- Hardened dispatcher (`tools/trialbench.py`) — `/health` pre-flight, fleet-state
  node discovery, retry-on-429, allows single-node smoke for offline demos.
- Scorer + bootstrap CI + manuscript generator (`tools/trialbench_score.py`) —
  ITT primary contrast, six sensitivity analyses, prereg-style markdown writeup.
- Outcome governor ingest (`memory/outcome_governor.py`, shipped 2026-05-06) —
  every scored outcome lands in the evidence ledger, idempotent.
- One-button demo runner (`scripts/demo-trialbench.sh`) — registry validation,
  dispatch, score, ingest, invariant gate. Idempotent, dry-run by default safe.

### What v0 does *not* yet have (acknowledged, not hidden)

| Gap | Where it gets closed |
|---|---|
| Arm B (`B_generic_context`) live runs | v1 — needs the packet builder's B-arm composer wired into the dispatcher. Currently the dispatcher only emits A and C. |
| Real human blinded adjudication | v2+ — the synthetic reviewer is a placeholder. Adjudicator UI / blinded-package generator is unspecified in any plan. |
| Stratified RNG shuffle | v1 — the registry locks a seed, but the dispatcher currently round-robins (no Python `random` call). Seed is reserved. |
| Cross-node real dispatch | Blocked on (a) bridge-truncation fix (P1's current work, commit `2fd01457c`) and (b) mac3 reactivation. v0 demo runs single-node with a logged warning that crossover validity is reduced. |
| TrialTheater IDE surface | 1.5d build estimated in the roadmap audit. This is what makes TrialBench a *visible product feature* rather than infra. |
| Outcome → SOP-update loop | The ingest is shipped; the *consumer* loop (read ledger → decide whether to update an SOP) is not. Currently the ledger fills up and nobody reads it. |
| Arm-effect adjustment for node | Statistical: with single-node smoke, you can't separate "Arm C is better" from "the one node we used had a tailwind". Multi-node real runs fix this. |

These are deferred *with explicit named owners* in the roadmap audit and the
trial registry's `v1_deferred` field. None of them is hidden.

### Cost envelope

A single demo run is bounded by 10 Anthropic Messages calls, max 1024 output
tokens each, with a Sonnet-class model (alias `claude-sonnet-4-6` resolved via
the local bridge). Conservative envelope:

- 10 calls × ~2k input tokens × $3/Mtok input  ≈ $0.06
- 10 calls × ~1k output tokens × $15/Mtok output ≈ $0.15
- **Per-run total: ~$0.20** (plus dispatcher overhead on the local fleet,
  which is free).

`--dry-run` mode exercises the full code path with zero network spend — the
demo Aaron clicks on while camping costs $0.

---

## What "good" looks like at v1

A run of `bash scripts/demo-trialbench.sh` should produce:

1. A fresh `trial_id` under `artifacts/trialbench/`.
2. A scored manuscript with non-zero `synthetic_reviewer_used=false` (i.e., a
   real adjudicator weighed in).
3. A primary contrast (Arm C vs Arm A) with a bootstrap CI that excludes 0 in
   the direction the thesis predicts (C > A).
4. An idempotent ingest into `memory/outcome_ledger.jsonl`.
5. At least one downstream consumer (governor SOP-update loop, IDE TrialTheater)
   that *visibly does something with the score* — the score moves a permission,
   pulses the dashboard, or updates a memory.

Until point 5 lands, TrialBench is a beautiful cathedral with no congregation.
The point is to build the congregation.

---

## Pointers

- Lock: [`trial_registry/ctxdna_trial_001.lock.json`](../../trial_registry/ctxdna_trial_001.lock.json)
- Protocol: [`docs/dao/trialbench-protocol.md`](../dao/trialbench-protocol.md)
- Roadmap audit: [`docs/reflect/2026-05-06-trialbench-roadmap-audit.md`](../reflect/2026-05-06-trialbench-roadmap-audit.md)
- Dispatcher: [`tools/trialbench.py`](../../tools/trialbench.py)
- Scorer + manuscript: [`tools/trialbench_score.py`](../../tools/trialbench_score.py)
- Packet builder: [`tools/trialbench_packet.py`](../../tools/trialbench_packet.py)
- Outcome governor: [`memory/outcome_governor.py`](../../memory/outcome_governor.py)
- Demo runner: [`scripts/demo-trialbench.sh`](../../scripts/demo-trialbench.sh)
- Scaffold audit: [`.fleet/audits/2026-05-04-M2-trialbench-v0-scaffold.md`](../../.fleet/audits/2026-05-04-M2-trialbench-v0-scaffold.md)
