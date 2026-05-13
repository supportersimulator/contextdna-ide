#!/bin/bash
# =============================================================================
# Ensure Context DNA Is Running
# =============================================================================
# Called by MCP server wrapper to guarantee services are available.
# Auto-starts if needed, waits for health, exits with status code.
#
# Exit Codes:
#   0 - Context DNA is healthy
#   1 - Docker not available
#   2 - Context DNA failed to start
#   3 - Timeout waiting for health
# =============================================================================

set -e

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
MAX_WAIT=30  # seconds
LOG_FILE="/tmp/context-dna-autostart.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
    echo "$1" >&2
}

check_health() {
    curl -s --max-time 2 http://localhost:8029/health 2>/dev/null | grep -q '"code":0\|"msg":"ok"'
}

# Check if already healthy
if check_health; then
    log "✓ Context DNA already running"
    exit 0
fi

log "Context DNA not running - attempting auto-start..."

# Check Docker
if ! docker info &>/dev/null 2>&1; then
    log "✗ Docker not running - cannot auto-start Context DNA"
    log "  Start Docker Desktop and try again"
    exit 1
fi

log "✓ Docker is running"

# Start Context DNA
log "Starting Context DNA services..."
cd "$REPO_ROOT"
if ./scripts/context-dna up >> "$LOG_FILE" 2>&1; then
    log "✓ Context DNA startup initiated"
else
    log "✗ Failed to start Context DNA"
    exit 2
fi

# Wait for health
log "Waiting for services to be ready..."
COUNT=0
while [ $COUNT -lt $MAX_WAIT ]; do
    if check_health; then
        log "✓ Context DNA is healthy (${COUNT}s)"
        exit 0
    fi
    sleep 1
    COUNT=$((COUNT + 1))
done

log "✗ Timeout waiting for Context DNA health check"
log "  Services may still be starting - check: context-dna status"
exit 3
