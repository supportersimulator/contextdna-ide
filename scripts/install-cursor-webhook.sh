#!/bin/bash
# Install Context DNA Webhook Integration for Cursor IDE
# Complete setup script that configures all components

set -e

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO_ROOT"

echo "🧬 Context DNA → Cursor IDE Integration Installer"
echo "=================================================="
echo ""

# Step 1: Register Cursor in database
echo "Step 1: Registering Cursor in configuration database..."
if PYTHONPATH=. .venv/bin/python3 scripts/register-cursor-ide.py; then
    echo "✅ Cursor registered"
else
    echo "⚠️ Registration failed (may already exist)"
fi
echo ""

# Step 2: Verify .cursor/settings.json
echo "Step 2: Verifying .cursor/settings.json..."
if [ -f ".cursor/settings.json" ]; then
    echo "✅ Settings file exists"
else
    echo "❌ Settings file missing - should have been created"
    exit 1
fi
echo ""

# Step 3: Verify .cursor/contextdna-bridge.sh
echo "Step 3: Verifying bridge script..."
if [ -x ".cursor/contextdna-bridge.sh" ]; then
    echo "✅ Bridge script ready"
else
    echo "❌ Bridge script missing or not executable"
    exit 1
fi
echo ""

# Step 4: Verify .cursorrules has session recovery
echo "Step 4: Verifying .cursorrules session recovery protocol..."
if grep -q "SESSION CRASH RECOVERY" .cursorrules; then
    echo "✅ Session recovery protocol present"
else
    echo "⚠️ Session recovery protocol not found in .cursorrules"
fi
echo ""

# Step 5: Verify helper agent endpoint
echo "Step 5: Testing helper agent Cursor endpoint..."
if curl -s --max-time 2 http://127.0.0.1:8080/health > /dev/null 2>&1; then
    echo "✅ Helper agent online"
    
    # Test Cursor endpoint
    TEST=$(curl -s --max-time 5 -X POST http://127.0.0.1:8080/contextdna/inject/cursor \
        -H "Content-Type: application/json" \
        -d '{"prompt":"test","workspace":"'$(pwd)'"}' 2>/dev/null)
    
    if echo "$TEST" | grep -q '"payload"'; then
        echo "✅ Cursor endpoint functional"
    else
        echo "⚠️ Cursor endpoint returned unexpected response"
    fi
else
    echo "⚠️ Helper agent offline - start it with:"
    echo "   python memory/agent_service.py"
fi
echo ""

# Step 6: Verify activity watcher
echo "Step 6: Verifying activity watcher..."
if [ -x "memory/cursor_activity_watcher.py" ]; then
    echo "✅ Activity watcher installed"
    
    # Test status command
    if PYTHONPATH=. .venv/bin/python3 memory/cursor_activity_watcher.py --status 2>&1 | grep -q "Cursor Activity"; then
        echo "✅ Activity watcher commands work"
    else
        echo "⚠️ Activity watcher status check failed"
    fi
else
    echo "❌ Activity watcher missing or not executable"
    exit 1
fi
echo ""

# Step 7: Verify scheduler job
echo "Step 7: Verifying scheduler integration..."
if grep -q "cursor_context_refresh" memory/lite_scheduler.py; then
    echo "✅ Cursor job registered in scheduler"
    
    if grep -q "_run_cursor_context_refresh" memory/lite_scheduler.py; then
        echo "✅ Cursor job function implemented"
    else
        echo "⚠️ Cursor job function not found"
    fi
else
    echo "❌ Cursor job not in scheduler"
    exit 1
fi
echo ""

# Step 8: Mark as configured in database
echo "Step 8: Marking Cursor as configured..."
sqlite3 ~/.context-dna/context_dna.db "
UPDATE ide_configurations 
SET is_configured = 1,
    hook_installed_at = datetime('now'),
    updated_at = datetime('now')
WHERE ide_type = 'cursor';
" 2>/dev/null && echo "✅ Configuration status updated" || echo "⚠️ Database update failed"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "✅ Installation Complete!"
echo ""
echo "━━━ Usage ━━━"
echo ""
echo "1. Session Recovery (run at start of new session):"
echo "   PYTHONPATH=. .venv/bin/python3 memory/session_historian.py rehydrate"
echo ""
echo "2. Manual Context Fetch (anytime):"
echo "   .cursor/contextdna-bridge.sh \"your task description\""
echo ""
echo "3. Check Auto-Refresh Status:"
echo "   python memory/cursor_activity_watcher.py --status"
echo ""
echo "4. Start Activity Watcher Daemon (optional - auto-updates .cursorrules):"
echo "   python memory/cursor_activity_watcher.py --daemon"
echo ""
echo "━━━ Verification ━━━"
echo ""
echo "Run comprehensive tests:"
echo "   ./scripts/verify-cursor-webhook.sh"
echo ""
echo "━━━ What Happens Now ━━━"
echo ""
echo "Every time you type in Cursor:"
echo "  1. .cursorrules is read (includes session recovery protocol)"
echo "  2. Every 60s, context is auto-refreshed (when Cursor active)"
echo "  3. Helper agent tracks your activity in database"
echo "  4. Section 8 (8th Intelligence) provides Synaptic's real-time insights"
echo ""
echo "The 9-section Context DNA payload is now available to Cursor!"
echo ""
