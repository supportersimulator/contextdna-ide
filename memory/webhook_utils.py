#!/usr/bin/env python3
"""
WEBHOOK UTILS — Shared utilities for webhook injection system.

Extracted from persistent_hook_structure.py (God File Phase 1).

Functions:
- _safe_log() — Defensive logging with stderr fallback
- is_redis_available() / reset_redis_check() — Redis availability
- Cache infrastructure: _cache_store, get_cached(), invalidate_cache(), etc.
- _sanitize_prompt_for_query() — Clean IDE noise from prompts
- _extract_concern_keywords() — Extract keywords from concerns
- _infer_changed_files() — Infer files from prompt context
- get_professor_wisdom_dicts() — Lazy-load professor wisdom
- detect_domain_from_prompt() — Domain detection from prompt
- get_domain_specific_wisdom() — Domain-specific wisdom lookup
- _make_cache_key() — Redis cache key generation
"""

import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Base paths (match persistent_hook_structure.py)
PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PROJECT_ROOT / "memory"

logger = logging.getLogger('context_dna')


# =============================================================================
# LOGGING
# =============================================================================

def _safe_log(level: str, message: str):
    """Per-section logging with stderr fallback (preserves GAP 3 FIX intention)."""
    try:
        if level == 'warning':
            logger.warning(message)
        elif level == 'debug':
            logger.debug(message)
        else:
            logger.info(message)
    except Exception:
        # Defensive fallback: stderr persistence (always available)
        print(f"[{level.upper()}] {message}", file=sys.stderr)


# =============================================================================
# REDIS AVAILABILITY
# =============================================================================

_redis_available = None  # Cached status


def is_redis_available() -> bool:
    """Check if Redis is available (cached for performance)."""
    global _redis_available

    # Return cached result if checked recently (within same hook execution)
    if _redis_available is not None:
        return _redis_available

    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client:
            client.ping()
            _redis_available = True
            return True
    except Exception as e:
        print(f"[WARN] Redis availability check failed: {e}")

    _redis_available = False
    return False


def reset_redis_check():
    """Reset Redis availability check (call at start of new injection)."""
    global _redis_available
    _redis_available = None


# =============================================================================
# INTELLIGENT CACHING
# =============================================================================

_cache_store = {}
_cache_timestamps = {}

# Cache TTLs (in seconds)
CACHE_TTL = {
    "brain_state": 30,        # Brain state changes slowly
    "pattern_evolution": 60,  # Patterns are relatively stable
    "hierarchy_profile": 120, # Profile rarely changes
    "section_5_static": 300,  # Protocol is mostly static
}


def get_cached(key: str, loader: Callable, ttl_key: str = None) -> Any:
    """Get cached value or load fresh if expired.

    Args:
        key: Cache key (unique identifier)
        loader: Callable to load fresh data
        ttl_key: TTL category key (defaults to 60s)

    Returns:
        Cached or freshly loaded data
    """
    global _cache_store, _cache_timestamps

    ttl = CACHE_TTL.get(ttl_key, 60) if ttl_key else 60
    now = time.time()

    # Check if cached and not expired
    if key in _cache_store and key in _cache_timestamps:
        if now - _cache_timestamps[key] < ttl:
            return _cache_store[key]

    # Load fresh data
    try:
        data = loader()
        _cache_store[key] = data
        _cache_timestamps[key] = now
        return data
    except Exception:
        # Return stale data if available, otherwise None
        return _cache_store.get(key)


def invalidate_cache(key: str = None):
    """Invalidate cache entry or all entries.

    Args:
        key: Specific key to invalidate, or None for all
    """
    global _cache_store, _cache_timestamps

    if key:
        _cache_store.pop(key, None)
        _cache_timestamps.pop(key, None)
    else:
        _cache_store.clear()
        _cache_timestamps.clear()


def get_brain_state_cached() -> str:
    """Get brain state content with intelligent caching."""
    def loader():
        brain_state_file = MEMORY_DIR / "brain_state.md"
        if brain_state_file.exists():
            return brain_state_file.read_text()
        return ""

    return get_cached("brain_state_content", loader, "brain_state")


def get_hierarchy_profile_cached() -> Dict:
    """Get hierarchy profile with intelligent caching."""
    def loader():
        profile_paths = [
            Path.home() / ".context-dna" / "hierarchy_profile.json",
            MEMORY_DIR.parent / ".context-dna" / "hierarchy_profile.json",
        ]
        for p in profile_paths:
            if p.exists():
                try:
                    return json.loads(p.read_text())
                except Exception:
                    continue
        return {}

    return get_cached("hierarchy_profile", loader, "hierarchy_profile")


# =============================================================================
# PROFESSOR WISDOM (domain-specific wisdom access)
# =============================================================================

_professor_wisdom_available = None
_domain_keywords = None
_professor_wisdom = None


def get_professor_wisdom_dicts():
    """Lazy load DOMAIN_KEYWORDS and PROFESSOR_WISDOM from professor.py."""
    global _professor_wisdom_available, _domain_keywords, _professor_wisdom

    if _professor_wisdom_available is None:
        try:
            from memory.professor import DOMAIN_KEYWORDS, PROFESSOR_WISDOM
            _domain_keywords = DOMAIN_KEYWORDS
            _professor_wisdom = PROFESSOR_WISDOM
            _professor_wisdom_available = True
        except ImportError:
            _professor_wisdom_available = False
            _domain_keywords = {}
            _professor_wisdom = {}

    return _domain_keywords, _professor_wisdom


def detect_domain_from_prompt(prompt: str) -> Optional[str]:
    """Detect the most relevant domain from a prompt using DOMAIN_KEYWORDS.

    Scoring rules:
    - Multi-word phrases (e.g. "github actions") score 1.5x (more specific)
    - Single words use word-boundary matching to avoid substring false positives
    - Tiebreak: prefer domain with fewer total keywords (more focused domain)
    """
    domain_keywords, _ = get_professor_wisdom_dicts()
    if not domain_keywords:
        return None

    prompt_lower = prompt.lower()

    scores = {}
    for domain, keywords in domain_keywords.items():
        score = 0.0
        for kw in keywords:
            if ' ' in kw:
                # Multi-word phrase: exact substring match, weighted higher
                if kw in prompt_lower:
                    score += 1.5
            else:
                # Single word: word boundary match (prevents "git" matching "digit")
                if re.search(r'\b' + re.escape(kw) + r'\b', prompt_lower):
                    score += 1.0
        if score > 0:
            scores[domain] = score

    if not scores:
        return None

    max_score = max(scores.values())
    tied = [d for d, s in scores.items() if s == max_score]
    if len(tied) == 1:
        return tied[0]
    # Tiebreak: fewer keywords = more specific domain
    return min(tied, key=lambda d: (len(domain_keywords[d]), d))


def get_domain_specific_wisdom(domain: str, depth: str = "full") -> Optional[str]:
    """Get wisdom for a specific domain directly from PROFESSOR_WISDOM.

    Args:
        domain: Domain key (e.g., "async_python", "docker_ecs")
        depth: "full", "summary", or "one_thing_only"

    Returns:
        Formatted wisdom string or None if domain not found
    """
    _, professor_wisdom = get_professor_wisdom_dicts()
    if not professor_wisdom or domain not in professor_wisdom:
        return None

    wisdom = professor_wisdom[domain]

    if depth == "one_thing_only":
        # Return just THE ONE THING
        the_one_thing = wisdom.get("the_one_thing", "").strip()
        if the_one_thing:
            return f"THE ONE THING: {the_one_thing}"
        return None

    # Build formatted wisdom
    lines = []

    # THE ONE THING (always first)
    the_one_thing = wisdom.get("the_one_thing", "").strip()
    if the_one_thing:
        lines.append("THE ONE THING:")
        lines.append(the_one_thing)
        lines.append("")

    # LANDMINES (critical warnings)
    landmines = wisdom.get("landmines", [])
    if landmines:
        lines.append("LANDMINES:")
        for mine in landmines:
            lines.append(f"  - {mine}")
        lines.append("")

    if depth == "summary":
        # Stop here for summary
        result = "\n".join(lines)
        return result[:500] + "..." if len(result) > 500 else result

    # THE PATTERN (for full depth)
    pattern = wisdom.get("pattern", "").strip()
    if pattern:
        lines.append("THE PATTERN:")
        lines.append(pattern)
        lines.append("")

    # CONTEXT
    context = wisdom.get("context", "").strip()
    if context:
        lines.append("CONTEXT:")
        lines.append(context)

    return "\n".join(lines) if lines else None


# =============================================================================
# PROMPT PROCESSING
# =============================================================================

def _sanitize_prompt_for_query(prompt: str) -> str:
    """Remove IDE tags and other noise from prompt before querying."""
    # Remove <ide_opened_file>...</ide_opened_file> and similar tags
    clean = re.sub(r'<ide_[^>]+>[^<]*</[^>]+>', '', prompt)
    clean = re.sub(r'<ide_[^>]+>[^<]*', '', clean)
    # Remove <system-reminder> tags
    clean = re.sub(r'<system-reminder>.*?</system-reminder>', '', clean, flags=re.DOTALL)
    # Trim whitespace
    return clean.strip()


def _extract_concern_keywords(concern: str) -> List[str]:
    """Extract meaningful keywords from a meta-analysis concern."""
    # Remove common words, keep domain-specific terms
    stop = {"the", "a", "an", "is", "was", "are", "in", "to", "for", "of", "and", "or", "issue", "repeated"}
    words = re.findall(r'\b[a-z]{4,}\b', concern)
    return [w for w in words if w not in stop][:5]


def _infer_changed_files(prompt: str) -> List[str]:
    """Infer which files are being modified based on prompt context.

    Looks for patterns like:
    - "modify file.py"
    - "edit memory/module.py"
    - "changing context-dna/services"
    - File paths in the prompt
    """
    changed = []
    prompt_lower = (prompt or "").lower()

    # Pattern 1: "modify/edit/change <filepath>"
    patterns = [
        r'(?:modify|edit|change|update)\s+[\w\-./]+\.(?:py|ts|tsx|js)',
        r'file[s]?:?\s+[\w\-./]+\.(?:py|ts|tsx|js)',
        r'(?:memory|admin|backend|infra|context-dna)/[\w\-./]*\.(?:py|ts|tsx|js)',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, prompt_lower)
        for match in matches:
            # Extract file path from match
            file_path = re.search(r'[\w\-./]*\.(?:py|ts|tsx|js)', match)
            if file_path:
                changed.append(file_path.group())

    # Pattern 2: Common critical files referenced in prompt
    critical_files = {
        'hook': 'memory/persistent_hook_structure.py',
        'injection': 'memory/persistent_hook_structure.py',
        'agent_service': 'memory/agent_service.py',
        'scheduler': 'memory/lite_scheduler.py',
        'graph': 'memory/graph_traversal.py',
        'dialogue': 'memory/dialogue_mirror.py',
    }

    for keyword, filepath in critical_files.items():
        if keyword in prompt_lower:
            changed.append(filepath)

    return list(set(changed))


# =============================================================================
# REDIS CACHE KEY GENERATION
# =============================================================================

def _make_cache_key(prefix: str, prompt: str, risk_level: str, version: int = 1) -> str:
    """
    Generate cache key for context pack sections.

    Args:
        prefix: Section prefix (e.g., "s1", "s4")
        prompt: User prompt (hashed for key stability)
        risk_level: Risk level string for differentiation
        version: Cache version for invalidation

    Returns:
        Redis key like "contextdna:s1:abc123:high:v1"
    """
    # Hash first 100 chars of prompt for stable key
    prompt_hash = hashlib.md5(prompt[:100].encode()).hexdigest()[:12]
    return f"contextdna:{prefix}:{prompt_hash}:{risk_level}:v{version}"
