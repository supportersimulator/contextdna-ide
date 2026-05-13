#!/bin/bash
# GhostAgent Checkpoint — preserves project state before model failover
# Triggered by hooks: PreToolUse (risky edits), Stop, SubagentStop

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s)}"
CHECKPOINT_DIR="$HOME/.claude/ghost"
mkdir -p "$CHECKPOINT_DIR"

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# 1. Save current branch + git state
BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
DIRTY=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')

# 2. Save checkpoint
cat > "$CHECKPOINT_DIR/checkpoint.json" << CPEOF
{
  "node_id": "$NODE_ID",
  "timestamp": "$TIMESTAMP",
  "branch": "$BRANCH",
  "dirty_files": $DIRTY,
  "working_dir": "$(pwd)",
  "session_id": "${CLAUDE_SESSION_ID:-unknown}",
  "model": "${ANTHROPIC_MODEL:-claude}",
  "note": "Auto-checkpoint before potential model switch"
}
CPEOF

# 3. Save invariants reminder
cat > "$CHECKPOINT_DIR/invariants.md" << INVEOF
# Project Invariants (auto-generated)
- Context DNA IDE is THE focus (ER Simulator AFTER)
- 13-session path to revenue: 6/13 complete
- Zero silent failures invariant
- 3-surgeon consultation on arch decisions
- Integration over features (moratorium active)
- Channel self-healing via invariance rules
INVEOF

echo "[ghost] Checkpoint saved: $CHECKPOINT_DIR/checkpoint.json"
