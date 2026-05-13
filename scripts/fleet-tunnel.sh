#!/usr/bin/env bash
# fleet-tunnel.sh — Persistent SSH tunnels for fleet NATS/HTTP connectivity
#
# Solves: Router/firewall blocks non-SSH ports between machines on the same LAN.
# Solution: Each worker tunnels NATS (4222) and HTTP (8855) through SSH to chief.
# Chief tunnels back to workers via reverse ports for HTTP access.
#
# All node discovery is config-driven — supports 1 to 100+ nodes.
# Nodes and IPs are read from .multifleet/config.json.
#
# Usage:
#   fleet-tunnel.sh setup          # Generate SSH keys + test connectivity
#   fleet-tunnel.sh start          # Start tunnels for this node's role
#   fleet-tunnel.sh stop           # Stop all tunnels
#   fleet-tunnel.sh status         # Show tunnel state
#   fleet-tunnel.sh install        # Install as LaunchAgent (persistent)
#   fleet-tunnel.sh uninstall      # Remove LaunchAgent
#
# Requires: autossh (brew install autossh), SSH key auth between nodes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="$REPO_ROOT/.multifleet/config.json"
TUNNEL_DIR="$HOME/.fleet-tunnels"
LOG_DIR="/tmp"
PLIST_DIR="$HOME/Library/LaunchAgents"

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"

mkdir -p "$TUNNEL_DIR"

_log() { echo "[fleet-tunnel] $(date '+%H:%M:%S') $*"; }

# ── Config-driven node discovery ──

_get_node_ip() {
    # Returns best reachable IP: LAN first, tailscale fallback
    local lan_ip tailscale_ip
    lan_ip=$(python3 -c "
import json
cfg = json.load(open('$CONFIG'))
node = cfg.get('nodes', {}).get('$1', {})
print(node.get('host', ''))
" 2>/dev/null)
    tailscale_ip=$(python3 -c "
import json
cfg = json.load(open('$CONFIG'))
node = cfg.get('nodes', {}).get('$1', {})
print(node.get('tailscale_ip', ''))
" 2>/dev/null)
    # Try LAN first (1s ping), fall back to tailscale
    if [ -n "$lan_ip" ] && ping -c 1 -W 1 "$lan_ip" >/dev/null 2>&1; then
        echo "$lan_ip"
    elif [ -n "$tailscale_ip" ] && ping -c 1 -W 1 "$tailscale_ip" >/dev/null 2>&1; then
        _log "Using tailscale IP for $1 (LAN unreachable)"
        echo "$tailscale_ip"
    elif [ -n "$lan_ip" ]; then
        echo "$lan_ip"  # return LAN anyway, let SSH timeout handle it
    elif [ -n "$tailscale_ip" ]; then
        echo "$tailscale_ip"
    fi
}

_get_chief_id() {
    python3 -c "
import json
cfg = json.load(open('$CONFIG'))
print(cfg.get('chief', {}).get('nodeId', ''))
" 2>/dev/null
}

_get_all_node_ids() {
    # Returns all node IDs from config, one per line
    python3 -c "
import json
cfg = json.load(open('$CONFIG'))
for nid in sorted(cfg.get('nodes', {}).keys()):
    print(nid)
" 2>/dev/null
}

_get_peer_node_ids() {
    # All nodes except self
    python3 -c "
import json
cfg = json.load(open('$CONFIG'))
for nid in sorted(cfg.get('nodes', {}).keys()):
    if nid != '$NODE_ID':
        print(nid)
" 2>/dev/null
}

_get_worker_node_ids() {
    # All non-chief nodes
    local chief_id="$(_get_chief_id)"
    python3 -c "
import json
cfg = json.load(open('$CONFIG'))
for nid in sorted(cfg.get('nodes', {}).keys()):
    if nid != '$chief_id':
        print(nid)
" 2>/dev/null
}

_get_node_role() {
    python3 -c "
import json
cfg = json.load(open('$CONFIG'))
print(cfg.get('nodes', {}).get('$1', {}).get('role', 'worker'))
" 2>/dev/null
}

_get_node_user() {
    # Per-node user from config, or fallback to env/current user
    python3 -c "
import json
cfg = json.load(open('$CONFIG'))
node = cfg.get('nodes', {}).get('$1', {})
print(node.get('user', ''))
" 2>/dev/null | grep -v '^$' || echo "${FLEET_SSH_USER:-$(whoami)}"
}

_is_chief() {
    [ "$NODE_ID" = "$(_get_chief_id)" ]
}

# ─�� Dynamic reverse port assignment ──
# Chief needs a unique local port per worker to reach their HTTP (8855).
# Formula: base_port + deterministic offset from node name hash.
# This avoids hardcoding and scales to 100+ nodes.

REVERSE_PORT_BASE=18855

_reverse_port_for_node() {
    # Read tunnel_port from config.json first (authoritative).
    # Fall back to deterministic hash if not set in config.
    local node_id="$1"
    python3 -c "
import json, hashlib
cfg = json.load(open('$CONFIG'))
tp = cfg.get('nodes', {}).get('$node_id', {}).get('tunnel_port')
if tp:
    print(tp)
else:
    h = int(hashlib.md5('$node_id'.encode()).hexdigest()[:5], 16) % 10000
    print($REVERSE_PORT_BASE + h)
" 2>/dev/null
}

# ── Tunnel management ──

CHIEF_NATS_PORT=4222
CHIEF_HTTP_PORT=8855
CHIEF_INGEST_PORT=8844

_tunnel_pid_file() { echo "$TUNNEL_DIR/tunnel-${1}.pid"; }

_is_tunnel_alive() {
    local pidfile="$(_tunnel_pid_file "$1")"
    [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

# ── Commands ──

cmd_setup() {
    _log "Setting up SSH key auth for fleet tunnels..."

    local keyfile="$HOME/.ssh/fleet_tunnel_ed25519"
    if [ ! -f "$keyfile" ]; then
        ssh-keygen -t ed25519 -f "$keyfile" -N "" -C "fleet-tunnel-${NODE_ID}"
        _log "Generated tunnel key: $keyfile"
    else
        _log "Tunnel key exists: $keyfile"
    fi

    _log ""
    _log "Copy this public key to each peer's ~/.ssh/authorized_keys:"
    _log "───────────────────────────────────────────────────────────"
    cat "${keyfile}.pub"
    _log "───────────────────────────────────────────────────────────"
    _log ""

    # Test connectivity to all peers from config
    local peers
    peers="$(_get_peer_node_ids)"
    for node in $peers; do
        local ip="$(_get_node_ip "$node")"
        local user="$(_get_node_user "$node")"
        if [ -z "$ip" ]; then
            _log "SKIP $node — no IP in config"
            continue
        fi
        _log "Testing SSH to $node ($ip)..."
        if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
               -i "$keyfile" "${user}@${ip}" "echo OK" 2>/dev/null; then
            _log "  ✓ $node SSH works"
        else
            _log "  ✗ $node SSH failed — copy the key to ${user}@${ip}"
        fi
    done
}

cmd_start() {
    _log "Starting tunnels for node=$NODE_ID ($([ "$(_is_chief && echo chief)" ] && echo chief || echo worker))"

    local keyfile="$HOME/.ssh/fleet_tunnel_ed25519"
    [ ! -f "$keyfile" ] && keyfile="$HOME/.ssh/id_ed25519"
    [ ! -f "$keyfile" ] && keyfile="$HOME/.ssh/id_rsa"

    if _is_chief; then
        _start_chief_tunnels "$keyfile"
    else
        _start_worker_tunnel "$keyfile"
        _start_peer_tunnels "$keyfile"
    fi
}

_start_worker_tunnel() {
    local keyfile="$1"
    local chief_id="$(_get_chief_id)"
    local chief_ip="$(_get_node_ip "$chief_id")"
    local chief_user="$(_get_node_user "$chief_id")"
    local reverse_port="$(_reverse_port_for_node "$NODE_ID")"

    if [ -z "$chief_ip" ]; then
        _log "✗ Chief IP not found in config"
        return 1
    fi

    if _is_tunnel_alive "to-chief"; then
        _log "Tunnel to chief already running (PID $(cat "$(_tunnel_pid_file "to-chief")"))"
        return 0
    fi

    _log "Connecting to chief $chief_id ($chief_ip) — tunneling NATS+Ingest, reverse HTTP on :$reverse_port"

    AUTOSSH_PIDFILE="$(_tunnel_pid_file "to-chief")" \
    AUTOSSH_GATETIME=0 \
    AUTOSSH_LOGFILE="$LOG_DIR/fleet-tunnel-to-chief.log" \
    autossh -M 0 -f -N \
        -o "ServerAliveInterval=15" \
        -o "ServerAliveCountMax=3" \
        -o "ExitOnForwardFailure=yes" \
        -o "StrictHostKeyChecking=no" \
        -o "BatchMode=yes" \
        -i "$keyfile" \
        -L "127.0.0.1:${CHIEF_NATS_PORT}:127.0.0.1:${CHIEF_NATS_PORT}" \
        -L "127.0.0.1:${CHIEF_INGEST_PORT}:127.0.0.1:${CHIEF_INGEST_PORT}" \
        -R "${reverse_port}:127.0.0.1:${CHIEF_HTTP_PORT}" \
        "${chief_user}@${chief_ip}"

    sleep 2
    if _is_tunnel_alive "to-chief"; then
        _log "✓ Tunnel to chief UP"
        _log "  Local :${CHIEF_NATS_PORT} → chief NATS"
        _log "  Local :${CHIEF_INGEST_PORT} → chief ingest"
        _log "  Chief :${reverse_port} → our :${CHIEF_HTTP_PORT}"
    else
        _log "✗ Tunnel to chief FAILED — check $LOG_DIR/fleet-tunnel-to-chief.log"
        return 1
    fi
}

_start_peer_tunnels() {
    # Worker-to-worker tunnels via chief as SSH jump host.
    # Needed because direct LAN may be blocked (firewall/router).
    # Tunnel: local:tunnel_port → ssh(chief) → ssh(peer) → peer:8855
    local keyfile="$1"
    local chief_id="$(_get_chief_id)"
    local chief_ip="$(_get_node_ip "$chief_id")"
    local chief_user="$(_get_node_user "$chief_id")"

    local peers
    peers="$(_get_worker_node_ids)"
    for node in $peers; do
        [ "$node" = "$NODE_ID" ] && continue
        local ip="$(_get_node_ip "$node")"
        local user="$(_get_node_user "$node")"
        local tunnel_port
        tunnel_port=$(python3 -c "
import json
cfg = json.load(open('$CONFIG'))
print(cfg.get('nodes', {}).get('$node', {}).get('tunnel_port', ''))
" 2>/dev/null)
        [ -z "$tunnel_port" ] && continue
        [ -z "$ip" ] && continue

        if _is_tunnel_alive "peer-${node}"; then
            _log "Peer tunnel to $node already running"
            continue
        fi

        _log "Peer tunnel to $node ($ip) via chief — local :${tunnel_port} → ${node}:${CHIEF_HTTP_PORT}"

        AUTOSSH_PIDFILE="$(_tunnel_pid_file "peer-${node}")" \
        AUTOSSH_GATETIME=0 \
        AUTOSSH_LOGFILE="$LOG_DIR/fleet-tunnel-peer-${node}.log" \
        autossh -M 0 -f -N \
            -o "ServerAliveInterval=15" \
            -o "ServerAliveCountMax=3" \
            -o "StrictHostKeyChecking=no" \
            -o "BatchMode=yes" \
            -i "$keyfile" \
            -L "${tunnel_port}:127.0.0.1:${CHIEF_HTTP_PORT}" \
            -J "${chief_user}@${chief_ip}" \
            "${user}@${ip}" 2>/dev/null || {
                _log "✗ Peer $node unreachable via jump — will retry on next start"
                continue
            }

        sleep 2
        if _is_tunnel_alive "peer-${node}"; then
            _log "✓ Peer tunnel to $node UP — :${tunnel_port} → ${node}:${CHIEF_HTTP_PORT}"
        else
            _log "✗ Peer tunnel to $node FAILED"
        fi
    done
}

_start_chief_tunnels() {
    local keyfile="$1"

    # Chief establishes forward tunnels to each worker for HTTP access
    local workers
    workers="$(_get_worker_node_ids)"
    for node in $workers; do
        local ip="$(_get_node_ip "$node")"
        local user="$(_get_node_user "$node")"
        local reverse_port="$(_reverse_port_for_node "$node")"
        [ -z "$ip" ] && continue

        if _is_tunnel_alive "to-${node}"; then
            _log "Tunnel to $node already running"
            continue
        fi

        _log "Connecting to $node ($ip) — forward tunnel for HTTP on :$reverse_port"

        AUTOSSH_PIDFILE="$(_tunnel_pid_file "to-${node}")" \
        AUTOSSH_GATETIME=0 \
        AUTOSSH_LOGFILE="$LOG_DIR/fleet-tunnel-to-${node}.log" \
        autossh -M 0 -f -N \
            -o "ServerAliveInterval=15" \
            -o "ServerAliveCountMax=3" \
            -o "StrictHostKeyChecking=no" \
            -o "BatchMode=yes" \
            -i "$keyfile" \
            -L "${reverse_port}:127.0.0.1:${CHIEF_HTTP_PORT}" \
            "${user}@${ip}" 2>/dev/null || {
                _log "✗ $node unreachable — will retry on next start"
                continue
            }

        sleep 2
        if _is_tunnel_alive "to-${node}"; then
            _log "✓ Tunnel to $node UP — their :${CHIEF_HTTP_PORT} on our :${reverse_port}"
        else
            _log "✗ Tunnel to $node FAILED"
        fi
    done
}

cmd_stop() {
    _log "Stopping all tunnels..."
    for pidfile in "$TUNNEL_DIR"/tunnel-*.pid; do
        [ -f "$pidfile" ] || continue
        local pid="$(cat "$pidfile")"
        local name="$(basename "$pidfile" .pid)"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            _log "  Stopped $name (PID $pid)"
        fi
        rm -f "$pidfile"
    done
    pkill -f "autossh.*fleet" 2>/dev/null || true
    _log "All tunnels stopped"
}

cmd_status() {
    echo "=== Fleet Tunnel Status (node=$NODE_ID) ==="
    echo ""

    local any_alive=false
    for pidfile in "$TUNNEL_DIR"/tunnel-*.pid; do
        [ -f "$pidfile" ] || continue
        local pid="$(cat "$pidfile")"
        local name="$(basename "$pidfile" .pid | sed 's/tunnel-//')"
        if kill -0 "$pid" 2>/dev/null; then
            echo "  ✓ $name — PID $pid (alive)"
            any_alive=true
        else
            echo "  ✗ $name — PID $pid (dead)"
        fi
    done

    if [ "$any_alive" = false ]; then
        echo "  No active tunnels"
    fi

    echo ""
    echo "=== Port Forwarding ==="
    if _is_chief; then
        local workers
        workers="$(_get_worker_node_ids)"
        for node in $workers; do
            local rp="$(_reverse_port_for_node "$node")"
            echo "  127.0.0.1:${rp} → ${node}:${CHIEF_HTTP_PORT} (HTTP)"
        done
    else
        local chief_id="$(_get_chief_id)"
        echo "  127.0.0.1:${CHIEF_NATS_PORT}   → ${chief_id}:${CHIEF_NATS_PORT}   (NATS)"
        echo "  127.0.0.1:${CHIEF_INGEST_PORT}  → ${chief_id}:${CHIEF_INGEST_PORT}  (Chief Ingest)"
        local rp="$(_reverse_port_for_node "$NODE_ID")"
        echo "  ${chief_id}:${rp} → our:${CHIEF_HTTP_PORT} (reverse HTTP)"
    fi

    echo ""
    echo "=== Connectivity Test ==="
    if nc -zv -w 2 127.0.0.1 ${CHIEF_NATS_PORT} 2>&1 | grep -q succeeded; then
        echo "  ✓ NATS (:${CHIEF_NATS_PORT}) reachable"
    else
        echo "  ✗ NATS (:${CHIEF_NATS_PORT}) unreachable"
    fi
    if nc -zv -w 2 127.0.0.1 ${CHIEF_HTTP_PORT} 2>&1 | grep -q succeeded; then
        echo "  ✓ HTTP (:${CHIEF_HTTP_PORT}) reachable"
    else
        echo "  ✗ HTTP (:${CHIEF_HTTP_PORT}) unreachable"
    fi

    echo ""
    echo "=== Fleet Nodes (from config) ==="
    local all_nodes
    all_nodes="$(_get_all_node_ids)"
    for node in $all_nodes; do
        local ip="$(_get_node_ip "$node")"
        local role="$(_get_node_role "$node")"
        local rp="$(_reverse_port_for_node "$node")"
        local marker=""
        [ "$node" = "$NODE_ID" ] && marker=" (this node)"
        echo "  $node — $ip ($role) tunnel-port:$rp$marker"
    done
}

cmd_install() {
    local plist="$PLIST_DIR/io.multifleet.tunnel.plist"

    cat > "$plist" <<PEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.multifleet.tunnel</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SCRIPT_DIR}/fleet-tunnel.sh</string>
        <string>start</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MULTIFLEET_NODE_ID</key>
        <string>${NODE_ID}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <key>NetworkState</key><true/>
    </dict>
    <key>StartInterval</key><integer>120</integer>
    <key>StandardOutPath</key><string>/tmp/fleet-tunnel.log</string>
    <key>StandardErrorPath</key><string>/tmp/fleet-tunnel.log</string>
</dict>
</plist>
PEOF

    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
    _log "LaunchAgent installed — tunnels start on boot + 120s health check"
}

cmd_uninstall() {
    local plist="$PLIST_DIR/io.multifleet.tunnel.plist"
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    cmd_stop
    _log "LaunchAgent removed"
}

# ── Main ──
case "${1:-status}" in
    setup)     cmd_setup ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    status)    cmd_status ;;
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    *)         echo "Usage: fleet-tunnel.sh {setup|start|stop|status|install|uninstall}"; exit 1 ;;
esac
