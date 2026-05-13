#!/usr/bin/env bash
# nats-channel-smoke.sh — NATS connectivity smoke test
# ZSF: every check is independent; failures are logged but never fatal
# Output: PASS/FAIL per check with color, suitable for fleet-check.sh integration
# Usage: bash scripts/nats-channel-smoke.sh [--node <node_id>] [--quiet]

set -uo pipefail

# ── PATH: add common nats CLI locations ──────────────────────────────────────
# nats CLI may live in ~/go/bin or ~/.nats/bin — neither is in default PATH
for _nats_dir in "$HOME/go/bin" "$HOME/.nats/bin" /usr/local/bin /opt/homebrew/bin; do
  if [[ -x "${_nats_dir}/nats" ]] && [[ ":$PATH:" != *":${_nats_dir}:"* ]]; then
    export PATH="${_nats_dir}:${PATH}"
  fi
done

# ── Colors ──────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[0;33m'
  CYAN='\033[0;36m'
  BOLD='\033[1m'
  RESET='\033[0m'
else
  GREEN='' RED='' YELLOW='' CYAN='' BOLD='' RESET=''
fi

# ── Args ─────────────────────────────────────────────────────────────────────
NODE_ID="${MULTIFLEET_NODE_ID:-}"
QUIET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --node) NODE_ID="$2"; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    *) shift ;;
  esac
done

# Derive node id from hostname if not set
if [[ -z "$NODE_ID" ]]; then
  NODE_ID="$(hostname -s 2>/dev/null || echo "unknown")"
fi

NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
NATS_MON="${NATS_MON_URL:-http://127.0.0.1:8222}"

# ── Counters ─────────────────────────────────────────────────────────────────
PASS=0
FAIL=0
WARN=0
RESULTS=()

# ── Helpers ──────────────────────────────────────────────────────────────────
log() { [[ "$QUIET" -eq 0 ]] && echo -e "$*" || true; }

check_pass() {
  local label="$1"
  PASS=$((PASS + 1))
  RESULTS+=("${GREEN}[PASS]${RESET} $label")
  log "${GREEN}[PASS]${RESET} $label"
}

check_fail() {
  local label="$1"
  local detail="${2:-}"
  FAIL=$((FAIL + 1))
  local msg="${RED}[FAIL]${RESET} $label"
  [[ -n "$detail" ]] && msg+=" — $detail"
  RESULTS+=("$msg")
  log "$msg"
}

check_warn() {
  local label="$1"
  local detail="${2:-}"
  WARN=$((WARN + 1))
  local msg="${YELLOW}[WARN]${RESET} $label"
  [[ -n "$detail" ]] && msg+=" — $detail"
  RESULTS+=("$msg")
  log "$msg"
}

# ── Header ────────────────────────────────────────────────────────────────────
log ""
log "${BOLD}${CYAN}=== NATS Channel Smoke Test ===${RESET}"
log "  Node   : ${NODE_ID}"
log "  NATS   : ${NATS_URL}"
log "  Monitor: ${NATS_MON}"
log "  Time   : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
log ""

# ── Check 1: NATS healthz endpoint ───────────────────────────────────────────
{
  resp=$(curl -sf --max-time 3 "${NATS_MON}/healthz" 2>/dev/null) || true
  if [[ -n "$resp" ]]; then
    check_pass "NATS healthz reachable (${NATS_MON}/healthz)"
  else
    check_fail "NATS healthz unreachable" "${NATS_MON}/healthz did not respond"
  fi
} || check_fail "NATS healthz check" "unexpected error"

# ── Check 2: NATS server info via monitoring ──────────────────────────────────
{
  varz=$(curl -sf --max-time 3 "${NATS_MON}/varz" 2>/dev/null) || true
  if [[ -n "$varz" ]]; then
    server_name=$(echo "$varz" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('server_name','?'))" 2>/dev/null || echo "?")
    check_pass "NATS varz readable (server: ${server_name})"
  else
    check_warn "NATS varz unavailable" "monitoring endpoint may be restricted"
  fi
} || check_warn "NATS varz check" "parse error"

# ── Check 3: Publish to fleet.event.smoke_test.<node> ────────────────────────
{
  SMOKE_SUBJECT="fleet.event.smoke_test.${NODE_ID}"
  SMOKE_PAYLOAD="{\"node\":\"${NODE_ID}\",\"ts\":\"$(date -u '+%Y-%m-%dT%H:%M:%SZ')\",\"check\":\"smoke\"}"
  if command -v nats &>/dev/null; then
    out=$(nats pub "$SMOKE_SUBJECT" "$SMOKE_PAYLOAD" \
      --server "$NATS_URL" 2>&1) || true
    if echo "$out" | grep -qi "error\|connection refused\|timeout"; then
      check_fail "Publish to ${SMOKE_SUBJECT}" "$out"
    else
      check_pass "Publish to ${SMOKE_SUBJECT}"
    fi
  else
    check_warn "nats CLI not found" "install nats CLI to enable publish checks"
  fi
} || check_fail "Smoke publish check" "unexpected error"

# ── Check 4: JetStream stream list ───────────────────────────────────────────
{
  if command -v nats &>/dev/null; then
    js_out=$(nats stream ls --server "$NATS_URL" 2>&1) || true
    if echo "$js_out" | grep -qi "error\|no streams\|not enabled\|connection refused"; then
      check_warn "JetStream stream list" "$js_out"
    else
      stream_count=$(echo "$js_out" | grep -c "^[[:space:]]*[A-Z]" 2>/dev/null || echo "?")
      check_pass "JetStream stream list (approx ${stream_count} streams visible)"
    fi
  else
    check_warn "JetStream stream ls skipped" "nats CLI not found"
  fi
} || check_warn "JetStream stream list" "unexpected error"

# ── Check 5: FLEET_EVENTS stream info ────────────────────────────────────────
{
  if command -v nats &>/dev/null; then
    fe_out=$(nats stream info FLEET_EVENTS --server "$NATS_URL" 2>&1) || true
    if echo "$fe_out" | grep -qi "not found\|error\|connection refused"; then
      check_warn "FLEET_EVENTS stream" "stream not found or not reachable"
    else
      check_pass "FLEET_EVENTS stream exists"

      # ── Check 6: Replica count ≥ 3 ─────────────────────────────────────────
      {
        replicas=$(echo "$fe_out" | grep -i "^[[:space:]]*Replication:" | awk '{print $NF}' 2>/dev/null || echo "")
        if [[ -z "$replicas" ]]; then
          replicas=$(echo "$fe_out" | grep -i "replication" | grep -oE '[0-9]+' | head -1 || echo "")
        fi
        if [[ -z "$replicas" ]]; then
          check_warn "FLEET_EVENTS replicas" "could not parse Replication field"
        elif [[ "$replicas" -ge 3 ]]; then
          check_pass "FLEET_EVENTS replicas ≥ 3 (R=${replicas})"
        elif [[ "$replicas" -ge 1 ]]; then
          check_warn "FLEET_EVENTS replicas < 3 (R=${replicas})" "cluster may be degraded or single-node"
        else
          check_fail "FLEET_EVENTS replicas" "invalid replica count: ${replicas}"
        fi
      } || check_warn "FLEET_EVENTS replica check" "unexpected error"
    fi
  else
    check_warn "FLEET_EVENTS check skipped" "nats CLI not found"
  fi
} || check_warn "FLEET_EVENTS stream info" "unexpected error"

# ── Check 7: Publish to fleet.heartbeat.<node> ────────────────────────────────
{
  HB_SUBJECT="fleet.heartbeat.${NODE_ID}"
  HB_PAYLOAD="{\"node\":\"${NODE_ID}\",\"ts\":\"$(date -u '+%Y-%m-%dT%H:%M:%SZ')\",\"type\":\"smoke_heartbeat\"}"
  if command -v nats &>/dev/null; then
    hb_out=$(nats pub "$HB_SUBJECT" "$HB_PAYLOAD" \
      --server "$NATS_URL" 2>&1) || true
    if echo "$hb_out" | grep -qi "error\|connection refused\|timeout"; then
      check_fail "Heartbeat publish to ${HB_SUBJECT}" "$hb_out"
    else
      check_pass "Heartbeat publish to ${HB_SUBJECT}"
    fi
  else
    check_warn "Heartbeat publish skipped" "nats CLI not found"
  fi
} || check_fail "Heartbeat publish check" "unexpected error"

# ── Check 8: NATS cluster routes (if clustered) ───────────────────────────────
{
  routez=$(curl -sf --max-time 3 "${NATS_MON}/routez" 2>/dev/null) || true
  if [[ -n "$routez" ]]; then
    route_count=$(echo "$routez" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    routes = d.get('routes', [])
    print(len(routes))
except Exception:
    print('?')
" 2>/dev/null || echo "?")
    if [[ "$route_count" == "?" ]]; then
      check_warn "NATS cluster routes" "could not parse routez response"
    elif [[ "$route_count" -eq 0 ]]; then
      check_warn "NATS cluster routes" "0 routes (single-node or isolated)"
    else
      check_pass "NATS cluster routes: ${route_count} peer(s) connected"
    fi
  else
    check_warn "NATS routez unavailable" "may be single-node or monitoring restricted"
  fi
} || check_warn "NATS cluster routes check" "unexpected error"

# ── Summary ──────────────────────────────────────────────────────────────────
TOTAL=$((PASS + FAIL + WARN))
log ""
log "${BOLD}=== Summary: ${TOTAL} checks ===${RESET}"
log "  ${GREEN}PASS${RESET}: ${PASS}"
log "  ${YELLOW}WARN${RESET}: ${WARN}"
log "  ${RED}FAIL${RESET}: ${FAIL}"
log ""

# Machine-readable line for fleet-check.sh parsing
echo "NATS_SMOKE: PASS=${PASS} WARN=${WARN} FAIL=${FAIL} NODE=${NODE_ID}"

# Exit code: 0 if no hard failures, 1 if any FAIL
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
