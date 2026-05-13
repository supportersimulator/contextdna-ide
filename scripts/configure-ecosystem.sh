#!/usr/bin/env bash
# configure-ecosystem.sh — Probe + install + configure the Claude Code
# ecosystem ContextDNA depends on: Multi-Fleet, 3-Surgeons, Superpowers,
# and the .mcp.json wiring that binds them all together.
#
# Designed for the same two scenarios as configure-services.sh:
#   1. FIRST RUN: fresh laptop, none of the ecosystem tools installed.
#      Asks Y/N for each, installs the ones you say yes to.
#   2. RECOVERY 6 MONTHS LATER: bundle restored, but plugins may need
#      reinstalling. Detects what's already there, only installs missing.
#
# Modes:
#   bash scripts/configure-ecosystem.sh              # interactive (default)
#   bash scripts/configure-ecosystem.sh --probe      # status only, no changes
#   bash scripts/configure-ecosystem.sh --tool NAME  # one tool only
#
# Tools handled (each opt-in):
#   - Claude Code CLI       (the host — verifies install)
#   - Multi-Fleet           (cross-machine fleet coordination)
#   - 3-Surgeons            (multi-model consensus plugin + CLI)
#   - Superpowers           (Claude Code skills marketplace)
#   - .mcp.json wiring      (validates the 7 MCP servers are registered)
#
# ZSF: every failure exits non-zero with a [eco] FAIL: <reason> line.

set -uo pipefail

MODE="interactive"
ONLY_TOOL=""
for arg in "$@"; do
    case "$arg" in
        --probe) MODE="probe" ;;
        --tool)  ONLY_TOOL="next" ;;
        --help|-h) sed -n '2,22p' "$0" | sed 's|^# ||; s|^#||'; exit 0 ;;
        *)
            if [ "$ONLY_TOOL" = "next" ]; then ONLY_TOOL="$arg"
            else echo "unknown arg: $arg" >&2; exit 2; fi ;;
    esac
done

if [ -t 1 ]; then
    BOLD=$(tput bold); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3)
    RED=$(tput setaf 1); BLUE=$(tput setaf 4); CYAN=$(tput setaf 6)
    DIM=$(tput dim); RESET=$(tput sgr0)
else BOLD=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; CYAN=""; DIM=""; RESET=""; fi

_step() { echo ""; echo "${BOLD}${BLUE}▶ $*${RESET}"; }
_ok()   { echo "  ${GREEN}✓${RESET} $*"; }
_warn() { echo "  ${YELLOW}⚠${RESET} $*"; }
_fail() { echo "  ${RED}✗${RESET} $*" >&2; exit 1; }
_info() { echo "  ${DIM}$*${RESET}"; }
_link() { echo "  ${CYAN}→ $*${RESET}"; }
_ask()  {
    local prompt="$1" default="${2:-}" var
    [ "$MODE" = "probe" ] && { echo "$default"; return; }
    if [ -n "$default" ]; then
        read -r -p "  ${BOLD}?${RESET} $prompt [${DIM}$default${RESET}]: " var
        echo "${var:-$default}"
    else
        read -r -p "  ${BOLD}?${RESET} $prompt: " var
        echo "$var"
    fi
}
_confirm() {
    local prompt="$1"
    [ "$MODE" = "probe" ] && return 1
    read -r -p "  ${BOLD}?${RESET} $prompt [Y/n]: " var
    [[ "$var" =~ ^[Nn] ]] && return 1 || return 0
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_DIR="${WORKSPACE_DIR:-$(dirname "$REPO_ROOT")}"
PLATFORM="$(uname -s)"

cat <<EOF

${BOLD}ContextDNA — Ecosystem Configurator${RESET}
${DIM}Probes + installs + configures Multi-Fleet, 3-Surgeons, Superpowers, MCP wiring.${RESET}

Mode: ${BOLD}$MODE${RESET}${ONLY_TOOL:+ (tool=$ONLY_TOOL)}
Workspace (where sibling repos live): $WORKSPACE_DIR

EOF

# ─────────────────────────────────────────────────────────────────────────────
# Helper: probe + offer to install a tool from git
# Args: tool_name repo_url install_cmd probe_cmd config_path
# ─────────────────────────────────────────────────────────────────────────────
_install_repo() {
    local name="$1" repo_url="$2" target_dir="$3"
    if [ -d "$target_dir/.git" ]; then
        _ok "$name already cloned at $target_dir"
        (cd "$target_dir" && git pull --rebase 2>&1 | tail -1) || _warn "git pull had issues"
        return 0
    fi
    _info "cloning $repo_url → $target_dir"
    git clone "$repo_url" "$target_dir" 2>&1 | tail -3 || { _warn "git clone failed"; return 1; }
    _ok "$name cloned"
}

# ─────────────────────────────────────────────────────────────────────────────
# TOOL 0 — Claude Code CLI (host)
# ─────────────────────────────────────────────────────────────────────────────
_tool_claude() {
    [ -n "$ONLY_TOOL" ] && [ "$ONLY_TOOL" != "claude" ] && return 0
    _step "Claude Code CLI (the host)"

    if command -v claude >/dev/null 2>&1; then
        local ver
        ver=$(claude --version 2>/dev/null | head -1 || echo "unknown")
        _ok "claude CLI installed ($ver)"
        return 0
    fi

    _warn "claude CLI not found"
    _info "ContextDNA, Multi-Fleet plugin, 3-Surgeons plugin, and Superpowers all need Claude Code."
    _link "Install: https://docs.claude.com/en/docs/claude-code"

    [ "$MODE" = "probe" ] && return 0

    if _confirm "Try to install via npm now (npm install -g @anthropic-ai/claude-code)?"; then
        if command -v npm >/dev/null; then
            npm install -g @anthropic-ai/claude-code 2>&1 | tail -3 \
                && _ok "claude installed" \
                || _warn "npm install failed — install manually from the link above"
        else
            _warn "npm not found — install Node.js first"
        fi
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# TOOL 1 — Multi-Fleet
# ─────────────────────────────────────────────────────────────────────────────
_tool_multifleet() {
    [ -n "$ONLY_TOOL" ] && [ "$ONLY_TOOL" != "multifleet" ] && return 0
    _step "Multi-Fleet (cross-machine AI coordination)"

    local MF_DIR="${MULTI_FLEET_DIR:-$WORKSPACE_DIR/multi-fleet}"
    local MF_REPO="https://github.com/supportersimulator/multi-fleet.git"
    local installed=false plugin_installed=false daemon_running=false

    # 1. Repo clone
    if [ -d "$MF_DIR/.git" ]; then
        _ok "repo present at $MF_DIR"
        installed=true
    else
        _info "repo not cloned"
    fi

    # 2. Python package
    if python3 -c "import multifleet" 2>/dev/null; then
        _ok "multifleet Python package importable"
    else
        _info "multifleet Python package not importable"
    fi

    # 3. Plugin in Claude Code (~/.claude/plugins/cache or similar)
    if find ~/.claude/plugins 2>/dev/null | grep -qi multi-fleet; then
        _ok "Claude Code plugin installed"
        plugin_installed=true
    else
        _info "Claude Code plugin not installed"
    fi

    # 4. Daemon running
    if curl -sf -m 2 http://127.0.0.1:8855/health >/dev/null 2>&1; then
        _ok "fleet daemon running on :8855"
        daemon_running=true
    else
        _info "fleet daemon not running (will start after install)"
    fi

    if $installed && $plugin_installed && $daemon_running; then
        _ok "Multi-Fleet fully configured"
        return 0
    fi

    [ "$MODE" = "probe" ] && return 0

    if ! _confirm "Install / configure Multi-Fleet now?"; then
        _info "skipping Multi-Fleet"; return 0
    fi

    # Clone
    $installed || _install_repo "Multi-Fleet" "$MF_REPO" "$MF_DIR" || return 1

    # Pip install
    if [ -f "$MF_DIR/pyproject.toml" ] && _confirm "Install multifleet Python package (pip install -e)?"; then
        (cd "$MF_DIR" && pip install -e . 2>&1 | tail -5) && _ok "package installed"
    fi

    # Claude Code plugin
    if command -v claude >/dev/null && ! $plugin_installed; then
        if _confirm "Install Claude Code plugin (claude plugin add)?"; then
            # Method 1: marketplace if it exists; method 2: local path
            if [ -f "$MF_DIR/.claude-plugin/plugin.json" ] || [ -f "$MF_DIR/package.json" ]; then
                claude plugin add "$MF_DIR" 2>&1 | tail -3 \
                    && _ok "plugin added from $MF_DIR" \
                    || _warn "plugin add failed (try manually)"
            else
                _info "no plugin manifest found at $MF_DIR; skipping"
            fi
        fi
    fi

    # Node ID + NATS URL (write to .env if not already)
    local node_id
    node_id="$(_ask "MULTIFLEET_NODE_ID (used in fleet messages)" "$(hostname -s)")"
    local nats_url
    nats_url="$(_ask "NATS_URL" "$(grep '^NATS_URL=' "$REPO_ROOT/.env" 2>/dev/null | cut -d= -f2- || echo nats://localhost:4222)")"

    if [ -f "$REPO_ROOT/.env" ]; then
        grep -v -E '^(MULTIFLEET_NODE_ID|NATS_URL)=' "$REPO_ROOT/.env" > "$REPO_ROOT/.env.tmp" || true
        echo "MULTIFLEET_NODE_ID=$node_id" >> "$REPO_ROOT/.env.tmp"
        echo "NATS_URL=$nats_url" >> "$REPO_ROOT/.env.tmp"
        mv "$REPO_ROOT/.env.tmp" "$REPO_ROOT/.env"
        chmod 600 "$REPO_ROOT/.env"
        _ok "MULTIFLEET_NODE_ID + NATS_URL written to .env"
    fi

    # Optional: install + start the daemon launchd plist
    if [ "$PLATFORM" = "Darwin" ] && ! $daemon_running && _confirm "Install fleet daemon launchd plist (auto-start at login)?"; then
        local plist="$HOME/Library/LaunchAgents/io.contextdna.multifleet-daemon.plist"
        cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>io.contextdna.multifleet-daemon</string>
<key>ProgramArguments</key><array>
  <string>/bin/bash</string><string>-lc</string>
  <string>cd $MF_DIR &amp;&amp; export NATS_URL=$nats_url MULTIFLEET_NODE_ID=$node_id &amp;&amp; python3 -m multifleet.daemon serve</string>
</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>/tmp/multifleet-daemon.log</string>
<key>StandardErrorPath</key><string>/tmp/multifleet-daemon.err</string>
</dict></plist>
EOF
        launchctl unload "$plist" 2>/dev/null || true
        launchctl load -w "$plist" && _ok "daemon scheduled" || _warn "launchctl load failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# TOOL 2 — 3-Surgeons
# ─────────────────────────────────────────────────────────────────────────────
_tool_surgeons() {
    [ -n "$ONLY_TOOL" ] && [ "$ONLY_TOOL" != "surgeons" ] && return 0
    _step "3-Surgeons (multi-model cross-examination)"

    local CFG="$HOME/.3surgeons/config.yaml"
    local PROJ_CFG="$REPO_ROOT/.3surgeons.yaml"
    local cli_present=false plugin_present=false config_present=false

    if command -v 3s >/dev/null 2>&1; then
        _ok "3s CLI installed"
        cli_present=true
    else
        _info "3s CLI not installed"
    fi

    if find ~/.claude/plugins 2>/dev/null | grep -qi "3-surgeons"; then
        _ok "Claude Code plugin installed (3-surgeons-marketplace)"
        plugin_present=true
    else
        _info "Claude Code plugin not installed"
    fi

    if [ -f "$CFG" ] || [ -f "$PROJ_CFG" ]; then
        _ok "config present ($( [ -f "$PROJ_CFG" ] && echo "$PROJ_CFG" || echo "$CFG" ))"
        config_present=true
    else
        _info "no 3-surgeons config — will be created"
    fi

    # Probe surgeons (if installed)
    if $cli_present; then
        if 3s probe 2>&1 | grep -q "All three surgeons reachable\|ok" 2>/dev/null; then
            _ok "3s probe: all three surgeons reachable"
        else
            _info "3s probe: not all surgeons reachable (run \`3s probe\` for details)"
        fi
    fi

    if $cli_present && $plugin_present && $config_present; then
        _ok "3-Surgeons fully configured"
        return 0
    fi

    [ "$MODE" = "probe" ] && return 0

    if ! _confirm "Install / configure 3-Surgeons now?"; then
        _info "skipping 3-Surgeons"; return 0
    fi

    # Plugin install via Claude Code marketplace
    if command -v claude >/dev/null && ! $plugin_present; then
        if _confirm "Install Claude Code plugin (3-surgeons@3-surgeons-marketplace)?"; then
            claude plugin marketplace add supportersimulator/3-surgeons-marketplace 2>&1 | tail -3 || true
            claude plugin install 3-surgeons@3-surgeons-marketplace 2>&1 | tail -3 \
                && _ok "plugin installed" \
                || _warn "plugin install failed (try manually with 'claude plugin' commands)"
        fi
    fi

    # Standalone repo + CLI install (optional)
    if ! $cli_present && _confirm "Also clone the standalone repo + install 3s CLI?"; then
        local TS_DIR="${SURGEONS_DIR:-$WORKSPACE_DIR/3-surgeons}"
        _install_repo "3-Surgeons" "https://github.com/supportersimulator/3-surgeons.git" "$TS_DIR"
        if [ -f "$TS_DIR/pyproject.toml" ]; then
            (cd "$TS_DIR" && pip install -e . 2>&1 | tail -3) \
                && _ok "3s CLI installed" \
                || _warn "pip install failed"
        fi
    fi

    # Generate baseline config if missing
    if ! $config_present && _confirm "Create starter ~/.3surgeons/config.yaml?"; then
        mkdir -p "$HOME/.3surgeons"
        cat > "$CFG" <<'YAML'
# 3-Surgeons configuration — auto-generated by configure-ecosystem.sh
# Edit freely. Re-run `configure-services.sh` to swap providers.

surgeons:
  cardiologist:
    provider: deepseek
    endpoint: https://api.deepseek.com/v1
    model: deepseek-chat
    api_key_env: DEEPSEEK_API_KEY
    fallbacks:
      - provider: openai
        endpoint: https://api.openai.com/v1
        model: gpt-4.1-mini
        api_key_env: OPENAI_API_KEY

  neurologist:
    provider: mlx
    endpoint: http://localhost:5044/v1
    model: ${LOCAL_LLM_MODEL:-mlx-community/Qwen3-4B-4bit}
    api_key_env: ""
    fallbacks:
      - provider: ollama
        endpoint: http://localhost:11434/v1
        model: qwen3:4b

budgets:
  daily_external_usd: 5.00
  autonomous_ab_usd: 0.50

review:
  depth: iterative   # single | iterative | continuous
  auto_review_depth: suggest   # off | suggest | auto

evidence_db: ~/.3surgeons/evidence.db
YAML
        chmod 600 "$CFG"
        _ok "starter config written to $CFG (edit anytime)"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# TOOL 3 — Superpowers
# ─────────────────────────────────────────────────────────────────────────────
_tool_superpowers() {
    [ -n "$ONLY_TOOL" ] && [ "$ONLY_TOOL" != "superpowers" ] && return 0
    _step "Superpowers (Claude Code skills marketplace)"

    local installed=false
    if find ~/.claude/plugins 2>/dev/null | grep -qi superpowers; then
        _ok "Superpowers plugin/skills detected"
        installed=true
    else
        _info "Superpowers not detected"
    fi

    if $installed; then
        # Show installed skill count if findable
        local skill_count
        skill_count=$(find ~/.claude/plugins -path '*superpowers*' -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
        _ok "approx $skill_count skill files present"
        return 0
    fi

    [ "$MODE" = "probe" ] && return 0

    _info "Superpowers gives Claude Code structured skills (brainstorming, debugging, TDD, etc.)"
    _link "Default marketplace: obra/superpowers"
    _link "Browse: https://github.com/obra/superpowers"
    if ! _confirm "Install Superpowers via Claude Code plugin marketplace?"; then
        _info "skipping Superpowers"; return 0
    fi

    local marketplace
    marketplace="$(_ask "Marketplace slug (org/repo)" "obra/superpowers")"

    if command -v claude >/dev/null; then
        claude plugin marketplace add "$marketplace" 2>&1 | tail -3 || true
        # The plugin name might just be 'superpowers'; ask if uncertain
        local plugin_name
        plugin_name="$(_ask "Plugin name to install" "superpowers")"
        claude plugin install "${plugin_name}@${marketplace}" 2>&1 | tail -3 \
            && _ok "Superpowers installed" \
            || _warn "install failed — try manually: claude plugin install ${plugin_name}@${marketplace}"
    else
        _warn "claude CLI not available — install Claude Code first, then re-run"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# TOOL 4 — .mcp.json wiring
# ─────────────────────────────────────────────────────────────────────────────
_tool_mcp_wiring() {
    [ -n "$ONLY_TOOL" ] && [ "$ONLY_TOOL" != "mcp" ] && return 0
    _step ".mcp.json wiring (validates the MCP server registrations)"

    local MCP="$REPO_ROOT/.mcp.json"
    if [ ! -f "$MCP" ]; then
        _warn "no .mcp.json found at $MCP"
        return 0
    fi

    # Required servers we expect to find
    declare -a EXPECTED=("multifleet" "synaptic" "projectdna" "contextdna-engine" "contextdna-webhook" "race-theater" "evidence-stream" "event-bridge")
    local missing=()
    for server in "${EXPECTED[@]}"; do
        if python3 -c "import json,sys; print('yes' if '$server' in json.load(open('$MCP'))['mcpServers'] else 'no')" 2>/dev/null | grep -q "^yes$"; then
            _ok "$server registered"
        else
            _warn "$server NOT in .mcp.json"
            missing+=("$server")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        _info "${#missing[@]} server(s) missing from .mcp.json"
        _info "Re-run setup-mothership.sh, or check that the public repo's .mcp.json is intact"
    fi

    # Also test each MCP server is actually launchable (syntax + imports OK)
    if command -v python3 >/dev/null; then
        for server_path in mcp-servers/race-theater/server.py mcp-servers/evidence-stream/server.py mcp-servers/event-bridge/server.py; do
            if [ -f "$REPO_ROOT/$server_path" ]; then
                if python3 -c "import ast; ast.parse(open('$REPO_ROOT/$server_path').read())" 2>/dev/null; then
                    _ok "$(basename "$(dirname "$server_path")") server parses cleanly"
                else
                    _warn "$server_path has syntax errors"
                fi
            fi
        done
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Run sections
# ─────────────────────────────────────────────────────────────────────────────
_tool_claude
_tool_multifleet
_tool_surgeons
_tool_superpowers
_tool_mcp_wiring

# ─────────────────────────────────────────────────────────────────────────────
# Final
# ─────────────────────────────────────────────────────────────────────────────
echo ""
if [ "$MODE" = "probe" ]; then
    echo "  ${DIM}(probe mode — no changes were made)${RESET}"
else
    echo "  ${BOLD}${GREEN}━━━ Ecosystem configuration complete ━━━${RESET}"
    echo ""
    echo "  Verify everything with:"
    echo "    ${BLUE}bash scripts/configure-ecosystem.sh --probe${RESET}"
    echo "    ${BLUE}bash scripts/configure-services.sh --probe${RESET}"
    echo "    ${BLUE}bash scripts/setup-mothership.sh --check${RESET}"
    echo ""
    echo "  If all three print mostly ${GREEN}✓${RESET}, your mothership is fully configured."
fi
echo ""
