"""SQLite storage backend for Context DNA.

Zero external dependencies. Uses FTS5 for fast full-text search.
Designed for solo developers and small-medium projects.

Performance targets:
- Query: <50ms for 10K learnings
- Insert: <10ms
- Startup: <100ms
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from context_dna.storage.backend import Learning, LearningType, StorageBackend


class SQLiteBackend(StorageBackend):
    """SQLite-based storage with FTS5 full-text search.

    Features:
    - Zero dependencies (stdlib only)
    - FTS5 for fast keyword search
    - JSON metadata support
    - Automatic schema migration
    - Thread-safe with WAL mode
    """

    SCHEMA_VERSION = 1

    def __init__(self, db_path: str = ".context-dna/learnings.db"):
        """Initialize SQLite backend.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,  # Allow multi-threaded access
            isolation_level=None,  # Autocommit mode
        )
        self.conn.row_factory = sqlite3.Row

        # Enable WAL mode for better concurrent performance
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=10000")  # 10MB cache

        self._init_schema()

    def _init_schema(self) -> None:
        """Initialize database schema with FTS5."""
        # Main learnings table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS learnings (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)

        # FTS5 virtual table for fast full-text search
        self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS learnings_fts USING fts5(
                title,
                content,
                tags,
                content=learnings,
                content_rowid=rowid
            )
        """)

        # Triggers to keep FTS in sync
        self.conn.execute("""
            CREATE TRIGGER IF NOT EXISTS learnings_ai AFTER INSERT ON learnings BEGIN
                INSERT INTO learnings_fts(rowid, title, content, tags)
                VALUES (new.rowid, new.title, new.content, new.tags);
            END
        """)

        self.conn.execute("""
            CREATE TRIGGER IF NOT EXISTS learnings_ad AFTER DELETE ON learnings BEGIN
                INSERT INTO learnings_fts(learnings_fts, rowid, title, content, tags)
                VALUES ('delete', old.rowid, old.title, old.content, old.tags);
            END
        """)

        self.conn.execute("""
            CREATE TRIGGER IF NOT EXISTS learnings_au AFTER UPDATE ON learnings BEGIN
                INSERT INTO learnings_fts(learnings_fts, rowid, title, content, tags)
                VALUES ('delete', old.rowid, old.title, old.content, old.tags);
                INSERT INTO learnings_fts(rowid, title, content, tags)
                VALUES (new.rowid, new.title, new.content, new.tags);
            END
        """)

        # Index for type queries
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_learnings_type ON learnings(type)
        """)

        # Index for time-based queries
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_learnings_created ON learnings(created_at DESC)
        """)

        # Stats table for fast counts
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Schema version tracking
        self.conn.execute("""
            INSERT OR IGNORE INTO stats (key, value) VALUES ('schema_version', ?)
        """, (str(self.SCHEMA_VERSION),))

    def record(self, learning: Learning) -> str:
        """Record a learning and return its ID."""
        if not learning.id:
            learning.id = str(uuid.uuid4())[:8]  # Short IDs for readability

        self.conn.execute("""
            INSERT OR REPLACE INTO learnings
            (id, type, title, content, tags, created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            learning.id,
            learning.type.value,
            learning.title,
            learning.content,
            json.dumps(learning.tags),
            learning.created_at.isoformat(),
            learning.updated_at.isoformat(),
            json.dumps(learning.metadata),
        ))

        # Update stats
        self._increment_stat(f"count_{learning.type.value}")
        self._increment_stat("count_total")

        return learning.id

    def query(self, search: str, limit: int = 10,
              learning_type: Optional[LearningType] = None) -> List[Learning]:
        """Search learnings using FTS5.

        Supports:
        - Simple keywords: "async boto3"
        - Phrases: '"event loop"'
        - Boolean: "async AND NOT blocking"
        """
        # Sanitize search for FTS5
        search = search.replace('"', '""')

        if learning_type:
            rows = self.conn.execute("""
                SELECT l.* FROM learnings l
                JOIN learnings_fts fts ON l.rowid = fts.rowid
                WHERE learnings_fts MATCH ?
                AND l.type = ?
                ORDER BY rank
                LIMIT ?
            """, (search, learning_type.value, limit)).fetchall()
        else:
            rows = self.conn.execute("""
                SELECT l.* FROM learnings l
                JOIN learnings_fts fts ON l.rowid = fts.rowid
                WHERE learnings_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (search, limit)).fetchall()

        return [self._row_to_learning(row) for row in rows]

    def get_recent(self, hours: int = 24, limit: int = 20) -> List[Learning]:
        """Get learnings from the last N hours."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

        rows = self.conn.execute("""
            SELECT * FROM learnings
            WHERE created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (cutoff, limit)).fetchall()

        return [self._row_to_learning(row) for row in rows]

    def get_by_id(self, learning_id: str) -> Optional[Learning]:
        """Get a specific learning by ID."""
        row = self.conn.execute(
            "SELECT * FROM learnings WHERE id = ?",
            (learning_id,)
        ).fetchone()

        return self._row_to_learning(row) if row else None

    def get_by_type(self, learning_type: LearningType, limit: int = 50) -> List[Learning]:
        """Get learnings by type."""
        rows = self.conn.execute("""
            SELECT * FROM learnings
            WHERE type = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (learning_type.value, limit)).fetchall()

        return [self._row_to_learning(row) for row in rows]

    def list_learnings(
        self,
        limit: int = 100,
        offset: int = 0,
        learning_type: Optional[str] = None,
    ) -> List[Learning]:
        """List learnings with pagination and optional type filter.

        This method provides a consistent interface for the API server.

        Args:
            limit: Maximum number of learnings to return
            offset: Number of learnings to skip (for pagination)
            learning_type: Optional type filter (as string, e.g., "win", "fix")

        Returns:
            List of Learning objects, sorted by created_at descending
        """
        if learning_type:
            rows = self.conn.execute("""
                SELECT * FROM learnings
                WHERE type = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (learning_type, limit, offset)).fetchall()
        else:
            rows = self.conn.execute("""
                SELECT * FROM learnings
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()

        return [self._row_to_learning(row) for row in rows]

    def get_stats(self) -> dict:
        """Get storage statistics."""
        total = self.conn.execute(
            "SELECT COUNT(*) FROM learnings"
        ).fetchone()[0]

        by_type = {}
        for lt in LearningType:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM learnings WHERE type = ?",
                (lt.value,)
            ).fetchone()[0]
            by_type[lt.value] = count

        # Get today's count
        today = datetime.now().replace(hour=0, minute=0, second=0).isoformat()
        today_count = self.conn.execute(
            "SELECT COUNT(*) FROM learnings WHERE created_at >= ?",
            (today,)
        ).fetchone()[0]

        # Get last capture time
        last = self.conn.execute(
            "SELECT created_at FROM learnings ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        last_capture = last[0] if last else None

        return {
            "total": total,
            "by_type": by_type,
            "today": today_count,
            "last_capture": last_capture,
            "db_size_mb": round(self.db_path.stat().st_size / 1024 / 1024, 2)
            if self.db_path.exists() else 0,
        }

    def health_check(self) -> bool:
        """Check if SQLite is working."""
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    def _row_to_learning(self, row: sqlite3.Row) -> Learning:
        """Convert a database row to a Learning object.

        Falls back to INSIGHT for unknown types to prevent API crashes
        while preserving data integrity (the raw type string stays in DB).
        """
        try:
            learning_type = LearningType(row["type"])
        except ValueError:
            learning_type = LearningType.INSIGHT
        return Learning(
            id=row["id"],
            type=learning_type,
            title=row["title"],
            content=row["content"],
            tags=json.loads(row["tags"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]),
        )

    def _increment_stat(self, key: str) -> None:
        """Increment a stat counter."""
        self.conn.execute("""
            INSERT INTO stats (key, value) VALUES (?, '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1
        """, (key,))

    def export_json(self) -> str:
        """Export all learnings as JSON."""
        learnings = []
        for row in self.conn.execute("SELECT * FROM learnings ORDER BY created_at"):
            learnings.append(self._row_to_learning(row).to_dict())
        return json.dumps(learnings, indent=2)

    def import_json(self, json_data: str) -> int:
        """Import learnings from JSON. Returns count imported."""
        data = json.loads(json_data)
        count = 0
        for item in data:
            learning = Learning.from_dict(item)
            self.record(learning)
            count += 1
        return count
