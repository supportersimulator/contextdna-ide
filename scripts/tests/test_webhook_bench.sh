#!/usr/bin/env bash
# RACE W4 — Webhook benchmark regression test.
#
# Runs scripts/webhook-bench.py in a subprocess and asserts perf budgets.
# Baselines (docs/webhook-perf-baseline.md):
#   cold-p95 wall      < 20000 ms  (target after S4-S6 fix series; was 46s)
#   warm-p95 wall      <  1500 ms  (allows ~700ms python startup overhead +
#                                   ~500ms slack on top of ~318ms parallel_ms)
#   worst section      < 10000 ms  (no single section may dominate)
#
# Why parallel_ms vs wall: parallel_ms = pure injection work. wall_ms includes
# python start-up + module import. The webhook hot path that Atlas experiences
# is parallel_ms (post-import). We assert BOTH so a regression in either layer
# is caught.
#
# Exit codes:
#   0 — all asserts pass
#   1 — one or more budgets breached (regression)
#   2 — bench failed to run (infrastructure)
#
# Override knobs (env):
#   WEBHOOK_BENCH_COLD_P95_MS  (default 20000)
#   WEBHOOK_BENCH_WARM_P95_MS  (default 1500)
#   WEBHOOK_BENCH_WORST_MS     (default 10000)
#   WEBHOOK_BENCH_COLD_ITERS   (default 3)
#   WEBHOOK_BENCH_WARM_ITERS   (default 10)
#   WEBHOOK_BENCH_PYTHON       (default <repo>/.venv/bin/python3)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Resolve python: prefer explicit override, else worktree .venv, else
# walk up parents (worktrees often share the parent repo's .venv).
_default_python() {
  local candidate="${REPO_ROOT}/.venv/bin/python3"
  if [[ -x "${candidate}" ]]; then
    echo "${candidate}"; return 0
  fi
  # Walk up to /, looking for a .venv/bin/python3 (typical for git worktrees).
  local dir="${REPO_ROOT}"
  while [[ "${dir}" != "/" ]]; do
    dir="$(dirname "${dir}")"
    if [[ -x "${dir}/.venv/bin/python3" ]]; then
      echo "${dir}/.venv/bin/python3"; return 0
    fi
  done
  # Last resort — system python3.
  command -v python3 || true
}

PYTHON_BIN="${WEBHOOK_BENCH_PYTHON:-$(_default_python)}"
COLD_P95_MS="${WEBHOOK_BENCH_COLD_P95_MS:-20000}"
WARM_P95_MS="${WEBHOOK_BENCH_WARM_P95_MS:-1500}"
WORST_MS="${WEBHOOK_BENCH_WORST_MS:-10000}"
COLD_ITERS="${WEBHOOK_BENCH_COLD_ITERS:-3}"
WARM_ITERS="${WEBHOOK_BENCH_WARM_ITERS:-10}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "[test_webhook_bench] FATAL: python not found at ${PYTHON_BIN}" >&2
  echo "[test_webhook_bench] override with WEBHOOK_BENCH_PYTHON=/path/to/python3" >&2
  exit 2
fi

if ! command -v redis-cli >/dev/null 2>&1; then
  echo "[test_webhook_bench] WARN: redis-cli not on PATH; bench will still try to connect." >&2
elif ! redis-cli ping >/dev/null 2>&1; then
  echo "[test_webhook_bench] FATAL: redis (127.0.0.1:6379) not reachable" >&2
  exit 2
fi

OUT_JSON="$(mktemp -t webhook-bench-XXXXXX.json)"
LOG_FILE="$(mktemp -t webhook-bench-XXXXXX.log)"
trap 'rm -f "${OUT_JSON}" "${LOG_FILE}"' EXIT

echo "[test_webhook_bench] running bench (cold=${COLD_ITERS}, warm=${WARM_ITERS})..."
echo "[test_webhook_bench]   thresholds: cold-p95<${COLD_P95_MS}ms warm-p95<${WARM_P95_MS}ms worst-section<${WORST_MS}ms"

set +e
PYTHONPATH="${REPO_ROOT}" "${PYTHON_BIN}" "${REPO_ROOT}/scripts/webhook-bench.py" \
  --cold-iterations "${COLD_ITERS}" \
  --warm-iterations "${WARM_ITERS}" \
  --json-out "${OUT_JSON}" \
  >/dev/null 2>"${LOG_FILE}"
RC=$?
set -e

if [[ ${RC} -ne 0 ]]; then
  echo "[test_webhook_bench] FATAL: bench exited rc=${RC}" >&2
  echo "[test_webhook_bench] --- stderr tail ---" >&2
  tail -20 "${LOG_FILE}" >&2 || true
  exit 2
fi

if [[ ! -s "${OUT_JSON}" ]]; then
  echo "[test_webhook_bench] FATAL: bench produced no JSON output" >&2
  exit 2
fi

# Parse + assert via inline python (no jq dep).
# `set -e` is on; do not chain via $? since the heredoc-driven command's
# exit code is the final exit of the script when run as the last command.
set +e
PYTHONPATH="${REPO_ROOT}" "${PYTHON_BIN}" - "${OUT_JSON}" "${COLD_P95_MS}" "${WARM_P95_MS}" "${WORST_MS}" <<'PYEOF'
import json
import sys

path, cold_max, warm_max, worst_max = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
report = json.loads(open(path).read())

failures = []
cold = report.get("cold") or {}
warm = report.get("warm") or {}

cold_p95 = (cold.get("wall_ms") or {}).get("p95", 0)
warm_p95 = (warm.get("wall_ms") or {}).get("p95", 0)

print(f"[assert] cold-p95 wall = {cold_p95}ms  (limit {cold_max}ms)")
print(f"[assert] warm-p95 wall = {warm_p95}ms  (limit {warm_max}ms)")

if cold_p95 >= cold_max:
    failures.append(f"cold-p95 wall {cold_p95}ms >= {cold_max}ms")
if warm_p95 >= warm_max:
    failures.append(f"warm-p95 wall {warm_p95}ms >= {warm_max}ms")

# n_ok must be > 0 in each window we ran — otherwise something is silently wrong.
if cold and cold.get("n_ok", 0) == 0:
    failures.append("cold path: zero successful runs")
if warm and warm.get("n_ok", 0) == 0:
    failures.append("warm path: zero successful runs")

# No single section may exceed worst_max in either window.
for label in ("cold", "warm"):
    section_worst = (report.get(label) or {}).get("section_worst_ms") or {}
    for sec, ms in section_worst.items():
        if ms >= worst_max:
            failures.append(f"{label} section {sec} = {ms}ms >= {worst_max}ms")
        else:
            # only print the loud ones
            if ms >= worst_max // 2:
                print(f"[assert] {label} section {sec} = {ms}ms (warn: >50% of {worst_max}ms budget)")

if failures:
    print("", file=sys.stderr)
    print("FAIL — webhook performance regression detected:", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    sys.exit(1)

print("PASS — all webhook perf budgets within limits.")
PYEOF
ASSERT_RC=$?
exit "${ASSERT_RC}"
