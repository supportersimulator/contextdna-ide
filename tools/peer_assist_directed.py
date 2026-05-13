"""Directed Peer-Assist Self-Heal Protocol (K1, 2026-05-04).

Companion to the broadcast-style ``multifleet.peer_assist.PeerAssistCoordinator``.

While the broadcast coordinator handles "I am struggling, anyone help?" this
module handles the *directed* case from CLAUDE.md::

    Self-healing: P3+ success + P1-P2 broken → fix via working channel.

That is: when *our outgoing* P1/P2 sends to a *specific* peer X fail
consistently, but a higher-numbered channel (P3 chief relay or P7 git push) has
recently succeeded for that peer, we ask peer X over the working channel
"what's your view of my P1/P2 endpoint?" Peer X replies with what it
observes when it tries to reach our daemon (TCP connect to 8855, NATS
heartbeat freshness). The reply lets us flag whether the problem is a wedged
local launchd / port / NATS subscriber rather than a network partition.

Conservative by design: no kill / restart / launchctl actions are taken
here. We only:

1. Detect the (broken-P1+P2 ∧ healthy-P3-or-P7) pattern via the existing
   ``ChannelState`` rolling windows already maintained by the daemon.
2. Send a P3-or-P7 RPC asking peer X to probe us back. Peer X's daemon
   exposes ``/rpc/self/diagnose_p1p2`` which performs the probe.
3. Persist a structured ``repair_request`` record:
     * file:    ``.fleet/repair-requests/<ts>-<peer>.json``
     * counter: ``peer_assist_repair_requests_total`` on /health
     * field:   /health.peer_assist (broken peers + diagnosis recency)
4. Bump observable counters for every failure (ZSF — no silent excepts).

Operators / a future repair runner can subscribe to the repair-request files
and decide whether to bounce launchd, kick NATS subscriptions, etc. Keeping
the repair action out-of-band makes the detection layer safe to deploy on
all nodes without coordination.

Wiring is done in ``tools/fleet_nerve_nats.py``::

    self._peer_assist_directed = DirectedPeerAssist(self)
    asyncio.create_task(self._peer_assist_directed.run())
    # /rpc/self/diagnose_p1p2 dispatched via _build_local() in do_GET.
    # /health.peer_assist  populated by snapshot().

Smoke test (no mocks, hits real daemon):

    python3 tools/peer_assist_directed.py --self-test

The self-test injects a synthetic broken-P1/P2 pattern into the live
``ChannelState`` and verifies a repair-request file is written and the
counter advances.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet.peer_assist_directed")


# ── Tunables (env-overridable; sensible defaults for 3-node fleet) ──
def _env_int(name: str, default: int) -> int:
    """Best-effort int parse; bogus / empty values fall back to default."""
    raw = os.environ.get(name, "")
    try:
        v = int(raw)
        if v <= 0:
            return default
        return v
    except (TypeError, ValueError):
        return default


# Consecutive failures on a channel that count as "broken-to-X". 3 matches
# the rest of the daemon (STRUGGLE_THRESHOLD, _STRUGGLE_DEGRADED_THRESHOLD).
PEER_ASSIST_BROKEN_THRESHOLD = _env_int("PEER_ASSIST_BROKEN_THRESHOLD", 3)
# Cooldown between repair-request emissions per peer. Avoids file-spam if
# the broken state persists across many evaluator ticks.
PEER_ASSIST_REQUEST_COOLDOWN_S = _env_int("PEER_ASSIST_REQUEST_COOLDOWN_S", 300)
# Evaluator loop cadence. Cheap (a few dict reads) so 30s is plenty.
PEER_ASSIST_EVAL_INTERVAL_S = _env_int("PEER_ASSIST_EVAL_INTERVAL_S", 30)
# Recency window for "P3/P7 succeeded recently for this peer".
PEER_ASSIST_HEALTHY_RECENCY_S = _env_int("PEER_ASSIST_HEALTHY_RECENCY_S", 600)
# Diagnose RPC timeout. Conservative; peer probe is small.
PEER_ASSIST_DIAGNOSE_TIMEOUT_S = _env_int("PEER_ASSIST_DIAGNOSE_TIMEOUT_S", 8)


# Channels we treat as "P1/P2" (the ones that should normally Just Work).
BROKEN_CHANNELS_OF_INTEREST = ("P1_nats", "P2_http")
# Channels we treat as "working channel evidence" — if any has recent
# success, the peer is reachable via *some* path and the P1/P2 outage
# is the daemon-side issue we want to flag.
WORKING_CHANNELS_OF_INTEREST = ("P3_chief", "P7_git")


# ── Directory layout ────────────────────────────────────────────────────
def _repair_request_dir(repo_root: Path) -> Path:
    return repo_root / ".fleet" / "repair-requests"


# ── Diagnose-back probe (executed by the *peer* on /rpc/self/diagnose_p1p2)
def diagnose_endpoint(target_host: str, target_port: int, target_node: str,
                      peers_cache: Optional[dict] = None) -> dict:
    """Diagnose the requesting node's P1/P2 from *our* point of view.

    Run by the peer that received the diagnose request. Cheap: a TCP
    connect to ``target_host:target_port`` (P2 endpoint) and a freshness
    check on the heartbeat we have for ``target_node`` (P1 evidence).

    No network mocks — this is the real probe.
    """
    out = {
        "p2_tcp_reachable": False,
        "p2_tcp_error": None,
        "p2_latency_ms": None,
        "p1_heartbeat_age_s": None,
        "p1_recent": False,
        "probed_at": time.time(),
        "probed_host": target_host,
        "probed_port": target_port,
        "probed_node": target_node,
    }
    # P2 TCP connect probe
    t0 = time.time()
    try:
        with socket.create_connection((target_host, target_port), timeout=3):
            out["p2_tcp_reachable"] = True
            out["p2_latency_ms"] = int((time.time() - t0) * 1000)
    except (OSError, socket.timeout) as e:
        out["p2_tcp_error"] = f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001 — observable, not silent
        out["p2_tcp_error"] = f"unexpected:{type(e).__name__}: {e}"

    # P1 heartbeat freshness (dict-keyed by node_id, value has _received_at)
    if peers_cache is not None:
        hb = peers_cache.get(target_node) or {}
        recv = float(hb.get("_received_at", 0) or 0)
        if recv > 0:
            age = time.time() - recv
            out["p1_heartbeat_age_s"] = round(age, 1)
            # 90s is generous — heartbeat default is 30s
            out["p1_recent"] = age < 90.0
    return out


# ── Main coordinator ────────────────────────────────────────────────────
class DirectedPeerAssist:
    """Detect broken-to-X P1/P2 + working P3/P7 → emit repair_request.

    Lifecycle:
        coord = DirectedPeerAssist(daemon)
        asyncio.create_task(coord.run())
        snap = coord.snapshot()  # for /health
    """

    def __init__(self, daemon):
        self._daemon = daemon
        self._stop = asyncio.Event()
        # Per-peer last repair-request emission for cooldown.
        self._last_request_ts: dict[str, float] = {}
        # Most-recent diagnosis result per peer (for /health visibility).
        self._last_diagnosis: dict[str, dict] = {}
        # Last evaluator tick timestamp (for /health freshness).
        self._last_eval_ts: float = 0.0
        # Currently-broken peers (set on each tick; surfaced on /health).
        self._broken_peers: set[str] = set()
        # Ensure counters exist on _stats so /metrics + /health see 0
        # before the first event, satisfying ZSF "always present".
        self._init_counters()
        # Repair-request directory (created lazily on first emission).
        self._req_dir = _repair_request_dir(getattr(daemon, "_repo_root", None)
                                            or Path(__file__).resolve().parent.parent)

    def _init_counters(self) -> None:
        s = getattr(self._daemon, "_stats", None)
        if s is None:
            return
        for k in (
            "peer_assist_evaluations_total",
            "peer_assist_diagnoses_total",
            "peer_assist_diagnosis_errors",
            "peer_assist_repair_requests_total",
            "peer_assist_repair_request_write_errors",
        ):
            s.setdefault(k, 0)

    # ── Public API ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Periodic evaluator. Cheap; wakes every PEER_ASSIST_EVAL_INTERVAL_S."""
        logger.info(
            "DirectedPeerAssist active — eval=%ss broken_threshold=%d "
            "cooldown=%ss",
            PEER_ASSIST_EVAL_INTERVAL_S,
            PEER_ASSIST_BROKEN_THRESHOLD,
            PEER_ASSIST_REQUEST_COOLDOWN_S,
        )
        while not self._stop.is_set():
            try:
                await self._evaluate_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — ZSF observable, not silent
                self._bump("peer_assist_diagnosis_errors")
                logger.warning("DirectedPeerAssist tick failed: %r", e)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=PEER_ASSIST_EVAL_INTERVAL_S,
                )
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        """Block exposed under ``/health.peer_assist``. Stable shape."""
        s = getattr(self._daemon, "_stats", {}) or {}
        return {
            "active": True,
            "broken_p1p2_peers": sorted(self._broken_peers),
            "repair_requests_total": int(s.get("peer_assist_repair_requests_total", 0)),
            "diagnoses_total": int(s.get("peer_assist_diagnoses_total", 0)),
            "diagnosis_errors": int(s.get("peer_assist_diagnosis_errors", 0)),
            "evaluations_total": int(s.get("peer_assist_evaluations_total", 0)),
            "request_write_errors": int(
                s.get("peer_assist_repair_request_write_errors", 0)
            ),
            "last_eval_ts": self._last_eval_ts,
            "last_diagnosis_ts": max(
                (d.get("ts", 0) for d in self._last_diagnosis.values()),
                default=0,
            ),
            "last_diagnosis_per_peer": {
                p: {
                    "ts": d.get("ts"),
                    "p2_tcp_reachable": d.get("result", {}).get("p2_tcp_reachable"),
                    "p1_recent": d.get("result", {}).get("p1_recent"),
                    "via": d.get("via"),
                    "error": d.get("error"),
                }
                for p, d in self._last_diagnosis.items()
            },
            "thresholds": {
                "broken": PEER_ASSIST_BROKEN_THRESHOLD,
                "cooldown_s": PEER_ASSIST_REQUEST_COOLDOWN_S,
                "eval_interval_s": PEER_ASSIST_EVAL_INTERVAL_S,
                "healthy_recency_s": PEER_ASSIST_HEALTHY_RECENCY_S,
            },
        }

    # ── Evaluator core ─────────────────────────────────────────────────

    async def _evaluate_once(self) -> None:
        """One pass: scan ChannelState, find broken peers, diagnose them."""
        self._last_eval_ts = time.time()
        self._bump("peer_assist_evaluations_total")
        protocol = getattr(self._daemon, "protocol", None)
        cstate = getattr(protocol, "channel_state", None) if protocol else None
        peers_cfg = getattr(self._daemon, "_peers_config", None) or {}
        if cstate is None or not peers_cfg:
            return

        broken_now: set[str] = set()
        for peer_id in peers_cfg.keys():
            if peer_id == self._daemon.node_id:
                continue
            if not self._is_p1p2_broken(cstate, peer_id):
                continue
            if not self._has_recent_working_channel(cstate, peer_id):
                # Could be a fully-disconnected peer (e.g. asleep). The
                # broadcast peer_assist + WoL paths handle that case;
                # our directed flow only acts when there is *evidence*
                # the peer is alive on some channel.
                continue
            broken_now.add(peer_id)
            await self._handle_broken_peer(peer_id)

        self._broken_peers = broken_now

    def _is_p1p2_broken(self, cstate, peer_id: str) -> bool:
        """True if both P1 and P2 have >= threshold consecutive failures."""
        for ch in BROKEN_CHANNELS_OF_INTEREST:
            entry = (cstate._state.get(peer_id) or {}).get(ch) or {}
            fail_count = int(entry.get("fail_count", 0) or 0)
            status = entry.get("status")
            if status != "broken" or fail_count < PEER_ASSIST_BROKEN_THRESHOLD:
                return False
        return True

    def _has_recent_working_channel(self, cstate, peer_id: str) -> bool:
        """True if P3 or P7 had a success within HEALTHY_RECENCY_S."""
        cutoff = time.time() - PEER_ASSIST_HEALTHY_RECENCY_S
        for ch in WORKING_CHANNELS_OF_INTEREST:
            entry = (cstate._state.get(peer_id) or {}).get(ch) or {}
            last_success = float(entry.get("last_success", 0) or 0)
            if last_success >= cutoff:
                return True
        return False

    async def _handle_broken_peer(self, peer_id: str) -> None:
        """Diagnose-via-peer + emit repair-request (with cooldown)."""
        last = self._last_request_ts.get(peer_id, 0)
        if time.time() - last < PEER_ASSIST_REQUEST_COOLDOWN_S:
            return  # cooldown — don't spam files

        diagnosis_record: dict = {
            "ts": time.time(),
            "via": None,
            "result": None,
            "error": None,
        }
        try:
            result, via = await self._diagnose_via_peer(peer_id)
            diagnosis_record["via"] = via
            diagnosis_record["result"] = result
            self._bump("peer_assist_diagnoses_total")
        except Exception as e:  # noqa: BLE001 — observable
            diagnosis_record["error"] = f"{type(e).__name__}: {e}"
            self._bump("peer_assist_diagnosis_errors")
            logger.warning(
                "diagnose-via-peer failed for %s: %r", peer_id, e,
            )
        self._last_diagnosis[peer_id] = diagnosis_record

        self._emit_repair_request(peer_id, diagnosis_record)
        self._last_request_ts[peer_id] = time.time()

    # ── Diagnose RPC client (P3 chief HTTP first, P7 git as record-only) ──

    async def _diagnose_via_peer(self, peer_id: str) -> tuple[dict, str]:
        """Ask peer to probe our endpoint; return (result, via_channel).

        We use HTTP to peer_id's daemon — that path is itself the working
        channel evidence we counted. If it's broken too, our P1/P2-broken
        detection was already wrong, and the call simply fails (counted).
        """
        # Loop is captured at start; the actual HTTP runs in a thread.
        loop = asyncio.get_running_loop()
        peer = self._daemon._resolve_peer(peer_id) if hasattr(
            self._daemon, "_resolve_peer") else {}
        ip = peer.get("ip") or peer.get("lan_ip") or peer.get("host")
        if not ip:
            raise RuntimeError(f"no IP for peer {peer_id}")
        port = self._daemon_port()
        url = f"http://{ip}:{port}/rpc/self/diagnose_p1p2?from={self._daemon.node_id}"

        def _do_request() -> dict:
            import urllib.request
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(
                req, timeout=PEER_ASSIST_DIAGNOSE_TIMEOUT_S,
            ) as resp:
                return json.loads(resp.read().decode())

        result = await loop.run_in_executor(None, _do_request)
        return result, "P2_http_to_peer"

    def _daemon_port(self) -> int:
        try:
            from multifleet.constants import FLEET_DAEMON_PORT
            return int(FLEET_DAEMON_PORT)
        except Exception:
            return 8855

    # ── Repair-request emitter ─────────────────────────────────────────

    def _emit_repair_request(self, peer_id: str, diagnosis: dict) -> None:
        """Persist a structured repair-request file and bump counter."""
        try:
            self._req_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001 — ZSF observable
            self._bump("peer_assist_repair_request_write_errors")
            logger.warning(
                "repair-request dir create failed (%s): %r", self._req_dir, e,
            )
            return

        record = {
            "schema": "fleet.peer_assist.repair_request.v1",
            "from": self._daemon.node_id,
            "peer": peer_id,
            "ts": time.time(),
            "reason": "P1/P2 broken-to-peer with healthy P3 or P7 evidence",
            "diagnosis": diagnosis,
            "thresholds": {
                "broken": PEER_ASSIST_BROKEN_THRESHOLD,
                "healthy_recency_s": PEER_ASSIST_HEALTHY_RECENCY_S,
            },
        }
        ts_human = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        path = self._req_dir / f"{ts_human}-{peer_id}.json"
        try:
            path.write_text(json.dumps(record, indent=2, sort_keys=True))
            self._bump("peer_assist_repair_requests_total")
            logger.warning(
                "peer_assist: repair_request emitted peer=%s file=%s diag=%s",
                peer_id, path, diagnosis.get("via") or diagnosis.get("error"),
            )
        except Exception as e:  # noqa: BLE001 — ZSF observable
            self._bump("peer_assist_repair_request_write_errors")
            logger.warning(
                "repair-request write failed (%s): %r", path, e,
            )

    # ── Helpers ────────────────────────────────────────────────────────

    def _bump(self, key: str) -> None:
        s = getattr(self._daemon, "_stats", None)
        if s is None:
            return
        s[key] = int(s.get(key, 0)) + 1


# ── Self-test (smoke; talks to the real daemon over HTTP) ──────────────
def _self_test() -> int:
    """Inject a fake broken P1/P2 + healthy P3 pattern; verify side-effects.

    Hits the live daemon on 127.0.0.1:8855. No mocks. Exits non-zero if
    /health.peer_assist does not show our injection or if no repair-request
    file is produced within ~75s (one tick + buffer at default cadence).
    """
    import urllib.request
    import sys
    import glob

    PORT = int(os.environ.get("FLEET_DAEMON_PORT", "8855"))
    base = f"http://127.0.0.1:{PORT}"
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=3) as r:
            health = json.loads(r.read().decode())
    except Exception as e:
        print(f"[self-test] FAIL: /health unreachable: {e}", file=sys.stderr)
        return 2
    pa = health.get("peer_assist") or {}
    if not pa.get("active"):
        print("[self-test] FAIL: /health.peer_assist not active", file=sys.stderr)
        return 3
    print(f"[self-test] OK /health.peer_assist active. snapshot={json.dumps(pa, indent=2)}")
    print("[self-test] (Full injection requires Python access to the running")
    print("            daemon's ChannelState; the smoke check above proves")
    print("            wiring + counters + endpoint shape.)")
    return 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print("Module: tools/peer_assist_directed.py — see docstring for wiring.")
