#!/usr/bin/env bash
# fleet-nerve-watchdog.sh — Liveness watchdog for fleet-nerve NATS daemon.
#
# WHY: launchd KeepAlive=true respawns crashed processes, but does NOT detect
# wedged-but-alive processes (e.g. /health socket dead while PID lingers).
# This watchdog probes /health every run; after 3 consecutive failures it
# SIGKILLs the daemon so launchd respawns it.
#
# Invariants:
#   - ZERO SILENT FAILURES: every state transition + kick logged to LOG_FILE.
#   - Bounded blast radius: only kills processes matching DAEMON_LABEL via launchctl.
#   - Reversible: counter persists in STATE_FILE; deleting it resets.
#
# Usage:
#   bash scripts/fleet-nerve-watchdog.sh                 # probe canonical daemon
#   FLEET_HEALTH_URL=http://127.0.0.1:8856/health \
#     FLEET_DAEMON_LABEL=test.fleet-nerve \
#     FLEET_WATCHDOG_STATE=/tmp/test-watchdog.state \
#     FLEET_WATCHDOG_LOG=/tmp/test-watchdog.log \
#     bash scripts/fleet-nerve-watchdog.sh               # test against dummy
#
# Counter contract (state file is single line: "<consecutive_fails> <total_kicks>"):
#   - consecutive_fails resets to 0 on every successful probe
#   - total_kicks (watchdog_kicks_total) is monotonic across daemon restarts
#
# Bootout/uninstall:
#   launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/io.contextdna.fleet-nerve-watchdog.plist
#   rm ~/Library/LaunchAgents/io.contextdna.fleet-nerve-watchdog.plist

set -uo pipefail

HEALTH_URL="${FLEET_HEALTH_URL:-http://127.0.0.1:8855/health}"
# Auto-detect label: some nodes use legacy 'fleet-nerve', others 'fleet-nats'.
# FLEET_DAEMON_LABEL env overrides; otherwise try fleet-nats then fleet-nerve.
_auto_label() {
    for _l in io.contextdna.fleet-nats io.contextdna.fleet-nerve; do
        if launchctl list 2>/dev/null | awk '{print $3}' | grep -qx "$_l"; then
            echo "$_l"; return
        fi
    done
    echo "io.contextdna.fleet-nats"  # safe default — launchd will miss, KICK_SKIPPED logged
}
DAEMON_LABEL="${FLEET_DAEMON_LABEL:-$(_auto_label)}"
STATE_FILE="${FLEET_WATCHDOG_STATE:-/tmp/fleet-nerve-watchdog.state}"
LOG_FILE="${FLEET_WATCHDOG_LOG:-/tmp/fleet-nerve-watchdog.log}"
FAIL_THRESHOLD="${FLEET_WATCHDOG_THRESHOLD:-3}"
HEALTH_TIMEOUT="${FLEET_WATCHDOG_TIMEOUT:-5}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(ts) $*" >> "$LOG_FILE"; }

# Read state: "consecutive_fails total_kicks"
read_state() {
    if [[ -f "$STATE_FILE" ]]; then
        read -r CONSEC KICKS < "$STATE_FILE" 2>/dev/null || { CONSEC=0; KICKS=0; }
        [[ -z "${CONSEC:-}" ]] && CONSEC=0
        [[ -z "${KICKS:-}" ]] && KICKS=0
    else
        CONSEC=0
        KICKS=0
    fi
}

write_state() {
    # Atomic write: temp + rename. ZSF: log if write fails.
    local tmp="${STATE_FILE}.tmp.$$"
    if ! echo "$1 $2" > "$tmp" 2>>"$LOG_FILE"; then
        log "ERROR write_state tmp_write_failed state_file=$STATE_FILE"
        return 1
    fi
    if ! mv "$tmp" "$STATE_FILE" 2>>"$LOG_FILE"; then
        log "ERROR write_state rename_failed state_file=$STATE_FILE"
        rm -f "$tmp"
        return 1
    fi
    return 0
}

# Find daemon PID from launchctl. Returns "" if not loaded.
find_daemon_pid() {
    launchctl list 2>/dev/null \
        | awk -v label="$DAEMON_LABEL" '$3 == label { print $1 }' \
        | head -1
}

kick_daemon() {
    local pid
    pid="$(find_daemon_pid)"
    if [[ -z "$pid" || "$pid" == "-" ]]; then
        log "KICK_SKIPPED reason=no_pid label=$DAEMON_LABEL"
        return 1
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        log "KICK_SKIPPED reason=pid_not_running pid=$pid"
        return 1
    fi
    if kill -KILL "$pid" 2>>"$LOG_FILE"; then
        log "KICK pid=$pid label=$DAEMON_LABEL reason=health_unreachable_${FAIL_THRESHOLD}x"
        return 0
    else
        log "KICK_FAILED pid=$pid label=$DAEMON_LABEL"
        return 1
    fi
}

main() {
    read_state

    # Probe /health. -f: fail on HTTP errors. -s: silent. -m: max time.
    if curl -fsS -m "$HEALTH_TIMEOUT" "$HEALTH_URL" >/dev/null 2>&1; then
        # Healthy: reset consecutive counter, preserve total kicks.
        if [[ "$CONSEC" -ne 0 ]]; then
            log "RECOVERED prior_consec=$CONSEC total_kicks=$KICKS"
        fi
        write_state 0 "$KICKS" || exit 1
        exit 0
    fi

    # Unhealthy: increment consecutive counter.
    CONSEC=$((CONSEC + 1))
    log "PROBE_FAIL consec=$CONSEC threshold=$FAIL_THRESHOLD url=$HEALTH_URL"

    if [[ "$CONSEC" -ge "$FAIL_THRESHOLD" ]]; then
        if kick_daemon; then
            KICKS=$((KICKS + 1))
        fi
        # Reset consecutive count after kick (success or failure) so we don't
        # spam. If daemon stays dead, next probe will start a new sequence.
        write_state 0 "$KICKS" || exit 1
    else
        write_state "$CONSEC" "$KICKS" || exit 1
    fi
}

main "$@"
