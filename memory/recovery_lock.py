"""
Recovery coordination lock — prevents multiple monitors from
triggering simultaneous recovery attempts.

Usage:
    from memory.recovery_lock import acquire_recovery_lock

    if acquire_recovery_lock("llm_watchdog"):
        # Only one monitor wins — proceed with recovery
        start_missing_services(dry_run=False)
    else:
        # Another monitor already recovering — skip
        logger.info("Recovery already in progress, skipping")
"""

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_REDIS_KEY = "contextdna:recovery:in_progress"
_LOCK_TTL_SECONDS = 30  # Auto-expire if holder dies


def acquire_recovery_lock(monitor_name: str, ttl: int = _LOCK_TTL_SECONDS) -> bool:
    """Try to acquire the recovery lock. Returns True if acquired.

    Uses Redis SET NX (atomic) with TTL auto-expire.
    Only ONE monitor can hold the lock at a time.
    """
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        payload = json.dumps({
            "monitor": monitor_name,
            "started_at": time.time(),
            "pid": __import__("os").getpid(),
        })
        acquired = r.set(_REDIS_KEY, payload, nx=True, ex=ttl)
        if acquired:
            logger.info(f"Recovery lock acquired by {monitor_name}")
        else:
            existing = r.get(_REDIS_KEY)
            logger.debug(f"Recovery lock held: {existing}")
        return bool(acquired)
    except Exception as e:
        logger.warning(f"Recovery lock unavailable ({e}), proceeding anyway")
        return True  # Fail-open: if Redis down, allow recovery


def release_recovery_lock() -> None:
    """Release the recovery lock after recovery completes."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        r.delete(_REDIS_KEY)
        logger.info("Recovery lock released")
    except Exception:
        pass  # Lock will auto-expire via TTL


def get_recovery_status() -> Optional[dict]:
    """Check if recovery is in progress. Returns lock payload or None."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        raw = r.get(_REDIS_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None
