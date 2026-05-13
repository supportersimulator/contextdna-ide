#!/usr/bin/env bash
# fleet-session-init.sh — Injected at session start via hook
#
# Writes fleet monitor setup instruction to seed file so the session
# auto-starts the /loop cron on its first interaction.

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
SEED="/tmp/fleet-seed-${NODE_ID}.md"

# Only inject if Fleet Nerve daemon is running
if curl -sf http://localhost:8855/health >/dev/null 2>&1; then
    # Check if this seed file already has the init instruction (prevent duplicates)
    REPO="$(cd "$(dirname "$0")/.." && pwd)"
    if ! grep -q "FLEET MONITOR INIT" "$SEED" 2>/dev/null; then
        cat >> "$SEED" <<INIT
## [FLEET MONITOR INIT]

Fleet Nerve is running on this machine. Start the activity monitor:

/loop 1m bash ${REPO}/scripts/fleet-check.sh

Then check the work backlog: curl -s localhost:8855/work/next

---

INIT
    fi
fi
