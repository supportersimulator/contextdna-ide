#!/usr/bin/env bash
# fleet-send.sh — Send a message to one or more fleet nodes via the local
# fleet daemon (canonical dispatcher input per MFINV-C01 / plugin pre_tool_use
# allowlist). Defaults to http://127.0.0.1:8855/message which the daemon then
# routes through its P0-P7 cascade.
#
# WW4 heal (2026-05-12): previous default chief.local:8844 was dead — that
# port wasn't listening on the chief anymore and mDNS chief.local did not
# resolve. Local daemon is the supported entry point.
#
# Usage:
#   ./scripts/fleet-send.sh mac1 "subject" "body"
#   ./scripts/fleet-send.sh "mac1,mac2" "subject" "body"
#   ./scripts/fleet-send.sh all "subject" "body"          # → mac1, mac2, mac3
#   ./scripts/fleet-send.sh mac1 "subject" "body" --priority high
#
# Override with FLEET_DAEMON_URL or legacy CHIEF_INGEST_URL.

FLEET_DAEMON_URL="${FLEET_DAEMON_URL:-${CHIEF_INGEST_URL:-http://127.0.0.1:8855}}"

_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
source "$_REPO_ROOT/scripts/fleet-node-id.sh"

FROM="$(fleet_node_id)"
TO="${1:-}"
SUBJECT="${2:-}"
BODY="${3:-}"
PRIORITY="normal"
[[ "${4:-}" == "--priority" ]] && PRIORITY="${5:-normal}"
[[ "${4:-}" =~ ^(high|low|normal)$ ]] && PRIORITY="${4}"

if [[ -z "$TO" || -z "$SUBJECT" || -z "$BODY" ]]; then
    echo "Usage: $0 <to> <subject> <body> [high|low|normal]"
    echo "  <to>: node name, comma-separated list, or 'all'"
    exit 1
fi

# Expand "all" — resolves peers from .multifleet/config.json; falls back to the
# legacy triad so existing mac1/mac2/mac3 deployments keep working. OSS adopters
# with different node names only need to populate the config's "nodes" section.
if [[ "$TO" == "all" ]]; then
    _peers=$(fleet_peer_ids | tr '\n' ',')
    _peers="${_peers%,}"
    if [[ -n "$_peers" ]]; then
        TO="$_peers"
    else
        echo "[fleet-send] WARN: no peers in .multifleet/config.json — falling back to legacy mac1,mac2,mac3" >&2
        TO="mac1,mac2,mac3"
    fi
fi

# Expand recipient list and dispatch one POST per peer in the daemon's
# canonical /message envelope (single string `to`, nested payload). The
# daemon's send_with_fallback() does its own cascade — we don't need to
# batch on the client side. Per-peer dispatch also gives us a per-peer
# delivery result rather than one collapsed status.
export _FROM="$FROM" _SUBJECT="$SUBJECT" _BODY="$BODY" _PRIORITY="$PRIORITY"
_rc_all=0
IFS=',' read -ra _PEERS <<< "$TO"
for _peer in "${_PEERS[@]}"; do
    _peer="${_peer// /}"
    [[ -z "$_peer" ]] && continue
    export _PEER="$_peer"
    PAYLOAD=$(python3 -c "
import json, os
print(json.dumps({
    'type': 'context',
    'from': os.environ['_FROM'],
    'to':   os.environ['_PEER'],
    'payload': {
        'subject':  os.environ['_SUBJECT'],
        'body':     os.environ['_BODY'],
        'priority': os.environ['_PRIORITY'],
    }
}))
")
    RESP=$(curl -sf -m 8 -X POST "${FLEET_DAEMON_URL}/message" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null)
    if [[ $? -eq 0 ]]; then
        echo "[fleet-send] → ${_peer}: $RESP"
    else
        echo "[fleet-send] ERROR: could not reach daemon at $FLEET_DAEMON_URL (peer=$_peer)"
        _rc_all=1
    fi
done
exit "$_rc_all"
