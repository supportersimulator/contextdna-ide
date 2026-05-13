#!/usr/bin/env bash
# mac1-self-diagnostic.sh — run on mac1 when it boots/reconnects.
# Collects all observable state, writes to .fleet-messages/all/mac1-diag-<ts>.md, commits + pushes.
# Aaron and fleet read the diagnostic via P7 pull.

set -uo pipefail
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
OUT="$REPO/.fleet-messages/all/mac1-diag-$(date +%Y%m%d-%H%M%S).md"

mkdir -p "$(dirname "$OUT")"

{
  echo "# mac1 Self-Diagnostic — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo ""
  echo "## Identity"
  echo "- hostname: $(hostname -s)"
  echo "- LAN IPs: $(ifconfig | grep 'inet ' | awk '{print $2}' | grep -v 127 | tr '\n' ' ')"
  echo "- Tailscale: $(tailscale ip -4 2>/dev/null || echo 'NOT INSTALLED')"
  echo "- MAC: $(ifconfig en0 2>/dev/null | grep ether | awk '{print $2}')"
  echo "- uptime: $(uptime)"
  echo ""
  echo "## Daemon"
  echo "- fleet-nats LaunchAgent: $(launchctl list | grep contextdna | head -5)"
  echo "- fleet daemon health: $(curl -sf --max-time 3 http://127.0.0.1:8855/health 2>&1 | head -c 300)"
  echo "- NATS port 4222: $(lsof -i :4222 -P -n 2>&1 | head -3)"
  echo ""
  echo "## Reachability outbound"
  echo "- ping mac2 LAN (192.168.1.183): $(ping -c 1 -W 1000 -t 2 192.168.1.183 2>&1 | tail -2 | head -1)"
  echo "- ping mac3 LAN (192.168.1.191): $(ping -c 1 -W 1000 -t 2 192.168.1.191 2>&1 | tail -2 | head -1)"
  echo "- curl mac2 :8855: $(curl -sf --max-time 3 http://192.168.1.183:8855/health 2>&1 | head -c 100)"
  echo "- curl mac3 :8855: $(curl -sf --max-time 3 http://192.168.1.191:8855/health 2>&1 | head -c 100)"
  echo ""
  echo "## Reachability inbound (what listens)"
  echo "- port 22 (ssh): $(lsof -i :22 -P -n 2>&1 | head -3)"
  echo "- port 8855 (daemon): $(lsof -i :8855 -P -n 2>&1 | head -3)"
  echo "- port 4222 (nats): $(lsof -i :4222 -P -n 2>&1 | head -3)"
  echo "- port 6222 (cluster): $(lsof -i :6222 -P -n 2>&1 | head -3)"
  echo ""
  echo "## Firewall"
  echo "- macOS firewall: $(sudo -n /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>&1 || echo 'requires sudo')"
  echo "- SSH system pref: $(sudo -n systemsetup -getremotelogin 2>&1 || echo 'requires sudo')"
  echo ""
  echo "## Power/Sleep"
  echo "- pmset: $(pmset -g | head -10)"
  echo "- caffeinate active: $(pgrep -fla caffeinate | head -3)"
  echo "- last sleep/wake: $(pmset -g log 2>/dev/null | grep -iE 'wake|sleep' | tail -5)"
  echo ""
  echo "## Git"
  echo "- HEAD: $(git -C "$REPO" log --oneline -1)"
  echo "- branch: $(git -C "$REPO" branch --show-current)"
  echo "- status: $(git -C "$REPO" status --porcelain | head -10)"
  echo ""
  echo "## Fleet process tree"
  echo '```'
  ps aux | grep -iE "fleet|nats|discord|claude" | grep -v grep | head -20
  echo '```'
  echo ""
  echo "---"
  echo "End of diagnostic. mac2 will read this on next git pull."
} > "$OUT"

cd "$REPO"
git add "$OUT"
git commit -m "diag: mac1 self-diagnostic $(date +%Y%m%d-%H%M%S)" --no-verify
git push origin main || echo "push deferred"

echo "Diagnostic at $OUT"
