#!/usr/bin/env bash
# mac1-auto-wire.sh — Self-healing auto-deploy: wires up mac1 when it becomes reachable.
#
# Runs on every fleet-check tick. Idempotent, rate-limited, non-blocking.
# When mac1 becomes reachable (LAN or Tailscale), it will:
#   1) git fetch/pull origin main
#   2) Run enable-clustering.sh if not already clustered
#   3) Clean stale rebase state
#   4) Kickstart fleet daemon
#   5) Verify via /health poll (30s exp backoff)
#   6) Emit ack via .fleet-messages/mac1/auto-wire-ack.md
#
# Controls:
#   bash scripts/mac1-auto-wire.sh              # normal tick
#   bash scripts/mac1-auto-wire.sh --force      # bypass rate limits
#   cat /tmp/mac1-auto-wire.state               # view status
#   tail -f /tmp/mac1-auto-wire.log             # watch log
#   touch /tmp/mac1-auto-wire.disabled          # disable entirely

set -uo pipefail

# ── Paths / constants ──
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
LOCK_FILE="/tmp/mac1-auto-wire.lock"
STATE_FILE="/tmp/mac1-auto-wire.state"
LOG_FILE="/tmp/mac1-auto-wire.log"
DISABLED_FILE="/tmp/mac1-auto-wire.disabled"
CONFIG_FILE="${REPO_DIR}/.multifleet/config.json"
FLEET_MSG_DIR="${REPO_DIR}/.fleet-messages/mac1"

SUCCESS_COOLDOWN_SECS=$((6 * 60 * 60))   # 6h — don't re-run when working
REATTEMPT_COOLDOWN_SECS=300              # 5min — rate-limit retries
SSH_CONNECT_TIMEOUT=3
SSH_TOTAL_TIMEOUT=60
VERIFY_TIMEOUT_SECS=30

FORCE=false
[[ "${1:-}" == "--force" ]] && FORCE=true

# ── Logging (rolling 100-line limit) ──
log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
    # Rotate: keep last 100 lines
    if [[ -f "$LOG_FILE" ]]; then
        local lines
        lines=$(wc -l < "$LOG_FILE" 2>/dev/null | tr -d ' ')
        if [[ "${lines:-0}" -gt 100 ]]; then
            tail -100 "$LOG_FILE" > "${LOG_FILE}.tmp" 2>/dev/null && mv "${LOG_FILE}.tmp" "$LOG_FILE"
        fi
    fi
}

# ── State file helpers ──
state_get() {
    # Usage: state_get <key>
    [[ -f "$STATE_FILE" ]] || { echo ""; return; }
    /usr/bin/python3 - "$STATE_FILE" "$1" <<'PY' 2>/dev/null || echo ""
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get(sys.argv[2], ""))
except Exception:
    pass
PY
}

state_set() {
    # Usage: state_set <key1> <val1> [<key2> <val2> ...]
    /usr/bin/python3 - "$STATE_FILE" "$@" <<'PY' 2>/dev/null || true
import json, sys, os
path = sys.argv[1]
kvs = sys.argv[2:]
try:
    d = json.load(open(path)) if os.path.exists(path) else {}
except Exception:
    d = {}
for i in range(0, len(kvs), 2):
    if i + 1 < len(kvs):
        d[kvs[i]] = kvs[i + 1]
with open(path, "w") as f:
    json.dump(d, f, indent=2)
PY
}

# ── Disabled check ──
if [[ -f "$DISABLED_FILE" ]]; then
    log "disabled (remove $DISABLED_FILE to re-enable), skip"
    exit 0
fi

# ── Lock (concurrent run guard) ──
if [[ -f "$LOCK_FILE" ]]; then
    # Stale lock >10min old gets cleared
    lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK_FILE" 2>/dev/null || echo 0) ))
    if [[ "$lock_age" -gt 600 ]]; then
        log "stale lock (${lock_age}s), clearing"
        rm -f "$LOCK_FILE"
    else
        log "lock held (age=${lock_age}s), skip concurrent run"
        exit 0
    fi
fi
trap 'rm -f "$LOCK_FILE"' EXIT
touch "$LOCK_FILE"

NOW=$(date +%s)
LAST_SUCCESS=$(state_get last_success_ts)
LAST_ATTEMPT=$(state_get last_attempt_ts)

# ── Rate limits ──
if ! $FORCE; then
    if [[ -n "$LAST_SUCCESS" && "$LAST_SUCCESS" =~ ^[0-9]+$ ]]; then
        age=$(( NOW - LAST_SUCCESS ))
        if [[ "$age" -lt "$SUCCESS_COOLDOWN_SECS" ]]; then
            log "last success ${age}s ago (<6h), skip"
            exit 0
        fi
    fi
    if [[ -n "$LAST_ATTEMPT" && "$LAST_ATTEMPT" =~ ^[0-9]+$ && -n "$LAST_SUCCESS" && "$LAST_SUCCESS" =~ ^[0-9]+$ ]]; then
        age=$(( NOW - LAST_ATTEMPT ))
        if [[ "$age" -lt "$REATTEMPT_COOLDOWN_SECS" ]]; then
            log "last attempt ${age}s ago (<5min) and prior success exists, skip"
            exit 0
        fi
    fi
fi

state_set last_attempt_ts "$NOW"

# ── Resolve mac1 IPs from config ──
MAC1_LAN=$(/usr/bin/python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c.get('nodes',{}).get('mac1',{}).get('lan_ip','') or c.get('nodes',{}).get('mac1',{}).get('host',''))" 2>/dev/null || echo "")
MAC1_TS=$(/usr/bin/python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c.get('nodes',{}).get('mac1',{}).get('tailscale_ip',''))" 2>/dev/null || echo "")
MAC1_USER=$(/usr/bin/python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c.get('nodes',{}).get('mac1',{}).get('user','aarontjomsland'))" 2>/dev/null || echo "aarontjomsland")

# ── Reachability probe ──
probe_ssh() {
    local ip="$1"
    [[ -z "$ip" ]] && return 1
    ssh -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        "${MAC1_USER}@${ip}" "echo ok" 2>/dev/null | grep -q "^ok$"
}

REACHABLE_IP=""
if probe_ssh "$MAC1_LAN"; then
    REACHABLE_IP="$MAC1_LAN"
    log "mac1 reachable via LAN: $MAC1_LAN"
elif probe_ssh "$MAC1_TS"; then
    REACHABLE_IP="$MAC1_TS"
    log "mac1 reachable via Tailscale: $MAC1_TS"
else
    log "mac1 unreachable (LAN=$MAC1_LAN, TS=${MAC1_TS:-<none>}), skip"
    state_set last_state "unreachable"
    exit 0
fi

# ── Pre-flight: check if active sessions — skip restart if busy ──
SKIP_RESTART=false
HEALTH=$(curl -s --max-time 5 "http://${REACHABLE_IP}:8855/health" 2>/dev/null || echo "")
if [[ -n "$HEALTH" ]]; then
    ACTIVE=$(echo "$HEALTH" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('activeSessions', 0))" 2>/dev/null || echo 0)
    if [[ "${ACTIVE:-0}" -gt 0 ]]; then
        log "mac1 has ${ACTIVE} active session(s), will skip daemon restart"
        SKIP_RESTART=true
    fi
fi

# ── Wire-up sequence ──
log "starting wire-up via ${REACHABLE_IP}"

WIRE_CMD=$(cat <<REMOTE
set -e
cd ~/dev/er-simulator-superrepo 2>/dev/null || cd ~/Documents/er-simulator-superrepo 2>/dev/null || { echo "NO_REPO"; exit 1; }
git fetch origin main 2>&1 | tail -5 || true
git pull --no-rebase --no-edit origin main 2>&1 | tail -5 || true
ROUTES=\$(curl -s --max-time 3 http://127.0.0.1:8222/routez 2>/dev/null | /usr/bin/python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('server_name',''))" 2>/dev/null || echo "")
if [ "\$ROUTES" != "mac1" ]; then
  echo "CLUSTERING: enabling..."
  MULTIFLEET_NODE_ID=mac1 bash multi-fleet/scripts/enable-clustering.sh 2>&1 | tail -10 || true
else
  echo "CLUSTERING: already configured (server_name=mac1)"
fi
[ -x scripts/verify-git-clean.sh ] && bash scripts/verify-git-clean.sh --quiet 2>/dev/null || true
if [ "${SKIP_RESTART}" = "false" ]; then
  launchctl kickstart -k "gui/\$(id -u)/io.contextdna.fleet-nats" 2>/dev/null || true
  echo "DAEMON: kickstarted"
else
  echo "DAEMON: restart skipped (active sessions)"
fi
echo "WIRE_UP_OK: \$(hostname -s)"
REMOTE
)

WIRE_OUT=$(ssh -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    -o ServerAliveInterval=10 \
    -o ServerAliveCountMax=3 \
    "${MAC1_USER}@${REACHABLE_IP}" \
    "timeout ${SSH_TOTAL_TIMEOUT} bash -s" <<< "$WIRE_CMD" 2>&1)
WIRE_EXIT=$?

log "wire-up output: $WIRE_OUT"

if [[ "$WIRE_EXIT" -ne 0 ]] || ! echo "$WIRE_OUT" | grep -q "WIRE_UP_OK:"; then
    log "wire-up FAILED (exit=$WIRE_EXIT)"
    state_set last_state "wire_up_failed" last_error "exit=$WIRE_EXIT"
    exit 0  # Not a hard error — fleet-check doesn't fail
fi

# ── Verification (poll /health for 30s with exp backoff) ──
log "verifying via http://${REACHABLE_IP}:8855/health"
VERIFIED=false
BACKOFF=2
ELAPSED=0
PEER_COUNT=0

while [[ "$ELAPSED" -lt "$VERIFY_TIMEOUT_SECS" ]]; do
    H=$(curl -s --max-time 3 "http://${REACHABLE_IP}:8855/health" 2>/dev/null || echo "")
    if [[ -n "$H" ]]; then
        PEER_COUNT=$(/usr/bin/python3 - "$H" <<'PY' 2>/dev/null || echo 0
import json, sys
try:
    d = json.loads(sys.argv[1])
    peers = d.get("peers") or d.get("peerList") or []
    if isinstance(peers, dict):
        peers = list(peers.keys())
    if isinstance(peers, list):
        print(len([p for p in peers if p and p != "mac1"]))
    else:
        print(0)
except Exception:
    print(0)
PY
)
        if [[ "${PEER_COUNT:-0}" -ge 1 ]]; then
            VERIFIED=true
            break
        fi
    fi
    sleep "$BACKOFF"
    ELAPSED=$(( ELAPSED + BACKOFF ))
    BACKOFF=$(( BACKOFF * 2 ))
    [[ "$BACKOFF" -gt 10 ]] && BACKOFF=10
done

if $VERIFIED; then
    log "VERIFIED: mac1 sees ${PEER_COUNT} peer(s) after ${ELAPSED}s"
    state_set last_success_ts "$(date +%s)" last_state "verified" peer_count "$PEER_COUNT" via_ip "$REACHABLE_IP"

    # Emit ack message
    mkdir -p "$FLEET_MSG_DIR" 2>/dev/null || true
    cat > "${FLEET_MSG_DIR}/auto-wire-ack.md" <<EOF
# Auto-Wire ACK
From: mac2
To: mac1
Time: $(date -u '+%Y-%m-%dT%H:%M:%SZ')
Via: ${REACHABLE_IP}
Peers visible: ${PEER_COUNT}
Verification elapsed: ${ELAPSED}s

mac1 wire-up completed and verified. Cluster membership active.
EOF
    log "ack emitted → ${FLEET_MSG_DIR}/auto-wire-ack.md"
else
    log "PARTIAL: wire-up ran but verification timed out (peers=${PEER_COUNT})"
    state_set last_state "partial" peer_count "$PEER_COUNT" via_ip "$REACHABLE_IP"
fi

exit 0
