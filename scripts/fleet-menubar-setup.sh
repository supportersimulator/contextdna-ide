#!/usr/bin/env bash
# fleet-menubar-setup.sh — One-command fleet menu bar setup for any node
#
# Usage:
#   bash scripts/fleet-menubar-setup.sh          # Interactive (detects node)
#   bash scripts/fleet-menubar-setup.sh mac2     # Explicit node ID
#
# Installs:
#   1. xbar (if missing)
#   2. Fleet status menu bar plugin (F:N indicator)
#   3. NATS server LaunchAgent (auto-start, KeepAlive)
#   4. Fleet daemon LaunchAgent (auto-start, KeepAlive)
#   5. Secrets via macOS Keychain (never in plists, env files, or git)
#
# Secrets: All API keys stored in macOS Keychain under service "fleet-nerve".
#          LaunchAgent plists contain ZERO secrets. Daemon reads from Keychain at runtime.
#          Env file (~/.fleet-nerve/env) holds non-secret config only.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── Colors ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
step() { echo -e "\n${CYAN}[$1]${NC} $2"; }

# ── Detect node ID (config-driven, no hard-coded names) ──
# shellcheck source=/dev/null
source "$REPO_ROOT/scripts/fleet-node-id.sh"

NODE_ID="${1:-$(fleet_node_id)}"
PYTHON="$(which python3)"
NATS_SERVER="$(which nats-server 2>/dev/null || echo "")"
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1")

echo ""
echo "Fleet Menu Bar Setup"
echo "  Node:   $NODE_ID"
echo "  Repo:   $REPO_ROOT"
echo "  Python: $PYTHON"
echo "  LAN IP: $LAN_IP"

# ══════════════════════════════════════════════════════════════════════
# Step 1: xbar
# ══════════════════════════════════════════════════════════════════════
step "1/5" "xbar app"

XBAR_PLUGINS="$HOME/Library/Application Support/xbar/plugins"
if [[ -d "$XBAR_PLUGINS" ]]; then
    ok "xbar installed"
elif command -v brew &>/dev/null; then
    warn "Installing xbar via Homebrew..."
    brew install --cask xbar
    # xbar creates plugin dir on first launch
    mkdir -p "$XBAR_PLUGINS"
    ok "xbar installed"
else
    fail "xbar not found and brew unavailable"
    echo "    Install manually: https://xbarapp.com"
    echo "    Then re-run this script"
    exit 1
fi

# ══════════════════════════════════════════════════════════════════════
# Step 2: Fleet status plugin
# ══════════════════════════════════════════════════════════════════════
step "2/5" "Fleet status plugin"

PLUGIN_SRC="$REPO_ROOT/scripts/fleet-status-xbar.sh"
PLUGIN_DST="$XBAR_PLUGINS/fleet-status.5m.sh"

if [[ ! -f "$PLUGIN_SRC" ]]; then
    fail "Plugin source not found at $PLUGIN_SRC"
    exit 1
fi

# Remove old .py version if present (xbar beta had parse issues with unicode)
rm -f "$XBAR_PLUGINS/fleet-status.5m.py" "$XBAR_PLUGINS/fleet-status.5m.py.disabled"

cp "$PLUGIN_SRC" "$PLUGIN_DST"
chmod +x "$PLUGIN_DST"
ok "Fleet plugin installed → $PLUGIN_DST"

# Also install context-dna brain plugin (one unified version, not duplicates)
CDNA_SRC="$REPO_ROOT/context-dna/clients/xbar/context-dna.1m.py"
CDNA_DST="$XBAR_PLUGINS/context-dna.1m.py"

if [[ -f "$CDNA_SRC" ]]; then
    # Remove old disabled variants that xbar beta 2.1.7 runs anyway
    rm -f "$XBAR_PLUGINS/context-dna.2m.sh.disabled" \
          "$XBAR_PLUGINS/context-dna.5m.sh.disabled" \
          "$XBAR_PLUGINS/context-dna."*.disabled
    # Remove any stale symlinks so we install the real file
    [[ -L "$CDNA_DST" ]] && rm -f "$CDNA_DST"
    cp "$CDNA_SRC" "$CDNA_DST"
    chmod +x "$CDNA_DST"
    ok "Context DNA plugin installed → $CDNA_DST (brain icon)"
else
    warn "Context DNA plugin source not found at $CDNA_SRC (skipped)"
fi

# ══════════════════════════════════════════════════════════════════════
# Step 3: NATS server
# ══════════════════════════════════════════════════════════════════════
step "3/5" "NATS server"

if [[ -z "$NATS_SERVER" ]]; then
    if command -v brew &>/dev/null; then
        warn "Installing nats-server via Homebrew..."
        brew install nats-server
        NATS_SERVER="$(which nats-server)"
        ok "nats-server installed"
    else
        fail "nats-server not found and brew unavailable"
        echo "    Install: brew install nats-server"
        exit 1
    fi
else
    ok "nats-server found: $NATS_SERVER"
fi

NATS_PLIST="$HOME/Library/LaunchAgents/io.contextdna.nats-server.plist"

cat > "$NATS_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.contextdna.nats-server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$NATS_SERVER</string>
        <string>-p</string>
        <string>4222</string>
        <string>-m</string>
        <string>8222</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/nats-server.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/nats-server.log</string>
</dict>
</plist>
EOF

launchctl unload "$NATS_PLIST" 2>/dev/null || true
launchctl load "$NATS_PLIST"
sleep 1

if lsof -ti:4222 &>/dev/null; then
    ok "NATS server running on :4222"
else
    warn "NATS server may still be starting..."
fi

# ══════════════════════════════════════════════════════════════════════
# Step 4: Fleet daemon
# ══════════════════════════════════════════════════════════════════════
step "4/5" "Fleet daemon"

FLEET_PLIST="$HOME/Library/LaunchAgents/io.contextdna.fleet-nats.plist"
FLEET_LOG="/tmp/fleet-nats.log"

# NATS URL: connect to local server, which peers discover via LAN broadcast
NATS_URL="nats://127.0.0.1:4222"

# Plist contains ZERO secrets — only node ID and paths
cat > "$FLEET_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.contextdna.fleet-nats</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$REPO_ROOT/tools/fleet_nerve_nats.py</string>
        <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
        <key>MULTIFLEET_NODE_ID</key>
        <string>$NODE_ID</string>
        <key>NATS_URL</key>
        <string>$NATS_URL</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$FLEET_LOG</string>
    <key>StandardErrorPath</key>
    <string>$FLEET_LOG</string>
</dict>
</plist>
EOF

launchctl unload "$FLEET_PLIST" 2>/dev/null || true
sleep 1
launchctl load "$FLEET_PLIST"
sleep 2

if curl -sf http://127.0.0.1:8855/health &>/dev/null; then
    ok "Fleet daemon running on :8855 (node=$NODE_ID)"
else
    warn "Fleet daemon starting... check: tail -f $FLEET_LOG"
fi

# Purge stale polling injector if present (event-driven stack replaces it)
STALE_AUTO="$HOME/Library/LaunchAgents/io.contextdna.fleet-auto-loop.plist"
if [ -f "$STALE_AUTO" ]; then
    launchctl unload "$STALE_AUTO" 2>/dev/null || true
    mv "$STALE_AUTO" "${STALE_AUTO}.disabled"
    rm -f /tmp/fleet-loop-injected-* 2>/dev/null
    ok "Purged stale fleet-auto-loop (was polling every 60s, burning tokens)"
fi

# ══════════════════════════════════════════════════════════════════════
# Step 5: Secrets (Keychain only — never in plists or git)
# ══════════════════════════════════════════════════════════════════════
step "5/5" "Secrets (macOS Keychain)"

mkdir -p "$HOME/.fleet-nerve"
echo "keychain" > "$HOME/.fleet-nerve/secrets.conf"

# Non-secret config only — ZERO API keys in this file
ENV_FILE="$HOME/.fleet-nerve/env"
cat > "$ENV_FILE" <<ENVEOF
# Fleet Nerve config — secrets stored in macOS Keychain (NOT here)
# Read a secret: security find-generic-password -s "fleet-nerve" -a KEY_NAME -w
# Store a secret: security add-generic-password -s "fleet-nerve" -a KEY_NAME -w "value"
EXTERNAL_LLM_ENDPOINT=https://api.deepseek.com/v1
EXTERNAL_LLM_API_KEY_ENV=Context_DNA_Deepseek
EXTERNAL_LLM_MODEL=deepseek-chat
ENVEOF
chmod 600 "$ENV_FILE"

# Check which keys are already in Keychain
_check_key() {
    if security find-generic-password -s "fleet-nerve" -a "$1" -w &>/dev/null; then
        ok "$1: stored in Keychain"
    else
        warn "$1: not in Keychain"
        echo "    Store: security add-generic-password -s \"fleet-nerve\" -a \"$1\" -w \"your-key\""
    fi
}

_check_key "Context_DNA_OPENAI"
_check_key "Context_DNA_Deepseek"
_check_key "ANTHROPIC_API_KEY"

# If running interactively and keys missing, offer to store them
if [[ -t 0 ]]; then
    for key_name in Context_DNA_OPENAI Context_DNA_Deepseek ANTHROPIC_API_KEY; do
        if ! security find-generic-password -s "fleet-nerve" -a "$key_name" -w &>/dev/null 2>&1; then
            # Check if it's in current env
            env_val="${!key_name:-}"
            if [[ -n "$env_val" ]]; then
                security delete-generic-password -s "fleet-nerve" -a "$key_name" 2>/dev/null || true
                security add-generic-password -s "fleet-nerve" -a "$key_name" -w "$env_val"
                ok "$key_name: imported from environment → Keychain"
            fi
        fi
    done
fi

# ══════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Fleet Menu Bar Setup Complete"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Node:      $NODE_ID"
echo "  Menu bar:  F:N indicator (refreshes every 5m)"
echo "  NATS:      :4222 (auto-start, KeepAlive)"
echo "  Daemon:    :8855 (auto-start, KeepAlive)"
echo "  Secrets:   macOS Keychain (service: fleet-nerve)"
echo "  Config:    $ENV_FILE (non-secret only)"
echo ""
echo "  Verify:  curl -s http://localhost:8855/health | python3 -m json.tool"
echo "  Logs:    tail -f $FLEET_LOG"
echo "  Arbiter: open http://localhost:8855/arbiter"
echo ""
echo "  Security:"
echo "    - Plists contain ZERO secrets"
echo "    - API keys in macOS Keychain only"
echo "    - Env file is chmod 600, non-secret config only"
echo "    - .fleet-nerve/ is in .gitignore"
echo ""
