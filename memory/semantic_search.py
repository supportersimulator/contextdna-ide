#!/usr/bin/env python3
"""
Semantic Rescue Layer — activates when FTS5 returns <3 results.

Uses sentence-transformers (lazy-loaded) for cosine similarity search.
Falls back gracefully if sentence-transformers not installed.

Architecture:
    Query → FTS5 search → 3+ results? → return results
                        → <3 results? → semantic_search() → merge → return

Model: all-MiniLM-L6-v2 (~80MB, fast inference)
Embeddings stored in: ~/.context-dna/.semantic_embeddings.db (WAL mode)

IMPORTANT:
- Model is lazy-loaded — zero cost on import
- If sentence-transformers not installed, silently degrades (returns [])
- All SQLite connections use try/finally/conn.close() pattern
- Embeddings DB is separate from learnings.db to avoid bloat
"""

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Lazy-loaded model (None until first use)
_model = None
_model_load_attempted = False

EMBEDDINGS_DB = Path(
    os.environ.get(
        "SEMANTIC_EMBEDDINGS_DB",
        os.path.expanduser("~/.context-dna/.semantic_embeddings.db"),
    )
)

# Model config
MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 output dimension


def is_index_available() -> bool:
    """Check if semantic embedding index exists and has entries."""
    try:
        if not EMBEDDINGS_DB.exists():
            return False
        from memory.db_utils import safe_conn
        with safe_conn(str(EMBEDDINGS_DB)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            return count > 0
    except Exception:
        return False


def get_model():
    """Lazy-load sentence-transformer model. Returns None if not installed.

    Only attempts load once per process. Subsequent calls return cached
    result (model or None) instantly.
    """
    global _model, _model_load_attempted
    if _model is not None:
        return _model
    if _model_load_attempted:
        return None  # Already tried and failed
    _model_load_attempted = True
    try:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(MODEL_NAME)
        logger.info("Semantic rescue layer: model loaded (%s)", MODEL_NAME)
        return _model
    except ImportError:
        logger.debug(
            "sentence-transformers not installed — semantic rescue disabled"
        )
        return None
    except Exception as e:
        logger.warning("Failed to load sentence-transformer model: %s", e)
        return None


def _get_embeddings_conn() -> sqlite3.Connection:
    """Get a new connection to the embeddings DB. Caller MUST close it."""
    EMBEDDINGS_DB.parent.mkdir(parents=True, exist_ok=True)
    from memory.db_utils import connect_wal
    return connect_wal(str(EMBEDDINGS_DB))


def _ensure_embeddings_schema(conn: sqlite3.Connection) -> None:
    """Create embeddings table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            learning_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content_snippet TEXT NOT NULL DEFAULT '',
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL,
            model_name TEXT NOT NULL DEFAULT 'all-MiniLM-L6-v2'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()


def _cosine_similarity(a, b) -> float:
    """Compute cosine similarity between two vectors (numpy arrays)."""
    import numpy as np

    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def semantic_search(query: str, top_k: int = 5) -> List[Dict]:
    """
    Search learnings by semantic similarity.

    Only call this when FTS5 returns <3 results.
    Returns list of {id, title, content, score, source} dicts.

    Performance: ~5-20ms per query after model warmup (embeddings pre-computed).
    """
    model = get_model()
    if model is None:
        return []

    try:
        import numpy as np
    except ImportError:
        logger.debug("numpy not available — semantic search disabled")
        return []

    # Encode query
    try:
        query_embedding = model.encode(query, normalize_embeddings=True)
    except Exception as e:
        logger.warning("Failed to encode query: %s", e)
        return []

    # Load all embeddings from DB
    conn = _get_embeddings_conn()
    try:
        _ensure_embeddings_schema(conn)
        cursor = conn.execute(
            "SELECT learning_id, title, content_snippet, embedding FROM embeddings"
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    if not rows:
        logger.debug("No embeddings in index — run build_embedding_index()")
        return []

    # Compute similarities
    scored = []
    for row in rows:
        try:
            stored_embedding = np.frombuffer(row["embedding"], dtype=np.float32)
            if len(stored_embedding) != EMBEDDING_DIM:
                continue  # Dimension mismatch — skip corrupt entry
            sim = _cosine_similarity(query_embedding, stored_embedding)
            scored.append(
                {
                    "id": row["learning_id"],
                    "title": row["title"],
                    "content": row["content_snippet"],
                    "score": sim,
                    "source": "semantic_rescue",
                }
            )
        except Exception:
            continue  # Skip corrupt embeddings

    # Sort by similarity (descending) and return top_k
    scored.sort(key=lambda x: x["score"], reverse=True)

    # Filter out very low similarity results (noise floor)
    MIN_SIMILARITY = 0.15
    scored = [s for s in scored if s["score"] >= MIN_SIMILARITY]

    return scored[:top_k]


def build_embedding_index(force_rebuild: bool = False) -> Dict:
    """
    Build/rebuild embedding index from all learnings in SQLite.

    Reads from the learnings.db (via singleton) and creates embeddings
    for each learning's title + content. Stores in embeddings DB.

    Args:
        force_rebuild: If True, rebuild all embeddings. If False, only
                       add new learnings not already indexed.

    Returns:
        Dict with stats: {total, new, skipped, errors, duration_ms}
    """
    model = get_model()
    if model is None:
        return {"total": 0, "new": 0, "skipped": 0, "errors": 0, "duration_ms": 0,
                "status": "model_unavailable"}

    try:
        import numpy as np
    except ImportError:
        return {"total": 0, "new": 0, "skipped": 0, "errors": 0, "duration_ms": 0,
                "status": "numpy_unavailable"}

    start = time.monotonic()

    # Get all learnings from SQLite storage
    try:
        from memory.sqlite_storage import get_sqlite_storage

        store = get_sqlite_storage()
        # Fetch all learnings (up to 5000 for safety)
        all_learnings = store.conn.execute(
            "SELECT id, title, content, tags FROM learnings ORDER BY created_at DESC LIMIT 5000"
        ).fetchall()
    except Exception as e:
        logger.warning("Failed to read learnings from SQLite: %s", e)
        return {"total": 0, "new": 0, "skipped": 0, "errors": 1, "duration_ms": 0,
                "status": f"sqlite_error: {e}"}

    if not all_learnings:
        return {"total": 0, "new": 0, "skipped": 0, "errors": 0, "duration_ms": 0,
                "status": "no_learnings"}

    # Open embeddings DB
    conn = _get_embeddings_conn()
    try:
        _ensure_embeddings_schema(conn)

        # Get already-indexed IDs (skip if not force_rebuild)
        existing_ids = set()
        if not force_rebuild:
            cursor = conn.execute("SELECT learning_id FROM embeddings")
            existing_ids = {row["learning_id"] for row in cursor.fetchall()}

        new_count = 0
        skip_count = 0
        error_count = 0

        # Batch encode for efficiency
        to_encode = []
        to_encode_ids = []
        to_encode_titles = []
        to_encode_snippets = []

        for row in all_learnings:
            learning_id = row["id"]

            if not force_rebuild and learning_id in existing_ids:
                skip_count += 1
                continue

            title = row["title"] or ""
            content = row["content"] or ""
            tags_str = row["tags"] or "[]"

            # Parse tags for richer text
            try:
                tags = json.loads(tags_str)
                if isinstance(tags, list):
                    tags_text = " ".join(tags)
                else:
                    tags_text = str(tags)
            except (json.JSONDecodeError, TypeError):
                tags_text = ""

            # Combine title + content snippet + tags for embedding
            # Truncate content to avoid excessive compute
            content_snippet = content[:500]
            text_to_embed = f"{title}. {content_snippet} {tags_text}".strip()

            if not text_to_embed:
                skip_count += 1
                continue

            to_encode.append(text_to_embed)
            to_encode_ids.append(learning_id)
            to_encode_titles.append(title)
            to_encode_snippets.append(content_snippet)

        # Batch encode all at once (much faster than one-by-one)
        if to_encode:
            try:
                embeddings = model.encode(
                    to_encode,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                    batch_size=64,
                )

                # Store in DB
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc).isoformat()

                for i, embedding in enumerate(embeddings):
                    try:
                        embedding_bytes = np.array(
                            embedding, dtype=np.float32
                        ).tobytes()
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO embeddings
                            (learning_id, title, content_snippet, embedding, created_at, model_name)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """,
                            (
                                to_encode_ids[i],
                                to_encode_titles[i],
                                to_encode_snippets[i][:200],
                                embedding_bytes,
                                now,
                                MODEL_NAME,
                            ),
                        )
                        new_count += 1
                    except Exception as e:
                        logger.debug(
                            "Failed to store embedding for %s: %s",
                            to_encode_ids[i],
                            e,
                        )
                        error_count += 1

                conn.commit()
            except Exception as e:
                logger.warning("Batch encoding failed: %s", e)
                error_count += len(to_encode)

        # Clean up embeddings for deleted learnings
        if force_rebuild:
            current_ids = {row["id"] for row in all_learnings}
            cursor = conn.execute("SELECT learning_id FROM embeddings")
            indexed_ids = {row["learning_id"] for row in cursor.fetchall()}
            stale_ids = indexed_ids - current_ids
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                conn.execute(
                    f"DELETE FROM embeddings WHERE learning_id IN ({placeholders})",
                    list(stale_ids),
                )
                conn.commit()
                logger.info("Cleaned %d stale embeddings", len(stale_ids))

        # Update meta
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_build', ?)",
            (str(time.time()),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('total_embeddings', ?)",
            (str(new_count + skip_count),),
        )
        conn.commit()

        duration_ms = int((time.monotonic() - start) * 1000)

        result = {
            "total": len(all_learnings),
            "new": new_count,
            "skipped": skip_count,
            "errors": error_count,
            "duration_ms": duration_ms,
            "status": "ok",
        }
        logger.info(
            "Embedding index built: %d new, %d skipped, %d errors (%dms)",
            new_count,
            skip_count,
            error_count,
            duration_ms,
        )
        return result

    finally:
        conn.close()


def rerank_results(results: list, query: str = "") -> list:
    """Rerank search results by recency, type priority, and usage.

    Applies weighted scoring:
    - 0.5 * original relevance score
    - 0.3 * recency (newer = higher)
    - 0.15 * type priority (SOPs/fixes > patterns > wins)
    - 0.05 * usage/merge count
    """
    from datetime import datetime

    TYPE_PRIORITY = {
        'fix': 1.5, 'gotcha': 1.5, 'bug-fix': 1.5,
        'process': 1.3, 'sop': 1.3,
        'pattern': 1.2,
        'win': 1.0, 'learning': 1.0,
    }

    reranked = []
    now = datetime.now()

    for r in results:
        # Original relevance (position-based if no score)
        base_score = r.get('score', r.get('_hybrid_score', 0.5))

        # Recency boost
        created_at = r.get('created_at', '')
        try:
            if created_at:
                created = datetime.fromisoformat(
                    created_at.replace('Z', '+00:00').replace('+00:00', '')
                )
                days_old = max((now - created).days, 0)
                recency = max(1.0 - (days_old / 30.0), 0.0)
            else:
                recency = 0.3  # Unknown age = mid-score
        except Exception:
            recency = 0.3

        # Type priority
        result_type = r.get('type', r.get('sop_type', 'learning')).lower()
        type_boost = TYPE_PRIORITY.get(result_type, 1.0)

        # Usage/merge count
        merge_count = r.get('merge_count', 1)
        usage_boost = min(merge_count / 5.0, 1.0)

        combined = (
            (0.5 * base_score * type_boost)
            + (0.3 * recency)
            + (0.15 * type_boost / 1.5)
            + (0.05 * usage_boost)
        )

        r['_rerank_score'] = combined
        reranked.append((combined, r))

    reranked.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in reranked]


def rescue_search(
    query: str,
    fts5_results: list,
    min_results: int = 3,
    top_k: int = 5,
) -> list:
    """
    Main entry point. If fts5_results has enough results, return them.
    Otherwise, augment with semantic search results.

    Args:
        query: The search query string
        fts5_results: Results already obtained from FTS5
        min_results: Minimum acceptable result count from FTS5
        top_k: Max semantic results to add

    Returns:
        Merged list of results (FTS5 + semantic, deduplicated)
    """
    if len(fts5_results) >= min_results:
        return fts5_results

    # How many more results do we need?
    needed = top_k - len(fts5_results)
    if needed <= 0:
        needed = 1

    semantic_results = semantic_search(query, top_k=needed + 2)  # Fetch extra for dedup

    if not semantic_results:
        return fts5_results

    # Collect existing IDs for deduplication
    existing_ids = set()
    for r in fts5_results:
        rid = r.get("id", "")
        if rid:
            existing_ids.add(rid)
        # Also deduplicate by title (some results may have different IDs
        # but same content)
        rtitle = r.get("title", "").lower().strip()
        if rtitle:
            existing_ids.add(rtitle)

    # Merge: add semantic results not already in FTS5 results
    merged = list(fts5_results)
    for sr in semantic_results:
        sr_id = sr.get("id", "")
        sr_title = sr.get("title", "").lower().strip()

        if sr_id in existing_ids or sr_title in existing_ids:
            continue  # Skip duplicate

        # Convert semantic result to match FTS5 result format
        merged.append(
            {
                "id": sr_id,
                "title": sr.get("title", ""),
                "content": sr.get("content", ""),
                "type": "semantic_rescue",
                "tags": [],
                "distance": 1.0 - sr.get("score", 0.5),
                "source": "semantic_rescue",
            }
        )
        existing_ids.add(sr_id)
        if sr_title:
            existing_ids.add(sr_title)

    # Before returning merged results, apply reranking
    try:
        merged = rerank_results(merged)
    except Exception:
        pass  # Reranking failure is non-fatal
    return merged


def get_index_stats() -> Dict:
    """Get statistics about the embedding index."""
    if not EMBEDDINGS_DB.exists():
        return {"exists": False, "count": 0, "size_kb": 0, "last_build": None}

    conn = _get_embeddings_conn()
    try:
        _ensure_embeddings_schema(conn)
        count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

        last_build = None
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_build'"
            ).fetchone()
            if row:
                last_build = float(row["value"])
        except Exception:
            pass

        size_kb = round(EMBEDDINGS_DB.stat().st_size / 1024, 2)

        return {
            "exists": True,
            "count": count,
            "size_kb": size_kb,
            "last_build": last_build,
            "model": MODEL_NAME,
            "embedding_dim": EMBEDDING_DIM,
        }
    finally:
        conn.close()


# =========================================================================
# CLI
# =========================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python semantic_search.py build       # Build/update embedding index")
        print("  python semantic_search.py rebuild     # Force full rebuild")
        print("  python semantic_search.py search <q>  # Search by query")
        print("  python semantic_search.py stats       # Show index statistics")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "build":
        result = build_embedding_index(force_rebuild=False)
        print(f"Build result: {json.dumps(result, indent=2)}")

    elif cmd == "rebuild":
        result = build_embedding_index(force_rebuild=True)
        print(f"Rebuild result: {json.dumps(result, indent=2)}")

    elif cmd == "search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "deploy production"
        print(f"Searching for: '{query}'")
        results = semantic_search(query, top_k=5)
        if results:
            for i, r in enumerate(results, 1):
                print(f"\n  {i}. [{r['score']:.3f}] {r['title']}")
                if r["content"]:
                    print(f"     {r['content'][:120]}...")
        else:
            print("  No results (model not loaded or no embeddings)")

    elif cmd == "stats":
        stats = get_index_stats()
        print(f"Index stats: {json.dumps(stats, indent=2)}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
