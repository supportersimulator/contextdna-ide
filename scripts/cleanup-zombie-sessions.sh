#!/usr/bin/env bash
# cleanup-zombie-sessions.sh — Find and remove dead Claude session files
# Safe: verifies PID is dead before removing anything. Never kills processes.
# Usage: ./scripts/cleanup-zombie-sessions.sh [--dry-run]

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

SESSIONS_DIR="$HOME/.claude/sessions"
PROJECTS_DIR="$HOME/.claude/projects"
CLEANED=0
ALIVE=0
STALE_PROJECTS=0
STALE_TMP=0

echo "=== Claude Zombie Session Cleanup ==="
echo "Mode: $( $DRY_RUN && echo 'DRY RUN' || echo 'LIVE' )"
echo ""

# --- 1. Scan session files for dead PIDs ---
echo "--- Session Files ($SESSIONS_DIR) ---"
if [[ -d "$SESSIONS_DIR" ]]; then
    for f in "$SESSIONS_DIR"/*.json; do
        [[ -f "$f" ]] || continue
        basename_f="$(basename "$f")"
        pid="${basename_f%.json}"

        # Validate PID is numeric
        if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
            echo "  SKIP $basename_f (non-numeric PID)"
            continue
        fi

        if ps -p "$pid" > /dev/null 2>&1; then
            echo "  ALIVE  PID $pid"
            ALIVE=$((ALIVE + 1))
        else
            echo "  DEAD   PID $pid — $(cat "$f" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'session={d.get(\"sessionId\",\"?\")[:12]} started={d.get(\"startedAt\",\"?\")}')" 2>/dev/null || echo "unreadable")"
            if ! $DRY_RUN; then
                rm -f "$f"
                echo "         REMOVED $f"
            else
                echo "         WOULD REMOVE $f"
            fi
            CLEANED=$((CLEANED + 1))
        fi
    done
else
    echo "  (directory does not exist)"
fi
echo ""

# --- 2. Check project dirs for stale worktree sessions ---
echo "--- Stale Worktree Project Dirs ($PROJECTS_DIR) ---"
if [[ -d "$PROJECTS_DIR" ]]; then
    for d in "$PROJECTS_DIR"/*worktree*/; do
        [[ -d "$d" ]] || continue

        # Extract the worktree path from the dir name
        # Format: -Users-user-dev-repo--claude-worktrees-agent-HASH
        dir_name="$(basename "$d")"

        # Convert encoded path back: leading - becomes /, remaining - becomes /
        decoded_path="/$(echo "$dir_name" | sed 's/^-//; s/-/\//g')"

        if [[ ! -d "$decoded_path" ]]; then
            file_count=$(find "$d" -type f 2>/dev/null | wc -l | tr -d ' ')
            total_size=$(du -sh "$d" 2>/dev/null | cut -f1)
            echo "  STALE  $dir_name ($file_count files, $total_size) — worktree gone"
            if ! $DRY_RUN; then
                rm -rf "$d"
                echo "         REMOVED $d"
            else
                echo "         WOULD REMOVE $d"
            fi
            STALE_PROJECTS=$((STALE_PROJECTS + 1))
        else
            echo "  OK     $dir_name"
        fi
    done
fi
echo ""

# --- 3. Check /tmp for stale fleet/atlas files ---
echo "--- Stale Temp Files (/tmp) ---"
# Fleet seed files older than 24 hours
while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    echo "  STALE  $(basename "$f") (>24h old)"
    if ! $DRY_RUN; then
        rm -f "$f"
        echo "         REMOVED"
    else
        echo "         WOULD REMOVE"
    fi
    STALE_TMP=$((STALE_TMP + 1))
done < <(find /tmp -maxdepth 1 -name "*fleet-seed*" -mtime +1 2>/dev/null)

# Fleet log files older than 7 days
while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    echo "  STALE  $(basename "$f") (>7d old)"
    if ! $DRY_RUN; then
        rm -f "$f"
        echo "         REMOVED"
    else
        echo "         WOULD REMOVE"
    fi
    STALE_TMP=$((STALE_TMP + 1))
done < <(find /tmp -maxdepth 1 -name "fleet-*.log" -mtime +7 2>/dev/null)

# Atlas agent result files older than 7 days
if [[ -d /tmp/atlas-agent-results ]]; then
    while IFS= read -r f; do
        [[ -f "$f" ]] || continue
        echo "  STALE  atlas-agent-results/$(basename "$f") (>7d old)"
        if ! $DRY_RUN; then
            rm -f "$f"
        fi
        STALE_TMP=$((STALE_TMP + 1))
    done < <(find /tmp/atlas-agent-results -type f -mtime +7 2>/dev/null)
fi

[[ $STALE_TMP -eq 0 ]] && echo "  (none found)"
echo ""

# --- 4. Running claude processes (info only) ---
echo "--- Running Claude Processes (info only, not killing) ---"
ps aux | grep -i "[c]laude" | grep -v "cleanup-zombie" | head -10 || echo "  (none)"
echo ""

# --- Summary ---
echo "=== Summary ==="
echo "  Sessions alive:           $ALIVE"
echo "  Dead sessions cleaned:    $CLEANED"
echo "  Stale worktree dirs:      $STALE_PROJECTS"
echo "  Stale temp files:         $STALE_TMP"
echo "  Total cleaned:            $((CLEANED + STALE_PROJECTS + STALE_TMP))"
