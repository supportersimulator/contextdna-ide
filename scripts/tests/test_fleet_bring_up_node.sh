#!/usr/bin/env bash
# =============================================================================
# Test: scripts/fleet-bring-up-node.sh + scripts/fleet-profile-publish.sh
# =============================================================================
# Covers:
#   1. --help exits 0.
#   2. Unknown arg → exit 2.
#   3. Dry-run never mutates (no KV write, no launchd touch).
#   4. Dry-run produces a summary line per REQUIRED check.
#   5. fleet-profile-publish.sh --dry-run detects + prints profile JSON.
#   6. fleet-profile-publish.sh refuses --apply if NATS unreachable (ZSF).
#   7. _detect_ides import surface still resolves (mirrors 3s).
#   8. ZSF on partial install: missing venv-rebuild handled gracefully.
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BRING_UP="$REPO_ROOT/scripts/fleet-bring-up-node.sh"
PUBLISH="$REPO_ROOT/scripts/fleet-profile-publish.sh"

if [ ! -f "$BRING_UP" ]; then echo "FAIL: $BRING_UP missing" >&2; exit 1; fi
if [ ! -f "$PUBLISH" ];  then echo "FAIL: $PUBLISH missing"  >&2; exit 1; fi

PASS=0; FAIL=0; CASES=0
_case() {
    local name="$1"; shift
    CASES=$((CASES+1))
    if "$@"; then
        echo "  PASS: $name"; PASS=$((PASS+1))
    else
        echo "  FAIL: $name" >&2; FAIL=$((FAIL+1))
    fi
}

# 1. --help exits 0
case_help_bring_up() { bash "$BRING_UP" --help >/dev/null 2>&1; }
case_help_publish()  { bash "$PUBLISH"  --help >/dev/null 2>&1; }

# 2. Unknown arg → exit 2
case_unknown_bring_up() {
    local rc=0
    bash "$BRING_UP" --bogus >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 2 ]
}
case_unknown_publish() {
    local rc=0
    bash "$PUBLISH" --bogus >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 2 ]
}

# 3. Dry-run never mutates: no /tmp/yy2-...apply marker, no NATS conn attempt
#    visible via apply log line.
case_dry_run_no_mutation() {
    local log="/tmp/yy2-fleet-bring-up-testnode.log"
    rm -f "$log"
    # We don't care if it exits 0 or 1 (likely 1 — no daemon on test box);
    # we care that no "--apply: attempting profile publish" line appears.
    bash "$BRING_UP" --dry-run --node testnode >/dev/null 2>&1 || true
    [ -f "$log" ] || { echo "    log not written: $log" >&2; return 1; }
    if grep -q "attempting profile publish" "$log"; then
        echo "    dry-run wrote apply line — leak" >&2
        return 1
    fi
    return 0
}

# 4. Dry-run summary contains the 6 named checks.
case_dry_run_emits_all_checks() {
    local out
    out="$(bash "$BRING_UP" --dry-run --node testnode 2>&1 || true)"
    for chk in vscode_claude_code_extension ide_markers venv_health \
               fleet_daemon_health nats_tcp capability_profile_kv; do
        if ! echo "$out" | grep -q "$chk"; then
            echo "    missing check in summary: $chk" >&2
            return 1
        fi
    done
}

# 5. publish --dry-run prints DETECTED + DRY-RUN lines, never PUBLISHED.
case_publish_dry_run_detects() {
    local out
    out="$(bash "$PUBLISH" --dry-run --node testnode 2>&1 || true)"
    echo "$out" | grep -q "DETECTED" || { echo "    no DETECTED line" >&2; return 1; }
    echo "$out" | grep -q "DRY-RUN"  || { echo "    no DRY-RUN line"  >&2; return 1; }
    if echo "$out" | grep -q "^PUBLISHED"; then
        echo "    dry-run wrote PUBLISHED — leak" >&2; return 1
    fi
}

# 6. publish --apply against unreachable NATS → exit 1, ZSF (no traceback).
case_publish_apply_zsf_on_no_nats() {
    local out rc=0
    out="$(bash "$PUBLISH" --apply --node testnode \
        --nats-url "nats://127.0.0.1:65530" 2>&1)" || rc=$?
    # nats-py may be absent on a fresh box — also acceptable, ZSF still
    # demands a single-line error.
    if echo "$out" | grep -qE "(ERR nats connect:|ERR nats-py missing:|ERR publish_profile_to_kv)"; then
        [ "$rc" -ne 0 ]
    else
        echo "    expected ZSF ERR line, got:" >&2
        echo "$out" | head -5 >&2
        return 1
    fi
}

# 7. _detect_ides import surface resolves.
case_detect_ides_importable() {
    PYTHONPATH="$REPO_ROOT/3-surgeons" python3 - <<'PY' >/dev/null 2>&1
from three_surgeons.cli.main import _detect_ides
res = _detect_ides()
assert isinstance(res, list)
PY
}

# 8. ZSF on missing venv-rebuild — rename it for one run, expect REQ FAIL line.
case_zsf_missing_venv_rebuild() {
    local rebuild="$REPO_ROOT/scripts/venv-rebuild.sh"
    local stash="$REPO_ROOT/scripts/venv-rebuild.sh.yy2bak"
    if [ ! -f "$rebuild" ]; then
        # Test environment already missing it — that's fine, just run.
        local out; out="$(bash "$BRING_UP" --dry-run --node testnode 2>&1 || true)"
        echo "$out" | grep -q "venv_health.*FAIL"
        return $?
    fi
    mv "$rebuild" "$stash" || return 1
    local out rc=0
    out="$(bash "$BRING_UP" --dry-run --node testnode 2>&1)" || rc=$?
    mv "$stash" "$rebuild"
    if echo "$out" | grep -q "venv_health.*FAIL"; then
        return 0
    fi
    echo "    no venv_health FAIL line when rebuild missing" >&2
    return 1
}

echo "── test_fleet_bring_up_node.sh ──"
_case "help_bring_up_exit_0"        case_help_bring_up
_case "help_publish_exit_0"         case_help_publish
_case "unknown_bring_up_exit_2"     case_unknown_bring_up
_case "unknown_publish_exit_2"      case_unknown_publish
_case "dry_run_no_mutation"         case_dry_run_no_mutation
_case "dry_run_emits_all_checks"    case_dry_run_emits_all_checks
_case "publish_dry_run_detects"     case_publish_dry_run_detects
_case "publish_apply_zsf_on_no_nats" case_publish_apply_zsf_on_no_nats
_case "detect_ides_importable"      case_detect_ides_importable
_case "zsf_missing_venv_rebuild"    case_zsf_missing_venv_rebuild

echo ""
echo "── results: $PASS/$CASES pass, $FAIL fail ──"
[ "$FAIL" -eq 0 ]
