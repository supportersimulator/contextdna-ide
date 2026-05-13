#!/usr/bin/env bash
# discord-wake-atlas.sh — Full autonomous loop:
#   Discord message → Claude CLI → response → Discord
#
# Called by LaunchAgent when /tmp/discord-wake-trigger is modified.
# TRIGGER-BASED ONLY. Zero polling. Zero idle cost.
#
# Flow: iPad → Discord → bot writes seed + trigger → macOS WatchPaths fires this
#       → reads message → runs `claude -p` → captures response → posts back to Discord

set -euo pipefail

# Harden file permissions for everything this script writes (logs, locks,
# session id, token cache). Only the invoking user can read.
umask 077

TRIGGER="/tmp/discord-wake-trigger"
SEED_DIR="/tmp"
LOG="/tmp/discord-wake.log"
LOCK="/tmp/discord-wake.lock"
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MAX_RESPONSE_CHARS=1900  # Discord limit is 2000, leave room for prefix
MAX_CHUNKS=5             # Max Discord messages per response (was 3, now 5 = ~9500 chars)
CLAUDE_TIMEOUT=900       # 15 min — covers 3-surgeon cross-exam (worst case: 6min observed + margin)
SESSION_FILE="/tmp/discord-claude-session-id"
SESSION_MAX_AGE=14400    # 4 hours — rotate session to avoid context bloat
# race/u4: NATS subjects + ZSF counter for the progress-pinger wire.
# discord-wake-atlas.sh publishes start/done envelopes; the Discord bridge
# subscribes fleet.delegation.start.>/.done.> and drives _progress_pinger.
# Every publish failure increments NATS_PUBLISH_FAILS — never silent.
NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
NATS_PUBLISH_FAIL_FILE="/tmp/discord-wake-nats-publish-fails"
DISCORD_DELEGATION_TASK_ID=""

# ── Session persistence ──
# Dedicated session UUID for Discord, rotated every 4h.
# Aaron can force reset: message "!newsession" from Discord.
DISCORD_SESSION_IS_NEW=false
get_or_create_session() {
    if [ -f "$SESSION_FILE" ]; then
        local age=$(( $(date +%s) - $(stat -f %m "$SESSION_FILE" 2>/dev/null || echo 0) ))
        if [ "$age" -lt "$SESSION_MAX_AGE" ]; then
            # P1-c: touch session file on USE so active conversations don't rotate
            # mid-thread. mtime == last-used, not creation time.
            touch "$SESSION_FILE" 2>/dev/null || true
            cat "$SESSION_FILE"
            return
        fi
        log "Session expired (age: ${age}s > ${SESSION_MAX_AGE}s). Rotating."
    fi
    local new_id
    new_id=$(uuidgen | tr '[:upper:]' '[:lower:]')
    printf '%s' "$new_id" > "$SESSION_FILE"
    chmod 600 "$SESSION_FILE" 2>/dev/null || true
    DISCORD_SESSION_IS_NEW=true
    log "New Discord session: $new_id"
    echo "$new_id"
}

reset_session() {
    rm -f "$SESSION_FILE"
    log "Session reset by user request"
}

# Discord-aware system prompt — chief is trusted to orchestrate.
# Agent spawning is now ENCOURAGED for multi-part work (chief only; non-chief
# nodes exit earlier in this script). Keep brevity + cost-awareness.
DISCORD_SYSTEM_PROMPT="You are replying via Discord to Aaron on his iPad from the CHIEF node. HARD CONSTRAINTS:
1. Response MUST be under 6000 characters total. Use bullet points. Lead with the answer, skip preamble.
2. Agent spawning IS allowed and encouraged for multi-part work. Soft cap: max 5 agents per Discord message — respect Aaron's token budget.
3. Delegate heavy work to fleet peers via NATS when it fits naturally (future protocol placeholder — see Phase 4 wiring): publish to subject fleet.delegate.<node> with a task description, then collect the result from fleet.delegate.result.<corrId>. Do NOT attempt to implement or invent this protocol here; describe the delegation in the reply if you would have used it.
4. Heavy tool use is still discouraged IF the request only needs a quick reply — read the room, don't gold-plate.
5. Keep the 6000-char cap. If the honest answer is long, summarize and offer the full version in a follow-up full session.
6. For complex multi-step requests on the chief: either execute with ≤5 agents OR outline the plan and ask before burning tokens."

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# race/u4: publish a delegation envelope (start|done) on NATS.
# Args: <action: start|done> <task_id> <json_extra (optional)>
# Returns 0 on success, 1 on failure. Failures are NEVER silent — they
# bump $NATS_PUBLISH_FAIL_FILE counter and emit a log line so the bridge's
# wake_delegation_publish_errors stat (sourced from this counter) is
# observable via fleet-check / gains-gate.
nats_publish_delegation() {
    local action="$1"
    local task_id="$2"
    local extra_json="${3:-{\}}"
    local subject="fleet.delegation.${action}.${task_id}"
    local payload
    payload=$(python3 -c "
import json, sys, time
extra = json.loads(sys.argv[1] or '{}')
extra.setdefault('task_id', sys.argv[2])
extra.setdefault('action', sys.argv[3])
extra.setdefault('ts', time.time())
extra.setdefault('source', 'discord-wake-atlas')
print(json.dumps(extra))
" "$extra_json" "$task_id" "$action" 2>/dev/null) || {
        printf '%s\n' "$(( $(cat "$NATS_PUBLISH_FAIL_FILE" 2>/dev/null || echo 0) + 1 ))" > "$NATS_PUBLISH_FAIL_FILE"
        log "nats_publish_delegation: payload build FAILED for $subject"
        return 1
    }
    if ! python3 - "$NATS_URL" "$subject" "$payload" <<'PY' 2>>"$LOG"; then
import asyncio, sys
nats_url, subject, payload = sys.argv[1], sys.argv[2], sys.argv[3]

async def go():
    import nats
    nc = await nats.connect(servers=[nats_url], connect_timeout=3)
    try:
        await nc.publish(subject, payload.encode())
        await nc.flush(timeout=2)
    finally:
        await nc.close()

try:
    asyncio.run(go())
except Exception as e:
    print(f"[wake-nats] publish to {subject} failed: {e}", file=sys.stderr)
    sys.exit(1)
PY
        printf '%s\n' "$(( $(cat "$NATS_PUBLISH_FAIL_FILE" 2>/dev/null || echo 0) + 1 ))" > "$NATS_PUBLISH_FAIL_FILE"
        log "nats_publish_delegation: PUBLISH FAILED $subject (counter=$(cat "$NATS_PUBLISH_FAIL_FILE" 2>/dev/null || echo ?))"
        return 1
    fi
    log "nats_publish_delegation: $subject ok"
    return 0
}

# Prevent concurrent runs (LaunchAgent can re-trigger while we're processing)
# Lock stores timestamp on line 1, PID on line 2 for liveness checks.
# Handles: SIGKILL (stale lock detected by age+PID), crash mid-run (trap cleans up)
if [ -f "$LOCK" ]; then
    LOCK_PID=$(sed -n '2p' "$LOCK" 2>/dev/null || echo "")
    LOCK_AGE=$(( $(date +%s) - $(sed -n '1p' "$LOCK" 2>/dev/null || echo 0) ))
    # Only honor lock if holder is alive AND lock is fresh
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null && [ "$LOCK_AGE" -lt 300 ]; then
        log "Locked by PID $LOCK_PID (age: ${LOCK_AGE}s). Skipping."
        exit 0
    fi
    log "Stale lock (age: ${LOCK_AGE}s, PID: ${LOCK_PID:-unknown}). Removing."
    rm -f "$LOCK"
fi
printf '%s\n%s\n' "$(date +%s)" "$$" > "$LOCK"
chmod 600 "$LOCK" 2>/dev/null || true
# Ensure log file has restrictive perms if it exists (older deployments may have 644).
[ -f "$LOG" ] && chmod 600 "$LOG" 2>/dev/null || true
trap 'rm -f "$LOCK"' EXIT INT TERM HUP

# Consume trigger
rm -f "$TRIGGER"

# ── Chief-only gate ──
# Only the designated chief node handles Discord. Otherwise every mac wakes on
# every message → N-way divergent sessions and N reply storms.
# Identity resolution: MULTIFLEET_NODE_ID env (set by LaunchAgent) → ComputerName fallback.
_node_id="${MULTIFLEET_NODE_ID:-}"
if [[ -z "$_node_id" ]]; then
    _node_id=$(scutil --get ComputerName 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo unknown)
fi
_chief=$(cd "$REPO" 2>/dev/null && python3 -c 'import json;print(json.load(open(".multifleet/config.json"))["chief"]["nodeId"])' 2>/dev/null || true)
# Legacy fallback: older deployments assumed the chief was "mac1". OSS adopters
# should populate .multifleet/config.json's "chief.nodeId" to avoid this.
[[ -z "$_chief" ]] && _chief="${MULTIFLEET_CHIEF_ID:-mac1}"
if [[ "$_node_id" != "$_chief" ]]; then
    log "Not chief ($_node_id != $_chief) — skipping Discord processing"
    exit 0
fi

# Cleanup old Discord images (>24h) to prevent disk fill
if [ -d /tmp/fleet-discord-images ]; then
    find /tmp/fleet-discord-images -type f -mmin +1440 -delete 2>/dev/null || true
fi

# Find Discord seed files
shopt -s nullglob
SEEDS=($SEED_DIR/fleet-seed-discord-*.md)
shopt -u nullglob

if [ ${#SEEDS[@]} -eq 0 ]; then
    log "Triggered but no seeds found"
    exit 0
fi

log "Discord wake: ${#SEEDS[@]} seed(s) found"

# Extract raw message text (strip markdown headers) and image paths
MESSAGE=""
IMAGE_PATHS=()
SEED_COUNT=0
for f in "${SEEDS[@]}"; do
    [ -f "$f" ] || continue
    # Get content after the first blank line (skip header)
    CONTENT=$(sed '1,/^$/d' "$f" | head -30)
    if [ -n "$CONTENT" ]; then
        SEED_COUNT=$((SEED_COUNT + 1))
        if [ $SEED_COUNT -gt 1 ]; then
            MESSAGE+=$'\n---\n'
        fi
        MESSAGE+="$CONTENT"$'\n'
    fi
    # Extract image paths from seed file (format: - **Image**: `/path/to/image`)
    while IFS= read -r imgline; do
        imgpath=$(echo "$imgline" | sed -n 's/.*`\(\/tmp\/fleet-discord-images\/[^`]*\)`.*/\1/p')
        if [ -n "$imgpath" ] && [ -f "$imgpath" ]; then
            IMAGE_PATHS+=("$imgpath")
        fi
    done < <(grep "fleet-discord-images" "$f" 2>/dev/null || true)
done

if [ ${#IMAGE_PATHS[@]} -gt 0 ]; then
    log "Found ${#IMAGE_PATHS[@]} image(s) in seed files"
fi

# Archive seeds (nanosecond timestamp to avoid collisions from rapid messages)
mkdir -p /tmp/fleet-seed-archive
for f in "${SEEDS[@]}"; do
    [ -f "$f" ] || continue
    mv "$f" "/tmp/fleet-seed-archive/$(date +%s%N 2>/dev/null || date +%s)-$(basename "$f")"
done

if [ -z "$MESSAGE" ]; then
    log "No message content extracted"
    exit 0
fi

# ── Handle !newsession command ──
if echo "$MESSAGE" | grep -qi '^\s*!newsession'; then
    reset_session
    send_discord "🔄 Session reset. Next message starts fresh conversation." || true
    log "!newsession command — session reset, exiting"
    exit 0
fi

# Prefix context when multiple messages arrived at once (ThrottleInterval=10s batching)
if [ $SEED_COUNT -gt 1 ]; then
    MESSAGE="[${SEED_COUNT} messages received together]"$'\n'"$MESSAGE"
    log "Processing $SEED_COUNT combined messages"
else
    log "Processing: $(echo "$MESSAGE" | head -1 | cut -c1-80)"
fi

# Append image reading instructions if images present
if [ ${#IMAGE_PATHS[@]} -gt 0 ]; then
    MESSAGE+=$'\n\n[IMAGES FROM DISCORD — Use Read tool on each path to view]\n'
    for img in "${IMAGE_PATHS[@]}"; do
        MESSAGE+="Image: $img"$'\n'
    done
fi

# Get bot token for Discord reply
# security(1) accesses login keychain — works from LaunchAgent (user session)
# but may fail if keychain is locked after sleep/reboot. Cache as fallback.
BOT_TOKEN=""
CACHED_TOKEN="/tmp/.discord-bot-token-cache"
if BOT_TOKEN=$(security find-generic-password -a fleet -s DISCORD_BOT_TOKEN -w 2>/dev/null); then
    # Cache for keychain-locked scenarios (restricted permissions)
    printf '%s' "$BOT_TOKEN" > "$CACHED_TOKEN"
    chmod 600 "$CACHED_TOKEN"
elif [ -f "$CACHED_TOKEN" ]; then
    BOT_TOKEN=$(cat "$CACHED_TOKEN" 2>/dev/null || true)
    log "Keychain locked — using cached bot token"
fi
CHANNEL_ID="${FLEET_DISCORD_CHANNEL_ID:-1491820715421466865}"

send_discord() {
    local msg="$1"
    if [ -z "$BOT_TOKEN" ]; then
        log "No bot token — cannot reply to Discord"
        return 1
    fi
    # Truncate if too long
    if [ ${#msg} -gt $MAX_RESPONSE_CHARS ]; then
        msg="${msg:0:$MAX_RESPONSE_CHARS}... (truncated)"
    fi
    # Escape for JSON — python json.dumps handles all special chars:
    # backticks, quotes, newlines, unicode, control chars
    local json_msg
    json_msg=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$msg" 2>/dev/null) || {
        # Fallback: basic escape if python3 unavailable
        json_msg=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' | tr '\n' ' ')
        json_msg="\"$json_msg\""
    }
    curl -sf --max-time 10 -X POST "https://discord.com/api/v10/channels/$CHANNEL_ID/messages" \
        -H "Authorization: Bot $BOT_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"content\": $json_msg}" > /dev/null 2>&1
    log "Discord reply sent (${#msg} chars)"
}

# Acknowledge receipt immediately
send_discord "⏳ Processing your message..." || true

# Run Claude CLI — full capability, no turn limits
# Aaron's directive: no max-turns, no tool restrictions — full Atlas power from iPad
# Session persistence: --session-id keeps conversation context across Discord messages
# Timeout via background process + kill (macOS lacks timeout(1))
# IS_NEW computed in parent scope — assignments inside $(subshell) do not propagate back.
DISCORD_SESSION_IS_NEW=false
if [ ! -f "$SESSION_FILE" ]; then
    DISCORD_SESSION_IS_NEW=true
else
    _age=$(( $(date +%s) - $(stat -f %m "$SESSION_FILE" 2>/dev/null || echo 0) ))
    [ "$_age" -ge "$SESSION_MAX_AGE" ] && DISCORD_SESSION_IS_NEW=true
fi
DISCORD_SESSION=$(get_or_create_session)
log "Running claude -p (session: ${DISCORD_SESSION:0:8}..., timeout: ${CLAUDE_TIMEOUT}s) ..."
TMPOUT=$(mktemp /tmp/discord-claude-XXXXXX)
trap 'rm -f "$LOCK" "$TMPOUT"' EXIT INT TERM HUP

# race/u4: announce delegation start so Discord bridge spins up _progress_pinger.
# task_id is short hex tied to the session + nanosecond clock — uniquely
# identifies THIS claude -p invocation across retries. ETA = CLAUDE_TIMEOUT
# (worst case); pinger interval defaults to 30s, capped at 5 pings, total
# 150s window before the bridge falls silent (well under the 15-min wall).
DISCORD_DELEGATION_TASK_ID="wake-${DISCORD_SESSION:0:8}-$(date +%s%N 2>/dev/null || date +%s)"
DISCORD_DELEGATION_TASK_ID="${DISCORD_DELEGATION_TASK_ID:0:48}"
nats_publish_delegation "start" "$DISCORD_DELEGATION_TASK_ID" \
    "{\"eta_s\":${CLAUDE_TIMEOUT},\"interval_s\":30,\"channel_id\":\"${CHANNEL_ID}\",\"source\":\"discord-wake-atlas\"}" \
    || log "wake: delegation.start publish failed — bridge will not pinger this run"

# Belt-and-braces: ensure delegation.done fires on EVERY exit path (success,
# fallback failure, timeout-then-no-fallback, hard exit). Without this, a
# bridge-side _progress_pinger would keep emitting "still working" embeds
# long after the wake-script has died — UX worse than no-pinger-at-all.
_publish_delegation_done_on_exit() {
    local exit_code=$?
    if [ -n "${DISCORD_DELEGATION_TASK_ID:-}" ]; then
        local resp_len="${#CLAUDE_RESPONSE:-0}"
        nats_publish_delegation "done" "$DISCORD_DELEGATION_TASK_ID" \
            "{\"exit\":${exit_code},\"response_chars\":${resp_len:-0}}" \
            || log "wake: delegation.done publish failed (exit=${exit_code})"
        DISCORD_DELEGATION_TASK_ID=""  # idempotent — only fire once
    fi
    return $exit_code
}
trap '_publish_delegation_done_on_exit; rm -f "$LOCK" "$TMPOUT"' EXIT INT TERM HUP

# Fresh session → --session-id (create). Existing → --resume (continue conversation).
CLAUDE_SESSION_ARGS=()
if [ "$DISCORD_SESSION_IS_NEW" = "true" ]; then
    CLAUDE_SESSION_ARGS=(--session-id "$DISCORD_SESSION")
    log "Starting new session: ${DISCORD_SESSION:0:8}..."
else
    CLAUDE_SESSION_ARGS=(--resume "$DISCORD_SESSION")
    log "Resuming session: ${DISCORD_SESSION:0:8}..."
fi
(cd "$REPO" && CLAUDECODE="" /usr/local/bin/claude -p "$MESSAGE" \
    "${CLAUDE_SESSION_ARGS[@]}" \
    --append-system-prompt "$DISCORD_SYSTEM_PROMPT" \
    --output-format text \
    > "$TMPOUT" 2>> "$LOG") &
CLAUDE_PID=$!

# Wait with timeout
ELAPSED=0
while kill -0 "$CLAUDE_PID" 2>/dev/null; do
    if [ $ELAPSED -ge $CLAUDE_TIMEOUT ]; then
        kill "$CLAUDE_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$CLAUDE_PID" 2>/dev/null || true
        log "Claude CLI timed out after ${CLAUDE_TIMEOUT}s (PID: $CLAUDE_PID)"
        rm -f "$TMPOUT"
        # Set exit code to trigger DeepSeek fallback below
        CLAUDE_EXIT=124
        CLAUDE_RESPONSE=""
        send_discord "⏱️ Claude timed out — trying DeepSeek fallback..." || true
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

# Only read results if we didn't already timeout (CLAUDE_EXIT=124 from break above)
if [ "${CLAUDE_EXIT:-0}" -ne 124 ]; then
    wait "$CLAUDE_PID" 2>/dev/null
    CLAUDE_EXIT=$?
    CLAUDE_RESPONSE=$(cat "$TMPOUT" 2>/dev/null || true)
    rm -f "$TMPOUT"
fi

# Defensive retry: if --resume failed because the session file exists on disk
# but Claude CLI's internal store has no matching conversation (crash, manual
# cleanup, etc.), re-run once with --session-id to recreate fresh.
# Only applies when we were resuming (IS_NEW=false) and didn't timeout.
if [ "${CLAUDE_EXIT:-0}" -ne 0 ] && [ "${CLAUDE_EXIT:-0}" -ne 124 ] \
   && [ "$DISCORD_SESSION_IS_NEW" = "false" ] \
   && tail -n 50 "$LOG" 2>/dev/null | grep -q "No conversation found"; then
    log "Resume failed — session stale — recreating with --session-id"
    TMPOUT2=$(mktemp /tmp/discord-claude-XXXXXX)
    # race/u4: keep the delegation.done publisher in the trap chain across the
    # recreate retry. Otherwise the bridge would never see a `done` envelope
    # for this task_id and the pinger would run to its 5-ping cap.
    trap '_publish_delegation_done_on_exit; rm -f "$LOCK" "$TMPOUT" "$TMPOUT2"' EXIT INT TERM HUP
    (cd "$REPO" && CLAUDECODE="" /usr/local/bin/claude -p "$MESSAGE" \
        --session-id "$DISCORD_SESSION" \
        --append-system-prompt "$DISCORD_SYSTEM_PROMPT" \
        --output-format text \
        > "$TMPOUT2" 2>> "$LOG") &
    CLAUDE_PID=$!
    ELAPSED=0
    while kill -0 "$CLAUDE_PID" 2>/dev/null; do
        if [ $ELAPSED -ge $CLAUDE_TIMEOUT ]; then
            kill "$CLAUDE_PID" 2>/dev/null || true
            sleep 1
            kill -9 "$CLAUDE_PID" 2>/dev/null || true
            log "Claude CLI (recreate retry) timed out after ${CLAUDE_TIMEOUT}s"
            CLAUDE_EXIT=124
            CLAUDE_RESPONSE=""
            rm -f "$TMPOUT2"
            break
        fi
        sleep 2
        ELAPSED=$((ELAPSED + 2))
    done
    if [ "${CLAUDE_EXIT:-0}" -ne 124 ]; then
        wait "$CLAUDE_PID" 2>/dev/null
        CLAUDE_EXIT=$?
        CLAUDE_RESPONSE=$(cat "$TMPOUT2" 2>/dev/null || true)
        rm -f "$TMPOUT2"
        [ $CLAUDE_EXIT -eq 0 ] && log "Recreate-with-session-id retry succeeded"
    fi
fi

if [ $CLAUDE_EXIT -ne 0 ] || [ -z "$CLAUDE_RESPONSE" ]; then
    log "Claude CLI failed or empty (exit: $CLAUDE_EXIT) — trying DeepSeek fallback"

    # Fallback API key resolution: try DeepSeek first (cheapest), then OpenAI
    FALLBACK_KEY=""
    FALLBACK_PROVIDER=""
    FALLBACK_URL=""
    FALLBACK_MODEL=""

    # Try DeepSeek ($0.28/1M input — cheapest)
    DS_KEY="${Context_DNA_Deepseek:-${DEEPSEEK_API_KEY:-}}"
    if [ -z "$DS_KEY" ]; then
        DS_KEY=$(security find-generic-password -a fleet -s Context_DNA_Deepseek -w 2>/dev/null || true)
    fi
    if [ -z "$DS_KEY" ] && [ -f "$REPO/context-dna/.env" ]; then
        DS_KEY=$(grep -E '^Context_DNA_Deepseek=' "$REPO/context-dna/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    fi
    if [ -n "$DS_KEY" ]; then
        FALLBACK_KEY="$DS_KEY"
        FALLBACK_PROVIDER="DeepSeek"
        FALLBACK_URL="https://api.deepseek.com/v1/chat/completions"
        FALLBACK_MODEL="deepseek-chat"
    fi

    # Try OpenAI as second choice
    if [ -z "$FALLBACK_KEY" ]; then
        OAI_KEY="${Context_DNA_OPENAI:-${OPENAI_API_KEY:-}}"
        if [ -z "$OAI_KEY" ]; then
            OAI_KEY=$(security find-generic-password -s Context_DNA_OPENAI -w 2>/dev/null || true)
        fi
        if [ -z "$OAI_KEY" ] && [ -f "$REPO/context-dna/.env" ]; then
            OAI_KEY=$(grep -E '^Context_DNA_OPENAI=' "$REPO/context-dna/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
        fi
        if [ -n "$OAI_KEY" ]; then
            FALLBACK_KEY="$OAI_KEY"
            FALLBACK_PROVIDER="OpenAI"
            FALLBACK_URL="https://api.openai.com/v1/chat/completions"
            FALLBACK_MODEL="gpt-4.1-mini"
        fi
    fi

    if [ -n "$FALLBACK_KEY" ]; then
        send_discord "⏳ Claude unavailable — falling back to ${FALLBACK_PROVIDER}..." || true
        log "${FALLBACK_PROVIDER} fallback: calling ${FALLBACK_MODEL} API"

        FALLBACK_RESPONSE=$(python3 -c "
import json, sys, urllib.request, urllib.error

api_key = sys.argv[1]
message = sys.argv[2]
system = sys.argv[3]
api_url = sys.argv[4]
model = sys.argv[5]

payload = json.dumps({
    'model': model,
    'messages': [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': message},
    ],
    'max_tokens': 2048,
    'temperature': 0.5,
}).encode()

req = urllib.request.Request(
    api_url,
    data=payload,
    headers={
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    },
)
try:
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read().decode())
    content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
    print(content)
except Exception as e:
    print(f'API error: {e}', file=sys.stderr)
    sys.exit(1)
" "$FALLBACK_KEY" "$MESSAGE" "$DISCORD_SYSTEM_PROMPT" "$FALLBACK_URL" "$FALLBACK_MODEL" 2>> "$LOG")

        if [ $? -eq 0 ] && [ -n "$FALLBACK_RESPONSE" ]; then
            log "${FALLBACK_PROVIDER} fallback succeeded (${#FALLBACK_RESPONSE} chars)"
            CLAUDE_RESPONSE="$FALLBACK_RESPONSE"
            CLAUDE_EXIT=0
            USED_FALLBACK="$FALLBACK_PROVIDER"
        else
            log "${FALLBACK_PROVIDER} fallback also failed"
            if [ $CLAUDE_EXIT -ne 0 ]; then
                send_discord "❌ Claude CLI failed (exit $CLAUDE_EXIT) and ${FALLBACK_PROVIDER} fallback also failed. Both APIs may be down." || true
            else
                send_discord "⚠️ Atlas returned empty and ${FALLBACK_PROVIDER} fallback also failed." || true
            fi
            exit 1
        fi
    else
        log "No fallback API key available (checked DeepSeek + OpenAI)"
        if [ $CLAUDE_EXIT -ne 0 ]; then
            send_discord "❌ Claude CLI failed (exit $CLAUDE_EXIT). No fallback API keys configured. Set Context_DNA_Deepseek or Context_DNA_OPENAI." || true
        else
            send_discord "⚠️ Atlas returned empty. No fallback API keys configured." || true
        fi
        exit 1
    fi
fi

log "Response received (${#CLAUDE_RESPONSE} chars)"

# ── Secret redaction pass ──
# Chief keeps Bash, so the assistant could accidentally surface a token in
# the reply (env dumps, grep hits, stack traces). Scrub common patterns
# before posting to Discord.
# Patterns covered (ordered most-specific → least-specific to avoid
# generic matchers eating more-specific tokens):
#   - Slack webhook URL:   https://hooks.slack.com/services/T.../B.../...
#   - SendGrid API key:    SG.<22>.<43>
#   - GitHub fine-grained PAT: github_pat_<82>
#   - GitHub classic PAT:  ghp_<36>
#   - Google API key:      AIza<35>
#   - Stripe secret/pub:   sk|pk_test|live_<24+>
#   - JWT token:           eyJ....eyJ....<sig>
#   - Discord bot token:   [MN]<23+>.<6>.<27+>
#   - OpenAI/Anthropic sk: sk-<20+>
#   - AWS access key:      AKIA<16>
#   - Slack legacy tokens: xox[abp]-...
#   - Discord "Bot <tok>": Bot <tok>
_redacted=$(printf '%s' "$CLAUDE_RESPONSE" | sed -E \
    -e 's|https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+|[REDACTED]|g' \
    -e 's/SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}/[REDACTED]/g' \
    -e 's/github_pat_[A-Za-z0-9_]{82}/[REDACTED]/g' \
    -e 's/ghp_[A-Za-z0-9]{36}/[REDACTED]/g' \
    -e 's/AIza[0-9A-Za-z_-]{35}/[REDACTED]/g' \
    -e 's/(sk|pk)_(test|live)_[A-Za-z0-9]{24,}/[REDACTED]/g' \
    -e 's/eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/[REDACTED]/g' \
    -e 's/[MN][A-Za-z0-9]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}/[REDACTED]/g' \
    -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED]/g' \
    -e 's/AKIA[0-9A-Z]{16}/[REDACTED]/g' \
    -e 's/xox[abp]-[A-Za-z0-9-]+/[REDACTED]/g' \
    -e 's/Bot [A-Za-z0-9._-]+/Bot [REDACTED]/g')
if [ "$_redacted" != "$CLAUDE_RESPONSE" ]; then
    log "Secret redaction: scrubbed sensitive pattern(s) from response"
    CLAUDE_RESPONSE="$_redacted"
fi
unset _redacted

# Send response back to Discord
# Split into chunks — $MAX_CHUNKS messages max to avoid spam
SESSION_SHORT="${DISCORD_SESSION:0:8}"
REPLY_PREFIX="🧠 **Atlas** [\`${SESSION_SHORT}\`]:"
if [ -n "${USED_FALLBACK:-}" ]; then
    REPLY_PREFIX="🔄 **Atlas (via ${USED_FALLBACK})** [\`${SESSION_SHORT}\`]:"
fi

# P1-a fix: chunk 1 = REPLY_PREFIX + body must fit 2000 char Discord limit.
# Conservative size: 1700 body chars so REPLY_PREFIX (~150-250) + body < 2000.
# Chunks 2+ get a "(cont N/M)" marker so mobile doesn't render them as user messages.
#
# Markdown-aware splitting via scripts/discord-split.py — prefers paragraph
# boundaries, closes+reopens ``` code fences across chunks (with language
# tag preserved), and avoids breaking `[text](url)` links. Chunks are
# NUL-separated on disk so multi-paragraph bodies survive intact.
CHUNK_BODY_CHARS=1700
if [ ${#CLAUDE_RESPONSE} -gt $CHUNK_BODY_CHARS ]; then
    CHUNKS_FILE="$(mktemp -t discord-chunks.XXXXXX)"
    # shellcheck disable=SC2064
    trap "rm -f '$CHUNKS_FILE'" EXIT
    if ! printf '%s' "$CLAUDE_RESPONSE" \
        | python3 "$REPO/scripts/discord-split.py" "$CHUNK_BODY_CHARS" \
        > "$CHUNKS_FILE"; then
        log "discord-split.py failed — falling back to raw slice"
        # Legacy fallback: fixed-width slice (old behavior).
        send_discord "$REPLY_PREFIX
${CLAUDE_RESPONSE:0:$CHUNK_BODY_CHARS}" || true
        OFFSET=$CHUNK_BODY_CHARS
        CHUNK=2
        while [ $OFFSET -lt ${#CLAUDE_RESPONSE} ] && [ $CHUNK -le $MAX_CHUNKS ]; do
            PART="${CLAUDE_RESPONSE:$OFFSET:$CHUNK_BODY_CHARS}"
            [ -n "$PART" ] && { sleep 0.5; send_discord "↳ *(cont ${CHUNK}/?)*
${PART}" || true; }
            OFFSET=$((OFFSET + CHUNK_BODY_CHARS))
            CHUNK=$((CHUNK + 1))
        done
    else
        # Each chunk is NUL-TERMINATED by discord-split.py, so the chunk
        # count equals the NUL byte count.
        ACTUAL_CHUNKS=$(tr -cd '\000' < "$CHUNKS_FILE" | wc -c | tr -d ' ')
        TOTAL_CHUNKS=$ACTUAL_CHUNKS
        [ $TOTAL_CHUNKS -gt $MAX_CHUNKS ] && TOTAL_CHUNKS=$MAX_CHUNKS
        CHUNK=1
        while IFS= read -r -d '' PART; do
            [ $CHUNK -gt $MAX_CHUNKS ] && break
            if [ $CHUNK -eq 1 ]; then
                send_discord "$REPLY_PREFIX
${PART}" || true
            else
                sleep 0.5
                send_discord "↳ *(cont ${CHUNK}/${TOTAL_CHUNKS})*
${PART}" || true
            fi
            CHUNK=$((CHUNK + 1))
        done < "$CHUNKS_FILE"
        if [ $ACTUAL_CHUNKS -gt $MAX_CHUNKS ]; then
            send_discord "*(truncated — ${#CLAUDE_RESPONSE} chars total, ${ACTUAL_CHUNKS} chunks, showed ${MAX_CHUNKS})*" || true
        fi
    fi
    rm -f "$CHUNKS_FILE"
    # race/u4: drop the chunks-file-only trap, but KEEP delegation.done +
    # lock cleanup. Bare `trap - EXIT` here erased the publisher and the
    # bridge would never clear the pinger.
    trap '_publish_delegation_done_on_exit; rm -f "$LOCK" "$TMPOUT"' EXIT INT TERM HUP
else
    send_discord "$REPLY_PREFIX
$CLAUDE_RESPONSE" || true
fi

log "Complete. Message processed and response sent to Discord."
