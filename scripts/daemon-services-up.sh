#!/bin/bash
# =============================================================================
# daemon-services-up.sh — R1 startup harness for 6 mf services (v3 sprint)
# =============================================================================
#
# Brings the currently-down core daemons to life with health-check ordering.
#
# Per CLAUDE.md: webhook = #1 priority. Per gains-gate: 6 services CRITICAL.
# Order matters — dependencies first, leaves last.
#
# Order:
#   1. fleet daemon         (verify-only via /health)
#   2. Synaptic doc index   :8888 /health
#   3. Scheduler            (PID via pgrep scheduler_coordinator)
#   4. Webhook agent_service:8080 /health
#   5. MLX server           :5044 /v1/models  (SKIP on Intel)
#
# Modes:
#   --dry-run                preview probes + exact start commands, no side effects
#   --apply                  start missing services, prompt before each start
#   --apply --no-prompt      start missing services, no prompts (autonomous)
#
# Idempotent: services already up are reported OK and skipped.
# Logs:        /tmp/r1-daemon-services-up.log (overwritten each run)
# Exit codes:  0 = all OK or SKIP, 1 = any FAIL
#
# Per-spec invariants:
#   * ZERO SILENT FAILURES — every start attempt logs to the run log
#   * Aaron consent rule    — print exact command, prompt unless --no-prompt
#   * P1 self-heartbeat fix (873c25ea) — fleet liveness via /health, not fleet-state.json
#   * MLX skipped on non-arm64 (Cycle 10 finding: would FAIL on Intel anyway)
# =============================================================================

set -u  # NOT -e — we handle each step's failure explicitly so summary is complete

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="/tmp/daemon-services-up.log"
ARCH="$(uname -m)"

MODE="dry-run"
NO_PROMPT=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|--check-only) MODE="dry-run" ;;
        --apply)     MODE="apply" ;;
        --no-prompt) NO_PROMPT=1 ;;
        -h|--help)
            sed -n '3,30p' "$0"
            exit 0
            ;;
        *)
            echo "[r1] unknown arg: $arg" >&2
            echo "     usage: $0 [--check-only | --dry-run | --apply [--no-prompt]]" >&2
            exit 2
            ;;
    esac
done

# Fresh log every run (per spec: overwrite)
: > "$LOG_FILE"

log() {
    # Timestamped log line — also echoed to stdout
    local msg="[$(date '+%H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

log_only() {
    # Log without printing to stdout (for verbose probe details)
    echo "[$(date '+%H:%M:%S')] $*" >> "$LOG_FILE"
}

VENV_REBUILD_SCRIPT="$REPO_ROOT/scripts/venv-rebuild.sh"

# Track per-service results for final summary
SUMMARY_NAMES=()
SUMMARY_STATUS=()
SUMMARY_REASON=()

record() {
    SUMMARY_NAMES+=("$1")
    SUMMARY_STATUS+=("$2")
    SUMMARY_REASON+=("$3")
}

confirm_start() {
    # $1 = service name, $2 = exact command
    local name="$1" cmd="$2"
    echo "[r1] About to start: $name"
    echo "[r1] Exact command : $cmd"
    if [ "$NO_PROMPT" -eq 1 ]; then
        log "consent skipped (--no-prompt) for $name"
        return 0
    fi
    printf "[r1] Proceed? [y/N] "
    local ans
    read -r ans
    case "$ans" in
        y|Y|yes|YES) log "Aaron consent: YES for $name"; return 0 ;;
        *)           log "Aaron consent: NO for $name (skipped)"; return 1 ;;
    esac
}

# -----------------------------------------------------------------------------
# Probe primitives
# -----------------------------------------------------------------------------

probe_http() {
    # $1 = url   →  exit 0 if 2xx within 2s
    local url="$1"
    curl -sf --max-time 2 "$url" >/dev/null 2>&1
}

probe_pgrep() {
    # $1 = pattern
    pgrep -f "$1" >/dev/null 2>&1
}

# Wait for an HTTP probe to succeed for up to $1 seconds.
wait_http_up() {
    local url="$1" timeout="${2:-60}" elapsed=0
    while [ "$elapsed" -lt "$timeout" ]; do
        if probe_http "$url"; then
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    return 1
}

wait_pgrep_up() {
    local pattern="$1" timeout="${2:-60}" elapsed=0
    while [ "$elapsed" -lt "$timeout" ]; do
        if probe_pgrep "$pattern"; then
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    return 1
}

# -----------------------------------------------------------------------------
# Pre-flight: venv health (M1 — calls venv-rebuild.sh --check, never mutates)
# -----------------------------------------------------------------------------

step_venv_preflight() {
    local name="venv_preflight"
    log "[pre] $name — probe: $VENV_REBUILD_SCRIPT --check"
    if [ ! -x "$VENV_REBUILD_SCRIPT" ]; then
        log "      SKIP (venv-rebuild.sh not present yet — M1 not deployed)"
        record "$name" "SKIP" "venv-rebuild.sh not installed"
        return
    fi
    if "$VENV_REBUILD_SCRIPT" --check >>"$LOG_FILE" 2>&1; then
        log "      OK (venv healthy — all essential packages importable)"
        record "$name" "OK" "all essential packages importable"
    else
        log "      WARN — venv missing packages. Run: bash $VENV_REBUILD_SCRIPT --apply"
        log "      (continuing; daemon starts may fail if uvicorn/nats/redis absent)"
        record "$name" "SKIP" "venv needs rebuild (see $VENV_REBUILD_SCRIPT)"
    fi
}

# -----------------------------------------------------------------------------
# Service handlers — each returns nothing, calls record() with OK/SKIP/FAIL
# -----------------------------------------------------------------------------

step_nats_server() {
    local name="nats_server_4222"
    local uid; uid=$(id -u)
    log "[0/7] $name — probe: nc -z 127.0.0.1 4222"
    if nc -z 127.0.0.1 4222 2>/dev/null; then
        log "      OK (already up on :4222)"
        record "$name" "OK" "already up :4222"
        return
    fi

    local cmd="launchctl kickstart gui/$uid/io.contextdna.nats-server"

    if [ "$MODE" = "dry-run" ]; then
        log "      DRY-RUN — NATS server down. Would run: $cmd"
        record "$name" "SKIP" "dry-run; would start"
        return
    fi

    if ! confirm_start "$name" "$cmd"; then
        record "$name" "SKIP" "user declined consent"
        return
    fi

    log "      starting via launchctl…"
    local plist="$HOME/Library/LaunchAgents/io.contextdna.nats-server.plist"
    if [[ -f "$plist" ]]; then
        launchctl bootstrap "gui/$uid" "$plist" >>"$LOG_FILE" 2>&1 || true
    fi
    launchctl kickstart "gui/$uid/io.contextdna.nats-server" >>"$LOG_FILE" 2>&1 || true
    sleep 3

    if nc -z 127.0.0.1 4222 2>/dev/null; then
        log "      OK (now up on :4222)"
        record "$name" "OK" "started"
    else
        log "      FAIL — port 4222 still closed after start attempt"
        record "$name" "FAIL" "port 4222 still closed"
    fi
}

step_fleet_daemon() {
    local name="fleet_daemon" url="http://127.0.0.1:8855/health"
    log "[1/7] $name — probe: curl -sf $url"
    if probe_http "$url"; then
        log "      OK (already up — using /health, not fleet-state.json per P1 self-heartbeat)"
        record "$name" "OK" "already up via /health"
        return
    fi
    # Try launchd start
    local cmd="launchctl kickstart gui/$(id -u)/io.contextdna.fleet-nats"
    if [ "$MODE" = "dry-run" ]; then
        log "      DRY-RUN — fleet-nats down. Would run: $cmd"
        record "$name" "SKIP" "dry-run; would start"
        return
    fi

    if ! confirm_start "$name" "$cmd"; then
        record "$name" "SKIP" "user declined consent"
        return
    fi

    local uid; uid=$(id -u)
    local plist="$HOME/Library/LaunchAgents/io.contextdna.fleet-nats.plist"
    [[ -f "$plist" ]] && launchctl bootstrap "gui/$uid" "$plist" >>"$LOG_FILE" 2>&1 || true
    launchctl kickstart "gui/$uid/io.contextdna.fleet-nats" >>"$LOG_FILE" 2>&1 || true
    sleep 4

    if probe_http "$url"; then
        log "      OK (now responding on :8855)"
        record "$name" "OK" "started + healthy"
    else
        log "      FAIL — /health still unreachable after start"
        record "$name" "FAIL" "/health unreachable after start attempt"
    fi
}

step_redis() {
    local name="redis_6379"
    log "[2/7] $name — probe: nc -z 127.0.0.1 6379"
    if nc -z 127.0.0.1 6379 2>/dev/null; then
        log "      OK (already up on :6379)"
        record "$name" "OK" "already up :6379"
        return
    fi

    local cmd="brew services start redis"

    if [ "$MODE" = "dry-run" ]; then
        log "      DRY-RUN — Redis down. Would run: $cmd"
        record "$name" "SKIP" "dry-run; would start"
        return
    fi

    if ! confirm_start "$name" "$cmd"; then
        record "$name" "SKIP" "user declined consent"
        return
    fi

    log "      starting Redis via brew services…"
    if command -v brew >/dev/null 2>&1; then
        brew services start redis >>"$LOG_FILE" 2>&1 || true
        sleep 3
    else
        log "      WARN — brew not found; trying redis-server directly"
        nohup redis-server --daemonize yes >>"$LOG_FILE" 2>&1 || true
        sleep 2
    fi

    if nc -z 127.0.0.1 6379 2>/dev/null; then
        log "      OK (now up on :6379)"
        record "$name" "OK" "started"
    else
        log "      FAIL — port 6379 still closed after start attempt"
        record "$name" "FAIL" "port 6379 still closed"
    fi
}

step_synaptic() {
    local name="synaptic_8888" url="http://127.0.0.1:8888/health"
    log "[3/7] $name — probe: curl -sf $url"
    if probe_http "$url"; then
        log "      OK (already up)"
        record "$name" "OK" "already up :8888"
        return
    fi

    # Start command: invoke context-dna-start canonical entrypoint with mode that
    # includes synaptic_chat_server. We use direct uvicorn so step 4 (agent_service)
    # remains a distinct, separable step (context-dna-start voice would co-start
    # both, breaking the per-service ordering contract of this harness).
    local cmd="cd $REPO_ROOT && nohup .venv/bin/python -m uvicorn memory.synaptic_chat_server:app --host 0.0.0.0 --port 8888 >> logs/synaptic_chat_server.log 2>&1 &"

    if [ "$MODE" = "dry-run" ]; then
        log "      DRY-RUN would run: $cmd"
        log "      DRY-RUN would poll: $url (up to 60s)"
        record "$name" "SKIP" "dry-run; would start"
        return
    fi

    if ! confirm_start "$name" "$cmd"; then
        record "$name" "SKIP" "user declined consent"
        return
    fi

    mkdir -p "$REPO_ROOT/logs"
    log "      starting…"
    ( cd "$REPO_ROOT" && nohup .venv/bin/python -m uvicorn memory.synaptic_chat_server:app \
        --host 0.0.0.0 --port 8888 >> logs/synaptic_chat_server.log 2>&1 & ) \
        || { log "      FAIL — launch returned non-zero"; record "$name" "FAIL" "launch error"; return; }

    log "      polling $url for up to 60s…"
    if wait_http_up "$url" 60; then
        log "      OK (now responding on :8888)"
        record "$name" "OK" "started + healthy"
    else
        log "      FAIL — no /health response within 60s (see logs/synaptic_chat_server.log)"
        record "$name" "FAIL" "no /health within 60s"
    fi
}

step_scheduler() {
    local name="scheduler" pattern="scheduler_coordinator"
    log "[4/7] $name — probe: pgrep -f $pattern"
    if probe_pgrep "$pattern"; then
        log "      OK (already running, PID $(pgrep -f $pattern | head -1))"
        record "$name" "OK" "already running"
        return
    fi

    local cmd="$REPO_ROOT/scripts/atlas-ops.sh scheduler start"

    if [ "$MODE" = "dry-run" ]; then
        log "      DRY-RUN would run: $cmd"
        log "      DRY-RUN would poll: pgrep -f $pattern (up to 60s)"
        record "$name" "SKIP" "dry-run; would start"
        return
    fi

    if ! confirm_start "$name" "$cmd"; then
        record "$name" "SKIP" "user declined consent"
        return
    fi

    log "      starting…"
    ( "$REPO_ROOT/scripts/atlas-ops.sh" scheduler start >> "$LOG_FILE" 2>&1 ) \
        || log_only "atlas-ops.sh scheduler start exited non-zero (continuing to poll)"

    log "      polling pgrep -f $pattern for up to 60s…"
    if wait_pgrep_up "$pattern" 60; then
        log "      OK (PID $(pgrep -f $pattern | head -1))"
        record "$name" "OK" "started, PID found"
    else
        log "      FAIL — no scheduler PID within 60s (see /tmp/scheduler_coordinator.log)"
        record "$name" "FAIL" "no PID within 60s"
    fi
}

step_webhook() {
    local name="webhook_agent_8080" url="http://127.0.0.1:8080/health"
    log "[5/7] $name (#1 priority per CLAUDE.md) — probe: curl -sf $url"
    if probe_http "$url"; then
        log "      OK (already up)"
        record "$name" "OK" "already up :8080"
        return
    fi

    local cmd="bash $REPO_ROOT/scripts/start-helper-agent.sh"

    if [ "$MODE" = "dry-run" ]; then
        log "      DRY-RUN would run: $cmd"
        log "      DRY-RUN would poll: $url (up to 60s)"
        record "$name" "SKIP" "dry-run; would start"
        return
    fi

    if ! confirm_start "$name" "$cmd"; then
        record "$name" "SKIP" "user declined consent"
        return
    fi

    mkdir -p "$REPO_ROOT/logs"
    local plist="$HOME/Library/LaunchAgents/io.contextdna.agent-service.plist"
    local uid; uid=$(id -u)
    if [[ -f "$plist" ]]; then
        log "      starting via launchctl (plist found)…"
        # Bootstrap if not yet known to launchd, then kickstart
        launchctl bootstrap "gui/$uid" "$plist" >>"$LOG_FILE" 2>&1 || true
        launchctl kickstart "gui/$uid/io.contextdna.agent-service" >>"$LOG_FILE" 2>&1 || true
    else
        log "      starting in background (no plist, falling back to nohup)…"
        # start-helper-agent.sh runs uvicorn in foreground (exec); we background it.
        ( nohup bash "$REPO_ROOT/scripts/start-helper-agent.sh" >> "$REPO_ROOT/logs/agent_service.log" 2>&1 & ) \
            || { log "      FAIL — launch returned non-zero"; record "$name" "FAIL" "launch error"; return; }
    fi

    log "      polling $url for up to 60s…"
    if wait_http_up "$url" 60; then
        log "      OK (now responding on :8080)"
        record "$name" "OK" "started + healthy"
    else
        log "      FAIL — no /health response within 60s (see logs/agent_service.log)"
        record "$name" "FAIL" "no /health within 60s"
    fi
}

step_mlx() {
    local name="mlx_5044" url="http://127.0.0.1:5044/v1/models"
    log "[6/7] $name — probe: curl -sf $url"

    # ARCH gate: MLX requires Apple Silicon. mac2 is x86_64 (Intel).
    if [ "$ARCH" != "arm64" ]; then
        log "      SKIP — ARCH=$ARCH (MLX requires arm64 / Apple Silicon)"
        log "      Note: run on mac3 to start MLX. mac2 is Intel x86_64."
        record "$name" "SKIP" "ARCH=$ARCH not arm64"
        return
    fi

    if probe_http "$url"; then
        log "      OK (already up)"
        record "$name" "OK" "already up :5044"
        return
    fi

    local cmd="bash $REPO_ROOT/scripts/start-llm.sh"

    if [ "$MODE" = "dry-run" ]; then
        log "      DRY-RUN would run: $cmd"
        log "      DRY-RUN would poll: $url (up to 60s)"
        record "$name" "SKIP" "dry-run; would start"
        return
    fi

    if ! confirm_start "$name" "$cmd"; then
        record "$name" "SKIP" "user declined consent"
        return
    fi

    log "      starting in background…"
    ( nohup bash "$REPO_ROOT/scripts/start-llm.sh" >> "$REPO_ROOT/logs/mlx_server.log" 2>&1 & ) \
        || { log "      FAIL — launch returned non-zero"; record "$name" "FAIL" "launch error"; return; }

    log "      polling $url for up to 60s…"
    if wait_http_up "$url" 60; then
        log "      OK (now responding on :5044)"
        record "$name" "OK" "started + healthy"
    else
        log "      FAIL — no /v1/models response within 60s (see logs/mlx_server.log)"
        record "$name" "FAIL" "no /v1/models within 60s"
    fi
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

log "==========================================================================="
log "daemon-services-up.sh (v2) — mode=$MODE  no_prompt=$NO_PROMPT  arch=$ARCH"
log "Repo: $REPO_ROOT"
log "Log:  $LOG_FILE"
log "==========================================================================="

step_venv_preflight
step_nats_server
step_redis
step_fleet_daemon
step_synaptic
step_scheduler
step_webhook
step_mlx

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------

log "---------------------------------------------------------------------------"
log "Final summary:"

failed=0
i=0
while [ $i -lt ${#SUMMARY_NAMES[@]} ]; do
    name="${SUMMARY_NAMES[$i]}"
    status="${SUMMARY_STATUS[$i]}"
    reason="${SUMMARY_REASON[$i]}"
    # Pad name to 22 chars using printf
    line="$(printf '  %-22s %-4s  %s' "$name" "$status" "$reason")"
    echo "$line"
    echo "$line" >> "$LOG_FILE"
    if [ "$status" = "FAIL" ]; then
        failed=$((failed + 1))
    fi
    i=$((i + 1))
done

log "---------------------------------------------------------------------------"
if [ $failed -eq 0 ]; then
    log "Result: 0 failures (all OK or SKIP)"
    exit 0
else
    log "Result: $failed FAIL — see $LOG_FILE for details"
    exit 1
fi
