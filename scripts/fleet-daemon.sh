#!/bin/bash
# fleet-daemon.sh — fleet daemon ops swiss-army.
#
# Usage:
#   fleet-daemon.sh status          # one-line health summary
#   fleet-daemon.sh peers           # list peers + lastSeen
#   fleet-daemon.sh metrics [grep]  # show selected counters
#   fleet-daemon.sh logs [N]        # tail N lines (default 30) of fleet-nerve + fleet-nats logs
#   fleet-daemon.sh start           # bootstrap fleet-nerve plist
#   fleet-daemon.sh stop            # bootout fleet-nerve (no auto-respawn)
#   fleet-daemon.sh restart         # bootout + bootstrap + /health poll
#   fleet-daemon.sh which           # which label currently owns :8855
#   fleet-daemon.sh dup-check       # detect duplicate plist crash-loop
#
# Flags (apply to all subcommands):
#   --label <name>     # default io.contextdna.fleet-nats
#   --daemon <url>     # default $TRIALBENCH_DAEMON_URL or http://127.0.0.1:8855
#   --timeout <sec>    # health-poll timeout, default 30
#
# Exit codes per subcommand match the underlying op (0 ok, non-zero on failure).

set -uo pipefail

LABEL="io.contextdna.fleet-nats"
DAEMON="${TRIALBENCH_DAEMON_URL:-http://127.0.0.1:8855}"
HEALTH_TIMEOUT=30
LOG_LINES=30

# Color helpers (TTY-aware)
if [[ -t 1 ]]; then
    R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; B='\033[1m'; N='\033[0m'
else
    R=''; G=''; Y=''; C=''; B=''; N=''
fi

# Parse global flags first (subcommand expected as first non-flag arg)
SUBCMD=""
ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --label) LABEL="$2"; shift 2 ;;
        --daemon) DAEMON="$2"; shift 2 ;;
        --timeout) HEALTH_TIMEOUT="$2"; shift 2 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        --) shift; while [[ $# -gt 0 ]]; do ARGS+=("$1"); shift; done ;;
        *) if [[ -z "$SUBCMD" ]]; then SUBCMD="$1"; else ARGS+=("$1"); fi; shift ;;
    esac
done

PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

# ---- Helpers ---------------------------------------------------------------

# Health JSON via urllib (bypasses RTK proxy preview). Returns JSON or empty.
_health_json() {
    python3 -c "
import urllib.request, sys
try:
    sys.stdout.write(urllib.request.urlopen('${DAEMON}/health', timeout=2).read().decode())
except Exception as e:
    sys.exit(1)
" 2>/dev/null
}

# Pretty-print one health field via python3
_health_field() {
    local expr="$1"; local fallback="${2:-?}"
    python3 -c "
import urllib.request, json, sys
try:
    d = json.loads(urllib.request.urlopen('${DAEMON}/health', timeout=2).read())
    print(${expr})
except Exception:
    print('${fallback}')
" 2>/dev/null
}

_poll_health_ok() {
    local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
    while [[ $(date +%s) -lt $deadline ]]; do
        if [[ "$(_health_field "d.get('status','')" "")" == "ok" ]]; then return 0; fi
        sleep 2
    done
    return 1
}

# ---- Subcommands -----------------------------------------------------------

cmd_status() {
    local s; s=$(_health_field "d.get('status','?')")
    local up; up=$(_health_field "d.get('uptime_s','?')")
    local n;  n=$(_health_field "d.get('nodeId','?')")
    local p;  p=$(_health_field "','.join(d.get('peers',{}).keys()) or 'none'")
    if [[ "$s" == "ok" ]]; then
        echo -e "${G}✓${N} node=$n uptime=${up}s peers=[${p}] daemon=$DAEMON"
        return 0
    else
        echo -e "${R}✗${N} status=$s — daemon=$DAEMON"
        return 1
    fi
}

cmd_peers() {
    python3 -c "
import urllib.request, json
d = json.loads(urllib.request.urlopen('${DAEMON}/health', timeout=2).read())
peers = d.get('peers',{}) or {}
if not peers: print('  (no peers)'); raise SystemExit(0)
for name, info in sorted(peers.items()):
    if not isinstance(info, dict): continue
    ls = info.get('lastSeen')
    ls_str = f'{ls}s' if isinstance(ls, (int,float)) else 'n/a'
    src = info.get('source','?')
    sess = info.get('sessions',0)
    print(f'  {name:8s}  lastSeen={ls_str:>6s}  sess={sess}  source={src}')
" 2>&1 | head -10
}

cmd_metrics() {
    local pattern="${ARGS[0]:-fleet_nerve_(bridge|webhook|queue|peers|nats_resub)}"
    curl -sf --max-time 3 "${DAEMON}/metrics" 2>/dev/null \
        | grep -E "$pattern" | head -40 \
        || { echo -e "${R}✗${N} /metrics fetch failed" >&2; return 1; }
}

cmd_logs() {
    local n="${ARGS[0]:-$LOG_LINES}"
    for f in /tmp/fleet-nerve.log /tmp/fleet-nats.log; do
        if [[ -f "$f" ]]; then
            echo -e "${C}── ${f} (last ${n} lines) ──${N}"
            tail -n "$n" "$f"
            echo
        fi
    done
}

cmd_which() {
    # Detect who actually owns :8855 + cross-check launchctl
    local pid
    pid=$(lsof -iTCP:8855 -sTCP:LISTEN -n -P 2>/dev/null | awk 'NR>1 {print $2; exit}')
    if [[ -z "$pid" ]]; then
        echo -e "${R}✗${N} nothing listening on :8855"; return 1
    fi
    local cmd; cmd=$(ps -o command= -p "$pid" 2>/dev/null | head -c 100)
    echo "  pid:    $pid"
    echo "  cmd:    ${cmd:-?}"
    echo "  via launchctl:"
    launchctl list 2>/dev/null | grep "io.contextdna" | sed 's/^/    /'
}

cmd_dup_check() {
    # Detect duplicate plist crash-loop pattern (both fleet-nerve + fleet-nats loaded)
    local nerve nats
    nerve=$(launchctl list | awk '$3=="io.contextdna.fleet-nerve" {print $1}')
    nats=$(launchctl list | awk '$3=="io.contextdna.fleet-nats" {print $1}')
    if [[ -n "$nerve" && "$nerve" != "-" && -n "$nats" && "$nats" != "-" ]]; then
        echo -e "${Y}⚠${N} DUP detected: fleet-nerve=PID ${nerve}, fleet-nats=PID ${nats}"
        echo "  one is in respawn-loop. Heal:"
        echo -e "    ${B}bash scripts/fleet-daemon.sh stop --label io.contextdna.fleet-nats${N}"
        return 1
    fi
    echo -e "${G}✓${N} no DUP — only one fleet plist loaded"
    return 0
}

cmd_stop() {
    [[ -f "$PLIST" ]] || { echo -e "${R}✗${N} plist not found: $PLIST" >&2; return 2; }
    local rc=0
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>&1; rc=$?
    if [[ $rc -eq 0 ]]; then
        echo -e "${G}✓${N} bootout: $LABEL"
    elif [[ $rc -eq 113 ]]; then
        echo -e "${Y}—${N} already not loaded: $LABEL"
        rc=0
    else
        echo -e "${R}✗${N} bootout rc=$rc" >&2
    fi
    return $rc
}

cmd_start() {
    [[ -f "$PLIST" ]] || { echo -e "${R}✗${N} plist not found: $PLIST" >&2; return 2; }
    if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>&1; then
        echo -e "${G}✓${N} bootstrap: $LABEL"
        if _poll_health_ok; then
            cmd_status
        else
            echo -e "${Y}⚠${N} bootstrapped but /health did not return ok within ${HEALTH_TIMEOUT}s" >&2
            return 3
        fi
    else
        echo -e "${R}✗${N} bootstrap failed" >&2
        return 1
    fi
}

cmd_restart() {
    cmd_stop || true
    sleep 1
    cmd_start
}

# ---- Dispatch --------------------------------------------------------------

case "${SUBCMD:-status}" in
    status)     cmd_status ;;
    peers)      cmd_peers ;;
    metrics)    cmd_metrics ;;
    logs)       cmd_logs ;;
    which)      cmd_which ;;
    dup-check)  cmd_dup_check ;;
    stop)       cmd_stop ;;
    start)      cmd_start ;;
    restart)    cmd_restart ;;
    *)          echo -e "${R}✗${N} unknown subcommand: $SUBCMD" >&2
                echo "run with --help for usage" >&2
                exit 1 ;;
esac
