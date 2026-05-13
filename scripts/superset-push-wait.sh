#!/usr/bin/env bash
# WW11-B: superset-push-wait.sh — push prompt to Superset and wait for completion.
#
# Usage:
#   bash scripts/superset-push-wait.sh "prompt text" [--timeout 120] [--interval 5]
#
# Exits 0 on completion, 1 on timeout/error/push-failure.
# Passes all --workspace, --agent, --device-id, --peer flags through to superset_poller.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MULTIFLEET_DIR="${REPO_ROOT}/multi-fleet"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 \"prompt text\" [--timeout 120] [--interval 5] [--workspace ID] [--peer NODE]" >&2
    exit 1
fi

cd "${REPO_ROOT}"

# Run the poller module; all extra args are forwarded.
PYTHONPATH="${MULTIFLEET_DIR}:${REPO_ROOT}" \
    python3 -m multifleet.superset_poller push_and_wait "$@"
