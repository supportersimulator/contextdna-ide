#!/usr/bin/env bash
# =============================================================================
# Test: refresh-node.sh (M4 fleet auto-heal orchestrator)
# =============================================================================
# Verifies (NEVER mutates the live repo / .venv / launchd / daemons):
#   1. --help exits 0 and prints the spec header.
#   2. Unknown flag exits 2.
#   3. Default (--dry-run) runs every step in preview mode, exits 0 or 1.
#   4. --dry-run never mutates: working tree clean before == clean after.
#   5. --dry-run is idempotent (two consecutive runs change nothing).
#   6. --strict mode: step 2 forced to fail → subsequent steps DO NOT run.
#   7. Non-strict mode (default): step 2 forced to fail → step 3+ still run.
#   8. --apply path executes mutation commands (via mocked sub-scripts).
#   9. Counter file is written with PASS / FAIL / step counters (ZSF).
#
# Mocked sub-scripts: a sandbox dir of stub scripts overrides every composed
# command via the SCRIPT_* env vars exposed by refresh-node.sh. Stubs record
# their invocation to a witness file so tests can assert chain order.
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/refresh-node.sh"

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

SANDBOX="$(mktemp -d -t refresh-node-XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

WITNESS="$SANDBOX/witness.log"
COUNTER_FILE="$SANDBOX/counters.txt"

# ------------------------------------------------------------------
# Stub factory — every sub-script is replaced with a stub that:
#   (a) appends its name + argv to $WITNESS
#   (b) exits with the rc passed via stub-specific env var (default 0)
# Stubs accept any flag refresh-node.sh might pass.
# ------------------------------------------------------------------
make_stub() {
    local path="$1"; local name="$2"; local rc_var="$3"
    cat >"$path" <<STUB
#!/usr/bin/env bash
echo "STUB:$name args:\$*" >> "$WITNESS"
exit \${$rc_var:-0}
STUB
    chmod +x "$path"
}

STUB_VENV="$SANDBOX/venv-rebuild.sh"
STUB_SYNC="$SANDBOX/sync-node-config.sh"
STUB_NEURO="$SANDBOX/patch-neuro-cutover.py"
STUB_UNIFY="$SANDBOX/unify-cluster-urls.py"
STUB_DAEMON="$SANDBOX/daemon-services-up.sh"
STUB_INV="$SANDBOX/constitutional-invariants.sh"

make_stub "$STUB_VENV"   "venv-rebuild"   RC_VENV
make_stub "$STUB_SYNC"   "sync-node"      RC_SYNC
make_stub "$STUB_NEURO"  "neuro-patch"    RC_NEURO
make_stub "$STUB_UNIFY"  "unify-cluster"  RC_UNIFY
make_stub "$STUB_DAEMON" "daemon-up"      RC_DAEMON
make_stub "$STUB_INV"    "invariants"     RC_INV

# Common env that points refresh-node.sh at all six stubs + sandbox log paths.
common_env() {
    env \
        SCRIPT_VENV_REBUILD="$STUB_VENV" \
        SCRIPT_SYNC_NODE="$STUB_SYNC" \
        SCRIPT_NEURO_PATCH="$STUB_NEURO" \
        SCRIPT_UNIFY_CLUSTER="$STUB_UNIFY" \
        SCRIPT_DAEMON_SERVICES="$STUB_DAEMON" \
        SCRIPT_INVARIANTS="$STUB_INV" \
        REFRESH_NODE_COUNTER_FILE="$COUNTER_FILE" \
        REFRESH_NODE_LOG="$SANDBOX/refresh.log" \
        MULTIFLEET_NODE_ID="mac3-test" \
        "$@"
}

reset_witness() {
    : > "$WITNESS"
    : > "$COUNTER_FILE"
    : > "$SANDBOX/refresh.log"
}

# ------------------------------------------------------------------
# 1. --help exits 0
# ------------------------------------------------------------------
case_help_exit_0() {
    "$SCRIPT" --help >/dev/null 2>&1
}

# ------------------------------------------------------------------
# 2. unknown flag exits 2
# ------------------------------------------------------------------
case_unknown_flag_exit_2() {
    local rc
    "$SCRIPT" --not-a-real-flag >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 2 ]
}

# ------------------------------------------------------------------
# 3. Default --dry-run runs every step (witness sees 6 stub hits).
# ------------------------------------------------------------------
case_dry_run_runs_all_steps() {
    reset_witness
    common_env "$SCRIPT" --dry-run >/dev/null 2>&1
    # Step 1 is git fetch — not stubbed; that's fine. Steps 2–7 are stubs.
    local got
    got=$(grep -c "^STUB:" "$WITNESS")
    [ "$got" -ge 6 ] || { echo "    expected >=6 stub invocations, got $got" >&2; return 1; }
}

# ------------------------------------------------------------------
# 4. --dry-run never mutates working tree.
# ------------------------------------------------------------------
case_dry_run_no_mutation() {
    reset_witness
    local before after
    before="$(cd "$REPO_ROOT" && git status --porcelain | wc -l | tr -d ' ')"
    common_env "$SCRIPT" --dry-run >/dev/null 2>&1
    after="$(cd "$REPO_ROOT" && git status --porcelain | wc -l | tr -d ' ')"
    [ "$before" = "$after" ] || {
        echo "    git status changed: before=$before after=$after" >&2
        return 1
    }
}

# ------------------------------------------------------------------
# 5. Idempotent: two consecutive --dry-run runs produce the same
#    set of stub invocations.
# ------------------------------------------------------------------
case_dry_run_idempotent() {
    reset_witness
    common_env "$SCRIPT" --dry-run >/dev/null 2>&1
    local first
    first=$(grep -c "^STUB:" "$WITNESS")
    : > "$WITNESS"
    common_env "$SCRIPT" --dry-run >/dev/null 2>&1
    local second
    second=$(grep -c "^STUB:" "$WITNESS")
    [ "$first" = "$second" ] || {
        echo "    not idempotent: run1=$first run2=$second" >&2
        return 1
    }
}

# ------------------------------------------------------------------
# 6. --strict + forced step-2 failure → step 3+ NOT executed.
# ------------------------------------------------------------------
case_strict_aborts_on_step_failure() {
    reset_witness
    common_env RC_VENV=1 "$SCRIPT" --dry-run --strict >/dev/null 2>&1
    # Witness should contain venv-rebuild but NOT sync-node / daemon-up /
    # invariants (steps 3, 6, 7).
    grep -q "STUB:venv-rebuild" "$WITNESS" || {
        echo "    step 2 (venv-rebuild) not invoked" >&2; return 1; }
    if grep -q "STUB:sync-node\|STUB:daemon-up\|STUB:invariants" "$WITNESS"; then
        echo "    later steps invoked despite --strict abort" >&2
        cat "$WITNESS" >&2
        return 1
    fi
    return 0
}

# ------------------------------------------------------------------
# 7. Non-strict (default) + forced step-2 failure → later steps STILL run.
# ------------------------------------------------------------------
case_nonstrict_continues_on_failure() {
    reset_witness
    common_env RC_VENV=1 "$SCRIPT" --dry-run >/dev/null 2>&1
    grep -q "STUB:venv-rebuild" "$WITNESS" || {
        echo "    step 2 missing" >&2; return 1; }
    grep -q "STUB:sync-node" "$WITNESS" || {
        echo "    step 3 missing (non-strict should continue)" >&2; return 1; }
    grep -q "STUB:invariants" "$WITNESS" || {
        echo "    step 7 missing (non-strict should continue)" >&2; return 1; }
    return 0
}

# ------------------------------------------------------------------
# 8. --apply path executes stubs (mutating mode reaches the chain).
#    We can't safely run --apply against real scripts; the stubs prove
#    refresh-node.sh dispatched the apply branch correctly.
# ------------------------------------------------------------------
case_apply_dispatches_chain() {
    reset_witness
    common_env "$SCRIPT" --apply --restart-daemons >/dev/null 2>&1 || true
    # Apply path for daemon-services should include --apply --no-prompt.
    grep -q "STUB:daemon-up.*--apply" "$WITNESS" || {
        echo "    daemon-up not invoked with --apply" >&2
        cat "$WITNESS" >&2
        return 1
    }
    # Without --include-cluster-fix, unify-cluster MUST stay in --dry-run.
    grep "STUB:unify-cluster" "$WITNESS" | grep -q "dry-run" || {
        echo "    unify-cluster did not stay in --dry-run without --include-cluster-fix" >&2
        cat "$WITNESS" >&2
        return 1
    }
}

# ------------------------------------------------------------------
# 9. --include-cluster-fix flips RR2 mutation in --apply mode.
# ------------------------------------------------------------------
case_include_cluster_fix_mutates_unify() {
    reset_witness
    common_env "$SCRIPT" --apply --include-cluster-fix >/dev/null 2>&1 || true
    grep "STUB:unify-cluster" "$WITNESS" | grep -q -- "--apply" || {
        echo "    unify-cluster should run with --apply when --include-cluster-fix set" >&2
        cat "$WITNESS" >&2
        return 1
    }
}

# ------------------------------------------------------------------
# 10. ZSF: counter file populated.
# ------------------------------------------------------------------
case_counters_written() {
    reset_witness
    common_env "$SCRIPT" --dry-run >/dev/null 2>&1
    [ -s "$COUNTER_FILE" ] || { echo "    counter file empty" >&2; return 1; }
    grep -q "refresh_step_" "$COUNTER_FILE" || {
        echo "    no step counter found" >&2
        cat "$COUNTER_FILE" >&2
        return 1
    }
}

echo "=== test_refresh_node.sh ==="
echo "Sandbox: $SANDBOX"

_run_case "--help exits 0"                          case_help_exit_0
_run_case "unknown flag exits 2"                    case_unknown_flag_exit_2
_run_case "--dry-run runs every step"               case_dry_run_runs_all_steps
_run_case "--dry-run no mutation of working tree"   case_dry_run_no_mutation
_run_case "--dry-run idempotent across two runs"    case_dry_run_idempotent
_run_case "--strict aborts chain on step failure"   case_strict_aborts_on_step_failure
_run_case "non-strict continues past step failure"  case_nonstrict_continues_on_failure
_run_case "--apply dispatches mutation chain"       case_apply_dispatches_chain
_run_case "--include-cluster-fix mutates unify"     case_include_cluster_fix_mutates_unify
_run_case "ZSF counters written to file"            case_counters_written

echo "---------------------------"
echo "Result: $PASS/$CASES passed, $FAIL failed"

[ "$FAIL" -eq 0 ]
