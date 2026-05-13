#!/bin/bash
# Hook Review Check-In Script
#
# Automatically triggers hook review agent when enough time has passed.
# Designed to be called from Claude Code SessionEnd hook.
#
# Usage (in Claude Code settings.json):
#   "hooks": {
#     "Stop": [
#       {
#         "command": "scripts/hook-review-checkin.sh",
#         "timeout": 30000
#       }
#     ]
#   }
#
# Or run manually:
#   ./scripts/hook-review-checkin.sh
#   ./scripts/hook-review-checkin.sh --force
#   ./scripts/hook-review-checkin.sh --auto  # Auto-create proposed tests

set -e

REPO_DIR="${CONTEXT_DNA_REPO:-${CLAUDE_PROJECT_DIR:-$HOME/dev/er-simulator-superrepo}}"
PYTHON="${CONTEXT_DNA_PYTHON:-$REPO_DIR/.venv/bin/python3}"
REVIEW_SCRIPT="$REPO_DIR/memory/hook_review_agent.py"
REVIEW_LOG="$HOME/.context-dna/hook_reviews.log"

# Ensure log directory exists
mkdir -p "$(dirname "$REVIEW_LOG")"

# Parse arguments
FORCE=""
AUTO=""
for arg in "$@"; do
    case $arg in
        --force) FORCE="force" ;;
        --auto) AUTO="auto" ;;
    esac
done

# Determine command
if [ -n "$FORCE" ]; then
    CMD="force"
elif [ -n "$AUTO" ]; then
    CMD="auto"
else
    CMD="review"
fi

# Run review
RESULT=$("$PYTHON" "$REVIEW_SCRIPT" "$CMD" 2>&1)
EXIT_CODE=$?

# Check if review was actually performed (not skipped)
if echo "$RESULT" | grep -q "Review ID:"; then
    # Review was performed - log and show summary
    TIMESTAMP=$(date +%Y-%m-%d_%H:%M:%S)

    # Extract key info
    HEALTH=$(echo "$RESULT" | grep "Overall Health:" | cut -d: -f2 | tr -d ' ')
    PROPOSALS=$(echo "$RESULT" | grep -c "Hypothesis:" || echo "0")

    # Log
    echo "[$TIMESTAMP] Review completed - Health: $HEALTH, Proposals: $PROPOSALS" >> "$REVIEW_LOG"

    # Output for user visibility (will appear in hook output)
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "🔬 HOOK REVIEW AGENT CHECK-IN"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "$RESULT"
    echo ""
    echo "Full review saved to: $REVIEW_LOG"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # Save full report
    echo "" >> "$REVIEW_LOG"
    echo "=== Full Report ($TIMESTAMP) ===" >> "$REVIEW_LOG"
    echo "$RESULT" >> "$REVIEW_LOG"
    echo "" >> "$REVIEW_LOG"

elif echo "$RESULT" | grep -q "skipped"; then
    # Review was skipped - silent exit (don't spam session end)
    exit 0
else
    # Some error
    echo "Hook review error: $RESULT" >&2
    exit $EXIT_CODE
fi

exit 0
