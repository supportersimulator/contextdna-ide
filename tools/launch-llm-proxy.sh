#!/bin/bash
# Launcher for llm_priority_proxy.py — reads secrets from Keychain, not plaintext.
# Used by io.contextdna.llm-proxy LaunchAgent.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO_ROOT/scripts/read-secret.sh"

export MULTIFLEET_NODE_ID="${MULTIFLEET_NODE_ID:-mac3}"
export EXTERNAL_LLM_ENDPOINT="https://api.deepseek.com/v1"
export EXTERNAL_LLM_API_KEY_ENV="DEEPSEEK_API_KEY"
export EXTERNAL_LLM_MODEL="deepseek-chat"

# Read API keys from Keychain (unified resolution)
export DEEPSEEK_API_KEY="$(read_secret DEEPSEEK_API_KEY || read_secret Context_DNA_Deepseek)"
export Context_DNA_Deepseek="$DEEPSEEK_API_KEY"

if [ -z "$DEEPSEEK_API_KEY" ]; then
    echo "ERROR: DEEPSEEK_API_KEY not found. Add with:" >&2
    echo "  security add-generic-password -s fleet-nerve -a DEEPSEEK_API_KEY -w '<key>'" >&2
    exit 1
fi

exec /usr/bin/python3 "$REPO_ROOT/tools/llm_priority_proxy.py"
