"""PostgreSQL + pgvector Storage Backend for Context DNA.

Provides semantic search capabilities using vector embeddings.
Requires PostgreSQL with pgvector extension.
"""

import json
import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

from context_dna.storage.backend import Learning, LearningType, StorageBackend


class VectorStore(StorageBackend):
    """PostgreSQL + pgvector storage backend.

    Features:
    - Semantic search using vector embeddings
    - Full-text search as fallback
    - Hybrid search combining both
    - Efficient batch operations
    - Connection pooling support
    """

    def __init__(
        self,
        connection_string: Optional[str] = None,
        embedding_dimension: int = 1536,
    ):
        """Initialize vector store.

        Args:
            connection_string: PostgreSQL connection string
            embedding_dimension: Dimension of embedding vectors (default: 1536 for OpenAI)
        """
        if psycopg2 is None:
            raise ImportError(
                "psycopg2 package required for VectorStore. "
                "Install with: pip install psycopg2-binary"
            )

        self._conn_string = connection_string or os.getenv(
            "CONTEXT_DNA_POSTGRES_URL",
            f"postgresql://context_dna:{os.getenv('POSTGRES_PASSWORD', 'context_dna_dev')}@localhost:5432/context_dna"
        )
        self._embedding_dimension = embedding_dimension
        self._conn = None

    @property
    def conn(self):
        """Get database connection (lazy initialization)."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._conn_string)
            self._conn.autocommit = True
        return self._conn

    def record(self, learning: Learning, embedding: Optional[List[float]] = None) -> str:
        """Record a learning with optional embedding.

        Args:
            learning: Learning object to store
            embedding: Optional pre-computed embedding vector

        Returns:
            Learning ID
        """
        with self.conn.cursor() as cur:
            # Generate ID if not provided
            if not learning.id:
                cur.execute("SELECT gen_random_uuid()::text")
                learning.id = cur.fetchone()[0][:8]  # Short ID for readability

            # Build embedding value
            embedding_value = None
            if embedding:
                embedding_value = f"[{','.join(str(x) for x in embedding)}]"

            cur.execute(
                """
                INSERT INTO learnings (
                    id, type, title, content, tags, embedding,
                    created_at, updated_at, metadata, source
                ) VALUES (
                    %s::uuid, %s, %s, %s, %s, %s::vector,
                    %s, %s, %s::jsonb, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    tags = EXCLUDED.tags,
                    embedding = COALESCE(EXCLUDED.embedding, learnings.embedding),
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata
                RETURNING id::text
                """,
                (
                    learning.id if len(learning.id) > 8 else f"{learning.id}-0000-0000-0000-000000000000",
                    learning.type.value,
                    learning.title,
                    learning.content,
                    learning.tags,
                    embedding_value,
                    learning.created_at,
                    learning.updated_at,
                    json.dumps(learning.metadata),
                    learning.metadata.get("source", "manual"),
                ),
            )

            result = cur.fetchone()
            return result[0][:8] if result else learning.id

    def record_with_embedding(
        self,
        learning: Learning,
        embedding_provider: Any,  # ProviderManager or BaseLLMProvider
    ) -> str:
        """Record a learning and generate embedding automatically.

        Args:
            learning: Learning object to store
            embedding_provider: Provider to generate embedding

        Returns:
            Learning ID
        """
        # Generate embedding from title + content
        text_to_embed = f"{learning.title}\n\n{learning.content}"
        embedding_response = embedding_provider.embed(text_to_embed)

        return self.record(learning, embedding_response.embedding)

    def query(
        self,
        search: str,
        limit: int = 10,
        learning_type: Optional[LearningType] = None,
    ) -> List[Learning]:
        """Search learnings using full-text search.

        For semantic search, use semantic_search() instead.

        Args:
            search: Search query
            limit: Maximum results
            learning_type: Filter by type

        Returns:
            List of matching learnings
        """
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if learning_type:
                cur.execute(
                    """
                    SELECT id::text, type, title, content, tags, metadata,
                           created_at, updated_at,
                           ts_rank(to_tsvector('english', title || ' ' || content),
                                   plainto_tsquery('english', %s)) as rank
                    FROM learnings
                    WHERE to_tsvector('english', title || ' ' || content)
                          @@ plainto_tsquery('english', %s)
                      AND type = %s
                    ORDER BY rank DESC
                    LIMIT %s
                    """,
                    (search, search, learning_type.value, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id::text, type, title, content, tags, metadata,
                           created_at, updated_at,
                           ts_rank(to_tsvector('english', title || ' ' || content),
                                   plainto_tsquery('english', %s)) as rank
                    FROM learnings
                    WHERE to_tsvector('english', title || ' ' || content)
                          @@ plainto_tsquery('english', %s)
                    ORDER BY rank DESC
                    LIMIT %s
                    """,
                    (search, search, limit),
                )

            return [self._row_to_learning(row) for row in cur.fetchall()]

    def semantic_search(
        self,
        query_embedding: List[float],
        limit: int = 10,
        learning_type: Optional[LearningType] = None,
        similarity_threshold: float = 0.7,
        project: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search learnings using vector similarity.

        Args:
            query_embedding: Query embedding vector
            limit: Maximum results
            learning_type: Filter by type
            similarity_threshold: Minimum similarity score (0-1)
            project: Filter by project name

        Returns:
            List of results with similarity scores
        """
        embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id::text, type, title, content, tags, metadata,
                    created_at, updated_at,
                    1 - (embedding <=> %s::vector) as similarity
                FROM learnings
                WHERE embedding IS NOT NULL
                  AND (%s IS NULL OR type = %s)
                  AND (%s IS NULL OR project = %s)
                  AND 1 - (embedding <=> %s::vector) > %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (
                    embedding_str,
                    learning_type.value if learning_type else None,
                    learning_type.value if learning_type else None,
                    project,
                    project,
                    embedding_str,
                    similarity_threshold,
                    embedding_str,
                    limit,
                ),
            )

            results = []
            for row in cur.fetchall():
                learning = self._row_to_learning(row)
                results.append({
                    "learning": learning,
                    "similarity": row["similarity"],
                })

            return results

    def hybrid_search(
        self,
        text_query: str,
        query_embedding: List[float],
        limit: int = 10,
        learning_type: Optional[LearningType] = None,
        semantic_weight: float = 0.7,
    ) -> List[Dict[str, Any]]:
        """Hybrid search combining semantic and full-text search.

        Args:
            text_query: Text search query
            query_embedding: Query embedding vector
            limit: Maximum results
            learning_type: Filter by type
            semantic_weight: Weight for semantic vs text search (0-1)

        Returns:
            List of results with combined scores
        """
        embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"
        text_weight = 1 - semantic_weight

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                WITH semantic AS (
                    SELECT id, 1 - (embedding <=> %s::vector) as sem_score
                    FROM learnings
                    WHERE embedding IS NOT NULL
                ),
                fulltext AS (
                    SELECT id,
                           ts_rank(to_tsvector('english', title || ' ' || content),
                                   plainto_tsquery('english', %s)) as ft_score
                    FROM learnings
                )
                SELECT
                    l.id::text, l.type, l.title, l.content, l.tags, l.metadata,
                    l.created_at, l.updated_at,
                    COALESCE(s.sem_score, 0) * %s + COALESCE(f.ft_score, 0) * %s as score,
                    COALESCE(s.sem_score, 0) as semantic_score,
                    COALESCE(f.ft_score, 0) as fulltext_score
                FROM learnings l
                LEFT JOIN semantic s ON l.id = s.id
                LEFT JOIN fulltext f ON l.id = f.id
                WHERE (%s IS NULL OR l.type = %s)
                ORDER BY score DESC
                LIMIT %s
                """,
                (
                    embedding_str,
                    text_query,
                    semantic_weight,
                    text_weight,
                    learning_type.value if learning_type else None,
                    learning_type.value if learning_type else None,
                    limit,
                ),
            )

            results = []
            for row in cur.fetchall():
                learning = self._row_to_learning(row)
                results.append({
                    "learning": learning,
                    "score": row["score"],
                    "semantic_score": row["semantic_score"],
                    "fulltext_score": row["fulltext_score"],
                })

            return results

    def get_recent(self, hours: int = 24, limit: int = 20) -> List[Learning]:
        """Get recent learnings."""
        cutoff = datetime.now() - timedelta(hours=hours)

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id::text, type, title, content, tags, metadata,
                       created_at, updated_at
                FROM learnings
                WHERE created_at >= %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (cutoff, limit),
            )

            return [self._row_to_learning(row) for row in cur.fetchall()]

    def get_by_id(self, learning_id: str) -> Optional[Learning]:
        """Get a learning by ID."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Handle short IDs
            if len(learning_id) <= 8:
                cur.execute(
                    """
                    SELECT id::text, type, title, content, tags, metadata,
                           created_at, updated_at
                    FROM learnings
                    WHERE id::text LIKE %s
                    LIMIT 1
                    """,
                    (f"{learning_id}%",),
                )
            else:
                cur.execute(
                    """
                    SELECT id::text, type, title, content, tags, metadata,
                           created_at, updated_at
                    FROM learnings
                    WHERE id = %s::uuid
                    """,
                    (learning_id,),
                )

            row = cur.fetchone()
            return self._row_to_learning(row) if row else None

    def get_by_type(
        self, learning_type: LearningType, limit: int = 50
    ) -> List[Learning]:
        """Get learnings by type."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id::text, type, title, content, tags, metadata,
                       created_at, updated_at
                FROM learnings
                WHERE type = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (learning_type.value, limit),
            )

            return [self._row_to_learning(row) for row in cur.fetchall()]

    def get_stats(self) -> dict:
        """Get storage statistics."""
        with self.conn.cursor() as cur:
            # Total count
            cur.execute("SELECT COUNT(*) FROM learnings")
            total = cur.fetchone()[0]

            # Count by type
            cur.execute(
                """
                SELECT type, COUNT(*) as count
                FROM learnings
                GROUP BY type
                """
            )
            by_type = {row[0]: row[1] for row in cur.fetchall()}

            # Today's count
            cur.execute(
                """
                SELECT COUNT(*) FROM learnings
                WHERE created_at >= CURRENT_DATE
                """
            )
            today = cur.fetchone()[0]

            # Last capture
            cur.execute(
                """
                SELECT created_at FROM learnings
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            last = cur.fetchone()
            last_capture = last[0].isoformat() if last else None

            # Embedding coverage
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE embedding IS NOT NULL) as with_embedding,
                    COUNT(*) as total
                FROM learnings
                """
            )
            embed_stats = cur.fetchone()
            embedding_coverage = (
                embed_stats[0] / embed_stats[1] if embed_stats[1] > 0 else 0
            )

            return {
                "total": total,
                "by_type": by_type,
                "today": today,
                "last_capture": last_capture,
                "embedding_coverage": f"{embedding_coverage:.1%}",
                "backend": "pgvector",
            }

    def health_check(self) -> bool:
        """Check database health."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                return cur.fetchone() is not None
        except Exception:
            return False

    def close(self) -> None:
        """Close database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()

    def _row_to_learning(self, row: dict) -> Learning:
        """Convert database row to Learning object."""
        return Learning(
            id=row["id"][:8] if row["id"] else "",
            type=LearningType(row["type"]),
            title=row["title"],
            content=row["content"],
            tags=row["tags"] or [],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=row["metadata"] or {},
        )

    def update_embedding(
        self,
        learning_id: str,
        embedding: List[float],
    ) -> bool:
        """Update embedding for an existing learning.

        Args:
            learning_id: Learning ID
            embedding: New embedding vector

        Returns:
            True if updated
        """
        embedding_str = f"[{','.join(str(x) for x in embedding)}]"

        with self.conn.cursor() as cur:
            if len(learning_id) <= 8:
                cur.execute(
                    """
                    UPDATE learnings
                    SET embedding = %s::vector, updated_at = NOW()
                    WHERE id::text LIKE %s
                    """,
                    (embedding_str, f"{learning_id}%"),
                )
            else:
                cur.execute(
                    """
                    UPDATE learnings
                    SET embedding = %s::vector, updated_at = NOW()
                    WHERE id = %s::uuid
                    """,
                    (embedding_str, learning_id),
                )

            return cur.rowcount > 0

    def backfill_embeddings(
        self,
        embedding_provider: Any,
        batch_size: int = 100,
        limit: Optional[int] = None,
    ) -> int:
        """Backfill embeddings for learnings that don't have them.

        Args:
            embedding_provider: Provider to generate embeddings
            batch_size: Number to process at a time
            limit: Maximum total to process (None for all)

        Returns:
            Number of embeddings generated
        """
        count = 0

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            while True:
                # Get batch of learnings without embeddings
                query = """
                    SELECT id::text, title, content
                    FROM learnings
                    WHERE embedding IS NULL
                    ORDER BY created_at DESC
                    LIMIT %s
                """
                cur.execute(query, (batch_size,))
                rows = cur.fetchall()

                if not rows:
                    break

                # Generate embeddings
                texts = [f"{row['title']}\n\n{row['content']}" for row in rows]
                embeddings = embedding_provider.embed_batch(texts)

                # Update each learning
                for row, emb_response in zip(rows, embeddings):
                    self.update_embedding(row["id"], emb_response.embedding)
                    count += 1

                    if limit and count >= limit:
                        return count

        return count

    def export_json(self) -> str:
        """Export all learnings as JSON."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id::text, type, title, content, tags, metadata,
                       created_at, updated_at
                FROM learnings
                ORDER BY created_at
                """
            )

            learnings = []
            for row in cur.fetchall():
                learning = self._row_to_learning(row)
                learnings.append(learning.to_dict())

            return json.dumps(learnings, indent=2, default=str)

    def import_json(self, json_data: str) -> int:
        """Import learnings from JSON."""
        data = json.loads(json_data)
        count = 0

        for item in data:
            learning = Learning.from_dict(item)
            self.record(learning)
            count += 1

        return count
