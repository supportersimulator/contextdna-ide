#!/usr/bin/env bash
# fleet-health.sh — Quick fleet health summary (one-liner per node)
#
# Shows: node status, sessions, idle time, branch
# Silent if daemon not running.

PORT="${FLEET_NERVE_PORT:-8855}"

FLEET=$(curl -sf "http://127.0.0.1:${PORT}/fleet/live" 2>/dev/null) || exit 0

echo "$FLEET" | python3 -c "
import sys, json
d = json.load(sys.stdin)
status = d.get('fleetStatus', '')
if status:
    print(f'⚡ {status}')
" 2>/dev/null
