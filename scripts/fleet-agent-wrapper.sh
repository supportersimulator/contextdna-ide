#!/usr/bin/env bash
# fleet-agent-wrapper.sh — Wraps claude -p with guaranteed reply-on-completion.
#
# Ensures every fleet task agent reports back, even on crash/timeout.
# Uses trap EXIT to send a reply with status (completed/failed/timeout).
#
# Usage (called by fleet_nerve_daemon.py, not directly):
#   bash scripts/fleet-agent-wrapper.sh \
#     --task-id <id> --sender <node> --reply-to <node> --reply-ip <ip> \
#     --log <path> --timeout <seconds> --repo <path> -- <claude args...>

set -uo pipefail

TASK_ID=""
SENDER=""
REPLY_TO=""
REPLY_IP=""
LOG_PATH=""
TIMEOUT=600
REPO=""
CLAUDE_ARGS=()

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --task-id) TASK_ID="$2"; shift 2 ;;
        --sender) SENDER="$2"; shift 2 ;;
        --reply-to) REPLY_TO="$2"; shift 2 ;;
        --reply-ip) REPLY_IP="$2"; shift 2 ;;
        --log) LOG_PATH="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        --) shift; CLAUDE_ARGS=("$@"); break ;;
        *) CLAUDE_ARGS+=("$1"); shift ;;
    esac
done

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
STATUS="failed"
EXIT_CODE=1
START_TIME=$(date +%s)

# ── Guaranteed reply on exit (trap EXIT) ──
_send_reply() {
    local elapsed=$(( $(date +%s) - START_TIME ))
    local result_summary=""

    if [[ -f "$LOG_PATH" ]]; then
        # Last 500 chars of agent output as summary
        result_summary=$(tail -c 500 "$LOG_PATH" 2>/dev/null || echo "no output")
    fi

    # Send reply via Fleet Nerve HTTP (not fleet_nerve_send.py to avoid python dependency issues)
    local payload
    payload=$(python3 -c "
import json, time
print(json.dumps({
    'id': '$(uuidgen 2>/dev/null || python3 -c \"import uuid; print(uuid.uuid4())\")',
    'type': 'reply',
    'from': '$NODE_ID',
    'to': '$REPLY_TO',
    'ref': '$TASK_ID',
    'seq': int(time.time()) % 100000,
    'fleetId': 'contextdna-main',
    'priority': 2,
    'payload': {
        'subject': 'Task $STATUS: ${TASK_ID:0:8}',
        'body': 'Task from $SENDER completed with status=$STATUS exit=$EXIT_CODE elapsed=${elapsed}s.\n\nResult:\n' + '''$result_summary'''[:1000]
    },
    'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'ttl_hours': 24,
    'requires_ack': False,
}))
" 2>/dev/null)

    if [[ -n "$payload" && -n "$REPLY_IP" ]]; then
        curl -sf -X POST "http://${REPLY_IP}:8855/message" \
            -H "Content-Type: application/json" \
            -d "$payload" \
            --max-time 5 >/dev/null 2>&1 || true
    fi

    echo "[fleet-agent-wrapper] Task ${TASK_ID:0:8} $STATUS (exit=$EXIT_CODE, ${elapsed}s)" >> "$LOG_PATH" 2>/dev/null

    # ── Push result into LOCAL interactive session's seed file ──
    local seed="/tmp/fleet-seed-${NODE_ID}.md"
    {
        echo "## [TASK $STATUS] ${TASK_ID:0:8} from $SENDER (${elapsed}s)"
        echo ""
        echo "$result_summary"
        echo ""
        echo "---"
        echo ""
    } >> "$seed" 2>/dev/null

    # ── macOS notification (Approach 2: gentle push) ──
    osascript -e "display notification \"Task ${TASK_ID:0:8} $STATUS (${elapsed}s)\" with title \"Fleet Nerve — $NODE_ID\" sound name \"Glass\"" 2>/dev/null || true
}

trap _send_reply EXIT

# ── Timeout watchdog ──
if [[ "$TIMEOUT" -gt 0 ]]; then
    (
        sleep "$TIMEOUT"
        # If we're still running after timeout, kill the claude process
        if kill -0 $$ 2>/dev/null; then
            STATUS="timeout"
            echo "[fleet-agent-wrapper] TIMEOUT after ${TIMEOUT}s — killing agent" >> "$LOG_PATH" 2>/dev/null
            kill -TERM $$ 2>/dev/null
        fi
    ) &
    WATCHDOG_PID=$!
fi

# ── Run the claude agent ──
cd "$REPO" 2>/dev/null || true

"${CLAUDE_ARGS[@]}" > "$LOG_PATH" 2>&1
EXIT_CODE=$?

if [[ $EXIT_CODE -eq 0 ]]; then
    STATUS="completed"
else
    STATUS="failed"
fi

# Kill watchdog if still running
[[ -n "${WATCHDOG_PID:-}" ]] && kill "$WATCHDOG_PID" 2>/dev/null || true

# EXIT trap fires here → _send_reply() runs guaranteed
