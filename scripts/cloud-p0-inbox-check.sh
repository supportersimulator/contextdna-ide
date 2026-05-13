#!/usr/bin/env bash
# cloud-p0-inbox-check.sh — Guard for the cloud node's P0 inbox-check task.
#
# WHY
# ----
# 2026-05-12 VV4 found cloud ran the same "P0 inbox check" three times in
# 3 hours (commits f00310fbe → 1dcd75da4 → 2229e61bd → b532457bc → 964968499)
# producing no signal mac1 consumed. Pure mailbox-only nodes should NOT
# spend a Claude turn + git commit on "inbox empty, plans unshipped" when
# nothing changed since the last report.
#
# CONTRACT
# --------
# This script is the GATE the cloud Claude session must call BEFORE
# producing a P0 status report. It enforces two invariants:
#
#   1. Idempotency — exit non-zero (skip) if the inbox state hash is
#      unchanged since the last successful run.
#   2. Cooldown   — exit non-zero (skip) if the previous successful run
#      was less than COOLDOWN_S ago (default 6h).
#
# If both gates pass, the script prints "PROCEED" on stdout and updates
# the state file; the caller may then write its status report and commit.
#
# Exit codes:
#   0 — PROCEED. State changed AND cooldown elapsed.
#   1 — SKIP idempotent (state unchanged).
#   2 — SKIP cooldown (recent run, even though state may have changed).
#   3 — error (unreadable state dir, write failed, etc.). ZSF counter bump.
#
# ENV
# ---
#   CLOUD_P0_REPO_ROOT       — repo root (default $HOME/dev/er-simulator-superrepo)
#   CLOUD_P0_COOLDOWN_S      — min seconds between runs (default 21600 = 6h)
#   CLOUD_P0_STATE_FILE      — last-run state cache (default /tmp/cloud-p0-state.json)
#   CLOUD_P0_COUNTERS_FILE   — counter log (default /tmp/cloud-p0-counters.txt)
#   CLOUD_P0_DRY_RUN         — if "1", do not update state file
#
# Zero Silent Failures: every skip increments a named counter in
# CLOUD_P0_COUNTERS_FILE. Aaron can grep that file to see how often the
# throttle saved a redundant commit.

set -u

REPO_ROOT="${CLOUD_P0_REPO_ROOT:-$HOME/dev/er-simulator-superrepo}"
COOLDOWN_S="${CLOUD_P0_COOLDOWN_S:-21600}"
STATE_FILE="${CLOUD_P0_STATE_FILE:-/tmp/cloud-p0-state.json}"
COUNTERS_FILE="${CLOUD_P0_COUNTERS_FILE:-/tmp/cloud-p0-counters.txt}"
DRY_RUN="${CLOUD_P0_DRY_RUN:-0}"

# ── Counter bump (ZSF) ───────────────────────────────────────────────────
bump_counter() {
    local name="$1"
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # Append-only log. Aggregation: `grep -c '^cloud_p0_skipped_idempotent_total' ...`
    printf '%s %s\n' "$name" "$ts" >> "$COUNTERS_FILE" 2>/dev/null || true
}

# ── Snapshot inbox state ─────────────────────────────────────────────────
# Hash = sha256 of (inbox file list + mtimes + open-plan file paths).
# Catches: new fleet messages, archived messages, new docs/plans entries,
# changes inside .fleet-messages/*. Misses: in-place edits with same mtime
# — acceptable because the listener catches those via NATS events.
compute_state_hash() {
    local root="$1"
    if [ ! -d "$root" ]; then
        return 1
    fi
    {
        # All inboxes (mac1, mac2, mac3, cloud, all)
        find "$root/.fleet-messages" -maxdepth 2 -type f \
            ! -path '*/archive/*' \
            -printf '%p %T@\n' 2>/dev/null \
            || find "$root/.fleet-messages" -maxdepth 2 -type f \
                ! -path '*/archive/*' \
                -exec stat -f '%N %m' {} + 2>/dev/null
        # Plans dir — change in plan inventory invalidates "0% implemented" copy
        find "$root/docs/plans" -maxdepth 1 -type f -name '*.md' \
            -printf '%p %T@\n' 2>/dev/null \
            || find "$root/docs/plans" -maxdepth 1 -type f -name '*.md' \
                -exec stat -f '%N %m' {} + 2>/dev/null
        # Seed files in /tmp
        find /tmp -maxdepth 1 -type f -name 'fleet-seed-*.md' \
            -printf '%p %T@\n' 2>/dev/null \
            || find /tmp -maxdepth 1 -type f -name 'fleet-seed-*.md' \
                -exec stat -f '%N %m' {} + 2>/dev/null
    } | LC_ALL=C sort | shasum -a 256 | cut -d' ' -f1
}

CURRENT_HASH="$(compute_state_hash "$REPO_ROOT" || echo '')"
if [ -z "$CURRENT_HASH" ]; then
    bump_counter "cloud_p0_state_hash_error_total"
    echo "ERROR: cannot compute inbox state hash (repo_root=$REPO_ROOT)" >&2
    exit 3
fi

# ── Read previous state ──────────────────────────────────────────────────
PREV_HASH=""
PREV_TS=0
if [ -f "$STATE_FILE" ]; then
    PREV_HASH="$(python3 -c "
import json, sys
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('hash', ''))
except Exception:
    pass
" 2>/dev/null || echo '')"
    PREV_TS="$(python3 -c "
import json
try:
    d = json.load(open('$STATE_FILE'))
    print(int(d.get('ts', 0)))
except Exception:
    print(0)
" 2>/dev/null || echo 0)"
fi

NOW="$(date +%s)"
ELAPSED=$(( NOW - PREV_TS ))

# ── Gate 1: idempotency ──────────────────────────────────────────────────
if [ -n "$PREV_HASH" ] && [ "$PREV_HASH" = "$CURRENT_HASH" ]; then
    bump_counter "cloud_p0_skipped_idempotent_total"
    echo "SKIP idempotent: inbox state unchanged since $(date -r "$PREV_TS" 2>/dev/null || echo unknown)"
    echo "  hash=$CURRENT_HASH"
    echo "  elapsed=${ELAPSED}s"
    exit 1
fi

# ── Gate 2: cooldown ─────────────────────────────────────────────────────
if [ "$PREV_TS" -gt 0 ] && [ "$ELAPSED" -lt "$COOLDOWN_S" ]; then
    bump_counter "cloud_p0_skipped_cooldown_total"
    REMAINING=$(( COOLDOWN_S - ELAPSED ))
    echo "SKIP cooldown: ${ELAPSED}s elapsed < ${COOLDOWN_S}s cooldown"
    echo "  next eligible in ${REMAINING}s"
    echo "  (state hash DID change — will fire on next call after cooldown)"
    exit 2
fi

# ── PROCEED ──────────────────────────────────────────────────────────────
echo "PROCEED: state changed (hash $PREV_HASH → $CURRENT_HASH) and cooldown elapsed (${ELAPSED}s)"

if [ "$DRY_RUN" = "1" ]; then
    bump_counter "cloud_p0_proceed_dry_run_total"
    echo "  DRY-RUN: not updating state file"
    exit 0
fi

# Atomic write — temp + rename.
TMP_FILE="${STATE_FILE}.tmp.$$"
cat > "$TMP_FILE" <<EOF
{
  "hash": "$CURRENT_HASH",
  "ts": $NOW,
  "iso": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "node": "cloud",
  "cooldown_s": $COOLDOWN_S
}
EOF
if mv "$TMP_FILE" "$STATE_FILE" 2>/dev/null; then
    bump_counter "cloud_p0_proceed_total"
    exit 0
else
    rm -f "$TMP_FILE" 2>/dev/null || true
    bump_counter "cloud_p0_state_write_error_total"
    echo "ERROR: state file write failed ($STATE_FILE)" >&2
    exit 3
fi
