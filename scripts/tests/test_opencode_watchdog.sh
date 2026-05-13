#!/usr/bin/env bash
# test_opencode_watchdog.sh — Unit tests for opencode-watchdog.sh
#
# Tests:
#   1. --dry-run identifies a long-running process matching OPENCODE_PATTERN
#      but does NOT kill it.
#   2. --apply with short MAX_AGE_HOURS + short GRACE_SECONDS kills the matching
#      process.
#   3. A "young" process (just spawned, below MAX_AGE_HOURS) is left alone.
#   4. Counter file is updated on --apply.
#
# Strategy: spawn `sleep 9999` workers whose argv contains a sentinel string,
# point OPENCODE_PATTERN at that sentinel, and verify watchdog behavior.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG="$SCRIPT_DIR/../opencode-watchdog.sh"

if [[ ! -x "$WATCHDOG" ]]; then
    echo "FAIL: watchdog not executable: $WATCHDOG"
    exit 1
fi

PASS=0
FAIL=0

note_pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
note_fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

# Sentinel that mimics "opencode --prompt" without colliding with real procs.
# We point OPENCODE_PATTERN at the unique tag so we don't catch real opencode.
TAG="FAKETEST_$$_$(date +%s)"
SENTINEL="opencode --prompt $TAG"
TMP_DIR="$(mktemp -d -t opencode-watchdog-test.XXXXXX)"
LOG_FILE="$TMP_DIR/watchdog.log"
STATS_FILE="$TMP_DIR/stats.json"

# macOS strategy for fake workers: use `sh -c 'sleep 9999; true' opencode --prompt TAG`.
# The string after `-c` is the script; the next positional args (starting with
# argv[0]) become the script's $0, $1, ... `ps -axo command` shows them all, so
# the watchdog's substring match against "opencode --prompt TAG" hits.
# `exec -a` and shebang wrappers both lose the argv on macOS. The trailing
# `; true` prevents sh from optimizing into a direct `exec sleep`.

cleanup() {
    # Kill any surviving test workers (regardless of exit path). Use the
    # generic FAKETEST_ prefix so leaks from earlier test runs are reaped too.
    pkill -KILL -f "FAKETEST_" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# Pre-test cleanup — reap any leftovers from previously botched runs so this
# test sees a clean environment.
pkill -KILL -f "FAKETEST_" 2>/dev/null || true
sleep 1

# Spawn a worker. `ps` shows it as "sh -c sleep 9999 opencode --prompt TAG"
# which contains our sentinel as a substring.
#
# CRITICAL: redirect stdin/stdout/stderr so `$(spawn_worker)` returns immediately.
# Without this, bash command substitution waits for the bg process to close fd1,
# which would never happen (the bg process is the long-lived `sleep`).
spawn_worker() {
    # Trailing `; true` prevents `sh` from optimizing into a direct `exec sleep`
    # (which would replace argv and erase our sentinel from `ps` output).
    sh -c 'sleep 9999; true' opencode --prompt "$TAG" </dev/null >/dev/null 2>&1 &
    echo $!
}

# Helper to invoke watchdog with our isolated state.
run_watchdog() {
    OPENCODE_PATTERN="$SENTINEL" \
    OPENCODE_WATCHDOG_LOG="$LOG_FILE" \
    OPENCODE_WATCHDOG_STATS="$STATS_FILE" \
    "$@"
}

# ---- Test 1: dry-run identifies stale worker ----
echo "=== Test 1: dry-run identifies stale worker ==="
W1="$(spawn_worker)"
sleep 2  # let it accumulate ~2s of etime
# MAX_AGE_HOURS=0 means "any process is stale" (max_age_secs=0).
DRY_OUT="$(OPENCODE_MAX_AGE_HOURS=0 run_watchdog "$WATCHDOG" --dry-run 2>&1)"
echo "$DRY_OUT" | sed 's/^/    /'

if echo "$DRY_OUT" | grep -q "DRY-RUN: would SIGTERM pid=$W1"; then
    note_pass "dry-run identifies stale worker pid=$W1"
else
    note_fail "dry-run did NOT identify stale worker pid=$W1"
fi

if kill -0 "$W1" 2>/dev/null; then
    note_pass "dry-run left worker alive"
else
    note_fail "dry-run wrongly killed worker"
fi

# ---- Test 2: --apply with short grace kills the worker ----
echo ""
echo "=== Test 2: --apply kills stale worker ==="
APPLY_OUT="$(OPENCODE_MAX_AGE_HOURS=0 OPENCODE_GRACE_SECONDS=3 run_watchdog "$WATCHDOG" --apply 2>&1)"
echo "$APPLY_OUT" | sed 's/^/    /'

# Give SIGTERM a moment to land (sleep handles SIGTERM cleanly).
sleep 1
if kill -0 "$W1" 2>/dev/null; then
    note_fail "--apply did NOT kill worker pid=$W1"
    kill -9 "$W1" 2>/dev/null || true
else
    note_pass "--apply killed worker pid=$W1"
fi

if echo "$APPLY_OUT" | grep -q "SIGTERM sent: pid=$W1"; then
    note_pass "log records SIGTERM"
else
    note_fail "log missing SIGTERM record"
fi

# ---- Test 3: counter file updated ----
echo ""
echo "=== Test 3: counter file updated ==="
if [[ -f "$STATS_FILE" ]] && grep -q '"sigterm_count":[ ]*[1-9]' "$STATS_FILE"; then
    note_pass "sigterm_count bumped in stats file"
    cat "$STATS_FILE" | sed 's/^/    /'
else
    note_fail "stats file missing or sigterm_count not bumped"
    [[ -f "$STATS_FILE" ]] && cat "$STATS_FILE" | sed 's/^/    /'
fi

# ---- Test 4: young process (above MAX_AGE_HOURS=1) untouched ----
echo ""
echo "=== Test 4: young process untouched ==="
W4="$(spawn_worker)"
sleep 1
# MAX_AGE_HOURS=1 means worker must be >= 3600s old. It's ~1s old, so safe.
YOUNG_OUT="$(OPENCODE_MAX_AGE_HOURS=1 OPENCODE_GRACE_SECONDS=3 run_watchdog "$WATCHDOG" --apply 2>&1)"
echo "$YOUNG_OUT" | sed 's/^/    /'

if echo "$YOUNG_OUT" | grep -q "no stale opencode --prompt PIDs found"; then
    note_pass "young worker correctly skipped"
else
    note_fail "watchdog flagged young worker as stale"
fi

if kill -0 "$W4" 2>/dev/null; then
    note_pass "young worker still alive after --apply"
    kill -9 "$W4" 2>/dev/null || true
else
    note_fail "young worker was killed despite being below MAX_AGE_HOURS"
fi

# ---- Results ----
echo ""
echo "=== Results ==="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
