#!/bin/bash
# Auto-Session Summary Hook for Claude Code
# Runs on SessionEnd - summarizes session wins and captures to brain
#
# ARCHITECTURE:
#   SessionEnd fires when Claude Code conversation ends
#   We analyze the transcript for wins that weren't captured by PostToolUse
#   Provides end-of-session summary capture
#
# This complements:
#   - PostToolUse (captures individual command wins)
#   - UserPromptSubmit (captures user confirmations)
# Together they provide 100% autonomous win capture.

# Configuration - use environment variables with defaults
REPO_DIR="${CONTEXT_DNA_REPO:-$HOME/dev/er-simulator-superrepo}"
CONTEXT_DNA_DIR="${CONTEXT_DNA_DIR:-$HOME/.context-dna}"
PYTHON="${CONTEXT_DNA_PYTHON:-$REPO_DIR/.venv/bin/python3}"
LOG_FILE="$CONTEXT_DNA_DIR/.session_summary.log"

# Ensure log directory exists
mkdir -p "$CONTEXT_DNA_DIR"

# Read hook input from stdin (contains session_id, transcript summary)
INPUT=$(cat)

# Extract session info
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript // empty' 2>/dev/null)

# Log session end
echo "[$(date -Iseconds)] Session ended: $SESSION_ID" >> "$LOG_FILE"

# Skip if no transcript
if [ -z "$TRANSCRIPT" ]; then
    exit 0
fi

# Convert to lowercase for pattern matching
TRANSCRIPT_LOWER=$(echo "$TRANSCRIPT" | tr '[:upper:]' '[:lower:]')

# =============================================================================
# DETECT SESSION-LEVEL WINS (patterns in full transcript)
# =============================================================================

SESSION_WINS=""

# User confirmations we might have missed
if echo "$TRANSCRIPT_LOWER" | grep -qE "(that worked|success!|perfect|awesome|great job|exactly what i needed)"; then
    SESSION_WINS="$SESSION_WINS\n- User confirmed success during session"
fi

# Major completions
if echo "$TRANSCRIPT_LOWER" | grep -qE "(feature complete|implementation done|task completed|all tests pass|deployed to production)"; then
    SESSION_WINS="$SESSION_WINS\n- Major task completed"
fi

# Architecture work
if echo "$TRANSCRIPT_LOWER" | grep -qE "(terraform apply.*complete|docker.*running|ecs.*updated|lambda.*deployed)"; then
    SESSION_WINS="$SESSION_WINS\n- Infrastructure changes applied"
fi

# Bug fixes
if echo "$TRANSCRIPT_LOWER" | grep -qE "(bug.*fixed|issue.*resolved|error.*fixed|problem.*solved)"; then
    SESSION_WINS="$SESSION_WINS\n- Bug fix completed"
fi

# =============================================================================
# CAPTURE SESSION SUMMARY
# =============================================================================

if [ -n "$SESSION_WINS" ]; then
    echo "[$(date -Iseconds)] Session wins detected:$SESSION_WINS" >> "$LOG_FILE"

    # Capture session summary to brain
    "$PYTHON" -c "
from memory.brain import brain
import datetime

brain.capture_win(
    task='Session completed with wins',
    details='''Session $SESSION_ID ended with accomplishments:$SESSION_WINS''',
    area='session',
    command='session_end'
)
" 2>/dev/null &
fi

# =============================================================================
# HOOK EVOLUTION: Finalize session outcomes for all hooks
# =============================================================================
# At session end, ensure all hooks that fired during this session have outcomes.
# Hooks without explicit outcomes are marked as neutral.
# This provides complete data for A/B test evaluation.

"$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.hook_evolution import get_hook_evolution_engine
    engine = get_hook_evolution_engine()
    engine.finalize_session_outcomes('$SESSION_ID', '''${TRANSCRIPT:0:2000}''')
except Exception as e:
    pass
" 2>/dev/null &

exit 0
