#!/usr/bin/env python3
"""
SQLite Storage Backend - Single Source of Truth for Context DNA
================================================================

This module provides SQLite-based storage with FTS5 full-text search.
Used by all Context DNA services:
- Helper Agent (port 8080)
- Python API (port 3456)
- xbar plugin
- Dashboard

Location Strategy (matching install wizard pattern):
- PRIMARY: ~/.context-dna/learnings.db (user data, persists across repo updates)
- FALLBACK: memory/.context-dna.db (repo-local, for development)

Features:
- FTS5 for fast keyword search
- WAL mode for concurrent access
- Smart deduplication (ported from JSON store)
- No record limit (unlike JSON's MAX_HISTORY=100)
- Proper indexes for fast queries
- Device token support for user identification
"""

import json
import os
import sqlite3
import threading
import uuid
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# User data directory (matches install wizard)
USER_DATA_DIR = Path(os.environ.get("CONTEXT_DNA_DIR", Path.home() / ".context-dna"))
MEMORY_DIR = Path(__file__).parent

# Database location - SINGLE SOURCE OF TRUTH
# Priority: Unified DB > User data dir > Repo-local
def get_db_path() -> Path:
    """Get the database path, preferring unified then user data directory."""
    from memory.db_utils import get_unified_db_path
    user_db = USER_DATA_DIR / "learnings.db"
    unified = get_unified_db_path(user_db)
    if unified != user_db:
        return unified  # Using unified DB

    repo_db = MEMORY_DIR / ".context-dna.db"

    # Prefer user data directory if it exists or can be created
    if user_db.exists():
        return user_db
    if USER_DATA_DIR.exists():
        return user_db  # Will be created

    # Fallback to repo-local
    return repo_db


def _tbl(name: str) -> str:
    """Resolve table name with unified prefix if applicable."""
    from memory.db_utils import unified_table
    return unified_table("learnings.db", name)


DB_PATH = get_db_path()


class SQLiteStorage:
    """
    SQLite storage backend with FTS5 search and smart deduplication.

    This replaces the JSON-based LearningStore for better scalability.

    Storage Location:
    - Primary: ~/.context-dna/learnings.db (user data, persists across updates)
    - Fallback: memory/.context-dna.db (repo-local)
    """

    SCHEMA_VERSION = 3  # Bumped for workspace_id column

    def __init__(self, db_path: Path = None):
        """Initialize SQLite storage.

        Args:
            db_path: Path to database file. Defaults to ~/.context-dna/learnings.db
        """
        self.db_path = db_path or get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._device_id = self._get_or_create_device_id()

        from memory.db_utils import connect_wal
        self.conn = connect_wal(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode
        )
        self.conn.execute("PRAGMA cache_size=10000")  # 10MB cache

        self._init_schema()

    def _init_schema(self) -> None:
        """Initialize database schema with FTS5."""
        t_learnings = _tbl("learnings")
        t_fts = _tbl("learnings_fts")
        t_stats = _tbl("stats")
        t_device = _tbl("device_config")
        t_negpat = _tbl("negative_patterns")

        # Main learnings table
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {t_learnings} (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                session_id TEXT DEFAULT '',
                injection_id TEXT DEFAULT '',
                source TEXT DEFAULT 'manual',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{{}}',
                merge_count INTEGER DEFAULT 1,
                workspace_id TEXT NOT NULL DEFAULT ''
            )
        """)

        # FTS5 virtual table for fast full-text search
        self.conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {t_fts} USING fts5(
                title,
                content,
                tags,
                content={t_learnings},
                content_rowid=rowid
            )
        """)

        # Triggers to keep FTS in sync
        trig_ai = _tbl("learnings") + "_ai"
        trig_ad = _tbl("learnings") + "_ad"
        trig_au = _tbl("learnings") + "_au"
        self.conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {trig_ai} AFTER INSERT ON {t_learnings} BEGIN
                INSERT INTO {t_fts}(rowid, title, content, tags)
                VALUES (new.rowid, new.title, new.content, new.tags);
            END
        """)

        self.conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {trig_ad} AFTER DELETE ON {t_learnings} BEGIN
                INSERT INTO {t_fts}({t_fts}, rowid, title, content, tags)
                VALUES ('delete', old.rowid, old.title, old.content, old.tags);
            END
        """)

        self.conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {trig_au} AFTER UPDATE ON {t_learnings} BEGIN
                INSERT INTO {t_fts}({t_fts}, rowid, title, content, tags)
                VALUES ('delete', old.rowid, old.title, old.content, old.tags);
                INSERT INTO {t_fts}(rowid, title, content, tags)
                VALUES (new.rowid, new.title, new.content, new.tags);
            END
        """)

        # Indexes for fast queries
        self.conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{t_learnings}_type ON {t_learnings}(type)
        """)
        self.conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{t_learnings}_created ON {t_learnings}(created_at DESC)
        """)
        self.conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{t_learnings}_session ON {t_learnings}(session_id)
        """)
        # Migration: add workspace_id if missing (schema v2 -> v3)
        try:
            self.conn.execute(f"SELECT workspace_id FROM {t_learnings} LIMIT 1")
        except sqlite3.OperationalError:
            self.conn.execute(
                f"ALTER TABLE {t_learnings} ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''"
            )
        # Index must come AFTER migration ensures column exists
        self.conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{t_learnings}_workspace ON {t_learnings}(workspace_id)
        """)

        # Stats table
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {t_stats} (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Schema version
        self.conn.execute(f"""
            INSERT OR IGNORE INTO {t_stats} (key, value) VALUES ('schema_version', ?)
        """, (str(self.SCHEMA_VERSION),))

        # Device token table for user identification
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {t_device} (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Negative patterns table - tracks anti-patterns for SOP quality evolution
        # Only promoted to SOP content after 3+ occurrences (Aaron's rule)
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {t_negpat} (
                pattern_key TEXT PRIMARY KEY,
                description TEXT,
                goal TEXT DEFAULT '',
                first_seen TEXT,
                last_seen TEXT,
                frequency INTEGER DEFAULT 1,
                promoted_to_sop INTEGER DEFAULT 0,
                context_samples TEXT DEFAULT '[]'
            )
        """)

        self.conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{t_negpat}_goal
            ON {t_negpat}(goal)
        """)
        self.conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{t_negpat}_freq
            ON {t_negpat}(frequency DESC)
        """)

    def _get_or_create_device_id(self) -> str:
        """
        Get or create a device ID for this machine.

        The device ID is used to:
        1. Identify this machine's learnings
        2. Link to Django backend user (via device token registration)
        3. Support multi-device sync in the future

        Storage: ~/.context-dna/device.json (outside SQLite for portability)
        """
        device_file = USER_DATA_DIR / "device.json"

        # Try to load existing device ID
        if device_file.exists():
            try:
                with open(device_file, 'r') as f:
                    data = json.load(f)
                    if 'device_id' in data:
                        return data['device_id']
            except (json.JSONDecodeError, IOError) as e:
                print(f"[WARN] Device ID file read failed: {e}")

        # Generate new device ID
        device_id = str(uuid.uuid4())

        # Save to file
        device_file.parent.mkdir(parents=True, exist_ok=True)
        device_data = {
            'device_id': device_id,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'platform': self._detect_platform(),
            'user_email': os.environ.get('CONTEXT_DNA_USER_EMAIL', ''),
            'linked_to_backend': False,
        }

        with open(device_file, 'w') as f:
            json.dump(device_data, f, indent=2)

        return device_id

    def _detect_platform(self) -> str:
        """Detect the current platform."""
        import platform
        system = platform.system().lower()
        if system == 'darwin':
            return 'macos'
        return system

    @property
    def device_id(self) -> str:
        """Get the device ID for this machine."""
        return self._device_id

    def get_device_info(self) -> Dict[str, Any]:
        """Get device information including registration status."""
        device_file = USER_DATA_DIR / "device.json"
        if device_file.exists():
            try:
                with open(device_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"[WARN] Device info file read failed: {e}")
        return {
            'device_id': self._device_id,
            'linked_to_backend': False,
        }

    def link_to_backend(self, user_email: str, device_token: str) -> bool:
        """
        Link this device to the Django backend.

        Called after successful JWT authentication to persist the device token.

        Args:
            user_email: The authenticated user's email
            device_token: The device token from Django backend

        Returns:
            True if link was successful
        """
        device_file = USER_DATA_DIR / "device.json"
        try:
            data = self.get_device_info()
            data['user_email'] = user_email
            data['device_token'] = device_token
            data['linked_to_backend'] = True
            data['linked_at'] = datetime.now(timezone.utc).isoformat()

            with open(device_file, 'w') as f:
                json.dump(data, f, indent=2)
            return True
        except IOError:
            return False

    def _find_duplicate(self, new_learning: Dict[str, Any],
                        time_window_hours: int = 24,
                        workspace_id: str = '') -> Optional[sqlite3.Row]:
        """
        Find duplicate learning within time window.

        Uses same logic as JSON store:
        1. 80%+ word overlap in title
        2. Exact core title match (after stripping prefixes)
        """
        new_title = (new_learning.get('title') or '').lower().strip()
        if not new_title:
            return None

        # Get core title (remove SOP tags)
        def get_core_title(title: str) -> str:
            core = title.lower().strip()
            prefixes = [
                r'^\[process sop\]\s*',
                r'^\[bug-fix sop\]\s*',
                r'^agent success:\s*',
                r'^fix:\s*',
            ]
            for pattern in prefixes:
                core = re.sub(pattern, '', core)
            return core.strip()

        new_core = get_core_title(new_title)
        new_words = set(new_title.split())

        # Get recent learnings (workspace-scoped to preserve isolation)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=time_window_hours)).isoformat()
        t = _tbl("learnings")
        rows = self.conn.execute(f"""
            SELECT * FROM {t} WHERE created_at >= ? AND workspace_id = ? ORDER BY created_at DESC
        """, (cutoff, workspace_id)).fetchall()

        for row in rows:
            existing_title = (row['title'] or '').lower().strip()
            existing_words = set(existing_title.split())

            # Check 80% word overlap
            if new_words and existing_words:
                overlap = len(new_words & existing_words)
                ratio = overlap / max(len(new_words), len(existing_words))
                if ratio >= 0.8:
                    return row

            # Check exact core title match
            existing_core = get_core_title(existing_title)
            if new_core and existing_core and new_core == existing_core:
                return row

        return None

    def _smart_merge(self, existing: sqlite3.Row, new: Dict[str, Any]) -> Dict[str, Any]:
        """Smart merge two learnings - keep best of both."""
        merged = dict(existing)

        # Keep longer title
        new_title = new.get('title') or ''
        if len(new_title) > len(existing['title'] or ''):
            merged['title'] = new_title

        # Smart content merge
        new_content = new.get('content') or ''
        existing_content = existing['content'] or ''

        if len(new_content) > len(existing_content):
            merged['content'] = new_content

        # Merge tags
        existing_tags = json.loads(existing['tags'] or '[]')
        new_tags = new.get('tags') or []
        merged['tags'] = json.dumps(list(set(existing_tags) | set(new_tags)))

        # Update merge count
        merged['merge_count'] = (existing['merge_count'] or 1) + 1
        merged['updated_at'] = datetime.now(timezone.utc).isoformat()

        return merged

    def store_learning(self, learning_data: Dict[str, Any],
                       skip_dedup: bool = False,
                       consolidate: bool = True,
                       workspace_id: str = '') -> Dict[str, Any]:
        """
        Store a learning with smart deduplication.

        Args:
            learning_data: The learning to store
            skip_dedup: Skip duplicate checking
            consolidate: Merge with existing duplicate
            workspace_id: Workspace scope for isolation (default: '')

        Returns:
            The stored/merged learning with status flags
        """
        t = _tbl("learnings")
        # Check for duplicate
        if not skip_dedup:
            duplicate = self._find_duplicate(learning_data, workspace_id=workspace_id)
            if duplicate:
                MAX_MERGE = 3
                if consolidate and (duplicate['merge_count'] or 1) < MAX_MERGE:
                    # Merge with existing
                    merged = self._smart_merge(duplicate, learning_data)
                    self.conn.execute(f"""
                        UPDATE {t} SET
                            title = ?, content = ?, tags = ?,
                            merge_count = ?, updated_at = ?
                        WHERE id = ?
                    """, (
                        merged['title'], merged['content'], merged['tags'],
                        merged['merge_count'], merged['updated_at'], duplicate['id']
                    ))
                    result = self._row_to_dict(self.get_by_id(duplicate['id']))
                    result['_consolidated'] = True
                    return result
                else:
                    result = self._row_to_dict(duplicate)
                    result['_duplicate'] = True
                    return result

        # Create new learning
        learning_id = learning_data.get('id') or f"learn_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
        now = datetime.now(timezone.utc).isoformat()

        self.conn.execute(f"""
            INSERT INTO {t}
            (id, type, title, content, tags, session_id, injection_id, source,
             created_at, updated_at, metadata, merge_count, workspace_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (
            learning_id,
            learning_data.get('type', 'win'),
            learning_data.get('title', ''),
            learning_data.get('content', ''),
            json.dumps(learning_data.get('tags', [])),
            learning_data.get('session_id', ''),
            learning_data.get('injection_id', ''),
            learning_data.get('source', 'manual'),
            learning_data.get('timestamp') or now,
            now,
            json.dumps(learning_data.get('metadata', {})),
            workspace_id,
        ))

        # Return stored learning
        stored = self.get_by_id(learning_id)
        return self._row_to_dict(stored) if stored else learning_data

    def get_by_id(self, learning_id: str) -> Optional[sqlite3.Row]:
        """Get learning by ID."""
        t = _tbl("learnings")
        return self.conn.execute(
            f"SELECT * FROM {t} WHERE id = ?", (learning_id,)
        ).fetchone()

    def get_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent learnings."""
        t = _tbl("learnings")
        rows = self.conn.execute(f"""
            SELECT * FROM {t} ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_by_session(self, session_id: str) -> List[Dict[str, Any]]:
        """Get learnings for a session."""
        t = _tbl("learnings")
        rows = self.conn.execute(f"""
            SELECT * FROM {t} WHERE session_id = ? ORDER BY created_at DESC
        """, (session_id,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_since(self, timestamp: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get learnings since a timestamp."""
        t = _tbl("learnings")
        rows = self.conn.execute(f"""
            SELECT * FROM {t} WHERE created_at >= ?
            ORDER BY created_at DESC LIMIT ?
        """, (timestamp, limit)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_by_type(self, learning_type: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get learnings by type."""
        t = _tbl("learnings")
        rows = self.conn.execute(f"""
            SELECT * FROM {t} WHERE type = ?
            ORDER BY created_at DESC LIMIT ?
        """, (learning_type, limit)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def query(self, search: str, limit: int = 10, _skip_hybrid: bool = False,
              workspace_id: str = None) -> List[Dict[str, Any]]:
        """Full-text search with OR matching and keyword fallback.

        Uses FTS5 OR matching so any matching word returns results,
        then falls back to keyword scoring if FTS yields nothing.
        If semantic embeddings are available, upgrades to hybrid search automatically.

        Args:
            search: Search query string
            limit: Maximum results to return
            _skip_hybrid: Skip hybrid/semantic search (internal)
            workspace_id: If provided, only return learnings from this workspace
        """
        # Try hybrid search if semantic embeddings are available
        # Skip hybrid when workspace_id is set -- hybrid_query doesn't support workspace filtering yet
        if not _skip_hybrid and workspace_id is None:
            try:
                from memory.semantic_search import is_index_available
                if is_index_available():
                    return self.hybrid_query(search, limit=limit)
            except (ImportError, Exception):
                pass  # Fall through to FTS5-only

        # Build workspace filter clause
        ws_clause = ""
        ws_params = []
        if workspace_id is not None:
            ws_clause = " AND l.workspace_id = ?"
            ws_params = [workspace_id]

        stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be',
                      'been', 'being', 'have', 'has', 'had', 'do', 'does',
                      'did', 'will', 'would', 'could', 'should', 'may',
                      'might', 'can', 'shall', 'to', 'of', 'in', 'for',
                      'on', 'with', 'at', 'by', 'from', 'it', 'its',
                      'this', 'that', 'and', 'or', 'but', 'not', 'so'}
        words = [w for w in search.lower().split()
                 if w not in stop_words and len(w) > 1]
        if not words:
            words = search.lower().split()

        t = _tbl("learnings")
        t_fts = _tbl("learnings_fts")

        # Strategy 1: FTS5 OR matching
        if words:
            fts_terms = ' OR '.join(
                w.replace('"', '""') for w in words[:8]
            )
            try:
                rows = self.conn.execute(f"""
                    SELECT l.* FROM {t} l
                    JOIN {t_fts} fts ON l.rowid = fts.rowid
                    WHERE {t_fts} MATCH ?
                    AND COALESCE(l.type, '') != 'prompt_leak'
                    {ws_clause}
                    ORDER BY rank LIMIT ?
                """, (fts_terms, *ws_params, limit)).fetchall()
                if rows:
                    return [self._row_to_dict(r) for r in rows]
            except Exception:
                pass

        # Strategy 2: keyword scoring fallback
        ws_clause_bare = ""
        if workspace_id is not None:
            ws_clause_bare = " AND workspace_id = ?"

        query_words = set(words) if words else set(search.lower().split())
        all_rows = self.conn.execute(
            f"SELECT * FROM {t} WHERE COALESCE(type, '') != 'prompt_leak'{ws_clause_bare} ORDER BY created_at DESC LIMIT 200",
            (*ws_params,)
        ).fetchall()
        scored = []
        for row in all_rows:
            title = (row['title'] or '').lower()
            content = (row['content'] or '').lower()
            tags_str = (row['tags'] or '[]').lower()
            text = f"{title} {content} {tags_str}"
            text_words = set(text.split())
            if query_words and text_words:
                overlap = len(query_words & text_words)
                if overlap > 0:
                    ratio = overlap / len(query_words)
                    boost = 1.5 if any(w in title for w in query_words) else 1.0
                    scored.append((ratio * boost, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._row_to_dict(r) for _, r in scored[:limit]]

    def hybrid_query(self, query_text: str, limit: int = 10,
                     semantic_weight: float = 0.6,
                     keyword_weight: float = 0.4) -> list:
        """Hybrid search combining FTS5 keyword scores with semantic vector similarity.

        Inspired by vector_store.py hybrid_search() but using SQLite + local embeddings.
        Falls back to FTS5-only if semantic search unavailable.
        """
        # Step 1: Get FTS5 results with scores (_skip_hybrid prevents recursion)
        fts5_results = self.query(query_text, limit=limit * 2, _skip_hybrid=True)
        fts5_scored = {}
        for i, r in enumerate(fts5_results):
            rid = r.get('id', r.get('title', str(i)))
            fts5_scored[rid] = {
                'result': r,
                'fts5_rank': 1.0 - (i / max(len(fts5_results), 1)),
            }

        # Step 2: Get semantic results
        semantic_scored = {}
        try:
            from memory.semantic_search import semantic_search as sem_search
            sem_results = sem_search(query_text, top_k=limit * 2)
            for r in (sem_results or []):
                rid = r.get('id', r.get('title', ''))
                score = r.get('score', 0.5)
                semantic_scored[rid] = {
                    'result': r,
                    'sem_score': score,
                }
        except Exception:
            pass  # Semantic unavailable — FTS5-only mode

        # Step 3: Merge with weighted scoring
        all_ids = set(list(fts5_scored.keys()) + list(semantic_scored.keys()))
        merged = []
        for rid in all_ids:
            fts5_data = fts5_scored.get(rid, {})
            sem_data = semantic_scored.get(rid, {})

            fts5_score = fts5_data.get('fts5_rank', 0.0)
            sem_score = sem_data.get('sem_score', 0.0)

            combined_score = (keyword_weight * fts5_score) + (semantic_weight * sem_score)

            # Use whichever result dict we have (prefer FTS5 as it has more metadata)
            result = fts5_data.get('result') or sem_data.get('result', {})
            result['_hybrid_score'] = combined_score
            result['_search_method'] = 'hybrid'
            merged.append((combined_score, result))

        # Step 4: Sort by combined score, return top results
        merged.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in merged[:limit]]

    def get_stats(self) -> Dict[str, Any]:
        """Get storage statistics."""
        t = _tbl("learnings")
        total = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

        # Count by type
        by_type = {}
        for lt in ['win', 'fix', 'pattern', 'insight', 'gotcha', 'sop']:
            count = self.conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE type = ?", (lt,)
            ).fetchone()[0]
            if count > 0:
                by_type[lt] = count

        # Today's count
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        today_count = self.conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE created_at >= ?", (today,)
        ).fetchone()[0]

        # Last capture
        last = self.conn.execute(
            f"SELECT created_at FROM {t} ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        last_capture = last[0] if last else None

        # Calculate streak
        streak = self._calculate_streak()

        return {
            "total": total,
            "wins": by_type.get("win", 0),
            "fixes": by_type.get("fix", 0),
            "patterns": by_type.get("pattern", 0),
            "by_type": by_type,
            "today": today_count,
            "last_capture": last_capture,
            "streak": streak,
            "db_size_kb": round(self.db_path.stat().st_size / 1024, 2)
                if self.db_path.exists() else 0,
        }

    def _calculate_streak(self) -> int:
        """Calculate consecutive days with learnings."""
        t = _tbl("learnings")
        rows = self.conn.execute(f"""
            SELECT DISTINCT date(created_at) as day FROM {t}
            ORDER BY day DESC LIMIT 30
        """).fetchall()

        if not rows:
            return 0

        today = datetime.now(timezone.utc).date()
        streak = 0

        for row in rows:
            try:
                day = datetime.fromisoformat(row[0]).date()
            except (ValueError, TypeError):
                continue

            expected = today - timedelta(days=streak)
            if day == expected:
                streak += 1
            else:
                break

        return streak

    def health_check(self) -> bool:
        """Check if storage is healthy."""
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except (sqlite3.Error, AttributeError):
            return False

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert row to dictionary matching JSON format."""
        if not row:
            return {}
        return {
            'id': row['id'],
            'type': row['type'],
            'title': row['title'],
            'content': row['content'],
            'tags': json.loads(row['tags']) if row['tags'] and row['tags'].strip() else [],
            'session_id': row['session_id'],
            'injection_id': row['injection_id'],
            'source': row['source'],
            'timestamp': row['created_at'],
            'metadata': json.loads(row['metadata']) if row['metadata'] and row['metadata'].strip() else {},
            '_merge_count': row['merge_count'],
            'workspace_id': row['workspace_id'],
        }

    # ==========================================================================
    # NEGATIVE PATTERN TRACKING — SOP Quality Evolution
    # ==========================================================================

    def record_negative_pattern(self, pattern_key: str, context: str,
                                goal: str = "", description: str = "") -> int:
        """
        Record a negative outcome pattern. Only promotes to SOP after 3+ occurrences.

        Args:
            pattern_key: Normalized key (e.g., "docker restart doesn't reload env")
            context: Surrounding context snippet (max 200 chars stored)
            goal: Related SOP goal if known (e.g., "Deploy Django to production")
            description: Human-readable description of the anti-pattern

        Returns:
            Current frequency count for this pattern
        """
        now = datetime.now(timezone.utc).isoformat()
        context_snippet = (context or "")[:200]
        t = _tbl("negative_patterns")

        existing = self.conn.execute(
            f"SELECT frequency, context_samples FROM {t} WHERE pattern_key = ?",
            (pattern_key,)
        ).fetchone()

        if existing:
            freq = existing[0] + 1
            # Keep first 3 context samples (evidence for SOP promotion)
            try:
                samples = json.loads(existing[1] or "[]")
            except (json.JSONDecodeError, TypeError):
                samples = []
            if len(samples) < 3 and context_snippet:
                samples.append(context_snippet)

            self.conn.execute(f"""
                UPDATE {t}
                SET frequency = ?, last_seen = ?, context_samples = ?,
                    goal = CASE WHEN goal = '' AND ? != '' THEN ? ELSE goal END,
                    description = CASE WHEN description IS NULL OR description = '' THEN ? ELSE description END
                WHERE pattern_key = ?
            """, (freq, now, json.dumps(samples), goal, goal, description or pattern_key, pattern_key))
            return freq
        else:
            samples = [context_snippet] if context_snippet else []
            self.conn.execute(f"""
                INSERT INTO {t}
                (pattern_key, description, goal, first_seen, last_seen, frequency, context_samples)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            """, (pattern_key, description or pattern_key, goal, now, now, json.dumps(samples)))
            return 1

    def get_frequent_negative_patterns(self, min_frequency: int = 3,
                                       goal: str = None) -> List[Dict[str, Any]]:
        """
        Get negative patterns that have occurred min_frequency+ times.

        These are ready for SOP inclusion below the divider line.

        Args:
            min_frequency: Minimum occurrences required (default 3)
            goal: Optional filter by related SOP goal

        Returns:
            List of pattern dicts with pattern_key, description, frequency, etc.
        """
        t = _tbl("negative_patterns")
        if goal:
            rows = self.conn.execute(f"""
                SELECT * FROM {t}
                WHERE frequency >= ? AND goal = ?
                ORDER BY frequency DESC
            """, (min_frequency, goal)).fetchall()
        else:
            rows = self.conn.execute(f"""
                SELECT * FROM {t}
                WHERE frequency >= ?
                ORDER BY frequency DESC
            """, (min_frequency,)).fetchall()

        results = []
        for row in rows:
            try:
                samples = json.loads(row["context_samples"] or "[]")
            except (json.JSONDecodeError, TypeError):
                samples = []
            results.append({
                "pattern_key": row["pattern_key"],
                "description": row["description"],
                "goal": row["goal"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "frequency": row["frequency"],
                "promoted_to_sop": bool(row["promoted_to_sop"]),
                "context_samples": samples,
            })
        return results

    def mark_pattern_promoted(self, pattern_key: str) -> None:
        """Mark a negative pattern as promoted to SOP content."""
        t = _tbl("negative_patterns")
        self.conn.execute(
            f"UPDATE {t} SET promoted_to_sop = 1 WHERE pattern_key = ?",
            (pattern_key,)
        )

    def close(self):
        """Close database connection."""
        self.conn.close()


# Singleton instance
_sqlite_storage: Optional[SQLiteStorage] = None
_sqlite_storage_lock = threading.Lock()


def get_sqlite_storage() -> SQLiteStorage:
    """Get singleton SQLite storage instance (thread-safe)."""
    global _sqlite_storage
    if _sqlite_storage is None:
        with _sqlite_storage_lock:
            if _sqlite_storage is None:
                _sqlite_storage = SQLiteStorage()
                try:
                    from memory.evented_write import get_evented_write_service
                    get_evented_write_service().gate(_sqlite_storage, "sqlite_storage")
                except Exception:
                    pass  # Fail-open — store works without event logging
    return _sqlite_storage


def migrate_from_json(json_path: Path = None) -> int:
    """
    Migrate learnings from JSON file to SQLite.

    Args:
        json_path: Path to JSON file. Defaults to .learning_history.json

    Returns:
        Number of learnings migrated
    """
    json_path = json_path or (MEMORY_DIR / ".learning_history.json")

    if not json_path.exists():
        print(f"No JSON file found at {json_path}")
        return 0

    with open(json_path, 'r') as f:
        learnings = json.load(f)

    print(f"Found {len(learnings)} learnings in JSON file")

    storage = get_sqlite_storage()
    migrated = 0
    skipped = 0

    # Reverse to maintain chronological order (JSON has newest first)
    for learning in reversed(learnings):
        result = storage.store_learning(learning, skip_dedup=True)
        if result.get('_duplicate'):
            skipped += 1
        else:
            migrated += 1

    print(f"Migrated: {migrated}, Skipped (duplicates): {skipped}")
    return migrated


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        # Run migration
        count = migrate_from_json()
        print(f"\nMigration complete. {count} learnings now in SQLite.")

        # Show stats
        storage = get_sqlite_storage()
        stats = storage.get_stats()
        print(f"\nSQLite Stats:")
        print(f"  Total: {stats['total']}")
        print(f"  Wins: {stats['wins']}")
        print(f"  Fixes: {stats['fixes']}")
        print(f"  Today: {stats['today']}")
        print(f"  Streak: {stats['streak']} days")
    else:
        # Test basic operations
        storage = get_sqlite_storage()
        print(f"Database: {storage.db_path}")
        print(f"Health: {storage.health_check()}")
        print(f"Stats: {storage.get_stats()}")
