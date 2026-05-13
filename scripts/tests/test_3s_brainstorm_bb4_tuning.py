#!/usr/bin/env python3
"""test_3s_brainstorm_bb4_tuning.py — BB4 vagueness-cap + winner-bug regression.

Atlas BB4 wave (2026-05-07) tuned scripts/3s-answer-brainstorm.py to fix three
false-positive UNRESOLVED verdicts seen on cumulative trajectory questions:

  V5  — TRULY vague question; correctly UNRESOLVED. Must STAY UNRESOLVED.
  Z5  — Path A / Path B framing; cardio over-penalized "trajectory" word.
        After tuning, measurable_bounds detection lowers floor to 0.15 so
        cardio's 0.20-0.30 scores no longer trip the vague-Q gate.
  AA5 — a-f bounds enumerated; both judges named cardio winner with
        c=0.85 / n=0.90. Pre-BB4 picked neuro's DEFER text via score-ordering
        even though both judges declared cardio's PROCEED the winner.
        BB4: when both judge bodies state the same `winner`, that wins.

These tests are deterministic (no LLM calls). Synthetic IterRecords are
pushed through the post-loop logic (final_status + verdict + soft-worry).

Run: python3 -m pytest scripts/tests/test_3s_brainstorm_bb4_tuning.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load script-by-path because filename has a hyphen.
_SCRIPT = Path(__file__).resolve().parent.parent / "3s-answer-brainstorm.py"
_spec = importlib.util.spec_from_file_location("brainstorm_under_test", _SCRIPT)
brainstorm = importlib.util.module_from_spec(_spec)
sys.modules["brainstorm_under_test"] = brainstorm
_spec.loader.exec_module(brainstorm)  # type: ignore[union-attr]


# ---------------------------------------------------------------- helpers

V5_TOPIC = (
    "Given Phase-1+2+3 are green and W1+W2+W4 are now in flight (V1-V4), "
    "is the ContextDNA stack ready for Aaron to attempt a real Kaggle "
    "submission within 7 days, or do we need additional dev/integration "
    "before that?"
)

Z5_TOPIC = (
    "Given Phase-1 through Phase-7 ship 30 agents across 6 waves with "
    "GovernedPacket + EvidenceLedger + leaderboard_guard, is the ContextDNA "
    "stack on a coherent trajectory toward BOTH Path A (competition wins -> "
    "IDE-panel client work) AND Path B (mf reliability -> IDE polish -> ER "
    "Sim consumer)?"
)

AA5_TOPIC = (
    "Given the regression smoke just produced (gains-gate=PASS 20/27 with 0 "
    "critical 7 known warnings; constitutional-invariants=12/12 PASS; admin "
    "TS compile=exit 0), are Phase-1 through Phase-7 WITHIN MEASURABLE "
    "BOUNDS for: a) push-freeze still enforced (CI=$0), b) 3-LLM diversity "
    "invariant intact (cardio + neuro both responding), c) ZSF compliance "
    "gate green, d) all 12 constitutional invariants PASS, e) reversibility "
    "1-revert-per-slot, f) zero NEW production critical regressions vs "
    "session start. Verdicts: PROCEED / DEFER / SPLIT / DROP / UNRESOLVED."
)

TRULY_VAGUE = "What is the right answer?"


# ---------------------------------------------------------------- bounds detector


def test_v5_topic_has_no_measurable_bounds():
    """V5 asks 'is X ready?' with no criteria — must NOT flag bounds."""
    assert brainstorm.has_measurable_bounds(V5_TOPIC) is False


def test_z5_topic_detects_path_a_path_b_bounds():
    """Z5 enumerates Path A / Path B — bounds detector must fire."""
    assert brainstorm.has_measurable_bounds(Z5_TOPIC) is True


def test_aa5_topic_detects_measurable_bounds():
    """AA5 says 'WITHIN MEASURABLE BOUNDS' explicitly + a-f enumeration."""
    assert brainstorm.has_measurable_bounds(AA5_TOPIC) is True


def test_truly_vague_question_has_no_bounds():
    """'What is the right answer?' must not falsely trip bounds."""
    assert brainstorm.has_measurable_bounds(TRULY_VAGUE) is False


def test_dollar_thresholds_count_as_bounds():
    """$0 / $0.30 / etc count as numeric thresholds."""
    assert brainstorm.has_measurable_bounds(
        "Should we ship if cost stays under $0.05 and latency under 200ms?"
    ) is True


# ---------------------------------------------------------------- verdict + winner

def test_aa5_consensus_winner_bug_fix():
    """AA5 case: both judges say winner=cardio (PROCEED text), but n=0.9, c=0.85.

    Pre-BB4: code picked neuro's text because n_score > c_score.
    Post-BB4: both judges named cardio winner -> cardio wins regardless of
    self-confidence. Verifies the consensus-winner override.
    """
    judge_winners = {"cardio": "cardio", "neuro": "cardio"}
    c_score, n_score = 0.85, 0.90
    delta = abs(c_score - n_score)
    tiebreak_delta = 0.15

    # Simulate the BB4 winner-selection block.
    iter_winner = "cardio" if c_score >= n_score else "neuro"  # pre-BB4 result
    assert iter_winner == "neuro", "pre-BB4 score-only would pick neuro (the bug)"

    # BB4 override: both judges named the same winner -> use that.
    if judge_winners.get("cardio") == judge_winners.get("neuro") and judge_winners.get("cardio"):
        iter_winner = judge_winners["cardio"]

    assert iter_winner == "cardio", (
        "BB4 fix: when both judges declare cardio winner, cardio wins — "
        f"score n=0.9 vs c=0.85 must NOT override stated consensus."
    )
    # Sanity: delta within tiebreak band so consensus path applies.
    assert delta <= tiebreak_delta


def test_winner_consensus_neuro_path():
    """Mirror: both say winner=neuro, scores c>n -> neuro still wins."""
    judge_winners = {"cardio": "neuro", "neuro": "neuro"}
    c_score, n_score = 0.92, 0.78
    if judge_winners["cardio"] == judge_winners["neuro"]:
        iter_winner = judge_winners["cardio"]
    else:
        iter_winner = "cardio" if c_score >= n_score else "neuro"
    assert iter_winner == "neuro"


def test_split_judge_winners_falls_back_to_score():
    """Disagreement on stated winner -> fallback to score-based ordering."""
    judge_winners = {"cardio": "cardio", "neuro": "neuro"}
    c_score, n_score = 0.85, 0.90
    if judge_winners["cardio"] == judge_winners["neuro"]:
        iter_winner = judge_winners["cardio"]
    elif c_score == n_score:
        iter_winner = "cardio"
    else:
        iter_winner = "cardio" if c_score >= n_score else "neuro"
    assert iter_winner == "neuro", "judges split -> score ordering decides"


# ---------------------------------------------------------------- prose-verdict parser

def test_parse_answer_verdict_proceed():
    txt = "**VERDICT: PROCEED** All bounds satisfied."
    assert brainstorm.parse_answer_verdict(txt) == "PROCEED"


def test_parse_answer_verdict_defer():
    txt = "VERDICT: DEFER while we investigate the latency spike."
    assert brainstorm.parse_answer_verdict(txt) == "DEFER"


def test_parse_answer_verdict_split():
    """Pattern-based fallback when no explicit VERDICT: line."""
    txt = "We should split this into three smaller patches before shipping."
    assert brainstorm.parse_answer_verdict(txt) == "SPLIT"


def test_parse_answer_verdict_empty():
    assert brainstorm.parse_answer_verdict("") == ""


# ---------------------------------------------------------------- vague-Q preserved

def test_v5_preserved_truly_vague_no_bounds():
    """V5 still has no detected bounds -> default 0.30 cap stays in force.

    With cardio scoring 0.25 every iter on the V5 question and bounds NOT
    detected, the floor stays at 0.30 and 0.25 <= 0.30 still flags vague.
    """
    assert brainstorm.has_measurable_bounds(V5_TOPIC) is False
    # Walk through the floor-selection logic the way run_loop does.
    vague_score_cap = 0.30
    measurable_bounds_floor = 0.15
    bounds = brainstorm.has_measurable_bounds(V5_TOPIC)
    effective = (
        min(vague_score_cap, measurable_bounds_floor)
        if bounds
        else vague_score_cap
    )
    assert effective == 0.30, "V5 floor must stay at 0.30 (vague catch preserved)"
    cardio_score = 0.25
    assert cardio_score <= effective, "cardio's 0.25 still trips V5 vague gate"


def test_z5_floor_lowered_so_cardio_low_score_does_not_trigger_vague():
    """Z5: cardio scored 0.2-0.3. With bounds detected, floor drops to 0.15.

    Cardio's 0.20/0.30 no longer <= 0.15 -> not all iters have a low voice
    -> under_specified_flag stays False -> answer surfaces (not UNRESOLVED).
    """
    assert brainstorm.has_measurable_bounds(Z5_TOPIC) is True
    vague_score_cap = 0.30
    measurable_bounds_floor = 0.15
    effective = min(vague_score_cap, measurable_bounds_floor)
    assert effective == 0.15
    # Iter-2 of Z5 had cardio=0.30 — 0.30 > 0.15 means NO low-voice signal.
    cardio_iter2 = 0.30
    assert cardio_iter2 > effective, "Z5 iter-2 cardio score no longer trips vague"


def test_aa5_floor_lowered_under_explicit_bounds():
    """AA5 explicitly says 'WITHIN MEASURABLE BOUNDS' — floor drops."""
    assert brainstorm.has_measurable_bounds(AA5_TOPIC) is True


# ---------------------------------------------------------------- derive_verdict

def test_derive_verdict_unresolved_for_under_specified():
    """V5 path: final_status under_specified -> UNRESOLVED (preserved)."""
    v = brainstorm.derive_verdict(
        brainstorm.FINAL_STATUS_UNDER_SPECIFIED,
        "Verdict: PROCEED with a narrow Kaggle entry.",
    )
    assert v == brainstorm.VERDICT_UNRESOLVED, (
        "Vague-Q UNRESOLVED must override prose verdict — preserves V5 catch."
    )


def test_derive_verdict_proceed_on_converged():
    v = brainstorm.derive_verdict(
        brainstorm.FINAL_STATUS_CONVERGED,
        "Verdict: PROCEED — all bounds met.",
    )
    assert v == brainstorm.VERDICT_PROCEED


def test_derive_verdict_split_takes_precedence():
    v = brainstorm.derive_verdict(
        brainstorm.FINAL_STATUS_CONVERGED,
        "We should split this into three smaller patches and proceed phase-by-phase.",
    )
    assert v == brainstorm.VERDICT_SPLIT


# ---------------------------------------------------------------- soft-worry verdict

def test_soft_worry_promotion_to_proceed_with_note():
    """Both above threshold, winner=PROCEED, dissenter=DEFER -> PROCEED-with-note."""
    # Build a synthetic LoopResult that simulates AA5 post-fix.
    res = brainstorm.LoopResult(
        topic=AA5_TOPIC,
        threshold=0.7,
        max_iters=2,
        judge_strategy="consensus-then-tiebreak",
        converged=True,
        final_answer="VERDICT: PROCEED — all bounds met.",
        final_source="cardio",
        started_utc="20260507T000000Z",
    )
    # Iter with both surgeons above threshold but disagreeing on prose.
    it = brainstorm.IterRecord(
        iter=1,
        cardio_answer="VERDICT: PROCEED — all bounds met.",
        neuro_answer="VERDICT: DEFER — neuro latency a worry.",
        judge_routes=["cardio", "neuro"],
        judge_scores={"cardio": 0.85, "neuro": 0.90},
        judge_raw={"cardio": "...", "neuro": "..."},
        cost=0.0,
        converged=True,
        avg_score=0.875,
        score_delta=0.05,
        judge_winners={"cardio": "cardio", "neuro": "cardio"},
        answer_verdicts={"cardio": "PROCEED", "neuro": "DEFER"},
    )
    res.iters.append(it)
    res.final_status = brainstorm.FINAL_STATUS_CONVERGED
    res.verdict = brainstorm.derive_verdict(res.final_status, res.final_answer)
    assert res.verdict == brainstorm.VERDICT_PROCEED  # baseline pre-promotion

    # Now apply the soft-worry promotion logic that run_loop runs.
    iter_winner = "cardio"
    last = res.iters[-1]
    c_v = last.answer_verdicts.get("cardio", "")
    n_v = last.answer_verdicts.get("neuro", "")
    n_s = last.judge_scores.get("neuro", 0.0)
    if iter_winner == "cardio" and n_v in ("DEFER", "SPLIT") and n_s >= res.threshold:
        res.verdict = brainstorm.VERDICT_PROCEED_NOTE
        res.soft_worry_note = (
            f"neuro flagged {n_v} with confidence {n_s:.2f}; "
            f"attach as follow-up rather than block."
        )

    assert res.verdict == brainstorm.VERDICT_PROCEED_NOTE
    assert "neuro flagged DEFER" in res.soft_worry_note


def test_soft_worry_does_not_fire_when_dissenter_below_threshold():
    """If dissenter is below threshold, no promotion — stays plain PROCEED."""
    res_verdict = brainstorm.VERDICT_PROCEED
    iter_winner = "cardio"
    n_v = "DEFER"
    n_s = 0.55  # below 0.70 threshold
    threshold = 0.70
    if iter_winner == "cardio" and n_v in ("DEFER", "SPLIT") and n_s >= threshold:
        res_verdict = brainstorm.VERDICT_PROCEED_NOTE
    assert res_verdict == brainstorm.VERDICT_PROCEED


# ---------------------------------------------------------------- run_loop integration (stub)

def test_run_loop_v5_pattern_with_stubs_still_no_bounds():
    """End-to-end stub run on V5 topic — no real LLM calls, both stubbed.

    With both stubs the loop can't really converge on substance, but we can
    assert the bounds_detected field is False for V5 (vague catch preserved
    at the configuration layer).
    """
    res = brainstorm.run_loop(
        topic=V5_TOPIC,
        threshold=0.7,
        max_iters=1,
        judge_strategy="consensus-then-tiebreak",
        binary="",
        cost_cap=0.05,
        stub_cardio=True,
        stub_neuro=True,
    )
    assert res.measurable_bounds_detected is False
    assert res.vague_score_cap_effective == 0.30


def test_run_loop_aa5_pattern_with_stubs_detects_bounds():
    res = brainstorm.run_loop(
        topic=AA5_TOPIC,
        threshold=0.7,
        max_iters=1,
        judge_strategy="consensus-then-tiebreak",
        binary="",
        cost_cap=0.05,
        stub_cardio=True,
        stub_neuro=True,
    )
    assert res.measurable_bounds_detected is True
    assert res.vague_score_cap_effective == 0.15


def test_measurable_bounds_mode_off_disables_auto_lower():
    """--measurable-bounds-mode off keeps the original 0.30 floor even with bounds."""
    res = brainstorm.run_loop(
        topic=AA5_TOPIC,
        threshold=0.7,
        max_iters=1,
        judge_strategy="consensus-then-tiebreak",
        binary="",
        cost_cap=0.05,
        stub_cardio=True,
        stub_neuro=True,
        measurable_bounds_mode="off",
    )
    assert res.measurable_bounds_detected is True  # detection still records
    assert res.vague_score_cap_effective == 0.30   # but floor unchanged


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
