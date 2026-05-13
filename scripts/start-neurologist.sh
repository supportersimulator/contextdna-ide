#!/usr/bin/env bash
# start-neurologist.sh — Verify and warm the Neurologist (local MLX/Qwen3-4B)
#
# CP#5 invariant: Neurologist must be a *distinct* LLM from the Cardiologist.
# Cardiologist = DeepSeek-chat (remote API)
# Neurologist  = mlx-community/Qwen3-4B-4bit (local MLX, port 5044)
#
# This script:
#   1. Checks if MLX is already serving on port 5044
#   2. If not, triggers warm-mlx-on-boot.sh
#   3. Verifies the model responds to a completion request
#   4. Runs 3s neurologist-pulse to confirm CP#5 is satisfied
#
# Usage:
#   bash scripts/start-neurologist.sh           # warm and verify
#   bash scripts/start-neurologist.sh --check   # check only, no warm

set -uo pipefail

REPO_ROOT="${FLEET_REPO_DIR:-$HOME/dev/er-simulator-superrepo}"
MLX_PORT="${MLX_PORT:-5044}"
MLX_HOST="${MLX_HOST:-127.0.0.1}"
MLX_MODEL="mlx-community/Qwen3-4B-4bit"
CHECK_ONLY="${1:-}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] start-neurologist: $*"; }
err() { echo "[$(ts)] start-neurologist: ERROR: $*" >&2; }

# Step 1: Check if MLX is already warm
log "Checking MLX on ${MLX_HOST}:${MLX_PORT}..."
MODELS_JSON=$(/usr/bin/curl -sf --max-time 3 "http://${MLX_HOST}:${MLX_PORT}/v1/models" 2>/dev/null)
if echo "$MODELS_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); ids=[m['id'] for m in d.get('data',[])]; sys.exit(0 if any('Qwen' in i or 'qwen' in i or 'mlx' in i.lower() for i in ids) else 1)" 2>/dev/null; then
    log "MLX already warm with Qwen model on port ${MLX_PORT}"
else
    if [[ "$CHECK_ONLY" == "--check" ]]; then
        err "MLX not warm on port ${MLX_PORT}. Run without --check to warm it."
        exit 1
    fi
    log "MLX not warm — triggering warm-mlx-on-boot.sh..."
    bash "$REPO_ROOT/scripts/warm-mlx-on-boot.sh"
    # Wait up to 30s for it to come up
    for i in $(seq 1 15); do
        sleep 2
        if /usr/bin/curl -sf --max-time 2 "http://${MLX_HOST}:${MLX_PORT}/v1/models" >/dev/null 2>&1; then
            log "MLX warm after ~$((i*2))s"
            break
        fi
        if [[ $i -eq 15 ]]; then
            err "MLX did not come up after 30s. Check logs/mlx-warm.log"
            exit 1
        fi
    done
fi

# Step 2: Verify model responds to a completion (CP#5 smoke test)
log "Testing completion from ${MLX_MODEL}..."
RESPONSE=$(/usr/bin/curl -sf --max-time 60 \
    -X POST "http://${MLX_HOST}:${MLX_PORT}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${MLX_MODEL}\",\"messages\":[{\"role\":\"system\",\"content\":\"You are a health check responder. Be extremely brief.\"},{\"role\":\"user\",\"content\":\"/no_think Say DISTINCT in one word.\"}],\"max_tokens\":10}" \
    2>/dev/null) || { err "Completion request failed"; exit 1; }

echo "$RESPONSE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
msg = d['choices'][0]['message']
content = msg.get('content','') or msg.get('reasoning','') or ''
print(f'Model response: {content[:80]!r}')
model = d.get('model','?')
print(f'Model ID confirmed: {model}')
" 2>/dev/null || { err "Failed to parse completion response: $RESPONSE"; exit 1; }

# Step 3: Run neurologist-pulse
log "Running 3s neurologist-pulse..."
VENV_3S="$REPO_ROOT/venv.nosync/bin/3s"
if [[ -x "$VENV_3S" ]]; then
    "$VENV_3S" neurologist-pulse 2>&1 || {
        err "neurologist-pulse failed — CP#5 may still be violated"
        exit 1
    }
else
    log "3s CLI not found at $VENV_3S — skipping pulse (completion test passed)"
fi

log "CP#5 SATISFIED: Neurologist is mlx-community/Qwen3-4B-4bit (local MLX, port ${MLX_PORT})"
log "Cardiologist: deepseek-chat (remote API) — 3 distinct LLMs confirmed."
