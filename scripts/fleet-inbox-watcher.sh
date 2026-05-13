#!/usr/bin/env bash
# fleet-inbox-watcher.sh — Event-driven fleet inbox processor using fswatch
#
# Pattern 1 from ClaudeExchange: watches for seed file changes INSTANTLY
# instead of polling every 60s. When the daemon writes a seed file,
# fswatch fires within milliseconds → processes → archives.
#
# Runs as a background process (started by fleet-nerve-setup.sh).
# Fallback: if fswatch unavailable, uses a tight poll loop (2s).
#
# Usage:
#   bash scripts/fleet-inbox-watcher.sh           # foreground
#   bash scripts/fleet-inbox-watcher.sh --install  # LaunchAgent

set -uo pipefail

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
WATCH_DIR="/tmp"
ARCHIVE_DIR="/tmp/fleet-seed-archive"
LOG="/tmp/fleet-inbox-watcher.log"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO_ROOT/scripts/fleet-inbox-hook.sh"

mkdir -p "$ARCHIVE_DIR"

_log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"; }

_process_seed() {
    local f="$1"
    [ ! -f "$f" ] && return
    [ ! -s "$f" ] && return

    _log "Processing: $(basename "$f")"

    # Run the hook script to format the output
    bash "$HOOK" 2>/dev/null

    _log "Archived: $(basename "$f")"
}

# ── Install as LaunchAgent ──
if [[ "${1:-}" == "--install" ]]; then
    PLIST="$HOME/Library/LaunchAgents/io.contextdna.fleet-inbox-watcher.plist"
    cat > "$PLIST" <<PEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.contextdna.fleet-inbox-watcher</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO_ROOT/scripts/fleet-inbox-watcher.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MULTIFLEET_NODE_ID</key>
        <string>$NODE_ID</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PEOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Fleet inbox watcher installed: $PLIST"
    echo "Watching: /tmp/fleet-seed-${NODE_ID}.md"
    exit 0
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    PLIST="$HOME/Library/LaunchAgents/io.contextdna.fleet-inbox-watcher.plist"
    launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "Fleet inbox watcher uninstalled"
    exit 0
fi

# ── Main: watch for seed files ──
_log "Fleet inbox watcher starting — node=$NODE_ID"

SEED_PATTERN="fleet-seed-"

if command -v fswatch >/dev/null 2>&1; then
    _log "Using fswatch (event-driven, instant)"

    # Watch /tmp for any fleet-seed-* file creation/modification
    fswatch -0 --event Created --event Updated --event Renamed \
        --include "fleet-seed-" --exclude ".*" "$WATCH_DIR" | while IFS= read -r -d '' file; do

        # Only process our seed files
        [[ "$(basename "$file")" == fleet-seed-* ]] || continue
        [[ -s "$file" ]] || continue

        _log "fswatch triggered: $(basename "$file")"
        _process_seed "$file"
    done
else
    _log "fswatch not available — using poll fallback (2s)"

    while true; do
        for f in "$WATCH_DIR"/fleet-seed-"${NODE_ID}".md "$WATCH_DIR"/fleet-seed-"$(hostname -s | tr '[:upper:]' '[:lower:]')".md; do
            [ -f "$f" ] && [ -s "$f" ] && _process_seed "$f"
        done
        sleep 2
    done
fi
