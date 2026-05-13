#!/bin/bash
# =============================================================================
# Butler Watchdog — Unified Service Health Monitor
# =============================================================================
# Replaces the stale vllm-watchdog.sh. Probes ALL 5 core services every 60s.
# Pure bash — no Python dependency (works even if .venv is broken).
#
# Services monitored:
#   1. LLM (mlx_lm.server on port 5044)
#   2. Redis (port 6379)
#   3. PostgreSQL (port 5432)
#   4. Agent Service (port 8080)
#   5. Scheduler (lite_scheduler.py)
#   6. NATS server (port 4222)
#   7. Fleet daemon (fleet_nerve_nats.py)
#   8. Discord bridge (multifleet.discord_bridge)
#   9. LLM priority proxy (port 5045)
#
# Recovery actions:
#   - LLM: restart via launchctl (com.contextdna.llm)
#   - Scheduler: restart via PYTHONPATH=. .venv/bin/python3 memory/lite_scheduler.py
#   - Redis/PG: Docker — restart container
#   - Agent Service: restart via launchctl (com.contextdna.unified)
#   - NATS: restart via launchctl (io.contextdna.nats-server)
#   - Fleet daemon: restart via launchctl (io.contextdna.fleet-nats)
#   - Discord bridge: restart via launchctl (com.contextdna.discord-bridge)
#   - LLM proxy: restart via launchctl (io.contextdna.llm-proxy)
#
# Installation (script MUST be outside ~/Documents/ for launchd TCC):
#   mkdir -p ~/.local/bin
#   cp scripts/butler-watchdog.sh ~/.local/bin/butler-watchdog.sh
#   chmod +x ~/.local/bin/butler-watchdog.sh
#   cp scripts/launchd/com.contextdna.butler-watchdog.plist ~/Library/LaunchAgents/
#   launchctl unload ~/Library/LaunchAgents/com.contextdna.butler-watchdog.plist 2>/dev/null
#   launchctl load ~/Library/LaunchAgents/com.contextdna.butler-watchdog.plist
# =============================================================================

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG_FILE="/tmp/butler-watchdog.log"
# State file in /tmp (not Documents/) so launchd can write without Full Disk Access
STATE_FILE="/tmp/butler-watchdog-state.json"
MAX_LOG_LINES=500

# Trim log to prevent unbounded growth
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt "$MAX_LOG_LINES" ]; then
    tail -n "$MAX_LOG_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

notify() {
    local title="$1"
    local msg="$2"
    osascript -e "display notification \"$msg\" with title \"$title\" sound name \"Glass\"" 2>/dev/null || true
}

# =============================================================================
# SERVICE CHECKS (fast — each times out in 3s max)
# =============================================================================

check_llm() {
    curl -s --max-time 3 http://127.0.0.1:5044/v1/models > /dev/null 2>&1
}

check_redis() {
    # nc -z is macOS-native port check (no timeout command needed)
    nc -z -w2 127.0.0.1 6379 2>/dev/null
}

check_postgres() {
    nc -z -w2 127.0.0.1 5432 2>/dev/null
}

check_agent_service() {
    curl -s --max-time 3 http://127.0.0.1:8080/health > /dev/null 2>&1
}

check_scheduler() {
    # scheduler_coordinator.py spawns lite_scheduler — check for either
    pgrep -f "scheduler_coordinator|lite_scheduler" > /dev/null 2>&1
}

check_nats() {
    # NATS monitoring endpoint
    curl -s --max-time 3 http://127.0.0.1:8222/varz > /dev/null 2>&1
}

check_fleet_daemon() {
    pgrep -f "fleet_nerve_nats.py.*serve" > /dev/null 2>&1
}

check_discord_bridge() {
    pgrep -f "multifleet.discord_bridge" > /dev/null 2>&1
}

check_llm_proxy() {
    curl -s --max-time 3 http://127.0.0.1:5045/health > /dev/null 2>&1 ||
    pgrep -f "llm_priority_proxy" > /dev/null 2>&1
}

# =============================================================================
# RECOVERY ACTIONS
# =============================================================================

restart_llm() {
    log "RESTART: LLM via launchctl"
    launchctl kickstart -k "gui/$(id -u)/com.contextdna.llm" 2>/dev/null ||
    (launchctl unload ~/Library/LaunchAgents/com.contextdna.llm.plist 2>/dev/null;
     sleep 2;
     launchctl load ~/Library/LaunchAgents/com.contextdna.llm.plist 2>/dev/null)
    sleep 15  # Model load takes ~10-15s
}

restart_redis() {
    log "RESTART: Redis containers"
    docker restart context-dna-redis contextdna-redis 2>/dev/null || true
    sleep 3
}

restart_postgres() {
    log "RESTART: PostgreSQL container"
    docker restart context-dna-postgres 2>/dev/null || true
    sleep 3
}

restart_agent_service() {
    log "RESTART: Agent Service via launchctl"
    launchctl kickstart -k "gui/$(id -u)/com.contextdna.unified" 2>/dev/null ||
    (launchctl unload ~/Library/LaunchAgents/com.contextdna.unified.plist 2>/dev/null;
     sleep 2;
     launchctl load ~/Library/LaunchAgents/com.contextdna.unified.plist 2>/dev/null)
    sleep 5
}

restart_scheduler() {
    log "RESTART: Scheduler via launchctl"
    # Kill existing if running, then kickstart fresh (KeepAlive=false, so no -k)
    pkill -f "scheduler_coordinator|lite_scheduler" 2>/dev/null || true
    sleep 1
    launchctl kickstart "gui/$(id -u)/com.contextdna.scheduler" 2>/dev/null || {
        # Fallback: unload + load
        launchctl unload ~/Library/LaunchAgents/com.contextdna.scheduler.plist 2>/dev/null
        sleep 1
        launchctl load ~/Library/LaunchAgents/com.contextdna.scheduler.plist 2>/dev/null
    }
    sleep 3
}

restart_nats() {
    log "RESTART: NATS server via launchctl"
    launchctl kickstart -k "gui/$(id -u)/io.contextdna.nats-server" 2>/dev/null ||
    (launchctl unload ~/Library/LaunchAgents/io.contextdna.nats-server.plist 2>/dev/null;
     sleep 2;
     launchctl load ~/Library/LaunchAgents/io.contextdna.nats-server.plist 2>/dev/null)
    sleep 3
}

restart_fleet_daemon() {
    log "RESTART: Fleet daemon via launchctl"
    launchctl kickstart -k "gui/$(id -u)/io.contextdna.fleet-nats" 2>/dev/null ||
    (launchctl unload ~/Library/LaunchAgents/io.contextdna.fleet-nats.plist 2>/dev/null;
     sleep 2;
     launchctl load ~/Library/LaunchAgents/io.contextdna.fleet-nats.plist 2>/dev/null)
    sleep 3
}

restart_discord_bridge() {
    log "RESTART: Discord bridge via launchctl"
    launchctl kickstart -k "gui/$(id -u)/com.contextdna.discord-bridge" 2>/dev/null ||
    (launchctl unload ~/Library/LaunchAgents/com.contextdna.discord-bridge.plist 2>/dev/null;
     sleep 2;
     launchctl load ~/Library/LaunchAgents/com.contextdna.discord-bridge.plist 2>/dev/null)
    sleep 3
}

restart_llm_proxy() {
    log "RESTART: LLM priority proxy via launchctl"
    launchctl kickstart -k "gui/$(id -u)/io.contextdna.llm-proxy" 2>/dev/null ||
    (launchctl unload ~/Library/LaunchAgents/io.contextdna.llm-proxy.plist 2>/dev/null;
     sleep 2;
     launchctl load ~/Library/LaunchAgents/io.contextdna.llm-proxy.plist 2>/dev/null)
    sleep 3
}

# =============================================================================
# MAIN WATCHDOG LOOP
# =============================================================================

main() {
    local all_ok=true
    local status_parts=()
    local downs=()
    local restarts=0

    # Check each service
    if check_llm; then
        status_parts+=("LLM:UP")
    else
        status_parts+=("LLM:DOWN")
        downs+=("LLM")
        all_ok=false
        restart_llm
        restarts=$((restarts + 1))
        if check_llm; then
            log "RECOVERED: LLM"
            notify "Butler Watchdog" "LLM auto-recovered"
        else
            log "FAILED: LLM restart failed"
            notify "Butler Watchdog" "LLM restart FAILED — manual check needed"
        fi
    fi

    if check_redis; then
        status_parts+=("Redis:UP")
    else
        status_parts+=("Redis:DOWN")
        downs+=("Redis")
        all_ok=false
        restart_redis
        restarts=$((restarts + 1))
    fi

    if check_postgres; then
        status_parts+=("PG:UP")
    else
        status_parts+=("PG:DOWN")
        downs+=("PG")
        all_ok=false
        restart_postgres
        restarts=$((restarts + 1))
    fi

    if check_agent_service; then
        status_parts+=("Agent:UP")
    else
        status_parts+=("Agent:DOWN")
        downs+=("Agent")
        all_ok=false
        # NOTE: Do NOT restart agent_service here — launchd KeepAlive handles it.
        # Triple-restart race (launchd + ecosystem_health + watchdog) caused exit 78.
        log "SKIP: Agent restart deferred to launchd KeepAlive (avoids race condition)"
    fi

    if check_scheduler; then
        status_parts+=("Sched:UP")
    else
        status_parts+=("Sched:DOWN")
        downs+=("Scheduler")
        all_ok=false
        restart_scheduler
        restarts=$((restarts + 1))
    fi

    # ── Fleet infrastructure services ──

    if check_nats; then
        status_parts+=("NATS:UP")
    else
        status_parts+=("NATS:DOWN")
        downs+=("NATS")
        all_ok=false
        restart_nats
        restarts=$((restarts + 1))
        if check_nats; then
            log "RECOVERED: NATS"
            notify "Butler Watchdog" "NATS server auto-recovered"
        else
            log "FAILED: NATS restart failed"
        fi
    fi

    if check_fleet_daemon; then
        status_parts+=("Fleet:UP")
    else
        status_parts+=("Fleet:DOWN")
        downs+=("Fleet")
        all_ok=false
        # Fleet daemon depends on NATS — only restart if NATS is up
        if check_nats; then
            restart_fleet_daemon
            restarts=$((restarts + 1))
            if check_fleet_daemon; then
                log "RECOVERED: Fleet daemon"
                notify "Butler Watchdog" "Fleet daemon auto-recovered"
            else
                log "FAILED: Fleet daemon restart failed"
            fi
        else
            log "SKIP: Fleet daemon restart deferred — NATS not ready"
        fi
    fi

    if check_discord_bridge; then
        status_parts+=("Discord:UP")
    else
        status_parts+=("Discord:DOWN")
        downs+=("Discord")
        all_ok=false
        restart_discord_bridge
        restarts=$((restarts + 1))
        if check_discord_bridge; then
            log "RECOVERED: Discord bridge"
            notify "Butler Watchdog" "Discord bridge auto-recovered"
        else
            log "FAILED: Discord bridge restart failed"
        fi
    fi

    if check_llm_proxy; then
        status_parts+=("Proxy:UP")
    else
        status_parts+=("Proxy:DOWN")
        downs+=("LLMProxy")
        all_ok=false
        restart_llm_proxy
        restarts=$((restarts + 1))
    fi

    # Write state file (for programmatic access)
    local now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local status_str=$(IFS=', '; echo "${status_parts[*]}")

    cat > "$STATE_FILE" <<EOF
{
    "timestamp": "$now",
    "all_ok": $all_ok,
    "services": "$status_str",
    "restarts": $restarts,
    "downs": "$(IFS=', '; echo "${downs[*]}")"
}
EOF

    if $all_ok; then
        # Only log healthy every 10th check (~10 min) to reduce noise
        local check_count=0
        if [ -f "$STATE_FILE.count" ]; then
            check_count=$(cat "$STATE_FILE.count" 2>/dev/null || echo 0)
        fi
        check_count=$((check_count + 1))
        echo "$check_count" > "$STATE_FILE.count"
        if [ "$((check_count % 10))" -eq 0 ]; then
            log "OK: $status_str"
        fi
    else
        log "ALERT: $status_str (restarted $restarts services)"
    fi
}

main
