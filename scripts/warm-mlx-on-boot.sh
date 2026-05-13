#!/usr/bin/env bash
# warm-mlx-on-boot.sh — Ensure local MLX server (Qwen3-4B) is loaded on 127.0.0.1:5044.
#
# This script is the durable side of RACE U2 (3-surgeons distinctness). When MLX
# is up, the neurologist can target a *different* (vendor, endpoint, model)
# tuple than the cardiologist — restoring the "3 distinct LLMs" invariant
# (Constitutional Physics #5).
#
# Behavior:
#   1. If 127.0.0.1:5044 already serves a Qwen* model → exit 0 (fast no-op).
#   2. If the .venv-mlx and mlx_lm.server are present → start the server in the
#      background, log to logs/llm_server.log, exit 0 once /v1/models responds.
#   3. If prerequisites are missing → log a clear failure line and exit 0
#      anyway (we never want this script to fail boot — observability over
#      blocking; ZERO SILENT FAILURES still applies via the log line).
#
# Designed to be invoked by launchd (RunAtLoad=true) via
#   ~/Library/LaunchAgents/io.contextdna.mlx-warm.plist
# and is safe to run interactively for debugging.
#
# Usage:
#   bash scripts/warm-mlx-on-boot.sh                # warm if not warm
#   FORCE_RESTART=1 bash scripts/warm-mlx-on-boot.sh   # restart even if alive
set -uo pipefail

REPO_ROOT="${FLEET_REPO_DIR:-$HOME/dev/er-simulator-superrepo}"
MLX_VENV="$REPO_ROOT/context-dna/local_llm/.venv-mlx"
MLX_PYTHON="$MLX_VENV/bin/python"
MLX_MODEL="${MLX_MODEL:-mlx-community/Qwen3-4B-4bit}"
MLX_PORT="${MLX_PORT:-5044}"
MLX_HOST="${MLX_HOST:-127.0.0.1}"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/llm_server.log"
WARM_LOG="$LOG_DIR/mlx-warm.log"

mkdir -p "$LOG_DIR"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] warm-mlx: $*" | tee -a "$WARM_LOG"; }

# --- Step 1: probe current state ---------------------------------------------
probe_models() {
    curl -sS -m 3 "http://${MLX_HOST}:${MLX_PORT}/v1/models" 2>/dev/null
}

is_warm() {
    local body
    body="$(probe_models)" || return 1
    # Heuristic: any Qwen model present → warm. Match case-insensitively.
    echo "$body" | grep -qi -E '"id"[[:space:]]*:[[:space:]]*"[^"]*qwen' && return 0
    return 1
}

if [[ "${FORCE_RESTART:-0}" != "1" ]] && is_warm; then
    log "already warm (Qwen model on ${MLX_HOST}:${MLX_PORT})"
    exit 0
fi

# --- Step 2: prerequisites ---------------------------------------------------
if [[ ! -x "$MLX_PYTHON" ]]; then
    log "FAIL prerequisite missing: $MLX_PYTHON not executable. mac3 is API-only by design; this is expected there. On mac1, run mlx_installer to create .venv-mlx."
    exit 0
fi

if ! "$MLX_PYTHON" -c "import mlx_lm" 2>/dev/null; then
    log "FAIL prerequisite missing: mlx_lm not importable from $MLX_PYTHON"
    exit 0
fi

# --- Step 3: launch ----------------------------------------------------------
log "starting mlx_lm.server model=$MLX_MODEL host=$MLX_HOST port=$MLX_PORT log=$LOG_FILE"
nohup "$MLX_PYTHON" -m mlx_lm.server \
    --model "$MLX_MODEL" \
    --host "$MLX_HOST" \
    --port "$MLX_PORT" \
    >>"$LOG_FILE" 2>&1 &
SERVER_PID=$!
log "spawned pid=$SERVER_PID"

# Wait up to 60s for /v1/models to respond.
for i in $(seq 1 30); do
    sleep 2
    if is_warm; then
        log "warm after ${i} attempts (~$((i*2))s)"
        exit 0
    fi
    # Did the child die?
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        log "FAIL spawned process exited; see $LOG_FILE"
        exit 0
    fi
done

log "WARN spawned but did not become warm within 60s; pid=$SERVER_PID still alive — model may still be downloading"
exit 0
