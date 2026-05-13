#!/usr/bin/env python3
"""
Generate Embeddings Migration Script

One-time migration to populate pgvector embeddings for semantic search.
This enables the /api/query endpoint to use vector similarity instead of keyword matching.

Prerequisites:
    1. Context DNA postgres running: cd context-dna && docker-compose up -d postgres
    2. Install sentence-transformers: pip install sentence-transformers
    3. Ensure DATABASE_URL is set (or uses default)

Usage:
    # Check current state (dry run)
    python memory/generate_embeddings.py --check

    # Generate embeddings for all blocks
    python memory/generate_embeddings.py

    # Generate with specific batch size
    python memory/generate_embeddings.py --batch-size 50

    # Use a different model
    python memory/generate_embeddings.py --model all-mpnet-base-v2

Models (trade-offs):
    - all-MiniLM-L6-v2 (default): Fast, 384 dims, good quality, ~80MB
    - all-mpnet-base-v2: Best quality, 768 dims, slower, ~420MB
    - paraphrase-MiniLM-L3-v2: Fastest, 384 dims, lower quality, ~60MB
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Tuple

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Database connection
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    os.environ.get(
        "CONTEXT_DNA_DATABASE_URL",
        f"postgresql://context_dna:{os.getenv('POSTGRES_PASSWORD', 'context_dna_dev')}@127.0.0.1:5432/context_dna"
    )
)

# Default embedding model
DEFAULT_MODEL = "all-MiniLM-L6-v2"


def check_dependencies() -> Tuple[bool, List[str]]:
    """Check if required dependencies are available."""
    missing = []

    try:
        import psycopg2
    except ImportError:
        missing.append("psycopg2-binary (pip install psycopg2-binary)")

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        missing.append("sentence-transformers (pip install sentence-transformers)")

    return len(missing) == 0, missing


def get_db_connection():
    """Get database connection."""
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def check_pgvector_extension(conn) -> bool:
    """Check if pgvector extension is installed."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS(
                SELECT 1 FROM pg_extension WHERE extname = 'vector'
            )
        """)
        return cur.fetchone()[0]


def get_embedding_stats(conn) -> Dict:
    """Get current embedding statistics."""
    stats = {}

    with conn.cursor() as cur:
        # Total learnings
        cur.execute("SELECT COUNT(*) FROM learnings")
        stats["total_blocks"] = cur.fetchone()[0]

        # Learnings with embeddings
        cur.execute("SELECT COUNT(*) FROM learnings WHERE embedding IS NOT NULL")
        stats["with_embeddings"] = cur.fetchone()[0]

        # Learnings without embeddings
        stats["without_embeddings"] = stats["total_blocks"] - stats["with_embeddings"]

        # Check embedding dimension (if any exist)
        cur.execute("""
            SELECT array_length(embedding::real[], 1)
            FROM learnings
            WHERE embedding IS NOT NULL
            LIMIT 1
        """)
        row = cur.fetchone()
        stats["embedding_dim"] = row[0] if row else None

    return stats


def get_blocks_without_embeddings(conn, limit: int = None) -> List[Dict]:
    """Get blocks that need embeddings."""
    with conn.cursor() as cur:
        query = """
            SELECT id, title, content, type, tags
            FROM learnings
            WHERE embedding IS NULL
            ORDER BY created_at DESC
        """
        if limit:
            query += f" LIMIT {limit}"

        cur.execute(query)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def generate_embedding_text(block: Dict) -> str:
    """Generate text to embed from a learning."""
    parts = []

    if block.get("title"):
        parts.append(block["title"])

    if block.get("content"):
        # Truncate long content
        content = block["content"][:2000]
        parts.append(content)

    if block.get("tags"):
        tags = block["tags"]
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = [tags]
        if tags:
            parts.append(" ".join(tags))

    return " ".join(parts)


def update_block_embedding(conn, block_id: str, embedding: List[float]):
    """Update a block's embedding in the database."""
    with conn.cursor() as cur:
        # Convert to pgvector format
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        cur.execute(
            "UPDATE learnings SET embedding = %s::vector WHERE id = %s",
            (embedding_str, block_id)
        )
    conn.commit()


def batch_update_embeddings(conn, updates: List[Tuple[str, List[float]]]):
    """Batch update embeddings for efficiency."""
    with conn.cursor() as cur:
        for block_id, embedding in updates:
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
            cur.execute(
                "UPDATE learnings SET embedding = %s::vector WHERE id = %s",
                (embedding_str, block_id)
            )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Generate embeddings for Context DNA learnings")
    parser.add_argument("--check", action="store_true", help="Check current state without generating")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Embedding model (default: {DEFAULT_MODEL})")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size for processing (default: 32)")
    parser.add_argument("--limit", type=int, help="Limit number of learnings to process")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it")
    args = parser.parse_args()

    print("=" * 60)
    print("Context DNA Embeddings Migration")
    print("=" * 60)
    print()

    # Check dependencies
    deps_ok, missing = check_dependencies()
    if not deps_ok:
        print("Missing dependencies:")
        for dep in missing:
            print(f"  - {dep}")
        print()
        print("Install with:")
        print("  pip install psycopg2-binary sentence-transformers")
        sys.exit(1)

    print(f"Database: {DATABASE_URL.split('@')[-1]}")  # Hide credentials
    print(f"Model: {args.model}")
    print()

    # Connect to database
    try:
        conn = get_db_connection()
        print("Database connection: OK")
    except Exception as e:
        print(f"Database connection: FAILED")
        print(f"  Error: {e}")
        print()
        print("Make sure Context DNA postgres is running:")
        print("  cd context-dna && docker-compose up -d postgres")
        sys.exit(1)

    # Check pgvector
    if not check_pgvector_extension(conn):
        print("pgvector extension: NOT INSTALLED")
        print()
        print("Install pgvector in the database:")
        print("  CREATE EXTENSION IF NOT EXISTS vector;")
        conn.close()
        sys.exit(1)
    print("pgvector extension: OK")
    print()

    # Get current stats
    stats = get_embedding_stats(conn)
    print("Current State:")
    print(f"  Total learnings: {stats['total_blocks']}")
    print(f"  With embeddings: {stats['with_embeddings']}")
    print(f"  Without embeddings: {stats['without_embeddings']}")
    if stats['embedding_dim']:
        print(f"  Embedding dimension: {stats['embedding_dim']}")
    print()

    if args.check:
        conn.close()
        return

    if stats['without_embeddings'] == 0:
        print("All learnings already have embeddings!")
        conn.close()
        return

    if args.dry_run:
        print(f"DRY RUN: Would generate embeddings for {stats['without_embeddings']} blocks")
        conn.close()
        return

    # Load embedding model
    print(f"Loading embedding model: {args.model}")
    print("  (This may take a moment on first run...)")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(args.model)
    embedding_dim = model.get_sentence_embedding_dimension()
    print(f"  Model loaded! Dimension: {embedding_dim}")
    print()

    # Get learnings to process
    blocks = get_blocks_without_embeddings(conn, limit=args.limit)
    total = len(blocks)
    print(f"Processing {total} blocks...")
    print()

    # Process in batches
    processed = 0
    failed = 0
    start_time = datetime.now()

    for i in range(0, total, args.batch_size):
        batch = blocks[i:i + args.batch_size]

        # Generate texts
        texts = [generate_embedding_text(b) for b in batch]

        # Generate embeddings
        try:
            embeddings = model.encode(texts, show_progress_bar=False)

            # Prepare updates
            updates = [(batch[j]["id"], embeddings[j].tolist()) for j in range(len(batch))]

            # Update database
            batch_update_embeddings(conn, updates)

            processed += len(batch)

            # Progress
            pct = (processed / total) * 100
            elapsed = (datetime.now() - start_time).total_seconds()
            rate = processed / elapsed if elapsed > 0 else 0
            remaining = (total - processed) / rate if rate > 0 else 0

            print(f"  [{processed}/{total}] {pct:.1f}% complete | {rate:.1f} learnings/sec | ~{remaining:.0f}s remaining")

        except Exception as e:
            print(f"  Error processing batch: {e}")
            failed += len(batch)

    # Final stats
    elapsed = (datetime.now() - start_time).total_seconds()
    print()
    print("=" * 60)
    print("Migration Complete!")
    print("=" * 60)
    print(f"  Processed: {processed} blocks")
    print(f"  Failed: {failed} blocks")
    print(f"  Time: {elapsed:.1f} seconds")
    print(f"  Rate: {processed / elapsed:.1f} learnings/second")
    print()

    # Verify
    new_stats = get_embedding_stats(conn)
    print("New State:")
    print(f"  Total learnings: {new_stats['total_blocks']}")
    print(f"  With embeddings: {new_stats['with_embeddings']}")
    print(f"  Without embeddings: {new_stats['without_embeddings']}")
    print()

    if new_stats['without_embeddings'] == 0:
        print("All learnings now have embeddings!")

    conn.close()


if __name__ == "__main__":
    main()
