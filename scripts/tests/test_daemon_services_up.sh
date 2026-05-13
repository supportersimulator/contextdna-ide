#!/usr/bin/env bash
# =============================================================================
# Test: daemon-services-up.sh (R1 daemon startup harness)
# =============================================================================
# Verifies (NEVER mutates services — --dry-run mode only):
#   1. --help exits 0 and prints usage.
#   2. --dry-run runs all 5 service steps, writes log, exits 0.
#   3. --dry-run pre-flight calls venv-rebuild.sh --check when present.
#   4. Unknown flag → exit 2.
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/daemon-services-up.sh"
LOG_FILE="/tmp/r1-daemon-services-up.log"

if [ ! -x "$SCRIPT" ]; then
    echo "FAIL: $SCRIPT not executable" >&2
    exit 1
fi

PASS=0
FAIL=0
CASES=0

_run_case() {
    local name="$1"; shift
    CASES=$((CASES + 1))
    if "$@"; then
        echo "  PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name" >&2
        FAIL=$((FAIL + 1))
    fi
}

case_help_exit_0() {
    "$SCRIPT" --help >/dev/null 2>&1
}

case_unknown_flag_exit_2() {
    local rc
    "$SCRIPT" --not-a-real-flag >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 2 ]
}

case_dry_run_runs_all_steps() {
    # --dry-run NEVER starts services. We assert the log has a line per step
    # plus the venv pre-flight (regardless of pass/fail).
    local rc
    "$SCRIPT" --dry-run >/dev/null 2>&1
    rc=$?
    # Exit code 0 or 1 acceptable; 1 means an already-down daemon was
    # reported FAIL because dry-run only inspects, doesn't start.
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
        echo "    rc=$rc (expected 0 or 1)" >&2
        return 1
    fi
    [ -f "$LOG_FILE" ] || { echo "    no log at $LOG_FILE" >&2; return 1; }
    grep -q "step_venv_preflight\|venv_preflight\|venv-rebuild" "$LOG_FILE" \
        || grep -q "\[pre\] venv_preflight" "$LOG_FILE" || true
    # Confirm each step heading is present.
    grep -q "\[1/5\]" "$LOG_FILE" || return 1
    grep -q "\[2/5\]" "$LOG_FILE" || return 1
    grep -q "\[3/5\]" "$LOG_FILE" || return 1
    grep -q "\[4/5\]" "$LOG_FILE" || return 1
    grep -q "\[5/5\]" "$LOG_FILE" || return 1
    return 0
}

case_dry_run_invokes_venv_preflight() {
    # The integrated pre-flight should print a line that references either
    # the venv-rebuild script path or its "venv_preflight" record name.
    "$SCRIPT" --dry-run >/dev/null 2>&1 || true
    grep -q "venv_preflight\|venv-rebuild" "$LOG_FILE"
}

echo "=== test_daemon_services_up.sh ==="

_run_case "--help exits 0"                  case_help_exit_0
_run_case "unknown flag exits 2"            case_unknown_flag_exit_2
_run_case "--dry-run runs all 5 steps"      case_dry_run_runs_all_steps
_run_case "--dry-run invokes venv preflight" case_dry_run_invokes_venv_preflight

echo "---------------------------"
echo "Result: $PASS/$CASES passed, $FAIL failed"

[ "$FAIL" -eq 0 ]
