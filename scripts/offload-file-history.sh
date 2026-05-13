#!/bin/bash
# offload-file-history.sh — Archive old Claude Code file-history sessions
#
# Moves sessions older than KEEP_DAYS (default: 7) from ~/.claude/file-history/
# to an archive location. Keeps recent sessions for active undo capability.
#
# Usage:
#   ./scripts/offload-file-history.sh              # Dry run (preview)
#   ./scripts/offload-file-history.sh --execute    # Actually move files
#   ./scripts/offload-file-history.sh --restore <session-id>  # Restore session
#
# Archive location: ~/.context-dna/file-history-archive/

set -euo pipefail

FILE_HISTORY="$HOME/.claude/file-history"
ARCHIVE_DIR="$HOME/.context-dna/file-history-archive"
KEEP_DAYS="${KEEP_DAYS:-7}"
EXECUTE=false
RESTORE_ID=""

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --execute) EXECUTE=true; shift ;;
        --restore) RESTORE_ID="$2"; shift 2 ;;
        --keep-days) KEEP_DAYS="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Restore mode
if [[ -n "$RESTORE_ID" ]]; then
    src="$ARCHIVE_DIR/$RESTORE_ID"
    dst="$FILE_HISTORY/$RESTORE_ID"
    if [[ ! -d "$src" ]]; then
        echo "ERROR: Session $RESTORE_ID not found in archive"
        echo "Available archived sessions:"
        ls "$ARCHIVE_DIR" 2>/dev/null | head -20
        exit 1
    fi
    if [[ -d "$dst" ]]; then
        echo "Session $RESTORE_ID already exists in file-history"
        exit 1
    fi
    mv "$src" "$dst"
    echo "Restored $RESTORE_ID from archive to file-history"
    exit 0
fi

# Check source exists
if [[ ! -d "$FILE_HISTORY" ]]; then
    echo "No file-history directory found at $FILE_HISTORY"
    exit 0
fi

# Ensure archive dir
mkdir -p "$ARCHIVE_DIR"

# Find sessions older than KEEP_DAYS
total_sessions=$(ls -d "$FILE_HISTORY"/*/ 2>/dev/null | wc -l | tr -d ' ')
old_count=0
old_size=0
current_session="${CLAUDE_SESSION_ID:-}"

echo "=== Claude Code File-History Offloader ==="
echo "Source: $FILE_HISTORY"
echo "Archive: $ARCHIVE_DIR"
echo "Keep days: $KEEP_DAYS"
echo "Total sessions: $total_sessions"
echo ""

for session_dir in "$FILE_HISTORY"/*/; do
    [[ -d "$session_dir" ]] || continue
    session_id=$(basename "$session_dir")

    # Never offload current session
    if [[ -n "$current_session" && "$session_id" == "$current_session"* ]]; then
        continue
    fi

    # Check modification time
    if [[ $(find "$session_dir" -maxdepth 0 -mtime +"$KEEP_DAYS" 2>/dev/null) ]]; then
        dir_size=$(du -sk "$session_dir" 2>/dev/null | cut -f1)
        old_size=$((old_size + dir_size))
        old_count=$((old_count + 1))

        if $EXECUTE; then
            mv "$session_dir" "$ARCHIVE_DIR/"
            echo "  MOVED: $session_id (${dir_size}K)"
        else
            echo "  WOULD MOVE: $session_id (${dir_size}K)"
        fi
    fi
done

echo ""
echo "Sessions to offload: $old_count"
echo "Space to reclaim: $((old_size / 1024))MB"
echo "Sessions to keep: $((total_sessions - old_count))"

if ! $EXECUTE && [[ $old_count -gt 0 ]]; then
    echo ""
    echo "Run with --execute to actually move files"
fi

# Show archive stats
if [[ -d "$ARCHIVE_DIR" ]]; then
    archive_count=$(ls -d "$ARCHIVE_DIR"/*/ 2>/dev/null | wc -l | tr -d ' ')
    archive_size=$(du -sh "$ARCHIVE_DIR" 2>/dev/null | cut -f1)
    echo ""
    echo "Archive: $archive_count sessions, $archive_size"
fi
