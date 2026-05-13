"""
session_gold_passes.py — 16-Pass Session Gold Mining Architecture
================================================================
Each pass: ONE narrow focus + exhaustive LLM instructions + specific downstream.
Critical findings → anticipation engine (Redis) + big picture tracker (strategic_planner).

TIERS:
  1 (Passes 1-4):  SOP Extraction — mining raw gold from session insights
  2 (Passes 5-8):  Quality Evaluation — measuring what we have
  3 (Passes 9-13): System Intelligence — cross-session wisdom
  4 (Passes 14-16): Operations — keeping the mansion running

Token budgets: classify=64, extract=256 (via llm_priority_queue profiles)
"""

import sqlite3
import json
from memory.db_utils import connect_wal
import logging
import uuid
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

logger = logging.getLogger("session_gold_passes")

# ============================================================
# CONSTANTS
# ============================================================

ARCHIVE_DB = Path.home() / ".context-dna" / "session_archive.db"
OBS_DB = Path(__file__).parent / ".observability.db"
_LEGACY_PLANS_DB = Path(__file__).parent / ".strategic_plans.db"


def _get_plans_db():
    from memory.db_utils import get_unified_db_path
    return get_unified_db_path(_LEGACY_PLANS_DB)


PLANS_DB = _get_plans_db()

CRITICAL_SUFFIX = (
    "\n\nCRITICAL CHECK: If this finding could cause DATA LOSS, SYSTEM CRASH, "
    "or SILENT CORRUPTION, append on its own line:\n"
    "CRITICAL: <specific issue, e.g. 'SQLite write without WAL causes corruption under concurrent access'>\n"
    "Do NOT flag performance issues, missing features, or minor quality concerns as CRITICAL.\n"
    "If nothing is critical, do NOT include a CRITICAL line at all."
)

REDIS_CRITICAL_PREFIX = "contextdna:critical:"
REDIS_CRITICAL_TTL = 86400  # 24 hours
REDIS_CRITICAL_WAL_KEY = "contextdna:critical:wal"  # Sorted set — additive, never trimmed
REDIS_CRITICAL_WAL_TTL = 604800  # 7 days (WAL retains longer than cache)
REDIS_PASS_LOCK = "contextdna:pass_runner:active"  # Lock so anticipation engine defers

# ============================================================
# WEBHOOK INFRASTRUCTURE HEALTH PROBES
# ============================================================
# Programmatic checks — no LLM needed. Any failure = instant CRITICAL.
# These detect the cascading failure chain:
#   scheduler dead → anticipation dead → Redis empty → S2/S8 placeholders
#   agent_service dead → S1 Foundation empty → SOPs garbage
#   LLM dead → all pre-compute fails → all sections degrade
#
# See: admin.contextdna.io/docs/anti-miswiring-plugin-extensions.md

WEBHOOK_INFRA_CHECKS = [
    {
        "id": "scheduler_alive",
        "name": "Scheduler Coordinator or LiteScheduler Running",
        "probe": "pgrep",
        "target": "scheduler_coordinator|lite_scheduler",
        "critical_if_down": True,
        "cascade": "ALL scheduled jobs stop: anticipation, gold mining, health checks, sync",
        "fix": "PYTHONPATH=. nohup .venv/bin/python3 memory/scheduler_coordinator.py &",
    },
    {
        "id": "llm_alive",
        "name": "Local LLM (mlx_lm.server) Running",
        "probe": "pgrep",
        "target": "mlx_lm",
        "critical_if_down": True,
        "cascade": "S2 Professor, S8 Synaptic, all gold mining passes, anticipation engine — ALL fail",
        "fix": "./scripts/start-llm.sh",
    },
    {
        "id": "agent_service_reachable",
        "name": "Agent Service (port 8080) Reachable",
        "probe": "http",
        "target": "http://127.0.0.1:8080/health",
        "critical_if_down": True,
        "cascade": "S1 Foundation SOPs empty → '[CONSOLIDATED] 0 patterns, 0 insights'",
        "fix": "cd context-dna && docker-compose up -d agent_service",
    },
    {
        "id": "contextdna_reachable",
        "name": "ContextDNA Service (port 8029) Reachable",
        "probe": "http",
        "target": "http://127.0.0.1:8029/health",
        "critical_if_down": False,  # Not directly in webhook hot path
        "cascade": "Dashboard, admin panel, query API unavailable",
        "fix": "cd context-dna && docker-compose up -d contextdna",
    },
    {
        "id": "redis_reachable",
        "name": "Redis (port 6379) Reachable",
        "probe": "redis",
        "target": "127.0.0.1:6379",
        "critical_if_down": True,
        "cascade": "Anticipation cache, pass locks, critical findings, S1 cache — ALL lost",
        "fix": "docker start redis-context-dna",
    },
    {
        "id": "anticipation_keys_exist",
        "name": "Anticipation Pre-Compute Keys in Redis",
        "probe": "redis_keys",
        "target": "contextdna:anticipation:*",
        "critical_if_down": False,  # Keys naturally expire between cycles (TTL-based) — not a real failure
        "cascade": "S2 Professor = placeholder, S8 Synaptic = placeholder — webhook quality drops to ~30%",
        "fix": "Restart scheduler — anticipation engine populates these every 45s",
    },
    {
        "id": "s1_cache_not_empty_hash",
        "name": "S1 Cache Not Serving Empty Content",
        "probe": "redis_key_value",
        "target": "contextdna:s1:",
        "bad_pattern": "d41d8cd98f00",  # MD5 of empty string
        "critical_if_down": True,
        "cascade": "S1 Foundation caching empty prompt responses — garbage SOPs injected",
        "fix": "Flush S1 keys: redis-cli DEL $(redis-cli KEYS 'contextdna:s1:*')",
    },
    {
        "id": "postgres_context_dna",
        "name": "PostgreSQL (port 5432) Reachable",
        "probe": "pg",
        "target": "127.0.0.1:5432",
        "critical_if_down": False,  # SQLite fallback exists
        "cascade": "Learnings sync breaks, agent_service can't query PG, falls to SQLite FTS5",
        "fix": "docker start postgres-context-dna",
    },
    {
        "id": "docker_running",
        "name": "Docker Engine Running",
        "probe": "command",
        "target": "docker info",
        "critical_if_down": True,
        "cascade": "Redis, PostgreSQL, agent_service — ALL containers stop. Entire backend offline.",
        "fix": "open -a Docker",
    },
]


# ============================================================
# PASS REGISTRY — 16 passes across 4 tiers
# ============================================================

PASS_REGISTRY = {
    # ──── TIER 1: SOP EXTRACTION (Passes 1-4) ────

    "sop_bugfix": {
        "id": 1, "name": "SOP: Bug Fix Mining", "tier": 1,
        "data_source": "gold_segments",
        "classify_profile": "classify", "extract_profile": "extract",
        "downstream": "store_learning", "downstream_type": "fix",
        "classify_system": (
            "You classify session conversation segments. Answer ONE word only.\n"
            "SOP_CANDIDATE = contains a specific bug with a specific fix (symptom + cause + resolution)\n"
            "SKIP = no bug fix described, just discussion/planning/status"
        ),
        "classify_template": (
            "Session conversation:\n{content}\n\n"
            "Does this contain a specific bug-fix? ONE word: SOP_CANDIDATE or SKIP"
        ),
        "extract_system": (
            "You extract bug-fix SOPs from session conversations. Output ONLY the structured format.\n"
            "Be specific — include exact error messages, file paths, commands, port numbers.\n"
            "If no concrete bug-fix, output: SKIP"
        ),
        "extract_template": (
            "Session conversation:\n{content}\n\n"
            "Extract bug-fix SOP in this EXACT format:\n"
            "TITLE: <what was fixed, under 10 words>\n"
            "SYMPTOM: <what was observed/broken, include error messages>\n"
            "ROOT_CAUSE: <why it broke, include file paths>\n"
            "FIX: <exact steps to fix, include commands>\n"
            "VERIFY: <how to confirm it's fixed>"
        ),
    },

    "sop_pattern": {
        "id": 2, "name": "SOP: Pattern/Process Mining", "tier": 1,
        "data_source": "gold_segments",
        "classify_profile": "classify", "extract_profile": "extract",
        "downstream": "store_learning", "downstream_type": "pattern",
        "classify_system": (
            "You classify session conversation segments. Answer ONE word only.\n"
            "SOP_CANDIDATE = contains a repeatable process/workflow that was executed and worked\n"
            "SKIP = no clear reusable process, just one-time actions or discussion"
        ),
        "classify_template": (
            "Session conversation:\n{content}\n\n"
            "Does this contain a repeatable process? ONE word: SOP_CANDIDATE or SKIP"
        ),
        "extract_system": (
            "You extract process/workflow SOPs from session conversations. Output ONLY the structured format.\n"
            "Focus on WHEN to use this process and the exact STEPS with commands and file paths.\n"
            "If no clear reusable process, output: SKIP"
        ),
        "extract_template": (
            "Session conversation:\n{content}\n\n"
            "Extract process SOP in this EXACT format:\n"
            "TITLE: <process name, under 10 words>\n"
            "WHEN: <trigger condition — when to use this>\n"
            "PROCESS: <exact steps, numbered, with commands>\n"
            "WHY: <what value this provides>\n"
            "VERIFY: <how to confirm it worked>"
        ),
    },

    "sop_antipattern": {
        "id": 3, "name": "SOP: Anti-Pattern Mining", "tier": 1,
        "data_source": "gold_segments",
        "classify_profile": "classify", "extract_profile": "extract",
        "downstream": "store_learning", "downstream_type": "gotcha",
        "classify_system": (
            "You classify session conversation segments. Answer ONE word only.\n"
            "SOP_CANDIDATE = contains a specific mistake, wrong approach, or gotcha that caused problems\n"
            "SKIP = no anti-pattern, or just successful work without mistakes"
        ),
        "classify_template": (
            "Session conversation:\n{content}\n\n"
            "Does this contain a specific anti-pattern/gotcha? ONE word: SOP_CANDIDATE or SKIP"
        ),
        "extract_system": (
            "You extract anti-pattern warnings from session conversations. Output ONLY the structured format.\n"
            "Be specific about what NOT to do, what happened, and what to do INSTEAD.\n"
            "If no clear anti-pattern, output: SKIP"
        ),
        "extract_template": (
            "Session conversation:\n{content}\n\n"
            "Extract anti-pattern in this EXACT format:\n"
            "TITLE: <warning title, under 10 words>\n"
            "NEVER_DO: <the specific mistake to avoid, with file paths/commands>\n"
            "BECAUSE: <exact consequences of this mistake>\n"
            "INSTEAD: <what to do instead, with specific steps>\n"
            "VERIFY: <how to check you're not making this mistake>"
        ),
    },

    "sop_architecture": {
        "id": 4, "name": "SOP: Architecture Decision Mining", "tier": 1,
        "data_source": "gold_segments",
        "classify_profile": "classify", "extract_profile": "extract",
        "downstream": "store_learning", "downstream_type": "decision",
        "classify_system": (
            "You classify session conversation segments. Answer ONE word only.\n"
            "SOP_CANDIDATE = contains a design choice where alternatives were considered\n"
            "SKIP = no design decision, just implementation or discussion"
        ),
        "classify_template": (
            "Session conversation:\n{content}\n\n"
            "Does this contain an architecture/design decision? ONE word: SOP_CANDIDATE or SKIP"
        ),
        "extract_system": (
            "You extract architecture decisions from session conversations. Output ONLY the structured format.\n"
            "Focus on WHY this choice was made over alternatives. Include file paths and specifics.\n"
            "If no clear decision with alternatives, output: SKIP"
        ),
        "extract_template": (
            "Session conversation:\n{content}\n\n"
            "Extract architecture decision in this EXACT format:\n"
            "TITLE: <decision title, under 10 words>\n"
            "DECISION: <what was chosen, with specifics>\n"
            "ALTERNATIVES: <what was considered but rejected>\n"
            "RATIONALE: <why this choice was made>\n"
            "CONSEQUENCES: <what this means for future work>"
        ),
    },

    # ──── TIER 2: QUALITY EVALUATION (Passes 5-8) ────

    "eval_sop_quality": {
        "id": 5, "name": "Eval: SOP Specificity Audit", "tier": 2,
        "data_source": "existing_sops",
        "extract_profile": "extract_deep",
        "downstream": "sop_quality_score",
        "extract_system": (
            "You audit SOP quality. Score 1-5 for actionability.\n"
            "1=vague/generic ('tests passed')\n"
            "2=somewhat specific but missing steps\n"
            "3=specific but could be more actionable\n"
            "4=specific with clear steps\n"
            "5=exact steps, file paths, commands, verification"
        ),
        "extract_template": (
            "SOP to audit:\n{content}\n\n"
            "Rate this SOP in EXACT format:\n"
            "SCORE: <1-5>\n"
            "WEAKNESS: <main problem with this SOP, be specific>\n"
            "SUGGESTION: <one concrete improvement>"
        ),
    },

    "eval_webhook_quality": {
        "id": 6, "name": "Eval: Webhook Injection Quality", "tier": 2,
        "data_source": "injection_outcomes",
        "extract_profile": "extract_deep",  # fallback if multi_pass disabled
        "downstream": "injection_quality_log",
        "infra_audit": True,  # Triggers live infrastructure probes before LLM eval
        # Single-pass kept as fallback reference
        "extract_system": "Score webhook injection quality.",
        "extract_template": "Injection: {injection_context}\nTask: {task_type}\nOutcome: {outcome}",
        # ── 4B MULTI-PASS: 4 narrow scoring calls + Python merge ──
        # Each sub-pass scores ONE dimension (0-3). The 4B excels at this
        # narrow task. Python merge computes total + identifies weakness.
        # Cost: 4 LLM calls × ~64 tokens each = ~256 tokens total
        # vs old: 1 call × 1024 tokens that produced ~0% usable output
        "multi_pass_extract": [
            {
                "name": "relevance",
                "system": (
                    "You score webhook injection relevance. Output ONLY: <number> <5 words>.\n"
                    "Do NOT explain. Do NOT think out loud. Just score.\n"
                    "0=irrelevant 1=tangential 2=mostly relevant 3=exactly needed"
                ),
                "template": "Atlas was doing: {task_type}\nInjection contained: {injection_context}\n\nRelevance (0-3):",
                "profile": "classify",
            },
            {
                "name": "completeness",
                "system": (
                    "You score injection completeness. Output ONLY: <number> <5 words>.\n"
                    "Do NOT explain. Do NOT think out loud. Just score.\n"
                    "0=missing critical info 1=major gaps 2=minor gaps 3=complete"
                ),
                "template": "Atlas was doing: {task_type}\nInjection contained: {injection_context}\nResult: {outcome}\n\nCompleteness (0-3):",
                "profile": "classify",
            },
            {
                "name": "freshness",
                "system": (
                    "You score data freshness. Output ONLY: <number> <5 words>.\n"
                    "Do NOT explain. Do NOT think out loud. Just score.\n"
                    "0=stale/outdated 1=dated 2=mostly current 3=fresh"
                ),
                "template": "Atlas was doing: {task_type}\nInjection contained: {injection_context}\n\nFreshness (0-3):",
                "profile": "classify",
            },
            {
                "name": "actionability",
                "system": (
                    "You score actionability. Output ONLY: <number> <5 words>.\n"
                    "Do NOT explain. Do NOT think out loud. Just score.\n"
                    "0=useless 1=vague hints 2=useful 3=immediately actionable"
                ),
                "template": "Atlas was doing: {task_type}\nInjection contained: {injection_context}\nTask succeeded: {success}\n\nActionability (0-3):",
                "profile": "classify",
            },
            {
                "name": "final",
                "type": "python_merge",
                "merge_fn": "_merge_webhook_quality",
            },
        ],
    },

    "eval_success": {
        "id": 7, "name": "Eval: Success Measurement", "tier": 2,
        "data_source": "gold_segments",
        "extract_profile": "extract_deep",  # fallback
        "downstream": "outcome_event", "downstream_success": True,
        # Classifier: more permissive gate — catch candidates, let pipeline filter
        "classify_system": (
            "Answer ONE word: SUCCESS or SKIP.\n"
            "SUCCESS = mentions something that WORKED, PASSED, was FIXED, or was CONFIRMED.\n"
            "SKIP = only questions, plans, status updates, or no completion described."
        ),
        "classify_template": (
            "Session conversation:\n{content}\n\n"
            "Does this describe something that worked or was completed? ONE word: SUCCESS or SKIP"
        ),
        "classify_profile": "classify",
        # Single-pass fallback
        "extract_system": "Extract success measurement from session conversation.",
        "extract_template": "Session conversation:\n{content}",
        # ── 4B MULTI-PASS: 3 narrow extractions + Python merge ──
        # Gate → What → Metric → Merge
        # Now fed ~20K char gold_segments instead of generic insight strings.
        # The 4B can find actual successes with error messages, metrics, file paths.
        "multi_pass_extract": [
            {
                "name": "what_succeeded",
                "is_gate": True,
                "system": (
                    "Output ONE sentence: what concrete thing worked or was completed.\n"
                    "Include specifics: file paths, commands, metrics, error messages.\n"
                    "Do NOT explain. Do NOT think out loud. Just state the achievement.\n"
                    "If nothing concrete, output: SKIP"
                ),
                "template": "Session conversation:\n{content}\n\nWhat worked?",
                "profile": "classify",
            },
            {
                "name": "metric_extract",
                "system": (
                    "Output ONE metric from this text. Examples: '5/5 tests pass', '30s→3s'.\n"
                    "Do NOT explain. Just the metric. If no number exists, output: NONE"
                ),
                "template": "Achievement: {what_succeeded}\nSession conversation:\n{content}\n\nMetric:",
                "profile": "classify",
            },
            {
                "name": "final",
                "type": "python_merge",
                "merge_fn": "_merge_success_measurement",
            },
        ],
    },

    "eval_failure": {
        "id": 8, "name": "Eval: Failure Measurement", "tier": 2,
        "data_source": "gold_segments",
        "extract_profile": "extract_deep",  # fallback
        "downstream": "outcome_event", "downstream_success": False,
        # Classifier: more permissive gate
        "classify_system": (
            "Answer ONE word: FAILURE or SKIP.\n"
            "FAILURE = mentions something that BROKE, CRASHED, FAILED, or caused WASTED time.\n"
            "SKIP = only questions, plans, minor hiccups quickly resolved."
        ),
        "classify_template": (
            "Session conversation:\n{content}\n\n"
            "Does this describe something that failed or broke? ONE word: FAILURE or SKIP"
        ),
        "classify_profile": "classify",
        # Single-pass fallback
        "extract_system": "Extract failure measurement from session conversation.",
        "extract_template": "Session conversation:\n{content}",
        # ── 4B MULTI-PASS: 3 narrow extractions + Python merge ──
        # Now fed ~20K char gold_segments instead of generic insight strings.
        # The 4B can find actual failures with error messages, stack traces, root causes.
        "multi_pass_extract": [
            {
                "name": "what_failed",
                "is_gate": True,
                "system": (
                    "Output ONE sentence: what concrete thing failed or broke.\n"
                    "Include specifics: error messages, file paths, port numbers.\n"
                    "Do NOT explain. Do NOT think out loud. Just state the failure.\n"
                    "If nothing actually failed, output: SKIP"
                ),
                "template": "Session conversation:\n{content}\n\nWhat failed?",
                "profile": "classify",
            },
            {
                "name": "impact_extract",
                "system": (
                    "Output the impact in under 15 words.\n"
                    "Examples: '2 hours wasted', 'data lost', 'service down 30min'.\n"
                    "Do NOT explain. Just the impact. If unknown, output: NONE"
                ),
                "template": "Failure: {what_failed}\nSession conversation:\n{content}\n\nImpact:",
                "profile": "classify",
            },
            {
                "name": "root_cause",
                "system": (
                    "Output the root cause in ONE sentence.\n"
                    "Include specific technical details: file paths, config values.\n"
                    "Do NOT explain. Just the cause. If unknown, output: UNKNOWN"
                ),
                "template": "Failure: {what_failed}\nImpact: {impact_extract}\nSession conversation:\n{content}\n\nRoot cause:",
                "profile": "classify",
            },
            {
                "name": "final",
                "type": "python_merge",
                "merge_fn": "_merge_failure_measurement",
            },
        ],
    },

    # ──── TIER 3: SYSTEM INTELLIGENCE (Passes 9-13) ────

    "intel_bigpicture": {
        "id": 9, "name": "Intel: Big Picture Tracker", "tier": 3,
        "data_source": "gold_segments",
        "extract_profile": "extract_deep",  # fallback
        "downstream": "big_picture",
        # Single-pass fallback
        "extract_system": "Track strategic goals.",
        "extract_template": "Session: {content}",
        # ── 4B MULTI-PASS: 5 narrow extractions + Python merge ──
        # Each field extracted independently. The 4B handles "what was the goal?"
        # much better than "extract GOAL, PLANNED, ACTUAL, DRIFT, RECOMMENDATION".
        # Now fed 20K char segments instead of 800-char truncated summaries.
        "multi_pass_extract": [
            {
                "name": "goal",
                "is_gate": True,
                "system": (
                    "What strategic goal was being pursued in this conversation?\n"
                    "Output ONE sentence. If no strategic content, output: SKIP"
                ),
                "template": "Session conversation:\n{content}\n\nStrategic goal:",
                "profile": "classify",
            },
            {
                "name": "planned",
                "system": (
                    "What was the intended plan in this conversation?\n"
                    "Output ONE sentence describing what was supposed to happen."
                ),
                "template": "Goal: {goal}\nSession conversation:\n{content}\n\nWhat was planned:",
                "profile": "classify",
            },
            {
                "name": "actual",
                "system": (
                    "What actually happened in this conversation?\n"
                    "Output ONE sentence describing the real outcome."
                ),
                "template": "Planned: {planned}\nSession conversation:\n{content}\n\nWhat actually happened:",
                "profile": "classify",
            },
            {
                "name": "drift",
                "system": (
                    "How much did actual deviate from planned?\n"
                    "Output ONE word: ALIGNED, MINOR, MAJOR, or CRITICAL.\n"
                    "Then add a short reason (under 10 words)."
                ),
                "template": "Planned: {planned}\nActual: {actual}\n\nDrift level:",
                "profile": "classify",
            },
            {
                "name": "final",
                "type": "python_merge",
                "merge_fn": "_merge_bigpicture",
            },
        ],
    },

    "intel_crosssession": {
        "id": 10, "name": "Intel: Cross-Session Patterns", "tier": 3,
        "data_source": "insight_clusters",
        "extract_profile": "extract_deep",
        "downstream": "meta_analysis",
        "extract_system": (
            "You find recurring patterns across multiple sessions.\n"
            "Given similar insights from different sessions, identify the pattern.\n"
            "If no clear pattern, output: SKIP"
        ),
        "extract_template": (
            "Similar insights from different sessions:\n{cluster_items}\n\n"
            "Extract cross-session pattern in EXACT format:\n"
            "PATTERN: <the recurring pattern, one line>\n"
            "FREQUENCY: <how many sessions show this>\n"
            "SIGNIFICANCE: <why this matters>\n"
            "ACTION: <what should be done about this pattern>"
        ),
    },

    "intel_feedback_loops": {
        "id": 11, "name": "Intel: Feedback Loop Wiring", "tier": 3,
        "data_source": "injection_outcomes",
        "extract_profile": "extract_deep",
        "downstream": "feedback_loop_registry",
        "extract_system": (
            "You detect disconnected cause-effect chains.\n"
            "Given an injection and its outcome, identify if the cause-effect\n"
            "relationship is tracked or if there's a gap in the feedback loop.\n"
            "If the loop is properly wired, output: WIRED"
        ),
        "extract_template": (
            "Injection: {injection_context}\n"
            "Task: {task_type}\n"
            "Outcome: {outcome}\n"
            "Success: {success}\n"
            "Reward: {reward}\n\n"
            "Analyze feedback loop in EXACT format:\n"
            "CAUSE: <what was injected/changed>\n"
            "EFFECT: <what outcome resulted>\n"
            "GAP: <what's missing in the tracking, or NONE>\n"
            "WIRING: <what needs to be connected to close the loop>"
        ),
    },

    "intel_code_artifacts": {
        "id": 12, "name": "Intel: Code Artifact Analysis", "tier": 3,
        "data_source": "code_artifacts",
        "extract_profile": "extract_deep",  # fallback
        "downstream": "code_intelligence",
        # Single-pass fallback
        "extract_system": "Analyze code artifact.",
        "extract_template": "File: {file_path}\nCode: {code}",
        # ── 4B MULTI-PASS: 4 narrow extractions + Python merge ──
        # Each dimension analyzed independently. The 4B can classify
        # SCOPE and FRAGILITY accurately when that's the ONLY question.
        "multi_pass_extract": [
            {
                "name": "change_desc",
                "is_gate": True,
                "system": (
                    "What does this code change do? Output ONE sentence.\n"
                    "If trivial (import, comment, whitespace), output: SKIP"
                ),
                "template": "File: {file_path}\nType: {artifact_type}\nCode:\n{code}\n\nWhat it does:",
                "profile": "classify",
            },
            {
                "name": "pattern",
                "system": (
                    "What coding or architecture pattern does this use?\n"
                    "Output ONE phrase (e.g., 'singleton', 'event-driven', 'strategy pattern').\n"
                    "If no clear pattern, output: ad-hoc"
                ),
                "template": "Change: {change_desc}\nFile: {file_path}\nCode:\n{code}\n\nPattern:",
                "profile": "classify",
            },
            {
                "name": "scope",
                "system": (
                    "Rate architectural significance. Output ONE word:\n"
                    "CORE = affects whole system, 10+ files depend on it\n"
                    "MODULE = affects one module/feature\n"
                    "UTILITY = helper/tool, easily replaced\n"
                    "TRIVIAL = no architectural significance"
                ),
                "template": "Change: {change_desc}\nFile: {file_path}\nPattern: {pattern}\n\nScope:",
                "profile": "classify",
            },
            {
                "name": "fragility",
                "system": (
                    "Rate fragility. Output ONE word: HIGH, MEDIUM, or LOW.\n"
                    "HIGH = likely to break with changes, tightly coupled\n"
                    "MEDIUM = some coupling, moderate risk\n"
                    "LOW = well-isolated, unlikely to break"
                ),
                "template": "Change: {change_desc}\nScope: {scope}\nCode:\n{code}\n\nFragility:",
                "profile": "classify",
            },
            {
                "name": "final",
                "type": "python_merge",
                "merge_fn": "_merge_code_artifacts",
            },
        ],
    },

    "intel_evidence_quality": {
        "id": 13, "name": "Intel: Evidence Quality Audit", "tier": 3,
        "data_source": "claims",
        "extract_profile": "extract_deep",
        "downstream": "evidence_health",
        "extract_system": (
            "You audit evidence pipeline health.\n"
            "Given a claim with its evidence grade and confidence, assess if\n"
            "grading is appropriate and confidence is calibrated.\n"
            "If properly calibrated, output: CALIBRATED"
        ),
        "extract_template": (
            "Claim: {statement}\n"
            "Evidence grade: {evidence_grade}\n"
            "Confidence: {confidence}\n"
            "Created by: {created_by}\n"
            "Area: {area}\n\n"
            "Audit in EXACT format:\n"
            "GRADE_CORRECT: <YES/NO>\n"
            "CONFIDENCE_CALIBRATED: <YES/NO>\n"
            "SHOULD_BE: <recommended grade and confidence, or KEEP>\n"
            "ISSUE: <main quality concern, or NONE>"
        ),
    },

    # ──── TIER 4: OPERATIONS (Passes 14-16) ────

    "ops_butler_perf": {
        "id": 14, "name": "Ops: Butler Performance", "tier": 4,
        "data_source": "task_run_events",
        "extract_profile": "extract",
        "downstream": "butler_scorecard",
        "extract_system": (
            "You evaluate butler task performance.\n"
            "Given a task execution, assess goal achievement and efficiency.\n"
            "If the task succeeded normally, output: NOMINAL"
        ),
        "extract_template": (
            "Task: {task_name}\n"
            "Status: {status}\n"
            "Duration: {duration_ms}ms (budget: {budget_ms}ms)\n"
            "Mode: {mode}\n"
            "Details: {details}\n\n"
            "Evaluate in EXACT format:\n"
            "GOAL_MET: <YES/NO/PARTIAL>\n"
            "EFFICIENCY: <GOOD/FAIR/POOR>\n"
            "OUTPUT_QUALITY: <assessment of what was produced>\n"
            "IMPROVEMENT: <suggestion, or NONE>"
        ),
    },

    "ops_capture_quality": {
        "id": 15, "name": "Ops: Learnings Capture Quality", "tier": 4,
        "data_source": "session_capture_pairs",
        "extract_profile": "extract",
        "downstream": "historian_quality",
        "extract_system": (
            "You evaluate session historian extraction quality.\n"
            "Compare captured insights vs session gold summary.\n"
            "Did the historian miss important things? Capture noise?\n"
            "If capture quality is good, output: GOOD_CAPTURE"
        ),
        "extract_template": (
            "Session gold (raw summary):\n{gold_text}\n\n"
            "Captured insights ({insight_count} total):\n{insights_summary}\n\n"
            "Evaluate capture quality in EXACT format:\n"
            "PRECISION: <HIGH/MEDIUM/LOW — are captured insights relevant?>\n"
            "RECALL: <HIGH/MEDIUM/LOW — was important content missed?>\n"
            "MISSED: <most important thing NOT captured, or NONE>\n"
            "NOISE: <most irrelevant thing captured, or NONE>"
        ),
    },

    "eval_anticipation_cross_exam": {
        "id": 17, "name": "Eval: Anticipation Cross-Exam", "tier": 2,
        "data_source": "anticipation_archive",
        "classify_profile": "classify",
        "extract_profile": "extract",
        "downstream": "anticipation_quality",
        "classify_system": (
            "You evaluate archived anticipation engine outputs.\n"
            "These are pre-computed webhook sections (S2 wisdom, S8 Synaptic, etc.)\n"
            "from previous sessions, archived at session boundaries.\n"
            "Classify each as: VALUABLE (reusable insight), STALE (session-specific noise),\n"
            "or PATTERN (recurring theme worth promoting to long-term memory).\n"
            "Output EXACTLY one of: VALUABLE | STALE | PATTERN"
        ),
        "classify_template": (
            "Session: {session_id}\nSection: {section}\n"
            "Generated at: {generated_at}\n\nContent:\n{content}"
        ),
        "extract_system": (
            "You cross-examine archived anticipation outputs.\n"
            "Extract durable insights that transcend the original session.\n"
            "Focus on: recurring patterns, professor wisdom worth preserving,\n"
            "Synaptic observations that apply broadly, gotchas that recur.\n"
            "If content is purely session-specific with no reusable value, output: NO_VALUE"
        ),
        "extract_template": (
            "Session: {session_id}\nSection: {section}\n"
            "Source prompt: {source_prompt}\n\nContent:\n{content}\n\n"
            "Extract durable insights in EXACT format:\n"
            "TYPE: <WISDOM/PATTERN/GOTCHA/SOP_CANDIDATE>\n"
            "INSIGHT: <the reusable insight, 1-2 sentences>\n"
            "CONFIDENCE: <HIGH/MEDIUM/LOW>\n"
            "APPLIES_TO: <what future tasks this helps with>"
        ),
    },

    "ops_constitutional": {
        "id": 16, "name": "Ops: Constitutional Compliance", "tier": 4,
        "data_source": "session_summaries",
        "extract_profile": "extract",
        "downstream": "constitutional_audit",
        "extract_system": (
            "You check constitutional physics compliance.\n"
            "Principles: 1.Preserve Determinism 2.No Discovery at Injection\n"
            "3.Respect SOP Integrity 4.Evidence Over Confidence\n"
            "5.Prefer Reversible Actions 6.Minimalism\n"
            "If session is compliant, output: COMPLIANT"
        ),
        "extract_template": (
            "Session summary:\n{gold_text}\n\n"
            "Check constitutional compliance in EXACT format:\n"
            "COMPLIANT: <YES/NO>\n"
            "PRINCIPLE: <which principle was at risk, if any>\n"
            "EVIDENCE: <what action raised the concern>\n"
            "SEVERITY: <LOW/MEDIUM/HIGH/CRITICAL>\n"
            "RECOMMENDATION: <how to prevent this>"
        ),
    },
}


# ============================================================
# DATABASE SETUP
# ============================================================

def _ensure_tables():
    """Create tracking tables in session archive DB."""
    db = ARCHIVE_DB
    if not db.parent.exists():
        db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_wal(db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pass_processing_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pass_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            item_type TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            verdict TEXT,
            extracted_content TEXT,
            critical_finding TEXT,
            downstream_action TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS critical_holding_tank (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pass_id TEXT NOT NULL,
            finding TEXT NOT NULL,
            session_id TEXT,
            item_id TEXT,
            source_content TEXT,
            found_at TEXT NOT NULL,
            evaluated INTEGER DEFAULT 0,
            is_real_critical INTEGER DEFAULT 0,
            evaluation_reason TEXT,
            evaluated_at TEXT
        )
    """)
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gold_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            segment_index INTEGER NOT NULL,
            segment_text TEXT NOT NULL,
            char_offset_start INTEGER NOT NULL,
            char_offset_end INTEGER NOT NULL,
            user_turns INTEGER DEFAULT 0,
            atlas_turns INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            gold_text_size_at_creation INTEGER NOT NULL,
            UNIQUE(session_id, segment_index)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plog_pass ON pass_processing_log(pass_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plog_item ON pass_processing_log(item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crit_ack ON critical_findings(acknowledged)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crit_pass ON critical_findings(pass_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tank_eval ON critical_holding_tank(evaluated)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gs_session ON gold_segments(session_id)")
    conn.commit()
    conn.close()


# ============================================================
# DATA FETCHERS
# ============================================================

class WebhookInfraProber:
    """
    Programmatic infrastructure health probes — no LLM needed.

    Checks every component in the webhook cascading dependency chain.
    Any CRITICAL failure is promoted directly (bypasses LLM holding tank)
    because infrastructure-down is objectively verifiable.
    """

    def probe_all(self) -> Dict[str, Dict]:
        """Run all infrastructure probes. Returns {check_id: {status, detail, ...}}."""
        results = {}
        for check in WEBHOOK_INFRA_CHECKS:
            cid = check["id"]
            try:
                ok, detail = self._run_probe(check)
                results[cid] = {
                    "ok": ok, "name": check["name"], "detail": detail,
                    "critical_if_down": check.get("critical_if_down", False),
                    "cascade": check.get("cascade", ""),
                    "fix": check.get("fix", ""),
                }
            except Exception as e:
                results[cid] = {
                    "ok": False, "name": check["name"], "detail": str(e),
                    "critical_if_down": check.get("critical_if_down", False),
                    "cascade": check.get("cascade", ""),
                    "fix": check.get("fix", ""),
                }
        return results

    def _run_probe(self, check: dict) -> tuple:
        probe_type = check["probe"]
        target = check["target"]

        if probe_type == "pgrep":
            return self._probe_pgrep(target)
        elif probe_type == "http":
            return self._probe_http(target)
        elif probe_type == "redis":
            return self._probe_redis()
        elif probe_type == "redis_keys":
            return self._probe_redis_keys(target)
        elif probe_type == "redis_key_value":
            return self._probe_redis_key_value(target, check.get("bad_pattern", ""))
        elif probe_type == "pg":
            return self._probe_pg()
        elif probe_type == "command":
            return self._probe_command(target)
        else:
            return False, f"unknown probe type: {probe_type}"

    def _probe_pgrep(self, process_name: str) -> tuple:
        import subprocess
        r = subprocess.run(["pgrep", "-f", process_name], capture_output=True, text=True)
        pids = r.stdout.strip()
        if pids:
            return True, f"PIDs: {pids}"
        return False, "process not found"

    def _probe_http(self, url: str) -> tuple:
        import urllib.request
        try:
            req = urllib.request.urlopen(url, timeout=3)
            return True, f"HTTP {req.status}"
        except Exception as e:
            return False, str(e)[:100]

    def _probe_redis(self) -> tuple:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        r.ping()
        return True, "PONG"

    def _probe_redis_keys(self, pattern: str) -> tuple:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        keys = r.keys(pattern)
        if keys:
            ttls = [r.ttl(k) for k in keys[:5]]
            return True, f"{len(keys)} keys, TTLs: {ttls}"
        return False, f"0 keys matching {pattern}"

    def _probe_redis_key_value(self, prefix: str, bad_pattern: str) -> tuple:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        keys = r.keys(f"{prefix}*")
        if not keys:
            return True, "no S1 cache keys (cold start, not a failure)"
        for k in keys:
            if bad_pattern and bad_pattern in k:
                return False, f"key {k} contains empty-hash pattern {bad_pattern}"
        return True, f"{len(keys)} keys, no bad patterns"

    def _probe_pg(self) -> tuple:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        try:
            s.connect(("127.0.0.1", 5432))
            s.close()
            return True, "port open"
        except Exception as e:
            s.close()
            return False, str(e)[:100]

    def _probe_command(self, cmd: str) -> tuple:
        import subprocess
        r = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return True, "OK"
        return False, r.stderr[:100] or f"exit code {r.returncode}"

    def get_failures(self, results: Dict) -> List[Dict]:
        """Extract failures from probe results."""
        return [
            {**v, "check_id": k}
            for k, v in results.items()
            if not v["ok"]
        ]

    def get_critical_failures(self, results: Dict) -> List[Dict]:
        """Extract critical failures (infrastructure that causes cascading degradation)."""
        return [
            {**v, "check_id": k}
            for k, v in results.items()
            if not v["ok"] and v.get("critical_if_down")
        ]

    def format_status_line(self, results: Dict) -> str:
        """Format for injection into LLM prompts."""
        parts = []
        for cid, r in results.items():
            status = "UP" if r["ok"] else "DOWN"
            parts.append(f"{r['name']}: {status}")
            if not r["ok"]:
                parts.append(f"  CASCADE: {r['cascade']}")
                parts.append(f"  FIX: {r['fix']}")
        return "\n".join(parts)


class DataFetcher:
    """Fetch data for each pass from appropriate sources."""

    def __init__(self):
        self._infra_prober = WebhookInfraProber()
        self._last_infra_results = None

    def fetch(self, pass_key: str, pass_def: dict, limit: int) -> List[Dict]:
        source = pass_def["data_source"]

        # For passes with infra_audit=True, run infrastructure probes
        # and inject status into each item's template vars
        if pass_def.get("infra_audit"):
            self._last_infra_results = self._infra_prober.probe_all()

        method = getattr(self, f"_fetch_{source}", None)
        if not method:
            logger.warning(f"No fetcher for data_source={source}")
            return []
        try:
            items = method(pass_key, limit)
            # Inject infrastructure status into items for webhook quality pass
            if pass_def.get("infra_audit") and self._last_infra_results:
                status_line = self._infra_prober.format_status_line(self._last_infra_results)
                for item in items:
                    item["infra_status"] = status_line
            return items
        except Exception as e:
            logger.error(f"Fetch error ({source}): {e}")
            return []

    def _get_processed_ids(self, pass_key: str) -> set:
        """Get item IDs already processed by this pass."""
        try:
            conn = connect_wal(ARCHIVE_DB)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT item_id FROM pass_processing_log WHERE pass_id = ?",
                (pass_key,)
            ).fetchall()
            conn.close()
            return {r["item_id"] for r in rows}
        except Exception:
            return set()

    def _fetch_session_insights(self, pass_key: str, limit: int) -> List[Dict]:
        if not ARCHIVE_DB.exists():
            return []
        processed = self._get_processed_ids(pass_key)
        conn = connect_wal(ARCHIVE_DB)
        conn.row_factory = sqlite3.Row
        # Fetch ALL eligible — no artificial SQL cap. Post-filter skips processed.
        rows = conn.execute("""
            SELECT id, session_id, insight_type, content, confidence
            FROM session_insights
            WHERE confidence >= 0.4 AND length(content) > 30
            ORDER BY confidence DESC
        """).fetchall()
        conn.close()
        result = []
        for r in rows:
            if str(r["id"]) in processed:
                continue
            result.append(dict(r))
            if len(result) >= limit:
                break
        return result

    def _fetch_existing_sops(self, pass_key: str, limit: int) -> List[Dict]:
        try:
            from memory.sqlite_storage import get_sqlite_storage
            storage = get_sqlite_storage()
            # Direct SQL fetch — query("") returns nothing (no search terms)
            # Fetch ALL SOPs — no SQL cap. Post-filter handles dedup.
            rows = storage.conn.execute(
                "SELECT * FROM learnings ORDER BY created_at DESC"
            ).fetchall()
            processed = self._get_processed_ids(pass_key)
            result = []
            for r in rows:
                lid = str(r["id"] if "id" in r.keys() else r["title"][:30])
                if lid not in processed:
                    result.append({
                        "id": lid,
                        "content": r["content"] or "",
                        "type": r["type"] if "type" in r.keys() else "",
                    })
                    if len(result) >= limit:
                        break
            return result
        except Exception as e:
            logger.error(f"Fetch existing_sops: {e}")
            return []

    def _fetch_injection_outcomes(self, pass_key: str, limit: int) -> List[Dict]:
        if not OBS_DB.exists():
            return []
        processed = self._get_processed_ids(pass_key)
        conn = connect_wal(OBS_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT i.injection_id, i.task_type, i.total_latency_ms,
                   i.session_id, i.timestamp_utc,
                   o.outcome_type, o.success, o.reward, o.notes
            FROM injection_event i
            LEFT JOIN outcome_event o ON o.injection_id = i.injection_id
            ORDER BY i.timestamp_utc DESC
        """).fetchall()
        conn.close()
        result = []
        for r in rows:
            rid = r["injection_id"]
            if rid in processed:
                continue
            result.append({
                "id": rid,
                "injection_context": f"latency={r['total_latency_ms']}ms",
                "task_type": r["task_type"],
                "outcome": r["outcome_type"] or "unknown",
                "success": str(r["success"]) if r["success"] is not None else "unknown",
                "reward": str(r["reward"]) if r["reward"] is not None else "unknown",
                "session_id": r["session_id"],
            })
            if len(result) >= limit:
                break
        return result

    def _fetch_session_summaries(self, pass_key: str, limit: int) -> List[Dict]:
        if not ARCHIVE_DB.exists():
            return []
        processed = self._get_processed_ids(pass_key)
        conn = connect_wal(ARCHIVE_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT session_id, session_date, gold_text, key_topics
            FROM archived_sessions
            WHERE gold_text IS NOT NULL AND length(gold_text) > 50
            ORDER BY session_date DESC
        """).fetchall()
        conn.close()
        result = []
        for r in rows:
            if r["session_id"] in processed:
                continue
            result.append({
                "id": r["session_id"],
                "session_id": r["session_id"],
                "gold_text": (r["gold_text"] or "")[:2000],
                "topics": r["key_topics"] or "",
                "date": r["session_date"] or "",
            })
            if len(result) >= limit:
                break
        return result

    def _fetch_insight_clusters(self, pass_key: str, limit: int) -> List[Dict]:
        if not ARCHIVE_DB.exists():
            return []
        conn = connect_wal(ARCHIVE_DB)
        conn.row_factory = sqlite3.Row
        # Fetch ALL for proper clustering
        rows = conn.execute("""
            SELECT session_id, insight_type, content, confidence
            FROM session_insights WHERE length(content) > 40
            ORDER BY confidence DESC
        """).fetchall()
        conn.close()
        # Group by content prefix (simple clustering)
        clusters = {}
        for r in rows:
            key = r["content"][:40].lower().strip()
            clusters.setdefault(key, []).append(dict(r))
        processed = self._get_processed_ids(pass_key)
        result = []
        for key, items in sorted(clusters.items(), key=lambda x: -len(x[1])):
            if len(items) < 2:
                continue
            cid = f"cluster:{key[:20]}"
            if cid in processed:
                continue
            cluster_text = "\n".join(
                f"- [{i['session_id'][:8]}] {i['content'][:150]}"
                for i in items[:5]
            )
            result.append({
                "id": cid,
                "cluster_items": cluster_text,
                "session_count": len(set(i["session_id"][:8] for i in items)),
                "item_count": len(items),
            })
            if len(result) >= limit:
                break
        return result

    def _fetch_gold_segments(self, pass_key: str, limit: int) -> List[Dict]:
        """Fetch conversation-boundary segments (~20K chars each) for rich pass input.
        Recency-first: newest segments processed first for immediate anticipation value.
        IDs namespaced as 'gs:{id}' to avoid collision with session_insights IDs."""
        if not ARCHIVE_DB.exists():
            return []
        processed = self._get_processed_ids(pass_key)
        conn = connect_wal(ARCHIVE_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, session_id, segment_index, segment_text,
                   user_turns, atlas_turns
            FROM gold_segments
            WHERE length(segment_text) > 100
            ORDER BY created_at DESC
        """).fetchall()
        conn.close()
        result = []
        for r in rows:
            namespaced_id = f"gs:{r['id']}"
            if namespaced_id in processed:
                continue
            result.append({
                "id": namespaced_id,
                "session_id": r["session_id"],
                "segment_index": r["segment_index"],
                "content": r["segment_text"],
                "user_turns": r["user_turns"],
                "atlas_turns": r["atlas_turns"],
            })
            if len(result) >= limit:
                break
        return result

    def _fetch_code_artifacts(self, pass_key: str, limit: int) -> List[Dict]:
        if not ARCHIVE_DB.exists():
            return []
        processed = self._get_processed_ids(pass_key)
        conn = connect_wal(ARCHIVE_DB)
        conn.row_factory = sqlite3.Row
        # Fetch ALL artifacts — no SQL cap, no size ceiling.
        # Large artifacts (>2000 chars) have the most architectural value.
        # Code is truncated to 500 chars at output anyway.
        rows = conn.execute("""
            SELECT id, session_id, file_path, language, code, artifact_type
            FROM code_artifacts
            WHERE length(code) > 20
            ORDER BY size_bytes DESC
        """).fetchall()
        conn.close()
        result = []
        for r in rows:
            if str(r["id"]) in processed:
                continue
            result.append({
                "id": str(r["id"]),
                "session_id": r["session_id"],
                "file_path": r["file_path"] or "unknown",
                "language": r["language"] or "unknown",
                "code": (r["code"] or "")[:1500],
                "artifact_type": r["artifact_type"] or "written",
            })
            if len(result) >= limit:
                break
        return result

    def _fetch_claims(self, pass_key: str, limit: int) -> List[Dict]:
        if not OBS_DB.exists():
            return []
        processed = self._get_processed_ids(pass_key)
        conn = connect_wal(OBS_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT claim_id, statement, evidence_grade, confidence, created_by, area
            FROM claim WHERE status IN ('active', 'applied_to_wisdom', 'quarantine')
            ORDER BY created_at_utc DESC
        """).fetchall()
        conn.close()
        result = []
        for r in rows:
            if r["claim_id"] in processed:
                continue
            result.append({
                "id": r["claim_id"],
                "statement": r["statement"][:300],
                "evidence_grade": r["evidence_grade"],
                "confidence": r["confidence"],
                "created_by": r["created_by"],
                "area": r["area"] or "general",
            })
            if len(result) >= limit:
                break
        return result

    def _fetch_task_run_events(self, pass_key: str, limit: int) -> List[Dict]:
        if not OBS_DB.exists():
            return []
        processed = self._get_processed_ids(pass_key)
        conn = connect_wal(OBS_DB)
        conn.row_factory = sqlite3.Row
        # Sample strategy: prioritize failures/slow tasks (high signal),
        # then random sample from the rest. Avoids exhaustive 24K+ scan.
        rows = conn.execute("""
            SELECT task_run_id, task_name, status, duration_ms, budget_ms, mode, details_json
            FROM task_run_event
            WHERE status != 'success' OR duration_ms > 5000
            ORDER BY timestamp_utc DESC LIMIT ?
        """, (limit * 3,)).fetchall()
        # If not enough anomalies, add recent normal events
        if len(rows) < limit:
            extra = conn.execute("""
                SELECT task_run_id, task_name, status, duration_ms, budget_ms, mode, details_json
                FROM task_run_event ORDER BY timestamp_utc DESC LIMIT ?
            """, (limit * 3,)).fetchall()
            seen = {r["task_run_id"] for r in rows}
            rows.extend(r for r in extra if r["task_run_id"] not in seen)
        conn.close()
        result = []
        for r in rows:
            if r["task_run_id"] in processed:
                continue
            result.append({
                "id": r["task_run_id"],
                "task_name": r["task_name"],
                "status": r["status"],
                "duration_ms": r["duration_ms"],
                "budget_ms": r["budget_ms"] or 0,
                "mode": r["mode"],
                "details": (r["details_json"] or "{}")[:300],
            })
            if len(result) >= limit:
                break
        return result

    def _fetch_session_capture_pairs(self, pass_key: str, limit: int) -> List[Dict]:
        if not ARCHIVE_DB.exists():
            return []
        processed = self._get_processed_ids(pass_key)
        conn = connect_wal(ARCHIVE_DB)
        conn.row_factory = sqlite3.Row
        sessions = conn.execute("""
            SELECT session_id, gold_text FROM archived_sessions
            WHERE gold_text IS NOT NULL AND length(gold_text) > 100
            ORDER BY session_date DESC
        """).fetchall()
        result = []
        for s in sessions:
            sid = s["session_id"]
            if sid in processed:
                continue
            insights = conn.execute("""
                SELECT content, insight_type FROM session_insights
                WHERE session_id = ? LIMIT 10
            """, (sid,)).fetchall()
            summary = "\n".join(
                f"- [{i['insight_type']}] {i['content'][:100]}" for i in insights
            )
            result.append({
                "id": sid, "session_id": sid,
                "gold_text": (s["gold_text"] or "")[:600],
                "insight_count": len(insights),
                "insights_summary": summary[:400],
            })
            if len(result) >= limit:
                break
        conn.close()
        return result

    def _fetch_anticipation_archive(self, pass_key: str, limit: int) -> List[Dict]:
        """Fetch unexamined anticipation cache entries archived at session boundaries."""
        archive_db = Path(__file__).parent / ".anticipation_archive.db"
        if not archive_db.exists():
            return []
        processed = self._get_processed_ids(pass_key)
        try:
            conn = connect_wal(archive_db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, session_id, section, content, source_prompt,
                       generated_at, archived_at
                FROM anticipation_archive
                WHERE cross_examined = 0 AND length(content) > 50
                ORDER BY archived_at DESC
            """).fetchall()
            conn.close()
        except Exception:
            return []
        result = []
        for r in rows:
            item_id = str(r["id"])
            if item_id in processed:
                continue
            result.append({
                "id": item_id,
                "session_id": r["session_id"],
                "section": r["section"],
                "content": (r["content"] or "")[:1500],
                "source_prompt": (r["source_prompt"] or "")[:500],
                "generated_at": r["generated_at"] or "",
            })
            if len(result) >= limit:
                break
        return result


# ============================================================
# CRITICAL FINDINGS HANDLER
# ============================================================

class CriticalFindingHandler:
    """
    Two-stage critical findings: holding tank → LLM evaluation → promotion.

    Stage 1: All "CRITICAL:" findings go to holding_tank (no promotion yet).
    Stage 2: Separate evaluate_holding_tank() call uses LLM to verify.
    Only confirmed criticals get promoted to Redis + big picture + anticipation.
    """

    def hold(self, pass_key: str, pass_def: dict, item: dict, finding: str):
        """Stage 1: Put finding in holding tank for later evaluation."""
        logger.info(f"HOLDING TANK [{pass_def['name']}]: {finding}")
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn = connect_wal(ARCHIVE_DB)
            conn.execute("""
                INSERT INTO critical_holding_tank
                (pass_id, finding, session_id, item_id, source_content, found_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                pass_key, finding,
                str(item.get("session_id", "unknown")),
                str(item.get("id", "unknown")),
                str(item.get("content", item.get("gold_text", "")))[:300],
                now,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Holding tank error: {e}")

    def evaluate_holding_tank(self, limit: int = 5) -> Dict[str, int]:
        """
        Stage 2: Evaluate held findings via LLM. Promote real criticals.

        Returns: {"evaluated": N, "promoted": N, "rejected": N}
        """
        result = {"evaluated": 0, "promoted": 0, "rejected": 0}
        try:
            conn = connect_wal(ARCHIVE_DB)
            conn.row_factory = sqlite3.Row
            held = conn.execute("""
                SELECT id, pass_id, finding, source_content, session_id, item_id, found_at
                FROM critical_holding_tank
                WHERE evaluated = 0
                ORDER BY found_at ASC LIMIT ?
            """, (limit,)).fetchall()

            if not held:
                conn.close()
                return result

            from memory.llm_priority_queue import butler_query

            for h in held:
                result["evaluated"] += 1
                is_real = self._llm_evaluate(butler_query, h)

                now = datetime.now(timezone.utc).isoformat()
                if is_real:
                    result["promoted"] += 1
                    self._promote(h, now)
                    conn.execute("""
                        UPDATE critical_holding_tank
                        SET evaluated = 1, is_real_critical = 1,
                            evaluation_reason = 'LLM confirmed', evaluated_at = ?
                        WHERE id = ?
                    """, (now, h["id"]))
                else:
                    result["rejected"] += 1
                    conn.execute("""
                        UPDATE critical_holding_tank
                        SET evaluated = 1, is_real_critical = 0,
                            evaluation_reason = 'LLM rejected', evaluated_at = ?
                        WHERE id = ?
                    """, (now, h["id"]))
                conn.commit()

            conn.close()
        except Exception as e:
            logger.error(f"Holding tank evaluation error: {e}")
        return result

    def _llm_evaluate(self, butler_query, held) -> bool:
        """Ask LLM: is this really critical (infra) or architecturally significant?"""
        is_arch = held["pass_id"].startswith("arch_")

        if is_arch:
            system = (
                "You evaluate whether a code artifact is ARCHITECTURALLY SIGNIFICANT.\n"
                "SIGNIFICANT means: core pattern that other code depends on, structural\n"
                "foundation that if wrong causes cascading issues, or a repeated pattern\n"
                "that should be standardized before more code builds on it.\n"
                "NOT significant: cosmetic, single-use, trivial helper, style preference.\n"
                "Answer ONE word: YES or NO."
            )
            user = (
                f"Finding: {held['finding']}\n"
                f"Source: {held['source_content'][:200]}\n\n"
                "Is this architecturally significant (structural foundation / cascading pattern)? "
                "ONE word: YES or NO"
            )
        else:
            system = (
                "You evaluate whether a finding is truly CRITICAL.\n"
                "CRITICAL means: data loss, system crash, silent corruption, security breach.\n"
                "NOT critical: performance issues, missing features, quality concerns, suggestions.\n"
                "Answer ONE word: YES or NO."
            )
            user = (
                f"Pass: {held['pass_id']}\n"
                f"Finding: {held['finding']}\n"
                f"Source: {held['source_content'][:200]}\n\n"
                "Is this truly CRITICAL (data loss / crash / corruption)? ONE word: YES or NO"
            )

        response = butler_query(system, user, profile="classify")
        if response and "YES" in response.strip().upper():
            return True
        return False

    def _promote(self, held, now: str):
        """Promote confirmed critical/architectural finding to real systems."""
        pass_key = held["pass_id"]
        finding = held["finding"]
        is_arch = pass_key.startswith("arch_")
        severity = "architectural" if is_arch else "critical"
        pass_def = PASS_REGISTRY.get(pass_key, {"name": pass_key, "id": "?"})

        # 1. SQLite critical_findings (with dedup — skip if same pattern exists within 48h)
        try:
            conn = connect_wal(ARCHIVE_DB)
            existing = conn.execute("""
                SELECT id FROM critical_findings
                WHERE pass_id = ? AND substr(finding, 1, 100) = substr(?, 1, 100)
                AND found_at > datetime(?, '-48 hours')
                LIMIT 1
            """, (pass_key, finding, now)).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO critical_findings
                    (pass_id, finding, severity, session_id, item_id, found_at,
                     promoted_from_tank, wired_to_anticipation, wired_to_bigpicture)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1)
                """, (pass_key, finding, severity, held["session_id"], held["item_id"], now, held["id"]))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Critical promote SQLite error: {e}")

        # 2. Redis (fast access) — with dedup to prevent same finding accumulating
        try:
            import redis
            r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
            list_key = f"{REDIS_CRITICAL_PREFIX}recent"
            # Check for existing entry with same pass+finding combo
            existing = r.lrange(list_key, 0, 49)
            already_exists = False
            for item in existing:
                try:
                    entry = json.loads(item)
                    if entry.get("pass") == pass_key and entry.get("finding", "")[:100] == finding[:100]:
                        already_exists = True
                        break
                except Exception:
                    continue
            if not already_exists:
                _crit_payload = {
                    "pass": pass_key, "finding": finding, "found_at": now,
                    "verified": True, "severity": severity,
                }
                r.lpush(list_key, json.dumps(_crit_payload))
                r.ltrim(list_key, 0, 49)
                _wal_append_critical(_crit_payload)
            r.expire(list_key, REDIS_CRITICAL_TTL)
        except Exception as e:
            logger.error(f"Critical promote Redis error: {e}")

        # 3. Big picture
        try:
            if not PLANS_DB.exists():
                return
            conn = connect_wal(PLANS_DB)
            plan_id = f"{severity}_{pass_key}_{now[:10]}"
            pname = pass_def["name"] if isinstance(pass_def, dict) else pass_key
            tag = "ARCH-STRUCTURAL" if is_arch else "CRITICAL-VERIFIED"
            category = "architectural_debt" if is_arch else "critical_finding"
            priority = "high" if is_arch else "critical"
            conn.execute("""
                INSERT OR REPLACE INTO major_plans
                (plan_id, title, category, description, status, priority,
                 mentioned_count, last_mentioned, extracted_by, extracted_at,
                 confidence, project)
                VALUES (?, ?, ?, ?, 'needs_review', ?,
                        1, ?, 'session_gold_pass', ?, 0.9, 'context-dna')
            """, (
                plan_id,
                f"[{tag}] {pname}: {finding[:80]}",
                category,
                f"Pass {pass_def.get('id','?')} ({pname}) verified {severity}: {finding}",
                priority, now, now,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Critical promote big picture error: {e}")


# ============================================================
# PASS RUNNER ENGINE
# ============================================================

class SessionGoldPassRunner:
    """Execute session gold mining passes against session archive data.

    Uses Redis lock (REDIS_PASS_LOCK) so anticipation engine knows to defer
    LLM calls while passes are running. Critical findings go to holding tank
    first, then evaluate_holding_tank() verifies before promotion.
    """

    def __init__(self):
        _ensure_tables()
        self.fetcher = DataFetcher()
        self.critical_handler = CriticalFindingHandler()

    def _acquire_lock(self):
        """Set Redis lock so anticipation engine defers."""
        try:
            import redis
            r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
            r.setex(REDIS_PASS_LOCK, 600, "running")  # 10 min max
        except Exception:
            pass

    def _release_lock(self):
        """Release Redis lock."""
        try:
            import redis
            r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
            r.delete(REDIS_PASS_LOCK)
        except Exception:
            pass

    def run_pass(self, pass_key: str, limit: int = 10) -> Dict[str, Any]:
        """Run a single pass by key name."""
        if pass_key not in PASS_REGISTRY:
            return {"error": f"Unknown pass: {pass_key}"}

        pass_def = PASS_REGISTRY[pass_key]
        data = self.fetcher.fetch(pass_key, pass_def, limit)

        results = {
            "pass": pass_key, "name": pass_def["name"], "tier": pass_def["tier"],
            "processed": 0, "extracted": 0, "skipped": 0,
            "held_for_review": 0, "errors": 0,
        }

        if not data:
            results["note"] = "no data available"
            return results

        self._acquire_lock()
        try:
            from memory.llm_priority_queue import butler_query

            for item in data:
                try:
                    result = self._process_item(pass_key, pass_def, item, butler_query)
                    results["processed"] += 1

                    if result.get("extracted"):
                        results["extracted"] += 1
                        self._route_downstream(pass_key, pass_def, item, result)
                    else:
                        results["skipped"] += 1

                    if result.get("critical"):
                        results["held_for_review"] += 1
                        self.critical_handler.hold(
                            pass_key, pass_def, item, result["critical"]
                        )

                    self._log_processing(pass_key, item, result)

                except Exception as e:
                    results["errors"] += 1
                    logger.error(f"Pass {pass_key} error: {e}")
        finally:
            self._release_lock()

        return results

    def run_webhook_infrastructure_audit(self) -> Dict[str, Any]:
        """
        Programmatic infrastructure health audit — no LLM needed.

        Checks every component in the webhook cascading dependency chain.
        Critical failures are promoted DIRECTLY to critical_findings
        (bypasses holding tank — infrastructure-down is objectively verifiable).

        Returns: {"total_checks": N, "passed": N, "failed": N, "critical": N,
                  "failures": [...], "promoted": N}
        """
        prober = WebhookInfraProber()
        results = prober.probe_all()
        failures = prober.get_failures(results)
        criticals = prober.get_critical_failures(results)

        audit = {
            "total_checks": len(results),
            "passed": sum(1 for r in results.values() if r["ok"]),
            "failed": len(failures),
            "critical": len(criticals),
            "failures": failures,
            "promoted": 0,
        }

        # Auto-CLEAR: When probes pass, acknowledge their stale critical findings
        now = datetime.now(timezone.utc).isoformat()
        passed_checks = [cid for cid, r in results.items() if r["ok"]]
        for cid in passed_checks:
            pass_id = f"infra_{cid}"
            try:
                conn = connect_wal(ARCHIVE_DB)
                conn.execute("""
                    UPDATE critical_findings SET acknowledged = 1, acknowledged_at = ?,
                    action_taken = 'auto-cleared: probe passed'
                    WHERE pass_id = ? AND acknowledged = 0
                """, (now, pass_id))
                conn.commit()
                conn.close()
            except Exception:
                pass

        # Auto-CLEAR: Age-based expiry for ALL findings >48h old
        # Prevents stale non-infra findings (eval_sop_quality, arch_code_artifact)
        # from poisoning the LLM prompt forever
        try:
            conn = connect_wal(ARCHIVE_DB)
            conn.execute("""
                UPDATE critical_findings SET acknowledged = 1, acknowledged_at = ?,
                action_taken = 'auto-cleared: aged out (>48h)'
                WHERE acknowledged = 0
                AND found_at < datetime(?, '-48 hours')
            """, (now, now))
            conn.commit()
            conn.close()
        except Exception:
            pass

        # Rebuild Redis from SQLite truth (handles both infra and non-infra clears)
        _rebuild_redis_from_sqlite()

        # Auto-promote critical infrastructure failures (no LLM evaluation needed)
        for crit in criticals:
            finding = f"INFRA DOWN: {crit['name']} — CASCADE: {crit['cascade']} — FIX: {crit['fix']}"
            logger.warning(f"INFRA CRITICAL: {finding}")

            # Promote directly to critical_findings (skip holding tank)
            try:
                conn = connect_wal(ARCHIVE_DB)
                # Check if same infra check already has an unacknowledged finding
                existing = conn.execute("""
                    SELECT id FROM critical_findings
                    WHERE pass_id = ? AND acknowledged = 0
                    ORDER BY found_at DESC LIMIT 1
                """, (f"infra_{crit['check_id']}",)).fetchone()

                if not existing:
                    conn.execute("""
                        INSERT INTO critical_findings
                        (pass_id, finding, severity, session_id, item_id, found_at,
                         promoted_from_tank, wired_to_anticipation, wired_to_bigpicture)
                        VALUES (?, ?, 'critical', 'infrastructure', ?, ?, 0, 1, 1)
                    """, (f"infra_{crit['check_id']}", finding, crit["check_id"], now))
                    conn.commit()
                    audit["promoted"] += 1

                    # Push to Redis list (fast access) + WAL (additive, durable)
                    _crit_entry = {
                        "pass": f"infra_{crit['check_id']}",
                        "finding": finding,
                        "found_at": now,
                        "verified": True,
                        "source": "infrastructure",
                    }
                    try:
                        import redis
                        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
                        r.lpush(f"{REDIS_CRITICAL_PREFIX}recent", json.dumps(_crit_entry))
                        r.ltrim(f"{REDIS_CRITICAL_PREFIX}recent", 0, 49)
                        r.expire(f"{REDIS_CRITICAL_PREFIX}recent", REDIS_CRITICAL_TTL)
                    except Exception:
                        pass  # Redis itself might be down
                    _wal_append_critical(_crit_entry)
                conn.close()
            except Exception as e:
                logger.error(f"Infra critical promote error: {e}")

        return audit

    def run_tier(self, tier: int, limit_per_pass: int = 5) -> Dict[str, Any]:
        """Run all passes in a specific tier."""
        return {
            k: self.run_pass(k, limit_per_pass)
            for k, d in PASS_REGISTRY.items() if d["tier"] == tier
        }

    def run_all(self, limit_per_pass: int = 5) -> Dict[str, Any]:
        """Run all 16 passes."""
        return {k: self.run_pass(k, limit_per_pass) for k in PASS_REGISTRY}

    @staticmethod
    def _clean_llm_output(text: str) -> str:
        """Strip thinking chains and conversational preamble from LLM output."""
        if not text:
            return ""
        import re as _re
        cleaned = text.strip()
        cleaned = _re.sub(r"<think>.*?</think>", "", cleaned, flags=_re.DOTALL).strip()
        cleaned = _re.sub(r"<think>.*", "", cleaned, flags=_re.DOTALL).strip()
        preamble_patterns = [
            r"^Okay,?\s+(let'?s|the|so|I)\s+.*?\.\s*",  # "Okay, let's..." / "Okay, the user..."
            r"^Let me\s+.*?\.\s*",
            r"^First,?\s+I\s+need\s+to\s+.*?\.\s*",
            r"^I'll\s+.*?\.\s*",
            r"^Alright,?\s+.*?\.\s*",
            r"^(Here|So|Now|Well|Looking|Based)\s+.*?\.\s*",  # "Here is...", "So the...", etc.
            r"^The user\s+.*?\.\s*",  # "The user provided..."
            r"^They\s+want\s+.*?\.\s*",  # "They want me to extract..."
            r"^I\s+need\s+to\s+.*?\.\s*",  # "I need to identify..."
            r"^This\s+(is|looks|appears|seems)\s+.*?\.\s*",  # "This is about..."
            r"^The task\s+(is|was)\s+.*?\.\s*",  # "The task is called..."
            r"^(So|And)\s+.*?,\s+(so|which|maybe)\s+.*?\.\s*",  # "So maybe..."
            r"^I\s+think\s+.*?\.\s*",  # "I think that means..."
            r"^(Wait|Hmm),?\s+.*?\.\s*",  # "Wait, let me..."
        ]
        for pat in preamble_patterns:
            cleaned = _re.sub(pat, "", cleaned, count=1, flags=_re.IGNORECASE).strip()
        return cleaned

    def _process_item(self, pass_key, pass_def, item, butler_query) -> Dict:
        """Process a single data item through a pass's LLM pipeline."""
        # Truncation guard: prevent silent LLM corruption from oversized content
        # Segment-sourced items get 20K budget (benchmarked optimal for Qwen3-4B narrow tasks)
        # Other items get 6K budget (~1500 tokens, enough for extract profile)
        is_segment = pass_def.get("data_source") == "gold_segments"
        MAX_CONTENT_CHARS = 20000 if is_segment else 6000
        for key in ("content", "gold_text", "code", "statement"):
            if key in item and isinstance(item[key], str) and len(item[key]) > MAX_CONTENT_CHARS:
                original_len = len(item[key])
                item[key] = item[key][:MAX_CONTENT_CHARS] + f"\n[...truncated from {original_len} chars]"

        has_classify = "classify_system" in pass_def

        if has_classify:
            # Step 1: Classify (64 tokens — fast yes/no decision)
            try:
                classify_prompt = pass_def["classify_template"].format(**item)
            except KeyError:
                classify_prompt = pass_def["classify_template"].format(
                    content=item.get("content", ""), insight_type=item.get("insight_type", ""),
                    **{k: item.get(k, "") for k in item}
                )
            classify_result = butler_query(
                pass_def["classify_system"], classify_prompt,
                profile=pass_def.get("classify_profile", "classify"),
            )
            if not classify_result:
                return {"extracted": False, "verdict": "llm_error"}

            verdict_upper = classify_result.strip().upper()
            is_candidate = any(
                kw in verdict_upper
                for kw in ["SOP_CANDIDATE", "SUCCESS", "FAILURE"]
            )
            if not is_candidate:
                return {"extracted": False, "verdict": "skip"}

        # ── Multi-pass extraction: decompose complex eval into narrow sub-tasks ──
        # The 4B model excels at narrow, focused tasks but struggles with multi-
        # dimensional evaluation. Multi-pass runs N narrow LLM calls (each doing
        # ONE thing) then merges results deterministically in Python.
        if "multi_pass_extract" in pass_def:
            return self._run_multi_pass_extract(pass_def, item, butler_query)

        # ── Single-pass extraction (default) ──
        try:
            extract_prompt = pass_def["extract_template"].format(**item)
        except KeyError:
            extract_prompt = pass_def["extract_template"].format(
                **{k: item.get(k, "") for k in item}
            )
        extract_prompt += CRITICAL_SUFFIX

        extract_result = butler_query(
            pass_def["extract_system"], extract_prompt,
            profile=pass_def.get("extract_profile", "extract"),
        )
        if not extract_result or len(extract_result.strip()) < 10:
            return {"extracted": False, "verdict": "extract_failed"}

        cleaned = self._clean_llm_output(extract_result)

        if not cleaned or len(cleaned) < 10:
            return {"extracted": False, "verdict": "extract_failed"}

        # Check for SKIP / nominal responses
        skip_words = {"SKIP", "NOMINAL", "WIRED", "CALIBRATED", "COMPLIANT", "GOOD_CAPTURE"}
        if cleaned.upper().split()[0] in skip_words if cleaned else False:
            return {"extracted": False, "verdict": cleaned.split()[0].upper()}

        # Check for critical finding (filter false positives aggressively)
        critical = None
        if "CRITICAL:" in cleaned:
            parts = cleaned.rsplit("CRITICAL:", 1)
            cleaned = parts[0].strip()
            crit_text = parts[1].strip()[:200].lower()
            false_pos = (
                "no critical", "none", "n/a", "not critical", "no issues",
                "only critical", "only flag", "would be", "most ", "if they",
                "no system", "not applicable", "no data loss", "non-critical",
                "could lead", "may cause", "might", "potentially", "risk of",
                "without proper", "if not", "ensure", "should be", "recommend",
                "consider", "important to", "check that", "verify that",
            )
            if not any(fp in crit_text for fp in false_pos) and len(crit_text) > 5:
                critical = parts[1].strip()[:200]

        return {"extracted": True, "content": cleaned, "critical": critical, "verdict": "extracted"}

    # ── Multi-pass extraction pipeline ──

    def _run_multi_pass_extract(self, pass_def, item, butler_query) -> Dict:
        """Run multi-pass narrow extraction for complex eval tasks.

        Instead of one complex LLM call that the 4B struggles with, runs
        N narrow sub-passes where each does ONE thing well (scoring a single
        dimension, extracting one fact). Results are merged deterministically
        in Python — no LLM call for the merge step.

        Sub-pass types:
          - LLM sub-pass: narrow query, typically 'classify' profile (64 tok)
          - gate sub-pass: LLM + rejection if result starts with NO/SKIP
          - python_merge: deterministic merge function, no LLM call
        """
        sub_results = {}

        for sub_pass in pass_def["multi_pass_extract"]:
            name = sub_pass["name"]

            if sub_pass.get("type") == "python_merge":
                merge_fn = getattr(self, sub_pass["merge_fn"], None)
                if not merge_fn:
                    return {"extracted": False, "verdict": "missing_merge_fn"}
                merged = merge_fn(sub_results, item)
                if not merged or len(merged) < 10:
                    return {"extracted": False, "verdict": "merge_failed"}
                sub_results[name] = merged
                continue

            # LLM sub-pass
            system = sub_pass["system"]
            template_vars = {**item, **sub_results}
            try:
                prompt = sub_pass["template"].format(**template_vars)
            except KeyError as e:
                logger.warning(f"Multi-pass template key error: {e}")
                return {"extracted": False, "verdict": "template_error"}

            result = butler_query(
                system, prompt,
                profile=sub_pass.get("profile", "classify"),
            )
            if not result:
                return {"extracted": False, "verdict": f"multipass_{name}_error"}

            cleaned = self._clean_llm_output(result)

            # Gate sub-passes can reject the item early
            if sub_pass.get("is_gate") and cleaned.upper().startswith(("NO", "SKIP", "N/A")):
                return {"extracted": False, "verdict": "skip"}

            sub_results[name] = cleaned

        # Final result is the last sub-pass output
        final_key = pass_def["multi_pass_extract"][-1]["name"]
        final = sub_results.get(final_key, "")

        if not final or len(final) < 10:
            return {"extracted": False, "verdict": "multipass_empty"}

        # Check for critical findings in merged output
        critical = None
        if "CRITICAL:" in final:
            parts = final.rsplit("CRITICAL:", 1)
            final = parts[0].strip()
            crit_text = parts[1].strip()[:200].lower()
            false_pos = (
                "no critical", "none", "n/a", "not critical", "no issues",
                "only critical", "only flag", "would be", "most ", "if they",
                "no system", "not applicable", "no data loss", "non-critical",
                "could lead", "may cause", "might", "potentially", "risk of",
                "without proper", "if not", "ensure", "should be", "recommend",
                "consider", "important to", "check that", "verify that",
            )
            if not any(fp in crit_text for fp in false_pos) and len(crit_text) > 5:
                critical = parts[1].strip()[:200]

        return {"extracted": True, "content": final, "critical": critical, "verdict": "extracted"}

    # ── Multi-pass merge functions (deterministic, no LLM) ──

    def _merge_webhook_quality(self, sub_results: Dict, item: Dict) -> str:
        """Merge webhook quality dimension scores into structured output.

        Each dimension sub-pass outputs '2 mostly relevant' style.
        We parse the number, keep the reason, compute total, identify weakest.
        """
        import re
        scores = {}
        for dim in ["relevance", "completeness", "freshness", "actionability"]:
            text = sub_results.get(dim, "0 unknown")
            match = re.search(r'\b([0-3])\b', text)
            score = int(match.group(1)) if match else 0
            reason = re.sub(r'^\s*[0-3]\s*[-:.]?\s*', '', text).strip()[:60] or "no detail"
            scores[dim] = (score, reason)

        total = sum(s[0] for s in scores.values())

        if total >= 10:
            issue = "NONE"
        else:
            weakest = min(scores, key=lambda k: scores[k][0])
            issue = f"Weak {weakest}: {scores[weakest][1]}"

        infra = str(item.get("infra_status", ""))
        if "DOWN" in infra.upper():
            issue = f"INFRA DOWN — {issue}"

        lines = []
        for dim in ["relevance", "completeness", "freshness", "actionability"]:
            s, r = scores[dim]
            lines.append(f"{dim.upper()}: {s} {r}")
        lines.append(f"TOTAL: {total}/12")
        lines.append(f"ISSUE: {issue}")
        return "\n".join(lines)

    def _merge_success_measurement(self, sub_results: Dict, item: Dict) -> str:
        """Merge success measurement sub-pass results into structured output."""
        what = sub_results.get("what_succeeded", "").strip()
        metric = sub_results.get("metric_extract", "").strip()

        if not what or what.upper() in ("NONE", "SKIP", "N/A", "NO"):
            return ""  # Will be caught as merge_failed → filtered

        has_metric = metric and metric.upper() not in ("NONE", "N/A", "NO METRIC", "NO")
        confidence = "0.8" if has_metric else "0.5"

        lines = [
            f"SUCCESS: {what}",
            f"EVIDENCE: {what}",
            f"METRIC: {metric if has_metric else 'qualitative only'}",
            f"CONFIDENCE: {confidence}",
        ]
        return "\n".join(lines)

    def _merge_failure_measurement(self, sub_results: Dict, item: Dict) -> str:
        """Merge failure measurement sub-pass results into structured output."""
        what = sub_results.get("what_failed", "").strip()
        impact = sub_results.get("impact_extract", "").strip()
        root = sub_results.get("root_cause", "").strip()

        if not what or what.upper() in ("NONE", "SKIP", "N/A", "NO"):
            return ""

        lines = [
            f"FAILURE: {what}",
            f"EVIDENCE: {what}",
            f"IMPACT: {impact if impact and impact.upper() not in ('NONE', 'N/A') else 'unknown'}",
            f"ROOT_CAUSE: {root if root and root.upper() not in ('NONE', 'N/A', 'UNKNOWN') else 'unknown'}",
        ]
        return "\n".join(lines)

    def _merge_bigpicture(self, sub_results: Dict, item: Dict) -> str:
        """Merge big picture tracking sub-pass results."""
        goal = sub_results.get("goal", "").strip()
        planned = sub_results.get("planned", "").strip()
        actual = sub_results.get("actual", "").strip()
        drift = sub_results.get("drift", "").strip()

        if not goal or goal.upper() in ("SKIP", "NONE", "N/A"):
            return ""

        # Parse drift level from output like "MINOR deviated from plan"
        drift_upper = drift.upper().split()[0] if drift else "UNKNOWN"
        if drift_upper not in ("ALIGNED", "MINOR", "MAJOR", "CRITICAL"):
            drift_upper = "UNKNOWN"

        # Recommendation based on drift level
        rec_map = {
            "ALIGNED": "Continue current trajectory",
            "MINOR": f"Minor correction needed: {actual}",
            "MAJOR": f"Re-evaluate approach: planned '{planned}' but got '{actual}'",
            "CRITICAL": f"URGENT: session drifted critically from goal '{goal}'",
            "UNKNOWN": "Review session outcome vs goal",
        }

        lines = [
            f"GOAL: {goal}",
            f"PLANNED: {planned}",
            f"ACTUAL: {actual}",
            f"DRIFT: {drift}",
            f"RECOMMENDATION: {rec_map.get(drift_upper, rec_map['UNKNOWN'])}",
        ]
        return "\n".join(lines)

    def _merge_code_artifacts(self, sub_results: Dict, item: Dict) -> str:
        """Merge code artifact analysis sub-pass results."""
        change = sub_results.get("change_desc", "").strip()
        pattern = sub_results.get("pattern", "").strip()
        scope = sub_results.get("scope", "").strip()
        fragility = sub_results.get("fragility", "").strip()

        if not change or change.upper() in ("SKIP", "NONE", "TRIVIAL"):
            return ""

        # Normalize scope and fragility to expected values
        scope_word = scope.upper().split()[0] if scope else "UTILITY"
        if scope_word not in ("CORE", "MODULE", "UTILITY", "TRIVIAL"):
            scope_word = "UTILITY"

        frag_word = fragility.upper().split()[0] if fragility else "LOW"
        if frag_word not in ("HIGH", "MEDIUM", "LOW"):
            frag_word = "LOW"

        # Auto-generate recommendation based on scope + fragility
        if scope_word == "CORE" and frag_word == "HIGH":
            rec = "Critical: add tests and reduce coupling"
        elif scope_word == "CORE":
            rec = "Consider extraction into a documented pattern"
        elif frag_word == "HIGH":
            rec = "Reduce coupling or add defensive checks"
        else:
            rec = "NONE"

        file_path = item.get("file_path", "unknown")
        lines = [
            f"CHANGE: {change}",
            f"PATTERN: {pattern if pattern and pattern.upper() != 'AD-HOC' else 'none identified'}",
            f"SCOPE: {scope_word}",
            f"FRAGILITY: {frag_word}",
            f"RECOMMENDATION: {rec}",
        ]
        return "\n".join(lines)

    # ── Downstream routing ──

    def _route_downstream(self, pass_key, pass_def, item, result):
        downstream = pass_def.get("downstream", "")
        content = result.get("content", "")
        try:
            handler = getattr(self, f"_ds_{downstream}", None)
            if handler:
                handler(pass_def, item, content)
            else:
                logger.warning(f"No downstream handler: {downstream}")
        except Exception as e:
            logger.error(f"Downstream {downstream} error: {e}")

        # Chain: store_learning passes also feed quarantine validation pipeline
        if downstream == "store_learning":
            try:
                self._ds_quarantine_claim(pass_def, item, content)
            except Exception as e:
                logger.error(f"Quarantine claim downstream error: {e}")

    def _ds_store_learning(self, pass_def, item, content):
        from memory.sqlite_storage import get_sqlite_storage
        storage = get_sqlite_storage()
        sop_type = pass_def.get("downstream_type", "pattern")
        session_id = str(item.get("session_id", item.get("id", "?")))
        src = session_id[:12]
        storage.store_learning({
            "type": sop_type,
            "content": content[:2000],
            "source": f"gold_pass:{pass_def['id']}:{src}",
            "title": content[:120],
            "session_id": session_id,
            "metadata": {"mined_at": datetime.now(timezone.utc).isoformat(), "pass_id": pass_def["id"]},
        })

    # ── Gold type → EBM evidence grade mapping ──
    # Tier 1 gold passes produce anecdotal-grade evidence (single session observation).
    # Repeated confirmations via quarantine promotion raise the effective grade.
    GOLD_TYPE_EVIDENCE_GRADE = {
        "fix": "anecdotal",          # Bug fix from one session
        "pattern": "case_series",    # Repeatable process (slightly stronger)
        "gotcha": "anecdotal",       # Anti-pattern from one incident
        "decision": "expert_opinion", # Architecture decision with rationale
    }

    def _ds_quarantine_claim(self, pass_def, item, content):
        """Route gold extraction into quarantine validation pipeline.

        Creates a quarantined claim in ObservabilityStore so the learning
        goes through evidence accumulation before becoming trusted wisdom.
        Runs as ADDITIONAL downstream — store_learning still fires first.
        """
        if not content or content.strip().upper() == "SKIP":
            return

        from memory.observability_store import get_observability_store

        sop_type = pass_def.get("downstream_type", "pattern")
        session_id = str(item.get("session_id", item.get("id", "?")))
        pass_id = pass_def.get("id", 0)
        pass_name = pass_def.get("name", "unknown")

        evidence_grade = self.GOLD_TYPE_EVIDENCE_GRADE.get(sop_type, "anecdotal")

        # Base confidence: anecdotal=0.3, case_series=0.4, expert_opinion=0.5
        base_confidence_map = {
            "anecdotal": 0.3,
            "case_series": 0.4,
            "expert_opinion": 0.5,
        }
        base_confidence = base_confidence_map.get(evidence_grade, 0.3)

        # Build claim text with gold pass provenance
        title_line = content[:120].split("\n")[0]
        claim_text = f"[GOLD:{sop_type.upper()}] {title_line}"

        source = f"gold_pass:{pass_id}:{session_id[:12]}"
        tags = [f"gold_type:{sop_type}", f"pass:{pass_id}", f"pass_name:{pass_name}"]
        area = "gold_mining"

        store = get_observability_store()
        store.record_claim_with_evidence(
            claim_text=claim_text[:500],
            evidence_grade=evidence_grade,
            source=source,
            confidence=base_confidence,
            tags=tags,
            area=area,
        )

    def _ds_sop_quality_score(self, pass_def, item, content):
        score = 0
        for line in content.split("\n"):
            if line.strip().startswith("SCORE:"):
                try:
                    score = int(re.search(r"\d", line.split(":", 1)[1]).group())
                except Exception:
                    pass
        self._store_obs_note("sop_quality_audit", content, score)

        # Wire quality score back to learning metadata — feeds S1 selection
        learning_id = item.get("id")
        if learning_id and score > 0:
            try:
                from memory.sqlite_storage import get_sqlite_storage
                import json as _json
                storage = get_sqlite_storage()
                row = storage.conn.execute(
                    "SELECT metadata FROM learnings WHERE id = ?", (learning_id,)
                ).fetchone()
                if row:
                    meta = _json.loads(row["metadata"] or "{}")
                    meta["quality_score"] = score / 5.0  # Normalize 0-1
                    meta["quality_raw"] = score
                    storage.conn.execute(
                        "UPDATE learnings SET metadata = ? WHERE id = ?",
                        (_json.dumps(meta), learning_id)
                    )
                    storage.conn.commit()
            except Exception as e:
                logger.debug(f"Quality score writeback: {e}")

    def _ds_injection_quality_log(self, pass_def, item, content):
        import re as _re
        total = 0
        dimensions = {}
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("TOTAL:"):
                try:
                    total = float(stripped.split(":")[1].strip().split("/")[0])
                except Exception:
                    pass
            # Parse dimension scores: "RELEVANCE: 1 tangential"
            for dim in ("RELEVANCE", "COMPLETENESS", "FRESHNESS", "ACTIONABILITY"):
                if stripped.startswith(f"{dim}:"):
                    m = _re.search(r':\s*([0-3])', stripped)
                    if m:
                        dimensions[dim.lower()] = int(m.group(1))
        self._store_obs_note("injection_quality_audit", content, total)

        # Queue cardiologist investigation for degraded dimensions (score 0-1)
        degraded = {k: v for k, v in dimensions.items() if v <= 1}
        if degraded and total < 8:
            try:
                import redis, json as _json
                r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
                investigation = {
                    "injection_id": item.get("id", "unknown"),
                    "task_type": item.get("task_type", "unknown"),
                    "total_score": total,
                    "degraded_dimensions": degraded,
                    "all_dimensions": dimensions,
                    "context_snippet": content[:300],
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                }
                r.lpush("quality:investigation_queue", _json.dumps(investigation))
                # Cap queue to 20 pending investigations
                r.ltrim("quality:investigation_queue", 0, 19)
                logger.info(f"QUALITY INVESTIGATION QUEUED: {degraded} (total={total}/12)")
            except Exception as e:
                logger.debug(f"Quality investigation queue: {e}")

    def _ds_outcome_event(self, pass_def, item, content):
        success = pass_def.get("downstream_success", True)
        reward = 0.3 if success else -0.3
        if not OBS_DB.exists():
            return
        conn = connect_wal(OBS_DB)
        oid = f"gold_{uuid.uuid4().hex[:12]}"
        sid = item.get("session_id", "unknown")
        conn.execute("""
            INSERT OR IGNORE INTO outcome_event
            (outcome_id, timestamp_utc, session_id, outcome_type, success, reward, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (oid, datetime.now(timezone.utc).isoformat(), sid,
              "gold_mining", 1 if success else 0, reward, content[:1000]))
        conn.commit()
        conn.close()

    def _ds_big_picture(self, pass_def, item, content):
        if not PLANS_DB.exists():
            return
        goal = "Unknown"
        for line in content.split("\n"):
            if line.strip().startswith("GOAL:"):
                goal = line.split(":", 1)[1].strip()[:80]
                break
        if goal.upper() == "NONE":
            return
        conn = connect_wal(PLANS_DB)
        now = datetime.now(timezone.utc).isoformat()
        plan_id = f"bp_{item.get('id', '?')[:20]}"
        conn.execute("""
            INSERT OR REPLACE INTO major_plans
            (plan_id, title, category, description, status, priority,
             mentioned_count, last_mentioned, extracted_by, extracted_at,
             confidence, project)
            VALUES (?, ?, 'big_picture', ?, 'tracked', 'medium',
                    1, ?, 'gold_pass_9', ?, 0.7, 'context-dna')
        """, (plan_id, goal, content[:500], now, now))
        conn.commit()
        conn.close()

    def _ds_meta_analysis(self, pd, item, content):
        self._store_obs_note("cross_session_pattern", content)

    def _ds_feedback_loop_registry(self, pd, item, content):
        self._store_obs_note("feedback_loop_gap", content)

    def _ds_code_intelligence(self, pd, item, content):
        self._store_obs_note("code_artifact_analysis", content)

        # Parse LLM analysis output
        fragility = "LOW"
        scope = "TRIVIAL"
        pattern = ""
        change = ""
        recommendation = ""
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("FRAGILITY:"):
                fragility = stripped.split(":", 1)[1].strip().upper()
            elif stripped.startswith("SCOPE:"):
                scope = stripped.split(":", 1)[1].strip().upper()
            elif stripped.startswith("PATTERN:"):
                pattern = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("CHANGE:"):
                change = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("RECOMMENDATION:"):
                recommendation = stripped.split(":", 1)[1].strip()

        file_path = item.get("file_path", "unknown")
        language = item.get("language", "unknown")

        # ── ARCHITECTURAL CRITICAL: CORE scope or HIGH fragility ──
        # These are structural gold that must not be skipped.
        # Routes to holding tank → LLM verification → webhook injection.
        is_architectural = (
            "CORE" in scope
            or (fragility == "HIGH" and "TRIVIAL" not in scope)
            or (recommendation and recommendation.upper() != "NONE"
                and ("CORE" in scope or "MODULE" in scope))
        )
        if is_architectural and change:
            finding = (
                f"[{language}] {file_path}: {change[:80]} | "
                f"Pattern: {pattern[:40]} | Fragility: {fragility} | "
                f"Scope: {scope} | Rec: {recommendation[:80]}"
            )
            self.critical_handler.hold(
                "arch_code_artifact", pd, item, finding
            )

        # HIGH fragility or non-trivial recommendations → big picture
        if fragility == "HIGH" or (recommendation and recommendation.upper() != "NONE"):
            try:
                if PLANS_DB.exists():
                    conn = connect_wal(PLANS_DB)
                    now = datetime.now(timezone.utc).isoformat()
                    plan_id = f"code_{item.get('id', uuid.uuid4().hex[:12])}"
                    title = f"[{language}] {change[:60]}" if change else f"[{language}] {file_path}"
                    conn.execute("""
                        INSERT OR REPLACE INTO major_plans
                        (plan_id, title, category, description, status, priority,
                         mentioned_count, last_mentioned, extracted_by, extracted_at,
                         confidence, project)
                        VALUES (?, ?, 'code_architecture', ?, 'tracked',
                                ?, 1, ?, 'gold_pass_12', ?, 0.6, 'context-dna')
                    """, (plan_id, title, content[:500],
                          'high' if fragility == 'HIGH' else 'medium',
                          now, now))
                    conn.commit()
                    conn.close()
            except Exception as e:
                logger.debug(f"Code artifact → big_picture failed: {e}")

        # All analyzed artifacts (not SKIP) → learnings for professor wisdom
        if change and content.upper() != "SKIP":
            try:
                from memory.sqlite_storage import get_sqlite_storage
                storage = get_sqlite_storage()
                storage.store_learning({
                    "type": "code_pattern",
                    "content": f"File: {file_path}\n{content[:400]}",
                    "source": f"gold_pass:12:{item.get('id', '?')[:12]}",
                    "title": f"[{language}] {pattern[:60]}" if pattern else change[:60],
                })
            except Exception as e:
                logger.debug(f"Code artifact → learning failed: {e}")

    def _ds_evidence_health(self, pd, item, content):
        self._store_obs_note("evidence_quality_audit", content)

        # Wire LLM verdict back as claim outcome — feeds grade ladder
        claim_id = item.get("id")
        if not claim_id:
            return
        try:
            grade_correct = False
            for line in content.split("\n"):
                stripped = line.strip().upper()
                if stripped.startswith("GRADE_CORRECT:"):
                    grade_correct = "YES" in stripped
                    break
            from memory.observability_store import get_observability_store
            store = get_observability_store()
            store.record_direct_claim_outcome(
                claim_id=claim_id,
                success=grade_correct,
                reward=0.3 if grade_correct else -0.1,
                source="gold_pass_13_evidence_audit",
                notes=content[:200],
            )
        except Exception as e:
            logger.debug(f"Evidence health outcome writeback: {e}")

    def _ds_butler_scorecard(self, pd, item, content):
        self._store_obs_note("butler_performance", content)

    def _ds_historian_quality(self, pd, item, content):
        self._store_obs_note("historian_capture_quality", content)

    def _ds_constitutional_audit(self, pd, item, content):
        self._store_obs_note("constitutional_compliance", content)

    def _ds_anticipation_quality(self, pass_def, item, content):
        """Route pass 17 insights by extracted TYPE. Breaks feedback loops via source tag."""
        if not content or "NO_VALUE" in content.upper():
            return
        # Parse TYPE from structured output
        insight_type = "pattern"  # default
        for line in content.split("\n"):
            if line.strip().startswith("TYPE:"):
                raw = line.split(":", 1)[1].strip().upper()
                if "WISDOM" in raw:
                    insight_type = "pattern"
                elif "PATTERN" in raw:
                    insight_type = "pattern"
                elif "GOTCHA" in raw:
                    insight_type = "gotcha"
                elif "SOP" in raw:
                    insight_type = "sop"
                break
        # Check for CRITICAL findings — route to holding tank, NOT store_learning
        if "CRITICAL:" in content.upper():
            critical_handler = CriticalFindingHandler()
            critical_handler.hold("eval_anticipation_cross_exam", pass_def, item, content)
            return
        # Store as learning with lineage source tag to prevent feedback loops.
        # Anticipation engine context builder can filter by source prefix.
        from memory.sqlite_storage import get_sqlite_storage
        storage = get_sqlite_storage()
        session_id = str(item.get("session_id", item.get("id", "?")))
        storage.store_learning({
            "type": insight_type,
            "content": content[:2000],
            "source": f"anticipation_cross_exam:17:{session_id[:12]}",
            "title": content[:120],
            "session_id": session_id,
            "metadata": {
                "mined_at": datetime.now(timezone.utc).isoformat(),
                "pass_id": 17,
                "origin": "anticipation_archive",
                "original_section": item.get("section", "unknown"),
            },
        })
        # Mark cross_examined in archive DB to prevent re-fetch
        self._mark_cross_examined(item)

    def _mark_cross_examined(self, item):
        """Set cross_examined=1 in anticipation archive DB."""
        archive_db = Path(__file__).parent / ".anticipation_archive.db"
        if not archive_db.exists():
            return
        try:
            conn = connect_wal(archive_db)
            conn.execute(
                "UPDATE anticipation_archive SET cross_examined = 1 WHERE id = ?",
                (int(item.get("id", 0)),)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to mark cross_examined: {e}")

    def _store_obs_note(self, source_job, content, score=None):
        """Store evaluation as butler_code_note in observability DB."""
        if not OBS_DB.exists():
            return
        conn = connect_wal(OBS_DB)
        nid = f"gp_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        sev = "critical" if score is not None and score <= 1 else "info"
        conn.execute("""
            INSERT OR IGNORE INTO butler_code_note
            (note_id, timestamp_utc, source_job, anatomic_location, error_type,
             severity, error_message, llm_analysis, llm_confidence)
            VALUES (?, ?, ?, 'general', 'other', ?, ?, ?, ?)
        """, (nid, now, source_job, sev, content[:200], content[:500], score or 0.5))
        conn.commit()
        conn.close()

    # ── Processing log ──

    def _log_processing(self, pass_key, item, result):
        try:
            conn = connect_wal(ARCHIVE_DB)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                INSERT INTO pass_processing_log
                (pass_id, item_id, item_type, processed_at, verdict,
                 extracted_content, critical_finding, downstream_action)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pass_key,
                str(item.get("id", "unknown")),
                "gold_segment" if str(item.get("id", "")).startswith("gs:") else item.get("insight_type", item.get("artifact_type", "unknown")),
                now,
                result.get("verdict", ""),
                result.get("content", "")[:2000] if result.get("extracted") else None,
                result.get("critical"),
                PASS_REGISTRY[pass_key].get("downstream", ""),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Log error: {e}")


# ============================================================
# PUBLIC API
# ============================================================

def run_all_passes(limit_per_pass: int = 5) -> Dict[str, Any]:
    """Run all 16 session gold mining passes. Returns results per pass."""
    runner = SessionGoldPassRunner()
    return runner.run_all(limit_per_pass)


def run_pass(pass_key: str, limit: int = 10) -> Dict[str, Any]:
    """Run a single pass by key name."""
    runner = SessionGoldPassRunner()
    return runner.run_pass(pass_key, limit)


def run_tier(tier: int, limit_per_pass: int = 5) -> Dict[str, Any]:
    """Run all passes in a tier (1-4)."""
    runner = SessionGoldPassRunner()
    return runner.run_tier(tier, limit_per_pass)


def evaluate_critical_holding_tank(limit: int = 5) -> Dict[str, int]:
    """Evaluate held critical findings via LLM. Promote real ones."""
    handler = CriticalFindingHandler()
    return handler.evaluate_holding_tank(limit)


def run_webhook_infrastructure_audit() -> Dict[str, Any]:
    """Run programmatic infrastructure health audit. Critical failures auto-promoted."""
    runner = SessionGoldPassRunner()
    return runner.run_webhook_infrastructure_audit()


def is_pass_running() -> bool:
    """Check if a pass is currently running (for anticipation engine to defer)."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        return r.exists(REDIS_PASS_LOCK) > 0
    except Exception:
        return False


def get_critical_findings(acknowledged: bool = False) -> List[Dict]:
    """Get critical findings. SQLite authoritative, Redis list cache, WAL sorted set as durable fallback.

    Priority: SQLite (has ack state) → Redis list (fast cache) → WAL sorted set (additive, never trimmed).
    WAL entries also merged when SQLite is available — WAL may contain entries from
    cardiologist, sentinel, or surgery-team that haven't yet hit SQLite.
    """
    findings = []

    # 1. SQLite is authoritative (has acknowledged field)
    try:
        conn = connect_wal(ARCHIVE_DB)
        conn.row_factory = sqlite3.Row
        q = "SELECT * FROM critical_findings"
        if not acknowledged:
            q += " WHERE acknowledged = 0"
        q += " ORDER BY found_at DESC LIMIT 20"
        rows = conn.execute(q).fetchall()
        conn.close()
        findings = [dict(r) for r in rows]
    except Exception:
        pass

    # 2. Redis list fallback only if SQLite unavailable
    if not findings:
        try:
            import redis
            r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
            items = r.lrange(f"{REDIS_CRITICAL_PREFIX}recent", 0, 49)
            for item in items:
                findings.append(json.loads(item))
        except Exception:
            pass

    # 3. WAL sorted set — merge in entries not already in findings
    #    WAL is additive and may have entries from sources that bypass SQLite
    try:
        wal_items = wal_get_criticals(max_age_hours=168, limit=50)
        for w in wal_items:
            findings.append(w)
    except Exception:
        pass

    # Dedup by pass+finding combo (same finding from multiple cycles)
    seen = set()
    deduped = []
    for f in findings:
        key = (f.get("pass", f.get("pass_id", "")), f.get("finding", "")[:100])
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    return deduped


def acknowledge_critical_finding(finding_id: int, action: str = "") -> bool:
    """Mark a critical finding as acknowledged. Rebuilds Redis cache.

    Previous bug: r.delete() nuked ALL Redis entries when acking one finding.
    Now we ack in SQLite and rebuild Redis from SQLite truth.
    """
    try:
        conn = connect_wal(ARCHIVE_DB)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE critical_findings
            SET acknowledged = 1, acknowledged_at = ?, action_taken = ?
            WHERE id = ?
        """, (now, action, finding_id))
        conn.commit()
        conn.close()
        # Rebuild Redis from SQLite (instead of nuclear delete)
        _rebuild_redis_from_sqlite()
        return True
    except Exception:
        return False


def _rebuild_redis_from_sqlite():
    """Rebuild Redis critical list from SQLite truth (unacknowledged only)."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        list_key = f"{REDIS_CRITICAL_PREFIX}recent"

        # Get unacknowledged from SQLite
        conn = connect_wal(ARCHIVE_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM critical_findings
            WHERE acknowledged = 0
            ORDER BY found_at DESC LIMIT 20
        """).fetchall()
        conn.close()

        # Rebuild Redis list
        r.delete(list_key)
        for row in rows:
            r.rpush(list_key, json.dumps({
                "pass": row["pass_id"], "finding": row["finding"],
                "found_at": row["found_at"], "severity": row["severity"],
                "verified": True,
            }))
        if rows:
            r.expire(list_key, REDIS_CRITICAL_TTL)
    except Exception:
        pass


def _wal_append_critical(finding_dict: Dict) -> bool:
    """Append a critical finding to the Redis WAL (sorted set).

    WAL properties: additive (never trimmed), ordered by timestamp score,
    deduped by (pass, finding[:100]) — ZADD won't duplicate same member.
    Only explicit ZREM or 7-day pruning removes entries.
    """
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

        # Score = unix timestamp for ordering
        ts = finding_dict.get("found_at", datetime.now(timezone.utc).isoformat())
        try:
            from datetime import datetime as _dt
            if isinstance(ts, str):
                # Parse ISO to unix timestamp
                ts_clean = ts.replace("Z", "+00:00")
                score = _dt.fromisoformat(ts_clean).timestamp()
            else:
                score = float(ts)
        except Exception:
            score = datetime.now(timezone.utc).timestamp()

        # Member = dedup key + full payload
        pass_id = finding_dict.get("pass", finding_dict.get("pass_id", "unknown"))
        finding_text = finding_dict.get("finding", "")
        member = json.dumps({
            "pass": pass_id,
            "finding": finding_text,
            "severity": finding_dict.get("severity", "critical"),
            "found_at": finding_dict.get("found_at", ""),
            "verified": finding_dict.get("verified", False),
            "verified_at": finding_dict.get("verified_at", ""),
            "source": finding_dict.get("source", "gold_mining"),
        }, sort_keys=True)

        r.zadd(REDIS_CRITICAL_WAL_KEY, {member: score})
        r.expire(REDIS_CRITICAL_WAL_KEY, REDIS_CRITICAL_WAL_TTL)
        return True
    except Exception as e:
        logger.debug(f"WAL append error: {e}")
        return False


def wal_get_criticals(max_age_hours: int = 168, limit: int = 50) -> List[Dict]:
    """Read criticals from the WAL sorted set (newest first).

    Args:
        max_age_hours: Only return entries newer than this (default 7 days)
        limit: Max entries to return
    """
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

        min_score = (datetime.now(timezone.utc).timestamp()
                     - (max_age_hours * 3600))
        items = r.zrangebyscore(
            REDIS_CRITICAL_WAL_KEY, min_score, "+inf",
            start=0, num=limit, withscores=True
        )
        results = []
        for member, score in reversed(items):  # newest first
            try:
                entry = json.loads(member)
                entry["_wal_score"] = score
                results.append(entry)
            except Exception:
                pass
        return results
    except Exception:
        return []


def wal_remove_critical(pass_id: str, finding_prefix: str = "") -> int:
    """Remove a specific critical from the WAL (after acknowledgment).

    Returns number of entries removed.
    """
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

        # Get all members and find matching ones
        all_items = r.zrangebyscore(REDIS_CRITICAL_WAL_KEY, "-inf", "+inf")
        removed = 0
        for member in all_items:
            try:
                entry = json.loads(member)
                if entry.get("pass", "") == pass_id:
                    if not finding_prefix or entry.get("finding", "").startswith(finding_prefix):
                        r.zrem(REDIS_CRITICAL_WAL_KEY, member)
                        removed += 1
            except Exception:
                pass
        return removed
    except Exception:
        return 0


def wal_prune_old(max_age_hours: int = 168) -> int:
    """Prune WAL entries older than max_age_hours. Returns count removed."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        cutoff = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)
        return r.zremrangebyscore(REDIS_CRITICAL_WAL_KEY, "-inf", cutoff)
    except Exception:
        return 0


def verify_critical(pass_id: str, finding_prefix: str = "") -> Dict:
    """Test validity of a critical finding by re-running its source check.

    Returns: {valid: bool, reason: str, checked_at: str}
    """
    result = {
        "valid": False,
        "reason": "unknown",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "pass_id": pass_id,
    }

    try:
        # Infrastructure criticals: re-run the specific health probe
        if pass_id.startswith("infra_"):
            check_id = pass_id.replace("infra_", "")
            from memory.session_gold_passes import GoldMiner
            miner = GoldMiner()
            # _run_infra_audit returns dict with "criticals" list
            audit = miner._run_infra_audit()
            active_checks = [c["check_id"] for c in audit.get("criticals", [])]
            if check_id in active_checks:
                result["valid"] = True
                result["reason"] = f"Infrastructure check {check_id} still failing"
            else:
                result["valid"] = False
                result["reason"] = f"Infrastructure check {check_id} now passing"

        # Corrigibility gate criticals
        elif pass_id == "corrigibility_gate":
            result["valid"] = True  # Requires manual verification
            result["reason"] = "Corrigibility finding — requires manual review"

        # CV Sentinel criticals
        elif pass_id == "cv_sentinel":
            result["valid"] = True  # Vectors drift is ongoing
            result["reason"] = "Vector drift — re-run sentinel for current state"

        # Quality-based criticals (cardiologist, eval_webhook_quality, etc.)
        elif pass_id.startswith("quality_") or pass_id.startswith("eval_"):
            result["valid"] = True  # Requires re-evaluation
            result["reason"] = "Quality finding — re-run cardiologist for current state"

        # SOP/antipattern criticals
        elif pass_id in ("sop_antipattern", "eval_sop_quality"):
            result["valid"] = True
            result["reason"] = "SOP finding — verify fix was applied"

        # Architectural findings
        elif pass_id == "arch_code_artifact":
            result["valid"] = True
            result["reason"] = "Architectural finding — persistent until addressed"

        else:
            result["reason"] = f"Unknown pass_id: {pass_id} — manual verification required"

    except Exception as e:
        result["reason"] = f"Verification error: {e}"

    return result


def get_pass_stats() -> Dict[str, Any]:
    """Get processing statistics for all passes."""
    stats = {}
    try:
        conn = connect_wal(ARCHIVE_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT pass_id,
                   COUNT(*) as total,
                   SUM(CASE WHEN extracted_content IS NOT NULL THEN 1 ELSE 0 END) as extracted,
                   SUM(CASE WHEN critical_finding IS NOT NULL THEN 1 ELSE 0 END) as critical,
                   MAX(processed_at) as last_run
            FROM pass_processing_log GROUP BY pass_id
        """).fetchall()
        conn.close()
        for r in rows:
            stats[r["pass_id"]] = dict(r)
    except Exception:
        pass
    return stats


def list_passes() -> List[Dict]:
    """List all 16 passes with definitions."""
    return [
        {"key": k, "id": v["id"], "name": v["name"], "tier": v["tier"],
         "data_source": v["data_source"], "downstream": v.get("downstream", "")}
        for k, v in sorted(PASS_REGISTRY.items(), key=lambda x: x[1]["id"])
    ]
