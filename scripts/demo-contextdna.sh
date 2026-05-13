#!/usr/bin/env bash
# demo-contextdna.sh — See Context DNA IDE intelligence in 60 seconds
# Usage: bash scripts/demo-contextdna.sh [--dry-run]
set -uo pipefail

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
    esac
done

# Colors
R='\033[0;31m' G='\033[0;32m' Y='\033[0;33m'
C='\033[0;36m' W='\033[1;37m' D='\033[0;90m' M='\033[0;35m'
BOLD='\033[1m' RST='\033[0m'

banner()  { printf "\n${M}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}\n"; }
section() { printf "\n${BOLD}${C}▸ %s${RST}\n" "$1"; }
ok()      { printf "  ${G}✓${RST} %s\n" "$1"; }
warn()    { printf "  ${Y}⚠${RST} %s\n" "$1"; }
fail()    { printf "  ${R}✗${RST} %s\n" "$1"; }
info()    { printf "  ${D}│${RST} %s\n" "$1"; }
stream()  { while IFS= read -r line; do printf "  ${D}│${RST} %s\n" "$line"; done; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"; cd "$REPO"

# Portable timeout: prefer GNU timeout/gtimeout, fall back to perl-based
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_CMD="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_CMD="gtimeout"
else
    # Shell-based fallback for macOS without coreutils
    _timeout() {
        local secs="$1"; shift
        "$@" &
        local pid=$!
        ( sleep "$secs" && kill "$pid" 2>/dev/null ) &
        local watcher=$!
        wait "$pid" 2>/dev/null
        local ret=$?
        kill "$watcher" 2>/dev/null
        wait "$watcher" 2>/dev/null
        return $ret
    }
    TIMEOUT_CMD="_timeout"
fi
PORT="${FLEET_NERVE_PORT:-8855}"

banner
printf "${BOLD}${W}  CONTEXT DNA IDE — Live Intelligence Demo${RST}\n"
printf "${D}  3 LLMs × Fleet Coordination × Real-time Dashboard${RST}\n"
banner

if [ "$DRY_RUN" = "1" ]; then
    printf "${BOLD}${Y}  DRY RUN — scenario timeline (no commands executed)${RST}\n\n"
    printf "  ${C}Phase 1${RST}  Prerequisites            ${D}~5s${RST}\n"
    printf "  ${D}         Check fleet daemon on :${PORT}${RST}\n"
    printf "  ${D}         Check 3s CLI + surgeon availability${RST}\n"
    printf "  ${D}         Check Python venv${RST}\n\n"
    printf "  ${C}Phase 2${RST}  Fleet Health               ${D}~10s${RST}\n"
    printf "  ${D}         GET /health → node, peers, NATS status${RST}\n"
    printf "  ${D}         GET /dashboard → fleet overview${RST}\n\n"
    printf "  ${C}Phase 3${RST}  3-Surgeon Cross-Examination ${D}~20s${RST}\n"
    printf "  ${D}         3s consult (Atlas + Cardiologist)${RST}\n"
    printf "  ${D}         Requires OPENAI_API_KEY${RST}\n\n"
    printf "  ${C}Phase 4${RST}  Intelligence Probes         ${D}~10s${RST}\n"
    printf "  ${D}         3s probe (all surgeons)${RST}\n"
    printf "  ${D}         gains-gate.sh (10 infra checks)${RST}\n\n"
    printf "  ${C}Phase 5${RST}  Theatrical Dashboard        ${D}~5s${RST}\n"
    printf "  ${D}         Check dashboard on :8856${RST}\n"
    printf "  ${D}         Open in browser if available${RST}\n\n"
    printf "  ${C}Phase 6${RST}  Summary                     ${D}<1s${RST}\n"
    printf "  ${D}         Module/test/message counts${RST}\n"
    banner
    printf "${BOLD}${G}  Total estimated time: ~50s${RST}\n"
    banner
    exit 0
fi

# 1. PREREQUISITES (5s)
section "Prerequisites"
if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    ok "Fleet daemon running on :${PORT}"; FLEET_UP=1
else
    warn "Fleet daemon offline"; FLEET_UP=0
    info "Start: NATS_URL=\"nats://127.0.0.1:4222\" python3 tools/fleet_nerve_nats.py serve"
fi

if command -v 3s >/dev/null 2>&1; then
    PROBE=$(3s probe 2>&1 || true)
    echo "$PROBE" | grep -qi "reachable\|ok\|success\|available" \
        && ok "3-Surgeon team available (Atlas + Cardiologist + Neurologist)" \
        || warn "3-Surgeons partially available"
else
    fail "3s CLI not found — install 3-surgeons plugin"
fi
[ -f "$REPO/.venv/bin/python3" ] && ok "Python venv active" || warn "No .venv found"

# 2. FLEET STATUS (10s)
section "Fleet Health"
if [ "$FLEET_UP" = "1" ]; then
    H=$(curl -sf "http://127.0.0.1:${PORT}/health" 2>/dev/null || echo "{}")
    pj() { echo "$H" | python3 -c "import sys,json; d=json.load(sys.stdin); print($1)" 2>/dev/null || echo "$2"; }
    NODE=$(pj "d.get('node_id','unknown')" "unknown")
    PEERS=$(pj "len(d.get('peers',[])) if isinstance(d.get('peers'),list) else d.get('peer_count',0)" "0")
    NATS_ST=$(pj "d.get('nats','unknown')" "unknown")
    ok "Node: ${BOLD}${NODE}${RST}  |  Peers: ${BOLD}${PEERS}${RST}  |  NATS: ${BOLD}${NATS_ST}${RST}"
    DASH=$(curl -sf "http://127.0.0.1:${PORT}/dashboard" 2>/dev/null || true)
    [ -n "$DASH" ] && printf "\n${D}%s${RST}\n" "$DASH"
else
    info "Skipping — fleet daemon not running"
fi

# 3. CROSS-EXAMINATION (20s)
section "3-Surgeon Cross-Examination"
printf "  ${D}Consulting Atlas (Claude) + Cardiologist (GPT-4.1-mini) ...${RST}\n"
if command -v 3s >/dev/null 2>&1; then
    # Load API key from keychain if needed
    if command -v security >/dev/null 2>&1; then
        OPENAI_API_KEY="${OPENAI_API_KEY:-$(security find-generic-password -s "openai-api-key" -w 2>/dev/null || true)}"
        export OPENAI_API_KEY
    fi
    OUT=$($TIMEOUT_CMD 30 3s consult "What is the single highest-impact improvement for a multi-LLM IDE intelligence system?" 2>&1 || true)
    if [ -n "$OUT" ]; then
        echo "$OUT" | head -20 | stream
        LINES=$(echo "$OUT" | wc -l | tr -d ' ')
        [ "$LINES" -gt 20 ] && info "(${LINES} total lines — first 20 shown)"
    else
        warn "Timed out — ensure OPENAI_API_KEY is set"
    fi
else
    info "Skipping — 3s CLI not available"
fi

# 4. PROBES + GAINS GATE (10s)
section "Intelligence Probes"
if command -v 3s >/dev/null 2>&1; then
    $TIMEOUT_CMD 15 3s probe 2>&1 | stream || true
fi
if [ -x "$REPO/scripts/gains-gate.sh" ]; then
    printf "\n"; section "Gains Gate (Infrastructure Health)"
    $TIMEOUT_CMD 30 bash "$REPO/scripts/gains-gate.sh" 2>&1 \
        | grep -E "PASS|FAIL|✓|✗|OK|WARN" | head -12 | stream || true
fi

# 5. DASHBOARD (5s)
section "Theatrical Dashboard"
DASH_PORT=8856
if curl -sf "http://127.0.0.1:${DASH_PORT}/" >/dev/null 2>&1; then
    ok "Dashboard running on :${DASH_PORT}"
    command -v open >/dev/null 2>&1 && open "http://127.0.0.1:${DASH_PORT}/dashboard/components" 2>/dev/null && ok "Opened in browser"
    info "9 real-time intelligence components visualized"
else
    warn "Dashboard not running on :${DASH_PORT}"
fi

# 6. SUMMARY
banner
MOD_COUNT=$(find "$REPO/memory" "$REPO/tools" "$REPO/scripts" \( -name "*.py" -o -name "*.sh" \) 2>/dev/null | wc -l | tr -d ' ')
TEST_COUNT=$(find "$REPO" -path "*/tests/test_*.py" 2>/dev/null | wc -l | tr -d ' ')
MSG_COUNT=$(find "$REPO/.fleet-messages" -name "*.md" 2>/dev/null | wc -l | tr -d ' ')

printf "${BOLD}${W}  Context DNA IDE — Summary${RST}\n\n"
printf "  ${C}Modules${RST}      ${BOLD}%-4s${RST} scripts & tools\n" "$MOD_COUNT"
printf "  ${C}Tests${RST}        ${BOLD}%-4s${RST} test files\n" "$TEST_COUNT"
printf "  ${C}Fleet Msgs${RST}   ${BOLD}%-4s${RST} coordination messages\n" "$MSG_COUNT"
printf "  ${C}Channels${RST}     ${BOLD}7${RST}    priority levels (NATS → Git)\n"
printf "  ${C}Surgeons${RST}     ${BOLD}3${RST}    LLMs (Claude + GPT-4.1 + Qwen3-4B)\n"
printf "  ${C}Sections${RST}     ${BOLD}9${RST}    webhook intelligence sections\n"
banner
printf "${BOLD}${G}  This is Context DNA IDE — intelligence you can see.${RST}\n"
banner
