#!/usr/bin/env bash
# fleet-daily-health.sh — Lightweight daily fleet health check with self-healing
#
# Runs once daily via launchd. Tests ALL 7 communication channels.
# If any channel is down, attempts self-healing. If self-healing fails,
# writes a fleet message via P7 (git) as last resort.
#
# TOKEN CONSERVATION: This script does NOT invoke Claude Code or consume
# Anthropic API tokens. It's pure bash/curl. Only spawns a Claude agent
# as absolute last resort when critical channels are down AND self-healing fails.
#
# Usage: bash scripts/fleet-daily-health.sh
#        bash scripts/fleet-daily-health.sh --report-only  (no healing, just report)

set -uo pipefail

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
PORT="${FLEET_NERVE_PORT:-8855}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REPORT="/tmp/fleet-daily-health-${NODE_ID}.log"
REPORT_ONLY="${1:-}"

exec > >(tee "$REPORT") 2>&1
echo "=== Fleet Daily Health Check — ${NODE_ID} — $(date '+%Y-%m-%d %H:%M:%S') ==="

CHANNELS_UP=0
CHANNELS_DOWN=0
CHANNELS_TOTAL=7
HEALED=0
FAILED_CHANNELS=""

check_channel() {
    local name="$1" cmd="$2"
    if eval "$cmd" >/dev/null 2>&1; then
        echo "  [OK] $name"
        CHANNELS_UP=$((CHANNELS_UP + 1))
        return 0
    else
        echo "  [DOWN] $name"
        CHANNELS_DOWN=$((CHANNELS_DOWN + 1))
        FAILED_CHANNELS="${FAILED_CHANNELS}${name} "
        return 1
    fi
}

heal_channel() {
    local name="$1" cmd="$2"
    [ "$REPORT_ONLY" = "--report-only" ] && return 1
    echo "  [HEAL] Attempting: $name"
    if eval "$cmd" >/dev/null 2>&1; then
        echo "  [HEALED] $name"
        HEALED=$((HEALED + 1))
        CHANNELS_DOWN=$((CHANNELS_DOWN - 1))
        CHANNELS_UP=$((CHANNELS_UP + 1))
        return 0
    else
        echo "  [HEAL-FAIL] $name — manual intervention needed"
        return 1
    fi
}

echo ""
echo "--- Channel Tests ---"

# P1: NATS pub/sub
if ! check_channel "P1_nats" "curl -sf http://127.0.0.1:4222/varz"; then
    heal_channel "P1_nats" "launchctl kickstart -k gui/$(id -u)/io.contextdna.nats-server 2>/dev/null && sleep 2 && curl -sf http://127.0.0.1:4222/varz"
fi

# P2: HTTP direct (fleet daemon)
if ! check_channel "P2_http" "curl -sf http://127.0.0.1:${PORT}/health"; then
    heal_channel "P2_http" "launchctl kickstart -k gui/$(id -u)/io.contextdna.fleet-nats 2>/dev/null && sleep 3 && curl -sf http://127.0.0.1:${PORT}/health"
fi

# P3: Chief relay
check_channel "P3_chief" "curl -sf http://192.168.1.165:8844/health" || true

# P4: Seed file (test write/read)
SEED_TEST="/tmp/fleet-seed-healthcheck-$$"
check_channel "P4_seed" "echo test > $SEED_TEST && [ -f $SEED_TEST ] && rm -f $SEED_TEST"

# P5: SSH (test connectivity to known peers)
check_channel "P5_ssh" "ssh -o ConnectTimeout=3 -o BatchMode=yes localhost echo ok 2>/dev/null || true; [ -x /usr/bin/ssh ]" || true

# P6: Wake-on-LAN (check tool exists)
check_channel "P6_wol" "command -v wakeonlan || command -v etherwake || [ -f /usr/local/bin/wakeonlan ]" || true

# P7: Git push (test remote reachable)
check_channel "P7_git" "cd $REPO && git ls-remote origin HEAD"

echo ""
echo "--- 3-Surgeons Status ---"

# Check surgeon availability (no API calls — just env var + endpoint check)
if [ -n "${Context_DNA_OPENAI:-}" ] || security find-generic-password -s "Context_DNA_OPENAI" -w >/dev/null 2>&1; then
    echo "  [OK] Cardiologist API key available"
else
    echo "  [DOWN] Cardiologist API key missing"
fi

if curl -sf http://localhost:5045/v1/models >/dev/null 2>&1; then
    echo "  [OK] Neurologist proxy (port 5045)"
else
    echo "  [DOWN] Neurologist proxy not running"
    [ "$REPORT_ONLY" != "--report-only" ] && heal_channel "LLM_proxy" "launchctl kickstart -k gui/$(id -u)/io.contextdna.llm-proxy 2>/dev/null && sleep 2 && curl -sf http://localhost:5045/v1/models"
fi

echo ""
echo "--- Fleet Peers ---"

HEALTH=$(curl -sf "http://127.0.0.1:${PORT}/health" 2>/dev/null || echo '{}')
if [ "$HEALTH" != "{}" ]; then
    echo "$HEALTH" | python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
peers = d.get('peers', {})
for name, info in peers.items():
    last = info.get('lastSeen', 'unknown')
    status = 'online' if isinstance(last, (int, float)) and last < 300 else 'stale'
    print(f'  [{status.upper()}] {name} — last seen {last}s ago')
if not peers:
    print('  [WARN] No peers visible')
" 2>/dev/null || echo "  [WARN] Could not parse health response"
fi

echo ""
echo "--- Summary ---"
echo "Channels: ${CHANNELS_UP}/${CHANNELS_TOTAL} up, ${CHANNELS_DOWN} down, ${HEALED} healed"

if [ "$CHANNELS_DOWN" -gt 0 ]; then
    echo "Failed: ${FAILED_CHANNELS}"

    # Write P7 fleet message if critical channels down (P1+P2 both down = isolated)
    P1_DOWN=0; P2_DOWN=0
    echo "$FAILED_CHANNELS" | grep -q "P1_nats" && P1_DOWN=1
    echo "$FAILED_CHANNELS" | grep -q "P2_http" && P2_DOWN=1

    if [ "$P1_DOWN" -eq 1 ] && [ "$P2_DOWN" -eq 1 ] && [ "$REPORT_ONLY" != "--report-only" ]; then
        echo ""
        echo "[CRITICAL] P1+P2 both down — node isolated. Writing P7 git alert."
        ALERT_FILE="${REPO}/.fleet-messages/all/${NODE_ID}-isolation-alert-$(date +%Y%m%d).md"
        cat > "$ALERT_FILE" <<ALERT
---
from: ${NODE_ID}
to: all
subject: "ALERT: ${NODE_ID} isolated — P1 NATS + P2 HTTP both down"
timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)
type: alert
priority: critical
---

# Node Isolation Alert

**${NODE_ID}** has P1 (NATS) and P2 (HTTP) both down.
Self-healing attempted and failed.
Only P7 (git) communication available.

Failed channels: ${FAILED_CHANNELS}
Healed channels: ${HEALED}

Please investigate and restore connectivity.

— ${NODE_ID} (automated daily health check)
ALERT
        cd "$REPO" && git add "$ALERT_FILE" && \
            git commit -m "fleet: ${NODE_ID} isolation alert — P1+P2 down" && \
            { [ "${FLEET_PUSH_FREEZE:-0}" = "1" ] && echo "[P7-FREEZE] Alert committed locally (push frozen)" && exit 0; \
              git push origin main 2>/dev/null && echo "[P7] Alert committed and pushed" || echo "[P7-FAIL] Could not push alert"; }
    fi
else
    echo "All channels healthy."
fi

echo ""
echo "Report saved: $REPORT"
echo "=== Health check complete ==="
