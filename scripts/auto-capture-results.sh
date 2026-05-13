#!/bin/bash
# Auto-Capture Results Hook for Claude Code
# Runs on PostToolUse - captures objective wins from tool results
#
# ARCHITECTURE:
#   PostToolUse fires AFTER every successful tool execution
#   We have access to: tool_name, tool_input, tool_response
#   We analyze results for OBJECTIVE success signals and capture wins
#
# OBJECTIVE SUCCESS SIGNALS (no user input needed):
#   - Bash exit code 0 with success keywords
#   - Git commit succeeded
#   - Tests passed
#   - Health check healthy
#   - Deploy completed
#   - File written successfully
#
# This complements UserPromptSubmit (which captures user confirmations)
# Together they provide 100% autonomous win capture.

# Configuration - use environment variables with defaults
REPO_DIR="${CONTEXT_DNA_REPO:-$HOME/dev/er-simulator-superrepo}"
CONTEXT_DNA_DIR="${CONTEXT_DNA_DIR:-$HOME/.context-dna}"
PYTHON="${CONTEXT_DNA_PYTHON:-$REPO_DIR/.venv/bin/python3}"
BRAIN_TOOL="$REPO_DIR/memory/brain.py"
LOG_FILE="$CONTEXT_DNA_DIR/.auto_capture.log"

# Ensure log directory exists
mkdir -p "$CONTEXT_DNA_DIR"

# Session ID for hook outcome attribution (should match UserPromptSubmit hook)
SESSION_ID="${CLAUDE_SESSION_ID:-$(date +%Y%m%d)_session}"

# Read hook input from stdin
INPUT=$(cat)

# Extract tool info
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
TOOL_RESPONSE=$(echo "$INPUT" | jq -r '.tool_response // empty' 2>/dev/null)
TOOL_INPUT=$(echo "$INPUT" | jq -r '.tool_input // empty' 2>/dev/null)

# =============================================================================
# AGENT REVIEW BRIDGE: Detect Task tool completions
# =============================================================================
# PostToolUse fires AFTER the agent completes — tool_response has the output.
# We enqueue + mark completed + trigger async Synaptic review in one shot.
# Background agents (run_in_background=true) only get enqueued (no output yet).
if [ "$TOOL_NAME" = "Task" ]; then
    AGENT_TASK=$(echo "$TOOL_INPUT" | jq -r '.prompt // empty' 2>/dev/null | head -c 500)
    AGENT_DESC=$(echo "$TOOL_INPUT" | jq -r '.description // empty' 2>/dev/null | head -c 200)
    AGENT_OUTPUT=$(echo "$TOOL_RESPONSE" 2>/dev/null | head -c 2000)
    IS_BG=$(echo "$TOOL_INPUT" | jq -r '.run_in_background // "false"' 2>/dev/null)
    if [ -n "$AGENT_TASK" ]; then
        "$PYTHON" "$REPO_DIR/memory/agent_review_bridge.py" enqueue "$SESSION_ID" "$AGENT_DESC: $AGENT_TASK" "$AGENT_DESC" 2>/dev/null

        # For non-background agents, we have output — complete + review
        if [ "$IS_BG" != "true" ] && [ ${#AGENT_OUTPUT} -gt 20 ]; then
            "$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
from memory.agent_review_bridge import mark_completed
entry = mark_completed('$SESSION_ID', '''$(echo "$AGENT_OUTPUT" | tr "'" " " | head -c 2000)''')
if entry:
    try:
        from memory.synaptic_reviewer import trigger_review_async
        trigger_review_async('$SESSION_ID', entry['agent_id'])
    except Exception:
        pass
" 2>/dev/null &
        fi
        echo "[$(date -Iseconds)] AGENT: $AGENT_DESC [bg=$IS_BG]" >> "$LOG_FILE"
    fi
    exit 0
fi

# Only process Bash commands (main source of objective wins)
if [ "$TOOL_NAME" != "Bash" ]; then
    exit 0
fi

# Extract command and output from tool_input/tool_response
COMMAND=$(echo "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null)
EXIT_CODE=$(echo "$TOOL_RESPONSE" | jq -r '.exit_code // 0' 2>/dev/null)
STDOUT=$(echo "$TOOL_RESPONSE" | jq -r '.stdout // empty' 2>/dev/null)

# Skip if no command or non-zero exit
if [ -z "$COMMAND" ] || [ "$EXIT_CODE" != "0" ]; then
    exit 0
fi

# Combine for analysis
COMBINED="$COMMAND $STDOUT"
COMBINED_LOWER=$(echo "$COMBINED" | tr '[:upper:]' '[:lower:]')

# =============================================================================
# OBJECTIVE SUCCESS DETECTION
# =============================================================================

DETECTED_WIN=""
DETECTED_AREA=""
DETECTED_DETAILS=""

# Git commit success
if echo "$COMMAND" | grep -qE "^git commit" && echo "$STDOUT" | grep -qE "(create mode|insertions|deletions|\[\w+\s+\w+\])"; then
    DETECTED_WIN="Git commit succeeded"
    DETECTED_AREA="git"
    DETECTED_DETAILS=$(echo "$STDOUT" | head -3)
fi

# Docker/container success - ONLY for actual deployment, NOT status checks
# Require action verb: up, restart, deploy, run (not: ps, status, logs, exec)
if echo "$COMMAND" | grep -qE "docker(-compose)?.*(up|restart|deploy|run|build)" && \
   echo "$COMBINED_LOWER" | grep -qE "(container.*(healthy|running|started)|successfully|created)"; then
    DETECTED_WIN="Docker deployment completed"
    DETECTED_AREA="docker"
    DETECTED_DETAILS=$(echo "$STDOUT" | grep -iE "(healthy|running|started|created)" | head -3)
fi

# Terraform apply success
if echo "$COMMAND" | grep -qE "terraform (apply|plan)" && echo "$STDOUT" | grep -qE "(Apply complete|Plan:|resources)"; then
    DETECTED_WIN="Terraform operation completed"
    DETECTED_AREA="terraform"
    DETECTED_DETAILS=$(echo "$STDOUT" | grep -E "(Apply complete|added|changed|destroyed)" | head -3)
fi

# Test success
if echo "$COMBINED_LOWER" | grep -qE "(tests? passed|all tests|passed.*tests|\d+ passed|pytest.*ok|jest.*passed)"; then
    DETECTED_WIN="Tests passed"
    DETECTED_AREA="testing"
    DETECTED_DETAILS=$(echo "$STDOUT" | grep -iE "(passed|ok|success)" | head -3)
fi

# Deployment success
if echo "$COMBINED_LOWER" | grep -qE "(deploy(ed|ment)?.*(success|complete)|successfully deployed|service.*updated)"; then
    DETECTED_WIN="Deployment succeeded"
    DETECTED_AREA="deployment"
    DETECTED_DETAILS=$(echo "$STDOUT" | grep -iE "(deploy|success|complete)" | head -3)
fi

# Health check success - SKIP routine checks
# Health checks are verification, not wins. Only capture if part of deployment flow.
# (Removed - was capturing every curl to /health endpoint)

# Memory system success (our own system)
if echo "$COMBINED_LOWER" | grep -qE "(sop extraction triggered|agent success recorded|recorded.*sop|brain.*100%)"; then
    DETECTED_WIN="Memory system operation succeeded"
    DETECTED_AREA="memory"
    DETECTED_DETAILS=$(echo "$STDOUT" | grep -iE "(recorded|triggered|success)" | head -3)
fi

# =============================================================================
# CAPTURE THE WIN
# =============================================================================

if [ -n "$DETECTED_WIN" ]; then
    # Log for debugging
    echo "[$(date -Iseconds)] WIN: $DETECTED_WIN [$DETECTED_AREA]" >> "$LOG_FILE"

    # Capture to brain ONLY (single source of truth, prevents race conditions)
    # Brain.capture_win handles storage to learning_store internally
    "$PYTHON" -c "
from memory.brain import brain
brain.capture_win(
    task='$DETECTED_WIN',
    details='''$DETECTED_DETAILS''',
    area='$DETECTED_AREA',
    command='''$(echo "$COMMAND" | head -c 200)'''
)
" 2>/dev/null &
    # Note: Removed duplicate curl to /api/learnings - was causing race conditions

    # =============================================================================
    # HOOK EVOLUTION: Attribute positive outcome to hooks that fired this session
    # =============================================================================
    # When we detect a win, credit the hooks that were active during this session.
    # This enables A/B testing to determine which hook variants lead to better outcomes.
    "$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.hook_evolution import get_hook_evolution_engine
    engine = get_hook_evolution_engine()
    engine.attribute_session_outcome(
        session_id='$SESSION_ID',
        outcome='positive',
        signals=['task_completed', '$DETECTED_WIN', '$DETECTED_AREA'],
        task_completed=True,
        confidence=0.8
    )
except Exception as e:
    pass
" 2>/dev/null &

    # Output for Claude's context (optional - can be noisy)
    # echo "🎯 Auto-captured: $DETECTED_WIN [$DETECTED_AREA]"
fi

exit 0
