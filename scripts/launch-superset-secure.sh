#!/usr/bin/env bash
# launch-superset-secure.sh — launch Superset with OpenAI credentials loaded
# from secure storage, without writing secrets into repo files.
#
# Usage:
#   bash scripts/launch-superset-secure.sh status
#   bash scripts/launch-superset-secure.sh launch
#   bash scripts/launch-superset-secure.sh clear

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/read-secret.sh
source "$SCRIPT_DIR/read-secret.sh"

usage() {
    cat <<'EOF'
Usage:
  bash scripts/launch-superset-secure.sh status
  bash scripts/launch-superset-secure.sh launch
  bash scripts/launch-superset-secure.sh clear

Commands:
  status  Show whether Superset/OpenAI keys are resolvable from secure storage.
  launch  Export OpenAI aliases into the current launchd user session and open Superset.
  clear   Remove those launchd session variables after you quit Superset.
EOF
}

resolve_first() {
    local key_name
    local value=""
    for key_name in "$@"; do
        value="$(read_secret "$key_name")"
        if [ -n "$value" ]; then
            printf '%s' "$value"
            return 0
        fi
    done
    return 1
}

set_launchd_var() {
    local name="$1"
    local value="$2"
    launchctl setenv "$name" "$value"
    printf 'Set %s in launchd session\n' "$name"
}

clear_launchd_var() {
    local name="$1"
    launchctl unsetenv "$name" || true
    printf 'Cleared %s from launchd session\n' "$name"
}

command_name="${1:-status}"

case "$command_name" in
    status)
        if openai_key="$(resolve_first FLEET_OPENAI_API_KEY Context_DNA_OPENAI OPENAI_API_KEY)"; then
            echo "OpenAI key: available"
        else
            echo "OpenAI key: missing"
        fi

        if superset_key="$(resolve_first SUPERSET_API_KEY Superset_contextdna_key)"; then
            echo "Superset key: available"
        else
            echo "Superset key: missing"
        fi
        ;;

    launch)
        if ! command -v launchctl >/dev/null 2>&1; then
            echo "launchctl not found; this helper is for macOS user sessions." >&2
            exit 1
        fi
        if ! command -v open >/dev/null 2>&1; then
            echo "open not found; cannot launch Superset.app." >&2
            exit 1
        fi

        if ! openai_key="$(resolve_first FLEET_OPENAI_API_KEY Context_DNA_OPENAI OPENAI_API_KEY)"; then
            cat >&2 <<'EOF'
No OpenAI API key found.

Store it in macOS Keychain without committing it:
  security add-generic-password -s fleet-nerve -a Context_DNA_OPENAI -w 'sk-...' -U
EOF
            exit 1
        fi

        set_launchd_var "OPENAI_API_KEY" "$openai_key"
        set_launchd_var "Context_DNA_OPENAI" "$openai_key"
        set_launchd_var "FLEET_OPENAI_API_KEY" "$openai_key"

        if superset_key="$(resolve_first SUPERSET_API_KEY Superset_contextdna_key)"; then
            set_launchd_var "SUPERSET_API_KEY" "$superset_key"
        else
            echo "Superset API key not found; launching without SUPERSET_API_KEY."
        fi

        echo "Launching Superset.app"
        open -a Superset
        echo "Superset launched. Run 'bash scripts/launch-superset-secure.sh clear' after quitting it if you want to remove the session vars."
        ;;

    clear)
        clear_launchd_var "OPENAI_API_KEY"
        clear_launchd_var "Context_DNA_OPENAI"
        clear_launchd_var "FLEET_OPENAI_API_KEY"
        clear_launchd_var "SUPERSET_API_KEY"
        ;;

    *)
        usage >&2
        exit 1
        ;;
esac
