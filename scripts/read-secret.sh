#!/usr/bin/env bash
# read-secret.sh — Resolve a secret from the best available backend.
#
# Usage:
#   source scripts/read-secret.sh
#   val=$(read_secret "DEEPSEEK_API_KEY")
#
# Or standalone:
#   scripts/read-secret.sh DEEPSEEK_API_KEY
#
# Resolution order:
#   1. macOS Keychain (service: "fleet-nerve", account: <key_name>)
#   2. macOS Keychain (service: <key_name>, account: $USER)  — legacy/llm-proxy style
#   3. Cross-platform `keyring` CLI (service: "multifleet") — Linux libsecret,
#      Windows Credential Manager when running under Git-Bash/WSL
#   4. ~/.fleet-nerve/env file
#   5. Shell environment variable
#
# Returns empty string if not found anywhere.

_strip_quotes() {
    # Remove leading/trailing double or single quotes (env files often include them)
    local s="$1"
    s="${s#\"}" ; s="${s%\"}"   # strip double quotes
    s="${s#\'}" ; s="${s%\'}"   # strip single quotes
    echo "$s"
}

read_secret() {
    local key_name="$1"
    local val=""

    # 1. Keychain — fleet-nerve service (preferred)
    val=$(security find-generic-password -s "fleet-nerve" -a "$key_name" -w 2>/dev/null) && [ -n "$val" ] && echo "$(_strip_quotes "$val")" && return

    # 2. Keychain — standalone service (e.g. DEEPSEEK_API_KEY added directly)
    val=$(security find-generic-password -s "$key_name" -a "$USER" -w 2>/dev/null) && [ -n "$val" ] && echo "$(_strip_quotes "$val")" && return

    # 3. Cross-platform keyring — libsecret on Linux, Credential Manager on Windows
    if command -v keyring >/dev/null 2>&1; then
        val=$(keyring get "multifleet" "$key_name" 2>/dev/null) || true
        [ -n "$val" ] && echo "$(_strip_quotes "$val")" && return
    fi

    # 4. Env file fallback
    local env_file="$HOME/.fleet-nerve/env"
    if [ -f "$env_file" ]; then
        val=$(grep "^${key_name}=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2-) || true
        [ -n "$val" ] && echo "$(_strip_quotes "$val")" && return
    fi

    # 5. Shell environment
    val="${!key_name:-}"
    [ -n "$val" ] && echo "$(_strip_quotes "$val")" && return

    # Not found
    echo ""
}

# Standalone mode: scripts/read-secret.sh KEY_NAME
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    [ -z "${1:-}" ] && echo "Usage: $0 <KEY_NAME>" >&2 && exit 1
    read_secret "$1"
fi
