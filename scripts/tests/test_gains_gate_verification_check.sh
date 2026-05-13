#!/usr/bin/env bash
# test_gains_gate_verification_check.sh — RACE X3
#
# Verifies scripts/gains_gate_verification.py — the
# verification-before-completion gate wire that backs gains-gate.sh check #17.
#
#   T1. Recent commit claims "all tests pass" + has a real tests/ dir whose
#       tests pass        → exit 0 (PASS).
#   T2. Recent commit claims "all tests pass" + tests/ dir has a FAILING test
#                         → exit 1 (CRITICAL — skill Iron Law fires).
#   T3. Recent commit with NO completion vocabulary
#                         → exit 0 (PASS — nothing to verify).
#   T4. Recent commit claims completion but no test scope detected
#                         → exit 2 (WARNING — exploratory work allowed).
#   T5. Every run writes a gains-gate-verification-${YYYY-MM-DD}.log line.
#
# Each test runs in its own throwaway repo under $TMPDIR so the host repo's
# git log / index is never touched. The helper script is copied in (not
# symlinked) so the full file-resolution path is exercised.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HELPER_SRC="$REPO_ROOT/scripts/gains_gate_verification.py"
NORTH_STAR_SRC="$REPO_ROOT/scripts/north_star.py"

if [ ! -f "$HELPER_SRC" ]; then
  echo "FAIL: missing $HELPER_SRC" >&2
  exit 2
fi

PASS=0
FAIL=0

ok() { printf "  PASS: %s\n" "$*"; PASS=$((PASS+1)); }
ko() { printf "  FAIL: %s\n" "$*"; FAIL=$((FAIL+1)); }

# Pick a python3. Prefer the repo venv when present, else the host one.
PY="$REPO_ROOT/.venv/bin/python3"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  echo "FAIL: no python3 on PATH" >&2
  exit 2
fi

new_sandbox() {
  local dir
  dir=$(mktemp -d -t race-x3-XXXXXX)
  (
    cd "$dir" || exit 1
    git init -q
    git config user.email "race-x3@test.local"
    git config user.name  "RACE X3 Test"
    git config commit.gpgsign false
    mkdir -p scripts logs
    cp "$HELPER_SRC" scripts/gains_gate_verification.py
    if [ -f "$NORTH_STAR_SRC" ]; then
      cp "$NORTH_STAR_SRC" scripts/north_star.py
    fi
    chmod +x scripts/gains_gate_verification.py
    # Seed initial commit so HEAD exists.
    echo "seed" > README.md
    git add README.md
    git commit -q -m "chore: seed sandbox"
  )
  echo "$dir"
}

# -------------------------------------------------------------------------
# T1: claim + passing tests → PASS
# -------------------------------------------------------------------------
echo "[T1] completion claim with passing pytest → PASS"
SBX=$(new_sandbox)
(
  cd "$SBX" || exit 1
  mkdir -p mymod/tests
  cat > mymod/__init__.py <<'PY'
def add(a, b):
    return a + b
PY
  cat > mymod/tests/test_add.py <<'PY'
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from mymod import add

def test_add_passes():
    assert add(2, 3) == 5
PY
  git add mymod
  git commit -q -m "feat(mymod): all tests pass"
  set +o pipefail
  out=$("$PY" scripts/gains_gate_verification.py --repo "$PWD" 2>&1)
  rc=$?
  set -o pipefail
  if [ $rc -ne 0 ]; then
    echo "rc=$rc out=$out"
    exit 2
  fi
  echo "$out" | tail -1 | grep -q "^PASS|" || exit 3
)
RC=$?
if [ $RC -eq 0 ]; then ok "passing-tests claim accepted"; else ko "T1 rc=$RC"; fi
rm -rf "$SBX"

# -------------------------------------------------------------------------
# T2: claim + FAILING tests → FAIL (skill Iron Law)
# -------------------------------------------------------------------------
echo "[T2] completion claim with FAILING pytest → CRITICAL"
SBX=$(new_sandbox)
(
  cd "$SBX" || exit 1
  mkdir -p mymod/tests
  cat > mymod/__init__.py <<'PY'
def add(a, b):
    return a + b + 1  # bug
PY
  cat > mymod/tests/test_add.py <<'PY'
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from mymod import add

def test_add_passes():
    assert add(2, 3) == 5  # will fail because impl is buggy
PY
  git add mymod
  git commit -q -m "feat(mymod): bug fixed and all tests pass"
  set +o pipefail
  out=$("$PY" scripts/gains_gate_verification.py --repo "$PWD" 2>&1)
  rc=$?
  set -o pipefail
  if [ $rc -ne 1 ]; then
    echo "expected rc=1 got rc=$rc out=$out"
    exit 2
  fi
  echo "$out" | tail -1 | grep -q "^FAIL|" || exit 3
)
RC=$?
if [ $RC -eq 0 ]; then ok "failing-tests claim blocks"; else ko "T2 rc=$RC"; fi
rm -rf "$SBX"

# -------------------------------------------------------------------------
# T3: no completion vocabulary → PASS (nothing to verify)
# -------------------------------------------------------------------------
echo "[T3] no completion vocabulary → PASS"
SBX=$(new_sandbox)
(
  cd "$SBX" || exit 1
  echo "exploratory" > scratch.txt
  git add scratch.txt
  # Subject must contain NONE of: complete done passes passing pass fixed
  # working verified green ship/shipped landed succeeded build success lgtm
  git commit -q -m "wip: rough sketch of approach"
  set +o pipefail
  out=$("$PY" scripts/gains_gate_verification.py --repo "$PWD" 2>&1)
  rc=$?
  set -o pipefail
  if [ $rc -ne 0 ]; then
    echo "expected rc=0 got rc=$rc out=$out"
    exit 2
  fi
  echo "$out" | tail -1 | grep -q "^PASS|" || exit 3
)
RC=$?
if [ $RC -eq 0 ]; then ok "no-claim commit allowed"; else ko "T3 rc=$RC"; fi
rm -rf "$SBX"

# -------------------------------------------------------------------------
# T4: claim made, no detectable test scope → WARN (exploratory permitted)
# -------------------------------------------------------------------------
echo "[T4] completion claim with no test scope → WARN"
SBX=$(new_sandbox)
(
  cd "$SBX" || exit 1
  mkdir -p docs
  echo "fixed bug" > docs/notes.md
  git add docs/notes.md
  git commit -q -m "docs: notes complete and verified"
  set +o pipefail
  out=$("$PY" scripts/gains_gate_verification.py --repo "$PWD" 2>&1)
  rc=$?
  set -o pipefail
  if [ $rc -ne 2 ]; then
    echo "expected rc=2 got rc=$rc out=$out"
    exit 2
  fi
  echo "$out" | tail -1 | grep -q "^WARN|" || exit 3
)
RC=$?
if [ $RC -eq 0 ]; then ok "claim-without-tests warns (not fail)"; else ko "T4 rc=$RC"; fi
rm -rf "$SBX"

# -------------------------------------------------------------------------
# T5: log file is created and parseable
# -------------------------------------------------------------------------
echo "[T5] observability log written"
SBX=$(new_sandbox)
(
  cd "$SBX" || exit 1
  echo "x" > x.txt
  git add x.txt
  git commit -q -m "chore: x"
  "$PY" scripts/gains_gate_verification.py --repo "$PWD" >/dev/null 2>&1
  ls logs/gains-gate-verification-*.log >/dev/null 2>&1 || exit 2
  # First line should be valid JSON
  head -1 logs/gains-gate-verification-*.log \
    | "$PY" -c 'import json,sys; json.loads(sys.stdin.read())' \
    || exit 3
)
RC=$?
if [ $RC -eq 0 ]; then ok "log file created + JSON parseable"; else ko "T5 rc=$RC"; fi
rm -rf "$SBX"

# -------------------------------------------------------------------------
echo
echo "Verification-check tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
