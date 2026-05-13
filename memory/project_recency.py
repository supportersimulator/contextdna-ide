#!/usr/bin/env python3
"""
PROJECT RECENCY TRACKER - Redis-backed recent project tracking

Tracks recently worked-on projects using Redis sorted sets with time decay.
This is one of the 5 input signals for Project Boundary Intelligence.

ARCHITECTURE:
- Redis sorted set stores projects with timestamps as scores
- Time decay gives recent projects higher weight
- Automatic cleanup of stale entries via Celery beat

Redis Keys:
- contextdna:boundary:recency:{session_id} - Per-session recency (1 hour TTL)
- contextdna:boundary:recency:global - Global recency across sessions
- contextdna:boundary:activity:{project} - Project activity timestamps

Usage:
    from memory.project_recency import get_recency_tracker

    tracker = get_recency_tracker()

    # Record activity
    tracker.record_activity("context-dna", "memory/hook_evolution.py", session_id="sess_123")

    # Get recent projects with decay-weighted scores
    recent = tracker.get_recent_projects(session_id="sess_123", limit=5)
    # Returns: [("context-dna", 0.95), ("ersim-voice-stack", 0.72), ...]
"""

import os
import time
import json
import math
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger('contextdna.recency')

# =============================================================================
# CONFIGURATION
# =============================================================================

# Redis key prefixes
KEY_PREFIX = "contextdna:boundary:"
KEY_RECENCY_SESSION = f"{KEY_PREFIX}recency:session:"
KEY_RECENCY_GLOBAL = f"{KEY_PREFIX}recency:global"
KEY_ACTIVITY = f"{KEY_PREFIX}activity:"
KEY_FILE_PROJECT = f"{KEY_PREFIX}file_project:"

# TTLs (in seconds)
TTL_SESSION_RECENCY = 3600      # 1 hour for session-specific recency
TTL_GLOBAL_RECENCY = 86400      # 24 hours for global recency
TTL_FILE_PROJECT = 604800       # 7 days for file→project mappings

# Decay configuration
DECAY_HALF_LIFE = 3600  # 1 hour half-life for decay
MAX_PROJECTS = 50       # Max projects to track
MIN_SCORE = 0.01        # Minimum score before removal


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class ProjectActivity:
    """Record of activity on a project."""
    project: str
    file_path: str
    timestamp: float
    session_id: str
    score: float = 1.0

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class RecencyScore:
    """A project with its recency-weighted score."""
    project: str
    score: float
    last_active: float
    activity_count: int

    @property
    def last_active_iso(self) -> str:
        return datetime.fromtimestamp(self.last_active).isoformat()


# =============================================================================
# RECENCY TRACKER
# =============================================================================

class ProjectRecencyTracker:
    """
    Track recently worked-on projects with time decay.

    Uses Redis sorted sets for efficient tracking:
    - ZADD for adding/updating projects with timestamp scores
    - ZREVRANGEBYSCORE for getting recent projects
    - Decay applied at query time (not stored)
    """

    def __init__(self, redis_client=None):
        """
        Initialize recency tracker.

        Args:
            redis_client: Optional Redis client. If None, will try to get from redis_cache.
        """
        self._redis = redis_client
        self._local_cache: Dict[str, List[Tuple[str, float]]] = {}  # Fallback when Redis unavailable

    @property
    def redis(self):
        """Lazy load Redis client."""
        if self._redis is None:
            try:
                from memory.redis_cache import get_redis_client
                self._redis = get_redis_client()
            except ImportError:
                logger.warning("redis_cache not available")
        return self._redis

    # =========================================================================
    # RECORDING ACTIVITY
    # =========================================================================

    def record_activity(
        self,
        project: str,
        file_path: str = None,
        session_id: str = None,
        timestamp: float = None
    ) -> bool:
        """
        Record activity on a project.

        Updates both session-specific and global recency trackers.

        Args:
            project: Project name or path
            file_path: The file being worked on (for file→project mapping)
            session_id: Current session ID
            timestamp: Activity timestamp (defaults to now)

        Returns:
            True if recorded successfully
        """
        if not project:
            return False

        ts = timestamp or time.time()
        project = self._normalize_project_name(project)

        # Record to Redis if available
        if self.redis:
            try:
                # Update global recency
                self.redis.zadd(KEY_RECENCY_GLOBAL, {project: ts})
                self.redis.expire(KEY_RECENCY_GLOBAL, TTL_GLOBAL_RECENCY)

                # Update session recency if session provided
                if session_id:
                    session_key = f"{KEY_RECENCY_SESSION}{session_id}"
                    self.redis.zadd(session_key, {project: ts})
                    self.redis.expire(session_key, TTL_SESSION_RECENCY)

                # Store file→project mapping for learning
                if file_path:
                    file_key = f"{KEY_FILE_PROJECT}{self._hash_path(file_path)}"
                    self.redis.setex(file_key, TTL_FILE_PROJECT, project)

                # Track activity count
                activity_key = f"{KEY_ACTIVITY}{project}"
                self.redis.hincrby(activity_key, "count", 1)
                self.redis.hset(activity_key, "last_active", str(ts))
                self.redis.expire(activity_key, TTL_GLOBAL_RECENCY)

                logger.debug(f"Recorded activity: {project}")
                return True

            except Exception as e:
                logger.warning(f"Redis record failed: {e}")
                # Fall through to local cache

        # Fallback to local cache
        return self._record_local(project, ts, session_id)

    def _record_local(self, project: str, timestamp: float, session_id: str = None) -> bool:
        """Record activity to local cache (fallback)."""
        cache_key = session_id or "global"

        if cache_key not in self._local_cache:
            self._local_cache[cache_key] = []

        # Update or add project
        entries = self._local_cache[cache_key]
        for i, (p, _) in enumerate(entries):
            if p == project:
                entries[i] = (project, timestamp)
                break
        else:
            entries.append((project, timestamp))

        # Sort by timestamp descending and trim
        entries.sort(key=lambda x: -x[1])
        self._local_cache[cache_key] = entries[:MAX_PROJECTS]

        return True

    # =========================================================================
    # QUERYING RECENT PROJECTS
    # =========================================================================

    def get_recent_projects(
        self,
        session_id: str = None,
        limit: int = 5,
        include_global: bool = True,
        apply_decay: bool = True
    ) -> List[Tuple[str, float]]:
        """
        Get recently worked-on projects with decay-weighted scores.

        Args:
            session_id: Session ID for session-specific recency
            limit: Maximum number of projects to return
            include_global: Whether to include global recency (merged with session)
            apply_decay: Whether to apply time decay to scores

        Returns:
            List of (project_name, score) tuples, sorted by score descending
        """
        now = time.time()
        project_scores: Dict[str, float] = {}

        # Get from Redis if available
        if self.redis:
            try:
                # Get session-specific recency
                if session_id:
                    session_key = f"{KEY_RECENCY_SESSION}{session_id}"
                    session_data = self.redis.zrevrange(session_key, 0, limit * 2, withscores=True)
                    for project, ts in session_data:
                        score = self._calculate_decay_score(ts, now) if apply_decay else 1.0
                        # Session recency gets 1.2x weight
                        project_scores[project] = score * 1.2

                # Get global recency
                if include_global:
                    global_data = self.redis.zrevrange(KEY_RECENCY_GLOBAL, 0, limit * 2, withscores=True)
                    for project, ts in global_data:
                        score = self._calculate_decay_score(ts, now) if apply_decay else 1.0
                        if project in project_scores:
                            # Combine session and global (session has priority)
                            project_scores[project] = max(project_scores[project], score * 0.8)
                        else:
                            project_scores[project] = score * 0.8

            except Exception as e:
                logger.warning(f"Redis query failed: {e}")
                # Fall through to local cache

        # Use local cache if Redis unavailable or empty
        if not project_scores:
            project_scores = self._get_recent_local(session_id, now, apply_decay, include_global)

        # Sort by score descending and limit
        sorted_projects = sorted(project_scores.items(), key=lambda x: -x[1])
        return sorted_projects[:limit]

    def _get_recent_local(
        self,
        session_id: str,
        now: float,
        apply_decay: bool,
        include_global: bool
    ) -> Dict[str, float]:
        """Get recent projects from local cache."""
        project_scores: Dict[str, float] = {}

        # Session cache
        if session_id and session_id in self._local_cache:
            for project, ts in self._local_cache[session_id]:
                score = self._calculate_decay_score(ts, now) if apply_decay else 1.0
                project_scores[project] = score * 1.2

        # Global cache
        if include_global and "global" in self._local_cache:
            for project, ts in self._local_cache["global"]:
                score = self._calculate_decay_score(ts, now) if apply_decay else 1.0
                if project in project_scores:
                    project_scores[project] = max(project_scores[project], score * 0.8)
                else:
                    project_scores[project] = score * 0.8

        return project_scores

    def get_project_activity(self, project: str) -> Optional[Dict]:
        """Get activity statistics for a project."""
        if not self.redis:
            return None

        try:
            activity_key = f"{KEY_ACTIVITY}{self._normalize_project_name(project)}"
            data = self.redis.hgetall(activity_key)
            if data:
                return {
                    "project": project,
                    "count": int(data.get("count", 0)),
                    "last_active": float(data.get("last_active", 0)),
                    "last_active_iso": datetime.fromtimestamp(float(data.get("last_active", 0))).isoformat()
                }
        except Exception as e:
            logger.warning(f"Failed to get project activity: {e}")

        return None

    def get_project_from_file(self, file_path: str) -> Optional[str]:
        """Get the project associated with a file path (from learned mapping)."""
        if not self.redis or not file_path:
            return None

        try:
            file_key = f"{KEY_FILE_PROJECT}{self._hash_path(file_path)}"
            return self.redis.get(file_key)
        except Exception as e:
            logger.warning(f"Failed to get project from file: {e}")
            return None

    # =========================================================================
    # DECAY CALCULATIONS
    # =========================================================================

    def _calculate_decay_score(self, timestamp: float, now: float) -> float:
        """
        Calculate decay-weighted score for a timestamp.

        Uses exponential decay with configurable half-life.
        Score = 0.5 ^ (time_elapsed / half_life)

        Args:
            timestamp: Unix timestamp of activity
            now: Current timestamp

        Returns:
            Score between 0.0 and 1.0
        """
        elapsed = now - timestamp
        if elapsed <= 0:
            return 1.0

        # Exponential decay: score = 0.5 ^ (elapsed / half_life)
        decay = math.pow(0.5, elapsed / DECAY_HALF_LIFE)
        return max(MIN_SCORE, decay)

    def apply_decay(self) -> int:
        """
        Apply decay and clean up stale entries.

        Called by Celery beat periodically.

        Returns:
            Number of entries cleaned up
        """
        if not self.redis:
            return 0

        cleaned = 0
        now = time.time()
        cutoff = now - (DECAY_HALF_LIFE * 8)  # Remove entries after 8 half-lives (~0.4% score)

        try:
            # Clean global recency
            removed = self.redis.zremrangebyscore(KEY_RECENCY_GLOBAL, "-inf", cutoff)
            cleaned += removed or 0

            # Clean session recency keys
            # Note: Session keys have TTL, so they self-clean, but we can trim old entries
            for key in self.redis.scan_iter(f"{KEY_RECENCY_SESSION}*"):
                removed = self.redis.zremrangebyscore(key, "-inf", cutoff)
                cleaned += removed or 0

            if cleaned > 0:
                logger.info(f"Cleaned up {cleaned} stale recency entries")

        except Exception as e:
            logger.error(f"Failed to apply decay: {e}")

        return cleaned

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _normalize_project_name(self, project: str) -> str:
        """Normalize project name for consistent storage."""
        if not project:
            return ""

        # Remove common path prefixes
        project = project.strip()

        # Extract project name from path if needed
        if "/" in project:
            # Get the last meaningful directory
            parts = Path(project).parts
            for part in reversed(parts):
                if part not in {"src", "lib", "app", "services", "packages"}:
                    return part.lower()

        return project.lower()

    def _hash_path(self, file_path: str) -> str:
        """Create a short hash of a file path for Redis key."""
        import hashlib
        return hashlib.md5(file_path.encode()).hexdigest()[:12]

    # =========================================================================
    # STATISTICS
    # =========================================================================

    def get_stats(self) -> Dict:
        """Get recency tracker statistics."""
        stats = {
            "redis_available": self.redis is not None,
            "global_project_count": 0,
            "session_count": 0,
            "total_activities": 0
        }

        if self.redis:
            try:
                stats["global_project_count"] = self.redis.zcard(KEY_RECENCY_GLOBAL) or 0

                # Count session keys
                session_count = 0
                for _ in self.redis.scan_iter(f"{KEY_RECENCY_SESSION}*"):
                    session_count += 1
                stats["session_count"] = session_count

                # Sum activity counts
                total_activities = 0
                for key in self.redis.scan_iter(f"{KEY_ACTIVITY}*"):
                    count = self.redis.hget(key, "count")
                    if count:
                        total_activities += int(count)
                stats["total_activities"] = total_activities

            except Exception as e:
                logger.warning(f"Failed to get stats: {e}")

        return stats


# =============================================================================
# SINGLETON
# =============================================================================

_instance: Optional[ProjectRecencyTracker] = None


def get_recency_tracker(redis_client=None) -> ProjectRecencyTracker:
    """Get the singleton recency tracker instance."""
    global _instance
    if _instance is None:
        _instance = ProjectRecencyTracker(redis_client)
    elif redis_client and not _instance._redis:
        _instance._redis = redis_client
    return _instance


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    tracker = get_recency_tracker()

    if len(sys.argv) < 2:
        print("Project Recency Tracker")
        print("=" * 50)
        stats = tracker.get_stats()
        print(f"Redis Available: {stats['redis_available']}")
        print(f"Global Projects: {stats['global_project_count']}")
        print(f"Active Sessions: {stats['session_count']}")
        print(f"Total Activities: {stats['total_activities']}")
        print()
        print("Commands:")
        print("  python project_recency.py record <project> [file_path] [session_id]")
        print("  python project_recency.py recent [session_id] [limit]")
        print("  python project_recency.py activity <project>")
        print("  python project_recency.py decay")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "record":
        if len(sys.argv) < 3:
            print("Usage: python project_recency.py record <project> [file_path] [session_id]")
            sys.exit(1)

        project = sys.argv[2]
        file_path = sys.argv[3] if len(sys.argv) > 3 else None
        session_id = sys.argv[4] if len(sys.argv) > 4 else None

        result = tracker.record_activity(project, file_path, session_id)
        print(f"Recorded: {project} -> {'Success' if result else 'Failed'}")

    elif cmd == "recent":
        session_id = sys.argv[2] if len(sys.argv) > 2 else None
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 5

        recent = tracker.get_recent_projects(session_id, limit)
        print(f"Recent Projects (session={session_id or 'global'}):")
        for project, score in recent:
            print(f"  {project}: {score:.3f}")

    elif cmd == "activity":
        if len(sys.argv) < 3:
            print("Usage: python project_recency.py activity <project>")
            sys.exit(1)

        project = sys.argv[2]
        activity = tracker.get_project_activity(project)
        if activity:
            print(f"Project: {activity['project']}")
            print(f"  Activity Count: {activity['count']}")
            print(f"  Last Active: {activity['last_active_iso']}")
        else:
            print(f"No activity data for: {project}")

    elif cmd == "decay":
        cleaned = tracker.apply_decay()
        print(f"Cleaned up {cleaned} stale entries")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
