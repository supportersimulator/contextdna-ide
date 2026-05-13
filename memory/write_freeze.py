"""
Write Freeze Guard — blocks writes during migration drain stage.

Usage:
    from memory.write_freeze import get_write_freeze_guard, WriteFrozenError

    guard = get_write_freeze_guard()
    guard.check_or_raise()  # Before any write op

    # Or check without raising:
    if guard.is_frozen():
        logger.warning("Writes frozen, skipping")

Freeze lifecycle (managed by mode_switch.py):
    guard.freeze(workspace_id='ws1', reason='migration')
    # ... migration runs ...
    guard.thaw(workspace_id='ws1')

Safety:
    - TTL auto-expires freeze (default 60s) — prevents permanent lockout
    - Redis failure = fail open (writes allowed if Redis unreachable)
    - Module-level kill switch: _write_freeze_enabled = False to disable
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

FREEZE_KEY = 'contextdna:write_freeze'
FREEZE_TTL = 60  # safety TTL in seconds

# Module-level kill switch for testing/emergency override
_write_freeze_enabled = True


class WriteFrozenError(Exception):
    """Raised when a write operation is attempted during an active freeze."""
    pass


class WriteFreezeGuard:
    """Guards write operations during migration drain stage.

    Backed by Redis keys with TTL. Fail-open on Redis errors.
    Supports workspace-scoped freezes (freeze ws A, ws B unaffected).
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client

    def _get_redis(self):
        """Lazy Redis connection — fail open if unavailable."""
        if self._redis is not None:
            return self._redis
        try:
            import redis
            self._redis = redis.Redis(host='127.0.0.1', port=6379)
            # Verify connection
            self._redis.ping()
            return self._redis
        except Exception:
            return None

    def _key(self, workspace_id: Optional[str] = None) -> str:
        if workspace_id:
            return f'{FREEZE_KEY}:{workspace_id}'
        return FREEZE_KEY

    def is_frozen(self, workspace_id: Optional[str] = None) -> bool:
        """Check if writes are frozen. Returns False on Redis failure (fail open)."""
        if not _write_freeze_enabled:
            return False
        try:
            r = self._get_redis()
            if r is None:
                return False  # Fail open
            return bool(r.exists(self._key(workspace_id)))
        except Exception:
            return False  # Fail open

    def freeze(self, workspace_id: Optional[str] = None, reason: str = 'migration', ttl: int = FREEZE_TTL) -> None:
        """Activate write freeze with safety TTL."""
        try:
            r = self._get_redis()
            if r is None:
                logger.warning("Cannot set write freeze: Redis unavailable")
                return
            key = self._key(workspace_id)
            value = f'{reason}:{time.time()}'
            r.setex(key, ttl, value)
            logger.info(f"Write freeze ACTIVATED: key={key} ttl={ttl}s reason={reason}")
        except Exception as e:
            logger.error(f"Failed to set write freeze: {e}")

    def thaw(self, workspace_id: Optional[str] = None) -> None:
        """Clear write freeze."""
        try:
            r = self._get_redis()
            if r is None:
                logger.warning("Cannot clear write freeze: Redis unavailable")
                return
            key = self._key(workspace_id)
            r.delete(key)
            logger.info(f"Write freeze CLEARED: key={key}")
        except Exception as e:
            logger.error(f"Failed to clear write freeze: {e}")

    def check_or_raise(self, workspace_id: Optional[str] = None) -> None:
        """Raise WriteFrozenError if frozen. Call before any write op.

        Fail-open: if Redis is unreachable, does NOT raise (allows writes).
        """
        if self.is_frozen(workspace_id):
            raise WriteFrozenError(
                f'Writes frozen for workspace {workspace_id or "global"}'
            )


# Module-level singleton
_guard_instance: Optional[WriteFreezeGuard] = None


def get_write_freeze_guard() -> WriteFreezeGuard:
    """Get the module-level WriteFreezeGuard singleton."""
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = WriteFreezeGuard()
    return _guard_instance
