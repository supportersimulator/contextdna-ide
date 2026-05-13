#!/bin/bash
# <xbar.title>Claude Bridge Mode</xbar.title>
# <xbar.version>v1.1.0</xbar.version>
# <xbar.desc>Toggle Claude Code routing between Anthropic and DeepSeek bridge — persistent state survives daemon death</xbar.desc>
# <xbar.author>fleet</xbar.author>
#
# Persistent state lives in ~/.fleet-bridge-mode (single line: "anthropic" or "bridge").
# Aaron's shell rc reads this file and conditionally exports ANTHROPIC_BASE_URL —
# toggle here, open new terminal, claude picks up the new mode automatically.
#
# Install:
#   ln -sf "$REPO/scripts/xbar-claude-bridge-mode.60s.sh" \
#     "$HOME/Library/Application Support/xbar/plugins/claude-bridge-mode.60s.sh"
#
# Add to ~/.zshrc (one-time):
#   if [ -f ~/.fleet-bridge-mode ] && [ "$(cat ~/.fleet-bridge-mode)" = "bridge" ]; then
#       export ANTHROPIC_BASE_URL=http://localhost:8855/v1
#   fi
#
# v1.1.0 hardening:
#   - 4-state badge (ANT / BRG / MIX(N) / BRG!) — MIX warns when bridge counters move
#     while shell is in anthropic mode (or vice versa) → silent fallback detector.
#   - Dropdown dumps live bridge counters from /health.stats so Aaron sees DeepSeek
#     fallback rate, passthrough errors, and skipped requests at a glance.

set -e

STATE_FILE="$HOME/.fleet-bridge-mode"
BRIDGE_URL="http://127.0.0.1:8855"
SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

# Counters worth showing in the dropdown — keys must match daemon._stats in
# tools/fleet_nerve_nats.py (~line 1624). Ordered by importance.
COUNTER_KEYS=(
    bridge_anthropic_ok
    bridge_fallback_to_deepseek
    bridge_anthropic_skipped
    bridge_anthropic_passthrough_errors
    bridge_deepseek_failures
)

# Locate repo for "open shell" actions
REPO=""
for d in "$HOME/dev/er-simulator-superrepo" "$HOME/Documents/er-simulator-superrepo"; do
    [ -d "$d" ] && REPO="$d" && break
done

# Read current mode (default: anthropic)
MODE="anthropic"
[ -f "$STATE_FILE" ] && MODE="$(cat "$STATE_FILE" 2>/dev/null || echo anthropic)"
[ "$MODE" != "bridge" ] && [ "$MODE" != "anthropic" ] && MODE="anthropic"

# Handle clicks (xbar passes "$1" arg to invoked plugin via bash= directives)
case "$1" in
    toggle-bridge)
        echo "bridge" > "$STATE_FILE"
        # Bell to notify (xbar refresh is automatic on next interval)
        osascript -e 'display notification "Bridge mode ON. Open new terminal for new claude sessions to route through DeepSeek." with title "Claude Bridge"' 2>/dev/null || true
        exit 0
        ;;
    toggle-anthropic)
        echo "anthropic" > "$STATE_FILE"
        osascript -e 'display notification "Anthropic mode ON. Open new terminal for new claude sessions to use Anthropic API directly." with title "Claude Bridge"' 2>/dev/null || true
        exit 0
        ;;
    test-bridge)
        # Probe bridge endpoint with simple ping
        result=$(curl -sf --max-time 30 -X POST "$BRIDGE_URL/v1/messages" \
            -H 'Content-Type: application/json' \
            -d '{"model":"claude-sonnet-4-6","max_tokens":20,"messages":[{"role":"user","content":"Reply with the single word PONG."}]}' \
            2>&1 || echo "ERROR: $?")
        osascript -e "display notification \"$result\" with title \"Bridge Test\"" 2>/dev/null || true
        exit 0
        ;;
    open-shell-bridge)
        [ -z "$REPO" ] && REPO="$HOME"
        osascript <<EOF 2>/dev/null
tell application "Terminal"
    activate
    do script "cd $REPO && export ANTHROPIC_BASE_URL=$BRIDGE_URL/v1 && echo '🔵 Claude Code → DeepSeek Bridge' && claude"
end tell
EOF
        exit 0
        ;;
    open-shell-anthropic)
        [ -z "$REPO" ] && REPO="$HOME"
        osascript <<EOF 2>/dev/null
tell application "Terminal"
    activate
    do script "cd $REPO && unset ANTHROPIC_BASE_URL && echo '🟢 Claude Code → Anthropic API' && claude"
end tell
EOF
        exit 0
        ;;
esac

# ── Render menubar (default action: no $1) ────────────────────────────────

# Snapshot daemon /health once (≤2s budget, fail-soft to "").
HEALTH_JSON=""
DAEMON_OK=false
if HEALTH_JSON="$(curl -sf --max-time 2 "$BRIDGE_URL/health" 2>/dev/null)"; then
    DAEMON_OK=true
fi

# Extract a counter from the JSON snapshot. Fail-soft: returns 0 if missing
# or if the daemon is down. We avoid jq dependency — small grep is enough
# because keys are unique under stats. Falls back to python if available.
counter() {
    local key="$1"
    [ -z "$HEALTH_JSON" ] && { echo 0; return; }
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(int((d.get('stats') or {}).get('$key', 0) or 0))
except Exception:
    print(0)
" <<<"$HEALTH_JSON" 2>/dev/null || echo 0
    else
        # Last-resort regex (less robust, fine for integer counters)
        echo "$HEALTH_JSON" | grep -oE "\"$key\"[[:space:]]*:[[:space:]]*[0-9]+" | head -1 | grep -oE '[0-9]+$' || echo 0
    fi
}

# Pull all counters once.
C_ANT_OK=$(counter bridge_anthropic_ok)
C_FALLBACK=$(counter bridge_fallback_to_deepseek)
C_SKIPPED=$(counter bridge_anthropic_skipped)
C_PASS_ERR=$(counter bridge_anthropic_passthrough_errors)
C_DS_FAIL=$(counter bridge_deepseek_failures)

# Cycle 4 / D2 — Anthropic rate-limit gauges. Daemon publishes these on every
# Anthropic upstream call; -1 = "no Anthropic call has landed yet" (counter()
# returns 0 on missing key, so the diff between "never seen" and "exhausted"
# is detected with a separate signed-int helper below).
signed_counter() {
    # Returns the int value or -1 if missing/daemon-down. Distinct from
    # counter() which collapses missing → 0 (would lie about quota state).
    local key="$1"
    [ -z "$HEALTH_JSON" ] && { echo -1; return; }
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    s = (d.get('stats') or {})
    v = s.get('$key')
    print(int(v) if v is not None else -1)
except Exception:
    print(-1)
" <<<"$HEALTH_JSON" 2>/dev/null || echo -1
    else
        echo "$HEALTH_JSON" | grep -oE "\"$key\"[[:space:]]*:[[:space:]]*-?[0-9]+" | head -1 | grep -oE '\-?[0-9]+$' || echo -1
    fi
}

RL_REQ_REMAIN=$(signed_counter bridge_ratelimit_requests_remaining)
RL_TOK_REMAIN=$(signed_counter bridge_ratelimit_tokens_remaining)
RL_IN_TOK_REMAIN=$(signed_counter bridge_ratelimit_input_tokens_remaining)
RL_OUT_TOK_REMAIN=$(signed_counter bridge_ratelimit_output_tokens_remaining)
RL_REQ_RESET=$(signed_counter bridge_ratelimit_requests_reset_epoch)
RL_TOK_RESET=$(signed_counter bridge_ratelimit_tokens_reset_epoch)
RL_LAST_429=$(signed_counter bridge_ratelimit_last_429_epoch)
RL_RETRY_AFTER=$(signed_counter bridge_ratelimit_last_retry_after_seconds)

# Quota state: low when <50 requests remaining OR reset is within 30 min.
NOW_EPOCH=$(date +%s)
QUOTA_LOW=false
if [ "$RL_REQ_REMAIN" -ge 0 ] && [ "$RL_REQ_REMAIN" -lt 50 ]; then
    QUOTA_LOW=true
fi
# Soonest reset across both buckets — if either is within 30 min, flag low.
SOON_RESET=-1
for r in "$RL_REQ_RESET" "$RL_TOK_RESET"; do
    if [ "$r" -gt 0 ] && [ "$r" -gt "$NOW_EPOCH" ]; then
        if [ "$SOON_RESET" -lt 0 ] || [ "$r" -lt "$SOON_RESET" ]; then
            SOON_RESET=$r
        fi
    fi
done
if [ "$SOON_RESET" -gt 0 ]; then
    DELTA=$(( SOON_RESET - NOW_EPOCH ))
    if [ "$DELTA" -lt 1800 ] && [ "$DELTA" -gt 0 ]; then
        QUOTA_LOW=true
    fi
fi

# Format the soonest reset as HH:MM (local) — fail-soft to "?".
fmt_reset_hhmm() {
    local epoch="$1"
    [ "$epoch" -le 0 ] && { echo "?"; return; }
    # macOS BSD date uses -r for epoch input; GNU date uses -d @epoch.
    date -r "$epoch" "+%H:%M" 2>/dev/null || date -d "@$epoch" "+%H:%M" 2>/dev/null || echo "?"
}
RESET_HHMM="?"
[ "$SOON_RESET" -gt 0 ] && RESET_HHMM=$(fmt_reset_hhmm "$SOON_RESET")

# MIX detector: in ANT mode but bridge counters are climbing (or in BRG mode but
# anthropic_ok is climbing). Either way it means *some* claude session is on the
# wrong rail. We expose the count of "wrong-rail" requests so Aaron can see scale.
# Persist last-seen counters in a tiny scratch file so we compute deltas across
# the 60s xbar refresh window.
SNAPSHOT_FILE="$HOME/.fleet-bridge-mode.last-counters"
PREV_ANT_OK=0; PREV_FALLBACK=0; PREV_SKIPPED=0
if [ -f "$SNAPSHOT_FILE" ]; then
    # shellcheck disable=SC1090
    . "$SNAPSHOT_FILE" 2>/dev/null || true
fi
DELTA_ANT_OK=$(( C_ANT_OK - PREV_ANT_OK ))
DELTA_FALLBACK=$(( C_FALLBACK - PREV_FALLBACK ))
DELTA_SKIPPED=$(( C_SKIPPED - PREV_SKIPPED ))
[ $DELTA_ANT_OK -lt 0 ] && DELTA_ANT_OK=0   # daemon restart resets stats
[ $DELTA_FALLBACK -lt 0 ] && DELTA_FALLBACK=0
[ $DELTA_SKIPPED -lt 0 ] && DELTA_SKIPPED=0

# Persist current snapshot for next refresh.
cat >"$SNAPSHOT_FILE" <<EOF
PREV_ANT_OK=$C_ANT_OK
PREV_FALLBACK=$C_FALLBACK
PREV_SKIPPED=$C_SKIPPED
EOF

# Compute MIX count: requests on the *wrong* rail in the last refresh window.
# In ANT mode, any bridge activity is "wrong rail".
# In BRG mode, an anthropic_ok delta means a session is bypassing the bridge.
MIX_COUNT=0
if [ "$MODE" = "anthropic" ]; then
    MIX_COUNT=$(( DELTA_FALLBACK + DELTA_SKIPPED ))
else
    MIX_COUNT=$DELTA_ANT_OK
fi

# Menubar badge: 5 states (Cycle 4 / D2 added 🟠 QUOTA-LOW).
#   🟢 ANT      — anthropic mode, no bridge traffic detected
#   🔵 BRG      — bridge mode, daemon healthy
#   🟡 MIX(N)   — split traffic detected (wrong-rail requests this window)
#   🔴 BRG!     — bridge mode but daemon unreachable
#   🟠 LOW(R/T) — Anthropic ratelimit headers say <50 req remaining or reset
#                 within 30 min. Tooltip shows exact "X req / Y tokens until HH:MM".
if [ "$MODE" = "bridge" ] && [ "$DAEMON_OK" = "false" ]; then
    echo "🔴 BRG! | color=red"
elif [ "$MIX_COUNT" -gt 0 ]; then
    echo "🟡 MIX($MIX_COUNT) | color=#eab308"
elif [ "$QUOTA_LOW" = "true" ]; then
    # Tooltip carries quota detail; menubar text stays compact.
    TOOLTIP="$RL_REQ_REMAIN req / $RL_TOK_REMAIN tokens until reset $RESET_HHMM"
    echo "🟠 LOW($RL_REQ_REMAIN/$RL_TOK_REMAIN) | color=#f97316 tooltip='$TOOLTIP'"
elif [ "$MODE" = "bridge" ]; then
    echo "🔵 BRG | color=#3b82f6"
else
    echo "🟢 ANT | color=#10b981"
fi
echo "---"

# Dropdown — current state
if [ "$MODE" = "bridge" ]; then
    echo "Mode: BRIDGE (Claude Code → DeepSeek)"
    if [ "$DAEMON_OK" = "true" ]; then
        echo "Bridge daemon: OK at $BRIDGE_URL | color=green"
    else
        echo "⚠️  Bridge daemon UNREACHABLE — claude sessions will fail | color=red"
        echo "Start fleet daemon to restore | color=red"
    fi
else
    echo "Mode: ANTHROPIC (Claude Code → api.anthropic.com)"
    echo "Burns Anthropic quota normally | color=#666"
fi

# MIX warning detail
if [ "$MIX_COUNT" -gt 0 ]; then
    echo "---"
    if [ "$MODE" = "anthropic" ]; then
        echo "⚠️  MIX: $MIX_COUNT bridge request(s) this window — some session is using ANTHROPIC_BASE_URL | color=#eab308"
    else
        echo "⚠️  MIX: $MIX_COUNT anthropic request(s) this window — some session bypassed the bridge | color=#eab308"
    fi
    echo "Open a fresh terminal so new sessions inherit the right env | color=#888"
fi
echo "---"

# Toggle actions
if [ "$MODE" = "bridge" ]; then
    echo "Switch to ANTHROPIC mode | bash='$SCRIPT_PATH' param1=toggle-anthropic terminal=false refresh=true"
else
    echo "Switch to BRIDGE mode | bash='$SCRIPT_PATH' param1=toggle-bridge terminal=false refresh=true"
fi
echo "---"

# Quick-launch new claude session in either mode
echo "Open new claude session…"
echo "--🔵 …in BRIDGE mode (DeepSeek) | bash='$SCRIPT_PATH' param1=open-shell-bridge terminal=false"
echo "--🟢 …in ANTHROPIC mode (direct) | bash='$SCRIPT_PATH' param1=open-shell-anthropic terminal=false"
echo "---"

# Anthropic rate-limit quota (Cycle 4 / D2 — live from response headers)
echo "Anthropic quota (live headers)"
if [ "$DAEMON_OK" = "true" ]; then
    if [ "$RL_REQ_REMAIN" -lt 0 ] && [ "$RL_TOK_REMAIN" -lt 0 ]; then
        echo "--no Anthropic call observed yet | color=#888 font=Menlo"
    else
        # Color-code the remaining-requests line by severity.
        REQ_COLOR="#10b981"  # green
        if [ "$RL_REQ_REMAIN" -ge 0 ] && [ "$RL_REQ_REMAIN" -lt 50 ]; then
            REQ_COLOR="#f97316"  # orange
        fi
        if [ "$RL_REQ_REMAIN" -eq 0 ]; then
            REQ_COLOR="red"
        fi
        echo "--requests_remaining: $RL_REQ_REMAIN | color=$REQ_COLOR font=Menlo"
        echo "--tokens_remaining: $RL_TOK_REMAIN | color=#3b82f6 font=Menlo"
        echo "--input_tokens_remaining: $RL_IN_TOK_REMAIN | color=#666 font=Menlo"
        echo "--output_tokens_remaining: $RL_OUT_TOK_REMAIN | color=#666 font=Menlo"
        if [ "$SOON_RESET" -gt 0 ]; then
            DELTA_MIN=$(( (SOON_RESET - NOW_EPOCH) / 60 ))
            echo "--next reset: $RESET_HHMM (in ${DELTA_MIN}m) | color=#888 font=Menlo"
        fi
        if [ "$RL_LAST_429" -gt 0 ]; then
            AGO=$(( NOW_EPOCH - RL_LAST_429 ))
            RA="?"
            [ "$RL_RETRY_AFTER" -ge 0 ] && RA="${RL_RETRY_AFTER}s"
            echo "--last 429: ${AGO}s ago (retry-after $RA) | color=red font=Menlo"
        fi
    fi
else
    echo "--daemon down — quota unavailable | color=red"
fi
echo "---"

# Counter dump (live from /health.stats)
echo "Bridge counters (cumulative)"
if [ "$DAEMON_OK" = "true" ]; then
    echo "--anthropic_ok: $C_ANT_OK | color=#10b981 font=Menlo"
    echo "--fallback_to_deepseek: $C_FALLBACK | color=#3b82f6 font=Menlo"
    echo "--anthropic_skipped: $C_SKIPPED | color=#888 font=Menlo"
    echo "--passthrough_errors: $C_PASS_ERR | color=#eab308 font=Menlo"
    echo "--deepseek_failures: $C_DS_FAIL | color=red font=Menlo"
    echo "-----"
    echo "--Δ this window — ant_ok:$DELTA_ANT_OK fallback:$DELTA_FALLBACK skipped:$DELTA_SKIPPED | color=#888 font=Menlo"
else
    echo "--daemon down — counters unavailable | color=red"
fi
echo "---"

# Diagnostics
echo "Test bridge now (curl /v1/messages) | bash='$SCRIPT_PATH' param1=test-bridge terminal=false refresh=true"
echo "State file: $STATE_FILE | color=#888"
echo "Snapshot file: $SNAPSHOT_FILE | color=#888"
echo "---"
echo "Refresh | refresh=true"
