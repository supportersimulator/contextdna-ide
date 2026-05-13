#!/usr/bin/env bash
# scripts/demo-record-onetake.sh — Aaron's paste-and-record helper.
#
# One paste-and-press command that:
#   1. Runs scripts/demo-preflight.sh (exits 1 if anything is FAIL).
#   2. Builds the canonical output path:
#         ~/recordings/contextdna-corrigibility-moat-<YYYY-MM-DD>-take<N>.mov
#      Auto-numbers <N> by scanning existing files unless --take is given.
#   3. Starts ffmpeg (or screencapture -v) recording the main display.
#   4. Runs scripts/demo-surgeon-theater.sh --interactive=0 (auto, ~70 s).
#   5. (Optional) Runs scripts/demo-halt-rehearse.sh after the main demo
#      if --with-halt is set (Scene-6 cutaway).
#   6. SIGINTs the recorder so it finalises the .mov, then prints the path
#      + size.
#
# Flags:
#   --take N           override take number (else auto-increment).
#   --with-halt        also run demo-halt-rehearse.sh after Scene 5.
#   --no-record        print the planned commands, exit 0 (does NOT record).
#   --dry-run          alias for --no-record.
#   --no-preflight     skip preflight (use only if you JUST ran it).
#   --port-daemon N    forwarded to demo + preflight.
#   --port-admin N     forwarded to demo + preflight.
#   --display N        macOS display index for ffmpeg (default 1).
#   --audio "Device"   AVFoundation audio device; default "none".
#   --fps N            recorder fps (default 30).
#   -h | --help        show this header.
#
# Exit codes:
#   0  recording finished (or --no-record completed).
#   1  preflight failed, recorder failed, or demo failed.
#   2  bad CLI args.
#
# Why this exists:
#   N5's demo-companion-record.sh intentionally does NOT auto-record (it
#   refuses with a "paste the command yourself" message). That's correct
#   for casual use, but for a recording session Aaron wants ONE command.
#   This wrapper provides that, while still keeping a --dry-run path for
#   safety.
# ---------------------------------------------------------------------------
set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

TAKE=""
WITH_HALT=0
NO_RECORD=0
NO_PREFLIGHT=0
DAEMON_PORT="${FLEET_DAEMON_PORT:-8855}"
ADMIN_PORT="${ADMIN_PORT:-3000}"
DISPLAY_IDX=1
AUDIO_DEV="none"
FPS=30

while [ $# -gt 0 ]; do
  case "$1" in
    --take)          TAKE="${2:?}"; shift ;;
    --with-halt)     WITH_HALT=1 ;;
    --no-record)     NO_RECORD=1 ;;
    --dry-run)       NO_RECORD=1 ;;
    --no-preflight)  NO_PREFLIGHT=1 ;;
    --port-daemon)   DAEMON_PORT="${2:?}"; shift ;;
    --port-admin)    ADMIN_PORT="${2:?}"; shift ;;
    --display)       DISPLAY_IDX="${2:?}"; shift ;;
    --audio)         AUDIO_DEV="${2:?}"; shift ;;
    --fps)           FPS="${2:?}"; shift ;;
    -h|--help)       grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[onetake] unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── Pretty ──
if [ -t 1 ]; then
  C_G=$'\033[0;32m'; C_R=$'\033[0;31m'; C_Y=$'\033[0;33m'; C_X=$'\033[0m'
  C_B=$'\033[1m'; C_C=$'\033[0;36m'
else
  C_G=""; C_R=""; C_Y=""; C_X=""; C_B=""; C_C=""
fi
say()  { printf "%b\n" "$*"; }
ok()   { printf "  ${C_G}OK${C_X}   %s\n" "$*"; }
warn() { printf "  ${C_Y}WARN${C_X} %s\n" "$*"; }
fail() { printf "  ${C_R}FAIL${C_X} %s\n" "$*" >&2; }

# ── Preflight ─────────────────────────────────────────────────────────────
if [ "$NO_PREFLIGHT" -eq 0 ]; then
  say ""
  say "${C_B}[1/4] preflight${C_X}"
  if bash "$REPO_ROOT/scripts/demo-preflight.sh" \
       --port-daemon "$DAEMON_PORT" --port-admin "$ADMIN_PORT" --quiet; then
    ok "preflight passed"
  else
    fail "preflight FAILED — fix flagged items, then re-run"
    fail "for details: bash $REPO_ROOT/scripts/demo-preflight.sh"
    exit 1
  fi
else
  warn "preflight skipped (--no-preflight)"
fi

# ── Resolve take number + output path ─────────────────────────────────────
REC_DIR="$HOME/recordings"
mkdir -p "$REC_DIR"
DATE_TAG="$(date +%F)"
PREFIX="contextdna-corrigibility-moat-${DATE_TAG}"

if [ -z "$TAKE" ]; then
  # Find max existing take number for this date, increment by 1.
  max_n=0
  for f in "$REC_DIR"/${PREFIX}-take*.mov; do
    [ -e "$f" ] || continue
    n="$(basename "$f" | sed -E "s/^${PREFIX}-take([0-9]+)\\.mov$/\\1/")"
    [[ "$n" =~ ^[0-9]+$ ]] || continue
    [ "$n" -gt "$max_n" ] && max_n="$n"
  done
  TAKE=$((max_n + 1))
fi
OUT="${REC_DIR}/${PREFIX}-take${TAKE}.mov"

say ""
say "${C_B}[2/4] output path${C_X}"
ok "$OUT  (take=${TAKE})"
if [ -e "$OUT" ]; then
  fail "$OUT already exists — pass a higher --take N"
  exit 1
fi

# ── Build recorder command ────────────────────────────────────────────────
TOOL=""
REC_CMD=()
if command -v ffmpeg >/dev/null 2>&1 && [[ "$(uname -s)" == "Darwin" ]]; then
  TOOL="ffmpeg"
  if [ "$AUDIO_DEV" = "none" ] || [ -z "$AUDIO_DEV" ]; then
    INPUT="${DISPLAY_IDX}:none"
  else
    INPUT="${DISPLAY_IDX}:${AUDIO_DEV}"
  fi
  REC_CMD=(ffmpeg -y -hide_banner -loglevel warning
           -f avfoundation -framerate "$FPS" -capture_cursor 1 -i "$INPUT"
           -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p
           -c:a aac -b:a 128k
           "$OUT")
elif [[ "$(uname -s)" == "Darwin" ]] && command -v screencapture >/dev/null 2>&1; then
  TOOL="screencapture"
  REC_CMD=(screencapture -v "$OUT")
else
  fail "no recorder available (ffmpeg/screencapture). install one or use --no-record."
  exit 1
fi
ok "recorder: $TOOL"

DEMO_CMD=(bash "$REPO_ROOT/scripts/demo-surgeon-theater.sh"
          --interactive=0 --port-daemon "$DAEMON_PORT" --port-admin "$ADMIN_PORT")
HALT_CMD=(bash "$REPO_ROOT/scripts/demo-halt-rehearse.sh")

say ""
say "${C_B}[3/4] planned commands${C_X}"
say "  ${C_C}recorder${C_X}: ${REC_CMD[*]}"
say "  ${C_C}demo${C_X}    : ${DEMO_CMD[*]}"
[ "$WITH_HALT" -eq 1 ] && say "  ${C_C}halt${C_X}    : ${HALT_CMD[*]}"

if [ "$NO_RECORD" -eq 1 ]; then
  say ""
  ok "[--dry-run / --no-record] not running."
  exit 0
fi

# ── Record ────────────────────────────────────────────────────────────────
say ""
say "${C_B}[4/4] recording${C_X}"
say "  ${C_Y}starting recorder (${TOOL}) → ${OUT}${C_X}"

# Start recorder in background; capture pid so we can SIGINT it cleanly.
"${REC_CMD[@]}" &
REC_PID=$!
sleep 1.5  # let the recorder allocate the file + start writing frames

# Belt-and-braces: if recorder died immediately, abort.
if ! kill -0 "$REC_PID" 2>/dev/null; then
  fail "recorder exited immediately — likely a permissions prompt."
  fail "grant Screen Recording permission to your terminal in System Settings → Privacy & Security."
  exit 1
fi
ok "recorder pid=${REC_PID}"

# Cleanup handler: even if the demo throws, we still finalise the recording.
finish_recording() {
  local rc=$?
  if kill -0 "$REC_PID" 2>/dev/null; then
    say "  stopping recorder (SIGINT)…"
    kill -INT "$REC_PID" 2>/dev/null || true
    # Wait up to 10s for graceful finalise (ffmpeg flushes on SIGINT).
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$REC_PID" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$REC_PID" 2>/dev/null && kill -9 "$REC_PID" 2>/dev/null || true
  fi
  if [ -f "$OUT" ]; then
    sz="$(du -h "$OUT" 2>/dev/null | awk '{print $1}')"
    ok "recording saved: $OUT (${sz:-unknown})"
  else
    warn "no output file at $OUT — recorder may have failed"
  fi
  exit $rc
}
trap finish_recording EXIT INT TERM

# Run the demo (foreground; we want its exit status).
say "  running demo…"
"${DEMO_CMD[@]}" || warn "demo exited non-zero — keeping the recording for triage"

if [ "$WITH_HALT" -eq 1 ]; then
  say ""
  say "  ${C_C}--with-halt${C_X}: running HALT rehearsal cutaway…"
  "${HALT_CMD[@]}" || warn "halt rehearsal exited non-zero — keeping the recording for triage"
fi

# Trap will SIGINT the recorder and print the saved path + size.
exit 0
