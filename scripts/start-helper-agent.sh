#!/bin/bash
#
# Helper Agent Service Launcher (for launchd)
#
# This script runs in the FOREGROUND so launchd can manage the process.
# launchd calls this, and KeepAlive=true auto-restarts on crash.
#
# Manual usage:  ./scripts/start-helper-agent.sh
# launchd usage: Configured in com.contextdna.unified.plist
#

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/context-dna/src"
# HOME must be set by the environment (launchd or shell)

# R-0021 hardening: launchd's default PATH omits /usr/sbin, so plain `lsof`
# silently fails and port_in_use returns false, letting us race uvicorn into a
# bind failure (9266 crash-loop). Use the absolute path defensively even when
# the parent has fixed PATH, so this script stays correct under stripped envs.
LSOF_BIN="/usr/sbin/lsof"
if [ ! -x "$LSOF_BIN" ]; then
    # Fallback to PATH lookup (developer machines may have lsof in /usr/bin).
    LSOF_BIN="$(command -v lsof 2>/dev/null || true)"
fi

# Use the venv python directly
# Use 'python' not 'python3' - venv symlinks differ (python→3.14, python3→3.9)
PYTHON="${VENV_PYTHON:-$REPO_ROOT/.venv/bin/python}"

# Ensure logs directory exists
mkdir -p "$REPO_ROOT/logs"

# ============================================================================
# STARTUP LOCK: Prevent race condition with watchdog/launchd dual restart
# ============================================================================
# Problem: When agent_service dies, both watchdog AND launchd try to restart.
# They race to bind port 8080, one fails with exit 78, triggering more restarts.
# Solution: Startup lock + port check + exponential backoff.

LOCK_FILE="$REPO_ROOT/memory/.agent_service_startup.lock"
PORT=8080
MAX_WAIT=30  # Maximum seconds to wait for port to free

# Function to check if port is in use
port_in_use() {
    if [ -z "$LSOF_BIN" ]; then
        # ZSF: lsof unavailable — record once via stderr (launchd captures it
        # in agent_service.log), and fall back to /dev/tcp probe so we never
        # silently say "port free" and race uvicorn into bind failure.
        echo "[$(date '+%H:%M:%S')] WARN: lsof not found, falling back to /dev/tcp probe" >&2
        ( exec 3<>"/dev/tcp/127.0.0.1/$PORT" ) 2>/dev/null && return 0
        return 1
    fi
    "$LSOF_BIN" -i :$PORT -sTCP:LISTEN >/dev/null 2>&1
}

# Detect whether the listener is a Docker-backed container (e.g.
# `contextdna-helper-agent`). Docker forwards via com.docker.backend, which
# we must NOT SIGTERM — killing it tears down all containers. When detected,
# we exit cleanly so launchd can simmer down via ThrottleInterval instead of
# hammering through 9000+ respawns trying to murder Docker.
listener_is_docker() {
    [ -z "$LSOF_BIN" ] && return 1
    "$LSOF_BIN" -i :$PORT -sTCP:LISTEN 2>/dev/null \
        | awk 'NR>1 {print $1}' \
        | grep -qiE 'docker|com\.docke|vpnkit'
}

# Function to get lock file age in seconds
get_lock_age() {
    if [ -f "$LOCK_FILE" ]; then
        # macOS stat format
        local mtime=$(stat -f %m "$LOCK_FILE" 2>/dev/null)
        if [ -z "$mtime" ]; then
            # Linux stat format fallback
            mtime=$(stat -c %Y "$LOCK_FILE" 2>/dev/null)
        fi
        if [ -n "$mtime" ]; then
            echo $(( $(date +%s) - mtime ))
        else
            echo 999  # If can't read, assume old
        fi
    else
        echo 999  # No lock file = proceed
    fi
}

# Check 1: Recent startup attempt?
LOCK_AGE=$(get_lock_age)
if [ "$LOCK_AGE" -lt 10 ]; then
    echo "[$(date '+%H:%M:%S')] Another instance started ${LOCK_AGE}s ago, waiting 5s..."
    sleep 5
fi

# Create/update lock file (atomically)
echo "$$:$(date +%s)" > "$LOCK_FILE"

# Check 2: Port already in use?
if port_in_use; then
    echo "[$(date '+%H:%M:%S')] Port $PORT in use, checking if it's healthy..."

    # Quick health check - if healthy, exit cleanly (another instance is running)
    if curl -s --max-time 2 "http://localhost:$PORT/health" | grep -q "ok\|healthy"; then
        echo "[$(date '+%H:%M:%S')] Healthy instance already running, exiting cleanly"
        rm -f "$LOCK_FILE"
        exit 0
    fi

    # If a Docker container holds the port, NEVER try to SIGTERM —
    # com.docker.backend manages the entire Docker subsystem. Exit cleanly
    # and rely on launchd ThrottleInterval to back off further restarts.
    if listener_is_docker; then
        echo "[$(date '+%H:%M:%S')] Port $PORT held by Docker (contextdna-helper-agent or similar). Refusing to SIGTERM Docker; exiting cleanly."
        rm -f "$LOCK_FILE"
        exit 0
    fi

    # Port bound but not healthy - kill the zombie process
    echo "[$(date '+%H:%M:%S')] Port bound but unhealthy, attempting cleanup..."
    ZOMBIE_PID=$([ -n "$LSOF_BIN" ] && "$LSOF_BIN" -i :$PORT -sTCP:LISTEN -t 2>/dev/null)
    if [ -n "$ZOMBIE_PID" ]; then
        # Check FD count — high FD count confirms zombie/leaked state
        FD_COUNT=$(lsof -p "$ZOMBIE_PID" 2>/dev/null | wc -l)
        echo "[$(date '+%H:%M:%S')] Zombie PID $ZOMBIE_PID has $FD_COUNT FDs, sending SIGTERM..."
        kill "$ZOMBIE_PID" 2>/dev/null
        sleep 3
        if kill -0 "$ZOMBIE_PID" 2>/dev/null; then
            echo "[$(date '+%H:%M:%S')] SIGTERM failed, sending SIGKILL to $ZOMBIE_PID..."
            kill -9 "$ZOMBIE_PID" 2>/dev/null
            sleep 2
        fi
    fi

    # Wait for port to free after cleanup
    WAITED=0
    while port_in_use && [ $WAITED -lt $MAX_WAIT ]; do
        sleep 1
        WAITED=$((WAITED + 1))
        if [ $((WAITED % 5)) -eq 0 ]; then
            echo "[$(date '+%H:%M:%S')] Still waiting for port $PORT... (${WAITED}s)"
        fi
    done

    if port_in_use; then
        echo "[$(date '+%H:%M:%S')] ERROR: Port $PORT still in use after ${MAX_WAIT}s, aborting"
        rm -f "$LOCK_FILE"
        exit 1
    fi

    echo "[$(date '+%H:%M:%S')] Port $PORT freed after ${WAITED}s"
fi

# Small delay after lock acquired to let any racing processes see it
sleep 1

echo "[$(date '+%H:%M:%S')] Starting agent_service on port $PORT..."

# ============================================================================
# Run uvicorn in foreground (exec replaces shell — launchd tracks the real process)
exec "$PYTHON" -m uvicorn memory.agent_service:app \
    --host 0.0.0.0 \
    --port $PORT \
    2>&1
