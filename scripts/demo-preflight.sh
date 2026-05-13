#!/usr/bin/env bash
# scripts/demo-preflight.sh — Aaron's recording-day readiness check.
#
# Runs BEFORE any take. Verifies every external dependency the demo touches
# is healthy enough to produce a clean recording. Each check returns a clear
# OK / WARN / FAIL line; the script exits 0 only if zero FAILs.
#
# Checks (Q5 round-9, follow-up to N5):
#   1. python3 + repo .venv (writes the audit decision)
#   2. fleet daemon up on :8855 (or alt) + FLEET_DEV_EVENTS=1
#   3. admin.contextdna.io reachable on :3000 (or alt) — /dashboard responds
#   4. MLX server warm on :5044 (Apple Silicon only — SKIP cleanly on Intel)
#   5. /tmp clean of stale demo pidfiles
#   6. ~/recordings exists and has > 5 GB free
#   7. Do Not Disturb on (best-effort macOS check)
#
# Exit codes:
#   0  every required check passes (warnings allowed)
#   1  at least one FAIL — do not start recording
#   2  bad CLI args
#
# Flags:
#   --port-daemon N   override daemon port (default 8855)
#   --port-admin N    override admin port  (default 3000)
#   --no-mlx          skip MLX warm-up check
#   --no-disk         skip disk-space check
#   --no-dnd          skip Do-Not-Disturb check
#   --quiet           only print summary line + nonzero exit on fail
#   -h | --help       show this header
#
# This script NEVER starts services. It only observes. If something is
# unhealthy, the operator (Aaron) is the one who fixes it.
# ---------------------------------------------------------------------------
set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DAEMON_PORT="${FLEET_DAEMON_PORT:-8855}"
ADMIN_PORT="${ADMIN_PORT:-3000}"
NO_MLX=0
NO_DISK=0
NO_DND=0
QUIET=0

while [ $# -gt 0 ]; do
  case "$1" in
    --port-daemon) DAEMON_PORT="${2:?}"; shift ;;
    --port-admin)  ADMIN_PORT="${2:?}";  shift ;;
    --no-mlx)      NO_MLX=1 ;;
    --no-disk)     NO_DISK=1 ;;
    --no-dnd)      NO_DND=1 ;;
    --quiet)       QUIET=1 ;;
    -h|--help)     grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[preflight] unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── Colour helpers ─────────────────────────────────────────────────────────
if [ -t 1 ] && [ "$QUIET" -eq 0 ]; then
  C_R=$'\033[0;31m'; C_G=$'\033[0;32m'; C_Y=$'\033[0;33m'; C_X=$'\033[0m'
  C_B=$'\033[1m'; C_D=$'\033[0;90m'
else
  C_R=""; C_G=""; C_Y=""; C_X=""; C_B=""; C_D=""
fi

OK_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
TOTAL=0

ok()   { TOTAL=$((TOTAL+1)); OK_COUNT=$((OK_COUNT+1));   [ "$QUIET" -eq 0 ] && printf "  ${C_G}OK${C_X}    %s\n" "$*"; }
warn() { TOTAL=$((TOTAL+1)); WARN_COUNT=$((WARN_COUNT+1));[ "$QUIET" -eq 0 ] && printf "  ${C_Y}WARN${C_X}  %s\n" "$*"; }
fail() { TOTAL=$((TOTAL+1)); FAIL_COUNT=$((FAIL_COUNT+1));[ "$QUIET" -eq 0 ] && printf "  ${C_R}FAIL${C_X}  %s\n" "$*" >&2; }
banner() { [ "$QUIET" -eq 0 ] && printf "${C_B}%s${C_X}\n" "$*"; }
info()   { [ "$QUIET" -eq 0 ] && printf "        ${C_D}%s${C_X}\n" "$*"; }

banner "═══ demo-preflight (recording-day readiness) ═══"

# ── 1. Python + venv ───────────────────────────────────────────────────────
banner "[1] python3 + repo .venv"
if command -v python3 >/dev/null 2>&1; then
  ok "python3 on PATH ($(command -v python3))"
else
  fail "python3 not on PATH"
fi

if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
  ok ".venv/bin/python3 present (audit-log writer will work)"
else
  warn ".venv/bin/python3 missing — decision write may fall back to system python"
  info "rebuild: cd $REPO_ROOT && python3 -m venv .venv && .venv/bin/pip install -e multi-fleet"
fi

# ── 2. Fleet daemon + FLEET_DEV_EVENTS ─────────────────────────────────────
banner "[2] fleet daemon on :${DAEMON_PORT} (FLEET_DEV_EVENTS=1)"
if curl -sf --max-time 5 "http://127.0.0.1:${DAEMON_PORT}/health" >/dev/null 2>&1; then
  ok "/health on :${DAEMON_PORT} responds"
  pub_status="$(curl -sS --max-time 3 -o /dev/null -w '%{http_code}' \
    -X POST "http://127.0.0.1:${DAEMON_PORT}/events/publish" \
    -H 'Content-Type: application/json' \
    -d '{"kind":"sse.demo-preflight-probe","payload":{}}' 2>/dev/null || echo 000)"
  case "$pub_status" in
    200) ok "/events/publish accepts (FLEET_DEV_EVENTS=1)" ;;
    403) fail "/events/publish returns 403 — daemon was started without FLEET_DEV_EVENTS=1"
         info "fix: kill the daemon and restart with FLEET_DEV_EVENTS=1, or run the demo with --port-daemon $((DAEMON_PORT+2))" ;;
    *)   warn "/events/publish returned http=${pub_status} — unexpected" ;;
  esac
else
  warn "fleet daemon not running on :${DAEMON_PORT} — demo will spawn its own"
  info "the demo script handles this; no action required, but the take will start ~10s slower"
fi

# ── 3. admin.contextdna.io ─────────────────────────────────────────────────
banner "[3] admin.contextdna.io on :${ADMIN_PORT}"
admin_status="$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${ADMIN_PORT}/" 2>/dev/null || echo 000)"
case "$admin_status" in
  200|307|308) ok "admin reachable on :${ADMIN_PORT} (http=${admin_status})" ;;
  000)         fail "admin NOT running on :${ADMIN_PORT}"
               info "fix: cd ${REPO_ROOT}/admin.contextdna.io && pnpm dev   (or npm run dev)" ;;
  *)           warn "admin on :${ADMIN_PORT} returned http=${admin_status} — verify manually" ;;
esac

# Verify the dashboard route specifically — that's where SurgeonTheater renders.
dash_status="$(curl -sS --max-time 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${ADMIN_PORT}/dashboard" 2>/dev/null || echo 000)"
case "$dash_status" in
  200|307|308) ok "/dashboard responds (http=${dash_status})" ;;
  000)         fail "/dashboard unreachable" ;;
  *)           warn "/dashboard returned http=${dash_status}" ;;
esac

# ── 4. MLX warm-up (Apple Silicon only) ────────────────────────────────────
banner "[4] MLX server warm-up (:5044, ARM only)"
if [ "$NO_MLX" -eq 1 ]; then
  ok "MLX check skipped (--no-mlx)"
else
  ARCH="$(uname -m 2>/dev/null || echo unknown)"
  if [ "$ARCH" = "arm64" ]; then
    if curl -sf --max-time 3 "http://127.0.0.1:5044/v1/models" >/dev/null 2>&1; then
      # Warm up the model with a tiny query so first-token latency is low.
      warm_resp="$(curl -sS --max-time 8 \
        -H 'Content-Type: application/json' \
        -d '{"model":"local","messages":[{"role":"user","content":"hi"}],"max_tokens":4}' \
        "http://127.0.0.1:5044/v1/chat/completions" 2>/dev/null | head -c 200 || true)"
      if [ -n "$warm_resp" ]; then
        ok "MLX :5044 responding + warmed up"
      else
        warn "MLX :5044 /v1/models OK but warm-up call did not return content (may still be loading)"
      fi
    else
      warn "MLX server not running on :5044 — demo is synthetic so OK, but local-surgeon scenes will be slower"
      info "start: bash ${REPO_ROOT}/scripts/start-llm.sh"
    fi
  else
    ok "Intel host (ARCH=${ARCH}) — MLX skipped, demo is synthetic so this is fine"
  fi
fi

# ── 5. /tmp clean of stale demo pidfiles ──────────────────────────────────
banner "[5] /tmp clean of stale demo pidfiles"
stale=0
for pf in /tmp/demo-st-nats.pid /tmp/demo-st-daemon.pid /tmp/demo-st-admin.pid; do
  [ -f "$pf" ] || continue
  pid="$(cat "$pf" 2>/dev/null || true)"
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    warn "pidfile $pf points to live pid=$pid (previous demo not cleaned up)"
    info "stop with: kill $pid && rm $pf  — or run scripts/demo-surgeon-theater.sh which traps cleanup"
    stale=$((stale+1))
  else
    rm -f "$pf"
  fi
done
[ "$stale" -eq 0 ] && ok "no stale demo pidfiles"

# ── 6. ~/recordings exists + > 5 GB free ──────────────────────────────────
banner "[6] ~/recordings + disk space"
if [ "$NO_DISK" -eq 1 ]; then
  ok "disk-space check skipped (--no-disk)"
else
  REC_DIR="$HOME/recordings"
  if [ ! -d "$REC_DIR" ]; then
    if mkdir -p "$REC_DIR" 2>/dev/null; then
      ok "created ~/recordings"
    else
      fail "~/recordings missing and could not be created"
    fi
  else
    ok "~/recordings exists"
  fi
  # GB free on the parent volume
  if [ -d "$REC_DIR" ]; then
    free_gb="$(df -g "$REC_DIR" 2>/dev/null | awk 'NR==2 {print $4}')"
    if [ -n "${free_gb:-}" ] && [ "$free_gb" -ge 5 ] 2>/dev/null; then
      ok "~/recordings has ${free_gb} GB free (≥ 5 GB)"
    elif [ -n "${free_gb:-}" ]; then
      warn "~/recordings has only ${free_gb} GB free (< 5 GB recommended)"
    else
      warn "could not determine free disk space on ~/recordings"
    fi
  fi
fi

# ── 7. Do-Not-Disturb (best-effort macOS check) ───────────────────────────
banner "[7] Do Not Disturb (macOS focus)"
if [ "$NO_DND" -eq 1 ]; then
  ok "DnD check skipped (--no-dnd)"
elif [[ "$(uname -s)" == "Darwin" ]]; then
  # macOS Sonoma/Sequoia: ~/Library/DoNotDisturb/DB/Assertions.json holds
  # active focus assertions. We don't fail if we can't read it; just warn.
  dnd_db="$HOME/Library/DoNotDisturb/DB/Assertions.json"
  if [ -r "$dnd_db" ] && grep -q '"name"' "$dnd_db" 2>/dev/null; then
    ok "Do Not Disturb appears active (focus assertion present)"
  else
    warn "Do Not Disturb may not be on — turn it on before recording"
    info "menu bar → Control Center → Focus → Do Not Disturb"
  fi
else
  ok "non-macOS — skipping DnD check"
fi

# ── Summary ───────────────────────────────────────────────────────────────
banner ""
banner "═══ summary ═══"
printf "  ${C_G}OK${C_X}=%d  ${C_Y}WARN${C_X}=%d  ${C_R}FAIL${C_X}=%d  TOTAL=%d\n" \
  "$OK_COUNT" "$WARN_COUNT" "$FAIL_COUNT" "$TOTAL"

if [ "$FAIL_COUNT" -gt 0 ]; then
  printf "${C_R}NOT READY${C_X} — fix the FAIL items above before recording.\n"
  exit 1
fi
printf "${C_G}READY${C_X} — paste-and-press: bash scripts/demo-record-onetake.sh\n"
exit 0
