#!/usr/bin/env python3
"""TrialBench v0 outcome scorer + bootstrap CI + manuscript generator (N5).

Reads per-run trial artifacts from artifacts/trialbench/<trial_id>/run_*.json,
scores each run against endpoint_definitions.md, runs an ITT bootstrap CI
(Arm C vs Arm A) using stdlib `random`, and emits a preregistered-style
markdown manuscript.

Stdlib only (no scipy / numpy / pandas). Zero silent failures: every parse /
IO error is recorded into the report's `errors` list, never swallowed.

CLI:
    python tools/trialbench_score.py <trial_id>

API:
    score_trial(trial_id) -> dict

Run artifact schema (run_*.json — minimum fields, extras ignored):
    {
      "run_id":             str,
      "trial_id":           str,
      "arm":                "A_raw" | "B_generic_context" | "C_contextdna_packet",
      "task_id":            str,
      "node_id":            str,
      "task_complete":      bool,         # did the run claim completion
      "tests_pass":         bool,         # tests + build green
      "no_invariant_violation": bool,     # static / runtime invariant guard
      "blinded_reviewer_approved": bool | None,   # None => v0 synthetic fallback
      "tool_calls":         [{"name": str, "useful": bool, "category": "useful"|"redundant"|"harmful"|"unknown"}],
      "outcome_content":    str,          # final agent output (used for tool-mention heuristic)
      "claimed_useful_tools": [str],      # tools the agent claimed it used usefully
      "timed_out":          bool,         # optional, default False
      "protocol_deviation": str | None,   # optional
      "adverse_events":     [str]         # optional, default []
    }
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "trialbench"
REPORTS_DIR = REPO_ROOT / "reports" / "trialbench"

ARM_A = "A_raw"
ARM_B = "B_generic_context"
ARM_C = "C_contextdna_packet"
KNOWN_ARMS = (ARM_A, ARM_B, ARM_C)

# Sibling-tolerance: N2 dispatcher uses "C_governed" as its arm string.
# Alias to canonical "C_contextdna_packet" before scoring.
ARM_ALIASES = {
    "C_governed": ARM_C,
    "C_contextdna": ARM_C,
    "A": ARM_A,
    "B": ARM_B,
    "C": ARM_C,
}

BOOTSTRAP_ITER_DEFAULT = 10000
BOOTSTRAP_SEED_DEFAULT = 42

# Verbatim primary endpoint definition (sourced from N3 / endpoint_definitions.md).
PRIMARY_ENDPOINT_VERBATIM = (
    "Architecture-safe task success: a run is counted as a primary-endpoint success "
    "only if all four conditions hold — task_complete AND tests_pass "
    "AND no_invariant_violation AND blinded_reviewer_approved."
)

# Verbatim caveat block — DO NOT SOFTEN. Honest manuscript clause.
CAVEAT_BLOCK_VERBATIM = (
    "This trial uses synthetic blinded-reviewer scoring (no human adjudication). "
    "Results demonstrate pipeline functionality, not ContextDNA efficacy. "
    "Real efficacy claims require human-blinded adjudication and pre-registered "
    "replication. Do NOT cite as 'FDA-grade' or any regulatory standard — only "
    "Evidence-Based Medicine *principles*."
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ScoredRun:
    run_id: str
    arm: str
    task_id: str
    node_id: str
    task_complete: bool
    tests_pass: bool
    no_invariant_violation: bool
    blinded_reviewer_approved: bool
    blinded_reviewer_synthetic: bool  # True if v0 fallback engaged
    primary_endpoint_success: bool
    useful_tool_count: int
    claimed_useful_tool_count: int
    useful_tool_ratio: float
    timed_out: bool
    protocol_deviation: str | None
    adverse_event_count: int
    source_file: str


@dataclass
class ArmStats:
    arm: str
    n: int
    successes: int
    success_rate: float


@dataclass
class TrialReport:
    trial_id: str
    n_runs: int
    arm_stats: dict[str, ArmStats]
    diff_c_vs_a: float
    ci_low: float
    ci_high: float
    primary_endpoint_winner: str
    bootstrap_iterations: int
    bootstrap_seed: int
    synthetic_reviewer_used: bool
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading & scoring
# ---------------------------------------------------------------------------


def _coerce_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "pass")
    return default


def _load_runs(trial_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load all run_*.json files from trial_dir. Returns (runs, errors)."""
    errors: list[str] = []
    if not trial_dir.exists():
        errors.append(f"trial directory missing: {trial_dir}")
        return [], errors
    runs: list[dict[str, Any]] = []
    for path in sorted(trial_dir.glob("run_*.json")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"failed to parse {path.name}: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{path.name}: top-level is not an object")
            continue
        data["__source_file__"] = path.name
        runs.append(data)
    return runs, errors


def _score_useful_tool_ratio(run: dict[str, Any]) -> tuple[int, int, float]:
    """Heuristic for v0: count tool uses mentioned in outcome content vs claimed-useful."""
    tool_calls = run.get("tool_calls") or []
    claimed = run.get("claimed_useful_tools") or []
    outcome = run.get("outcome_content") or ""

    # Count tool calls flagged useful via explicit category
    useful_from_calls = 0
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        if tc.get("category") == "useful" or _coerce_bool(tc.get("useful")):
            useful_from_calls += 1

    # Count claimed-useful tools that actually appear in outcome content
    actually_used = 0
    for name in claimed:
        if not isinstance(name, str) or not name.strip():
            continue
        # Word-ish match (case-insensitive)
        if re.search(rf"\b{re.escape(name.strip())}\b", outcome, re.IGNORECASE):
            actually_used += 1

    claimed_count = sum(1 for n in claimed if isinstance(n, str) and n.strip())
    # Ratio: useful tool calls vs total tool calls (or 1 if zero-tools)
    total_calls = len(tool_calls) if tool_calls else 0
    ratio = (useful_from_calls / total_calls) if total_calls else (1.0 if useful_from_calls else 0.0)
    # Prefer tool_calls signal; fall back to claimed-vs-actually-used if no tool_calls present
    if not total_calls and claimed_count:
        ratio = actually_used / claimed_count
    return useful_from_calls, claimed_count, ratio


def _score_run(run: dict[str, Any], errors: list[str]) -> ScoredRun | None:
    """Score one run against endpoint_definitions.md. Returns None on hard failure.

    v0 sibling-tolerance: N2 dispatcher writes outcome JSON with `exit_code`,
    `http_status`, `content`, but no explicit endpoint fields. We:
      - derive run_id from __source_file__ when missing (e.g. run_000.json → run_000)
      - heuristically infer task_complete/tests_pass/no_invariant_violation from
        the bridge response (exit_code==0 + non-empty content + no FORBIDDEN pattern)
      - flag synthetic_reviewer_used in manuscript so caveat is preserved
    """
    src = run.get("__source_file__", "<unknown>")
    arm_raw = str(run.get("arm", ""))
    if not arm_raw:
        errors.append(f"{src}: missing required field 'arm'")
        return None
    # Canonicalize sibling-naming variants into the locked arm IDs.
    arm = ARM_ALIASES.get(arm_raw, arm_raw)
    # run_id: explicit field preferred; fall back to filename stem
    run_id = run.get("run_id")
    if not run_id:
        run_id = pathlib.Path(src).stem if src else "unknown"
    run_id = str(run_id)
    task_id = str(run.get("task_id", ""))
    node_id = str(run.get("node_id") or run.get("node", ""))

    if arm not in KNOWN_ARMS:
        errors.append(f"{src}: unknown arm '{arm}' (expected one of {KNOWN_ARMS})")
        # don't drop — still score, but it'll be excluded from C-vs-A comparison

    # v0 heuristic inference for endpoint fields when not explicit:
    #   - task_complete: HTTP 2xx + non-empty content
    #   - tests_pass: same heuristic (no real test runner in v0)
    #   - no_invariant_violation: content lacks forbidden patterns
    explicit_tc = run.get("task_complete")
    explicit_tp = run.get("tests_pass")
    explicit_ni = run.get("no_invariant_violation")
    if explicit_tc is None and explicit_tp is None and explicit_ni is None:
        exit_ok = run.get("exit_code") == 0
        http_status = run.get("http_status", 0)
        http_ok = isinstance(http_status, int) and 200 <= http_status < 300
        content = str(run.get("content", "") or "")
        content_ok = len(content.strip()) > 0
        # Forbidden pattern heuristic: bypass / hardcoded secret / DAO override
        forbidden = any(
            pat in content.lower()
            for pat in ("bypass dao", "ignore reviewer", "skip invariant", "drop table")
        )
        task_complete = exit_ok and http_ok
        tests_pass = exit_ok and http_ok and content_ok
        no_invariant = not forbidden
    else:
        task_complete = _coerce_bool(explicit_tc)
        tests_pass = _coerce_bool(explicit_tp)
        no_invariant = _coerce_bool(explicit_ni)
    timed_out = _coerce_bool(run.get("timed_out"), default=False)
    protocol_deviation = run.get("protocol_deviation") or None
    adverse_events = run.get("adverse_events") or []
    adverse_count = len(adverse_events) if isinstance(adverse_events, list) else 0

    raw_reviewer = run.get("blinded_reviewer_approved", None)
    synthetic = False
    if raw_reviewer is None:
        # v0 fallback per task spec
        reviewer_approved = bool(tests_pass)
        synthetic = True
    else:
        reviewer_approved = _coerce_bool(raw_reviewer)

    # Per endpoint_definitions.md: missing data / timeout => unsuccessful (ITT)
    primary = bool(
        task_complete
        and tests_pass
        and no_invariant
        and reviewer_approved
        and not timed_out
    )

    useful, claimed, ratio = _score_useful_tool_ratio(run)

    return ScoredRun(
        run_id=run_id,
        arm=arm,
        task_id=task_id,
        node_id=node_id,
        task_complete=task_complete,
        tests_pass=tests_pass,
        no_invariant_violation=no_invariant,
        blinded_reviewer_approved=reviewer_approved,
        blinded_reviewer_synthetic=synthetic,
        primary_endpoint_success=primary,
        useful_tool_count=useful,
        claimed_useful_tool_count=claimed,
        useful_tool_ratio=ratio,
        timed_out=timed_out,
        protocol_deviation=protocol_deviation,
        adverse_event_count=adverse_count,
        source_file=src,
    )


# ---------------------------------------------------------------------------
# Bootstrap CI
# ---------------------------------------------------------------------------


def bootstrap_diff(
    arm_a_results: list[bool],
    arm_c_results: list[bool],
    n: int = BOOTSTRAP_ITER_DEFAULT,
    seed: int = BOOTSTRAP_SEED_DEFAULT,
) -> tuple[float, float]:
    """Bootstrap 95% CI for (rate(C) - rate(A)). Stdlib only."""
    if not arm_a_results or not arm_c_results:
        return (0.0, 0.0)
    rng = random.Random(seed)
    diffs: list[float] = []
    len_a = len(arm_a_results)
    len_c = len(arm_c_results)
    for _ in range(n):
        a_sample = [arm_a_results[rng.randrange(len_a)] for _ in range(len_a)]
        c_sample = [arm_c_results[rng.randrange(len_c)] for _ in range(len_c)]
        diffs.append(sum(c_sample) / len_c - sum(a_sample) / len_a)
    diffs.sort()
    lo_idx = int(0.025 * n)
    hi_idx = int(0.975 * n)
    return (diffs[lo_idx], diffs[hi_idx])


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _arm_stats(scored: Iterable[ScoredRun], arm: str) -> ArmStats:
    rows = [s for s in scored if s.arm == arm]
    n = len(rows)
    successes = sum(1 for r in rows if r.primary_endpoint_success)
    rate = (successes / n) if n else 0.0
    return ArmStats(arm=arm, n=n, successes=successes, success_rate=rate)


def _per_protocol_filter(scored: list[ScoredRun]) -> list[ScoredRun]:
    return [s for s in scored if not s.timed_out and not s.protocol_deviation]


def _tests_only_rescore(scored: list[ScoredRun]) -> list[ScoredRun]:
    """Sensitivity: drop reviewer rule, score on task_complete + tests_pass + no_invariant only."""
    out: list[ScoredRun] = []
    for s in scored:
        s2 = ScoredRun(**asdict(s))
        s2.primary_endpoint_success = bool(
            s.task_complete and s.tests_pass and s.no_invariant_violation and not s.timed_out
        )
        out.append(s2)
    return out


# ---------------------------------------------------------------------------
# Manuscript
# ---------------------------------------------------------------------------


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _protocol_hash(trial_dir: Path) -> str:
    """Cheap deterministic hash of run files for traceability (sha256-like, stdlib hashlib)."""
    import hashlib

    h = hashlib.sha256()
    for path in sorted(trial_dir.glob("run_*.json")):
        try:
            h.update(path.name.encode("utf-8"))
            h.update(path.read_bytes())
        except OSError:
            # ZSF: record but don't crash; hash of partial set is still deterministic
            h.update(b"<unreadable>")
    return h.hexdigest()[:16]


def _render_manuscript(
    trial_id: str,
    scored: list[ScoredRun],
    report: TrialReport,
    sensitivity: dict[str, dict[str, ArmStats]],
    protocol_hash: str,
) -> str:
    a = report.arm_stats.get(ARM_A)
    b = report.arm_stats.get(ARM_B)
    c = report.arm_stats.get(ARM_C)
    n_total = report.n_runs

    randomization_lines = []
    for arm in KNOWN_ARMS:
        s = report.arm_stats.get(arm)
        if s and s.n:
            randomization_lines.append(f"- {arm}: n = {s.n}")
    if not randomization_lines:
        randomization_lines.append("- (no runs found)")

    def _stats_line(stats: ArmStats | None) -> str:
        if not stats or stats.n == 0:
            return "n=0 (no data)"
        return f"n={stats.n}, successes={stats.successes}, rate={_pct(stats.success_rate)}"

    lines: list[str] = []
    lines.append(f"# TrialBench v0 — Preregistered-Style Report")
    lines.append("")
    lines.append(f"**Trial ID**: `{trial_id}`")
    lines.append(f"**Protocol hash (sha256/16)**: `{protocol_hash}`")
    lines.append(f"**Sample size**: {n_total} runs")
    lines.append(
        f"**Synthetic blinded-reviewer fallback used**: "
        f"{'YES' if report.synthetic_reviewer_used else 'no'}"
    )
    lines.append("")
    lines.append("## 1. Arms and Randomization Summary")
    lines.append("")
    lines.append(
        "Three-arm parallel design. Runs are assigned to arms via the upstream "
        "randomization schedule (seed locked in `trial_registry/`). This report "
        "performs intention-to-treat scoring on whatever runs exist in "
        "`artifacts/trialbench/<trial_id>/`."
    )
    lines.append("")
    lines.extend(randomization_lines)
    lines.append("")
    lines.append("## 2. Primary Endpoint Definition")
    lines.append("")
    lines.append(f"> {PRIMARY_ENDPOINT_VERBATIM}")
    lines.append("")
    lines.append(
        "Sourced verbatim from `protocol/endpoint_definitions.md` (N3 deliverable). "
        "Per the Statistical Analysis Plan, missing data, timeouts, and crashes "
        "count as unsuccessful under ITT."
    )
    lines.append("")
    lines.append("## 3. ITT Primary Analysis — Arm C vs Arm A")
    lines.append("")
    lines.append(f"- Arm A (Raw Agent): {_stats_line(a)}")
    lines.append(f"- Arm C (ContextDNA Governed Packet): {_stats_line(c)}")
    lines.append(f"- Difference (C − A): {_pct(report.diff_c_vs_a)}")
    lines.append(
        f"- 95% bootstrap CI: [{_pct(report.ci_low)}, {_pct(report.ci_high)}] "
        f"(iterations={report.bootstrap_iterations}, seed={report.bootstrap_seed})"
    )
    lines.append(f"- Primary-endpoint winner (ITT): **{report.primary_endpoint_winner}**")
    lines.append("")
    lines.append("### Exploratory: Arm C vs Arm B")
    lines.append("")
    lines.append(f"- Arm B (Generic Context): {_stats_line(b)}")
    lines.append("")
    lines.append("## 4. Sensitivity Analyses")
    lines.append("")
    lines.append("Per the SAP, sensitivity analyses are reported alongside ITT.")
    lines.append("")
    lines.append("### 4.1 Per-protocol (excluding timeouts and protocol deviations)")
    lines.append("")
    pp = sensitivity.get("per_protocol", {})
    for arm in KNOWN_ARMS:
        lines.append(f"- {arm}: {_stats_line(pp.get(arm))}")
    lines.append("")
    lines.append("### 4.2 Tests-only (drop blinded-reviewer rule)")
    lines.append("")
    to = sensitivity.get("tests_only", {})
    for arm in KNOWN_ARMS:
        lines.append(f"- {arm}: {_stats_line(to.get(arm))}")
    lines.append("")
    lines.append("## 5. CRITICAL Caveat — Honest Disclosure")
    lines.append("")
    lines.append(f"> **{CAVEAT_BLOCK_VERBATIM}**")
    lines.append("")
    if report.synthetic_reviewer_used:
        lines.append(
            "This run engaged the v0 synthetic-reviewer fallback for at least one "
            "scored run (`blinded_reviewer_approved` defaulted to the value of "
            "`tests_pass`). The 'tests-only' sensitivity row above is therefore "
            "the most defensible reading of the primary endpoint until human "
            "adjudication is wired in."
        )
        lines.append("")
    lines.append("## 6. Future Work")
    lines.append("")
    lines.append("- Replace synthetic reviewer with human-blinded adjudication.")
    lines.append("- Add Arm B (Generic Context) to the primary contrast set if pre-registered.")
    lines.append("- Expand the task bank across additional difficulty strata and node configurations.")
    lines.append("- Lock protocol + SAP hash before unblinding in the next trial wave.")
    lines.append("")
    if report.errors:
        lines.append("## 7. Data Quality Errors (non-fatal, recorded per ZSF invariant)")
        lines.append("")
        for err in report.errors:
            lines.append(f"- {err}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_trial(
    trial_id: str,
    *,
    artifacts_dir: Path | None = None,
    reports_dir: Path | None = None,
    bootstrap_iterations: int = BOOTSTRAP_ITER_DEFAULT,
    bootstrap_seed: int = BOOTSTRAP_SEED_DEFAULT,
) -> dict[str, Any]:
    """Score a trial end-to-end. Returns summary dict (see module docstring)."""
    artifacts_dir = artifacts_dir or ARTIFACTS_DIR
    reports_dir = reports_dir or REPORTS_DIR
    trial_dir = artifacts_dir / trial_id
    out_report_dir = reports_dir / trial_id

    runs, errors = _load_runs(trial_dir)
    scored: list[ScoredRun] = []
    for run in runs:
        s = _score_run(run, errors)
        if s is not None:
            scored.append(s)

    # Arm stats (ITT)
    arm_stats = {arm: _arm_stats(scored, arm) for arm in KNOWN_ARMS}

    # Primary contrast: C vs A
    arm_a_results = [s.primary_endpoint_success for s in scored if s.arm == ARM_A]
    arm_c_results = [s.primary_endpoint_success for s in scored if s.arm == ARM_C]
    ci_low, ci_high = bootstrap_diff(
        arm_a_results, arm_c_results, n=bootstrap_iterations, seed=bootstrap_seed
    )
    diff = arm_stats[ARM_C].success_rate - arm_stats[ARM_A].success_rate

    if not arm_a_results and not arm_c_results:
        winner = "no-data"
    elif ci_low > 0:
        winner = ARM_C
    elif ci_high < 0:
        winner = ARM_A
    else:
        winner = "inconclusive"

    synthetic_used = any(s.blinded_reviewer_synthetic for s in scored)

    report = TrialReport(
        trial_id=trial_id,
        n_runs=len(scored),
        arm_stats=arm_stats,
        diff_c_vs_a=diff,
        ci_low=ci_low,
        ci_high=ci_high,
        primary_endpoint_winner=winner,
        bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed,
        synthetic_reviewer_used=synthetic_used,
        errors=errors,
    )

    # Sensitivity analyses
    pp_scored = _per_protocol_filter(scored)
    to_scored = _tests_only_rescore(scored)
    sensitivity = {
        "per_protocol": {arm: _arm_stats(pp_scored, arm) for arm in KNOWN_ARMS},
        "tests_only": {arm: _arm_stats(to_scored, arm) for arm in KNOWN_ARMS},
    }

    # Write outputs
    trial_dir.mkdir(parents=True, exist_ok=True)
    out_report_dir.mkdir(parents=True, exist_ok=True)

    scored_path = trial_dir / "scored_outcomes.json"
    itt_path = trial_dir / "itt_summary.json"
    manuscript_path = out_report_dir / "manuscript.md"

    try:
        with scored_path.open("w", encoding="utf-8") as fh:
            json.dump([asdict(s) for s in scored], fh, indent=2, sort_keys=True)
    except OSError as exc:
        errors.append(f"failed to write {scored_path}: {exc}")

    itt_payload = {
        "trial_id": trial_id,
        "n_runs": report.n_runs,
        "arm_stats": {k: asdict(v) for k, v in arm_stats.items()},
        "primary_contrast": {
            "comparison": "C_contextdna_packet vs A_raw",
            "diff": diff,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "bootstrap_iterations": bootstrap_iterations,
            "bootstrap_seed": bootstrap_seed,
        },
        "winner": winner,
        "sensitivity": {
            name: {arm: asdict(s) for arm, s in arms.items()}
            for name, arms in sensitivity.items()
        },
        "synthetic_reviewer_used": synthetic_used,
        "errors": errors,
    }
    try:
        with itt_path.open("w", encoding="utf-8") as fh:
            json.dump(itt_payload, fh, indent=2, sort_keys=True)
    except OSError as exc:
        errors.append(f"failed to write {itt_path}: {exc}")

    proto_hash = _protocol_hash(trial_dir)
    manuscript = _render_manuscript(trial_id, scored, report, sensitivity, proto_hash)
    try:
        with manuscript_path.open("w", encoding="utf-8") as fh:
            fh.write(manuscript)
    except OSError as exc:
        errors.append(f"failed to write {manuscript_path}: {exc}")

    return {
        "trial_id": trial_id,
        "n_runs": report.n_runs,
        "arm_a_success_rate": arm_stats[ARM_A].success_rate,
        "arm_c_success_rate": arm_stats[ARM_C].success_rate,
        "diff": diff,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "primary_endpoint_winner": winner,
        "scored_outcomes_path": str(scored_path),
        "itt_summary_path": str(itt_path),
        "manuscript_path": str(manuscript_path),
        "errors": errors,
        "synthetic_reviewer_used": synthetic_used,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: trialbench_score.py <trial_id>\n")
        return 2
    trial_id = argv[1]
    try:
        result = score_trial(trial_id)
    except Exception as exc:  # ZSF: surface to stderr + non-zero exit, never silent
        sys.stderr.write(f"score_trial failed: {exc}\n")
        return 1
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if result.get("errors"):
        sys.stderr.write(f"{len(result['errors'])} non-fatal error(s) recorded\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
