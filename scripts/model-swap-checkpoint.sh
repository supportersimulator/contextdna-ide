#!/usr/bin/env bash
# model-swap-checkpoint.sh — Pre-model-fallback invariance hook
#
# Called BEFORE model fallback occurs. Captures project spine so any
# replacement model can restore context: git state, active tasks,
# CLAUDE.md awareness, last commits.
#
# Output: /tmp/model-swap-checkpoint.json
#
# Usage:
#   bash scripts/model-swap-checkpoint.sh [reason] [target_model]
#   bash scripts/model-swap-checkpoint.sh "periodic" "deepseek-v3"
#   bash scripts/model-swap-checkpoint.sh  # defaults: "model-swap", ""

set -uo pipefail

REASON="${1:-model-swap}"
TARGET_MODEL="${2:-}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="/tmp/model-swap-checkpoint.json"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CP_ID="swap-$(date -u +%Y%m%d-%H%M%S)"

# ── Git state ──
BRANCH="$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo "")"
LAST_5="$(git -C "$REPO_ROOT" log --oneline -5 2>/dev/null || echo "")"
DIRTY="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || echo "")"
STAGED="$(git -C "$REPO_ROOT" diff --cached --name-only 2>/dev/null || echo "")"

# ── Active task from fleet messages ──
ACTIVE_TASK=""
INBOX_COUNT=0
INBOX_DIR="$REPO_ROOT/.fleet-messages/$NODE_ID"
if [ -d "$INBOX_DIR" ]; then
    INBOX_COUNT=$(find "$INBOX_DIR" -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
    NEWEST=$(find "$INBOX_DIR" -name "*.md" -print0 2>/dev/null | xargs -0 ls -t 2>/dev/null | head -1)
    if [ -n "$NEWEST" ]; then
        ACTIVE_TASK=$(grep -m1 "^subject:" "$NEWEST" 2>/dev/null | cut -d: -f2- | xargs || basename "$NEWEST" .md)
    fi
fi

# ── CLAUDE.md awareness ──
CLAUDE_MD_HASH=""
CLAUDE_MD_SUMMARY=""
if [ -f "$REPO_ROOT/CLAUDE.md" ]; then
    CLAUDE_MD_HASH=$(shasum -a 256 "$REPO_ROOT/CLAUDE.md" 2>/dev/null | cut -c1-16)
    CLAUDE_MD_SUMMARY=$(grep -v '^#' "$REPO_ROOT/CLAUDE.md" | grep -v '^\s*$' | head -1 | cut -c1-200)
fi

# ── Build JSON ──
# Use python if available for proper JSON escaping, else jq, else raw
if command -v python3 &>/dev/null; then
    python3 -c "
import json, sys
data = {
    'checkpoint_id': '$CP_ID',
    'timestamp': '$TS',
    'reason': '$REASON',
    'source_model': '${ANTHROPIC_MODEL:-unknown}',
    'target_model': '$TARGET_MODEL',
    'branch': '$BRANCH',
    'last_5_commits': [l for l in '''$LAST_5'''.strip().splitlines() if l.strip()],
    'dirty_files': [l.strip() for l in '''$DIRTY'''.strip().splitlines() if l.strip()],
    'staged_files': [l.strip() for l in '''$STAGED'''.strip().splitlines() if l.strip()],
    'active_task': '$ACTIVE_TASK',
    'fleet_inbox_count': $INBOX_COUNT,
    'claude_md_hash': '$CLAUDE_MD_HASH',
    'claude_md_summary': '$CLAUDE_MD_SUMMARY',
    'session_id': '${CLAUDE_SESSION_ID:-}',
    'node_id': '$NODE_ID',
    'working_dir': '$REPO_ROOT',
}
json.dump(data, open('$OUT', 'w'), indent=2)
print(json.dumps({'status': 'ok', 'checkpoint_id': data['checkpoint_id'], 'path': '$OUT'}, indent=2))
"
else
    echo "python3 not found — cannot write checkpoint" >&2
    exit 1
fi
