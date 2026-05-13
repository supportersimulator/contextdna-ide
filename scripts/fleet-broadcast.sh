#!/usr/bin/env bash
# fleet-broadcast.sh — fire ALL working channels in parallel to a target peer.
#
# Self-healing variant of fleet-send.sh: instead of first-success-wins, this
# fires P1 NATS + P2 HTTP-LAN + P4 seed (via SSH) + P5 SSH + P6 WoL all at
# once. Every channel's verdict surfaces in the output. ZSF: no silent skips.
#
# Use when: diagnostic / alert / cross-node coord / aaron-action-required.
# Aaron 2026-05-12: "use ALL channels until fixed — no human in the middle."
#
# Usage:
#   bash scripts/fleet-broadcast.sh <peer> "<subject>" "<body>"
#   bash scripts/fleet-broadcast.sh mac1 "subject" "$(cat /tmp/body.txt)"
#
# Resolves peer LAN IP + MAC from .multifleet/config.json.

set -uo pipefail

PEER="${1:-}"
SUBJECT="${2:-}"
BODY="${3:-}"

if [[ -z "$PEER" || -z "$SUBJECT" ]]; then
    echo "Usage: $0 <peer> <subject> [body]" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
COUNTER="/tmp/fleet-broadcast.count"
LOG="/tmp/fleet-broadcast-$TS.log"

# Resolve peer config (LAN IP + MAC + ssh_user).
PEER_CFG=$(python3 - "$PEER" <<'EOF'
import json, sys
peer = sys.argv[1]
try:
    d = json.load(open(".multifleet/config.json"))
    n = d.get("nodes", {}).get(peer, {})
    print(f"{n.get('lan_ip','')}|{n.get('mac_address','')}|{n.get('user','aarontjomsland')}")
except Exception as e:
    print(f"||")
EOF
)
IFS='|' read -r LAN_IP MAC_ADDR SSH_USER <<< "$PEER_CFG"

if [[ -z "$LAN_IP" ]]; then
    echo "[broadcast] no LAN IP for peer=$PEER in .multifleet/config.json" >&2
    exit 1
fi

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

log "broadcast start: peer=$PEER ip=$LAN_IP subject=\"$SUBJECT\""

# Stage payload once.
PAYLOAD_FILE="/tmp/fleet-broadcast-$TS.body.txt"
{
    echo "# $SUBJECT"
    echo ""
    echo "**TS**: $TS"
    echo "**From**: $(hostname -s)  →  **To**: $PEER"
    echo "**Sent via fleet-broadcast.sh — all channels parallel**"
    echo ""
    echo "---"
    echo ""
    [[ -n "$BODY" ]] && echo "$BODY" || echo "(no body)"
} > "$PAYLOAD_FILE"

# Channel results — plain vars for bash 3.2 compat (macOS default /bin/bash).
RESULTS_P1=""
RESULTS_P2=""
RESULTS_P4=""
RESULTS_P5=""
RESULTS_P6=""

# ── P1 NATS pub/sub (parallel) ─────────────────────────────────────────────
(
    PYTHONPATH="$REPO_ROOT" python3 -u - "$PEER" "$SUBJECT" "$PAYLOAD_FILE" <<'PY'
import asyncio, json, sys, time
peer, subject, payload_file = sys.argv[1], sys.argv[2], sys.argv[3]
body = open(payload_file).read()
async def go():
    try:
        from nats.aio.client import Client as NATS
        nc = NATS(); await nc.connect("nats://127.0.0.1:4222", connect_timeout=3)
        env = {"from": "broadcast", "to": peer, "type": "context",
               "subject": subject, "body": body, "ts": time.time(),
               "_via": "fleet-broadcast.sh"}
        for subj in (f"fleet.message.{peer}.context", f"fleet.message.{peer}"):
            await nc.publish(subj, json.dumps(env).encode())
        await nc.flush(timeout=2)
        await nc.close()
        print("OK")
    except Exception as e:
        print(f"FAIL:{type(e).__name__}:{e}")

asyncio.run(go())
PY
) >/tmp/fbc-$TS-P1.out 2>&1 &
PID_P1=$!

# ── P2 HTTP-LAN direct ─────────────────────────────────────────────────────
(
    python3 - "$LAN_IP" "$PEER" "$SUBJECT" "$PAYLOAD_FILE" <<'PY'
import urllib.request, json, sys, time
ip, peer, subject, payload_file = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
body = open(payload_file).read()
try:
    payload = {"type": "context", "to": peer, "from": "mac2",
               "payload": {"subject": subject, "body": body, "_via": "P2_http_lan"}}
    req = urllib.request.Request(f"http://{ip}:8855/message",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    r = urllib.request.urlopen(req, timeout=5)
    elapsed_ms = int((time.time() - t0) * 1000)
    resp = r.read().decode()[:300]
    print(f"OK status={r.status} {elapsed_ms}ms resp={resp}")
except Exception as e:
    print(f"FAIL:{type(e).__name__}:{e}")
PY
) >/tmp/fbc-$TS-P2.out 2>&1 &
PID_P2=$!

# ── P4 seed file via SSH (P5 carrier) ──────────────────────────────────────
(
    SEED_PATH="$REPO_ROOT/.fleet-messages/$PEER/${TS}-broadcast-$(echo "$SUBJECT" | tr -c '[:alnum:]' '_' | head -c 40).md"
    if ssh -o BatchMode=yes -o ConnectTimeout=4 "$SSH_USER@$LAN_IP" \
        "mkdir -p \$(dirname '$SEED_PATH') && cat > '$SEED_PATH'" < "$PAYLOAD_FILE" 2>/tmp/fbc-$TS-P4err; then
        echo "OK seed=$SEED_PATH"
    else
        echo "FAIL:ssh_or_write_failed:$(cat /tmp/fbc-$TS-P4err 2>/dev/null | head -c 200)"
    fi
) >/tmp/fbc-$TS-P4.out 2>&1 &
PID_P4=$!

# ── P5 SSH direct echo to /tmp ─────────────────────────────────────────────
(
    REMOTE_TMP="/tmp/fleet-broadcast-from-mac2-$TS.txt"
    if ssh -o BatchMode=yes -o ConnectTimeout=4 "$SSH_USER@$LAN_IP" \
        "cat > '$REMOTE_TMP' && echo 'OK $REMOTE_TMP'" < "$PAYLOAD_FILE" 2>/tmp/fbc-$TS-P5err; then
        echo "OK tmp=$REMOTE_TMP"
    else
        echo "FAIL:ssh_failed:$(cat /tmp/fbc-$TS-P5err 2>/dev/null | head -c 200)"
    fi
) >/tmp/fbc-$TS-P5.out 2>&1 &
PID_P5=$!

# ── P6 Wake-on-LAN (probe-only when peer alive) ────────────────────────────
(
    if [[ -n "$MAC_ADDR" ]] && command -v wakeonlan >/dev/null 2>&1; then
        out=$(wakeonlan "$MAC_ADDR" 2>&1)
        echo "OK $out"
    else
        echo "SKIP no MAC or wakeonlan absent"
    fi
) >/tmp/fbc-$TS-P6.out 2>&1 &
PID_P6=$!

# Await all channels with a budget.
wait $PID_P1 $PID_P2 $PID_P4 $PID_P5 $PID_P6 2>/dev/null

# Collect results (bash 3.2 compat — plain vars).
RESULTS_P1=$(cat "/tmp/fbc-$TS-P1.out" 2>/dev/null | tr -d '\n' | head -c 400)
RESULTS_P2=$(cat "/tmp/fbc-$TS-P2.out" 2>/dev/null | tr -d '\n' | head -c 400)
RESULTS_P4=$(cat "/tmp/fbc-$TS-P4.out" 2>/dev/null | tr -d '\n' | head -c 400)
RESULTS_P5=$(cat "/tmp/fbc-$TS-P5.out" 2>/dev/null | tr -d '\n' | head -c 400)
RESULTS_P6=$(cat "/tmp/fbc-$TS-P6.out" 2>/dev/null | tr -d '\n' | head -c 400)

# Atomic counter per outcome.
bump() {
    local key="$1"
    local f="${COUNTER}.${key}"
    local cur=0
    [[ -f "$f" ]] && cur=$(cat "$f" 2>/dev/null || echo 0)
    [[ "$cur" =~ ^[0-9]+$ ]] || cur=0
    echo $((cur + 1)) > "${f}.tmp.$$" && mv -f "${f}.tmp.$$" "$f" 2>/dev/null || true
}

echo ""
log "broadcast results (target=$PEER):"
SUCCESS_COUNT=0
TOTAL=5
for ch in P1 P2 P4 P5 P6; do
    # bash 3.2 compat — indirect var expansion via eval.
    eval "result=\"\${RESULTS_${ch}}\""
    if [[ "$result" == OK* ]]; then
        log "  $ch: OK"
        bump "${ch}_ok"
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    else
        log "  $ch: ${result:-<empty>}"
        bump "${ch}_fail"
    fi
done
log "delivered via $SUCCESS_COUNT/$TOTAL channels (P7 skipped — Aaron rule)"

# Aggregate counter.
bump "total_broadcasts"
if [[ $SUCCESS_COUNT -ge 1 ]]; then
    bump "delivered_any"
    exit 0
fi
bump "all_failed"
exit 1
