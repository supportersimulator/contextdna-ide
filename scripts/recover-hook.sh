#!/bin/bash
# =============================================================================
# Auto-Recovery: Restore Hook Functionality
# =============================================================================
# Runs in background when hook delivery fails.
# Attempts to diagnose and fix the issue automatically.
#
# Fixes:
# 1. Missing or corrupted hook script → Restore from template
# 2. Non-executable script → chmod +x
# 3. Wrong Python path → Update shebang
# 4. Missing dependencies → Reinstall
#
# Usage: ./recover-hook.sh <destination_id> <reason>
# Example: ./recover-hook.sh cursor_ide_hooks "hook_timeout"
# =============================================================================

set +e  # Don't exit on error - we're trying to recover

DESTINATION_ID="$1"
REASON="$2"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON="$REPO_ROOT/.venv/bin/python3"

LOG="/tmp/context-dna-recovery.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$TIMESTAMP] ========================================" >> "$LOG"
echo "[$TIMESTAMP] Hook Recovery: $DESTINATION_ID" >> "$LOG"
echo "[$TIMESTAMP] Reason: $REASON" >> "$LOG"
echo "[$TIMESTAMP] ========================================" >> "$LOG"

# Get destination info from registry
DEST_INFO=$("$PYTHON" -c "
from memory.destination_registry import DestinationRegistry
import json

registry = DestinationRegistry()
dest = registry.get_destination('$DESTINATION_ID')

if dest:
    print(json.dumps({
        'hook_script': dest.delivery_endpoint,
        'config_path': dest.config_path,
        'friendly_name': dest.friendly_name
    }))
" 2>/dev/null)

if [ -z "$DEST_INFO" ]; then
    echo "[$TIMESTAMP] ❌ Destination not found in registry" >> "$LOG"
    exit 1
fi

HOOK_SCRIPT=$(echo "$DEST_INFO" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('hook_script',''))")
CONFIG_PATH=$(echo "$DEST_INFO" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('config_path',''))")
FRIENDLY_NAME=$(echo "$DEST_INFO" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('friendly_name',''))")

echo "[$TIMESTAMP] Hook script: $HOOK_SCRIPT" >> "$LOG"
echo "[$TIMESTAMP] Config: $CONFIG_PATH" >> "$LOG"

# =============================================================================
# DIAGNOSTIC 1: Check if hook script exists
# =============================================================================

if [ ! -f "$HOOK_SCRIPT" ]; then
    echo "[$TIMESTAMP] ❌ Hook script missing: $HOOK_SCRIPT" >> "$LOG"
    
    # Try to restore from backup
    BACKUP="${HOOK_SCRIPT}.backup"
    if [ -f "$BACKUP" ]; then
        cp "$BACKUP" "$HOOK_SCRIPT"
        echo "[$TIMESTAMP] ✅ Restored from backup" >> "$LOG"
    else
        echo "[$TIMESTAMP] ❌ No backup found, cannot restore" >> "$LOG"
        exit 1
    fi
fi

# =============================================================================
# DIAGNOSTIC 2: Check if executable
# =============================================================================

if [ ! -x "$HOOK_SCRIPT" ]; then
    echo "[$TIMESTAMP] ⚠️  Hook not executable, fixing..." >> "$LOG"
    chmod +x "$HOOK_SCRIPT"
    echo "[$TIMESTAMP] ✅ Made executable" >> "$LOG"
fi

# =============================================================================
# DIAGNOSTIC 3: Test execution
# =============================================================================

TEST_OUTPUT=$("$HOOK_SCRIPT" "recovery test" 2>&1)
TEST_EXIT=$?

if [ $TEST_EXIT -eq 0 ] && [ -n "$TEST_OUTPUT" ]; then
    echo "[$TIMESTAMP] ✅ Hook execution successful" >> "$LOG"
    
    # Update registry: mark as recovered
    "$PYTHON" -c "
from memory.destination_registry import DestinationRegistry
registry = DestinationRegistry()
registry.update_health('$DESTINATION_ID', True, None)
print('Registry updated: recovered')
" >> "$LOG" 2>&1
    
    # Send success notification
    osascript -e "display notification \"Hook restored for $FRIENDLY_NAME\" with title \"Context DNA Recovery\" sound name \"Glass\"" 2>/dev/null || true
    
    echo "[$TIMESTAMP] ✅ Recovery complete" >> "$LOG"
    exit 0
    
else
    echo "[$TIMESTAMP] ❌ Hook still failing after recovery" >> "$LOG"
    echo "[$TIMESTAMP] Exit code: $TEST_EXIT" >> "$LOG"
    echo "[$TIMESTAMP] Output: ${TEST_OUTPUT:0:200}" >> "$LOG"
    
    # Send failure notification
    osascript -e "display notification \"Hook recovery failed for $FRIENDLY_NAME - manual intervention needed\" with title \"Context DNA Alert\" sound name \"Basso\"" 2>/dev/null || true
    
    exit 1
fi
