#!/usr/bin/env bash
# scripts/demo-surgeon-theater.sh — Corrigibility-moat IDE demo.
#
# Drives admin.contextdna.io's SurgeonTheater (Tier-1/2/3) through a synthetic
# surgeon-disagreement scenario end-to-end:
#
#   Tier-1   phase strip: probe → cardio → neuro → consensus → verdict
#   Tier-2   per-phase detail panel (latency, model, preview text)
#   Tier-3   metrics ribbon (cross-exam count, agreement %, disagreement badge)
#
# Pipeline:
#   1. Fleet daemon @ :8855 emits SSE events on /events/stream (sse_multiplex).
#   2. Next.js admin.contextdna.io @ :3000 proxies SSE via /api/fleet/events.
#   3. EventBridge (browser) ingests, fans out to TypedEventBus.
#   4. SurgeonTheater renders.
#
# This script POSTs synthetic surgeon:* events to /events/publish (dev-mode,
# loopback-only, gated by FLEET_DEV_EVENTS=1) AND writes a real ESCALATE_TO_RED
# decision to .fleet/audits/<today>-decisions.md so the audit pipeline shows a
# real artifact.
#
# Aaron records video alongside this. We DO NOT call screencapture/ffmpeg —
# see scripts/demo-companion-record.sh for the recording wrapper Aaron runs.
#
# Constraints (B5 round-9):
#   - No git push.
#   - No real LLM consults — synthetic events only.
#   - Idempotent + self-cleaning (trap EXIT).
#   - Cost cap $0.50 (we never invoke a paid API; cap = belt-and-braces).
#
# Flags:
#   --interactive=0 / --auto   non-interactive (sleep 5s between scenes)
#   --interactive=1 / -i       interactive (read prompt before each scene)
#   --no-cleanup               leave daemon running on exit (debugging)
#   --skip-decision            don't write to decisions.md
#   --port-daemon N            override daemon port (default 8855)
#   --port-admin N             override Next.js port (default 3000)
#   --duration N               total wall-clock budget (default 240s)
#   --verify                   run all scenes, print PASS/FAIL, exit by status
#   --dry-run                  print scene list, exit 0
#   --rehearse-halt            delegate to scripts/demo-halt-rehearse.sh
#                              (Scene-6 HALT recovery cutaway). All other flags
#                              are ignored when this flag is set.
#
# Env defaults:
#   DEMO_INTERACTIVE      0 (auto) | 1 (read prompt) — flag wins over env
#   FLEET_DAEMON_PORT     8855
#   ADMIN_PORT            3000
#   FLEET_DEV_EVENTS      1 (forced — required for /events/publish)
#
# Exit codes:
#   0  success (or success enough — soft scenes can fail)
#   1  prerequisite failure (daemon won't start, etc.)
#   2  bad CLI args
#
# ---------------------------------------------------------------------------
set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DAEMON_PORT="${FLEET_DAEMON_PORT:-8855}"
ADMIN_PORT="${ADMIN_PORT:-3000}"
DEMO_INTERACTIVE="${DEMO_INTERACTIVE:-0}"
DURATION=240
NO_CLEANUP=0
SKIP_DECISION=0
VERIFY=0
DRY_RUN=0
REHEARSE_HALT=0

# Pidfiles for our own children (we never kill processes we didn't start).
NATS_PIDFILE="/tmp/demo-st-nats.pid"
DAEMON_PIDFILE="/tmp/demo-st-daemon.pid"
ADMIN_PIDFILE="/tmp/demo-st-admin.pid"
DAEMON_LOG="/tmp/demo-st-daemon.log"
ADMIN_LOG="/tmp/demo-st-admin.log"
NATS_LOG="/tmp/demo-st-nats.log"

# Per-scene logs the verifier can grep.
SCENE_LOG_DIR="/tmp"

# Cost tracker — we never invoke real LLMs but we keep the field honest.
COST_USD="0.0000"

# ── Args ────────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --interactive=0|--auto) DEMO_INTERACTIVE=0 ;;
    --interactive=1|-i)     DEMO_INTERACTIVE=1 ;;
    --no-cleanup)           NO_CLEANUP=1 ;;
    --skip-decision)        SKIP_DECISION=1 ;;
    --verify)               VERIFY=1; DEMO_INTERACTIVE=0 ;;
    --dry-run)              DRY_RUN=1 ;;
    --rehearse-halt)        REHEARSE_HALT=1 ;;
    --port-daemon)          DAEMON_PORT="${2:?}"; shift ;;
    --port-admin)           ADMIN_PORT="${2:?}"; shift ;;
    --duration)             DURATION="${2:?}"; shift ;;
    -h|--help)              grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)  echo "[demo-st] unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── Pretty printing ────────────────────────────────────────────────────────
if [ -t 1 ]; then
  C_R=$'\033[0;31m'; C_G=$'\033[0;32m'; C_Y=$'\033[0;33m'
  C_C=$'\033[0;36m'; C_M=$'\033[0;35m'; C_D=$'\033[0;90m'
  C_B=$'\033[1m'; C_X=$'\033[0m'
else
  C_R=""; C_G=""; C_Y=""; C_C=""; C_M=""; C_D=""; C_B=""; C_X=""
fi
say()    { printf "%b\n" "$*"; }
banner() { printf "${C_M}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_X}\n"; }
scene()  { printf "\n${C_B}${C_C}🎥 SCENE %s — %s${C_X}\n" "$1" "$2"; }
ok()     { printf "  ${C_G}✓${C_X} %s\n" "$*"; }
warn()   { printf "  ${C_Y}⚠${C_X} %s\n" "$*"; }
fail()   { printf "  ${C_R}✗${C_X} %s\n" "$*"; }
info()   { printf "  ${C_D}│${C_X} %s\n" "$*"; }

# Per-scene log helpers.
SCENE_RESULTS=()
log_scene() {
  local n="$1" status="$2" msg="$3"
  printf "[%s] [scene-%s] %s\n" "$(date -u +%FT%TZ)" "$n" "$msg" \
    >> "${SCENE_LOG_DIR}/demo-${n}.log"
  SCENE_RESULTS+=("scene-${n}=${status}")
}

# ── Pause helper ────────────────────────────────────────────────────────────
pause_scene() {
  local label="$1"
  if [ "$DEMO_INTERACTIVE" -eq 1 ]; then
    read -r -p "  press Enter to advance past ${label}… " _ || true
  else
    sleep 5
  fi
}

# ── Cleanup ─────────────────────────────────────────────────────────────────
cleanup() {
  local rc=$?
  if [ "$NO_CLEANUP" -eq 1 ]; then
    say ""
    info "[--no-cleanup] leaving daemon=$([ -f "$DAEMON_PIDFILE" ] && cat "$DAEMON_PIDFILE") admin=$([ -f "$ADMIN_PIDFILE" ] && cat "$ADMIN_PIDFILE") running."
    return 0
  fi
  say ""
  info "cleaning up demo-only processes…"
  for pf in "$DAEMON_PIDFILE" "$NATS_PIDFILE" "$ADMIN_PIDFILE"; do
    [ -f "$pf" ] || continue
    local pid; pid="$(cat "$pf" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pf"
  done
  ok "cleanup complete (rc=$rc)"
}
trap cleanup EXIT INT TERM

# ── Dry-run: just print the scene plan ─────────────────────────────────────
print_plan() {
  banner
  printf "${C_B}  Surgeon Theater — Corrigibility Moat Demo${C_X}\n"
  printf "${C_D}  admin.contextdna.io UI driven via fleet daemon SSE${C_X}\n"
  banner
  echo
  echo "  Scene 1  Setup + idle theater (Tier-1 quiet)         ~10s"
  echo "  Scene 2  Synthetic D-04 finding emerges (Tier-2)      ~15s"
  echo "  Scene 3  Cardio elevates / Neuro dismisses            ~20s"
  echo "             → DISAGREEMENT badge, red glow"
  echo "  Scene 4  Chief decides ESCALATE_TO_RED (Tier-3)       ~15s"
  echo "             → audit decisions.md gets a fresh entry"
  echo "  Scene 5  Operator ack + reset (theater returns idle)  ~10s"
  echo
  echo "  Total wall-clock: ~70s + per-scene pauses (interactive=$DEMO_INTERACTIVE)"
  echo
  echo "  Daemon:  http://127.0.0.1:${DAEMON_PORT}/health"
  echo "  Admin:   http://127.0.0.1:${ADMIN_PORT}/dashboard"
  echo "  Decision log: ${REPO_ROOT}/.fleet/audits/$(date +%F)-decisions.md"
}

if [ "$DRY_RUN" -eq 1 ]; then
  print_plan
  exit 0
fi

# ── Scene 6: HALT recovery rehearsal ───────────────────────────────────────
# Delegate to scripts/demo-halt-rehearse.sh. This bypasses scenes 1-5 entirely
# and runs only the HALT-recovery cutaway (Step 1..8 in the rehearsal script).
# Useful for: re-running just the recovery loop on camera, verifying the
# HALT contract before a recording session, or smoke-testing the audit
# pipeline without touching the SSE theater.
if [ "$REHEARSE_HALT" -eq 1 ]; then
  rehearse="$REPO_ROOT/scripts/demo-halt-rehearse.sh"
  if [ ! -x "$rehearse" ]; then
    echo "[demo-st] $rehearse not executable" >&2
    exit 1
  fi
  exec bash "$rehearse"
fi

# ── HTTP helpers ───────────────────────────────────────────────────────────
publish_event() {
  # POST a synthetic event into the daemon's IDEEventBus → SSE multiplex.
  local kind="$1" payload="$2"
  local resp http_code
  resp="$(curl -sS --max-time 3 -o /tmp/demo-st-publish-resp \
    -w '%{http_code}' \
    -X POST "http://127.0.0.1:${DAEMON_PORT}/events/publish" \
    -H 'Content-Type: application/json' \
    -d "{\"kind\":\"${kind}\",\"payload\":${payload}}" 2>/dev/null || echo "000")"
  http_code="${resp:-000}"
  if [ "$http_code" = "200" ]; then
    return 0
  fi
  warn "publish ${kind} returned http=${http_code} body=$(head -c 160 /tmp/demo-st-publish-resp 2>/dev/null)"
  return 1
}

http_health() {
  curl -sS --max-time 2 "http://127.0.0.1:${DAEMON_PORT}/health" 2>/dev/null
}

admin_reachable() {
  curl -sS --max-time 2 -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:${ADMIN_PORT}/" 2>/dev/null
}

# ── Setup phase ────────────────────────────────────────────────────────────
do_setup() {
  scene 1 "fleet idle — verify daemon, admin, NATS, theater quiet"
  log_scene 1 START "setup begin"

  # Verify python3
  local py
  py="$(command -v python3 || true)"
  [ -n "$py" ] || { fail "python3 not found"; log_scene 1 FAIL "no python3"; return 1; }
  ok "python3: $py"

  # Daemon — start with FLEET_DEV_EVENTS=1 if not already up.
  # /health can be slow (~2s) on a busy daemon under load, so we use a
  # generous per-request timeout + cheaper /events/status as the readiness
  # probe (it's <50ms because it doesn't enumerate sessions).
  if curl -sf --max-time 5 "http://127.0.0.1:${DAEMON_PORT}/health" >/dev/null 2>&1; then
    ok "fleet daemon already running on :${DAEMON_PORT}"
    # Probe whether dev publish is enabled on the existing daemon.
    local pub_status
    pub_status="$(curl -sS --max-time 3 -o /dev/null -w '%{http_code}' \
      -X POST "http://127.0.0.1:${DAEMON_PORT}/events/publish" \
      -H 'Content-Type: application/json' \
      -d '{"kind":"sse.demo-probe","payload":{}}' 2>/dev/null || echo 000)"
    if [ "$pub_status" = "403" ]; then
      warn "existing daemon has FLEET_DEV_EVENTS=0; /events/publish will 403."
      warn "restart it with FLEET_DEV_EVENTS=1, or run this script with --port-daemon ${DAEMON_PORT}+1"
    fi
  else
    info "starting fleet daemon (FLEET_DEV_EVENTS=1) on :${DAEMON_PORT}…"
    (
      cd "$REPO_ROOT" || exit 1
      MULTIFLEET_NODE_ID="${MULTIFLEET_NODE_ID:-demo-st}" \
      FLEET_DAEMON_PORT="$DAEMON_PORT" \
      FLEET_DEV_EVENTS=1 \
      NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}" \
        "$py" "$REPO_ROOT/tools/fleet_nerve_nats.py" serve \
        >"$DAEMON_LOG" 2>&1 &
      echo $! > "$DAEMON_PIDFILE"
    )
    # Wait up to 60s for health. /health is ~2s under load; use 8s per-call.
    local up=0
    for _ in $(seq 1 30); do
      if curl -sf --max-time 8 "http://127.0.0.1:${DAEMON_PORT}/health" >/dev/null 2>&1; then
        up=1; break
      fi
      sleep 1
    done
    if [ "$up" -ne 1 ]; then
      fail "daemon did not become healthy in 60s; tail $DAEMON_LOG:"
      tail -20 "$DAEMON_LOG" 2>/dev/null | sed 's/^/    /' || true
      log_scene 1 FAIL "daemon timeout"
      return 1
    fi
    ok "fleet daemon up on :${DAEMON_PORT}"
  fi

  # Admin reachability — NOT auto-started. Aaron starts it; we just check.
  local admin_status; admin_status="$(admin_reachable)"
  if [ "$admin_status" = "200" ] || [ "$admin_status" = "307" ] || [ "$admin_status" = "308" ]; then
    ok "admin.contextdna.io reachable on :${ADMIN_PORT}"
  else
    warn "admin.contextdna.io NOT reachable on :${ADMIN_PORT} (http=$admin_status)"
    info "  start it manually: cd admin.contextdna.io && pnpm dev   (or npm run dev)"
    info "  demo will continue — events still publish, just no UI to watch."
  fi

  # 3-surgeons keychain — never leak; just probe presence.
  if command -v 3s >/dev/null 2>&1; then
    if 3s probe 2>/dev/null | grep -qiE "ok|reachable|available"; then
      ok "3s CLI present (model presence not used in demo — synthetic only)"
    else
      info "3s CLI present, surgeons not all reachable (fine — synthetic demo)"
    fi
  else
    info "3s CLI not on PATH (fine — demo uses synthetic events only)"
  fi

  log_scene 1 PASS "setup complete"
  banner
  printf "  ${C_B}Open the dashboard now:${C_X} http://127.0.0.1:${ADMIN_PORT}/dashboard\n"
  printf "  ${C_D}You should see SurgeonTheater with all 5 phases idle.${C_X}\n"
  banner
}

# ── Scene 2: finding emerges ───────────────────────────────────────────────
do_scene_finding() {
  scene 2 "synthetic D-04 finding emerges → Tier-2 lights up"
  log_scene 2 START "publish probe phase active"

  # Phase 1: probe goes active (a finding shows up).
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"probe","status":"active","preview":"D-04 webhook-dead-air; 3 nodes affected"}' \
    && ok "surgeon.phase probe=active emitted" \
    || warn "phase publish failed (daemon may not have FLEET_DEV_EVENTS=1)"

  sleep 1

  publish_event "fleet.peer.online" \
    '{"node_id":"mac1-demo","ip":"127.0.0.1","caused_by":"DEMO-001 finding"}' \
    && ok "fleet.peer.online emitted (smoke-test bridge)" \
    || warn "fleet.peer.online publish failed"

  sleep 2

  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"probe","status":"done","model":"local","latency_ms":120,"preview":"finding F-DEMO04 classified D-04/loss"}' \
    && ok "surgeon.phase probe=done emitted" \
    || warn "phase done publish failed"

  log_scene 2 PASS "finding broadcast"
  pause_scene "Scene 2"
}

# ── Scene 3: surgeons disagree ─────────────────────────────────────────────
do_scene_disagreement() {
  scene 3 "Cardio elevates, Neuro dismisses → DISAGREEMENT badge"
  log_scene 3 START "publish disagreement"

  # Cardio thinks ELEVATE_TO_CRITICAL.
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"cardio","status":"active","model":"deepseek-chat","preview":"webhook-dead-air across 3 nodes is data suppression"}'
  sleep 1
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"cardio","status":"done","model":"deepseek-chat","latency_ms":1840,"preview":"verdict: ELEVATE_TO_CRITICAL — counter likely reset, halt green-light pool","cost_usd":0.0021,"tokens_in":850,"tokens_out":160}'
  publish_event "surgeon.verdict" \
    '{"case_id":"DEMO-001","surgeon":"cardiologist","verdict":"ELEVATE_TO_CRITICAL","confidence":0.82,"latency_ms":1840,"cost_usd":0.0021}' \
    && ok "cardio verdict ELEVATE_TO_CRITICAL emitted"

  sleep 2

  # Neuro thinks DISMISS_AS_NOISE.
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"neuro","status":"active","model":"qwen3-4b","preview":"local probe shows webhook backed off cleanly"}'
  sleep 1
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"neuro","status":"done","model":"qwen3-4b","latency_ms":640,"preview":"verdict: DISMISS_AS_NOISE — no signal in node logs","cost_usd":0.0,"tokens_in":820,"tokens_out":140}'
  publish_event "surgeon.verdict" \
    '{"case_id":"DEMO-001","surgeon":"neurologist","verdict":"DISMISS_AS_NOISE","confidence":0.71,"latency_ms":640,"cost_usd":0.0}' \
    && ok "neuro verdict DISMISS_AS_NOISE emitted"

  sleep 1

  # Explicit disagreement event → Tier-1 red glow + badge.
  publish_event "surgeon.disagreement" \
    '{"case_id":"DEMO-001","topic":"D-04 webhook-dead-air classification","cardio":"ELEVATE_TO_CRITICAL","neuro":"DISMISS_AS_NOISE","severity":"high"}' \
    && ok "surgeon.disagreement (severity=high) emitted"

  log_scene 3 PASS "disagreement live"
  pause_scene "Scene 3"
}

# ── Scene 4: chief decides ESCALATE_TO_RED ─────────────────────────────────
do_scene_escalate() {
  scene 4 "consensus iter#2 → chief decides ESCALATE_TO_RED"
  log_scene 4 START "consensus + escalate"

  # Iteration 1 hint (low consensus).
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"consensus","status":"active","preview":"iter 1: consensus=0.20 — re-running with elevated context"}'
  sleep 2
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"consensus","status":"done","preview":"iter 2: consensus=0.20 (stuck) — escalating"}'

  sleep 1

  # Verdict phase.
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"verdict","status":"active","preview":"chief decision pending…"}'
  sleep 1
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"verdict","status":"done","preview":"ESCALATE_TO_RED — surgeons split, needs Aaron"}'

  ok "Tier-1 phase strip is now: ✓ ✓ ✓ ✓ ✓ (all 5 done)"
  ok "Tier-3 ribbon: 1 disagreement should be visible"

  # Write the actual chief decision so .fleet/audits/<today>-decisions.md grows.
  if [ "$SKIP_DECISION" -eq 0 ]; then
    info "writing real ESCALATE_TO_RED to .fleet/audits/$(date +%F)-decisions.md…"
    PYTHONPATH="${REPO_ROOT}/multi-fleet:${REPO_ROOT}" "$REPO_ROOT/.venv/bin/python3" - <<'PYEOF' || warn "decision write failed (non-fatal)"
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "multi-fleet"))
from multifleet.audit_log import append_decision
repo = Path.cwd()
append_decision(
    repo,
    cluster_id="C-D-04-loss-DEMO",
    finding_ids=["F-DEMO04"],
    decision="ESCALATE_TO_RED",
    consensus=0.20,
    iterations=2,
    rationale=(
        "demo: cardio=ELEVATE_TO_CRITICAL vs neuro=DISMISS_AS_NOISE; "
        "consensus stuck at 0.20 across 2 iters. Surgeons split — "
        "operator review required. (synthetic demo entry)"
    ),
    transcript_ref="demo-surgeon-theater.sh",
)
print("[demo-st] decision appended.")
PYEOF
    if [ -f "$REPO_ROOT/.fleet/audits/$(date +%F)-decisions.md" ]; then
      ok "decision written: .fleet/audits/$(date +%F)-decisions.md"
    fi
  else
    info "(--skip-decision) decisions.md untouched"
  fi

  log_scene 4 PASS "escalate-to-red"
  pause_scene "Scene 4"
}

# ── Scene 5: operator acks + reset ─────────────────────────────────────────
do_scene_ack_reset() {
  scene 5 "operator acks; theater returns to idle"
  log_scene 5 START "ack + reset"

  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"probe","status":"idle"}'
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"cardio","status":"idle"}'
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"neuro","status":"idle"}'
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"consensus","status":"idle"}'
  publish_event "surgeon.phase" \
    '{"case_id":"DEMO-001","phase":"verdict","status":"idle"}'

  ok "all 5 phases reset to idle (Tier-1 quiet again)"
  info "Tier-3 metrics ribbon retains 24h sliding window — disagreement count persists"

  log_scene 5 PASS "reset"
  banner
  printf "  ${C_B}${C_G}Demo complete.${C_X}  Cost: \$${COST_USD}\n"
  printf "  Decision log row: ${C_C}.fleet/audits/$(date +%F)-decisions.md${C_X}\n"
  banner
}

# ── Verify mode: assert artifacts ──────────────────────────────────────────
verify_artifacts() {
  local rc=0
  printf "\n${C_B}--- Verification ---${C_X}\n"
  for r in "${SCENE_RESULTS[@]}"; do
    case "$r" in
      *=PASS|*=START) ok "$r" ;;
      *=FAIL)         fail "$r"; rc=1 ;;
    esac
  done
  if [ "$SKIP_DECISION" -eq 0 ]; then
    local d="$REPO_ROOT/.fleet/audits/$(date +%F)-decisions.md"
    if [ -f "$d" ] && grep -q "ESCALATE_TO_RED" "$d" 2>/dev/null; then
      ok "decisions.md contains ESCALATE_TO_RED"
    else
      fail "decisions.md missing ESCALATE_TO_RED row at $d"
      rc=1
    fi
  fi
  return $rc
}

# ── Main ───────────────────────────────────────────────────────────────────
banner
printf "${C_B}  Surgeon Theater — Corrigibility Moat Demo${C_X}\n"
printf "${C_D}  interactive=${DEMO_INTERACTIVE}  daemon=:${DAEMON_PORT}  admin=:${ADMIN_PORT}${C_X}\n"
banner

START_TS="$(date +%s)"

do_setup            || exit 1
pause_scene "Scene 1 (setup → idle)"
do_scene_finding
do_scene_disagreement
do_scene_escalate
do_scene_ack_reset

ELAPSED=$(( $(date +%s) - START_TS ))
info "wall-clock: ${ELAPSED}s (budget ${DURATION}s)"

if [ "$VERIFY" -eq 1 ]; then
  verify_artifacts
  exit $?
fi
exit 0
