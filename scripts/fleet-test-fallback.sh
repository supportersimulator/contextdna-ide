#!/usr/bin/env bash
# fleet-test-fallback.sh — Test all 7 communication channels to a target node
# Usage: bash scripts/fleet-test-fallback.sh <target_node>
#        bash scripts/fleet-test-fallback.sh           # tests all peers
#
# Reads config from .multifleet/config.json via fleet_nerve_config.py.
# No hardcoded IPs, secrets, or node names.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="$REPO_ROOT/.multifleet/config.json"

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"

# ── Helpers ──────────────────────────────────────────────────────────────

_resolve_peer() {
  local target="$1"
  python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT/tools')
from fleet_nerve_config import load_peers
peers, _ = load_peers()
p = peers.get('$target', {})
if not p.get('ip'):
    sys.exit(1)
print(f\"{p['ip']}|{p.get('user','')}|{p.get('mac_address','')}|{p.get('port',8855)}|{p.get('tunnel_port','')}\")
" 2>/dev/null
}

_get_all_peers() {
  python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT/tools')
from fleet_nerve_config import load_peers, detect_node_id
peers, _ = load_peers()
me = detect_node_id()
for nid in sorted(peers):
    if nid != me:
        print(nid)
" 2>/dev/null
}

_get_chief() {
  python3 -c "
import sys, json
cfg = json.load(open('$CONFIG'))
print(cfg.get('chief',{}).get('nodeId',''))
" 2>/dev/null
}

_get_chief_ingest_url() {
  python3 -c "
import sys, json
cfg = json.load(open('$CONFIG'))
print(cfg.get('chief',{}).get('ingestUrl','').strip())
" 2>/dev/null
}

# ── Colors & symbols ────────────────────────────────────────────────────

GREEN=$'\033[32m'
RED=$'\033[31m'
DIM=$'\033[2m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

SYM_PASS="${GREEN}✓${RESET}"
SYM_FAIL="${RED}✗${RESET}"
SYM_SKIP="${DIM}—${RESET}"

# ── Channel testers ─────────────────────────────────────────────────────
# Each prints: status|latency_ms|detail
# status: pass / fail / skip

_ms_since() {
  local start="$1"
  local now
  now=$(python3 -c "import time; print(int(time.time()*1000))")
  echo $(( now - start ))
}

_now_ms() {
  python3 -c "import time; print(int(time.time()*1000))"
}

test_p1_nats() {
  local target="$1" ip="$2"
  local t0; t0=$(_now_ms)
  # Test NATS by posting to local daemon with nats-only hint.
  # Retry once because daemon may be busy with concurrent fleet traffic.
  local resp
  resp=$(curl -sf --max-time 2 -X POST "http://127.0.0.1:8855/message" \
    -H "Content-Type: application/json" \
    -d "{\"type\":\"ping\",\"from\":\"$NODE_ID\",\"to\":\"$target\",\"channel_hint\":\"nats\",\"payload\":{\"subject\":\"fallback-test-p1\",\"body\":\"nats probe\",\"test\":true}}" 2>&1) || resp=""
  if [ -z "$resp" ]; then
    sleep 0.2
    resp=$(curl -sf --max-time 2 -X POST "http://127.0.0.1:8855/message" \
      -H "Content-Type: application/json" \
      -d "{\"type\":\"ping\",\"from\":\"$NODE_ID\",\"to\":\"$target\",\"channel_hint\":\"nats\",\"payload\":{\"subject\":\"fallback-test-p1\",\"body\":\"nats probe\",\"test\":true}}" 2>&1) || resp=""
  fi
  local ms; ms=$(_ms_since "$t0")
  if echo "$resp" | python3 -c "import sys,json;d=json.load(sys.stdin);sys.exit(0 if d.get('delivered') or d.get('queued') else 1)" 2>/dev/null; then
    echo "pass|${ms}|"
  else
    local err
    err=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin).get('error','no response'))" 2>/dev/null || echo "daemon unreachable")
    echo "fail|${ms}|${err}"
  fi
}

test_p2_http() {
  local target="$1" ip="$2" port="$4" tunnel_port="$6"
  local t0; t0=$(_now_ms)
  # Try direct IP first (short timeout — LAN is fast or blocked)
  local resp
  resp=$(curl -sf --max-time 3 "http://${ip}:${port}/health" 2>&1) || resp=""
  if [ -n "$resp" ] && echo "$resp" | python3 -c "import sys,json;d=json.load(sys.stdin);sys.exit(0 if d.get('status')=='ok' else 1)" 2>/dev/null; then
    local ms; ms=$(_ms_since "$t0")
    echo "pass|${ms}|"
    return
  fi
  # Fallback: try SSH tunnel port (longer timeout — tunnels via jump host are slower)
  if [ -n "$tunnel_port" ]; then
    resp=$(curl -sf --max-time 8 "http://127.0.0.1:${tunnel_port}/health" 2>&1) || resp=""
    if [ -n "$resp" ] && echo "$resp" | python3 -c "import sys,json;d=json.load(sys.stdin);sys.exit(0 if d.get('status')=='ok' else 1)" 2>/dev/null; then
      local ms; ms=$(_ms_since "$t0")
      echo "pass|${ms}|via tunnel :${tunnel_port}"
      return
    fi
  fi
  local ms; ms=$(_ms_since "$t0")
  echo "fail|${ms}|no HTTP response from ${ip}:${port}${tunnel_port:+ or tunnel :${tunnel_port}}"
}

test_p3_chief() {
  local target="$1" ip="$2"
  local chief; chief=$(_get_chief)
  if [ -z "$chief" ]; then
    echo "skip|0|no chief configured"
    return
  fi
  # Resolve chief IP + ingest URL
  local chief_info; chief_info=$(_resolve_peer "$chief") || { echo "fail|0|chief peer unresolvable"; return; }
  local chief_ip; chief_ip=$(echo "$chief_info" | cut -d'|' -f1)
  local ingest_url; ingest_url=$(_get_chief_ingest_url)
  local t0; t0=$(_now_ms)
  local resp=""
  if [ -n "$ingest_url" ]; then
    # Modern chief path: chief daemon /message endpoint (same schema as P1/P2)
    resp=$(curl -sf --max-time 3 -X POST "$ingest_url" \
      -H "Content-Type: application/json" \
      -d "{\"type\":\"ping\",\"from\":\"$NODE_ID\",\"to\":\"$target\",\"payload\":{\"subject\":\"fallback-test-p3\",\"body\":\"chief relay probe\",\"test\":true}}" 2>&1) || resp=""
  fi
  # If modern schema call produced no response, try legacy chief-ingest schema.
  if [ -z "$resp" ] && [ -n "$ingest_url" ]; then
    resp=$(curl -sf --max-time 3 -X POST "$ingest_url" \
      -H "Content-Type: application/json" \
      -d "{\"from\":\"$NODE_ID\",\"to\":[\"$target\"],\"subject\":\"fallback-test-p3\",\"body\":\"chief relay probe\",\"test\":true}" 2>&1) || resp=""
  fi
  # Legacy fallback (older chief ingest service)
  if [ -z "$resp" ]; then
    resp=$(curl -sf --max-time 3 -X POST "http://${chief_ip}:8844/message" \
      -H "Content-Type: application/json" \
      -d "{\"from\":\"$NODE_ID\",\"to\":[\"$target\"],\"subject\":\"fallback-test-p3\",\"body\":\"chief relay probe\",\"test\":true}" 2>&1) || resp=""
  fi
  local ms; ms=$(_ms_since "$t0")
  if [ -n "$resp" ]; then
    echo "pass|${ms}|"
  else
    echo "fail|${ms}|chief relay unresponsive"
  fi
}

test_p4_seed() {
  local target="$1" ip="$2" user="$3"
  local t0; t0=$(_now_ms)
  local seed_text="Fleet fallback test from $NODE_ID at $(date -u +%H:%M:%S)"

  # Method 1: NATS-based seed_write via daemon (preferred, no SSH needed)
  local daemon_url="http://127.0.0.1:8855/message"
  local nats_result
  nats_result=$(curl -sf -m 5 -X POST "$daemon_url" \
    -H "Content-Type: application/json" \
    -d "{\"type\":\"seed_write\",\"to\":\"${target}\",\"payload\":{\"subject\":\"P4 test\",\"body\":\"${seed_text}\"}}" 2>/dev/null)
  if echo "$nats_result" | grep -q '"delivered": true\|"delivered":true'; then
    local ms; ms=$(_ms_since "$t0")
    echo "pass|${ms}|via NATS seed_write"
    return
  fi

  # Method 2: SSH fallback
  if [ -z "$user" ] || [ -z "$ip" ]; then
    local ms; ms=$(_ms_since "$t0")
    echo "fail|${ms}|NATS seed_write failed, no user/ip for SSH fallback"
    return
  fi
  local b64; b64=$(echo "$seed_text" | base64)
  ssh -o ConnectTimeout=5 -o BatchMode=yes "${target}" \
    "echo '${b64}' | base64 -d >> /tmp/fleet-seed-${target}.md" 2>/dev/null || \
  ssh -o ConnectTimeout=2 -o BatchMode=yes "${user}@${ip}" \
    "echo '${b64}' | base64 -d >> /tmp/fleet-seed-${target}.md" 2>/dev/null
  local rc=$?
  local ms; ms=$(_ms_since "$t0")
  if [ $rc -eq 0 ]; then
    echo "pass|${ms}|via SSH"
  else
    echo "fail|${ms}|both NATS seed_write and SSH failed"
  fi
}

test_p5_ssh() {
  local target="$1" ip="$2" user="$3" port="$4"
  if [ -z "$user" ] || [ -z "$ip" ]; then
    echo "fail|0|no user/ip"
    return
  fi
  local t0; t0=$(_now_ms)
  local resp
  # Use SSH host alias (target name) to pick up ~/.ssh/config (ProxyJump, etc.)
  # Fall back to user@ip if alias fails
  # max-time 6 because some daemons (mac2) take 4-5s to respond to health checks
  resp=$(ssh -o ConnectTimeout=5 -o BatchMode=yes "${target}" \
    "curl -sf --max-time 6 http://127.0.0.1:${port}/health" 2>/dev/null) || \
  resp=$(ssh -o ConnectTimeout=5 -o BatchMode=yes "${user}@${ip}" \
    "curl -sf --max-time 6 http://127.0.0.1:${port}/health" 2>/dev/null) || resp=""
  local ms; ms=$(_ms_since "$t0")
  if [ -n "$resp" ] && echo "$resp" | python3 -c "import sys,json;d=json.load(sys.stdin);sys.exit(0 if d.get('status')=='ok' else 1)" 2>/dev/null; then
    echo "pass|${ms}|"
  else
    echo "fail|${ms}|SSH or remote daemon unreachable"
  fi
}

test_p6_wol() {
  local target="$1" ip="$2" user="$3" port="$4" mac="$5"
  if [ -z "$mac" ]; then
    echo "skip|0|no MAC address"
    return
  fi
  # Check if target is already online
  if curl -sf --max-time 1 "http://${ip}:${port}/health" >/dev/null 2>&1; then
    echo "skip|0|already online"
    return
  fi
  local t0; t0=$(_now_ms)
  # Send magic packet via python
  python3 -c "
import socket
mac_bytes = bytes.fromhex('${mac}'.replace(':',''))
magic = b'\xff'*6 + mac_bytes*16
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.sendto(magic, ('<broadcast>', 9))
s.close()
print('sent')
" 2>/dev/null
  local rc=$?
  local ms; ms=$(_ms_since "$t0")
  if [ $rc -eq 0 ]; then
    echo "pass|${ms}|packet sent (wake unverified)"
  else
    echo "fail|${ms}|magic packet failed"
  fi
}

test_p7_git() {
  local target="$1"
  local t0; t0=$(_now_ms)
  local inbox_dir="$REPO_ROOT/fleet-inbox/${target}"
  mkdir -p "$inbox_dir"
  local ts; ts=$(date -u +%Y%m%dT%H%M%S)
  local test_file="$inbox_dir/${ts}-fallback-test.json"
  cat > "$test_file" << JSONEOF
{
  "type": "ping",
  "from": "$NODE_ID",
  "to": "$target",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "payload": {"subject": "fallback-test-p7", "body": "git channel probe", "test": true}
}
JSONEOF
  (
    cd "$REPO_ROOT"
    git add "$test_file" 2>/dev/null
    git commit -m "fleet-test: P7 probe $NODE_ID→$target" --no-gpg-sign -- "$test_file" >/dev/null 2>&1
    git push origin HEAD >/dev/null 2>&1
  )
  local rc=$?
  local ms; ms=$(_ms_since "$t0")
  # Cleanup: remove the test file
  if [ -f "$test_file" ]; then
    (
      cd "$REPO_ROOT"
      git rm -f "$test_file" >/dev/null 2>&1
      git commit -m "fleet-test: cleanup P7 probe" --no-gpg-sign -- "$test_file" >/dev/null 2>&1
      git push origin HEAD >/dev/null 2>&1
    ) &  # cleanup in background
  fi
  if [ $rc -eq 0 ]; then
    echo "pass|${ms}|"
  else
    echo "fail|${ms}|git commit/push failed"
  fi
}

# ── Main test runner ─────────────────────────────────────────────────────

run_test() {
  local target="$1"

  # Resolve peer info
  local peer_info; peer_info=$(_resolve_peer "$target") || {
    echo "  ${RED}Error:${RESET} Cannot resolve peer '$target' from $CONFIG"
    return 1
  }
  local ip user mac port
  ip=$(echo "$peer_info" | cut -d'|' -f1)
  user=$(echo "$peer_info" | cut -d'|' -f2)
  mac=$(echo "$peer_info" | cut -d'|' -f3)
  port=$(echo "$peer_info" | cut -d'|' -f4)
  port="${port:-8855}"
  local tunnel_port
  tunnel_port=$(echo "$peer_info" | cut -d'|' -f5)

  echo ""
  echo "${BOLD}Fleet Fallback Test → ${target}${RESET}"
  echo "════════════════════════════════"

  local total_pass=0
  local total_tested=0
  local total_ms=0
  local budget_start; budget_start=$(_now_ms)

  # Channel definitions: name|timeout_label|function
  local channels=(
    "P1 NATS|2s|test_p1_nats"
    "P2 HTTP|3s|test_p2_http"
    "P3 Chief|3s|test_p3_chief"
    "P4 Seed|2s|test_p4_seed"
    "P5 SSH|15s|test_p5_ssh"
    "P6 WoL|3s|test_p6_wol"
    "P7 Git|10s|test_p7_git"
  )

  for ch in "${channels[@]}"; do
    local name timeout_label func
    name=$(echo "$ch" | cut -d'|' -f1)
    timeout_label=$(echo "$ch" | cut -d'|' -f2)
    func=$(echo "$ch" | cut -d'|' -f3)

    # Check 60s budget (increased from 30s — SSH timeouts were starving P6/P7)
    local elapsed_ms; elapsed_ms=$(( $(_now_ms) - budget_start ))
    if [ "$elapsed_ms" -ge 60000 ]; then
      printf "  %-10s %s %s\n" "$name" "$SYM_SKIP" "budget exhausted"
      continue
    fi

    local result
    result=$($func "$target" "$ip" "$user" "$port" "$mac" "$tunnel_port" 2>/dev/null) || result="fail|0|exception"

    local status latency detail
    status=$(echo "$result" | cut -d'|' -f1)
    latency=$(echo "$result" | cut -d'|' -f2)
    detail=$(echo "$result" | cut -d'|' -f3-)

    case "$status" in
      pass)
        total_pass=$((total_pass + 1))
        total_tested=$((total_tested + 1))
        total_ms=$((total_ms + latency))
        if [ -n "$detail" ]; then
          printf "  %-10s %s ${DIM}%sms${RESET} %s\n" "$name" "$SYM_PASS" "$latency" "$detail"
        else
          printf "  %-10s %s ${DIM}%sms${RESET}\n" "$name" "$SYM_PASS" "$latency"
        fi
        ;;
      fail)
        total_tested=$((total_tested + 1))
        total_ms=$((total_ms + latency))
        printf "  %-10s %s %s\n" "$name" "$SYM_FAIL" "$detail"
        ;;
      skip)
        printf "  %-10s %s skipped (%s)\n" "$name" "$SYM_SKIP" "$detail"
        ;;
    esac
  done

  local budget_elapsed_ms; budget_elapsed_ms=$(( $(_now_ms) - budget_start ))
  local budget_s; budget_s=$(python3 -c "print(f'{${budget_elapsed_ms}/1000:.1f}')")

  echo "════════════════════════════════"
  echo "  Score: ${total_pass}/${total_tested} | Budget: ${budget_s}s / 60s"
  echo ""
}

# ── Entry point ──────────────────────────────────────────────────────────

if [ ! -f "$CONFIG" ]; then
  echo "Error: $CONFIG not found. Run: cp .multifleet/config.template.json .multifleet/config.json"
  exit 1
fi

TARGET="${1:-}"

if [ -z "$TARGET" ] || [ "$TARGET" = "ALL_NODES" ]; then
  echo "${BOLD}Fleet Fallback Test — All Peers${RESET}"
  echo "From: $NODE_ID"
  peers=$(_get_all_peers)
  if [ -z "$peers" ]; then
    echo "No peers found (or all peers are this node: $NODE_ID)"
    exit 0
  fi
  while IFS= read -r peer; do
    run_test "$peer"
  done <<< "$peers"
else
  if [ "$TARGET" = "$NODE_ID" ]; then
    echo "Warning: testing self ($NODE_ID). Results may not be meaningful."
  fi
  echo "From: $NODE_ID"
  run_test "$TARGET"
fi
