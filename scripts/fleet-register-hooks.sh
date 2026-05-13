#!/usr/bin/env bash
# fleet-register-hooks.sh — Idempotent fleet-inbox hook registration
#
# Detects current node, sets correct CHIEF_INGEST_URL, merges hooks into
# ~/.claude/settings.json without touching permissions/plugins/marketplaces.
#
# Usage:
#   ./scripts/fleet-register-hooks.sh              # Apply hooks
#   ./scripts/fleet-register-hooks.sh --dry-run     # Show what would change
#   ssh mac1 "bash ~/dev/er-simulator-superrepo/scripts/fleet-register-hooks.sh"
#   ssh mac2 "bash ~/dev/er-simulator-superrepo/scripts/fleet-register-hooks.sh"

set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

SETTINGS_FILE="$HOME/.claude/settings.json"

# --- Detect node + chief (config-driven, no hard-coded names) ---
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
source "$REPO_ROOT/scripts/fleet-node-id.sh"

NODE_ID="$(fleet_node_id)"
CHIEF_ID="$(fleet_chief_id)"
NETWORK_CONF="${REPO_ROOT}/scripts/3s-network.local.conf"

if [[ -n "$CHIEF_ID" && "$NODE_ID" == "$CHIEF_ID" ]]; then
    # We ARE the chief — always loopback.
    CHIEF_INGEST_URL="http://127.0.0.1:8844"
elif [[ -f "$NETWORK_CONF" ]]; then
    # shellcheck source=/dev/null
    source "$NETWORK_CONF"
    CHIEF_VAR="PEER_${CHIEF:-${CHIEF_ID:-mac1}}"
    CHIEF_IP="${!CHIEF_VAR#*@}"
    CHIEF_INGEST_URL="http://${CHIEF_IP}:8844"
else
    # Fallback: mDNS hostname (requires chief to advertise via Bonjour)
    CHIEF_INGEST_URL="http://chief.local:8844"
fi

HOOK_CMD="CHIEF_INGEST_URL=${CHIEF_INGEST_URL} MULTIFLEET_NODE_ID=${NODE_ID} ~/dev/er-simulator-superrepo/scripts/fleet-inbox-hook.sh"

echo "[fleet-register-hooks] Node: ${NODE_ID}"
echo "[fleet-register-hooks] Chief URL: ${CHIEF_INGEST_URL}"
echo "[fleet-register-hooks] Hook command: ${HOOK_CMD}"

# --- Ensure settings file exists ---
if [[ ! -f "$SETTINGS_FILE" ]]; then
    mkdir -p "$(dirname "$SETTINGS_FILE")"
    echo '{}' > "$SETTINGS_FILE"
    echo "[fleet-register-hooks] Created empty $SETTINGS_FILE"
fi

# --- Build desired hooks JSON and merge via python3 (available on all macOS) ---
python3 << PYEOF
import json, sys, os

settings_file = os.path.expanduser("${SETTINGS_FILE}")
hook_cmd = """${HOOK_CMD}"""
dry_run = ${DRY_RUN}

with open(settings_file, "r") as f:
    settings = json.load(f)

# Build the desired hook entry
def make_hook_entry(status_msg=None):
    entry = {
        "type": "command",
        "command": hook_cmd,
        "asyncRewake": True,
    }
    if status_msg:
        entry["statusMessage"] = status_msg
    return entry

desired_hooks = {
    "SessionStart": [{"hooks": [make_hook_entry("Checking fleet inbox...")]}],
    "Stop": [{"hooks": [make_hook_entry()]}],
    "UserPromptSubmit": [{"hooks": [make_hook_entry()]}],
}

# Check if current hooks already match
current_hooks = settings.get("hooks", {})
already_correct = True

for event_name, desired_value in desired_hooks.items():
    current_value = current_hooks.get(event_name)
    if current_value != desired_value:
        already_correct = False
        break

if already_correct:
    print("[fleet-register-hooks] Hooks already correct. No changes needed.")
    sys.exit(0)

if dry_run:
    print()
    print("--- Current hooks ---")
    print(json.dumps(current_hooks, indent=2) if current_hooks else "(none)")
    print()
    print("--- Desired hooks ---")
    print(json.dumps(desired_hooks, indent=2))
    print()
    print("[fleet-register-hooks] DRY RUN — no changes written.")
    sys.exit(0)

# Merge: only touch the hooks key, preserve everything else
settings["hooks"] = desired_hooks

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print("[fleet-register-hooks] Hooks updated in " + settings_file)
PYEOF
