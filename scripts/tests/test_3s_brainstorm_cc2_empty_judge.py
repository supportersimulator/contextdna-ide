#!/usr/bin/env python3
"""test_3s_brainstorm_cc2_empty_judge.py — CC2 empty-judge probe robustness.

Atlas CC2 wave (2026-05-07) hardened scripts/3s-answer-brainstorm.py against
the empty-judge-response failure mode that BB5 surfaced (iter-2 neuro returned
empty stdout under load → was scored 0.0 → contaminated consensus arithmetic).

Layered fix:
  L1  Retry-once on empty judge body (judge_retry_total counter).
  L2  MISSING_DATA sentinel when both attempts empty (score = MISSING_SCORE,
      excluded from convergence math, surfaces as PROBE_FAILURE final_status).
  L3  judge_empty_response_total{judge=label} counter + ERR_LOG entry.

These tests are deterministic — they do NOT call real LLMs. The judge call
path is monkey-patched to inject empty/non-empty bodies on demand.

Run: python3 -m pytest scripts/tests/test_3s_brainstorm_cc2_empty_judge.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "3s-answer-brainstorm.py"
_spec = importlib.util.spec_from_file_location("brainstorm_under_test_cc2", _SCRIPT)
brainstorm = importlib.util.module_from_spec(_spec)
sys.modules["brainstorm_under_test_cc2"] = brainstorm
_spec.loader.exec_module(brainstorm)  # type: ignore[union-attr]


# ----------------------------------------------------- _is_empty_judge_body

def test_is_empty_judge_body_empty_string():
    assert brainstorm._is_empty_judge_body("") is True


def test_is_empty_judge_body_whitespace_only():
    assert brainstorm._is_empty_judge_body("   \n\t ") is True


def test_is_empty_judge_body_real_content():
    assert brainstorm._is_empty_judge_body('{"confidence": 0.9}') is False


def test_is_empty_judge_body_none_safe():
    """ZSF: None must not crash the predicate."""
    assert brainstorm._is_empty_judge_body(None) is True  # type: ignore[arg-type]


# ----------------------------------------------------- MISSING_DATA sentinels

def test_missing_data_is_real_string_constant_not_magic_number():
    """MISSING_DATA must be a typed sentinel, not int/float."""
    assert isinstance(brainstorm.MISSING_DATA, str)
    assert brainstorm.MISSING_DATA == "MISSING_DATA"


def test_missing_score_is_distinguishable_from_real_scores():
    """MISSING_SCORE < 0.0 so any real confidence (0.0-1.0) is distinct."""
    assert brainstorm.MISSING_SCORE < 0.0


def test_final_status_probe_failure_constant_exists():
    assert hasattr(brainstorm, "FINAL_STATUS_PROBE_FAILURE")
    assert brainstorm.FINAL_STATUS_PROBE_FAILURE == "probe_failure"


# ----------------------------------------------------- derive_verdict + PROBE_FAILURE

def test_derive_verdict_probe_failure_returns_unresolved():
    """PROBE_FAILURE always maps to UNRESOLVED — caller knows by final_status."""
    v = brainstorm.derive_verdict(
        brainstorm.FINAL_STATUS_PROBE_FAILURE,
        "Verdict: PROCEED — looks fine.",  # prose ignored
    )
    assert v == brainstorm.VERDICT_UNRESOLVED


# ----------------------------------------------------- judge call retry harness
#
# The brainstorm script invokes call_3s(binary, route, prompt) for judges.
# We monkey-patch call_3s to drive empty/non-empty/sequential responses.


class FakeCallSequence:
    """Deterministic response sequencer keyed by (route, call_index)."""

    def __init__(self, responses_by_route):
        # responses_by_route: dict[route] -> list[CallResult]
        self.responses_by_route = responses_by_route
        self.call_counts = {r: 0 for r in responses_by_route}

    def __call__(self, binary, route, prompt, timeout=90):
        seq = self.responses_by_route.get(route, [])
        idx = self.call_counts.get(route, 0)
        self.call_counts[route] = idx + 1
        if idx >= len(seq):
            # Default: empty rc=0 (simulate persistent failure if exhausted)
            return brainstorm.CallResult(
                body="", cost=0.0, rc=0, raw="", route=route
            )
        return seq[idx]


def _mk(body, cost=0.0, rc=0):
    return brainstorm.CallResult(body=body, cost=cost, rc=rc, raw=body, route="test")


HEALTHY_DRAFT_CARDIO = _mk(
    "Cardio's draft answer. Verdict: PROCEED — all bounds met. Cost: $0.0001",
    cost=0.0001,
)
HEALTHY_DRAFT_NEURO = _mk(
    "Neuro's draft answer. Verdict: PROCEED — same conclusion.\nCost: $0.0001",
    cost=0.0001,
)
HEALTHY_JUDGE_CARDIO = _mk(
    '{"confidence": 0.85, "winner": "cardio", "reason": "Both align."}',
    cost=0.00005,
)
HEALTHY_JUDGE_NEURO = _mk(
    '{"confidence": 0.90, "winner": "cardio", "reason": "Cardio crisper."}',
    cost=0.00005,
)
EMPTY_JUDGE = _mk("", cost=0.0, rc=0)


# ----------------------------------------------------- L1 retry


def test_neuro_empty_first_attempt_retries_and_succeeds(monkeypatch):
    """Neuro judge returns empty on attempt 1, valid on attempt 2.

    Expected:
      - judge_retry_total == 1
      - judge_empty_response_total["neuro"] == 1 (first attempt only)
      - converged True (both judges above threshold via consensus path)
      - NO probe_failure_reason
    """
    seq = FakeCallSequence({
        # Drafts: cardio + neuro each called once per iter; with max_iters=1,
        # one draft per surgeon is enough.
        "ask-remote": [
            HEALTHY_DRAFT_CARDIO,  # cardio draft
            HEALTHY_JUDGE_CARDIO,  # cardio judge
        ],
        "ask-local": [
            HEALTHY_DRAFT_NEURO,   # neuro draft
            EMPTY_JUDGE,           # neuro judge attempt 1 → empty
            HEALTHY_JUDGE_NEURO,   # neuro judge attempt 2 → valid
        ],
    })
    monkeypatch.setattr(brainstorm, "call_3s", seq)

    res = brainstorm.run_loop(
        topic="Should we ship if cost stays under $0.05?",  # has bounds
        threshold=0.7,
        max_iters=1,
        judge_strategy="consensus-then-tiebreak",
        binary="/fake/3s",
        cost_cap=1.0,
    )
    assert res.judge_retry_total == 1
    assert res.judge_empty_response_total.get("neuro", 0) == 1
    assert res.converged is True
    assert res.final_status == brainstorm.FINAL_STATUS_CONVERGED
    assert res.probe_failure_reason == ""


# ----------------------------------------------------- L2 both judges empty


def test_both_judges_empty_returns_probe_failure_unresolved(monkeypatch):
    """Both cardio and neuro judges empty on both attempts → PROBE_FAILURE."""
    seq = FakeCallSequence({
        "ask-remote": [
            HEALTHY_DRAFT_CARDIO,  # cardio draft
            EMPTY_JUDGE,           # cardio judge attempt 1
            EMPTY_JUDGE,           # cardio judge attempt 2
        ],
        "ask-local": [
            HEALTHY_DRAFT_NEURO,   # neuro draft
            EMPTY_JUDGE,           # neuro judge attempt 1
            EMPTY_JUDGE,           # neuro judge attempt 2
        ],
    })
    monkeypatch.setattr(brainstorm, "call_3s", seq)

    res = brainstorm.run_loop(
        topic="Should we ship if cost stays under $0.05?",
        threshold=0.7,
        max_iters=1,
        judge_strategy="consensus-then-tiebreak",
        binary="/fake/3s",
        cost_cap=1.0,
    )
    assert res.converged is False
    assert res.final_status == brainstorm.FINAL_STATUS_PROBE_FAILURE
    assert res.probe_failure_reason == "both_judges_failed"
    assert res.verdict == brainstorm.VERDICT_UNRESOLVED
    # Both retries attempted.
    assert res.judge_retry_total == 2
    assert res.judge_empty_response_total.get("cardio", 0) == 2  # attempt + final
    assert res.judge_empty_response_total.get("neuro", 0) == 2


# ----------------------------------------------------- L2 one empty, other converged


def test_one_judge_empty_other_converged_falls_back_to_other(monkeypatch):
    """Neuro empty (both attempts), cardio above threshold → fallback to cardio."""
    high_conf_cardio = _mk(
        '{"confidence": 0.92, "winner": "cardio", "reason": "Strong proof."}',
        cost=0.00005,
    )
    seq = FakeCallSequence({
        "ask-remote": [
            HEALTHY_DRAFT_CARDIO,
            high_conf_cardio,  # cardio judge — strong above threshold
        ],
        "ask-local": [
            HEALTHY_DRAFT_NEURO,
            EMPTY_JUDGE,  # neuro judge attempt 1
            EMPTY_JUDGE,  # neuro judge attempt 2 → MISSING_DATA
        ],
    })
    monkeypatch.setattr(brainstorm, "call_3s", seq)

    res = brainstorm.run_loop(
        topic="Should we ship if cost stays under $0.05?",
        threshold=0.7,
        max_iters=1,
        judge_strategy="consensus-then-tiebreak",
        binary="/fake/3s",
        cost_cap=1.0,
    )
    # Fallback: cardio above threshold → loop converges.
    assert res.converged is True
    assert res.final_status == brainstorm.FINAL_STATUS_CONVERGED
    assert res.probe_failure_reason == ""
    # Neuro flagged as unavailable.
    assert "neuro" in res.judge_unavailable
    assert res.judge_retry_total == 1
    # MISSING sentinel surfaced in iter scores.
    last_iter_scores = res.iters[-1].judge_scores
    assert last_iter_scores.get("neuro") == brainstorm.MISSING_SCORE
    # Cardio score real and >= threshold.
    assert last_iter_scores.get("cardio", 0.0) >= 0.7
    # Final answer should be cardio's prose (the only judge that spoke).
    assert "Cardio's draft" in res.final_answer


def test_one_judge_empty_other_below_threshold_unresolved(monkeypatch):
    """Neuro empty + cardio below threshold → PROBE_FAILURE with judge_unavailable."""
    weak_cardio_judge = _mk(
        '{"confidence": 0.40, "winner": "cardio", "reason": "Lukewarm."}',
        cost=0.00005,
    )
    seq = FakeCallSequence({
        "ask-remote": [
            HEALTHY_DRAFT_CARDIO,
            weak_cardio_judge,
        ],
        "ask-local": [
            HEALTHY_DRAFT_NEURO,
            EMPTY_JUDGE,
            EMPTY_JUDGE,
        ],
    })
    monkeypatch.setattr(brainstorm, "call_3s", seq)
    res = brainstorm.run_loop(
        topic="Should we ship if cost stays under $0.05?",
        threshold=0.7,
        max_iters=1,
        judge_strategy="consensus-then-tiebreak",
        binary="/fake/3s",
        cost_cap=1.0,
    )
    assert res.converged is False
    assert res.final_status == brainstorm.FINAL_STATUS_PROBE_FAILURE
    assert "neuro" in res.probe_failure_reason
    assert res.verdict == brainstorm.VERDICT_UNRESOLVED


# ----------------------------------------------------- happy-path backwards compat


def test_existing_happy_path_unchanged_no_retry_no_extra_calls(monkeypatch):
    """Both judges respond on attempt 1 → no retry, no extra cost, identical to BB4."""
    seq = FakeCallSequence({
        "ask-remote": [HEALTHY_DRAFT_CARDIO, HEALTHY_JUDGE_CARDIO],
        "ask-local": [HEALTHY_DRAFT_NEURO, HEALTHY_JUDGE_NEURO],
    })
    monkeypatch.setattr(brainstorm, "call_3s", seq)

    res = brainstorm.run_loop(
        topic="Should we ship if cost stays under $0.05?",
        threshold=0.7,
        max_iters=1,
        judge_strategy="consensus-then-tiebreak",
        binary="/fake/3s",
        cost_cap=1.0,
    )
    # ZERO retries, ZERO empty responses recorded.
    assert res.judge_retry_total == 0
    assert sum(res.judge_empty_response_total.values()) == 0
    assert res.judge_unavailable == []
    # Loop converged — happy path unchanged.
    assert res.converged is True
    assert res.final_status == brainstorm.FINAL_STATUS_CONVERGED
    # Exactly 4 calls: 2 drafts + 2 judges (no retry).
    total_calls = sum(seq.call_counts.values())
    assert total_calls == 4, (
        f"Expected exactly 4 calls (2 drafts + 2 judges), got {total_calls}"
    )


# ----------------------------------------------------- counters surface in JSON


def test_counters_appear_in_loop_result_for_observability(monkeypatch):
    """ZSF: counters must be on LoopResult so JSON sidecar exposes them."""
    seq = FakeCallSequence({
        "ask-remote": [HEALTHY_DRAFT_CARDIO, HEALTHY_JUDGE_CARDIO],
        "ask-local": [HEALTHY_DRAFT_NEURO, EMPTY_JUDGE, HEALTHY_JUDGE_NEURO],
    })
    monkeypatch.setattr(brainstorm, "call_3s", seq)
    res = brainstorm.run_loop(
        topic="Should we ship if cost stays under $0.05?",
        threshold=0.7,
        max_iters=1,
        judge_strategy="consensus-then-tiebreak",
        binary="/fake/3s",
        cost_cap=1.0,
    )
    # Fields are part of the dataclass.
    assert hasattr(res, "judge_retry_total")
    assert hasattr(res, "judge_empty_response_total")
    assert hasattr(res, "probe_failure_reason")
    assert hasattr(res, "judge_unavailable")
    # asdict round-trip works (JSON-friendly).
    from dataclasses import asdict
    payload = asdict(res)
    assert payload["judge_retry_total"] == 1
    assert payload["judge_empty_response_total"] == {"neuro": 1}


# ----------------------------------------------------- BB4 regression preserved


def test_bb4_v5_truly_vague_no_regression():
    """V5 path: under_specified flag still wins — CC2 must not weaken it."""
    # No bounds on V5; no LLM calls (stubbed both).
    res = brainstorm.run_loop(
        topic="What is the right answer?",  # truly vague
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
    # No probe failure (stubs return non-empty deterministic strings).
    assert res.final_status != brainstorm.FINAL_STATUS_PROBE_FAILURE


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
