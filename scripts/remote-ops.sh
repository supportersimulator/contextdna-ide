#!/usr/bin/env bash
# remote-ops.sh — UNIFIED remote operation script for Multi-Fleet
#
# Consolidates: trip-mode.sh, activate-full-remote.sh, fleet-keepalive.sh,
#               fleet-keepawake.sh (best features from each)
# Separate: ghost-checkpoint.sh (hook-triggered, different lifecycle)
#           model-switch-setup.sh (install utility, not runtime)
#
# Usage:
#   fleet remote start [hours]   # Lock fleet for remote operation (default: 72h)
#   fleet remote stop            # Release remote mode
#   fleet remote status          # Full readiness check
#   fleet remote wake <node>     # Wake a sleeping fleet node
#   fleet remote all-start       # Bootstrap keepalive on ALL fleet nodes via SSH
#   fleet remote check           # Audit power/network settings (read-only)
#
# Portable: reads all config from .multifleet/config.json — zero hardcoded IPs/nodes.
# Secrets: API keys retrieved from macOS Keychain or env vars only — never stored in files.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$REPO_ROOT/.multifleet/config.json"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
PID_FILE="/tmp/fleet-remote-${NODE_ID}.pid"
STATE_FILE="/tmp/fleet-power-state"

# ── Colors ──
G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; B='\033[1;34m'; NC='\033[0m'
ok()   { echo -e "  ${G}✓${NC} $*"; }
warn() { echo -e "  ${Y}⚠${NC} $*"; }
fail() { echo -e "  ${R}✗${NC} $*"; }
hdr()  { echo -e "\n${B}━━━ $* ━━━${NC}"; }

# ── Config helpers (no hardcoded IPs) ──

_get_chief_host() {
    [ -f "$CONFIG" ] && python3 -c "
import json; cfg=json.loads(open('$CONFIG').read())
print(cfg.get('chief',{}).get('host',''))" 2>/dev/null || echo ""
}

_get_node_info() {
    local node="$1"
    [ -f "$CONFIG" ] && python3 -c "
import json; cfg=json.loads(open('$CONFIG').read())
n=cfg.get('nodes',{}).get('$node',{})
print(f\"{n.get('user','unknown')}|{n.get('host','')}|{n.get('mac_address','')}|{n.get('role','worker')}\")" 2>/dev/null || echo "|||"
}

_get_nats_url() {
    local chief
    chief=$(_get_chief_host)
    if [ -n "$chief" ]; then
        echo "nats://${chief}:4222"
    else
        echo "nats://127.0.0.1:4222"
    fi
}

_get_api_key() {
    # Retrieve API key from: env var → macOS Keychain → empty
    # NEVER stores keys in files
    local key_name="$1"
    local env_val="${!key_name:-}"
    if [ -n "$env_val" ]; then
        echo "$env_val"
        return
    fi
    # Try macOS Keychain
    if command -v security &>/dev/null; then
        security find-generic-password -s fleet-nerve -a "$key_name" -w 2>/dev/null || echo ""
    else
        echo ""
    fi
}

set_state() { echo "$1" > "$STATE_FILE"; }
get_state() { cat "$STATE_FILE" 2>/dev/null || echo "unknown"; }

# ══════════════════════════════════════════════════════════
# START — lock fleet for remote operation
# ══════════════════════════════════════════════════════════

cmd_start() {
    local hours="${1:-72}"
    local secs=$((hours * 3600))

    echo "═══════════════════════════════════════════════════"
    echo " REMOTE OPS — Locking $NODE_ID for ${hours}h"
    echo "═══════════════════════════════════════════════════"

    # 1. Keepalive
    hdr "1. Keepalive"
    [ -f "$PID_FILE" ] && kill "$(cat "$PID_FILE")" 2>/dev/null || true
    caffeinate -s -i -t "$secs" &
    echo $! > "$PID_FILE"
    set_state "busy-protected"
    ok "caffeinate active for ${hours}h (PID $!)"

    # 2. Fleet daemon
    hdr "2. Fleet Daemon"
    if curl -sf http://127.0.0.1:8855/health >/dev/null 2>&1; then
        ok "Already running"
    else
        local nats_url
        nats_url=$(_get_nats_url)
        NATS_URL="$nats_url" MULTIFLEET_NODE_ID="$NODE_ID" \
            python3 "$REPO_ROOT/tools/fleet_nerve_nats.py" serve &>/tmp/fleet-daemon-${NODE_ID}.log &
        sleep 3
        if curl -sf http://127.0.0.1:8855/health >/dev/null 2>&1; then
            ok "Started (NATS: $nats_url)"
        else
            warn "Failed to start (check /tmp/fleet-daemon-${NODE_ID}.log)"
        fi
    fi

    # 3. API failover router
    hdr "3. API Failover"
    if command -v omniroute &>/dev/null; then
        if pgrep -f "omniroute" >/dev/null 2>&1; then
            ok "OmniRoute running on :20128"
        else
            omniroute --no-open &>/tmp/omniroute.log &
            sleep 2
            ok "OmniRoute started on :20128 (auto-failover: Anthropic→DeepSeek→OpenRouter)"
        fi
        echo "       Dashboard: http://localhost:20128"
    elif command -v claude-code-router &>/dev/null; then
        if curl -sf http://127.0.0.1:3456/health >/dev/null 2>&1; then
            ok "claude-code-router running on :3456"
        else
            local dk
            dk=$(_get_api_key "DEEPSEEK_API_KEY")
            if [ -n "$dk" ]; then
                DEEPSEEK_API_KEY="$dk" claude-code-router start --background 2>/dev/null &
                sleep 2
                ok "claude-code-router started (Anthropic→DeepSeek)"
            else
                warn "No DeepSeek key. Set DEEPSEEK_API_KEY env or add to Keychain"
            fi
        fi
    else
        warn "No router installed. Run: npm install -g omniroute"
    fi

    # 4. RTK token saver
    hdr "4. RTK Token Saver"
    if command -v rtk &>/dev/null; then
        ok "RTK $(rtk --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+'): active"
    else
        warn "Not installed (brew install rtk && rtk init -g)"
    fi

    # 5. Tailscale
    hdr "5. Tailscale"
    if command -v tailscale &>/dev/null; then
        local ts
        ts=$(tailscale status 2>&1 | head -1)
        if echo "$ts" | grep -qi "stopped\|logged out"; then
            warn "Installed but stopped — run: tailscale up"
        else
            ok "Tailscale: $ts"
        fi
    else
        warn "Not installed (brew install tailscale)"
    fi

    # 6. NATS server
    hdr "6. NATS Server"
    if pgrep -f "nats-server" >/dev/null 2>&1; then
        ok "Running"
    else
        warn "Not running — start with: fleet nats-cluster start"
    fi

    # 7. Ghost checkpoint
    hdr "7. Ghost Checkpoint"
    if [ -x "$REPO_ROOT/scripts/ghost-checkpoint.sh" ]; then
        bash "$REPO_ROOT/scripts/ghost-checkpoint.sh" 2>/dev/null
        ok "Checkpoint saved to ~/.claude/ghost/"
    else
        warn "ghost-checkpoint.sh not found"
    fi

    # Summary
    echo ""
    echo "═══════════════════════════════════════════════════"
    echo " REMOTE ACCESS"
    echo "═══════════════════════════════════════════════════"
    echo ""
    echo "  Claude Code:  https://claude.ai/code"
    echo "  Fleet check:  bash scripts/fleet-check.sh"
    echo "  Wake peer:    bash scripts/remote-ops.sh wake mac2"
    echo "  Deactivate:   bash scripts/remote-ops.sh stop"
    echo ""
    echo "═══════════════════════════════════════════════════"
    echo " REMOTE OPS ACTIVE — $NODE_ID locked for ${hours}h"
    echo "═══════════════════════════════════════════════════"
}

# ══════════════════════════════════════════════════════════
# STOP — release remote mode
# ══════════════════════════════════════════════════════════

cmd_stop() {
    echo "Releasing remote mode on $NODE_ID..."

    # Kill keepalive
    if [ -f "$PID_FILE" ]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        rm -f "$PID_FILE"
        ok "Keepalive released"
    fi

    # Stop OmniRoute
    pgrep -f "omniroute" >/dev/null 2>&1 && pkill -f "omniroute" && ok "OmniRoute stopped" || true

    set_state "warm-idle"
    ok "Remote mode released"
}

# ══════════════════════════════════════════════════════════
# STATUS — full readiness check
# ══════════════════════════════════════════════════════════

cmd_status() {
    echo "=== Remote Ops Status: $NODE_ID ==="
    echo "Power state: $(get_state)"
    echo ""

    # Keepalive
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        ok "Keepalive: ACTIVE (PID $(cat "$PID_FILE"))"
    else
        fail "Keepalive: INACTIVE"
    fi

    # Daemon
    if curl -sf http://127.0.0.1:8855/health >/dev/null 2>&1; then
        ok "Fleet daemon: HEALTHY"
        # Parse fleet health
        curl -sf http://127.0.0.1:8855/health 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    ch=d.get('channel_health',{})
    for k,v in ch.items(): print(f'       {k}: {v.get(\"score\",\"?\")}')
except: pass" 2>/dev/null
    else
        fail "Fleet daemon: DOWN"
    fi

    # Router
    if pgrep -f "omniroute" >/dev/null 2>&1; then
        ok "OmniRoute: RUNNING on :20128"
    elif curl -sf http://127.0.0.1:3456/health >/dev/null 2>&1; then
        ok "claude-code-router: RUNNING on :3456"
    else
        fail "API router: NOT RUNNING"
    fi

    # RTK
    command -v rtk &>/dev/null && ok "RTK: installed" || warn "RTK: not installed"

    # Tailscale
    if command -v tailscale &>/dev/null; then
        local ts; ts=$(tailscale status 2>&1 | head -1)
        echo "$ts" | grep -qi "stopped" && warn "Tailscale: STOPPED" || ok "Tailscale: $ts"
    fi

    # NATS
    pgrep -f "nats-server" >/dev/null 2>&1 && ok "NATS: running" || fail "NATS: not running"

    # Power
    echo ""
    echo "--- Power Settings ---"
    local sleep_v; sleep_v=$(pmset -g 2>/dev/null | grep "^ sleep" | awk '{print $2}' || echo "?")
    local womp; womp=$(pmset -g 2>/dev/null | grep "womp" | awk '{print $2}' || echo "?")
    [ "$sleep_v" = "0" ] && ok "System sleep: never" || warn "System sleep: ${sleep_v}min"
    [ "$womp" = "1" ] && ok "Wake-on-LAN: enabled" || warn "WoL: disabled"

    # Env vars (show presence, NEVER values)
    echo ""
    echo "--- API Keys (presence only) ---"
    [ -n "${ANTHROPIC_API_KEY:-}" ] && ok "ANTHROPIC_API_KEY: set" || warn "ANTHROPIC_API_KEY: not set"
    [ -n "${DEEPSEEK_API_KEY:-}" ] && ok "DEEPSEEK_API_KEY: set" || warn "DEEPSEEK_API_KEY: not set"
    [ -n "${OPENROUTER_API_KEY:-}" ] && ok "OPENROUTER_API_KEY: set" || warn "OPENROUTER_API_KEY: not set"
    [ -n "${ANTHROPIC_BASE_URL:-}" ] && ok "ANTHROPIC_BASE_URL: ${ANTHROPIC_BASE_URL}" || warn "ANTHROPIC_BASE_URL: default"
}

# ══════════════════════════════════════════════════════════
# WAKE — send Wake-on-LAN to a sleeping fleet node
# ══════════════════════════════════════════════════════════

cmd_wake() {
    local target="$1"
    local info; info=$(_get_node_info "$target")
    local user host mac role
    IFS='|' read -r user host mac role <<< "$info"

    if [ -z "$host" ]; then
        fail "No host for '$target' in config.json"
        return 1
    fi
    if [ -z "$mac" ]; then
        fail "No MAC address for '$target' in config.json"
        return 1
    fi

    echo "Waking $target ($host, MAC: $mac)..."

    # Method 1: wakeonlan CLI
    if command -v wakeonlan &>/dev/null; then
        wakeonlan "$mac" 2>/dev/null && ok "WoL sent via wakeonlan" || true
    fi

    # Method 2: Python magic packet (always available)
    python3 -c "
import socket
mac='$mac'.replace(':','').replace('-','')
data=b'\\xff'*6 + bytes.fromhex(mac)*16
s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.sendto(data, ('255.255.255.255', 9))
s.sendto(data, ('255.255.255.255', 7))
s.close()
print('Magic packet sent')" 2>/dev/null && ok "WoL magic packet broadcast"

    # Method 3: Tailscale SSH wake (if available)
    if command -v tailscale &>/dev/null; then
        local ts_ip
        ts_ip=$(tailscale status 2>/dev/null | grep -i "$target" | awk '{print $1}' || true)
        if [ -n "$ts_ip" ]; then
            ok "Tailscale: $target at $ts_ip — trying SSH wake"
            ssh -o ConnectTimeout=5 "$user@$ts_ip" "echo awake" 2>/dev/null && ok "SSH wake confirmed via Tailscale" || true
        fi
    fi

    # Wait for response
    echo "Waiting for $target..."
    for i in 1 2 3 4 5; do
        sleep 5
        if ping -c 1 -W 2 "$host" &>/dev/null; then
            ok "$target is UP after $((i*5))s"
            return 0
        fi
    done
    warn "$target did not respond after 25s"
    return 1
}

# ══════════════════════════════════════════════════════════
# ALL-START — bootstrap keepalive on all fleet peers via SSH
# ══════════════════════════════════════════════════════════

cmd_all_start() {
    local hours="${1:-72}"
    echo "Bootstrapping remote ops on all fleet nodes (${hours}h)..."

    # Start locally first
    cmd_start "$hours"

    # SSH to each peer
    [ -f "$CONFIG" ] || { fail "No config.json"; return 1; }

    python3 -c "
import json
cfg=json.loads(open('$CONFIG').read())
for nid,node in cfg.get('nodes',{}).items():
    if nid != '$NODE_ID':
        print(f\"{node.get('user','')}@{node.get('host','')} {nid}\")" 2>/dev/null | while read -r ssh_target nid; do
        [ -z "$ssh_target" ] || [ "$ssh_target" = "@" ] && continue
        echo ""
        echo "→ Starting remote ops on $nid ($ssh_target)..."
        ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$ssh_target" \
            "caffeinate -s -i -t $((hours*3600)) & echo \$! > /tmp/fleet-remote-${nid}.pid; echo busy-protected > /tmp/fleet-power-state" \
            2>/dev/null && ok "$nid: keepalive started" || warn "$nid: SSH failed (may need wake first)"
    done
}

# ══════════════════════════════════════════════════════════
# CHECK — audit power/network settings (read-only)
# ══════════════════════════════════════════════════════════

cmd_check() {
    echo "=== System Audit: $NODE_ID ==="
    echo ""

    # pmset
    local sleep_v; sleep_v=$(pmset -g 2>/dev/null | grep "^ sleep" | awk '{print $2}' || echo "?")
    local womp; womp=$(pmset -g 2>/dev/null | grep "womp" | awk '{print $2}' || echo "?")
    local disp; disp=$(pmset -g 2>/dev/null | grep "displaysleep" | awk '{print $2}' || echo "?")

    [ "$sleep_v" = "0" ] && ok "System sleep: never" || fail "System sleep: ${sleep_v}min — FIX: sudo pmset -a sleep 0"
    [ "$womp" = "1" ] && ok "Wake-on-LAN: enabled" || fail "WoL: disabled — FIX: sudo pmset -a womp 1"
    echo "  Display sleep: ${disp}min (OK for remote — display can sleep)"

    # SSH
    if systemsetup -getremotelogin 2>/dev/null | grep -q "On"; then
        ok "SSH: enabled"
    else
        fail "SSH: disabled — FIX: sudo systemsetup -setremotelogin on"
    fi

    # Tailscale
    if command -v tailscale &>/dev/null; then
        local ts; ts=$(tailscale status 2>&1 | head -1)
        echo "$ts" | grep -qi "stopped" && fail "Tailscale: stopped — FIX: tailscale up" || ok "Tailscale: $ts"
    else
        warn "Tailscale: not installed — OPTIONAL: brew install tailscale"
    fi

    # Tools
    command -v omniroute &>/dev/null && ok "OmniRoute: installed" || warn "OmniRoute: not installed (npm install -g omniroute)"
    command -v rtk &>/dev/null && ok "RTK: installed" || warn "RTK: not installed (brew install rtk)"
    command -v nats-server &>/dev/null && ok "nats-server: installed" || warn "nats-server: not installed (brew install nats-server)"

    # Config
    [ -f "$CONFIG" ] && ok "Fleet config: found" || fail "Fleet config: missing — cp config/config.template.json .multifleet/config.json"
}

# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

case "${1:-status}" in
    start)     cmd_start "${2:-72}" ;;
    stop)      cmd_stop ;;
    status)    cmd_status ;;
    wake)      [ -n "${2:-}" ] || { echo "Usage: remote-ops.sh wake <node>"; exit 1; }; cmd_wake "$2" ;;
    all-start) cmd_all_start "${2:-72}" ;;
    check)     cmd_check ;;
    *)
        echo "Usage: remote-ops.sh {start [hours]|stop|status|wake <node>|all-start [hours]|check}"
        exit 1
        ;;
esac
