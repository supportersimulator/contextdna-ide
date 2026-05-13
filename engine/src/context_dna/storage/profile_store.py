"""
PostgreSQL Profile Store for Context DNA Adaptive Hierarchy.

Provides version history, automatic backups, and rollback capability
for hierarchy profiles. This is the foundation for adaptive evolution.

Features:
- Full version history per machine
- Automatic backup before changes
- Rollback to any previous version
- Machine-specific + team base profiles
- Graceful degradation (falls back to SQLite when PostgreSQL unavailable)

Usage:
    from context_dna.storage.profile_store import ProfileStore

    store = ProfileStore()

    # Save profile (auto-backups current)
    version_id = await store.save_profile(profile, reason="user update")

    # Get active profile
    profile = await store.get_active_profile()

    # Rollback
    await store.rollback_to_version(old_version_id)

    # List versions
    versions = await store.list_versions()
"""

import os
import json
import uuid
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

# Try to import asyncpg, fall back gracefully
try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

from .backend import StorageBackend


# =============================================================================
# SCHEMA SQL
# =============================================================================

SCHEMA_SQL = """
-- Hierarchy Profiles with Full Version History
--
-- This schema supports:
-- - Per-machine profiles with version history
-- - Automatic backups before changes
-- - Rollback capability
-- - Team-level defaults

-- Main profiles table
CREATE TABLE IF NOT EXISTS hierarchy_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    machine_id TEXT NOT NULL,           -- Identifies which machine
    profile_version INTEGER NOT NULL,   -- Auto-incrementing per machine
    profile_data JSONB NOT NULL,        -- Full hierarchy profile
    source TEXT NOT NULL,               -- 'auto_scan', 'user_input', 'merge', 'rollback'
    parent_version_id UUID REFERENCES hierarchy_profiles(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    created_by TEXT DEFAULT 'system',   -- User or 'system'
    notes TEXT,                         -- Why this version was created
    is_active BOOLEAN DEFAULT TRUE,     -- Current active profile for this machine
    is_milestone BOOLEAN DEFAULT FALSE, -- Protected from cleanup
    schema_version TEXT DEFAULT '1.0.0' -- For migrations
);

-- Backups table (before any structural change)
CREATE TABLE IF NOT EXISTS hierarchy_backups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id UUID REFERENCES hierarchy_profiles(id),
    backup_reason TEXT NOT NULL,        -- 'pre_restructure', 'manual', 'scheduled'
    backup_data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- User preferences per machine (cached answers, pins, etc.)
CREATE TABLE IF NOT EXISTS user_preferences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    machine_id TEXT NOT NULL,
    preference_key TEXT NOT NULL,
    preference_value JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(machine_id, preference_key)
);

-- Structure suggestions tracking (what was suggested, user response)
CREATE TABLE IF NOT EXISTS structure_suggestions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    machine_id TEXT NOT NULL,
    suggestion_type TEXT NOT NULL,      -- 'inconsistent_naming', 'scattered_configs', etc.
    suggestion_data JSONB NOT NULL,
    user_response TEXT,                 -- 'accepted', 'dismissed', 'remind_later'
    dismissed_at TIMESTAMPTZ,
    remind_after TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_active_profile
    ON hierarchy_profiles(machine_id, is_active)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_profile_versions
    ON hierarchy_profiles(machine_id, profile_version DESC);

CREATE INDEX IF NOT EXISTS idx_suggestions_machine
    ON structure_suggestions(machine_id, suggestion_type);
"""


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ProfileVersion:
    """A version entry in the profile history."""
    id: str
    machine_id: str
    version: int
    source: str
    created_at: datetime
    created_by: str
    notes: Optional[str]
    is_active: bool
    is_milestone: bool

    @classmethod
    def from_row(cls, row: dict) -> "ProfileVersion":
        return cls(
            id=str(row["id"]),
            machine_id=row["machine_id"],
            version=row["profile_version"],
            source=row["source"],
            created_at=row["created_at"],
            created_by=row["created_by"] or "system",
            notes=row.get("notes"),
            is_active=row["is_active"],
            is_milestone=row.get("is_milestone", False),
        )


@dataclass
class ProfileBackup:
    """A backup entry."""
    id: str
    profile_id: str
    reason: str
    created_at: datetime


# =============================================================================
# PROFILE STORE
# =============================================================================

class ProfileStore:
    """
    PostgreSQL-backed version control for hierarchy profiles.

    Core principles:
    - Always backup before changes
    - Never lose user data
    - Machine-specific with team fallback
    - Graceful degradation to SQLite
    """

    SCHEMA_VERSION = "1.0.0"
    KEEP_RECENT_VERSIONS = 10
    KEEP_DAYS = 30

    def __init__(
        self,
        database_url: str = None,
        machine_id: str = None,
        fallback_path: Path = None,
    ):
        """
        Initialize profile store.

        Args:
            database_url: PostgreSQL connection URL
            machine_id: Unique identifier for this machine
            fallback_path: Path for SQLite fallback
        """
        self.database_url = database_url or os.environ.get(
            "DATABASE_URL",
            f"postgresql://context_dna:{os.getenv('POSTGRES_PASSWORD', 'context_dna_dev')}@localhost:5432/contextdna"
        )
        self.machine_id = machine_id or self._get_machine_id()
        self.fallback_path = fallback_path or Path.home() / ".context-dna" / "profiles.db"

        self._pool: Optional[asyncpg.Pool] = None
        self._using_fallback = False
        self._initialized = False

    def _get_machine_id(self) -> str:
        """Get a unique identifier for this machine."""
        import hashlib
        import socket

        # Combine hostname + user for unique but stable ID
        hostname = socket.gethostname()
        username = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
        combined = f"{hostname}:{username}"

        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    # -------------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------------

    async def connect(self) -> bool:
        """
        Connect to PostgreSQL (or fall back to SQLite).

        Returns:
            True if connected successfully
        """
        if not ASYNCPG_AVAILABLE:
            print("asyncpg not available - using SQLite fallback")
            self._using_fallback = True
            return await self._init_sqlite_fallback()

        try:
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=5,
                timeout=10,
            )

            # Initialize schema
            await self._init_schema()
            self._initialized = True
            return True

        except Exception as e:
            print(f"PostgreSQL connection failed: {e}")
            print("Falling back to SQLite")
            self._using_fallback = True
            return await self._init_sqlite_fallback()

    async def _init_schema(self):
        """Initialize database schema."""
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)

    async def _init_sqlite_fallback(self) -> bool:
        """Initialize SQLite fallback storage."""
        import sqlite3

        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)

        # SQLite version of the schema (simplified)
        sqlite_schema = """
        CREATE TABLE IF NOT EXISTS hierarchy_profiles (
            id TEXT PRIMARY KEY,
            machine_id TEXT NOT NULL,
            profile_version INTEGER NOT NULL,
            profile_data TEXT NOT NULL,
            source TEXT NOT NULL,
            parent_version_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT DEFAULT 'system',
            notes TEXT,
            is_active INTEGER DEFAULT 1,
            is_milestone INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS hierarchy_backups (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            backup_reason TEXT NOT NULL,
            backup_data TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_active_profile
            ON hierarchy_profiles(machine_id, is_active);
        """

        conn = sqlite3.connect(str(self.fallback_path))
        conn.executescript(sqlite_schema)
        conn.close()

        self._initialized = True
        return True

    async def close(self):
        """Close database connections."""
        if self._pool:
            await self._pool.close()

    # -------------------------------------------------------------------------
    # Profile Operations
    # -------------------------------------------------------------------------

    async def save_profile(
        self,
        profile_data: dict,
        source: str = "user_update",
        notes: str = None,
        created_by: str = None,
    ) -> str:
        """
        Save a new profile version.

        ALWAYS backs up current profile first!

        Args:
            profile_data: The profile dictionary to save
            source: How this profile was created
            notes: Optional notes about this version
            created_by: User who created this version

        Returns:
            The new version's UUID
        """
        if not self._initialized:
            await self.connect()

        # Get current active profile for backup
        current = await self.get_active_profile()
        if current:
            await self._create_backup(current["id"], f"pre_{source}")

        # Get next version number
        next_version = await self._get_next_version()

        # Create new version
        version_id = str(uuid.uuid4())

        if self._using_fallback:
            return await self._sqlite_save_profile(
                version_id, profile_data, source, notes,
                created_by, next_version, current
            )

        async with self._pool.acquire() as conn:
            # Deactivate old profiles for this machine
            await conn.execute("""
                UPDATE hierarchy_profiles
                SET is_active = FALSE
                WHERE machine_id = $1 AND is_active = TRUE
            """, self.machine_id)

            # Insert new profile
            await conn.execute("""
                INSERT INTO hierarchy_profiles
                (id, machine_id, profile_version, profile_data, source,
                 parent_version_id, notes, created_by, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE)
            """,
                uuid.UUID(version_id),
                self.machine_id,
                next_version,
                json.dumps(profile_data),
                source,
                uuid.UUID(current["id"]) if current else None,
                notes,
                created_by or "system",
            )

        return version_id

    async def get_active_profile(self) -> Optional[dict]:
        """Get the currently active profile for this machine."""
        if not self._initialized:
            await self.connect()

        if self._using_fallback:
            return await self._sqlite_get_active_profile()

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id, profile_data, profile_version, source, created_at, notes
                FROM hierarchy_profiles
                WHERE machine_id = $1 AND is_active = TRUE
                ORDER BY profile_version DESC
                LIMIT 1
            """, self.machine_id)

            if row:
                return {
                    "id": str(row["id"]),
                    "data": json.loads(row["profile_data"]),
                    "version": row["profile_version"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                    "notes": row["notes"],
                }

        return None

    async def rollback_to_version(self, version_id: str) -> Optional[str]:
        """
        Rollback to a previous version.

        Creates backup of current, then restores target version.

        Args:
            version_id: UUID of version to restore

        Returns:
            New version ID (copy of restored version)
        """
        if not self._initialized:
            await self.connect()

        # Get target version
        target = await self._get_version_by_id(version_id)
        if not target:
            return None

        # Save as new version (preserves history)
        return await self.save_profile(
            profile_data=target["data"],
            source="rollback",
            notes=f"Rolled back to version {target['version']}",
        )

    async def list_versions(self, limit: int = 10) -> List[ProfileVersion]:
        """List version history for this machine."""
        if not self._initialized:
            await self.connect()

        if self._using_fallback:
            return await self._sqlite_list_versions(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, machine_id, profile_version, source,
                       created_at, created_by, notes, is_active, is_milestone
                FROM hierarchy_profiles
                WHERE machine_id = $1
                ORDER BY profile_version DESC
                LIMIT $2
            """, self.machine_id, limit)

            return [ProfileVersion.from_row(dict(r)) for r in rows]

    async def mark_as_milestone(self, version_id: str):
        """Mark a version as milestone (protected from cleanup)."""
        if self._using_fallback:
            return await self._sqlite_mark_milestone(version_id)

        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE hierarchy_profiles
                SET is_milestone = TRUE
                WHERE id = $1
            """, uuid.UUID(version_id))

    # -------------------------------------------------------------------------
    # Backup Operations
    # -------------------------------------------------------------------------

    async def _create_backup(self, profile_id: str, reason: str):
        """Create a backup of a profile."""
        if self._using_fallback:
            return await self._sqlite_create_backup(profile_id, reason)

        async with self._pool.acquire() as conn:
            # Get profile data
            row = await conn.fetchrow("""
                SELECT profile_data FROM hierarchy_profiles WHERE id = $1
            """, uuid.UUID(profile_id))

            if row:
                await conn.execute("""
                    INSERT INTO hierarchy_backups (id, profile_id, backup_reason, backup_data)
                    VALUES ($1, $2, $3, $4)
                """, uuid.uuid4(), uuid.UUID(profile_id), reason, row["profile_data"])

    async def cleanup_old_versions(self):
        """
        Clean up old versions based on retention policy.

        Keeps:
        - Last KEEP_RECENT_VERSIONS versions
        - All versions from last KEEP_DAYS days
        - All milestone versions
        """
        if self._using_fallback:
            return  # SQLite cleanup not implemented yet

        async with self._pool.acquire() as conn:
            # Get versions to potentially delete
            cutoff_date = datetime.now() - timedelta(days=self.KEEP_DAYS)

            # Find versions older than cutoff that aren't recent or milestones
            await conn.execute("""
                DELETE FROM hierarchy_profiles
                WHERE machine_id = $1
                  AND created_at < $2
                  AND is_milestone = FALSE
                  AND profile_version < (
                      SELECT MAX(profile_version) - $3
                      FROM hierarchy_profiles
                      WHERE machine_id = $1
                  )
            """, self.machine_id, cutoff_date, self.KEEP_RECENT_VERSIONS)

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    async def _get_next_version(self) -> int:
        """Get next version number for this machine."""
        if self._using_fallback:
            return await self._sqlite_get_next_version()

        async with self._pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT COALESCE(MAX(profile_version), 0) + 1
                FROM hierarchy_profiles
                WHERE machine_id = $1
            """, self.machine_id)
            return result

    async def _get_version_by_id(self, version_id: str) -> Optional[dict]:
        """Get a specific version by ID."""
        if self._using_fallback:
            return await self._sqlite_get_version_by_id(version_id)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id, profile_data, profile_version, source, created_at
                FROM hierarchy_profiles
                WHERE id = $1
            """, uuid.UUID(version_id))

            if row:
                return {
                    "id": str(row["id"]),
                    "data": json.loads(row["profile_data"]),
                    "version": row["profile_version"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                }
        return None

    # -------------------------------------------------------------------------
    # SQLite Fallback Methods
    # -------------------------------------------------------------------------

    async def _sqlite_save_profile(
        self, version_id, profile_data, source, notes, created_by, next_version, current
    ) -> str:
        """SQLite implementation of save_profile."""
        import sqlite3

        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()

        # Deactivate old
        cursor.execute(
            "UPDATE hierarchy_profiles SET is_active = 0 WHERE machine_id = ? AND is_active = 1",
            (self.machine_id,)
        )

        # Insert new
        cursor.execute("""
            INSERT INTO hierarchy_profiles
            (id, machine_id, profile_version, profile_data, source,
             parent_version_id, notes, created_by, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            version_id,
            self.machine_id,
            next_version,
            json.dumps(profile_data),
            source,
            current["id"] if current else None,
            notes,
            created_by or "system",
        ))

        conn.commit()
        conn.close()
        return version_id

    async def _sqlite_get_active_profile(self) -> Optional[dict]:
        """SQLite implementation of get_active_profile."""
        import sqlite3

        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, profile_data, profile_version, source, created_at, notes
            FROM hierarchy_profiles
            WHERE machine_id = ? AND is_active = 1
            ORDER BY profile_version DESC
            LIMIT 1
        """, (self.machine_id,))

        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                "id": row[0],
                "data": json.loads(row[1]),
                "version": row[2],
                "source": row[3],
                "created_at": row[4],
                "notes": row[5],
            }
        return None

    async def _sqlite_list_versions(self, limit: int) -> List[ProfileVersion]:
        """SQLite implementation of list_versions."""
        import sqlite3

        conn = sqlite3.connect(str(self.fallback_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, machine_id, profile_version, source,
                   created_at, created_by, notes, is_active, is_milestone
            FROM hierarchy_profiles
            WHERE machine_id = ?
            ORDER BY profile_version DESC
            LIMIT ?
        """, (self.machine_id, limit))

        rows = cursor.fetchall()
        conn.close()

        versions = []
        for row in rows:
            versions.append(ProfileVersion(
                id=row["id"],
                machine_id=row["machine_id"],
                version=row["profile_version"],
                source=row["source"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
                created_by=row["created_by"] or "system",
                notes=row["notes"],
                is_active=bool(row["is_active"]),
                is_milestone=bool(row["is_milestone"]),
            ))
        return versions

    async def _sqlite_get_next_version(self) -> int:
        """SQLite implementation of _get_next_version."""
        import sqlite3

        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()

        cursor.execute("""
            SELECT COALESCE(MAX(profile_version), 0) + 1
            FROM hierarchy_profiles
            WHERE machine_id = ?
        """, (self.machine_id,))

        result = cursor.fetchone()[0]
        conn.close()
        return result

    async def _sqlite_get_version_by_id(self, version_id: str) -> Optional[dict]:
        """SQLite implementation of _get_version_by_id."""
        import sqlite3

        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, profile_data, profile_version, source, created_at
            FROM hierarchy_profiles
            WHERE id = ?
        """, (version_id,))

        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                "id": row[0],
                "data": json.loads(row[1]),
                "version": row[2],
                "source": row[3],
                "created_at": row[4],
            }
        return None

    async def _sqlite_create_backup(self, profile_id: str, reason: str):
        """SQLite implementation of _create_backup."""
        import sqlite3

        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()

        # Get profile data
        cursor.execute(
            "SELECT profile_data FROM hierarchy_profiles WHERE id = ?",
            (profile_id,)
        )
        row = cursor.fetchone()

        if row:
            backup_id = str(uuid.uuid4())
            cursor.execute("""
                INSERT INTO hierarchy_backups (id, profile_id, backup_reason, backup_data)
                VALUES (?, ?, ?, ?)
            """, (backup_id, profile_id, reason, row[0]))
            conn.commit()

        conn.close()

    async def _sqlite_mark_milestone(self, version_id: str):
        """SQLite implementation of mark_as_milestone."""
        import sqlite3

        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE hierarchy_profiles SET is_milestone = 1 WHERE id = ?",
            (version_id,)
        )
        conn.commit()
        conn.close()


# =============================================================================
# SYNC WRAPPER
# =============================================================================

def get_profile_store() -> ProfileStore:
    """Get a profile store instance."""
    return ProfileStore()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    async def main():
        store = ProfileStore()

        if len(sys.argv) > 1:
            cmd = sys.argv[1]

            if cmd == "init":
                success = await store.connect()
                if success:
                    print("Profile store initialized successfully")
                    if store._using_fallback:
                        print(f"Using SQLite fallback: {store.fallback_path}")
                    else:
                        print("Connected to PostgreSQL")
                else:
                    print("Failed to initialize profile store")

            elif cmd == "history":
                await store.connect()
                versions = await store.list_versions()
                print(f"\nProfile History ({len(versions)} versions):")
                print("-" * 60)
                for v in versions:
                    active = " (active)" if v.is_active else ""
                    milestone = " [milestone]" if v.is_milestone else ""
                    print(f"v{v.version} - {v.created_at:%Y-%m-%d %H:%M} - {v.source}{active}{milestone}")
                    if v.notes:
                        print(f"    {v.notes}")

            elif cmd == "current":
                await store.connect()
                profile = await store.get_active_profile()
                if profile:
                    print(f"\nActive Profile (v{profile['version']}):")
                    print(f"Source: {profile['source']}")
                    print(f"Created: {profile['created_at']}")
                    print(f"Data keys: {list(profile['data'].keys())}")
                else:
                    print("No active profile found")

            else:
                print(f"Unknown command: {cmd}")
                print("Usage: python -m context_dna.storage.profile_store [init|history|current]")
        else:
            print("Usage: python -m context_dna.storage.profile_store [init|history|current]")

        await store.close()

    asyncio.run(main())
