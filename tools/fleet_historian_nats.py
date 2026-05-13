#!/usr/bin/env python3
"""Fleet Historian NATS -- session pub/sub for fleet daemon.

Standalone module that publishes session state (gold, active, idle)
over NATS and maintains a peer session cache. Designed to be called
from the fleet daemon heartbeat loop.

All functions accept the daemon instance as first arg instead of
using self -- this keeps fleet_nerve_nats.py clean and avoids
monkey-patching the class.

Python 3.14 compatible: no unicode arrows, no apostrophes in
docstrings, no digit-letter combos.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("fleet.historian_nats")

# ---- NATS subject helpers ----

IDLE_THRESHOLD_S = 300  # five minutes -- session considered idle


def _sub_session_gold(node: str) -> str:
    """NATS subject for session gold summary."""
    return f"fleet.sessions.{node}.gold"


def _sub_session_active(node: str) -> str:
    """NATS subject for session active events."""
    return f"fleet.sessions.{node}.active"


def _sub_session_idle(node: str) -> str:
    """NATS subject for session idle notifications."""
    return f"fleet.sessions.{node}.idle"


def _sub_sessions_wildcard() -> str:
    """NATS wildcard subject for all session updates."""
    return "fleet.sessions.>"


# ---- State holders (module-level, one per daemon process) ----

_peer_sessions: dict[str, dict] = {}
_last_session_snapshot: list = []
_last_gold_hash: str = ""
_last_idle_publish: dict[str, float] = {}


# ---- Core functions ----


def get_session_gold_summary(daemon: Any) -> dict:
    """Fetch gold summary from session historian DB.

    Returns safe payload with no secrets. Queries the SQLite archive
    for topics and summaries of currently active sessions.
    """
    try:
        db_path = os.path.expanduser("~/.context-dna/session_archive.db")
        if not os.path.exists(db_path):
            return {}

        conn = sqlite3.connect(db_path, timeout=2)
        conn.row_factory = sqlite3.Row
        # Get active sessions gold
        sessions = daemon._detect_sessions()
        topics: list[str] = []
        summaries: list[str] = []
        for s in sessions:
            sid = s.get("sessionId", "")
            if not sid:
                continue
            # Try full ID first, then truncated match
            row = conn.execute(
                "SELECT key_topics, llm_summary FROM archived_sessions "
                "WHERE session_id LIKE ? AND gold_text IS NOT NULL "
                "ORDER BY extracted_at DESC LIMIT 1",
                (f"{sid}%",),
            ).fetchone()
            if row:
                if row["key_topics"]:
                    topics.extend(
                        [t.strip() for t in row["key_topics"].split(",") if t.strip()]
                    )
                if row["llm_summary"]:
                    summaries.append(row["llm_summary"][:200])
        conn.close()

        # Deduplicate topics
        seen: set[str] = set()
        unique_topics: list[str] = []
        for t in topics:
            tl = t.lower()
            if tl not in seen:
                seen.add(tl)
                unique_topics.append(t)

        return {
            "topics": unique_topics[:15],  # cap at fifteen topics
            "gold_summary": "; ".join(summaries)[:500],  # cap total summary
        }
    except Exception as e:
        logger.debug(f"Session gold fetch failed: {e}")
        return {}


async def publish_session_gold(daemon: Any) -> None:
    """Publish gold summary on fleet.sessions.<node>.gold.

    Only publishes when content has actually changed (hash comparison).
    """
    global _last_gold_hash

    gold = get_session_gold_summary(daemon)
    sessions = daemon._detect_sessions()

    # Find primary session (lowest idle seconds)
    primary = ""
    if sessions:
        primary_s = min(sessions, key=lambda s: s.get("idle_s", 9999))
        primary = primary_s.get("sessionId", "")[:12]

    payload = {
        "node": daemon.node_id,
        "timestamp": time.time(),
        "active_count": len(sessions),
        "primary_session": primary,
        "topics": gold.get("topics", []),
        "gold_summary": gold.get("gold_summary", ""),
    }

    # Only publish if content changed (avoid noise)
    content_hash = hashlib.md5(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:12]
    if content_hash == _last_gold_hash:
        return
    _last_gold_hash = content_hash

    try:
        await daemon.nc.publish(
            _sub_session_gold(daemon.node_id),
            json.dumps(payload).encode(),
        )
        logger.debug(
            f"Published session gold: {len(sessions)} sessions, "
            f"{len(gold.get('topics', []))} topics"
        )
    except Exception as e:
        logger.debug(f"Session gold publish failed: {e}")


async def publish_session_active(daemon: Any) -> None:
    """Publish on fleet.sessions.<node>.active when sessions change.

    Detects session start and stop events by comparing snapshots.
    Only publishes when the set of active sessions differs from last check.
    """
    global _last_session_snapshot

    sessions = daemon._detect_sessions()
    # Build a comparable snapshot (session IDs plus kinds)
    snapshot = sorted(
        [(s.get("sessionId", ""), s.get("kind", "")) for s in sessions]
    )

    if snapshot == _last_session_snapshot:
        return  # no change

    _last_session_snapshot = snapshot
    # Sanitize: only include safe fields (no cwds with paths, no PIDs)
    safe_sessions = [
        {
            "sessionId": s.get("sessionId", "")[:12],
            "kind": s.get("kind", "?"),
            "idle_s": s.get("idle_s", 0),
            "project": s.get("project", ""),
        }
        for s in sessions
    ]

    payload = {
        "node": daemon.node_id,
        "timestamp": time.time(),
        "active_count": len(sessions),
        "sessions": safe_sessions,
        "event": "session_change",
    }

    try:
        await daemon.nc.publish(
            _sub_session_active(daemon.node_id),
            json.dumps(payload).encode(),
        )
        logger.info(f"Published session change: {len(sessions)} active sessions")
    except Exception as e:
        logger.debug(f"Session active publish failed: {e}")


async def publish_session_idle(daemon: Any) -> None:
    """Publish on fleet.sessions.<node>.idle for sessions idle over threshold.

    Rate-limited per session: will not re-publish idle for the same
    session within sixty seconds.
    """
    global _last_idle_publish

    sessions = daemon._detect_sessions()
    idle_sessions = [
        {
            "sessionId": s.get("sessionId", "")[:12],
            "idle_s": s.get("idle_s", 0),
            "kind": s.get("kind", "?"),
        }
        for s in sessions
        if s.get("idle_s", 0) > IDLE_THRESHOLD_S
    ]

    if not idle_sessions:
        return

    # Rate limit: do not re-publish idle for same session within sixty seconds
    now = time.time()
    new_idle = []
    for s in idle_sessions:
        sid = s["sessionId"]
        last = _last_idle_publish.get(sid, 0)
        if now - last > 60:
            new_idle.append(s)
            _last_idle_publish[sid] = now

    if not new_idle:
        return

    # Clean up old idle tracking entries (older than ten minutes)
    _last_idle_publish.update(
        {sid: ts for sid, ts in _last_idle_publish.items() if now - ts < 600}
    )

    payload = {
        "node": daemon.node_id,
        "timestamp": now,
        "idle_sessions": new_idle,
    }

    try:
        await daemon.nc.publish(
            _sub_session_idle(daemon.node_id),
            json.dumps(payload).encode(),
        )
        logger.debug(
            f"Published idle: {len(new_idle)} sessions idle over {IDLE_THRESHOLD_S} sec"
        )
    except Exception as e:
        logger.debug(f"Session idle publish failed: {e}")


async def handle_session_update(msg: Any) -> None:
    """Handle fleet.sessions.<node>.<type> messages.

    Maintains the peer session cache. Listens on fleet.sessions.>
    wildcard. Updates _peer_sessions with session info from other
    nodes. Never stores full content or secrets.

    Args:
        msg: NATS message with subject fleet.sessions.<node>.<type>
    """
    global _peer_sessions

    try:
        data = json.loads(msg.data.decode())
        node = data.get("node", "unknown")

        subject_parts = msg.subject.split(".")
        # fleet.sessions.<node>.<type>
        msg_type = subject_parts[-1] if len(subject_parts) >= 4 else "unknown"

        now = time.time()
        if node not in _peer_sessions:
            _peer_sessions[node] = {
                "sessions": [],
                "gold_summary": "",
                "last_update": now,
                "idle_sessions": [],
            }

        peer = _peer_sessions[node]
        peer["last_update"] = now

        if msg_type == "gold":
            peer["gold_summary"] = data.get("gold_summary", "")
            peer["topics"] = data.get("topics", [])
            peer["active_count"] = data.get("active_count", 0)
            peer["primary_session"] = data.get("primary_session", "")

        elif msg_type == "active":
            peer["sessions"] = data.get("sessions", [])
            peer["active_count"] = data.get("active_count", 0)

        elif msg_type == "idle":
            peer["idle_sessions"] = data.get("idle_sessions", [])

    except Exception as e:
        logger.debug(f"Session update handler error: {e}")


def get_peer_sessions(daemon: Any) -> dict:
    """Return all peer session summaries for the /fleet/sessions endpoint.

    Returns dict with per-node session data from _peer_sessions cache,
    plus this node own session data.
    """
    result = {}
    now = time.time()

    # Add self
    sessions = daemon._detect_sessions()
    gold = get_session_gold_summary(daemon)
    result[daemon.node_id] = {
        "sessions": [
            {
                "sessionId": s.get("sessionId", "")[:12],
                "kind": s.get("kind", "?"),
                "idle_s": s.get("idle_s", 0),
            }
            for s in sessions
        ],
        "active_count": len(sessions),
        "gold_summary": gold.get("gold_summary", ""),
        "topics": gold.get("topics", []),
        "last_update": now,
        "is_self": True,
    }

    # Add peers from cache
    for node_id, peer_data in _peer_sessions.items():
        staleness = now - peer_data.get("last_update", 0)
        result[node_id] = {
            "sessions": peer_data.get("sessions", []),
            "active_count": peer_data.get("active_count", 0),
            "gold_summary": peer_data.get("gold_summary", ""),
            "topics": peer_data.get("topics", []),
            "idle_sessions": peer_data.get("idle_sessions", []),
            "primary_session": peer_data.get("primary_session", ""),
            "last_update": peer_data.get("last_update", 0),
            "staleness_s": int(staleness),
            "is_self": False,
        }

    return result


def peer_has_idle_session(target: str) -> bool:
    """Check if a peer has any idle session via NATS session cache.

    Used by send_smart to prefer seed file delivery for idle peers.
    Returns False if peer data is stale (older than two minutes).
    """
    peer = _peer_sessions.get(target)
    if not peer:
        return False
    # Check staleness -- data older than two minutes is unreliable
    if time.time() - peer.get("last_update", 0) > 120:
        return False
    idle = peer.get("idle_sessions", [])
    if idle:
        return True
    # Also check sessions list for idle_s over threshold
    for s in peer.get("sessions", []):
        if s.get("idle_s", 0) > IDLE_THRESHOLD_S:
            return True
    return False


# ---- Integration helpers ----


async def publish_all_session_data(daemon: Any) -> None:
    """Publish all session data types. Call from heartbeat loop.

    Publishes active (only on change), idle (rate-limited per session),
    and gold (only on content change). Each call is individually
    protected -- one failure does not block the others.
    """
    try:
        await publish_session_active(daemon)
    except Exception as e:
        logger.debug(f"Session active publish error: {e}")

    try:
        await publish_session_idle(daemon)
    except Exception as e:
        logger.debug(f"Session idle publish error: {e}")

    try:
        await publish_session_gold(daemon)
    except Exception as e:
        logger.debug(f"Session gold publish error: {e}")


def subscribe_sessions(daemon: Any):
    """Return (subject, callback) tuple for NATS subscription.

    Usage in daemon subscribe_all::

        sub, cb = fleet_historian_nats.subscribe_sessions(daemon)
        await daemon.nc.subscribe(sub, cb=cb)
    """
    # The callback needs to filter out own node messages
    own_node = daemon.node_id

    async def _cb(msg: Any) -> None:
        try:
            data = json.loads(msg.data.decode())
            if data.get("node") == own_node:
                return  # skip own messages
        except Exception as _zsf_e:
            # EEE4/ZSF: malformed historian message — don't silently drop.
            # Log so a corrupted-producer pattern surfaces; we still fall
            # through to handle_session_update so downstream gets a chance
            # to record/normalize as well.
            logger.debug(
                "historian _cb malformed payload: %s: %s",
                type(_zsf_e).__name__, _zsf_e,
            )
        await handle_session_update(msg)

    return _sub_sessions_wildcard(), _cb
