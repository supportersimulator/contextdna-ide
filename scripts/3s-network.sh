#!/usr/bin/env bash
# 3s-network.sh — Multi-machine 3-Surgeons coordinator over SSH
#
# Usage:
#   ./scripts/3s-network.sh "your question"
#   ./scripts/3s-network.sh --probe
#
# Peer registry — loaded from local config (never committed)
# Copy scripts/3s-network.conf.example to scripts/3s-network.local.conf and fill in your values.

CONF="${BASH_SOURCE%/*}/3s-network.local.conf"
if [[ ! -f "$CONF" ]]; then
    echo "ERROR: Peer config not found: $CONF"
    echo "Copy scripts/3s-network.conf.example to scripts/3s-network.local.conf and fill in your IPs."
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

CHIEF="${CHIEF:-mac1}"
REPO="$HOME/dev/er-simulator-superrepo"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=yes"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

log()  { echo -e "${CYAN}[3s-net]${RESET} $*"; }
ok()   { echo -e "${GREEN}[✓]${RESET} $*"; }
warn() { echo -e "${YELLOW}[!]${RESET} $*"; }
err()  { echo -e "${RED}[✗]${RESET} $*"; }

peer_host() { eval echo "\$PEER_$1"; }

LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "")

peer_ssh() {
    local node="$1"; shift
    local host; host=$(peer_host "$node")
    local ip; ip=$(echo "$host" | cut -d@ -f2)
    # Run locally if this peer is the current machine
    if [[ "$ip" == "$LOCAL_IP" ]]; then
        source ~/.zshrc 2>/dev/null
        export PATH="$REPO/venv.nosync/bin:$PATH"
        cd "$REPO" && eval "$*"
    else
        ssh $SSH_OPTS "$host" \
            "source ~/.zshrc 2>/dev/null; export PATH=\"$REPO/venv.nosync/bin:\$PATH\"; cd $REPO && $*"
    fi
}

# ── Probe mode ────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--probe" ]]; then
    log "Probing all peers..."
    for node in $PEER_NAMES; do
        host=$(peer_host "$node")
        if peer_ssh "$node" "3s probe 2>&1 | grep -q 'All surgeons operational'" 2>/dev/null; then
            ok "$node ($host) — surgeons operational"
        else
            # Try just SSH connectivity
            if ssh $SSH_OPTS "$host" "echo ok" &>/dev/null; then
                warn "$node ($host) — SSH ok but 3s not operational"
            else
                err "$node ($host) — unreachable"
            fi
        fi
    done
    exit 0
fi

# ── Parse question ────────────────────────────────────────────────────────────
QUESTION="${1:-}"
if [[ -z "$QUESTION" ]]; then
    echo "Usage: $0 \"your question\""
    echo "       $0 --probe"
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RESULTS_DIR="/tmp/3s-network-$TIMESTAMP"
mkdir -p "$RESULTS_DIR"

echo ""
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  3s Network — $TIMESTAMP${RESET}"
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
log "Question: $QUESTION"
log "Chief:    $CHIEF ($(peer_host $CHIEF))"
log "Peers:    $PEER_NAMES"
log "Results:  $RESULTS_DIR"
echo ""

# ── Phase 1: Fire local surgery on all peers in parallel ─────────────────────
log "Phase 1 — Dispatching to all peers (parallel)..."

for node in $PEER_NAMES; do
    outfile="$RESULTS_DIR/$node.txt"
    log "  → $node: starting cross-exam..."
    peer_ssh "$node" "3s cross-exam '$QUESTION' 2>&1" > "$outfile" 2>&1 &
    eval "PID_$node=$!"
done

# ── Wait for all ──────────────────────────────────────────────────────────────
echo ""
log "Phase 1 — Waiting for local surgeries to complete (this takes 30-120s)..."

for node in $PEER_NAMES; do
    pid_var="PID_$node"
    pid="${!pid_var}"
    if wait "$pid" 2>/dev/null; then
        ok "$node — complete"
        eval "STATUS_$node=ok"
    else
        warn "$node — failed or timed out"
        eval "STATUS_$node=failed"
    fi
done

# ── Phase 2: Show local verdicts ──────────────────────────────────────────────
echo ""
log "Phase 2 — Local verdicts:"

for node in $PEER_NAMES; do
    status_var="STATUS_$node"
    echo ""
    echo -e "${BOLD}══ $node (${!status_var}) ══${RESET}"
    cat "$RESULTS_DIR/$node.txt" 2>/dev/null || warn "No output"
done

# ── Phase 3: Package and ship to Chief ───────────────────────────────────────
echo ""
log "Phase 3 — Shipping verdicts to Chief ($CHIEF)..."

CONTEXT_FILE="$RESULTS_DIR/combined-verdicts.txt"
{
    echo "# 3s Network — Combined Local Verdicts"
    echo "# Question: $QUESTION"
    echo "# Timestamp: $TIMESTAMP"
    echo ""
    for node in $PEER_NAMES; do
        status_var="STATUS_$node"
        echo "## $node (${!status_var})"
        echo ""
        cat "$RESULTS_DIR/$node.txt" 2>/dev/null || echo "(no output)"
        echo ""
        echo "---"
        echo ""
    done
} > "$CONTEXT_FILE"

CHIEF_HOST=$(peer_host "$CHIEF")
REMOTE_CONTEXT="/tmp/3s-verdicts-$TIMESTAMP.txt"
scp $SSH_OPTS "$CONTEXT_FILE" "$CHIEF_HOST:$REMOTE_CONTEXT" 2>/dev/null \
    && ok "Verdicts delivered to Chief ($CHIEF_HOST)" \
    || warn "SCP to Chief failed — verdicts available locally at $CONTEXT_FILE"

# ── Phase 4: Chief synthesis ──────────────────────────────────────────────────
echo ""
log "Phase 4 — Chief synthesis ($CHIEF)..."
echo ""
echo -e "${BOLD}══ CHIEF SYNTHESIS ($CHIEF) ══${RESET}"
echo ""

SYNTHESIS="You are the Chief Surgeon synthesizing verdicts from 3 independent surgical teams on this question: '$QUESTION'. Synthesize into: (1) recommended approach, (2) strongest alternative, (3) key disagreements, (4) next action. See $REMOTE_CONTEXT for full verdicts."

peer_ssh "$CHIEF" "3s cross-exam '$SYNTHESIS' 2>&1" \
    | tee "$RESULTS_DIR/chief-synthesis.txt"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
log "Done. Results: $RESULTS_DIR"
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo ""
