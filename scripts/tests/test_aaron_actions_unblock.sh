#!/usr/bin/env bash
# Smoke tests for scripts/aaron-actions-unblock.sh
# ≤50 LOC contract.
set -u
SCRIPT="/Users/aarontjomsland/dev/er-simulator-superrepo/scripts/aaron-actions-unblock.sh"
PASS=0; FAIL=0
chk() { if eval "$2"; then echo "PASS $1"; PASS=$((PASS+1)); else echo "FAIL $1 :: $2"; FAIL=$((FAIL+1)); fi; }

# T1: script exists + executable
chk "executable"          "[[ -x '$SCRIPT' ]]"

# T2: --help works (exit 0)
chk "--help exits 0"      "bash '$SCRIPT' --help >/dev/null 2>&1"

# T3: --dry-run runs (exit may be 1 due to preflight FAIL — that IS ZSF working)
OUT=$(bash "$SCRIPT" --dry-run 2>&1)
chk "dry-run produces 13 steps" "echo \"\$OUT\" | grep -qE '\\[13/13\\]'"

# T4: dry-run never claims OK (no mutations possible) except step 1 preflight
NUM_OK=$(echo "$OUT" | grep -cE '\\.\\.\\. OK')
chk "dry-run OK count ≤ 1"  "[[ $NUM_OK -le 1 ]]"

# T5: --only=4 runs only step 4 (KV orphan check)
OUT2=$(bash "$SCRIPT" --dry-run --only 4 2>&1)
chk "--only filters"      "echo \"\$OUT2\" | grep -q 'KV orphan' && ! echo \"\$OUT2\" | grep -q 'PyPI multifleet'"

# T6: unknown arg fails fast (exit 2)
bash "$SCRIPT" --bogus >/dev/null 2>&1
chk "unknown arg exits 2" "[[ \$? -eq 2 ]]"

# T7: ZSF — daemon-unreachable produces FAIL (loud, not silent)
if ! curl -sf -m 1 http://127.0.0.1:8855/health >/dev/null 2>&1; then
  chk "ZSF preflight FAIL surfaced" "echo \"\$OUT\" | grep -qE 'preflight.*FAIL'"
fi

# T8: dependency order — AWS rotation (step 2) appears before PyPI (step 11)
chk "dep order AWS<PyPI"  "[[ \$(echo \"\$OUT\" | grep -nE 'AWS IAM' | head -1 | cut -d: -f1) -lt \$(echo \"\$OUT\" | grep -nE 'PyPI multifleet' | head -1 | cut -d: -f1) ]]"

echo "---"
echo "Tests: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
