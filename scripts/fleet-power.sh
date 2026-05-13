#!/usr/bin/env bash
# fleet-power.sh — Unified fleet power management (7-state model)
#
# Integrates: caffeinate, WoL, fleet daemon, OmniRoute, GhostCheckpoint
#
# States: asleep → waking → warm-idle → session-hot → busy-protected → cooldown → parking
#
# Usage:
#   bash scripts/fleet-power.sh status           # Show power state + keepawake
#   bash scripts/fleet-power.sh busy [reason]    # Assert busy-protected (start caffeinate)
#   bash scripts/fleet-power.sh idle             # Release to warm-idle (stop caffeinate)
#   bash scripts/fleet-power.sh park             # Transition to parking → asleep
#   bash scripts/fleet-power.sh wake <node>      # Wake a remote fleet node
#   bash scripts/fleet-power.sh all-busy         # Assert busy on all fleet nodes

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"

# ── Python power state manager ──

_py_power() {
    cd "$REPO_ROOT" && python3 -c "
import sys, json
sys.path.insert(0, 'multi-fleet')
from multifleet.power_state import PowerStateManager, PowerState
mgr = PowerStateManager(repo_root='.')
$1
" 2>/dev/null
}

case "${1:-status}" in
    status)
        echo "=== Fleet Power Status — $NODE_ID ==="
        echo ""

        # Power state from Python manager
        STATE_JSON=$(_py_power "print(json.dumps(mgr.status()))" || echo '{}')
        if [ -n "$STATE_JSON" ] && [ "$STATE_JSON" != "{}" ]; then
            STATE=$(echo "$STATE_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('state','?'))")
            DURATION=$(echo "$STATE_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"{d.get('duration_s',0):.0f}s\")")
            CAFF=$(echo "$STATE_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print('ACTIVE' if d.get('caffeinate_active') else 'inactive')")
            echo "  State: $STATE (for $DURATION)"
            echo "  Caffeinate: $CAFF"
        else
            echo "  State: unknown (power_state.py not available)"
            # Fallback: check caffeinate directly
            if pgrep -x caffeinate >/dev/null 2>&1; then
                echo "  Caffeinate: ACTIVE (PID $(pgrep -x caffeinate | head -1))"
            else
                echo "  Caffeinate: inactive"
            fi
        fi
        echo ""

        # System sleep settings (macOS)
        if command -v pmset &>/dev/null; then
            echo "--- macOS Power Settings ---"
            SLEEP=$(pmset -g 2>/dev/null | grep "^ sleep" | awk '{print $2}' || echo "?")
            DISP=$(pmset -g 2>/dev/null | grep "displaysleep" | awk '{print $2}' || echo "?")
            WOMP=$(pmset -g 2>/dev/null | grep "womp" | awk '{print $2}' || echo "?")
            echo "  System sleep: ${SLEEP} (0=never)"
            echo "  Display sleep: ${DISP} min"
            echo "  Wake-on-LAN: ${WOMP} (1=enabled)"
            echo ""
        fi

        # Fleet daemon
        echo "--- Fleet Services ---"
        if curl -sf http://127.0.0.1:8855/health >/dev/null 2>&1; then
            echo "  Fleet daemon: RUNNING"
        else
            echo "  Fleet daemon: NOT RUNNING"
        fi

        # OmniRoute state
        if [ -f /tmp/omniroute-state.json ]; then
            TIER=$(python3 -c "import json; d=json.loads(open('/tmp/omniroute-state.json').read()); print(d.get('active_tier',1))" 2>/dev/null || echo "?")
            echo "  OmniRoute tier: $TIER/5"
        else
            echo "  OmniRoute: no state file"
        fi

        # Router status
        if curl -sf http://127.0.0.1:3456/health >/dev/null 2>&1; then
            echo "  claude-code-router: RUNNING"
        else
            echo "  claude-code-router: not running"
        fi
        if curl -sf http://localhost:20128 >/dev/null 2>&1; then
            echo "  OmniRoute gateway: RUNNING"
        else
            echo "  OmniRoute gateway: not running"
        fi
        ;;

    busy)
        REASON="${2:-manual-assert}"
        RESULT=$(_py_power "
r = mgr.transition(PowerState.BUSY_PROTECTED, reason='$REASON')
print(json.dumps(r))
")
        echo "$RESULT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d.get('action') == 'transitioned':
    print(f\"Power: {d['from']} -> {d['to']} | Caffeinate: {'ACTIVE' if d.get('caffeinate_active') else 'inactive'}\")
else:
    print(f\"Result: {d.get('action','')} — {d.get('reason','')}\")
"
        ;;

    idle)
        RESULT=$(_py_power "
r = mgr.transition(PowerState.WARM_IDLE, reason='manual-release')
print(json.dumps(r))
")
        echo "Released to warm-idle. Caffeinate stopped."
        ;;

    park)
        echo "Parking $NODE_ID..."
        _py_power "
r = mgr.transition(PowerState.PARKING, reason='manual-park')
print(f\"State: {r.get('to', '?')}\")"
        echo "Node will transition to asleep per system settings."
        ;;

    wake)
        if [ -z "${2:-}" ]; then
            echo "Usage: fleet-power.sh wake <node>"
            exit 1
        fi
        # Delegate to fleet-keepalive.sh which has WoL logic
        bash "$REPO_ROOT/scripts/fleet-keepalive.sh" wake "$2"
        ;;

    all-busy)
        echo "Asserting busy-protected on all fleet nodes..."
        bash "$REPO_ROOT/scripts/fleet-keepalive.sh" all-start
        _py_power "
r = mgr.transition(PowerState.BUSY_PROTECTED, reason='all-busy-assert')
print(f\"Local: {r.get('to', '?')}\")"
        ;;

    *)
        echo "Usage: fleet-power.sh {status|busy [reason]|idle|park|wake <node>|all-busy}"
        exit 1
        ;;
esac
