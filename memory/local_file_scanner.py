#!/usr/bin/env python3
"""
Synaptic's Local File Scanner - Know What's on the Computer

A lightweight weekly scanner that indexes file names and paths for universal
organization awareness. Runs overnight (3am Sunday) when user is least likely
to be active.

Philosophy:
- Lightweight: Only file names, paths, sizes, extensions
- Non-invasive: Never reads file contents
- Helpful: Category tags for quick retrieval
- Space-efficient: ~1KB per 100 files in SQLite

Storage: ~/.context-dna/local_files.db (~1-5MB for typical computer)
Schedule: Weekly (Sunday 3am) via Celery beat

Usage:
    # Manual scan (foreground)
    python memory/local_file_scanner.py scan

    # Quick scan (just home directory)
    python memory/local_file_scanner.py quick

    # Query files
    python memory/local_file_scanner.py find "*.py"
    python memory/local_file_scanner.py category code

    # Stats
    python memory/local_file_scanner.py stats
"""

import os
import sys
import sqlite3
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass
from enum import Enum
import json
import fnmatch

logger = logging.getLogger(__name__)


# =============================================================================
# FILE CATEGORIES - Universal Organization
# =============================================================================

class FileCategory(str, Enum):
    """Universal file categories for adaptive organization."""
    CODE = "code"           # Source code, scripts
    DOCS = "docs"           # Documents, PDFs, text
    MEDIA = "media"         # Images, video, audio
    DATA = "data"           # Datasets, databases, CSVs
    CONFIG = "config"       # Configuration files
    PROJECT = "project"     # Project directories
    ARCHIVE = "archive"     # Compressed files
    SYSTEM = "system"       # System/hidden files
    CACHE = "cache"         # Cache/temp files
    OTHER = "other"         # Uncategorized


# Extension to category mapping
EXTENSION_CATEGORIES: Dict[str, FileCategory] = {
    # Code
    ".py": FileCategory.CODE,
    ".js": FileCategory.CODE,
    ".ts": FileCategory.CODE,
    ".tsx": FileCategory.CODE,
    ".jsx": FileCategory.CODE,
    ".java": FileCategory.CODE,
    ".c": FileCategory.CODE,
    ".cpp": FileCategory.CODE,
    ".h": FileCategory.CODE,
    ".go": FileCategory.CODE,
    ".rs": FileCategory.CODE,
    ".rb": FileCategory.CODE,
    ".php": FileCategory.CODE,
    ".swift": FileCategory.CODE,
    ".kt": FileCategory.CODE,
    ".scala": FileCategory.CODE,
    ".sh": FileCategory.CODE,
    ".bash": FileCategory.CODE,
    ".zsh": FileCategory.CODE,
    ".ps1": FileCategory.CODE,
    ".sql": FileCategory.CODE,
    ".r": FileCategory.CODE,
    ".m": FileCategory.CODE,
    ".pl": FileCategory.CODE,
    ".lua": FileCategory.CODE,

    # Docs
    ".md": FileCategory.DOCS,
    ".txt": FileCategory.DOCS,
    ".pdf": FileCategory.DOCS,
    ".doc": FileCategory.DOCS,
    ".docx": FileCategory.DOCS,
    ".rtf": FileCategory.DOCS,
    ".odt": FileCategory.DOCS,
    ".xls": FileCategory.DOCS,
    ".xlsx": FileCategory.DOCS,
    ".ppt": FileCategory.DOCS,
    ".pptx": FileCategory.DOCS,
    ".pages": FileCategory.DOCS,
    ".numbers": FileCategory.DOCS,
    ".keynote": FileCategory.DOCS,

    # Media
    ".jpg": FileCategory.MEDIA,
    ".jpeg": FileCategory.MEDIA,
    ".png": FileCategory.MEDIA,
    ".gif": FileCategory.MEDIA,
    ".svg": FileCategory.MEDIA,
    ".webp": FileCategory.MEDIA,
    ".ico": FileCategory.MEDIA,
    ".mp3": FileCategory.MEDIA,
    ".wav": FileCategory.MEDIA,
    ".flac": FileCategory.MEDIA,
    ".aac": FileCategory.MEDIA,
    ".ogg": FileCategory.MEDIA,
    ".mp4": FileCategory.MEDIA,
    ".mov": FileCategory.MEDIA,
    ".avi": FileCategory.MEDIA,
    ".mkv": FileCategory.MEDIA,
    ".webm": FileCategory.MEDIA,
    ".m4a": FileCategory.MEDIA,
    ".m4v": FileCategory.MEDIA,

    # Data
    ".json": FileCategory.DATA,
    ".csv": FileCategory.DATA,
    ".xml": FileCategory.DATA,
    ".yaml": FileCategory.DATA,
    ".yml": FileCategory.DATA,
    ".toml": FileCategory.DATA,
    ".db": FileCategory.DATA,
    ".sqlite": FileCategory.DATA,
    ".sqlite3": FileCategory.DATA,
    ".parquet": FileCategory.DATA,
    ".pickle": FileCategory.DATA,
    ".pkl": FileCategory.DATA,
    ".npy": FileCategory.DATA,
    ".npz": FileCategory.DATA,
    ".h5": FileCategory.DATA,
    ".hdf5": FileCategory.DATA,

    # Config
    ".env": FileCategory.CONFIG,
    ".ini": FileCategory.CONFIG,
    ".conf": FileCategory.CONFIG,
    ".cfg": FileCategory.CONFIG,
    ".properties": FileCategory.CONFIG,
    ".plist": FileCategory.CONFIG,

    # Archive
    ".zip": FileCategory.ARCHIVE,
    ".tar": FileCategory.ARCHIVE,
    ".gz": FileCategory.ARCHIVE,
    ".bz2": FileCategory.ARCHIVE,
    ".xz": FileCategory.ARCHIVE,
    ".7z": FileCategory.ARCHIVE,
    ".rar": FileCategory.ARCHIVE,
    ".dmg": FileCategory.ARCHIVE,
    ".iso": FileCategory.ARCHIVE,

    # Cache/Temp
    ".tmp": FileCategory.CACHE,
    ".temp": FileCategory.CACHE,
    ".swp": FileCategory.CACHE,
    ".swo": FileCategory.CACHE,
    ".pyc": FileCategory.CACHE,
    ".pyo": FileCategory.CACHE,
    ".class": FileCategory.CACHE,
    ".o": FileCategory.CACHE,
}

# Directories to skip entirely (performance + privacy)
SKIP_DIRECTORIES: Set[str] = {
    # System
    ".Trash",
    ".Spotlight-V100",
    ".fseventsd",
    ".DocumentRevisions-V100",
    ".TemporaryItems",
    "System",
    "private",

    # Dev caches
    "node_modules",
    "__pycache__",
    ".git",
    ".svn",
    ".hg",
    "venv",
    ".venv",
    "env",
    ".env",
    ".tox",
    ".nox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".build",
    "target",
    ".gradle",
    ".m2",
    ".cargo",

    # App caches
    "Library",  # macOS Library (massive)
    "Caches",
    "CachedData",
    "Cache",
    "DerivedData",
    "xcuserdata",

    # Cloud sync internals
    ".dropbox.cache",
    ".icloud",

    # ProjectDNA vault (self-referential metadata)
    ".projectdna",

    # Large binary directories
    ".ollama",  # LLM models (huge)
    ".cache",   # Various caches
    "huggingface",  # ML models
}

# Directories that indicate a project root
PROJECT_INDICATORS: Set[str] = {
    ".git",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Makefile",
    "CMakeLists.txt",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    ".project",
    "*.sln",
    "*.xcodeproj",
    "*.xcworkspace",
}


@dataclass
class FileEntry:
    """A scanned file entry."""
    path: str
    name: str
    extension: str
    category: FileCategory
    size_bytes: int
    modified_at: datetime
    parent_project: Optional[str]  # Project root if inside a project


@dataclass
class ScanStats:
    """Statistics from a scan."""
    total_files: int
    total_size_bytes: int
    by_category: Dict[str, int]
    by_extension: Dict[str, int]
    projects_found: int
    scan_duration_seconds: float
    scanned_at: datetime


class LocalFileScanner:
    """
    Synaptic's eyes on the local filesystem.

    Lightweight weekly scanner that builds an index of what's on the computer
    for adaptive organization and quick file retrieval.
    """

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize scanner with SQLite storage."""
        if db_path is None:
            config_dir = Path.home() / ".context-dna"
            config_dir.mkdir(exist_ok=True)
            db_path = config_dir / "local_files.db"

        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database with performance optimizations."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
            conn.execute("PRAGMA mmap_size=268435456")  # 256MB mmap

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY,
                    path TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    extension TEXT,
                    category TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    modified_at TEXT NOT NULL,
                    parent_project TEXT,
                    scanned_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY,
                    path TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    indicator TEXT,  -- What made us detect it as a project
                    file_count INTEGER DEFAULT 0,
                    total_size_bytes INTEGER DEFAULT 0,
                    scanned_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scan_history (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    total_files INTEGER,
                    total_size_bytes INTEGER,
                    projects_found INTEGER,
                    status TEXT DEFAULT 'running'
                );

                -- Indexes for fast queries
                CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
                CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension);
                CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
                CREATE INDEX IF NOT EXISTS idx_files_project ON files(parent_project);
                CREATE INDEX IF NOT EXISTS idx_files_scanned ON files(scanned_at);
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error initializing database: {e}")

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            return conn
        except Exception as e:
            logger.error(f"Error getting connection: {e}")
            return None

    def _categorize_file(self, path: Path) -> FileCategory:
        """Determine file category from extension and path."""
        ext = path.suffix.lower()

        # Check extension mapping
        if ext in EXTENSION_CATEGORIES:
            return EXTENSION_CATEGORIES[ext]

        # Check for hidden/system files
        if path.name.startswith("."):
            return FileCategory.SYSTEM

        # Check for config patterns
        if path.name in {"Dockerfile", "Makefile", "Vagrantfile", "Jenkinsfile"}:
            return FileCategory.CONFIG

        return FileCategory.OTHER

    def _is_project_root(self, path: Path) -> Optional[str]:
        """Check if directory is a project root, return indicator if so."""
        if not path.is_dir():
            return None

        for indicator in PROJECT_INDICATORS:
            if "*" in indicator:
                # Glob pattern
                pattern = indicator
                if any(path.glob(pattern)):
                    return indicator
            else:
                # Exact file/dir name
                if (path / indicator).exists():
                    return indicator

        return None

    def _should_skip(self, path: Path) -> bool:
        """Check if path should be skipped."""
        name = path.name

        # Skip hidden files/dirs (except important configs)
        if name.startswith(".") and name not in {".env", ".gitignore", ".dockerignore"}:
            if name in SKIP_DIRECTORIES:
                return True

        # Skip known heavy directories
        if name in SKIP_DIRECTORIES:
            return True

        return False

    def scan(
        self,
        root_paths: Optional[List[Path]] = None,
        max_depth: int = 10,
        progress_callback=None
    ) -> ScanStats:
        """
        Perform full scan of specified directories.

        Args:
            root_paths: Directories to scan. Defaults to home + common locations.
            max_depth: Maximum directory depth to traverse.
            progress_callback: Optional callback(current_count, current_path)

        Returns:
            ScanStats with scan results.
        """
        if root_paths is None:
            home = Path.home()
            root_paths = [
                home / "Documents",
                home / "Desktop",
                home / "Downloads",
                home / "Projects",
                home / "Development",
                home / "Code",
                home / "Work",
                home / "Personal",
            ]
            # Filter to existing directories
            root_paths = [p for p in root_paths if p.exists()]

        start_time = datetime.now()
        conn = self._get_conn()

        # Record scan start
        cursor = conn.execute(
            "INSERT INTO scan_history (started_at, status) VALUES (?, 'running')",
            (start_time.isoformat(),)
        )
        scan_id = cursor.lastrowid
        conn.commit()

        # Clear old entries (we rebuild fresh each scan)
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM projects")
        conn.commit()

        stats = {
            "total_files": 0,
            "total_size": 0,
            "by_category": {},
            "by_extension": {},
            "projects": [],
        }

        try:
            for root_path in root_paths:
                self._scan_directory(
                    conn,
                    root_path,
                    stats,
                    current_depth=0,
                    max_depth=max_depth,
                    parent_project=None,
                    progress_callback=progress_callback
                )

            # Commit final batch
            conn.commit()

            # Update scan record
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            conn.execute("""
                UPDATE scan_history
                SET completed_at = ?, total_files = ?, total_size_bytes = ?,
                    projects_found = ?, status = 'completed'
                WHERE id = ?
            """, (
                end_time.isoformat(),
                stats["total_files"],
                stats["total_size"],
                len(stats["projects"]),
                scan_id
            ))
            conn.commit()

            return ScanStats(
                total_files=stats["total_files"],
                total_size_bytes=stats["total_size"],
                by_category=stats["by_category"],
                by_extension=stats["by_extension"],
                projects_found=len(stats["projects"]),
                scan_duration_seconds=duration,
                scanned_at=start_time
            )

        except Exception as e:
            # Mark scan as failed
            conn.execute(
                "UPDATE scan_history SET status = 'failed' WHERE id = ?",
                (scan_id,)
            )
            conn.commit()
            raise

        finally:
            conn.close()

    def _scan_directory(
        self,
        conn: sqlite3.Connection,
        path: Path,
        stats: dict,
        current_depth: int,
        max_depth: int,
        parent_project: Optional[str],
        progress_callback=None
    ):
        """Recursively scan a directory."""
        if current_depth > max_depth:
            return

        if not path.exists() or not path.is_dir():
            return

        if self._should_skip(path):
            return

        # Check if this is a project root
        indicator = self._is_project_root(path)
        if indicator:
            parent_project = str(path)
            stats["projects"].append(path)
            conn.execute("""
                INSERT OR REPLACE INTO projects (path, name, indicator, scanned_at)
                VALUES (?, ?, ?, ?)
            """, (str(path), path.name, indicator, datetime.now().isoformat()))

        try:
            entries = list(path.iterdir())
        except PermissionError:
            return
        except OSError:
            return

        batch = []

        for entry in entries:
            try:
                if entry.is_dir():
                    self._scan_directory(
                        conn, entry, stats,
                        current_depth + 1, max_depth,
                        parent_project, progress_callback
                    )
                elif entry.is_file():
                    try:
                        stat = entry.stat()
                    except (PermissionError, OSError):
                        continue

                    category = self._categorize_file(entry)
                    ext = entry.suffix.lower() or "(none)"

                    batch.append((
                        str(entry),
                        entry.name,
                        ext,
                        category.value,
                        stat.st_size,
                        datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        parent_project,
                        datetime.now().isoformat()
                    ))

                    stats["total_files"] += 1
                    stats["total_size"] += stat.st_size
                    stats["by_category"][category.value] = stats["by_category"].get(category.value, 0) + 1
                    stats["by_extension"][ext] = stats["by_extension"].get(ext, 0) + 1

                    if progress_callback and stats["total_files"] % 1000 == 0:
                        progress_callback(stats["total_files"], str(entry))

                    # Batch insert every 500 files
                    if len(batch) >= 500:
                        conn.executemany("""
                            INSERT OR REPLACE INTO files
                            (path, name, extension, category, size_bytes, modified_at, parent_project, scanned_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, batch)
                        conn.commit()
                        batch = []

            except (PermissionError, OSError):
                continue

        # Insert remaining batch
        if batch:
            conn.executemany("""
                INSERT OR REPLACE INTO files
                (path, name, extension, category, size_bytes, modified_at, parent_project, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, batch)

    def quick_scan(self, progress_callback=None) -> ScanStats:
        """Quick scan of just the home directory (shallow)."""
        home = Path.home()
        return self.scan(
            root_paths=[home],
            max_depth=3,  # Shallow
            progress_callback=progress_callback
        )

    def find_files(
        self,
        pattern: str,
        category: Optional[FileCategory] = None,
        limit: int = 100
    ) -> List[Dict]:
        """
        Find files matching pattern.

        Args:
            pattern: Glob pattern (e.g., "*.py", "*test*")
            category: Optional category filter
            limit: Max results

        Returns:
            List of matching file records.
        """
        conn = self._get_conn()

        # Convert glob to SQL LIKE
        sql_pattern = pattern.replace("*", "%").replace("?", "_")

        query = "SELECT * FROM files WHERE name LIKE ?"
        params = [sql_pattern]

        if category:
            query += " AND category = ?"
            params.append(category.value)

        query += " ORDER BY modified_at DESC LIMIT ?"
        params.append(limit)

        results = conn.execute(query, params).fetchall()
        conn.close()

        return [dict(r) for r in results]

    def find_by_category(
        self,
        category: FileCategory,
        limit: int = 100
    ) -> List[Dict]:
        """Find files by category."""
        conn = self._get_conn()
        results = conn.execute("""
            SELECT * FROM files
            WHERE category = ?
            ORDER BY modified_at DESC
            LIMIT ?
        """, (category.value, limit)).fetchall()
        conn.close()
        return [dict(r) for r in results]

    def get_projects(self) -> List[Dict]:
        """Get all detected projects."""
        conn = self._get_conn()

        # Update project stats
        conn.execute("""
            UPDATE projects SET
                file_count = (SELECT COUNT(*) FROM files WHERE parent_project = projects.path),
                total_size_bytes = (SELECT COALESCE(SUM(size_bytes), 0) FROM files WHERE parent_project = projects.path)
        """)
        conn.commit()

        results = conn.execute("""
            SELECT * FROM projects
            ORDER BY total_size_bytes DESC
        """).fetchall()
        conn.close()
        return [dict(r) for r in results]

    def get_stats(self) -> Dict:
        """Get current index statistics."""
        conn = self._get_conn()

        total = conn.execute("SELECT COUNT(*), SUM(size_bytes) FROM files").fetchone()
        categories = conn.execute("""
            SELECT category, COUNT(*), SUM(size_bytes)
            FROM files GROUP BY category
        """).fetchall()
        extensions = conn.execute("""
            SELECT extension, COUNT(*)
            FROM files GROUP BY extension
            ORDER BY COUNT(*) DESC LIMIT 20
        """).fetchall()
        projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()
        last_scan = conn.execute("""
            SELECT * FROM scan_history
            ORDER BY started_at DESC LIMIT 1
        """).fetchone()

        conn.close()

        return {
            "total_files": total[0] or 0,
            "total_size_bytes": total[1] or 0,
            "total_size_human": self._format_size(total[1] or 0),
            "by_category": {r[0]: {"count": r[1], "size": r[2]} for r in categories},
            "top_extensions": {r[0]: r[1] for r in extensions},
            "projects_count": projects[0],
            "last_scan": dict(last_scan) if last_scan else None,
            "db_size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }

    def _format_size(self, size_bytes: int) -> str:
        """Format bytes as human-readable string."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if abs(size_bytes) < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} PB"

    def get_recent_files(self, days: int = 7, limit: int = 50) -> List[Dict]:
        """Get recently modified files."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        results = conn.execute("""
            SELECT * FROM files
            WHERE modified_at >= ?
            ORDER BY modified_at DESC
            LIMIT ?
        """, (cutoff, limit)).fetchall()
        conn.close()
        return [dict(r) for r in results]


# =============================================================================
# CLI Interface
# =============================================================================

def _progress(count: int, path: str):
    """Progress callback for CLI."""
    print(f"\r  Scanned {count:,} files... {path[:60]:<60}", end="", flush=True)


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Synaptic's Local File Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python local_file_scanner.py scan      # Full weekly scan
  python local_file_scanner.py quick     # Quick shallow scan
  python local_file_scanner.py find "*.py"
  python local_file_scanner.py category code
  python local_file_scanner.py stats
  python local_file_scanner.py projects
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Full weekly scan")
    scan_parser.add_argument("--depth", type=int, default=10, help="Max depth")

    # Quick scan
    subparsers.add_parser("quick", help="Quick shallow scan")

    # Find command
    find_parser = subparsers.add_parser("find", help="Find files by pattern")
    find_parser.add_argument("pattern", help="Glob pattern (e.g., *.py)")
    find_parser.add_argument("--category", help="Filter by category")
    find_parser.add_argument("--limit", type=int, default=50)

    # Category command
    cat_parser = subparsers.add_parser("category", help="List files by category")
    cat_parser.add_argument("category", choices=[c.value for c in FileCategory])
    cat_parser.add_argument("--limit", type=int, default=50)

    # Stats command
    subparsers.add_parser("stats", help="Show index statistics")

    # Projects command
    subparsers.add_parser("projects", help="List detected projects")

    # Recent command
    recent_parser = subparsers.add_parser("recent", help="Show recently modified files")
    recent_parser.add_argument("--days", type=int, default=7)
    recent_parser.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()
    scanner = LocalFileScanner()

    if args.command == "scan":
        print("🔍 Starting full scan...")
        stats = scanner.scan(max_depth=args.depth, progress_callback=_progress)
        print(f"\n\n✅ Scan complete!")
        print(f"   Files: {stats.total_files:,}")
        print(f"   Size: {scanner._format_size(stats.total_size_bytes)}")
        print(f"   Projects: {stats.projects_found}")
        print(f"   Duration: {stats.scan_duration_seconds:.1f}s")

    elif args.command == "quick":
        print("⚡ Starting quick scan...")
        stats = scanner.quick_scan(progress_callback=_progress)
        print(f"\n\n✅ Quick scan complete!")
        print(f"   Files: {stats.total_files:,}")
        print(f"   Projects: {stats.projects_found}")

    elif args.command == "find":
        results = scanner.find_files(
            args.pattern,
            category=FileCategory(args.category) if args.category else None,
            limit=args.limit
        )
        print(f"Found {len(results)} files matching '{args.pattern}':\n")
        for f in results:
            print(f"  [{f['category']:8}] {f['path']}")

    elif args.command == "category":
        results = scanner.find_by_category(FileCategory(args.category), limit=args.limit)
        print(f"Files in category '{args.category}' ({len(results)} shown):\n")
        for f in results:
            print(f"  {f['name']:<40} {scanner._format_size(f['size_bytes']):>10}")

    elif args.command == "stats":
        stats = scanner.get_stats()
        print("📊 Local File Index Statistics\n")
        print(f"  Total Files: {stats['total_files']:,}")
        print(f"  Total Size:  {stats['total_size_human']}")
        print(f"  Projects:    {stats['projects_count']}")
        print(f"  DB Size:     {scanner._format_size(stats['db_size_bytes'])}")
        print("\n  By Category:")
        for cat, data in sorted(stats['by_category'].items()):
            print(f"    {cat:12} {data['count']:>8,} files  ({scanner._format_size(data['size'])})")
        if stats['last_scan']:
            print(f"\n  Last Scan:   {stats['last_scan']['started_at']}")
            print(f"  Status:      {stats['last_scan']['status']}")

    elif args.command == "projects":
        projects = scanner.get_projects()
        print(f"🗂️ Detected Projects ({len(projects)}):\n")
        for p in projects:
            print(f"  {p['name']:<30} {p['file_count']:>6} files  {scanner._format_size(p['total_size_bytes']):>10}")
            print(f"    {p['path']}")

    elif args.command == "recent":
        results = scanner.get_recent_files(days=args.days, limit=args.limit)
        print(f"📅 Files modified in last {args.days} days ({len(results)} shown):\n")
        for f in results:
            print(f"  {f['modified_at'][:10]} [{f['category']:8}] {f['name']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
