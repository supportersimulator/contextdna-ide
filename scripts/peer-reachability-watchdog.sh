#!/usr/bin/env bash
# peer-reachability-watchdog.sh — periodic trigger (cron/launchd or /loop).
# Reads /tmp/fleet-daemon-fail-<peer>.count (incremented by smart router on
# channel-fail) or falls back to a fresh probe. Auto-fires recovery when
# fail-count > N within T window. ZSF: every action logged.
set -uo pipefail
THRESHOLD="${PEER_FAIL_THRESHOLD:-3}"
LOG=/tmp/peer-reachability-watchdog.log
echo "[$(date -u +%FT%TZ)] tick" >> "$LOG"
for PEER in mac1 mac3; do
  CNT_FILE="/tmp/fleet-daemon-fail-${PEER}.count"
  CNT=$(cat "$CNT_FILE" 2>/dev/null || echo 0)
  if (( CNT >= THRESHOLD )) || ! bash "$(dirname "$0")/probe-peer-reachability.sh" "$PEER" >/dev/null 2>&1; then
    echo "[$(date -u +%FT%TZ)] $PEER degraded (fail=$CNT) → recovery" >> "$LOG"
    bash "$(dirname "$0")/recover-peer-reachability.sh" "$PEER" >> "$LOG" 2>&1 || true
    : > "$CNT_FILE"
  fi
done
