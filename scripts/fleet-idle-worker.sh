#!/usr/bin/env bash
# fleet-idle-worker.sh — WW2 work-on-idle puller.
#
# When THIS node has been idle for N minutes, ask the FLEET_WORK_QUEUE for a
# matching task and (optionally) execute it. Default --dry-run so deployment
# is per-node opt-in: each node decides when to flip to --apply.
#
# Idle-check heuristic (multifleet.work_queue.is_node_idle):
#   • No git commit by this user/node in the last N min, AND
#   • No /health request served by the local daemon in the last N min, AND
#   • No LLM API call from this node in the last N min.
#
# Per-loop logging → /tmp/fleet-idle-worker-<node>.log (ZSF: every iteration
# emits one timestamped line + outcome).
#
# Usage:
#   bash scripts/fleet-idle-worker.sh                    # dry-run, one iteration
#   bash scripts/fleet-idle-worker.sh --apply            # claim + execute
#   bash scripts/fleet-idle-worker.sh --loop             # repeat every $INTERVAL
#   bash scripts/fleet-idle-worker.sh --node mac3 --apply --loop
#
# Aaron opt-in note: never launch this from a session that is itself
# automating mac2/mac3. The worker is meant to run as a per-node launchd
# job started by the operator on that machine.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# ── Args ─────────────────────────────────────────────────────────────────
APPLY=0
LOOP=0
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
INTERVAL_S="${FLEET_IDLE_INTERVAL_S:-300}"
IDLE_THRESHOLD_S="${FLEET_IDLE_THRESHOLD_S:-300}"
PORT="${FLEET_NERVE_PORT:-8855}"
NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --dry-run) APPLY=0; shift ;;
        --loop) LOOP=1; shift ;;
        --node) NODE_ID="$2"; shift 2 ;;
        --interval) INTERVAL_S="$2"; shift 2 ;;
        --threshold) IDLE_THRESHOLD_S="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

LOG="/tmp/fleet-idle-worker-${NODE_ID}.log"

# ── Helpers ──────────────────────────────────────────────────────────────
_log() {
    # ZSF: every iteration emits a structured line, never silent.
    local level="$1"; shift
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s [%s] [%s] %s\n' "$ts" "$NODE_ID" "$level" "$*" | tee -a "$LOG"
}

# Best-effort timestamps. Any failure → ts=0 (treated as "ancient", which
# biases toward "this node is idle" — acceptable; the dry-run gate stops
# anything destructive from happening on a false positive).

_last_commit_ts() {
    local ts
    ts="$(cd "$REPO_DIR" 2>/dev/null && git log -1 --format=%ct --author="${USER:-aaron}" 2>/dev/null || true)"
    [[ -z "$ts" ]] && ts=0
    echo "$ts"
}

_last_health_ts() {
    # /health snapshot — last request log line. Daemon may not be up on
    # this node; if curl fails return 0 (idle).
    local body
    body="$(curl -sf --max-time 2 "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)"
    if [[ -z "$body" ]]; then echo 0; return; fi
    # Daemon /health includes "uptime_s" — derive a "last_request_age_s"
    # by reading "last_request_ts" if present, else fall back to uptime so
    # an active daemon still counts as activity.
    python3 - <<'PY' 2>/dev/null <<<"$body" || echo 0
import json, sys, time
try:
    d = json.load(sys.stdin)
    ts = d.get("last_request_ts") or d.get("started_ts") or 0
    print(int(ts) if ts else 0)
except Exception:
    print(0)
PY
}

_last_llm_ts() {
    # Look for an LLM call marker file (memory/llm_priority_queue writes
    # /tmp/llm-last-call-<node> on each dispatch). Missing → 0 (idle).
    local marker="/tmp/llm-last-call-${NODE_ID}"
    if [[ -f "$marker" ]]; then
        stat -f '%m' "$marker" 2>/dev/null || stat -c '%Y' "$marker" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

# ── Capability inference ─────────────────────────────────────────────────
# We let multifleet.node_profile.detect_local_profile() decide; the python
# block below converts the profile dataclass into a list of cap tags the
# work_queue can match against.

_caps_json() {
    PYTHONPATH="$REPO_DIR/multi-fleet" python3 - "$NODE_ID" <<'PY' 2>/dev/null || echo '[]'
import json, sys
try:
    from multifleet.node_profile import detect_local_profile
    p = detect_local_profile(node_id=sys.argv[1])
    caps = [
        f"tier:{p.tier}",
        f"gpu:{p.gpu}",
        f"role:{p.role}",
        f"ram_gb:{p.ram_gb}",
    ]
    if p.ram_gb >= 32:
        caps.append("ram>=32")
    if p.ram_gb >= 64:
        caps.append("ram>=64")
    if p.is_apple_silicon():
        caps.append("apple_silicon")
    if p.can_run_local_llm():
        caps.append("local_llm")
    print(json.dumps(caps))
except Exception as e:
    print('[]')
PY
}

# ── Idle gate ────────────────────────────────────────────────────────────
_is_idle() {
    local commit_ts health_ts llm_ts
    commit_ts="$(_last_commit_ts)"
    health_ts="$(_last_health_ts)"
    llm_ts="$(_last_llm_ts)"
    PYTHONPATH="$REPO_DIR/multi-fleet" python3 - \
        "$commit_ts" "$health_ts" "$llm_ts" "$IDLE_THRESHOLD_S" <<'PY' 2>/dev/null
import sys
try:
    from multifleet.work_queue import is_node_idle
except Exception as e:
    print("ERR", e); sys.exit(2)
c, h, l, thr = (float(x) for x in sys.argv[1:5])
idle = is_node_idle(
    last_commit_ts=c or None,
    last_health_ts=h or None,
    last_llm_call_ts=l or None,
    idle_threshold_s=thr,
)
print("IDLE" if idle else "BUSY")
PY
}

# ── Pull + execute ───────────────────────────────────────────────────────
# Pull runs in a single python subprocess that owns the asyncio loop +
# NATS client. Probe-and-claim atomically; the shell only decides whether
# to surface the result (--dry-run) or hand it to a local executor.

_pull_and_claim() {
    local apply_flag="$1"
    local caps_json="$2"
    PYTHONPATH="$REPO_DIR/multi-fleet" python3 - \
        "$NODE_ID" "$NATS_URL" "$apply_flag" "$caps_json" <<'PY' 2>/dev/null
import asyncio, json, os, sys
node, nats_url, apply_flag, caps_json = sys.argv[1:5]
try:
    caps = json.loads(caps_json)
except Exception:
    caps = []
try:
    import nats  # type: ignore
except Exception as e:
    print(json.dumps({"ok": False, "reason": f"nats-py missing: {e}"}))
    sys.exit(0)
try:
    from multifleet.work_queue import pull_idle_work, get_stats
except Exception as e:
    print(json.dumps({"ok": False, "reason": f"work_queue import: {e}"}))
    sys.exit(0)

async def main():
    try:
        nc = await asyncio.wait_for(nats.connect(nats_url), timeout=5)
    except Exception as e:
        print(json.dumps({"ok": False, "reason": f"nats connect: {e}"}))
        return
    try:
        task = await pull_idle_work(nc, node_id=node, current_caps=caps)
    finally:
        try:
            await nc.drain()
        except Exception:
            pass
    if not task:
        stats = get_stats()
        print(json.dumps({"ok": True, "claimed": False, "stats": stats}))
        return
    print(json.dumps({"ok": True, "claimed": True, "task": task,
                      "would_execute": apply_flag == "1"}))

asyncio.run(main())
PY
}

# ── Main loop ────────────────────────────────────────────────────────────
_iterate() {
    local idle_state caps
    idle_state="$(_is_idle)"
    caps="$(_caps_json)"
    if [[ "$idle_state" != "IDLE" ]]; then
        _log info "BUSY caps=$caps — skip pull"
        return 0
    fi
    _log info "IDLE caps=$caps apply=$APPLY — probing queue"
    local out
    out="$(_pull_and_claim "$APPLY" "$caps")"
    if [[ -z "$out" ]]; then
        _log warn "pull produced no output (subprocess failure)"
        return 0
    fi
    _log info "pull_result $out"
    if [[ "$APPLY" -eq 1 ]]; then
        # In --apply mode, hand the task payload to a downstream executor.
        # Today the executor is a stub: we log the kind + id so an operator
        # can wire a real dispatcher (e.g. 3s-brainstorm.sh) without
        # touching this script.
        _log info "EXECUTE-STUB — wire a kind→cmd mapper here"
    fi
}

if [[ "$LOOP" -eq 1 ]]; then
    _log info "starting loop interval=${INTERVAL_S}s threshold=${IDLE_THRESHOLD_S}s apply=$APPLY"
    while true; do
        _iterate
        sleep "$INTERVAL_S"
    done
else
    _iterate
fi
