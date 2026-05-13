#!/bin/bash
# =============================================================================
# Auto-Memory Query Hook for Cursor IDE
# =============================================================================
# Runs on beforeSubmitPrompt - injects memory context before Cursor agent processes prompts
#
# CURSOR-SPECIFIC:
# - Uses CURSOR_FILE_PATH env var (not CLAUDE_PROJECT_DIR)
# - Receives JSON on stdin: {"prompt": "...", "files": [...], "conversationId": "..."}
# - Returns context to stdout (injected into prompt)
#
# ISOLATION GUARANTEE:
# - Separate from Claude Code (scripts/auto-memory-query.sh)
# - Can't affect Claude Code operation
# - Uses shared memory modules (read-only)
#
# DELIVERY: beforeSubmitPrompt → stdout → injected before agent sees prompt
# =============================================================================

set -e

# =============================================================================
# CURSOR-SPECIFIC CONFIGURATION
# =============================================================================
REPO_DIR="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
CONTEXT_DNA_DIR="$HOME/.context-dna"
PYTHON="$REPO_DIR/.venv/bin/python3"

# Cursor-specific env var namespace
export IDE_TYPE="cursor"
export IDE_FAMILY="cursor"

# Hook execution log (Cursor-specific)
HOOK_LOG="/tmp/context-dna-cursor-hook.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cursor hook called" >> "$HOOK_LOG"

# Load credentials for PostgreSQL/Redis
ENV_FILE="$REPO_DIR/context-dna/infra/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# =============================================================================
# PROMPT EXTRACTION (Cursor-Specific JSON Format)
# =============================================================================
# Cursor's beforeSubmitPrompt sends JSON on stdin:
# {
#   "prompt": "user's message",
#   "files": [{"path": "...", "content": "..."}],
#   "conversationId": "abc123",
#   "generationId": "xyz789",
#   "workspaceRoots": ["/path/to/workspace"]
# }

if [ -n "$1" ]; then
    # Test mode: prompt passed as argument
    PROMPT="$1"
    SESSION_ID="cursor-test-$$"
else
    # Production: Read JSON from stdin
    INPUT=$(cat)
    
    PROMPT=$("$PYTHON" -c "
import sys, json
try:
    data = json.loads('''$INPUT''')
    print(data.get('prompt', ''))
except:
    # Fallback: use raw input
    print('''$INPUT''')
" 2>/dev/null)
    
    # Extract session ID from conversationId
    SESSION_ID=$("$PYTHON" -c "
import sys, json
try:
    data = json.loads('''$INPUT''')
    conv_id = data.get('conversationId', '')
    # Use conversationId as session ID for continuity
    print(f'cursor-{conv_id}' if conv_id else 'cursor-default')
except:
    print('cursor-default')
" 2>/dev/null)
fi

# If we couldn't extract the prompt, exit silently
if [ -z "$PROMPT" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] No prompt extracted, exiting" >> "$HOOK_LOG"
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Prompt: ${PROMPT:0:60}..." >> "$HOOK_LOG"

# =============================================================================
# SYNAPTIC DIRECT RESPONSE (Same as Claude Code)
# =============================================================================
SYNAPTIC_ADDRESS_PATTERN="^synaptic[,:]|^hey synaptic|^@synaptic|synaptic[?]$|synaptic,? are you|synaptic,? can you"

if echo "$PROMPT" | grep -qiE "$SYNAPTIC_ADDRESS_PATTERN"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Synaptic addressed directly" >> "$HOOK_LOG"
    
    SYNAPTIC_RESPONSE=$(curl -s -X POST "http://localhost:8888/speak-direct" \
        -H "Content-Type: application/json" \
        -d "{\"message\": \"$PROMPT\"}" \
        --max-time 30 2>/dev/null | "$PYTHON" -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('full_response', data.get('response_preview', data.get('error', 'No response'))))
except:
    print('Synaptic is thinking...')
" 2>/dev/null)
    
    if [ -n "$SYNAPTIC_RESPONSE" ]; then
        echo ""
        echo "╔══════════════════════════════════════════════════════════════════════╗"
        echo "║  🧠 SYNAPTIC SPEAKS (via Cursor Hook)                                ║"
        echo "╠══════════════════════════════════════════════════════════════════════╣"
        echo ""
        echo "$SYNAPTIC_RESPONSE"
        echo ""
        echo "╚══════════════════════════════════════════════════════════════════════╝"
    fi
fi

# =============================================================================
# CONTEXT INJECTION - Use Unified Structure (9-Section Payload)
# =============================================================================
# Always use hybrid mode (best of layered + greedy)
INJECTION_MODE="hybrid"

# Generate context via persistent_hook_structure.py
"$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')

from memory.persistent_hook_structure import generate_context_injection

try:
    result = generate_context_injection(
        prompt='''${PROMPT}''',
        mode='$INJECTION_MODE',
        session_id='$SESSION_ID'
    )
    
    if hasattr(result, 'content') and result.content:
        print(result.content)
    else:
        # Fallback if no content generated
        print('Context DNA ready - querying memory...')
except Exception as e:
    # Ultimate fallback - never crash Cursor
    print(f'[Context DNA] Ready (fallback mode: {str(e)[:50]})')
" 2>/dev/null || echo "[Context DNA] Ready (minimal mode)"

# =============================================================================
# SUCCESS CAPTURE REMINDER
# =============================================================================
# Detect success keywords and remind to capture
SUCCESS_KEYWORDS="success|worked|perfect|excellent|awesome|nice|great|fixed|solved"

if echo "$PROMPT" | grep -qiE "($SUCCESS_KEYWORDS)"; then
    echo ""
    echo "💡 Success detected! Remember to capture:"
    echo "   python memory/brain.py success \"<task>\" \"<what worked>\""
    echo ""
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Hook completed" >> "$HOOK_LOG"

exit 0
