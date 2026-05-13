#!/usr/bin/env bash
# fleet-monitor-cron.sh — Standalone fleet monitoring (NO Claude involvement)
#
# This runs via system crontab, NOT through Claude Code.
# Saves ~14,300 tokens per tick that would otherwise be wasted.
#
# Install: crontab -e → add:
#   */5 * * * * /path/to/er-simulator-superrepo/scripts/fleet-monitor-cron.sh
#
# Outputs to log file. Only alerts Claude (via seed file) on REAL anomalies.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="/tmp/fleet-monitor.log"
ALERT_FILE="/tmp/fleet-monitor-alert"
ALERT_COOLDOWN=300  # Only alert Claude once per 5 minutes

# Run fleet-check silently
OUTPUT=$(bash "$SCRIPT_DIR/fleet-check.sh" 2>&1) || true

# Log
echo "[$(date '+%Y-%m-%d %H:%M:%S')] $OUTPUT" >> "$LOG"

# Detect anomalies worth alerting Claude about
ALERT=""
if echo "$OUTPUT" | grep -q "FAIL\|down\|0/3\|1/3"; then
    ALERT="Fleet degraded: $(echo "$OUTPUT" | grep -oE '[0-9]/3|FAIL|down' | head -3 | tr '\n' ' ')"
fi

# Only write alert seed file if anomaly detected AND cooldown expired
if [ -n "$ALERT" ]; then
    if [ -f "$ALERT_FILE" ]; then
        LAST=$(cat "$ALERT_FILE" 2>/dev/null || echo 0)
        NOW=$(date +%s)
        if [ $((NOW - LAST)) -lt $ALERT_COOLDOWN ]; then
            exit 0  # Cooldown active, don't spam Claude
        fi
    fi
    date +%s > "$ALERT_FILE"
    # Write seed file that fleet-inbox-hook will pick up on next USER interaction
    echo "## [ALERT] Fleet Monitor" > "/tmp/fleet-seed-monitor.md"
    echo "" >> "/tmp/fleet-seed-monitor.md"
    echo "$ALERT" >> "/tmp/fleet-seed-monitor.md"
fi
