#!/usr/bin/env bash
# mac1-auto-diagnose-on-mismatch.sh — run on mac2 (invoked by fleet-check.sh).
# If mac1 appears offline via fleet-nerve for >1hr BUT has pushed git commits within last 10min,
# that's a "mac1 alive via P7 only" mismatch → send a P7 message asking mac1 to self-diagnose.
# Idempotent: won't re-request if an un-ACKed request is already queued.

set -uo pipefail
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO"

NODE="mac1"
NERVE_HEALTH_URL="${NERVE_HEALTH_URL:-http://127.0.0.1:8855/health}"
OFFLINE_THRESHOLD_SEC=3600   # 1hr
RECENT_PUSH_WINDOW_SEC=600   # 10min
MSG_DIR="$REPO/.fleet-messages/$NODE"
FLAG_FILE="$REPO/.fleet-messages/archive/.mac1-diag-requested"

mkdir -p "$MSG_DIR" "$REPO/.fleet-messages/archive"

# 1. Check fleet-nerve view of mac1 — offline for how long?
nerve_json=$(curl -sf --max-time 3 "$NERVE_HEALTH_URL" 2>/dev/null || echo '{}')

# naive extraction: look for mac1 last_seen seconds field; skip if absent
mac1_last_seen_age=$(printf '%s' "$nerve_json" | python3 -c '
import json, sys, time
try:
    d = json.load(sys.stdin)
    peers = d.get("peers", {}) or d.get("nodes", {}) or {}
    m1 = peers.get("mac1", {})
    ts = m1.get("last_seen_ts") or m1.get("last_seen") or 0
    if isinstance(ts, str):
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
        except Exception:
            ts = 0
    age = int(time.time() - float(ts)) if ts else 99999
    print(age)
except Exception:
    print(99999)
' 2>/dev/null || echo 99999)

if [ "$mac1_last_seen_age" -lt "$OFFLINE_THRESHOLD_SEC" ]; then
  echo "mac1 online via nerve (age ${mac1_last_seen_age}s) — no diagnostic needed"
  exit 0
fi

# 2. Check recent git commits from mac1
git fetch origin main --quiet 2>/dev/null || true
recent_mac1_commit_age=$(git log origin/main --since="${RECENT_PUSH_WINDOW_SEC} seconds ago" \
  --grep="mac1" --format="%ct" 2>/dev/null | head -1)

if [ -z "$recent_mac1_commit_age" ]; then
  echo "mac1 offline AND no recent git activity — likely fully asleep. Skip P7 nudge."
  exit 0
fi

now=$(date +%s)
commit_age=$((now - recent_mac1_commit_age))
if [ "$commit_age" -gt "$RECENT_PUSH_WINDOW_SEC" ]; then
  echo "mac1 last commit ${commit_age}s ago — outside window. Skip."
  exit 0
fi

# 3. Mismatch detected. Check flag to avoid spam.
if [ -f "$FLAG_FILE" ]; then
  flag_age=$(( now - $(stat -f %m "$FLAG_FILE" 2>/dev/null || echo 0) ))
  if [ "$flag_age" -lt 3600 ]; then
    echo "diagnostic already requested ${flag_age}s ago — skipping re-request"
    exit 0
  fi
fi

# 4. Compose request P7
TS=$(date -u +%Y%m%d-%H%M%SZ)
REQ="$MSG_DIR/${TS}-auto-diagnostic-request.md"
cat > "$REQ" <<EOF
# mac1 — auto-requested self-diagnostic

**From:** mac2 (auto-detected mismatch at $(date -u +%Y-%m-%dT%H:%M:%SZ))
**Trigger:** fleet-nerve shows mac1 offline (${mac1_last_seen_age}s) but git push seen ${commit_age}s ago

## Action

Please run (non-destructive, read-only):

\`\`\`bash
bash scripts/mac1-self-diagnostic.sh
\`\`\`

It will commit + push a diagnostic report so mac2 can understand the daemon/network split.
EOF

touch "$FLAG_FILE"

echo "queued auto-diagnostic request: $REQ"
echo "(not committed — batched by operator or fleet sync)"
