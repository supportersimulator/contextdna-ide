"""
Markdown Memory Layer — Synaptic's Documentation Consciousness.

Local LLM maintains living summaries of all project .md files.
Atlas queries this layer instead of reading raw docs.
Alfred maintains the library index so Batman never has to browse the stacks.

Components:
  MarkdownScanner  — discovers .md files, respects focus mode, detects changes
  MarkdownDigester — sends changed files to LLM for summarization
  MarkdownIndex    — Redis-backed + in-memory index of summaries
  query_markdown_layer() — main entry point for consumers
  run_markdown_scan_cycle() — scheduler entry point
"""

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("context_dna.markdown_memory")

# ─── Constants ────────────────────────────────────────────────────────────────

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
FOCUS_FILE = os.path.join(REPO_ROOT, ".claude/focus.json")

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    ".next", "dist", "build", ".tox", "egg-info", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "htmlcov", ".coverage",
}

MAX_FILE_SIZE = 50_000       # 50KB — skip giant generated docs
MAX_CONTENT_FOR_LLM = 3000   # First 3K chars sent to LLM for summarization
MAX_DIGESTIONS_PER_CYCLE = 5  # Rate limit: avoid GPU contention with gold mining
REDIS_PREFIX = "contextdna:markdown:"
REDIS_TTL = 86400             # 24 hours — doc summaries are stable

SUMMARIZE_SYSTEM_PROMPT = (
    "Summarize this documentation file in 2-4 sentences. "
    "Extract: purpose, key concepts, gotchas, and any actionable constraints. "
    "Be extremely concise. Output plain text, no markdown formatting."
)

SECTION_SUMMARIZE_SYSTEM_PROMPT = (
    "Summarize this section of a documentation file in 1-2 sentences. "
    "Extract: purpose, key facts, and any actionable constraints. "
    "Be extremely concise. Output plain text, no markdown formatting."
)

MIN_SECTION_CHARS_FOR_LLM = 100  # Sections with <= this many chars skip LLM
MIN_HEADINGS_FOR_SECTIONS = 2     # Files need 2+ headings to warrant section digestion
MIN_FILE_SIZE_FOR_SECTIONS = 5000 # Files need >5000 bytes to warrant section digestion


# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class SectionSummary:
    path: str
    heading: str
    heading_level: int
    heading_slug: str
    summary: str
    content_hash: str
    start_line: int
    end_line: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SectionSummary":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DocSummary:
    path: str
    summary: str
    topics: List[str]
    content_hash: str
    file_mtime: float
    digested_at: float
    file_size: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DocSummary":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ScanResult:
    scanned: int = 0
    changed: int = 0
    digested: int = 0
    errors: int = 0
    skipped: int = 0
    elapsed_ms: int = 0


# ─── Focus Mode ──────────────────────────────────────────────────────────────

def _get_allowed_roots() -> List[str]:
    """Read focus.json and return absolute paths of allowed scan roots."""
    try:
        with open(FOCUS_FILE, "r") as f:
            focus = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # No focus file — scan everything under repo root
        return [REPO_ROOT]

    allowed_dirs = list(focus.get("core_always_active", []))
    allowed_dirs.extend(focus.get("active", []))

    # Convert to absolute paths
    roots = []
    for d in allowed_dirs:
        abs_path = os.path.join(REPO_ROOT, d)
        if os.path.isdir(abs_path):
            roots.append(abs_path)

    # Always include repo-root-level .md files (CLAUDE.md, README.md etc.)
    roots.append(REPO_ROOT)
    return roots


def _is_under_allowed_root(file_path: str, allowed_roots: List[str]) -> bool:
    """Check if file is within an allowed root directory."""
    for root in allowed_roots:
        if root == REPO_ROOT:
            # For repo root, only allow files directly in root (not subdirs)
            if os.path.dirname(file_path) == REPO_ROOT:
                return True
        elif file_path.startswith(root + "/") or file_path == root:
            return True
    return False


# ─── Scanner ─────────────────────────────────────────────────────────────────

class MarkdownScanner:
    """Discovers .md files respecting focus mode. Tracks changes via mtime+hash."""

    def __init__(self):
        self._known: Dict[str, Tuple[float, str]] = {}  # path → (mtime, hash)

    def scan(self) -> Tuple[List[str], List[str]]:
        """Scan for .md files. Returns (all_paths, changed_paths)."""
        allowed_roots = _get_allowed_roots()
        all_paths = []
        changed_paths = []

        # Scan each allowed root
        seen_dirs = set()
        for root in allowed_roots:
            if root in seen_dirs:
                continue
            seen_dirs.add(root)

            if root == REPO_ROOT:
                # Only scan root-level files, not subdirs
                for entry in os.scandir(root):
                    if entry.is_file() and entry.name.endswith(".md"):
                        self._check_file(entry.path, all_paths, changed_paths)
                continue

            for dirpath, dirnames, filenames in os.walk(root):
                # Prune skip dirs in-place
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]

                for fname in filenames:
                    if not fname.endswith(".md"):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    self._check_file(fpath, all_paths, changed_paths)

        return all_paths, changed_paths

    def _check_file(self, fpath: str, all_paths: List[str], changed_paths: List[str]):
        """Check a single file for changes."""
        try:
            stat = os.stat(fpath)
        except OSError:
            return

        if stat.st_size > MAX_FILE_SIZE or stat.st_size == 0:
            return

        all_paths.append(fpath)
        mtime = stat.st_mtime

        # Quick check: mtime unchanged → skip hash computation
        prev = self._known.get(fpath)
        if prev and prev[0] == mtime:
            return

        # Compute content hash (first 5KB for speed)
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                content_head = f.read(5000)
            content_hash = hashlib.md5(content_head.encode()).hexdigest()
        except (OSError, UnicodeDecodeError):
            return

        if prev and prev[1] == content_hash:
            # mtime changed but content didn't — update mtime only
            self._known[fpath] = (mtime, content_hash)
            return

        # Content actually changed (or new file)
        self._known[fpath] = (mtime, content_hash)
        changed_paths.append(fpath)


# ─── Digester ────────────────────────────────────────────────────────────────

class MarkdownDigester:
    """Sends .md files to local LLM for summarization."""

    def digest(self, file_path: str) -> Optional[DocSummary]:
        """Summarize a single .md file via local LLM. Returns None on failure."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(MAX_CONTENT_FOR_LLM)
            stat = os.stat(file_path)
        except OSError as e:
            logger.debug(f"Cannot read {file_path}: {e}")
            return None

        # Build prompt with file path context
        rel_path = file_path.replace(REPO_ROOT + "/", "")
        user_prompt = f"File: {rel_path}\n\n{content}"

        try:
            from memory.llm_priority_queue import butler_query
            response = butler_query(
                system_prompt=SUMMARIZE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                profile="summarize",
            )
        except Exception as e:
            logger.debug(f"LLM query failed for {rel_path}: {e}")
            return None

        if not response or len(response.strip()) < 10:
            logger.debug(f"LLM returned empty/short response for {rel_path}")
            return None

        summary = response.strip()

        # Extract topic keywords from summary (simple: unique words 4+ chars)
        words = set(re.findall(r"\b[a-z]{4,}\b", summary.lower()))
        # Also add path components as topics
        path_parts = set(Path(rel_path).parts[:-1])  # dirs, not filename
        topics = sorted(words | {p.lower() for p in path_parts if len(p) >= 3})

        content_hash = hashlib.md5(content.encode()).hexdigest()

        return DocSummary(
            path=file_path,
            summary=summary,
            topics=topics[:20],  # Cap at 20 topics
            content_hash=content_hash,
            file_mtime=stat.st_mtime,
            digested_at=time.time(),
            file_size=stat.st_size,
        )

    def digest_sections(self, file_path: str) -> List[SectionSummary]:
        """Chunk a file by headings and summarize each section.

        Sections >100 chars get LLM summaries. Sections <=100 chars use content as-is.
        Returns list of SectionSummary objects.
        """
        from memory.markdown_chunker import chunk, slugify

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            logger.debug(f"Cannot read {file_path}: {e}")
            return []

        sections = chunk(content)
        rel_path = file_path.replace(REPO_ROOT + "/", "")
        results: List[SectionSummary] = []

        for section in sections:
            heading_slug = slugify(section.heading)
            section_text = section.content.strip()
            content_hash = hashlib.md5(section_text.encode()).hexdigest()

            if len(section_text) <= MIN_SECTION_CHARS_FOR_LLM:
                # Short section — use content directly, no LLM needed
                summary = section_text
            else:
                # Send to LLM for summarization
                user_prompt = (
                    f"File: {rel_path}\n"
                    f"Section: {section.heading}\n\n"
                    f"{section_text[:MAX_CONTENT_FOR_LLM]}"
                )
                try:
                    from memory.llm_priority_queue import butler_query
                    response = butler_query(
                        system_prompt=SECTION_SUMMARIZE_SYSTEM_PROMPT,
                        user_prompt=user_prompt,
                        profile="summarize",
                    )
                except Exception as e:
                    logger.debug(f"LLM query failed for {rel_path}#{heading_slug}: {e}")
                    response = None

                if response and len(response.strip()) >= 10:
                    summary = response.strip()
                else:
                    # Fallback: use first 200 chars of section content
                    summary = section_text[:200]

            results.append(SectionSummary(
                path=file_path,
                heading=section.heading,
                heading_level=section.heading_level,
                heading_slug=heading_slug,
                summary=summary,
                content_hash=content_hash,
                start_line=section.start_line,
                end_line=section.end_line,
            ))

        return results


# ─── Index ───────────────────────────────────────────────────────────────────

class MarkdownIndex:
    """Redis-backed + in-memory index of document summaries."""

    def __init__(self):
        self._cache: Dict[str, DocSummary] = {}  # path → DocSummary
        self._section_cache: Dict[str, List[SectionSummary]] = {}  # path → [SectionSummary]
        self._warmed = False
        self._sections_warmed = False

    def _redis(self):
        """Get Redis client, or None if unavailable."""
        try:
            import redis
            return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        except Exception:
            return None

    def _redis_key(self, file_path: str) -> str:
        """Generate Redis key for a file path."""
        path_hash = hashlib.md5(file_path.encode()).hexdigest()[:12]
        return f"{REDIS_PREFIX}summary:{path_hash}"

    def warm_from_redis(self) -> int:
        """Load all cached summaries from Redis into memory. Returns count loaded."""
        if self._warmed:
            return len(self._cache)

        r = self._redis()
        if not r:
            self._warmed = True
            return 0

        loaded = 0
        try:
            paths_key = f"{REDIS_PREFIX}paths"
            all_paths = r.smembers(paths_key) or set()
            for path in all_paths:
                try:
                    raw = r.get(self._redis_key(path))
                    if raw:
                        data = json.loads(raw)
                        self._cache[path] = DocSummary.from_dict(data)
                        loaded += 1
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue
        except Exception as e:
            logger.debug(f"Redis warm failed: {e}")

        self._warmed = True
        logger.info(f"[markdown_memory] Warmed {loaded} summaries from Redis")
        return loaded

    def put(self, doc: DocSummary):
        """Store a summary in both memory and Redis."""
        self._cache[doc.path] = doc

        r = self._redis()
        if r:
            try:
                r.setex(self._redis_key(doc.path), REDIS_TTL, json.dumps(doc.to_dict()))
                r.sadd(f"{REDIS_PREFIX}paths", doc.path)
            except Exception as e:
                logger.debug(f"Redis write failed for {doc.path}: {e}")

    def get(self, file_path: str) -> Optional[DocSummary]:
        """Get summary for a specific file."""
        return self._cache.get(file_path)

    def query(self, text: str, top_k: int = 5, focus_filter: bool = True) -> List[dict]:
        """Query summaries by keyword relevance. Returns list of dicts sorted by score."""
        if not self._cache:
            return []

        # Tokenize query
        query_tokens = set(re.findall(r"\b[a-z]{3,}\b", text.lower()))
        if not query_tokens:
            return []

        # Optional focus filtering
        allowed_roots = _get_allowed_roots() if focus_filter else None

        scored = []
        for path, doc in self._cache.items():
            # Focus filter
            if allowed_roots and not _is_under_allowed_root(path, allowed_roots):
                continue

            # Score: keyword overlap with summary + topics + path
            rel_path = path.replace(REPO_ROOT + "/", "").lower()
            searchable = f"{doc.summary.lower()} {' '.join(doc.topics)} {rel_path}"
            searchable_tokens = set(re.findall(r"\b[a-z]{3,}\b", searchable))

            overlap = len(query_tokens & searchable_tokens)
            if overlap == 0:
                continue

            # Bonus for path match (file is in a relevant directory)
            path_bonus = sum(1 for t in query_tokens if t in rel_path) * 0.5

            # 4-folder trust weighting: dao > reflect > vision > inbox
            trust_bonus = 0.0
            if "/dao/" in rel_path:
                trust_bonus = 2.0
            elif "/reflect/" in rel_path:
                trust_bonus = 1.0
            elif "/inbox/" in rel_path:
                trust_bonus = -0.5

            score = overlap + path_bonus + trust_bonus
            scored.append((score, doc))

        # Sort by score descending
        scored.sort(key=lambda x: -x[0])

        return [
            {
                "path": doc.path,
                "rel_path": doc.path.replace(REPO_ROOT + "/", ""),
                "summary": doc.summary,
                "score": round(score, 2),
                "digested_at": doc.digested_at,
                "file_size": doc.file_size,
            }
            for score, doc in scored[:top_k]
        ]

    def stats(self) -> dict:
        """Return index statistics."""
        r = self._redis()
        redis_count = 0
        last_scan = None
        if r:
            try:
                redis_count = r.scard(f"{REDIS_PREFIX}paths") or 0
                last_scan = r.get(f"{REDIS_PREFIX}meta:last_scan")
            except Exception:
                pass

        return {
            "indexed_count": len(self._cache),
            "redis_count": redis_count,
            "last_scan_time": last_scan,
            "total_summary_chars": sum(len(d.summary) for d in self._cache.values()),
        }


# ─── Singleton ───────────────────────────────────────────────────────────────

class MarkdownMemoryLayer:
    """Orchestrates scanner, digester, and index."""

    def __init__(self):
        self.scanner = MarkdownScanner()
        self.digester = MarkdownDigester()
        self.index = MarkdownIndex()

    def run_cycle(self) -> dict:
        """Run one scan+digest cycle. Returns ScanResult as dict."""
        start = time.time()
        result = ScanResult()

        # Warm index from Redis if cold
        self.index.warm_from_redis()

        # Scan
        all_paths, changed_paths = self.scanner.scan()
        result.scanned = len(all_paths)
        result.changed = len(changed_paths)

        # Digest changed files (rate limited)
        digested = 0
        for fpath in changed_paths:
            if digested >= MAX_DIGESTIONS_PER_CYCLE:
                break

            doc = self.digester.digest(fpath)
            if doc:
                self.index.put(doc)
                digested += 1
            else:
                result.errors += 1

        result.digested = digested
        result.elapsed_ms = int((time.time() - start) * 1000)

        # Record scan time in Redis
        r = self.index._redis()
        if r:
            try:
                import datetime
                r.set(f"{REDIS_PREFIX}meta:last_scan",
                      datetime.datetime.now().isoformat())
                r.set(f"{REDIS_PREFIX}meta:last_result",
                      json.dumps(asdict(result)))
            except Exception:
                pass

        if result.digested > 0 or result.errors > 0:
            logger.info(
                f"[markdown_memory] scanned={result.scanned} changed={result.changed} "
                f"digested={result.digested} errors={result.errors} "
                f"elapsed={result.elapsed_ms}ms"
            )

        return asdict(result)


# ─── Singleton Access ────────────────────────────────────────────────────────

_instance: Optional[MarkdownMemoryLayer] = None
_instance_lock = __import__("threading").Lock()


def get_markdown_layer() -> MarkdownMemoryLayer:
    """Singleton access (matches get_sqlite_storage() convention)."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = MarkdownMemoryLayer()
    return _instance


# ─── Public API ──────────────────────────────────────────────────────────────

def query_markdown_layer(
    query: str,
    top_k: int = 5,
    focus_filter: bool = True,
) -> List[dict]:
    """Main entry point for Atlas/webhook to query doc summaries."""
    layer = get_markdown_layer()
    layer.index.warm_from_redis()
    return layer.index.query(query, top_k=top_k, focus_filter=focus_filter)


def run_markdown_scan_cycle() -> dict:
    """Scheduler entry point. Returns {scanned, changed, digested, errors, elapsed_ms}."""
    layer = get_markdown_layer()
    return layer.run_cycle()
