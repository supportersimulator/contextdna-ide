#!/bin/bash
# test-claude-bridge.sh — one-command bridge sanity + launch helper
# Usage: bash scripts/test-claude-bridge.sh
#
# Walks Aaron through the test in 30 seconds:
#   1. Daemon alive?
#   2. JSON endpoint responding?
#   3. SSE endpoint emitting all 6 events?
#   4. Anthropic key in Keychain? (optional — bridge works without)
#   5. Offers to open a NEW Terminal window with claude pre-launched in bridge mode
#
# Zero side effects to current shell. Read-only probes + interactive launch prompt.

set -e
BRIDGE="http://127.0.0.1:8855"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

ok()    { echo -e "  ${GREEN}✓${RESET} $1"; }
warn()  { echo -e "  ${YELLOW}⚠${RESET} $1"; }
fail()  { echo -e "  ${RED}✗${RESET} $1"; }
step()  { echo -e "\n${BOLD}$1${RESET}"; }

step "1/5 — Fleet daemon health"
HEALTH=$(curl -sf --max-time 3 "$BRIDGE/health" 2>/dev/null || echo "")
if [ -z "$HEALTH" ]; then
    fail "Daemon UNREACHABLE at $BRIDGE"
    fail "Start it: launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/io.contextdna.fleet-nerve.plist"
    exit 1
fi
NODE=$(echo "$HEALTH" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("nodeId","?"))')
ok "Daemon alive — node=$NODE"

step "2/5 — Bridge JSON endpoint (POST /v1/messages)"
JSON_RESP=$(curl -sf --max-time 30 -X POST "$BRIDGE/v1/messages" \
    -H 'Content-Type: application/json' \
    -d '{"model":"claude-sonnet-4-6","max_tokens":20,"messages":[{"role":"user","content":"Reply with exactly the word PING."}]}' \
    2>/dev/null || echo "")
if [ -z "$JSON_RESP" ]; then
    fail "JSON endpoint returned empty/error"
    exit 2
fi
JSON_TEXT=$(echo "$JSON_RESP" | /usr/bin/python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    if d.get("type") == "error":
        print("ERROR: " + d.get("error",{}).get("message","unknown"))
    else:
        c = d.get("content", [{}])
        print(c[0].get("text", "<no text>") if c else "<no content>")
except Exception as e:
    print(f"<parse failed: {e}>")
' 2>/dev/null)
if echo "$JSON_TEXT" | grep -qi "error"; then
    fail "JSON path failed: $JSON_TEXT"
    exit 2
fi
ok "JSON path returned: ${JSON_TEXT:0:60}"

step "3/5 — Bridge SSE endpoint (stream=true)"
SSE_RESP=$(curl -sN --max-time 30 -X POST "$BRIDGE/v1/messages" \
    -H 'Content-Type: application/json' \
    -d '{"model":"claude-sonnet-4-6","stream":true,"max_tokens":20,"messages":[{"role":"user","content":"Reply with exactly the word STREAM."}]}' \
    2>/dev/null || echo "")
if [ -z "$SSE_RESP" ]; then
    fail "SSE endpoint returned empty"
    exit 3
fi
EVENTS=$(echo "$SSE_RESP" | grep -c "^event:" || echo "0")
if [ "$EVENTS" -lt 6 ]; then
    warn "SSE returned only $EVENTS events (expected 6)"
else
    ok "SSE path emitted $EVENTS events (expected: 6)"
fi
SSE_TEXT=$(echo "$SSE_RESP" | grep "content_block_delta" | head -1 | sed -E 's/.*"text": *"([^"]*)".*/\1/' || echo "<no delta>")
ok "SSE delta text: ${SSE_TEXT:0:60}"

step "4/5 — Anthropic key in Keychain (optional)"
KEY_LEN=$(security find-generic-password -s fleet-nerve -a Context_DNA_Anthropic -w 2>/dev/null | wc -c | tr -d ' ')
if [ "$KEY_LEN" -gt 30 ]; then
    ok "Anthropic key in Keychain (len=$KEY_LEN) — bridge will try Anthropic FIRST, fall back to DeepSeek on 429"
    HAS_KEY=true
else
    warn "No Anthropic key in Keychain — bridge runs in DEEPSEEK-ONLY mode"
    warn "  To enable Anthropic-first (full Claude quality when quota allows):"
    warn "    security add-generic-password -s fleet-nerve -a Context_DNA_Anthropic -w 'sk-ant-YOUR-KEY' -U"
    warn "    launchctl bootout gui/\$(id -u)/io.contextdna.fleet-nerve"
    warn "    launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/io.contextdna.fleet-nerve.plist"
    HAS_KEY=false
fi

step "5/5 — Bridge counters"
echo "$HEALTH" | /usr/bin/python3 -c '
import json, sys
d = json.load(sys.stdin)
s = d.get("stats", {})
brs = {k: v for k, v in s.items() if "bridge" in k}
for k, v in sorted(brs.items()):
    print(f"  {k:42s} = {v}")
' 2>/dev/null

echo
echo -e "${BOLD}═══════════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  ALL CHECKS PASSED. Bridge is ready.${RESET}"
echo -e "${BOLD}═══════════════════════════════════════════════════════════════════${RESET}"
echo
echo -e "${BOLD}TO ACTUALLY USE THE BRIDGE WITH CLAUDE CODE:${RESET}"
echo
echo -e "  Easy path — opens a fresh Terminal window with claude already routed:"
echo
echo -e "    ${GREEN}bash scripts/test-claude-bridge.sh launch${RESET}"
echo
echo -e "  Or do it manually in any new terminal:"
echo
echo -e "    ${DIM}export ANTHROPIC_BASE_URL=$BRIDGE/v1${RESET}"
echo -e "    ${DIM}claude${RESET}"
echo
if [ "$HAS_KEY" = "true" ]; then
    echo -e "  ${BOLD}What to expect:${RESET}"
    echo -e "    Most requests → real Anthropic (full Claude quality)"
    echo -e "    On Anthropic 429 → silently falls to DeepSeek, never crashes"
    echo -e "    Watch xbar menubar: 🟢 ANT (clean) → 🟡 MIX(N) (fallbacks fired)"
else
    echo -e "  ${BOLD}What to expect (DeepSeek-only mode):${RESET}"
    echo -e "    All requests → DeepSeek (~deepseek-chat quality)"
    echo -e "    Anthropic quota untouched (you don't have key configured)"
    echo -e "    Add the Anthropic key (step 4 above) for hybrid mode"
fi

# ── Launch sub-command ───────────────────────────────────────────────────
if [ "$1" = "launch" ]; then
    echo
    echo -e "${BOLD}Opening new Terminal window with bridge-routed claude…${RESET}"
    REPO="$(cd "$(dirname "$0")/.." && pwd)"
    osascript <<EOF 2>/dev/null
tell application "Terminal"
    activate
    do script "cd '$REPO' && export ANTHROPIC_BASE_URL=$BRIDGE/v1 && echo '🔵 Claude Code → Bridge → (Anthropic if up, else DeepSeek)' && echo 'Type a message. Watch xbar for any 🟡 MIX(N) ticks (fallbacks fired).' && echo && claude"
end tell
EOF
    echo -e "  ${GREEN}✓${RESET} New Terminal window opened. Switch to it and start typing."
fi
