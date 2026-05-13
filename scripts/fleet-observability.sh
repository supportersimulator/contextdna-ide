#!/usr/bin/env bash
# fleet-observability.sh — read-only swiss-army companion to fleet-daemon.sh
#
# Surfaces per-node health (mac1, mac2, mac3, …) in a single observable view
# that Atlas can poll quickly. Wraps the /observability/nodes JSON endpoint
# (tools/fleet_nerve_nats.py).
#
# T2 v4 deliverable. Addresses Neuro anchor-bias #2 ("no per-node health
# monitoring"). Hooks into existing /sprint-status + /health.
#
# Usage:
#   scripts/fleet-observability.sh nodes      # per-node health table
#   scripts/fleet-observability.sh evidence   # evidence-stream counters
#   scripts/fleet-observability.sh anomalies  # top 5 anomalies
#   scripts/fleet-observability.sh raw        # raw JSON (for piping)
#
# ZERO SILENT FAILURES — every probe wrapped, missing data shown as "?".
# POSIX bash. No external deps beyond curl + python3 stdlib.

set -uo pipefail

PORT="${FLEET_NERVE_PORT:-8855}"
HOST="${FLEET_NERVE_HOST:-127.0.0.1}"
URL="http://${HOST}:${PORT}/observability/nodes"
TIMEOUT="${FLEET_OBS_TIMEOUT:-8}"

cmd="${1:-nodes}"

# ── Fetch JSON once; cache in tmp file for sub-renders. ──
TMP_JSON="$(mktemp -t fleet-obs.XXXXXX 2>/dev/null || echo /tmp/fleet-obs.$$.json)"
trap 'rm -f "$TMP_JSON" 2>/dev/null || true' EXIT

if ! curl -sf --max-time "$TIMEOUT" "$URL" -o "$TMP_JSON" 2>/dev/null; then
    echo "ERROR: cannot reach $URL (daemon down or slow?)" >&2
    echo "  hint: curl --max-time $TIMEOUT $URL" >&2
    exit 2
fi

# Sanity check — JSON parsable?
if ! python3 -c "import json,sys; json.load(open('$TMP_JSON'))" 2>/dev/null; then
    echo "ERROR: $URL returned non-JSON or empty body" >&2
    exit 3
fi

case "$cmd" in
    nodes)
        python3 - "$TMP_JSON" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
nodes = d.get("nodes", {}) or {}
self_node = d.get("self_node", "?")
# Header
hdr = ["NODE", "DAEMON", "MLX", "WEBHOOK", "SYNAPTIC", "CARDIO", "NEURO",
       "LAST_SEEN", "IDE_VER", "SOURCE"]
fmt = "{:<8} {:<7} {:<5} {:<8} {:<9} {:<7} {:<6} {:<11} {:<14} {:<14}"
print(fmt.format(*hdr))
print("-" * 100)
def _mark(v):
    if v in ("up", "ok"):
        return v
    if v in ("down",):
        return "DOWN"
    return v if v else "?"
def _ls(v):
    if v is None:
        return "?"
    if isinstance(v, int):
        if v == 0:
            return "now"
        if v < 60:
            return f"{v}s"
        if v < 3600:
            return f"{v//60}m"
        return f"{v//3600}h"
    return str(v)
# Stable sort: self first, then alphabetical
keys = sorted(nodes.keys(), key=lambda k: (k != self_node, k))
for nid in keys:
    row = nodes.get(nid, {}) or {}
    star = "*" if nid == self_node else " "
    print(fmt.format(
        f"{star}{nid}",
        _mark(row.get("daemon")),
        _mark(row.get("mlx")),
        _mark(row.get("webhook")),
        _mark(row.get("synaptic")),
        _mark(row.get("cardio")),
        _mark(row.get("neuro")),
        _ls(row.get("last_seen")),
        str(row.get("ide_version", "?"))[:14],
        str(row.get("source", "?"))[:14],
    ))
print()
print(f"(* = self;  values: up/down/ok/?  last_seen: now/<n>s/<n>m/<n>h)")
PY
        ;;
    evidence)
        python3 - "$TMP_JSON" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
ec = d.get("evidence_counts", {}) or {}
print("EVIDENCE STREAM")
print("-" * 30)
order = ["trials", "outcomes", "permissions", "manuscripts"]
for k in order:
    v = ec.get(k, "?")
    print(f"  {k:<14} {v}")
err = ec.get("error")
if err:
    print()
    print(f"  WARN: ledger error -> {err}")
PY
        ;;
    anomalies)
        python3 - "$TMP_JSON" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
anom = d.get("anomalies", []) or []
if not anom:
    print("ANOMALIES: none (top 5 sentinel — fleet healthy)")
    sys.exit(0)
print("TOP ANOMALIES (max 5, severity-ranked)")
print("-" * 60)
for i, a in enumerate(anom[:5], 1):
    sev = a.get("severity", "?").upper()
    node = a.get("node", "?")
    issue = a.get("issue", "?")
    print(f"  {i}. [{sev:<4}] {node:<8} {issue}")
PY
        ;;
    raw)
        cat "$TMP_JSON"
        ;;
    -h|--help|help)
        cat <<EOF
fleet-observability.sh — per-node health panel (T2 v4)

Subcommands:
  nodes       Per-node table: daemon, MLX, webhook, synaptic, cardio, neuro,
              last_seen, IDE-version. (* marks self.)
  evidence    Counts of trials/outcomes/permissions/manuscripts (drive
              evidence-stream visibility).
  anomalies   Top 5 anomalies (severity-ranked).
  raw         Raw JSON from /observability/nodes (for piping into jq, etc.).

Env:
  FLEET_NERVE_HOST   default 127.0.0.1
  FLEET_NERVE_PORT   default 8855
  FLEET_OBS_TIMEOUT  default 8 (curl --max-time)
EOF
        ;;
    *)
        echo "unknown subcommand: $cmd" >&2
        echo "valid: nodes | evidence | anomalies | raw | help" >&2
        exit 1
        ;;
esac
