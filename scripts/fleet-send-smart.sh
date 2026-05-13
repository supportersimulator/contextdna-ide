#!/usr/bin/env bash
# fleet-send-smart.sh — hybrid layered fleet-comm router (VV8).
#
# Per 3-Surgeons consensus (Cardio 0.85 efficiency-first, Neuro 0.80 redundancy-
# for-critical, both agree at the boundary):
#
#   CRITICAL (alert/diagnostic/coord/repair/sync/aaron_action) → mode=redundant
#     L1: fleet-broadcast.sh (5 channels parallel)           [PRIMARY]
#     L2: daemon POST /message + parallel reliability probe  [FALLBACK]
#     L3: P7 git push to .fleet-messages/<peer>/             [LAST RESORT]
#
#   ROUTINE (status/context/health/fleet-state) → mode=efficient
#     L1: daemon POST /message + bundle_thread_id            [PRIMARY: HHH1+GGG2]
#     L2: fleet-broadcast.sh                                 [FALLBACK]
#     L3: P7 git push                                        [LAST RESORT]
#
# ZSF: every layer attempt bumps /tmp/fleet-send-smart.count.<layer>_{ok,fail}.
#
# Usage:
#   bash scripts/fleet-send-smart.sh <peer> <critical|routine|auto> <subject> [body]
#
# Companion to: scripts/fleet-broadcast.sh, daemon /message, .fleet-messages/<peer>/

set -uo pipefail

PEER="${1:-}"
URGENCY="${2:-auto}"
SUBJECT="${3:-}"
BODY="${4:-}"

if [[ -z "$PEER" || -z "$URGENCY" || -z "$SUBJECT" ]]; then
    echo "Usage: $0 <peer> <critical|routine|auto> <subject> [body]" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
COUNTER="/tmp/fleet-send-smart.count"
LOG="/tmp/fleet-send-smart-$TS.log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
bump() {
    local f="${COUNTER}.${1}"
    local cur=0
    [[ -f "$f" ]] && cur=$(cat "$f" 2>/dev/null || echo 0)
    [[ "$cur" =~ ^[0-9]+$ ]] || cur=0
    echo $((cur + 1)) > "${f}.tmp.$$" && mv -f "${f}.tmp.$$" "$f" 2>/dev/null || true
}

# Auto-classify by subject keyword.
if [[ "$URGENCY" == "auto" ]]; then
    case "$SUBJECT" in
        *alert*|*ALERT*|*diagnostic*|*coord*|*repair*|*sync*|*aaron_action*|*urgent*|*critical*|*HEAL*)
            URGENCY="critical" ;;
        *) URGENCY="routine" ;;
    esac
    log "auto-classified: urgency=$URGENCY (subject=\"$SUBJECT\")"
fi

PAYLOAD_FILE="/tmp/fleet-send-smart-$TS.body.txt"
[[ -n "$BODY" ]] && echo "$BODY" > "$PAYLOAD_FILE" || echo "(no body)" > "$PAYLOAD_FILE"

# ── Layer 1: daemon POST /message ──────────────────────────────────────────
try_daemon() {
    local body
    body=$(cat "$PAYLOAD_FILE")
    local result
    result=$(python3 - "$PEER" "$SUBJECT" "$body" "$URGENCY" <<'PY' 2>&1
import urllib.request, json, sys, time
peer, subject, body, urgency = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
payload = {
    "type": "context", "to": peer, "from": "auto",
    "payload": {"subject": subject, "body": body},
    "bundle_thread_id": f"smart-{urgency}-{peer}",
    "urgency": urgency,
}
try:
    req = urllib.request.Request("http://127.0.0.1:8855/message",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    r = urllib.request.urlopen(req, timeout=8)
    elapsed_ms = int((time.time() - t0) * 1000)
    d = json.loads(r.read())
    if d.get("delivered"):
        print(f"OK channel={d.get('channel','?')} elapsed_ms={elapsed_ms}")
    else:
        print(f"FAIL errors={d.get('errors')}")
except Exception as e:
    print(f"FAIL:{type(e).__name__}:{e}")
PY
)
    if [[ "$result" == OK* ]]; then
        bump "daemon_ok"
        log "L1 daemon: $result"
        return 0
    fi
    bump "daemon_fail"
    log "L1 daemon FAIL: $result"
    return 1
}

# ── Layer 2: fleet-broadcast.sh ────────────────────────────────────────────
try_broadcast() {
    if [[ ! -x "$REPO_ROOT/scripts/fleet-broadcast.sh" ]]; then
        log "L2 broadcast: missing — skip"
        bump "broadcast_missing"
        return 1
    fi
    local body
    body=$(cat "$PAYLOAD_FILE")
    if bash "$REPO_ROOT/scripts/fleet-broadcast.sh" "$PEER" "$SUBJECT" "$body" 2>&1 | tee -a "$LOG" | grep -q "delivered via [1-9]"; then
        bump "broadcast_ok"
        return 0
    fi
    bump "broadcast_fail"
    return 1
}

# ── Layer 3: P7 git push ───────────────────────────────────────────────────
try_git() {
    local inbox_dir="$REPO_ROOT/.fleet-messages/$PEER"
    mkdir -p "$inbox_dir" 2>/dev/null
    local file="$inbox_dir/${TS}-smart-${URGENCY}.md"
    {
        echo "# $SUBJECT"
        echo "**TS**: $TS  **From**: $(hostname -s)  **To**: $PEER  **Urgency**: $URGENCY  **Layer**: P7-git"
        echo ""
        cat "$PAYLOAD_FILE"
    } > "$file"
    if [[ -f "$file" ]]; then
        bump "git_ok"
        log "L3 git: written $file"
        return 0
    fi
    bump "git_fail"
    return 1
}

# ── Smart routing ─────────────────────────────────────────────────────────
bump "total_invocations"
log "smart-send start: peer=$PEER urgency=$URGENCY subject=\"$SUBJECT\""

if [[ "$URGENCY" == "critical" ]]; then
    log "mode=redundant (critical) — broadcast PRIMARY"
    if try_broadcast; then
        bump "delivered_via_L1_broadcast"
        log "delivered via L1 broadcast (primary for critical)"
        try_daemon || true  # parallel probe — keeps HHH1 reliability data fresh
        exit 0
    fi
    log "L1 broadcast failed — fall to L2 daemon"
    if try_daemon; then
        bump "delivered_via_L2_daemon_fallback"
        log "delivered via L2 daemon (fallback)"
        exit 0
    fi
    log "L2 daemon failed — fall to L3 git"
    if try_git; then
        bump "delivered_via_L3_git_critical_lastresort"
        log "delivered via L3 git only (critical queued for peer poll)"
        exit 0
    fi
    bump "all_layers_failed_critical"
    log "ALL 3 LAYERS FAILED"
    exit 1
fi

# routine
log "mode=efficient (routine) — daemon PRIMARY"
if try_daemon; then
    bump "delivered_via_L1_daemon"
    log "delivered via L1 daemon (primary for routine)"
    exit 0
fi
log "L1 daemon failed — fall to L2 broadcast"
if try_broadcast; then
    bump "delivered_via_L2_broadcast_fallback"
    log "delivered via L2 broadcast (fallback)"
    exit 0
fi
log "L2 broadcast failed — fall to L3 git"
if try_git; then
    bump "delivered_via_L3_git_routine_lastresort"
    log "delivered via L3 git only (routine queued for peer poll)"
    exit 0
fi
bump "all_layers_failed_routine"
log "ALL 3 LAYERS FAILED"
exit 1
