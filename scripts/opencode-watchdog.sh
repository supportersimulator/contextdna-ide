#!/usr/bin/env bash
# =============================================================================
# opencode-watchdog.sh — Kill stuck opencode --prompt processes
# =============================================================================
# Finds /Users/<user>/.opencode/bin/opencode --prompt PIDs older than
# OPENCODE_MAX_AGE_HOURS (default 24h), sends SIGTERM, waits a grace window
# (OPENCODE_GRACE_SECONDS, default 1800s = 30min), then SIGKILLs survivors.
#
# Default mode is --dry-run. Pass --apply to actually kill.
#
# Symptom that motivated this watchdog (WW3, 2026-05-12):
#   opencode --prompt sessions invoking `security find-generic-password ...` to
#   retrieve DISCORD_BOT_TOKEN hung indefinitely (PIDs 77968, 84877 accumulated
#   ~358 CPU-min before manual cleanup). Pattern: keychain prompt requires
#   interactive UI auth, but the opencode session has no TTY, so the process
#   blocks forever waiting on stdin.
#
# Root cause: opencode CLI does not bound the lifetime of --prompt invocations
# nor detect when the shell command it spawns is blocking on a TTY. Pending an
# upstream fix, this watchdog provides a safety net.
#
# Counter: /tmp/opencode-watchdog-stats.json tracks kills (ZSF — every kill is
# observable via log + counter).
#
# Usage:
#   bash scripts/opencode-watchdog.sh                  # dry-run, list stale PIDs
#   bash scripts/opencode-watchdog.sh --apply          # actually kill
#   OPENCODE_MAX_AGE_HOURS=1 bash scripts/opencode-watchdog.sh --apply
#   OPENCODE_GRACE_SECONDS=5 bash scripts/opencode-watchdog.sh --apply
#
# Exit codes:
#   0 — no stale PIDs found, or --dry-run completed
#   0 — --apply completed (success — see log for kill count)
#   1 — internal error (bad args, missing tools)
# =============================================================================

set -euo pipefail

# ---- config ----
MAX_AGE_HOURS="${OPENCODE_MAX_AGE_HOURS:-24}"
GRACE_SECONDS="${OPENCODE_GRACE_SECONDS:-1800}"
LOG_FILE="${OPENCODE_WATCHDOG_LOG:-/tmp/opencode-watchdog.log}"
STATS_FILE="${OPENCODE_WATCHDOG_STATS:-/tmp/opencode-watchdog-stats.json}"
# Pattern that identifies a stale opencode --prompt session. Override in tests.
OPENCODE_PATTERN="${OPENCODE_PATTERN:-opencode --prompt}"

# ---- args ----
MODE="dry-run"
for arg in "$@"; do
    case "$arg" in
        --apply) MODE="apply" ;;
        --dry-run) MODE="dry-run" ;;
        -h|--help)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg" >&2
            echo "Use --apply, --dry-run, or --help" >&2
            exit 1
            ;;
    esac
done

log() {
    local line
    line="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$line" >> "$LOG_FILE"
    echo "$line"
}

bump_counter() {
    # Best-effort counter persistence. Pure shell — no jq dependency.
    local key="$1"
    local cur=0
    if [[ -f "$STATS_FILE" ]]; then
        cur=$(grep -o "\"$key\":[ ]*[0-9]\+" "$STATS_FILE" 2>/dev/null | grep -o '[0-9]\+$' || echo 0)
    fi
    cur=$((cur + 1))
    # Rewrite minimal stats file (idempotent shape).
    local sigterm_count=0
    local sigkill_count=0
    local scans=0
    if [[ -f "$STATS_FILE" ]]; then
        sigterm_count=$(grep -o '"sigterm_count":[ ]*[0-9]\+' "$STATS_FILE" 2>/dev/null | grep -o '[0-9]\+$' || echo 0)
        sigkill_count=$(grep -o '"sigkill_count":[ ]*[0-9]\+' "$STATS_FILE" 2>/dev/null | grep -o '[0-9]\+$' || echo 0)
        scans=$(grep -o '"scans":[ ]*[0-9]\+' "$STATS_FILE" 2>/dev/null | grep -o '[0-9]\+$' || echo 0)
    fi
    case "$key" in
        sigterm_count) sigterm_count="$cur" ;;
        sigkill_count) sigkill_count="$cur" ;;
        scans)         scans="$cur" ;;
    esac
    cat > "$STATS_FILE" <<EOF
{"sigterm_count": $sigterm_count, "sigkill_count": $sigkill_count, "scans": $scans, "last_run": "$(date -u +%FT%TZ)"}
EOF
}

# ---- PID discovery ----
# Lists "<pid> <etime_seconds>" for processes whose command matches the
# opencode pattern. etime is process elapsed time since start (BSD ps).
list_candidates() {
    # ps fields: pid (PID), etime (D-HH:MM:SS), command
    # macOS ps doesn't support etimes — parse etime manually.
    ps -axo pid=,etime=,command= 2>/dev/null \
        | awk -v pat="$OPENCODE_PATTERN" '
            index($0, pat) > 0 {
                pid = $1
                etime = $2
                # etime: [[DD-]HH:]MM:SS
                days = 0; hh = 0; mm = 0; ss = 0
                if (index(etime, "-") > 0) {
                    split(etime, A, "-")
                    days = A[1] + 0
                    rest = A[2]
                } else {
                    rest = etime
                }
                n = split(rest, B, ":")
                if (n == 3)      { hh = B[1]+0; mm = B[2]+0; ss = B[3]+0 }
                else if (n == 2) { mm = B[1]+0; ss = B[2]+0 }
                else             { ss = B[1]+0 }
                secs = days*86400 + hh*3600 + mm*60 + ss
                print pid, secs
            }
        '
}

# ---- main scan ----
mkdir -p "$(dirname "$LOG_FILE")"
: > /dev/null  # touch noop; ensures set -e doesn't fail on empty cmds below

max_age_secs=$((MAX_AGE_HOURS * 3600))
bump_counter scans

log "scan start: mode=$MODE max_age_hours=$MAX_AGE_HOURS grace_seconds=$GRACE_SECONDS pattern='$OPENCODE_PATTERN'"

stale_pids=()
while read -r pid secs; do
    [[ -z "${pid:-}" ]] && continue
    if [[ "$secs" -ge "$max_age_secs" ]]; then
        stale_pids+=("$pid:$secs")
    fi
done < <(list_candidates)

if [[ ${#stale_pids[@]} -eq 0 ]]; then
    log "no stale opencode --prompt PIDs found"
    exit 0
fi

log "found ${#stale_pids[@]} stale candidate(s)"
for entry in "${stale_pids[@]}"; do
    pid="${entry%%:*}"
    secs="${entry##*:}"
    hours=$((secs / 3600))
    if [[ "$MODE" == "dry-run" ]]; then
        log "DRY-RUN: would SIGTERM pid=$pid age=${hours}h (grace=${GRACE_SECONDS}s then SIGKILL)"
        continue
    fi

    # --apply path: SIGTERM, wait grace, SIGKILL survivors.
    if kill -TERM "$pid" 2>/dev/null; then
        bump_counter sigterm_count
        log "SIGTERM sent: pid=$pid age=${hours}h"
    else
        log "SIGTERM failed (already gone?): pid=$pid"
        continue
    fi

    # Wait up to grace_seconds for graceful exit.
    waited=0
    while [[ "$waited" -lt "$GRACE_SECONDS" ]]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            log "exited gracefully: pid=$pid after ${waited}s"
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        if kill -KILL "$pid" 2>/dev/null; then
            bump_counter sigkill_count
            log "SIGKILL sent: pid=$pid (survived ${GRACE_SECONDS}s grace)"
        else
            log "SIGKILL failed: pid=$pid"
        fi
    fi
done

log "scan complete: mode=$MODE killed=${#stale_pids[@]}"
exit 0
