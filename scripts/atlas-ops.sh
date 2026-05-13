#!/bin/bash
# =============================================================================
# Atlas Operations — unified CLI for all manual system operations
# =============================================================================
# Usage: ./scripts/atlas-ops.sh <service> <action> [args...]
#
# Services:
#   scheduler      start|status|stop|log
#   synaptic       restart|speak|health|log
#   inject         test|cache|refresh|quality|strategic
#   llm            health|stats|restart|status|gpu-lock
#   surgery        check|probe|ask-local|ask-remote|consult|cross-exam|consensus|status
#   neurologist    pulse|challenge "topic"
#   ab             status|propose|collaborate|start|measure|conclude|validate|veto|queue
#   sentinel       run|status
#   corrigibility  gate [phase]|review "topic"
#   recover        hook|mcp|all
#   criticals      check|ack|all
#   session        rehydrate|extract|extract-fast|stats|search
#   cardio         status|run|manual|dry-run
#   gains          check|cardio
#   evidence       "query"
#   professor      "task description"
#   redis          keys|info|get
#   docker         status|logs|restart-api|stop-api
#   status         (full system status)
# =============================================================================

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python3"
REDIS="docker exec context-dna-redis redis-cli"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
info() { echo -e "  ${CYAN}→${NC} $1"; }
header() { echo -e "\n${BOLD}$1${NC}"; }

SERVICE="${1:-help}"
ACTION="${2:-}"
shift 2 2>/dev/null
ARGS="$@"

# =============================================================================
# V12 ENFORCEMENT — Action Registry Pre-Flight Check
# =============================================================================
# Logs every dispatch to Redis telemetry. Emits deprecation warning for
# unregistered SERVICE+ACTION combos. Non-blocking (warn only, never gate).
#
# Phase 3 of V12 Action Fragmentation remediation.
# =============================================================================

REGISTRY_CACHE="$REPO_ROOT/scripts/.action-registry-cache.json"

registry_preflight() {
    local svc="$1"
    local act="$2"

    # Skip for meta-commands
    [[ "$svc" == "help" || "$svc" == "-h" || "$svc" == "--help" || "$svc" == "status" ]] && return 0

    # Log invocation to Redis (fire-and-forget, never block)
    PYTHONPATH="$REPO_ROOT" "$PYTHON" -c "
import redis, json, time
try:
    r = redis.Redis(decode_responses=True, socket_timeout=1)
    key = 'contextdna:atlas_ops:invocations'
    r.zadd(key, {json.dumps({'svc': '$svc', 'act': '$act', 'ts': time.time()}): time.time()})
    r.zremrangebyrank(key, 0, -501)
except Exception:
    pass
" 2>/dev/null &

    # Rebuild cache if missing or >1hr old (npx tsx is slow, ~1.8s)
    if [[ ! -f "$REGISTRY_CACHE" ]] || [[ $(( $(date +%s) - $(stat -f%m "$REGISTRY_CACHE" 2>/dev/null || echo 0) )) -gt 3600 ]]; then
        cd "$REPO_ROOT" && npx --silent tsx context-dna/engine/actions/action-registry.ts --json list > "$REGISTRY_CACHE" 2>/dev/null || true
    fi

    # Fast lookup from cached JSON (<50ms vs ~1.8s)
    [[ ! -f "$REGISTRY_CACHE" ]] && return 0

    local REGISTRY_HIT
    REGISTRY_HIT=$("$PYTHON" -c "
import sys, json
try:
    with open('$REGISTRY_CACHE') as f:
        actions = json.load(f)
    svc, act = '$svc', '$act'
    for a in actions:
        cli = a.get('cli', {})
        args = cli.get('requiredArgs', [])
        if a.get('path','') == 'scripts/atlas-ops.sh' and len(args) >= 2 and args[0] == svc and args[1] == act:
            print(a['id']); sys.exit(0)
        if a.get('path','') == 'scripts/atlas-ops.sh' and len(args) == 1 and args[0] == svc:
            print(a['id']); sys.exit(0)
    print('UNREGISTERED')
except Exception:
    print('SKIP')
" 2>/dev/null)

    if [[ "$REGISTRY_HIT" == "UNREGISTERED" ]]; then
        echo -e "  ${YELLOW}⚠ V12${NC} atlas-ops.sh ${svc} ${act} — not in action registry (run: ./scripts/action-registry.sh find <id>)" >&2
    fi
}

# Run pre-flight (never blocks dispatch)
registry_preflight "$SERVICE" "$ACTION" || true

# =============================================================================
# SCHEDULER
# =============================================================================
scheduler_cmd() {
    case "$ACTION" in
        start)
            if pgrep -f "scheduler_coordinator" > /dev/null 2>&1; then
                warn "Scheduler already running (PID $(pgrep -f scheduler_coordinator | head -1))"
                return
            fi
            header "Starting scheduler..."
            cd "$REPO_ROOT"
            PYTHONPATH=. nohup "$PYTHON" memory/scheduler_coordinator.py > /tmp/scheduler_coordinator.log 2>&1 &
            sleep 2
            if pgrep -f "scheduler_coordinator" > /dev/null 2>&1; then
                ok "Scheduler started (PID $(pgrep -f scheduler_coordinator | head -1))"
            else
                fail "Scheduler failed to start"
                echo "  Check: tail -20 /tmp/scheduler_coordinator.log"
            fi
            ;;
        status)
            header "Scheduler Status"
            PID=$(pgrep -f "scheduler_coordinator" 2>/dev/null | head -1)
            if [ -n "$PID" ]; then
                ok "Running (PID $PID)"
                # Uptime
                PS_START=$(ps -o lstart= -p "$PID" 2>/dev/null)
                [ -n "$PS_START" ] && info "Started: $PS_START"
            else
                fail "Not running"
            fi
            # Last log lines
            if [ -f /tmp/scheduler_coordinator.log ]; then
                info "Last 5 log lines:"
                tail -5 /tmp/scheduler_coordinator.log | sed 's/^/    /'
            fi
            ;;
        stop)
            PID=$(pgrep -f "scheduler_coordinator" 2>/dev/null | head -1)
            if [ -n "$PID" ]; then
                header "Stopping scheduler (PID $PID)..."
                kill "$PID" 2>/dev/null
                sleep 1
                if pgrep -f "scheduler_coordinator" > /dev/null 2>&1; then
                    kill -9 "$PID" 2>/dev/null
                fi
                ok "Scheduler stopped"
            else
                warn "Scheduler not running"
            fi
            ;;
        log)
            N="${1:-30}"
            tail -"$N" /tmp/scheduler_coordinator.log 2>/dev/null || fail "No scheduler log"
            ;;
        *)
            echo "Usage: atlas-ops.sh scheduler [start|status|stop|log [n]]"
            ;;
    esac
}

# =============================================================================
# SYNAPTIC
# =============================================================================
synaptic_cmd() {
    case "$ACTION" in
        restart)
            header "Restarting Synaptic chat server..."
            PID=$(pgrep -f "synaptic_chat_server" 2>/dev/null | head -1)
            if [ -n "$PID" ]; then
                info "Killing existing (PID $PID)"
                kill "$PID" 2>/dev/null
                sleep 2
            fi
            cd "$REPO_ROOT"
            PYTHONPATH=. nohup "$PYTHON" memory/synaptic_chat_server.py > /tmp/synaptic_chat_server.log 2>&1 &
            sleep 3
            if pgrep -f "synaptic_chat_server" > /dev/null 2>&1; then
                ok "Synaptic restarted (PID $(pgrep -f synaptic_chat_server | head -1))"
            else
                fail "Synaptic failed to start"
                echo "  Check: tail -20 /tmp/synaptic_chat_server.log"
            fi
            ;;
        speak)
            MSG="${1:-what is your current state}"
            WORD_COUNT=$(echo "$MSG" | wc -w | tr -d ' ')
            if [ "$WORD_COUNT" -lt 6 ]; then
                fail "Message too short ($WORD_COUNT words). Need 6+ words to trigger LLM."
                info "Webhook ignores <5 word prompts. Be descriptive."
                info "Example: atlas-ops.sh synaptic speak \"what is your current state and focus\""
                return 1
            fi
            header "Speaking to Synaptic..."
            info "Message: $MSG ($WORD_COUNT words)"
            START=$("$PYTHON" -c "import time; print(time.time())")
            RESPONSE=$(curl -s -X POST "http://localhost:8888/speak-direct" \
                -H "Content-Type: application/json" \
                -d "{\"message\": \"$MSG\"}" \
                --max-time 60 2>/dev/null)
            END=$("$PYTHON" -c "import time; print(time.time())")
            ELAPSED=$("$PYTHON" -c "print(f'{$END - $START:.1f}')")
            if [ -n "$RESPONSE" ]; then
                ok "Response received (${ELAPSED}s)"
                echo "$RESPONSE" | "$PYTHON" -m json.tool 2>/dev/null || echo "$RESPONSE"
            else
                fail "No response (${ELAPSED}s)"
            fi
            ;;
        health)
            header "Synaptic Health"
            RESP=$(curl -s --max-time 3 "http://localhost:8888/health" 2>/dev/null)
            if [ -n "$RESP" ]; then
                ok "Synaptic responding on :8888"
                echo "$RESP" | "$PYTHON" -m json.tool 2>/dev/null || echo "$RESP"
            else
                fail "Synaptic not responding on :8888"
            fi
            ;;
        log)
            N="${1:-30}"
            tail -"$N" /tmp/synaptic_chat_server.log 2>/dev/null || fail "No synaptic log"
            ;;
        *)
            echo "Usage: atlas-ops.sh synaptic [restart|speak \"msg\"|health|log [n]]"
            ;;
    esac
}

# =============================================================================
# INJECT
# =============================================================================
inject_cmd() {
    case "$ACTION" in
        test)
            PROMPT="${1:-how does the webhook injection system work}"
            WORD_COUNT=$(echo "$PROMPT" | wc -w | tr -d ' ')
            if [ "$WORD_COUNT" -lt 6 ]; then
                fail "Prompt too short ($WORD_COUNT words). Need 6+ words to trigger webhook."
                info "Webhook ignores <5 word prompts. Be descriptive."
                info "Example: atlas-ops.sh inject test \"how does the webhook injection system work\""
                return 1
            fi
            header "Testing injection latency..."
            info "Prompt: $PROMPT ($WORD_COUNT words)"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
import time
from memory.unified_injection import get_injection, InjectionPreset
start = time.time()
r = get_injection('$PROMPT', preset=InjectionPreset.CHAT, use_boundary_intelligence=False)
elapsed = time.time() - start
latency = r.metadata.get('latency_ms', '?')
sections = list(r.sections.keys())
print(f'Wall clock: {elapsed:.1f}s')
print(f'Metadata latency: {latency}ms')
print(f'Sections: {sections}')
print(f'Total chars: {sum(len(v[0]) if isinstance(v, tuple) else len(str(v)) for v in r.sections.values())}')
"
            ;;
        cache)
            header "Anticipation Cache Status"
            for SEC in s2 s6 s8; do
                KEY="contextdna:anticipation:${SEC}:er-simulator-superrepo:fallback"
                TTL=$($REDIS TTL "$KEY" 2>/dev/null)
                SIZE=$($REDIS STRLEN "$KEY" 2>/dev/null)
                if [ "$TTL" -gt 0 ] 2>/dev/null; then
                    ok "${SEC}: ${SIZE}B, TTL ${TTL}s ($(( TTL / 60 ))min)"
                else
                    fail "${SEC}: not cached (TTL=$TTL)"
                fi
            done
            # S10 strategic
            S10_KEY=$($REDIS KEYS "contextdna:strategic:s10:*" 2>/dev/null | head -1)
            if [ -n "$S10_KEY" ]; then
                TTL=$($REDIS TTL "$S10_KEY" 2>/dev/null)
                SIZE=$($REDIS STRLEN "$S10_KEY" 2>/dev/null)
                ok "s10: ${SIZE}B, TTL ${TTL}s ($(( TTL / 60 ))min)"
            else
                fail "s10: not cached"
            fi
            ;;
        refresh)
            header "Refreshing Anticipation Cache (S2/S6/S8)..."
            info "This runs the anticipation engine — may take 30-90s"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
from memory.anticipation_engine import run_anticipation_cycle
r = run_anticipation_cycle()
if r.get('ran'):
    print(f'Cached: {r[\"sections_cached\"]} in {r[\"elapsed_ms\"]:.0f}ms')
    print(f'Session: {r[\"session_id\"][:12]}... TTL: {r[\"adaptive_ttl\"]}s')
else:
    print(f'Skipped: {r.get(\"skipped_reason\", \"unknown\")}')
"
            ;;
        quality)
            header "S2/S6/S8 Content Quality"
            for SEC in s2 s6 s8; do
                echo -e "\n${CYAN}--- ${SEC} ---${NC}"
                # Try session-specific first, then fallback
                VALUE=""
                for KEY in $($REDIS KEYS "contextdna:anticipation:${SEC}:er-simulator-superrepo:*" 2>/dev/null); do
                    VALUE=$($REDIS GET "$KEY" 2>/dev/null)
                    if [ -n "$VALUE" ]; then
                        # Extract source_prompt and content preview
                        echo "$VALUE" | "$PYTHON" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    src = d.get('source_prompt', '?')
    content = d.get('content', '')
    age = d.get('generated_at', '?')
    print(f'  Source: \"{src}\"')
    print(f'  Generated: {age}')
    print(f'  Size: {len(content)} chars')
    print(f'  Preview: {content[:200]}...' if len(content) > 200 else f'  Content: {content}')
except Exception as e:
    print(f'  Parse error: {e}')
" 2>/dev/null
                        break
                    fi
                done
                [ -z "$VALUE" ] && fail "  Not cached"
            done
            ;;
        strategic)
            header "Running Strategic Analyst (S10 via GPT-4.1)..."
            info "This calls GPT-4.1 — costs ~$0.01"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
from memory.strategic_analyst import run_strategic_analysis_cycle
r = run_strategic_analysis_cycle()
if r:
    print(r[:500])
else:
    print('No result — check GPT-4.1 API key and budget')
"
            ;;
        *)
            echo "Usage: atlas-ops.sh inject [test \"prompt\"|cache|refresh|quality|strategic]"
            ;;
    esac
}

# =============================================================================
# LLM
# =============================================================================
llm_cmd() {
    case "$ACTION" in
        health)
            header "LLM Health (non-blocking)"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
from memory.llm_health_nonblocking import check_llm_health_nonblocking
healthy, reason = check_llm_health_nonblocking()
print(f\"{'Healthy' if healthy else 'Unhealthy'}: {reason}\")
"
            ;;
        stats)
            header "LLM Performance Stats"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
from memory.llm_health_nonblocking import get_llm_stats
stats = get_llm_stats()
if stats:
    print(f\"Last: {stats['last_generation_tokens']} tokens in {stats['last_generation_time']:.1f}s\")
    print(f\"Speed: {stats['last_generation_speed']:.1f} tok/s\")
    print(f\"Avg (last {stats['sample_count']}): {stats['avg_speed_recent']:.1f} tok/s\")
else:
    print('No generation stats available')
"
            ;;
        restart)
            header "Restarting LLM via launchctl..."
            launchctl kickstart -k "gui/$(id -u)/com.contextdna.llm" 2>/dev/null || \
            (launchctl unload ~/Library/LaunchAgents/com.contextdna.llm.plist 2>/dev/null; \
             sleep 2; \
             launchctl load ~/Library/LaunchAgents/com.contextdna.llm.plist 2>/dev/null)
            info "Waiting 15s for model load..."
            sleep 15
            if curl -s --max-time 2 http://127.0.0.1:5044/v1/models > /dev/null 2>&1; then
                ok "LLM responding on :5044"
            else
                fail "LLM not responding after restart"
            fi
            ;;
        status)
            header "LLM Hybrid Status (Surgery Team of 3)"
            cd "$REPO_ROOT"
            # Local surgeon
            if curl -s --max-time 2 http://127.0.0.1:5044/v1/models > /dev/null 2>&1; then
                ok "Local: Qwen3-4B-4bit @ :5044 (healthy)"
            else
                fail "Local: Qwen3-4B-4bit @ :5044 (down)"
            fi
            # External surgeon + cost/events
            PYTHONPATH=. "$PYTHON" -c "
import redis, json, os
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_timeout=1)
from datetime import date
today = date.today().isoformat()
# OpenAI key
has_key = bool(os.environ.get('Context_DNA_OPENAI') or '')
# Try loading from .env
if not has_key:
    try:
        with open('context-dna/.env') as f:
            for line in f:
                if line.startswith('Context_DNA_OPENAI=') and len(line.strip().split('=',1)[1]) > 10:
                    has_key = True
    except: pass
print(f\"  {'✓' if has_key else '✗'} External: GPT-4.1-mini via OpenAI ({'key present' if has_key else 'NO KEY'})\")
# Hybrid mode
mode = r.get('llm:hybrid_mode') or 'fallback-only'
print(f'  → Hybrid mode: {mode}')
# Budget
cost = float(r.get(f'llm:costs:{today}') or 0)
budget = float(os.environ.get('LLM_DAILY_BUDGET_USD', '5.0'))
print(f'  → Today cost: \${cost:.4f} / \${budget:.2f} budget')
# Provider stats
stats = r.hgetall('llm:provider_stats')
if stats:
    calls = stats.get('openai:calls', '0')
    total = float(stats.get('openai:total_cost', '0'))
    print(f'  → External calls: {calls} (total \${total:.4f})')
# Recent fallback events
events = r.lrange('llm:fallback_events', 0, 4)
if events:
    print(f'  → Recent fallbacks ({len(events)}):')
    for e in events[:3]:
        d = json.loads(e)
        from datetime import datetime
        ts = datetime.fromtimestamp(d['ts']).strftime('%H:%M:%S')
        print(f'    {ts} [{d[\"profile\"]}] → {d[\"model\"]} {d[\"latency_ms\"]}ms \${d[\"cost_usd\"]:.6f}')
else:
    print('  → No fallback events today')
" 2>/dev/null || warn "Could not query Redis telemetry"
            ;;
        gpu-lock)
            header "GPU Lock Status"
            LOCK_VAL=$($REDIS GET "llm:gpu_lock" 2>/dev/null)
            if [ -n "$LOCK_VAL" ]; then
                HOLDER_PID="$LOCK_VAL"
                LOCK_TTL=$($REDIS TTL "llm:gpu_lock" 2>/dev/null)
                # Identify the holder process
                PROC_NAME=$(ps -p "$HOLDER_PID" -o comm= 2>/dev/null || echo "unknown")
                if kill -0 "$HOLDER_PID" 2>/dev/null; then
                    warn "GPU locked by PID $HOLDER_PID ($PROC_NAME) — TTL ${LOCK_TTL}s — ALIVE"
                else
                    fail "GPU locked by PID $HOLDER_PID — TTL ${LOCK_TTL}s — DEAD (stale)"
                    info "Stale lock — will be auto-stolen on next request"
                fi
            else
                ok "GPU lock: FREE (no holder)"
            fi
            ;;
        *)
            echo "Usage: atlas-ops.sh llm [health|stats|restart|status|gpu-lock]"
            ;;
    esac
}

# =============================================================================
# REDIS
# =============================================================================
redis_cmd() {
    case "$ACTION" in
        keys)
            header "Redis Keys"
            $REDIS KEYS "*" 2>/dev/null | sort
            ;;
        info)
            header "Redis Info"
            $REDIS INFO keyspace 2>/dev/null
            $REDIS DBSIZE 2>/dev/null
            ;;
        get)
            KEY="$1"
            if [ -z "$KEY" ]; then
                echo "Usage: atlas-ops.sh redis get <key>"
                return
            fi
            header "Redis GET $KEY"
            VALUE=$($REDIS GET "$KEY" 2>/dev/null)
            if [ -n "$VALUE" ]; then
                LEN=$(echo -n "$VALUE" | wc -c)
                TTL=$($REDIS TTL "$KEY" 2>/dev/null)
                info "Length: ${LEN}B, TTL: ${TTL}s"
                echo "$VALUE" | head -c 500
                [ "$LEN" -gt 500 ] && echo -e "\n  ... (truncated, ${LEN}B total)"
            else
                fail "Key not found or empty"
            fi
            ;;
        *)
            echo "Usage: atlas-ops.sh redis [keys|info|get <key>]"
            ;;
    esac
}

# =============================================================================
# DOCKER (dual-stack diagnostics)
# =============================================================================
docker_cmd() {
    case "$ACTION" in
        status)
            docker_status
            ;;
        logs)
            CONTAINER="${1:-contextdna-api}"
            N="${2:-30}"
            header "Logs: $CONTAINER (last $N)"
            docker logs "$CONTAINER" --tail "$N" 2>&1
            ;;
        restart-api)
            header "Restarting ContextDNA API stack..."
            info "Dependency chain: rabbitmq → core → api"

            # Check if rabbitmq is running first
            RMQSTATUS=$(docker inspect -f '{{.State.Running}}' contextdna-rabbitmq 2>/dev/null)
            if [ "$RMQSTATUS" != "true" ]; then
                info "Starting contextdna-rabbitmq..."
                docker start contextdna-rabbitmq 2>/dev/null
                sleep 5
                RMQSTATUS=$(docker inspect -f '{{.State.Running}}' contextdna-rabbitmq 2>/dev/null)
                if [ "$RMQSTATUS" = "true" ]; then
                    ok "RabbitMQ started"
                else
                    fail "RabbitMQ failed to start — cannot proceed"
                    return 1
                fi
            else
                ok "RabbitMQ already running"
            fi

            # Start core (depends on rabbitmq)
            info "Starting contextdna-core..."
            docker start contextdna-core 2>/dev/null
            sleep 5

            # Start api (depends on core)
            info "Starting contextdna-api..."
            docker start contextdna-api 2>/dev/null
            sleep 5

            # Verify
            if curl -s --max-time 3 http://127.0.0.1:8029/health > /dev/null 2>&1; then
                ok "ContextDNA API responding on :8029"
            else
                fail "API not responding — check: atlas-ops.sh docker logs contextdna-api"
            fi
            ;;
        stop-api)
            header "Stopping ContextDNA API stack..."
            for C in contextdna-api contextdna-core contextdna-celery-worker contextdna-celery-beat contextdna-rabbitmq; do
                RUNNING=$(docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null)
                if [ "$RUNNING" = "true" ]; then
                    docker stop "$C" 2>/dev/null
                    ok "Stopped $C"
                else
                    info "$C already stopped"
                fi
            done
            ;;
        *)
            echo "Usage: atlas-ops.sh docker [status|logs <container> [n]|restart-api|stop-api]"
            ;;
    esac
}

docker_status() {
    header "=== DOCKER DUAL-STACK STATUS ==="
    echo ""

    # Stack 1: context-dna (local development — always needed)
    header "Stack 1: context-dna (local dev — ports 5432, 6379)"
    for C in context-dna-postgres context-dna-redis; do
        STATUS=$(docker inspect -f '{{.State.Status}} ({{.State.Health.Status}})' "$C" 2>/dev/null)
        if echo "$STATUS" | grep -q "running"; then
            ok "$C: $STATUS"
        else
            fail "$C: $STATUS"
        fi
    done

    # Stack 2: contextdna (full platform — API, Core, Celery)
    header "Stack 2: contextdna platform"

    # Critical path: rabbitmq → core → api
    echo -e "  ${CYAN}Critical path: rabbitmq → core → api${NC}"
    for C in contextdna-rabbitmq contextdna-core contextdna-api; do
        STATUS=$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null)
        if [ "$STATUS" = "running" ]; then
            ok "$C: running"
        else
            EXIT=$(docker inspect -f '{{.State.ExitCode}}' "$C" 2>/dev/null)
            FINISHED=$(docker inspect -f '{{.State.FinishedAt}}' "$C" 2>/dev/null | cut -c1-19)
            fail "$C: $STATUS (exit $EXIT, stopped $FINISHED)"
        fi
    done

    # Workers
    echo -e "  ${CYAN}Workers:${NC}"
    for C in contextdna-celery-worker contextdna-celery-beat; do
        STATUS=$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null)
        if [ "$STATUS" = "running" ]; then
            ok "$C: running"
        else
            EXIT=$(docker inspect -f '{{.State.ExitCode}}' "$C" 2>/dev/null)
            fail "$C: $STATUS (exit $EXIT)"
        fi
    done

    # Infrastructure
    echo -e "  ${CYAN}Infrastructure:${NC}"
    for C in contextdna-redis contextdna-pg contextdna-opensearch contextdna-traefik contextdna-grafana; do
        STATUS=$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null)
        if [ "$STATUS" = "running" ]; then
            ok "$C: running"
        else
            fail "$C: $STATUS"
        fi
    done

    # Stack 3: acontext-server (legacy)
    header "Stack 3: acontext-server (legacy)"
    for C in acontext-server-rabbitmq acontext-server-redis acontext-server-seaweedfs acontext-server-jaeger; do
        STATUS=$(docker inspect -f '{{.State.Status}} ({{.State.Health.Status}})' "$C" 2>/dev/null)
        if echo "$STATUS" | grep -q "running"; then
            ok "$C: $STATUS"
        else
            fail "$C: $STATUS"
        fi
    done

    # Port reachability
    header "Port Reachability"
    for PORT_DESC in "8029:ContextDNA-API" "8080:agent_service" "8888:Synaptic" "5044:LLM" "6379:Redis-local" "16379:Redis-docker"; do
        PORT="${PORT_DESC%%:*}"
        DESC="${PORT_DESC##*:}"
        if [ "$PORT" = "6379" ] || [ "$PORT" = "16379" ]; then
            # Redis check via docker exec
            if [ "$PORT" = "6379" ]; then
                PONG=$(docker exec context-dna-redis redis-cli PING 2>/dev/null)
            else
                PONG=$(docker exec contextdna-redis redis-cli -a "REDACTED-REDIS-PASSWORD" PING 2>/dev/null)
            fi
            if [ "$PONG" = "PONG" ]; then
                ok "$DESC (:$PORT): reachable"
            else
                fail "$DESC (:$PORT): unreachable"
            fi
        else
            if curl -s --max-time 2 "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
                ok "$DESC (:$PORT): reachable"
            else
                fail "$DESC (:$PORT): unreachable"
            fi
        fi
    done

    # Dual Redis key counts
    header "Redis Dual-Plane"
    LOCAL_KEYS=$(docker exec context-dna-redis redis-cli DBSIZE 2>/dev/null | grep -o '[0-9]*')
    DOCKER_KEYS=$(docker exec contextdna-redis redis-cli -a "REDACTED-REDIS-PASSWORD" DBSIZE 2>/dev/null | grep -o '[0-9]*')
    info "context-dna-redis (:6379): ${LOCAL_KEYS:-?} keys (local Python stack)"
    info "contextdna-redis (:16379): ${DOCKER_KEYS:-?} keys (Docker platform stack)"
    echo ""
}

# =============================================================================
# STATUS (full system)
# =============================================================================
status_cmd() {
    header "=== ATLAS SYSTEM STATUS ==="
    echo ""

    # Docker (summary)
    header "Docker"
    if docker info &>/dev/null 2>&1; then
        ok "Docker running"
        # Count running vs stopped contextdna containers
        RUNNING=$(docker ps --filter "name=contextdna" --format "{{.Names}}" 2>/dev/null | wc -l | tr -d ' ')
        STOPPED=$(docker ps -a --filter "name=contextdna" --filter "status=exited" --format "{{.Names}}" 2>/dev/null | wc -l | tr -d ' ')
        info "contextdna stack: ${RUNNING} running, ${STOPPED} stopped"
        # Port 8029 quick check
        if curl -s --max-time 2 http://127.0.0.1:8029/health > /dev/null 2>&1; then
            ok "ContextDNA API (:8029): UP"
        else
            fail "ContextDNA API (:8029): DOWN — run 'atlas-ops.sh docker status' for details"
        fi
        # Local stack containers
        for C in context-dna-postgres context-dna-redis; do
            STATUS=$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null)
            [ "$STATUS" = "running" ] && ok "$C: running" || fail "$C: $STATUS"
        done
    else
        fail "Docker not running"
    fi

    # Redis
    header "Redis"
    PONG=$($REDIS PING 2>/dev/null)
    if [ "$PONG" = "PONG" ]; then
        ok "Redis responding"
        DBSIZE=$($REDIS DBSIZE 2>/dev/null)
        info "$DBSIZE"
    else
        fail "Redis not responding"
    fi

    # LLM
    header "LLM (mlx_lm.server)"
    if pgrep -f "mlx_lm" > /dev/null 2>&1; then
        ok "Process running (PID $(pgrep -f mlx_lm | head -1))"
        if curl -s --max-time 2 http://127.0.0.1:5044/v1/models > /dev/null 2>&1; then
            ok "API responding on :5044"
        else
            warn "Process running but API not responding"
        fi
    else
        fail "Not running"
    fi

    # Scheduler
    header "Scheduler"
    if pgrep -f "scheduler_coordinator" > /dev/null 2>&1; then
        ok "Running (PID $(pgrep -f scheduler_coordinator | head -1))"
    else
        fail "Not running"
    fi

    # Synaptic
    header "Synaptic Chat Server"
    if pgrep -f "synaptic_chat_server" > /dev/null 2>&1; then
        ok "Process running (PID $(pgrep -f synaptic_chat_server | head -1))"
        HEALTH=$(curl -s --max-time 2 "http://localhost:8888/health" 2>/dev/null)
        if [ -n "$HEALTH" ]; then
            ok "API responding on :8888"
        else
            warn "Process running but API not responding"
        fi
    else
        fail "Not running"
    fi

    # Context DNA
    header "Context DNA"
    if curl -s --max-time 2 http://localhost:8029/health 2>/dev/null | grep -q '"code":0\|"msg":"ok"'; then
        ok "API responding on :8029"
    else
        fail "Not responding on :8029"
    fi

    # Agent Service
    header "Agent Service"
    if curl -s --max-time 2 http://localhost:8080/health 2>/dev/null | grep -q "ok\|healthy"; then
        ok "API responding on :8080"
    else
        fail "Not responding on :8080"
    fi

    # Anticipation Cache
    header "Anticipation Cache"
    for SEC in s2 s6 s8; do
        KEY="contextdna:anticipation:${SEC}:er-simulator-superrepo:fallback"
        TTL=$($REDIS TTL "$KEY" 2>/dev/null)
        if [ "$TTL" -gt 0 ] 2>/dev/null; then
            ok "${SEC}: cached (TTL ${TTL}s)"
        else
            fail "${SEC}: not cached"
        fi
    done
    echo ""
}

# =============================================================================
# SURGERY (multi-model collaboration)
# =============================================================================
surgery_cmd() {
    # Quick availability check via dedicated script
    if [ "$ACTION" = "check" ]; then
        "$REPO_ROOT/scripts/check-surgeons.sh" "$@"
        return $?
    fi
    SURGERY_SCRIPT="$REPO_ROOT/scripts/surgery-team.py"
    if [ ! -f "$SURGERY_SCRIPT" ]; then
        fail "Surgery team script not found: $SURGERY_SCRIPT"
        return 1
    fi
    cd "$REPO_ROOT"
    PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" "$ACTION" "$@"
}

# =============================================================================
# CRITICALS (session gold mining critical findings)
# =============================================================================
criticals_cmd() {
    case "$ACTION" in
        check|"")
            header "Critical Findings"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
from memory.session_gold_passes import get_critical_findings
cf = get_critical_findings()
if not cf:
    print('  ✓ No unacknowledged critical findings')
else:
    print(f'  ⚠ {len(cf)} critical finding(s):')
    for i, f in enumerate(cf):
        fid = f.get('finding_id', '?')
        p = f.get('pass', f.get('pass_id', '?'))
        sev = f.get('severity', 'critical')
        txt = f.get('finding', '')
        ts = f.get('created_at', '?')
        print(f'  [{fid}] [{p}] [{sev}] {txt}')
        print(f'       Created: {ts}')
"
            ;;
        ack)
            FINDING_ID="$1"
            ACK_ACTION="${2:-reviewed}"
            if [ -z "$FINDING_ID" ]; then
                echo "Usage: atlas-ops.sh criticals ack <finding_id> [action]"
                return 1
            fi
            header "Acknowledging finding #$FINDING_ID"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
from memory.session_gold_passes import acknowledge_critical_finding
ok = acknowledge_critical_finding($FINDING_ID, '$ACK_ACTION')
print(f'  {\"✓ Acknowledged\" if ok else \"✗ Failed\"}: finding #$FINDING_ID (action: $ACK_ACTION)')
"
            ;;
        all)
            header "All Critical Findings (including acknowledged)"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
from memory.session_gold_passes import get_critical_findings
cf = get_critical_findings(acknowledged=True)
if not cf:
    print('  No critical findings at all')
else:
    for f in cf:
        fid = f.get('finding_id', '?')
        p = f.get('pass', f.get('pass_id', '?'))
        sev = f.get('severity', 'critical')
        txt = f.get('finding', '')
        ack = f.get('ack_at', '')
        status = f'ACK ({ack})' if ack else 'OPEN'
        print(f'  [{fid}] [{p}] [{sev}] [{status}] {txt}')
"
            ;;
        *)
            echo "Usage: atlas-ops.sh criticals [check|ack <id> [action]|all]"
            ;;
    esac
}

# =============================================================================
# SESSION (historian — rehydrate, extract, stats)
# =============================================================================
session_cmd() {
    case "$ACTION" in
        rehydrate)
            header "Rehydrating Last Session"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" memory/session_historian.py rehydrate "$@"
            ;;
        extract)
            header "Running Session Historian (full pipeline)"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
from memory.session_historian import SessionHistorian
h = SessionHistorian()
result = h.run()
print(f'Extracted: {result.get(\"extracted\", 0)}, Cleaned: {result.get(\"reclaimed_mb\", 0):.1f}MB')
"
            ;;
        extract-fast)
            header "Running Session Historian (fast extraction)"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" memory/session_historian.py run-fast
            ;;
        stats)
            header "Session Archive Stats"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" memory/session_historian.py stats
            ;;
        search)
            QUERY="$1"
            if [ -z "$QUERY" ]; then
                echo "Usage: atlas-ops.sh session search \"query\""
                return 1
            fi
            header "Searching Session Archive"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" memory/session_historian.py search "$QUERY"
            ;;
        *)
            echo "Usage: atlas-ops.sh session [rehydrate [id]|extract|extract-fast|stats|search \"query\"]"
            ;;
    esac
}

# =============================================================================
# CARDIO (cardiologist pipeline)
# =============================================================================
cardio_cmd() {
    case "$ACTION" in
        status)
            header "Cardiologist Status"
            cd "$REPO_ROOT"
            # Last run from Redis
            PYTHONPATH=. "$PYTHON" -c "
import redis, json
from datetime import datetime
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_timeout=2)
# Last diagnosis
diag = r.get('quality:cardiologist:last_diagnosis')
if diag:
    d = json.loads(diag)
    ts = d.get('timestamp', '?')
    status = d.get('status', '?')
    severity = d.get('severity', '?')
    print(f'  Last diagnosis: {status} (severity: {severity})')
    print(f'  Timestamp: {ts}')
    findings = d.get('findings', [])
    if findings:
        print(f'  Findings ({len(findings)}):')
        for f in findings[:5]:
            print(f'    - {f}')
else:
    print('  No cardiologist diagnosis in Redis')
# Cross-exam result
xr = r.get('quality:cross_exam_result')
if xr:
    x = json.loads(xr)
    print(f'  Cross-exam: {x.get(\"consensus_status\", \"?\")} (confidence: {x.get(\"confidence\", \"?\")})')
    if x.get('blind_spots'):
        print(f'  Blind spots: {len(x[\"blind_spots\"])}')
" 2>/dev/null || warn "Could not query Redis"
            ;;
        run)
            header "Running Cardio Gate..."
            "$REPO_ROOT/scripts/cardio-gate.sh"
            ;;
        manual)
            header "Running Cardio Gate (manual, skip rate limit)..."
            "$REPO_ROOT/scripts/cardio-gate.sh" --manual
            ;;
        dry-run)
            header "Cardio Gate Dry Run..."
            "$REPO_ROOT/scripts/cardio-gate.sh" --dry-run
            ;;
        *)
            echo "Usage: atlas-ops.sh cardio [status|run|manual|dry-run]"
            ;;
    esac
}

# =============================================================================
# GAINS (gains-gate wrapper)
# =============================================================================
gains_cmd() {
    case "$ACTION" in
        check|"")
            header "Running Gains Gate..."
            "$REPO_ROOT/scripts/gains-gate.sh" --verbose
            EXIT=$?
            echo ""
            if [ $EXIT -eq 0 ]; then
                ok "All critical checks passed"
            else
                fail "Critical failures detected (exit $EXIT)"
            fi
            ;;
        cardio)
            header "Running Gains Gate (cardio mode)..."
            "$REPO_ROOT/scripts/gains-gate.sh" --cardio
            ;;
        *)
            echo "Usage: atlas-ops.sh gains [check|cardio]"
            ;;
    esac
}

# =============================================================================
# EVIDENCE (query evidence/memory store)
# =============================================================================
evidence_cmd() {
    if [ -z "$ACTION" ]; then
        echo "Usage: atlas-ops.sh evidence \"search query\""
        echo ""
        echo "Examples:"
        echo "  atlas-ops.sh evidence \"webhook latency\""
        echo "  atlas-ops.sh evidence \"docker restart\""
        echo "  atlas-ops.sh evidence \"GPU lock stale\""
        return 1
    fi
    QUERY="$ACTION"
    [ -n "$ARGS" ] && QUERY="$ACTION $ARGS"
    header "Evidence Query: $QUERY"
    cd "$REPO_ROOT"
    PYTHONPATH=. "$PYTHON" memory/query.py "$QUERY"
}

# =============================================================================
# NEUROLOGIST (Qwen3-4B dedicated skills)
# =============================================================================
neurologist_cmd() {
    SURGERY_SCRIPT="$REPO_ROOT/scripts/surgery-team.py"
    case "$ACTION" in
        pulse|"")
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" neurologist-pulse
            ;;
        challenge)
            TOPIC="$1"
            if [ -z "$TOPIC" ]; then
                echo "Usage: atlas-ops.sh neurologist challenge \"topic to challenge\""
                echo ""
                echo "Examples:"
                echo "  atlas-ops.sh neurologist challenge \"webhook injection is reliable\""
                echo "  atlas-ops.sh neurologist challenge \"gold mining yield is optimal\""
                return 1
            fi
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" neurologist-challenge "$TOPIC"
            ;;
        *)
            echo "Usage: atlas-ops.sh neurologist [pulse|challenge \"topic\"]"
            echo ""
            echo "  pulse      Live system health from Redis + SQLite (default)"
            echo "  challenge  Corrigibility skeptic — challenge assumptions"
            ;;
    esac
}

# =============================================================================
# AB (A/B test lifecycle)
# =============================================================================
ab_cmd() {
    SURGERY_SCRIPT="$REPO_ROOT/scripts/surgery-team.py"
    case "$ACTION" in
        propose)
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" ab-propose "$@"
            ;;
        collaborate)
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" ab-collaborate "$@"
            ;;
        start)
            REF="$1"
            if [ -z "$REF" ]; then
                echo "Usage: atlas-ops.sh ab start <ref>"
                return 1
            fi
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" ab-start "$REF"
            ;;
        measure)
            REF="$1"
            if [ -z "$REF" ]; then
                echo "Usage: atlas-ops.sh ab measure <ref>"
                return 1
            fi
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" ab-measure "$REF"
            ;;
        conclude)
            REF="$1"
            VERDICT="$2"
            if [ -z "$REF" ] || [ -z "$VERDICT" ]; then
                echo "Usage: atlas-ops.sh ab conclude <ref> win|lose|inconclusive"
                return 1
            fi
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" ab-conclude "$REF" "$VERDICT"
            ;;
        veto)
            REF="$1"
            if [ -z "$REF" ]; then
                echo "Usage: atlas-ops.sh ab veto <ref>"
                return 1
            fi
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" ab-veto "$REF"
            ;;
        validate)
            DESC="$*"
            if [ -z "$DESC" ]; then
                echo "Usage: atlas-ops.sh ab validate \"description of fix\""
                return 1
            fi
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" ab-validate "$DESC"
            ;;
        queue)
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" ab-queue
            ;;
        status|"")
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$SURGERY_SCRIPT" ab-status
            ;;
        *)
            echo "Usage: atlas-ops.sh ab [status|propose|collaborate|start|measure|conclude|validate|veto|queue]"
            echo ""
            echo "  status       A/B test dashboard (default)"
            echo "  propose      Design A/B test for a claim"
            echo "  collaborate  3-surgeon A/B design (full consensus)"
            echo "  start <ref>  Activate an approved test"
            echo "  measure <ref> Measure an active test"
            echo "  conclude <ref> win|lose|inconclusive"
            echo "  validate     Quick 3-surgeon fix validation (consensus required)"
            echo "  veto <ref>   Veto an autonomous test"
            echo "  queue        Autonomous A/B test queue"
            ;;
    esac
}

# =============================================================================
# SENTINEL (complexity vector drift detection)
# =============================================================================
sentinel_cmd() {
    case "$ACTION" in
        run|"")
            header "Running Complexity Vector Sentinel..."
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
from memory.complexity_vector_sentinel import ComplexityVectorSentinel
s = ComplexityVectorSentinel()
r = s.run_cycle()
risk = r.get('risk_level', 'unknown')
triggered = r.get('triggered', [])
skipped = r.get('skipped', '')
dur = r.get('duration_ms', 0)
if skipped:
    print(f'  Skipped: {skipped} ({dur}ms)')
else:
    emoji = {'none': '✓', 'low': '→', 'medium': '⚠', 'high': '⚠', 'critical': '✗'}.get(risk, '?')
    print(f'  {emoji} Risk: {risk} (score: {r.get(\"risk_score\", 0)}) — {dur}ms')
    if triggered:
        print(f'  Triggered vectors: {triggered}')
    if r.get('injected'):
        print(f'  → Injected into S0 critical')
    if r.get('escalated'):
        print(f'  → Escalated to corrigibility challenge')
"
            ;;
        status)
            header "Sentinel Status"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" -c "
import redis, json
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_timeout=2)
# Last run
last = r.get('contextdna:cv_sentinel:last_run')
if last:
    d = json.loads(last)
    print(f'  Last run: {d.get(\"at\", \"?\")}')
    print(f'  Risk: {d.get(\"risk\", \"?\")} (score: {d.get(\"score\", 0)})')
    print(f'  Triggered: {d.get(\"triggered\", [])}')
    print(f'  Duration: {d.get(\"duration_ms\", 0)}ms')
    ttl = r.ttl('contextdna:cv_sentinel:last_run')
    print(f'  TTL: {ttl}s')
else:
    print('  No sentinel run recorded in Redis')
# Current risk level
risk = r.get('contextdna:cv_sentinel:risk_level')
if risk:
    print(f'  Current escalation: {risk}')
# Recent criticals
crits = r.lrange('contextdna:critical:recent', 0, 4)
if crits:
    print(f'  Recent criticals ({r.llen(\"contextdna:critical:recent\")}):')
    for c in crits[:3]:
        d = json.loads(c)
        print(f'    [{d.get(\"severity\",\"?\")}] {d.get(\"finding\",\"\")[:100]}')
" 2>/dev/null || warn "Could not query Redis"
            ;;
        *)
            echo "Usage: atlas-ops.sh sentinel [run|status]"
            echo ""
            echo "  run      Run sentinel cycle now (default)"
            echo "  status   Last run + current risk level from Redis"
            ;;
    esac
}

# =============================================================================
# CORRIGIBILITY (gate + review)
# =============================================================================
corrigibility_cmd() {
    case "$ACTION" in
        gate|"")
            PHASE="${1:-unknown}"
            header "Running Corrigibility Gate (phase: $PHASE)..."
            "$REPO_ROOT/scripts/corrigibility-gate.sh" "$PHASE"
            EXIT=$?
            if [ $EXIT -eq 0 ]; then
                ok "All corrigibility checks passed"
            else
                fail "Regression detected (exit $EXIT) — STOP"
            fi
            ;;
        review)
            TOPIC="$1"
            if [ -z "$TOPIC" ]; then
                echo "Usage: atlas-ops.sh corrigibility review \"topic\""
                echo ""
                echo "Examples:"
                echo "  atlas-ops.sh corrigibility review \"webhook reliability\""
                echo "  atlas-ops.sh corrigibility review \"priority queue fairness\""
                return 1
            fi
            header "Corrigibility Review: $TOPIC"
            cd "$REPO_ROOT"
            PYTHONPATH=. "$PYTHON" "$REPO_ROOT/scripts/surgery-team.py" cardio-review "$TOPIC"
            ;;
        *)
            echo "Usage: atlas-ops.sh corrigibility [gate [phase]|review \"topic\"]"
            echo ""
            echo "  gate [phase]      Run corrigibility gate (default)"
            echo "  review \"topic\"    3-surgeon corrigibility review"
            ;;
    esac
}

# =============================================================================
# RECOVER (hook/mcp/all recovery)
# =============================================================================
recover_cmd() {
    case "$ACTION" in
        hook)
            header "Recovering Hook..."
            "$REPO_ROOT/scripts/recover-hook.sh"
            ;;
        mcp)
            header "Recovering MCP..."
            "$REPO_ROOT/scripts/recover-mcp.sh"
            ;;
        all|"")
            header "Full Recovery (hook + mcp + services)..."
            "$REPO_ROOT/scripts/recover-all.sh"
            ;;
        *)
            echo "Usage: atlas-ops.sh recover [hook|mcp|all]"
            echo ""
            echo "  hook    Recover webhook hook"
            echo "  mcp     Recover MCP connection"
            echo "  all     Full recovery (default)"
            ;;
    esac
}

# =============================================================================
# PROFESSOR (quick professor wisdom query)
# =============================================================================
professor_cmd() {
    if [ -z "$ACTION" ]; then
        echo "Usage: atlas-ops.sh professor \"task description\""
        echo ""
        echo "Examples:"
        echo "  atlas-ops.sh professor \"adding recency decay to SOP scoring\""
        echo "  atlas-ops.sh professor \"fixing webhook injection overflow\""
        echo "  atlas-ops.sh professor \"optimizing gold mining throughput\""
        return 1
    fi
    QUERY="$ACTION"
    [ -n "$ARGS" ] && QUERY="$ACTION $ARGS"
    header "Professor Consultation: $QUERY"
    cd "$REPO_ROOT"
    PYTHONPATH=. "$PYTHON" memory/professor.py "$QUERY"
}

# =============================================================================
# HELP
# =============================================================================
help_cmd() {
    echo -e "${BOLD}Atlas Operations${NC} — unified CLI for system operations"
    echo ""
    echo "Usage: atlas-ops.sh <service> <action> [args...]"
    echo ""
    echo -e "${CYAN}Infrastructure:${NC}"
    echo "  scheduler  start|status|stop|log [n]"
    echo "  llm        health|stats|restart|status|gpu-lock"
    echo "  docker     status|logs <container> [n]|restart-api|stop-api"
    echo "  redis      keys|info|get <key>"
    echo "  status     Full system status check"
    echo ""
    echo -e "${CYAN}Intelligence:${NC}"
    echo "  inject     test \"prompt (6+ words)\"|cache|refresh|quality|strategic"
    echo "  synaptic   restart|speak \"msg (6+ words)\"|health|log [n]"
    echo "  professor  \"task description\"                    — professor wisdom query"
    echo "  evidence   \"search query\"                        — search evidence/memory store"
    echo "  surgery    probe|ask-local|ask-remote|consult|cross-exam|consensus|status"
    echo ""
    echo -e "${CYAN}Surgeons (dedicated):${NC}"
    echo "  neurologist    pulse|challenge \"topic\"             — Qwen3-4B system pulse + corrigibility"
    echo "  ab             status|propose|collaborate|start|measure|conclude|validate|veto|queue"
    echo "  sentinel       run|status                          — complexity vector drift detection"
    echo ""
    echo -e "${CYAN}Quality:${NC}"
    echo "  criticals      check|ack <id> [action]|all         — critical findings from gold mining"
    echo "  cardio         status|run|manual|dry-run           — cardiologist EKG pipeline"
    echo "  corrigibility  gate [phase]|review \"topic\"         — corrigibility gate + 3-surgeon review"
    echo "  gains          check|cardio                        — gains-gate (system health checks)"
    echo "  recover        hook|mcp|all                        — recovery scripts"
    echo "  session        rehydrate [id]|extract|extract-fast|stats|search \"query\""
    echo ""
    echo "Examples:"
    echo "  atlas-ops.sh status                                  # Full system overview"
    echo "  atlas-ops.sh criticals check                         # Unacknowledged critical findings"
    echo "  atlas-ops.sh criticals ack 42 \"fixed in session\"     # Acknowledge finding"
    echo "  atlas-ops.sh session rehydrate                       # Recover last session context"
    echo "  atlas-ops.sh session extract                         # Run full historian pipeline"
    echo "  atlas-ops.sh cardio status                           # Last cardiologist diagnosis"
    echo "  atlas-ops.sh cardio dry-run                          # Check without executing"
    echo "  atlas-ops.sh gains check                             # Run all health checks"
    echo "  atlas-ops.sh professor \"adding recency decay to SOPs\"  # Quick professor consult"
    echo "  atlas-ops.sh evidence \"webhook latency\"               # Search evidence store"
    echo "  atlas-ops.sh inject cache                            # Check anticipation cache"
    echo "  atlas-ops.sh surgery cross-exam \"webhook architecture\" # Full cross-examination"
    echo "  atlas-ops.sh llm status                              # Hybrid Surgery Team status"
    echo "  atlas-ops.sh neurologist pulse                         # Qwen3-4B system health deep dive"
    echo "  atlas-ops.sh neurologist challenge \"gold mining yield\"  # Corrigibility skeptic"
    echo "  atlas-ops.sh ab status                                 # A/B test dashboard"
    echo "  atlas-ops.sh ab propose \"webhook caching improves P95\" # Design A/B test"
    echo "  atlas-ops.sh sentinel run                              # Run complexity vector scan now"
    echo "  atlas-ops.sh sentinel status                           # Last sentinel results"
    echo "  atlas-ops.sh corrigibility gate phase-d                # Run corrigibility gate"
    echo "  atlas-ops.sh corrigibility review \"webhook reliability\" # 3-surgeon review"
    echo "  atlas-ops.sh recover all                               # Full system recovery"
}

# =============================================================================
# DISPATCH
# =============================================================================
case "$SERVICE" in
    scheduler)      scheduler_cmd "$@" ;;
    synaptic)       synaptic_cmd "$@" ;;
    inject)         inject_cmd "$@" ;;
    llm)            llm_cmd "$@" ;;
    surgery)        surgery_cmd "$@" ;;
    criticals)      criticals_cmd "$@" ;;
    session)        session_cmd "$@" ;;
    cardio)         cardio_cmd "$@" ;;
    gains)          gains_cmd "$@" ;;
    evidence)       evidence_cmd ;;
    professor)      professor_cmd ;;
    neurologist)    neurologist_cmd "$@" ;;
    ab)             ab_cmd "$@" ;;
    sentinel)       sentinel_cmd "$@" ;;
    corrigibility)  corrigibility_cmd "$@" ;;
    recover)        recover_cmd "$@" ;;
    redis)          redis_cmd "$@" ;;
    docker)         docker_cmd "$@" ;;
    status)         status_cmd ;;
    help|-h|--help) help_cmd ;;
    *)
        echo "Unknown service: $SERVICE"
        help_cmd
        exit 1
        ;;
esac
