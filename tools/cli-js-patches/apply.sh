#!/usr/bin/env bash
# Apply the null-safe em1() patch to Claude Code's cli.js.
# Idempotent — safe to re-run after `npm i -g @anthropic-ai/claude-code`.
#
# Usage:
#   bash apply.sh           # apply patch (default)
#   bash apply.sh --check   # report state without modifying
#   bash apply.sh --revert  # restore latest backup
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

"$PYTHON" "$SCRIPT_DIR/apply.py" "$@"
