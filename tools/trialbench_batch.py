#!/usr/bin/env python3
"""TrialBench batch processor — cohort report across multiple trials.

Discovers all trial directories under artifacts/trialbench/<trial_id>/,
scores each via trialbench_score.score_trial(), then emits:

  reports/trialbench/cohort_report.json  — full JSON cohort summary
  reports/trialbench/cohort_report.md    — human-readable markdown

Per-case (task_id) statistics:
  mean, median, p25, p75 of primary_endpoint_success across arms and trials.

Per-learner (node_id) statistics:
  success_rate, n_runs, n_successes across all trials.

Stdlib only. Zero Silent Failures: every error is collected and written to the
report; no exception is swallowed silently.

CLI:
    python tools/trialbench_batch.py [--artifacts-dir DIR] [--reports-dir DIR]
                                     [--trial-ids ID [ID ...]]
                                     [--output-json PATH] [--output-md PATH]

API:
    from tools.trialbench_batch import run_batch
    report = run_batch()   # returns cohort dict
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path defaults (mirroring trialbench_score.py layout)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "trialbench"
REPORTS_DIR = REPO_ROOT / "reports" / "trialbench"

DEFAULT_JSON_OUT = REPORTS_DIR / "cohort_report.json"
DEFAULT_MD_OUT = REPORTS_DIR / "cohort_report.md"

# Make sibling module importable when running directly
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Stats helpers (stdlib only)
# ---------------------------------------------------------------------------


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Return q-th quantile of a pre-sorted list using linear interpolation."""
    if not sorted_vals:
        return float("nan")
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = lo + 1
    if hi >= n:
        return sorted_vals[-1]
    frac = pos - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def _describe(values: list[float]) -> dict[str, float | None]:
    """Return basic descriptive stats dict for a list of floats."""
    if not values:
        return {"n": 0, "mean": None, "median": None, "p25": None, "p75": None}
    sv = sorted(values)
    n = len(sv)
    mean = sum(sv) / n
    median = _quantile(sv, 0.5)
    p25 = _quantile(sv, 0.25)
    p75 = _quantile(sv, 0.75)
    return {"n": n, "mean": round(mean, 4), "median": round(median, 4),
            "p25": round(p25, 4), "p75": round(p75, 4)}


# ---------------------------------------------------------------------------
# Trial discovery
# ---------------------------------------------------------------------------


def _discover_trial_ids(artifacts_dir: Path) -> list[str]:
    """Return sorted list of trial_id dirs that contain at least one run_*.json."""
    if not artifacts_dir.exists():
        return []
    ids: list[str] = []
    for child in sorted(artifacts_dir.iterdir()):
        if child.is_dir() and any(child.glob("run_*.json")):
            ids.append(child.name)
    return ids


# ---------------------------------------------------------------------------
# Scored-outcomes loader (reads cached scored_outcomes.json from score_trial)
# ---------------------------------------------------------------------------


def _load_scored_outcomes(trial_dir: Path, errors: list[str]) -> list[dict[str, Any]]:
    """Load pre-scored outcomes JSON written by score_trial. Returns list of run dicts."""
    path = trial_dir / "scored_outcomes.json"
    if not path.exists():
        errors.append(f"scored_outcomes.json missing in {trial_dir.name} (run score_trial first or use --rescore)")
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            errors.append(f"{trial_dir.name}/scored_outcomes.json: expected list, got {type(data).__name__}")
            return []
        return data
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"failed to read {trial_dir.name}/scored_outcomes.json: {exc}")
        return []


# ---------------------------------------------------------------------------
# Core batch logic
# ---------------------------------------------------------------------------


def run_batch(
    *,
    artifacts_dir: Path | None = None,
    reports_dir: Path | None = None,
    trial_ids: list[str] | None = None,
    rescore: bool = False,
    bootstrap_iterations: int = 10_000,
    bootstrap_seed: int = 42,
) -> dict[str, Any]:
    """Score all trials and compute cohort statistics.

    Args:
        artifacts_dir: Root of trial artifact directories (default: ARTIFACTS_DIR).
        reports_dir:   Where to write cohort reports (default: REPORTS_DIR).
        trial_ids:     Explicit list; if None, auto-discovers from artifacts_dir.
        rescore:       If True, call score_trial() to regenerate scored_outcomes.json
                       before reading it. Useful when run artifacts were updated.
        bootstrap_iterations / bootstrap_seed: passed to score_trial when rescore=True.

    Returns cohort dict with keys:
        trial_count, run_count, errors, per_case, per_learner, per_arm,
        trial_summaries, cohort_report_json_path, cohort_report_md_path
    """
    artifacts_dir = artifacts_dir or ARTIFACTS_DIR
    reports_dir = reports_dir or REPORTS_DIR

    errors: list[str] = []

    # --- Trial discovery --------------------------------------------------
    if trial_ids is None:
        trial_ids = _discover_trial_ids(artifacts_dir)
    if not trial_ids:
        errors.append(f"no trial directories found under {artifacts_dir}")

    # --- Optionally rescore via score_trial --------------------------------
    if rescore and trial_ids:
        try:
            from tools.trialbench_score import score_trial  # type: ignore
        except ImportError as exc:
            errors.append(f"rescore requested but could not import trialbench_score: {exc}")
            rescore = False

    trial_summaries: list[dict[str, Any]] = []

    # Accumulation structures
    # per_case[task_id][arm] -> list of 0/1 success values
    per_case: dict[str, dict[str, list[float]]] = {}
    # per_learner[node_id] -> {"n": int, "successes": int}
    per_learner: dict[str, dict[str, int]] = {}
    # per_arm[arm] -> list of 0/1 success values
    per_arm: dict[str, list[float]] = {}

    total_runs = 0

    for tid in trial_ids:
        trial_dir = artifacts_dir / tid

        # Optionally refresh scored_outcomes.json
        if rescore:
            try:
                result = score_trial(  # type: ignore  # noqa: F821
                    tid,
                    artifacts_dir=artifacts_dir,
                    reports_dir=reports_dir,
                    bootstrap_iterations=bootstrap_iterations,
                    bootstrap_seed=bootstrap_seed,
                )
                if result.get("errors"):
                    for e in result["errors"]:
                        errors.append(f"[{tid}] {e}")
            except Exception as exc:
                errors.append(f"[{tid}] score_trial failed: {exc}")
                # Continue — we'll still try to read whatever exists

        # Load scored outcomes
        runs = _load_scored_outcomes(trial_dir, errors)
        total_runs += len(runs)

        trial_n = len(runs)
        trial_successes = 0

        for run in runs:
            success_val = float(bool(run.get("primary_endpoint_success", False)))
            task_id = str(run.get("task_id") or "unknown_task")
            node_id = str(run.get("node_id") or "unknown_learner")
            arm = str(run.get("arm") or "unknown_arm")

            trial_successes += int(success_val)

            # per_case accumulation
            per_case.setdefault(task_id, {})
            per_case[task_id].setdefault(arm, [])
            per_case[task_id][arm].append(success_val)

            # per_learner accumulation
            per_learner.setdefault(node_id, {"n": 0, "successes": 0})
            per_learner[node_id]["n"] += 1
            per_learner[node_id]["successes"] += int(success_val)

            # per_arm accumulation
            per_arm.setdefault(arm, [])
            per_arm[arm].append(success_val)

        trial_summaries.append({
            "trial_id": tid,
            "n_runs": trial_n,
            "n_successes": trial_successes,
            "success_rate": round(trial_successes / trial_n, 4) if trial_n else None,
        })

    # --- Per-case stats ----------------------------------------------------
    per_case_stats: dict[str, Any] = {}
    for task_id, arm_map in per_case.items():
        per_case_stats[task_id] = {}
        # Aggregate across all arms
        all_vals = [v for vals in arm_map.values() for v in vals]
        per_case_stats[task_id]["all_arms"] = _describe(all_vals)
        for arm, vals in sorted(arm_map.items()):
            per_case_stats[task_id][arm] = _describe(vals)

    # --- Per-learner stats -------------------------------------------------
    per_learner_stats: dict[str, Any] = {}
    for node_id, counts in per_learner.items():
        n = counts["n"]
        s = counts["successes"]
        per_learner_stats[node_id] = {
            "n_runs": n,
            "n_successes": s,
            "success_rate": round(s / n, 4) if n else None,
        }

    # --- Per-arm stats -----------------------------------------------------
    per_arm_stats: dict[str, Any] = {}
    for arm, vals in sorted(per_arm.items()):
        per_arm_stats[arm] = _describe(vals)

    # --- Assemble cohort report -------------------------------------------
    reports_dir.mkdir(parents=True, exist_ok=True)

    cohort: dict[str, Any] = {
        "trial_count": len(trial_ids),
        "run_count": total_runs,
        "trial_summaries": trial_summaries,
        "per_case": per_case_stats,
        "per_learner": per_learner_stats,
        "per_arm": per_arm_stats,
        "errors": errors,
        "cohort_report_json_path": str(DEFAULT_JSON_OUT),
        "cohort_report_md_path": str(DEFAULT_MD_OUT),
    }

    # Write JSON
    try:
        json_path = reports_dir / "cohort_report.json"
        with json_path.open("w", encoding="utf-8") as fh:
            json.dump(cohort, fh, indent=2, sort_keys=True)
        cohort["cohort_report_json_path"] = str(json_path)
    except OSError as exc:
        errors.append(f"failed to write cohort_report.json: {exc}")

    # Write Markdown
    try:
        md_path = reports_dir / "cohort_report.md"
        with md_path.open("w", encoding="utf-8") as fh:
            fh.write(_render_cohort_md(cohort))
        cohort["cohort_report_md_path"] = str(md_path)
    except OSError as exc:
        errors.append(f"failed to write cohort_report.md: {exc}")

    return cohort


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _fmt_rate(r: float | None) -> str:
    if r is None:
        return "—"
    return f"{r * 100:.1f}%"


def _fmt_stat(d: dict[str, Any] | None, key: str) -> str:
    if d is None:
        return "—"
    v = d.get(key)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _render_cohort_md(cohort: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# TrialBench Cohort Report")
    lines.append("")
    lines.append(f"**Trials**: {cohort.get('trial_count', 0)}  |  "
                 f"**Total runs**: {cohort.get('run_count', 0)}")
    lines.append("")

    # Trial summaries
    lines.append("## 1. Trial Summaries")
    lines.append("")
    lines.append("| Trial ID | Runs | Successes | Success Rate |")
    lines.append("|----------|------|-----------|--------------|")
    for ts in cohort.get("trial_summaries", []):
        lines.append(
            f"| {ts['trial_id']} | {ts['n_runs']} | {ts['n_successes']} "
            f"| {_fmt_rate(ts.get('success_rate'))} |"
        )
    lines.append("")

    # Per-arm
    lines.append("## 2. Per-Arm Statistics (all trials)")
    lines.append("")
    lines.append("| Arm | N | Mean | Median | P25 | P75 |")
    lines.append("|-----|---|------|--------|-----|-----|")
    for arm, d in cohort.get("per_arm", {}).items():
        lines.append(
            f"| {arm} | {_fmt_stat(d, 'n')} | {_fmt_stat(d, 'mean')} "
            f"| {_fmt_stat(d, 'median')} | {_fmt_stat(d, 'p25')} | {_fmt_stat(d, 'p75')} |"
        )
    lines.append("")

    # Per-case
    lines.append("## 3. Per-Case (task_id) Statistics")
    lines.append("")
    per_case = cohort.get("per_case", {})
    if not per_case:
        lines.append("_(no case data)_")
        lines.append("")
    else:
        lines.append("| Task ID | Arm | N | Mean | Median | P25 | P75 |")
        lines.append("|---------|-----|---|------|--------|-----|-----|")
        for task_id in sorted(per_case.keys()):
            arm_map = per_case[task_id]
            for arm in sorted(arm_map.keys()):
                d = arm_map[arm]
                lines.append(
                    f"| {task_id} | {arm} | {_fmt_stat(d, 'n')} "
                    f"| {_fmt_stat(d, 'mean')} | {_fmt_stat(d, 'median')} "
                    f"| {_fmt_stat(d, 'p25')} | {_fmt_stat(d, 'p75')} |"
                )
        lines.append("")

    # Per-learner
    lines.append("## 4. Per-Learner (node_id) Statistics")
    lines.append("")
    per_learner = cohort.get("per_learner", {})
    if not per_learner:
        lines.append("_(no learner data)_")
        lines.append("")
    else:
        lines.append("| Node ID | Runs | Successes | Success Rate |")
        lines.append("|---------|------|-----------|--------------|")
        for node_id in sorted(per_learner.keys()):
            d = per_learner[node_id]
            lines.append(
                f"| {node_id} | {d['n_runs']} | {d['n_successes']} "
                f"| {_fmt_rate(d.get('success_rate'))} |"
            )
        lines.append("")

    # Errors
    errors = cohort.get("errors", [])
    if errors:
        lines.append("## 5. Non-Fatal Errors")
        lines.append("")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="TrialBench batch scorer — cohort report across multiple trials."
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=ARTIFACTS_DIR,
        help=f"Root artifacts directory (default: {ARTIFACTS_DIR})",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=REPORTS_DIR,
        help=f"Output reports directory (default: {REPORTS_DIR})",
    )
    parser.add_argument(
        "--trial-ids",
        nargs="+",
        metavar="TRIAL_ID",
        default=None,
        help="Explicit trial IDs to process. If omitted, auto-discovers from artifacts-dir.",
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Re-run score_trial() for each trial before aggregating (re-generates scored_outcomes.json).",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Override output path for cohort_report.json.",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=None,
        help="Override output path for cohort_report.md.",
    )
    args = parser.parse_args(argv[1:])

    try:
        cohort = run_batch(
            artifacts_dir=args.artifacts_dir,
            reports_dir=args.reports_dir,
            trial_ids=args.trial_ids,
            rescore=args.rescore,
        )
    except Exception as exc:
        sys.stderr.write(f"run_batch failed: {exc}\n")
        return 1

    # Handle output path overrides
    if args.output_json and args.output_json != Path(cohort.get("cohort_report_json_path", "")):
        try:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            with args.output_json.open("w", encoding="utf-8") as fh:
                json.dump(cohort, fh, indent=2, sort_keys=True)
            cohort["cohort_report_json_path"] = str(args.output_json)
        except OSError as exc:
            sys.stderr.write(f"failed to write --output-json: {exc}\n")
            return 1

    if args.output_md and args.output_md != Path(cohort.get("cohort_report_md_path", "")):
        try:
            args.output_md.parent.mkdir(parents=True, exist_ok=True)
            with args.output_md.open("w", encoding="utf-8") as fh:
                fh.write(_render_cohort_md(cohort))
            cohort["cohort_report_md_path"] = str(args.output_md)
        except OSError as exc:
            sys.stderr.write(f"failed to write --output-md: {exc}\n")
            return 1

    json.dump(cohort, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if cohort.get("errors"):
        sys.stderr.write(f"{len(cohort['errors'])} non-fatal error(s) recorded\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
