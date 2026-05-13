#!/usr/bin/env bash
# fleet-git-msg.sh — Git-based fleet messaging (P7 fallback — always works)
#
# When all real-time channels fail (NATS, HTTP, SSH tunnels, SSH direct),
# messages are committed to git and pushed. Recipients pull and process.
#
# Message format: .fleet-messages/<to>/<timestamp>-<from>.md
# Consumed on pull: processed messages moved to .fleet-messages/archive/
#
# Usage:
#   fleet-git-msg.sh send <to> <subject> <body>    # Send via git commit+push
#   fleet-git-msg.sh send all <subject> <body>      # Broadcast
#   fleet-git-msg.sh check                           # Pull + process inbox
#   fleet-git-msg.sh install                         # LaunchAgent (poll every 60s)
#   fleet-git-msg.sh uninstall                       # Remove LaunchAgent

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MSG_DIR="$REPO_ROOT/.fleet-messages"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
ARCHIVE="$MSG_DIR/archive"
LOG="/tmp/fleet-git-msg.log"

_log() { echo "[fleet-git-msg] $(date '+%H:%M:%S') $*"; }  # launchd captures stdout → $LOG (no tee, avoids doubling)

# Push-freeze guard (FLEET_PUSH_FREEZE=1 → skip all git push origin main).
# CI budget protection. ZSF: every skip is logged + counted.
_push_or_skip() {
    if [ "${FLEET_PUSH_FREEZE:-0}" = "1" ]; then
        local n
        n=$(cat /tmp/fleet-push-freeze.count 2>/dev/null || echo 0)
        echo $((n + 1)) > /tmp/fleet-push-freeze.count
        _log "FREEZE: skip git push origin main (FLEET_PUSH_FREEZE=1, count=$((n + 1)))"
        return 0
    fi
    git push origin main 2>/dev/null
}

_get_all_node_ids() {
    python3 -c "
import json
cfg = json.load(open('$REPO_ROOT/.multifleet/config.json'))
for nid in sorted(cfg.get('nodes', {}).keys()):
    print(nid)
" 2>/dev/null
}

_ensure_dirs() {
    mkdir -p "$MSG_DIR/all" "$ARCHIVE"
    # Create inbox dir for every node in config
    local nodes
    nodes="$(_get_all_node_ids)"
    for node in $nodes; do
        mkdir -p "$MSG_DIR/$node"
        [ -f "$MSG_DIR/$node/.gitkeep" ] || touch "$MSG_DIR/$node/.gitkeep"
    done
    [ -f "$MSG_DIR/all/.gitkeep" ] || touch "$MSG_DIR/all/.gitkeep"
    [ -f "$ARCHIVE/.gitkeep" ] || touch "$ARCHIVE/.gitkeep"
}

cmd_send() {
    local to="${1:?Usage: fleet-git-msg.sh send <to> <subject> <body>}"
    local subject="${2:?subject required}"
    local body="${3:-}"
    local ts="$(date +%Y%m%d-%H%M%S)"
    local filename="${ts}-from-${NODE_ID}.md"

    _ensure_dirs
    cd "$REPO_ROOT"

    # Write message
    local targets=()
    if [ "$to" = "all" ]; then
        local all_nodes
        all_nodes="$(_get_all_node_ids)"
        for n in $all_nodes; do targets+=("$n"); done
    else
        targets=("$to")
    fi

    for target in "${targets[@]}"; do
        [ "$target" = "$NODE_ID" ] && continue  # don't message self
        local filepath="$MSG_DIR/${target}/${filename}"
        cat > "$filepath" <<EOF
---
from: ${NODE_ID}
to: ${target}
subject: ${subject}
timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)
---

${body}
EOF
        _log "Wrote: ${target}/${filename}"
    done

    # Commit and push
    git add "$MSG_DIR/" 2>/dev/null
    if git diff --cached --quiet 2>/dev/null; then
        _log "No new messages to commit"
        return 0
    fi

    git commit -m "fleet-msg: ${NODE_ID}→${to}: ${subject}" --no-verify 2>/dev/null
    if _push_or_skip; then
        _log "✓ Message pushed to origin (or freeze-skipped)"
    else
        _log "✗ Push failed — will retry on next check"
    fi
}

cmd_check() {
    cd "$REPO_ROOT"
    _ensure_dirs

    # Auto-pull invariance: stash-then-merge-then-pop so dirty working tree never blocks pull.
    # Previously, "cannot pull with rebase: unstaged changes" killed the daemon between sessions.
    # Using --no-rebase (merge) to avoid HEAD-detached failure mode we hit earlier.
    local stashed=0
    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        if git stash push -u -m "fleet-git-msg auto-stash $(date +%Y%m%d-%H%M%S)" >/dev/null 2>&1; then
            stashed=1
            _log "Auto-stashed local changes before pull"
        fi
    fi

    # Pull latest (merge strategy — uglier history, won't abort on conflicts)
    if ! git fetch origin main 2>/dev/null; then
        _log "Fetch failed"
        [ "$stashed" -eq 1 ] && git stash pop >/dev/null 2>&1 || true
        return 1
    fi

    # Check if there are new commits
    local behind
    behind=$(git rev-list --count HEAD..FETCH_HEAD 2>/dev/null || echo 0)
    if [ "$behind" -gt 0 ]; then
        # --no-rebase + --no-edit: merge strategy (never creates
        # .git/rebase-merge/), auto-accept default merge message so an
        # interactive editor cannot wedge the daemon.
        if git pull --no-rebase --no-edit origin main 2>/dev/null; then
            _log "Pulled $behind new commit(s) (merge)"
        else
            _log "Pull failed — manual resolution needed"
            [ "$stashed" -eq 1 ] && git stash pop >/dev/null 2>&1 || true
            return 1
        fi
    fi

    # Restore stashed changes (best-effort)
    if [ "$stashed" -eq 1 ]; then
        if git stash pop >/dev/null 2>&1; then
            _log "Restored auto-stashed changes"
        else
            _log "WARN: stash pop had conflicts — inspect 'git stash list'"
        fi
    fi

    # Process our inbox
    local inbox="$MSG_DIR/$NODE_ID"
    local all_inbox="$MSG_DIR/all"
    local count=0

    for msg_file in "$inbox"/*.md "$all_inbox"/*.md; do
        [ -f "$msg_file" ] || continue
        [ "$(basename "$msg_file")" = ".gitkeep" ] && continue

        # Don't process our own broadcasts
        if echo "$msg_file" | grep -q "from-${NODE_ID}"; then
            continue
        fi

        _log "Processing: $(basename "$msg_file")"

        # Extract and inject into seed file for active session.
        # Bug A fix: each grep/sed wrapped so pipeline failure (no YAML frontmatter,
        # e.g. mac1 cloud-scan broadcasts) does NOT trigger set -e mid-loop and abort
        # BEFORE the archive mv. Previously: file stays in inbox, re-processed forever.
        local subject body from_node
        subject=$({ grep "^subject:" "$msg_file" 2>/dev/null || echo ""; } | head -1 | sed 's/^subject: *//' || echo "")
        from_node=$({ grep "^from:" "$msg_file" 2>/dev/null || echo ""; } | head -1 | sed 's/^from: *//' || echo "")
        [ -z "$from_node" ] && from_node="unknown"
        # Body: everything after the second YAML separator (---). If no YAML, fall back to full file.
        if grep -q "^---$" "$msg_file" 2>/dev/null; then
            body=$({ sed '1,/^---$/d' "$msg_file" 2>/dev/null || echo ""; } | { sed '1,/^$/d' 2>/dev/null || cat; } || true)
        else
            body=$(cat "$msg_file" 2>/dev/null || echo "")
        fi
        [ -z "$subject" ] && subject="$(basename "$msg_file" .md)"
        [ -z "$body" ] && body="(empty message)"

        # Write to seed file for session injection
        local seed="/tmp/fleet-seed-${NODE_ID}.md"
        {
            echo "## [GIT-MSG] from ${from_node}: ${subject}"
            echo ""
            echo "$body"
            echo ""
            echo "---"
            echo "_delivered via git at $(date '+%H:%M:%S')_"
            echo ""
        } >> "$seed"

        # Archive
        mv "$msg_file" "$ARCHIVE/" 2>/dev/null || true
        count=$((count + 1))
    done

    if [ "$count" -gt 0 ]; then
        _log "Processed $count message(s)"
        # Commit archive moves
        git add "$MSG_DIR/" 2>/dev/null
        if ! git diff --cached --quiet 2>/dev/null; then
            git commit -m "fleet-msg: ${NODE_ID} processed ${count} message(s)" --no-verify 2>/dev/null
            _push_or_skip || _log "Archive push deferred"
        fi

        # macOS notification
        osascript -e "display notification \"${count} fleet message(s) via git\" with title \"Fleet Git\" sound name \"Glass\"" 2>/dev/null || true
    fi
}

cmd_install() {
    local plist="$HOME/Library/LaunchAgents/io.multifleet.git-msg.plist"
    cat > "$plist" <<PEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.multifleet.git-msg</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${REPO_ROOT}/scripts/fleet-git-msg.sh</string>
        <string>check</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MULTIFLEET_NODE_ID</key>
        <string>${NODE_ID}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StartInterval</key><integer>60</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/tmp/fleet-git-msg.log</string>
    <key>StandardErrorPath</key><string>/tmp/fleet-git-msg.log</string>
</dict>
</plist>
PEOF
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
    _log "Git messaging LaunchAgent installed — checks every 60s"
}

cmd_uninstall() {
    local plist="$HOME/Library/LaunchAgents/io.multifleet.git-msg.plist"
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    _log "Git messaging LaunchAgent removed"
}

cmd_purge_stuck() {
    # Manual cleanup: move any message file >24h old out of inboxes to archive/.
    # For when BugA-style failures left files stranded — recovers without re-processing.
    cd "$REPO_ROOT"
    _ensure_dirs
    local moved=0
    local node_inbox="$MSG_DIR/$NODE_ID"
    local all_inbox="$MSG_DIR/all"

    for dir in "$node_inbox" "$all_inbox"; do
        [ -d "$dir" ] || continue
        # find files modified >24h ago (mtime +1 = >24h), excluding .gitkeep
        while IFS= read -r -d '' stale; do
            [ "$(basename "$stale")" = ".gitkeep" ] && continue
            mv "$stale" "$ARCHIVE/" 2>/dev/null && {
                _log "Purged stuck: $(basename "$stale") (from $dir)"
                moved=$((moved + 1))
            }
        done < <(find "$dir" -maxdepth 1 -type f -name "*.md" -mtime +1 -print0 2>/dev/null)
    done

    _log "Purge complete: $moved file(s) moved to archive"
    if [ "$moved" -gt 0 ]; then
        git add "$MSG_DIR/" 2>/dev/null || true
        if ! git diff --cached --quiet 2>/dev/null; then
            git commit -m "fleet-msg: ${NODE_ID} purged ${moved} stuck message(s)" --no-verify 2>/dev/null || true
            _push_or_skip || _log "Purge push deferred"
        fi
    fi
}

# ── Main ──
case "${1:-check}" in
    send)         shift; cmd_send "$@" ;;
    check)        cmd_check ;;
    install)      cmd_install ;;
    uninstall)    cmd_uninstall ;;
    purge-stuck)  cmd_purge_stuck ;;
    *)            echo "Usage: fleet-git-msg.sh {send <to> <subject> <body>|check|install|uninstall|purge-stuck}"; exit 1 ;;
esac
