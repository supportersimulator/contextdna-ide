"""
Hippocampus Agent - Context Indexer

The Hippocampus indexes and organizes context - creating searchable
structures from raw information for efficient retrieval.

Anatomical Label: Hippocampus (Context Indexer)
"""

from __future__ import annotations
import json
import hashlib
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class HippocampusAgent(Agent):
    """
    Hippocampus Agent - Context indexing and organization.

    Responsibilities:
    - Index prompts and responses
    - Create searchable context structures
    - Track context relationships
    - Enable fast context retrieval
    """

    NAME = "hippocampus"
    CATEGORY = AgentCategory.MEMORY
    DESCRIPTION = "Context indexing and organization for fast retrieval"
    ANATOMICAL_LABEL = "Hippocampus (Context Indexer)"
    IS_VITAL = True

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._db_path = Path.home() / ".context-dna" / ".hippocampus.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def _on_start(self):
        """Initialize indexer."""
        self._ensure_db()

    def _on_stop(self):
        """Shutdown indexer."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_db(self):
        """Create indexer database."""
        self._conn = sqlite3.connect(str(self._db_path))

        # Context index table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS context_index (
                id TEXT PRIMARY KEY,
                content_hash TEXT,
                content_preview TEXT,
                context_type TEXT,
                project TEXT,
                file_path TEXT,
                keywords TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                accessed_at TEXT,
                access_count INTEGER DEFAULT 0
            )
        """)

        # Relationships table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS context_relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT,
                target_id TEXT,
                relationship_type TEXT,
                weight REAL DEFAULT 1.0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (source_id) REFERENCES context_index(id),
                FOREIGN KEY (target_id) REFERENCES context_index(id)
            )
        """)

        # Indexes for fast search
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_context_keywords ON context_index(keywords)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_context_project ON context_index(project)
        """)

        # FTS virtual table for full-text search
        try:
            self._conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS context_fts
                USING fts5(content_preview, keywords, content=context_index, content_rowid=rowid)
            """)
        except Exception:
            pass  # FTS may not be available

        self._conn.commit()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check hippocampus health."""
        try:
            if not self._conn:
                return {"healthy": False, "message": "Not connected", "score": 0.0}

            cursor = self._conn.execute("SELECT COUNT(*) FROM context_index")
            count = cursor.fetchone()[0]

            return {
                "healthy": True,
                "score": 1.0,
                "message": f"Indexing {count} contexts",
                "metrics": {"indexed_count": count}
            }
        except Exception as e:
            return {"healthy": False, "message": str(e), "score": 0.0}

    def process(self, input_data: Any) -> Any:
        """Process indexing operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "index")
            if op == "index":
                return self.index_context(input_data)
            elif op == "search":
                return self.search(input_data.get("query"), input_data.get("limit", 10))
            elif op == "relate":
                return self.add_relationship(
                    input_data.get("source_id"),
                    input_data.get("target_id"),
                    input_data.get("relationship_type", "related")
                )
        return None

    def index_context(self, context: Dict[str, Any]) -> str:
        """
        Index a context entry.

        Returns the context ID.
        """
        try:
            if not self._conn:
                self._ensure_db()

            content = context.get("content", "")
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            context_id = f"ctx_{content_hash}"

            # Extract keywords
            keywords = self._extract_keywords(content)

            self._conn.execute("""
                INSERT OR REPLACE INTO context_index
                (id, content_hash, content_preview, context_type, project, file_path, keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                context_id,
                content_hash,
                content[:500],
                context.get("type", "general"),
                context.get("project"),
                context.get("file_path"),
                ",".join(keywords)
            ))
            self._conn.commit()
            self._last_active = datetime.utcnow()

            return context_id
        except Exception as e:
            self._handle_error(e)
            return ""

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search indexed contexts."""
        try:
            if not self._conn:
                self._ensure_db()

            # Try FTS first
            try:
                cursor = self._conn.execute("""
                    SELECT c.id, c.content_preview, c.context_type, c.project, c.file_path
                    FROM context_index c
                    JOIN context_fts f ON c.rowid = f.rowid
                    WHERE context_fts MATCH ?
                    LIMIT ?
                """, (query, limit))
            except Exception:
                # Fallback to LIKE
                cursor = self._conn.execute("""
                    SELECT id, content_preview, context_type, project, file_path
                    FROM context_index
                    WHERE content_preview LIKE ? OR keywords LIKE ?
                    ORDER BY access_count DESC
                    LIMIT ?
                """, (f"%{query}%", f"%{query}%", limit))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "id": row[0],
                    "preview": row[1],
                    "type": row[2],
                    "project": row[3],
                    "file_path": row[4]
                })

                # Update access tracking
                self._conn.execute("""
                    UPDATE context_index
                    SET accessed_at = ?, access_count = access_count + 1
                    WHERE id = ?
                """, (datetime.utcnow().isoformat(), row[0]))

            self._conn.commit()
            return results

        except Exception as e:
            self._handle_error(e)
            return []

    def add_relationship(self, source_id: str, target_id: str, rel_type: str = "related") -> bool:
        """Add a relationship between contexts."""
        try:
            if not self._conn:
                self._ensure_db()

            self._conn.execute("""
                INSERT INTO context_relationships (source_id, target_id, relationship_type)
                VALUES (?, ?, ?)
            """, (source_id, target_id, rel_type))
            self._conn.commit()
            return True
        except Exception as e:
            self._handle_error(e)
            return False

    def get_related(self, context_id: str) -> List[Dict[str, Any]]:
        """Get contexts related to a given context."""
        try:
            if not self._conn:
                self._ensure_db()

            cursor = self._conn.execute("""
                SELECT c.id, c.content_preview, r.relationship_type, r.weight
                FROM context_relationships r
                JOIN context_index c ON r.target_id = c.id
                WHERE r.source_id = ?
                ORDER BY r.weight DESC
            """, (context_id,))

            return [
                {
                    "id": row[0],
                    "preview": row[1],
                    "relationship": row[2],
                    "weight": row[3]
                }
                for row in cursor.fetchall()
            ]
        except Exception as e:
            self._handle_error(e)
            return []

    def _extract_keywords(self, content: str) -> List[str]:
        """Extract keywords from content."""
        # Simple keyword extraction
        words = content.lower().split()
        # Filter common words and short words
        stopwords = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                     'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
                     'would', 'could', 'should', 'may', 'might', 'must', 'shall',
                     'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
                     'this', 'that', 'these', 'those', 'it', 'its', 'and', 'or'}
        keywords = [w for w in words if len(w) > 3 and w not in stopwords]
        # Return unique keywords, max 20
        return list(set(keywords))[:20]
