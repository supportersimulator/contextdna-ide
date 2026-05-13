#!/usr/bin/env bash
# GhostAgent SessionStart Hook — injects invariants + last checkpoint on session start
#
# Claude Code hook: SessionStart
# Outputs JSON with additionalContext to inject into the session.
#
# From chat design: "At SessionStart, GhostAgent should read the last checkpoint
# and inject only four things: current branch/goal, invariants, last good stopping
# point, and downgrade/failover notes."

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GHOST_DIR="$HOME/.claude/ghost"
CHECKPOINT_DIR="$HOME/.fleet-nerve/ghost/checkpoints"
OMNIROUTE_STATE="/tmp/omniroute-state.json"
POWER_STATE="/tmp/fleet-power-state.json"

# Read the hook input from stdin
INPUT=$(cat)

# Determine session reason (startup/resume/clear/compact)
SESSION_REASON=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('session_reason', d.get('type', 'startup')))
except: print('startup')
" 2>/dev/null || echo "startup")

# ── Gather context ──

CONTEXT_PARTS=()

# 1. Current branch/goal
BRANCH=$(cd "$REPO_ROOT" && git branch --show-current 2>/dev/null || echo "unknown")
LAST_COMMIT=$(cd "$REPO_ROOT" && git log -1 --oneline 2>/dev/null || echo "none")
CONTEXT_PARTS+=("[Branch] $BRANCH | Last commit: $LAST_COMMIT")

# 2. Invariants (from checkpoint file or fallback)
if [ -f "$GHOST_DIR/invariants.md" ]; then
    INVARIANTS=$(head -10 "$GHOST_DIR/invariants.md" 2>/dev/null || echo "")
    CONTEXT_PARTS+=("[Invariants] $INVARIANTS")
fi

# 3. Last checkpoint (stopping point)
LATEST_CP=""
if [ -d "$CHECKPOINT_DIR" ]; then
    LATEST_CP=$(ls -t "$CHECKPOINT_DIR"/*.json 2>/dev/null | head -1)
fi
if [ -n "$LATEST_CP" ] && [ -f "$LATEST_CP" ]; then
    CP_INFO=$(python3 -c "
import json
d = json.loads(open('$LATEST_CP').read())
parts = []
if d.get('reason'): parts.append(f\"reason={d['reason']}\")
if d.get('branch'): parts.append(f\"branch={d['branch']}\")
if d.get('workflow_phase'): parts.append(f\"phase={d['workflow_phase']}\")
if d.get('last_synthesis_task'): parts.append(f\"task={d['last_synthesis_task'][:80]}\")
ts = d.get('timestamp', '')
if ts: parts.append(f\"at={ts}\")
print(' | '.join(parts))
" 2>/dev/null || echo "checkpoint found but unreadable")
    CONTEXT_PARTS+=("[Last Checkpoint] $CP_INFO")
fi

# Also check model-swap checkpoint
if [ -f "/tmp/model-swap-checkpoint.json" ]; then
    SWAP_INFO=$(python3 -c "
import json
d = json.loads(open('/tmp/model-swap-checkpoint.json').read())
src = d.get('source_model', '?')
tgt = d.get('target_model', '?')
task = d.get('active_task', '')[:80]
print(f'Last swap: {src} -> {tgt}' + (f' | task: {task}' if task else ''))
" 2>/dev/null || echo "")
    if [ -n "$SWAP_INFO" ]; then
        CONTEXT_PARTS+=("[Model Swap] $SWAP_INFO")
    fi
fi

# 4. Failover/downgrade warnings
if [ -f "$OMNIROUTE_STATE" ]; then
    FAILOVER_INFO=$(python3 -c "
import json
d = json.loads(open('$OMNIROUTE_STATE').read())
tier = d.get('active_tier', 1)
if tier > 1:
    providers = d.get('providers', {})
    active = next((p for p in providers.values() if p.get('tier') == tier), {})
    name = active.get('name', 'unknown')
    print(f'WARNING: Running on fallback provider: {name} (tier {tier}/5)')
else:
    print('')
" 2>/dev/null || echo "")
    if [ -n "$FAILOVER_INFO" ]; then
        CONTEXT_PARTS+=("[Failover] $FAILOVER_INFO")
    fi
fi

# 5. Power state
if [ -f "$POWER_STATE" ]; then
    POWER_INFO=$(python3 -c "
import json
d = json.loads(open('$POWER_STATE').read())
state = d.get('state', 'unknown')
caff = d.get('caffeinate_active', False)
print(f'Power: {state}' + (' (keepawake active)' if caff else ''))
" 2>/dev/null || echo "")
    if [ -n "$POWER_INFO" ]; then
        CONTEXT_PARTS+=("[Power] $POWER_INFO")
    fi
fi

# 6. Fleet inbox count
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
INBOX_DIR="$REPO_ROOT/.fleet-messages/$NODE_ID"
if [ -d "$INBOX_DIR" ]; then
    MSG_COUNT=$(ls "$INBOX_DIR"/*.md 2>/dev/null | wc -l | tr -d ' ')
    if [ "$MSG_COUNT" -gt 0 ]; then
        CONTEXT_PARTS+=("[Fleet Inbox] $MSG_COUNT unread message(s)")
    fi
fi

# ── Assemble and output ──

# Join all context parts
ADDITIONAL_CONTEXT=""
for part in "${CONTEXT_PARTS[@]}"; do
    ADDITIONAL_CONTEXT="${ADDITIONAL_CONTEXT}${part}\n"
done

# Transition power state to session-hot
if [ -f "$REPO_ROOT/multi-fleet/multifleet/power_state.py" ]; then
    cd "$REPO_ROOT" && python3 -c "
import sys; sys.path.insert(0, 'multi-fleet')
from multifleet.power_state import PowerStateManager, PowerState
mgr = PowerStateManager(repo_root='.')
mgr.transition(PowerState.SESSION_HOT, reason='session-start-hook')
" 2>/dev/null || true
fi

# Output JSON for Claude Code hook system
# additionalContext is injected into the session context
if [ -n "$ADDITIONAL_CONTEXT" ]; then
    python3 -c "
import json
ctx = '''$(echo -e "$ADDITIONAL_CONTEXT")'''
print(json.dumps({
    'additionalContext': '[GhostAgent] Session context:\\n' + ctx.strip()
}))
"
else
    echo '{"additionalContext": ""}'
fi
