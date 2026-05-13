#!/bin/bash
# Verify Cursor Webhook Integration
# Tests all components of the Cursor Context DNA integration

# Don't exit on first error - we want to see all test results
set +e

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
HELPER_AGENT_URL="http://127.0.0.1:8080"

echo "🧬 Context DNA → Cursor Webhook Verification"
echo "=============================================="
echo ""

# Color codes for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

pass_count=0
fail_count=0

# Test helper function
test_check() {
    local name="$1"
    local result="$2"
    
    if [ "$result" = "pass" ]; then
        echo -e "${GREEN}✅ PASS${NC} - $name"
        ((pass_count++))
    elif [ "$result" = "warn" ]; then
        echo -e "${YELLOW}⚠️  WARN${NC} - $name"
    else
        echo -e "${RED}❌ FAIL${NC} - $name"
        ((fail_count++))
    fi
}

echo "━━━ Phase 1: Database Registration ━━━"
echo ""

# Test 1: Check Cursor registration in database
if sqlite3 ~/.context-dna/context_dna.db \
    "SELECT id FROM ide_configurations WHERE ide_type = 'cursor'" 2>/dev/null | grep -q "."; then
    CURSOR_ID=$(sqlite3 ~/.context-dna/context_dna.db \
        "SELECT id FROM ide_configurations WHERE ide_type = 'cursor'" 2>/dev/null)
    test_check "Cursor registered in database (ID: ${CURSOR_ID:0:8}...)" "pass"
    
    # Get registration details
    DETAILS=$(sqlite3 ~/.context-dna/context_dna.db \
        "SELECT is_installed, is_configured, injection_style FROM ide_configurations WHERE ide_type = 'cursor'" 2>/dev/null)
    echo "   Details: $DETAILS"
else
    test_check "Cursor database registration" "fail"
    echo "   Run: python scripts/register-cursor-ide.py"
fi

echo ""
echo "━━━ Phase 2: Helper Agent ━━━"
echo ""

# Test 2: Helper agent health
if curl -s --max-time 2 "$HELPER_AGENT_URL/health" > /dev/null 2>&1; then
    test_check "Helper agent online (port 8080)" "pass"
else
    test_check "Helper agent online (port 8080)" "fail"
    echo "   Start: python memory/agent_service.py"
fi

# Test 3: Cursor endpoint exists
if curl -s --max-time 2 "$HELPER_AGENT_URL/openapi.json" 2>/dev/null | grep -q "inject/cursor"; then
    test_check "Cursor endpoint available (/contextdna/inject/cursor)" "pass"
else
    test_check "Cursor endpoint available" "warn"
    echo "   Endpoint may exist but not in OpenAPI spec"
fi

# Test 4: Cursor endpoint functional
TEST_RESPONSE=$(curl -s --max-time 5 -X POST "$HELPER_AGENT_URL/contextdna/inject/cursor" \
    -H "Content-Type: application/json" \
    -d '{"prompt":"test verification","workspace":"'"$REPO_ROOT"'"}' 2>/dev/null)

if echo "$TEST_RESPONSE" | grep -q '"payload"'; then
    PAYLOAD_SIZE=$(echo "$TEST_RESPONSE" | .venv/bin/python3 -c "import sys,json; print(len(json.load(sys.stdin).get('payload','')))" 2>/dev/null || echo "0")
    test_check "Cursor endpoint returns payload ($PAYLOAD_SIZE chars)" "pass"
else
    test_check "Cursor endpoint functional" "fail"
    echo "   Response: ${TEST_RESPONSE:0:100}"
fi

echo ""
echo "━━━ Phase 3: Session Historian ━━━"
echo ""

# Test 5: Session historian database
if [ -f ~/.context-dna/session_archive.db ]; then
    SESSION_COUNT=$(sqlite3 ~/.context-dna/session_archive.db \
        "SELECT COUNT(*) FROM archived_sessions" 2>/dev/null || echo "0")
    test_check "Session historian database ($SESSION_COUNT sessions)" "pass"
else
    test_check "Session historian database" "warn"
    echo "   Database will be created on first extraction"
fi

# Test 6: Rehydration command
REHYDRATE_OUTPUT=$(cd "$REPO_ROOT" && PYTHONPATH=. .venv/bin/python3 memory/session_historian.py rehydrate 2>/dev/null | head -20)
if echo "$REHYDRATE_OUTPUT" | grep -q "SESSION REHYDRATION\|No archived sessions"; then
    test_check "Session rehydration command" "pass"
else
    test_check "Session rehydration command" "fail"
fi

echo ""
echo "━━━ Phase 4: File Configuration ━━━"
echo ""

# Test 7: .cursorrules exists
if [ -f "$REPO_ROOT/.cursorrules" ]; then
    RULES_SIZE=$(wc -c < "$REPO_ROOT/.cursorrules" | tr -d ' ')
    test_check ".cursorrules file exists ($RULES_SIZE bytes)" "pass"
    
    # Check for session recovery protocol
    if grep -q "SESSION CRASH RECOVERY" "$REPO_ROOT/.cursorrules"; then
        test_check "Session recovery protocol in .cursorrules" "pass"
    else
        test_check "Session recovery protocol in .cursorrules" "fail"
    fi
    
    # Check for Context DNA section
    if grep -q "CONTEXT DNA" "$REPO_ROOT/.cursorrules"; then
        test_check "Context DNA section in .cursorrules" "pass"
    else
        test_check "Context DNA section in .cursorrules" "warn"
    fi
else
    test_check ".cursorrules file" "fail"
fi

# Test 8: .cursor/settings.json exists
if [ -f "$REPO_ROOT/.cursor/settings.json" ]; then
    test_check ".cursor/settings.json exists" "pass"
    
    # Check for environment variables
    if grep -q "CURSOR_FILE_PATH" "$REPO_ROOT/.cursor/settings.json"; then
        test_check "Environment variables configured" "pass"
    else
        test_check "Environment variables configured" "fail"
    fi
else
    test_check ".cursor/settings.json" "fail"
fi

# Test 9: Bridge script exists
if [ -x "$REPO_ROOT/.cursor/contextdna-bridge.sh" ]; then
    test_check "Cursor bridge script (executable)" "pass"
else
    test_check "Cursor bridge script" "fail"
fi

echo ""
echo "━━━ Phase 5: Activity Watcher ━━━"
echo ""

# Test 10: Activity watcher script exists
if [ -x "$REPO_ROOT/memory/cursor_activity_watcher.py" ]; then
    test_check "Activity watcher script (executable)" "pass"
    
    # Test watcher status
    WATCHER_STATUS=$(cd "$REPO_ROOT" && PYTHONPATH=. .venv/bin/python3 memory/cursor_activity_watcher.py --status 2>&1)
    if echo "$WATCHER_STATUS" | grep -q "Cursor Activity Watcher"; then
        test_check "Activity watcher command works" "pass"
    else
        test_check "Activity watcher command" "fail"
    fi
else
    test_check "Activity watcher script" "fail"
fi

# Test 11: Manual refresh works
echo -n "Testing manual context refresh... "
if cd "$REPO_ROOT" && PYTHONPATH=. .venv/bin/python3 memory/cursor_activity_watcher.py --refresh 2>/dev/null; then
    test_check "Manual context refresh" "pass"
else
    test_check "Manual context refresh" "warn"
    echo "   (May fail if helper agent offline or Cursor not running)"
fi

echo ""
echo "━━━ Phase 6: Scheduler Integration ━━━"
echo ""

# Test 12: Scheduler job registered
if grep -q "cursor_context_refresh" "$REPO_ROOT/memory/lite_scheduler.py"; then
    test_check "Cursor job in scheduler" "pass"
    
    # Check for function implementation
    if grep -q "_run_cursor_context_refresh" "$REPO_ROOT/memory/lite_scheduler.py"; then
        test_check "Cursor job function implemented" "pass"
    else
        test_check "Cursor job function implemented" "fail"
    fi
else
    test_check "Cursor job in scheduler" "fail"
fi

echo ""
echo "━━━ Phase 7: End-to-End Test ━━━"
echo ""

# Test 13: Full injection pipeline
TEST_PROMPT="test full cursor webhook integration pipeline"
END_TO_END=$(cd "$REPO_ROOT" && \
    PYTHONPATH=. .venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from memory.persistent_hook_structure import generate_context_injection

result = generate_context_injection('$TEST_PROMPT', 'hybrid')
print('SUCCESS' if hasattr(result, 'full_payload') and len(result.full_payload) > 100 else 'FAIL')
print(f'Sections: {len(result.sections) if hasattr(result, \"sections\") else 0}')
print(f'Size: {len(result.full_payload) if hasattr(result, \"full_payload\") else 0} chars')
" 2>/dev/null)

if echo "$END_TO_END" | grep -q "SUCCESS"; then
    test_check "End-to-end injection pipeline" "pass"
    echo "$END_TO_END" | tail -2 | sed 's/^/   /'
else
    test_check "End-to-end injection pipeline" "fail"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "VERIFICATION SUMMARY"
echo "  Passed: $pass_count"
echo "  Failed: $fail_count"
echo ""

if [ $fail_count -eq 0 ]; then
    echo -e "${GREEN}✅ All critical tests passed!${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Register Cursor (if not done):"
    echo "     python scripts/register-cursor-ide.py"
    echo ""
    echo "  2. Test manual context fetch:"
    echo "     .cursor/contextdna-bridge.sh \"test prompt\""
    echo ""
    echo "  3. Verify auto-refresh:"
    echo "     python memory/cursor_activity_watcher.py --status"
    echo ""
    echo "  4. Start activity watcher daemon (optional):"
    echo "     python memory/cursor_activity_watcher.py --daemon"
    echo ""
    exit 0
else
    echo -e "${RED}❌ $fail_count test(s) failed${NC}"
    echo ""
    echo "Fix issues above, then re-run:"
    echo "  ./scripts/verify-cursor-webhook.sh"
    echo ""
    exit 1
fi
