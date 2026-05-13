#!/usr/bin/env bash
# fleet-node-id.sh — Single source of truth for fleet node identity in shell.
#
# Mirrors multifleet/fleet_config.py resolution order so public users who name
# their nodes anything (laptop-1, prod-east-a, dev01, …) don't have to patch
# every script that historically hard-coded mac1/mac2/mac3.
#
# Resolution:
#   1. $MULTIFLEET_NODE_ID env var (explicit override wins)
#   2. IP match against .multifleet/config.json "nodes" table
#      (matches either "host" or "lan_ip" against our local IP)
#   3. legacy PEER_* entries in scripts/3s-network.local.conf
#   4. hostname -s, lowercased, first dot-segment
#
# Usage (from another shell script):
#
#     REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
#     # shellcheck source=/dev/null
#     source "$REPO_ROOT/scripts/fleet-node-id.sh"
#     NODE_ID="$(fleet_node_id)"
#
# Or standalone:  bash scripts/fleet-node-id.sh

fleet_node_id() {
    if [[ -n "${MULTIFLEET_NODE_ID:-}" ]]; then
        printf '%s\n' "$MULTIFLEET_NODE_ID"
        return 0
    fi

    local repo_root
    if [[ -n "${FLEET_REPO_ROOT:-}" ]]; then
        repo_root="$FLEET_REPO_ROOT"
    elif [[ -n "${BASH_SOURCE[0]:-}" ]]; then
        repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
    else
        repo_root="$(pwd)"
    fi

    local my_ip
    my_ip=$(ipconfig getifaddr en0 2>/dev/null \
         || ipconfig getifaddr en1 2>/dev/null \
         || hostname -I 2>/dev/null | awk '{print $1}' \
         || echo "")

    # 2. .multifleet/config.json — preferred (single source of truth)
    local mf_conf="$repo_root/.multifleet/config.json"
    if [[ -f "$mf_conf" && -n "$my_ip" ]] && command -v python3 >/dev/null 2>&1; then
        local resolved
        resolved=$(MF_IP="$my_ip" MF_CONF="$mf_conf" python3 - <<'PYEOF' 2>/dev/null
import json, os, sys
try:
    cfg = json.load(open(os.environ["MF_CONF"]))
except Exception:
    sys.exit(0)
my_ip = os.environ.get("MF_IP", "")
for nid, node in (cfg.get("nodes") or {}).items():
    if not isinstance(node, dict):
        continue
    if node.get("host") == my_ip or node.get("lan_ip") == my_ip:
        print(nid)
        break
PYEOF
)
        if [[ -n "$resolved" ]]; then
            printf '%s\n' "$resolved"
            return 0
        fi
    fi

    # 3. Legacy scripts/3s-network.local.conf
    local legacy_conf="$repo_root/scripts/3s-network.local.conf"
    if [[ -f "$legacy_conf" && -n "$my_ip" ]]; then
        while IFS= read -r line; do
            if [[ "$line" =~ PEER_([a-zA-Z0-9_-]+)=\"[^@]+@([^\"]+)\" ]]; then
                local name="${BASH_REMATCH[1]}"
                local ip="${BASH_REMATCH[2]}"
                if [[ "$ip" == "$my_ip" ]]; then
                    printf '%s\n' "$name"
                    return 0
                fi
            fi
        done < "$legacy_conf"
    fi

    # 4. Hostname fallback
    local h
    h=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')
    printf '%s\n' "${h%%.*}"
}

# fleet_chief_id — resolve chief node id from config; fallback to "mac1" for
# legacy deployments. Public users set "chief.nodeId" in .multifleet/config.json.
fleet_chief_id() {
    local repo_root
    if [[ -n "${FLEET_REPO_ROOT:-}" ]]; then
        repo_root="$FLEET_REPO_ROOT"
    elif [[ -n "${BASH_SOURCE[0]:-}" ]]; then
        repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
    else
        repo_root="$(pwd)"
    fi
    local mf_conf="$repo_root/.multifleet/config.json"
    if [[ -f "$mf_conf" ]] && command -v python3 >/dev/null 2>&1; then
        MF_CONF="$mf_conf" python3 - <<'PYEOF' 2>/dev/null
import json, os
try:
    cfg = json.load(open(os.environ["MF_CONF"]))
    cid = (cfg.get("chief") or {}).get("nodeId")
    if cid:
        print(cid)
except Exception:
    pass
PYEOF
    fi
}

# fleet_peer_ids — print all known peer node IDs (one per line) from config.
# Empty output => single-node standalone. Callers that need a legacy fallback
# should apply it themselves so OSS adopters see an explicit warning.
fleet_peer_ids() {
    local repo_root
    if [[ -n "${FLEET_REPO_ROOT:-}" ]]; then
        repo_root="$FLEET_REPO_ROOT"
    elif [[ -n "${BASH_SOURCE[0]:-}" ]]; then
        repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
    else
        repo_root="$(pwd)"
    fi
    local mf_conf="$repo_root/.multifleet/config.json"
    if [[ -f "$mf_conf" ]] && command -v python3 >/dev/null 2>&1; then
        MF_CONF="$mf_conf" python3 - <<'PYEOF' 2>/dev/null
import json, os
try:
    cfg = json.load(open(os.environ["MF_CONF"]))
    for nid in sorted((cfg.get("nodes") or {}).keys()):
        print(nid)
except Exception:
    pass
PYEOF
    fi
}

# Standalone CLI: `bash scripts/fleet-node-id.sh` prints our node id.
# Subcommands: `chief`, `peers`.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    case "${1:-self}" in
        self|"") fleet_node_id ;;
        chief)   fleet_chief_id ;;
        peers)   fleet_peer_ids ;;
        *) echo "Usage: $0 [self|chief|peers]" >&2; exit 2 ;;
    esac
fi
