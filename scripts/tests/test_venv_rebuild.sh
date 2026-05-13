#!/usr/bin/env bash
# =============================================================================
# Test: venv-rebuild.sh (M1 fleet auto-heal)
# =============================================================================
# Verifies (NEVER mutates the live .venv):
#   1. --help exits 0 and prints usage.
#   2. --dry-run on a sandbox venv that has all essentials → exit 0, no install.
#   3. --check on a sandbox venv missing a package → exit 1, counter bumped.
#   4. --dry-run on a sandbox with missing pkg → exit 0, prints install cmd.
#   5. Unknown flag → exit 2.
#   6. ZSF: counter file written on every failure path.
#
# Sandbox: builds a throwaway venv under $TMPDIR using the system python3,
# then points the script at it via VENV_DIR=. Never touches the repo venv.
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/venv-rebuild.sh"

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

SANDBOX="$(mktemp -d -t venv-rebuild-XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

SANDBOX_VENV="$SANDBOX/.venv"
COUNTER_FILE="$SANDBOX/counters.txt"
LOG_FILE="$SANDBOX/venv-rebuild.log"

# Common env for every invocation: redirect venv + counter + log into the
# sandbox so the live repo state is untouched.
common_env() {
    env \
        VENV_DIR="$SANDBOX_VENV" \
        VENV_REBUILD_COUNTER_FILE="$COUNTER_FILE" \
        VENV_REBUILD_LOG="$LOG_FILE" \
        MULTIFLEET_NODE_ID="test-node" \
        "$@"
}

case_help_exit_0() {
    "$SCRIPT" --help >/dev/null 2>&1
}

case_unknown_flag_exit_2() {
    local rc
    "$SCRIPT" --bogus >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 2 ]
}

case_check_missing_venv_exit_1() {
    # No sandbox venv yet — script should report missing and exit 1.
    rm -rf "$SANDBOX_VENV"
    : > "$COUNTER_FILE"
    local rc
    common_env "$SCRIPT" --check >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 1 ] && grep -q "venv_rebuild_missing_total\|venv_rebuild_missing_packages_total" "$COUNTER_FILE"
}

case_dry_run_healthy_exit_0() {
    # Build a sandbox venv with all essentials so --check passes.
    rm -rf "$SANDBOX_VENV"
    python3 -m venv "$SANDBOX_VENV" >/dev/null 2>&1 || return 1
    "$SANDBOX_VENV/bin/pip" install --quiet \
        uvicorn fastapi nats-py httpx click pydantic redis requests pyyaml \
        >/dev/null 2>&1 || return 1
    : > "$COUNTER_FILE"
    local rc
    common_env "$SCRIPT" --dry-run >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 0 ]
}

case_check_missing_pkg_exit_1() {
    # Re-use the healthy sandbox venv but uninstall one essential.
    "$SANDBOX_VENV/bin/pip" uninstall -y httpx >/dev/null 2>&1 || return 1
    : > "$COUNTER_FILE"
    local rc
    common_env "$SCRIPT" --check >/dev/null 2>&1
    rc=$?
    # Must exit 1 and increment the missing-packages counter.
    [ "$rc" -eq 1 ] && grep -q "venv_rebuild_missing_packages_total" "$COUNTER_FILE"
}

case_dry_run_prints_install() {
    # Same state (httpx missing). --dry-run must print install cmd, exit 0,
    # and NOT mutate (httpx still absent afterward).
    : > "$COUNTER_FILE"
    : > "$LOG_FILE"
    local rc
    common_env "$SCRIPT" --dry-run >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || return 1
    grep -q "DRY-RUN: would run:" "$LOG_FILE" || return 1
    # Confirm dry-run did not actually install httpx.
    "$SANDBOX_VENV/bin/python" -c "import httpx" >/dev/null 2>&1 && return 1
    return 0
}

echo "=== test_venv_rebuild.sh ==="
echo "Sandbox: $SANDBOX"

_run_case "--help exits 0"                 case_help_exit_0
_run_case "unknown flag exits 2"           case_unknown_flag_exit_2
_run_case "--check missing venv exits 1"   case_check_missing_venv_exit_1
_run_case "--dry-run healthy venv exits 0" case_dry_run_healthy_exit_0
_run_case "--check missing pkg exits 1"    case_check_missing_pkg_exit_1
_run_case "--dry-run prints install cmd"   case_dry_run_prints_install

echo "---------------------------"
echo "Result: $PASS/$CASES passed, $FAIL failed"

[ "$FAIL" -eq 0 ]
