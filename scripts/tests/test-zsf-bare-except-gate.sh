#!/bin/bash
# ============================================================================
# Test: ZSF Bare-Except Gate (scripts/check-zsf-bare-except.sh)
# ============================================================================
# Verifies:
#   1. Baseline (no fixture)         → gate exits 0
#   2. Synthetic `except: pass` lands → gate exits 1, names the file
#   3. Synthetic `except Exception: pass` lands → gate exits 1
#   4. Synthetic with `# zsf-allow`  → gate exits 0
#   5. Fixture removed               → gate exits 0
#
# This is a smoke test, not a tox-managed pytest. Intent: a single bash file
# that any operator can run to confirm the gate works end to end.
#
# Exit: 0 = all assertions pass, 1 = any assertion failed
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
GATE="$REPO_DIR/scripts/check-zsf-bare-except.sh"

if [[ ! -x "$GATE" ]]; then
    echo "FAIL: gate script not executable: $GATE"
    exit 1
fi

FIXTURE_DIR="$SCRIPT_DIR/zsf-bare-except-fixtures"
FIXTURE="$FIXTURE_DIR/synthetic_violation.py"
mkdir -p "$FIXTURE_DIR"

cleanup() {
    rm -f "$FIXTURE"
}
trap cleanup EXIT

PASS=0
FAIL=0

assert_pass() {
    local name="$1"
    "$GATE" >/dev/null 2>&1
    local rc=$?
    if [[ "$rc" -eq 0 ]]; then
        echo "  PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name (gate rc=$rc, expected 0)"
        FAIL=$((FAIL + 1))
    fi
}

assert_fail() {
    local name="$1"
    local needle="${2:-}"
    local out
    out="$("$GATE" 2>&1)"
    local rc=$?
    if [[ "$rc" -eq 1 ]] && [[ -z "$needle" || "$out" == *"$needle"* ]]; then
        echo "  PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name (rc=$rc, expected 1; needle='$needle')"
        echo "        output: ${out:0:200}"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== ZSF Bare-Except Gate Tests ==="

# 1. Baseline
cleanup
assert_pass "baseline (no fixture) → exit 0"

# 2. except: pass
cleanup
cat > "$FIXTURE" <<'PY'
def bad():
    try:
        return 1 / 0
    except:
        pass
PY
assert_fail "synthetic 'except: pass' → exit 1" "synthetic_violation.py:<bare>"

# 3. except Exception: pass
cleanup
cat > "$FIXTURE" <<'PY'
def bad():
    try:
        return 1 / 0
    except Exception:
        pass
PY
assert_fail "synthetic 'except Exception: pass' → exit 1" "synthetic_violation.py:Exception"

# 4. zsf-allow tag clears the gate
cleanup
cat > "$FIXTURE" <<'PY'
def good():
    try:
        return 1 / 0
    except Exception:  # zsf-allow: audited fixture
        pass
PY
assert_pass "synthetic with '# zsf-allow' → exit 0"

# 4b. noqa: BLE001 also clears (legacy ruff allowlist marker)
cleanup
cat > "$FIXTURE" <<'PY'
def good():
    try:
        return 1 / 0
    except Exception:  # noqa: BLE001
        pass
PY
assert_pass "synthetic with '# noqa: BLE001' → exit 0"

# 5. fixture removed → clean
cleanup
assert_pass "fixture removed → exit 0"

echo "=== Results: ${PASS} pass, ${FAIL} fail ==="
[[ "$FAIL" -gt 0 ]] && exit 1
exit 0
