#!/bin/bash
# <xbar.title>Fleet Audit Watchdog</xbar.title>
# <xbar.version>v1.0.0</xbar.version>
# <xbar.desc>Trigger-based fleet audit detector — runs every 30s, only emits on STATE CHANGE</xbar.desc>
# <xbar.author>fleet</xbar.author>
#
# Modeled on fleet-docker-watchdog.30s.sh. Calls the one-shot audit-tick
# script. xbar handles the schedule (free, OS-native), this just runs the
# tick and renders a tiny menu-bar badge.
#
# Install: ln -sf "$REPO/scripts/xbar-fleet-audit-watchdog.30s.sh" \
#   "$HOME/Library/Application Support/xbar/plugins/fleet-audit-watchdog.30s.sh"

set -e

# Locate repo
REPO=""
for d in "$HOME/dev/er-simulator-superrepo" "$HOME/Documents/er-simulator-superrepo"; do
    [ -d "$d" ] && REPO="$d" && break
done
[ -z "$REPO" ] && {
    echo "🟠 audits: no repo"
    echo "---"
    echo "fleet-audit: superrepo not found in dev/ or Documents/"
    exit 0
}

VENV_PY="$REPO/multi-fleet/venv.nosync/bin/python3"
[ ! -x "$VENV_PY" ] && VENV_PY="/usr/bin/env python3"

TICK="$REPO/scripts/fleet-audit-tick.py"
TODAY=$(date +%Y-%m-%d)
FINDINGS_DOC="$REPO/.fleet/audits/${TODAY}-findings.md"
DECISIONS_DOC="$REPO/.fleet/audits/${TODAY}-decisions.md"
HALT_FLAG="$REPO/.fleet/HALT"
LAST_HASH_FILE="/tmp/fleet-audit-xbar-last-hash"

# Run tick in --no-consult mode from xbar (it ticks every 30s; consults
# happen in the daemon's NATS handler or in git hooks where the cost is
# bounded). xbar's job: detect change + render badge.
"$VENV_PY" "$TICK" --source xbar --no-consult --no-chief --json \
    > /tmp/fleet-audit-xbar-last.json 2>/dev/null || true

# Count findings by severity for badge
FINDINGS_JSON="/tmp/fleet-audit-xbar-last.json"
if [ -f "$FINDINGS_JSON" ]; then
    NEW_TOTAL=$(grep -c '"id":' "$FINDINGS_JSON" 2>/dev/null || echo "0")
else
    NEW_TOTAL=0
fi

# Total severity counts from today's findings doc (if any)
WARN_CT=0; CRIT_CT=0; EMERG_CT=0
if [ -f "$FINDINGS_DOC" ]; then
    WARN_CT=$(grep -c "^- \[WARN\]" "$FINDINGS_DOC" 2>/dev/null || echo "0")
    CRIT_CT=$(grep -c "^- \[CRITICAL\]" "$FINDINGS_DOC" 2>/dev/null || echo "0")
    EMERG_CT=$(grep -c "^- \[EMERGENCY\]" "$FINDINGS_DOC" 2>/dev/null || echo "0")
fi

# Badge color/state
if [ -f "$HALT_FLAG" ]; then
    BADGE="🛑 HALT"
    COLOR="red"
elif [ "$EMERG_CT" -gt 0 ]; then
    BADGE="🚨 ${EMERG_CT}E"
    COLOR="red"
elif [ "$CRIT_CT" -gt 0 ]; then
    BADGE="🔴 ${CRIT_CT}C/${WARN_CT}W"
    COLOR="red"
elif [ "$WARN_CT" -gt 0 ]; then
    BADGE="🟡 ${WARN_CT}W"
    COLOR="orange"
else
    BADGE="🟢 audits"
    COLOR="green"
fi

echo "$BADGE | color=$COLOR"
echo "---"
echo "Fleet Audit (today)"
echo "WARN: $WARN_CT · CRITICAL: $CRIT_CT · EMERGENCY: $EMERG_CT"
echo "New this tick: $NEW_TOTAL"
[ -f "$HALT_FLAG" ] && echo "⛔ HALT active — see $HALT_FLAG"
echo "---"
[ -f "$FINDINGS_DOC" ] && echo "📋 Findings | href=file://$FINDINGS_DOC"
[ -f "$DECISIONS_DOC" ] && echo "⚖️  Decisions | href=file://$DECISIONS_DOC"
echo "---"
echo "Run tick now | bash='$VENV_PY' param1='$TICK' param2='--source' param3='xbar-manual' terminal=false refresh=true"
echo "Refresh | refresh=true"
