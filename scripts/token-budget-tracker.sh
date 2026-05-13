#!/bin/bash
# ============================================================================
# TOKEN BUDGET TRACKER — Claude Code weekly usage monitoring
# ============================================================================
# Reads Claude Code telemetry to estimate API consumption.
# Writes status to /tmp/token-budget-status.json for xbar/statusline.
#
# Context: Burned entire weekly budget in one session via 1-min cron loop.
# This provides visibility before hitting limits.
#
# Usage: ./scripts/token-budget-tracker.sh [--json] [--xbar]
# ============================================================================

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

OUTPUT_JSON=false
OUTPUT_XBAR=false
for arg in "$@"; do
    [[ "$arg" == "--json" ]] && OUTPUT_JSON=true
    [[ "$arg" == "--xbar" ]] && OUTPUT_XBAR=true
done

CLAUDE_DIR="$HOME/.claude"
TELEMETRY_DIR="$CLAUDE_DIR/telemetry"
STATUS_FILE="/tmp/token-budget-status.json"

# Weekly limit estimate (tokens). Adjust for your plan.
WEEKLY_LIMIT=${TOKEN_WEEKLY_LIMIT:-45000000}

# ── Parse telemetry for limits status ──
LIMITS_STATUS="unknown"
HOURS_TILL_RESET=""
FALLBACK_AVAILABLE=""

if [[ -d "$TELEMETRY_DIR" ]]; then
    # Find most recent limits event across telemetry files
    LATEST=$(python3 -c "
import json, glob, os
files = sorted(glob.glob('$TELEMETRY_DIR/1p_failed_events.*.json'), key=os.path.getmtime, reverse=True)
best = None
for f in files[:10]:
    try:
        for line in open(f):
            d = json.loads(line.strip())
            ed = d.get('event_data', {})
            if ed.get('event_name') == 'tengu_claudeai_limits_status_changed':
                meta = json.loads(ed.get('additional_metadata', '{}'))
                ts = ed.get('client_timestamp', '')
                if best is None or ts > best.get('ts', ''):
                    best = {'ts': ts, 'status': meta.get('status', 'unknown'),
                            'hours': meta.get('hoursTillReset', ''),
                            'fallback': meta.get('unifiedRateLimitFallbackAvailable', False)}
    except: pass
if best:
    print(json.dumps(best))
else:
    print('{}')
" 2>/dev/null)

    if [[ -n "$LATEST" && "$LATEST" != "{}" ]]; then
        LIMITS_STATUS=$(echo "$LATEST" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','unknown'))")
        HOURS_TILL_RESET=$(echo "$LATEST" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('hours',''))")
        FALLBACK_AVAILABLE=$(echo "$LATEST" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('fallback','false'))")
    fi
fi

# ── Count API calls this week ──
TURN_COUNT=$(python3 -c "
import json, glob, os, datetime
files = glob.glob('$TELEMETRY_DIR/1p_failed_events.*.json')
week_start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=datetime.datetime.now(datetime.timezone.utc).weekday())
week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
cutoff = week_start.isoformat()
count = 0
for f in files:
    try:
        for line in open(f):
            d = json.loads(line.strip())
            ed = d.get('event_data', {})
            if ed.get('event_name') == 'tengu_api_success':
                ts = ed.get('client_timestamp', '')
                if ts >= cutoff:
                    count += 1
    except: pass
print(count)
" 2>/dev/null || echo "0")

# ── Estimate tokens (avg ~50k input + 5k output per turn) ──
AVG_TOKENS_PER_TURN=55000
ESTIMATED_TOKENS=$((TURN_COUNT * AVG_TOKENS_PER_TURN))
if [[ $WEEKLY_LIMIT -gt 0 ]]; then
    USAGE_PCT=$((ESTIMATED_TOKENS * 100 / WEEKLY_LIMIT))
else
    USAGE_PCT=0
fi

# ── Determine budget level ──
if [[ $USAGE_PCT -ge 95 ]]; then
    LEVEL="CRITICAL"
    COLOR_EMOJI="🔴"
    COLOR_HEX="#FF0000"
elif [[ $USAGE_PCT -ge 80 ]]; then
    LEVEL="RED"
    COLOR_EMOJI="🟠"
    COLOR_HEX="#FF6600"
elif [[ $USAGE_PCT -ge 50 ]]; then
    LEVEL="YELLOW"
    COLOR_EMOJI="🟡"
    COLOR_HEX="#FFB800"
else
    LEVEL="GREEN"
    COLOR_EMOJI="🟢"
    COLOR_HEX="#00CC00"
fi

# ── Override with actual limits status if available ──
if [[ "$LIMITS_STATUS" == "blocked" ]]; then
    LEVEL="CRITICAL"
    COLOR_EMOJI="🔴"
    COLOR_HEX="#FF0000"
elif [[ "$LIMITS_STATUS" == "allowed_warning" && "$LEVEL" == "GREEN" ]]; then
    LEVEL="YELLOW"
    COLOR_EMOJI="🟡"
    COLOR_HEX="#FFB800"
fi

# ── Write status JSON ──
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > "$STATUS_FILE" <<ENDJSON
{
  "status": "$LEVEL",
  "limits_status": "$LIMITS_STATUS",
  "usage_pct": $USAGE_PCT,
  "estimated_tokens": $ESTIMATED_TOKENS,
  "weekly_limit": $WEEKLY_LIMIT,
  "turn_count": $TURN_COUNT,
  "hours_till_reset": "$HOURS_TILL_RESET",
  "fallback_available": $( [[ "$FALLBACK_AVAILABLE" == "true" ]] && echo "true" || echo "false" ),
  "timestamp": "$NOW"
}
ENDJSON

# ── Output ──
if $OUTPUT_JSON; then
    cat "$STATUS_FILE"
elif $OUTPUT_XBAR; then
    # xbar format: first line = menu bar, rest = dropdown
    echo "$COLOR_EMOJI CC ${USAGE_PCT}% | color=$COLOR_HEX"
    echo "---"
    echo "Token Budget: $LEVEL | color=$COLOR_HEX"
    echo "Usage: ${USAGE_PCT}% (~${ESTIMATED_TOKENS} tokens)"
    echo "Turns this week: $TURN_COUNT"
    echo "Weekly limit: $WEEKLY_LIMIT"
    [[ -n "$HOURS_TILL_RESET" ]] && echo "Reset in: ${HOURS_TILL_RESET}h"
    echo "API status: $LIMITS_STATUS"
    echo "---"
    if [[ "$LEVEL" == "CRITICAL" ]]; then
        echo "⚠️ STOP: Switch to Haiku or wait for reset | color=red"
    elif [[ "$LEVEL" == "RED" ]]; then
        echo "⚠️ Consider switching to cheaper model | color=orange"
    fi
    echo "Refresh | refresh=true"
else
    # Terminal output
    echo "$COLOR_EMOJI Token Budget: $LEVEL (${USAGE_PCT}%)"
    echo "  Estimated: ~$ESTIMATED_TOKENS / $WEEKLY_LIMIT tokens"
    echo "  Turns this week: $TURN_COUNT"
    [[ -n "$HOURS_TILL_RESET" ]] && echo "  Reset in: ${HOURS_TILL_RESET}h"
    echo "  API status: $LIMITS_STATUS"
    if [[ "$LEVEL" == "CRITICAL" || "$LEVEL" == "RED" ]]; then
        echo "  ⚠️  Consider switching to a cheaper model"
    fi
fi
