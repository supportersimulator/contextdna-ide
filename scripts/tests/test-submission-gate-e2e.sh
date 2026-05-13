#!/bin/bash
# ============================================================================
# T3 — End-to-end test for S5's competition-submission-gate
# ============================================================================
# Runs the gate against the synthetic-competition-001 test fixture and
# asserts:
#   1. All 8 named checks fire (gate emits a status line for each).
#   2. Exit code is 0 (the fixture is built to PASS).
#   3. EvidenceLedger row was written (kind="submission_gate" with our
#      submission_id as subject).
#   4. Audit-log row was appended to .fleet/audits/<date>-submission-gate.log.
#   5. After cleanup, our injected rows are removed:
#        - decisions.md: blocks containing the e2e marker excised (other
#          appenders' blocks preserved — robust to concurrent writers).
#        - .fleet/audits/<date>-submission-gate.log: JSONL lines containing
#          our submission_id stripped.
#        - production EvidenceLedger DB at ~/.fleet-nerve/evidence.db is
#          NEVER touched (we redirect HOME to a tempdir for the gate run).
#
# Reversibility (Constitutional Physics #5):
#   - Synthetic fixture lives under submissions/test-fixtures/ so a single
#     `rm -r` reverts.
#   - We redirect HOME for the gate so the production EvidenceLedger DB is
#     never opened.
#   - Audit log + decisions.md are truncated to their pre-run sizes.
#
# ZSF: every assertion is a hard-fail with stderr explanation. No silent
# `|| true` swallows.
#
# Exit:
#   0 — all assertions PASS, baseline restored.
#   1 — at least one assertion failed (state may be partially mutated; we
#       still attempt cleanup in the trap).
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
GATE="$REPO_DIR/scripts/competition-submission-gate.sh"
FIXTURE_DIR="$REPO_DIR/submissions/test-fixtures/synthetic-competition-001"
ARTIFACT="$FIXTURE_DIR/predictions.csv"
METADATA="$FIXTURE_DIR/metadata.json"

if [[ ! -x "$GATE" ]]; then
    echo "[e2e] FAIL: gate not executable: $GATE" >&2
    exit 1
fi
if [[ ! -f "$ARTIFACT" || ! -f "$METADATA" ]]; then
    echo "[e2e] FAIL: fixture missing — regenerating" >&2
    "$REPO_DIR/.venv/bin/python3" "$FIXTURE_DIR/generate.py" || {
        echo "[e2e] FAIL: fixture regeneration failed" >&2
        exit 1
    }
fi

SUBMISSION_ID=$("$REPO_DIR/.venv/bin/python3" -c \
    "import json; print(json.load(open('$METADATA'))['submission_id'])")
TODAY=$(date +%Y-%m-%d)
AUDIT_LOG="$REPO_DIR/.fleet/audits/${TODAY}-submission-gate.log"
DECISIONS_FILE="$REPO_DIR/.fleet/audits/${TODAY}-decisions.md"

mkdir -p "$REPO_DIR/.fleet/audits"

# ── Baseline snapshot ─────────────────────────────────────────────────────
# Strategy: do NOT cp the entire file back during cleanup — that would clobber
# concurrent appends from other processes (real chief decisions, demo loops,
# etc.). Instead, embed a unique marker in our injected content and excise
# ONLY blocks containing that marker during cleanup. Sizes are tracked for
# observability/diagnostic logging only.
BASELINE_AUDIT_SIZE=0
BASELINE_DECISIONS_SIZE=0
[[ -f "$AUDIT_LOG" ]]      && BASELINE_AUDIT_SIZE=$(wc -c < "$AUDIT_LOG" | tr -d ' ')
[[ -f "$DECISIONS_FILE" ]] && BASELINE_DECISIONS_SIZE=$(wc -c < "$DECISIONS_FILE" | tr -d ' ')

# Unique marker — embedded in BOTH the decisions.md signoff AND the audit
# log (via submission_id). cleanup() greps for these to remove only OUR rows.
E2E_MARKER="e2e-submission-gate-${SUBMISSION_ID}"

# ── Sandboxed HOME: redirect EvidenceLedger to a tempdir ──────────────────
SANDBOX_HOME=$(mktemp -d -t e2e-sub-gate-home-XXXXXX)
SANDBOX_LEDGER="$SANDBOX_HOME/.fleet-nerve/evidence.db"

PASS_COUNT=0
FAIL_COUNT=0

cleanup() {
    local rc=$?

    # decisions.md: excise only the block that contains our marker. A "block"
    # starts at "### " and runs until the next "### " or EOF. Using Python
    # for atomic rewrite — concurrent appenders that win the race after our
    # rewrite will simply append to the cleaned file.
    if [[ -f "$DECISIONS_FILE" ]]; then
        "$REPO_DIR/.venv/bin/python3" - "$DECISIONS_FILE" "$E2E_MARKER" <<'PY'
import sys, pathlib, re
path, marker = sys.argv[1], sys.argv[2]
text = pathlib.Path(path).read_text(encoding="utf-8")
# Split into blocks delimited by lines starting with "### ".
parts = re.split(r'(?m)^(?=### )', text)
kept = [p for p in parts if marker not in p]
out = "".join(kept)
# If we removed something, write back.
if out != text:
    pathlib.Path(path).write_text(out, encoding="utf-8")
    print(f"[cleanup] removed {len(parts)-len(kept)} block(s) from {path}", file=sys.stderr)
PY
    fi

    # Audit log: JSONL — strip lines containing our submission_id.
    if [[ -f "$AUDIT_LOG" ]]; then
        "$REPO_DIR/.venv/bin/python3" - "$AUDIT_LOG" "$SUBMISSION_ID" <<'PY'
import sys, pathlib
path, needle = sys.argv[1], sys.argv[2]
src = pathlib.Path(path)
lines = src.read_text(encoding="utf-8").splitlines(keepends=True)
kept = [l for l in lines if needle not in l]
removed = len(lines) - len(kept)
if removed:
    if kept:
        src.write_text("".join(kept), encoding="utf-8")
    else:
        src.unlink()
    print(f"[cleanup] removed {removed} audit line(s) from {path}", file=sys.stderr)
PY
    fi

    rm -rf "$SANDBOX_HOME"
    exit "$rc"
}
trap cleanup EXIT

assert() {
    local name="$1"; local cond="$2"
    if eval "$cond"; then
        echo "  PASS: $name"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo "  FAIL: $name" >&2
        echo "    cond: $cond" >&2
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

# ── Step 1: write a synthetic chief signoff for our submission_id ─────────
# Pattern matches what S5's gate looks for in *-decisions.md.
{
    printf '\n'
    printf '### C-submission-%s — ACCEPT\n' "$SUBMISSION_ID"
    printf -- '- ts: %s\n' "$(date +%s)"
    printf -- '- finding_ids: e2e-submission-gate-%s\n' "$SUBMISSION_ID"
    printf -- '- consensus: 1.00 (1 iter)\n'
    printf -- '- rationale: T3 e2e test fixture (synthetic, NOT a real signoff)\n'
} >> "$DECISIONS_FILE"

# ── Step 2: run the gate under sandboxed HOME ─────────────────────────────
# HOME redirect → EvidenceLedger writes to $SANDBOX_HOME/.fleet-nerve/evidence.db
# instead of the production ~/.fleet-nerve/evidence.db.
echo "=== Running gate (HOME=$SANDBOX_HOME) ==="
GATE_OUT=$(HOME="$SANDBOX_HOME" "$GATE" --artifact "$ARTIFACT" --metadata "$METADATA" 2>&1)
GATE_RC=$?
echo "$GATE_OUT"
echo "=== Gate exit rc=$GATE_RC ==="

# ── Step 3: assertions ────────────────────────────────────────────────────
assert "gate exit rc==0"                "[[ '$GATE_RC' -eq 0 ]]"

# All 8 named checks must appear in the gate's status output.
for check_name in artifact-exists metadata-schema determinism \
                  constitutional-signoff evidence-ledger leaderboard-guard \
                  no-secrets reversibility-path; do
    assert "check fired: $check_name" \
           "echo \"\$GATE_OUT\" | grep -q -- '$check_name'"
done

# Exactly 8 status lines appeared (defends against accidental drift).
STATUS_LINES=$(echo "$GATE_OUT" | grep -cE '^\s*\[(PASS|CRIT|WARN)\]')
assert "exactly 8 status lines (got $STATUS_LINES)" \
       "[[ '$STATUS_LINES' -eq 8 ]]"

# ── Step 4: EvidenceLedger row was written (sandboxed DB) ─────────────────
LEDGER_QUERY=$(HOME="$SANDBOX_HOME" PYTHONPATH="$REPO_DIR/multi-fleet" \
    "$REPO_DIR/.venv/bin/python3" -c "
from multifleet.evidence_ledger import EvidenceLedger
e = EvidenceLedger()
rows = e.query(event_type='submission_gate', limit=10)
hits = [r for r in rows if r.get('subject') == '$SUBMISSION_ID']
print('HITS=' + str(len(hits)))
print('TOTAL=' + str(e.stats['total']))
for h in hits[:1]:
    print('ENTRY_ID=' + h['entry_id'])
    print('CHAIN_VALID=' + str(e.verify_chain(limit=10)['valid']))
" 2>&1)
echo "$LEDGER_QUERY"
assert "evidence ledger has >=1 row for our submission_id" \
       "echo '$LEDGER_QUERY' | grep -qE 'HITS=[1-9]'"
assert "evidence ledger chain is valid"   \
       "echo '$LEDGER_QUERY' | grep -q 'CHAIN_VALID=True'"

# ── Step 5: audit-log row appeared ────────────────────────────────────────
assert "audit log file exists"            "[[ -f '$AUDIT_LOG' ]]"
assert "audit log grew (>= baseline+1 byte)" \
       "[[ \"\$(wc -c < '$AUDIT_LOG' | tr -d ' ')\" -gt '$BASELINE_AUDIT_SIZE' ]]"
assert "audit row references our submission_id" \
       "tail -5 '$AUDIT_LOG' | grep -q '$SUBMISSION_ID'"

# ── Step 6: production EvidenceLedger DB was NEVER opened ─────────────────
# We redirected HOME, so $HOME/.fleet-nerve/evidence.db should be untouched.
# We can't easily prove untouched on a multi-process system, but we CAN
# prove that the sandbox DB exists (i.e., the gate did write somewhere
# under the sandbox). If the sandbox DB is empty, that means the gate hit
# the production DB instead — we'd flag.
assert "sandbox evidence DB was created"  "[[ -f '$SANDBOX_LEDGER' ]]"
SANDBOX_TOTAL=$(HOME="$SANDBOX_HOME" PYTHONPATH="$REPO_DIR/multi-fleet" \
    "$REPO_DIR/.venv/bin/python3" -c "
from multifleet.evidence_ledger import EvidenceLedger
print(EvidenceLedger().stats['total'])
" 2>/dev/null)
assert "sandbox evidence DB has at least 1 row (gate wrote there, not prod)" \
       "[[ '${SANDBOX_TOTAL:-0}' -ge 1 ]]"

# ── Summary ───────────────────────────────────────────────────────────────
echo
echo "=== Pre-cleanup verification: $PASS_COUNT pass, $FAIL_COUNT fail ==="

# ── Step 7: cleanup runs in the EXIT trap; print expected post-state ─────
# The trap excises ONLY rows containing our marker — concurrent appends from
# other processes are preserved (does not clobber demo-surgeon-theater /
# real chief decisions written during our run).
echo "Post-cleanup expectations (handled by EXIT trap):"
echo "  decisions.md: blocks containing '$E2E_MARKER' will be excised"
echo "  audit log:    JSONL lines containing '$SUBMISSION_ID' will be removed"
echo "  sandbox HOME: $SANDBOX_HOME will be deleted"
echo "  baseline sizes (informational): audit=$BASELINE_AUDIT_SIZE decisions=$BASELINE_DECISIONS_SIZE"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
exit 0
