#!/usr/bin/env python3
"""
Fleet Nerve v2 -- NATS-based real-time fleet communication.

Replaces the HTTP daemon with NATS pub/sub. Zero port conflicts, zero polling,
instant message delivery, built-in offline queuing via JetStream.

Architecture:
  - mac1 runs nats-server (chief) -- all nodes connect to nats://mac1:4222
  - Each node subscribes to fleet.<node>.> and fleet.all.>
  - Messages publish instantly -- no HTTP, no seed files, no hooks
  - JetStream persists messages for offline nodes (they get them on reconnect)
  - Request/reply pattern for synchronous queries (health, status)

Subjects:
  fleet.<node>.message     -- direct message to a node
  fleet.all.message        -- broadcast to all nodes
  fleet.all.health         -- broadcast health request (fire-and-forget)
  fleet.<node>.task        -- autonomous task assignment
  fleet.all.alert          -- fleet-wide alert
  fleet.heartbeat          -- periodic heartbeat (all nodes publish)
  fleet.<node>.ack         -- delivery acknowledgment back to sender
  rpc.<node>.health        -- request/reply health check (NOT fleet.*, see NOTE)
  rpc.<node>.status        -- request/reply status snapshot
  rpc.<node>.commits       -- request/reply recent git commits
  rpc.<node>.tracks        -- request/reply current tracks

NOTE: single-node health uses rpc.<node>.health (not fleet.<node>.health) so
request/reply escapes the JetStream FLEET_MESSAGES stream (subject fleet.>).
If the request landed on fleet.*, JetStream's publish-ack would race the
subscriber's reply and deliver {"stream":"FLEET_MESSAGES","seq":N} instead
of real health data -- silently breaking !fleet ping and !fleet health.

Usage:
  python3 tools/fleet_nerve_nats.py                    # start daemon
  python3 tools/fleet_nerve_nats.py send mac2 "hello"  # send message
  python3 tools/fleet_nerve_nats.py broadcast "hello"  # broadcast
  python3 tools/fleet_nerve_nats.py health              # fleet health
  python3 tools/fleet_nerve_nats.py monitor             # live monitor
"""

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Handle import for both pip install locations
try:
    import nats
    from nats.errors import ConnectionClosedError, TimeoutError as NatsTimeout
    # NoRespondersError: raised by nc.request() when no subscribers for a subject.
    # This IS a peer-death signal -- no separate probe loop needed.
    try:
        from nats.errors import NoRespondersError
    except ImportError:  # older nats-py exposes it via aio.errors
        try:
            from nats.aio.errors import ErrNoRespondersError as NoRespondersError  # type: ignore
        except ImportError:
            class NoRespondersError(Exception):  # type: ignore
                """Fallback stub when nats-py version predates NoRespondersError."""
                pass
except ImportError:
    print("ERROR: nats-py not installed. Run: pip3 install nats-py")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "multi-fleet"))

# Centralized port constants — env-overridable at import time
from multifleet.constants import FLEET_DAEMON_PORT, NATS_PORT  # noqa: E402

# ── Invariance gates (graceful import — None on slim fleet installs) ──
# Wires FleetRepairGate + FleetSendGate into repair_with_escalation() and
# send_with_fallback() so repair/send operations enforce stale-diagnostic,
# concurrent-repair, allowlist, chief-only-L4, envelope, schema, quorum,
# and stress-based checks before proceeding.
try:
    from multifleet.invariance_gates import FleetRepairGate, FleetSendGate
except ImportError:
    FleetRepairGate = None
    FleetSendGate = None

# ── CapKVFreshness invariant (VV3-B — stale capability window detection) ──
try:
    from multifleet.invariance_rules import CapKVFreshnessInvariant as _CapKVFreshnessInvariant
    _cap_kv_freshness_inv = _CapKVFreshnessInvariant()
except ImportError:
    _CapKVFreshnessInvariant = None
    _cap_kv_freshness_inv = None

# ── Stress score (graceful import — stubs to 0.0 when unavailable) ──
try:
    from multifleet.quorum.stress_score import compute_stress_score
except ImportError:
    compute_stress_score = None

# ── Unified event bus + state sync (graceful — None on slim installs) ──
try:
    from multifleet.fleet_event_bus import startup_event_bus
    from multifleet.node_profile import detect_local_profile
    from multifleet.fleet_state_sync import FleetStateSync
except ImportError:
    startup_event_bus = None
    detect_local_profile = None
    FleetStateSync = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [fleet-nats/%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fleet_nats")


# ── Observability — recent-errors capture (RACE B6) ──
class _ObservabilityErrorHandler(logging.Handler):
    """Logging handler that tees ERROR+ records into a bounded ring buffer.

    Used by ``FleetNerveNATS`` to expose the last N errors via
    ``/health.recent_errors`` without requiring operators to SSH in and tail
    a log file. Minimal by design: timestamp, logger name, level, truncated
    message (≤200 chars). Stack traces are intentionally omitted — /health
    must stay cheap to render and ship over NATS request/reply.
    """

    def __init__(self, ring_buffer, max_msg_len: int = 200):
        super().__init__()
        self._ring = ring_buffer
        self._max_msg_len = max_msg_len

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if len(msg) > self._max_msg_len:
                msg = msg[: self._max_msg_len - 1] + "…"
            self._ring.append({
                "ts": record.created,
                "logger": record.name,
                "level": record.levelname,
                "message": msg,
            })
        except Exception:
            # Must never raise — a broken handler could loop inside logging.
            pass


# ── Redis Counter Fallback (restored — 68bc82435 deleted via --theirs; WaveD/WaveI) ──

import threading as _counter_threading

_COUNTER_FALLBACK_PATH = Path(
    os.environ.get("FLEET_COUNTER_FALLBACK_PATH", "/tmp/fleet-counters.jsonl")
)
_COUNTER_FALLBACK_LOCK = _counter_threading.Lock()


def _incr_counter_redis(counter_name: str, value: int = 1,
                        *, daemon_stats: Optional[dict] = None) -> None:
    """Increment counter_name in Redis; JSONL file fallback on failure. ZSF."""
    try:
        import redis as _redis  # type: ignore
        client = _redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            db=int(os.environ.get("REDIS_DB", "0")),
            socket_timeout=1.0, socket_connect_timeout=1.0,
        )
        client.incr(f"fleet:{counter_name}", value)
        return
    except Exception as redis_err:
        logger.debug("_incr_counter_redis: Redis unavailable for %s: %s", counter_name, redis_err)
    try:
        line = json.dumps({"counter": counter_name, "value": value, "ts": time.time()})
        with _COUNTER_FALLBACK_LOCK:
            with open(_COUNTER_FALLBACK_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if daemon_stats is not None:
            daemon_stats["counter_file_fallback_writes"] = daemon_stats.get("counter_file_fallback_writes", 0) + 1
    except Exception as file_err:
        logger.warning("_incr_counter_redis: file fallback failed for %s: %s", counter_name, file_err)
        if daemon_stats is not None:
            daemon_stats["counter_file_fallback_errors"] = daemon_stats.get("counter_file_fallback_errors", 0) + 1


# ── Inbound Message Validation ──

VALID_MESSAGE_TYPES = frozenset({
    "context", "task", "alert", "repair", "heartbeat",
    "ack", "chain", "reply",
    # Extended types used by fleet protocol
    "sync", "rebuttal_proposal", "rebuttal_dissent", "rebuttal_resolution",
    # Observability / fleet-local signals that were being rejected as noise
    # but are legitimate intra-fleet subjects: "idle" + "idle-suggestions"
    # published by the idle watcher, "health" used for ad-hoc probes,
    # "delegation" + "delegation_result" for chief→worker task flow.
    "idle", "idle-suggestions", "health",
    "delegation", "delegation_result",
})

PAYLOAD_SUBJECT_MAX = 200
PAYLOAD_BODY_MAX = 50000


def validate_inbound_message(data: dict) -> Optional[str]:
    """Validate an inbound fleet message dict.

    Returns None if valid, or a string describing the first validation error.
    Pure Python -- no external schema library.
    """
    if not isinstance(data, dict):
        return "message must be a JSON object"

    # Required: type
    msg_type = data.get("type")
    if msg_type is None:
        return "missing required field 'type'"
    if not isinstance(msg_type, str):
        return "'type' must be a string"
    if msg_type not in VALID_MESSAGE_TYPES:
        return f"unknown message type '{msg_type}' (valid: {', '.join(sorted(VALID_MESSAGE_TYPES))})"

    # Required: to
    to = data.get("to")
    if to is None:
        return "missing required field 'to'"
    if not isinstance(to, str) or not to:
        return "'to' must be a non-empty string"

    # Required: from
    frm = data.get("from")
    if frm is None:
        return "missing required field 'from'"
    if not isinstance(frm, str) or not frm:
        return "'from' must be a non-empty string"

    # Optional: id (string)
    if "id" in data and not isinstance(data["id"], str):
        return "'id' must be a string"

    # Optional: timestamp (ISO string)
    if "timestamp" in data:
        ts = data["timestamp"]
        if not isinstance(ts, str):
            return "'timestamp' must be an ISO-8601 string"

    # Optional: payload (dict with subject/body constraints)
    if "payload" in data:
        payload = data["payload"]
        if not isinstance(payload, dict):
            return "'payload' must be a JSON object"
        if "subject" in payload:
            subj = payload["subject"]
            if not isinstance(subj, str):
                return "'payload.subject' must be a string"
            if len(subj) > PAYLOAD_SUBJECT_MAX:
                return f"'payload.subject' exceeds {PAYLOAD_SUBJECT_MAX} chars ({len(subj)})"
        if "body" in payload:
            body = payload["body"]
            if not isinstance(body, str):
                return "'payload.body' must be a string"
            if len(body) > PAYLOAD_BODY_MAX:
                return f"'payload.body' exceeds {PAYLOAD_BODY_MAX} chars ({len(body)})"

    return None


# ── Config ──
NODE_ID = os.environ.get("MULTIFLEET_NODE_ID") or socket.gethostname().split(".")[0]
def _default_nats_url():
    """Resolve NATS URL from config or env.

    Priority: NATS_URL env > tunnel detection > config file > loopback.
    If an SSH tunnel is forwarding port 4222 locally, use 127.0.0.1 to avoid
    router-blocked direct connections.
    """
    # Check if a local tunnel is forwarding the NATS port
    try:
        import subprocess as _sp
        out = _sp.check_output(["lsof", "-i", f":{NATS_PORT}", "-sTCP:LISTEN"], text=True, timeout=3, stderr=_sp.DEVNULL)
        if "ssh" in out.lower():
            return f"nats://127.0.0.1:{NATS_PORT}"
    except (OSError, _sp.SubprocessError) as e:
        logger.debug(f"_default_nats_url: lsof tunnel probe skipped: {e}")

    # Try legacy peer config file for the chief IP. Public fleets should use
    # .multifleet/config.json (handled below) — this branch preserves backwards
    # compat for existing mac1/mac2/mac3 deployments.
    conf = REPO_ROOT / "scripts" / "3s-network.local.conf"
    if conf.exists():
        # Resolve chief node id from .multifleet/config.json; default to "mac1"
        # so legacy layouts still work unchanged.
        chief_id = "mac1"
        try:
            import json as _json
            _mf_conf = REPO_ROOT / ".multifleet" / "config.json"
            if _mf_conf.exists():
                _cfg = _json.loads(_mf_conf.read_text())
                chief_id = (_cfg.get("chief") or {}).get("nodeId") or chief_id
        except Exception as e:
            logger.debug(f"_default_nats_url: chief-id resolution skipped: {e}")
        peer_prefix = f"PEER_{chief_id}="
        for line in conf.read_text().split("\n"):
            if line.startswith(peer_prefix):
                ip = line.split("@")[-1].strip('"')
                return f"nats://{ip}:{NATS_PORT}"

    # Try multifleet config
    mf_conf = REPO_ROOT / ".multifleet" / "config.json"
    if mf_conf.exists():
        try:
            import json as _json
            c = _json.loads(mf_conf.read_text())
            chief_host = c.get("chief", {}).get("host", "")
            if chief_host:
                return f"nats://{chief_host}:{NATS_PORT}"
        except (OSError, ValueError) as e:
            logger.debug(f"_default_nats_url: multifleet config parse skipped: {e}")

    return f"nats://localhost:{NATS_PORT}"

NATS_URL = os.environ.get("NATS_URL") or _default_nats_url()
SEED_DIR = "/tmp"
STREAM_NAME = "FLEET"
ALL_CHANNELS = ("P1_nats", "P2_http", "P3_chief", "P4_seed", "P5_ssh", "P6_wol", "P7_git")
HEAL_SCORE_THRESHOLD = 5  # Score below this triggers automatic repair escalation

# ── Channel Priority Constants ──
CHANNEL_P0_CLOUD = "P0_cloud"
CHANNEL_P1_NATS = "P1_nats"
CHANNEL_P2_HTTP = "P2_http"
CHANNEL_P3_CHIEF = "P3_chief"
CHANNEL_P4_SEED = "P4_seed"
CHANNEL_P5_SSH = "P5_ssh"
CHANNEL_P6_WOL = "P6_wol"
CHANNEL_P7_GIT = "P7_git"
CHANNEL_P8_KEYSTROKE = "P8_keystroke"

CHANNEL_ORDER = [
    CHANNEL_P1_NATS, CHANNEL_P2_HTTP, CHANNEL_P3_CHIEF,
    CHANNEL_P4_SEED, CHANNEL_P5_SSH, CHANNEL_P6_WOL,
    CHANNEL_P7_GIT, CHANNEL_P8_KEYSTROKE,
]

# Per-channel timeout (30s max each, but tuned per channel for efficiency)
CHANNEL_TIMEOUT_S = {
    CHANNEL_P0_CLOUD: 30,
    CHANNEL_P1_NATS: 30,
    CHANNEL_P2_HTTP: 30,
    CHANNEL_P3_CHIEF: 30,
    CHANNEL_P4_SEED: 30,
    CHANNEL_P5_SSH: 30,
    CHANNEL_P6_WOL: 30,
    CHANNEL_P7_GIT: 30,
    CHANNEL_P8_KEYSTROKE: 30,
}

# Self-heal loop interval (legacy polling mode)
SELF_HEAL_INTERVAL_S = 60
# Repair cooldown per action per peer
REPAIR_COOLDOWN_S = 300  # 5 min

# Zero-polling mode: replace 60s probe loop with NATS callback + NoRespondersError signal.
# A single 5-minute watchdog-of-last-resort remains as defense-in-depth for split-brain
# scenarios that callbacks cannot observe. Default: ON (canary-verified 2026-04-24).
# Set FLEET_ZERO_POLLING=0 to revert to legacy 60s probe loop.
ZERO_POLLING_ENABLED = os.environ.get("FLEET_ZERO_POLLING", "1") not in ("0", "false", "False", "")
WATCHDOG_OF_LAST_RESORT_S = 300  # 5 min sanity check when zero-polling is active

# ── Inter-node repair (peer-quorum L5 assist) ──
# When a node's self-heal L1-L4 fails for N consecutive cycles for the same
# target, it publishes fleet.peer.struggling so healthier peers can self-elect
# to run SSH-from-third-party repair. Rate-limited 1 assist per (assister,
# target) per 15 min; max 3 concurrent assists fleet-wide.
STRUGGLE_THRESHOLD = 3                  # consecutive self-heal failures before broadcasting struggle
ASSIST_COOLDOWN_S = 900                 # 15 min per (assister, target) -- one assist per window
ASSIST_CLAIM_WAIT_S = 5                 # wait this long for dueling claims before executing
ASSIST_MAX_CONCURRENT = 3               # fleet-wide election cap
ASSIST_HEALTH_SAMPLE = 50               # sliding window size for health score
ASSIST_SCRIPT = "scripts/repair-peer.sh"  # relative to REPO_ROOT

SUBJECT_PEER_STRUGGLING = "fleet.peer.struggling"
SUBJECT_PEER_ASSIST_CLAIM = "fleet.peer.assist.claim"
SUBJECT_PEER_REPAIR_RESULT = "fleet.peer.repair.result"

# B4 — Broadcast fanout: O(1) NATS primary, parallel HTTP fanout fallback.
# fleet.broadcast.<type> — single publish → all peers via `fleet.broadcast.>`.
# Every peer must subscribe to `fleet.broadcast.>` and de-dup via id+timestamp.
SUBJECT_BROADCAST_PREFIX = "fleet.broadcast"
def _sub_broadcast() -> str:
    return "fleet.broadcast.>"

# Bounded LRU of recently-seen broadcast IDs (replay window prevention).
BROADCAST_SEEN_MAX = 2048

# RACE Q4 — Message-size telemetry tunables.
# MSG_SIZE_WINDOW: rolling window of recent message sizes for p95 calc.
#   512 samples ≈ a few minutes of traffic at typical fleet rates; bounded
#   so RSS stays flat regardless of throughput.
# MSG_SIZE_WARN_BYTES: any single message above this threshold logs a
#   WARNING and increments _stats["large_msg_count"]. 100 KB is the
#   bandwidth-planning threshold flagged by RACE Q4.
MSG_SIZE_WINDOW = 512
MSG_SIZE_WARN_BYTES = 100 * 1024
# Per-peer HTTP fanout timeout in the fallback path (B4).
BROADCAST_HTTP_PEER_TIMEOUT_S = 5.0

# Fleet upgrading protocol — docs/fleet/FLEET-UPGRADING.md (cebf24d3)
SUBJECT_CAPABILITY_QUERY = "rpc.capability.query"
SUBJECT_CANARY_APPLIED = "capability.canary.applied"


class PeerRateLimiter:
    """Token bucket rate limiter per peer per message type.

    Each (peer, msg_type) pair gets an independent token bucket with
    configurable rate (tokens per window).  No external dependencies.
    """

    DEFAULT_RATES = {
        "heartbeat": (6, 60),
        "repair": (2, 300),
        "task": (5, 60),
        "context": (20, 60),
        "reply": (20, 60),
        "alert": (10, 60),
        "ack": (30, 60),
        "sync": (10, 60),
        "message": (20, 60),
        # P0 Discord — stricter budget since it hits external API on offline emergencies
        "p0_discord": (2, 60),  # 2 per 60s per target
    }
    FALLBACK_RATE = (10, 60)

    def __init__(self, rates: dict = None):
        self._rates = rates or self.DEFAULT_RATES
        self._buckets: dict = {}
        self._stats: dict = {"allowed": 0, "denied": 0, "denied_by_peer": {}}

    def allow(self, peer_id: str, msg_type: str) -> bool:
        """Return True if this message is within rate limits."""
        max_tokens, window = self._rates.get(msg_type, self.FALLBACK_RATE)
        key = (peer_id, msg_type)
        now = time.time()
        if key not in self._buckets:
            self._buckets[key] = []
        cutoff = now - window
        self._buckets[key] = [t for t in self._buckets[key] if t > cutoff]
        bucket = self._buckets[key]
        if len(bucket) >= max_tokens:
            self._stats["denied"] += 1
            self._stats["denied_by_peer"].setdefault(peer_id, 0)
            self._stats["denied_by_peer"][peer_id] += 1
            return False
        bucket.append(now)
        self._stats["allowed"] += 1
        return True

    def reset(self, peer_id: str):
        """Reset all limits for a peer (e.g., after reconnection)."""
        to_remove = [k for k in self._buckets if k[0] == peer_id]
        for k in to_remove:
            del self._buckets[k]

    def get_state(self) -> dict:
        """Return current rate limit state for /health endpoint."""
        now = time.time()
        peers: dict = {}
        for (peer, msg_type), timestamps in self._buckets.items():
            max_tokens, window = self._rates.get(msg_type, self.FALLBACK_RATE)
            cutoff = now - window
            active = [t for t in timestamps if t > cutoff]
            if not active:
                continue
            if peer not in peers:
                peers[peer] = {}
            peers[peer][msg_type] = {
                "used": len(active),
                "limit": max_tokens,
                "window_s": window,
            }
        return {"stats": self._stats, "peers": peers}


class ChannelState:
    """Tracks per-peer, per-channel health state.

    Also maintains a rolling window of the last ROLLING_WINDOW attempts per
    (peer, channel) for adaptive re-ranking. See get_channel_order().
    """

    # Rolling-window size for success-rate computation. Small window reacts
    # quickly to network changes; large window resists transient blips.
    ROLLING_WINDOW = 20
    # Minimum attempts before a channel is eligible for dynamic re-ranking.
    # Below this, static CHANNEL_ORDER priority is used (cold-start fallback).
    MIN_ATTEMPTS_FOR_RERANK = 5

    def __init__(self):
        # {peer: {channel: {status, last_success, last_failure, latency_ms,
        #                   fail_count, window: deque[bool]}}}
        self._state: dict = {}
        # EEE3 — cascade harmonization. When the daemon wires us, we fire
        # ``fleet.cascade.peer_degraded.<reporter>.<channel>`` on OK→broken
        # transitions so the target peer can self-heal that specific
        # transport without waiting for the 60s heal loop. Both are
        # optional + ZSF: failures bump counters inside channel_priority.
        self._transition_publisher = None  # type: ignore[var-annotated]
        self._reporter_node_id: str = ""

    def set_transition_publisher(self, publisher, reporter_node_id: str) -> None:
        """Register the daemon-side NATS publisher used for cascade events.

        ``publisher`` is a callable ``(subject: str, data: bytes) -> Any`` that
        synchronously enqueues a publish on the daemon's event loop.
        ``reporter_node_id`` is stamped into the subject + payload.

        Idempotent. Passing ``publisher=None`` disables the hook.
        """
        self._transition_publisher = publisher
        self._reporter_node_id = str(reporter_node_id or "")

    def _ensure_peer(self, peer: str, channel: str):
        if peer not in self._state:
            self._state[peer] = {}
        if channel not in self._state[peer]:
            self._state[peer][channel] = {
                "status": "unknown",
                "last_success": 0.0,
                "last_failure": 0.0,
                "latency_ms": 0,
                "fail_count": 0,
                # Rolling window: True=success, False=failure. Capped at ROLLING_WINDOW.
                "window": deque(maxlen=self.ROLLING_WINDOW),
            }
        elif "window" not in self._state[peer][channel]:
            # Back-compat: entry exists from older code path without rolling window.
            self._state[peer][channel]["window"] = deque(maxlen=self.ROLLING_WINDOW)

    def record_success(self, peer: str, channel: str, latency_ms: int):
        """Record a successful channel send (updates rolling window)."""
        self._ensure_peer(peer, channel)
        entry = self._state[peer][channel]
        entry["status"] = "ok"
        entry["last_success"] = time.time()
        entry["latency_ms"] = latency_ms
        entry["fail_count"] = 0
        entry["window"].append(True)
        prev = entry.get("_prev_status")
        transitioned = bool(prev and prev != "ok")
        if transitioned:
            logger.info(f"Channel state change: {peer}/{channel} {prev} -> ok")
        entry["_prev_status"] = "ok"
        # EEE3 — fire cascade.peer_healed on broken→ok transition so the
        # target peer's self-repair handler can clear its degraded hint.
        if transitioned and self._transition_publisher is not None and self._reporter_node_id:
            try:
                from multifleet import channel_priority as _cp
                _cp.mark_healed(
                    peer, channel,
                    reporter=self._reporter_node_id,
                    publisher=self._transition_publisher,
                )
            except Exception as exc:  # noqa: BLE001 — ZSF
                logger.debug(f"cascade.mark_healed failed: {exc}")

    def record_failure(self, peer: str, channel: str, reason: str = "") -> str:
        """Record a failed channel send (updates rolling window).

        Returns the previous status (``ok``/``degraded``/``unknown``/``broken``)
        so callers can detect HEALTHY→BROKEN transitions and trigger
        per-channel repair without waiting for a dual P1+P2 outage.
        """
        self._ensure_peer(peer, channel)
        entry = self._state[peer][channel]
        prev_status = entry["status"]
        entry["status"] = "broken"
        entry["last_failure"] = time.time()
        entry["fail_count"] = entry.get("fail_count", 0) + 1
        entry["window"].append(False)
        transitioned = prev_status != "broken"
        if transitioned:
            logger.info(f"Channel state change: {peer}/{channel} {prev_status} -> broken ({reason})")
        entry["_prev_status"] = "broken"
        # EEE3 — fire cascade.peer_degraded on (ok|unknown|degraded)→broken so
        # the target peer can self-repair THIS channel without waiting for the
        # 60s heal loop. Skip when prev was already "broken" (avoid flooding).
        if transitioned and self._transition_publisher is not None and self._reporter_node_id:
            try:
                from multifleet import channel_priority as _cp
                _cp.mark_broken(
                    peer, channel,
                    reporter=self._reporter_node_id,
                    reason=reason,
                    publisher=self._transition_publisher,
                )
            except Exception as exc:  # noqa: BLE001 — ZSF
                logger.debug(f"cascade.mark_broken failed: {exc}")
        return prev_status

    def success_rate(self, peer: str, channel: str) -> float:
        """Return rolling-window success rate in [0.0, 1.0].

        Returns 0.0 when no samples exist (cold start — caller should use
        static order as fallback).
        """
        if peer not in self._state or channel not in self._state[peer]:
            return 0.0
        window = self._state[peer][channel].get("window")
        if not window:
            return 0.0
        return sum(1 for ok in window if ok) / len(window)

    def attempts(self, peer: str, channel: str) -> int:
        """Return count of attempts currently in the rolling window."""
        if peer not in self._state or channel not in self._state[peer]:
            return 0
        return len(self._state[peer][channel].get("window") or [])

    def get_channel_order(
        self,
        peer: str,
        static_order: list,
        min_attempts: int = MIN_ATTEMPTS_FOR_RERANK,
    ) -> list:
        """Return channels re-ranked by rolling success rate.

        Args:
            peer: target node id
            static_order: baseline priority list (CHANNEL_ORDER) — used both as
                the cold-start fallback and as the stable-sort tiebreaker.
            min_attempts: channels with fewer than N attempts in the rolling
                window keep their static priority rank (cold-start guard).

        Ranking key (highest-preference first):
            1. success_rate DESC              (primary)
            2. last_success timestamp DESC    (recency; breaks ties between
                                               channels with equal rates)
            3. static priority index ASC      (final tiebreaker — P1 beats P2
                                               when both are tied)

        Channels below the min_attempts threshold are sorted purely by static
        priority, preserving the documented P1→P8 fallback during warm-up.
        """
        if not static_order:
            return []

        warm: list = []
        cold: list = []
        for idx, name in enumerate(static_order):
            attempts = self.attempts(peer, name)
            if attempts >= min_attempts:
                warm.append((name, idx))
            else:
                cold.append((name, idx))

        def _last_success(ch: str) -> float:
            return (
                self._state.get(peer, {})
                .get(ch, {})
                .get("last_success", 0.0)
            )

        # Warm channels: rank by success_rate DESC, last_success DESC, static idx ASC
        warm.sort(
            key=lambda pair: (
                -self.success_rate(peer, pair[0]),
                -_last_success(pair[0]),
                pair[1],
            )
        )
        # Cold channels: preserve static priority exactly (idx ASC)
        cold.sort(key=lambda pair: pair[1])

        # Merge: warm channels first (they have evidence), cold channels after.
        # A warm P2 with 0.95 success outranks a cold P1 (evidence > priority).
        # A completely-cold peer → warm=[] → returns static_order unchanged.
        return [name for name, _ in warm] + [name for name, _ in cold]

    def success_rate_report(self, peer: Optional[str] = None) -> dict:
        """Return per-peer per-channel success-rate report for /stats endpoint.

        Shape: {peer: {channel: {"success_rate": float, "attempts": int,
                                 "window_size": int, "last_success": float}}}
        """
        out: dict = {}
        peers = [peer] if peer else list(self._state.keys())
        for p in peers:
            channels = self._state.get(p, {})
            out[p] = {}
            for ch, entry in channels.items():
                if ch.startswith("_"):
                    continue
                window = entry.get("window") or deque()
                total = len(window)
                rate = (sum(1 for ok in window if ok) / total) if total else 0.0
                out[p][ch] = {
                    "success_rate": round(rate, 3),
                    "attempts": total,
                    "window_size": self.ROLLING_WINDOW,
                    "last_success": entry.get("last_success", 0.0),
                }
        return out

    def is_known_broken(self, peer: str, channel: str, max_age_s: float = 120.0) -> bool:
        """Check if a channel is known broken (failed recently)."""
        if peer not in self._state or channel not in self._state[peer]:
            return False
        entry = self._state[peer][channel]
        if entry["status"] != "broken":
            return False
        # Stale broken state (>max_age_s) -- retry it
        if time.time() - entry["last_failure"] > max_age_s:
            return False
        return True

    def get_broken_channels(self, peer: str) -> list:
        """Return list of broken channel names for a peer."""
        if peer not in self._state:
            return []
        return [ch for ch, v in self._state[peer].items() if v["status"] == "broken"]

    def get_all(self) -> dict:
        """Return full state dict (safe for JSON serialization)."""
        result = {}
        for peer, channels in self._state.items():
            result[peer] = {}
            for ch, v in channels.items():
                # Omit internal keys
                result[peer][ch] = {k: v2 for k, v2 in v.items() if not k.startswith("_")}
        return result

    def get_peer_summary(self, peer: str) -> dict:
        """Summarized channel health for one peer."""
        if peer not in self._state:
            return {}
        return {ch: {"status": v["status"], "latency_ms": v["latency_ms"]}
                for ch, v in self._state[peer].items() if not ch.startswith("_")}

# ── Subjects ──
def _sub_direct(node: str) -> str:
    return f"fleet.{node}.>"

def _sub_all() -> str:
    return "fleet.all.>"

def _sub_heartbeat() -> str:
    return "fleet.heartbeat"

def _sub_ack(node: str) -> str:
    return f"fleet.{node}.ack"


# Module-level cached FleetSecurity for daemon-side publishers (mf-1 HMAC parity,
# 5-agent priority audit 2026-04-26). Lazy-init on first call; None if security
# module fails to import. Caller does best-effort: signature degrades gracefully
# (subscribers accept unsigned per security.py:242-253 graceful-degrade).
_SIGN_SECURITY = None
_SIGN_SECURITY_TRIED = False


def _sign_envelope(payload_dict: dict, node_id: str) -> bytes:
    """Build + best-effort-sign envelope; return JSON bytes ready to publish.

    Adds `from=node_id` then attempts HMAC sign via FleetSecurity. Never raises
    — sign failures fall through to unsigned bytes. Subscribers' verify_message
    accepts unsigned in graceful-degrade mode (security.py:242-253).
    """
    global _SIGN_SECURITY, _SIGN_SECURITY_TRIED
    envelope = dict(payload_dict)
    envelope.setdefault("from", node_id)
    if not _SIGN_SECURITY_TRIED:
        _SIGN_SECURITY_TRIED = True
        try:
            from multifleet.security import FleetSecurity
            _SIGN_SECURITY = FleetSecurity()
        except Exception as e:
            logger.debug("FleetSecurity import for daemon publishers failed: %s", e)
            _SIGN_SECURITY = None
    if _SIGN_SECURITY is not None:
        try:
            envelope = _SIGN_SECURITY.sign_message(envelope)
        except Exception as e:
            logger.debug("envelope sign failed: %s", e)
    return json.dumps(envelope).encode()


# ── ContextDNA Claude Bridge — Anthropic upstream + DeepSeek fallback ──────
# Module-scoped helpers used by /v1/messages handler. Lazy-init pattern: the
# Anthropic key is resolved once per process from env → Keychain → None.
# When None, bridge skips Anthropic try entirely and goes straight to DeepSeek
# (matches Aaron's "never crash" goal — best-effort upstream, DeepSeek is the
# safety net).
_ANTHROPIC_KEY: Optional[str] = None
_ANTHROPIC_KEY_TRIED: bool = False
_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"  # Stable Anthropic Messages API version


# ── Anthropic rate-limit telemetry (Cycle 4 / D2) ─────────────────────────
# Anthropic responses include `anthropic-ratelimit-*` headers describing
# remaining quota + reset epochs. We capture them here so /metrics + /health
# expose Aaron's quota state at a glance and the xbar plugin can flip badge
# color when capacity is low. -1 sentinel = "never observed" (0 means
# semantically "exhausted" — preserving the distinction matters for alarms).
import threading as _ratelimit_threading  # local alias to avoid shadowing

_BRIDGE_RATELIMIT_KEYS = (
    "bridge_ratelimit_requests_remaining",
    "bridge_ratelimit_tokens_remaining",
    "bridge_ratelimit_input_tokens_remaining",
    "bridge_ratelimit_output_tokens_remaining",
    "bridge_ratelimit_requests_reset_epoch",
    "bridge_ratelimit_tokens_reset_epoch",
    "bridge_ratelimit_last_429_epoch",
    "bridge_ratelimit_last_retry_after_seconds",
)
_BRIDGE_RATELIMIT_STATE: dict = {k: -1 for k in _BRIDGE_RATELIMIT_KEYS}
_BRIDGE_RATELIMIT_LOCK = _ratelimit_threading.Lock()
# Observability counter: how many times header parsing failed (ZSF — never
# silent). Surfaced via /metrics when whitelisted in build_metrics.
_BRIDGE_RATELIMIT_PARSE_ERRORS: int = 0


def _coerce_header_value(v) -> Optional[str]:
    """httpx returns str; raw http.client / urllib3 may hand back bytes.
    Return a stripped str or None. ZSF: never raises, returns None on weird
    types so the caller can decide (not silently treat as 'present')."""
    if v is None:
        return None
    if isinstance(v, bytes):
        try:
            return v.decode("ascii", "replace").strip()
        except Exception:
            return None
    if isinstance(v, str):
        return v.strip()
    try:
        return str(v).strip()
    except Exception:
        return None


def _parse_iso8601_to_epoch(s: str) -> int:
    """Anthropic returns reset times as ISO-8601 ('2026-05-04T12:34:56Z').
    Return int epoch seconds, or -1 on parse failure. Tolerant of both
    'Z' suffix and '+00:00' offset."""
    if not s:
        return -1
    try:
        # Python <3.11 doesn't accept 'Z' in fromisoformat — normalize.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        from datetime import datetime
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return -1


def _record_anthropic_ratelimit_headers(headers, status_code: int) -> None:
    """Parse Anthropic ratelimit response headers into module-level gauges.

    headers: any object exposing .get(name) → str|bytes|None (httpx.Headers,
    requests CaseInsensitiveDict, raw urllib3 HTTPHeaderDict all qualify).

    ZSF: errors increment _BRIDGE_RATELIMIT_PARSE_ERRORS. Never raises into
    the caller's hot path — best-effort observability layer.
    """
    global _BRIDGE_RATELIMIT_PARSE_ERRORS
    if headers is None:
        return
    try:
        getter = headers.get  # most header containers
    except AttributeError:
        _BRIDGE_RATELIMIT_PARSE_ERRORS += 1
        return

    def _bump_parse_err():
        global _BRIDGE_RATELIMIT_PARSE_ERRORS
        _BRIDGE_RATELIMIT_PARSE_ERRORS += 1

    def _int_header(name: str) -> int:
        try:
            v = _coerce_header_value(getter(name))
            if v is None or v == "":
                return -1
            return int(float(v))  # float() tolerates "1234.0"
        except Exception:
            _bump_parse_err()
            return -1

    try:
        req_lim = _int_header("anthropic-ratelimit-requests-remaining")
        tok_lim = _int_header("anthropic-ratelimit-tokens-remaining")
        in_tok = _int_header("anthropic-ratelimit-input-tokens-remaining")
        out_tok = _int_header("anthropic-ratelimit-output-tokens-remaining")
        req_reset = _parse_iso8601_to_epoch(
            _coerce_header_value(getter("anthropic-ratelimit-requests-reset")) or ""
        )
        tok_reset = _parse_iso8601_to_epoch(
            _coerce_header_value(getter("anthropic-ratelimit-tokens-reset")) or ""
        )
        last_429 = -1
        retry_after = -1
        if int(status_code or 0) == 429:
            last_429 = int(time.time())
            ra_raw = _coerce_header_value(getter("retry-after"))
            if ra_raw:
                try:
                    retry_after = int(float(ra_raw))
                except Exception:
                    _bump_parse_err()
                    retry_after = -1
        with _BRIDGE_RATELIMIT_LOCK:
            # Only overwrite when header was present (>= 0). Preserves the
            # last-known value so the badge keeps showing meaningful state
            # across calls where Anthropic omits a particular header.
            if req_lim >= 0:
                _BRIDGE_RATELIMIT_STATE["bridge_ratelimit_requests_remaining"] = req_lim
            if tok_lim >= 0:
                _BRIDGE_RATELIMIT_STATE["bridge_ratelimit_tokens_remaining"] = tok_lim
            if in_tok >= 0:
                _BRIDGE_RATELIMIT_STATE["bridge_ratelimit_input_tokens_remaining"] = in_tok
            if out_tok >= 0:
                _BRIDGE_RATELIMIT_STATE["bridge_ratelimit_output_tokens_remaining"] = out_tok
            if req_reset >= 0:
                _BRIDGE_RATELIMIT_STATE["bridge_ratelimit_requests_reset_epoch"] = req_reset
            if tok_reset >= 0:
                _BRIDGE_RATELIMIT_STATE["bridge_ratelimit_tokens_reset_epoch"] = tok_reset
            if last_429 >= 0:
                _BRIDGE_RATELIMIT_STATE["bridge_ratelimit_last_429_epoch"] = last_429
            if retry_after >= 0:
                _BRIDGE_RATELIMIT_STATE["bridge_ratelimit_last_retry_after_seconds"] = retry_after
    except Exception:
        _BRIDGE_RATELIMIT_PARSE_ERRORS += 1


def _bridge_ratelimit_snapshot() -> dict:
    """Thread-safe shallow copy of the current ratelimit state, plus the
    parse-error counter. Suitable for splatting into /health.stats and
    /metrics output."""
    with _BRIDGE_RATELIMIT_LOCK:
        snap = dict(_BRIDGE_RATELIMIT_STATE)
    snap["bridge_ratelimit_parse_errors"] = int(_BRIDGE_RATELIMIT_PARSE_ERRORS)
    return snap


def _resolve_anthropic_key() -> Optional[str]:
    """Resolve Anthropic API key once per process. env > Keychain > None."""
    global _ANTHROPIC_KEY, _ANTHROPIC_KEY_TRIED
    if _ANTHROPIC_KEY_TRIED:
        return _ANTHROPIC_KEY
    _ANTHROPIC_KEY_TRIED = True
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key and env_key.startswith("sk-ant-"):
        _ANTHROPIC_KEY = env_key
        logger.info("Bridge: Anthropic key loaded from env (len=%d)", len(env_key))
        return _ANTHROPIC_KEY
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", "fleet-nerve", "-a", "Context_DNA_Anthropic", "-w"],
            capture_output=True, text=True, timeout=3.0,
        )
        kc = (out.stdout or "").strip()
        if kc and kc.startswith("sk-ant-"):
            _ANTHROPIC_KEY = kc
            logger.info("Bridge: Anthropic key loaded from Keychain (len=%d)", len(kc))
            return _ANTHROPIC_KEY
    except Exception as e:
        logger.debug("Bridge: Keychain Anthropic lookup failed: %s", e)
    logger.info("Bridge: no Anthropic key found — bridge will go DeepSeek-only")
    return None


# OAuth Bearer pass-through (Pro/Max users like Aaron). When opt-in via
# BRIDGE_OAUTH_PASSTHROUGH=1, an incoming `Authorization: Bearer sk-ant-oat-…`
# header is forwarded as-is to api.anthropic.com (Bearer auth, NOT x-api-key).
# 200 → pass through (Aaron's Max quota, full quality). 429/5xx/overloaded
# → fall to DeepSeek (existing quota-survival path). Default OFF until Aaron
# explicitly enables it in launchd plist.
_OAT_PREFIX = "sk-ant-oat-"


def _extract_oauth_bearer(handler) -> Optional[str]:
    """Extract a Bearer OAuth token from the incoming request, gated by
    BRIDGE_OAUTH_PASSTHROUGH=1. Strict prefix-match on ``sk-ant-oat-`` to
    avoid forwarding arbitrary bearer tokens. Returns the token string when
    valid + opt-in active, else None. Never raises — observability via
    bridge_oauth_passthrough_attempted in the caller."""
    try:
        if os.environ.get("BRIDGE_OAUTH_PASSTHROUGH", "") != "1":
            return None
        auth_header = handler.headers.get("Authorization", "") or ""
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[len("Bearer "):].strip()
        if not token.startswith(_OAT_PREFIX):
            # Specific prefix gate — don't forward arbitrary bearer tokens.
            logger.info(
                "Bridge OAuth: Bearer present but not sk-ant-oat-* prefix; skipping"
            )
            return None
        return token
    except Exception as e:
        logger.debug("Bridge OAuth: bearer extraction failed: %s", e)
        return None


def _try_anthropic_upstream(
    body: dict,
    api_key: Optional[str] = None,
    timeout_s: float = 90.0,
    bearer_token: Optional[str] = None,
) -> tuple:
    """POST to Anthropic Messages API. Returns (status, response_dict_or_None, error_str).

    Auth: pass either ``api_key`` (sent as ``x-api-key``) OR ``bearer_token``
    (sent as ``Authorization: Bearer <token>``). Bearer is preferred when
    both are present (OAuth pass-through path for Pro/Max users on Aaron's
    plan). Caller is responsible for the BRIDGE_OAUTH_PASSTHROUGH gate.

    Strips stream=true (MVP non-streaming only — passes through Anthropic
    response unchanged when it works, falls to DeepSeek otherwise).
    """
    try:
        import httpx  # type: ignore
    except ImportError:
        return (0, None, "httpx not installed")
    if not api_key and not bearer_token:
        return (0, None, "no anthropic credential provided")
    upstream_body = dict(body)
    upstream_body.pop("stream", None)  # Force non-stream for MVP
    headers = {
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    else:
        headers["x-api-key"] = api_key  # type: ignore[assignment]
    try:
        with httpx.Client(timeout=timeout_s) as client:
            r = client.post(_ANTHROPIC_API_URL, json=upstream_body, headers=headers)
            # Capture ratelimit headers BEFORE parsing body so we never lose
            # quota telemetry on JSON parse failure (Cycle 4 / D2).
            try:
                _record_anthropic_ratelimit_headers(r.headers, r.status_code)
            except Exception:
                pass  # ZSF — counter inside recorder; never break upstream call
            try:
                data = r.json()
            except Exception:
                data = None
            return (r.status_code, data, None)
    except Exception as e:
        return (0, None, f"anthropic upstream error: {type(e).__name__}: {str(e)[:100]}")


def _stream_anthropic_passthrough(
    handler,
    body: dict,
    api_key: Optional[str] = None,
    bearer_token: Optional[str] = None,
) -> tuple:
    """Pure pass-through: open Anthropic SSE stream, pipe bytes to handler.wfile.
    Returns (committed: bool, status: int).

    Auth: pass either ``api_key`` or ``bearer_token`` (Bearer preferred when
    both present). See ``_try_anthropic_upstream`` for the OAuth pass-through
    rationale.
    """
    try:
        import httpx  # type: ignore
    except ImportError:
        return (False, 0)
    if not api_key and not bearer_token:
        return (False, 0)
    upstream_body = dict(body)
    upstream_body["stream"] = True
    headers = {
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    else:
        headers["x-api-key"] = api_key  # type: ignore[assignment]
    client = None
    r = None
    try:
        client = httpx.Client(timeout=300.0)
        req = client.build_request("POST", _ANTHROPIC_API_URL, json=upstream_body, headers=headers)
        r = client.send(req, stream=True)
        # Ratelimit headers arrive with the response head, before the SSE
        # body streams. Capture immediately (Cycle 4 / D2) so quota state
        # updates even when the stream itself runs long.
        try:
            _record_anthropic_ratelimit_headers(r.headers, r.status_code)
        except Exception:
            pass  # ZSF — recorder is self-observable; never break the stream
        if r.status_code != 200:
            try:
                r.read()
            except Exception:
                pass
            return (False, r.status_code)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache, no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        for chunk in r.iter_bytes():
            if not chunk:
                continue
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
        return (True, 200)
    except Exception as e:
        logger.debug("anthropic stream error: %s", e)
        return (False, 0)
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass
        try:
            if client is not None:
                client.close()
        except Exception:
            pass


def _emit_anthropic_sse_synthetic(handler, full_text: str, model: str, in_tokens: int) -> None:
    """Synthesize Anthropic SSE event sequence around full-text DeepSeek result."""
    msg_id = f"msg_bridge_{int(time.time()*1000)}"
    out_tokens = max(1, len(full_text) // 4)
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache, no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()

    def _emit(event_type, data):
        line = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        try:
            handler.wfile.write(line.encode())
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    _emit("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": in_tokens, "output_tokens": 0},
        },
    })
    _emit("content_block_start", {"type": "content_block_start", "index": 0,
                                   "content_block": {"type": "text", "text": ""}})
    _emit("content_block_delta", {"type": "content_block_delta", "index": 0,
                                   "delta": {"type": "text_delta", "text": full_text}})
    _emit("content_block_stop", {"type": "content_block_stop", "index": 0})
    _emit("message_delta", {"type": "message_delta",
                             "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                             "usage": {"output_tokens": out_tokens}})
    _emit("message_stop", {"type": "message_stop"})


# ── WaveC — 3rd-tier Superset fallback (BBB3/CCC3 contract) ─────────────
# Aaron's rate-limit-resilience vision: when DeepSeek + Anthropic both
# fail on the bridge, route the prompt to Superset (cloud-hosted MCP at
# api.superset.sh) instead of returning 503. Single function, monkey-
# patchable in tests. ZSF: every outcome bumps an observable counter.
def _try_superset_fallback(prompt: str, stats: dict) -> Optional[str]:
    """Last-resort upstream — POST prompt to Superset agent MCP.

    Returns the assistant text on success, ``None`` on any failure. Never
    raises — caller decides whether to surface 503. ``stats`` is the
    daemon's ``self._stats`` dict; we bump:
      * ``bridge_superset_fallback_ok`` on success
      * ``bridge_superset_fallback_errors`` on unavailable / failure

    Implementation: use ``multifleet.superset_bridge.SupersetBridge`` and
    its ``launch_agent_with_prompt`` against the active workspace from
    ``get_app_context()``. Falls through to None when no API key, no
    active workspace, or any transport error.
    """
    try:
        from multifleet.superset_bridge import SupersetBridge  # local lazy import
        bridge = SupersetBridge()
        ctx = bridge.get_app_context() or {}
        ws_id = (
            ctx.get("activeWorkspaceId")
            or ctx.get("workspace_id")
            or ctx.get("workspaceId")
        )
        if not ws_id:
            stats["bridge_superset_fallback_errors"] = (
                stats.get("bridge_superset_fallback_errors", 0) + 1
            )
            return None
        result = bridge.launch_agent_with_prompt(
            workspace_id=ws_id, prompt=prompt, agent="claude",
        )
        text = None
        if isinstance(result, dict):
            text = (
                result.get("text")
                or result.get("content")
                or result.get("response")
                or result.get("output")
            )
        elif isinstance(result, str):
            text = result
        if text:
            stats["bridge_superset_fallback_ok"] = (
                stats.get("bridge_superset_fallback_ok", 0) + 1
            )
            return text
        stats["bridge_superset_fallback_errors"] = (
            stats.get("bridge_superset_fallback_errors", 0) + 1
        )
        return None
    except Exception as e:  # noqa: BLE001 — ZSF, observable via counter
        stats["bridge_superset_fallback_errors"] = (
            stats.get("bridge_superset_fallback_errors", 0) + 1
        )
        logger.warning("Superset fallback failed: %s: %s", type(e).__name__, e)
        return None


# WaveL: cross-node Superset peer failover. When local Superset also fails,
# round-robin push to peer nodes (mac1 -> mac3 -> mac2 minus self). ZSF.
_PEER_ROUND_ROBIN = ["mac1", "mac3", "mac2"]
_PEER_RR_INDEX = {"i": 0}


def _try_peer_superset_failover(prompt: str, stats: dict) -> Optional[str]:
    """Round-robin peer push via vscode_superset_bridge.push_prompt(peer=).

    Skips current node. Returns text on success, else None. Open question
    (3s cross-exam): round-robin vs channel_scoring reliability-weighted.
    """
    try:
        from multifleet.vscode_superset_bridge import push_prompt as _peer_push
    except Exception:
        stats["bridge_superset_peer_failover_errors"] = (
            stats.get("bridge_superset_peer_failover_errors", 0) + 1
        )
        return None
    self_node = os.environ.get("MULTIFLEET_NODE_ID", "")
    peers = [p for p in _PEER_ROUND_ROBIN if p != self_node]
    if not peers:
        return None
    start = _PEER_RR_INDEX["i"] % len(peers)
    for offset in range(len(peers)):
        peer = peers[(start + offset) % len(peers)]
        _PEER_RR_INDEX["i"] = (start + offset + 1) % len(peers)
        stats["bridge_superset_peer_failover_attempts"] = (
            stats.get("bridge_superset_peer_failover_attempts", 0) + 1
        )
        try:
            res = _peer_push(prompt, peer=peer)
            if isinstance(res, dict) and res.get("ok"):
                stats["bridge_superset_peer_failover_ok"] = (
                    stats.get("bridge_superset_peer_failover_ok", 0) + 1
                )
                inner = res.get("result")
                text = None
                if isinstance(inner, dict):
                    text = (inner.get("text") or inner.get("content")
                            or inner.get("response") or inner.get("output"))
                elif isinstance(inner, str):
                    text = inner
                return text or f"[peer-handoff:{peer}]"
        except Exception as e:  # noqa: BLE001 — ZSF
            stats["bridge_superset_peer_failover_errors"] = (
                stats.get("bridge_superset_peer_failover_errors", 0) + 1
            )
            logger.warning("peer failover to %s failed: %s: %s",
                           peer, type(e).__name__, e)
    return None


# G1: Anthropic tool_use <-> OpenAI function-call translation.
# When the bridge falls back from Anthropic Messages API to DeepSeek's
# OpenAI-compatible /chat/completions, Claude Code's tool-using sessions
# would otherwise break. These helpers translate request and response.
#
# Schema map (Anthropic -> OpenAI request):
#   tools[].input_schema     -> tools[].function.parameters
#   tools[].name/description -> tools[].function.{name,description}
#   tool_choice "auto"/"any"/"none" -> "auto"/"required"/"none"
#   tool_choice {type:tool, name:X} -> {type:function, function:{name:X}}
# Schema map (OpenAI -> Anthropic response):
#   choices[0].message.tool_calls[i] -> content[].{type:tool_use, id, name, input}
#
# Schema-fidelity gaps (ship partial + counter):
#   - DeepSeek deepseek-reasoner does not support tools; bridge auto-routes
#     to deepseek-chat when tools present.
#   - Anthropic disable_parallel_tool_use is silently dropped (no equivalent).
#   - cache_control on tool defs is dropped (no DeepSeek prompt-cache surface).
_DEEPSEEK_API_URL_TOOLS = "https://api.deepseek.com/v1/chat/completions"


def _translate_anthropic_tools_to_openai(tools):
    """Anthropic tools[] -> OpenAI tools[]. Returns None on empty/invalid."""
    if not tools or not isinstance(tools, list):
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if not name:
            continue
        params = t.get("input_schema") or {"type": "object", "properties": {}}
        fn = {"name": name, "parameters": params}
        desc = t.get("description")
        if desc:
            fn["description"] = desc
        out.append({"type": "function", "function": fn})
    return out or None


def _translate_anthropic_tool_choice_to_openai(tc):
    """Anthropic tool_choice -> OpenAI tool_choice.

    Strings: auto/any/none -> auto/required/none.
    Object {type:tool, name:X} -> {type:function, function:{name:X}}.
    Object {type:auto|any|none} -> string forms. Unknown -> auto.
    """
    if tc is None:
        return None
    if isinstance(tc, str):
        if tc == "any":
            return "required"
        if tc in ("auto", "none", "required"):
            return tc
        return "auto"
    if isinstance(tc, dict):
        ttype = tc.get("type")
        if ttype == "tool":
            name = tc.get("name")
            if name:
                return {"type": "function", "function": {"name": name}}
            return "auto"
        if ttype == "any":
            return "required"
        if ttype in ("auto", "none"):
            return ttype
        if ttype == "function":
            return tc
    return "auto"


def _translate_openai_tool_calls_to_anthropic(tool_calls):
    """OpenAI tool_calls[] -> Anthropic tool_use blocks.

    Preserves ids, parses JSON arguments, never drops a malformed call
    (surfaces raw arguments under input._raw_arguments).
    """
    out = []
    if not tool_calls or not isinstance(tool_calls, list):
        return out
    for i, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        tool_id = tc.get("id") or f"toolu_bridge_{int(time.time()*1000)}_{i}"
        raw_args = fn.get("arguments", "")
        try:
            if isinstance(raw_args, str) and raw_args:
                parsed = json.loads(raw_args)
            elif isinstance(raw_args, dict):
                parsed = raw_args
            else:
                parsed = {}
        except Exception:
            parsed = {"_raw_arguments": str(raw_args)[:2000]}
        out.append({"type": "tool_use", "id": tool_id, "name": name, "input": parsed})
    return out


def _flatten_anthropic_messages_to_openai(messages, system_text):
    """Anthropic messages[] -> OpenAI messages[] for tool-aware DeepSeek call.

    Preserves tool_use / tool_result round-trips. tool_use -> assistant
    tool_calls; tool_result -> role=tool message tied by tool_use_id.
    """
    out = []
    if system_text:
        out.append({"role": "system", "content": system_text})
    if not isinstance(messages, list):
        return out
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            out.append({"role": role, "content": str(content)})
            continue
        text_parts = []
        tool_calls = []
        tool_results = []
        for blk in content:
            if not isinstance(blk, dict):
                continue
            bt = blk.get("type")
            if bt == "text":
                text_parts.append(blk.get("text", ""))
            elif bt == "tool_use":
                try:
                    args_json = json.dumps(blk.get("input", {}))
                except Exception:
                    args_json = "{}"
                tool_calls.append({
                    "id": blk.get("id") or f"call_bridge_{int(time.time()*1000)}",
                    "type": "function",
                    "function": {
                        "name": blk.get("name", "unknown_tool"),
                        "arguments": args_json,
                    },
                })
            elif bt == "tool_result":
                tc = blk.get("content", "")
                if isinstance(tc, list):
                    tc = "".join(
                        b.get("text", "") for b in tc
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": blk.get("tool_use_id") or "unknown",
                    "content": str(tc),
                })
            elif bt == "image":
                text_parts.append("[image attachment]")
        if role == "assistant":
            asst_msg = {"role": "assistant"}
            asst_msg["content"] = "\n".join(p for p in text_parts if p) or None
            if tool_calls:
                asst_msg["tool_calls"] = tool_calls
            if asst_msg["content"] or tool_calls:
                out.append(asst_msg)
        else:
            joined = "\n".join(p for p in text_parts if p)
            if joined:
                out.append({"role": role, "content": joined})
        for tr in tool_results:
            out.append(tr)
    return out


def _call_deepseek_with_tools(*, messages, tools, tool_choice,
                               model="deepseek-chat", max_tokens=4096,
                               temperature=0.7, timeout_s=180.0):
    """POST direct to DeepSeek /chat/completions with tools.

    Returns (status:int, body:dict|None, err:str|None). Loads the DeepSeek
    api key via the same chain as llm_priority_queue. Caller forces
    deepseek-chat when tools present (deepseek-reasoner has no tool support).
    """
    try:
        import httpx  # type: ignore
    except ImportError:
        return (0, None, "httpx not installed")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from memory.llm_priority_queue import _load_deepseek_api_key  # type: ignore
    except Exception as e:
        return (0, None, f"deepseek key import failed: {e!r}")
    api_key = _load_deepseek_api_key()
    if not api_key:
        return (0, None, "no deepseek api key")
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=timeout_s) as client:
            r = client.post(_DEEPSEEK_API_URL_TOOLS, json=payload, headers=headers)
            try:
                data = r.json()
            except Exception:
                data = None
            return (r.status_code, data, None)
    except Exception as e:
        return (0, None, f"deepseek tools call error: {type(e).__name__}: {str(e)[:100]}")


def _emit_anthropic_sse_synthetic_with_tools(handler, text, tool_blocks, model, in_tokens):
    """SSE variant including tool_use blocks.

    Emits one text content block (when text non-empty) followed by one
    content_block per tool_use, each with its own input_json_delta event.
    """
    msg_id = f"msg_bridge_{int(time.time()*1000)}"
    stop_reason = "tool_use" if tool_blocks else "end_turn"
    out_tokens = max(
        1,
        (len(text) + sum(len(json.dumps(b.get("input", {}))) for b in tool_blocks)) // 4,
    )
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache, no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()

    def _emit(event_type, data):
        line = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        try:
            handler.wfile.write(line.encode())
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    _emit("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": in_tokens, "output_tokens": 0},
        },
    })
    idx = 0
    if text:
        _emit("content_block_start", {"type": "content_block_start", "index": idx,
                                       "content_block": {"type": "text", "text": ""}})
        _emit("content_block_delta", {"type": "content_block_delta", "index": idx,
                                       "delta": {"type": "text_delta", "text": text}})
        _emit("content_block_stop", {"type": "content_block_stop", "index": idx})
        idx += 1
    for tb in tool_blocks:
        _emit("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": {
                "type": "tool_use",
                "id": tb.get("id"),
                "name": tb.get("name"),
                "input": {},
            },
        })
        try:
            partial_json = json.dumps(tb.get("input", {}))
        except Exception:
            partial_json = "{}"
        _emit("content_block_delta", {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        })
        _emit("content_block_stop", {"type": "content_block_stop", "index": idx})
        idx += 1
    _emit("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": out_tokens},
    })
    _emit("message_stop", {"type": "message_stop"})


# ── P2-B1+B5 convergent: live Vital Signs Bar (sticky strip) ──────
# Injected into /dashboard HTML response. 5 tiles polling /metrics +
# /health every 5s via vanilla JS (CLIENT polling — preserves daemon
# no-internal-polling invariant). Total payload <2.5KB inline so
# /dashboard stays well under the 8KB constraint imposed in brief.
_VITAL_SIGNS_BAR_FRAGMENT = """
<div class="vital-signs-bar" id="vital-signs-bar" role="status" aria-live="polite">
  <div class="vsb-tile" data-tile="peers"><span class="vsb-label">PEERS</span><span class="vsb-value" data-k="peers">--</span></div>
  <div class="vsb-tile" data-tile="dissents"><span class="vsb-label">DISSENT 24h</span><span class="vsb-value" data-k="dissents">--</span></div>
  <div class="vsb-tile" data-tile="gates"><span class="vsb-label">GATES 24h</span><span class="vsb-value" data-k="gates">--</span></div>
  <div class="vsb-tile" data-tile="bridge"><span class="vsb-label">BRIDGE</span><span class="vsb-value" data-k="bridge">--</span></div>
  <div class="vsb-tile" data-tile="stress"><span class="vsb-label">STRESS</span><span class="vsb-value" data-k="stress">--</span></div>
  <div class="vsb-spark" data-k="ts">init...</div>
</div>
<style>
.vital-signs-bar{position:sticky;top:0;z-index:9999;display:flex;gap:.4rem;
 padding:.4rem .75rem;background:#020617;border-bottom:1px solid #1e293b;
 font:600 11px/1 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;
 color:#cbd5e1;letter-spacing:.04em;align-items:center}
.vsb-tile{display:flex;flex-direction:column;gap:2px;padding:.3rem .55rem;
 background:#0f172a;border:1px solid #1e293b;border-radius:4px;min-width:88px}
.vsb-label{color:#64748b;font-size:9px;font-weight:700}
.vsb-value{color:#22d3ee;font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}
.vsb-tile.warn .vsb-value{color:#f59e0b}.vsb-tile.bad .vsb-value{color:#ef4444}
.vsb-tile.ok .vsb-value{color:#10b981}
.vsb-spark{margin-left:auto;color:#475569;font-size:10px}
</style>
<script>
(function(){
 var $=function(s,c){return (c||document).querySelector(s);};
 function setT(k,v,cls){var t=$('[data-tile="'+k+'"]');if(!t)return;
  $('[data-k="'+k+'"]',t).textContent=v;
  t.classList.remove('ok','warn','bad');if(cls)t.classList.add(cls);}
 function parseProm(text){var m={};text.split('\\n').forEach(function(l){
  if(!l||l[0]=='#')return;var sp=l.lastIndexOf(' ');if(sp<0)return;
  var name=l.slice(0,sp).replace(/\\{[^}]*\\}/,'');var v=parseFloat(l.slice(sp+1));
  if(!isNaN(v))m[name]=v;});return m;}
 function fmt(n){if(n>=1000)return(n/1000).toFixed(1)+'k';return String(n);}
 function refresh(){
  Promise.all([
   fetch('/health').then(function(r){return r.json();}).catch(function(){return null;}),
   fetch('/metrics').then(function(r){return r.text();}).catch(function(){return '';})
  ]).then(function(res){
   var h=res[0]||{},mtxt=res[1]||'',m=parseProm(mtxt);
   /* peers */
   var peers=h.peers||{},pks=Object.keys(peers);
   var ok=0,stale=0,off=0;
   pks.forEach(function(k){var s=(peers[k]||{}).status||(peers[k]||{}).state||'';
    if(/online|ok|healthy/i.test(s))ok++;else if(/stale|degr/i.test(s))stale++;else off++;});
   var pcount=pks.length||(h.cluster_state&&h.cluster_state.observed_peer_count)||0;
   setT('peers',pcount+' ('+ok+'/'+stale+'/'+off+')',
    pcount===0?'warn':(off>0?'bad':'ok'));
   /* dissent: rebuttals.with_dissent if exposed */
   var dis=(h.rebuttals&&h.rebuttals.with_dissent);
   setT('dissents',(dis==null?'N/A':String(dis)),(dis>0?'warn':null));
   /* gates blocked counter */
   var gb=m['fleet_nerve_gates_blocked']||(h.stats&&h.stats.gates_blocked)||0;
   setT('gates',fmt(gb),(gb>5?'warn':null));
   /* bridge mode + ratio */
   var aok=m['fleet_nerve_bridge_anthropic_ok']||0;
   var dok=(m['fleet_nerve_bridge_fallback_to_deepseek']||0)+
           (m['fleet_nerve_bridge_anthropic_skipped']||0)+
           (m['fleet_nerve_bridge_stream_synthetic_deepseek']||0);
   var tot=aok+dok,mode=(aok>=dok?'A':'D'),pct=tot?Math.round(100*aok/tot):0;
   setT('bridge',mode+' '+pct+'%',(tot===0?null:(aok===0?'warn':'ok')));
   /* stress: prefer fleet_stress counter, else uptime */
   var st=m['fleet_nerve_fleet_stress'];
   if(st!=null){setT('stress',st.toFixed(2),(st>0.7?'bad':(st>0.4?'warn':'ok')));}
   else{var up=h.uptime_s||0;
    setT('stress',(up<60?up+'s':(up<3600?Math.round(up/60)+'m':Math.round(up/3600)+'h')),'ok');}
   var ts=$('.vsb-spark');if(ts)ts.textContent='live '+new Date().toLocaleTimeString();
  });
 }
 refresh();setInterval(refresh,5000);
})();
</script>
"""


def _inject_vital_signs_bar(html: str) -> str:
    """Insert the live Vital Signs Bar fragment immediately after <body...>.

    Idempotent: skip if already present.
    """
    if not html or "vital-signs-bar" in html:
        return html
    import re as _re
    m = _re.search(r"<body[^>]*>", html, _re.IGNORECASE)
    if not m:
        return html
    return html[:m.end()] + _VITAL_SIGNS_BAR_FRAGMENT + html[m.end():]


# -- Cycle 6 F5 -- /sprint-status consolidated 5-cycle review page --
#
# Single read-only HTML endpoint that renders the state of the 10h
# autonomous sprint. Five panels:
#   1. Sprint header     (cycle/mile/start/end/commits-shipped count)
#   2. Per-cycle table   (sha + agent + status + 1-line summary per row)
#   3. Live counters     (5s client-poll of /metrics -> DOM update)
#   4. Aaron-actions     (file-sentinel checkbox status)
#   5. Constitutional    (6 invariant dots, heuristic-graded)
#
# Constraints honoured:
#   - Page <50KB (current build ~14KB).
#   - Endpoint never blocks: git log shells out at most once per 60s
#     (cached on the daemon instance via _sprint_commits_cache).
#   - ZSF: every parse/IO step has try/except returning a placeholder.
#   - Additive: /dashboard, /health, /metrics untouched.
#   - Re-uses dashboard colour palette (slate / cyan / amber / emerald).

# Hardcoded sprint commit roster (from Hermes ledger, cycles 1-5).
# Each entry: (sha_prefix, agent_label, status, one_line_summary, cycle).
# status: "ok" (shipped), "partial" (yellow), "fail" (red).
# Resolved live against `git log` so SHA prefixes that don't match get
# rendered with a "missing" badge -- keeps the table truthful when a
# cycle commit hasn't actually landed yet.
_SPRINT_COMMITS_ROSTER = [
    # Cycle 1 -- north-star priority audits
    ("d9b9362", "B-priorities", "ok", "cross-cutting north-star priorities 2026-05-04", 1),
    ("6bd1a02", "B-priorities", "ok", "IDE state vs April 16 sprint + bridge cross-impact", 1),
    ("d15bba1", "B-priorities", "ok", "bridge gap re-rank post stress-test", 1),
    ("b70b13d", "B-priorities", "ok", "3s priorities post-bridge", 1),
    ("90537df", "B-priorities", "ok", "mf priorities post-bridge sweep", 1),
    # Cycle 2 -- RACE V* convergence
    ("4f5dd7d", "RACE-V3",      "ok", "3-surgeons surface disagreements (RACE V3)", 2),
    ("273b162", "RACE-V4",      "ok", "wire superset_bridge as S6/S8 context entry path", 2),
    ("c873700", "RACE-V1",      "ok", "fleet superpowers phase events on NATS", 2),
    ("d20d998", "audit-D5",     "ok", "round-4 regression sweep -- post-race-v* convergence", 2),
    # Cycle 3 -- interlocks + bridge OAuth + queue parallelisation
    ("46f26ff", "B1-interlock", "ok", "flip 7/7 mf-3s interlocks default-on", 3),
    ("c88a351", "CP5-3s",       "ok", "synaptic_3s_health DEGRADED detector", 3),
    ("df56465", "B3-bridge",    "ok", "OAuth Bearer pass-through for Pro/Max upstream", 3),
    ("4d84514", "B3-3s",        "ok", "opt-in route Cardiologist via ContextDNA bridge", 3),
    ("7ccde6d", "Neuro-restore","ok", "restore local Neuro (Qwen3-4B-4bit MLX)", 3),
    ("771c154", "B4-queue",     "ok", "parallelize AARON priority cap=4", 3),
    ("daf44f7", "supervision",  "ok", "liveness watchdog for fleet-nerve daemon", 3),
    ("f517758", "webhook-fix",  "ok", "NATS subscription resilience for webhook events", 3),
    ("fd83f2f", "subs-fix",     "ok", "re-register subscriptions on reconnect", 3),
    # Cycle 4 -- D/G ratelimit + tool-use translate + ZSF
    ("a270900", "D2-bridge",    "ok", "expose Anthropic ratelimit headers in /metrics + xbar", 4),
    ("ae5006b", "G1-bridge",    "ok", "tool_use to function-call translation", 4),
    ("14f6080", "audit-D1",     "ok", "webhook stability post-round-3 -- LIVE", 4),
    ("86b32e6", "C4-test-iso",  "ok", "isolate FleetRepairGate state between tests", 4),
    ("c1ffb11", "C4-zsf-fix",   "ok", "close 3 ZSF silent-except sites at line 386", 4),
    # Cycle 5 -- E series + Cycle 6 prelude
    ("8f4b7da", "E4-conflict",  "ok", "resolve submodule drift + fleet-state.json conflict", 5),
    ("3f414a8", "E5-hermes",    "ok", "10h autonomous sprint ledger entry, cycles 1-4", 5),
    ("1f2f27e", "ER-defense",   "ok", "defensive invariant smoke test for sacred audio", 5),
    ("837464d", "C6-vscode",    "ok", "bridge-mode status bar -- mirror xbar 4-state badge", 5),
]

# 8 known Aaron-actions for the queue panel. Each tuple: (id, kind, label).
# kind in {"auto", "pending"}; if /tmp/sprint-action-<id>.done exists we
# render "done" regardless. Roster is hardcoded so the panel keeps
# working even if F1 hasn't shipped scripts/sprint-aaron-actions.md yet.
_SPRINT_AARON_ACTIONS = [
    ("1", "pending", "Verify /sprint-status renders end-to-end (curl + browser)"),
    ("2", "auto",    "Confirm webhook /health.webhook.events_recorded advanced"),
    ("3", "pending", "Tag end-of-sprint commit on origin/main"),
    ("4", "pending", "Cardiologist sign-off on bridge OAuth pass-through (B3)"),
    ("5", "auto",    "Gains-gate after final cycle (./scripts/gains-gate.sh)"),
    ("6", "pending", "Hermes ledger appends cycle-6 + cycle-7 entries"),
    ("7", "auto",    "ER-simulator audio invariants smoke test green"),
    ("8", "pending", "Aaron review: rotate sprint sentinel files, archive Cycle 1-5"),
]


def _build_sprint_status_html(daemon_obj) -> bytes:
    """Render the 5-panel sprint-status page. Returns UTF-8 bytes.

    All IO is best-effort -- every block traps Exception and substitutes
    a placeholder so a single missing file or git timeout never breaks
    the page (ZERO SILENT FAILURES: failures count via daemon._stats
    where appropriate, but they never raise to the HTTP layer).
    """
    import html as _html
    import os as _os
    import subprocess as _sp

    # -- 1. Header data --------------------------------------------------
    try:
        started_at = float(daemon_obj._stats.get("started_at") or time.time())
    except Exception:
        started_at = time.time()
    # Sprint started ~6h ago by Aaron's brief; daemon may have restarted
    # mid-sprint, so fall back to the earliest of (daemon start,
    # "6 hours ago") to keep the header honest.
    sprint_start = min(started_at, time.time() - 6 * 3600)
    sprint_end = sprint_start + 10 * 3600
    cycle_n, mile_n = 6, 6
    commits_shipped = len(_SPRINT_COMMITS_ROSTER)

    # -- 2. Resolve roster against live git log (60s cache) --------------
    cache = getattr(daemon_obj, "_sprint_commits_cache", None)
    now = time.time()
    if not cache or (now - cache.get("ts", 0)) > 60:
        live_shas: dict = {}
        try:
            r = _sp.run(
                ["git", "log", "--since=10 hours ago",
                 "--pretty=format:%h|%H|%ai|%s"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=4,
            )
            if r.returncode == 0:
                for line in r.stdout.strip().split("\n"):
                    parts = line.split("|", 3)
                    if len(parts) == 4:
                        live_shas[parts[0]] = {
                            "full": parts[1], "time": parts[2], "subject": parts[3],
                        }
        except Exception as exc:
            live_shas = {"_err": str(exc)}
        cache = {"ts": now, "shas": live_shas}
        daemon_obj._sprint_commits_cache = cache
    live_shas = cache.get("shas", {}) or {}

    def _resolve(sha7):
        # Match by 7-char prefix in the live log; if absent flag missing.
        for k, v in live_shas.items():
            if isinstance(v, dict) and (k.startswith(sha7) or sha7.startswith(k)):
                return v
        return None

    # -- 3. Aaron-actions sentinel check ---------------------------------
    actions_rendered = []
    for aid, kind, label in _SPRINT_AARON_ACTIONS:
        try:
            sentinel = f"/tmp/sprint-action-{aid}.done"
            done = _os.path.exists(sentinel)
        except Exception:
            done = False
        actions_rendered.append((aid, kind, label, done))

    # -- 4. Constitutional Physics heuristics ----------------------------
    invariants = []

    # Determinism: green if no random_state/uuid keys appear in commit
    # subjects (heuristic -- true determinism check is impossible here).
    try:
        bad = [s for _h, _a, _st, s, _c in _SPRINT_COMMITS_ROSTER
               if "random" in s.lower() or "uuid" in s.lower()]
        invariants.append(
            ("Determinism", "ok" if not bad else "warn",
             "no random/uuid in subjects" if not bad
             else f"{len(bad)} suspicious subject(s)")
        )
    except Exception as exc:
        invariants.append(("Determinism", "warn", f"check skipped: {exc}"))

    # No-Discovery-at-Injection: green if no commit subject contains
    # "discover" outside of allowed ledger contexts.
    try:
        bad = [s for _h, _a, _st, s, _c in _SPRINT_COMMITS_ROSTER
               if "discover" in s.lower() and "ledger" not in s.lower()]
        invariants.append(
            ("No-Discovery-at-Injection",
             "ok" if not bad else "warn",
             "retrieval-only" if not bad else f"{len(bad)} discovery-flavoured commit(s)")
        )
    except Exception as exc:
        invariants.append(("No-Discovery-at-Injection", "warn", f"skip: {exc}"))

    # SOP-Integrity: green if every roster commit resolved to a real sha.
    try:
        missing = sum(1 for h, *_ in _SPRINT_COMMITS_ROSTER if _resolve(h) is None)
        invariants.append(
            ("SOP-Integrity",
             "ok" if missing == 0 else ("warn" if missing < 5 else "bad"),
             "all roster commits land in git log" if missing == 0
             else f"{missing} roster sha(s) not found in last 10h")
        )
    except Exception as exc:
        invariants.append(("SOP-Integrity", "warn", f"skip: {exc}"))

    # Evidence-Over-Confidence: green if /metrics has > 0 bridge calls
    # recorded -- proves observed behaviour, not just claims.
    try:
        bridge_total = (int(daemon_obj._stats.get("bridge_anthropic_ok", 0)) +
                        int(daemon_obj._stats.get("bridge_fallback_to_deepseek", 0)) +
                        int(daemon_obj._stats.get("bridge_chat_completions_requests", 0)))
        invariants.append(
            ("Evidence-Over-Confidence",
             "ok" if bridge_total > 0 else "warn",
             f"{bridge_total} bridge calls observed" if bridge_total > 0
             else "no bridge traffic yet -- claims unbacked")
        )
    except Exception as exc:
        invariants.append(("Evidence-Over-Confidence", "warn", f"skip: {exc}"))

    # Reversible: green if the most-recent roster commit is reachable
    # from HEAD (cheap sniff -- full revert dry-run would mutate WC).
    try:
        most_recent = _SPRINT_COMMITS_ROSTER[-1][0]
        full = (_resolve(most_recent) or {}).get("full") or most_recent
        r = _sp.run(["git", "merge-base", "--is-ancestor", full, "HEAD"],
                    cwd=str(REPO_ROOT), capture_output=True, timeout=2)
        ok = (r.returncode == 0)
        invariants.append(
            ("Reversible",
             "ok" if ok else "warn",
             "tip-most roster commit is ancestor of HEAD" if ok
             else "tip-most roster commit not reachable")
        )
    except Exception as exc:
        invariants.append(("Reversible", "warn", f"skip: {exc}"))

    # Minimalism: green if avg subject length < 80 chars (axe-vs-scalpel).
    try:
        subs = [s for _h, _a, _st, s, _c in _SPRINT_COMMITS_ROSTER]
        avg = sum(len(s) for s in subs) / max(1, len(subs))
        invariants.append(
            ("Minimalism",
             "ok" if avg < 80 else ("warn" if avg < 110 else "bad"),
             f"avg subject {avg:.0f} chars")
        )
    except Exception as exc:
        invariants.append(("Minimalism", "warn", f"skip: {exc}"))

    # -- 5. Build HTML ---------------------------------------------------
    def _esc(x):
        try:
            return _html.escape(str(x))
        except Exception:
            return "?"

    def _ts(epoch):
        try:
            return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))
        except Exception:
            return "?"

    cycle_rows = []
    for sha7, agent, status, summary, cy in _SPRINT_COMMITS_ROSTER:
        info = _resolve(sha7)
        landed = info is not None
        if not landed:
            badge = '<span class="badge bad">missing</span>'
        elif status == "ok":
            badge = '<span class="badge ok">shipped</span>'
        elif status == "partial":
            badge = '<span class="badge warn">partial</span>'
        else:
            badge = '<span class="badge bad">failed</span>'
        cycle_rows.append(
            f"<tr><td>{cy}</td><td><code>{_esc(sha7)}</code></td>"
            f"<td>{_esc(agent)}</td><td>{badge}</td>"
            f"<td>{_esc(summary)}</td></tr>"
        )

    action_rows = []
    for aid, kind, label, done in actions_rendered:
        if done:
            badge = '<span class="badge ok">done</span>'
        elif kind == "auto":
            badge = '<span class="badge auto">auto</span>'
        else:
            badge = '<span class="badge warn">pending</span>'
        action_rows.append(
            f"<tr><td>#{_esc(aid)}</td><td>{badge}</td><td>{_esc(label)}</td></tr>"
        )

    inv_dots = []
    for name, level, reason in invariants:
        klass = {"ok": "dot-ok", "warn": "dot-warn", "bad": "dot-bad"}.get(level, "dot-warn")
        inv_dots.append(
            f'<li><span class="dot {klass}"></span><b>{_esc(name)}</b>'
            f'<span class="reason"> -- {_esc(reason)}</span></li>'
        )

    # Counters poll: list of (prom-name, label) pairs the JS should
    # fetch from /metrics every 5s.
    counter_keys = [
        ("fleet_nerve_bridge_anthropic_ok",            "bridge anthropic ok"),
        ("fleet_nerve_bridge_fallback_to_deepseek",    "bridge fallback to deepseek"),
        ("fleet_nerve_bridge_oauth_passthrough_attempted", "bridge oauth passthru attempts"),
        ("fleet_nerve_bridge_tool_translate_attempted",   "bridge tool translate attempts"),
        ("fleet_nerve_bridge_chat_completions_requests",  "bridge chat-completions reqs"),
        ("fleet_nerve_bridge_ratelimit_requests_remaining", "ratelimit requests left"),
        ("fleet_nerve_bridge_ratelimit_tokens_remaining",   "ratelimit tokens left"),
        ("fleet_nerve_webhook_subscription_count",     "webhook subscription count"),
        ("fleet_nerve_webhook_events_recorded",        "webhook events recorded"),
        ("fleet_nerve_webhook_publish_errors",         "webhook publish errors"),
        ("fleet_nerve_queue_concurrent_inflight_max_1", "queue inflight AARON (P1)"),
        ("fleet_nerve_queue_concurrent_inflight_max_2", "queue inflight ATLAS (P2)"),
        ("fleet_nerve_queue_concurrent_inflight_max_3", "queue inflight EXTERNAL (P3)"),
        ("fleet_nerve_queue_concurrent_inflight_max_4", "queue inflight BACKGROUND (P4)"),
    ]
    counter_rows = "".join(
        f'<tr data-counter="{_esc(k)}"><td>{_esc(label)}</td>'
        f'<td class="counter-val" data-k="{_esc(k)}">...</td></tr>'
        for k, label in counter_keys
    )
    counters_keys_json = json.dumps([k for k, _ in counter_keys])

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<title>Sprint Status -- Cycle {cycle_n} / Mile {mile_n}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
 :root{{--bg:#020617;--fg:#cbd5e1;--mute:#64748b;--cyan:#22d3ee;
   --amber:#f59e0b;--red:#ef4444;--green:#10b981;--card:#0f172a;--brd:#1e293b;}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--fg);
   font:14px/1.45 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;}}
 header{{padding:1rem 1.25rem;border-bottom:1px solid var(--brd);
   background:linear-gradient(180deg,#0b1224 0%,#020617 100%);}}
 header h1{{margin:0;font-size:18px;color:var(--cyan);
   letter-spacing:.04em;font-weight:700}}
 header .meta{{color:var(--mute);font-size:12px;margin-top:4px}}
 main{{padding:1rem 1.25rem;display:grid;gap:1rem;
   grid-template-columns:repeat(auto-fit,minmax(360px,1fr));}}
 section{{background:var(--card);border:1px solid var(--brd);
   border-radius:6px;padding:.85rem 1rem;}}
 section h2{{margin:0 0 .55rem;font-size:12px;color:var(--cyan);
   letter-spacing:.06em;text-transform:uppercase;font-weight:700}}
 table{{width:100%;border-collapse:collapse;font-size:12px}}
 th,td{{text-align:left;padding:4px 6px;border-bottom:1px solid #111c33;
   vertical-align:top}}
 th{{color:var(--mute);font-weight:600;font-size:10px;text-transform:uppercase;
   letter-spacing:.05em}}
 code{{color:#e2e8f0;background:#0b1224;padding:1px 4px;border-radius:3px;
   font:600 11px ui-monospace,SFMono-Regular,Menlo,monospace}}
 .badge{{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;
   font-weight:700;letter-spacing:.04em}}
 .badge.ok{{background:#063a2a;color:var(--green)}}
 .badge.warn{{background:#3d2c08;color:var(--amber)}}
 .badge.bad{{background:#3a0d12;color:var(--red)}}
 .badge.auto{{background:#0b2237;color:var(--cyan)}}
 ul.invariants{{list-style:none;margin:0;padding:0}}
 ul.invariants li{{padding:5px 0;border-bottom:1px solid #111c33;font-size:12px}}
 ul.invariants li:last-child{{border-bottom:none}}
 .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;
   margin-right:8px;vertical-align:middle}}
 .dot-ok{{background:var(--green);box-shadow:0 0 6px #10b98199}}
 .dot-warn{{background:var(--amber);box-shadow:0 0 6px #f59e0b99}}
 .dot-bad{{background:var(--red);box-shadow:0 0 6px #ef444499}}
 .reason{{color:var(--mute)}}
 .counter-val{{color:var(--cyan);font-variant-numeric:tabular-nums;text-align:right}}
 .counter-val.bad{{color:var(--red)}}
 .counter-val.warn{{color:var(--amber)}}
 footer{{padding:.6rem 1.25rem;color:var(--mute);font-size:11px;
   border-top:1px solid var(--brd)}}
 .pill{{display:inline-block;padding:1px 7px;border-radius:10px;
   background:#0b2237;color:var(--cyan);font-size:11px;margin-right:6px}}
</style>
</head><body>
<header>
 <h1>Sprint Status -- Cycle {cycle_n} / Mile {mile_n}</h1>
 <div class="meta">
   <span class="pill">start {_ts(sprint_start)}</span>
   <span class="pill">target end {_ts(sprint_end)}</span>
   <span class="pill">commits shipped {commits_shipped}</span>
   <span class="pill">node {_esc(daemon_obj.node_id)}</span>
 </div>
</header>
<main>
 <section style="grid-column:1/-1">
  <h2>Per-cycle commits ({commits_shipped})</h2>
  <table>
   <thead><tr><th>Cy</th><th>SHA</th><th>Agent</th><th>Status</th><th>Summary</th></tr></thead>
   <tbody>
    {{cycle_rows_html}}
   </tbody>
  </table>
 </section>

 <section>
  <h2>Live counters (poll /metrics @5s)</h2>
  <table>
   <thead><tr><th>Counter</th><th style="text-align:right">Value</th></tr></thead>
   <tbody>
    {{counter_rows_html}}
   </tbody>
  </table>
  <div style="margin-top:.4rem;color:var(--mute);font-size:10px"
       id="counters-status">awaiting first poll...</div>
 </section>

 <section>
  <h2>Aaron actions queue (sentinel: /tmp/sprint-action-N.done)</h2>
  <table>
   <thead><tr><th>#</th><th>State</th><th>Action</th></tr></thead>
   <tbody>
    {{action_rows_html}}
   </tbody>
  </table>
 </section>

 <section style="grid-column:1/-1">
  <h2>Constitutional Physics -- invariant pulse</h2>
  <ul class="invariants">
   {{inv_dots_html}}
  </ul>
 </section>
</main>
<footer>
 fleet-nerve / sprint-status / view #{int(daemon_obj._stats.get("sprint_status_views", 0))}
 / git log cache TTL 60s / client-poll only (zero internal polling)
</footer>
<script>
(function(){{
 var keys = {counters_keys_json};
 function parseProm(text){{
   var m={{}};text.split("\n").forEach(function(l){{
     if(!l||l[0]=="#")return;
     var sp=l.lastIndexOf(" ");if(sp<0)return;
     var name=l.slice(0,sp).replace(/[{{][^}}]*[}}]/,"");
     var v=parseFloat(l.slice(sp+1));if(!isNaN(v))m[name]=v;
   }});
   return m;
 }}
 function fmt(n){{
   if(n<0)return "-";
   if(n>=1e6)return (n/1e6).toFixed(1)+"M";
   if(n>=1e3)return (n/1e3).toFixed(1)+"k";
   return String(n);
 }}
 function refresh(){{
   fetch("/metrics").then(function(r){{return r.text();}})
    .then(function(t){{
      var m=parseProm(t);
      keys.forEach(function(k){{
        var cell=document.querySelector('[data-k="'+k+'"]');
        if(!cell)return;
        var v=m[k];
        if(v===undefined){{cell.textContent="N/A";cell.classList.add("warn");return;}}
        cell.textContent=fmt(v);
        cell.classList.remove("warn","bad");
        if(/ratelimit_(requests|tokens)_remaining/.test(k) && v===0)
          cell.classList.add("bad");
        if(/publish_errors|failures/.test(k) && v>0)
          cell.classList.add("warn");
      }});
      var s=document.getElementById("counters-status");
      if(s)s.textContent="last poll "+new Date().toLocaleTimeString();
    }})
    .catch(function(e){{
      var s=document.getElementById("counters-status");
      if(s)s.textContent="poll error: "+e;
    }});
 }}
 refresh();setInterval(refresh,5000);
}})();
</script>
</body></html>
"""
    page = page.replace("{cycle_rows_html}", "".join(cycle_rows))
    page = page.replace("{counter_rows_html}", counter_rows)
    page = page.replace("{action_rows_html}", "".join(action_rows))
    page = page.replace("{inv_dots_html}", "".join(inv_dots))
    return page.encode("utf-8")


class CommunicationProtocol:
    """Invariant self-healing communication protocol.

    Enforces:
    - Channel cascade P0-P8 with 30s timeout per channel
    - Standing self-heal loop (every 60s) that probes peers and auto-repairs
    - Channel state tracking per peer, exposed via get_channel_state()
    - Secrets protection: never logs message content, API keys, or auth tokens
    - P8 (direct user text input) as absolute last resort, rate-limited 1/30s
    """

    def __init__(self, nerve: "FleetNerveNATS"):
        self._nerve = nerve
        self.channel_state = ChannelState()
        self._heal_cooldowns: dict = {}  # "peer:action" -> last_attempt_time
        self._self_heal_task: Optional[asyncio.Task] = None
        # Event-driven immediate audit (race/a1-immediate-audit):
        # When send_with_fallback exhausts all channels, NATS drops, or a
        # previously-broken peer's heartbeat arrives, we fire a one-shot
        # audit task per peer instead of waiting up to 30s for the next
        # _watchdog_loop tick. The dict holds the in-flight asyncio.Task
        # so re-entrant triggers debounce to a single audit per peer.
        self._pending_audits: dict = {}  # peer_id -> asyncio.Task
        # Hard timeout for a single audit — prevents a wedged channel
        # probe from pinning _pending_audits and blocking re-trigger.
        self._audit_timeout_s: float = 10.0

    def start(self):
        """Start the standing self-heal background loop."""
        if self._self_heal_task is None or self._self_heal_task.done():
            self._self_heal_task = asyncio.create_task(self._self_heal_loop())
            logger.info("CommunicationProtocol: self-heal loop started (60s interval)")

    def stop(self):
        """Stop the self-heal loop."""
        if self._self_heal_task and not self._self_heal_task.done():
            self._self_heal_task.cancel()
            logger.info("CommunicationProtocol: self-heal loop stopped")

    # ── Channel Cascade (30s per channel) ──

    async def send(self, target: str, message: dict, use_cloud: bool = False) -> dict:
        """Send message via channel cascade. Each channel gets max 30s.

        P0 Cloud only when use_cloud=True or all local channels fail.
        P8 Keystroke only as absolute last resort after all others fail.
        Never logs message content, API keys, or auth tokens.
        """
        # Build sender map. Static order uses CHANNEL_ORDER (P1→P7); P0 cloud
        # is optionally prepended when use_cloud=True. P8 keystroke stays out
        # of the cascade loop — it runs below as an absolute last resort.
        sender_map = {
            CHANNEL_P0_CLOUD: self._nerve._send_p0_cloud,
            CHANNEL_P1_NATS: self._nerve._send_p1_nats,
            CHANNEL_P2_HTTP: self._nerve._send_p2_http,
            CHANNEL_P3_CHIEF: self._nerve._send_p3_chief,
            CHANNEL_P4_SEED: self._nerve._send_p4_seed,
            CHANNEL_P5_SSH: self._nerve._send_p5_ssh,
            CHANNEL_P6_WOL: self._nerve._send_p6_wol,
            CHANNEL_P7_GIT: self._nerve._send_p7_git,
        }
        # Drop P8_keystroke — it is handled below as the absolute last resort.
        static_order = [ch for ch in CHANNEL_ORDER if ch != CHANNEL_P8_KEYSTROKE]
        if use_cloud:
            static_order = [CHANNEL_P0_CLOUD] + static_order

        # Adaptive re-rank: channels with >= MIN_ATTEMPTS_FOR_RERANK samples
        # get sorted by rolling success_rate (desc), then last_success recency
        # (desc), then static priority (asc) as tiebreaker. Cold channels keep
        # their static rank — preserving P1→P8 fallback during warm-up.
        ordered_names = self.channel_state.get_channel_order(target, static_order)
        channels = [(name, sender_map[name]) for name in ordered_names if name in sender_map]

        # Ensure message envelope
        if "id" not in message:
            message["id"] = str(uuid.uuid4())
        if "from" not in message:
            message["from"] = self._nerve.node_id
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat() + "Z"

        errors = []
        for name, sender in channels:
            # Skip known-broken channels (saves time, retried by self-heal loop)
            if self.channel_state.is_known_broken(target, name):
                errors.append(f"{name}: skipped (known broken)")
                logger.debug(f"[{name}] Skipped for {target} -- known broken")
                continue

            timeout = CHANNEL_TIMEOUT_S.get(name, 30)
            t0 = time.time()
            try:
                result = await asyncio.wait_for(sender(target, message), timeout=timeout)
                elapsed_ms = int((time.time() - t0) * 1000)
                if result.get("delivered"):
                    self.channel_state.record_success(target, name, elapsed_ms)
                    logger.info(f"[{name}] Delivered to {target} ({elapsed_ms}ms)")
                    # If we fell through to P3+, earlier channels are broken -- trigger heal
                    if name not in (CHANNEL_P0_CLOUD, CHANNEL_P1_NATS, CHANNEL_P2_HTTP):
                        broken = [e.split(":")[0] for e in errors if "skipped" not in e]
                        if broken:
                            asyncio.ensure_future(self._trigger_heal(target, name, broken))
                    return {
                        "delivered": True, "channel": name,
                        "elapsed_ms": elapsed_ms, **result,
                    }
                if result.get("wol_sent"):
                    self.channel_state.record_failure(target, name, "wol_only")
                    errors.append(f"{name}: WoL sent, not delivered")
                    continue
                # Not delivered -- record failure
                reason = result.get("error", "delivered=false")
                self.channel_state.record_failure(target, name, reason)
                errors.append(f"{name}: {reason}")
            except asyncio.TimeoutError:
                elapsed_ms = int((time.time() - t0) * 1000)
                self.channel_state.record_failure(target, name, f"timeout ({timeout}s)")
                errors.append(f"{name}: timeout ({timeout}s)")
                logger.debug(f"[{name}] Timed out for {target} after {timeout}s")
            except Exception as e:
                elapsed_ms = int((time.time() - t0) * 1000)
                self.channel_state.record_failure(target, name, str(e))
                errors.append(f"{name}: {e}")
                logger.debug(f"[{name}] Failed for {target}: {e}")

        # P0 cloud fallback if not already tried
        if not use_cloud:
            t0 = time.time()
            try:
                result = await asyncio.wait_for(
                    self._nerve._send_p0_cloud(target, message),
                    timeout=CHANNEL_TIMEOUT_S[CHANNEL_P0_CLOUD])
                elapsed_ms = int((time.time() - t0) * 1000)
                if result.get("delivered"):
                    self.channel_state.record_success(target, CHANNEL_P0_CLOUD, elapsed_ms)
                    logger.info(f"[{CHANNEL_P0_CLOUD}] Delivered to {target} (fallback, {elapsed_ms}ms)")
                    return {"delivered": True, "channel": CHANNEL_P0_CLOUD, **result}
                self.channel_state.record_failure(target, CHANNEL_P0_CLOUD, "delivered=false")
            except Exception as e:
                self.channel_state.record_failure(target, CHANNEL_P0_CLOUD, str(e))
                errors.append(f"{CHANNEL_P0_CLOUD}: {e}")

        # P8: Direct user text input -- ABSOLUTE LAST RESORT
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                self._send_p8_keystroke(target, message),
                timeout=CHANNEL_TIMEOUT_S[CHANNEL_P8_KEYSTROKE])
            elapsed_ms = int((time.time() - t0) * 1000)
            if result.get("delivered"):
                self.channel_state.record_success(target, CHANNEL_P8_KEYSTROKE, elapsed_ms)
                return {"delivered": True, "channel": CHANNEL_P8_KEYSTROKE, **result}
            self.channel_state.record_failure(target, CHANNEL_P8_KEYSTROKE, "not_delivered")
            errors.append(f"{CHANNEL_P8_KEYSTROKE}: not delivered")
        except asyncio.TimeoutError:
            self.channel_state.record_failure(target, CHANNEL_P8_KEYSTROKE, "timeout")
            errors.append(f"{CHANNEL_P8_KEYSTROKE}: timeout")
        except Exception as e:
            self.channel_state.record_failure(target, CHANNEL_P8_KEYSTROKE, str(e))
            errors.append(f"{CHANNEL_P8_KEYSTROKE}: {e}")

        logger.warning(f"All channels (P0-P8) failed for {target}: {[e.split(':')[0] for e in errors]}")
        return {"delivered": False, "errors": errors}

    # ── P8: Direct User Text Input (LAST RESORT) ──

    _last_p8_time = 0.0  # Class-level rate limit: 1 per 30s

    async def _send_p8_keystroke(self, target: str, message: dict) -> dict:
        """P8: Direct user text input via osascript keystroke.

        LAST RESORT ONLY. Rate-limited to 1 per 30s. Logs a warning when used.
        Only works when target is the local machine (can't keystroke remote machines).
        Never includes message content in the keystroke -- only a trigger command.
        """
        # Only works for local node
        if target != self._nerve.node_id:
            return {"delivered": False, "error": "P8 only works for local node"}

        # Rate limit: max 1 keystroke per 30s
        now = time.time()
        if now - CommunicationProtocol._last_p8_time < 30:
            remaining = int(30 - (now - CommunicationProtocol._last_p8_time))
            return {"delivered": False, "error": f"P8 rate limited ({remaining}s remaining)"}

        logger.warning(
            f"P8 LAST RESORT: all channels P0-P7 failed for {target}. "
            f"Using direct keystroke input. This should be investigated."
        )
        CommunicationProtocol._last_p8_time = now

        # Write message to seed file first (keystroke just triggers reading it)
        payload = message.get("payload", {})
        seed_path = os.path.join(SEED_DIR, f"fleet-seed-{target}.md")
        sender = message.get("from", "unknown")
        subject = payload.get("subject", "fleet message")
        with open(seed_path, "a") as f:
            f.write(f"\n## [P8-LASTRESORT] from {sender}: {subject}\n\n")
            f.write("(Delivered via P8 keystroke -- all other channels failed)\n\n---\n")

        # Trigger VS Code keystroke
        self._nerve._keystroke_trigger(f"P8 last resort from {sender}: {subject}")
        return {"delivered": True, "method": "p8_keystroke"}

    # ── Standing Self-Heal Loop ──

    async def _self_heal_loop(self):
        """Background heal coordinator.

        Two modes:
          - ZERO_POLLING_ENABLED=True (default): event-driven. Peer death is detected by
            NATS callbacks (on_peer_disconnect) and by NoRespondersError when a caller
            tries to talk to a peer. A 5-minute watchdog-of-last-resort runs to catch
            split-brain cases that no callback observes (e.g. silent partition where
            the NATS server still accepts publishes but nobody's listening).
          - ZERO_POLLING_ENABLED=False (legacy): 60-second probe-every-peer loop.

        Even in zero-polling mode this coroutine stays alive so shutdown/cancel
        semantics match the existing daemon lifecycle.
        """
        # Wait for startup stabilization — need heartbeats before judging health
        await asyncio.sleep(120)

        interval = WATCHDOG_OF_LAST_RESORT_S if ZERO_POLLING_ENABLED else SELF_HEAL_INTERVAL_S
        mode = "zero-polling (callback-driven, 5min sanity watchdog)" if ZERO_POLLING_ENABLED else "legacy (60s probe)"
        logger.info(f"Self-heal loop active -- mode={mode}, interval={interval}s")

        while True:  # noqa: no-poll — self-heal watchdog of-last-resort (5-min sanity, callback-driven elsewhere)
            try:
                await self._self_heal_cycle()
            except asyncio.CancelledError:
                logger.info("Self-heal loop cancelled")
                return
            except Exception as e:
                logger.error(f"Self-heal cycle error: {e}")
            await asyncio.sleep(interval)

    async def on_peer_disconnect(self, peer_id: str, reason: str = "callback"):
        """Event-driven entrypoint: NATS signals peer death.

        Does NOT immediately repair. Instead debounces: waits 10s then runs
        verification probe. Transient disconnects (network blip, brief sleep)
        self-resolve within 10s — no repair needed.
        """
        nerve = self._nerve
        counters = getattr(nerve, "_stats", None)
        if counters is not None:
            counters["peer_failures_detected_via_callback"] = (
                counters.get("peer_failures_detected_via_callback", 0) + 1
            )
        logger.info(f"Zero-polling: peer_disconnect for {peer_id} ({reason}) — debounce 10s")
        # Debounce: wait before verifying. Most disconnects are transient.
        await asyncio.sleep(10)
        # Re-check heartbeat — if peer reconnected during debounce, skip repair
        hb = nerve._peers.get(peer_id, {})
        if hb:
            age = time.time() - hb.get("_received_at", 0)
            if age < 45:
                logger.info(f"Zero-polling: {peer_id} reconnected during debounce ({int(age)}s ago)")
                return
        try:
            await self._probe_and_heal_peer(peer_id)
        except Exception as e:
            logger.debug(f"on_peer_disconnect probe failed for {peer_id}: {e}")

    async def on_no_responders(self, peer_id: str, subject: str):
        """Event-driven entrypoint: NoRespondersError on NATS request.

        NoRespondersError means no subscriber on that subject RIGHT NOW.
        But peer might be restarting or subject might be wrong. Verify
        with heartbeat check before entering repair path.
        """
        nerve = self._nerve
        counters = getattr(nerve, "_stats", None)
        if counters is not None:
            counters["peer_failures_detected_via_no_responders"] = (
                counters.get("peer_failures_detected_via_no_responders", 0) + 1
            )
        # Check if peer has recent heartbeat — if so, subject mismatch not peer death
        hb = nerve._peers.get(peer_id, {})
        if hb:
            age = time.time() - hb.get("_received_at", 0)
            if age < 45:
                logger.debug(
                    f"Zero-polling: NoRespondersError for {peer_id} on {subject} "
                    f"but heartbeat {int(age)}s ago — subject issue, not peer death"
                )
                return
        logger.info(f"Zero-polling: NoRespondersError for {peer_id} on {subject} — verified stale")
        self.channel_state.record_failure(peer_id, CHANNEL_P1_NATS, f"no_responders:{subject}")
        # Wire NO_RESPONDERS into repair quorum
        nerve = self._nerve
        if hasattr(nerve, "_event_bus") and nerve._event_bus is not None:
            rq = nerve._event_bus.get("repair_quorum")
            if rq is not None:
                try:
                    from multifleet.fleet_event_bus import RepairSignal
                    rq.record_signal(peer_id, RepairSignal.NO_RESPONDERS)
                except Exception:
                    pass
        try:
            await self._probe_and_heal_peer(peer_id)
        except Exception as e:
            logger.debug(f"on_no_responders probe failed for {peer_id}: {e}")

    async def _self_heal_cycle(self):
        """One cycle of the self-heal loop."""
        all_peers = set(self._nerve._peers_config.keys()) - {self._nerve.node_id}
        if not all_peers:
            return

        for peer_id in all_peers:
            try:
                await self._probe_and_heal_peer(peer_id)
            except Exception as e:
                logger.debug(f"Self-heal probe failed for {peer_id}: {e}")

    async def _probe_and_heal_peer(self, peer_id: str):
        """Probe a single peer's channels and trigger repair only on verified failure.

        Philosophy: repair is REACTIVE, not proactive. We only enter the repair
        path when a real communication attempt has failed AND a verification probe
        confirms the peer is actually unreachable. Transient blips and expected
        NAT failures do not count.

        Flow:
          1. Check P1 NATS heartbeat recency (passive — no network call)
          2. If P1 healthy → peer is alive, record success, done. No P2 probe.
          3. If P1 stale → verify with a direct NATS request-reply ping
          4. If verification ping succeeds → transient, record recovery, done
          5. Only if verification also fails → try P2 HTTP as secondary confirm
          6. Only if BOTH P1 verified-dead AND P2 failed → enter repair path
        """
        peer = self._nerve._resolve_peer(peer_id)
        ip = peer.get("ip")
        if not ip:
            return

        # Step 1: Check P1 NATS heartbeat (passive, zero cost)
        hb = self._nerve._peers.get(peer_id, {})
        if hb:
            last_seen = time.time() - hb.get("_received_at", 0)
            if last_seen < 45:  # Heartbeat within 45s = healthy
                self.channel_state.record_success(peer_id, CHANNEL_P1_NATS, int(last_seen * 1000))
                # Peer alive — no P2 probe needed, no repair needed
                if hasattr(self._nerve, "_struggle_counter"):
                    self._nerve._struggle_counter.pop(peer_id, None)
                return

        # Step 2: P1 stale or missing — verify with active NATS ping before
        # declaring broken. Transient heartbeat delays are common and don't
        # warrant repair.
        p1_verified_dead = True
        try:
            nc = self._nerve._nc
            if nc and nc.is_connected:
                reply = await asyncio.wait_for(
                    nc.request(f"fleet.health.{peer_id}", b"ping", timeout=5),
                    timeout=8,
                )
                if reply and reply.data:
                    # Peer responded — heartbeat was just delayed, not dead
                    p1_verified_dead = False
                    self.channel_state.record_success(peer_id, CHANNEL_P1_NATS, 0)
                    logger.debug(f"Self-heal: {peer_id} P1 stale but ping OK — transient")
                    if hasattr(self._nerve, "_struggle_counter"):
                        self._nerve._struggle_counter.pop(peer_id, None)
                    return
        except Exception:
            pass  # Ping failed — P1 confirmed unreachable

        if p1_verified_dead:
            if hb:
                last_seen = time.time() - hb.get("_received_at", 0)
                self.channel_state.record_failure(
                    peer_id, CHANNEL_P1_NATS,
                    f"verified dead: stale heartbeat ({int(last_seen)}s) + ping failed")
            else:
                self.channel_state.record_failure(
                    peer_id, CHANNEL_P1_NATS, "verified dead: no heartbeat + ping failed")

        # Step 3: P1 verified dead — now check P2 HTTP as secondary confirmation.
        # Only probe P2 when P1 is confirmed broken (not speculatively).
        p2_ok = False
        t0 = time.time()
        try:
            import urllib.request
            req = urllib.request.Request(f"http://{ip}:{FLEET_DAEMON_PORT}/health", method="GET")
            resp = urllib.request.urlopen(req, timeout=5)
            elapsed_ms = int((time.time() - t0) * 1000)
            if resp.status == 200:
                p2_ok = True
                self.channel_state.record_success(peer_id, CHANNEL_P2_HTTP, elapsed_ms)
                # P2 works — peer is alive, P1 is just having NATS issues
                # Don't repair the whole peer, just note P1 degradation
                logger.info(f"Self-heal: {peer_id} P1 dead but P2 OK — NATS-only issue")
                return
        except Exception:
            elapsed_ms = int((time.time() - t0) * 1000)
            self.channel_state.record_failure(peer_id, CHANNEL_P2_HTTP, "health probe failed")

        # Both P1 and P2 are broken -- check cooldown before repair
        broken = self.channel_state.get_broken_channels(peer_id)
        if not broken:
            return

        # Determine repair action
        action = "restart_nats" if CHANNEL_P1_NATS in broken else "check_connectivity"
        cooldown_key = f"{peer_id}:{action}"
        now = time.time()
        last_attempt = self._heal_cooldowns.get(cooldown_key, 0)
        if now - last_attempt < REPAIR_COOLDOWN_S:
            logger.debug(
                f"Self-heal cooldown for {cooldown_key}: "
                f"{int(now - last_attempt)}s < {REPAIR_COOLDOWN_S}s"
            )
            return

        self._heal_cooldowns[cooldown_key] = now
        logger.info(
            f"Self-heal: {peer_id} has broken channels {broken}. "
            f"Sending repair via best working channel."
        )

        # Send repair request via best working channel
        heal_msg = {
            "id": str(uuid.uuid4()),
            "type": "repair",
            "from": self._nerve.node_id,
            "to": peer_id,
            "payload": {
                "subject": "self-heal-request",
                "body": json.dumps({
                    "reporter": self._nerve.node_id,
                    "broken_channels": broken,
                    "action": action,
                    "level": 3,  # ASSIST -- auto-exec
                }),
            },
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        }

        # Try each channel in order (use cascade), skip known-broken for this peer
        for ch_name in CHANNEL_ORDER:
            if self.channel_state.is_known_broken(peer_id, ch_name):
                continue
            # NEVER use P7_git for self-heal — it creates git commits that flood the repo
            sender_map = {
                CHANNEL_P1_NATS: self._nerve._send_p1_nats,
                CHANNEL_P2_HTTP: self._nerve._send_p2_http,
                CHANNEL_P3_CHIEF: self._nerve._send_p3_chief,
                CHANNEL_P4_SEED: self._nerve._send_p4_seed,
                CHANNEL_P5_SSH: self._nerve._send_p5_ssh,
            }
            sender = sender_map.get(ch_name)
            if not sender:
                continue
            try:
                result = await asyncio.wait_for(sender(peer_id, heal_msg), timeout=10)
                # Feed outcome into peer-quorum self_health_score history
                if hasattr(self._nerve, "_record_send_outcome"):
                    self._nerve._record_send_outcome(peer_id, bool(result.get("delivered")))
                if result.get("delivered"):
                    logger.info(f"Self-heal repair sent to {peer_id} via {ch_name}")
                    # Delivered — L1-L4 has a chance to land; reset struggle counter
                    if hasattr(self._nerve, "_struggle_counter"):
                        self._nerve._struggle_counter.pop(peer_id, None)
                    return
            except Exception:
                if hasattr(self._nerve, "_record_send_outcome"):
                    self._nerve._record_send_outcome(peer_id, False)
                continue

        logger.warning(f"Self-heal: could not deliver repair to {peer_id} -- all channels failed")

        # All local channels failed → escalate toward peer-quorum L5.
        # Increment consecutive-failure counter; at STRUGGLE_THRESHOLD publish
        # fleet.peer.struggling so another node can self-elect to assist.
        if hasattr(self._nerve, "_struggle_counter"):
            self._nerve._struggle_counter[peer_id] = self._nerve._struggle_counter.get(peer_id, 0) + 1
            attempts = self._nerve._struggle_counter[peer_id]
            if attempts >= STRUGGLE_THRESHOLD:
                logger.warning(
                    f"Self-heal: {peer_id} struggling for {attempts} cycles — "
                    f"broadcasting fleet.peer.struggling for peer-quorum assist"
                )
                try:
                    await self._nerve._publish_struggle(peer_id, broken, attempts)
                except Exception as e:
                    logger.warning(f"struggle broadcast failed for {peer_id}: {e}")

    async def _trigger_heal(self, target: str, working_channel: str, broken: list):
        """Lightweight heal trigger when send_with_fallback falls through."""
        if not broken:
            return
        action = "restart_nats" if CHANNEL_P1_NATS in broken else "check_connectivity"
        cooldown_key = f"{target}:{action}"
        now = time.time()
        if now - self._heal_cooldowns.get(cooldown_key, 0) < REPAIR_COOLDOWN_S:
            return
        self._heal_cooldowns[cooldown_key] = now
        logger.info(f"Cascade heal trigger: {target} reached via {working_channel}, broken: {broken}")
        # Delegate to the existing repair_with_escalation
        asyncio.ensure_future(
            self._nerve.repair_with_escalation(target, broken, action))

    # ── Event-Driven Immediate Audit (race/a1-immediate-audit) ──
    #
    # When ANY failure signal arrives (send exhaustion, NATS drop, recovery
    # heartbeat), fire a one-shot audit task that probes every channel for
    # the affected peer RIGHT NOW. Replaces "wait up to 30s for next
    # _watchdog_loop tick" with "fire in < 100ms on the event loop".
    #
    # Debounce guarantee: if an audit task for peer X is already in flight,
    # further triggers within that window increment _stats.audits_debounced
    # and return early (no new task scheduled). This prevents floods when
    # many sends fail concurrently to the same peer.
    #
    # Hard timeout: each audit runs under asyncio.wait_for(_audit_timeout_s)
    # so a wedged probe cannot pin the debounce slot forever.

    def trigger_immediate_audit(self, peer_id: str, reason: str = "unspecified") -> bool:
        """Schedule an immediate audit of all channels for ``peer_id``.

        Returns True if a new audit task was scheduled, False if debounced
        (already running) or rejected (self/broadcast target).

        Safe to call from any coroutine context that has a running event
        loop. The audit runs in the background; callers do NOT await it.
        """
        # Never audit self or broadcast target -- spec constraint
        if peer_id == self._nerve.node_id or peer_id in ("all", ""):
            return False

        # Debounce: if a task is already running for this peer, skip.
        # Task may exist but be done() -- in that case we clear and re-schedule.
        existing = self._pending_audits.get(peer_id)
        if existing is not None and not existing.done():
            self._nerve._stats["audits_debounced"] = (
                self._nerve._stats.get("audits_debounced", 0) + 1
            )
            logger.debug(
                f"audit.debounced peer={peer_id} reason={reason} "
                f"(audit already in flight)"
            )
            return False

        # Only schedule if we have a running event loop -- otherwise we
        # would raise RuntimeError instead of observable counter tick.
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_running():
                # No running loop (e.g. called from sync HTTP handler).
                # Fall through to create_task via the nerve's _loop attribute
                # if available, otherwise record and bail.
                if getattr(self._nerve, "_loop", None) is not None:
                    coro = self._run_audit(peer_id, reason)
                    task = asyncio.run_coroutine_threadsafe(coro, self._nerve._loop)
                    # run_coroutine_threadsafe returns concurrent.futures.Future,
                    # not asyncio.Task. Wrap for the pending_audits dict by
                    # checking .done() -- concurrent Futures also expose done().
                    self._pending_audits[peer_id] = task
                    self._nerve._stats["audits_triggered"] = (
                        self._nerve._stats.get("audits_triggered", 0) + 1
                    )
                    logger.info(
                        f"audit.triggered peer={peer_id} reason={reason} "
                        f"(cross-thread)"
                    )
                    return True
                logger.warning(
                    f"audit.trigger peer={peer_id} reason={reason}: "
                    f"no running event loop -- skipped"
                )
                return False
        except RuntimeError as e:
            logger.warning(
                f"audit.trigger peer={peer_id} reason={reason}: "
                f"event-loop lookup failed ({e})"
            )
            return False

        task = asyncio.create_task(self._run_audit(peer_id, reason))
        self._pending_audits[peer_id] = task
        self._nerve._stats["audits_triggered"] = (
            self._nerve._stats.get("audits_triggered", 0) + 1
        )
        logger.info(f"audit.triggered peer={peer_id} reason={reason}")
        return True

    async def _run_audit(self, peer_id: str, reason: str) -> dict:
        """Run the audit for ``peer_id``: probe every channel once.

        Updates ``channel_state`` per probe result. If the probe reveals
        broken channels AND repair cooldown has expired, fires one
        ``repair_with_escalation`` via the nerve (gate-protected).

        Structured log events:
          * ``audit.triggered`` (emitted from the caller, before task start)
          * ``audit.complete`` (emitted here, with per-channel result dict)

        Hard timeout: ``self._audit_timeout_s`` (default 10s). On timeout
        we log ``audit.timeout`` and let the asyncio.CancelledError surface
        so the in-flight probe subroutines abort cleanly.

        ZSF: every probe failure routes through ``channel_state.record_failure``
        -- no silent swallow. Final ``finally`` clause ALWAYS clears the
        debounce slot and ticks ``audits_completed`` so a wedged probe
        cannot strand the peer.
        """
        t0 = time.time()
        results: dict = {}
        try:
            # Wrap the whole probe sequence in a single timeout. If we
            # exceed _audit_timeout_s, wait_for raises TimeoutError and
            # we record the partial results.
            results = await asyncio.wait_for(
                self._probe_all_channels(peer_id),
                timeout=self._audit_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"audit.timeout peer={peer_id} reason={reason} "
                f"elapsed_s={self._audit_timeout_s:.1f}"
            )
            self._nerve._stats["audits_timed_out"] = (
                self._nerve._stats.get("audits_timed_out", 0) + 1
            )
        except asyncio.CancelledError:
            # Task was cancelled (e.g. daemon shutdown) -- surface it.
            logger.info(f"audit.cancelled peer={peer_id} reason={reason}")
            raise
        except Exception as e:
            # ZSF: observable, counted, not swallowed.
            logger.warning(
                f"audit.error peer={peer_id} reason={reason} error={e}"
            )
            self._nerve._stats["audit_errors"] = (
                self._nerve._stats.get("audit_errors", 0) + 1
            )
        finally:
            elapsed_ms = int((time.time() - t0) * 1000)
            logger.info(
                f"audit.complete peer={peer_id} reason={reason} "
                f"elapsed_ms={elapsed_ms} results={results}"
            )
            self._nerve._stats["audits_completed"] = (
                self._nerve._stats.get("audits_completed", 0) + 1
            )
            # ALWAYS clear the debounce slot so the next trigger can proceed.
            self._pending_audits.pop(peer_id, None)

        # If probe revealed broken channels and cooldown has expired,
        # dispatch repair. The repair_with_escalation path enforces
        # its own gate + cooldown, so this is idempotent.
        broken = [ch for ch, ok in results.items() if ok is False]
        if broken:
            action = "restart_nats" if CHANNEL_P1_NATS in broken else "check_connectivity"
            cooldown_key = f"{peer_id}:{action}"
            now = time.time()
            last = self._heal_cooldowns.get(cooldown_key, 0)
            if now - last >= REPAIR_COOLDOWN_S:
                self._heal_cooldowns[cooldown_key] = now
                try:
                    asyncio.create_task(
                        self._nerve.repair_with_escalation(peer_id, broken, action)
                    )
                except RuntimeError as e:
                    # Event loop not running -- observable, not silent.
                    logger.warning(
                        f"audit.repair_dispatch_failed peer={peer_id} "
                        f"broken={broken} error={e}"
                    )

        return results

    async def _probe_all_channels(self, peer_id: str) -> dict:
        """Probe every cascade channel for ``peer_id``. Returns {channel: bool}.

        Channels probed: P1_nats (heartbeat recency), P2_http (/health GET),
        P3_chief (chief reachable), P5_ssh (SSH key auth), P7_git (git
        fetchable). P4_seed and P6_wol are one-way and not probed.

        Each probe has a short individual timeout so the total audit
        stays under ``_audit_timeout_s``. Probe failures record to
        channel_state but do NOT raise -- this keeps the audit survey
        complete even when several channels are broken.
        """
        nerve = self._nerve
        results: dict = {}

        # P1 NATS — heartbeat recency check (free, no network hop)
        hb = nerve._peers.get(peer_id, {})
        if hb:
            last_seen = time.time() - hb.get("_received_at", 0)
            if last_seen < 45:
                self.channel_state.record_success(peer_id, CHANNEL_P1_NATS, int(last_seen * 1000))
                results[CHANNEL_P1_NATS] = True
            else:
                self.channel_state.record_failure(
                    peer_id, CHANNEL_P1_NATS, f"stale heartbeat ({int(last_seen)}s)"
                )
                results[CHANNEL_P1_NATS] = False
        else:
            self.channel_state.record_failure(peer_id, CHANNEL_P1_NATS, "no heartbeat received")
            results[CHANNEL_P1_NATS] = False

        # P2 HTTP /health
        peer = nerve._resolve_peer(peer_id)
        ip = peer.get("ip")
        if ip:
            t0 = time.time()
            try:
                import urllib.request
                req = urllib.request.Request(f"http://{ip}:{FLEET_DAEMON_PORT}/health", method="GET")
                # urlopen is blocking -- run in executor to honour audit timeout.
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(
                    None, lambda: urllib.request.urlopen(req, timeout=2)
                )
                if resp.status == 200:
                    self.channel_state.record_success(
                        peer_id, CHANNEL_P2_HTTP, int((time.time() - t0) * 1000)
                    )
                    results[CHANNEL_P2_HTTP] = True
                else:
                    self.channel_state.record_failure(
                        peer_id, CHANNEL_P2_HTTP, f"status {resp.status}"
                    )
                    results[CHANNEL_P2_HTTP] = False
            except (OSError, ValueError) as e:
                self.channel_state.record_failure(peer_id, CHANNEL_P2_HTTP, str(e))
                results[CHANNEL_P2_HTTP] = False
        else:
            results[CHANNEL_P2_HTTP] = False

        # P3 chief — chief reachable
        if nerve._chief and nerve.node_id != nerve._chief:
            chief = nerve._resolve_peer(nerve._chief)
            chief_ip = chief.get("ip")
            if chief_ip:
                t0 = time.time()
                try:
                    import urllib.request
                    req = urllib.request.Request(f"http://{chief_ip}:8844/health", method="GET")
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None, lambda: urllib.request.urlopen(req, timeout=2)
                    )
                    self.channel_state.record_success(
                        peer_id, CHANNEL_P3_CHIEF, int((time.time() - t0) * 1000)
                    )
                    results[CHANNEL_P3_CHIEF] = True
                except (OSError, ValueError) as e:
                    self.channel_state.record_failure(peer_id, CHANNEL_P3_CHIEF, str(e))
                    results[CHANNEL_P3_CHIEF] = False
            else:
                results[CHANNEL_P3_CHIEF] = False
        else:
            # No chief or we ARE the chief -- channel N/A.
            results[CHANNEL_P3_CHIEF] = None

        # P5 SSH — a lightweight key-auth probe via BatchMode=yes,
        # ConnectTimeout=3. We just test whether SSH would succeed,
        # without executing any remote command.
        ssh_user = peer.get("ssh_user") or peer.get("user")
        if ip and ssh_user:
            t0 = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=3",
                    "-o", "StrictHostKeyChecking=accept-new",
                    f"{ssh_user}@{ip}",
                    "true",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await asyncio.wait_for(proc.wait(), timeout=4)
                if rc == 0:
                    self.channel_state.record_success(
                        peer_id, CHANNEL_P5_SSH, int((time.time() - t0) * 1000)
                    )
                    results[CHANNEL_P5_SSH] = True
                else:
                    self.channel_state.record_failure(
                        peer_id, CHANNEL_P5_SSH, f"ssh exit={rc}"
                    )
                    results[CHANNEL_P5_SSH] = False
            except (asyncio.TimeoutError, OSError, FileNotFoundError) as e:
                self.channel_state.record_failure(peer_id, CHANNEL_P5_SSH, str(e))
                results[CHANNEL_P5_SSH] = False
        else:
            results[CHANNEL_P5_SSH] = None

        # P7 git — can we fetch from origin? Proxy: assume origin reachable
        # when a fast ls-remote succeeds. This is shared across peers so
        # we only probe once per audit (record against the peer anyway).
        t0 = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "ls-remote", "--exit-code", "origin", "HEAD",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await asyncio.wait_for(proc.wait(), timeout=4)
            if rc == 0:
                self.channel_state.record_success(
                    peer_id, CHANNEL_P7_GIT, int((time.time() - t0) * 1000)
                )
                results[CHANNEL_P7_GIT] = True
            else:
                self.channel_state.record_failure(
                    peer_id, CHANNEL_P7_GIT, f"git ls-remote exit={rc}"
                )
                results[CHANNEL_P7_GIT] = False
        except (asyncio.TimeoutError, OSError, FileNotFoundError) as e:
            self.channel_state.record_failure(peer_id, CHANNEL_P7_GIT, str(e))
            results[CHANNEL_P7_GIT] = False

        return results

    def get_channel_state(self) -> dict:
        """Return full channel state for all peers (for /channels endpoint)."""
        return self.channel_state.get_all()


class FleetNerveNATS:
    """NATS-based fleet communication daemon."""

    def __init__(self, node_id: str = NODE_ID, nats_url: str = NATS_URL):
        self.node_id = node_id
        self.nats_url = nats_url
        self.nc: Optional[nats.NATS] = None
        self.js = None  # JetStream context
        self._loop = None  # Set in serve() for HTTP handler threads
        # Per-stream JetStream circuit breaker state (CLOSED/OPEN/HALF_OPEN).
        # Prevents repeated 3.0s stream_info timeouts from blocking /health.
        # Keys are stream names; each entry: {state, failures, open_until, backoff_s}
        self._js_cb: dict = {}
        # Counter snapshot buffer for /health counter_rates section (90-min window).
        # Pre-init prevents AttributeError when _build_counter_rates fires before
        # the first snapshot append (regression flagged in Discord audit 2026-04-25).
        self._counter_snapshots: list = []
        self._last_counter_snapshot_ts: float = 0.0
        self._stats = {
            "received": 0, "sent": 0, "broadcasts": 0,
            "errors": 0, "started_at": time.time(),
            "last_restart_reason": None,  # Fix B: set before sys.exit for post-mortem
            "gates_blocked": 0,  # Count of send/repair ops blocked by invariance gates
            "validation_rejected": 0,  # Schema validation rejections (pre-gate, not errors)
            "p0_discord_sends": 0,  # P0 Discord opt-in last-resort sends (gated, observable)
            # ContextDNA Claude Bridge — observability for Aaron's quota survival
            "bridge_anthropic_ok": 0,        # /v1/messages: Anthropic upstream returned 200
            "bridge_fallback_to_deepseek": 0,  # /v1/messages: Anthropic 429/5xx → fell to DeepSeek
            "bridge_anthropic_skipped": 0,   # /v1/messages: no key, went straight to DeepSeek
            "bridge_anthropic_passthrough_errors": 0,  # /v1/messages: Anthropic 4xx (non-429) — surfaced as-is
            "bridge_deepseek_failures": 0,   # /v1/messages: both Anthropic AND DeepSeek failed
            # Anthropic ratelimit gauges (Cycle 4 / D2). -1 = "never observed"
            # (0 means "exhausted" — preserved as a distinct alarm signal).
            # Updated by _record_anthropic_ratelimit_headers via /metrics + /health
            # overlay; not mutated directly here so this is a baseline only.
            "bridge_ratelimit_requests_remaining": -1,
            "bridge_ratelimit_tokens_remaining": -1,
            "bridge_ratelimit_input_tokens_remaining": -1,
            "bridge_ratelimit_output_tokens_remaining": -1,
            "bridge_ratelimit_requests_reset_epoch": -1,
            "bridge_ratelimit_tokens_reset_epoch": -1,
            "bridge_ratelimit_last_429_epoch": -1,
            "bridge_ratelimit_last_retry_after_seconds": -1,
            "bridge_ratelimit_parse_errors": 0,  # ZSF observability counter
            # /v1/chat/completions (OpenAI shim) — used by 3-Surgeons cardiologist
            # when THREE_SURGEONS_VIA_BRIDGE=1. Same Anthropic→DeepSeek fallback
            # ladder as /v1/messages but with OpenAI request/response translation
            # so 3s LLMProvider sees a normal OpenAI-compatible /chat/completions.
            "bridge_chat_completions_requests": 0,    # all incoming OpenAI-shim calls
            "bridge_chat_completions_anthropic_ok": 0,    # Anthropic upstream answered (full quality)
            "bridge_chat_completions_fallback_to_deepseek": 0,  # Anthropic 429/5xx → DeepSeek
            "bridge_chat_completions_skipped": 0,     # no Anthropic key — straight to DeepSeek
            "bridge_chat_completions_failures": 0,    # both upstreams failed
            # WaveC — 3rd-tier Superset fallback (BBB3/CCC3 contract).
            # _try_superset_fallback bumps these directly via the stats dict
            # so /health surfaces rate-limit-resilience observability.
            "bridge_superset_fallback_ok": 0,         # Superset answered after DS+Anthropic failed
            "bridge_superset_fallback_errors": 0,     # Superset unavailable / no active workspace
            # WaveL — cross-node peer Superset failover (mac1 -> mac3 -> mac2).
            "bridge_superset_peer_failover_attempts": 0,
            "bridge_superset_peer_failover_ok": 0,
            "bridge_superset_peer_failover_errors": 0,
            "bridge_token_overflow_to_deepseek": 0,   # >180K tokens skip Anthropic (gate)
            "bridge_deepseek_skipped": 0,             # DS short-circuited by gate / config
            # OAuth Bearer pass-through (Pro/Max users — Aaron). Gated by
            # BRIDGE_OAUTH_PASSTHROUGH=1. attempted = bearer detected + opt-in;
            # success = upstream returned 200; failed = upstream non-200 / error.
            "bridge_oauth_passthrough_attempted": 0,
            "bridge_oauth_passthrough_success": 0,
            "bridge_oauth_passthrough_failed": 0,
            # G1 — Anthropic tool_use <-> OpenAI function-call translation
            # observability (DeepSeek fallback path for tool-using sessions).
            # attempted = request had tools/tool_choice + we hit DeepSeek path.
            # success = full round-trip ended with valid Anthropic response.
            # failed = translation or DeepSeek tool call raised / returned non-200.
            # calls_translated_count = sum of OpenAI tool_calls -> tool_use blocks
            # actually emitted to clients (proves end-to-end fidelity, not just attempt).
            "bridge_tool_translate_attempted": 0,
            "bridge_tool_translate_success": 0,
            "bridge_tool_translate_failed": 0,
            "bridge_tool_calls_translated_count": 0,
            # Cycle 8 H3 — 3-Surgeons consult on G1 flagged the three
            # silently-dropped Anthropic features as the #1 integrity gap.
            # ZSF: surface them as observable counters + one-line log per hit
            # so Atlas can quantify the degradation cost in real sessions
            # before deciding whether to invest in full fidelity later.
            "bridge_tool_reasoner_to_chat_downgrades": 0,   # deepseek-reasoner asked, served by deepseek-chat (tool support)
            "bridge_tool_disable_parallel_dropped": 0,      # disable_parallel_tool_use=true silently ignored
            "bridge_tool_cache_control_dropped": 0,         # cache_control on tool defs silently ignored
            # Cycle 6 F5 — /sprint-status endpoint usage counter. Bumps on
            # every successful HTML render so we can spot whether the
            # consolidated sprint-review page is actually being consulted
            # (or quietly rotting). Reset on daemon restart like all
            # _stats counters; whitelisted in _build_prometheus_metrics.
            "sprint_status_views": 0,
            # ZSF-hardening counters (surface silent failures in hot paths).
            # See fix/daemon-zsf-hardening: bare-excepts narrowed to observable counters.
            "ack_handler_errors": 0,      # _handle_ack: malformed ACK or tracker mutation error
            "ack_send_errors": 0,         # _send_ack: NATS publish of ACK failed
            "repair_gate_health_errors": 0,  # repair_with_escalation: ch_health build threw
            "gate_discord_publish_errors": 0,  # ZSF (race/b1): Discord outbound publish
                                              # failed during gate-block / death-alert
                                              # Discord notify — gate/repair path still
                                              # proceeds, counter surfaces Discord outage
            "death_alert_discord_errors": 0,  # ZSF (race/b1): watchdog's death-alert
                                              # Discord publish failed (daemon exiting anyway)
            # JetStream circuit breaker (EEE3) — counts each CLOSED→OPEN transition.
            # Non-zero means repeated stream_info timeouts; investigate quorum.
            "jetstream_circuit_breaker_opens_total": 0,
            # Zero-polling observability (FLEET_ZERO_POLLING=1).
            # When the legacy 60s probe loop is replaced by event callbacks, these
            # counters prove the callbacks are catching peer failures. If both stay
            # at 0 while peers actually fail, the callback wiring is broken.
            "peer_failures_detected_via_callback": 0,
            "peer_failures_detected_via_no_responders": 0,
            "heartbeats_skipped_due_to_traffic": 0,  # set by multifleet_heartbeat (when adaptive)
            # B4 — Broadcast fanout metrics. broadcast_method tracked via
            # distinct counters so Prometheus-style scrape sees labels.
            "broadcast_method_nats": 0,        # successful O(1) NATS broadcasts
            "broadcast_method_http": 0,        # HTTP-fanout fallback broadcasts
            "broadcast_peer_count": 0,          # peer count of most-recent broadcast
            "broadcast_http_peer_failures": 0,  # individual HTTP peer failures (fanout)
            "broadcast_duplicates_dropped": 0,  # replay-window drops on receive
            # RACE Q4 — NATS bandwidth + message-size telemetry.
            # bytes_{sent,received} are monotonic counters (Prometheus-friendly).
            # largest_msg_bytes is a high-water gauge; large_msg_count counts any
            # message > MSG_SIZE_WARN_BYTES (default 100 KB) so bandwidth-planners
            # can spot fat-tail traffic at 100+ node scale. p95 is computed over
            # the rolling window self._msg_size_window (deque, see init below).
            "bytes_sent": 0,
            "bytes_received": 0,
            "largest_msg_bytes": 0,
            "large_msg_count": 0,
            # RACE R3 — JetStream replay-on-reconnect.
            # Counts messages drained from durable consumers when the daemon
            # reconnects to NATS. Anything > 0 here proves the durable cursor
            # caught a real disconnect-window gap and the message would have
            # been silently lost on plain pub/sub. Always present so /health
            # exposes 0 even before the first reconnect.
            "messages_replayed_on_reconnect": 0,
            "replay_on_reconnect_runs": 0,      # # of reconnect cycles that ran replay
            "replay_on_reconnect_errors": 0,    # exceptions raised by replay path
            # ROUND-3 C1 — NATS resubscribe / reconnect-loop visibility.
            # ``nats_resubscribe_total`` increments each time _subscribe_all
            # completes after reconnect or the connect-retry loop. ZSF: must
            # grow for /health.subscription_count to grow. ``nats_connect_retry_total``
            # increments each time the retry loop attempts ``connect()`` because
            # ``self.nc`` is None or closed -- proves the loop ran when initial
            # connect failed and we landed in HTTP-only mode.
            "nats_resubscribe_total": 0,
            "nats_connect_retry_total": 0,
            "nats_connect_retry_errors": 0,
            # WW1 (2026-05-12) — offsite-NATS watchdog. Detects the
            # "daemon floated to a peer's NATS server" failure mode: libnats
            # picks a non-local URL from the cluster INFO discovered_servers
            # pool on reconnect, the subscription lives on the wrong server,
            # and the local publisher's events are silently dropped because
            # NATS cluster interest only propagates one hop. Counters surface
            # the detection (`detected`) and the corrective reconnect attempt
            # (`reconnect_total` / `reconnect_errors`). ZSF.
            #
            # WW7-A restore (2026-05-12): re-instated after commit 68bc82435
            # ("chore(merge): resolve stale UU/AA conflicts") wiped the WW1
            # patch via `git checkout --theirs`. See
            # .fleet/audits/2026-05-12-WW7-A-webhook-silence-fix.md.
            "webhook_offsite_nats_detected": 0,
            "webhook_offsite_nats_reconnect_total": 0,
            "webhook_offsite_nats_reconnect_errors": 0,
            # WEBHOOK #1 PRIORITY (CLAUDE.md) — preventive watchdog. Detects
            # "connected but zero subs" edge case where nc.is_connected==True
            # but len(nc._subs)==0 (libnats reconnect race). Without this,
            # webhook events are silently lost: producer publishes fine, daemon
            # has no subscriber, /health.subscription_count stays 0 even though
            # the connect-retry loop sees a healthy connection.
            "webhook_subscription_resubscribe_attempts_total": 0,
            "webhook_subscription_resubscribe_failures_total": 0,
            # RACE S5 — webhook composite-latency budget alerts.
            # Monotonic count of event.webhook.budget_exceeded.<node> events
            # received since daemon start. Mirrors the per-aggregator counter
            # so Prometheus-style scrapers find it under /health._stats too.
            "webhook_budget_exceeded_count": 0,
            # WaveJ 2026-05-12 — daemon /message wedge root-cause fix.
            # Counts blocking subprocess calls offloaded to a worker thread
            # via asyncio.to_thread so the asyncio main loop stays
            # responsive under concurrent /message load. Before this fix
            # P5_ssh / P7_git ran subprocess.run() directly inside async
            # coroutines and blocked the entire event loop for 8-30s,
            # starving sibling /message handlers + the NATS connect-retry
            # loop. ZSF: every offload bumps offloaded_total; every
            # offload exception bumps offload_errors. See audit
            # .fleet/audits/2026-05-12-WaveJ-daemon-wedge-root-cause.md.
            "subprocess_offloaded_total": 0,
            "subprocess_offload_errors": 0,
            # VV3-B INV-CapKVFreshness — stale capability doc detection.
            # Increments each time the watchdog check raised an exception (ZSF).
            "cap_kv_freshness_check_errors": 0,
            # WW11-AC (2026-05-12) — cross-node SSH peer reachability recovery.
            # ssh_recover_attempts_total: # times recover-peer-reachability.sh was invoked.
            # ssh_recover_success_total:  # times the script exited 0 (peer came back).
            # ZSF: subprocess errors are caught; both counters always present.
            "ssh_recover_attempts_total": 0,
            "ssh_recover_success_total": 0,
        }
        # Rolling window of recent message sizes (bytes). Bounded so the
        # daemon's RSS doesn't grow with traffic. Reuses race/b6 deque pattern
        # (see ChannelStateTracker.window). p95 computed on demand by
        # _compute_msg_size_p95() — cheap (sort O(N log N), N=512).
        self._msg_size_window: "deque[int]" = deque(maxlen=MSG_SIZE_WINDOW)
        # VV3-B INV-CapKVFreshness — last_publish_ts per peer (float epoch seconds).
        # Updated by any path that learns a peer's capability doc was refreshed.
        # Consumed by _watchdog_check_cap_kv_freshness() every 30s watchdog tick.
        self._peer_cap_publish_ts: dict[str, float] = {}
        # Most-recent stale-peers snapshot (peer_id -> {stale_hours, action}).
        # Written by _watchdog_check_cap_kv_freshness(); read by _build_health().
        self._cap_kv_stale_peers: dict[str, dict] = {}
        # Invariance gates (None if module unavailable -- slim fleet installs)
        self._repair_gate = FleetRepairGate() if FleetRepairGate is not None else None
        self._send_gate = FleetSendGate() if FleetSendGate is not None else None
        # Delegation receiver (chief→worker task hand-off). Only activated on
        # workers; chief does NOT listen to its own delegations.
        self._delegation_receiver = None
        self._delegation_stats_cache = {}  # Last snapshot for /health
        self._delegation_queue_dir = Path.home() / ".multifleet" / "delegations"
        # Delegation runner (FSEvents-driven claude-code spawner). Ships
        # dormant — set DELEGATION_RUNNER_ENABLED=1 to activate. Chief may
        # also spawn its own tasks, so this is not gated by worker-only.
        self._delegation_runner = None
        # Unified event bus — quorum-based repair + upgrade triggers
        self._event_bus = None  # dict from startup_event_bus() or None
        self._state_sync = None  # FleetStateSync instance or None
        # RACE N2 — webhook health aggregator (cross-process bridge via NATS).
        # Subscribes to event.webhook.completed.<node> and surfaces a summary
        # under /health.webhook so operators see webhook generation health
        # without grepping logs. Always present so the response shape is
        # stable; events_recorded=0 means no webhook has fired yet.
        try:
            from tools.webhook_health_bridge import WebhookHealthAggregator
            self._webhook_health = WebhookHealthAggregator(self.node_id)
        except Exception as e:  # noqa: BLE001 — bridge optional, daemon must boot
            logger.warning(f"WebhookHealthAggregator init failed: {e}")
            self._webhook_health = None
        # RACE S5 — webhook composite-latency budget aggregator. Subscribes to
        # event.webhook.budget_exceeded.<node> and surfaces a 1h rolling
        # count under /health.webhook.budget_exceeded_recent so latency
        # regressions are observable without grepping logs.
        try:
            from tools.webhook_health_bridge import WebhookBudgetAggregator
            self._webhook_budget = WebhookBudgetAggregator(self.node_id)
        except Exception as e:  # noqa: BLE001 — bridge optional, daemon must boot
            logger.warning(f"WebhookBudgetAggregator init failed: {e}")
            self._webhook_budget = None
        # RACE V1 — superpowers phase aggregator. Subscribes to
        # event.superpowers.phase.* (wildcard, so we follow every fleet
        # node) and surfaces last-known phase per peer at
        # /health.fleet_phase_states. Other fleet nodes can poll that to
        # gate on a peer's BLOCKING / EXECUTION state. ZSF: every receive
        # failure increments superpowers_phase_receive_errors.
        try:
            from multifleet.superpowers_phase_bridge import (
                SuperpowersPhaseAggregator,
            )
            self._superpowers_phase = SuperpowersPhaseAggregator(self.node_id)
        except Exception as e:  # noqa: BLE001 — bridge optional, daemon must boot
            logger.warning(f"SuperpowersPhaseAggregator init failed: {e}")
            self._superpowers_phase = None
        # Fix B: error-rate tracking for self-kill watchdog.
        # Rolling 60s window; if errors exceed threshold we exit(2) so launchd respawns clean
        # rather than letting the daemon rot in a degraded state (observed: 1672 errors over
        # 92h uptime with no self-recovery, mac2 stuck rate-limit-loop for 4 days).
        self._error_timestamps: list = []  # monotonic timestamps of recent errors
        self._error_rate_threshold = int(os.environ.get("FLEET_ERROR_RATE_THRESHOLD", "100"))  # errors per 60s
        self._peers = {}  # node_id -> last heartbeat data
        # RACE A4 — replica auto-scale state.
        # `_last_known_peer_count` tracks the live fleet size (active peers +
        # self) observed from heartbeats; a change triggers a debounced
        # rebalance. `_last_replica_rebalance_ts` debounces rebalance calls to
        # at most one every `_replica_rebalance_debounce_s` seconds.
        self._last_known_peer_count: int = 0
        self._last_replica_rebalance_ts: float = 0.0
        self._replica_rebalance_debounce_s: float = float(
            os.environ.get("FLEET_REPLICA_REBALANCE_DEBOUNCE_S", "60")
        )
        self._running = True
        # ACK tracking store
        try:
            from tools.fleet_nerve_store import FleetNerveStore
            self._store = FleetNerveStore()
        except Exception as e:
            logger.warning(f"Store init failed: {e} -- ACK tracking disabled")
            self._store = None
        self._channel_health = {}  # peer -> {channel: "ok"|"broken", ...}
        # RACE A3 — gate cascade reordering behind FLEET_CHANNEL_ORDER env.
        # Missing init before this line caused `'FleetNerveNATS' object has no
        # attribute '_channel_order_mode'` AttributeError on every send call
        # post-bounce (introduced by 5922378d, init was missed).
        # Defaults to "strict" so spec-compliant P0→P7 priority order holds
        # unless operator opts into reliability-based reordering.
        self._channel_order_mode = os.environ.get("FLEET_CHANNEL_ORDER", "strict").lower()
        if self._channel_order_mode not in ("strict", "reliability"):
            logger.warning(
                "FLEET_CHANNEL_ORDER=%r unrecognized — falling back to 'strict'",
                self._channel_order_mode,
            )
            self._channel_order_mode = "strict"
        # Reliability-ranked channel ordering: per-peer per-channel success/fail counters.
        # Shape: {peer_id: {channel_name: {"ok": int, "fail": int, "last_ok_ts": float}}}
        # Used by send_with_fallback to re-order channels by recent reliability.
        self._channel_success_rate: dict = {}
        # ── Inter-node repair (peer-quorum L5 assist) ──
        # self→peer send history: deque of bools (True=delivered). Capped at
        # ASSIST_HEALTH_SAMPLE per peer. Fuels _self_health_score(peer).
        self._send_history: dict = {}          # peer -> list[bool] (most recent last)
        # Per-peer struggle-counter: incremented when _probe_and_heal_peer
        # fails to repair; reset on first success. Triggers publish_struggle
        # when >= STRUGGLE_THRESHOLD.
        self._struggle_counter: dict = {}      # peer -> int
        # Per-peer back-off after seeing another node's claim. Lets us defer
        # to whoever won the election so we don't dogpile.
        self._assist_backoff_until: dict = {}  # peer -> ts
        # Per (target) pending election: {target: {proposer, claim_ts, claim_id}}.
        # Used to detect dueling claims and apply split-brain resolution
        # (oldest claim_ts wins; tie-break on node_id lexicographic sort).
        self._pending_claims: dict = {}
        # Per-assister-per-target rate limit: (assister, target) -> last ts.
        # Enforced both on claim emission and on receive.
        self._assist_rate: dict = {}
        # Set of targets we currently hold an active assist task for.
        self._active_assists: set = set()
        # B4 — Broadcast replay-window prevention.
        # Bounded FIFO of recently-seen broadcast ids (insertion order = age).
        # Incoming `fleet.broadcast.>` messages are dropped when id already
        # present. HMAC validation still runs on receive (via _handle_message).
        self._seen_broadcast_ids: "deque[str]" = deque(maxlen=BROADCAST_SEEN_MAX)
        self._seen_broadcast_set: set = set()
        self._heal_in_flight = {}  # peer -> asyncio.Task (prevent duplicate heals)
        self._pending_acks = {}  # msg_id -> {sent_at, target, retries, acked, event?}
        # K2 2026-05-04 — event-driven ACK tracker. Each pending ack carries
        # an asyncio.Event the ACK-handler sets on arrival; a per-msg awaiter
        # task blocks on it with `wait_for(timeout=ack_timeout_s)` and fires
        # the retry/drop path immediately. The 30s watchdog tick is now a
        # fallback heartbeat (≤1/hour). See
        # `.fleet/audits/2026-05-04-K2-orchestrator-polling-killed.md`.
        self._ack_awaiter_tasks: dict = {}  # msg_id -> asyncio.Task
        self._ack_stats = {
            "ack_event_total": 0,                # ACK arrived before timeout
            "ack_timeout_event_total": 0,        # event-driven timeout fired
            "ack_fallback_heartbeat_total": 0,   # fallback watchdog had to act
            "ack_awaiter_errors": 0,
        }
        self._chains = {}  # chain_id -> {steps: [...], current: 0, status: "running"|"done"|"failed", results: []}
        self._dispatches = {}  # dispatch_id -> {target, prompt, status, sent_at, result}
        self._ack_timeout_s = int(os.environ.get("FLEET_ACK_TIMEOUT_S", "60"))
        self._ack_max_retries = int(os.environ.get("FLEET_ACK_MAX_RETRIES", "3"))
        # Load peer config for SSH/HTTP/WoL channels
        try:
            from tools.fleet_nerve_config import load_peers
            self._peers_config, self._chief = load_peers()
        except Exception:
            self._peers_config, self._chief = {}, None
        self._active_subs: list = []  # Track subs for reconnect dedup
        # RACE R3 — durable JetStream pull subscriptions, one per stream.
        # Created lazily in _ensure_durable_consumers() after JetStream is
        # available. Keyed by stream name (FLEET_MESSAGES, FLEET_EVENTS).
        # On reconnect, _replay_missed_on_reconnect() drains each one via
        # multifleet.jetstream.replay_missed so the disconnect-window gap
        # is closed before the daemon resumes normal operation.
        self._durable_subs: dict = {}
        # Ring buffer of recently received messages (last 200, for /messages/unread
        # polling used by discord_bridge.py and similar fleet->external relays).
        # Import locally to avoid touching the module imports block (Agent 1 owns it).
        from collections import deque as _deque
        self._recent_messages: "_deque[dict]" = _deque(maxlen=200)
        # ── Struggle metrics (transient — peer-assist signal) ──
        # Tracks consecutive failures per channel + last self-heal outcome so
        # /health can advertise needs_peer_assist=True. Reset to 0 on first
        # success; never persisted (restart clears state).
        self._struggle_state: dict = {
            "consecutive_failed_p1_pubs": 0,
            "consecutive_failed_p2_calls": 0,
            "consecutive_failed_health_replies": 0,
            "last_self_heal_attempt_ts": 0.0,
            "last_self_heal_outcome": "none",  # "ok" | "fail" | "none"
            "channel_degraded": [],            # list[str] of degraded channels
        }
        # Peer-assist coordinator (wired in serve() once NATS is up).
        # Tracks last-observed needs_peer_assist so struggle-state mutators
        # can detect the False→True transition and broadcast rpc.peer.struggling.
        self._peer_assist = None
        self._last_needs_peer_assist = False
        # K1 2026-05-04 — Directed peer-assist (P3+ working ∧ P1/P2 broken
        # → ask peer to probe us back, write repair-request file).
        # Activated in serve(); see tools/peer_assist_directed.py.
        self._peer_assist_directed = None
        # Per-peer rate limiter (OSS blocker #5 -- prevents message floods)
        self.rate_limiter = PeerRateLimiter()
        # Communication protocol (self-healing, channel state tracking)
        self.protocol = CommunicationProtocol(self)
        # MFINV-C01 / WW2 mac3 gap #4 (restored EEE3): register as the
        # channel_priority default so script-callers
        # (multifleet.channel_priority.send) reuse the daemon's protocol
        # instead of building a fresh one. ZSF — failure logs, does not
        # abort daemon startup.
        try:
            from multifleet import channel_priority as _cp
            _cp.set_default_protocol(self.protocol)
        except Exception as e:  # noqa: BLE001 — ZSF
            logger.warning(f"channel_priority.set_default_protocol failed: {e}")
        # EEE3 — wire the cascade-event publisher so OK↔broken transitions
        # announce themselves over P1 NATS. The daemon's ChannelState lives
        # under self.protocol.channel_state; we set the publisher AFTER
        # serve() has nc connected (see serve() init block).
        # ── EEE3 cascade-relay enable flag (memoized snapshot, env-overridable)
        self._cascade_peer_relay_enabled = bool(
            os.environ.get("FLEET_PEER_RELAY_ENABLED", "0") in ("1", "true", "TRUE", "yes")
        )
        # Cooldown bookkeeping for self-repair triggered by peer-degraded events.
        # Prevents flap: a peer reporting our P2 as broken every 30s would
        # otherwise schedule unbounded repairs. Keyed by channel.
        self._cascade_self_repair_cooldowns: dict = {}
        # Rebuttal protocol (cross-machine preserved dissent)
        try:
            from multifleet.rebuttal_protocol import RebuttalProtocol
            self._rebuttal = RebuttalProtocol(
                node_id=self.node_id,
                nats_publish=self._sync_nats_publish,
            )
        except Exception as e:
            logger.warning(f"RebuttalProtocol init failed: {e} -- rebuttals disabled")
            self._rebuttal = None

    def _sync_nats_publish(self, subject: str, payload: dict):
        """Synchronous NATS publish wrapper for RebuttalProtocol callback."""
        if not self.nc or not self.nc.is_connected:
            return
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self.nc.publish(subject, json.dumps(payload).encode()),
                self._loop,
            )

    def _sync_nats_publish_bytes(self, subject: str, data: bytes) -> bool:
        """Synchronous NATS publish for raw bytes — used by cascade events.

        Returns ``True`` if the publish was scheduled, ``False`` otherwise.
        Never raises (ZSF — caller already counts errors).
        """
        if not self.nc or not getattr(self.nc, "is_connected", False):
            return False
        if not self._loop:
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                self.nc.publish(subject, data),
                self._loop,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — ZSF
            logger.debug(f"_sync_nats_publish_bytes failed for {subject}: {exc}")
            return False

    async def connect(self):
        """Connect to NATS server with auto-reconnect.

        CV-001: Supports token auth via NATS_TOKEN env var or config.
        If set, all connections are authenticated. If not, connects without
        auth (backward-compatible for LAN-only fleets).
        """
        connect_kwargs = dict(
            servers=[self.nats_url],
            name=self.node_id,
            reconnected_cb=self._on_reconnect,
            disconnected_cb=self._on_disconnect,
            error_cb=self._on_error,
            max_reconnect_attempts=-1,  # infinite reconnect
            reconnect_time_wait=2,
        )
        # Token auth: NATS_TOKEN env var or .multifleet/config.json natsToken
        nats_token = os.environ.get("NATS_TOKEN")
        if not nats_token:
            try:
                cfg_path = REPO_ROOT / ".multifleet" / "config.json"
                if cfg_path.exists():
                    cfg = json.loads(cfg_path.read_text())
                    nats_token = cfg.get("protocol", {}).get("natsToken")
            except (OSError, ValueError, AttributeError) as e:
                logger.debug(f"NATS token config load skipped: {e}")
        if nats_token:
            connect_kwargs["token"] = nats_token
            logger.info("NATS auth: using token")
        else:
            logger.warning("NATS auth: no token configured (NATS_TOKEN env or config.protocol.natsToken) -- LAN-only mode")

        # Leaf node support (gated — requires NATS_LEAF_URL env var set)
        # Full wiring deferred until Aaron provisions a public broker.
        # See docs/plans/2026-04-23-nats-leaf-node-plan.md
        leaf_url = os.environ.get("NATS_LEAF_URL")
        if leaf_url:
            logger.info(f"Leaf node: connecting outbound to {leaf_url}")
            # Connection pattern: add to servers list with special creds
            # (Creds loaded from macOS Keychain item NATS_LEAF_CREDS — TODO)

        self.nc = await nats.connect(**connect_kwargs)
        logger.info(f"Connected to NATS at {self.nats_url} as {self.node_id}")

        # Set up JetStream via multifleet.jetstream.ensure_streams (blocker #2)
        # Uses the module's idempotent stream creation + proper error handling
        # (handles ServiceUnavailableError by retrying after brief wait)
        self.js = None
        try:
            from multifleet.jetstream import ensure_streams
            # Brief delay lets NATS JetStream finish initializing after server start
            await asyncio.sleep(0.5)
            # RACE A4: pass configured fleet size so num_replicas auto-scales
            # (capped at 5). Heartbeats later call _maybe_rebalance_replicas
            # when the live peer count drifts from this initial estimate.
            initial_peer_count = max(1, len(self._peers_config or {}))
            self.js = await ensure_streams(self.nc, peer_count=initial_peer_count)
            if self.js is None:
                logger.info("JetStream not available on server — pub/sub-only mode")
            else:
                # RACE N3: verify each stream's replica set actually contains
                # every configured peer. Catches the "mac1 missing from
                # FLEET_EVENTS" failure mode where a daemon restart left a
                # peer outside the replica list. Auto-rebalance is gated
                # behind JETSTREAM_AUTO_REBALANCE_ON_GAP=1 (default off so
                # the diagnostic-only path ships without behavior change).
                try:
                    from multifleet.jetstream import _verify_replica_membership
                    expected_peers = list((self._peers_config or {}).keys())
                    if self.node_id and self.node_id not in expected_peers:
                        expected_peers.append(self.node_id)
                    if expected_peers:
                        await _verify_replica_membership(
                            self.js,
                            expected_peers=expected_peers,
                            observability=self._on_jetstream_replica_gap,
                        )
                except Exception as e:
                    self._stats["jetstream_verify_errors"] = (
                        self._stats.get("jetstream_verify_errors", 0) + 1
                    )
                    logger.warning(f"JetStream replica verify failed: {e}")

                # RACE R3: create durable pull consumers per stream so the
                # daemon owns a per-node cursor on FLEET_MESSAGES and
                # FLEET_EVENTS. Cursor advances as messages are acked during
                # replay; on reconnect we drain whatever the server queued
                # while we were offline. Soft-fail — durable consumer
                # unavailable just means no replay-on-reconnect, plain
                # pub/sub still works.
                await self._ensure_durable_consumers()
        except ImportError:
            # Fallback: legacy inline setup if multifleet package not on path
            try:
                self.js = self.nc.jetstream()
                await self.js.find_stream_name_by_subject("fleet.>")
                logger.info("JetStream stream FLEET found (legacy path)")
            except Exception:
                try:
                    await self.js.add_stream(
                        name=STREAM_NAME,
                        subjects=["fleet.>"],
                        retention="limits",
                        max_age=24 * 3600,
                        storage="file",
                    )
                    logger.info("JetStream stream FLEET created (legacy path)")
                except Exception as e:
                    logger.warning(f"JetStream setup failed (non-fatal, pub/sub still works): {e}")
                    self.js = None
        except Exception as e:
            logger.warning(f"JetStream setup failed (non-fatal, pub/sub still works): {e}")
            self.js = None

    def _on_jetstream_replica_gap(self, event: dict) -> None:
        """RACE N3 observability hook for replica-membership drift.

        Called by ``_verify_replica_membership`` whenever a configured
        peer is missing from a stream's replica set. Bumps a counter,
        logs a warning, and pushes an event onto the bus when present
        so dashboards / fleet-check can surface the drift without
        polling the JetStream API directly.
        """
        try:
            stream = event.get("stream", "?")
            missing = event.get("missing", [])
            self._stats["jetstream_replica_gaps_detected"] = (
                self._stats.get("jetstream_replica_gaps_detected", 0) + 1
            )
            logger.warning(
                f"jetstream replica gap: stream={stream} missing={missing} "
                f"replica_set={event.get('replica_set')} leader={event.get('leader')}"
            )
            if self._event_bus is not None:
                try:
                    pub = self._event_bus.get("publisher")
                    if pub is not None and hasattr(pub, "publish"):
                        pub.publish("jetstream.replica_gap", event)
                except Exception as e:
                    self._stats["jetstream_replica_gap_publish_errors"] = (
                        self._stats.get("jetstream_replica_gap_publish_errors", 0) + 1
                    )
                    logger.debug(f"replica_gap event publish failed: {e}")
        except Exception as e:
            # ZSF: never silently swallow — count + log.
            self._stats["jetstream_replica_gap_hook_errors"] = (
                self._stats.get("jetstream_replica_gap_hook_errors", 0) + 1
            )
            logger.warning(f"replica_gap hook error: {e}")

    async def _ensure_durable_consumers(self) -> None:
        """RACE R3 — create per-node durable pull consumers for replay.

        Creates one durable consumer per stream (FLEET_MESSAGES + FLEET_EVENTS)
        named ``{stream}_{node_id}`` (race/n3 pattern). Cursor is server-side
        so replay starts from the last acked seq, not the beginning of the
        stream. Soft-fail: on any error this method just leaves
        ``self._durable_subs`` partial / empty and the daemon continues
        without replay capability. Idempotent — safe to call repeatedly.
        """
        if self.js is None:
            return
        try:
            from multifleet.jetstream import (
                STREAM_MESSAGES,
                STREAM_EVENTS,
                STREAM_SUBJECTS,
                create_durable_consumer,
                durable_consumer_name,
            )
        except ImportError as e:
            logger.debug(f"durable consumer setup skipped (no multifleet): {e}")
            return

        targets = [
            (STREAM_MESSAGES, STREAM_SUBJECTS.get(STREAM_MESSAGES, ["fleet.>"])[0]),
            (STREAM_EVENTS, STREAM_SUBJECTS.get(STREAM_EVENTS, ["event.>"])[0]),
        ]
        for stream, subject in targets:
            if stream in self._durable_subs and self._durable_subs[stream] is not None:
                continue  # already established
            durable = durable_consumer_name(stream, self.node_id)
            try:
                sub = await create_durable_consumer(
                    self.js,
                    stream=stream,
                    durable_name=durable,
                    subject=subject,
                    deliver_policy="new",
                )
            except Exception as e:  # noqa: BLE001 — counted, daemon continues
                self._stats["replay_on_reconnect_errors"] = (
                    self._stats.get("replay_on_reconnect_errors", 0) + 1
                )
                logger.warning(
                    f"durable consumer create failed stream={stream}: {e}"
                )
                sub = None
            self._durable_subs[stream] = sub
            if sub is not None:
                logger.info(
                    f"RACE R3: durable consumer ready stream={stream} durable={durable}"
                )

    async def _replay_missed_on_reconnect(self) -> int:
        """RACE R3 — drain durable consumers via ``replay_missed``.

        Each fetched message is routed through ``_handle_message`` (the
        normal pub/sub path) so validation, dedup, HMAC, ack handling and
        stats stay consistent. Returns the total replayed count and
        increments ``messages_replayed_on_reconnect``.

        Idempotent and safe to call when no consumers exist. Used by
        ``_on_reconnect``. Tested via test_replay_on_reconnect.py.
        """
        if not self._durable_subs:
            # Try late-create — JetStream may have come up after the first
            # connect race condition. Cheap if it can't, no-op if it works.
            await self._ensure_durable_consumers()
        if not self._durable_subs:
            return 0
        try:
            from multifleet.jetstream import replay_missed
        except ImportError:
            return 0

        self._stats["replay_on_reconnect_runs"] = (
            self._stats.get("replay_on_reconnect_runs", 0) + 1
        )
        total = 0
        for stream, sub in list(self._durable_subs.items()):
            if sub is None:
                continue
            try:
                count = await replay_missed(sub, self._handle_message)
            except Exception as e:  # noqa: BLE001 — counted, continue with others
                self._stats["replay_on_reconnect_errors"] = (
                    self._stats.get("replay_on_reconnect_errors", 0) + 1
                )
                logger.warning(f"replay_missed failed for {stream}: {e}")
                continue
            if count:
                logger.info(
                    f"RACE R3: replayed {count} message(s) from {stream} "
                    f"after reconnect"
                )
            total += count
        if total:
            self._stats["messages_replayed_on_reconnect"] = (
                self._stats.get("messages_replayed_on_reconnect", 0) + total
            )
        return total

    async def _on_reconnect(self):
        logger.info("Reconnected to NATS — re-subscribing to all subjects")
        # Clear NATS_DISCONNECT signals on reconnect
        if self._event_bus is not None:
            rq = self._event_bus.get("repair_quorum")
            if rq is not None:
                try:
                    from multifleet.fleet_event_bus import RepairSignal
                    for peer_id in list(self._peers_config.keys()):
                        if peer_id == self.node_id:
                            continue
                        rq.clear_signal(peer_id, RepairSignal.NATS_DISCONNECT)
                except Exception:
                    pass
        try:
            await self._subscribe_all()
            logger.info("Re-subscribed to all NATS subjects after reconnect")
        except Exception as e:
            logger.error(f"Failed to re-subscribe after reconnect: {e}")
            self._stats["errors"] += 1

        # RACE R3 — drain JetStream durable consumers so messages received
        # while NATS was disconnected surface through _handle_message before
        # we go back to live processing. ZSF: any failure is counted, never
        # swallowed. The replay path is best-effort — a broken durable
        # consumer must not crash the reconnect handler.
        try:
            await self._replay_missed_on_reconnect()
        except Exception as e:  # noqa: BLE001 — counted via _stats below
            self._stats["replay_on_reconnect_errors"] = (
                self._stats.get("replay_on_reconnect_errors", 0) + 1
            )
            logger.warning(f"JetStream replay-on-reconnect failed: {e}")

    async def _on_disconnect(self):
        logger.warning(f"Disconnected from NATS -- will auto-reconnect")
        # Auto-restart NATS server if it crashed (rate-limited, non-blocking)
        self._trigger_service_restart("nats")
        # Wire NATS_DISCONNECT into repair quorum (all peers suspect)
        if self._event_bus is not None:
            rq = self._event_bus.get("repair_quorum")
            if rq is not None:
                for peer_id in list(self._peers_config.keys()):
                    if peer_id == self.node_id:
                        continue
                    try:
                        from multifleet.fleet_event_bus import RepairSignal
                        rq.record_signal(peer_id, RepairSignal.NATS_DISCONNECT)
                    except Exception:
                        pass
        # Zero-polling: notify all peers as potentially disconnected so the
        # protocol layer can cascade-check via callback rather than wait
        # for the 5min watchdog sanity tick.
        if ZERO_POLLING_ENABLED and hasattr(self, "protocol") and self.protocol is not None:
            for peer_id in list(self._peers_config.keys()):
                if peer_id == self.node_id:
                    continue
                asyncio.ensure_future(
                    self.protocol.on_peer_disconnect(peer_id, reason="nats_disconnect")
                )

    async def _on_error(self, e):
        logger.error(f"NATS error: {e}")
        self._stats["errors"] += 1
        # Zero-polling: if the error is a NoRespondersError attached to an
        # rpc.<peer>.* subject, fire the peer-death callback immediately.
        if ZERO_POLLING_ENABLED and isinstance(e, NoRespondersError):
            subj = getattr(e, "subject", "") or ""
            peer = subj.split(".")[1] if subj.startswith("rpc.") and "." in subj[4:] else ""
            if peer and hasattr(self, "protocol") and self.protocol is not None:
                asyncio.ensure_future(self.protocol.on_no_responders(peer, subj))

    async def request_peer(self, peer_id: str, rpc_name: str, payload: bytes = b"", timeout: float = 2.0):
        """Request-reply a peer via rpc.<peer>.<rpc_name>.

        Zero-polling primitive: NoRespondersError IS the death signal. The NATS
        server knows no subscriber exists and raises synchronously -- no HTTP
        probe needed, no heartbeat-wait needed.

        Returns the reply bytes on success. Raises NoRespondersError when the
        peer is truly gone; raises nats.errors.TimeoutError when the peer is
        subscribed but slow/hung.
        """
        subject = f"rpc.{peer_id}.{rpc_name}"
        try:
            msg = await self.nc.request(subject, payload, timeout=timeout)
            return msg.data
        except NoRespondersError:
            if hasattr(self, "protocol") and self.protocol is not None:
                await self.protocol.on_no_responders(peer_id, subject)
            raise

    def _trigger_service_restart(self, service: str) -> None:
        """Non-blocking auto-restart of a service via ServiceManager."""
        import threading

        def _do():
            try:
                sys.path.insert(0, str(REPO_ROOT / "multi-fleet"))
                from multifleet.service_manager import ServiceManager
                result = ServiceManager.ensure_healthy(service)
                if result.get("now_healthy"):
                    logger.info(f"Auto-restart recovered {service}")
                elif result.get("rate_limited"):
                    logger.debug(f"Auto-restart rate-limited for {service}")
                else:
                    logger.warning(f"Auto-restart failed for {service}: {result.get('error')}")
            except ImportError:
                logger.debug("ServiceManager unavailable — auto-restart skipped")
            except Exception as exc:
                logger.debug(f"Auto-restart trigger failed: {exc}")

        threading.Thread(target=_do, daemon=True).start()

    # ── Message Handlers ──

    async def _handle_message(self, msg):
        """Route incoming messages by subject."""
        try:
            data = json.loads(msg.data.decode())
            subject_parts = msg.subject.split(".")
            msg_type = subject_parts[-1] if len(subject_parts) >= 3 else "message"

            # ── Schema validation (OSS blocker #4) ──
            validation_err = validate_inbound_message(data)
            if validation_err:
                logger.warning(f"NATS message rejected: {validation_err}")
                self._stats["validation_rejected"] += 1
                if msg.reply:
                    await self.nc.publish(msg.reply, json.dumps({
                        "error": validation_err, "valid": False
                    }).encode())
                return

            # Don't process our own broadcasts or ACKs (handled by _handle_ack)
            if data.get("from") == self.node_id:
                return
            if msg_type == "ack":
                return

            self._stats["received"] += 1
            # RACE Q4 — bandwidth telemetry. msg.data is bytes already on
            # the wire; using its length captures the true on-wire payload
            # size (no JSON re-serialization cost).
            try:
                self._record_msg_size("received", len(msg.data))
            except Exception as _q4_err:
                # ZSF: never let telemetry break the receive path.
                self._stats["size_record_errors"] = (
                    self._stats.get("size_record_errors", 0) + 1
                )
                logger.debug(f"Q4 record_msg_size(received) failed: {_q4_err}")
            self._touch_activity()
            sender = data.get("from", "unknown")

            # ── Per-peer rate limiting (OSS blocker #5) ──
            inbound_type = data.get("type", msg_type)
            if not self.rate_limiter.allow(sender, inbound_type):
                logger.warning(f"Rate limited INBOUND {inbound_type} from {sender} -- dropping")
                self._stats["errors"] += 1
                return

            # ── ACK messages: update tracking, do NOT ack back (no infinite loop) ──
            if data.get("type") == "ack":
                ref_id = data.get("ref")
                ack_status = data.get("status", "delivered")
                if ref_id and self._store:
                    self._store.update_ack_status(ref_id, ack_status)
                    logger.info(f"ACK received for {ref_id[:8]}… status={ack_status} from {sender}")
                return

            if msg_type == "health":
                # Request/reply -- respond with our health
                if msg.reply:
                    health = self._build_health()
                    await self.nc.publish(msg.reply, json.dumps(health).encode())
                return

            # Log and inject
            payload = data.get("payload", {})
            subject = payload.get("subject", "no subject")
            body = payload.get("body", "")
            priority = data.get("type", "context")

            logger.info(f"[{priority}] from {sender}: {subject}")

            # Handle repair requests (local-first self-healing)
            # Protocol: nodes ask each other to fix themselves before SSHing in
            # Cooldown: 5min per action to prevent spam (mac3 proposal 2026-04-05)
            if priority in ("sync", "repair") and subject in ("self-heal-request", "repair"):
                try:
                    heal = json.loads(body) if body.startswith("{") else {"action": body}
                    action = heal.get("action", heal.get("suggested_action", ""))
                    reporter = heal.get("reporter", sender)
                    broken = heal.get("broken_channels", [])

                    # Cooldown check -- 5min per action per reporter
                    if not hasattr(self, '_repair_cooldowns'):
                        self._repair_cooldowns = {}
                    cooldown_key = f"{reporter}:{action}"
                    now = time.time()
                    last_attempt = self._repair_cooldowns.get(cooldown_key, 0)
                    if now - last_attempt < 300:  # 5 min cooldown
                        logger.info(f"Repair cooldown: {cooldown_key} ({int(now - last_attempt)}s < 300s)")
                        return

                    logger.info(f"Repair request from {reporter}: action={action}, broken={broken}")

                    SAFE_ACTIONS = {
                        "restart_nats": self._repair_restart_nats,
                        "restart_tunnel": self._repair_restart_tunnel,
                        "git_pull": self._repair_git_pull,
                        "kill_zombie": self._repair_kill_zombie,
                        "restart_daemon": self._repair_restart_daemon,
                    }

                    handler = SAFE_ACTIONS.get(action)
                    if handler:
                        self._repair_cooldowns[cooldown_key] = now
                        logger.info(f"Self-repair: executing {action}")
                        asyncio.create_task(handler(heal))
                    else:
                        # Unknown action -- surface to Claude session
                        logger.warning(f"Unknown repair action '{action}' from {reporter} -- injecting for human review")
                        self._inject_alert(data)
                except Exception as e:
                    logger.warning(f"Repair handler error: {e}")
                return

            # Route rebuttal messages to RebuttalProtocol
            if priority in ("rebuttal_proposal", "rebuttal_dissent", "rebuttal_resolution"):
                if self._rebuttal:
                    result = self._rebuttal.handle_incoming(data)
                    logger.info(f"Rebuttal handled: {result.get('action', '?')} for {data.get('task_id', '?')[:8]}")
                else:
                    logger.warning("Rebuttal message received but protocol not initialized")
                # Still inject as context so the session is aware
                self._inject_context(data)
            # Inject into session based on type
            elif priority == "chain":
                self._handle_chain(data)
            elif priority == "chain_complete":
                # Remote node reports chain step done -- advance the chain
                cid = data.get("_chain_id") or payload.get("chain_id", "")
                ok = payload.get("success", True)
                res = payload.get("result", body)
                self._advance_chain(cid, ok, res)
            elif priority == "alert":
                self._inject_alert(data)
            elif priority == "task":
                self._inject_task(data)
            elif priority == "task_result":
                self._handle_task_result(data)
            else:
                self._inject_context(data)

            # Append to /messages/unread ring buffer (skip self-sourced to avoid echo).
            # Consumed by discord_bridge.py and similar external relays that poll every 5s.
            if sender and sender != self.node_id:
                try:
                    self._recent_messages.append({**data, "_ts": time.time()})
                except Exception:
                    # Never let buffer append break message routing.
                    self._stats["errors"] += 1

            # Reply with ack if requested (request/reply pattern)
            if msg.reply:
                await self.nc.publish(msg.reply, json.dumps({
                    "status": "delivered", "node": self.node_id
                }).encode())

            # Publish ACK back to sender (fire-and-forget delivery confirmation)
            msg_id = data.get("id")
            if msg_id and sender != self.node_id:
                await self._send_ack(sender, msg_id, "delivered", "P1_nats")

        except Exception as e:
            logger.error(f"Message handler error: {e}")
            self._stats["errors"] += 1

    async def _handle_heartbeat(self, msg):
        """Track peer heartbeats. Clears stale repair/channel state on fresh heartbeat.

        ZSF: every failure path increments a counter and logs; no silent swallow.
        Splits the work into independent try/except blocks so one failing
        secondary operation (e.g. protocol.channel_state.record_success) cannot
        block the primary _peers update — that was the heartbeat-stale bug
        observed on mac3 where mac2's _peers entry went 5h stale while mac2 was
        actively publishing.
        """
        # 1) Parse + update _peers (primary operation — must never be blocked
        # by secondary bookkeeping failures).
        try:
            data = json.loads(msg.data.decode())
        except Exception as e:
            self._stats["heartbeat_parse_errors"] = self._stats.get("heartbeat_parse_errors", 0) + 1
            logger.warning(f"heartbeat parse failed: {e}")
            return

        node = data.get("node", "unknown")
        self._stats["peer_heartbeat_received_total"] = self._stats.get("peer_heartbeat_received_total", 0) + 1
        if node == "unknown":
            self._stats["peer_heartbeat_dropped_unknown"] = self._stats.get("peer_heartbeat_dropped_unknown", 0) + 1
            self._stats["peer_heartbeat_dropped_total"] = self._stats.get("peer_heartbeat_dropped_total", 0) + 1
            return
        if node == self.node_id:
            self._stats["peer_heartbeat_dropped_self"] = self._stats.get("peer_heartbeat_dropped_self", 0) + 1
            self._stats["peer_heartbeat_dropped_total"] = self._stats.get("peer_heartbeat_dropped_total", 0) + 1
            return

        # race/a1-immediate-audit: detect recovery BEFORE overwriting the
        # previous _peers snapshot. If the peer was marked broken on any
        # channel and this heartbeat is fresh (lastSeen > 45s stale, or
        # never seen before), a recovery has happened -- re-probe every
        # channel now instead of waiting 30s for the watchdog.
        prev = self._peers.get(node)
        was_stale = True
        if prev is not None:
            was_stale = (time.time() - prev.get("_received_at", 0)) > 45

        self._peers[node] = {**data, "_received_at": time.time()}
        self._stats["peer_heartbeat_processed_total"] = self._stats.get("peer_heartbeat_processed_total", 0) + 1

        # Fire audit only when the heartbeat represents a recovery signal:
        # either we've never seen this peer, or the previous heartbeat was
        # stale. This avoids re-probing every peer on every 15s heartbeat.
        if was_stale:
            try:
                self.trigger_immediate_audit(node, reason="heartbeat_recovery")
            except Exception as e:
                # ZSF: counted, not swallowed. Never block heartbeat processing.
                logger.warning(
                    f"heartbeat audit trigger failed for {node}: {e}"
                )
                self._stats["audit_errors"] = self._stats.get("audit_errors", 0) + 1

        # 2) Clear repair-in-progress lock for this peer (best-effort)
        if hasattr(self, "_repair_in_progress"):
            try:
                self._repair_in_progress.pop(node, None)
            except Exception as e:
                self._stats["heartbeat_repair_clear_errors"] = self._stats.get("heartbeat_repair_clear_errors", 0) + 1
                logger.warning(f"heartbeat repair_in_progress clear failed for {node}: {e}")

        # 3) Notify channel-state tracker (best-effort — prior bare except silently
        # swallowed errors here, starving _peers updates when this throws)
        if hasattr(self, "protocol") and self.protocol:
            try:
                self.protocol.channel_state.record_success(node, "P1_nats", 0)
            except Exception as e:
                self._stats["heartbeat_channel_state_errors"] = self._stats.get("heartbeat_channel_state_errors", 0) + 1
                logger.warning(f"channel_state.record_success failed for {node}: {e}")

        # 4) RACE A4: detect fleet size change and schedule a debounced
        # JetStream replica rebalance. Heartbeat = event trigger (not polling).
        try:
            current_peer_count = len(self._peers) + 1  # +1 for self
            if current_peer_count != self._last_known_peer_count:
                self._last_known_peer_count = current_peer_count
                await self._maybe_rebalance_replicas(current_peer_count)
        except Exception as e:
            self._stats["replica_rebalance_errors"] = (
                self._stats.get("replica_rebalance_errors", 0) + 1
            )
            logger.warning(f"replica rebalance trigger failed: {e}")

    async def _maybe_rebalance_replicas(self, peer_count: int) -> None:
        """Debounced JetStream replica rebalance. RACE A4.

        Called from `_handle_heartbeat` when the active fleet size changes.
        Rate-limited to one call per `_replica_rebalance_debounce_s` seconds
        to avoid churning `update_stream` on flappy heartbeats.

        Counters (surfaced on /health):
          - replica_rebalances: successful ensure_streams calls
          - replica_rebalance_skipped_debounce: debounce suppressions
          - replica_rebalance_errors: exceptions during rebalance
        """
        now = time.time()
        elapsed = now - self._last_replica_rebalance_ts
        if elapsed < self._replica_rebalance_debounce_s:
            self._stats["replica_rebalance_skipped_debounce"] = (
                self._stats.get("replica_rebalance_skipped_debounce", 0) + 1
            )
            return
        if self.nc is None or not getattr(self.nc, "is_connected", False):
            # No connection — nothing to rebalance against.
            return
        self._last_replica_rebalance_ts = now
        try:
            from multifleet.jetstream import ensure_streams
            await ensure_streams(self.nc, peer_count=peer_count)
            self._stats["replica_rebalances"] = (
                self._stats.get("replica_rebalances", 0) + 1
            )
            logger.info(
                f"JetStream replica rebalance triggered (peer_count={peer_count})"
            )
        except Exception as e:
            self._stats["replica_rebalance_errors"] = (
                self._stats.get("replica_rebalance_errors", 0) + 1
            )
            logger.warning(f"replica rebalance failed (peer_count={peer_count}): {e}")

    async def _handle_ack(self, msg):
        """Process inbound ACK -- mark pending message as delivered.

        ZSF-hardening: narrow bare-except. A silent swallow here hides bugs
        in ACK tracking (mirror of the heartbeat-stale bug class).
        """
        # 1) Decode + parse — malformed payload is observable but non-fatal.
        try:
            data = json.loads(msg.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as e:
            self._stats["ack_handler_errors"] = self._stats.get("ack_handler_errors", 0) + 1
            logger.warning(f"ACK decode/parse failed: {e}")
            return
        # 2) Lookup + mutate — KeyError/TypeError are the only real risks here.
        try:
            ref = data.get("ref")
            if ref in self._pending_acks:
                self._pending_acks[ref]["acked"] = True
                # K2 2026-05-04 — wake the per-msg awaiter so the deadline
                # task exits immediately instead of waiting on its timer.
                ev = self._pending_acks[ref].get("event")
                if ev is not None:
                    try:
                        ev.set()
                        self._ack_stats["ack_event_total"] += 1
                    except Exception as set_exc:
                        # ZSF: never silent — count and log.
                        self._ack_stats["ack_awaiter_errors"] += 1
                        logger.warning(
                            f"ACK event.set failed (ref={ref[:8]}): {set_exc}"
                        )
                logger.info(f"ACK: {ref[:8]} from {data.get('node','?')} via {data.get('channel','?')}")
        except (KeyError, TypeError, AttributeError) as e:
            self._stats["ack_handler_errors"] = self._stats.get("ack_handler_errors", 0) + 1
            logger.warning(f"ACK tracker update failed (ref={data.get('ref','?')}): {e}")

    async def _handle_rpc(self, msg):
        """Synchronous request-reply RPC over NATS.

        Subjects: rpc.<self>.status, rpc.<self>.health, rpc.<self>.commits, rpc.<self>.tracks

        Uses `rpc.*` prefix (NOT `fleet.*`) so messages bypass the JetStream
        FLEET_MESSAGES stream (subject `fleet.>`). Otherwise JetStream's publish-ack
        would win the reply race over our handler.

        Any peer can call `nc.request("rpc.mac1.status", b"", timeout=5)` to get
        a live JSON snapshot back. Also exposed as `GET /rpc/<node>/<method>` via HTTP.
        """
        method = "status"
        try:
            subject_parts = msg.subject.split(".")
            # Expect: rpc.<node>.<method> (3 parts). Earlier `>= 4` check was an
            # off-by-one: `rpc.mac1.health` splits to 3 parts, so EVERY method
            # silently fell through to "status" — race/y-rpc-mac1-diagnosis.md
            # (latent since 1e63b33c, 2026-04-23). Use index 2 directly so the
            # intent is explicit and a deeper subject (e.g. `rpc.mac1.foo.bar`)
            # also routes by its first method token rather than the last.
            method = subject_parts[2] if len(subject_parts) >= 3 else "status"

            if method == "status":
                reply = self._build_rpc_status()
            elif method == "health":
                reply = self._build_health()
            elif method == "commits":
                reply = {"commits": self._recent_self_commits(limit=10)}
            elif method == "tracks":
                reply = {"tracks": self._read_current_tracks()}
            elif method == "cascade_relay":
                # EEE3 — peer-fallback relay handler. A sibling has asked
                # us to forward `message` to `target`. We re-enter the
                # cascade (with peer_relay disabled on re-entry to avoid
                # ping-pong) and report the outcome synchronously.
                reply = await self._handle_cascade_relay_request(msg.data)
            else:
                reply = {"error": f"unknown rpc method: {method}",
                         "valid": ["status", "health", "commits", "tracks", "cascade_relay"]}

            if msg.reply:
                await self.nc.publish(msg.reply, json.dumps(reply).encode())
            if method == "health":
                self._struggle_record_success("consecutive_failed_health_replies")
        except Exception as e:
            if method == "health":
                self._struggle_record_failure(
                    "consecutive_failed_health_replies",
                    reason=f"rpc_health_error:{type(e).__name__}",
                )
            logger.warning(f"RPC handler error [{type(e).__name__}]: {e!r}")
            if msg.reply:
                try:
                    await self.nc.publish(msg.reply, json.dumps(
                        {"error": f"{type(e).__name__}: {e!r}"}).encode())
                except Exception as pub_err:  # noqa: BLE001 — best-effort on already-errored reply
                    if method == "health":
                        self._struggle_record_failure(
                            "consecutive_failed_health_replies",
                            reason=f"rpc_health_publish_error:{type(pub_err).__name__}",
                        )
                    logger.debug(f"RPC error-reply publish failed: {pub_err}")

    async def _handle_rpc_relay(self, msg):
        """Hub-relay stub for rpc.* — subscribed only by hub candidates (nodes
        with 2+ peers) to establish cluster-wide subscription interest for
        rpc.* subjects. Without this, a publisher on mac2 sending to
        rpc.mac1.* hits NoRespondersError because mac2's nats-server sees
        no cluster interest (star topology via mac3; direct mac1↔mac2
        route dead).

        Having mac3 subscribe to rpc.> means mac3 advertises interest to both
        mac1 and mac2. Publishes from mac2 reach mac3, which forwards to
        mac1 (cluster subject routing). mac1's specific subscriber on
        rpc.mac1.* wins over our wildcard and generates the actual reply.

        This handler itself is a no-op — NATS routing is the feature.
        """
        # Intentional no-op — presence of subscription IS the cluster-gossip fix.
        pass

    async def _handle_capability_query(self, msg):
        """Fleet-upgrading protocol — reply to ``rpc.capability.query``.

        Per docs/fleet/FLEET-UPGRADING.md (cebf24d3):
          * Read ``fleet.capability/<self>/HEAD`` from NATS KV.
          * Verify the sha exists locally via ``git cat-file -e`` before
            replying (guards against stale KV).
          * Reply on ``msg.reply`` with the KV value + ``verified_local: true``.
          * If no reply subject, the querier doesn't want us to advertise — drop.
        """
        if not msg.reply:
            return
        try:
            from multifleet.fleet_upgrader import (
                read_local_capability,
                DEFAULT_BUCKET as _CAP_BUCKET,
            )
        except ImportError as e:
            logger.warning("capability.query: fleet_upgrader import failed: %s", e)
            return
        try:
            data = await read_local_capability(
                self.nc, bucket=_CAP_BUCKET, node_id=self.node_id
            )
        except Exception as e:  # noqa: BLE001 — log + drop; peers time out cleanly
            logger.warning(
                "capability.query: read_local_capability failed: %s", e
            )
            return
        if data is None:
            # Nothing to advertise — silently decline (querier times out).
            return
        try:
            await self.nc.publish(msg.reply, json.dumps(data).encode())
        except Exception as e:  # noqa: BLE001
            logger.warning("capability.query: reply publish failed: %s", e)

    async def _handle_canary_applied(self, msg):
        """Record a ``capability.canary.applied`` broadcast from a peer."""
        try:
            payload = json.loads(msg.data.decode())
        except (ValueError, AttributeError, UnicodeDecodeError) as e:
            logger.warning("canary.applied: decode failed: %s", e)
            return
        sha = payload.get("sha") or ""
        canary_node = payload.get("node_id") or ""
        if not sha:
            logger.warning("canary.applied: empty sha — ignoring")
            return
        try:
            from multifleet.fleet_upgrader import record_canary_applied
        except ImportError as e:
            logger.warning("canary.applied: fleet_upgrader import failed: %s", e)
            return
        record_canary_applied(sha=sha, canary_node=canary_node)
        # VV3-B INV-CapKVFreshness — record freshness timestamp when a peer
        # broadcasts that they pulled a new capability sha (canary applied).
        # This is the earliest signal that a peer's capability doc was refreshed.
        if canary_node:
            self._peer_cap_publish_ts[canary_node] = time.time()

    async def _handle_cache_invalidation_event(self, msg):
        """RACE T4 — wipe all four webhook caches on config / restart events.

        Subjects handled (declared in webhook_cache_control.event_subjects()):
          * ``event.config.changed``  — operator changed a webhook section flag,
                                        TTL, or upstream config that the cache
                                        layers can no longer assume valid.
          * ``event.fleet.restart``    — fleet-wide restart; flush stale state
                                        before peers reload.

        ZSF: any failure here is logged AND counted into the per-section
        ``invalidation_errors`` Redis counter via webhook_cache_control. We
        deliberately do NOT raise — the daemon must keep serving even if the
        cache layer is misbehaving.
        """
        subject = getattr(msg, "subject", "<unknown>") or "<unknown>"
        try:
            from memory.webhook_cache_control import handle_invalidation_event
            result = handle_invalidation_event(subject)
            logger.info(
                "RACE T4 cache invalidation %s -> total=%d errors=%d",
                subject, result.get("total", 0), result.get("errors", 0),
            )
        except Exception as e:  # noqa: BLE001 — observability path
            logger.warning(
                "cache invalidation handler failed for %s: %s", subject, e
            )

    def _recent_self_commits(self, limit: int = 5) -> list:
        """Return recent git commits authored from this machine."""
        try:
            import subprocess as _sp
            r = _sp.run(
                ["git", "log", f"-{limit}", "--pretty=format:%h|%ai|%s"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=3,
            )
            if r.returncode != 0:
                return []
            commits = []
            for line in r.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) == 3:
                    commits.append({"sha": parts[0], "time": parts[1], "msg": parts[2][:120]})
            return commits
        except Exception:
            return []

    def _read_current_tracks(self) -> list:
        """Read the per-node track/WIP state file written by active sessions."""
        try:
            track_file = REPO_ROOT / ".fleet-state" / f"tracks-{self.node_id}.json"
            if track_file.exists():
                return json.loads(track_file.read_text())
        except (OSError, ValueError) as e:
            logger.debug(f"_read_current_tracks: read/parse skipped: {e}")
        return []

    def _build_rpc_status(self) -> dict:
        """Compact status snapshot for peer queries."""
        health = self._build_health()
        return {
            "node_id": self.node_id,
            "uptime_s": health.get("uptime_s", 0),
            "status": health.get("status", "?"),
            "transport": health.get("transport", "?"),
            "peers": list(health.get("peers", {}).keys()),
            "active_sessions": health.get("activeSessions", 0),
            "stats": {
                k: health.get("stats", {}).get(k, 0)
                for k in ("sent", "received", "errors", "validation_rejected",
                          "gates_blocked", "p0_discord_sends")
            },
            "last_restart_reason": health.get("stats", {}).get("last_restart_reason"),
            "surgeons": health.get("surgeons", {}),
            "recent_commits": self._recent_self_commits(limit=5),
            "current_tracks": self._read_current_tracks(),
            "git_head": health.get("git", {}).get("commit", "")[:64],
            "git_branch": health.get("git", {}).get("branch", ""),
            "reply_ts": time.time(),
        }

    async def _send_ack(self, sender: str, msg_id: str, status: str, channel: str):
        """Publish ACK back to the original sender.

        ZSF-hardening: narrow bare-except. NATS publish failure on ACK means
        the sender will retry — we must surface this via counter so fleet
        operators can see ACK delivery problems.
        """
        if not self.nc or not self.nc.is_connected or sender == self.node_id:
            return
        ack = {"ref": msg_id, "status": status, "node": self.node_id, "channel": channel}
        try:
            await self.nc.publish(_sub_ack(sender), json.dumps(ack).encode())
        except (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            self._stats["ack_send_errors"] = self._stats.get("ack_send_errors", 0) + 1
            logger.warning(f"ACK publish to {sender} failed ({msg_id[:8]}): {e}")

    # ── Injection (last mile) ──

    def _format_resume_message(self, data: dict) -> str:
        """Format a fleet message for --resume injection. Never includes raw body in logs."""
        msg_type = data.get("type", "context").upper()
        sender = data.get("from", "unknown")
        payload = data.get("payload", {})
        subject = payload.get("subject", "Fleet message")
        body = payload.get("body", "")
        lines = [
            f"[FLEET {msg_type}] from {sender}: {subject}",
            "",
            body,
        ]
        return "\n".join(lines)

    def _get_active_session_full_id(self) -> Optional[str]:
        """Get the full session ID of the most recently active interactive session.

        Prefers sessions detected via PID files (have full IDs).
        Falls back to JSONL filename stems for full UUID.
        Returns None if no active session found.
        """
        best_sid = None
        best_mtime = 0.0

        # Method 1: PID session files -- have full session ID
        sessions_dir = Path.home() / ".claude" / "sessions"
        if sessions_dir.exists():
            for pid_file in sessions_dir.glob("*.json"):
                try:
                    pid = int(pid_file.stem)
                    os.kill(pid, 0)  # alive?
                    data = json.loads(pid_file.read_text())
                    kind = data.get("kind", "")
                    # Prefer interactive sessions over agents
                    if kind == "agent":
                        continue
                    sid = data.get("sessionId", "")
                    if sid:
                        mtime = pid_file.stat().st_mtime
                        if mtime > best_mtime:
                            best_mtime = mtime
                            best_sid = sid
                except (OSError, ValueError, json.JSONDecodeError):
                    pass

        # Method 2: Recently modified JSONL (full UUID in filename)
        projects_dir = Path.home() / ".claude" / "projects"
        if projects_dir.exists() and not best_sid:
            cutoff = time.time() - 1800  # 30 min
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                for jsonl in proj_dir.glob("*.jsonl"):
                    if jsonl.stem.startswith("agent-"):
                        continue
                    try:
                        mtime = jsonl.stat().st_mtime
                        if mtime > cutoff and mtime > best_mtime:
                            best_mtime = mtime
                            best_sid = jsonl.stem  # full UUID
                    except OSError:
                        pass

        return best_sid

    def _inject_via_resume(self, session_id: str, message: str) -> bool:
        """Inject message into active Claude session via `claude --resume`.

        Args:
            session_id: Full session UUID
            message: Pre-formatted message text

        Returns:
            True if injection succeeded, False otherwise.
            Never logs message content -- only type and delivery status.
        """
        try:
            cmd = ["claude", "--resume", session_id, "--message", message]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f"resume-inject: success session={session_id[:12]}")
                return True
            logger.warning(f"resume-inject: failed rc={result.returncode} session={session_id[:12]}")
            return False
        except Exception as e:
            logger.warning(f"resume-inject: error session={session_id[:12]}: {e}")
            return False

    def _resolve_seed_path(self, data: dict) -> str:
        """Resolve seed file path - per-session when possible, per-node fallback.

        Priority:
        1. Explicit _target_session in message -> /tmp/fleet-seed-<session_id>.md
        2. Primary active session (most recently active) -> /tmp/fleet-seed-<session_id>.md
        3. Fallback -> /tmp/fleet-seed-<node_id>.md
        """
        # Check explicit target
        target_session = data.get("_target_session")
        if target_session:
            safe_sid = "".join(c for c in target_session if c.isalnum() or c in "-_")
            if safe_sid:
                return os.path.join(SEED_DIR, f"fleet-seed-{safe_sid}.md")

        # Try to detect primary session from daemon health endpoint
        try:
            import urllib.request
            resp = urllib.request.urlopen(f"http://127.0.0.1:{FLEET_DAEMON_PORT}/health", timeout=2)
            health = json.loads(resp.read())
            sessions = health.get("sessions", [])
            if sessions:
                # Pick most recently active (lowest idle)
                best = min(sessions, key=lambda s: s.get("idle_s", s.get("lastModified", 0) or 9999))
                sid = best.get("sessionId", "")
                safe_sid = "".join(c for c in sid if c.isalnum() or c in "-_") if sid else ""
                if safe_sid:
                    return os.path.join(SEED_DIR, f"fleet-seed-{safe_sid}.md")
        except Exception as e:  # noqa: BLE001 — urllib + json best-effort fallback to per-node seed
            logger.debug(f"_get_active_session_full_id: HTTP probe skipped: {e}")

        # Fallback: per-node
        return os.path.join(SEED_DIR, f"fleet-seed-{self.node_id}.md")

    def _inject_context(self, data: dict):
        """Write to seed file AND auto-trigger Claude session to read it.

        Full automated loop (zero human interaction):
        1. Write message to per-session (or per-node fallback) seed file
        2. Focus VS Code via osascript
        3. Keystroke a trigger prompt into Claude input
        4. Press Enter -> hook fires -> reads seed file -> message appears in session

        Idle gating: if _idle_gated is set, skip keystroke trigger (seed only).
        """
        msg_type = data.get("type", "context")
        sender = data.get("from", "unknown")
        payload = data.get("payload", {})

        # Try --resume first for context and reply types
        if msg_type in ("context", "reply"):
            session_id = self._get_active_session_full_id()
            if session_id:
                message = self._format_resume_message(data)
                if self._inject_via_resume(session_id, message):
                    self._notify(f"Fleet: {payload.get('subject', 'message')} from {sender}")
                    return

        # Fallback: seed file + keystroke trigger
        self._inject_seed(data)
        self._notify(f"Fleet: {payload.get('subject', 'message')} from {sender}")
        # Keystroke disabled 2026-04-17: hijacks Aaron's input box + burns session turns.
        # Seed file waits for next real prompt; UserPromptSubmit hook batch-injects.
        # xbar F:N/T + notification provide zero-cost visibility.
        logger.info(f"seed-only delivery (keystroke disabled) for {sender} {data.get('type', 'context')}")

    def _inject_seed(self, data: dict):
        """Write message to seed file (fallback injection method)."""
        sender = data.get("from", "unknown")
        payload = data.get("payload", {})
        seed_path = self._resolve_seed_path(data)
        with open(seed_path, "a") as f:
            f.write(f"\n## [{data.get('type', 'context').upper()}] from {sender}: {payload.get('subject', '')}\n\n")
            f.write(f"{payload.get('body', '')}\n\n---\n")
        logger.info(f"seed-inject: written to {seed_path}")

    _last_keystroke = 0.0  # Rate limit: max 1 keystroke per 30s

    def _keystroke_trigger(self, summary: str):
        """Focus VS Code and type a trigger prompt so the hook fires automatically.

        Uses osascript to simulate user input. Only fires if VS Code is running
        and no Claude agent is mid-response (checks for idle session).
        Rate-limited: max 1 keystroke per 30s to prevent spam when multiple messages arrive.
        """
        # Rate limit -- don't spam keystrokes
        now = time.time()
        if now - FleetNerveNATS._last_keystroke < 30:
            logger.debug(f"Keystroke throttled ({int(30 - (now - FleetNerveNATS._last_keystroke))}s remaining)")
            return
        FleetNerveNATS._last_keystroke = now
        try:
            # Check if VS Code is running
            check = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to (name of processes) contains "Code"'],
                capture_output=True, text=True, timeout=3)
            if "true" not in check.stdout.lower():
                logger.debug("VS Code not running -- skipping keystroke trigger")
                return

            # Trigger: focus VS Code, type the fleet check command, press Enter
            # The fleet-inbox-hook.sh will fire on UserPromptSubmit and read the seed file
            trigger_cmd = f"bash {REPO_ROOT}/scripts/fleet-check.sh"
            subprocess.run(
                ["osascript", "-e", f'''
                    tell application "Visual Studio Code 3" to activate
                    delay 0.5
                    tell application "System Events"
                        keystroke "{trigger_cmd}"
                        delay 0.2
                        keystroke return
                    end tell
                '''],
                capture_output=True, timeout=5)
            logger.info(f"Keystroke trigger sent to VS Code: {summary[:60]}")
        except Exception as e:
            logger.debug(f"Keystroke trigger failed: {e}")

    def _inject_alert(self, data: dict):
        """Alert: --resume (preferred) > seed file + auto-trigger + focus VS Code + notification."""
        self._inject_context(data)  # handles --resume with seed fallback
        # Additional focus for alerts (in case --resume didn't bring VS Code forward)
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to if exists (process "Code") then '
                 'tell application "Visual Studio Code 3" to activate'],
                capture_output=True, timeout=3,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug(f"_inject_alert: VS Code focus skipped: {e}")

    def _inject_task(self, data: dict):
        """Task: spawn session-driver worker (invisible to user) or fallback to claude -p."""
        payload = data.get("payload", {})
        sender = data.get("from", "unknown")
        task_id = data.get("id", str(uuid.uuid4()))[:8]
        chain_id = data.get("_chain_id")
        dispatch_id = payload.get("dispatch_id", task_id)
        timeout_s = int(payload.get("timeout_s", 300))
        prompt = f"Fleet task from {sender}: {payload.get('subject', '')}. {payload.get('body', '')}"

        def _send_result(response: str, status: str = "completed"):
            """Send task_result back to the dispatching node."""
            if self._loop and sender != self.node_id:
                result_msg = {
                    "type": "task_result", "from": self.node_id, "to": sender,
                    "payload": {"dispatch_id": dispatch_id, "status": status, "response": response[:2000]},
                    "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                }
                asyncio.run_coroutine_threadsafe(
                    self.send_with_fallback(sender, result_msg), self._loop)

        # Prefer session-driver worker (runs in tmux, never disrupts user)
        try:
            from tools.fleet_remote_worker import FleetWorkerManager
            mgr = FleetWorkerManager()
            worker_name = f"fleet-task-{task_id}"
            worker = mgr.launch_worker(self.node_id, worker_name)
            if "error" not in worker:
                # Send task in background thread (don't block message handler)
                def _run_task():
                    try:
                        response = mgr.send_task(worker_name, prompt, timeout=timeout_s)
                        mgr.stop_worker(worker_name)
                        logger.info(f"Worker {worker_name} completed: {response[:100]}...")
                        # Chain advancement: report completion
                        if chain_id:
                            self._chain_step_done(chain_id, sender, True, response[:2000])
                        _send_result(response, "completed")
                    except Exception as e:
                        logger.warning(f"Worker {worker_name} failed: {e}")
                        if chain_id:
                            self._chain_step_done(chain_id, sender, False, str(e))
                        _send_result(str(e), "failed")
                from threading import Thread
                Thread(target=_run_task, daemon=True).start()
                logger.info(f"Launched worker {worker_name} for task from {sender}")
                return
        except Exception as e:
            logger.debug(f"Session-driver unavailable ({e}), falling back to claude -p")

        # DISABLED: claude -p fallback burns API tokens with no human approval.
        # Was spawning headless Claude sessions (~300-420K tokens/hr).
        # Fall through to seed file injection instead (safe, no API cost).
        logger.warning(f"Task {task_id} from {sender}: claude -p fallback DISABLED (token protection). Using seed file.")
        self._inject_context(data)

    # ── Chain Orchestration ──

    def _handle_chain(self, data: dict):
        """Register a chain and dispatch its first step."""
        chain_id = data.get("chain_id") or str(uuid.uuid4())
        steps = data.get("steps", data.get("payload", {}).get("steps", []))
        if not steps:
            logger.warning(f"Chain {chain_id[:8]}: no steps provided")
            return
        self._chains[chain_id] = {
            "steps": steps,
            "current": 0,
            "status": "running",
            "results": [],
            "created_at": time.time(),
        }
        logger.info(f"Chain {chain_id[:8]}: registered {len(steps)} steps")
        self._dispatch_chain_step(chain_id)

    def _dispatch_chain_step(self, chain_id: str):
        """Dispatch the current step of a chain as a task message."""
        chain = self._chains.get(chain_id)
        if not chain or chain["status"] != "running":
            return
        idx = chain["current"]
        if idx >= len(chain["steps"]):
            chain["status"] = "done"
            logger.info(f"Chain {chain_id[:8]}: complete ({len(chain['steps'])} steps)")
            return
        step = chain["steps"][idx]
        target = step.get("node", self.node_id)
        task_desc = step.get("task", "chain step")
        timeout_s = step.get("timeout_s", 300)
        # Build a task message with chain metadata for tracking
        task_msg = {
            "id": str(uuid.uuid4()),
            "type": "task",
            "from": self.node_id,
            "to": target,
            "payload": {"subject": f"chain:{chain_id[:8]} step {idx}", "body": task_desc},
            "_chain_id": chain_id,
            "_chain_step": idx,
            "_chain_timeout_s": timeout_s,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        }
        if target == self.node_id:
            # Local: inject directly
            self._inject_task(task_msg)
        elif self._loop:
            # Remote: send via NATS
            asyncio.run_coroutine_threadsafe(
                self.send(target, "task", f"chain:{chain_id[:8]} step {idx}", task_desc),
                self._loop,
            )
        logger.info(f"Chain {chain_id[:8]}: dispatched step {idx} -> {target}")

    def _advance_chain(self, chain_id: str, success: bool, result: str = ""):
        """Advance a chain after step completion (or fail it)."""
        chain = self._chains.get(chain_id)
        if not chain or chain["status"] != "running":
            return
        chain["results"].append({"step": chain["current"], "success": success, "result": result[:2000]})
        if not success:
            chain["status"] = "failed"
            logger.warning(f"Chain {chain_id[:8]}: failed at step {chain['current']}")
            return
        chain["current"] += 1
        if chain["current"] >= len(chain["steps"]):
            chain["status"] = "done"
            logger.info(f"Chain {chain_id[:8]}: all {len(chain['steps'])} steps complete")
        else:
            self._dispatch_chain_step(chain_id)

    def _get_chain_status(self, chain_id: str) -> dict:
        """Return status dict for a chain."""
        chain = self._chains.get(chain_id)
        if not chain:
            return {"error": "chain not found", "chain_id": chain_id}
        return {
            "chain_id": chain_id,
            "status": chain["status"],
            "current": chain["current"],
            "total": len(chain["steps"]),
            "steps": chain["steps"],
            "results": chain["results"],
            "created_at": chain.get("created_at"),
        }

    def _chain_step_done(self, chain_id: str, origin: str, success: bool, result: str):
        """Handle chain step completion -- local advance or notify remote originator."""
        if chain_id in self._chains:
            # We own this chain -- advance locally
            self._advance_chain(chain_id, success, result)
        elif self._loop and origin != self.node_id:
            # Remote chain -- send completion back to originator
            asyncio.run_coroutine_threadsafe(
                self.send(origin, "chain_complete", f"chain:{chain_id[:8]} done",
                          json.dumps({"chain_id": chain_id, "success": success, "result": result[:2000]})),
                self._loop,
            )

    # ── Remote Worker Dispatch ──

    def _handle_task_result(self, data: dict):
        """Handle incoming task_result -- update dispatch tracking."""
        payload = data.get("payload", {})
        dispatch_id = payload.get("dispatch_id")
        if dispatch_id and dispatch_id in self._dispatches:
            self._dispatches[dispatch_id]["status"] = payload.get("status", "completed")
            self._dispatches[dispatch_id]["result"] = payload.get("response", "")
            self._dispatches[dispatch_id]["completed_at"] = time.time()
            logger.info(f"Dispatch {dispatch_id[:8]} result: {payload.get('status')}")
        else:
            logger.info(f"Task result for unknown dispatch {dispatch_id} -- injecting as context")
            self._inject_context(data)

    def _notify(self, message: str):
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "Fleet Nerve" sound name "Glass"'],
                capture_output=True, timeout=3,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug(f"_notify: osascript display notification skipped: {e}")

    def _find_claude(self) -> Optional[str]:
        for path in ["/usr/local/bin/claude", "/opt/homebrew/bin/claude"]:
            if os.path.exists(path):
                return path
        return None

    # ── Health ──

    # Mapping of struggle-counter names to the channel label that should appear
    # in `channel_degraded` once the counter trips its threshold.
    _STRUGGLE_COUNTER_TO_CHANNEL = {
        "consecutive_failed_p1_pubs": "P1",
        "consecutive_failed_p2_calls": "P2",
        "consecutive_failed_health_replies": "P_health",
    }
    # Threshold at which a counter is considered "degraded enough" to add the
    # channel to channel_degraded. Mirrors the >=3 trigger for needs_peer_assist.
    _STRUGGLE_DEGRADED_THRESHOLD = 3

    def _struggle_record_failure(self, counter_name: str, reason: str = "") -> None:
        """Increment a struggle counter + log a structured warning.

        Adds the corresponding channel label to channel_degraded once the
        counter reaches _STRUGGLE_DEGRADED_THRESHOLD (idempotent).
        """
        if counter_name not in self._struggle_state:
            logger.warning(
                "_struggle_record_failure: unknown counter %s (reason=%s)",
                counter_name, reason,
            )
            return
        self._struggle_state[counter_name] = int(self._struggle_state.get(counter_name, 0)) + 1
        count = self._struggle_state[counter_name]
        ch = self._STRUGGLE_COUNTER_TO_CHANNEL.get(counter_name)
        if ch and count >= self._STRUGGLE_DEGRADED_THRESHOLD:
            degraded = self._struggle_state.get("channel_degraded") or []
            if ch not in degraded:
                degraded.append(ch)
                self._struggle_state["channel_degraded"] = degraded
        logger.warning(
            "struggle: counter=%s count=%d reason=%s",
            counter_name, count, reason or "(unspecified)",
        )
        self._maybe_signal_peer_assist()

    def _struggle_record_success(self, counter_name: str) -> None:
        """Reset a struggle counter on first success; clear channel_degraded entry."""
        if counter_name not in self._struggle_state:
            return
        if int(self._struggle_state.get(counter_name, 0)) == 0:
            return
        self._struggle_state[counter_name] = 0
        ch = self._STRUGGLE_COUNTER_TO_CHANNEL.get(counter_name)
        if ch:
            degraded = self._struggle_state.get("channel_degraded") or []
            if ch in degraded:
                degraded = [c for c in degraded if c != ch]
                self._struggle_state["channel_degraded"] = degraded
        # Recompute cached state so True→False transitions are observable.
        self._maybe_signal_peer_assist()

    def _struggle_record_self_heal(self, outcome: str) -> None:
        """Record the most-recent self-heal attempt outcome.

        outcome: "ok" (success) | "fail" (failure) | "none" (n/a).
        Always stamps last_self_heal_attempt_ts so /health can detect a
        recent failed attempt within the 5min peer-assist window.
        """
        if outcome not in ("ok", "fail", "none"):
            outcome = "none"
        self._struggle_state["last_self_heal_attempt_ts"] = time.time()
        self._struggle_state["last_self_heal_outcome"] = outcome
        self._maybe_signal_peer_assist()

    def _maybe_signal_peer_assist(self) -> None:
        """Detect a False→True flip of `needs_peer_assist` and notify peers.

        Caches the last-observed value of `needs_peer_assist` on
        ``self._last_needs_peer_assist``. On a clean flip to True, fires
        ``PeerAssistCoordinator.on_struggling_signal`` via the running
        asyncio loop. Fully best-effort — a missing coordinator or a
        disconnected NATS client downgrades to a structured log.
        """
        snapshot = self._build_struggle_snapshot()
        curr = bool(snapshot.get("needs_peer_assist"))
        prev = bool(getattr(self, "_last_needs_peer_assist", False))
        self._last_needs_peer_assist = curr
        if not (curr and not prev):
            return
        coord = getattr(self, "_peer_assist", None)
        if coord is None:
            logger.warning(
                "peer_assist: needs_peer_assist flipped True but coordinator "
                "not wired yet (snapshot=%s)", snapshot,
            )
            return
        loop = getattr(self, "_loop", None)
        if loop is None:
            # __init__ is complete but serve() has not yet cached the loop —
            # drop to a structured warning so the signal is observable.
            logger.warning(
                "peer_assist: event loop not set — cannot dispatch "
                "on_struggling_signal (snapshot=%s)", snapshot,
            )
            return
        try:
            asyncio.run_coroutine_threadsafe(
                coord.on_struggling_signal(snapshot), loop,
            )
        except RuntimeError as e:  # loop closed / not running
            logger.warning(
                "peer_assist: run_coroutine_threadsafe failed: %s (snapshot=%s)",
                e, snapshot,
            )

    def _build_struggle_snapshot(self) -> dict:
        """Compute the `struggle` block exposed under /health.

        needs_peer_assist trips when ANY of:
          - a consecutive_failed_* counter >= 3
          - channel_degraded contains 2+ entries
          - last_self_heal_outcome == "fail" within the last 5 min
        """
        s = self._struggle_state
        needs = False
        for k in ("consecutive_failed_p1_pubs", "consecutive_failed_p2_calls",
                  "consecutive_failed_health_replies"):
            if int(s.get(k, 0)) >= self._STRUGGLE_DEGRADED_THRESHOLD:
                needs = True
                break
        if not needs:
            degraded = s.get("channel_degraded") or []
            if len(degraded) >= 2:
                needs = True
        if not needs:
            last_outcome = s.get("last_self_heal_outcome", "none")
            last_ts = float(s.get("last_self_heal_attempt_ts", 0.0) or 0.0)
            if last_outcome == "fail" and (time.time() - last_ts) <= 300:
                needs = True
        return {
            "consecutive_failed_p1_pubs": int(s.get("consecutive_failed_p1_pubs", 0)),
            "consecutive_failed_p2_calls": int(s.get("consecutive_failed_p2_calls", 0)),
            "consecutive_failed_health_replies": int(s.get("consecutive_failed_health_replies", 0)),
            "last_self_heal_attempt_ts": float(s.get("last_self_heal_attempt_ts", 0.0) or 0.0),
            "last_self_heal_outcome": s.get("last_self_heal_outcome", "none"),
            "channel_degraded": list(s.get("channel_degraded") or []),
            "needs_peer_assist": bool(needs),
        }

    def _build_peers_union(self) -> dict:
        """Wave-P: surface UNION of heartbeat + config + channel_reliability peers.

        Root-cause fix for xbar ``F:?`` / ``No peers`` UX failure: previously
        ``_health.peers`` was populated **only** from received NATS heartbeats
        (``self._peers``). When the cluster mesh was disconnected — e.g. NATS
        peer unreachable on LAN, daemon just restarted, fleet partitioned —
        the dict went empty even though ``_peers_config`` declared peers and
        ``channel_reliability`` was actively tracking them. Consumers (xbar,
        dashboards) showed "No peers" / "F:?", an actionable lie.

        Union policy (ZSF — no silent ambiguity):
          * heartbeat-present (``self._peers`` entry) → ``status="live"`` with
            lastSeen seconds (heartbeat semantics preserved, backward-compat).
          * config-declared but no heartbeat AND channel_reliability tracking →
            ``status="tracked-no-heartbeat"`` (cluster sees it, mesh broken).
          * config-declared but no heartbeat AND not tracked →
            ``status="configured-not-yet-seen"``.

        Also bumps ``peers_dict_state_invariant_violations_total`` when config
        has peers but ``_peers`` is missing entries channel_reliability is
        tracking — surfaces the asymmetric/lying state to /metrics + /health.
        """
        now = time.time()
        out: dict = {}
        # 1) Live heartbeats (preserve existing shape — xbar parses lastSeen)
        for k, v in (self._peers or {}).items():
            out[k] = {
                "lastSeen": int(now - v.get("_received_at", now)),
                "sessions": v.get("sessions", 0),
                "transport": "nats",
                "vscode": v.get("vscode", "?"),
                "status": "live",
            }
        # 2) Config + channel_reliability augmentation
        try:
            cfg_peers = set((self._peers_config or {}).keys()) - {self.node_id}
        except Exception:
            cfg_peers = set()
        try:
            cr_peers = set(self._channel_health.keys()) if isinstance(self._channel_health, dict) else set()
        except Exception:
            cr_peers = set()
        missing_but_tracked = 0
        for peer in cfg_peers:
            if peer in out:
                continue
            if peer in cr_peers:
                out[peer] = {
                    "lastSeen": 9999,
                    "sessions": 0,
                    "transport": "nats",
                    "vscode": "?",
                    "status": "tracked-no-heartbeat",
                }
                missing_but_tracked += 1
            else:
                out[peer] = {
                    "lastSeen": 9999,
                    "sessions": 0,
                    "transport": "nats",
                    "vscode": "?",
                    "status": "configured-not-yet-seen",
                }
        # 3) Invariant counter — config peers tracked by channel_reliability
        # MUST also surface in /health.peers. The union above guarantees that
        # now, so this counts how often the un-augmented dict would have lied.
        if missing_but_tracked > 0:
            self._stats["peers_dict_state_invariant_violations_total"] = (
                int(self._stats.get("peers_dict_state_invariant_violations_total", 0))
                + missing_but_tracked
            )
        # Also expose current asymmetry (gauge, not monotonic) for observability.
        self._stats["peers_dict_missing_but_tracked_current"] = missing_but_tracked
        return out

    def _build_health(self) -> dict:
        """Build health response with VS Code state and session idle times."""
        git = self._get_git_context()
        sessions = self._detect_sessions()
        # ── INTERLOCK 7 (opt-in, Session 3) ─────────────────────────
        # Merge gains-gate rolling-window snapshot into /health so
        # external dashboards / IDE panels can chart the fleet gains
        # score without re-running the gate. Snapshot returns
        # ``{"enabled": False, ...}`` when the env var is unset, so
        # the key is always present and the response shape is stable.
        gains_health: dict = {}
        try:
            from multifleet.interlocks import gains_health_snapshot
            gains_health = gains_health_snapshot()
        except Exception:
            gains_health = {"enabled": False, "error": "snapshot-failed"}
        return {
            "nodeId": self.node_id,
            "status": "ok",
            "transport": "nats",
            "natsUrl": self.nats_url,
            "vscode": self._detect_vscode_state(),
            "uptime_s": int(time.time() - self._stats["started_at"]),
            "activeSessions": len(sessions),
            "sessions": sessions[:5],
            "peers": self._build_peers_union(),
            "git": git,
            # RACE Q4 — overlay msg_size_p95_bytes (computed) onto the
            # otherwise-mutable _stats dict so /health.stats exposes the
            # rolling p95 alongside bytes_{sent,received}/largest_msg_bytes.
            # We do not mutate self._stats (keeps it monotonic-only); a
            # shallow copy is cheap and shape-stable.
            "stats": {
                **self._stats,
                "msg_size_p95_bytes": self._compute_msg_size_p95(),
                # RACE S5 — webhook budget alerts are owned by the aggregator;
                # mirror the count into _stats so /health.stats stays a single
                # scrape point. _stats stays monotonic-only — this is a view.
                **(
                    self._webhook_budget.stats()
                    if self._webhook_budget is not None
                    else {}
                ),
                # RACE V1 — superpowers phase publish/receive counters merged
                # into /health.stats so a single scrape spots ZSF regressions.
                # _phase_pub_stats() / _phase_agg_stats() are wrapped so a
                # broken bridge cannot block /health.
                **self._phase_publisher_stats(),
                **self._phase_aggregator_stats(),
                # Cycle 4 / D2 — live Anthropic ratelimit gauges, overlaid
                # on top of the -1 baseline so xbar / dashboards always see
                # the most recent header values without scraping /metrics.
                **_bridge_ratelimit_snapshot(),
                # VV3-B M5 — fleet-git-msg ff-only escalation counters.
                # Loaded from /tmp/fleet-git-msg-counters.json so operators
                # see git FF escalation metrics in a single /health scrape
                # without reading tmp files directly. ZSF: any error yields
                # git_ff_escalation_load_error counter bump, never raises.
                **self._load_git_ff_counters(),
            },
            "surgeons": self._probe_surgeons(),
            "channel_health": {
                peer: {
                    "score": f"{score}/{len(ALL_CHANNELS)}",
                    "broken": broken,
                }
                for peer in self._channel_health
                for score, broken in [self._get_peer_health_score(peer)]
            },
            "channel_reliability": self._build_channel_reliability(),
            "cascade": {
                # RACE A3 — cascade channel order mode. "strict" = P0→P7 fixed;
                # "reliability" = reordered by per-peer success history. Observable
                # here so diagnostic tools and fleet-check.sh can confirm which
                # invariant is active without reading env vars on every host.
                "mode": self._channel_order_mode,
                # EEE3 — harmonization counters. Always present so /health
                # consumers can rely on the keys. Snapshot is read-only.
                "harmonize": self._cascade_harmonize_snapshot(),
            },
            "rebuttals": self._get_rebuttal_health(),
            "rate_limits": self.rate_limiter.get_state(),
            "struggle": self._build_struggle_snapshot(),
            "delegation": (
                {
                    "active": True,
                    "chief": self._chief,
                    "queue_dir": str(self._delegation_queue_dir),
                    **self._delegation_receiver.stats(),
                }
                if self._delegation_receiver is not None
                else {"active": False, "reason": "chief-node or setup-failed"}
            ),
            # ── RACE B6 — comprehensive observability sections ─────────
            # Each section is defensively wrapped: failures produce
            # {"status": "error", "error": <msg>} rather than crashing /health.
            "jetstream_health": self._build_jetstream_health(),
            "cluster_state": self._build_cluster_state(),
            "recent_errors": self._build_recent_errors(),
            "counter_rates": self._build_counter_rates(),
            "delegation_queue": self._build_delegation_queue(),
            # Session 3 — Point 7 rolling gains-gate health window.
            # Always present (returns {"enabled": False, ...} when off)
            # so /health consumers can rely on key existence.
            "gains_health": gains_health,
            "event_bus": (
                {
                    "active": True,
                    "repair": self._event_bus["repair_quorum"].get_status(),
                }
                if self._event_bus is not None
                else {"active": False}
            ),
            # K1 2026-05-04 — Directed peer-assist snapshot. Always present
            # (returns {"active": False, ...} when not wired) so /health
            # consumers can rely on key existence. ZSF: snapshot() never
            # raises; failures are bumped on its own counters.
            "peer_assist": (
                self._peer_assist_directed.snapshot()
                if self._peer_assist_directed is not None
                else {
                    "active": False,
                    "broken_p1p2_peers": [],
                    "repair_requests_total": 0,
                    "diagnoses_total": 0,
                    "diagnosis_errors": 0,
                    "evaluations_total": 0,
                    "request_write_errors": 0,
                    "last_eval_ts": 0,
                    "last_diagnosis_ts": 0,
                }
            ),
            "state_sync": (
                self._state_sync.get_status()
                if self._state_sync is not None
                else {"active": False}
            ),
            # RACE N2 — webhook generation health bridged from the webhook
            # process via NATS pub/sub (event.webhook.completed.<node>). Always
            # present (returns {"active": False, ...} when bridge disabled) so
            # /health consumers can rely on key existence.
            #
            # RACE S5 — composite latency budget exceeded count is merged in
            # under "budget_exceeded_recent" (rolling 1h) and full breakdown
            # under "budget_exceeded" so operators see regressions at a glance.
            "webhook": self._build_webhook_health(),
            # RACE R5 — Redis health. Many subsystems (cache, llm health,
            # webhook S2/S6 cache, gains-gate) depend on Redis; when Redis
            # is down they fail silently across the board. Surface it here
            # so operators see one observable status instead of digging
            # through downstream errors. ZSF: never crashes /health — any
            # failure becomes ``{"status": "down", "last_error": ...}``.
            "redis_health": self._build_redis_health(),
            # RACE S3 — NATS client connection-pool stats. Surfaces the
            # nats-py internal counters (in/out msgs, subs, pending bytes,
            # reconnects, max payload) so operators can spot leaking
            # subscriptions or growing send buffers BEFORE the daemon
            # falls over. ZSF: never crashes /health — any failure
            # becomes ``{"status": "error", "error": <msg>}``.
            "nats_connection_health": self._build_nats_connection_health(),
            # RACE V1 — superpowers fleet phase states. Maps node_id to
            # the most recent phase event seen on event.superpowers.phase.*.
            # Other fleet nodes use this to gate on each other's
            # BLOCKING / EXECUTION state. Always present; empty dict when
            # no events recorded yet.
            "fleet_phase_states": self._build_fleet_phase_states(),
            # EEE1 2026-05-12 — Fallback transport ZSF counters. Aaron:
            # "we will want superset to operate flawlessly... we do not
            # want silent failures." transport_superset is currently
            # observability-only (no public Superset CLI / protocol);
            # the gap is made visible here so operators can spot when a
            # send was attempted, when the CLI is missing, and when the
            # registry itself failed to load. Always present (returns
            # {"superset": {...}, "telegram": {...}, "registry": {...}})
            # so /health consumers can rely on key existence.
            "fallback_transports": self._build_fallback_transport_health(),
            # GGG2 MFINV-C06 + HHH1 MFINV-C07 — surface module-level counters
            # so peers can see lifetime delta-bundle savings + channel-scoring
            # usage without scraping per-process state. ZSF: defensive
            # try/except so a single missing module never breaks /health.
            "channel_priority_modules": self._build_channel_priority_modules_health(),
            # WaveH 2026-05-12 — feature-flag dark surface enumeration.
            # Makes the "shipped opt-in but never activated" gap measurable.
            # See _build_feature_flags_dark above for rationale.
            "feature_flags_dark": self._build_feature_flags_dark(),
            # VV3-B 2026-05-12 — self-heal loop observability. Surfaces
            # _heal_in_flight, _repair_in_progress, _heal_cooldowns, and
            # the protocol-level self_heal_task so the loop is no longer
            # invisible from outside. ZSF: any error yields a structured
            # error dict and bumps zsf_counters.self_heal_obs_errors.
            "self_heal_observability": self._build_self_heal_observability(),
            # VV3-B INV-CapKVFreshness — stale capability docs per peer.
            # Empty dict = all known peers are fresh (or no peers observed yet).
            # Each entry: peer_id -> {"stale_hours": float, "action": str}.
            "cap_kv_stale_peers": dict(self._cap_kv_stale_peers),
        }

    def _build_channel_priority_modules_health(self) -> dict:
        """Collect counter snapshots from channel_priority companion modules.

        Surfaces:
        - delta_bundle (GGG2 / MFINV-C06) — bytes saved, compose/apply totals
        - channel_scoring (HHH1 / MFINV-C07) — calls, reorderings, per-channel

        Each block is independently try/except'd: one broken module never
        hides the others. Missing module yields {"<mod>_unavailable": 1}.
        """
        out: dict = {}
        try:
            from multifleet import delta_bundle as _db
            out["delta_bundle"] = _db.bundle_counters()
        except Exception as e:
            out["delta_bundle"] = {"unavailable": 1, "error": f"{type(e).__name__}"}
        try:
            from multifleet import channel_scoring as _cs
            # Module exposes get_counters_snapshot() per HHH1
            snap = _cs.get_counters_snapshot()
            # Wave-B 2026-05-12 — surface the A/B opt-in flag so operators
            # can confirm scoring activation per-node from /health without
            # SSH'ing to inspect env vars. ZSF: is_enabled() never raises.
            try:
                snap["enabled"] = bool(_cs.is_enabled())
                snap["env_flag"] = _cs.ENV_SCORING_ENABLED
            except Exception:
                snap["enabled"] = False
                snap["env_flag"] = "FLEET_CHANNEL_SCORING_ENABLED"
            out["channel_scoring"] = snap
        except Exception as e:
            out["channel_scoring"] = {"unavailable": 1, "error": f"{type(e).__name__}"}
        try:
            # HHH6 — VS Code → Superset bridge counters (per-session pushes)
            from multifleet import vscode_superset_bridge as _vsb
            out["vscode_superset"] = _vsb.vscode_superset_counters_snapshot()
        except Exception as e:
            out["vscode_superset"] = {"unavailable": 1, "error": f"{type(e).__name__}"}
        return out

    # ── WaveH 2026-05-12 feature-flag dark enumeration ──────────────
    #
    # ROOT CAUSE surfaced: HHH1's ``FLEET_CHANNEL_SCORING_ENABLED`` shipped
    # opt-in but never activated on any node — scoring counters stuck at
    # 0 fleet-wide. The broader truth is that feature flags ship dark
    # all the time and we have no observability for the gap. This block
    # walks the repo for ``FLEET_*`` env vars referenced in code, then
    # diffs against the live process environment so /health makes the
    # dark surface area measurable. ZSF: any failure yields
    # ``{"unavailable": 1, "error": "..."}`` — never raises.
    _FEATURE_FLAGS_DARK_CACHE: dict = {"ts": 0.0, "payload": None}
    _FEATURE_FLAGS_DARK_TTL_S: float = 300.0  # recompute every 5 min

    def _build_feature_flags_dark(self) -> dict:
        """Enumerate ``FLEET_*`` env vars referenced in code vs. process env.

        Returns:
            {
              "scanned": int,           # FLEET_* names found in code
              "set":     int,           # of which are present in os.environ
              "dark":    int,           # of which are unset
              "dark_names": [str, ...], # sorted dark var names
              "scan_ts": float,
              "scan_age_s": float,
            }
        """
        import os, re, time, pathlib
        now = time.time()
        cache = type(self)._FEATURE_FLAGS_DARK_CACHE
        if cache.get("payload") and (now - cache["ts"]) < type(self)._FEATURE_FLAGS_DARK_TTL_S:
            payload = dict(cache["payload"])
            payload["scan_age_s"] = round(now - cache["ts"], 2)
            return payload
        try:
            repo_root = pathlib.Path(__file__).resolve().parent.parent
            scan_dirs = [
                repo_root / "multi-fleet",
                repo_root / "tools",
                repo_root / "memory",
            ]
            pattern = re.compile(r"FLEET_[A-Z][A-Z0-9_]+")
            # Skip the patterns above (env var references), but include any
            # FLEET_* mention so we catch hardcoded reads as well.
            found: set = set()
            for d in scan_dirs:
                if not d.exists():
                    continue
                for p in d.rglob("*.py"):
                    try:
                        text = p.read_text(errors="ignore")
                    except Exception:
                        continue
                    for m in pattern.findall(text):
                        found.add(m)
            # Filter obvious non-env (e.g., FLEET_NERVE_HMAC_KEY is a secret,
            # but still a flag — keep). Do NOT filter on naming.
            in_env = {n for n in found if os.environ.get(n) is not None}
            dark = sorted(found - in_env)
            payload = {
                "scanned": len(found),
                "set": len(in_env),
                "dark": len(dark),
                "dark_names": dark,
                "scan_ts": now,
                "scan_age_s": 0.0,
            }
            cache["ts"] = now
            cache["payload"] = dict(payload)
            return payload
        except Exception as e:  # noqa: BLE001 — observability never breaks /health
            return {"unavailable": 1, "error": f"{type(e).__name__}: {e}"}

    # ── VV3-B self-heal + git-ff observability ───────────────────────

    def _load_git_ff_counters(self) -> dict:
        """Load /tmp/fleet-git-msg-counters.json → git_ff_escalation_* keys.

        Prefixes every key with ``git_ff_escalation_`` so consumers can
        identify the source. Returns ``{}`` (no keys) on missing file so
        the stats dict stays clean on nodes that never ran fleet-git-msg.sh.
        ZSF: any error bumps zsf_counters.git_ff_load_errors, never raises.
        """
        try:
            _path = "/tmp/fleet-git-msg-counters.json"
            if not os.path.exists(_path):
                return {}
            with open(_path) as _fh:
                raw = json.load(_fh)
            if not isinstance(raw, dict):
                return {}
            return {f"git_ff_escalation_{k}": v for k, v in raw.items()}
        except Exception as _e:  # noqa: BLE001 — ZSF
            self._stats["zsf_counters"] = self._stats.get("zsf_counters", {})
            self._stats["zsf_counters"]["git_ff_load_errors"] = (
                self._stats["zsf_counters"].get("git_ff_load_errors", 0) + 1
            )
            return {"git_ff_escalation_load_error": f"{type(_e).__name__}: {_e}"}

    def _build_self_heal_observability(self) -> dict:
        """Surface self-heal loop state for VV3-B ZSF compliance.

        Fields:
        - protocol_self_heal_active: bool — heal loop task running
        - peers: per-peer dict with:
            - self_heal_active: bool (heal-in-flight task is running)
            - repair_in_progress: dict or null (from _repair_in_progress)
            - cooldowns_active: list of (key, age_s) for keys in 300s window
        - heal_cooldowns_active: list of peer_ids in any active cooldown

        ZSF: any error bumps zsf_counters.self_heal_obs_errors, returns
        a structured error dict — never raises.
        """
        try:
            now = time.time()

            # CommunicationProtocol-level heal task
            proto = getattr(self, "protocol", None)
            proto_task = getattr(proto, "_self_heal_task", None)
            protocol_active = (
                proto_task is not None and not proto_task.done()
            )

            # Per-peer heal-in-flight (FleetNerveNATS._heal_in_flight)
            heal_in_flight: dict = getattr(self, "_heal_in_flight", {})

            # Per-peer repair progress (FleetNerveNATS._repair_in_progress)
            repair_in_progress: dict = getattr(self, "_repair_in_progress", {})

            # Protocol-level cooldowns ("peer:action" -> timestamp)
            proto_cooldowns: dict = getattr(proto, "_heal_cooldowns", {})

            # Collect all peers mentioned across all three sources
            all_peers = (
                set(heal_in_flight.keys())
                | set(repair_in_progress.keys())
                | {k.split(":")[0] for k in proto_cooldowns}
            )

            peers_out: dict = {}
            heal_cooldowns_active: list = []

            for peer in sorted(all_peers):
                task = heal_in_flight.get(peer)
                self_heal_active = task is not None and not task.done()

                repair = repair_in_progress.get(peer)

                # Find any cooldown entries for this peer still within 300s
                peer_cooldowns = [
                    {"key": k, "age_s": round(now - v, 1)}
                    for k, v in proto_cooldowns.items()
                    if k.startswith(f"{peer}:") and (now - v) < REPAIR_COOLDOWN_S
                ]
                if peer_cooldowns:
                    heal_cooldowns_active.append(peer)

                peers_out[peer] = {
                    "self_heal_active": self_heal_active,
                    "repair_in_progress": repair,
                    "cooldowns_active": peer_cooldowns,
                }

            return {
                "protocol_self_heal_active": protocol_active,
                "peers": peers_out,
                "heal_cooldowns_active": heal_cooldowns_active,
                # WW11-AC — cross-node SSH peer reachability recovery counters.
                "ssh_recover_attempts_total": self._stats.get("ssh_recover_attempts_total", 0),
                "ssh_recover_success_total": self._stats.get("ssh_recover_success_total", 0),
            }
        except Exception as _e:  # noqa: BLE001 — ZSF
            self._stats["zsf_counters"] = self._stats.get("zsf_counters", {})
            self._stats["zsf_counters"]["self_heal_obs_errors"] = (
                self._stats["zsf_counters"].get("self_heal_obs_errors", 0) + 1
            )
            return {
                "error": f"{type(_e).__name__}: {_e}",
                "protocol_self_heal_active": None,
                "peers": {},
                "heal_cooldowns_active": [],
            }

    # ── RACE B6 observability helpers ────────────────────────────────

    def _build_webhook_health(self) -> dict:
        """Compose /health.webhook block — generation health + budget alerts.

        Merges:
        - RACE N2 ``WebhookHealthAggregator.snapshot()`` (latency / sections)
        - RACE S5 ``WebhookBudgetAggregator.snapshot()`` (budget breaches)

        Always returns a stable shape — keys present even when bridges are
        disabled, so /health consumers never KeyError. The flat top-level
        ``budget_exceeded_recent`` int is exposed for cheap monitoring scrapes.
        """
        if self._webhook_health is not None:
            base = self._webhook_health.snapshot()
        else:
            base = {"active": False, "reason": "bridge-init-failed"}
        if self._webhook_budget is not None:
            try:
                budget_snap = self._webhook_budget.snapshot()
                base["budget_exceeded"] = budget_snap
                # Flat key for cheap polling: "is the webhook regressing?"
                base["budget_exceeded_recent"] = int(budget_snap.get("recent_count", 0))
            except Exception as e:  # noqa: BLE001 — observability never breaks /health
                base["budget_exceeded"] = {
                    "active": False,
                    "error": f"{type(e).__name__}: {e}",
                }
                base["budget_exceeded_recent"] = 0
        else:
            base["budget_exceeded"] = {"active": False, "reason": "bridge-init-failed"}
            base["budget_exceeded_recent"] = 0
        return base

    def _phase_publisher_stats(self) -> dict:
        """Wrapped read of the local SuperpowersPhasePublisher counters.

        Returns ``{}`` if the bridge module is missing — keeps /health.stats
        shape resilient to install-time skew. Never raises.
        """
        try:
            from multifleet.superpowers_adapter import get_phase_publisher
            return get_phase_publisher().stats()
        except Exception as e:  # noqa: BLE001 — observability never breaks /health
            return {"superpowers_phase_publisher_stats_error": f"{type(e).__name__}"}

    def _phase_aggregator_stats(self) -> dict:
        """Wrapped read of the SuperpowersPhaseAggregator counters."""
        if self._superpowers_phase is None:
            return {}
        try:
            return self._superpowers_phase.stats()
        except Exception as e:  # noqa: BLE001
            return {"superpowers_phase_aggregator_stats_error": f"{type(e).__name__}"}

    def _build_fleet_phase_states(self) -> dict:
        """RACE V1 — fleet-wide superpowers phase snapshot.

        Returns a mapping ``{<node_id>: {phase, prev_phase, ts, age_s, ...}}``
        sourced from the SuperpowersPhaseAggregator. Always returns a dict
        with a stable shape — empty ``{}`` when the aggregator is disabled
        or no events recorded yet, so /health consumers never KeyError.

        Wraps any aggregator exception so a broken bridge can never crash
        /health (ZSF: surface the failure as a sentinel field instead).
        """
        if self._superpowers_phase is None:
            return {"_status": "disabled"}
        try:
            return self._superpowers_phase.snapshot()
        except Exception as e:  # noqa: BLE001 — observability never breaks /health
            return {"_status": "error", "_error": f"{type(e).__name__}: {e}"}

    def _build_fallback_transport_health(self) -> dict:
        """EEE1 — surface P8 Superset + P9 Telegram + registry ZSF counters.

        Aaron's mandate (2026-05-12): "we will want superset to operate
        flawlessly... we do not want silent failures - otherwise we can't
        fix anything." transport_superset.py defines 9 ZSF counters
        (cli_missing, version_probe_failures, version_too_old,
        send_failures, send_timeouts, send_unavailable, receive_stub_hits,
        receive_failures, unexpected_errors) but those were not previously
        surfaced on /health. Same for transport_telegram (11 counters)
        and channels_registry (5 counters). This helper merges all three
        under a single stable key.

        Returns
        -------
        dict
            Shape::

                {
                    "superset": {<9 counters>},
                    "telegram": {<11 counters>},
                    "registry": {<5 counters>},
                }

            Always present; per-section sentinel ``{"_status": "error",
            "_error": "..."}`` when an import fails so a broken module
            never crashes /health (ZSF).
        """
        out: dict[str, Any] = {}
        try:
            from multifleet import transport_superset as _ts
            out["superset"] = _ts.get_counters_snapshot()
        except Exception as e:  # noqa: BLE001 — observability never breaks /health
            out["superset"] = {"_status": "error", "_error": f"{type(e).__name__}: {e}"}
        try:
            from multifleet import transport_telegram as _tt
            out["telegram"] = _tt.get_counters_snapshot()
        except Exception as e:  # noqa: BLE001
            out["telegram"] = {"_status": "error", "_error": f"{type(e).__name__}: {e}"}
        try:
            from multifleet import channels_registry as _cr
            out["registry"] = _cr.get_counters_snapshot()
        except Exception as e:  # noqa: BLE001
            out["registry"] = {"_status": "error", "_error": f"{type(e).__name__}: {e}"}
        return out

    def _build_jetstream_health(self) -> dict:
        """Per-stream JetStream health: replicas, lag, leader, state.

        Caches results for 15s so repeated /health calls don't hammer the
        server. Returns ``{"status": "disabled"}`` when JetStream isn't
        configured, ``{"status": "error", ...}`` on failure, else a
        per-stream dict with replica-follower lag (R=3 quorum observability).
        """
        now = time.time()
        if hasattr(self, "_js_health_cache") and now - self._js_health_cache_at < 15:
            return self._js_health_cache
        result: dict = {"status": "unknown", "streams": {}}
        try:
            if self.js is None:
                result = {"status": "disabled", "streams": {}}
            else:
                from multifleet.jetstream import STREAM_EVENTS, STREAM_MESSAGES
                streams_to_probe = [STREAM_MESSAGES, STREAM_EVENTS]
                probed: dict = {}
                for stream_name in streams_to_probe:
                    probed[stream_name] = self._probe_stream_sync(stream_name)
                # Overall status: ok if all streams reachable, degraded if any
                # stream has degraded/timeout status, circuit_open (CB tripped),
                # or follower lagging.  Hard "error" only for non-timeout failures.
                any_hard_err = any(
                    s.get("status") in ("error", "circuit_open") for s in probed.values()
                )
                any_degraded = any(
                    s.get("status") == "degraded" for s in probed.values()
                )
                any_lag = any(
                    (s.get("max_follower_lag", 0) or 0) > 1000 for s in probed.values()
                )
                if any_hard_err:
                    overall = "degraded"
                elif any_degraded or any_lag:
                    overall = "degraded"
                else:
                    overall = "ok"
                result = {
                    "status": overall,
                    "streams": probed,
                }
        except Exception as e:
            self._stats["errors"] += 1
            result = {"status": "error", "error": str(e), "streams": {}}
        self._js_health_cache = result
        self._js_health_cache_at = now
        return result

    # JetStream circuit breaker constants (EEE3)
    _JS_CB_FAIL_THRESHOLD = 3    # consecutive failures before OPEN
    _JS_CB_BACKOFF_INIT_S = 30   # initial OPEN window (seconds)
    _JS_CB_BACKOFF_MAX_S  = 300  # max OPEN window (5 min)

    # Configurable stream_info probe timeout — set FLEET_JETSTREAM_TIMEOUT_S to
    # override.  Default 5s (raised from 3s) tolerates transient quorum delays
    # without spurious hard errors.
    _JS_PROBE_TIMEOUT_S: float = float(os.environ.get("FLEET_JETSTREAM_TIMEOUT_S", "5"))

    def _probe_stream_sync(self, stream_name: str) -> dict:
        """Synchronously request stream info via a threadsafe asyncio call.

        Returns a compact dict:
          - replicas: configured replica count (R)
          - leader: leader node id (or None)
          - messages / bytes / first_seq / last_seq: stream state
          - max_follower_lag: worst-case follower-to-leader seq gap
            (the R=3 quorum lag signal operators actually care about)
          - status: "ok" | "error" | "circuit_open"

        Circuit breaker (EEE3): after _JS_CB_FAIL_THRESHOLD consecutive
        stream_info timeouts, switches OPEN for backoff_s seconds so /health
        returns instantly instead of blocking 3.0s on every call.
        Backoff doubles on each re-open (30 → 60 → 120 → 300s max).
        """
        now = time.monotonic()
        cb = self._js_cb.setdefault(stream_name, {
            "state": "CLOSED",
            "failures": 0,
            "open_until": 0.0,
            "backoff_s": self._JS_CB_BACKOFF_INIT_S,
        })

        # --- OPEN: return immediately if still within backoff window ---
        if cb["state"] == "OPEN":
            if now < cb["open_until"]:
                return {
                    "status": "circuit_open",
                    "error": f"circuit open for {cb['open_until'] - now:.0f}s more",
                    "backoff_s": cb["backoff_s"],
                }
            # Backoff expired → try one probe (HALF_OPEN)
            cb["state"] = "HALF_OPEN"

        try:
            if not self._loop or not self.js:
                return {"status": "error", "error": "jetstream-not-ready"}
            fut = asyncio.run_coroutine_threadsafe(
                self.js.stream_info(stream_name), self._loop
            )
            info = fut.result(timeout=self._JS_PROBE_TIMEOUT_S)
        except (TimeoutError, asyncio.TimeoutError) as e:
            # Timeout is a transient/quorum condition — report as "degraded"
            # (not hard "error") so the overall health status doesn't alarm on
            # intermittent JetStream delays.  Still observable via error field.
            # FLEET_JETSTREAM_TIMEOUT_S controls the probe window (default 5s).
            err_str = repr(e) or f"{type(e).__name__}"
            try:
                if self.nc:
                    self.js = self.nc.jetstream()
            except Exception:  # noqa: BLE001 — refresh is best-effort
                pass
            cb["failures"] += 1
            if cb["state"] == "HALF_OPEN" or cb["failures"] >= self._JS_CB_FAIL_THRESHOLD:
                cb["backoff_s"] = min(cb["backoff_s"] * 2, self._JS_CB_BACKOFF_MAX_S)
                cb["open_until"] = now + cb["backoff_s"]
                cb["state"] = "OPEN"
                cb["failures"] = 0
                self._stats["jetstream_circuit_breaker_opens_total"] += 1
            return {"status": "degraded", "error": f"timeout after {self._JS_PROBE_TIMEOUT_S}s: {err_str[:200]}"}
        except Exception as e:
            # ZSF (G3/H1 finding): asyncio.TimeoutError + nats-py errors often
            # stringify to "" — masks the real failure. Use repr() so the type
            # is always observable in /health. Refresh js handle (best-effort)
            # so a wedged JetStream context can recover without daemon bounce.
            err_str = str(e) or repr(e) or f"{type(e).__name__}"
            try:
                if self.nc:
                    self.js = self.nc.jetstream()
            except Exception:  # noqa: BLE001 — handle refresh is best-effort
                pass
            cb["failures"] += 1
            if cb["state"] == "HALF_OPEN" or cb["failures"] >= self._JS_CB_FAIL_THRESHOLD:
                # Transition CLOSED/HALF_OPEN → OPEN, double the backoff
                cb["backoff_s"] = min(cb["backoff_s"] * 2, self._JS_CB_BACKOFF_MAX_S)
                cb["open_until"] = now + cb["backoff_s"]
                cb["state"] = "OPEN"
                cb["failures"] = 0
                self._stats["jetstream_circuit_breaker_opens_total"] += 1
            return {"status": "error", "error": err_str[:200]}
        # Probe succeeded — reset circuit breaker to CLOSED
        cb["state"] = "CLOSED"
        cb["failures"] = 0
        cb["backoff_s"] = self._JS_CB_BACKOFF_INIT_S
        try:
            cfg = getattr(info, "config", None)
            state = getattr(info, "state", None)
            cluster = getattr(info, "cluster", None)
            replicas = getattr(cfg, "num_replicas", 1) if cfg else 1
            leader = getattr(cluster, "leader", None) if cluster else None
            # replicas list holds follower replicas (peer -> lag seq)
            followers = getattr(cluster, "replicas", []) if cluster else []
            lags: list = []
            follower_detail: list = []
            for r in followers or []:
                # nats-py surfaces ``current`` / ``active`` / ``lag`` per replica
                lag = getattr(r, "lag", 0) or 0
                lags.append(int(lag))
                follower_detail.append({
                    "name": getattr(r, "name", "?"),
                    "current": bool(getattr(r, "current", False)),
                    "active_ns": int(getattr(r, "active", 0) or 0),
                    "lag": int(lag),
                })
            return {
                "status": "ok",
                "replicas": int(replicas),
                "leader": leader,
                "followers": follower_detail,
                "max_follower_lag": max(lags) if lags else 0,
                "messages": int(getattr(state, "messages", 0) or 0) if state else 0,
                "bytes": int(getattr(state, "bytes", 0) or 0) if state else 0,
                "first_seq": int(getattr(state, "first_seq", 0) or 0) if state else 0,
                "last_seq": int(getattr(state, "last_seq", 0) or 0) if state else 0,
            }
        except Exception as e:
            return {"status": "error", "error": f"parse-failed: {e}"}

    def _build_cluster_state(self) -> dict:
        """NATS cluster meta: connected URL, server id, observed peers.

        Surfaces the cluster-topology view the daemon has so operators can
        compare peer discovery against what each node *thinks* it sees.
        """
        try:
            nc = self.nc
            if nc is None:
                return {"status": "disconnected"}
            # nats-py exposes connected_url, _server_info; we forward only the
            # safe subset (never credentials).
            server_info = getattr(nc, "_server_info", None) or {}
            connected_url = getattr(nc, "connected_url", None)
            out = {
                "status": "connected" if getattr(nc, "is_connected", False) else "disconnected",
                "connected_url": str(connected_url) if connected_url else None,
                "server_id": server_info.get("server_id") if isinstance(server_info, dict) else None,
                "server_name": server_info.get("server_name") if isinstance(server_info, dict) else None,
                "cluster": server_info.get("cluster") if isinstance(server_info, dict) else None,
                "version": server_info.get("version") if isinstance(server_info, dict) else None,
                "jetstream_enabled": bool(
                    isinstance(server_info, dict) and server_info.get("jetstream")
                ),
                # The daemon observes peers via heartbeats — this is the route
                # count from our perspective. Actual cluster routes are a
                # server-internal detail.
                "observed_peer_count": len(self._peers),
                "observed_peers": sorted(self._peers.keys()),
            }
            return out
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}

    def _build_recent_errors(self, limit: int = 10) -> list:
        """Return the last ``limit`` ERROR-level log entries captured by
        :class:`_ObservabilityErrorHandler`. Newest last.
        """
        try:
            # deque supports slicing via list()[-N:]. Copy to avoid concurrent
            # mutation during JSON serialisation.
            snapshot = list(self._recent_errors)
            return snapshot[-limit:]
        except Exception:
            return []

    def _build_counter_rates(self) -> dict:
        """Compute per-minute and per-hour rates for key counters.

        Strategy: snapshot counters once per call, keep the history for 90
        minutes, and derive rates by diffing against snapshots ≥60s and
        ≥3600s old.
        """
        now = time.time()
        try:
            counters = {
                k: v for k, v in self._stats.items()
                if isinstance(v, (int, float))
            }
            # Append a new snapshot and prune old ones.
            self._counter_snapshots.append({"ts": now, "counters": counters})
            cutoff = now - 5400  # 90 min
            self._counter_snapshots = [
                s for s in self._counter_snapshots if s["ts"] >= cutoff
            ]
            self._last_counter_snapshot_ts = now

            def _delta(since_s: float) -> dict:
                target_ts = now - since_s
                # Find newest snapshot at-or-before target_ts.
                baseline = None
                for s in self._counter_snapshots:
                    if s["ts"] <= target_ts:
                        baseline = s
                if baseline is None:
                    return {}
                elapsed = max(now - baseline["ts"], 1e-6)
                out = {}
                for k, v in counters.items():
                    prev = baseline["counters"].get(k, 0)
                    out[k] = round((v - prev) / elapsed, 4)
                return out

            return {
                "window_minute": _delta(60),
                "window_hour": _delta(3600),
                "snapshot_count": len(self._counter_snapshots),
                "oldest_snapshot_age_s": int(
                    now - self._counter_snapshots[0]["ts"]
                ) if self._counter_snapshots else 0,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}

    def _build_delegation_queue(self) -> dict:
        """Depth of the delegation work queue (pending/done/failed)."""
        try:
            base = self._delegation_queue_dir
            if not base.exists():
                return {"depth": 0, "done": 0, "failed": 0, "status": "empty"}
            pending = len(list(base.glob("*.json")))
            done = 0
            failed = 0
            done_dir = base / "done"
            failed_dir = base / "failed"
            if done_dir.exists():
                done = len(list(done_dir.glob("*.json")))
            if failed_dir.exists():
                failed = len(list(failed_dir.glob("*.json")))
            return {
                "depth": pending,
                "done": done,
                "failed": failed,
                "status": "ok",
                "path": str(base),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}

    # ── RACE Q4 — bandwidth + message-size telemetry helpers ─────────

    def _record_msg_size(self, direction: str, n_bytes: int) -> None:
        """Record a single message's size against bandwidth telemetry.

        direction: "sent" or "received" — selects which monotonic byte
            counter to bump. Anything else is treated as best-effort and
            only updates the rolling window + largest_msg_bytes (so future
            non-symmetric paths still feed p95 without double-counting).
        n_bytes: payload size in bytes (typically ``len(msg.data)`` on
            inbound or ``len(json.dumps(envelope).encode())`` on outbound).

        ZSF: every branch is observable. If n_bytes is bogus we count it
        as ``size_record_errors`` so the failure can't hide.
        """
        try:
            n = int(n_bytes)
            if n < 0:
                raise ValueError("negative size")
        except (TypeError, ValueError):
            self._stats["size_record_errors"] = (
                self._stats.get("size_record_errors", 0) + 1
            )
            return

        if direction == "sent":
            self._stats["bytes_sent"] = self._stats.get("bytes_sent", 0) + n
        elif direction == "received":
            self._stats["bytes_received"] = (
                self._stats.get("bytes_received", 0) + n
            )

        # High-water gauge — never decreases. Useful for capacity planning.
        if n > self._stats.get("largest_msg_bytes", 0):
            self._stats["largest_msg_bytes"] = n

        # Rolling window for p95 (bounded by MSG_SIZE_WINDOW).
        try:
            self._msg_size_window.append(n)
        except AttributeError:
            # Back-compat: older instances built before Q4 init may lack
            # the deque. Lazy-create so /metrics never crashes.
            self._msg_size_window = deque(maxlen=MSG_SIZE_WINDOW)
            self._msg_size_window.append(n)

        # Large-message warning — single-shot per message, observable.
        if n > MSG_SIZE_WARN_BYTES:
            self._stats["large_msg_count"] = (
                self._stats.get("large_msg_count", 0) + 1
            )
            logger.warning(
                "Large NATS message: direction=%s size=%d bytes (threshold=%d)",
                direction, n, MSG_SIZE_WARN_BYTES,
            )

    def _compute_msg_size_p95(self) -> int:
        """Return p95 of the rolling msg-size window in bytes.

        Returns 0 when the window is empty (cold start). Uses nearest-rank
        (no interpolation) which is standard for byte-size SLOs and avoids
        floating-point surprises in Prometheus output.
        """
        try:
            window = list(getattr(self, "_msg_size_window", ()))
        except Exception:
            return 0
        if not window:
            return 0
        ordered = sorted(window)
        # Nearest-rank p95 — integer math so small windows behave sanely.
        # rank = ceil(0.95 * N); index = rank - 1, clamped to [0, N-1].
        rank = -(-len(ordered) * 95 // 100)  # ceil(N * 0.95)
        idx = max(0, min(len(ordered) - 1, rank - 1))
        return int(ordered[idx])

    def _build_prometheus_metrics(self) -> str:
        """Emit Prometheus-format text for counters ending in
        _count / _errors / _total (plus well-known counters in _stats).

        RACE B6 optional endpoint — useful for ops dashboards that already
        speak Prometheus. Keeps the format minimal: a ``# TYPE`` line plus
        a single ``<name> <value>`` data line per counter. No labels.
        """
        lines: list = [
            "# HELP fleet_nerve_counter Aggregate fleet-nerve counter value.",
            "# TYPE fleet_nerve_counter counter",
        ]
        try:
            suffixes = ("_count", "_errors", "_total")
            # Well-known counters that don't match suffix but are counter-like.
            always = {
                "received", "sent", "broadcasts", "errors", "gates_blocked",
                "validation_rejected", "p0_discord_sends",
                "ack_handler_errors", "ack_send_errors",
                "repair_gate_health_errors",
                "peer_failures_detected_via_callback",
                "peer_failures_detected_via_no_responders",
                "heartbeats_skipped_due_to_traffic",
                # RACE Q4 — bandwidth + large-msg counters (monotonic).
                # bytes_{sent,received} are bandwidth meters; large_msg_count
                # surfaces messages above MSG_SIZE_WARN_BYTES so dashboards can
                # alert on bandwidth-blowing payloads at 100+ node scale.
                "bytes_sent", "bytes_received", "large_msg_count",
                "size_record_errors",
                # ContextDNA Claude Bridge counters (not _count/_errors/_total
                # suffixed — must whitelist or /metrics drops them).
                "bridge_anthropic_ok", "bridge_fallback_to_deepseek",
                "bridge_anthropic_skipped",
                "bridge_anthropic_passthrough_errors",
                "bridge_deepseek_failures",
                "bridge_stream_requests",
                "bridge_stream_anthropic_passthru",
                "bridge_stream_synthetic_deepseek",
                "bridge_bad_request_envelopes",
                # Anthropic ratelimit gauges (Cycle 4 / D2). Negative -1 sentinel
                # is preserved verbatim — Prometheus accepts it and alerts can
                # special-case `< 0` as "no data yet" vs `0` = "exhausted".
                "bridge_ratelimit_requests_remaining",
                "bridge_ratelimit_tokens_remaining",
                "bridge_ratelimit_input_tokens_remaining",
                "bridge_ratelimit_output_tokens_remaining",
                "bridge_ratelimit_requests_reset_epoch",
                "bridge_ratelimit_tokens_reset_epoch",
                "bridge_ratelimit_last_429_epoch",
                "bridge_ratelimit_last_retry_after_seconds",
                "bridge_ratelimit_parse_errors",
                # G1 — tool_use <-> function-call translation
                # (calls_translated_count auto-passes via _count suffix gate;
                # the other three need explicit whitelist below).
                "bridge_tool_translate_attempted",
                "bridge_tool_translate_success",
                "bridge_tool_translate_failed",
                # Cycle 8 H3 — degradation counters from 3-Surgeons consult.
                # No suffix in (_count|_errors|_total) so explicit whitelist.
                "bridge_tool_reasoner_to_chat_downgrades",
                "bridge_tool_disable_parallel_dropped",
                "bridge_tool_cache_control_dropped",
                # ZSF — HTTP client disconnects on /health (BrokenPipe/
                # ConnectionReset). Doesn't match _count/_errors/_total so
                # must be whitelisted explicitly. Counter increments in
                # do_GET when self.wfile.write(data) raises mid-response.
                "client_disconnect",
                # Cycle 6 F5 — /sprint-status page-view counter (no suffix match).
                "sprint_status_views",
                # WEBHOOK #1 PRIORITY — preventive-watchdog counters. Match
                # the _total suffix so they auto-emit, but whitelisted
                # explicitly here so dashboards know to scrape them and
                # they survive any future suffix-filter refactor.
                "webhook_subscription_resubscribe_attempts_total",
                "webhook_subscription_resubscribe_failures_total",
                # Cycle 7 G2 — ZSF counters from memory.llm_priority_queue.
                # The _errors-suffixed ones already pass the suffix gate;
                # the rest don't, so whitelist them so daemon /metrics
                # surfaces "redis missing in py3.14" and similar previously-
                # silent failures that would otherwise leave llm:queue_stats
                # forever unwritten + Atlas blind to queue depth.
                "queue_publish_module_missing",
                "queue_health_publish_module_missing",
                "queue_publish_module_warned",
                "queue_health_module_warned",
                "queue_redis_publish_available",
                "queue_worker_get_timeouts",
                "queue_zsf_log_write_errors",
                "queue_zsf_import_errors",
            }
            # Overlay live Anthropic ratelimit gauges on top of the static
            # _stats baseline (Cycle 4 / D2). _bridge_ratelimit_snapshot()
            # returns the latest header-derived values (or -1 sentinels when
            # no Anthropic call has landed yet).
            stats_view = dict(self._stats)
            try:
                stats_view.update(_bridge_ratelimit_snapshot())
            except Exception:
                pass  # ZSF — snapshot can never break /metrics emission
            # Cycle 7 G2 — overlay llm_priority_queue ZSF counters so
            # `queue_publish_errors`, `queue_publish_module_missing`, etc.
            # show up on /metrics. Lazy-import (module may not be loaded
            # in every daemon configuration; absence is itself recorded).
            try:
                from memory.llm_priority_queue import get_zsf_counters as _lpq_zsf  # type: ignore
                stats_view.update(_lpq_zsf())
            except Exception as _e:
                # Don't let an llm_priority_queue import failure break
                # /metrics — instead surface as its own counter so ops
                # can spot the integration outage.
                stats_view["queue_zsf_import_errors"] = (
                    int(stats_view.get("queue_zsf_import_errors", 0)) + 1
                )
            for k, v in stats_view.items():
                if not isinstance(v, (int, float)):
                    continue
                if not (k.endswith(suffixes) or k in always):
                    continue
                metric = f"fleet_nerve_{k}"
                lines.append(f'{metric}{{node="{self.node_id}"}} {v}')
            # Delegation queue depth as a gauge (still counter-format for
            # simplicity; ops dashboards tolerate this).
            dq = self._build_delegation_queue()
            if isinstance(dq, dict) and "depth" in dq:
                lines.append(
                    f'fleet_nerve_delegation_queue_depth{{node="{self.node_id}"}} {dq["depth"]}'
                )
            # RACE Q4 — message-size gauges. largest_msg_bytes is a
            # high-water mark; msg_size_p95_bytes is computed on demand
            # from the rolling window. Emit a real gauge TYPE so Prometheus
            # tooling distinguishes them from counters.
            largest = int(self._stats.get("largest_msg_bytes", 0))
            p95 = int(self._compute_msg_size_p95())
            lines.append(
                "# HELP fleet_nerve_largest_msg_bytes Largest single NATS "
                "message observed since start (gauge, bytes)."
            )
            lines.append("# TYPE fleet_nerve_largest_msg_bytes gauge")
            lines.append(
                f'fleet_nerve_largest_msg_bytes{{node="{self.node_id}"}} {largest}'
            )
            lines.append(
                "# HELP fleet_nerve_msg_size_p95_bytes p95 message size over "
                f"the last {MSG_SIZE_WINDOW} messages (gauge, bytes)."
            )
            lines.append("# TYPE fleet_nerve_msg_size_p95_bytes gauge")
            lines.append(
                f'fleet_nerve_msg_size_p95_bytes{{node="{self.node_id}"}} {p95}'
            )
        except Exception as e:
            lines.append(f"# error: {e}")
        # RACE R5 — Redis health gauges. ping_ms surfaces broker latency;
        # status is a 1/0 readiness signal so dashboards can alert on
        # ``fleet_nerve_redis_status{status="ok"} == 0`` without parsing
        # JSON. Many subsystems (cache, llm health, webhook S2/S6 cache,
        # gains-gate) depend on Redis — without this they fail silently.
        try:
            rh = self._build_redis_health()
            if isinstance(rh, dict):
                ping_ms = rh.get("ping_ms")
                status = rh.get("status", "down")
                lines.append(
                    "# HELP fleet_nerve_redis_ping_ms Redis PING round-trip "
                    "(gauge, milliseconds; -1 when unreachable)."
                )
                lines.append("# TYPE fleet_nerve_redis_ping_ms gauge")
                # Emit -1 when we couldn't ping (down) so PromQL can filter.
                ping_value = ping_ms if isinstance(ping_ms, (int, float)) else -1
                lines.append(
                    f'fleet_nerve_redis_ping_ms{{node="{self.node_id}"}} {ping_value}'
                )
                lines.append(
                    "# HELP fleet_nerve_redis_status Redis readiness "
                    "(1 = ok, 0 = degraded/down)."
                )
                lines.append("# TYPE fleet_nerve_redis_status gauge")
                ok_val = 1 if status == "ok" else 0
                lines.append(
                    f'fleet_nerve_redis_status{{node="{self.node_id}",status="{status}"}} {ok_val}'
                )
        except Exception as e:
            lines.append(f"# redis-metrics-error: {e}")
        # RACE S3 — NATS connection-pool counters/gauges so dashboards can
        # alert on subscription leaks, growing pending buffers, or repeated
        # client reconnects without scraping JSON. ZSF: failures here become
        # a comment line, never break /metrics.
        try:
            nch = self._build_nats_connection_health()
            if isinstance(nch, dict) and nch.get("status") != "error":
                in_msgs = int(nch.get("in_msgs_total", 0) or 0)
                out_msgs = int(nch.get("out_msgs_total", 0) or 0)
                subs = int(nch.get("subscription_count", 0) or 0)
                pending = int(nch.get("pending_data_bytes", 0) or 0)
                reconnects = int(nch.get("reconnects", 0) or 0)
                max_payload = int(nch.get("max_payload_bytes", 0) or 0)
                lines.append(
                    "# HELP fleet_nerve_nats_in_msgs_total Total NATS "
                    "messages received by this client (counter)."
                )
                lines.append("# TYPE fleet_nerve_nats_in_msgs_total counter")
                lines.append(
                    f'fleet_nerve_nats_in_msgs_total{{node="{self.node_id}"}} {in_msgs}'
                )
                lines.append(
                    "# HELP fleet_nerve_nats_out_msgs_total Total NATS "
                    "messages sent by this client (counter)."
                )
                lines.append("# TYPE fleet_nerve_nats_out_msgs_total counter")
                lines.append(
                    f'fleet_nerve_nats_out_msgs_total{{node="{self.node_id}"}} {out_msgs}'
                )
                lines.append(
                    "# HELP fleet_nerve_nats_subs_active Active NATS "
                    "subscription count (gauge — leak signal)."
                )
                lines.append("# TYPE fleet_nerve_nats_subs_active gauge")
                lines.append(
                    f'fleet_nerve_nats_subs_active{{node="{self.node_id}"}} {subs}'
                )
                lines.append(
                    "# HELP fleet_nerve_nats_pending_bytes Bytes queued "
                    "for outbound NATS write (gauge — back-pressure signal)."
                )
                lines.append("# TYPE fleet_nerve_nats_pending_bytes gauge")
                lines.append(
                    f'fleet_nerve_nats_pending_bytes{{node="{self.node_id}"}} {pending}'
                )
                lines.append(
                    "# HELP fleet_nerve_nats_reconnects_total NATS client "
                    "reconnect count since process start (counter)."
                )
                lines.append("# TYPE fleet_nerve_nats_reconnects_total counter")
                lines.append(
                    f'fleet_nerve_nats_reconnects_total{{node="{self.node_id}"}} {reconnects}'
                )
                lines.append(
                    "# HELP fleet_nerve_nats_max_payload_bytes Server-"
                    "advertised max message payload (gauge, bytes)."
                )
                lines.append("# TYPE fleet_nerve_nats_max_payload_bytes gauge")
                lines.append(
                    f'fleet_nerve_nats_max_payload_bytes{{node="{self.node_id}"}} {max_payload}'
                )
        except Exception as e:
            lines.append(f"# nats-conn-metrics-error: {e}")
        # Prometheus requires trailing newline.
        return "\n".join(lines) + "\n"

    # ── end RACE B6 observability helpers ────────────────────────────

    # ── RACE R5 — Redis health ───────────────────────────────────────

    def _build_redis_health(self) -> dict:
        """Return a compact Redis health snapshot for /health.

        Many subsystems (cache, llm health, webhook S2/S6 cache,
        gains-gate) depend on Redis; when Redis is down those subsystems
        fail silently. Exposing one status here lets operators (and
        ``fleet-check.sh``) catch the root cause instead of chasing
        downstream symptoms.

        ZSF invariant: never raises — connection failures, missing
        ``redis-py`` package, or any other exception become
        ``{"status": "down", "last_error": ...}`` so /health stays 200.

        Caches the result for 5s so a noisy /health scrape (or the
        Prometheus rendering path that also calls this) doesn't open a
        fresh socket per request.

        Shape (stable):
            status:            "ok" | "degraded" | "down"
            ping_ms:           int (round-trip; ``None`` when down)
            memory_used_mb:    int (rounded; ``None`` when unavailable)
            connected_clients: int (``None`` when unavailable)
            last_error:        str | None
        """
        now = time.time()
        cached = getattr(self, "_redis_health_cache", None)
        cached_at = getattr(self, "_redis_health_cache_at", 0.0)
        if cached is not None and (now - cached_at) < 5.0:
            return cached

        result: dict = {
            "status": "down",
            "ping_ms": None,
            "memory_used_mb": None,
            "connected_clients": None,
            "last_error": None,
        }
        try:
            try:
                import redis  # type: ignore
            except ImportError as e:
                result["last_error"] = f"redis-py-not-installed: {e}"
                self._redis_health_cache = result
                self._redis_health_cache_at = now
                return result

            host = os.environ.get("REDIS_HOST", "127.0.0.1")
            port = int(os.environ.get("REDIS_PORT", "6379"))
            db = int(os.environ.get("REDIS_DB", "0"))
            # 1s timeout so /health never blocks. socket_connect_timeout
            # short-circuits unreachable hosts even faster (kernel SYN
            # retry would otherwise dominate).
            client = redis.Redis(
                host=host,
                port=port,
                db=db,
                socket_timeout=1.0,
                socket_connect_timeout=1.0,
            )
            t0 = time.perf_counter()
            client.ping()
            elapsed_ms = int(round((time.perf_counter() - t0) * 1000))

            mem_mb: Optional[int] = None
            clients: Optional[int] = None
            try:
                info = client.info(section="memory")
                used_bytes = int(info.get("used_memory", 0) or 0)
                mem_mb = int(round(used_bytes / (1024 * 1024)))
            except Exception as e:
                # Don't downgrade overall status — ping succeeded.
                logger.debug(f"redis info(memory) skipped: {e}")
            try:
                info_c = client.info(section="clients")
                clients = int(info_c.get("connected_clients", 0) or 0)
            except Exception as e:
                logger.debug(f"redis info(clients) skipped: {e}")

            # Degraded if ping is suspiciously slow (>250ms on localhost
            # is a real signal — saturation, swap, or connection storm).
            status = "degraded" if elapsed_ms > 250 else "ok"
            result = {
                "status": status,
                "ping_ms": elapsed_ms,
                "memory_used_mb": mem_mb,
                "connected_clients": clients,
                "last_error": None,
            }
        except Exception as e:
            # ZSF: surface the failure (count + log) but keep /health 200.
            self._stats["redis_health_errors"] = (
                self._stats.get("redis_health_errors", 0) + 1
            )
            result["status"] = "down"
            result["last_error"] = str(e)[:200]
            logger.debug(f"_build_redis_health: redis unreachable: {e}")

        self._redis_health_cache = result
        self._redis_health_cache_at = now
        return result

    # ── RACE S3 — NATS connection-pool health ────────────────────────

    def _build_nats_connection_health(self) -> dict:
        """Return a compact NATS client connection-pool snapshot.

        Surfaces the nats-py internal counters (``nc.stats``) plus a few
        client-state fields so operators can see *before* the daemon falls
        over:

          - ``client_status``: connected | reconnecting | closed | draining |
            disconnected (derived from is_connected / is_reconnecting /
            is_closed / is_draining)
          - ``in_msgs_total`` / ``out_msgs_total``: lifetime counters from
            ``nc.stats`` (counter semantics — monotonic since process start)
          - ``in_bytes_total`` / ``out_bytes_total``: same, byte counters
          - ``subscription_count``: ``len(nc._subs)`` — the leak signal.
            ``_subs`` is internal but stable across nats-py 2.x; reading it
            is the only way to count active subs without hooking every
            ``subscribe`` call site.
          - ``pending_data_bytes``: outbound write-buffer size (back-pressure
            signal). 0 in steady state; growing means the consumer can't
            keep up with the publisher.
          - ``reconnects``: count of reconnect events since process start
          - ``errors_received``: protocol-level errors NATS reported
          - ``max_payload_bytes``: server-advertised max payload

        ZSF invariant: never raises — any failure becomes
        ``{"status": "error", "error": <msg>, ...zeros...}`` so /health
        keeps returning 200 even if nats-py changes its internals.
        """
        # Stable shape — defaults for the disconnected / error path so
        # callers (Prometheus emit, ops dashboards) can rely on key
        # existence without guarding every field.
        result: dict = {
            "status": "ok",
            "client_status": "disconnected",
            "in_msgs_total": 0,
            "out_msgs_total": 0,
            "in_bytes_total": 0,
            "out_bytes_total": 0,
            "subscription_count": 0,
            "pending_data_bytes": 0,
            "reconnects": 0,
            "errors_received": 0,
            "max_payload_bytes": 0,
        }
        try:
            nc = getattr(self, "nc", None)
            if nc is None:
                result["client_status"] = "closed"
                return result

            # Derive a single status string. Order matters: closed wins over
            # draining wins over reconnecting wins over connected.
            if getattr(nc, "is_closed", False):
                result["client_status"] = "closed"
            elif getattr(nc, "is_draining", False):
                result["client_status"] = "draining"
            elif getattr(nc, "is_reconnecting", False):
                result["client_status"] = "reconnecting"
            elif getattr(nc, "is_connected", False):
                result["client_status"] = "connected"
            else:
                result["client_status"] = "disconnected"

            # nats-py exposes a public ``stats`` dict with these keys:
            # in_msgs / out_msgs / in_bytes / out_bytes / reconnects /
            # errors_received. Defensive .get() — a future nats-py rename
            # must NOT crash /health.
            stats = getattr(nc, "stats", None) or {}
            if isinstance(stats, dict):
                result["in_msgs_total"] = int(stats.get("in_msgs", 0) or 0)
                result["out_msgs_total"] = int(stats.get("out_msgs", 0) or 0)
                result["in_bytes_total"] = int(stats.get("in_bytes", 0) or 0)
                result["out_bytes_total"] = int(stats.get("out_bytes", 0) or 0)
                result["reconnects"] = int(stats.get("reconnects", 0) or 0)
                result["errors_received"] = int(stats.get("errors_received", 0) or 0)

            # Subscription leak signal. ``_subs`` is internal but the only
            # source of truth — wrapping len() in try/except keeps us safe
            # if the field disappears in a future nats-py release.
            try:
                subs = getattr(nc, "_subs", None)
                if subs is not None:
                    result["subscription_count"] = int(len(subs))
            except Exception:
                pass

            # Outbound buffer back-pressure. Property on Client.
            try:
                pending = getattr(nc, "pending_data_size", 0)
                result["pending_data_bytes"] = int(pending or 0)
            except Exception:
                pass

            # Server-advertised max payload — useful when sending fails
            # with "MaxPayloadError" and ops need to know the cap.
            try:
                mp = getattr(nc, "max_payload", 0)
                result["max_payload_bytes"] = int(mp or 0)
            except Exception:
                pass

            return result
        except Exception as e:
            # ZSF: count the failure but keep /health alive with a stable
            # shape (zeros for counters, 'error' status, error message).
            try:
                self._stats["nats_conn_health_errors"] = (
                    self._stats.get("nats_conn_health_errors", 0) + 1
                )
            except Exception:
                pass
            result["status"] = "error"
            result["error"] = str(e)[:200]
            return result

    def _dispatch_delegation_to_queue(self, envelope: dict) -> None:
        """Safe dispatch stub: write accepted delegation to queue file.

        INVARIANT: this callback NEVER spawns an agent. That requires an explicit
        runner the operator opts into (separate PR + Aaron approval). The goal
        here is to prove receiver→validation→callback works end-to-end without
        risking runaway API spend.

        The task_id file is the contract for the future runner: it reads the
        oldest file, executes the brief under the budget, moves to done/ or
        failed/, and publishes the result back to reply_to.
        """
        try:
            task_id = envelope.get("task_id", "unknown")
            path = self._delegation_queue_dir / f"{task_id}.json"
            path.write_text(json.dumps(envelope, indent=2))
            logger.info(
                f"Delegation queued for manual/runner pickup: "
                f"task_id={task_id} from={envelope.get('from')} "
                f"profile={envelope.get('budget', {}).get('profile')} "
                f"subject={envelope.get('subject', '?')[:60]}"
            )
        except Exception as e:
            # ZSF: log the failure so it's visible; the receiver already
            # counted it as dispatched, so this being a queue-write issue
            # is surfaced separately.
            logger.warning(f"Delegation queue write failed (task_id={task_id}): {e}")

    def _cascade_harmonize_snapshot(self) -> dict:
        """EEE3 — Read-only counter view for /health.cascade.harmonize.

        Pulls counts from ``multifleet.channel_priority`` (which owns the
        canonical ZSF counters for cascade events + peer-relay), plus the
        daemon-local self-repair cooldown / parse-error counters from
        ``self._stats``. Always present so /health consumers can rely on
        the keys; failures bump ``cascade_harmonize_snapshot_errors``.
        """
        try:
            from multifleet import channel_priority as _cp
            cp = _cp.get_counters_snapshot()
        except Exception as exc:  # noqa: BLE001 — ZSF
            self._stats["cascade_harmonize_snapshot_errors"] = (
                self._stats.get("cascade_harmonize_snapshot_errors", 0) + 1
            )
            logger.debug(f"cascade_harmonize_snapshot: cp import failed: {exc}")
            cp = {}
        return {
            "peer_relay_enabled": bool(self._cascade_peer_relay_enabled),
            "events": {
                "peer_degraded_published": cp.get("cascade_peer_degraded_published", 0),
                "peer_healed_published": cp.get("cascade_peer_healed_published", 0),
                "peer_degraded_publish_errors": cp.get("cascade_peer_degraded_publish_errors", 0),
                "self_repair_triggered": cp.get("cascade_self_repair_triggered", 0),
                "self_repair_failures": cp.get("cascade_self_repair_failures", 0),
                "peer_relay_used": cp.get("cascade_peer_relay_used", 0),
                "peer_relay_errors": cp.get("cascade_peer_relay_errors", 0),
                "peer_relay_disabled_skips": cp.get("cascade_peer_relay_disabled_skips", 0),
            },
            "daemon": {
                "degraded_parse_errors": self._stats.get("cascade_degraded_parse_errors", 0),
                "degraded_untrusted_reporter": self._stats.get("cascade_degraded_untrusted_reporter", 0),
                "self_repair_cooldown_skips": self._stats.get("cascade_self_repair_cooldown_skips", 0),
                "self_repair_exceptions": self._stats.get("cascade_self_repair_exceptions", 0),
                "healed_observed": self._stats.get("cascade_healed_observed", 0),
                "relay_fulfilled": self._stats.get("cascade_relay_fulfilled", 0),
                "relay_declined": self._stats.get("cascade_relay_declined", 0),
                "relay_parse_errors": self._stats.get("cascade_relay_parse_errors", 0),
                "relay_untrusted_origin": self._stats.get("cascade_relay_untrusted_origin", 0),
            },
        }

    def _build_channel_reliability(self) -> dict:
        """Expose per-peer per-channel success/fail/last_ok_ts + reliability score.

        Observability hook for the dashboard and diagnostic tools. Read-only view
        of self._channel_success_rate with computed score/fail_rate fields.
        """
        now = time.time()
        out: dict = {}
        for peer, channels in self._channel_success_rate.items():
            peer_view: dict = {}
            for ch, stats in channels.items():
                ok = stats.get("ok", 0)
                fail = stats.get("fail", 0)
                last_ok_ts = stats.get("last_ok_ts", 0.0)
                total = ok + fail
                peer_view[ch] = {
                    "ok": ok,
                    "fail": fail,
                    "last_ok_ts": last_ok_ts,
                    "last_ok_s_ago": int(now - last_ok_ts) if last_ok_ts else None,
                    "success_rate": (ok / total) if total > 0 else None,
                    "score": round(self._channel_reliability_score(peer, ch), 3),
                }
            out[peer] = peer_view
        return out

    def _get_rebuttal_health(self) -> dict:
        """Rebuttal protocol stats + active proposals for health/dashboard."""
        if not self._rebuttal:
            return {"enabled": False}
        # Expire stale proposals first
        self._rebuttal.expire_stale()
        stats = self._rebuttal.stats
        # Include active proposals summary for the theatrical dashboard
        active = []
        for p in self._rebuttal.active_proposals():
            active.append({
                "task_id": p.task_id,
                "phase": p.state.value,
                "verdicts_count": len(p.rebuttals),
                "expected": 2,  # typical fleet size minus proposer
                "proposer": p.proposer,
                "decision": p.decision,
            })
        stats["enabled"] = True
        stats["active"] = active
        return stats

    def _probe_surgeons(self) -> dict:
        """Quick probe of 3-surgeon backends for health display.

        Caches results for 30s to prevent /health from blocking on slow
        network calls (MLX timeout + keychain + AWS = up to 10s).

        Q4 fix (2026-05-06): align with active 3s config to prevent
        split-brain ("daemon says down, 3s probe says OK"). Reads
        ~/.3surgeons/config.yaml to know the *currently active*
        neurologist provider, then probes that. Legacy MLX probe is
        retained as `neurologist_local` for diagnostic visibility.
        """
        now = time.time()
        if hasattr(self, '_surgeon_cache') and now - self._surgeon_cache_at < 30:
            return self._surgeon_cache

        result = {}

        # Legacy MLX/LM Studio probe -- now diagnostic only.
        # Surfaced as neurologist_local so xbar / dashboards keep visibility
        # into the local backend even when 3s has fallen back to remote.
        try:
            import urllib.request
            req = urllib.request.Request("http://127.0.0.1:5044/v1/models", method="GET")
            urllib.request.urlopen(req, timeout=1)
            result["neurologist_local"] = "ok"
        except Exception:
            result["neurologist_local"] = "down"

        # Active neurologist -- determined by ~/.3surgeons/config.yaml.
        # Cycle-10 split: when DeepSeek-reasoner is the active model, MLX
        # being down does NOT mean neuro is down. Probe what 3s USES.
        active_provider = "unknown"
        active_model = "unknown"
        active_endpoint = ""
        active_key_env = ""
        try:
            cfg_path = os.path.expanduser("~/.3surgeons/config.yaml")
            if os.path.exists(cfg_path):
                # Lightweight parse -- avoid hard yaml dependency on /health path.
                # Look for the neurologist block and pull provider/model/endpoint/api_key_env.
                with open(cfg_path, "r") as f:
                    raw = f.read()
                import re as _re
                # Extract the neurologist sub-block (until next top-level surgeon or doc key)
                m = _re.search(
                    r"^\s{2}neurologist:\s*\n((?:\s{4,}.*\n)+)",
                    raw, flags=_re.MULTILINE)
                if m:
                    block = m.group(1)
                    pm = _re.search(r"^\s{4}provider:\s*(\S+)", block, flags=_re.MULTILINE)
                    mm = _re.search(r"^\s{4}model:\s*(\S+)", block, flags=_re.MULTILINE)
                    em = _re.search(r"^\s{4}endpoint:\s*(\S+)", block, flags=_re.MULTILINE)
                    km = _re.search(r"^\s{4}api_key_env:\s*(\S+)", block, flags=_re.MULTILINE)
                    if pm: active_provider = pm.group(1).strip()
                    if mm: active_model = mm.group(1).strip()
                    if em: active_endpoint = em.group(1).strip()
                    if km: active_key_env = km.group(1).strip().strip('"\'')
        except Exception as e:
            logger.debug(f"3s config parse skipped: {e}")

        # Probe the active backend.
        neuro_status = "unknown"
        if active_provider in ("mlx", "ollama") or active_endpoint.startswith("http://127.0.0.1"):
            # Local backend -- already covered by neurologist_local.
            neuro_status = result.get("neurologist_local", "unknown")
        elif active_provider == "deepseek" or "deepseek.com" in active_endpoint:
            # Remote DeepSeek -- "reachable" means we have a usable key
            # (probing the endpoint costs $$ and adds latency to /health).
            # Same key sourcing logic as cardiologist below: env -> keychain.
            ds_key = os.environ.get(active_key_env or "Context_DNA_Deepseek", "") \
                or os.environ.get("DEEPSEEK_API_KEY", "")
            if not ds_key:
                try:
                    r = subprocess.run(
                        ["security", "find-generic-password", "-s", "fleet-nerve",
                         "-a", active_key_env or "Context_DNA_Deepseek", "-w"],
                        capture_output=True, text=True, timeout=2)
                    if r.returncode == 0 and r.stdout.strip():
                        ds_key = r.stdout.strip()
                except (OSError, subprocess.SubprocessError) as e:
                    logger.debug(f"Keychain neurologist key probe skipped: {e}")
            neuro_status = "ok" if ds_key else "no-key"
        else:
            # Unknown / config missing -- fall back to legacy semantics.
            neuro_status = result.get("neurologist_local", "unknown")

        result["neurologist"] = neuro_status
        result["neurologist_active"] = (
            f"{active_provider}:{active_model}" if active_provider != "unknown"
            else "unknown"
        )
        # Cardiologist (OpenAI) -- check key availability (never log the key)
        # Priority: env var -> macOS Keychain (fleet-nerve service) -> skip AWS (too slow for health)
        key = ""
        key_source = "none"
        if os.environ.get("OPENAI_API_KEY", ""):
            key = os.environ["OPENAI_API_KEY"]
            key_source = "env"
        if not key:
            # Try macOS Keychain (fleet-nerve service, most secure, fast local call)
            try:
                r = subprocess.run(
                    ["security", "find-generic-password", "-s", "fleet-nerve",
                     "-a", "Context_DNA_OPENAI", "-w"],
                    capture_output=True, text=True, timeout=2)
                if r.returncode == 0 and r.stdout.strip():
                    key = r.stdout.strip()
                    key_source = "keychain"
            except (OSError, subprocess.SubprocessError) as e:
                logger.debug(f"Keychain cardiologist key probe skipped: {e}")
        has_key = bool(key and key.startswith("sk-"))
        result["cardiologist"] = "ok" if has_key else "no-key"
        result["cardiologist_key_source"] = key_source
        # Flag fallback usage -- recommend fleet collaboration for best practice
        if key_source in ("none",):
            result["cardiologist_recommendation"] = (
                "Key not found. Store: security add-generic-password "
                "-s fleet-nerve -a Context_DNA_OPENAI -w <key>"
            )
        self._surgeon_cache = result
        self._surgeon_cache_at = now
        return result

    def _get_git_context(self) -> dict:
        # JJJ1 — cache 15s to prevent subprocess storms when /health is probed concurrently.
        # 30 concurrent probes × 2 git subprocesses = 60 forks → fd exhaustion + wedge.
        cache = getattr(self, "_git_context_cache", None)
        if cache and time.time() - cache[0] < 15:
            return cache[1]
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            commit = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            result = {"branch": branch, "commit": commit}
        except Exception:
            result = {"branch": "unknown", "commit": "unknown"}
        self._git_context_cache = (time.time(), result)
        return result

    def _detect_vscode_state(self) -> str:
        """Returns: 'active' (frontmost), 'open' (running), 'closed'."""
        # JJJ1 — cache 10s; pgrep + osascript per request cause fork storm under concurrent probes.
        cache = getattr(self, "_vscode_state_cache", None)
        if cache and time.time() - cache[0] < 10:
            return cache[1]
        try:
            result = subprocess.run(["pgrep", "-f", "Visual Studio Code"], capture_output=True, timeout=2)
            if result.returncode != 0:
                state = "closed"
            else:
                result = subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to get name of first application process whose frontmost is true'],
                    capture_output=True, text=True, timeout=2)
                state = "active" if "Code" in result.stdout else "open"
        except Exception:
            state = "unknown"
        self._vscode_state_cache = (time.time(), state)
        return state

    def _detect_sessions(self) -> list:
        sessions = []
        # Method 1: PID files (most reliable)
        sessions_dir = Path.home() / ".claude" / "sessions"
        if sessions_dir.exists():
            for pid_file in sessions_dir.glob("*.json"):
                try:
                    pid = int(pid_file.stem)
                    os.kill(pid, 0)
                    data = json.loads(pid_file.read_text())
                    sessions.append({
                        "sessionId": data.get("sessionId", pid_file.stem)[:12],
                        "pid": pid,
                        "kind": data.get("kind", "?"),
                        "entrypoint": data.get("entrypoint", "?"),
                    })
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
        # Method 2: JSONL mtime for idle_s
        projects_dir = Path.home() / ".claude" / "projects"
        if projects_dir.exists():
            cutoff = time.time() - 1800
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                for jsonl in proj_dir.glob("*.jsonl"):
                    if jsonl.stem.startswith("agent-"):
                        continue
                    try:
                        mtime = jsonl.stat().st_mtime
                        if mtime > cutoff:
                            sid = jsonl.stem[:12]
                            # Update idle_s on existing session or add new one
                            existing = next((s for s in sessions if s["sessionId"] == sid), None)
                            if existing:
                                existing["idle_s"] = int(time.time() - mtime)
                                existing["project"] = proj_dir.name
                            else:
                                sessions.append({
                                    "sessionId": sid,
                                    "project": proj_dir.name,
                                    "idle_s": int(time.time() - mtime),
                                    "kind": "interactive",
                                })
                    except OSError:
                        pass
        return sessions

    # ── Publish ──

    async def send(self, to: str, msg_type: str, subject: str, body: str):
        """Send a message to a specific node or all."""
        msg = {
            "id": str(uuid.uuid4()),
            "type": msg_type,
            "from": self.node_id,
            "to": to,
            "payload": {"subject": subject, "body": body},
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        }
        target = f"fleet.{to}.message" if to != "all" else "fleet.all.message"
        # RACE Q4 — encode once so we count what actually goes on the wire.
        encoded = json.dumps(msg).encode()
        await self.nc.publish(target, encoded)
        self._stats["sent"] += 1
        try:
            self._record_msg_size("sent", len(encoded))
        except Exception as _q4_err:
            self._stats["size_record_errors"] = (
                self._stats.get("size_record_errors", 0) + 1
            )
            logger.debug(f"Q4 record_msg_size(sent) failed: {_q4_err}")
        self._touch_activity()
        if to == "all":
            self._stats["broadcasts"] += 1
        logger.info(f"Sent [{msg_type}] to {to}: {subject}")

    # ── 7-Priority Fallback Protocol ──
    # P1 NATS -> P2 HTTP -> P3 Chief -> P4 Seed -> P5 SSH -> P6 WoL -> P7 Git
    # Designed for 100+ nodes. Peers resolved from NATS heartbeats + config.

    def _resolve_peer(self, target: str) -> dict:
        """Resolve peer connection info from heartbeats (dynamic) or config (static)."""
        # Dynamic: from NATS heartbeat cache (scales to 100+)
        hb = self._peers.get(target, {})
        if hb:
            return {"ip": hb.get("ip", ""), "user": hb.get("user", ""), "mac_address": hb.get("mac", "")}
        # Static: from config file (bootstrap / fallback)
        return self._peers_config.get(target, {})

    # ── Local-first repair actions (Protocol: fix yourself before others SSH in) ──

    async def _subscribe_all(self):
        """Subscribe to all fleet NATS subjects. Called on connect and reconnect.

        Tracks subscription objects in self._active_subs. On reconnect,
        existing subs are unsubscribed first to prevent duplicate delivery.
        """
        for sub in self._active_subs:
            try:
                await sub.unsubscribe()
            except Exception as e:  # noqa: BLE001 — unsubscribe on reconnect is best-effort; subs may already be dead
                logger.debug(f"_subscribe_all: unsubscribe on reconnect skipped: {e}")
        self._active_subs.clear()

        self._active_subs.append(await self.nc.subscribe(_sub_direct(self.node_id), cb=self._handle_message))
        self._active_subs.append(await self.nc.subscribe(_sub_all(), cb=self._handle_message))
        self._active_subs.append(await self.nc.subscribe(_sub_heartbeat(), cb=self._handle_heartbeat))
        self._active_subs.append(await self.nc.subscribe(_sub_ack(self.node_id), cb=self._handle_ack))
        # RPC: synchronous request-reply for peer queries (status, health, commits)
        self._active_subs.append(await self.nc.subscribe(f"rpc.{self.node_id}.>", cb=self._handle_rpc))

        # Hub-relay: if this node is hub-ish (multiple peers connected), subscribe
        # to rpc.> so cross-cluster subject gossip works. In our current
        # star topology mac3 is the hub — subscribing to rpc.> here makes
        # mac3 a passive relay for all rpc.* traffic it doesn't own.
        # Safe because _handle_rpc filters by msg.subject; non-owned rpc calls
        # are logged and dropped cleanly via msg.reply=None path.
        peer_count = len((self._peers_config or {})) - 1  # excludes self
        is_hub_candidate = peer_count >= 2  # has 2+ peers = could be star center
        if is_hub_candidate:
            self._active_subs.append(await self.nc.subscribe("rpc.>", cb=self._handle_rpc_relay))
            logger.info(f"NATS hub-relay active: rpc.> (peer_count={peer_count})")

        # ── Inter-node repair (peer-quorum L5 assist) ──
        # Struggle signals: any peer may broadcast it cannot heal target B.
        # Claims: any peer may announce "I am going to assist with B".
        # Results: assisting peer publishes success/fail so fleet can learn.
        self._active_subs.append(
            await self.nc.subscribe(SUBJECT_PEER_STRUGGLING, cb=self._handle_struggle_signal)
        )
        self._active_subs.append(
            await self.nc.subscribe(SUBJECT_PEER_ASSIST_CLAIM, cb=self._handle_assist_claim)
        )
        self._active_subs.append(
            await self.nc.subscribe(SUBJECT_PEER_REPAIR_RESULT, cb=self._handle_repair_result)
        )

        # EEE3 — Cascade harmonization (self-upgrade trigger).
        # Subscribe to peer-reported degraded/healed events:
        #   fleet.cascade.peer_degraded.<reporter>.<channel>
        # When a sibling reports our channel toward them is broken, we
        # initiate a targeted self-repair on that transport instead of
        # waiting for the 60s heal loop.
        try:
            self._active_subs.append(
                await self.nc.subscribe(
                    "fleet.cascade.peer_degraded.>",
                    cb=self._handle_cascade_peer_degraded,
                )
            )
            self._active_subs.append(
                await self.nc.subscribe(
                    "fleet.cascade.peer_healed.>",
                    cb=self._handle_cascade_peer_healed,
                )
            )
            logger.info(
                "NATS subscribed: fleet.cascade.peer_degraded.> + peer_healed.>"
            )
        except Exception as e:  # noqa: BLE001 — ZSF: cascade subs best-effort
            logger.warning(
                f"Cascade harmonization subscribe failed (non-fatal): {e}"
            )

        # EEE3 — wire the transition publisher now that nc is ready. This
        # lets ChannelState.record_failure / record_success fire the
        # cascade.peer_{degraded,healed} events on real OK↔broken edges.
        try:
            self.protocol.channel_state.set_transition_publisher(
                self._sync_nats_publish_bytes, self.node_id,
            )
        except Exception as e:  # noqa: BLE001 — ZSF
            logger.warning(
                f"channel_state.set_transition_publisher failed: {e}"
            )

        # B4 — Broadcast fanout. Subscribe to fleet.broadcast.> so a single
        # NATS publish from any peer reaches everyone at O(1) sender cost.
        # De-dup + HMAC validation happens in _handle_broadcast.
        self._active_subs.append(
            await self.nc.subscribe(_sub_broadcast(), cb=self._handle_broadcast)
        )

        # ── Fleet-upgrading protocol ──
        # rpc.capability.query  — peer asks us "what's your HEAD?"
        # capability.canary.applied — peer broadcasts they pulled a sha;
        #                              we track it for our 60s canary window.
        try:
            self._active_subs.append(
                await self.nc.subscribe(SUBJECT_CAPABILITY_QUERY, cb=self._handle_capability_query)
            )
            self._active_subs.append(
                await self.nc.subscribe(SUBJECT_CANARY_APPLIED, cb=self._handle_canary_applied)
            )
            logger.info("NATS subscribed: fleet-upgrading (capability.query + canary.applied)")
        except Exception as e:  # noqa: BLE001 — capability subs are best-effort, fleet still works
            logger.warning(f"Capability subscribe failed (non-fatal): {e}")

        # RACE N2 — webhook health bridge subscription. Webhook process
        # publishes event.webhook.completed.<node> after every generation;
        # daemon ingests, aggregates over a rolling window, and surfaces under
        # /health.webhook. Per-node subject so cross-node events don't pollute
        # the local snapshot.
        if self._webhook_health is not None:
            try:
                sub = await self._webhook_health.subscribe(self.nc)
                if sub is not None:
                    self._active_subs.append(sub)
                logger.info(
                    f"NATS subscribed: event.webhook.completed.{self.node_id} (RACE N2)"
                )
            except Exception as e:  # noqa: BLE001 — bridge is observability, not critical-path
                logger.warning(f"Webhook health subscribe failed (non-fatal): {e}")

        # RACE S5 — webhook composite-latency budget alerts subscription.
        # Per-node subject; counts feed /health.webhook.budget_exceeded_recent.
        if self._webhook_budget is not None:
            try:
                sub = await self._webhook_budget.subscribe(self.nc)
                if sub is not None:
                    self._active_subs.append(sub)
                logger.info(
                    f"NATS subscribed: event.webhook.budget_exceeded.{self.node_id} (RACE S5)"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Webhook budget subscribe failed (non-fatal): {e}")

        # RACE V1 — superpowers phase fleet-bus subscription. Wildcard so we
        # see ALL nodes' phases (not just our own); aggregator filters and
        # exposes last-known state per peer at /health.fleet_phase_states.
        # Also wires the local SuperpowersAdapter publisher to NATS so this
        # node's phase transitions broadcast outward.
        if self._superpowers_phase is not None:
            try:
                sub = await self._superpowers_phase.subscribe(self.nc)
                if sub is not None:
                    self._active_subs.append(sub)
                logger.info(
                    "NATS subscribed: event.superpowers.phase.* (RACE V1)"
                )
            except Exception as e:  # noqa: BLE001 — observability bridge
                logger.warning(
                    f"Superpowers phase subscribe failed (non-fatal): {e}"
                )
        # Wire the adapter's module-level publisher to this connection so
        # every local phase transition fires onto NATS. Best-effort: failure
        # leaves the publisher in no-transport mode (counts as publish errors,
        # observable at /health.stats.superpowers_phase_publish_errors).
        try:
            from multifleet.superpowers_adapter import (
                set_phase_publisher_transport,
            )
            set_phase_publisher_transport(
                self.nc.publish, asyncio.get_running_loop()
            )
            logger.info(
                "Superpowers phase publisher wired to NATS (RACE V1)"
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.warning(
                f"Superpowers phase publisher wiring failed (non-fatal): {e}"
            )

        # RACE T4 — auto-invalidate webhook caches on config-change /
        # fleet-restart events. Hooked into the daemon so an operator
        # publishing event.config.changed (or event.fleet.restart) wipes the
        # four TTL-based caches (S2/S6/S8/S4) across the cluster without
        # waiting for individual TTLs to expire. Best-effort: failures here
        # do NOT block daemon start (cache is observability + speedup, not
        # critical-path) but ARE logged + counted into the per-section
        # invalidation_errors stat.
        try:
            from memory.webhook_cache_control import event_subjects
            for subj in event_subjects():
                self._active_subs.append(
                    await self.nc.subscribe(subj, cb=self._handle_cache_invalidation_event)
                )
            logger.info(
                f"NATS subscribed: webhook cache invalidation "
                f"({', '.join(event_subjects())}) (RACE T4)"
            )
        except Exception as e:  # noqa: BLE001 — invalidation hook is best-effort
            logger.warning(f"Webhook cache invalidation subscribe failed (non-fatal): {e}")

        logger.info(
            f"NATS subscribed: fleet.{self.node_id}.> + fleet.all.> + fleet.heartbeat "
            f"+ ack + rpc.{self.node_id}.> + peer-quorum (struggling/claim/result) "
            f"+ fleet.broadcast.> + capability (query/canary)"
        )
        # ROUND-3 C1 — ZSF visibility. Bump every time we (re)bind subs so
        # /health surfaces the resubscribe path actually ran. If this stays
        # 0 while subscription_count is 0 the bug is connect, not subscribe.
        try:
            self._stats["nats_resubscribe_total"] = (
                self._stats.get("nats_resubscribe_total", 0) + 1
            )
        except Exception:
            pass

    async def _connect_retry_loop(self):
        """Background loop: keep retrying connect+subscribe until NATS is up.

        ROUND-3 C1 root-cause fix. Without this, a failed initial
        ``connect()`` (server unreachable, JetStream init transient, etc.)
        traps the daemon in HTTP-only mode forever — ``self.nc`` stays
        ``None`` so /health reports ``client_status: closed`` and
        ``subscription_count: 0`` until the process restarts. The
        nats-py client's ``reconnect`` logic only runs when an existing
        connection drops; it never retries an initial connect that
        never succeeded.

        ZSF: every retry increments ``nats_connect_retry_total``;
        every exception increments ``nats_connect_retry_errors``.

        Idempotent: if ``self.nc`` is already connected we sleep and
        re-check. Backoff: 5s on first miss, 10s on subsequent ones,
        capped at 30s — matches reconnect_time_wait order of magnitude.
        """
        miss_count = 0
        while self._running:
            try:
                nc = self.nc
                if nc is not None and getattr(nc, "is_connected", False):
                    miss_count = 0
                    # WEBHOOK #1 PRIORITY — preventive watchdog. Detects the
                    # "connected but zero subs" edge case where the libnats
                    # client reports is_connected==True but len(nc._subs)==0
                    # (subscription map race during reconnect). Without this,
                    # webhook events are silently dropped: producer publishes,
                    # daemon has no subscriber, events_recorded stays at 0.
                    # Logs every attempt to /tmp/webhook-resubscribe.log + bumps
                    # whitelisted counters so /metrics dashboards can alert.
                    try:
                        sub_count = len(getattr(nc, "_subs", {}) or {})
                    except Exception:
                        sub_count = -1
                    if sub_count == 0:
                        self._stats["webhook_subscription_resubscribe_attempts_total"] = (
                            self._stats.get("webhook_subscription_resubscribe_attempts_total", 0) + 1
                        )
                        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        attempts = self._stats["webhook_subscription_resubscribe_attempts_total"]
                        try:
                            await self._subscribe_all()
                            new_count = len(getattr(nc, "_subs", {}) or {})
                            line = (
                                f"{ts} node={self.node_id} subject=event.webhook.completed.{self.node_id} "
                                f"event=resubscribe attempt={attempts} result=ok subs_after={new_count}\n"
                            )
                            try:
                                with open("/tmp/webhook-resubscribe.log", "a") as f:
                                    f.write(line)
                            except Exception:
                                pass
                            logger.warning(
                                f"NATS watchdog: connected but 0 subs — resubscribed "
                                f"(attempt={attempts}, subs_after={new_count})"
                            )
                        except Exception as e:  # noqa: BLE001 — every failure counted (ZSF)
                            self._stats["webhook_subscription_resubscribe_failures_total"] = (
                                self._stats.get("webhook_subscription_resubscribe_failures_total", 0) + 1
                            )
                            failures = self._stats["webhook_subscription_resubscribe_failures_total"]
                            line = (
                                f"{ts} node={self.node_id} subject=event.webhook.completed.{self.node_id} "
                                f"event=resubscribe attempt={attempts} result=error err={type(e).__name__}:{e} "
                                f"failures_total={failures}\n"
                            )
                            try:
                                with open("/tmp/webhook-resubscribe.log", "a") as f:
                                    f.write(line)
                            except Exception:
                                pass
                            logger.error(
                                f"NATS watchdog: resubscribe failed: {type(e).__name__}: {e} "
                                f"(failures_total={failures})"
                            )

                    # WW1 (2026-05-12) — offsite-NATS watchdog.
                    # WEBHOOK #1 PRIORITY: detect "daemon connected to a peer's
                    # NATS server instead of local" — root cause of the 45h
                    # webhook silence diagnosed in
                    # .fleet/audits/2026-05-12-WW1-webhook-silence-fix.md and
                    # re-discovered in WW7-A after commit 68bc82435 wiped the
                    # patch (.fleet/audits/2026-05-12-WW7-A-webhook-silence-fix.md).
                    # On reconnect, libnats can pick any server from the
                    # cluster INFO discovered_servers pool. If we land off-host
                    # AND the local server is reachable, NATS cluster interest
                    # only propagates one hop, so the local publisher's events
                    # are silently dropped. Force-close so the connect-retry
                    # branch re-runs ``await nats.connect(servers=[self.nats_url])``
                    # and prefers the configured (local) server.
                    #
                    # Only fires when:
                    #   1. configured nats_url is loopback (127.0.0.1 / ::1 / localhost), AND
                    #   2. nc.connected_url is non-loopback, AND
                    #   3. local NATS server is reachable on configured port.
                    # ZSF: every detection bumps a counter, every reconnect
                    # attempt bumps a counter, every failure bumps a counter.
                    try:
                        configured = (self.nats_url or "").lower()
                        configured_is_local = (
                            "127.0.0.1" in configured
                            or "localhost" in configured
                            or "[::1]" in configured
                        )
                        connected_url_attr = getattr(nc, "connected_url", None)
                        connected_str = (str(connected_url_attr) if connected_url_attr else "").lower()
                        connected_is_local = (
                            connected_str
                            and (
                                "127.0.0.1" in connected_str
                                or "localhost" in connected_str
                                or "[::1]" in connected_str
                            )
                        )
                        if configured_is_local and connected_str and not connected_is_local:
                            self._stats["webhook_offsite_nats_detected"] = (
                                self._stats.get("webhook_offsite_nats_detected", 0) + 1
                            )
                            # Probe local server before forcing reconnect — if local
                            # is unreachable, staying on the remote peer is the lesser
                            # evil (don't churn through "reconnect → fail → fall back
                            # to remote" loops).
                            reachable = False
                            try:
                                import socket as _socket
                                import urllib.parse as _urlp
                                p = _urlp.urlparse(self.nats_url)
                                port = p.port or NATS_PORT
                                with _socket.create_connection(("127.0.0.1", port), timeout=1.0) as _s:
                                    reachable = True
                            except Exception as _probe_exc:  # noqa: BLE001 — counted via reachable=False
                                logger.debug(f"WW1 offsite-watchdog: local probe failed: {_probe_exc!r}")
                            if reachable:
                                logger.error(
                                    "WW1 offsite-NATS detected: daemon connected to %s "
                                    "(configured %s, local probe ok). Forcing reconnect "
                                    "so the local subscription is re-established.",
                                    connected_str, configured,
                                )
                                self._stats["webhook_offsite_nats_reconnect_total"] = (
                                    self._stats.get("webhook_offsite_nats_reconnect_total", 0) + 1
                                )
                                try:
                                    await nc.close()
                                    self.nc = None
                                    miss_count = 0  # force immediate retry branch
                                    await asyncio.sleep(0.5)
                                    continue
                                except Exception as e:  # noqa: BLE001 — ZSF
                                    self._stats["webhook_offsite_nats_reconnect_errors"] = (
                                        self._stats.get("webhook_offsite_nats_reconnect_errors", 0) + 1
                                    )
                                    logger.warning(
                                        f"WW1 offsite-NATS reconnect close failed: {type(e).__name__}: {e}"
                                    )
                            else:
                                logger.debug(
                                    "WW1 offsite-NATS detected but local probe failed — "
                                    "staying on remote peer (better than no transport)."
                                )
                    except Exception as e:  # noqa: BLE001 — ZSF
                        # Watchdog must never crash the connect-retry loop.
                        # Errors are counted into the existing retry-errors
                        # bucket so they remain observable.
                        self._stats["nats_connect_retry_errors"] = (
                            self._stats.get("nats_connect_retry_errors", 0) + 1
                        )
                        logger.debug(f"WW1 offsite-watchdog inner error: {e!r}")

                    await asyncio.sleep(15)
                    continue

                # Retry needed. Bump counter BEFORE attempt so a hang/crash
                # is still visible in /health.
                self._stats["nats_connect_retry_total"] = (
                    self._stats.get("nats_connect_retry_total", 0) + 1
                )
                logger.info(
                    f"NATS connect-retry loop: attempting reconnect "
                    f"(miss_count={miss_count}, retry_total={self._stats['nats_connect_retry_total']})"
                )
                # Best-effort close any half-dead client object first so the
                # next ``await nats.connect`` starts clean.
                if nc is not None:
                    try:
                        await nc.close()
                    except Exception as e:  # noqa: BLE001 — close-before-reconnect is best-effort
                        logger.debug(f"_connect_retry_loop: nc.close skipped: {e}")
                    self.nc = None

                await self.connect()
                await self._subscribe_all()
                logger.info(
                    f"NATS connect-retry loop: SUCCESS — "
                    f"resubscribe_total={self._stats.get('nats_resubscribe_total', 0)}"
                )
                miss_count = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — every retry exception counted (ZSF)
                self._stats["nats_connect_retry_errors"] = (
                    self._stats.get("nats_connect_retry_errors", 0) + 1
                )
                logger.warning(
                    f"NATS connect-retry loop: attempt failed: {e} "
                    f"(errors={self._stats['nats_connect_retry_errors']})"
                )
                miss_count += 1
            # Backoff: 5s, then 10s, capped at 30s
            await asyncio.sleep(min(30, 5 * max(1, miss_count)))

    async def _repair_restart_nats(self, heal: dict):
        """Reconnect to NATS server without killing the daemon."""
        logger.info("Repair: reconnecting to NATS...")
        if self.nc:
            try:
                await self.nc.close()
            except Exception as e:  # noqa: BLE001 — close before reconnect is best-effort
                logger.debug(f"_repair_restart_nats: nc.close before reconnect skipped: {e}")
        await asyncio.sleep(1)
        await self.connect()
        await self._subscribe_all()
        logger.info("Repair: NATS reconnected + resubscribed")

    async def _repair_restart_tunnel(self, heal: dict):
        """Restart the autossh tunnel via launchctl."""
        logger.info("Repair: restarting SSH tunnel...")
        subprocess.run(["launchctl", "kickstart", "-k",
                        f"gui/{os.getuid()}/io.contextdna.fleet-tunnel"],
                       capture_output=True, timeout=10)
        logger.info("Repair: tunnel restart requested")

    async def _repair_git_pull(self, heal: dict):
        """Pull latest code from origin."""
        logger.info("Repair: git pull...")
        result = subprocess.run(["git", "pull", "origin", "main"],
                                cwd=str(REPO_ROOT), capture_output=True, timeout=30)
        logger.info(f"Repair: git pull {'OK' if result.returncode == 0 else 'FAILED'}")

    async def _repair_kill_zombie(self, heal: dict):
        """Kill a specific zombie/rogue process by PID."""
        pid = heal.get("pid")
        if not pid:
            logger.warning("Repair: kill_zombie requires 'pid' in heal payload")
            return
        try:
            os.kill(int(pid), 9)
            logger.info(f"Repair: killed PID {pid}")
        except (ProcessLookupError, PermissionError) as e:
            logger.warning(f"Repair: kill PID {pid} failed: {e}")

    async def _repair_restart_daemon(self, heal: dict):
        """Restart this daemon process (exec replaces current process)."""
        logger.info("Repair: daemon restart requested -- re-execing in 2s...")
        await asyncio.sleep(2)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ── B4: Broadcast fanout (O(1) NATS primary, parallel HTTP fallback) ──

    def _is_broadcast_target(self, target: str) -> bool:
        """Return True when target means 'deliver to every peer'.

        Accepts the documented 'all' plus common aliases '*' and ''.
        Kept as a small predicate so call sites stay readable.
        """
        return target in ("all", "*", "")

    async def _handle_broadcast(self, msg):
        """Receive end of fleet.broadcast.> — de-dup then route locally.

        Replay window: drop if we've already seen this message id inside
        BROADCAST_SEEN_MAX. HMAC validation still runs inside
        _handle_message via validate_inbound_message, so this handler is a
        thin translator: de-dup → delegate.
        """
        try:
            data = json.loads(msg.data.decode())
        except Exception:
            self._stats["errors"] += 1
            return
        msg_id = data.get("id")
        # Never process our own broadcasts.
        if data.get("from") == self.node_id:
            return
        # Bounded replay-window drop (O(1) set check).
        if msg_id:
            if msg_id in self._seen_broadcast_set:
                self._stats["broadcast_duplicates_dropped"] += 1
                return
            # Evict oldest when capacity reached so the set cannot grow
            # unbounded on long-lived daemons.
            if len(self._seen_broadcast_ids) == self._seen_broadcast_ids.maxlen:
                oldest = self._seen_broadcast_ids[0]
                self._seen_broadcast_set.discard(oldest)
            self._seen_broadcast_ids.append(msg_id)
            self._seen_broadcast_set.add(msg_id)
        # Delegate to the normal message handler. _handle_message validates
        # schema, rate-limits, applies HMAC check, and records stats.
        await self._handle_message(msg)

    async def _broadcast_http_fanout(
        self,
        peers: list,
        message: dict,
        per_peer_timeout: float = BROADCAST_HTTP_PEER_TIMEOUT_S,
    ) -> dict:
        """Parallel HTTP POST to every peer's /message endpoint.

        Uses asyncio.gather with return_exceptions=True so one 500 or timeout
        never blocks the others. Wall-clock is bounded by the slowest peer
        (or per_peer_timeout), not N * timeout.
        """
        import urllib.request

        payload = json.dumps(message).encode()

        async def _post_one(peer_id: str, peer: dict) -> tuple:
            ip = peer.get("ip")
            if not ip:
                return (peer_id, False, "no_ip")
            tunnel_port = peer.get("tunnel_port")
            urls = [f"http://{ip}:{FLEET_DAEMON_PORT}/message"]
            if tunnel_port:
                urls.append(f"http://127.0.0.1:{tunnel_port}/message")

            def _sync_post() -> tuple:
                last_err = None
                for url in urls:
                    try:
                        req = urllib.request.Request(
                            url, data=payload,
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        urllib.request.urlopen(req, timeout=per_peer_timeout)
                        return (True, url)
                    except Exception as e:  # noqa: BLE001 — any failure → try next URL
                        last_err = e
                return (False, f"all_urls_failed:{type(last_err).__name__ if last_err else 'unknown'}")

            try:
                ok, detail = await asyncio.wait_for(
                    asyncio.to_thread(_sync_post),
                    timeout=per_peer_timeout + 0.5,
                )
                return (peer_id, ok, detail)
            except asyncio.TimeoutError:
                return (peer_id, False, "timeout")
            except Exception as e:  # noqa: BLE001 — observable via counters
                return (peer_id, False, f"{type(e).__name__}:{e}")

        tasks = [_post_one(pid, p) for pid, p in peers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        delivered_to = []
        failed = []
        for r in results:
            if isinstance(r, Exception):
                failed.append(("?", str(r)))
                continue
            peer_id, ok, detail = r
            if ok:
                delivered_to.append(peer_id)
            else:
                failed.append((peer_id, detail))
        self._stats["broadcast_http_peer_failures"] += len(failed)
        return {
            "delivered": len(delivered_to) > 0,
            "delivered_to": delivered_to,
            "failed": failed,
            "peer_count": len(peers),
        }

    async def _send_broadcast(self, message: dict) -> dict:
        """B4: O(1) NATS publish primary, parallel HTTP fanout fallback.

        Preferred path: publish ONCE to fleet.broadcast.<type>. All peers
        subscribe to fleet.broadcast.> and route locally. O(1) for sender
        regardless of fleet size.

        Fallback (NATS dead): asyncio.gather POST to every peer with
        per-peer timeout. Wall-clock bounded by slowest peer, not N*timeout.

        Replay prevention: msg_id + timestamp already live in the envelope;
        peers de-dup in _handle_broadcast.
        """
        # Ensure envelope has id + timestamp so peers can de-dup.
        if "id" not in message:
            message["id"] = str(uuid.uuid4())
        if "from" not in message:
            message["from"] = self.node_id
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat() + "Z"
        if "to" not in message:
            message["to"] = "all"

        msg_type = message.get("type", "message") or "message"
        # Compute peer list once — used by both metric + fallback path.
        peers = [
            (pid, p) for pid, p in (self._peers_config or {}).items()
            if pid != self.node_id
        ]
        self._stats["broadcast_peer_count"] = len(peers)

        # ── Primary: O(1) NATS publish on fleet.broadcast.<type> ──
        if self.nc and getattr(self.nc, "is_connected", False):
            subject = f"{SUBJECT_BROADCAST_PREFIX}.{msg_type}"
            try:
                # RACE Q4 — encode once for both publish + bandwidth telemetry.
                encoded = json.dumps(message).encode()
                await self.nc.publish(subject, encoded)
                self._stats["broadcast_method_nats"] += 1
                self._stats["sent"] += 1
                self._stats["broadcasts"] += 1
                try:
                    self._record_msg_size("sent", len(encoded))
                except Exception as _q4_err:
                    self._stats["size_record_errors"] = (
                        self._stats.get("size_record_errors", 0) + 1
                    )
                    logger.debug(f"Q4 record_msg_size(broadcast) failed: {_q4_err}")
                logger.info(
                    f"broadcast via NATS [{subject}] id={message['id'][:8]} "
                    f"peer_count={len(peers)} (O(1) fanout)"
                )
                return {
                    "delivered": True,
                    "method": "nats",
                    "subject": subject,
                    "peer_count": len(peers),
                }
            except Exception as e:  # noqa: BLE001 — fall through to HTTP fanout
                logger.warning(
                    f"broadcast NATS publish failed ({e}); falling back to HTTP fanout"
                )

        # ── Fallback: parallel HTTP POST per peer ──
        if not peers:
            return {"delivered": False, "method": "http", "error": "no_peers", "peer_count": 0}
        result = await self._broadcast_http_fanout(peers, message)
        self._stats["broadcast_method_http"] += 1
        self._stats["broadcasts"] += 1
        logger.info(
            f"broadcast via HTTP fanout id={message['id'][:8]} "
            f"delivered_to={len(result.get('delivered_to', []))}/{len(peers)}"
        )
        result["method"] = "http"
        return result

    async def _send_p1_nats(self, target: str, message: dict) -> dict:
        """P1: NATS pub/sub -- fastest, scales to 100+ nodes.

        Increments consecutive_failed_p1_pubs on publish failure and resets
        it on the first successful publish (struggle metrics).

        B4: When target is a broadcast ('all'/'*'), delegate to
        _send_broadcast so O(1) fleet.broadcast.<type> is used and metrics
        (broadcast_method_nats, peer_count) stay accurate.
        """
        if self._is_broadcast_target(target):
            # Preserve existing "fleet.all.message" envelope + delegate.
            result = await self._send_broadcast(message)
            # Normalize envelope for the cascade caller.
            return {"delivered": bool(result.get("delivered"))}
        if not self.nc or not self.nc.is_connected:
            self._struggle_record_failure(
                "consecutive_failed_p1_pubs", reason="nats_disconnected")
            return {"delivered": False}
        subject = f"fleet.{target}.message" if target != "all" else "fleet.all.message"
        # RACE Q4 — encode once so we record exactly what NATS publishes.
        encoded = json.dumps(message).encode()
        try:
            await self.nc.publish(subject, encoded)
        except Exception as e:
            self._struggle_record_failure(
                "consecutive_failed_p1_pubs",
                reason=f"publish_error:{type(e).__name__}",
            )
            logger.warning("P1 NATS publish failed for %s: %s", target, e)
            return {"delivered": False, "error": f"nats_publish_error:{e}"}
        self._struggle_record_success("consecutive_failed_p1_pubs")
        self._stats["sent"] += 1
        try:
            self._record_msg_size("sent", len(encoded))
        except Exception as _q4_err:
            self._stats["size_record_errors"] = (
                self._stats.get("size_record_errors", 0) + 1
            )
            logger.debug(f"Q4 record_msg_size(p1) failed: {_q4_err}")
        return {"delivered": True}

    async def _send_p2_http(self, target: str, message: dict) -> dict:
        """P2: HTTP direct to target daemon. Tries direct IP, then tunnel port.

        Increments consecutive_failed_p2_calls when ALL configured URLs error
        out; resets to 0 on first success.

        B4: When target is a broadcast ('all'/'*'), delegate to
        _send_broadcast. If NATS is live it uses O(1) fleet.broadcast.<type>;
        if NATS is down it runs parallel HTTP fanout (asyncio.gather, per-peer
        timeout). Previously returned {"delivered": False} silently because
        _resolve_peer('all') was empty — a documented gap this closes.
        """
        if self._is_broadcast_target(target):
            result = await self._send_broadcast(message)
            return {"delivered": bool(result.get("delivered"))}
        peer = self._resolve_peer(target)
        ip = peer.get("ip")
        tunnel_port = peer.get("tunnel_port")
        if not ip:
            self._struggle_record_failure(
                "consecutive_failed_p2_calls", reason="no_peer_ip")
            return {"delivered": False}
        import urllib.request
        payload = json.dumps(message).encode()
        # Try direct first, then tunnel
        urls = [f"http://{ip}:{FLEET_DAEMON_PORT}/message"]
        if tunnel_port:
            urls.append(f"http://127.0.0.1:{tunnel_port}/message")
        last_err: Optional[Exception] = None
        for url in urls:
            try:
                req = urllib.request.Request(url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=3)
                self._struggle_record_success("consecutive_failed_p2_calls")
                return {"delivered": True, "url": url}
            except Exception as e:
                last_err = e
                logger.debug("P2 HTTP attempt %s failed: %s", url, e)
                continue
        self._struggle_record_failure(
            "consecutive_failed_p2_calls",
            reason=f"all_urls_failed:{type(last_err).__name__ if last_err else 'unknown'}",
        )
        return {"delivered": False}

    async def _send_p3_chief(self, target: str, message: dict) -> dict:
        """P3: Chief relay -- stores and forwards. Tries direct, then tunnel."""
        if not self._chief:
            return {"delivered": False}
        chief = self._resolve_peer(self._chief)
        chief_ip = chief.get("ip")
        if not chief_ip or self.node_id == self._chief:
            return {"delivered": False}
        import urllib.request
        payload = json.dumps({"from": self.node_id, "to": [target],
                              "subject": message.get("payload", {}).get("subject", ""),
                              "body": message.get("payload", {}).get("body", "")}).encode()
        # Try direct chief, then via tunnel (localhost:8844 if tunneled)
        urls = [f"http://{chief_ip}:8844/message", "http://127.0.0.1:8844/message"]
        for url in urls:
            try:
                req = urllib.request.Request(url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=3)
                return {"delivered": True, "url": url}
            except Exception:
                continue
        return {"delivered": False}

    async def _send_p4_seed(self, target: str, message: dict) -> dict:
        """P4: Seed file -- local (session-aware) or via SSH.

        Local: resolves to per-session seed file when sessions are detected.
        Remote: falls through to P5 SSH logic.
        """
        payload = message.get("payload", {})
        text = f"# Fleet: {payload.get('subject','')}\nFrom: {message.get('from','?')}\n\n{payload.get('body','')}\n"
        if target == self.node_id:
            seed_path = self._resolve_seed_path(message)
            Path(seed_path).write_text(text)
            return {"delivered": True, "seedPath": seed_path}
        # Remote via SSH (falls through to P5 logic)
        return {"delivered": False}

    async def _run_subprocess_offloaded(self, *args, **kwargs):
        """WaveJ 2026-05-12 — root-cause fix for /message wedge.

        Offloads synchronous ``subprocess.run`` to a worker thread via
        ``asyncio.to_thread`` so the asyncio main loop stays responsive
        under concurrent send load. Without this wrapper, P5_ssh / P7_git
        coroutines blocked the event loop for up to 8-30s per call —
        starving every other coroutine including the 49 sibling
        ``/message`` handlers waiting on
        ``run_coroutine_threadsafe(...).result(timeout=20)``, the NATS
        connect-retry loop (``nats_connect_retry_total`` stayed at 0
        even after 250s of ``client_status: closed``), and the /health
        builder (observed 9s response times).

        ZSF: every successful offload bumps ``subprocess_offloaded_total``;
        every offload exception bumps ``subprocess_offload_errors``. Both
        surface on /health.stats so the wedge cannot recur silently.
        """
        try:
            result = await asyncio.to_thread(subprocess.run, *args, **kwargs)
            self._stats["subprocess_offloaded_total"] = (
                self._stats.get("subprocess_offloaded_total", 0) + 1
            )
            return result
        except Exception:
            self._stats["subprocess_offload_errors"] = (
                self._stats.get("subprocess_offload_errors", 0) + 1
            )
            raise

    async def _send_p5_ssh(self, target: str, message: dict) -> dict:
        """P5: SSH -- curl the target's daemon. Tries direct IP, then tunnel.

        CV-001 fixes:
        - Removed StrictHostKeyChecking=no (use known_hosts)
        - Payload sent via stdin (not shell interpolation) to prevent injection
        - Seed file write uses base64 to avoid shell metachar injection
        """
        peer = self._resolve_peer(target)
        user, ip = peer.get("user"), peer.get("ip")
        tunnel_port = peer.get("tunnel_port")
        if not user or not ip:
            return {"delivered": False}
        # Validate target is alphanumeric (prevent path traversal)
        if not target.replace("-", "").replace("_", "").isalnum():
            logger.warning(f"P5 SSH: rejected invalid target name: {target}")
            return {"delivered": False}
        payload_bytes = json.dumps(message).encode()
        # Try SSH to direct IP first -- pipe payload via stdin to avoid shell injection
        try:
            # WaveJ: offload to thread so the asyncio loop doesn't block
            # for the full SSH timeout (was a primary wedge contributor).
            result = await self._run_subprocess_offloaded(
                ["ssh", "-o", "ConnectTimeout=3",
                 f"{user}@{ip}",
                 f"curl -sf 127.0.0.1:{FLEET_DAEMON_PORT}/message -H 'Content-Type: application/json' -d @-"],
                input=payload_bytes, capture_output=True, timeout=8)
            if result.returncode == 0:
                return {"delivered": True, "method": "ssh_direct"}
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug(f"_send_p5_ssh: ssh_direct to {target} failed (falling back): {e}")
        # Try via tunnel port (HTTP through SSH tunnel)
        if tunnel_port:
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"http://127.0.0.1:{tunnel_port}/message",
                    data=payload_bytes,
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=3)
                return {"delivered": True, "method": "ssh_tunnel"}
            except (OSError, ValueError) as e:
                logger.debug(f"_send_p5_ssh: ssh_tunnel to {target}:{tunnel_port} failed (falling back): {e}")
        # Fallback: write seed file via SSH using base64 to prevent injection
        # Session-aware: use _target_session if set, otherwise per-node fallback
        try:
            import base64
            payload = message.get("payload", {})
            seed_text = f"Fleet: {payload.get('subject','')}\n{payload.get('body','')}"
            b64 = base64.b64encode(seed_text.encode()).decode()
            # Determine seed file name -- per-session if targeted, per-node otherwise
            target_session = message.get("_target_session")
            if target_session:
                safe_sid = "".join(c for c in target_session if c.isalnum() or c in "-_")
                seed_name = f"fleet-seed-{safe_sid}.md" if safe_sid else f"fleet-seed-{target}.md"
            else:
                seed_name = f"fleet-seed-{target}.md"
            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=3", f"{user}@{ip}",
                 f"echo '{b64}' | base64 -d > /tmp/{seed_name}"],
                capture_output=True, timeout=8)
            return {"delivered": True, "method": "seed_via_ssh", "seedFile": seed_name}
        except Exception:
            return {"delivered": False}

    async def _send_p6_wol(self, target: str, message: dict) -> dict:
        """P6: Wake-on-LAN -- wake sleeping machine."""
        peer = self._resolve_peer(target)
        mac_addr = peer.get("mac_address")
        if not mac_addr:
            return {"delivered": False}
        try:
            mac_bytes = bytes.fromhex(mac_addr.replace(":", ""))
            magic = b'\xff' * 6 + mac_bytes * 16
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic, ('<broadcast>', 9))
            sock.close()
            logger.info(f"WoL sent to {target} ({mac_addr})")
            return {"delivered": False, "wol_sent": True}
        except Exception:
            return {"delivered": False}

    async def _send_p7_git(self, target: str, message: dict) -> dict:
        """P7: Git push -- guaranteed delivery, works at any scale."""
        try:
            inbox_dir = REPO_ROOT / "fleet-inbox" / target
            inbox_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            msg_file = inbox_dir / f"{ts}-{message.get('id','msg')[:8]}.json"
            msg_file.write_text(json.dumps(message, indent=2))
            # WaveJ: 3× git ops offloaded to thread — combined wall time up
            # to 30s. Running these inline blocked every coroutine for the
            # duration (root cause of /message uniform 20s wedge).
            await self._run_subprocess_offloaded(
                ["git", "add", str(msg_file)], cwd=str(REPO_ROOT), timeout=5)
            await self._run_subprocess_offloaded(
                ["git", "commit", "-m", f"fleet: P7 message to {target}"],
                cwd=str(REPO_ROOT), capture_output=True, timeout=10)
            await self._run_subprocess_offloaded(
                ["git", "push", "origin", "main"],
                cwd=str(REPO_ROOT), capture_output=True, timeout=15)
            return {"delivered": True}
        except Exception:
            return {"delivered": False}

    # ── Reliability-ranked channel ordering ──

    def _record_channel_attempt(self, peer_id: str, channel: str, ok: bool) -> None:
        """Update per-peer per-channel success/fail counters.

        On success: increment ok, stamp last_ok_ts = now.
        On failure/timeout: increment fail.
        """
        if not peer_id or peer_id == self.node_id:
            return
        bucket = self._channel_success_rate.setdefault(peer_id, {})
        stats = bucket.setdefault(channel, {"ok": 0, "fail": 0, "last_ok_ts": 0.0})
        if ok:
            stats["ok"] = stats.get("ok", 0) + 1
            stats["last_ok_ts"] = time.time()
        else:
            stats["fail"] = stats.get("fail", 0) + 1

    def _channel_reliability_score(self, peer_id: str, channel: str) -> float:
        """Compute reliability score for (peer, channel). Higher = prefer earlier.

        Formula: score = recent_bonus + (ok / max(ok+fail, 1)) - fail_penalty
        - recent_bonus: +2.0 if last success within 60s
        - base rate:    ok / (ok + fail)  ∈ [0, 1]
        - fail_penalty: min(fail * 0.05, 1.0), capped
        No history → returns 0.0 so static order wins.
        """
        stats = self._channel_success_rate.get(peer_id, {}).get(channel)
        if not stats:
            return 0.0
        ok = stats.get("ok", 0)
        fail = stats.get("fail", 0)
        last_ok_ts = stats.get("last_ok_ts", 0.0)
        recent_bonus = 2.0 if (last_ok_ts and time.time() - last_ok_ts < 60) else 0.0
        total = ok + fail
        base_rate = (ok / total) if total > 0 else 0.0
        fail_penalty = min(fail * 0.05, 1.0)
        return recent_bonus + base_rate - fail_penalty

    def _order_channels_by_reliability(self, peer_id: str, channels: list) -> list:
        """Sort (name, sender) pairs by reliability score (desc), stable on ties.

        Channels with no history keep their static order (score 0.0 — stable sort).
        """
        # Enumerate to preserve static order as tiebreaker
        scored = [
            (self._channel_reliability_score(peer_id, name), idx, name, sender)
            for idx, (name, sender) in enumerate(channels)
        ]
        # Higher score first, then original index (stable order for no-history peers)
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [(name, sender) for _, _, name, sender in scored]

    def _channel_fail_rate(self, peer_id: str, channel: str) -> float:
        """Return fail_rate in [0, 1] for (peer, channel). 0 if no history."""
        stats = self._channel_success_rate.get(peer_id, {}).get(channel)
        if not stats:
            return 0.0
        ok = stats.get("ok", 0)
        fail = stats.get("fail", 0)
        total = ok + fail
        if total == 0:
            return 0.0
        return fail / total

    async def send_with_fallback(self, target: str, message: dict, use_cloud: bool = False,
                                  use_discord_p0: bool = False) -> dict:
        """9-priority fallback: P0 Cloud -> P1 NATS -> P2 HTTP -> P3 Chief -> P4 Seed -> P5 SSH -> P6 WoL -> P7 Git -> P8 Keystroke.

        Delegates to CommunicationProtocol.send() which enforces:
        - 30s timeout per channel (invariant)
        - Channel state tracking (skip known-broken, auto-retry via self-heal)
        - P8 keystroke as absolute last resort (rate-limited 1/30s)
        - Secrets protection (never logs message content)

        P0 Discord (opt-in, never automatic):
        - Appended AFTER P7 git as an explicit emergency last-resort channel
        - Fires only when use_discord_p0=True OR message.priority == "emergency"
        - Additionally gated by _should_use_p0_discord (peer offline + msg type)
        - Rate-limited via self.rate_limiter bucket "p0_discord"
        - Tracked in self._stats["p0_discord_sends"] for observability
        """
        # ── FleetSendGate invariance check (envelope, schema, quorum, stress) ──
        # Gate skipped gracefully when multifleet.invariance_gates is unavailable
        # (slim fleet installs). When wired, validates envelope, schema, rate
        # limit, HMAC, target-known, quorum + stress policy BEFORE any channel.
        if self._send_gate is not None:
            try:
                # Build a packet view for the gate. Callers pass partial messages
                # (id/from/timestamp are added later in this method), so inject
                # defaults matching what we would publish downstream.
                gate_packet = dict(message)
                if "to" not in gate_packet and "to_node" not in gate_packet:
                    gate_packet["to"] = target
                if "from" not in gate_packet and "from_node" not in gate_packet:
                    gate_packet["from"] = self.node_id
                if "id" not in gate_packet:
                    gate_packet["id"] = str(uuid.uuid4())
                    message.setdefault("id", gate_packet["id"])
                stress = 0.0
                if compute_stress_score is not None:
                    try:
                        stress = compute_stress_score()
                    except Exception:
                        stress = 0.0
                gate_result = await asyncio.to_thread(self._send_gate.check, gate_packet, stress_score=stress)
                if not gate_result.passed:
                    self._stats["gates_blocked"] += 1
                    reason = gate_result.summary
                    logger.warning(
                        f"FleetSendGate BLOCKED send to {target}: {reason}"
                    )
                    # Surface gate block to Discord (gated — only when nc connected).
                    # HMAC-signed envelope (mf-1 priority audit 2026-04-26):
                    # subscribers' verify_message gracefully accepts unsigned, so
                    # this hardens new path without breaking existing receivers.
                    if self.nc and self.nc.is_connected:
                        try:
                            payload = _sign_envelope({
                                "node": self.node_id,
                                "subject": f"🚨 FleetSendGate blocked send",
                                "body": f"Target: {target}\nReason: {reason}\nMessage type: {message.get('type','?')}",
                                "type": "alert",
                            }, self.node_id)
                            await self.nc.publish("fleet.discord.outbound", payload)
                        except (OSError, ValueError, TypeError, AttributeError) as pub_err:
                            # ZSF (race/b1): gate block return path is critical —
                            # a Discord notify failure must never prevent the
                            # gate from blocking. Counter surfaces Discord
                            # outage; block still enforced.
                            self._stats["gate_discord_publish_errors"] = (
                                self._stats.get("gate_discord_publish_errors", 0) + 1
                            )
                            logger.warning(
                                "FleetSendGate Discord notify failed: %s — block still enforced",
                                pub_err,
                            )
                    return {
                        "delivered": False,
                        "errors": [f"gate_blocked:{reason}"],
                        "channel": "none",
                    }
            except Exception as e:
                # Never let gate errors break sends -- log and continue (ZSF: logged)
                logger.warning(f"FleetSendGate check errored (degraded): {e}")

        channels = []
        if use_cloud:
            channels.append(("P0_cloud", self._send_p0_cloud))
        channels.extend([
            ("P1_nats", self._send_p1_nats),
            ("P2_http", self._send_p2_http),
            ("P3_chief", self._send_p3_chief),
            ("P4_seed", self._send_p4_seed),
            ("P5_ssh", self._send_p5_ssh),
            ("P6_wol", self._send_p6_wol),
        ])
        # P7_git creates commits — NEVER use for repair/self-heal (causes flood)
        msg_type = message.get("type", "")
        is_repair = msg_type == "repair" or "self-heal" in str(message.get("payload", {}).get("subject", ""))
        if not is_repair:
            channels.append(("P7_git", self._send_p7_git))

        # RACE A3 — cascade order is gated by FLEET_CHANNEL_ORDER:
        #   "strict"      → always P0→P7 in priority order (spec-compliant default).
        #   "reliability" → reorder by per-peer per-channel success history so peers
        #                   with observed success on e.g. P2 see P2 tried before P1.
        # Skip-broken logic (handled inside the per-channel loop / known_broken state)
        # applies in BOTH modes — strict mode still skips audit-marked broken channels.
        # The difference is ORDER, not COMPLETENESS.
        if self._channel_order_mode == "reliability":
            channels = self._order_channels_by_reliability(target, channels)
        # else: strict — preserve P0→P7 priority order as built above.

        # P0_discord — opt-in LAST RESORT (after P7 git). Never automatic.
        # Fires only when caller passes use_discord_p0=True OR message has
        # priority="emergency". The _send_p0_discord method re-checks
        # _should_use_p0_discord as a safety gate (peer offline + msg type + rate).
        priority = message.get("priority", "")
        wants_discord = use_discord_p0 or priority == "emergency"
        if wants_discord:
            channels.append(("P0_discord", self._send_p0_discord))

        # ── Per-peer outbound rate limiting (OSS blocker #5) ──
        outbound_type = msg_type or "message"
        if not self.rate_limiter.allow(target, outbound_type):
            logger.warning(f"Rate limited OUTBOUND {outbound_type} to {target} -- skipping send")
            return {"delivered": False, "error": f"rate_limited:{outbound_type}", "channel": "none"}

        # Ensure message has an ID
        if "id" not in message:
            message["id"] = str(uuid.uuid4())
        if "from" not in message:
            message["from"] = self.node_id
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat() + "Z"

        # Per-channel timeout budget (total 30s max across all channels)
        CHANNEL_TIMEOUTS = {
            "P0_cloud": 5, "P1_nats": 2, "P2_http": 3, "P3_chief": 3,
            "P4_seed": 2, "P5_ssh": 8, "P6_wol": 3, "P7_git": 10,
            "P0_discord": 10,  # Discord REST POST can be slow under load
        }
        TOTAL_BUDGET = 30.0
        budget_start = time.time()

        # Store message and mark awaiting ACK (skip for ack messages and heartbeats)
        if self._store and message.get("type") not in ("ack", "heartbeat"):
            existing = self._store.get_ack_status(message["id"])
            if not existing:
                self._store.store_message(message)
                self._store.update_ack_status(message["id"], "awaiting")

        errors = []
        for name, sender in channels:
            # Check total budget
            elapsed = time.time() - budget_start
            if elapsed >= TOTAL_BUDGET:
                errors.append(f"{name}: skipped (30s budget exhausted)")
                logger.info(f"[{name}] Skipped -- 30s budget exhausted after {elapsed:.0f}s")
                continue

            try:
                # Base timeout, halved when fail_rate > 50% so degraded channels
                # skip faster instead of eating the full budget.
                base_timeout = CHANNEL_TIMEOUTS.get(name, 5)
                if self._channel_fail_rate(target, name) > 0.5:
                    channel_timeout = max(1, base_timeout // 2)
                else:
                    channel_timeout = base_timeout
                result = await asyncio.wait_for(sender(target, message), timeout=channel_timeout)
                if result.get("delivered"):
                    logger.info(f"[{name}] Delivered to {target} ({time.time()-budget_start:.1f}s)")
                    self._update_channel_health(target, name, "ok")
                    self._record_channel_attempt(target, name, ok=True)
                    # Track for ACK confirmation
                    msg_id = message.get("id")
                    if msg_id and target != "all":
                        # K2 2026-05-04 — event-driven tracker. Schedules a
                        # per-msg deadline task; ACK handler signals it on
                        # arrival. Watchdog tick is now fallback-only.
                        self._track_pending_ack(msg_id, target, message)
                    # Self-heal: if we succeeded on P3+, earlier channels are broken
                    if name not in ("P1_nats", "P2_http"):
                        broken = [e.split(":")[0] for e in errors]
                        self._attempt_self_heal(target, name, broken)
                    return {"delivered": True, "channel": name, "elapsed_s": round(time.time()-budget_start, 1), **result}
                if result.get("wol_sent"):
                    logger.info(f"[{name}] WoL sent to {target}")
                    errors.append(f"{name}: WoL sent, queued for retry")
                    continue
                # ZSF: always log why a channel didn't deliver (no silent failures)
                reason = result.get("error", "delivered=false")
                errors.append(f"{name}: {reason}")
                self._update_channel_health(target, name, "broken")
                self._record_channel_attempt(target, name, ok=False)
                logger.debug(f"[{name}] not delivered to {target}: {reason}")
            except asyncio.TimeoutError:
                errors.append(f"{name}: timeout ({channel_timeout}s)")
                self._update_channel_health(target, name, "broken")
                self._record_channel_attempt(target, name, ok=False)
                logger.debug(f"[{name}] timed out for {target} after {channel_timeout}s")
            except Exception as e:
                errors.append(f"{name}: {e}")
                self._update_channel_health(target, name, "broken")
                self._record_channel_attempt(target, name, ok=False)
                logger.debug(f"[{name}] failed for {target}: {e}")

        # P0 cloud DISABLED as automatic fallback — spawns cloud Claude sessions
        # with no human approval or rate limit. Only used when explicitly requested
        # via use_cloud=True (which prepends P0 to the channel list above).

        # ── EEE3 peer-relay fallback (opt-in, FLEET_PEER_RELAY_ENABLED=1) ──
        # All direct channels exhausted. If the feature flag is on AND we
        # have a known-healthy sibling, ask that sibling to relay this
        # message. The relay RPC is best-effort and short-budget — peer
        # may decline (rate-limited, offline, or no path to target).
        if self._cascade_peer_relay_enabled and target and target != "all" and target != self.node_id:
            healthy = self._healthy_relay_candidates(target)
            if healthy:
                try:
                    from multifleet import channel_priority as _cp
                    relay_res = _cp.try_peer_relay(
                        target, message,
                        healthy_peers=healthy,
                        relay_fn=self._invoke_peer_relay_rpc,
                    )
                except Exception as exc:  # noqa: BLE001 — ZSF
                    relay_res = {"delivered": False, "via": None, "error": f"exc:{exc}"}
                    logger.warning(
                        f"cascade peer-relay raised for {target}: {exc}"
                    )
                if isinstance(relay_res, dict) and relay_res.get("delivered"):
                    via = relay_res.get("via")
                    logger.info(
                        f"cascade peer-relay delivered to {target} via {via}"
                    )
                    return {
                        "delivered": True,
                        "channel": "peer_relay",
                        "via_peer": via,
                        "elapsed_s": round(time.time() - budget_start, 1),
                    }
                # not delivered — fall through to the final failure return,
                # but tag the trail so /health.recent_errors shows the path.
                if isinstance(relay_res, dict):
                    errors.append(f"peer_relay: {relay_res.get('error','exhausted')}")
                else:
                    errors.append("peer_relay: non_dict_return")

        logger.warning(f"All channels failed for {target}: {errors}")
        # race/a1-immediate-audit: fire event-driven audit on exhaustion.
        # Previously this left the fleet waiting up to 30s for the next
        # _watchdog_loop tick. Now we re-probe every channel in < 100ms.
        # Never audit broadcast target or self.
        if target and target != "all" and target != self.node_id:
            try:
                self.trigger_immediate_audit(target, reason="send_exhausted")
            except Exception as e:
                # ZSF: observable, not swallowed. Do NOT block the return.
                logger.warning(f"trigger_immediate_audit failed for {target}: {e}")
                self._stats["audit_errors"] = self._stats.get("audit_errors", 0) + 1
        return {"delivered": False, "errors": errors}

    # ── EEE3 peer-relay helpers (trust boundary + RPC wrapper) ──

    def _healthy_relay_candidates(self, target: str) -> list:
        """Return peers from fleet config that look healthy enough to relay.

        Trust boundary: only peers in ``self._peers_config`` (i.e. real
        fleet members) are eligible. A peer is "healthy enough" when we
        have observed a heartbeat from it within RELAY_HEALTHY_LASTSEEN_S.
        Cold callers (no heartbeats seen) get an empty list — we won't
        try to relay through an unknown node.
        """
        cfg_peers = self._peers_config or {}
        if not cfg_peers:
            return []
        healthy: list = []
        now = time.time()
        threshold_s = 180.0  # 3 min — conservative; tunable later
        for pid in cfg_peers.keys():
            if pid == self.node_id or pid == target:
                continue
            hb = self._peers.get(pid)
            if not hb:
                continue
            last_seen = now - hb.get("_received_at", 0)
            if last_seen <= threshold_s:
                healthy.append(pid)
        return healthy

    def _invoke_peer_relay_rpc(self, via: str, target: str, message: dict) -> dict:
        """Synchronous wrapper around rpc.<via>.cascade_relay.

        Returns ``{"delivered": bool, "error": str|None}``. Never raises.
        Used as the ``relay_fn`` for ``channel_priority.try_peer_relay``.
        """
        if not self.nc or not getattr(self.nc, "is_connected", False):
            return {"delivered": False, "error": "nats_disconnected"}
        if not self._loop:
            return {"delivered": False, "error": "no_loop"}
        try:
            payload = json.dumps({
                "target": target,
                "message": message,
                "origin": self.node_id,
            }).encode()
        except Exception as exc:  # noqa: BLE001 — ZSF
            return {"delivered": False, "error": f"encode:{exc}"}
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.request_peer(via, "cascade_relay", payload, timeout=5.0),
                self._loop,
            )
            data = fut.result(timeout=6.0)
        except Exception as exc:  # noqa: BLE001 — ZSF
            return {"delivered": False, "error": f"rpc:{type(exc).__name__}"}
        try:
            reply = json.loads(data.decode()) if data else {}
        except Exception as exc:  # noqa: BLE001 — ZSF
            return {"delivered": False, "error": f"decode:{exc}"}
        if isinstance(reply, dict) and reply.get("delivered"):
            return {"delivered": True, "error": None}
        err = "relay_declined"
        if isinstance(reply, dict) and reply.get("error"):
            err = str(reply["error"])
        elif not isinstance(reply, dict):
            err = "non_dict"
        return {"delivered": False, "error": err}

    async def _handle_cascade_relay_request(self, raw_data: bytes) -> dict:
        """Handle an inbound rpc.<self>.cascade_relay request.

        A peer is asking us to forward ``message`` to ``target`` because
        its own cascade exhausted toward ``target``. We re-enter
        ``send_with_fallback`` with the relay path DISABLED so two-hop
        traffic cannot trigger a third hop (ping-pong prevention).

        ZSF — every error path returns a deterministic dict with an
        ``error`` tag; never raises into the NATS handler.
        """
        try:
            data = json.loads(raw_data.decode()) if raw_data else {}
        except Exception as exc:  # noqa: BLE001 — ZSF
            self._stats["cascade_relay_parse_errors"] = (
                self._stats.get("cascade_relay_parse_errors", 0) + 1
            )
            return {"delivered": False, "error": f"parse:{exc}"}

        target = data.get("target")
        message = data.get("message")
        origin = data.get("origin")
        if not target or not isinstance(message, dict):
            return {"delivered": False, "error": "bad_request"}

        # Trust boundary: origin must be a known peer.
        if origin and origin not in (self._peers_config or {}):
            self._stats["cascade_relay_untrusted_origin"] = (
                self._stats.get("cascade_relay_untrusted_origin", 0) + 1
            )
            return {"delivered": False, "error": "untrusted_origin"}

        # Ping-pong prevention: a relay request must not itself trigger
        # another relay hop. Temporarily flip the flag for this call.
        was_enabled = self._cascade_peer_relay_enabled
        self._cascade_peer_relay_enabled = False
        try:
            result = await self.send_with_fallback(target, message)
        except Exception as exc:  # noqa: BLE001 — ZSF
            self._stats["cascade_relay_exceptions"] = (
                self._stats.get("cascade_relay_exceptions", 0) + 1
            )
            return {"delivered": False, "error": f"send_exc:{exc}"}
        finally:
            self._cascade_peer_relay_enabled = was_enabled

        if isinstance(result, dict) and result.get("delivered"):
            self._stats["cascade_relay_fulfilled"] = (
                self._stats.get("cascade_relay_fulfilled", 0) + 1
            )
            return {
                "delivered": True,
                "channel": result.get("channel"),
                "error": None,
            }
        self._stats["cascade_relay_declined"] = (
            self._stats.get("cascade_relay_declined", 0) + 1
        )
        return {"delivered": False, "error": "no_path"}

    def trigger_immediate_audit(self, target: str, reason: str = "unspecified") -> bool:
        """Public wrapper: fire an immediate all-channel audit for ``target``.

        Delegates to ``self.protocol.trigger_immediate_audit``. Safe to call
        from sync or async contexts. Returns False if debounced, rejected
        (self/broadcast), or if no protocol is wired.
        """
        if self.protocol is None:
            return False
        return self.protocol.trigger_immediate_audit(target, reason=reason)

    async def _send_p0_cloud(self, target: str, message: dict) -> dict:
        """P0: Cloud dispatch via RemoteTrigger API. Spawns isolated Claude Code in Anthropic cloud."""
        try:
            from tools.fleet_nerve_cloud import send_cloud
        except ImportError:
            return {"delivered": False}

        prompt = message.get("payload", {}).get("body", "")
        if not prompt:
            prompt = json.dumps(message.get("payload", {}))

        return await send_cloud(
            prompt=prompt,
            payload=message,
        )

    # ── P0 Discord (opt-in emergency last-resort) ──

    # Allowed message types for P0 Discord (gates out noisy heartbeat/ack)
    P0_DISCORD_ALLOWED_TYPES = {"alert", "urgent", "critique"}

    # Peer is considered offline for Discord-gate purposes when:
    #   last NATS heartbeat older than this OR
    #   all primary channels have been broken
    P0_DISCORD_OFFLINE_LASTSEEN_S = 900   # 15 min — peer lastSeen threshold
    P0_DISCORD_CHANNELS_BROKEN_S = 1800   # 30 min — all-channels-broken threshold

    def _should_use_p0_discord(self, target: str, message: dict) -> tuple:
        """Approval gate for P0 Discord. Returns (allowed: bool, reason: str).

        All conditions must pass:
          1. Peer is known offline: lastSeen > 900s OR all primary channels broken
          2. Message type is NOT heartbeat/ack (noise filter)
          3. Message type IS in {alert, urgent, critique} OR priority=="emergency"
          4. Not rate-limited (self.rate_limiter, bucket "p0_discord")
        """
        msg_type = message.get("type", "")
        priority = message.get("priority", "")

        # (2) Noise filter — never Discord for heartbeat/ack
        if msg_type in ("heartbeat", "ack"):
            return False, f"noise_type:{msg_type}"

        # (3) Must be severe type OR explicit emergency priority
        is_severe = msg_type in self.P0_DISCORD_ALLOWED_TYPES
        is_emergency = priority == "emergency"
        if not (is_severe or is_emergency):
            return False, f"not_severe:type={msg_type},priority={priority}"

        # (1) Peer must be known-offline — never burn Discord API on healthy peers
        peer = self._peers.get(target)
        now = time.time()
        last_seen_s = None
        if peer:
            last_seen_s = now - peer.get("_received_at", 0)

        ch_map = self._channel_health.get(target, {})
        primary = ("P1_nats", "P2_http", "P3_chief", "P4_seed", "P5_ssh", "P6_wol", "P7_git")
        primary_states = [ch_map.get(c) for c in primary if c in ch_map]
        all_broken = bool(primary_states) and all(s == "broken" for s in primary_states)

        peer_stale = last_seen_s is not None and last_seen_s > self.P0_DISCORD_OFFLINE_LASTSEEN_S
        peer_unknown = peer is None  # Never heard — offline only for explicit emergencies
        offline_condition_met = peer_stale or all_broken or (peer_unknown and is_emergency)

        if not offline_condition_met:
            return False, (
                f"peer_online:lastSeen={int(last_seen_s) if last_seen_s is not None else 'n/a'}s,"
                f"all_broken={all_broken}"
            )

        # (4) Rate-limit bucket — dedicated "p0_discord" bucket
        if not self.rate_limiter.allow(target, "p0_discord"):
            return False, "rate_limited:p0_discord"

        return True, "ok"

    async def _send_p0_discord(self, target: str, message: dict) -> dict:
        """P0 Discord: opt-in emergency last-resort REST POST to Discord channel.

        Reuses bot-token + channel-id pattern from
        multifleet.discord_bridge.report_to_discord (module-level helper). Posts a
        node-colored embed so offline-peer alerts reach the human operator.

        SAFETY:
        - Never auto-triggered — caller must pass use_discord_p0=True OR set
          message.priority="emergency" in send_with_fallback.
        - Internal _should_use_p0_discord gate enforces: peer offline, allowed
          msg type, rate limit. Rejections are logged (ZSF — no silent failures).
        - Missing token or channel-id → graceful degrade (delivered=False, logged).

        Returns {"delivered": bool, "channel": "P0_discord", "method": "discord_rest",
                 "error": str}.
        """
        # ── Approval gate ──
        allowed, reason = self._should_use_p0_discord(target, message)
        if not allowed:
            logger.info(f"[P0_discord] REJECTED for {target}: {reason}")
            return {
                "delivered": False,
                "channel": "P0_discord",
                "method": "discord_rest",
                "error": f"gate_rejected:{reason}",
            }

        # ── Credentials ──
        try:
            from multifleet.discord_bridge import (
                get_bot_token, validate_bot_token, validate_channel_id,
                _NODE_COLORS, _DEFAULT_COLOR,
            )
        except ImportError as e:
            logger.warning(f"[P0_discord] discord_bridge unavailable: {e}")
            return {
                "delivered": False,
                "channel": "P0_discord",
                "method": "discord_rest",
                "error": f"import_failed:{e}",
            }

        token = get_bot_token()
        channel_id = os.environ.get("FLEET_DISCORD_CHANNEL_ID", "")
        # Optional config fallback (best-effort — Agent C owns config loading)
        if not channel_id:
            conf = getattr(self, "_peers_config", None)
            if isinstance(conf, dict):
                channel_id = conf.get("_discord_channel_id", "") or ""

        if not validate_bot_token(token):
            logger.warning("[P0_discord] bot token missing/invalid — graceful degrade")
            return {
                "delivered": False,
                "channel": "P0_discord",
                "method": "discord_rest",
                "error": "missing_token",
            }
        if not validate_channel_id(channel_id):
            logger.warning("[P0_discord] FLEET_DISCORD_CHANNEL_ID missing/invalid — graceful degrade")
            return {
                "delivered": False,
                "channel": "P0_discord",
                "method": "discord_rest",
                "error": "missing_channel_id",
            }

        # ── Build node-colored embed ──
        source = message.get("from", self.node_id)
        msg_type = message.get("type", "context")
        payload = message.get("payload") or {}
        subject = payload.get("subject") or f"[{msg_type.upper()}] {source} -> {target}"
        body = payload.get("body") or json.dumps(payload)[:1500]
        color = _NODE_COLORS.get(source, _DEFAULT_COLOR)

        embed = {
            "title": f"[P0] {subject}"[:256],
            "description": f"**{source}** -> **{target}** | {msg_type} (emergency last-resort)"[:4096],
            "color": color,
            "fields": [
                {"name": "Details", "value": str(body)[:1024], "inline": False},
                {"name": "Node", "value": str(target), "inline": True},
                {"name": "Type", "value": msg_type, "inline": True},
            ],
            "footer": {"text": f"P0 Discord | {datetime.now(timezone.utc).isoformat()}"},
        }
        rest_payload = json.dumps({"embeds": [embed]}).encode()

        # ── POST (blocking urllib in executor) ──
        try:
            import urllib.request
            req = urllib.request.Request(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                data=rest_payload,
                headers={
                    "Authorization": f"Bot {token}",
                    "Content-Type": "application/json",
                    "User-Agent": "fleet-nerve-nats/P0_discord",
                },
                method="POST",
            )
            loop = asyncio.get_event_loop()
            resp = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10)),
                timeout=10,
            )
            status_code = getattr(resp, "status", 200)
            if 200 <= status_code < 300:
                self._stats["p0_discord_sends"] = self._stats.get("p0_discord_sends", 0) + 1
                logger.info(
                    f"[P0_discord] Delivered target={target} type={msg_type} "
                    f"(p0_discord_sends={self._stats['p0_discord_sends']})"
                )
                return {
                    "delivered": True,
                    "channel": "P0_discord",
                    "method": "discord_rest",
                    "http_status": status_code,
                    "error": "",
                }
            logger.warning(f"[P0_discord] HTTP {status_code} from Discord")
            return {
                "delivered": False,
                "channel": "P0_discord",
                "method": "discord_rest",
                "error": f"http_{status_code}",
            }
        except asyncio.TimeoutError:
            logger.warning("[P0_discord] timeout after 10s")
            return {
                "delivered": False,
                "channel": "P0_discord",
                "method": "discord_rest",
                "error": "timeout",
            }
        except Exception as e:
            logger.warning(f"[P0_discord] REST POST failed: {e}")
            return {
                "delivered": False,
                "channel": "P0_discord",
                "method": "discord_rest",
                "error": str(e),
            }


    # ── send_smart -- session-aware routing ──

    async def send_smart(self, target: str, message: dict) -> dict:
        """Session-aware message routing using NATS peer health cache.

        Routes by type:
          task    -> prefer idle session (idle > 5min)
          context/reply -> primary (most recently active) session
          alert   -> ALL sessions + macOS notification
        Falls back to send_with_fallback() for actual delivery.
        """
        t0 = time.time()
        msg_type = message.get('type', 'context')
        message.setdefault('id', str(uuid.uuid4()))
        message.setdefault('from', self.node_id)
        message.setdefault('to', target)
        message.setdefault('timestamp', datetime.now(timezone.utc).isoformat() + 'Z')

        # Probe target via NATS heartbeat cache
        peer = self._peers.get(target)
        if not peer or time.time() - peer.get('_received_at', 0) > 120:
            result = await self.send_with_fallback(target, message)
            result.update(routing='no_peer_health', session_target=None,
                          elapsed_s=round(time.time() - t0, 2))
            return result

        sessions = peer.get('sessions', [])
        routing = 'direct'
        session_target = None

        if msg_type == 'alert':
            routing = 'alert_all_sessions'
            session_target = f'all({len(sessions)})'

        elif msg_type == 'task':
            idle = [(s.get('idle_s', 0), s) for s in sessions
                    if s.get('idle_s', 0) > 300]
            idle.sort(key=lambda x: x[0], reverse=True)
            if idle:
                chosen = idle[0][1]
                session_target = chosen.get('sessionId', '?')[:12]
                message['_target_session'] = session_target
                routing = 'task_to_idle_session'
            else:
                routing = 'task_no_idle'

        elif msg_type in ('context', 'reply'):
            if sessions:
                primary = min(sessions, key=lambda s: s.get('idle_s', 9999))
                session_target = primary.get('sessionId', '?')[:12]
                message['_target_session'] = session_target
                routing = 'context_to_primary'

        result = await self.send_with_fallback(target, message)
        result.update(routing=routing, session_target=session_target,
                      elapsed_s=round(time.time() - t0, 2))
        logger.info(f'send_smart: {target} type={msg_type} routing={routing} '
                     f'session={session_target} delivered={result.get("delivered")}')
        return result

    def _update_channel_health(self, target: str, channel: str, status: str):
        """Track per-peer channel health. Status: 'ok' or 'broken'."""
        if target not in self._channel_health:
            self._channel_health[target] = {ch: "ok" for ch in ALL_CHANNELS}
        self._channel_health[target][channel] = status

    def _get_peer_health_score(self, target: str) -> tuple:
        """Returns (score, broken_list) for a peer. Max score = len(ALL_CHANNELS)."""
        ch = self._channel_health.get(target, {})
        broken = [name for name in ALL_CHANNELS if ch.get(name) == "broken"]
        score = len(ALL_CHANNELS) - len(broken)
        return score, broken

    def _attempt_self_heal(self, target: str, working_channel: str, broken: list):
        """Aggressive self-heal: immediately start repair_with_escalation for broken channels.

        Replaces the old L1-only notify. Now triggers full L1-L4 escalation,
        then re-probes each broken channel to verify the fix actually worked.
        Deduplicates: only one heal task per peer at a time.
        """
        if not broken:
            return
        # Don't stack heals -- one per peer
        existing = self._heal_in_flight.get(target)
        if existing and not existing.done():
            logger.debug(f"Self-heal already in flight for {target}, skipping")
            return
        score, _ = self._get_peer_health_score(target)
        action = "restart_nats" if "P1_nats" in broken else "check_connectivity"
        logger.info(f"Self-heal triggered for {target}: score={score}/{len(ALL_CHANNELS)}, broken={broken}")

        async def _heal_and_verify():
            try:
                result = await self.repair_with_escalation(target, broken, action)
                if result and result.get("repaired"):
                    # Re-probe each broken channel to verify repair
                    for ch in broken:
                        probe_ok = await self._verify_channel(target, ch)
                        self._update_channel_health(target, ch, "ok" if probe_ok else "broken")
                    new_score, still_broken = self._get_peer_health_score(target)
                    logger.info(f"Self-heal result for {target}: "
                                f"score {score}->{new_score}/{len(ALL_CHANNELS)}, "
                                f"still_broken={still_broken}")
                else:
                    logger.warning(f"Self-heal for {target} did not fully repair: {result}")
            except Exception as e:
                logger.warning(f"Self-heal task failed for {target}: {e}")
            finally:
                self._heal_in_flight.pop(target, None)

        self._heal_in_flight[target] = asyncio.ensure_future(_heal_and_verify())

    async def _verify_channel(self, target: str, channel: str) -> bool:
        """Probe a single channel to verify it works. Returns True if delivered."""
        probe_msg = {"type": "sync", "payload": {"subject": "ping"}, "from": self.node_id}
        sender_map = {
            "P1_nats": self._send_p1_nats, "P2_http": self._send_p2_http,
            "P3_chief": self._send_p3_chief, "P4_seed": self._send_p4_seed,
            "P5_ssh": self._send_p5_ssh, "P7_git": self._send_p7_git,
        }
        sender = sender_map.get(channel)
        if not sender:
            return False
        try:
            result = await asyncio.wait_for(sender(target, probe_msg), timeout=5)
            return bool(result.get("delivered"))
        except Exception:
            return False

    async def repair_with_escalation(self, target: str, broken_channels: list, action: str = "restart_nats"):
        """Multi-level repair escalation. Total time to L4: ~90s.

        Level 1 NOTIFY: Silent repair msg to target (30s wait)
        Level 2 GUIDE: Send specific repair command (30s wait)
        Level 3 ASSIST: Send auto-exec repair action (30s wait)
        Level 4 REMOTE: SSH direct intervention (~90s after detection)

        All repair messages use type:"repair" (NOT "context") to avoid
        seed file writes and keystroke injection spam on the target.
        Each level uses send_with_fallback() for delivery.
        """
        logger.info(f"Repair escalation for {target}: broken={broken_channels}, action={action}")

        # ── FleetRepairGate invariance check ──
        # Verifies: target actually unhealthy (not stale diagnostic), no concurrent
        # repair in progress, action in allowlist, L4 SSH only from chief, 5min
        # cooldown per target, stress-based destructive-action policy.
        # Skipped gracefully when multifleet.invariance_gates is unavailable.
        if self._repair_gate is not None:
            try:
                stress = 0.0
                if compute_stress_score is not None:
                    try:
                        stress = compute_stress_score()
                    except Exception:
                        stress = 0.0
                # Build channel_health snapshot for the gate.
                # ZSF-hardening: narrow bare-except. A silent swallow here
                # sent an empty ch_health to the gate, which could approve a
                # destructive repair on a misdiagnosed peer (same bug class
                # as the 55-repair-attempt heartbeat-stale incident).
                ch_health = {}
                try:
                    score, broken = self._get_peer_health_score(target)
                    ch_health[target] = {
                        "score": score,
                        "broken": broken,
                        "last_seen": self._peers.get(target, {}).get(
                            "_received_at", time.time()
                        ),
                    }
                except (KeyError, AttributeError, TypeError, ValueError) as e:
                    self._stats["repair_gate_health_errors"] = self._stats.get("repair_gate_health_errors", 0) + 1
                    logger.warning(
                        f"repair_gate: _get_peer_health_score failed for {target}: {e} -- "
                        f"gate will evaluate without channel_health snapshot"
                    )
                gate_result = self._repair_gate.check(
                    target=target,
                    action=action,
                    channel_health=ch_health or None,
                    node_id=self.node_id,
                    stress_score=stress,
                )
                if not gate_result.passed:
                    self._stats["gates_blocked"] += 1
                    failures = [
                        c.name for c in gate_result.checks
                        if not c.passed and getattr(c, "critical", True)
                    ]
                    logger.warning(
                        f"FleetRepairGate BLOCKED repair on {target} "
                        f"(action={action}): {failures} -- {gate_result.summary}"
                    )
                    # Surface to Discord — HMAC-signed envelope (mf-1 priority
                    # audit 2026-04-26). Subscribers' verify_message graceful-degrade.
                    if self.nc and self.nc.is_connected:
                        try:
                            payload = _sign_envelope({
                                "node": self.node_id,
                                "subject": f"🚨 FleetRepairGate blocked repair",
                                "body": f"Target: {target}\nAction: {action}\nFailures: {failures}\nSummary: {gate_result.summary}",
                                "type": "alert",
                            }, self.node_id)
                            await self.nc.publish("fleet.discord.outbound", payload)
                        except (OSError, ValueError, TypeError, AttributeError) as pub_err:
                            # ZSF (race/b1): repair-gate block must never
                            # be undone by a Discord notify failure. Counter
                            # surfaces Discord outage; block still enforced.
                            self._stats["gate_discord_publish_errors"] = (
                                self._stats.get("gate_discord_publish_errors", 0) + 1
                            )
                            logger.warning(
                                "FleetRepairGate Discord notify failed: %s — repair still blocked",
                                pub_err,
                            )
                    return {
                        "repaired": False,
                        "gate_blocked": True,
                        "failures": failures,
                        "summary": gate_result.summary,
                    }
            except Exception as e:
                # Never let gate errors block repair operations (ZSF: logged)
                logger.warning(f"FleetRepairGate check errored (degraded): {e}")

        # Per-peer rate limiter check (prevents repair storms -- OSS blocker #5)
        if not self.rate_limiter.allow(target, "repair"):
            logger.warning(f"Repair rate limited for {target} -- skipping escalation")
            return {"repaired": False, "error": "rate_limited"}

        # Level 1: NOTIFY -- tell target to fix itself (silent repair msg, no UI spam)
        logger.info(f"Repair L1 NOTIFY -> {target}")
        notify_msg = {
            "type": "repair", "from": self.node_id, "to": target,
            "payload": {
                "subject": "self-heal-notify",
                "body": json.dumps({
                    "action": "notify",
                    "reporter": self.node_id,
                    "broken_channels": broken_channels,
                    "level": 1,
                    "suggested": action,
                }),
            },
        }
        result = await self.send_with_fallback(target, notify_msg)
        if result.get("delivered"):
            logger.info(f"Repair L1 delivered via {result.get('channel')}. Waiting 30s for local fix...")
            await asyncio.sleep(30)
            probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
            if probe.get("delivered"):
                logger.info(f"Repair SUCCESS: {target} back on P1 after L1 notify")
                return {"repaired": True, "level": 1}

        # Level 2: GUIDE -- send specific repair command (still silent, no seed/keystroke)
        logger.info(f"Repair L2 GUIDE -> {target}")
        guide_msg = {
            "type": "repair", "from": self.node_id, "to": target,
            "payload": {
                "subject": "self-heal-guide",
                "body": json.dumps({
                    "action": action,
                    "reporter": self.node_id,
                    "broken_channels": broken_channels,
                    "level": 2,
                    "command": f"pkill -f fleet_nerve_nats.py && sleep 2 && "
                               f"MULTIFLEET_NODE_ID={target} nohup python3 tools/fleet_nerve_nats.py serve &",
                }),
            },
        }
        result = await self.send_with_fallback(target, guide_msg)
        if result.get("delivered"):
            logger.info(f"Repair L2 delivered via {result.get('channel')}. Waiting 30s...")
            await asyncio.sleep(30)
            probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
            if probe.get("delivered"):
                logger.info(f"Repair SUCCESS: {target} back on P1 after L2 guide")
                return {"repaired": True, "level": 2}

        # Level 3: ASSIST -- send auto-executable repair action (mac1's handlers)
        logger.info(f"Repair L3 ASSIST -> {target}")
        repair_msg = {
            "type": "repair", "from": self.node_id, "to": target,
            "payload": {
                "subject": "repair",
                "body": json.dumps({
                    "action": action,
                    "reporter": self.node_id,
                    "broken_channels": broken_channels,
                    "level": 3,
                }),
            },
        }
        result = await self.send_with_fallback(target, repair_msg)
        if result.get("delivered"):
            logger.info(f"Repair L3 delivered via {result.get('channel')}. Waiting 30s...")
            await asyncio.sleep(30)
            probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
            if probe.get("delivered"):
                logger.info(f"Repair SUCCESS: {target} back on P1 after L3 assist")
                return {"repaired": True, "level": 3}

        # Level 3.5: SSH PEER REACHABILITY RECOVERY -- cross-node reachability fix.
        # When P1+P2 are broken and L1-L3 messaging failed to reach the target, the
        # problem may be a network-layer issue (WiFi sleep, stale ARP/mDNS, daemon-down)
        # rather than a daemon-logic issue.  Invoke recover-peer-reachability.sh from
        # THIS node so it can perform class-aware remediation (WoL, mDNS flush, ARP
        # flush, chief-relay restart) before escalating to raw SSH restart in L4.
        # ZSF: all errors logged + countered, never raises.
        _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _recover_script = os.path.join(_repo_root, "scripts", "recover-peer-reachability.sh")
        if os.path.isfile(_recover_script):
            logger.info(f"Repair L3.5 SSH_PEER_RECOVER -> {target}: running recover-peer-reachability.sh")
            self._stats["ssh_recover_attempts_total"] = (
                self._stats.get("ssh_recover_attempts_total", 0) + 1
            )
            try:
                _rec = subprocess.run(
                    ["bash", _recover_script, target],
                    capture_output=True, text=True, timeout=60,
                )
                _stdout = _rec.stdout.strip()
                _stderr = _rec.stderr.strip()
                if _rec.returncode == 0:
                    self._stats["ssh_recover_success_total"] = (
                        self._stats.get("ssh_recover_success_total", 0) + 1
                    )
                    logger.info(
                        f"Repair L3.5 SSH_PEER_RECOVER SUCCESS for {target}: {_stdout}"
                    )
                    # Give the peer a moment to stabilise then re-probe P1.
                    await asyncio.sleep(5)
                    probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
                    if probe.get("delivered"):
                        logger.info(f"Repair SUCCESS: {target} back on P1 after L3.5 ssh-peer-recover")
                        return {"repaired": True, "level": "3.5"}
                else:
                    logger.warning(
                        f"Repair L3.5 SSH_PEER_RECOVER partial/fail for {target} "
                        f"(rc={_rec.returncode}): stdout={_stdout!r} stderr={_stderr!r}"
                    )
                    if _stderr:
                        try:
                            with open(f"/tmp/peer-recover-{target}.err", "a") as _ef:
                                _ef.write(_stderr + "\n")
                        except OSError:
                            pass
            except Exception as _e:
                logger.warning(f"Repair L3.5 SSH_PEER_RECOVER exception for {target}: {_e}")
        else:
            logger.debug(f"Repair L3.5: recover script not found at {_recover_script}, skipping")

        # Level 4: REMOTE -- SSH direct intervention (last resort, ~90s after first detection)
        logger.info(f"Repair L4 REMOTE -> {target} (~90s failure, all local attempts exhausted)")
        peer = self._resolve_peer(target)
        user, ip = peer.get("user"), peer.get("ip")
        if user and ip:
            try:
                # Check if target has active session -- don't interrupt
                check = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=3", f"{user}@{ip}",
                     "pgrep -f 'claude.*native-binary' > /dev/null && echo ACTIVE || echo NONE"],
                    capture_output=True, text=True, timeout=8)
                if "ACTIVE" in check.stdout:
                    logger.warning(f"Repair L4: {target} has active session -- NOT restarting remotely")
                    # Notify the active session instead -- use base64 to prevent injection
                    import base64
                    notice = f"Fleet repair: your channels {broken_channels} are down for 3min+. Please restart daemon."
                    b64_notice = base64.b64encode(notice.encode()).decode()
                    # Validate target name before using in path
                    safe_target = "".join(c for c in target if c.isalnum() or c in "-_")
                    subprocess.run(
                        ["ssh", "-o", "ConnectTimeout=3", f"{user}@{ip}",
                         f"echo '{b64_notice}' | base64 -d > /tmp/fleet-seed-{safe_target}.md"],
                        capture_output=True, timeout=8)
                    return {"repaired": False, "level": 4, "reason": "active_session_detected"}
                else:
                    # No active session -- safe to remote restart
                    logger.info(f"Repair L4: no active session on {target} -- remote restart")
                    safe_target = "".join(c for c in target if c.isalnum() or c in "-_")
                    subprocess.run(
                        ["ssh", "-o", "ConnectTimeout=3", f"{user}@{ip}",
                         f"pkill -f fleet_nerve_nats.py; sleep 2; cd ~/dev/er-simulator-superrepo && "
                         f"MULTIFLEET_NODE_ID={safe_target} nohup python3 tools/fleet_nerve_nats.py serve &>/tmp/fleet-nats-{safe_target}.log &"],
                        capture_output=True, timeout=15)
                    await asyncio.sleep(10)
                    probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
                    if probe.get("delivered"):
                        logger.info(f"Repair SUCCESS: {target} back on P1 after L4 remote restart")
                        return {"repaired": True, "level": 4}
            except Exception as e:
                logger.warning(f"Repair L4 SSH failed: {e}")

        logger.warning(f"Repair FAILED: all 4 levels exhausted for {target}")
        return {"repaired": False, "level": 4, "reason": "all_levels_failed"}

    # ── Inter-node repair (peer-quorum L5 assist) ────────────────────────────
    # When local L1-L4 escalation exhausts repeatedly for the same target,
    # this node broadcasts fleet.peer.struggling. Peers evaluate their own
    # connectivity to the target via _self_health_score(), and the best-
    # positioned peer self-elects to run SSH-from-third-party (L5 assist).
    # Rate limited 1 assist/(assister,target)/15min. Election cap = 3 fleet
    # wide. Repair script is idempotent (lockfile on target).
    # ───────────────────────────────────────────────────────────────────────
    def _record_send_outcome(self, peer: str, delivered: bool) -> None:
        """Record a self→peer send attempt for _self_health_score(peer)."""
        hist = self._send_history.setdefault(peer, [])
        hist.append(bool(delivered))
        if len(hist) > ASSIST_HEALTH_SAMPLE:
            del hist[: len(hist) - ASSIST_HEALTH_SAMPLE]

    def _self_health_score(self, peer: str) -> float:
        """Return composite self→peer health score in [0.0, 1.0]."""
        hist = self._send_history.get(peer) or []
        if not hist:
            return 0.0
        success_rate = sum(1 for h in hist if h) / len(hist)
        load = min(1.0, len(self._active_assists) / max(1, ASSIST_MAX_CONCURRENT))
        return max(0.0, success_rate * (1.0 - load))

    async def _publish_struggle(self, peer: str, broken_channels: list, attempts: int) -> None:
        """Emit fleet.peer.struggling when local self-heal can't recover peer."""
        if not self.nc or not self.nc.is_connected:
            logger.debug(f"_publish_struggle: NATS not connected, skipping for {peer}")
            return
        payload = {
            "peer": peer,
            "reporter": self.node_id,
            "channels_broken": list(broken_channels or []),
            "attempts": int(attempts),
            "since": time.time(),
        }
        try:
            await self.nc.publish(SUBJECT_PEER_STRUGGLING, json.dumps(payload).encode())
            logger.warning(
                f"peer-quorum: STRUGGLING -> {peer} after {attempts} cycles "
                f"(broken={broken_channels}); awaiting assist claim"
            )
            self._stats["struggle_broadcasts"] = self._stats.get("struggle_broadcasts", 0) + 1
        except Exception as e:
            self._stats["struggle_publish_errors"] = self._stats.get("struggle_publish_errors", 0) + 1
            logger.warning(f"_publish_struggle failed for {peer}: {e}")

    # ── EEE3 — Cascade harmonization handlers ───────────────────────
    #
    # _handle_cascade_peer_degraded:
    #     A sibling reports that *our* channel toward them is broken.
    #     We extract the channel from the subject (the trustworthy half —
    #     payload is advisory) and schedule a targeted self-repair via the
    #     existing repair_with_escalation path. Per-channel cooldown
    #     prevents flap-driven storms.
    #
    # _handle_cascade_peer_healed:
    #     Sibling reports the channel recovered. We just bump a counter
    #     for observability; the recovering side's own record_success
    #     edge already cleared the broken-state cache locally.

    _CASCADE_SELF_REPAIR_COOLDOWN_S = 60.0

    async def _handle_cascade_peer_degraded(self, msg) -> None:
        """Receive fleet.cascade.peer_degraded.<reporter>.<channel>."""
        try:
            subject = getattr(msg, "subject", "") or ""
            raw = msg.data.decode() if getattr(msg, "data", None) else "{}"
            data = json.loads(raw)
        except Exception:
            self._stats["cascade_degraded_parse_errors"] = (
                self._stats.get("cascade_degraded_parse_errors", 0) + 1
            )
            return

        # Subject shape: fleet.cascade.peer_degraded.<reporter>.<channel>
        parts = subject.split(".")
        if len(parts) < 5:
            self._stats["cascade_degraded_parse_errors"] = (
                self._stats.get("cascade_degraded_parse_errors", 0) + 1
            )
            return
        reporter = parts[3]
        channel = ".".join(parts[4:])
        peer_subject = data.get("peer")

        # Only act when the reporter says OUR channel is broken toward them.
        if peer_subject and peer_subject != self.node_id:
            return
        if reporter == self.node_id:
            return  # our own broadcast — ignore (NATS may echo)
        if not channel:
            return

        # Trust boundary: reporter MUST be in our peers_config.
        peers_cfg = self._peers_config or {}
        if reporter not in peers_cfg:
            self._stats["cascade_degraded_untrusted_reporter"] = (
                self._stats.get("cascade_degraded_untrusted_reporter", 0) + 1
            )
            logger.debug(
                f"cascade.peer_degraded from untrusted reporter={reporter} ignored"
            )
            return

        # HHH5: cooldown skip is visible — Aaron 2026-05-12 "we want an
        # ecosystem that operates flawlessly". Silent skip would look like
        # a hung path; log the throttle + countdown.
        now = time.time()
        last = self._cascade_self_repair_cooldowns.get(channel, 0.0)
        if now - last < self._CASCADE_SELF_REPAIR_COOLDOWN_S:
            self._stats["cascade_self_repair_cooldown_skips"] = (
                self._stats.get("cascade_self_repair_cooldown_skips", 0) + 1
            )
            remaining = int(self._CASCADE_SELF_REPAIR_COOLDOWN_S - (now - last))
            logger.info(
                f"cascade.peer_degraded: {reporter} reports our {channel} "
                f"broken — cooldown active ({remaining}s left). NEXT "
                f"AUTONOMOUS RETRY in {remaining}s."
            )
            return
        self._cascade_self_repair_cooldowns[channel] = now

        # HHH5: say what we're DOING, not what we're scheduling. The repair
        # is awaited inline below — fully autonomous L1→L4 escalation, no
        # human in the loop.
        logger.info(
            f"cascade.peer_degraded: {reporter} reports our {channel} broken — "
            f"EXECUTING autonomous L1→L4 self-repair now (no user action required)"
        )
        repair_t0 = time.time()
        ok = True
        try:
            await self.repair_with_escalation(
                reporter, [channel], action="check_connectivity",
            )
        except Exception as exc:  # noqa: BLE001 — ZSF
            ok = False
            self._stats["cascade_self_repair_exceptions"] = (
                self._stats.get("cascade_self_repair_exceptions", 0) + 1
            )
            logger.warning(
                f"cascade self-repair raised for {channel}: {exc}"
            )
        # HHH7 — Verify recovery before claiming OK. The L1 NOTIFY path in
        # repair_with_escalation publishes a message and returns; without a
        # post-repair probe we'd log OK for a still-broken channel (observed
        # 2026-05-12 — "after 0.0s" OK while channel stayed broken ~95min).
        # Split the outcome: VERIFIED-RECOVERED vs NOTIFIED-NOT-YET-RECOVERED.
        post_state = None
        try:
            if hasattr(self, "_get_peer_health_score"):
                _score, broken = self._get_peer_health_score(reporter)
                post_state = "broken" if channel in (broken or []) else "ok"
        except Exception as _pe:  # noqa: BLE001 — ZSF
            post_state = "unknown"
            self._stats["cascade_self_repair_verify_errors"] = (
                self._stats.get("cascade_self_repair_verify_errors", 0) + 1
            )

        if not ok:
            outcome = "FAILED"
        elif post_state == "ok":
            outcome = "VERIFIED-RECOVERED"
            self._stats["cascade_self_repair_verified_total"] = (
                self._stats.get("cascade_self_repair_verified_total", 0) + 1
            )
        elif post_state == "broken":
            outcome = "NOTIFIED-NOT-YET-RECOVERED"
            self._stats["cascade_self_repair_notified_only_total"] = (
                self._stats.get("cascade_self_repair_notified_only_total", 0) + 1
            )
        else:
            outcome = "NOTIFIED-UNVERIFIED"
            self._stats["cascade_self_repair_notified_only_total"] = (
                self._stats.get("cascade_self_repair_notified_only_total", 0) + 1
            )

        logger.info(
            f"cascade.self-repair {outcome}: {reporter}/{channel} after "
            f"{time.time() - repair_t0:.1f}s — autonomous loop closed "
            f"(post_state={post_state})"
        )
        try:
            from multifleet import channel_priority as _cp
            _cp.record_self_repair(ok=ok)
        except Exception as exc:  # noqa: BLE001 — ZSF
            logger.debug(f"cascade.record_self_repair failed: {exc}")

    async def _handle_cascade_peer_healed(self, msg) -> None:
        """Receive fleet.cascade.peer_healed.<reporter>.<channel>."""
        try:
            subject = getattr(msg, "subject", "") or ""
        except Exception:
            return
        parts = subject.split(".")
        if len(parts) < 5:
            self._stats["cascade_healed_parse_errors"] = (
                self._stats.get("cascade_healed_parse_errors", 0) + 1
            )
            return
        reporter = parts[3]
        channel = ".".join(parts[4:])
        if reporter == self.node_id:
            return
        self._stats["cascade_healed_observed"] = (
            self._stats.get("cascade_healed_observed", 0) + 1
        )
        logger.debug(
            f"cascade.peer_healed: {reporter}/{channel} recovered"
        )

    async def _handle_struggle_signal(self, msg) -> None:
        """Receive fleet.peer.struggling; self-elect to assist if best-positioned."""
        try:
            data = json.loads(msg.data.decode())
        except Exception:
            self._stats["struggle_parse_errors"] = self._stats.get("struggle_parse_errors", 0) + 1
            return

        peer = data.get("peer")
        reporter = data.get("reporter")
        if not peer or peer == self.node_id or reporter == self.node_id:
            return  # not for us to act on

        if peer not in (self._peers_config or {}):
            return

        last = self._assist_rate.get(peer, 0)
        if time.time() - last < ASSIST_COOLDOWN_S:
            logger.info(
                f"peer-quorum: struggle signal for {peer} ignored (own cooldown "
                f"{int(time.time() - last)}s < {ASSIST_COOLDOWN_S}s)"
            )
            return

        if len(self._active_assists) >= ASSIST_MAX_CONCURRENT:
            logger.info(
                f"peer-quorum: struggle for {peer} skipped (active={len(self._active_assists)}, "
                f"cap={ASSIST_MAX_CONCURRENT})"
            )
            return

        back = self._assist_backoff_until.get(peer, 0)
        if back and time.time() < back:
            logger.info(
                f"peer-quorum: struggle for {peer} deferred (back-off "
                f"{int(back - time.time())}s remaining)"
            )
            return

        score = self._self_health_score(peer)
        logger.info(
            f"peer-quorum: struggle signal for {peer} from {reporter}; "
            f"self_health_score={score:.3f}"
        )
        if score <= 0.0:
            return

        await self._publish_assist_claim(peer, score, data.get("channels_broken", []))

    async def _publish_assist_claim(self, peer: str, score: float, broken_channels: list) -> None:
        """Publish fleet.peer.assist.claim with TTL and score."""
        if not self.nc or not self.nc.is_connected:
            return
        claim = {
            "target": peer,
            "claimer": self.node_id,
            "score": float(score),
            "ts": time.time(),
            "ttl_s": ASSIST_CLAIM_WAIT_S,
            "channels_broken": list(broken_channels or []),
            "claim_id": str(uuid.uuid4()),
        }
        self._pending_claims[peer] = claim
        try:
            await self.nc.publish(SUBJECT_PEER_ASSIST_CLAIM, json.dumps(claim).encode())
            logger.info(
                f"peer-quorum: CLAIM -> {peer} (score={score:.3f}); "
                f"waiting {ASSIST_CLAIM_WAIT_S}s for duels"
            )
            self._stats["assist_claims_emitted"] = self._stats.get("assist_claims_emitted", 0) + 1
        except Exception as e:
            self._pending_claims.pop(peer, None)
            logger.warning(f"_publish_assist_claim failed for {peer}: {e}")
            return

        asyncio.ensure_future(self._execute_assist_if_uncontested(claim))

    async def _handle_assist_claim(self, msg) -> None:
        """Receive fleet.peer.assist.claim. Back off if not us; resolve duels.

        Split-brain resolution: when two peers claim the same target within
        TTL, the higher score wins; ties broken by earliest ts; then by
        node_id lexicographic sort. Deterministic on all observers.
        """
        try:
            data = json.loads(msg.data.decode())
        except Exception:
            self._stats["claim_parse_errors"] = self._stats.get("claim_parse_errors", 0) + 1
            return

        target = data.get("target")
        claimer = data.get("claimer")
        if not target or not claimer:
            return

        if claimer != self.node_id:
            self._assist_backoff_until[target] = time.time() + ASSIST_COOLDOWN_S
            ours = self._pending_claims.get(target)
            if ours:
                winner = self._resolve_claim_duel(ours, data)
                if winner is not ours:
                    logger.info(
                        f"peer-quorum: lost election for {target} to {claimer} "
                        f"(their score={data.get('score',0.0):.3f} vs ours={ours.get('score',0.0):.3f})"
                    )
                    self._pending_claims.pop(target, None)
                else:
                    logger.info(
                        f"peer-quorum: won election for {target} vs {claimer} "
                        f"(our score={ours.get('score',0.0):.3f})"
                    )
            else:
                logger.debug(
                    f"peer-quorum: noted claim by {claimer} for {target}; backing off "
                    f"{ASSIST_COOLDOWN_S}s"
                )
            return

        return  # own claim echo — no-op

    @staticmethod
    def _resolve_claim_duel(a: dict, b: dict) -> dict:
        """Deterministic election: higher score > earlier ts > lex node_id."""
        score_a, score_b = a.get("score", 0.0), b.get("score", 0.0)
        if score_a != score_b:
            return a if score_a > score_b else b
        ts_a, ts_b = a.get("ts", 0.0), b.get("ts", 0.0)
        if ts_a != ts_b:
            return a if ts_a < ts_b else b
        return a if a.get("claimer", "") < b.get("claimer", "") else b

    async def _execute_assist_if_uncontested(self, claim: dict) -> None:
        """After ASSIST_CLAIM_WAIT_S, run L5 SSH repair if still our claim."""
        target = claim["target"]
        try:
            await asyncio.sleep(ASSIST_CLAIM_WAIT_S)
        except asyncio.CancelledError:
            return

        active = self._pending_claims.get(target)
        if not active or active.get("claim_id") != claim.get("claim_id"):
            logger.info(f"peer-quorum: claim for {target} no longer held — skipping L5")
            return

        last = self._assist_rate.get(target, 0)
        if time.time() - last < ASSIST_COOLDOWN_S:
            self._pending_claims.pop(target, None)
            return

        if len(self._active_assists) >= ASSIST_MAX_CONCURRENT:
            self._pending_claims.pop(target, None)
            return

        self._assist_rate[target] = time.time()
        self._active_assists.add(target)
        self._pending_claims.pop(target, None)

        logger.warning(
            f"peer-quorum: L5 REPAIR-PEER -> {target} (uncontested claim; "
            f"running {ASSIST_SCRIPT})"
        )
        result = await self._run_repair_peer_script(target, claim.get("channels_broken", []))
        self._active_assists.discard(target)
        await self._publish_repair_result(target, result)

    async def _run_repair_peer_script(self, target: str, broken_channels: list) -> dict:
        """Invoke scripts/repair-peer.sh <target> via local shell.

        The script handles platform detection (launchctl on macOS, systemctl
        on Linux) and acquires an SSH lockfile on the target (idempotency).
        """
        peer = self._resolve_peer(target)
        user, ip = peer.get("user"), peer.get("ip")
        if not user or not ip:
            return {"ok": False, "reason": "no_ssh_info", "target": target}

        safe_target = "".join(c for c in target if c.isalnum() or c in "-_")
        if safe_target != target:
            return {"ok": False, "reason": "unsafe_target", "target": target}

        script = str(REPO_ROOT / ASSIST_SCRIPT)
        if not Path(script).exists():
            return {"ok": False, "reason": "script_missing", "script": script}

        env = dict(os.environ)
        env["FLEET_PEER_SSH_USER"] = user
        env["FLEET_PEER_SSH_HOST"] = ip
        env["FLEET_PEER_NODE_ID"] = safe_target
        env["FLEET_PEER_BROKEN"] = ",".join(broken_channels or [])

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", script, safe_target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
            tail = (stdout or b"").decode(errors="replace")[-512:]
            ok = proc.returncode == 0
            logger.info(
                f"peer-quorum: repair-peer.sh for {target} exit={proc.returncode}"
            )
            return {
                "ok": ok,
                "exit_code": proc.returncode,
                "target": target,
                "tail": tail,
            }
        except asyncio.TimeoutError:
            logger.warning(f"peer-quorum: repair-peer.sh for {target} timed out after 90s")
            return {"ok": False, "reason": "timeout", "target": target}
        except Exception as e:
            logger.warning(f"peer-quorum: repair-peer.sh for {target} failed: {e}")
            return {"ok": False, "reason": str(e), "target": target}

    async def _publish_repair_result(self, target: str, result: dict) -> None:
        """Broadcast fleet.peer.repair.result so the fleet can learn."""
        if not self.nc or not self.nc.is_connected:
            return
        payload = {
            "target": target,
            "assister": self.node_id,
            "ok": bool(result.get("ok")),
            "reason": result.get("reason"),
            "exit_code": result.get("exit_code"),
            "ts": time.time(),
        }
        try:
            await self.nc.publish(SUBJECT_PEER_REPAIR_RESULT, json.dumps(payload).encode())
            self._stats["assist_results_emitted"] = self._stats.get("assist_results_emitted", 0) + 1
            logger.info(f"peer-quorum: RESULT -> {target} ok={payload['ok']}")
        except Exception as e:
            logger.warning(f"_publish_repair_result failed: {e}")

    async def _handle_repair_result(self, msg) -> None:
        """Track assist outcomes for fleet-wide learning + stats."""
        try:
            data = json.loads(msg.data.decode())
        except Exception:
            self._stats["result_parse_errors"] = self._stats.get("result_parse_errors", 0) + 1
            return
        target = data.get("target")
        ok = data.get("ok")
        if ok:
            self._struggle_counter.pop(target, None)
            logger.info(
                f"peer-quorum: {data.get('assister')} repaired {target} — "
                f"struggle counter cleared"
            )
        else:
            logger.info(
                f"peer-quorum: {data.get('assister')} FAILED repair of {target} "
                f"(reason={data.get('reason')})"
            )

    async def broadcast_health(self) -> list:
        """Request health from all nodes, collect replies."""
        results = []

        # Self
        results.append({"node": self.node_id, "status": 200, "data": self._build_health()})

        # Request/reply to each known peer + discovered peers
        inbox = self.nc.new_inbox()
        sub = await self.nc.subscribe(inbox)

        await self.nc.publish("fleet.all.health", json.dumps({
            "from": self.node_id, "type": "health_request"
        }).encode(), reply=inbox)

        # Collect replies for 3 seconds
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(sub.next_msg(), timeout=0.5)
                data = json.loads(msg.data.decode())
                if data.get("nodeId") != self.node_id:
                    results.append({"node": data["nodeId"], "status": 200, "data": data})
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

        await sub.unsubscribe()
        results.sort(key=lambda r: r["node"])
        return results

    # ── Heartbeat ──

    async def _heartbeat_loop(self):
        """Publish heartbeat every 10s.

        Also refreshes own ``health.last_seen`` in FleetStateSync KV so peers
        (and trialbench's discover_nodes) don't see this node as stale. Without
        this, ``publish_health`` only fires once at startup and our own row in
        fleet-state.json ages out while the daemon is fully alive (v1 P1 Block 2).
        """
        # Counter for self-heartbeat cadence. We publish health every 3rd loop
        # (~30s) — matches FleetStateSync cold-sync debounce so each cold write
        # captures a fresh last_seen instead of a 30s-old one.
        self_health_tick = 0
        while self._running:
            try:
                detected = self._detect_sessions()
                hb = {
                    "node": self.node_id,
                    "timestamp": time.time(),
                    "ip": self._get_local_ip(),
                    "sessions": len(detected),
                    "session_list": [
                        {"sessionId": s.get("sessionId", ""), "idle_s": s.get("idle_s", 0)}
                        for s in detected
                    ],
                    "vscode": self._detect_vscode_state(),
                    "git": self._get_git_context(),
                }
                await self.nc.publish("fleet.heartbeat", json.dumps(hb).encode())
            except Exception as e:
                logger.debug(f"Heartbeat publish failed: {e}")

            # KKK1 — Refresh health snapshot from async context so HTTP threads
            # serve a pre-built dict instead of calling _build_health() live.
            # Eliminates self.nc read-race (async state read from OS threads)
            # and subprocess latency blocking HTTP workers under concurrent load.
            # GIL protects the dict reference swap — no explicit lock needed.
            try:
                self._health_snapshot = self._build_health()
                self._health_snapshot_ts = time.time()
                self._stats["health_snapshot_builds"] = (
                    self._stats.get("health_snapshot_builds", 0) + 1
                )
            except Exception as _e:  # noqa: BLE001 — ZSF
                self._stats["health_snapshot_errors"] = (
                    self._stats.get("health_snapshot_errors", 0) + 1
                )

            # Self-heartbeat into FleetStateSync. ZSF: best-effort, every error
            # bumps a visible counter so silent staleness is impossible.
            self_health_tick += 1
            if self_health_tick >= 3 and getattr(self, "_state_sync", None) is not None:
                self_health_tick = 0
                try:
                    ok = await self._state_sync.publish_health("online")
                    if not ok:
                        self._stats["self_heartbeat_publish_errors"] = (
                            self._stats.get("self_heartbeat_publish_errors", 0) + 1
                        )
                    else:
                        self._stats["self_heartbeat_publishes"] = (
                            self._stats.get("self_heartbeat_publishes", 0) + 1
                        )
                except Exception as e:
                    self._stats["self_heartbeat_publish_errors"] = (
                        self._stats.get("self_heartbeat_publish_errors", 0) + 1
                    )
                    logger.debug(f"Self-heartbeat publish_health failed: {e}")

            await asyncio.sleep(10)

    def _get_local_ip(self) -> str:
        """Get LAN IP for heartbeat advertisement."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return ""

    # ── Error-Rate Watchdog (Fix B) ──

    def _record_error(self):
        """Record an error timestamp and update rolling counter. Called by _error_rate_watchdog.

        Separated so callers already incrementing self._stats['errors'] don't need to know
        about the ring buffer -- the watchdog samples _stats['errors'] deltas itself.
        """
        now = time.time()
        self._error_timestamps.append(now)
        # Keep only last 60s
        cutoff = now - 60.0
        self._error_timestamps = [t for t in self._error_timestamps if t >= cutoff]

    async def _error_rate_watchdog(self):
        """Watch error counter; if errors exceed threshold/min, publish degraded event + exit(2).

        Prevents the death spiral observed on mac2: 1672 errors over 92h uptime, daemon
        stayed alive in degraded state, stuck in rate-limit-loop for 4 days. launchd
        KeepAlive=true will respawn us clean after sys.exit(2).
        """
        # Let the daemon settle before arming -- avoids killing ourselves during startup bursts
        await asyncio.sleep(120)
        logger.info(
            f"Error-rate watchdog armed (threshold={self._error_rate_threshold}/min)"
        )
        last_error_count = self._stats.get("errors", 0)
        while self._running:
            try:
                await asyncio.sleep(10)
                current = self._stats.get("errors", 0)
                delta = current - last_error_count
                last_error_count = current
                # Record each new error in the rolling window
                now = time.time()
                for _ in range(max(0, delta)):
                    self._error_timestamps.append(now)
                # Drop entries older than 60s
                cutoff = now - 60.0
                self._error_timestamps = [t for t in self._error_timestamps if t >= cutoff]
                rate = len(self._error_timestamps)
                if rate > self._error_rate_threshold:
                    reason = f"error_rate_exceeded:{rate}/min > {self._error_rate_threshold}"
                    logger.critical(
                        f"ERROR SPIRAL DETECTED: {rate} errors in last 60s "
                        f"(threshold {self._error_rate_threshold}) -- exiting for launchd respawn"
                    )
                    self._stats["last_restart_reason"] = reason
                    # Best-effort publish degraded-state event so fleet sees why we died
                    try:
                        if self.nc and not self.nc.is_closed:
                            payload = {
                                "from": self.node_id,
                                "event": "degraded_self_kill",
                                "reason": reason,
                                "error_rate_per_min": rate,
                                "threshold": self._error_rate_threshold,
                                "total_errors": current,
                                "uptime_s": int(time.time() - self._stats["started_at"]),
                                "timestamp": time.time(),
                            }
                            await asyncio.wait_for(
                                self.nc.publish(
                                    "fleet.all.alert",
                                    json.dumps(payload).encode(),
                                ),
                                timeout=2.0,
                            )
                            # Also surface to Discord via fleet.discord.outbound
                            discord_payload = json.dumps({
                                "node": self.node_id,
                                "subject": f"💀 {self.node_id} daemon self-killing (error spiral)",
                                "body": f"Reason: {reason}\nErrors: {current}\nUptime: {payload['uptime_s']}s\nlaunchd will respawn.",
                                "type": "alert",
                            }).encode()
                            try:
                                await asyncio.wait_for(
                                    self.nc.publish("fleet.discord.outbound", discord_payload),
                                    timeout=2.0,
                                )
                            except (OSError, asyncio.TimeoutError, ValueError, TypeError, AttributeError) as discord_err:
                                # ZSF (race/b1): daemon is already exiting via
                                # sys.exit(2) below — a Discord publish failure
                                # here must not block launchd respawn. Count +
                                # log so operators can see Discord was down
                                # when the daemon died.
                                self._stats["death_alert_discord_errors"] = (
                                    self._stats.get("death_alert_discord_errors", 0) + 1
                                )
                                logger.warning(
                                    "death-alert Discord publish failed: %s — exiting anyway",
                                    discord_err,
                                )
                            # Flush best-effort; ignore if NATS is already flaky
                            try:
                                await asyncio.wait_for(self.nc.flush(), timeout=2.0)
                            except Exception as flush_err:  # noqa: BLE001 — dying daemon, flush may race
                                logger.debug(f"Shutdown flush best-effort failed: {flush_err}")
                    except Exception as e:
                        logger.warning(f"Degraded-state publish failed: {e}")
                    # Exit(2) -- launchd respawn
                    sys.exit(2)
            except SystemExit:
                raise
            except Exception as e:
                logger.debug(f"Error-rate watchdog cycle error: {e}")

    # ── Proactive Fleet Watchdog ──

    async def _watchdog_loop(self):
        """Proactive health monitor -- detects degraded peers and auto-repairs.

        Runs every 30s. Checks:
        1. Peer heartbeat staleness -> triggers repair_with_escalation()
        2. Local zombie processes -> auto-kills known runaways
        3. Git divergence -> auto-pulls if behind origin
        4. Tunnel health -> restarts dead tunnels
        5. Idle detection -> proactive work discovery for self and peers
        """
        # Wait 60s for fleet to stabilize on startup
        await asyncio.sleep(60)
        logger.info("Watchdog started -- proactive fleet health monitoring (30s interval)")

        # Track repair state to avoid re-triggering during escalation
        self._repair_in_progress = {}  # target -> {started_at, level}

        # Idle tracking state
        self._last_activity_at = time.time()  # updated on send/receive
        self._idle_wake_sent = {}  # peer_id -> timestamp of last idle-wake sent
        self._last_suggestion_scan = 0.0  # productive idle rate limit

        while self._running:
            try:
                await self._watchdog_check_peers()
                await self._watchdog_check_channel_health()
                await self._watchdog_check_cap_kv_freshness()
                await self._watchdog_check_zombies()
                await self._watchdog_check_git()
                await self._watchdog_check_tunnels()
                await self._watchdog_check_idle()
                await self._watchdog_check_pending_acks()
            except Exception as e:
                logger.debug(f"Watchdog cycle error: {e}")
            await asyncio.sleep(30)

    def _touch_activity(self):
        """Mark that NATS activity occurred (call on send/receive)."""
        if hasattr(self, "_last_activity_at"):
            self._last_activity_at = time.time()

    async def _watchdog_check_idle(self):
        """Detect idle state for this node and peers, trigger proactive work discovery.

        Self-idle (no NATS messages sent/received >5min):
          - Check fleet inbox (.fleet-messages/<node>/) for unprocessed messages
          - Check /tmp/fleet-seed-<node>.md for pending messages
          - Broadcast idle status to fleet
          - Trigger git pull if behind origin
          - Log for observability

        Peer-idle (session count = 0 or last heartbeat >10min):
          - Send gentle wake message (rate-limited: 1 per peer per 30min)
        """
        now = time.time()
        idle_threshold_self = 300   # 5 minutes
        idle_threshold_peer = 600   # 10 minutes
        wake_cooldown = 1800        # 30 minutes between idle-wakes per peer

        # ── Self idle check ──
        idle_seconds = now - self._last_activity_at
        if idle_seconds > idle_threshold_self:
            pending_items = []

            # Check fleet inbox for unprocessed messages
            inbox_dir = REPO_ROOT / ".fleet-messages" / self.node_id
            if inbox_dir.exists():
                unprocessed = [f.name for f in inbox_dir.iterdir()
                               if f.is_file() and f.suffix == ".md"]
                if unprocessed:
                    pending_items.append(f"{len(unprocessed)} inbox message(s)")

            # Check seed file for pending messages
            seed_path = Path(SEED_DIR) / f"fleet-seed-{self.node_id}.md"
            if seed_path.exists() and seed_path.stat().st_size > 0:
                pending_items.append("pending seed file")

            pending_note = f" -- found: {', '.join(pending_items)}" if pending_items else ""
            idle_msg = (
                f"Node {self.node_id} idle ({int(idle_seconds)}s)"
                f" -- checking plans and exploring opportunities{pending_note}"
            )
            logger.info(f"Watchdog idle: {idle_msg}")

            # Broadcast idle status to fleet
            try:
                if self.nc and self.nc.is_connected:
                    await self.send("all", "idle", "idle-check", idle_msg)
            except Exception as e:
                logger.debug(f"Watchdog idle broadcast failed: {e}")

            # Trigger git pull if behind (reuses existing method's cooldown)
            await self._watchdog_check_git()

            # Productive idle: scan for actionable work and broadcast suggestions
            await self._productive_idle_scan()

            # Reset activity timer so we don't spam every 30s
            self._last_activity_at = now

        # ── Peer idle check ──
        for peer_id, hb_data in self._peers.items():
            if peer_id == self.node_id:
                continue

            peer_last_seen = now - hb_data.get("_received_at", 0)
            peer_sessions = hb_data.get("sessions", -1)
            peer_is_idle = (
                peer_last_seen > idle_threshold_peer
                or peer_sessions == 0
            )

            if not peer_is_idle:
                continue

            # Rate limit: max 1 idle-wake per peer per 30min
            last_wake = self._idle_wake_sent.get(peer_id, 0)
            if now - last_wake < wake_cooldown:
                continue

            idle_min = int(peer_last_seen / 60)
            reason = (
                f"no heartbeat for {idle_min}min"
                if peer_last_seen > idle_threshold_peer
                else "0 active sessions"
            )
            wake_msg = (
                f"You've been idle for {idle_min}min ({reason})."
                f" Any pending fleet work? Check plans."
            )

            try:
                if self.nc and self.nc.is_connected:
                    await self.send(peer_id, "idle-wake", "idle-wake", wake_msg)
                    self._idle_wake_sent[peer_id] = now
                    logger.info(f"Watchdog idle-wake sent to {peer_id} ({reason})")
            except Exception as e:
                logger.debug(f"Watchdog idle-wake to {peer_id} failed: {e}")

    async def _productive_idle_scan(self):
        """Scan for productive work when idle. Rate-limited: 1 per 10min.

        Checks incomplete plan items, unanswered fleet messages, and recent
        commits lacking tests.  Writes to /tmp/fleet-watchdog-suggestions.json
        and broadcasts a summary to the fleet.
        """
        now = time.time()
        if now - self._last_suggestion_scan < 600:  # 10min cooldown
            return
        self._last_suggestion_scan = now
        suggestions = []

        # 1. Incomplete items in docs/plans/
        plans_dir = REPO_ROOT / "docs" / "plans"
        if plans_dir.exists():
            for p in sorted(plans_dir.iterdir()):
                if not p.is_file() or p.suffix != ".md":
                    continue
                try:
                    text = p.read_text(errors="replace")
                    todos = [ln.strip() for ln in text.splitlines()
                             if any(k in ln for k in ("[ ]", "TODO", "UNRESOLVED"))]
                    if todos:
                        suggestions.append({
                            "source": "plans",
                            "file": str(p.relative_to(REPO_ROOT)),
                            "items": todos[:5],
                        })
                except (OSError, UnicodeDecodeError) as e:
                    logger.debug(f"_productive_idle_scan: plans file read skipped {p.name}: {e}")

        # 2. Unanswered fleet messages (all nodes' inboxes)
        fleet_dir = REPO_ROOT / ".fleet-messages"
        if fleet_dir.exists():
            for node_dir in fleet_dir.iterdir():
                if not node_dir.is_dir() or node_dir.name in ("archive",):
                    continue
                msgs = [f.name for f in node_dir.iterdir()
                        if f.is_file() and f.suffix == ".md"]
                if msgs:
                    suggestions.append({
                        "source": "fleet-messages",
                        "node": node_dir.name,
                        "pending": msgs[:10],
                    })

        # 3. Recent commits without tests (last 24h)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(REPO_ROOT), "log",
                "--since=24 hours ago", "--pretty=format:%h %s",
                "--no-merges", "-20",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            commits = stdout.decode(errors="replace").strip().splitlines()
            untested = [c for c in commits
                        if c and "test" not in c.lower()
                        and "fleet-msg" not in c.lower()]
            if untested:
                suggestions.append({
                    "source": "untested-commits",
                    "commits": untested[:10],
                })
        except (OSError, asyncio.TimeoutError, ValueError) as e:
            logger.debug(f"_productive_idle_scan: git log untested-commits skipped: {e}")

        # Write to disk
        output = {
            "node": self.node_id,
            "scanned_at": datetime.now(timezone.utc).isoformat() + "Z",
            "suggestions": suggestions,
        }
        try:
            Path("/tmp/fleet-watchdog-suggestions.json").write_text(
                json.dumps(output, indent=2))
        except Exception as e:
            logger.debug(f"Productive idle: write suggestions failed: {e}")

        # Broadcast summary to fleet
        if suggestions and self.nc and self.nc.is_connected:
            lines = []
            for s in suggestions:
                src = s["source"]
                if src == "plans":
                    lines.append(
                        f"  plans/{Path(s['file']).name}: "
                        f"{len(s['items'])} TODO(s)")
                elif src == "fleet-messages":
                    lines.append(
                        f"  fleet-messages/{s['node']}: "
                        f"{len(s['pending'])} pending")
                elif src == "untested-commits":
                    lines.append(
                        f"  {len(s['commits'])} recent commit(s) "
                        f"may need tests")
            body = ("Productive idle scan found work:\n"
                    + "\n".join(lines))
            try:
                await self.send(
                    "all", "idle-suggestions", "productive-idle", body)
            except Exception as e:  # noqa: BLE001 — broadcast is best-effort observational signal
                logger.debug(f"_productive_idle_scan: broadcast suggestions send skipped: {e}")

        logger.info(f"Productive idle scan: "
                    f"{len(suggestions)} suggestion group(s)")

    async def _watchdog_check_peers(self):
        """Detect stale peers and trigger repair escalation.

        R4 root-cause fix (2026-05-12): adds per-peer stale alerter that
        emits a fleet alert to chief when a peer heartbeat is absent >600s
        despite repair attempts. Counter ``peer_heartbeat_stale_total`` tracks
        each stale detection for observability. Rate-limited to one alert per
        peer per 15 min (``_stale_alert_sent`` dict). ZSF: all alert paths
        are wrapped so a send failure never blocks watchdog iteration.
        """
        now = time.time()
        all_peers = set(self._peers_config.keys()) - {self.node_id}
        seen_peers = set(self._peers.keys())

        # Track last peer-IP refresh per peer so we don't re-read config.json
        # every 30s watchdog cycle once a peer is stale.
        if not hasattr(self, "_last_peer_ip_refresh"):
            self._last_peer_ip_refresh: dict[str, float] = {}

        # R4: rate-limit stale-alert fleet messages (max 1 per peer per 15 min).
        if not hasattr(self, "_stale_alert_sent"):
            self._stale_alert_sent: dict[str, float] = {}

        for peer_id in all_peers:
            # Skip if repair already in progress
            repair = self._repair_in_progress.get(peer_id, {})
            if repair and now - repair.get("started_at", 0) < 300:  # 5min cooldown
                continue

            if peer_id in seen_peers:
                last_seen = now - self._peers[peer_id].get("_received_at", 0)
                if last_seen > 90:  # 90s = 9 missed heartbeats
                    # R4: bump per-detection counter (one per watchdog tick while stale).
                    self._stats["peer_heartbeat_stale_total"] = (
                        self._stats.get("peer_heartbeat_stale_total", 0) + 1
                    )
                    # Extra-stale (>5min): re-read .multifleet/config.json so
                    # the next repair attempt uses the freshest peer IPs.
                    # Rate-limit: at most once per 5min per peer.
                    if last_seen > 300:
                        last_refresh = self._last_peer_ip_refresh.get(peer_id, 0.0)
                        if now - last_refresh > 300:
                            try:
                                self._refresh_peer_ips("stale_peer")
                            except Exception as e:
                                logger.debug(
                                    f"_refresh_peer_ips(stale_peer) failed "
                                    f"for {peer_id}: {e}"
                                )
                            self._last_peer_ip_refresh[peer_id] = now
                    logger.warning(f"Watchdog: {peer_id} heartbeat stale ({int(last_seen)}s)")
                    self._repair_in_progress[peer_id] = {"started_at": now, "level": 1}
                    asyncio.create_task(
                        self.repair_with_escalation(peer_id, ["P1_nats"], "restart_nats"))

                    # R4 alerter: emit fleet alert to chief when stale >600s.
                    # One alert per peer per 15 min to avoid flooding chief.
                    if last_seen > 600:
                        last_alerted = self._stale_alert_sent.get(peer_id, 0.0)
                        if now - last_alerted > 900:  # 15-min rate limit
                            self._stale_alert_sent[peer_id] = now
                            try:
                                chief = getattr(self, "_chief", None) or "mac1"
                                await self.send_message(
                                    chief,
                                    {
                                        "type": "alert",
                                        "subject": f"heartbeat_stale:{peer_id}",
                                        "body": (
                                            f"{self.node_id} reports {peer_id} heartbeat "
                                            f"absent {int(last_seen)}s. Repair attempted. "
                                            f"Peer may be offline or daemon down."
                                        ),
                                        "severity": "warning",
                                        "stale_s": int(last_seen),
                                        "reporter": self.node_id,
                                        "peer": peer_id,
                                    },
                                )
                                self._stats["peer_heartbeat_stale_alerts_sent"] = (
                                    self._stats.get("peer_heartbeat_stale_alerts_sent", 0) + 1
                                )
                                logger.info(
                                    f"R4 stale-alert sent to {chief} for {peer_id} "
                                    f"(stale {int(last_seen)}s)"
                                )
                            except Exception as e:
                                # ZSF: never block watchdog on alert send failure.
                                self._stats["peer_heartbeat_stale_alert_errors"] = (
                                    self._stats.get("peer_heartbeat_stale_alert_errors", 0) + 1
                                )
                                logger.debug(
                                    f"R4 stale-alert send failed for {peer_id}: {e}"
                                )
                elif last_seen > 45:  # Warning threshold
                    logger.info(f"Watchdog: {peer_id} heartbeat slow ({int(last_seen)}s)")
            else:
                # Never seen this peer -- they might not be online yet
                # Only escalate if we've been running >5min (past startup window)
                if now - self._stats["started_at"] > 300:
                    # R4: count never-seen as stale too.
                    self._stats["peer_heartbeat_stale_total"] = (
                        self._stats.get("peer_heartbeat_stale_total", 0) + 1
                    )
                    # Re-read config in case the peer's IP changed since daemon
                    # start (rate-limited to one refresh per peer per 5min).
                    last_refresh = self._last_peer_ip_refresh.get(peer_id, 0.0)
                    if now - last_refresh > 300:
                        try:
                            self._refresh_peer_ips("stale_peer")
                        except Exception as e:
                            logger.debug(
                                f"_refresh_peer_ips(stale_peer) failed "
                                f"for {peer_id}: {e}"
                            )
                        self._last_peer_ip_refresh[peer_id] = now
                    logger.warning(f"Watchdog: {peer_id} never seen -- attempting contact")
                    self._repair_in_progress[peer_id] = {"started_at": now, "level": 1}
                    asyncio.create_task(
                        self.repair_with_escalation(peer_id, ["P1_nats", "P2_http"], "restart_nats"))

                    # R4 alerter: never-seen peer after 600s is also worth alerting chief.
                    never_seen_age = now - self._stats["started_at"]
                    if never_seen_age > 600:
                        last_alerted = self._stale_alert_sent.get(peer_id, 0.0)
                        if now - last_alerted > 900:
                            self._stale_alert_sent[peer_id] = now
                            try:
                                chief = getattr(self, "_chief", None) or "mac1"
                                await self.send_message(
                                    chief,
                                    {
                                        "type": "alert",
                                        "subject": f"heartbeat_never_seen:{peer_id}",
                                        "body": (
                                            f"{self.node_id} has never received a heartbeat "
                                            f"from {peer_id} in {int(never_seen_age)}s uptime. "
                                            f"Peer may be offline or not in NATS cluster."
                                        ),
                                        "severity": "warning",
                                        "uptime_s": int(never_seen_age),
                                        "reporter": self.node_id,
                                        "peer": peer_id,
                                    },
                                )
                                self._stats["peer_heartbeat_stale_alerts_sent"] = (
                                    self._stats.get("peer_heartbeat_stale_alerts_sent", 0) + 1
                                )
                                logger.info(
                                    f"R4 never-seen alert sent to {chief} for {peer_id}"
                                )
                            except Exception as e:
                                self._stats["peer_heartbeat_stale_alert_errors"] = (
                                    self._stats.get("peer_heartbeat_stale_alert_errors", 0) + 1
                                )
                                logger.debug(
                                    f"R4 never-seen alert send failed for {peer_id}: {e}"
                                )

    async def _watchdog_check_channel_health(self):
        """Check channel health scores and trigger repair for degraded peers."""
        now = time.time()
        for peer_id, channels in self._channel_health.items():
            score, broken = self._get_peer_health_score(peer_id)
            if score >= HEAL_SCORE_THRESHOLD or not broken:
                continue
            # Skip if repair already in progress (5min cooldown)
            repair = self._repair_in_progress.get(peer_id, {})
            if repair and now - repair.get("started_at", 0) < 300:
                continue
            existing = self._heal_in_flight.get(peer_id)
            if existing and not existing.done():
                continue
            logger.warning(f"Watchdog: {peer_id} channel score {score}/{len(ALL_CHANNELS)} "
                           f"< {HEAL_SCORE_THRESHOLD} -- auto-repair: {broken}")
            self._repair_in_progress[peer_id] = {"started_at": now, "level": 1}
            action = "restart_nats" if "P1_nats" in broken else "check_connectivity"
            self._attempt_self_heal(peer_id, "watchdog", broken)

    async def _watchdog_check_cap_kv_freshness(self):
        """VV3-B INV-CapKVFreshness — log stale capability docs for known peers.

        Fires when ``time.time() - last_publish_ts > 48h`` for any peer whose
        last capability-doc refresh we have observed.  Stale peers are surfaced
        via a WARNING log and tracked in ``self._cap_kv_stale_peers`` so the
        /health endpoint can expose them without an extra NATS round-trip.

        ZSF: any exception increments ``cap_kv_freshness_check_errors`` and
        returns without raising so the 30s watchdog loop continues normally.
        """
        try:
            if _cap_kv_freshness_inv is None:
                return
            if not self._peer_cap_publish_ts:
                return
            violations = _cap_kv_freshness_inv.check(self._peer_cap_publish_ts)
            self._cap_kv_stale_peers = {
                v["peer"]: {"stale_hours": v["stale_hours"], "action": v["action"]}
                for v in violations
            }
            for v in violations:
                logger.warning(
                    "INV-CapKVFreshness: peer %s capability doc stale %.1fh — action: %s",
                    v["peer"], v["stale_hours"], v["action"],
                )
        except Exception as exc:  # noqa: BLE001 — ZSF
            self._stats["cap_kv_freshness_check_errors"] += 1
            logger.debug("_watchdog_check_cap_kv_freshness error: %s", exc)

    # ── K2 2026-05-04 — event-driven ACK tracker ──────────────────────────
    # Replaces the previous polling pattern (watchdog scanned `_pending_acks`
    # every 30s looking for `age > timeout`). Each pending ack now has a
    # per-msg `asyncio.Event` and a deadline-awaiter task. On ACK arrival
    # `_handle_ack` sets the event → awaiter exits, no retry. On timeout the
    # awaiter fires the retry/drop path immediately (no 30s lag).
    #
    # The old watchdog tick is now `_watchdog_check_pending_acks` — a
    # heartbeat that only acts on entries WITHOUT a live awaiter (e.g. very
    # old code paths that skip `_track_pending_ack`, or an awaiter that
    # crashed). Cadence ≤ 1/hour via `_ACK_FALLBACK_INTERVAL_S`. ZSF: every
    # triggered fallback action increments `ack_fallback_heartbeat_total`,
    # so we can tell whether the fallback is actually catching anything
    # (it should NOT, in steady state).

    _ACK_FALLBACK_INTERVAL_S = 3600  # ≤ 1/hour — primary signal is event-driven

    def _track_pending_ack(self, msg_id: str, target: str, message: dict) -> None:
        """Register a pending ACK with an event-driven deadline.

        Idempotent on retries: existing retry-count is preserved; a fresh
        Event + awaiter task replace the previous deadline.
        """
        existing = self._pending_acks.get(msg_id) or {}
        retries = existing.get("retries", 0)
        old_task = self._ack_awaiter_tasks.pop(msg_id, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()
        ev = asyncio.Event()
        self._pending_acks[msg_id] = {
            "sent_at": time.time(),
            "target": target,
            "retries": retries,
            "acked": False,
            "message": message,
            "event": ev,
        }
        try:
            task = asyncio.create_task(
                self._await_ack_deadline(msg_id, ev),
                name=f"ack-await-{msg_id[:8]}",
            )
        except RuntimeError:
            # No running event loop (tests). Fallback heartbeat will clean up.
            self._ack_stats["ack_awaiter_errors"] += 1
            logger.debug(f"_track_pending_ack: no running loop for {msg_id[:8]}")
            return
        self._ack_awaiter_tasks[msg_id] = task

    async def _await_ack_deadline(self, msg_id: str, ev: "asyncio.Event") -> None:
        """Block on the per-msg ACK event with a hard deadline.

        - ACK arrives → event set → return (no retry).
        - Deadline elapses → fire retry (or drop after max retries).
        Cancellation is honoured so the daemon shuts down cleanly.
        """
        try:
            await asyncio.wait_for(ev.wait(), timeout=self._ack_timeout_s)
            return  # ACK arrived in time.
        except asyncio.TimeoutError:
            self._ack_stats["ack_timeout_event_total"] += 1
            self._on_ack_deadline_expired(msg_id)
        except asyncio.CancelledError:
            return
        except Exception as e:
            # ZSF: never silent — count and log.
            self._ack_stats["ack_awaiter_errors"] += 1
            logger.warning(f"ACK awaiter error msg={msg_id[:8]}: {e}")
        finally:
            self._ack_awaiter_tasks.pop(msg_id, None)

    def _on_ack_deadline_expired(self, msg_id: str) -> None:
        """Called when the per-msg deadline fires without an ACK."""
        info = self._pending_acks.get(msg_id)
        if info is None or info.get("acked"):
            return
        if info["retries"] >= self._ack_max_retries:
            logger.warning(
                f"ACK failed after {self._ack_max_retries} retries: "
                f"{msg_id[:8]} -> {info['target']}"
            )
            self._pending_acks.pop(msg_id, None)
            return
        info["retries"] += 1
        logger.info(
            f"ACK retry {info['retries']}/{self._ack_max_retries}: "
            f"{msg_id[:8]} -> {info['target']}"
        )
        try:
            asyncio.create_task(
                self.send_with_fallback(info["target"], info["message"]),
                name=f"ack-retry-{msg_id[:8]}",
            )
        except RuntimeError:
            self._ack_stats["ack_awaiter_errors"] += 1
            logger.debug(f"ack retry create_task failed for {msg_id[:8]}")

    async def _watchdog_check_pending_acks(self):
        """Fallback heartbeat for pending-ACK timeouts.

        Primary signal is the per-msg deadline awaiter scheduled by
        `_track_pending_ack`. This heartbeat only acts on entries WITHOUT a
        live awaiter (e.g. old paths bypassing `_track_pending_ack`, or an
        awaiter that crashed). Throttled to ≤1/hour internally so the outer
        30s `_watchdog_loop` cadence stays unchanged for other checks.
        """
        now = time.time()
        last = getattr(self, "_last_ack_fallback_run", 0.0)
        if now - last < self._ACK_FALLBACK_INTERVAL_S:
            return
        self._last_ack_fallback_run = now

        to_remove = []
        for msg_id, info in list(self._pending_acks.items()):
            if info.get("acked"):
                to_remove.append(msg_id)
                continue
            task = self._ack_awaiter_tasks.get(msg_id)
            if task is not None and not task.done():
                continue
            age = now - info["sent_at"]
            if age < self._ack_timeout_s:
                continue
            self._ack_stats["ack_fallback_heartbeat_total"] += 1
            if info["retries"] >= self._ack_max_retries:
                logger.warning(
                    f"ACK fallback drop after {self._ack_max_retries} retries: "
                    f"{msg_id[:8]} -> {info['target']}"
                )
                to_remove.append(msg_id)
                continue
            info["retries"] += 1
            logger.info(
                f"ACK fallback retry {info['retries']}/{self._ack_max_retries}: "
                f"{msg_id[:8]} -> {info['target']}"
            )
            asyncio.create_task(
                self.send_with_fallback(info["target"], info["message"]),
                name=f"ack-fallback-retry-{msg_id[:8]}",
            )
        for msg_id in to_remove:
            self._pending_acks.pop(msg_id, None)
            self._ack_awaiter_tasks.pop(msg_id, None)

    async def _watchdog_check_zombies(self):
        """Detect and kill known zombie patterns (runaway processes)."""
        try:
            result = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 11:
                    continue
                try:
                    pid = int(parts[1])
                    cpu = float(parts[2])
                    # Skip our own daemon process
                    if pid == os.getpid():
                        continue
                    # Flag: python process using >10% CPU for extended time
                    # (We can't tell duration from ps alone, so only kill known patterns)
                    cmd = " ".join(parts[10:])
                    # Known zombie: fleet_nerve_daemon.py fighting for our port
                    if "fleet_nerve_daemon.py" in cmd and "--port 8855" in cmd:
                        logger.warning(f"Watchdog: killing rogue fleet_nerve_daemon.py (PID {pid})")
                        os.kill(pid, 9)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug(f"_watchdog_check_zombies: ps probe skipped: {e}")

    async def _watchdog_check_git(self):
        """Auto-pull if behind origin (check every cycle, pull at most every 5min).

        Uses ``--no-rebase`` (merge strategy) instead of ``git rebase
        FETCH_HEAD`` so an interrupted pull cannot leave the repo stuck in
        ``.git/rebase-merge/`` — the root cause of the recurring "interactive
        rebase in progress" messages. Also proactively cleans up any stale
        rebase or detached-HEAD state inherited from prior crashes before
        attempting a new pull.
        """
        if not hasattr(self, "_last_git_pull"):
            self._last_git_pull = 0
        now = time.time()
        if now - self._last_git_pull < 300:  # 5min cooldown
            return
        try:
            # ── Pre-pull hygiene: clear stale rebase state from prior crashes ──
            # If `.git/rebase-merge/` (or rebase-apply) is older than 1h we
            # know nothing is actively rebasing — abort + remove so the
            # repo exits the "rebase in progress" state permanently.
            rebase_merge = REPO_ROOT / ".git" / "rebase-merge"
            rebase_apply = REPO_ROOT / ".git" / "rebase-apply"
            for stale_dir in (rebase_merge, rebase_apply):
                if not stale_dir.exists():
                    continue
                try:
                    age = now - stale_dir.stat().st_mtime
                except OSError:
                    age = 0
                if age > 3600:  # >1h old -> stale, nothing is running
                    logger.warning(
                        f"Watchdog: stale {stale_dir.name} (age={int(age)}s) — auto-cleanup"
                    )
                    subprocess.run(
                        ["git", "rebase", "--abort"],
                        cwd=str(REPO_ROOT), capture_output=True, timeout=10,
                    )
                    try:
                        import shutil
                        if stale_dir.exists():
                            shutil.rmtree(stale_dir, ignore_errors=True)
                    except Exception as _e:
                        logger.debug(f"Watchdog: rmtree({stale_dir}) failed: {_e}")

            # Re-attach detached HEAD to main if we ended up detached.
            sym = subprocess.run(
                ["git", "symbolic-ref", "-q", "HEAD"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5,
            )
            if sym.returncode != 0:  # detached HEAD
                logger.warning("Watchdog: detached HEAD detected — re-attaching to main")
                subprocess.run(
                    ["git", "checkout", "main"],
                    cwd=str(REPO_ROOT), capture_output=True, timeout=10,
                )

            # Check if behind
            subprocess.run(["git", "fetch", "origin", "main"],
                           cwd=str(REPO_ROOT), capture_output=True, timeout=15)
            result = subprocess.run(
                ["git", "rev-list", "--count", "HEAD..FETCH_HEAD"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5)
            behind = int(result.stdout.strip()) if result.returncode == 0 else 0
            if behind > 0:
                logger.info(f"Watchdog: {behind} commits behind origin -- auto-pulling (merge)")
                # IMPORTANT: --no-rebase. Merge strategy never creates
                # .git/rebase-merge/, so an interrupted pull cannot leave
                # the repo in "interactive rebase in progress" state.
                subprocess.run(
                    ["git", "pull", "--no-rebase", "--no-edit", "origin", "main"],
                    cwd=str(REPO_ROOT), capture_output=True, timeout=30,
                )
                self._last_git_pull = now
                logger.info("Watchdog: git pull complete")
        except Exception as e:
            logger.debug(f"Watchdog git check error: {e}")

    async def _watchdog_check_tunnels(self):
        """Verify SSH tunnels are alive, restart if dead."""
        tunnel_dir = Path.home() / ".fleet-tunnels"
        if not tunnel_dir.exists():
            return
        any_dead = False
        for pidfile in tunnel_dir.glob("tunnel-*.pid"):
            try:
                pid = int(pidfile.read_text().strip())
                os.kill(pid, 0)  # Check if alive (signal 0)
            except (ProcessLookupError, ValueError):
                logger.warning(f"Watchdog: tunnel {pidfile.stem} is dead -- restarting")
                any_dead = True
                pidfile.unlink(missing_ok=True)
        if any_dead:
            # Restart tunnels
            try:
                subprocess.run(
                    ["bash", str(REPO_ROOT / "scripts" / "fleet-tunnel.sh"), "start"],
                    capture_output=True, timeout=30,
                    env={**os.environ, "MULTIFLEET_NODE_ID": self.node_id})
                logger.info("Watchdog: tunnels restarted")
            except Exception as e:
                logger.debug(f"Watchdog tunnel restart failed: {e}")

    # ── ACK Retry Loop ──

    ACK_TIMEOUT_S = 60.0
    ACK_MAX_RETRIES = 3

    async def _ack_retry_loop(self):
        """Check for unacknowledged messages and retry via next fallback channel.

        Runs every 30s. Messages awaiting ACK past ACK_TIMEOUT_S get re-sent.
        After ACK_MAX_RETRIES, marked as failed and surfaced via _inject_alert.
        """
        await asyncio.sleep(30)  # Let daemon stabilize
        logger.info("ACK retry loop started (30s interval)")
        while self._running:
            try:
                if not self._store:
                    await asyncio.sleep(30)
                    continue
                pending = self._store.get_awaiting_ack(
                    max_age_s=self.ACK_TIMEOUT_S, max_retries=self.ACK_MAX_RETRIES
                )
                for row in pending:
                    msg_id = row["id"]
                    target = row["to_node"]
                    retries = row.get("retry_count", 0)

                    if retries >= self.ACK_MAX_RETRIES:
                        # Exhausted retries -- mark failed and alert
                        self._store.mark_ack_failed(msg_id)
                        logger.warning(f"ACK failed after {retries} retries: {msg_id[:8]}… -> {target}")
                        self._inject_alert({
                            "from": "fleet-ack",
                            "type": "alert",
                            "payload": {
                                "subject": f"Message delivery failed: {msg_id[:8]}…",
                                "body": f"Message to {target} failed after {retries} retries. ID: {msg_id}",
                            },
                        })
                        continue

                    # Rebuild message from stored body
                    try:
                        body_data = json.loads(row.get("body", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        body_data = {}

                    message = {
                        "id": msg_id,
                        "type": row.get("type", "context"),
                        "from": row.get("from_node", self.node_id),
                        "to": target,
                        "payload": body_data,
                        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                    }

                    self._store.bump_retry(msg_id)
                    logger.info(f"ACK retry {retries + 1}/{self.ACK_MAX_RETRIES}: {msg_id[:8]}… -> {target}")

                    # Re-send (send_with_fallback will skip re-storing since it already exists)
                    try:
                        result = await self.send_with_fallback(target, message)
                        if result.get("delivered"):
                            logger.info(f"ACK retry delivered: {msg_id[:8]}… via {result.get('channel')}")
                    except Exception as e:
                        logger.warning(f"ACK retry send failed: {msg_id[:8]}…: {e}")

            except Exception as e:
                logger.error(f"ACK retry loop error: {e}")
            await asyncio.sleep(30)

    # ── Main Loop ──

    async def serve(self):
        """Start the NATS daemon -- HTTP first (always useful), then NATS (best-effort)."""
        self._loop = asyncio.get_running_loop()

        # Start HTTP compat FIRST -- useful even without NATS (fallback channels work)
        await self._start_http_compat()
        logger.info(f"Fleet Nerve | node={self.node_id} | nats={self.nats_url}")

        # Try NATS connection (non-blocking -- don't block HTTP if NATS is down)
        try:
            await self.connect()
            await self._subscribe_all()
            # Refresh peer IPs from .multifleet/config.json once NATS is up so
            # late edits to config take effect without a daemon restart.
            try:
                self._refresh_peer_ips("startup")
            except Exception as e:
                logger.debug(f"_refresh_peer_ips(startup) failed: {e}")
            asyncio.create_task(self._heartbeat_loop())
            asyncio.create_task(self._watchdog_loop())
        except Exception as e:
            logger.warning(f"NATS connect failed: {e} -- HTTP-only mode, P2-P7 channels available")

        # ROUND-3 C1 — Always start the connect-retry loop, even if the
        # initial connect succeeded. It self-detects: when ``self.nc`` is
        # connected it sleeps; when it goes ``None`` / closed it retries
        # with backoff. This is the only path that recovers from a failed
        # initial connect — nats-py only auto-reconnects an existing
        # connection that drops. Without this loop, ``client_status: closed``
        # on startup is permanent until the daemon restarts.
        asyncio.create_task(self._connect_retry_loop())

        # Peer-assist coordinator — subscribes to rpc.peer.* once NATS is up.
        # Non-fatal: daemon still works as a struggler signaler via log
        # fallback even if the coordinator init fails.
        if self.nc is not None:
            try:
                from multifleet.peer_assist import PeerAssistCoordinator
                from multifleet import fleet_config as _fleet_config

                def _self_struggle() -> bool:
                    snap = self._build_struggle_snapshot()
                    return bool(snap.get("needs_peer_assist"))

                def _load_pct() -> int:
                    # Rough fleet load proxy: in-flight assists vs cap, scaled
                    # to 0–100. Keeps peer-assist decisions observable without
                    # pulling in psutil.
                    active = len(getattr(self, "_active_assists", set()) or set())
                    cap = max(1, int(globals().get("ASSIST_MAX_CONCURRENT", 3) or 3))
                    return int(min(100, (active * 100) // cap))

                def _head_sha() -> str:
                    git = self._get_git_context() or {}
                    return str(git.get("commit") or git.get("sha") or "")

                # Wire fleet-upgrading runtime as the peer_assist offer_git_upgrade
                # capability handler (per docs/fleet/FLEET-UPGRADING.md cebf24d3).
                # Adapter captures nc + fleet_config + repo_root so peer_assist's
                # call site can use the simple `check_capability_deltas(target)`
                # signature it expects, while fleet_upgrader sees its full kwargs.
                _fleet_upgrader_adapter = None
                try:
                    from multifleet import fleet_upgrader as _fu  # noqa: PLC0415

                    class _FleetUpgraderBound:
                        """Lightweight adapter exposing only the methods peer_assist needs."""
                        def __init__(self, nc, fc):
                            self._nc = nc
                            self._fc = fc

                        async def check_capability_deltas(self, target: str):
                            return await _fu.check_capability_deltas(
                                self._nc,
                                self._fc,
                                reason=f"peer_assist:target={target}",
                            )

                    _fleet_upgrader_adapter = _FleetUpgraderBound(self.nc, _fleet_config)
                    logger.info(
                        "fleet_upgrader adapter wired into peer_assist "
                        "offer_git_upgrade capability"
                    )
                except ImportError as e:
                    logger.warning(
                        f"fleet_upgrader unavailable — peer_assist offer_git_upgrade "
                        f"will log TODO: {e}"
                    )

                self._peer_assist = PeerAssistCoordinator(
                    nats_client=self.nc,
                    node_id=self.node_id,
                    fleet_config=_fleet_config,
                    is_struggling_cb=_self_struggle,
                    self_load_cb=_load_pct,
                    head_sha_cb=_head_sha,
                    fleet_upgrader=_fleet_upgrader_adapter,
                )
                await self._peer_assist.start()
                logger.info(
                    "PeerAssistCoordinator active — subjects=rpc.peer.{struggling,"
                    "help.offer,help.assigned,help.complete}"
                )
            except Exception as e:
                logger.warning(
                    f"PeerAssistCoordinator setup failed (non-fatal): {e}"
                )
                self._peer_assist = None

        # ── Layer 0.5: JetStream warmup ──────────────────────────────────
        # First JetStream op after connect is slow (API discovery).
        # Warm up here so Layer 1 KV bind doesn't eat the cold-start penalty.
        if self.nc is not None:
            try:
                js = self.nc.jetstream()
                await asyncio.wait_for(js.account_info(), timeout=5)
                logger.info("JetStream API ready")
            except Exception as e:
                logger.warning(f"JetStream warmup failed (non-fatal): {e}")

        # ── Layer 1: Fleet State Sync — KV (hot) + git JSON (cold) ──────
        # Waterfall: starts BEFORE EventBus so KV state is populated when
        # repair signals begin evaluating peer health.  Owns its own KV
        # watcher (no shared router — eliminates circular dependency).
        if self.nc is not None and FleetStateSync is not None:
            try:
                profile = detect_local_profile(self.node_id) if detect_local_profile else None
                self._state_sync = FleetStateSync(
                    node_id=self.node_id,
                    profile=profile,
                )
                await self._state_sync.init_kv(self.nc)
                seeded = await self._state_sync.seed_kv_from_cold()
                # Publish own profile + version to KV
                if profile:
                    await self._state_sync.write_own_state("profile", {
                        "tier": profile.tier,
                        "ram_gb": profile.ram_gb,
                        "gpu": profile.gpu,
                        "role": profile.role,
                    })
                try:
                    sha = subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], timeout=5,
                        cwd=str(self._state_sync.repo_root),
                    ).decode().strip()[:12]
                except Exception:
                    sha = "unknown"
                await self._state_sync.publish_version(sha, daemon_version="nerve-nats")
                await self._state_sync.publish_health("online")
                # StateSync always owns its own KV watcher (no shared router)
                await self._state_sync.start_kv_watcher()
                # Layer 3: Cold git sync — starts last, only fires when dirty
                await self._state_sync.start_cold_sync()
                logger.info(
                    f"FleetStateSync active — seeded={seeded} nodes_tracked={len(self._state_sync.state)}"
                )
            except Exception as e:
                logger.warning(f"FleetStateSync setup failed (non-fatal): {e}")
                self._state_sync = None

        # ── Layer 2: Event Bus — repair quorum signals ───────────────────
        # Waterfall: starts AFTER StateSync so repair signals can read
        # populated KV state.  Upgrade callback = None (transport does not
        # own upgrade decisions — orchestration layer responsibility).
        if self.nc is not None and startup_event_bus is not None:
            try:
                profile = detect_local_profile(self.node_id)

                async def _on_repair(peer_id: str, attempt: int):
                    if attempt == -1:
                        # Escalate to peer-assist
                        logger.warning(f"EventBus: peer-assist escalation for {peer_id}")
                        if hasattr(self, "_peer_assist") and self._peer_assist:
                            await self._peer_assist.publish_struggle(peer_id, "quorum_escalation")
                    else:
                        logger.info(f"EventBus: self-repair attempt {attempt} for {peer_id}")
                        await self._probe_and_heal_peer(peer_id)

                self._event_bus = await startup_event_bus(
                    nc=self.nc,
                    node_id=self.node_id,
                    profile=profile,
                    peers=self._peers,
                    on_repair=_on_repair,
                    on_upgrade=None,  # transport = pure transport, no upgrade actions
                )
                logger.info(
                    f"EventBus active — repair_quorum (upgrade signals record-only), "
                    f"profile={profile.tier}/{profile.ram_gb}GB"
                )
            except Exception as e:
                logger.warning(f"EventBus setup failed (non-fatal): {e}")
                self._event_bus = None

        # Delegation receiver — subscribe after NATS is connected.
        # Workers only; chief skips (chief publishes, doesn't receive its own).
        if self.nc is not None and self._chief and self.node_id != self._chief:
            try:
                from multifleet.delegation_receiver import DelegationReceiver
                from multifleet.security import FleetSecurity
                self._delegation_queue_dir.mkdir(parents=True, exist_ok=True)
                self._delegation_receiver = DelegationReceiver(
                    self_node=self.node_id,
                    chief_node=self._chief,
                    security=FleetSecurity(),
                    nc=self.nc,
                    dispatch_cb=self._dispatch_delegation_to_queue,
                )
                await self._delegation_receiver.start()
                logger.info(
                    f"DelegationReceiver active — chief={self._chief} "
                    f"queue={self._delegation_queue_dir}"
                )
            except Exception as e:
                # Non-fatal: daemon still works without delegation path.
                logger.warning(f"DelegationReceiver setup failed (non-fatal): {e}")
                self._delegation_receiver = None

        # Delegation runner — FSEvents-triggered claude-code executor.
        # Ships dormant; flip DELEGATION_RUNNER_ENABLED=1 to activate. Runs on
        # BOTH chief and worker (chief may queue self-directed delegations).
        # Completely event-driven: no polling, only watchdog events + bounded
        # subprocess waits.
        if os.environ.get("DELEGATION_RUNNER_ENABLED", "0") == "1":
            try:
                from multifleet.delegation_runner import DelegationRunner
                from multifleet.security import FleetSecurity
                self._delegation_queue_dir.mkdir(parents=True, exist_ok=True)
                self._delegation_runner = DelegationRunner(
                    queue_dir=self._delegation_queue_dir,
                    nc=self.nc,
                    security=FleetSecurity(),
                    self_node=self.node_id,
                )
                started_ok = await self._delegation_runner.start()
                if started_ok:
                    logger.info(
                        f"DelegationRunner active — queue={self._delegation_queue_dir}"
                    )
                else:
                    logger.warning(
                        "DelegationRunner did not start (watchdog or claude-code "
                        "missing). Envelopes will queue but not auto-execute."
                    )
            except Exception as e:
                logger.warning(f"DelegationRunner setup failed (non-fatal): {e}")
                self._delegation_runner = None
        else:
            logger.info(
                "DelegationRunner dormant (set DELEGATION_RUNNER_ENABLED=1 to enable)"
            )

        # Start ACK retry loop (works with or without NATS -- uses send_with_fallback)
        asyncio.create_task(self._ack_retry_loop())
        # Fix B: start error-rate watchdog -- kills daemon if errors exceed threshold/min
        asyncio.create_task(self._error_rate_watchdog())
        # Start CommunicationProtocol self-heal loop (works with or without NATS)
        self.protocol.start()

        # GGG3 — M3 plist_drift sanity loop (trigger-based: primary is
        # _refresh_peer_ips hook; this 5min loop catches manual launchd
        # edits that bypass the trigger). mac2 audit 2026-05-12 flagged
        # _plist_drift_loop() was lost in the CCC3/R2 refactor lineage.
        asyncio.create_task(self._plist_drift_sanity_loop())
        # Run one drift check at startup so /health surfaces current state
        # immediately (otherwise daemon would show drift=False until the
        # first sanity loop tick or first peer-config change).
        try:
            self._check_plist_drift(reason="startup")
        except Exception as _e:
            logger.warning(f"plist_drift startup check failed: {_e!r}")

        # K1 2026-05-04 — Directed peer-assist evaluator. Watches
        # ChannelState for "P1+P2 broken-to-X ∧ P3 or P7 healthy-to-X" and
        # asks peer X to probe us back via /rpc/<peer>/diagnose_p1p2,
        # then writes a structured repair-request file. ZSF — the loop
        # bumps observable counters on every failure; never silent.
        try:
            from tools.peer_assist_directed import DirectedPeerAssist
            self._peer_assist_directed = DirectedPeerAssist(self)
            asyncio.create_task(self._peer_assist_directed.run())
            logger.info(
                "DirectedPeerAssist active — /health.peer_assist exposes "
                "broken_p1p2_peers + repair_requests_total"
            )
        except Exception as e:  # noqa: BLE001 — non-fatal; daemon still usable
            logger.warning(f"DirectedPeerAssist setup failed (non-fatal): {e}")
            self._peer_assist_directed = None

        # Keep running
        try:
            while self._running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            # Graceful shutdown of state_sync tasks
            if self._state_sync is not None:
                if self._state_sync._cold_sync_task is not None:
                    self._state_sync._cold_sync_task.cancel()
                try:
                    await asyncio.wait_for(
                        self._state_sync.publish_health("offline"), timeout=3
                    )
                except Exception:
                    pass
            if self.nc:
                try:
                    await asyncio.wait_for(self.nc.drain(), timeout=5)
                except Exception:
                    pass
                await self.nc.close()

    async def _start_http_compat(self):
        """Minimal HTTP server on :8855 for backward compatibility with existing tools."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        daemon = self
        main_loop = asyncio.get_running_loop()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle(self):
                # III1 — keep-alive guard: handle() loops handle_one_request for
                # HTTP/1.1 persistent connections; disconnect between iterations
                # surfaces here, not in handle_one_request.
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                    daemon._stats["http_request_peer_closed_total"] = (
                        daemon._stats.get("http_request_peer_closed_total", 0) + 1
                    )
                except Exception as _e:  # noqa: BLE001 — ZSF
                    daemon._stats["http_request_errors_total"] = (
                        daemon._stats.get("http_request_errors_total", 0) + 1
                    )

            def handle_one_request(self):
                # III1 — ROOT FIX for mac2 HTTP wedge at TCP accept layer.
                # BrokenPipeError in BaseHTTPRequestHandler.handle_one_request
                # propagates through process_request_thread, wedging ThreadingHTTPServer.
                # Catching here protects ALL endpoints regardless of write path.
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                    daemon._stats["http_request_peer_closed_total"] = (
                        daemon._stats.get("http_request_peer_closed_total", 0) + 1
                    )
                except Exception as _e:  # noqa: BLE001 — ZSF
                    daemon._stats["http_request_errors_total"] = (
                        daemon._stats.get("http_request_errors_total", 0) + 1
                    )

            def do_GET(self):
                # CV-001 parity: restrict sensitive GET endpoints to loopback + fleet peers.
                # /health is left open (health probes expected from monitoring);
                # /rpc/* exposes peer state; /messages/unread exposes recent traffic buffer.
                sensitive_paths = ("/rpc/", "/messages/unread", "/dispatches", "/chains",
                                   "/dashboard", "/dashboard/data")
                if any(self.path == p or self.path.startswith(p) for p in sensitive_paths):
                    if not self._validate_peer_ip():
                        self._json_response(403, {"error": "forbidden: peer IP not in allowlist"})
                        return

                # ── ContextDNA Claude Bridge — Anthropic-compat masquerade GET endpoints ──
                # Claude Code (Pro/Max OAuth flow) probes these at startup. If we
                # 404, response shape mismatches and Claude Code crashes parsing
                # `data.client_data` as undefined (TypeError reading 'alwaysThinking').
                # Stub returns minimal valid shape so startup succeeds.
                if self.path == "/api/oauth/claude_cli/client_data":
                    self._json_response(200, {
                        "client_data": {
                            "alwaysThinking": False,
                            "alwaysThinkingEnabled": False,
                        },
                    })
                    return
                if self.path.startswith("/api/oauth/"):
                    logger.info("Bridge OAuth GET stub: %s", self.path)
                    self._json_response(200, {})
                    return
                if self.path == "/v1/models" or self.path.startswith("/v1/models?"):
                    # Listed models include both Anthropic-native IDs (used by
                    # /v1/messages clients) and OpenAI-compatible IDs that the
                    # /v1/chat/completions shim accepts (used by 3-Surgeons
                    # cardiologist when THREE_SURGEONS_VIA_BRIDGE=1). Listing
                    # them here lets the 3s probe show OK rather than a WARN
                    # about "model not in /models list".
                    models = [
                        {"id": mid, "type": "model", "display_name": mid,
                         "created_at": "2025-01-01T00:00:00Z"}
                        for mid in (
                            "claude-opus-4-7", "claude-opus-4-1", "claude-opus-4-0",
                            "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-sonnet-4",
                            "claude-haiku-4-5",
                            # OpenAI-shim model IDs (forwarded through llm_priority_queue)
                            "deepseek-chat", "deepseek-reasoner",
                            "gpt-4.1-mini", "gpt-4o-mini",
                        )
                    ]
                    self._json_response(200, {
                        "data": models, "has_more": False,
                        "first_id": models[0]["id"], "last_id": models[-1]["id"],
                    })
                    return

                # ── SSE multiplex: /events/stream + /events/status ─────────
                # Loopback-only — IDE on same host. Long-lived; cap=5 in
                # multifleet.sse_multiplex (env-overridable).
                if self.path == "/events/stream" or self.path.startswith("/events/stream?"):
                    client_ip = self.client_address[0]
                    if client_ip not in ("127.0.0.1", "::1"):
                        self._json_response(403, {"error": "forbidden: SSE is loopback-only"})
                        return
                    try:
                        from multifleet import sse_multiplex
                        import urllib.parse as _u
                        query = _u.urlparse(self.path).query
                        sse_multiplex.stream_handler(self, query=query)
                    except Exception as exc:
                        logger.warning(f"SSE stream handler error: {exc!r}")
                        try:
                            self._json_response(500, {"error": f"sse failed: {exc!r}"})
                        except Exception as _e:
                            logger.debug("sse 500 send swallowed: %r", _e)
                    return
                if self.path == "/events/status":
                    try:
                        from multifleet import sse_multiplex
                        self._json_response(200, sse_multiplex.status())
                    except Exception as exc:
                        self._json_response(500, {"error": f"sse status failed: {exc!r}"})
                    return

                if self.path == "/health":
                    # KKK1 — serve pre-built snapshot from async event loop to
                    # avoid self.nc read-race and subprocess latency blocking
                    # HTTP workers. Fall back to live build if snapshot stale (>30s).
                    snap = getattr(daemon, "_health_snapshot", None)
                    snap_ts = getattr(daemon, "_health_snapshot_ts", 0.0)
                    if snap and (time.time() - snap_ts) < 30:
                        health_payload = snap
                        daemon._stats["health_served_from_snapshot"] = (
                            daemon._stats.get("health_served_from_snapshot", 0) + 1
                        )
                    else:
                        health_payload = daemon._build_health()
                        daemon._stats["health_served_live"] = (
                            daemon._stats.get("health_served_live", 0) + 1
                        )
                    data = json.dumps(health_payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    # ZSF: client-side disconnects (fast health pollers, kubectl
                    # probes, curl --max-time) routinely close mid-response.
                    # Server-side we don't care, but we MUST count so the
                    # invariant "no silent failures" holds — observable via
                    # /metrics + /health stats.client_disconnect.
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        daemon._stats["client_disconnect"] = (
                            daemon._stats.get("client_disconnect", 0) + 1
                        )
                elif self.path == "/metrics":
                    # RACE B6 — Prometheus-format exposition for counters.
                    # Text format (version 0.0.4) so scrapers consume directly.
                    text = daemon._build_prometheus_metrics()
                    data = text.encode()
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        "text/plain; version=0.0.4; charset=utf-8",
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/messages/unread" or self.path.startswith("/messages/unread?"):
                    # Polled by discord_bridge.py (and similar external relays) every 5s.
                    # Optional ?since=<unix_ts> filters to messages newer than that timestamp.
                    # Entries older than 300s are pruned on access (cheap TTL, no background task).
                    import urllib.parse as _urlparse
                    query = _urlparse.urlparse(self.path).query
                    params = _urlparse.parse_qs(query)
                    try:
                        since = float(params.get("since", ["0"])[0])
                    except (TypeError, ValueError):
                        since = 0.0
                    cutoff = time.time() - 300
                    floor = max(since, cutoff)
                    msgs = [m for m in daemon._recent_messages if m.get("_ts", 0) > floor]
                    data = json.dumps(msgs).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/channels":
                    state = daemon.protocol.get_channel_state()
                    data = json.dumps({
                        "nodeId": daemon.node_id,
                        "channels": state,
                        "timestamp": time.time(),
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/stats" or self.path.startswith("/stats?"):
                    # Adaptive channel re-ranking observability.
                    # Exposes per-peer per-channel rolling-window success rate
                    # (last N attempts where N = ChannelState.ROLLING_WINDOW).
                    # Fed by ChannelState.record_success/record_failure; consumed
                    # by dashboards + health probes verifying re-rank behaviour.
                    report = daemon.protocol.channel_state.success_rate_report()
                    data = json.dumps({
                        "nodeId": daemon.node_id,
                        "window_size": ChannelState.ROLLING_WINDOW,
                        "min_attempts_for_rerank": ChannelState.MIN_ATTEMPTS_FOR_RERANK,
                        "channel_success_rate": report,
                        "timestamp": time.time(),
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path.startswith("/rpc/"):
                    # Peer RPC over HTTP. Two modes:
                    #   GET /rpc/self/<method>           — local answer (no network)
                    #   GET /rpc/<peer>/<method>          — HTTP forward to peer's /rpc/self/<method>
                    # HTTP used because cluster sub-gossip for rpc.* is unreliable
                    # in hub-and-spoke topologies. HTTP works direct between all 3 nodes.
                    path_parts = self.path[5:].split("/")
                    if len(path_parts) < 2:
                        self._json_response(400, {"error": "usage: /rpc/<node|self>/<method>",
                                                    "valid_methods": ["status", "health", "commits", "tracks"]})
                        return
                    target_node, method = path_parts[0], path_parts[1]

                    def _build_local(m):
                        if m == "status": return daemon._build_rpc_status()
                        if m == "health": return daemon._build_health()
                        if m == "commits": return {"commits": daemon._recent_self_commits(limit=10)}
                        if m == "tracks": return {"tracks": daemon._read_current_tracks()}
                        if m == "diagnose_p1p2":
                            # K1 2026-05-04 — peer-assist directed probe-back.
                            # Caller passes ?from=<their_node_id>. We probe
                            # their daemon's P2 endpoint and check our P1
                            # heartbeat freshness, then return what we see.
                            try:
                                import urllib.parse as _u
                                q = _u.urlparse(self.path).query
                                params = _u.parse_qs(q)
                                from_node = (params.get("from", [""])[0]
                                             or "").strip()
                            except Exception as _e:
                                from_node = ""
                            if not from_node:
                                return {"error": "diagnose_p1p2 requires ?from=<node_id>"}
                            from tools.peer_assist_directed import diagnose_endpoint
                            cfg = (daemon._peers_config or {}).get(from_node) or {}
                            host = (cfg.get("lan_ip") or cfg.get("ip")
                                    or cfg.get("host") or cfg.get("mdns_name"))
                            if not host:
                                return {"error": f"unknown peer: {from_node}"}
                            try:
                                from multifleet.constants import FLEET_DAEMON_PORT as _PP
                                port = int(_PP)
                            except Exception:
                                port = 8855
                            return {
                                "diagnose_p1p2": diagnose_endpoint(
                                    target_host=host,
                                    target_port=port,
                                    target_node=from_node,
                                    peers_cache=daemon._peers,
                                ),
                                "responder": daemon.node_id,
                            }
                        return {"error": f"unknown method: {m}"}

                    if target_node == "self" or target_node == daemon.node_id:
                        reply = _build_local(method)
                        self._json_response(200, reply)
                        return
                    # Forward via HTTP to peer. Use raw IPv4 socket to bypass
                    # macOS dual-stack urllib quirks (IPv6 first → EHOSTUNREACH under launchd).
                    try:
                        import socket as _socket
                        peer_cfg = (daemon._peers_config or {}).get(target_node, {})
                        peer_host = (peer_cfg.get("lan_ip") or peer_cfg.get("host")
                                     or peer_cfg.get("ip") or peer_cfg.get("mdns_name"))
                        if not peer_host:
                            self._json_response(404, {"error": f"unknown peer: {target_node}",
                                                       "known_peers": list((daemon._peers_config or {}).keys())})
                            return
                        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                        sock.settimeout(5)
                        try:
                            sock.connect((peer_host, FLEET_DAEMON_PORT))
                            sock.sendall((f"GET /rpc/self/{method} HTTP/1.1\r\n"
                                          f"Host: {peer_host}\r\nConnection: close\r\n\r\n").encode())
                            raw = b""
                            while True:
                                chunk = sock.recv(65536)
                                if not chunk:
                                    break
                                raw += chunk
                        finally:
                            sock.close()
                        sep = raw.find(b"\r\n\r\n")
                        body = raw[sep + 4:] if sep > 0 else raw
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        err_name = type(e).__name__
                        self._json_response(504, {"error": f"RPC forward failed: {e!r}",
                                                    "detail": err_name, "peer": target_node,
                                                    "peer_host": peer_host if 'peer_host' in dir() else None})
                elif self.path == "/dashboard":
                    # Session 1 VISIBLE: serve TheatricalDashboard with live fleet
                    # snapshot via FleetDashboardData (which calls /health,
                    # /races, /evidence/latest on this daemon and shapes results
                    # into the snapshot format components expect — nodes list,
                    # rebuttals.active=int, etc.). Using raw _build_health()
                    # would crash quorum_pulse/probe_status_grid. Always served
                    # no-cache so the refresh=10 meta-tag shows live state.
                    # Falls back to legacy text dashboard if theatrical import
                    # fails.
                    content_type = "text/plain"
                    try:
                        from multifleet.theatrical.theatrical_dashboard import TheatricalDashboard
                        from multifleet.dashboard_data import FleetDashboardData
                        try:
                            from multifleet.evidence_ledger import EvidenceLedger
                            ledger = EvidenceLedger()
                        except Exception:
                            ledger = None
                        fdata = FleetDashboardData(daemon_port=FLEET_DAEMON_PORT, cache_ttl=2.0)
                        td = TheatricalDashboard(dashboard_data=fdata, evidence_ledger=ledger)
                        html = td.render()
                        # P2-B1+B5 convergent: inject live Vital Signs Bar
                        # (sticky 5-tile strip polling /metrics + /health every 5s).
                        # Distinct from the existing TheatricalDashboard tc-vital-bar
                        # (server-render); this one is client-poll for "thinking
                        # visible" IDE alignment without SPA framework.
                        html = _inject_vital_signs_bar(html)
                        data = html.encode()
                        content_type = "text/html; charset=utf-8"
                    except Exception:
                        try:
                            from tools.fleet_nerve_dashboard import render_dashboard
                            from tools.fleet_nerve_config import load_peers
                            peers, _chief = load_peers()
                            text = render_dashboard(daemon.node_id, peers, port=FLEET_DAEMON_PORT)
                            data = (text or "Fleet idle -- no activity").encode()
                        except Exception as e:
                            data = f"Dashboard error: {e}".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/dashboard/data":
                    # JSON snapshot of all 9 theatrical components for
                    # JS-based polling/WebSocket consumers. Mirrors what
                    # /dashboard renders, but data-only — no HTML, no view.
                    payload = {"timestamp": time.time(), "components": {}}
                    try:
                        from multifleet.theatrical.theatrical_dashboard import TheatricalDashboard
                        from multifleet.dashboard_data import FleetDashboardData
                        try:
                            from multifleet.evidence_ledger import EvidenceLedger
                            ledger = EvidenceLedger()
                        except Exception:
                            ledger = None
                        fdata = FleetDashboardData(daemon_port=FLEET_DAEMON_PORT, cache_ttl=2.0)
                        td = TheatricalDashboard(dashboard_data=fdata, evidence_ledger=ledger)
                        live = td.gather_data()
                        # Component-keyed view so clients can render selectively
                        fleet = live.get("fleet")
                        payload["components"] = {
                            "vital_signs": fleet,
                            "surgeon_feed": live.get("surgeon"),
                            "fleet_constellation": fleet,
                            "evidence_timeline": live.get("evidence"),
                            "quorum_pulse": fleet,
                            "memory_heat_map": live.get("memory"),
                            "probe_grid": live.get("probes"),
                            "corrigibility_gauge": live.get("corrigibility"),
                            "gold_stream": live.get("gold"),
                        }
                        payload["synaptic"] = live.get("synaptic")
                    except Exception as exc:
                        payload["error"] = str(exc)
                    body = json.dumps(payload, default=str).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/dashboard/legacy":
                    try:
                        from tools.fleet_nerve_dashboard import render_dashboard
                        from tools.fleet_nerve_config import load_peers
                        peers, _chief = load_peers()
                        text = render_dashboard(daemon.node_id, peers, port=FLEET_DAEMON_PORT)
                        data = (text or "Fleet idle -- no activity").encode()
                    except Exception as e:
                        data = f"Dashboard error: {e}".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/sprint-status":
                    # Cycle 6 F5 -- consolidated 5-cycle review page.
                    # Loopback-friendly (no peer-IP gate) so Aaron can browse
                    # from any machine on the LAN. ZSF: render errors fall
                    # back to a minimal HTML stub + 500 status.
                    try:
                        body = _build_sprint_status_html(daemon)
                        daemon._stats["sprint_status_views"] = (
                            daemon._stats.get("sprint_status_views", 0) + 1
                        )
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        try:
                            self.wfile.write(body)
                        except (BrokenPipeError, ConnectionResetError):
                            daemon._stats["client_disconnect"] = (
                                daemon._stats.get("client_disconnect", 0) + 1
                            )
                    except Exception as exc:
                        # ZSF: surface render failure as a tiny HTML stub
                        # rather than a silent traceback. Counter tracks
                        # repeat failures so dashboards alarm.
                        daemon._stats["sprint_status_render_errors"] = (
                            daemon._stats.get("sprint_status_render_errors", 0) + 1
                        )
                        msg = f"<html><body><h1>sprint-status render error</h1><pre>{exc!r}</pre></body></html>".encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(msg)))
                        self.end_headers()
                        try:
                            self.wfile.write(msg)
                        except Exception:
                            pass
                elif self.path == "/arbiter":
                    try:
                        from multifleet.human_arbiter_view import build_human_arbiter_html
                        html = build_human_arbiter_html()
                        data = html.encode()
                    except Exception as e:
                        data = f"<html><body><h1>Arbiter View Error</h1><pre>{e}</pre></body></html>".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/broadcast/health":
                    # Run async broadcast in the event loop
                    future = asyncio.run_coroutine_threadsafe(daemon.broadcast_health(), main_loop)
                    results = future.result(timeout=10)
                    online = sum(1 for r in results if r["status"] == 200)
                    data = json.dumps({
                        "nodes": len(results), "online": online, "results": results,
                        "transport": "nats",
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path.startswith("/ack/"):
                    msg_id = self.path[5:]  # strip "/ack/"
                    if daemon._store:
                        info = daemon._store.get_ack_status(msg_id)
                        if info:
                            data = json.dumps(info).encode()
                            self.send_response(200)
                        else:
                            data = json.dumps({"error": "message not found", "id": msg_id}).encode()
                            self.send_response(404)
                    else:
                        data = json.dumps({"error": "store not available"}).encode()
                        self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/dispatches":
                    self._json_response(200, {
                        "dispatches": {k: {kk: vv for kk, vv in v.items() if kk != "result"}
                                       for k, v in daemon._dispatches.items()},
                        "count": len(daemon._dispatches),
                    })
                elif self.path.startswith("/dispatch/"):
                    did = self.path[10:]  # strip "/dispatch/"
                    info = daemon._dispatches.get(did)
                    if info:
                        self._json_response(200, {"id": did, **info})
                    else:
                        self._json_response(404, {"error": "dispatch not found", "id": did})
                elif self.path == "/chains":
                    chains = {cid: daemon._get_chain_status(cid) for cid in daemon._chains}
                    self._json_response(200, {"chains": chains, "count": len(chains)})
                elif self.path.startswith("/chain/"):
                    cid = self.path[7:]  # strip "/chain/"
                    status = daemon._get_chain_status(cid)
                    code = 404 if "error" in status else 200
                    self._json_response(code, status)
                elif self.path == "/watchdog/suggestions":
                    sug_path = Path("/tmp/fleet-watchdog-suggestions.json")
                    if sug_path.exists():
                        try:
                            raw = sug_path.read_text()
                            json.loads(raw)  # validate JSON
                            data = raw.encode()
                            self.send_response(200)
                        except Exception:
                            data = json.dumps({"error": "corrupt suggestions file"}).encode()
                            self.send_response(500)
                    else:
                        data = json.dumps({
                            "node": daemon.node_id,
                            "scanned_at": None,
                            "suggestions": [],
                            "note": "no scan yet -- idle threshold not reached",
                        }).encode()
                        self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/evidence/latest":
                    try:
                        from multifleet.evidence_ledger import EvidenceLedger
                        ledger = EvidenceLedger()
                        entries = ledger.latest(count=20)
                        self._json_response(200, {"entries": entries, "count": len(entries)})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/evidence/stats":
                    try:
                        from multifleet.evidence_ledger import EvidenceLedger
                        ledger = EvidenceLedger()
                        self._json_response(200, ledger.stats)
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/evidence/verify":
                    try:
                        from multifleet.evidence_ledger import EvidenceLedger
                        ledger = EvidenceLedger()
                        result = ledger.verify_chain()
                        self._json_response(200, result)
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/api/surgeon-status":
                    try:
                        from multifleet.surgeon_adapter import SurgeonAdapter
                        adapter = SurgeonAdapter()
                        self._json_response(200, {
                            "available": adapter.is_available(),
                            "state": adapter.get_state(),
                            "counters": adapter.get_counters(),
                        })
                    except ImportError:
                        self._json_response(200, {"available": False, "error": "surgeon_adapter not importable"})
                    except Exception as e:
                        self._json_response(500, {"available": False, "error": str(e)})
                elif self.path.startswith("/rebuttal/"):
                    task_id = self.path[10:]  # strip "/rebuttal/"
                    if not daemon._rebuttal:
                        self._json_response(503, {"error": "rebuttal protocol not initialized"})
                    elif not task_id:
                        # List all proposals
                        proposals = [p.to_dict() for p in daemon._rebuttal.all_proposals()]
                        self._json_response(200, {"proposals": proposals, "count": len(proposals)})
                    else:
                        proposal = daemon._rebuttal.get_proposal_dict(task_id)
                        if proposal:
                            self._json_response(200, proposal)
                        else:
                            self._json_response(404, {"error": f"no proposal for task {task_id}"})
                else:
                    self.send_response(404)
                    self.end_headers()

            def _validate_peer_ip(self) -> bool:
                """CV-001: Validate request comes from known fleet peer or loopback."""
                client_ip = self.client_address[0]
                if client_ip in ("127.0.0.1", "::1"):
                    return True
                # Static config IPs
                allowed = {p.get("ip") for p in daemon._peers_config.values() if p.get("ip")}
                # Dynamic NATS peer IPs (from heartbeats)
                for peer_data in daemon._peers.values():
                    peer_ip = peer_data.get("ip")
                    if peer_ip:
                        allowed.add(peer_ip)
                if client_ip in allowed:
                    return True
                logger.warning(f"Rejected POST /message from unknown IP: {client_ip}")
                return False

            def _json_response(self, code, obj):
                # HHH9 — ROOT CAUSE FIX for mac2 daemon HTTP wedge.
                # Aaron 2026-05-12: client-side timeout (e.g. our curl --max-time 3)
                # raced the daemon's response. send_header/end_headers/write all
                # call socketserver.sendall, which raises BrokenPipeError /
                # ConnectionResetError on a closed peer socket. The exception
                # propagated up through process_request_thread, leaving the
                # HTTP responder in a torn-down state — subsequent requests
                # 100% timed out (observed: 10/10 probes returned code=000 in
                # ~3s on mac2 daemon @ uptime 5min, but logs showed prior
                # BrokenPipe traceback before silence began).
                #
                # Fix: catch the socket-close family of errors and ZSF-record.
                # ALL writer ops (send_response, send_header, end_headers, write)
                # need to be inside the try because any can raise once the peer
                # has gone.
                try:
                    data = json.dumps(obj).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as _e:
                    # Peer disconnected before we finished. Not a server bug;
                    # observable via counter so the rate of client-side
                    # timeouts is visible.
                    daemon._stats["http_response_peer_closed_total"] = (
                        daemon._stats.get("http_response_peer_closed_total", 0) + 1
                    )
                except Exception as _e:  # noqa: BLE001 — ZSF
                    daemon._stats["http_response_errors_total"] = (
                        daemon._stats.get("http_response_errors_total", 0) + 1
                    )

            def do_POST(self):
                # CV-001: Peer IP validation on all POST endpoints
                if not self._validate_peer_ip():
                    self._json_response(403, {"error": "forbidden: unknown peer IP"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                # JSON parse guard — bridge/v1/messages clients sometimes send
                # malformed bodies; surface clean Anthropic-shape 400 instead
                # of HTTP 000 connection-drop. Stress test A4 found this gap.
                try:
                    body = json.loads(self.rfile.read(length)) if length else {}
                except (ValueError, json.JSONDecodeError) as _parse_err:
                    daemon._stats["bridge_bad_request_envelopes"] = (
                        daemon._stats.get("bridge_bad_request_envelopes", 0) + 1
                    )
                    self._json_response(400, {
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": f"Invalid JSON body: {_parse_err}",
                        },
                    })
                    return

                # ── DEV-only synthetic event publisher (gated by FLEET_DEV_EVENTS=1) ──
                # Used to verify SSE multiplex end-to-end without waiting for real
                # surgeon/fleet activity. Loopback-only, env-gated, never enabled
                # by default. See multifleet/sse_multiplex.py.
                if self.path == "/events/publish":
                    client_ip = self.client_address[0]
                    if client_ip not in ("127.0.0.1", "::1"):
                        self._json_response(403, {"error": "forbidden: dev publish loopback-only"})
                        return
                    try:
                        from multifleet import sse_multiplex
                        sse_multiplex.publish_handler(self, body)
                    except Exception as exc:
                        self._json_response(500, {"error": f"publish failed: {exc!r}"})
                    return

                # ── OpenAI-compatible shim — /v1/chat/completions ─────────────
                # Used by 3-Surgeons cardiologist when THREE_SURGEONS_VIA_BRIDGE=1
                # (B3 #2: observability unification). 3s LLMProvider POSTs an
                # OpenAI Chat Completions request; we translate to Anthropic
                # body shape, run through the same Anthropic→DeepSeek fallback
                # ladder as /v1/messages, then translate the answer back to
                # OpenAI Chat Completions response so 3s parses it natively.
                #
                # Default routing for 3s remains direct (api.deepseek.com); this
                # shim only fires when Aaron opts in. Loopback-only via
                # _validate_peer_ip (already enforced for all POSTs above).
                if self.path == "/v1/chat/completions":
                    try:
                        daemon._stats["bridge_chat_completions_requests"] = (
                            daemon._stats.get("bridge_chat_completions_requests", 0) + 1
                        )
                        # ── Translate OpenAI Chat Completions → Anthropic body ──
                        # 3s sends: {model, messages:[{role,content}], max_tokens, temperature}
                        # Anthropic wants: {model, messages, system?, max_tokens, ...}
                        oai_messages = body.get("messages") or []
                        if not isinstance(oai_messages, list) or not oai_messages:
                            self._json_response(400, {"error": {
                                "message": "messages must be a non-empty array",
                                "type": "invalid_request_error",
                            }})
                            return
                        sys_parts: list = []
                        ant_messages: list = []
                        for m in oai_messages:
                            role = m.get("role", "user")
                            content = m.get("content", "")
                            if not isinstance(content, str):
                                # OpenAI multimodal blocks not supported by shim — best-effort flatten
                                if isinstance(content, list):
                                    content = " ".join(
                                        b.get("text","") for b in content if isinstance(b, dict)
                                    )
                                else:
                                    content = str(content)
                            if role == "system":
                                sys_parts.append(content)
                            elif role in ("user", "assistant"):
                                ant_messages.append({"role": role, "content": content})
                            else:
                                # tool/function roles → fold into user as readable hint
                                ant_messages.append({"role": "user", "content": f"[{role}]: {content}"})
                        oai_model = body.get("model", "deepseek-chat")
                        # Map to a Claude tier for the upstream try; the user's
                        # actual choice is preserved in the OpenAI response model field.
                        claude_proxy_model = "claude-sonnet-4-6"
                        ant_body = {
                            "model": claude_proxy_model,
                            "messages": ant_messages,
                            "max_tokens": int(body.get("max_tokens") or 1024),
                        }
                        if sys_parts:
                            ant_body["system"] = "\n\n".join(sys_parts)
                        if "temperature" in body:
                            ant_body["temperature"] = body["temperature"]

                        # ── Step 1: try Anthropic upstream (full quality) ────
                        bearer_cc = _extract_oauth_bearer(self)
                        anthropic_key_cc = _resolve_anthropic_key()
                        upstream_used_cc: Optional[str] = None
                        status_cc = 0
                        ant_resp_cc: Optional[dict] = None
                        ant_err_cc: Optional[str] = None
                        if bearer_cc:
                            upstream_used_cc = "oauth"
                            status_cc, ant_resp_cc, ant_err_cc = _try_anthropic_upstream(
                                ant_body, bearer_token=bearer_cc,
                            )
                        elif anthropic_key_cc:
                            upstream_used_cc = "apikey"
                            status_cc, ant_resp_cc, ant_err_cc = _try_anthropic_upstream(
                                ant_body, api_key=anthropic_key_cc,
                            )

                        if upstream_used_cc and status_cc == 200 and ant_resp_cc is not None:
                            # Translate Anthropic response → OpenAI Chat Completions
                            content_blocks = ant_resp_cc.get("content") or []
                            text_out = "".join(
                                b.get("text","") for b in content_blocks
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                            usage = ant_resp_cc.get("usage") or {}
                            in_tok = int(usage.get("input_tokens") or 0)
                            out_tok = int(usage.get("output_tokens") or 0)
                            daemon._stats["bridge_chat_completions_anthropic_ok"] = (
                                daemon._stats.get("bridge_chat_completions_anthropic_ok", 0) + 1
                            )
                            daemon._stats["bridge_anthropic_ok"] = (
                                daemon._stats.get("bridge_anthropic_ok", 0) + 1
                            )
                            self._json_response(200, {
                                "id": f"chatcmpl-bridge-{int(time.time()*1000)}",
                                "object": "chat.completion",
                                "created": int(time.time()),
                                "model": oai_model,
                                "choices": [{
                                    "index": 0,
                                    "message": {"role": "assistant", "content": text_out},
                                    "finish_reason": "stop",
                                }],
                                "usage": {
                                    "prompt_tokens": in_tok,
                                    "completion_tokens": out_tok,
                                    "total_tokens": in_tok + out_tok,
                                },
                            })
                            return

                        if upstream_used_cc:
                            ant_resp_type_cc = ""
                            if isinstance(ant_resp_cc, dict):
                                err_obj = ant_resp_cc.get("error") or {}
                                ant_resp_type_cc = err_obj.get("type", "") if isinstance(err_obj, dict) else ""
                            should_fallback_cc = (
                                status_cc == 429
                                or status_cc == 529
                                or (500 <= status_cc < 600)
                                or status_cc == 0
                                or ant_resp_type_cc in ("overloaded_error", "rate_limit_error")
                            )
                            if not should_fallback_cc and status_cc != 0 and ant_resp_cc is not None:
                                err_obj2 = ant_resp_cc.get("error") if isinstance(ant_resp_cc, dict) else None
                                msg = "upstream error"
                                if isinstance(err_obj2, dict):
                                    msg = err_obj2.get("message", msg)
                                daemon._stats["bridge_anthropic_passthrough_errors"] = (
                                    daemon._stats.get("bridge_anthropic_passthrough_errors", 0) + 1
                                )
                                self._json_response(status_cc, {
                                    "error": {"message": msg, "type": "upstream_error", "code": status_cc}
                                })
                                return
                            daemon._stats["bridge_chat_completions_fallback_to_deepseek"] = (
                                daemon._stats.get("bridge_chat_completions_fallback_to_deepseek", 0) + 1
                            )
                            daemon._stats["bridge_fallback_to_deepseek"] = (
                                daemon._stats.get("bridge_fallback_to_deepseek", 0) + 1
                            )
                            logger.info(
                                "Bridge chat-completions fallback (%s): Anthropic %s → DeepSeek",
                                upstream_used_cc, status_cc,
                            )
                        else:
                            daemon._stats["bridge_chat_completions_skipped"] = (
                                daemon._stats.get("bridge_chat_completions_skipped", 0) + 1
                            )
                            daemon._stats["bridge_anthropic_skipped"] = (
                                daemon._stats.get("bridge_anthropic_skipped", 0) + 1
                            )

                        # ── Step 2: DeepSeek path via llm_priority_queue ─────
                        try:
                            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
                            from memory.llm_priority_queue import llm_generate, Priority
                        except Exception as imp_err:
                            self._json_response(503, {"error": {
                                "message": f"llm_priority_queue unavailable: {imp_err!r}",
                                "type": "service_unavailable_error",
                            }})
                            return

                        sys_text_cc = "\n\n".join(sys_parts) if sys_parts else ""
                        flat_lines_cc: list = []
                        for m in ant_messages:
                            flat_lines_cc.append(f"[{m['role']}]: {m['content']}")
                        user_text_cc = "\n\n".join(flat_lines_cc)

                        # 3s consult routes via Priority.EXTERNAL (P3) — eligible
                        # for DeepSeek when local LLM is unavailable. P4/BACKGROUND
                        # would be local-only (cost control for gold mining), which
                        # defeats the bridge's "always answer" goal.
                        result_cc = llm_generate(
                            system_prompt=sys_text_cc,
                            user_prompt=user_text_cc,
                            priority=Priority.EXTERNAL,
                            profile="deep",
                            caller=f"3s-bridge:{oai_model}",
                            timeout_s=180.0,
                        )
                        if not result_cc:
                            daemon._stats["bridge_chat_completions_failures"] = (
                                daemon._stats.get("bridge_chat_completions_failures", 0) + 1
                            )
                            daemon._stats["bridge_deepseek_failures"] = (
                                daemon._stats.get("bridge_deepseek_failures", 0) + 1
                            )
                            self._json_response(503, {"error": {
                                "message": "Bridge exhausted: Anthropic + DeepSeek both failed",
                                "type": "service_unavailable_error",
                            }})
                            return

                        in_tok_cc = max(1, len(sys_text_cc + user_text_cc) // 4)
                        out_tok_cc = max(1, len(result_cc) // 4)
                        self._json_response(200, {
                            "id": f"chatcmpl-bridge-{int(time.time()*1000)}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": oai_model,
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": result_cc},
                                "finish_reason": "stop",
                            }],
                            "usage": {
                                "prompt_tokens": in_tok_cc,
                                "completion_tokens": out_tok_cc,
                                "total_tokens": in_tok_cc + out_tok_cc,
                            },
                        })
                        return
                    except Exception as cc_err:
                        logger.warning("Bridge /v1/chat/completions error: %s", cc_err)
                        daemon._stats["bridge_chat_completions_failures"] = (
                            daemon._stats.get("bridge_chat_completions_failures", 0) + 1
                        )
                        self._json_response(500, {"error": {
                            "message": f"bridge error: {cc_err!r}",
                            "type": "internal_server_error",
                        }})
                        return

                # ── ContextDNA Claude Bridge — Anthropic-compatible /v1/messages ──
                # Per Doc 3 plan cbaf78c7: route Claude Code through DeepSeek so
                # Aaron's session never hits Anthropic's quota. MVP: non-streaming
                # text I/O. Translates Anthropic Messages request → llm_generate
                # → Anthropic response shape. Loopback-only enforced via existing
                # _validate_peer_ip (already gates localhost = OK, peers = OK,
                # external = 403). Tool-use + streaming deferred — works for plain
                # Q&A, may degrade for tool-heavy agent loops.
                if self.path == "/v1/messages":
                    try:
                        # ── STREAMING BRANCH (Claude Code TUI requires SSE) ──
                        # Re-shipped after lost-in-rebase. Try real Anthropic
                        # SSE pass-through first; fall to synthesized SSE
                        # around DeepSeek non-stream when fallback fires.
                        if body.get("stream") is True:
                            daemon._stats["bridge_stream_requests"] = (
                                daemon._stats.get("bridge_stream_requests", 0) + 1
                            )
                            # OAuth Bearer pass-through takes precedence when
                            # opt-in via BRIDGE_OAUTH_PASSTHROUGH=1 + valid
                            # sk-ant-oat-* in Authorization. Falls through to
                            # api-key path on bearer non-200.
                            bearer_s = _extract_oauth_bearer(self)
                            anthropic_key_s = _resolve_anthropic_key()
                            tried_upstream_s = False
                            if bearer_s:
                                daemon._stats["bridge_oauth_passthrough_attempted"] = (
                                    daemon._stats.get("bridge_oauth_passthrough_attempted", 0) + 1
                                )
                                tried_upstream_s = True
                                committed, status_s = _stream_anthropic_passthrough(
                                    self, body, bearer_token=bearer_s,
                                )
                                if committed:
                                    daemon._stats["bridge_oauth_passthrough_success"] = (
                                        daemon._stats.get("bridge_oauth_passthrough_success", 0) + 1
                                    )
                                    daemon._stats["bridge_anthropic_ok"] = (
                                        daemon._stats.get("bridge_anthropic_ok", 0) + 1
                                    )
                                    daemon._stats["bridge_stream_anthropic_passthru"] = (
                                        daemon._stats.get("bridge_stream_anthropic_passthru", 0) + 1
                                    )
                                    return
                                daemon._stats["bridge_oauth_passthrough_failed"] = (
                                    daemon._stats.get("bridge_oauth_passthrough_failed", 0) + 1
                                )
                                if status_s and status_s != 429 and status_s != 529 and status_s < 500:
                                    daemon._stats["bridge_anthropic_passthrough_errors"] = (
                                        daemon._stats.get("bridge_anthropic_passthrough_errors", 0) + 1
                                    )
                                    self._json_response(status_s, {
                                        "type": "error",
                                        "error": {"type": "upstream_error",
                                                  "message": f"anthropic oauth returned {status_s}"},
                                    })
                                    return
                                daemon._stats["bridge_fallback_to_deepseek"] = (
                                    daemon._stats.get("bridge_fallback_to_deepseek", 0) + 1
                                )
                            elif anthropic_key_s:
                                tried_upstream_s = True
                                committed, status_s = _stream_anthropic_passthrough(
                                    self, body, api_key=anthropic_key_s,
                                )
                                if committed:
                                    daemon._stats["bridge_anthropic_ok"] = (
                                        daemon._stats.get("bridge_anthropic_ok", 0) + 1
                                    )
                                    daemon._stats["bridge_stream_anthropic_passthru"] = (
                                        daemon._stats.get("bridge_stream_anthropic_passthru", 0) + 1
                                    )
                                    return
                                if status_s and status_s != 429 and status_s != 529 and status_s < 500:
                                    daemon._stats["bridge_anthropic_passthrough_errors"] = (
                                        daemon._stats.get("bridge_anthropic_passthrough_errors", 0) + 1
                                    )
                                    self._json_response(status_s, {
                                        "type": "error",
                                        "error": {"type": "upstream_error",
                                                  "message": f"anthropic returned {status_s}"},
                                    })
                                    return
                                daemon._stats["bridge_fallback_to_deepseek"] = (
                                    daemon._stats.get("bridge_fallback_to_deepseek", 0) + 1
                                )
                            if not tried_upstream_s:
                                daemon._stats["bridge_anthropic_skipped"] = (
                                    daemon._stats.get("bridge_anthropic_skipped", 0) + 1
                                )
                            # Synth SSE from DeepSeek text
                            try:
                                sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
                                from memory.llm_priority_queue import llm_generate, Priority
                            except Exception as imp_err:
                                self._json_response(503, {
                                    "type": "error",
                                    "error": {"type": "service_unavailable_error",
                                              "message": f"llm_priority_queue: {imp_err!r}"},
                                })
                                return
                            model_s = body.get("model", "claude-sonnet-4-6")
                            profile_s = "extract_deep" if "haiku" in model_s.lower() else "deep"
                            sys_raw_s = body.get("system", "")
                            if isinstance(sys_raw_s, list):
                                sys_text_s = "\n".join(b.get("text","") for b in sys_raw_s if isinstance(b,dict))
                            else:
                                sys_text_s = str(sys_raw_s or "")
                            msgs_s = body.get("messages") or []
                            flat_s: list = []
                            for m in msgs_s:
                                role_s = m.get("role", "user")
                                content_s = m.get("content", "")
                                if isinstance(content_s, str):
                                    flat_s.append(f"[{role_s}]: {content_s}")
                                    continue
                                for blk in content_s if isinstance(content_s, list) else []:
                                    bt = blk.get("type")
                                    if bt == "text":
                                        flat_s.append(f"[{role_s}]: {blk.get('text','')}")
                                    elif bt == "tool_use":
                                        flat_s.append(
                                            f"[{role_s} called {blk.get('name','?')}]: "
                                            f"{json.dumps(blk.get('input',{}))[:500]}"
                                        )
                                    elif bt == "tool_result":
                                        tc = blk.get("content","")
                                        if isinstance(tc, list):
                                            tc = " ".join(b.get("text","") for b in tc if isinstance(b,dict))
                                        flat_s.append(f"[tool_result]: {str(tc)[:500]}")
                                    elif bt == "image":
                                        flat_s.append(f"[{role_s} sent image]")
                            user_text_s = "\n\n".join(flat_s)
                            result_s = llm_generate(
                                system_prompt=sys_text_s, user_prompt=user_text_s,
                                priority=Priority.AARON, profile=profile_s,
                                caller=f"claude-bridge-stream:{model_s}", timeout_s=180.0,
                            )
                            if not result_s:
                                daemon._stats["bridge_deepseek_failures"] = (
                                    daemon._stats.get("bridge_deepseek_failures", 0) + 1
                                )
                                self._json_response(503, {
                                    "type": "error",
                                    "error": {"type": "service_unavailable_error",
                                              "message": "Bridge exhausted: Anthropic + DeepSeek both failed"},
                                })
                                return
                            in_tokens_s = max(1, len(sys_text_s + user_text_s) // 4)
                            daemon._stats["bridge_stream_synthetic_deepseek"] = (
                                daemon._stats.get("bridge_stream_synthetic_deepseek", 0) + 1
                            )
                            _emit_anthropic_sse_synthetic(self, result_s, model_s, in_tokens_s)
                            return

                        # ── NON-STREAMING (JSON) BRANCH ───────────────────────
                        # Step 1: try Anthropic upstream first (full quality).
                        # OAuth Bearer (Pro/Max) takes precedence when opt-in
                        # via BRIDGE_OAUTH_PASSTHROUGH=1. Else fall back to
                        # api-key path (env / Keychain).  On 429/5xx/overloaded
                        # → fall to DeepSeek (Aaron's quota survival path).
                        # On other 4xx → surface as-is.
                        bearer_ns = _extract_oauth_bearer(self)
                        anthropic_key = _resolve_anthropic_key()
                        upstream_used: Optional[str] = None  # "oauth" | "apikey" | None
                        status = 0
                        ant_resp: Optional[dict] = None
                        ant_err: Optional[str] = None
                        if bearer_ns:
                            upstream_used = "oauth"
                            daemon._stats["bridge_oauth_passthrough_attempted"] = (
                                daemon._stats.get("bridge_oauth_passthrough_attempted", 0) + 1
                            )
                            status, ant_resp, ant_err = _try_anthropic_upstream(
                                body, bearer_token=bearer_ns,
                            )
                            if status == 200 and ant_resp is not None:
                                daemon._stats["bridge_oauth_passthrough_success"] = (
                                    daemon._stats.get("bridge_oauth_passthrough_success", 0) + 1
                                )
                                daemon._stats["bridge_anthropic_ok"] = (
                                    daemon._stats.get("bridge_anthropic_ok", 0) + 1
                                )
                                self._json_response(200, ant_resp)
                                return
                            daemon._stats["bridge_oauth_passthrough_failed"] = (
                                daemon._stats.get("bridge_oauth_passthrough_failed", 0) + 1
                            )
                        elif anthropic_key:
                            upstream_used = "apikey"
                            status, ant_resp, ant_err = _try_anthropic_upstream(
                                body, api_key=anthropic_key,
                            )
                            if status == 200 and ant_resp is not None:
                                daemon._stats["bridge_anthropic_ok"] = (
                                    daemon._stats.get("bridge_anthropic_ok", 0) + 1
                                )
                                self._json_response(200, ant_resp)
                                return
                        if upstream_used is not None:
                            # Fall-through cases: 429 (quota), 529 (overloaded),
                            # any 5xx, network errors (status==0). Try DeepSeek.
                            ant_resp_type = ""
                            if isinstance(ant_resp, dict):
                                ant_err_obj = ant_resp.get("error") or {}
                                ant_resp_type = ant_err_obj.get("type", "") if isinstance(ant_err_obj, dict) else ""
                            should_fallback = (
                                status == 429
                                or status == 529
                                or (500 <= status < 600)
                                or status == 0
                                or ant_resp_type in ("overloaded_error", "rate_limit_error")
                            )
                            if not should_fallback and status != 0 and ant_resp is not None:
                                # 4xx other than 429 → Aaron's request bug, surface as-is
                                daemon._stats["bridge_anthropic_passthrough_errors"] = (
                                    daemon._stats.get("bridge_anthropic_passthrough_errors", 0) + 1
                                )
                                self._json_response(status, ant_resp)
                                return
                            # Falling to DeepSeek
                            daemon._stats["bridge_fallback_to_deepseek"] = (
                                daemon._stats.get("bridge_fallback_to_deepseek", 0) + 1
                            )
                            logger.info(
                                "Bridge fallback (%s): Anthropic %s%s → DeepSeek",
                                upstream_used,
                                status,
                                f" ({ant_err})" if ant_err else f" ({ant_resp_type})" if ant_resp_type else "",
                            )
                        else:
                            # No upstream credential — direct to DeepSeek
                            daemon._stats["bridge_anthropic_skipped"] = (
                                daemon._stats.get("bridge_anthropic_skipped", 0) + 1
                            )

                        # ── Step 2: DeepSeek path (existing MVP logic) ──────
                        # Map Anthropic model → llm_priority_queue profile.
                        # opus/sonnet → deep (highest quality routing).
                        # haiku → classify (fast/cheap path; classify is local-only
                        #   per _LOCAL_ONLY_PROFILES, may fail if local LLM down —
                        #   so we promote to 'extract' which is external-eligible).
                        model = body.get("model", "claude-sonnet-4-6")
                        if "haiku" in model.lower():
                            profile = "extract_deep"  # external-eligible, cheapest tier
                        elif "opus" in model.lower():
                            profile = "deep"
                        else:  # sonnet + default
                            profile = "deep"

                        # Anthropic top-level system → system_prompt.
                        # Spec allows system as str OR list of {type:text, text:str}.
                        sys_raw = body.get("system", "")
                        if isinstance(sys_raw, list):
                            sys_text = "\n".join(
                                b.get("text", "") for b in sys_raw if isinstance(b, dict)
                            )
                        else:
                            sys_text = str(sys_raw or "")

                        # ── G1: tool-aware DeepSeek branch ───────────────────
                        # When the incoming Anthropic request carries `tools`
                        # and/or `tool_choice`, bypass the text-only llm_generate
                        # path (which can't return tool_calls) and POST direct
                        # to DeepSeek's OpenAI-compatible /chat/completions with
                        # the translated tool schema. Translate the response
                        # back to Anthropic tool_use blocks so Claude Code's
                        # session continues unchanged.
                        ant_tools = body.get("tools")
                        ant_tool_choice = body.get("tool_choice")
                        if ant_tools or ant_tool_choice:
                            daemon._stats["bridge_tool_translate_attempted"] = (
                                daemon._stats.get("bridge_tool_translate_attempted", 0) + 1
                            )
                            # Cycle 8 H3 — observability for the 3 schema-fidelity gaps
                            # the 3-Surgeons consult flagged. Counters + one-line log per
                            # hit; no behavior change. ZSF: silent drops become measurable.
                            try:
                                req_model = str(body.get("model") or "")
                                if "reasoner" in req_model.lower():
                                    daemon._stats["bridge_tool_reasoner_to_chat_downgrades"] = (
                                        daemon._stats.get("bridge_tool_reasoner_to_chat_downgrades", 0) + 1
                                    )
                                    logger.info(
                                        "G1 reasoner->chat downgrade: client asked %s with tools; deepseek-reasoner has no tool support, serving via deepseek-chat",
                                        req_model,
                                    )
                                if body.get("disable_parallel_tool_use") is True:
                                    daemon._stats["bridge_tool_disable_parallel_dropped"] = (
                                        daemon._stats.get("bridge_tool_disable_parallel_dropped", 0) + 1
                                    )
                                    logger.info(
                                        "G1 disable_parallel_tool_use=true dropped: DeepSeek has no equivalent; parallel calls may occur",
                                    )
                                if isinstance(ant_tools, list) and any(
                                    isinstance(t, dict) and t.get("cache_control") for t in ant_tools
                                ):
                                    daemon._stats["bridge_tool_cache_control_dropped"] = (
                                        daemon._stats.get("bridge_tool_cache_control_dropped", 0) + 1
                                    )
                                    logger.info(
                                        "G1 cache_control on tool defs dropped: no DeepSeek prompt-cache surface",
                                    )
                            except Exception as gap_err:
                                # ZSF: gap-observability must never break the request path.
                                logger.warning("G1 gap-counter increment failed: %s", gap_err)
                            try:
                                oai_tools = _translate_anthropic_tools_to_openai(ant_tools)
                                oai_tool_choice = _translate_anthropic_tool_choice_to_openai(ant_tool_choice)
                                oai_messages = _flatten_anthropic_messages_to_openai(
                                    body.get("messages") or [], sys_text,
                                )
                                # deepseek-reasoner has no tool support — force
                                # deepseek-chat regardless of profile mapping.
                                ds_status, ds_resp, ds_err = _call_deepseek_with_tools(
                                    messages=oai_messages,
                                    tools=oai_tools,
                                    tool_choice=oai_tool_choice,
                                    model="deepseek-chat",
                                    max_tokens=int(body.get("max_tokens") or 4096),
                                    temperature=float(body.get("temperature", 0.7)),
                                )
                                if ds_status == 200 and isinstance(ds_resp, dict):
                                    choices = ds_resp.get("choices") or []
                                    msg0 = (choices[0] or {}).get("message", {}) if choices else {}
                                    msg_text = msg0.get("content") or ""
                                    raw_tool_calls = msg0.get("tool_calls") or []
                                    tool_use_blocks = _translate_openai_tool_calls_to_anthropic(raw_tool_calls)
                                    daemon._stats["bridge_tool_calls_translated_count"] = (
                                        daemon._stats.get("bridge_tool_calls_translated_count", 0)
                                        + len(tool_use_blocks)
                                    )
                                    content_blocks = []
                                    if msg_text:
                                        content_blocks.append({"type": "text", "text": msg_text})
                                    content_blocks.extend(tool_use_blocks)
                                    if not content_blocks:
                                        content_blocks.append({"type": "text", "text": ""})
                                    finish = (choices[0] or {}).get("finish_reason") if choices else None
                                    stop_reason = "tool_use" if tool_use_blocks else (
                                        "end_turn" if finish in (None, "stop") else "end_turn"
                                    )
                                    usage = ds_resp.get("usage") or {}
                                    in_tok = int(usage.get("prompt_tokens") or max(1, len(sys_text) // 4))
                                    out_tok = int(usage.get("completion_tokens") or max(1, len(msg_text) // 4))
                                    daemon._stats["bridge_tool_translate_success"] = (
                                        daemon._stats.get("bridge_tool_translate_success", 0) + 1
                                    )
                                    if body.get("stream") is True:
                                        # Streaming caller fell through Anthropic
                                        # path above; emit synthetic SSE here.
                                        daemon._stats["bridge_stream_synthetic_deepseek"] = (
                                            daemon._stats.get("bridge_stream_synthetic_deepseek", 0) + 1
                                        )
                                        _emit_anthropic_sse_synthetic_with_tools(
                                            self, msg_text, tool_use_blocks, model, in_tok,
                                        )
                                        return
                                    self._json_response(200, {
                                        "id": f"msg_bridge_{int(time.time()*1000)}",
                                        "type": "message",
                                        "role": "assistant",
                                        "model": model,
                                        "content": content_blocks,
                                        "stop_reason": stop_reason,
                                        "stop_sequence": None,
                                        "usage": {
                                            "input_tokens": in_tok,
                                            "output_tokens": out_tok,
                                        },
                                    })
                                    return
                                # Non-200 from DeepSeek-with-tools: count + log,
                                # then drop through to text-only fallback so the
                                # session still gets *some* answer rather than 503.
                                daemon._stats["bridge_tool_translate_failed"] = (
                                    daemon._stats.get("bridge_tool_translate_failed", 0) + 1
                                )
                                logger.warning(
                                    "G1 tool-aware DeepSeek failed: status=%s err=%s body=%s",
                                    ds_status, ds_err,
                                    (str(ds_resp)[:200] if ds_resp else None),
                                )
                                print(
                                    f"[G1] tool-aware DeepSeek failed status={ds_status} err={ds_err}",
                                    file=sys.stderr, flush=True,
                                )
                            except Exception as g1_err:
                                daemon._stats["bridge_tool_translate_failed"] = (
                                    daemon._stats.get("bridge_tool_translate_failed", 0) + 1
                                )
                                logger.warning("G1 translation exception: %s", g1_err)
                                print(
                                    f"[G1] translation exception: {g1_err!r}",
                                    file=sys.stderr, flush=True,
                                )
                            # fall through to text-only path below
                        # ── /G1 ──────────────────────────────────────────────

                        # Anthropic messages → flatten to user_prompt. Each message
                        # is {role, content} where content is str OR list of
                        # blocks (text/tool_use/tool_result/image). We extract text
                        # only; tool blocks become readable summaries so the LLM
                        # can reason about them (degraded but workable).
                        msgs = body.get("messages") or []
                        flat_lines: list = []
                        for m in msgs:
                            role = m.get("role", "user")
                            content = m.get("content", "")
                            if isinstance(content, str):
                                flat_lines.append(f"[{role}]: {content}")
                                continue
                            for block in content if isinstance(content, list) else []:
                                btype = block.get("type")
                                if btype == "text":
                                    flat_lines.append(f"[{role}]: {block.get('text','')}")
                                elif btype == "tool_use":
                                    flat_lines.append(
                                        f"[{role} called tool {block.get('name','?')}]: "
                                        f"{json.dumps(block.get('input', {}))[:500]}"
                                    )
                                elif btype == "tool_result":
                                    tc = block.get("content", "")
                                    if isinstance(tc, list):
                                        tc = " ".join(
                                            b.get("text","") for b in tc if isinstance(b, dict)
                                        )
                                    flat_lines.append(f"[tool_result]: {str(tc)[:500]}")
                                elif btype == "image":
                                    flat_lines.append(f"[{role} sent image]")
                        user_text = "\n\n".join(flat_lines)

                        # Import llm_generate here so daemon doesn't fail at startup
                        # if memory/ is missing on bare-bones installs.
                        try:
                            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
                            from memory.llm_priority_queue import llm_generate, Priority
                        except Exception as imp_err:
                            self._json_response(503, {
                                "type": "error",
                                "error": {
                                    "type": "service_unavailable_error",
                                    "message": f"llm_priority_queue unavailable: {imp_err!r}",
                                },
                            })
                            return

                        # Aaron's chat = Priority.AARON (highest, preempts background work).
                        result = llm_generate(
                            system_prompt=sys_text,
                            user_prompt=user_text,
                            priority=Priority.AARON,
                            profile=profile,
                            caller=f"claude-bridge:{model}",
                            timeout_s=180.0,
                        )
                        if not result:
                            daemon._stats["bridge_deepseek_failures"] = (
                                daemon._stats.get("bridge_deepseek_failures", 0) + 1
                            )
                            self._json_response(503, {
                                "type": "error",
                                "error": {
                                    "type": "service_unavailable_error",
                                    "message": (
                                        "Bridge exhausted: Anthropic upstream + DeepSeek "
                                        "fallback both failed. Check fleet daemon logs."
                                    ),
                                },
                            })
                            return

                        # Build Anthropic Messages response shape.
                        msg_id = f"msg_bridge_{int(time.time()*1000)}"
                        # Rough token estimate: 1 token ≈ 4 chars (Anthropic's own heuristic).
                        in_tokens = max(1, len(sys_text + user_text) // 4)
                        out_tokens = max(1, len(result) // 4)
                        self._json_response(200, {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [{"type": "text", "text": result}],
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {
                                "input_tokens": in_tokens,
                                "output_tokens": out_tokens,
                            },
                        })
                        return
                    except Exception as bridge_err:
                        logger.warning("ContextDNA Claude Bridge error: %s", bridge_err)
                        self._json_response(500, {
                            "type": "error",
                            "error": {
                                "type": "internal_server_error",
                                "message": f"bridge error: {bridge_err!r}",
                            },
                        })
                        return

                if self.path == "/message":
                    # ── target=superset shortcut ──
                    # action=prompt(default)|task|tasks|update-task
                    if body.get("target") == "superset":
                        action = body.get("action", "prompt")
                        daemon._stats.setdefault("message_superset_routed_total", 0)
                        daemon._stats.setdefault("message_superset_errors", 0)
                        try:
                            from multifleet import vscode_superset_bridge as _vsb
                            if action == "shepherd-dispatch":
                                # Health-aware dispatch: picks healthy device or NATS-escalates
                                from multifleet.superset_shepherd import shepherd_dispatch as _shep
                                prompt = body.get("prompt") or body.get("body") or ""
                                if not prompt:
                                    self._json_response(400, {"error": "missing 'prompt' for shepherd-dispatch"})
                                    return
                                result = _shep(
                                    prompt=prompt,
                                    prefer_node=body.get("prefer_node"),
                                    fallback_all=body.get("fallback_all", True),
                                )
                            elif action == "device-health":
                                # Return health snapshot for all fleet nodes
                                from multifleet.superset_shepherd import get_device_health as _gdh
                                result = {"devices": _gdh()}
                            elif action == "tasks":
                                result = _vsb.list_tasks(status=body.get("status"))
                            elif action == "update-task":
                                tid = body.get("task_id") or body.get("taskId") or ""
                                if not tid:
                                    self._json_response(400, {"error": "missing task_id"})
                                    return
                                kw = {k: v for k, v in body.items()
                                      if k not in ("target", "action", "task_id", "taskId")}
                                result = _vsb.update_task(tid, **kw)
                            elif action == "task":
                                result = _vsb.push_task(
                                    title=body.get("title", ""),
                                    description=body.get("description", ""),
                                    priority=body.get("priority", "medium"),
                                )
                            elif action == "prompt-and-wait":
                                # Blocking poll in handler thread (BaseHTTPRequestHandler runs
                                # each request in its own thread — safe to block here).
                                prompt = body.get("prompt") or body.get("body") or ""
                                if not prompt:
                                    self._json_response(400, {"error": "missing 'prompt' for target=superset action=prompt-and-wait"})
                                    return
                                from multifleet.superset_poller import push_and_wait as _paw
                                timeout_s = float(body.get("timeout_s", 120))
                                poll_interval_s = float(body.get("poll_interval_s", 5))
                                result = _paw(
                                    prompt,
                                    timeout_s=timeout_s,
                                    poll_interval_s=poll_interval_s,
                                    workspace_id=body.get("workspace_id"),
                                    agent=body.get("agent", "claude"),
                                    device_id=body.get("device_id"),
                                    peer=body.get("peer"),
                                )
                            else:
                                prompt = body.get("prompt") or body.get("body") or ""
                                if not prompt:
                                    self._json_response(400, {"error": "missing 'prompt' for target=superset"})
                                    return
                                result = _vsb.push_prompt(prompt)
                            daemon._stats["message_superset_routed_total"] += 1
                        except Exception as _se:
                            daemon._stats["message_superset_errors"] += 1
                            logger.warning(f"target=superset action={action} failed: {_se!r}")
                            result = {"ok": False, "error": f"{type(_se).__name__}: {_se!r}"}
                        self._json_response(200, result)
                        return
                    # ── Schema validation (OSS blocker #4) ──
                    # Auto-fill 'from' if missing (local HTTP clients may omit it)
                    if "from" not in body:
                        body["from"] = daemon.node_id
                    validation_err = validate_inbound_message(body)
                    if validation_err:
                        logger.warning(f"POST /message rejected: {validation_err}")
                        daemon._stats["validation_rejected"] += 1
                        self._json_response(400, {"error": validation_err, "valid": False})
                        return
                    try:
                        to = body.get("to", "all")
                        use_cloud = body.pop("use_cloud", False)
                        use_discord_p0 = body.pop("use_discord_p0", False)
                        # Use 9-priority fallback protocol (P0 cloud + P0 discord both opt-in)
                        future = asyncio.run_coroutine_threadsafe(
                            daemon.send_with_fallback(to, body, use_cloud=use_cloud,
                                                      use_discord_p0=use_discord_p0), main_loop)
                        result = future.result(timeout=20)  # Longer for fallback chain
                        data = json.dumps(result).encode()
                    except Exception as e:
                        logger.warning(f"POST /message error [{type(e).__name__}]: {e!r}")
                        data = json.dumps({"delivered": False, "error": f"{type(e).__name__}: {e!r}"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/inject":
                    # Direct --resume injection for testing
                    # Accepts: {"session_id": "...", "message": "..."} or {"message": "..."}
                    # If session_id omitted, auto-detects active session
                    session_id = body.get("session_id") or daemon._get_active_session_full_id()
                    message = body.get("message", "")
                    if not message:
                        self._json_response(400, {"error": "missing 'message' field"})
                        return
                    if not session_id:
                        self._json_response(404, {
                            "delivered": False,
                            "method": "none",
                            "error": "no active session found",
                        })
                        return
                    ok = daemon._inject_via_resume(session_id, message)
                    if ok:
                        self._json_response(200, {
                            "delivered": True,
                            "method": "resume",
                            "session": session_id[:12],
                        })
                    else:
                        # Fallback to seed file
                        seed_path = os.path.join(SEED_DIR, f"fleet-seed-{daemon.node_id}.md")
                        with open(seed_path, "a") as f:
                            f.write(f"\n## [INJECT] Direct injection\n\n{message}\n\n---\n")
                        self._json_response(200, {
                            "delivered": True,
                            "method": "seed_fallback",
                            "session": session_id[:12],
                            "seed_path": seed_path,
                        })
                elif self.path == "/send-smart":
                    try:
                        to = body.get("to", body.get("target", "all"))
                        future = asyncio.run_coroutine_threadsafe(
                            daemon.send_smart(to, body), main_loop)
                        result = future.result(timeout=35)
                        data = json.dumps(result).encode()
                    except Exception as e:
                        logger.warning(f"POST /send-smart error: {e}")
                        data = json.dumps({"delivered": False, "error": str(e)}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/chain":
                    steps = body.get("steps", [])
                    chain_id = body.get("chain_id") or str(uuid.uuid4())
                    if not steps:
                        self._json_response(400, {"error": "missing 'steps' array"})
                        return
                    daemon._handle_chain({"chain_id": chain_id, "steps": steps})
                    self._json_response(200, {
                        "chain_id": chain_id,
                        "status": "running",
                        "steps": len(steps),
                    })
                elif self.path == "/dispatch":
                    target = body.get("target", "")
                    prompt = body.get("prompt", "")
                    timeout_s = int(body.get("timeout_s", 300))
                    priority = body.get("priority", "normal")
                    if not target or not prompt:
                        self._json_response(400, {"error": "missing target or prompt"})
                        return
                    dispatch_id = str(uuid.uuid4())[:12]
                    daemon._dispatches[dispatch_id] = {
                        "target": target, "prompt": prompt[:200], "status": "sent",
                        "sent_at": time.time(), "result": None, "priority": priority,
                    }
                    task_msg = {
                        "type": "task", "from": daemon.node_id, "to": target,
                        "payload": {
                            "subject": f"Dispatch {dispatch_id[:8]}",
                            "body": prompt,
                            "dispatch_id": dispatch_id,
                            "timeout_s": timeout_s,
                        },
                    }
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            daemon.send_with_fallback(target, task_msg), main_loop)
                        result = future.result(timeout=20)
                        if result.get("delivered"):
                            self._json_response(200, {"dispatch_id": dispatch_id, "status": "sent", "delivered": True})
                        else:
                            daemon._dispatches[dispatch_id]["status"] = "send_failed"
                            self._json_response(502, {"dispatch_id": dispatch_id, "status": "send_failed", "error": result.get("error", "delivery failed")})
                    except Exception as e:
                        daemon._dispatches[dispatch_id]["status"] = "send_failed"
                        self._json_response(500, {"dispatch_id": dispatch_id, "error": str(e)})
                elif self.path == "/rebuttal/propose":
                    if not daemon._rebuttal:
                        self._json_response(503, {"error": "rebuttal protocol not initialized"})
                        return
                    try:
                        proposal = daemon._rebuttal.propose(
                            task_id=body.get("task_id", str(uuid.uuid4())),
                            decision=body.get("decision", "merge"),
                            evidence_refs=body.get("evidence_refs", []),
                            reasoning=body.get("reasoning", ""),
                            confidence=float(body.get("confidence", 0.8)),
                            rebuttal_window_s=body.get("rebuttal_window_s"),
                        )
                        self._json_response(200, {"status": "proposed", "proposal": proposal.to_dict()})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/rebuttal/rebut":
                    if not daemon._rebuttal:
                        self._json_response(503, {"error": "rebuttal protocol not initialized"})
                        return
                    task_id = body.get("task_id", "")
                    if not task_id:
                        self._json_response(400, {"error": "missing task_id"})
                        return
                    try:
                        rebuttal = daemon._rebuttal.rebut(
                            task_id=task_id,
                            counter_evidence=body.get("counter_evidence", []),
                            reasoning=body.get("reasoning", ""),
                            confidence=float(body.get("confidence", 0.7)),
                            metadata=body.get("metadata"),
                        )
                        if rebuttal:
                            self._json_response(200, {"status": "rebutted", "rebuttal": rebuttal.to_dict()})
                        else:
                            self._json_response(409, {"error": "rebuttal rejected (window closed or proposal not found)"})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/rebuttal/resolve":
                    if not daemon._rebuttal:
                        self._json_response(503, {"error": "rebuttal protocol not initialized"})
                        return
                    task_id = body.get("task_id", "")
                    if not task_id:
                        self._json_response(400, {"error": "missing task_id"})
                        return
                    try:
                        resolution = daemon._rebuttal.resolve(
                            task_id=task_id,
                            final_decision=body.get("final_decision"),
                            final_reasoning=body.get("final_reasoning"),
                        )
                        if resolution:
                            self._json_response(200, {"status": "resolved", "resolution": resolution})
                        else:
                            self._json_response(404, {"error": f"no proposal for task {task_id}"})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                else:
                    self.send_response(404)
                    self.end_headers()

        # Fix A: SO_REUSEADDR + exponential backoff to survive launchd KeepAlive restart races.
        # CLOSE_WAIT on 8855 can hold the port for several seconds after a crash; prior linear
        # 2s x 5 retry often exhausted before the kernel released the socket, silently degrading
        # the daemon to NATS-only (no HTTP health endpoint). We now allow address reuse, back off
        # exponentially (1,2,4,8,16,32,64,128s = ~4min total), and hard-exit on failure so
        # launchd respawns us clean instead of wedging in a zombie state.
        import socketserver as _socketserver
        _socketserver.TCPServer.allow_reuse_address = True
        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer.request_queue_size = 64  # JJJ1-b: default 5 saturates under concurrent health probes
        server = None
        max_retries = 8
        for attempt in range(max_retries):
            try:
                server = ThreadingHTTPServer(("0.0.0.0", FLEET_DAEMON_PORT), Handler)
                break
            except OSError as e:
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt  # 1, 2, 4, 8, 16, 32, 64 seconds
                    logger.warning(
                        f"HTTP port {FLEET_DAEMON_PORT} busy ({e}), retry {attempt+1}/{max_retries} in {backoff}s..."
                    )
                    import time as _t; _t.sleep(backoff)
                else:
                    logger.critical(
                        f"HTTP port {FLEET_DAEMON_PORT} bind FAILED after {max_retries} attempts ({e}) -- "
                        "exiting so launchd respawns clean"
                    )
                    self._stats["last_restart_reason"] = f"http_bind_failed:{e}"
                    sys.exit(1)
        Thread(target=server.serve_forever, daemon=True).start()
        logger.info(f"HTTP compat server on :{FLEET_DAEMON_PORT} (health + broadcast + message)")

        # Pre-attach SSE multiplex to IDEEventBus so the first client connection
        # is fast and stats are populated. Best-effort -- failure is benign.
        try:
            from multifleet import sse_multiplex
            attached = sse_multiplex.attach()
            logger.info(
                "SSE multiplex /events/stream %s (cap=%d, dev_publish=%s)",
                "attached" if attached else "deferred (bus not yet available)",
                sse_multiplex.MAX_SUBSCRIBERS,
                sse_multiplex.DEV_PUBLISH_ENABLED,
            )
        except Exception as _exc:
            logger.warning(f"SSE multiplex pre-attach failed: {_exc!r}")

    def _refresh_peer_ips(self, reason: str = "startup") -> dict:
        """Re-read .multifleet/config.json and refresh in-memory peer IP map.

        NOTE: synchronous -- config file I/O only (load_peers reads JSON from
        disk and updates self._peers_config / self._chief). No NATS publishes,
        no awaitables. Safe to call from async context without ``await``; all
        callers in ``serve()`` and ``_watchdog_check_peers()`` invoke it as a
        regular function. If this ever grows to emit NATS messages or perform
        network I/O, convert to ``async def`` and audit every call site.

        Solves the "NATS islands" persistence problem:
        - DHCP drift can change a peer's LAN IP under us; heartbeats keep flowing
          to the stale IP and the peer appears "never seen".
        - When a peer goes stale >5min, the watchdog calls this so the next
          repair attempt uses the freshest config-declared address.
        - Called once at startup so late edits to config.json take effect without
          restarting the daemon.

        Returns a dict {peer_id: {old_ip, new_ip, changed}} for observability.
        Logs at INFO when anything changed, DEBUG otherwise. Never raises.
        """
        changes: dict = {}
        try:
            from tools.fleet_nerve_config import load_peers
            new_peers, new_chief = load_peers()
        except Exception as e:
            logger.warning(f"_refresh_peer_ips: load_peers failed ({reason}): {e}")
            return changes

        old_peers = dict(self._peers_config or {})
        for peer_id, new_meta in new_peers.items():
            old_meta = old_peers.get(peer_id, {}) or {}
            old_ip = old_meta.get("ip", "")
            new_ip = new_meta.get("ip", "")
            old_ts = old_meta.get("tailscale_ip", "")
            new_ts = new_meta.get("tailscale_ip", "")
            if old_ip != new_ip or old_ts != new_ts:
                changes[peer_id] = {
                    "old_ip": old_ip,
                    "new_ip": new_ip,
                    "old_tailscale_ip": old_ts,
                    "new_tailscale_ip": new_ts,
                    "changed": True,
                }

        # Commit the refreshed map even if nothing changed -- cheap, keeps
        # chief reference in sync when the config file is rewritten in place.
        self._peers_config = new_peers
        if new_chief:
            self._chief = new_chief

        if changes:
            logger.info(
                f"_refresh_peer_ips({reason}): {len(changes)} peer(s) changed: "
                + ", ".join(
                    f"{pid}: {c['old_ip']}->{c['new_ip']}" for pid, c in changes.items()
                )
            )
            # GGG3 (mac2 M3 ask) — peer config change is the natural trigger
            # for plist drift re-check. Canonical plist routes are derived
            # from peer config; if peers moved IPs the launchd plist may now
            # disagree with config. Synchronous + ZSF; runs the check inline
            # since _refresh_peer_ips is sync and called from async callers
            # via plain function call.
            try:
                self._check_plist_drift(reason=f"peer_refresh:{reason}")
            except Exception as e:  # noqa: BLE001 — ZSF: never let trigger crash refresh
                logger.warning(f"_check_plist_drift trigger failed: {e}")
                self._stats["plist_drift_trigger_errors_total"] = (
                    self._stats.get("plist_drift_trigger_errors_total", 0) + 1
                )
        else:
            logger.debug(f"_refresh_peer_ips({reason}): no changes")

        return changes

    # ── GGG3 — M3 plist drift sentinel (restored per mac2 audit) ──
    def _check_plist_drift(self, reason: str = "manual") -> dict:
        """Compare launchd plist routes vs canonical config; publish NATS
        event + log on drift transitions.

        Trigger-based per Aaron's no-polling directive:
        - PRIMARY: called from ``_refresh_peer_ips`` when peer config changes
        - SANITY FALLBACK: ``_plist_drift_sanity_loop`` runs every 5 min as
          watchdog-of-last-resort (catches drift introduced by manual
          launchd edits or stale plist-on-disk that pre-dates config)

        Publishes:
            ``fleet.plist.drift.detected.<node>`` when OK→drift transition
            ``fleet.plist.drift.cleared.<node>`` when drift→OK transition

        ZSF: every error path increments a counter; never raises through.

        Returns the detect_plist_drift dict (shape from plist_drift.py
        docstring) for caller observability.
        """
        # Counter bookkeeping
        self._stats["plist_drift_checks_total"] = (
            self._stats.get("plist_drift_checks_total", 0) + 1
        )
        try:
            from multifleet.plist_drift import detect_plist_drift
        except Exception as e:  # noqa: BLE001 — ZSF
            self._stats["plist_drift_import_errors_total"] = (
                self._stats.get("plist_drift_import_errors_total", 0) + 1
            )
            logger.warning(f"plist_drift import failed: {e}")
            return {"drift": False, "reason": "import_error", "error": str(e)}

        # Build the wrapping {"nodes": ...} dict per the test fixture shape
        # (mac2 VV3-B call-shape fix). Include self under self.node_id.
        try:
            self_cfg = getattr(self, "config_self", None) or {}
            wrapped_config = {
                "nodes": {
                    self.node_id: self_cfg,
                    **(self._peers_config or {}),
                }
            }
        except Exception as e:  # noqa: BLE001
            self._stats["plist_drift_config_errors_total"] = (
                self._stats.get("plist_drift_config_errors_total", 0) + 1
            )
            logger.warning(f"plist_drift config-build failed: {e}")
            return {"drift": False, "reason": "config_error", "error": str(e)}

        try:
            result = detect_plist_drift(self.node_id, wrapped_config)
        except Exception as e:  # noqa: BLE001 — detect_plist_drift claims
            # ZSF but defend in depth at the call site.
            self._stats["plist_drift_detect_errors_total"] = (
                self._stats.get("plist_drift_detect_errors_total", 0) + 1
            )
            logger.warning(f"detect_plist_drift raised unexpectedly: {e}")
            return {"drift": False, "reason": "detect_error", "error": str(e)}

        prev_drift = bool(self._stats.get("plist_drift_currently_drifted", 0))
        now_drift = bool(result.get("drift"))
        reason_tag = str(result.get("reason", "?"))

        # Reason-specific counters for diagnostics
        if reason_tag == "unreadable":
            self._stats["plist_drift_unreadable_total"] = (
                self._stats.get("plist_drift_unreadable_total", 0) + 1
            )
        elif reason_tag == "parse_error":
            self._stats["plist_drift_parse_errors_total"] = (
                self._stats.get("plist_drift_parse_errors_total", 0) + 1
            )
        elif reason_tag == "no_routes_element":
            self._stats["plist_drift_no_routes_total"] = (
                self._stats.get("plist_drift_no_routes_total", 0) + 1
            )

        # Edge transitions trigger NATS events — ZSF: publish errors counted,
        # never raise.
        if now_drift and not prev_drift:
            self._stats["plist_drift_detected_total"] = (
                self._stats.get("plist_drift_detected_total", 0) + 1
            )
            self._stats["plist_drift_currently_drifted"] = 1
            logger.warning(
                f"PLIST DRIFT DETECTED on {self.node_id} (trigger={reason}): "
                f"reason={reason_tag} canonical={result.get('canonical','')!r}"
            )
            self._publish_plist_drift_event("detected", result, reason)
        elif (not now_drift) and prev_drift and reason_tag == "clean":
            self._stats["plist_drift_cleared_total"] = (
                self._stats.get("plist_drift_cleared_total", 0) + 1
            )
            self._stats["plist_drift_currently_drifted"] = 0
            logger.info(
                f"plist drift CLEARED on {self.node_id} (trigger={reason})"
            )
            self._publish_plist_drift_event("cleared", result, reason)

        return result

    def _publish_plist_drift_event(self, kind: str, result: dict, trigger: str) -> None:
        """Publish ``fleet.plist.drift.<kind>.<node>`` on NATS. ZSF — counter on err."""
        subject = f"fleet.plist.drift.{kind}.{self.node_id}"
        payload = {
            "node": self.node_id,
            "kind": kind,
            "trigger": trigger,
            "reason": result.get("reason"),
            "canonical": result.get("canonical", ""),
            "actual": result.get("actual"),
            "plist_path": result.get("plist_path"),
            "ts": time.time(),
        }
        try:
            if self.nc is None or self.nc.is_closed:
                self._stats["plist_drift_publish_no_nats_total"] = (
                    self._stats.get("plist_drift_publish_no_nats_total", 0) + 1
                )
                return
            blob = json.dumps(payload).encode("utf-8")
            # Use the daemon's existing publish helper if available; else fall
            # back to nc.publish directly. This module IS the dispatcher; it
            # is allowlisted under MFINV-C01.
            loop = getattr(self, "_loop", None)
            coro = self.nc.publish(subject, blob)
            if loop is not None and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(coro, loop)
            else:
                # Best-effort fire-and-forget if no loop handle
                asyncio.ensure_future(coro)
        except Exception as e:  # noqa: BLE001 — ZSF
            self._stats["plist_drift_publish_errors_total"] = (
                self._stats.get("plist_drift_publish_errors_total", 0) + 1
            )
            logger.warning(f"plist_drift publish failed: {e}")

    async def _plist_drift_sanity_loop(self):
        """Sanity-fallback 5-min loop. Trigger-based design's safety net per
        Aaron 'we want trigger-based not polling' — this is the last-resort
        catch for drift introduced by manual launchd edits that bypassed
        the peer-config-change trigger path.
        """
        interval_s = int(os.environ.get("PLIST_DRIFT_SANITY_INTERVAL_S", "300"))
        if interval_s <= 0:
            logger.info("plist_drift sanity loop disabled (interval<=0)")
            return
        logger.info(f"plist_drift sanity loop armed @ {interval_s}s interval")
        while True:
            try:
                await asyncio.sleep(interval_s)
                self._check_plist_drift(reason="sanity_loop")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — ZSF
                self._stats["plist_drift_loop_errors_total"] = (
                    self._stats.get("plist_drift_loop_errors_total", 0) + 1
                )
                logger.warning(f"plist_drift sanity loop error: {e}")


# ── CLI ──

async def cli_send(args):
    daemon = FleetNerveNATS()
    await daemon.connect()
    await daemon.send(args[0], "context", "CLI message", " ".join(args[1:]))
    await daemon.nc.close()

async def cli_broadcast(args):
    daemon = FleetNerveNATS()
    await daemon.connect()
    await daemon.send("all", "context", "Broadcast", " ".join(args))
    await daemon.nc.close()

async def cli_health(args):
    daemon = FleetNerveNATS()
    await daemon.connect()
    results = await daemon.broadcast_health()
    online = sum(1 for r in results if r["status"] == 200)
    print(f"\nFleet Health: {online}/{len(results)} online (via NATS)\n")
    for r in results:
        n = r["node"]
        if r["status"] == 200:
            d = r["data"]
            print(f"  OK {n}: sessions={d.get('activeSessions', '?')} uptime={d.get('uptime_s', '?')}s")
            print(f"    git: {d.get('git', {}).get('commit', '?')[:50]}")
        else:
            print(f"  FAIL {n}: down")
    print()
    await daemon.nc.close()

async def cli_monitor(args):
    """Live fleet monitor -- prints events as they arrive."""
    nc = await nats.connect(NATS_URL, name=f"{NODE_ID}-monitor")
    print(f"Fleet Monitor -- connected to {NATS_URL}")
    print(f"Watching fleet.> ...\n")

    async def on_msg(msg):
        try:
            data = json.loads(msg.data.decode())
            ts = datetime.now().strftime("%H:%M:%S")
            node = data.get("from", data.get("node", "?"))
            if "heartbeat" in msg.subject:
                sessions = data.get("sessions", 0)
                print(f"  {ts} ♥ {node} (sessions={sessions})")
            else:
                payload = data.get("payload", {})
                subj = payload.get("subject", msg.subject)
                print(f"  {ts} ◉ [{msg.subject}] {node}: {subj}")
        except Exception:
            print(f"  {msg.subject}: {msg.data[:100]}")

    await nc.subscribe("fleet.>", cb=on_msg)
    try:
        while True:  # noqa: no-poll — CLI keepalive (block until Ctrl-C)
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        await nc.close()


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "serve":
        daemon = FleetNerveNATS()
        asyncio.run(daemon.serve())
    elif sys.argv[1] == "send" and len(sys.argv) >= 4:
        asyncio.run(cli_send(sys.argv[2:]))
    elif sys.argv[1] == "broadcast" and len(sys.argv) >= 3:
        asyncio.run(cli_broadcast(sys.argv[2:]))
    elif sys.argv[1] == "health":
        asyncio.run(cli_health(sys.argv[2:]))
    elif sys.argv[1] == "monitor":
        asyncio.run(cli_monitor(sys.argv[2:]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
