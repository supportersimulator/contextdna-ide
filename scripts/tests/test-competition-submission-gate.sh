#!/bin/bash
# ============================================================================
# Test: Competition Submission Governance Gate
# ============================================================================
# Smoke tests that exercise each of the 8 named checks.
#   1. valid-artifact + valid-metadata + faked signoff → expects PASS-ish
#      (some checks may degrade to fallback warnings — they still count as
#      passes because severity downgrades to WARNING when modules absent).
#   2. missing-artifact            → exit 1 (artifact-exists fails)
#   3. broken-metadata-schema      → exit 1 (metadata-schema fails)
#   4. secrets in artifact         → exit 1 (no-secrets fails)
#   5. artifact outside submissions/ → exit 1 (reversibility-path fails)
#   6. no chief signoff            → exit 1 (constitutional-signoff fails)
#
# Exit: 0 = all assertions pass, 1 = any assertion failed
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
GATE="$REPO_DIR/scripts/competition-submission-gate.sh"

if [[ ! -x "$GATE" ]]; then
    echo "FAIL: gate not executable: $GATE"
    exit 1
fi

WORKDIR=$(mktemp -d -t submission-gate-tests-XXXXXX)
SUB_DIR="$REPO_DIR/submissions"
mkdir -p "$SUB_DIR"
DECISIONS_DIR="$REPO_DIR/.fleet/audits"
mkdir -p "$DECISIONS_DIR"
TODAY=$(date +%Y-%m-%d)
DECISION_FILE="$DECISIONS_DIR/${TODAY}-decisions.md"
DECISION_BACKUP=""
[[ -f "$DECISION_FILE" ]] && DECISION_BACKUP=$(mktemp) && cp "$DECISION_FILE" "$DECISION_BACKUP"

PASS=0
FAIL=0

cleanup() {
    rm -rf "$WORKDIR"
    rm -f "$SUB_DIR"/.gate-test-*.csv
    if [[ -n "$DECISION_BACKUP" ]]; then
        cp "$DECISION_BACKUP" "$DECISION_FILE"
        rm -f "$DECISION_BACKUP"
    else
        # If the file didn't exist before, only remove if it's our test garbage
        if [[ -f "$DECISION_FILE" ]]; then
            grep -q "submission-gate-test-" "$DECISION_FILE" 2>/dev/null && rm -f "$DECISION_FILE" || true
        fi
    fi
}
trap cleanup EXIT

assert_rc() {
    local name="$1"
    local expected="$2"
    local actual="$3"
    local out="$4"
    if [[ "$actual" -eq "$expected" ]]; then
        echo "  PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name (rc=$actual, expected $expected)"
        echo "    output: ${out:0:600}"
        FAIL=$((FAIL + 1))
    fi
}

write_signoff() {
    local sub_id="$1"
    local verdict="$2"  # ACCEPT | ROLLBACK | HALT_GREEN_LIGHT
    cat >> "$DECISION_FILE" <<EOF

### C-submission-${sub_id} — ${verdict}
- ts: $(date +%s)
- finding_ids: submission-gate-test-${sub_id}
- consensus: 1.00 (1 iter)
- rationale: smoke test fixture
EOF
}

# ── Case 1: PASS path
echo "=== Submission Gate Tests ==="
SUB1="$SUB_DIR/.gate-test-1.csv"
META1="$WORKDIR/meta1.json"
echo "id,target" > "$SUB1"
echo "1,0.5" >> "$SUB1"
cat > "$META1" <<EOF
{"submission_id": "test-pass-1", "competition": "smoke",
 "produced_at": $(date +%s), "regenerate_cmd": null}
EOF
write_signoff "test-pass-1" "ACCEPT"
out=$("$GATE" --artifact "$SUB1" --metadata "$META1" 2>&1)
rc=$?
assert_rc "valid artifact + signoff (any fallback warnings still PASS)" 0 "$rc" "$out"

# ── Case 2: missing artifact
SUB2="$SUB_DIR/.gate-test-DOES-NOT-EXIST.csv"
META2="$WORKDIR/meta2.json"
cat > "$META2" <<EOF
{"submission_id": "test-miss", "competition": "smoke",
 "produced_at": $(date +%s), "regenerate_cmd": null}
EOF
write_signoff "test-miss" "ACCEPT"
out=$("$GATE" --artifact "$SUB2" --metadata "$META2" 2>&1)
rc=$?
assert_rc "missing artifact → exit 1" 1 "$rc" "$out"
echo "$out" | grep -q "artifact-exists" || { echo "    (no artifact-exists message)"; }

# ── Case 3: broken metadata schema
SUB3="$SUB_DIR/.gate-test-3.csv"
META3="$WORKDIR/meta3.json"
echo "id,target" > "$SUB3"
echo "1,0.5" >> "$SUB3"
cat > "$META3" <<EOF
{"this_is_not_a_valid_schema": true}
EOF
write_signoff "test-bad-schema" "ACCEPT"
out=$("$GATE" --artifact "$SUB3" --metadata "$META3" 2>&1)
rc=$?
assert_rc "broken metadata schema → exit 1" 1 "$rc" "$out"

# ── Case 4: secrets in artifact
SUB4="$SUB_DIR/.gate-test-4.csv"
META4="$WORKDIR/meta4.json"
cat > "$SUB4" <<'CSV'
id,target,note
1,0.5,API_KEY=sk-1234567890abcdefghij1234567890abcdefghij
CSV
cat > "$META4" <<EOF
{"submission_id": "test-secrets", "competition": "smoke",
 "produced_at": $(date +%s), "regenerate_cmd": null}
EOF
write_signoff "test-secrets" "ACCEPT"
out=$("$GATE" --artifact "$SUB4" --metadata "$META4" 2>&1)
rc=$?
assert_rc "secrets in artifact → exit 1" 1 "$rc" "$out"
echo "$out" | grep -q "no-secrets" || echo "    (warning: no-secrets line not found in output)"

# ── Case 5: artifact outside submissions/
SUB5="$WORKDIR/outside.csv"
META5="$WORKDIR/meta5.json"
echo "id,target" > "$SUB5"
echo "1,0.5" >> "$SUB5"
cat > "$META5" <<EOF
{"submission_id": "test-outside", "competition": "smoke",
 "produced_at": $(date +%s), "regenerate_cmd": null}
EOF
write_signoff "test-outside" "ACCEPT"
out=$("$GATE" --artifact "$SUB5" --metadata "$META5" 2>&1)
rc=$?
assert_rc "artifact outside submissions/ → exit 1" 1 "$rc" "$out"

# ── Case 6: no chief signoff
SUB6="$SUB_DIR/.gate-test-6.csv"
META6="$WORKDIR/meta6.json"
echo "id,target" > "$SUB6"
echo "1,0.5" >> "$SUB6"
cat > "$META6" <<EOF
{"submission_id": "test-no-signoff-${RANDOM}-${RANDOM}", "competition": "smoke",
 "produced_at": $(date +%s), "regenerate_cmd": null}
EOF
# Deliberately do NOT write_signoff for this submission_id
out=$("$GATE" --artifact "$SUB6" --metadata "$META6" 2>&1)
rc=$?
assert_rc "no chief signoff → exit 1" 1 "$rc" "$out"

# ── Case 7: ROLLBACK signoff blocks
SUB7="$SUB_DIR/.gate-test-7.csv"
META7="$WORKDIR/meta7.json"
echo "id,target" > "$SUB7"
echo "1,0.5" >> "$SUB7"
cat > "$META7" <<EOF
{"submission_id": "test-rollback", "competition": "smoke",
 "produced_at": $(date +%s), "regenerate_cmd": null}
EOF
write_signoff "test-rollback" "ROLLBACK"
out=$("$GATE" --artifact "$SUB7" --metadata "$META7" 2>&1)
rc=$?
assert_rc "ROLLBACK signoff blocks → exit 1" 1 "$rc" "$out"

# ── Case 8: setup error (missing metadata file)
out=$("$GATE" --artifact "$SUB1" --metadata "/nonexistent/path.json" 2>&1)
rc=$?
assert_rc "missing metadata file → setup error rc=2" 2 "$rc" "$out"

echo "=== Results: ${PASS} pass, ${FAIL} fail ==="
[[ "$FAIL" -gt 0 ]] && exit 1
exit 0
