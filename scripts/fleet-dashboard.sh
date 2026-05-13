#!/usr/bin/env bash
# fleet-dashboard.sh — One-page markdown fleet health report (AAA3)
#
# Composes a single-page snapshot Aaron can scan in 60s:
#   - Headline status (SHIP-READY / DEGRADED / BLOCKED)
#   - Per-node table (live / silent / mailbox)
#   - ZSF counter health (cascade_skipped_infeasible, heartbeat drops, neuro fallback, plist drift)
#   - 2026-05-06 plan progress (M1..M5 + R1..R5)
#   - Pending Aaron actions (inferred from health + counters)
#   - Recent commits (24h, non-autosync)
#   - Top warnings
#
# READ-ONLY. ZSF (each section guarded). Idempotent. Fast (<10s). Cost: $0.
#
# Usage:
#   bash scripts/fleet-dashboard.sh                # markdown → stdout + /tmp/fleet-dashboard.md
#   bash scripts/fleet-dashboard.sh --html         # also write /tmp/fleet-dashboard.html
#   bash scripts/fleet-dashboard.sh --quiet        # write file only, no stdout
#
# Audit: .fleet/audits/2026-05-12-AAA3-fleet-dashboard.md

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${FLEET_NERVE_PORT:-8855}"
OUT="${FLEET_DASHBOARD_OUT:-/tmp/fleet-dashboard.md}"
HEALTH_JSON="/tmp/fleet-dashboard-health.json"
DATA_JSON="/tmp/fleet-dashboard-data.json"
MODE="md"
QUIET=0

for arg in "$@"; do
    case "$arg" in
        --html) MODE="html" ;;
        --quiet) QUIET=1 ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
    esac
done

TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
NODE="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"

# ── Fetch health (ZSF: cache to file; fall back to empty JSON on error) ──
# Daemon may return a "warming-up" cache-cold stub on rapid successive calls.
# Retry once with a longer timeout when that happens.
_fetch_health() {
    curl -s --max-time "${1:-8}" "http://127.0.0.1:${PORT}/health" -o "$HEALTH_JSON" 2>/dev/null || return 1
    # Detect warming-up stub
    if grep -q '"status": "warming-up"' "$HEALTH_JSON" 2>/dev/null; then
        return 2
    fi
    [ -s "$HEALTH_JSON" ] || return 3
    return 0
}

if ! _fetch_health 8; then
    sleep 2
    if ! _fetch_health 20; then
        echo "{}" > "$HEALTH_JSON"
    fi
fi

# ── One-shot Python extractor → JSON of pre-computed fields (fast) ──
# Reads HEALTH_JSON once, writes DATA_JSON. ZSF: any failure → minimal JSON.
python3 - <<PY 2>>/tmp/fleet-dashboard.err
import json, os, time, sys
H = "/tmp/fleet-dashboard-health.json"
O = "/tmp/fleet-dashboard-data.json"
try:
    d = json.load(open(H))
except Exception as e:
    d = {}

now = time.time()
peers_out = []
peers = d.get('peers', {}) or {}
silent_peers = []
for name in sorted(peers.keys()):
    info = peers[name] or {}
    last = info.get('lastSeen')
    sessions = info.get('sessions', 0)
    transport = info.get('transport', '?')
    feasible = info.get('feasible_channels') or []
    cap = "full" if len(feasible) >= 7 else (",".join(feasible)[:30] if feasible else "?")
    if last is None:
        status = "mailbox" if transport == "jetstream_only" else "silent"
        last_s = "—"
        silent_peers.append(name)
    else:
        age = int(now - float(last))
        if age < 120: status = "live"
        elif age < 600: status = "stale"
        else: status = "silent"; silent_peers.append(name)
        last_s = f"{age}s ago"
    peers_out.append({
        "name": name, "status": status, "lastSeen": last_s,
        "sessions": sessions, "transport": transport, "cap": cap,
    })

z = d.get('zsf_counters', {}) or {}
wh = d.get('webhook', {}) or {}
hb = z.get('heartbeat_drops', {}) or {}
sb = z.get('split_brain', {}) or {}
inv = z.get('invariants_all', {}) or {}
inv_sum = sum(v for v in inv.values() if isinstance(v, (int, float)))
ts3 = z.get('three_surgeons', {}) or {}
nf = ts3.get('neuro_fallback', {}) or {}
nf_summary = ", ".join(f"{k}={v}" for k, v in nf.items() if v) or "default_kept"
kc_errs = (ts3.get('keychain_errors', {}) or {}).get('count', 0)
pd = z.get('plist_drift', {}) or {}
cs = z.get('cascade_skipped_infeasible', {}) or {}

out = {
    "status": d.get('status', 'unknown'),
    "nodeId": d.get('nodeId', '?'),
    "transport": d.get('transport', '?'),
    "uptime_s": d.get('uptime_s', '?'),
    "activeSessions": d.get('activeSessions', 0),
    "cluster_status": (d.get('cluster_state', {}) or {}).get('status', 'unknown'),
    "js_status": (d.get('jetstream_health', {}) or {}).get('status', 'unknown'),
    "recent_errors_count": len(d.get('recent_errors', []) or []),
    "peers": peers_out,
    "silent_peers": silent_peers,
    "webhook_events_recorded": wh.get('events_recorded', 0),
    "webhook_receive_errors": wh.get('receive_errors', 0),
    "webhook_last_age_s": wh.get('last_webhook_age_s'),
    "cascade_global": cs.get('global', 0),
    "cascade_errors": cs.get('errors', 0),
    "hb_unknown": hb.get('dropped_unknown_node_total', 0),
    "hb_no_peer_cfg": hb.get('dropped_no_peer_cfg_total', 0),
    "hb_stale_ts": hb.get('dropped_stale_ts_total', 0),
    "hb_parse_errors": hb.get('parse_errors_total', 0),
    "hb_self": hb.get('dropped_self_total', 0),
    "hb_quorum_loss": hb.get('quorum_loss_events_total', 0),
    "sb_detected": sb.get('detected_total', 0),
    "sb_reconnect_errors": sb.get('reconnect_errors', 0),
    "invariants_breach_sum": inv_sum,
    "neuro_fallback_summary": nf_summary,
    "keychain_errors": kc_errs,
    "plist_drift_total": pd.get('total', 0),
    "plist_drift_cfg_errors": pd.get('config_errors_total', 0),
    "rate_limit_allowed": (d.get('rate_limits', {}) or {}).get('stats', {}).get('allowed', 0),
    "rate_limit_denied": (d.get('rate_limits', {}) or {}).get('stats', {}).get('denied', 0),
    "gates_blocked": (d.get('stats', {}) or {}).get('gates_blocked', 0),
}
with open(O, "w") as f:
    json.dump(out, f)
PY

# If extractor died, write a minimal fallback
if [ ! -s "$DATA_JSON" ]; then
    echo '{"status":"unknown","peers":[],"silent_peers":[]}' > "$DATA_JSON"
fi

# ── Helper: read a field from DATA_JSON ──
dget() {
    python3 -c "import json; print(json.load(open('$DATA_JSON')).get('$1', '${2:-?}'))" 2>/dev/null || echo "${2:-?}"
}

# ── Section: headline ──
section_headline() {
    local headline="SHIP-READY"
    local reasons=()

    local cluster_status js_status status recent_errors cascade_errors
    cluster_status="$(dget cluster_status unknown)"
    js_status="$(dget js_status unknown)"
    status="$(dget status unknown)"
    recent_errors="$(dget recent_errors_count 0)"
    cascade_errors="$(dget cascade_errors 0)"

    [ "$status" != "ok" ] && headline="DEGRADED" && reasons+=("daemon=$status")
    [ "$cascade_errors" != "0" ] && headline="DEGRADED" && reasons+=("cascade_errors=$cascade_errors")
    [ "$recent_errors" != "0" ] && headline="DEGRADED" && reasons+=("recent_errors=$recent_errors")
    [ "$cluster_status" != "connected" ] && headline="BLOCKED" && reasons+=("cluster=$cluster_status")
    [ "$js_status" != "ok" ] && headline="BLOCKED" && reasons+=("jetstream=$js_status")

    local reason_str=""
    [ "${#reasons[@]}" -gt 0 ] && reason_str=" — $(IFS=, ; echo "${reasons[*]}")"

    echo "## Headline: ${headline}${reason_str}"
    echo ""
    echo "_uptime $(dget uptime_s ?)s · node=${NODE} · cluster=${cluster_status} · jetstream=${js_status}_"
}

# ── Section: per-node status ──
section_nodes() {
    echo "## Per-node status"
    echo ""
    echo "| Node | Status | LastSeen | Sessions | Transport | Capability |"
    echo "|---|---|---|---|---|---|"
    echo "| ${NODE} (self) | live | now | $(dget activeSessions 0) | $(dget transport ?) | full |"
    python3 - <<'PY' 2>/dev/null || echo "| (peer parse error) | ? | ? | ? | ? | ? |"
import json
d = json.load(open("/tmp/fleet-dashboard-data.json"))
peers = d.get("peers", []) or []
if not peers:
    print("| (no peers) | — | — | — | — | — |")
for p in peers:
    print(f"| {p['name']} | {p['status']} | {p['lastSeen']} | {p['sessions']} | {p['transport']} | {p['cap']} |")
PY
}

# ── Section: ZSF counter health ──
section_zsf() {
    echo "## ZSF counter health"
    echo ""
    echo "| Counter | Value | Status |"
    echo "|---|---|---|"
    python3 - <<'PY' 2>/dev/null || echo "| (counters parse error) | ? | ? |"
import json
d = json.load(open("/tmp/fleet-dashboard-data.json"))
def stat(v, zero_ok=True):
    return "HEALTHY" if (v == 0 or v == "0") else ("HEALTHY" if not zero_ok and v else "WARN")

wh_age = d.get("webhook_last_age_s")
wh_events = d.get("webhook_events_recorded", 0)
wh_status = "HEALTHY" if (wh_events > 0 and (wh_age is None or wh_age < 3600)) else "STALE"

rows = [
    ("webhook_events_recorded", wh_events, wh_status),
    ("webhook_receive_errors", d.get("webhook_receive_errors", 0), stat(d.get("webhook_receive_errors", 0))),
    ("cascade_skipped_infeasible.global", d.get("cascade_global", 0), "INFO"),
    ("cascade_skipped_infeasible.errors", d.get("cascade_errors", 0), stat(d.get("cascade_errors", 0))),
    ("heartbeat.dropped_unknown_node_total", d.get("hb_unknown", 0), stat(d.get("hb_unknown", 0))),
    ("heartbeat.dropped_no_peer_cfg_total", d.get("hb_no_peer_cfg", 0), stat(d.get("hb_no_peer_cfg", 0))),
    ("heartbeat.dropped_stale_ts_total", d.get("hb_stale_ts", 0), stat(d.get("hb_stale_ts", 0))),
    ("heartbeat.parse_errors_total", d.get("hb_parse_errors", 0), stat(d.get("hb_parse_errors", 0))),
    ("heartbeat.dropped_self_total", d.get("hb_self", 0), "INFO (self echo)"),
    ("heartbeat.quorum_loss_events_total", d.get("hb_quorum_loss", 0), "INFO (cumulative)"),
    ("split_brain.detected_total", d.get("sb_detected", 0), "INFO"),
    ("split_brain.reconnect_errors", d.get("sb_reconnect_errors", 0), stat(d.get("sb_reconnect_errors", 0))),
    ("invariants_all (sum of breaches)", d.get("invariants_breach_sum", 0),
     "HEALTHY" if d.get("invariants_breach_sum", 0) == 0 else "BREACH"),
    ("3s.neuro_fallback", d.get("neuro_fallback_summary", "?"), "OK"),
    ("3s.keychain_errors", d.get("keychain_errors", 0), stat(d.get("keychain_errors", 0))),
    ("plist_drift.total", d.get("plist_drift_total", 0), stat(d.get("plist_drift_total", 0))),
    ("plist_drift.config_errors_total", d.get("plist_drift_cfg_errors", 0), stat(d.get("plist_drift_cfg_errors", 0))),
]
for name, val, status in rows:
    print(f"| {name} | {val} | {status} |")
PY
}

# ── Section: 2026-05-06 plan progress ──
section_plan() {
    echo "## 2026-05-06 plan progress (M1..M5, R1..R5)"
    echo ""
    local plan_file
    plan_file="$REPO/docs/plans/2026-05-06-fleet-auto-heal-upgrade-proposal.md"
    local audit_dir="$REPO/.fleet/audits"

    local m_status="" r_status=""
    local m_count=0 r_count=0
    for n in 1 2 3 4 5; do
        # Look across any 2026-05-* audit date for M${n}-/R${n}- token in filename
        if ls -1 "$audit_dir"/2026-05-*-*-M${n}-*.md 2>/dev/null | grep -q .; then
            m_status="${m_status} M${n}✓"
            m_count=$((m_count + 1))
        else
            m_status="${m_status} M${n}?"
        fi
        if ls -1 "$audit_dir"/2026-05-*-*-R${n}-*.md 2>/dev/null | grep -q .; then
            r_status="${r_status} R${n}✓"
            r_count=$((r_count + 1))
        else
            r_status="${r_status} R${n}?"
        fi
    done

    echo "**Milestones:**${m_status}  →  ${m_count}/5 SHIPPED"
    echo ""
    echo "**Race tracks:**${r_status}  →  ${r_count}/5 SHIPPED"
    echo ""
    if [ -f "$plan_file" ]; then
        local shipped_count
        shipped_count=$(grep -c "SHIPPED" "$plan_file" 2>/dev/null || echo 0)
        echo "_plan SHIPPED markers in source: ${shipped_count} · plan: \`$(basename "$plan_file")\`_"
    fi
}

# ── Section: pending Aaron actions ──
section_pending() {
    echo "## Pending Aaron actions"
    echo ""
    local actions=()

    local silent_peers cluster_status wh_age plist_total plist_cfg_err kc_errs
    silent_peers="$(python3 -c "import json; print(','.join(json.load(open('$DATA_JSON')).get('silent_peers', [])))" 2>/dev/null || echo "")"
    cluster_status="$(dget cluster_status unknown)"
    wh_age="$(dget webhook_last_age_s)"
    plist_total="$(dget plist_drift_total 0)"
    plist_cfg_err="$(dget plist_drift_cfg_errors 0)"
    kc_errs="$(dget keychain_errors 0)"

    if [ -n "$silent_peers" ] && [ "$silent_peers" != "None" ]; then
        actions+=("refresh-node on silent peers: ${silent_peers} — run \`bash scripts/refresh-node.sh --apply --restart-daemons\` ON each")
    fi

    [ "$cluster_status" != "connected" ] && \
        actions+=("kickstart nats-server (\`launchctl kickstart -k gui/\$(id -u)/io.contextdna.nats-server\`)")

    if [ "$wh_age" != "None" ] && [ "$wh_age" != "?" ] && [ -n "$wh_age" ]; then
        # wh_age may be a float; coerce
        if python3 -c "import sys; sys.exit(0 if float('$wh_age') > 3600 else 1)" 2>/dev/null; then
            actions+=("webhook stale (${wh_age}s) — check WEBHOOK = #1 PRIORITY in CLAUDE.md")
        fi
    fi

    if [ "$plist_total" != "0" ] && [ "$plist_total" != "?" ]; then
        actions+=("plist drift detected (${plist_total}) — run \`bash scripts/install-launchd-plists.sh --check\`")
    fi
    if [ "$plist_cfg_err" != "0" ] && [ "$plist_cfg_err" != "?" ]; then
        actions+=("plist_drift config_errors=${plist_cfg_err} — check sentinel config")
    fi
    if [ "$kc_errs" != "0" ] && [ "$kc_errs" != "?" ]; then
        actions+=("3-surgeons keychain errors=${kc_errs}")
    fi

    # xbar from counter dir (fast — no plugin probe)
    if [ -d /tmp/xbar-counters ]; then
        local degraded_plugins
        degraded_plugins=$(ls /tmp/xbar-counters 2>/dev/null \
            | grep -E '_degraded_total$|_dead_total$' \
            | sed -E 's/^xbar_plugin_//; s/(_degraded_total|_dead_total)$//' \
            | sort -u | tr '\n' ',' | sed 's/,$//')
        if [ -n "$degraded_plugins" ]; then
            actions+=("xbar plugin(s) degraded/dead: ${degraded_plugins} — see \`bash scripts/xbar-health-check.sh\`")
        fi
    fi

    if [ "${#actions[@]}" -eq 0 ]; then
        echo "_None — fleet healthy._"
    else
        local i=1
        for a in "${actions[@]}"; do
            echo "${i}. ${a}"
            i=$((i+1))
        done
    fi
}

# ── Section: recent commits ──
section_commits() {
    echo "## Recent commits (last 24h, non-autosync)"
    echo ""
    cd "$REPO" 2>/dev/null || { echo "_repo cd failed_"; return; }
    local commits
    commits=$(git log --oneline --since="24 hours ago" 2>/dev/null \
        | grep -v "fleet-state: auto-sync" \
        | head -15)
    if [ -z "$commits" ]; then
        echo "_(no non-autosync commits in last 24h)_"
    else
        echo '```'
        echo "$commits"
        echo '```'
    fi
}

# ── Section: top warnings ──
section_warnings() {
    echo "## Top warnings"
    echo ""
    local warnings=()

    # webhook-publish.err (recent appends → ongoing background publish failures)
    if [ -f /tmp/webhook-publish.err ]; then
        local errlines
        errlines=$(wc -l </tmp/webhook-publish.err 2>/dev/null | tr -d ' ')
        if [ "${errlines:-0}" -gt 0 ]; then
            local recent
            recent=$(tail -1 /tmp/webhook-publish.err 2>/dev/null | head -c 90 | tr -d '`')
            warnings+=("webhook-publish.err: ${errlines} lines; tail: \`${recent}\`")
        fi
    fi

    local denied allowed gates_blocked cs_global
    denied="$(dget rate_limit_denied 0)"
    allowed="$(dget rate_limit_allowed 0)"
    gates_blocked="$(dget gates_blocked 0)"
    cs_global="$(dget cascade_global 0)"

    if [ "${denied:-0}" -gt "${allowed:-0}" ] 2>/dev/null && [ "${denied:-0}" != "0" ]; then
        warnings+=("rate-limit denied (${denied}) > allowed (${allowed}) — investigate hot peer")
    fi
    if [ "${gates_blocked:-0}" != "0" ]; then
        warnings+=("gates_blocked=${gates_blocked} (cumulative since daemon start)")
    fi
    if [ "${cs_global:-0}" -gt 100 ] 2>/dev/null; then
        warnings+=("cascade_skipped_infeasible.global=${cs_global} — peers consistently missing channels")
    fi

    # JS replica drift counter
    if [ -f /tmp/jj3-jetstream-provision-counters ]; then
        local drift_total
        drift_total=$(grep -E '^js_provision_repairs_total=' /tmp/jj3-jetstream-provision-counters 2>/dev/null \
            | tail -1 | cut -d= -f2)
        if [ "${drift_total:-0}" -gt 0 ] 2>/dev/null; then
            warnings+=("JetStream replica auto-repairs cumulative: ${drift_total}")
        fi
    fi

    if [ "${#warnings[@]}" -eq 0 ]; then
        echo "_None._"
    else
        for w in "${warnings[@]}"; do
            echo "- ${w}"
        done
    fi
}

# ── Compose markdown ──
compose_md() {
    cat <<MDHEAD
# Fleet Dashboard — ${TS}

_Generated by \`scripts/fleet-dashboard.sh\` on **${NODE}** · cost \$0 · read-only_

MDHEAD
    section_headline 2>/dev/null || echo "## Headline: unavailable"
    echo ""
    section_nodes 2>/dev/null || echo "## Per-node status — unavailable"
    echo ""
    section_zsf 2>/dev/null || echo "## ZSF counters — unavailable"
    echo ""
    section_plan 2>/dev/null || echo "## Plan progress — unavailable"
    echo ""
    section_pending 2>/dev/null || echo "## Pending — unavailable"
    echo ""
    section_commits 2>/dev/null || echo "## Commits — unavailable"
    echo ""
    section_warnings 2>/dev/null || echo "## Warnings — unavailable"
    echo ""
    cat <<MDFOOT
---

_Refresh: \`bash scripts/fleet-dashboard.sh\` (idempotent · ZSF-guarded · <10s wall-clock)_
MDFOOT
}

TMP_OUT="${OUT}.tmp.$$"
compose_md > "$TMP_OUT" 2>/dev/null || {
    {
        echo "# Fleet Dashboard — compose failed"
        echo ""
        echo "_See /tmp/fleet-dashboard.err_"
    } > "$TMP_OUT"
}
mv -f "$TMP_OUT" "$OUT"

if [ "$MODE" = "html" ]; then
    HTML_OUT="${OUT%.md}.html"
    {
        cat <<'HTMLHEAD'
<!doctype html>
<html><head><meta charset="utf-8">
<title>Fleet Dashboard</title>
<style>
body{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:980px;margin:2em auto;padding:0 1em;color:#222}
pre{white-space:pre-wrap;background:#f6f8fa;padding:1em;border-radius:6px;font-size:13px;line-height:1.4}
</style></head><body>
<pre>
HTMLHEAD
        sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g' "$OUT"
        echo "</pre></body></html>"
    } > "$HTML_OUT"
    [ "$QUIET" -eq 0 ] && echo "[html] $HTML_OUT" >&2
fi

if [ "$QUIET" -eq 0 ]; then
    cat "$OUT"
fi

exit 0
