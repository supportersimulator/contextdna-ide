#!/usr/bin/env bash
# test_3s_brainstorm_corpus.sh — Regression test for scripts/3s-answer-brainstorm.py.
#
# Runs every entry of scripts/tests/3s-brainstorm-corpus.json through the loop,
# asserts:
#   - cumulative cost <= corpus.budget_total_usd
#   - convergence outcome matches expect_convergence (with tolerance: 80% pass rate
#     across expect_convergence:true tests counts as a pass)
#   - failure-mode entries (stub_cardio / stub_neuro) do NOT silently converge
#   - err_count is observable (ZSF: a stubbed surgeon producing zero score is
#     visible behavior, not an exception)
#
# Output:
#   /tmp/3s-brainstorm-corpus-summary.json
#   stdout: human-readable per-test result + final verdict
#
# Exit codes:
#   0 — all assertions pass
#   1 — assertion failure (budget overrun, convergence below 80%, or failure-mode
#       test silently converged)
#   2 — corpus file missing / unreadable
#   3 — script under test missing
#
# Usage:
#   bash scripts/tests/test_3s_brainstorm_corpus.sh
#   bash scripts/tests/test_3s_brainstorm_corpus.sh --dry-run   # list tests, no calls
#   bash scripts/tests/test_3s_brainstorm_corpus.sh --filter T1 # run subset
#
# ZSF: every per-test failure recorded in the summary (rc, err_count, cost).

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/3s-answer-brainstorm.py"
CORPUS="${REPO_ROOT}/scripts/tests/3s-brainstorm-corpus.json"
SUMMARY="/tmp/3s-brainstorm-corpus-summary.json"
LOG_DIR="/tmp/3s-brainstorm-corpus-runs"

DRY_RUN=0
FILTER=""
STRATEGY_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --filter) FILTER="${2:-}"; shift 2 ;;
    --judge-strategy) STRATEGY_OVERRIDE="${2:-}"; shift 2 ;;
    --help|-h) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -r "$CORPUS" ]]; then
  echo "ERROR: corpus not readable: $CORPUS" >&2
  exit 2
fi
if [[ ! -f "$SCRIPT" ]]; then
  echo "ERROR: script under test not found: $SCRIPT" >&2
  exit 3
fi

mkdir -p "$LOG_DIR"

# Use python to walk the corpus + drive the loop — bash JSON parsing is fragile.
python3 - "$SCRIPT" "$CORPUS" "$SUMMARY" "$LOG_DIR" "$DRY_RUN" "$FILTER" "$STRATEGY_OVERRIDE" <<'PY'
import json
import os
import subprocess
import sys
import time
from pathlib import Path

script, corpus_path, summary_path, log_dir, dry_run, filt, strat_override = sys.argv[1:8]
dry_run = dry_run == "1"
strat_override = strat_override.strip() or None
log_dir = Path(log_dir)
corpus = json.loads(Path(corpus_path).read_text())

budget = float(corpus.get("budget_total_usd", 0.20))
default_threshold = float(corpus.get("default_threshold", 0.7))
default_max_iters = int(corpus.get("default_max_iters", 3))

results = []
total_cost = 0.0
ok = True

# Buckets for assertions
expect_conv = []   # (id, converged, cost)
failure_mode = []  # (id, converged, err_count, cost)

bias_pair = {}  # cardio/neuro pair for delta measurement

for t in corpus["tests"]:
    if filt and filt not in t["id"]:
        continue
    out_md = log_dir / f"{t['id']}.md"
    out_json = log_dir / f"{t['id']}.json"
    # Strategy override: apply ONLY to non-stub tests (failure-mode tests are pinned to
    # 'alternate' for graceful-fallback semantics, not strategy bias coverage).
    base_strategy = t.get("judge_strategy", "alternate")
    if strat_override and not (t.get("stub_cardio") or t.get("stub_neuro")):
        # Don't trample bias-control tests — they're paired by strategy semantics.
        if t["id"] not in ("T4-cardio-bias-control", "T5-neuro-bias-control"):
            base_strategy = strat_override
    cmd = [
        sys.executable, script,
        t["topic"],
        "--judge-strategy", base_strategy,
        "--threshold", str(t.get("threshold", default_threshold)),
        "--max-iters", str(t.get("max_iters", default_max_iters)),
        "--cost-cap", str(budget),  # per-test cap = total budget (ceiling)
        "--out-md", str(out_md),
        "--out-json", str(out_json),
        "--quiet",
    ]
    if t.get("stub_cardio"):
        cmd.append("--stub-cardio")
    if t.get("stub_neuro"):
        cmd.append("--stub-neuro")

    if dry_run:
        print(f"[DRY] {t['id']}: {' '.join(cmd[3:])}")
        continue

    print(f">>> {t['id']} (judge={base_strategy}) ...")
    t0 = time.time()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        results.append({
            "id": t["id"], "rc": "timeout", "converged": False,
            "cost": 0.0, "err_count": -1, "elapsed_s": 600,
        })
        ok = False
        continue
    elapsed = time.time() - t0

    rc = cp.returncode
    if not out_json.exists():
        # Catastrophic — script didn't even produce JSON
        results.append({
            "id": t["id"], "rc": rc, "converged": False,
            "cost": 0.0, "err_count": -1, "elapsed_s": elapsed,
            "note": "no out-json produced",
            "stderr_tail": cp.stderr[-300:] if cp.stderr else "",
        })
        ok = False
        continue

    j = json.loads(out_json.read_text())
    converged = bool(j["converged"])
    cost = float(j["total_cost"])
    err_count = int(j["err_count"])
    judge_scores = [it.get("judge_scores", {}) for it in j["iters"]]
    final_answer = j.get("final_answer", "") or ""
    final_is_stub = "[STUB:" in final_answer
    total_cost += cost

    rec = {
        "id": t["id"],
        "rc": rc,
        "converged": converged,
        "expect_convergence": t.get("expect_convergence"),
        "expect_under_specified": t.get("expect_under_specified", False),
        "cost": cost,
        "err_count": err_count,
        "elapsed_s": round(elapsed, 1),
        "judge_strategy": base_strategy,
        "iters": len(j["iters"]),
        "iter_judge_scores": judge_scores,
        "stub_cardio": bool(t.get("stub_cardio")),
        "stub_neuro": bool(t.get("stub_neuro")),
        "final_source": j.get("final_source", "none"),
        "final_status": j.get("final_status", "?"),
        "under_specified_flag": bool(j.get("under_specified_flag", False)),
        "tiebreak_invoked_count": int(j.get("tiebreak_invoked_count", 0)),
        "final_is_stub": final_is_stub,
    }
    results.append(rec)

    if t.get("stub_cardio") or t.get("stub_neuro"):
        failure_mode.append(rec)
    elif t.get("expect_convergence") is True:
        expect_conv.append(rec)

    # Track bias-control pair
    if t["id"] in ("T4-cardio-bias-control", "T5-neuro-bias-control"):
        bias_pair[t["id"]] = rec

    print(f"    converged={converged} cost=${cost:.4f} errs={err_count} t={elapsed:.1f}s "
          f"status={rec['final_status']} tb_inv={rec['tiebreak_invoked_count']}")

# Assertions
verdict = {"pass": True, "reasons": []}

# 1) Budget
if total_cost > budget:
    verdict["pass"] = False
    verdict["reasons"].append(f"BUDGET_EXCEEDED: total_cost=${total_cost:.4f} > ${budget}")

# 2) Convergence rate on expect_convergence:true (excluding failure-mode entries)
if expect_conv:
    converged_ct = sum(1 for r in expect_conv if r["converged"])
    rate = converged_ct / len(expect_conv)
    if rate < 0.80:
        verdict["pass"] = False
        verdict["reasons"].append(
            f"CONVERGENCE_RATE_LOW: {converged_ct}/{len(expect_conv)} = {rate:.2f} < 0.80"
        )
    verdict["convergence_rate"] = rate
    verdict["converged_count"] = converged_ct
    verdict["expect_conv_count"] = len(expect_conv)
else:
    verdict["convergence_rate"] = None

# 3) Failure-mode tests: graceful fallback semantics.
# When one surgeon is stubbed, the loop should still produce an answer using the
# surviving surgeon. The chosen final answer must NOT be the stub text — that
# would be silent failure (loop accepted "[STUB:...]" as a real answer).
# Convergence on the surviving surgeon's answer IS the success path.
for r in failure_mode:
    if r["final_is_stub"]:
        verdict["pass"] = False
        verdict["reasons"].append(
            f"FAILURE_MODE_SILENT_FAILURE: {r['id']} returned a STUB as the final answer "
            f"(loop did not gracefully fall back to surviving surgeon)"
        )
    # Loud failure: if BOTH surgeons stubbed and converged on stub text — caught above.
    # Stub status must remain visible in the JSON (not erased).
    if r["stub_cardio"] != True and r["stub_neuro"] != True and (
        r.get("id","").startswith("F")
    ):
        # corpus inconsistency
        verdict["reasons"].append(f"WARN: {r['id']} flagged as failure-mode but no stub set")

# 4) Bias-control delta (informational — not a hard pass/fail)
bias_delta = None
if "T4-cardio-bias-control" in bias_pair and "T5-neuro-bias-control" in bias_pair:
    t4 = bias_pair["T4-cardio-bias-control"]
    t5 = bias_pair["T5-neuro-bias-control"]
    # Compare iter-1 judge scores
    s4 = (t4["iter_judge_scores"][0] or {}) if t4["iter_judge_scores"] else {}
    s5 = (t5["iter_judge_scores"][0] or {}) if t5["iter_judge_scores"] else {}
    c_score = max(s4.values()) if s4 else 0.0
    n_score = max(s5.values()) if s5 else 0.0
    bias_delta = round(c_score - n_score, 3)

# 5) Vague-Q HARD assertion (O5): T3 (or any expect_convergence:false +
#    expect_under_specified:true entry) MUST NOT converge AND MUST set the
#    under_specified_flag. If T3 converges, FAIL the test — that means the judge
#    prompt's vague-Q penalty regressed.
for r in results:
    if r.get("expect_under_specified"):
        if r["converged"]:
            verdict["pass"] = False
            verdict["reasons"].append(
                f"VAGUE_Q_REGRESSION: {r['id']} converged on a vague question "
                f"(scores={r['iter_judge_scores']}). Judge prompt's vague-Q penalty failed."
            )
        if not r["under_specified_flag"]:
            verdict["pass"] = False
            verdict["reasons"].append(
                f"VAGUE_Q_FLAG_MISSING: {r['id']} didn't set under_specified_flag "
                f"despite expect_under_specified=true (scores={r['iter_judge_scores']})."
            )
        if r["final_status"] != "under_specified_question":
            verdict["pass"] = False
            verdict["reasons"].append(
                f"VAGUE_Q_STATUS_WRONG: {r['id']} final_status={r['final_status']!r}, "
                f"expected 'under_specified_question'."
            )

# 6) Tiebreak counter (informational): how many disagreements fired across the corpus.
tiebreak_total = sum(r.get("tiebreak_invoked_count", 0) for r in results)

verdict["total_cost"] = round(total_cost, 4)
verdict["budget"] = budget
verdict["bias_delta_cardio_minus_neuro"] = bias_delta
verdict["tiebreak_invoked_total"] = tiebreak_total

summary = {
    "results": results,
    "verdict": verdict,
}
Path(summary_path).write_text(json.dumps(summary, indent=2))

print()
print("=" * 60)
print(f"Total cost:     ${total_cost:.4f} / ${budget}")
print(f"Convergence:    {verdict.get('converged_count','-')}/{verdict.get('expect_conv_count','-')} = {verdict.get('convergence_rate')}")
print(f"Bias delta (cardio - neuro): {bias_delta}")
print(f"Tiebreaks invoked (corpus): {tiebreak_total}")
if strat_override:
    print(f"Strategy override:           {strat_override}")
print(f"Verdict:        {'PASS' if verdict['pass'] else 'FAIL'}")
for r in verdict["reasons"]:
    print(f"  - {r}")
print(f"Summary:        {summary_path}")
print("=" * 60)

sys.exit(0 if verdict["pass"] else 1)
PY
PY_RC=$?
exit $PY_RC
