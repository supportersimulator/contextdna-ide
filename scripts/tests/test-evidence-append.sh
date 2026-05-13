#!/bin/bash
# ============================================================================
# W1.a self-test — scripts/append-evidence-ledger.py
# ============================================================================
# Validates the WRITE helper end-to-end:
#
#   1. Happy path: posts an audit event with secret-looking values, asserts
#      record gets written, redaction counter advances, secret NOT visible
#      in stored payload (round-trip via sqlite3).
#   2. Failure: invalid JSON payload -> validation_error, validation counter
#      bumps.
#   3. Failure: missing subject -> argparse rejects (no counter bump because
#      argparse exits before our handler runs — instead we test "empty
#      subject string" which DOES hit our validator).
#   4. Failure: parent_record_id not in ledger -> parent_not_found counter
#      bumps.
#
# All four assertions must pass. Non-zero exit on any failure.
#
# Reversibility
# -------------
# Runs against a SCRATCH ledger (EVIDENCE_LEDGER_DB env redirect via a
# tempdir SUPERREPO_ROOT-relative copy is overkill; instead we point the
# helper at a tempdir SQLite file via SQLITE_DB_OVERRIDE — implemented by
# the helper looking at EVIDENCE_LEDGER_DB env var picked up by the
# memory.evidence_ledger module). Counters file is also redirected.
#
# We DO NOT touch the real `memory/evidence_ledger.db` or
# `memory/.evidence_ledger_append_counters.json`.
#
# ZSF: every assertion is a hard-fail with stderr explanation.
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
HELPER="$REPO_DIR/scripts/append-evidence-ledger.py"
PYTHON="${PYTHON:-python3}"

if [[ ! -f "$HELPER" ]]; then
    echo "[test-evidence-append] FAIL: helper missing: $HELPER" >&2
    exit 1
fi

TMP_DIR=$(mktemp -d -t evidence-append-test-XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT

SCRATCH_DB="$TMP_DIR/evidence.db"
SCRATCH_COUNTERS="$TMP_DIR/counters.json"

# Force the underlying memory.evidence_ledger to use our scratch DB by
# monkey-patching the DB_PATH via a tiny prelude we run BEFORE invoking
# the CLI. We accomplish this by exporting an env var the helper reads,
# but the helper currently doesn't honour one — so we run via a wrapper
# that injects the override at import time.
#
# Cleanest: run python -c that imports memory.evidence_ledger, sets
# DB_PATH, then execs the CLI via runpy. Avoids modifying production
# code paths just for a test.

run_helper() {
    PYTHONPATH="$REPO_DIR" \
    EVIDENCE_LEDGER_APPEND_COUNTERS="$SCRATCH_COUNTERS" \
    "$PYTHON" -c "
import pathlib, runpy, sys
import memory.evidence_ledger as el
el.DB_PATH = pathlib.Path('$SCRATCH_DB')
sys.argv = ['append-evidence-ledger.py'] + sys.argv[1:]
runpy.run_path('$HELPER', run_name='__main__')
" "$@"
}

# Counters helper
counter_value() {
    local key="$1"
    if [[ ! -f "$SCRATCH_COUNTERS" ]]; then
        echo 0
        return
    fi
    "$PYTHON" -c "
import json, sys
try:
    d = json.load(open('$SCRATCH_COUNTERS'))
except Exception:
    print(0); sys.exit(0)
print(int(d.get('$key', 0)))
"
}

assert_eq() {
    local got="$1" want="$2" label="$3"
    if [[ "$got" != "$want" ]]; then
        echo "[test-evidence-append] FAIL: $label: got=$got want=$want" >&2
        exit 1
    fi
    echo "[test-evidence-append] PASS: $label (=$got)"
}

assert_ge() {
    local got="$1" want="$2" label="$3"
    if (( got < want )); then
        echo "[test-evidence-append] FAIL: $label: got=$got want>=$want" >&2
        exit 1
    fi
    echo "[test-evidence-append] PASS: $label (=$got, >= $want)"
}

# ============================================================================
# Snapshot starting counters
# ============================================================================
echo "=== test-evidence-append.sh ==="
echo "scratch DB: $SCRATCH_DB"
echo "scratch counters: $SCRATCH_COUNTERS"

OK_BEFORE=$(counter_value ledger_append_ok_total)
ERR_BEFORE=$(counter_value ledger_append_errors_total)
RED_BEFORE=$(counter_value ledger_append_redactions_total)
VAL_BEFORE=$(counter_value ledger_append_validation_errors_total)
PARENT_BEFORE=$(counter_value ledger_append_parent_not_found_total)

echo "[before] ok=$OK_BEFORE errs=$ERR_BEFORE red=$RED_BEFORE val=$VAL_BEFORE parent=$PARENT_BEFORE"

# ============================================================================
# 1. Happy path with a secret-looking value (api_key + email + nested token)
# ============================================================================
SECRET_PAYLOAD='{"score": 0.92, "api_key": "api_key_leak123", "user_email": "alice@example.com", "nested": {"auth_token": "tok_abc", "ok_field": 42}}'

OUT1=$(run_helper \
    --event-type audit \
    --subject "happy-path-$(date +%s)" \
    --actor "test:happy" \
    --payload-json "$SECRET_PAYLOAD") || {
    echo "[test-evidence-append] FAIL: happy path exited non-zero" >&2
    echo "$OUT1" >&2
    exit 1
}

# Parse JSON output
RECORD_ID=$("$PYTHON" -c "import json,sys; print(json.loads(sys.argv[1])['record_id'])" "$OUT1") || {
    echo "[test-evidence-append] FAIL: happy path output not valid JSON: $OUT1" >&2
    exit 1
}
REDACTED_COUNT=$("$PYTHON" -c "import json,sys; print(json.loads(sys.argv[1])['redacted_count'])" "$OUT1")

assert_ge "$REDACTED_COUNT" 3 "happy path redacted_count >= 3 (api_key + email + token)"

# Verify the secret is NOT in the stored payload (round-trip via sqlite3 read)
STORED_CONTENT=$("$PYTHON" -c "
import sqlite3, sys
conn = sqlite3.connect('$SCRATCH_DB')
row = conn.execute('SELECT content_json FROM evidence_records WHERE record_id = ?', (sys.argv[1],)).fetchone()
print(row[0] if row else '')
" "$RECORD_ID")

if [[ -z "$STORED_CONTENT" ]]; then
    echo "[test-evidence-append] FAIL: stored content empty for record_id=$RECORD_ID" >&2
    exit 1
fi

if echo "$STORED_CONTENT" | grep -q "api_key_leak123"; then
    echo "[test-evidence-append] FAIL: secret 'api_key_leak123' found in stored payload!" >&2
    echo "$STORED_CONTENT" >&2
    exit 1
fi
echo "[test-evidence-append] PASS: secret 'api_key_leak123' NOT in stored payload"

if echo "$STORED_CONTENT" | grep -q "tok_abc"; then
    echo "[test-evidence-append] FAIL: nested secret 'tok_abc' found in stored payload!" >&2
    echo "$STORED_CONTENT" >&2
    exit 1
fi
echo "[test-evidence-append] PASS: nested secret 'tok_abc' NOT in stored payload"

if echo "$STORED_CONTENT" | grep -q "alice@example.com"; then
    echo "[test-evidence-append] FAIL: email 'alice@example.com' found in stored payload!" >&2
    exit 1
fi
echo "[test-evidence-append] PASS: email 'alice@example.com' NOT in stored payload"

# Verify counters bumped
OK_AFTER1=$(counter_value ledger_append_ok_total)
RED_AFTER1=$(counter_value ledger_append_redactions_total)
assert_eq "$OK_AFTER1" "$((OK_BEFORE + 1))" "ledger_append_ok_total bumped"
assert_ge "$RED_AFTER1" "$((RED_BEFORE + 3))" "ledger_append_redactions_total bumped >= 3"

# Save the happy-path record_id so we can reference it as a non-existent
# parent later (deliberately constructing a bad sha256 below).
HAPPY_RECORD_ID="$RECORD_ID"

# ============================================================================
# 2. Failure: invalid JSON payload
# ============================================================================
VAL_BEFORE2=$(counter_value ledger_append_validation_errors_total)
ERR_BEFORE2=$(counter_value ledger_append_errors_total)

OUT2=$(run_helper \
    --event-type audit \
    --subject "bad-json" \
    --actor "test:bad-json" \
    --payload-json "{not json") || RC2=$?
RC2=${RC2:-0}

if [[ "$RC2" != "2" ]]; then
    echo "[test-evidence-append] FAIL: invalid JSON expected rc=2 got=$RC2" >&2
    echo "$OUT2" >&2
    exit 1
fi
echo "[test-evidence-append] PASS: invalid JSON rc=2"

ERR_KIND2=$("$PYTHON" -c "import json,sys; print(json.loads(sys.argv[1])['error_kind'])" "$OUT2" 2>/dev/null || echo "?")
assert_eq "$ERR_KIND2" "validation_error" "invalid JSON error_kind=validation_error"

VAL_AFTER2=$(counter_value ledger_append_validation_errors_total)
assert_eq "$VAL_AFTER2" "$((VAL_BEFORE2 + 1))" "validation_errors counter bumped"

# ============================================================================
# 3. Failure: empty subject (validator rejects)
# ============================================================================
VAL_BEFORE3=$(counter_value ledger_append_validation_errors_total)

OUT3=$(run_helper \
    --event-type audit \
    --subject "" \
    --actor "test:no-subject" \
    --payload-json "{}") || RC3=$?
RC3=${RC3:-0}

if [[ "$RC3" != "2" ]]; then
    echo "[test-evidence-append] FAIL: empty subject expected rc=2 got=$RC3" >&2
    echo "$OUT3" >&2
    exit 1
fi
echo "[test-evidence-append] PASS: empty subject rc=2"

ERR_KIND3=$("$PYTHON" -c "import json,sys; print(json.loads(sys.argv[1])['error_kind'])" "$OUT3" 2>/dev/null || echo "?")
assert_eq "$ERR_KIND3" "validation_error" "empty subject error_kind=validation_error"

VAL_AFTER3=$(counter_value ledger_append_validation_errors_total)
assert_eq "$VAL_AFTER3" "$((VAL_BEFORE3 + 1))" "validation_errors counter bumped (empty subject)"

# ============================================================================
# 4. Failure: parent_record_id not found
# ============================================================================
PARENT_BEFORE4=$(counter_value ledger_append_parent_not_found_total)

# Construct a sha256-shaped string that doesn't exist in the ledger.
BOGUS_PARENT="0000000000000000000000000000000000000000000000000000000000000000"

OUT4=$(run_helper \
    --event-type audit \
    --subject "bad-parent" \
    --actor "test:bad-parent" \
    --payload-json "{}" \
    --parent-record-id "$BOGUS_PARENT") || RC4=$?
RC4=${RC4:-0}

if [[ "$RC4" != "3" ]]; then
    echo "[test-evidence-append] FAIL: bad parent expected rc=3 got=$RC4" >&2
    echo "$OUT4" >&2
    exit 1
fi
echo "[test-evidence-append] PASS: parent_not_found rc=3"

ERR_KIND4=$("$PYTHON" -c "import json,sys; print(json.loads(sys.argv[1])['error_kind'])" "$OUT4" 2>/dev/null || echo "?")
assert_eq "$ERR_KIND4" "parent_not_found" "bad parent error_kind=parent_not_found"

PARENT_AFTER4=$(counter_value ledger_append_parent_not_found_total)
assert_eq "$PARENT_AFTER4" "$((PARENT_BEFORE4 + 1))" "parent_not_found counter bumped"

# ============================================================================
# Final snapshot
# ============================================================================
OK_FINAL=$(counter_value ledger_append_ok_total)
ERR_FINAL=$(counter_value ledger_append_errors_total)
RED_FINAL=$(counter_value ledger_append_redactions_total)
VAL_FINAL=$(counter_value ledger_append_validation_errors_total)
PARENT_FINAL=$(counter_value ledger_append_parent_not_found_total)

echo
echo "=== counters: before -> after ==="
echo "  ledger_append_ok_total:                   $OK_BEFORE -> $OK_FINAL"
echo "  ledger_append_errors_total:               $ERR_BEFORE -> $ERR_FINAL"
echo "  ledger_append_redactions_total:           $RED_BEFORE -> $RED_FINAL"
echo "  ledger_append_validation_errors_total:    $VAL_BEFORE -> $VAL_FINAL"
echo "  ledger_append_parent_not_found_total:     $PARENT_BEFORE -> $PARENT_FINAL"
echo
echo "ALL ASSERTIONS PASSED"
exit 0
