#!/bin/bash
# =============================================================================
# Auto-Recovery: Comprehensive System Recovery
# =============================================================================
# Runs when ALL delivery methods fail (worst case scenario).
# Attempts to restore the entire Context DNA stack.
#
# Recovery sequence:
# 1. Restart Context DNA Docker services
# 2. Restart local LLM (mlx_lm.server via launchd)
# 3. Restart MCP server
# 4. Fix hook scripts
# 5. Verify each layer works
# 6. Send detailed status report
#
# Usage: ./recover-all.sh <destination_id> <reason>
# Example: ./recover-all.sh cursor_ide_hooks "all_fallbacks_exhausted"
# =============================================================================

set +e

DESTINATION_ID="$1"
REASON="$2"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON="$REPO_ROOT/.venv/bin/python3"

LOG="/tmp/context-dna-recovery.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$TIMESTAMP] ==========================================" >> "$LOG"
echo "[$TIMESTAMP] COMPREHENSIVE RECOVERY: $DESTINATION_ID" >> "$LOG"
echo "[$TIMESTAMP] Reason: $REASON" >> "$LOG"
echo "[$TIMESTAMP] ==========================================" >> "$LOG"

RECOVERY_STEPS=0
RECOVERY_SUCCESS=0

# =============================================================================
# STEP 1: Restart Context DNA Services
# =============================================================================

echo "[$TIMESTAMP] [1/5] Checking Context DNA services..." >> "$LOG"
RECOVERY_STEPS=$((RECOVERY_STEPS + 1))

cd "$REPO_ROOT"

if ./scripts/context-dna status | grep -q "API.*Running"; then
    echo "[$TIMESTAMP] ✅ Context DNA services healthy" >> "$LOG"
    RECOVERY_SUCCESS=$((RECOVERY_SUCCESS + 1))
else
    echo "[$TIMESTAMP] ⚠️  Restarting Context DNA services..." >> "$LOG"
    
    ./scripts/context-dna restart >> "$LOG" 2>&1
    sleep 5
    
    if ./scripts/context-dna status | grep -q "API.*Running"; then
        echo "[$TIMESTAMP] ✅ Context DNA services restored" >> "$LOG"
        RECOVERY_SUCCESS=$((RECOVERY_SUCCESS + 1))
    else
        echo "[$TIMESTAMP] ❌ Context DNA services still down" >> "$LOG"
    fi
fi

# =============================================================================
# STEP 2: Restart Local LLM (mlx_lm.server via launchd)
# =============================================================================

echo "[$TIMESTAMP] [2/5] Checking local LLM (mlx_lm.server)..." >> "$LOG"
RECOVERY_STEPS=$((RECOVERY_STEPS + 1))

if curl -s --max-time 2 http://127.0.0.1:5044/v1/models > /dev/null 2>&1; then
    echo "[$TIMESTAMP] ✅ Local LLM healthy" >> "$LOG"
    RECOVERY_SUCCESS=$((RECOVERY_SUCCESS + 1))
else
    echo "[$TIMESTAMP] ⚠️  Restarting local LLM via launchctl..." >> "$LOG"

    launchctl kickstart -k "gui/$(id -u)/com.contextdna.llm" 2>/dev/null || \
    (launchctl unload ~/Library/LaunchAgents/com.contextdna.llm.plist 2>/dev/null; \
     sleep 2; \
     launchctl load ~/Library/LaunchAgents/com.contextdna.llm.plist 2>/dev/null)

    echo "[$TIMESTAMP] ⏳ Waiting for model load (15s)..." >> "$LOG"
    sleep 15

    if curl -s --max-time 2 http://127.0.0.1:5044/v1/models > /dev/null 2>&1; then
        echo "[$TIMESTAMP] ✅ Local LLM restored" >> "$LOG"
        RECOVERY_SUCCESS=$((RECOVERY_SUCCESS + 1))
    else
        echo "[$TIMESTAMP] ❌ Local LLM failed to start" >> "$LOG"
    fi
fi

# =============================================================================
# STEP 3: Restart MCP Server
# =============================================================================

echo "[$TIMESTAMP] [3/5] Checking MCP server..." >> "$LOG"
RECOVERY_STEPS=$((RECOVERY_STEPS + 1))

cd "$REPO_ROOT"
./scripts/recover-mcp.sh "$DESTINATION_ID" "comprehensive_recovery" >> "$LOG" 2>&1

if [ $? -eq 0 ]; then
    RECOVERY_SUCCESS=$((RECOVERY_SUCCESS + 1))
fi

# =============================================================================
# STEP 4: Fix Hook Scripts
# =============================================================================

echo "[$TIMESTAMP] [4/5] Checking hook script..." >> "$LOG"
RECOVERY_STEPS=$((RECOVERY_STEPS + 1))

if [ -f "$HOOK_SCRIPT" ]; then
    # Make executable
    chmod +x "$HOOK_SCRIPT" 2>/dev/null
    
    # Test execution
    if "$HOOK_SCRIPT" "recovery test" > /dev/null 2>&1; then
        echo "[$TIMESTAMP] ✅ Hook script working" >> "$LOG"
        RECOVERY_SUCCESS=$((RECOVERY_SUCCESS + 1))
    else
        echo "[$TIMESTAMP] ❌ Hook script still failing" >> "$LOG"
    fi
else
    echo "[$TIMESTAMP] ❌ Hook script not found" >> "$LOG"
fi

# =============================================================================
# STEP 5: Verify File-Based Fallback
# =============================================================================

echo "[$TIMESTAMP] [5/5] Checking file-based fallback..." >> "$LOG"
RECOVERY_STEPS=$((RECOVERY_STEPS + 1))

INJECTION_FILE="$REPO_ROOT/memory/.injection_latest.json"

if [ -f "$INJECTION_FILE" ] && [ -r "$INJECTION_FILE" ]; then
    echo "[$TIMESTAMP] ✅ File-based fallback available" >> "$LOG"
    RECOVERY_SUCCESS=$((RECOVERY_SUCCESS + 1))
else
    echo "[$TIMESTAMP] ❌ File-based fallback missing" >> "$LOG"
fi

# =============================================================================
# SUMMARY & NOTIFICATION
# =============================================================================

SUCCESS_RATE=$((100 * RECOVERY_SUCCESS / RECOVERY_STEPS))

echo "[$TIMESTAMP] ========================================" >> "$LOG"
echo "[$TIMESTAMP] Recovery Summary: $RECOVERY_SUCCESS/$RECOVERY_STEPS ($SUCCESS_RATE%)" >> "$LOG"
echo "[$TIMESTAMP] ========================================" >> "$LOG"

if [ $SUCCESS_RATE -ge 80 ]; then
    # Mostly recovered
    osascript -e "display notification \"System recovered: $RECOVERY_SUCCESS/$RECOVERY_STEPS layers operational\" with title \"Context DNA Recovery Complete\" sound name \"Glass\"" 2>/dev/null || true
    
    echo "[$TIMESTAMP] ✅ RECOVERY SUCCESSFUL ($SUCCESS_RATE%)" >> "$LOG"
    exit 0
    
elif [ $SUCCESS_RATE -ge 40 ]; then
    # Partial recovery
    osascript -e "display notification \"Partial recovery: $RECOVERY_SUCCESS/$RECOVERY_STEPS layers operational\" with title \"Context DNA Recovery\" sound name \"Purr\"" 2>/dev/null || true
    
    echo "[$TIMESTAMP] ⚠️  PARTIAL RECOVERY ($SUCCESS_RATE%)" >> "$LOG"
    exit 1
    
else
    # Recovery failed
    osascript -e "display notification \"Recovery failed: Only $RECOVERY_SUCCESS/$RECOVERY_STEPS layers working - manual intervention needed\" with title \"Context DNA Alert\" sound name \"Basso\"" 2>/dev/null || true
    
    echo "[$TIMESTAMP] ❌ RECOVERY FAILED ($SUCCESS_RATE%)" >> "$LOG"
    exit 1
fi
