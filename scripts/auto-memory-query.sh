#!/bin/bash
# Auto-Memory Query Hook for Claude Code
# Runs on UserPromptSubmit - injects memory context before Atlas processes prompts
#
# THE BRAIN ANALOGY:
#
# PROFESSOR (Layer 1) = WISDOM CENTER
#   - How to approach problems
#   - Mental models to apply
#   - Landmines to avoid
#   - Patterns veterans use
#   → Shapes HOW the agent thinks
#
# BRAIN LEARNINGS (Layer 2) = LONG-TERM MEMORY
#   - Specific past experiences
#   - Detailed SOPs and procedures
#   - Real examples from the codebase
#   - The 47+ learnings accumulated over time
#   → Provides WHAT the agent knows
#
# Together: Wisdom guides interpretation → Memory provides specifics → Excellence
#
# GUARANTEED LAYERS (moderate+ risk tasks):
#   Layer 0: Codebase Locator (file breadcrumbs) ← ALWAYS (WHERE to look)
#   Layer 1: Professor (guiding wisdom) ← ALWAYS (HOW to think)
#   Layer 2: Brain Learnings (specific experiences) ← ALWAYS (WHAT happened before)
#   Layer 3: Gotcha Check (specific warnings) ← ALWAYS (WHAT to avoid)
#   Layer 4: Architecture Blueprint ← critical/high only
#   Layer 5: Brain State ← critical only
#
# FIVE FUNCTIONS (100% Autonomy + Professional Features):
# 1. CONTEXT INJECTION - Queries ALL memory systems based on task risk
# 2. SUCCESS CAPTURE - Detects user confirmations and prompts to record wins
# 3. OBJECTIVE SUCCESS REVIEW - Detects system-confirmed wins
# 4. BREADTH-BASED SEARCH - Cross-references related hierarchy branches
# 5. ENHANCEMENT SUGGESTIONS - Periodic pattern detection for improvements
#
# DEPTH = INVERSE OF FIRST-TRY SUCCESS LIKELIHOOD
# The harder a task is to get right first try, the deeper we search.
#
# PROFESSIONAL FEATURES:
# - Breadth-based hierarchy search (risk → wider branch search)
# - Cross-reference linking between categories
# - Config file change tracking
# - Version-aware learnings (git commit tagging)
# - Stale detection for outdated learnings
# - Auto-enhancement suggestions (patterns → SOPs)
#
# QUERIES:
# - memory/codebase_locator.py (file breadcrumbs: WHERE to look)
# - memory/query.py (Context DNA SOPs + patterns)
# - memory/context.py (full architecture blueprints)
# - memory/knowledge_graph.py (hierarchical search with cross-refs)
# - memory/auto_enhance.py (pattern detection suggestions)

set -e

# =============================================================================
# DEBUG LOGGING - Track when hook is called
# =============================================================================
HOOK_LOG="/tmp/context-dna-hook.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Hook called with args: $*" >> "$HOOK_LOG"

# =============================================================================
# DEDUP GUARD - Claude Code fires UserPromptSubmit twice per prompt in VS Code
# Skip the second invocation if called within 2 seconds of the last
# =============================================================================
DEDUP_FILE="/tmp/.context-dna-hook-dedup"
NOW=$(date +%s)
if [ -f "$DEDUP_FILE" ]; then
    LAST=$(cat "$DEDUP_FILE" 2>/dev/null || echo 0)
    DIFF=$((NOW - LAST))
    if [ "$DIFF" -lt 2 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] DEDUP: skipped (${DIFF}s since last)" >> "$HOOK_LOG"
        exit 0
    fi
fi
echo "$NOW" > "$DEDUP_FILE"

# =============================================================================
# CONFIGURATION - Use environment variables with defaults
# =============================================================================
# These can be overridden by setting environment variables:
#   CONTEXT_DNA_REPO - Path to the er-simulator-superrepo
#   CONTEXT_DNA_DIR - Path to user data directory (~/.context-dna)
#   CONTEXT_DNA_PYTHON - Path to Python interpreter

REPO_DIR="${CONTEXT_DNA_REPO:-${CLAUDE_PROJECT_DIR:-$HOME/dev/er-simulator-superrepo}}"
CONTEXT_DNA_DIR="${CONTEXT_DNA_DIR:-$HOME/.context-dna}"
# Use 'python' not 'python3' — venv: python→3.14 (correct), python3→3.9 (Xcode, wrong)
PYTHON="${CONTEXT_DNA_PYTHON:-$REPO_DIR/.venv/bin/python}"

# =============================================================================
# LOAD CREDENTIALS - Source .env file if it exists (for PostgreSQL/Redis auth)
# =============================================================================
ENV_FILE="$REPO_DIR/context-dna/infra/.env"
if [ -f "$ENV_FILE" ]; then
    set -a  # Export all variables
    source "$ENV_FILE"
    set +a
fi

# Script paths (all relative to REPO_DIR)
QUERY_SCRIPT="$REPO_DIR/memory/query.py"
CONTEXT_SCRIPT="$REPO_DIR/memory/context.py"
BRAIN_SCRIPT="$REPO_DIR/memory/brain.py"
KNOWLEDGE_GRAPH_SCRIPT="$REPO_DIR/memory/knowledge_graph.py"
AUTO_ENHANCE_SCRIPT="$REPO_DIR/memory/auto_enhance.py"
PROFESSOR_SCRIPT="$REPO_DIR/memory/professor.py"
HOOK_EVOLUTION_SCRIPT="$REPO_DIR/memory/hook_evolution.py"
CODEBASE_LOCATOR_SCRIPT="$REPO_DIR/memory/codebase_locator.py"
PERSISTENT_HOOK_SCRIPT="$REPO_DIR/memory/persistent_hook_structure.py"

# =============================================================================
# INJECTION MODE: layered (legacy) or unified (new persistent structure)
# =============================================================================
# Read mode from config file, default to "layered" for backward compatibility
# Modes:
#   - layered: Traditional 6-layer system (Layer 0-5)
#   - hybrid:  New unified structure (greedy + layered best features)
#   - greedy:  Agent-focused wishlist (exact file, SOPs, gotchas)
#   - minimal: Safety + Foundation only (fast for low-risk)
INJECTION_MODE_FILE="$REPO_DIR/memory/.context_injection_mode"
if [ -f "$INJECTION_MODE_FILE" ]; then
    INJECTION_MODE=$(cat "$INJECTION_MODE_FILE" | tr -d '[:space:]')
else
    INJECTION_MODE="layered"  # Default to legacy for backward compatibility
fi

# Ensure user data directory exists
mkdir -p "$CONTEXT_DNA_DIR"

# Generate session ID for hook tracking (persistent within Claude Code session)
SESSION_ID="${CLAUDE_SESSION_ID:-$(date +%Y%m%d_%H%M%S)_$$}"

# =============================================================================
# PROMPT EXTRACTION - Support both command line args and stdin
# =============================================================================
# Claude Code hooks can pass prompt as:
# 1. Command line argument: $1
# 2. Stdin (JSON format with 'prompt' key)
# 3. Environment variable: $PROMPT

if [ -n "$1" ]; then
    # Prompt passed as command line argument
    PROMPT="$1"
elif [ -n "$PROMPT" ]; then
    # Prompt already in environment variable
    :  # Keep existing PROMPT
else
    # Read from stdin (JSON format from Claude Code)
    INPUT=$(cat)

    # Extract prompt using a more robust method that handles special characters
    PROMPT=$(echo "$INPUT" | "$PYTHON" -c "
import sys, json
try:
    raw = sys.stdin.read()
    data = json.loads(raw)
    # Try multiple possible keys Claude Code might use
    prompt = data.get('prompt') or data.get('message') or data.get('content') or data.get('text', '')
    print(prompt)
except json.JSONDecodeError:
    # Not JSON - use raw input as prompt
    print(raw.strip())
except Exception:
    print('')
" 2>/dev/null)

    # If extraction failed, use raw input (truncated)
    if [ -z "$PROMPT" ]; then
        PROMPT=$(echo "$INPUT" | head -c 500)
    fi
fi

# If we couldn't extract the prompt, exit silently
if [ -z "$PROMPT" ]; then
    exit 0
fi

# =============================================================================
# AUTOMATED/CRON BYPASS - Skip injection for fleet-check and cron ticks
# =============================================================================
# The 1-min fleet-check cron loop burned millions of tokens per session by
# triggering full 9-section injection on every tick. Gate: if the prompt is
# only bash fleet-check commands, skip injection entirely.
# Also skip for /loop prompts that are just monitoring commands.
if echo "$PROMPT" | grep -qE '^[[:space:]]*(bash[[:space:]]+.*/fleet-check\.sh[[:space:]]*)+$'; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] BYPASS: fleet-check cron tick" >> "$HOOK_LOG"
    exit 0
fi

# =============================================================================
# SHORT PROMPT BYPASS - Skip injection for ≤5 word messages
# =============================================================================
# Very short prompts ("do it", "yes", "ok thanks", "go ahead") don't benefit
# from full 9-section injection. This is the ONLY injection bypass exception.
WORD_COUNT=$(echo "$PROMPT" | wc -w | tr -d ' ')
if [ "$WORD_COUNT" -le 5 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] BYPASS: short prompt ($WORD_COUNT words): ${PROMPT:0:50}" >> "$HOOK_LOG"
    exit 0
fi

# =============================================================================
# SYNAPTIC DIRECT RESPONSE TRIGGER
# =============================================================================
# When Aaron addresses Synaptic directly, trigger Synaptic to respond via the
# independent voice channel (outbox + daemon). Atlas is NOT the middleman.
#
# Detection patterns: "synaptic," "hey synaptic" "@synaptic" "synaptic?"
# The webhook triggers Synaptic → outbox → daemon → independent delivery
# Atlas does NOT present Synaptic's response - the daemon handles it.

SYNAPTIC_ADDRESS_PATTERN="^synaptic[,:]|^hey synaptic|^@synaptic|synaptic[?]$|synaptic,? are you|synaptic,? can you|synaptic,? do you|synaptic,? what|synaptic,? how|synaptic,? why"

# Check if Aaron is addressing Synaptic directly
if echo "$PROMPT" | grep -qiE "$SYNAPTIC_ADDRESS_PATTERN"; then
    # Call Synaptic SYNCHRONOUSLY and print response directly to this chat
    # The response appears in the webhook output = visible in Claude Code chat

    SYNAPTIC_RESPONSE=$(curl -s -X POST "http://localhost:8888/speak-direct" \
        -H "Content-Type: application/json" \
        -d "{\"message\": \"$PROMPT\"}" \
        --max-time 35 2>/dev/null | "$PYTHON" -c "
import sys, json
try:
    data = json.load(sys.stdin)
    # Use full_response for complete Synaptic wisdom, fall back to preview
    print(data.get('full_response', data.get('response_preview', data.get('error', 'No response'))))
except:
    print('Synaptic is thinking...')
" 2>/dev/null)

    # Output Synaptic's response directly - appears in this chat thread!
    if [ -n "$SYNAPTIC_RESPONSE" ]; then
        echo ""
        echo "╔══════════════════════════════════════════════════════════════════════╗"
        echo "║  🧠 SYNAPTIC SPEAKS (Direct LLM Response)                            ║"
        echo "╠══════════════════════════════════════════════════════════════════════╣"
        echo ""
        echo "$SYNAPTIC_RESPONSE"
        echo ""
        echo "╚══════════════════════════════════════════════════════════════════════╝"
    fi

    # Log the trigger
    echo "[Webhook] Synaptic direct response for: ${PROMPT:0:50}..." >> "$REPO_DIR/memory/.synaptic_webhook_triggers.log" 2>/dev/null
fi

# =============================================================================
# HOOK EVOLUTION: Get active variant for A/B/C testing
# =============================================================================
# The hook evolution system tracks which hook variant is active and records
# outcomes to objectively evaluate hook effectiveness over time.
#
# A/B/C Testing Strategy:
# - Control (baseline): Current production configuration
# - Variant A (minor): Small phrasing/formatting tweaks
# - Variant B (major): Dramatic structural changes
# - Variant C (wisdom): User-learned wisdom injections
#
# Selection is deterministic based on session_id for consistency.
# Distribution: 40% control, 20% A, 20% B, 20% C
#
# C variant analyzes user prompts for effective patterns (stakes framing,
# quality signals, vision anchoring, etc.) and injects proven patterns
# situationally to improve outcomes.

# Get active variant AND any wisdom injection for C variant
HOOK_RESULT=$("$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.hook_evolution import get_hook_evolution_engine
    import json

    engine = get_hook_evolution_engine()
    prompt_text = '''${PROMPT:0:1000}'''

    # Get variant and potential wisdom injection
    variant, wisdom_injection = engine.get_active_variant(
        'UserPromptSubmit',
        '$SESSION_ID',
        prompt_text=prompt_text
    )

    # Output as JSON for parsing
    result = {
        'variant_id': variant.variant_id,
        'wisdom_injection': wisdom_injection or ''
    }
    print(json.dumps(result))

except Exception as e:
    import json
    print(json.dumps({'variant_id': 'userpromptsubmit_default', 'wisdom_injection': ''}))
" 2>/dev/null || echo '{"variant_id":"userpromptsubmit_default","wisdom_injection":""}')

# Parse the result
HOOK_VARIANT_ID=$(echo "$HOOK_RESULT" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('variant_id','userpromptsubmit_default'))" 2>/dev/null || echo "userpromptsubmit_default")
WISDOM_INJECTION=$(echo "$HOOK_RESULT" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('wisdom_injection',''))" 2>/dev/null || echo "")

# Record that hook fired (for later outcome attribution)
"$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.hook_evolution import get_hook_evolution_engine
    engine = get_hook_evolution_engine()
    engine.record_hook_fired('$HOOK_VARIANT_ID', '$SESSION_ID', '''${PROMPT:0:500}''', '')
except:
    pass
" 2>/dev/null &

# Also record prompt patterns for the prompt pattern analyzer
"$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.prompt_pattern_analyzer import get_analyzer
    analyzer = get_analyzer()
    analyzer.record_session_patterns('$SESSION_ID', '''${PROMPT:0:2000}''', '')
except:
    pass
" 2>/dev/null &

# =============================================================================
# FUNCTION 1: SUCCESS CAPTURE - Detect user confirmations and prompt recording
# =============================================================================
# User confirmation patterns that indicate previous work succeeded
SUCCESS_KEYWORDS="success|worked|perfect|excellent|awesome|beautiful|nice|great|looks good|that's it|nailed it|exactly|yes!|yep!|done|fixed|solved"

# Check if the user message looks like a success confirmation
if echo "$PROMPT" | grep -qiE "($SUCCESS_KEYWORDS)"; then
    # Check for uncaptured objective successes
    UNCAPTURED=$("$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.objective_success import get_objective_successes
    from memory.architecture_enhancer import work_log

    # Get recent entries to find what the success refers to
    entries = work_log.get_recent_entries(hours=2, include_processed=False)

    # Find the most recent task-like entry (command, success, observation)
    recent_task = None
    for entry in reversed(entries):
        if entry.get('entry_type') in ('command', 'success', 'observation'):
            content = entry.get('content', '')
            if len(content) > 10:  # Meaningful content
                recent_task = content[:100]
                break

    if recent_task:
        print(f'TASK:{recent_task}')
    else:
        print('NOTASK')
except Exception as e:
    print(f'ERROR:{e}')
" 2>/dev/null)

    # If we found a recent task that might need capturing
    if echo "$UNCAPTURED" | grep -q "^TASK:"; then
        RECENT_TASK=$(echo "$UNCAPTURED" | sed 's/^TASK://')
        echo ""
        echo "[SUCCESS DETECTED] Task: $RECENT_TASK"
        echo "Capture: python memory/brain.py success \"<task>\" \"<what worked>\""
        echo ""
    fi
fi

# =============================================================================
# FUNCTION 2: CONTEXT INJECTION - Query memory based on task risk
# =============================================================================

# =============================================================================
# FIRST-TRY SUCCESS LIKELIHOOD ASSESSMENT
# =============================================================================
#
# CRITICAL (5% first-try success) - Full memory + blueprint + gotchas + brain
#   These RARELY work first try. Hours of debugging when they fail.
#   - Production deployments, migrations, terraform destroy
#   - Database schema changes, auth system changes
#   - DNS/SSL/cert changes, IAM permissions
#
# HIGH-RISK (30% first-try success) - Full memory + relevant blueprints
#   Often need multiple attempts. Gotchas are common.
#   - Non-prod deployments, terraform plan/apply
#   - Docker networking, ECS service updates
#   - WebRTC/LiveKit configuration
#
# MODERATE (60% first-try success) - Memory + quick gotcha check
#   Usually work but gotchas exist. Quick verification helps.
#   - Docker compose changes, config updates
#   - API endpoint changes, toggle features
#   - Health check modifications
#
# LOW (90% first-try success) - Quick memory check only
#   Almost always work first try. Minimal context needed.
#   - UI changes, styling, text updates
#   - Simple additions, button labels
#   - Documentation updates

# CRITICAL: 5% first-try success (almost always fails first try)
CRITICAL_RISK="destroy|migration.*prod|schema.*change|auth.*system|delete.*prod|force.*push|rollback|permission.*change|iam.*policy|ssl.*cert|dns.*record|database.*alter|drop.*table"

# HIGH: 30% first-try success (often fails)
HIGH_RISK="deploy|terraform|migration|refactor|ecs.*service|lambda.*deploy|database|nginx.*config|cloudflare|subnet|security.*group|load.*balancer|asg|autoscaling"

# MODERATE: 60% first-try success (sometimes fails)
MODERATE_RISK="docker|config|env|toggle|health|sync|api|endpoint|websocket|livekit|bedrock|gpu|stt|tts|llm|voice|webrtc"

# LOW: 90% first-try success (rarely fails)
LOW_RISK="admin|dashboard|display|show|add.*button|style|color|text|label|readme|docs|comment|log"

# Determine risk level based on inverse of first-try success likelihood
RISK_LEVEL="none"
FIRST_TRY_LIKELIHOOD="unknown"

if echo "$PROMPT" | grep -qiE "($CRITICAL_RISK)"; then
    RISK_LEVEL="critical"
    FIRST_TRY_LIKELIHOOD="5%"
elif echo "$PROMPT" | grep -qiE "($HIGH_RISK)"; then
    RISK_LEVEL="high"
    FIRST_TRY_LIKELIHOOD="30%"
elif echo "$PROMPT" | grep -qiE "($MODERATE_RISK)"; then
    RISK_LEVEL="moderate"
    FIRST_TRY_LIKELIHOOD="60%"
elif echo "$PROMPT" | grep -qiE "($LOW_RISK)"; then
    RISK_LEVEL="low"
    FIRST_TRY_LIKELIHOOD="90%"
fi

# Default to "low" if no risk keywords matched (still inject minimal context)
# This ensures every prompt gets SOME context injection
if [ "$RISK_LEVEL" = "none" ]; then
    RISK_LEVEL="low"
    FIRST_TRY_LIKELIHOOD="90%"
fi

# Update hook firing record with risk level (non-blocking)
"$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.hook_evolution import get_hook_evolution_engine
    engine = get_hook_evolution_engine()
    # Update the most recent firing record for this session with risk level
    engine.db.execute('''
        UPDATE hook_firings SET risk_level = ?
        WHERE variant_id = ? AND session_id = ? AND risk_level = ''
        ORDER BY fired_at DESC LIMIT 1
    ''', ('$RISK_LEVEL', '$HOOK_VARIANT_ID', '$SESSION_ID'))
    engine.db.commit()
except:
    pass
" 2>/dev/null &

# =============================================================================
# CONFIGURE SEARCH DEPTH BASED ON RISK
# =============================================================================

case "$RISK_LEVEL" in
    "critical")
        QUERY_TEXT=$(echo "$PROMPT" | head -c 200 | tr '\n' ' ')
        RESULT_LIMIT=60
        SEARCH_DEPTH="EXHAUSTIVE"
        GET_BLUEPRINT="yes"
        GET_BRAIN_STATE="yes"
        GET_GOTCHAS="yes"
        ;;
    "high")
        QUERY_TEXT=$(echo "$PROMPT" | head -c 150 | tr '\n' ' ')
        RESULT_LIMIT=40
        SEARCH_DEPTH="DEEP"
        GET_BLUEPRINT="yes"
        GET_BRAIN_STATE="no"
        GET_GOTCHAS="yes"
        ;;
    "moderate")
        QUERY_TEXT=$(echo "$PROMPT" | head -c 100 | tr '\n' ' ')
        RESULT_LIMIT=25
        SEARCH_DEPTH="STANDARD"
        GET_BLUEPRINT="no"
        GET_BRAIN_STATE="no"
        GET_GOTCHAS="yes"
        ;;
    "low")
        QUERY_TEXT=$(echo "$PROMPT" | head -c 60 | tr '\n' ' ')
        RESULT_LIMIT=10
        SEARCH_DEPTH="QUICK"
        GET_BLUEPRINT="no"
        GET_BRAIN_STATE="no"
        GET_GOTCHAS="no"
        ;;
esac

# =============================================================================
# OUTPUT MEMORY CONTEXT
# =============================================================================
# Mode determines structure:
#   - layered: Traditional 6-layer system (below)
#   - hybrid/greedy/minimal: Use persistent_hook_structure.py (unified)

if [ "$INJECTION_MODE" != "layered" ]; then
    # =============================================================================
    # UNIFIED STRUCTURE MODE (hybrid/greedy/minimal)
    # =============================================================================
    # Uses helper agent webhook for consistent injection and dashboard tracking.
    # The helper agent is the single source of truth for context injection.

    # Track start time so we can report total_ms to webhook health (RACE N2 producer).
    # CLAUDE.md invariant: both layered AND hybrid/greedy/minimal MUST publish a
    # completion event so /health.webhook.events_recorded advances.
    UNIFIED_START_MS=$(($(date +%s%N 2>/dev/null || echo "$(date +%s)000000000") / 1000000))

    # URL-encode the query text for the webhook (limit to 500 chars)
    ENCODED_QUERY=$(printf '%s' "${QUERY_TEXT:0:500}" | "$PYTHON" -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read()))")

    # Call the helper agent webhook and extract formatted output
    # --connect-timeout 3: fail fast if agent_service is down
    # --max-time 12: Pre-compute architecture means no LLM blocking in webhook path
    #   S2/S8 served from anticipation cache or fast fallback (~0ms)
    #   Data sections (S1,S3,S4,S6,S7,S10) run in parallel (~5-10s max)
    UNIFIED_OUTPUT=$(curl -s --connect-timeout 3 --max-time 12 -X POST "http://localhost:8080/consult/unified?prompt=$ENCODED_QUERY&mode=$INJECTION_MODE" 2>/dev/null | \
        "$PYTHON" -c "import sys, json; r=json.load(sys.stdin); print(r.get('formatted', ''))" 2>/dev/null) || true

    # Fallback to local Python if webhook fails (time-boxed via background + wait)
    if [ -z "$UNIFIED_OUTPUT" ]; then
        TMPOUT=$(mktemp)
        "$PYTHON" "$PERSISTENT_HOOK_SCRIPT" "$QUERY_TEXT" "$INJECTION_MODE" > "$TMPOUT" 2>/dev/null &
        BGPID=$!
        ( sleep 15 && kill "$BGPID" 2>/dev/null ) &
        WATCHDOG=$!
        wait "$BGPID" 2>/dev/null
        kill "$WATCHDOG" 2>/dev/null
        UNIFIED_OUTPUT=$(cat "$TMPOUT") || true
        rm -f "$TMPOUT"
    fi

    if [ -n "$UNIFIED_OUTPUT" ]; then
        echo ""
        echo "$UNIFIED_OUTPUT"

        # Success capture reminder (concise)
        echo ""
        echo "[Success? Run: python memory/brain.py success \"<task>\" \"<insight>\"]"

        # =============================================================================
        # WEBHOOK HEALTH PUBLISH (hybrid/greedy/minimal branch — RACE N2 producer)
        # =============================================================================
        # CLAUDE.md invariant: both layered AND hybrid/greedy/minimal branches MUST
        # publish a completion event so /health.webhook.events_recorded advances.
        # Without this, Atlas is blind when running in non-layered injection mode.
        # Backgrounded + disowned so the prompt path never blocks. Errors → ZSF.
        UNIFIED_END_MS=$(($(date +%s%N 2>/dev/null || echo "$(date +%s)000000000") / 1000000))
        UNIFIED_TOTAL_MS=$((UNIFIED_END_MS - UNIFIED_START_MS))
        if [ -z "$UNIFIED_TOTAL_MS" ] || [ "$UNIFIED_TOTAL_MS" -lt 0 ]; then
            UNIFIED_TOTAL_MS=1
        fi
        (
            cd "$REPO_DIR" && PYTHONPATH="$REPO_DIR" "$PYTHON" -m memory.webhook_health_publisher publish \
                --total-ms "$UNIFIED_TOTAL_MS" \
                --section "${INJECTION_MODE}:${UNIFIED_TOTAL_MS}:ok" \
                --section "risk_${RISK_LEVEL}:1:ok" \
                --wait-s 1.0 \
                >/dev/null 2>>/tmp/webhook-publish.err
        ) &
        disown 2>/dev/null || true
    else
        # Fallback to layered if unified fails
        INJECTION_MODE="layered"
    fi
fi

# Only run layered mode if not using unified
if [ "$INJECTION_MODE" = "layered" ]; then

# Track wall-clock time for the layered block so we can publish a real
# total_ms to the webhook health daemon (RACE N2 producer side).
# A1 audit (.fleet/audits/2026-05-04-A1-webhook-dead-air.md) found the
# layered branch never published, leaving events_recorded stuck at 0.
LAYERED_START_MS=$(($(date +%s%N 2>/dev/null || echo "$(date +%s)000000000") / 1000000))

echo ""
echo "[Context DNA] Risk:$RISK_LEVEL First-try:$FIRST_TRY_LIKELIHOOD Depth:$SEARCH_DEPTH"

# =============================================================================
# LAYER 0: CODEBASE LOCATOR - "Where to consider looking" breadcrumbs
# =============================================================================
# Provides contextual file path suggestions based on the prompt.
# These are SUGGESTIONS, not guarantees - the index may be incomplete.
# Format: View 1: dir/ → file.py → function()
#
# This helps Atlas navigate the codebase by showing likely relevant locations
# BEFORE diving into the detailed memory/wisdom layers.

LOCATION_HINTS=$("$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.codebase_locator import get_hook_output
    output = get_hook_output('''$QUERY_TEXT''')
    if output:
        print(output)
except Exception as e:
    pass  # Silent fail - breadcrumbs are optional
" 2>/dev/null)

if [ -n "$LOCATION_HINTS" ]; then
    echo ""
    echo "$LOCATION_HINTS"
fi

# =============================================================================
# LAYER 0.25: NEVER DO - Critical prohibitions (show prominently)
# =============================================================================
# These are ABSOLUTE PROHIBITIONS that must be surfaced before any other context.
# Format: 🚫 NEVER: description

NEVER_DO_WARNINGS=$("$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.codebase_locator import format_never_do
    output = format_never_do('''$QUERY_TEXT''')
    if output:
        print(output)
except Exception as e:
    pass
" 2>/dev/null)

if [ -n "$NEVER_DO_WARNINGS" ]; then
    echo ""
    echo "$NEVER_DO_WARNINGS"
fi

# =============================================================================
# LAYER 0.5: WISDOM INJECTION (C Variant - User-Learned Patterns)
# =============================================================================
# If this session was selected for the C variant (wisdom injection), display
# the dynamically generated injection based on user prompt patterns that have
# been correlated with positive outcomes.

if [ -n "$WISDOM_INJECTION" ]; then
    echo ""
    echo "$WISDOM_INJECTION"
fi

# =============================================================================
# LAYER 1: PROFESSOR GUIDANCE (guiding wisdom - how to approach)
# =============================================================================
# The Professor is like the WISDOM CENTER of the brain - it doesn't store every
# memory, but it knows HOW to approach problems, WHERE the landmines are, and
# WHAT mental models to apply. This shapes the agent's entire approach.
#
# Professor provides: THE ONE THING, LANDMINES, THE PATTERN, CONTEXT
# This is the "guiding wisdom" - read it FIRST, internalize it, THEN use memories.

# ALL moderate+ risk tasks get Professor guidance (it's the compass)
if [ "$RISK_LEVEL" = "critical" ] || [ "$RISK_LEVEL" = "high" ] || [ "$RISK_LEVEL" = "moderate" ]; then
    PROFESSOR_GUIDANCE=$("$PYTHON" "$PROFESSOR_SCRIPT" "$QUERY_TEXT" 2>/dev/null | head -80)
    if [ -n "$PROFESSOR_GUIDANCE" ]; then
        echo ""
        echo "--- PROFESSOR WISDOM ---"
        echo "$PROFESSOR_GUIDANCE"
    fi
fi

# =============================================================================
# LAYER 2: BRAIN LEARNINGS (long-term memory - specific experiences)
# =============================================================================
# The Brain is like LONG-TERM MEMORY - the specific experiences, detailed SOPs,
# and real examples that the Professor's wisdom helps you interpret correctly.
#
# The Professor tells you HOW to think about the problem.
# The Brain gives you SPECIFIC EXPERIENCES to draw from.
#
# Together: Wisdom guides → Memory supports → Agent executes excellently.

# ALL risk levels get relevant learnings (scaled by limit)
MEMORY_CONTEXT=$("$PYTHON" "$QUERY_SCRIPT" "$QUERY_TEXT" 2>/dev/null | head -$RESULT_LIMIT)
if [ -n "$MEMORY_CONTEXT" ] && ! echo "$MEMORY_CONTEXT" | grep -q "No relevant learnings"; then
    echo ""
    echo "--- LEARNINGS ---"
    echo "$MEMORY_CONTEXT"
fi

# =============================================================================
# LAYER 3: GOTCHA CHECK (warnings from experience)
# =============================================================================
# ALL moderate+ risk tasks get gotcha check - these are the hard-won lessons
# that prevent repeating expensive mistakes. Professor has strategic landmines,
# but Gotcha Check surfaces SPECIFIC warnings from the learning database.

if [ "$RISK_LEVEL" = "critical" ] || [ "$RISK_LEVEL" = "high" ] || [ "$RISK_LEVEL" = "moderate" ]; then
    GOTCHAS=$("$PYTHON" "$QUERY_SCRIPT" "gotcha warning $QUERY_TEXT" 2>/dev/null | grep -iE "(gotcha|warning|careful|avoid|don't|never|always|critical|must|required)" | head -12)
    if [ -n "$GOTCHAS" ]; then
        echo ""
        echo "--- GOTCHAS ---"
        echo "$GOTCHAS"
    fi
fi

# =============================================================================
# LAYER 4: ARCHITECTURE BLUEPRINT (for infrastructure tasks)
# =============================================================================
# Get full blueprint for critical/high risk tasks

if [ "$GET_BLUEPRINT" = "yes" ]; then
    BLUEPRINT=$("$PYTHON" "$CONTEXT_SCRIPT" "$QUERY_TEXT" 2>/dev/null | head -40)
    if [ -n "$BLUEPRINT" ] && ! echo "$BLUEPRINT" | grep -q "No task or file"; then
        echo ""
        echo "━━━ ARCHITECTURE BLUEPRINT ━━━"
        echo "$BLUEPRINT"
    fi
fi

# =============================================================================
# LAYER 5: BRAIN STATE (active patterns from recent work)
# =============================================================================
# For critical tasks, show what the brain has been learning recently

if [ "$GET_BRAIN_STATE" = "yes" ]; then
    BRAIN_STATE=$("$PYTHON" "$BRAIN_SCRIPT" context "$QUERY_TEXT" 2>/dev/null | head -25)
    if [ -n "$BRAIN_STATE" ] && [ "$BRAIN_STATE" != "No relevant context found." ]; then
        echo ""
        echo "━━━ BRAIN STATE (recent patterns) ━━━"
        echo "$BRAIN_STATE"
    fi
fi

# 6. Protocol reminder (concise — full protocol is in CLAUDE.md)
if [ "$RISK_LEVEL" = "critical" ] || [ "$RISK_LEVEL" = "high" ]; then
    echo ""
    echo "--- PROTOCOL: Read all context. Verify prerequisites. Plan before executing. ---"
fi

# =============================================================================
# FUNCTION 3: OBJECTIVE SUCCESS REVIEW - Check for uncaptured wins
# =============================================================================
# At the END of every context injection, check if there are objective successes
# that haven't been captured yet. This catches:
# - System confirmations (exit 0, healthy, 200 OK) that happened without user saying "worked"
# - Architecture changes to the memory system itself
# - Any wins that slipped through

UNCAPTURED_SUCCESSES=$("$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.objective_success import ObjectiveSuccessDetector
    from memory.architecture_enhancer import work_log

    # Get recent entries (last 4 hours, unprocessed)
    entries = work_log.get_recent_entries(hours=4, include_processed=False)

    if not entries:
        sys.exit(0)

    # Run objective success detection
    detector = ObjectiveSuccessDetector()
    detector.analyze_entries(entries)  # Populates pending_successes

    # Get system-confirmed wins (don't need user confirmation)
    # These are IRREFUTABLE - things like 'Recorded SOP:', '5/5 systems active'
    system_wins = detector.get_objective_successes_without_user(min_confidence=0.7)

    if system_wins:
        print('FOUND')
        for s in system_wins[:3]:  # Show top 3
            evidence = ', '.join(s.evidence)
            print(f'WIN:[{s.confidence:.0%}] {s.task[:80]}|{evidence}')
    else:
        print('NONE')

except Exception as e:
    print(f'ERROR:{e}')
" 2>/dev/null)

# If we found uncaptured objective successes
if echo "$UNCAPTURED_SUCCESSES" | grep -q "^FOUND"; then
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════╗"
    echo "║  📊 UNCAPTURED OBJECTIVE SUCCESSES DETECTED                          ║"
    echo "╚══════════════════════════════════════════════════════════════════════╝"
    echo ""
    echo "The following tasks show OBJECTIVE evidence of success (system signals)"
    echo "but haven't been captured to the Architecture Brain yet:"
    echo ""
    echo "$UNCAPTURED_SUCCESSES" | grep "^WIN:" | while read -r line; do
        TASK=$(echo "$line" | sed 's/^WIN:\[//' | sed 's/\].*//')
        DESC=$(echo "$line" | sed 's/^WIN:\[[^]]*\] //' | sed 's/|.*//')
        EVIDENCE=$(echo "$line" | sed 's/.*|//')
        echo "  [$TASK confidence] $DESC"
        echo "       Evidence: $EVIDENCE"
        echo ""
    done
    echo "━━━ ACTION: Capture these wins ━━━"
    echo "  from memory.brain import win"
    echo "  win('<task>', '<what worked>', area='<area>')"
    echo ""
fi

# =============================================================================
# FUNCTION 5: PERIODIC ENHANCEMENT SUGGESTIONS
# =============================================================================
# Every ~20 queries, show enhancement suggestions (patterns that could become SOPs)
# Uses a counter file to track queries

QUERY_COUNTER_FILE="$CONTEXT_DNA_DIR/.query_counter"
ENHANCEMENT_INTERVAL=20

# Read/increment counter
QUERY_COUNT=0
if [ -f "$QUERY_COUNTER_FILE" ]; then
    QUERY_COUNT=$(cat "$QUERY_COUNTER_FILE" 2>/dev/null || echo "0")
fi
QUERY_COUNT=$((QUERY_COUNT + 1))
echo "$QUERY_COUNT" > "$QUERY_COUNTER_FILE"

# Show enhancement suggestions every N queries
if [ $((QUERY_COUNT % ENHANCEMENT_INTERVAL)) -eq 0 ]; then
    ENHANCEMENTS=$("$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
try:
    from memory.auto_enhance import AutoEnhancer
    enhancer = AutoEnhancer()
    suggestions = enhancer.analyze_for_enhancements()

    if suggestions:
        high_priority = [s for s in suggestions if s.priority == 'high']
        if high_priority:
            print('FOUND')
            for s in high_priority[:3]:
                print(f'SUGGEST:[{s.suggestion_type}] {s.title}')
                print(f'ACTION: {s.action}')
        else:
            print('NONE')
    else:
        print('NONE')
except Exception as e:
    print(f'ERROR:{e}')
" 2>/dev/null)

    if echo "$ENHANCEMENTS" | grep -q "^FOUND"; then
        echo ""
        echo "╔══════════════════════════════════════════════════════════════════════╗"
        echo "║  💡 ENHANCEMENT SUGGESTIONS (periodic review)                         ║"
        echo "╚══════════════════════════════════════════════════════════════════════╝"
        echo ""
        echo "The system has detected patterns that could improve the knowledge base:"
        echo ""
        echo "$ENHANCEMENTS" | grep -E "^(SUGGEST|ACTION):" | while read -r line; do
            if echo "$line" | grep -q "^SUGGEST:"; then
                echo "  • $(echo "$line" | sed 's/^SUGGEST://')"
            else
                echo "    → $(echo "$line" | sed 's/^ACTION://')"
            fi
        done
        echo ""
        echo "Run: python memory/auto_enhance.py analyze  (for full details)"
        echo ""
    fi
fi

# =============================================================================
# WEBHOOK HEALTH PUBLISH (layered branch — RACE N2 producer)
# =============================================================================
# Fixes A1 audit "Webhook Dead-Air" (.fleet/audits/2026-05-04-A1-webhook-dead-air.md):
# layered branch never reached generate_context_injection's publish hook, so
# events_recorded stayed at 0 forever and Atlas was blind to the webhook
# pipeline. We dispatch a synthetic completion event via the CLI publisher
# A1 added to memory.webhook_health_publisher.
#
# Backgrounded with `&` + `disown` so the prompt path never blocks. Errors
# are captured to /tmp/webhook-publish.err (ZSF — never silently swallowed)
# and the publisher itself increments webhook_publish_errors on transport
# failure (also observable on /health).
LAYERED_END_MS=$(($(date +%s%N 2>/dev/null || echo "$(date +%s)000000000") / 1000000))
LAYERED_TOTAL_MS=$((LAYERED_END_MS - LAYERED_START_MS))
# Guard against missing/zero start time so --total-ms always parses as int.
if [ -z "$LAYERED_TOTAL_MS" ] || [ "$LAYERED_TOTAL_MS" -lt 0 ]; then
    LAYERED_TOTAL_MS=1
fi
(
    cd "$REPO_DIR" && PYTHONPATH="$REPO_DIR" "$PYTHON" -m memory.webhook_health_publisher publish \
        --total-ms "$LAYERED_TOTAL_MS" \
        --section "layered:${LAYERED_TOTAL_MS}:ok" \
        --section "risk_${RISK_LEVEL}:1:ok" \
        --wait-s 1.0 \
        >/dev/null 2>>/tmp/webhook-publish.err
) &
disown 2>/dev/null || true

fi  # End of layered mode conditional

exit 0
