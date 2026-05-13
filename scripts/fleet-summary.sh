#!/usr/bin/env bash
# fleet-summary.sh — Terse one-liner fleet status for status bars / task injection.
# Output example:
#   FLEET mac1 | NATS ✓ | daemon ✓ | watchdog ✓ | pool 8/10 done | wave 0/2
# ZSF: all checks wrapped; never blocks if a service is down.

set -uo pipefail

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
PORT="${FLEET_NERVE_PORT:-8855}"
_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# NATS
if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    _nats_status="NATS ✓"
else
    _nats_status="NATS ✗"
fi

# Fleet daemon
if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    _daemon_status="daemon ✓"
else
    _daemon_status="daemon ✗"
fi

# Watchdog
if launchctl list 2>/dev/null | grep -q fleet-nerve-watchdog; then
    _wd_status="watchdog ✓"
else
    _wd_status="watchdog ✗"
fi

# Green-light pool
_GL_FILE="${_REPO_ROOT}/.fleet/priorities/green-light.md"
if [ -f "$_GL_FILE" ]; then
    _gl_done=$(grep '\- \[x\]' "$_GL_FILE" 2>/dev/null | awk 'END{print NR}')
    _gl_total=$(grep '\- \[' "$_GL_FILE" 2>/dev/null | awk 'END{print NR}')
    _pool_status="pool ${_gl_done}/${_gl_total} done"
else
    _pool_status="pool ?"
fi

# Wave / unclaimed items (open tasks)
if [ -f "$_GL_FILE" ]; then
    _gl_open=$(grep '\- \[ \]' "$_GL_FILE" 2>/dev/null | awk 'END{print NR}')
    _gl_claimed=$(grep '\- \[⏳' "$_GL_FILE" 2>/dev/null | awk 'END{print NR}')
    _wave_status="wave ${_gl_claimed}/${_gl_open}"
else
    _wave_status="wave ?"
fi

printf "FLEET %s | %s | %s | %s | %s | %s\n" \
    "$NODE_ID" "$_nats_status" "$_daemon_status" "$_wd_status" "$_pool_status" "$_wave_status"
