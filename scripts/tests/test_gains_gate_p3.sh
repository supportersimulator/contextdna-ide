#!/usr/bin/env bash
# test_gains_gate_p3.sh — verify Race P3 fixes to scripts/gains-gate.sh
#
# Two fixes under test:
#   1. Banner is built dynamically from $TOTAL_CHECKS_EXPECTED (no hardcode);
#      the rendered banner string contains a count that matches the number of
#      numbered check blocks in the script.
#   2. Check 12 (V12 action registry coverage) flips to WARNING when the
#      Python helper hits an exception (e.g. zero-byte cache → JSON parse
#      error). Previously it fell through to PASS — a ZSF violation.
#
# Strategy: extract the relevant block(s) from gains-gate.sh and run them in
# isolation with a stubbed `check` so we can inspect the result without bringing
# up Redis / MLX / surgeons.
#
# Exit 0 = all subtests pass. Exit 1 = any subtest failed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GATE="$REPO_ROOT/scripts/gains-gate.sh"

if [[ ! -f "$GATE" ]]; then
    echo "FATAL: gains-gate.sh not found at $GATE"
    exit 1
fi

PASS=0
FAIL=0
FAILED_TESTS=()

assert() {
    local name="$1"
    local actual="$2"
    local expected_pattern="$3"
    if echo "$actual" | grep -qE "$expected_pattern"; then
        echo "  PASS  $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $name"
        echo "        expected pattern: $expected_pattern"
        echo "        actual (first 8 lines):"
        echo "$actual" | head -8 | sed 's/^/          /'
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("$name")
    fi
}

# Run check 12 with the python helper stubbed to emit a controlled V12_RESULT.
# This isolates the bash branch logic from Redis/python availability — what we
# care about is whether the "error:" substring routes to WARNING vs PASS.
#
# We do NOT extract the block from gains-gate.sh; instead we re-implement the
# post-python branch logic in lockstep with the script. Tests 4 & 5 verify the
# script source itself contains the matching guard, so divergence is caught.
#
# Args:
#   $1 = REGISTRY_CACHE fixture path (file must exist for the [[ -f ]] guard,
#        or non-existent to test the missing-cache branch)
#   $2 = simulated python output (e.g. "0|0|error: Expecting value: line 1");
#        ignored if the cache file doesn't exist.
run_v12_branch() {
    local cache_path="$1"
    local stubbed_output="$2"
    bash <<EOF
set -uo pipefail
REGISTRY_CACHE='$cache_path'
check() {
    local name="\$1"
    local sev="\$2"
    local res="\$3"
    local detail="\${4:-}"
    if [[ "\$res" -eq 0 ]]; then
        echo "CHECK_PASS sev=\$sev name=\$name detail=\$detail"
    else
        echo "CHECK_FAIL sev=\$sev name=\$name detail=\$detail"
    fi
}

# Mirror of check 12 (V12 action registry coverage) from scripts/gains-gate.sh.
# Keep these branches in lockstep with the script — Test 5 below asserts the
# script source contains the same "error:" guard.
if [[ -f "\$REGISTRY_CACHE" ]]; then
    V12_RESULT='$stubbed_output'
    V12_DARK=\$(echo "\$V12_RESULT" | cut -d'|' -f1)
    V12_TOTAL=\$(echo "\$V12_RESULT" | cut -d'|' -f2)
    V12_MSG=\$(echo "\$V12_RESULT" | cut -d'|' -f3)
    if [[ "\$V12_MSG" == *"error:"* ]]; then
        check "V12 action registry coverage" "warning" 1 "\${V12_MSG}"
    elif [[ "\${V12_DARK:-0}" -gt 5 ]]; then
        check "V12 action registry coverage" "warning" 1 "\${V12_MSG} (\${V12_DARK}/\${V12_TOTAL} dark actions)"
    else
        check "V12 action registry coverage" "warning" 0 "\${V12_TOTAL} invocations tracked, \${V12_MSG}"
    fi
else
    check "V12 action registry coverage" "warning" 1 "no registry cache (run: ./scripts/action-registry.sh list)"
fi
EOF
}

# ── Test 1: empty cache file → check 12 reports WARNING (not PASS) ──
# This is the bug O5 found: scripts/.action-registry-cache.json was 0 bytes,
# json.load() raised, the python helper printed 'error: ...' in V12_MSG, and
# the bash branch fell through to PASS. After the fix, V12_MSG containing
# 'error:' must flip to WARNING.
echo
echo "Test 1: empty cache file → check 12 reports WARNING (ZSF)"
EMPTY_CACHE=$(mktemp)
: > "$EMPTY_CACHE"  # truncate to 0 bytes
[[ -s "$EMPTY_CACHE" ]] && { echo "FATAL: fixture not empty"; exit 1; }
# Simulated python output for the empty-cache case (matches the catch-all
# branch in the python helper: "0|0|error: <60 chars>").
OUT=$(run_v12_branch "$EMPTY_CACHE" "0|0|error: Expecting value: line 1 column 1 (char 0)")
assert "empty cache → CHECK_FAIL (warning) not CHECK_PASS" "$OUT" "^CHECK_FAIL sev=warning name=V12 action registry coverage"
assert "empty cache → 'error:' substring surfaces in detail" "$OUT" "error:"
rm -f "$EMPTY_CACHE"

# ── Test 2: any python exception → flips to WARNING (broader coverage) ──
# Covers Redis-down, JSON parse, file permission, etc. — anything the python
# helper catches and stringifies as "0|0|error: ...".
echo
echo "Test 2: python exception in V12_MSG → check 12 reports WARNING"
ANY_CACHE=$(mktemp)
printf '[]' > "$ANY_CACHE"  # valid JSON, but python helper still raised for other reason
OUT=$(run_v12_branch "$ANY_CACHE" "0|0|error: redis.exceptions.ConnectionError")
assert "python exception → CHECK_FAIL (warning)" "$OUT" "^CHECK_FAIL sev=warning name=V12 action registry coverage"
assert "python exception → 'error:' substring surfaces in detail" "$OUT" "error:"
rm -f "$ANY_CACHE"

# ── Test 3: clean python output → still PASSes (regression guard) ──
# Make sure the new error-detection branch doesn't accidentally fire on the
# happy path (no "error:" substring → existing PASS path must work).
echo
echo "Test 3: clean python output → CHECK_PASS (regression guard)"
GOOD_CACHE=$(mktemp)
printf '[]' > "$GOOD_CACHE"
OUT=$(run_v12_branch "$GOOD_CACHE" "0|42|10 registered actions")
assert "clean output → CHECK_PASS warning sev" "$OUT" "^CHECK_PASS sev=warning name=V12 action registry coverage"
assert "clean output → invocation count surfaces" "$OUT" "42 invocations tracked"
rm -f "$GOOD_CACHE"

# ── Test 3b: missing cache file → 'no registry cache' branch still works ──
echo
echo "Test 3b: missing cache file → still WARNING (existing branch intact)"
MISSING="/tmp/nonexistent-cache-$$.json"
[[ -f "$MISSING" ]] && rm -f "$MISSING"
OUT=$(run_v12_branch "$MISSING" "ignored")
assert "missing cache → CHECK_FAIL (warning)" "$OUT" "^CHECK_FAIL sev=warning name=V12 action registry coverage"
assert "missing cache → 'no registry cache' detail" "$OUT" "no registry cache"

# ── Test 4: rendered banner contains correct count (matches numbered headers) ──
# We don't run the full gains-gate.sh (it brings up Redis/MLX/surgeon probes,
# 30s+). Instead we extract the head of the script up to (and including) the
# banner echo, run that as bash, and check the rendered output.
echo
echo "Test 4: rendered banner reflects dynamic check count"
EXPECTED_COUNT=$(grep -cE '^# [0-9]+\. ' "$GATE")
[[ "$EXPECTED_COUNT" -lt 1 ]] && { echo "FATAL: could not count check blocks"; exit 1; }
HEAD_SLICE=$(awk '
    /^set -uo pipefail/ { go=1 }
    go { print }
    /Post-Phase Verification.*critical checks/ { exit }
' "$GATE")
BANNER_OUT=$(SCRIPT_DIR="$REPO_ROOT/scripts" REPO_DIR="$REPO_ROOT" bash -c "$HEAD_SLICE" 2>&1 | sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g')
assert "banner contains expected count ($EXPECTED_COUNT)" "$BANNER_OUT" "$EXPECTED_COUNT critical checks"
assert "banner has GAINS GATE prefix" "$BANNER_OUT" "GAINS GATE — Post-Phase Verification"

# ── Test 5: source code uses $TOTAL_CHECKS_EXPECTED (not a hardcoded number) ──
# Lock in the dynamic-count approach so a future edit doesn't silently
# reintroduce a hardcode.
echo
echo "Test 5: banner source uses TOTAL_CHECKS_EXPECTED placeholder"
BANNER_SRC=$(grep -E "echo .*GAINS GATE.*Post-Phase" "$GATE" | head -1)
assert "banner source references TOTAL_CHECKS_EXPECTED" "$BANNER_SRC" "TOTAL_CHECKS_EXPECTED"
if echo "$BANNER_SRC" | grep -q "12 critical checks"; then
    echo "  FAIL  banner source must not hardcode '12 critical checks'"
    FAIL=$((FAIL + 1))
    FAILED_TESTS+=("no hardcoded 12")
else
    echo "  PASS  banner source has no hardcoded '12 critical checks'"
    PASS=$((PASS + 1))
fi

# ── Test 6: gains-gate.sh source contains the V12 'error:' guard ──
# Our run_v12_branch helper re-implements the branch logic in lockstep — so
# we must verify the script source actually has the same guard, otherwise
# tests pass while the real script regresses. (Caught a tester pitfall.)
echo
echo "Test 6: gains-gate.sh contains V12 'error:' WARNING guard"
if grep -qE 'V12_MSG.*==.*\*"error:"\*' "$GATE"; then
    echo "  PASS  gains-gate.sh has V12_MSG error: substring guard"
    PASS=$((PASS + 1))
else
    echo "  FAIL  gains-gate.sh missing V12_MSG error: substring guard"
    FAIL=$((FAIL + 1))
    FAILED_TESTS+=("V12 error guard in source")
fi

# ── Summary ──
echo
echo "──────────────────────────────────────────"
echo "Results: $PASS passed, $FAIL failed (of $((PASS + FAIL)))"
if [[ $FAIL -gt 0 ]]; then
    echo "Failed tests:"
    for t in "${FAILED_TESTS[@]}"; do
        echo "  - $t"
    done
    exit 1
fi
echo "All Race P3 gains-gate fix tests passed."
exit 0
