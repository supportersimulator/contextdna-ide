#!/usr/bin/env bash
# pre-task.sh — Professor wisdom + fleet health before any task
# Usage: bash scripts/pre-task.sh "describe what you're about to do"
set -euo pipefail

TASK="${1:-}"
if [[ -z "$TASK" ]]; then
  echo "Usage: bash scripts/pre-task.sh \"task description\"" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== PROFESSOR CHECK ==="
PYTHONPATH=. .venv/bin/python3 memory/professor.py "$TASK" 2>/dev/null || echo "[professor unavailable]"

echo ""
echo "=== FLEET HEALTH ==="
if curl -sf --max-time 2 http://127.0.0.1:8855/health > /tmp/fleet-health-$$.json 2>/dev/null; then
  STATUS=$(python3 -c "import json,sys; d=json.load(open('/tmp/fleet-health-$$.json')); print('UP —', d.get('status','?'))" 2>/dev/null || echo "UP")
  echo "Daemon: $STATUS"
  rm -f /tmp/fleet-health-$$.json
else
  echo "Daemon: DOWN (start with: MULTIFLEET_NODE_ID=mac2 python3 tools/fleet_nerve_nats.py serve)"
fi
