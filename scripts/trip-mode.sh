#!/usr/bin/env bash
# Trip Mode — UNIFIED remote fleet activation
#
# Merges: mac1/trip-mode.sh (OmniRoute, Tailscale, NATS config-driven, power checks)
#       + mac2/activate-full-remote.sh (model router, RTK, fleet connectivity)
#       + mac3 (ghost-checkpoint integration)
#
# Usage:
#   bash scripts/trip-mode.sh [hours]   # Activate (default: 72h)
#   bash scripts/trip-mode.sh stop      # Deactivate
#   bash scripts/trip-mode.sh status    # Check all systems

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
CONFIG="$REPO_ROOT/.multifleet/config.json"

# ── Stop mode ──

if [ "${1:-}" = "stop" ]; then
    echo "Deactivating trip mode on $NODE_ID..."
    bash "$REPO_ROOT/scripts/fleet-keepawake.sh" stop
    # Stop OmniRoute
    pkill -f omniroute 2>/dev/null && echo "  OmniRoute stopped" || true
    # Stop claude-code-router
    pkill -f claude-code-router 2>/dev/null && echo "  Model router stopped" || true
    echo "Trip mode deactivated."
    exit 0
fi

# ── Status mode ──

if [ "${1:-}" = "status" ]; then
    echo ""
    echo "=== TRIP MODE STATUS — $NODE_ID ==="
    echo ""
    bash "$REPO_ROOT/scripts/fleet-keepawake.sh" status
    echo ""
    echo "--- Services ---"
    # Fleet daemon
    if curl -sf http://127.0.0.1:8855/health >/dev/null 2>&1; then
        echo "Fleet daemon: RUNNING"
    else
        echo "Fleet daemon: NOT RUNNING"
    fi
    # OmniRoute
    if curl -sf http://localhost:20128 >/dev/null 2>&1; then
        echo "OmniRoute: RUNNING on :20128"
    else
        echo "OmniRoute: not running"
    fi
    # Model router
    if curl -sf http://127.0.0.1:3456/health >/dev/null 2>&1; then
        echo "Model router: RUNNING on :3456"
    else
        echo "Model router: not running"
    fi
    # RTK
    if command -v rtk &>/dev/null; then
        echo "RTK: $(rtk --version 2>/dev/null || echo 'installed')"
    else
        echo "RTK: not installed"
    fi
    # NATS
    if pgrep -f "nats-server" >/dev/null 2>&1; then
        echo "NATS server: RUNNING"
    else
        echo "NATS server: not running"
    fi
    exit 0
fi

# ── Activation mode ──

HOURS="${1:-72}"
SECONDS_VAL=$((HOURS * 3600))

echo ""
echo "============================================================"
echo "       FLEET TRIP MODE — Remote Operation"
echo "       Node: $NODE_ID"
echo "       Duration: ${HOURS} hours"
echo "============================================================"
echo ""

CHECKS_OK=0
CHECKS_WARN=0

ok()   { echo "  [OK] $*"; CHECKS_OK=$((CHECKS_OK + 1)); }
warn() { echo "  [!!] $*"; CHECKS_WARN=$((CHECKS_WARN + 1)); }
skip() { echo "  [--] $*"; }

# ── 1. Keepawake ──
echo "--- 1. Keep-Awake ---"
bash "$REPO_ROOT/scripts/fleet-keepawake.sh" start "$HOURS" 2>/dev/null
if [ -f "/tmp/fleet-keepawake-${NODE_ID}.pid" ] && kill -0 "$(cat "/tmp/fleet-keepawake-${NODE_ID}.pid")" 2>/dev/null; then
    ok "Keep-awake: ${HOURS}h (PID $(cat "/tmp/fleet-keepawake-${NODE_ID}.pid"))"
else
    warn "Keep-awake: may not have started"
fi

# ── 2. Fleet daemon ──
echo ""
echo "--- 2. Fleet Daemon ---"
if curl -sf http://127.0.0.1:8855/health >/dev/null 2>&1; then
    ok "Fleet daemon: already running"
else
    # Config-driven NATS URL (from mac1 — no hardcoded IPs)
    NATS_URLS=""
    if [ -f "$CONFIG" ] && command -v python3 &>/dev/null; then
        NATS_URLS=$(python3 -c "
import json
cfg = json.loads(open('$CONFIG').read())
chief = cfg.get('chief', {}).get('host', '')
if chief: print(f'nats://{chief}:4222')
else: print('nats://127.0.0.1:4222')
" 2>/dev/null)
    fi

    if [ -f "$REPO_ROOT/tools/fleet_nerve_nats.py" ]; then
        NATS_URL="${NATS_URLS:-nats://127.0.0.1:4222}" MULTIFLEET_NODE_ID="$NODE_ID" \
            python3 "$REPO_ROOT/tools/fleet_nerve_nats.py" serve &>/tmp/fleet-daemon-${NODE_ID}.log &
        sleep 2
        if curl -sf http://127.0.0.1:8855/health >/dev/null 2>&1; then
            ok "Fleet daemon: started"
        else
            warn "Fleet daemon: started but health not responding yet"
        fi
    else
        warn "Fleet daemon: fleet_nerve_nats.py not found"
    fi
fi

# ── 3. API Failover (OmniRoute or claude-code-router) ──
echo ""
echo "--- 3. API Failover ---"

# Try OmniRoute first (from mac1)
if command -v omniroute &>/dev/null; then
    if pgrep -f "omniroute" >/dev/null 2>&1; then
        ok "OmniRoute: already running on :20128"
    else
        omniroute --no-open &>/tmp/omniroute.log &
        sleep 2
        if curl -sf http://localhost:20128 >/dev/null 2>&1; then
            ok "OmniRoute: started on :20128"
        else
            warn "OmniRoute: started but health pending"
        fi
    fi
    echo "       Dashboard: http://localhost:20128"
    echo "       Failover: Anthropic -> DeepSeek -> OpenRouter"
# Fallback: claude-code-router (from mac2)
elif command -v claude-code-router &>/dev/null; then
    if curl -sf http://127.0.0.1:3456/health >/dev/null 2>&1; then
        ok "Model router: already running on :3456"
    else
        # Get DeepSeek key from keychain (mac2 approach)
        DK=$(security find-generic-password -s fleet-nerve -a DEEPSEEK_API_KEY -w 2>/dev/null || echo "")
        if [ -n "$DK" ]; then
            export DEEPSEEK_API_KEY="$DK"
            claude-code-router start --background 2>/dev/null &
            sleep 2
            if curl -sf http://127.0.0.1:3456/health >/dev/null 2>&1; then
                ok "Model router: started (Anthropic -> DeepSeek)"
            else
                warn "Model router: started but health pending"
            fi
        else
            warn "Model router: no DeepSeek key in keychain"
        fi
    fi
else
    skip "API failover: neither omniroute nor claude-code-router installed"
    echo "       Install: npm install -g omniroute  OR  bash scripts/model-switch-setup.sh install"
fi

# ── 4. RTK Token Saver (from mac2) ──
echo ""
echo "--- 4. RTK Token Saver ---"
if command -v rtk &>/dev/null; then
    ok "RTK: $(rtk --version 2>/dev/null || echo 'active')"
else
    skip "RTK: not installed (brew install rtk && rtk init -g)"
fi

# ── 5. Tailscale (from mac1) ──
echo ""
echo "--- 5. Tailscale VPN ---"
if command -v tailscale &>/dev/null; then
    TS_STATUS=$(tailscale status 2>&1 | head -1 || true)
    if echo "$TS_STATUS" | grep -qi "stopped\|logged out"; then
        warn "Tailscale: stopped — run: tailscale up"
    else
        ok "Tailscale: $TS_STATUS"
    fi
else
    skip "Tailscale: not installed (brew install tailscale)"
fi

# ── 6. SSH ──
echo ""
echo "--- 6. SSH Remote Access ---"
if systemsetup -getremotelogin 2>/dev/null | grep -q "On"; then
    ok "SSH: enabled"
else
    warn "SSH: disabled (enable: sudo systemsetup -setremotelogin on)"
fi

# ── 7. Power Settings ──
echo ""
echo "--- 7. Power Settings ---"
SLEEP_VAL=$(pmset -g 2>/dev/null | grep "^ sleep" | awk '{print $2}' || echo "?")
WOMP=$(pmset -g 2>/dev/null | grep "womp" | awk '{print $2}' || echo "?")
if [ "$SLEEP_VAL" = "0" ]; then
    ok "System sleep: never"
else
    warn "System sleep: ${SLEEP_VAL}min (run: sudo pmset -a sleep 0)"
fi
if [ "$WOMP" = "1" ]; then
    ok "Wake-on-LAN: enabled"
else
    warn "WoL: disabled (run: sudo pmset -a womp 1)"
fi

# ── 8. NATS ──
echo ""
echo "--- 8. NATS Server ---"
if pgrep -f "nats-server" >/dev/null 2>&1; then
    ok "NATS server: running"
else
    warn "NATS server: not running"
fi

# ── 9. GhostAgent Checkpoint (from mac3) ──
echo ""
echo "--- 9. GhostAgent Checkpoint ---"
if [ -f "$REPO_ROOT/scripts/ghost-checkpoint.sh" ]; then
    bash "$REPO_ROOT/scripts/ghost-checkpoint.sh" 2>/dev/null && ok "Ghost checkpoint: saved" || warn "Ghost checkpoint: failed"
else
    skip "Ghost checkpoint: script not found"
fi

# ── 10. Fleet Connectivity (from mac2) ──
echo ""
echo "--- 10. Fleet Connectivity ---"
HEALTH=$(curl -sf http://127.0.0.1:8855/health 2>/dev/null || echo "")
if [ -n "$HEALTH" ]; then
    python3 -c "
import json, sys
d = json.loads('$HEALTH'.replace(\"'\", '\"'))
print(f'  Daemon status: {d.get(\"status\", \"unknown\")}')
ch = d.get('channel_health', {})
for k, v in ch.items():
    score = v.get('score', '?') if isinstance(v, dict) else v
    print(f'  {k}: {score}')
" 2>/dev/null || echo "  Daemon responding but health parse failed"
    ok "Fleet daemon: connected"
else
    warn "Fleet daemon: not responding"
fi

# ── Summary ──
echo ""
echo "============================================================"
echo "  TRIP MODE SUMMARY"
echo "============================================================"
echo ""
echo "  Checks passed: $CHECKS_OK"
echo "  Warnings: $CHECKS_WARN"
echo ""
echo "  REMOTE ACCESS:"
echo "    Claude Code:  https://claude.ai/code"
echo "    SSH (LAN):    ssh $(whoami)@$(hostname).local"
if command -v tailscale &>/dev/null; then
echo "    SSH (remote):  ssh $(whoami)@${NODE_ID}"
fi
echo ""
echo "  FLEET:"
echo "    Messages:     .fleet-messages/${NODE_ID}/"
echo "    Wake peers:   bash scripts/fleet-keepawake.sh wake mac2"
echo "    Fleet check:  bash scripts/fleet-check.sh"
echo ""
echo "  DEACTIVATE:"
echo "    bash scripts/trip-mode.sh stop"
echo ""
if [ "$CHECKS_WARN" -eq 0 ]; then
    echo "  ALL SYSTEMS GO — safe to leave ($HOURS hours)"
else
    echo "  $CHECKS_WARN WARNING(S) — review above before leaving"
fi
echo "============================================================"
