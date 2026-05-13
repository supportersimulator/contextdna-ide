#!/usr/bin/env python3
"""
WEBHOOK SESSION — Session failure tracking and volume tier escalation.

Extracted from persistent_hook_structure.py (God File Phase 1).

Functions:
- check_session_failures() — Check if session has failures
- record_session_failure() — Record a failure event
- clear_session_failures() — Clear session failures
- get_session_failure_count() — Get failure count for tier escalation
- _postgres_is_available() — PostgreSQL availability check
- determine_volume_tier() — Three-tier volume escalation logic
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from memory.webhook_types import VolumeEscalationTier

# Base paths (match persistent_hook_structure.py)
PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PROJECT_ROOT / "memory"

logger = logging.getLogger('context_dna')


def _safe_log(level: str, message: str):
    """Per-section logging with stderr fallback."""
    try:
        if level == 'warning':
            logger.warning(message)
        elif level == 'debug':
            logger.debug(message)
        else:
            logger.info(message)
    except Exception:
        print(f"[{level.upper()}] {message}", file=sys.stderr)


# Session failure tracking file
SESSION_FAILURE_FILE = MEMORY_DIR / ".session_failures.json"


def _is_redis_available() -> bool:
    """Internal Redis check — avoids circular import with webhook_utils."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client:
            client.ping()
            return True
    except Exception:
        pass
    return False


def check_session_failures(session_id: Optional[str] = None) -> bool:
    """Check if current session has any failures recorded.

    Uses Redis when available for real-time sync, falls back to file storage.
    """
    # Try Redis first (if containers running)
    if _is_redis_available():
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
    if _is_redis_available():
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
    if _is_redis_available():
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


def get_session_failure_count(session_id: Optional[str] = None) -> int:
    """Get actual failure count for session (not just boolean).

    This enables Three-Tier Volume Escalation:
    - 0 failures -> Tier 1 (Silver Platter)
    - 1 failure -> Tier 2 (Expanded)
    - 2+ failures -> Tier 3 (Full Library / Textbook Mode)

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
    if _is_redis_available():
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
