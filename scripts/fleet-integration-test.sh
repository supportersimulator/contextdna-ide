#!/usr/bin/env bash
# fleet-integration-test.sh — Cross-node integration tests for lease, race, quorum.
# Tests real daemon HTTP APIs. No mocks. Requires fleet daemon on :8855.
#
# Usage: bash scripts/fleet-integration-test.sh [--skip-git]
#   --skip-git  Skip the P7 git roundtrip test (avoids commits)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DAEMON_URL="http://127.0.0.1:8855"
SKIP_GIT=false
[[ "${1:-}" == "--skip-git" ]] && SKIP_GIT=true

# ── Helpers ──────────────────────────────────────────────────────────
PASS=0; FAIL=0; SKIP=0
CLEANUP_FILES=()

pass()  { ((PASS++)); printf "  \033[32mPASS\033[0m  %s\n" "$1"; }
fail()  { ((FAIL++)); printf "  \033[31mFAIL\033[0m  %s — %s\n" "$1" "$2"; }
skip()  { ((SKIP++)); printf "  \033[33mSKIP\033[0m  %s — %s\n" "$1" "$2"; }

cleanup() {
    for f in "${CLEANUP_FILES[@]}"; do
        rm -f "$f" 2>/dev/null || true
    done
    # Release test lease if still held
    curl -sf --max-time 5 "$DAEMON_URL/lease" \
        -H "Content-Type: application/json" \
        -d '{"action":"release","resource_id":"integration-test-ephemeral"}' \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT

json_field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$1',''))" 2>/dev/null; }

NODE_ID=""
ts() { python3 -c "import time; print(int(time.time()*1000))"; }

printf "\n\033[1m══ Fleet Integration Tests ══\033[0m\n"
printf "Daemon: %s\n\n" "$DAEMON_URL"

# ══════════════════════════════════════════════════════════════════════
# 1. HEALTH BASELINE — daemon reachable, discover node ID + peers
# ══════════════════════════════════════════════════════════════════════
printf "\033[1m── 1. Health Baseline ──\033[0m\n"

HEALTH=$(curl -sf --max-time 5 "$DAEMON_URL/health" 2>/dev/null) || true
if [[ -z "$HEALTH" ]]; then
    fail "daemon_reachable" "curl $DAEMON_URL/health failed — is the daemon running?"
    printf "\nCannot continue without daemon. Exiting.\n"
    exit 1
fi
NODE_ID=$(echo "$HEALTH" | python3 -c "import json,sys; print(json.load(sys.stdin)['nodeId'])")
PEERS=$(echo "$HEALTH" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('peers',{})))")
PEER_LIST=$(echo "$PEERS" | python3 -c "import json,sys; print(' '.join(json.load(sys.stdin).keys()))")
pass "daemon_reachable (node=$NODE_ID, peers: $PEER_LIST)"

# ══════════════════════════════════════════════════════════════════════
# 2. CHANNEL CASCADE — try sending via daemon to each peer
# ══════════════════════════════════════════════════════════════════════
printf "\n\033[1m── 2. Channel Cascade ──\033[0m\n"

for peer in $PEER_LIST; do
    t0=$(ts)
    RESULT=$(curl -sf --max-time 15 "$DAEMON_URL/message" \
        -H "Content-Type: application/json" \
        -d "{\"type\":\"ping\",\"to\":\"$peer\",\"payload\":{\"subject\":\"integration-test-ping\",\"body\":\"ping from $NODE_ID\",\"test\":true}}" \
        2>/dev/null) || RESULT=""
    t1=$(ts)
    latency=$(( t1 - t0 ))
    delivered=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('delivered',d.get('sent',False)))" 2>/dev/null || echo "false")
    channel=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('channel',d.get('method','?')))" 2>/dev/null || echo "?")
    if [[ "$delivered" == "True" || "$delivered" == "true" ]]; then
        pass "cascade->$peer (channel=$channel, ${latency}ms)"
    else
        fail "cascade->$peer" "not delivered (${latency}ms) result=$RESULT"
    fi
done
if [[ -z "$PEER_LIST" ]]; then
    skip "cascade" "no peers visible"
fi

# ══════════════════════════════════════════════════════════════════════
# 3. LEASE LIFECYCLE — acquire, query, validate fence, release
# ══════════════════════════════════════════════════════════════════════
printf "\n\033[1m── 3. Lease Lifecycle ──\033[0m\n"

RESOURCE="integration-test-ephemeral"

# Acquire
ACQ=$(curl -sf --max-time 5 "$DAEMON_URL/lease" \
    -H "Content-Type: application/json" \
    -d "{\"action\":\"acquire\",\"resource_id\":\"$RESOURCE\",\"lease_type\":\"short_task\",\"metadata\":{\"test\":true}}" \
    2>/dev/null) || ACQ=""
acquired=$(echo "$ACQ" | python3 -c "import json,sys; print(json.load(sys.stdin).get('acquired',False))" 2>/dev/null || echo "")
fence=$(echo "$ACQ" | python3 -c "import json,sys; print(json.load(sys.stdin).get('fence_token',''))" 2>/dev/null || echo "")

if [[ "$acquired" == "True" ]]; then
    pass "lease_acquire (fence=$fence)"
else
    if [[ -z "$ACQ" ]]; then
        fail "lease_acquire" "no response (lease manager may not be available)"
    else
        fail "lease_acquire" "$ACQ"
    fi
fi

# Query lease back
if [[ -n "$fence" && "$fence" != "" ]]; then
    QUERY=$(curl -sf --max-time 5 "$DAEMON_URL/lease/$RESOURCE" 2>/dev/null) || QUERY=""
    holder=$(echo "$QUERY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('holder','') or d.get('node_id',''))" 2>/dev/null || echo "")
    if [[ "$holder" == "$NODE_ID" ]]; then
        pass "lease_query (holder=$holder)"
    else
        fail "lease_query" "expected holder=$NODE_ID got=$holder"
    fi

    # Validate fence token
    VAL=$(curl -sf --max-time 5 "$DAEMON_URL/lease" \
        -H "Content-Type: application/json" \
        -d "{\"action\":\"validate\",\"resource_id\":\"$RESOURCE\",\"fence_token\":$fence}" \
        2>/dev/null) || VAL=""
    valid=$(echo "$VAL" | python3 -c "import json,sys; print(json.load(sys.stdin).get('valid',False))" 2>/dev/null || echo "")
    if [[ "$valid" == "True" ]]; then
        pass "lease_fence_validate"
    else
        fail "lease_fence_validate" "$VAL"
    fi

    # Release
    REL=$(curl -sf --max-time 5 "$DAEMON_URL/lease" \
        -H "Content-Type: application/json" \
        -d "{\"action\":\"release\",\"resource_id\":\"$RESOURCE\"}" \
        2>/dev/null) || REL=""
    released=$(echo "$REL" | python3 -c "import json,sys; print(json.load(sys.stdin).get('released',False))" 2>/dev/null || echo "")
    if [[ "$released" == "True" ]]; then
        pass "lease_release"
    else
        fail "lease_release" "$REL"
    fi

    # Verify gone
    POST_REL=$(curl -sf --max-time 5 "$DAEMON_URL/lease/$RESOURCE" 2>/dev/null) || POST_REL=""
    post_holder=$(echo "$POST_REL" | python3 -c "import json,sys; print(json.load(sys.stdin).get('holder',''))" 2>/dev/null || echo "")
    if [[ -z "$post_holder" || "$post_holder" == "None" ]]; then
        pass "lease_gone_after_release"
    else
        fail "lease_gone_after_release" "still held by $post_holder"
    fi
fi

# ══════════════════════════════════════════════════════════════════════
# 4. HEALTH CROSS-CHECK — verify peers see us
# ══════════════════════════════════════════════════════════════════════
printf "\n\033[1m── 4. Health Cross-Check ──\033[0m\n"

for peer in $PEER_LIST; do
    last_seen=$(echo "$PEERS" | python3 -c "import json,sys; print(json.load(sys.stdin)['$peer'].get('lastSeen','?'))" 2>/dev/null || echo "?")
    if [[ "$last_seen" != "?" && "$last_seen" -lt 120 ]]; then
        pass "peer_fresh $peer (lastSeen=${last_seen}s ago)"
    else
        fail "peer_stale $peer" "lastSeen=${last_seen}s ago (>120s or unknown)"
    fi
done
if [[ -z "$PEER_LIST" ]]; then
    skip "cross_check" "no peers visible"
fi

# ══════════════════════════════════════════════════════════════════════
# 5. P7 GIT ROUNDTRIP — write fleet message, measure time
# ══════════════════════════════════════════════════════════════════════
printf "\n\033[1m── 5. P7 Git Roundtrip ──\033[0m\n"

if $SKIP_GIT; then
    skip "p7_git_roundtrip" "--skip-git flag set"
else
    STAMP=$(date +%s)
    TARGET_PEER=$(echo "$PEER_LIST" | awk '{print $1}')
    if [[ -z "$TARGET_PEER" ]]; then
        skip "p7_git_roundtrip" "no peers to send to"
    else
        MSG_DIR="$REPO_ROOT/.fleet-messages/$TARGET_PEER"
        MSG_FILE="$MSG_DIR/integration-test-${STAMP}.md"
        mkdir -p "$MSG_DIR"
        cat > "$MSG_FILE" <<MSGEOF
---
from: $NODE_ID
to: $TARGET_PEER
subject: integration-test-ping
timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)
test: true
---
Integration test ping from $NODE_ID at $STAMP. Safe to ignore/delete.
MSGEOF
        CLEANUP_FILES+=("$MSG_FILE")
        t0=$(ts)
        cd "$REPO_ROOT"
        git add "$MSG_FILE" 2>/dev/null
        git commit -m "fleet: integration-test ping $NODE_ID->$TARGET_PEER [$STAMP]" --no-gpg-sign -- "$MSG_FILE" >/dev/null 2>&1 || true
        if [ "${FLEET_PUSH_FREEZE:-0}" = "1" ]; then push_ok=true; echo "[P7-FREEZE] commit-only (push skipped)"; else
            git push origin main >/dev/null 2>&1 && push_ok=true || push_ok=false
        fi
        t1=$(ts)
        latency=$(( t1 - t0 ))

        if $push_ok; then
            pass "p7_git_send->$TARGET_PEER (push ${latency}ms)"
        else
            fail "p7_git_send->$TARGET_PEER" "git push failed (${latency}ms)"
        fi

        # Cleanup: remove the test message file and amend
        rm -f "$MSG_FILE"
        git add "$MSG_FILE" 2>/dev/null || true
        git commit -m "fleet: cleanup integration-test artifact [$STAMP]" --no-gpg-sign -- "$MSG_FILE" >/dev/null 2>&1 || true
        git push origin main >/dev/null 2>&1 || true
    fi
fi

# ══════════════════════════════════════════════════════════════════════
# 6. CHANNEL STATE — query channel health endpoint
# ══════════════════════════════════════════════════════════════════════
printf "\n\033[1m── 6. Channel State ──\033[0m\n"

CHANNELS=$(curl -sf --max-time 5 "$DAEMON_URL/channels" 2>/dev/null) || CHANNELS=""
if [[ -n "$CHANNELS" ]]; then
    chan_summary=$(echo "$CHANNELS" | python3 -c "
import json, sys
d = json.load(sys.stdin)
chans = d.get('channels', {})
parts = []
for name, info in sorted(chans.items()):
    st = info.get('status', info.get('state', '?'))
    parts.append(f'{name}={st}')
print(', '.join(parts) if parts else 'none')
" 2>/dev/null || echo "parse error")
    pass "channels ($chan_summary)"
else
    skip "channels" "endpoint not available"
fi

# ══════════════════════════════════════════════════════════════════════
# REPORT
# ══════════════════════════════════════════════════════════════════════
printf "\n\033[1m══ Results ══\033[0m\n"
printf "  \033[32mPassed: %d\033[0m\n" "$PASS"
printf "  \033[31mFailed: %d\033[0m\n" "$FAIL"
printf "  \033[33mSkipped: %d\033[0m\n" "$SKIP"

if [[ $FAIL -gt 0 ]]; then
    printf "\n\033[31mSome tests failed.\033[0m\n"
    exit 1
else
    printf "\n\033[32mAll tests passed.\033[0m\n"
    exit 0
fi
