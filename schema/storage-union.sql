-- =====================================================================
-- Context DNA Unified Storage Schema
-- =====================================================================
-- Canonical DDL for the unified storage layer. Both SQLite and PostgreSQL
-- backends materialize these tables with engine-specific dialect tweaks
-- (SQLite uses FTS5 virtual tables; Postgres uses GIN over a generated
-- tsvector column). Indexes mirror the access patterns exercised by the
-- 26 consumer call sites in memory/agent_service.py,
-- memory/session_gold_passes.py, memory/persistent_hook_structure.py,
-- and scripts/surgery-team.py.
--
-- Conventions:
--   * All TEXT columns are UTF-8.
--   * Timestamps are ISO-8601 strings in SQLite and TIMESTAMPTZ in Postgres.
--   * "tags" / "metadata" / "context_samples" hold JSON; Postgres stores
--     them as JSONB, SQLite stores them as TEXT containing JSON.
--   * Soft-deletes are modelled by setting type='retired' rather than
--     removing rows — preserves audit history.
--
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. learnings: durable wins / fixes / patterns / insights / SOPs
-- ---------------------------------------------------------------------
-- Sole source of truth for everything the dashboard and xbar render.
-- The webhook injection pipeline (sections 0-8) writes here on every
-- ≤5-word ACK gate pass. Workspace-scoped so multi-repo installs stay
-- isolated. Schema version pinned at 3 (workspace_id introduced).
CREATE TABLE IF NOT EXISTS learnings (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,                  -- win | fix | pattern | insight | gotcha | sop | retired
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',     -- JSON array of strings
    session_id TEXT DEFAULT '',
    injection_id TEXT DEFAULT '',
    source TEXT DEFAULT 'manual',        -- hook | manual | auto | agent
    workspace_id TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}', -- JSON
    merge_count INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learnings_type      ON learnings(type);
CREATE INDEX IF NOT EXISTS idx_learnings_created   ON learnings(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_learnings_session   ON learnings(session_id);
CREATE INDEX IF NOT EXISTS idx_learnings_workspace ON learnings(workspace_id);


-- ---------------------------------------------------------------------
-- 2. learnings_fts: full-text search index
-- ---------------------------------------------------------------------
-- SQLite: FTS5 virtual table with content-mirror triggers.
-- Postgres equivalent: GIN over generated tsvector column inside
-- learnings itself (defined in postgres_storage.init_tables()).
-- This file documents the SQLite shape; both backends MUST keep the
-- index in sync with INSERT / DELETE / UPDATE on learnings.
CREATE VIRTUAL TABLE IF NOT EXISTS learnings_fts USING fts5(
    title,
    content,
    tags,
    content=learnings,
    content_rowid=rowid
);

-- Sync triggers for SQLite FTS5
CREATE TRIGGER IF NOT EXISTS learnings_ai AFTER INSERT ON learnings BEGIN
    INSERT INTO learnings_fts(rowid, title, content, tags)
    VALUES (new.rowid, new.title, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS learnings_ad AFTER DELETE ON learnings BEGIN
    INSERT INTO learnings_fts(learnings_fts, rowid, title, content, tags)
    VALUES ('delete', old.rowid, old.title, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS learnings_au AFTER UPDATE ON learnings BEGIN
    INSERT INTO learnings_fts(learnings_fts, rowid, title, content, tags)
    VALUES ('delete', old.rowid, old.title, old.content, old.tags);
    INSERT INTO learnings_fts(rowid, title, content, tags)
    VALUES (new.rowid, new.title, new.content, new.tags);
END;


-- ---------------------------------------------------------------------
-- 3. patterns (alias: negative_patterns): SOP quality evolution
-- ---------------------------------------------------------------------
-- Tracks anti-patterns observed in the wild. Promoted to SOP content
-- only after 3+ occurrences (Aaron's rule). Drives the "lessons learned"
-- divider section of every dynamically-rendered SOP.
CREATE TABLE IF NOT EXISTS patterns (
    pattern_key TEXT PRIMARY KEY,
    description TEXT,
    goal TEXT DEFAULT '',                -- which SOP goal it belongs to
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    frequency INTEGER DEFAULT 1,
    promoted_to_sop INTEGER DEFAULT 0,   -- 0 = pending, 1 = promoted
    context_samples TEXT DEFAULT '[]'    -- JSON: up to 3 evidence snippets
);

CREATE INDEX IF NOT EXISTS idx_patterns_goal ON patterns(goal);
CREATE INDEX IF NOT EXISTS idx_patterns_freq ON patterns(frequency DESC);

-- Legacy alias retained for SQLite compatibility with the original module
CREATE TABLE IF NOT EXISTS negative_patterns (
    pattern_key TEXT PRIMARY KEY,
    description TEXT,
    goal TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    frequency INTEGER DEFAULT 1,
    promoted_to_sop INTEGER DEFAULT 0,
    context_samples TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_negpat_goal ON negative_patterns(goal);
CREATE INDEX IF NOT EXISTS idx_negpat_freq ON negative_patterns(frequency DESC);


-- ---------------------------------------------------------------------
-- 4. evidence: pre-promotion claim store
-- ---------------------------------------------------------------------
-- Holds raw claims emitted by the agent before the wisdom-promotion
-- pipeline upgrades them into the learnings table. Includes confidence
-- score (0.0-1.0) and decision (pending | accepted | rejected | promoted).
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    claim TEXT NOT NULL,
    evidence_type TEXT DEFAULT 'observation',
    confidence REAL DEFAULT 0.5,
    decision TEXT DEFAULT 'pending',
    promoted_to_learning_id TEXT,        -- FK reference, null until promotion
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_evidence_session  ON evidence(session_id);
CREATE INDEX IF NOT EXISTS idx_evidence_decision ON evidence(decision);
CREATE INDEX IF NOT EXISTS idx_evidence_created  ON evidence(created_at DESC);


-- ---------------------------------------------------------------------
-- 5. sops: dynamic Standard Operating Procedure registry
-- ---------------------------------------------------------------------
-- Each SOP is a goal-keyed document with the canonical step list and a
-- divider section auto-populated from `patterns` once promotion criteria
-- are met. SOPs are versioned via `revision` (monotonic int per goal).
CREATE TABLE IF NOT EXISTS sops (
    goal TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    project_id TEXT,
    scope TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (goal, revision)
);

CREATE INDEX IF NOT EXISTS idx_sops_goal    ON sops(goal);
CREATE INDEX IF NOT EXISTS idx_sops_updated ON sops(updated_at DESC);


-- ---------------------------------------------------------------------
-- 6. observability_events: append-only fleet event log
-- ---------------------------------------------------------------------
-- Powers /health, the gains-gate, cardio EKG, and the post-phase
-- verification pipeline. Strictly append-only. Time-slice queries hit
-- the (created_at) index; type-filtered queries hit (event_type).
CREATE TABLE IF NOT EXISTS observability_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,            -- webhook.completed | gains.check | health.probe | ...
    project_id TEXT,
    scope TEXT,                          -- node id, channel, agent, etc.
    payload TEXT NOT NULL,               -- JSON
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_events_created ON observability_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_events_type    ON observability_events(event_type);
CREATE INDEX IF NOT EXISTS idx_obs_events_scope   ON observability_events(scope);


-- ---------------------------------------------------------------------
-- 7. dialogue_messages: persistent multi-agent conversation log
-- ---------------------------------------------------------------------
-- Holds the running synaptic / 3-surgeon / atlas dialogue. session_id
-- ties a message to a Claude Code session; thread_id groups messages
-- inside a single subprocess invocation. role tracks the speaker
-- (atlas | synaptic | cardiologist | neurologist | user | system).
CREATE TABLE IF NOT EXISTS dialogue_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    thread_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,                          -- the LLM that produced the line
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL DEFAULT 0.0,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dialogue_session ON dialogue_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_dialogue_thread  ON dialogue_messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_dialogue_created ON dialogue_messages(created_at DESC);


-- ---------------------------------------------------------------------
-- 8. failures: Three-Tier escalation tracking
-- ---------------------------------------------------------------------
-- Counted by get_failure_count(session_id) in persistent_hook_structure
-- to decide when to escalate to a heavier surgeon panel.
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    failure_type TEXT,
    prompt TEXT,
    error_message TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_failures_session ON failures(session_id, created_at DESC);


-- ---------------------------------------------------------------------
-- 9. schema_version: migration tracking
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version (component, version, applied_at)
VALUES ('storage_union', 1, CURRENT_TIMESTAMP);

-- =====================================================================
-- END storage-union.sql
-- =====================================================================
