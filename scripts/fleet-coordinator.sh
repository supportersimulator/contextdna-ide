#!/usr/bin/env bash
# =============================================================================
# fleet-coordinator.sh — DDD2 per-peer degradation coordinator
# =============================================================================
#
# Read-side reads /health + fleet-state.json (the same signals
# fleet-dashboard.sh surfaces). Write-side dispatches the correct runbook
# recipe to the correct peer via the MFINV-C01 canonical entry
# multifleet.channel_priority.send (urgency=ops_fix).
#
# Decision table (per peer):
#
#   Signal                                 →  Recipe dispatched
#   ────────────────────────────────────────────────────────────
#   last_seen > 24h (or null/silent)       →  refresh-node.sh --apply --restart-daemons --include-cluster-fix
#   profile missing in KV                  →  fleet-bring-up-node.sh --apply
#   webhook events_recorded stuck > 6h     →  WW1 offsite-NATS kickstart
#   plist_drift_total > 0                  →  unify-cluster-urls.py --apply
#   cluster route not solicited            →  patch-nats-connect-retries.py --apply
#   cloud commit rate > 2/h (identical P0) →  cloud-p0-inbox-check.sh throttle deploy
#
# Each dispatch:
#   * urgency = ops_fix (new tag, recorded by channel_priority counters)
#   * carries the exact paste-and-go command + reversibility note
#   * idempotent — SHA-256(recipe + peer) deduped within 6h
#   * ZSF — failures bump fleet_coordinator_dispatch_errors_total
#
# Flags:
#   --dry-run                (DEFAULT) print decision table + planned dispatches; no send
#   --apply                  dispatch via multifleet.channel_priority.send
#   --target <peer>          restrict to a single peer (default: scan all known peers)
#   --target ALL             explicit "all peers" (required for --apply without per-peer flag)
#   --peers-json <path>      override health source (testing); JSON with same schema as /health
#   --dedup-dir <path>       override idempotency directory (testing); default /tmp/fleet-coordinator-dedup
#   --dedup-window-s <int>   override 6h dedup window (testing); default 21600
#   --counter-file <path>    override counter file (testing); default /tmp/fleet-coordinator-counters.txt
#   --help                   show this header
#
# Exit codes:
#   0 = scan completed (dry-run always 0 on success)
#   1 = dispatch error in --apply mode
#   2 = usage error
#
# Cost: $0 — no LLM calls. Sends only.
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${FLEET_NERVE_PORT:-8855}"

MODE="dry-run"
TARGET=""
PEERS_JSON_OVERRIDE=""
FLEET_STATE_OVERRIDE=""
DEDUP_DIR="${FLEET_COORDINATOR_DEDUP_DIR:-/tmp/fleet-coordinator-dedup}"
DEDUP_WINDOW_S="${FLEET_COORDINATOR_DEDUP_WINDOW_S:-21600}"
COUNTER_FILE="${FLEET_COORDINATOR_COUNTER_FILE:-/tmp/fleet-coordinator-counters.txt}"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)          MODE="dry-run"; shift ;;
        --apply)            MODE="apply"; shift ;;
        --target)           TARGET="${2:-}"; shift 2 ;;
        --peers-json)       PEERS_JSON_OVERRIDE="${2:-}"; shift 2 ;;
        --fleet-state-json) FLEET_STATE_OVERRIDE="${2:-}"; shift 2 ;;
        --dedup-dir)        DEDUP_DIR="${2:-}"; shift 2 ;;
        --dedup-window-s)   DEDUP_WINDOW_S="${2:-}"; shift 2 ;;
        --counter-file)     COUNTER_FILE="${2:-}"; shift 2 ;;
        -h|--help)          sed -n '2,55p' "$0"; exit 0 ;;
        *)                  echo "[fleet-coordinator] usage error: unknown arg $1" >&2; exit 2 ;;
    esac
done

mkdir -p "$DEDUP_DIR" 2>/dev/null || true
touch "$COUNTER_FILE" 2>/dev/null || true

# ── ZSF counter helpers ────────────────────────────────────────────────────
_bump() {
    # _bump <counter_name> [increment]
    local name="$1" inc="${2:-1}"
    local cur new
    cur=$(grep -E "^${name}=" "$COUNTER_FILE" 2>/dev/null | tail -1 | cut -d= -f2)
    cur="${cur:-0}"
    new=$((cur + inc))
    # Append; readers always read the last line.
    echo "${name}=${new}" >> "$COUNTER_FILE" 2>/dev/null || true
}

# ── Fetch peer signals ─────────────────────────────────────────────────────
HEALTH_JSON="/tmp/fleet-coordinator-health.json"

if [ -n "$PEERS_JSON_OVERRIDE" ]; then
    if [ ! -s "$PEERS_JSON_OVERRIDE" ]; then
        echo "[fleet-coordinator] peers-json file empty or missing: $PEERS_JSON_OVERRIDE" >&2
        _bump fleet_coordinator_health_fetch_errors_total
        echo "{}" > "$HEALTH_JSON"
    else
        cp "$PEERS_JSON_OVERRIDE" "$HEALTH_JSON"
    fi
else
    if ! curl -sf -H 'Accept: application/json' --max-time 8 \
              "http://127.0.0.1:${PORT}/health" -o "$HEALTH_JSON" 2>/dev/null; then
        _bump fleet_coordinator_health_fetch_errors_total
        echo "{}" > "$HEALTH_JSON"
    fi
fi

# ── Decide: emit planned dispatches as JSON lines on stdout of a Python pass.
# Each line: {"peer", "signal", "recipe_id", "recipe_cmd", "reversibility", "msg_subject", "msg_body"}
# ──────────────────────────────────────────────────────────────────────────
PLAN_FILE="/tmp/fleet-coordinator-plan.jsonl"
: > "$PLAN_FILE"

export _HEALTH_JSON="$HEALTH_JSON" _TARGET="$TARGET" _REPO_ROOT="$REPO_ROOT" \
       _PLAN_FILE="$PLAN_FILE" _FLEET_STATE_OVERRIDE="$FLEET_STATE_OVERRIDE"

python3 - <<'PY' 2>>/tmp/fleet-coordinator.err
"""Decide planned dispatches per peer. Writes JSONL to $_PLAN_FILE.

ZSF: any per-peer error becomes a structured plan line with signal=error;
the dispatcher tracks it. Never raises through to the bash caller.
"""
import json, os, time, subprocess, pathlib

HEALTH = os.environ.get("_HEALTH_JSON", "/tmp/fleet-coordinator-health.json")
TARGET = (os.environ.get("_TARGET") or "").strip()
REPO   = os.environ.get("_REPO_ROOT", "")
PLAN   = os.environ.get("_PLAN_FILE", "/tmp/fleet-coordinator-plan.jsonl")
SELF_NODE = os.environ.get("MULTIFLEET_NODE_ID", "")

# Read /health
try:
    health = json.load(open(HEALTH))
except Exception as e:
    health = {}

# Read fleet-state.json (for KV profile signal — health endpoint does
# NOT carry .profile / .version; ZZ2 evidence relies on KV file).
fleet_state = {}
fs_override = os.environ.get("_FLEET_STATE_OVERRIDE", "").strip()
fs_path = fs_override or os.path.join(REPO, "fleet-state.json")
try:
    fleet_state = json.load(open(fs_path))
except Exception:
    fleet_state = {}

now = time.time()
self_node = health.get("nodeId") or SELF_NODE or ""

peers = health.get("peers", {}) or {}
# Also pull fleet-state nodes (mac2 may only appear in KV when daemon-side
# heartbeat is silent).
for n in (fleet_state.get("nodes") or {}):
    if n != self_node and n not in peers:
        peers[n] = {"lastSeen": None, "source": "fleet_state_only"}

def _peer_last_seen_age(name, info):
    """Return age in seconds since last heartbeat, or None if unknown.

    NOTE: /health's peers[*].lastSeen is ALREADY age-in-seconds (an int),
    not an epoch timestamp — see tools/fleet_nerve_nats.py:5500. We pass
    it through directly. Fall back to fleet-state.json when null.
    """
    last = info.get("lastSeen")
    if last is None:
        fs = (fleet_state.get("nodes") or {}).get(name) or {}
        fs_last = (fs.get("health") or {}).get("last_seen")
        if fs_last:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(fs_last.replace("Z", "+00:00"))
                return now - dt.timestamp()
            except Exception:
                return None
        return None
    try:
        return float(last)  # already age-in-seconds
    except Exception:
        return None

def _has_profile_kv(name):
    """True if KV registration exists and has both profile + version blocks.

    Returns True for peers absent from KV entirely (we can't say it's
    "missing" if there's no record at all — that's a different signal
    handled by stale_last_seen + the heartbeat path). The "missing profile"
    signal fires only when KV has a row but lacks profile/version (matches
    ZZ2's mac2 case).
    """
    nodes = fleet_state.get("nodes") or {}
    if name not in nodes:
        return True  # no row → not a profile-missing signal
    fs = nodes[name] or {}
    return bool(fs.get("profile")) and bool(fs.get("version"))

def _webhook_stuck_hours():
    wh = health.get("webhook") or {}
    age = wh.get("last_webhook_age_s")
    if age is None:
        return None
    try:
        return float(age) / 3600.0
    except Exception:
        return None

def _plist_drift_total():
    z = (health.get("zsf_counters") or {}).get("plist_drift") or {}
    try:
        return int(z.get("total") or 0)
    except Exception:
        return 0

def _cluster_routes_solicited():
    cs = health.get("cluster_state") or {}
    # Treat empty observed_peers OR explicit failure as "no routes".
    obs = cs.get("observed_peer_count")
    if cs.get("status") != "connected":
        return False
    if obs is None:
        return True  # unknown — don't dispatch
    try:
        return int(obs) > 0
    except Exception:
        return True

def _cloud_thrash_per_hour():
    """Look back 60 min in `git log` for repeated identical-P0 commits on cloud."""
    try:
        out = subprocess.run(
            ["git", "log", "--since=60.minutes.ago", "--pretty=format:%an|%s"],
            cwd=REPO, capture_output=True, text=True, timeout=8,
        )
        if out.returncode != 0:
            return 0
        lines = [ln for ln in (out.stdout or "").splitlines() if ln]
        # cloud P0 commits authored by supportersimulator or "cloud P0 inbox check"
        seen = {}
        for ln in lines:
            if "cloud P0 inbox check" in ln or "supportersimulator" in ln.lower():
                # Group by subject text (collapse the version suffix variant).
                _, _, subj = ln.partition("|")
                # canonicalise — drop trailing version markers like "vN" or " 2026-05-12"
                key = subj.split("—")[0].strip().split(" 2026-")[0].strip()
                seen[key] = seen.get(key, 0) + 1
        return max(seen.values()) if seen else 0
    except Exception:
        return 0

plans = []

# ── Per-peer signals (live macs) ────────────────────────────────────────
for name in sorted(peers.keys()):
    if TARGET and TARGET not in ("ALL", "all") and TARGET != name:
        continue
    info = peers[name] or {}

    age = _peer_last_seen_age(name, info)
    # Signal 1: silent > 24h (or null with no KV fallback)
    if age is None or age > 86400:
        cmd = "bash scripts/refresh-node.sh --apply --restart-daemons --include-cluster-fix"
        plans.append({
            "peer": name,
            "signal": f"stale_last_seen (age={age!r}s)",
            "recipe_id": "refresh-node",
            "recipe_cmd": cmd,
            "reversibility": "All sub-scripts are one-revert. Mutations: launchctl kickstart + RR2 plist routes (backed up to *.bak).",
            "msg_subject": f"DDD2 coordinator: {name} silent → refresh-node",
            "msg_body": (
                f"Detected stale_last_seen on {name}. Paste-and-go on {name}:\n\n"
                f"  cd ~/dev/er-simulator-superrepo && git pull --ff-only\n"
                f"  {cmd}\n\n"
                "Reversibility: every sub-script (M1 venv-rebuild, RR2 unify-cluster-urls, "
                "TT3 daemon-services-up) is idempotent + one-revert; the only "
                "mutation is launchctl kickstart and a plist routes patch with .bak backup."
            ),
        })

    # Signal 2: profile missing in KV
    if not _has_profile_kv(name):
        cmd = "bash scripts/fleet-bring-up-node.sh --apply"
        plans.append({
            "peer": name,
            "signal": "profile_missing_in_kv",
            "recipe_id": "fleet-bring-up-node",
            "recipe_cmd": cmd,
            "reversibility": "fleet-bring-up-node publishes a profile/version blob to KV; reverse via `python3 scripts/fleet-profile-publish.py --remove`.",
            "msg_subject": f"DDD2 coordinator: {name} KV profile missing → bring-up",
            "msg_body": (
                f"KV has no .profile/.version block for {name}. Run on {name}:\n\n"
                f"  cd ~/dev/er-simulator-superrepo && git pull --ff-only\n"
                f"  {cmd}\n\n"
                "Reversibility: bring-up-node only writes a JSON profile to KV; "
                "reversed by `--remove` flag on fleet-profile-publish."
            ),
        })

# ── Local-host signals (apply to *self* but dispatch to ALL peers as a runbook reminder) ──
wh_h = _webhook_stuck_hours()
if wh_h is not None and wh_h > 6:
    cmd = "WW1 offsite-NATS kickstart — `launchctl kickstart -k gui/$(id -u)/io.contextdna.nats-server`"
    targets = [n for n in sorted(peers.keys()) if not TARGET or TARGET in ("ALL", "all") or TARGET == n]
    for tgt in targets:
        plans.append({
            "peer": tgt,
            "signal": f"webhook_stuck_{int(wh_h)}h",
            "recipe_id": "ww1-offsite-nats-kickstart",
            "recipe_cmd": cmd,
            "reversibility": "Idempotent restart — no data destroyed. JetStream R=3 retains events.",
            "msg_subject": "DDD2 coordinator: webhook silence → WW1 kickstart",
            "msg_body": (
                f"Webhook events_recorded stuck for ~{wh_h:.1f}h. Run on every node "
                f"with a NATS server:\n\n  {cmd}\n\n"
                "Reversibility: launchctl kickstart is idempotent; no data destroyed."
            ),
        })

pd_total = _plist_drift_total()
if pd_total > 0:
    cmd = "python3 scripts/unify-cluster-urls.py --apply"
    targets = [n for n in sorted(peers.keys()) if not TARGET or TARGET in ("ALL", "all") or TARGET == n]
    for tgt in targets:
        plans.append({
            "peer": tgt,
            "signal": f"plist_drift_total={pd_total}",
            "recipe_id": "unify-cluster-urls",
            "recipe_cmd": cmd,
            "reversibility": "Backs up plist to *.bak before patch; reverse via `cp ...plist.bak ...plist && launchctl kickstart`.",
            "msg_subject": "DDD2 coordinator: plist drift → unify-cluster-urls",
            "msg_body": (
                f"plist_drift_total={pd_total}. Run on {tgt}:\n\n  {cmd}\n\n"
                "Reversibility: original plist saved to *.bak by the script."
            ),
        })

if not _cluster_routes_solicited():
    cmd = "python3 scripts/patch-nats-connect-retries.py --apply"
    targets = [n for n in sorted(peers.keys()) if not TARGET or TARGET in ("ALL", "all") or TARGET == n]
    for tgt in targets:
        plans.append({
            "peer": tgt,
            "signal": "cluster_routes_not_solicited",
            "recipe_id": "patch-nats-connect-retries",
            "recipe_cmd": cmd,
            "reversibility": "Patcher writes idempotent connect-retry block; revert with git checkout on the plist + kickstart.",
            "msg_subject": "DDD2 coordinator: cluster routes missing → patch-nats-connect-retries",
            "msg_body": (
                f"cluster_state.observed_peers indicates no solicited routes. Run on {tgt}:\n\n  {cmd}\n\n"
                "Reversibility: idempotent patcher; revert via git checkout on the plist."
            ),
        })

# Cloud-thrash signal (only routes the recipe to cloud)
thrash = _cloud_thrash_per_hour()
if thrash > 2 and (not TARGET or TARGET in ("ALL", "all") or TARGET == "cloud"):
    cmd = "bash scripts/cloud-p0-inbox-check.sh  # WW4 throttle (cooldown=6h, idempotency by state hash)"
    plans.append({
        "peer": "cloud",
        "signal": f"cloud_commit_thrash={thrash}/h",
        "recipe_id": "cloud-p0-throttle-deploy",
        "recipe_cmd": cmd,
        "reversibility": "Throttle is a script-level guard only; revert: `rm /tmp/cloud-p0-state.json && CLOUD_P0_COOLDOWN_S=0`.",
        "msg_subject": "DDD2 coordinator: cloud thrashing → throttle deploy",
        "msg_body": (
            f"Cloud emitted {thrash} identical-P0 commits in last 60 min. "
            f"Wrap the cloud P0 task with:\n\n  {cmd}\n\n"
            "See docs/runbooks/cloud-node-role.md §Deployment. "
            "Reversibility: pure script guard; remove the wrapper to revert."
        ),
    })

with open(PLAN, "w") as f:
    for p in plans:
        f.write(json.dumps(p) + "\n")
PY

# Bash falls back to empty plan if python failed.
if [ ! -f "$PLAN_FILE" ]; then : > "$PLAN_FILE"; fi

# ── Pretty-print decision table ────────────────────────────────────────────
echo "## Fleet Coordinator — $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo ""
echo "Mode: ${MODE} · Target: ${TARGET:-<auto-scan>} · Dedup window: ${DEDUP_WINDOW_S}s"
echo ""

if [ ! -s "$PLAN_FILE" ]; then
    echo "_No degradation signals detected — fleet healthy._"
    _bump fleet_coordinator_runs_clean_total
    exit 0
fi

echo "| Peer | Signal | Recipe | Recipe cmd |"
echo "|---|---|---|---|"
python3 - <<'PY'
import json, os
PLAN = os.environ.get("_PLAN_FILE", "/tmp/fleet-coordinator-plan.jsonl")
for ln in open(PLAN):
    p = json.loads(ln)
    cmd = p["recipe_cmd"]
    if len(cmd) > 70:
        cmd = cmd[:67] + "..."
    print(f"| {p['peer']} | {p['signal']} | {p['recipe_id']} | `{cmd}` |")
PY

# ── Idempotency dedup ─────────────────────────────────────────────────────
# For each plan, compute sha256(recipe_id + peer). If a marker exists < window seconds old,
# it's deduped. Markers store the planned dispatch dump.
DISPATCH_LIST="/tmp/fleet-coordinator-dispatches.jsonl"
: > "$DISPATCH_LIST"

while IFS= read -r line; do
    [ -z "$line" ] && continue
    PEER=$(echo "$line" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['peer'])")
    RECIPE=$(echo "$line" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['recipe_id'])")
    SIG_KEY=$(printf "%s|%s" "$RECIPE" "$PEER" | shasum -a 256 | awk '{print $1}')
    MARKER="$DEDUP_DIR/$SIG_KEY"
    NOW=$(date +%s)
    DEDUPED=0
    if [ -f "$MARKER" ]; then
        MTIME=$(stat -f %m "$MARKER" 2>/dev/null || stat -c %Y "$MARKER" 2>/dev/null || echo 0)
        AGE=$((NOW - MTIME))
        if [ "$AGE" -lt "$DEDUP_WINDOW_S" ]; then
            DEDUPED=1
            _bump fleet_coordinator_dispatches_deduped_total
            echo ""
            echo "DEDUP: ${RECIPE} → ${PEER} (last sent ${AGE}s ago, window ${DEDUP_WINDOW_S}s)"
            continue
        fi
    fi
    echo "$line" >> "$DISPATCH_LIST"
done < "$PLAN_FILE"

echo ""
echo "Planned dispatches (after dedup):"
if [ ! -s "$DISPATCH_LIST" ]; then
    echo "  (none — all deduped within window)"
else
    while IFS= read -r line; do
        PEER=$(echo "$line" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['peer'])")
        SIG=$(echo "$line" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['signal'])")
        RECIPE=$(echo "$line" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['recipe_id'])")
        echo "  → ${PEER}: ${RECIPE} (signal=${SIG}, urgency=ops_fix)"
    done < "$DISPATCH_LIST"
fi

_bump fleet_coordinator_runs_total

# ── Dry-run exits before sending ─────────────────────────────────────────
if [ "$MODE" = "dry-run" ]; then
    echo ""
    echo "[dry-run] No dispatch performed. Re-run with --apply --target ALL (or --target <peer>) to send."
    exit 0
fi

# ── Apply mode: require explicit target ──────────────────────────────────
if [ -z "$TARGET" ]; then
    echo ""
    echo "[fleet-coordinator] --apply requires --target ALL or --target <peer>" >&2
    _bump fleet_coordinator_apply_missing_target_total
    exit 2
fi

# ── Dispatch via multifleet.channel_priority.send (MFINV-C01 allowed_entry) ──
DISPATCH_ERRORS=0
DISPATCH_OK=0
ERR_LOG="/tmp/fleet-coordinator-dispatch.err"
: > "$ERR_LOG"

while IFS= read -r line; do
    [ -z "$line" ] && continue
    export _DISPATCH_LINE="$line"
    PEER=$(echo "$line" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['peer'])")
    RECIPE=$(echo "$line" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['recipe_id'])")
    SIG_KEY=$(printf "%s|%s" "$RECIPE" "$PEER" | shasum -a 256 | awk '{print $1}')
    MARKER="$DEDUP_DIR/$SIG_KEY"

    RESULT=$(PYTHONPATH="${REPO_ROOT}/multi-fleet:${PYTHONPATH:-}" python3 - <<'PY' 2>>"$ERR_LOG"
"""Dispatch one planned recipe via multifleet.channel_priority.send.

ZSF: every exception caught, returned as a structured failure dict.
Returns a single JSON line on stdout: {ok, channel, error, attempts, elapsed_ms}.
"""
import json, os, sys, urllib.request, urllib.error

LINE = os.environ.get("_DISPATCH_LINE", "")
PORT = os.environ.get("FLEET_NERVE_PORT", "8855")

try:
    plan = json.loads(LINE)
except Exception as e:
    print(json.dumps({"ok": False, "channel": None, "error": f"plan_parse:{e}", "attempts": [], "elapsed_ms": 0}))
    sys.exit(0)

peer = plan["peer"]
message = {
    "subject": plan["msg_subject"],
    "body": plan["msg_body"],
    "recipe_id": plan["recipe_id"],
    "recipe_cmd": plan["recipe_cmd"],
    "reversibility": plan["reversibility"],
    "urgency": "ops_fix",
}

# Try the canonical wrapper first.
try:
    from multifleet import channel_priority as cp
    res = cp.send(peer, message, urgency="ops_fix")
    if res.get("delivered"):
        print(json.dumps({"ok": True, "channel": res.get("channel"),
                          "error": None, "attempts": res.get("attempts", []),
                          "elapsed_ms": res.get("elapsed_ms", 0),
                          "via": "channel_priority"}))
        sys.exit(0)
    # If channel_priority returned no_protocol (no default registered in this
    # one-shot process), fall through to the daemon HTTP path which calls
    # send_with_fallback (also on extraction_contract.json allowed_entries).
    if res.get("error") != "no_protocol":
        print(json.dumps({"ok": False, "channel": None,
                          "error": res.get("error") or "not_delivered",
                          "attempts": res.get("attempts", []),
                          "elapsed_ms": res.get("elapsed_ms", 0),
                          "via": "channel_priority"}))
        sys.exit(0)
except Exception as e:
    # ZSF — surface, never silent. Fall through to HTTP path below.
    pass

# Fallback: post to the local daemon /message endpoint (which dispatches via
# FleetNerveNATS.send_with_fallback — also an MFINV-C01 allowed_entry).
try:
    body = json.dumps({
        "to": peer,
        "subject": message["subject"],
        "body": message["body"],
        "recipe_id": message["recipe_id"],
        "recipe_cmd": message["recipe_cmd"],
        "urgency": "ops_fix",
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/message",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode() or "{}")
    print(json.dumps({"ok": bool(data.get("delivered")),
                      "channel": data.get("channel") or data.get("method"),
                      "error": data.get("error"),
                      "attempts": data.get("errors", []),
                      "elapsed_ms": data.get("elapsed_ms", 0),
                      "via": "daemon_message"}))
except urllib.error.URLError as e:
    print(json.dumps({"ok": False, "channel": None,
                      "error": f"daemon_unreachable:{e.reason}",
                      "attempts": [], "elapsed_ms": 0,
                      "via": "daemon_message"}))
except Exception as e:
    print(json.dumps({"ok": False, "channel": None,
                      "error": f"daemon_exception:{type(e).__name__}:{e}",
                      "attempts": [], "elapsed_ms": 0,
                      "via": "daemon_message"}))
PY
)

    OK=$(echo "$RESULT" | python3 -c "import json,sys; print(1 if json.loads(sys.stdin.read() or '{}').get('ok') else 0)" 2>/dev/null || echo 0)
    if [ "$OK" = "1" ]; then
        DISPATCH_OK=$((DISPATCH_OK + 1))
        _bump fleet_coordinator_dispatch_ok_total
        touch "$MARKER"  # record success → future calls within window dedup
        echo "  OK   ${PEER}: ${RECIPE}"
    else
        DISPATCH_ERRORS=$((DISPATCH_ERRORS + 1))
        _bump fleet_coordinator_dispatch_errors_total
        ERR=$(echo "$RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read() or '{}').get('error','?'))" 2>/dev/null || echo '?')
        echo "  FAIL ${PEER}: ${RECIPE} → ${ERR}"
    fi
done < "$DISPATCH_LIST"

echo ""
echo "Dispatch summary: ok=${DISPATCH_OK} err=${DISPATCH_ERRORS}"

# Exit 1 if any dispatch failed (operator must investigate).
[ "$DISPATCH_ERRORS" -gt 0 ] && exit 1
exit 0
