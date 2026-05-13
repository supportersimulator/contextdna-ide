#!/usr/bin/env bash
# =============================================================================
# Test: scripts/fleet-coordinator.sh (DDD2)
# =============================================================================
# Cases:
#   1. --help exits 0
#   2. Unknown arg → exit 2
#   3. Healthy mock peers → no dispatches
#   4. mac2 stale → refresh-node recipe planned for mac2
#   5. mac2 missing profile → fleet-bring-up-node recipe planned for mac2
#   6. Idempotent — re-run within window dedups (only after --apply marks it)
#   7. ZSF — bogus channel_priority path → counter bumps, no crash
#   8. Plist drift signal triggers unify-cluster-urls recipe
#   9. Apply requires --target → exit 2 without target
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/fleet-coordinator.sh"

if [ ! -f "$SCRIPT" ]; then echo "FAIL: $SCRIPT missing" >&2; exit 1; fi

PASS=0; FAIL=0; CASES=0
TMPDIR="$(mktemp -d -t fleet-coordinator-test.XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

_case() {
    local name="$1"; shift
    CASES=$((CASES+1))
    if "$@"; then
        echo "  PASS: $name"; PASS=$((PASS+1))
    else
        echo "  FAIL: $name" >&2; FAIL=$((FAIL+1))
    fi
}

# Helper: write a /health-shaped mock JSON with given peers
_mock_health() {
    local outpath="$1" peers_json="$2" extra="${3:-}"
    cat > "$outpath" <<JSON
{
  "nodeId": "mac1",
  "status": "ok",
  "transport": "nats",
  "uptime_s": 100,
  "peers": ${peers_json},
  "webhook": {"events_recorded": 36, "last_webhook_age_s": 60},
  "zsf_counters": {"plist_drift": {"total": 0}},
  "cluster_state": {"status": "connected", "observed_peer_count": 2}${extra:+,${extra}}
}
JSON
}

# Each case gets a fresh dedup dir + counter file
_fresh_workdir() {
    local label="$1"
    local d="$TMPDIR/$label"
    mkdir -p "$d"
    echo "$d"
}

_run() {
    # Usage: _run <peers_json_file> <extra_args...>
    local hf="$1"; shift
    local d
    d=$(_fresh_workdir "run-$RANDOM")
    # Use an empty fleet-state-json so tests don't see the live repo state.
    local fs="$TMPDIR/empty-fleet-state.json"
    [ -f "$fs" ] || echo '{"nodes":{}}' > "$fs"
    "$SCRIPT" \
        --peers-json "$hf" \
        --fleet-state-json "$fs" \
        --dedup-dir "$d/dedup" \
        --counter-file "$d/counters.txt" \
        --dedup-window-s 21600 \
        "$@"
    local rc=$?
    # capture for later inspection by leaving paths in env
    LAST_DEDUP_DIR="$d/dedup"
    LAST_COUNTER_FILE="$d/counters.txt"
    return $rc
}

# 1. --help
case_help() { bash "$SCRIPT" --help >/dev/null 2>&1; }

# 2. unknown arg
case_unknown_arg() {
    local rc=0
    bash "$SCRIPT" --bogus >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 2 ]
}

# 3. all healthy
case_all_healthy() {
    local hf="$TMPDIR/healthy.json"
    # Both peers live (lastSeen=age=10s), profile fields irrelevant since
    # fleet-state.json absence on test paths is handled — but the test runs
    # FROM repo root where fleet-state.json has profile for mac1/mac3/cloud.
    # To isolate, run with CWD=$TMPDIR (no fleet-state.json there) AND mock
    # peers that have a fleet-state-style fake — instead we use real CWD and
    # include real profile-carrying peer names (mac3) which the script sees.
    _mock_health "$hf" '{"mac3": {"lastSeen": 10, "feasible_channels":["P1_nats"]}}'
    local out rc=0
    out=$(_run "$hf" 2>&1) || rc=$?
    [ "$rc" -eq 0 ] || { echo "      rc=$rc"; return 1; }
    if echo "$out" | grep -q "_No degradation signals detected"; then
        return 0
    fi
    # mac3 is also profile-carrying in real fleet-state.json so should pass
    if ! echo "$out" | grep -qE "(refresh-node|fleet-bring-up-node|unify-cluster|patch-nats|throttle)" ; then
        return 0
    fi
    echo "      unexpected dispatch in healthy case:"
    echo "$out" | head -20
    return 1
}

# 4. mac2 stale → refresh-node planned
case_mac2_stale() {
    local hf="$TMPDIR/mac2-stale.json"
    _mock_health "$hf" '{"mac2": {"lastSeen": null, "transport": "jetstream_only", "feasible_channels":["P1_nats"]}, "mac3": {"lastSeen": 5, "feasible_channels":["P1_nats"]}}'
    local out
    out=$(_run "$hf" --target mac2 2>&1)
    echo "$out" | grep -qE "mac2.*refresh-node" || { echo "      missing dispatch: mac2/refresh-node"; echo "$out" | head -20; return 1; }
}

# 5. mac2 missing profile → bring-up planned
#    Use an explicit fleet-state that has mac2 row but lacks profile/version
#    (the ZZ2 pattern).
case_mac2_missing_profile() {
    local hf="$TMPDIR/mac2-noprofile.json"
    local fs="$TMPDIR/mac2-noprofile-fs.json"
    _mock_health "$hf" '{"mac2": {"lastSeen": 5, "transport": "jetstream_only", "feasible_channels":["P1_nats"]}}'
    cat > "$fs" <<'JSON'
{"nodes": {"mac2": {"health": {"last_seen": "2026-05-12T15:00:00+00:00", "source": "heartbeat_mirror"}}}}
JSON
    local d
    d=$(_fresh_workdir "noprof-$RANDOM")
    local out
    out=$("$SCRIPT" --peers-json "$hf" --fleet-state-json "$fs" \
                    --dedup-dir "$d/dedup" --counter-file "$d/counters.txt" \
                    --target mac2 2>&1)
    echo "$out" | grep -q "fleet-bring-up-node" || { echo "      missing bring-up plan"; echo "$out" | head -20; return 1; }
}

# 6. Idempotency — pre-seed a marker, expect DEDUP line
case_idempotent_dedup() {
    local hf="$TMPDIR/dedup.json"
    _mock_health "$hf" '{"mac2": {"lastSeen": null, "feasible_channels":["P1_nats"]}}'
    local d
    d=$(_fresh_workdir "dedup-$RANDOM")
    mkdir -p "$d/dedup"
    # SHA256("refresh-node|mac2")
    local key
    key=$(printf "refresh-node|mac2" | shasum -a 256 | awk '{print $1}')
    touch "$d/dedup/$key"
    local fs="$TMPDIR/empty-fleet-state.json"
    [ -f "$fs" ] || echo '{"nodes":{}}' > "$fs"
    local out
    out=$("$SCRIPT" --peers-json "$hf" --fleet-state-json "$fs" \
                    --dedup-dir "$d/dedup" \
                    --counter-file "$d/counters.txt" --dedup-window-s 21600 \
                    --target mac2 2>&1)
    echo "$out" | grep -q "^DEDUP: refresh-node" || { echo "      no DEDUP line"; echo "$out" | head -20; return 1; }
    grep -q "fleet_coordinator_dispatches_deduped_total=" "$d/counters.txt" || \
        { echo "      counter not bumped"; cat "$d/counters.txt"; return 1; }
}

# 7. ZSF — bogus PYTHONPATH causes channel_priority import to fail in --apply.
#    The dispatcher must catch, bump counter, NOT crash the script.
#    Since the daemon /message endpoint is also tried, and the local mac1
#    daemon IS up, --apply may actually deliver — to keep this test fully
#    hermetic we point at a peer with no daemon AND override the port.
case_zsf_dispatch_error() {
    local hf="$TMPDIR/zsf.json"
    _mock_health "$hf" '{"nonode": {"lastSeen": null, "feasible_channels":[]}}'
    local d
    d=$(_fresh_workdir "zsf-$RANDOM")
    # Use a port no daemon listens on AND a bogus PYTHONPATH to force both
    # paths to fail. Bash MUST still exit cleanly (rc=1 = "dispatch errors";
    # never crash with rc>1).
    local rc=0
    PYTHONPATH="/nonexistent" FLEET_NERVE_PORT="59999" \
        "$SCRIPT" --peers-json "$hf" --dedup-dir "$d/dedup" \
                  --counter-file "$d/counters.txt" --dedup-window-s 21600 \
                  --apply --target nonode >/dev/null 2>&1 || rc=$?
    # rc=1 (dispatch error reported) is the correct ZSF outcome
    [ "$rc" -eq 1 ] || { echo "      expected rc=1 got $rc"; return 1; }
    grep -q "fleet_coordinator_dispatch_errors_total=" "$d/counters.txt" || \
        { echo "      error counter not bumped"; cat "$d/counters.txt"; return 1; }
}

# 8. Plist drift → unify-cluster-urls planned
case_plist_drift() {
    local hf="$TMPDIR/plist.json"
    cat > "$hf" <<'JSON'
{
  "nodeId": "mac1",
  "status": "ok",
  "peers": {"mac3": {"lastSeen": 10, "feasible_channels":["P1_nats"]}},
  "webhook": {"events_recorded": 1, "last_webhook_age_s": 60},
  "zsf_counters": {"plist_drift": {"total": 3}},
  "cluster_state": {"status": "connected", "observed_peer_count": 1}
}
JSON
    local out
    out=$(_run "$hf" --target mac3 2>&1)
    echo "$out" | grep -q "unify-cluster-urls" || { echo "      missing unify-cluster-urls plan"; echo "$out" | head -20; return 1; }
}

# 9. --apply without --target → exit 2
case_apply_requires_target() {
    local hf="$TMPDIR/applytgt.json"
    _mock_health "$hf" '{"mac2": {"lastSeen": null, "feasible_channels":["P1_nats"]}}'
    local d
    d=$(_fresh_workdir "applytgt-$RANDOM")
    local rc=0
    "$SCRIPT" --peers-json "$hf" --dedup-dir "$d/dedup" \
              --counter-file "$d/counters.txt" --apply \
              >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 2 ]
}

echo "== test_fleet_coordinator.sh =="
_case "case_help"                  case_help
_case "case_unknown_arg"           case_unknown_arg
_case "case_all_healthy"           case_all_healthy
_case "case_mac2_stale"            case_mac2_stale
_case "case_mac2_missing_profile"  case_mac2_missing_profile
_case "case_idempotent_dedup"      case_idempotent_dedup
_case "case_zsf_dispatch_error"    case_zsf_dispatch_error
_case "case_plist_drift"           case_plist_drift
_case "case_apply_requires_target" case_apply_requires_target

echo ""
echo "  Total: ${PASS}/${CASES} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
