#!/usr/bin/env bash
# repair-peer.sh — Generic L5 SSH-from-third-party fleet peer repair.
#
# Invoked by the fleet daemon's peer-quorum L5 assist path in
# tools/fleet_nerve_nats.py (_run_repair_peer_script) when a node wins the
# fleet.peer.assist.claim election for a struggling peer.
#
# Inputs (env vars set by caller) + positional arg:
#   $1                      — target node id (e.g. mac2)  [required, validated]
#   FLEET_PEER_SSH_USER     — SSH user on target          [required]
#   FLEET_PEER_SSH_HOST     — SSH host/IP of target       [required]
#   FLEET_PEER_NODE_ID      — same as $1 (sanity)          [optional]
#   FLEET_PEER_BROKEN       — comma-separated broken channels list (log only)
#
# Idempotency: acquires /tmp/fleet-repair-<target>.lock on the TARGET via SSH
# using flock (Linux) or noclobber (macOS). Safe to run concurrently — only
# one wins; the other exits 0 with "already-in-progress" tail.
#
# Platform detection: runs `uname -s` on target. macOS uses launchctl
# bootout/bootstrap; Linux uses systemctl --user restart fleet-nerve.
# Falls back to pkill+nohup exec when no service manager is detected.
#
# Does NOT bypass IP allowlists or HMAC — only restarts the daemon process.

set -euo pipefail

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
    echo "ERR: usage: $0 <target-node-id>" >&2
    exit 64
fi
# Defence-in-depth: only alnum + dash/underscore permitted (the daemon already
# filters, but we may be run by hand or by a future caller).
if ! [[ "$TARGET" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "ERR: target '$TARGET' contains unsafe chars" >&2
    exit 64
fi

USER_="${FLEET_PEER_SSH_USER:-}"
HOST="${FLEET_PEER_SSH_HOST:-}"
BROKEN="${FLEET_PEER_BROKEN:-}"
if [[ -z "$USER_" || -z "$HOST" ]]; then
    echo "ERR: FLEET_PEER_SSH_USER and FLEET_PEER_SSH_HOST must be set" >&2
    exit 64
fi

LOG_PREFIX="[repair-peer][${TARGET}]"
echo "${LOG_PREFIX} starting L5 repair: ${USER_}@${HOST} (broken=${BROKEN})"

# Remote script — acquires lock, detects platform, restarts daemon.
# Heredoc is quoted so no local expansion; the remote values we want interpolated
# are passed through the SSH argv instead (more auditable than a mixed heredoc).
REMOTE_SCRIPT=$(cat <<'EOF'
set -eu
TARGET_NODE="$1"
BROKEN="$2"
LOCK="/tmp/fleet-repair-${TARGET_NODE}.lock"

# Platform-agnostic noclobber lockfile (works on macOS + Linux out of the box).
# Acquire with O_EXCL; exit 0 cleanly if another assister already holds it.
if ! ( set -o noclobber; echo "$$:$(date +%s)" > "$LOCK" ) 2>/dev/null; then
    LOCK_AGE=$(( $(date +%s) - $(stat -c%Y "$LOCK" 2>/dev/null || stat -f%m "$LOCK" 2>/dev/null || echo 0) ))
    # Stale lock (>300s) — take it over; daemon repair takes <90s nominally.
    if [ "$LOCK_AGE" -gt 300 ]; then
        rm -f "$LOCK"
        ( set -o noclobber; echo "$$:$(date +%s)" > "$LOCK" ) 2>/dev/null || {
            echo "already-in-progress (raced stale-lock replace)"
            exit 0
        }
    else
        echo "already-in-progress (lock held ${LOCK_AGE}s ago, pid=$(cat "$LOCK" 2>/dev/null || echo ?))"
        exit 0
    fi
fi
trap 'rm -f "$LOCK"' EXIT

echo "lock acquired on $(hostname -s) for ${TARGET_NODE} (broken=${BROKEN})"

OS=$(uname -s)
REPAIRED="no"
case "$OS" in
    Darwin)
        # Prefer launchctl if a fleet-nerve agent is registered; fall back to pkill+nohup.
        if launchctl list 2>/dev/null | grep -q "io.contextdna.fleet-nerve"; then
            echo "darwin: launchctl kickstart io.contextdna.fleet-nerve"
            launchctl kickstart -k "gui/$(id -u)/io.contextdna.fleet-nerve" || true
            REPAIRED="launchctl"
        else
            echo "darwin: no launch agent — pkill + nohup"
            pkill -f fleet_nerve_nats.py 2>/dev/null || true
            sleep 2
            REPO="${HOME}/dev/er-simulator-superrepo"
            if [ -d "$REPO" ]; then
                cd "$REPO"
                MULTIFLEET_NODE_ID="${TARGET_NODE}" \
                    nohup python3 tools/fleet_nerve_nats.py serve \
                    >/tmp/fleet-nats-${TARGET_NODE}.log 2>&1 &
                REPAIRED="nohup"
            else
                echo "darwin: repo not found at ${REPO} — cannot restart"
            fi
        fi
        ;;
    Linux)
        if systemctl --user status fleet-nerve >/dev/null 2>&1; then
            echo "linux: systemctl --user restart fleet-nerve"
            systemctl --user restart fleet-nerve || true
            REPAIRED="systemctl-user"
        elif command -v systemctl >/dev/null 2>&1 && sudo -n systemctl status fleet-nerve >/dev/null 2>&1; then
            echo "linux: sudo systemctl restart fleet-nerve"
            sudo -n systemctl restart fleet-nerve || true
            REPAIRED="systemctl-system"
        else
            echo "linux: no systemd unit — pkill + nohup"
            pkill -f fleet_nerve_nats.py 2>/dev/null || true
            sleep 2
            REPO="${HOME}/dev/er-simulator-superrepo"
            if [ -d "$REPO" ]; then
                cd "$REPO"
                MULTIFLEET_NODE_ID="${TARGET_NODE}" \
                    nohup python3 tools/fleet_nerve_nats.py serve \
                    >/tmp/fleet-nats-${TARGET_NODE}.log 2>&1 &
                REPAIRED="nohup"
            else
                echo "linux: repo not found at ${REPO} — cannot restart"
            fi
        fi
        ;;
    *)
        echo "unsupported OS: $OS"
        exit 2
        ;;
esac

# Verify the daemon is actually up again (port 8855 or process match)
sleep 3
if pgrep -f fleet_nerve_nats.py >/dev/null 2>&1; then
    echo "verify OK: fleet_nerve_nats.py running (method=${REPAIRED})"
    exit 0
fi
echo "verify FAILED: fleet_nerve_nats.py not running after repair (method=${REPAIRED})"
exit 1
EOF
)

# Push the remote script via ssh stdin so no shell quoting hell.
set +e
OUT=$(ssh -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
      "${USER_}@${HOST}" bash -s -- "${TARGET}" "${BROKEN}" <<<"${REMOTE_SCRIPT}" 2>&1)
RC=$?
set -e

printf '%s\n' "${OUT}" | sed "s|^|${LOG_PREFIX} |"

if [[ $RC -eq 0 ]]; then
    echo "${LOG_PREFIX} OK"
    exit 0
fi
echo "${LOG_PREFIX} FAILED exit=${RC}" >&2
exit "$RC"
