#!/bin/bash
# ============================================================================
# GAINS GATE — Post-Phase Verification Script
# ============================================================================
# Runs after every major phase completion (A→C, C→D, D→E) to verify
# infrastructure integrity. All critical checks must pass before proceeding.
#
# 3-Surgeon Consensus (2026-02-23): 100% agreement on automated script,
# <30s runtime, block on critical failures only.
#
# Usage: ./scripts/gains-gate.sh [--verbose] [--cardio] [--soft]
#   --soft: downgrades 3s probe to warning (use during 3s outage when surgeons
#           are not required for current work; does NOT downgrade other checks)
# Exit: 0 = all critical checks pass, 1 = critical failure(s)
# ============================================================================

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

VERBOSE=false
CARDIO_MODE=false
SOFT_MODE=false
for arg in "$@"; do
    [[ "$arg" == "--verbose" ]] && VERBOSE=true
    [[ "$arg" == "--cardio" ]] && CARDIO_MODE=true
    [[ "$arg" == "--soft" ]] && SOFT_MODE=true
done

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

CRITICAL_FAILURES=0
WARNINGS=0
TOTAL_CHECKS=0
RESULTS=()

# Race P3: derive expected check count from script source so the banner stays
# truthful even when checks are added/removed. Counts the numbered `# N.` block
# headers in the Checks section (one per distinct check, regardless of how many
# `check` invocations its if/elif branches contain).
TOTAL_CHECKS_EXPECTED=$(grep -cE '^# [0-9]+\. ' "$0" 2>/dev/null || echo 0)
[[ "$TOTAL_CHECKS_EXPECTED" -lt 1 ]] && TOTAL_CHECKS_EXPECTED=15  # safety fallback

# ── Helpers ──────────────────────────────────────────────────────────────────

check() {
    local name="$1"
    local critical="$2"  # "critical" or "warning"
    local result="$3"    # 0=pass, 1=fail
    local detail="${4:-}"

    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))

    if [[ "$result" -eq 0 ]]; then
        RESULTS+=("${GREEN}✓${NC} ${name}${detail:+ — $detail}")
    elif [[ "$critical" == "critical" ]]; then
        CRITICAL_FAILURES=$((CRITICAL_FAILURES + 1))
        RESULTS+=("${RED}✗ CRITICAL${NC} ${name}${detail:+ — $detail}")
    else
        WARNINGS=$((WARNINGS + 1))
        RESULTS+=("${YELLOW}⚠ WARNING${NC} ${name}${detail:+ — $detail}")
    fi
}

# ── Checks ───────────────────────────────────────────────────────────────────

echo -e "${BOLD}${CYAN}═══ GAINS GATE — Post-Phase Verification (${TOTAL_CHECKS_EXPECTED} critical checks) ═══${NC}"
[[ "$SOFT_MODE" == true ]] && echo -e "${YELLOW}  (--soft: 3s probe downgraded to warning)${NC}"
echo ""
START_TIME=$(date +%s%N)

# 1. Webhook / agent_service (port 8080)
if curl -sf --max-time 3 http://127.0.0.1:8080/health > /dev/null 2>&1; then
    check "Webhook (agent_service:8080)" "critical" 0
else
    check "Webhook (agent_service:8080)" "critical" 1 "port 8080 not responding"
fi

# 2. LLM server (port 5044) — critical on arm64 (Apple Silicon), warning on Intel (no MLX)
if curl -sf --max-time 3 http://127.0.0.1:5044/v1/models > /dev/null 2>&1; then
    check "LLM server (mlx:5044)" "critical" 0
else
    _ARCH=$(uname -m)
    if [[ "$_ARCH" == "arm64" ]]; then
        check "LLM server (mlx:5044)" "critical" 1 "port 5044 not responding"
    else
        check "LLM server (mlx:5044)" "warning" 1 "port 5044 not responding (Intel: mlx not available)"
    fi
fi

# 3. Redis (via Python — redis-cli not installed on host)
REDIS_RESULT=$(PYTHONPATH=. .venv/bin/python3 -c "
import redis
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_timeout=2)
print(f'OK:{r.dbsize()}')
" 2>/dev/null || echo "FAIL")
if [[ "$REDIS_RESULT" == OK:* ]]; then
    DBSIZE="${REDIS_RESULT#OK:}"
    check "Redis (6379)" "critical" 0 "${DBSIZE} keys"
else
    DBSIZE="0"
    check "Redis (6379)" "critical" 1 "not responding"
fi

# 4. Scheduler PID
SCHED_PID_FILE="$REPO_DIR/memory/.scheduler_coordinator.pid"
if [[ -f "$SCHED_PID_FILE" ]]; then
    SCHED_PID=$(cat "$SCHED_PID_FILE" 2>/dev/null)
    if kill -0 "$SCHED_PID" 2>/dev/null; then
        check "Scheduler (PID $SCHED_PID)" "critical" 0
    else
        check "Scheduler" "critical" 1 "PID $SCHED_PID not alive"
    fi
else
    check "Scheduler" "critical" 1 "no PID file"
fi

# 5. Synaptic (port 8888) — markdown doc-index server.
# Severity: WARNING (not critical). Synaptic is a token-saving optimisation:
# when down, agents fall back to raw markdown reads (token explosion), but the
# P0 path (webhook/agent_service/llm/scheduler/redis) keeps working. See
# scripts/sprint-aaron-actions.sh action_synaptic — explicitly "MANUAL" with
# a "fall back to raw doc reads" note, never auto-restarted.
# 2026-05-06 (U3): downgraded from critical → warning to stop firing on every
# gate run. Canonical start command surfaced in detail when down.
SYNAPTIC_START_HINT="./scripts/context-dna-start (or: PYTHONPATH=. .venv/bin/python -m uvicorn memory.synaptic_chat_server:app --host 0.0.0.0 --port 8888)"
if curl -sf --max-time 3 http://127.0.0.1:8888/markdown/health > /dev/null 2>&1; then
    INDEXED=$(curl -sf --max-time 3 http://127.0.0.1:8888/markdown/health 2>/dev/null | .venv/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('indexed','?'))" 2>/dev/null || echo "?")
    check "Synaptic (8888)" "warning" 0 "${INDEXED} docs indexed"
else
    check "Synaptic (8888)" "warning" 1 "not responding (optional; raw-doc fallback active). Start: ${SYNAPTIC_START_HINT}"
fi

# 6. ContextDNA (port 8029) — non-critical (Docker-dependent)
if curl -sf --max-time 3 http://127.0.0.1:8029/health > /dev/null 2>&1; then
    check "ContextDNA (8029)" "warning" 0
else
    check "ContextDNA (8029)" "warning" 1 "not responding (Docker-dependent)"
fi

# 7. GPU lock — check for stale lock (via Python)
GPU_RESULT=$(PYTHONPATH=. .venv/bin/python3 -c "
import redis, json, os
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_timeout=2)
lock = r.get('llm:gpu_lock')
if not lock:
    print('FREE')
else:
    # Lock value can be plain PID string or JSON with pid field
    try:
        data = json.loads(lock)
        pid = data.get('pid', 0)
    except (json.JSONDecodeError, AttributeError):
        pid = lock  # Plain PID string
    try:
        os.kill(int(pid), 0)
        print(f'HELD:{pid}')
    except (OSError, ValueError):
        print(f'STALE:{pid}')
" 2>/dev/null || echo "UNKNOWN")
if [[ "$GPU_RESULT" == "FREE" ]]; then
    check "GPU lock" "critical" 0 "no lock held"
elif [[ "$GPU_RESULT" == HELD:* ]]; then
    check "GPU lock" "critical" 0 "held by PID ${GPU_RESULT#HELD:} (alive)"
elif [[ "$GPU_RESULT" == STALE:* ]]; then
    check "GPU lock" "critical" 1 "stale lock held by dead PID ${GPU_RESULT#STALE:}"
else
    check "GPU lock" "warning" 1 "could not check"
fi

# 8. Critical findings — must be 0 unacknowledged
# --cardio mode: exclude quality_cardiologist findings (cross-exam IS the response)
if [[ "$CARDIO_MODE" == true ]]; then
    CRIT_COUNT=$(PYTHONPATH=. .venv/bin/python3 -c "
from memory.session_gold_passes import get_critical_findings
cf = get_critical_findings()
non_cardio = [f for f in (cf or []) if f.get('pass_id','') != 'quality_cardiologist' and f.get('pass','') != 'quality_cardiologist']
print(len(non_cardio))
" 2>/dev/null || echo "-1")
else
    CRIT_COUNT=$(PYTHONPATH=. .venv/bin/python3 -c "
from memory.session_gold_passes import get_critical_findings
cf = get_critical_findings()
print(len(cf) if cf else 0)
" 2>/dev/null || echo "-1")
fi
if [[ "$CRIT_COUNT" == "0" ]]; then
    if [[ "$CARDIO_MODE" == true ]]; then
        check "Critical findings" "critical" 0 "0 non-cardio unacknowledged (cardio mode)"
    else
        check "Critical findings" "critical" 0 "0 unacknowledged"
    fi
elif [[ "$CRIT_COUNT" == "-1" ]]; then
    check "Critical findings" "warning" 1 "query failed"
else
    check "Critical findings" "critical" 1 "${CRIT_COUNT} unacknowledged"
fi

# 9. LLM test query — P2 classify (local) or extract_deep (external fallback on Intel/no-MLX)
# classify is LOCAL_ONLY so falls back to extract_deep when local LLM absent (Intel mac2).
LLM_TEST=$(PYTHONPATH=. .venv/bin/python3 -c "
import platform, sys
from memory.llm_priority_queue import llm_generate, Priority
# Try classify first (local LLM path)
r = llm_generate('Classify: test', 'hello', Priority.ATLAS, 'classify', 'gains_gate', timeout_s=10.0)
if not r:
    # classify is local-only; on Intel macs (no MLX) fall through to external-eligible profile
    machine = platform.machine()
    if machine != 'arm64':
        r = llm_generate('Say ok', 'ok', Priority.ATLAS, 'extract_deep', 'gains_gate_fallback', timeout_s=15.0)
print('OK' if r and len(r) > 0 else 'FAIL')
" 2>/dev/null || echo "FAIL")
if [[ "$LLM_TEST" == "OK" ]]; then
    check "LLM test query (P2 classify)" "critical" 0
else
    check "LLM test query (P2 classify)" "critical" 1 "classify call failed"
fi

# 10. WAL health — .observability.db (uncheckpointed pages, not file size)
# WHY: PASSIVE checkpoint leaves WAL file physically large but logically empty —
# file size triggers false alarms. Uncheckpointed pages (log > ckpt) = real problem.
# NOTE: PASSIVE is the lightest checkpoint mode — never blocks writers/readers,
# only moves pages already free. ~1ms cost. It DOES write (moves pages to DB),
# but this is beneficial cleanup, equivalent to SQLite's auto-checkpoint at 1000 pages.
# No read-only alternative exists for uncheckpointed page count.
OBS_DB="$REPO_DIR/memory/.observability.db"
if [[ -f "$OBS_DB" ]]; then
    WAL_RESULT=$(.venv/bin/python3 -c "
import sqlite3, sys
try:
    conn = sqlite3.connect('$OBS_DB', timeout=5)
    r = conn.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
    conn.close()
    print(f'{r[0]} {r[1]} {r[2]}')
except Exception as e:
    print(f'ERROR {e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)
    if [[ -n "$WAL_RESULT" ]]; then
        WAL_LOG=$(echo "$WAL_RESULT" | cut -d' ' -f2)
        WAL_CKPT=$(echo "$WAL_RESULT" | cut -d' ' -f3)
        WAL_UNCHECKPOINTED=$((WAL_LOG - WAL_CKPT))
        if [[ "$WAL_UNCHECKPOINTED" -gt 1000 ]]; then
            check "WAL size (.observability.db)" "critical" 1 "${WAL_UNCHECKPOINTED} uncheckpointed pages (log=${WAL_LOG} ckpt=${WAL_CKPT})"
        elif [[ "$WAL_UNCHECKPOINTED" -gt 100 ]]; then
            check "WAL size (.observability.db)" "warning" 1 "${WAL_UNCHECKPOINTED} uncheckpointed pages"
        else
            check "WAL size (.observability.db)" "warning" 0 "healthy (log=${WAL_LOG} ckpt=${WAL_CKPT})"
        fi
    else
        check "WAL size (.observability.db)" "warning" 1 "could not query wal_checkpoint (DB may be locked)"
    fi
else
    check "WAL size (.observability.db)" "warning" 0 "no DB file"
fi

# 11. Redis key sanity
if [[ "${DBSIZE:-0}" -gt 0 ]]; then
    check "Redis key sanity" "warning" 0 "${DBSIZE} keys"
else
    check "Redis key sanity" "warning" 1 "0 keys — suspiciously empty"
fi

# 12. V12 Action Registry coverage (warning-only — tracks drift)
REGISTRY_CACHE="$REPO_DIR/scripts/.action-registry-cache.json"
if [[ -f "$REGISTRY_CACHE" ]]; then
    V12_RESULT=$(PYTHONPATH="$REPO_DIR" .venv/bin/python3 -c "
import redis, json
try:
    r = redis.Redis(decode_responses=True, socket_timeout=2)
    # Check atlas-ops invocations (logged by atlas-ops.sh preflight)
    ops = r.zrevrange('contextdna:atlas_ops:invocations', 0, 99)
    # Check scheduler invocations (logged by lite_scheduler.py)
    sched = r.zrevrange('contextdna:scheduler:invocations', 0, 99)
    with open('$REGISTRY_CACHE') as f:
        registry = json.load(f)
    reg_ids = {a.get('id','') for a in registry}
    total_ops = len(ops) + len(sched)
    # Count dark ops: invocations with action IDs not in registry
    dark = 0
    for entry in ops:
        d = json.loads(entry)
        aid = d.get('action_id', d.get('svc', ''))
        if aid and aid not in reg_ids:
            dark += 1
    print(f'{dark}|{total_ops}|{len(reg_ids)} registered actions')
except Exception as e:
    print(f'0|0|error: {str(e)[:60]}')
" 2>/dev/null)
    V12_DARK=$(echo "$V12_RESULT" | cut -d'|' -f1)
    V12_TOTAL=$(echo "$V12_RESULT" | cut -d'|' -f2)
    V12_MSG=$(echo "$V12_RESULT" | cut -d'|' -f3)
    # Race P3 (ZSF): if the python helper hit an exception (e.g. zero-byte
    # cache → JSON parse error) the message carries "error:" — flip to WARNING
    # instead of falling through to PASS. Silent parse fails violate ZSF.
    if [[ "$V12_MSG" == *"error:"* ]]; then
        check "V12 action registry coverage" "warning" 1 "${V12_MSG}"
    elif [[ "${V12_DARK:-0}" -gt 5 ]]; then
        check "V12 action registry coverage" "warning" 1 "${V12_MSG} (${V12_DARK}/${V12_TOTAL} dark actions)"
    else
        check "V12 action registry coverage" "warning" 0 "${V12_TOTAL} invocations tracked, ${V12_MSG}"
    fi
else
    check "V12 action registry coverage" "warning" 1 "no registry cache (run: ./scripts/action-registry.sh list)"
fi

# 13. Secret scan — multi-fleet/ subtree must contain no leaked credentials
# Race S: gates pre-publish (and any phase commit) on credential exposure.
# Critical because a leak in a webhook/broadcast path = blast radius across
# NATS / HTTP / git / Discord (see secret_redact.py for the patterns mirrored).
SECRET_SCAN="$REPO_DIR/multi-fleet/scripts/secret-scan.sh"
if [[ -x "$SECRET_SCAN" ]]; then
    SECRET_ERR=$("$SECRET_SCAN" 2>&1 >/dev/null)
    SECRET_RC=$?
    SECRET_LAST=$(echo "$SECRET_ERR" | tail -1)
    if [[ "$SECRET_RC" -eq 0 ]]; then
        DETAIL=$(echo "$SECRET_LAST" | .venv/bin/python3 -c "
import json, sys
try:
    r = json.loads(sys.stdin.read())
    print(f\"0 leaks / {r.get('scanned_files','?')} files ({r.get('scanner','?')})\")
except Exception:
    print('clean')
" 2>/dev/null || echo "clean")
        check "Secret scan (multi-fleet/)" "critical" 0 "$DETAIL"
    elif [[ "$SECRET_RC" -eq 1 ]]; then
        LEAKS=$(echo "$SECRET_LAST" | .venv/bin/python3 -c "
import json, sys
try:
    print(json.loads(sys.stdin.read()).get('leaks','?'))
except Exception:
    print('?')
" 2>/dev/null || echo "?")
        # ZSF: log full reason to stderr so operators see WHAT leaked.
        echo "$SECRET_ERR" >&2
        check "Secret scan (multi-fleet/)" "critical" 1 "${LEAKS} leaked credential(s) — see scanner JSON above"
    else
        # rc=2 or unknown: scanner internal error. Surface it (ZSF), don't pass silently.
        echo "$SECRET_ERR" >&2
        check "Secret scan (multi-fleet/)" "critical" 1 "scanner exited $SECRET_RC (internal error)"
    fi
else
    check "Secret scan (multi-fleet/)" "warning" 1 "secret-scan.sh missing or not executable"
fi

# 14. 3-Surgeon probe — Cardiologist + Neurologist must be reachable.
# Why critical: any 3-surgeon-gated workflow (architectural-gate, counter-position,
# pre/post-implementation-review) silently degrades to single-model when surgeons
# are down. Catching here prevents shipping work that depends on consensus when
# the consensus pipeline is offline. Use --soft to downgrade during known outages.
#
# Race M4 (2026-04-24): added per surgeon-outage class found in fleet retros.
PROBE_SEVERITY="critical"
[[ "$SOFT_MODE" == true ]] && PROBE_SEVERITY="warning"
# Q4 fix (2026-05-06): auto-load DeepSeek key from macOS Keychain when env
# is empty. Bash tool init misses keychain, so a clean shell sees no key
# and 3s probe FAILs (false negative — key IS in keychain). Eliminates a
# whole class of false criticals on this gate.
if [ -z "${Context_DNA_Deepseek:-}" ]; then
    Context_DNA_Deepseek="$(security find-generic-password -s fleet-nerve -a Context_DNA_Deepseek -w 2>/dev/null || true)"
    if [ -n "$Context_DNA_Deepseek" ]; then
        export Context_DNA_Deepseek
        export DEEPSEEK_API_KEY="$Context_DNA_Deepseek"
    fi
fi
if command -v 3s >/dev/null 2>&1; then
    PROBE_OUT=$(3s probe 2>&1)
    PROBE_RC=$?
    if [[ "$PROBE_RC" -eq 0 ]]; then
        # Extract latency line(s) for detail (Cardiologist: OK (Xms), Neurologist: OK (Yms))
        DETAIL=$(echo "$PROBE_OUT" | grep -E "Cardiologist|Neurologist" | tr '\n' ' ' | sed 's/  */ /g' | sed 's/^ //;s/ $//')
        check "3-Surgeon probe (Cardio+Neuro)" "$PROBE_SEVERITY" 0 "${DETAIL:-all surgeons OK}"
    else
        # Surface which surgeon(s) failed
        FAILED=$(echo "$PROBE_OUT" | grep -E "FAIL|ERROR|down|unreachable|timeout" | head -2 | tr '\n' '; ' | sed 's/; $//')
        [[ -z "$FAILED" ]] && FAILED="probe rc=$PROBE_RC"
        if [[ "$SOFT_MODE" == true ]]; then
            check "3-Surgeon probe (Cardio+Neuro)" "warning" 1 "soft-mode: ${FAILED}"
        else
            check "3-Surgeon probe (Cardio+Neuro)" "critical" 1 "${FAILED}"
        fi
    fi
else
    if [[ "$SOFT_MODE" == true ]]; then
        check "3-Surgeon probe (Cardio+Neuro)" "warning" 1 "soft-mode: 3s CLI not on PATH"
    else
        check "3-Surgeon probe (Cardio+Neuro)" "critical" 1 "3s CLI not on PATH"
    fi
fi

# 15. Import-smoke — critical hot-path modules must import cleanly.
# Why critical: NameError-at-module-load bugs (e.g. anticipation_engine
# missing typing.List on 2026-04-26) silently break runtime — webhook S4
# timed out for hours before detection. Delegates to import-smoke-gate.sh.
IMPORT_SMOKE="$SCRIPT_DIR/import-smoke-gate.sh"
if [[ -x "$IMPORT_SMOKE" ]]; then
    SMOKE_OUT=$("$IMPORT_SMOKE" 2>&1)
    SMOKE_RC=$?
    if [[ "$SMOKE_RC" -eq 0 ]]; then
        # Pull the "X/Y clean" summary line for detail
        SUMMARY=$(echo "$SMOKE_OUT" | grep -E "^import-smoke:" | head -1)
        check "Import-smoke (hot-path modules)" "critical" 0 "${SUMMARY:-all modules import OK}"
    else
        # Surface failed module names — operators need to know WHICH failed
        FAILED_MODS=$(echo "$SMOKE_OUT" | grep -E "^BLOCKED:" | head -1 | sed 's/^BLOCKED: //')
        [[ -z "$FAILED_MODS" ]] && FAILED_MODS=$(echo "$SMOKE_OUT" | grep -E "^FAIL " | awk '{print $2}' | tr '\n' ' ' | sed 's/ $//')
        [[ -z "$FAILED_MODS" ]] && FAILED_MODS="rc=$SMOKE_RC"
        check "Import-smoke (hot-path modules)" "critical" 1 "failed: ${FAILED_MODS}"
    fi
else
    check "Import-smoke (hot-path modules)" "critical" 1 "import-smoke-gate.sh missing or not executable"
fi

# 16. Interlock defaults — 7/7 mf<->3s interlocks must be active on this node
# (B1 priority, 2026-05-04). Active = either set in launchd plist
# EnvironmentVariables (canonical for mac2 daemon) OR enabled by default in
# multi-fleet/multifleet/interlocks/mf_3s_bridge.py. Warning-only because
# non-mac2 nodes / dev shells legitimately run with interlocks off.
INTERLOCK_VARS=(
    FLEET_INTERLOCK_GATE_REVIEW
    FLEET_INTERLOCK_DISSENT_TO_QUORUM
    FLEET_INTERLOCK_STRESS_RISK_MODE
    FLEET_INTERLOCK_REPAIR_REVIEW
    FLEET_INTERLOCK_CHANNEL_TO_CORRIGIBILITY
    FLEET_INTERLOCK_SURGEON_EVIDENCE
    FLEET_INTERLOCK_GAINS_TO_HEALTH
)
INTERLOCK_PLIST="$HOME/Library/LaunchAgents/io.contextdna.fleet-nats.plist"
INTERLOCK_PLIST_FALLBACK="$HOME/Library/LaunchAgents/io.contextdna.fleet-nerve.plist"
INTERLOCK_MISSING=()
for v in "${INTERLOCK_VARS[@]}"; do
    in_plist=0
    for p in "$INTERLOCK_PLIST" "$INTERLOCK_PLIST_FALLBACK"; do
        [[ -f "$p" ]] && grep -q "<key>${v}</key>" "$p" 2>/dev/null && in_plist=1 && break
    done
    in_env=0
    [[ "${!v:-}" == "1" || "${!v:-}" == "true" || "${!v:-}" == "yes" || "${!v:-}" == "on" ]] && in_env=1
    if [[ "$in_plist" -eq 0 && "$in_env" -eq 0 ]]; then
        INTERLOCK_MISSING+=("$v")
    fi
done
if [[ "${#INTERLOCK_MISSING[@]}" -eq 0 ]]; then
    check "Interlocks default-on (7/7)" "warning" 0 "all 7 mf<->3s interlocks active"
else
    check "Interlocks default-on (7/7)" "warning" 1 "missing: ${INTERLOCK_MISSING[*]}"
fi

# Fleet-nerve daemon supervision: exactly one of {fleet-nats, fleet-nerve}
# must be loaded in launchctl. Both = duplicate spawns racing for port 8855.
# Canonical = io.contextdna.fleet-nats (mac2 daemon, B1 interlocks live there).
# fleet-nerve.plist exists on disk as historical fallback but must stay
# unloaded. Watchdog liveness probe (io.contextdna.fleet-nerve-watchdog)
# closes the launchd-can't-detect-hang gap.
LOADED_DAEMONS_STR="$(launchctl list 2>/dev/null | awk '$3=="io.contextdna.fleet-nats"{print "fleet-nats"} $3=="io.contextdna.fleet-nerve"{print "fleet-nerve"}' | tr '\n' ' ' | sed 's/ $//')"
LOADED_DAEMONS_COUNT="$(echo -n "$LOADED_DAEMONS_STR" | wc -w | tr -d ' ')"
if [[ "$LOADED_DAEMONS_COUNT" -eq 1 ]]; then
    check "Fleet daemon supervision (single canonical)" "critical" 0 "$LOADED_DAEMONS_STR loaded"
elif [[ "$LOADED_DAEMONS_COUNT" -eq 0 ]]; then
    check "Fleet daemon supervision (single canonical)" "critical" 1 "no fleet daemon loaded — start with: launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/io.contextdna.fleet-nats.plist"
else
    check "Fleet daemon supervision (single canonical)" "critical" 1 "DUPLICATE: $LOADED_DAEMONS_STR both loaded — bootout io.contextdna.fleet-nerve"
fi

# Fleet-nerve liveness watchdog must be loaded (catches wedged daemon that
# launchd KeepAlive cannot detect — alive PID, dead /health socket).
if launchctl list 2>/dev/null | grep -q "io.contextdna.fleet-nerve-watchdog"; then
    KICKS=$(awk '{print $2}' /tmp/fleet-nerve-watchdog.state 2>/dev/null)
    KICKS="${KICKS:-0}"
    check "Fleet-nerve liveness watchdog loaded" "warning" 0 "watchdog_kicks_total=$KICKS"
else
    check "Fleet-nerve liveness watchdog loaded" "warning" 1 "install: bash scripts/install-launchd-plists.sh fleet-nerve-watchdog"
fi

# 17. Verification-before-completion (RACE X3) — wires the superpowers skill.
# Until now the skill only surfaced as a [SUGGESTED_SKILL: ...] pointer line
# from S6/S8 (memory/synaptic_deep_voice.py) — it was a hint, not a gate.
# This check enforces it programmatically: scan staged + recent commits for
# completion vocabulary ("complete", "done", "all tests pass", "fixed", …),
# classify scope via north_star.py (V5), then RUN the actual verification
# command (pytest over derived test scope) and PASS only if exit==0.
#
# Cross-cutting priorities (Priority #2) + V2 wave wanted the skill wired
# here. Behavior:
#   - rc=0 → check passes (no claims, OR claims all verified clean)
#   - rc=1 → CRITICAL (claim made, verification failed) — the skill's
#            Iron Law: NO COMPLETION CLAIMS WITHOUT FRESH EVIDENCE
#   - rc=2 → WARNING (claims found but no test scope — exploratory work
#            permitted by the skill, surfaced for review only)
#
# Observability: appends one JSON line per run to
# logs/gains-gate-verification-${YYYY-MM-DD}.log.
VERIF_SCRIPT="$SCRIPT_DIR/gains_gate_verification.py"
VERIF_PY="$REPO_DIR/.venv/bin/python3"
[[ ! -x "$VERIF_PY" ]] && VERIF_PY="$(command -v python3 || true)"
if [[ -f "$VERIF_SCRIPT" && -n "$VERIF_PY" ]]; then
    # The python helper writes its log + prints "STATUS|DETAIL|LOGPATH" on stdout.
    VERIF_OUT=$("$VERIF_PY" "$VERIF_SCRIPT" --repo "$REPO_DIR" 2>&1)
    VERIF_RC=$?
    VERIF_LINE=$(echo "$VERIF_OUT" | tail -1)
    VERIF_STATUS=$(echo "$VERIF_LINE" | cut -d'|' -f1)
    VERIF_DETAIL=$(echo "$VERIF_LINE" | cut -d'|' -f2)
    case "$VERIF_RC" in
        0)
            check "Verification-before-completion (skill wire)" "critical" 0 "${VERIF_DETAIL:-claims verified}"
            ;;
        2)
            check "Verification-before-completion (skill wire)" "warning" 1 "${VERIF_DETAIL:-no test scope}"
            ;;
        *)
            # rc=1 (skill Iron Law fired) or unexpected — surface the helper's
            # stderr so operators see WHY (ZSF: never silently pass).
            echo "$VERIF_OUT" >&2
            check "Verification-before-completion (skill wire)" "critical" 1 "${VERIF_DETAIL:-verification failed (rc=$VERIF_RC)}"
            ;;
    esac
else
    check "Verification-before-completion (skill wire)" "warning" 1 "helper missing: scripts/gains_gate_verification.py or python3"
fi

# 18. Multi-Fleet Extraction Contract — freezes drift of forbidden imports
# (memory.* / scripts.* / tools.*) inside multi-fleet/multifleet/. Phase 1
# of the mf extraction plan ("stop the bleeding"). See
# multi-fleet/EXTRACTION-CONTRACT.md for the full contract.
EXTRACT_SCRIPT="$SCRIPT_DIR/check-mf-extraction-contract.sh"
if [[ -x "$EXTRACT_SCRIPT" ]]; then
    EXTRACT_OUT=$("$EXTRACT_SCRIPT" 2>&1)
    EXTRACT_RC=$?
    case "$EXTRACT_RC" in
        0)
            check "MF extraction contract" "critical" 0 "no new forbidden imports"
            ;;
        1)
            # Emit the offender list so operators can see WHICH new pair landed.
            echo "$EXTRACT_OUT" >&2
            check "MF extraction contract" "critical" 1 "new forbidden import detected"
            ;;
        *)
            echo "$EXTRACT_OUT" >&2
            check "MF extraction contract" "warning" 1 "scanner error (rc=$EXTRACT_RC)"
            ;;
    esac
else
    check "MF extraction contract" "warning" 1 "helper missing: scripts/check-mf-extraction-contract.sh"
fi

# 18b. Channel-cascade bypass linter (QQ2 — Gap 1 of mf channel-invariance).
# Forbids direct nc.publish() calls on peer subjects (rpc.peer.*, fleet.peer.*,
# fleet.message.*, fleet.context.*) outside the dispatcher + transport
# allowlist defined in multi-fleet/multifleet/extraction_contract.json.
#
# Companion to MFINV-C01 / CTXDNA-INV-022 (runtime invariants in
# memory/invariants.py): this is the static-analysis half — catches new
# bypass code BEFORE it ships, while the invariants catch any proposal that
# runs without traversing the cascade.
#
# ZSF: scanner is fail-safe; rc=2 (config/regex/IO error) degrades to
# warning so a busted gate cannot itself silence the fleet.
CASCADE_VENV_PY="$REPO_DIR/.venv/bin/python3"
[[ ! -x "$CASCADE_VENV_PY" ]] && CASCADE_VENV_PY="$(command -v python3 || true)"
if [[ -n "$CASCADE_VENV_PY" && -f "$REPO_DIR/multi-fleet/multifleet/contract_check.py" ]]; then
    CASCADE_OUT=$(cd "$REPO_DIR" && PYTHONPATH="$REPO_DIR/multi-fleet" "$CASCADE_VENV_PY" -m multifleet.contract_check 2>&1)
    CASCADE_RC=$?
    case "$CASCADE_RC" in
        0)
            check "MF channel-cascade bypass linter" "critical" 0 "no peer-cascade bypass detected"
            ;;
        1)
            echo "$CASCADE_OUT" >&2
            check "MF channel-cascade bypass linter" "critical" 1 "new direct nc.publish on peer subject(s)"
            ;;
        *)
            echo "$CASCADE_OUT" >&2
            check "MF channel-cascade bypass linter" "warning" 1 "scanner error (rc=$CASCADE_RC)"
            ;;
    esac
else
    check "MF channel-cascade bypass linter" "warning" 1 "helper missing: multi-fleet/multifleet/contract_check.py"
fi

# 19. ZSF bare-except gate (S5 sprint) — catches the bare-swallow anti-pattern
# (`except:` / `except Exception:` followed by `pass` or only inert literals)
# in any tracked OR newly-added .py file. Critical because the Zero Silent
# Failures invariant is constitutional — exceptions MUST be observable.
# v6 carried 7 such sites; this gate stops them from re-landing as S1/S2/S3/S4
# import the IDEAS without importing the anti-pattern. Allowlist baseline
# lives at scripts/.zsf-bare-except-allowlist.txt.
ZSF_SCRIPT="$SCRIPT_DIR/check-zsf-bare-except.sh"
if [[ -x "$ZSF_SCRIPT" ]]; then
    ZSF_OUT=$("$ZSF_SCRIPT" 2>&1)
    ZSF_RC=$?
    case "$ZSF_RC" in
        0)
            check "ZSF bare-except gate" "critical" 0 "no new bare-except violations"
            ;;
        1)
            # Emit the offender list (ZSF: surface, never silent).
            echo "$ZSF_OUT" >&2
            check "ZSF bare-except gate" "critical" 1 "new bare-except violation(s)"
            ;;
        *)
            echo "$ZSF_OUT" >&2
            check "ZSF bare-except gate" "warning" 1 "scanner error (rc=$ZSF_RC)"
            ;;
    esac
else
    check "ZSF bare-except gate" "warning" 1 "helper missing: scripts/check-zsf-bare-except.sh"
fi

# 20. Venv critical-dep import-smoke — catches silent drift between
# requirements pins and the live `.venv/` (the 2026-05-04 webhook P0 root
# cause: `nats-py` was pinned in memory/requirements-agent.txt:38 +
# multi-fleet/pyproject.toml:31 but missing from `.venv/`. Producer caught
# ImportError → `nats-unavailable` RuntimeError → events never reached
# daemon → Atlas blind ~1.5h before T5 regression smoke noticed).
#
# Each dep gets its OWN check() line (ZSF: one observable counter per
# missing dep, never a single rolled-up flag). FAIL detail includes the
# pip install command + the pin source line so operators can repair in
# one paste.
#
# Severity matrix:
#   nats / nats.js / redis / httpx → critical (moat-critical pipeline)
#   mlx_lm                          → warning (optional; non-darwin/x86_64
#                                     skipped — LLM server check #2 already
#                                     covers runtime presence on macOS arm64)
VENV_PY="$REPO_DIR/.venv/bin/python3"
if [[ ! -x "$VENV_PY" ]]; then
    check "Venv import-smoke (nats)"               "critical" 1 "no .venv/bin/python3"
    check "Venv import-smoke (nats.js JetStream)"  "critical" 1 "no .venv/bin/python3"
    check "Venv import-smoke (redis)"              "critical" 1 "no .venv/bin/python3"
    check "Venv import-smoke (httpx)"              "critical" 1 "no .venv/bin/python3"
    check "Venv import-smoke (mlx_lm)"             "warning"  1 "no .venv/bin/python3"
else
    # nats — webhook publisher → daemon round-trip.
    if "$VENV_PY" -c "import nats" >/dev/null 2>&1; then
        check "Venv import-smoke (nats)" "critical" 0 "import OK"
    else
        check "Venv import-smoke (nats)" "critical" 1 "ImportError — repair: .venv/bin/pip install nats-py (pin: memory/requirements-agent.txt:38, multi-fleet/pyproject.toml:31)"
    fi
    # nats.js — JetStream consumer for FLEET_MESSAGES + FLEET_EVENTS streams.
    # Subpackage of nats-py; if root nats imports but nats.js doesn't, the
    # wheel is broken or nats-py is too old (pre-2.x layout).
    if "$VENV_PY" -c "import nats.js" >/dev/null 2>&1; then
        check "Venv import-smoke (nats.js JetStream)" "critical" 0 "import OK"
    else
        check "Venv import-smoke (nats.js JetStream)" "critical" 1 "ImportError — repair: .venv/bin/pip install --upgrade 'nats-py>=2.14.0,<3.0' (pin: memory/requirements-agent.txt:38)"
    fi
    # redis — priority queue + gains store.
    if "$VENV_PY" -c "import redis" >/dev/null 2>&1; then
        check "Venv import-smoke (redis)" "critical" 0 "import OK"
    else
        check "Venv import-smoke (redis)" "critical" 1 "ImportError — repair: .venv/bin/pip install 'redis>=5.0.0' (pin: memory/requirements-agent.txt:20)"
    fi
    # httpx — 3-surgeon HTTP transport + DeepSeek fallback client.
    if "$VENV_PY" -c "import httpx" >/dev/null 2>&1; then
        check "Venv import-smoke (httpx)" "critical" 0 "import OK"
    else
        check "Venv import-smoke (httpx)" "critical" 1 "ImportError — repair: .venv/bin/pip install 'httpx>=0.25.0' (pin: memory/requirements-agent.txt:43, multi-fleet/pyproject.toml:51)"
    fi
    # mlx_lm — Neurologist local model. WARN-only:
    #   1. Optional; macOS arm64 only (skipped on linux/x86_64).
    #   2. `mlx_lm install` is a known Aaron-pending action — failing here
    #      would create a permanent CRITICAL until Aaron resolves it.
    #   3. LLM server health (check #2, port 5044) already covers runtime.
    UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
    UNAME_M="$(uname -m 2>/dev/null || echo unknown)"
    if [[ "$UNAME_S" != "Darwin" || "$UNAME_M" != "arm64" ]]; then
        check "Venv import-smoke (mlx_lm)" "warning" 0 "skipped (host=${UNAME_S}/${UNAME_M}, mlx_lm is macOS arm64 only)"
    elif "$VENV_PY" -c "import mlx_lm" >/dev/null 2>&1; then
        check "Venv import-smoke (mlx_lm)" "warning" 0 "import OK"
    else
        check "Venv import-smoke (mlx_lm)" "warning" 1 "ImportError — repair: .venv/bin/pip install mlx-lm (optional; LLM server check covers runtime)"
    fi
fi

# 21. ER Sim invariants (advisory) — guards the Sacred Architecture documented in
# CLAUDE-reference.md (ER SIMULATOR section) and simulator-core/er-sim-monitor/
# CLAUDE.md. The MM2 LL5 audit surfaced that gains-gate had ZERO ER-Sim
# references — a regression to a sacred file (`_ecg` suffix on canonical
# waveforms, event-driven audio via playSoundEvent, PHASE_TIMINGS 15s/45s,
# SoundManager MIN_SOUND_INTERVAL >= 2000ms) would not trip any fleet alarm.
# Wire is ADVISORY (warning-only) so a missing `node` or transient simulator
# refactor never blocks gains-gate; but a real regression now lights up.
ER_SIM_VALIDATOR="$REPO_DIR/simulator-core/er-sim-monitor/scripts/validateERSimInvariants.cjs"
if ! command -v node >/dev/null 2>&1; then
    check "ER Sim invariants (advisory)" "warning" 1 "node not installed; ER Sim invariants advisory skipped"
elif [[ ! -f "$ER_SIM_VALIDATOR" ]]; then
    check "ER Sim invariants (advisory)" "warning" 1 "validator missing: $ER_SIM_VALIDATOR"
else
    ER_SIM_OUT=$(node "$ER_SIM_VALIDATOR" 2>&1)
    ER_SIM_RC=$?
    ER_SIM_PASS_COUNT=$(echo "$ER_SIM_OUT" | grep -c '^\[PASS\]' 2>/dev/null || echo 0)
    ER_SIM_FAIL_LINES=$(echo "$ER_SIM_OUT" | grep '^\[FAIL\]' | head -3 | tr '\n' '; ' | sed 's/; $//')
    if [[ "$ER_SIM_RC" -eq 0 && "$ER_SIM_PASS_COUNT" -gt 0 ]]; then
        check "ER Sim invariants (advisory)" "warning" 0 "${ER_SIM_PASS_COUNT}/8 invariants pass"
    elif [[ "$ER_SIM_RC" -eq 0 ]]; then
        # rc=0 but no [PASS] lines parsed — surface as warning (ZSF, never silent pass)
        check "ER Sim invariants (advisory)" "warning" 1 "validator returned 0 but no invariants parsed"
    else
        # ZSF: surface failure detail to stderr so regression diagnosis is one paste away.
        echo "$ER_SIM_OUT" >&2
        check "ER Sim invariants (advisory)" "warning" 1 "ER Sim invariants — REGRESSION: ${ER_SIM_FAIL_LINES:-rc=$ER_SIM_RC}"
    fi
fi

# 22. PATH-python import-smoke (nats) — Z4 root-cause-fix sibling.
# The race-event publisher (tools/fleet_race_publisher.py) is spawned by
# the 3s brainstorm hook via `sys.executable`, which on most machines is
# whatever `python3` on PATH resolves to (typically the system / pyenv /
# brew interpreter, NOT the .venv covered by check #20). If THAT python
# lacks `nats-py`, every race-publish silently fails with reason=
# nats_py_missing. Y1's "broker not up locally" flag was actually this
# class of gap (env-mismatch between webhook and race publishers).
#
# Forward-compat: defaults to skip when `python3` on PATH is the same
# binary as $VENV_PY — avoids double-counting check #20 above.
PATH_PY="$(command -v python3 2>/dev/null || true)"
if [[ -z "$PATH_PY" ]]; then
    check "PATH python3 import-smoke (nats — race publisher)" "warning" 1 "no python3 on PATH"
elif [[ -x "$VENV_PY" && "$(readlink -f "$PATH_PY" 2>/dev/null || echo "$PATH_PY")" == "$(readlink -f "$VENV_PY" 2>/dev/null || echo "$VENV_PY")" ]]; then
    check "PATH python3 import-smoke (nats — race publisher)" "warning" 0 "skipped (PATH python3 is .venv, already covered by #20)"
elif "$PATH_PY" -c "import nats" >/dev/null 2>&1; then
    check "PATH python3 import-smoke (nats — race publisher)" "warning" 0 "import OK ($PATH_PY)"
else
    check "PATH python3 import-smoke (nats — race publisher)" "warning" 1 "ImportError — repair: $PATH_PY -m pip install 'nats-py>=2.14.0,<3.0' (race-publish silently fails with reason=nats_py_missing otherwise)"
fi

# ── Results ──────────────────────────────────────────────────────────────────

END_TIME=$(date +%s%N)
ELAPSED_MS=$(( (END_TIME - START_TIME) / 1000000 ))

echo ""
for result in "${RESULTS[@]}"; do
    echo -e "  $result"
done

echo ""
echo -e "${BOLD}───────────────────────────────────────────${NC}"
echo -e "  Checks: ${TOTAL_CHECKS} | ${GREEN}Passed${NC}: $((TOTAL_CHECKS - CRITICAL_FAILURES - WARNINGS)) | ${RED}Critical${NC}: ${CRITICAL_FAILURES} | ${YELLOW}Warnings${NC}: ${WARNINGS}"
echo -e "  Time: ${ELAPSED_MS}ms"

if [[ "$CRITICAL_FAILURES" -gt 0 ]]; then
    echo ""
    echo -e "  ${RED}${BOLD}GATE: BLOCKED${NC} — ${CRITICAL_FAILURES} critical failure(s). Fix before proceeding to next phase."
    echo ""
    exit 1
else
    echo ""
    echo -e "  ${GREEN}${BOLD}GATE: PASSED${NC} — All critical checks OK. Safe to proceed to next phase."
    if [[ "$WARNINGS" -gt 0 ]]; then
        echo -e "  ${YELLOW}(${WARNINGS} non-critical warnings — review but don't block)${NC}"
    fi
    echo ""
    exit 0
fi
