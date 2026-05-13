"""
Graph Builder for Architectural Awareness

Orchestrates the AST analyzers and builds a complete architecture graph
of the codebase. Integrates with knowledge_graph.py for categorization.
"""

import os
import sys
import json
import hashlib
import subprocess
import fcntl
from pathlib import Path
from typing import List, Dict, Optional, Set, Tuple
from datetime import datetime
from functools import lru_cache

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from memory.code_parser.models import (
    NodeType,
    EdgeType,
    ArchNode,
    ArchEdge,
    ArchGraph,
)
from memory.code_parser.ast_analyzer import PythonASTAnalyzer
from memory.code_parser.ts_analyzer import TypeScriptAnalyzer

# Import category keywords for categorization (no API needed)
try:
    from memory.knowledge_graph import CATEGORY_KEYWORDS
    CATEGORIZATION_AVAILABLE = True
except ImportError:
    CATEGORIZATION_AVAILABLE = False
    CATEGORY_KEYWORDS = {}

# Import Redis cache for architecture graph
try:
    from memory.redis_cache import (
        cache_architecture_graph,
        get_cached_architecture_graph,
    )
    REDIS_CACHE_AVAILABLE = True
except ImportError:
    REDIS_CACHE_AVAILABLE = False


def categorize_content(content: str) -> str:
    """
    Auto-categorize content based on keywords.

    Uses CATEGORY_KEYWORDS from knowledge_graph.py for keyword matching.
    This is a local function - no API connection needed.

    Args:
        content: Text content to categorize

    Returns:
        Category path (e.g., "Voice Pipeline/LLM/Async") or "General"
    """
    if not CATEGORY_KEYWORDS:
        return "General"

    content_lower = content.lower()

    # Score each category by keyword matches
    scores = {}
    for path, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            if keyword.lower() in content_lower:
                # Weight longer matches higher
                score += len(keyword.split())
        if score > 0:
            scores[path] = score

    if not scores:
        return "General"

    # Return highest scoring category
    return max(scores.items(), key=lambda x: x[1])[0]


class ArchitectureGraphBuilder:
    """
    Builds an architecture graph from a codebase by orchestrating
    language-specific analyzers.
    """

    # Default file patterns to include
    DEFAULT_INCLUDE = [
        "**/*.py",
        "**/*.ts",
        "**/*.tsx",
        "**/*.js",
        "**/*.jsx",
    ]

    # Default patterns to exclude
    DEFAULT_EXCLUDE = [
        "**/node_modules/**",
        "**/__pycache__/**",
        "**/.git/**",
        "**/build/**",
        "**/dist/**",
        "**/.venv/**",
        "**/venv/**",
        "**/.next/**",
        "**/coverage/**",
        "**/*.min.js",
        "**/*.test.*",
        "**/*.spec.*",
        "**/test_*.py",
        "**/*_test.py",
    ]

    # Cache location
    CACHE_FILE = ".architecture_graph_cache.json"

    def __init__(
        self,
        repo_root: str,
        include_patterns: List[str] = None,
        exclude_patterns: List[str] = None,
        cache_dir: str = None,
    ):
        """
        Initialize the graph builder.

        Args:
            repo_root: Root directory of the repository
            include_patterns: Glob patterns for files to include
            exclude_patterns: Glob patterns for files to exclude
            cache_dir: Directory to store cache (defaults to context-dna-data)
        """
        self.repo_root = Path(repo_root).resolve()
        self.include_patterns = include_patterns or self.DEFAULT_INCLUDE
        self.exclude_patterns = exclude_patterns or self.DEFAULT_EXCLUDE

        # Set up cache directory
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        else:
            # Use context-dna-data if it exists, otherwise repo root
            default_cache = self.repo_root / "context-dna-data"
            self.cache_dir = default_cache if default_cache.exists() else self.repo_root

        # Initialize analyzers
        self.python_analyzer = PythonASTAnalyzer(str(self.repo_root))
        self.ts_analyzer = TypeScriptAnalyzer(str(self.repo_root))

        # Categorization uses local keyword matching (no API needed)
        self._categorization_available = CATEGORIZATION_AVAILABLE

        # Graph cache
        self._graph_cache: Optional[ArchGraph] = None
        self._file_hashes: Dict[str, str] = {}

    def build_graph(
        self,
        force_rebuild: bool = False,
        specific_dirs: List[str] = None,
    ) -> ArchGraph:
        """
        Build the complete architecture graph.

        Args:
            force_rebuild: If True, ignore cache and rebuild from scratch
            specific_dirs: Only scan these directories (relative to repo root)

        Returns:
            ArchGraph containing all nodes and edges
        """
        # Try to load from cache if not forcing rebuild
        if not force_rebuild:
            cached = self._load_cache()
            if cached:
                # Check if any files have changed
                changed_files = self._detect_changed_files()
                if not changed_files:
                    return cached

                # Incremental update
                return self._incremental_update(cached, changed_files)

        # Full rebuild
        all_nodes: List[ArchNode] = []
        all_edges: List[ArchEdge] = []
        file_hashes: Dict[str, str] = {}

        # Find all files to analyze
        files = self._find_files(specific_dirs)

        print(f"Analyzing {len(files)} files...")

        for file_path in files:
            try:
                nodes, edges = self._analyze_file(file_path)
                all_nodes.extend(nodes)
                all_edges.extend(edges)

                # Store file hash
                file_hashes[str(file_path)] = self._compute_file_hash(file_path)
            except Exception as e:
                print(f"Error analyzing {file_path}: {e}")

        # Categorize nodes using knowledge graph
        self._categorize_nodes(all_nodes)

        # Get git version
        version = self._get_git_version()

        # Build graph
        graph = ArchGraph(
            nodes=all_nodes,
            edges=all_edges,
            version=version,
        )

        # Save cache
        self._save_cache(graph, file_hashes)
        self._graph_cache = graph
        self._file_hashes = file_hashes

        return graph

    def get_subgraph(
        self,
        center_node_id: str,
        depth: int = 2,
    ) -> Optional[ArchGraph]:
        """
        Get a subgraph centered on a specific node.

        Args:
            center_node_id: The node ID to center on
            depth: How many edges away to include

        Returns:
            Subgraph or None if center node not found
        """
        graph = self._graph_cache or self._load_cache()
        if not graph:
            graph = self.build_graph()

        return graph.get_subgraph(center_node_id, depth)

    def get_stats(self) -> Dict:
        """Get statistics about the architecture graph."""
        graph = self._graph_cache or self._load_cache()
        if not graph:
            graph = self.build_graph()

        return graph.stats

    def _find_files(self, specific_dirs: List[str] = None) -> List[Path]:
        """Find all files matching include patterns."""
        files: Set[Path] = set()

        search_dirs = [self.repo_root]
        if specific_dirs:
            search_dirs = [self.repo_root / d for d in specific_dirs]

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue

            for pattern in self.include_patterns:
                for file_path in search_dir.glob(pattern):
                    if file_path.is_file() and not self._is_excluded(file_path):
                        files.add(file_path)

        return sorted(files)

    def _is_excluded(self, file_path: Path) -> bool:
        """Check if a file should be excluded."""
        import fnmatch
        rel_path = str(file_path.relative_to(self.repo_root))
        path_parts = rel_path.split("/")

        for pattern in self.exclude_patterns:
            # Split pattern into segments and find the meaningful one
            # e.g., "**/.venv/**" → [".venv"], "**/node_modules/**" → ["node_modules"]
            segments = [s for s in pattern.split("/") if s and s != "**"]
            for seg in segments:
                if "*" in seg:
                    # Glob pattern in segment (e.g. ".venv-*", "*.min.js")
                    for part in path_parts:
                        if fnmatch.fnmatch(part, seg):
                            return True
                elif seg in path_parts:
                    return True

        return False

    def _analyze_file(self, file_path: Path) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Analyze a single file with the appropriate analyzer."""
        suffix = file_path.suffix.lower()

        if suffix == ".py":
            return self.python_analyzer.analyze_file(str(file_path))
        elif suffix in (".ts", ".tsx", ".js", ".jsx"):
            return self.ts_analyzer.analyze_file(str(file_path))

        return [], []

    def _categorize_nodes(self, nodes: List[ArchNode]) -> None:
        """Categorize nodes using CATEGORY_KEYWORDS from knowledge_graph.py."""
        for node in nodes:
            # Build content string for categorization
            content_parts = [node.name, node.file_path]
            if node.metadata.get("docstring"):
                content_parts.append(node.metadata["docstring"])

            content = " ".join(content_parts)

            # Use local categorization (keyword matching, no API needed)
            if self._categorization_available:
                category = categorize_content(content)
                # If only "General" returned, try path-based fallback
                if category == "General":
                    category = self._path_based_category(node.file_path)
                node.category = category
            else:
                # Fall back to path-based categorization
                node.category = self._path_based_category(node.file_path)

    def _path_based_category(self, file_path: str) -> str:
        """Simple path-based categorization as fallback."""
        path_lower = file_path.lower()

        if "memory" in path_lower or "context-dna" in path_lower:
            return "Memory_System"
        elif "admin" in path_lower or "dashboard" in path_lower:
            return "Frontend"
        elif "backend" in path_lower or "api" in path_lower:
            return "Backend"
        elif "infra" in path_lower or "terraform" in path_lower or "docker" in path_lower:
            return "Infrastructure"
        elif "voice" in path_lower or "stt" in path_lower or "tts" in path_lower:
            return "Voice_Pipeline"

        return "General"

    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute hash of file contents."""
        try:
            with open(file_path, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()
        except IOError:
            return ""

    def _detect_changed_files(self) -> List[Path]:
        """Detect which files have changed since last build.

        Strategy: git-first (fast, O(1) via git diff), hash fallback (slow, O(n) full scan).
        Git approach avoids reading all 1281 files just to compute hashes.
        """
        # Try git-first approach
        git_changed = self._detect_changed_files_git()
        if git_changed is not None:
            return git_changed

        # Fallback: full hash scan (original approach)
        if not self._file_hashes:
            cache = self._load_cache_metadata()
            if cache:
                self._file_hashes = cache.get("file_hashes", {})

        changed: List[Path] = []
        current_files = self._find_files()

        for file_path in current_files:
            str_path = str(file_path)
            current_hash = self._compute_file_hash(file_path)

            if str_path not in self._file_hashes:
                changed.append(file_path)  # New file
            elif self._file_hashes[str_path] != current_hash:
                changed.append(file_path)  # Modified file

        return changed

    def _detect_changed_files_git(self) -> Optional[List[Path]]:
        """Fast git-based change detection: O(1) via git diff.

        Compares current HEAD against the commit hash stored in cache.
        Returns None if git detection unavailable (triggers hash fallback).
        Returns empty list if no changes since last cached commit.
        """
        # Load cached commit hash
        cache = self._load_cache_metadata()
        if not cache:
            return None  # No cache = full rebuild needed

        last_commit = cache.get("last_commit_hash", "")
        if not last_commit:
            return None  # No stored commit = full rebuild needed

        try:
            # Get current HEAD
            head_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                capture_output=True, text=True, timeout=5,
            )
            if head_result.returncode != 0:
                return None

            current_commit = head_result.stdout.strip()
            if current_commit == last_commit:
                return []  # No changes

            # Get changed files between cached commit and HEAD
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", last_commit, current_commit],
                cwd=self.repo_root,
                capture_output=True, text=True, timeout=10,
            )
            if diff_result.returncode != 0:
                return None  # git diff failed (maybe force-pushed?) → fallback

            # Filter to only files we care about
            include_exts = set()
            for pattern in self.include_patterns:
                if pattern.startswith("**/*."):
                    include_exts.add(pattern.replace("**/*", ""))

            changed = []
            for line in diff_result.stdout.strip().split("\n"):
                if not line:
                    continue
                file_path = self.repo_root / line
                if not file_path.exists():
                    continue  # Deleted file
                # Check against include patterns
                if include_exts:
                    if not any(line.endswith(ext) for ext in include_exts):
                        continue
                # Check against exclude patterns
                excluded = False
                for exc in self.exclude_patterns:
                    exc_dir = exc.replace("**", "").strip("/")
                    if exc_dir and exc_dir in line:
                        excluded = True
                        break
                if not excluded:
                    changed.append(file_path)

            return changed

        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            return None  # Git unavailable → fallback to hash scan

    def _incremental_update(
        self, cached_graph: ArchGraph, changed_files: List[Path]
    ) -> ArchGraph:
        """Update graph incrementally for changed files only."""
        # Remove old nodes/edges for changed files
        changed_paths = {str(f.relative_to(self.repo_root)) for f in changed_files}

        new_nodes = [n for n in cached_graph.nodes if n.file_path not in changed_paths]
        new_edges = []

        # Keep edges that don't involve removed nodes
        removed_node_ids = {
            n.id for n in cached_graph.nodes if n.file_path in changed_paths
        }
        for edge in cached_graph.edges:
            if edge.source not in removed_node_ids and edge.target not in removed_node_ids:
                new_edges.append(edge)

        # Analyze changed files
        changed_node_ids = []
        for file_path in changed_files:
            try:
                nodes, edges = self._analyze_file(file_path)
                new_nodes.extend(nodes)
                new_edges.extend(edges)
                changed_node_ids.extend(n.id for n in nodes)

                # Update hash
                self._file_hashes[str(file_path)] = self._compute_file_hash(file_path)
            except Exception as e:
                print(f"Error updating {file_path}: {e}")

        # Categorize new nodes
        self._categorize_nodes([n for n in new_nodes if n.id in changed_node_ids])

        # Build updated graph
        graph = ArchGraph(
            nodes=new_nodes,
            edges=new_edges,
            version=self._get_git_version(),
            changed_nodes=changed_node_ids,
        )

        # Save cache
        self._save_cache(graph, self._file_hashes)
        self._graph_cache = graph

        return graph

    def _get_git_version(self) -> str:
        """Get current git commit hash (short, for display)."""
        full = self._get_git_version_full()
        return full[:8] if full else ""

    def _get_git_version_full(self) -> str:
        """Get full git commit hash (for cache comparison)."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            print(f"[WARN] Git hash retrieval failed: {e}")
        return ""

    def _load_cache(self) -> Optional[ArchGraph]:
        """
        Load cached graph using fallback chain:
        1. Try Redis (fast, concurrent-safe)
        2. Fall back to JSON file (with locking)

        Returns None if no valid cache exists.
        """
        # Try Redis first (fastest, concurrent-safe)
        if REDIS_CACHE_AVAILABLE:
            try:
                data = get_cached_architecture_graph()
                if data:
                    self._file_hashes = data.get("file_hashes", {})
                    return ArchGraph.from_dict(data.get("graph", {}))
            except Exception as e:
                print(f"Warning: Redis cache failed, falling back to JSON: {e}")

        # Fall back to JSON file (with file locking for safety)
        cache_path = self.cache_dir / self.CACHE_FILE
        if not cache_path.exists():
            return None

        try:
            with open(cache_path, "r") as f:
                # Acquire shared lock for reading (LOCK_SH)
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                    self._file_hashes = data.get("file_hashes", {})
                    return ArchGraph.from_dict(data.get("graph", {}))
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (IOError, json.JSONDecodeError) as e:
            print(f"Warning: Could not load cache: {e}")
            return None

    def _load_cache_metadata(self) -> Optional[Dict]:
        """
        Load just the metadata from cache (for hash checking).
        Uses same fallback chain as _load_cache: Redis → JSON.
        """
        # Try Redis first
        if REDIS_CACHE_AVAILABLE:
            try:
                data = get_cached_architecture_graph()
                if data:
                    return data
            except Exception:
                pass  # Fall through to JSON

        # Fall back to JSON file with shared lock
        cache_path = self.cache_dir / self.CACHE_FILE
        if not cache_path.exists():
            return None

        try:
            with open(cache_path, "r") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    return json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (IOError, json.JSONDecodeError):
            return None

    def _save_cache(self, graph: ArchGraph, file_hashes: Dict[str, str]) -> None:
        """
        Save graph using dual-write strategy:
        1. Write to Redis (fast, concurrent-safe, 5min TTL)
        2. Write to JSON file (persistent fallback, with exclusive lock)

        Both writes are best-effort; failure of one doesn't block the other.
        """
        cache_data = {
            "graph": graph.to_dict(),
            "file_hashes": file_hashes,
            "cached_at": datetime.now().isoformat(),
            "last_commit_hash": self._get_git_version_full(),
        }

        # Write to Redis (best effort, fast path)
        if REDIS_CACHE_AVAILABLE:
            try:
                cache_architecture_graph(cache_data)
            except Exception as e:
                print(f"Warning: Could not save to Redis cache: {e}")

        # Write to JSON file (persistent fallback, with exclusive lock)
        cache_path = self.cache_dir / self.CACHE_FILE
        try:
            with open(cache_path, "w") as f:
                # Acquire exclusive lock for writing (LOCK_EX)
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(cache_data, f, indent=2)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except IOError as e:
            print(f"Warning: Could not save to JSON cache: {e}")


def build_architecture_graph(
    repo_root: str = None,
    specific_dirs: List[str] = None,
    force_rebuild: bool = False,
) -> ArchGraph:
    """
    Convenience function to build an architecture graph.

    Args:
        repo_root: Root directory of the repository
        specific_dirs: Only scan these directories
        force_rebuild: Force complete rebuild

    Returns:
        ArchGraph containing all nodes and edges
    """
    repo_root = repo_root or str(Path.cwd())
    builder = ArchitectureGraphBuilder(repo_root)
    return builder.build_graph(
        force_rebuild=force_rebuild,
        specific_dirs=specific_dirs,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build architecture graph")
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parent.parent.parent),
        help="Repository root directory",
    )
    parser.add_argument(
        "--dirs",
        nargs="*",
        help="Specific directories to scan",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force rebuild (ignore cache)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )

    args = parser.parse_args()

    graph = build_architecture_graph(
        repo_root=args.repo,
        specific_dirs=args.dirs,
        force_rebuild=args.force,
    )

    if args.json:
        print(json.dumps(graph.to_dict(), indent=2))
    else:
        print(f"\n=== Architecture Graph ===")
        print(f"Nodes: {len(graph.nodes)}")
        print(f"Edges: {len(graph.edges)}")
        print(f"Version: {graph.version}")
        print(f"\nStats:")
        for key, value in graph.stats.items():
            print(f"  {key}: {value}")

        print(f"\nSample nodes:")
        for node in graph.nodes[:10]:
            print(f"  [{node.type.value}] {node.name} ({node.category})")
