#!/usr/bin/env python3
"""
Context DNA PostgreSQL Storage Adapter

This module provides PostgreSQL-backed storage for the memory system,
replacing SQLite for production use. Connects to the containerized
contextdna-pg service (pgvector/pgvector:pg16).

ARCHITECTURE:
- PostgreSQL: durable source of truth (sessions, messages, tasks, skills, metadata)
- Enables: versioned diffs, time-slicing ("last 4 hours"), auditing
- JSONB for evolving schemas (perfect for "hierarchies that change")
- pgvector for embeddings (semantic search)

CONNECTION (from context-dna/docker-compose.yml):
- Docker: context-dna-postgres (port 5432)
- Database: contextdna (hierarchy/profiles) on same server as context_dna (observability)
- User: context_dna
- Password: context_dna_dev (default)

Tables:
- hierarchies: Project hierarchy metadata
- skills: Extracted skills/SOPs
- events: Event log (append-only)
- snapshots: Periodic state snapshots
- facts: Curated decisions/conventions/constraints

Usage:
    from memory.postgres_storage import (
        store_hierarchy, get_hierarchy,
        store_skill, get_skills,
        store_event, get_recent_events
    )
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from contextlib import contextmanager
from pathlib import Path

# Load Context DNA credentials if not already set
try:
    from dotenv import load_dotenv
    _env_paths = [
        Path(__file__).parent.parent / "context-dna" / "infra" / ".env",
        Path(__file__).parent.parent / ".env",
    ]
    for _env_path in _env_paths:
        if _env_path.exists():
            load_dotenv(_env_path)
            break
except ImportError:
    pass  # dotenv not available, rely on environment

logger = logging.getLogger('contextdna.postgres')

# =============================================================================
# DATABASE CONFIGURATION
# =============================================================================

# LEARNINGS_DB_* takes priority over DATABASE_* to prevent .env hijacking
# (context-dna/infra/.env sets DATABASE_USER=acontext for the upstream DB,
#  but this module needs the context_dna database)
DATABASE_USER = os.environ.get('LEARNINGS_DB_USER', os.environ.get('DATABASE_USER', 'context_dna'))
DATABASE_PASSWORD = os.environ.get('LEARNINGS_DB_PASSWORD', os.environ.get('DATABASE_PASSWORD', 'context_dna_dev'))
DATABASE_HOST = os.environ.get('LEARNINGS_DB_HOST', os.environ.get('DATABASE_HOST', '127.0.0.1'))
DATABASE_PORT = os.environ.get('LEARNINGS_DB_PORT', os.environ.get('DATABASE_PORT', '5432'))
DATABASE_NAME = os.environ.get('LEARNINGS_DB_NAME', os.environ.get('DATABASE_NAME', 'context_dna'))

DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    f"postgresql://{DATABASE_USER}:{DATABASE_PASSWORD}@{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}"
)

# Global connection pool
_connection_pool = None


# =============================================================================
# CONNECTION MANAGEMENT
# =============================================================================

def get_connection():
    """Get a database connection from the pool."""
    global _connection_pool

    try:
        import psycopg2
        from psycopg2 import pool
    except ImportError:
        logger.warning("psycopg2 not available - PostgreSQL storage disabled")
        return None

    try:
        if _connection_pool is None:
            _connection_pool = pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=DATABASE_URL
            )
            logger.info("PostgreSQL connection pool created")

        return _connection_pool.getconn()
    except Exception as e:
        logger.error(f"Failed to get PostgreSQL connection: {e}")
        return None


def release_connection(conn):
    """Return a connection to the pool."""
    global _connection_pool
    if _connection_pool and conn:
        _connection_pool.putconn(conn)


@contextmanager
def get_db_connection():
    """Context manager for database connections."""
    conn = get_connection()
    try:
        yield conn
    finally:
        if conn:
            release_connection(conn)


def init_tables():
    """Initialize database tables if they don't exist."""
    with get_db_connection() as conn:
        if not conn:
            logger.warning("Cannot initialize tables - no database connection")
            return False

        try:
            cur = conn.cursor()

            # Hierarchies table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contextdna_hierarchies (
                    id SERIAL PRIMARY KEY,
                    project_path TEXT UNIQUE NOT NULL,
                    hierarchy_type TEXT,
                    boundaries JSONB DEFAULT '[]'::jsonb,
                    stack_hints JSONB DEFAULT '[]'::jsonb,
                    entrypoints JSONB DEFAULT '[]'::jsonb,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Skills table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contextdna_skills (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT,
                    tags JSONB DEFAULT '[]'::jsonb,
                    confidence FLOAT DEFAULT 0.5,
                    project_id TEXT,
                    scope TEXT,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Events table (append-only log)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contextdna_events (
                    id SERIAL PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    project_id TEXT,
                    scope TEXT,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Create index for time-based queries
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_created_at
                ON contextdna_events(created_at DESC)
            """)

            # Snapshots table (periodic state captures)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contextdna_snapshots (
                    id SERIAL PRIMARY KEY,
                    snapshot_type TEXT NOT NULL,
                    project_id TEXT,
                    data JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Facts table (curated decisions/conventions)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contextdna_facts (
                    id SERIAL PRIMARY KEY,
                    fact_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT,
                    project_id TEXT,
                    scope TEXT,
                    locked BOOLEAN DEFAULT FALSE,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Failures table (for Three-Tier escalation tracking)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contextdna_failures (
                    id SERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    failure_type TEXT,
                    prompt TEXT,
                    error_message TEXT,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Create index for session-based failure queries
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_failures_session
                ON contextdna_failures(session_id, created_at DESC)
            """)

            conn.commit()
            logger.info("PostgreSQL tables initialized successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize tables: {e}")
            conn.rollback()
            return False


# =============================================================================
# HIERARCHY STORAGE
# =============================================================================

def store_hierarchy(project_path: str, hierarchy: Dict) -> bool:
    """Store or update project hierarchy."""
    with get_db_connection() as conn:
        if not conn:
            return False

        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO contextdna_hierarchies
                    (project_path, hierarchy_type, boundaries, stack_hints, entrypoints, metadata, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (project_path) DO UPDATE SET
                    hierarchy_type = EXCLUDED.hierarchy_type,
                    boundaries = EXCLUDED.boundaries,
                    stack_hints = EXCLUDED.stack_hints,
                    entrypoints = EXCLUDED.entrypoints,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
            """, (
                project_path,
                hierarchy.get("type", "unknown"),
                json.dumps(hierarchy.get("boundaries", [])),
                json.dumps(hierarchy.get("stack_hints", [])),
                json.dumps(hierarchy.get("entrypoints", [])),
                json.dumps(hierarchy.get("metadata", {}))
            ))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to store hierarchy: {e}")
            conn.rollback()
            return False


def get_hierarchy(project_path: str) -> Optional[Dict]:
    """Get project hierarchy."""
    with get_db_connection() as conn:
        if not conn:
            return None

        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT hierarchy_type, boundaries, stack_hints, entrypoints, metadata, updated_at
                FROM contextdna_hierarchies
                WHERE project_path = %s
            """, (project_path,))

            row = cur.fetchone()
            if row:
                return {
                    "type": row[0],
                    "boundaries": row[1] or [],
                    "stack_hints": row[2] or [],
                    "entrypoints": row[3] or [],
                    "metadata": row[4] or {},
                    "updated_at": row[5].isoformat() if row[5] else None
                }
            return None
        except Exception as e:
            logger.error(f"Failed to get hierarchy: {e}")
            return None


# =============================================================================
# SKILLS STORAGE
# =============================================================================

def store_skill(skill: Dict) -> bool:
    """Store a new skill/SOP."""
    with get_db_connection() as conn:
        if not conn:
            return False

        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO contextdna_skills
                    (title, content, tags, confidence, project_id, scope, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                skill.get("title", "Untitled"),
                skill.get("content", ""),
                json.dumps(skill.get("tags", [])),
                skill.get("confidence", 0.5),
                skill.get("project_id"),
                skill.get("scope"),
                json.dumps(skill.get("metadata", {}))
            ))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to store skill: {e}")
            conn.rollback()
            return False


def get_skills(project_id: str = None, limit: int = 20) -> List[Dict]:
    """Get skills, optionally filtered by project."""
    with get_db_connection() as conn:
        if not conn:
            return []

        try:
            cur = conn.cursor()
            if project_id:
                cur.execute("""
                    SELECT id, title, content, tags, confidence, project_id, scope, created_at
                    FROM contextdna_skills
                    WHERE project_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (project_id, limit))
            else:
                cur.execute("""
                    SELECT id, title, content, tags, confidence, project_id, scope, created_at
                    FROM contextdna_skills
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (limit,))

            rows = cur.fetchall()
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "content": row[2],
                    "tags": row[3] or [],
                    "confidence": row[4],
                    "project_id": row[5],
                    "scope": row[6],
                    "created_at": row[7].isoformat() if row[7] else None
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Failed to get skills: {e}")
            return []


# =============================================================================
# EVENT STORAGE (Append-only log)
# =============================================================================

def store_event(event_type: str, payload: Dict, project_id: str = None, scope: str = None) -> bool:
    """Store an event in the append-only log."""
    with get_db_connection() as conn:
        if not conn:
            return False

        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO contextdna_events (event_type, project_id, scope, payload)
                VALUES (%s, %s, %s, %s)
            """, (event_type, project_id, scope, json.dumps(payload)))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to store event: {e}")
            conn.rollback()
            return False


def get_recent_events(hours: int = 4, event_type: str = None, limit: int = 100) -> List[Dict]:
    """Get recent events within a time window."""
    with get_db_connection() as conn:
        if not conn:
            return []

        try:
            cur = conn.cursor()
            cutoff = datetime.utcnow() - timedelta(hours=hours)

            if event_type:
                cur.execute("""
                    SELECT id, event_type, project_id, scope, payload, created_at
                    FROM contextdna_events
                    WHERE created_at > %s AND event_type = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (cutoff, event_type, limit))
            else:
                cur.execute("""
                    SELECT id, event_type, project_id, scope, payload, created_at
                    FROM contextdna_events
                    WHERE created_at > %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (cutoff, limit))

            rows = cur.fetchall()
            return [
                {
                    "id": row[0],
                    "event_type": row[1],
                    "project_id": row[2],
                    "scope": row[3],
                    "payload": row[4] or {},
                    "created_at": row[5].isoformat() if row[5] else None
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Failed to get events: {e}")
            return []


# =============================================================================
# FAILURE TRACKING (For Three-Tier Escalation)
# =============================================================================

def record_failure(session_id: str, failure_type: str = "task_failed",
                   prompt: str = None, error_message: str = None) -> bool:
    """Record a failure for Three-Tier escalation tracking."""
    with get_db_connection() as conn:
        if not conn:
            return False

        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO contextdna_failures
                    (session_id, failure_type, prompt, error_message)
                VALUES (%s, %s, %s, %s)
            """, (session_id, failure_type, prompt, error_message))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to record failure: {e}")
            conn.rollback()
            return False


def get_failure_count(session_id: str, hours: int = 1) -> int:
    """Get failure count for a session within a time window."""
    with get_db_connection() as conn:
        if not conn:
            return 0

        try:
            cur = conn.cursor()
            cutoff = datetime.utcnow() - timedelta(hours=hours)
            cur.execute("""
                SELECT COUNT(*)
                FROM contextdna_failures
                WHERE session_id = %s AND created_at > %s
            """, (session_id, cutoff))
            return cur.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to get failure count: {e}")
            return 0


def clear_failures(session_id: str) -> bool:
    """Clear failures for a session (after success)."""
    with get_db_connection() as conn:
        if not conn:
            return False

        try:
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM contextdna_failures
                WHERE session_id = %s
            """, (session_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to clear failures: {e}")
            conn.rollback()
            return False


# =============================================================================
# SNAPSHOT STORAGE
# =============================================================================

def store_snapshot(snapshot_type: str, data: Dict, project_id: str = None) -> bool:
    """Store a state snapshot."""
    with get_db_connection() as conn:
        if not conn:
            return False

        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO contextdna_snapshots (snapshot_type, project_id, data)
                VALUES (%s, %s, %s)
            """, (snapshot_type, project_id, json.dumps(data)))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to store snapshot: {e}")
            conn.rollback()
            return False


def get_latest_snapshot(snapshot_type: str, project_id: str = None) -> Optional[Dict]:
    """Get the most recent snapshot of a given type."""
    with get_db_connection() as conn:
        if not conn:
            return None

        try:
            cur = conn.cursor()
            if project_id:
                cur.execute("""
                    SELECT data, created_at
                    FROM contextdna_snapshots
                    WHERE snapshot_type = %s AND project_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (snapshot_type, project_id))
            else:
                cur.execute("""
                    SELECT data, created_at
                    FROM contextdna_snapshots
                    WHERE snapshot_type = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (snapshot_type,))

            row = cur.fetchone()
            if row:
                return {
                    "data": row[0] or {},
                    "created_at": row[1].isoformat() if row[1] else None
                }
            return None
        except Exception as e:
            logger.error(f"Failed to get snapshot: {e}")
            return None


def diff_snapshots(snapshot_type: str, project_id: str = None, hours: int = 4) -> Dict:
    """Compare current state with snapshot from N hours ago."""
    with get_db_connection() as conn:
        if not conn:
            return {"error": "No database connection"}

        try:
            cur = conn.cursor()
            cutoff = datetime.utcnow() - timedelta(hours=hours)

            # Get latest snapshot
            cur.execute("""
                SELECT data, created_at
                FROM contextdna_snapshots
                WHERE snapshot_type = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (snapshot_type,))
            latest = cur.fetchone()

            # Get snapshot from N hours ago
            cur.execute("""
                SELECT data, created_at
                FROM contextdna_snapshots
                WHERE snapshot_type = %s AND created_at <= %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (snapshot_type, cutoff))
            older = cur.fetchone()

            if not latest:
                return {"error": "No snapshots found"}

            if not older:
                return {
                    "current": latest[0],
                    "previous": None,
                    "diff": "No previous snapshot for comparison"
                }

            # Simple diff (in production, use a proper diff library)
            return {
                "current": latest[0],
                "current_at": latest[1].isoformat() if latest[1] else None,
                "previous": older[0],
                "previous_at": older[1].isoformat() if older[1] else None,
                "hours_apart": hours
            }

        except Exception as e:
            logger.error(f"Failed to diff snapshots: {e}")
            return {"error": str(e)}


# =============================================================================
# INITIALIZATION
# =============================================================================

# Try to initialize tables on import
try:
    init_tables()
except Exception as e:
    logger.warning(f"Could not initialize PostgreSQL tables on import: {e}")
