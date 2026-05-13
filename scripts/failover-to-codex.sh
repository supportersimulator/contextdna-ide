#!/usr/bin/env bash
# failover-to-codex.sh — When Claude/Anthropic limits hit, hand off the run
# to a non-Anthropic model. Captures model_swap checkpoint, writes task
# context, then launches the target.
#
# ZZ5 2026-05-12: added --target=deepseek so a single Anthropic outage does
# not force OpenAI spend. Default target stays codex (OpenAI gpt-5.4) for
# backward compat. ZSF: unknown target → exit 2 with a clear message.
#
# Usage:
#   bash scripts/failover-to-codex.sh                 # default → codex (openai)
#   bash scripts/failover-to-codex.sh --target=codex
#   bash scripts/failover-to-codex.sh --target=deepseek
#   bash scripts/failover-to-codex.sh --force         # bypass active-claude check
set -euo pipefail

# Argument parsing — accept --target=<codex|deepseek> and --force in any order.
TARGET="${FAILOVER_TARGET:-codex}"
FORCE_FLAG=""
for arg in "$@"; do
    case "$arg" in
        --target=*)    TARGET="${arg#--target=}" ;;
        --force)       FORCE_FLAG="--force" ;;
        -h|--help)
            grep -E '^# ' "$0" | sed 's/^# //' | head -20
            exit 0
            ;;
        *) ;;
    esac
done
case "$TARGET" in
    codex|deepseek) ;;
    *)
        echo "[failover] unknown --target=$TARGET (expected: codex|deepseek)" >&2
        exit 2
        ;;
esac

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
FLEET_DIR="$REPO_DIR/.fleet-messages/mac3"
TASK_FILE="$FLEET_DIR/${TARGET}-failover-tasks.md"
HANDOFF_FILE="$FLEET_DIR/${TARGET}-handoff-$(date +%Y-%m-%d-%H%M).md"
CHECKPOINT="/tmp/model-swap-checkpoint.json"
VENV="$REPO_DIR/.venv/bin/python3"
TS=$(date +%Y-%m-%d-%H%M)
log() { echo "[failover/$TARGET] $(date +%H:%M:%S) $*"; }

# --- 1. Check if Claude Code is actually inactive ---
claude_active() {
    pgrep -f "claude" >/dev/null 2>&1 && return 0
    # Also check for recent activity (checkpoint < 5 min old)
    if [[ -f "$CHECKPOINT" ]]; then
        local age=$(( $(date +%s) - $(stat -f%m "$CHECKPOINT" 2>/dev/null || echo 0) ))
        [[ $age -lt 300 ]] && return 0
    fi
    return 1
}

if claude_active; then
    log "Claude Code appears active. Use --force to override."
    [[ -z "$FORCE_FLAG" ]] && exit 0
fi

# Per-target model labels (used in checkpoint metadata + handoff file).
if [[ "$TARGET" == "deepseek" ]]; then
    TARGET_MODEL="deepseek-chat"
    FAILOVER_REASON="anthropic-limit-failover-to-deepseek"
else
    TARGET_MODEL="codex-gpt5.4"
    FAILOVER_REASON="anthropic-limit-failover"
fi

# --- 2. Capture model_swap checkpoint ---
log "Capturing model_swap checkpoint..."
cd "$REPO_DIR"
$VENV -c "
import sys; sys.path.insert(0, '.')
sys.path.insert(0, 'multi-fleet')
from multifleet.model_swap import ModelSwapCheckpoint
ms = ModelSwapCheckpoint('$REPO_DIR')
cp = ms.capture(reason='$FAILOVER_REASON', source_model='claude-opus', target_model='$TARGET_MODEL')
print(f'Checkpoint: {cp.checkpoint_id} | Branch: {cp.branch} | Dirty: {len(cp.dirty_files)} files')
" 2>/dev/null || log "WARN: checkpoint capture failed (non-fatal)"

# --- 3. Build task file from plans + git status ---
log "Building task file..."
mkdir -p "$FLEET_DIR"
ACTION_PLAN="$REPO_DIR/docs/plans/2026-04-16-core-alignment-action-plan.md"
INVENTORY="$REPO_DIR/docs/plans/2026-04-08-master-unbuilt-inventory.md"

{
    echo "---"
    echo "from: mac3-atlas"
    echo "to: $TARGET"
    echo "subject: Failover task list (Anthropic limit reached → $TARGET)"
    echo "timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "---"
    echo "# Codex Failover Tasks"
    echo ""
    echo "## Rules"
    echo "- Work inside \`$REPO_DIR\`"
    echo "- Commit with messages prefixed \`${TARGET}:\`"
    echo "- Do NOT push to main without review"
    echo "- Write results to \`.fleet-messages/mac3/${TARGET}-results-$TS.md\`"
    echo "- Read CLAUDE.md for project context"
    echo ""
    echo "## Current State"
    echo '```'
    git -C "$REPO_DIR" status --short 2>/dev/null | head -20
    echo '```'
    echo ""
    if [[ -f "$CHECKPOINT" ]]; then
        echo "## Active Task (from checkpoint)"
        $VENV -c "import json; d=json.load(open('$CHECKPOINT')); print(d.get('active_task','(none)'))" 2>/dev/null || echo "(unknown)"
        echo ""
    fi
    if [[ -f "$ACTION_PLAN" ]]; then
        echo "## Priority Tasks (from action plan)"
        grep -E "^#{1,3} " "$ACTION_PLAN" | head -15
        echo ""
    fi
    if [[ -f "$INVENTORY" ]]; then
        echo "## Unbuilt Inventory (top items)"
        grep -E "^#{1,3} |^\- \[" "$INVENTORY" | head -15
    fi
} > "$TASK_FILE"

# Append structured continuity capsule from session historian + model swap
log "Appending continuity bridge context..."
$VENV -c "
import sys
sys.path.insert(0, '$REPO_DIR')
sys.path.insert(0, '$REPO_DIR/multi-fleet')
from multifleet.continuity_bridge import ContinuityBridge

bridge = ContinuityBridge(repo_root='$REPO_DIR', node_id='mac3')
bundle = bridge.build_bundle(
    reason='$FAILOVER_REASON',
    source_model='claude-opus',
    target_model='$TARGET_MODEL',
    compact=False,
)
print()
print(bridge.render_markdown(bundle, to='$TARGET', subject='Continuity handoff: anthropic limit reached'))
" >> "$TASK_FILE" 2>/dev/null || log "WARN: continuity bridge context failed (non-fatal)"

log "Task file: $TASK_FILE"

# --- 4. Launch target ---
LAUNCH_LOG="/tmp/${TARGET}-failover-$TS.log"
RESULTS_FILE=".fleet-messages/mac3/${TARGET}-results-$TS.md"
if [[ "$TARGET" == "codex" ]]; then
    log "Launching Codex..."
    codex --prompt "Read $TASK_FILE for tasks. Read CLAUDE.md for conventions. Write results to $RESULTS_FILE" \
        2>&1 | tee "$LAUNCH_LOG"
elif [[ "$TARGET" == "deepseek" ]]; then
    # DeepSeek path — no interactive CLI ships with the API. We dispatch a
    # single batch run via the priority queue (P3 EXTERNAL) so the handoff
    # produces results without spawning an interactive session. ZSF: errors
    # land in $LAUNCH_LOG; the post-step writes a handoff file regardless.
    log "Dispatching DeepSeek batch run..."
    cd "$REPO_DIR"
    PYTHONPATH=. "$VENV" -c "
import sys, time, traceback
sys.path.insert(0, '.')
from memory.llm_priority_queue import llm_generate, Priority

prompt_path = '$TASK_FILE'
try:
    body = open(prompt_path).read()
except Exception as e:
    print(f'ERROR: failed to read $TASK_FILE: {e}')
    raise SystemExit(1)

t0 = time.time()
try:
    out = llm_generate(
        system_prompt='You are a DeepSeek failover worker. Produce a results report.',
        user_prompt=body,
        priority=Priority.EXTERNAL,
        profile='deep',
        caller='failover-to-deepseek',
    )
except Exception:
    traceback.print_exc()
    raise SystemExit(2)
ms = int((time.time() - t0) * 1000)
print(f'[deepseek] {ms}ms')
import os
os.makedirs(os.path.dirname('$RESULTS_FILE'), exist_ok=True)
open('$RESULTS_FILE', 'w').write(out or '(empty)')
print('Results: $RESULTS_FILE')
" 2>&1 | tee "$LAUNCH_LOG" || log "WARN: deepseek dispatch failed (see $LAUNCH_LOG)"
fi

# --- 5. Post-target handoff ---
log "Writing handoff..."
{
    echo "---"
    echo "from: $TARGET"
    echo "to: mac3-atlas"
    echo "subject: $TARGET failover complete"
    echo "timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "---"
    echo "# ${TARGET} Failover Complete"
    echo "Log: $LAUNCH_LOG"
    echo "Checkpoint: $CHECKPOINT"
    echo "## Recent Commits"
    git -C "$REPO_DIR" log --oneline -5 2>/dev/null
    echo "## Modified Files"
    git -C "$REPO_DIR" status --short 2>/dev/null | head -20
} > "$HANDOFF_FILE"

log "Handoff: $HANDOFF_FILE — restore via model_swap.py"
