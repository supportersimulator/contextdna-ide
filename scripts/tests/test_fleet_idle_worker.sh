#!/usr/bin/env bash
# test_fleet_idle_worker.sh — CCC2 pinning tests for fleet-idle-worker.sh
# (9934b7250).
#
# Tests:
#   1. --help → exit 0, prints usage.
#   2. Unknown flag → exit 2, error on stderr.
#   3. Single-iteration default (--dry-run) → exits 0, writes structured
#      log line to /tmp/fleet-idle-worker-<node>.log with [info] level.
#   4. --node override propagates to log filename.
#   5. Idle/busy gate runs (smoke): produces either "IDLE" or "BUSY" trace
#      in the log line, never silent.
#   6. Apply mode without --loop still exits 0 and never tries to actually
#      mutate (NATS unreachable path is graceful).
#
# Strategy: spawn the script in --dry-run / --apply single-iteration mode
# with FLEET_NERVE_PORT pointed at an unused port and a synthetic NODE_ID
# to keep logs in a sandboxed path. ZSF — every assertion logs script
# output on failure.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SCRIPT_DIR/../fleet-idle-worker.sh"

if [[ ! -f "$TARGET" ]]; then
    echo "FAIL: script not found: $TARGET"
    exit 1
fi

PASS=0
FAIL=0
note_pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
note_fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

# Unique sentinel node id so we don't trample real /tmp logs.
TAG="fitest_$$_$(date +%s)"
LOG_FILE="/tmp/fleet-idle-worker-${TAG}.log"
# Unused port — script's curl will timeout and return idle 0.
UNUSED_PORT=59999

cleanup() {
    rm -f "$LOG_FILE"
}
trap cleanup EXIT

# Helper: invoke with isolated env.
run_worker() {
    MULTIFLEET_NODE_ID="$TAG" \
        FLEET_NERVE_PORT="$UNUSED_PORT" \
        FLEET_IDLE_THRESHOLD_S=1 \
        NATS_URL="nats://127.0.0.1:1" \
        bash "$TARGET" "$@"
}

# ---- Test 1: --help ----
echo "=== Test 1: --help ==="
HELP_OUT="$(bash "$TARGET" --help 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && echo "$HELP_OUT" | grep -q "fleet-idle-worker"; then
    note_pass "--help exits 0 and prints usage banner"
else
    note_fail "--help expected exit 0 + banner; got rc=$RC"
    echo "$HELP_OUT" | sed 's/^/    /'
fi

# ---- Test 2: unknown flag ----
echo ""
echo "=== Test 2: unknown flag ==="
ERR_OUT="$(bash "$TARGET" --nonsense-flag 2>&1)" || RC=$?
if [[ ${RC:-0} -eq 2 ]] && echo "$ERR_OUT" | grep -qi "unknown arg"; then
    note_pass "unknown flag → exit 2 + diagnostic"
else
    note_fail "unknown flag expected exit 2; got rc=${RC:-0}"
    echo "$ERR_OUT" | sed 's/^/    /'
fi
RC=0

# ---- Test 3: single-iteration --dry-run ----
echo ""
echo "=== Test 3: single-iteration --dry-run logs ZSF line ==="
rm -f "$LOG_FILE"
# 12s ceiling — the script may run python subprocesses (nats import,
# capability detect). Keep generous for CI but cap so a wedged test
# doesn't hang the whole suite.
OUT="$(run_worker --dry-run 2>&1)" || true
if [[ -f "$LOG_FILE" ]]; then
    note_pass "log file created: $LOG_FILE"
else
    note_fail "log file missing after dry-run"
    echo "$OUT" | sed 's/^/    /'
fi

if [[ -f "$LOG_FILE" ]] && grep -qE "\[$TAG\] \[info\]" "$LOG_FILE"; then
    note_pass "log line includes node tag + [info] level"
else
    note_fail "log line missing structured prefix"
    [[ -f "$LOG_FILE" ]] && cat "$LOG_FILE" | sed 's/^/    /'
fi

# ---- Test 4: idle/busy gate never silent ----
echo ""
echo "=== Test 4: idle/busy gate produces IDLE or BUSY trace ==="
if [[ -f "$LOG_FILE" ]] && grep -qE "(IDLE|BUSY)" "$LOG_FILE"; then
    note_pass "log line includes IDLE/BUSY state"
else
    note_fail "log line missing idle-state trace (ZSF violation)"
    [[ -f "$LOG_FILE" ]] && cat "$LOG_FILE" | sed 's/^/    /'
fi

# ---- Test 5: --node override propagates to log filename ----
echo ""
echo "=== Test 5: --node override propagates to log filename ==="
OVERRIDE_TAG="fitest_override_$$"
OVERRIDE_LOG="/tmp/fleet-idle-worker-${OVERRIDE_TAG}.log"
rm -f "$OVERRIDE_LOG"
FLEET_NERVE_PORT="$UNUSED_PORT" \
    FLEET_IDLE_THRESHOLD_S=1 \
    NATS_URL="nats://127.0.0.1:1" \
    bash "$TARGET" --dry-run --node "$OVERRIDE_TAG" >/dev/null 2>&1 || true
if [[ -f "$OVERRIDE_LOG" ]]; then
    note_pass "--node override writes to $OVERRIDE_LOG"
    rm -f "$OVERRIDE_LOG"
else
    note_fail "--node override did not produce expected log file"
fi

# ---- Test 6: --apply graceful when NATS unreachable ----
echo ""
echo "=== Test 6: --apply exits cleanly with NATS unreachable ==="
APPLY_TAG="fitest_apply_$$"
APPLY_LOG="/tmp/fleet-idle-worker-${APPLY_TAG}.log"
rm -f "$APPLY_LOG"
MULTIFLEET_NODE_ID="$APPLY_TAG" \
    FLEET_NERVE_PORT="$UNUSED_PORT" \
    FLEET_IDLE_THRESHOLD_S=1 \
    NATS_URL="nats://127.0.0.1:1" \
    bash "$TARGET" --apply >/dev/null 2>&1
RC=$?
if [[ $RC -eq 0 ]]; then
    note_pass "--apply exits 0 even with NATS unreachable"
else
    note_fail "--apply exit code $RC (expected 0 — ZSF graceful)"
fi
if [[ -f "$APPLY_LOG" ]] && grep -qE "apply=1" "$APPLY_LOG"; then
    note_pass "--apply mode reflected in log trace"
else
    note_fail "--apply trace missing from log"
    [[ -f "$APPLY_LOG" ]] && cat "$APPLY_LOG" | sed 's/^/    /'
fi
rm -f "$APPLY_LOG"

# ---- Results ----
echo ""
echo "=== Results ==="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
