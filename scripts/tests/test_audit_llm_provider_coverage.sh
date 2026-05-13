#!/usr/bin/env bash
# test_audit_llm_provider_coverage.sh — CCC2 pinning tests for
# audit-llm-provider-coverage.sh (720bcc175).
#
# Tests:
#   1. Empty REPO_DIR → exits 0 with total_call_sites: 0 on stderr.
#   2. File mentioning ONLY deepseek → primary=deepseek, has_deepseek=yes.
#   3. File mentioning ONLY anthropic → primary=anthropic, has_deepseek=no
#      (the gap-finding case the audit was built for).
#   4. Excluded dirs (.venv, worktrees) are skipped.
#   5. --json mode emits one JSON line per call site.
#   6. --summary mode prints summary, no table rows.
#
# Strategy: build a synthetic REPO_DIR with sentinel files under
# scripts/, memory/, tools/, etc. Use the REPO_DIR env var (already
# overridable in the script) to point the audit at our sandbox.
# ZSF: every assertion logs script stdout/stderr on failure.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SCRIPT_DIR/../audit-llm-provider-coverage.sh"

if [[ ! -f "$TARGET" ]]; then
    echo "FAIL: script not found: $TARGET"
    exit 1
fi

PASS=0
FAIL=0
note_pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
note_fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

SANDBOX="$(mktemp -d -t llm-audit-test.XXXXXX)"
cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

# Layout matches what the script scans: scripts/, memory/, 3-surgeons/, tools/.
mkdir -p "$SANDBOX/scripts" "$SANDBOX/memory" "$SANDBOX/3-surgeons" "$SANDBOX/tools"

# ---- Test 1: empty repo ----
echo "=== Test 1: empty REPO_DIR ==="
STDERR_FILE="$SANDBOX/stderr1"
STDOUT="$(REPO_DIR="$SANDBOX" bash "$TARGET" 2>"$STDERR_FILE" || true)"
if grep -q "total_call_sites:        0" "$STDERR_FILE"; then
    note_pass "empty repo → total_call_sites: 0"
else
    note_fail "empty repo summary mismatch"
    echo "    stdout: $STDOUT"
    echo "    stderr:"; sed 's/^/      /' "$STDERR_FILE"
fi

# ---- Test 2: deepseek-only file ----
echo ""
echo "=== Test 2: deepseek-only file ==="
cat > "$SANDBOX/scripts/ds_only.py" <<'EOF'
# Hits deepseek pattern.
import os
key = os.environ["DEEPSEEK_API_KEY"]
endpoint = "https://api.deepseek.com/v1/chat/completions"
model = "deepseek-chat"
EOF
STDOUT="$(REPO_DIR="$SANDBOX" bash "$TARGET" 2>/dev/null || true)"
if echo "$STDOUT" | grep -qE "ds_only\.py.*deepseek.*yes"; then
    note_pass "deepseek-only file → primary=deepseek has_deepseek=yes"
else
    note_fail "deepseek-only row missing/incorrect"
    echo "$STDOUT" | sed 's/^/    /'
fi

# ---- Test 3: anthropic-only gap file ----
echo ""
echo "=== Test 3: anthropic-only file (gap case) ==="
cat > "$SANDBOX/memory/anth_only.py" <<'EOF'
# Anthropic-only call site — this is the gap case the audit catches.
import os
key = os.environ["ANTHROPIC_API_KEY"]
endpoint = "https://api.anthropic.com/v1/messages"
model = "claude-3-5-sonnet-latest"
EOF
STDOUT="$(REPO_DIR="$SANDBOX" bash "$TARGET" 2>/dev/null || true)"
if echo "$STDOUT" | grep -qE "anth_only\.py.*anthropic.*no$"; then
    note_pass "anthropic-only file flagged with has_deepseek=no"
else
    note_fail "anthropic-only gap row missing/incorrect"
    echo "$STDOUT" | sed 's/^/    /'
fi

# Confirm primary classification — should be 'anthropic' for this file.
if echo "$STDOUT" | grep -E "anth_only\.py" | grep -q "anthropic"; then
    note_pass "anthropic-only file primary=anthropic"
else
    note_fail "anthropic-only file primary not classified anthropic"
fi

# ---- Test 4: excluded dirs ----
echo ""
echo "=== Test 4: excluded dirs (.venv, worktrees) ==="
mkdir -p "$SANDBOX/scripts/.venv" "$SANDBOX/scripts/worktrees"
cat > "$SANDBOX/scripts/.venv/forbidden.py" <<'EOF'
key = "ANTHROPIC_API_KEY"
EOF
cat > "$SANDBOX/scripts/worktrees/forbidden.py" <<'EOF'
key = "OPENAI_API_KEY"
EOF
STDOUT="$(REPO_DIR="$SANDBOX" bash "$TARGET" 2>/dev/null || true)"
if echo "$STDOUT" | grep -qE "/\.venv/|/worktrees/"; then
    note_fail "excluded dir leaked into audit"
    echo "$STDOUT" | grep -E "/\.venv/|/worktrees/" | sed 's/^/    /'
else
    note_pass "excluded dirs not present in output"
fi

# ---- Test 5: --json mode ----
echo ""
echo "=== Test 5: --json mode ==="
JSON_OUT="$(REPO_DIR="$SANDBOX" bash "$TARGET" --json 2>/dev/null || true)"
if echo "$JSON_OUT" | grep -qE '^\{"site":".*ds_only\.py'; then
    note_pass "json mode emitted deepseek-only record"
else
    note_fail "json mode did not emit expected record"
    echo "$JSON_OUT" | sed 's/^/    /'
fi

# ---- Test 6: --summary mode ----
echo ""
echo "=== Test 6: --summary mode ==="
STDOUT_FILE="$SANDBOX/sum.out"
STDERR_FILE="$SANDBOX/sum.err"
REPO_DIR="$SANDBOX" bash "$TARGET" --summary >"$STDOUT_FILE" 2>"$STDERR_FILE" || true
# Summary mode: no table rows on stdout, summary goes to stderr.
if [[ ! -s "$STDOUT_FILE" ]]; then
    note_pass "summary mode emits no rows on stdout"
else
    note_fail "summary mode unexpectedly emitted stdout"
    sed 's/^/    /' "$STDOUT_FILE"
fi
if grep -q "total_call_sites:" "$STDERR_FILE"; then
    note_pass "summary mode emits totals to stderr"
else
    note_fail "summary mode missing totals on stderr"
    sed 's/^/    /' "$STDERR_FILE"
fi

# ---- Results ----
echo ""
echo "=== Results ==="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
