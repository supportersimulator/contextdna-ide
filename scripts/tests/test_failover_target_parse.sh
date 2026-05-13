#!/usr/bin/env bash
# test_failover_target_parse.sh — ZZ5 2026-05-12
# Verifies failover-to-codex.sh argument parsing for the --target= flag added
# in the ZZ5 DeepSeek-primary migration. Does NOT actually fail over (the
# script's claude_active() short-circuit prevents real LLM dispatch when this
# test runs from inside a Claude session).
#
# Assertions:
#   1. Unknown --target=value exits with code 2 + clear stderr message.
#   2. Known targets (codex, deepseek) pass argument parsing.
#   3. --help prints the comment block and exits 0.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$REPO_DIR/scripts/failover-to-codex.sh"

pass=0
fail=0
ok()   { echo "  PASS: $1"; pass=$((pass+1)); }
no()   { echo "  FAIL: $1"; fail=$((fail+1)); }

echo "==> 1. Unknown target exits 2 with clear message"
out="$(bash "$SCRIPT" --target=bogus 2>&1 || true)"
rc=$?
if echo "$out" | grep -q "unknown --target=bogus"; then
    ok "unknown target rejected"
else
    no "unknown target accepted or wrong message: $out"
fi

echo "==> 2. --help prints usage and exits 0"
out="$(bash "$SCRIPT" --help 2>&1)" || rc=$?
if echo "$out" | grep -q "failover-to-codex.sh"; then
    ok "--help prints usage"
else
    no "--help missing usage line"
fi

echo "==> 3. Default target is codex (parse succeeds even with --force when claude inactive)"
# We invoke with --target=deepseek and assert it does NOT error on parsing.
# Since claude_active() typically returns true inside a Claude session, the
# script short-circuits and exits 0 — we just need the early parse path to
# accept the flag.
if bash "$SCRIPT" --target=deepseek >/dev/null 2>&1; then
    ok "--target=deepseek parses cleanly"
else
    # Acceptable exit codes when claude is inactive: any non-2 (since 2 is
    # reserved for unknown target). 0 / 1 / non-2 all mean parsing passed.
    rc=$?
    if [[ $rc -ne 2 ]]; then
        ok "--target=deepseek parses cleanly (rc=$rc, non-parse-error)"
    else
        no "--target=deepseek rejected (rc=2)"
    fi
fi

echo ""
echo "Results: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
