#!/bin/bash
# =============================================================================
# Auto-Capture Results + Post-Work Analysis Hook for Cursor IDE
# =============================================================================
# Runs on afterFileEdit - captures successes AND provides forward-looking analysis
#
# NEW: Post-Work Analysis (LLM Thinking Mode)
# - Reflects on what was accomplished
# - Anticipates next steps (Butler + Agent reasoning)
# - Analyzes dependencies and failure risks
# - Suggests hardening and ecosystem harmonization
#
# CURSOR-SPECIFIC:
# - Uses afterFileEdit hook (Cursor's equivalent to PostToolUse)
# - Receives JSON with edited files, conversation context
# - Captures objective successes (exit 0, tests passing, etc.)
#
# ISOLATION GUARANTEE:
# - Separate from Claude Code's auto-capture-results.sh
# - Cannot affect Claude Code operation
#
# DELIVERY: afterFileEdit → analyze changes → capture wins → provide analysis
# =============================================================================

set -e

REPO_DIR="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON="$REPO_DIR/.venv/bin/python3"

# Cursor-specific log
CAPTURE_LOG="/tmp/context-dna-cursor-capture.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cursor afterFileEdit triggered" >> "$CAPTURE_LOG"

# Load env
ENV_FILE="$REPO_DIR/context-dna/infra/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# =============================================================================
# PARSE CURSOR'S afterFileEdit JSON
# =============================================================================
# Cursor sends:
# {
#   "files": [{"path": "...", "content": "..."}],
#   "conversationId": "...",
#   "generationId": "...",
#   "workspaceRoots": [...]
# }

if [ -n "$1" ]; then
    # Test mode
    EDITED_FILES="$1"
else
    INPUT=$(cat)
    
    EDITED_FILES=$("$PYTHON" -c "
import sys, json
try:
    data = json.loads('''$INPUT''')
    files = data.get('files', [])
    paths = [f.get('path', '') for f in files if f.get('path')]
    print('|'.join(paths) if paths else '')
except:
    print('')
" 2>/dev/null)
fi

if [ -z "$EDITED_FILES" ]; then
    echo "[$(date)] No files edited" >> "$CAPTURE_LOG"
    exit 0
fi

echo "[$(date)] Files edited: $EDITED_FILES" >> "$CAPTURE_LOG"

# =============================================================================
# OBJECTIVE SUCCESS DETECTION
# =============================================================================
# Check if recent operations show objective success signals:
# - Tests passing (exit 0)
# - Services healthy (200 OK)
# - Files saved without errors
# - Linter passing

PYTHONPATH="$REPO_DIR" "$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')

try:
    from memory.objective_success import ObjectiveSuccessDetector
    from memory.auto_capture import capture_objective_success
    
    # Get recent work log entries
    from memory.architecture_enhancer import work_log
    entries = work_log.get_recent_entries(hours=1, include_processed=False)
    
    if entries:
        detector = ObjectiveSuccessDetector()
        detector.analyze_entries(entries)
        
        # Get objective successes (don't need user confirmation)
        wins = detector.get_objective_successes_without_user(min_confidence=0.7)
        
        if wins:
            for win in wins[:3]:  # Capture top 3
                try:
                    capture_objective_success(
                        task=win.task,
                        details=win.evidence[0] if win.evidence else '',
                        confidence=win.confidence,
                        source='cursor_afterFileEdit'
                    )
                except:
                    pass

except Exception as e:
    # Non-blocking - don't fail if capture fails
    pass
" >> "$CAPTURE_LOG" 2>&1 &

# Log completion
echo "[$(date)] Capture analysis queued" >> "$CAPTURE_LOG"

# =============================================================================
# POST-WORK ANALYSIS (LLM Thinking Mode)
# =============================================================================
# Provide comprehensive forward-looking analysis:
# - Anticipated next steps (Butler + Agent)
# - Dependency analysis
# - Failure prediction (100 customers, 30 days)
# - Hardening recommendations
# - Ecosystem harmonization
# - Performance considerations

# Build work summary from files edited
WORK_SUMMARY="Modified: $EDITED_FILES"

# Generate comprehensive analysis via LLM
PYTHONPATH="$REPO_DIR" "$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')

try:
    from memory.post_work_analysis import generate_post_work_analysis
    
    analysis = generate_post_work_analysis(
        work_summary='''$WORK_SUMMARY''',
        files_modified='''$EDITED_FILES'''.split('|') if '''$EDITED_FILES''' else [],
        session_context='Cursor session - file edits completed'
    )
    
    if analysis:
        print(analysis)
except Exception as e:
    # Non-blocking - don't fail if analysis fails
    pass
" 2>/dev/null || true  # Never fail the hook

echo "[$(date)] Post-work analysis completed" >> "$CAPTURE_LOG"

exit 0
