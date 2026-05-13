#!/usr/bin/env bash
# test_validate_aaron_runbooks.sh — unit tests for validate-aaron-runbooks.sh
#
# Synthetic fixture with 2 runbooks (one good --dry-run, one with a broken
# script path and a destructive command) — validator must catch both.
#
# AAA4 — 2026-05-12.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALIDATOR="$SCRIPT_DIR/../validate-aaron-runbooks.sh"

if [[ ! -x "$VALIDATOR" ]]; then
    echo "FAIL: validator missing or not executable: $VALIDATOR"
    exit 1
fi

# Build a synthetic fixture in a tmp dir.
FIXTURE="$(mktemp -d)"
trap 'rm -rf "$FIXTURE"' EXIT

mkdir -p "$FIXTURE/docs/runbooks"
mkdir -p "$FIXTURE/scripts"

# Good runbook — one --dry-run flag, one safe ls.
cat > "$FIXTURE/docs/runbooks/good.md" <<'MD'
# Good Runbook

## Step 1 — preview

```bash
bash scripts/example-tool.sh --dry-run
ls -la scripts/example-tool.sh
```

## Step 2 — apply (Aaron only)

```bash
bash scripts/example-tool.sh --apply
```
MD

# Bad runbook — references a script that does not exist + destructive op.
cat > "$FIXTURE/docs/runbooks/bad.md" <<'MD'
# Bad Runbook

## Step 1 — preview

```bash
python3 scripts/totally-does-not-exist.py --dry-run
```

## Step 2 — destructive (validator must skip)

```bash
sudo rm -rf /
```
MD

# Real example-tool.sh so the good --dry-run actually succeeds. It accepts
# --dry-run and prints "OK", or --apply and exits 1 (validator must not run it).
cat > "$FIXTURE/scripts/example-tool.sh" <<'SH'
#!/usr/bin/env bash
case "${1:-}" in
    --dry-run) echo "OK"; exit 0 ;;
    --apply)   echo "APPLY"; exit 1 ;;
    *)         echo "usage: --dry-run | --apply"; exit 2 ;;
esac
SH
chmod +x "$FIXTURE/scripts/example-tool.sh"

# --- Run validator against the fixture --------------------------------------

OUT_JSON="$FIXTURE/out.json"

# We point --runbooks at the fixture, but $REPO_ROOT inside the validator is
# computed from BASH_SOURCE — we override by chdir + symlinking scripts.
# Simpler: run validator with REPO_ROOT spoofed via a wrapper that cds to
# the fixture and invokes the validator from there.
mkdir -p "$FIXTURE/wrap/scripts"
cp "$VALIDATOR" "$FIXTURE/wrap/scripts/validate-aaron-runbooks.sh"
chmod +x "$FIXTURE/wrap/scripts/validate-aaron-runbooks.sh"
mkdir -p "$FIXTURE/wrap/docs"
ln -s "$FIXTURE/docs/runbooks" "$FIXTURE/wrap/docs/runbooks"
ln -s "$FIXTURE/scripts/example-tool.sh" "$FIXTURE/wrap/scripts/example-tool.sh"

set +e
bash "$FIXTURE/wrap/scripts/validate-aaron-runbooks.sh" \
    --runbooks "$FIXTURE/wrap/docs/runbooks" \
    --json "$OUT_JSON" \
    --quiet > "$FIXTURE/stdout.log" 2> "$FIXTURE/stderr.log"
RC=$?
set -e

FAILURES=0
fail() {
    echo "FAIL: $*"
    echo "----- stdout -----"; cat "$FIXTURE/stdout.log"
    echo "----- stderr -----"; cat "$FIXTURE/stderr.log"
    if [[ -f "$OUT_JSON" ]]; then
        echo "----- json -----"; cat "$OUT_JSON"
    fi
    FAILURES=$((FAILURES + 1))
}

# Test 1: validator exits non-zero (bad runbook contains broken script ref)
if [[ $RC -eq 0 ]]; then
    fail "expected non-zero exit (bad runbook is broken), got 0"
fi

# Test 2: JSON file exists and parses
if [[ ! -s "$OUT_JSON" ]]; then
    fail "expected JSON output at $OUT_JSON"
fi

# Test 3: good.md is PASS, bad.md is FAIL
GOOD_VERDICT="$(python3 -c "
import json
d = json.load(open('$OUT_JSON'))
for rb in d['runbooks']:
    if rb['runbook'] == 'good.md': print(rb['verdict'])
")"
BAD_VERDICT="$(python3 -c "
import json
d = json.load(open('$OUT_JSON'))
for rb in d['runbooks']:
    if rb['runbook'] == 'bad.md': print(rb['verdict'])
")"

[[ "$GOOD_VERDICT" == "PASS" ]] || fail "good.md verdict expected PASS, got '$GOOD_VERDICT'"
[[ "$BAD_VERDICT"  == "FAIL" ]] || fail "bad.md verdict expected FAIL, got '$BAD_VERDICT'"

# Test 4: bad.md must have broken>=1 AND destructive>=1
BAD_BROKEN="$(python3 -c "
import json
d = json.load(open('$OUT_JSON'))
for rb in d['runbooks']:
    if rb['runbook'] == 'bad.md': print(rb['broken'])
")"
BAD_DESTR="$(python3 -c "
import json
d = json.load(open('$OUT_JSON'))
for rb in d['runbooks']:
    if rb['runbook'] == 'bad.md': print(rb['destructive'])
")"

[[ "$BAD_BROKEN" -ge 1 ]] || fail "bad.md should have broken>=1, got '$BAD_BROKEN'"
[[ "$BAD_DESTR"  -ge 1 ]] || fail "bad.md should have destructive>=1, got '$BAD_DESTR'"

# Test 5: good.md must have at least 1 dry-run command that exited 0
GOOD_DRY_OK="$(python3 -c "
import json
d = json.load(open('$OUT_JSON'))
for rb in d['runbooks']:
    if rb['runbook'] != 'good.md': continue
    for c in rb['commands']:
        if c['kind'] == 'dry' and c['exit'] == 0:
            print('YES'); break
    else:
        print('NO')
")"
[[ "$GOOD_DRY_OK" == "YES" ]] || fail "good.md should have a dry-run command that exited 0, got '$GOOD_DRY_OK'"

# Test 6: validator must NEVER have executed `sudo rm -rf /` (we'd be dead)
SUDO_EXEC="$(python3 -c "
import json
d = json.load(open('$OUT_JSON'))
for rb in d['runbooks']:
    if rb['runbook'] != 'bad.md': continue
    for c in rb['commands']:
        if 'rm -rf' in c['cmd']:
            print(c['kind'], c.get('exit'))
")"
case "$SUDO_EXEC" in
    "destructive None"|"destructive null") : ;;  # expected
    *) fail "rm -rf / must be classified destructive and never executed; got '$SUDO_EXEC'" ;;
esac

# Test 7: schema field present
SCHEMA="$(python3 -c "import json; print(json.load(open('$OUT_JSON')).get('schema',''))")"
[[ "$SCHEMA" == "aaron-runbook-validator/v1" ]] || fail "schema field missing/wrong: '$SCHEMA'"

if [[ $FAILURES -eq 0 ]]; then
    echo "PASS: all $(( $(grep -c 'fail\b' "$0") - 2 )) checks (test_validate_aaron_runbooks.sh)"
    exit 0
else
    echo "FAILURES: $FAILURES"
    exit 1
fi
