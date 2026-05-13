#!/bin/bash
# ============================================================================
# CARDIO GATE — Gains-Preserving Critical Response
# ============================================================================
# Event-triggered by cardiologist when critical quality degradation detected.
# Like frequent EKG reviews — immediate drift correction, not scheduled polling.
#
# Pipeline: gains-gate.sh MUST pass → cardio-review cross-exam → results to WAL
# Nothing automatic proceeds without gains verification first.
#
# Usage:
#   ./scripts/cardio-gate.sh              # Auto-triggered by scheduler
#   ./scripts/cardio-gate.sh --manual     # Manual trigger (skips rate limit)
#   ./scripts/cardio-gate.sh --dry-run    # Check state without executing
#
# Exit: 0 = pipeline completed, 1 = gains gate blocked, 2 = rate limited
# ============================================================================

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

MANUAL=false
DRY_RUN=false
[[ "${1:-}" == "--manual" ]] && MANUAL=true
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

LOG_DIR="/tmp/atlas-agent-results"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/cardio_gate_$(date +%Y%m%d_%H%M%S).log"

log() { echo -e "[$(date +%H:%M:%S)] $1" | tee -a "$LOG_FILE"; }

# ── Rate Limit (max 3/hour unless manual) ──────────────────────────────────

if [[ "$MANUAL" == false && "$DRY_RUN" == false ]]; then
    RATE_COUNT=$(PYTHONPATH=. .venv/bin/python3 -c "
import redis
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_timeout=2)
key = 'quality:cardio_gate_count'
count = r.incr(key)
if count == 1:
    r.expire(key, 3600)  # 1h window
print(count)
" 2>/dev/null || echo "0")

    if [[ "$RATE_COUNT" -gt 3 ]]; then
        log "${YELLOW}Rate limited: ${RATE_COUNT}/3 per hour. Use --manual to override.${NC}"
        exit 2
    fi
    log "${CYAN}Cardio gate invocation ${RATE_COUNT}/3 this hour${NC}"
fi

# ── Step 1: Check for critical findings ────────────────────────────────────

log "${BOLD}${CYAN}═══ CARDIO GATE — Critical Response Pipeline ═══${NC}"

CRIT_COUNT=$(PYTHONPATH=. .venv/bin/python3 -c "
import redis, json
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_timeout=2)
notifications = r.lrange('quality:critical_notifications', 0, -1)
count = len(notifications)
if not count:
    # Check cardiologist_findings for criticals
    for raw in r.lrange('quality:cardiologist_findings', 0, 19):
        try:
            f = json.loads(raw)
            if f.get('severity') == 'critical':
                count += 1
        except Exception:
            pass
print(count)
" 2>/dev/null || echo "0")

if [[ "$CRIT_COUNT" == "0" ]]; then
    log "${GREEN}No critical findings — nothing to review${NC}"
    exit 0
fi

log "Found ${RED}${CRIT_COUNT} critical finding(s)${NC} requiring cross-examination"

if [[ "$DRY_RUN" == true ]]; then
    log "${YELLOW}DRY RUN — would run gains gate then cardio-review${NC}"
    exit 0
fi

# ── Step 2: Gains Gate (MUST pass before any automated action) ─────────────

log ""
log "${BOLD}Step 1/2: Verifying gains are preserved...${NC}"

# --cardio: exclude cardiologist criticals from gate (cross-exam IS the response)
if ! "$SCRIPT_DIR/gains-gate.sh" --cardio >> "$LOG_FILE" 2>&1; then
    log ""
    log "${RED}${BOLD}GAINS GATE BLOCKED${NC} — Critical failures detected."
    log "Automated cross-exam ABORTED to prevent compounding drift."
    log "Fix gains gate failures first, then: ${CYAN}./scripts/cardio-gate.sh --manual${NC}"

    # Notify Redis that gate blocked (Atlas can pick this up)
    PYTHONPATH=. .venv/bin/python3 -c "
import redis
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_timeout=2)
r.setex('quality:cardio_gate_blocked', 3600, 'gains_gate_failed')
" 2>/dev/null || true

    exit 1
fi

log "${GREEN}Gains gate passed — safe to proceed${NC}"

# ── Step 3: Run 3-surgeon cross-examination ────────────────────────────────

log ""
log "${BOLD}Step 2/2: Running 3-surgeon cross-examination...${NC}"

# REVERSIBILITY: old subprocess path — uncomment to revert
# PYTHONPATH=. .venv/bin/python3 scripts/surgery-team.py cardio-review "quality degradation" >> "$LOG_FILE" 2>&1
# New: route through 3s CLI (plugin or standalone)
if command -v 3s &>/dev/null; then
    3s cardio-review "quality degradation" >> "$LOG_FILE" 2>&1
else
    # Fallback if 3s CLI not on PATH
    PYTHONPATH=. .venv/bin/python3 scripts/surgery-team.py cardio-review "quality degradation" >> "$LOG_FILE" 2>&1
fi
REVIEW_EXIT=$?

if [[ "$REVIEW_EXIT" -eq 0 ]]; then
    log ""
    log "${GREEN}${BOLD}CARDIO GATE COMPLETE${NC} — Cross-exam results in Redis + WAL"
    log "  Results: ${CYAN}quality:cross_exam_result${NC} (Redis, 24h TTL)"
    log "  Log: ${CYAN}${LOG_FILE}${NC}"

    # Clear the new_critical flag — this event has been processed
    PYTHONPATH=. .venv/bin/python3 -c "
import redis
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_timeout=2)
r.delete('quality:new_critical')
r.delete('quality:cardio_gate_blocked')
" 2>/dev/null || true

else
    log "${YELLOW}Cross-exam exited with code ${REVIEW_EXIT} — check log${NC}"
fi

exit 0
