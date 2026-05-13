"""
Bidirectional Sync Layer for Context DNA Lite ↔ Heavy Mode

EXTENDS (does not replace) the existing ProfileStore with:
- Mode switch detection
- Sync queue management
- Bidirectional PostgreSQL ↔ SQLite sync
- Conflict resolution
- **CRITICAL: Mid-operation failover when Docker crashes**

Design Principles:
- ADDITIVE ONLY - doesn't modify existing ProfileStore
- BACKWARD COMPATIBLE - works with existing data
- ROLLBACK SAFE - can revert without data loss
- **NEVER LOSE DATA** - write-through + retry ensures durability

Key Safety Features:
1. WRITE-THROUGH: Every Postgres write also goes to SQLite (shadow copy)
2. RETRY-WITH-FALLBACK: If Postgres fails mid-op, automatically use SQLite
3. PERIODIC HEALTH CHECK: Detect Docker down before operations fail
4. LAST KNOWN GOOD: SQLite always has recent snapshot

Usage:
    from context_dna.storage.bidirectional_sync import BidirectionalSyncStore

    store = BidirectionalSyncStore()
    await store.connect()

    # Use normally - failover is automatic
    await store.save_profile(profile_data)  # NEVER loses data

    # Check sync state
    state = await store.get_sync_state()

    # Manual sync if needed
    await store.force_sync()
"""

import json
import sqlite3
import uuid
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

# Python 3.11+ has asyncio.timeout, earlier versions need async_timeout
try:
    from asyncio import timeout as asyncio_timeout
except ImportError:
    # Fallback for Python < 3.11
    class asyncio_timeout:
        def __init__(self, seconds):
            self.seconds = seconds
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

from .profile_store import ProfileStore, ProfileVersion
from .docker_recovery import get_docker_recovery, DockerRecovery

# =============================================================================
# SYNC STATE DATA CLASSES
# =============================================================================

@dataclass
class SyncState:
    """Current mode and sync state - enhanced for crash recovery."""
    current_mode: str  # 'lite' or 'heavy'
    last_mode: Optional[str]
    last_sync_at: Optional[datetime]
    last_crash_at: Optional[datetime]  # Detected unclean shutdown
    postgres_available: bool
    postgres_latency_ms: Optional[int]
    pending_to_postgres: int
    pending_from_postgres: int
    sync_in_progress: bool  # Lock flag
    auto_sync_enabled: bool
    write_through_enabled: bool  # CRITICAL: shadow writes to SQLite
    conflict_resolution: str  # 'latest_wins', 'lite_wins', 'heavy_wins', 'manual'
    consecutive_failures: int
    total_syncs_completed: int

    def to_dict(self) -> dict:
        return {
            'current_mode': self.current_mode,
            'last_mode': self.last_mode,
            'last_sync_at': self.last_sync_at.isoformat() if self.last_sync_at else None,
            'last_crash_at': self.last_crash_at.isoformat() if self.last_crash_at else None,
            'postgres_available': self.postgres_available,
            'postgres_latency_ms': self.postgres_latency_ms,
            'pending_to_postgres': self.pending_to_postgres,
            'pending_from_postgres': self.pending_from_postgres,
            'sync_in_progress': self.sync_in_progress,
            'auto_sync_enabled': self.auto_sync_enabled,
            'write_through_enabled': self.write_through_enabled,
            'conflict_resolution': self.conflict_resolution,
            'consecutive_failures': self.consecutive_failures,
            'total_syncs_completed': self.total_syncs_completed,
        }


@dataclass
class SyncQueueItem:
    """A queued sync operation."""
    id: str
    table_name: str
    record_id: str
    operation: str  # 'INSERT', 'UPDATE', 'DELETE'
    data_json: dict
    target_mode: str  # 'postgres' or 'sqlite'
    created_at: datetime
    status: str  # 'pending', 'syncing', 'completed', 'failed'
    retry_count: int = 0
    last_error: Optional[str] = None


# =============================================================================
# ADDITIONAL SQLITE SCHEMA FOR SYNC
# =============================================================================

SYNC_SCHEMA_SQL = """
-- =============================================================================
-- MODE SYNC STATE - Enhanced for crash recovery & data integrity
-- =============================================================================
-- Singleton table tracking bidirectional sync state.
-- CRITICAL: Enables Context DNA to survive crashes and mode transitions.

CREATE TABLE IF NOT EXISTS mode_sync_state (
    id TEXT PRIMARY KEY DEFAULT 'singleton',

    -- Current operational state
    current_mode TEXT NOT NULL DEFAULT 'lite' CHECK (current_mode IN ('lite','heavy')),
    last_mode TEXT CHECK (last_mode IN ('lite','heavy')),

    -- Timing & health
    last_sync_at TEXT,
    last_crash_at TEXT,                      -- Detected on restart if unclean shutdown
    last_healthy_at TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

    -- Connection state
    postgres_available INTEGER DEFAULT 0,
    postgres_latency_ms INTEGER,

    -- Sync queue state
    pending_to_postgres INTEGER DEFAULT 0,
    pending_from_postgres INTEGER DEFAULT 0,
    sync_in_progress INTEGER DEFAULT 0,      -- Lock flag

    -- Configuration
    auto_sync_enabled INTEGER DEFAULT 1,
    write_through_enabled INTEGER DEFAULT 1, -- Shadow writes to SQLite (CRITICAL SAFETY)
    snapshot_interval_s INTEGER DEFAULT 300,

    -- Conflict handling
    conflict_resolution TEXT DEFAULT 'latest_wins' CHECK (
        conflict_resolution IN ('latest_wins', 'lite_wins', 'heavy_wins', 'manual')
    ),
    last_conflict_at TEXT,
    last_conflict_details TEXT,
    conflicts_total INTEGER DEFAULT 0,
    conflicts_auto_resolved INTEGER DEFAULT 0,

    -- Error tracking
    last_error TEXT,
    last_error_at TEXT,
    consecutive_failures INTEGER DEFAULT 0,

    -- Statistics
    total_syncs_completed INTEGER DEFAULT 0,
    total_items_synced INTEGER DEFAULT 0
);

-- Ensure singleton row exists
INSERT OR IGNORE INTO mode_sync_state (id, current_mode, updated_at)
VALUES ('singleton', 'lite', datetime('now'));

-- =============================================================================
-- SYNC QUEUE - Priority-based with deduplication
-- =============================================================================

CREATE TABLE IF NOT EXISTS sync_queue (
    id TEXT PRIMARY KEY,

    -- What to sync
    table_name TEXT NOT NULL,
    record_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
    data_json TEXT NOT NULL,
    data_hash TEXT,                          -- SHA256 for deduplication

    -- Direction (postgres/sqlite for backward compat, lite/heavy for new code)
    source_mode TEXT DEFAULT 'lite',
    target_mode TEXT NOT NULL CHECK (target_mode IN ('postgres', 'sqlite', 'lite', 'heavy')),

    -- Processing state
    priority INTEGER DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
    status TEXT DEFAULT 'pending' CHECK (
        status IN ('pending', 'syncing', 'processing', 'completed', 'failed', 'abandoned', 'conflict')
    ),
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,

    -- Timing
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    attempted_at TEXT,
    completed_at TEXT,

    -- Error tracking
    last_error TEXT,
    error_history TEXT
);

-- Efficient queue processing
CREATE INDEX IF NOT EXISTS idx_sync_queue_status
    ON sync_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_sync_queue_target
    ON sync_queue(target_mode, status);
CREATE INDEX IF NOT EXISTS idx_sync_queue_processing
    ON sync_queue(status, priority ASC, created_at ASC);

-- =============================================================================
-- SYNC HISTORY - Audit trail for debugging & compliance
-- =============================================================================

CREATE TABLE IF NOT EXISTS sync_history (
    id TEXT PRIMARY KEY,

    -- Sync operation details
    sync_type TEXT NOT NULL,
    direction TEXT NOT NULL,                 -- Backward compat: 'lite_to_heavy', 'heavy_to_lite'
    from_mode TEXT,
    to_mode TEXT,
    trigger_reason TEXT,

    -- Timing
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,

    -- Results
    status TEXT DEFAULT 'in_progress' CHECK (
        status IN ('in_progress', 'completed', 'partial', 'failed', 'cancelled')
    ),
    items_queued INTEGER DEFAULT 0,
    items_synced INTEGER DEFAULT 0,
    items_failed INTEGER DEFAULT 0,
    items_skipped INTEGER DEFAULT 0,

    -- Conflict handling
    conflicts_detected INTEGER DEFAULT 0,
    conflicts_resolved INTEGER DEFAULT 0,

    -- Error tracking
    error_details TEXT,

    -- Context
    machine_id TEXT,
    triggered_by TEXT DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS idx_sync_history_time
    ON sync_history(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_history_status
    ON sync_history(status, started_at DESC);

-- =============================================================================
-- PROJECT BOUNDARIES (Project Boundary Intelligence)
-- =============================================================================
-- Critical for filtering context injections by detected project.

CREATE TABLE IF NOT EXISTS project_boundaries (
    id TEXT PRIMARY KEY,                    -- UUID
    machine_id TEXT NOT NULL,

    -- Project identification
    project_root TEXT NOT NULL,             -- Absolute path to project root
    project_name TEXT NOT NULL,             -- Human-readable name
    project_type TEXT DEFAULT 'standalone', -- 'standalone', 'monorepo', 'workspace', 'submodule'

    -- Detection metadata
    detected_by TEXT NOT NULL,              -- 'git_root', 'package_json', 'workspace_config', 'user_pin'
    detection_confidence REAL DEFAULT 0.8,  -- 0.0 to 1.0
    detection_markers TEXT DEFAULT '[]',    -- JSON

    -- Boundaries
    boundary_paths TEXT DEFAULT '[]',       -- JSON
    excluded_paths TEXT DEFAULT '[]',       -- JSON

    -- Relationships
    parent_project_id TEXT,                 -- For nested projects

    -- User overrides
    is_pinned INTEGER DEFAULT 0,
    pinned_at TEXT,
    pinned_reason TEXT,

    -- Timing
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_validated_at TEXT,

    -- Sync tracking
    synced_to_postgres INTEGER DEFAULT 0,
    synced_at TEXT,
    postgres_id TEXT,

    FOREIGN KEY (parent_project_id) REFERENCES project_boundaries(id),
    UNIQUE(machine_id, project_root)
);

CREATE INDEX IF NOT EXISTS idx_project_boundaries_machine
    ON project_boundaries(machine_id);
CREATE INDEX IF NOT EXISTS idx_project_boundaries_root
    ON project_boundaries(project_root);

-- =============================================================================
-- KEYWORD ↔ PROJECT ASSOCIATIONS
-- =============================================================================

CREATE TABLE IF NOT EXISTS keyword_project_associations (
    id TEXT PRIMARY KEY,                    -- UUID
    machine_id TEXT NOT NULL,

    -- Association
    keyword TEXT NOT NULL,
    project_id TEXT NOT NULL,               -- FK to project_boundaries

    -- Scoring
    association_strength REAL DEFAULT 0.5,
    occurrence_count INTEGER DEFAULT 1,

    -- Learning
    learned_from TEXT NOT NULL,             -- 'file_content', 'commit_message', 'user_tag'
    last_occurrence_at TEXT,

    -- Timing
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

    -- Sync tracking
    synced_to_postgres INTEGER DEFAULT 0,
    synced_at TEXT,
    postgres_id TEXT,

    FOREIGN KEY (project_id) REFERENCES project_boundaries(id) ON DELETE CASCADE,
    UNIQUE(machine_id, keyword, project_id)
);

CREATE INDEX IF NOT EXISTS idx_keyword_assoc_keyword
    ON keyword_project_associations(keyword);
CREATE INDEX IF NOT EXISTS idx_keyword_assoc_project
    ON keyword_project_associations(project_id);

-- =============================================================================
-- BOUNDARY DECISIONS (Learning from user corrections)
-- =============================================================================

CREATE TABLE IF NOT EXISTS boundary_decisions (
    id TEXT PRIMARY KEY,                    -- UUID
    machine_id TEXT NOT NULL,

    -- The decision context
    file_path TEXT NOT NULL,
    detected_project_id TEXT,
    correct_project_id TEXT,

    -- Decision outcome
    was_correct INTEGER NOT NULL,           -- 0 = wrong, 1 = correct
    user_feedback TEXT,

    -- Applied correction
    correction_rule TEXT,                   -- JSON
    correction_applied INTEGER DEFAULT 0,

    -- Timing
    decided_at TEXT DEFAULT CURRENT_TIMESTAMP,

    -- Sync tracking
    synced_to_postgres INTEGER DEFAULT 0,
    synced_at TEXT,
    postgres_id TEXT,

    FOREIGN KEY (detected_project_id) REFERENCES project_boundaries(id) ON DELETE SET NULL,
    FOREIGN KEY (correct_project_id) REFERENCES project_boundaries(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_boundary_decisions_machine
    ON boundary_decisions(machine_id);
CREATE INDEX IF NOT EXISTS idx_boundary_decisions_file
    ON boundary_decisions(file_path);

-- =============================================================================
-- IDE CONFIGURATIONS (Multi-IDE Support)
-- =============================================================================

CREATE TABLE IF NOT EXISTS ide_configurations (
    id TEXT PRIMARY KEY,                    -- UUID
    machine_id TEXT NOT NULL,

    -- IDE identification
    ide_type TEXT NOT NULL CHECK (ide_type IN (
        'cursor', 'vscode', 'windsurf', 'zed', 'jetbrains',
        'neovim', 'emacs', 'sublime', 'xcode', 'other'
    )),
    ide_version TEXT,
    ide_path TEXT,

    -- Configuration
    is_installed INTEGER DEFAULT 0,
    is_configured INTEGER DEFAULT 0,
    is_primary INTEGER DEFAULT 0,
    config_path TEXT,

    -- Integration status
    hook_version TEXT,
    hook_installed_at TEXT,
    last_hook_activity TEXT,

    -- User preferences
    enable_injections INTEGER DEFAULT 1,
    injection_style TEXT DEFAULT 'full',

    -- Timing
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

    -- Sync tracking
    synced_to_postgres INTEGER DEFAULT 0,
    synced_at TEXT,
    postgres_id TEXT,

    UNIQUE(machine_id, ide_type)
);

CREATE INDEX IF NOT EXISTS idx_ide_config_machine
    ON ide_configurations(machine_id);
CREATE INDEX IF NOT EXISTS idx_ide_config_type
    ON ide_configurations(ide_type);

-- =============================================================================
-- DOCKER ENVIRONMENT (Self-learning recovery)
-- =============================================================================
-- Captures Docker config when running, uses cache for recovery when down.
-- Same pattern as ide_configurations - synced to PostgreSQL when available.

CREATE TABLE IF NOT EXISTS docker_environment (
    id TEXT PRIMARY KEY DEFAULT 'singleton',
    machine_id TEXT NOT NULL,

    -- Docker version info (from `docker version`)
    docker_client_version TEXT,          -- e.g., "24.0.7"
    docker_server_version TEXT,          -- e.g., "24.0.7"
    docker_api_version TEXT,             -- e.g., "1.43"

    -- Docker Desktop app location
    docker_app_path TEXT,                -- /Applications/Docker.app
    docker_cli_path TEXT,                -- /usr/local/bin/docker
    docker_socket_path TEXT,             -- ~/.docker/run/docker.sock

    -- Platform info (for multi-machine sync)
    platform TEXT,                       -- darwin, linux, windows
    platform_version TEXT,               -- macOS 14.2, etc.
    architecture TEXT,                   -- arm64, x86_64

    -- Container runtime
    runtime_type TEXT,                   -- 'desktop-linux', 'moby', etc.
    storage_driver TEXT,                 -- 'overlay2', etc.

    -- Resource allocation
    memory_total_bytes INTEGER,          -- Docker's total memory
    cpu_count INTEGER,                   -- Docker's CPU count

    -- Launch method that worked last time
    successful_launch_method TEXT,       -- 'open -a Docker', 'launchctl', etc.
    successful_launch_command TEXT,      -- Full command that worked
    last_successful_launch_at TEXT,
    launch_success_count INTEGER DEFAULT 0,

    -- Recovery attempts tracking
    last_recovery_attempt_at TEXT,
    last_recovery_success INTEGER,       -- 0 = failed, 1 = success
    recovery_attempts_total INTEGER DEFAULT 0,
    recovery_successes_total INTEGER DEFAULT 0,

    -- Health tracking
    last_healthy_at TEXT,
    consecutive_failures INTEGER DEFAULT 0,

    -- Timestamps
    captured_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

    -- Sync tracking (same pattern as ide_configurations)
    synced_to_postgres INTEGER DEFAULT 0,
    synced_at TEXT,
    postgres_id TEXT,

    UNIQUE(machine_id)
);

CREATE INDEX IF NOT EXISTS idx_docker_env_machine
    ON docker_environment(machine_id);
CREATE INDEX IF NOT EXISTS idx_docker_env_platform
    ON docker_environment(platform);

-- =============================================================================
-- APP RECOVERY CONFIGURATIONS (Multi-App Recovery Support)
-- =============================================================================
-- Stores recovery configurations for various apps used by Context DNA.
-- Local LLM can use this data to assist in recovery configuration attempts.

CREATE TABLE IF NOT EXISTS app_recovery_configs (
    id TEXT PRIMARY KEY,
    machine_id TEXT NOT NULL,

    -- App identification
    app_name TEXT NOT NULL,                     -- docker, ollama, postgres, redis, etc.
    app_type TEXT NOT NULL,                     -- container_runtime, llm_runtime, database, etc.
    app_version TEXT,

    -- Discovery info (captured when app is running)
    executable_path TEXT,                       -- /usr/local/bin/ollama
    app_bundle_path TEXT,                       -- /Applications/Ollama.app
    config_path TEXT,                           -- ~/.ollama, /etc/postgresql
    data_path TEXT,                             -- Where app stores data
    socket_path TEXT,                           -- Unix socket if applicable
    pid_file_path TEXT,                         -- PID file location

    -- Launch configuration (learned from successful starts)
    launch_method TEXT,                         -- open, systemctl, launchctl, direct
    launch_command TEXT,                        -- Full command that works
    launch_args TEXT DEFAULT '[]',              -- JSON: Arguments needed
    environment_vars TEXT DEFAULT '{}',         -- JSON: Required env vars
    working_directory TEXT,                     -- Required cwd
    required_ports TEXT DEFAULT '[]',           -- JSON: Ports app needs

    -- Dependencies (for orchestration)
    depends_on TEXT DEFAULT '[]',               -- JSON: Other apps that must run first
    startup_order INTEGER DEFAULT 100,          -- Lower = starts earlier

    -- Health checking
    health_check_method TEXT,                   -- http, tcp, socket, process, command
    health_check_endpoint TEXT,                 -- http://localhost:11434/api/version
    health_check_command TEXT,                  -- Custom health check command
    health_check_interval_ms INTEGER DEFAULT 5000,
    health_check_timeout_ms INTEGER DEFAULT 3000,
    startup_timeout_ms INTEGER DEFAULT 60000,

    -- Recovery tracking
    last_healthy_at TEXT,
    last_recovery_at TEXT,
    recovery_attempts INTEGER DEFAULT 0,
    recovery_successes INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0,
    last_error TEXT,
    last_error_at TEXT,

    -- Learning from recovery
    successful_recovery_commands TEXT DEFAULT '[]',  -- JSON: Commands that worked
    failed_recovery_commands TEXT DEFAULT '[]',      -- JSON: Commands that failed
    recovery_notes TEXT,                             -- Human/LLM notes

    -- Feature flags
    auto_recovery_enabled INTEGER DEFAULT 1,    -- Boolean
    is_critical INTEGER DEFAULT 0,              -- Must be running for heavy mode
    is_optional INTEGER DEFAULT 0,              -- Nice to have but not required

    -- Timing
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

    -- Sync tracking
    synced_to_postgres INTEGER DEFAULT 0,
    synced_at TEXT,
    postgres_id TEXT,

    UNIQUE(machine_id, app_name)
);

CREATE INDEX IF NOT EXISTS idx_app_recovery_machine
    ON app_recovery_configs(machine_id);
CREATE INDEX IF NOT EXISTS idx_app_recovery_type
    ON app_recovery_configs(app_type);
CREATE INDEX IF NOT EXISTS idx_app_recovery_critical
    ON app_recovery_configs(is_critical);
CREATE INDEX IF NOT EXISTS idx_app_recovery_order
    ON app_recovery_configs(startup_order);
"""


# =============================================================================
# BIDIRECTIONAL SYNC STORE
# =============================================================================

class BidirectionalSyncStore(ProfileStore):
    """
    Extends ProfileStore with bidirectional lite ↔ heavy sync.

    CRITICAL SAFETY FEATURES (handles Docker going offline):

    1. WRITE-THROUGH MODE:
       Every Postgres write ALSO writes to SQLite as shadow copy.
       If Docker dies, SQLite has the data.

    2. RETRY-WITH-FALLBACK:
       If Postgres operation fails mid-execution:
       - Catch the exception
       - Automatically retry with SQLite
       - Queue failed op for later Postgres sync

    3. HEALTH CHECK BEFORE WRITE:
       Quick ping before expensive operations.
       Detects Docker down BEFORE data loss can occur.

    4. PERIODIC SNAPSHOT:
       Background task snapshots Postgres → SQLite every N minutes.
       Ensures SQLite is never too stale.

    Seamless mode switching:
    - When Postgres available → heavy mode (full features)
    - When Postgres unavailable → lite mode (SQLite)
    - Changes in one mode sync to other when connection restored
    - NO DATA LOSS even if Docker crashes mid-operation
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sync_initialized = False
        self._mode_check_interval = 30  # seconds
        self._last_mode_check: Optional[datetime] = None
        self._write_through_enabled = True  # Always shadow-write to SQLite
        self._health_check_timeout = 2.0  # seconds - fast fail
        self._last_health_check: Optional[datetime] = None
        self._postgres_healthy = None  # None = unknown, True/False = known state

        # Docker auto-launch recovery
        self._docker_recovery = get_docker_recovery()
        self._auto_launch_docker = True  # Enable auto-launch in heavy mode
        self._docker_launch_timeout = 60.0  # Max seconds to wait for Docker

    async def connect(self) -> bool:
        """Connect with sync schema initialization."""
        result = await super().connect()
        await self._init_sync_schema()
        await self._detect_and_handle_mode_switch()
        return result

    # =========================================================================
    # SYNC SCHEMA INITIALIZATION
    # =========================================================================

    async def _init_sync_schema(self):
        """Initialize sync-specific tables in SQLite."""
        if self._sync_initialized:
            return

        conn = sqlite3.connect(str(self.fallback_path))
        conn.executescript(SYNC_SCHEMA_SQL)
        conn.close()
        self._sync_initialized = True

    # =========================================================================
    # MODE DETECTION & SWITCHING
    # =========================================================================

    async def get_sync_state(self) -> SyncState:
        """Get current sync state with enhanced crash recovery info."""
        conn = sqlite3.connect(str(self.fallback_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM mode_sync_state WHERE id = 'singleton'")
        row = cursor.fetchone()
        conn.close()

        # Helper to safely get optional fields (backward compat with older schemas)
        def get_field(name, default=None):
            try:
                return row[name] if row and name in row.keys() else default
            except (KeyError, IndexError):
                return default

        if not row:
            return SyncState(
                current_mode='lite',
                last_mode=None,
                last_sync_at=None,
                last_crash_at=None,
                postgres_available=False,
                postgres_latency_ms=None,
                pending_to_postgres=0,
                pending_from_postgres=0,
                sync_in_progress=False,
                auto_sync_enabled=True,
                write_through_enabled=True,
                conflict_resolution='latest_wins',
                consecutive_failures=0,
                total_syncs_completed=0,
            )

        return SyncState(
            current_mode=row['current_mode'],
            last_mode=get_field('last_mode'),
            last_sync_at=datetime.fromisoformat(row['last_sync_at']) if get_field('last_sync_at') else None,
            last_crash_at=datetime.fromisoformat(get_field('last_crash_at')) if get_field('last_crash_at') else None,
            postgres_available=bool(get_field('postgres_available', 0)),
            postgres_latency_ms=get_field('postgres_latency_ms'),
            pending_to_postgres=get_field('pending_to_postgres', 0) or 0,
            pending_from_postgres=get_field('pending_from_postgres', 0) or 0,
            sync_in_progress=bool(get_field('sync_in_progress', 0)),
            auto_sync_enabled=bool(get_field('auto_sync_enabled', 1)),
            write_through_enabled=bool(get_field('write_through_enabled', 1)),
            conflict_resolution=get_field('conflict_resolution', 'latest_wins') or 'latest_wins',
            consecutive_failures=get_field('consecutive_failures', 0) or 0,
            total_syncs_completed=get_field('total_syncs_completed', 0) or 0,
        )

    async def _update_sync_state(self, **kwargs):
        """Update sync state fields."""
        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()

        # Build update query dynamically
        fields = []
        values = []
        for key, value in kwargs.items():
            fields.append(f"{key} = ?")
            if isinstance(value, bool):
                values.append(1 if value else 0)
            elif isinstance(value, datetime):
                values.append(value.isoformat())
            else:
                values.append(value)

        fields.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())

        cursor.execute(
            f"UPDATE mode_sync_state SET {', '.join(fields)} WHERE id = 'singleton'",
            values
        )
        conn.commit()
        conn.close()

    async def _detect_and_handle_mode_switch(self):
        """
        Detect if mode has changed and handle sync.

        ENHANCED: If previous mode was 'heavy' but Postgres is unavailable,
        attempts Docker auto-launch recovery before falling back to lite mode.
        """
        state = await self.get_sync_state()

        # Check Postgres availability
        postgres_now_available = not self._using_fallback

        # CRASH RECOVERY: If we were in heavy mode but Postgres isn't available,
        # attempt to auto-launch Docker before giving up
        if not postgres_now_available and state.last_mode == 'heavy':
            print("🔄 Heavy mode crash detected - attempting Docker recovery...")

            # Record crash detection
            await self._update_sync_state(last_crash_at=datetime.utcnow())

            # Attempt Docker recovery
            if await self._ensure_docker_for_heavy_mode():
                # Docker is up, try reconnecting to Postgres
                try:
                    await super().connect()
                    postgres_now_available = not self._using_fallback
                    if postgres_now_available:
                        print("✓ Recovered to heavy mode after Docker restart")
                except Exception as e:
                    print(f"Postgres reconnection failed after Docker recovery: {e}")

        # Determine current mode
        new_mode = 'heavy' if postgres_now_available else 'lite'

        # Detect mode switch
        if state.current_mode != new_mode:
            await self._handle_mode_switch(state.current_mode, new_mode)

        # Update state
        await self._update_sync_state(
            current_mode=new_mode,
            postgres_available=postgres_now_available,
        )

    async def _handle_mode_switch(self, old_mode: str, new_mode: str):
        """Handle mode switch with data sync."""
        print(f"MODE SWITCH: {old_mode} → {new_mode}")

        if old_mode == 'lite' and new_mode == 'heavy':
            # Switching TO heavy: sync SQLite → Postgres
            await self._sync_lite_to_heavy()
        elif old_mode == 'heavy' and new_mode == 'lite':
            # Switching TO lite: snapshot Postgres → SQLite
            await self._sync_heavy_to_lite()

        # Update state
        await self._update_sync_state(
            last_mode=old_mode,
            last_sync_at=datetime.utcnow(),
        )

    # =========================================================================
    # SYNC OPERATIONS
    # =========================================================================

    async def _sync_lite_to_heavy(self):
        """Sync queued SQLite changes to PostgreSQL."""
        queue = await self._get_pending_queue('postgres')
        if not queue:
            print("No pending changes to sync to PostgreSQL")
            return

        print(f"Syncing {len(queue)} items to PostgreSQL...")

        sync_id = str(uuid.uuid4())
        await self._record_sync_start(sync_id, 'queue_process', 'lite_to_heavy')

        success_count = 0
        fail_count = 0

        for item in queue:
            try:
                await self._sync_item_to_postgres(item)
                await self._mark_queue_item_completed(item.id)
                success_count += 1
            except Exception as e:
                await self._mark_queue_item_failed(item.id, str(e))
                fail_count += 1

        await self._record_sync_complete(sync_id, success_count, fail_count)
        await self._update_sync_state(pending_to_postgres=fail_count)

        print(f"Sync complete: {success_count} success, {fail_count} failed")

    async def _sync_heavy_to_lite(self) -> dict:
        """Snapshot PostgreSQL state to SQLite for offline use.

        Returns:
            dict with 'items_synced' count
        """
        if self._using_fallback:
            print("Cannot sync from Postgres - not connected")
            return {'items_synced': 0}

        print("Snapshotting PostgreSQL → SQLite...")

        sync_id = str(uuid.uuid4())
        await self._record_sync_start(sync_id, 'snapshot', 'heavy_to_lite')

        try:
            # Get active profile from Postgres
            profile = await self.get_active_profile()

            if profile:
                # Save to SQLite (using parent's fallback methods)
                await self._sqlite_save_profile(
                    version_id=str(uuid.uuid4()),
                    profile_data=profile.get('data', profile),
                    source='postgres_snapshot',
                    notes='Snapshot from PostgreSQL for lite mode',
                    created_by='system',
                    next_version=await self._sqlite_get_next_version(),
                    current=None,
                )
                await self._record_sync_complete(sync_id, 1, 0)
                print("Snapshot complete")
                return {'items_synced': 1}
            else:
                await self._record_sync_complete(sync_id, 0, 0)
                print("No profile to snapshot")
                return {'items_synced': 0}

        except Exception as e:
            await self._record_sync_complete(sync_id, 0, 1, str(e))
            print(f"Snapshot failed: {e}")
            return {'items_synced': 0}

    async def _sync_item_to_postgres(self, item: SyncQueueItem):
        """Sync a single queue item to PostgreSQL."""
        if self._using_fallback:
            raise Exception("PostgreSQL not available")

        async with self._pool.acquire() as conn:
            if item.table_name == 'hierarchy_profiles':
                if item.operation in ('INSERT', 'UPDATE'):
                    # Use parent's save method
                    await self.save_profile(
                        profile_data=item.data_json,
                        source='sync_from_lite',
                        notes=f'Synced from lite mode (queue: {item.id})',
                    )
                elif item.operation == 'DELETE':
                    # Mark as inactive rather than delete
                    await conn.execute("""
                        UPDATE hierarchy_profiles
                        SET is_active = FALSE
                        WHERE id = $1
                    """, uuid.UUID(item.record_id))

    # =========================================================================
    # QUEUE MANAGEMENT
    # =========================================================================

    async def queue_for_sync(self, table: str, record_id: str, operation: str, data: dict):
        """Queue a change for sync to the other mode."""
        state = await self.get_sync_state()
        target = 'postgres' if state.current_mode == 'lite' else 'sqlite'

        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO sync_queue (id, table_name, record_id, operation, data_json, target_mode)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(uuid.uuid4()),
            table,
            record_id,
            operation,
            json.dumps(data),
            target,
        ))

        conn.commit()
        conn.close()

        # Update pending count
        if target == 'postgres':
            await self._update_sync_state(
                pending_to_postgres=state.pending_to_postgres + 1
            )

    async def _get_pending_queue(self, target_mode: str) -> List[SyncQueueItem]:
        """Get pending items for a target mode."""
        conn = sqlite3.connect(str(self.fallback_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM sync_queue
            WHERE target_mode = ? AND status = 'pending'
            ORDER BY created_at
        """, (target_mode,))

        rows = cursor.fetchall()
        conn.close()

        return [
            SyncQueueItem(
                id=row['id'],
                table_name=row['table_name'],
                record_id=row['record_id'],
                operation=row['operation'],
                data_json=json.loads(row['data_json']),
                target_mode=row['target_mode'],
                created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.utcnow(),
                status=row['status'],
                retry_count=row['retry_count'] or 0,
                last_error=row['last_error'],
            )
            for row in rows
        ]

    async def _mark_queue_item_completed(self, item_id: str):
        """Mark queue item as completed."""
        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE sync_queue SET status = 'completed' WHERE id = ?",
            (item_id,)
        )
        conn.commit()
        conn.close()

    async def _mark_queue_item_failed(self, item_id: str, error: str):
        """Mark queue item as failed."""
        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE sync_queue SET status = 'failed', last_error = ?, retry_count = retry_count + 1 WHERE id = ?",
            (error, item_id)
        )
        conn.commit()
        conn.close()

    # =========================================================================
    # SYNC HISTORY
    # =========================================================================

    async def _record_sync_start(self, sync_id: str, sync_type: str, direction: str):
        """Record start of a sync operation."""
        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sync_history (id, sync_type, direction, started_at)
            VALUES (?, ?, ?, ?)
        """, (sync_id, sync_type, direction, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()

    async def _record_sync_complete(self, sync_id: str, success: int, failed: int, error: str = None):
        """Record completion of a sync operation."""
        conn = sqlite3.connect(str(self.fallback_path))
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE sync_history
            SET completed_at = ?, items_synced = ?, items_failed = ?, error_details = ?
            WHERE id = ?
        """, (datetime.utcnow().isoformat(), success, failed, error, sync_id))
        conn.commit()
        conn.close()

    # =========================================================================
    # HEALTH CHECK (Detect Docker down BEFORE operations fail)
    # =========================================================================

    async def _quick_health_check(self) -> bool:
        """
        Fast health check - detect if Postgres/Docker is available.
        Returns True if healthy, False if down.

        This is called BEFORE expensive operations to fail fast.
        """
        if self._using_fallback:
            return False  # Already in fallback mode

        if not self._pool:
            return False

        try:
            async with asyncio_timeout(self._health_check_timeout):
                async with self._pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
            self._postgres_healthy = True
            self._last_health_check = datetime.utcnow()
            return True
        except Exception as e:
            print(f"Health check failed (Docker may be down): {e}")
            self._postgres_healthy = False
            return False

    async def _ensure_docker_for_heavy_mode(self) -> bool:
        """
        Ensure Docker is running when heavy mode is requested/expected.

        AUTO-RECOVERY FLOW:
        1. Check if Docker daemon is running
        2. If not, and auto_launch_docker is enabled:
           a. Launch Docker Desktop
           b. Wait for daemon to be ready
        3. Return whether Docker is now available

        This is called when:
        - System was last in heavy mode and restarting
        - User explicitly requests heavy mode
        - Sync state indicates heavy mode preference
        """
        if not self._auto_launch_docker:
            return self._docker_recovery.is_docker_daemon_running()

        # Check current state first (fast path)
        if self._docker_recovery.is_docker_daemon_running():
            return True

        # Docker not running - attempt auto-recovery
        print("🐳 Docker not running - attempting auto-launch for heavy mode...")

        try:
            is_ready = await self._docker_recovery.ensure_docker_ready(
                timeout=self._docker_launch_timeout,
                auto_launch=True
            )

            if is_ready:
                print("✓ Docker recovered - heavy mode available")
                return True
            else:
                print("✗ Docker auto-launch failed - falling back to lite mode")
                return False

        except Exception as e:
            print(f"✗ Docker recovery error: {e}")
            return False

    async def _should_use_postgres(self) -> bool:
        """
        Determine if we should attempt Postgres or go straight to SQLite.
        Uses cached health check to avoid repeated slow failures.

        ENHANCED: Now includes Docker auto-launch for heavy mode recovery.
        """
        # If already in fallback mode, check if we should try to recover
        if self._using_fallback:
            # Check if we were in heavy mode before (crash recovery scenario)
            state = await self.get_sync_state()
            if state.last_mode == 'heavy' or state.current_mode == 'heavy':
                # Attempt Docker recovery
                if await self._ensure_docker_for_heavy_mode():
                    # Docker is back! Try to reconnect
                    try:
                        await super().connect()
                        if not self._using_fallback:
                            print("✓ Reconnected to PostgreSQL after Docker recovery")
                            return True
                    except Exception as e:
                        print(f"Failed to reconnect after Docker recovery: {e}")
            return False

        # If we recently checked and it was unhealthy, don't retry yet
        if self._last_health_check and self._postgres_healthy == False:
            time_since_check = (datetime.utcnow() - self._last_health_check).total_seconds()
            if time_since_check < self._mode_check_interval:
                return False  # Still in cooldown, use SQLite

        # Do a quick health check
        return await self._quick_health_check()

    async def recover_to_heavy_mode(self) -> bool:
        """
        Explicitly attempt to recover to heavy mode.

        Call this when you want to force an attempt to restore
        heavy mode (PostgreSQL + Docker) operation.

        Returns True if heavy mode is now available, False otherwise.
        """
        print("Attempting heavy mode recovery...")

        # Step 1: Ensure Docker is running
        if not await self._ensure_docker_for_heavy_mode():
            print("Cannot recover: Docker not available")
            return False

        # Step 2: Reconnect to PostgreSQL
        try:
            await super().connect()

            if self._using_fallback:
                print("Cannot recover: PostgreSQL connection failed")
                return False

            # Step 3: Handle mode switch
            await self._detect_and_handle_mode_switch()

            state = await self.get_sync_state()
            if state.current_mode == 'heavy':
                print("✓ Heavy mode recovered successfully")
                return True
            else:
                print("Mode switch did not complete to heavy")
                return False

        except Exception as e:
            print(f"Heavy mode recovery failed: {e}")
            return False

    # =========================================================================
    # WRITE-THROUGH (Shadow copy to SQLite for safety)
    # =========================================================================

    async def _write_through_to_sqlite(self, profile_data: dict, source: str,
                                       notes: str, created_by: str) -> str:
        """
        Write to SQLite as shadow copy (for disaster recovery).
        Called alongside Postgres writes to ensure data durability.
        """
        version_id = str(uuid.uuid4())
        next_version = await self._sqlite_get_next_version()

        await self._sqlite_save_profile(
            version_id=version_id,
            profile_data=profile_data,
            source=f"{source}_shadow",  # Mark as shadow copy
            notes=notes,
            created_by=created_by,
            next_version=next_version,
            current=None,  # Don't backup for shadow writes
        )
        return version_id

    # =========================================================================
    # OVERRIDE SAVE WITH FAILOVER (CRITICAL FOR DOCKER CRASHES)
    # =========================================================================

    async def save_profile(self, profile_data: dict, source: str = "user_update",
                          notes: str = None, created_by: str = None) -> str:
        """
        Save profile with automatic failover if Docker/Postgres crashes.

        SAFETY GUARANTEES:
        1. If Postgres healthy → write to Postgres + shadow to SQLite
        2. If Postgres fails mid-operation → retry with SQLite + queue for sync
        3. If Postgres unavailable → write to SQLite + queue for sync
        4. NEVER returns without saving data somewhere
        """
        # Ensure sync schema is initialized
        if not self._sync_initialized:
            await self._init_sync_schema()

        # Check if we should attempt Postgres
        use_postgres = await self._should_use_postgres()

        if use_postgres:
            # ATTEMPT 1: Try Postgres with write-through
            try:
                # Write to Postgres (parent method)
                version_id = await super().save_profile(profile_data, source, notes, created_by)

                # Shadow write to SQLite (for disaster recovery)
                if self._write_through_enabled:
                    try:
                        await self._write_through_to_sqlite(profile_data, source, notes, created_by)
                    except Exception as shadow_err:
                        print(f"Shadow write failed (non-fatal): {shadow_err}")

                # Update sync state
                await self._update_sync_state(
                    current_mode='heavy',
                    postgres_available=True,
                )

                return version_id

            except Exception as postgres_err:
                # POSTGRES FAILED MID-OPERATION
                print(f"Postgres failed mid-operation: {postgres_err}")
                print("Falling back to SQLite...")

                # Mark Postgres as unhealthy
                self._postgres_healthy = False
                self._last_health_check = datetime.utcnow()

                # Fall through to SQLite save below

        # ATTEMPT 2: Save to SQLite (either primary or fallback)
        version_id = str(uuid.uuid4())
        next_version = await self._sqlite_get_next_version()

        # Get current for backup
        current = await self._sqlite_get_active_profile()

        await self._sqlite_save_profile(
            version_id=version_id,
            profile_data=profile_data,
            source=source,
            notes=notes,
            created_by=created_by,
            next_version=next_version,
            current=current,
        )

        # Queue for Postgres sync when it comes back
        await self.queue_for_sync(
            table='hierarchy_profiles',
            record_id=version_id,
            operation='INSERT',
            data=profile_data,
        )

        # Update sync state
        await self._update_sync_state(
            current_mode='lite',
            postgres_available=False,
            pending_to_postgres=(await self.get_sync_state()).pending_to_postgres + 1,
        )

        print(f"Saved to SQLite (queued for Postgres sync): {version_id}")
        return version_id

    # =========================================================================
    # PERIODIC SNAPSHOT (Keep SQLite fresh for Docker crash recovery)
    # =========================================================================

    async def start_background_sync(self, interval_seconds: int = 300):
        """
        Start background task that periodically snapshots Postgres → SQLite.

        This ensures SQLite is never more than `interval_seconds` stale,
        minimizing data loss if Docker crashes unexpectedly.

        Args:
            interval_seconds: How often to snapshot (default: 5 minutes)
        """
        async def snapshot_loop():
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    if await self._should_use_postgres():
                        await self._periodic_snapshot()
                except Exception as e:
                    print(f"Periodic snapshot failed: {e}")

        # Start as background task
        asyncio.create_task(snapshot_loop())
        print(f"Started background sync (every {interval_seconds}s)")

    async def _periodic_snapshot(self):
        """Snapshot current Postgres state to SQLite."""
        if self._using_fallback:
            return  # Nothing to snapshot

        try:
            # Get active profile from Postgres
            profile = await super().get_active_profile()
            if not profile:
                return

            # Check if SQLite is stale
            sqlite_profile = await self._sqlite_get_active_profile()

            # Compare versions
            pg_version = profile.get('version', 0)
            sqlite_version = sqlite_profile.get('version', 0) if sqlite_profile else 0

            if pg_version > sqlite_version:
                # SQLite is stale, update it
                await self._write_through_to_sqlite(
                    profile_data=profile.get('data', profile),
                    source='periodic_snapshot',
                    notes=f'Snapshot from Postgres v{pg_version}',
                    created_by='system',
                )
                print(f"Periodic snapshot: Postgres v{pg_version} → SQLite")

        except Exception as e:
            print(f"Periodic snapshot error: {e}")

    # =========================================================================
    # RECOVERY: Detect Docker restart and resync
    # =========================================================================

    async def check_and_recover(self) -> dict:
        """
        Check if Postgres came back online and recover if needed.

        Call this periodically (e.g., on each operation) to detect
        when Docker restarts and sync queued changes.

        Returns dict with recovery status.
        """
        was_postgres_healthy = self._postgres_healthy
        is_postgres_healthy = await self._quick_health_check()

        result = {
            'postgres_recovered': False,
            'items_synced': 0,
            'items_failed': 0,
        }

        # Postgres just came back online!
        if is_postgres_healthy and was_postgres_healthy == False:
            print("Postgres recovered! Syncing queued changes...")
            result['postgres_recovered'] = True

            # Process the sync queue
            queue = await self._get_pending_queue('postgres')
            for item in queue:
                try:
                    await self._sync_item_to_postgres(item)
                    await self._mark_queue_item_completed(item.id)
                    result['items_synced'] += 1
                except Exception as e:
                    await self._mark_queue_item_failed(item.id, str(e))
                    result['items_failed'] += 1

            # Update sync state
            await self._update_sync_state(
                current_mode='heavy',
                postgres_available=True,
                pending_to_postgres=result['items_failed'],
                last_sync_at=datetime.utcnow(),
            )

        # Postgres just went down - snapshot to SQLite
        elif not is_postgres_healthy and was_postgres_healthy == True:
            print("Postgres went down! Snapshotting to SQLite...")
            snapshot = await self._sync_heavy_to_lite()
            result['items_synced_from_postgres'] = snapshot.get('items_synced', 0)

            await self._update_sync_state(
                current_mode='lite',
                postgres_available=False,
                last_sync_at=datetime.utcnow(),
            )

        return result

    # =========================================================================
    # MANUAL SYNC CONTROLS
    # =========================================================================

    async def force_sync(self) -> dict:
        """Force immediate sync in both directions."""
        result = {
            'lite_to_heavy': {'success': 0, 'failed': 0},
            'heavy_to_lite': {'success': 0, 'failed': 0},
        }

        # Sync lite → heavy first (process queue)
        queue = await self._get_pending_queue('postgres')
        for item in queue:
            try:
                await self._sync_item_to_postgres(item)
                await self._mark_queue_item_completed(item.id)
                result['lite_to_heavy']['success'] += 1
            except Exception as e:
                print(f"[WARN] Failed to sync item {item.id} to postgres: {e}")
                result['lite_to_heavy']['failed'] += 1

        # Then snapshot heavy → lite
        try:
            await self._sync_heavy_to_lite()
            result['heavy_to_lite']['success'] = 1
        except Exception as e:
            print(f"[WARN] Failed to sync heavy to lite: {e}")
            result['heavy_to_lite']['failed'] = 1

        return result

    async def get_sync_history(self, limit: int = 10) -> List[dict]:
        """Get recent sync history."""
        conn = sqlite3.connect(str(self.fallback_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM sync_history
            ORDER BY started_at DESC
            LIMIT ?
        """, (limit,))

        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]


# =============================================================================
# FACTORY FUNCTION
# =============================================================================

def get_sync_store() -> BidirectionalSyncStore:
    """Get a bidirectional sync store instance."""
    return BidirectionalSyncStore()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    async def main():
        store = BidirectionalSyncStore()

        if len(sys.argv) > 1:
            cmd = sys.argv[1]

            if cmd == "status":
                await store.connect()
                state = await store.get_sync_state()

                # Include Docker status
                docker_status = store._docker_recovery.get_status()

                print("\n=== Sync State ===")
                print(json.dumps(state.to_dict(), indent=2))

                print("\n=== Docker Status ===")
                print(f"  Platform: {docker_status['platform']}")
                print(f"  Daemon running: {'✓' if docker_status['daemon_running'] else '✗'}")
                print(f"  App running: {'✓' if docker_status['app_running'] else '✗'}")

            elif cmd == "history":
                await store.connect()
                history = await store.get_sync_history()
                print("\n=== Sync History ===")
                for h in history:
                    print(f"{h['started_at']}: {h['direction']} - {h['items_synced']} synced, {h['items_failed']} failed")

            elif cmd == "sync":
                await store.connect()
                result = await store.force_sync()
                print("\n=== Force Sync Result ===")
                print(json.dumps(result, indent=2))

            elif cmd == "docker":
                # Docker-specific commands
                docker = get_docker_recovery()
                status = docker.get_status()
                print("\n=== Docker Status ===")
                print(f"Platform: {status['platform']}")
                print(f"Daemon running: {'✓' if status['daemon_running'] else '✗'}")
                print(f"App running: {'✓' if status['app_running'] else '✗'}")
                print(f"Last launch attempt: {status['last_launch_attempt'] or 'Never'}")

            elif cmd == "docker-launch":
                # Launch Docker Desktop
                docker = get_docker_recovery()
                print("Launching Docker Desktop...")
                if docker.launch_docker_desktop():
                    print("✓ Launch initiated - waiting for daemon...")
                    ready = await docker.wait_for_docker_ready(timeout=60.0)
                    print("✓ Docker ready" if ready else "✗ Timeout waiting for Docker")
                else:
                    print("✗ Launch failed")

            elif cmd == "recover":
                # Attempt full heavy mode recovery
                print("Attempting heavy mode recovery...")
                await store.connect()
                success = await store.recover_to_heavy_mode()
                if success:
                    print("✓ Heavy mode recovery successful")
                else:
                    print("✗ Recovery failed - operating in lite mode")

            else:
                print(f"Unknown command: {cmd}")
                print("Usage: python -m context_dna.storage.bidirectional_sync [status|history|sync|docker|docker-launch|recover]")
        else:
            print("Usage: python -m context_dna.storage.bidirectional_sync [status|history|sync|docker|docker-launch|recover]")
            print("\nCommands:")
            print("  status        - Show sync state and Docker status")
            print("  history       - Show sync history")
            print("  sync          - Force sync between modes")
            print("  docker        - Show Docker status")
            print("  docker-launch - Launch Docker Desktop and wait for ready")
            print("  recover       - Attempt to recover to heavy mode")

        await store.close()

    asyncio.run(main())
