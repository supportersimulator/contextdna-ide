#!/usr/bin/env bash
# refresh-mac3.sh — M4 orchestrator (2026-05-06 auto-heal plan).
#
# Detect mac3 last_seen divergence from this node; run
# sprint-aaron-actions.sh --apply --no-prompt on mac3 via the best
# available channel and ship the log artifact back.
#
# Channel choice:
#   P5 SSH direct (192.168.1.191) first — sub-second, deterministic.
#   P7 fleet-message (via scripts/fleet-send.sh) fallback when SSH fails.
#
# Idempotency: sprint-aaron-actions.sh is itself idempotent (each step
# emits OK / SKIP / FAIL / MANUAL per cycle-6 F1 spec).
#
# Counter: /tmp/mac3-refresh-runs.count (atomic). Bumped on every outcome
# (ok / ssh_fail / send_fail / sprint_fail / divergence_clean / config_err).
# Best-effort NATS event publish: fleet.event.mac3_refresh.<outcome>.
#
# ZSF: every code path bumps an observable counter + logs a single line.
#
# Usage:
#   bash scripts/refresh-mac3.sh                 # dry-run (probe + report only)
#   bash scripts/refresh-mac3.sh --apply         # apply if diverged
#   bash scripts/refresh-mac3.sh --apply --force # apply unconditionally
#   bash scripts/refresh-mac3.sh --help

set -uo pipefail

DRY_RUN=1
FORCE=0
DIVERGENCE_THRESHOLD_S="${MAC3_REFRESH_DIVERGENCE_S:-300}"  # 5min default

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)    DRY_RUN=0; shift ;;
        --force)    FORCE=1; shift ;;
        --dry-run)  DRY_RUN=1; shift ;;
        -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "[refresh-mac3] unknown arg: $1" >&2; exit 2 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="$REPO_ROOT/.fleet/audits"
LOG="$LOG_DIR/refresh-mac3-$TS.log"
COUNTER="/tmp/mac3-refresh-runs.count"
MAC3_USER="${MAC3_SSH_USER:-aarontjomsland}"
MAC3_HOST="${MAC3_SSH_HOST:-192.168.1.191}"
mkdir -p "$LOG_DIR" 2>/dev/null || true

log() { local m="[$(date '+%H:%M:%S')] $*"; echo "$m" | tee -a "$LOG" 2>/dev/null; }
say() { log ">>> $*"; }

# Atomic counter bump — every code path lands here. ZSF.
bump_counter() {
    local outcome="$1"
    local cur=0
    [[ -f "$COUNTER" ]] && cur=$(cat "$COUNTER" 2>/dev/null || echo 0)
    [[ "$cur" =~ ^[0-9]+$ ]] || cur=0
    local next=$((cur + 1))
    local tmp="${COUNTER}.tmp.$$"
    echo "$next" > "$tmp" && mv -f "$tmp" "$COUNTER" 2>/dev/null || true
    # Outcome breakdown — separate counter file per reason.
    local rfile="${COUNTER}.${outcome}"
    local rcur=0
    [[ -f "$rfile" ]] && rcur=$(cat "$rfile" 2>/dev/null || echo 0)
    [[ "$rcur" =~ ^[0-9]+$ ]] || rcur=0
    echo $((rcur + 1)) > "${rfile}.tmp.$$" && mv -f "${rfile}.tmp.$$" "$rfile" 2>/dev/null || true
    log "counter: mac3_refresh_runs_total=$next outcome=$outcome"
}

# Best-effort NATS event publish — ZSF, fire-and-forget.
publish_event() {
    local outcome="$1"
    local extra="${2:-}"
    (
        cd "$REPO_ROOT" 2>/dev/null || return 0
        PYTHONPATH=. python3 - <<EOF >/dev/null 2>&1 &
import asyncio, json, time
try:
    from nats.aio.client import Client as NATS
    async def go():
        nc = NATS()
        await nc.connect("nats://127.0.0.1:4222", connect_timeout=2)
        subj = "fleet.event.mac3_refresh.$outcome"
        body = json.dumps({"ts": time.time(), "outcome": "$outcome", "from": "mac2", "extra": "$extra"})
        await nc.publish(subj, body.encode())
        await nc.flush(timeout=2)
        await nc.close()
    asyncio.run(go())
except Exception:
    pass
EOF
    )
    disown 2>/dev/null || true
}

# Compute mac3 last_seen divergence (seconds) from fleet-state.json.
compute_divergence() {
    python3 - "$REPO_ROOT/fleet-state.json" <<'EOF'
import json, sys, time
from datetime import datetime, timezone
try:
    d = json.load(open(sys.argv[1]))
    ls = d.get("nodes", {}).get("mac3", {}).get("health", {}).get("last_seen")
    if not ls:
        print("MISSING")
        sys.exit(0)
    # Normalize trailing Z + offsets.
    if ls.endswith("Z"):
        ls = ls[:-1] + "+00:00"
    dt = datetime.fromisoformat(ls)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    print(int(max(0, delta)))
except Exception as e:
    print(f"ERROR:{e}")
EOF
}

# Probe SSH reachability (P5).
ssh_reachable() {
    ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new \
        "$MAC3_USER@$MAC3_HOST" 'echo ok' 2>/dev/null | grep -q '^ok$'
}

# Run sprint-aaron-actions on mac3 via SSH (P5). Returns 0 on success.
run_sprint_via_ssh() {
    local remote_repo="${MAC3_REPO_PATH:-/Users/aarontjomsland/dev/er-simulator-superrepo}"
    say "P5 SSH: invoking sprint-aaron-actions on mac3 ($MAC3_HOST)"
    # Disable host pseudo-tty (-T); run with timeout so a wedged step can't hang us.
    if ssh -T -o BatchMode=yes -o ConnectTimeout=5 "$MAC3_USER@$MAC3_HOST" \
        "cd '$remote_repo' && bash scripts/sprint-aaron-actions.sh --apply --no-prompt" \
        2>&1 | tee -a "$LOG"; then
        return 0
    fi
    return 1
}

# Queue a P7 fleet-message asking mac3 to run sprint-aaron-actions locally.
send_via_p7() {
    say "P7 fleet-send: dispatching refresh request to mac3 via chief"
    local subject="refresh-mac3: run sprint-aaron-actions --apply --no-prompt"
    local body="Auto-heal M4 ($TS): mac3 last_seen diverged from mac2. Please run \`bash scripts/sprint-aaron-actions.sh --apply --no-prompt\` and reply with the log digest. Counter mac3_refresh_runs_total will increment via NATS event."
    if bash "$REPO_ROOT/scripts/fleet-send.sh" mac3 "$subject" "$body" 2>&1 | tee -a "$LOG"; then
        return 0
    fi
    return 1
}

# ---- main ---------------------------------------------------------------

say "refresh-mac3 start (dry-run=$DRY_RUN force=$FORCE threshold=${DIVERGENCE_THRESHOLD_S}s)"
say "node: $(hostname -s)  target: mac3 ($MAC3_HOST)  log: $LOG"

DIV=$(compute_divergence)
say "mac3 last_seen divergence: ${DIV}s"

case "$DIV" in
    MISSING)
        bump_counter "config_err"
        publish_event "config_err" "fleet-state missing mac3"
        log "FAIL: mac3 not present in fleet-state.json — cannot compute divergence"
        exit 1
        ;;
    ERROR:*)
        bump_counter "config_err"
        publish_event "config_err" "parse error"
        log "FAIL: fleet-state.json parse error — $DIV"
        exit 1
        ;;
esac

if [[ "$DIV" -lt "$DIVERGENCE_THRESHOLD_S" && $FORCE -eq 0 ]]; then
    bump_counter "divergence_clean"
    publish_event "divergence_clean" "${DIV}s"
    log "OK: mac3 within threshold (${DIV}s < ${DIVERGENCE_THRESHOLD_S}s) — no refresh needed (use --force to override)"
    exit 0
fi

if [[ $DRY_RUN -eq 1 ]]; then
    bump_counter "dry_run"
    publish_event "dry_run" "${DIV}s"
    log "DRY-RUN: would invoke sprint-aaron-actions on mac3 (divergence ${DIV}s) — pass --apply to execute"
    exit 0
fi

# --apply: try P5 SSH first, fall back to P7.
if ssh_reachable; then
    say "channel: P5 SSH ($MAC3_HOST reachable)"
    if run_sprint_via_ssh; then
        bump_counter "ok_p5"
        publish_event "ok_p5" "${DIV}s"
        log "OK: sprint-aaron-actions completed on mac3 via P5 SSH"
        exit 0
    fi
    bump_counter "sprint_fail"
    publish_event "sprint_fail" "p5_remote_nonzero"
    log "FAIL: P5 SSH reachable but sprint-aaron-actions exited non-zero — falling back to P7"
fi

# P7 fallback.
if send_via_p7; then
    bump_counter "ok_p7"
    publish_event "ok_p7" "${DIV}s"
    log "OK: P7 fleet-message queued for mac3 — mac3 will run sprint-aaron-actions on next inbox cycle"
    exit 0
fi

bump_counter "send_fail"
publish_event "send_fail" "p7_send_failed"
log "FAIL: both P5 SSH and P7 fleet-send failed — manual intervention needed"
exit 1
