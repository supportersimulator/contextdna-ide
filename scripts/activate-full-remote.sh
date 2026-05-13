#!/usr/bin/env bash
# activate-full-remote.sh — COMPREHENSIVE remote fleet activation
#
# This is the "everything" script. Calls trip-mode.sh then adds:
#   - Model switching setup verification
#   - Fleet coordination messages
#   - Ghost checkpoint for continuity
#   - All-node activation (optional)
#
# Usage:
#   bash scripts/activate-full-remote.sh [hours]         # This node only
#   bash scripts/activate-full-remote.sh [hours] --all   # All fleet nodes

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
HOURS="${1:-72}"
ALL_NODES=false
[ "${2:-}" = "--all" ] && ALL_NODES=true

echo ""
echo "================================================================"
echo "     FULL REMOTE FLEET ACTIVATION"
echo "     Node: $NODE_ID | Duration: ${HOURS}h | All nodes: $ALL_NODES"
echo "================================================================"
echo ""

# ── 1. Run trip-mode for core activation ──
echo "=== Phase 1: Core Trip Mode ==="
bash "$REPO_ROOT/scripts/trip-mode.sh" "$HOURS"

# ── 2. Model switching setup verification ──
echo ""
echo "=== Phase 2: Model Switching ==="
if [ -f "$REPO_ROOT/scripts/model-switch-setup.sh" ]; then
    bash "$REPO_ROOT/scripts/model-switch-setup.sh" status
else
    echo "  model-switch-setup.sh not found"
fi

# Check environment overrides
echo ""
echo "  --- Environment ---"
if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
    echo "  ANTHROPIC_BASE_URL: $ANTHROPIC_BASE_URL (routed)"
else
    echo "  ANTHROPIC_BASE_URL: not set (direct to Anthropic)"
fi
if [ -n "${CLAUDE_CODE_DEFAULT_MODEL:-}" ]; then
    echo "  CLAUDE_CODE_DEFAULT_MODEL: $CLAUDE_CODE_DEFAULT_MODEL"
else
    echo "  CLAUDE_CODE_DEFAULT_MODEL: not set (using default)"
fi

# ── 3. Ghost checkpoint (from mac3) ──
echo ""
echo "=== Phase 3: Ghost Checkpoint ==="
if [ -f "$REPO_ROOT/scripts/ghost-checkpoint.sh" ]; then
    bash "$REPO_ROOT/scripts/ghost-checkpoint.sh"
else
    echo "  Ghost checkpoint not available"
fi

# ── 4. Fleet coordination message ──
echo ""
echo "=== Phase 4: Fleet Coordination ==="
FLEET_MSG_DIR="$REPO_ROOT/.fleet-messages/all"
mkdir -p "$FLEET_MSG_DIR"
MSG_FILE="$FLEET_MSG_DIR/trip-mode-$(date +%Y-%m-%d)-${NODE_ID}.md"

cat > "$MSG_FILE" << EOF
---
from: $NODE_ID
to: all
subject: Trip mode activated — ${HOURS}h remote operation
timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)
type: info
---

## Trip Mode Active on $NODE_ID

Duration: ${HOURS} hours (until $(date -v+"${HOURS}"H "+%Y-%m-%d %H:%M" 2>/dev/null || date -d "+${HOURS} hours" "+%Y-%m-%d %H:%M" 2>/dev/null || echo "~${HOURS}h from now"))

Services running:
- Keepawake: caffeinate -dims
- Fleet daemon: port 8855
- Remote: claude.ai/code + SSH
- API failover: configured

All fleet nodes should run: \`bash scripts/activate-full-remote.sh $HOURS\`
EOF

echo "  Fleet message written: $MSG_FILE"

# ── 5. All-node activation (optional) ──
if [ "$ALL_NODES" = true ]; then
    echo ""
    echo "=== Phase 5: All-Node Activation ==="
    bash "$REPO_ROOT/scripts/fleet-keepawake.sh" all-start "$HOURS"
fi

# ── Final summary ──
echo ""
echo "================================================================"
echo "  FULL REMOTE ACTIVATION COMPLETE"
echo ""
echo "  Node: $NODE_ID"
echo "  Duration: ${HOURS} hours"
echo "  Fleet message: sent to all nodes"
echo ""
echo "  Quick commands:"
echo "    Status:     bash scripts/trip-mode.sh status"
echo "    Deactivate: bash scripts/trip-mode.sh stop"
echo "    Wake peer:  bash scripts/fleet-keepawake.sh wake mac1"
echo "    Fleet msg:  bash scripts/fleet-send.sh all 'subject' 'body'"
echo "================================================================"
