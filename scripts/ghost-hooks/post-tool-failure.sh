#!/usr/bin/env bash
# GhostAgent PostToolUseFailure Hook — classifies failures and injects corrective context
#
# Claude Code hook: PostToolUseFailure
# Detects: rate-limit errors, provider failures, network issues, model exhaustion
# Triggers OmniRoute failover when appropriate
#
# From chat design: "At PostToolUseFailure, GhostAgent should classify the failure
# as: local code failure, provider/rate-limit failure, network/peer failure, or
# human interruption."

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# Read hook input from stdin
INPUT=$(cat)

# Extract error details
ERROR_INFO=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    tool = d.get('tool_name', d.get('tool', 'unknown'))
    error = d.get('error', d.get('stderr', d.get('output', '')))
    # Truncate to avoid context bloat
    if isinstance(error, str) and len(error) > 500:
        error = error[:500] + '...'
    print(json.dumps({'tool': tool, 'error': str(error)}))
except Exception as e:
    print(json.dumps({'tool': 'unknown', 'error': str(e)}))
" 2>/dev/null || echo '{"tool":"unknown","error":"parse failed"}')

TOOL=$(echo "$ERROR_INFO" | python3 -c "import json,sys; print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || echo "")
ERROR=$(echo "$ERROR_INFO" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error',''))" 2>/dev/null || echo "")

# ── Classify the failure ──

CLASSIFICATION="local"
CORRECTIVE_CONTEXT=""
FAILOVER_NEEDED=false

# Check for rate-limit / provider errors
if echo "$ERROR" | grep -qi "429\|rate.limit\|too.many.requests\|quota\|exceeded\|overloaded\|529"; then
    CLASSIFICATION="provider-rate-limit"
    FAILOVER_NEEDED=true

    # Trigger OmniRoute failover
    FAILOVER_RESULT=""
    if [ -f "$REPO_ROOT/multi-fleet/multifleet/omniroute.py" ]; then
        # Extract HTTP status code if present
        STATUS_CODE=$(echo "$ERROR" | grep -oE '[0-9]{3}' | head -1 || echo "429")

        FAILOVER_RESULT=$(cd "$REPO_ROOT" && python3 -c "
import sys, json
sys.path.insert(0, 'multi-fleet')
from multifleet.omniroute import OmniRouteOrchestrator
orch = OmniRouteOrchestrator(repo_root='.')
result = orch.detect_and_handle_error(
    error_code=${STATUS_CODE:-429},
    error_message='''${ERROR:0:200}''',
)
print(json.dumps(result))
" 2>/dev/null || echo '{"action":"error","details":"omniroute unavailable"}')

        ACTION=$(echo "$FAILOVER_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('action',''))" 2>/dev/null || echo "")

        if [ "$ACTION" = "failover" ]; then
            TO_PROVIDER=$(echo "$FAILOVER_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('to_provider',''))" 2>/dev/null || echo "unknown")
            CORRECTIVE_CONTEXT="[GhostAgent] RATE LIMIT HIT — OmniRoute failover triggered to $TO_PROVIDER. Session state preserved via checkpoint. Continue with current task."
        elif [ "$ACTION" = "fleet_handoff" ]; then
            TARGET_NODE=$(echo "$FAILOVER_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('target_node',''))" 2>/dev/null || echo "unknown")
            CHECKPOINT=$(echo "$FAILOVER_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('checkpoint_id',''))" 2>/dev/null || echo "")
            CORRECTIVE_CONTEXT="[GhostAgent] ALL LOCAL PROVIDERS EXHAUSTED — baton handed to fleet node $TARGET_NODE. Checkpoint: $CHECKPOINT. This node should pause until rate limits reset. Other node will continue the task."
        elif [ "$ACTION" = "exhausted_no_peers" ]; then
            CORRECTIVE_CONTEXT="[GhostAgent] ALL PROVIDERS EXHAUSTED and NO fleet peers reachable. Options: (1) wait for rate limit reset, (2) try /model to switch manually, (3) start fleet daemon on another node."
        else
            CORRECTIVE_CONTEXT="[GhostAgent] Rate limit detected — retry recommended. Error: ${ERROR:0:100}"
        fi
    else
        CORRECTIVE_CONTEXT="[GhostAgent] Rate limit detected but OmniRoute not available. Consider: export ANTHROPIC_BASE_URL=http://localhost:3456 (claude-code-router with DeepSeek fallback)"
    fi

elif echo "$ERROR" | grep -qi "network\|connection\|timeout\|ECONNREFUSED\|ENOTFOUND\|DNS\|unreachable"; then
    CLASSIFICATION="network"
    CORRECTIVE_CONTEXT="[GhostAgent] Network error detected. Check: (1) internet connectivity, (2) provider endpoint reachability, (3) fleet daemon status (curl http://127.0.0.1:8855/health)"

elif echo "$ERROR" | grep -qi "context.limit\|context.window\|token.limit\|context.length"; then
    CLASSIFICATION="context-exhaustion"
    # Capture checkpoint before suggesting /compact
    if [ -f "$REPO_ROOT/scripts/ghost-checkpoint.sh" ]; then
        bash "$REPO_ROOT/scripts/ghost-checkpoint.sh" 2>/dev/null || true
    fi
    CORRECTIVE_CONTEXT="[GhostAgent] Context window limit hit. Checkpoint saved. Run /compact to free context, then continue. If using fallback model, context window may be smaller."

elif echo "$ERROR" | grep -qi "permission\|denied\|forbidden\|401\|403"; then
    CLASSIFICATION="auth"
    CORRECTIVE_CONTEXT="[GhostAgent] Authentication/permission error. Check API key configuration. Do NOT log or expose key values."

else
    # Local code/tool failure — no special handling needed
    CLASSIFICATION="local"
    CORRECTIVE_CONTEXT=""
fi

# ── Output corrective context ──

if [ -n "$CORRECTIVE_CONTEXT" ]; then
    python3 -c "
import json
print(json.dumps({
    'additionalContext': '''$CORRECTIVE_CONTEXT''',
    '_ghost_classification': '$CLASSIFICATION',
    '_ghost_failover_needed': $( [ "$FAILOVER_NEEDED" = true ] && echo "true" || echo "false" )
}))
"
else
    echo '{"additionalContext": ""}'
fi
