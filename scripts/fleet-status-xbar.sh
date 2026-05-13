#!/bin/bash
# <xbar.title>Fleet Status</xbar.title>
# <xbar.version>v2.0.0</xbar.version>
# <xbar.desc>Multi-fleet node status</xbar.desc>

DAEMON=http://127.0.0.1:8855
PY=/usr/bin/python3

# Auto-detect repo path
REPO=""
for d in "$HOME/dev/er-simulator-superrepo" "$HOME/Documents/er-simulator-superrepo"; do
    [ -d "$d" ] && REPO="$d" && break
done
[ -z "$REPO" ] && REPO="$HOME/dev/er-simulator-superrepo"

health=$(curl -sf --max-time 4 "$DAEMON/health" 2>/dev/null)

if [ -z "$health" ]; then
    echo "F:? | color=red"
    echo "---"
    echo "Fleet daemon offline | color=red"
    echo "Start Daemon | bash=/bin/bash param1=-c param2='cd $REPO && NATS_URL=nats://127.0.0.1:4222 python3 tools/fleet_nerve_nats.py serve' terminal=true"
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
    icon = "G" if ls < 30 else "Y" if ls < 300 else "R"
    if ls < 60: seen = f"{ls}s"
    elif ls < 3600: seen = f"{ls//60}m"
    elif ls < 86400: seen = f"{ls//3600}h"
    else: seen = f"{ls//86400}d"
    sess = p.get("sessions", 0)
    print(f"PEER|{clean(name, 10)}|{icon}|{seen}|{sess}")
' 2>/dev/null)

# Eval key=value pairs
eval "$(echo "$parsed" | grep -E '^[A-Z_]+=' | head -13)"

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

# === Actions ===
echo "---"
echo "Run Fleet Check | bash=/bin/bash param1=-c param2='cd $REPO && bash scripts/fleet-check.sh' terminal=true"
echo "Open Arbiter | bash=/usr/bin/open param1=$DAEMON/arbiter terminal=false"
echo "Open Dashboard | bash=/usr/bin/open param1=$DAEMON/dashboard terminal=false"
echo "---"
echo "Restart Daemon | bash=/bin/bash param1=-c param2='cd $REPO && pkill -f fleet_nerve_nats; sleep 1; NATS_URL=nats://127.0.0.1:4222 python3 tools/fleet_nerve_nats.py serve' terminal=true"
echo "Refresh | refresh=true"
