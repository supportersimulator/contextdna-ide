#!/bin/bash
# <xbar.title>Webhook Watchdog</xbar.title>
# <xbar.version>v1.0.0</xbar.version>
# <xbar.desc>5min webhook plateau detector — wraps webhook-watchdog.sh --json</xbar.desc>
# <xbar.author>fleet (G5)</xbar.author>
#
# Wraps scripts/webhook-watchdog.sh (Round-6 F5) into xbar.
#   🟢 advancing/baseline — events flowing
#   🟡 plateau/quiet     — counter hasn't advanced (≥10min = plateau)
#   🔴 unreachable       — daemon /health down (ZSF: never silent)
#
# Install: bash scripts/install-webhook-watchdog.sh

set -e

REPO=""
for d in "$HOME/dev/er-simulator-superrepo" "$HOME/Documents/er-simulator-superrepo"; do
    [ -d "$d" ] && REPO="$d" && break
done
[ -z "$REPO" ] && {
    echo "🔴 webhook: no repo"
    echo "---"
    echo "superrepo not found"
    exit 0
}

WATCHDOG="$REPO/scripts/webhook-watchdog.sh"
[ ! -x "$WATCHDOG" ] && chmod +x "$WATCHDOG" 2>/dev/null || true

# 600s plateau threshold (10min). xbar reruns every 5min so first plateau
# alert lands ~10min after counter freezes.
RAW=$("$WATCHDOG" --json --threshold 600 2>/dev/null || true)
EXIT=$?

# Default fields
STATUS="unknown"
EVENTS="?"
LAST_AGE="?"
PLATEAU="0"
THRESHOLD="600"

if [ -n "$RAW" ]; then
    parse() { echo "$RAW" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1','?'))" 2>/dev/null || echo "?"; }
    STATUS=$(parse status)
    EVENTS=$(parse events_recorded)
    LAST_AGE=$(parse last_webhook_age_s)
    PLATEAU=$(parse plateau_s)
    THRESHOLD=$(parse threshold_s)
fi

case "$STATUS" in
    unreachable)
        BADGE="🔴 webhook"; COLOR="red"; LABEL="daemon /health unreachable" ;;
    plateau)
        BADGE="🟡 webhook"; COLOR="orange"; LABEL="PLATEAU ${PLATEAU}s ≥ ${THRESHOLD}s" ;;
    quiet)
        BADGE="🟢 webhook"; COLOR="green"; LABEL="quiet ${PLATEAU}s (under ${THRESHOLD}s)" ;;
    advancing)
        BADGE="🟢 webhook"; COLOR="green"; LABEL="advancing — events=$EVENTS" ;;
    baseline)
        BADGE="🟢 webhook"; COLOR="green"; LABEL="baseline written" ;;
    *)
        BADGE="🔴 webhook"; COLOR="red"; LABEL="status=$STATUS (parse fail)" ;;
esac

echo "$BADGE | color=$COLOR"
echo "---"
echo "Webhook Watchdog (5min)"
echo "$LABEL"
echo "events_recorded: $EVENTS"
echo "last_webhook_age_s: $LAST_AGE"
echo "plateau_s: $PLATEAU / threshold ${THRESHOLD}s"
echo "---"
echo "Run watchdog now | bash='$WATCHDOG' param1='--threshold' param2='600' terminal=true refresh=true"
echo "Refresh | refresh=true"
