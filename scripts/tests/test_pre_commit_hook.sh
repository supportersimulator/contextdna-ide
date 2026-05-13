#!/usr/bin/env bash
# test_pre_commit_hook.sh — RACE Q3
#
# Verifies scripts/git-hooks/pre-commit-zsf-smoke.sh:
#   T1. Stage a file with `except Exception: pass`        -> exit 1
#   T2. Stage a file with NameError at import time         -> exit 1
#   T3. Stage a clean file                                 -> exit 0
#   T4. `git commit --no-verify` bypasses the dispatcher   -> commit succeeds
#
# Each test runs in its own throwaway repo under $TMPDIR so the host repo's
# index is never touched. The hook script is copied in (not symlinked) so we
# can exercise the full file-resolution path it uses in production.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK_SRC="$REPO_ROOT/scripts/git-hooks/pre-commit-zsf-smoke.sh"
INSTALLER="$REPO_ROOT/scripts/install-hooks.sh"
SECRETS_HOOK="$REPO_ROOT/scripts/pre-commit-secrets-check.sh"

if [ ! -x "$HOOK_SRC" ]; then
  chmod +x "$HOOK_SRC" 2>/dev/null || true
fi

PASS=0
FAIL=0

note() { printf "  %s\n" "$*"; }
ok()   { printf "  PASS: %s\n" "$*"; PASS=$((PASS+1)); }
ko()   { printf "  FAIL: %s\n" "$*"; FAIL=$((FAIL+1)); }

new_sandbox() {
  local dir
  dir=$(mktemp -d -t race-q3-XXXXXX)
  (
    cd "$dir" || exit 1
    git init -q
    git config user.email "race-q3@test.local"
    git config user.name  "RACE Q3 Test"
    git config commit.gpgsign false
    mkdir -p memory multi-fleet/multifleet scripts/git-hooks scripts/tests \
             .git/hooks
    cp "$HOOK_SRC"     scripts/git-hooks/pre-commit-zsf-smoke.sh
    cp "$INSTALLER"    scripts/install-hooks.sh 2>/dev/null || true
    cp "$SECRETS_HOOK" scripts/pre-commit-secrets-check.sh 2>/dev/null || true
    chmod +x scripts/git-hooks/pre-commit-zsf-smoke.sh \
             scripts/install-hooks.sh \
             scripts/pre-commit-secrets-check.sh 2>/dev/null || true
    # Install dispatcher.
    bash scripts/install-hooks.sh >/dev/null 2>&1 || true
  )
  echo "$dir"
}

# -------------------------------------------------------------------------
# T1: bare except → fail
# -------------------------------------------------------------------------
echo "[T1] silent-except detection"
SBX=$(new_sandbox)
(
  cd "$SBX" || exit 1
  cat > memory/bad_silent.py <<'PY'
def do_thing():
    try:
        risky()
    except Exception:
        pass
PY
  git add memory/bad_silent.py
  # NOTE: disable pipefail locally — we want grep's exit code, not git's.
  set +o pipefail
  git commit -m "should be blocked" 2>&1 | grep -q "BLOCKED:"
  rc=$?
  set -o pipefail
  exit $([ $rc -eq 0 ] && echo 0 || echo 2)
)
RC=$?
if [ $RC -eq 0 ]; then ok "silent-except blocks commit"; else ko "silent-except slipped through (rc=$RC)"; fi
rm -rf "$SBX"

# -------------------------------------------------------------------------
# T2: NameError at import → fail
# -------------------------------------------------------------------------
echo "[T2] import-smoke catches NameError"
SBX=$(new_sandbox)
(
  cd "$SBX" || exit 1
  cat > multi-fleet/multifleet/bad_import.py <<'PY'
# Top-level reference to an undefined name — surfaces at import time.
VALUE = totally_undefined_symbol + 1
PY
  git add multi-fleet/multifleet/bad_import.py
  set +o pipefail
  git commit -m "should be blocked" 2>&1 | grep -q "BLOCKED: import-smoke"
  rc=$?
  set -o pipefail
  exit $([ $rc -eq 0 ] && echo 0 || echo 2)
)
RC=$?
if [ $RC -eq 0 ]; then ok "NameError blocks commit"; else ko "NameError slipped through (rc=$RC)"; fi
rm -rf "$SBX"

# -------------------------------------------------------------------------
# T3: clean file → pass
# -------------------------------------------------------------------------
echo "[T3] clean file commits successfully"
SBX=$(new_sandbox)
(
  cd "$SBX" || exit 1
  cat > memory/clean_module.py <<'PY'
"""A perfectly innocent module."""
import logging

LOG = logging.getLogger(__name__)


def safe_op(value: int) -> int:
    try:
        return value * 2
    except Exception as exc:
        LOG.exception("safe_op failed: %s", exc)
        raise
PY
  git add memory/clean_module.py
  git commit -m "clean commit" >/dev/null 2>&1
)
RC=$?
if [ $RC -eq 0 ]; then ok "clean file commits cleanly"; else ko "clean file blocked unexpectedly (rc=$RC)"; fi
rm -rf "$SBX"

# -------------------------------------------------------------------------
# T4: --no-verify bypass works
# -------------------------------------------------------------------------
echo "[T4] --no-verify escape hatch"
SBX=$(new_sandbox)
(
  cd "$SBX" || exit 1
  cat > memory/bad_silent.py <<'PY'
def f():
    try:
        x = 1
    except Exception:
        pass
PY
  git add memory/bad_silent.py
  git commit --no-verify -m "bypass" >/dev/null 2>&1
)
RC=$?
if [ $RC -eq 0 ]; then ok "--no-verify bypasses hook"; else ko "--no-verify did not bypass (rc=$RC)"; fi
rm -rf "$SBX"

# -------------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------------
echo
echo "Pre-commit hook tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
