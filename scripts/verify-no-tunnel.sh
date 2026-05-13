#!/usr/bin/env bash
# verify-no-tunnel.sh — Watchdog: ensure no SSH tunnel is hijacking port 4222.
#
# Context: `io.contextdna.fleet-tunnel` (autossh) used to tunnel 127.0.0.1:4222
# on workers to the chief's NATS. With clustering enabled, each node runs its
# own NATS server — a tunnel on :4222 STEALS the port from the local server and
# creates an "island" topology where peers can't see each other.
#
# This watchdog:
#   - Scans lsof for ssh/autossh holding :4222.
#   - If found → logs CRITICAL, kills the offender, removes stale pidfiles.
#   - Exits non-zero so xbar / fleet-check consumers see the failure.
#
# Usage: bash scripts/verify-no-tunnel.sh
# Returns: 0 = clean, 2 = tunnel found and killed, 3 = tunnel found but kill failed
set -uo pipefail

LOG="/tmp/verify-no-tunnel.log"
TS="$(date '+%Y-%m-%d %H:%M:%S')"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"

_log() { echo "[$TS] [$NODE_ID] $*" | tee -a "$LOG"; }

# ── 1. Direct check: lsof for ssh on :4222 ──
OFFENDERS="$(lsof -iTCP:4222 -P -n 2>/dev/null | awk 'NR>1 && ($1 ~ /^(ssh|autossh)$/) {print $2":"$1}' | sort -u)"

# ── 2. Belt-and-suspenders: scan process table for autossh + any ssh with :4222 in args ──
PS_OFFENDERS="$(ps -Ao pid,command 2>/dev/null | awk '
    /autossh/ && !/awk/ {print $1":autossh"; next}
    /ssh / && /4222/ && !/awk/ && !/verify-no-tunnel/ {print $1":ssh"}
' | sort -u)"

ALL="$(printf '%s\n%s\n' "$OFFENDERS" "$PS_OFFENDERS" | awk 'NF' | sort -u)"

if [ -z "$ALL" ]; then
    # Clean. Quiet exit.
    exit 0
fi

_log "CRITICAL: tunnel hijack detected on :4222 — offenders: $(echo "$ALL" | tr '\n' ' ')"

KILL_FAILED=0
for entry in $ALL; do
    PID="${entry%%:*}"
    NAME="${entry#*:}"
    if [ -z "$PID" ] || ! [[ "$PID" =~ ^[0-9]+$ ]]; then
        continue
    fi
    _log "  killing $NAME PID=$PID"
    if kill -9 "$PID" 2>/dev/null; then
        _log "  ✓ killed PID=$PID"
    else
        _log "  ✗ kill failed PID=$PID (already gone or permission denied)"
        # Only count as failure if process is still alive
        kill -0 "$PID" 2>/dev/null && KILL_FAILED=1
    fi
done

# ── 3. Scrub stale tunnel artifacts that would re-spawn via self_heal watchdog ──
if [ -d "$HOME/.fleet-tunnels" ]; then
    rm -f "$HOME/.fleet-tunnels"/tunnel-*.pid
    _log "  removed stale pidfiles from ~/.fleet-tunnels"
fi

# ── 4. Double-check plist is NOT active ──
if launchctl print "gui/$(id -u)/io.contextdna.fleet-tunnel" >/dev/null 2>&1; then
    _log "  WARN: io.contextdna.fleet-tunnel is LOADED in launchctl — unloading"
    # Locate whichever file is registered (disabled or live)
    for p in "$HOME/Library/LaunchAgents/io.contextdna.fleet-tunnel.plist" \
             "$HOME/Library/LaunchAgents/io.contextdna.fleet-tunnel.plist.disabled" \
             "$HOME/Library/LaunchAgents/io.contextdna.fleet-tunnel.plist.purged-2026-04-18"; do
        [ -f "$p" ] && launchctl unload "$p" 2>/dev/null && _log "    unloaded $p"
    done
fi

if [ "$KILL_FAILED" = "1" ]; then
    _log "FAIL: one or more offenders could not be killed"
    exit 3
fi

_log "RECOVERED: offenders killed, port :4222 should now be clean"
exit 2
