#!/usr/bin/env bash
# discord-setup-node.sh — Bootstrap the full Discord wake pipeline on any fleet node.
# Idempotent: safe to run multiple times.
#
# Usage:
#   ./scripts/discord-setup-node.sh mac2
#   MULTIFLEET_NODE_ID=mac3 ./scripts/discord-setup-node.sh
#
# Creates/updates:
#   - discord.py pip package
#   - DISCORD_BOT_TOKEN in Keychain (prompts if missing)
#   - LaunchAgent: com.contextdna.discord-bridge (persistent bridge process)
#   - LaunchAgent: com.contextdna.discord-wake (WatchPaths trigger for Claude CLI)
#   - scripts/discord-wake-atlas.sh (wake script)
#   - Sends test message to Discord on success

set -euo pipefail

CHANNEL_ID="1491820715421466865"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
BRIDGE_LABEL="com.contextdna.discord-bridge"
WAKE_LABEL="com.contextdna.discord-wake"

# ── Resolve NODE_ID ──
# Accepts any node id matching ^[a-z][a-z0-9-]{1,31}$ so new nodes
# (mac4, pc-windows, linux-build-01, …) can join via .multifleet/config.json
# without a script edit. Matches multifleet.fleet_config.validate_node_id.
NODE_ID="${1:-${MULTIFLEET_NODE_ID:-}}"
if [ -z "$NODE_ID" ]; then
    echo "ERROR: Provide NODE_ID as arg or set MULTIFLEET_NODE_ID env var"
    echo "Usage: $0 <node-id>   (e.g. mac1, mac4, pc-windows-01)"
    exit 1
fi
if [[ ! "$NODE_ID" =~ ^[a-z][a-z0-9-]{1,31}$ ]]; then
    echo "ERROR: NODE_ID must match ^[a-z][a-z0-9-]{1,31}$ (got: $NODE_ID)"
    echo "       Allowed: lowercase letter start, then letters/digits/dashes."
    exit 1
fi

echo "=== Discord Wake Pipeline Setup for $NODE_ID ==="
echo "Repo: $REPO_DIR"
echo "Channel: $CHANNEL_ID"
echo ""

# ── Allowlist preflight ──
# Phase 4 decision #3: allowlist may come from env vars OR .multifleet/config.json.
# Fail-closed only fires when BOTH sources are empty. Probe the config file so
# this script stops insisting env vars be set when the OSS-friendly config-file
# path is sufficient.
CONFIG_FILE="$REPO_DIR/.multifleet/config.json"
CONFIG_HAS_AUTHORS=0
CONFIG_HAS_GUILDS=0
if [ -f "$CONFIG_FILE" ]; then
    # Don't require jq — lean on python, which is already a hard dep of the bridge.
    CONFIG_HAS_AUTHORS=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(1 if d.get('discord', {}).get('allowed_authors') else 0)
except Exception:
    print(0)
" "$CONFIG_FILE" 2>/dev/null || echo 0)
    CONFIG_HAS_GUILDS=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(1 if d.get('discord', {}).get('allowed_guilds') else 0)
except Exception:
    print(0)
" "$CONFIG_FILE" 2>/dev/null || echo 0)
fi

HAS_ENV_AUTHORS=0; [ -n "${DISCORD_ALLOWED_AUTHORS:-}" ] && HAS_ENV_AUTHORS=1
HAS_ENV_GUILDS=0;  [ -n "${DISCORD_ALLOWED_GUILDS:-}" ]  && HAS_ENV_GUILDS=1

AUTHORS_OK=$(( HAS_ENV_AUTHORS + CONFIG_HAS_AUTHORS ))
GUILDS_OK=$((  HAS_ENV_GUILDS  + CONFIG_HAS_GUILDS  ))

if [ "$AUTHORS_OK" -eq 0 ] || [ "$GUILDS_OK" -eq 0 ]; then
    echo "WARNING: Discord allowlist empty in BOTH sources:"
    echo "           env DISCORD_ALLOWED_AUTHORS/GUILDS unset AND"
    echo "           $CONFIG_FILE lacks discord.allowed_authors/_guilds."
    echo "         Bridge will FAIL-CLOSED and ignore every Discord message."
    echo ""
    echo "         Fix via EITHER:"
    echo "           a) Populate .multifleet/config.json discord.{allowed_authors,allowed_guilds}"
    echo "           b) export DISCORD_ALLOWED_AUTHORS=<snowflake> DISCORD_ALLOWED_GUILDS=<snowflake>"
    echo ""
else
    echo "Allowlist sources: env=${HAS_ENV_AUTHORS}a/${HAS_ENV_GUILDS}g, config=${CONFIG_HAS_AUTHORS}a/${CONFIG_HAS_GUILDS}g"
fi

# ── Step 1: Ensure discord.py installed ──
echo "[1/8] Checking discord.py..."
VENV_PIP="$REPO_DIR/.venv/bin/pip"
VENV_PYTHON="$REPO_DIR/.venv/bin/python3"

if [ ! -x "$VENV_PIP" ]; then
    echo "  WARNING: No .venv found at $REPO_DIR/.venv — trying system pip"
    VENV_PIP="pip3"
    VENV_PYTHON="python3"
fi

if "$VENV_PYTHON" -c "import discord" 2>/dev/null; then
    echo "  discord.py already installed"
else
    echo "  Installing discord.py..."
    "$VENV_PIP" install discord.py -q
    echo "  Installed"
fi

# ── Step 2: Check DISCORD_BOT_TOKEN in Keychain ──
echo "[2/8] Checking DISCORD_BOT_TOKEN in Keychain..."
BOT_TOKEN=$(security find-generic-password -a fleet -s DISCORD_BOT_TOKEN -w 2>/dev/null || true)

if [ -z "$BOT_TOKEN" ]; then
    echo "  No DISCORD_BOT_TOKEN found in Keychain."
    echo ""
    echo "  The fleet shares ONE bot token across all nodes."
    echo "  Get it from another node: security find-generic-password -a fleet -s DISCORD_BOT_TOKEN -w"
    echo ""
    read -rp "  Paste bot token (or Ctrl-C to abort): " BOT_TOKEN
    if [ -z "$BOT_TOKEN" ]; then
        echo "  ERROR: Empty token. Aborting."
        exit 1
    fi
    # -U updates if exists, adds if not
    security add-generic-password -a fleet -s DISCORD_BOT_TOKEN -U -w "$BOT_TOKEN"
    echo "  Stored in Keychain"
else
    echo "  Token found (${#BOT_TOKEN} chars)"
fi

# ── Step 3: Create discord-wake-atlas.sh if missing ──
echo "[3/8] Checking discord-wake-atlas.sh..."
WAKE_SCRIPT="$REPO_DIR/scripts/discord-wake-atlas.sh"
if [ -f "$WAKE_SCRIPT" ]; then
    echo "  Already exists"
else
    echo "  Creating discord-wake-atlas.sh..."
    cat > "$WAKE_SCRIPT" << 'WAKESCRIPT'
#!/usr/bin/env bash
# discord-wake-atlas.sh — Full autonomous loop:
#   Discord message -> Claude CLI -> response -> Discord
#
# Called by LaunchAgent when /tmp/discord-wake-trigger is modified.
# TRIGGER-BASED ONLY. Zero polling. Zero idle cost.

set -euo pipefail

TRIGGER="/tmp/discord-wake-trigger"
SEED_DIR="/tmp"
LOG="/tmp/discord-wake.log"
LOCK="/tmp/discord-wake.lock"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MAX_RESPONSE_CHARS=1800

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# Prevent concurrent runs
if [ -f "$LOCK" ]; then
    LOCK_AGE=$(( $(date +%s) - $(cat "$LOCK" 2>/dev/null || echo 0) ))
    if [ "$LOCK_AGE" -lt 300 ]; then
        log "Locked (age: ${LOCK_AGE}s). Skipping."
        exit 0
    fi
    log "Stale lock (age: ${LOCK_AGE}s). Removing."
fi
date +%s > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

rm -f "$TRIGGER"

shopt -s nullglob
SEEDS=($SEED_DIR/fleet-seed-discord-*.md)
shopt -u nullglob

if [ ${#SEEDS[@]} -eq 0 ]; then
    log "Triggered but no seeds found"
    exit 0
fi

log "Discord wake: ${#SEEDS[@]} seed(s) found"

MESSAGE=""
for f in "${SEEDS[@]}"; do
    [ -f "$f" ] || continue
    CONTENT=$(sed '1,/^$/d' "$f" | head -20)
    if [ -n "$CONTENT" ]; then
        MESSAGE+="$CONTENT"$'\n'
    fi
done

mkdir -p /tmp/fleet-seed-archive
for f in "${SEEDS[@]}"; do
    [ -f "$f" ] || continue
    mv "$f" "/tmp/fleet-seed-archive/$(date +%s)-$(basename "$f")"
done

if [ -z "$MESSAGE" ]; then
    log "No message content extracted"
    exit 0
fi

log "Processing: $(echo "$MESSAGE" | head -1 | cut -c1-80)"

BOT_TOKEN=$(security find-generic-password -a fleet -s DISCORD_BOT_TOKEN -w 2>/dev/null || true)
CHANNEL_ID="${FLEET_DISCORD_CHANNEL_ID:-1491820715421466865}"

send_discord() {
    local msg="$1"
    if [ -z "$BOT_TOKEN" ]; then
        log "No bot token — cannot reply to Discord"
        return 1
    fi
    if [ ${#msg} -gt $MAX_RESPONSE_CHARS ]; then
        msg="${msg:0:$MAX_RESPONSE_CHARS}... (truncated)"
    fi
    local json_msg
    json_msg=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$msg")
    curl -sf -X POST "https://discord.com/api/v10/channels/$CHANNEL_ID/messages" \
        -H "Authorization: Bot $BOT_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"content\": $json_msg}" > /dev/null 2>&1
    log "Discord reply sent (${#msg} chars)"
}

send_discord "Processing your message..." || true

log "Running claude -p ..."
CLAUDE_RESPONSE=$(cd "$REPO" && /usr/local/bin/claude -p "$MESSAGE" --output-format text --max-turns 5 2>/dev/null) || {
    log "Claude CLI failed (exit: $?)"
    send_discord "Claude CLI failed to process. Error logged." || true
    exit 1
}

if [ -z "$CLAUDE_RESPONSE" ]; then
    log "Empty response from Claude"
    send_discord "Atlas processed but returned empty response." || true
    exit 0
fi

log "Response received (${#CLAUDE_RESPONSE} chars)"

if [ ${#CLAUDE_RESPONSE} -gt $MAX_RESPONSE_CHARS ]; then
    send_discord "Atlas response:
${CLAUDE_RESPONSE:0:$MAX_RESPONSE_CHARS}" || true
    REMAINING="${CLAUDE_RESPONSE:$MAX_RESPONSE_CHARS}"
    if [ -n "$REMAINING" ]; then
        send_discord "(continued)
${REMAINING:0:$MAX_RESPONSE_CHARS}" || true
    fi
else
    send_discord "Atlas response:
$CLAUDE_RESPONSE" || true
fi

log "Complete. Message processed and response sent to Discord."
WAKESCRIPT
    chmod +x "$WAKE_SCRIPT"
    echo "  Created"
fi

# ── Step 4: Create discord-bridge LaunchAgent plist ──
echo "[4/8] Creating $BRIDGE_LABEL plist..."
BRIDGE_PLIST="$LAUNCH_DIR/$BRIDGE_LABEL.plist"
mkdir -p "$LAUNCH_DIR"

cat > "$BRIDGE_PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$BRIDGE_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PYTHON</string>
        <string>-m</string>
        <string>multifleet.discord_bridge</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR/multi-fleet</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
        <key>MULTIFLEET_NODE_ID</key>
        <string>$NODE_ID</string>
        <key>FLEET_DISCORD_CHANNEL_ID</key>
        <string>$CHANNEL_ID</string>
        <key>DISCORD_ALLOWED_AUTHORS</key>
        <string>${DISCORD_ALLOWED_AUTHORS:-}</string>
        <key>DISCORD_ALLOWED_GUILDS</key>
        <string>${DISCORD_ALLOWED_GUILDS:-}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/discord-bridge-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/discord-bridge-stderr.log</string>
</dict>
</plist>
EOF
echo "  Written: $BRIDGE_PLIST"

# ── Step 5: Create discord-wake LaunchAgent plist ──
echo "[5/8] Creating $WAKE_LABEL plist..."
WAKE_PLIST="$LAUNCH_DIR/$WAKE_LABEL.plist"

cat > "$WAKE_PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$WAKE_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$REPO_DIR/scripts/discord-wake-atlas.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>FLEET_DISCORD_CHANNEL_ID</key>
        <string>$CHANNEL_ID</string>
    </dict>
    <key>WatchPaths</key>
    <array>
        <string>/tmp/discord-wake-trigger</string>
    </array>
    <key>StandardOutPath</key>
    <string>/tmp/discord-wake-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/discord-wake-stderr.log</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
EOF
echo "  Written: $WAKE_PLIST"

# ── Step 6: Load LaunchAgents ──
echo "[6/8] Loading LaunchAgents..."

# Unload first if already loaded (idempotent)
launchctl bootout "gui/$(id -u)/$BRIDGE_LABEL" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$WAKE_LABEL" 2>/dev/null || true

launchctl bootstrap "gui/$(id -u)" "$BRIDGE_PLIST"
echo "  Loaded $BRIDGE_LABEL"
launchctl bootstrap "gui/$(id -u)" "$WAKE_PLIST"
echo "  Loaded $WAKE_LABEL"

# ── Step 7: Verify bridge connects ──
echo "[7/8] Waiting for bridge to connect (10s)..."
sleep 10

BRIDGE_PID=$(pgrep -f "multifleet.discord_bridge" || true)
if [ -n "$BRIDGE_PID" ]; then
    echo "  Bridge running (PID: $BRIDGE_PID)"
else
    echo "  WARNING: Bridge process not found. Check logs:"
    echo "    tail -20 /tmp/discord-bridge-stderr.log"
    echo "    tail -20 /tmp/discord-bridge-stdout.log"
fi

# ── Step 8: Send test message to Discord ──
echo "[8/8] Sending test message to Discord..."
BOT_TOKEN=$(security find-generic-password -a fleet -s DISCORD_BOT_TOKEN -w 2>/dev/null || true)

if [ -n "$BOT_TOKEN" ]; then
    MSG="[$NODE_ID] Discord wake pipeline bootstrapped. Bridge + wake LaunchAgents active. $(date '+%Y-%m-%d %H:%M:%S')"
    JSON_MSG=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$MSG")
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
        "https://discord.com/api/v10/channels/$CHANNEL_ID/messages" \
        -H "Authorization: Bot $BOT_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"content\": $JSON_MSG}" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "  Test message sent to Discord"
    else
        echo "  WARNING: Discord API returned HTTP $HTTP_CODE"
        echo "  Check token and channel ID"
    fi
else
    echo "  WARNING: No bot token — skipping test message"
fi

echo ""
echo "=== Setup Complete ==="
echo "Node:    $NODE_ID"
echo "Bridge:  $BRIDGE_LABEL (KeepAlive, auto-restart)"
echo "Wake:    $WAKE_LABEL (WatchPaths trigger)"
echo "Channel: $CHANNEL_ID"
echo ""
echo "Troubleshooting:"
echo "  tail -f /tmp/discord-bridge-stderr.log"
echo "  tail -f /tmp/discord-wake.log"
echo "  launchctl list | grep contextdna.discord"
