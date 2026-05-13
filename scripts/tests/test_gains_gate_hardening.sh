#!/usr/bin/env bash
# test_gains_gate_hardening.sh — verify Race M4 hardening of gains-gate.sh
#
# Tests the two new HARD checks added to gains-gate.sh:
#   - 3s probe (Cardiologist + Neurologist reachability)
#   - import-smoke-gate (hot-path module import sanity)
# Plus the new --soft flag that downgrades 3s probe to warning.
#
# Strategy: stub out commands via PATH manipulation so we can simulate
# pass/fail without depending on live surgeons or modules. We extract
# the two new check blocks from gains-gate.sh and run them in isolation
# so unrelated infra failures (Redis/MLX/etc.) don't pollute the outcome.
#
# Exit 0 = all subtests pass. Exit 1 = any subtest failed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GATE="$REPO_ROOT/scripts/gains-gate.sh"
SMOKE="$REPO_ROOT/scripts/import-smoke-gate.sh"

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
        echo "        actual (first 5 lines):"
        echo "$actual" | head -5 | sed 's/^/          /'
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("$name")
    fi
}

# ── Helper: extract a single check block from gains-gate.sh by header pattern ──
# We isolate just the new check logic by sourcing only the helpers + one block.
extract_check_block() {
    local marker="$1"  # e.g. "# 14. 3-Surgeon probe"
    awk -v m="$marker" '
        BEGIN { in_block=0 }
        $0 ~ m { in_block=1 }
        in_block && /^# 1[5-9]\./ && $0 !~ m { exit }
        in_block && /^# ── Results/ { exit }
        in_block { print }
    ' "$GATE"
}

# Minimal harness: runs check blocks with a stubbed `check` function so we can
# inspect the result without running the full 15-check gate (which depends on
# Redis/MLX/etc. that aren't up in test env).
run_block_with_stubs() {
    local block="$1"      # gains-gate check block source
    local stub_path="$2"  # PATH prefix containing stubs
    local soft="${3:-false}"
    PATH="$stub_path:$PATH" bash -c "
        set -uo pipefail
        SCRIPT_DIR='$REPO_ROOT/scripts'
        REPO_DIR='$REPO_ROOT'
        SOFT_MODE=$soft
        # Stub check() to print the args we care about
        check() {
            local name=\"\$1\"
            local sev=\"\$2\"
            local res=\"\$3\"
            local detail=\"\${4:-}\"
            if [[ \"\$res\" -eq 0 ]]; then
                echo \"CHECK_PASS sev=\$sev name=\$name detail=\$detail\"
            else
                echo \"CHECK_FAIL sev=\$sev name=\$name detail=\$detail\"
            fi
        }
        $block
    " 2>&1
}

# ── Test 1: 3s probe pass → gate proceeds (CHECK_PASS, sev=critical) ──
echo
echo "Test 1: 3s probe pass → gate proceeds"
STUB_DIR=$(mktemp -d)
cat > "$STUB_DIR/3s" <<'EOF'
#!/bin/bash
# Stub: simulates healthy 3s probe
echo "Probing surgeons..."
echo ""
echo "  Cardiologist: OK (123ms)"
echo "  Neurologist: OK (456ms)"
echo ""
echo "All surgeons operational."
exit 0
EOF
chmod +x "$STUB_DIR/3s"
PROBE_BLOCK=$(extract_check_block "# 14. 3-Surgeon probe")
OUT=$(run_block_with_stubs "$PROBE_BLOCK" "$STUB_DIR" false)
assert "3s probe pass emits CHECK_PASS" "$OUT" "^CHECK_PASS sev=critical name=3-Surgeon probe"
assert "3s probe pass surfaces latency detail" "$OUT" "Cardiologist: OK.*Neurologist: OK"
rm -rf "$STUB_DIR"

# ── Test 2: 3s probe fail → gate fails with clear error ──
echo
echo "Test 2: 3s probe fail → gate fails (critical) with clear error"
STUB_DIR=$(mktemp -d)
cat > "$STUB_DIR/3s" <<'EOF'
#!/bin/bash
echo "Probing surgeons..."
echo "  Cardiologist: FAIL (timeout after 5000ms)"
echo "  Neurologist: OK (200ms)"
echo "ERROR: 1 surgeon down"
exit 1
EOF
chmod +x "$STUB_DIR/3s"
OUT=$(run_block_with_stubs "$PROBE_BLOCK" "$STUB_DIR" false)
assert "3s probe fail emits CHECK_FAIL critical" "$OUT" "^CHECK_FAIL sev=critical name=3-Surgeon probe"
assert "3s probe fail surfaces failed surgeon" "$OUT" "FAIL|timeout|ERROR"
rm -rf "$STUB_DIR"

# ── Test 3: --soft flag downgrades 3s probe failure to warning ──
echo
echo "Test 3: --soft flag downgrades 3s probe to warning"
STUB_DIR=$(mktemp -d)
cat > "$STUB_DIR/3s" <<'EOF'
#!/bin/bash
echo "Cardiologist: FAIL (unreachable)"
exit 1
EOF
chmod +x "$STUB_DIR/3s"
OUT=$(run_block_with_stubs "$PROBE_BLOCK" "$STUB_DIR" true)
assert "soft mode + 3s fail → warning not critical" "$OUT" "^CHECK_FAIL sev=warning name=3-Surgeon probe"
assert "soft mode prefix surfaces in detail" "$OUT" "soft-mode:"
rm -rf "$STUB_DIR"

# ── Test 4: --soft flag with 3s probe passing → still passes (sev=warning) ──
echo
echo "Test 4: --soft flag + healthy 3s probe → CHECK_PASS sev=warning"
STUB_DIR=$(mktemp -d)
cat > "$STUB_DIR/3s" <<'EOF'
#!/bin/bash
echo "Cardiologist: OK (100ms)"
echo "Neurologist: OK (200ms)"
exit 0
EOF
chmod +x "$STUB_DIR/3s"
OUT=$(run_block_with_stubs "$PROBE_BLOCK" "$STUB_DIR" true)
assert "soft mode + 3s pass → severity=warning on PASS line" "$OUT" "^CHECK_PASS sev=warning name=3-Surgeon probe"
rm -rf "$STUB_DIR"

# ── Test 5: import-smoke pass → gate proceeds ──
echo
echo "Test 5: import-smoke pass → gate proceeds"
SMOKE_BLOCK=$(extract_check_block "# 15. Import-smoke")
# Use a temp script that simulates import-smoke success; place at expected path.
TMP_SMOKE=$(mktemp)
cat > "$TMP_SMOKE" <<'EOF'
#!/bin/bash
echo "OK   memory.agent_service"
echo "OK   memory.anticipation_engine"
echo ""
echo "import-smoke: 5/5 clean"
exit 0
EOF
chmod +x "$TMP_SMOKE"
# Patch the SMOKE_BLOCK to use our temp script
PATCHED_BLOCK="${SMOKE_BLOCK//\$SCRIPT_DIR\/import-smoke-gate.sh/$TMP_SMOKE}"
OUT=$(run_block_with_stubs "$PATCHED_BLOCK" "$(mktemp -d)" false)
assert "import-smoke pass emits CHECK_PASS" "$OUT" "^CHECK_PASS sev=critical name=Import-smoke"
assert "import-smoke pass surfaces summary" "$OUT" "5/5 clean"
rm -f "$TMP_SMOKE"

# ── Test 6: import-smoke fail → gate fails with module name ──
echo
echo "Test 6: import-smoke fail → gate fails with module name"
TMP_SMOKE=$(mktemp)
cat > "$TMP_SMOKE" <<'EOF'
#!/bin/bash
echo "OK   memory.llm_priority_queue"
echo "FAIL memory.anticipation_engine -- NameError: name 'List' is not defined"
echo ""
echo "import-smoke: 1/2 clean"
echo "BLOCKED: memory.anticipation_engine"
exit 1
EOF
chmod +x "$TMP_SMOKE"
PATCHED_BLOCK="${SMOKE_BLOCK//\$SCRIPT_DIR\/import-smoke-gate.sh/$TMP_SMOKE}"
OUT=$(run_block_with_stubs "$PATCHED_BLOCK" "$(mktemp -d)" false)
assert "import-smoke fail emits CHECK_FAIL critical" "$OUT" "^CHECK_FAIL sev=critical name=Import-smoke"
assert "import-smoke fail surfaces module name" "$OUT" "memory.anticipation_engine"
rm -f "$TMP_SMOKE"

# ── Test 7: --soft flag in usage doc + banner uses dynamic check count ──
# Race P3 (2026-04-24): banner is now built from $TOTAL_CHECKS_EXPECTED, derived
# from numbered check headers in the script. We assert the placeholder is in the
# echo line (not a hardcoded number) so adding/removing a check stays truthful.
echo
echo "Test 7: gains-gate.sh banner uses dynamic check count"
HEADER=$(grep -E "^echo .*GAINS GATE" "$GATE" | head -1)
assert "banner uses TOTAL_CHECKS_EXPECTED placeholder" "$HEADER" "\\\$\\{?TOTAL_CHECKS_EXPECTED\\}? critical checks"

USAGE_LINE=$(grep -E "^# Usage:" "$GATE" | head -1)
assert "usage line documents --soft flag" "$USAGE_LINE" "\\-\\-soft"

# ── Test 8: SOFT_MODE variable is parsed from --soft arg ──
echo
echo "Test 8: --soft argument parsed into SOFT_MODE"
PARSE_LINE=$(grep -E 'SOFT_MODE=true' "$GATE" | head -1)
assert "--soft sets SOFT_MODE=true" "$PARSE_LINE" 'arg.*--soft.*SOFT_MODE=true'

# ── Test 9: import-smoke-gate.sh exists and is executable ──
echo
echo "Test 9: import-smoke-gate.sh exists and is executable"
if [[ -x "$SMOKE" ]]; then
    echo "  PASS  import-smoke-gate.sh executable at $SMOKE"
    PASS=$((PASS + 1))
else
    echo "  FAIL  import-smoke-gate.sh missing or not executable"
    FAIL=$((FAIL + 1))
    FAILED_TESTS+=("import-smoke-gate exists")
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
echo "All gains-gate hardening tests passed."
exit 0
