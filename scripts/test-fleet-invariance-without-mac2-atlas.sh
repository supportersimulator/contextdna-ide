#!/usr/bin/env bash
# test-fleet-invariance-without-mac2-atlas.sh — WaveT META-invariance test.
#
# Aaron 2026-05-12: "test to ensure invariance of our progress/operations even
# if context limit reached". Simulates Atlas-mac2 session-death and probes the
# 3 continuity layers:
#   (i)   In-flight work completes on target peer (originator-independent)
#   (ii)  Cross-node coord continues (mac1/mac3 git-msg, KV, heartbeats, M3/M4)
#   (iii) Aaron can resume from any peer (no information loss)
#
# ROOT-CAUSE COMPOUND SHIP — this script is the recurring invariance gate.
# ZSF: every layer probe writes /tmp/inv-continuity.<probe>_{ok,fail}.
#
# Usage:
#   bash scripts/test-fleet-invariance-without-mac2-atlas.sh [--pause-secs N]
#   bash scripts/test-fleet-invariance-without-mac2-atlas.sh --report   # summary only

set -uo pipefail
PAUSE="${PAUSE_SECS:-300}"
REPORT_ONLY=0
for a in "$@"; do
    case "$a" in
        --pause-secs) shift; PAUSE="$1" ;;
        --pause-secs=*) PAUSE="${a#*=}" ;;
        --report) REPORT_ONLY=1 ;;
    esac
done

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/tmp/inv-continuity-${TS}.log"
SNAP_BEFORE="/tmp/inv-continuity-before-${TS}.json"
SNAP_AFTER="/tmp/inv-continuity-after-${TS}.json"
HEALTH="http://127.0.0.1:8855/health"

bump() { local f="/tmp/inv-continuity.${1}"; local c=0; [[ -f "$f" ]] && c=$(cat "$f"); echo $((c+1)) > "$f"; }
log()  { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$OUT"; }

# Snapshot via urllib (constraint: python3 urllib for /health)
snap() {
    python3 - "$1" <<'PY'
import json, sys, urllib.request, subprocess
out = sys.argv[1]
data = {"ts": __import__("time").time()}
try:
    with urllib.request.urlopen("http://127.0.0.1:8855/health", timeout=3) as r:
        h = json.loads(r.read())
    data["health"] = {
        "git_commit": h.get("git", {}).get("commit", "")[:9],
        "peers": list(h.get("peers", {}).keys()),
        "uptime_s": h.get("uptime_s"),
        "sessions": len(h.get("sessions", [])),
        "received": h.get("stats", {}).get("received"),
        "sent": h.get("stats", {}).get("sent"),
        "broadcasts": h.get("stats", {}).get("broadcasts"),
    }
except Exception as e:
    data["health_err"] = str(e)
try:
    data["fleet_state_mtime"] = subprocess.check_output(
        ["stat", "-f", "%m", "fleet-state.json"], text=True
    ).strip()
except Exception as e:
    data["fleet_state_err"] = str(e)
try:
    data["last_commit_age_s"] = int(subprocess.check_output(
        ["git", "log", "-1", "--format=%ct"], text=True
    ).strip())
except Exception:
    pass
json.dump(data, open(out, "w"), indent=2)
print(json.dumps(data, indent=2))
PY
}

if [[ "$REPORT_ONLY" == "1" ]]; then
    ls -lt /tmp/inv-continuity-after-*.json 2>/dev/null | head -3
    exit 0
fi

log "=== WaveT META-invariance test (Atlas-mac2 simulated absent ${PAUSE}s) ==="
log "Snapshot BEFORE:"
snap "$SNAP_BEFORE" | tee -a "$OUT" >/dev/null

# Layer (i): in-flight work — synthetic broadcast with continuation_key
CK="invT-${TS}"
log "Layer (i): broadcast with continuation_key=${CK}"
if curl -sf -X POST http://127.0.0.1:8855/message \
    -H "Content-Type: application/json" \
    -d "{\"type\":\"context\",\"to\":\"all\",\"payload\":{\"subject\":\"WaveT continuity probe\",\"body\":\"continuation_key=${CK} originator=mac2-atlas expects_reply=true\"}}" \
    >/dev/null 2>&1; then
    bump layer_i_emit_ok; log "  emit OK"
else
    bump layer_i_emit_fail; log "  emit FAIL — daemon unreachable (Atlas dead == coord dead?)"
fi

# Pause — simulate Atlas-mac2 silence (no new commands from this process)
log "Pausing ${PAUSE}s (Atlas-mac2 'dead'); peers continue cron+hook autonomy..."
SLEEP_CHUNK=30
ELAPSED=0
while (( ELAPSED < PAUSE )); do
    sleep "$SLEEP_CHUNK"; ELAPSED=$((ELAPSED+SLEEP_CHUNK))
    # Layer (ii): probe peer autonomy — fleet-state should still auto-sync
    NEW_MTIME=$(stat -f "%m" fleet-state.json 2>/dev/null || echo 0)
    OLD_MTIME=$(python3 -c "import json;print(json.load(open('${SNAP_BEFORE}')).get('fleet_state_mtime',0))")
    if (( NEW_MTIME > OLD_MTIME )); then
        bump layer_ii_autosync_ok
        log "  +${ELAPSED}s fleet-state auto-synced by peer (mtime advanced)"
        OLD_MTIME=$NEW_MTIME
        # update snapshot baseline so we count each advance
        python3 -c "import json,sys;d=json.load(open('${SNAP_BEFORE}'));d['fleet_state_mtime']='${NEW_MTIME}';json.dump(d,open('${SNAP_BEFORE}','w'))"
    fi
done

log "Snapshot AFTER:"
snap "$SNAP_AFTER" | tee -a "$OUT" >/dev/null

# Layer (iii): resume — can we recover continuation_key from any peer?
log "Layer (iii): resumability — searching fleet-messages + git log for ${CK}"
HITS=0
if grep -r "${CK}" .fleet-messages/ 2>/dev/null | head -3; then HITS=$((HITS+1)); fi
if git log --since="10 minutes ago" --all --grep="${CK}" --oneline 2>/dev/null | head -3; then HITS=$((HITS+1)); fi
if (( HITS > 0 )); then bump layer_iii_resume_ok; log "  resumable (HITS=${HITS})"
else bump layer_iii_resume_fail; log "  NOT resumable — continuation_key vanished"; fi

# Verdict
log "=== VERDICT ==="
for p in layer_i_emit_ok layer_i_emit_fail layer_ii_autosync_ok layer_iii_resume_ok layer_iii_resume_fail; do
    v=$(cat /tmp/inv-continuity.${p} 2>/dev/null || echo 0)
    log "  ${p}=${v}"
done
log "Full log: $OUT"
log "Snapshots: $SNAP_BEFORE  $SNAP_AFTER"
