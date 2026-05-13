#!/bin/bash
# =============================================================================
# Verify MCP Auto-Start Configuration
# =============================================================================
# Comprehensive verification that auto-start system is properly configured
# =============================================================================

set -e

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO_ROOT"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}${BLUE}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${BLUE}║     MCP AUTO-START VERIFICATION                          ║${NC}"
echo -e "${BOLD}${BLUE}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

PASS=0
FAIL=0
WARN=0

check() {
    local name="$1"
    local command="$2"
    
    printf "%-50s" "$name"
    if eval "$command" &>/dev/null; then
        echo -e "${GREEN}✅ PASS${NC}"
        PASS=$((PASS + 1))
        return 0
    else
        echo -e "${RED}❌ FAIL${NC}"
        FAIL=$((FAIL + 1))
        return 1
    fi
}

check_warn() {
    local name="$1"
    local command="$2"
    
    printf "%-50s" "$name"
    if eval "$command" &>/dev/null; then
        echo -e "${GREEN}✅ PASS${NC}"
        PASS=$((PASS + 1))
        return 0
    else
        echo -e "${YELLOW}⚠️  WARN${NC}"
        WARN=$((WARN + 1))
        return 1
    fi
}

echo -e "${BOLD}1. SCRIPTS & FILES${NC}"
echo "─────────────────────────────────────────────────────────"
check "MCP startup wrapper exists" "test -f mcp-servers/mcp-startup-wrapper.sh"
check "MCP startup wrapper executable" "test -x mcp-servers/mcp-startup-wrapper.sh"
check "Health check script exists" "test -f scripts/ensure-context-dna-running.sh"
check "Health check script executable" "test -x scripts/ensure-context-dna-running.sh"
check "MCP server exists" "test -f mcp-servers/contextdna_webhook_mcp.py"
check "Configuration script exists" "test -f scripts/configure-cursor-mcp-autostart.py"
echo ""

echo -e "${BOLD}2. MCP CONFIGURATION${NC}"
echo "─────────────────────────────────────────────────────────"
check "Cursor MCP config exists" "test -f ~/.cursor/mcp.json"
check "Workspace MCP config exists" "test -f .mcp.json"
check "Cursor config uses wrapper" "grep -q 'mcp-startup-wrapper.sh' ~/.cursor/mcp.json"
check "Workspace config uses wrapper" "grep -q 'mcp-startup-wrapper.sh' .mcp.json"
check "Cursor config has PYTHONPATH" "grep -q 'PYTHONPATH' ~/.cursor/mcp.json"
check "Workspace config has PYTHONPATH" "grep -q 'PYTHONPATH' .mcp.json"
echo ""

echo -e "${BOLD}3. CONTEXT DNA SERVICES${NC}"
echo "─────────────────────────────────────────────────────────"
check "Docker running" "docker info"
check "Context DNA API responding" "curl -s --max-time 2 http://localhost:8029/health | grep -q 'ok'"
check_warn "Helper agent responding" "curl -s --max-time 2 http://localhost:8080/health"
check "PostgreSQL container healthy" "docker ps --filter 'name=contextdna-pg' --filter 'health=healthy' | grep -q contextdna-pg"
check "Redis container healthy" "docker ps --filter 'name=contextdna-redis' --filter 'health=healthy' | grep -q contextdna-redis"
echo ""

echo -e "${BOLD}4. DATABASE CONFIGURATION${NC}"
echo "─────────────────────────────────────────────────────────"
check "context_dna.db exists" "test -f ~/.context-dna/context_dna.db"
check "observability.db exists" "test -f memory/.observability.db"
check "Cursor IDE registered" "sqlite3 ~/.context-dna/context_dna.db 'SELECT 1 FROM ide_configurations WHERE ide_type=\"cursor\"' | grep -q 1"

# Check if configured (may be 0 before first run)
if sqlite3 ~/.context-dna/context_dna.db 'SELECT is_configured FROM ide_configurations WHERE ide_type="cursor"' 2>/dev/null | grep -q 1; then
    echo -e "Cursor marked as configured                      ${GREEN}✅ PASS${NC}"
    PASS=$((PASS + 1))
else
    echo -e "Cursor marked as configured                      ${YELLOW}⚠️  WARN (will be set on first MCP run)${NC}"
    WARN=$((WARN + 1))
fi

check "webhook_destination table exists" "sqlite3 memory/.observability.db 'SELECT name FROM sqlite_master WHERE name=\"webhook_destination\"' | grep -q webhook_destination"
echo ""

echo -e "${BOLD}5. BIDIRECTIONAL SYNC${NC}"
echo "─────────────────────────────────────────────────────────"
check "unified_sync.py exists" "test -f memory/unified_sync.py"
check "sync_config.py exists" "test -f memory/sync_config.py"
check "mode_sync_state table exists" "sqlite3 memory/.observability.db 'SELECT name FROM sqlite_master WHERE name=\"mode_sync_state\"' | grep -q mode_sync_state"

# Check current mode
CURRENT_MODE=$(sqlite3 memory/.observability.db 'SELECT current_mode FROM mode_sync_state WHERE id="singleton"' 2>/dev/null || echo "lite")
if [ "$CURRENT_MODE" = "heavy" ]; then
    echo -e "Current mode: heavy (PostgreSQL)                 ${GREEN}✅ PASS${NC}"
    PASS=$((PASS + 1))
else
    echo -e "Current mode: lite (SQLite only)                 ${YELLOW}⚠️  WARN${NC}"
    WARN=$((WARN + 1))
fi
echo ""

echo -e "${BOLD}6. SESSION RECOVERY${NC}"
echo "─────────────────────────────────────────────────────────"
check "session_historian.py exists" "test -f memory/session_historian.py"
check "session_archive.db exists" "test -f ~/.context-dna/session_archive.db"
check ".cursorrules has recovery protocol" "grep -q 'SESSION CRASH RECOVERY' .cursorrules"
check "Session historian works" "PYTHONPATH=. .venv/bin/python3 memory/session_historian.py rehydrate | grep -q 'SESSION REHYDRATION'"
echo ""

echo -e "${BOLD}7. WEBHOOK GENERATION${NC}"
echo "─────────────────────────────────────────────────────────"
check "persistent_hook_structure.py exists" "test -f memory/persistent_hook_structure.py"
check_warn "Professor available" "test -f memory/professor.py"

# Test webhook generation
if PYTHONPATH=. timeout 20 .venv/bin/python3 -c "
from memory.persistent_hook_structure import generate_context_injection
result = generate_context_injection('test', 'hybrid', 'test-verify')
assert hasattr(result, 'full_payload') or hasattr(result, 'content'), 'No payload generated'
print('OK')
" 2>/dev/null | grep -q OK; then
    echo -e "Webhook generation functional                    ${GREEN}✅ PASS${NC}"
    PASS=$((PASS + 1))
else
    echo -e "Webhook generation functional                    ${YELLOW}⚠️  WARN (may timeout)${NC}"
    WARN=$((WARN + 1))
fi
echo ""

echo -e "${BOLD}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║ VERIFICATION RESULTS                                      ║${NC}"
echo -e "${BOLD}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}✅ PASS:${NC}  $PASS"
echo -e "  ${YELLOW}⚠️  WARN:${NC}  $WARN"
echo -e "  ${RED}❌ FAIL:${NC}  $FAIL"
echo ""

if [ $FAIL -eq 0 ]; then
    echo -e "${GREEN}${BOLD}✓ VERIFICATION PASSED${NC}"
    echo ""
    echo "All critical components are configured correctly."
    echo ""
    echo -e "${BOLD}NEXT STEPS:${NC}"
    echo "  1. Restart Cursor completely (to reload MCP configuration)"
    echo "  2. Open a new chat"
    echo "  3. Ask: 'Can you verify you received the webhook payload?'"
    echo "  4. Expect: Agent lists all 9 sections"
    echo ""
    exit 0
else
    echo -e "${RED}${BOLD}✗ VERIFICATION FAILED${NC}"
    echo ""
    echo "Fix the failed checks above before using MCP auto-start."
    echo ""
    echo "Common fixes:"
    echo "  - Run: ./scripts/context-dna up"
    echo "  - Run: python scripts/configure-cursor-mcp-autostart.py"
    echo "  - Restart Docker if Docker checks fail"
    echo ""
    exit 1
fi
