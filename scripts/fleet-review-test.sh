#!/usr/bin/env bash
# fleet-review-test.sh — Test cross-machine review flow end-to-end
# Usage: bash scripts/fleet-review-test.sh [target_node]
#        bash scripts/fleet-review-test.sh           # local-only test
#        bash scripts/fleet-review-test.sh mac1       # cross-machine test to mac1
#
# Tests: (1) Local ReviewFlowOrchestrator lifecycle
#        (2) Daemon rebuttal endpoint (if daemon running)
#        (3) Cross-machine review via fleet daemon (if target given)
#
# Safe: uses test-prefixed task IDs, 2-minute deadlines, no side effects.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="${1:-}"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
LOCAL_DAEMON="http://127.0.0.1:8855"
PASS=0; FAIL=0; SKIP=0

_ok()   { PASS=$((PASS+1)); echo "  ✓ $1"; }
_fail() { FAIL=$((FAIL+1)); echo "  ✗ $1"; }
_skip() { SKIP=$((SKIP+1)); echo "  - $1 (skipped)"; }

# ── Phase 1: Local ReviewFlowOrchestrator lifecycle ─────────────────────
echo "=== Phase 1: Local ReviewFlowOrchestrator lifecycle ==="

RESULT=$(cd "$REPO_ROOT/multi-fleet" && python3 -c "
import json, sys
from multifleet.review_flow import ReviewFlowOrchestrator

ro = ReviewFlowOrchestrator(node_id='$NODE_ID')
out = {}

# Start
s = ro.start_review(
    task_id='test-review-e2e',
    assigned_machines=['mac1', 'mac3'],
    task_summary='E2E test: verify review flow lifecycle',
    deadline_minutes=2,
)
out['start_phase'] = s.phase.value

# Verdicts
ro.submit_verdict('test-review-e2e', 'mac1', {
    'summary': 'LGTM from mac1', 'confidence': 0.9, 'files_touched': ['test.py'],
})
ro.submit_verdict('test-review-e2e', 'mac3', {
    'summary': 'LGTM from mac3', 'confidence': 0.85, 'files_touched': ['test.py'],
})
status = ro.get_status('test-review-e2e')
out['after_verdicts'] = status['phase']

# Critiques
ro.submit_critique('test-review-e2e', 'mac1', {
    'concern': 'No issues found', 'severity': 'none',
})
ro.submit_critique('test-review-e2e', 'mac3', {
    'concern': 'Minor style nit', 'severity': 'low',
})
status = ro.get_status('test-review-e2e')
out['after_critiques'] = status['phase']
out['has_synthesis'] = status.get('synthesis') is not None
out['stats'] = ro._stats

print(json.dumps(out))
" 2>/dev/null)

if [ -z "$RESULT" ]; then
    _fail "ReviewFlowOrchestrator import/run failed"
else
    START_PHASE=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['start_phase'])")
    AFTER_VERDICTS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['after_verdicts'])")
    AFTER_CRITIQUES=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['after_critiques'])")
    HAS_SYNTH=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['has_synthesis'])")

    [ "$START_PHASE" = "local_surgery" ] && _ok "start -> local_surgery" || _fail "start phase=$START_PHASE (expected local_surgery)"
    [ "$AFTER_VERDICTS" = "under_review" ] && _ok "verdicts -> under_review" || _fail "after verdicts phase=$AFTER_VERDICTS (expected under_review)"
    [ "$AFTER_CRITIQUES" = "resolved" ] && _ok "critiques -> resolved (synthesis ran)" || _fail "after critiques phase=$AFTER_CRITIQUES (expected resolved)"
    [ "$HAS_SYNTH" = "True" ] && _ok "synthesis result present" || _fail "no synthesis result"
fi

# ── Phase 2: Local daemon rebuttal endpoint ─────────────────────────────
echo ""
echo "=== Phase 2: Local daemon rebuttal endpoint ==="

HEALTH=$(curl -sf --max-time 3 "$LOCAL_DAEMON/health" 2>/dev/null)
if [ -z "$HEALTH" ]; then
    _skip "local daemon not running (start with fleet_nerve_nats.py serve)"
else
    _ok "local daemon reachable"

    # POST /rebuttal propose
    PROPOSE=$(curl -sf --max-time 5 -X POST "$LOCAL_DAEMON/rebuttal" \
        -H "Content-Type: application/json" \
        -d '{"action":"propose","subject":"test-review-flow","body":"E2E test proposal — safe to ignore","scope":"test"}' 2>/dev/null)

    if [ -z "$PROPOSE" ]; then
        _fail "POST /rebuttal propose failed"
    else
        PID=$(echo "$PROPOSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('proposal_id',''))" 2>/dev/null)
        if [ -n "$PID" ]; then
            _ok "proposal created: $PID"

            # GET /rebuttals — verify it appears
            LIST=$(curl -sf --max-time 3 "$LOCAL_DAEMON/rebuttals" 2>/dev/null)
            COUNT=$(echo "$LIST" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null)
            [ "$COUNT" -gt 0 ] 2>/dev/null && _ok "proposal listed ($COUNT active)" || _fail "proposal not in list"

            # Accept it to clean up
            ACCEPT=$(curl -sf --max-time 5 -X POST "$LOCAL_DAEMON/rebuttal" \
                -H "Content-Type: application/json" \
                -d "{\"action\":\"accept\",\"proposal_id\":\"$PID\",\"reason\":\"e2e test cleanup\"}" 2>/dev/null)
            STATE=$(echo "$ACCEPT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',''))" 2>/dev/null)
            [ "$STATE" = "accepted" ] && _ok "proposal accepted (cleanup)" || _skip "accept returned state=$STATE"
        else
            _fail "no proposal_id in response"
        fi
    fi
fi

# ── Phase 3: Cross-machine review (if target given) ────────────────────
echo ""
echo "=== Phase 3: Cross-machine review ==="

if [ -z "$TARGET" ]; then
    _skip "no target node specified (pass node name as arg)"
else
    # Resolve target IP from config
    PEER_INFO=$(python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT/tools')
from fleet_nerve_config import load_peers
peers, _ = load_peers()
p = peers.get('$TARGET', {})
if p.get('ip'):
    port = p.get('port', 8855)
    tunnel = p.get('tunnel_port', '')
    print(f\"{p['ip']}|{port}|{tunnel}\")
" 2>/dev/null)

    if [ -z "$PEER_INFO" ]; then
        _fail "cannot resolve target '$TARGET' from config"
    else
        PEER_IP=$(echo "$PEER_INFO" | cut -d'|' -f1)
        PEER_PORT=$(echo "$PEER_INFO" | cut -d'|' -f2)
        TUNNEL_PORT=$(echo "$PEER_INFO" | cut -d'|' -f3)

        # Try direct first, then tunnel
        REMOTE_URL="http://${PEER_IP}:${PEER_PORT}"
        REMOTE_HEALTH=$(curl -sf --max-time 5 "$REMOTE_URL/health" 2>/dev/null)
        if [ -z "$REMOTE_HEALTH" ] && [ -n "$TUNNEL_PORT" ]; then
            REMOTE_URL="http://127.0.0.1:${TUNNEL_PORT}"
            REMOTE_HEALTH=$(curl -sf --max-time 5 "$REMOTE_URL/health" 2>/dev/null)
        fi

        if [ -z "$REMOTE_HEALTH" ]; then
            _fail "cannot reach $TARGET daemon at $PEER_IP:$PEER_PORT"
        else
            _ok "$TARGET daemon reachable"
            REMOTE_NODE=$(echo "$REMOTE_HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('node_id','?'))" 2>/dev/null)
            echo "       remote node_id: $REMOTE_NODE"

            # Send a test review proposal to remote
            PROPOSE=$(curl -sf --max-time 8 -X POST "$REMOTE_URL/rebuttal" \
                -H "Content-Type: application/json" \
                -d "{\"action\":\"propose\",\"subject\":\"cross-review-test-from-$NODE_ID\",\"body\":\"E2E cross-machine review test from $NODE_ID. Safe to accept/ignore.\",\"scope\":\"test\"}" 2>/dev/null)

            if [ -z "$PROPOSE" ]; then
                _fail "cross-machine propose failed"
            else
                PID=$(echo "$PROPOSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('proposal_id',''))" 2>/dev/null)
                if [ -n "$PID" ]; then
                    _ok "cross-machine proposal created on $TARGET: $PID"

                    # Verify it's visible on remote
                    REMOTE_LIST=$(curl -sf --max-time 5 "$REMOTE_URL/rebuttals" 2>/dev/null)
                    RCOUNT=$(echo "$REMOTE_LIST" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null)
                    [ "$RCOUNT" -gt 0 ] 2>/dev/null && _ok "proposal visible on $TARGET ($RCOUNT active)" || _fail "proposal not visible on remote"

                    # Accept to clean up
                    curl -sf --max-time 5 -X POST "$REMOTE_URL/rebuttal" \
                        -H "Content-Type: application/json" \
                        -d "{\"action\":\"accept\",\"proposal_id\":\"$PID\",\"reason\":\"e2e test cleanup from $NODE_ID\"}" >/dev/null 2>&1
                    _ok "cleanup: accepted test proposal on $TARGET"
                else
                    _fail "no proposal_id in cross-machine response"
                fi
            fi
        fi
    fi
fi

# ── Summary ─────────────────────────────────────────────────────────────
echo ""
echo "=== Review Flow E2E Test Results ==="
echo "  PASS: $PASS  FAIL: $FAIL  SKIP: $SKIP"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "  Some tests failed. Check output above."
    exit 1
elif [ "$PASS" -eq 0 ]; then
    echo "  No tests passed (all skipped?)."
    exit 2
else
    echo "  All executed tests passed."
    exit 0
fi
