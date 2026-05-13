#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════════════════════╗
║                    CONTEXT DNA UNIFIED STORAGE LAYER                           ║
╠═══════════════════════════════════════════════════════════════════════════════╣
║                                                                                ║
║   ██████╗ ██████╗ ██╗███╗   ███╗ █████╗ ██████╗ ██╗   ██╗                     ║
║   ██╔══██╗██╔══██╗██║████╗ ████║██╔══██╗██╔══██╗╚██╗ ██╔╝                     ║
║   ██████╔╝██████╔╝██║██╔████╔██║███████║██████╔╝ ╚████╔╝                      ║
║   ██╔═══╝ ██╔══██╗██║██║╚██╔╝██║██╔══██║██╔══██╗  ╚██╔╝                       ║
║   ██║     ██║  ██║██║██║ ╚═╝ ██║██║  ██║██║  ██║   ██║                        ║
║   ╚═╝     ╚═╝  ╚═╝╚═╝╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝                        ║
║                                                                                ║
║   ████████████████████████████████████████████████████████████                 ║
║   █  POSTGRESQL = PRIMARY DATABASE (FULL FEATURES, ALWAYS USE)  █             ║
║   ████████████████████████████████████████████████████████████                 ║
║                                                                                ║
║   ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒                 ║
║   ▒  REDIS = HOT CACHE (Speed Layer, Pub/Sub, Realtime)       ▒                ║
║   ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒                 ║
║                                                                                ║
║   ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░                 ║
║   ░  SQLITE = EMERGENCY FALLBACK ONLY (When containers down)  ░               ║
║   ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░                 ║
║                                                                                ║
╠═══════════════════════════════════════════════════════════════════════════════╣
║                                                                                ║
║   PRIORITY ORDER (ALWAYS FOLLOW):                                              ║
║   ┌────┬───────────────────────────────────────────────────────────────────┐  ║
║   │ 1. │ PostgreSQL - PRIMARY - Full features, events, embeddings, adapt  │  ║
║   │ 2. │ Redis      - CACHE   - Fast reads, pub/sub, realtime updates     │  ║
║   │ 3. │ SQLite     - FALLBACK- ONLY when PostgreSQL is UNAVAILABLE       │  ║
║   └────┴───────────────────────────────────────────────────────────────────┘  ║
║                                                                                ║
║   ⚠️  IMPORTANT FOR AI AGENTS (Atlas, Synaptic, Workers):                     ║
║       - SQLite is the OLD Acontext system, kept for backwards compatibility   ║
║       - PostgreSQL has adaptive hierarchy learning, embeddings, events        ║
║       - ALWAYS prefer PostgreSQL when available                               ║
║       - SQLite fallback is ONLY for when containers are down                  ║
║                                                                                ║
╚═══════════════════════════════════════════════════════════════════════════════╝

Philosophy: The system MUST work even if containers are down.
SQLite fallback ensures Context DNA never stops functioning, but it's
INFERIOR to PostgreSQL and should NEVER be used as the primary store.

Architecture:
┌─────────────────────────────────────────────────────────────────────────────┐
│                    UNIFIED STORAGE LAYER                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                   │
│  │  PostgreSQL  │    │    Redis     │    │   SQLite     │                   │
│  │  (PRIMARY)   │    │  (HOT CACHE) │    │  (FALLBACK)  │                   │
│  │              │    │              │    │              │                   │
│  │ • Learnings  │    │ • Read cache │    │ • Learnings  │                   │
│  │ • Hierarchy  │    │ • Pub/Sub    │    │ • Patterns   │                   │
│  │ • Events     │    │ • Sessions   │    │ • Session ctx│                   │
│  │ • Embeddings │    │              │    │              │                   │
│  └──────────────┘    └──────────────┘    └──────────────┘                   │
│         │                  │                    │                            │
│         └──────────────────┼────────────────────┘                            │
│                            │                                                 │
│                   ┌────────▼────────┐                                        │
│                   │ UnifiedStorage  │                                        │
│                   │     Client      │                                        │
│                   └─────────────────┘                                        │
│                            │                                                 │
│              ┌─────────────┴─────────────┐                                   │
│              │                           │                                   │
│         ┌────▼────┐               ┌──────▼─────┐                             │
│         │ Webhook │               │  Dashboard  │                            │
│         │Injection│               │     API     │                            │
│         └─────────┘               └─────────────┘                            │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘

Usage:
    from memory.unified_storage import UnifiedStorage

    storage = UnifiedStorage()

    # Store a learning (auto-routes to best available backend)
    storage.store_learning(type="fix", title="Bug fix", content="...")

    # Query learnings (uses cache if available)
    results = storage.query_learnings("async boto3")

    # Record an event (for Store→Observe→Learn pipeline)
    storage.record_event("session_start", {"ide": "claude_code"})
"""

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Optional imports - graceful degradation
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

try:
    import redis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class StorageConfig:
    """
    Configuration for storage backends.

    ═══════════════════════════════════════════════════════════════════════
    BACKEND HIERARCHY (IMPORTANT):

    ★★★ PostgreSQL = PRIMARY DATABASE
        - Docker stack: context-dna (container: context-dna-postgres)
        - Port 5432 (host:container same)
        - Two databases: context_dna (observability) + contextdna (hierarchy)
        - This is where all new features go

    ★★  Redis = CACHE/PUBSUB LAYER
        - Docker stack: context-dna (container: context-dna-redis)
        - Port 6379 (host:container same)

    ★   SQLite = EMERGENCY FALLBACK ONLY
        - Files named *_FALLBACK_* to be explicit
        - ONLY used when PostgreSQL is unavailable
    ═══════════════════════════════════════════════════════════════════════
    """
    # ═══════════════════════════════════════════════════════════════════
    # ★★★ PRIMARY: PostgreSQL (ALWAYS PREFER THIS)
    # ═══════════════════════════════════════════════════════════════════
    # LEARNINGS_DB_* env vars take priority (specific to this storage layer),
    # then fall back to general DATABASE_* vars, then hardcoded defaults.
    # The learnings table lives in context_dna DB on port 5432 (Stack 1),
    # NOT in acontext DB on port 15432 (Stack 2).
    pg_host: str = os.environ.get("LEARNINGS_DB_HOST", os.environ.get("DATABASE_HOST", "127.0.0.1"))
    pg_port: int = int(os.environ.get("LEARNINGS_DB_PORT", os.environ.get("DATABASE_PORT", "5432")))
    pg_database: str = os.environ.get("LEARNINGS_DB_NAME", os.environ.get("DATABASE_NAME", "context_dna"))
    pg_user: str = os.environ.get("LEARNINGS_DB_USER", os.environ.get("DATABASE_USER", "context_dna"))
    pg_password: str = os.environ.get("LEARNINGS_DB_PASSWORD", os.environ.get("DATABASE_PASSWORD", "context_dna_dev"))

    # ═══════════════════════════════════════════════════════════════════
    # ★★ CACHE: Redis (Speed Layer)
    # ═══════════════════════════════════════════════════════════════════
    # CD_REDIS_* avoids .env hijacking (REDIS_* is for contextdna-redis on 16379)
    redis_host: str = os.environ.get("CD_REDIS_HOST", "127.0.0.1")
    redis_port: int = int(os.environ.get("CD_REDIS_PORT", "6379"))
    redis_password: str = os.environ.get("CD_REDIS_PASSWORD", "")
    redis_cache_ttl: int = 300  # 5 minutes

    # ═══════════════════════════════════════════════════════════════════
    # ★ EMERGENCY FALLBACK: SQLite (ONLY when containers down!)
    # ═══════════════════════════════════════════════════════════════════
    # NOTE: These files are named with FALLBACK to make it CRYSTAL CLEAR
    # that they are NOT the primary database. SQLite is the OLD Acontext
    # system kept for backwards compatibility ONLY.
    #
    # ⚠️  If you're reading from these files when PostgreSQL is available,
    #     something is WRONG with the configuration!
    # ═══════════════════════════════════════════════════════════════════
    sqlite_fallback_learnings_path: Path = field(
        default_factory=lambda: Path.home() / ".context-dna" / "FALLBACK_learnings.db"
    )
    sqlite_fallback_patterns_path: Path = field(
        default_factory=lambda: Path.home() / ".context-dna" / "FALLBACK_pattern_evolution.db"
    )

    # Legacy paths (for migration only - symlink to new FALLBACK names)
    _legacy_learnings_path: Path = field(
        default_factory=lambda: Path.home() / ".context-dna" / "learnings.db"
    )
    _legacy_patterns_path: Path = field(
        default_factory=lambda: Path.home() / ".context-dna" / ".pattern_evolution.db"
    )


class StorageBackend(Enum):
    """
    Available storage backends with EXPLICIT priority.

    ████████████████████████████████████████████████████████████
    █  ORDER OF PREFERENCE (ALWAYS FOLLOW):                    █
    █  1. POSTGRESQL = PRIMARY (full features, use this!)      █
    █  2. REDIS = CACHE (speed layer, pub/sub)                 █
    █  3. SQLITE = EMERGENCY FALLBACK ONLY (old Acontext)      █
    ████████████████████████████████████████████████████████████
    """
    POSTGRESQL = "postgresql"          # ★★★ PRIMARY - Always prefer this
    REDIS = "redis"                    # ★★  CACHE - Speed layer
    SQLITE_FALLBACK = "sqlite_fallback"  # ★   EMERGENCY ONLY - When containers down


# =============================================================================
# Unified Storage Client
# =============================================================================

class UnifiedStorage:
    """
    Unified storage interface with automatic fallback.

    Priority order:
    1. PostgreSQL (primary, full features)
    2. Redis (hot cache for reads)
    3. SQLite (fallback, always available)
    """

    def __init__(self, config: Optional[StorageConfig] = None):
        self.config = config or StorageConfig()
        self._pg_conn: Optional[Any] = None
        self._redis_client: Optional[Any] = None
        self._backends_available: Dict[StorageBackend, bool] = {}

        # Check which backends are available
        self._check_backends()

    def _check_backends(self):
        """Check which storage backends are available."""
        # ═══════════════════════════════════════════════════════════════════
        # ★★★ CHECK PRIMARY: PostgreSQL (ALWAYS TRY THIS FIRST)
        # ═══════════════════════════════════════════════════════════════════
        self._backends_available[StorageBackend.POSTGRESQL] = False
        if HAS_PSYCOPG2:
            try:
                conn = psycopg2.connect(
                    host=self.config.pg_host,
                    port=self.config.pg_port,
                    database=self.config.pg_database,
                    user=self.config.pg_user,
                    password=self.config.pg_password,
                    connect_timeout=3
                )
                conn.close()
                self._backends_available[StorageBackend.POSTGRESQL] = True
            except Exception as e:
                print(f"[WARN] PostgreSQL connection check failed: {e}")

        # ═══════════════════════════════════════════════════════════════════
        # ★★ CHECK CACHE: Redis
        # ═══════════════════════════════════════════════════════════════════
        self._backends_available[StorageBackend.REDIS] = False
        if HAS_REDIS:
            try:
                client = redis.Redis(
                    host=self.config.redis_host,
                    port=self.config.redis_port,
                    password=self.config.redis_password,
                    socket_timeout=3
                )
                client.ping()
                self._backends_available[StorageBackend.REDIS] = True
            except Exception as e:
                print(f"[WARN] Redis connection check failed: {e}")

        # ═══════════════════════════════════════════════════════════════════
        # ★ EMERGENCY FALLBACK: SQLite (ONLY when PostgreSQL unavailable!)
        # ⚠️  This is NOT the primary database - just emergency fallback
        # ═══════════════════════════════════════════════════════════════════
        self._backends_available[StorageBackend.SQLITE_FALLBACK] = True
        self._setup_fallback_symlinks()

    def _setup_fallback_symlinks(self):
        """
        Create symlinks from legacy SQLite paths to new FALLBACK-named paths.
        This ensures:
        1. Old code still works (reads from learnings.db)
        2. New names make it OBVIOUS these are fallback databases
        3. Data is not duplicated
        """
        try:
            # Migrate legacy learnings.db → FALLBACK_learnings.db
            legacy = self.config._legacy_learnings_path
            fallback = self.config.sqlite_fallback_learnings_path

            if legacy.exists() and not legacy.is_symlink():
                # Legacy file exists - rename to FALLBACK name
                if not fallback.exists():
                    legacy.rename(fallback)
                    legacy.symlink_to(fallback)
            elif not fallback.exists() and legacy.exists() and legacy.is_symlink():
                # Symlink exists but target doesn't - create empty fallback
                fallback.parent.mkdir(parents=True, exist_ok=True)
                fallback.touch()
        except Exception:
            pass  # Non-critical - just use whichever exists

    def _get_pg_conn(self):
        """Get PostgreSQL connection (lazy, pooled). ★★★ PRIMARY DATABASE"""
        if not self._backends_available[StorageBackend.POSTGRESQL]:
            return None
        if self._pg_conn is None or self._pg_conn.closed:
            try:
                self._pg_conn = psycopg2.connect(
                    host=self.config.pg_host,
                    port=self.config.pg_port,
                    database=self.config.pg_database,
                    user=self.config.pg_user,
                    password=self.config.pg_password
                )
                self._pg_conn.autocommit = True
            except Exception:
                self._backends_available[StorageBackend.POSTGRESQL] = False
                return None
        return self._pg_conn

    def _get_redis(self):
        """Get Redis client."""
        if not self._backends_available[StorageBackend.REDIS]:
            return None
        if self._redis_client is None:
            try:
                self._redis_client = redis.Redis(
                    host=self.config.redis_host,
                    port=self.config.redis_port,
                    password=self.config.redis_password,
                    decode_responses=True,
                    socket_timeout=3
                )
            except Exception:
                self._backends_available[StorageBackend.REDIS] = False
                return None
        return self._redis_client

    def _get_sqlite_fallback_conn(self, db_path: Path) -> sqlite3.Connection:
        """
        Get SQLite FALLBACK connection.

        ⚠️  WARNING: This is the EMERGENCY FALLBACK database!
        Only use this when PostgreSQL is UNAVAILABLE.
        If you're reading this code because SQLite is being used
        while PostgreSQL is up, something is WRONG!
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # =========================================================================
    # Public API: Learnings
    # =========================================================================

    def store_learning(
        self,
        type: str,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        source: str = "manual",
        metadata: Optional[Dict] = None
    ) -> str:
        """
        Store a learning (fix, pattern, win, etc).

        Writes to PostgreSQL if available, falls back to SQLite.
        Invalidates relevant Redis cache.

        Returns the learning ID.
        """
        tags = tags or []
        metadata = metadata or {}
        learning_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()

        # Try PostgreSQL first
        pg_conn = self._get_pg_conn()
        if pg_conn:
            try:
                cursor = pg_conn.cursor()
                cursor.execute("""
                    INSERT INTO learnings (id, type, title, content, tags, session_id, source, metadata, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    learning_id,
                    type,
                    title,
                    content,
                    json.dumps(tags),
                    session_id,
                    source,
                    json.dumps(metadata),
                    timestamp
                ))

                # Record event for pipeline
                cursor.execute("""
                    SELECT record_event('learning_created', 'unified_storage', %s, NULL, NULL, NULL)
                """, (json.dumps({"learning_id": learning_id, "type": type, "title": title}),))

                # Invalidate cache
                redis_client = self._get_redis()
                if redis_client:
                    redis_client.delete("learnings:all", f"learnings:type:{type}")

                return learning_id
            except Exception as e:
                print(f"[UnifiedStorage] PostgreSQL error, falling back to SQLite: {e}")

        # Fallback to SQLite
        conn = self._get_sqlite_fallback_conn(self.config.sqlite_fallback_learnings_path)
        try:
            # Ensure table exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learnings (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    session_id TEXT DEFAULT '',
                    source TEXT DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("""
                INSERT INTO learnings (id, type, title, content, tags, session_id, source, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                learning_id,
                type,
                title,
                content,
                json.dumps(tags),
                session_id or "",
                source,
                timestamp,
                json.dumps(metadata)
            ))
            conn.commit()
        finally:
            conn.close()

        return learning_id

    def query_learnings(
        self,
        search_term: Optional[str] = None,
        type_filter: Optional[str] = None,
        limit: int = 20,
        use_cache: bool = True
    ) -> List[Dict]:
        """
        Query learnings by search term and/or type.

        Checks Redis cache first, then PostgreSQL, then SQLite.
        """
        cache_key = f"learnings:query:{search_term}:{type_filter}:{limit}"

        # Try Redis cache
        if use_cache:
            redis_client = self._get_redis()
            if redis_client:
                try:
                    cached = redis_client.get(cache_key)
                    if cached:
                        return json.loads(cached)
                except Exception as e:
                    print(f"[WARN] Redis cache read failed: {e}")

        results = []

        # Try PostgreSQL
        pg_conn = self._get_pg_conn()
        if pg_conn:
            try:
                cursor = pg_conn.cursor(cursor_factory=RealDictCursor)

                query = "SELECT id, type, title, content, tags, created_at FROM learnings WHERE 1=1"
                params = []

                if search_term:
                    query += " AND (title ILIKE %s OR content ILIKE %s OR tags::text ILIKE %s)"
                    search_pattern = f"%{search_term}%"
                    params.extend([search_pattern, search_pattern, search_pattern])

                if type_filter:
                    query += " AND type = %s"
                    params.append(type_filter)

                query += " ORDER BY created_at DESC LIMIT %s"
                params.append(limit)

                cursor.execute(query, params)
                results = [dict(row) for row in cursor.fetchall()]

                # Cache results
                redis_client = self._get_redis()
                if redis_client and results:
                    redis_client.setex(cache_key, self.config.redis_cache_ttl, json.dumps(results, default=str))

                return results
            except Exception as e:
                print(f"[UnifiedStorage] PostgreSQL query error: {e}")

        # Fallback to SQLite
        conn = self._get_sqlite_fallback_conn(self.config.sqlite_fallback_learnings_path)
        try:
            query = "SELECT id, type, title, content, tags, created_at FROM learnings WHERE 1=1"
            params = []

            if search_term:
                query += " AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)"
                search_pattern = f"%{search_term}%"
                params.extend([search_pattern, search_pattern, search_pattern])

            if type_filter:
                query += " AND type = ?"
                params.append(type_filter)

            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            results = [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

        return results

    def get_learning_by_id(self, learning_id: str) -> Optional[Dict]:
        """Get a specific learning by ID."""
        # Try PostgreSQL
        pg_conn = self._get_pg_conn()
        if pg_conn:
            try:
                cursor = pg_conn.cursor(cursor_factory=RealDictCursor)
                cursor.execute("SELECT * FROM learnings WHERE id = %s", (learning_id,))
                row = cursor.fetchone()
                if row:
                    return dict(row)
            except Exception as e:
                print(f"[WARN] PostgreSQL learning lookup failed: {e}")

        # Fallback to SQLite
        conn = self._get_sqlite_fallback_conn(self.config.sqlite_fallback_learnings_path)
        try:
            cursor = conn.execute("SELECT * FROM learnings WHERE id = ?", (learning_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        finally:
            conn.close()

        return None

    # =========================================================================
    # Public API: Events (Store→Observe→Learn Pipeline)
    # =========================================================================

    def record_event(
        self,
        event_type: str,
        payload: Dict,
        source: str = "webhook",
        session_id: Optional[str] = None,
        project_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Record an event for the Store→Observe→Learn pipeline.

        Events are append-only and processed by Celery workers.
        Only available when PostgreSQL is running.

        Returns event ID if stored, None if PostgreSQL unavailable.
        """
        pg_conn = self._get_pg_conn()
        if not pg_conn:
            # Events require PostgreSQL - no SQLite fallback
            # (Events are for background processing, not critical path)
            return None

        try:
            cursor = pg_conn.cursor()
            cursor.execute("""
                SELECT record_event(%s, %s, %s, %s, %s, NULL)
            """, (
                event_type,
                source,
                json.dumps(payload),
                session_id,
                project_id
            ))
            result = cursor.fetchone()
            event_id = str(result[0]) if result else None

            # Publish to Redis for real-time subscribers
            redis_client = self._get_redis()
            if redis_client and event_id:
                redis_client.publish("cd_events", json.dumps({
                    "id": event_id,
                    "type": event_type,
                    "source": source
                }))

            return event_id
        except Exception as e:
            print(f"[UnifiedStorage] Event recording error: {e}")
            return None

    # =========================================================================
    # Public API: Hierarchy Detection
    # =========================================================================

    def detect_project_hierarchy(self, project_path: str) -> Optional[Dict]:
        """
        Detect the hierarchy pattern of a project.

        Uses learned patterns from cd_hierarchy_patterns table.
        Returns detected pattern info or None if can't determine.
        """
        pg_conn = self._get_pg_conn()
        if not pg_conn:
            return None

        try:
            cursor = pg_conn.cursor(cursor_factory=RealDictCursor)

            # Get all hierarchy patterns
            cursor.execute("SELECT * FROM cd_hierarchy_patterns ORDER BY usage_count DESC")
            patterns = cursor.fetchall()

            # Check for pattern indicators in the project
            detected_pattern = None
            highest_confidence = 0.0

            for pattern in patterns:
                detection_rules = pattern["detection_rules"]
                markers = detection_rules.get("markers", [])
                confidence_threshold = detection_rules.get("confidence_threshold", 0.7)

                # Check how many markers exist in the project
                markers_found = 0
                for marker in markers:
                    marker_path = Path(project_path) / marker
                    if marker_path.exists() or list(Path(project_path).glob(marker)):
                        markers_found += 1

                if markers:
                    confidence = markers_found / len(markers)
                    if confidence >= confidence_threshold and confidence > highest_confidence:
                        highest_confidence = confidence
                        detected_pattern = dict(pattern)
                        detected_pattern["confidence"] = confidence

            return detected_pattern
        except Exception as e:
            print(f"[UnifiedStorage] Hierarchy detection error: {e}")
            return None

    # =========================================================================
    # Public API: Health & Status
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """
        Get storage layer status.

        Returns a status dict showing:
        - Which backends are available
        - Whether we're using PRIMARY (PostgreSQL) or FALLBACK (SQLite)
        - Data counts in each database
        """
        status = {
            "backends": {
                backend.value: available
                for backend, available in self._backends_available.items()
            },
            "primary": None,
            "fallback_active": False,
            "warning": None,
            "counts": {}
        }

        # Determine primary
        if self._backends_available[StorageBackend.POSTGRESQL]:
            status["primary"] = "★★★ postgresql (PRIMARY)"
        else:
            status["primary"] = "★ sqlite_fallback (EMERGENCY MODE!)"
            status["fallback_active"] = True
            status["warning"] = "⚠️  PostgreSQL unavailable - using EMERGENCY FALLBACK SQLite!"

        # Get counts
        pg_conn = self._get_pg_conn()
        if pg_conn:
            try:
                cursor = pg_conn.cursor()
                cursor.execute("SELECT type, COUNT(*) FROM learnings GROUP BY type")
                for row in cursor.fetchall():
                    status["counts"][f"pg_{row[0]}"] = row[1]
            except Exception as e:
                print(f"[WARN] PostgreSQL status count query failed: {e}")

        # SQLite counts
        try:
            conn = self._get_sqlite_fallback_conn(self.config.sqlite_fallback_learnings_path)
            cursor = conn.execute("SELECT type, COUNT(*) FROM learnings GROUP BY type")
            for row in cursor.fetchall():
                status["counts"][f"sqlite_{row[0]}"] = row[1]
            conn.close()
        except Exception as e:
            print(f"[WARN] SQLite status count query failed: {e}")

        return status

    def close(self):
        """Close all connections."""
        if self._pg_conn:
            self._pg_conn.close()
        if self._redis_client:
            self._redis_client.close()


# =============================================================================
# Convenience Functions
# =============================================================================

_storage_instance: Optional[UnifiedStorage] = None


def get_storage() -> UnifiedStorage:
    """Get the global storage instance."""
    global _storage_instance
    if _storage_instance is None:
        _storage_instance = UnifiedStorage()
    return _storage_instance


def store_learning(type: str, title: str, content: str, **kwargs) -> str:
    """Convenience function to store a learning."""
    return get_storage().store_learning(type, title, content, **kwargs)


def query_learnings(search_term: Optional[str] = None, **kwargs) -> List[Dict]:
    """Convenience function to query learnings."""
    return get_storage().query_learnings(search_term, **kwargs)


def record_event(event_type: str, payload: Dict, **kwargs) -> Optional[str]:
    """Convenience function to record an event."""
    return get_storage().record_event(event_type, payload, **kwargs)


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    storage = UnifiedStorage()
    status = storage.get_status()

    print("╔" + "═" * 68 + "╗")
    print("║" + " CONTEXT DNA UNIFIED STORAGE STATUS".center(68) + "║")
    print("╠" + "═" * 68 + "╣")

    if status["fallback_active"]:
        print("║" + " ⚠️  WARNING: RUNNING IN EMERGENCY FALLBACK MODE!".center(68) + "║")
        print("║" + " PostgreSQL is DOWN - using SQLite fallback only".center(68) + "║")
        print("║" + " Start containers: ./scripts/context-dna up".center(68) + "║")
        print("╠" + "═" * 68 + "╣")

    print(f"║  Primary Backend: {status['primary']:<47}║")
    print(f"║  Fallback Active: {str(status['fallback_active']):<47}║")
    print("╠" + "═" * 68 + "╣")
    print("║  Backend Availability:                                              ║")
    for backend, available in status["backends"].items():
        icon = "★★★ PRIMARY" if backend == "postgresql" and available else \
               "★★  CACHE" if backend == "redis" and available else \
               "★   FALLBACK" if "fallback" in backend else \
               "❌  DOWN"
        status_text = f"  {icon}: {backend}"
        print(f"║  {status_text:<64}║")

    if status["counts"]:
        print("╠" + "═" * 68 + "╣")
        print("║  Learning Counts:                                                  ║")
        for key, count in status["counts"].items():
            location = "★★★ PRIMARY" if key.startswith("pg_") else "★ FALLBACK"
            print(f"║    {key}: {count} ({location}){' ' * (45 - len(str(count)) - len(key))}║")

    print("╚" + "═" * 68 + "╝")
