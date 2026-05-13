#!/usr/bin/env bash
# continuity-bridge.sh — preview or relay a non-destructive Claude/Codex handoff

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON="${REPO_DIR}/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

MODE="${1:-preview}"
TARGET="${2:-codex}"
REASON="${CONTINUITY_REASON:-anthropic-limit-failover}"
TARGET_MODEL="${CONTINUITY_TARGET_MODEL:-codex-gpt5.4}"

case "$MODE" in
  preview)
    "$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
sys.path.insert(0, '$REPO_DIR/multi-fleet')
from multifleet.continuity_bridge import ContinuityBridge
bridge = ContinuityBridge(repo_root='$REPO_DIR')
bundle = bridge.build_bundle(reason='$REASON', target_model='$TARGET_MODEL')
print(bridge.render_markdown(bundle, to='$TARGET'))
"
    ;;
  relay)
    "$PYTHON" -c "
import json, sys
sys.path.insert(0, '$REPO_DIR')
sys.path.insert(0, '$REPO_DIR/multi-fleet')
from multifleet.continuity_bridge import ContinuityBridge
bridge = ContinuityBridge(repo_root='$REPO_DIR')
bundle = bridge.build_bundle(reason='$REASON', target_model='$TARGET_MODEL')
result = bridge.relay_bundle(bundle, '$TARGET')
print(json.dumps(result, indent=2))
"
    ;;
  write)
    "$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
sys.path.insert(0, '$REPO_DIR/multi-fleet')
from multifleet.continuity_bridge import ContinuityBridge
bridge = ContinuityBridge(repo_root='$REPO_DIR')
bundle = bridge.build_bundle(reason='$REASON', target_model='$TARGET_MODEL')
path = bridge.write_git_fallback(
    '$TARGET',
    bridge.render_markdown(bundle, to='$TARGET'),
    subject='Continuity handoff: $REASON',
)
print(path)
"
    ;;
  announce)
    "$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
sys.path.insert(0, '$REPO_DIR/multi-fleet')
from multifleet.continuity_bridge import ContinuityBridge
bridge = ContinuityBridge(repo_root='$REPO_DIR')
bundle = bridge.build_bundle(reason='$REASON', target_model='$TARGET_MODEL', compact=True)
path = bridge.write_git_fallback(
    '$TARGET',
    bridge.render_alignment_broadcast(bundle, to='$TARGET'),
    subject='Continuity alignment rollout',
)
print(path)
"
    ;;
  *)
    echo "Usage: bash scripts/continuity-bridge.sh {preview|relay|write|announce} [target]"
    echo "Examples:"
    echo "  bash scripts/continuity-bridge.sh preview codex"
    echo "  bash scripts/continuity-bridge.sh relay mac2"
    echo "  bash scripts/continuity-bridge.sh write mac3"
    echo "  bash scripts/continuity-bridge.sh announce all"
    exit 1
    ;;
esac
