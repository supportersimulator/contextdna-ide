#!/usr/bin/env bash
# fleet-inbox-daemon.sh — Idle-aware fleet inbox daemon.
#
# Runs continuously on every node. Two modes:
#
#   INBOX MODE: New messages from other nodes arrive → deliver to Claude session
#     - Has claude CLI (mac1): runs `claude -p "<message>"` in repo dir
#     - No claude CLI (mac2/mac3): writes to seed file, osascript notification
#       The UserPromptSubmit + Stop hooks pick up the seed file on next turn.
#
#   IDLE MODE: No messages, agent idle > IDLE_THRESHOLD minutes →
#     Calls local LLM proxy (port 5045) to generate a productive task list.
#     Writes to seed file. Agent picks it up on next turn via hooks.
#     Prevents "staring at nothing" during gaps between work.
#
# Deploy:
#   ./scripts/fleet-inbox-daemon.sh --install
#   ./scripts/fleet-inbox-daemon.sh --uninstall
#   ./scripts/fleet-inbox-daemon.sh             # foreground

POLL_INTERVAL="${FLEET_POLL_INTERVAL:-30}"       # seconds between inbox checks
IDLE_THRESHOLD="${FLEET_IDLE_THRESHOLD:-5}"      # minutes before idle mode fires
CHIEF_URL="${CHIEF_INGEST_URL:-http://chief.local:8844}"
NODE="${MULTIFLEET_NODE_ID:-}"
REPO="${CONTEXT_DNA_REPO:-$HOME/dev/er-simulator-superrepo}"
LLM_PROXY="${LLM_PROXY_URL:-http://127.0.0.1:5045/v1}"
PLIST="$HOME/Library/LaunchAgents/io.contextdna.fleet-inbox.plist"
LOG="/tmp/fleet-inbox-daemon-${NODE}.log"
SEED_FILE="/tmp/fleet-seed-${NODE}.md"      # picked up by hooks on next turn

# ── Node / URL detection ──────────────────────────────────────────────────────
_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
source "$_REPO_ROOT/scripts/fleet-node-id.sh"
[[ -z "$NODE" ]] && NODE=$(fleet_node_id)
# Chief runs the ingest endpoint locally — short-circuit DNS / chief.local.
_CHIEF_ID="$(fleet_chief_id)"
if [[ -n "$_CHIEF_ID" && "$NODE" == "$_CHIEF_ID" ]]; then
    CHIEF_URL="http://127.0.0.1:8844"
fi

CLAUDE_BIN=$(command -v claude 2>/dev/null \
    || ls /usr/local/bin/claude 2>/dev/null \
    || ls "$HOME/.local/bin/claude" 2>/dev/null \
    || echo "")

# ── Install / uninstall ───────────────────────────────────────────────────────
if [[ "${1:-}" == "--install" ]]; then
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.contextdna.fleet-inbox</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO/scripts/fleet-inbox-daemon.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MULTIFLEET_NODE_ID</key><string>$NODE</string>
        <key>CHIEF_INGEST_URL</key><string>$CHIEF_URL</string>
        <key>CONTEXT_DNA_REPO</key><string>$REPO</string>
        <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
    <key>ThrottleInterval</key><integer>10</integer>
</dict>
</plist>
PLISTEOF
    launchctl unload "$PLIST" 2>/dev/null
    launchctl load "$PLIST"
    echo "[fleet-inbox-daemon] Installed: $PLIST"
    echo "[fleet-inbox-daemon] Node=$NODE | Chief=$CHIEF_URL | Idle threshold=${IDLE_THRESHOLD}m"
    exit 0
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl unload "$PLIST" 2>/dev/null && rm -f "$PLIST"
    echo "[fleet-inbox-daemon] Uninstalled"
    exit 0
fi

# ── LLM proxy call for idle task generation ───────────────────────────────────
_generate_idle_tasks() {
    local recent_git
    recent_git=$(cd "$REPO" && git log --oneline -5 2>/dev/null | head -5)

    local payload
    payload=$(python3 -c "
import json
system = 'You are a productive AI coding assistant. Generate a concise, actionable task list.'
user = '''The coding session on $NODE has been idle. Based on recent git commits, suggest 3-5 productive next steps.

Recent commits:
$recent_git

Format as a short markdown list. Be specific and actionable. Focus on what logically comes next.'''
print(json.dumps({'model': 'local', 'messages': [
    {'role': 'system', 'content': system},
    {'role': 'user', 'content': user}
], 'max_tokens': 512}))
" 2>/dev/null)

    local resp
    resp=$(curl -sf --max-time 30 -X POST "$LLM_PROXY/chat/completions" \
        -H "Content-Type: application/json" \
        -d "$payload" 2>/dev/null)

    if [[ $? -eq 0 ]]; then
        echo "$resp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['choices'][0]['message']['content'])
" 2>/dev/null
    fi
}

# ── Deliver messages to Claude ────────────────────────────────────────────────
_deliver() {
    local prompt="$1"
    local subject="$2"

    if [[ -n "$CLAUDE_BIN" ]]; then
        echo "[$(date +%H:%M:%S')] Waking Claude via CLI: $subject" | tee -a "$LOG"
        cd "$REPO" && "$CLAUDE_BIN" -p "$prompt" >> "$LOG" 2>&1 &
    else
        # Write to seed file — hooks inject on next turn
        {
            echo "# Fleet Message for $NODE"
            echo "## $subject"
            echo ""
            echo "$prompt"
            echo ""
            echo "_delivered $(date '+%H:%M:%S')_"
        } > "$SEED_FILE"
        echo "[$(date +'%H:%M:%S')] Seed file written: $SEED_FILE" | tee -a "$LOG"
        # Also fire a macOS notification so human knows
        osascript -e "display notification \"$subject\" with title \"Fleet Inbox — $NODE\" sound name \"Glass\"" 2>/dev/null
    fi
}

# ── Main loop ─────────────────────────────────────────────────────────────────
echo "[$(date +'%H:%M:%S')] fleet-inbox-daemon starting — node=$NODE chief=$CHIEF_URL idle=${IDLE_THRESHOLD}m claude=${CLAUDE_BIN:-NONE}"

LAST_ACTIVITY=$(date +%s)
LAST_IDLE_TASK=0

while true; do
    # ── Check inbox ────────────────────────────────────────────────────────
    RESP=$(curl -sf --max-time 5 "${CHIEF_URL}/inbox?node=${NODE}" 2>/dev/null)
    if [[ $? -ne 0 ]]; then
        sleep "$POLL_INTERVAL"
        continue
    fi

    COUNT=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])" 2>/dev/null)

    if [[ -n "$COUNT" && "$COUNT" != "0" ]]; then
        LAST_ACTIVITY=$(date +%s)
        PROMPT=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
lines = ['You have ' + str(d['count']) + ' fleet message(s):']
for m in d['messages']:
    lines += ['', 'FROM: ' + m['from'] + '  [' + m['priority'].upper() + ']',
              'SUBJECT: ' + m['subject'], 'MESSAGE: ' + m['body']]
lines += ['', 'Please acknowledge and take any necessary action.']
print('\n'.join(lines))
" 2>/dev/null)
        SUBJECT=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
m = d['messages'][0]
print('[' + m['priority'].upper() + '] ' + m['from'] + ': ' + m['subject'])
" 2>/dev/null)
        _deliver "$PROMPT" "$SUBJECT"
        sleep "$POLL_INTERVAL"
        continue
    fi

    # ── Idle mode: generate productive tasks ───────────────────────────────
    NOW=$(date +%s)
    IDLE_SECS=$(( NOW - LAST_ACTIVITY ))
    IDLE_THRESH_SECS=$(( IDLE_THRESHOLD * 60 ))
    SINCE_LAST_IDLE=$(( NOW - LAST_IDLE_TASK ))

    if [[ $IDLE_SECS -ge $IDLE_THRESH_SECS && $SINCE_LAST_IDLE -ge $IDLE_THRESH_SECS ]]; then
        echo "[$(date +'%H:%M:%S')] Idle ${IDLE_SECS}s — generating productive task list..." | tee -a "$LOG"
        TASKS=$(_generate_idle_tasks)
        if [[ -n "$TASKS" ]]; then
            LAST_IDLE_TASK=$(date +%s)
            PROMPT="The session has been idle. Here are suggested next steps based on recent work:

$TASKS

Pick the most impactful item and start on it, or ask if you need more context."
            _deliver "$PROMPT" "Idle task list — $NODE"
        fi
    fi

    sleep "$POLL_INTERVAL"
done
