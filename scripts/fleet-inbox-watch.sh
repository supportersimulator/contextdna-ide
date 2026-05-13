#!/usr/bin/env bash
# fleet-inbox-watch.sh — Poll chief for messages addressed to this node.
#
# Runs in a loop. When messages arrive, writes them to:
#   /tmp/fleet-inbox-<node>.txt  (append log)
#   /tmp/fleet-inbox-NEW         (sentinel — Claude session hook reads this)
#
# Usage:
#   ./scripts/fleet-inbox-watch.sh              # auto-detects node from hostname
#   ./scripts/fleet-inbox-watch.sh mac2         # explicit node id
#   ./scripts/fleet-inbox-watch.sh --once       # single poll (for hooks/cron)
#
# Deploy as LaunchAgent: see scripts/fleet-inbox-watch.plist.example

CHIEF_URL="${CHIEF_INGEST_URL:-http://chief.local:8844}"
POLL_INTERVAL="${FLEET_POLL_INTERVAL:-15}"   # seconds

_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
source "$_REPO_ROOT/scripts/fleet-node-id.sh"

NODE="${1:-}"
ONCE=false
if [[ "$NODE" == "--once" ]]; then
    ONCE=true
    NODE=""
fi
[[ -z "$NODE" || "$NODE" == "--once" ]] && NODE=$(fleet_node_id)

INBOX_LOG="/tmp/fleet-inbox-${NODE}.txt"
SENTINEL="/tmp/fleet-inbox-NEW"

RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

_poll() {
    local resp
    resp=$(curl -sf "${CHIEF_URL}/inbox?node=${NODE}" 2>/dev/null) || return 1

    local count
    count=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])" 2>/dev/null)
    [[ -z "$count" || "$count" == "0" ]] && return 0

    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "" >> "$INBOX_LOG"
    echo "══════════════════════════════════" >> "$INBOX_LOG"
    echo "  Fleet Inbox — $ts" >> "$INBOX_LOG"
    echo "══════════════════════════════════" >> "$INBOX_LOG"

    echo "$resp" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data['messages']:
    print(f\"  From:     {m['from']}\")
    print(f\"  Subject:  {m['subject']}\")
    print(f\"  Priority: {m['priority']}\")
    print(f\"  Sent:     {m['sent_at']}\")
    print(f\"  Body:     {m['body']}\")
    print()
" | tee -a "$INBOX_LOG"

    # Write sentinel so Claude session hook can detect and surface messages
    echo "$resp" > "$SENTINEL"

    # Also print to stdout for live terminal display
    echo -e "\n${BOLD}${YELLOW}╔══════════════════════════════════╗${RESET}"
    echo -e "${BOLD}${YELLOW}║  Fleet Inbox — $count new message(s)  ║${RESET}"
    echo -e "${BOLD}${YELLOW}╚══════════════════════════════════╝${RESET}"
    echo "$resp" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data['messages']:
    pri = m['priority'].upper()
    color = '\033[0;31m' if pri == 'HIGH' else '\033[0;33m' if pri == 'NORMAL' else '\033[0;36m'
    reset = '\033[0m'
    print(f\"  {color}[{pri}]{reset} From {m['from']}: {m['subject']}\")
    print(f\"  {m['body']}\")
    print()
"
}

if $ONCE; then
    _poll
    exit 0
fi

echo -e "${CYAN}[fleet-inbox]${RESET} Watching inbox for ${BOLD}${NODE}${RESET} — polling ${CHIEF_URL} every ${POLL_INTERVAL}s"
while true; do
    _poll
    sleep "$POLL_INTERVAL"
done
