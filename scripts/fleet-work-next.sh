#!/usr/bin/env bash
# fleet-work-next.sh — Pick and display the next unclaimed work item
#
# Used by sessions to auto-pick tasks from the backlog.
# Silent if no work available.

PORT="${FLEET_NERVE_PORT:-8855}"

RESP=$(curl -sf "http://127.0.0.1:${PORT}/work/next" 2>/dev/null) || exit 0

AVAILABLE=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('available', False))" 2>/dev/null)

[ "$AVAILABLE" != "True" ] && exit 0

echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)['item']
print(f'📋 Next work item available:')
print(f'  P{d.get(\"priority\", \"?\")} — {d.get(\"title\", \"?\")}')
print(f'  Claim with: curl -X POST localhost:${PORT}/work/start -H \"Content-Type: application/json\" -d \'{{\"title\": \"{d.get(\"title\",\"\")}\", \"node_id\": \"$(hostname -s | tr \"[:upper:]\" \"[:lower:]\")\"}}\'')
" 2>/dev/null
