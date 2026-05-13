#!/usr/bin/env bash
# fleet-node-readiness.sh — verify each fleet node has operational fallback
# Checks: Claude Code, Codex config, Superset API key, fleet daemon, OmniRoute
#
# Usage: bash scripts/fleet-node-readiness.sh [node_id]
#   No args = check local node
#   node_id = check remote node via SSH

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s)}"
TARGET="${1:-local}"

if [ -f "$SCRIPT_DIR/read-secret.sh" ]; then
    # shellcheck source=scripts/read-secret.sh
    source "$SCRIPT_DIR/read-secret.sh"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

check() {
    if eval "$1" >/dev/null 2>&1; then
        pass "$2"
        ((PASS_COUNT++))
    else
        fail "$2"
        ((FAIL_COUNT++))
    fi
}

check_warn() {
    if eval "$1" >/dev/null 2>&1; then
        pass "$2"
        ((PASS_COUNT++))
    else
        warn "$2 (optional)"
        ((WARN_COUNT++))
    fi
}

has_secret() {
    local key_name
    for key_name in "$@"; do
        if [ -n "${!key_name:-}" ]; then
            return 0
        fi
        if command -v read_secret >/dev/null 2>&1; then
            if [ -n "$(read_secret "$key_name" 2>/dev/null || true)" ]; then
                return 0
            fi
        fi
    done
    return 1
}

echo "=== Fleet Node Readiness Check ==="
echo "Node: $NODE_ID | Target: $TARGET"
echo ""

if [ "$TARGET" != "local" ]; then
    # Remote check via SSH
    CONFIG="$REPO_ROOT/.multifleet/config.json"
    if [ ! -f "$CONFIG" ]; then
        fail "Fleet config not found: $CONFIG"
        exit 1
    fi
    HOST=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c['nodes']['$TARGET']['host'])" 2>/dev/null || echo "")
    USER=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c['nodes']['$TARGET'].get('user','aarontjomsland'))" 2>/dev/null || echo "aarontjomsland")
    if [ -z "$HOST" ]; then
        fail "Unknown node: $TARGET"
        exit 1
    fi
    echo "Remote: $USER@$HOST"
    echo ""

    # Copy this script and run remotely
    ssh -o ConnectTimeout=5 "$USER@$HOST" "bash -s" < "$0" <<< "local"
    exit $?
fi

# ── Local checks ──

echo "1. Claude Code (primary IDE)"
check "command -v claude" "claude CLI installed"
check_warn "pgrep -qf 'claude' 2>/dev/null" "Claude Code process running"

echo ""
echo "2. Codex CLI (OpenAI fallback)"
check_warn "command -v codex" "codex CLI installed"
if [ -f "$HOME/.codex/config.toml" ]; then
    pass "Codex global config exists"
    ((PASS_COUNT++))
    # Check for fleet-nerve MCP server
    if grep -q "fleet-nerve" "$HOME/.codex/config.toml" 2>/dev/null; then
        pass "Fleet-nerve MCP server configured in Codex"
        ((PASS_COUNT++))
    else
        warn "Fleet-nerve MCP not in Codex config (optional)"
        ((WARN_COUNT++))
    fi
elif [ -f "$REPO_ROOT/.codex/config.toml" ]; then
    pass "Codex project config exists"
    ((PASS_COUNT++))
else
    warn "No Codex config found (optional fallback)"
    ((WARN_COUNT++))
fi

echo ""
echo "3. 3-Surgeons plugin"
check_warn "command -v 3s || [ -f $HOME/.3surgeons/plugin/bin/3surgeons-mcp ]" "3-Surgeons installed"
check_warn "has_secret FLEET_OPENAI_API_KEY Context_DNA_OPENAI OPENAI_API_KEY" \
    "OpenAI API key accessible (env or secure store)"

echo ""
echo "4. Superset (backup runtime)"
check_warn "has_secret SUPERSET_API_KEY Superset_contextdna_key" \
    "Superset API key accessible"

echo ""
echo "5. Fleet daemon"
check "curl -sf http://127.0.0.1:8855/health" "Fleet daemon running on :8855"

echo ""
echo "6. OmniRoute state"
if [ -f /tmp/omniroute-state.json ]; then
    TIER=$(python3 -c "import json; print(json.load(open('/tmp/omniroute-state.json')).get('active_tier',1))" 2>/dev/null || echo "?")
    pass "OmniRoute state file exists (active tier: $TIER)"
    ((PASS_COUNT++))
else
    warn "No OmniRoute state file (will init on first use)"
    ((WARN_COUNT++))
fi

echo ""
echo "7. Power state"
if [ -f /tmp/fleet-power-state.json ]; then
    STATE=$(python3 -c "import json; print(json.load(open('/tmp/fleet-power-state.json')).get('state','?'))" 2>/dev/null || echo "?")
    pass "Power state: $STATE"
    ((PASS_COUNT++))
else
    warn "No power state file (defaults to warm-idle)"
    ((WARN_COUNT++))
fi

echo ""
echo "8. LLM providers"
check_warn "[ -n \"${ANTHROPIC_API_KEY:-}\" ]" "ANTHROPIC_API_KEY set"
check_warn "[ -n \"${DEEPSEEK_API_KEY:-}\" ] || [ -n \"${Context_DNA_Deepseek:-}\" ]" "DeepSeek API key set"
check_warn "curl -sf http://127.0.0.1:5044/v1/models" "Local MLX server running"

echo ""
echo "9. Git + secrets protection"
check "git -C '$REPO_ROOT' status" "Git repo accessible"
# Verify no secrets in recent commits
if git -C "$REPO_ROOT" log -5 --diff-filter=A --name-only --pretty=format: | grep -qiE '\.env$|credentials|secret|api.key'; then
    fail "Potential secret file in recent commits"
    ((FAIL_COUNT++))
else
    pass "No secret files in recent 5 commits"
    ((PASS_COUNT++))
fi

echo ""
echo "=== Results ==="
echo -e "${GREEN}Pass: $PASS_COUNT${NC} | ${RED}Fail: $FAIL_COUNT${NC} | ${YELLOW}Warn: $WARN_COUNT${NC}"

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo -e "${RED}Node NOT fully ready for baton handoff${NC}"
    exit 1
else
    echo -e "${GREEN}Node ready for baton handoff${NC}"
    exit 0
fi
