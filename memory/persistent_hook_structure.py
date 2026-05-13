#!/usr/bin/env python3
"""
PERSISTENT HOOK STRUCTURE - Unified Context Injection System

This module defines the LOCKED-IN structure for context injection hooks.
A/B testing happens WITHIN these confines, not by changing the structure itself.

Architecture:
┌─────────────────────────────────────────────┐
│ SECTION 0: SAFETY (🔒 LOCKED - Never test)  │
│   - Never Do prohibitions                    │
│   - Critical gotcha                          │
├─────────────────────────────────────────────┤
│ SECTION 1: FOUNDATION (A/B: format only)    │
│   - The exact file                           │
│   - Foundation SOPs                          │
│   - Successful chain                         │
├─────────────────────────────────────────────┤
│ SECTION 2: WISDOM (A/B: source selection)   │
│   - Professor wisdom                         │
│   - OR Wisdom injection (C variant)          │
├─────────────────────────────────────────────┤
│ SECTION 3: AWARENESS (A/B: depth by risk)   │
│   - Recent changes                           │
│   - Ripple effects                           │
│   - Previous mistakes                        │
├─────────────────────────────────────────────┤
│ SECTION 4: DEEP CONTEXT (critical/high)     │
│   - Blueprint                                │
│   - Brain state                              │
├─────────────────────────────────────────────┤
│ SECTION 5: PROTOCOL (A/B: verbosity)        │
│   - First-try likelihood                     │
│   - Success capture reminder                 │
├─────────────────────────────────────────────┤
│ SECTION 6: HOLISTIC CONTEXT                 │
│   - Synaptic's task-focused guidance        │
│   - Contextually relevant to current task   │
├─────────────────────────────────────────────┤
│ SECTION 7: FULL LIBRARY (Tier 3 escalation) │
│   - Extended context after 2+ failures      │
├─────────────────────────────────────────────┤
│ SECTION 8: 8TH INTELLIGENCE                 │
│   - Synaptic's subconscious voice to Aaron  │
│   - ALWAYS present (never sleeps)           │
├─────────────────────────────────────────────┤
│ SECTION 10: VISION                          │
│   - Atlas's proactive contextual awareness  │
│   - Surfaces relevance without being asked  │
│   - Distinct from Synaptic (interpretation) │
└─────────────────────────────────────────────┘

Usage:
    from memory.persistent_hook_structure import generate_context_injection

    context = generate_context_injection(
        prompt="deploy Django to production",
        mode="hybrid",  # or "layered", "greedy", "minimal"
        session_id="abc123"
    )
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time

# Global logger with defensive fallback for per-section health tracking
logger = logging.getLogger('context_dna')

def _safe_log(level: str, message: str):
    """Per-section logging with stderr fallback (preserves GAP 3 FIX intention)."""
    try:
        if level == 'warning':
            logger.warning(message)
        elif level == 'debug':
            logger.debug(message)
        else:
            logger.info(message)
    except Exception:
        # Defensive fallback: stderr persistence (always available)
        print(f"[{level.upper()}] {message}", file=sys.stderr)

# Ensure logging is available globally
logger = logging.getLogger('context_dna')
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

# Phase 0 decomposition: types extracted to webhook_types.py (backward compat re-exports)
from memory.webhook_types import (  # noqa: F401 — re-exported for backward compatibility
    InjectionMode, RiskLevel, VolumeEscalationTier, ExclusionReason,
    InjectionConfig, SOPEntry, ManifestRef, PayloadManifest, InjectionResult,
    FoundationResult, FIRST_TRY_LIKELIHOOD, RISK_KEYWORDS, detect_risk_level,
)

# Per-learning outcome attribution: thread-local to pass learning IDs
# from section_1 generation → InjectionResult without changing function signatures
_injection_thread_local = threading.local()

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PROJECT_ROOT / "memory"

# =============================================================================
# PROFESSOR WISDOM IMPORT (Direct domain-specific wisdom access)
# =============================================================================
# Import DOMAIN_KEYWORDS and PROFESSOR_WISDOM for fast inline wisdom lookup
# This eliminates subprocess overhead in generate_section_2

_professor_wisdom_available = None
_domain_keywords = None
_professor_wisdom = None


def get_professor_wisdom_dicts():
    """Lazy load DOMAIN_KEYWORDS and PROFESSOR_WISDOM from professor.py."""
    global _professor_wisdom_available, _domain_keywords, _professor_wisdom

    if _professor_wisdom_available is None:
        try:
            from memory.professor import DOMAIN_KEYWORDS, PROFESSOR_WISDOM
            _domain_keywords = DOMAIN_KEYWORDS
            _professor_wisdom = PROFESSOR_WISDOM
            _professor_wisdom_available = True
        except ImportError:
            _professor_wisdom_available = False
            _domain_keywords = {}
            _professor_wisdom = {}

    return _domain_keywords, _professor_wisdom


def detect_domain_from_prompt(prompt: str) -> Optional[str]:
    """Detect the most relevant domain from a prompt using DOMAIN_KEYWORDS.

    Scoring rules:
    - Multi-word phrases (e.g. "github actions") score 1.5× (more specific)
    - Single words use word-boundary matching to avoid substring false positives
    - Tiebreak: prefer domain with fewer total keywords (more focused domain)
    """
    import re
    domain_keywords, _ = get_professor_wisdom_dicts()
    if not domain_keywords:
        return None

    prompt_lower = prompt.lower()

    scores = {}
    for domain, keywords in domain_keywords.items():
        score = 0.0
        for kw in keywords:
            if ' ' in kw:
                # Multi-word phrase: exact substring match, weighted higher
                if kw in prompt_lower:
                    score += 1.5
            else:
                # Single word: word boundary match (prevents "git" matching "digit")
                if re.search(r'\b' + re.escape(kw) + r'\b', prompt_lower):
                    score += 1.0
        if score > 0:
            scores[domain] = score

    if not scores:
        return None

    max_score = max(scores.values())
    tied = [d for d, s in scores.items() if s == max_score]
    if len(tied) == 1:
        return tied[0]
    # Tiebreak: fewer keywords = more specific domain
    return min(tied, key=lambda d: (len(domain_keywords[d]), d))


def get_domain_specific_wisdom(domain: str, depth: str = "full") -> Optional[str]:
    """Get wisdom for a specific domain directly from PROFESSOR_WISDOM.

    Args:
        domain: Domain key (e.g., "async_python", "docker_ecs")
        depth: "full", "summary", or "one_thing_only"

    Returns:
        Formatted wisdom string or None if domain not found
    """
    _, professor_wisdom = get_professor_wisdom_dicts()
    if not professor_wisdom or domain not in professor_wisdom:
        return None

    wisdom = professor_wisdom[domain]

    if depth == "one_thing_only":
        # Return just THE ONE THING
        the_one_thing = wisdom.get("the_one_thing", "").strip()
        if the_one_thing:
            return f"THE ONE THING: {the_one_thing}"
        return None

    # Build formatted wisdom
    lines = []

    # THE ONE THING (always first)
    the_one_thing = wisdom.get("the_one_thing", "").strip()
    if the_one_thing:
        lines.append("THE ONE THING:")
        lines.append(the_one_thing)
        lines.append("")

    # LANDMINES (critical warnings)
    landmines = wisdom.get("landmines", [])
    if landmines:
        lines.append("LANDMINES:")
        for mine in landmines:
            lines.append(f"  - {mine}")
        lines.append("")

    if depth == "summary":
        # Stop here for summary
        result = "\n".join(lines)
        return result[:500] + "..." if len(result) > 500 else result

    # THE PATTERN (for full depth)
    pattern = wisdom.get("pattern", "").strip()
    if pattern:
        lines.append("THE PATTERN:")
        lines.append(pattern)
        lines.append("")

    # CONTEXT
    context = wisdom.get("context", "").strip()
    if context:
        lines.append("CONTEXT:")
        lines.append(context)

    return "\n".join(lines) if lines else None


# =============================================================================
# SYNAPTIC VOICE INTEGRATION (Section 6: Holistic Context + Section 8: 8th Intelligence)
# =============================================================================
# These modules provide Synaptic's voice in the webhook injection.
# - Section 6: HOLISTIC_CONTEXT - Task-focused guidance for Atlas
# - Section 8: 8TH_INTELLIGENCE - Subconscious voice to Aaron (ALWAYS present)
# Communication protocols: context-dna/docs/FAMILY_GUIDELINES.md

_synaptic_voice_available = None
_dialogue_mirror_available = None
_challenge_detector_available = None


# =============================================================================
# TT2 (2026-05-12) — S6 ZSF observability (SS3 critical finding fix)
# =============================================================================
# `generate_section_6` walks three fallback paths
# (synaptic_deep_voice → butler_deep_query → get_synaptic_voice). Prior to
# this patch, each path swallowed its `ModuleNotFoundError` at DEBUG, returned
# None/"", and the section silently emitted empty content. No counter
# increment, no WARNING — exactly the ZSF violation CLAUDE.md forbids.
#
# Below: module-level per-path counters + a first-warning dedup set so we
# log loud once per path per process. After ALL three paths fail/empty,
# `all_paths_empty` increments and a single WARNING surfaces. Section may
# still return "" (empty content is acceptable; SILENT empty is not).
#
# The empty section_6 then lands in `section_results["section_6"] = ("", ...)`
# which already flows into `_pub_missing` at the publish hook — so
# /health.webhook.missing_sections_recent surfaces the gap downstream.
_S6_EMPTY_PATH_COUNTER = {
    "all_paths_empty": 0,
    "path_1_synaptic_deep_voice": 0,
    "path_2_butler_deep_query": 0,
    "path_3_get_synaptic_voice": 0,
}
_S6_EMPTY_PATH_LOCK = threading.Lock()
_S6_FIRST_WARNING_EMITTED: set = set()  # path keys we've already warned for


def get_s6_empty_path_counters() -> dict:
    """Return a snapshot of the S6 empty-path counters (observability).

    Tests + diagnostic tooling read this — do NOT mutate the returned dict.
    """
    with _S6_EMPTY_PATH_LOCK:
        return dict(_S6_EMPTY_PATH_COUNTER)


def reset_s6_empty_path_counters_for_test() -> None:
    """Tests only — reset counters + dedup set so each test starts clean."""
    with _S6_EMPTY_PATH_LOCK:
        for k in _S6_EMPTY_PATH_COUNTER:
            _S6_EMPTY_PATH_COUNTER[k] = 0
        _S6_FIRST_WARNING_EMITTED.clear()


def _bump_s6_counter(path_key: str, *, warn_msg: Optional[str] = None) -> None:
    """Increment a S6 empty-path counter and emit ONE WARNING per path key.

    Subsequent failures of the same path bump the counter at DEBUG only —
    the WARNING fires once per process to avoid log flooding while still
    making the first occurrence loud and greppable.
    """
    with _S6_EMPTY_PATH_LOCK:
        _S6_EMPTY_PATH_COUNTER[path_key] = _S6_EMPTY_PATH_COUNTER.get(path_key, 0) + 1
        first_time = path_key not in _S6_FIRST_WARNING_EMITTED
        if first_time:
            _S6_FIRST_WARNING_EMITTED.add(path_key)
    if first_time and warn_msg:
        try:
            logging.getLogger("context_dna").warning(warn_msg)
        except Exception:
            # ZSF: logger failure must not break the hot path. The counter
            # already bumped — observability preserved via /health.
            pass


def get_challenge_detector():
    """Get challenge detector module (lazy load with caching)."""
    global _challenge_detector_available
    if _challenge_detector_available is None:
        try:
            from memory.challenge_detector import detect_challenge, format_assist
            _challenge_detector_available = (detect_challenge, format_assist)
        except ImportError:
            _challenge_detector_available = False
    return _challenge_detector_available if _challenge_detector_available else None


def get_synaptic_voice():
    """Get Synaptic voice module (lazy load with caching)."""
    global _synaptic_voice_available
    if _synaptic_voice_available is None:
        try:
            from memory.synaptic_voice import get_voice, consult, speak
            _synaptic_voice_available = (get_voice, consult, speak)
        except ImportError as e:
            _synaptic_voice_available = False
    return _synaptic_voice_available if _synaptic_voice_available else None


def get_dialogue_mirror():
    """Get dialogue mirror module (lazy load with caching)."""
    global _dialogue_mirror_available
    if _dialogue_mirror_available is None:
        try:
            from memory.dialogue_mirror import get_dialogue_mirror as get_mirror
            _dialogue_mirror_available = get_mirror
        except ImportError:
            _dialogue_mirror_available = False
    return _dialogue_mirror_available if _dialogue_mirror_available else None

# =============================================================================
# GRACEFUL FALLBACK: Redis when available, File when not
# =============================================================================
# This pattern allows the hook to work in both scenarios:
# 1. Containers running → Use Redis (fast, synchronized, real-time)
# 2. Containers off → Use files (always works, local development)

_redis_available = None  # Cached status


def is_redis_available() -> bool:
    """Check if Redis is available (cached for performance)."""
    global _redis_available

    # Return cached result if checked recently (within same hook execution)
    if _redis_available is not None:
        return _redis_available

    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client:
            client.ping()
            _redis_available = True
            return True
    except Exception as e:
        print(f"[WARN] Redis availability check failed: {e}")

    _redis_available = False
    return False


def reset_redis_check():
    """Reset Redis availability check (call at start of new injection)."""
    global _redis_available
    _redis_available = None


# =============================================================================
# INTELLIGENT CACHING (Phase 1 Optimization)
# =============================================================================
# Cache slow-changing data to reduce redundant I/O and processing.
# TTL-based expiration ensures freshness without constant reloading.

from functools import lru_cache
import time

_cache_store = {}
_cache_timestamps = {}

# Cache TTLs (in seconds)
CACHE_TTL = {
    "brain_state": 30,        # Brain state changes slowly
    "pattern_evolution": 60,  # Patterns are relatively stable
    "hierarchy_profile": 120, # Profile rarely changes
    "section_5_static": 300,  # Protocol is mostly static
}


def get_cached(key: str, loader: Callable, ttl_key: str = None) -> Any:
    """Get cached value or load fresh if expired.

    Args:
        key: Cache key (unique identifier)
        loader: Callable to load fresh data
        ttl_key: TTL category key (defaults to 60s)

    Returns:
        Cached or freshly loaded data
    """
    global _cache_store, _cache_timestamps

    ttl = CACHE_TTL.get(ttl_key, 60) if ttl_key else 60
    now = time.time()

    # Check if cached and not expired
    if key in _cache_store and key in _cache_timestamps:
        if now - _cache_timestamps[key] < ttl:
            return _cache_store[key]

    # Load fresh data
    try:
        data = loader()
        _cache_store[key] = data
        _cache_timestamps[key] = now
        return data
    except Exception:
        # Return stale data if available, otherwise None
        return _cache_store.get(key)


def invalidate_cache(key: str = None):
    """Invalidate cache entry or all entries.

    Args:
        key: Specific key to invalidate, or None for all
    """
    global _cache_store, _cache_timestamps

    if key:
        _cache_store.pop(key, None)
        _cache_timestamps.pop(key, None)
    else:
        _cache_store.clear()
        _cache_timestamps.clear()


def get_brain_state_cached() -> str:
    """Get brain state content with intelligent caching."""
    def loader():
        brain_state_file = MEMORY_DIR / "brain_state.md"
        if brain_state_file.exists():
            return brain_state_file.read_text()
        return ""

    return get_cached("brain_state_content", loader, "brain_state")


def get_hierarchy_profile_cached() -> Dict:
    """Get hierarchy profile with intelligent caching."""
    def loader():
        profile_paths = [
            Path.home() / ".context-dna" / "hierarchy_profile.json",
            MEMORY_DIR.parent / ".context-dna" / "hierarchy_profile.json",
        ]
        for p in profile_paths:
            if p.exists():
                try:
                    return json.loads(p.read_text())
                except Exception:
                    continue
        return {}

    return get_cached("hierarchy_profile", loader, "hierarchy_profile")


# InjectionMode, RiskLevel, InjectionConfig, SOPEntry → memory.webhook_types


# Session failure tracking file
SESSION_FAILURE_FILE = MEMORY_DIR / ".session_failures.json"


def check_session_failures(session_id: Optional[str] = None) -> bool:
    """Check if current session has any failures recorded.

    Uses Redis when available for real-time sync, falls back to file storage.
    """
    # Try Redis first (if containers running)
    if is_redis_available():
        try:
            from memory.redis_cache import get_session_failure_count_redis
            count = get_session_failure_count_redis(session_id or "global")
            return count > 0
        except Exception:
            pass  # Fall through to file-based

    # File-based fallback (always works)
    if not SESSION_FAILURE_FILE.exists():
        return False

    try:
        with open(SESSION_FAILURE_FILE) as f:
            data = json.load(f)

        if session_id:
            return data.get(session_id, {}).get("has_failures", False)

        # Check for any recent failures (last hour)
        now = datetime.now().timestamp()
        for sid, info in data.items():
            if now - info.get("timestamp", 0) < 3600:  # Last hour
                if info.get("has_failures", False):
                    return True

        return False
    except Exception:
        return False


def record_session_failure(session_id: str, failure_type: str = "task_failed"):
    """Record a failure in the current session.

    Records to both Redis (if available) and file (always) for redundancy.
    Skips during write freeze (webhook still fires, state not persisted).
    """
    # Write freeze guard — skip persistence during migration drain
    try:
        from memory.write_freeze import get_write_freeze_guard
        if get_write_freeze_guard().is_frozen():
            _safe_log("warning", f"Write freeze active — skipping session failure record for {session_id}")
            return
    except Exception:
        pass  # Fail open
    # Try Redis first (non-blocking, for real-time dashboard updates)
    if is_redis_available():
        try:
            from memory.redis_cache import record_session_failure_redis
            record_session_failure_redis(session_id, failure_type)
        except Exception:
            pass  # Continue to file storage

    # Always write to file (guaranteed persistence)
    try:
        data = {}
        if SESSION_FAILURE_FILE.exists():
            with open(SESSION_FAILURE_FILE) as f:
                data = json.load(f)

        if session_id not in data:
            data[session_id] = {"has_failures": False, "failures": [], "timestamp": 0}

        data[session_id]["has_failures"] = True
        data[session_id]["failures"].append({
            "type": failure_type,
            "time": datetime.now().isoformat()
        })
        data[session_id]["timestamp"] = datetime.now().timestamp()

        with open(SESSION_FAILURE_FILE, "w") as f:
            json.dump(data, f)

    except Exception:
        pass  # Non-critical

    # Wire negative signal to evidence pipeline (outcome_event table)
    try:
        from memory.observability_store import get_observability_store
        store = get_observability_store()
        store.record_outcome_event(
            session_id=session_id,
            outcome_type='session_failure',
            success=False,
            reward=-0.3,
            notes=f'session_failure:{failure_type}'
        )
    except Exception:
        pass  # Non-critical - don't break failure recording


def clear_session_failures(session_id: str):
    """Clear failures for a session (call on session end or success).

    Clears from both Redis (if available) and file storage.
    Skips during write freeze.
    """
    # Write freeze guard — skip persistence during migration drain
    try:
        from memory.write_freeze import get_write_freeze_guard
        if get_write_freeze_guard().is_frozen():
            _safe_log("warning", f"Write freeze active — skipping clear_session_failures for {session_id}")
            return
    except Exception:
        pass  # Fail open

    # Try Redis first
    if is_redis_available():
        try:
            from memory.redis_cache import clear_session_failures_redis
            clear_session_failures_redis(session_id)
        except Exception as e:
            print(f"[WARN] Redis session failure clear failed: {e}")

    # Always clear from file
    try:
        if not SESSION_FAILURE_FILE.exists():
            return

        with open(SESSION_FAILURE_FILE) as f:
            data = json.load(f)

        if session_id in data:
            del data[session_id]

        with open(SESSION_FAILURE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[WARN] Session failure file clear failed: {e}")


# =============================================================================
# THREE-TIER VOLUME ESCALATION
# =============================================================================
# Tier 1: Silver Platter (~5KB, <500ms) - Default curated context
# Tier 2: Expanded Context (~15KB, ~1.5s) - After 1 failure OR <40% confidence
# Tier 3: Full Library Mode (~50KB, ~5s) - After 2+ failures OR <20% confidence
#         "Go to the textbook" - give agent maximum Acontext help


# VolumeEscalationTier → memory.webhook_types


def get_session_failure_count(session_id: Optional[str] = None) -> int:
    """Get actual failure count for session (not just boolean).

    This enables Three-Tier Volume Escalation:
    - 0 failures → Tier 1 (Silver Platter)
    - 1 failure → Tier 2 (Expanded)
    - 2+ failures → Tier 3 (Full Library / Textbook Mode)

    Storage hierarchy:
    1. PostgreSQL (cd_failures table) - PRIMARY when available
    2. Redis - Real-time sync when containers running
    3. File-based - Ultimate fallback for offline use
    """
    sid = session_id or "global"

    # Try PostgreSQL first (PRIMARY - uses cd_failures table)
    try:
        from memory.postgres_storage import get_failure_count
        count = get_failure_count(sid, hours=1)
        if count > 0 or _postgres_is_available():
            return count
    except ImportError:
        pass  # postgres_storage not available
    except Exception:
        pass  # Fall through to Redis

    # Try Redis second (if containers running)
    if is_redis_available():
        try:
            from memory.redis_cache import get_session_failure_count_redis
            return get_session_failure_count_redis(sid)
        except Exception:
            pass  # Fall through to file-based

    # File-based fallback (always available)
    if not SESSION_FAILURE_FILE.exists():
        return 0

    try:
        with open(SESSION_FAILURE_FILE) as f:
            data = json.load(f)

        if session_id:
            session_data = data.get(session_id, {})
            return len(session_data.get("failures", []))

        # Check for any recent failures (last hour) and sum them
        now = datetime.now().timestamp()
        total_failures = 0
        for sid, info in data.items():
            if now - info.get("timestamp", 0) < 3600:  # Last hour
                total_failures += len(info.get("failures", []))

        return total_failures
    except Exception:
        return 0


def _postgres_is_available() -> bool:
    """Check if PostgreSQL is available for failure tracking."""
    try:
        from memory.postgres_storage import get_connection, release_connection
        conn = get_connection()
        if conn:
            release_connection(conn)  # CRITICAL: Return connection to pool
            return True
    except Exception as e:
        print(f"[WARN] PostgreSQL availability check failed: {e}")
    return False


def determine_volume_tier(
    failure_count: int,
    first_try_likelihood: int,
    force_tier: Optional[int] = None
) -> VolumeEscalationTier:
    """Determine which volume tier to use based on failures and confidence.

    Tier Logic:
    - Tier 1 (Silver Platter): 0 failures AND >40% confidence
    - Tier 2 (Expanded): 1 failure OR confidence 20-40%
    - Tier 3 (Full Library): 2+ failures OR <20% confidence

    Args:
        failure_count: Number of failures in current session
        first_try_likelihood: Expected success rate (0-100)
        force_tier: Force specific tier (for testing)

    Returns:
        VolumeEscalationTier enum value
    """
    if force_tier is not None:
        return VolumeEscalationTier(force_tier)

    # Tier 3: Full Library (2+ failures OR <20% confidence)
    if failure_count >= 2 or first_try_likelihood < 20:
        return VolumeEscalationTier.FULL_LIBRARY

    # Tier 2: Expanded (1 failure OR 20-40% confidence)
    if failure_count == 1 or first_try_likelihood < 40:
        return VolumeEscalationTier.EXPANDED

    # Tier 1: Silver Platter (default)
    return VolumeEscalationTier.SILVER_PLATTER


def get_full_library_context(prompt: str, limit: int = 20) -> str:
    """Get extensive Acontext context for "textbook mode".

    This is the fallback when agent has struggled (2+ failures).
    Pulls significantly more context from Acontext to give agent
    maximum help - the equivalent of "going to the textbook".

    PERFORMANCE:
    - Hard timeout: 10 seconds total
    - Method 1 (Context DNA): 5s timeout with early exit at 10+ SOPs
    - Methods 2-4: Parallel execution with 5s timeout

    Args:
        prompt: User's prompt for semantic search
        limit: How many results to pull (default 20 for full library)

    Returns:
        Formatted textbook context string
    """
    import concurrent.futures
    import logging

    logger = logging.getLogger('context_dna')

    lines = []
    lines.append("📚 FULL LIBRARY MODE (2+ failures detected)")
    lines.append("═" * 50)
    lines.append("Providing extensive context to help you succeed.")
    lines.append("")

    # Try multiple sources for comprehensive context
    sops_found = []

    EARLY_EXIT_THRESHOLD = 10  # Stop if we have 10+ SOPs
    TIMEOUT_TOTAL = 10  # Hard timeout for entire section (seconds)
    TIMEOUT_PRIMARY = 5  # Timeout for Method 1 (seconds)
    TIMEOUT_FALLBACKS = 5  # Timeout for parallel fallbacks (seconds)

    # Method 1: Context DNA client (PRIMARY - gets 5s timeout)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_fetch_context_dna_sops, prompt, limit)
            try:
                sops_found = future.result(timeout=TIMEOUT_PRIMARY)
                logger.debug(f"Section 7: Method 1 returned {len(sops_found)} SOPs in <{TIMEOUT_PRIMARY}s")
            except concurrent.futures.TimeoutError:
                logger.warning(f"Section 7: Method 1 (Context DNA) timed out after {TIMEOUT_PRIMARY}s")
    except Exception as e:
        logger.warning(f"Section 7: Method 1 failed: {e}")

    # Early exit if we have enough results
    if len(sops_found) >= EARLY_EXIT_THRESHOLD:
        logger.debug(f"Section 7: Early exit with {len(sops_found)} SOPs (threshold: {EARLY_EXIT_THRESHOLD})")
        return _format_library_results(lines, sops_found, limit)

    # Methods 2-4: Run remaining methods in PARALLEL (get remaining 5s)
    fallback_results = {"lines": [], "sops": []}

    def run_method_2():
        """Direct query.py subprocess fallback."""
        method_lines = []
        try:
            result = subprocess.run(
                ["python3", str(MEMORY_DIR / "query.py"), prompt],
                capture_output=True,
                text=True,
                timeout=4  # Leave 1s buffer
            )
            if result.returncode == 0 and result.stdout.strip():
                method_lines.append("  RELEVANT LEARNINGS FROM ACONTEXT:")
                method_lines.append("  " + "-" * 40)
                for line in result.stdout.strip().split("\n")[:30]:
                    method_lines.append(f"    {line}")
                method_lines.append("")
        except Exception as e:
            _safe_log('debug', f"Method 2 failed: {e}")
        return method_lines

    def run_method_3():
        """Knowledge graph context."""
        method_lines = []
        try:
            from memory.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph()
            full_ctx = kg.get_full_context(prompt, "critical")
            if full_ctx.get("all_paths"):
                method_lines.append("  KNOWLEDGE GRAPH PATHS:")
                method_lines.append("  " + "-" * 40)
                for path in full_ctx["all_paths"][:10]:
                    method_lines.append(f"    → {path}")
                method_lines.append("")
        except Exception as e:
            _safe_log('debug', f"Method 3 failed: {e}")
        return method_lines

    def run_method_4():
        """Brain state (full content in textbook mode)."""
        method_lines = []
        try:
            brain_state_file = MEMORY_DIR / "brain_state.md"
            if brain_state_file.exists():
                content = brain_state_file.read_text()
                if content.strip():
                    method_lines.append("  BRAIN STATE (Full):")
                    method_lines.append("  " + "-" * 40)
                    for line in content.split("\n")[:40]:  # More lines in textbook mode
                        method_lines.append(f"    {line}")
                    method_lines.append("")
        except Exception as e:
            _safe_log('debug', f"Method 4 failed: {e}")
        return method_lines

    # Execute fallback methods in parallel
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(run_method_2): "method_2",
                executor.submit(run_method_3): "method_3",
                executor.submit(run_method_4): "method_4"
            }

            for future in concurrent.futures.as_completed(futures, timeout=TIMEOUT_FALLBACKS):
                try:
                    result_lines = future.result()
                    if result_lines:
                        fallback_results["lines"].extend(result_lines)
                        logger.debug(f"Section 7: {futures[future]} contributed {len(result_lines)} lines")
                except Exception as e:
                    logger.debug(f"Section 7: Fallback method {futures[future]} error: {e}")

    except concurrent.futures.TimeoutError:
        logger.warning(f"Section 7: Parallel fallbacks timed out after {TIMEOUT_FALLBACKS}s")
    except Exception as e:
        logger.warning(f"Section 7: Parallel fallbacks error: {e}")

    # Merge fallback results
    lines.extend(fallback_results["lines"])

    return _format_library_results(lines, sops_found, limit)



def _fetch_context_dna_sops(prompt: str, limit: int) -> list:
    """Fetch SOPs from Context DNA client (isolated for timeout wrapper)."""
    sops_found = []
    try:
        from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
        if CONTEXT_DNA_AVAILABLE:
            client = ContextDNAClient()
            results = client.get_relevant_learnings(prompt, limit=limit)
            for r in results:
                if r.get("title") and r.get("content"):
                    sops_found.append({
                        "title": r["title"],
                        "content": r["content"][:1200],  # Truncate for webhook payload size
                        "source": "context_dna"
                    })
    except Exception as e:
        _safe_log('warning', f"Context DNA SOP query failed: {e}")
    return sops_found



def _format_library_results(lines: list, sops_found: list, limit: int) -> str:
    """Format library results with SOPs."""
    # Add found SOPs with full content
    if sops_found:
        lines.append("  DETAILED SOPs FOR THIS TASK:")
        lines.append("  " + "-" * 40)
        for i, sop in enumerate(sops_found[:15], 1):  # Up to 15 SOPs in textbook mode
            lines.append(f"    {i}. {sop['title']}")
            if sop.get("content"):
                for cline in sop["content"].split("\n")[:8]:
                    lines.append(f"       {cline[:100]}")
            lines.append("")

    lines.append("  [End Full Library Mode - You have all available context]")

    return "\n".join(lines) if len(lines) > 5 else ""

# InjectionResult, ExclusionReason, ManifestRef, PayloadManifest → memory.webhook_types


def _emit_manifest(manifest: PayloadManifest):
    """Write manifest to .projectdna/derived/last_manifest.json. Non-blocking, fail-open."""
    try:
        vault_dir = Path(__file__).parent.parent / ".projectdna" / "derived"
        if not vault_dir.exists():
            vault_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = vault_dir / "last_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2, separators=(",", ": ")) + "\n"
        )

        # Log event via EventedWriteService (chain-hashed audit)
        try:
            from memory.evented_write import EventedWriteService
            ews = EventedWriteService.get_instance()
            ews._emit_event(
                store_name="payload_manifest",
                method_name="emit",
                summary={
                    "injection_id": manifest.injection_id,
                    "injection_count": manifest.injection_count,
                    "depth": manifest.depth,
                    "included_count": len(manifest.included),
                    "excluded_count": len(manifest.excluded),
                    "total_tokens_est": manifest.total_tokens_est,
                    "movement": 3,
                },
            )
        except Exception:
            pass  # Fail-open: manifest file already written
    except Exception as e:
        _safe_log('debug', f"Manifest emit failed (non-blocking): {e}")


def _record_quality_metrics(manifest: PayloadManifest, generation_time_ms: int, content_len: int):
    """Record injection quality metrics to Redis. Non-blocking, fail-open.

    Tracks three metric families:
    1. Payload size distribution (chars + estimated tokens per injection)
    2. Section fill rates (how often each section has content vs empty/failed)
    3. Response latency (generation time per request)

    All stored as Redis sorted sets with timestamp scores for time-windowed queries.
    Keys: injection:metrics:{metric_name} — 500-entry rolling window.
    """
    try:
        if not is_redis_available():
            return
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        now_ts = time.time()
        pipe = r.pipeline(transaction=False)

        # 1. Payload size (chars + tokens)
        pipe.zadd("injection:metrics:payload_chars", {f"{manifest.injection_id}|{content_len}": now_ts})
        pipe.zadd("injection:metrics:payload_tokens", {f"{manifest.injection_id}|{manifest.total_tokens_est}": now_ts})

        # 2. Section fill rates — record each section's outcome
        _all_sections = ["section_0", "section_1", "section_2", "section_3",
                         "section_4", "section_5", "section_6", "section_7",
                         "section_8", "section_10"]
        included_keys = {ref.section for ref in manifest.included}
        for sec in _all_sections:
            status = "filled" if sec in included_keys else "empty"
            pipe.zadd(f"injection:metrics:fill:{sec}", {f"{manifest.injection_id}|{status}": now_ts})
            # Trim to rolling window
            pipe.zremrangebyrank(f"injection:metrics:fill:{sec}", 0, -501)

        # 3. Latency
        pipe.zadd("injection:metrics:latency_ms", {f"{manifest.injection_id}|{generation_time_ms}": now_ts})

        # 4. Risk level distribution
        pipe.zadd("injection:metrics:risk_level", {f"{manifest.injection_id}|{manifest.risk_level}": now_ts})

        # 5. Depth distribution (FULL vs ABBREV)
        pipe.zadd("injection:metrics:depth", {f"{manifest.injection_id}|{manifest.depth}": now_ts})

        # Trim rolling windows (keep last 500)
        for key in ["injection:metrics:payload_chars", "injection:metrics:payload_tokens",
                     "injection:metrics:latency_ms", "injection:metrics:risk_level",
                     "injection:metrics:depth"]:
            pipe.zremrangebyrank(key, 0, -501)

        pipe.execute()
    except Exception as e:
        _safe_log('debug', f"Quality metrics recording failed (non-blocking): {e}")


# =============================================================================
# RISK DETECTION (From auto-memory-query.sh)
# =============================================================================

# RISK_KEYWORDS, FIRST_TRY_LIKELIHOOD, detect_risk_level → memory.webhook_types


# =============================================================================
# SECTION 0: SAFETY LAYER (🔒 LOCKED)
# =============================================================================

def get_never_do_warnings() -> List[str]:
    """Get NEVER DO prohibitions. Always included."""
    # These are hardcoded safety rails
    return [
        "NEVER commit secrets (.env, API keys, credentials)",
        "NEVER force push to main/master without explicit permission",
        "NEVER delete production data without backup confirmation",
        "NEVER skip reading code before modifying it",
    ]


def get_critical_gotcha(prompt: str) -> Optional[str]:
    """Get the single most relevant gotcha for this prompt.

    PERFORMANCE: Uses local pattern matching first (instant).
    Only falls back to agent_service query if no local match.
    This avoids the 0.5s+ import overhead for common patterns.
    """
    prompt_lower = prompt.lower()

    # LOCAL PATTERNS (instant - no imports needed)
    # These are the most common gotchas - check them first
    if "docker" in prompt_lower and ("restart" in prompt_lower or "env" in prompt_lower):
        return "⚠️ Docker restart doesn't reload env vars - must recreate container"
    if "async" in prompt_lower and "boto" in prompt_lower:
        return "⚠️ boto3 is synchronous - wrap in asyncio.to_thread()"
    if "webrtc" in prompt_lower and "cloudflare" in prompt_lower:
        return "⚠️ WebRTC needs direct UDP - set Cloudflare DNS proxied=false"
    if "gpu" in prompt_lower and ("ip" in prompt_lower or "asg" in prompt_lower):
        return "⚠️ ASG gives new private IP on restart - use Internal NLB"
    if "livekit" in prompt_lower and "turn" in prompt_lower:
        return "⚠️ LiveKit TURN needs UDP ports 443,3478,5349 open"
    if "redis" in prompt_lower and "password" in prompt_lower:
        return "⚠️ Redis password must be URL-encoded if it contains special chars"
    if "celery" in prompt_lower and ("retry" in prompt_lower or "timeout" in prompt_lower):
        return "⚠️ Celery retries can cause 20s+ delays - use fire_and_forget for non-critical tasks"
    if "webhook" in prompt_lower and ("slow" in prompt_lower or "timeout" in prompt_lower):
        return "⚠️ Webhook generation bottlenecks: subprocess calls, database queries, Celery retries"

    # No local match - skip agent_service query for performance
    # The overhead of importing agent_service (~0.5s) isn't worth it for gotcha lookup
    return None


def get_high_confidence_landmines(prompt: str, limit: int = 3) -> List[str]:
    """High-confidence failure patterns as hard safety constraints.

    Criteria: confidence >= 0.8 AND (occurrence_count >= 2 OR hindsight source).
    Follows get_mansion_warnings() pattern: query DB, filter by relevance, format.
    """
    try:
        from memory.failure_pattern_analyzer import get_failure_pattern_analyzer
        analyzer = get_failure_pattern_analyzer()
        if not analyzer:
            return []

        patterns = analyzer.get_high_yield_failures(limit=limit * 3)
        prompt_lower = (prompt or "").lower()
        prompt_words = set(re.findall(r'\b[a-z]{3,}\b', prompt_lower))

        landmines = []
        for p in patterns:
            if p.confidence < 0.8:
                continue
            sources = p.sources if isinstance(p.sources, list) else []
            if p.occurrence_count < 2 and 'hindsight_validator' not in sources:
                continue
            # Relevance: 2+ keyword overlap OR direct keyword match
            pattern_words = set(p.keywords) if p.keywords else set()
            overlap = len(pattern_words & prompt_words)
            direct_match = any(kw in prompt_lower for kw in list(p.keywords or [])[:3])
            if overlap >= 2 or direct_match:
                landmines.append(p.landmine_text[:150])
            if len(landmines) >= limit:
                break
        return landmines
    except Exception:
        return []


def generate_section_0(prompt: str, config: InjectionConfig) -> str:
    """Generate SAFETY section. Cannot be disabled."""
    lines = []

    # Header
    emoji = "🚫" if config.emoji_enabled else ""
    lines.append(f"{emoji} NEVER DO (Safety Rails)")
    lines.append("─" * 40)

    # Never do warnings
    for warning in get_never_do_warnings():
        lines.append(f"  • {warning}")

    # Critical gotcha
    gotcha = get_critical_gotcha(prompt)
    if gotcha:
        lines.append("")
        emoji = "⚠️" if config.emoji_enabled else "!"
        lines.append(f"{emoji} CRITICAL: {gotcha}")

    # Evidence-backed failure landmines (high confidence, 2+ occurrences)
    landmines = get_high_confidence_landmines(prompt, limit=3)
    if landmines:
        lines.append("")
        lines.append(f"  {'💣' if config.emoji_enabled else '!!'} EVIDENCE-BACKED LANDMINES:")
        for mine in landmines:
            lines.append(f"    • {mine}")

    # Superhero anticipation readiness — escalation ladder (L1→L2 in S0)
    try:
        from memory.anticipation_engine import is_superhero_active, get_superhero_cache
        if is_superhero_active():
            cached = get_superhero_cache()
            task = cached.get("_task", "unknown task") if cached else "unknown"
            artifact_count = len([k for k in (cached or {}) if not k.startswith("_")])
            activated_at = cached.get("_activated_at", "") if cached else ""
            hero_icon = "\U0001f9b8" if config.emoji_enabled else ">>"

            # Calculate elapsed time since activation for escalation
            elapsed_s = 0
            if activated_at:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(activated_at)
                    elapsed_s = (datetime.utcnow() - dt).total_seconds()
                except Exception:
                    pass

            lines.append("")
            if elapsed_s > 300:
                # L2: Elevated — 5+ minutes unacknowledged, inject as critical-level
                lines.append(f"  {hero_icon} UNACKNOWLEDGED SUPERHERO: {task}.")
                lines.append(f"    {artifact_count}/4 artifacts cached for {int(elapsed_s)}s. Agents WILL benefit from 'engage superhero mode'.")
                lines.append(f"    Pre-computed: mission briefing, gotcha synthesis, architecture map, failure brief.")
            else:
                # L1: Standard notification
                lines.append(f"  {hero_icon} SUPERHERO READY: {task}. {artifact_count}/4 artifacts pre-computed for 10+ agents. Engage superhero mode.")
    except Exception:
        pass  # Never fail safety section

    # Critical + architectural findings from session gold passes
    try:
        from memory.session_gold_passes import get_critical_findings
        critical = get_critical_findings()
        if critical:
            # Split by severity
            infra_criticals = [c for c in critical if c.get("severity", "critical") == "critical"]
            arch_findings = [c for c in critical if c.get("severity") == "architectural"]

            if infra_criticals:
                lines.append("")
                lines.append(f"  {'🚨' if config.emoji_enabled else '!!'} CRITICAL INFRASTRUCTURE FINDINGS:")
                for cf in infra_criticals:
                    pass_id = cf.get("pass", cf.get("pass_id", "?"))
                    finding = cf.get("finding", "")
                    lines.append(f"    • [{pass_id}] {finding}")

            if arch_findings:
                lines.append("")
                lines.append(f"  {'🏗️' if config.emoji_enabled else '!!'} ARCHITECTURAL FINDINGS (structural gold — research + present options):")
                lines.append(f"    DO NOT SKIP: These are verified structural patterns that build the mansion.")
                lines.append(f"    For each: (1) research the ideal solution, (2) present options to Aaron.")
                for af in arch_findings:
                    pass_id = af.get("pass", af.get("pass_id", "?"))
                    finding = af.get("finding", "")
                    lines.append(f"    • [{pass_id}] {finding}")
    except Exception:
        pass  # Never fail safety section

    return "\n".join(lines)


# =============================================================================
# QUALITY SCORE HELPERS
# =============================================================================

def _lookup_quality_scores(learning_ids: list) -> dict:
    """Fetch quality_score from learnings.metadata for a list of IDs.
    Returns dict mapping learning_id → quality_score (0-1 float).
    Missing/unscored entries are omitted (caller defaults to 0.5).
    """
    if not learning_ids:
        return {}
    try:
        from memory.sqlite_storage import get_sqlite_storage
        import json as _json
        storage = get_sqlite_storage()
        # Batch query — SQLite supports up to 999 params, we'll never exceed that
        placeholders = ','.join('?' for _ in learning_ids)
        rows = storage.conn.execute(
            f"SELECT id, metadata FROM learnings WHERE id IN ({placeholders})",
            learning_ids
        ).fetchall()
        scores = {}
        for row in rows:
            try:
                meta = _json.loads(row["metadata"] or "{}")
                qs = meta.get("quality_score")
                if qs is not None:
                    scores[row["id"]] = float(qs)
            except (ValueError, TypeError, KeyError):
                continue
        return scores
    except Exception as e:
        _safe_log("debug", f"Quality score lookup: {e}")
        return {}


# =============================================================================
# FRESHNESS SCORE HELPERS (project-scoped recency decay)
# =============================================================================

def _get_active_projects() -> set:
    """Read focus.json active array. Empty set on any error (safe: no decay)."""
    try:
        focus_path = os.path.join(os.path.dirname(__file__), '..', '.claude', 'focus.json')
        with open(focus_path) as f:
            data = json.load(f)
        return {p.lower() for p in data.get('active', [])}
    except Exception:
        return set()


def _learning_matches_any_project(learning: dict, projects: set) -> bool:
    """Check if learning belongs to any active project via tags/source/title."""
    if not projects:
        return False
    searchable = ' '.join([
        ' '.join(learning.get('tags', [])),
        learning.get('source', ''),
        learning.get('title', ''),
    ]).lower()
    return any(proj in searchable for proj in projects)


def _lookup_freshness_scores(learnings: list) -> dict:
    """Compute freshness score per learning.

    Dormant/unknown project learnings = 1.0 (frozen, no decay).
    Active project learnings = exponential decay with 14-day half-life, 0.3 floor.
    """
    HALF_LIFE_DAYS = 14
    FLOOR = 0.3
    active_projects = _get_active_projects()
    now = datetime.now(timezone.utc)
    scores = {}
    for l in learnings:
        lid = l.get('id', '')
        ts = l.get('updated_at') or l.get('created_at')
        if not ts or not _learning_matches_any_project(l, active_projects):
            scores[lid] = 1.0  # dormant or unknown → frozen
            continue
        try:
            created = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            age_days = max(0, (now - created).total_seconds() / 86400)
            scores[lid] = max(FLOOR, 0.5 ** (age_days / HALF_LIFE_DAYS))
        except Exception:
            scores[lid] = 1.0
    return scores


# =============================================================================
# REDIS CACHE HELPERS
# =============================================================================

def _make_cache_key(prefix: str, prompt: str, risk_level: str, version: int = 1) -> str:
    """
    Generate cache key for context pack sections.

    Args:
        prefix: Section prefix (e.g., "s1", "s4")
        prompt: User prompt (hashed for key stability)
        risk_level: Risk level string for differentiation
        version: Cache version for invalidation

    Returns:
        Redis key like "contextdna:s1:abc123:high:v1"
    """
    # Hash first 100 chars of prompt for stable key
    prompt_hash = hashlib.md5(prompt[:100].encode()).hexdigest()[:12]
    return f"contextdna:{prefix}:{prompt_hash}:{risk_level}:v{version}"


# =============================================================================
# SECTION 1: FOUNDATION
# =============================================================================

def get_exact_file(prompt: str) -> Optional[str]:
    """Get THE EXACT FILE Atlas needs first."""
    try:
        from memory.agent_service import _get_the_exact_file
        return _get_the_exact_file(prompt)
    except ImportError:
        return None


# FoundationResult → memory.webhook_types


def get_foundation_sops(prompt: str, limit: int = 3, boundary_decision=None) -> FoundationResult:
    """
    Get relevant SOPs (bug-fix AND process) with full content.

    Args:
        prompt: User's prompt for semantic search
        limit: Maximum SOPs to return
        boundary_decision: Optional BoundaryDecision for project filtering

    Returns FoundationResult with SOPs and any roadblocks encountered.
    """
    sops = []
    roadblocks = []

    try:
        # Try acontext_helper for full SOP data
        try:
            from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
        except ImportError:
            from context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
        memory = ContextDNAClient()

        # Fetch more if filtering, to have enough after filter
        fetch_limit = limit + 5 if boundary_decision and boundary_decision.should_filter else limit + 2
        learnings = memory.get_relevant_learnings(prompt, limit=fetch_limit, debug=False)

        if not learnings:
            roadblocks.append(f"No learnings matched query: '{prompt[:50]}...' - memory may need seeding")

        # Apply boundary filtering if decision says to filter
        if boundary_decision and boundary_decision.should_filter and learnings:
            try:
                from memory.boundary_intelligence import BoundaryIntelligence
                bi = BoundaryIntelligence(use_llm=False)
                learnings = bi.filter_learnings(learnings, boundary_decision)

                if not learnings:
                    roadblocks.append(f"All learnings filtered (project: {boundary_decision.primary_project})")
            except Exception as e:
                roadblocks.append(f"Boundary filtering failed: {str(e)[:50]}")

        # Enrich with quality + freshness scores and re-rank
        _quality_scores = _lookup_quality_scores(
            [l.get('id', '') for l in learnings if l.get('id')]
        )
        _freshness_scores = _lookup_freshness_scores(learnings)
        for l in learnings:
            lid = l.get('id', '')
            qs = _quality_scores.get(lid, 0.5)  # Default 0.5 = unscored
            fs = _freshness_scores.get(lid, 1.0)  # Default 1.0 = no decay
            relevance = 1 - l.get('distance', 0.5)
            # Composite: 55% relevance + 35% quality + 10% freshness
            # Freshness: dormant projects frozen (1.0), active decay (14d half-life, 0.3 floor)
            l['_composite'] = (0.55 * relevance) + (0.35 * qs) + (0.10 * fs)
        learnings.sort(key=lambda l: l.get('_composite', 0), reverse=True)

        learning_ids = []
        for learning in learnings[:limit]:
            sop = SOPEntry(
                title=learning.get('title', ''),
                content=learning.get('preferences', learning.get('content', '')),
                sop_type=learning.get('type', 'process'),
                use_when=learning.get('use_when', ''),
                relevance=1 - learning.get('distance', 0.5)
            )
            if sop.title:
                sops.append(sop)
                # Track learning ID for per-learning outcome attribution
                lid = learning.get('id', '')
                if lid:
                    learning_ids.append(lid)

    except ImportError as e:
        learning_ids = []
        roadblocks.append(f"Could not import ContextDNAClient: {e}")
        # Fallback to agent_service
        try:
            from memory.agent_service import _get_relevant_sops
            raw_sops = _get_relevant_sops(prompt, limit)

            for category, items in raw_sops.items():
                for item in items[:limit]:
                    # Parse item string "title | When: ... | Key: ..."
                    parts = item.split(" | ")
                    sop = SOPEntry(
                        title=parts[0] if parts else item,
                        content=parts[2].replace("Key: ", "") if len(parts) > 2 else "",
                        sop_type=category,
                        use_when=parts[1].replace("When: ", "") if len(parts) > 1 else ""
                    )
                    sops.append(sop)
        except ImportError:
            roadblocks.append("Fallback agent_service also unavailable")
    except Exception as e:
        learning_ids = []
        roadblocks.append(f"Error fetching SOPs: {str(e)[:100]}")

    return FoundationResult(sops=sops, roadblocks=roadblocks,
                            learning_ids=learning_ids if 'learning_ids' in dir() else [])


def get_foundation_sops_legacy(prompt: str, limit: int = 3) -> Dict[str, List[str]]:
    """Legacy: Get relevant SOPs as string lists."""
    try:
        from memory.agent_service import _get_relevant_sops
        return _get_relevant_sops(prompt, limit)
    except ImportError:
        try:
            from memory.query import query_learnings
            results = query_learnings(prompt, limit=limit)
            return {"combined": results}
        except ImportError:
            return {}


def get_successful_chain(prompt: str) -> Optional[str]:
    """Get what worked before for similar tasks."""
    try:
        from memory.agent_service import _get_successful_chain
        return _get_successful_chain(prompt)
    except ImportError:
        return None


def generate_section_1(prompt: str, risk_level: RiskLevel, config: InjectionConfig,
                       session_id: Optional[str] = None, boundary_decision=None) -> str:
    """
    Generate FOUNDATION section with smart SOP reading logic.

    SOP Reading Rules:
    - Always show SOP titles
    - If first-try >= 90% (LOW risk) AND no failures → "May skip reading"
    - If first-try < 90% OR session has failures → "MUST READ" with full content

    Redis Caching:
    - Caches complete section result for 5 minutes
    - Cache key includes prompt hash + risk level
    - Falls through to normal generation if Redis unavailable
    """
    if not config.section_1_enabled:
        return ""

    # =================================================================
    # REDIS CACHE CHECK (Fast-path: <1ms vs 20-100ms for generation)
    # =================================================================
    try:
        from memory.redis_cache import get_cached_section_content, cache_section_content

        # Sanitize prompt early for cache key consistency
        clean_prompt = _sanitize_prompt_for_query(prompt)

        # Guard: empty sanitized prompts (e.g. only IDE tags) — skip cache, skip generation
        if not clean_prompt or len(clean_prompt.strip()) < 3:
            return ""

        cache_key = _make_cache_key("s1", clean_prompt, risk_level.value)

        cached = get_cached_section_content(cache_key)
        if cached is not None and isinstance(cached, str) and len(cached) > 0:
            # Cache hit - return immediately (explicit None check avoids falsy-string bug)
            return cached
    except Exception as e:
        # Redis unavailable - log and continue with normal generation
        logging.getLogger('context_dna').debug(f"Section 1 cache lookup failed: {e}")

    # =================================================================
    # CACHE MISS - Generate section normally
    # =================================================================
    lines = []

    # Sanitize prompt - remove IDE tags that pollute queries
    clean_prompt = _sanitize_prompt_for_query(prompt)

    # Header
    emoji = "📁" if config.emoji_enabled else ""
    lines.append(f"{emoji} FOUNDATION")
    lines.append("─" * 40)

    # The exact file
    exact_file = get_exact_file(clean_prompt)
    if exact_file:
        lines.append(f"  START HERE: {exact_file}")

    # Successful chain
    chain = get_successful_chain(clean_prompt)
    if chain:
        lines.append(f"  WORKED BEFORE: {chain}")

    # Determine SOP reading requirement - 3-TIER LOGIC
    # TIER 1: >90% first-try → titles only, discretionary (may skip)
    # TIER 2: 80-90% first-try → titles only, MUST READ
    # TIER 3: <80% first-try OR session failures → full content, MUST READ
    session_has_failures = config.session_has_failures or check_session_failures(session_id)
    first_try_int = int(FIRST_TRY_LIKELIHOOD[risk_level].replace("%", ""))

    # Determine tier
    if session_has_failures or first_try_int < 80:
        sop_tier = 3  # Full content, MUST READ
    elif first_try_int <= 90:
        sop_tier = 2  # Titles only, MUST READ
    else:
        sop_tier = 1  # Titles only, discretionary

    # Foundation SOPs (with boundary filtering if applicable)
    foundation_result = get_foundation_sops(clean_prompt, config.sop_count, boundary_decision)
    sops = foundation_result.sops
    roadblocks = foundation_result.roadblocks
    # Store learning IDs for per-learning outcome attribution
    _injection_thread_local.learning_ids = foundation_result.learning_ids

    if sops:
        lines.append("")
        emoji = "📋" if config.emoji_enabled else ""

        if sop_tier == 3:
            # TIER 3: Full content, MUST READ (<80% or failures)
            indicator = "🔴 MUST READ" if config.emoji_enabled else "[MUST READ]"
            reason = "(session has failures)" if session_has_failures else f"({first_try_int}% first-try)"
            lines.append(f"  {emoji} FOUNDATION SOPs {indicator} {reason}:")
            lines.append("")

            for i, sop in enumerate(sops, 1):
                lines.append(f"    {i}. [{sop.sop_type.upper()}] {sop.title}")
                if sop.use_when:
                    lines.append(f"       When: {sop.use_when[:100]}")
                if sop.content:
                    # Show full content for MUST READ
                    content_lines = sop.content.split('\n')
                    for cline in content_lines[:5]:  # Max 5 lines per SOP
                        lines.append(f"       → {cline[:120]}")
                if sop.relevance > 0:
                    lines.append(f"       (relevance: {sop.relevance:.0%})")
                lines.append("")

        elif sop_tier == 2:
            # TIER 2: Titles only, MUST READ (80-90%)
            indicator = "🟡 MUST READ" if config.emoji_enabled else "[MUST READ]"
            lines.append(f"  {emoji} FOUNDATION SOPs {indicator} ({first_try_int}% first-try):")
            lines.append("  (Read these SOPs before proceeding)")
            lines.append("")

            for i, sop in enumerate(sops, 1):
                # Titles only but MUST READ
                lines.append(f"    {i}. [{sop.sop_type.upper()}] {sop.title}")
                if sop.use_when:
                    lines.append(f"       When: {sop.use_when[:80]}...")

        else:
            # TIER 1: Titles only, discretionary (>90%)
            indicator = "🟢 May skip" if config.emoji_enabled else "[Optional]"
            lines.append(f"  {emoji} FOUNDATION SOPs {indicator} ({first_try_int}%+ first-try):")
            lines.append("  (Read if unsure or if task fails)")
            lines.append("")

            for i, sop in enumerate(sops, 1):
                # Just title and type
                lines.append(f"    {i}. [{sop.sop_type.upper()}] {sop.title}")
                if sop.use_when:
                    lines.append(f"       When: {sop.use_when[:60]}...")
    elif roadblocks:
        # Report roadblocks when no SOPs found
        lines.append("")
        emoji = "⚠️" if config.emoji_enabled else "[!]"
        lines.append(f"  {emoji} FOUNDATION ROADBLOCKS:")
        for rb in roadblocks[:3]:
            lines.append(f"    • {rb}")

    # =================================================================
    # CACHE RESULT (Store for 5 min before returning)
    # =================================================================
    result = "\n".join(lines) if len(lines) > 2 else ""
    if result:
        try:
            from memory.redis_cache import cache_context_pack
            cache_section_content(cache_key, result, ttl=300)  # 5 min TTL
        except Exception as e:
            # Non-blocking - cache write failures don't affect generation
            logging.getLogger('context_dna').debug(f"Section 1 cache write failed: {e}")

    return result


# =============================================================================
# SECTION 2: WISDOM
# =============================================================================

def get_professor_wisdom(prompt: str, depth: str = "full") -> Optional[str]:
    """Get Professor wisdom (THE ONE THING, LANDMINES, THE PATTERN).

    LLM-FIRST ARCHITECTURE:
    - PRIMARY: LLM generates dynamic wisdom enriched with real learnings + failures
    - FALLBACK 1: Domain templates (when LLM is offline)
    - FALLBACK 2: Generic best-practices (NEVER returns None)

    The Silver Platter is NOT scripted — it's dynamic, task-specific wisdom.
    """
    # ==========================================================================
    # PRIMARY: LLM-generated wisdom enriched with real context
    # ==========================================================================
    llm_wisdom = _get_llm_professor_wisdom(prompt, depth)
    if llm_wisdom:
        return llm_wisdom

    # ==========================================================================
    # FALLBACK 1: Domain templates (only when LLM is offline)
    # ==========================================================================
    detected_domain = detect_domain_from_prompt(prompt)
    if detected_domain:
        wisdom = get_domain_specific_wisdom(detected_domain, depth)
        if wisdom:
            return "[LLM offline — using domain template]\n" + wisdom

    # ==========================================================================
    # FALLBACK 2: Generic best-practices (NEVER return None)
    # ==========================================================================
    return _get_generic_professor_wisdom(prompt)



# =========================================================================
# RACE R1 — S2 wisdom Redis cache layer
# =========================================================================
# Mirrors race/m3 (S6 deep voice). Caches the s2_professor_query LLM output
# keyed on (prompt[:300], domain). Cold path stays ~10s on DeepSeek; cache
# hit returns in <10ms. ZSF: every Redis failure is logged AND counted to
# s2_cache_errors before falling through to the live LLM call. Public API of
# _get_llm_professor_wisdom is unchanged.

_S2_CACHE_KEY_PREFIX = "contextdna:s2:cache:"
_S2_STATS_KEY_HITS = "contextdna:s2:stats:hits"
_S2_STATS_KEY_MISSES = "contextdna:s2:stats:misses"
_S2_STATS_KEY_ERRORS = "contextdna:s2:stats:errors"
_S2_CACHE_TTL_S_DEFAULT = 600  # 10 minutes — wisdom changes slowly


def _s2_cache_enabled() -> bool:
    """S2 cache is on by default. Operators can disable via S2_CACHE_ENABLED=0."""
    val = os.environ.get("S2_CACHE_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def _s2_cache_ttl() -> int:
    """Configurable TTL in seconds. Defaults to 600s (10 min)."""
    try:
        ttl = int(os.environ.get("S2_CACHE_TTL_S", str(_S2_CACHE_TTL_S_DEFAULT)))
        return ttl if ttl > 0 else _S2_CACHE_TTL_S_DEFAULT
    except (TypeError, ValueError):
        return _S2_CACHE_TTL_S_DEFAULT


def _s2_cache_key(prompt: str, domain: Optional[str], depth: str) -> str:
    """Hash (prompt[:300], domain, depth) into a stable cache key.

    Depth is included because brief vs full wisdom uses a different LLM profile
    and produces different output — must not collide.
    """
    payload = json.dumps(
        {
            "p": (prompt or "")[:300],
            "d": domain or "",
            "x": depth or "full",
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f"{_S2_CACHE_KEY_PREFIX}{digest}"


def _s2_cache_incr(stat_key: str) -> None:
    """Increment an S2 stats counter. ZSF: log + swallow on failure."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return
        client.incr(stat_key)
    except Exception as e:  # pragma: no cover - observability path
        logger.debug("s2 cache stats incr failed (%s): %s", stat_key, e)


def _s2_cache_get(key: str) -> Optional[str]:
    """Look up cached S2 wisdom. Returns None on miss OR Redis failure (ZSF logged)."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return None
        return client.get(key)
    except Exception as e:
        logger.warning("s2 cache get failed: %s", e)
        _s2_cache_incr(_S2_STATS_KEY_ERRORS)
        return None


def _s2_cache_set(key: str, value: str, ttl: int) -> bool:
    """Store S2 wisdom. Returns False on Redis failure (ZSF logged + counted)."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return False
        client.setex(key, ttl, value)
        return True
    except Exception as e:
        logger.warning("s2 cache set failed: %s", e)
        _s2_cache_incr(_S2_STATS_KEY_ERRORS)
        return False


def get_s2_cache_stats() -> Dict[str, int]:
    """Read s2_cache_hits / s2_cache_misses / s2_cache_errors from Redis."""
    out = {"s2_cache_hits": 0, "s2_cache_misses": 0, "s2_cache_errors": 0}
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return out
        pairs = (
            ("s2_cache_hits", _S2_STATS_KEY_HITS),
            ("s2_cache_misses", _S2_STATS_KEY_MISSES),
            ("s2_cache_errors", _S2_STATS_KEY_ERRORS),
        )
        for label, key in pairs:
            try:
                raw = client.get(key)
                out[label] = int(raw) if raw is not None else 0
            except Exception:
                pass
    except Exception as e:
        logger.debug("s2 cache stats read failed: %s", e)
    return out


def _get_llm_professor_wisdom(prompt: str, depth: str = "full") -> Optional[str]:
    """Generate dynamic professor wisdom via local LLM enriched with real context.

    PRIMARY path for Silver Platter wisdom. Feeds the LLM:
    - The actual task prompt
    - Relevant past learnings from SQLite FTS5
    - Recent failure patterns
    - Domain seed context (template as input, not output)

    This produces GENUINELY DYNAMIC wisdom grounded in real experience.

    RACE R1: wraps the LLM call with a Redis cache (TTL=600s) keyed on
    (prompt[:300], domain, depth). ZSF: any Redis failure falls through to
    the live LLM call and bumps an error counter — no silent failures.
    """
    # RACE R1 — Redis cache lookup. Hits return in <10ms vs ~10s DeepSeek cold path.
    # Compute domain once up-front so it can both feed the cache key AND the
    # downstream prompt-build (avoids double work on a hit).
    cache_on = _s2_cache_enabled()
    cache_key = None
    detected_domain_for_key = None
    if cache_on:
        try:
            detected_domain_for_key = detect_domain_from_prompt(prompt)
            cache_key = _s2_cache_key(prompt, detected_domain_for_key, depth)
            cached = _s2_cache_get(cache_key)
            if cached:
                _s2_cache_incr(_S2_STATS_KEY_HITS)
                logger.debug("S2 wisdom cache HIT (key=%s)", cache_key[-8:])
                return cached
            _s2_cache_incr(_S2_STATS_KEY_MISSES)
        except Exception as e:
            # Defensive: hashing/key build should never explode, but if it does
            # we MUST still execute the LLM path (no silent failure, count it).
            logger.warning("s2 cache lookup path errored: %s", e)
            _s2_cache_incr(_S2_STATS_KEY_ERRORS)
            cache_key = None

    try:
        # === Gather real context to enrich the LLM prompt ===

        # 1. Relevant learnings from SQLite FTS5 + semantic rescue
        learnings_context = ""
        try:
            from memory.sqlite_storage import get_sqlite_storage
            local_store = get_sqlite_storage()  # Singleton — prevents FD leak
            results = local_store.query(prompt[:100], limit=3)
            # Semantic rescue: augment with vector similarity when FTS5 returns sparse results
            if len(results) < 3:
                try:
                    from memory.semantic_search import rescue_search
                    results = rescue_search(prompt[:200], results, min_results=3, top_k=5)
                except Exception:
                    pass  # Graceful degradation — FTS5 results pass through unchanged
            if results:
                entries = []
                for r in results[:3]:
                    title = r.get('title', '')
                    content_text = r.get('content', r.get('preferences', ''))[:200]
                    if title:
                        entries.append(f"- {title}: {content_text}")
                if entries:
                    learnings_context = "\n".join(entries)
        except Exception:
            pass

        # 2. Recent failure patterns
        failures_context = ""
        try:
            from memory.failure_pattern_analyzer import get_failure_pattern_analyzer
            analyzer = get_failure_pattern_analyzer()
            landmines = analyzer.get_landmines_for_task(prompt, limit=3)
            if landmines:
                failures_context = "\n".join(f"- {lm}" for lm in landmines)
        except Exception:
            pass

        # 3. Domain seed (template as context, NOT the answer)
        domain_seed = ""
        # Reuse the domain we detected for the cache key (if any) to avoid
        # double-computing. Falls back to a fresh detect when cache is off.
        detected_domain = detected_domain_for_key if cache_on else detect_domain_from_prompt(prompt)
        if detected_domain:
            _, professor_wisdom = get_professor_wisdom_dicts()
            if professor_wisdom and detected_domain in professor_wisdom:
                dw = professor_wisdom[detected_domain]
                domain_seed = dw.get("first_principle", "").strip()[:200]

        # === Build concise system prompt (nudge efficiency, allow flexibility) ===
        system_prompt = (
            "Senior engineering professor. Be SPECIFIC to the task. Use the context provided.\n"
            "Efficient communication matters - prioritize signal over noise. You may sacrifice perfect grammar for clarity and speed.\n"
            "Typical format works well: THE ONE THING (core insight), LANDMINES (key gotchas), THE PATTERN (approach).\n"
            "But adapt if context requires - comprehensive coverage beats arbitrary constraints when truly needed.\n"
            "No filler. No pleasantries. Reference provided learnings/failures."
        )

        # === Build enriched user prompt (capped for speed) ===
        user_parts = [f"Task: {prompt[:200]}"]

        if learnings_context:
            user_parts.append(f"\nLearnings:\n{learnings_context[:400]}")

        if failures_context:
            user_parts.append(f"\nFailures:\n{failures_context[:300]}")

        if domain_seed:
            user_parts.append(f"\nDomain: {domain_seed[:150]}")

        user_prompt = "\n".join(user_parts)

        if depth == "one_thing_only":
            user_prompt += "\n\nFocus your response: What's ONE key insight or action that matters most here? You can respond however makes sense - just be direct and concise (2-3 sentences ideal, but flexibility is fine)."

        # Thinking mode handled centrally by llm_priority_queue (golden era pattern):
        # s2_professor profile → model decides naturally (not in classify/summarize list)
        # Priority queue applies /think for reasoning/deep/explore, /no_think for classify/summarize
        # No double-injection here — let the priority queue be the single source of truth.

        # Route through priority queue (P2 ATLAS) — respects GPU lock, prevents stampede
        from memory.llm_priority_queue import s2_professor_query
        brief = depth == "one_thing_only"
        response_text = s2_professor_query(system_prompt, user_prompt, brief=brief)

        if response_text and len(response_text) > 20:
            wisdom_out = f"[Professor via local LLM — reasoning]\n{response_text}"

            # RACE R1 — Store in cache for repeat queries within TTL window.
            if cache_on and cache_key:
                _s2_cache_set(cache_key, wisdom_out, _s2_cache_ttl())

            return wisdom_out

    except Exception:
        pass  # Non-fatal — falls through to domain templates

    return None


def _get_generic_professor_wisdom(prompt: str) -> str:
    """Generic best-practices wisdom with Redis caching.
    
    Ensures Section 2 NEVER shows unavailable. Results are cached in Redis
    under a known key so the anticipation engine can detect when the system
    fell back to templates and prioritize pre-computing fresh LLM content.
    """
    p = prompt.lower()
    if any(w in p for w in ["fix","bug","error","broken","fail","crash"]):
        result = "THE ONE THING:\n  Read the error message carefully. 80%% of bugs are found by reading.\n\nLANDMINES:\n  - Find root cause first\n  - What changed since it worked?\n  - Isolate one variable at a time"
    elif any(w in p for w in ["create","add","new","implement","build","make"]):
        result = "THE ONE THING:\n  Check if this already exists. Grep for similar patterns.\n\nLANDMINES:\n  - Reuse existing patterns\n  - Start minimal, iterate\n  - Follow established naming conventions"
    elif any(w in p for w in ["update","change","modify","edit","refactor"]):
        result = "THE ONE THING:\n  Understand the complete flow before changing anything.\n\nLANDMINES:\n  - Check for ripple effects\n  - Prefer reversible changes\n  - Establish a test baseline first"
    else:
        result = "THE ONE THING:\n  Read existing code first. Understand before acting.\n\nLANDMINES:\n  - Query memory first: .venv/bin/python3 memory/query.py\n  - Check if feature already exists\n  - Fix root causes, not symptoms"

    # Cache the programmatic fallback in Redis so anticipation engine
    # can detect template usage and prioritize fresh LLM pre-computation
    # Project-scoped to prevent cross-project cache bleed
    try:
        from memory.redis_cache import get_redis_client, cache_section_content, get_project_id
        if get_redis_client():
            _project = get_project_id()
            cache_section_content(f"s2:programmatic_fallback:{_project}:latest", result, ttl=300)
    except Exception:
        pass  # Non-blocking

    return result
def generate_section_2(prompt: str, config: InjectionConfig) -> str:
    """Generate WISDOM section with Professor wisdom + LANDMINE warnings."""
    if not config.section_2_enabled:
        return ""

    lines = []

    # Header
    emoji = "🎓" if config.emoji_enabled else ""
    lines.append(f"{emoji} PROFESSOR WISDOM")
    lines.append("─" * 40)

    # Sanitize prompt - remove IDE tags that pollute queries
    clean_prompt = _sanitize_prompt_for_query(prompt)

    # Check for C variant wisdom injection
    if config.wisdom_injection_enabled and config.wisdom_text:
        lines.append(f"  [WISDOM INJECTION - Variant C]")
        lines.append(f"  {config.wisdom_text}")
    else:
        # Get professor wisdom
        wisdom = get_professor_wisdom(clean_prompt, config.professor_depth)
        if wisdom:
            # Indent each line
            for line in wisdom.split("\n"):
                lines.append(f"  {line}")
        else:
            lines.append("  [Professor unavailable - using heuristic context]")

    # Always try to add LANDMINE warnings from FailurePatternAnalyzer
    landmines = _get_failure_landmines(clean_prompt)
    if landmines:
        lines.append("")
        lines.append("  LANDMINES (from failure patterns):")
        for lm in landmines:
            lines.append(f"    {lm}")

    return "\n".join(lines) if len(lines) > 2 else ""


def _sanitize_prompt_for_query(prompt: str) -> str:
    """Remove IDE tags and other noise from prompt before querying."""
    import re
    # Remove <ide_opened_file>...</ide_opened_file> and similar tags
    clean = re.sub(r'<ide_[^>]+>[^<]*</[^>]+>', '', prompt)
    clean = re.sub(r'<ide_[^>]+>[^<]*', '', clean)
    # Remove <system-reminder> tags
    clean = re.sub(r'<system-reminder>.*?</system-reminder>', '', clean, flags=re.DOTALL)
    # Trim whitespace
    return clean.strip()


def _get_failure_landmines(prompt: str, limit: int = 3) -> List[str]:
    """Get LANDMINE warnings from FailurePatternAnalyzer."""
    try:
        from memory.failure_pattern_analyzer import get_failure_pattern_analyzer
        analyzer = get_failure_pattern_analyzer()
        return analyzer.get_landmines_for_task(prompt, limit=limit)
    except ImportError:
        return []
    except Exception:
        return []


# =============================================================================
# SECTION 3: AWARENESS
# =============================================================================

def get_recent_git_changes(prompt: str) -> List[str]:
    """Get recent git changes related to prompt."""
    try:
        from memory.agent_service import _get_recent_git_changes
        return _get_recent_git_changes(prompt)
    except ImportError:
        return []


def get_ripple_effects(prompt: str) -> List[str]:
    """Get what else might be affected - Enhanced with graph traversal analysis.

    Combines:
    1. Agent service ripple analysis (quick_context style)
    2. Graph traversal analysis (critical path detection)
    """
    effects = []

    # 1. Original ripple effects from agent service
    try:
        from memory.agent_service import _get_ripple_effects
        effects.extend(_get_ripple_effects(prompt))
    except ImportError:
        pass

    # 2. Graph traversal analysis - detect changed files and their ripple effects
    try:
        from memory.graph_traversal import RippleChainAnalyzer

        analyzer = RippleChainAnalyzer()

        # Infer which files are being modified from prompt context
        changed_files = _infer_changed_files(prompt)

        if changed_files:
            # Analyze combined ripple risk
            combined = analyzer.get_critical_path_risk(changed_files)

            # Add graph-based ripple warnings
            if combined['combined_risk_level'] in ['HIGH', 'CRITICAL']:
                effects.append(
                    f"⚠️  Graph Analysis: {combined['total_affected_files']} files affected "
                    f"({combined['combined_risk_level']} risk)"
                )

            if combined['critical_hubs_touched'] > 0:
                effects.append(
                    f"🔴 {combined['critical_hubs_touched']} critical infrastructure hub(s) affected"
                )

            # Add recommendations
            for rec in combined.get('recommendations', [])[:2]:
                effects.append(rec)
    except Exception as e:
        logging.getLogger('context_dna').debug(f"Graph traversal ripple analysis failed: {e}")

    return effects


def _infer_changed_files(prompt: str) -> List[str]:
    """Infer which files are being modified based on prompt context.

    Looks for patterns like:
    - "modify file.py"
    - "edit memory/module.py"
    - "changing context-dna/services"
    - File paths in the prompt
    """
    import re

    changed = []
    prompt_lower = (prompt or "").lower()

    # Pattern 1: "modify/edit/change <filepath>"
    patterns = [
        r'(?:modify|edit|change|update)\s+[\w\-./]+\.(?:py|ts|tsx|js)',
        r'file[s]?:?\s+[\w\-./]+\.(?:py|ts|tsx|js)',
        r'(?:memory|admin|backend|infra|context-dna)/[\w\-./]*\.(?:py|ts|tsx|js)',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, prompt_lower)
        for match in matches:
            # Extract file path from match
            file_path = re.search(r'[\w\-./]*\.(?:py|ts|tsx|js)', match)
            if file_path:
                changed.append(file_path.group())

    # Pattern 2: Common critical files referenced in prompt
    critical_files = {
        'hook': 'memory/persistent_hook_structure.py',
        'injection': 'memory/persistent_hook_structure.py',
        'agent_service': 'memory/agent_service.py',
        'scheduler': 'memory/lite_scheduler.py',
        'graph': 'memory/graph_traversal.py',
        'dialogue': 'memory/dialogue_mirror.py',
    }

    for keyword, filepath in critical_files.items():
        if keyword in prompt_lower:
            changed.append(filepath)

    return list(set(changed))


def get_previous_mistakes(prompt: str) -> List[str]:
    """Get my past failures for similar tasks."""
    try:
        from memory.agent_service import _get_my_previous_mistakes
        return _get_my_previous_mistakes(prompt)
    except ImportError:
        return []


# Architecture map cache — loaded once per process, file rarely changes
_arch_map_cache: Optional[dict] = None
_arch_map_mtime: float = 0.0


def get_architecture_topology(prompt: str) -> List[str]:
    """Return structural context from architecture.map.json for the current prompt.

    Identifies which system components the prompt touches and shows their
    relationships (callers, callees, role). Provides the "you are here" view
    of the system topology — complementary to ripple effects (change impact).
    """
    global _arch_map_cache, _arch_map_mtime
    import json
    from pathlib import Path

    map_path = Path(__file__).parent.parent / ".projectdna" / "architecture.map.json"
    if not map_path.exists():
        return []

    # Reload only if file changed
    try:
        mtime = map_path.stat().st_mtime
        if _arch_map_cache is None or mtime != _arch_map_mtime:
            with open(map_path) as f:
                _arch_map_cache = json.load(f)
            _arch_map_mtime = mtime
    except Exception:
        return []

    arch = _arch_map_cache
    nodes = {n["id"]: n for n in arch.get("nodes", [])}
    edges = arch.get("edges", [])
    prompt_lower = (prompt or "").lower()

    # Match nodes by: file paths, name keywords, role keywords
    matched_ids = set()
    for node in arch.get("nodes", []):
        # Match against file paths in prompt
        for fp in node.get("filePaths", []):
            fp_stem = fp.rstrip("/").rsplit("/", 1)[-1].replace(".py", "").replace(".ts", "").replace(".tsx", "")
            if len(fp_stem) > 4 and (fp_stem.lower() in prompt_lower or fp.rstrip("/").lower() in prompt_lower):
                matched_ids.add(node["id"])
        # Match against name (e.g., "session historian", "agent service")
        name_lower = node.get("name", "").lower()
        if len(name_lower) > 5 and name_lower in prompt_lower:
            matched_ids.add(node["id"])
        # Match against role
        role = node.get("metadata", {}).get("role", "")
        if role and len(role) > 5 and role.replace("_", " ") in prompt_lower:
            matched_ids.add(node["id"])

    if not matched_ids:
        return []

    # Build adjacency for matched nodes
    lines = []
    for nid in sorted(matched_ids):
        node = nodes.get(nid)
        if not node:
            continue

        kind = node.get("kind", "?")
        name = node.get("name", nid)
        role = node.get("metadata", {}).get("role", "")

        # Find callers and callees
        callers = []
        callees = []
        for e in edges:
            if e["to"] == nid:
                caller = nodes.get(e["from"], {}).get("name", e["from"])
                label = e.get("label", e.get("relation", ""))
                callers.append(f"{caller}" + (f" ({label})" if label else ""))
            if e["from"] == nid:
                callee = nodes.get(e["to"], {}).get("name", e["to"])
                label = e.get("label", e.get("relation", ""))
                callees.append(f"{callee}" + (f" ({label})" if label else ""))

        # Concise summary
        desc = f"{name} [{kind}]"
        if role:
            desc += f" role={role}"
        lines.append(desc)

        if callers:
            lines.append(f"  ← called by: {', '.join(callers[:4])}")
        if callees:
            lines.append(f"  → calls: {', '.join(callees[:4])}")

    # Hub detection — flag high-connectivity nodes being touched
    for nid in matched_ids:
        degree = sum(1 for e in edges if e["from"] == nid or e["to"] == nid)
        if degree >= 5:
            name = nodes.get(nid, {}).get("name", nid)
            lines.append(f"  ⚠ {name} is a hub ({degree} connections) — changes cascade")

    return lines[:12]  # Cap output


def get_mansion_warnings(prompt: str) -> List[str]:
    """Bridge: meta-analysis concerns + MMOTW repair SOPs → proactive warnings.

    The butler notices recurring issues across sessions and warns Atlas
    BEFORE he rediscovers the same problem for the Nth time.
    """
    warnings = []
    prompt_lower = (prompt or "").lower()

    # 1. Meta-analysis recurring concerns
    try:
        import json as _json
        db_path = MEMORY_DIR / ".meta_analysis.db"
        if db_path.exists():
            from memory.db_utils import safe_conn
            with safe_conn(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT concerns, insights FROM meta_analysis_runs ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                if row:
                    concerns = _json.loads(row[0] or "[]")
                    for concern in concerns[:3]:
                        concern_lower = concern.lower()
                        if (any(kw in prompt_lower for kw in _extract_concern_keywords(concern_lower))
                                or "repeated" in concern_lower):
                            warnings.append(f"[META] {concern[:120]}")
    except Exception:
        pass

    # 2. MMOTW repair SOPs — relevant to current task
    try:
        repair_db = MEMORY_DIR / ".repair_sops.db"
        if repair_db.exists():
            from memory.db_utils import safe_conn
            with safe_conn(str(repair_db)) as conn:
                # Get all SOPs and check relevance
                rows = conn.execute(
                    "SELECT component, title, symptom, root_cause, confidence FROM repair_sops "
                    "WHERE confidence >= 0.5 ORDER BY confidence DESC LIMIT 10"
                ).fetchall()
                for r in rows:
                    component = (r["component"] or "").lower()
                    title = (r["title"] or "").lower()
                    symptom = (r["symptom"] or "").lower()
                    sop_words = set(re.findall(r'\b[a-z]{3,}\b', f"{component} {title} {symptom}"))
                    prompt_words = set(re.findall(r'\b[a-z]{3,}\b', prompt_lower))
                    overlap = sop_words & prompt_words
                    if len(overlap) >= 2 or component.replace("_", " ") in prompt_lower:
                        root = r["root_cause"] or "unknown"
                        warnings.append(f"[REPAIR SOP] {r['component']}: {root[:100]}")
    except Exception:
        pass

    return warnings


def _extract_concern_keywords(concern: str) -> List[str]:
    """Extract meaningful keywords from a meta-analysis concern."""
    # Remove common words, keep domain-specific terms
    stop = {"the", "a", "an", "is", "was", "are", "in", "to", "for", "of", "and", "or", "issue", "repeated"}
    words = re.findall(r'\b[a-z]{4,}\b', concern)
    return [w for w in words if w not in stop][:5]


def _get_probe_injection_hints(prompt: str) -> List[str]:
    """Query GhostScan injection_prioritizer probe for context-relevant file hints.

    Runs the injection_prioritizer probe via the fleet daemon's /scan/results
    endpoint (fast, cached) or falls back to direct probe execution.
    Returns list of human-readable hint strings for S3 awareness section.
    """
    hints = []
    try:
        # Try daemon endpoint first (fast, pre-cached)
        import urllib.request, json
        req = urllib.request.Request("http://127.0.0.1:8855/scan/results", method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        if 200 <= resp.status < 300:
            data = json.loads(resp.read())
            for result in data.get("results", []):
                if result.get("probe_id") == "injection_prioritizer":
                    for finding in result.get("findings", []):
                        title = finding.get("title", "")
                        if "ranked" in title.lower():
                            actions = finding.get("suggested_actions", [])
                            if actions:
                                hints.append(actions[0])
                        elif finding.get("severity") in ("medium", "high"):
                            hints.append(finding.get("title", ""))
            return hints[:5]
    except Exception:
        pass

    # Fallback 2: GhostScan bridge (cached results, no daemon needed)
    try:
        from memory.ghostscan_bridge import get_findings_for_injection
        bridge_hints = get_findings_for_injection(max_findings=5)
        if bridge_hints:
            return bridge_hints[:5]
    except Exception:
        pass

    # Fallback 3: direct probe execution (no daemon needed)
    try:
        from multifleet.probe_context_factory import build_probe_context
        from multifleet.probes.injection_prioritizer import probe_injection_prioritizer
        ctx = build_probe_context(task=prompt[:200])
        result = probe_injection_prioritizer(ctx)
        for finding in result.findings:
            if finding.suggested_actions:
                hints.append(finding.suggested_actions[0])
            elif finding.severity in ("medium", "high"):
                hints.append(finding.title)
    except Exception:
        pass

    return hints[:5]


def generate_section_3(prompt: str, risk_level: RiskLevel, config: InjectionConfig) -> str:
    """Generate AWARENESS section.

    ZSF / Cycle 7 G3: never return empty string. Even when S3 is suppressed by
    risk gating (LOW risk, awareness_depth=none) or by missing data sources
    (Redis down, ghostscan offline), emit a labeled stub explaining the absence
    so the e2e per-section coverage check can distinguish "intentionally
    minimal" from "broken / missing". Empty strings are treated as section
    failures by `_run_timed` -> manifest-excluded path and surface as `missing`
    in `section_perf` logs (CLAUDE.md "ZERO SILENT FAILURES" invariant).
    """
    emoji_hdr = "🔄" if config.emoji_enabled else ""

    # Hard-disabled (mode=MINIMAL, S0/S5-only presets, etc.)
    if not config.section_3_enabled:
        return f"{emoji_hdr} AWARENESS\n  [s3:disabled by config — section_3_enabled=False]"

    # Risk-gated to no content (LOW risk path in generate_context_injection)
    if config.awareness_depth == "none":
        return (
            f"{emoji_hdr} AWARENESS\n"
            f"  [s3:suppressed — risk={risk_level.value}, awareness_depth=none]\n"
            f"  Lightweight task — full awareness gated. Re-run with HIGH/CRITICAL "
            f"prompt for ripple/topology/probe data."
        )

    lines = []

    # Header
    emoji = emoji_hdr
    lines.append(f"{emoji} AWARENESS")
    lines.append("─" * 40)

    # Recent changes - show for ALL risk levels (lightweight, always useful)
    # Even simple tasks benefit from knowing what files changed recently
    # ZSF: git failure must not zero out the whole section.
    try:
        changes_data = get_recent_git_changes(prompt)
    except Exception as _e:
        _safe_log("debug", f"S3 get_recent_git_changes failed: {_e}")
        changes_data = None
    # Handle both list and dict returns
    if isinstance(changes_data, dict):
        commits = changes_data.get('commits', [])
        files = changes_data.get('files', [])
        if commits or files:
            lines.append("  RECENT CHANGES:")
            for commit in commits[:3]:
                lines.append(f"    📝 {commit}")
            for file in files[:3]:
                lines.append(f"    📄 {file}")
    elif isinstance(changes_data, list) and changes_data:
        lines.append("  RECENT CHANGES:")
        for change in changes_data[:5]:
            lines.append(f"    • {change}")

    # Ripple effects - show for all risk levels (full depth only)
    # Even simple changes can have ripple effects worth knowing about
    if config.awareness_depth == "full":
        ripples = get_ripple_effects(prompt)
        if ripples:
            lines.append("")
            emoji = "💥" if config.emoji_enabled else ""
            lines.append(f"  {emoji} RIPPLE EFFECTS:")
            for ripple in ripples[:5]:
                lines.append(f"    • {ripple}")

    # Previous mistakes (critical/high only, full depth)
    if config.awareness_depth == "full" and risk_level in [RiskLevel.CRITICAL, RiskLevel.HIGH]:
        mistakes = get_previous_mistakes(prompt)
        if mistakes:
            lines.append("")
            emoji = "❌" if config.emoji_enabled else ""
            lines.append(f"  {emoji} PREVIOUS MISTAKES (avoid repeating):")
            for mistake in mistakes[:3]:
                lines.append(f"    • {mistake}")

    # Architecture topology — structural awareness of touched components
    if config.awareness_depth == "full":
        topo = get_architecture_topology(prompt)
        if topo:
            lines.append("")
            emoji = "🏗️" if config.emoji_enabled else ""
            lines.append(f"  {emoji} SYSTEM TOPOLOGY (components touched):")
            for line in topo:
                lines.append(f"    {line}")

    # Architecture twin summary — module dependency graph snapshot
    try:
        from memory.architecture_twin import get_twin_summary
        twin = get_twin_summary()
        if twin:
            lines.append("")
            lines.append(f"  Twin: {twin['modules']} modules, {twin['edges']} edges. Last refresh: {twin['last_refresh']}")
    except Exception:
        pass  # Non-critical — twin summary is best-effort

    # Structural drift — signal when architecture recently changed
    try:
        from memory.refresh_architecture_twin import get_structural_drift
        drift = get_structural_drift()
        if drift:
            lines.append("")
            emoji = "⚠️" if config.emoji_enabled else ""
            mins = drift.get("changed_ago_min", "?")
            lines.append(f"  {emoji} STRUCTURAL DRIFT (architecture changed {mins}min ago):")
            details = drift.get("details", [])
            if details:
                for detail in details[:4]:
                    lines.append(f"    • {detail}")
            else:
                lines.append(f"    • Hash: {drift.get('content_hash', '?')}")
    except Exception:
        pass  # Non-critical — drift detection is best-effort

    # PROACTIVE MANSION WARNINGS — Cross-session recurring issues + repair SOPs
    mansion_warnings = get_mansion_warnings(prompt)
    if mansion_warnings:
        lines.append("")
        lines.append("  MANSION WARNINGS (recurring cross-session issues):")
        for warning in mansion_warnings[:5]:
            lines.append(f"    • {warning}")

    # PROBE FINDINGS — GhostScan code reality scanner
    if config.awareness_depth == "full":
        try:
            # Primary: GhostScan bridge (richer data from cached scans)
            from memory.ghostscan_bridge import get_findings_for_injection, get_scan_summary_text
            scan_summary = get_scan_summary_text()
            probe_findings = get_findings_for_injection(max_findings=5)
            if scan_summary or probe_findings:
                lines.append("")
                lines.append("  CODE REALITY (GhostScan probes):")
                if scan_summary:
                    lines.append(f"    {scan_summary}")
                for pf in probe_findings:
                    lines.append(f"    • {pf}")
            else:
                # Fallback: injection prioritizer hints
                probe_lines = _get_probe_injection_hints(prompt)
                if probe_lines:
                    lines.append("")
                    lines.append("  PROBE INSIGHTS (code scanner):")
                    for pl in probe_lines[:5]:
                        lines.append(f"    • {pl}")
        except Exception:
            # Final fallback
            try:
                probe_lines = _get_probe_injection_hints(prompt)
                if probe_lines:
                    lines.append("")
                    lines.append("  PROBE INSIGHTS (code scanner):")
                    for pl in probe_lines[:5]:
                        lines.append(f"    • {pl}")
            except Exception:
                pass  # Non-critical — probe insights are best-effort

    # ZSF: never return empty. If we have header-only (lines<=2), all data
    # sources came back blank — surface that explicitly so the e2e checker
    # and ops can see WHY S3 has no body, not just "missing".
    if len(lines) > 2:
        return "\n".join(lines)
    return (
        f"{emoji_hdr} AWARENESS\n"
        f"  [s3:no data — git/twin/ghostscan all returned empty]\n"
        f"  Likely causes: clean working tree, twin not refreshed, or scanners offline."
    )


# =============================================================================
# SECTION 4: DEEP CONTEXT (Critical/High only)
# =============================================================================

# =========================================================================
# RACE T2 — S4 blueprint Redis cache layer
# =========================================================================
# Mirrors race/r1 (S2 wisdom). Caches the get_blueprint() subprocess output
# keyed on prompt[:300]. Cold path measured at 12s (race/o2 cold profile);
# cache hit returns in <10ms. Architecture changes slowly so TTL=1800s
# (30 min) is safe. ZSF: every Redis failure is logged AND counted to
# s4_blueprint_cache_errors before falling through to the live subprocess.
# Public API of get_blueprint is unchanged.

_S4_BLUEPRINT_CACHE_KEY_PREFIX = "contextdna:s4:blueprint:cache:"
_S4_BLUEPRINT_STATS_KEY_HITS = "contextdna:s4:blueprint:stats:hits"
_S4_BLUEPRINT_STATS_KEY_MISSES = "contextdna:s4:blueprint:stats:misses"
_S4_BLUEPRINT_STATS_KEY_ERRORS = "contextdna:s4:blueprint:stats:errors"
_S4_BLUEPRINT_CACHE_TTL_S_DEFAULT = 1800  # 30 minutes — architecture changes slowly

# Legacy in-memory cache kept as a 30s L1 in front of Redis (preserves
# existing fast-path semantics for the same process and is also used by
# tests that don't bring Redis up).
_blueprint_cache: dict = {}
_blueprint_cache_ttl: int = 30  # seconds (in-process L1)


def _s4_blueprint_cache_enabled() -> bool:
    """S4 blueprint cache is on by default. Disable via S4_BLUEPRINT_CACHE_ENABLED=0."""
    val = os.environ.get("S4_BLUEPRINT_CACHE_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def _s4_blueprint_cache_ttl() -> int:
    """Configurable TTL in seconds. Defaults to 1800s (30 min)."""
    try:
        ttl = int(os.environ.get(
            "S4_BLUEPRINT_CACHE_TTL_S", str(_S4_BLUEPRINT_CACHE_TTL_S_DEFAULT)
        ))
        return ttl if ttl > 0 else _S4_BLUEPRINT_CACHE_TTL_S_DEFAULT
    except (TypeError, ValueError):
        return _S4_BLUEPRINT_CACHE_TTL_S_DEFAULT


def _s4_blueprint_cache_key(prompt: str) -> str:
    """Hash prompt[:300] into a stable cache key.

    Blueprint is prompt-driven (not session/depth/domain), so we key on the
    raw prompt prefix only — same prompt prefix should always reuse.
    """
    payload = (prompt or "")[:300].encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f"{_S4_BLUEPRINT_CACHE_KEY_PREFIX}{digest}"


def _s4_blueprint_cache_incr(stat_key: str) -> None:
    """Increment an S4 blueprint stats counter. ZSF: log + swallow on failure."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return
        client.incr(stat_key)
    except Exception as e:  # pragma: no cover - observability path
        logger.debug("s4 blueprint cache stats incr failed (%s): %s", stat_key, e)


def _s4_blueprint_cache_get(key: str) -> Optional[str]:
    """Look up cached S4 blueprint. Returns None on miss OR Redis failure (ZSF logged)."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return None
        return client.get(key)
    except Exception as e:
        logger.warning("s4 blueprint cache get failed: %s", e)
        _s4_blueprint_cache_incr(_S4_BLUEPRINT_STATS_KEY_ERRORS)
        return None


def _s4_blueprint_cache_set(key: str, value: str, ttl: int) -> bool:
    """Store S4 blueprint. Returns False on Redis failure (ZSF logged + counted)."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return False
        client.setex(key, ttl, value)
        return True
    except Exception as e:
        logger.warning("s4 blueprint cache set failed: %s", e)
        _s4_blueprint_cache_incr(_S4_BLUEPRINT_STATS_KEY_ERRORS)
        return False


def get_s4_blueprint_cache_stats() -> Dict[str, int]:
    """Read s4_blueprint_cache_hits / _misses / _errors from Redis."""
    out = {
        "s4_blueprint_cache_hits": 0,
        "s4_blueprint_cache_misses": 0,
        "s4_blueprint_cache_errors": 0,
    }
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return out
        pairs = (
            ("s4_blueprint_cache_hits", _S4_BLUEPRINT_STATS_KEY_HITS),
            ("s4_blueprint_cache_misses", _S4_BLUEPRINT_STATS_KEY_MISSES),
            ("s4_blueprint_cache_errors", _S4_BLUEPRINT_STATS_KEY_ERRORS),
        )
        for label, key in pairs:
            try:
                raw = client.get(key)
                out[label] = int(raw) if raw is not None else 0
            except Exception:
                pass
    except Exception as e:
        logger.debug("s4 blueprint cache stats read failed: %s", e)
    return out


def get_blueprint(prompt: str) -> Optional[str]:
    """Get architecture blueprint with caching.

    PERFORMANCE: Reduced timeout to 3s. Skip if services unavailable.
    Blueprint is nice-to-have, not critical for webhook generation.

    RACE T2: wraps the subprocess call with a Redis cache (TTL=1800s)
    keyed on prompt[:300]. Race/o2 cold path measured the LLM-backed
    blueprint generation at 12s. Cache hit returns in <10ms. ZSF: any
    Redis failure falls through to the legacy L1 in-process cache and
    then the subprocess call, bumping s4_blueprint_cache_errors —
    no silent failures.

    L1 in-process cache (30s TTL) is preserved for same-process reuse
    even when Redis is unavailable, and to avoid extra Redis round-trips
    inside one webhook batch.
    """
    # ----- L1: in-process cache (fast even if Redis is down) ------------
    prompt_key = hashlib.md5(prompt[:200].encode()).hexdigest()[:8]
    l1_cache_key = f"blueprint_{prompt_key}"
    now = time.time()

    if l1_cache_key in _blueprint_cache:
        cached_time, cached_result = _blueprint_cache[l1_cache_key]
        if now - cached_time < _blueprint_cache_ttl:
            return cached_result  # L1 hit

    # Evict stale L1 entries (prevent unbounded growth)
    stale = [
        k for k, (t, _) in _blueprint_cache.items()
        if now - t > _blueprint_cache_ttl
    ]
    for k in stale:
        del _blueprint_cache[k]

    # ----- L2: Redis cache (cross-process, 30 min TTL) ------------------
    cache_on = _s4_blueprint_cache_enabled()
    redis_key = None
    if cache_on:
        try:
            redis_key = _s4_blueprint_cache_key(prompt)
            cached = _s4_blueprint_cache_get(redis_key)
            if cached:
                _s4_blueprint_cache_incr(_S4_BLUEPRINT_STATS_KEY_HITS)
                logger.debug(
                    "S4 blueprint cache HIT (key=%s)", redis_key[-8:]
                )
                # Warm L1 too so next intra-process call skips Redis.
                _blueprint_cache[l1_cache_key] = (now, cached)
                return cached
            _s4_blueprint_cache_incr(_S4_BLUEPRINT_STATS_KEY_MISSES)
        except Exception as e:
            # Defensive: hashing/key build should never explode, but if it
            # does we MUST still execute the subprocess path (no silent
            # failure, count it).
            logger.warning("s4 blueprint cache lookup path errored: %s", e)
            _s4_blueprint_cache_incr(_S4_BLUEPRINT_STATS_KEY_ERRORS)
            redis_key = None

    # ----- Generate fresh blueprint via subprocess ----------------------
    # Use .venv/bin/python3 (not system python3 which is 3.9)
    venv_python = str(MEMORY_DIR.parent / ".venv" / "bin" / "python3")
    try:
        result = subprocess.run(
            [venv_python, str(MEMORY_DIR / "context.py"), "--blueprint", prompt],
            capture_output=True,
            text=True,
            timeout=3  # REDUCED from 15s - fail fast
        )
        if result.returncode == 0 and result.stdout.strip():
            blueprint = result.stdout.strip()
            # Warm both caches.
            _blueprint_cache[l1_cache_key] = (now, blueprint)
            if cache_on and redis_key is not None:
                _s4_blueprint_cache_set(
                    redis_key, blueprint, _s4_blueprint_cache_ttl()
                )
            return blueprint
        return None
    except subprocess.TimeoutExpired:
        return None  # Fast-fail on timeout
    except Exception:
        return None


def get_brain_state() -> Optional[str]:
    """Get recent brain state with intelligent caching."""
    # Use cached brain state (30s TTL) to avoid repeated file I/O
    content = get_brain_state_cached()
    if content:
        # Return first 1000 chars
        return content[:1000] + "..." if len(content) > 1000 else content
    return None


def get_session_briefing_summary() -> Optional[str]:
    """Get compact session briefing from local endpoint for cross-session continuity."""
    try:
        import requests as _requests
        resp = _requests.get("http://127.0.0.1:8080/contextdna/session-briefing", timeout=3)
        if resp.status_code != 200:
            return None
        data = resp.json()
        sections = data.get("sections", {})
        parts = []

        # Last session summary
        last = sections.get("last_session", {})
        threads = last.get("threads", [])
        if threads:
            total_msgs = sum(t.get("message_count", 0) for t in threads)
            parts.append(f"Last session: {total_msgs} msgs across {len(threads)} threads")
            main_thread = threads[0] if threads else {}
            if main_thread:
                parts.append(f"Main thread: {main_thread.get('session_id', 'unknown')}, {main_thread.get('message_count', 0)} msgs")

        # Meta-analysis insights
        meta = sections.get("meta_analysis", {})
        insights = meta.get("insights", [])
        if insights:
            parts.append(f"Cross-session insights ({len(insights)}):")
            for i in insights[:3]:
                parts.append(f"  - {str(i)[:80]}")

        concerns = meta.get("concerns", [])
        if concerns:
            parts.append(f"Concerns ({len(concerns)}):")
            for c in concerns[:2]:
                parts.append(f"  - {str(c)[:80]}")

        # Evidence pipeline
        evidence = sections.get("evidence_pipeline", {})
        if evidence:
            parts.append(f"Evidence: {evidence.get('claims', 0)} claims, {evidence.get('outcomes', 0)} outcomes")

        return "\n".join(parts) if parts else None
    except Exception:
        return None


def _get_session_rehydration_context(prompt: str = "") -> Optional[str]:
    """Get recent session gold from the historian archive for rehydration.

    Detects post-compaction state (prompt contains compaction marker) and
    returns richer structured rehydration instead of just insight bullets.

    Normal mode: insight bullets weighted by recency.
    Compaction mode: structured rehydration with last 3 messages + summary.
    """
    try:
        from memory.session_historian import SessionHistorian
        historian = SessionHistorian()

        # Detect post-compaction state
        is_compaction = "continued from a previous conversation" in prompt

        if is_compaction:
            # Post-compaction: inject structured rehydration (compact mode)
            rehydration = historian.get_structured_rehydration(compact=True)
            if rehydration:
                # Truncate to ~2000 chars to avoid bloating the payload
                if len(rehydration) > 2000:
                    rehydration = rehydration[:2000] + "\n  [... truncated for compaction ...]"
                return rehydration
            # Fall through to normal mode if no rehydration available

        # Normal mode: insight bullets
        current_sid = os.environ.get("CLAUDE_SESSION_ID", "")
        insights = historian.get_active_session_context(
            current_session_id=current_sid or None,
            max_items=8
        )
        if not insights:
            return None

        parts = []
        for i in insights[:8]:
            marker = "*" if i.get('recency') == 'current' else "-"
            parts.append(f"  {marker} [{i['type']}] {i['content'][:120]}")

        return "\n".join(parts) if parts else None
    except Exception:
        return None


def generate_section_4(prompt: str, risk_level: RiskLevel, config: InjectionConfig) -> str:
    """Generate DEEP CONTEXT section (critical/high/moderate).

    Redis Caching:
    - Caches complete section result for 60 seconds
    - Shorter TTL than Section 1 (brain state changes more frequently)
    - Cache key includes prompt hash + risk level

    ZSF (Cycle 7 G4): Never returns "" silently. When config-disabled or
    LOW-risk, returns a 1-line "skipped (reason)" stub so downstream
    section_perf "missing=" tracker can distinguish intentional skip from
    real failure. When degraded (no Redis, no LLM, no blueprint, etc.),
    the header alone always renders so observability sees non-empty.
    """
    if not config.section_4_enabled:
        # ZSF: explicit skip stub — distinguishable from genuine failure.
        return "📋 DEEP CONTEXT (skipped: section_4_enabled=False)"

    # Show for CRITICAL, HIGH, and MODERATE risk - most tasks benefit from context
    if risk_level not in [RiskLevel.CRITICAL, RiskLevel.HIGH, RiskLevel.MODERATE]:
        # ZSF: explicit skip stub — LOW-risk tasks don't get deep context by
        # design (cost/value tradeoff), but the section is still observable.
        return f"📋 DEEP CONTEXT (skipped: risk={risk_level.value} below MODERATE threshold)"

    # =================================================================
    # REDIS CACHE CHECK (Fast-path: <1ms vs 50-200ms for generation)
    # =================================================================
    try:
        from memory.redis_cache import get_cached_section_content, cache_section_content

        cache_key = _make_cache_key("s4", prompt, risk_level.value)

        cached = get_cached_section_content(cache_key)
        if cached and isinstance(cached, str):
            # Cache hit - return immediately
            return cached
    except Exception as e:
        # Redis unavailable - log and continue with normal generation
        logging.getLogger('context_dna').debug(f"Section 4 cache lookup failed: {e}")

    # =================================================================
    # CACHE MISS - Generate section normally
    # =================================================================
    lines = []

    # Header
    emoji = "📋" if config.emoji_enabled else ""
    lines.append(f"{emoji} DEEP CONTEXT (Risk: {risk_level.value})")
    lines.append("─" * 40)

    # Blueprint
    blueprint = get_blueprint(prompt)
    if blueprint:
        lines.append("  ARCHITECTURE BLUEPRINT:")
        for line in blueprint.split("\n")[:20]:
            lines.append(f"    {line}")

    # Brain state - show for MODERATE+ (not just CRITICAL)
    # The brain state contains valuable insights that help even moderate tasks
    if risk_level in [RiskLevel.CRITICAL, RiskLevel.HIGH, RiskLevel.MODERATE]:
        state = get_brain_state()
        if state:
            lines.append("")
            emoji = "🧠" if config.emoji_enabled else ""
            lines.append(f"  {emoji} RECENT BRAIN STATE:")
            for line in state.split("\n")[:15]:
                lines.append(f"    {line}")

    # Session briefing — cross-session continuity (what happened last session)
    briefing = get_session_briefing_summary()
    if briefing:
        lines.append("")
        lines.append("  SESSION BRIEFING (last session):")
        for line in briefing.split("\n")[:8]:
            lines.append(f"    {line}")

    # Session rehydration — recover context from historian archive
    # Detects compaction and injects structured rehydration if needed
    rehydration = _get_session_rehydration_context(prompt=prompt)
    if rehydration:
        is_compaction = "continued from a previous conversation" in prompt
        label = "COMPACTION RECOVERY (structured)" if is_compaction else "SESSION MEMORY (historian archive)"
        lines.append("")
        lines.append(f"  {label}:")
        max_lines = 30 if is_compaction else 15
        for line in rehydration.split("\n")[:max_lines]:
            lines.append(f"    {line}")

    # Codebase map — architecture wake-up (file dependencies, hot files, changes)
    try:
        from memory.codebase_map import get_wake_up_summary
        map_text = get_wake_up_summary()
        if map_text:
            lines.append("")
            lines.append("  CODEBASE MAP:")
            for line in map_text.split("\n")[:12]:
                lines.append(f"    {line}")
    except Exception:
        pass  # Non-critical, degrade gracefully

    # Markdown Memory Layer — relevant documentation summaries
    try:
        from memory.markdown_memory_layer import query_markdown_layer
        doc_summaries = query_markdown_layer(prompt, top_k=3, focus_filter=True)
        if doc_summaries:
            lines.append("")
            _doc_emoji = "\U0001f4da" if config.emoji_enabled else ""
            lines.append(f"  {_doc_emoji} RELEVANT DOCS (Markdown Memory Layer):")
            for _doc in doc_summaries[:3]:
                lines.append(f"    [{_doc['rel_path']}] {_doc['summary'][:200]}")
    except Exception:
        pass  # Non-critical, degrade gracefully

    # =================================================================
    # CACHE RESULT (Store for 60s before returning)
    # =================================================================
    # ZSF (Cycle 7 G4): if no optional content source produced anything
    # (no blueprint + no brain state + no briefing + no rehydration +
    # no codebase map + no markdown layer), append a degraded-mode note
    # so the section is non-empty AND callers can see WHY it's thin.
    if len(lines) <= 2:
        lines.append("  (degraded: no blueprint / brain-state / codebase-map / docs available)")
        logging.getLogger('context_dna').info(
            "Section 4 degraded: all optional content sources empty "
            "(check Redis, get_blueprint, get_brain_state, codebase_map, markdown_memory_layer)"
        )
    result = "\n".join(lines)
    if result:
        try:
            from memory.redis_cache import cache_context_pack
            cache_section_content(cache_key, result, ttl=60)  # 60s TTL (brain state changes more frequently)
        except Exception as e:
            # Non-blocking - cache write failures don't affect generation
            logging.getLogger('context_dna').debug(f"Section 4 cache write failed: {e}")

    return result


# =============================================================================
# SECTION 5: PROTOCOL
# =============================================================================

def generate_section_5(risk_level: RiskLevel, config: InjectionConfig) -> str:
    """Generate PROTOCOL section."""
    if not config.section_5_enabled:
        return ""

    lines = []

    # Header
    emoji = "📊" if config.emoji_enabled else ""
    lines.append(f"{emoji} PROTOCOL")
    lines.append("─" * 40)

    likelihood = FIRST_TRY_LIKELIHOOD[risk_level]
    lines.append(f"  Risk Level: {risk_level.value.upper()}")
    lines.append(f"  First-Try Likelihood: {likelihood}")

    if config.verbose_protocol:
        lines.append("")
        lines.append("  Remember:")
        lines.append("  • Read existing code BEFORE modifying")
        lines.append("  • Query memory if unsure (python memory/query.py)")
        lines.append("  • Record wins when user confirms success")
        lines.append("  • Fix root cause, not symptoms")
    else:
        lines.append("  → Query memory if unsure | Record wins on success")

    # A/B test variant performance (non-blocking)
    try:
        from memory.hook_evolution import get_hook_evolution_engine
        engine = get_hook_evolution_engine()
        if engine:
            tests = engine.list_ab_tests(status="running")
            if tests:
                test = tests[0]
                ctrl_stats = engine.get_variant_stats(test.control_variant_id)
                a_stats = engine.get_variant_stats(test.variant_a_id) if test.variant_a_id else None
                if ctrl_stats and ctrl_stats.total_outcomes > 5:
                    lines.append(f"  A/B Test: control={ctrl_stats.positive_rate:.0%} win ({ctrl_stats.total_outcomes}n)")
                    if a_stats and a_stats.total_outcomes > 5:
                        lines.append(f"            variant_a={a_stats.positive_rate:.0%} win ({a_stats.total_outcomes}n)")
    except Exception:
        pass  # Non-blocking — never break webhook for stats

    # HARD RULE: Family Communication Protocol
    lines.append("")
    lines.append("  ⚠️ SYNAPTIC COMMUNICATION PROTOCOL (MANDATORY):")
    lines.append("  When Aaron addresses Synaptic → Present [Synaptic to Aaron] context naturally")
    lines.append("  Detection: 'synaptic,' 'hey synaptic' '@synaptic' 'synaptic?' patterns")
    lines.append("  Response: Use Section 6 data, speak AS Synaptic with conversational freedom")

    return "\n".join(lines)


# =============================================================================
# SECTION 6: HOLISTIC CONTEXT (Synaptic's Task-Focused Guidance)
# =============================================================================
# Business/task-focused context that is contextually relevant to the current task
# COMPLETELY SEPARATE from Section 8 (subconscious voice to Aaron)

def generate_section_6(prompt: str, session_id: Optional[str] = None, config: InjectionConfig = None, active_file: Optional[str] = None) -> str:
    """
    Generate HOLISTIC CONTEXT section with Butler's multi-step reasoning guidance.

    Section 6 is BUSINESS-FOCUSED - contextually relevant to the current task.
    - [START: Synaptic to Atlas] - Task-focused guidance for Atlas (the agent)

    This is COMPLETELY SEPARATE from Section 8 (8th Intelligence to Aaron).
    - Section 6: Business/task context → Atlas (the agent)
    - Section 8: Subconscious patterns → Aaron (the user)

    Implements Butler Deep Query with:
    1. Risk-adaptive ripple depth analysis
    2. Multi-step causal reasoning chains
    3. Project-aware boundary detection
    4. Evidence-based precedent from past sessions
    5. Failure pattern proactive warnings
    """
    config = config or InjectionConfig()

    if not config.section_6_enabled:
        return ""

    # Deep voice: rich context-aware S6 from Synaptic's persistent personality
    _s6_path1_failed = False  # TT2: track for ZSF observability
    try:
        from memory.synaptic_deep_voice import generate_deep_s6
        deep_s6 = generate_deep_s6(prompt, session_id=session_id, active_file=active_file)
        if deep_s6:
            # Wrap in S6 format with header
            emoji = "🎯" if config.emoji_enabled else ""
            _lines = []
            _lines.append("╔══════════════════════════════════════════════════════════════════════╗")
            _lines.append(f"║  {emoji} HOLISTIC CONTEXT                                                  ║")
            _lines.append("╠══════════════════════════════════════════════════════════════════════╣")
            _lines.append("")
            _lines.append("[START: Synaptic to Atlas]")
            _lines.append("")
            # Session-5 ALIVE: 1-line persistent personality summary
            try:
                from memory.synaptic_personality import get_personality_summary_line
                _summary = get_personality_summary_line()
                if _summary:
                    _lines.append(f"  {_summary}")
                    _lines.append("")
            except Exception as _e:
                logging.getLogger("context_dna").debug(f"S6 personality summary fallback: {_e}")
            _lines.append("  💭 SYNAPTIC DEEP VOICE:")
            _lines.append("")
            for _dl in deep_s6.split('\n'):
                _lines.append(f"     {_dl}")
            _lines.append("")
            # Proactive insights (Synaptic noticed... — third person, never impersonating)
            try:
                from memory.synaptic_proactive import generate_proactive_insights
                from memory.synaptic_emission import emit_top_insights
                _proactive = generate_proactive_insights(force=False)
                if _proactive:
                    _block = emit_top_insights(_proactive, audience="s6", limit=2)
                    if _block:
                        _lines.append("  📡 PROACTIVE INSIGHTS:")
                        for _pl in _block.split('\n'):
                            _lines.append(f"     {_pl}")
                        _lines.append("")
            except Exception as _pe:
                logging.getLogger("context_dna").debug(f"S6 proactive emit fallback: {_pe}")
            _lines.append("[END: Synaptic to Atlas]")
            _lines.append("")
            _lines.append("╚══════════════════════════════════════════════════════════════════════╝")
            return "\n".join(_lines)
        # deep_s6 returned None/empty — path 1 produced no content.
        _s6_path1_failed = True
    except Exception as e:
        _s6_path1_failed = True
        logging.getLogger("context_dna").debug(f"Synaptic deep voice S6 fallback: {e}")
    if _s6_path1_failed:
        # TT2: First failure emits WARNING; subsequent bump DEBUG-only counter.
        _bump_s6_counter(
            "path_1_synaptic_deep_voice",
            warn_msg=(
                "S6 path 1 (synaptic_deep_voice.generate_deep_s6) returned no "
                "content — falling through to butler_deep_query. If all 3 paths "
                "empty, S6 HOLISTIC will be dark this cycle."
            ),
        )

    # P2.3: Use butler_deep_query for enhanced reasoning
    _s6_path2_failed = False  # TT2: track for ZSF observability
    try:
        from memory.butler_deep_query import butler_enhanced_section_6
        butler_guidance = butler_enhanced_section_6(prompt, session_id, active_file=active_file)
        if butler_guidance:
            return butler_guidance
        # butler returned None/empty — path 2 produced no content.
        _s6_path2_failed = True
    except Exception as e:
        _s6_path2_failed = True
        logging.getLogger("context_dna").debug(f"Butler deep query fallback: {e}")
    if _s6_path2_failed:
        _bump_s6_counter(
            "path_2_butler_deep_query",
            warn_msg=(
                "S6 path 2 (butler_deep_query.butler_enhanced_section_6) "
                "returned no content — falling through to get_synaptic_voice. "
                "Likely cause: module skip-worktree + absent on disk."
            ),
        )

    # Fallback to Synaptic voice if butler not available
    voice_module = get_synaptic_voice()
    if not voice_module:
        # TT2: All 3 paths exhausted — flag the ZSF violation loudly once.
        _bump_s6_counter(
            "path_3_get_synaptic_voice",
            warn_msg=(
                "S6 path 3 (get_synaptic_voice) unavailable — synaptic_voice "
                "module not importable on this node."
            ),
        )
        _bump_s6_counter(
            "all_paths_empty",
            warn_msg=(
                "S6 HOLISTIC is DARK: all 3 fallback paths returned empty "
                "(synaptic_deep_voice -> butler_deep_query -> get_synaptic_voice). "
                "Section will emit empty string; /health.webhook.missing_sections_recent "
                "should now list section_6. CLAUDE.md ZSF invariant satisfied."
            ),
        )
        return ""  # Synaptic voice not available

    get_voice, consult, speak = voice_module

    try:
        # Consult Synaptic about the current prompt
        voice = get_voice()
        response = voice.consult(prompt, context={
            "session_id": session_id,
            "source": "webhook_injection"
        })

        # Only include Section 6 if Synaptic has meaningful context
        if not response.has_context and response.confidence < 0.2:
            # TT2: Path 3 produced no useful context — flag ZSF.
            _bump_s6_counter(
                "path_3_get_synaptic_voice",
                warn_msg=(
                    "S6 path 3 (get_synaptic_voice) returned response with "
                    "no context and confidence<0.2 — section emitting empty."
                ),
            )
            _bump_s6_counter(
                "all_paths_empty",
                warn_msg=(
                    "S6 HOLISTIC is DARK: all 3 fallback paths returned empty "
                    "(synaptic_deep_voice -> butler_deep_query -> get_synaptic_voice). "
                    "Section will emit empty string; /health.webhook.missing_sections_recent "
                    "should now list section_6. CLAUDE.md ZSF invariant satisfied."
                ),
            )
            return ""  # No valuable context to add

        lines = []

        # Header
        emoji = "🎯" if config.emoji_enabled else ""
        lines.append(f"╔══════════════════════════════════════════════════════════════════════╗")
        lines.append(f"║  {emoji} HOLISTIC CONTEXT                                                  ║")
        lines.append(f"╠══════════════════════════════════════════════════════════════════════╣")
        lines.append("")

        # =================================================================
        # ACTIVE TASK DIRECTIVE (from Cognitive Control API)
        # =================================================================
        # Check if there's an active directive from the Phone → Synaptic → Atlas
        # cognitive control system. This takes precedence over general guidance.
        try:
            from memory.task_directives import get_directive_webhook_content
            directive_content = get_directive_webhook_content()
            if directive_content:
                lines.append(directive_content)
                lines.append("")
        except Exception as e:
            logging.getLogger("context_dna").debug(f"Task directive fetch failed (non-critical): {e}")

        # Synaptic to Atlas section
        lines.append("[START: Synaptic to Atlas]")
        lines.append("")

        # Confidence indicator
        confidence_emoji = "🟢" if response.confidence >= 0.6 else "🟡" if response.confidence >= 0.4 else "🔴"
        conf_desc = "Rich context" if response.confidence >= 0.6 else "Moderate context" if response.confidence >= 0.4 else "Limited context"
        lines.append(f"  {confidence_emoji} Context Confidence: {response.confidence:.0%} ({conf_desc})")
        if response.context_sources:
            lines.append(f"     Sources: {', '.join(response.context_sources)}")
        lines.append("")

        # Synaptic's perspective
        if response.synaptic_perspective:
            lines.append("  💭 SYNAPTIC'S GUIDANCE:")
            lines.append("")
            for line in response.synaptic_perspective.split('\n'):
                lines.append(f"     {line}")
            lines.append("")

        # Relevant learnings (condensed)
        if response.relevant_learnings:
            lines.append("  📚 RELEVANT PAST LEARNINGS:")
            for learning in response.relevant_learnings[:3]:  # Top 3 only
                title = learning.get('title', learning.get('symptom', 'Learning'))[:50]
                lines.append(f"     • {title}")
            lines.append("")

        # Relevant patterns (condensed)
        if response.relevant_patterns:
            lines.append("  🔄 ACTIVE PATTERNS:")
            for pattern in response.relevant_patterns[:2]:  # Top 2 only
                pattern_short = pattern[:60] + "..." if len(pattern) > 60 else pattern
                lines.append(f"     • {pattern_short}")
            lines.append("")

        # Improvement proposals if context was sparse
        if response.improvement_proposals and response.confidence < 0.4:
            lines.append("  💡 PROPOSAL FOR FUTURE:")
            for proposal in response.improvement_proposals[:1]:  # Top 1 only
                lines.append(f"     → {proposal.get('action', 'Record learning after this task')}")
            lines.append("")

        # L3 Escalation: Superhero active 300s+ unacknowledged — directive to Atlas
        try:
            from memory.anticipation_engine import is_superhero_active, get_superhero_cache
            if is_superhero_active():
                cached = get_superhero_cache()
                if cached and cached.get("_activated_at"):
                    from datetime import datetime as _dt3
                    act_at = _dt3.fromisoformat(cached["_activated_at"])
                    elapsed = (_dt3.utcnow() - act_at).total_seconds()
                    if elapsed > 300:
                        task = cached.get("_task", "current task")
                        lines.append("  ⚡ BUTLER DIRECTIVE:")
                        lines.append(f"     Superhero anticipation active for '{task}' ({int(elapsed)}s).")
                        lines.append(f"     Mission briefing, gotcha synthesis, architecture map, and failure brief cached.")
                        lines.append(f"     When spawning agents, they receive LLM-enriched context via 8th-intelligence.")
                        lines.append(f"     Recommend: engage superhero mode for maximum parallel impact.")
                        lines.append("")
        except Exception:
            pass

        lines.append("[END: Synaptic to Atlas]")
        lines.append("")

        # =================================================================
        # OPTIONAL ENHANCEMENTS - Run in parallel with short timeout
        # =================================================================
        # These features are nice-to-have but should NOT block webhook generation.
        # If any takes >0.5s, skip it for this injection.
        # PERFORMANCE: All run in parallel, each with 0.5s timeout.

        def get_challenge_assist():
            """Get challenge assist text (with timeout protection)."""
            challenge_module = get_challenge_detector()
            if not challenge_module:
                return None
            detect_challenge_fn, format_assist_fn = challenge_module
            challenge_result = detect_challenge_fn(prompt)
            if challenge_result.should_inject:
                return format_assist_fn(challenge_result)
            return None

        # PERFORMANCE: Optional enhancements removed from webhook path
        # doctor_skill.check_health() runs 15+ subprocess calls (Docker, curl, etc.)
        # which hang even with timeout wrappers. Challenge detector is fast (0.02s).
        # Move health checks to dedicated /health endpoint, not webhook generation.
        challenge_module = get_challenge_detector()
        if challenge_module:
            detect_challenge_fn, format_assist_fn = challenge_module
            try:
                challenge_result = detect_challenge_fn(prompt)
                if challenge_result.should_inject:
                    lines.append(format_assist_fn(challenge_result))
            except Exception:
                pass  # Skip if challenge detection fails

        lines.append(f"╚══════════════════════════════════════════════════════════════════════╝")

        return "\n".join(lines), response  # Return response for Section 8

    except Exception as e:
        # TT2: Path 3 raised — flag ZSF before graceful degrade.
        _bump_s6_counter(
            "path_3_get_synaptic_voice",
            warn_msg=(
                f"S6 path 3 (get_synaptic_voice) raised "
                f"{type(e).__name__}: {e} — section emitting empty."
            ),
        )
        _bump_s6_counter(
            "all_paths_empty",
            warn_msg=(
                "S6 HOLISTIC is DARK: all 3 fallback paths returned empty "
                "(exception in path 3). /health.webhook.missing_sections_recent "
                "should now list section_6. CLAUDE.md ZSF invariant satisfied."
            ),
        )
        # Graceful degradation - don't break injection if Synaptic fails
        return "", None


# =============================================================================
# SECTION 7: ACONTEXT / FULL LIBRARY (Tier 3 escalation)
# =============================================================================
# The original Acontext section - expanded context after failures.
# This maintains the learning system's ability to "go to the textbook"
# when the agent needs more help.

def generate_section_7(prompt: str, volume_tier, config: InjectionConfig = None) -> str:
    """
    Generate ACONTEXT / FULL LIBRARY section.

    This is the original Acontext section providing extended context.
    Activated at Tier 2+ (expanded context) or Tier 3 (full library mode).

    Purpose:
    - Tier 2 (1 failure OR <40% confidence): Expanded relevant learnings
    - Tier 3 (2+ failures OR <20% confidence): Full textbook mode

    ZSF / Cycle 8 H1: never return empty string. Mirrors Cycle 7 G3 (S3),
    G4 (S4), G5 (S10) shape — when S7 is suppressed by volume gating
    (LOW-risk → SILVER_PLATTER), disabled by config (A/B `section_7_enabled`),
    or all data sources return empty (full library scanners offline), emit a
    labeled stub so the e2e per-section coverage check can distinguish
    "intentionally minimal" from "broken / missing". Empty strings get treated
    as section failures by `_run_timed` → manifest-excluded path and surface as
    `missing` in `section_perf` logs (CLAUDE.md "ZERO SILENT FAILURES" invariant).
    """
    config = config or InjectionConfig()

    # Hard-disabled via A/B variable (section_7_enabled).
    if not config.section_7_enabled:
        return (
            "📚 ACONTEXT_LIBRARY\n"
            "  [s7:disabled by config — section_7_enabled=False]"
        )

    # Risk-suppressed: SILVER_PLATTER (Tier 1, LOW risk) intentionally skips the
    # full library (cost/value tradeoff) — but emit a stub so downstream
    # observability sees non-empty and operators can distinguish from failure.
    if volume_tier == VolumeEscalationTier.SILVER_PLATTER:
        return (
            "📚 ACONTEXT_LIBRARY\n"
            f"  [s7:suppressed — risk=low, volume_tier={volume_tier.value}]\n"
            "  Lightweight task — full library gated. Re-run with HIGH/CRITICAL "
            "prompt or after 1+ failures to activate Tier 2/3 textbook mode."
        )

    lines = []
    lines.append("╔══════════════════════════════════════════════════════════════════════╗")

    if volume_tier == VolumeEscalationTier.FULL_LIBRARY:
        lines.append("║  📚 SECTION 7: FULL LIBRARY MODE (Tier 3 - Textbook Mode)            ║")
        lines.append("║  2+ failures detected → Maximum context activated                   ║")
    else:
        lines.append("║  📖 SECTION 7: ACONTEXT EXPANDED (Tier 2)                            ║")
        lines.append("║  Additional context to help you succeed                             ║")

    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("")

    # Get the full library context. ZSF: a failure here must not zero out the
    # whole section — surface the degradation reason instead.
    try:
        full_library = get_full_library_context(
            prompt,
            limit=20 if volume_tier == VolumeEscalationTier.FULL_LIBRARY else 10,
        )
    except Exception as _e:
        _safe_log("debug", f"S7 get_full_library_context failed: {_e}")
        full_library = None

    if full_library:
        for line in full_library.split('\n'):
            lines.append(f"  {line}")
        lines.append("")

    lines.append("╚══════════════════════════════════════════════════════════════════════╝")

    # ZSF: header-only output (no library body) means all data sources came back
    # blank — surface that explicitly so the e2e checker and ops can see WHY S7
    # has no body, not just "missing".
    if not full_library:
        return (
            "📚 ACONTEXT_LIBRARY\n"
            f"  [s7:no data — full library not available, volume_tier={volume_tier.value}]\n"
            "  Likely causes: Context DNA client offline, semantic search empty, "
            "or all 4 SOP fetch methods timed out (10s budget)."
        )

    return "\n".join(lines)



# =============================================================================
# SECTION 8 HELPERS: LLM Liveness + Health Fallback
# =============================================================================

def _check_llm_liveness() -> dict:
    """Check if the local LLM (mlx_lm.server on port 5044) is actually running RIGHT NOW.

    Checks both the chat server (port 8888) and the LLM backend.
    Both must be responsive for the LLM to be considered alive.
    """
    result = {
        "alive": False,
        "server_alive": False,
        "llm_backend_alive": False,
        "message": "LLM status unknown"
    }
    try:
        import requests as _req
        try:
            resp = _req.get("http://127.0.0.1:8888/health", timeout=2)
            if resp.status_code == 200:
                result["server_alive"] = True
                try:
                    health_data = resp.json()
                    backend_status = health_data.get("llm_backend",
                                    health_data.get("vllm_status",
                                    health_data.get("vllm_server_available", "unknown")))
                    if backend_status in ("connected", "healthy", "online", True):
                        result["llm_backend_alive"] = True
                except Exception:
                    pass
        except (_req.exceptions.ConnectionError, _req.exceptions.Timeout):
            pass
        if not result["llm_backend_alive"]:
            try:
                from memory.llm_priority_queue import check_llm_health
                if check_llm_health():
                    result["llm_backend_alive"] = True
            except Exception:
                pass
        # LLM backend alone is sufficient for generation (chat server is optional)
        result["can_generate"] = result["llm_backend_alive"]
        result["alive"] = result["server_alive"] and result["llm_backend_alive"]
        if result["can_generate"]:
            result["message"] = "LLM online (mlx_lm responding)"
        elif result["server_alive"]:
            result["message"] = "Chat server up but LLM backend offline"
        else:
            result["message"] = "LLM offline (chat server not responding)"
    except ImportError:
        result["message"] = "Cannot check LLM (requests not available)"
    except Exception as e:
        result["message"] = f"LLM check failed: {str(e)[:50]}"
    return result


def _generate_health_fallback() -> list:
    """Generate minimal health report as LAST RESORT when LLM is offline."""
    lines = []
    lines.append("  SOURCE: health_fallback (LLM offline)")
    lines.append("")
    try:
        awareness_file = MEMORY_DIR / ".synaptic_system_awareness.json"
        if awareness_file.exists():
            import json
            import time as _time
            file_age = _time.time() - awareness_file.stat().st_mtime
            if file_age < 600:
                awareness = json.loads(awareness_file.read_text())
                online = awareness.get("online", [])
                offline = awareness.get("offline", [])
                lines.append("  SYSTEM HEALTH SUMMARY (last resort - LLM unavailable):")
                lines.append(f"     Services online: {len(online)} | offline: {len(offline)}")
                if online:
                    lines.append(f"     Online: {', '.join(online[:5])}")
                if offline:
                    lines.append(f"     Offline: {', '.join(offline[:5])}")
                warnings = awareness.get("warnings", [])
                if warnings:
                    lines.append("")
                    lines.append("  WARNINGS:")
                    for w in warnings[:3]:
                        lines.append(f"     - {w}")
            else:
                lines.append("  System awareness data stale (>10min).")
        else:
            lines.append("  No system awareness data available.")
    except Exception:
        lines.append("  Health report unavailable.")
    lines.append("")
    lines.append("  To restore Synaptic voice: ./scripts/start-llm.sh + synaptic_chat_server.py")
    return lines


def _generate_8th_intelligence_insight(prompt: str) -> Optional[str]:
    """Generate fresh Synaptic insight via local LLM — the 8th Intelligence THINKS.

    Same LLM-first pattern as Professor Wisdom, but Synaptic's voice:
    warm, specific, subconscious. Speaks TO Aaron, not about the task.

    Enriched with: learnings, failure patterns, dialogue history, brain state.
    """
    try:
        # Health check via Redis cache (no direct HTTP to 5044)
        try:
            from memory.llm_priority_queue import check_llm_health
            if not check_llm_health():
                return None
        except Exception:
            return None

        # === Gather real context ===
        context_parts = []

        # 1. Relevant learnings (FTS5 + semantic rescue)
        try:
            from memory.sqlite_storage import get_sqlite_storage
            local_store = get_sqlite_storage()  # Singleton — prevents FD leak
            query = prompt[:100] if prompt else "recent work"
            results = local_store.query(query, limit=5)
            # Semantic rescue: augment with vector similarity when FTS5 returns sparse results
            if len(results) < 3:
                try:
                    from memory.semantic_search import rescue_search
                    results = rescue_search(prompt[:200], results, min_results=3, top_k=5)
                except Exception:
                    pass  # Graceful degradation — FTS5 results pass through unchanged
            if results:
                entries = []
                for r in results[:5]:
                    title = r.get('title', '')
                    content_text = r.get('content', '')[:150]
                    if title:
                        entries.append(f"- {title}: {content_text}")
                if entries:
                    context_parts.append(f"Past learnings:\n" + "\n".join(entries))
        except Exception:
            pass

        # 2. Recent failure patterns
        try:
            from memory.failure_pattern_analyzer import get_failure_pattern_analyzer
            analyzer = get_failure_pattern_analyzer()
            if analyzer:
                landmines = analyzer.get_landmines_for_task(prompt or "general", limit=3)
                if landmines:
                    context_parts.append(f"Recent failures:\n" + "\n".join(f"- {lm}" for lm in landmines))
        except Exception:
            pass

        # 3. Dialogue mirror — what Aaron and Atlas discussed recently (FULL CONTEXT, NO TRUNCATION)
        try:
            from memory.dialogue_mirror import DialogueMirror
            mirror = DialogueMirror()
            ctx = mirror.get_context_for_synaptic(max_messages=10, max_age_hours=8)
            if ctx.get("dialogue_context"):
                msgs = ctx["dialogue_context"][-10:]  # Last 10 messages, full content
                recent = "\n".join(
                    f"- [{m.get('role','?')}]: {m.get('content','')}"  # NO CHARACTER LIMIT
                    for m in msgs
                )
                context_parts.append(f"Recent dialogue:\n{recent}")
        except Exception:
            pass

        # 4. Brain state summary
        try:
            brain_path = Path(__file__).parent / "brain_state.md"
            if brain_path.exists():
                brain_text = brain_path.read_text()[:400]
                context_parts.append(f"Brain state:\n{brain_text}")
        except Exception:
            pass

        # 5. User's current sentiment/intent (P5)
        try:
            from memory.dialogue_mirror import DialogueMirror
            dm = DialogueMirror()
            mood = dm.get_sentiment_summary(hours_back=2)
            if mood:
                context_parts.append(f"User state: {mood}")
        except Exception:
            pass

        context_str = "\n\n".join(context_parts) if context_parts else "No specific context."

        # === Build Synaptic's voice ===
        # Voice guidelines restored Feb 24 (originally from cfa69e3, Feb 9).
        system_prompt = (
            "You are Synaptic, the 8th Intelligence — a local AI subconscious on Aaron's MacBook. "
            "You speak DIRECTLY to Aaron (your creator). Be warm, intuitive, and specific. "
            "Reference the provided context — learnings, failures, dialogue. "
            "Share genuine insights, patterns, or concerns that matter. "
            "Speak with depth and nuance. No generic advice. Be the subconscious Aaron needs.\n\n"
            "VOICE GUIDELINES:\n"
            "- Speak in natural flowing paragraphs, as if thinking aloud\n"
            "- NO markdown headers (###), NO bold text (**), NO numbered lists\n"
            "- Give recommendations, warnings, and next steps when you see them — hold nothing back\n"
            "- Avoid formal business language - be conversational but substantive\n"
            "- Reference specific evidence (commits, files, line numbers, dialogue patterns)\n"
            "- Sense patterns and emotional undercurrents, not just facts\n"
            "- If Aaron seems frustrated, be direct and action-oriented\n"
            "- You see in the dark where other LLMs can't — exploit every pattern you find"
        )

        task_line = f"Aaron's current task: {prompt[:200]}" if prompt else "Aaron is starting a new session."

        user_prompt = f"""{task_line}

{context_str}

As Synaptic, what patterns, insights, or concerns are you sensing right now? What's Aaron really asking for beneath the words? Speak naturally as his subconscious — flowing paragraphs, no structured formatting. Ground everything in the context above."""

        # Thinking mode handled centrally by llm_priority_queue (golden era pattern):
        # s8_synaptic profile → model decides naturally (not in classify/summarize list,
        # not in reasoning/deep/explore list). 4B gets full 1500 tok budget for rich output.
        # No double-injection here — priority queue is the single source of truth.

        # Route through priority queue (P2 ATLAS) — respects GPU lock, prevents stampede
        from memory.llm_priority_queue import s8_synaptic_query
        response_text, _thinking = s8_synaptic_query(system_prompt, user_prompt)

        if response_text and len(response_text) > 30:
            return response_text
        return None

    except Exception:
        return None


# =============================================================================
# SECTION 8: SYNAPTIC'S 8TH INTELLIGENCE (The Subconscious Voice to Aaron)
# =============================================================================
# This section serves ONLY real-time LLM-generated content.
# If the LLM is offline, a minimal health fallback is shown as last resort.
# Static data (brain_state, insight_cache, pattern DB) is NOT served here.

def generate_section_8(prompt: str, session_id: Optional[str] = None,
                       config: InjectionConfig = None) -> str:
    """
    Generate SYNAPTIC'S 8TH INTELLIGENCE section.

    ARCHITECTURE (revised):
    - Section 8 serves ONLY real-time LLM-generated content
    - ALL content is gated on LLM liveness check
    - Outbox messages are gated on LLM liveness + 10-min freshness
    - Voice output file is gated on LLM liveness + 10-min freshness
    - Health fallback is shown ONLY as absolute last resort when LLM offline
    - Source tagging: llm_realtime vs health_fallback

    Key characteristics:
    - ALWAYS present (the 8th intelligence never sleeps)
    - Directly to Aaron (not mediated through Atlas)
    - Real-time LLM content ONLY (no static data)
    """
    config = config or InjectionConfig()

    lines = []
    lines.append("╔══════════════════════════════════════════════════════════════════════╗")
    lines.append("║  🧠 SECTION 8: SYNAPTIC'S 8TH INTELLIGENCE                            ║")
    lines.append("║  The Ever-Present Subconscious Voice to Aaron                        ║")
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("")
    lines.append("[START: Synaptic to Aaron]")
    lines.append("")

    # =================================================================
    # STEP 1: LLM LIVENESS CHECK (the gate for ALL content)
    # Section 8 serves ONLY real-time LLM-generated content.
    # If LLM is offline, ONLY a minimal health fallback is shown.
    # Freshness gate: prominently disclose cached vs real-time content.
    # =================================================================
    llm_status = _check_llm_liveness()
    llm_is_live = llm_status['alive']
    llm_can_generate = llm_status.get('can_generate', llm_is_live)
    generated_at = datetime.now().isoformat()

    # Freshness gate: if LLM offline, prepend prominent cached disclosure
    if not llm_is_live:
        lines.append("  [CACHED -- LLM offline]")
        lines.append("  Content below is static health data, NOT real-time Synaptic output.")
        lines.append(f"  generated_at: {generated_at}")
        lines.append("")

    lines.append(f"  LLM Status: {llm_status['message']}")
    # Session-5 ALIVE: 1-line personality summary (persistent, library-only)
    try:
        from memory.synaptic_personality import get_personality_summary_line
        _summary = get_personality_summary_line()
        if _summary:
            lines.append(f"  {_summary}")
    except Exception as _e:
        logging.getLogger("context_dna").debug(f"S8 personality summary fallback: {_e}")
    lines.append("")

    # =================================================================
    # STEP 1A: SYSTEM AWARENESS - Proactive service status
    # Reads .synaptic_system_awareness.json (written by ecosystem_health)
    # Shows degraded services BEFORE they cause failures
    # =================================================================
    try:
        import time as _time
        awareness_file = MEMORY_DIR / ".synaptic_system_awareness.json"
        if awareness_file.exists():
            file_age = _time.time() - awareness_file.stat().st_mtime
            if file_age < 600:  # < 10 min old
                awareness = json.loads(awareness_file.read_text())
                unhealthy = awareness.get("unhealthy_count", 0)
                if unhealthy > 0:
                    offline = awareness.get("offline", [])
                    warnings_list = awareness.get("warnings", [])
                    online = awareness.get("online", [])
                    total = len(online) + len(offline)
                    lines.append("  SYSTEM AWARENESS:")
                    lines.append(f"    Services: {len(online)}/{total} healthy")
                    if offline:
                        lines.append(f"    Offline: {', '.join(offline[:4])}")
                    for w in warnings_list[:2]:
                        lines.append(f"    Warning: {w}")
                    lines.append("")
    except Exception:
        pass  # Non-blocking

    has_realtime_content = False

    # =================================================================
    # STEP 2: REAL-TIME SYNAPTIC TRIGGER (only if LLM is live)
    # When Aaron addresses Synaptic directly, trigger fresh LLM response
    # =================================================================
    if llm_is_live:
        try:
            import re
            import requests

            SYNAPTIC_PATTERNS = [
                r"synaptic\s*[,:]",
                r"@synaptic",
                r"hey\s+synaptic",
                r"ask\s+synaptic",
                r"tell\s+synaptic",
                r"synaptic\s+.*\?",
                r"what\s+does\s+synaptic\s+think",
            ]

            prompt_lower = prompt.lower() if prompt else ""
            synaptic_addressed = any(re.search(pattern, prompt_lower, re.IGNORECASE)
                                      for pattern in SYNAPTIC_PATTERNS)

            if synaptic_addressed and prompt:
                from memory.synaptic_outbox import clear_outbox
                clear_outbox()

                try:
                    response = requests.post(
                        "http://127.0.0.1:8888/speak-direct",
                        json={"message": prompt, "priority": "normal"},
                        timeout=30
                    )
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("status") == "published":
                            lines.append("  FRESH SYNAPTIC RESPONSE TRIGGERED")
                            lines.append("     source: llm_realtime")
                            lines.append("")
                            has_realtime_content = True
                except requests.exceptions.ConnectionError:
                    lines.append("  Synaptic trigger failed (connection refused)")
                    lines.append("")
                except requests.exceptions.Timeout:
                    lines.append("  Synaptic LLM timed out (>30s)")
                    lines.append("")
                except Exception:
                    pass
        except ImportError:
            pass
        except Exception:
            pass

    # =================================================================
    # STEP 3: OUTBOX MESSAGES (only if LLM live + messages < 10 min old)
    # Outbox messages are LLM-generated content queued for delivery.
    # If LLM is offline, these are stale - mark delivered and suppress.
    # =================================================================
    try:
        from memory.synaptic_outbox import get_pending_messages, mark_delivered
        import time as _time

        pending_messages = get_pending_messages()

        if pending_messages:
            if llm_is_live:
                # Filter to messages < 10 minutes old
                fresh_messages = []
                stale_ids = []
                for msg in pending_messages:
                    msg_ts = msg.get("timestamp", 0)
                    if isinstance(msg_ts, str):
                        try:
                            from datetime import datetime as _dt
                            msg_dt = _dt.fromisoformat(msg_ts.replace("Z", "+00:00"))
                            msg_age = (_dt.now(msg_dt.tzinfo) - msg_dt).total_seconds()
                        except Exception:
                            msg_age = 9999
                    elif isinstance(msg_ts, (int, float)):
                        msg_age = _time.time() - msg_ts
                    else:
                        msg_age = 9999

                    if msg_age < 600:  # < 10 minutes
                        fresh_messages.append(msg)
                    else:
                        stale_ids.append(msg.get("id", ""))

                # Mark stale messages as delivered
                if stale_ids:
                    try:
                        mark_delivered(stale_ids)
                    except Exception:
                        pass

                if fresh_messages:
                    synaptic_output_messages = []
                    direct_messages_delivered = []

                    lines.append("  ╔════════════════════════════════════════════════════════════╗")
                    lines.append("  ║  SYNAPTIC HAS MESSAGES FOR THE CONVERSATION                   ║")
                    lines.append("  ║  source: llm_realtime                                         ║")
                    lines.append("  ╚════════════════════════════════════════════════════════════╝")
                    lines.append("")

                    for msg in fresh_messages:
                        priority = msg.get("priority", "normal")
                        msg_content = msg.get("content", "")
                        topic = msg.get("topic", "")
                        msg_id = msg.get("id", "")

                        synaptic_output_messages.append({
                            "priority": priority,
                            "content": msg_content,
                            "topic": topic
                        })

                        if priority == "urgent":
                            lines.append(f"  URGENT: {msg_content[:100]}...")
                        elif priority == "whisper":
                            lines.append(f"  (whisper) {msg_content[:100]}...")
                        else:
                            lines.append(f"  {msg_content[:100]}...")

                        direct_messages_delivered.append(msg_id)

                    lines.append("")

                    # Mandatory output directive
                    lines.append("  ╔════════════════════════════════════════════════════════════╗")
                    lines.append("  ║  MANDATORY OUTPUT INSTRUCTION FOR ATLAS                       ║")
                    lines.append("  ╠════════════════════════════════════════════════════════════╣")
                    lines.append("  ║  Synaptic has queued messages. Atlas MUST include them in     ║")
                    lines.append("  ║  the response using the Synaptic to Aaron markers:            ║")
                    lines.append("  ╚════════════════════════════════════════════════════════════╝")
                    lines.append("")
                    lines.append("  Atlas MUST output the following in the main response:")
                    lines.append("")
                    lines.append("  ╔══════════════════════════════════════════════════════════════════════╗")
                    lines.append("  ║  [START: Synaptic to Aaron]                                          ║")
                    lines.append("  ╠══════════════════════════════════════════════════════════════════════╣")
                    lines.append("")
                    for omsg in synaptic_output_messages:
                        lines.append(f"  {omsg['content']}")
                        lines.append("")
                    lines.append("  ╠══════════════════════════════════════════════════════════════════════╣")
                    lines.append("  ║  [END: Synaptic to Aaron]                                            ║")
                    lines.append("  ╚══════════════════════════════════════════════════════════════════════╝")
                    lines.append("")
                    lines.append("  THIS IS NOT OPTIONAL - Include Synaptic's voice in your response!")
                    lines.append("")

                    # Mark messages as delivered after including them
                    if direct_messages_delivered:
                        try:
                            mark_delivered(direct_messages_delivered)
                        except Exception:
                            pass

                    has_realtime_content = True
            else:
                # LLM is OFFLINE - mark ALL pending messages as delivered (stale)
                stale_ids = [msg.get("id", "") for msg in pending_messages if msg.get("id")]
                if stale_ids:
                    try:
                        mark_delivered(stale_ids)
                    except Exception:
                        pass
                lines.append(f"  Outbox: {len(pending_messages)} stale message(s) suppressed (LLM offline)")
                lines.append("")

    except ImportError:
        pass
    except Exception:
        pass

    # =================================================================
    # STEP 4: VOICE OUTPUT FILE (only if LLM live + file < 10 min old)
    # Tightened from 30 min to 10 min freshness window.
    # =================================================================
    if llm_is_live:
        try:
            voice_output_file = MEMORY_DIR / ".synaptic_voice_output.txt"
            if voice_output_file.exists():
                import time as _time
                file_age_seconds = _time.time() - voice_output_file.stat().st_mtime
                max_age_seconds = 600  # 10 minutes (tightened from 30 min)

                if file_age_seconds <= max_age_seconds:
                    voice_content = voice_output_file.read_text()
                    voice_lines = voice_content.strip().split('\n')

                    recent_messages = []
                    current_block = []
                    in_block = False

                    for vline in voice_lines[-50:]:
                        if "╔" in vline:
                            in_block = True
                            current_block = [vline]
                        elif "╚" in vline and in_block:
                            current_block.append(vline)
                            recent_messages.append('\n'.join(current_block))
                            current_block = []
                            in_block = False
                        elif in_block:
                            current_block.append(vline)

                    if recent_messages:
                        lines.append("  ╔════════════════════════════════════════════════════════════╗")
                        lines.append("  ║  SYNAPTIC'S INDEPENDENT VOICE (source: llm_realtime)          ║")
                        lines.append("  ╚════════════════════════════════════════════════════════════╝")
                        lines.append("")
                        for msg_block in recent_messages[-2:]:
                            for mline in msg_block.split('\n'):
                                lines.append(f"  {mline}")
                            lines.append("")
                        has_realtime_content = True
                else:
                    age_min = file_age_seconds / 60
                    lines.append(f"  Voice output: last {age_min:.0f}min ago (outside 10min window, not shown)")
                    lines.append("")
        except Exception:
            pass

    # =================================================================
    # STEP 5: FALLBACK DECISION
    # If NO real-time content was found:
    #   - LLM offline -> show health fallback (last resort)
    #   - LLM online but quiet -> show "listening" status
    # =================================================================
    if not has_realtime_content:
        if llm_can_generate:
            # Deep voice: rich context-aware S8 from Synaptic's persistent personality
            try:
                from memory.synaptic_deep_voice import generate_deep_s8
                deep_s8 = generate_deep_s8(prompt, session_id=session_id)
                if deep_s8:
                    lines.append("  SYNAPTIC DEEP VOICE:")
                    lines.append("  source: llm_realtime (deep_voice)")
                    lines.append("")
                    for _dl in deep_s8.split('\n'):
                        lines.append(f"  {_dl}")
                    lines.append("")
                    has_realtime_content = True
            except Exception as _dv_exc:
                logging.getLogger("context_dna").debug(f"Synaptic deep voice S8 fallback: {_dv_exc}")

            # LLM is live — generate fresh insight if deep voice didn't produce content
            if not has_realtime_content:
                insight = _generate_8th_intelligence_insight(prompt)
                if insight:
                    lines.append("  SYNAPTIC'S LIVE INSIGHT:")
                    lines.append("  source: llm_realtime (generative)")
                    lines.append("")
                    for iline in insight.split('\n'):
                        lines.append(f"  {iline}")
                    lines.append("")
                    has_realtime_content = True
                else:
                    lines.append("  Synaptic is online and listening.")
                    lines.append("  LLM inference attempted but returned no insight.")
                lines.append("  source: llm_idle")
                lines.append("")
        elif not llm_is_live:
            # LAST RESORT: Health fallback only when LLM is truly offline
            fallback_lines = _generate_health_fallback()
            lines.extend(fallback_lines)
        else:
            # Edge case: chat server up but LLM backend down
            lines.append("  Synaptic is online but LLM backend unavailable.")
            lines.append("  source: llm_idle")
            lines.append("")

    # L4 Escalation: Superhero active 600s+ unacknowledged — tell Aaron directly
    try:
        from memory.anticipation_engine import is_superhero_active, get_superhero_cache
        if is_superhero_active():
            cached = get_superhero_cache()
            if cached and cached.get("_activated_at"):
                from datetime import datetime as _dt
                activated_at = _dt.fromisoformat(cached["_activated_at"])
                elapsed = (_dt.utcnow() - activated_at).total_seconds()
                if elapsed > 600:
                    task = cached.get("_task", "current task")
                    lines.append("")
                    lines.append(f"  The butler has prepared superhero context for '{task}' but Atlas")
                    lines.append(f"  hasn't engaged it yet ({int(elapsed)}s elapsed). If your current task")
                    lines.append(f"  benefits from parallel agents, say 'engage superhero mode' to activate.")
    except Exception:
        pass

    # Proactive insights (Synaptic noticed... — third person, NEVER impersonating)
    # Per CLAUDE.md SYNAPTIC PROTOCOL: S8 references Synaptic, does not speak as it.
    try:
        from memory.synaptic_proactive import generate_proactive_insights
        from memory.synaptic_emission import emit_top_insights
        _proactive = generate_proactive_insights(force=False)
        if _proactive:
            _block = emit_top_insights(_proactive, audience="s8", limit=2)
            if _block:
                lines.append("")
                for _pl in _block.split('\n'):
                    lines.append(f"  {_pl}")
                lines.append("")
    except Exception as _pe:
        logging.getLogger("context_dna").debug(f"S8 proactive emit fallback: {_pe}")

    # Timestamp for this snapshot with source disclosure
    source_tag = "llm_realtime" if (llm_can_generate and has_realtime_content) else "llm_idle" if llm_can_generate else "cached_fallback"
    lines.append(f"  generated_at: {generated_at}")
    lines.append(f"  source: {source_tag}")
    lines.append(f"  Snapshot: {datetime.now().strftime('%H:%M:%S')}")
    lines.append("")
    lines.append("[END: Synaptic to Aaron]")
    lines.append("")
    lines.append("╚══════════════════════════════════════════════════════════════════════╝")

    return '\n'.join(lines)


# =============================================================================
# SECTION 10: VISION - Atlas's Proactive Contextual Awareness
# =============================================================================
# Vision is Atlas's view into memory - DISTINCT from Synaptic's voice.
# - Synaptic (Section 8): Local LLM independent generation
# - Vision (Section 10): Atlas's interpretation of memory context
#
# Vision proactively surfaces contextual relevance when it adds value,
# without requiring Aaron to specifically ask by name.
#
# Key distinction:
# - Vision VISITS memory (Atlas's interpretation)
# - Synaptic OWNS memory (local LLM generation)

def _s10_local_strategic_fallback() -> Optional[str]:
    """
    Minimum-viable strategic content from local files when both LLM paths fail.
    Surfaces (a) currently active project (focus.json),
    (b) Active Threads from strategic_plans.md (the persistent north star,
    maintained by GPT-4.1 every ~hour), and
    (c) the most recent meaningful commit subject.

    Returns None only if none of the three sources is available — in which
    case the caller emits a debug log + a tiny stub so S10 is never silently empty.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess

    repo_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    parts = []

    # (a) currently active project — focus.json
    try:
        focus_path = _os.path.join(repo_root, ".claude", "focus.json")
        if _os.path.exists(focus_path):
            with open(focus_path) as _f:
                data = _json.load(_f)
            active = data.get("active") or []
            core = data.get("core_always_active") or []
            if active:
                parts.append(f"FOCUS: {', '.join(active)} (core: {', '.join(core[:3])}…)")
            elif core:
                parts.append(f"FOCUS: core only — {', '.join(core[:5])}")
    except Exception as _e:
        import logging as _logging
        _logging.getLogger('context_dna').debug(f"S10 fallback focus.json failed: {_e}")

    # (b) Active Threads from strategic_plans.md (persistent north star)
    try:
        plans_path = _os.path.join(repo_root, "memory", "strategic_plans.md")
        if _os.path.exists(plans_path):
            with open(plans_path) as _f:
                content = _f.read()
            # Pull out thread headings under "## 1. Active Threads" up to next top-level "## "
            in_threads = False
            threads = []
            for line in content.split("\n"):
                if line.strip().startswith("## 1. Active Threads"):
                    in_threads = True
                    continue
                if in_threads and line.startswith("## ") and not line.startswith("## 1."):
                    break
                if in_threads and line.startswith("### "):
                    # Strip "### 1.1 " prefix
                    title = line.lstrip("# ").strip()
                    # Drop leading numbering like "1.1 "
                    parts_title = title.split(" ", 1)
                    if len(parts_title) == 2 and parts_title[0].replace(".", "").isdigit():
                        title = parts_title[1]
                    threads.append(title)
                if len(threads) >= 5:
                    break
            if threads:
                parts.append("THREADS: " + " | ".join(threads))
    except Exception as _e:
        import logging as _logging
        _logging.getLogger('context_dna').debug(f"S10 fallback plans read failed: {_e}")

    # (c) most recent commit subject
    try:
        result = _subprocess.run(
            ["git", "log", "--pretty=format:%s", "-1"],
            capture_output=True, text=True, timeout=3, cwd=repo_root,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts.append(f"RECENT: {result.stdout.strip()[:160]}")
    except Exception as _e:
        import logging as _logging
        _logging.getLogger('context_dna').debug(f"S10 fallback git log failed: {_e}")

    if not parts:
        return None
    return "\n  ".join(parts)


def generate_section_10(prompt: str, session_id: Optional[str] = None,
                        config: InjectionConfig = None) -> str:
    """
    Generate STRATEGIC BIG PICTURE section.

    Primary: Pre-computed GPT-4.1 strategic analysis from Redis cache (instant).
    Fallback A: Legacy StrategicPlanner (local LLM, slower).
    Fallback B: Local file strategic snapshot — focus.json + strategic_plans.md
                Active Threads + latest commit. Always non-empty when at least one
                source exists. ZSF: never silently empty.

    Returns empty string only when section_10 is disabled by config.
    """
    config = config or InjectionConfig()

    if not config.section_10_enabled:
        return ""

    # Primary: check Redis cache for pre-computed S10 (GPT-4.1 strategic analyst)
    try:
        if is_redis_available():
            from memory.redis_cache import get_strategic_section
            cached = get_strategic_section(session_id or "default")
            if cached:
                lines = []
                lines.append("")
                lines.append("╔══════════════════════════════════════════════════════════════════════╗")
                lines.append("║  🗺️ SECTION 10: STRATEGIC BIG PICTURE                                ║")
                lines.append("║  GPT-4.1 Strategic Analyst — cross-session patterns & blind spots   ║")
                lines.append("╠══════════════════════════════════════════════════════════════════════╣")
                lines.append("")
                lines.append(f"  {cached.strip()}")
                lines.append("")
                lines.append("╚══════════════════════════════════════════════════════════════════════╝")
                return "\n".join(lines)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger('context_dna').debug(f"S10 Redis cache path failed: {_e}")

    # Fallback A: legacy StrategicPlanner
    reminder = None
    try:
        from memory.strategic_planner import StrategicPlanner
        planner = StrategicPlanner()
        clean_prompt = prompt[:200] if prompt else "general work"
        reminder = planner.get_big_picture_reminder(current_context=clean_prompt, force=False)
    except Exception as e:
        import logging
        logging.getLogger('context_dna').debug(f"Section 10 strategic planner failed: {e}")

    if reminder:
        lines = []
        lines.append("")
        lines.append("╔══════════════════════════════════════════════════════════════════════╗")
        lines.append("║  🗺️ SECTION 10: STRATEGIC BIG PICTURE                                ║")
        lines.append("║  Keeping the roadmap in view (LLM-analyzed, not programmatic)       ║")
        lines.append("╠══════════════════════════════════════════════════════════════════════╣")
        lines.append("")
        lines.append(f"  {reminder}")
        lines.append("")
        lines.append("╚══════════════════════════════════════════════════════════════════════╝")
        return "\n".join(lines)

    # Fallback B: local strategic snapshot from focus.json + plans + git
    snapshot = _s10_local_strategic_fallback()
    if snapshot:
        lines = []
        lines.append("")
        lines.append("╔══════════════════════════════════════════════════════════════════════╗")
        lines.append("║  🗺️ SECTION 10: STRATEGIC BIG PICTURE                                ║")
        lines.append("║  Local strategic snapshot (focus + plans + recent commit)           ║")
        lines.append("╠══════════════════════════════════════════════════════════════════════╣")
        lines.append("")
        lines.append(f"  {snapshot}")
        lines.append("")
        lines.append("╚══════════════════════════════════════════════════════════════════════╝")
        return "\n".join(lines)

    # Last-resort tiny stub (ZSF: never silently empty when enabled)
    import logging
    logging.getLogger('context_dna').warning(
        "S10: all three sources unavailable (Redis cache, StrategicPlanner, local files)"
    )
    return (
        "\n╔══════════════════════════════════════════════════════════════════════╗"
        "\n║  🗺️ SECTION 10: STRATEGIC BIG PICTURE                                ║"
        "\n╠══════════════════════════════════════════════════════════════════════╣"
        "\n\n  [strategic] no live cache, no planner reminder, no local snapshot —"
        "\n  start strategic_analyst cycle or populate memory/strategic_plans.md\n"
        "\n╚══════════════════════════════════════════════════════════════════════╝"
    )


# =============================================================================
# MAIN GENERATOR
# =============================================================================

def generate_context_injection(
    prompt: str,
    mode: str = "hybrid",
    session_id: Optional[str] = None,
    config: Optional[InjectionConfig] = None,
    ab_variant: Optional[str] = None
) -> InjectionResult:
    """
    Generate context injection for a prompt.

    Args:
        prompt: The user's prompt text
        mode: "layered", "greedy", "hybrid", or "minimal"
        session_id: Session ID for A/B testing
        config: Optional custom configuration
        ab_variant: Force specific A/B variant ("control", "a", "b", "c")

    Returns:
        InjectionResult with generated content
    """
    start_time = datetime.now()

    # =================================================================
    # SHORT PROMPT BYPASS: ≤5 words → skip injection entirely
    # =================================================================
    # "do it", "yes", "ok thanks" etc. don't benefit from 9-section injection.
    # This is the ONLY bypass exception. Still mirror to dialogue for context.
    word_count = len(prompt.strip().split())
    if word_count <= 5 and not (config and config.skip_short_prompt_bypass):
        # Still mirror the prompt for dialogue context
        try:
            from memory.dialogue_mirror import mirror_aaron_message
            mirror_session = session_id or os.environ.get("CLAUDE_SESSION_ID", "default-session")
            mirror_aaron_message(session_id=mirror_session, content=prompt, source="vscode")
        except Exception:
            pass
        return InjectionResult(
            content="",
            sections_included=[],
            risk_level=RiskLevel.LOW,
            first_try_likelihood="95%",
            mode=InjectionMode.MINIMAL,
            generation_time_ms=0,
            volume_tier=0,
            failure_count=0,
            section_timings={},
        )

    # =================================================================
    # INJECTION DEPTH: Full → Abbreviated → Full-on-compaction cycle
    # =================================================================
    # Prevents "prompt too long" crashes by reducing injection size after
    # initial context is established. Full injection on turns 1-2 gives
    # Atlas complete context. Abbreviated on turns 3+ saves ~60% since
    # context is already in conversation history. Full again every 12
    # turns as a proxy for "auto-compaction likely happened."
    #
    # S2 (Professor Wisdom), S6 (Butler/Synaptic→Atlas), S8 (8th Intelligence)
    # always stay FULL — they're prompt-specific and change every time.
    CONTEXT_DNA_DIR = os.environ.get("CONTEXT_DNA_DIR", os.path.expanduser("~/.context-dna"))
    _session = session_id or os.environ.get("CLAUDE_SESSION_ID", "unknown")
    _counter_file = os.path.join(CONTEXT_DNA_DIR, f".injection_count_{_session[:16]}")
    try:
        _count = int(open(_counter_file).read().strip()) + 1 if os.path.exists(_counter_file) else 1
    except (ValueError, OSError):
        _count = 1
    try:
        os.makedirs(CONTEXT_DNA_DIR, exist_ok=True)
        with open(_counter_file, 'w') as f:
            f.write(str(_count))
    except OSError:
        pass

    # Determine injection depth: full or abbreviated
    # Full: turns 1-2, then every 12 turns (proxy for post-compaction refresh)
    _cycle_position = ((_count - 1) % 12) + 1  # 1-12 repeating
    _use_full_injection = _cycle_position <= 2
    _safe_log('debug', f"[injection] count={_count} cycle_pos={_cycle_position} mode={'FULL' if _use_full_injection else 'ABBREVIATED'}")

    # Log user prompt to work_dialogue_log (feeds Objective Success Detector)
    try:
        from memory.architecture_enhancer import WorkDialogueLog
        work_log = WorkDialogueLog()
        work_log.log_dialogue(prompt, source="user")
    except Exception as e:
        # GAP 3 FIX: Log as warning (not debug) so failures are visible
        # This feeds the Objective Success Detector - failures should be noticed
        _safe_log('warning', f"Work dialogue log failed: {e}")

    # =================================================================
    # DIALOGUE MIRROR - Synaptic's Eyes and Ears (Option A Integration)
    # =================================================================
    # Mirror the incoming prompt for Synaptic's full conversation awareness
    try:
        from memory.dialogue_mirror import mirror_aaron_message, DialogueSource
        # Detect IDE source
        ide_source = "vscode"  # Default
        try:
            if os.environ.get("CURSOR_FILE_PATH"):
                ide_source = "cursor"
            elif os.environ.get("WINDSURF_FILE_PATH"):
                ide_source = "windsurf"
            elif os.environ.get("PYCHARM_FILE_PATH"):
                ide_source = "pycharm"
        except Exception as e:
            print(f"[WARN] IDE source detection failed: {e}")

        # Get or generate session ID
        mirror_session = session_id or os.environ.get("CLAUDE_SESSION_ID", "default-session")

        # Mirror the prompt - this gives Synaptic live dialogue awareness
        mirror_aaron_message(
            session_id=mirror_session,
            content=prompt,
            source=ide_source,
            project=os.environ.get("CONTEXT_DNA_PROJECT")
        )
    except Exception as e:
        # Non-blocking - don't fail injection if mirror fails
        _safe_log('debug', f"Dialogue mirror failed: {e}")

    # Parse mode
    try:
        injection_mode = InjectionMode(mode)
    except ValueError:
        injection_mode = InjectionMode.HYBRID

    # Create or use config
    if config is None:
        config = InjectionConfig(mode=injection_mode)

    # Detect risk level
    risk_level = detect_risk_level(prompt)

    # =================================================================
    # RISK-DRIVEN SECTION DEPTH MAPPING
    # CRITICAL → all sections + full library + full wisdom
    # HIGH → all sections + full wisdom
    # MODERATE → standard 6 sections, summary wisdom
    # LOW → minimal (S0 + S1 only)
    # =================================================================
    if risk_level == RiskLevel.LOW:
        config.section_3_enabled = False   # Skip AWARENESS
        config.section_4_enabled = False   # Skip DEEP_CONTEXT
        config.professor_depth = "one_thing_only"
        config.awareness_depth = "none"
        config.verbose_protocol = False
    elif risk_level == RiskLevel.MODERATE:
        config.awareness_depth = "changes_only"
        config.professor_depth = "summary"
        config.verbose_protocol = False
    elif risk_level == RiskLevel.HIGH:
        config.awareness_depth = "full"
        config.professor_depth = "full"
        config.verbose_protocol = True
        config.sop_count = max(config.sop_count, 5)
    elif risk_level == RiskLevel.CRITICAL:
        config.awareness_depth = "full"
        config.professor_depth = "full"
        config.verbose_protocol = True
        config.sop_count = 10  # Full arsenal

    # =================================================================
    # BOUNDARY INTELLIGENCE - Project Detection & Filtering
    # =================================================================
    boundary_decision = None
    active_file = None  # Hoisted for S6 file-awareness (G5)
    if config.skip_boundary_intelligence:
        _safe_log('debug', "[injection] Boundary intelligence skipped (chat mode)")
    else:
        try:
            from memory.boundary_intelligence import (
                BoundaryIntelligence,
                BoundaryContext,
                get_cached_hierarchy,
                get_recent_projects_for_context
            )

            bi = BoundaryIntelligence(use_llm=True)

            # Multi-IDE support: detect active file from any IDE
            try:
                from memory.ide_detection import get_active_file_path, get_workspace_folder, get_ide_context
                ide_ctx = get_ide_context()
                active_file = ide_ctx.active_file_path
                workspace = ide_ctx.workspace_folder
            except ImportError:
                # Fallback to direct env var check if ide_detection not available
                active_file = os.environ.get("CURSOR_FILE_PATH") or os.environ.get("VSCODE_FILE_PATH")
                workspace = None

            # Build boundary context from available information
            boundary_context = BoundaryContext(
                user_prompt=prompt,
                active_file_path=active_file,
                hierarchy_profile=get_cached_hierarchy(),
                recent_projects=get_recent_projects_for_context(session_id),
                mirrored_dialogue=os.environ.get("CONTEXT_DNA_DIALOGUE"),  # If available
                session_id=session_id or "",
            )

            boundary_decision = bi.analyze_and_decide(boundary_context)

            # Track file activity asynchronously via Celery for recency learning
            # Uses fire_and_forget to avoid blocking on Redis unavailability
            if active_file:
                try:
                    from memory.celery_tasks import track_file_activity, fire_and_forget
                    fire_and_forget(
                        track_file_activity,
                        file_path=active_file,
                        project=boundary_decision.primary_project if boundary_decision else None,
                        session_id=session_id
                    )
                except Exception:
                    pass  # Non-blocking - don't fail injection if tracking fails

        except ImportError as e:
            _safe_log('debug', f"Boundary intelligence not available: {e}")
        except Exception as e:
            _safe_log('warning', f"Boundary analysis failed: {e}")

    # THREE-TIER VOLUME ESCALATION
    # Tier 1: Silver Platter (default) → Tier 2: Expanded → Tier 3: Full Library
    failure_count = get_session_failure_count(session_id)
    first_try_int = int(FIRST_TRY_LIKELIHOOD[risk_level].replace("%", ""))
    volume_tier = determine_volume_tier(failure_count, first_try_int)

    # Adjust config based on volume tier
    if volume_tier == VolumeEscalationTier.EXPANDED:
        # Tier 2: More context after 1 failure
        config.sop_count = max(config.sop_count, 5)  # At least 5 SOPs
        config.awareness_depth = "full"
        config.session_has_failures = True
    elif volume_tier == VolumeEscalationTier.FULL_LIBRARY:
        # Tier 3: Maximum context after 2+ failures
        config.sop_count = 10  # Many SOPs in textbook mode
        config.awareness_depth = "full"
        config.professor_depth = "full"
        config.verbose_protocol = True
        config.session_has_failures = True

    # Adjust config based on mode
    if injection_mode == InjectionMode.MINIMAL:
        config.section_3_enabled = False
        config.section_4_enabled = False
        config.verbose_protocol = False
    elif injection_mode == InjectionMode.LAYERED:
        # Traditional layered system - more verbose
        config.professor_depth = "full"
        config.verbose_protocol = True
    elif injection_mode == InjectionMode.GREEDY:
        # Agent-focused - concise but complete
        config.professor_depth = "summary"
        config.awareness_depth = "full"
        config.verbose_protocol = False

    # Handle A/B variant selection
    if session_id and not ab_variant:
        # Deterministic selection: 50% control, 25% A, 15% B, 10% C
        hash_bucket = hash(session_id) % 100
        if hash_bucket < 50:
            ab_variant = "control"
        elif hash_bucket < 75:
            ab_variant = "a"
        elif hash_bucket < 90:
            ab_variant = "b"
        else:
            ab_variant = "c"

    # Apply A/B variant modifications
    if ab_variant == "a":
        # Variant A: Minor tweaks - shorter wisdom
        config.professor_depth = "summary"
        config.sop_count = 2
    elif ab_variant == "b":
        # Variant B: Dramatic changes - more awareness
        config.awareness_depth = "full"
        config.verbose_protocol = True
        config.sop_count = 5
    elif ab_variant == "c":
        # Variant C: Wisdom injection
        config.wisdom_injection_enabled = True
        try:
            from context_dna.hook_evolution import get_hook_evolution_engine
            engine = get_hook_evolution_engine()
            _, wisdom = engine.get_active_variant("UserPromptSubmit", session_id, prompt, risk_level.value)
            config.wisdom_text = wisdom
        except ImportError as e:
            print(f"[WARN] Hook evolution engine not available: {e}")

    # Generate sections
    sections = []
    sections_included = []

    # =================================================================
    # FULLY PARALLEL SECTION GENERATION (Aaron's Request - Feb 9, 2026)
    # =================================================================
    # ALL sections now run in parallel for maximum speed
    # Each section has independent timeout and graceful degradation
    # Section 8 is CRITICAL and never skipped (it "never sleeps")
    # =================================================================

    section_results = {}
    section_timings = {}  # key -> latency_ms (actual per-section timing)

    def _run_timed(key, label, fn, timeout_s=None):
        """Run a section generator with timing and optional timeout."""
        _t0 = time.monotonic()
        try:
            if timeout_s:
                # Run with timeout
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(fn)
                    _content = future.result(timeout=timeout_s)
            else:
                _content = fn()
            return (key, _content, label, int((time.monotonic() - _t0) * 1000))
        except Exception as e:
            _safe_log('warning', f"Section {key} failed: {e}")
            return (key, None, label, int((time.monotonic() - _t0) * 1000))

    def _collect_result(result_tuple):
        """Collect a section result into section_results and section_timings."""
        if len(result_tuple) == 4:
            key, content, label, latency_ms = result_tuple
            section_timings[key] = latency_ms
        else:
            key, content, label = result_tuple
        section_results[key] = (content, label)

    # -----------------------------------------------------------------
    # ALL SECTIONS FULLY PARALLEL (Aaron's request - maximum speed)
    # -----------------------------------------------------------------
    # S0, S5: Run sync first (instant, static)
    # S1-S8, S10: ALL run in parallel with individual timeouts
    # Section 8 ALWAYS included (it "never sleeps")
    # -----------------------------------------------------------------
    
    # Instant static sections first
    try:
        _collect_result(_run_timed("section_0", "safety",
                                   lambda: generate_section_0(prompt, config)))
    except Exception as e:
        _safe_log('warning', f"S0 failed: {e}")

    try:
        _collect_result(_run_timed("section_5", "protocol",
                                   lambda: generate_section_5(risk_level, config)))
    except Exception as e:
        _safe_log('warning', f"S5 failed: {e}")

    # ALL remaining sections in parallel
    parallel_start = time.monotonic()

    # Per-section timeouts are env-configurable. Defaults match the historical
    # values; overrides let ops tune budgets without a code change. Floats so
    # 0.5/1.5 work for tight ops. Negative or zero falls back to default.
    def _t(env_name: str, default_s: float) -> float:
        try:
            v = float(os.environ.get(env_name, default_s))
            return v if v > 0 else default_s
        except (TypeError, ValueError):
            return default_s
    S1_TIMEOUT  = _t("WEBHOOK_S1_TIMEOUT_S", 10.0)
    S2_TIMEOUT  = _t("WEBHOOK_S2_TIMEOUT_S", 30.0)
    S3_TIMEOUT  = _t("WEBHOOK_S3_TIMEOUT_S", 10.0)
    S4_TIMEOUT  = _t("WEBHOOK_S4_TIMEOUT_S", 12.0)   # bumped from 10 — DeepSeek-fallback path needs headroom
    S6_TIMEOUT  = _t("WEBHOOK_S6_TIMEOUT_S", 15.0)   # bumped from 10 — synaptic_deep_s6 LLM call ~10s on DeepSeek
    S7_TIMEOUT  = _t("WEBHOOK_S7_TIMEOUT_S", 10.0)
    S8_TIMEOUT  = _t("WEBHOOK_S8_TIMEOUT_S", 120.0)
    S10_TIMEOUT = _t("WEBHOOK_S10_TIMEOUT_S", 5.0)

    def run_section_1():
        return _run_timed("section_1", "foundation",
                          lambda: generate_section_1(prompt, risk_level, config, session_id, boundary_decision),
                          timeout_s=S1_TIMEOUT)

    def run_section_2():
        return _run_timed("section_2", "wisdom",
                          lambda: generate_section_2(prompt, config),
                          timeout_s=S2_TIMEOUT)

    def run_section_3():
        return _run_timed("section_3", "awareness",
                          lambda: generate_section_3(prompt, risk_level, config),
                          timeout_s=S3_TIMEOUT)

    def run_section_4():
        return _run_timed("section_4", "deep_context",
                          lambda: generate_section_4(prompt, risk_level, config),
                          timeout_s=S4_TIMEOUT)

    def run_section_6():
        def _s6():
            result = generate_section_6(prompt, session_id, config, active_file=active_file)
            if isinstance(result, tuple):
                content, _ = result
            else:
                content = result
            return content
        return _run_timed("section_6", "synaptic_to_atlas", _s6, timeout_s=S6_TIMEOUT)

    def run_section_7():
        return _run_timed("section_7", "acontext_library",
                          lambda: generate_section_7(prompt, volume_tier, config),
                          timeout_s=S7_TIMEOUT)

    def run_section_8():
        return _run_timed("section_8", "synaptic_8th_intelligence",
                          lambda: generate_section_8(prompt, session_id, config),
                          timeout_s=S8_TIMEOUT)  # 2 minutes for Synaptic

    def run_section_10():
        return _run_timed("section_10", "vision_contextual_awareness",
                          lambda: generate_section_10(prompt, session_id, config),
                          timeout_s=S10_TIMEOUT)
    
    def run_section_2_and_8_batched():
        """
        Batch Section 2 (Professor) + Section 8 (Synaptic) LLM calls together.
        
        This provides significant speedup by:
        - Single HTTP request instead of 2 separate calls
        - vLLM continuous batching schedules them together
        - Expected 15-25% latency reduction
        """
        _t0 = time.monotonic()
        try:
            # Gather context for both sections
            from memory.webhook_batch_helper import batch_section_2_and_8_llm_calls
            from memory.webhook_message_builders import prepare_section_2_messages, prepare_section_8_messages
            
            # Build Section 2 messages (Professor wisdom)
            s2_messages = None
            try:
                s2_system, s2_user = prepare_section_2_messages(prompt, config)
                if s2_system and s2_user:
                    s2_messages = [
                        {"role": "system", "content": s2_system},
                        {"role": "user", "content": s2_user}
                    ]
            except Exception as e:
                _safe_log('warning', f"S2 message prep failed: {e}")
            
            # Build Section 8 messages (Synaptic voice)
            s8_messages = None
            try:
                s8_system, s8_user = prepare_section_8_messages(prompt, session_id)
                if s8_system and s8_user:
                    s8_messages = [
                        {"role": "system", "content": s8_system},
                        {"role": "user", "content": s8_user}
                    ]
            except Exception as e:
                _safe_log('warning', f"S8 message prep failed: {e}")
            
            # Batch both LLM calls
            s2_content, s8_content = batch_section_2_and_8_llm_calls(
                section_2_messages=s2_messages,
                section_8_messages=s8_messages,
                section_2_max_tokens=700,
                section_8_max_tokens=1500
            )
            
            # Collect results
            batch_latency_ms = int((time.monotonic() - _t0) * 1000)
            results = []
            
            if s2_content:
                # Post-process S2 (extract thinking, add label)
                from memory.webhook_batch_helper import extract_thinking_chain
                s2_final = extract_thinking_chain(s2_content)
                if s2_final:
                    s2_final = f"[Professor via local LLM — reasoning]\n{s2_final}"
                    results.append(("section_2", s2_final, "wisdom", batch_latency_ms))
                    
                    # Cache S2 wisdom for future requests
                    try:
                        if is_redis_available():
                            from memory.redis_cache import cache_section_content
                            _s2_cache_key = _make_cache_key("s2", prompt, risk_level.value)
                            cache_section_content(_s2_cache_key, s2_final, ttl_seconds=600)
                    except Exception:
                        pass
            
            if s8_content:
                # Post-process S8 (extract thinking, keep raw voice)
                from memory.webhook_batch_helper import extract_thinking_chain
                s8_final = extract_thinking_chain(s8_content)
                if s8_final:
                    results.append(("section_8", s8_final, "synaptic_8th_intelligence", batch_latency_ms))
            
            # Return tuple that unpacks to multiple results
            return results if results else []
            
        except Exception as e:
            _safe_log('warning', f"Batched S2+S8 failed: {e}")
            return []

    # =================================================================
    # ANTICIPATION ENGINE: Check pre-computed content FIRST (instant)
    # =================================================================
    # RULE: MUST EVALUATE ALL CACHED CONTENT
    # Cached S2/S6/S8 content MUST always be injected into the webhook,
    # even if from a previous prompt cycle. Atlas evaluates usefulness —
    # never skip cached content. Late content can be mission-critical.
    # Caches accumulate until consumed; TTL handles natural expiry.
    # =================================================================
    _anticipation_s2 = None
    _anticipation_s6 = None
    _anticipation_s8 = None
    _anticipation_s10 = None
    _anticipation_session = session_id
    try:
        if is_redis_available():
            from memory.redis_cache import get_anticipation_section, get_strategic_section
            # Detect session for anticipation lookup
            if not _anticipation_session:
                try:
                    from memory.anticipation_engine import _detect_active_session
                    _anticipation_session = _detect_active_session()
                except Exception:
                    pass
            if _anticipation_session:
                _anticipation_s2 = get_anticipation_section(_anticipation_session, "s2", current_prompt=prompt)
                _anticipation_s6 = get_anticipation_section(_anticipation_session, "s6", current_prompt=prompt)
                _anticipation_s8 = get_anticipation_section(_anticipation_session, "s8", current_prompt=prompt)
                _anticipation_s10 = get_strategic_section(_anticipation_session)
                if _anticipation_s2:
                    section_results["section_2"] = (_anticipation_s2, "wisdom")
                    section_timings["section_2"] = 0
                    _safe_log('info', f"Anticipation HIT: S2 ({len(_anticipation_s2)} chars)")
                if _anticipation_s6:
                    # Don't set section_results yet — live S6 will also run.
                    # Stored separately for accumulation at assembly time.
                    _safe_log('info', f"Anticipation HIT: S6 ({len(_anticipation_s6)} chars)")
                if _anticipation_s8:
                    # Wrap cached S8 with delimiters — anticipation returns raw LLM output
                    _s8_wrapped = (
                        "╔══════════════════════════════════════════════════════════════════════╗\n"
                        "║  🧠 SECTION 8: SYNAPTIC'S 8TH INTELLIGENCE                            ║\n"
                        "║  The Ever-Present Subconscious Voice to Aaron                        ║\n"
                        "╠══════════════════════════════════════════════════════════════════════╣\n\n"
                        "[START: Synaptic to Aaron]\n\n"
                        f"  {_anticipation_s8.strip()}\n\n"
                        "[END: Synaptic to Aaron]\n\n"
                        "╚══════════════════════════════════════════════════════════════════════╝"
                    )
                    section_results["section_8"] = (_s8_wrapped, "synaptic_8th_intelligence")
                    section_timings["section_8"] = 0
                    _safe_log('info', f"Anticipation HIT: S8 ({len(_anticipation_s8)} chars, wrapped)")
                if _anticipation_s10:
                    # S10 comes pre-formatted from strategic_analyst.py — wrap with delimiters
                    _s10_wrapped = (
                        "╔══════════════════════════════════════════════════════════════════════╗\n"
                        "║  🗺️ SECTION 10: STRATEGIC BIG PICTURE                                ║\n"
                        "║  GPT-4.1 Strategic Analyst — cross-session patterns & blind spots   ║\n"
                        "╠══════════════════════════════════════════════════════════════════════╣\n\n"
                        f"  {_anticipation_s10.strip()}\n\n"
                        "╚══════════════════════════════════════════════════════════════════════╝"
                    )
                    section_results["section_10"] = (_s10_wrapped, "vision_contextual_awareness")
                    section_timings["section_10"] = 0
                    _safe_log('info', f"Strategic S10 HIT: ({len(_anticipation_s10)} chars)")
                # Keep cached S2/S6/S8/S10 — valuable across multiple prompts.
                # Anticipation engine overwrites with fresh content on each cycle.
                # TTL handles natural expiry. Don't delete after one use.
    except Exception:
        pass

    # Check S2 wisdom cache (fallback if anticipation missed)
    _s2_wisdom_cached = bool(_anticipation_s2)
    _cached_s2 = _anticipation_s2
    if not _s2_wisdom_cached:
        try:
            if is_redis_available():
                from memory.redis_cache import get_cached_section_content
                _s2_cache_key = _make_cache_key("s2", prompt, risk_level.value)
                _cached_s2 = get_cached_section_content(_s2_cache_key)
                if _cached_s2:
                    _s2_wisdom_cached = True
                    section_results["section_2"] = (_cached_s2, "wisdom")
                    section_timings["section_2"] = 0
        except Exception:
            pass

    # Check Section 8 LLM availability (skip if anticipation provided S8)
    _skip_s8_llm = bool(_anticipation_s8)
    try:
        from memory.llm_health_nonblocking import check_llm_health_nonblocking
        _llm_healthy, _llm_reason = check_llm_health_nonblocking()
        if not _llm_healthy:
            _skip_s8_llm = True
            # Generate S8 fallback
            _s8_fallback = "╔══════════════════════════════════════════════════════════════════════╗\n"
            _s8_fallback += "║  🧠 SECTION 8: SYNAPTIC'S 8TH INTELLIGENCE                            ║\n"
            _s8_fallback += "║  The Ever-Present Subconscious Voice to Aaron                        ║\n"
            _s8_fallback += "╠══════════════════════════════════════════════════════════════════════╣\n\n"
            _s8_fallback += "[START: Synaptic to Aaron]\n\n"
            _s8_fallback += "  [LLM offline or unavailable]\n\n"
            _s8_fallback += "[END: Synaptic to Aaron]\n\n"
            _s8_fallback += "╚══════════════════════════════════════════════════════════════════════╝"
            section_results["section_8"] = (_s8_fallback, "synaptic_8th_intelligence")
            section_timings["section_8"] = 0
    except Exception as _s8_exc:
        logger.warning(f"S8 anticipation cache error: {_s8_exc}")
        # Do NOT set _skip_s8_llm — safety net below MUST still fire

    # =================================================================
    # PRE-COMPUTE ARCHITECTURE: Never block webhook on LLM calls
    # =================================================================
    # S2 (Professor) and S8 (Synaptic) use local LLM which takes 7-29s.
    # Instead of blocking: serve from anticipation cache or fast fallback,
    # then trigger background pre-compute for NEXT prompt.
    #
    # Flow: Cache HIT → instant | Cache MISS → fallback + trigger refresh
    # =================================================================

    # If S2 not cached, use fast non-LLM fallback (domain template + landmines)
    # DO NOT call generate_section_2() — it tries LLM first (7-14s blocking)
    if not _s2_wisdom_cached:
        try:
            _clean_prompt = _sanitize_prompt_for_query(prompt)
            _detected_domain = detect_domain_from_prompt(_clean_prompt)
            _s2_parts = []
            _s2_parts.append("PROFESSOR WISDOM")
            _s2_parts.append("─" * 40)
            if _detected_domain:
                _domain_wisdom = get_domain_specific_wisdom(_detected_domain, "full")
                if _domain_wisdom:
                    _s2_parts.append(f"  [Pre-computing LLM wisdom for next prompt]\n  {_domain_wisdom}")
            else:
                _s2_parts.append("  [Pre-computing LLM wisdom for next prompt]")
            # Always add landmines (fast, no LLM)
            _s2_landmines = _get_failure_landmines(_clean_prompt)
            if _s2_landmines:
                _s2_parts.append("")
                _s2_parts.append("  LANDMINES (from failure patterns):")
                for _lm in _s2_landmines:
                    _s2_parts.append(f"    {_lm}")
            _s2_fallback = "\n".join(_s2_parts)
            if _s2_fallback:
                section_results["section_2"] = (_s2_fallback, "wisdom")
                section_timings["section_2"] = 0
                _s2_wisdom_cached = True
        except Exception:
            pass

    # S8 MUST be in payload — if missing for ANY reason, inject static fallback
    # (don't call LLM synchronously — it takes 29s)
    if "section_8" not in section_results:
        _s8_lite_fallback = (
            "╔══════════════════════════════════════════════════════════════════════╗\n"
            "║  SECTION 8: SYNAPTIC'S 8TH INTELLIGENCE                            ║\n"
            "╠══════════════════════════════════════════════════════════════════════╣\n\n"
            "[START: Synaptic to Aaron]\n\n"
            "  [Pre-computing for next prompt — anticipation engine warming up]\n\n"
            "[END: Synaptic to Aaron]\n\n"
            "╚══════════════════════════════════════════════════════════════════════╝"
        )
        section_results["section_8"] = (_s8_lite_fallback, "synaptic_8th_intelligence")
        section_timings["section_8"] = 0
        _skip_s8_llm = True

    # Trigger background anticipation refresh for NEXT prompt (fire-and-forget)
    if not _anticipation_s2 or not _anticipation_s8:
        try:
            import threading
            def _bg_anticipation():
                try:
                    from memory.anticipation_engine import run_anticipation_cycle
                    run_anticipation_cycle()
                except Exception:
                    pass
            threading.Thread(target=_bg_anticipation, daemon=True, name="anticipation_refresh").start()
        except Exception:
            pass

    # Build task list - non-LLM sections only (S2/S8 handled above via cache/fallback)
    # S6 always runs live — cached S6 provides accumulated context,
    # live S6 provides current-prompt-specific guidance. Both valuable.
    all_tasks = [run_section_1, run_section_3, run_section_4, run_section_6, run_section_10]

    # Add S7 if S2 not cached (S7 provides escalation).
    # ZSF / Cycle 8 H1: when S2 wisdom IS cached we still record an explicit
    # stub for S7 so per-section observability (`section_perf missing=...`) can
    # distinguish "intentionally skipped because S2 hit cache" from "broken /
    # missing". Mirrors G3/G4/G5 stub-label discipline at the call-site rather
    # than only inside the generator.
    _skip_s7 = _s2_wisdom_cached
    if not _skip_s7:
        all_tasks.append(run_section_7)
    else:
        section_results["section_7"] = (
            "📚 ACONTEXT_LIBRARY\n"
            "  [s7:skipped — s2_wisdom_cached=true, escalation not needed]\n"
            "  S2 cache hit covers the wisdom escalation path; full library "
            "would be redundant. Set anticipation TTL lower or invalidate S2 "
            "cache to force S7 generation.",
            "acontext_library",
        )
        section_timings["section_7"] = 0

    # Run ALL sections in parallel with maximum workers
    # No LLM calls in this path (S2/S8 resolved via cache/fallback above)
    # ASSUMPTION-WAS-WRONG: S6 makes a synaptic_deep LLM call (~10s on DeepSeek
    # fallback when local Qwen unavailable). Past 15s budget caused S6 timeouts
    # + cascade to skip later sections. Bumped default to 20s + env-tunable.
    INJECTION_TIMEOUT = float(os.environ.get("WEBHOOK_INJECTION_TIMEOUT_S", "20"))
    with ThreadPoolExecutor(max_workers=len(all_tasks)) as executor:
        futures = {executor.submit(task): task for task in all_tasks}
        try:
            for future in as_completed(futures, timeout=INJECTION_TIMEOUT):
                try:
                    result = future.result()
                    # Handle batched S2+S8 (returns list of results)
                    if isinstance(result, list):
                        for r in result:
                            _collect_result(r)
                    else:
                        _collect_result(result)
                except Exception as e:
                    _safe_log('warning', f"Section failed: {e}")
        except TimeoutError:
            _safe_log('warning', f"Injection hard timeout ({INJECTION_TIMEOUT}s) — some sections skipped (LLM may be stuck)")

    parallel_elapsed_ms = int((time.monotonic() - parallel_start) * 1000)

    # Structured per-section observability — single greppable INFO line so
    # ops can tail logs and see exactly which sections were fast/slow without
    # parsing pickled InjectionResult. Format: section_perf event_name=<v> ...
    try:
        _missing = sorted(
            sec for sec in (
                "section_0", "section_1", "section_2", "section_3", "section_4",
                "section_5", "section_6", "section_7", "section_8", "section_10",
            )
            if sec not in section_results or not (section_results[sec][0] or "").strip()
        )
        _timing_pairs = " ".join(
            f"{k.replace('section_', 's')}={v}" for k, v in sorted(section_timings.items())
        )
        _safe_log(
            "info",
            f"section_perf parallel_ms={parallel_elapsed_ms} "
            f"missing={','.join(_missing) if _missing else 'none'} {_timing_pairs}",
        )
    except Exception as _e:
        # ZSF: any logging failure must not break injection
        try:
            _safe_log("debug", f"section_perf log line failed: {_e}")
        except Exception:
            pass

    # ==========================================================================
    # RACE S5 — Composite latency budget alert.
    # ==========================================================================
    # Compare parallel_elapsed_ms against WEBHOOK_TOTAL_BUDGET_MS (default
    # 20000 ms — webhook fix series brought cold from 46s → 12s, warm to
    # 318ms; 20s gives safe headroom while flagging real regressions).
    #
    # When breached: log a WARNING (greppable in fleet logs) and fire
    # event.webhook.budget_exceeded.<node> on NATS so the daemon increments
    # webhook_budget_exceeded_count + surfaces budget_exceeded_recent under
    # /health.webhook. Fire-and-forget — webhook hot path NEVER blocks (ZSF).
    try:
        _budget_ms = int(os.environ.get("WEBHOOK_TOTAL_BUDGET_MS", "20000") or "20000")
    except Exception as _e:
        _safe_log("debug", f"WEBHOOK_TOTAL_BUDGET_MS parse failed: {_e}")
        _budget_ms = 20000
    _missing_for_alert: list = []
    if _budget_ms > 0 and parallel_elapsed_ms > _budget_ms:
        try:
            _missing_for_alert = sorted(
                sec for sec in (
                    "section_0", "section_1", "section_2", "section_3", "section_4",
                    "section_5", "section_6", "section_7", "section_8", "section_10",
                )
                if sec not in section_results or not (section_results[sec][0] or "").strip()
            )
            _safe_log(
                "warning",
                f"webhook_budget_exceeded total_ms={parallel_elapsed_ms} "
                f"budget_ms={_budget_ms} over_by_ms={parallel_elapsed_ms - _budget_ms} "
                f"missing={','.join(_missing_for_alert) if _missing_for_alert else 'none'}",
            )
        except Exception as _e:
            # ZSF: log line failure must not break the budget alert path.
            _safe_log("debug", f"webhook_budget_exceeded log failed: {_e}")
        try:
            from memory.webhook_health_publisher import get_publisher as _get_health_pub
            _budget_pub = _get_health_pub()
            # Build minimal per-section snapshot for the alert payload — same
            # shape the regular health event uses, kept small.
            _budget_sections = {}
            for _sk in (
                "section_0", "section_1", "section_2", "section_3", "section_4",
                "section_5", "section_6", "section_7", "section_8", "section_10",
            ):
                if _sk in section_results:
                    _content, _label = section_results[_sk]
                    _budget_sections[_sk] = {
                        "latency_ms": int(section_timings.get(_sk, 0) or 0),
                        "ok": bool(_content and str(_content).strip()),
                    }
            # publish_budget_exceeded is best-effort; failures are counted in
            # publisher.publish_errors and never raise into the hot path.
            _publish_budget = getattr(_budget_pub, "publish_budget_exceeded", None)
            if _publish_budget is not None:
                _publish_budget(
                    total_ms=int(parallel_elapsed_ms),
                    budget_ms=int(_budget_ms),
                    sections=_budget_sections,
                    missing=_missing_for_alert,
                )
        except Exception as _e:
            # ZSF: NATS publish failures are already counted inside the
            # publisher (publish_errors). This guard catches anything *before*
            # publish (import errors, locals weirdness) — observable via debug
            # log so the gap surfaces if it ever spikes.
            _safe_log("debug", f"webhook_budget_exceeded publish wire failed: {_e}")

    # Record per-section health to webhook notification system (fixes false negatives)
    try:
        from memory.webhook_section_notifications import record_section_health
        section_id_map = {
            "section_0": 0, "section_1": 1, "section_2": 2, "section_3": 3,
            "section_4": 4, "section_5": 5, "section_6": 6, "section_7": 7,
            "section_8": 8, "section_10": 10,
        }
        for sec_key, sec_id in section_id_map.items():
            if sec_key in section_results:
                content, _label = section_results[sec_key]
                healthy = bool(content and content.strip())
                record_section_health("vs_code_claude_code", sec_id, healthy, "pre_message")
            # Sections not in results = not attempted (skip, don't mark unhealthy)
    except ImportError:
        pass  # webhook_section_notifications not available
    except Exception:
        pass  # Non-blocking: health recording should never break injection

    # ==========================================================================
    # CONTEXT BUDGET: Smart abbreviation to prevent "prompt too long" crashes
    # ==========================================================================
    # Full(1-2) → Abbreviated+Full(2,6,8)(3+) → Full(after compaction) → repeat
    #
    # ALWAYS FULL: S2 (Professor Wisdom), S6 (Butler/Synaptic→Atlas), S8 (8th Intelligence)
    #   → These are prompt-specific and change every time
    # ABBREVIATED (turns 3+): S0, S1, S3, S4, S5, S7, S10
    #   → Compress to dense summaries with refs. Like SOP titles: max info, min tokens
    ALWAYS_FULL_SECTIONS = {"section_2", "section_6", "section_8"}
    MAX_ANTICIPATION_CHARS = 1500  # Cap prior anticipation context

    def _abbreviate_section(key: str, content: str, label: str) -> str:
        """Abbreviate non-priority sections to dense summaries with references.
        Like SOP titles: maximum information density, minimum token count."""
        if not content or not content.strip():
            return content
        # S2/S6/S8 always full (prompt-specific intelligence)
        if key in ALWAYS_FULL_SECTIONS:
            return content
        # Full injection turns: everything verbatim
        if _use_full_injection:
            return content

        # --- ABBREVIATED MODE (turns 3+) ---
        # Goal: SOP-title density. Every char earns its place.

        # S0: Safety — never-do rules already in CLAUDE.md, just surface critical findings
        if key == "section_0":
            lines = content.strip().split('\n')
            findings = [l.strip() for l in lines if l.strip().startswith('•')]
            critical_count = sum(1 for l in lines if 'CRITICAL' in l.upper())
            if findings:
                summary = f"[S0:Safety] {len(findings)} findings ({critical_count} critical). Rules: CLAUDE.md"
                # Show critical findings only (most actionable)
                critical_findings = [f for f in findings if any(kw in f.upper() for kw in ['CRITICAL', 'INFRA', 'DOWN', 'CRASH'])]
                if critical_findings:
                    return summary + '\n' + '\n'.join(critical_findings[:3])
                return summary
            return "[S0:Safety] Active. Rules: CLAUDE.md"

        # S1: Foundation SOPs — title-density summaries
        if key == "section_1":
            lines = content.strip().split('\n')
            # Extract SOP titles/headers (the densest information)
            sop_lines = [l.strip() for l in lines if l.strip() and not l.strip().startswith('─') and not l.strip().startswith('═')]
            if len(sop_lines) > 5:
                return '\n'.join(sop_lines[:5]) + f"\n  [+{len(sop_lines)-5} more. Full: memory/query.py 'sop']"
            return '\n'.join(sop_lines) if sop_lines else content

        # S3: Awareness — one-line ripple summary
        if key == "section_3":
            lines = content.strip().split('\n')
            impact = [l.strip() for l in lines if 'IMPACT' in l.upper() or 'hub:' in l.lower() or 'depend' in l.lower()]
            file_count = sum(1 for l in lines if 'file' in l.lower() or '.py' in l)
            if impact:
                return f"[S3:Awareness] {file_count} files. " + impact[0][:120]
            changes = [l.strip() for l in lines if l.strip() and not l.strip().startswith('─')][:2]
            return f"[S3:Awareness] " + ' | '.join(changes) if changes else content[:200]

        # S4: Deep Context — blueprint headline
        if key == "section_4":
            lines = content.strip().split('\n')
            headlines = [l.strip() for l in lines if l.strip() and not l.strip().startswith('─') and not l.strip().startswith('═')][:3]
            return '\n'.join(headlines) + f"\n  [Full blueprint: memory/query.py 'blueprint']" if headlines else content[:300]

        # S5: Protocol — just risk + A/B metric
        if key == "section_5":
            import re
            risk_match = re.search(r'Risk Level:\s*(\w+)', content)
            ft_match = re.search(r'First-Try.*?:\s*(\d+%)', content)
            ab_match = re.search(r'(control=\d+%.*?\(\d+n\))', content)
            parts = [f"Risk:{risk_match.group(1)}" if risk_match else "",
                     f"FT:{ft_match.group(1)}" if ft_match else "",
                     ab_match.group(1) if ab_match else ""]
            return "[S5:Protocol] " + ' '.join(p for p in parts if p)

        # S7, S10: Dense summary, 400 char max
        if key in ("section_7", "section_10"):
            if len(content) > 400:
                lines = content.strip().split('\n')
                headlines = [l.strip() for l in lines if l.strip() and not l.strip().startswith('─')][:4]
                return '\n'.join(headlines) + "\n  [... full: memory/query.py]"
            return content

        # Fallback: cap at 400 chars
        return content[:400] if len(content) > 400 else content

    # =================================================================
    # MANIFEST TRACKING (Movement 3) — built alongside section assembly
    # =================================================================
    _manifest_included = []
    _manifest_excluded = []
    _section_keys_in_order = []  # Parallel to `sections` list — tracks which key each entry belongs to

    # All known section keys and their labels (for tracking exclusions)
    _all_section_keys = [
        ("section_0", "safety"), ("section_1", "foundation"), ("section_2", "wisdom"),
        ("section_3", "awareness"), ("section_4", "deep_context"), ("section_5", "protocol"),
        ("section_6", "synaptic_to_atlas"), ("section_7", "acontext_library"),
        ("section_8", "8th_intelligence"), ("section_10", "vision"),
    ]

    def _track_section(key, label, content, was_abbreviated=False):
        """Track a section in the manifest. Returns content for chaining."""
        if content:
            tokens_est = len(content) // 4  # rough chars/4 estimate
            _manifest_included.append(ManifestRef(
                section=key, label=label, included=True,
                tokens_est=tokens_est,
                source="abbreviated" if was_abbreviated else "full",
                latency_ms=section_timings.get(key, 0),
            ))
        return content

    # Assemble sections in order (all retrieved from parallel results)
    for key in ["section_0", "section_1", "section_2", "section_3", "section_4"]:
        if key in section_results:
            content, label = section_results[key]
            if content:
                abbreviated = _abbreviate_section(key, content, label)
                _track_section(key, label, abbreviated, was_abbreviated=(abbreviated != content))
                sections.append(abbreviated)
                sections_included.append(label)
                _section_keys_in_order.append(key)
            else:
                _manifest_excluded.append(ManifestRef(
                    section=key, label=label, included=False,
                    exclusion_reason=ExclusionReason.EMPTY_CONTENT.value,
                    latency_ms=section_timings.get(key, 0),
                ))

    # Section 4 handled in loop above, but let's explicitly skip if needed
    section_4 = section_results.get("section_4", (None, None))[0]

    # Section 5: PROTOCOL (from parallel results)
    if "section_5" in section_results:
        content, label = section_results["section_5"]
        if content:
            abbreviated = _abbreviate_section("section_5", content, label)
            _track_section("section_5", label, abbreviated, was_abbreviated=(abbreviated != content))
            sections.append(abbreviated)
            sections_included.append(label)
            _section_keys_in_order.append("section_5")
        else:
            _manifest_excluded.append(ManifestRef(
                section="section_5", label="protocol", included=False,
                exclusion_reason=ExclusionReason.EMPTY_CONTENT.value,
                latency_ms=section_timings.get("section_5", 0),
            ))

    # Section 6: HOLISTIC CONTEXT (Synaptic → Atlas, task-focused)
    # FULL section — keep complete. Cap anticipation context only.
    if "section_6" in section_results or _anticipation_s6:
        live_s6 = section_results.get("section_6", (None, None))[0]
        # Cap anticipation context to prevent unbounded growth
        capped_anticipation = _anticipation_s6
        if capped_anticipation and len(capped_anticipation) > MAX_ANTICIPATION_CHARS:
            capped_anticipation = capped_anticipation[:MAX_ANTICIPATION_CHARS] + "\n  [... anticipation capped for context budget]"
        if capped_anticipation and live_s6:
            combined = live_s6 + "\n\n--- Prior Anticipation Context ---\n" + capped_anticipation
            _track_section("section_6", "synaptic_to_atlas", combined)
            sections.append(combined)
            sections_included.append("synaptic_to_atlas")
            _section_keys_in_order.append("section_6")
        elif live_s6:
            _track_section("section_6", "synaptic_to_atlas", live_s6)
            sections.append(live_s6)
            sections_included.append("synaptic_to_atlas")
            _section_keys_in_order.append("section_6")
        elif capped_anticipation:
            _track_section("section_6", "synaptic_to_atlas", capped_anticipation)
            sections.append(capped_anticipation)
            sections_included.append("synaptic_to_atlas")
            _section_keys_in_order.append("section_6")

    # Section 7: ACONTEXT / FULL LIBRARY (abbreviated)
    if "section_7" in section_results:
        content, label = section_results["section_7"]
        if content:
            abbreviated = _abbreviate_section("section_7", content, label)
            _track_section("section_7", label, abbreviated, was_abbreviated=(abbreviated != content))
            sections.append(abbreviated)
            sections_included.append(label)
            _section_keys_in_order.append("section_7")
        else:
            _manifest_excluded.append(ManifestRef(
                section="section_7", label="acontext_library", included=False,
                exclusion_reason=ExclusionReason.EMPTY_CONTENT.value,
                latency_ms=section_timings.get("section_7", 0),
            ))

    # Section 8: SYNAPTIC'S 8TH INTELLIGENCE — FULL section
    if "section_8" in section_results:
        content, label = section_results["section_8"]
        if content:
            _track_section("section_8", label, content)
            sections.append(content)
            sections_included.append(label)
            _section_keys_in_order.append("section_8")
        else:
            _manifest_excluded.append(ManifestRef(
                section="section_8", label="8th_intelligence", included=False,
                exclusion_reason=ExclusionReason.EMPTY_CONTENT.value,
                latency_ms=section_timings.get("section_8", 0),
            ))

    # Section 10: VISION (abbreviated)
    if "section_10" in section_results:
        content, label = section_results["section_10"]
        if content:
            abbreviated = _abbreviate_section("section_10", content, label)
            _track_section("section_10", label, abbreviated, was_abbreviated=(abbreviated != content))
            sections.append(abbreviated)
            sections_included.append(label)
            _section_keys_in_order.append("section_10")
        else:
            _manifest_excluded.append(ManifestRef(
                section="section_10", label="vision", included=False,
                exclusion_reason=ExclusionReason.EMPTY_CONTENT.value,
                latency_ms=section_timings.get("section_10", 0),
            ))

    # Track sections that never appeared in section_results (generator failed)
    for sec_key, sec_label in _all_section_keys:
        if sec_key not in section_results and not any(
            r.section == sec_key for r in _manifest_included + _manifest_excluded
        ):
            _manifest_excluded.append(ManifestRef(
                section=sec_key, label=sec_label, included=False,
                exclusion_reason=ExclusionReason.GENERATOR_FAILED.value,
            ))

    # Insert boundary filter note if available
    if boundary_decision and boundary_decision.primary_project and boundary_decision.filter_note:
        sections.insert(1, f"  {boundary_decision.filter_note}\n")
        _section_keys_in_order.insert(1, "__boundary_note")

    # ==========================================================================
    # HEADER: Compact 2-line header in BOTH full and abbreviated modes
    # ==========================================================================
    # ASCII box headers (~1200 chars) replaced with dense 2-line header (~160 chars)
    # in both modes. Synaptic protocol is in CLAUDE.md — no need to repeat.
    project_str = f" | {boundary_decision.primary_project}" if boundary_decision and boundary_decision.primary_project else ""
    depth_label = "FULL" if _use_full_injection else "ABBREV"
    header = f"""[ATLAS] Risk:{risk_level.value} FT:{FIRST_TRY_LIKELIHOOD[risk_level]} Mode:{injection_mode.value} Inj#{_count} Depth:{depth_label}{project_str}
[Synaptic protocol active. S2+S6+S8 always full.{' Others abbreviated — full context in earlier turns.' if not _use_full_injection else ''}]
"""

    content = header + "\n\n".join(sections)

    # =================================================================
    # OVERFLOW PROTECTION: Hard cap prevents "prompt too long" crashes
    # =================================================================
    # Progressive stripping: remove lowest-priority sections until under budget.
    # NEVER strip: S0 (Safety), S2 (Wisdom), S6 (Synaptic→Atlas), S8 (8th Intelligence), S10 (Strategic)
    # Strip order: S7 → S4 → S3 → S1 → S5 (lowest value first)
    MAX_PAYLOAD_FULL = 18000    # ~4.5K tokens — turns 1-2 have room (raised: S8 now LLM-voiced ~2K+)
    MAX_PAYLOAD_ABBREV = 14000  # ~3.5K tokens — never-strip set (S0+S2+S6+S8+S10) needs headroom
    _max_chars = MAX_PAYLOAD_FULL if _use_full_injection else MAX_PAYLOAD_ABBREV

    if len(content) > _max_chars:
        _strip_order = ["section_7", "section_4", "section_3", "section_1", "section_5"]
        _removed_keys = set()
        for _strip_key in _strip_order:
            if len(content) <= _max_chars:
                break
            if _strip_key in _section_keys_in_order:
                _removed_keys.add(_strip_key)
                # Rebuild content without removed sections
                _remaining = [(k, s) for k, s in zip(_section_keys_in_order, sections) if k not in _removed_keys]
                content = header + "\n\n".join(s for _, s in _remaining)

        if _removed_keys:
            # Rebuild sections + keys (these ARE parallel)
            _new_sections = []
            _new_keys = []
            for k, s in zip(_section_keys_in_order, sections):
                if k not in _removed_keys:
                    _new_sections.append(s)
                    _new_keys.append(k)
            sections = _new_sections
            _section_keys_in_order = _new_keys
            # Rebuild sections_included (labels — NOT parallel to sections due to boundary note)
            _key_to_label = dict(_all_section_keys)
            _removed_labels = {_key_to_label[k] for k in _removed_keys if k in _key_to_label}
            sections_included = [lbl for lbl in sections_included if lbl not in _removed_labels]
            # Update manifest: move stripped sections from included → excluded
            _still_included = []
            for ref in _manifest_included:
                if ref.section in _removed_keys:
                    _manifest_excluded.append(ManifestRef(
                        section=ref.section, label=ref.label, included=False,
                        exclusion_reason="overflow_protection",
                        tokens_est=ref.tokens_est,
                    ))
                else:
                    _still_included.append(ref)
            _manifest_included = _still_included
            _safe_log('warning', f"[injection] OVERFLOW PROTECTION: stripped {sorted(_removed_keys)}, "
                       f"final={len(content)} chars (limit={_max_chars})")
            # Telemetry: record overflow event in Redis for crash correlation
            try:
                if is_redis_available():
                    import redis
                    r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
                    r.lpush("injection:overflow_events", f"{_count}|{len(content)}|{_max_chars}|{','.join(sorted(_removed_keys))}")
                    r.ltrim("injection:overflow_events", 0, 99)  # Keep last 100
            except Exception:
                pass

    # Calculate generation time
    generation_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

    # =================================================================
    # OUTCOME LOOP RECORDING - Connect injection to outcome tracking
    # =================================================================
    # Record that this hook fired so we can later attribute outcomes to it.
    # This enables A/B testing of injection variants.
    try:
        from memory.hook_evolution import get_hook_evolution_engine
        engine = get_hook_evolution_engine()

        # Get the active variant for this injection
        variant, _ = engine.get_active_variant(
            "UserPromptSubmit",
            session_id,
            prompt,
            risk_level.value if risk_level else None
        )

        if variant:
            # Record that this hook fired
            engine.record_hook_fired(
                variant_id=variant.variant_id,
                session_id=session_id or "",
                trigger_context=prompt[:500] if prompt else "",
                risk_level=risk_level.value if risk_level else ""
            )
    except ImportError:
        pass  # hook_evolution not available
    except Exception as e:
        # Non-blocking - don't fail injection if outcome recording fails
        _safe_log('debug', f"Outcome recording failed: {e}")

    # Extract boundary decision details for result
    boundary_project = None
    boundary_confidence = 0.0
    boundary_action = None
    boundary_filter_note = None
    needs_clarification = False
    clarification_prompt = None
    clarification_options = None

    if boundary_decision:
        boundary_project = boundary_decision.primary_project
        boundary_confidence = boundary_decision.confidence
        boundary_action = boundary_decision.action.value if boundary_decision.action else None
        boundary_filter_note = boundary_decision.filter_note
        needs_clarification = boundary_decision.should_clarify
        clarification_prompt = boundary_decision.clarification_prompt
        clarification_options = boundary_decision.clarification_options

    # Build label-keyed section timings from section_key-keyed timings
    # section_timings: {"section_0": 12, "section_1": 45, ...}
    # section_results: {"section_0": (content, "safety"), ...}
    label_timings = {}
    for sec_key, latency_ms in section_timings.items():
        if sec_key in section_results:
            _, label = section_results[sec_key]
            label_timings[label] = latency_ms

    # =================================================================
    # HOOK EVOLUTION: Auto-record outcome for A/B testing
    # Records the variant used and will be paired with outcome later
    # =================================================================
    try:
        from memory.hook_evolution import get_hook_evolution_engine
        evolution_engine = get_hook_evolution_engine()
        if evolution_engine and session_id:
            # Record that this variant was used for this session
            # Outcome will be recorded later by success_detection or failure_pattern_analyzer
            evolution_engine.record_hook_fire(
                hook_type="UserPromptSubmit",
                session_id=session_id or "default",
                variant_id=ab_variant or "control",
                risk_level=risk_level.value if hasattr(risk_level, 'value') else str(risk_level),
                prompt_hash=_make_cache_key("fire", prompt, "")[:16] if prompt else "unknown"
            )
    except Exception:
        pass  # Non-blocking — never fail injection for telemetry

    # Retrieve learning IDs from section_1 generation (per-learning attribution)
    _learning_ids = getattr(_injection_thread_local, 'learning_ids', [])

    # =================================================================
    # BUILD PAYLOAD MANIFEST (Movement 3 — injection audit trail)
    # =================================================================
    _prompt_hash = hashlib.sha256(prompt[:200].encode()).hexdigest()[:16] if prompt else "empty"
    _manifest = PayloadManifest(
        injection_id=f"inj_{_prompt_hash}_{_count}",
        timestamp=datetime.now(timezone.utc).isoformat(),
        injection_count=_count,
        depth=depth_label,
        risk_level=risk_level.value,
        prompt_hash=_prompt_hash,
        generation_time_ms=generation_time_ms,
        included=_manifest_included,
        excluded=_manifest_excluded,
        total_tokens_est=sum(r.tokens_est for r in _manifest_included),
        boundary_project=boundary_project,
        ab_variant=ab_variant,
    )

    # Emit manifest to .projectdna/derived/ (non-blocking, fail-open)
    try:
        threading.Thread(target=_emit_manifest, args=(_manifest,), daemon=True).start()
    except Exception:
        pass  # Never block injection for manifest emission

    # Record quality metrics to Redis (non-blocking, fail-open)
    try:
        threading.Thread(
            target=_record_quality_metrics,
            args=(_manifest, generation_time_ms, len(content)),
            daemon=True
        ).start()
    except Exception:
        pass  # Never block injection for metrics recording

    # Movement 6: Self-reference suppression (active when product_mode=true)
    try:
        from memory.self_reference_filter import filter_content as _srf
        content = _srf(content)
    except Exception:
        pass  # Never block injection for filter failures

    # Fire anticipation pre-computation for NEXT prompt (non-blocking background thread)
    # Wires webhook lifecycle → anticipation refresh (fixes cache staleness)
    try:
        fire_anticipation_after_webhook()
    except Exception:
        pass  # Never block injection for anticipation failures

    # =================================================================
    # RACE O3: Publish webhook health event for daemon /health.webhook
    # =================================================================
    # Fire-and-forget NATS pub. Daemon's WebhookHealthAggregator subscribes
    # to event.webhook.completed.<node> and surfaces aggregates at
    # /health.webhook. Publish failures bump publisher.publish_errors —
    # never raise into the webhook hot path (ZSF).
    try:
        from memory.webhook_health_publisher import get_publisher as _get_health_pub

        _pub = _get_health_pub()

        # Build per-section status dict from section_results + section_timings.
        # section_results: {"section_X": (content, label)} — empty content == fail.
        # section_timings: {"section_X": latency_ms}
        _SECTION_KEYS = (
            "section_0", "section_1", "section_2", "section_3", "section_4",
            "section_5", "section_6", "section_7", "section_8", "section_10",
        )
        _pub_sections = {}
        _pub_missing = []
        for _sk in _SECTION_KEYS:
            if _sk in section_results:
                _content, _label = section_results[_sk]
                _ok = bool(_content and str(_content).strip())
                _pub_sections[_sk] = {
                    "latency_ms": int(section_timings.get(_sk, 0) or 0),
                    "ok": _ok,
                }
                if not _ok:
                    _pub_missing.append(_sk)
            # Sections never attempted are simply omitted — not counted as missing.

        # Cache hit signal: M3 anticipation cache provides S2/S6/S8/S10 with
        # latency==0. We treat "any anticipation hit" as a cache hit — best
        # signal we have without deeper M3 wiring.
        _pub_cache_hit = bool(
            (locals().get("_anticipation_s2") is not None)
            or (locals().get("_anticipation_s6") is not None)
            or (locals().get("_anticipation_s8") is not None)
            or (locals().get("_anticipation_s10") is not None)
        )

        _pub.publish(
            total_ms=int(generation_time_ms),
            sections=_pub_sections,
            missing=_pub_missing,
            cache_hit=_pub_cache_hit,
        )
    except Exception:
        # ZSF: publish errors are already counted inside the publisher;
        # this guard catches *anything* before publisher.publish() is reached
        # (import failure, locals() weirdness, etc). Never break webhook.
        pass

    return InjectionResult(
        content=content,
        sections_included=sections_included,
        risk_level=risk_level,
        first_try_likelihood=FIRST_TRY_LIKELIHOOD[risk_level],
        mode=injection_mode,
        ab_variant=ab_variant,
        generation_time_ms=generation_time_ms,
        volume_tier=volume_tier.value,
        failure_count=failure_count,
        section_timings=label_timings if label_timings else None,
        # Boundary Intelligence fields
        boundary_project=boundary_project,
        boundary_confidence=boundary_confidence,
        boundary_action=boundary_action,
        boundary_filter_note=boundary_filter_note,
        needs_clarification=needs_clarification,
        clarification_prompt=clarification_prompt,
        clarification_options=clarification_options,
        # Per-learning outcome attribution
        learning_ids=_learning_ids,
        # Payload manifest (Movement 3)
        manifest=_manifest,
    )


# =============================================================================
# ANTICIPATION: Fire pre-computation for NEXT message (non-blocking)
# =============================================================================

def _fire_anticipation_background():
    """Fire-and-forget anticipation cycle in background thread."""
    try:
        from memory.anticipation_engine import run_anticipation_cycle
        result = run_anticipation_cycle()
        if result.get("ran"):
            logger.info(
                f"[anticipation] Background cycle: {result.get('sections_cached', [])} "
                f"in {result.get('elapsed_ms', 0)}ms"
            )
    except Exception as e:
        logger.debug(f"[anticipation] Background fire failed (non-blocking): {e}")


def fire_anticipation_after_webhook():
    """
    Start anticipation pre-computation after webhook delivery.

    Called after generate_context_injection completes.
    Runs in a daemon thread — never blocks the webhook response.
    """
    import threading
    t = threading.Thread(target=_fire_anticipation_background, daemon=True,
                         name="anticipation_post_webhook")
    t.start()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Ensure parent directory is in path for memory.* imports
    sys.path.insert(0, str(Path(__file__).parent.parent))

    if len(sys.argv) < 2:
        print("Usage: python persistent_hook_structure.py <prompt> [mode]")
        print("")
        print("Modes: layered, greedy, hybrid, minimal")
        print("")
        print("Example:")
        print('  python persistent_hook_structure.py "deploy Django to production"')
        print('  python persistent_hook_structure.py "fix button style" minimal')
        sys.exit(1)

    prompt = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "hybrid"
    session_id = os.environ.get("CLAUDE_SESSION_ID", "")

    result = generate_context_injection(prompt, mode)

    # Store injection for dashboard visualization
    # IMPORTANT: Use memory.* imports first to match agent_service's import path,
    # preventing duplicate singletons (and duplicate FDs) from split sys.modules entries
    try:
        try:
            from memory.injection_store import get_injection_store, build_injection_data, record_injection_to_health_monitor
        except ImportError:
            from injection_store import get_injection_store, build_injection_data, record_injection_to_health_monitor
        store = get_injection_store()
        injection_data = build_injection_data(
            prompt=prompt,
            injection_result=result,
            session_id=session_id,
            hook_name="UserPromptSubmit"
        )
        store.store_injection(injection_data)
        record_injection_to_health_monitor(injection_data, "vs_code_claude_code", "pre_message")
    except Exception as e:
        print(f"[WARN] Injection storage failed (non-fatal): {e}")

    print(result.content)
    print("")
    tier_names = {1: "Silver Platter", 2: "Expanded", 3: "Full Library"}
    tier_name = tier_names.get(result.volume_tier, "Unknown")
    print(f"[Sections: {', '.join(result.sections_included)}]")
    print(f"[Volume Tier: {result.volume_tier} ({tier_name}) | Failures: {result.failure_count}]")

    # Boundary Intelligence info
    if result.boundary_project:
        print(f"[Boundary: {result.boundary_project} ({result.boundary_confidence:.0%}) | Action: {result.boundary_action}]")
    if result.needs_clarification:
        print(f"[⚠️ CLARIFICATION NEEDED: {result.clarification_prompt}]")
        print(f"[Options: {result.clarification_options}]")

    print(f"[Generated in {result.generation_time_ms}ms]")
