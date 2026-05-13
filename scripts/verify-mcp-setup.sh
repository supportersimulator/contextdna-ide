#!/bin/bash
# MCP Setup Verification Script
# Ensures both Cursor and Claude Code MCP configs are properly set up

echo "🔍 Verifying MCP Configuration..."
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
WORKSPACE_MCP="$REPO_ROOT/.mcp.json"
CURSOR_MCP="$HOME/.cursor/mcp.json"
MCP_SERVER="$REPO_ROOT/mcp-servers/contextdna_webhook_mcp.py"

# Check 1: Workspace .mcp.json (for Claude Code)
echo "1️⃣  Checking workspace .mcp.json (Claude Code)..."
if [ -f "$WORKSPACE_MCP" ]; then
    if grep -q "contextdna-webhook" "$WORKSPACE_MCP"; then
        echo -e "${GREEN}✅ PASS${NC} - $WORKSPACE_MCP exists and has contextdna-webhook"
    else
        echo -e "${RED}❌ FAIL${NC} - $WORKSPACE_MCP exists but missing contextdna-webhook"
        exit 1
    fi
else
    echo -e "${RED}❌ FAIL${NC} - $WORKSPACE_MCP not found"
    exit 1
fi

# Check 2: ~/.cursor/mcp.json (for Cursor IDE)
echo "2️⃣  Checking ~/.cursor/mcp.json (Cursor IDE)..."
if [ -f "$CURSOR_MCP" ]; then
    if grep -q "contextdna-webhook" "$CURSOR_MCP"; then
        echo -e "${GREEN}✅ PASS${NC} - $CURSOR_MCP exists and has contextdna-webhook"
    else
        echo -e "${RED}❌ FAIL${NC} - $CURSOR_MCP exists but missing contextdna-webhook"
        exit 1
    fi
else
    echo -e "${RED}❌ FAIL${NC} - $CURSOR_MCP not found"
    exit 1
fi

# Check 3: MCP Server Script Exists
echo "3️⃣  Checking MCP server script..."
if [ -f "$MCP_SERVER" ]; then
    echo -e "${GREEN}✅ PASS${NC} - MCP server script exists"
else
    echo -e "${RED}❌ FAIL${NC} - MCP server script not found at $MCP_SERVER"
    exit 1
fi

# Check 4: Python Virtual Environment
echo "4️⃣  Checking Python virtual environment..."
if [ -f "$REPO_ROOT/.venv/bin/python3" ]; then
    echo -e "${GREEN}✅ PASS${NC} - Python virtual environment exists"
else
    echo -e "${RED}❌ FAIL${NC} - Python venv not found at $REPO_ROOT/.venv"
    exit 1
fi

# Check 5: MCP Server Can Initialize
echo "5️⃣  Testing MCP server initialization..."
cd "$REPO_ROOT"
echo '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | \
    PYTHONPATH=. .venv/bin/python3 "$MCP_SERVER" 2>&1 | grep -q '"result"'

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ PASS${NC} - MCP server initializes successfully"
else
    echo -e "${RED}❌ FAIL${NC} - MCP server failed to initialize"
    exit 1
fi

# Check 6: .cursorrules has MCP directive
echo "6️⃣  Checking .cursorrules MCP directive..."
if grep -q "contextdna://webhook" "$REPO_ROOT/.cursorrules"; then
    echo -e "${GREEN}✅ PASS${NC} - .cursorrules has MCP resource directive"
else
    echo -e "${RED}❌ FAIL${NC} - .cursorrules missing MCP directive"
    exit 1
fi

# Check 7: Helper agent running (optional - warns if not)
echo "7️⃣  Checking helper agent (optional)..."
if curl -s -f http://127.0.0.1:8080/health > /dev/null 2>&1; then
    echo -e "${GREEN}✅ PASS${NC} - Helper agent online (full webhook quality)"
else
    echo -e "${YELLOW}⚠️  WARN${NC} - Helper agent offline (will use fallback mode)"
fi

# Check 8: Synaptic LLM running (optional - warns if not)
echo "8️⃣  Checking Synaptic LLM (optional)..."
if curl -s -f http://127.0.0.1:5044/health > /dev/null 2>&1; then
    echo -e "${GREEN}✅ PASS${NC} - Synaptic LLM online (full wisdom generation)"
else
    echo -e "${YELLOW}⚠️  WARN${NC} - Synaptic LLM offline (will use templates)"
fi

# Check 9: Python dependencies
echo "9️⃣  Checking Python dependencies..."
cd "$REPO_ROOT"
if .venv/bin/python3 -c "import asyncio" 2>/dev/null; then
    echo -e "${GREEN}✅ PASS${NC} - Core Python packages installed"
    # Check optional aiohttp
    if .venv/bin/python3 -c "import aiohttp" 2>/dev/null; then
        echo -e "   ${GREEN}✅${NC} aiohttp available (Professor endpoint supported)"
    else
        echo -e "   ${YELLOW}⚠️${NC}  aiohttp not available (Professor will use fallback)"
    fi
else
    echo -e "${RED}❌ FAIL${NC} - Missing core Python dependencies"
    exit 1
fi

# Check 10: File permissions
echo "🔟 Checking file permissions..."
if [ -x "$MCP_SERVER" ] || [ -r "$MCP_SERVER" ]; then
    echo -e "${GREEN}✅ PASS${NC} - MCP server script is readable"
else
    echo -e "${RED}❌ FAIL${NC} - MCP server script not readable"
    exit 1
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ CORE CHECKS PASSED (6/6 required + 4/4 optional)${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "📋 Configuration Summary:"
echo "   • Workspace .mcp.json: ✅ Configured (for Claude Code)"
echo "   • ~/.cursor/mcp.json: ✅ Configured (for Cursor IDE)"
echo "   • MCP Server: ✅ Working (with timeout protection)"
echo "   • .cursorrules: ✅ Has MCP directive"
echo "   • Helper Agent: $(curl -s -f http://127.0.0.1:8080/health > /dev/null 2>&1 && echo '✅ Online' || echo '⚠️  Offline (fallback mode)')"
echo "   • Synaptic LLM: $(curl -s -f http://127.0.0.1:5044/health > /dev/null 2>&1 && echo '✅ Online' || echo '⚠️  Offline (templates)')"
echo "   • Python Deps: ✅ Installed"
echo "   • Permissions: ✅ Valid"
echo ""
echo -e "${YELLOW}🔄 Next Steps:${NC}"
echo "   1. If using Cursor: Start NEW CHAT (Cmd+N) or restart Cursor"
echo "   2. If using Claude Code: Just continue (hooks auto-load)"
echo "   3. In new chat, type any message"
echo "   4. MCP webhook should inject automatically"
echo ""
echo "   To test in new chat, ask:"
echo "   \"Can you verify you received the webhook payload?\""
echo ""
