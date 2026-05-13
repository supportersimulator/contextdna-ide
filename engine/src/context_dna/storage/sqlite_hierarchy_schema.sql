-- ============================================================================
-- Context DNA SQLite Hierarchy Schema v1.0
-- ============================================================================
-- PURPOSE: Mirror PostgreSQL hierarchy tables for Lite mode with IDENTICAL
--          semantics, enabling seamless bidirectional sync.
--
-- DESIGN PRINCIPLES:
-- 1. Same table names, same column names, same constraints as Postgres
-- 2. JSON stored as TEXT (SQLite doesn't have JSONB)
-- 3. UUID stored as TEXT (36-char format: 8-4-4-4-12)
-- 4. Timestamps stored as TEXT in ISO8601 format
-- 5. Functions reimplemented in Python (SQLite has no stored procedures)
--
-- COMPATIBILITY:
-- - Postgres UUIDs → SQLite TEXT (hex-dash format)
-- - Postgres JSONB → SQLite TEXT (JSON string)
-- - Postgres TIMESTAMPTZ → SQLite TEXT (ISO8601 with Z suffix)
-- - Postgres SERIAL → SQLite INTEGER PRIMARY KEY AUTOINCREMENT
-- ============================================================================

PRAGMA journal_mode=WAL;           -- Concurrent reads during writes
PRAGMA synchronous=NORMAL;         -- Good balance of safety/speed
PRAGMA foreign_keys=ON;            -- Enforce referential integrity
PRAGMA busy_timeout=5000;          -- Wait 5s for locks before failing

-- ============================================================================
-- PART 1: HIERARCHY PATTERNS (Shared vocabulary of project structures)
-- ============================================================================
-- Global patterns learned across all projects: superrepo, monorepo, polyrepo, etc.

CREATE TABLE IF NOT EXISTS cd_hierarchy_patterns (
    id TEXT PRIMARY KEY,                    -- UUID as TEXT
    name TEXT NOT NULL UNIQUE,              -- e.g., 'superrepo', 'polyrepo', 'monorepo'
    description TEXT,
    detection_rules TEXT NOT NULL DEFAULT '{}',  -- JSON: indicators, markers, threshold
    default_scoping TEXT NOT NULL DEFAULT '{}',  -- JSON: scope mapping rules
    usage_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now') || 'Z'),
    updated_at TEXT DEFAULT (datetime('now') || 'Z')
);

CREATE INDEX IF NOT EXISTS idx_cd_hierarchy_patterns_name
    ON cd_hierarchy_patterns(name);

-- ============================================================================
-- PART 2: HIERARCHY PROFILES (Per-machine versioned profiles)
-- ============================================================================
-- This is the CORE table for adaptive hierarchy learning.
-- Each machine can have multiple versions; only one is active.

CREATE TABLE IF NOT EXISTS hierarchy_profiles (
    id TEXT PRIMARY KEY,                    -- UUID
    machine_id TEXT NOT NULL,               -- Identifies which machine/installation
    profile_version INTEGER NOT NULL,       -- Auto-incrementing per machine
    profile_data TEXT NOT NULL,             -- JSON: full HierarchyProfile serialized
    source TEXT NOT NULL DEFAULT 'auto_scan', -- 'auto_scan', 'user_input', 'merge', 'rollback', 'migration'
    parent_version_id TEXT,                 -- Previous version (for rollback chain)
    created_at TEXT DEFAULT (datetime('now') || 'Z'),
    created_by TEXT DEFAULT 'system',       -- User identifier or 'system'
    notes TEXT,                             -- Why this version was created
    is_active INTEGER DEFAULT 1,            -- 0 or 1 (SQLite doesn't have BOOLEAN)

    -- Sync tracking for bidirectional mode switching
    synced_to_postgres INTEGER DEFAULT 0,   -- Has been synced to heavy mode
    synced_at TEXT,                         -- When last synced
    postgres_id TEXT,                       -- Corresponding Postgres UUID (if synced)

    FOREIGN KEY (parent_version_id) REFERENCES hierarchy_profiles(id)
);

-- Enforce unique active profile per machine
CREATE UNIQUE INDEX IF NOT EXISTS idx_hierarchy_profiles_active_unique
    ON hierarchy_profiles(machine_id) WHERE is_active = 1;

-- Fast lookups
CREATE INDEX IF NOT EXISTS idx_hierarchy_profiles_machine
    ON hierarchy_profiles(machine_id, is_active);
CREATE INDEX IF NOT EXISTS idx_hierarchy_profiles_version
    ON hierarchy_profiles(machine_id, profile_version DESC);
CREATE INDEX IF NOT EXISTS idx_hierarchy_profiles_sync
    ON hierarchy_profiles(synced_to_postgres) WHERE synced_to_postgres = 0;

-- ============================================================================
-- PART 3: HIERARCHY BACKUPS (Auto-backup before changes)
-- ============================================================================
-- Every profile change creates a backup for safety. 30-day auto-cleanup.

CREATE TABLE IF NOT EXISTS hierarchy_backups (
    id TEXT PRIMARY KEY,                    -- UUID
    profile_id TEXT NOT NULL,               -- Which profile was backed up
    backup_reason TEXT NOT NULL,            -- 'pre_restructure', 'pre_rollback', 'pre_migration', etc.
    backup_data TEXT NOT NULL,              -- JSON: full profile snapshot
    created_at TEXT DEFAULT (datetime('now') || 'Z'),
    expires_at TEXT,                        -- Auto-cleanup after this time

    FOREIGN KEY (profile_id) REFERENCES hierarchy_profiles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_hierarchy_backups_profile
    ON hierarchy_backups(profile_id);
CREATE INDEX IF NOT EXISTS idx_hierarchy_backups_expires
    ON hierarchy_backups(expires_at);

-- ============================================================================
-- PART 4: PROJECT HIERARCHIES (Per-project detection results)
-- ============================================================================
-- Stores what Context DNA detected about each project's structure.

CREATE TABLE IF NOT EXISTS cd_project_hierarchies (
    id TEXT PRIMARY KEY,                    -- UUID
    project_path TEXT NOT NULL UNIQUE,      -- Absolute path to project root
    detected_pattern_id TEXT,               -- FK to cd_hierarchy_patterns
    confidence REAL DEFAULT 0.0,            -- 0.0 to 1.0
    structure_map TEXT NOT NULL DEFAULT '{}', -- JSON: actual structure detected
    user_overrides TEXT DEFAULT '{}',       -- JSON: manual corrections by user
    detected_at TEXT DEFAULT (datetime('now') || 'Z'),
    last_scan_at TEXT DEFAULT (datetime('now') || 'Z'),

    FOREIGN KEY (detected_pattern_id) REFERENCES cd_hierarchy_patterns(id)
);

CREATE INDEX IF NOT EXISTS idx_cd_project_hierarchies_pattern
    ON cd_project_hierarchies(detected_pattern_id);

-- ============================================================================
-- PART 5: QUESTION/ANSWER CACHING (Prevents question fatigue)
-- ============================================================================
-- Remembers user answers so we don't re-ask the same questions.

CREATE TABLE IF NOT EXISTS hierarchy_question_answers (
    id TEXT PRIMARY KEY,                    -- UUID
    machine_id TEXT NOT NULL,
    question_id TEXT NOT NULL,              -- e.g., 'submodule_tracking', 'docker_integration'
    answer TEXT NOT NULL,                   -- JSON: the user's answer
    context_hash TEXT,                      -- Hash of project structure at time of answer
    created_at TEXT DEFAULT (datetime('now') || 'Z'),

    CONSTRAINT unique_question_per_machine UNIQUE (machine_id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_question_answers_machine
    ON hierarchy_question_answers(machine_id);

-- ============================================================================
-- PART 6: PINNED SETTINGS (User overrides that survive re-scans)
-- ============================================================================
-- When user says "always use this setting", it's pinned here.

CREATE TABLE IF NOT EXISTS hierarchy_pinned_settings (
    id TEXT PRIMARY KEY,                    -- UUID
    machine_id TEXT NOT NULL,
    setting_key TEXT NOT NULL,              -- e.g., 'memory_location', 'tracked_submodules'
    setting_value TEXT NOT NULL,            -- JSON: the pinned value
    pinned_at TEXT DEFAULT (datetime('now') || 'Z'),
    pinned_reason TEXT,                     -- User's reason for pinning

    CONSTRAINT unique_pin_per_machine UNIQUE (machine_id, setting_key)
);

CREATE INDEX IF NOT EXISTS idx_pinned_settings_machine
    ON hierarchy_pinned_settings(machine_id);

-- ============================================================================
-- PART 7: SCOPES (Three-tier scoping system)
-- ============================================================================
-- global → org → repo → subrepo → session

CREATE TABLE IF NOT EXISTS cd_scopes (
    id TEXT PRIMARY KEY,                    -- UUID
    name TEXT NOT NULL,                     -- 'global', 'org', 'repo', 'subrepo', 'session'
    level INTEGER NOT NULL,                 -- 0=global, 1=org, 2=repo, 3=subrepo, 4=session
    parent_scope_id TEXT,                   -- Parent scope for inheritance
    project_path TEXT,                      -- For repo/subrepo scopes
    path TEXT,                              -- Path within project (e.g., 'context-dna')
    metadata TEXT DEFAULT '{}',             -- JSON: additional scope metadata
    created_at TEXT DEFAULT (datetime('now') || 'Z'),

    CONSTRAINT unique_scope UNIQUE (project_path, name, path),
    FOREIGN KEY (parent_scope_id) REFERENCES cd_scopes(id)
);

CREATE INDEX IF NOT EXISTS idx_cd_scopes_level
    ON cd_scopes(level);
CREATE INDEX IF NOT EXISTS idx_cd_scopes_project
    ON cd_scopes(project_path);

-- ============================================================================
-- PART 8: MODE SYNC TRACKING (Bidirectional lite/heavy sync)
-- ============================================================================
-- Tracks sync state between SQLite and PostgreSQL for seamless mode switching.
-- CRITICAL: This enables Context DNA to run on any computer and survive crashes.
--
-- Safety guarantees:
-- 1. WRITE-THROUGH: Every Postgres write also shadows to SQLite
-- 2. CRASH DETECTION: last_crash_at tracks unexpected terminations
-- 3. SYNC LOCK: sync_in_progress prevents concurrent sync corruption
-- 4. DIRECTIONAL QUEUES: Separate pending counts for each direction
-- 5. CONFLICT RESOLUTION: Configurable strategy for data conflicts

CREATE TABLE IF NOT EXISTS mode_sync_state (
    id TEXT PRIMARY KEY DEFAULT 'singleton', -- Only one row ever (singleton pattern)

    -- Current operational state
    current_mode TEXT NOT NULL DEFAULT 'lite' CHECK (current_mode IN ('lite','heavy')),
    last_mode TEXT CHECK (last_mode IN ('lite','heavy')),

    -- Timing & health
    last_sync_at TEXT,                       -- Last successful bidirectional sync
    last_crash_at TEXT,                      -- When system crashed (detected on restart)
    last_healthy_at TEXT,                    -- Last confirmed healthy state
    updated_at TEXT DEFAULT (datetime('now') || 'Z'),

    -- Connection state
    postgres_available INTEGER DEFAULT 0,    -- Can we reach Postgres? (0/1)
    postgres_latency_ms INTEGER,             -- Last measured latency to Postgres

    -- Sync queue state
    pending_to_postgres INTEGER DEFAULT 0,   -- Changes queued for Postgres
    pending_from_postgres INTEGER DEFAULT 0, -- Changes queued from Postgres
    sync_in_progress INTEGER DEFAULT 0,      -- Lock flag to prevent concurrent sync (0/1)

    -- Configuration
    auto_sync_enabled INTEGER DEFAULT 1,     -- Auto-sync when mode changes (0/1)
    write_through_enabled INTEGER DEFAULT 1, -- Shadow writes to SQLite (0/1) - CRITICAL SAFETY
    snapshot_interval_s INTEGER DEFAULT 300, -- How often to snapshot Postgres → SQLite

    -- Conflict handling
    conflict_resolution TEXT DEFAULT 'latest_wins' CHECK (
        conflict_resolution IN ('latest_wins', 'lite_wins', 'heavy_wins', 'manual')
    ),
    last_conflict_at TEXT,
    last_conflict_details TEXT,              -- JSON: details of last conflict
    conflicts_total INTEGER DEFAULT 0,       -- Lifetime conflict count
    conflicts_auto_resolved INTEGER DEFAULT 0,

    -- Error tracking
    last_error TEXT,
    last_error_at TEXT,
    consecutive_failures INTEGER DEFAULT 0,

    -- Statistics
    total_syncs_completed INTEGER DEFAULT 0,
    total_items_synced INTEGER DEFAULT 0,
    total_bytes_synced INTEGER DEFAULT 0
);

-- Ensure only one sync state row exists
INSERT OR IGNORE INTO mode_sync_state (id, current_mode, updated_at)
VALUES ('singleton', 'lite', datetime('now') || 'Z');

-- ============================================================================
-- PART 9: SYNC QUEUE (Pending changes for bidirectional sync)
-- ============================================================================
-- When offline or mode is different, queue changes for later sync.
-- Priority-based processing ensures critical data syncs first.
-- Deduplication prevents redundant sync operations.

CREATE TABLE IF NOT EXISTS sync_queue (
    id TEXT PRIMARY KEY,                    -- UUID

    -- What to sync
    table_name TEXT NOT NULL,               -- Which table the change is for
    record_id TEXT NOT NULL,                -- ID of the changed record
    operation TEXT NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
    data_json TEXT NOT NULL,                -- JSON: the change payload
    data_hash TEXT,                         -- SHA256 of data_json for deduplication

    -- Direction
    source_mode TEXT NOT NULL CHECK (source_mode IN ('lite', 'heavy')),
    target_mode TEXT NOT NULL CHECK (target_mode IN ('lite', 'heavy')),

    -- Processing state
    priority INTEGER DEFAULT 5 CHECK (priority BETWEEN 1 AND 10), -- 1=highest, 10=lowest
    status TEXT DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'completed', 'failed', 'abandoned', 'conflict')
    ),
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,

    -- Timing
    created_at TEXT DEFAULT (datetime('now') || 'Z'),
    attempted_at TEXT,                      -- Last attempt timestamp
    completed_at TEXT,                      -- When sync completed (success or abandon)

    -- Error tracking
    last_error TEXT,
    error_history TEXT,                     -- JSON array of past errors

    -- Deduplication: prevent duplicate pending syncs for same record
    UNIQUE(table_name, record_id, operation, target_mode, status)
);

-- Efficient queue processing (priority first, then FIFO)
CREATE INDEX IF NOT EXISTS idx_sync_queue_processing
    ON sync_queue(status, priority ASC, created_at ASC)
    WHERE status IN ('pending', 'processing');

CREATE INDEX IF NOT EXISTS idx_sync_queue_target
    ON sync_queue(target_mode, status);

CREATE INDEX IF NOT EXISTS idx_sync_queue_table
    ON sync_queue(table_name, record_id);

CREATE INDEX IF NOT EXISTS idx_sync_queue_cleanup
    ON sync_queue(completed_at)
    WHERE status IN ('completed', 'abandoned');

-- ============================================================================
-- PART 9.5: SYNC HISTORY (Audit trail for debugging & compliance)
-- ============================================================================
-- Every sync operation is logged for debugging crashed syncs and auditing.

CREATE TABLE IF NOT EXISTS sync_history (
    id TEXT PRIMARY KEY,                    -- UUID

    -- Sync operation details
    sync_type TEXT NOT NULL CHECK (
        sync_type IN ('full', 'incremental', 'snapshot', 'recovery', 'manual')
    ),
    from_mode TEXT NOT NULL CHECK (from_mode IN ('lite', 'heavy')),
    to_mode TEXT NOT NULL CHECK (to_mode IN ('lite', 'heavy')),
    trigger_reason TEXT NOT NULL,           -- What initiated the sync

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
    bytes_transferred INTEGER DEFAULT 0,

    -- Conflict handling
    conflicts_detected INTEGER DEFAULT 0,
    conflicts_resolved INTEGER DEFAULT 0,
    conflict_details TEXT,                  -- JSON: per-item conflict info

    -- Error tracking
    error_details TEXT,                     -- JSON: error info if failed

    -- Context
    machine_id TEXT,                        -- Which machine ran the sync
    triggered_by TEXT DEFAULT 'system'      -- 'system', 'user', 'scheduler', 'recovery'
);

CREATE INDEX IF NOT EXISTS idx_sync_history_time
    ON sync_history(started_at DESC);

CREATE INDEX IF NOT EXISTS idx_sync_history_status
    ON sync_history(status, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_sync_history_machine
    ON sync_history(machine_id, started_at DESC);

-- ============================================================================
-- PART 10: PROJECT BOUNDARY INTELLIGENCE
-- ============================================================================
-- Critical for filtering context injections by detected project.
-- Enables "which project is this file in?" queries for scoping.
-- Source: install-adapt-hierarchy.md PostgreSQL schema

-- 10a: Detected project boundaries (root directories)
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
    detection_markers TEXT DEFAULT '[]',    -- JSON: what markers confirmed this boundary

    -- Boundaries
    boundary_paths TEXT DEFAULT '[]',       -- JSON: paths that define this project's scope
    excluded_paths TEXT DEFAULT '[]',       -- JSON: paths within boundary to exclude

    -- Relationships
    parent_project_id TEXT,                 -- For nested projects (submodule in superrepo)

    -- User overrides
    is_pinned INTEGER DEFAULT 0,            -- User explicitly confirmed this boundary
    pinned_at TEXT,
    pinned_reason TEXT,

    -- Timing
    created_at TEXT DEFAULT (datetime('now') || 'Z'),
    updated_at TEXT DEFAULT (datetime('now') || 'Z'),
    last_validated_at TEXT,

    -- Sync tracking
    synced_to_postgres INTEGER DEFAULT 0,
    synced_at TEXT,
    postgres_id TEXT,

    FOREIGN KEY (parent_project_id) REFERENCES project_boundaries(id),
    CONSTRAINT unique_boundary UNIQUE (machine_id, project_root)
);

CREATE INDEX IF NOT EXISTS idx_project_boundaries_machine
    ON project_boundaries(machine_id);
CREATE INDEX IF NOT EXISTS idx_project_boundaries_root
    ON project_boundaries(project_root);
CREATE INDEX IF NOT EXISTS idx_project_boundaries_type
    ON project_boundaries(project_type);
CREATE INDEX IF NOT EXISTS idx_project_boundaries_sync
    ON project_boundaries(synced_to_postgres) WHERE synced_to_postgres = 0;

-- 10b: Keyword ↔ Project associations for context filtering
CREATE TABLE IF NOT EXISTS keyword_project_associations (
    id TEXT PRIMARY KEY,                    -- UUID
    machine_id TEXT NOT NULL,

    -- Association
    keyword TEXT NOT NULL,                  -- e.g., 'livekit', 'django', 'terraform'
    project_id TEXT NOT NULL,               -- FK to project_boundaries

    -- Scoring
    association_strength REAL DEFAULT 0.5,  -- 0.0 to 1.0 (how strong is the link)
    occurrence_count INTEGER DEFAULT 1,     -- How many times seen together

    -- Learning
    learned_from TEXT NOT NULL,             -- 'file_content', 'commit_message', 'user_tag', 'import_analysis'
    last_occurrence_at TEXT,

    -- Timing
    created_at TEXT DEFAULT (datetime('now') || 'Z'),
    updated_at TEXT DEFAULT (datetime('now') || 'Z'),

    -- Sync tracking
    synced_to_postgres INTEGER DEFAULT 0,
    synced_at TEXT,
    postgres_id TEXT,

    FOREIGN KEY (project_id) REFERENCES project_boundaries(id) ON DELETE CASCADE,
    CONSTRAINT unique_keyword_project UNIQUE (machine_id, keyword, project_id)
);

CREATE INDEX IF NOT EXISTS idx_keyword_assoc_keyword
    ON keyword_project_associations(keyword);
CREATE INDEX IF NOT EXISTS idx_keyword_assoc_project
    ON keyword_project_associations(project_id);
CREATE INDEX IF NOT EXISTS idx_keyword_assoc_strength
    ON keyword_project_associations(association_strength DESC);
CREATE INDEX IF NOT EXISTS idx_keyword_assoc_sync
    ON keyword_project_associations(synced_to_postgres) WHERE synced_to_postgres = 0;

-- 10c: Boundary decision history (learning from user corrections)
CREATE TABLE IF NOT EXISTS boundary_decisions (
    id TEXT PRIMARY KEY,                    -- UUID
    machine_id TEXT NOT NULL,

    -- The decision context
    file_path TEXT NOT NULL,                -- File that triggered the decision
    detected_project_id TEXT,               -- What we detected
    correct_project_id TEXT,                -- What user said was correct (NULL if detection was right)

    -- Decision outcome
    was_correct INTEGER NOT NULL,           -- 0 = wrong detection, 1 = correct detection
    user_feedback TEXT,                     -- User's explanation (for learning)

    -- Applied correction
    correction_rule TEXT,                   -- JSON: rule derived from this correction
    correction_applied INTEGER DEFAULT 0,   -- Did we update boundaries based on this?

    -- Timing
    decided_at TEXT DEFAULT (datetime('now') || 'Z'),

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
CREATE INDEX IF NOT EXISTS idx_boundary_decisions_correct
    ON boundary_decisions(was_correct);
CREATE INDEX IF NOT EXISTS idx_boundary_decisions_sync
    ON boundary_decisions(synced_to_postgres) WHERE synced_to_postgres = 0;

-- ============================================================================
-- PART 11: IDE CONFIGURATION (Multi-IDE support)
-- ============================================================================
-- Context DNA supports 9 IDEs. Track which are installed and configured.
-- Source: install-adapt-hierarchy.md IDE Detection section

CREATE TABLE IF NOT EXISTS ide_configurations (
    id TEXT PRIMARY KEY,                    -- UUID
    machine_id TEXT NOT NULL,

    -- IDE identification
    ide_type TEXT NOT NULL CHECK (ide_type IN (
        'cursor', 'vscode', 'windsurf', 'zed', 'jetbrains',
        'neovim', 'emacs', 'sublime', 'xcode', 'other'
    )),
    ide_version TEXT,                       -- Installed version
    ide_path TEXT,                          -- Path to IDE executable/app

    -- Configuration
    is_installed INTEGER DEFAULT 0,         -- Detected as installed
    is_configured INTEGER DEFAULT 0,        -- Context DNA hooks installed
    is_primary INTEGER DEFAULT 0,           -- User's primary IDE
    config_path TEXT,                       -- Path to IDE config (e.g., ~/.cursor/)

    -- Integration status
    hook_version TEXT,                      -- Version of Context DNA hook installed
    hook_installed_at TEXT,
    last_hook_activity TEXT,                -- When hook last fired

    -- User preferences
    enable_injections INTEGER DEFAULT 1,    -- Enable context injection for this IDE
    injection_style TEXT DEFAULT 'full',    -- 'full', 'minimal', 'custom'

    -- Timing
    created_at TEXT DEFAULT (datetime('now') || 'Z'),
    updated_at TEXT DEFAULT (datetime('now') || 'Z'),

    -- Sync tracking
    synced_to_postgres INTEGER DEFAULT 0,
    synced_at TEXT,
    postgres_id TEXT,

    CONSTRAINT unique_ide_per_machine UNIQUE (machine_id, ide_type)
);

CREATE INDEX IF NOT EXISTS idx_ide_config_machine
    ON ide_configurations(machine_id);
CREATE INDEX IF NOT EXISTS idx_ide_config_type
    ON ide_configurations(ide_type);
CREATE INDEX IF NOT EXISTS idx_ide_config_primary
    ON ide_configurations(is_primary) WHERE is_primary = 1;
CREATE INDEX IF NOT EXISTS idx_ide_config_sync
    ON ide_configurations(synced_to_postgres) WHERE synced_to_postgres = 0;

-- ============================================================================
-- PART 12: MIGRATIONS TRACKING
-- ============================================================================

CREATE TABLE IF NOT EXISTS cd_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    description TEXT,
    applied_at TEXT DEFAULT (datetime('now') || 'Z')
);

-- Record migrations
INSERT OR IGNORE INTO cd_migrations (version, description) VALUES
    ('001_hierarchy', 'SQLite Hierarchy Schema - Bidirectional Postgres-compatible'),
    ('002_enhanced_sync', 'Enhanced sync tracking: crash detection, write-through, priority queues, audit history'),
    ('003_project_boundaries', 'Project Boundary Intelligence: boundaries, keyword associations, decision learning'),
    ('004_ide_configurations', 'Multi-IDE support: 9 IDE types, hook tracking, injection preferences');

-- ============================================================================
-- PART 13: SEED DATA (Common hierarchy patterns)
-- ============================================================================
-- Same seed data as PostgreSQL for consistency.

INSERT OR IGNORE INTO cd_hierarchy_patterns (id, name, description, detection_rules, default_scoping) VALUES
(
    'pat-superrepo-001',
    'superrepo',
    'Monolithic repository with git submodules for separate deployable components',
    '{"indicators": ["has_submodules", "multiple_deploy_targets"], "markers": [".gitmodules"], "confidence_threshold": 0.8}',
    '{"root": "repo", "submodules": "subrepo", "scope_inheritance": true}'
),
(
    'pat-monorepo-001',
    'monorepo',
    'Single repository with multiple packages/services managed together',
    '{"indicators": ["workspace_config", "multiple_package_json", "shared_deps"], "markers": ["packages/", "lerna.json", "pnpm-workspace.yaml", "turbo.json"], "confidence_threshold": 0.7}',
    '{"root": "repo", "packages": "subrepo", "scope_inheritance": true}'
),
(
    'pat-polyrepo-001',
    'polyrepo',
    'Multiple separate repositories for different components',
    '{"indicators": ["single_package", "no_submodules", "standalone_deploy"], "markers": ["single package.json at root"], "confidence_threshold": 0.6}',
    '{"root": "repo", "scope_inheritance": false}'
),
(
    'pat-microservices-001',
    'microservices',
    'Service-oriented architecture with independent deployable services',
    '{"indicators": ["docker_compose", "multiple_dockerfiles", "service_dirs"], "markers": ["docker-compose.yaml", "services/", "*/Dockerfile"], "confidence_threshold": 0.75}',
    '{"root": "repo", "services": "subrepo", "shared": "subrepo", "scope_inheritance": true}'
);

-- Insert default global scopes
INSERT OR IGNORE INTO cd_scopes (id, name, level, path) VALUES
    ('scope-global-001', 'global', 0, NULL),
    ('scope-org-001', 'org', 1, NULL);

-- ============================================================================
-- END OF SCHEMA
-- ============================================================================
