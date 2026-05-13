#!/usr/bin/env bash
# superset-shepherd.sh — Dispatch a prompt to a healthy Superset device.
#
# Usage:
#   superset-shepherd.sh "prompt text"
#   superset-shepherd.sh "prompt text" --node mac1
#
# Selects healthy device via superset_shepherd.get_device_health() before
# dispatching. Falls back to NATS wake-escalation if all devices stale.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PROMPT="${1:-}"
PREFER_NODE=""

shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --node) PREFER_NODE="${2:-}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$PROMPT" ]]; then
    echo "usage: $(basename "$0") \"prompt\" [--node mac1|mac2|mac3]" >&2
    exit 2
fi

export SUPERSET_SHEPHERD_PROMPT="$PROMPT"
export SUPERSET_SHEPHERD_NODE="$PREFER_NODE"

PYTHONPATH=multi-fleet python3 - <<'PYEOF'
import json, os, sys
from multifleet.superset_shepherd import shepherd_dispatch

prompt = os.environ["SUPERSET_SHEPHERD_PROMPT"]
node = os.environ.get("SUPERSET_SHEPHERD_NODE") or None

result = shepherd_dispatch(prompt=prompt, prefer_node=node)
print(json.dumps(result, indent=2, default=str))
sys.exit(0 if result.get("ok") else 1)
PYEOF
