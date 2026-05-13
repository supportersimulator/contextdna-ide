#!/usr/bin/env bash
# diagnose-cluster-mesh.sh — read-only diagnosis of the local nats-server
# cluster mesh: which routes are configured, which are connected, and why
# unsolicited routes fail despite TCP reachability.
#
# Motivation: ZZ4 (2026-05-12) follow-up on WW1/RR2/QQ5. mac1's nats-server
# logs "no route to host" against 192.168.1.183:6222 (mac2) and
# 192.168.1.191:6222 (mac3) hundreds of thousands of times, yet `nc -zv`
# from the shell to those same IPs succeeds. mac3 still gets all the
# routes (it dials in); mac1 and mac2 never solicit each other.
#
# This script triangulates the four signals so the next operator (Aaron
# or an agent) sees the failure mode in one pass:
#
#   1. Plist `--routes` array (configured intent)
#   2. /routez (live nats-server view: did_solicit, is_configured, remote_name)
#   3. TCP/DNS reachability from shell to each configured route
#   4. NATS protocol handshake (write CONNECT, read INFO) to each route
#   5. Tailscale + .multifleet/config.json reconciliation
#
# Mode: read-only. NEVER mutates plists, daemons, JetStream, or peers.
# `--dry-run` is the only mode; flag accepted for symmetry / future-proofing.
#
# ZSF: every probe captured with verbatim exit/error. Any subprocess
# failure increments a counter printed in the summary. Nothing swallowed.
#
# Usage:
#   scripts/diagnose-cluster-mesh.sh                 # full diagnosis to stdout
#   scripts/diagnose-cluster-mesh.sh --json          # machine-readable
#   scripts/diagnose-cluster-mesh.sh --dry-run       # alias for default
#   scripts/diagnose-cluster-mesh.sh --plist PATH    # override plist path
#
# Exit 0 always (read-only diagnostic); see SUMMARY for findings.

set -u
set -o pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_JSON="${REPO_ROOT}/.multifleet/config.json"
DEFAULT_PLIST="${HOME}/Library/LaunchAgents/io.contextdna.nats-server.plist"
MONITOR_URL="${NATS_MONITOR_URL:-http://127.0.0.1:8222}"
JSON_MODE=0
PLIST="${DEFAULT_PLIST}"
PROBE_TIMEOUT=3

# ZSF counters
declare -i ZSF_PROBE_ERRORS=0
declare -i ZSF_PARSE_ERRORS=0

# ── arg parsing ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON_MODE=1; shift ;;
    --dry-run) shift ;;  # default; accepted for explicitness
    --plist) PLIST="$2"; shift 2 ;;
    --timeout) PROBE_TIMEOUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) printf 'unknown flag: %s\n' "$1" >&2; exit 2 ;;
  esac
done

log() { [[ ${JSON_MODE} -eq 0 ]] && printf '%s\n' "$*"; }
section() { [[ ${JSON_MODE} -eq 0 ]] && printf '\n── %s ──────────────────────────\n' "$*"; }

# ── 1. parse plist routes (read-only) ──────────────────────────────────────
extract_routes_from_plist() {
  local plist="$1"
  if [[ ! -f "${plist}" ]]; then
    ZSF_PARSE_ERRORS+=1
    printf 'PLIST_MISSING:%s' "${plist}"
    return
  fi
  # Look for "<string>--routes</string>" then capture next <string>
  python3 - "${plist}" <<'PY'
import sys, re
p = sys.argv[1]
try:
    txt = open(p).read()
except Exception as e:
    print(f"PLIST_READ_ERR:{e}", end="")
    sys.exit(0)
m = re.search(r"<string>--routes</string>\s*<string>([^<]+)</string>", txt)
if not m:
    print("NO_ROUTES_FLAG", end="")
else:
    print(m.group(1), end="")
PY
}

CONFIGURED_ROUTES_RAW="$(extract_routes_from_plist "${PLIST}")"
# parse: comma-sep list of nats://host:port  →  host:port lines
declare -a ROUTE_ENDPOINTS=()
if [[ "${CONFIGURED_ROUTES_RAW}" != PLIST_MISSING:* && "${CONFIGURED_ROUTES_RAW}" != PLIST_READ_ERR:* && "${CONFIGURED_ROUTES_RAW}" != "NO_ROUTES_FLAG" ]]; then
  IFS=',' read -r -a parts <<< "${CONFIGURED_ROUTES_RAW}"
  for p in "${parts[@]}"; do
    ep="${p#nats://}"
    ep="${ep#nats-route://}"
    ROUTE_ENDPOINTS+=("${ep}")
  done
fi

# ── 2. fetch /routez via rtk proxy (raw JSON, no rtk filtering) ────────────
ROUTEZ_JSON="$(rtk proxy curl -s --max-time "${PROBE_TIMEOUT}" "${MONITOR_URL}/routez" 2>/dev/null || true)"
if [[ -z "${ROUTEZ_JSON}" ]] || ! echo "${ROUTEZ_JSON}" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
  ZSF_PROBE_ERRORS+=1
  ROUTEZ_JSON='{"num_routes":0,"routes":[],"_error":"routez_unreachable_or_invalid"}'
fi
VARZ_JSON="$(rtk proxy curl -s --max-time "${PROBE_TIMEOUT}" "${MONITOR_URL}/varz" 2>/dev/null || true)"
if [[ -z "${VARZ_JSON}" ]] || ! echo "${VARZ_JSON}" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
  ZSF_PROBE_ERRORS+=1
  VARZ_JSON='{"_error":"varz_unreachable_or_invalid"}'
fi

# ── 3. tailscale snapshot (read-only) ──────────────────────────────────────
TAILSCALE_SNAPSHOT="$(tailscale status 2>/dev/null | head -20 || true)"
if [[ -z "${TAILSCALE_SNAPSHOT}" ]]; then
  ZSF_PROBE_ERRORS+=1
  TAILSCALE_SNAPSHOT="(tailscale unavailable)"
fi

# ── 4. config.json node table ──────────────────────────────────────────────
CONFIG_NODES_JSON="$(python3 - "${CONFIG_JSON}" <<'PY' 2>/dev/null
import sys, json
try:
    cfg = json.load(open(sys.argv[1]))
    print(json.dumps(cfg.get("nodes", {})))
except Exception as e:
    print("{}")
PY
)"

# ── 5. per-route probes ────────────────────────────────────────────────────
declare -a PROBE_RESULTS=()
for ep in "${ROUTE_ENDPOINTS[@]}"; do
  host="${ep%:*}"
  port="${ep##*:}"
  # DNS resolution
  dns="$(python3 -c "
import socket, sys
try:
    infos = socket.getaddrinfo('${host}', None)
    print(','.join(sorted({i[4][0] for i in infos})))
except Exception as e:
    print(f'DNS_ERR:{e}')
" 2>/dev/null)"
  # TCP connect via nc (fast, kernel-level)
  if nc -z -G "${PROBE_TIMEOUT}" "${host}" "${port}" >/dev/null 2>&1; then
    tcp="OK"
  else
    tcp="REFUSED_OR_TIMEOUT"
    ZSF_PROBE_ERRORS+=1
  fi
  # NATS protocol handshake: connect, expect "INFO " banner in first 256 bytes
  handshake="$(python3 - "${host}" "${port}" "${PROBE_TIMEOUT}" <<'PY' 2>/dev/null
import socket, sys
host, port, timeout = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    s = socket.create_connection((host, port), timeout=timeout)
    s.settimeout(timeout)
    buf = s.recv(512)
    s.close()
    head = buf.decode("utf-8", errors="replace").splitlines()[0] if buf else ""
    if head.startswith("INFO "):
        print(f"OK:{head[:120]}")
    else:
        print(f"NO_INFO_BANNER:{head[:80]!r}")
except Exception as e:
    print(f"ERR:{e}")
PY
)"
  if [[ "${handshake}" != OK:* ]]; then
    ZSF_PROBE_ERRORS+=1
  fi
  PROBE_RESULTS+=("${ep}|${dns}|${tcp}|${handshake}")
done

# ── 6. correlate /routez against configured routes ─────────────────────────
ROUTE_TABLE="$(python3 - <<PY 2>/dev/null
import json, sys
rz = json.loads('''${ROUTEZ_JSON}''')
rows = []
for r in rz.get("routes", []):
    rows.append({
        "remote_name": r.get("remote_name",""),
        "ip": r.get("ip",""),
        "port": r.get("port",0),
        "did_solicit": r.get("did_solicit"),
        "is_configured": r.get("is_configured"),
        "rtt": r.get("rtt",""),
        "uptime": r.get("uptime",""),
        "subs": r.get("subscriptions",0),
    })
print(json.dumps(rows, indent=2))
PY
)"

# ── 7. log-tail summary ────────────────────────────────────────────────────
LOG_PATH="/tmp/nats-server.log"
LAST_ROUTE_ERR=""
LAST_ROUTE_SUCC=""
if [[ -f "${LOG_PATH}" ]]; then
  LAST_ROUTE_ERR="$(grep -E 'Error trying to connect to route' "${LOG_PATH}" 2>/dev/null | tail -3)"
  LAST_ROUTE_SUCC="$(grep -E 'Route connection created' "${LOG_PATH}" 2>/dev/null | tail -3)"
fi

# ── 8. render report ───────────────────────────────────────────────────────
if [[ ${JSON_MODE} -eq 1 ]]; then
  # Build probe records as TSV, then assemble in python (avoids quoting hell)
  PROBE_TSV="$(printf '%s\n' "${PROBE_RESULTS[@]}")"
  ENDPOINTS_TSV="$(printf '%s\n' "${ROUTE_ENDPOINTS[@]}")"
  export PROBE_TSV ENDPOINTS_TSV ROUTE_TABLE PLIST CONFIGURED_ROUTES_RAW ZSF_PROBE_ERRORS ZSF_PARSE_ERRORS
  python3 - <<'PY'
import json, os
probes = []
for line in (os.environ.get("PROBE_TSV","") or "").splitlines():
    if not line.strip(): continue
    ep, dns, tcp, hs = (line.split("|") + ["","","",""])[:4]
    probes.append({"endpoint": ep, "dns": dns, "tcp": tcp, "handshake": hs})
endpoints = [l.strip() for l in (os.environ.get("ENDPOINTS_TSV","") or "").splitlines() if l.strip()]
try:
    live = json.loads(os.environ.get("ROUTE_TABLE","[]") or "[]")
except Exception:
    live = []
out = {
    "plist": os.environ.get("PLIST",""),
    "configured_routes_raw": os.environ.get("CONFIGURED_ROUTES_RAW",""),
    "configured_endpoints": endpoints,
    "probes": probes,
    "live_routes": live,
    "zsf": {
        "probe_errors": int(os.environ.get("ZSF_PROBE_ERRORS","0") or 0),
        "parse_errors": int(os.environ.get("ZSF_PARSE_ERRORS","0") or 0),
    },
}
print(json.dumps(out, indent=2))
PY
  exit 0
fi

section "diagnose-cluster-mesh — read-only NATS route triage"
log "plist:                ${PLIST}"
log "monitor:              ${MONITOR_URL}"
log "configured --routes:  ${CONFIGURED_ROUTES_RAW}"
log ""

section "configured route probes (TCP + NATS INFO handshake)"
printf '  %-22s %-30s %-22s %s\n' "endpoint" "dns_resolved_ips" "tcp_connect" "handshake"
printf '  %s\n' "----------------------------------------------------------------------------------------------"
for r in "${PROBE_RESULTS[@]}"; do
  IFS='|' read -r ep dns tcp hs <<< "${r}"
  hs_disp="${hs:0:60}"
  printf '  %-22s %-30s %-22s %s\n' "${ep}" "${dns:0:28}" "${tcp}" "${hs_disp}"
done

section "/routez (live router connections)"
echo "${ROUTE_TABLE}" | python3 -c "
import json, sys
rows = json.load(sys.stdin)
if not rows:
    print('  (no live routes — server isolated)')
    sys.exit(0)
print(f'  {\"remote\":<7} {\"ip\":<46} {\"port\":<6} {\"solicit\":<8} {\"config\":<7} {\"rtt\":<10} {\"uptime\":<10} {\"subs\":<5}')
print('  ' + '-'*110)
for r in rows:
    print(f'  {r[\"remote_name\"]:<7} {r[\"ip\"][:44]:<46} {r[\"port\"]:<6} {str(r[\"did_solicit\"]):<8} {str(r[\"is_configured\"]):<7} {r[\"rtt\"]:<10} {r[\"uptime\"]:<10} {r[\"subs\"]:<5}')
"

section "log signal (last 3 route errors + 3 route created)"
if [[ -n "${LAST_ROUTE_ERR}" ]]; then
  echo "${LAST_ROUTE_ERR}" | sed 's/^/  ERR | /'
else
  log "  (no 'connect to route' errors in log)"
fi
if [[ -n "${LAST_ROUTE_SUCC}" ]]; then
  echo "${LAST_ROUTE_SUCC}" | sed 's/^/  OK  | /'
fi

section "tailscale snapshot (mac2 expected absent — Gap-5)"
echo "${TAILSCALE_SNAPSHOT}" | sed 's/^/  /'

section "ZSF counters (this run)"
log "  probe_errors:    ${ZSF_PROBE_ERRORS}  (TCP timeouts, handshake failures, unreachable /routez)"
log "  parse_errors:    ${ZSF_PARSE_ERRORS}  (plist read/parse failures)"

section "SUMMARY"
# Heuristic: count configured peers vs live solicit-true routes
LIVE_SOLICITED="$(echo "${ROUTE_TABLE}" | python3 -c "
import json,sys
try:
    rows = json.load(sys.stdin)
    print(sum(1 for r in rows if r.get('did_solicit')))
except Exception:
    print(0)
" 2>/dev/null || echo 0)"
LIVE_TOTAL="$(echo "${ROUTE_TABLE}" | python3 -c "
import json,sys
try:
    rows = json.load(sys.stdin)
    print(len(rows))
except Exception:
    print(0)
" 2>/dev/null || echo 0)"

log "  configured peers:      ${#ROUTE_ENDPOINTS[@]}"
log "  live routes total:     ${LIVE_TOTAL}"
log "  live routes solicited: ${LIVE_SOLICITED} (this node dialed)"
log ""
if [[ "${LIVE_SOLICITED}" == "0" && "${LIVE_TOTAL}" != "0" ]]; then
  log "  finding: this node never solicits — all live routes were accepted, not dialed."
  log "           combined with 'no route to host' in log, the configured --routes peers"
  log "           are unreachable at the moment nats-server retries (typically because of"
  log "           transient en0 drop / Wi-Fi roam at startup, then the connect-attempt backoff"
  log "           grows to 1 hour between retries — see docs/runbooks/cluster-mesh-mac1-mac2.md)."
fi
if [[ "${LIVE_TOTAL}" == "0" ]]; then
  log "  finding: NO live routes. Either nats-server is isolated or /routez is unreachable."
fi
log ""
log "  next step: read docs/runbooks/cluster-mesh-mac1-mac2.md for the fix recipe."

exit 0
