#!/usr/bin/env python3
"""
Context DNA Redis Cache Layer

This module provides Redis-backed caching and pub/sub for the memory system.
Connects to the containerized contextdna-redis service.

PURPOSE (from ChatGPT's analysis):
"Redis isn't 'the brain.' It's the speed layer + coordination layer."

Redis advantages for Context DNA:
1. Hot context cache - keeps re-reading same "top context blocks" instant
2. Queues / background jobs - scanner + distiller via Celery
3. Pub/Sub for live UI + live inject updates
4. Rate limiting + debouncing - prevent scanner thrashing

CONNECTION:
- Python code uses: context-dna-redis (127.0.0.1:6379, NO AUTH)
- Docker contextdna-redis (16379, with password) is for upstream GHCR stack only
- Env vars: CD_REDIS_HOST, CD_REDIS_PORT, CD_REDIS_PASSWORD (avoids .env hijacking)

Keys:
- contextdna:context:pack:{project_id} - Hot context pack cache
- contextdna:project:fingerprint - Project state fingerprint
- contextdna:scanner:last_scan - Timestamp of last scan
- contextdna:llm:status - LLM health status
- contextdna:events - Pub/sub channel for real-time events

Usage:
    from memory.redis_cache import (
        get_redis_client,
        cache_context_pack, get_cached_context_pack,
        publish_event, subscribe_events
    )
"""

import hashlib
import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
import threading
from pathlib import Path

# Load Context DNA credentials if not already set
try:
    from dotenv import load_dotenv
    _env_paths = [
        Path(__file__).parent.parent / "context-dna" / "infra" / ".env",
        Path(__file__).parent.parent / ".env",
    ]
    for _env_path in _env_paths:
        if _env_path.exists():
            load_dotenv(_env_path)
            break
except ImportError:
    pass  # dotenv not available, rely on environment

logger = logging.getLogger('contextdna.redis')

# =============================================================================
# REDIS CONFIGURATION
# =============================================================================

# CD_REDIS_* env vars override REDIS_* to prevent .env hijacking
# (.env has REDIS_PASSWORD for contextdna-redis on 16379 — wrong container)
# Python code connects to context-dna-redis on 6379 (no auth)
REDIS_HOST = os.environ.get('CD_REDIS_HOST', os.environ.get('REDIS_HOST', '127.0.0.1'))
REDIS_PORT = int(os.environ.get('CD_REDIS_PORT', os.environ.get('REDIS_PORT', '6379')))
# CD_REDIS_PASSWORD='' means "no auth" — don't fall through to REDIS_PASSWORD
# which is set in .env for the upstream contextdna-redis on port 16379
_cd_redis_pw = os.environ.get('CD_REDIS_PASSWORD')
REDIS_PASSWORD = _cd_redis_pw if _cd_redis_pw is not None else os.environ.get('REDIS_PASSWORD', '')
# Force no-auth when connecting to 127.0.0.1:6379 (context-dna-redis, no auth)
# BUT NOT inside Docker where contextdna-redis:6379 requires auth
if REDIS_PORT == 6379 and REDIS_HOST == '127.0.0.1' and _cd_redis_pw is None:
    REDIS_PASSWORD = ''
REDIS_DB = int(os.environ.get('CD_REDIS_DB', '0'))

# Redis key prefixes
KEY_PREFIX = "contextdna:"
KEY_CONTEXT_PACK = f"{KEY_PREFIX}context:pack:"
KEY_PROJECT_FINGERPRINT = f"{KEY_PREFIX}project:fingerprint"
KEY_LAST_SCAN = f"{KEY_PREFIX}scanner:last_scan"
KEY_LLM_STATUS = f"{KEY_PREFIX}llm:status"
KEY_SESSION_FAILURES = f"{KEY_PREFIX}session:failures:"
KEY_INJECTION_HISTORY = f"{KEY_PREFIX}injection:history"

# Pub/sub channels
CHANNEL_EVENTS = f"{KEY_PREFIX}events"
CHANNEL_HIERARCHY_UPDATES = f"{KEY_PREFIX}hierarchy"
CHANNEL_SUCCESS_DETECTED = f"{KEY_PREFIX}success"

# Cache TTLs (in seconds)
TTL_CONTEXT_PACK = 300      # 5 minutes
TTL_LLM_STATUS = 600        # 10 minutes
TTL_FINGERPRINT = 60        # 1 minute
TTL_INJECTION_HISTORY = 3600  # 1 hour

# Global client
_redis_client = None


# =============================================================================
# PROJECT SCOPING (Prevents cross-project cache bleed)
# =============================================================================

_cached_project_id: Optional[str] = None


def get_project_id() -> str:
    """Get current project identifier for cache scoping.

    Prevents cache bleed when switching between projects.
    Result is cached for process lifetime (project doesn't change mid-process).

    Priority:
    1. CONTEXT_DNA_PROJECT env var (explicit override)
    2. Git repo root directory name
    3. Current working directory name
    4. 'default'
    """
    global _cached_project_id
    if _cached_project_id is not None:
        return _cached_project_id

    import subprocess

    # 1. Explicit env var
    project = os.environ.get("CONTEXT_DNA_PROJECT")
    if project:
        _cached_project_id = project.strip().lower().replace(" ", "-")
        return _cached_project_id

    # 2. Git repo root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            _cached_project_id = os.path.basename(result.stdout.strip()).lower()
            return _cached_project_id
    except Exception:
        pass

    # 3. CWD basename
    cwd_name = os.path.basename(os.getcwd()).lower()
    _cached_project_id = cwd_name or "default"
    return _cached_project_id


# =============================================================================
# CONNECTION MANAGEMENT
# =============================================================================

def get_redis_client():
    """Get or create Redis client."""
    global _redis_client

    if _redis_client is not None:
        return _redis_client

    try:
        import redis
        _redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD or None,
            db=REDIS_DB,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5
        )
        # Test connection
        _redis_client.ping()
        logger.info(f"Redis connected: {REDIS_HOST}:{REDIS_PORT}")
        return _redis_client
    except ImportError:
        logger.warning("Redis library not available")
        return None
    except Exception as e:
        logger.warning(f"Failed to connect to Redis: {e}")
        return None


def get_async_redis_client():
    """Get async Redis client for FastAPI/async contexts."""
    try:
        import redis.asyncio as aioredis
        return aioredis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD or None,
            db=REDIS_DB,
            decode_responses=True,
            socket_timeout=3,
            socket_connect_timeout=3
        )
    except ImportError:
        logger.warning("Async Redis not available")
        return None


# =============================================================================
# CONTEXT PACK CACHING
# =============================================================================
# "Hot cache of top context blocks - makes injections fast and consistent"

def cache_context_pack(project_id: str, context_pack: Dict, ttl: int = TTL_CONTEXT_PACK) -> bool:
    """
    Cache a context pack for quick retrieval.

    This is the "hot cache" that makes injections fast:
    - Stores the pre-computed context pack
    - Expires after 5 minutes (refreshed by relevance agent)
    """
    client = get_redis_client()
    if not client:
        return False

    try:
        key = f"{KEY_CONTEXT_PACK}{project_id}"
        data = json.dumps({
            "pack": context_pack,
            "cached_at": datetime.utcnow().isoformat()
        })
        client.setex(key, ttl, data)
        logger.debug(f"Cached context pack for {project_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to cache context pack: {e}")
        return False


def get_cached_context_pack(project_id: str) -> Optional[Dict]:
    """
    Get cached context pack if available.

    Returns None if:
    - No cache exists
    - Cache expired
    - Redis unavailable
    """
    client = get_redis_client()
    if not client:
        return None

    try:
        key = f"{KEY_CONTEXT_PACK}{project_id}"
        data = client.get(key)
        if data:
            parsed = json.loads(data)
            logger.debug(f"Cache hit for {project_id}")
            return parsed.get("pack")
        return None
    except Exception as e:
        logger.error(f"Failed to get cached context pack: {e}")
        return None


def invalidate_context_pack(project_id: str) -> bool:
    """Invalidate cached context pack (force refresh)."""
    client = get_redis_client()
    if not client:
        return False

    try:
        key = f"{KEY_CONTEXT_PACK}{project_id}"
        client.delete(key)
        return True
    except Exception as e:
        logger.error(f"Failed to invalidate context pack: {e}")
        return False


# =============================================================================
# SESSION FAILURE TRACKING (Redis-backed for speed)
# =============================================================================
# For Three-Tier Volume Escalation

def record_session_failure_redis(session_id: str, failure_type: str = "task_failed") -> int:
    """
    Record a session failure in Redis.

    Returns the new failure count for this session.
    """
    client = get_redis_client()
    if not client:
        return 0

    try:
        key = f"{KEY_SESSION_FAILURES}{session_id}"

        # Add failure to list
        failure = json.dumps({
            "type": failure_type,
            "time": datetime.utcnow().isoformat()
        })
        pipe = client.pipeline()
        pipe.rpush(key, failure)
        pipe.expire(key, 3600)
        pipe.llen(key)
        results = pipe.execute()
        return results[2]  # llen result
    except Exception as e:
        logger.error(f"Failed to record session failure: {e}")
        return 0


def get_session_failure_count_redis(session_id: str) -> int:
    """Get failure count for a session from Redis."""
    client = get_redis_client()
    if not client:
        return 0

    try:
        key = f"{KEY_SESSION_FAILURES}{session_id}"
        return client.llen(key)
    except Exception as e:
        logger.error(f"Failed to get failure count: {e}")
        return 0


def clear_session_failures_redis(session_id: str) -> bool:
    """Clear failures for a session (after success)."""
    client = get_redis_client()
    if not client:
        return False

    try:
        key = f"{KEY_SESSION_FAILURES}{session_id}"
        client.delete(key)
        return True
    except Exception as e:
        logger.error(f"Failed to clear session failures: {e}")
        return False


# =============================================================================
# PROJECT STATE TRACKING
# =============================================================================

def set_project_fingerprint(fingerprint: str) -> bool:
    """Store the current project fingerprint."""
    client = get_redis_client()
    if not client:
        return False

    try:
        client.setex(KEY_PROJECT_FINGERPRINT, TTL_FINGERPRINT, fingerprint)
        return True
    except Exception as e:
        logger.error(f"Failed to set fingerprint: {e}")
        return False


def get_project_fingerprint() -> Optional[str]:
    """Get the stored project fingerprint."""
    client = get_redis_client()
    if not client:
        return None

    try:
        return client.get(KEY_PROJECT_FINGERPRINT)
    except Exception as e:
        logger.error(f"Failed to get fingerprint: {e}")
        return None


def set_last_scan_time() -> bool:
    """Record when the last scan happened."""
    client = get_redis_client()
    if not client:
        return False

    try:
        client.set(KEY_LAST_SCAN, datetime.utcnow().isoformat())
        return True
    except Exception as e:
        logger.error(f"Failed to set last scan time: {e}")
        return False


def get_last_scan_time() -> Optional[str]:
    """Get the last scan timestamp."""
    client = get_redis_client()
    if not client:
        return None

    try:
        return client.get(KEY_LAST_SCAN)
    except Exception as e:
        logger.error(f"Failed to get last scan time: {e}")
        return None


# =============================================================================
# LLM STATUS CACHING
# =============================================================================

def set_llm_status(status: Dict) -> bool:
    """Cache LLM health status."""
    client = get_redis_client()
    if not client:
        return False

    try:
        client.setex(KEY_LLM_STATUS, TTL_LLM_STATUS, json.dumps(status))
        return True
    except Exception as e:
        logger.error(f"Failed to set LLM status: {e}")
        return False


def get_llm_status() -> Optional[Dict]:
    """Get cached LLM health status."""
    client = get_redis_client()
    if not client:
        return None

    try:
        data = client.get(KEY_LLM_STATUS)
        return json.loads(data) if data else None
    except Exception as e:
        logger.error(f"Failed to get LLM status: {e}")
        return None


# =============================================================================
# INJECTION HISTORY (For dashboard visualization)
# =============================================================================

def record_injection(injection_data: Dict, max_history: int = 100) -> bool:
    """Record an injection for history/debugging."""
    client = get_redis_client()
    if not client:
        return False

    try:
        data = json.dumps({
            **injection_data,
            "recorded_at": datetime.utcnow().isoformat()
        })
        pipe = client.pipeline()
        pipe.lpush(KEY_INJECTION_HISTORY, data)
        pipe.ltrim(KEY_INJECTION_HISTORY, 0, max_history - 1)
        pipe.expire(KEY_INJECTION_HISTORY, TTL_INJECTION_HISTORY)
        pipe.execute()
        return True
    except Exception as e:
        logger.error(f"Failed to record injection: {e}")
        return False


def get_injection_history(limit: int = 20) -> list:
    """Get recent injection history."""
    client = get_redis_client()
    if not client:
        return []

    try:
        data = client.lrange(KEY_INJECTION_HISTORY, 0, limit - 1)
        return [json.loads(d) for d in data]
    except Exception as e:
        logger.error(f"Failed to get injection history: {e}")
        return []


# =============================================================================
# PUB/SUB FOR REAL-TIME UPDATES
# =============================================================================
# "Pub/Sub for live UI + live inject updates"

def publish_event(event_type: str, data: Dict) -> bool:
    """
    Publish an event to the events channel.

    Used for:
    - Scanner detected new hierarchy → UI updates
    - Success detected → Dashboard notification
    - Context pack refreshed → Injector gets new context
    """
    client = get_redis_client()
    if not client:
        return False

    try:
        message = json.dumps({
            "type": event_type,
            "data": data,
            "timestamp": datetime.utcnow().isoformat()
        })
        client.publish(CHANNEL_EVENTS, message)
        logger.debug(f"Published event: {event_type}")
        return True
    except Exception as e:
        logger.error(f"Failed to publish event: {e}")
        return False


def subscribe_events(callback: Callable[[str, Dict], None], channels: list = None):
    """
    Subscribe to events with a callback.

    The callback receives (event_type, data) for each event.

    Usage:
        def handle_event(event_type, data):
            print(f"Got {event_type}: {data}")

        # This blocks - run in a thread
        subscribe_events(handle_event)
    """
    client = get_redis_client()
    if not client:
        logger.warning("Cannot subscribe - Redis unavailable")
        return

    channels = channels or [CHANNEL_EVENTS]

    try:
        pubsub = client.pubsub()
        pubsub.subscribe(*channels)

        logger.info(f"Subscribed to channels: {channels}")

        for message in pubsub.listen():
            if message['type'] == 'message':
                try:
                    data = json.loads(message['data'])
                    callback(data.get('type', 'unknown'), data.get('data', {}))
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON in message: {message['data']}")
                except Exception as e:
                    logger.error(f"Error in event callback: {e}")

    except Exception as e:
        logger.error(f"Subscription error: {e}")


def start_event_listener(callback: Callable[[str, Dict], None]) -> threading.Thread:
    """
    Start event listener in a background thread.

    Returns the thread so you can join() it if needed.
    """
    thread = threading.Thread(
        target=subscribe_events,
        args=(callback,),
        daemon=True
    )
    thread.start()
    return thread


# =============================================================================
# RATE LIMITING / DEBOUNCING
# =============================================================================
# "Prevent the scanner from thrashing when a repo changes rapidly"

def check_rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
    """
    Check if action is allowed under rate limit.

    Returns True if allowed, False if rate limited.
    """
    client = get_redis_client()
    if not client:
        return True  # Allow if Redis unavailable

    try:
        rate_key = f"{KEY_PREFIX}ratelimit:{key}"
        pipe = client.pipeline()
        pipe.incr(rate_key)
        pipe.expire(rate_key, window_seconds)
        results = pipe.execute()
        current = results[0]
        return current <= max_requests
    except Exception as e:
        logger.error(f"Rate limit check error: {e}")
        return True


def debounce_key(key: str, debounce_seconds: int) -> bool:
    """
    Check if key should be debounced.

    Returns True if this is a new action (not debounced).
    Returns False if action should be skipped (debounced).
    """
    client = get_redis_client()
    if not client:
        return True

    try:
        debounce_key = f"{KEY_PREFIX}debounce:{key}"
        result = client.set(debounce_key, "1", ex=debounce_seconds, nx=True)
        return result is not None
    except Exception as e:
        logger.error(f"Debounce check error: {e}")
        return True


# =============================================================================
# DISTRIBUTED LOCKS
# =============================================================================
# "Prevent 2 agents from rewriting the same hierarchy"

def acquire_lock(lock_name: str, timeout: int = 30) -> Optional[str]:
    """
    Acquire a distributed lock.

    Returns a lock token if acquired, None if lock is held by another process.
    """
    client = get_redis_client()
    if not client:
        return "no-redis-lock"  # Fallback - no locking

    try:
        import uuid
        lock_key = f"{KEY_PREFIX}lock:{lock_name}"
        token = str(uuid.uuid4())

        if client.set(lock_key, token, ex=timeout, nx=True):
            return token
        return None
    except Exception as e:
        logger.error(f"Lock acquisition error: {e}")
        return None


def release_lock(lock_name: str, token: str) -> bool:
    """
    Release a distributed lock.

    Only releases if the token matches (prevents releasing someone else's lock).
    """
    client = get_redis_client()
    if not client or token == "no-redis-lock":
        return True

    try:
        lock_key = f"{KEY_PREFIX}lock:{lock_name}"

        # Lua script for atomic check-and-delete
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        result = client.eval(script, 1, lock_key, token)
        return result == 1
    except Exception as e:
        logger.error(f"Lock release error: {e}")
        return False


# =============================================================================
# ARCHITECTURE GRAPH CACHE
# =============================================================================
# Fast, concurrent-safe caching for architecture graph with fallback to JSON

ARCH_GRAPH_KEY = "contextdna:arch_graph:v1"
ARCH_GRAPH_TTL = 300  # 5 minutes

def cache_architecture_graph(graph_data: dict) -> bool:
    """
    Store architecture graph in Redis (atomic, concurrent-safe).

    Returns True if cached successfully, False if Redis unavailable.
    """
    try:
        r = get_redis_client()
        if not r:
            return False
        r.set(ARCH_GRAPH_KEY, json.dumps(graph_data), ex=ARCH_GRAPH_TTL)
        logger.debug("Cached architecture graph to Redis")
        return True
    except Exception as e:
        logger.warning(f"Failed to cache architecture graph to Redis: {e}")
        return False

def get_cached_architecture_graph() -> Optional[dict]:
    """
    Retrieve architecture graph from Redis.

    Returns None if not cached or Redis unavailable.
    """
    try:
        r = get_redis_client()
        if not r:
            return None
        data = r.get(ARCH_GRAPH_KEY)
        if data:
            logger.debug("Retrieved architecture graph from Redis cache")
            return json.loads(data)
        return None
    except Exception as e:
        logger.warning(f"Failed to get cached architecture graph from Redis: {e}")
        return None

def invalidate_architecture_graph() -> bool:
    """Invalidate cached architecture graph (force refresh)."""
    try:
        r = get_redis_client()
        if not r:
            return False
        r.delete(ARCH_GRAPH_KEY)
        logger.debug("Invalidated architecture graph cache")
        return True
    except Exception as e:
        logger.warning(f"Failed to invalidate architecture graph: {e}")
        return False


# =============================================================================
# SECTION CACHING (For webhook sections - stores strings, not dicts)
# =============================================================================

def cache_section_content(section_key: str, content: str, ttl: int = TTL_CONTEXT_PACK) -> bool:
    """
    Cache a webhook section's generated content.

    Args:
        section_key: Full cache key (e.g., "contextdna:s1:abc123:high:v1")
        content: Section content (string)
        ttl: Time-to-live in seconds

    Returns:
        True if cached successfully, False otherwise
    """
    client = get_redis_client()
    if not client:
        return False

    try:
        data = json.dumps({
            "content": content,
            "cached_at": datetime.utcnow().isoformat()
        })
        client.setex(section_key, ttl, data)
        logger.debug(f"Cached section: {section_key} (TTL: {ttl}s)")
        return True
    except Exception as e:
        logger.error(f"Failed to cache section: {e}")
        return False


def get_cached_section_content(section_key: str) -> Optional[str]:
    """
    Get cached webhook section content.

    Args:
        section_key: Full cache key

    Returns:
        Section content (string) if found, None otherwise
    """
    client = get_redis_client()
    if not client:
        return None

    try:
        data = client.get(section_key)
        if data:
            parsed = json.loads(data)
            logger.debug(f"Cache hit: {section_key}")
            return parsed.get("content")
        return None
    except Exception as e:
        logger.error(f"Failed to get cached section: {e}")
        return None


# =============================================================================
# PER-SECTION INJECTION METRICS (Sorted Sets)
# =============================================================================
# Real-time visibility into per-section injection latency.
# Uses Redis sorted sets: score=timestamp, member="{latency_ms}:{injection_id}:{timestamp}"
# Enables P50/P95/P99 percentile queries for identifying slow sections.

METRICS_PREFIX = "contextdna:metrics"


def record_section_latency(section_name: str, latency_ms: float, injection_id: str = ""):
    """Record section latency in Redis sorted set (score = timestamp, member = latency).

    Args:
        section_name: Section identifier (e.g., "s1_foundation", "total_critical")
        latency_ms: Elapsed time in milliseconds
        injection_id: Optional injection correlation ID
    """
    r = get_redis_client()
    if not r:
        return
    try:
        import time
        key = f"{METRICS_PREFIX}:latency:{section_name}"
        now = time.time()
        # Store as sorted set: score=timestamp, member=f"{latency_ms}:{injection_id}:{timestamp}"
        r.zadd(key, {f"{latency_ms:.1f}:{injection_id}:{now:.0f}": now})
        # Trim to last 1000 entries (remove oldest beyond 1000)
        r.zremrangebyrank(key, 0, -1001)
    except Exception:
        pass


def get_section_latency_stats(section_name: str) -> dict:
    """Get P50, P95, P99, avg latency for a section.

    Args:
        section_name: Section identifier

    Returns:
        Dict with count, p50, p95, p99, avg, min, max. Empty dict if unavailable.
    """
    r = get_redis_client()
    if not r:
        return {}
    try:
        key = f"{METRICS_PREFIX}:latency:{section_name}"
        members = r.zrange(key, 0, -1)
        if not members:
            return {}
        # decode_responses=True means members are already strings
        latencies = sorted([float(m.split(':')[0]) for m in members])
        n = len(latencies)
        return {
            "count": n,
            "p50": latencies[n // 2],
            "p95": latencies[int(n * 0.95)],
            "p99": latencies[int(n * 0.99)],
            "avg": round(sum(latencies) / n, 1),
            "min": latencies[0],
            "max": latencies[-1],
        }
    except Exception:
        return {}


def get_all_section_stats() -> dict:
    """Get latency stats for all tracked sections.

    Returns:
        Dict mapping section names to their latency stats.
    """
    r = get_redis_client()
    if not r:
        return {}
    try:
        keys = r.keys(f"{METRICS_PREFIX}:latency:*")
        stats = {}
        for key in keys:
            # decode_responses=True means key is already a string
            section = key.split(":")[-1]
            stats[section] = get_section_latency_stats(section)
        return stats
    except Exception:
        return {}


def record_injection_total(total_ms: float, tier: str = "unknown"):
    """Record total injection latency, bucketed by risk tier.

    Args:
        total_ms: Total injection time in milliseconds
        tier: Risk tier (e.g., "critical", "high", "medium", "low")
    """
    record_section_latency(f"total_{tier}", total_ms)


# =============================================================================
# ANTICIPATION ENGINE CACHE (Predictive webhook pre-computation)
# =============================================================================
# Pre-computed S2/S8 content stored by session_id for instant webhook delivery.
# See memory/anticipation_engine.py for the producer side.

KEY_ANTICIPATION = f"{KEY_PREFIX}anticipation:"
TTL_ANTICIPATION = 600  # 10 minutes — must survive generation time + user think time


def cache_anticipation_section(session_id: str, section: str, content: str,
                                source_prompt: str = "", ttl: int = TTL_ANTICIPATION) -> bool:
    """
    Cache a pre-computed webhook section from the anticipation engine.

    Cache keys are project-scoped to prevent cross-project bleed:
        contextdna:anticipation:{section}:{project}:{session_id}

    Args:
        session_id: Active session ID
        section: Section identifier (e.g., "s2", "s8")
        content: Pre-computed section content
        source_prompt: The prompt that was used to generate this content
        ttl: Time-to-live in seconds (default 5 minutes)

    Returns:
        True if cached successfully
    """
    client = get_redis_client()
    if not client:
        return False

    try:
        project = get_project_id()
        key = f"{KEY_ANTICIPATION}{section}:{project}:{session_id}"
        data = json.dumps({
            "content": content,
            "source_prompt": source_prompt[:200],
            "cached_at": datetime.utcnow().isoformat(),
            "engine": "anticipation_v1",
            "project": project,
        })
        client.setex(key, ttl, data)
        logger.debug(f"Anticipation cached: {section} for {project}/{session_id[:12]}...")
        return True
    except Exception as e:
        logger.error(f"Failed to cache anticipation {section}: {e}")
        return False


def get_anticipation_section(session_id: str, section: str,
                              current_prompt: str = None) -> Optional[str]:
    """
    Retrieve pre-computed section content from the anticipation cache.

    Called by the webhook generation path BEFORE the LLM generation path.
    If this returns content, the webhook can skip the slow LLM call.

    Temporal State Drift protection: when current_prompt is provided,
    validates stored prompt_hash matches. Stale content from a different
    prompt is treated as a cache miss.

    Args:
        session_id: Current session ID
        section: Section identifier (e.g., "s2", "s8")
        current_prompt: If provided, validate cached content was generated for this prompt

    Returns:
        Pre-computed content string, or None if no cache hit
    """
    client = get_redis_client()
    if not client:
        return None

    # Pre-compute prompt hash for validation
    _current_hash = None
    if current_prompt:
        _current_hash = hashlib.md5(current_prompt[:200].encode()).hexdigest()[:12]

    try:
        project = get_project_id()
        # Try project-scoped key first
        key = f"{KEY_ANTICIPATION}{section}:{project}:{session_id}"
        data = client.get(key)

        # Backward compat: try legacy unscoped key
        if not data:
            legacy_key = f"{KEY_ANTICIPATION}{section}:{session_id}"
            data = client.get(legacy_key)
            if data:
                logger.debug(f"Anticipation legacy key hit: {legacy_key}")

        # Stale-but-present fallback: direct key lookup (fast, O(1))
        # Anticipation engine writes a :fallback key with 10min TTL alongside
        # session-scoped keys. Ensures Synaptic stays present during gold mining.
        if not data:
            fallback_key = f"{KEY_ANTICIPATION}{section}:{project}:fallback"
            data = client.get(fallback_key)
            if data:
                logger.info(f"Anticipation FALLBACK hit: {section} for {project} (stale-but-present)")

        # Cross-IDE SCAN fallback: handles session ID mismatch between IDEs
        # (e.g., Cursor uses cursor-auto-* IDs, VS Code uses JSONL stem IDs).
        # SCAN is O(N) but mitigated by MATCH filter and COUNT limit.
        if not data:
            scan_pattern = f"{KEY_ANTICIPATION}{section}:{project}:*"
            try:
                for found_key in client.scan_iter(match=scan_pattern, count=10):
                    key_str = found_key if isinstance(found_key, str) else found_key.decode()
                    # Skip fallback keys (already checked above)
                    if key_str.endswith(":fallback"):
                        continue
                    data = client.get(found_key)
                    if data:
                        logger.info(f"Anticipation SCAN hit: {section} for {project} (cross-IDE: {key_str})")
                        break
            except Exception as scan_err:
                logger.debug(f"Anticipation SCAN failed: {scan_err}")

        if not data:
            return None

        parsed = json.loads(data)
        content = parsed.get("content")

        # Temporal State Drift check: validate prompt hash if available
        if content and _current_hash:
            stored_hash = parsed.get("prompt_hash")
            if stored_hash and stored_hash != _current_hash:
                logger.info(f"Anticipation STALE: prompt changed for {section} "
                            f"(stored={stored_hash}, current={_current_hash})")
                return None

        if content:
            logger.info(f"Anticipation HIT: {section} for {project}/{session_id[:12]}... ({len(content)} chars)")
            return content
        return None
    except Exception as e:
        logger.debug(f"Anticipation lookup failed for {section}: {e}")
        return None


def get_anticipation_meta(session_id: str) -> Optional[Dict]:
    """Get metadata about what was pre-computed for this session."""
    client = get_redis_client()
    if not client:
        return None

    try:
        project = get_project_id()
        key = f"{KEY_ANTICIPATION}meta:{project}:{session_id}"
        data = client.get(key)
        # Backward compat: try legacy key
        if not data:
            data = client.get(f"{KEY_ANTICIPATION}meta:{session_id}")
        if data:
            return json.loads(data)
        return None
    except Exception:
        return None


def invalidate_anticipation(session_id: str) -> bool:
    """
    Invalidate all anticipation cache for a session.

    Call this when pre-computed content was consumed, to prevent serving
    the same content twice. Invalidates both project-scoped and legacy keys.
    """
    client = get_redis_client()
    if not client:
        return False

    try:
        project = get_project_id()
        for section in ["s2", "s8", "meta"]:
            # Delete project-scoped key
            key = f"{KEY_ANTICIPATION}{section}:{project}:{session_id}"
            client.delete(key)
            # Also delete legacy unscoped key (cleanup)
            legacy_key = f"{KEY_ANTICIPATION}{section}:{session_id}"
            client.delete(legacy_key)
        logger.debug(f"Anticipation invalidated for {project}/{session_id[:12]}...")
        return True
    except Exception as e:
        logger.error(f"Failed to invalidate anticipation: {e}")
        return False


# =============================================================================
# S10 STRATEGIC ANALYST CACHE
# =============================================================================
# Pre-computed strategic analysis from GPT-4.1, cached for instant webhook delivery.
# See memory/strategic_analyst.py for the producer side.

KEY_STRATEGIC = f"{KEY_PREFIX}strategic:"
TTL_STRATEGIC = 1800  # 30 minutes — longer than anticipation (runs every 15min)
TTL_STRATEGIC_FALLBACK = 3600  # 1 hour stale fallback


def cache_strategic_section(session_id: str, content: str, ttl: int = TTL_STRATEGIC) -> bool:
    """
    Cache S10 strategic analysis content from GPT-4.1.

    Key pattern: contextdna:strategic:s10:{project}:{session_id}
    Also writes a :fallback key with 1hr TTL for stale-but-present fallback.
    """
    client = get_redis_client()
    if not client:
        return False

    try:
        project = get_project_id()
        key = f"{KEY_STRATEGIC}s10:{project}:{session_id}"
        data = json.dumps({
            "content": content,
            "cached_at": datetime.utcnow().isoformat(),
            "engine": "strategic_analyst_v1",
            "project": project,
        })
        client.setex(key, ttl, data)

        # Also write fallback key with longer TTL (stale-but-present)
        fallback_key = f"{KEY_STRATEGIC}s10:{project}:fallback"
        client.setex(fallback_key, TTL_STRATEGIC_FALLBACK, data)

        logger.debug(f"Strategic S10 cached for {project}/{session_id[:12]}... ({len(content)} chars)")
        return True
    except Exception as e:
        logger.error(f"Failed to cache strategic S10: {e}")
        return False


def get_strategic_section(session_id: str) -> Optional[str]:
    """
    Retrieve pre-computed S10 strategic content from cache.

    4-level fallback (same pattern as get_anticipation_section):
    1. Project-scoped + session-scoped key
    2. Project-scoped fallback key (stale-but-present)
    3. Cross-IDE SCAN fallback
    """
    client = get_redis_client()
    if not client:
        return None

    try:
        project = get_project_id()

        # Level 1: exact session match
        key = f"{KEY_STRATEGIC}s10:{project}:{session_id}"
        data = client.get(key)

        # Level 2: stale fallback
        if not data:
            fallback_key = f"{KEY_STRATEGIC}s10:{project}:fallback"
            data = client.get(fallback_key)
            if data:
                logger.info(f"Strategic S10 FALLBACK hit for {project}")

        # Level 3: cross-IDE SCAN
        if not data:
            scan_pattern = f"{KEY_STRATEGIC}s10:{project}:*"
            try:
                for found_key in client.scan_iter(match=scan_pattern, count=10):
                    key_str = found_key if isinstance(found_key, str) else found_key.decode()
                    if key_str.endswith(":fallback"):
                        continue
                    data = client.get(found_key)
                    if data:
                        logger.info(f"Strategic S10 SCAN hit: {key_str}")
                        break
            except Exception as scan_err:
                logger.debug(f"Strategic S10 SCAN failed: {scan_err}")

        if not data:
            return None

        parsed = json.loads(data)
        content = parsed.get("content")
        if content:
            logger.info(f"Strategic S10 HIT: {project}/{session_id[:12]}... ({len(content)} chars)")
            return content
        return None
    except Exception as e:
        logger.debug(f"Strategic S10 lookup failed: {e}")
        return None


# =============================================================================
# SUPERHERO ANTICIPATION CACHE
# =============================================================================

KEY_SUPERHERO = f"{KEY_PREFIX}superhero:"
SUPERHERO_TTL = 900  # 15 minutes — covers a typical agent burst session
SUPERHERO_ARTIFACTS = ["mission", "gotchas", "architecture", "failures"]


def set_superhero_active(session_id: str, task: str = "", ttl: int = SUPERHERO_TTL) -> bool:
    """Flag superhero mode as active for this session."""
    client = get_redis_client()
    if not client:
        return False
    try:
        project = get_project_id()
        key = f"{KEY_SUPERHERO}{project}:{session_id}:active"
        client.setex(key, ttl, json.dumps({
            "active": True,
            "task": task[:200],
            "activated_at": datetime.utcnow().isoformat(),
            "project": project,
        }))
        logger.info(f"Superhero mode ACTIVATED for {project}/{session_id[:12]}... (TTL {ttl}s)")
        return True
    except Exception as e:
        logger.error(f"Failed to activate superhero mode: {e}")
        return False


def is_superhero_mode_active(session_id: str = None) -> bool:
    """Check if superhero mode is active. Auto-detects session if not provided."""
    client = get_redis_client()
    if not client:
        return False
    try:
        project = get_project_id()
        if session_id:
            key = f"{KEY_SUPERHERO}{project}:{session_id}:active"
            if client.exists(key):
                return True
        # Scan for any active superhero key for this project
        pattern = f"{KEY_SUPERHERO}{project}:*:active"
        for found_key in client.scan_iter(match=pattern, count=20):
            return True
        return False
    except Exception:
        return False


def cache_superhero_artifact(session_id: str, artifact_name: str,
                              content: str, ttl: int = SUPERHERO_TTL) -> bool:
    """Cache a pre-computed superhero artifact."""
    client = get_redis_client()
    if not client:
        return False
    try:
        project = get_project_id()
        key = f"{KEY_SUPERHERO}{project}:{session_id}:{artifact_name}"
        data = json.dumps({
            "content": content,
            "cached_at": datetime.utcnow().isoformat(),
            "project": project,
        })
        client.setex(key, ttl, data)
        logger.debug(f"Superhero artifact cached: {artifact_name} ({len(content)} chars)")
        return True
    except Exception as e:
        logger.error(f"Failed to cache superhero artifact {artifact_name}: {e}")
        return False


def get_superhero_artifact(session_id: str, artifact_name: str) -> Optional[str]:
    """Retrieve a single superhero artifact."""
    client = get_redis_client()
    if not client:
        return None
    try:
        project = get_project_id()
        key = f"{KEY_SUPERHERO}{project}:{session_id}:{artifact_name}"
        data = client.get(key)
        # Scan fallback (session ID mismatch)
        if not data:
            pattern = f"{KEY_SUPERHERO}{project}:*:{artifact_name}"
            for found_key in client.scan_iter(match=pattern, count=20):
                data = client.get(found_key)
                if data:
                    break
        if data:
            parsed = json.loads(data)
            return parsed.get("content")
        return None
    except Exception:
        return None


def get_all_superhero_artifacts(session_id: str = None) -> Optional[Dict[str, str]]:
    """Retrieve all superhero artifacts. Returns None if superhero not active."""
    if not is_superhero_mode_active(session_id):
        return None
    result = {}
    for name in SUPERHERO_ARTIFACTS:
        content = get_superhero_artifact(session_id or "", name)
        if content:
            result[name] = content
    if not result:
        return None
    # Include metadata
    client = get_redis_client()
    if client:
        try:
            project = get_project_id()
            pattern = f"{KEY_SUPERHERO}{project}:*:active"
            for found_key in client.scan_iter(match=pattern, count=5):
                active_data = client.get(found_key)
                if active_data:
                    parsed = json.loads(active_data)
                    result["_task"] = parsed.get("task", "")
                    result["_activated_at"] = parsed.get("activated_at", "")
                    break
        except Exception:
            pass
    return result


def clear_superhero_cache(session_id: str = None) -> bool:
    """Explicitly deactivate superhero mode and clear all artifacts."""
    client = get_redis_client()
    if not client:
        return False
    try:
        project = get_project_id()
        if session_id:
            for suffix in SUPERHERO_ARTIFACTS + ["active", "meta"]:
                client.delete(f"{KEY_SUPERHERO}{project}:{session_id}:{suffix}")
        else:
            # Clear all superhero keys for this project
            pattern = f"{KEY_SUPERHERO}{project}:*"
            for found_key in client.scan_iter(match=pattern, count=100):
                client.delete(found_key)
        logger.info(f"Superhero cache cleared for {project}")
        return True
    except Exception as e:
        logger.error(f"Failed to clear superhero cache: {e}")
        return False


# =============================================================================
# AGENT FINDINGS WAL (sorted set — time-indexed, queryable)
# =============================================================================

FINDINGS_WAL_TTL = 3600  # 1 hour — survives full session + post-session Atlas review
FINDINGS_WAL_CAP = 200


def record_agent_finding_wal(session_id: str, agent_id: str, finding: str,
                              finding_type: str = "observation",
                              severity: str = "info") -> bool:
    """Record agent finding in Redis sorted set WAL (score=timestamp).

    Args:
        session_id: Session ID (or "unknown")
        agent_id: Agent identifier (e.g. "exec-agent-03")
        finding: Finding text (capped at 500 chars)
        finding_type: observation|gotcha|file_found|error|suggestion|critical
        severity: info|low|medium|high|critical
    """
    client = get_redis_client()
    if not client:
        return False
    try:
        import time as _t
        project = get_project_id()
        key = f"{KEY_SUPERHERO}{project}:{session_id}:findings_wal"
        now = _t.time()
        entry = json.dumps({
            "agent_id": agent_id,
            "finding": finding[:500],
            "type": finding_type,
            "severity": severity,
            "ts": now,
            "ts_iso": _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime(now)),
        })
        pipe = client.pipeline()
        pipe.zadd(key, {entry: now})
        pipe.zremrangebyrank(key, 0, -(FINDINGS_WAL_CAP + 1))
        pipe.expire(key, FINDINGS_WAL_TTL)
        pipe.execute()
        return True
    except Exception as e:
        logger.debug(f"[findings_wal] record failed: {e}")
        return False


def get_agent_findings_wal(session_id: str, since_seconds: int = 0,
                            finding_type: str = None,
                            limit: int = 50) -> List[Dict]:
    """Query agent findings WAL by time range and optional type filter.

    Args:
        session_id: Session ID
        since_seconds: Only return findings from last N seconds (0 = all)
        finding_type: Filter by type (None = all types)
        limit: Max entries to return
    """
    client = get_redis_client()
    if not client:
        return []
    try:
        import time as _t
        project = get_project_id()
        key = f"{KEY_SUPERHERO}{project}:{session_id}:findings_wal"
        min_score = _t.time() - since_seconds if since_seconds > 0 else "-inf"
        raw = client.zrangebyscore(key, min_score, "+inf", start=0, num=limit)
        results = []
        for item in raw:
            parsed = json.loads(item)
            if finding_type and parsed.get("type") != finding_type:
                continue
            results.append(parsed)
        return results
    except Exception:
        return []


def get_agent_findings_summary(session_id: str) -> Dict:
    """Compact digest of all WAL findings for Atlas consumption.

    Returns: {total, by_type, by_agent, by_severity, criticals, recent_5}
    """
    client = get_redis_client()
    if not client:
        return {"total": 0}
    try:
        project = get_project_id()
        key = f"{KEY_SUPERHERO}{project}:{session_id}:findings_wal"
        raw = client.zrange(key, 0, -1)
        if not raw:
            return {"total": 0}
        findings = [json.loads(item) for item in raw]
        by_type = {}
        by_agent = {}
        by_severity = {}
        criticals = []
        for f in findings:
            ft = f.get("type", "unknown")
            by_type[ft] = by_type.get(ft, 0) + 1
            ag = f.get("agent_id", "unknown")
            by_agent[ag] = by_agent.get(ag, 0) + 1
            sv = f.get("severity", "info")
            by_severity[sv] = by_severity.get(sv, 0) + 1
            if sv in ("critical", "high"):
                criticals.append(f)
        return {
            "total": len(findings),
            "by_type": by_type,
            "by_agent": by_agent,
            "by_severity": by_severity,
            "criticals": criticals[-10:],
            "recent_5": findings[-5:],
        }
    except Exception:
        return {"total": 0}


# =============================================================================
# HEALTH CHECK
# =============================================================================

def redis_health_check() -> Dict:
    """Check Redis health and return status."""
    client = get_redis_client()

    if not client:
        return {
            "status": "unavailable",
            "error": "Could not connect to Redis"
        }

    try:
        info = client.info()
        return {
            "status": "healthy",
            "connected_clients": info.get("connected_clients"),
            "used_memory_human": info.get("used_memory_human"),
            "uptime_in_seconds": info.get("uptime_in_seconds"),
            "redis_version": info.get("redis_version")
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }
