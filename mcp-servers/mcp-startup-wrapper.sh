#!/bin/bash
# =============================================================================
# MCP Server Startup Wrapper
# =============================================================================
# This script is called by Cursor's MCP configuration.
# It ensures Context DNA is running before starting the MCP server.
#
# Usage in .mcp.json:
#   "command": "/path/to/mcp-startup-wrapper.sh"
#   "args": []
# =============================================================================

set -e

# Configuration
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
ENSURE_RUNNING="$REPO_ROOT/scripts/ensure-context-dna-running.sh"
MCP_SERVER="$REPO_ROOT/mcp-servers/contextdna_webhook_mcp.py"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python3"

# Logging to file (MCP servers use stdio for protocol)
LOG_FILE="/tmp/mcp-server-startup.log"
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
    # Also write to stderr so it appears in Cursor logs
    echo "[MCP Wrapper] $1" >&2
}

log "=== MCP Server Startup ==="

# Ensure Context DNA is running (uses dedicated script)
if [ -x "$ENSURE_RUNNING" ]; then
    log "Ensuring Context DNA services are running..."
    if "$ENSURE_RUNNING" 2>> "$LOG_FILE"; then
        log "✓ Context DNA is healthy"
    else
        EXIT_CODE=$?
        case $EXIT_CODE in
            1) log "⚠️ Docker not available - MCP will use fallback mode" ;;
            2) log "⚠️ Context DNA failed to start - MCP will use fallback mode" ;;
            3) log "⚠️ Context DNA startup timeout - MCP will use fallback mode" ;;
            *) log "⚠️ Unknown error ($EXIT_CODE) - MCP will use fallback mode" ;;
        esac
    fi
else
    log "⚠️ Auto-start script not found - assuming manual startup"
fi

# Register this MCP session in the database
log "Registering MCP session..."
PYTHONPATH="$REPO_ROOT" "$VENV_PYTHON" -c "
import sqlite3
from datetime import datetime, timezone

try:
    db = sqlite3.connect('$REPO_ROOT/memory/.observability.db')
    now = datetime.now(timezone.utc).isoformat()
    
    # Register MCP server as a destination
    db.execute('''
        INSERT OR REPLACE INTO webhook_destination
        (destination_id, destination_name, destination_type, endpoint_url, 
         is_active, created_at, last_delivery_at, config_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        'cursor_mcp',
        'Cursor MCP Server',
        'ide',
        'mcp://contextdna-webhook',
        1,
        now,
        now,
        '{\"protocol\": \"mcp\", \"version\": \"2.0.0\"}',
    ))
    db.commit()
    db.close()
    print('✓ MCP destination registered', file=__import__('sys').stderr)
except Exception as e:
    print(f'⚠️ Registration failed: {e}', file=__import__('sys').stderr)
" 2>> "$LOG_FILE"

# Start the MCP server
log "Starting MCP server protocol..."
export PYTHONPATH="$REPO_ROOT"
export CONTEXT_DNA_TIMEOUT="15000"
export REPO_ROOT="$REPO_ROOT"

# Execute the MCP server (this replaces the current process)
exec "$VENV_PYTHON" "$MCP_SERVER"
