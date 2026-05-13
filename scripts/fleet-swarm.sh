#!/usr/bin/env bash
# fleet-swarm.sh — Theatrical Multi-Fleet orchestrator with live commentary
#
# Like 3s-network.sh but for fleet-wide task coordination.
# Shows live progress from all nodes as they work in parallel.
#
# Usage:
#   ./scripts/fleet-swarm.sh "your task description"
#   ./scripts/fleet-swarm.sh --status
#   ./scripts/fleet-swarm.sh --tasks
#   ./scripts/fleet-swarm.sh --dry-run "your task description"
#   ./scripts/fleet-swarm.sh --auto docs/plans/some-plan.md

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$REPO_ROOT/scripts/3s-network.local.conf"
PYTHON="${PYTHON:-python3}"
SEND="$PYTHON $REPO_ROOT/tools/fleet_nerve_send.py"

[[ -f "$CONF" ]] && source "$CONF"
CHIEF="${CHIEF:-mac1}"
PEER_NAMES="${PEER_NAMES:-mac1 mac2 mac3}"

# ── Colours & Symbols ────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
CYAN='\033[0;36m'; MAGENTA='\033[0;35m'; BOLD='\033[1m'; DIM='\033[2m'
RESET='\033[0m'; BG_BLUE='\033[44m'; BG_GREEN='\033[42m'; BG_RED='\033[41m'
WHITE='\033[1;37m'

# Node colours (each machine gets its own)
declare -A NODE_COLOR
NODE_COLOR[mac1]="$MAGENTA"
NODE_COLOR[mac2]="$CYAN"
NODE_COLOR[mac3]="$GREEN"

_banner() {
    echo ""
    echo -e "${BOLD}${BG_BLUE}                                                        ${RESET}"
    echo -e "${BOLD}${BG_BLUE}   ⚡ MULTI-FLEET SWARM — $(date '+%H:%M:%S')                      ${RESET}"
    echo -e "${BOLD}${BG_BLUE}                                                        ${RESET}"
    echo ""
}

_node()  { local c="${NODE_COLOR[$1]:-$CYAN}"; echo -e "${c}${BOLD}[$1]${RESET}"; }
_phase() { echo -e "\n${BOLD}${YELLOW}━━━ Phase $1: $2 ━━━${RESET}\n"; }
_live()  { echo -e "${DIM}$(date '+%H:%M:%S')${RESET} $*"; }
_ok()    { echo -e "  ${GREEN}✓${RESET} $*"; }
_fail()  { echo -e "  ${RED}✗${RESET} $*"; }
_wait()  { echo -e "  ${YELLOW}◌${RESET} $*"; }
_spark() { echo -e "  ${MAGENTA}⚡${RESET} $*"; }
_info()  { echo -e "  ${BLUE}i${RESET} $*"; }

# ── Mode flags ───────────────────────────────────────────────────────────────
DRY_RUN=false
AUTO_MODE=false
AUTO_PLAN_FILE=""

# ── Quick commands ────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--status" ]]; then
    _banner
    _phase 0 "Fleet Status"
    $SEND --status
    exit 0
fi

if [[ "${1:-}" == "--tasks" ]]; then
    _banner
    _phase 0 "Active Task Agents"
    $SEND --tasks
    exit 0
fi

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    shift
fi

if [[ "${1:-}" == "--auto" ]]; then
    AUTO_MODE=true
    AUTO_PLAN_FILE="${2:-}"
    if [[ -z "$AUTO_PLAN_FILE" ]]; then
        echo -e "${RED}--auto requires a plan file path. Usage: fleet-swarm.sh --auto docs/plans/plan.md${RESET}"
        exit 1
    fi
    if [[ ! -f "$AUTO_PLAN_FILE" && ! -f "$REPO_ROOT/$AUTO_PLAN_FILE" ]]; then
        echo -e "${RED}Plan file not found: $AUTO_PLAN_FILE${RESET}"
        exit 1
    fi
    # Resolve to absolute path
    [[ ! -f "$AUTO_PLAN_FILE" ]] && AUTO_PLAN_FILE="$REPO_ROOT/$AUTO_PLAN_FILE"
    shift 2
fi

# ── Helpers ──────────────────────────────────────────────────────────────────

# Resolve peer IP from name
_peer_ip() {
    local name="$1"
    eval local HOST=\$PEER_$name
    local IP="${HOST#*@}"
    [[ "$name" == "$(hostname -s | tr '[:upper:]' '[:lower:]')" ]] && IP="127.0.0.1"
    echo "$IP"
}

# Check if a task title is already claimed via /work/all on local daemon
_task_is_claimed() {
    local title="$1"
    local work_json
    work_json=$(curl -sf "http://127.0.0.1:8855/work/all" --max-time 3 2>/dev/null)
    if [[ -n "$work_json" ]]; then
        $PYTHON -c "
import json, sys
items = json.loads('''$work_json''').get('items', [])
title = sys.argv[1]
for item in items:
    if item.get('title','') == title and item.get('status') == 'in_progress':
        print('claimed')
        sys.exit(0)
print('available')
" "$title" 2>/dev/null
    else
        echo "available"
    fi
}

# Register work item on local daemon
_register_work() {
    local title="$1"
    local node_id="$2"
    curl -sf -X POST "http://127.0.0.1:8855/work/start" \
        -H "Content-Type: application/json" \
        -d "{\"title\": $(echo "$title" | $PYTHON -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))'), \"node_id\": \"$node_id\"}" \
        --max-time 3 2>/dev/null
}

# Mark work complete
_complete_work() {
    local work_id="$1"
    local summary="$2"
    curl -sf -X POST "http://127.0.0.1:8855/work/update" \
        -H "Content-Type: application/json" \
        -d "{\"work_id\": $(echo "$work_id" | $PYTHON -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))'), \"status\": \"completed\", \"result_summary\": $(echo "$summary" | $PYTHON -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')}" \
        --max-time 3 2>/dev/null
}

# Display a progress table from /work/all
_progress_table() {
    local work_json
    work_json=$(curl -sf "http://127.0.0.1:8855/work/all" --max-time 3 2>/dev/null)
    if [[ -z "$work_json" ]]; then
        echo -e "  ${DIM}(work coordination unavailable)${RESET}"
        return
    fi

    $PYTHON -c "
import json, time, sys

data = json.loads('''$work_json''')
items = data.get('items', [])
if not items:
    print('  (no active work items)')
    sys.exit(0)

# Table header
print(f'  {\"NODE\":<8} {\"TASK\":<45} {\"STATUS\":<12} {\"ELAPSED\":>8}')
print(f'  {\"─\"*8} {\"─\"*45} {\"─\"*12} {\"─\"*8}')

now = time.time()
for item in items[:20]:  # max 20 rows
    node = item.get('node_id', '?')[:8]
    title = item.get('title', '?')
    if len(title) > 44:
        title = title[:41] + '...'
    status = item.get('status', '?')
    started = item.get('started_at', now)
    elapsed = int(now - started)
    mins, secs = divmod(elapsed, 60)
    elapsed_str = f'{mins}m{secs:02d}s'

    # Status indicator
    if status == 'in_progress':
        indicator = '\033[1;33m●\033[0m'
    elif status in ('completed', 'done'):
        indicator = '\033[0;32m✓\033[0m'
    elif status == 'failed':
        indicator = '\033[0;31m✗\033[0m'
    else:
        indicator = '\033[2m○\033[0m'

    print(f'  {node:<8} {title:<45} {indicator} {status:<10} {elapsed_str:>8}')
" 2>/dev/null
}

# ── Auto mode: parse plan into subtasks ──────────────────────────────────────

_auto_parse_plan() {
    local plan_file="$1"
    # Extract task-like lines from plan docs:
    # - Lines starting with "- [ ]" (markdown checkboxes)
    # - Lines starting with "N." or "N)" (numbered tasks)
    # - Lines starting with "- " under headers containing "task" or "step"
    $PYTHON -c "
import re, sys

with open(sys.argv[1]) as f:
    content = f.read()

tasks = []

# Markdown checkboxes: - [ ] task description
for m in re.finditer(r'^[\s]*-\s*\[[ x]\]\s*(.+)$', content, re.MULTILINE):
    task = m.group(1).strip()
    if len(task) > 10:  # skip trivially short items
        tasks.append(task)

# Numbered tasks: 1. task or 1) task
if not tasks:
    for m in re.finditer(r'^[\s]*\d+[\.\)]\s+(.+)$', content, re.MULTILINE):
        task = m.group(1).strip()
        if len(task) > 10 and not task.startswith('#'):
            tasks.append(task)

# Bullet lists under task/step headers
if not tasks:
    in_task_section = False
    for line in content.split('\n'):
        if re.match(r'^#{1,4}\s.*(task|step|todo|work|phase)', line, re.IGNORECASE):
            in_task_section = True
            continue
        if re.match(r'^#{1,4}\s', line):
            in_task_section = False
            continue
        if in_task_section and re.match(r'^[\s]*-\s+(.+)', line):
            task = re.match(r'^[\s]*-\s+(.+)', line).group(1).strip()
            if len(task) > 10:
                tasks.append(task)

# Deduplicate preserving order
seen = set()
unique = []
for t in tasks:
    key = t.lower()
    if key not in seen:
        seen.add(key)
        unique.append(t)

for t in unique:
    print(t)
" "$plan_file" 2>/dev/null
}

# ══════════════════════════════════════════════════════════════════════════════
# MAIN FLOW
# ══════════════════════════════════════════════════════════════════════════════

if $AUTO_MODE; then
    # Auto mode: read plan, split into subtasks, assign to nodes
    QUESTION="auto-plan: $(basename "$AUTO_PLAN_FILE")"
    SUBTASKS=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && SUBTASKS+=("$line")
    done < <(_auto_parse_plan "$AUTO_PLAN_FILE")

    if [[ ${#SUBTASKS[@]} -eq 0 ]]; then
        echo -e "${RED}No subtasks found in plan file: $AUTO_PLAN_FILE${RESET}"
        echo -e "${DIM}Expected markdown checkboxes (- [ ]), numbered lists (1.), or bullet lists under task headers.${RESET}"
        exit 1
    fi
else
    QUESTION="${1:?Usage: fleet-swarm.sh \"your task or question\" | --auto plan.md | --dry-run \"task\"}"
fi

TIMESTAMP=$(date '+%Y%m%d-%H%M%S')
RESULTS_DIR="/tmp/fleet-swarm-$TIMESTAMP"
mkdir -p "$RESULTS_DIR"

_banner
if $AUTO_MODE; then
    echo -e "  ${BOLD}Mode:${RESET} AUTO — splitting plan into subtasks"
    echo -e "  ${BOLD}Plan:${RESET} $AUTO_PLAN_FILE"
    echo -e "  ${BOLD}Subtasks:${RESET} ${#SUBTASKS[@]} found"
else
    echo -e "  ${BOLD}Task:${RESET} $QUESTION"
fi
echo -e "  ${BOLD}Chief:${RESET} $CHIEF"
echo -e "  ${BOLD}Results:${RESET} $RESULTS_DIR"
if $DRY_RUN; then
    echo -e "  ${BOLD}${YELLOW}DRY RUN — no tasks will be dispatched${RESET}"
fi

# ══════════════════════════════════════════════════════════════════════════════
_phase 1 "Wake Fleet — Bringing All Nodes Online"
# ══════════════════════════════════════════════════════════════════════════════

ONLINE_NODES=()
for name in $PEER_NAMES; do
    IP=$(_peer_ip "$name")

    HEALTH=$(curl -sf "http://${IP}:8855/health" --max-time 3 2>/dev/null)
    if [[ -n "$HEALTH" ]]; then
        SESSIONS=$($PYTHON -c "import json; d=json.loads('$HEALTH'); print(d.get('activeSessions', 0))" 2>/dev/null)
        BRANCH=$($PYTHON -c "import json; d=json.loads('$HEALTH'); print(d.get('git',{}).get('branch','?'))" 2>/dev/null)
        _ok "$(_node $name) ONLINE — ${SESSIONS} sessions, branch: ${BRANCH}"
        ONLINE_NODES+=("$name")
    else
        _wait "$(_node $name) SLEEPING — attempting wake..."
        if $DRY_RUN; then
            _info "$(_node $name) [dry-run] would attempt wake via SSH/WoL"
        else
            $SEND --wake "$name" 2>&1 | sed 's/^/    /'
            # Re-check after wake attempt
            sleep 2
            HEALTH=$(curl -sf "http://${IP}:8855/health" --max-time 5 2>/dev/null)
            if [[ -n "$HEALTH" ]]; then
                _ok "$(_node $name) WOKEN — now online"
                ONLINE_NODES+=("$name")
            else
                _fail "$(_node $name) could not be woken"
            fi
        fi
    fi
done

echo ""
echo -e "  ${BOLD}${#ONLINE_NODES[@]} / $(echo $PEER_NAMES | wc -w | tr -d ' ') nodes online${RESET}"

if [[ ${#ONLINE_NODES[@]} -eq 0 ]]; then
    echo -e "\n${RED}No nodes online. Cannot proceed.${RESET}"
    exit 1
fi

# ══════════════════════════════════════════════════════════════════════════════
_phase 2 "Dedup & Dispatch — Checking Claims, Sending Tasks"
# ══════════════════════════════════════════════════════════════════════════════

NODE_ID="${MULTIFLEET_NODE_ID:-$($PYTHON -c 'from tools.fleet_nerve_config import detect_node_id; print(detect_node_id())' 2>/dev/null)}"

# Build task list: either auto-split subtasks or single task
TASKS_TO_DISPATCH=()
TASK_ASSIGNMENTS=()  # parallel array: which node gets which task

if $AUTO_MODE; then
    # Round-robin assign subtasks to online nodes (excluding self)
    REMOTE_NODES=()
    for name in "${ONLINE_NODES[@]}"; do
        [[ "$name" != "$NODE_ID" ]] && REMOTE_NODES+=("$name")
    done

    if [[ ${#REMOTE_NODES[@]} -eq 0 ]]; then
        echo -e "${RED}No remote nodes available for auto-dispatch.${RESET}"
        exit 1
    fi

    IDX=0
    for task in "${SUBTASKS[@]}"; do
        target="${REMOTE_NODES[$((IDX % ${#REMOTE_NODES[@]}))]}"
        TASKS_TO_DISPATCH+=("$task")
        TASK_ASSIGNMENTS+=("$target")
        IDX=$((IDX + 1))
    done

    echo -e "  ${BOLD}Assignment plan:${RESET}"
    for i in "${!TASKS_TO_DISPATCH[@]}"; do
        echo -e "    $(_node ${TASK_ASSIGNMENTS[$i]}) ${TASKS_TO_DISPATCH[$i]}"
    done
    echo ""
else
    # Single task to all remote nodes
    for name in "${ONLINE_NODES[@]}"; do
        if [[ "$name" != "$NODE_ID" ]]; then
            TASKS_TO_DISPATCH+=("$QUESTION")
            TASK_ASSIGNMENTS+=("$name")
        fi
    done
fi

# Dedup check + dispatch
DISPATCHED_COUNT=0
SKIPPED_COUNT=0
declare -A WORK_IDS  # node -> work_id for tracking

for i in "${!TASKS_TO_DISPATCH[@]}"; do
    task="${TASKS_TO_DISPATCH[$i]}"
    target="${TASK_ASSIGNMENTS[$i]}"
    task_key="Fleet swarm: $task"

    # Check if already claimed
    CLAIM_STATUS=$(_task_is_claimed "$task_key")
    if [[ "$CLAIM_STATUS" == "claimed" ]]; then
        _info "$(_node $target) SKIPPED — \"${task:0:60}\" already claimed"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        continue
    fi

    if $DRY_RUN; then
        _info "$(_node $target) [dry-run] WOULD dispatch: ${task:0:70}"
        DISPATCHED_COUNT=$((DISPATCHED_COUNT + 1))
        continue
    fi

    # Register work to prevent duplicates
    WORK_REG=$(_register_work "$task_key" "$target")
    WORK_ID=$($PYTHON -c "import json; print(json.loads('''$WORK_REG''').get('work_id',''))" 2>/dev/null)
    [[ -n "$WORK_ID" ]] && WORK_IDS[$target]="$WORK_ID"

    _live "$(_node $target) Dispatching: ${task:0:60}..."
    RESULT=$($SEND --type task --subject "Fleet swarm: $task" "$target" "$task" 2>&1)
    STATUS=$(echo "$RESULT" | grep -o "delivered\|failed\|queued")
    if [[ "$STATUS" == "delivered" ]]; then
        _spark "$(_node $target) Task delivered — agent spawning"
        DISPATCHED_COUNT=$((DISPATCHED_COUNT + 1))
    else
        _fail "$(_node $target) Delivery: $STATUS"
    fi
done

echo ""
_live "${BOLD}Dispatched: $DISPATCHED_COUNT | Skipped (dedup): $SKIPPED_COUNT${RESET}"

if $DRY_RUN; then
    echo ""
    echo -e "${BOLD}${YELLOW}━━━ DRY RUN COMPLETE ━━━${RESET}"
    echo -e "  Would dispatch $DISPATCHED_COUNT task(s) to ${#ONLINE_NODES[@]} node(s)"
    echo -e "  Skipped $SKIPPED_COUNT already-claimed task(s)"
    echo -e "  Results would go to: $RESULTS_DIR"
    echo ""
    exit 0
fi

if [[ $DISPATCHED_COUNT -eq 0 ]]; then
    echo -e "\n${YELLOW}No tasks dispatched (all deduped or failed). Nothing to monitor.${RESET}"
    exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
_phase 3 "Monitor — Live Progress Table"
# ══════════════════════════════════════════════════════════════════════════════

echo -e "${DIM}Polling every 15s. Ctrl+C to stop.${RESET}"
echo ""

POLL_COUNT=0
MAX_POLLS=40  # 10 minutes
COMPLETE_COUNT=0
EXPECTED=$DISPATCHED_COUNT

while [[ $POLL_COUNT -lt $MAX_POLLS ]]; do
    POLL_COUNT=$((POLL_COUNT + 1))
    sleep 15

    echo -e "${DIM}── $(date '+%H:%M:%S') poll $POLL_COUNT/$MAX_POLLS ──${RESET}"

    # Show fleet-wide work progress table
    _progress_table

    # Check task agent status on all peers
    ACTIVE_TOTAL=0
    COMPLETE_TOTAL=0
    for name in "${ONLINE_NODES[@]}"; do
        [[ "$name" == "$NODE_ID" ]] && continue
        IP=$(_peer_ip "$name")
        HEALTH=$(curl -sf "http://${IP}:8855/health" --max-time 3 2>/dev/null)
        if [[ -n "$HEALTH" ]]; then
            ACTIVE=$($PYTHON -c "import json; d=json.loads('$HEALTH'); print(d.get('activeSessions', 0))" 2>/dev/null || echo 0)
            ACTIVE_TOTAL=$((ACTIVE_TOTAL + ACTIVE))
        fi
    done

    # Check for replies in our seed file
    REPLY_COUNT=0
    for f in /tmp/fleet-seed-${NODE_ID}.md /tmp/fleet-seed-$(hostname -s | tr '[:upper:]' '[:lower:]').md; do
        [[ -f "$f" ]] && REPLY_COUNT=$((REPLY_COUNT + $(grep -c "REPLY\|TASK completed\|TASK failed" "$f" 2>/dev/null || echo 0)))
    done

    if [[ $REPLY_COUNT -gt $COMPLETE_COUNT ]]; then
        NEW=$((REPLY_COUNT - COMPLETE_COUNT))
        COMPLETE_COUNT=$REPLY_COUNT
        echo ""
        echo -e "  ${GREEN}${BOLD}+$NEW reply(s) received! ($COMPLETE_COUNT/$EXPECTED)${RESET}"
        # Show latest reply snippet
        for f in /tmp/fleet-seed-${NODE_ID}.md /tmp/fleet-seed-$(hostname -s | tr '[:upper:]' '[:lower:]').md; do
            [[ -f "$f" ]] && tail -20 "$f" | grep -A 15 "REPLY\|TASK" | tail -10
        done
        echo ""
    fi

    # Check if all expected replies are in
    if [[ $COMPLETE_COUNT -ge $EXPECTED ]]; then
        echo ""
        echo -e "${GREEN}${BOLD}All ${EXPECTED} task(s) complete!${RESET}"
        break
    fi
done

# ══════════════════════════════════════════════════════════════════════════════
_phase 4 "Results — Synthesis & Summary"
# ══════════════════════════════════════════════════════════════════════════════

# Collect all fleet seed replies into results dir
for f in /tmp/fleet-seed-${NODE_ID}.md /tmp/fleet-seed-$(hostname -s | tr '[:upper:]' '[:lower:]').md; do
    [[ -f "$f" ]] && cp "$f" "$RESULTS_DIR/replies-$(basename "$f")" 2>/dev/null
done

# Collect work item results from /work/all
WORK_SUMMARY=$(curl -sf "http://127.0.0.1:8855/work/all" --max-time 3 2>/dev/null)
if [[ -n "$WORK_SUMMARY" ]]; then
    echo "$WORK_SUMMARY" | $PYTHON -m json.tool > "$RESULTS_DIR/work-items.json" 2>/dev/null
fi

# Show reply summaries
echo -e "${BOLD}Fleet Messages:${RESET}"
echo ""
$SEND --check 2>&1 | head -60

# Synthesize results into a summary
echo ""
echo -e "${BOLD}Work Item Results:${RESET}"
echo ""

if [[ -n "$WORK_SUMMARY" ]]; then
    $PYTHON -c "
import json, time, sys

data = json.loads('''$WORK_SUMMARY''')
items = data.get('items', [])

completed = [i for i in items if i.get('status') in ('completed', 'done')]
failed = [i for i in items if i.get('status') == 'failed']
active = [i for i in items if i.get('status') == 'in_progress']

print(f'  Completed: {len(completed)}  |  Failed: {len(failed)}  |  Still active: {len(active)}')
print()

for item in completed:
    node = item.get('node_id', '?')
    title = item.get('title', '?')
    summary = item.get('result_summary', '')
    if len(title) > 60:
        title = title[:57] + '...'
    print(f'  \033[0;32m✓\033[0m [{node}] {title}')
    if summary:
        # Indent summary lines
        for line in summary.split('\n')[:3]:
            print(f'    {line}')

for item in failed:
    node = item.get('node_id', '?')
    title = item.get('title', '?')
    summary = item.get('result_summary', '')
    if len(title) > 60:
        title = title[:57] + '...'
    print(f'  \033[0;31m✗\033[0m [{node}] {title}')
    if summary:
        for line in summary.split('\n')[:3]:
            print(f'    {line}')

for item in active:
    node = item.get('node_id', '?')
    title = item.get('title', '?')
    if len(title) > 60:
        title = title[:57] + '...'
    print(f'  \033[1;33m●\033[0m [{node}] {title} (still running)')
" 2>/dev/null
else
    echo -e "  ${DIM}(work coordination unavailable — check daemon)${RESET}"
fi

# Save synthesis report
{
    echo "# Fleet Swarm Results — $TIMESTAMP"
    echo ""
    echo "## Task"
    if $AUTO_MODE; then
        echo "Auto-plan: $AUTO_PLAN_FILE"
        echo ""
        echo "## Subtasks Dispatched"
        for i in "${!TASKS_TO_DISPATCH[@]}"; do
            echo "- [${TASK_ASSIGNMENTS[$i]}] ${TASKS_TO_DISPATCH[$i]}"
        done
    else
        echo "$QUESTION"
    fi
    echo ""
    echo "## Nodes"
    echo "Online: ${#ONLINE_NODES[@]} | Dispatched: $DISPATCHED_COUNT | Replied: $COMPLETE_COUNT"
    echo ""
    echo "## Timing"
    echo "Started: $TIMESTAMP"
    echo "Finished: $(date '+%Y%m%d-%H%M%S')"
    echo ""
    echo "## Work Items (raw)"
    [[ -n "${WORK_SUMMARY:-}" ]] && echo "$WORK_SUMMARY" | $PYTHON -m json.tool 2>/dev/null || echo "(none)"
} > "$RESULTS_DIR/summary.md"

echo ""
echo -e "${BOLD}${BG_GREEN}                                                        ${RESET}"
echo -e "${BOLD}${BG_GREEN}   ✓ Fleet Swarm Complete — $TIMESTAMP                  ${RESET}"
echo -e "${BOLD}${BG_GREEN}                                                        ${RESET}"
echo ""
echo -e "  Results:    $RESULTS_DIR"
echo -e "  Summary:    $RESULTS_DIR/summary.md"
echo -e "  Nodes:      ${#ONLINE_NODES[@]} online, $DISPATCHED_COUNT dispatched, $COMPLETE_COUNT replied"
echo -e "  Skipped:    $SKIPPED_COUNT (dedup)"
echo ""
