 #!/usr/bin/env python3
"""
LITE MODE SCHEDULER - Lightweight Task Scheduler (Replaces Celery Beat)

A simple, SQLite-backed job scheduler for Context DNA lite mode.
Runs scheduled tasks without requiring RabbitMQ, Redis, or Celery.

Features:
- SQLite-based job scheduling (no external dependencies)
- Compatible with observability_store for tracking
- Graceful fallback when heavy mode unavailable
- Configurable job intervals

Jobs (matching Celery beat_schedule):
- scan_project: every 30s
- brain_cycle: every 5m
- success_detection: every 60s
- refresh_relevance: every 2m
- (distill_skills: REMOVED — covered by brain_cycle)
- (consolidate_patterns: REMOVED — covered by session_gold_mining)

Usage:
    from memory.lite_scheduler import LiteScheduler

    scheduler = LiteScheduler()
    scheduler.run()  # Blocking main loop

    # Or async
    await scheduler.run_async()
"""

import asyncio
import json
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Callable, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Job:
    """A scheduled job definition."""
    name: str
    interval_s: int
    func: Callable
    args: tuple = ()
    kwargs: dict = None
    budget_ms: Optional[int] = None  # Time budget per spec Section 3

    def __post_init__(self):
        if self.kwargs is None:
            self.kwargs = {}


class LiteScheduler:
    """
    Lightweight task scheduler for Context DNA lite mode.

    Replaces Celery Beat when running without RabbitMQ/Redis.
    Uses SQLite for persistence via ObservabilityStore.
    """

    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._running = False
        self._store = None

        # Cached job instances (prevent memory leak from re-instantiation)
        self._cached_hindsight_validator = None
        self._cached_failure_analyzer = None
        self._cached_butler_repair = None
        # _cached_mmotw_miner removed — job pruned (covered by session_gold_mining)
        self._cached_meta_analyzer = None
        self._cached_health_monitor = None
        self._cached_session_historian = None

        # LLM watchdog state (mlx_lm.server)
        self._vllm_consecutive_failures = 0

        # Jobs that use local LLM (must serialize through priority queue)
        # These get throttled to 1 per cycle to avoid queue contention
        # NOTE: anticipation_engine is NOT in this set.
        # It manages its own GPU lock internally via priority queue (P2 priority).
        # Including it here caused starvation: 14 LLM jobs competing for 1 slot/cycle,
        # anticipation got ~1 turn every 7+ min while cache TTL is only 600s → stale.
        self._LLM_JOBS = {
            "code_patrol", "hindsight_check", "container_diagnostics",
            "llm_learnings_dedup", "post_session_meta_analysis",
            "sop_llm_evaluation", "cross_session_verification", "session_gold_mining",
            "session_historian",
            "wisdom_refinement", "markdown_memory_scan",
            "ab_scan_candidates", "ab_conclude_ready", "ab_auto_validate",
            "cv_sentinel",
        }

        # Default jobs (matching Celery beat_schedule intervals)
        self._register_default_jobs()

    def _get_store(self):
        """Get observability store (lazy init)."""
        if self._store is None:
            try:
                from memory.observability_store import get_observability_store
                self._store = get_observability_store()
            except Exception as e:
                logger.warning(f"Failed to get observability store: {e}")
        return self._store

    def _register_default_jobs(self):
        """Register the default job schedule (matching Celery beat)."""
        # Scanner: Check for file changes
        self.register_job(
            "scan_project",
            interval_s=120,  # Reduced from 30s — session_watcher_health covers real-time via FSEvents
            func=self._run_scan_project
        )

        # Brain cycle: Run consolidation
        self.register_job(
            "brain_cycle",
            interval_s=300,  # 5 minutes
            func=self._run_brain_cycle
        )

        # Success detection: Check work log
        self.register_job(
            "success_detection",
            interval_s=60,
            func=self._run_success_detection
        )

        # Relevance refresh: Update context packs
        self.register_job(
            "refresh_relevance",
            interval_s=120,  # 2 minutes
            func=self._run_refresh_relevance
        )

        # distill_skills: PRUNED — covered by brain_cycle
        # consolidate_patterns: PRUNED — covered by session_gold_mining

        # Session file watcher health check (replaces old 120s batch polling)
        # Actual dialogue sync is now real-time via FSEvents watcher
        self.register_job(
            "session_watcher_health",
            interval_s=30,
            func=self._run_session_watcher_health
        )

        # =====================================================
        # TTL DECAY (Expire stale claims)
        # =====================================================

        self.register_job(
            "ttl_decay",
            interval_s=3600,  # 1 hour
            func=self._run_ttl_decay,
            budget_ms=2000
        )

        # =====================================================
        # METRICS ROLLUP JOBS (Per spec Section 1.2)
        # =====================================================

        # Compute variant/section/claim rollups (every 60s, 500ms budget)
        self.register_job(
            "compute_rollups",
            interval_s=60,
            func=self._run_compute_rollups,
            budget_ms=500
        )

        # Evaluate quarantine status (every 5m, 1000ms budget)
        self.register_job(
            "evaluate_quarantine",
            interval_s=300,  # 5 minutes
            func=self._run_evaluate_quarantine,
            budget_ms=1000
        )

        # Health check (fast, 50ms budget)
        self.register_job(
            "health_check",
            interval_s=5,
            func=self._run_health_check,
            budget_ms=50
        )

        # =====================================================
        # INJECTION MONITORING (CRITICAL - Webhook Health)
        # =====================================================

        # Injection health monitor (every 60s, 200ms budget)
        # Monitors webhook payloads, sections 0-8, 8th Intelligence
        self.register_job(
            "injection_health",
            interval_s=60,
            func=self._run_injection_health,
            budget_ms=200
        )

        # =====================================================
        # TRUSTED → WISDOM PROMOTION (CRITICAL BRIDGE)
        # =====================================================
        # Bridges the gap between:
        #   _run_evaluate_quarantine() → promotes to 'trusted' (claim.status='active')
        #   professor.apply_learnings_to_wisdom() → processes 'flagged_for_review'
        # Without this job, trusted claims sit forever as 'active' and never
        # become wisdom/SOPs. This is the missing link in the evidence pipeline.

        self.register_job(
            "promote_trusted_to_wisdom",
            interval_s=600,  # 10 minutes
            func=self._run_promote_trusted_to_wisdom,
            budget_ms=3000
        )

        # =====================================================
        # PROFESSOR REINFORCEMENT LOOP (Compounding Mechanism)
        # =====================================================
        # When professor advice leads to SUCCESS → reinforce (+0.03)
        # When professor advice leads to FAILURE → penalize (-0.05)
        # This is THE compounding mechanism — learnings that help get
        # stronger, learnings that don't get weaker. Without this,
        # all memories have equal weight regardless of track record.
        self.register_job(
            "professor_refine_from_outcomes",
            interval_s=600,  # 10 minutes
            func=self._run_professor_refine,
            budget_ms=3000
        )

        # =====================================================
        # PROFESSOR CONFIDENCE DECAY (Natural Selection)
        # =====================================================
        # Complement to reinforcement: domains with no recent outcomes
        # gradually lose confidence. 30d=-0.05, 60d=-0.10, 90d=-0.15.
        # Floor: 0.3. Combined with refine_from_outcomes, this creates
        # natural selection — useful wisdom stays strong, stale fades.
        self.register_job(
            "professor_confidence_decay",
            interval_s=86400,  # Daily
            func=self._run_professor_decay,
            budget_ms=2000
        )

        # =====================================================
        # EVIDENCE GRADE RE-EVALUATION (Periodic Ladder Climb)
        # =====================================================
        # auto_upgrade_evidence_grade() only runs during quarantine→trusted
        # transition. Active claims accumulating new outcomes never get
        # re-evaluated. This job sweeps all active claims periodically.
        self.register_job(
            "evidence_grade_reevaluation",
            interval_s=600,  # 10 minutes
            func=self._run_evidence_grade_reevaluation,
            budget_ms=2000
        )

        # Quality cardiologist: when Pass 6 detects degraded webhook
        # dimensions, investigate root cause via 3-surgeon pattern and
        # produce remediation actions (retire stale learnings, flag gaps).
        self.register_job(
            "quality_cardiologist",
            interval_s=900,  # 15 minutes — batches degraded investigations
            func=self._run_quality_cardiologist,
            budget_ms=5000
        )

        # =====================================================
        # WATCHDOG FAILSAFE (Redundant Notification Path)
        # =====================================================

        # Monitor watchdog daemon health (every 30s)
        # If watchdog fails, lite_scheduler sends notifications directly
        self.register_job(
            "watchdog_failsafe",
            interval_s=30,
            func=self._run_watchdog_failsafe,
            budget_ms=100
        )

        # COMPREHENSIVE HEALTH (All 9 Subsystems + Notifications)
        self.register_job(
            "comprehensive_health",
            interval_s=300,  # Reduced from 120s — health_check(5s)+injection_health(60s) cover fast checks
            func=self._run_comprehensive_health,
            budget_ms=5000
        )

        # LLM HEARTBEAT — prevents false llm_down during idle periods.
        # The llm:health Redis key (300s TTL) is only updated on real LLM requests.
        # During idle (no prompts for 5+ min), the key expires and the watchdog
        # thinks the LLM is down → unnecessary restart. This job pings the LLM
        # process directly (pgrep + RSS check) and refreshes the health key.
        self.register_job(
            "llm_heartbeat",
            interval_s=180,  # 3 min — well within 300s TTL
            func=self._run_llm_heartbeat,
            budget_ms=100
        )

        # =====================================================
        # ADAPTIVE BIDIRECTIONAL SYNC (SQLite ↔ PostgreSQL)
        # Auto-discovers tables, creates PG mirrors, syncs
        # data both directions. Adapts to schema changes.
        # =====================================================
        self.register_job(
            "adaptive_sync",
            interval_s=300,  # 5 minutes
            func=self._run_adaptive_sync,
            budget_ms=30000  # 30s — schema introspection + data sync
        )

        # =====================================================
        # CONTAINER DIAGNOSTICS (Butler Handyman)
        # Uses local LLM to diagnose Docker container failures,
        # suggest fixes, and keep the mansion's plumbing working.
        # =====================================================
        self.register_job(
            "container_diagnostics",
            interval_s=900,  # Reduced from 300s — containers don't change every 5min
            func=self._run_container_diagnostics,
            budget_ms=60000  # 60s — LLM inference ~13s/container × 3 max
        )

        # =====================================================
        # CODE PATROL (Butler's Notepad for Batman)
        # Scans recent job failures and error patterns,
        # records code issues by anatomic location for
        # Atlas to review and fix.
        # =====================================================
        self.register_job(
            "code_patrol",
            interval_s=600,  # 10 minutes
            func=self._run_code_patrol,
            budget_ms=45000  # 45s — may invoke LLM
        )

        # =====================================================
        # HINDSIGHT VALIDATION (Butler monitors dialogue mirror)
        # Verifies "wins" via dialogue mirror analysis.
        # Detects repeated failure patterns, NOT individual failures.
        # Uses local LLM for miswiring root-cause analysis.
        # =====================================================
        self.register_job(
            "hindsight_check",
            interval_s=600,  # 10 minutes — butler actively monitors for miswirings
            func=self._run_hindsight_check,
            budget_ms=60000  # 60s — LLM analysis for misiwrings
        )

        # =====================================================
        # FAILURE PATTERN ANALYZER (Butler scans for high-yield patterns)
        # Unifies DialogueMirror + TemporalValidator + HindsightValidator
        # Generates LANDMINE warnings for Section 2 webhook injection
        # =====================================================
        self.register_job(
            "failure_pattern_analysis",
            interval_s=14400,  # 4 hours
            func=self._run_failure_pattern_analysis,
            budget_ms=90000  # 90s — may invoke LLM for pattern classification
        )

        # =====================================================
        # SKELETAL INTEGRITY (Butler DB repair — handyman protocol)
        # Checks all 11 SQLite databases for corruption.
        # Self-heals via: sqlite3 .recover → PG restore → recreate.
        # Records repairs as evidence-tracked SOPs.
        # =====================================================
        self.register_job(
            "skeletal_integrity",
            interval_s=1800,  # 30 minutes
            func=self._run_skeletal_integrity,
            budget_ms=120000  # 2 min — PG restore can take time
        )

        # mmotw_repair_mining: PRUNED — covered by session_gold_mining 16-pass architecture

        # =====================================================
        # COMPLEXITY VECTOR SENTINEL (Ecosystem drift detection)
        # Watches session gold + repair SOPs for complexity vector
        # risk signals. Injects warnings into S0 when risk detected.
        # The Neurologist's continuous protective awareness loop.
        # =====================================================
        try:
            from memory.complexity_vector_sentinel import job_cv_sentinel_cycle
            self.register_job(
                "cv_sentinel",
                interval_s=600,  # Reduced from 300s — vector drift is slow-moving
                func=job_cv_sentinel_cycle,
                budget_ms=15000
            )
        except ImportError as e:
            logger.warning(f"CV sentinel not available: {e}")

        # =====================================================
        # SOP DEDUPLICATION ANALYSIS (Butler hygiene)
        # Scans prompt_patterns for duplicates, near-duplicates,
        # category imbalances, and unused patterns.
        # Keeps the hook evolution library clean.
        # =====================================================
        self.register_job(
            "sop_dedup_analysis",
            interval_s=14400,  # 4 hours — library hygiene
            func=self._run_sop_dedup_analysis,
            budget_ms=60000  # 1 min — SQLite scans only
        )

        # =====================================================
        # VECTOR EMBEDDING BACKFILL (Semantic Search)
        # Generates embeddings for learnings without them.
        # Initial: 313 learnings at 50/batch → ~7 runs to complete.
        # Incremental: catches 5-20 new captures/day.
        # Enables hybrid FTS5+vector search for knowledge gaps.
        # =====================================================
        self.register_job(
            "embedding_backfill",
            interval_s=3600,  # 1 hour
            func=self._run_embedding_backfill,
            budget_ms=120000  # 2 min — model inference
        )

        # =====================================================
        # LLM-POWERED LEARNINGS DEDUPLICATION (Every 1h)
        # 4-tier: exact hash → >85% similarity → LLM decision → keep both
        # Deduplicates learnings.db to remove noise (generic "Tests passed" etc.)
        # Budget 180s — uses LLM classify profile for nuanced merge decisions.
        # =====================================================
        self.register_job(
            "llm_learnings_dedup",
            interval_s=3600,  # 1 hour
            func=self._run_llm_learnings_dedup,
            budget_ms=180000  # 3 min — LLM-assisted batch
        )

        # =====================================================
        # WISDOM REFINEMENT (Evidence-Based — refine, don't block)
        # LLM refines generic learnings ("Tests passed") into specific
        # actionable wisdoms using dialogue context. Max 5/cycle.
        # Budget 120s — uses LLM extract profile.
        # =====================================================
        self.register_job(
            "wisdom_refinement",
            interval_s=3600,  # 1 hour
            func=self._run_wisdom_refinement,
            budget_ms=120000  # 2 min — LLM extract calls
        )

        # =====================================================
        # POST-SESSION META-ANALYSIS (Evidence-Based Section 11)
        # 4-phase pipeline: Summarize → Cross-ref → Synthesize → Feed back
        # Runs every 30min; only acts when session gap detected.
        # Budget 180s for LLM summarization phases.
        # =====================================================
        self.register_job(
            "post_session_meta_analysis",
            interval_s=1800,  # 30 min — checks for session end
            func=self._run_post_session_meta_analysis,
            budget_ms=180000  # 3 min — LLM summarization phases
        )

        # =====================================================
        # CODEBASE MAP REFRESH — RE-ENABLED with git-first change detection
        # Was disabled due to 2.25GB memory spike (full AST parse of 1281 files).
        # Now uses git diff for O(1) change detection — only re-parses changed files.
        # Full hash scan fallback if git unavailable. Safe at 10min intervals.
        # =====================================================
        self.register_job(
            "codebase_map_refresh",
            interval_s=600,  # 10 min — git diff is fast, but no need for 5min
            func=self._run_codebase_map_refresh,
            budget_ms=15000  # 15s — incremental should be fast, budget for fallback
        )

        # =====================================================
        # LLM WATCHDOG (Synaptic's Brain Monitor)
        # Auto-restarts mlx_lm.server if stalled. Without LLM,
        # Section 2 (Professor), Section 8 (8th Intelligence),
        # and all LLM-first features degrade to templates/silence.
        # 3-strike pattern: 3 consecutive failures → restart.
        # =====================================================
        self.register_job(
            "llm_watchdog",
            interval_s=60,
            func=self._run_llm_watchdog,
            budget_ms=10000  # 10s — includes potential restart wait
        )

        # =====================================================
        # SEMANTIC EMBEDDING INDEX (Rescue Layer — P2.1)
        # Incrementally indexes new learnings for semantic search.
        # Activates when FTS5 returns <3 results (~15% of queries).
        # Model: all-MiniLM-L6-v2 (80MB, lazy-loaded).
        # Silently degrades if sentence-transformers not installed.
        # =====================================================
        self.register_job(
            "semantic_embedding_index",
            interval_s=600,  # 10 min — incremental, only indexes new learnings
            func=self._run_semantic_embedding_index,
            budget_ms=60000  # 60s — batch encoding ~300 learnings in 5s
        )

        # =====================================================
        # CODE CHUNK INDEXER (Neo Cortex — semantic code search)
        # Parses Python files into function-level searchable chunks
        # =====================================================
        self.register_job(
            "code_chunk_rebuild",
            interval_s=3600,  # Hourly
            func=self._run_code_chunk_rebuild,
            budget_ms=120000  # 2 min
        )

        # =====================================================
        # WAL MODE ENFORCEMENT (P1.4 — Corruption Prevention)
        # Ensures all SQLite DBs use WAL journal mode.
        # Prevents corruption under concurrent access (proven
        # by .observability.db corruption in Session 4).
        # Runs at startup + every 6h (catches new DBs).
        # =====================================================
        self.register_job(
            "enable_wal_all_dbs",
            interval_s=21600,  # 6 hours
            func=self._run_enable_wal_all_dbs,
            budget_ms=5000  # 5s — fast PRAGMA calls
        )

        # =====================================================
        # WAL CHECKPOINT — .observability.db
        # Prevents WAL file unbounded growth (634KB+ observed).
        # PASSIVE mode: non-blocking, safe alongside active writers.
        # Complements enable_wal_all_dbs (6h TRUNCATE) with frequent
        # lightweight checkpoints on the highest-contention DB.
        # =====================================================
        self.register_job(
            "wal_checkpoint_observability",
            interval_s=120,  # 2 minutes
            func=self._run_wal_checkpoint,
            budget_ms=200  # checkpoint is fast
        )

        # =====================================================
        # SESSION HISTORIAN (Hippocampus — Live Session Learner)
        # Incrementally extracts from ALL Claude Code sessions
        # (including active). LLM-analyzes via Qwen14B,
        # stores gold on disk, feeds insights to evidence pipeline.
        # Only cleans raw files for stale, inactive sessions.
        # =====================================================
        self.register_job(
            "session_historian",
            interval_s=900,  # 15 minutes — full pipeline (extract+analyze+cleanup)
            func=self._run_session_historian,
            budget_ms=120000  # 2min — LLM analysis takes time
        )

        # SESSION HISTORIAN FAST (Near real-time active session sync)
        # Extracts ONLY from active sessions (open tabs) every 2 min.
        # Lightweight: no LLM analysis, no cleanup — just capture gold.
        # Enables session crash recovery via rehydration.
        # =====================================================
        self.register_job(
            "session_historian_fast",
            interval_s=120,  # 2 minutes — near real-time for active tabs
            func=self._run_session_historian_fast,
            budget_ms=15000  # 15s — lightweight extraction only
        )

        # =====================================================
        # USER SENTIMENT/INTENT ANALYSIS (P5)
        # Analyzes user's recent messages for mood and intent.
        # Feeds into Section 8 (8th Intelligence) so Synaptic
        # adapts tone: frustrated → diagnostic, ship mode → checklist.
        # =====================================================
        self.register_job(
            "user_sentiment",
            interval_s=300,  # 5 min — sentiment shifts aren't instant
            func=self._run_user_sentiment,
            budget_ms=5000  # 5s — rule-based is fast
        )

        # =====================================================
        # ANTICIPATION ENGINE (Predictive Webhook Pre-computation)
        # Pre-generates S2+S8 during idle LLM time for instant
        # webhook delivery on the NEXT user prompt (~200ms vs 77-100s).
        # Triggers: Redis pub/sub (real-time) + scheduler fallback.
        # =====================================================
        self.register_job(
            "anticipation_engine",
            interval_s=30,  # 30s — check for new dialogue to anticipate + refresh expiring cache
            func=self._run_anticipation_engine,
            budget_ms=180000  # 3 min — LLM generation takes time (no pressure)
        )

        # =====================================================
        # S10 STRATEGIC ANALYST (GPT-4.1, every 15 min)
        # Pre-computes strategic analysis from 6 sources.
        # External-only (OpenAI) — no GPU lock contention.
        # NOT in _LLM_JOBS — runs independently of local LLM.
        # =====================================================
        self.register_job(
            "strategic_analyst",
            interval_s=900,  # 15 minutes
            func=self._run_strategic_analyst,
            budget_ms=60000  # 60s — external API call (fast)
        )

        # =====================================================
        # A/B TEST EVALUATION (Weekly experiment analysis)
        # Checks if any A/B test has enough data to conclude.
        # Reports statistical significance via evolution log.
        # =====================================================
        self.register_job(
            "ab_test_evaluation",
            interval_s=604800,  # Weekly (7 days)
            func=self._run_ab_test_evaluation,
            budget_ms=10000  # 10s — SQLite queries only
        )

        # =====================================================
        # SOP LLM CAUSAL EVALUATION (Every 1h)
        # Multi-pass: (1) classify GENUINE/COINCIDENTAL/NOISE,
        #   (2) structural quality check, (3) cross-SOP dedup merge.
        # Uses extract (128-tok) + classify (64-tok) profiles.
        # =====================================================
        self.register_job(
            "sop_llm_evaluation",
            interval_s=3600,  # 1 hour
            func=self._run_sop_llm_evaluation,
            budget_ms=180000  # 3 min — multi-pass evaluation
        )

        # =====================================================
        # CROSS-SESSION WIN VERIFICATION (Every 30min)
        # Re-verifies wins from prior sessions — "did fix stick?"
        # Uses 64-token "classify" profile.
        # =====================================================
        self.register_job(
            "cross_session_verification",
            interval_s=1800,  # 30 min
            func=self._run_cross_session_verification,
            budget_ms=180000  # 3 min — thorough cross-session analysis
        )

        # llm_learnings_dedup already registered above (6h interval)

        # =====================================================
        # SESSION GOLD MINING — 16-PASS ARCHITECTURE
        # Rotates 2 of 16 passes per cycle. Full rotation every ~40 min.
        # LLM should ALWAYS be running passes when not otherwise occupied.
        # Each pass: classify(64tok) + extract(256tok), ~9s per item.
        # Critical findings → anticipation engine + big picture.
        # =====================================================
        self.register_job(
            "session_gold_mining",
            interval_s=180,  # 3 min — faster rotation clears backlog ~3x faster
            func=self._run_session_gold_mining,
            budget_ms=300000  # 5 min budget — 4 passes × ~5 items × ~9s each
        )

        # =====================================================
        # ARCHITECTURE TWIN REFRESH (Movement 4)
        # Auto-generates architecture.current.md from code analysis
        # + architecture.map.json. No LLM needed — filesystem scan only.
        # Also generates architecture.diff.md (planned vs actual gaps).
        # Smart refresh: git-aware, skips if no architecture-relevant changes.
        # Content fingerprint: detects actual structural drift, signals S3.
        # =====================================================
        self.register_job(
            "architecture_twin_refresh",
            interval_s=600,  # 10min check — smart skip makes this cheap
            func=self._run_architecture_twin_refresh,
            budget_ms=15000  # 15s — filesystem scan + markdown generation (when needed)
        )

        # =====================================================
        # MARKDOWN MEMORY LAYER — doc summarization via LLM
        # Scans .md files, sends changed ones to local LLM for
        # 2-4 sentence summaries. Rate limited to 5 per cycle.
        # =====================================================
        self.register_job(
            "markdown_memory_scan",
            interval_s=120,  # 2 minutes
            func=self._run_markdown_scan,
            budget_ms=30000  # 30s — up to 5 LLM digestions per cycle
        )

        # NOTE: architecture_twin_refresh is already registered above (600s interval).
        # Duplicate registration was here (300s) from Phase 6/7 rollout — removed.
        # Dict-keyed register_job meant the 300s version silently overwrote the 600s one.

        # =====================================================
        # TWIN REFRESH (Movement 4 — Module dependency graph)
        # Reads .code_chunks.db, extracts module-level dependencies,
        # writes architecture.map.json to .projectdna/.
        # Pure DB + JSON — no LLM needed.
        # =====================================================
        self.register_job(
            "twin_refresh",
            interval_s=900,  # 15 minutes
            func=self._run_twin_refresh,
            budget_ms=10000  # 10s — SQLite read + JSON write
        )

        # =====================================================
        # AUTONOMOUS A/B TESTING (Self-Enhancing Engine)
        # 5 jobs manage the full A/B lifecycle autonomously.
        # Safety: auto-revert on degradation, 30-min grace veto,
        # max 1 concurrent test, $2/day budget cap.
        #
        # ZSF: memory.ab_autonomous was deprecated and removed (commit
        # 4553df794, 2026-04-25). Wrap import so scheduler keeps booting
        # the other 50+ jobs. Missing-module is observable via the
        # ab_autonomous_unavailable counter + WARNING log; never silent.
        # =====================================================
        try:
            from memory.ab_autonomous import (  # type: ignore[import-not-found]
                job_scan_candidates, job_activate_approved,
                job_monitor_active, job_conclude_ready, job_safety_check,
                job_auto_validate,
            )
            self.register_job(
                "ab_safety_check",
                interval_s=300,   # 5 min — safety critical, never starved
                func=job_safety_check,
                budget_ms=5000    # 5s — Redis metrics only, no LLM
            )
            self.register_job(
                "ab_monitor_active",
                interval_s=900,   # 15 min — timely degradation detection
                func=job_monitor_active,
                budget_ms=10000   # 10s — metrics capture + threshold check
            )
            self.register_job(
                "ab_activate_approved",
                interval_s=300,   # 5 min — check if grace period expired
                func=job_activate_approved,
                budget_ms=5000    # 5s — DB update + config apply
            )
            self.register_job(
                "ab_scan_candidates",
                interval_s=3600,  # hourly — background, non-urgent
                func=job_scan_candidates,
                budget_ms=120000  # 2 min — LLM classify + GPT-4.1 design + consensus
            )
            self.register_job(
                "ab_conclude_ready",
                interval_s=3600,  # hourly — background analysis
                func=job_conclude_ready,
                budget_ms=60000   # 1 min — GPT-4.1 conclusion analysis
            )
            self.register_job(
                "ab_auto_validate",
                interval_s=300,   # 5 min — check for pending fix validations
                func=job_auto_validate,
                budget_ms=300000  # 5 min — gains-gate + 3-surgeon consensus
            )
        except ImportError as _zsf_e:
            # Module deprecated/removed — record once, never silent.
            try:
                store = self._get_store()
                if store is not None:
                    store.increment_counter("ab_autonomous_unavailable", 1)
            except Exception as _zsf_store_e:  # noqa: BLE001 — observability best-effort
                logger.debug("zsf-swallow lite_scheduler ab_autonomous counter: %r", _zsf_store_e)
            logger.warning(
                "ab_autonomous module unavailable (deprecated 2026-04-25); "
                "skipping 6 A/B testing jobs: %s",
                _zsf_e,
            )

        # =====================================================
        # GHOSTSCAN BRIDGE (Probe Engine → Evidence Pipeline)
        # Runs multi-fleet probes against repo, stores findings
        # in SQLite, caches in Redis for webhook S3 injection.
        # Recurring findings (3+ occurrences) auto-promote to
        # evidence pipeline for wisdom consideration.
        # NOT in _LLM_JOBS — no LLM needed, pure probe engine.
        # =====================================================
        self.register_job(
            "ghostscan_background",
            interval_s=600,  # 10 minutes — lightweight cheap probes
            func=self._run_ghostscan_background,
            budget_ms=30000  # 30s — probe engine is fast
        )

        # GHOSTSCAN EVIDENCE PROMOTION (Recurring → Pipeline)
        # Checks for findings appearing 3+ times in 7 days.
        # Promotes them as claims into the evidence pipeline
        # for quarantine → trusted → wisdom progression.
        # Runs less frequently — accumulation takes time.
        # =====================================================
        self.register_job(
            "ghostscan_evidence_promotion",
            interval_s=3600,  # 1 hour — recurring patterns accumulate slowly
            func=self._run_ghostscan_evidence_promotion,
            budget_ms=10000  # 10s — SQLite queries + claim insertion
        )

    def register_job(
        self,
        name: str,
        interval_s: int,
        func: Callable,
        args: tuple = (),
        kwargs: dict = None,
        budget_ms: Optional[int] = None
    ):
        """
        Register a job to be scheduled.

        Args:
            name: Unique job identifier
            interval_s: Interval between runs in seconds
            func: Function to call
            args: Positional arguments
            kwargs: Keyword arguments
            budget_ms: Optional time budget (per spec Section 3)
        """
        self._jobs[name] = Job(
            name=name,
            interval_s=interval_s,
            func=func,
            args=args,
            kwargs=kwargs or {},
            budget_ms=budget_ms
        )

        # Register in observability store
        store = self._get_store()
        if store:
            store.register_job(name, interval_s)

        logger.info(f"Registered job: {name} (every {interval_s}s)")

    # Job execution timeouts (seconds) — prevents runaway jobs from spinning CPU
    _JOB_TIMEOUT_LLM = 300  # 5 min for LLM jobs (includes GPU lock wait + generation)
    _JOB_TIMEOUT_DEFAULT = 60  # 1 min for normal jobs

    def _execute_job(self, job: Job) -> tuple[bool, str, int, bool]:
        """
        Execute a single job with optional time budget enforcement.
        Jobs run in a sub-thread with a hard timeout to prevent CPU spinning.

        Returns:
            (success, message, duration_ms, exceeded_budget)
        """
        start_time = time.time()
        exceeded_budget = False
        timeout_s = self._JOB_TIMEOUT_LLM if job.name in self._LLM_JOBS else self._JOB_TIMEOUT_DEFAULT

        try:
            # Run job in sub-thread with hard timeout
            result_box = [None]
            error_box = [None]

            def _run():
                try:
                    result_box[0] = job.func(*job.args, **job.kwargs)
                except Exception as e:
                    error_box[0] = e

            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            worker.join(timeout=timeout_s)

            if worker.is_alive():
                duration_ms = int((time.time() - start_time) * 1000)
                logger.error(f"Job {job.name} TIMED OUT after {timeout_s}s — possible CPU spin")
                return False, f"timeout after {timeout_s}s", duration_ms, True

            if error_box[0]:
                raise error_box[0]

            result = result_box[0]
            duration_ms = int((time.time() - start_time) * 1000)

            # Check budget (per spec Section 3)
            if job.budget_ms and duration_ms > job.budget_ms:
                exceeded_budget = True
                logger.warning(
                    f"Job {job.name} exceeded budget: {duration_ms}ms > {job.budget_ms}ms"
                )

            if isinstance(result, tuple):
                success, message = result
            else:
                success = True
                message = str(result) if result else "completed"

            if exceeded_budget:
                message = f"{message} (exceeded budget)"

            return success, message, duration_ms, exceeded_budget

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            exceeded_budget = job.budget_ms and duration_ms > job.budget_ms
            logger.error(f"Job {job.name} failed: {e}")
            # Feed evidence pipeline with negative signal
            try:
                from memory.auto_capture import capture_failure
                capture_failure(
                    task=f"Scheduler job: {job.name}",
                    error=str(e)[:200],
                    area="scheduler",
                )
            except Exception:
                pass  # Never fail the failure capture
            return False, str(e), duration_ms, exceeded_budget

    def _process_due_jobs(self):
        """Check and run due jobs with LLM-aware throttling.

        LLM jobs: max 1 per cycle (local LLM handles 1 request at a time).
        Non-LLM jobs: max 3 per cycle (no contention).
        If LLM queue is busy (depth > 0), skip LLM jobs this cycle.
        """
        # Write freeze check — defer ALL jobs during migration drain
        try:
            from memory.write_freeze import get_write_freeze_guard
            guard = get_write_freeze_guard()
            if guard.is_frozen():
                logger.info("Write freeze active — deferring all scheduled jobs")
                return
        except Exception:
            pass  # Fail open — if freeze module unavailable, run jobs normally

        store = self._get_store()
        if not store:
            return

        due_jobs = store.get_due_jobs()
        if not due_jobs:
            return

        # Split into LLM and non-LLM jobs
        llm_due = [j for j in due_jobs if j.job_name in self._LLM_JOBS]
        non_llm_due = [j for j in due_jobs if j.job_name not in self._LLM_JOBS]
        total_original = len(due_jobs)

        # Non-LLM jobs: max 3 per cycle (no LLM contention)
        MAX_NON_LLM = 3
        if len(non_llm_due) > MAX_NON_LLM:
            logger.info(f"Throttling non-LLM: {len(non_llm_due)} due, running {MAX_NON_LLM}")
            non_llm_due = non_llm_due[:MAX_NON_LLM]

        # LLM jobs: max 1 per cycle, respect priority queue depth
        # The priority queue handles P1>P2>P3>P4 ordering automatically.
        # Aaron's queries (P1) always jump ahead of background P4 jobs.
        # We just avoid overfilling: if too many P4 jobs queued, defer.
        MAX_LLM = 1
        MAX_P4_QUEUE_DEPTH = 2  # Don't stack >2 P4 jobs (Aaron's P1 would wait)
        llm_queue_busy = False
        if llm_due:
            try:
                from memory.llm_priority_queue import get_queue_stats
                stats = get_queue_stats()
                depth = stats.get("queue_depth", 0)
                active = stats.get("active_priority")
                p4_count = stats.get("by_priority", {}).get(4, 0)

                if depth >= MAX_P4_QUEUE_DEPTH:
                    # Too many jobs queued — don't add more P4 work
                    llm_queue_busy = True
                    active_label = f"P{active}" if active else "idle"
                    logger.info(f"LLM queue depth={depth} (active={active_label}), "
                                f"deferring {len(llm_due)} LLM jobs (P4 backlog limit)")
                    llm_due = []
                else:
                    # Resource arbitrator: defer background LLM jobs when anticipation
                    # cache is about to expire — ensures webhook always gets fresh S2/S6/S8
                    cache_urgent = self._anticipation_cache_needs_refresh()
                    if cache_urgent and llm_due:
                        # Only defer if the due job ISN'T anticipation itself
                        non_anticipation = [j for j in llm_due
                                            if "anticipation" not in j.job_name]
                        if non_anticipation and len(non_anticipation) == len(llm_due):
                            logger.info(f"[arbitrator] Anticipation cache expiring soon, "
                                        f"deferring {len(llm_due)} background LLM jobs to yield GPU")
                            llm_due = []
                    if llm_due:
                        # Queue has room — submit 1 job (priority queue handles ordering)
                        logger.debug(f"LLM queue depth={depth}, submitting 1 of {len(llm_due)} LLM jobs")
                        llm_due = llm_due[:MAX_LLM]
            except Exception:
                llm_due = llm_due[:MAX_LLM]

        # Resource telemetry: publish utilization snapshot for observability
        self._publish_resource_telemetry(llm_due, non_llm_due, llm_queue_busy)

        # Merge and process
        due_jobs = non_llm_due + llm_due
        if len(due_jobs) < total_original:
            logger.info(f"Throttling: {total_original} due, running {len(due_jobs)} "
                        f"({len(non_llm_due)} non-LLM + {len(llm_due)} LLM)")

        for job_entry in due_jobs:
            job = self._jobs.get(job_entry.job_name)
            if not job:
                logger.warning(f"Job {job_entry.job_name} not found in registry")
                continue

            budget_info = f" (budget: {job.budget_ms}ms)" if job.budget_ms else ""
            logger.info(f"Running job: {job.name}{budget_info}")
            success, message, duration_ms, exceeded_budget = self._execute_job(job)

            # Determine status per spec
            if not success:
                status = "failure"
            elif exceeded_budget:
                status = "partial"  # Completed but over budget
            else:
                status = "success"

            # Record to observability
            store.record_task_run(
                task_name=job.name,
                run_type="scheduled",
                status=status,
                duration_ms=duration_ms,
                budget_ms=job.budget_ms,
                details={"message": message, "exceeded_budget": exceeded_budget},
                error={"message": message} if not success else None
            )

            # Mark job complete (schedules next run)
            store.mark_job_complete(
                job.name,
                status=status,
                error=message if not success else None
            )

            status_icon = "✓" if success else "✗"
            budget_warn = " ⚠️OVER BUDGET" if exceeded_budget else ""

            # Per-job RSS tracking (helps identify memory-heavy jobs)
            try:
                import resource
                rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
                rss_info = f" [RSS={rss_mb:.0f}MB]"
            except Exception:
                rss_info = ""

            # LLM queue stats after LLM jobs (observability)
            llm_info = ""
            if job.name in self._LLM_JOBS:
                try:
                    from memory.llm_priority_queue import get_queue_stats
                    qs = get_queue_stats()
                    llm_info = (f" [LLM: depth={qs.get('queue_depth', 0)}, "
                                f"total={qs.get('total_requests', 0)}, "
                                f"P4={qs.get('by_priority', {}).get(4, 0)}]")
                except Exception:
                    pass

            logger.info(f"{status_icon} Job {job.name} in {duration_ms}ms{budget_warn}{rss_info}{llm_info}")

            # V12 telemetry: log invocation to Redis for gains-gate audit
            try:
                import redis as _redis
                _r = _redis.Redis(decode_responses=True, socket_timeout=1)
                _inv_key = "contextdna:scheduler:invocations"
                _r.zadd(_inv_key, {json.dumps({
                    "job": job.name, "status": status,
                    "duration_ms": duration_ms, "ts": time.time(),
                }): time.time()})
                _r.zremrangebyrank(_inv_key, 0, -501)  # Keep last 500
            except Exception:
                pass  # Telemetry is best-effort

    # ─── Resource Arbitrator (additive wrapper) ───────────────────────────
    # Lightweight admission control that ensures anticipation cache refresh
    # gets GPU priority over background LLM jobs. No new files — wraps
    # existing scheduler throttling with cache-awareness + telemetry.

    def _anticipation_cache_needs_refresh(self, threshold_seconds: int = 120) -> bool:
        """Check if anticipation cache is about to expire.

        Returns True if ANY section (s2/s6/s8) has TTL < threshold_seconds,
        meaning the anticipation engine should get GPU priority over background jobs.
        """
        try:
            from memory.redis_cache import get_redis_client
            client = get_redis_client()
            if not client:
                return False

            # Check s2/s6/s8 cache TTLs for all active sessions
            for key in client.scan_iter(match="contextdna:anticipation:s*", count=50):
                key_str = key.decode() if isinstance(key, bytes) else key
                # Skip fallback keys and meta keys
                if ":fallback" in key_str or ":meta:" in key_str:
                    continue
                ttl = client.ttl(key)
                if 0 < ttl < threshold_seconds:
                    return True
            return False
        except Exception:
            return False  # If Redis fails, don't block scheduler jobs

    def _publish_resource_telemetry(self, llm_jobs_running: list, non_llm_running: list,
                                     queue_busy: bool):
        """Publish resource utilization snapshot to Redis for observability."""
        try:
            from memory.redis_cache import get_redis_client
            client = get_redis_client()
            if not client:
                return

            import json
            telemetry = {
                "timestamp": time.time(),
                "llm_jobs_this_cycle": len(llm_jobs_running),
                "non_llm_jobs_this_cycle": len(non_llm_running),
                "llm_queue_busy": queue_busy,
                "llm_job_names": [j.job_name for j in llm_jobs_running],
                "total_registered_jobs": len(self._jobs),
            }
            client.setex("scheduler:resource_telemetry", 300, json.dumps(telemetry))
        except Exception:
            pass  # Telemetry is best-effort, never block scheduler

    def run(self, check_interval: float = 1.0):
        """
        Run the scheduler (blocking).

        Args:
            check_interval: How often to check for due jobs (seconds)
        """
        logger.info("=" * 60)
        logger.info("LITE MODE SCHEDULER STARTING")
        logger.info(f"Registered jobs: {len(self._jobs)}")
        logger.info("=" * 60)

        self._running = True
        self._cycle_count = 0

        # Start real-time session file watcher (FSEvents)
        try:
            from memory.session_file_watcher import start_session_watcher
            if start_session_watcher():
                logger.info("Session file watcher started (FSEvents real-time)")
            else:
                logger.warning("Session file watcher failed to start")
        except Exception as e:
            logger.warning(f"Session file watcher init error: {e}")

        while self._running:
            try:
                self._process_due_jobs()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            # Log RSS every 30 cycles (~30s) to detect memory leaks
            self._cycle_count += 1
            if self._cycle_count % 30 == 0:
                try:
                    import resource
                    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
                    logger.info(f"[MEM] RSS={rss_mb:.1f}MB cycle={self._cycle_count}")
                except Exception:
                    pass

            time.sleep(check_interval)

        logger.info("Lite scheduler stopped")

    async def run_async(self, check_interval: float = 1.0):
        """
        Run the scheduler asynchronously.

        Args:
            check_interval: How often to check for due jobs (seconds)
        """
        logger.info("=" * 60)
        logger.info("LITE MODE SCHEDULER STARTING (ASYNC)")
        logger.info(f"Registered jobs: {len(self._jobs)}")
        logger.info("=" * 60)

        self._running = True

        while self._running:
            try:
                self._process_due_jobs()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            await asyncio.sleep(check_interval)

        logger.info("Lite scheduler stopped")

    def stop(self):
        """Stop the scheduler."""
        self._running = False

    # =========================================================================
    # DEFAULT JOB IMPLEMENTATIONS
    # =========================================================================

    def _run_scan_project(self) -> tuple[bool, str]:
        """Scan project for file changes."""
        try:
            # Import here to avoid circular imports
            from memory.auto_capture import scan_for_changes
            changes = scan_for_changes()
            return True, f"Scanned, {changes} changes detected"
        except ImportError:
            # Module not available
            return True, "scan_for_changes not available"
        except Exception as e:
            return False, str(e)

    def _run_brain_cycle(self) -> tuple[bool, str]:
        """Run brain consolidation cycle."""
        try:
            # Y4 cleanup (2026-05-07): the module-level export is `cycle`,
            # not `run_cycle`. `run_cycle` is a method on ArchitectureBrain.
            # Aliasing on import preserves the local callsite name without
            # touching the public memory.brain API.
            from memory.brain import cycle as run_cycle
            result = run_cycle()
            return True, str(result) if result else "cycle complete"
        except ImportError:
            return True, "brain.cycle not available"
        except Exception as e:
            return False, str(e)

    def _run_success_detection(self) -> tuple[bool, str]:
        """Detect successes via 4-layer EnhancedSuccessDetector → evidence pipeline."""
        try:
            from memory.architecture_enhancer import work_log
            from memory.auto_capture import capture_success

            # Use cached detector to avoid re-init per cycle (memory leak prevention)
            if not hasattr(self, '_cached_success_detector'):
                try:
                    from memory.enhanced_success_detector import EnhancedSuccessDetector
                    self._cached_success_detector = EnhancedSuccessDetector(
                        use_llm=True,  # Butler has 97% idle capacity — use LLM Layer 3
                        validation_window=300,
                    )
                except ImportError:
                    self._cached_success_detector = None

            # Enhanced path: 4-layer detection (regex → learned → LLM → temporal)
            if self._cached_success_detector:
                entries = work_log.get_recent_entries(hours=1, limit=50)
                if not entries:
                    return True, "no new entries"

                successes = self._cached_success_detector.analyze_entries(entries)
                high_conf = [s for s in successes if s.high_confidence]

                captured = 0
                processed_entries = []
                for s in high_conf[:3]:  # Max 3 per cycle
                    try:
                        capture_success(
                            task=s.task[:200],
                            details=f"{s.details[:150]} [layers:{','.join(s.detection_layers)}]",
                            area=s.area or "enhanced_detection",
                        )
                        captured += 1
                    except Exception:
                        pass

                # Learn from confirmed successes (self-improving patterns)
                for s in high_conf[:3]:
                    try:
                        self._cached_success_detector.learn_from_confirmed(s, entries)
                    except Exception:
                        pass

                # Mark processed to avoid double-capture
                if captured > 0:
                    try:
                        work_log.mark_entries_processed(entries[:len(high_conf)])
                    except Exception:
                        pass

                msg = f"{captured}/{len(successes)} high-conf successes captured (enhanced)"
            else:
                # Fallback: simple extraction (if enhanced detector unavailable)
                entries = work_log.get_successes(hours=1)
                if not entries:
                    return True, "no new successes"

                captured = 0
                for entry in entries[:3]:
                    try:
                        task = entry.get("content", "")[:200]
                        details = ""
                        meta = entry.get("metadata", {})
                        if isinstance(meta, dict):
                            details = meta.get("details", "") or ""
                        capture_success(task=task, details=details[:200], area="work_log")
                        captured += 1
                    except Exception:
                        pass

                if captured > 0:
                    try:
                        work_log.mark_entries_processed(entries[:3])
                    except Exception:
                        pass

                msg = f"{captured} successes captured (fallback)"

            # Resolve pending hook outcomes when success is detected
            if captured > 0:
                try:
                    from memory.hook_evolution import get_hook_evolution_engine
                    engine = get_hook_evolution_engine()
                    if engine:
                        engine.resolve_pending_outcomes("positive")
                except Exception:
                    pass

            return True, msg
        except ImportError:
            return True, "work_log not available"
        except Exception as e:
            return False, str(e)[:80]

    def _run_ab_test_evaluation(self) -> tuple[bool, str]:
        """Evaluate running A/B tests for statistical significance."""
        try:
            from memory.hook_evolution import get_hook_evolution_engine
            engine = get_hook_evolution_engine()
            if not engine:
                return True, "engine not available"

            tests = engine.list_ab_tests(status="running")
            if not tests:
                return True, "no running tests"

            results = []
            for test in tests:
                sig = engine.check_significance(test.test_id)
                ctrl_n = sig.get("control", {}).get("total", 0)
                a_n = sig.get("variant_a", {}).get("total", 0) if sig.get("variant_a") else 0
                has_enough = sig.get("has_enough_samples", False)
                is_sig = sig.get("is_significant", False)
                rec = sig.get("recommendation", "")

                results.append(f"{test.test_name}: ctrl={ctrl_n}n a={a_n}n sig={is_sig}")
                logger.info(
                    f"🧪 A/B Test '{test.test_name}': "
                    f"enough_samples={has_enough}, significant={is_sig}, "
                    f"recommendation={rec}"
                )

            return True, "; ".join(results)
        except Exception as e:
            logger.debug(f"A/B test evaluation failed: {e}")
            return False, str(e)[:80]

    def _run_refresh_relevance(self) -> tuple[bool, str]:
        """Refresh context relevance."""
        try:
            from memory.context import refresh_relevance
            refresh_relevance()
            return True, "relevance refreshed"
        except ImportError:
            return True, "context.refresh_relevance not available"
        except Exception as e:
            return False, str(e)

    def _run_distill_skills(self) -> tuple[bool, str]:
        """Distill skills from work log."""
        try:
            from memory.skill_distiller import distill
            count = distill()
            return True, f"{count} skills distilled"
        except ImportError:
            return True, "skill_distiller not available"
        except Exception as e:
            return False, str(e)

    def _run_consolidate_patterns(self) -> tuple[bool, str]:
        """Consolidate learned patterns."""
        try:
            from memory.pattern_consolidator import consolidate
            count = consolidate()
            return True, f"{count} patterns consolidated"
        except ImportError:
            return True, "pattern_consolidator not available"
        except Exception as e:
            return False, str(e)

    def _run_session_watcher_health(self) -> tuple[bool, str]:
        """Check session file watcher health + lite mode fallback.

        Three-tier degradation:
          Heavy: FSEvents → Redis pub/sub → agent_service (<500ms)
          Lite:  FSEvents → SQLite → scheduler polls SQLite (~3s)
          Emergency: scheduler polls JSONL directly (~5s)
        """
        try:
            from memory.session_file_watcher import get_session_watcher, start_session_watcher
            from memory.mode_authority import get_mode
            watcher = get_session_watcher()
            health = watcher.health()

            # Ensure watcher is running (tier 1+2 need it)
            if not health.get("running"):
                start_session_watcher()
                health = watcher.health()

            # Check if Redis path is alive (heavy mode)
            redis_ok = health.get("redis_connected", False)
            if not redis_ok:
                # Lite mode: poll DialogueMirror SQLite for recent messages
                self._lite_dialogue_poll()

            status_parts = [
                f"watcher={'ok' if health.get('running') else 'DOWN'}",
                f"files={health.get('tracked_files', 0)}",
                f"mode={get_mode()}",
            ]
            return True, ", ".join(status_parts)

        except ImportError:
            return True, "session_file_watcher not available"
        except Exception as e:
            return False, str(e)

    def _lite_dialogue_poll(self):
        """Lite mode fallback: poll DialogueMirror SQLite for recent messages.

        When Redis pub/sub is down, the watcher still writes to DialogueMirror
        SQLite. This polls that DB and broadcasts via agent_service HTTP API.
        """
        if not hasattr(self, "_lite_poll_ts"):
            self._lite_poll_ts = 0.0

        try:
            import sqlite3
            from pathlib import Path

            from memory.db_utils import get_unified_db_path
            db_path = get_unified_db_path(
                Path.home() / ".context-dna" / ".dialogue_mirror.db"
            )
            if not db_path.exists():
                return

            # Get messages newer than last poll
            cutoff = self._lite_poll_ts or (time.time() - 30)

            conn = sqlite3.connect(str(db_path), timeout=2)
            try:
                from memory.db_utils import unified_table
                _t_msgs = unified_table(".dialogue_mirror.db", "dialogue_messages")
                rows = conn.execute(
                    f"SELECT role, content, timestamp, session_id FROM {_t_msgs} "
                    "WHERE timestamp > ? ORDER BY timestamp ASC LIMIT 20",
                    (datetime.fromtimestamp(cutoff).isoformat(),)
                ).fetchall()
            finally:
                conn.close()

            if not rows:
                return

            # Update poll timestamp to latest message
            # Parse the ISO timestamp from the last row
            last_ts = rows[-1][2]
            try:
                from datetime import datetime as dt
                self._lite_poll_ts = dt.fromisoformat(last_ts).timestamp()
            except Exception:
                self._lite_poll_ts = time.time()

            # Write latest dialogue snapshot for webhook injection to read
            snapshot_path = Path.home() / ".context-dna" / ".dialogue_latest.json"
            snapshot = {
                "messages": [
                    {"role": r, "content": (c or "")[:300], "timestamp": t, "session_id": s}
                    for r, c, t, s in rows[-5:]  # Last 5 messages
                ],
                "source": "lite_poll",
                "polled_at": time.time(),
            }
            snapshot_path.write_text(json.dumps(snapshot))

            logger.info(f"Lite dialogue poll: {len(rows)} messages, snapshot written")

        except Exception as e:
            logger.debug(f"Lite dialogue poll error: {e}")

    # =========================================================================
    # TTL DECAY (Expire stale claims)
    # =========================================================================

    def _run_ttl_decay(self) -> tuple[bool, str]:
        """
        Expire claims past their TTL.

        Calls ObservabilityStore.enforce_ttl_decay() to transition
        active/quarantined claims to 'expired' when past ttl_seconds.
        """
        try:
            store = self._get_store()
            if not store:
                return True, "store unavailable"

            result = store.enforce_ttl_decay()
            return True, f"ttl decay: expired={result['expired']}, kept={result['kept']}"

        except Exception as e:
            return False, str(e)

    # =========================================================================
    # ROLLUP JOBS (Per spec Section 1.2, 1.3)
    # =========================================================================

    def _run_compute_rollups(self) -> tuple[bool, str]:
        """
        Compute metrics rollups for A/B testing analytics + SOP outcome scoring.

        Per spec Section 1.2: Populates SQLite rollup tables.
        Also computes SOP reliability scores from outcome data.
        """
        try:
            store = self._get_store()
            if not store:
                return True, "store unavailable"

            results = store.compute_all_rollups(window_minutes=60)

            # === SOP OUTCOME ROLLUP: Score SOPs by actual effectiveness ===
            sop_updated = 0
            try:
                sop_updated = store.compute_sop_outcome_rollup()
            except Exception:
                pass  # Table may not exist yet in older DBs

            # === Gap 3: SOP effectiveness rollup from sop_outcome_link ===
            sop_eff_updated = 0
            try:
                sop_ids = store._sqlite_conn.execute(
                    "SELECT DISTINCT sop_id FROM sop_outcome_link"
                ).fetchall()
                for row in sop_ids:
                    store.update_sop_effectiveness_rollup(row["sop_id"])
                    sop_eff_updated += 1
            except Exception:
                pass  # Table may not exist yet

            total_rows = sum(results.values())
            suffix = f" sop_scored={sop_updated}" if sop_updated else ""
            if sop_eff_updated:
                suffix += f" sop_eff={sop_eff_updated}"
            return True, f"rollups computed: {total_rows} rows ({results}){suffix}"

        except Exception as e:
            return False, str(e)

    def _run_evaluate_quarantine(self) -> tuple[bool, str]:
        """
        Evaluate quarantine status for claims/learnings/SOPs.

        Per spec Section 1.3:
        - Read knowledge_quarantine
        - Check direct_claim_outcome (primary) and claim_outcome_rollup (fallback)
        - Update BOTH knowledge_quarantine.status AND claim.status on promotion
        - Bootstrap threshold: n>=3, success_rate>=0.6
        - Mature threshold: n>=10, success_rate>=0.7
        - Age-based promotion: claims quarantined >48h with n>=1 and success_rate>=0.5
        """
        try:
            store = self._get_store()
            if not store:
                return True, "store unavailable"

            # Check if knowledge_quarantine table exists
            cursor = store._sqlite_conn.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='knowledge_quarantine'
            """)
            if not cursor.fetchone():
                return True, "knowledge_quarantine table not present"

            # Ensure direct_claim_outcome table exists
            store._sqlite_conn.execute("""
                CREATE TABLE IF NOT EXISTS direct_claim_outcome (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id          TEXT NOT NULL,
                    timestamp_utc     TEXT NOT NULL,
                    success           INTEGER NOT NULL CHECK (success IN (0, 1)),
                    reward            REAL NOT NULL DEFAULT 0.0,
                    source            TEXT NOT NULL DEFAULT 'auto',
                    notes             TEXT
                )
            """)

            # Get items in quarantine status
            cursor = store._sqlite_conn.execute("""
                SELECT item_id, item_type, promotion_rules_json, created_at_utc
                FROM knowledge_quarantine
                WHERE status IN ('quarantined', 'validating')
            """)

            promoted = 0
            rejected = 0
            evaluated = 0
            no_data = 0

            for row in cursor.fetchall():
                item_id = row["item_id"]
                item_type = row["item_type"]
                created_at = row["created_at_utc"]
                evaluated += 1

                # === PRIMARY PATH: direct_claim_outcome ===
                rollup = None
                try:
                    rollup = store.get_direct_claim_rollup(item_id)
                except Exception:
                    pass

                # === FALLBACK PATH: claim_outcome_rollup (injection-chain) ===
                if not rollup and item_type == "claim":
                    try:
                        outcome_cursor = store._sqlite_conn.execute("""
                            SELECT n, avg_reward, success_rate
                            FROM claim_outcome_rollup
                            WHERE claim_id = ?
                            ORDER BY window_end_utc DESC
                            LIMIT 1
                        """, (item_id,))
                        outcome = outcome_cursor.fetchone()
                        if outcome and outcome["n"] > 0:
                            rollup = {
                                "n": outcome["n"],
                                "success_rate": outcome["success_rate"],
                                "avg_reward": outcome["avg_reward"],
                            }
                    except Exception:
                        pass

                if rollup:
                    n = rollup["n"]
                    success_rate = rollup["success_rate"]

                    # Bootstrap threshold (n>=1): Initial promotion requires
                    #   any successful outcome + minimum 2h age for stability.
                    # Mature threshold (n>=10): Standard bar for established claims.
                    # Middle ground (n>=3): Moderate confidence level.
                    if n >= 10:
                        promote_n, promote_rate = 10, 0.7
                    elif n >= 3:
                        promote_n, promote_rate = 3, 0.6
                    else:
                        promote_n, promote_rate = 1, 0.5

                    if n >= promote_n:
                        if success_rate >= promote_rate:
                            self._promote_quarantine_item(
                                store, item_id, "trusted",
                                {"n": n, "success_rate": success_rate, "path": "outcome_data"}
                            )
                            promoted += 1
                            logger.info(
                                f"QUARANTINE PROMOTED: {item_id} "
                                f"(n={n}, rate={success_rate:.2f}, threshold=n>={promote_n})"
                            )
                        elif n >= 10 and success_rate < 0.3:
                            self._promote_quarantine_item(
                                store, item_id, "rejected",
                                {"n": n, "success_rate": success_rate, "path": "low_success_rate"}
                            )
                            rejected += 1
                            logger.info(
                                f"QUARANTINE REJECTED: {item_id} "
                                f"(n={n}, rate={success_rate:.2f})"
                            )
                else:
                    no_data += 1

                    # === AGE-BASED HANDLING (no outcomes) ===
                    # Absence of disconfirmation ≠ confirmation.
                    # 0 outcomes → remain quarantined. After 7 days → stale.
                    if created_at:
                        try:
                            age_cursor = store._sqlite_conn.execute("""
                                SELECT (julianday('now') - julianday(?)) * 24 AS hours_old
                            """, (created_at,))
                            age_row = age_cursor.fetchone()
                            hours_old = age_row[0] if age_row and age_row[0] else 0
                            if hours_old > 168:
                                self._promote_quarantine_item(
                                    store, item_id, "stale",
                                    {"n": 0, "success_rate": 0.0,
                                     "path": "age_based_stale_7d",
                                     "hours_old": round(hours_old, 1)}
                                )
                                logger.info(
                                    f"QUARANTINE STALE: {item_id} "
                                    f"(age={hours_old:.1f}h, 0 outcomes after 7d)"
                                )
                            # <=168h with 0 outcomes: remain quarantined (no action)
                        except Exception:
                            pass

            store._sqlite_conn.commit()

            msg = (
                f"quarantine evaluated: {evaluated} checked, "
                f"{promoted} promoted, {rejected} rejected, "
                f"{no_data} no outcome data"
            )
            if promoted > 0 or rejected > 0:
                logger.info(f"QUARANTINE EVAL: {msg}")
            return True, msg

        except Exception as e:
            logger.error(f"QUARANTINE EVAL ERROR: {e}")
            return False, str(e)

    def _promote_quarantine_item(
        self,
        store,
        item_id: str,
        new_status: str,
        stats: dict,
    ) -> None:
        """
        Promote/reject a quarantined item and sync claim.status.

        Updates BOTH knowledge_quarantine.status AND claim.status so the
        claim is actually usable after promotion.
        """
        import json as _json
        now = store._utc_now()
        stats_json = _json.dumps(stats)

        # Update knowledge_quarantine status
        store._sqlite_conn.execute("""
            UPDATE knowledge_quarantine
            SET status = ?,
                updated_at_utc = ?,
                validation_stats_json = ?
            WHERE item_id = ?
        """, (new_status, now, stats_json, item_id))

        # CRITICAL: Also update claim.status
        claim_status = "active" if new_status == "trusted" else ("quarantined" if new_status == "stale" else "rejected")
        store._sqlite_conn.execute("""
            UPDATE claim SET status = ?
            WHERE claim_id = ? AND status = 'quarantined'
        """, (claim_status, item_id))

        # Gap 5: Auto-upgrade evidence grade on promotion/rejection
        try:
            result = store.auto_upgrade_evidence_grade(item_id)
            if result:
                logger.info(
                    f"EVIDENCE GRADE: {item_id} {result['old_grade']} → {result['new_grade']} "
                    f"(n={result['n']}, sr={result['success_rate']:.2f})"
                )
        except Exception:
            pass  # Table may not exist in older DBs

    def _run_evidence_grade_reevaluation(self) -> tuple[bool, str]:
        """Re-evaluate evidence grades for all active claims.

        auto_upgrade_evidence_grade() only runs during quarantine→trusted
        transition (1-time). Active claims accumulating new outcomes via
        record_direct_claim_outcome() never get their grades re-evaluated.
        This job sweeps active claims and promotes grades that earned it.

        Batched: max 50 claims per run to stay within budget.
        """
        try:
            from memory.observability_store import get_observability_store
            store = get_observability_store()

            # Find active claims with outcomes that might earn a higher grade
            candidates = store._sqlite_conn.execute("""
                SELECT c.claim_id, c.evidence_grade,
                       COUNT(o.rowid) as n,
                       COALESCE(AVG(CASE WHEN o.success=1 THEN 1.0 ELSE 0.0 END), 0) as sr
                FROM claim c
                JOIN direct_claim_outcome o ON c.claim_id = o.claim_id
                WHERE c.status IN ('active', 'applied_to_wisdom')
                GROUP BY c.claim_id
                HAVING n >= 3
                ORDER BY n DESC
                LIMIT 50
            """).fetchall()

            if not candidates:
                return True, "No claims eligible for grade re-evaluation"

            promoted = 0
            for row in candidates:
                result = store.auto_upgrade_evidence_grade(row["claim_id"])
                if result:
                    promoted += 1
                    logger.info(
                        f"EVIDENCE RE-EVAL: {row['claim_id'][:20]}... "
                        f"{result['old_grade']} → {result['new_grade']} "
                        f"(n={result['n']}, sr={result['success_rate']:.2f})"
                    )

            msg = f"Re-evaluated {len(candidates)} claims, promoted {promoted}"
            if promoted > 0:
                logger.info(f"EVIDENCE RE-EVAL: {msg}")
            return True, msg

        except Exception as e:
            logger.error(f"EVIDENCE RE-EVAL ERROR: {e}")
            return False, str(e)

    def _run_quality_cardiologist(self) -> tuple[bool, str]:
        """Investigate degraded webhook quality dimensions via ChatGPT API.

        The Cardiologist IS the ChatGPT API (GPT-4.1-mini) — it has document
        research capability to vet findings against evidence and current state.

        Pipeline: Pass 6 queues degraded dims → Cardiologist researches evidence
        + diagnoses → critical findings promoted → Atlas spawns 3-agent git
        cross-exam → 3-surgeon corrigibility review.
        """
        try:
            import redis, json as _json
            r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

            # Drain up to 3 investigations per run
            investigations = []
            for _ in range(3):
                raw = r.rpop("quality:investigation_queue")
                if not raw:
                    break
                try:
                    investigations.append(_json.loads(raw))
                except Exception:
                    continue

            if not investigations:
                return True, "No quality investigations queued"

            remediation_count = 0
            critical_count = 0
            for inv in investigations:
                degraded = inv.get("degraded_dimensions", {})
                task_type = inv.get("task_type", "unknown")
                dims_str = ", ".join(f"{k}={v}/3" for k, v in degraded.items())

                # Step 1: Gather evidence context for the cardiologist
                evidence_text = self._cardiologist_gather_evidence(task_type, degraded)

                # Step 2: Call ChatGPT API (the cardiologist) with evidence
                diagnosis = self._cardiologist_diagnose(
                    inv, dims_str, task_type, evidence_text
                )
                if not diagnosis:
                    continue

                # Step 3: Store diagnosis in observability
                self._cardiologist_store_diagnosis(dims_str, task_type, diagnosis, inv)
                remediation_count += 1
                logger.info(f"QUALITY CARDIOLOGIST: diagnosed {dims_str} → {diagnosis[:100]}")

                # Step 4: Parse severity — promote critical findings
                severity = self._cardiologist_assess_severity(diagnosis, degraded, inv)
                finding = {
                    "type": "quality_degradation",
                    "severity": severity,
                    "dimensions": degraded,
                    "diagnosis": diagnosis[:500],
                    "task_type": task_type,
                    "total_score": inv.get("total_score", 0),
                    "timestamp": inv.get("queued_at", ""),
                }
                r.lpush("quality:cardiologist_findings", _json.dumps(finding))
                r.ltrim("quality:cardiologist_findings", 0, 19)

                if severity == "critical":
                    self._cardiologist_promote_critical(finding, r)
                    critical_count += 1

            msg = f"Investigated {len(investigations)}, diagnosed {remediation_count}"
            if critical_count:
                msg += f", {critical_count} CRITICAL promoted"
            return True, msg

        except ImportError as e:
            return True, f"Dependencies unavailable: {e}"
        except Exception as e:
            logger.error(f"QUALITY CARDIOLOGIST ERROR: {e}")
            return False, str(e)

    def _cardiologist_gather_evidence(self, task_type: str, degraded: dict) -> str:
        """Gather evidence context for the cardiologist to research against."""
        lines = []
        try:
            from memory.sqlite_storage import get_sqlite_storage
            storage = get_sqlite_storage()
            # Query learnings related to degraded dimensions
            for dim in degraded:
                query = f"webhook {dim} quality {task_type}"
                results = storage.query(query, limit=5)
                if results:
                    lines.append(f"\n## Evidence: {dim}")
                    for l in results[:3]:
                        lines.append(f"- [{l.get('type','?')}] {l.get('title','')}: "
                                     f"{l.get('content','')[:200]}")
        except Exception as e:
            lines.append(f"(evidence query failed: {e})")

        try:
            from memory.observability_store import ObservabilityStore
            obs = ObservabilityStore(mode="auto")
            # Check claims related to webhook quality
            for status in ("active", "quarantined"):
                claims = obs.get_claims_by_status(status, limit=20)
                keywords = ["webhook", "injection", "quality"] + list(degraded.keys())
                relevant = [c for c in claims
                           if any(kw in (c.get("statement", "") or "").lower()
                                  for kw in keywords)]
                if relevant:
                    lines.append(f"\n## Claims ({status})")
                    for c in relevant[:5]:
                        grade = c.get("evidence_grade", "?")
                        lines.append(f"- [{grade}] {c.get('statement', '')[:150]}")
        except Exception:
            pass

        try:
            from memory.observability_store import ObservabilityStore
            obs = ObservabilityStore(mode="auto")
            patterns = obs.get_frequent_negative_patterns(min_frequency=2)
            relevant = [p for p in patterns
                       if any(kw in (p.get("description", "") + p.get("pattern_key", "")).lower()
                              for kw in ["webhook", "injection", "quality"])]
            if relevant:
                lines.append("\n## Anti-Patterns")
                for p in relevant[:5]:
                    lines.append(f"- [{p.get('frequency',0)}x] {p.get('pattern_key','')}: "
                                 f"{p.get('description','')[:150]}")
        except Exception:
            pass

        return "\n".join(lines) if lines else "(no evidence available)"

    def _cardiologist_diagnose(self, inv: dict, dims_str: str, task_type: str,
                                evidence_text: str) -> str | None:
        """Call ChatGPT API (GPT-4.1-mini) for evidence-backed diagnosis."""
        system_prompt = (
            "You are the Cardiologist in a Surgery Team of 3 — a quality diagnostician "
            "for webhook injection systems. You have document research capability. "
            "Vet your findings against the evidence provided. Be specific, evidence-backed.\n\n"
            "For each degraded dimension, produce:\n"
            "DIMENSION: <name>\n"
            "CAUSE: <root cause, 15 words max, cite evidence if available>\n"
            "HYPOTHESIS: <testable prediction, 15 words max>\n"
            "FIX: <specific remediation action, 15 words max>\n"
            "SEVERITY: <critical|warning|info>\n\n"
            "Mark SEVERITY as 'critical' ONLY if: dimension=0, or multiple dims degraded "
            "simultaneously, or evidence shows recurring pattern. Otherwise 'warning' or 'info'."
        )
        user_prompt = (
            f"## Investigation\n"
            f"Webhook injection scored: {dims_str} (total={inv.get('total_score', '?')}/12)\n"
            f"Task type: {task_type}\n"
            f"Context: {inv.get('context_snippet', '')[:300]}\n\n"
            f"## Evidence & Current State\n{evidence_text[:2000]}\n\n"
            f"Diagnose each degraded dimension. Vet against the evidence above."
        )

        # Route through hybrid — uses GPT-4.1-mini via external fallback
        # Priority P2 ATLAS + profile extract_deep = eligible for external
        from memory.llm_priority_queue import llm_generate, Priority
        result = llm_generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            priority=Priority.ATLAS,
            profile="extract_deep",
            caller="quality_cardiologist",
            timeout_s=30.0,
        )
        if result:
            return result

        # Direct external fallback if local also failed
        try:
            from memory.llm_priority_queue import _external_fallback
            return _external_fallback(system_prompt, user_prompt,
                                      "extract_deep", "quality_cardiologist")
        except Exception:
            return None

    def _cardiologist_store_diagnosis(self, dims_str: str, task_type: str,
                                      diagnosis: str, inv: dict):
        """Store diagnosis in observability DB."""
        try:
            from memory.db_utils import connect_wal
            from pathlib import Path
            OBS_DB = Path("memory/.observability.db")
            if OBS_DB.exists():
                conn = connect_wal(OBS_DB)
                from datetime import datetime, timezone
                conn.execute("""
                    INSERT INTO observability_note
                    (note_id, timestamp_utc, note_type, content, score)
                    VALUES (?, ?, 'quality_cardiologist', ?, ?)
                """, (
                    f"qc_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                    datetime.now(timezone.utc).isoformat(),
                    f"DIMS: {dims_str}\nTASK: {task_type}\n{diagnosis}",
                    inv.get("total_score", 0),
                ))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.debug(f"Cardiologist store: {e}")

    def _cardiologist_assess_severity(self, diagnosis: str, degraded: dict,
                                       inv: dict) -> str:
        """Determine finding severity from diagnosis + metrics."""
        # Parse SEVERITY lines from diagnosis
        import re
        severity_matches = re.findall(r'SEVERITY:\s*(critical|warning|info)', diagnosis, re.I)
        has_critical = any(s.lower() == "critical" for s in severity_matches)

        # Heuristic escalation: total ≤4 OR 3+ degraded dims = critical
        total = inv.get("total_score", 12)
        if total <= 4 or len(degraded) >= 3:
            return "critical"
        if has_critical:
            return "critical"

        # Default: warning for degraded, info for minor
        return "warning" if len(degraded) >= 2 else "info"

    def _cardiologist_promote_critical(self, finding: dict, r):
        """Promote critical cardiologist finding to critical_findings table + Redis notification."""
        import json as _json
        try:
            from memory.db_utils import connect_wal
            from pathlib import Path
            from datetime import datetime, timezone
            ARCHIVE_DB = Path.home() / ".context-dna" / "session_archive.db"
            if ARCHIVE_DB.exists():
                conn = connect_wal(ARCHIVE_DB)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS critical_findings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pass_id TEXT NOT NULL,
                        finding TEXT NOT NULL,
                        severity TEXT DEFAULT 'critical',
                        session_id TEXT,
                        item_id TEXT,
                        found_at TEXT NOT NULL,
                        acknowledged INTEGER DEFAULT 0,
                        acknowledged_at TEXT,
                        action_taken TEXT,
                        promoted_from_tank INTEGER,
                        wired_to_anticipation INTEGER DEFAULT 0,
                        wired_to_bigpicture INTEGER DEFAULT 0
                    )
                """)
                dims = finding.get("dimensions", {})
                dims_str = ", ".join(f"{k}={v}/3" for k, v in dims.items())
                finding_text = (
                    f"CARDIOLOGIST CRITICAL: {dims_str} | "
                    f"task={finding.get('task_type', '?')} | "
                    f"total={finding.get('total_score', '?')}/12\n"
                    f"{finding.get('diagnosis', '')[:400]}"
                )
                conn.execute("""
                    INSERT INTO critical_findings
                    (pass_id, finding, severity, found_at, promoted_from_tank)
                    VALUES (?, ?, 'critical', ?, 0)
                """, (
                    "quality_cardiologist",
                    finding_text,
                    datetime.now(timezone.utc).isoformat(),
                ))
                conn.commit()
                conn.close()
                logger.warning(f"CARDIOLOGIST CRITICAL PROMOTED: {dims_str}")
        except Exception as e:
            logger.debug(f"Critical promotion: {e}")

        # Redis notification for Atlas to pick up
        try:
            notification = {
                "source": "quality_cardiologist",
                "severity": "critical",
                "diagnosis": finding.get("diagnosis", "")[:300],
                "dimensions": finding.get("dimensions", {}),
                "task_type": finding.get("task_type", ""),
                "timestamp": finding.get("timestamp", ""),
                "needs_cross_exam": True,
            }
            r.lpush("quality:critical_notifications", _json.dumps(notification))
            r.ltrim("quality:critical_notifications", 0, 9)
            # TTL flag for Atlas to detect new criticals
            r.setex("quality:new_critical", 3600, "1")
            # WAL: additive sorted set (never trimmed)
            try:
                from memory.session_gold_passes import _wal_append_critical
                _wal_append_critical({
                    "pass": "quality_cardiologist",
                    "finding": finding.get("diagnosis", "")[:300],
                    "severity": "critical",
                    "found_at": finding.get("timestamp", ""),
                    "verified": True,
                    "source": "cardiologist",
                })
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Critical notification: {e}")

        # EVENT-BASED: Immediately trigger 3-surgeon cross-exam via cardio-gate
        # No polling delay — drift correction happens the moment a critical is found
        self._cardiologist_trigger_cross_exam()

    def _cardiologist_trigger_cross_exam(self):
        """Event-based: fire cardio-gate.sh immediately on critical promotion.

        Like frequent EKG reviews — drift correction the moment an anomaly appears,
        not on a polling schedule. The gate script verifies gains are preserved
        before running the 3-surgeon cross-exam pipeline.
        """
        import subprocess
        gate_script = Path("scripts/cardio-gate.sh")
        if not gate_script.exists():
            logger.debug("cardio-gate.sh not found — skipping event trigger")
            return
        try:
            # Fire-and-forget: don't block the scheduler on the full cross-exam
            subprocess.Popen(
                [str(gate_script)],
                stdout=open("/tmp/atlas-agent-results/cardio_gate_latest.log", "w"),
                stderr=subprocess.STDOUT,
                cwd=str(Path.cwd()),
                env={**os.environ, "PYTHONPATH": "."},
            )
            logger.info("CARDIOLOGIST: Event-triggered cardio-gate.sh (fire-and-forget)")
        except Exception as e:
            logger.debug(f"Cardio gate trigger: {e}")

    def _run_llm_heartbeat(self) -> tuple[bool, str]:
        """Refresh llm:health Redis key during idle periods.

        Prevents false llm_down when no LLM requests have been made recently.
        Checks if mlx_lm process is alive + RSS indicates model loaded,
        then refreshes the health key so the watchdog doesn't false-trigger.
        """
        try:
            import subprocess as _sp
            # Check if mlx_lm process exists
            out = _sp.run(["pgrep", "-f", "mlx_lm"], capture_output=True, text=True, timeout=2)
            if not out.stdout.strip():
                return True, "llm not running, skipping heartbeat"

            # Check RSS — model loaded if >1.5GB
            pid = out.stdout.strip().split('\n')[0]
            ps = _sp.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True, timeout=2)
            rss_kb = int(ps.stdout.strip()) if ps.stdout.strip() else 0
            rss_mb = rss_kb // 1024
            if rss_mb < 1500:
                return True, f"llm process alive but RSS {rss_mb}MB (model not loaded)"

            # Process alive + model loaded → refresh health key
            from memory.llm_priority_queue import _update_health_status
            _update_health_status(True)
            return True, f"heartbeat: llm alive, RSS {rss_mb}MB, health key refreshed"
        except Exception as e:
            return True, f"heartbeat skip: {str(e)[:60]}"

    def _run_llm_watchdog(self) -> tuple[bool, str]:
        """Auto-restart local LLM (mlx_lm.server) if stalled (3-strike pattern).

        Uses Redis health cache (updated by priority queue on every request)
        + process RSS check to detect stalled model loads.
        NO direct HTTP to port 5044 — all health reads via Redis.

        History: Was vllm-mlx (crash-looped). Now mlx_lm.server (stable).
        """
        try:
            # Primary: Redis health cache (updated by llm_priority_queue)
            from memory.llm_priority_queue import check_llm_health
            llm_healthy = check_llm_health()

            if llm_healthy:
                # Verify model is actually loaded (not just port bound)
                # mlx_lm at 130MB = stalled, loaded model = 2-8GB
                rss_ok = True
                try:
                    import subprocess as _sp
                    out = _sp.run(["pgrep", "-f", "mlx_lm"], capture_output=True, text=True, timeout=2)
                    if out.stdout.strip():
                        pid = out.stdout.strip().split('\n')[0]
                        ps = _sp.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True, timeout=2)
                        rss_kb = int(ps.stdout.strip()) if ps.stdout.strip() else 0
                        rss_mb = rss_kb // 1024
                        if rss_mb < 1500:  # Model not loaded if <1.5GB RSS
                            rss_ok = False
                            logger.debug(f"LLM port up but RSS only {rss_mb}MB (model not loaded)")
                except Exception:
                    pass  # If RSS check fails, trust Redis health

                if rss_ok:
                    if self._vllm_consecutive_failures > 0:
                        logger.info(f"local LLM recovered after {self._vllm_consecutive_failures} failures")
                    self._vllm_consecutive_failures = 0
                    return True, "local LLM healthy"

            # Redis says down (or no recent health update) — also check process
            if not llm_healthy:
                try:
                    import subprocess as _sp
                    out = _sp.run(["pgrep", "-f", "mlx_lm"], capture_output=True, text=True, timeout=2)
                    if not out.stdout.strip():
                        # Process not running at all — definite failure
                        pass
                    else:
                        # Process running but Redis health expired — might just be idle
                        # Still count as failure for 3-strike pattern
                        pass
                except Exception:
                    pass

            self._vllm_consecutive_failures += 1

            if self._vllm_consecutive_failures < 3:
                return True, f"local LLM down {self._vllm_consecutive_failures}/3"

            # 3 consecutive failures — restart
            logger.warning(f"local LLM down for {self._vllm_consecutive_failures} checks, restarting...")
            try:
                from memory.ecosystem_health import start_missing_services
                actions = start_missing_services(dry_run=False)
                mlx_actions = [a for a in actions if "LLM" in a or "MLX" in a or "mlx" in a]
                self._vllm_consecutive_failures = 0
                msg = "; ".join(mlx_actions) if mlx_actions else "restart attempted"
                return True, f"local LLM restarted: {msg}"
            except Exception as e:
                return False, f"restart failed: {str(e)[:80]}"
        except Exception as e:
            return False, f"watchdog error: {str(e)[:80]}"

    def _run_health_check(self) -> tuple[bool, str]:
        """
        Fast health check (per spec Section 1.1, 50ms budget).

        Checks:
        - SQLite lock health
        - Disk free space
        - Memory usage
        """
        try:
            import os
            import shutil

            checks = []

            # SQLite lock test - quick read
            store = self._get_store()
            if store:
                cursor = store._sqlite_conn.execute("SELECT 1")
                cursor.fetchone()
                checks.append("sqlite:ok")
            else:
                checks.append("sqlite:unavailable")

            # Disk space check
            memory_dir = Path(__file__).parent
            disk = shutil.disk_usage(memory_dir)
            free_gb = disk.free / (1024**3)
            if free_gb < 1.0:
                checks.append(f"disk:low({free_gb:.1f}GB)")
            else:
                checks.append(f"disk:ok({free_gb:.1f}GB)")

            # Memory check (macOS/Linux)
            try:
                import resource
                usage = resource.getrusage(resource.RUSAGE_SELF)
                mem_mb = usage.ru_maxrss / (1024 * 1024)  # macOS returns bytes
                if mem_mb > 500:
                    checks.append(f"mem:high({mem_mb:.0f}MB)")
                else:
                    checks.append(f"mem:ok({mem_mb:.0f}MB)")
            except Exception:
                checks.append("mem:unknown")

            return True, " | ".join(checks)

        except Exception as e:
            return False, str(e)

    def _run_injection_health(self) -> tuple[bool, str]:
        """
        Monitor webhook injection health (CRITICAL).

        Checks:
        - Injection frequency (alert if stale)
        - Section health (0-8)
        - 8th Intelligence status (NEVER SLEEPS)
        - Sends notifications if issues detected
        """
        try:
            from memory.injection_health_monitor import run_injection_health_check
            return run_injection_health_check()
        except ImportError:
            return True, "injection_health_monitor not available"
        except Exception as e:
            return False, str(e)

    def _run_comprehensive_health(self) -> tuple[bool, str]:
        """Run comprehensive health check across all 9 subsystems with macOS notifications."""
        try:
            from memory.comprehensive_health_monitor import run_comprehensive_health_check
            return run_comprehensive_health_check()
        except ImportError:
            return False, "comprehensive_health_monitor not importable"
        except Exception as e:
            return False, "comprehensive health failed: " + str(e)

    def _run_watchdog_failsafe(self) -> tuple[bool, str]:
        """
        Monitor watchdog daemon health (FAILSAFE).

        If watchdog_daemon is not running, lite_scheduler takes over
        critical notifications directly.

        Notification Chain:
        lite_scheduler (60s) → watchdog_daemon → macOS notifications
                    ↓ (if watchdog fails)
        lite_scheduler → macOS notifications directly (FAILSAFE)
        """
        try:
            import subprocess

            # Check if watchdog daemon process is running (pgrep avoids psutil cache leak)
            watchdog_running = False
            try:
                result = subprocess.run(
                    ['pgrep', '-f', 'synaptic_watchdog_daemon'],
                    capture_output=True, text=True, timeout=3
                )
                watchdog_running = result.returncode == 0
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

            if watchdog_running:
                return True, "watchdog:running"

            # Watchdog is DOWN - take over notifications
            logger.warning("⚠️ Watchdog daemon not running - failsafe active")

            # Run injection health check directly and notify
            try:
                from memory.injection_health_monitor import get_webhook_monitor
                if self._cached_health_monitor is None:
                    self._cached_health_monitor = get_webhook_monitor()
                health = self._cached_health_monitor.check_health()

                if health.status == "critical":
                    # Watchdog down AND health critical — genuine failure
                    self._send_failsafe_notification(
                        title="🚨 Context DNA (Failsafe)",
                        message=f"Watchdog DOWN + {health.status.upper()}",
                        subtitle=health.alerts[0] if health.alerts else "Multiple issues detected"
                    )
                    return False, f"watchdog:down | health:critical"
                else:
                    # Watchdog down but failsafe is active and health is OK/warning
                    return True, f"watchdog:down | failsafe:active | health:{health.status}"

            except Exception as e:
                self._send_failsafe_notification(
                    title="🚨 Context DNA Critical",
                    message="Watchdog + Health Monitor Failed",
                    subtitle=str(e)[:50]
                )
                return False, f"watchdog:down | monitor:error | {e}"

        except ImportError:
            # psutil not available - just check via pgrep
            try:
                result = subprocess.run(
                    ["pgrep", "-f", "synaptic_watchdog_daemon"],
                    capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    return True, "watchdog:running (pgrep)"
                else:
                    return False, "watchdog:down (pgrep)"
            except Exception:
                return True, "watchdog:unknown (no psutil)"

        except Exception as e:
            return False, f"failsafe error: {e}"

    def _run_promote_trusted_to_wisdom(self) -> tuple[bool, str]:
        """
        Bridge: trusted claims → flagged_for_review → applied_to_wisdom.

        The evidence pipeline gap:
          evaluate_quarantine promotes quarantined → trusted (claim.status='active')
          professor.apply_learnings_to_wisdom processes 'flagged_for_review' only
          NO CODE transitions 'active' (from quarantine promotion) → 'flagged_for_review'

        This job finds claims that:
          1. Have a knowledge_quarantine entry with status='trusted'
          2. Have claim.status='active'
        And flags them for professor review, completing the pipeline.
        """
        try:
            store = self._get_store()
            if not store:
                return True, "store unavailable"

            # Find claims promoted from quarantine (trusted) but not yet flagged
            cursor = store._sqlite_conn.execute("""
                SELECT kq.item_id, c.statement, c.area
                FROM knowledge_quarantine kq
                JOIN claim c ON c.claim_id = kq.item_id
                WHERE kq.status = 'trusted'
                  AND c.status = 'active'
                LIMIT 50
            """)
            trusted_claims = cursor.fetchall()

            if not trusted_claims:
                return True, "no trusted claims pending promotion"

            flagged = 0
            for row in trusted_claims:
                claim_id = row[0]
                try:
                    store._sqlite_conn.execute("""
                        UPDATE claim SET status = 'flagged_for_review'
                        WHERE claim_id = ? AND status = 'active'
                    """, (claim_id,))
                    flagged += 1
                    logger.info(f"TRUSTED→FLAGGED: {claim_id}")
                except Exception as e:
                    logger.warning(f"Failed to flag claim {claim_id}: {e}")

            if flagged > 0:
                store._sqlite_conn.commit()
                logger.info(
                    f"PROMOTION BRIDGE: {flagged} trusted claims → flagged_for_review "
                    f"(will be processed by next brain_cycle → professor)"
                )

            return True, f"flagged {flagged}/{len(trusted_claims)} trusted claims"

        except Exception as e:
            logger.error(f"TRUSTED→WISDOM PROMOTION ERROR: {e}")
            return False, str(e)

    def _run_professor_refine(self) -> tuple[bool, str]:
        """
        THE COMPOUNDING MECHANISM.

        When professor advice leads to session SUCCESS → confidence +0.03 (reinforce)
        When professor advice leads to session FAILURE → confidence -0.05 (penalize)

        This is the reinforcement loop that makes Context DNA get smarter:
        - Learnings that consistently help get stronger (surface more prominently)
        - Learnings that don't help fade away (surface less or get pruned)
        - Without this, all memories have equal weight regardless of track record

        Calls professor.refine_from_outcomes() which reads outcome_tracker
        and adjusts professor domain confidences in .professor_domain_confidence.json
        """
        try:
            from memory.professor import refine_from_outcomes
            result = refine_from_outcomes(max_outcomes=50)
            processed = result.get("processed", 0)
            reinforced = result.get("reinforced", 0)
            penalized = result.get("penalized", 0)
            return True, f"processed {processed} outcomes: +{reinforced} reinforced, -{penalized} penalized"
        except ImportError:
            return True, "professor module not available"
        except Exception as e:
            logger.error(f"PROFESSOR REFINE ERROR: {e}")
            return False, str(e)

    def _run_professor_decay(self) -> tuple[bool, str]:
        """
        NATURAL SELECTION — complement to the reinforcement loop.

        Domains with no recent outcomes gradually lose confidence:
        - 30+ days stale: -0.05
        - 60+ days stale: -0.10
        - 90+ days stale: -0.15
        - Floor: 0.3 (never fully forget)

        Combined with refine_from_outcomes (reinforcement), this creates
        natural selection: useful wisdom stays strong, stale wisdom fades.
        """
        try:
            from memory.professor import decay_stale_confidence
            result = decay_stale_confidence()
            n_decayed = len(result.get("domains_decayed", {}))
            n_floor = len(result.get("domains_at_floor", []))
            if n_decayed:
                return True, f"decayed {n_decayed} domains, {n_floor} at floor"
            return True, "no domains stale enough to decay"
        except ImportError:
            return True, "professor module not available"
        except Exception as e:
            logger.error(f"PROFESSOR DECAY ERROR: {e}")
            return False, str(e)

    # =================================================================
    # ANATOMIC LOCATION MAPPER
    # Maps file paths to mansion rooms for organized troubleshooting
    # =================================================================

    # File path patterns → anatomic location
    ANATOMIC_MAP = {
        "nervous_system": [
            "scheduler_coordinator", "lite_scheduler", "celery_config",
            "celery_tasks", "mutual_heartbeat",
        ],
        "brain": [
            "professor", "local_llm_analyzer", "synaptic_",
            "brain.py", "query.py", "context.py",
        ],
        "circulatory": [
            "unified_injection", "persistent_hook_structure",
            "webhook_section_notifications", "hook_",
        ],
        "memory": [
            "observability_store", "knowledge_graph", "context_dna_client",
            "artifact_store", "architecture",
        ],
        "skeleton": [
            "docker", "postgres", "redis", "rabbitmq",
            "ecosystem_health", "seaweedfs",
        ],
        "immune_system": [
            "recovery_agent", "troubleshoot", "health",
            "watchdog", "failsafe",
        ],
        "eyes_ears": [
            "webhook_destination_registry", "integration_offer",
            "boundary_intelligence", "auto_capture",
        ],
        "voice": [
            "synaptic_chat", "heartbeat", "notification",
        ],
        "digestive": [
            "claim", "quarantine", "promote", "evidence",
            "outcome", "auto_learn",
        ],
    }

    def _classify_anatomic_location(self, file_path: str) -> str:
        """Map a file path to its anatomic mansion location."""
        if not file_path:
            return "general"
        path_lower = file_path.lower()
        for location, patterns in self.ANATOMIC_MAP.items():
            for pattern in patterns:
                if pattern in path_lower:
                    return location
        return "general"

    def _record_code_note(
        self,
        source_job: str,
        error_message: str,
        error_type: str = "other",
        severity: str = "info",
        file_path: str = None,
        line_number: int = None,
        traceback_str: str = None,
        llm_analysis: str = None,
        suggested_fix: str = None,
        llm_confidence: float = 0.0,
    ):
        """
        Record a code observation in the butler's notepad.

        Called by any job when it encounters a code issue.
        Deduplicates by file_path + error_message hash.
        """
        import uuid
        import hashlib

        store = self._get_store()
        if not store:
            return

        # Deduplicate: same file + same error = increment count
        dedup_key = hashlib.sha256(
            f"{file_path or ''}:{error_message[:200]}".encode()
        ).hexdigest()[:16]

        location = self._classify_anatomic_location(file_path or source_job)

        try:
            # Check if we already have this note
            cursor = store._sqlite_conn.execute("""
                SELECT note_id, occurrence_count FROM butler_code_note
                WHERE note_id = ?
            """, (dedup_key,))
            existing = cursor.fetchone()

            if existing:
                # Update occurrence count and last seen
                store._sqlite_conn.execute("""
                    UPDATE butler_code_note
                    SET occurrence_count = occurrence_count + 1,
                        last_seen_utc = datetime('now'),
                        llm_analysis = COALESCE(?, llm_analysis),
                        suggested_fix = COALESCE(?, suggested_fix)
                    WHERE note_id = ?
                """, (llm_analysis, suggested_fix, dedup_key))
            else:
                # Insert new note
                store._sqlite_conn.execute("""
                    INSERT INTO butler_code_note
                    (note_id, source_job, file_path, line_number,
                     anatomic_location, error_type, severity,
                     error_message, traceback, llm_analysis,
                     suggested_fix, llm_confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    dedup_key, source_job, file_path, line_number,
                    location, error_type, severity,
                    error_message[:2000], (traceback_str or "")[:4000],
                    (llm_analysis or "")[:2000], (suggested_fix or "")[:1000],
                    llm_confidence
                ))

            store._sqlite_conn.commit()
        except Exception as e:
            logger.debug(f"Failed to record code note: {e}")

    def _run_code_patrol(self) -> tuple[bool, str]:
        """
        Butler's Code Patrol: Scan for code issues across the mansion.

        Reads recent scheduler logs for Python errors (tracebacks, import
        failures, syntax errors), classifies by anatomic location, and
        optionally asks the local LLM for analysis. Accumulates notes
        for Atlas to review.
        """
        import re

        store = self._get_store()
        if not store:
            return True, "store unavailable"

        notes_recorded = 0

        # 1. Scan scheduler coordinator log for Python errors
        log_path = Path(__file__).parent.parent / "logs" / "scheduler_coordinator.log"
        if log_path.exists():
            try:
                # Read last 500 lines (deque avoids loading entire file into memory)
                from collections import deque
                with open(log_path, "r") as f:
                    lines = list(deque(f, maxlen=500))

                # Find Python tracebacks and errors
                error_patterns = [
                    # (regex, error_type, severity)
                    (r"(\S+\.py)[,:]\s*line\s+(\d+).*?(SyntaxError|IndentationError):?\s*(.*)",
                     "syntax_error", "critical"),
                    (r"(ImportError|ModuleNotFoundError):\s*(.*)",
                     "import_error", "warning"),
                    (r"(\S+\.py)[,:]\s*line\s+(\d+).*?(TypeError):?\s*(.*)",
                     "type_error", "warning"),
                    (r"(\S+\.py)[,:]\s*line\s+(\d+).*?(RuntimeError|Exception):?\s*(.*)",
                     "runtime_error", "info"),
                    (r"f-string:.*?\((\S+\.py),\s*line\s+(\d+)\)",
                     "syntax_error", "critical"),
                ]

                for line in lines:
                    for pattern, error_type, severity in error_patterns:
                        match = re.search(pattern, line.strip())
                        if match:
                            groups = match.groups()
                            file_path = None
                            line_num = None
                            error_msg = line.strip()

                            # Extract file path and line number
                            for g in groups:
                                if g and g.endswith(".py"):
                                    file_path = g
                                elif g and g.isdigit():
                                    line_num = int(g)

                            self._record_code_note(
                                source_job="code_patrol",
                                error_message=error_msg[-500:],
                                error_type=error_type,
                                severity=severity,
                                file_path=file_path,
                                line_number=line_num,
                            )
                            notes_recorded += 1
                            break  # One match per line

            except Exception as e:
                logger.debug(f"Log scan error: {e}")

        # 2. Check recent job failures from observability store
        try:
            cursor = store._sqlite_conn.execute("""
                SELECT job_name, last_status, last_error
                FROM job_schedule
                WHERE last_status = 'failed'
                  AND last_error IS NOT NULL
                  AND last_error != ''
            """)
            for row in cursor:
                job_name = row[0]
                error = row[2] or ""

                # Extract file path from error if present
                file_match = re.search(r"(\S+\.py)", error)
                file_path = file_match.group(1) if file_match else None

                self._record_code_note(
                    source_job=f"code_patrol:{job_name}",
                    error_message=error[:500],
                    error_type="runtime_error",
                    severity="warning",
                    file_path=file_path,
                )
                notes_recorded += 1
        except Exception as e:
            logger.debug(f"Job failure scan error: {e}")

        # 3. Query LLM for top unanalyzed notes (max 2 per patrol)
        try:
            cursor = store._sqlite_conn.execute("""
                SELECT note_id, error_message, file_path, error_type,
                       anatomic_location, traceback
                FROM butler_code_note
                WHERE llm_analysis IS NULL OR llm_analysis = ''
                ORDER BY
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'warning' THEN 1
                        ELSE 2
                    END,
                    occurrence_count DESC
                LIMIT 2
            """)
            unanalyzed = cursor.fetchall()

            if unanalyzed:
                try:
                    from memory.llm_priority_queue import butler_query

                    for row in unanalyzed:
                        note_id = row[0]
                        error_msg = row[1]
                        fpath = row[2] or "unknown"
                        etype = row[3]
                        location = row[4]

                        system_prompt = "You are a code diagnostician for Context DNA (a Python system). Analyze errors and suggest fixes. Be brief and practical."
                        user_prompt = f"""Consider whatever analysis seems relevant:
- What's happening? What do the exception type and message indicate?
- Why might this failure occur? What are likely root causes?
- How would you fix it? What's the most direct fix?

MANSION AREA: {location}
FILE: {fpath}
ERROR TYPE: {etype}
ERROR: {error_msg[:800]}

Respond in JSON: {{"analysis": "brief root cause", "fix": "specific fix suggestion", "confidence": 0.8}}"""

                        response = butler_query(system_prompt, user_prompt, profile="extract")
                        if response:
                            import json
                            import re

                            analysis_text = response[:2000]
                            fix_text = ""
                            confidence_val = 0.5

                            # Try to parse JSON first
                            try:
                                j_start = response.find("{")
                                j_end = response.rfind("}") + 1
                                if j_start >= 0 and j_end > j_start:
                                    parsed = json.loads(response[j_start:j_end])
                                    analysis_text = parsed.get("analysis", response)[:2000]
                                    fix_text = parsed.get("fix", "")[:1000]
                                    confidence_val = parsed.get("confidence", 0.5)
                            except (json.JSONDecodeError, ValueError):
                                analysis_text = response[:2000]

                                fix_patterns = [
                                    r'(?:fix|solution|try):\s*([^\n.]+)',
                                    r'(?:try|use|apply):\s*([^\n.]+)',
                                    r'(?:recommend|suggest):\s*([^\n.]+)'
                                ]
                                for pattern in fix_patterns:
                                    match = re.search(pattern, response, re.IGNORECASE)
                                    if match:
                                        fix_text = match.group(1).strip()[:1000]
                                        break

                                if len(response) > 150 and ('fix' in response.lower() or 'try' in response.lower()):
                                    confidence_val = 0.6
                                else:
                                    confidence_val = 0.4

                            store._sqlite_conn.execute("""
                                UPDATE butler_code_note
                                SET llm_analysis = ?,
                                    suggested_fix = ?,
                                    llm_confidence = ?
                                WHERE note_id = ?
                            """, (analysis_text, fix_text, confidence_val, note_id))

                    store._sqlite_conn.commit()
                except Exception as e:
                    logger.debug(f"LLM analysis failed: {e}")

        except Exception as e:
            logger.debug(f"Unanalyzed note scan error: {e}")

        # 4. Summary
        try:
            total = store._sqlite_conn.execute(
                "SELECT COUNT(*) FROM butler_code_note WHERE reviewed_by_atlas = 0"
            ).fetchone()[0]

            by_location = {}
            cursor = store._sqlite_conn.execute("""
                SELECT anatomic_location, COUNT(*)
                FROM butler_code_note
                WHERE reviewed_by_atlas = 0
                GROUP BY anatomic_location
            """)
            for row in cursor:
                by_location[row[0]] = row[1]

            location_summary = " | ".join(
                f"{loc}:{cnt}" for loc, cnt in sorted(by_location.items())
            ) if by_location else "clean"

            return True, f"{notes_recorded} new, {total} pending review [{location_summary}]"

        except Exception:
            return True, f"{notes_recorded} new notes recorded"

    def _run_hindsight_check(self) -> tuple[bool, str]:
        """
        Butler's hindsight validation via dialogue mirror.

        Verifies recorded wins by scanning dialogue mirror for subsequent
        error patterns. Emits negative outcome_events for miswirings
        (reward=-0.3) to feed the evidence pipeline's discrimination power.

        1. Checks pending wins against dialogue for related errors
        2. Flags suspects and misiwrings
        3. Uses local LLM to analyze root causes of misiwrings
        4. Creates MiswiringLearning records for Section 2 LANDMINE injection
        5. Emits negative outcome_event for each miswiring → evidence pipeline

        Aligned with EBM philosophy: pattern-based, not event-based.
        """
        try:
            from memory.hindsight_validator import HindsightValidator, VerificationStatus
            if self._cached_hindsight_validator is None:
                self._cached_hindsight_validator = HindsightValidator()
            results = self._cached_hindsight_validator.run_hindsight_check()

            verified = sum(1 for r in results if r.status == VerificationStatus.VERIFIED)
            suspects = sum(1 for r in results if r.status == VerificationStatus.SUSPECT)
            misiwrings = sum(1 for r in results if r.status == VerificationStatus.MISWIRING)

            # Negative signals are emitted by HindsightValidator._emit_negative_signal()
            # (authoritative emitter). No duplicate emission here.

            summary = f"{len(results)} checked: verified={verified} suspects={suspects} misiwrings={misiwrings}"

            return True, summary

        except ImportError:
            return True, "hindsight_validator not available"
        except Exception as e:
            return False, str(e)

    def _run_failure_pattern_analysis(self) -> tuple[bool, str]:
        """
        Butler's failure pattern analysis via unified sources.

        Scans DialogueMirror + TemporalValidator + HindsightValidator for
        high-yield failure patterns. Generates LANDMINE warnings for
        Section 2 (WISDOM) webhook injection.

        Only flags patterns with 3+ repetitions (not noise).
        """
        try:
            from memory.failure_pattern_analyzer import FailurePatternAnalyzer

            if self._cached_failure_analyzer is None:
                self._cached_failure_analyzer = FailurePatternAnalyzer()
            patterns = self._cached_failure_analyzer.analyze_for_patterns(hours_back=24)

            high_yield = [p for p in patterns if p.occurrence_count >= 3]
            domains = set(p.domain for p in high_yield if p.domain)

            summary = f"{len(patterns)} patterns found, {len(high_yield)} high-yield across {len(domains)} domains"

            if high_yield:
                logger.info(f"Failure analyzer: {len(high_yield)} high-yield patterns → LANDMINE injection")

            return True, summary

        except ImportError:
            return True, "failure_pattern_analyzer not available"
        except Exception as e:
            return False, str(e)

    def _run_skeletal_integrity(self) -> tuple[bool, str]:
        """
        Butler's skeletal integrity check — self-healing DB repair.

        Anatomical Role: Skeletal System (structural integrity of all databases)
        Checks all 11 SQLite databases for corruption.
        Self-heals via: sqlite3 .recover → PG restore → recreate empty.
        Records every repair as an evidence-tracked SOP.
        """
        try:
            from memory.butler_db_repair import ButlerDBRepair, RepairOutcome

            if self._cached_butler_repair is None:
                self._cached_butler_repair = ButlerDBRepair()
            results = self._cached_butler_repair.run_integrity_sweep()

            healthy = sum(1 for r in results.values() if r.outcome == RepairOutcome.NOT_NEEDED)
            repaired = sum(1 for r in results.values() if r.outcome == RepairOutcome.SUCCESS)
            partial = sum(1 for r in results.values() if r.outcome == RepairOutcome.PARTIAL)
            failed = sum(1 for r in results.values() if r.outcome == RepairOutcome.FAILED)

            summary = f"{healthy}/{len(results)} healthy"
            if repaired:
                summary += f", {repaired} repaired"
                logger.info(f"Skeletal repair: {repaired} database(s) self-healed")
            if partial:
                summary += f", {partial} partial (data loss)"
            if failed:
                summary += f", {failed} FAILED"
                logger.warning(f"Skeletal repair: {failed} database(s) could not be repaired!")

            return True, summary

        except ImportError:
            return True, "butler_db_repair not available"
        except Exception as e:
            return False, str(e)

    def _run_mmotw_mining(self) -> tuple[bool, str]:
        """MMOTW: Mine dialogue mirror for repair patterns Atlas performed."""
        try:
            from memory.butler_repair_miner import MMOTWMiner
            if self._cached_mmotw_miner is None:
                self._cached_mmotw_miner = MMOTWMiner()
            results = self._cached_mmotw_miner.run_mining_sweep()

            new_sops = results.get("new_sops", 0)
            updated_sops = results.get("updated_sops", 0)
            validated = results.get("validated", 0)
            sessions_mined = results.get("sessions_mined", 0)

            summary = f"MMOTW: mined={sessions_mined} new={new_sops} updated={updated_sops} validated={validated}"
            return True, summary
        except ImportError as e:
            return True, f"MMOTW miner not yet available: {e}"
        except Exception as e:
            logger.warning(f"MMOTW mining failed: {e}")
            return False, f"MMOTW error: {e}"

    def _run_post_session_meta_analysis(self) -> tuple[bool, str]:
        """
        Post-session meta-analysis (Evidence-Based Section 11).

        4-phase pipeline: Summarize → Cross-ref → Synthesize → Feed back.
        Only runs when session gap > 30min detected.
        """
        try:
            from memory.meta_analysis import PostSessionMetaAnalysis
            if self._cached_meta_analyzer is None:
                self._cached_meta_analyzer = PostSessionMetaAnalysis()

            if not self._cached_meta_analyzer.should_run():
                return True, "no session end detected"

            result = self._cached_meta_analyzer.run_analysis()
            if not result:
                return True, "no sessions to analyze"

            parts = [
                f"sessions={result.sessions_analyzed}",
                f"msgs={result.total_messages}",
                f"insights={len(result.insights)}",
                f"concerns={len(result.concerns)}",
                f"sop_candidates={len(result.sop_candidates)}",
                f"llm={'yes' if result.llm_used else 'no'}",
                f"{result.duration_ms}ms",
            ]
            return True, "meta: " + " ".join(parts)

        except ImportError:
            return True, "meta_analysis not available"
        except Exception as e:
            return False, f"meta error: {str(e)[:80]}"

    def _run_session_historian(self) -> tuple[bool, str]:
        """
        Session Historian — archives stale Claude Code sessions.

        Extracts gold, LLM-analyzes via Qwen14B, stores on disk,
        feeds insights to evidence pipeline, cleans raw files.
        Anatomy: Hippocampus (long-term memory from sessions).
        """
        try:
            from memory.session_historian import SessionHistorian
            if self._cached_session_historian is None:
                self._cached_session_historian = SessionHistorian()

            if not self._cached_session_historian.should_run():
                return True, "no stale sessions"

            result = self._cached_session_historian.run()
            parts = [
                f"extracted={result.get('extracted', 0)}",
                f"analyzed={result.get('analyzed', 0)}",
                f"insights={result.get('insights', 0)}",
                f"reclaimed={result.get('reclaimed_mb', 0):.1f}MB",
                f"{result.get('duration_ms', 0)}ms",
            ]
            return True, "historian: " + " ".join(parts)

        except ImportError:
            return True, "session_historian not available"
        except Exception as e:
            return False, f"historian error: {str(e)[:80]}"

    def _run_session_historian_fast(self) -> tuple[bool, str]:
        """
        Session Historian FAST — near real-time active session extraction.

        Only extracts from sessions with open VS Code tabs.
        No LLM analysis, no cleanup — just capture gold while it's fresh.
        Enables session crash recovery via rehydration.
        Anatomy: Hippocampus fast pathway (immediate memory capture).
        """
        try:
            from memory.session_historian import SessionHistorian
            if self._cached_session_historian is None:
                self._cached_session_historian = SessionHistorian()

            result = self._cached_session_historian.run_active_only()
            extracted = result.get('extracted', 0)
            if extracted == 0:
                return True, "no active changes"

            return True, f"fast: extracted={extracted} {result.get('duration_ms', 0)}ms"

        except ImportError:
            return True, "session_historian not available"
        except Exception as e:
            return False, f"historian_fast error: {str(e)[:80]}"

    def _run_user_sentiment(self) -> tuple[bool, str]:
        """Analyze user's recent messages for sentiment/intent (P5).

        Rule-based detection of mood (frustrated/satisfied/neutral),
        intent (debug/build/deploy/refactor), and energy level.
        Feeds into Section 8 so Synaptic adapts tone accordingly.
        """
        try:
            from memory.dialogue_mirror import get_dialogue_mirror
            mirror = get_dialogue_mirror()
            result = mirror.analyze_user_sentiment(hours_back=2)
            if result["message_count"] == 0:
                return True, "no recent messages"
            return True, (f"sentiment={result['sentiment']} intent={result['intent']} "
                         f"energy={result['energy']} msgs={result['message_count']}")
        except ImportError:
            return True, "dialogue_mirror not available"
        except Exception as e:
            return False, f"sentiment error: {str(e)[:80]}"

    def _run_strategic_analyst(self):
        """Run S10 Strategic Analyst cycle (GPT-4.1, external only).

        Pre-computes strategic big-picture analysis from 6 sources and caches
        in Redis for instant S10 webhook delivery. No GPU lock — external API only.
        """
        try:
            from memory.strategic_analyst import run_strategic_analysis_cycle
            result = run_strategic_analysis_cycle()
            if result:
                logger.info(f"🗺️ Strategic Analyst: cached S10 ({len(result)} chars)")
            else:
                logger.debug("🗺️ Strategic Analyst: skipped (budget/unavailable)")
        except ImportError:
            logger.debug("Strategic analyst not available")
        except Exception as e:
            logger.debug(f"Strategic analyst failed: {e}")

    def _run_anticipation_engine(self) -> tuple[bool, str]:
        """
        Predictive Webhook Pre-computation: Pre-generate S2+S8 during idle LLM time.

        While the AI coder works (30-120s idle LLM), generates S2+S8 for the NEXT
        webhook. Result: ~200ms webhook delivery instead of 77-100s.

        This is the scheduler fallback. Primary trigger is Redis pub/sub listener
        in anticipation_engine.py (real-time via session_file_watcher).
        """
        try:
            from memory.anticipation_engine import run_anticipation_cycle
            result = run_anticipation_cycle()

            if not result.get("ran"):
                reason = result.get("skipped_reason", "unknown")
                return True, f"skipped: {reason}"

            sections = result.get("sections_cached", [])
            elapsed = result.get("elapsed_ms", 0)
            session = result.get("session_id", "?")[:12]

            if sections:
                return True, f"pre-computed {','.join(sections)} in {elapsed}ms (session: {session}...)"
            else:
                return True, f"ran but no sections cached ({elapsed}ms)"

        except ImportError:
            return True, "anticipation_engine not available"
        except Exception as e:
            return False, f"anticipation error: {str(e)[:80]}"

    def _run_codebase_map_refresh(self) -> tuple[bool, str]:
        """Incremental rebuild of architecture graph cache for Section 4 injection."""
        try:
            from memory.codebase_map import refresh
            refresh()
            return True, "codebase map refreshed"
        except ImportError:
            return True, "codebase_map not available"
        except Exception as e:
            return False, f"codebase_map error: {str(e)[:80]}"

    def _run_markdown_scan(self) -> tuple[bool, str]:
        """Markdown Memory Layer: scan .md files and digest changed ones via LLM."""
        try:
            from memory.markdown_memory_layer import run_markdown_scan_cycle
            result = run_markdown_scan_cycle()
            digested = result.get("digested", 0)
            scanned = result.get("scanned", 0)
            changed = result.get("changed", 0)
            errors = result.get("errors", 0)
            if digested > 0:
                return True, f"markdown: digested {digested}/{changed} changed ({scanned} scanned)"
            elif changed > 0:
                return True, f"markdown: {changed} changed, {errors} errors ({scanned} scanned)"
            else:
                return True, f"markdown: no changes ({scanned} files)"
        except ImportError:
            return True, "markdown_memory_layer not available"
        except Exception as e:
            return False, f"markdown scan error: {str(e)[:80]}"

    def _run_architecture_twin_refresh(self) -> tuple[bool, str]:
        """Architecture Twin: refresh architecture.current.md + diff from live code."""
        try:
            from memory.refresh_architecture_twin import refresh
            result = refresh(generate_diff=True)
            if result.get("skipped"):
                return True, f"arch_twin: skipped ({result.get('reason', 'no_changes')})"
            changed = result.get("structure_changed", False)
            evo = result.get("evolution", {})
            parts = [f"arch_twin: refreshed (hash={result.get('content_hash', '?')[:8]})"]
            if changed:
                parts.append("STRUCTURE_CHANGED")
            if evo.get("new_nodes") or evo.get("new_edges"):
                parts.append(f"+{evo.get('new_nodes', 0)} nodes +{evo.get('new_edges', 0)} edges")
            return True, " ".join(parts)
        except ImportError:
            return True, "refresh_architecture_twin not available"
        except Exception as e:
            return False, f"arch_twin error: {str(e)[:80]}"

    def _run_twin_refresh(self) -> tuple[bool, str]:
        """Twin Refresh: rebuild module dependency graph from code_chunks.db."""
        try:
            from memory.architecture_twin import refresh_twin
            stats = refresh_twin()
            return True, f"twin_refresh: {stats['modules']} modules, {stats['edges']} edges, {stats['files']} files"
        except ImportError:
            return True, "architecture_twin not available"
        except Exception as e:
            return False, f"twin_refresh error: {str(e)[:80]}"

    def _run_semantic_embedding_index(self) -> tuple[bool, str]:
        """
        Semantic Rescue Layer: Incrementally index new learnings (P2.1).

        Builds/updates the sentence-transformer embedding index used by
        rescue_search() when FTS5 returns <3 results. Silently degrades
        if sentence-transformers is not installed.

        Performance: ~5s for 300 learnings (batch encode), incremental
        (skips already-indexed learnings).
        """
        try:
            from memory.semantic_search import build_embedding_index
            result = build_embedding_index(force_rebuild=False)
            status = result.get("status", "unknown")
            if status in ("model_unavailable", "numpy_unavailable"):
                return True, f"semantic index: {status} (graceful skip)"
            new = result.get("new", 0)
            total = result.get("total", 0)
            duration = result.get("duration_ms", 0)
            return True, f"semantic index: {new} new/{total} total ({duration}ms)"
        except ImportError:
            return True, "semantic_search not available"
        except Exception as e:
            return False, f"semantic index error: {str(e)[:80]}"

    def _run_code_chunk_rebuild(self) -> tuple[bool, str]:
        """Rebuild code chunk index for semantic code search."""
        try:
            from memory.code_chunk_indexer import index_project
            stats = index_project()
            if stats.get('chunks_indexed', 0) > 0:
                logger.info(f"📂 Code chunks: {stats['chunks_indexed']} indexed, {stats['chunks_skipped']} skipped")
            indexed = stats.get('chunks_indexed', 0)
            skipped = stats.get('chunks_skipped', 0)
            scanned = stats.get('files_scanned', 0)
            return True, f"code chunks: {indexed} indexed, {skipped} skipped ({scanned} files)"
        except ImportError:
            return True, "code chunk indexer not available"
        except Exception as e:
            return False, f"code chunk rebuild failed: {str(e)[:80]}"

    def _run_enable_wal_all_dbs(self) -> tuple[bool, str]:
        """
        Enable WAL mode on all SQLite databases (P1.4).

        Prevents corruption under concurrent access. Discovers all .db files
        in ~/.context-dna/, memory/, and repo root, enables WAL if not set.
        """
        try:
            from memory.enable_wal_all import run_enable_wal_all
            return run_enable_wal_all()
        except ImportError:
            return True, "enable_wal_all not available"
        except Exception as e:
            return False, f"wal sweep error: {str(e)[:80]}"

    def _run_wal_checkpoint(self) -> tuple[bool, str]:
        """Checkpoint .observability.db WAL to prevent unbounded growth."""
        try:
            from memory.db_utils import safe_conn
            db_path = str(Path(__file__).parent / ".observability.db")
            with safe_conn(db_path) as conn:
                result = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                # result = (busy, log, checkpointed)
                return True, f"wal_ckpt: log={result[1]} ckpt={result[2]}"
        except Exception as e:
            return True, f"wal_ckpt skip: {str(e)[:60]}"

    def _run_sop_dedup_analysis(self) -> tuple[bool, str]:
        """
        Butler's SOP deduplication sweep.

        Scans the hook evolution library (.pattern_evolution.db) for:
        - Exact duplicate patterns (same name/regex)
        - Near-duplicate patterns (>70% similarity)
        - Category imbalances (over-concentrated categories)
        - Unused patterns (zero outcomes)

        Records findings to observability for evidence pipeline.
        Does NOT auto-merge — flags for review only.
        """
        detector = None
        try:
            from memory.dedup_detector import DuplicateDetector
            detector = DuplicateDetector()

            exact = detector.find_exact_duplicates()
            similar = detector.find_similar_patterns(threshold=0.7)
            imbalances = detector.find_category_imbalances()
            unused = detector.find_unused_patterns(min_outcomes=0)

            # Record to observability if issues found
            total_issues = len(exact) + len(similar) + len(unused)
            if total_issues > 0:
                try:
                    from memory.observability_store import get_observability_store
                    obs = get_observability_store()
                    obs.record_outcome_event(
                        session_id="sop_dedup_sweep",
                        outcome_type="sop_hygiene",
                        success=total_issues == 0,
                        reward=0.0,
                        notes=f"exact={len(exact)} similar={len(similar)} unused={len(unused)}"
                    )
                except Exception:
                    pass

            parts = [f"exact={len(exact)}", f"similar={len(similar)}", f"unused={len(unused)}"]
            if imbalances:
                top = max(imbalances.items(), key=lambda x: x[1]) if imbalances else ("", 0)
                parts.append(f"top_cat={top[0]}:{top[1]}")

            return True, "dedup: " + " ".join(parts)

        except ImportError:
            return True, "dedup_detector not available"
        except Exception as e:
            return False, f"dedup error: {str(e)[:80]}"
        finally:
            if detector is not None:
                try:
                    detector.close()
                except Exception:
                    pass

    def _run_embedding_backfill(self) -> tuple[bool, str]:
        """
        Incremental vector embedding generation for semantic search.

        Generates embeddings for learnings that don't have them yet.
        Uses sentence-transformers (all-MiniLM-L6-v2, 384 dims).
        Stores in pgvector column for hybrid search capability.

        Frequency: hourly — initial backfill completes in ~7 runs (313 learnings),
        then catches new captures incrementally (5-20 new/day).
        """
        try:
            from memory.generate_embeddings import (
                get_db_connection, get_blocks_without_embeddings,
                generate_embedding_text, batch_update_embeddings,
                check_dependencies
            )

            deps_ok, _ = check_dependencies()
            if not deps_ok:
                return True, "embedding deps not installed (sentence-transformers)"

            conn = get_db_connection()
            blocks = get_blocks_without_embeddings(conn, limit=50)

            if not blocks:
                conn.close()
                return True, "all learnings have embeddings"

            # Lazy-load model to avoid startup cost
            from sentence_transformers import SentenceTransformer
            if not hasattr(self, '_cached_embedding_model'):
                self._cached_embedding_model = SentenceTransformer("all-MiniLM-L6-v2")

            model = self._cached_embedding_model
            texts = [generate_embedding_text(b) for b in blocks]
            embeddings = model.encode(texts, batch_size=50, show_progress_bar=False)

            updates = [(b["id"], emb.tolist()) for b, emb in zip(blocks, embeddings)]
            batch_update_embeddings(conn, updates)
            conn.close()

            return True, f"embedded {len(updates)} learnings"
        except ImportError:
            return True, "generate_embeddings not available"
        except Exception as e:
            return False, f"embedding error: {str(e)[:80]}"

    def _run_llm_learnings_dedup(self) -> tuple[bool, str]:
        """
        LLM-powered learnings deduplication.

        4-tier: exact hash → >85% similarity → LLM decision → keep both.
        Removes noise from learnings.db (generic "Tests passed" entries etc.)
        Uses butler_query at P4 priority — butler has 97% idle capacity.
        Looks back 24h to catch recent duplicates from capture pipeline.
        """
        try:
            from memory.llm_dedup import deduplicate_recent_learnings
            result = deduplicate_recent_learnings(hours=24)

            processed = result.get("processed", 0)
            merged = result.get("merged", 0)
            kept = result.get("kept", 0)
            error = result.get("error")

            if error:
                return False, f"dedup error: {error[:80]}"

            if processed == 0:
                return True, "no recent learnings to dedup"

            return True, f"dedup: {processed} checked, {merged} merged, {kept} kept"
        except ImportError:
            return True, "llm_dedup not available"
        except Exception as e:
            return False, f"llm_dedup error: {str(e)[:80]}"

    def _run_wisdom_refinement(self) -> tuple[bool, str]:
        """
        LLM-powered wisdom refinement.

        Finds generic learnings ("Tests passed") and refines them using
        LLM + dialogue context to extract specifics. Refine, don't block.
        """
        try:
            from memory.llm_dedup import refine_generic_learnings
            result = refine_generic_learnings(hours=24)

            error = result.get("error")
            if error:
                return False, f"refine error: {error[:80]}"

            refined = result.get("refined", 0)
            generic = result.get("generic_found", 0)
            processed = result.get("processed", 0)

            if processed == 0:
                return True, "no recent learnings"

            if generic == 0:
                return True, f"checked {processed}, none generic"

            return True, f"refine: {refined}/{generic} generic refined ({processed} total)"
        except ImportError:
            return True, "llm_dedup not available"
        except Exception as e:
            return False, f"wisdom_refine error: {str(e)[:80]}"

    def _run_sop_llm_evaluation(self) -> tuple[bool, str]:
        """
        LLM causal evaluation of top SOPs — genuine or coincidental success?

        Uses 128-token "extract" profile via observability_store.
        Demotes SOPs that LLM classifies as coincidental.
        """
        try:
            store = self._get_store()
            if not store:
                return True, "store unavailable"

            evaluations = store.llm_evaluate_sop_effectiveness(limit=5)
            if not evaluations:
                return True, "no SOPs with enough data to evaluate"

            genuine = sum(1 for e in evaluations if e["causal_verdict"] == "GENUINE")
            coincidental = sum(1 for e in evaluations if e["causal_verdict"] == "COINCIDENTAL")
            noise = sum(1 for e in evaluations if e["causal_verdict"] == "NOISE")
            merged = sum(1 for e in evaluations if (e.get("pass3_action") or "").startswith("MERGE"))
            improved = sum(1 for e in evaluations if e.get("pass2_score") and e["pass2_score"] >= 0.7)
            return True, (f"sop 3-pass: {len(evaluations)} eval, {genuine}G/{coincidental}C/{noise}N, "
                          f"{improved} quality, {merged} merged")

        except Exception as e:
            return False, f"sop eval error: {str(e)[:80]}"

    def _run_cross_session_verification(self) -> tuple[bool, str]:
        """
        Re-verify wins from prior sessions — "did the fix stick?"

        Uses 64-token "classify" profile via hindsight_validator.
        """
        try:
            from memory.hindsight_validator import HindsightValidator
            if self._cached_hindsight_validator is None:
                self._cached_hindsight_validator = HindsightValidator()

            results = self._cached_hindsight_validator.run_cross_session_verification()
            if not results:
                return True, "no prior wins to cross-verify"

            confirmed = sum(1 for r in results if r["verdict"] == "CONFIRMED")
            regressed = sum(1 for r in results if r["verdict"] == "REGRESSED")
            return True, f"cross-session: {len(results)} checked, {confirmed} confirmed, {regressed} regressed"

        except ImportError:
            return True, "hindsight_validator not available"
        except Exception as e:
            return False, f"cross-session error: {str(e)[:80]}"

    def _run_session_gold_mining(self) -> tuple[bool, str]:
        """
        16-pass session gold mining — rotates 4 passes per cycle.

        Each cycle runs the next 4 passes in sequence (round-robin).
        At 3min intervals, full rotation every ~12 minutes.
        After passes: evaluate critical findings holding tank.
        Anticipation engine defers while passes run (Redis lock).
        """
        try:
            from memory.session_gold_passes import (
                PASS_REGISTRY, run_pass, evaluate_critical_holding_tank,
                run_webhook_infrastructure_audit,
            )

            all_parts = []

            # Step 0: Infrastructure audit FIRST (catches cascading failures)
            infra = run_webhook_infrastructure_audit()
            if infra["failed"] > 0:
                all_parts.append(
                    f"INFRA: {infra['passed']}/{infra['total_checks']} ok"
                    f" [{infra['critical']} critical]"
                )

            # GATE: Skip gold mining if LLM is critically down (prevents 300+ llm_error verdicts)
            if infra.get("critical", 0) > 0:
                failures = infra.get("failures", [])
                llm_down = any("llm" in str(f).lower() for f in failures)
                if llm_down:
                    return True, f"SKIPPED: LLM down | {all_parts[0] if all_parts else 'infra failed'}"

            # Determine which 4 passes to run this cycle (round-robin)
            pass_keys = sorted(PASS_REGISTRY.keys(), key=lambda k: PASS_REGISTRY[k]["id"])
            state_key = "_gold_pass_index"
            start_idx = getattr(self, state_key, 0) % len(pass_keys)
            setattr(self, state_key, start_idx + 4)

            cycle_passes = [pass_keys[(start_idx + i) % len(pass_keys)] for i in range(4)]
            total_held = 0

            for pk in cycle_passes:
                result = run_pass(pk, limit=20)
                p = result.get("processed", 0)
                e = result.get("extracted", 0)
                h = result.get("held_for_review", 0)
                total_held += h
                pname = PASS_REGISTRY[pk]["name"]
                all_parts.append(f"{pname}: {p}→{e}" + (f" [{h} held]" if h else ""))

            # After passes: evaluate any held critical findings
            tank_result = evaluate_critical_holding_tank(limit=3)
            promoted = tank_result.get("promoted", 0)
            if promoted:
                all_parts.append(f"{promoted} VERIFIED CRITICAL")

            # Autonomous superhero detection: ask LLM if findings warrant superhero mode
            try:
                from memory.anticipation_engine import is_superhero_active
                if not is_superhero_active() and (promoted > 0 or total_held > 2):
                    # Only trigger if meaningful findings AND not already active
                    self._maybe_activate_superhero(all_parts)
            except Exception as sh_e:
                logger.debug(f"[scheduler] Superhero check skipped: {sh_e}")

            # FLC observability: log grade distribution + circuit breaker
            try:
                self._log_grade_distribution(all_parts)
            except Exception as gd_e:
                logger.debug(f"[scheduler] Grade distribution logging skipped: {gd_e}")

            summary = " | ".join(all_parts)
            return True, f"16-pass: {summary}"

        except Exception as e:
            return False, f"gold mining error: {str(e)[:80]}"

    def _log_grade_distribution(self, all_parts: list):
        """Log evidence grade distribution to Redis for FLC observability.

        Also acts as circuit breaker: if >50 learnings promoted in a single
        cycle, logs a warning (indicates possible contamination loop).
        """
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

        # Count promotions this cycle from all_parts (format: "PassName: N→M")
        total_extracted = 0
        for part in all_parts:
            if "→" in part:
                try:
                    extracted = int(part.split("→")[1].split()[0])
                    total_extracted += extracted
                except (ValueError, IndexError):
                    pass

        # Circuit breaker: flag if too many promotions in one cycle
        if total_extracted > 50:
            logger.warning(f"[FLC] Circuit breaker: {total_extracted} learnings promoted in single cycle")
            r.set("gold:circuit_breaker_tripped", f"{total_extracted}", ex=3600)

        # Log grade distribution from observability DB
        try:
            from memory.db_utils import safe_conn
            from pathlib import Path
            db_path = str(Path(__file__).parent / ".observability.db")
            with safe_conn(db_path) as conn:
                rows = conn.execute(
                    "SELECT evidence_grade, COUNT(*) FROM claim GROUP BY evidence_grade"
                ).fetchall()
                dist = {row[0]: row[1] for row in rows}
                if dist:
                    r.hset("gold:grade_distribution", mapping=dist)
                    r.expire("gold:grade_distribution", 7200)
        except Exception:
            pass  # DB may not exist yet — non-critical

    def _maybe_activate_superhero(self, gold_findings: list):
        """Autonomous superhero detection — LLM decides if findings warrant activation."""
        import threading
        def _bg():
            try:
                from memory.llm_priority_queue import butler_query
                findings_str = " | ".join(gold_findings[-5:])
                system = "/no_think\nYou decide if gold mining findings warrant superhero mode (parallel agent swarm). Answer ONLY 'yes' or 'no'."
                user = f"Recent findings: {findings_str}\nDo these suggest a complex multi-file task needing 10+ parallel agents?"
                answer = butler_query(system, user, profile="classify")
                if answer and "yes" in answer.lower()[:10]:
                    from memory.anticipation_engine import _activate_superhero_anticipation
                    result = _activate_superhero_anticipation()
                    logger.info(f"[scheduler] Autonomous superhero: {result.get('activated')}")
            except Exception as e:
                logger.debug(f"[scheduler] Superhero auto-detect failed: {e}")
        t = threading.Thread(target=_bg, daemon=True, name="superhero_auto")
        t.start()

    def _run_adaptive_sync(self) -> tuple[bool, str]:
        """
        Unified adaptive sync: 3 SQLite DBs ↔ 2 PG databases.

        Uses unified_sync.py engine with:
        - Auto-discovery of new tables/columns
        - PG advisory lock coordination (safe with agent_service)
        - Per-table conflict policies
        - PG→SQLite mirrors for offline access
        """
        try:
            from memory.unified_sync import get_sync_engine
            engine = get_sync_engine()
            report = engine.sync_all(caller="lite_scheduler")

            if not report.lock_acquired:
                return True, "lock contention — agent_service syncing"

            if report.mode_after == "lite":
                return True, "PG unavailable — sync deferred"

            parts = [f"tables:{report.tables_synced}"]
            if report.total_pushed:
                parts.append(f"push:{report.total_pushed}")
            if report.total_pulled:
                parts.append(f"pull:{report.total_pulled}")
            if not report.total_pushed and not report.total_pulled:
                parts.append("in-sync")
            if report.errors:
                parts.append(f"errs:{len(report.errors)}")
            parts.append(f"{report.duration_ms}ms")

            return True, " ".join(parts)

        except Exception as e:
            return True, f"sync error: {str(e)[:80]}"

    def _run_container_diagnostics(self) -> tuple[bool, str]:
        """
        Butler Handyman: Diagnose Docker container failures using local LLM.

        Checks all context-dna containers, grabs logs from unhealthy/stopped ones,
        sends to local LLM (mlx_lm.server on 5044) for analysis, records results
        in container_diagnostic table for learning.

        Reusable for any app troubleshooting — same table, same pattern.
        """
        import subprocess
        import json
        import uuid

        # Butler manages ALL Context DNA containers across both stacks:
        #   context-dna-*  = active stack (docker-compose.yml)
        #   contextdna-*   = full stack (infra/docker-compose.yaml)
        # acontext-server-* containers are separate and excluded.
        STACK_PREFIXES = ("context-dna-", "contextdna-")

        try:
            # 1. Get ALL container status (both stacks)
            result = subprocess.run(
                ["docker", "ps", "-a",
                 "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return True, "docker not available"

            lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
            if not lines:
                return True, "no containers found"

            # 2. Identify unhealthy/stopped containers (both stacks)
            unhealthy = []
            healthy_count = 0
            for line in lines:
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                name, status, image = parts[0], parts[1], parts[2]

                # Only manage context-dna-* and contextdna-* containers
                if not any(name.startswith(p) for p in STACK_PREFIXES):
                    continue

                status_lower = status.lower()
                is_healthy = ("up" in status_lower and
                              "unhealthy" not in status_lower)

                # Track which stack this belongs to
                stack = "active" if name.startswith("context-dna-") else "full"

                if is_healthy:
                    healthy_count += 1
                else:
                    unhealthy.append({
                        "name": name,
                        "status": status,
                        "image": image,
                        "stack": stack,
                    })

            if not unhealthy:
                return True, f"all {healthy_count} containers healthy"

            # 3. For each unhealthy container, grab logs and diagnose
            store = self._get_store()
            diagnosed = 0

            for container in unhealthy[:3]:  # Max 3 per cycle
                name = container["name"]

                # Check if we already diagnosed this recently (15min cooldown)
                if store:
                    try:
                        cursor = store._sqlite_conn.execute("""
                            SELECT diagnostic_id FROM container_diagnostic
                            WHERE container_name = ?
                              AND timestamp_utc > datetime('now', '-15 minutes')
                            LIMIT 1
                        """, (name,))
                        if cursor.fetchone():
                            continue  # Skip — recently diagnosed
                    except Exception:
                        pass  # Table may not exist yet on first run

                # Grab recent logs
                log_result = subprocess.run(
                    ["docker", "logs", "--tail", "30", name],
                    capture_output=True, text=True, timeout=10
                )
                error_logs = (log_result.stdout + log_result.stderr)[-2000:]  # Cap at 2KB

                # 4. Query local LLM for diagnosis
                llm_analysis = ""
                suggested_fix = ""
                fix_commands = []
                llm_confidence = 0.0

                try:
                    from memory.llm_priority_queue import butler_query

                    stack_label = ("active stack (context-dna/docker-compose.yml)"
                                  if container.get("stack") == "active"
                                  else "full stack (context-dna/infra/docker-compose.yaml)")
                    system_prompt = "You are a Docker infrastructure diagnostician for Context DNA. Analyze failing containers and suggest fixes."
                    user_prompt = f"""STACK: {stack_label}
CONTAINER: {name}
IMAGE: {container['image']}
STATUS: {container['status']}

RECENT LOGS:
{error_logs[:1500]}

Respond in JSON:
{{"analysis": "brief root cause", "fix_commands": ["cmd1", "cmd2"], "explanation": "why this should work", "confidence": 0.8}}"""

                    response = butler_query(system_prompt, user_prompt, profile="extract")
                    if response:
                        try:
                            json_start = response.find("{")
                            json_end = response.rfind("}") + 1
                            if json_start >= 0 and json_end > json_start:
                                parsed = json.loads(response[json_start:json_end])
                                llm_analysis = parsed.get("analysis", response)
                                suggested_fix = parsed.get("explanation", "")
                                fix_commands = parsed.get("fix_commands", [])
                                llm_confidence = parsed.get("confidence", 0.5)
                            else:
                                llm_analysis = response
                        except json.JSONDecodeError:
                            llm_analysis = response
                except Exception as e:
                    llm_analysis = f"LLM unavailable: {e}"

                # 5. Record diagnostic in SQLite
                if store:
                    try:
                        diag_id = f"diag-{uuid.uuid4().hex[:12]}"
                        store._sqlite_conn.execute("""
                            INSERT OR REPLACE INTO container_diagnostic
                            (diagnostic_id, timestamp_utc, container_name,
                             container_image, container_status, error_logs,
                             llm_analysis, suggested_fix, fix_commands_json,
                             llm_confidence)
                            VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            diag_id, name, container["image"],
                            container["status"], error_logs[:4000],
                            llm_analysis[:2000], suggested_fix[:1000],
                            json.dumps(fix_commands), llm_confidence
                        ))
                        store._sqlite_conn.commit()
                        diagnosed += 1
                        logger.info(
                            f"BUTLER DIAGNOSTIC: {name} — {llm_analysis[:100]}"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to record diagnostic for {name}: {e}")

            summary = (
                f"{diagnosed} diagnosed, {len(unhealthy)} unhealthy, "
                f"{healthy_count} healthy"
            )

            # 6. Notify if critical containers are down
            critical = {"context-dna-postgres", "context-dna-redis"}
            critical_down = [c["name"] for c in unhealthy
                           if c["name"] in critical]
            if critical_down:
                self._send_failsafe_notification(
                    title="Context DNA: Critical Container Down",
                    message=f"{', '.join(critical_down)} — butler diagnosing",
                    subtitle="Check logs/scheduler_coordinator.log"
                )

            return True, summary

        except FileNotFoundError:
            return True, "docker CLI not found"
        except subprocess.TimeoutExpired:
            return True, "docker command timed out"
        except Exception as e:
            logger.error(f"CONTAINER DIAGNOSTICS ERROR: {e}")
            return False, str(e)

    def _run_ghostscan_background(self) -> tuple[bool, str]:
        """Run lightweight probe scan against the repo.

        Uses 'cheap' cost filter — fast probes only (no LLM).
        Results stored in SQLite + Redis cache for webhook S3 injection.
        """
        try:
            from memory.ghostscan_bridge import run_background_scan
            summary = run_background_scan()
            if summary is None:
                return True, "ghostscan: engine unavailable (multi-fleet not installed)"
            return True, (
                f"ghostscan: {summary.probes_run} probes, "
                f"{summary.total_findings} findings "
                f"({summary.high_findings} high, {summary.medium_findings} med) "
                f"in {summary.duration_ms:.0f}ms"
            )
        except ImportError:
            return True, "ghostscan: bridge not available"
        except Exception as e:
            logger.error(f"GHOSTSCAN ERROR: {e}")
            return False, f"ghostscan error: {str(e)[:80]}"

    def _run_ghostscan_evidence_promotion(self) -> tuple[bool, str]:
        """Promote recurring GhostScan findings into the evidence pipeline.

        Finds probe findings that recurred 3+ times in 7 days — these are
        strong evidence candidates. Creates claims in the observability store
        so they enter the standard quarantine → trusted → wisdom pipeline.
        """
        try:
            from memory.ghostscan_bridge import get_recurring_findings
            recurring = get_recurring_findings(min_occurrences=3, days=7)

            if not recurring:
                return True, "ghostscan promotion: no recurring findings"

            store = self._get_store()
            if not store:
                return True, "ghostscan promotion: store unavailable"

            promoted = 0
            for finding in recurring:
                probe_id = finding["probe_id"]
                title = finding["title"]
                severity = finding["severity"]
                occurrences = finding["occurrences"]
                avg_confidence = finding["avg_confidence"]

                # Build a claim statement from the recurring finding
                statement = (
                    f"[GhostScan] {title} "
                    f"(probe={probe_id}, {occurrences}x in 7d, "
                    f"confidence={avg_confidence:.2f})"
                )

                # Check if we already promoted this finding recently
                # (avoid duplicate claims for the same recurring pattern)
                try:
                    existing = store._sqlite_conn.execute(
                        "SELECT claim_id FROM claim "
                        "WHERE statement LIKE ? AND status != 'rejected' "
                        "LIMIT 1",
                        (f"%[GhostScan] {title}%",)
                    ).fetchone()

                    if existing:
                        continue  # Already in pipeline
                except Exception:
                    pass  # Table may not exist yet

                # Record as evidence-graded claim — enters quarantine
                # Recurring probe findings = cohort-level evidence
                # (repeated independent observations, not single anecdote)
                try:
                    store.record_claim_with_evidence(
                        claim_text=statement,
                        evidence_grade="cohort",
                        source="ghostscan_bridge",
                        confidence=avg_confidence,
                        area=f"ghostscan.{probe_id}",
                        tags=["ghostscan", "probe", probe_id, severity],
                    )
                    promoted += 1
                    logger.info(
                        f"GHOSTSCAN→EVIDENCE: {title} ({occurrences}x, "
                        f"conf={avg_confidence:.2f})"
                    )
                except Exception as e:
                    logger.warning(f"Failed to promote ghostscan finding: {e}")

            return True, f"ghostscan promotion: {promoted} findings → evidence pipeline"

        except ImportError:
            return True, "ghostscan promotion: bridge not available"
        except Exception as e:
            logger.error(f"GHOSTSCAN PROMOTION ERROR: {e}")
            return False, f"ghostscan promotion error: {str(e)[:80]}"

    def _send_failsafe_notification(
        self,
        title: str,
        message: str,
        subtitle: str = ""
    ):
        """Send macOS notification directly (failsafe path)."""
        try:
            from memory.mutual_heartbeat import send_macos_notification
            send_macos_notification(
                title=title,
                message=message,
                subtitle=subtitle,
                sound="Sosumi"  # Alert sound for failsafe
            )
        except Exception as e:
            logger.error(f"Failsafe notification failed: {e}")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    scheduler = LiteScheduler()

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()

        if cmd == "status":
            print("=" * 60)
            print("LITE SCHEDULER STATUS")
            print("=" * 60)

            print(f"\nRegistered Jobs ({len(scheduler._jobs)}):")
            for name, job in scheduler._jobs.items():
                print(f"  {name}: every {job.interval_s}s")

            store = scheduler._get_store()
            if store:
                print("\nScheduled Jobs (from DB):")
                jobs = store.get_due_jobs()
                due_now = [j for j in jobs]
                print(f"  Due now: {len(due_now)}")

                # Show all jobs
                cursor = store._sqlite_conn.execute(
                    "SELECT * FROM job_schedule ORDER BY next_run_utc"
                )
                for row in cursor:
                    next_run = row["next_run_utc"][:19]
                    last_run = row["last_run_utc"][:19] if row["last_run_utc"] else "never"
                    status = row["last_status"] or "pending"
                    print(f"  {row['job_name']}: next={next_run} last={last_run} ({status})")

        elif cmd == "run":
            job_name = sys.argv[2] if len(sys.argv) > 2 else None
            if job_name:
                job = scheduler._jobs.get(job_name)
                if job:
                    budget_info = f" (budget: {job.budget_ms}ms)" if job.budget_ms else ""
                    print(f"Running job: {job_name}{budget_info}")
                    success, message, duration, exceeded = scheduler._execute_job(job)
                    icon = "✓" if success else "✗"
                    budget_warn = " ⚠️OVER BUDGET" if exceeded else ""
                    print(f"  Result: {icon} {message} ({duration}ms){budget_warn}")
                else:
                    print(f"Unknown job: {job_name}")
                    print(f"Available: {list(scheduler._jobs.keys())}")
            else:
                print("Usage: python lite_scheduler.py run <job_name>")

        elif cmd == "list":
            print("Available jobs:")
            for name, job in scheduler._jobs.items():
                print(f"  {name}: every {job.interval_s}s")

        else:
            print(f"Unknown command: {cmd}")
            print("Commands: status, run <job>, list")
            sys.exit(1)
    else:
        # Run the scheduler
        print("Starting lite scheduler...")
        print("Press Ctrl+C to stop")
        try:
            scheduler.run()
        except KeyboardInterrupt:
            print("\nStopping...")
            scheduler.stop()
