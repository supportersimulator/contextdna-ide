#!/usr/bin/env bash
# fleet-inbox-hook.sh — UserPromptSubmit hook for Fleet Nerve inbox injection
#
# Checks /tmp/fleet-seed-*.md for pending fleet messages and injects them
# into the Claude Code session context. Called by UserPromptSubmit hook.
#
# Checks BOTH the fleet node ID seed file and the hostname-based seed file
# to handle the nodeId migration (macbookair → mac2 etc).
#
# Exit 0 = no messages or messages injected successfully
# Seed file is consumed (moved to archive) after injection.

set -euo pipefail

# ── Token budget gate: skip injection on auto-prompts (keep value, cut waste) ──
# Auto-prompts (fleet-check bash commands from external automation / iPad
# shortcuts / etc.) should NOT block or lose seed-file messages — those
# messages are the point of this hook. But we DO skip S0-S8 memory injection
# on auto-prompts since the user never typed anything worth warming context for.
#
# Read prompt from env var (legacy) OR stdin JSON (modern Claude Code).
PROMPT="${USER_PROMPT:-}"
if [ -z "$PROMPT" ] && [ ! -t 0 ]; then
    STDIN_BUF=$(timeout 1 cat 2>/dev/null || true)
    if [ -n "$STDIN_BUF" ]; then
        PROMPT_FROM_JSON=$(echo "$STDIN_BUF" | /usr/bin/python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get('prompt', ''), end='')
except Exception:
    pass
" 2>/dev/null || true)
        [ -n "$PROMPT_FROM_JSON" ] && PROMPT="$PROMPT_FROM_JSON"
    fi
fi

# Auto-prompt detection: one or more repetitions of `bash .../fleet-check.sh`
# Short-circuit: skip ALL S0-S8 injection (not the seed files — those flow via
# the body below if there are pending fleet messages).
IS_AUTO_PROMPT=0
if [[ "$PROMPT" =~ ^[[:space:]]*(bash[[:space:]]+[^[:space:]]*fleet-check\.sh[[:space:]]*)+$ ]] || \
   [[ "$PROMPT" =~ ^[[:space:]]*$ ]]; then
    IS_AUTO_PROMPT=1
fi

# Also rate-limit: max 1 injection per 60 seconds to prevent flood
RATE_FILE="/tmp/.fleet-inbox-last-inject"
if [ -f "$RATE_FILE" ]; then
    LAST=$(cat "$RATE_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    ELAPSED=$(( NOW - LAST ))
    if [ "$ELAPSED" -lt 60 ]; then
        exit 0
    fi
fi

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
ARCHIVE_DIR="/tmp/fleet-seed-archive"
RESULTS_DIR="/tmp/fleet-agent-results"
INJECTED=0

_header() {
    if [ "$INJECTED" = "0" ]; then
        echo ""
        echo "================================================================"
        echo "[FLEET NERVE: messages injected]"
        echo "================================================================"
        echo ""
        INJECTED=1
    fi
}

# Check ALL seed files: per-node (legacy) + per-session (daemon writes)
# The daemon's _resolve_seed_path writes to /tmp/fleet-seed-<session-id>.md
# We glob all fleet-seed-*.md to catch both naming conventions.
for SEED_FILE in /tmp/fleet-seed-*.md; do
    if [ ! -f "$SEED_FILE" ] || [ ! -s "$SEED_FILE" ]; then
        continue
    fi
    _header
    cat "$SEED_FILE"
    echo ""
    mkdir -p "$ARCHIVE_DIR"
    mv "$SEED_FILE" "${ARCHIVE_DIR}/$(date +%s)-$(basename "$SEED_FILE")"
done

# Check NATS agent results (background agent relay)
if [ -d "$RESULTS_DIR" ]; then
    UNREAD=$(python3 -c "
import json, os, sys
results_dir = '$RESULTS_DIR'
msgs = []
for f in sorted(os.listdir(results_dir)):
    if not f.endswith('.json'): continue
    try:
        d = json.load(open(os.path.join(results_dir, f)))
        if not d.get('read', False):
            msgs.append(d)
    except: pass
if not msgs:
    sys.exit(0)
for m in msgs:
    t = m.get('type','context').upper()
    fr = m.get('from','?')
    subj = m.get('subject','')
    detail = m.get('detail','')
    action = ' [ACTION NEEDED]' if m.get('action_needed') else ''
    print(f'## [{t}] from {fr}: {subj}{action}')
    print()
    if detail:
        print(detail[:1000])
    print()
    print('---')
    # Mark as read
    path = os.path.join(results_dir, f)
    m['read'] = True
    json.dump(m, open(path, 'w'), indent=2)
" 2>/dev/null)

    if [ -n "$UNREAD" ]; then
        _header
        echo "$UNREAD"
        echo ""
    fi
fi

# Update rate-limit timestamp
if [ "$INJECTED" = "1" ]; then
    date +%s > "$RATE_FILE"
fi
