#!/usr/bin/env bash
# multi-fleet-status.sh — Live Multi-Fleet dashboard for Claude Code UI
#
# Shows: node health, active branch/worktree, surgeon status, last activity
#
# Usage:
#   ./scripts/multi-fleet-status.sh           # one-shot status
#   ./scripts/multi-fleet-status.sh --watch   # refresh every 10s
#   ./scripts/multi-fleet-status.sh --json    # machine-readable output

CONF="${BASH_SOURCE%/*}/3s-network.local.conf"
if [[ ! -f "$CONF" ]]; then
    echo "ERROR: $CONF not found. Copy 3s-network.conf.example and fill in IPs."
    exit 1
fi
source "$CONF"

REPO="$HOME/dev/er-simulator-superrepo"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=6 -o BatchMode=yes"

# ── Colors ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
CYAN='\033[0;36m'; MAGENTA='\033[0;35m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

peer_host() { eval echo "\$PEER_$1"; }

# ── Gather node info via SSH ───────────────────────────────────────────────────
gather_node_info() {
    local node="$1"
    local host; host=$(peer_host "$node")
    local ip; ip=$(echo "$host" | cut -d@ -f2)
    local local_ip; local_ip=$(ipconfig getifaddr en0 2>/dev/null || echo "")

    local cmd='
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
source ~/.zshrc 2>/dev/null
REPO="$HOME/dev/er-simulator-superrepo"
export PATH="$REPO/venv.nosync/bin:$HOME/.local/bin:$PATH"

# Machine identity
HOSTNAME_VAL=$(hostname -s 2>/dev/null || echo "unknown")
UPTIME_VAL=$(uptime | sed "s/.*up //; s/, load.*//" | xargs)

# Git state in repo
cd "$REPO" 2>/dev/null || { echo "BRANCH=N/A|COMMIT=N/A|DIRTY=N/A|WORKTREES=0"; exit; }
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "N/A")
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "N/A")
DIRTY=$(git status --porcelain 2>/dev/null | wc -l | xargs)
WORKTREES=$(git worktree list 2>/dev/null | wc -l | xargs)

# 3s health (quick — just check if binary works, skip API call)
if 3s --help &>/dev/null; then
    SURGEONS_BIN="ok"
else
    SURGEONS_BIN="missing"
fi

# Active Claude Code / agents (quick check)
AGENTS=$(pgrep -c -f "claude" 2>/dev/null || echo 0)

# Last git activity
LAST_COMMIT_MSG=$(git log -1 --format="%s" 2>/dev/null | cut -c1-60 || echo "N/A")
LAST_COMMIT_TIME=$(git log -1 --format="%ar" 2>/dev/null || echo "N/A")

# Chief marker
IS_CHIEF="no"
[[ "$(hostname -s)" == "$(echo $CHIEF 2>/dev/null)" ]] && IS_CHIEF="yes" || true

echo "HOSTNAME=$HOSTNAME_VAL|BRANCH=$BRANCH|COMMIT=$COMMIT|DIRTY=$DIRTY|WORKTREES=$WORKTREES|SURGEONS=$SURGEONS_BIN|AGENTS=$AGENTS|LAST_MSG=$LAST_COMMIT_MSG|LAST_TIME=$LAST_COMMIT_TIME|UPTIME=$UPTIME_VAL"
'

    if [[ "$ip" == "$local_ip" ]]; then
        eval "$cmd" 2>/dev/null
    else
        ssh $SSH_OPTS "$host" "$cmd" 2>/dev/null
    fi
}

# ── Parse info string ─────────────────────────────────────────────────────────
parse_field() {
    local str="$1" field="$2"
    echo "$str" | tr '|' '\n' | grep "^$field=" | cut -d= -f2-
}

# ── Render dashboard ──────────────────────────────────────────────────────────
render_dashboard() {
    local TIMESTAMP; TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

    echo ""
    echo -e "${BOLD}${BLUE}╔══════════════════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}${BLUE}║           ContextDNA Multi-Fleet Dashboard                  ║${RESET}"
    echo -e "${BOLD}${BLUE}║  $(printf '%-52s' "$TIMESTAMP")  ║${RESET}"
    echo -e "${BOLD}${BLUE}╠══════════════════════════════════════════════════════════════╣${RESET}"

    local all_json="["
    local first=1

    for node in $PEER_NAMES; do
        local host; host=$(peer_host "$node")
        local is_chief=""
        [[ "$node" == "$CHIEF" ]] && is_chief=" ${MAGENTA}★ CHIEF${RESET}"

        echo -e "${BOLD}${BLUE}║${RESET}"
        echo -ne "${BOLD}${BLUE}║${RESET}  ${BOLD}$(printf '%-6s' "$node")${RESET}  ${DIM}$host${RESET}${is_chief}"
        echo ""

        # Gather with timeout indicator
        echo -ne "${BOLD}${BLUE}║${RESET}  ${DIM}gathering...${RESET}\r"
        local info
        info=$(gather_node_info "$node" 2>/dev/null)

        if [[ -z "$info" ]]; then
            echo -e "${BOLD}${BLUE}║${RESET}  ${RED}✗ UNREACHABLE${RESET}                                                "
            echo -e "${BOLD}${BLUE}║${RESET}"
            continue
        fi

        local hostname; hostname=$(parse_field "$info" "HOSTNAME")
        local branch;   branch=$(parse_field "$info" "BRANCH")
        local commit;   commit=$(parse_field "$info" "COMMIT")
        local dirty;    dirty=$(parse_field "$info" "DIRTY")
        local worktrees; worktrees=$(parse_field "$info" "WORKTREES")
        local surgeons; surgeons=$(parse_field "$info" "SURGEONS")
        local agents;   agents=$(parse_field "$info" "AGENTS")
        local last_msg; last_msg=$(parse_field "$info" "LAST_MSG")
        local last_time; last_time=$(parse_field "$info" "LAST_TIME")
        local uptime_val; uptime_val=$(parse_field "$info" "UPTIME")

        # Status dots
        local surgeon_dot="${GREEN}●${RESET}"
        [[ "$surgeons" != "ok" ]] && surgeon_dot="${RED}●${RESET}"

        local dirty_indicator=""
        [[ "$dirty" -gt 0 ]] 2>/dev/null && dirty_indicator="${YELLOW} (~$dirty dirty)${RESET}"

        local agent_indicator="${DIM}no agents${RESET}"
        [[ "$agents" -gt 0 ]] 2>/dev/null && agent_indicator="${GREEN}${agents} agent(s) active${RESET}"

        # Render node card
        echo -e "${BOLD}${BLUE}║${RESET}  ${GREEN}✓ ONLINE${RESET}  ${DIM}up: $uptime_val${RESET}                                       "
        echo -e "${BOLD}${BLUE}║${RESET}  ${BOLD}Branch:${RESET}    ${CYAN}$branch${RESET} @ ${DIM}$commit${RESET}${dirty_indicator}"
        echo -e "${BOLD}${BLUE}║${RESET}  ${BOLD}Worktrees:${RESET} $worktrees active"
        echo -e "${BOLD}${BLUE}║${RESET}  ${BOLD}3-Surgeons:${RESET} ${surgeon_dot} ${surgeons}"
        echo -e "${BOLD}${BLUE}║${RESET}  ${BOLD}Agents:${RESET}    ${agent_indicator}"
        echo -e "${BOLD}${BLUE}║${RESET}  ${BOLD}Last commit:${RESET} ${DIM}\"$last_msg\" ($last_time)${RESET}"
        echo -e "${BOLD}${BLUE}║${RESET}"

        # Build JSON entry
        [[ "$first" -eq 0 ]] && all_json+=","
        all_json+="{\"node\":\"$node\",\"host\":\"$host\",\"branch\":\"$branch\",\"commit\":\"$commit\",\"dirty\":$dirty,\"surgeons\":\"$surgeons\",\"agents\":$agents,\"last_commit\":\"$last_msg\",\"last_time\":\"$last_time\"}"
        first=0
    done

    echo -e "${BOLD}${BLUE}╠══════════════════════════════════════════════════════════════╣${RESET}"
    echo -e "${BOLD}${BLUE}║  Chief: ${MAGENTA}$CHIEF${RESET} ($(peer_host $CHIEF))$(printf '%*s' 25)${BOLD}${BLUE}║${RESET}"
    echo -e "${BOLD}${BLUE}║  Run a swarm query: ${CYAN}./scripts/3s-network.sh \"your question\"${RESET}$(printf '%*s' 3)${BOLD}${BLUE}║${RESET}"
    echo -e "${BOLD}${BLUE}╚══════════════════════════════════════════════════════════════╝${RESET}"
    echo ""

    all_json+="]"
    # Write machine-readable state for other tools to consume
    echo "$all_json" > /tmp/multi-fleet-state.json
}

# ── JSON mode ─────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--json" ]]; then
    result="["
    first=1
    for node in $PEER_NAMES; do
        info=$(gather_node_info "$node" 2>/dev/null)
        [[ "$first" -eq 0 ]] && result+=","
        if [[ -z "$info" ]]; then
            result+="{\"node\":\"$node\",\"status\":\"unreachable\"}"
        else
            branch=$(parse_field "$info" "BRANCH")
            commit=$(parse_field "$info" "COMMIT")
            surgeons=$(parse_field "$info" "SURGEONS")
            result+="{\"node\":\"$node\",\"status\":\"online\",\"branch\":\"$branch\",\"commit\":\"$commit\",\"surgeons\":\"$surgeons\"}"
        fi
        first=0
    done
    result+="]"
    echo "$result"
    exit 0
fi

# ── Watch mode ────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--watch" ]]; then
    INTERVAL="${2:-10}"
    echo -e "${CYAN}[multi-fleet]${RESET} Watch mode — refreshing every ${INTERVAL}s. Ctrl+C to stop."
    while true; do
        clear
        render_dashboard
        sleep "$INTERVAL"
    done
fi

# ── One-shot ──────────────────────────────────────────────────────────────────
render_dashboard
