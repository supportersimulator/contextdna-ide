#!/usr/bin/env bash
# recover-peer-reachability.sh <peer> — class-aware recovery (ZSF).
# (a) wifi-sleep   → WoL via MAC, then re-probe
# (b) mdns-stale   → dscacheutil -flushcache + mDNSResponder kick (no sudo: best-effort)
# (c) lan-route    → arp -d, re-ping, fallback chief-relay seed
# (d) daemon-down  → P3 chief-relay request to restart fleet daemon
set -uo pipefail
PEER="${1:-}"; [[ -z "$PEER" ]] && { echo "usage: $0 <peer>" >&2; exit 64; }
ERR_LOG=/tmp/peer-recover-${PEER}.err
: > "$ERR_LOG"

OUT=$(bash "$(dirname "$0")/probe-peer-reachability.sh" "$PEER" 2>&1) || true
CLASS=$(echo "$OUT" | grep -oE 'class=[a-z0-9-]+' | head -1 | cut -d= -f2)
echo "recover: peer=$PEER class=$CLASS"

[[ -r "${FLEET_PEERS_ENV:-$HOME/.fleet-peers.env}" ]] && . "${FLEET_PEERS_ENV:-$HOME/.fleet-peers.env}"
UPEER=$(echo "$PEER" | tr '[:lower:]-' '[:upper:]_')
IP="$(eval echo \${FLEET_PEER_${UPEER}_IP:-})"
MAC="$(eval echo \${FLEET_PEER_${UPEER}_MAC:-})"
[[ -z "$IP" ]] && { echo "ERR: no IP for peer $PEER" >&2; exit 64; }

ACTION="none"
case "$CLASS" in
  ok) ACTION="none-ok"; echo "$PEER already reachable"; exit 0 ;;
  a-wifi-sleep)
    ACTION="wol"
    [[ -n "$MAC" ]] && command -v wakeonlan >/dev/null && wakeonlan "$MAC" >>"$ERR_LOG" 2>&1 || \
      python3 -c "import socket,struct; m=bytes.fromhex('$MAC'.replace(':','')); p=b'\xff'*6+m*16; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1); s.sendto(p,('255.255.255.255',9))" 2>>"$ERR_LOG" || echo "ZSF wol-fail $(date -u +%FT%TZ)" >> "$ERR_LOG"
    sleep 4 ;;
  b-mdns-stale)
    ACTION="mdns-flush"
    dscacheutil -flushcache 2>>"$ERR_LOG" || echo "ZSF mdns-flush-needs-sudo" >> "$ERR_LOG" ;;
  c-lan-route-stale)
    ACTION="arp-flush"
    sudo -n arp -d "$IP" 2>>"$ERR_LOG" || echo "ZSF arp-flush-skip-sudo" >> "$ERR_LOG"
    ping -c1 -W 1500 "$IP" >/dev/null 2>&1 || true ;;
  d-daemon-down)
    ACTION="chief-relay-restart"
    curl -sf -m 3 -X POST http://127.0.0.1:8855/message \
      -H "Content-Type: application/json" \
      -d "{\"type\":\"context\",\"to\":\"$PEER\",\"payload\":{\"subject\":\"restart-fleet-daemon\",\"body\":\"reachability recovery\"}}" \
      >>"$ERR_LOG" 2>&1 || echo "ZSF chief-relay-fail" >> "$ERR_LOG" ;;
  *) ACTION="probe-only" ;;
esac

# Re-probe (verify)
sleep 1
if bash "$(dirname "$0")/probe-peer-reachability.sh" "$PEER" >/dev/null 2>&1; then
  echo "recover OK peer=$PEER action=$ACTION"; exit 0
else
  echo "recover PARTIAL peer=$PEER action=$ACTION (still degraded)"; exit 1
fi
