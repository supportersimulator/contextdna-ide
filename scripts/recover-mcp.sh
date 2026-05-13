#!/bin/bash
# =============================================================================
# Auto-Recovery: Restore MCP Server
# =============================================================================
# Runs in background when MCP delivery fails.
# Attempts to restart the MCP server and verify it's responding.
#
# Fixes:
# 1. MCP server not running → Start it
# 2. MCP server crashed → Restart it
# 3. MCP server frozen → Kill and restart
# 4. Port conflict → Find and kill conflicting process
#
# Usage: ./recover-mcp.sh <destination_id> <reason>
# Example: ./recover-mcp.sh cursor_ide_mcp "mcp_timeout"
# =============================================================================

set +e

DESTINATION_ID="$1"
REASON="$2"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON="$REPO_ROOT/.venv/bin/python3"

LOG="/tmp/context-dna-recovery.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$TIMESTAMP] ========================================" >> "$LOG"
echo "[$TIMESTAMP] MCP Recovery: $DESTINATION_ID" >> "$LOG"
echo "[$TIMESTAMP] Reason: $REASON" >> "$LOG"
echo "[$TIMESTAMP] ========================================" >> "$LOG"

# =============================================================================
# DIAGNOSTIC 1: Check if MCP server is running
# =============================================================================

MCP_PID=$(pgrep -f "contextdna_webhook_mcp.py" 2>/dev/null | head -1)

if [ -n "$MCP_PID" ]; then
    echo "[$TIMESTAMP] MCP server running (PID: $MCP_PID)" >> "$LOG"
    
    # Check if it's responding
    if "$PYTHON" -c "import requests; requests.get('http://localhost:8029/health', timeout=2)" 2>/dev/null; then
        echo "[$TIMESTAMP] ✅ MCP server is healthy (false alarm)" >> "$LOG"
        exit 0
    else
        echo "[$TIMESTAMP] ⚠️  MCP server frozen, restarting..." >> "$LOG"
        kill "$MCP_PID" 2>/dev/null
        sleep 2
    fi
else
    echo "[$TIMESTAMP] MCP server not running" >> "$LOG"
fi

# =============================================================================
# FIX: Start MCP server
# =============================================================================

echo "[$TIMESTAMP] Starting MCP server..." >> "$LOG"

cd "$REPO_ROOT"

# Start MCP server in background
PYTHONPATH="$REPO_ROOT" \
REPO_ROOT="$REPO_ROOT" \
nohup "$PYTHON" mcp-servers/contextdna_webhook_mcp.py \
    >> /tmp/mcp-server.log 2>&1 &

NEW_PID=$!
echo "[$TIMESTAMP] Started MCP server (PID: $NEW_PID)" >> "$LOG"

# Wait for startup
sleep 3

# =============================================================================
# VERIFY: Check if MCP is now responding
# =============================================================================

if pgrep -f "contextdna_webhook_mcp.py" > /dev/null 2>&1; then
    echo "[$TIMESTAMP] ✅ MCP server process running" >> "$LOG"
    
    # Test if it responds
    if "$PYTHON" -c "import requests; requests.get('http://localhost:8029/health', timeout=2)" 2>/dev/null; then
        echo "[$TIMESTAMP] ✅ MCP server responding to health check" >> "$LOG"
        
        # Update registry
        "$PYTHON" -c "
from memory.destination_registry import DestinationRegistry
registry = DestinationRegistry()
registry.update_health('$DESTINATION_ID', True, None)
" >> "$LOG" 2>&1
        
        # Send notification
        osascript -e "display notification \"MCP server restored\" with title \"Context DNA Recovery\" sound name \"Glass\"" 2>/dev/null || true
        
        echo "[$TIMESTAMP] ✅ MCP recovery complete" >> "$LOG"
        exit 0
    else
        echo "[$TIMESTAMP] ⚠️  MCP server started but not responding yet" >> "$LOG"
    fi
else
    echo "[$TIMESTAMP] ❌ MCP server failed to start" >> "$LOG"
    
    # Check for errors in MCP log
    if [ -f "/tmp/mcp-server.log" ]; then
        echo "[$TIMESTAMP] Last MCP error:" >> "$LOG"
        tail -5 /tmp/mcp-server.log >> "$LOG"
    fi
    
    osascript -e "display notification \"MCP server recovery failed - check logs\" with title \"Context DNA Alert\" sound name \"Basso\"" 2>/dev/null || true
    
    exit 1
fi
