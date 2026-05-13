#!/usr/bin/env python3.14
"""RACE AK — Fleet-wide consistency check across mac1+mac2+mac3.

READ-ONLY end-to-end test. Does NOT send any messages to mac1/mac2 (only NATS
request-reply RPC, which is pull-only — peer chooses to answer; no inbox
writes), does NOT modify any state, does NOT restart any service.

Per-peer data is collected from:
  * Local NATS monitoring : http://127.0.0.1:8222/{varz,jsz,routez}
  * Per-peer RPC          : nc.request("rpc.<peer>.status", b"", timeout=4)
                            (mac1 only answers `rpc.mac1.health` and returns a
                            thin payload — degraded but observable)

7 consistency checks. Each FAIL gets one retry before being recorded.

Outputs:
  docs/cluster-consistency-status.md
  docs/cluster-consistency-status.json
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import nats
from nats.errors import TimeoutError as NatsTimeout

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
MON_BASE = os.environ.get("NATS_MON", "http://127.0.0.1:8222")
DAEMON_BASE = os.environ.get("FLEET_DAEMON", "http://127.0.0.1:8855")
LOCAL_NODE = os.environ.get("MULTIFLEET_NODE_ID", "mac3")
PEERS = ("mac1", "mac2", "mac3")
RPC_TIMEOUT = 4.0
COUNTER_GAP_S = float(os.environ.get("RACE_AK_COUNTER_GAP", "30"))
HEARTBEAT_FRESH_S = 60.0

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_MD = REPO_ROOT / "docs" / "cluster-consistency-status.md"
REPORT_JSON = REPO_ROOT / "docs" / "cluster-consistency-status.json"


# ───────────────────────────── helpers ──────────────────────────────

def http_json(path: str, base: str = MON_BASE, timeout: float = 5.0) -> dict:
    """GET a NATS monitoring endpoint. NATS 2.12 returns schema docs unless a
    query param is present, so always append one for `MON_BASE`."""
    if base == MON_BASE:
        sep = "&" if "?" in path else "?"
        url = f"{base}{path}{sep}_=1"
    else:
        url = f"{base}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "race-ak/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def merge_status(statuses: list[str]) -> str:
    if "FAIL" in statuses:
        return "FAIL"
    if "DEGRADED" in statuses:
        return "DEGRADED"
    return "PASS"


async def rpc_call(nc, peer: str, method: str, timeout: float = RPC_TIMEOUT) -> dict | None:
    """NATS request to rpc.<peer>.<method>. Returns dict on success, None on
    NoResponders / timeout / parse error."""
    try:
        msg = await nc.request(f"rpc.{peer}.{method}", b"", timeout=timeout)
        return json.loads(msg.data)
    except (NatsTimeout, asyncio.TimeoutError, Exception):
        return None


async def gather_peer_status(nc, peers=PEERS, retries: int = 1) -> dict[str, dict | None]:
    """Collect each peer's `rpc.status` snapshot.

    mac1 currently doesn't answer `rpc.mac1.status` — it only answers
    `rpc.mac1.health` (older/different daemon). We try `status` first, then
    `health` as a fallback, so mac1 still contributes what it can.

    Calls are spaced 0.4s apart so we don't hit NoRespondersError from the
    hub-relay path saturating.
    """
    out: dict[str, dict | None] = {}
    for p in peers:
        view = None
        for method in ("status", "health"):
            for _ in range(retries + 1):
                view = await rpc_call(nc, p, method)
                if view is not None:
                    view["_rpc_method"] = method
                    break
                await asyncio.sleep(0.4)
            if view is not None:
                break
        out[p] = view
        await asyncio.sleep(0.4)
    return out


def fetch_local_jsz() -> dict:
    try:
        return http_json("/jsz?streams=true&consumers=true")
    except Exception as e:
        return {"error": str(e)}


def fetch_local_routez() -> dict:
    try:
        return http_json("/routez")
    except Exception as e:
        return {"error": str(e)}


def fetch_local_health() -> dict:
    try:
        req = urllib.request.Request(f"{DAEMON_BASE}/health",
                                     headers={"User-Agent": "race-ak/1.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


# ───────────────────────────── checks ───────────────────────────────

def _stream_from_jsz(jsz: dict, name: str) -> dict | None:
    """Pull stream + cluster info from local /jsz output."""
    for a in jsz.get("account_details") or []:
        for s in a.get("stream_detail", []):
            if s.get("name") == name:
                cl = s.get("cluster") or {}
                cfg = s.get("config") or {}
                state = s.get("state") or {}
                rep_names = [r.get("name") for r in cl.get("replicas", [])]
                if cl.get("leader") and cl.get("leader") not in rep_names:
                    rep_names.append(cl.get("leader"))
                follower_meta = []
                for r in cl.get("replicas", []):
                    follower_meta.append({
                        "name": r.get("name"),
                        "current": r.get("current"),
                        "active": r.get("active"),
                        "lag": r.get("lag"),
                    })
                return {
                    "name": name,
                    "leader": cl.get("leader"),
                    "replicas_cfg": cfg.get("num_replicas"),
                    "replicas_effective": cfg.get("num_replicas") or len(rep_names),
                    "members": sorted(set(filter(None, rep_names))),
                    "messages": state.get("messages"),
                    "first_seq": state.get("first_seq"),
                    "last_seq": state.get("last_seq"),
                    "follower_detail": follower_meta,
                    "consumer_count": state.get("consumer_count"),
                }
    return None


def check_stream_replication(jsz: dict, stream: str) -> dict:
    """Stream replication: R=3, leader present, all 3 nodes are members,
    followers are current with no lag."""
    meta = _stream_from_jsz(jsz, stream)
    if not meta:
        return {"name": f"stream replication: {stream}", "status": "FAIL",
                "reason": "stream not found in /jsz", "raw_error": jsz.get("error")}

    sub: list[str] = []
    if (meta["replicas_effective"] or 0) != 3:
        sub.append("FAIL")
    if not meta["leader"]:
        sub.append("FAIL")
    if not set(PEERS).issubset(set(meta["members"])):
        sub.append("FAIL")
    bad_followers = [f for f in meta["follower_detail"]
                     if (f.get("lag") or 0) > 0 or f.get("current") is False]
    if bad_followers:
        sub.append("DEGRADED")

    return {
        "name": f"stream replication: {stream}",
        "status": merge_status(sub) if sub else "PASS",
        "stream": meta,
        "all_3_members": set(PEERS).issubset(set(meta["members"])),
        "lagging_followers": bad_followers,
    }


def check_election(local_health: dict, peer_status: dict[str, dict | None]) -> dict:
    """Mac3's local /health exposes delegation.chief_node. Other nodes do not
    expose this via RPC today — so we record the local view and note what is
    NOT verifiable so it shows up as a real consistency gap."""
    local_chief = (local_health.get("delegation") or {}).get("chief_node")
    delegation_local = local_health.get("delegation") or {}
    peer_chiefs: dict[str, str | None] = {}
    for p, view in peer_status.items():
        # Currently no peer exposes chief_node via rpc.status. Record None.
        peer_chiefs[p] = (view or {}).get("chief_node") or (view or {}).get("delegation", {}).get("chief_node")

    distinct = {c for c in peer_chiefs.values() if c is not None}
    distinct.add(local_chief) if local_chief else None
    distinct.discard(None)

    if not local_chief:
        status = "FAIL"
    elif len(distinct) > 1:
        status = "FAIL"
    else:
        # We have local but no cross-peer confirmation
        status = "DEGRADED"

    return {
        "name": "election consistency",
        "status": status,
        "local_chief_node": local_chief,
        "delegation_local": delegation_local,
        "chief_per_peer_via_rpc": peer_chiefs,
        "agreed_chief": next(iter(distinct)) if len(distinct) == 1 else None,
        "note": ("rpc.<peer>.status / rpc.<peer>.health do not currently expose "
                 "delegation.chief_node from peer daemons; verifiable only locally"),
    }


def _peers_list(view: dict | None) -> list[str]:
    if not view:
        return []
    p = view.get("peers")
    if isinstance(p, list):
        return list(p)
    if isinstance(p, dict):
        return list(p.keys())
    # mac1 fallback shape: lastSeen_per_peer dict
    lp = view.get("lastSeen_per_peer")
    if isinstance(lp, dict):
        return list(lp.keys())
    return []


def check_peer_map_mutual(peer_status: dict[str, dict | None]) -> dict:
    """A in B's peers ↔ B in A's peers, for every (A,B) pair."""
    seen: dict[str, set[str]] = {p: set(_peers_list(v)) for p, v in peer_status.items()}

    pairs: list[dict] = []
    for a in PEERS:
        for b in PEERS:
            if a >= b:
                continue
            a_sees_b = b in seen.get(a, set())
            b_sees_a = a in seen.get(b, set())
            pairs.append({
                "a": a, "b": b,
                "a_sees_b": a_sees_b,
                "b_sees_a": b_sees_a,
                "symmetric": a_sees_b == b_sees_a,
            })

    asymmetric = [p for p in pairs if not p["symmetric"]]
    no_view = [p for p, v in peer_status.items() if not v]
    pairs_both_seen = [p for p in pairs if p["a_sees_b"] and p["b_sees_a"]]
    if not pairs:
        status = "FAIL"
    elif asymmetric:
        status = "FAIL"
    elif no_view or len(pairs_both_seen) < len(pairs):
        status = "DEGRADED"
    else:
        status = "PASS"
    return {
        "name": "peer-map mutual consistency",
        "status": status,
        "peers_seen_by": {p: sorted(s) for p, s in seen.items()},
        "pairs": pairs,
        "asymmetric_pairs": asymmetric,
        "peers_with_no_view": no_view,
        "peers_with_thin_view": [p for p, v in peer_status.items()
                                 if v and not _peers_list(v)],
    }


def check_heartbeat_freshness(local_health: dict, jsz: dict) -> dict:
    """Two sources of freshness:
      1. local daemon /health: peers.<target>.lastSeen for the local node only
      2. /jsz follower `active_ns`: ns since last replication ack — applies
         to all 3 nodes from leader's perspective
    """
    local_view: dict[str, dict] = {}
    peers_block = local_health.get("peers") or {}
    if isinstance(peers_block, dict):
        for tgt, info in peers_block.items():
            ls = info.get("lastSeen") if isinstance(info, dict) else None
            local_view[tgt] = {"lastSeen_s": ls, "fresh": isinstance(ls, (int, float)) and ls < HEARTBEAT_FRESH_S}

    follower_view: dict[str, dict] = {}
    for sn in ("FLEET_MESSAGES", "FLEET_EVENTS"):
        meta = _stream_from_jsz(jsz, sn) or {}
        for f in meta.get("follower_detail") or []:
            ns = f.get("active") if isinstance(f.get("active"), (int, float)) else None
            # newer NATS uses active_ns (we stored as 'active') — fall back to lag
            name = f.get("name")
            if not name:
                continue
            row = follower_view.setdefault(name, {"streams": {}})
            row["streams"][sn] = {"current": f.get("current"), "lag": f.get("lag"),
                                  "active": ns}

    stale_local = [t for t, v in local_view.items() if not v["fresh"]]
    bad_followers = []
    for nm, info in follower_view.items():
        for sn, st in info.get("streams", {}).items():
            if st.get("current") is False or (st.get("lag") or 0) > 0:
                bad_followers.append({"node": nm, "stream": sn, **st})

    if not local_view and not follower_view:
        status = "FAIL"
    elif stale_local or bad_followers:
        status = "DEGRADED" if (len(stale_local) + len(bad_followers)) <= 1 else "FAIL"
    else:
        status = "PASS"

    return {
        "name": "heartbeat freshness",
        "status": status,
        "fresh_threshold_s": HEARTBEAT_FRESH_S,
        "local_lastseen": local_view,
        "follower_replication": follower_view,
        "stale_local_peers": stale_local,
        "lagging_followers": bad_followers,
    }


def check_routes(routez_local: dict) -> dict:
    """Local /routez has N-1 routes for an N-node cluster.

    Cross-peer routez requires hitting :8222 on mac1/mac2, which crosses the
    network and isn't part of the read-only RPC surface. Local-only check.
    """
    if "error" in routez_local:
        return {"name": "cluster route consistency (local)", "status": "FAIL",
                "error": routez_local["error"]}
    n_routes = routez_local.get("num_routes", 0) or 0
    routes = routez_local.get("routes", []) or []
    remote_names = sorted({(r.get("remote_name") or "").strip()
                           for r in routes if r.get("remote_name")})

    expected = len(PEERS) - 1
    status = "PASS" if (n_routes >= expected and len(remote_names) >= expected) \
        else ("DEGRADED" if n_routes >= 1 else "FAIL")
    return {
        "name": "cluster route consistency (local)",
        "status": status,
        "local_node": LOCAL_NODE,
        "num_routes": n_routes,
        "expected_min": expected,
        "remote_peers_via_routes": remote_names,
        "routes_summary": [
            {"rid": r.get("rid"), "remote_name": r.get("remote_name"),
             "ip": f"{r.get('ip')}:{r.get('port')}", "rtt": r.get("rtt"),
             "did_solicit": r.get("did_solicit")}
            for r in routes
        ],
    }


def _stats_counters(view: dict | None) -> dict[str, int]:
    """Extract integer-valued counters from rpc.status output. We track every
    key under `stats` plus standard `_count`/`_total` suffixes anywhere."""
    out: dict[str, int] = {}
    if not view:
        return out
    stats = view.get("stats") or {}
    if isinstance(stats, dict):
        for k, v in stats.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[f"stats.{k}"] = int(v)

    def walk(o, path: str):
        if isinstance(o, dict):
            for k, v in o.items():
                kp = f"{path}.{k}" if path else k
                walk(v, kp)
        elif isinstance(o, (int, float)) and not isinstance(o, bool):
            kn = path.rsplit(".", 1)[-1]
            if kn.endswith("_count") or kn.endswith("_total"):
                out[path] = int(o)
    walk(view, "")
    return out


def check_counter_monotonicity(snap_a: dict[str, dict | None],
                               snap_b: dict[str, dict | None]) -> dict:
    rows: list[dict] = []
    decreases: list[dict] = []
    for peer in PEERS:
        a = _stats_counters(snap_a.get(peer))
        b = _stats_counters(snap_b.get(peer))
        common = sorted(set(a) & set(b))
        peer_dec = []
        for k in common:
            if b[k] < a[k]:
                peer_dec.append({"peer": peer, "key": k, "before": a[k], "after": b[k]})
        rows.append({
            "peer": peer,
            "counters_observed": len(common),
            "snapshot_keys_a": len(a),
            "snapshot_keys_b": len(b),
            "decreases": len(peer_dec),
        })
        decreases.extend(peer_dec)

    if all(r["counters_observed"] == 0 for r in rows):
        status = "FAIL"
    elif decreases:
        status = "FAIL"
    else:
        # Some peers may have 0 counters — treat as degraded
        zero_obs = [r["peer"] for r in rows if r["counters_observed"] == 0]
        status = "DEGRADED" if zero_obs else "PASS"
    return {
        "name": "stats counter monotonicity",
        "status": status,
        "gap_s": COUNTER_GAP_S,
        "per_peer": rows,
        "decreases": decreases,
    }


def check_stream_subscribers(jsz: dict, peer_status: dict[str, dict | None]) -> dict:
    """FLEET_MESSAGES must have all 3 nodes participating as stream replicas
    (= guaranteed cluster-layer subscription interest from each), and
    rpc.<peer>.status must succeed for all 3 (= each daemon is alive and
    holding NATS subscriptions to fleet.* subjects).
    """
    meta = _stream_from_jsz(jsz, "FLEET_MESSAGES") or {}
    members = set(meta.get("members") or [])
    rpc_alive = {p: bool(v) for p, v in peer_status.items()}

    sub: list[str] = []
    if not set(PEERS).issubset(members):
        sub.append("FAIL")
    if not all(rpc_alive.values()):
        # mac1 historically only answers via 'health' fallback — degraded if so
        thin = [p for p, v in peer_status.items()
                if v and len(v) <= 7 and "lastSeen_per_peer" in v]
        if any(not rpc_alive[p] for p in PEERS):
            sub.append("FAIL")
        elif thin:
            sub.append("DEGRADED")

    return {
        "name": "FLEET_MESSAGES subscriber count (replicas + RPC liveness)",
        "status": merge_status(sub) if sub else "PASS",
        "stream_members": sorted(members),
        "rpc_alive_per_peer": rpc_alive,
        "rpc_method_used": {p: (v or {}).get("_rpc_method") for p, v in peer_status.items()},
        "expected_members": list(PEERS),
    }


# ───────────────────────────── reporter ─────────────────────────────

def render_report(results: dict) -> str:
    L: list[str] = []
    L.append("# RACE AK — Fleet-Wide Consistency Status")
    L.append("")
    L.append(f"**Generated**: {results['timestamp']}  ")
    L.append(f"**Origin node**: `{LOCAL_NODE}`  ")
    L.append(f"**Peers tested**: {', '.join(f'`{p}`' for p in PEERS)}  ")
    L.append(f"**Cluster**: `{results['varz'].get('cluster','?')}`  ")
    L.append(f"**Server**: `{results['varz'].get('server_name','?')}` v{results['varz'].get('version','?')}  ")
    L.append(f"**Connections**: {results['varz'].get('connections','?')}  ")
    L.append(f"**Counter gap**: {COUNTER_GAP_S}s  ")
    L.append("")

    L.append("## Summary")
    L.append("")
    L.append("| # | Check | Status |")
    L.append("|---|-------|--------|")
    for i, c in enumerate(results["checks"], 1):
        retried = " (retried)" if c.get("retried") else ""
        L.append(f"| {i} | {c['name']}{retried} | **{c['status']}** |")
    L.append("")

    overall = merge_status([c["status"] for c in results["checks"]])
    L.append(f"**Overall**: **{overall}**")
    L.append("")

    L.append("## Per-Peer RPC Availability")
    L.append("")
    for peer in PEERS:
        v = results["peer_status_available"].get(peer)
        method = results["peer_status_method"].get(peer) or "—"
        L.append(f"- `{peer}`: {'OK' if v else 'NO RESPONSE'} (via `rpc.{peer}.{method}`)")
    L.append("")

    L.append("## Detail")
    L.append("")
    for c in results["checks"]:
        L.append(f"### {c['name']} — {c['status']}")
        L.append("")
        L.append("```json")
        L.append(json.dumps({k: v for k, v in c.items() if k != "name"},
                            indent=2, default=str))
        L.append("```")
        L.append("")

    L.append("## Methodology")
    L.append("")
    L.append("- Each peer's `rpc.<peer>.status` (preferred) or `rpc.<peer>.health` (fallback)")
    L.append("  payload is collected via NATS request/reply. Read-only — no inbox writes,")
    L.append("  no state changes, no service restarts.")
    L.append("- Stream replication: parsed from local `:8222/jsz?streams=true`. Local view")
    L.append("  is authoritative because the leader (`mac3`) sees all replica health.")
    L.append("- Election: `delegation.chief_node` from the local daemon `/health`.")
    L.append("  Cross-peer agreement is currently NOT verifiable via RPC — recorded as")
    L.append("  DEGRADED with the local value captured.")
    L.append("- Peer map: `peers` field from each peer's RPC status; mutuality across all")
    L.append("  unordered (a,b) pairs.")
    L.append("- Heartbeat freshness: local `/health` `peers.<x>.lastSeen` < 60s, plus")
    L.append("  JetStream follower `current=true` and `lag==0` cross-validation.")
    L.append("- Routes: local-only `:8222/routez`. Cross-peer routez requires opening port")
    L.append("  8222 across the network — not done in this read-only check.")
    L.append("- Counter monotonicity: snapshots taken `gap_s` apart. Any decrease in any")
    L.append("  peer's `stats.*` integer counter or any `_count`/`_total` leaf ⇒ FAIL.")
    L.append("- Subscriber count: FLEET_MESSAGES leader+followers must include all 3 nodes,")
    L.append("  AND each peer's daemon must answer RPC (proves it holds live subscriptions).")
    L.append("- Retry: any check returning FAIL is re-evaluated once after a brief pause")
    L.append("  before the result is recorded. Retried checks are tagged `(retried)`.")
    L.append("")
    return "\n".join(L)


# ───────────────────────────── main ─────────────────────────────────

async def main_async() -> dict:
    started = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    results: dict = {
        "timestamp": started,
        "local_node": LOCAL_NODE,
        "peers": list(PEERS),
        "varz": {},
        "peer_status_available": {},
        "peer_status_method": {},
        "checks": [],
    }

    # 0. local NATS varz (best-effort)
    try:
        v = http_json("/varz")
        results["varz"] = {
            "server_name": v.get("server_name"),
            "version": v.get("version"),
            "cluster": (v.get("cluster") or {}).get("name"),
            "connections": v.get("connections"),
        }
    except Exception as e:
        results["varz"] = {"error": str(e)}

    # 1. NATS connect
    try:
        nc = await nats.connect(NATS_URL, name=f"race-ak-{LOCAL_NODE}", connect_timeout=3)
    except Exception as e:
        results["checks"].append({"name": "nats connect", "status": "FAIL", "error": str(e)})
        return results

    # 2. Snapshot A — peer rpc + local fixtures
    snap_a = await gather_peer_status(nc)
    results["peer_status_available"] = {p: bool(v) for p, v in snap_a.items()}
    results["peer_status_method"] = {p: (v or {}).get("_rpc_method") for p, v in snap_a.items()}
    jsz_a = fetch_local_jsz()
    routez_a = fetch_local_routez()
    health_a = fetch_local_health()

    # ── Define checks with one-retry semantics
    async def run_with_retry(fn, *, refetch_jsz: bool = False,
                             refetch_status: bool = False,
                             refetch_routes: bool = False,
                             refetch_health: bool = False):
        result = fn()
        if result.get("status") == "FAIL":
            await asyncio.sleep(1.5)
            nonlocal jsz_a, routez_a, health_a, snap_a
            if refetch_jsz:
                jsz_a = fetch_local_jsz()
            if refetch_routes:
                routez_a = fetch_local_routez()
            if refetch_health:
                health_a = fetch_local_health()
            if refetch_status:
                snap_a = await gather_peer_status(nc, retries=0)
                results["peer_status_available"] = {p: bool(v) for p, v in snap_a.items()}
                results["peer_status_method"] = {p: (v or {}).get("_rpc_method") for p, v in snap_a.items()}
            result2 = fn()
            if result2.get("status") != "FAIL":
                result2["retried"] = True
                return result2
            result["retried"] = True
        return result

    results["checks"].append(
        await run_with_retry(lambda: check_stream_replication(jsz_a, "FLEET_MESSAGES"),
                             refetch_jsz=True))
    results["checks"].append(
        await run_with_retry(lambda: check_stream_replication(jsz_a, "FLEET_EVENTS"),
                             refetch_jsz=True))
    results["checks"].append(
        await run_with_retry(lambda: check_election(health_a, snap_a),
                             refetch_health=True, refetch_status=True))
    results["checks"].append(
        await run_with_retry(lambda: check_peer_map_mutual(snap_a),
                             refetch_status=True))
    results["checks"].append(
        await run_with_retry(lambda: check_heartbeat_freshness(health_a, jsz_a),
                             refetch_health=True, refetch_jsz=True))
    results["checks"].append(
        await run_with_retry(lambda: check_routes(routez_a),
                             refetch_routes=True))
    results["checks"].append(
        await run_with_retry(lambda: check_stream_subscribers(jsz_a, snap_a),
                             refetch_jsz=True, refetch_status=True))

    # 3. Snapshot B — wait COUNTER_GAP_S, refetch peer status for monotonicity
    print(f"[race-ak] sleeping {COUNTER_GAP_S}s for counter-monotonicity snapshot…",
          flush=True)
    await asyncio.sleep(COUNTER_GAP_S)
    snap_b = await gather_peer_status(nc)

    mono = check_counter_monotonicity(snap_a, snap_b)
    if mono["status"] == "FAIL":
        await asyncio.sleep(2.0)
        snap_b2 = await gather_peer_status(nc, retries=0)
        mono2 = check_counter_monotonicity(snap_a, snap_b2)
        if mono2["status"] != "FAIL":
            mono2["retried"] = True
            mono = mono2
        else:
            mono["retried"] = True
    results["checks"].append(mono)

    await nc.drain()
    return results


def main() -> int:
    results = asyncio.run(main_async())
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(render_report(results))
    REPORT_JSON.write_text(json.dumps(results, indent=2, default=str))

    print(f"Report : {REPORT_MD}")
    print(f"JSON   : {REPORT_JSON}")
    statuses = [c["status"] for c in results["checks"]]
    for c in results["checks"]:
        rt = " (retried)" if c.get("retried") else ""
        print(f"  [{c['status']}] {c['name']}{rt}")
    overall = merge_status(statuses)
    print(f"Overall: {overall}")
    if overall == "FAIL":
        return 2
    if overall == "DEGRADED":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
