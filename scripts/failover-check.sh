#!/usr/bin/env bash
# failover-check.sh — Lightweight (zero-token) check for agent continuity
# Run via launchd every 15 minutes. No LLM calls, no API tokens.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOGFILE="/tmp/failover-check.log"
FLEET_DIR="$REPO_DIR/.fleet-messages/mac3"
LOCK="/tmp/failover-check.lock"

log() { echo "$(date +%Y-%m-%dT%H:%M:%S) $*" >> "$LOGFILE"; }

# Prevent concurrent runs
if [[ -f "$LOCK" ]]; then
    lock_age=$(( $(date +%s) - $(stat -f%m "$LOCK" 2>/dev/null || echo 0) ))
    if [[ $lock_age -lt 600 ]]; then
        exit 0  # Another check running (< 10 min old)
    fi
    rm -f "$LOCK"  # Stale lock
fi
trap 'rm -f "$LOCK"' EXIT
touch "$LOCK"

# --- Check 1: Is Claude Code active? ---
claude_running=false
if pgrep -f "claude" >/dev/null 2>&1; then
    claude_running=true
fi

# Also check for recent checkpoint (active within last 30 min)
checkpoint="/tmp/model-swap-checkpoint.json"
if [[ -f "$checkpoint" ]]; then
    cp_age=$(( $(date +%s) - $(stat -f%m "$checkpoint" 2>/dev/null || echo 0) ))
    if [[ $cp_age -lt 1800 ]]; then
        claude_running=true
    fi
fi

if $claude_running; then
    log "OK: Claude Code active"
    exit 0
fi

# --- Check 2: Is Codex already running? ---
if pgrep -f "codex" >/dev/null 2>&1; then
    log "OK: Codex active (Claude inactive)"
    exit 0
fi

# --- Check 3: Are there pending tasks? ---
has_tasks=false
if [[ -f "$FLEET_DIR/codex-failover-tasks.md" ]]; then
    task_age=$(( $(date +%s) - $(stat -f%m "$FLEET_DIR/codex-failover-tasks.md" 2>/dev/null || echo 0) ))
    # Only consider tasks < 24h old
    if [[ $task_age -lt 86400 ]]; then
        has_tasks=true
    fi
fi

# Check for dirty working tree (unfinished work)
dirty_count=$(git -C "$REPO_DIR" status --porcelain 2>/dev/null | grep -c "^ M\|^M " || true)
if [[ $dirty_count -gt 0 ]]; then
    has_tasks=true
fi

if ! $has_tasks; then
    log "IDLE: No agent active, no pending tasks"
    exit 0
fi

# --- Launch failover ---
log "FAILOVER: Neither Claude nor Codex active. Pending tasks found. Launching failover..."
bash "$REPO_DIR/scripts/failover-to-codex.sh" --force >> "$LOGFILE" 2>&1 &
log "Failover launched (PID: $!)"
