#!/bin/bash
# <xbar.title>Fleet Status</xbar.title>
# <xbar.version>v2.2.0</xbar.version>
# <xbar.desc>Multi-fleet node status — HHH4/GGG3/VV8 counters surfaced</xbar.desc>

DAEMON=http://127.0.0.1:8855
PY=/usr/bin/python3

# Auto-detect repo path
REPO=""
for d in "$HOME/dev/er-simulator-superrepo" "$HOME/Documents/er-simulator-superrepo"; do
    [ -d "$d" ] && REPO="$d" && break
done
[ -z "$REPO" ] && REPO="$HOME/dev/er-simulator-superrepo"

health=$(curl -sf --max-time 8 "$DAEMON/health" 2>/dev/null)

if [ -z "$health" ]; then
    echo "F:? | color=red"
    echo "---"
    echo "Fleet daemon offline | color=red"
    echo "Start Daemon | bash=$REPO/scripts/fleet-daemon.sh param1=start terminal=true"
    echo "Refresh | refresh=true"
    exit 0
fi

# Parse all fields in single python call, sanitize for xbar parser
parsed=$(echo "$health" | $PY -c '
import sys, json, re

def clean(s, n=60):
    if s is None: return "?"
    s = str(s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[|]", "/", s)
    return s[:n]

try:
    d = json.load(sys.stdin)
except Exception:
    print("ERROR")
    sys.exit(0)

node = clean(d.get("nodeId", "?"), 20)
# Extract node number (e.g. "mac2" -> 2)
node_num = "".join(c for c in node if c.isdigit()) or "?"
status = clean(d.get("status", "?"), 20)
transport = clean(d.get("transport", "?"), 20)
active = d.get("activeSessions", 0)
uptime = int(d.get("uptime_s", 0))

peers = d.get("peers", {}) or {}
online = sum(1 for p in peers.values() if isinstance(p, dict) and int(p.get("lastSeen", 9999)) < 120)

git = d.get("git", {}) or {}
branch = clean(git.get("branch", "?"), 30)

surgeons = d.get("surgeons", {}) or {}
neuro = clean(surgeons.get("neurologist", "?"), 15)
cardio = clean(surgeons.get("cardiologist", "?"), 15)

stats = d.get("stats", {}) or {}
errs = stats.get("errors", 0)
sent = stats.get("sent", 0)
recv = stats.get("received", 0)

# HHH4 channel_priority_modules counters
cpm = d.get("channel_priority_modules", {}) or {}
db = cpm.get("delta_bundle", {}) or {}
cs = cpm.get("channel_scoring", {}) or {}
vsx = cpm.get("vscode_superset", {}) or {}
db_n = int(db.get("delta_bundle_compose_full_total", 0)) + int(db.get("delta_bundle_compose_delta_total", 0))
cs_n = int(cs.get("channel_scoring_calls_total", 0))
vsx_n = int(vsx.get("vscode_superset_push_task_total", 0)) + int(vsx.get("vscode_superset_push_prompt_total", 0))
vsx_err = int(vsx.get("vscode_superset_push_task_errors_total", 0)) + int(vsx.get("vscode_superset_push_prompt_errors_total", 0))

# GGG3 plist_drift counters
drift_now = int(stats.get("plist_drift_currently_drifted", 0))
drift_checks = int(stats.get("plist_drift_checks_total", 0))
drift_detected = int(stats.get("plist_drift_detected_total", 0))

print(f"DB_N={db_n}")
print(f"CS_N={cs_n}")
print(f"VSX_N={vsx_n}")
print(f"VSX_ERR={vsx_err}")
print(f"DRIFT_NOW={drift_now}")
print(f"DRIFT_CHECKS={drift_checks}")
print(f"DRIFT_DETECTED={drift_detected}")

print(f"NODE={node}")
print(f"STATUS={status}")
print(f"TRANSPORT={transport}")
print(f"ACTIVE={active}")
print(f"UPTIME={uptime}")
print(f"ONLINE={online}")
print(f"TOTAL_PEERS={len(peers)}")
print(f"NODE_NUM={node_num}")
print(f"FLEET_SIZE={1 + len(peers)}")
print(f"BRANCH={branch}")
print(f"NEURO={neuro}")
print(f"CARDIO={cardio}")
print(f"ERRORS={errs}")
print(f"SENT={sent}")
print(f"RECV={recv}")

for name, p in sorted(peers.items()):
    if not isinstance(p, dict): continue
    ls = int(p.get("lastSeen", 9999))
    st = clean(p.get("status", ""), 30)
    # Wave-P v2.2.0: distinguish heartbeat vs config-only peers so xbar
    # never shows "No peers" when daemon clearly knows about them.
    if st == "configured-not-yet-seen":
        icon = "?"
        seen = "cfg"
    elif st == "tracked-no-heartbeat":
        icon = "T"  # cluster tracks, no heartbeat — partition or daemon-just-up
        seen = "trk"
    else:
        icon = "G" if ls < 30 else "Y" if ls < 300 else "R"
        if ls < 60: seen = f"{ls}s"
        elif ls < 3600: seen = f"{ls//60}m"
        elif ls < 86400: seen = f"{ls//3600}h"
        else: seen = f"{ls//86400}d"
    sess = p.get("sessions", 0)
    print(f"PEER|{clean(name, 10)}|{icon}|{seen}|{sess}")
' 2>/dev/null)

# Eval key=value pairs (extended for v2.1.0 counters)
eval "$(echo "$parsed" | grep -E '^[A-Z_]+=' | head -25)"

# VV8 smart-router counters (read /tmp/fleet-send-smart.count.* files)
SR_INV=0; SR_L1D=0; SR_L1B=0; SR_L2FB=0; SR_FAIL=0
[ -f /tmp/fleet-send-smart.count.total_invocations ] && SR_INV=$(cat /tmp/fleet-send-smart.count.total_invocations 2>/dev/null | tr -d '[:space:]')
[ -f /tmp/fleet-send-smart.count.delivered_via_L1_daemon ] && SR_L1D=$(cat /tmp/fleet-send-smart.count.delivered_via_L1_daemon 2>/dev/null | tr -d '[:space:]')
[ -f /tmp/fleet-send-smart.count.delivered_via_L1_broadcast ] && SR_L1B=$(cat /tmp/fleet-send-smart.count.delivered_via_L1_broadcast 2>/dev/null | tr -d '[:space:]')
[ -f /tmp/fleet-send-smart.count.delivered_via_L2_broadcast_fallback ] && SR_L2FB=$(cat /tmp/fleet-send-smart.count.delivered_via_L2_broadcast_fallback 2>/dev/null | tr -d '[:space:]')
[ -f /tmp/fleet-send-smart.count.daemon_fail ] && SR_FAIL=$(cat /tmp/fleet-send-smart.count.daemon_fail 2>/dev/null | tr -d '[:space:]')
SR_INV=${SR_INV:-0}; SR_L1D=${SR_L1D:-0}; SR_L1B=${SR_L1B:-0}; SR_L2FB=${SR_L2FB:-0}; SR_FAIL=${SR_FAIL:-0}

# Last smart-router activity (mtime of total_invocations file)
SR_LAST="never"
if [ -f /tmp/fleet-send-smart.count.total_invocations ]; then
    sr_age_s=$(( $(date +%s) - $(stat -f %m /tmp/fleet-send-smart.count.total_invocations 2>/dev/null || echo 0) ))
    if [ "$sr_age_s" -lt 60 ]; then SR_LAST="${sr_age_s}s ago"
    elif [ "$sr_age_s" -lt 3600 ]; then SR_LAST="$((sr_age_s/60))m ago"
    elif [ "$sr_age_s" -lt 86400 ]; then SR_LAST="$((sr_age_s/3600))h ago"
    else SR_LAST="$((sr_age_s/86400))d ago"
    fi
fi

fleet_size=${FLEET_SIZE:-1}
node_num=${NODE_NUM:-"?"}

# Color logic: green = all peers online, orange = some, red = none or daemon down
if [ "$STATUS" = "ok" ]; then
    if [ "${ONLINE:-0}" -eq "${TOTAL_PEERS:-0}" ] || [ "${TOTAL_PEERS:-0}" = "0" ]; then
        color=green
    elif [ "${ONLINE:-0}" -gt 0 ]; then
        color=orange
    else
        color=red
    fi
else
    color=red
fi

# Uptime format
up_h=$((UPTIME / 3600))
up_m=$(((UPTIME % 3600) / 60))
if [ "$up_h" -gt 0 ]; then
    uptime_str="${up_h}h ${up_m}m"
else
    uptime_str="${up_m}m"
fi

# === MENU BAR ===
echo "F:${node_num}/${fleet_size} | color=$color"
echo "---"

# === Header ===
if [ "$STATUS" = "ok" ]; then
    echo "Fleet $NODE | size=14 color=green"
else
    echo "Fleet $NODE | size=14 color=red"
fi
echo "Transport: $TRANSPORT  Uptime: $uptime_str | size=11 color=gray"
echo "Branch: $BRANCH  Sessions: $ACTIVE | size=11 color=gray"

# === Peers ===
echo "---"
if [ "${TOTAL_PEERS:-0}" = "0" ]; then
    echo "No peers | color=gray"
else
    echo "Peers ($ONLINE/$TOTAL_PEERS online) | size=12"
    echo "$parsed" | grep '^PEER|' | while IFS='|' read -r _ name icon seen sess; do
        case "$icon" in
            G) dot="[ok]" ;;
            Y) dot="[..]" ;;
            R) dot="[--]" ;;
            T) dot="[trk]" ;;
            "?") dot="[cfg]" ;;
            *) dot="[??]" ;;
        esac
        echo "  $dot $name  $sess sess  $seen ago | size=11"
    done
fi

# === Surgeons ===
echo "---"
echo "3-Surgeons | size=12"
[ "$NEURO" = "ok" ] && ni="[ok]" || ni="[--]"
[ "$CARDIO" = "ok" ] && ci="[ok]" || ci="[--]"
echo "  $ni Neurologist: $NEURO | size=11"
echo "  $ci Cardiologist: $CARDIO | size=11"
echo "  [ok] Atlas: this session | size=11"

# === Stats ===
echo "---"
echo "Msgs: ${SENT:-0} sent / ${RECV:-0} recv | size=11"
if [ "${ERRORS:-0}" -gt 0 ]; then
    echo "Errors: $ERRORS | size=11 color=red"
else
    echo "Errors: 0 | size=11 color=gray"
fi

# === Channel Modules (HHH4) ===
vsx_color="gray"
[ "${VSX_ERR:-0}" -gt 0 ] && vsx_color="red"
echo "Channels: dB:${DB_N:-0} cs:${CS_N:-0} vsx:${VSX_N:-0} | size=11 color=$vsx_color"

# === Plist Drift (GGG3 / M3) ===
if [ "${DRIFT_NOW:-0}" = "1" ]; then
    echo "Drift: DRIFT checks:${DRIFT_CHECKS:-0} detected:${DRIFT_DETECTED:-0} | size=11 color=red"
else
    echo "Drift: ok checks:${DRIFT_CHECKS:-0} detected:${DRIFT_DETECTED:-0} | size=11 color=gray"
fi

# === Smart-Router (VV8) ===
sr_color="gray"
[ "${SR_L2FB:-0}" -gt 0 ] && sr_color="orange"
[ "${SR_FAIL:-0}" -gt 0 ] && sr_color="red"
echo "Smart-router: inv:${SR_INV} L1d:${SR_L1D} L1b:${SR_L1B} L2-fb:${SR_L2FB} last:${SR_LAST} | size=11 color=$sr_color"

# === Actions ===
echo "---"
echo "Run Fleet Check | bash=/bin/bash param1=-c param2='cd $REPO && bash scripts/fleet-check.sh' terminal=true"
echo "Open Arbiter | bash=/usr/bin/open param1=$DAEMON/arbiter terminal=false"
echo "Open Dashboard | bash=/usr/bin/open param1=$DAEMON/dashboard terminal=false"
echo "Open Health JSON | bash=/usr/bin/open param1=$DAEMON/health terminal=false"
echo "---"
echo "Broadcast All Channels (mac1) | bash=/bin/bash param1=-c param2='cd $REPO && bash scripts/fleet-broadcast.sh mac1 \"xbar manual broadcast\" \"manual ping from xbar\" | tee /tmp/xbar-broadcast.log; read -p \"[enter to close]\"' terminal=true"
echo "Smart-Send Critical (mac1) | bash=/bin/bash param1=-c param2='cd $REPO && bash scripts/fleet-send-smart.sh mac1 critical \"xbar smart-send\" \"manual smart-send from xbar\" | tee /tmp/xbar-smart.log; read -p \"[enter to close]\"' terminal=true"
echo "Tail Smart-Router Counters | bash=/bin/bash param1=-c param2='watch -n2 \"ls -la /tmp/fleet-send-smart.count.* 2>/dev/null; echo; for f in /tmp/fleet-send-smart.count.*; do echo \\\"\$(basename \$f): \$(cat \$f)\\\"; done\"' terminal=true"
echo "---"
echo "Restart Daemon | bash=$REPO/scripts/fleet-daemon.sh param1=restart terminal=true"
echo "Run Status | bash=$REPO/scripts/fleet-daemon.sh param1=status terminal=true"
echo "DUP-Check | bash=$REPO/scripts/fleet-daemon.sh param1=dup-check terminal=true"
echo "View Logs | bash=$REPO/scripts/fleet-daemon.sh param1=logs param2=50 terminal=true"
echo "Refresh | refresh=true"
