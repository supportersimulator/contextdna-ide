#!/usr/bin/env python3
"""3s-answer-brainstorm.py — Iterative 3-surgeon brainstorm loop.

L3 prerequisite (originally specced in `.fleet/audits/2026-05-04-L3-brainstorm-3s-loop.md`,
unshipped at N4 start) — re-implemented here as a minimal viable loop, then hardened by N4
with alternating-judge bias mitigation, failure-mode handling, and corpus regression hooks.

Loop semantics:
  1. Cardio drafts an answer to the topic.
  2. Neuro proposes a counter-answer (different angle).
  3. A *judge* surgeon scores both answers on a 0.0–1.0 confidence scale.
  4. If max(score) >= --threshold, return the winning answer.
  5. Else feed both into the next iteration as "previous attempts" until --max-iters.

"Satisfied" is defined as: judge confidence >= threshold (default 0.7) on at least one
candidate answer, OR max_iters reached (degraded — return best seen).

Judge strategies (Part A — N4 hardening; Part A — O5 hardening):
  cardio                   — Cardio always judges (back-compat, self-bias risk).
  neuro                    — Neuro always judges (mirror, bias-test).
  alternate                — Cardio on odd iters (1,3,5...), Neuro on even (2,4,6...). Default.
  consensus                — BOTH judge, require both >= threshold to converge.
  consensus-then-tiebreak  — BOTH judge. If |cardio_score - neuro_score| <= tiebreak_delta
                              and avg >= threshold => converge (avg). If they disagree
                              (delta > tiebreak_delta), counter `judge_tiebreak_invoked_total`,
                              record both judgments, and (autonomous mode) mark unresolved
                              with final_status="tiebreak_required" so Aaron can review.
                              Constitutional 3-Surgeon mode: disagreement is the signal.

Output:
  /tmp/3s-answer-brainstorm-<UTC>.md       — transcript with per-iter judge tagged
  /tmp/3s-answer-brainstorm-<UTC>.json     — machine-readable summary (id/cost/judges/converged)
  stdout                                    — final answer + summary
  /tmp/3s-answer-brainstorm.err             — ZSF: every failure recorded with route+rc

Failure modes (Part C — N4 hardening):
  --stub-cardio  — replace cardio with a deterministic stub (failure-mode regression).
  --stub-neuro   — replace neuro with a deterministic stub.
  Both stubs return {"text": "STUB", "cost": 0.0} — verifies graceful degradation.

ZSF: stderr+stdout+rc captured for every 3s call. No silent except: pass anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ERR_LOG = Path("/tmp/3s-answer-brainstorm.err")
# Tiebreak counter file (ZSF: persisted, observable, never silenced).
TIEBREAK_COUNTER = Path("/tmp/3s-answer-brainstorm-tiebreak.counter")
# Tiebreak inbox — Aaron reviews unresolved cases here.
TIEBREAK_INBOX = Path("/tmp/3s-answer-brainstorm-tiebreaks")
DEFAULT_THRESHOLD = 0.7
DEFAULT_MAX_ITERS = 3
DEFAULT_TIEBREAK_DELTA = 0.15

# Final-status enum (string vals — JSON-friendly).
FINAL_STATUS_CONVERGED = "converged"
FINAL_STATUS_MAX_ITERS = "max_iters_exhausted"
FINAL_STATUS_TIEBREAK = "tiebreak_required"
FINAL_STATUS_UNDER_SPECIFIED = "under_specified_question"
FINAL_STATUS_NO_ANSWER = "no_answer"
# CC2 (2026-05-07): probe-failure status — at least one judge returned empty
# text on BOTH the initial call and the retry. Surfaced as UNRESOLVED at the
# verdict layer when no other judge converged on a verdict; otherwise we fall
# back to the responding judge's verdict with a `judge_unavailable` note.
FINAL_STATUS_PROBE_FAILURE = "probe_failure"

# CC2 (2026-05-07): MISSING_DATA sentinel for empty judge responses.
# Score-space sentinel used when a judge returned no parseable text on both
# initial call AND one retry. We do NOT score MISSING_DATA as 0.0 (that would
# contaminate consensus arithmetic — see BB5 audit iter-2 case). Instead the
# downstream consensus branch checks `judge_missing` and skips the absent
# judge from convergence math while still recording the event.
MISSING_DATA = "MISSING_DATA"  # string sentinel for type/JSON
MISSING_SCORE = -1.0  # numeric sentinel inside scores dict (lt 0 = missing)

# Decision verdict enum — Aaron's autonomy contract (U1 probe 2026-05-04).
# Maps prose answer -> one of these so brainstorm output is machine-actionable.
VERDICT_PROCEED = "PROCEED"   # ship as-is
VERDICT_PROCEED_NOTE = "PROCEED-with-note"  # BB4 (2026-05-07): both above threshold,
                                            # but prose verdicts disagree (one PROCEED,
                                            # one DEFER on a soft worry). Prefer PROCEED
                                            # with the dissenting concern attached as
                                            # a follow-up note rather than UNRESOLVED.
VERDICT_SPLIT = "SPLIT"       # break into smaller patches/phases
VERDICT_DEFER = "DEFER"       # need more info / wait
VERDICT_DROP = "DROP"         # don't pursue
VERDICT_UNRESOLVED = "UNRESOLVED"  # tiebreak/vague — no autonomous verdict
_VERDICT_PATTERNS = [
    (re.compile(r"\b(split|break (it )?(up|into|apart)|three (smaller|patches)|"
                r"smaller (patches|chunks|pieces)|ship (independently|separately)|"
                r"phase\s*(it|them)|incremental(ly)?)\b", re.I), VERDICT_SPLIT),
    (re.compile(r"\b(drop|abandon|don'?t (pursue|ship|build)|kill|reject|"
                r"not worth (it|pursuing))\b", re.I), VERDICT_DROP),
    (re.compile(r"\b(defer|postpone|wait|need more (info|context|data)|"
                r"insufficient|come back later|table (this|it))\b", re.I), VERDICT_DEFER),
    (re.compile(r"\b(proceed|ship (it|now|as-?is)|combined patch|one (patch|bundle)|"
                r"go ahead|approve|move forward|merge (it|now))\b", re.I), VERDICT_PROCEED),
]


def derive_verdict(final_status: str, answer: str) -> str:
    """Map (final_status, prose) -> machine verdict. ZSF: deterministic; no LLM call."""
    if final_status in (
        FINAL_STATUS_TIEBREAK,
        FINAL_STATUS_UNDER_SPECIFIED,
        FINAL_STATUS_NO_ANSWER,
        FINAL_STATUS_PROBE_FAILURE,  # CC2: empty-judge probe failure -> UNRESOLVED
    ):
        return VERDICT_UNRESOLVED
    if not answer:
        return VERDICT_UNRESOLVED
    # First match wins — order chosen so SPLIT/DROP/DEFER beat PROCEED when both fire
    # (prose like "ship as three smaller patches" hits SPLIT first, not PROCEED).
    for pat, verdict in _VERDICT_PATTERNS:
        if pat.search(answer):
            return verdict
    # Converged but no keyword hit -> assume PROCEED (the answer was satisfying enough
    # to converge, just doesn't use a canonical phrase).
    if final_status == FINAL_STATUS_CONVERGED:
        return VERDICT_PROCEED
    return VERDICT_UNRESOLVED


# BB4 hardening (2026-05-07): measurable-bounds heuristic.
# When a question contains explicit success criteria, numeric thresholds, or an
# enumerated verdict list, lower the floor below which we treat it as vague.
# This fixes false-positive UNRESOLVED on cumulative trajectory questions like
# Z5 (Path A / Path B framing) and AA5 (a-f bounds enumerated) without weakening
# the catch on truly vague questions like V5 ("ready or not?").
_MEASURABLE_BOUNDS_HINTS = re.compile(
    r"(?:"
    r"\bcriteria\s*:\s*"                       # "criteria:"
    r"|\bbounds?\s*:\s*"                        # "bound:" / "bounds:"
    r"|\bverdicts?\s*:\s*"                      # "verdicts:"
    r"|\bmeasurable\s+(?:bounds?|criteria|thresholds?)\b"
    r"|\bwithin\s+measurable\s+bounds?\b"
    r"|\bSLO\b|\bSLA\b"
    r"|\bgains-?gate\b|\binvariants?\s+\d+\s*/\s*\d+"
    r"|\b(?:[abcdef]\)|[1-9]\))\s+\w+.*?,\s*(?:[abcdef]\)|[2-9]\))"  # a) ... b)
    r"|\$[0-9]+(?:\.[0-9]+)?"                   # $0 / $0.30 thresholds
    r"|\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds?|%|MB|GB|kB)\b"
    r"|\b(?:exit\s+0|exit\s+code\s+\d+)\b"
    r"|\bPATH\s+[AB]\b"                         # "Path A / Path B" enumerated paths
    r")",
    re.I | re.S,
)


def has_measurable_bounds(topic: str) -> bool:
    """True if the topic contains explicit, machine-checkable success criteria.

    Used by the vague-Q gate: when measurable bounds are present, lower the
    vague-score floor so a single judge's "feels vague" reflex doesn't override
    a question with hard criteria. ZSF: deterministic; no LLM call.
    """
    if not topic:
        return False
    return bool(_MEASURABLE_BOUNDS_HINTS.search(topic))


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def find_3s_binary() -> Optional[str]:
    """Locate the 3s CLI — prefer PATH, fall back to plugin cache."""
    p = shutil.which("3s")
    if p:
        return p
    cached = Path(
        "/Users/aarontjomsland/.claude/plugins/cache/"
        "3-surgeons-marketplace/3-surgeons/1.0.0/.venv/bin/3s"
    )
    if cached.is_file() and os.access(cached, os.X_OK):
        return str(cached)
    return None


def append_err(line: str) -> None:
    """ZSF: every failure recorded. Never silenced."""
    try:
        with ERR_LOG.open("a") as f:
            f.write(line + "\n")
    except OSError as e:
        # ZSF: even err-log failures must be visible — fall back to stderr.
        sys.stderr.write(f"ERR_LOG_WRITE_FAILED: {e}\n")


def bump_tiebreak_counter() -> int:
    """Increment monotonic tiebreak counter. ZSF: failures surface to stderr."""
    try:
        cur = 0
        if TIEBREAK_COUNTER.exists():
            try:
                cur = int(TIEBREAK_COUNTER.read_text().strip() or "0")
            except (OSError, ValueError) as e:
                # ZSF: malformed counter -> log + reset to 0 (don't silently
                # mask the prior count by reusing it).
                append_err(f"{utc_stamp()}\ttiebreak-counter-parse-failed\terr={e!r}")
                cur = 0
        new = cur + 1
        TIEBREAK_COUNTER.write_text(str(new))
        return new
    except OSError as e:
        append_err(f"{utc_stamp()}\ttiebreak-counter-write-failed\terr={e!r}")
        return -1


def record_tiebreak(res: "LoopResult", iter_idx: int, scores: dict, judge_raws: dict, delta: float) -> Path:
    """Persist a tiebreak case for Aaron review. Returns the path."""
    try:
        TIEBREAK_INBOX.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", res.topic.lower())[:60].strip("-") or "topic"
        path = TIEBREAK_INBOX / f"{utc_stamp()}-{slug}-iter{iter_idx}.json"
        payload = {
            "topic": res.topic,
            "iter": iter_idx,
            "judge_strategy": res.judge_strategy,
            "scores": scores,
            "delta": delta,
            "tiebreak_delta_threshold": res.tiebreak_delta,
            "judges_raw": judge_raws,
            "started_utc": res.started_utc,
            "stamp_utc": utc_stamp(),
        }
        path.write_text(json.dumps(payload, indent=2))
        return path
    except OSError as e:
        append_err(f"{utc_stamp()}\ttiebreak-record-failed\terr={e!r}")
        return Path("")


# Cost regex: "Cost: $0.0005" or "Total cost: $0.0001"
_COST_RE = re.compile(r"(?:Total\s+)?[Cc]ost:\s*\$([0-9]+\.[0-9]+)")


def extract_cost(text: str) -> float:
    m = _COST_RE.search(text)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        # ZSF: malformed cost is a bug — surface it.
        append_err(f"{utc_stamp()}\tcost-parse-failed\t{m.group(1)!r}")
        return 0.0


def extract_body(text: str) -> str:
    """Strip trailing cost lines."""
    lines = text.splitlines()
    while lines and (
        not lines[-1].strip()
        or _COST_RE.match(lines[-1].strip())
    ):
        lines.pop()
    return "\n".join(lines)


@dataclass
class CallResult:
    body: str
    cost: float
    rc: int
    raw: str
    route: str


def call_3s(binary: str, route: str, prompt: str, timeout: int = 90) -> CallResult:
    """Invoke 3s. ZSF: capture rc/stdout/stderr; record failures explicitly."""
    if not binary:
        append_err(f"{utc_stamp()}\troute={route}\tno-binary")
        return CallResult(body="", cost=0.0, rc=127, raw="3s binary not found", route=route)
    try:
        cp = subprocess.run(
            [binary, route, prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        append_err(f"{utc_stamp()}\troute={route}\trc=timeout\tprompt={prompt[:120]!r}")
        return CallResult(body="", cost=0.0, rc=124, raw=f"timeout: {e}", route=route)
    except OSError as e:
        # ZSF: explicit, not silenced.
        append_err(f"{utc_stamp()}\troute={route}\trc=oserror\terr={e!r}")
        return CallResult(body="", cost=0.0, rc=126, raw=f"oserror: {e}", route=route)

    raw = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
    if cp.returncode != 0:
        append_err(
            f"{utc_stamp()}\troute={route}\trc={cp.returncode}\t"
            f"prompt={prompt[:120]!r}\nraw={raw[:400]!r}\n---"
        )
        return CallResult(body="", cost=0.0, rc=cp.returncode, raw=raw, route=route)
    return CallResult(
        body=extract_body(cp.stdout),
        cost=extract_cost(raw),
        rc=0,
        raw=raw,
        route=route,
    )


def stub_call(route: str, prompt: str) -> CallResult:
    """Failure-mode stub. Deterministic, free."""
    return CallResult(
        body=f"[STUB:{route}] (real surgeon stubbed for failure-mode test)",
        cost=0.0,
        rc=0,
        raw=f"STUB route={route} prompt={prompt[:80]}",
        route=route,
    )


# ---------- CC2 (2026-05-07): empty-judge-response robustness ----------
#
# Root cause (BB5 audit): the local neuro judge (3s ask-local → MLX 5044/5045
# with DeepSeek-chat fallback) intermittently returned rc=0 with empty stdout
# on the 2nd judge call of a tiebreak round. parse_judge_score("") -> 0.0,
# which the consensus arithmetic interpreted as a real disagreement (delta
# 0.95 vs cardio 0.95) and tipped the verdict to UNRESOLVED.
#
# Fix layers:
#   L1  retry once on empty body before scoring.
#   L2  if BOTH attempts return empty -> MISSING_DATA sentinel (not 0.0).
#       Verdict-derivation uses PROBE_FAILURE branch: fall back to the
#       other judge's verdict if it converged, else UNRESOLVED with reason.
#   L3  per-judge counter `judge_empty_response_total` + brainstorm err log.
#
# ZSF: every retry + every empty event is observable via res.judge_empty_*
# fields and ERR_LOG. Healthy path: zero behavior change (no retry, no
# extra calls, no extra cost).


def _is_empty_judge_body(body: str) -> bool:
    """True when judge body is None/empty/whitespace-only.

    ZSF: deterministic; the only place we decide a judge "didn't speak."
    Whitespace-only counts as empty so a stray newline doesn't slip past.
    """
    if not body:
        return True
    return not body.strip()


# ---------- Judge logic ----------


def parse_judge_score(text: str) -> float:
    """Pull a 0.0–1.0 confidence from judge output.

    Heuristics, in order:
      1. JSON-ish "confidence": 0.85
      2. "score: 0.85" / "confidence: 0.85"
      3. Bare 0.xx near the end
    Returns 0.0 if nothing parseable (judge effectively says "no opinion").
    """
    if not text:
        return 0.0
    # JSON-ish
    m = re.search(r'"confidence"\s*:\s*([01](?:\.\d+)?)', text)
    if m:
        try:
            return min(1.0, max(0.0, float(m.group(1))))
        except ValueError:
            pass
    # plain "score:" / "confidence:"
    m = re.search(r"(?:confidence|score)\s*[:=]\s*([01](?:\.\d+)?)", text, re.I)
    if m:
        try:
            return min(1.0, max(0.0, float(m.group(1))))
        except ValueError:
            pass
    # last-ditch: any 0.xx in the text
    m = re.findall(r"\b(0(?:\.\d+)?)\b", text[-200:])
    if m:
        try:
            return min(1.0, max(0.0, float(m[-1])))
        except ValueError:
            pass
    return 0.0


def pick_judge_routes(strategy: str, iter_idx: int) -> list[str]:
    """Return list of judge routes (1 or 2) for this iteration.

    iter_idx is 1-based.
    """
    if strategy == "cardio":
        return ["ask-remote"]
    if strategy == "neuro":
        return ["ask-local"]
    if strategy == "alternate":
        return ["ask-remote"] if iter_idx % 2 == 1 else ["ask-local"]
    if strategy == "consensus":
        return ["ask-remote", "ask-local"]
    if strategy == "consensus-then-tiebreak":
        # Both judge every iter; consensus-or-disagree decided post-scoring.
        return ["ask-remote", "ask-local"]
    raise ValueError(f"unknown judge strategy: {strategy}")


def judge_label(route: str) -> str:
    return {"ask-remote": "cardio", "ask-local": "neuro"}.get(route, route)


# ---------- Loop ----------


@dataclass
class IterRecord:
    iter: int
    cardio_answer: str
    neuro_answer: str
    judge_routes: list[str]
    judge_scores: dict[str, float]
    judge_raw: dict[str, str]
    cost: float
    converged: bool
    # Tiebreak diagnostics (consensus-then-tiebreak only).
    tiebreak_invoked: bool = False
    score_delta: float = 0.0
    avg_score: float = 0.0
    # BB4 (2026-05-07): track stated winners from each judge body so the
    # verdict picks the consensus-winner's prose, not the judge with the
    # higher self-confidence. (Pre-BB4 bug: AA5 had both judges vote
    # winner=cardio but n=0.9 > c=0.85 made the loop pick neuro's DEFER text.)
    judge_winners: dict[str, str] = field(default_factory=dict)
    # BB4: stated prose-verdicts per surgeon (best-effort regex on each answer).
    # When both above threshold but verdicts disagree, derive PROCEED-with-note.
    answer_verdicts: dict[str, str] = field(default_factory=dict)


@dataclass
class LoopResult:
    topic: str
    threshold: float
    max_iters: int
    judge_strategy: str
    converged: bool
    final_answer: str
    final_source: str  # "cardio"|"neuro"|"none"|"consensus-avg"
    iters: list[IterRecord] = field(default_factory=list)
    total_cost: float = 0.0
    err_count: int = 0
    started_utc: str = ""
    ended_utc: str = ""
    stub_cardio: bool = False
    stub_neuro: bool = False
    # O5 hardening
    tiebreak_delta: float = DEFAULT_TIEBREAK_DELTA
    tiebreak_invoked_count: int = 0
    tiebreak_inbox_paths: list[str] = field(default_factory=list)
    final_status: str = FINAL_STATUS_NO_ANSWER  # see FINAL_STATUS_* constants
    under_specified_flag: bool = False  # vague-Q signal from judge ≤0.3 every iter
    verdict: str = VERDICT_UNRESOLVED  # U1: machine-actionable PROCEED/SPLIT/DEFER/DROP/UNRESOLVED
    # BB4 (2026-05-07): forward-compat tuning fields.
    measurable_bounds_detected: bool = False  # auto-detected from topic prose
    vague_score_cap_effective: float = 0.30   # actual floor used (post-bounds adjust)
    soft_worry_note: str = ""                 # populated when verdict is PROCEED-with-note
    # CC2 (2026-05-07): empty-judge-response robustness counters.
    judge_retry_total: int = 0  # incremented per judge-call retry
    judge_empty_response_total: dict[str, int] = field(default_factory=dict)  # {judge_label: count}
    probe_failure_reason: str = ""  # set when final_status == FINAL_STATUS_PROBE_FAILURE
    judge_unavailable: list[str] = field(default_factory=list)  # judge labels that never responded


DRAFT_PROMPT = (
    "Topic: {topic}\n\n"
    "Provide your best answer in 4-8 sentences. Be specific, decision-oriented, "
    "and grounded. If prior attempts are shown below, improve on them — don't repeat.\n"
    "{history}"
)

JUDGE_PROMPT = (
    "Evaluate two candidate answers to this question and return ONLY a JSON object "
    'on a single line: {{"confidence": <0.0-1.0>, "winner": "cardio"|"neuro", '
    '"reason": "<one sentence>"}}.\n\n'
    "Confidence is your certainty that the winner answer is sufficient — i.e. that no "
    "further iteration is needed. 0.7+ means satisfied; 0.9+ means strongly satisfied.\n\n"
    "VAGUE-QUESTION PENALTY (mandatory): If the original question is vague, ambiguous, "
    "or under-specified — meaning it has no clear success criterion, no concrete domain, "
    "no specific systems / tradeoffs / constraints to anchor the answer — score the answer "
    "<= 0.3 regardless of how thoughtful or well-written it sounds. Examples of vague "
    "questions: 'What is the right answer?', 'How should we proceed?', 'What's best?', "
    "'What do you think?'. Concrete questions reference specific systems, tradeoffs, "
    "constraints, or decision criteria. A polished answer to a vague question is still "
    "guesswork; do not let prose quality compensate for missing context.\n\n"
    "Question: {topic}\n\n"
    "CARDIO_ANSWER:\n{cardio}\n\n"
    "NEURO_ANSWER:\n{neuro}\n\n"
    "Return only the JSON line."
)


def parse_winner(text: str) -> str:
    m = re.search(r'"winner"\s*:\s*"(cardio|neuro)"', text, re.I)
    return m.group(1).lower() if m else "cardio"


# BB4: extract a per-answer prose verdict (PROCEED/SPLIT/DEFER/DROP).
# Used for the soft-worry detector — when both surgeons score above threshold
# but their prose verdicts disagree (one PROCEED, one DEFER), prefer
# PROCEED-with-note rather than UNRESOLVED.
def parse_answer_verdict(answer: str) -> str:
    """Best-effort prose-verdict extraction. Returns "" when no signal."""
    if not answer:
        return ""
    # Look for explicit "VERDICT: X" or "Verdict: X" first (high-signal).
    m = re.search(r"verdict\s*:\s*\*{0,2}\s*(PROCEED|SPLIT|DEFER|DROP|UNRESOLVED)\b",
                  answer, re.I)
    if m:
        return m.group(1).upper()
    # Fall back to existing _VERDICT_PATTERNS (pattern-based heuristic).
    for pat, verdict in _VERDICT_PATTERNS:
        if pat.search(answer):
            return verdict
    return ""


def run_loop(
    topic: str,
    threshold: float,
    max_iters: int,
    judge_strategy: str,
    binary: str,
    cost_cap: float,
    stub_cardio: bool = False,
    stub_neuro: bool = False,
    tiebreak_delta: float = DEFAULT_TIEBREAK_DELTA,
    vague_score_cap: float = 0.3,
    measurable_bounds_mode: str = "auto",
    measurable_bounds_floor: float = 0.15,
) -> LoopResult:
    # BB4 (2026-05-07): when the topic has explicit measurable bounds, lower
    # the vague floor so a single judge's "feels vague" reflex doesn't
    # over-fire on cumulative trajectory questions (Z5/AA5 patterns).
    # mode="auto"  -> detect from prompt (default)
    # mode="on"    -> force measurable-bounds floor regardless of detection
    # mode="off"   -> back-compat (no auto-adjust; vague_score_cap as-is)
    bounds_detected = has_measurable_bounds(topic)
    if measurable_bounds_mode == "on" or (
        measurable_bounds_mode == "auto" and bounds_detected
    ):
        # Floor cannot go above the user-supplied vague_score_cap (don't loosen
        # if Aaron explicitly tightened it via the flag).
        effective_vague_cap = min(vague_score_cap, measurable_bounds_floor)
    else:
        effective_vague_cap = vague_score_cap

    res = LoopResult(
        topic=topic,
        threshold=threshold,
        max_iters=max_iters,
        judge_strategy=judge_strategy,
        converged=False,
        final_answer="",
        final_source="none",
        started_utc=utc_stamp(),
        stub_cardio=stub_cardio,
        stub_neuro=stub_neuro,
        tiebreak_delta=tiebreak_delta,
        measurable_bounds_detected=bounds_detected,
        vague_score_cap_effective=effective_vague_cap,
    )
    history = ""
    best_answer = ""
    best_source = "none"
    best_score = -1.0

    for i in range(1, max_iters + 1):
        # cost cap check
        if res.total_cost > cost_cap:
            append_err(
                f"{utc_stamp()}\tcost-cap-hit\ttotal={res.total_cost:.4f}\tcap={cost_cap}"
            )
            res.err_count += 1
            break

        draft_q = DRAFT_PROMPT.format(topic=topic, history=history)

        # cardio draft
        if stub_cardio:
            cardio = stub_call("ask-remote", draft_q)
        else:
            cardio = call_3s(binary, "ask-remote", draft_q)
        if cardio.rc != 0:
            res.err_count += 1
        res.total_cost += cardio.cost

        # neuro counter
        if stub_neuro:
            neuro = stub_call("ask-local", draft_q)
        else:
            neuro = call_3s(binary, "ask-local", draft_q)
        if neuro.rc != 0:
            res.err_count += 1
        res.total_cost += neuro.cost

        # judge selection
        judge_routes = pick_judge_routes(judge_strategy, i)
        judge_scores: dict[str, float] = {}
        judge_raws: dict[str, str] = {}
        judge_winners: dict[str, str] = {}  # BB4: per-judge stated winner
        iter_winner = "cardio"

        judge_q = JUDGE_PROMPT.format(
            topic=topic,
            cardio=cardio.body or "(empty)",
            neuro=neuro.body or "(empty)",
        )

        for jr in judge_routes:
            label = judge_label(jr)
            # If the judge route's surgeon is stubbed, stub the judge call too.
            if (jr == "ask-remote" and stub_cardio) or (jr == "ask-local" and stub_neuro):
                jres = stub_call(jr, judge_q)
                # stub gives 0.0 -> ZSF: forces fallback path
                if jres.rc != 0:
                    res.err_count += 1
                res.total_cost += jres.cost
                score = parse_judge_score(jres.body)
                judge_scores[label] = score
                judge_raws[label] = jres.body[:500]
                iter_winner = parse_winner(jres.body)
                judge_winners[label] = iter_winner
            else:
                # CC2 (2026-05-07): empty-judge-response retry.
                # call_3s_judge_with_retry handles the L1 retry + L3 counters.
                # Account for cost of BOTH attempts: account first call now,
                # then add second-call cost from the returned (possibly second)
                # CallResult below. Empty-on-both => MISSING_DATA sentinel.
                first = call_3s(binary, jr, judge_q)
                if first.rc != 0:
                    res.err_count += 1
                res.total_cost += first.cost
                if _is_empty_judge_body(first.body):
                    res.judge_empty_response_total[label] = (
                        res.judge_empty_response_total.get(label, 0) + 1
                    )
                    append_err(
                        f"{utc_stamp()}\tjudge-empty-first-attempt\t"
                        f"route={jr}\tjudge={label}\titer={i}\trc={first.rc}"
                    )
                    res.judge_retry_total += 1
                    second = call_3s(binary, jr, judge_q)
                    if second.rc != 0:
                        res.err_count += 1
                    res.total_cost += second.cost
                    if _is_empty_judge_body(second.body):
                        # Both empty → MISSING_DATA sentinel; do NOT score 0.0.
                        res.judge_empty_response_total[label] = (
                            res.judge_empty_response_total.get(label, 0) + 1
                        )
                        if label not in res.judge_unavailable:
                            res.judge_unavailable.append(label)
                        append_err(
                            f"{utc_stamp()}\tjudge-empty-both-attempts\t"
                            f"route={jr}\tjudge={label}\titer={i}\t"
                            f"sentinel=MISSING_DATA"
                        )
                        judge_scores[label] = MISSING_SCORE
                        judge_raws[label] = MISSING_DATA
                        # Don't override iter_winner from a missing judge.
                        judge_winners[label] = MISSING_DATA
                        continue
                    # Retry recovered.
                    jres = second
                else:
                    jres = first
                score = parse_judge_score(jres.body)
                judge_scores[label] = score
                judge_raws[label] = jres.body[:500]
                iter_winner = parse_winner(jres.body)
                judge_winners[label] = iter_winner

        # Convergence check (per-strategy)
        cardio_text = cardio.body or "(no cardio answer)"
        neuro_text = neuro.body or "(no neuro answer)"
        # CC2 (2026-05-07): identify judges that returned MISSING_DATA on
        # this iter. They MUST be excluded from convergence math — scoring
        # MISSING_SCORE (-1.0) as if it were a confidence value would break
        # the avg/delta computations and force false tiebreaks.
        missing_judges_iter = {
            lab for lab, s in judge_scores.items() if s == MISSING_SCORE
        }
        real_scores_iter = {
            lab: s for lab, s in judge_scores.items() if s != MISSING_SCORE
        }
        chosen_text = cardio_text if iter_winner == "cardio" else neuro_text
        # chosen_score must not include the MISSING sentinel (-1.0).
        chosen_score = max(real_scores_iter.values()) if real_scores_iter else 0.0

        tiebreak_invoked = False
        score_delta = 0.0
        avg_score = 0.0

        if judge_strategy == "consensus":
            converged = len(real_scores_iter) == 2 and all(
                s >= threshold for s in real_scores_iter.values()
            )
        elif judge_strategy == "consensus-then-tiebreak":
            # CC2: if EITHER judge returned MISSING_DATA, fall back to the
            # remaining judge if it converged; otherwise mark as not-converged
            # (final_status determination later promotes to PROBE_FAILURE).
            if missing_judges_iter and len(real_scores_iter) == 1:
                only_label, only_score = next(iter(real_scores_iter.items()))
                if only_score >= threshold:
                    converged = True
                    iter_winner = judge_winners.get(only_label, only_label) or only_label
                    if iter_winner == MISSING_DATA:
                        iter_winner = only_label
                    chosen_text = cardio_text if iter_winner == "cardio" else neuro_text
                    chosen_score = only_score
                    avg_score = only_score
                    score_delta = 0.0
                else:
                    converged = False
                    avg_score = only_score
                    score_delta = 0.0
            elif missing_judges_iter and not real_scores_iter:
                # Both empty: cannot converge.
                converged = False
            elif len(judge_scores) == 2:
                c_score = judge_scores.get("cardio", 0.0)
                n_score = judge_scores.get("neuro", 0.0)
                score_delta = abs(c_score - n_score)
                avg_score = (c_score + n_score) / 2.0
                if score_delta <= tiebreak_delta:
                    # Consensus path: avg above threshold => converge.
                    converged = avg_score >= threshold
                    if converged:
                        # BB4 (2026-05-07): when both judges named the SAME
                        # winner, use that — it's stated consensus on which
                        # answer is better. The score is confidence in
                        # convergence; the `winner` field is judgment on the
                        # better answer. Pre-BB4 used score-ordering only,
                        # which made AA5 pick neuro's prose (DEFER) even
                        # though both judges declared cardio's PROCEED the
                        # winner (just with c=0.85 < n=0.9 self-confidence).
                        c_w = judge_winners.get("cardio", "")
                        n_w = judge_winners.get("neuro", "")
                        if c_w and c_w == n_w:
                            iter_winner = c_w
                        elif c_score == n_score:
                            pass  # keep iter_winner from last judge body
                        else:
                            iter_winner = "cardio" if c_score >= n_score else "neuro"
                        chosen_text = cardio_text if iter_winner == "cardio" else neuro_text
                        chosen_score = avg_score
                else:
                    # Disagreement = constitutional signal. Don't converge.
                    converged = False
                    tiebreak_invoked = True
                    res.tiebreak_invoked_count += 1
                    bump_tiebreak_counter()
                    inbox_path = record_tiebreak(
                        res, i, judge_scores, judge_raws, score_delta
                    )
                    if inbox_path:
                        res.tiebreak_inbox_paths.append(str(inbox_path))
                    append_err(
                        f"{utc_stamp()}\ttiebreak-invoked\titer={i}\t"
                        f"scores={judge_scores}\tdelta={score_delta:.3f}\t"
                        f"threshold={tiebreak_delta:.3f}"
                    )
            else:
                # Missing one of the two judges (e.g., stub returned 0) — treat
                # as not-converged. ZSF: visible via err_count + judge_scores.
                converged = False
        else:
            # cardio / neuro / alternate strategies. CC2: exclude MISSING_DATA
            # judges from convergence math (don't let -1.0 sentinel leak in).
            converged = any(
                s >= threshold for s in real_scores_iter.values()
            )

        if chosen_score > best_score:
            best_score = chosen_score
            best_answer = chosen_text
            best_source = iter_winner

        # BB4: capture per-answer prose verdicts for soft-worry detection.
        ans_verdicts = {
            "cardio": parse_answer_verdict(cardio_text),
            "neuro": parse_answer_verdict(neuro_text),
        }

        res.iters.append(
            IterRecord(
                iter=i,
                cardio_answer=cardio_text,
                neuro_answer=neuro_text,
                judge_routes=[judge_label(r) for r in judge_routes],
                judge_scores=judge_scores,
                judge_raw=judge_raws,
                cost=cardio.cost + neuro.cost + sum(0.0 for _ in judge_routes),
                converged=converged,
                tiebreak_invoked=tiebreak_invoked,
                score_delta=round(score_delta, 4),
                avg_score=round(avg_score, 4),
                judge_winners=dict(judge_winners),
                answer_verdicts=ans_verdicts,
            )
        )

        if converged:
            res.converged = True
            res.final_answer = chosen_text
            res.final_source = (
                "consensus-avg"
                if judge_strategy == "consensus-then-tiebreak"
                else iter_winner
            )
            break

        # Prepare history for next iter
        history = (
            f"\n\nPrevious attempt {i} (cardio): {cardio_text[:600]}"
            f"\nPrevious attempt {i} (neuro): {neuro_text[:600]}"
            f"\nJudge ({','.join(judge_label(r) for r in judge_routes)}) "
            f"scores: {judge_scores}; not yet satisfied. Push further."
        )

    if not res.converged:
        res.final_answer = best_answer or "(no answer generated)"
        res.final_source = best_source

    # ----- Final-status determination (O5) -----
    # Vague-Q detection (variance-tolerant):
    # A question is under-specified if EVERY iter had at least one real judge
    # scoring <= vague_score_cap (i.e. one of the judges consistently signaled
    # "this Q is vague," even if the other judge over-scored). This tolerates
    # model variance where a single judge bends the rule occasionally. Only
    # iters where at least one real judge fired are counted.
    if res.iters and not res.converged:
        any_real_iter = False
        all_iters_have_low_voice = True
        for it in res.iters:
            real_judges = []
            for label, score in it.judge_scores.items():
                stubbed = (
                    (label == "cardio" and stub_cardio)
                    or (label == "neuro" and stub_neuro)
                )
                if stubbed:
                    continue
                # CC2: a MISSING_DATA judge contributes NO information about
                # vagueness — exclude from the low-voice analysis.
                if score == MISSING_SCORE:
                    continue
                real_judges.append(score)
            if not real_judges:
                continue
            any_real_iter = True
            # BB4: use the effective (possibly bounds-adjusted) cap, not the raw
            # CLI value — questions with measurable bounds get a tighter floor.
            if not any(s <= effective_vague_cap for s in real_judges):
                all_iters_have_low_voice = False
                break
        if any_real_iter and all_iters_have_low_voice:
            res.under_specified_flag = True

    # CC2 (2026-05-07): probe-failure detection.
    # If the loop did NOT converge AND any judge was unavailable (both attempts
    # empty) on the LAST iter, mark PROBE_FAILURE rather than TIEBREAK. The
    # tiebreak counter would have only fired in this case if the consensus
    # branch fell through to score arithmetic, which the MISSING_DATA branch
    # now prevents. This branch handles the both-judges-empty + the
    # one-empty-other-below-threshold cases.
    last_iter_missing: list[str] = []
    if res.iters:
        last_iter_missing = [
            lab for lab, s in res.iters[-1].judge_scores.items()
            if s == MISSING_SCORE
        ]

    # final_status set first, then verdict derived from it + prose answer.
    if res.converged:
        res.final_status = FINAL_STATUS_CONVERGED
    elif res.under_specified_flag:
        # Vague-Q wins over tiebreak — tiebreak on a vague question is just
        # noise; surface the root cause (the question itself).
        res.final_status = FINAL_STATUS_UNDER_SPECIFIED
    elif last_iter_missing:
        # CC2: at least one judge was unavailable on the final iter.
        # If the responding judge gave us an answer above threshold the
        # consensus-then-tiebreak branch would have converged; reaching here
        # means we have NO usable verdict.
        res.final_status = FINAL_STATUS_PROBE_FAILURE
        if len(last_iter_missing) >= 2 or (
            len(last_iter_missing) == 1
            and len(res.iters[-1].judge_scores) <= 1
        ):
            res.probe_failure_reason = "both_judges_failed"
        else:
            res.probe_failure_reason = (
                f"judge_unavailable={','.join(last_iter_missing)}"
            )
    elif res.tiebreak_invoked_count > 0 and judge_strategy == "consensus-then-tiebreak":
        res.final_status = FINAL_STATUS_TIEBREAK
    elif res.final_answer and "(no answer" not in res.final_answer:
        res.final_status = FINAL_STATUS_MAX_ITERS
    else:
        res.final_status = FINAL_STATUS_NO_ANSWER

    res.verdict = derive_verdict(res.final_status, res.final_answer)

    # BB4 (2026-05-07): soft-worry promotion. When the loop converged on a
    # PROCEED answer but the dissenting surgeon flagged a soft worry
    # (DEFER/SPLIT) while still scoring above threshold, mark
    # PROCEED-with-note instead of plain PROCEED so callers can attach the
    # dissenting concern as a follow-up note rather than discarding it.
    # Symmetric: if winner says DEFER and dissenter says PROCEED above threshold,
    # we leave it as DEFER (deferring is the conservative move; conserves V5
    # behavior — DEFER on a converged loop stays DEFER).
    if res.converged and res.verdict == VERDICT_PROCEED and res.iters:
        last = res.iters[-1]
        c_v = last.answer_verdicts.get("cardio", "")
        n_v = last.answer_verdicts.get("neuro", "")
        c_s = last.judge_scores.get("cardio", 0.0)
        n_s = last.judge_scores.get("neuro", 0.0)
        # Find dissenter (not the winner-source whose verdict is PROCEED).
        dissenter = ""
        dissent_verdict = ""
        if iter_winner == "cardio" and n_v in (VERDICT_DEFER, VERDICT_SPLIT) and n_s >= threshold:
            dissenter, dissent_verdict = "neuro", n_v
        elif iter_winner == "neuro" and c_v in (VERDICT_DEFER, VERDICT_SPLIT) and c_s >= threshold:
            dissenter, dissent_verdict = "cardio", c_v
        if dissenter:
            res.verdict = VERDICT_PROCEED_NOTE
            res.soft_worry_note = (
                f"{dissenter} flagged {dissent_verdict} with confidence "
                f"{(n_s if dissenter == 'neuro' else c_s):.2f}; "
                f"attach as follow-up rather than block."
            )

    res.ended_utc = utc_stamp()
    return res


# ---------- Render ----------


def render_md(res: LoopResult) -> str:
    lines = [
        f"# Answer Brainstorm: {res.topic}",
        "",
        f"**Started:** {res.started_utc}",
        f"**Ended:** {res.ended_utc}",
        f"**Converged:** {res.converged}",
        f"**Verdict:** {res.verdict}",
        f"**Final status:** {res.final_status}",
        f"**Final source:** {res.final_source}",
        f"**Judge strategy:** {res.judge_strategy}",
        f"**Threshold:** {res.threshold}",
        f"**Tiebreak delta:** {res.tiebreak_delta}",
        f"**Tiebreak invocations:** {res.tiebreak_invoked_count}",
        f"**Under-specified flag:** {res.under_specified_flag}",
        f"**Measurable bounds detected:** {res.measurable_bounds_detected}",
        f"**Vague-score cap (effective):** {res.vague_score_cap_effective}",
        f"**Soft-worry note:** {res.soft_worry_note or '(none)'}",
        f"**Iterations:** {len(res.iters)} / {res.max_iters}",
        f"**Total cost:** ${res.total_cost:.4f}",
        f"**Errors:** {res.err_count}",
        f"**Judge retries (CC2):** {res.judge_retry_total}",
        f"**Judge empty responses (CC2):** {dict(res.judge_empty_response_total) or '{}'}",
        f"**Probe failure reason:** {res.probe_failure_reason or '(none)'}",
        f"**Judge unavailable:** {res.judge_unavailable or '[]'}",
        f"**Stub cardio:** {res.stub_cardio}  |  **Stub neuro:** {res.stub_neuro}",
        "",
        "## Per-iteration judges & scores",
        "",
        "| iter | judge(s) | scores | delta | avg | tiebreak | converged |",
        "|------|----------|--------|-------|-----|----------|-----------|",
    ]
    for it in res.iters:
        lines.append(
            f"| {it.iter} | {','.join(it.judge_routes)} | "
            f"{it.judge_scores} | {it.score_delta} | {it.avg_score} | "
            f"{it.tiebreak_invoked} | {it.converged} |"
        )
    lines += [
        "",
        "## Final Answer",
        "",
        res.final_answer or "(none)",
        "",
        "## Iteration transcripts",
        "",
    ]
    for it in res.iters:
        lines += [
            f"### Iter {it.iter}",
            "",
            "**Cardio:**",
            "",
            it.cardio_answer,
            "",
            "**Neuro:**",
            "",
            it.neuro_answer,
            "",
            f"**Judge ({','.join(it.judge_routes)}) raw:**",
            "",
            "```",
            json.dumps(it.judge_raw, indent=2),
            "```",
            "",
        ]
    return "\n".join(lines)


def review_tiebreaks(args) -> int:
    """`review-tiebreaks` subcommand — list pending tiebreak cases for Aaron.

    Iterates `/tmp/3s-answer-brainstorm-tiebreaks/*.json`, prints one-line summary
    per case (topic, scores, delta), with optional --show-id to dump full JSON.
    """
    if not TIEBREAK_INBOX.exists():
        print(f"No tiebreak inbox at {TIEBREAK_INBOX} — nothing to review.")
        return 0
    cases = sorted(TIEBREAK_INBOX.glob("*.json"))
    if not cases:
        print(f"No pending tiebreaks in {TIEBREAK_INBOX}.")
        return 0
    counter_val = "?"
    if TIEBREAK_COUNTER.exists():
        try:
            counter_val = TIEBREAK_COUNTER.read_text().strip() or "0"
        except OSError as e:
            append_err(f"{utc_stamp()}\ttiebreak-counter-read-failed\terr={e!r}")
    print(f"Tiebreak counter (lifetime): {counter_val}")
    print(f"Pending cases: {len(cases)}")
    print("=" * 70)
    for p in cases:
        try:
            payload = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            append_err(f"{utc_stamp()}\ttiebreak-read-failed\tpath={p}\terr={e!r}")
            print(f"  [unreadable] {p.name}: {e}")
            continue
        scores = payload.get("scores", {})
        delta = payload.get("delta", "?")
        topic = payload.get("topic", "?")[:80]
        print(f"  {p.name}")
        print(f"    topic: {topic}")
        print(f"    iter:  {payload.get('iter', '?')}")
        print(f"    scores: {scores}  delta={delta}")
        if getattr(args, "show_id", None) and args.show_id in p.name:
            print("    --- full payload ---")
            print(json.dumps(payload, indent=2))
            print("    --- end ---")
    print("=" * 70)
    print(f"Resolve a case by reading {TIEBREAK_INBOX} and moving it to an")
    print("'archive/' subfolder once Aaron has decided which surgeon was right.")
    return 0


def main() -> int:
    # Pre-parse: detect `review-tiebreaks` subcommand without registering it as
    # a subparser (subparsers + positional `topic` collide because argparse
    # tries to interpret a free-text topic as a subcommand name).
    if len(sys.argv) >= 2 and sys.argv[1] == "review-tiebreaks":
        rt_ap = argparse.ArgumentParser(prog="3s-answer-brainstorm.py review-tiebreaks")
        rt_ap.add_argument("--show-id", default=None, help="Show full JSON for matching id.")
        rt_args = rt_ap.parse_args(sys.argv[2:])
        return review_tiebreaks(rt_args)

    ap = argparse.ArgumentParser(
        description="Iterative 3-surgeon brainstorm loop. "
                    "Subcommand: review-tiebreaks (list pending tiebreak cases).",
    )
    ap.add_argument("topic", nargs="?", help="Topic / question. If omitted, read from stdin.")
    ap.add_argument(
        "--judge-strategy",
        default="alternate",
        choices=["cardio", "neuro", "alternate", "consensus", "consensus-then-tiebreak"],
        help="Judge selection. Default=alternate (N4 bias-mitigation). "
             "consensus-then-tiebreak is O5 — both judge, disagreement triggers tiebreak.",
    )
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--max-iters", type=int, default=DEFAULT_MAX_ITERS)
    ap.add_argument("--cost-cap", type=float, default=0.05)
    ap.add_argument(
        "--tiebreak-delta",
        type=float,
        default=DEFAULT_TIEBREAK_DELTA,
        help="consensus-then-tiebreak: max |cardio - neuro| to count as consensus. Default 0.15.",
    )
    ap.add_argument(
        "--vague-score-cap",
        type=float,
        default=0.3,
        help="Score at/below which a question is treated as under-specified. Default 0.3.",
    )
    ap.add_argument(
        "--measurable-bounds-mode",
        default="auto",
        choices=["auto", "on", "off"],
        help="BB4: lower vague-score floor when the topic has explicit bounds "
             "(criteria:/bounds:/numeric thresholds/Path A/Path B). "
             "'auto' detects from prompt (default). 'on' forces. 'off' disables.",
    )
    ap.add_argument(
        "--measurable-bounds-floor",
        type=float,
        default=0.15,
        help="BB4: vague floor used when measurable bounds are detected/forced. "
             "Default 0.15 (vs 0.30 default cap). Lower = harder to flag vague.",
    )
    ap.add_argument("--stub-cardio", action="store_true")
    ap.add_argument("--stub-neuro", action="store_true")
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    topic = args.topic
    if not topic and not sys.stdin.isatty():
        topic = sys.stdin.read().strip().splitlines()[0] if sys.stdin else ""
    if not topic:
        print("ERROR: no topic provided.", file=sys.stderr)
        return 2

    binary = find_3s_binary()
    if binary is None and not (args.stub_cardio and args.stub_neuro):
        print("ERROR: 3s binary not found and not all routes stubbed.", file=sys.stderr)
        return 3

    stamp = utc_stamp()
    out_md = Path(args.out_md or f"/tmp/3s-answer-brainstorm-{stamp}.md")
    out_json = Path(args.out_json or f"/tmp/3s-answer-brainstorm-{stamp}.json")

    res = run_loop(
        topic=topic,
        threshold=args.threshold,
        max_iters=args.max_iters,
        judge_strategy=args.judge_strategy,
        binary=binary or "",
        cost_cap=args.cost_cap,
        stub_cardio=args.stub_cardio,
        stub_neuro=args.stub_neuro,
        tiebreak_delta=args.tiebreak_delta,
        vague_score_cap=args.vague_score_cap,
        measurable_bounds_mode=args.measurable_bounds_mode,
        measurable_bounds_floor=args.measurable_bounds_floor,
    )

    md = render_md(res)
    out_md.write_text(md)
    res_dict = asdict(res)
    out_json.write_text(json.dumps(res_dict, indent=2))

    # Y1 Race Theater hook (opt-in via env, default off — no behavior change
    # unless RACE_PUBLISH=1). The publisher runs in-process via subprocess
    # so a publisher import error never breaks the brainstorm hot path.
    # ZSF: failures surface to /tmp/3s-answer-brainstorm.err.
    if os.environ.get("RACE_PUBLISH") == "1":
        _race_publish_hook(out_json, args.quiet)

    # Z3 Validation Tribunal escalation hook (opt-in via env, default off).
    # When TRIBUNAL_ESCALATE=1 and the loop returned UNRESOLVED post-tiebreak,
    # we OPEN a TribunalCase as a forward-compat extension point — the v0
    # scaffold does not auto-decide here (decide() requires an LLM consult
    # callable wired through the priority queue). The opened case is dropped
    # as JSON next to the loop result so the next wave's write-side (or a
    # human operator) can pick it up. ZSF: import / write failures surface
    # to /tmp/3s-answer-brainstorm.err and do NOT break the hot path.
    if os.environ.get("TRIBUNAL_ESCALATE") == "1":
        _tribunal_escalate_hook(res, out_json, args.quiet)

    if not args.quiet:
        print(md)

    # Exit code: 0 if converged, 1 if hit max-iters but produced an answer,
    # 2 if catastrophic (no answer at all).
    if res.converged:
        return 0
    if res.final_answer and "(no answer" not in res.final_answer:
        return 1
    return 2


def _race_publish_hook(loop_result_json: Path, quiet: bool) -> None:
    """Spawn `tools.fleet_race_publisher publish --in <file>` (best-effort).

    Y1 hook — opt-in only via ``RACE_PUBLISH=1``. Runs as a subprocess so
    an import / NATS failure cannot crash the brainstorm script. Exit code
    + stderr captured to ERR_LOG for ZSF observability.
    """
    repo_root = Path(__file__).resolve().parent.parent
    publisher = repo_root / "tools" / "fleet_race_publisher.py"
    if not publisher.is_file():
        append_err(f"{utc_stamp()}\trace-publish-hook\tmissing\t{publisher}")
        return
    try:
        cp = subprocess.run(
            [sys.executable, str(publisher), "publish", "--in", str(loop_result_json)],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(repo_root),
        )
    except subprocess.TimeoutExpired as e:
        append_err(f"{utc_stamp()}\trace-publish-hook\trc=timeout\terr={e!r}")
        return
    except OSError as e:
        append_err(f"{utc_stamp()}\trace-publish-hook\trc=oserror\terr={e!r}")
        return
    if cp.returncode != 0:
        # Snapshot-only success (rc=2) is still useful — log it but don't
        # alarm. Real failures (rc=1) get logged with full context.
        append_err(
            f"{utc_stamp()}\trace-publish-hook\trc={cp.returncode}\t"
            f"stdout={cp.stdout[:240]!r}\tstderr={cp.stderr[:240]!r}"
        )
    if not quiet and cp.stdout:
        print("\n--- race-publish hook ---")
        print(cp.stdout)


def _tribunal_escalate_hook(res: "LoopResult", loop_result_json: Path, quiet: bool) -> None:
    """Z3 hook — open a Validation Tribunal case for UNRESOLVED loops.

    Opt-in via ``TRIBUNAL_ESCALATE=1``. Triggers ONLY when the brainstorm
    loop reached a post-tiebreak UNRESOLVED state (i.e., the verdict is
    UNRESOLVED). Other final statuses (CONVERGED, MAX_ITERS, vague-Q) are
    NOT tribunal-eligible — the dispute layer is for genuine deadlocks,
    not unfinished questions.

    The hook OPENS a case and writes the case JSON next to the loop result.
    It does NOT call decide() — that requires a real LLM consult callable
    wired through ``memory.llm_priority_queue``, which the next-wave write
    side will provide. Treat this hook as a "queue for tribunal" lever; the
    write side dequeues + decides + archives.

    ZSF: any error path bumps a counter via the tribunal module AND records
    to ERR_LOG. The brainstorm hot path is never crashed.
    """
    if res.verdict != VERDICT_UNRESOLVED:
        return  # Not tribunal-eligible — nothing to do.

    repo_root = Path(__file__).resolve().parent.parent
    multifleet_path = repo_root / "multi-fleet"
    if str(multifleet_path) not in sys.path:
        sys.path.insert(0, str(multifleet_path))
    try:
        from multifleet.validation_tribunal import ValidationTribunal  # type: ignore
    except ImportError as e:
        append_err(f"{utc_stamp()}\ttribunal-escalate-hook\timport-failed\terr={e!r}")
        return

    try:
        tribunal = ValidationTribunal()
        # The brainstorm doesn't carry a race/evidence id directly; use the
        # loop_result_json path as the disputed-artifact pointer. Forward-
        # compat: when Race Theater is wired, swap this for the race_id.
        artifact_id = str(loop_result_json)
        reason = (
            f"3s-brainstorm UNRESOLVED post-tiebreak; "
            f"final_status={res.final_status} "
            f"tiebreak_invocations={res.tiebreak_invoked_count}"
        )
        case = tribunal.open_case(artifact_id, reason)
    except Exception as e:  # noqa: BLE001 — explicit ZSF capture.
        append_err(
            f"{utc_stamp()}\ttribunal-escalate-hook\topen-failed\terr={e!r}"
        )
        return

    case_path = loop_result_json.with_suffix(".tribunal-case.json")
    try:
        case_path.write_text(json.dumps(case.to_dict(), indent=2))
    except OSError as e:
        append_err(
            f"{utc_stamp()}\ttribunal-escalate-hook\twrite-failed\terr={e!r}"
        )
        return

    if not quiet:
        print(f"\n--- tribunal escalate hook ---")
        print(f"opened case_id={case.case_id}")
        print(f"case file: {case_path}")


if __name__ == "__main__":
    sys.exit(main())
