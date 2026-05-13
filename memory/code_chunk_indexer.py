#!/usr/bin/env python3
"""
Code Chunk Indexer — Semantic Search Over Actual Codebase

Parses Python files into function/class-level chunks with metadata,
stores them with FTS5 + sentence-transformer embeddings for semantic search.

Usage:
    from memory.code_chunk_indexer import index_project, search_code
    
    # Index the project
    index_project("/path/to/repo")
    
    # Search code semantically
    results = search_code("how does deployment work")
"""

import ast
import hashlib
import logging
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

CHUNKS_DB = Path(os.path.expanduser("~/.context-dna/.code_chunks.db"))


@dataclass
class CodeChunk:
    file_path: str
    chunk_type: str  # 'function', 'class', 'method', 'module_doc'
    name: str
    content: str
    start_line: int
    end_line: int
    git_sha: str = ""


def _ensure_db():
    """Create code chunks database with FTS5."""
    CHUNKS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CHUNKS_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS code_chunks (
            id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            chunk_type TEXT,
            name TEXT,
            content TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            git_sha TEXT,
            embedding BLOB,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS code_chunks_fts
        USING fts5(name, content, file_path, content=code_chunks, content_rowid=rowid)
    """)
    # Sync triggers
    conn.execute("""CREATE TRIGGER IF NOT EXISTS cc_ai AFTER INSERT ON code_chunks BEGIN
        INSERT INTO code_chunks_fts(rowid, name, content, file_path) VALUES (new.rowid, new.name, new.content, new.file_path);
    END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS cc_ad AFTER DELETE ON code_chunks BEGIN
        INSERT INTO code_chunks_fts(code_chunks_fts, rowid, name, content, file_path) VALUES('delete', old.rowid, old.name, old.content, old.file_path);
    END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS cc_au AFTER UPDATE ON code_chunks BEGIN
        INSERT INTO code_chunks_fts(code_chunks_fts, rowid, name, content, file_path) VALUES('delete', old.rowid, old.name, old.content, old.file_path);
        INSERT INTO code_chunks_fts(rowid, name, content, file_path) VALUES (new.rowid, new.name, new.content, new.file_path);
    END""")
    conn.commit()
    conn.close()


def _get_git_sha(repo_root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3, cwd=repo_root,
        )
        return result.stdout.strip()[:12] if result.returncode == 0 else ""
    except Exception:
        return ""


def _parse_python_file(file_path: str) -> List[CodeChunk]:
    """Parse a Python file into function/class chunks using AST."""
    chunks = []
    try:
        with open(file_path, 'r', errors='replace') as f:
            source = f.read()
        tree = ast.parse(source, filename=file_path)
        lines = source.split('\n')

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end_line = getattr(node, 'end_lineno', node.lineno + 10)
                content = '\n'.join(lines[node.lineno - 1:end_line])
                if len(content) > 50:  # Skip trivial functions
                    chunks.append(CodeChunk(
                        file_path=file_path,
                        chunk_type='function',
                        name=node.name,
                        content=content[:2000],  # Cap at 2000 chars
                        start_line=node.lineno,
                        end_line=end_line,
                    ))
            elif isinstance(node, ast.ClassDef):
                # Store class docstring + method signatures (not full body)
                end_line = getattr(node, 'end_lineno', node.lineno + 5)
                # Get just the class definition + docstring + method signatures
                class_lines = lines[node.lineno - 1:min(node.lineno + 20, end_line)]
                content = '\n'.join(class_lines)
                if len(content) > 30:
                    chunks.append(CodeChunk(
                        file_path=file_path,
                        chunk_type='class',
                        name=node.name,
                        content=content[:2000],
                        start_line=node.lineno,
                        end_line=min(node.lineno + 20, end_line),
                    ))
    except SyntaxError:
        pass  # Skip files with syntax errors
    except Exception as e:
        logger.debug(f"Failed to parse {file_path}: {e}")
    return chunks


def index_project(repo_root: str = None, file_patterns: List[str] = None) -> Dict[str, int]:
    """Index a project's Python files into searchable chunks.

    Args:
        repo_root: Path to repository root (auto-detects if None)
        file_patterns: Glob patterns to match (default: ['**/*.py'])

    Returns:
        Dict with 'files_scanned', 'chunks_indexed', 'chunks_skipped'
    """
    if repo_root is None:
        repo_root = str(Path(__file__).parent.parent)

    _ensure_db()
    git_sha = _get_git_sha(repo_root)

    patterns = file_patterns or ['**/*.py']
    root = Path(repo_root)

    # Collect Python files (skip venvs, submodules, node_modules, etc.)
    skip_dirs = {'.venv', 'venv', 'node_modules', '.git', '__pycache__',
                 'acontext', '.tox', 'dist', 'build', 'site-packages',
                 'context-dna', 'landing-page', 'egg-info'}
    # Also match any directory starting with these prefixes
    skip_prefixes = ('.venv', 'venv-', '.egg')
    py_files = []
    for pattern in patterns:
        for f in root.glob(pattern):
            parts = f.parts
            if any(skip in parts for skip in skip_dirs):
                continue
            if any(p.startswith(prefix) for p in parts for prefix in skip_prefixes):
                continue
            py_files.append(str(f))

    conn = sqlite3.connect(str(CHUNKS_DB))
    stats = {'files_scanned': 0, 'chunks_indexed': 0, 'chunks_skipped': 0}

    try:
        for file_path in py_files:
            stats['files_scanned'] += 1
            rel_path = str(Path(file_path).relative_to(root))
            chunks = _parse_python_file(file_path)

            for chunk in chunks:
                chunk_id = hashlib.sha256(
                    f"{rel_path}:{chunk.name}:{chunk.start_line}".encode()
                ).hexdigest()[:16]

                # Check if already indexed with same git_sha
                existing = conn.execute(
                    "SELECT git_sha FROM code_chunks WHERE id = ?", (chunk_id,)
                ).fetchone()
                if existing and existing[0] == git_sha:
                    stats['chunks_skipped'] += 1
                    continue

                conn.execute("""
                    INSERT OR REPLACE INTO code_chunks
                    (id, file_path, chunk_type, name, content, start_line, end_line, git_sha, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """, (chunk_id, rel_path, chunk.chunk_type, chunk.name,
                      chunk.content, chunk.start_line, chunk.end_line, git_sha))
                stats['chunks_indexed'] += 1

        conn.commit()
    finally:
        conn.close()

    return stats


def search_code(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Search code chunks using FTS5."""
    _ensure_db()
    conn = sqlite3.connect(str(CHUNKS_DB))
    results = []
    try:
        # FTS5 search
        words = [w for w in query.lower().split() if len(w) > 2][:6]
        if words:
            fts_query = " OR ".join(words)
            cursor = conn.execute("""
                SELECT c.id, c.file_path, c.chunk_type, c.name, c.content,
                       c.start_line, c.end_line
                FROM code_chunks c
                JOIN code_chunks_fts fts ON c.rowid = fts.rowid
                WHERE code_chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, limit))
            for row in cursor.fetchall():
                results.append({
                    'id': row[0], 'file_path': row[1], 'chunk_type': row[2],
                    'name': row[3], 'content': row[4][:500],
                    'start_line': row[5], 'end_line': row[6],
                    'source': 'code_chunk_fts5'
                })
    except Exception as e:
        logger.debug(f"Code search failed: {e}")
    finally:
        conn.close()
    return results


def prune_blocked_dirs(dry_run: bool = True) -> Dict[str, int]:
    """Remove chunks from directories that should have been excluded.

    Returns:
        Dict with 'patterns_checked', 'chunks_deleted' (0 if dry_run)
    """
    _ensure_db()
    conn = sqlite3.connect(str(CHUNKS_DB))
    # Same skip rules as index_project
    blocked_prefixes = [
        'context-dna/', '.venv', 'venv/', 'venv-', 'node_modules/',
        'acontext/', '.tox/', 'dist/', 'build/', 'landing-page/',
    ]
    blocked_contains = ['/site-packages/', '/__pycache__/', '/egg-info/']

    stats = {'patterns_checked': 0, 'chunks_deleted': 0, 'by_pattern': {}}
    try:
        for prefix in blocked_prefixes:
            stats['patterns_checked'] += 1
            count = conn.execute(
                "SELECT count(*) FROM code_chunks WHERE file_path LIKE ?",
                (f"{prefix}%",)
            ).fetchone()[0]
            if count > 0:
                stats['by_pattern'][f"prefix:{prefix}"] = count
                stats['chunks_deleted'] += count
                if not dry_run:
                    conn.execute(
                        "DELETE FROM code_chunks WHERE file_path LIKE ?",
                        (f"{prefix}%",)
                    )
        for pattern in blocked_contains:
            stats['patterns_checked'] += 1
            count = conn.execute(
                "SELECT count(*) FROM code_chunks WHERE file_path LIKE ?",
                (f"%{pattern}%",)
            ).fetchone()[0]
            if count > 0:
                stats['by_pattern'][f"contains:{pattern}"] = count
                stats['chunks_deleted'] += count
                if not dry_run:
                    conn.execute(
                        "DELETE FROM code_chunks WHERE file_path LIKE ?",
                        (f"%{pattern}%",)
                    )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return stats


def prune_stale_files(repo_root: str = None, dry_run: bool = True) -> Dict[str, int]:
    """Remove chunks whose source files no longer exist on disk.

    Returns:
        Dict with 'files_checked', 'stale_files', 'chunks_deleted'
    """
    if repo_root is None:
        repo_root = str(Path(__file__).parent.parent)
    root = Path(repo_root)

    _ensure_db()
    conn = sqlite3.connect(str(CHUNKS_DB))
    stats = {'files_checked': 0, 'stale_files': 0, 'chunks_deleted': 0}
    try:
        cursor = conn.execute(
            "SELECT DISTINCT file_path FROM code_chunks"
        )
        stale_paths = []
        for (rel_path,) in cursor.fetchall():
            stats['files_checked'] += 1
            full_path = root / rel_path
            if not full_path.exists():
                stale_paths.append(rel_path)
                stats['stale_files'] += 1

        for rel_path in stale_paths:
            count = conn.execute(
                "SELECT count(*) FROM code_chunks WHERE file_path = ?",
                (rel_path,)
            ).fetchone()[0]
            stats['chunks_deleted'] += count
            if not dry_run:
                conn.execute(
                    "DELETE FROM code_chunks WHERE file_path = ?",
                    (rel_path,)
                )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return stats


def vacuum_db():
    """Reclaim disk space after pruning."""
    conn = sqlite3.connect(str(CHUNKS_DB))
    try:
        # Rebuild FTS index to remove orphaned entries
        conn.execute("INSERT INTO code_chunks_fts(code_chunks_fts) VALUES('rebuild')")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return os.path.getsize(str(CHUNKS_DB))


# CLI support
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "index":
        repo = sys.argv[2] if len(sys.argv) > 2 else None
        stats = index_project(repo)
        print(f"Indexed: {stats['files_scanned']} files, "
              f"{stats['chunks_indexed']} chunks "
              f"({stats['chunks_skipped']} skipped)")
    elif len(sys.argv) > 1 and sys.argv[1] == "search":
        query = " ".join(sys.argv[2:])
        results = search_code(query)
        for r in results:
            print(f"  [{r['chunk_type']}] {r['name']} in "
                  f"{r['file_path']}:{r['start_line']}")
            print(f"    {r['content'][:100]}...")
    elif len(sys.argv) > 1 and sys.argv[1] == "prune":
        dry = "--execute" not in sys.argv
        print(f"{'DRY RUN' if dry else 'EXECUTING'} — blocked dir prune:")
        s1 = prune_blocked_dirs(dry_run=dry)
        for pat, cnt in sorted(s1['by_pattern'].items(), key=lambda x: -x[1]):
            print(f"  {pat}: {cnt:,} chunks")
        print(f"  Total: {s1['chunks_deleted']:,} chunks")

        repo = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith('-') else None
        print(f"\n{'DRY RUN' if dry else 'EXECUTING'} — stale file prune:")
        s2 = prune_stale_files(repo_root=repo, dry_run=dry)
        print(f"  Checked: {s2['files_checked']:,} files, "
              f"Stale: {s2['stale_files']:,}, "
              f"Chunks: {s2['chunks_deleted']:,}")

        if not dry:
            print("\nVACUUMing...")
            size = vacuum_db()
            print(f"DB size after VACUUM: {size / 1024 / 1024:.1f}MB")
        else:
            size = os.path.getsize(str(CHUNKS_DB))
            print(f"\nCurrent DB size: {size / 1024 / 1024:.1f}MB")
            print("Run with --execute to apply changes")
    else:
        print("Usage: python code_chunk_indexer.py index [repo_path]")
        print("       python code_chunk_indexer.py search <query>")
        print("       python code_chunk_indexer.py prune [repo_path] [--execute]")
