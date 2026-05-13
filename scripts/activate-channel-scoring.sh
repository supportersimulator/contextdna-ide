#!/usr/bin/env bash
# WaveH 2026-05-12 — HHH1 / MFINV-C07 canary activation script.
#
# Idempotently adds FLEET_CHANNEL_SCORING_ENABLED=1 to the local
# fleet-nats LaunchAgent plist, with reversible backup. Run on the
# target node (e.g. mac3 when it returns to the fleet) to participate
# in the metric-driven channel cascade A/B.
#
# Reverse: cp the .bak.WaveH-* file back over the plist + bootout/bootstrap.
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/io.contextdna.fleet-nats.plist"
[ -f "$PLIST" ] || { echo "ERR: $PLIST not found"; exit 1; }

BAK="${PLIST}.bak.WaveH-$(date +%Y%m%d-%H%M%S)"
cp "$PLIST" "$BAK"
echo "Backed up plist -> $BAK"

python3 - <<PY
import plistlib
p = "$PLIST"
with open(p, "rb") as f:
    d = plistlib.load(f)
env = d.setdefault("EnvironmentVariables", {})
prior = env.get("FLEET_CHANNEL_SCORING_ENABLED")
env["FLEET_CHANNEL_SCORING_ENABLED"] = "1"
with open(p, "wb") as f:
    plistlib.dump(d, f)
print(f"FLEET_CHANNEL_SCORING_ENABLED: {prior!r} -> '1'")
PY

# Reload daemon
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
sleep 2
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 6

# Verify
echo "--- /health.channel_priority_modules.channel_scoring ---"
/usr/bin/curl -sf -m 15 http://127.0.0.1:8855/health -o /tmp/health-canary.json
python3 -c "
import json
d=json.load(open('/tmp/health-canary.json'))
cs=d['channel_priority_modules']['channel_scoring']
print('enabled:', cs.get('enabled'))
print('env_flag:', cs.get('env_flag'))
print('calls_total:', cs.get('channel_scoring_calls_total'))
"
echo "Canary activated."
