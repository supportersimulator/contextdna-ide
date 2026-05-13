#!/usr/bin/env bash
# Webhook Watchdog (Round-6 F5)
# =============================================================================
# Passive read-only plateau detector for /health.webhook.events_recorded.
#
# Complements scripts/webhook-e2e-test.sh (active trigger) — this script
# never publishes, only observes. Designed for cron / xbar.
#
# Behaviour:
#   - Reads /health.webhook on every run, persists last counter + ts to
#     /tmp/webhook-watchdog.state
#   - Alerts (exit 2 + stderr) if counter has not advanced in N seconds
#     (default 600 = 10 min)
#   - Alerts (exit 3 + stderr) if daemon /health unreachable
#   - Exit 0 = healthy + advancing (or first run baseline written)
#   - Exit 1 = CLI/usage error
#
# Usage:
#   scripts/webhook-watchdog.sh                 # default 600s plateau threshold
#   scripts/webhook-watchdog.sh --threshold 300 # 5 min plateau
#   scripts/webhook-watchdog.sh --json          # emit JSON status to stdout
#
# Why this is read-only:
#   - Active probes (publish + check) belong in webhook-e2e-test.sh
#   - This watchdog can run every minute without affecting webhook_publish_*
#     counters or risking budget_exceeded fires
# =============================================================================

set -euo pipefail

THRESHOLD_S=600
EMIT_JSON=0
HEALTH_URL="${FLEET_HEALTH_URL:-http://127.0.0.1:8855/health}"
STATE_FILE="${WEBHOOK_WATCHDOG_STATE:-/tmp/webhook-watchdog.state}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --threshold) THRESHOLD_S="$2"; shift 2 ;;
    --json)      EMIT_JSON=1; shift ;;
    --state)     STATE_FILE="$2"; shift 2 ;;
    --url)       HEALTH_URL="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

# Robust health fetch — daemon /health occasionally serves schema-form during
# warm-up (observed 2026-05-04). Retry up to 5x to dodge that race.
fetch_health() {
  for _ in 1 2 3 4 5; do
    raw=$(curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null || true)
    if [[ -n "$raw" ]] && echo "$raw" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
      printf '%s' "$raw"
      return 0
    fi
    sleep 1
  done
  return 1
}

now=$(date +%s)
raw=$(fetch_health) || {
  msg="webhook-watchdog: /health unreachable at $HEALTH_URL"
  if [[ $EMIT_JSON -eq 1 ]]; then
    printf '{"status":"unreachable","url":"%s","ts":%d}\n' "$HEALTH_URL" "$now"
  else
    echo "$msg" >&2
  fi
  exit 3
}

current=$(printf '%s' "$raw" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d['webhook']['events_recorded'])")
last_age=$(printf '%s' "$raw" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); v=d['webhook'].get('last_webhook_age_s'); print(v if v is not None else -1)")

prev_count=""
prev_ts=""
if [[ -f "$STATE_FILE" ]]; then
  # state format: COUNT TS  (single line, space-separated)
  read -r prev_count prev_ts < "$STATE_FILE" || true
fi

verdict="advancing"
plateau_s=0
if [[ -n "$prev_count" && -n "$prev_ts" ]]; then
  if [[ "$current" == "$prev_count" ]]; then
    plateau_s=$(( now - prev_ts ))
    if (( plateau_s >= THRESHOLD_S )); then
      verdict="plateau"
    else
      verdict="quiet"   # same count but under threshold, normal between events
    fi
  else
    # advanced — refresh baseline
    printf '%s %s\n' "$current" "$now" > "$STATE_FILE"
  fi
else
  # first run — write baseline, treat as healthy
  printf '%s %s\n' "$current" "$now" > "$STATE_FILE"
  verdict="baseline"
fi

if [[ $EMIT_JSON -eq 1 ]]; then
  printf '{"status":"%s","events_recorded":%s,"last_webhook_age_s":%s,"plateau_s":%d,"threshold_s":%d,"ts":%d}\n' \
    "$verdict" "$current" "$last_age" "$plateau_s" "$THRESHOLD_S" "$now"
else
  echo "webhook-watchdog: status=$verdict events=$current last_age=${last_age}s plateau=${plateau_s}s threshold=${THRESHOLD_S}s"
fi

case "$verdict" in
  plateau) exit 2 ;;
  *)       exit 0 ;;
esac
