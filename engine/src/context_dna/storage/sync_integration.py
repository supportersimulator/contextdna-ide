"""
Sync Integration for agent_service.py - NO NEW DAEMON NEEDED

This module provides hooks to integrate bidirectional sync into the
EXISTING agent_service.py background loops, avoiding daemon proliferation.

INTEGRATION APPROACH:
1. Add sync check to existing _brain_cycle_loop() (every 5 min)
2. No new asyncio.create_task() - reuse existing loops
3. Minimal footprint - just a few function calls

LITE → HEAVY TAKEOVER (Smart Sync):
When Docker/Postgres comes back online, we need intelligent takeover:

1. VERSION COMPARISON:
   - Get Postgres latest version
   - Get SQLite latest version
   - Compare timestamps, not just version numbers

2. CONFLICT RESOLUTION:
   - "latest_wins" (default): Most recent write wins
   - "merge": Keep both, mark conflicts for manual review
   - "lite_wins": Local SQLite always wins (offline-first)
   - "heavy_wins": Postgres always wins (server-authoritative)

3. DEDUPLICATION:
   - Profile ID + machine_id forms unique key
   - Don't create duplicates on sync
   - Update existing record if newer

Usage in agent_service.py:
    from context_dna.storage.sync_integration import (
        check_and_sync_postgres,
        get_sync_status,
        SYNC_CHECK_INTERVAL,
    )

    # In HelperAgent.__init__():
    self.sync_interval = SYNC_CHECK_INTERVAL

    # In HelperAgent.start():
    asyncio.create_task(self._postgres_sync_loop())

    # New loop to add:
    async def _postgres_sync_loop(self):
        while self.running:
            try:
                await check_and_sync_postgres()
            except Exception as e:
                print(f"Postgres sync error: {e}")
            await asyncio.sleep(self.sync_interval)
"""

import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple
from pathlib import Path
from enum import Enum
from dataclasses import dataclass

# Configuration
SYNC_CHECK_INTERVAL = int(os.getenv("POSTGRES_SYNC_INTERVAL", 120))  # 2 minutes default
SYNC_CONFLICT_RESOLUTION = os.getenv("SYNC_CONFLICT_RESOLUTION", "latest_wins")
SYNC_ENABLED = os.getenv("SYNC_ENABLED", "true").lower() == "true"


class ConflictResolution(Enum):
    LATEST_WINS = "latest_wins"
    MERGE = "merge"
    LITE_WINS = "lite_wins"
    HEAVY_WINS = "heavy_wins"


@dataclass
class SyncResult:
    """Result of a sync operation."""
    success: bool
    mode_before: str
    mode_after: str
    postgres_available: bool
    items_synced_to_postgres: int
    items_synced_from_postgres: int
    conflicts_detected: int
    conflicts_resolved: int
    error: Optional[str] = None
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "mode_before": self.mode_before,
            "mode_after": self.mode_after,
            "postgres_available": self.postgres_available,
            "items_synced_to_postgres": self.items_synced_to_postgres,
            "items_synced_from_postgres": self.items_synced_from_postgres,
            "conflicts_detected": self.conflicts_detected,
            "conflicts_resolved": self.conflicts_resolved,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


# Global state (singleton pattern like agent_service uses)
_sync_store = None
_last_sync_result: Optional[SyncResult] = None
_last_sync_time: Optional[datetime] = None


async def _get_sync_store():
    """Lazy-load the sync store to avoid import cycles."""
    global _sync_store
    if _sync_store is None:
        try:
            from context_dna.storage.bidirectional_sync import BidirectionalSyncStore
            _sync_store = BidirectionalSyncStore()
            await _sync_store.connect()
        except ImportError:
            print("BidirectionalSyncStore not available - sync disabled")
            return None
    return _sync_store


async def check_and_sync_postgres() -> Optional[SyncResult]:
    """
    Main sync function - call this from existing background loops.

    INTELLIGENT TAKEOVER LOGIC:

    1. Check if Postgres is available
    2. If was down, now up → sync lite→heavy (process queue)
    3. If was up, now down → snapshot heavy→lite
    4. If stable → periodic sync check
    """
    global _last_sync_result, _last_sync_time

    if not SYNC_ENABLED:
        return None

    store = await _get_sync_store()
    if not store:
        return None

    start_time = datetime.utcnow()

    try:
        # Get current state
        state = await store.get_sync_state()
        mode_before = state.current_mode

        # Check and potentially recover
        recovery = await store.check_and_recover()

        # Get updated state
        state_after = await store.get_sync_state()
        mode_after = state_after.current_mode

        # Build result
        result = SyncResult(
            success=True,
            mode_before=mode_before,
            mode_after=mode_after,
            postgres_available=state_after.postgres_available,
            items_synced_to_postgres=recovery.get('items_synced', 0),
            items_synced_from_postgres=recovery.get('items_synced_from_postgres', 0),
            conflicts_detected=0,
            conflicts_resolved=0,
            duration_ms=int((datetime.utcnow() - start_time).total_seconds() * 1000),
        )

        # Log mode transitions
        if mode_before != mode_after:
            print(f"[SYNC] Mode transition: {mode_before} → {mode_after}")
            if recovery.get('postgres_recovered'):
                print(f"[SYNC] Synced {result.items_synced_to_postgres} items to Postgres")

        _last_sync_result = result
        _last_sync_time = datetime.utcnow()
        return result

    except Exception as e:
        result = SyncResult(
            success=False,
            mode_before="unknown",
            mode_after="unknown",
            postgres_available=False,
            items_synced_to_postgres=0,
            items_synced_from_postgres=0,
            conflicts_detected=0,
            conflicts_resolved=0,
            error=str(e),
            duration_ms=int((datetime.utcnow() - start_time).total_seconds() * 1000),
        )
        _last_sync_result = result
        _last_sync_time = datetime.utcnow()
        return result


def get_sync_status() -> dict:
    """Get current sync status for health checks."""
    return {
        "enabled": SYNC_ENABLED,
        "interval_seconds": SYNC_CHECK_INTERVAL,
        "conflict_resolution": SYNC_CONFLICT_RESOLUTION,
        "last_sync": _last_sync_time.isoformat() if _last_sync_time else None,
        "last_result": _last_sync_result.to_dict() if _last_sync_result else None,
    }


# =============================================================================
# INTELLIGENT TAKEOVER: Conflict Resolution
# =============================================================================

async def resolve_version_conflict(
    postgres_version: dict,
    sqlite_version: dict,
    resolution: ConflictResolution = ConflictResolution.LATEST_WINS,
) -> Tuple[str, dict]:
    """
    Resolve conflict between Postgres and SQLite versions.

    Returns:
        Tuple of (winner: "postgres"|"sqlite"|"both", resolved_data: dict)
    """
    pg_time = postgres_version.get('created_at') or postgres_version.get('updated_at')
    sq_time = sqlite_version.get('created_at') or sqlite_version.get('updated_at')

    # Parse timestamps
    try:
        pg_dt = datetime.fromisoformat(pg_time.replace('Z', '+00:00')) if pg_time else datetime.min
    except:
        pg_dt = datetime.min

    try:
        sq_dt = datetime.fromisoformat(sq_time.replace('Z', '+00:00')) if sq_time else datetime.min
    except:
        sq_dt = datetime.min

    if resolution == ConflictResolution.LATEST_WINS:
        if pg_dt >= sq_dt:
            return "postgres", postgres_version.get('data', postgres_version)
        else:
            return "sqlite", sqlite_version.get('data', sqlite_version)

    elif resolution == ConflictResolution.LITE_WINS:
        return "sqlite", sqlite_version.get('data', sqlite_version)

    elif resolution == ConflictResolution.HEAVY_WINS:
        return "postgres", postgres_version.get('data', postgres_version)

    elif resolution == ConflictResolution.MERGE:
        # Merge both - mark as conflict for manual review
        merged = {
            **sqlite_version.get('data', sqlite_version),
            **postgres_version.get('data', postgres_version),
            "_conflict": True,
            "_conflict_time": datetime.utcnow().isoformat(),
            "_postgres_version": postgres_version,
            "_sqlite_version": sqlite_version,
        }
        return "both", merged

    # Default to latest
    if pg_dt >= sq_dt:
        return "postgres", postgres_version.get('data', postgres_version)
    else:
        return "sqlite", sqlite_version.get('data', sqlite_version)


# =============================================================================
# PATCH FOR agent_service.py (Copy this into HelperAgent class)
# =============================================================================

AGENT_SERVICE_PATCH = '''
# Add to HelperAgent.__init__():
self.sync_interval = int(os.getenv("POSTGRES_SYNC_INTERVAL", 120))

# Add to HelperAgent.start() after other create_task calls:
asyncio.create_task(self._postgres_sync_loop())
print(f"Postgres sync active - checking every {self.sync_interval}s")

# Add this new method to HelperAgent class:
async def _postgres_sync_loop(self):
    """
    Periodically sync with Postgres - INTEGRATED into existing daemon.
    No new daemon needed - reuses this service's event loop.
    """
    # Import here to avoid startup issues
    try:
        from context_dna.storage.sync_integration import check_and_sync_postgres
        SYNC_AVAILABLE = True
    except ImportError:
        SYNC_AVAILABLE = False
        print("Postgres sync not available - module not found")
        return

    while self.running:
        try:
            result = await check_and_sync_postgres()
            if result and result.success:
                self.stats["postgres_syncs"] = self.stats.get("postgres_syncs", 0) + 1
                if result.items_synced_to_postgres > 0:
                    self.stats["items_synced_to_postgres"] = (
                        self.stats.get("items_synced_to_postgres", 0) +
                        result.items_synced_to_postgres
                    )
        except Exception as e:
            print(f"Postgres sync error: {e}")

        await asyncio.sleep(self.sync_interval)
'''


# =============================================================================
# CLI for testing
# =============================================================================

if __name__ == "__main__":
    import sys

    async def main():
        if len(sys.argv) > 1:
            cmd = sys.argv[1]

            if cmd == "status":
                status = get_sync_status()
                print(json.dumps(status, indent=2))

            elif cmd == "sync":
                print("Running sync check...")
                result = await check_and_sync_postgres()
                if result:
                    print(json.dumps(result.to_dict(), indent=2))
                else:
                    print("Sync not available")

            elif cmd == "patch":
                print("=== PATCH FOR agent_service.py ===")
                print(AGENT_SERVICE_PATCH)

            else:
                print(f"Unknown command: {cmd}")
                print("Usage: python sync_integration.py [status|sync|patch]")
        else:
            print("Usage: python sync_integration.py [status|sync|patch]")

    asyncio.run(main())
