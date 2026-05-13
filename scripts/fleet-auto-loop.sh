#!/usr/bin/env bash
# fleet-auto-loop.sh — Auto-injects the fleet monitor /loop into active Claude Code sessions
#
# Runs as a LaunchAgent. Checks every 60s if:
# 1. A Claude Code session is active (PID file exists)
# 2. The /loop hasn't been injected yet (marker file)
#
# If both true: uses osascript to focus VS Code and type the /loop command.
# One-shot per session — marker file prevents re-injection.

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
MARKER="/tmp/fleet-loop-injected-${NODE_ID}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOOP_CMD="/loop 1m bash ${REPO}/scripts/fleet-check.sh"

# Already injected this session? Skip.
if [ -f "$MARKER" ]; then
    # Check if the session PID is still alive
    PID=$(cat "$MARKER" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        exit 0  # session still alive, already injected
    fi
    rm -f "$MARKER"  # stale marker, session died
fi

# Is Fleet Nerve running?
curl -sf http://localhost:8855/health >/dev/null 2>&1 || exit 0

# Is there an active Claude Code session?
SESSION_PID=""
if [ -d "$HOME/.claude/sessions" ]; then
    for f in "$HOME/.claude/sessions"/*.json; do
        [ -f "$f" ] || continue
        PID=$(basename "$f" .json)
        if kill -0 "$PID" 2>/dev/null; then
            SESSION_PID="$PID"
            break
        fi
    done
fi

[ -z "$SESSION_PID" ] && exit 0

# Is VS Code the frontmost app or at least running?
VS_CODE_RUNNING=$(osascript -e 'tell application "System Events" to (name of processes) contains "Code"' 2>/dev/null)
[ "$VS_CODE_RUNNING" != "true" ] && exit 0

# Inject: focus VS Code, type the /loop command, press Enter
osascript <<APPLESCRIPT 2>/dev/null
tell application "Visual Studio Code 3" to activate
delay 1
tell application "System Events"
    keystroke "${LOOP_CMD}"
    delay 0.3
    keystroke return
end tell
APPLESCRIPT

if [ $? -eq 0 ]; then
    echo "$SESSION_PID" > "$MARKER"
    echo "[$(date '+%H:%M:%S')] Fleet /loop injected into session $SESSION_PID"
fi
