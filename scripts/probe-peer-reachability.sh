#!/usr/bin/env bash
# probe-peer-reachability.sh <peer> — 6-layer reachability probe (ZSF).
# Surfaces WHY a peer appears down; classifies failure (a-d).
# Emits NATS event fleet.event.peer_reachability_degraded.<peer> on any miss.
set -uo pipefail
PEER="${1:-}"; [[ -z "$PEER" ]] && { echo "usage: $0 <peer>" >&2; exit 64; }
ERR_LOG=/tmp/peer-probe-${PEER}.err
: > "$ERR_LOG"

# Peer table — values pulled from env (set in ~/.fleet-peers.env, gitignored).
# Required: FLEET_PEER_<NAME>_IP, optional FLEET_PEER_<NAME>_TS / _MDNS / _MAC.
[[ -r "${FLEET_PEERS_ENV:-$HOME/.fleet-peers.env}" ]] && . "${FLEET_PEERS_ENV:-$HOME/.fleet-peers.env}"
UPEER=$(echo "$PEER" | tr '[:lower:]-' '[:upper:]_')
IP="$(eval echo \${FLEET_PEER_${UPEER}_IP:-})"
TS="$(eval echo \${FLEET_PEER_${UPEER}_TS:-})"
MDNS="$(eval echo \${FLEET_PEER_${UPEER}_MDNS:-})"
MAC="$(eval echo \${FLEET_PEER_${UPEER}_MAC:-})"
[[ -z "$IP" ]] && { echo "ERR: no IP for peer $PEER (set FLEET_PEER_${UPEER}_IP)" >&2; exit 64; }

ping_ok=0; ssh_ok=0; http_ok=0; mdns_ok=0; ts_ok=0; nats_ok=0
ping -c1 -W 1500 "$IP" >/dev/null 2>>"$ERR_LOG" && ping_ok=1
nc -zv -w 2 "$IP" 22 >/dev/null 2>>"$ERR_LOG" && ssh_ok=1
curl -sf -m 2 "http://${IP}:8855/health" >/dev/null 2>>"$ERR_LOG" && http_ok=1
dscacheutil -q host -a name "$MDNS" 2>>"$ERR_LOG" | grep -q ip_address && mdns_ok=1 || true
[[ "$TS" != "100.x" ]] && nc -zv -w 2 "$TS" 22 >/dev/null 2>>"$ERR_LOG" && ts_ok=1
curl -sf -m 2 "http://127.0.0.1:8222/connz" 2>>"$ERR_LOG" | grep -q "discord-bridge-${PEER}" && nats_ok=1

# Class:
# (a) WiFi power-save: ping=0/ssh=0 but nats=1   → degraded but reachable via NATS
# (b) mDNS stale:      mdns=0 but ping=1
# (c) LAN route stale: ping=0 mdns=1 nats=0
# (d) daemon-down:     ping=1 ssh=1 http=0
CLASS="ok"
if (( ping_ok && ssh_ok && http_ok )); then CLASS="ok"
elif (( !ping_ok && !ssh_ok && nats_ok )); then CLASS="a-wifi-sleep"
elif (( !mdns_ok && ping_ok )); then CLASS="b-mdns-stale"
elif (( !ping_ok && !nats_ok )); then CLASS="c-lan-route-stale"
elif (( ping_ok && ssh_ok && !http_ok )); then CLASS="d-daemon-down"
else CLASS="mixed"; fi

cat <<EOF
peer=$PEER ip=$IP class=$CLASS
  ping=$ping_ok ssh=$ssh_ok http=$http_ok mdns=$mdns_ok tailscale=$ts_ok nats=$nats_ok
EOF

if [[ "$CLASS" != "ok" ]]; then
  python3 - "$PEER" "$CLASS" "$ping_ok$ssh_ok$http_ok$mdns_ok$ts_ok$nats_ok" <<'PY' 2>>"$ERR_LOG" || echo "ZSF: nats-publish-failed $(date -u +%FT%TZ)" >> "$ERR_LOG"
import sys, json, subprocess
peer, klass, bits = sys.argv[1], sys.argv[2], sys.argv[3]
payload = json.dumps({"peer": peer, "class": klass, "probes": bits, "ts": __import__("time").time()})
subprocess.run(["curl","-sf","-m","2","-X","POST",
  "http://127.0.0.1:8855/event",
  "-H","Content-Type: application/json",
  "-d", json.dumps({"subject": f"fleet.event.peer_reachability_degraded.{peer}", "payload": payload})],
  check=False, timeout=3)
PY
  exit 1
fi
exit 0
