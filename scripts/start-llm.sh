#!/bin/bash
# =============================================================================
# CONTEXT DNA LOCAL LLM SERVER (mlx_lm.server)
# =============================================================================
#
# This script starts the local LLM server for this Context DNA installation.
# Uses mlx_lm.server for STABILITY over vllm-mlx (which crash-looped).
#
# CURRENT INSTALLATION:
#   Computer: Aaron's M5 MacBook Pro (32GB RAM)
#   Default Model: Qwen3-4B-4bit
#   Port: 5044
#   Server: mlx_lm.server (stable, simple, no batching overhead)
#
# HISTORY:
#   2026-02-07: vllm-mlx + Qwen3-14B-4bit (7.6GB, crashed under memory pressure)
#   2026-02-13: vllm-mlx + Qwen3-4B-4bit (2.85GB, still crash-looped)
#   2026-02-14: mlx_lm.server + Qwen3-4B-4bit (stable, never crashes)
#
# Usage:
#   ./scripts/start-llm.sh                     # Use default model (Qwen3-4B-4bit)
#   ./scripts/start-llm.sh qwen3-14b           # Qwen3-14B (needs 64GB+, slower)
#   ./scripts/start-llm.sh glm                 # GLM-4.7-Flash
#   ./scripts/start-llm.sh <custom-model>      # Any mlx model
#
# =============================================================================

set -e

# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_MODEL="mlx-community/Qwen3-4B-4bit"
PORT=5044

# =============================================================================
# PATHS
# =============================================================================

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PATH="$REPO_ROOT/context-dna/local_llm/.venv-mlx"
PYTHON="$VENV_PATH/bin/python"

# =============================================================================
# CROSS-PLATFORM UTILITIES
# =============================================================================

check_port_in_use() {
    local port=$1
    if command -v lsof >/dev/null 2>&1; then
        lsof -i :$port >/dev/null 2>&1
    elif command -v ss >/dev/null 2>&1; then
        ss -tuln | grep -q ":$port "
    elif command -v netstat >/dev/null 2>&1; then
        netstat -tuln | grep -q ":$port "
    else
        return 1
    fi
}

get_port_process_info() {
    local port=$1
    if command -v lsof >/dev/null 2>&1; then
        echo "   Use: lsof -i :$port | grep LISTEN  to see the process"
        echo "   Use: kill \$(lsof -t -i :$port)    to stop it first"
    fi
}

# =============================================================================
# STARTUP CHECK
# =============================================================================

echo "🔍 Checking port $PORT..."

if check_port_in_use $PORT; then
    echo "⚠️  LLM server already running on port $PORT"
    get_port_process_info $PORT
    exit 1
fi

echo "✅ Port available"
echo ""

# =============================================================================
# MODEL SELECTION
# =============================================================================

if [ -n "$CONTEXTDNA_LLM_MODEL" ]; then
    MODEL="$CONTEXTDNA_LLM_MODEL"
    echo "🔧 Using model from CONTEXTDNA_LLM_MODEL: $MODEL"
elif [ -n "$1" ]; then
    case "$1" in
        qwen3|qwen3-4b)
            MODEL="mlx-community/Qwen3-4B-4bit"
            ;;
        qwen3-14b)
            MODEL="mlx-community/Qwen3-14B-4bit"
            ;;
        qwen3-8b)
            MODEL="mlx-community/Qwen3-8B-4bit"
            ;;
        qwen3-coder|qwen3-moe)
            MODEL="mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
            ;;
        phi4|phi4-mini)
            MODEL="lmstudio-community/Phi-4-mini-reasoning-MLX-4bit"
            ;;
        phi4-instruct)
            MODEL="mlx-community/Phi-4-mini-instruct-4bit"
            ;;
        glm|glm-flash)
            MODEL="lmstudio-community/GLM-4.7-Flash-MLX-4bit"
            ;;
        qwen25|qwen2.5)
            MODEL="mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"
            ;;
        qwen7b|light)
            MODEL="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
            ;;
        *)
            MODEL="$1"
            ;;
    esac
    echo "🔧 Using model from CLI: $MODEL"
else
    MODEL="$DEFAULT_MODEL"
    echo "🔧 Using default model: $MODEL"
fi

echo "   Port: $PORT"
echo "   Model: $MODEL"
echo "   Server: mlx_lm.server (stable mode)"
echo ""
echo "   API at http://127.0.0.1:$PORT/v1/chat/completions"
echo ""

# =============================================================================
# START SERVER (with MLX memory configuration)
# =============================================================================
# mlx_lm.server: Simple, stable, no continuous batching overhead.
# Unlike vllm-mlx, this does NOT crash-loop under memory pressure.
#
# Memory strategy: Allocate generously on Apple Silicon unified memory.
# MLX defaults are conservative — we explicitly claim more GPU budget
# so KV cache + thinking mode have room to breathe.

exec "$PYTHON" -c "
import mlx.core as mx

# --- MLX Memory Configuration for Apple Silicon ---
info = mx.device_info()
total = info['memory_size']
recommended = info['max_recommended_working_set_size']

# Claim 75% of total unified memory for MLX (leaves ~8GB for system/display/Docker)
memory_limit = int(total * 0.75)
mx.set_memory_limit(memory_limit)

# Cache limit: allow 2GB of freed-buffer recycling (reduces Metal allocator fragmentation)
mx.set_cache_limit(2 * 1024**3)

# Wired limit: pin up to the recommended working set (default behavior, explicit for clarity)
mx.set_wired_limit(recommended)

import sys
gb = memory_limit / 1024**3
print(f'MLX memory: {gb:.0f}GB limit / {total/1024**3:.0f}GB total / 2GB cache', file=sys.stderr)

# Start the server
from mlx_lm.server import main
sys.argv = [
    'mlx_lm.server',
    '--model', '$MODEL',
    '--port', '$PORT',
    '--host', '127.0.0.1',
]
main()
"
