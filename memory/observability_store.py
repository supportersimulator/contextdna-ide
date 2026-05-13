#!/usr/bin/env python3
"""
OBSERVABILITY STORE - Persistence Layer for Health Events

Bridges the real-time MutualHeartbeat monitoring to historical observability tables.
Supports both SQLite (lite mode) and PostgreSQL (heavy mode).

Tables populated:
- dependency_status_event: Health check results over time
- task_run_event: Task executions including recovery attempts
- job_schedule: Lite mode job scheduling (SQLite only)

Usage:
    from memory.observability_store import ObservabilityStore

    store = ObservabilityStore()
    store.record_dependency_status("redis", "healthy", latency_ms=5)
    store.record_task_run("brain_cycle", "scheduled", "success", duration_ms=1200)
"""

import os
import uuid
import time
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Literal
from dataclasses import dataclass, asdict
from enum import Enum

logger = logging.getLogger("context_dna.observability")

# Version for tracking schema compatibility
ORCHESTRATOR_VERSION = "1.0.0"


# =============================================================================
# EVIDENCE-BASED MEDICINE (EBM) GRADING SYSTEM
# =============================================================================

class EvidenceGrade(Enum):
    """
    Evidence-Based Medicine grading hierarchy.

    Higher values = stronger evidence.
    These weights influence confidence scores in claim evaluation.

    Based on traditional EBM pyramid:
    - Systematic reviews/meta-analyses at top
    - RCTs below
    - Observational studies (cohort, case-control)
    - Case series/reports
    - Expert opinion/anecdotal at base
    """
    META_ANALYSIS = 1.0       # Systematic review of multiple RCTs
    SYSTEMATIC_REVIEW = 0.9   # Systematic review (may not include meta-analysis)
    RCT = 0.8                 # Randomized controlled trial
    COHORT = 0.7              # Prospective/retrospective cohort study
    CASE_CONTROL = 0.6        # Case-control study
    CASE_SERIES = 0.5         # Case series (multiple cases, no control)
    EXPERT_OPINION = 0.4      # Expert consensus/committee opinion
    ANECDOTAL = 0.3           # Single case report, anecdote

    @classmethod
    def from_string(cls, grade_str: str) -> 'EvidenceGrade':
        """Convert string to EvidenceGrade enum."""
        grade_map = {
            'meta_analysis': cls.META_ANALYSIS,
            'meta': cls.META_ANALYSIS,
            'systematic_review': cls.SYSTEMATIC_REVIEW,
            'systematic': cls.SYSTEMATIC_REVIEW,
            'rct': cls.RCT,
            'randomized': cls.RCT,
            'cohort': cls.COHORT,
            'case_control': cls.CASE_CONTROL,
            'case-control': cls.CASE_CONTROL,
            'quasi': cls.CASE_CONTROL,  # Backwards compatibility
            'case_series': cls.CASE_SERIES,
            'case-series': cls.CASE_SERIES,
            'correlation': cls.CASE_SERIES,  # Backwards compatibility
            'expert_opinion': cls.EXPERT_OPINION,
            'expert': cls.EXPERT_OPINION,
            'opinion': cls.EXPERT_OPINION,
            'anecdotal': cls.ANECDOTAL,
            'anecdote': cls.ANECDOTAL,
        }
        return grade_map.get(grade_str.lower(), cls.ANECDOTAL)

    @classmethod
    def all_grades(cls) -> List[str]:
        """Return all valid grade strings for DB CHECK constraint."""
        return [g.name.lower() for g in cls]

    @property
    def db_name(self) -> str:
        """Return DB-compatible grade string (handles old CHECK constraints)."""
        _db_map = {
            'META_ANALYSIS': 'meta',
            'SYSTEMATIC_REVIEW': 'meta',
            'RCT': 'rct',
            'COHORT': 'cohort',
            'CASE_CONTROL': 'quasi',
            'CASE_SERIES': 'correlation',
            'EXPERT_OPINION': 'opinion',
            'ANECDOTAL': 'anecdote',
        }
        return _db_map.get(self.name, 'anecdote')

    def weight(self) -> float:
        """Get numeric weight for this evidence grade."""
        return self.value

    def apply_to_confidence(self, base_confidence: float) -> float:
        """
        Apply evidence grade weight to a base confidence score.

        Formula: adjusted_confidence = base_confidence * evidence_weight

        This ensures high-confidence claims backed by weak evidence
        are appropriately discounted.

        Args:
            base_confidence: Raw confidence score (0-1)

        Returns:
            Evidence-weighted confidence (0-1)
        """
        return min(1.0, max(0.0, base_confidence * self.value))

# Database paths
MEMORY_DIR = Path(__file__).parent
SQLITE_DB_PATH = MEMORY_DIR / ".observability.db"


@dataclass
class DependencyStatusEvent:
    """A single dependency health check event."""
    dep_event_id: str
    timestamp_utc: str
    mode: Literal["lite", "heavy"]
    orchestrator_version: str
    dependency_name: str
    status: Literal["healthy", "degraded", "down"]
    latency_ms: Optional[int] = None
    reason_code: Optional[str] = None
    details_json: Optional[str] = None


@dataclass
class TaskRunEvent:
    """A single task execution event."""
    task_run_id: str
    timestamp_utc: str
    mode: Literal["lite", "heavy"]
    orchestrator_version: str
    task_name: str
    run_type: Literal["scheduled", "manual", "recovery"]
    status: Literal["success", "partial", "failure", "skipped"]
    duration_ms: int
    budget_ms: Optional[int] = None
    details_json: Optional[str] = None
    error_json: Optional[str] = None


@dataclass
class JobScheduleEntry:
    """A scheduled job entry (lite mode)."""
    job_name: str
    interval_s: int
    next_run_utc: str
    last_run_utc: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None


@dataclass
class Claim:
    """
    A knowledge claim with evidence-based grading.

    Claims represent assertions about system behavior, patterns, or decisions
    that can be tracked for validation over time.
    """
    claim_id: str
    created_at_utc: str
    created_by: str
    statement: str
    scope_json: str  # JSON defining where this claim applies
    evidence_grade: str  # EvidenceGrade enum name (lowercase)
    confidence: float  # Raw confidence 0-1
    weighted_confidence: float  # Evidence-adjusted confidence
    ttl_seconds: int  # Time-to-live before re-validation needed
    provenance_json: str  # Source information


class ObservabilityStore:
    """
    Unified observability persistence for lite and heavy modes.

    In lite mode: Uses SQLite for all storage
    In heavy mode: Uses PostgreSQL for primary, SQLite as fallback
    """

    def __init__(self, mode: str = "auto"):
        """
        Initialize the observability store.

        Args:
            mode: "lite", "heavy", or "auto" (detect from heartbeat config)
        """
        self.mode = mode if mode != "auto" else self._detect_mode()
        self._sqlite_conn: Optional[sqlite3.Connection] = None
        self._pg_conn = None
        self._write_lock = threading.Lock()  # Thread safety for concurrent writes
        self._init_storage()

    def _detect_mode(self) -> str:
        """Detect current mode via mode_authority (single source of truth)."""
        # Old: mutual_heartbeat.check_docker_status().mode — had "light" typo, bypassed Redis truth
        from memory.mode_authority import get_mode
        return get_mode()

    def _init_storage(self):
        """Initialize storage backends based on mode."""
        # Always init SQLite (fallback for heavy, primary for lite)
        self._init_sqlite()

        if self.mode == "heavy":
            self._init_postgres()

    def _init_sqlite(self):
        """Initialize SQLite database with observability schema.

        Includes corruption recovery: if the database file is corrupted
        (zeroed header, malformed disk image, etc.), the corrupted file is
        backed up with a timestamp suffix and a fresh database is created.
        This prevents ObservabilityStore init from crashing the entire
        agent service when corruption occurs (P1.2 fix 2026-02-07).
        """
        try:
            from memory.db_utils import connect_wal
            self._sqlite_conn = connect_wal(
                SQLITE_DB_PATH, check_same_thread=False, timeout=5
            )
            # Quick integrity probe — catches "file is not a database"
            self._sqlite_conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        except sqlite3.DatabaseError as e:
            logger.warning(
                "Corrupted .observability.db detected (%s) — backing up and recreating", e
            )
            # Close broken connection
            try:
                self._sqlite_conn.close()
            except Exception:
                pass
            # Back up corrupted file (preserve evidence)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            corrupted_path = SQLITE_DB_PATH.with_suffix(f".db.corrupted_{ts}")
            try:
                SQLITE_DB_PATH.rename(corrupted_path)
                logger.info("Corrupted DB backed up to %s", corrupted_path)
            except Exception as rename_err:
                logger.warning("Could not rename corrupted DB: %s", rename_err)
                # Force remove if rename fails
                try:
                    SQLITE_DB_PATH.unlink(missing_ok=True)
                except Exception:
                    pass
            # Remove stale WAL/SHM files
            for suffix in ("-wal", "-shm"):
                wal_path = SQLITE_DB_PATH.with_name(SQLITE_DB_PATH.name + suffix)
                try:
                    wal_path.unlink(missing_ok=True)
                except Exception:
                    pass
            # Create fresh connection
            self._sqlite_conn = connect_wal(
                SQLITE_DB_PATH, check_same_thread=False, timeout=5
            )

        # Create tables
        self._sqlite_conn.executescript("""
            PRAGMA foreign_keys=ON;

            -- Task run events
            CREATE TABLE IF NOT EXISTS task_run_event (
                task_run_id       TEXT PRIMARY KEY,
                timestamp_utc     TEXT NOT NULL,
                mode              TEXT NOT NULL CHECK (mode IN ('light','heavy')),
                orchestrator_version TEXT NOT NULL,
                task_name         TEXT NOT NULL,
                run_type          TEXT NOT NULL CHECK (run_type IN ('scheduled','manual','recovery')),
                status            TEXT NOT NULL CHECK (status IN ('success','partial','failure','skipped')),
                duration_ms       INTEGER NOT NULL CHECK (duration_ms >= 0),
                budget_ms         INTEGER,
                details_json      TEXT,
                error_json        TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_task_run_time
                ON task_run_event(timestamp_utc DESC);

            CREATE INDEX IF NOT EXISTS idx_task_run_task
                ON task_run_event(task_name, timestamp_utc DESC);

            -- Dependency status events
            CREATE TABLE IF NOT EXISTS dependency_status_event (
                dep_event_id      TEXT PRIMARY KEY,
                timestamp_utc     TEXT NOT NULL,
                mode              TEXT NOT NULL CHECK (mode IN ('light','heavy')),
                orchestrator_version TEXT NOT NULL,
                dependency_name   TEXT NOT NULL,
                status            TEXT NOT NULL CHECK (status IN ('healthy','degraded','down')),
                latency_ms        INTEGER,
                reason_code       TEXT,
                details_json      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_dep_status_time
                ON dependency_status_event(timestamp_utc DESC);

            CREATE INDEX IF NOT EXISTS idx_dep_status_dep
                ON dependency_status_event(dependency_name, timestamp_utc DESC);

            -- Job schedule (lite mode scheduler)
            CREATE TABLE IF NOT EXISTS job_schedule (
                job_name TEXT PRIMARY KEY,
                interval_s INTEGER NOT NULL,
                next_run_utc TEXT NOT NULL,
                last_run_utc TEXT,
                last_status TEXT,
                last_error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_job_schedule_next
                ON job_schedule(next_run_utc);

            -- =====================================================
            -- METRICS ROLLUP TABLES (A/B Testing Analytics)
            -- =====================================================

            -- Baseline mapping for experiments
            CREATE TABLE IF NOT EXISTS experiment_baseline (
                experiment_id        TEXT PRIMARY KEY,
                baseline_variant_id  TEXT NOT NULL
            );

            -- Variant summary aggregates per time window
            CREATE TABLE IF NOT EXISTS variant_summary_rollup (
                experiment_id        TEXT NOT NULL,
                variant_id           TEXT NOT NULL,
                window_start_utc     TEXT NOT NULL,
                window_end_utc       TEXT NOT NULL,
                n                    INTEGER NOT NULL,
                avg_reward           REAL NOT NULL,
                success_rate         REAL NOT NULL,
                avg_latency_ms       REAL NOT NULL,
                avg_tokens           REAL NOT NULL,
                avg_retry            REAL NOT NULL,
                avg_undo             REAL NOT NULL,
                avg_revert           REAL NOT NULL,
                error_recurrence_rate REAL NOT NULL,
                PRIMARY KEY (experiment_id, variant_id, window_start_utc, window_end_utc)
            );

            CREATE INDEX IF NOT EXISTS idx_variant_summary_time
                ON variant_summary_rollup(window_end_utc DESC);

            -- Variant vs baseline deltas
            CREATE TABLE IF NOT EXISTS variant_vs_baseline_rollup (
                experiment_id        TEXT NOT NULL,
                variant_id           TEXT NOT NULL,
                window_start_utc     TEXT NOT NULL,
                window_end_utc       TEXT NOT NULL,
                n                    INTEGER NOT NULL,
                avg_reward           REAL NOT NULL,
                success_rate         REAL NOT NULL,
                avg_latency_ms       REAL NOT NULL,
                avg_tokens           REAL NOT NULL,
                avg_revert           REAL NOT NULL,
                error_recurrence_rate REAL NOT NULL,
                latency_pct_delta    REAL,
                tokens_pct_delta     REAL,
                revert_abs_delta     REAL,
                error_recur_abs_delta REAL,
                PRIMARY KEY (experiment_id, variant_id, window_start_utc, window_end_utc)
            );

            -- Section-level metrics
            CREATE TABLE IF NOT EXISTS section_metrics_rollup (
                experiment_id     TEXT NOT NULL,
                section_id        TEXT NOT NULL,
                version           TEXT NOT NULL,
                window_start_utc  TEXT NOT NULL,
                window_end_utc    TEXT NOT NULL,
                n                 INTEGER NOT NULL,
                avg_latency_ms    REAL NOT NULL,
                avg_tokens        REAL NOT NULL,
                healthy_rate      REAL NOT NULL,
                PRIMARY KEY (experiment_id, section_id, version, window_start_utc, window_end_utc)
            );

            -- Claim outcome tracking
            CREATE TABLE IF NOT EXISTS claim_outcome_rollup (
                claim_id          TEXT NOT NULL,
                evidence_grade    TEXT NOT NULL,
                confidence        REAL NOT NULL,
                window_start_utc  TEXT NOT NULL,
                window_end_utc    TEXT NOT NULL,
                n                 INTEGER NOT NULL,
                avg_reward        REAL NOT NULL,
                success_rate      REAL NOT NULL,
                PRIMARY KEY (claim_id, window_start_utc, window_end_utc)
            );

            -- =====================================================
            -- GUARDRAIL STOP EVENTS (Safety)
            -- =====================================================

            CREATE TABLE IF NOT EXISTS guardrail_stop_event (
                stop_id           TEXT PRIMARY KEY,
                timestamp_utc     TEXT NOT NULL,
                experiment_id     TEXT NOT NULL,
                variant_id        TEXT NOT NULL,
                baseline_variant_id TEXT,
                severity          TEXT NOT NULL CHECK (severity IN ('soft','hard')),
                metric_name       TEXT NOT NULL,
                window_n          INTEGER NOT NULL CHECK (window_n > 0),
                baseline_value    REAL,
                variant_value     REAL,
                delta_value       REAL,
                threshold_value   REAL,
                action_taken      TEXT NOT NULL,
                notes             TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_guardrail_stop_experiment_time
                ON guardrail_stop_event(experiment_id, timestamp_utc DESC);

            -- =====================================================
            -- EEG TELEMETRY TABLES (Core Context DNA)
            -- From sqlite_eeg_ddl.sql
            -- =====================================================

            -- injection_event: Core injection tracking
            CREATE TABLE IF NOT EXISTS injection_event (
                injection_id         TEXT PRIMARY KEY,
                timestamp_utc        TEXT NOT NULL,
                project_id           TEXT NOT NULL,
                repo_id              TEXT NOT NULL,
                session_id           TEXT NOT NULL,
                user_id_hash         TEXT NOT NULL,
                agent_name           TEXT NOT NULL,
                agent_version        TEXT NOT NULL,
                ide                  TEXT,
                os                   TEXT,
                task_type            TEXT NOT NULL,
                entrypoint           TEXT NOT NULL,
                user_prompt_sha256   TEXT NOT NULL,
                selection_sha256     TEXT,
                orchestrator_version TEXT NOT NULL,
                total_latency_ms     INTEGER NOT NULL CHECK (total_latency_ms >= 0),
                total_tokens         INTEGER NOT NULL CHECK (total_tokens >= 0),
                payload_sha256       TEXT NOT NULL,
                experiment_id        TEXT,
                variant_id           TEXT,
                assignment_deterministic INTEGER NOT NULL DEFAULT 1,
                vault_snapshot_id    TEXT NOT NULL,
                signal_batch_id      TEXT NOT NULL,
                schema_version       TEXT NOT NULL DEFAULT '1.0'
            );

            CREATE INDEX IF NOT EXISTS idx_injection_event_time
                ON injection_event(timestamp_utc);

            CREATE INDEX IF NOT EXISTS idx_injection_event_repo_task
                ON injection_event(repo_id, task_type, timestamp_utc DESC);

            CREATE INDEX IF NOT EXISTS idx_injection_event_experiment
                ON injection_event(experiment_id, variant_id);

            -- injection_section: Per-section data
            CREATE TABLE IF NOT EXISTS injection_section (
                injection_id     TEXT NOT NULL,
                section_id       TEXT NOT NULL,
                enabled          INTEGER NOT NULL,
                version          TEXT NOT NULL,
                content_sha256   TEXT NOT NULL,
                latency_ms       INTEGER NOT NULL CHECK (latency_ms >= 0),
                tokens           INTEGER NOT NULL CHECK (tokens >= 0),
                health           TEXT NOT NULL CHECK (health IN ('healthy','degraded','down')),
                PRIMARY KEY (injection_id, section_id),
                FOREIGN KEY (injection_id) REFERENCES injection_event(injection_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_injection_section_section
                ON injection_section(section_id, version);

            -- outcome_event: Success/failure outcomes
            CREATE TABLE IF NOT EXISTS outcome_event (
                outcome_id        TEXT PRIMARY KEY,
                timestamp_utc     TEXT NOT NULL,
                injection_id      TEXT,
                session_id        TEXT NOT NULL,
                outcome_type      TEXT NOT NULL,
                success           INTEGER NOT NULL,
                reward            REAL NOT NULL CHECK (reward >= -1 AND reward <= 1),
                build_pass        INTEGER,
                test_pass         INTEGER,
                error_signature_before TEXT,
                error_signature_after  TEXT,
                time_to_next_success_s INTEGER CHECK (time_to_next_success_s >= 0),
                retry_count       INTEGER CHECK (retry_count >= 0),
                undo_count        INTEGER CHECK (undo_count >= 0),
                revert_count      INTEGER CHECK (revert_count >= 0),
                notes             TEXT,
                FOREIGN KEY (injection_id) REFERENCES injection_event(injection_id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_outcome_event_injection
                ON outcome_event(injection_id);

            CREATE INDEX IF NOT EXISTS idx_outcome_event_session_time
                ON outcome_event(session_id, timestamp_utc DESC);

            -- experiment: A/B test definitions
            CREATE TABLE IF NOT EXISTS experiment (
                experiment_id     TEXT PRIMARY KEY,
                name              TEXT NOT NULL,
                status            TEXT NOT NULL CHECK (status IN ('draft','running','paused','completed')),
                created_at_utc    TEXT NOT NULL,
                created_by        TEXT NOT NULL,
                hypothesis        TEXT,
                primary_metric    TEXT NOT NULL DEFAULT 'reward',
                guardrail_metrics TEXT NOT NULL DEFAULT 'total_latency_ms,total_tokens,revert_count',
                deterministic_salt TEXT NOT NULL
            );

            -- experiment_variant: Variant configurations
            CREATE TABLE IF NOT EXISTS experiment_variant (
                experiment_id     TEXT NOT NULL,
                variant_id        TEXT NOT NULL,
                description       TEXT,
                weight            REAL NOT NULL CHECK (weight > 0),
                config_json       TEXT NOT NULL,
                PRIMARY KEY (experiment_id, variant_id),
                FOREIGN KEY (experiment_id) REFERENCES experiment(experiment_id) ON DELETE CASCADE
            );

            -- claim: Knowledge claims
            CREATE TABLE IF NOT EXISTS claim (
                claim_id          TEXT PRIMARY KEY,
                created_at_utc    TEXT NOT NULL,
                created_by        TEXT NOT NULL,
                statement         TEXT NOT NULL,
                scope_json        TEXT NOT NULL,
                evidence_grade    TEXT NOT NULL CHECK (evidence_grade IN ('meta','rct','quasi','cohort','correlation','anecdote','opinion','meta_analysis','case_series','anecdotal','expert_opinion')),
                confidence        REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                weighted_confidence REAL CHECK (weighted_confidence >= 0 AND weighted_confidence <= 1),
                ttl_seconds       INTEGER DEFAULT 86400,
                provenance_json   TEXT NOT NULL DEFAULT '{}',
                tags_json         TEXT DEFAULT '[]',
                area              TEXT DEFAULT 'general',
                status            TEXT DEFAULT 'active'
            );

            CREATE INDEX IF NOT EXISTS idx_claim_grade_conf
                ON claim(evidence_grade, confidence DESC);

            -- injection_claim: Claims used in injection
            CREATE TABLE IF NOT EXISTS injection_claim (
                injection_id      TEXT NOT NULL,
                section_id        TEXT NOT NULL,
                claim_id          TEXT NOT NULL,
                PRIMARY KEY (injection_id, claim_id),
                FOREIGN KEY (injection_id) REFERENCES injection_event(injection_id) ON DELETE CASCADE,
                FOREIGN KEY (claim_id) REFERENCES claim(claim_id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_injection_claim_section
                ON injection_claim(section_id);

            -- =====================================================
            -- REASONING PATTERN OUTCOMES (Evidence-Based LLM Learning)
            -- Tracks which reasoning patterns are effective to enable
            -- evidence-based evolution of LLM reasoning approaches
            -- =====================================================

            CREATE TABLE IF NOT EXISTS reasoning_pattern_outcomes (
                id                   TEXT PRIMARY KEY,
                timestamp_utc        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                injection_id         TEXT,
                query_hash           TEXT NOT NULL,
                pattern_type         TEXT NOT NULL,
                thinking_mode_used   INTEGER NOT NULL CHECK (thinking_mode_used IN (0,1)),
                thinking_chain_length INTEGER,
                was_useful           INTEGER NOT NULL CHECK (was_useful IN (0,1)),
                prevented_bug        INTEGER NOT NULL CHECK (prevented_bug IN (0,1)),
                prevented_failure    INTEGER NOT NULL CHECK (prevented_failure IN (0,1)),
                accuracy_score       REAL CHECK (accuracy_score >= 0 AND accuracy_score <= 1),
                confidence           REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                outcome_type         TEXT NOT NULL CHECK (outcome_type IN ('success','partial','failure','neutral')),
                notes                TEXT,
                FOREIGN KEY (injection_id) REFERENCES injection_event(injection_id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_reasoning_pattern_query
                ON reasoning_pattern_outcomes(query_hash, pattern_type);

            CREATE INDEX IF NOT EXISTS idx_reasoning_pattern_time
                ON reasoning_pattern_outcomes(timestamp_utc DESC);

            CREATE INDEX IF NOT EXISTS idx_reasoning_pattern_thinking
                ON reasoning_pattern_outcomes(thinking_mode_used, was_useful, outcome_type);

            CREATE INDEX IF NOT EXISTS idx_reasoning_pattern_effectiveness
                ON reasoning_pattern_outcomes(pattern_type, was_useful, outcome_type);

            -- =====================================================
            -- KNOWLEDGE QUARANTINE (Continued)
            -- =====================================================
            CREATE TABLE IF NOT EXISTS knowledge_quarantine (
                item_id           TEXT PRIMARY KEY,
                item_type         TEXT NOT NULL CHECK (item_type IN ('claim','learning','sop')),
                status            TEXT NOT NULL CHECK (status IN ('quarantined','validating','trusted','rejected')),
                created_at_utc    TEXT NOT NULL,
                updated_at_utc    TEXT NOT NULL,
                promotion_rules_json TEXT NOT NULL,
                validation_stats_json TEXT,
                notes             TEXT
            );

            -- direct_claim_outcome: Bypass injection_claim chain for promotion
            -- Records claim-level outcomes directly from capture_success/capture_failure
            -- so quarantine promotion doesn't depend on the injection pipeline.
            CREATE TABLE IF NOT EXISTS direct_claim_outcome (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id          TEXT NOT NULL,
                timestamp_utc     TEXT NOT NULL,
                success           INTEGER NOT NULL CHECK (success IN (0, 1)),
                reward            REAL NOT NULL DEFAULT 0.0,
                source            TEXT NOT NULL DEFAULT 'auto',
                notes             TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_direct_claim_outcome_claim
                ON direct_claim_outcome(claim_id, timestamp_utc DESC);

            -- =====================================================
            -- GAP 2: INJECTION LEARNING OUTCOME (Per-learning attribution)
            -- Tracks which learnings were present in each injection
            -- and links to outcomes for per-learning effectiveness.
            -- =====================================================
            CREATE TABLE IF NOT EXISTS injection_learning_outcome (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                injection_id      TEXT NOT NULL,
                learning_id       TEXT NOT NULL,
                section_id        TEXT,
                outcome_success   INTEGER CHECK (outcome_success IN (0, 1)),
                outcome_reward    REAL,
                timestamp_utc     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (injection_id) REFERENCES injection_event(injection_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_injection_learning_outcome_learning
                ON injection_learning_outcome(learning_id, outcome_success);

            -- =====================================================
            -- GAP 3: SOP OUTCOME TRACKING (Per-SOP effectiveness)
            -- Links SOPs to outcomes for evidence-based SOP ranking.
            -- =====================================================
            CREATE TABLE IF NOT EXISTS sop_outcome_link (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                sop_id            TEXT NOT NULL,
                injection_id      TEXT,
                outcome_success   INTEGER NOT NULL CHECK (outcome_success IN (0, 1)),
                outcome_reward    REAL NOT NULL DEFAULT 0.0,
                context_hash      TEXT,
                timestamp_utc     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_sop_outcome_link_sop
                ON sop_outcome_link(sop_id, timestamp_utc DESC);

            CREATE TABLE IF NOT EXISTS sop_effectiveness_rollup (
                sop_id            TEXT PRIMARY KEY,
                total_uses        INTEGER NOT NULL DEFAULT 0,
                success_count     INTEGER NOT NULL DEFAULT 0,
                failure_count     INTEGER NOT NULL DEFAULT 0,
                avg_reward        REAL NOT NULL DEFAULT 0.0,
                effectiveness     REAL NOT NULL DEFAULT 0.5,
                last_used_utc     TEXT,
                updated_at_utc    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- =====================================================
            -- GAP 5: EVIDENCE GRADE HISTORY (Auto-upgrading)
            -- Tracks grade transitions: anecdote → case_series → validated
            -- =====================================================
            CREATE TABLE IF NOT EXISTS evidence_grade_history (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id          TEXT NOT NULL,
                old_grade         TEXT NOT NULL,
                new_grade         TEXT NOT NULL,
                reason            TEXT,
                outcome_count     INTEGER NOT NULL DEFAULT 0,
                success_rate      REAL,
                timestamp_utc     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_evidence_grade_history_claim
                ON evidence_grade_history(claim_id, timestamp_utc DESC);

            -- =====================================================
            -- GAP 7: CROSS-SESSION VERIFICATION TABLE
            -- Tracks whether prior session wins persist across sessions.
            -- =====================================================
            CREATE TABLE IF NOT EXISTS cross_session_verification (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                original_win_id   TEXT NOT NULL,
                original_session  TEXT,
                verification_session TEXT NOT NULL,
                verdict           TEXT NOT NULL CHECK (verdict IN ('CONFIRMED','REGRESSED','INCONCLUSIVE')),
                evidence          TEXT,
                confidence        REAL NOT NULL DEFAULT 0.5,
                timestamp_utc     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_cross_session_verification_win
                ON cross_session_verification(original_win_id, timestamp_utc DESC);

            -- =====================================================
            -- VAULT SNAPSHOT MANIFEST (Knowledge Versioning)
            -- From vault_snapshot_manifest_sqlite.sql
            -- =====================================================

            CREATE TABLE IF NOT EXISTS vault_snapshot_manifest (
                vault_snapshot_id   TEXT PRIMARY KEY,
                created_at_utc      TEXT NOT NULL,
                schema_version      TEXT NOT NULL DEFAULT '1.0',
                manifest_sha256     TEXT NOT NULL,
                manifest_json       TEXT NOT NULL,
                mode                TEXT NOT NULL CHECK (mode IN ('light','heavy')),
                storage_engine      TEXT NOT NULL CHECK (storage_engine IN ('sqlite','postgres')),
                project_id          TEXT,
                repo_id             TEXT,
                git_head_sha        TEXT,
                dirty               INTEGER,
                notes               TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_vault_snapshot_created
                ON vault_snapshot_manifest(created_at_utc DESC);

            CREATE INDEX IF NOT EXISTS idx_vault_snapshot_repo
                ON vault_snapshot_manifest(repo_id, created_at_utc DESC);

            -- =====================================================
            -- PAYLOAD BLOB STORE (Content Deduplication)
            -- From payload_blob_store_sqlite.sql
            -- =====================================================

            CREATE TABLE IF NOT EXISTS payload_blob (
                payload_sha256      TEXT PRIMARY KEY,
                created_at_utc      TEXT NOT NULL,
                content_type        TEXT NOT NULL DEFAULT 'text/markdown',
                compressed          INTEGER NOT NULL DEFAULT 0,
                compression_algo    TEXT,
                byte_size_raw       INTEGER,
                byte_size_stored    INTEGER,
                payload_text        TEXT,
                payload_json        TEXT,
                notes               TEXT,
                CHECK (
                    (payload_text IS NOT NULL AND payload_json IS NULL)
                    OR
                    (payload_text IS NULL AND payload_json IS NOT NULL)
                )
            );

            CREATE INDEX IF NOT EXISTS idx_payload_blob_created
                ON payload_blob(created_at_utc DESC);

            -- section_blob: Per-section content storage
            CREATE TABLE IF NOT EXISTS section_blob (
                content_sha256      TEXT PRIMARY KEY,
                created_at_utc      TEXT NOT NULL,
                content_type        TEXT NOT NULL DEFAULT 'text/markdown',
                compressed          INTEGER NOT NULL DEFAULT 0,
                compression_algo    TEXT,
                byte_size_raw       INTEGER,
                byte_size_stored    INTEGER,
                content_text        TEXT,
                content_json        TEXT,
                notes               TEXT,
                CHECK (
                    (content_text IS NOT NULL AND content_json IS NULL)
                    OR
                    (content_text IS NULL AND content_json IS NOT NULL)
                )
            );

            CREATE INDEX IF NOT EXISTS idx_section_blob_created
                ON section_blob(created_at_utc DESC);

            -- =====================================================
            -- MODE SYNC STATE (Lite ↔ Heavy Data Integrity)
            -- Ensures data survives mode crashes/transitions
            -- =====================================================

            -- Singleton table tracking current mode and sync state
            CREATE TABLE IF NOT EXISTS mode_sync_state (
                id                  TEXT PRIMARY KEY DEFAULT 'singleton',
                current_mode        TEXT NOT NULL DEFAULT 'lite' CHECK (current_mode IN ('lite','heavy')),
                last_mode           TEXT CHECK (last_mode IN ('lite','heavy')),
                last_sync_at        TEXT,
                last_crash_at       TEXT,
                postgres_available  INTEGER NOT NULL DEFAULT 0,
                pending_changes     INTEGER NOT NULL DEFAULT 0,
                sync_in_progress    INTEGER NOT NULL DEFAULT 0,
                last_error          TEXT,
                updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- Initialize singleton if not exists
            INSERT OR IGNORE INTO mode_sync_state (id, current_mode, updated_at)
            VALUES ('singleton', 'lite', datetime('now'));

            -- Queue of changes to sync between modes
            CREATE TABLE IF NOT EXISTS sync_queue (
                id                  TEXT PRIMARY KEY,
                table_name          TEXT NOT NULL,
                record_id           TEXT NOT NULL,
                operation           TEXT NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
                data_json           TEXT NOT NULL,
                target_mode         TEXT NOT NULL CHECK (target_mode IN ('lite','heavy')),
                source_mode         TEXT NOT NULL CHECK (source_mode IN ('lite','heavy')),
                priority            INTEGER NOT NULL DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
                created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                attempted_at        TEXT,
                completed_at        TEXT,
                status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','failed','abandoned')),
                retry_count         INTEGER NOT NULL DEFAULT 0,
                last_error          TEXT,
                UNIQUE(table_name, record_id, operation, target_mode, status)
            );

            CREATE INDEX IF NOT EXISTS idx_sync_queue_status
                ON sync_queue(status, priority DESC, created_at);

            CREATE INDEX IF NOT EXISTS idx_sync_queue_target
                ON sync_queue(target_mode, status);

            -- Sync history for audit trail
            CREATE TABLE IF NOT EXISTS sync_history (
                id                  TEXT PRIMARY KEY,
                sync_started_at     TEXT NOT NULL,
                sync_completed_at   TEXT,
                from_mode           TEXT NOT NULL CHECK (from_mode IN ('lite','heavy')),
                to_mode             TEXT NOT NULL CHECK (to_mode IN ('lite','heavy')),
                trigger_reason      TEXT NOT NULL,
                items_queued        INTEGER NOT NULL DEFAULT 0,
                items_synced        INTEGER NOT NULL DEFAULT 0,
                items_failed        INTEGER NOT NULL DEFAULT 0,
                duration_ms         INTEGER,
                status              TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress','completed','partial','failed')),
                error_details       TEXT,
                machine_id          TEXT,
                triggered_by        TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_sync_history_time
                ON sync_history(sync_started_at DESC);

            -- =====================================================
            -- WEBHOOK DESTINATION TRACKING
            -- Tracks webhook deliveries to various IDE/agent endpoints
            -- =====================================================

            CREATE TABLE IF NOT EXISTS webhook_destination (
                destination_id       TEXT PRIMARY KEY,
                destination_name     TEXT NOT NULL UNIQUE,
                destination_type     TEXT NOT NULL CHECK (destination_type IN (
                    'ide', 'agent', 'llm', 'monitor', 'external'
                )),
                endpoint_url         TEXT,
                is_active            INTEGER NOT NULL DEFAULT 1,
                created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_delivery_at     TEXT,
                total_deliveries     INTEGER NOT NULL DEFAULT 0,
                successful_deliveries INTEGER NOT NULL DEFAULT 0,
                failed_deliveries    INTEGER NOT NULL DEFAULT 0,
                avg_latency_ms       REAL,
                config_json          TEXT,
                notes                TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_webhook_dest_type
                ON webhook_destination(destination_type);
            CREATE INDEX IF NOT EXISTS idx_webhook_dest_active
                ON webhook_destination(is_active);

            -- Webhook delivery event tracking
            CREATE TABLE IF NOT EXISTS webhook_delivery_event (
                delivery_id          TEXT PRIMARY KEY,
                destination_id       TEXT NOT NULL,
                injection_id         TEXT,
                timestamp_utc        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                webhook_phase        TEXT NOT NULL CHECK (webhook_phase IN ('pre_message', 'post_message', 'on_demand')),
                section_ids          TEXT,  -- JSON array of section IDs
                payload_sha256       TEXT NOT NULL,
                status               TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'failed', 'timeout')),
                latency_ms           INTEGER,
                response_code        INTEGER,
                error_message        TEXT,
                retry_count          INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (destination_id) REFERENCES webhook_destination(destination_id)
            );

            CREATE INDEX IF NOT EXISTS idx_webhook_delivery_dest
                ON webhook_delivery_event(destination_id, timestamp_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_webhook_delivery_status
                ON webhook_delivery_event(status);
            CREATE INDEX IF NOT EXISTS idx_webhook_delivery_injection
                ON webhook_delivery_event(injection_id);

            -- =====================================================
            -- TECH STACK VERSION REGISTRY
            -- Tracks versions of all system components for compatibility
            -- =====================================================

            CREATE TABLE IF NOT EXISTS tech_stack_version_registry (
                component_name       TEXT PRIMARY KEY,
                component_type       TEXT NOT NULL CHECK (component_type IN (
                    'database', 'cache', 'queue', 'container', 'llm',
                    'api', 'service', 'extension', 'library', 'runtime'
                )),
                current_version      TEXT NOT NULL,
                min_compatible_ver   TEXT,
                max_compatible_ver   TEXT,
                first_seen_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_verified_at     TEXT,
                auto_detected        INTEGER NOT NULL DEFAULT 0,
                detection_method     TEXT,
                health_status        TEXT DEFAULT 'unknown' CHECK (health_status IN (
                    'healthy', 'degraded', 'down', 'unknown'
                )),
                metadata_json        TEXT,
                notes                TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_tech_stack_type
                ON tech_stack_version_registry(component_type);
            CREATE INDEX IF NOT EXISTS idx_tech_stack_health
                ON tech_stack_version_registry(health_status);
            CREATE INDEX IF NOT EXISTS idx_tech_stack_updated
                ON tech_stack_version_registry(last_updated_at DESC);

            -- =================================================================
            -- CONTAINER DIAGNOSTICS (Butler Handyman — LLM-assisted)
            -- Records Docker container issues, LLM analysis, and fix outcomes.
            -- Reusable for any app/service troubleshooting.
            -- =================================================================
            CREATE TABLE IF NOT EXISTS container_diagnostic (
                diagnostic_id        TEXT PRIMARY KEY,
                timestamp_utc        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                container_name       TEXT NOT NULL,
                container_image      TEXT,
                container_status     TEXT NOT NULL,
                error_logs           TEXT,
                llm_analysis         TEXT,
                suggested_fix        TEXT,
                fix_commands_json    TEXT DEFAULT '[]',
                fix_attempted        INTEGER NOT NULL DEFAULT 0,
                fix_succeeded        INTEGER,
                fix_outcome          TEXT,
                resolved_at_utc      TEXT,
                llm_provider         TEXT DEFAULT 'vllm-mlx',
                llm_confidence       REAL DEFAULT 0.0
            );

            CREATE INDEX IF NOT EXISTS idx_container_diag_name
                ON container_diagnostic(container_name);
            CREATE INDEX IF NOT EXISTS idx_container_diag_status
                ON container_diagnostic(container_status);
            CREATE INDEX IF NOT EXISTS idx_container_diag_time
                ON container_diagnostic(timestamp_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_container_diag_unresolved
                ON container_diagnostic(fix_succeeded) WHERE fix_succeeded IS NULL;

            -- =================================================================
            -- BUTLER CODE NOTES (Handyman's Notepad for Batman)
            -- The butler observes code issues during background rounds
            -- and accumulates them for Atlas to review and fix.
            -- =================================================================
            CREATE TABLE IF NOT EXISTS butler_code_note (
                note_id              TEXT PRIMARY KEY,
                timestamp_utc        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                source_job           TEXT NOT NULL,
                file_path            TEXT,
                line_number          INTEGER,
                -- Anatomic location: which "room" of the mansion
                anatomic_location    TEXT NOT NULL DEFAULT 'general' CHECK (anatomic_location IN (
                    'nervous_system',    -- Scheduler, Celery, coordinator
                    'brain',             -- Intelligence: professor, LLM, synaptic
                    'circulatory',       -- Injection pipeline, webhooks, hooks
                    'memory',            -- Storage: observability, knowledge graph
                    'skeleton',          -- Infrastructure: Docker, postgres, redis
                    'immune_system',     -- Health, recovery, troubleshooting
                    'eyes_ears',         -- Detection: destinations, boundary intel
                    'voice',             -- Communication: chat server, heartbeat
                    'digestive',         -- Evidence pipeline: claims, quarantine
                    'general'            -- Uncategorized
                )),
                error_type           TEXT NOT NULL CHECK (error_type IN (
                    'syntax_error', 'import_error', 'runtime_error',
                    'type_error', 'logic_error', 'config_error',
                    'performance', 'security', 'deprecation', 'other'
                )),
                severity             TEXT NOT NULL DEFAULT 'info' CHECK (severity IN (
                    'critical', 'warning', 'info'
                )),
                error_message        TEXT NOT NULL,
                traceback            TEXT,
                llm_analysis         TEXT,
                suggested_fix        TEXT,
                llm_confidence       REAL DEFAULT 0.0,
                reviewed_by_atlas    INTEGER NOT NULL DEFAULT 0,
                reviewed_at_utc      TEXT,
                atlas_action         TEXT,
                occurrence_count     INTEGER NOT NULL DEFAULT 1,
                last_seen_utc        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_code_note_severity
                ON butler_code_note(severity);
            CREATE INDEX IF NOT EXISTS idx_code_note_unreviewed
                ON butler_code_note(reviewed_by_atlas) WHERE reviewed_by_atlas = 0;
            CREATE INDEX IF NOT EXISTS idx_code_note_file
                ON butler_code_note(file_path);
            CREATE INDEX IF NOT EXISTS idx_code_note_time
                ON butler_code_note(last_seen_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_code_note_type
                ON butler_code_note(error_type);
            CREATE INDEX IF NOT EXISTS idx_code_note_location
                ON butler_code_note(anatomic_location);

            -- sop_outcome_rollup: Track SOP effectiveness from outcome data
            -- Bridges the gap: SOPs were promoted by time/confidence,
            -- NOT by whether they actually helped when injected.
            CREATE TABLE IF NOT EXISTS sop_outcome_rollup (
                sop_id              TEXT PRIMARY KEY,
                sop_title           TEXT NOT NULL,
                n                   INTEGER NOT NULL DEFAULT 0,
                success_count       INTEGER NOT NULL DEFAULT 0,
                failure_count       INTEGER NOT NULL DEFAULT 0,
                success_rate        REAL NOT NULL DEFAULT 0.0,
                avg_reward          REAL NOT NULL DEFAULT 0.0,
                reliability_score   REAL NOT NULL DEFAULT 0.5,
                last_outcome_ts     TEXT,
                updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- sop_outcome_link: Links individual SOPs to outcome events
            CREATE TABLE IF NOT EXISTS sop_outcome_link (
                sop_id              TEXT NOT NULL,
                outcome_event_id    TEXT NOT NULL,
                session_id          TEXT,
                timestamp_utc       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(sop_id, outcome_event_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sop_outcome_link_sop
                ON sop_outcome_link(sop_id);

            -- Seed known webhook destinations
            INSERT OR IGNORE INTO webhook_destination (destination_id, destination_name, destination_type, notes) VALUES
                ('vs_code_claude_code', 'VS Code Claude Code', 'ide', 'Primary development IDE integration'),
                ('synaptic_chat', 'Synaptic Chat', 'agent', '8th Intelligence subconscious agent'),
                ('chatgpt_app', 'ChatGPT App', 'llm', 'OpenAI ChatGPT integration'),
                ('cursor_overseer', 'Cursor Overseer', 'ide', 'Cursor IDE context injection'),
                ('antigravity', 'Antigravity', 'agent', 'Experimental gravity-defying agent');
        """)
        self._safe_commit()

        # Migrate existing claim table if missing columns (schema drift fix)
        self._migrate_claim_table()
        self._migrate_sync_history_table()

    def _migrate_claim_table(self):
        """Add missing columns to claim table for existing DBs."""
        cursor = self._sqlite_conn.execute("PRAGMA table_info(claim)")
        existing_cols = {row[1] for row in cursor.fetchall()}

        migrations = [
            ("weighted_confidence", "REAL"),
            ("tags_json", "TEXT DEFAULT '[]'"),
            ("area", "TEXT DEFAULT 'general'"),
            ("status", "TEXT DEFAULT 'active'"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                try:
                    self._sqlite_conn.execute(
                        f"ALTER TABLE claim ADD COLUMN {col_name} {col_def}"
                    )
                except Exception:
                    pass  # Column may already exist from concurrent init
        self._safe_commit()

    def _migrate_sync_history_table(self):
        """Add missing columns to sync_history table for existing DBs."""
        try:
            cursor = self._sqlite_conn.execute("PRAGMA table_info(sync_history)")
            existing_cols = {row[1] for row in cursor.fetchall()}
        except Exception:
            return  # Table may not exist yet

        migrations = [
            ("machine_id", "TEXT"),
            ("triggered_by", "TEXT"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                try:
                    self._sqlite_conn.execute(
                        f"ALTER TABLE sync_history ADD COLUMN {col_name} {col_def}"
                    )
                except Exception:
                    pass  # Column may already exist
        self._safe_commit()

    def _init_postgres(self):
        """Initialize PostgreSQL connection for heavy mode."""
        try:
            import psycopg2
            from psycopg2.extras import Json

            # Try to connect to Context DNA PostgreSQL
            self._pg_conn = psycopg2.connect(
                dbname="context_dna",
                user="context_dna",
                password=os.getenv("POSTGRES_PASSWORD", "context_dna_dev"),
                host="127.0.0.1",
                port=5432
            )

            # Tables should already exist from migrations
            # Just verify connection
            with self._pg_conn.cursor() as cur:
                cur.execute("SELECT 1")

        except Exception as e:
            # Fall back to SQLite only
            self._pg_conn = None
            print(f"PostgreSQL unavailable, using SQLite: {e}")

    def _utc_now(self) -> str:
        """Get current UTC timestamp as ISO string."""
        return datetime.now(timezone.utc).isoformat()

    def _generate_id(self) -> str:
        """Generate a unique event ID."""
        return str(uuid.uuid4())

    def _safe_commit(self):
        """Commit with write lock protection (thread-safe)."""
        with self._write_lock:
            self._safe_commit()

    # =========================================================================
    # DEPENDENCY STATUS RECORDING
    # =========================================================================

    def record_dependency_status(
        self,
        dependency_name: str,
        status: Literal["healthy", "degraded", "down"],
        latency_ms: Optional[int] = None,
        reason_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Record a dependency health check result.

        Args:
            dependency_name: Service name (redis, postgres, llm, celery, etc.)
            status: healthy, degraded, or down
            latency_ms: Response time in milliseconds
            reason_code: Error code if not healthy (e.g., REDIS_TIMEOUT)
            details: Additional details as dict

        Returns:
            Event ID
        """
        event = DependencyStatusEvent(
            dep_event_id=self._generate_id(),
            timestamp_utc=self._utc_now(),
            mode="heavy" if self.mode == "heavy" else "lite",
            orchestrator_version=ORCHESTRATOR_VERSION,
            dependency_name=dependency_name,
            status=status,
            latency_ms=latency_ms,
            reason_code=reason_code,
            details_json=json.dumps(details) if details else None
        )

        # Write to SQLite
        self._sqlite_conn.execute("""
            INSERT INTO dependency_status_event
            (dep_event_id, timestamp_utc, mode, orchestrator_version,
             dependency_name, status, latency_ms, reason_code, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.dep_event_id, event.timestamp_utc, event.mode,
            event.orchestrator_version, event.dependency_name, event.status,
            event.latency_ms, event.reason_code, event.details_json
        ))
        self._safe_commit()

        # Also write to PostgreSQL if available
        if self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO dependency_status_event
                        (dep_event_id, timestamp_utc, mode, orchestrator_version,
                         dependency_name, status, latency_ms, reason_code, details_json)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        event.dep_event_id, event.timestamp_utc, event.mode,
                        event.orchestrator_version, event.dependency_name, event.status,
                        event.latency_ms, event.reason_code, event.details_json
                    ))
                self._pg_conn.commit()
            except Exception as e:
                # Log but don't fail - SQLite has the data
                print(f"PostgreSQL write failed: {e}")

        return event.dep_event_id

    def record_infra_status(self, infra_status) -> List[str]:
        """
        Record all dependency statuses from an InfraStatus snapshot.

        Args:
            infra_status: InfraStatus dataclass from mutual_heartbeat

        Returns:
            List of event IDs created
        """
        event_ids = []

        # Map InfraStatus fields to dependency names
        dependencies = [
            ("docker", infra_status.docker.docker_running, None),
            ("postgres", infra_status.docker.postgres_available, None),
            ("redis", infra_status.redis_running, None),
            ("rabbitmq", infra_status.rabbitmq_running, None),
            ("celery_workers", infra_status.celery_workers_running,
             {"count": infra_status.celery_worker_count}),
            ("celery_beat", infra_status.celery_beat_running, None),
            ("seaweedfs", infra_status.seaweedfs_running, None),
            ("ollama", infra_status.ollama_running, None),
            ("llm_server", infra_status.llm_server_running,
             {"model": infra_status.llm_server_model, "port": infra_status.llm_server_port}),
            ("voice_server", infra_status.voice_server_running, None),
            ("agent_service", infra_status.agent_service_running, None),
        ]

        for dep_name, is_healthy, details in dependencies:
            status = "healthy" if is_healthy else "down"
            reason_code = None

            if not is_healthy:
                reason_code = f"{dep_name.upper()}_DOWN"

            event_id = self.record_dependency_status(
                dependency_name=dep_name,
                status=status,
                reason_code=reason_code,
                details=details
            )
            event_ids.append(event_id)

        return event_ids

    # =========================================================================
    # TASK RUN RECORDING
    # =========================================================================

    def record_task_run(
        self,
        task_name: str,
        run_type: Literal["scheduled", "manual", "recovery"],
        status: Literal["success", "partial", "failure", "skipped"],
        duration_ms: int,
        budget_ms: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Record a task execution event.

        Args:
            task_name: Name of the task (brain_cycle, scan_project, etc.)
            run_type: scheduled, manual, or recovery
            status: success, partial, failure, or skipped
            duration_ms: Execution time in milliseconds
            budget_ms: Optional time budget
            details: Additional details
            error: Error information if failed

        Returns:
            Event ID
        """
        event = TaskRunEvent(
            task_run_id=self._generate_id(),
            timestamp_utc=self._utc_now(),
            mode="heavy" if self.mode == "heavy" else "lite",
            orchestrator_version=ORCHESTRATOR_VERSION,
            task_name=task_name,
            run_type=run_type,
            status=status,
            duration_ms=duration_ms,
            budget_ms=budget_ms,
            details_json=json.dumps(details) if details else None,
            error_json=json.dumps(error) if error else None
        )

        # Write to SQLite
        self._sqlite_conn.execute("""
            INSERT INTO task_run_event
            (task_run_id, timestamp_utc, mode, orchestrator_version,
             task_name, run_type, status, duration_ms, budget_ms, details_json, error_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.task_run_id, event.timestamp_utc, event.mode,
            event.orchestrator_version, event.task_name, event.run_type,
            event.status, event.duration_ms, event.budget_ms,
            event.details_json, event.error_json
        ))
        self._safe_commit()

        # Also write to PostgreSQL if available
        if self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO task_run_event
                        (task_run_id, timestamp_utc, mode, orchestrator_version,
                         task_name, run_type, status, duration_ms, budget_ms, details_json, error_json)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        event.task_run_id, event.timestamp_utc, event.mode,
                        event.orchestrator_version, event.task_name, event.run_type,
                        event.status, event.duration_ms, event.budget_ms,
                        event.details_json, event.error_json
                    ))
                self._pg_conn.commit()
            except Exception as e:
                print(f"PostgreSQL write failed: {e}")

        return event.task_run_id

    def record_recovery_attempt(
        self,
        service_name: str,
        success: bool,
        duration_ms: int,
        message: str
    ) -> str:
        """
        Record a recovery attempt as a task run.

        Args:
            service_name: Service being recovered (llm_server, celery, etc.)
            success: Whether recovery succeeded
            duration_ms: Time taken
            message: Result message

        Returns:
            Event ID
        """
        return self.record_task_run(
            task_name=f"recover_{service_name}",
            run_type="recovery",
            status="success" if success else "failure",
            duration_ms=duration_ms,
            details={"message": message, "service": service_name},
            error={"message": message} if not success else None
        )

    # =========================================================================
    # LITE MODE JOB SCHEDULER
    # =========================================================================

    def register_job(self, job_name: str, interval_s: int):
        """
        Register a job for lite mode scheduling.

        Args:
            job_name: Unique job identifier
            interval_s: Interval between runs in seconds
        """
        next_run = datetime.now(timezone.utc).isoformat()

        self._sqlite_conn.execute("""
            INSERT OR REPLACE INTO job_schedule
            (job_name, interval_s, next_run_utc)
            VALUES (?, ?, ?)
        """, (job_name, interval_s, next_run))
        self._safe_commit()

    def get_due_jobs(self) -> List[JobScheduleEntry]:
        """Get all jobs that are due to run."""
        now = self._utc_now()

        cursor = self._sqlite_conn.execute("""
            SELECT * FROM job_schedule
            WHERE next_run_utc <= ?
            ORDER BY next_run_utc
        """, (now,))

        jobs = []
        for row in cursor:
            jobs.append(JobScheduleEntry(
                job_name=row["job_name"],
                interval_s=row["interval_s"],
                next_run_utc=row["next_run_utc"],
                last_run_utc=row["last_run_utc"],
                last_status=row["last_status"],
                last_error=row["last_error"]
            ))

        return jobs

    def mark_job_complete(
        self,
        job_name: str,
        status: str,
        error: Optional[str] = None
    ):
        """
        Mark a job as completed and schedule next run.

        Args:
            job_name: Job identifier
            status: "success" or "failure"
            error: Error message if failed
        """
        now = datetime.now(timezone.utc)

        # Get current interval
        cursor = self._sqlite_conn.execute(
            "SELECT interval_s FROM job_schedule WHERE job_name = ?",
            (job_name,)
        )
        row = cursor.fetchone()
        if not row:
            return

        interval_s = row["interval_s"]
        next_run = (now + __import__('datetime').timedelta(seconds=interval_s)).isoformat()

        self._sqlite_conn.execute("""
            UPDATE job_schedule
            SET last_run_utc = ?, last_status = ?, last_error = ?, next_run_utc = ?
            WHERE job_name = ?
        """, (now.isoformat(), status, error, next_run, job_name))
        self._safe_commit()

    # =========================================================================
    # METRICS ROLLUP COMPUTATION
    # =========================================================================

    def compute_variant_summary_rollup(
        self,
        start_utc: str,
        end_utc: str
    ) -> int:
        """
        Compute variant summary rollup for a time window.

        Requires injection_event and outcome_event tables to exist.

        Args:
            start_utc: Window start (ISO8601)
            end_utc: Window end (ISO8601)

        Returns:
            Number of rows inserted/updated
        """
        # Check if required tables exist
        cursor = self._sqlite_conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='injection_event'
        """)
        if not cursor.fetchone():
            return 0  # Core tables not present yet

        # Compute rollup per spec (sqlite_metrics_rollups.sql)
        self._sqlite_conn.execute("""
            INSERT OR REPLACE INTO variant_summary_rollup
            SELECT
                ie.experiment_id,
                ie.variant_id,
                ? AS window_start_utc,
                ? AS window_end_utc,
                COUNT(*) AS n,
                AVG(COALESCE(oe.reward, 0)) AS avg_reward,
                AVG(CASE WHEN COALESCE(oe.success,0)=1 THEN 1.0 ELSE 0.0 END) AS success_rate,
                AVG(ie.total_latency_ms) AS avg_latency_ms,
                AVG(ie.total_tokens) AS avg_tokens,
                AVG(COALESCE(oe.retry_count,0)) AS avg_retry,
                AVG(COALESCE(oe.undo_count,0)) AS avg_undo,
                AVG(COALESCE(oe.revert_count,0)) AS avg_revert,
                AVG(CASE
                    WHEN oe.error_signature_before IS NOT NULL
                     AND oe.error_signature_after IS NOT NULL
                     AND oe.error_signature_before = oe.error_signature_after
                    THEN 1.0 ELSE 0.0
                END) AS error_recurrence_rate
            FROM injection_event ie
            LEFT JOIN (
                SELECT o1.*
                FROM outcome_event o1
                JOIN (
                    SELECT injection_id, MAX(timestamp_utc) AS max_ts
                    FROM outcome_event
                    GROUP BY injection_id
                ) latest
                ON latest.injection_id = o1.injection_id AND latest.max_ts = o1.timestamp_utc
            ) oe ON oe.injection_id = ie.injection_id
            WHERE ie.experiment_id IS NOT NULL
              AND ie.variant_id IS NOT NULL
              AND ie.timestamp_utc >= ?
              AND ie.timestamp_utc < ?
            GROUP BY ie.experiment_id, ie.variant_id
        """, (start_utc, end_utc, start_utc, end_utc))

        rows = self._sqlite_conn.total_changes
        self._safe_commit()
        return rows

    def compute_variant_vs_baseline_rollup(
        self,
        start_utc: str,
        end_utc: str
    ) -> int:
        """
        Compute variant vs baseline deltas for a time window.

        Requires variant_summary_rollup and experiment_baseline to be populated.

        Args:
            start_utc: Window start (ISO8601)
            end_utc: Window end (ISO8601)

        Returns:
            Number of rows inserted/updated
        """
        self._sqlite_conn.execute("""
            INSERT OR REPLACE INTO variant_vs_baseline_rollup
            WITH base AS (
                SELECT
                    vs.experiment_id,
                    eb.baseline_variant_id,
                    vs.window_start_utc,
                    vs.window_end_utc,
                    vs.avg_latency_ms AS base_latency,
                    vs.avg_tokens AS base_tokens,
                    vs.avg_revert AS base_revert,
                    vs.error_recurrence_rate AS base_error_recur
                FROM variant_summary_rollup vs
                JOIN experiment_baseline eb ON eb.experiment_id = vs.experiment_id
                WHERE vs.variant_id = eb.baseline_variant_id
                  AND vs.window_start_utc = ?
                  AND vs.window_end_utc = ?
            )
            SELECT
                vs.experiment_id,
                vs.variant_id,
                ? AS window_start_utc,
                ? AS window_end_utc,
                vs.n,
                vs.avg_reward,
                vs.success_rate,
                vs.avg_latency_ms,
                vs.avg_tokens,
                vs.avg_revert,
                vs.error_recurrence_rate,
                CASE WHEN base.base_latency > 0
                     THEN (vs.avg_latency_ms - base.base_latency) / base.base_latency
                     ELSE NULL END AS latency_pct_delta,
                CASE WHEN base.base_tokens > 0
                     THEN (vs.avg_tokens - base.base_tokens) / base.base_tokens
                     ELSE NULL END AS tokens_pct_delta,
                (vs.avg_revert - base.base_revert) AS revert_abs_delta,
                (vs.error_recurrence_rate - base.base_error_recur) AS error_recur_abs_delta
            FROM variant_summary_rollup vs
            JOIN base ON base.experiment_id = vs.experiment_id
            WHERE vs.window_start_utc = ?
              AND vs.window_end_utc = ?
        """, (start_utc, end_utc, start_utc, end_utc, start_utc, end_utc))

        rows = self._sqlite_conn.total_changes
        self._safe_commit()
        return rows

    def compute_section_metrics_rollup(
        self,
        start_utc: str,
        end_utc: str
    ) -> int:
        """
        Compute section-level metrics rollup for a time window.

        Args:
            start_utc: Window start (ISO8601)
            end_utc: Window end (ISO8601)

        Returns:
            Number of rows inserted/updated
        """
        # Check if required tables exist
        cursor = self._sqlite_conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='injection_section'
        """)
        if not cursor.fetchone():
            return 0

        self._sqlite_conn.execute("""
            INSERT OR REPLACE INTO section_metrics_rollup
            SELECT
                ie.experiment_id,
                isec.section_id,
                isec.version,
                ? AS window_start_utc,
                ? AS window_end_utc,
                COUNT(*) AS n,
                AVG(isec.latency_ms) AS avg_latency_ms,
                AVG(isec.tokens) AS avg_tokens,
                AVG(CASE WHEN isec.health='healthy' THEN 1.0 ELSE 0.0 END) AS healthy_rate
            FROM injection_event ie
            JOIN injection_section isec ON isec.injection_id = ie.injection_id
            WHERE ie.experiment_id IS NOT NULL
              AND ie.timestamp_utc >= ?
              AND ie.timestamp_utc < ?
            GROUP BY ie.experiment_id, isec.section_id, isec.version
        """, (start_utc, end_utc, start_utc, end_utc))

        rows = self._sqlite_conn.total_changes
        self._safe_commit()
        return rows

    def compute_claim_outcome_rollup(
        self,
        start_utc: str,
        end_utc: str
    ) -> int:
        """
        Compute claim outcome rollup for a time window.

        Args:
            start_utc: Window start (ISO8601)
            end_utc: Window end (ISO8601)

        Returns:
            Number of rows inserted/updated
        """
        # Check if required tables exist
        cursor = self._sqlite_conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='claim'
        """)
        if not cursor.fetchone():
            return 0

        self._sqlite_conn.execute("""
            INSERT OR REPLACE INTO claim_outcome_rollup
            SELECT
                ic.claim_id,
                c.evidence_grade,
                c.confidence,
                ? AS window_start_utc,
                ? AS window_end_utc,
                COUNT(*) AS n,
                AVG(COALESCE(oe.reward, 0)) AS avg_reward,
                AVG(CASE WHEN COALESCE(oe.success,0)=1 THEN 1.0 ELSE 0.0 END) AS success_rate
            FROM injection_claim ic
            JOIN claim c ON c.claim_id = ic.claim_id
            JOIN injection_event ie ON ie.injection_id = ic.injection_id
            LEFT JOIN (
                SELECT o1.*
                FROM outcome_event o1
                JOIN (
                    SELECT injection_id, MAX(timestamp_utc) AS max_ts
                    FROM outcome_event
                    GROUP BY injection_id
                ) latest
                ON latest.injection_id = o1.injection_id AND latest.max_ts = o1.timestamp_utc
            ) oe ON oe.injection_id = ie.injection_id
            WHERE ie.timestamp_utc >= ?
              AND ie.timestamp_utc < ?
            GROUP BY ic.claim_id, c.evidence_grade, c.confidence
        """, (start_utc, end_utc, start_utc, end_utc))

        rows = self._sqlite_conn.total_changes
        self._safe_commit()
        return rows

    def compute_all_rollups(self, window_minutes: int = 60) -> Dict[str, int]:
        """
        Compute all rollups for the last N minutes.

        Args:
            window_minutes: Size of the rolling window

        Returns:
            Dict of rollup_name -> rows_affected
        """
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=window_minutes)).isoformat()
        end = now.isoformat()

        results = {}

        # Order matters: variant_summary must run before variant_vs_baseline
        results["variant_summary"] = self.compute_variant_summary_rollup(start, end)
        results["variant_vs_baseline"] = self.compute_variant_vs_baseline_rollup(start, end)
        results["section_metrics"] = self.compute_section_metrics_rollup(start, end)
        results["claim_outcome"] = self.compute_claim_outcome_rollup(start, end)

        return results

    # =========================================================================
    # GUARDRAIL RECORDING
    # =========================================================================

    def record_guardrail_stop(
        self,
        experiment_id: str,
        variant_id: str,
        severity: Literal["soft", "hard"],
        metric_name: str,
        window_n: int,
        baseline_value: Optional[float],
        variant_value: Optional[float],
        delta_value: Optional[float],
        threshold_value: Optional[float],
        action_taken: str,
        baseline_variant_id: Optional[str] = None,
        notes: Optional[str] = None
    ) -> str:
        """
        Record a guardrail stop event (experiment auto-stopped).

        Args:
            experiment_id: Experiment that was stopped
            variant_id: Variant that triggered the stop
            severity: "soft" (pause) or "hard" (terminate)
            metric_name: Which metric exceeded threshold
            window_n: Sample size in the window
            baseline_value: Baseline metric value
            variant_value: Variant metric value
            delta_value: Calculated delta
            threshold_value: Threshold that was exceeded
            action_taken: Description of action taken
            baseline_variant_id: Which variant was baseline
            notes: Additional context

        Returns:
            Stop event ID
        """
        stop_id = self._generate_id()

        now = self._utc_now()
        self._sqlite_conn.execute("""
            INSERT INTO guardrail_stop_event
            (stop_id, timestamp_utc, experiment_id, variant_id, baseline_variant_id,
             severity, metric_name, window_n, baseline_value, variant_value,
             delta_value, threshold_value, action_taken, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stop_id, now, experiment_id, variant_id, baseline_variant_id,
            severity, metric_name, window_n, baseline_value, variant_value,
            delta_value, threshold_value, action_taken, notes
        ))
        self._safe_commit()

        self._pg_mirror_row("guardrail_stop_event", {
            "stop_id": stop_id, "timestamp_utc": now,
            "experiment_id": experiment_id, "variant_id": variant_id,
            "baseline_variant_id": baseline_variant_id,
            "severity": severity, "metric_name": metric_name,
            "window_n": window_n, "baseline_value": baseline_value,
            "variant_value": variant_value, "delta_value": delta_value,
            "threshold_value": threshold_value,
            "action_taken": action_taken, "notes": notes,
        })

        return stop_id

    def get_guardrail_stops(
        self,
        experiment_id: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get recent guardrail stop events."""
        if experiment_id:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM guardrail_stop_event
                WHERE experiment_id = ?
                ORDER BY timestamp_utc DESC
                LIMIT ?
            """, (experiment_id, limit))
        else:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM guardrail_stop_event
                ORDER BY timestamp_utc DESC
                LIMIT ?
            """, (limit,))

        return [dict(row) for row in cursor]

    # =========================================================================
    # DETERMINISM VIOLATION RECORDING
    # =========================================================================

    def record_determinism_violation(
        self,
        input_hash: str,
        prompt_preview: str,
        preset: str,
        hash_1: str,
        hash_2: str,
    ) -> str:
        """
        Record a determinism violation (same input produced different payloads).

        This is a critical guardrail event — determinism is a constitutional
        requirement per Atlas MUST #1.

        Args:
            input_hash: Hash of the input snapshot
            prompt_preview: First 100 chars of the prompt
            preset: Injection preset used
            hash_1: SHA256 of first payload
            hash_2: SHA256 of second payload

        Returns:
            Event ID
        """
        event_id = self._generate_id()
        context = json.dumps({
            "input_hash": input_hash,
            "prompt_preview": prompt_preview,
            "preset": preset,
            "hash_1": hash_1,
            "hash_2": hash_2,
        })

        self._sqlite_conn.execute("""
            INSERT INTO guardrail_stop_event
            (stop_id, timestamp_utc, experiment_id, variant_id,
             severity, metric_name, window_n, baseline_value, variant_value,
             delta_value, threshold_value, action_taken, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event_id, self._utc_now(),
            "determinism_validator", preset,
            "hard", "payload_sha256_mismatch", 2,
            None, None, None, None,
            "logged_violation", context
        ))
        self._safe_commit()

        return event_id

    # =========================================================================
    # EVIDENCE-GRADED CLAIM RECORDING
    # =========================================================================

    def record_claim_with_evidence(
        self,
        claim_text: str,
        evidence_grade: str,
        source: str,
        confidence: float = 0.5,
        tags: Optional[List[str]] = None,
        area: str = "general",
    ) -> str:
        """
        Record a claim with evidence grade weighting, routed through quarantine.

        New claims start as quarantined and must earn trusted status through
        evidence accumulation. The lite_scheduler's _run_evaluate_quarantine
        promotes claims when n>=10 and success_rate>=0.7.

        Flow: claim INSERT (status='quarantined') -> knowledge_quarantine entry
              -> (later) evaluate_quarantine promotes to 'active'/'trusted'

        Falls back to status='active' if quarantine system fails,
        preserving backward compatibility.

        Args:
            claim_text: The claim or learning being recorded
            evidence_grade: EBM grade string (meta_analysis, rct, cohort, anecdotal, etc.)
            source: Where this claim originated
            confidence: Base confidence (0-1), will be adjusted by evidence weight
            tags: Optional tags for categorization
            area: Architecture area

        Returns:
            Claim ID
        """
        grade = EvidenceGrade.from_string(evidence_grade)
        weighted_confidence = grade.apply_to_confidence(confidence)
        claim_id = self._generate_id()

        # Route through quarantine by default
        initial_status = "quarantined"

        now = self._utc_now()
        scope_json = json.dumps({"area": area, "tags": tags or []})
        provenance_json = json.dumps({"source": source, "method": "capture_success"})
        tags_json = json.dumps(tags or [])

        self._sqlite_conn.execute("""
            INSERT INTO claim
            (claim_id, created_at_utc, created_by, statement, scope_json,
             evidence_grade, confidence, weighted_confidence, ttl_seconds,
             provenance_json, tags_json, area, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            claim_id, now, source, claim_text, scope_json,
            grade.db_name, confidence, weighted_confidence, 86400,
            provenance_json, tags_json, area, initial_status
        ))
        self._safe_commit()

        self._pg_mirror_row("claim", {
            "claim_id": claim_id, "created_at_utc": now,
            "created_by": source, "statement": claim_text,
            "scope_json": scope_json, "evidence_grade": grade.db_name,
            "confidence": confidence, "weighted_confidence": weighted_confidence,
            "ttl_seconds": 86400, "provenance_json": provenance_json,
            "tags_json": tags_json, "area": area, "status": initial_status,
        })

        # Register in quarantine table for promotion/rejection tracking
        try:
            is_sop = claim_text.startswith("[") and "SOP]" in claim_text.upper()
            item_type = "sop" if is_sop else "claim"
            self.quarantine_item(
                item_id=claim_id,
                item_type=item_type,
                promotion_rules={
                    "min_confirmations": 2,
                    "min_hours": 24,
                    "auto_promote_on": "repeated_success"
                },
                notes=f"Quarantine intake via record_claim_with_evidence: {claim_text[:80]}"
            )
        except Exception:
            # Fallback: if quarantine table insert fails, promote claim to active
            # so the system degrades gracefully rather than losing data
            self._sqlite_conn.execute(
                "UPDATE claim SET status = 'active' WHERE claim_id = ?",
                (claim_id,)
            )
            self._safe_commit()

        return claim_id

    def get_claims_by_status(
        self,
        status: str,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Retrieve claims filtered by status.

        Args:
            status: Claim status to filter by (e.g. 'flagged_for_review', 'active')
            limit: Maximum number of claims to return

        Returns:
            List of claim dicts with parsed JSON fields
        """
        cursor = self._sqlite_conn.execute("""
            SELECT * FROM claim
            WHERE status = ?
            ORDER BY created_at_utc DESC
            LIMIT ?
        """, (status, limit))

        results = []
        for row in cursor:
            item = dict(row)
            for json_field in ("scope_json", "provenance_json", "tags_json"):
                if item.get(json_field):
                    try:
                        item[json_field.replace("_json", "")] = json.loads(item[json_field])
                    except (json.JSONDecodeError, TypeError) as e:
                        print(f"[WARN] JSON field parse failed for {json_field}: {e}")
            results.append(item)
        return results

    def update_claim_status(
        self,
        claim_id: str,
        new_status: str
    ) -> bool:
        """
        Update the status of a claim.

        Args:
            claim_id: The claim to update
            new_status: New status string (e.g. 'applied_to_wisdom', 'active')

        Returns:
            True if the claim was found and updated
        """
        cursor = self._sqlite_conn.execute("""
            UPDATE claim SET status = ? WHERE claim_id = ?
        """, (new_status, claim_id))
        self._safe_commit()
        return cursor.rowcount > 0

    def enforce_ttl_decay(self) -> Dict[str, int]:
        """
        Expire claims past their TTL.

        Transitions active/quarantined claims to 'expired' when
        julianday(now) - julianday(created_at_utc) exceeds ttl_seconds.
        Claims with ttl_seconds <= 0 are exempt (infinite TTL).

        Returns:
            Dict with 'expired' (rows transitioned) and 'kept' (rows still live).
        """
        cursor = self._sqlite_conn.execute("""
            UPDATE claim SET status = 'expired'
            WHERE status IN ('active', 'quarantined')
              AND ttl_seconds > 0
              AND julianday('now') - julianday(created_at_utc) > (ttl_seconds / 86400.0)
        """)
        expired = cursor.rowcount

        kept_cursor = self._sqlite_conn.execute("""
            SELECT COUNT(*) FROM claim
            WHERE status IN ('active', 'quarantined')
        """)
        kept = kept_cursor.fetchone()[0]

        self._safe_commit()
        return {"expired": expired, "kept": kept}

    # =========================================================================
    # WEBHOOK DELIVERY RECORDING
    # =========================================================================

    def record_webhook_delivery(
        self,
        destination_id: str,
        webhook_phase: Literal["pre_message", "post_message", "on_demand"],
        payload_sha256: str,
        status: Literal["pending", "delivered", "failed", "timeout"],
        section_ids: Optional[List[str]] = None,
        injection_id: Optional[str] = None,
        latency_ms: Optional[int] = None,
        response_code: Optional[int] = None,
        error_message: Optional[str] = None
    ) -> str:
        """
        Record a webhook delivery event.

        Args:
            destination_id: Target destination (vs_code_claude_code, synaptic_chat, etc.)
            webhook_phase: pre_message, post_message, or on_demand
            payload_sha256: Hash of the delivered payload
            status: pending, delivered, failed, or timeout
            section_ids: List of section IDs included in payload
            injection_id: Related injection event ID if applicable
            latency_ms: Delivery latency in milliseconds
            response_code: HTTP response code
            error_message: Error message if failed

        Returns:
            Delivery event ID
        """
        delivery_id = self._generate_id()
        section_ids_json = json.dumps(section_ids) if section_ids else None

        self._sqlite_conn.execute("""
            INSERT INTO webhook_delivery_event
            (delivery_id, destination_id, injection_id, timestamp_utc, webhook_phase,
             section_ids, payload_sha256, status, latency_ms, response_code, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            delivery_id, destination_id, injection_id, self._utc_now(), webhook_phase,
            section_ids_json, payload_sha256, status, latency_ms, response_code, error_message
        ))

        # Update destination stats
        if status == "delivered":
            self._sqlite_conn.execute("""
                UPDATE webhook_destination
                SET total_deliveries = total_deliveries + 1,
                    successful_deliveries = successful_deliveries + 1,
                    last_delivery_at = ?,
                    avg_latency_ms = CASE
                        WHEN avg_latency_ms IS NULL THEN ?
                        ELSE (avg_latency_ms * successful_deliveries + ?) / (successful_deliveries + 1)
                    END
                WHERE destination_id = ?
            """, (self._utc_now(), latency_ms, latency_ms, destination_id))
        elif status in ("failed", "timeout"):
            self._sqlite_conn.execute("""
                UPDATE webhook_destination
                SET total_deliveries = total_deliveries + 1,
                    failed_deliveries = failed_deliveries + 1
                WHERE destination_id = ?
            """, (destination_id,))

        self._safe_commit()
        return delivery_id

    def get_webhook_destinations(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """Get registered webhook destinations."""
        if active_only:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM webhook_destination WHERE is_active = 1
                ORDER BY destination_name
            """)
        else:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM webhook_destination ORDER BY destination_name
            """)
        return [dict(row) for row in cursor]

    def get_webhook_delivery_stats(self, hours: int = 24) -> Dict[str, Dict[str, Any]]:
        """Get webhook delivery statistics per destination."""
        cutoff = (datetime.now(timezone.utc) -
                  __import__('datetime').timedelta(hours=hours)).isoformat()

        cursor = self._sqlite_conn.execute("""
            SELECT
                destination_id,
                COUNT(*) as total,
                SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) as delivered,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout,
                AVG(latency_ms) as avg_latency_ms
            FROM webhook_delivery_event
            WHERE timestamp_utc >= ?
            GROUP BY destination_id
        """, (cutoff,))

        stats = {}
        for row in cursor:
            dest_id = row["destination_id"]
            total = row["total"]
            stats[dest_id] = {
                "total": total,
                "delivered": row["delivered"],
                "failed": row["failed"],
                "timeout": row["timeout"],
                "success_rate": (row["delivered"] / total * 100) if total > 0 else 0,
                "avg_latency_ms": row["avg_latency_ms"]
            }
        return stats

    # =========================================================================
    # INJECTION EVENT RECORDING
    # =========================================================================

    def record_injection_event(
        self,
        injection_id: str,
        payload_sha256: str,
        total_latency_ms: int,
        total_tokens: int,
        session_id: Optional[str] = None,
        task_type: str = "context_injection",
        entrypoint: str = "unified_injection",
        user_prompt_sha256: str = "",
        experiment_id: Optional[str] = None,
        variant_id: Optional[str] = None,
    ) -> str:
        """
        Record an injection event to the injection_event table.

        Bridges payload generation (unified_injection.py) to the observability
        tables that all 4 rollup systems depend on (experiment analytics,
        outcome linking, section health, A/B testing).

        Best-effort: call sites wrap in try/except so telemetry never blocks.

        Args:
            injection_id: Unique injection ID (from unified_injection)
            payload_sha256: SHA256 of the generated payload
            total_latency_ms: Time taken to generate payload in ms
            total_tokens: Estimated token count of payload
            session_id: Session identifier for grouping
            task_type: Type of task (context_injection, fallback, safe_mode)
            entrypoint: Entry point (unified_injection, api, cli)
            user_prompt_sha256: SHA256 hash of the user prompt
            experiment_id: A/B experiment ID if applicable
            variant_id: A/B variant ID if applicable

        Returns:
            The injection_id
        """
        import platform

        now = self._utc_now()

        os_name = platform.system()
        sid = session_id or "unknown"

        self._sqlite_conn.execute("""
            INSERT OR IGNORE INTO injection_event
            (injection_id, timestamp_utc, project_id, repo_id, session_id,
             user_id_hash, agent_name, agent_version, ide, os,
             task_type, entrypoint, user_prompt_sha256, selection_sha256,
             orchestrator_version, total_latency_ms, total_tokens,
             payload_sha256, experiment_id, variant_id,
             assignment_deterministic, vault_snapshot_id, signal_batch_id,
             schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            injection_id, now,
            "er-simulator-superrepo", "er-simulator-superrepo", sid,
            "local", "atlas", ORCHESTRATOR_VERSION, "vscode", os_name,
            task_type, entrypoint, user_prompt_sha256, "",
            ORCHESTRATOR_VERSION, total_latency_ms, total_tokens,
            payload_sha256, experiment_id, variant_id,
            1, "none", "none", "1.0",
        ))
        self._safe_commit()

        self._pg_mirror_row("injection_event", {
            "injection_id": injection_id, "timestamp_utc": now,
            "project_id": "er-simulator-superrepo",
            "repo_id": "er-simulator-superrepo", "session_id": sid,
            "user_id_hash": "local", "agent_name": "atlas",
            "agent_version": ORCHESTRATOR_VERSION, "ide": "vscode",
            "os": os_name, "task_type": task_type, "entrypoint": entrypoint,
            "user_prompt_sha256": user_prompt_sha256,
            "selection_sha256": "",
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "total_latency_ms": total_latency_ms,
            "total_tokens": total_tokens,
            "payload_sha256": payload_sha256,
            "experiment_id": experiment_id, "variant_id": variant_id,
            "assignment_deterministic": 1,
            "vault_snapshot_id": "none", "signal_batch_id": "none",
            "schema_version": "1.0",
        })

        return injection_id

    def record_injection_sections(
        self,
        injection_id: str,
        sections_included: list,
        payload: str = "",
        total_latency_ms: int = 0,
        section_timings: dict = None,
    ):
        """
        Record per-section data to injection_section table.

        Bridges the gap where injection_event was recorded but
        injection_section was empty, causing injection_health_monitor
        to report all sections as 'missing' (false negatives).

        Args:
            injection_id: The injection ID from record_injection_event
            sections_included: List of section labels (e.g., ["safety", "foundation", ...])
            payload: Full payload text for computing per-section content hashes
            total_latency_ms: Total latency to distribute across sections (fallback)
            section_timings: Actual per-section latency dict {label: ms} from generators.
                             When provided, uses real timing instead of evenly dividing total.
        """
        import hashlib as _hashlib
        import logging as _logging
        _log = _logging.getLogger('context_dna.observability')

        # Map section labels to section IDs
        LABEL_TO_ID = {
            "safety": "0",
            "foundation": "1",
            "wisdom": "2",
            "awareness": "3",
            "deep_context": "4",
            "protocol": "5",
            "synaptic_to_atlas": "6",
            "acontext_library": "7",
            "synaptic_8th_intelligence": "8",
            "vision_contextual_awareness": "10",
        }

        if not sections_included:
            return

        # Fallback: evenly distribute total latency (used when section_timings unavailable)
        fallback_latency = max(1, total_latency_ms // len(sections_included))
        total_tokens = len(payload) // 4 if payload else 0
        per_section_tokens = max(1, total_tokens // len(sections_included)) if sections_included else 0

        for label in sections_included:
            section_id = LABEL_TO_ID.get(label, label)
            content_sha = _hashlib.sha256(
                f"{injection_id}:{section_id}".encode()
            ).hexdigest()

            # Use actual per-section timing when available, fallback to even distribution
            latency = fallback_latency
            if section_timings and label in section_timings:
                latency = max(1, section_timings[label])

            try:
                self._sqlite_conn.execute("""
                    INSERT OR IGNORE INTO injection_section
                    (injection_id, section_id, enabled, version,
                     content_sha256, latency_ms, tokens, health)
                    VALUES (?, ?, 1, '1.0', ?, ?, ?, 'healthy')
                """, (
                    injection_id, section_id, content_sha,
                    latency, per_section_tokens,
                ))
            except Exception as e:
                _log.debug(f"record_injection_section {section_id}: {e}")

        try:
            self._safe_commit()
        except Exception as e:
            _log.debug(f"commit injection_sections: {e}")

    def record_outcome_event(
        self,
        session_id: str,
        outcome_type: str,
        success: bool,
        reward: float = 0.0,
        injection_id: Optional[str] = None,
        build_pass: Optional[bool] = None,
        test_pass: Optional[bool] = None,
        error_signature_before: Optional[str] = None,
        error_signature_after: Optional[str] = None,
        time_to_next_success_s: Optional[int] = None,
        retry_count: Optional[int] = None,
        undo_count: Optional[int] = None,
        revert_count: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> str:
        """
        Record an outcome event to close the evidence feedback loop.

        Bridges task completion signals to the outcome_event table that
        compute_claim_outcome_rollup depends on for evidence-based promotion.

        Args:
            session_id: Session identifier
            outcome_type: Type (task_success, task_failure, build_result, test_result)
            success: Whether the outcome was successful
            reward: Reward signal [-1, 1]
            injection_id: Link to the injection that preceded this outcome
            build_pass: Whether build passed (if applicable)
            test_pass: Whether tests passed (if applicable)
            error_signature_before: Error hash before the task
            error_signature_after: Error hash after the task
            time_to_next_success_s: Time until next success in seconds
            retry_count: Number of retries needed
            undo_count: Number of undos during task
            revert_count: Number of reverts during task
            notes: Free-form notes

        Returns:
            The outcome_id
        """
        outcome_id = f"OE-{uuid.uuid4().hex[:12].upper()}"
        now = self._utc_now()

        success_int = 1 if success else 0
        reward_clamped = max(-1.0, min(1.0, reward))
        build_int = (1 if build_pass else 0) if build_pass is not None else None
        test_int = (1 if test_pass else 0) if test_pass is not None else None

        self._sqlite_conn.execute("""
            INSERT OR IGNORE INTO outcome_event
            (outcome_id, timestamp_utc, injection_id, session_id, outcome_type,
             success, reward, build_pass, test_pass,
             error_signature_before, error_signature_after,
             time_to_next_success_s, retry_count, undo_count, revert_count, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            outcome_id, now, injection_id, session_id, outcome_type,
            success_int, reward_clamped, build_int, test_int,
            error_signature_before, error_signature_after,
            time_to_next_success_s, retry_count, undo_count, revert_count, notes,
        ))
        self._safe_commit()

        self._pg_mirror_row("outcome_event", {
            "outcome_id": outcome_id, "timestamp_utc": now,
            "injection_id": injection_id, "session_id": session_id,
            "outcome_type": outcome_type, "success": success_int,
            "reward": reward_clamped, "build_pass": build_int,
            "test_pass": test_int,
            "error_signature_before": error_signature_before,
            "error_signature_after": error_signature_after,
            "time_to_next_success_s": time_to_next_success_s,
            "retry_count": retry_count, "undo_count": undo_count,
            "revert_count": revert_count, "notes": notes,
        })

        return outcome_id

    def link_sop_to_outcome(
        self,
        sop_id: str,
        sop_title: str,
        outcome_event_id: str,
        session_id: str = "",
    ) -> bool:
        """
        Link an SOP to an outcome event — bridges the SOP↔Evidence gap.

        This is the missing wire: SOPs were promoted by time/confidence,
        NOT by whether they actually helped when injected. This method
        creates the link that compute_sop_outcome_rollup() aggregates.
        """
        try:
            now = self._utc_now()
            self._sqlite_conn.execute("""
                INSERT OR IGNORE INTO sop_outcome_link
                (sop_id, outcome_event_id, session_id, timestamp_utc)
                VALUES (?, ?, ?, ?)
            """, (sop_id, outcome_event_id, session_id, now))

            # Ensure rollup row exists
            self._sqlite_conn.execute("""
                INSERT OR IGNORE INTO sop_outcome_rollup
                (sop_id, sop_title, updated_at)
                VALUES (?, ?, ?)
            """, (sop_id, sop_title, now))

            self._safe_commit()
            return True
        except Exception as e:
            logger.warning(f"Failed to link SOP to outcome: {e}")
            return False

    def compute_sop_outcome_rollup(self) -> int:
        """
        Compute SOP performance metrics from linked outcome events.

        Returns number of SOPs updated.
        """
        try:
            cursor = self._sqlite_conn.execute("""
                UPDATE sop_outcome_rollup SET
                    n = sub.n,
                    success_count = sub.success_count,
                    failure_count = sub.failure_count,
                    success_rate = sub.success_rate,
                    avg_reward = sub.avg_reward,
                    reliability_score = (sub.success_rate * 0.7) + (MIN(sub.n, 30) / 30.0 * 0.3),
                    last_outcome_ts = sub.last_ts,
                    updated_at = CURRENT_TIMESTAMP
                FROM (
                    SELECT
                        sol.sop_id,
                        COUNT(*) AS n,
                        SUM(CASE WHEN oe.success=1 THEN 1 ELSE 0 END) AS success_count,
                        SUM(CASE WHEN oe.success=0 THEN 1 ELSE 0 END) AS failure_count,
                        AVG(CASE WHEN oe.success=1 THEN 1.0 ELSE 0.0 END) AS success_rate,
                        AVG(oe.reward) AS avg_reward,
                        MAX(oe.timestamp_utc) AS last_ts
                    FROM sop_outcome_link sol
                    JOIN outcome_event oe ON oe.outcome_id = sol.outcome_event_id
                    GROUP BY sol.sop_id
                ) sub
                WHERE sop_outcome_rollup.sop_id = sub.sop_id
            """)
            self._safe_commit()
            return cursor.rowcount
        except Exception as e:
            logger.warning(f"Failed to compute SOP outcome rollup: {e}")
            return 0

    def get_reliable_sops(self, min_reliability: float = 0.7, min_n: int = 2, limit: int = 20) -> list:
        """
        Get SOPs ranked by reliability score (outcome-backed).

        Returns list of dicts with sop_id, sop_title, reliability_score, n, success_rate.
        """
        try:
            cursor = self._sqlite_conn.execute("""
                SELECT sop_id, sop_title, reliability_score, n, success_rate, avg_reward
                FROM sop_outcome_rollup
                WHERE reliability_score >= ? AND n >= ?
                ORDER BY reliability_score DESC
                LIMIT ?
            """, (min_reliability, min_n, limit))
            return [dict(zip(
                ['sop_id', 'sop_title', 'reliability_score', 'n', 'success_rate', 'avg_reward'],
                row
            )) for row in cursor.fetchall()]
        except Exception:
            return []

    def llm_evaluate_sop_effectiveness(self, limit: int = 10) -> List[Dict]:
        """
        Multi-pass LLM evaluation of SOPs — 3 passes for comprehensive quality.

        PASS 1: Causal genuineness (GENUINE/COINCIDENTAL/NOISE)
           → Does outcome data support this SOP's success claim?
        PASS 2: Structural completeness + actionability scoring
           → Bugfix: symptom + root cause + fix + verification?
           → Process: routes + steps + verification + anti-patterns (3+ occurrences)?
           → Can someone follow this SOP without guessing?
        PASS 3: Cross-SOP dedup + priority ranking
           → Merge near-duplicate SOPs (e.g. 5x "Deployment succeeded" → 1)
           → Rank by frequency of success (most-used method = first option)

        Uses extract (128-tok) + classify (64-tok) profiles.
        Budget: 180s (hourly cadence). Returns list of evaluations.
        """
        evaluations = []
        try:
            # Get SOPs with enough data to evaluate
            cursor = self._sqlite_conn.execute("""
                SELECT sop_id, sop_title, n, success_rate, avg_reward, reliability_score
                FROM sop_outcome_rollup
                WHERE n >= 2
                ORDER BY n DESC
                LIMIT ?
            """, (limit,))
            sops = [dict(zip(
                ['sop_id', 'sop_title', 'n', 'success_rate', 'avg_reward', 'reliability_score'],
                row
            )) for row in cursor.fetchall()]

            if not sops:
                return []

            from memory.llm_priority_queue import butler_query

            # ─── PASS 1: Causal genuineness ───
            for sop in sops:
                notes_cursor = self._sqlite_conn.execute("""
                    SELECT oe.notes, oe.success, oe.reward
                    FROM sop_outcome_link sol
                    JOIN outcome_event oe ON oe.outcome_id = sol.outcome_event_id
                    WHERE sol.sop_id = ?
                    ORDER BY oe.timestamp_utc DESC LIMIT 5
                """, (sop['sop_id'],))
                recent_outcomes = [
                    f"{'OK' if r[1] else 'FAIL'} (r={r[2]:.1f}): {(r[0] or '')[:80]}"
                    for r in notes_cursor.fetchall()
                ]

                sop_content = ""
                try:
                    from memory.sqlite_storage import get_sqlite_storage
                    storage = get_sqlite_storage()
                    hits = storage.query(sop['sop_title'][:60], limit=1, _skip_hybrid=True)
                    if hits:
                        sop_content = str(hits[0].get('content', ''))[:400]
                except Exception:
                    pass

                p1_prompt = (
                    f"SOP: {sop['sop_title']}\n"
                    f"Stats: {sop['n']} outcomes, {sop['success_rate']:.0%} success, "
                    f"avg reward={sop['avg_reward']:.2f}\n"
                    f"Outcomes: {'; '.join(recent_outcomes[:3])}\n\n"
                    "Reply ONE word: GENUINE, COINCIDENTAL, or NOISE. Then one sentence."
                )
                p1_result = butler_query(
                    "Is this SOP's success genuine or coincidental? "
                    "NOISE=vague title with no actionable content.",
                    p1_prompt, profile="classify"
                )

                if p1_result:
                    up = p1_result.strip().upper()
                    if up.startswith("GENUINE"):
                        verdict = "GENUINE"
                    elif up.startswith("COINCIDENTAL"):
                        verdict = "COINCIDENTAL"
                    elif up.startswith("NOISE"):
                        verdict = "NOISE"
                    else:
                        verdict = "INSUFFICIENT_DATA"
                else:
                    verdict = "INSUFFICIENT_DATA"

                eval_entry = {
                    "sop_id": sop["sop_id"],
                    "sop_title": sop["sop_title"],
                    "sop_content": sop_content,
                    "stats": sop,
                    "pass1_verdict": verdict,
                    "pass1_reasoning": (p1_result or "LLM unavailable")[:200],
                    "pass2_score": None,
                    "pass2_details": None,
                    "pass3_action": None,
                }
                evaluations.append(eval_entry)

                # Demote on Pass 1
                if verdict in ("COINCIDENTAL", "NOISE"):
                    penalty = 0.3 if verdict == "NOISE" else 0.2
                    try:
                        self._sqlite_conn.execute("""
                            UPDATE sop_outcome_rollup
                            SET reliability_score = MAX(0.05, reliability_score - ?),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE sop_id = ?
                        """, (penalty, sop['sop_id']))
                        self._safe_commit()
                        logger.info(f"Pass1: SOP {sop['sop_id']} demoted ({verdict})")
                    except Exception:
                        pass

            # ─── PASS 2: Structural completeness + actionability ───
            genuine_sops = [e for e in evaluations if e["pass1_verdict"] == "GENUINE"]
            for entry in genuine_sops:
                content = entry.get("sop_content", "")
                if not content or len(content) < 20:
                    entry["pass2_score"] = 0.1
                    entry["pass2_details"] = "No content to evaluate"
                    continue

                p2_prompt = (
                    f"SOP title: {entry['sop_title']}\n"
                    f"Content: {content}\n\n"
                    "Score this SOP 1-10 on THREE criteria:\n"
                    "1. STRUCTURE: Bugfix has symptom+cause+fix? Process has steps+verify?\n"
                    "2. ACTIONABLE: Can someone follow this without guessing? Has commands?\n"
                    "3. COMPLETE: Has verification step? Anti-patterns only if 3+ occurrences?\n\n"
                    "Reply format: SCORE/10 then one sentence summary.\n"
                    "Example: 7/10 Has symptom and fix but missing verification step."
                )
                p2_result = butler_query(
                    "Score SOP structural quality. 10=complete actionable SOP, "
                    "1=vague title only.",
                    p2_prompt, profile="extract"
                )

                if p2_result:
                    # Parse score from "7/10 ..." format
                    import re
                    score_match = re.search(r'(\d+)\s*/\s*10', p2_result)
                    if score_match:
                        entry["pass2_score"] = int(score_match.group(1)) / 10.0
                    else:
                        entry["pass2_score"] = 0.5  # unknown
                    entry["pass2_details"] = p2_result.strip()[:200]

                    # Boost or penalize reliability based on structural quality
                    score = entry["pass2_score"]
                    if score >= 0.7:
                        # Good structure — small reliability boost
                        try:
                            self._sqlite_conn.execute("""
                                UPDATE sop_outcome_rollup
                                SET reliability_score = MIN(1.0, reliability_score + 0.05),
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE sop_id = ?
                            """, (entry['sop_id'],))
                            self._safe_commit()
                        except Exception:
                            pass
                    elif score <= 0.3:
                        # Poor structure despite genuine outcomes — flag for improvement
                        entry["pass3_action"] = "NEEDS_IMPROVEMENT"

            # ─── PASS 3: Cross-SOP dedup + priority ranking ───
            # Group SOPs with similar titles for merge candidates
            if len(evaluations) >= 2:
                from memory.dedup_detector import DuplicateDetector
                detector = DuplicateDetector()
                merge_groups: Dict[str, List[Dict]] = {}
                processed_ids = set()

                for i, e1 in enumerate(evaluations):
                    if e1["sop_id"] in processed_ids:
                        continue
                    group = [e1]
                    for j, e2 in enumerate(evaluations):
                        if i == j or e2["sop_id"] in processed_ids:
                            continue
                        sim = detector.semantic_similarity(
                            e1["sop_title"], e2["sop_title"]
                        )
                        if sim >= 0.6:
                            group.append(e2)
                            processed_ids.add(e2["sop_id"])
                    if len(group) > 1:
                        # LLM confirms merge
                        titles = "\n".join(
                            f"- [{g['stats']['n']} outcomes, "
                            f"{g['stats']['success_rate']:.0%}] {g['sop_title']}"
                            for g in group
                        )
                        p3_prompt = (
                            f"These {len(group)} SOPs look similar:\n{titles}\n\n"
                            "Should they merge into ONE SOP? Reply:\n"
                            "MERGE: <best title to keep> or NO_MERGE: <why>"
                        )
                        p3_result = butler_query(
                            "Decide if similar SOPs should merge. "
                            "Keep the one with most outcomes.",
                            p3_prompt, profile="classify"
                        )

                        if p3_result and p3_result.strip().upper().startswith("MERGE"):
                            # Mark best (highest n) as keeper, others as merge targets
                            group.sort(key=lambda g: g["stats"]["n"], reverse=True)
                            keeper = group[0]
                            keeper["pass3_action"] = "KEEP_AS_PRIMARY"
                            for secondary in group[1:]:
                                secondary["pass3_action"] = f"MERGE_INTO:{keeper['sop_id']}"
                                # Demote secondaries
                                try:
                                    self._sqlite_conn.execute("""
                                        UPDATE sop_outcome_rollup
                                        SET reliability_score = MAX(0.05, reliability_score - 0.15),
                                            updated_at = CURRENT_TIMESTAMP
                                        WHERE sop_id = ?
                                    """, (secondary['sop_id'],))
                                    self._safe_commit()
                                    logger.info(
                                        f"Pass3: SOP {secondary['sop_id']} merged into "
                                        f"{keeper['sop_id']}"
                                    )
                                except Exception:
                                    pass

            # ─── PASS 4: Cross-method comparison ranking ───
            # Group SOPs addressing the same task/domain via LLM classification.
            # Rank each group by success rate so the most effective method surfaces first.
            genuine_with_stats = [e for e in evaluations
                                 if e["pass1_verdict"] == "GENUINE"
                                 and e.get("pass3_action") != "NEEDS_IMPROVEMENT"
                                 and not str(e.get("pass3_action", "")).startswith("MERGE_INTO")]
            if len(genuine_with_stats) >= 3:
                # Ask LLM to cluster SOPs by task/domain
                titles_for_cluster = "\n".join(
                    f"{i+1}. [{e['stats']['success_rate']:.0%} win, n={e['stats']['n']}] {e['sop_title']}"
                    for i, e in enumerate(genuine_with_stats)
                )
                p4_prompt = (
                    f"These {len(genuine_with_stats)} SOPs address different tasks.\n"
                    f"{titles_for_cluster}\n\n"
                    "Group SOPs that address the SAME task using different methods.\n"
                    "Reply format: GROUP <numbers>: <task name>\n"
                    "Only group SOPs that truly overlap. Omit unique SOPs.\n"
                    "Example: GROUP 1,3: Docker deployment"
                )
                p4_result = butler_query(
                    "Cluster SOPs by task. Only group those addressing the same problem "
                    "with different methods.",
                    p4_prompt, profile="extract"
                )

                if p4_result:
                    import re as _re
                    for match in _re.finditer(r'GROUP\s+([\d,\s]+):', p4_result):
                        indices = [int(x.strip()) - 1 for x in match.group(1).split(',')
                                   if x.strip().isdigit()]
                        group = [genuine_with_stats[i] for i in indices
                                 if 0 <= i < len(genuine_with_stats)]
                        if len(group) >= 2:
                            # Rank by success_rate descending
                            group.sort(key=lambda g: (
                                g["stats"]["success_rate"],
                                g["stats"]["n"]
                            ), reverse=True)
                            for rank, entry in enumerate(group, 1):
                                entry["pass4_rank"] = rank
                                entry["pass4_group_size"] = len(group)
                                if rank == 1:
                                    # Best method — boost reliability
                                    try:
                                        self._sqlite_conn.execute("""
                                            UPDATE sop_outcome_rollup
                                            SET reliability_score = MIN(1.0, reliability_score + 0.08),
                                                updated_at = CURRENT_TIMESTAMP
                                            WHERE sop_id = ?
                                        """, (entry['sop_id'],))
                                        self._safe_commit()
                                    except Exception:
                                        pass
                                elif rank == len(group):
                                    # Worst method — slight demotion
                                    try:
                                        self._sqlite_conn.execute("""
                                            UPDATE sop_outcome_rollup
                                            SET reliability_score = MAX(0.05, reliability_score - 0.05),
                                                updated_at = CURRENT_TIMESTAMP
                                            WHERE sop_id = ?
                                        """, (entry['sop_id'],))
                                        self._safe_commit()
                                    except Exception:
                                        pass

            # ─── PASS 5: Auto-enrichment of low-quality genuine SOPs ───
            # For SOPs that are GENUINE but scored <= 3/10, generate the
            # missing sections (symptom/root_cause/fix/verification).
            needs_enrichment = [e for e in evaluations
                                if e["pass1_verdict"] == "GENUINE"
                                and e.get("pass2_score") is not None
                                and e["pass2_score"] <= 0.3
                                and e.get("pass3_action") == "NEEDS_IMPROVEMENT"]
            for entry in needs_enrichment[:3]:  # Cap at 3 per run (budget)
                # Gather outcome context for enrichment
                outcome_notes = []
                try:
                    oc = self._sqlite_conn.execute("""
                        SELECT oe.notes FROM sop_outcome_link sol
                        JOIN outcome_event oe ON oe.outcome_id = sol.outcome_event_id
                        WHERE sol.sop_id = ? AND oe.notes IS NOT NULL
                        ORDER BY oe.timestamp_utc DESC LIMIT 5
                    """, (entry['sop_id'],))
                    outcome_notes = [r[0][:150] for r in oc.fetchall() if r[0]]
                except Exception:
                    pass

                p5_prompt = (
                    f"SOP title: {entry['sop_title']}\n"
                    f"Current quality: {entry['pass2_score']:.0%} (low)\n"
                    f"Outcome context: {'; '.join(outcome_notes[:3]) if outcome_notes else 'None'}\n\n"
                    "Generate the missing SOP sections. Reply in this format:\n"
                    "SYMPTOM: <what was observed>\n"
                    "ROOT_CAUSE: <why it happened>\n"
                    "FIX: <actual commands or code>\n"
                    "VERIFY: <how to confirm the fix>\n"
                    "Only include sections you can reasonably infer."
                )
                p5_result = butler_query(
                    "Enrich this low-quality SOP with missing sections. "
                    "Be specific and actionable.",
                    p5_prompt, profile="extract"
                )

                if p5_result and len(p5_result.strip()) > 30:
                    entry["pass5_enrichment"] = p5_result.strip()[:500]
                    # Store enrichment in learnings.db alongside original
                    try:
                        from memory.sqlite_storage import get_sqlite_storage
                        storage = get_sqlite_storage()
                        enriched_content = (
                            f"{entry['sop_title']}\n\n"
                            f"[Auto-enriched by butler — {datetime.now().strftime('%Y-%m-%d')}]\n"
                            f"{p5_result.strip()[:400]}"
                        )
                        storage.add({
                            'type': 'sop_enriched',
                            'content': enriched_content,
                            'source': f"pass5_enrichment:{entry['sop_id']}",
                            'confidence': 0.5,  # Lower than manual SOPs
                        })
                        entry["pass5_stored"] = True
                        logger.info(f"Pass5: Enriched SOP {entry['sop_id']}")
                    except Exception as e:
                        logger.debug(f"Pass5 storage failed: {e}")

            # Final summary
            for entry in evaluations:
                entry["causal_verdict"] = entry.pop("pass1_verdict")
                entry["reasoning"] = entry.pop("pass1_reasoning")
                # Remove internal fields
                entry.pop("sop_content", None)

        except ImportError:
            logger.debug("LLM priority queue not available for SOP evaluation")
        except Exception as e:
            logger.warning(f"SOP LLM evaluation failed: {e}")

        return evaluations

    def link_claim_to_injection(
        self,
        injection_id: str,
        claim_id: str,
        section_id: str = "unknown",
    ) -> bool:
        """
        Link a claim to the injection that was active when it was created.

        Populates the injection_claim table which is the critical JOIN key
        that compute_claim_outcome_rollup uses to connect claims to outcomes.

        The chain: injection_claim + outcome_event -> claim_outcome_rollup
                   -> evaluate_quarantine -> promotion/rejection

        Without injection_claim rows, rollups are always empty and claims
        can never be promoted from quarantine.

        Args:
            injection_id: The injection event ID
            claim_id: The claim being linked
            section_id: Which section generated this claim

        Returns:
            True if link was created, False if already exists or injection missing
        """
        try:
            cursor = self._sqlite_conn.execute("""
                INSERT OR IGNORE INTO injection_claim
                (injection_id, section_id, claim_id)
                VALUES (?, ?, ?)
            """, (injection_id, section_id, claim_id))
            self._safe_commit()
            return cursor.rowcount > 0
        except Exception:
            return False

    # =========================================================================
    # TECH STACK VERSION METHODS
    # =========================================================================

    def upsert_tech_version(
        self,
        component_name: str,
        component_type: str,
        current_version: str,
        auto_detected: bool = True,
        detection_method: Optional[str] = None,
        health_status: str = "healthy",
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Insert or update a tech stack component version.

        Args:
            component_name: Unique component identifier
            component_type: database, cache, queue, container, llm, api, service, extension, library, runtime
            current_version: Version string
            auto_detected: Whether this was auto-detected
            detection_method: How version was detected
            health_status: healthy, degraded, down, unknown
            metadata: Additional metadata as dict
        """
        metadata_json = json.dumps(metadata) if metadata else None

        self._sqlite_conn.execute("""
            INSERT INTO tech_stack_version_registry
            (component_name, component_type, current_version, auto_detected,
             detection_method, health_status, metadata_json, last_verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(component_name) DO UPDATE SET
                current_version = excluded.current_version,
                last_updated_at = CURRENT_TIMESTAMP,
                last_verified_at = CURRENT_TIMESTAMP,
                auto_detected = excluded.auto_detected,
                detection_method = COALESCE(excluded.detection_method, tech_stack_version_registry.detection_method),
                health_status = excluded.health_status,
                metadata_json = COALESCE(excluded.metadata_json, tech_stack_version_registry.metadata_json)
        """, (
            component_name, component_type, current_version, 1 if auto_detected else 0,
            detection_method, health_status, metadata_json, self._utc_now()
        ))
        self._safe_commit()

    def get_tech_stack_versions(self) -> List[Dict[str, Any]]:
        """Get all registered tech stack versions."""
        cursor = self._sqlite_conn.execute("""
            SELECT * FROM tech_stack_version_registry
            ORDER BY component_type, component_name
        """)
        return [dict(row) for row in cursor]

    # =========================================================================
    # KNOWLEDGE QUARANTINE METHODS
    # =========================================================================

    def quarantine_item(
        self,
        item_id: str,
        item_type: Literal["claim", "learning", "sop"],
        promotion_rules: Dict[str, Any],
        notes: Optional[str] = None
    ) -> str:
        """
        Insert an item into the knowledge quarantine with status='quarantined'.

        This is a PRE-CANDIDATE validation step. Items enter quarantine before
        being trusted by the system. The lite_scheduler's _run_evaluate_quarantine
        handles promotion/rejection based on outcome rollups.

        Args:
            item_id: Unique identifier for the item (claim_id, learning_id, etc.)
            item_type: Type of knowledge item ('claim', 'learning', or 'sop')
            promotion_rules: Dict of rules for promotion, e.g.:
                {"min_confirmations": 2, "min_hours": 24, "auto_promote_on": "repeated_success"}
            notes: Optional notes about why this was quarantined

        Returns:
            The item_id that was quarantined
        """
        now = self._utc_now()

        rules_json = json.dumps(promotion_rules)

        self._sqlite_conn.execute("""
            INSERT OR IGNORE INTO knowledge_quarantine
            (item_id, item_type, status, created_at_utc, updated_at_utc,
             promotion_rules_json, notes)
            VALUES (?, ?, 'quarantined', ?, ?, ?, ?)
        """, (
            item_id, item_type, now, now, rules_json, notes
        ))
        self._safe_commit()

        self._pg_mirror_row("knowledge_quarantine", {
            "item_id": item_id, "item_type": item_type,
            "status": "quarantined", "created_at_utc": now,
            "updated_at_utc": now, "promotion_rules_json": rules_json,
            "notes": notes,
        })

        return item_id

    def get_quarantined_items(
        self,
        item_type: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Retrieve items pending validation from the knowledge quarantine.

        Args:
            item_type: Filter by type ('claim', 'learning', 'sop') or None for all
            limit: Maximum number of items to return

        Returns:
            List of quarantined item dicts with parsed JSON fields
        """
        if item_type:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM knowledge_quarantine
                WHERE item_type = ? AND status IN ('quarantined', 'validating')
                ORDER BY created_at_utc DESC
                LIMIT ?
            """, (item_type, limit))
        else:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM knowledge_quarantine
                WHERE status IN ('quarantined', 'validating')
                ORDER BY created_at_utc DESC
                LIMIT ?
            """, (limit,))

        results = []
        for row in cursor:
            item = dict(row)
            # Parse JSON fields for convenience
            if item.get("promotion_rules_json"):
                try:
                    item["promotion_rules"] = json.loads(item["promotion_rules_json"])
                except (json.JSONDecodeError, TypeError):
                    item["promotion_rules"] = {}
            if item.get("validation_stats_json"):
                try:
                    item["validation_stats"] = json.loads(item["validation_stats_json"])
                except (json.JSONDecodeError, TypeError):
                    item["validation_stats"] = {}
            results.append(item)

        return results

    def update_quarantine_status(
        self,
        item_id: str,
        new_status: Literal["quarantined", "validating", "trusted", "rejected"],
        validation_stats: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Transition the status of a quarantined item.

        Args:
            item_id: The item to update
            new_status: New status ('quarantined', 'validating', 'trusted', 'rejected')
            validation_stats: Optional dict of validation statistics to record

        Returns:
            True if the item was found and updated, False otherwise
        """
        now = self._utc_now()
        validation_json = json.dumps(validation_stats) if validation_stats else None

        cursor = self._sqlite_conn.execute("""
            UPDATE knowledge_quarantine
            SET status = ?,
                updated_at_utc = ?,
                validation_stats_json = COALESCE(?, validation_stats_json)
            WHERE item_id = ?
        """, (new_status, now, validation_json, item_id))
        self._safe_commit()

        return cursor.rowcount > 0

    def record_direct_claim_outcome(
        self,
        claim_id: str,
        success: bool,
        reward: float = 0.0,
        source: str = "auto",
        notes: Optional[str] = None,
    ) -> int:
        """
        Record a direct outcome for a specific claim.

        Bypasses the injection_claim -> injection_event -> outcome_event chain
        which requires webhook injection to populate. This allows claims from
        capture_success/capture_failure to accumulate outcomes directly.

        The lite_scheduler's _run_evaluate_quarantine uses these direct outcomes
        to compute promotion eligibility (n>=3 bootstrap, n>=10 mature).

        Args:
            claim_id: The claim this outcome is for
            success: Whether the outcome was successful
            reward: Reward signal [-1, 1]
            source: Where this outcome came from
            notes: Optional notes

        Returns:
            Row ID of the inserted record
        """
        now = self._utc_now()
        success_int = 1 if success else 0
        reward_clamped = max(-1.0, min(1.0, reward))

        self._sqlite_conn.execute("""
            INSERT INTO direct_claim_outcome
            (claim_id, timestamp_utc, success, reward, source, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (claim_id, now, success_int, reward_clamped, source, notes))
        self._safe_commit()
        row_id = self._sqlite_conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        self._pg_mirror_row("direct_claim_outcome", {
            "id": row_id, "claim_id": claim_id, "timestamp_utc": now,
            "success": success_int, "reward": reward_clamped,
            "source": source, "notes": notes,
        })

        return row_id

    def get_direct_claim_rollup(
        self,
        claim_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Compute rollup stats for a claim from direct_claim_outcome table.

        Returns n, success_rate, avg_reward for the claim, or None if no outcomes.
        """
        cursor = self._sqlite_conn.execute("""
            SELECT
                COUNT(*) AS n,
                AVG(CASE WHEN success = 1 THEN 1.0 ELSE 0.0 END) AS success_rate,
                AVG(reward) AS avg_reward
            FROM direct_claim_outcome
            WHERE claim_id = ?
        """, (claim_id,))
        row = cursor.fetchone()
        if row and row["n"] > 0:
            return {
                "n": row["n"],
                "success_rate": row["success_rate"],
                "avg_reward": row["avg_reward"],
            }
        return None

    # =========================================================================
    # GAP 2: PER-LEARNING OUTCOME ATTRIBUTION
    # =========================================================================

    def record_learning_outcome(
        self,
        injection_id: str,
        learning_id: str,
        section_id: str,
        outcome_success: bool,
        outcome_reward: float = 0.0,
    ) -> int:
        """Record which learning was present in an injection and its outcome."""
        self._sqlite_conn.execute("""
            INSERT INTO injection_learning_outcome
            (injection_id, learning_id, section_id, outcome_success, outcome_reward)
            VALUES (?, ?, ?, ?, ?)
        """, (injection_id, learning_id, section_id,
              1 if outcome_success else 0, outcome_reward))
        self._safe_commit()
        return self._sqlite_conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_learning_effectiveness(self, learning_id: str) -> Optional[Dict[str, Any]]:
        """Get outcome stats for a specific learning."""
        row = self._sqlite_conn.execute("""
            SELECT COUNT(*) AS n,
                   AVG(CASE WHEN outcome_success=1 THEN 1.0 ELSE 0.0 END) AS success_rate,
                   AVG(outcome_reward) AS avg_reward
            FROM injection_learning_outcome WHERE learning_id = ?
        """, (learning_id,)).fetchone()
        if row and row["n"] > 0:
            return {"n": row["n"], "success_rate": row["success_rate"],
                    "avg_reward": row["avg_reward"]}
        return None

    # =========================================================================
    # GAP 3: PER-SOP OUTCOME TRACKING
    # =========================================================================

    def record_sop_outcome(
        self,
        sop_id: str,
        injection_id: Optional[str],
        outcome_success: bool,
        outcome_reward: float = 0.0,
        context_hash: Optional[str] = None,
    ) -> int:
        """Record an outcome for an SOP used in injection."""
        self._sqlite_conn.execute("""
            INSERT INTO sop_outcome_link
            (sop_id, injection_id, outcome_success, outcome_reward, context_hash)
            VALUES (?, ?, ?, ?, ?)
        """, (sop_id, injection_id, 1 if outcome_success else 0,
              outcome_reward, context_hash))
        self._safe_commit()
        return self._sqlite_conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_sop_effectiveness_rollup(self, sop_id: str) -> Dict[str, Any]:
        """Recompute effectiveness rollup for an SOP from its outcomes."""
        row = self._sqlite_conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN outcome_success=1 THEN 1 ELSE 0 END) AS successes,
                   SUM(CASE WHEN outcome_success=0 THEN 1 ELSE 0 END) AS failures,
                   AVG(outcome_reward) AS avg_reward,
                   MAX(timestamp_utc) AS last_used
            FROM sop_outcome_link WHERE sop_id = ?
        """, (sop_id,)).fetchone()
        if not row or row["total"] == 0:
            return {"sop_id": sop_id, "total_uses": 0}
        eff = row["successes"] / row["total"] if row["total"] > 0 else 0.5
        self._sqlite_conn.execute("""
            INSERT INTO sop_effectiveness_rollup
            (sop_id, total_uses, success_count, failure_count, avg_reward, effectiveness, last_used_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sop_id) DO UPDATE SET
                total_uses=excluded.total_uses, success_count=excluded.success_count,
                failure_count=excluded.failure_count, avg_reward=excluded.avg_reward,
                effectiveness=excluded.effectiveness, last_used_utc=excluded.last_used_utc,
                updated_at_utc=excluded.updated_at_utc
        """, (sop_id, row["total"], row["successes"], row["failures"],
              row["avg_reward"], eff, row["last_used"], self._utc_now()))
        self._safe_commit()
        return {"sop_id": sop_id, "total_uses": row["total"],
                "effectiveness": eff, "avg_reward": row["avg_reward"]}

    # =========================================================================
    # GAP 5: EVIDENCE GRADE AUTO-UPGRADING
    # =========================================================================

    # Grade thresholds: anecdote(1-4) → case_series(5-9) → cohort(10-19) → validated(20+)
    _GRADE_THRESHOLDS = [
        (20, 0.8, "validated"),      # 20+ outcomes, 80%+ success
        (10, 0.7, "cohort"),         # 10+ outcomes, 70%+ success
        (5, 0.6, "case_series"),     # 5+ outcomes, 60%+ success
        (3, 0.5, "correlation"),     # 3+ outcomes, 50%+ success
        (1, 0.0, "anecdote"),        # any outcomes
    ]

    # Grade ordering for UP-only enforcement (higher index = stronger evidence)
    _GRADE_RANK = {
        "opinion": 0, "anecdote": 1, "correlation": 2,
        "case_series": 3, "cohort": 4, "validated": 5,
        "meta_analysis": 6,
    }

    def auto_upgrade_evidence_grade(self, claim_id: str) -> Optional[Dict[str, Any]]:
        """
        Auto-UPGRADE evidence grade based on accumulated outcomes.
        NEVER downgrades — the evidence ladder only goes UP.
        Returns grade transition info if changed, None otherwise.
        """
        # Get current grade
        claim_row = self._sqlite_conn.execute(
            "SELECT evidence_grade FROM claim WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if not claim_row:
            return None
        old_grade = claim_row["evidence_grade"] or "anecdote"

        # Get outcome stats
        rollup = self.get_direct_claim_rollup(claim_id)
        if not rollup:
            return None

        n = rollup["n"]
        sr = rollup["success_rate"]

        # Determine earned grade from outcomes
        earned_grade = "anecdote"
        for threshold_n, threshold_sr, grade in self._GRADE_THRESHOLDS:
            if n >= threshold_n and sr >= threshold_sr:
                earned_grade = grade
                break

        # NEVER downgrade — only upgrade
        old_rank = self._GRADE_RANK.get(old_grade, 1)
        new_rank = self._GRADE_RANK.get(earned_grade, 1)
        if new_rank <= old_rank:
            return None  # Earned grade is same or lower — preserve current

        # Update claim (upgrade only)
        self._sqlite_conn.execute(
            "UPDATE claim SET evidence_grade = ? WHERE claim_id = ?",
            (earned_grade, claim_id)
        )
        # Record transition
        self._sqlite_conn.execute("""
            INSERT INTO evidence_grade_history
            (claim_id, old_grade, new_grade, reason, outcome_count, success_rate)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (claim_id, old_grade, earned_grade,
              f"Auto-upgrade: n={n}, sr={sr:.2f}", n, sr))
        self._safe_commit()
        return {"claim_id": claim_id, "old_grade": old_grade,
                "new_grade": earned_grade, "n": n, "success_rate": sr}

    # =========================================================================
    # GAP 6: A/B VARIANT ↔ OUTCOME LINKING
    # =========================================================================

    def record_variant_outcome(
        self,
        injection_id: str,
        experiment_id: str,
        variant_id: str,
        outcome_success: bool,
        outcome_reward: float = 0.0,
    ):
        """
        Link A/B variant to outcome. Uses existing injection_event + outcome_event
        tables but ensures experiment_id/variant_id propagate to outcome.
        """
        # Update injection_event with experiment info if not set
        self._sqlite_conn.execute("""
            UPDATE injection_event
            SET experiment_id = ?, variant_id = ?
            WHERE injection_id = ? AND (experiment_id IS NULL OR experiment_id = '')
        """, (experiment_id, variant_id, injection_id))
        # Record outcome linked to variant
        self._sqlite_conn.execute("""
            INSERT INTO outcome_event
            (session_id, outcome_type, success, reward, injection_id, notes)
            VALUES (?, 'ab_variant_outcome', ?, ?, ?, ?)
        """, (f"ab_{experiment_id}", 1 if outcome_success else 0,
              outcome_reward, injection_id,
              f"variant={variant_id}, exp={experiment_id}"))
        self._safe_commit()

    # =========================================================================
    # GAP 7: CROSS-SESSION VERIFICATION
    # =========================================================================

    def record_cross_session_verification(
        self,
        original_win_id: str,
        original_session: Optional[str],
        verification_session: str,
        verdict: str,
        evidence: Optional[str] = None,
        confidence: float = 0.5,
    ) -> int:
        """Record cross-session verification result for a prior win."""
        self._sqlite_conn.execute("""
            INSERT INTO cross_session_verification
            (original_win_id, original_session, verification_session,
             verdict, evidence, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (original_win_id, original_session, verification_session,
              verdict, evidence, confidence))
        self._safe_commit()
        return self._sqlite_conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_cross_session_stats(self, win_id: str) -> Optional[Dict[str, Any]]:
        """Get cross-session verification stats for a win."""
        row = self._sqlite_conn.execute("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN verdict='CONFIRMED' THEN 1 ELSE 0 END) AS confirmed,
                   SUM(CASE WHEN verdict='REGRESSED' THEN 1 ELSE 0 END) AS regressed,
                   AVG(confidence) AS avg_confidence
            FROM cross_session_verification WHERE original_win_id = ?
        """, (win_id,)).fetchone()
        if row and row["n"] > 0:
            return {"n": row["n"], "confirmed": row["confirmed"],
                    "regressed": row["regressed"],
                    "avg_confidence": row["avg_confidence"]}
        return None

    # =========================================================================
    # VAULT SNAPSHOT MANIFEST
    # =========================================================================

    def create_vault_snapshot(
        self,
        manifest: Dict[str, Any],
        mode: Literal["lite", "heavy"] = "lite",
        storage_engine: Literal["sqlite", "postgres"] = "sqlite",
        project_id: str = "er-simulator-superrepo",
        repo_id: str = "er-simulator-superrepo",
        git_head_sha: Optional[str] = None,
        dirty: bool = False,
        notes: Optional[str] = None
    ) -> str:
        """
        Create a vault snapshot manifest for knowledge versioning.

        Captures point-in-time state of the knowledge vault (claims, learnings,
        SOPs) for auditability and rollback capability.

        Args:
            manifest: Dict describing vault contents (table counts, checksums)
            mode: Operating mode ('light' for SQLite, 'heavy' for Postgres)
            storage_engine: Backend storage ('sqlite' or 'postgres')
            project_id: Project identifier
            repo_id: Repository identifier
            git_head_sha: Current git HEAD SHA if available
            dirty: Whether working directory has uncommitted changes
            notes: Optional notes about the snapshot

        Returns:
            The vault_snapshot_id
        """
        import hashlib
        snapshot_id = self._generate_id()
        manifest_json = json.dumps(manifest, sort_keys=True)
        manifest_sha256 = hashlib.sha256(manifest_json.encode()).hexdigest()

        self._sqlite_conn.execute("""
            INSERT INTO vault_snapshot_manifest
            (vault_snapshot_id, created_at_utc, schema_version, manifest_sha256,
             manifest_json, mode, storage_engine, project_id, repo_id,
             git_head_sha, dirty, notes)
            VALUES (?, ?, '1.0', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot_id, self._utc_now(), manifest_sha256,
            manifest_json, mode, storage_engine, project_id, repo_id,
            git_head_sha, 1 if dirty else 0, notes
        ))
        self._safe_commit()

        return snapshot_id

    def get_vault_snapshots(
        self,
        limit: int = 20,
        repo_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Retrieve recent vault snapshots for audit trail.

        Args:
            limit: Max snapshots to return
            repo_id: Filter by repository

        Returns:
            List of snapshot dicts with parsed manifest
        """
        if repo_id:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM vault_snapshot_manifest
                WHERE repo_id = ?
                ORDER BY created_at_utc DESC
                LIMIT ?
            """, (repo_id, limit))
        else:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM vault_snapshot_manifest
                ORDER BY created_at_utc DESC
                LIMIT ?
            """, (limit,))

        results = []
        for row in cursor:
            item = dict(row)
            if item.get("manifest_json"):
                try:
                    item["manifest"] = json.loads(item["manifest_json"])
                except (json.JSONDecodeError, TypeError):
                    item["manifest"] = {}
            results.append(item)

        return results

    # =========================================================================
    # QUERY METHODS
    # =========================================================================

    def get_recent_dependency_events(
        self,
        dependency_name: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get recent dependency status events."""
        if dependency_name:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM dependency_status_event
                WHERE dependency_name = ?
                ORDER BY timestamp_utc DESC
                LIMIT ?
            """, (dependency_name, limit))
        else:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM dependency_status_event
                ORDER BY timestamp_utc DESC
                LIMIT ?
            """, (limit,))

        return [dict(row) for row in cursor]

    def get_recent_task_runs(
        self,
        task_name: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get recent task run events."""
        if task_name:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM task_run_event
                WHERE task_name = ?
                ORDER BY timestamp_utc DESC
                LIMIT ?
            """, (task_name, limit))
        else:
            cursor = self._sqlite_conn.execute("""
                SELECT * FROM task_run_event
                ORDER BY timestamp_utc DESC
                LIMIT ?
            """, (limit,))

        return [dict(row) for row in cursor]

    def get_dependency_summary(self, hours: int = 24) -> Dict[str, Dict[str, Any]]:
        """
        Get summary of dependency health over time period.

        Returns:
            Dict mapping dependency_name to {healthy_pct, total_checks, last_status}
        """
        cutoff = (datetime.now(timezone.utc) -
                  __import__('datetime').timedelta(hours=hours)).isoformat()

        cursor = self._sqlite_conn.execute("""
            SELECT
                dependency_name,
                COUNT(*) as total_checks,
                SUM(CASE WHEN status = 'healthy' THEN 1 ELSE 0 END) as healthy_count,
                MAX(timestamp_utc) as last_check
            FROM dependency_status_event
            WHERE timestamp_utc >= ?
            GROUP BY dependency_name
        """, (cutoff,))

        summary = {}
        for row in cursor:
            name = row["dependency_name"]
            total = row["total_checks"]
            healthy = row["healthy_count"]

            # Get last status
            last_cursor = self._sqlite_conn.execute("""
                SELECT status FROM dependency_status_event
                WHERE dependency_name = ?
                ORDER BY timestamp_utc DESC
                LIMIT 1
            """, (name,))
            last_row = last_cursor.fetchone()

            summary[name] = {
                "healthy_pct": (healthy / total * 100) if total > 0 else 0,
                "total_checks": total,
                "healthy_count": healthy,
                "last_status": last_row["status"] if last_row else "unknown",
                "last_check": row["last_check"]
            }

        return summary

    # =========================================================================
    # PG WRITE-THROUGH (delegates to unified sync engine)
    # =========================================================================

    def _pg_mirror_row(self, table_name: str, data: dict):
        """
        Best-effort write-through of a single row to PG.
        Delegates to unified_sync.py engine.
        """
        try:
            from memory.unified_sync import get_sync_engine
            engine = get_sync_engine()
            engine.mirror_row(table_name, data)
        except Exception:
            pass  # Best-effort — failures are silent

    def get_most_recent_injection(
        self,
        session_id: Optional[str] = None,
        max_age_seconds: int = 600,
    ) -> Optional[Dict[str, Any]]:
        """
        Get the most recent injection_event, with session and recency filters.

        Resolution order:
        1. If session_id provided, find injection matching that session (any age).
        2. If no session match, fall back to most recent injection within max_age_seconds.
        3. If nothing within window, return None (stale injection = no attribution).

        This closes the injection->outcome attribution gap: capture_success/failure
        can reliably find which injection preceded the current task.

        Args:
            session_id: Session to match (preferred). Can be empty string.
            max_age_seconds: Max age for timestamp-proximity fallback (default 10min).

        Returns:
            Dict with injection_id, session_id, variant_id, timestamp_utc or None.
        """
        # Strategy 1: Session-based match (strongest signal)
        if session_id:
            row = self._sqlite_conn.execute(
                "SELECT injection_id, session_id, variant_id, timestamp_utc "
                "FROM injection_event WHERE session_id = ? "
                "ORDER BY timestamp_utc DESC LIMIT 1",
                (session_id,)
            ).fetchone()
            if row:
                return {
                    "injection_id": row["injection_id"],
                    "session_id": row["session_id"],
                    "variant_id": row["variant_id"],
                    "timestamp_utc": row["timestamp_utc"],
                }

        # Strategy 2: Timestamp-proximity fallback (within max_age_seconds)
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
        row = self._sqlite_conn.execute(
            "SELECT injection_id, session_id, variant_id, timestamp_utc "
            "FROM injection_event WHERE timestamp_utc >= ? "
            "ORDER BY timestamp_utc DESC LIMIT 1",
            (cutoff,)
        ).fetchone()
        if row:
            return {
                "injection_id": row["injection_id"],
                "session_id": row["session_id"],
                "variant_id": row["variant_id"],
                "timestamp_utc": row["timestamp_utc"],
            }

        return None

    def get_variant_outcome_attribution(self) -> dict:
        """
        Get outcome attribution per A/B test variant from the hook_evolution engine.

        Bridges the observability store with the hook_evolution A/B testing system.
        Returns variant performance stats for the running test, if any.

        Returns:
            Dict with test info, variant stats, and attribution summary.
        """
        try:
            from memory.hook_evolution import get_hook_evolution_engine
            engine = get_hook_evolution_engine()

            # Get running test
            running = engine.get_running_test("UserPromptSubmit")
            if not running:
                return {"status": "no_running_test", "variants": {}}

            result = {
                "status": "running",
                "test_id": running.test_id,
                "test_name": running.test_name,
                "started_at": running.started_at,
                "min_samples": running.min_samples_per_variant,
                "variants": {},
            }

            # Get stats for each variant
            for label, vid in [
                ("control", running.control_variant_id),
                ("variant_a", running.variant_a_id),
                ("variant_b", running.variant_b_id),
            ]:
                if not vid:
                    continue
                stats = engine.get_variant_stats(vid)
                firings = engine.db.execute(
                    "SELECT COUNT(*) FROM hook_firings WHERE variant_id = ?",
                    (vid,)
                ).fetchone()[0]

                result["variants"][label] = {
                    "variant_id": vid,
                    "firings": firings,
                    "outcomes": stats.total_outcomes,
                    "positive": stats.positive_count,
                    "negative": stats.negative_count,
                    "neutral": stats.neutral_count,
                    "positive_rate": round(stats.positive_rate, 3),
                    "experience_level": stats.experience_level,
                }

            # Check significance
            sig = engine.check_significance(running.test_id)
            result["significance"] = {
                "has_enough_samples": sig.get("has_enough_samples", False),
                "is_significant": sig.get("is_significant", False),
                "recommendation": sig.get("recommendation", ""),
            }

            return result

        except Exception as e:
            return {"status": "error", "error": str(e)}

    def mine_session_gold_to_sops(self, limit: int = 10, min_confidence: float = 0.6) -> Dict[str, Any]:
        """
        Mine archived session gold (75+ sessions, 345 insights) into SOP candidates.

        Process: session_insights → LLM classify → SOP candidate → quarantine.
        Only processes insights NOT yet fed to pipeline (fed_to_pipeline=0).

        Uses classify (64-tok) profile to decide: SOP_CANDIDATE / ALREADY_COVERED / NOISE.
        Then extract (128-tok) to generate the SOP content for candidates.

        Returns: {processed, candidates_created, noise_skipped, already_covered}
        """
        result = {"processed": 0, "candidates_created": 0,
                  "noise_skipped": 0, "already_covered": 0, "errors": 0}
        archive_db = Path.home() / ".context-dna" / "session_archive.db"
        if not archive_db.exists():
            return result

        try:
            import sqlite3 as _sqlite3
            from memory.llm_priority_queue import butler_query

            archive_conn = _sqlite3.connect(str(archive_db), timeout=5)
            archive_conn.row_factory = _sqlite3.Row

            # Get unprocessed insights with enough confidence
            insights = archive_conn.execute("""
                SELECT id, session_id, insight_type, content, confidence
                FROM session_insights
                WHERE fed_to_pipeline = 0
                  AND confidence >= ?
                  AND length(content) > 30
                ORDER BY confidence DESC
                LIMIT ?
            """, (min_confidence, limit)).fetchall()

            if not insights:
                archive_conn.close()
                return result

            # Get existing SOP titles for dedup check
            existing_sops = set()
            try:
                cursor = self._sqlite_conn.execute(
                    "SELECT sop_title FROM sop_outcome_rollup"
                )
                existing_sops = {r[0].lower()[:60] for r in cursor.fetchall()}
            except Exception:
                pass

            # Also get existing learnings titles for broader dedup
            existing_learnings = set()
            try:
                from memory.sqlite_storage import get_sqlite_storage
                storage = get_sqlite_storage()
                all_learn = storage.query("", limit=200, _skip_hybrid=True)
                existing_learnings = {
                    str(l.get('content', ''))[:60].lower() for l in all_learn
                }
            except Exception:
                pass

            combined_existing = existing_sops | existing_learnings

            for insight in insights:
                result["processed"] += 1
                content = insight["content"]
                itype = insight["insight_type"]

                # Quick string dedup before LLM
                if content[:60].lower() in combined_existing:
                    result["already_covered"] += 1
                    try:
                        archive_conn.execute(
                            "UPDATE session_insights SET fed_to_pipeline=1 WHERE id=?",
                            (insight["id"],))
                        archive_conn.commit()
                    except Exception:
                        pass
                    continue

                # LLM classify: is this insight SOP-worthy?
                p_classify = (
                    f"Insight type: {itype}\n"
                    f"Content: {content[:300]}\n\n"
                    "Is this actionable enough to become a Standard Operating Procedure?\n"
                    "Reply ONE word: SOP_CANDIDATE, ALREADY_COVERED, or NOISE."
                )
                classify_result = butler_query(
                    "Classify session insight for SOP candidacy. "
                    "SOP_CANDIDATE=actionable fix/process. NOISE=vague/generic.",
                    p_classify, profile="classify"
                )

                verdict = "NOISE"
                if classify_result:
                    up = classify_result.strip().upper()
                    if "SOP_CANDIDATE" in up:
                        verdict = "SOP_CANDIDATE"
                    elif "ALREADY_COVERED" in up:
                        verdict = "ALREADY_COVERED"

                if verdict == "SOP_CANDIDATE":
                    # Generate SOP structure via LLM
                    p_gen = (
                        f"Session insight ({itype}): {content[:400]}\n\n"
                        "Generate a concise SOP from this insight.\n"
                        "Format:\n"
                        "TITLE: <short actionable title>\n"
                        "SYMPTOM: <what was observed>\n"
                        "FIX: <what to do>\n"
                        "VERIFY: <how to confirm>"
                    )
                    gen_result = butler_query(
                        "Generate SOP from session insight. Be specific.",
                        p_gen, profile="extract"
                    )

                    if gen_result and len(gen_result.strip()) > 40:
                        # Store as learning
                        try:
                            from memory.sqlite_storage import get_sqlite_storage
                            storage = get_sqlite_storage()
                            sop_type = "fix" if itype == "failures" else "pattern"
                            storage.store_learning({
                                'type': sop_type,
                                'content': gen_result.strip()[:500],
                                'source': f"session_gold:{insight['session_id'][:12]}",
                                'title': gen_result.strip()[:80],
                            })
                            result["candidates_created"] += 1
                            logger.info(
                                f"Gold→SOP: {gen_result.strip()[:60]}... "
                                f"(from session {insight['session_id'][:12]})"
                            )
                        except Exception as e:
                            result["errors"] += 1
                            logger.debug(f"Gold SOP storage failed: {e}")
                    else:
                        result["noise_skipped"] += 1
                elif verdict == "ALREADY_COVERED":
                    result["already_covered"] += 1
                else:
                    result["noise_skipped"] += 1

                # Mark as fed regardless of outcome
                try:
                    archive_conn.execute(
                        "UPDATE session_insights SET fed_to_pipeline=1 WHERE id=?",
                        (insight["id"],))
                    archive_conn.commit()
                except Exception:
                    pass

            archive_conn.close()

        except ImportError:
            logger.debug("LLM priority queue unavailable for gold mining")
        except Exception as e:
            logger.warning(f"Session gold mining failed: {e}")

        return result

    def record_learning_attribution(
        self,
        injection_id: str,
        learning_ids: List[str],
        section_id: str = "section_7",
    ) -> int:
        """
        Record which specific learnings were included in an injection.

        This is the per-learning outcome attribution gap — knowing which
        FTS5 learnings appeared in each injection so their individual
        effectiveness can be measured.

        Creates injection_learning rows that JOIN with outcome_event
        to compute per-learning success rates.

        Returns: number of rows inserted.
        """
        if not learning_ids:
            return 0

        inserted = 0
        try:
            self._sqlite_conn.execute("""
                CREATE TABLE IF NOT EXISTS injection_learning (
                    injection_id TEXT NOT NULL,
                    learning_id TEXT NOT NULL,
                    section_id TEXT DEFAULT 'section_7',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (injection_id, learning_id)
                )
            """)
            self._sqlite_conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_inj_learn_learning
                ON injection_learning(learning_id)
            """)

            for lid in learning_ids:
                try:
                    self._sqlite_conn.execute("""
                        INSERT OR IGNORE INTO injection_learning
                        (injection_id, learning_id, section_id)
                        VALUES (?, ?, ?)
                    """, (injection_id, lid, section_id))
                    inserted += 1
                except Exception:
                    pass

            self._safe_commit()
        except Exception as e:
            logger.debug(f"Learning attribution failed: {e}")

        return inserted

    def compute_per_learning_effectiveness(self, min_injections: int = 3) -> List[Dict]:
        """
        Compute per-learning effectiveness by joining injection_learning
        with outcome_event via injection_id.

        Returns learnings ranked by their impact on outcomes:
        success_rate, avg_reward, injection_count.
        """
        try:
            cursor = self._sqlite_conn.execute("""
                SELECT
                    il.learning_id,
                    COUNT(DISTINCT il.injection_id) as injection_count,
                    COUNT(DISTINCT oe.outcome_id) as outcome_count,
                    AVG(CASE WHEN oe.success = 1 THEN 1.0 ELSE 0.0 END) as success_rate,
                    AVG(COALESCE(oe.reward, 0)) as avg_reward
                FROM injection_learning il
                LEFT JOIN outcome_event oe ON oe.injection_id = il.injection_id
                GROUP BY il.learning_id
                HAVING injection_count >= ?
                ORDER BY success_rate DESC, injection_count DESC
            """, (min_injections,))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "learning_id": row[0],
                    "injection_count": row[1],
                    "outcome_count": row[2],
                    "success_rate": row[3] if row[3] is not None else 0.0,
                    "avg_reward": row[4] if row[4] is not None else 0.0,
                })
            return results
        except Exception as e:
            logger.debug(f"Per-learning effectiveness query failed: {e}")
            return []

    def close(self):
        """Close database connections."""
        if self._sqlite_conn:
            self._sqlite_conn.close()
        if self._pg_conn:
            self._pg_conn.close()


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_store_instance: Optional[ObservabilityStore] = None
_store_lock = threading.Lock()

def get_observability_store() -> ObservabilityStore:
    """Get or create the singleton observability store (thread-safe)."""
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = ObservabilityStore()
                try:
                    from memory.evented_write import get_evented_write_service
                    get_evented_write_service().gate(_store_instance, "observability_store")
                except Exception:
                    pass  # Fail-open — store works without event logging
    return _store_instance


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    store = get_observability_store()

    if len(sys.argv) < 2:
        print("=" * 60)
        print("OBSERVABILITY STORE")
        print("=" * 60)
        print(f"\nMode: {store.mode}")
        print(f"SQLite: {SQLITE_DB_PATH}")
        print(f"PostgreSQL: {'connected' if store._pg_conn else 'not available'}")
        print()
        print("Commands:")
        print("  python observability_store.py summary     # Dependency health summary")
        print("  python observability_store.py tasks       # Recent task runs")
        print("  python observability_store.py deps        # Recent dependency events")
        print("  python observability_store.py jobs        # Scheduled jobs (lite mode)")
        print("  python observability_store.py test        # Record test events")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "summary":
        print("=" * 60)
        print("DEPENDENCY HEALTH SUMMARY (Last 24h)")
        print("=" * 60)

        summary = store.get_dependency_summary(hours=24)
        if not summary:
            print("\nNo data recorded yet.")
        else:
            for name, stats in sorted(summary.items()):
                status_icon = "✓" if stats["last_status"] == "healthy" else "✗"
                print(f"\n{status_icon} {name}")
                print(f"  Health: {stats['healthy_pct']:.1f}% ({stats['healthy_count']}/{stats['total_checks']})")
                print(f"  Last: {stats['last_status']} @ {stats['last_check'][:19]}")

    elif cmd == "tasks":
        print("=" * 60)
        print("RECENT TASK RUNS")
        print("=" * 60)

        tasks = store.get_recent_task_runs(limit=20)
        for task in tasks:
            icon = "✓" if task["status"] == "success" else "✗"
            print(f"\n{icon} {task['task_name']} ({task['run_type']})")
            print(f"  Status: {task['status']} | Duration: {task['duration_ms']}ms")
            print(f"  Time: {task['timestamp_utc'][:19]}")

    elif cmd == "deps":
        print("=" * 60)
        print("RECENT DEPENDENCY EVENTS")
        print("=" * 60)

        events = store.get_recent_dependency_events(limit=20)
        for event in events:
            icon = "✓" if event["status"] == "healthy" else "✗"
            print(f"\n{icon} {event['dependency_name']}: {event['status']}")
            if event["latency_ms"]:
                print(f"  Latency: {event['latency_ms']}ms")
            if event["reason_code"]:
                print(f"  Reason: {event['reason_code']}")
            print(f"  Time: {event['timestamp_utc'][:19]}")

    elif cmd == "jobs":
        print("=" * 60)
        print("SCHEDULED JOBS (Lite Mode)")
        print("=" * 60)

        cursor = store._sqlite_conn.execute("SELECT * FROM job_schedule ORDER BY next_run_utc")
        jobs = list(cursor)

        if not jobs:
            print("\nNo jobs scheduled.")
        else:
            for job in jobs:
                print(f"\n{job['job_name']}")
                print(f"  Interval: {job['interval_s']}s")
                print(f"  Next run: {job['next_run_utc'][:19]}")
                if job['last_status']:
                    print(f"  Last: {job['last_status']} @ {job['last_run_utc'][:19] if job['last_run_utc'] else 'never'}")

    elif cmd == "test":
        print("Recording test events...")

        # Test dependency status
        event_id = store.record_dependency_status(
            dependency_name="test_service",
            status="healthy",
            latency_ms=42,
            details={"test": True}
        )
        print(f"✓ Dependency event: {event_id}")

        # Test task run
        event_id = store.record_task_run(
            task_name="test_task",
            run_type="manual",
            status="success",
            duration_ms=100,
            details={"test": True}
        )
        print(f"✓ Task run event: {event_id}")

        # Test recovery
        event_id = store.record_recovery_attempt(
            service_name="test_service",
            success=True,
            duration_ms=500,
            message="Test recovery successful"
        )
        print(f"✓ Recovery event: {event_id}")

        print("\nTest events recorded. Run 'deps' or 'tasks' to view.")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
