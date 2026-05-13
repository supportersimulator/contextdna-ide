#!/usr/bin/env bash
# GhostAgent Stop Hook — captures checkpoint and transitions power state on session end
#
# Claude Code hook: Stop
# From chat design: "At Stop and SubagentStop, GhostAgent should refuse 'done'
# states unless checkpoint was updated and task ledger reflects current state."

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# Read hook input
INPUT=$(cat)

# 1. Capture ghost checkpoint
if [ -f "$REPO_ROOT/scripts/ghost-checkpoint.sh" ]; then
    bash "$REPO_ROOT/scripts/ghost-checkpoint.sh" 2>/dev/null || true
fi

# Also capture model-swap checkpoint (more detailed)
cd "$REPO_ROOT" && python3 -c "
import sys
sys.path.insert(0, 'multi-fleet')
try:
    from multifleet.model_swap import ModelSwapCheckpoint
    swapper = ModelSwapCheckpoint(repo_root='.')
    cp = swapper.capture(reason='session-stop')
except Exception as e:
    pass  # Non-critical
" 2>/dev/null || true

# 2. Transition power state to cooldown
cd "$REPO_ROOT" && python3 -c "
import sys
sys.path.insert(0, 'multi-fleet')
try:
    from multifleet.power_state import PowerStateManager, PowerState
    mgr = PowerStateManager(repo_root='.')
    mgr.transition(PowerState.COOLDOWN, reason='session-stop-hook')
except Exception:
    pass
" 2>/dev/null || true

# 3. Output (hooks at Stop don't inject context, but we log)
echo '{"additionalContext": ""}'
