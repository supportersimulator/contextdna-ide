#!/usr/bin/env bash
# vscode-to-superset.sh — HHH6 thin wrapper for pushing VS Code Claude Code
# session intent into Superset (opencode / agent / task).
#
# Usage:
#   bash scripts/vscode-to-superset.sh prompt "deploy the landing page"
#   bash scripts/vscode-to-superset.sh prompt "..." --peer mac3          # WaveL: cross-node
#   bash scripts/vscode-to-superset.sh prompt "..." --inject-context     # WW11-A: inject CLAUDE.md + memory preamble
#   bash scripts/vscode-to-superset.sh prompt-with-context "..."         # WW11-A: alias with inject always on
#   bash scripts/vscode-to-superset.sh task "Audit mac3 plist drift" --description "..." --priority high
#   bash scripts/vscode-to-superset.sh task "..." --peer mac1            # WaveL: cross-node
#   bash scripts/vscode-to-superset.sh workspace --project-id <id> --branch feature/x
#
# Exits 0 on success, non-zero on failure. ZSF: errors surface to stderr.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

if [[ ! -d multi-fleet/multifleet ]]; then
  echo "vscode-to-superset: multi-fleet not present at expected path" >&2
  exit 2
fi

PYTHONPATH=multi-fleet python3 -m multifleet.vscode_superset_bridge "$@"
