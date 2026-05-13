"""
PostgreSQL Advisory Lock for Sync Coordination

Prevents two daemons (lite_scheduler + agent_service) from syncing
simultaneously. Uses PG advisory locks — auto-released on disconnect,
zero overhead, no stale locks possible.

Usage:
    with sync_advisory_lock(pg_conn) as acquired:
        if acquired:
            # safe to sync
        else:
            # another daemon holds the lock, skip this cycle
"""

import logging

logger = logging.getLogger("contextdna.sync_lock")

# Fixed lock ID — all sync daemons compete for the same lock.
# 0x4F6E5379 = "OnSy" (One Sync)
SYNC_LOCK_ID = 0x4F6E5379


class SyncAdvisoryLock:
    """
    PG advisory lock context manager.

    Non-blocking: returns immediately with acquired=True/False.
    Auto-released when connection closes (no stale locks).
    """

    def __init__(self, conn):
        self.conn = conn
        self.acquired = False

    def __enter__(self):
        if not self.conn:
            return self
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_ID,))
                self.acquired = cur.fetchone()[0]
            if not self.acquired:
                logger.debug("sync lock held by another daemon — skipping cycle")
        except Exception as e:
            logger.warning(f"advisory lock check failed: {e}")
            self.acquired = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.acquired and self.conn:
            try:
                with self.conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_ID,))
            except Exception:
                pass  # auto-released on disconnect anyway
        return False


def sync_advisory_lock(conn):
    """Convenience wrapper."""
    return SyncAdvisoryLock(conn)
