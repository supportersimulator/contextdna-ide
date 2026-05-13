#!/usr/bin/env bash
# Ensure theatrical dashboard is running on session start.
# Called by Claude Code hook or manually.
# Exit 0 always — never block session start.

set -euo pipefail

PORT="${FLEET_DASHBOARD_PORT:-8856}"
MULTIFLEET_DIR="$(cd "$(dirname "$0")/../multi-fleet" && pwd)"

# Quick port check
if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    echo "[theatrical] Already running on :$PORT"
    exit 0
fi

# Launch in background
cd "$MULTIFLEET_DIR"
MULTIFLEET_NODE_ID="${MULTIFLEET_NODE_ID:-mac1}" \
    python3 -m multifleet.web_server --port "$PORT" &>/dev/null &

# Wait up to 3s
for i in 1 2 3 4 5 6; do
    sleep 0.5
    if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
        echo "[theatrical] Launched on :$PORT"
        exit 0
    fi
done

echo "[theatrical] Warning: launch may have failed" >&2
exit 0
