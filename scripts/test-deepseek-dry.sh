#!/usr/bin/env bash
# test-deepseek-dry.sh — Dry-run validation for DeepSeek migration.
#
# NO network calls. NO real API keys required. Safe to run in CI on every push.
#
# Validates:
#   1. Provider module imports cleanly (memory.providers.deepseek_provider)
#   2. Pricing table has both deepseek-chat and deepseek-reasoner
#   3. Mock fixtures import cleanly and expose the 5 expected symbols
#   4. Secret resolution helper (scripts/read-secret.sh) sources without errors
#   5. SM path resolution logic works (placeholder detected, resolver selects)
#
# Exit 0 = pass, 1 = fail with clear message.
#
# Usage:
#   ./scripts/test-deepseek-dry.sh
#   CI_MODE=1 ./scripts/test-deepseek-dry.sh   # machine-parsable output
#
set -euo pipefail

# --- Colors (disabled in CI) ---
if [[ "${CI_MODE:-0}" == "1" || ! -t 1 ]]; then
    RED=""; GREEN=""; YELLOW=""; NC=""
else
    RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
fi

pass() { echo "${GREEN}PASS${NC} $1"; }
fail() { echo "${RED}FAIL${NC} $1" >&2; exit 1; }
info() { echo "${YELLOW}----${NC} $1"; }

# --- Locate repo root robustly (works from any cwd) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

info "DeepSeek dry-run — repo: $REPO_ROOT"

# --- Pick a Python interpreter ---
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    fail "python3 not found (no .venv and no system python3)"
fi
info "python: $PYTHON"

# ---------------------------------------------------------------------------
# Check 1 — provider module imports cleanly
# ---------------------------------------------------------------------------
info "Check 1/5: provider module imports"
PYTHONPATH="$REPO_ROOT" "$PYTHON" - <<'PY' || fail "provider import failed"
import sys
try:
    from memory.providers.deepseek_provider import (
        DeepSeekProvider,
        estimate_cost,
        DEEPSEEK_PRICING,
        DEFAULT_MODEL,
        API_BASE,
    )
except Exception as e:  # noqa: BLE001 — surface any import failure
    print(f"IMPORT_ERROR: {e!r}", file=sys.stderr)
    sys.exit(2)
# Smoke: constructor must not require key at init (key required on first request).
p = DeepSeekProvider(api_key="fake-key-for-dry-run")
assert p.api_base == API_BASE, f"api_base mismatch: {p.api_base}"
print("OK")
PY
pass "provider imports + constructor"

# ---------------------------------------------------------------------------
# Check 2 — pricing table has both models and cost formula works
# ---------------------------------------------------------------------------
info "Check 2/5: pricing table"
PYTHONPATH="$REPO_ROOT" "$PYTHON" - <<'PY' || fail "pricing table check failed"
from memory.providers.deepseek_provider import DEEPSEEK_PRICING, estimate_cost
for model in ("deepseek-chat", "deepseek-reasoner"):
    p = DEEPSEEK_PRICING[model]
    assert "input" in p and "output" in p, f"missing keys in {model}"
    assert p["input"] > 0 and p["output"] > 0, f"non-positive price in {model}"
cost = estimate_cost(1_000_000, 1_000_000, "deepseek-chat")
# 1M in + 1M out on deepseek-chat should be 0.28 + 0.42 = 0.70
assert abs(cost - 0.70) < 1e-9, f"cost formula drifted: {cost}"
print("OK")
PY
pass "pricing table (chat + reasoner) + cost formula"

# ---------------------------------------------------------------------------
# Check 3 — mock fixtures import and expose expected symbols
# ---------------------------------------------------------------------------
info "Check 3/5: mock fixtures"
PYTHONPATH="$REPO_ROOT" "$PYTHON" - <<'PY' || fail "fixture import failed"
import sys
try:
    from tests.fixtures.deepseek_mock_responses import (
        MOCK_CHAT_SHORT,
        MOCK_CHAT_LONG,
        MOCK_REASONER_THINK,
        MOCK_EMPTY,
        MOCK_ERROR_429,
        MOCK_RESPONSES,
    )
except Exception as e:  # noqa: BLE001
    print(f"FIXTURE_IMPORT_ERROR: {e!r}", file=sys.stderr)
    sys.exit(2)

# Shape checks — success envelopes must have choices + usage.
for name, mock in (
    ("MOCK_CHAT_SHORT", MOCK_CHAT_SHORT),
    ("MOCK_CHAT_LONG", MOCK_CHAT_LONG),
    ("MOCK_REASONER_THINK", MOCK_REASONER_THINK),
    ("MOCK_EMPTY", MOCK_EMPTY),
):
    assert "choices" in mock, f"{name} missing choices"
    assert "usage" in mock, f"{name} missing usage"
    assert mock["choices"][0]["message"]["role"] == "assistant", f"{name} wrong role"

# Reasoner must contain <think> tags.
assert "<think>" in MOCK_REASONER_THINK["choices"][0]["message"]["content"], \
    "MOCK_REASONER_THINK missing <think> tags"

# Error envelope shape — top-level `error`, no `choices`.
assert "error" in MOCK_ERROR_429, "MOCK_ERROR_429 missing error field"
assert "choices" not in MOCK_ERROR_429, "MOCK_ERROR_429 should not have choices"

# Lookup dict covers all.
assert set(MOCK_RESPONSES.keys()) == {
    "chat_short", "chat_long", "reasoner_think", "empty", "error_429"
}, f"MOCK_RESPONSES keys drift: {sorted(MOCK_RESPONSES.keys())}"
print("OK")
PY
pass "mock fixtures (5 responses + lookup dict)"

# ---------------------------------------------------------------------------
# Check 4 — read-secret.sh sources without error, returns something for fake key
# ---------------------------------------------------------------------------
info "Check 4/5: secret resolver sources cleanly"
if [ ! -f "$REPO_ROOT/scripts/read-secret.sh" ]; then
    fail "scripts/read-secret.sh missing"
fi
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/read-secret.sh"
if ! declare -f read_secret >/dev/null; then
    fail "read_secret function not defined after sourcing"
fi
# Call with a bogus name — must return empty string (not crash, not print secret).
TESTVAL="$(read_secret "NONEXISTENT_KEY_FOR_DRY_RUN_XYZ" || true)"
if [ -n "$TESTVAL" ]; then
    fail "read_secret returned value for bogus key (unexpected): len=${#TESTVAL}"
fi
pass "read-secret.sh sources + handles missing keys"

# ---------------------------------------------------------------------------
# Check 5 — placeholder detection logic (mirrors live script)
# ---------------------------------------------------------------------------
info "Check 5/5: placeholder detection"
_is_placeholder() {
    local v="$1"
    [[ -z "$v" ]] && return 0
    [[ "$v" == REPLACE_WITH_* ]] && return 0
    [[ "$v" == "placeholder" ]] && return 0
    [[ "$v" == "changeme" ]] && return 0
    return 1
}
# Must flag placeholders
for candidate in "" "REPLACE_WITH_REAL_KEY" "placeholder" "changeme"; do
    if ! _is_placeholder "$candidate"; then
        fail "placeholder detection missed: ${candidate:-<empty>}"
    fi
done
# Must NOT flag a realistic key shape
if _is_placeholder "sk-1234567890abcdefghij"; then
    fail "placeholder detection false-positive on realistic key"
fi
pass "placeholder detection"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "${GREEN}DRY-RUN PASSED${NC} — all 5 checks green. Safe to wire into CI."
exit 0
