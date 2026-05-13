#!/usr/bin/env bash
# discord-read-history.sh — Fetch recent Discord messages for Atlas context
#
# Usage:
#   ./scripts/discord-read-history.sh              # Last 25 messages
#   ./scripts/discord-read-history.sh 50            # Last 50 messages
#   ./scripts/discord-read-history.sh 100 "search"  # Last 100, grep for "search"
#   ./scripts/discord-read-history.sh --around <msg_id>  # Messages around a specific message
#
# Requires: DISCORD_BOT_TOKEN (env or Keychain), FLEET_DISCORD_CHANNEL_ID (env or default)

set -euo pipefail

# ── Config ──
CHANNEL_ID="${FLEET_DISCORD_CHANNEL_ID:-1491820715421466865}"
LIMIT="${1:-25}"
SEARCH_FILTER="${2:-}"

# ── Token retrieval ──
TOKEN="${DISCORD_BOT_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
    TOKEN=$(security find-generic-password -a fleet -s DISCORD_BOT_TOKEN -w 2>/dev/null || true)
fi
if [[ -z "$TOKEN" ]]; then
    echo "ERROR: No bot token. Set DISCORD_BOT_TOKEN or store in Keychain." >&2
    exit 1
fi

# ── Handle --around mode ──
if [[ "${1:-}" == "--around" ]]; then
    MSG_ID="${2:?Usage: $0 --around <message_id>}"
    URL="https://discord.com/api/v10/channels/${CHANNEL_ID}/messages?around=${MSG_ID}&limit=25"
    SEARCH_FILTER="${3:-}"
else
    # Clamp limit to Discord max (100)
    if (( LIMIT > 100 )); then LIMIT=100; fi
    URL="https://discord.com/api/v10/channels/${CHANNEL_ID}/messages?limit=${LIMIT}"
fi

# ── Fetch ──
RAW=$(curl -sf -H "Authorization: Bot ${TOKEN}" -H "Content-Type: application/json" "$URL" 2>/dev/null)
if [[ -z "$RAW" ]]; then
    echo "ERROR: Failed to fetch messages from Discord API." >&2
    exit 1
fi

# ── Format output ──
# jq processes each message into readable format, reversed (oldest first)
OUTPUT=$(echo "$RAW" | python3 -c "
import json, sys
from datetime import datetime

msgs = json.load(sys.stdin)
if not isinstance(msgs, list):
    print('ERROR: Unexpected API response', file=sys.stderr)
    sys.exit(1)

# Reverse so oldest is first (Discord returns newest first)
msgs.reverse()

for m in msgs:
    ts = m.get('timestamp', '')[:19].replace('T', ' ')
    author = m.get('author', {}).get('global_name') or m.get('author', {}).get('username', '?')
    content = m.get('content', '')

    # Handle embeds (fleet bot messages)
    embeds = m.get('embeds', [])
    embed_text = ''
    for e in embeds:
        parts = []
        if e.get('title'):
            parts.append(e['title'])
        if e.get('description'):
            parts.append(e['description'])
        for f in e.get('fields', []):
            parts.append(f'{f.get(\"name\",\"\")}: {f.get(\"value\",\"\")}')
        if e.get('footer', {}).get('text'):
            parts.append(f'({e[\"footer\"][\"text\"]})')
        embed_text += ' | '.join(parts)

    # Handle attachments
    attachments = m.get('attachments', [])
    att_text = ''
    if attachments:
        att_names = [a.get('filename', '?') for a in attachments]
        att_text = f' [attachments: {\", \".join(att_names)}]'

    # Build display line
    display = content
    if embed_text and not content:
        display = f'[embed] {embed_text}'
    elif embed_text:
        display = f'{content} [embed] {embed_text}'

    if att_text:
        display += att_text

    # Truncate very long messages
    if len(display) > 500:
        display = display[:497] + '...'

    print(f'[{ts}] {author}: {display}')
")

# ── Apply search filter ──
if [[ -n "$SEARCH_FILTER" ]]; then
    echo "=== Discord History (filtered: \"$SEARCH_FILTER\") ==="
    echo "$OUTPUT" | grep -i "$SEARCH_FILTER" || echo "(no matches)"
else
    echo "=== Discord History (last ${LIMIT} messages) ==="
    echo "$OUTPUT"
fi

echo ""
echo "--- ${LIMIT} messages from #fleet channel ---"
