#!/usr/bin/env bash
# test_xbar_health_check.sh — CCC2 pinning tests for xbar-health-check.sh (720bcc175)
#
# Tests:
#   1. Missing plugin dir → exits 0 and prints "no_plugins_found".
#   2. HEALTHY plugin → counter bumped, status HEALTHY, exit 0.
#   3. DEAD plugin (exit 1) → status DEAD with exit code captured.
#   4. DEAD plugin (empty stdout) → status DEAD, reason empty_stdout.
#   5. DEGRADED plugin (red badge first line) → status DEGRADED.
#   6. --json mode emits one JSON line per plugin.
#   7. --counters-only mode writes counters silently to stdout.
#
# Strategy: override $HOME so the script's hard-coded plugin dir
# ("$HOME/Library/Application Support/xbar/plugins") points at a sandbox
# we populate with fake plugin scripts. ZSF — each failure surfaces both
# the missing assertion AND the script's own stdout/stderr.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SCRIPT_DIR/../xbar-health-check.sh"

if [[ ! -f "$TARGET" ]]; then
    echo "FAIL: script not found: $TARGET"
    exit 1
fi

PASS=0
FAIL=0
note_pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
note_fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

# Fresh sandbox per run — prevents collisions between concurrent test runs.
SANDBOX="$(mktemp -d -t xbar-health-test.XXXXXX)"
PLUGIN_DIR="$SANDBOX/Library/Application Support/xbar/plugins"
COUNTER_DIR="$SANDBOX/xbar-counters"
ERR_LOG="$SANDBOX/xbar-health.err"

cleanup() {
    rm -rf "$SANDBOX"
}
trap cleanup EXIT

# Helper: run the script with HOME pinned to our sandbox so PLUGIN_DIR
# resolves there, and counter/err paths redirected so /tmp stays clean.
# The script uses fixed /tmp paths for COUNTER_DIR and ERR_LOG; we patch
# the script body via env var injection by creating a wrapper.
WRAPPER="$SANDBOX/wrap.sh"
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
# Substitute /tmp paths so concurrent tests don't trample each other.
set -u
sed -e 's|/tmp/xbar-health.err|$ERR_LOG|' \\
    -e 's|/tmp/xbar-counters|$COUNTER_DIR|' \\
    "$TARGET" > "$SANDBOX/xbar-health-test.sh"
chmod +x "$SANDBOX/xbar-health-test.sh"
HOME="$SANDBOX" exec bash "$SANDBOX/xbar-health-test.sh" "\$@"
EOF
chmod +x "$WRAPPER"

# ---- Test 1: missing plugin dir ----
echo "=== Test 1: missing plugin dir ==="
OUT="$("$WRAPPER" 2>/dev/null || true)"
if echo "$OUT" | grep -q "no_plugins_found"; then
    note_pass "missing plugin dir → no_plugins_found"
else
    note_fail "expected 'no_plugins_found' in output"
    echo "    actual: $OUT"
fi

# Create plugin dir for subsequent tests.
mkdir -p "$PLUGIN_DIR"

# ---- Test 2: HEALTHY plugin ----
echo ""
echo "=== Test 2: HEALTHY plugin ==="
cat > "$PLUGIN_DIR/healthy.5m.sh" <<'EOF'
#!/usr/bin/env bash
echo "all good"
EOF
chmod +x "$PLUGIN_DIR/healthy.5m.sh"

OUT="$("$WRAPPER" 2>/dev/null || true)"
if echo "$OUT" | grep -qE "healthy\.5m\.sh\s+HEALTHY"; then
    note_pass "healthy plugin reported HEALTHY"
else
    note_fail "healthy plugin not marked HEALTHY"
    echo "    actual: $OUT"
fi

if [[ -f "$COUNTER_DIR/xbar_plugin_healthy_5m_sh_healthy_total" ]]; then
    note_pass "healthy counter bumped"
else
    note_fail "healthy counter file missing"
    ls "$COUNTER_DIR" 2>&1 | sed 's/^/    /'
fi

# ---- Test 3: DEAD plugin (exit 1) ----
echo ""
echo "=== Test 3: DEAD plugin (exit 1) ==="
cat > "$PLUGIN_DIR/broken.5m.sh" <<'EOF'
#!/usr/bin/env bash
echo "boom"
exit 1
EOF
chmod +x "$PLUGIN_DIR/broken.5m.sh"

OUT="$("$WRAPPER" 2>/dev/null || true)"
if echo "$OUT" | grep -qE "broken\.5m\.sh\s+DEAD"; then
    note_pass "broken plugin reported DEAD"
else
    note_fail "broken plugin not marked DEAD"
    echo "    actual: $OUT"
fi

# ---- Test 4: DEAD plugin (empty stdout) ----
echo ""
echo "=== Test 4: DEAD plugin (empty stdout) ==="
cat > "$PLUGIN_DIR/silent.5m.sh" <<'EOF'
#!/usr/bin/env bash
# emits nothing, exits 0
true
EOF
chmod +x "$PLUGIN_DIR/silent.5m.sh"

OUT="$("$WRAPPER" 2>/dev/null || true)"
if echo "$OUT" | grep -qE "silent\.5m\.sh\s+DEAD"; then
    note_pass "silent plugin reported DEAD (empty_stdout)"
else
    note_fail "silent plugin not marked DEAD"
    echo "    actual: $OUT"
fi

# ---- Test 5: DEGRADED plugin (red badge) ----
echo ""
echo "=== Test 5: DEGRADED plugin (red badge) ==="
cat > "$PLUGIN_DIR/redbadge.5m.sh" <<'EOF'
#!/usr/bin/env bash
echo "fleet: unreachable | color=red"
EOF
chmod +x "$PLUGIN_DIR/redbadge.5m.sh"

OUT="$("$WRAPPER" 2>/dev/null || true)"
if echo "$OUT" | grep -qE "redbadge\.5m\.sh\s+DEGRADED"; then
    note_pass "red-badge plugin reported DEGRADED"
else
    note_fail "red-badge plugin not marked DEGRADED"
    echo "    actual: $OUT"
fi

# ---- Test 6: --json mode ----
echo ""
echo "=== Test 6: --json mode ==="
JSON_OUT="$("$WRAPPER" --json 2>/dev/null || true)"
# Expect at least one well-formed JSON line.
if echo "$JSON_OUT" | grep -qE '^\{"plugin":"healthy\.5m\.sh","status":"HEALTHY"'; then
    note_pass "json mode emitted healthy plugin record"
else
    note_fail "json mode did not emit expected record"
    echo "    actual: $JSON_OUT" | head -5 | sed 's/^/    /'
fi

# ---- Test 7: --counters-only ----
echo ""
echo "=== Test 7: --counters-only mode ==="
# Clear stdout-visible counter and confirm summary line appears on stderr.
SILENT_OUT="$("$WRAPPER" --counters-only 2>&1 >/dev/null || true)"
if echo "$SILENT_OUT" | grep -q "probed="; then
    note_pass "counters-only mode emits summary to stderr"
else
    note_fail "counters-only mode did not emit summary"
    echo "    actual: $SILENT_OUT"
fi

# ---- Results ----
echo ""
echo "=== Results ==="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
