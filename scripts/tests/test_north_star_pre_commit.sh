#!/usr/bin/env bash
# test_north_star_pre_commit.sh — RACE Y5
#
# Verifies scripts/git-hooks/pre-commit-north-star.sh:
#   T1. Aligned subject (matches a priority slot)
#         -> exit 0, "north-star: <priority>" printed.
#   T2. Orphan subject (no slot match), non-interactive
#         -> exit 0 (advisory, never blocks), "WARN" printed.
#   T3. Empty subject
#         -> exit 0, no north-star: / WARN output.
#   T4. Hook is wired into the dispatcher installed by install-hooks.sh
#         and runs as part of `git commit` (smoke).
#
# All cases run against the real hook script using a synthetic
# COMMIT_EDITMSG via NORTH_STAR_COMMIT_MSG_FILE so we don't need a full
# git sandbox for T1-T3. T4 builds a sandbox to exercise the dispatcher.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK_SRC="$REPO_ROOT/scripts/git-hooks/pre-commit-north-star.sh"
INSTALLER="$REPO_ROOT/scripts/install-hooks.sh"
ZSF_HOOK="$REPO_ROOT/scripts/git-hooks/pre-commit-zsf-smoke.sh"
SECRETS_HOOK="$REPO_ROOT/scripts/pre-commit-secrets-check.sh"
NORTH_STAR_PY="$REPO_ROOT/scripts/north_star.py"

if [ ! -x "$HOOK_SRC" ]; then
  chmod +x "$HOOK_SRC" 2>/dev/null || true
fi
if [ ! -f "$HOOK_SRC" ]; then
  echo "FATAL: hook source missing: $HOOK_SRC"
  exit 1
fi
if [ ! -f "$NORTH_STAR_PY" ]; then
  echo "FATAL: north_star.py missing: $NORTH_STAR_PY"
  exit 1
fi

PASS=0
FAIL=0

ok()   { printf "  PASS: %s\n" "$*"; PASS=$((PASS+1)); }
ko()   { printf "  FAIL: %s\n" "$*"; FAIL=$((FAIL+1)); }

run_hook_with_subject() {
  # Args: $1 = subject text (may be empty). Returns: rc + captured stdout
  # in the global $OUT.
  local subject="$1"
  local msg_file
  msg_file=$(mktemp -t race-y5-XXXXXX)
  if [ -n "$subject" ]; then
    printf "%s\n" "$subject" > "$msg_file"
  else
    : > "$msg_file"
  fi
  # NORTH_STAR_NON_INTERACTIVE=1 ensures we never block on /dev/tty even on
  # dev machines where it's readable.
  OUT=$(NORTH_STAR_COMMIT_MSG_FILE="$msg_file" \
        NORTH_STAR_NON_INTERACTIVE=1 \
        bash "$HOOK_SRC" 2>&1)
  RC=$?
  rm -f "$msg_file"
  return $RC
}

# -------------------------------------------------------------------------
# T1: aligned subject -> exit 0 + "north-star:" printed
# -------------------------------------------------------------------------
echo "[T1] aligned subject classifies + prints"
run_hook_with_subject "feat(fleet): add NATS audit relay for chief node"
T1_RC=$?
if [ "$T1_RC" -ne 0 ]; then
  ko "aligned subject hook returned non-zero rc=$T1_RC"
elif ! echo "$OUT" | grep -q "^north-star:"; then
  ko "aligned subject — missing 'north-star:' line. got: $OUT"
elif echo "$OUT" | grep -q "^WARN"; then
  ko "aligned subject — WARN was printed for an in-vector commit. got: $OUT"
else
  ok "aligned subject prints north-star line, exits 0"
fi

# -------------------------------------------------------------------------
# T2: orphan subject -> exit 0, WARN printed (advisory, not blocking)
# -------------------------------------------------------------------------
echo "[T2] orphan subject -> WARN, advisory"
run_hook_with_subject "chore: tweak unrelated yak shaving widget"
T2_RC=$?
if [ "$T2_RC" -ne 0 ]; then
  ko "orphan subject — hook BLOCKED commit (rc=$T2_RC). Must be advisory."
elif ! echo "$OUT" | grep -q "^WARN"; then
  ko "orphan subject — missing WARN. got: $OUT"
elif echo "$OUT" | grep -q "^north-star:"; then
  ko "orphan subject — north-star: line should not appear. got: $OUT"
else
  ok "orphan subject WARNs and exits 0 (advisory, never blocks)"
fi

# -------------------------------------------------------------------------
# T3: empty subject -> exit 0, no output (skip silently)
# -------------------------------------------------------------------------
echo "[T3] empty subject -> skip silently"
run_hook_with_subject ""
T3_RC=$?
if [ "$T3_RC" -ne 0 ]; then
  ko "empty subject — hook returned non-zero rc=$T3_RC"
elif echo "$OUT" | grep -qE "^(north-star:|WARN)"; then
  ko "empty subject — should print nothing. got: $OUT"
else
  ok "empty subject is a clean no-op"
fi

# -------------------------------------------------------------------------
# T4: dispatcher installation wires the hook into a real commit flow.
#     We do NOT exercise a real commit with content (that pulls in ZSF
#     scanner + secrets scanner against the sandbox); instead we verify
#     the dispatcher file references the new hook after install-hooks runs.
# -------------------------------------------------------------------------
echo "[T4] install-hooks wires north-star into dispatcher"
SBX=$(mktemp -d -t race-y5-disp-XXXXXX)
(
  cd "$SBX" || exit 1
  git init -q
  git config user.email "race-y5@test.local"
  git config user.name  "RACE Y5 Test"
  git config commit.gpgsign false
  mkdir -p scripts/git-hooks .git/hooks
  cp "$HOOK_SRC"       scripts/git-hooks/pre-commit-north-star.sh
  cp "$ZSF_HOOK"       scripts/git-hooks/pre-commit-zsf-smoke.sh 2>/dev/null || true
  cp "$INSTALLER"      scripts/install-hooks.sh
  cp "$SECRETS_HOOK"   scripts/pre-commit-secrets-check.sh 2>/dev/null || true
  chmod +x scripts/git-hooks/*.sh scripts/install-hooks.sh \
           scripts/pre-commit-secrets-check.sh 2>/dev/null || true
  bash scripts/install-hooks.sh >/dev/null 2>&1 || exit 2
  if ! grep -q "RACE-Y5-DISPATCHER" .git/hooks/pre-commit; then
    exit 3
  fi
  if ! grep -q "pre-commit-north-star.sh" .git/hooks/pre-commit; then
    exit 4
  fi
)
T4_RC=$?
rm -rf "$SBX"
case "$T4_RC" in
  0) ok "installer wires Y5 dispatcher + north-star hook" ;;
  2) ko "install-hooks.sh failed in sandbox" ;;
  3) ko "dispatcher missing RACE-Y5-DISPATCHER mark" ;;
  4) ko "dispatcher missing reference to pre-commit-north-star.sh" ;;
  *) ko "T4 unexpected rc=$T4_RC" ;;
esac

# -------------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------------
echo
echo "north-star pre-commit tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
