#!/usr/bin/env bash
# fleet-keepalive.sh — keep fleet nodes awake for remote operation
#
# Power states: asleep → waking → warm-idle → session-hot → busy-protected → cooldown → parking
#
# Usage:
#   bash scripts/fleet-keepalive.sh start     # Assert keepalive on this node
#   bash scripts/fleet-keepalive.sh stop      # Release keepalive
#   bash scripts/fleet-keepalive.sh status    # Check power state
#   bash scripts/fleet-keepalive.sh wake <node>  # Wake a remote node
#   bash scripts/fleet-keepalive.sh all-start # Assert keepalive on all fleet nodes via SSH

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="/tmp/fleet-keepalive.pid"
STATE_FILE="/tmp/fleet-power-state"
LOG_FILE="/tmp/fleet-keepalive.log"

# Load fleet config for peer info
CONFIG="$REPO_ROOT/.multifleet/config.json"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

# ── Power state management ──

get_state() {
    if [ -f "$STATE_FILE" ]; then
        cat "$STATE_FILE"
    else
        echo "unknown"
    fi
}

set_state() {
    echo "$1" > "$STATE_FILE"
    log "Power state → $1"
}

# ── Keepalive ──

start_keepalive() {
    # Check if already running
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        log "Keepalive already active (PID $(cat "$PID_FILE"))"
        set_state "busy-protected"
        return 0
    fi

    # macOS: caffeinate prevents sleep (system + display idle)
    # -s: prevent system sleep
    # -i: prevent idle sleep
    # -d: prevent display sleep (optional, may want display off)
    caffeinate -s -i &
    echo $! > "$PID_FILE"
    set_state "busy-protected"
    log "Keepalive started (PID $!, caffeinate -s -i)"

    # Also ensure pmset sleep is 0
    if pmset -g 2>/dev/null | grep -q "sleep.*[1-9]"; then
        log "WARNING: System sleep is not 0. Run: sudo pmset -a sleep 0"
    fi

    # Ensure WoL is enabled
    if ! pmset -g 2>/dev/null | grep -q "womp.*1"; then
        log "WARNING: Wake-on-LAN not enabled. Run: sudo pmset -a womp 1"
    fi

    echo "Keepalive active. This node will not sleep."
    echo "State: busy-protected | PID: $(cat "$PID_FILE")"
}

stop_keepalive() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            log "Keepalive stopped (PID $pid)"
        fi
        rm -f "$PID_FILE"
    fi
    set_state "warm-idle"
    echo "Keepalive released. Node may sleep per system settings."
}

# ── Wake remote node ──

wake_node() {
    local target="$1"

    if [ ! -f "$CONFIG" ]; then
        echo "ERROR: No fleet config at $CONFIG"
        return 1
    fi

    # Get MAC address from config
    local mac_addr
    mac_addr=$(python3 -c "
import json
cfg = json.loads(open('$CONFIG').read())
node = cfg.get('nodes', {}).get('$target', {})
print(node.get('mac_address', ''))
" 2>/dev/null)

    if [ -z "$mac_addr" ]; then
        echo "ERROR: No MAC address for $target in config"
        return 1
    fi

    local ip
    ip=$(python3 -c "
import json
cfg = json.loads(open('$CONFIG').read())
print(cfg.get('nodes', {}).get('$target', {}).get('host', ''))
" 2>/dev/null)

    log "Waking $target (MAC: $mac_addr, IP: $ip)"

    # Method 1: wakeonlan command (if installed)
    if command -v wakeonlan &>/dev/null; then
        wakeonlan "$mac_addr" && log "WoL sent via wakeonlan"
    fi

    # Method 2: Python WoL magic packet
    python3 -c "
import socket, struct
mac = '$mac_addr'.replace(':', '').replace('-', '')
data = b'\\xff' * 6 + bytes.fromhex(mac) * 16
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.sendto(data, ('255.255.255.255', 9))
sock.sendto(data, ('255.255.255.255', 7))
print('Magic packet sent')
sock.close()
" 2>/dev/null && log "WoL magic packet sent"

    # Method 3: Tailscale wake (if available and target on tailnet)
    if command -v tailscale &>/dev/null; then
        local ts_status
        ts_status=$(tailscale status 2>/dev/null | grep -i "$target" || true)
        if [ -n "$ts_status" ]; then
            log "Tailscale: $target found on tailnet"
        fi
    fi

    # Wait and check if node came up
    echo "Waiting for $target to respond..."
    for i in 1 2 3 4 5; do
        sleep 5
        if ping -c 1 -W 2 "$ip" &>/dev/null; then
            log "$target is UP after ${i}x5s"
            set_state "waking" # Remote node waking
            echo "$target is awake!"
            return 0
        fi
    done

    echo "WARNING: $target did not respond after 25s. May need manual intervention."
    return 1
}

# ── Start keepalive on all fleet nodes ──

all_start() {
    if [ ! -f "$CONFIG" ]; then
        echo "ERROR: No fleet config"
        return 1
    fi

    local node_id
    node_id="${MULTIFLEET_NODE_ID:-$(hostname -s)}"

    # Start locally
    start_keepalive

    # SSH to each peer and start keepalive
    python3 -c "
import json
cfg = json.loads(open('$CONFIG').read())
for nid, node in cfg.get('nodes', {}).items():
    if nid != '$node_id':
        print(f\"{node.get('user', '')}@{node.get('host', '')} {nid}\")
" 2>/dev/null | while read -r ssh_target nid; do
        if [ -n "$ssh_target" ] && [ "$ssh_target" != "@" ]; then
            echo "Starting keepalive on $nid ($ssh_target)..."
            ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$ssh_target" \
                "caffeinate -s -i & echo \$! > /tmp/fleet-keepalive.pid; echo busy-protected > /tmp/fleet-power-state" \
                2>/dev/null && echo "  $nid: keepalive started" || echo "  $nid: SSH failed"
        fi
    done
}

# ── Status ──

show_status() {
    echo "=== Fleet Keepalive Status ==="
    echo "Node: ${MULTIFLEET_NODE_ID:-$(hostname -s)}"
    echo "State: $(get_state)"

    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Keepalive: ACTIVE (PID $pid)"
        else
            echo "Keepalive: STALE PID (process dead)"
        fi
    else
        echo "Keepalive: INACTIVE"
    fi

    # System sleep settings
    local sleep_val
    sleep_val=$(pmset -g 2>/dev/null | grep "^ sleep" | awk '{print $2}')
    echo "System sleep: ${sleep_val:-unknown} (0=never)"

    local womp
    womp=$(pmset -g 2>/dev/null | grep "womp" | awk '{print $2}')
    echo "Wake-on-LAN: ${womp:-unknown} (1=enabled)"

    # Display sleep
    local disp
    disp=$(pmset -g 2>/dev/null | grep "displaysleep" | awk '{print $2}')
    echo "Display sleep: ${disp:-unknown} min"

    # Tailscale
    if command -v tailscale &>/dev/null; then
        local ts
        ts=$(tailscale status 2>/dev/null | head -1)
        echo "Tailscale: ${ts:-not running}"
    else
        echo "Tailscale: not installed"
    fi

    # Check peer states
    if [ -f "$CONFIG" ]; then
        echo ""
        echo "=== Peer Power States ==="
        python3 -c "
import json
cfg = json.loads(open('$CONFIG').read())
for nid, node in cfg.get('nodes', {}).items():
    print(f\"  {nid}: {node.get('host', '?')} (role: {node.get('role', '?')})\")" 2>/dev/null
    fi
}

# ── Main ──

case "${1:-status}" in
    start)
        start_keepalive
        ;;
    stop)
        stop_keepalive
        ;;
    status)
        show_status
        ;;
    wake)
        if [ -z "${2:-}" ]; then
            echo "Usage: fleet-keepalive.sh wake <node>"
            exit 1
        fi
        wake_node "$2"
        ;;
    all-start)
        all_start
        ;;
    *)
        echo "Usage: fleet-keepalive.sh {start|stop|status|wake <node>|all-start}"
        exit 1
        ;;
esac
