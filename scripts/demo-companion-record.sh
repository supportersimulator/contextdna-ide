#!/usr/bin/env bash
# scripts/demo-companion-record.sh — Aaron's recording wrapper.
#
# Runs alongside scripts/demo-surgeon-theater.sh to capture screen + mic.
# This script is SCAFFOLDING ONLY — N5 does NOT invoke screencapture or
# ffmpeg under any flag. Aaron runs this manually when he records.
#
# Usage (Aaron, when ready to record):
#
#   # Terminal 1 — start the demo:
#   bash scripts/demo-surgeon-theater.sh --interactive=1
#
#   # Terminal 2 — start the recording (defaults to ~/Movies/demo-st-<ts>.mov):
#   bash scripts/demo-companion-record.sh
#
#   # When the demo finishes, hit Ctrl-C in Terminal 2.
#
# Flags:
#   --no-record       Print the planned ffmpeg command, do not run.
#   --output PATH     Override output path.
#   --display N       macOS display index (default 1).
#   --audio "Device"  AVFoundation audio device name; defaults to system mic.
#   --fps N           Capture frame rate (default 30).
#
# Two paths supported:
#   1. ffmpeg (cross-platform, recommended for control): captures avfoundation
#      input on macOS at the given fps + audio device.
#   2. screencapture -v (macOS native, lower fidelity): used if ffmpeg missing.
#
# Verifies before recording:
#   - Demo script exists and is executable.
#   - admin.contextdna.io is reachable on the configured port.
#   - Output directory writable.
#
# We refuse to overwrite an existing file unless --force is given.
# ---------------------------------------------------------------------------
set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_SCRIPT="$REPO_ROOT/scripts/demo-surgeon-theater.sh"

NO_RECORD=0
FORCE=0
OUT="$HOME/Movies/demo-surgeon-theater-$(date +%Y%m%d-%H%M%S).mov"
DISPLAY_IDX=1
AUDIO_DEV=""   # blank → no audio; pass --audio "MacBook Pro Microphone" to enable
FPS=30
ADMIN_PORT="${ADMIN_PORT:-3000}"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-record)  NO_RECORD=1 ;;
    --force)      FORCE=1 ;;
    --output)     OUT="${2:?}"; shift ;;
    --display)    DISPLAY_IDX="${2:?}"; shift ;;
    --audio)      AUDIO_DEV="${2:?}"; shift ;;
    --fps)        FPS="${2:?}"; shift ;;
    --port-admin) ADMIN_PORT="${2:?}"; shift ;;
    -h|--help)    grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[record] unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── Pretty ──
say()  { printf "%b\n" "$*"; }
ok()   { printf "  ✓ %s\n" "$*"; }
warn() { printf "  ⚠ %s\n" "$*"; }
fail() { printf "  ✗ %s\n" "$*" >&2; }

# ── Pre-flight ──
[ -x "$DEMO_SCRIPT" ] || { fail "$DEMO_SCRIPT not executable"; exit 1; }
ok "demo script: $DEMO_SCRIPT"

if curl -sf --max-time 2 "http://127.0.0.1:${ADMIN_PORT}/" >/dev/null 2>&1; then
  ok "admin.contextdna.io reachable :${ADMIN_PORT}"
else
  warn "admin.contextdna.io NOT reachable :${ADMIN_PORT} — start it before recording"
fi

OUT_DIR="$(dirname "$OUT")"
[ -d "$OUT_DIR" ] || mkdir -p "$OUT_DIR" || { fail "cannot create $OUT_DIR"; exit 1; }
[ -w "$OUT_DIR" ] || { fail "$OUT_DIR not writable"; exit 1; }

if [ -e "$OUT" ] && [ "$FORCE" -ne 1 ]; then
  fail "$OUT already exists (use --force to overwrite)"
  exit 1
fi
ok "output: $OUT"

# ── Build the recording command ──
TOOL=""
CMD=""
if command -v ffmpeg >/dev/null 2>&1 && [[ "$(uname -s)" == "Darwin" ]]; then
  TOOL="ffmpeg"
  # avfoundation input format for macOS. video device "1" = main display
  # (`ffmpeg -f avfoundation -list_devices true -i ""` to enumerate).
  if [ -n "$AUDIO_DEV" ]; then
    INPUT="${DISPLAY_IDX}:${AUDIO_DEV}"
  else
    INPUT="${DISPLAY_IDX}:none"
  fi
  CMD=(ffmpeg -y -hide_banner -loglevel warning
       -f avfoundation -framerate "$FPS" -capture_cursor 1 -i "$INPUT"
       -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p
       -c:a aac -b:a 128k
       "$OUT")
elif [[ "$(uname -s)" == "Darwin" ]] && command -v screencapture >/dev/null 2>&1; then
  TOOL="screencapture"
  # screencapture -v records the whole main display until Ctrl-C / SIGINT.
  CMD=(screencapture -v "$OUT")
else
  fail "no recorder found (ffmpeg/screencapture). install one or run --no-record."
  exit 1
fi

ok "tool: $TOOL"
say ""
say "  planned command:"
say "  ${CMD[*]}"
say ""

if [ "$NO_RECORD" -eq 1 ]; then
  ok "[--no-record] not running."
  exit 0
fi

# ── Refuse to record automatically — Aaron's choice. ──
fail "this script intentionally does NOT auto-record."
fail "to actually start recording, pass --force-record (not implemented)"
fail "or paste the planned command above into a terminal yourself."
say ""
say "  rationale: keeping recording an explicit human action prevents"
say "             surprise ~hour-long captures or full-disk situations."
exit 0
