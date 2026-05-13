#!/usr/bin/env bash
# Apply 3-Surgeons CLI patches: DeepSeek primary + Keychain fallback.
# Idempotent — safe to re-run after plugin updates.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

"$PYTHON" "$SCRIPT_DIR/apply.py" "$@"

# After applying (no flag = apply), run probe to verify
if [ $# -eq 0 ] || [ "${1:-}" = "--apply" ]; then
    THREE_S_BIN="$HOME/.claude/plugins/cache/3-surgeons-marketplace/3-surgeons/1.0.0/.venv/bin/3s"
    if [ -x "$THREE_S_BIN" ]; then
        echo ""
        echo "=== Running '3s probe' to verify ==="
        "$THREE_S_BIN" probe || true
    fi
fi
