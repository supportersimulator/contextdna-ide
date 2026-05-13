#!/usr/bin/env bash
# scripts/demo.sh — One-command theatrical fleet demo.
#
# Boots NATS (if absent) + fleet daemon, opens the dashboard in a browser,
# runs scripts/demo_scenario.py to drive simulated 3-node activity, surgeon
# cross-examination, gate fires, and an evidence chain. Cleans everything up
# on exit (Ctrl-C or scenario completion).
#
# Flags:
#   --dry-run     Print scenario timeline only (no daemon, no NATS), exit 0.
#   --no-browser  Skip opening the browser window.
#   --duration N  Cap wall-clock at N seconds (default 180).
set -u  # do not -e: we want best-effort cleanup even on failures

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCENARIO="$REPO_ROOT/scripts/demo_scenario.py"
NATS_PIDFILE="/tmp/fleet-demo-nats.pid"
DAEMON_PIDFILE="/tmp/fleet-demo-daemon.pid"
DAEMON_LOG="/tmp/fleet-demo-daemon.log"
NATS_LOG="/tmp/fleet-demo-nats.log"
DAEMON_PORT="${FLEET_DAEMON_PORT:-8855}"
NATS_PORT="${NATS_PORT:-4222}"

DRY_RUN=0
NO_BROWSER=0
DURATION=180

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY_RUN=1 ;;
    --no-browser) NO_BROWSER=1 ;;
    --duration)   DURATION="${2:-180}"; shift ;;
    -h|--help)
      grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "[demo] unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── Platform detection ──
case "$(uname -s)" in
  Darwin) OPENER="open" ; PLATFORM="macOS" ;;
  Linux)  OPENER="xdg-open" ; PLATFORM="Linux" ;;
  *)      OPENER="" ; PLATFORM="other" ;;
esac

cleanup() {
  local rc=$?
  echo
  echo "[demo] cleaning up..."
  for pf in "$DAEMON_PIDFILE" "$NATS_PIDFILE"; do
    if [ -f "$pf" ]; then
      local pid
      pid="$(cat "$pf" 2>/dev/null || true)"
      if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        # Give the process a moment, then force if still alive.
        for _ in 1 2 3 4 5; do
          kill -0 "$pid" 2>/dev/null || break
          sleep 0.2
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
      fi
      rm -f "$pf"
    fi
  done
  echo "[demo] cleanup complete (rc=$rc)."
}
trap cleanup EXIT INT TERM

# ── --dry-run: scenario only, no infrastructure ──
if [ "$DRY_RUN" -eq 1 ]; then
  echo "[demo] dry-run mode (no daemon, no NATS, no browser)"
  PY="$(command -v python3 || command -v python)"
  exec "$PY" "$SCENARIO" --dry-run --duration "$DURATION"
fi

# ── Prereq checks ──
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "[demo] ERROR: python3 not found in PATH" >&2; exit 1
fi
PY_VER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
case "$PY_VER" in
  3.1[0-9]|3.[2-9][0-9]) ;;  # 3.10 or newer
  *) echo "[demo] WARN: Python $PY_VER detected; 3.10+ recommended" >&2 ;;
esac

NATS_BIN="$(command -v nats-server || true)"
if [ -z "$NATS_BIN" ]; then
  echo "[demo] WARN: nats-server not on PATH — fleet will run NATS-less (P2/P7 still work)"
fi

echo "[demo] platform: $PLATFORM | python: $PY_VER | duration: ${DURATION}s"

# ── Start NATS if missing ──
if [ -n "$NATS_BIN" ]; then
  if (echo > "/dev/tcp/127.0.0.1/$NATS_PORT") 2>/dev/null; then
    echo "[demo] NATS already up on :$NATS_PORT — reusing"
  else
    echo "[demo] starting nats-server on :$NATS_PORT"
    "$NATS_BIN" -p "$NATS_PORT" >"$NATS_LOG" 2>&1 &
    echo $! > "$NATS_PIDFILE"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      (echo > "/dev/tcp/127.0.0.1/$NATS_PORT") 2>/dev/null && break
      sleep 0.5
    done
  fi
fi

# ── Start fleet daemon ──
if curl -sf "http://127.0.0.1:$DAEMON_PORT/health" >/dev/null 2>&1; then
  echo "[demo] fleet daemon already up on :$DAEMON_PORT — reusing"
else
  echo "[demo] starting fleet daemon (node=demo-node) on :$DAEMON_PORT"
  (
    cd "$REPO_ROOT" && \
    MULTIFLEET_NODE_ID=demo-node \
    MULTIFLEET_DEMO_MODE=1 \
    NATS_URL="nats://127.0.0.1:$NATS_PORT" \
    FLEET_DAEMON_PORT="$DAEMON_PORT" \
      "$PY" "$REPO_ROOT/tools/fleet_nerve_nats.py" serve >"$DAEMON_LOG" 2>&1 &
    echo $! > "$DAEMON_PIDFILE"
  )
fi

# ── Wait for daemon health (up to 30s) ──
echo -n "[demo] waiting for daemon health"
HEALTHY=0
for i in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$DAEMON_PORT/health" >/dev/null 2>&1; then
    echo " — up"
    HEALTHY=1
    break
  fi
  echo -n "."
  sleep 0.5
done
if [ "$HEALTHY" -ne 1 ]; then
  echo
  echo "[demo] ERROR: daemon did not become healthy within 30s" >&2
  echo "[demo]        tail of $DAEMON_LOG:" >&2
  tail -20 "$DAEMON_LOG" >&2 || true
  exit 1
fi

# ── Open dashboard ──
DASH_URL="http://127.0.0.1:$DAEMON_PORT/dashboard"
if [ "$NO_BROWSER" -eq 0 ] && [ -n "$OPENER" ]; then
  echo "[demo] opening dashboard: $DASH_URL"
  "$OPENER" "$DASH_URL" >/dev/null 2>&1 || echo "[demo] (browser open failed — visit $DASH_URL manually)"
else
  echo "[demo] dashboard URL: $DASH_URL"
fi

# ── Run scenario in foreground ──
echo "[demo] running scenario (~${DURATION}s)..."
set +e
"$PY" "$SCENARIO" --duration "$DURATION"
rc=$?
set -e
echo "[demo] scenario exit=$rc"
exit "$rc"
