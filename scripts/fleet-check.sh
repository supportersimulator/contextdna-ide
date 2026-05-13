#!/usr/bin/env bash
# fleet-check.sh — Fleet activity check (silent-when-idle, rate-limited)
#
# Event-driven stack (fleet-inbox-watcher, UserPromptSubmit hook, NATS pub/sub,
# menu-bar F:N plugin) already covers everything this script does. This exists
# only as a safety-net ping. Rate-limited to once per 5 min + logs caller.
#
# Usage: bash scripts/fleet-check.sh

set -uo pipefail

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
PORT="${FLEET_NERVE_PORT:-8855}"
ARCHIVE="/tmp/fleet-seed-archive"
CALLER_LOG="/tmp/fleet-check-callers.log"
RATELIMIT="/tmp/fleet-check-ratelimit"
RATELIMIT_SECS="${FLEET_CHECK_RATELIMIT_SECS:-300}"

# ── Tunnel hijack watchdog (runs every invocation, NOT rate-limited) ──
# Port 4222 must be our local NATS server. If autossh/ssh is holding it,
# the node is an "island" — kill the tunnel and log CRITICAL.
_TUNNEL_WATCHDOG="$(dirname "$0")/verify-no-tunnel.sh"
if [ -x "$_TUNNEL_WATCHDOG" ]; then
    if ! bash "$_TUNNEL_WATCHDOG" >/dev/null 2>&1; then
        echo "⚠️  CRITICAL: SSH tunnel hijack on :4222 detected — see /tmp/verify-no-tunnel.log"
    fi
fi

# ── Git state hygiene watchdog (runs every invocation, NOT rate-limited) ──
# Auto-aborts stale `.git/rebase-merge/` and re-attaches detached HEAD.
# Root cause of recurring "interactive rebase in progress" on session start.
_GIT_WATCHDOG="$(dirname "$0")/verify-git-clean.sh"
if [ -x "$_GIT_WATCHDOG" ]; then
    if ! bash "$_GIT_WATCHDOG" --quiet >/dev/null 2>&1; then
        echo "⚠️  git state corrupt — run scripts/verify-git-clean.sh for details"
    fi
fi

# ── JetStream replica drift auto-reconcile (JJ3 2026-05-08) ──
# Root cause: tools/fleet_nerve_nats.py:_maybe_rebalance_replicas() actively
# scales JS replicas to match live peer_count via compute_target_replicas().
# When a peer drops (Wi-Fi flap, route flap), peer_count → 2 → R=2.
# Recovery is silent unless another heartbeat crosses the change threshold,
# so streams can sit at R=2 for hours after the peer returns.
#
# Fix: idempotent provisioner runs on every fleet-check tick (rate-limited to
# 10 min). Adds counter `js_provision_runs_total` + `js_provision_repairs_total`
# to /tmp/jj3-jetstream-provision-counters. ZSF: errors append to .err log.
_JS_PROV_LIMIT="/tmp/jj3-jetstream-provision-ratelimit"
_JS_PROV_LIMIT_S="${FLEET_JS_PROV_RATELIMIT_S:-600}"
_JS_PROV_COUNTERS="/tmp/jj3-jetstream-provision-counters"
_JS_PROV_ERR="/tmp/jj3-jetstream-provision.err"
_JS_PROV_OUT="/tmp/jj3-jetstream-provision.json"
_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
_PY="${_REPO_ROOT}/.venv/bin/python3"
[ -x "$_PY" ] || _PY="$(command -v python3 || true)"
if [ -n "$_PY" ] && [ -f "${_REPO_ROOT}/tools/fleet_jetstream_provision.py" ]; then
    _now=$(date +%s)
    _last_js=$(cat "$_JS_PROV_LIMIT" 2>/dev/null || echo 0)
    if [ $((_now - _last_js)) -ge "$_JS_PROV_LIMIT_S" ]; then
        echo "$_now" > "$_JS_PROV_LIMIT"
        # Run in background; capture JSON report; bump counters from result.
        # ZSF: stderr → _JS_PROV_ERR, never silenced.
        (
            cd "$_REPO_ROOT" || exit 0
            if "$_PY" tools/fleet_jetstream_provision.py --json \
                >"$_JS_PROV_OUT" 2>>"$_JS_PROV_ERR"; then
                # Parse counters from JSON report (best-effort, no jq dep).
                # NB: pass JSON path via argv (sys.argv[1]) to avoid mixing
                # bash $-expansion with Python single-quoted string literals.
                _drift=$("$_PY" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    c = d.get('counters', {})
    print(c.get('js_streams_config_drift_total', 0))
except Exception:
    print(0)
" "$_JS_PROV_OUT" 2>/dev/null || echo 0)
                # Counter read-modify-write. macOS bash has no flock, but the
                # outer 10-min rate-limit (_JS_PROV_LIMIT) means concurrent
                # writers are practically impossible. Tmp-file + mv = atomic
                # rename so a partial write never leaves a half-file behind.
                runs=$(grep -E '^js_provision_runs_total=' "$_JS_PROV_COUNTERS" 2>/dev/null \
                    | tail -1 | cut -d= -f2)
                rep=$(grep -E '^js_provision_repairs_total=' "$_JS_PROV_COUNTERS" 2>/dev/null \
                    | tail -1 | cut -d= -f2)
                runs=$((${runs:-0} + 1))
                rep=$((${rep:-0} + ${_drift:-0}))
                _tmp="${_JS_PROV_COUNTERS}.tmp.$$"
                {
                    echo "js_provision_runs_total=$runs"
                    echo "js_provision_repairs_total=$rep"
                    echo "js_provision_last_run_ts=$_now"
                    echo "js_provision_last_drift=$_drift"
                } > "$_tmp" && mv -f "$_tmp" "$_JS_PROV_COUNTERS"
            else
                echo "[$(date '+%Y-%m-%dT%H:%M:%S')] provisioner exit non-zero (see $_JS_PROV_OUT)" \
                    >> "$_JS_PROV_ERR"
            fi
        ) &
        disown 2>/dev/null || true
    fi
fi

# ── Log every invocation with caller info ──
{
    echo "==== $(date '+%Y-%m-%d %H:%M:%S') — invoked ===="
    echo "PID=$$  PPID=$PPID"
    # Walk up parent chain to find who actually launched us
    p=$PPID
    for _ in 1 2 3 4 5; do
        [ -z "$p" ] || [ "$p" = "0" ] || [ "$p" = "1" ] && break
        ps -p "$p" -o pid=,ppid=,command= 2>/dev/null | head -1
        p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    done
    echo "args: $*"
    echo ""
} >> "$CALLER_LOG" 2>/dev/null

# ── Rate-limit: hard-silent unless RATELIMIT_SECS has elapsed since last run ──
now=$(date +%s)
last=$(cat "$RATELIMIT" 2>/dev/null || echo 0)
if [ $((now - last)) -lt "$RATELIMIT_SECS" ]; then
    exit 0
fi
echo "$now" > "$RATELIMIT"

# 1. Check seed files for messages
MSGS=0
for f in /tmp/fleet-seed-"${NODE_ID}".md /tmp/fleet-seed-"$(hostname -s | tr '[:upper:]' '[:lower:]')".md; do
    [ -f "$f" ] && [ -s "$f" ] || continue
    MSGS=1
    echo "📬 $(basename "$f"):"
    cat "$f"
    mkdir -p "$ARCHIVE"
    mv "$f" "${ARCHIVE}/$(date +%s)-$(basename "$f")"
done

# 2. Check dashboard (legacy text endpoint — /dashboard now returns 21KB HTML)
DASH=$(curl -s --max-time 5 "http://127.0.0.1:${PORT}/dashboard/legacy" 2>/dev/null || true)

# 2b. Suppress repeated identical dashboards (silent when idle = unchanged state)
# Strip timestamps + uptime counters before hashing so only real state changes trigger output
STATE="/tmp/fleet-check-last-hash"
if [ -n "$DASH" ]; then
    CURR_HASH=$(echo "$DASH" \
        | sed -E 's/`[0-9]{2}:[0-9]{2}:[0-9]{2}`//g; s/[0-9]+[smhd] ago//g; s/uptime [0-9]+//gi' \
        | shasum | cut -c1-16)
    LAST_HASH=$(cat "$STATE" 2>/dev/null || echo "")
    if [ "$CURR_HASH" = "$LAST_HASH" ] && [ "$MSGS" = "0" ]; then
        exit 0
    fi
    echo "$CURR_HASH" > "$STATE"
fi

# 3. Output only if there's something
if [ "$MSGS" = "0" ] && [ -z "$DASH" ]; then
    exit 0
fi

[ -n "$DASH" ] && echo "$DASH"

# ── Green-light pool status ──
_GL_FILE="${_REPO_ROOT}/.fleet/priorities/green-light.md"
if [ -f "$_GL_FILE" ]; then
    _gl_unclaimed=$(grep '\- \[ \]' "$_GL_FILE" 2>/dev/null | awk 'END{print NR}')
    _gl_claimed=$(grep '\- \[⏳' "$_GL_FILE" 2>/dev/null | awk 'END{print NR}')
    _gl_done=$(grep '\- \[x\]' "$_GL_FILE" 2>/dev/null | awk 'END{print NR}')
    echo "🟢 green-light pool: ${_gl_unclaimed} unclaimed | ${_gl_claimed} claimed | ${_gl_done} done"
else
    echo "🟢 green-light pool: file not found (${_GL_FILE})"
fi

# ── Watchdog launchd status ──
_wd_info=$(launchctl list 2>/dev/null | grep fleet-nerve-watchdog || true)
if [ -n "$_wd_info" ]; then
    _wd_pid=$(echo "$_wd_info" | awk '{print $1}')
    _wd_exit=$(echo "$_wd_info" | awk '{print $2}')
    echo "🐕 watchdog: pid=${_wd_pid} last_exit=${_wd_exit}"
else
    echo "🐕 watchdog: not registered in launchctl"
fi

# ── Race Theater server (:8877) ──
if curl -sf --max-time 3 http://127.0.0.1:8877/health >/dev/null 2>&1; then
    echo "🎭 race-theater (:8877): ok"
else
    echo "🎭 race-theater (:8877): down"
fi

# ── Evidence Stream server (:8878) ──
if curl -sf --max-time 3 http://127.0.0.1:8878/health >/dev/null 2>&1; then
    echo "📊 evidence-stream (:8878): ok"
else
    echo "📊 evidence-stream (:8878): down"
fi

# ── NATS JetStream streams ──
if command -v nats >/dev/null 2>&1; then
    _js_streams=$(nats stream ls 2>/dev/null | grep -c '^\s' 2>/dev/null || echo 0)
    echo "🌊 NATS JetStream: ${_js_streams} stream(s)"
else
    echo "🌊 NATS JetStream: nats CLI not found"
fi

# ── NATS channel smoke test ──
_SMOKE_SCRIPT="$(dirname "$0")/nats-channel-smoke.sh"
if [ -x "$_SMOKE_SCRIPT" ]; then
    _smoke_out=$(bash "$_SMOKE_SCRIPT" --quiet 2>/dev/null || true)
    _smoke_line=$(echo "$_smoke_out" | grep '^NATS_SMOKE:' || true)
    if [ -n "$_smoke_line" ]; then
        _sm_pass=$(echo "$_smoke_line" | grep -oE 'PASS=[0-9]+' | cut -d= -f2)
        _sm_warn=$(echo "$_smoke_line" | grep -oE 'WARN=[0-9]+' | cut -d= -f2)
        _sm_fail=$(echo "$_smoke_line" | grep -oE 'FAIL=[0-9]+' | cut -d= -f2)
        _sm_pass="${_sm_pass:-0}"; _sm_warn="${_sm_warn:-0}"; _sm_fail="${_sm_fail:-0}"
        if [ "$_sm_fail" -gt 0 ]; then
            printf "\033[0;31m🔥 nats-smoke: PASS=%s WARN=%s FAIL=%s\033[0m\n" "$_sm_pass" "$_sm_warn" "$_sm_fail"
        elif [ "$_sm_warn" -gt 0 ]; then
            printf "\033[0;33m⚡ nats-smoke: PASS=%s WARN=%s FAIL=%s\033[0m\n" "$_sm_pass" "$_sm_warn" "$_sm_fail"
        else
            printf "\033[0;32m✅ nats-smoke: PASS=%s WARN=%s FAIL=%s\033[0m\n" "$_sm_pass" "$_sm_warn" "$_sm_fail"
        fi
    else
        echo "🌐 nats-smoke: unavailable (no NATS_SMOKE line)"
    fi
else
    echo "🌐 nats-smoke: unavailable (script not found)"
fi

# ── mac1 auto-wire (self-healing: fires when mac1 becomes reachable) ──
# Idempotent, rate-limited internally. Non-blocking; errors swallowed.
_AUTO_WIRE="$(dirname "$0")/mac1-auto-wire.sh"
[ -x "$_AUTO_WIRE" ] && bash "$_AUTO_WIRE" >/dev/null 2>&1 || true
