#!/usr/bin/env python3
"""
Learning Store - Persists context learnings for visualization

This module stores learnings (wins, fixes, patterns) so they can be
retrieved and visualized by the frontend dashboard alongside injections.

Storage Strategy:
- PRIMARY: SQLite database (.context-dna.db) - scalable, FTS5 search
- BACKUP: JSON file still maintained for backwards compatibility

The SQLite backend is the SINGLE SOURCE OF TRUTH shared by:
- Helper Agent (port 8080)
- Python API (port 3456)
- xbar plugin
- Dashboard

Usage:
    from memory.learning_store import LearningStore, get_learning_store

    store = get_learning_store()
    store.store_learning(learning_data)
    recent = store.get_recent(limit=20)
    session_learnings = store.get_by_session(session_id)
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Base paths
MEMORY_DIR = Path(__file__).parent
LEARNING_HISTORY_FILE = MEMORY_DIR / ".learning_history.json"
MAX_HISTORY = 100

# Import SQLite storage - the single source of truth
try:
    from memory.sqlite_storage import get_sqlite_storage, SQLiteStorage
    SQLITE_AVAILABLE = True
except ImportError:
    try:
        from sqlite_storage import get_sqlite_storage, SQLiteStorage
        SQLITE_AVAILABLE = True
    except ImportError:
        SQLITE_AVAILABLE = False


class LearningStore:
    """
    Store and retrieve context learnings for visualization.

    PRIMARY STORAGE: SQLite database (.context-dna.db)
    - FTS5 full-text search
    - No record limit
    - WAL mode for concurrent access

    BACKUP: JSON file (.learning_history.json)
    - Maintained for backwards compatibility
    - Synced on writes

    Learnings are associated with:
    - session_id: Links to the injection session
    - timestamp: When the learning was captured
    - type: win, fix, pattern, insight, gotcha
    """

    def __init__(self):
        self._ensure_files()
        # Initialize SQLite storage if available
        self._sqlite = get_sqlite_storage() if SQLITE_AVAILABLE else None

    def _ensure_files(self):
        """Create storage files if they don't exist."""
        if not LEARNING_HISTORY_FILE.exists():
            self._write_json(LEARNING_HISTORY_FILE, [])

    def _read_json(self, path: Path) -> Any:
        """Read JSON from file."""
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _write_json(self, path: Path, data: Any):
        """Write JSON to file atomically."""
        temp_path = path.with_suffix('.tmp')
        with open(temp_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        temp_path.replace(path)

    def _find_duplicate(self, new_learning: Dict[str, Any], existing: List[Dict[str, Any]],
                         time_window_hours: int = 24) -> Optional[int]:
        """
        Find index of duplicate learning in existing list.

        Duplicate criteria - STRICT (prevents over-consolidation):
        1. 80%+ word overlap in title OR exact core title match
        2. Within the time window (24h default)

        This prevents learnings like "docker deployment" from matching "deployment succeeded"
        which was causing over-consolidation and content corruption.

        Args:
            new_learning: The learning to check
            existing: List of existing learnings
            time_window_hours: Only check for dupes within this window

        Returns:
            Index of the duplicate learning if found, None otherwise
        """
        import re

        new_title = (new_learning.get('title') or '').lower().strip()
        new_content = (new_learning.get('content') or '')[:200].lower().strip()

        # Skip dedup if title and content are both empty
        if not new_title and not new_content:
            return None

        # Extract core title (remove SOP tags, "Agent Success:", etc.)
        def get_core_title(title: str) -> str:
            core = title.lower().strip()
            # Remove common prefixes
            prefixes_to_strip = [
                r'^\[process sop\]\s*',
                r'^\[bug-fix sop\]\s*',
                r'^agent success:\s*',
                r'^fix:\s*',
            ]
            for pattern in prefixes_to_strip:
                core = re.sub(pattern, '', core)
            return core.strip()

        def word_overlap_ratio(text1: str, text2: str) -> float:
            """Calculate word overlap ratio between two texts."""
            words1 = set(text1.split())
            words2 = set(text2.split())
            if not words1 or not words2:
                return 0.0
            overlap = len(words1 & words2)
            # Use the larger set as denominator to be more strict
            return overlap / max(len(words1), len(words2))

        new_core_title = get_core_title(new_title)
        new_title_words = set(new_title.split())
        now = datetime.now(timezone.utc)

        for idx, learning in enumerate(existing):
            # Check time window
            try:
                learning_time = datetime.fromisoformat(
                    learning.get('timestamp', '').replace('Z', '+00:00')
                )
                hours_ago = (now - learning_time).total_seconds() / 3600
                if hours_ago > time_window_hours:
                    continue  # Outside time window, skip
            except (ValueError, TypeError):
                continue  # Invalid timestamp, skip

            existing_title = (learning.get('title') or '').lower().strip()
            existing_title_words = set(existing_title.split())

            # Match 1: 80%+ word overlap in title (STRICT)
            if new_title_words and existing_title_words:
                overlap_ratio = word_overlap_ratio(new_title, existing_title)
                if overlap_ratio >= 0.8:
                    return idx

            # Match 2: Exact core title match (after stripping prefixes)
            existing_core_title = get_core_title(existing_title)
            if new_core_title and existing_core_title and new_core_title == existing_core_title:
                return idx

            # NOTE: Removed content-only matching - it was too loose and caused
            # unrelated learnings to be merged. Now only title-based matching.

        return None

    def _extract_unique_value(self, shorter: str, longer: str) -> str:
        """
        Extract unique value from shorter content that isn't in longer content.

        Preserves MEANING by keeping full sentences/phrases.

        Returns:
            Unique content from shorter, or empty string if nothing unique
        """
        import re

        if not shorter or not longer:
            return ""

        longer_lower = longer.lower()
        shorter_lower = shorter.lower()

        # If shorter is fully contained in longer, nothing unique
        if shorter_lower in longer_lower:
            return ""

        # Extract sentences/phrases from shorter
        shorter_parts = re.split(r'[.\n]+', shorter)

        unique_parts = []
        longer_words = set(re.findall(r'\b\w{4,}\b', longer_lower))

        for part in shorter_parts:
            part = part.strip()
            if not part or len(part) < 10:
                continue

            # Check if this part's key words are in longer
            part_words = set(re.findall(r'\b\w{4,}\b', part.lower()))

            # If >50% of part's words aren't in longer, it's unique
            if part_words:
                missing_words = part_words - longer_words
                if len(missing_words) / len(part_words) > 0.5:
                    unique_parts.append(part)

        return ". ".join(unique_parts) if unique_parts else ""

    def _make_concise(self, content: str, max_length: int = 500) -> str:
        """
        Make content optimally concise while PRESERVING meaningful structure.

        Strategy:
        - PRESERVE meaningful headers like **Symptom:**, **Root Cause:**, **Fix:**
          (These provide valuable context for understanding the learning)
        - Remove only truly generic boilerplate (status, agent, etc.)
        - Clean up whitespace
        - Cap at max_length, ending at sentence boundary
        """
        import re

        if not content:
            return ""

        # NOTE: We NO LONGER strip meaningful headers!
        # Headers like **Task:**, **Symptom:**, **Root Cause:**, **Fix Applied:**
        # provide valuable structure that helps understand the learning.
        # Only strip truly generic boilerplate.

        # Remove boilerplate lines entirely (these have no unique value)
        boilerplate_patterns = [
            r'\*\*Status:\*\*[^\n]*',          # Status line - always generic
            r'\*\*Agent:\*\*[^\n]*',           # Agent line - always generic
            r'Successfully completed\.?\s*',
            r'✅ SUCCESS - Verified working\.?\s*',
            r'✅ RESOLVED - Fix verified and working\.?\s*',
            r'\[process SOP\]\s*',             # Process SOP markers
            r'→ ✓ success\s*',                 # Generic success markers
            r'\[\d+%\]',                       # Merge percentage artifacts like [50%]
        ]
        for pattern in boilerplate_patterns:
            content = re.sub(pattern, '', content, flags=re.IGNORECASE)

        # Clean up whitespace (multiple spaces, excess newlines)
        content = re.sub(r'\n{2,}', '\n', content)
        content = re.sub(r'\s+', ' ', content).strip()

        # Remove duplicate adjacent phrases (e.g., "X running successfully X running successfully")
        words = content.split()
        if len(words) > 4:
            # Check for repeated sequences
            for seq_len in range(len(words) // 2, 2, -1):
                first_half = ' '.join(words[:seq_len])
                second_half = ' '.join(words[seq_len:2*seq_len])
                if first_half.lower() == second_half.lower():
                    content = first_half + ' ' + ' '.join(words[2*seq_len:])
                    content = content.strip()
                    break

        # Only truncate if still too long
        if len(content) > max_length:
            # Find last sentence end before max_length
            truncated = content[:max_length]
            last_period = truncated.rfind('.')
            last_newline = truncated.rfind('\n')
            cut_point = max(last_period, last_newline)

            if cut_point > max_length // 2:
                content = content[:cut_point + 1].strip()
            else:
                content = truncated.strip() + "..."

        return content

    def _smart_merge(self, existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        """
        Smart merge two learnings - preserves meaning, then makes concise.

        Strategy:
        1. Title: Keep longer/more descriptive
        2. Content: Keep longer as base, append unique from shorter
        3. Tags: Union of both
        4. FINAL: Make result concise

        Args:
            existing: The existing learning
            new: The new learning to merge in

        Returns:
            Merged learning with best of both, optimally concise
        """
        merged = existing.copy()

        # Keep better title (longer = more descriptive)
        new_title = new.get('title') or ''
        existing_title = existing.get('title') or ''
        if len(new_title) > len(existing_title):
            merged['title'] = new_title

        # Smart content merge: keep longer, append unique from shorter
        new_content = new.get('content') or ''
        existing_content = existing.get('content') or ''

        if len(new_content) > len(existing_content):
            # New is longer - check if existing has unique value
            unique_from_existing = self._extract_unique_value(existing_content, new_content)
            if unique_from_existing:
                merged['content'] = f"{new_content}\n\nAlso: {unique_from_existing}"
            else:
                merged['content'] = new_content
        else:
            # Existing is longer - check if new has unique value
            unique_from_new = self._extract_unique_value(new_content, existing_content)
            if unique_from_new:
                merged['content'] = f"{existing_content}\n\nAlso: {unique_from_new}"
            # else: keep existing_content as-is

        # FINAL STEP: Make concise
        merged['content'] = self._make_concise(merged['content'])

        # Merge tags (union)
        existing_tags = set(existing.get('tags') or [])
        new_tags = set(new.get('tags') or [])
        merged['tags'] = list(existing_tags | new_tags)

        # Track merge
        merged['_merge_count'] = existing.get('_merge_count', 1) + 1

        return merged

    def store_learning(self, learning_data: Dict[str, Any], skip_dedup: bool = False,
                       consolidate: bool = True) -> Dict[str, Any]:
        """
        Store a new context learning with smart duplicate consolidation.

        PRIMARY: SQLite database (scalable, FTS5 search)
        BACKUP: JSON file (backwards compatibility)

        Args:
            learning_data: The learning data including:
                - type: win, fix, pattern, insight, gotcha
                - title: Short description
                - content: Full details
                - tags: List of relevant tags
                - session_id: Associated injection session (optional)
                - source: Where the learning came from
            skip_dedup: If True, skip duplicate checking entirely
            consolidate: If True, merge duplicates keeping best of both.
                        If False, just reject duplicates.

        Returns:
            The stored/consolidated learning.
            If duplicate found and consolidated, '_consolidated': True flag added.
            If duplicate found and not consolidated, '_duplicate': True flag added.
        """
        # PRIMARY: Use SQLite storage if available
        if self._sqlite:
            result = self._sqlite.store_learning(learning_data, skip_dedup, consolidate)
            # Also update JSON backup for backwards compatibility
            self._sync_to_json_backup(result)
            return result

        # FALLBACK: Use JSON file if SQLite not available
        # Read existing history first (for dedup check)
        history = self._read_json(LEARNING_HISTORY_FILE) or []

        # Check for duplicates (unless explicitly skipped)
        if not skip_dedup:
            dup_idx = self._find_duplicate(learning_data, history)
            if dup_idx is not None:
                existing = history[dup_idx]

                # Limit merge depth to prevent over-consolidation and content corruption
                # After 3 merges, store as new learning to preserve unique context
                MAX_MERGE_COUNT = 3
                existing_merge_count = existing.get('_merge_count', 1)

                if consolidate and existing_merge_count < MAX_MERGE_COUNT:
                    # Smart merge: keep best from both
                    merged = self._smart_merge(existing, learning_data)
                    history[dup_idx] = merged
                    self._write_json(LEARNING_HISTORY_FILE, history)
                    merged['_consolidated'] = True
                    return merged
                elif existing_merge_count >= MAX_MERGE_COUNT:
                    # Don't merge - already consolidated too many times
                    # Store as new learning to preserve unique context
                    pass  # Fall through to store as new
                else:
                    # Just reject the duplicate
                    existing['_duplicate'] = True
                    return existing

        # Add ID and ensure timestamp (use UTC with Z suffix for proper frontend conversion)
        learning_data['id'] = f"learn_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
        if 'timestamp' not in learning_data:
            learning_data['timestamp'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        if 'tags' not in learning_data:
            learning_data['tags'] = []

        # Add to history
        history.insert(0, learning_data)
        history = history[:MAX_HISTORY]  # Keep only recent
        self._write_json(LEARNING_HISTORY_FILE, history)

        return learning_data

    def _sync_to_json_backup(self, learning: Dict[str, Any]) -> None:
        """Sync a learning to JSON backup file for backwards compatibility."""
        try:
            history = self._read_json(LEARNING_HISTORY_FILE) or []
            # Check if already exists
            for i, l in enumerate(history):
                if l.get('id') == learning.get('id'):
                    history[i] = learning  # Update existing
                    self._write_json(LEARNING_HISTORY_FILE, history)
                    return
            # Add new
            history.insert(0, learning)
            history = history[:MAX_HISTORY]
            self._write_json(LEARNING_HISTORY_FILE, history)
        except Exception:
            pass  # Backup sync is best-effort

    def get_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent learnings."""
        if self._sqlite:
            return self._sqlite.get_recent(limit)
        history = self._read_json(LEARNING_HISTORY_FILE) or []
        return history[:limit]

    def get_by_session(self, session_id: str) -> List[Dict[str, Any]]:
        """Get learnings for a specific session."""
        if self._sqlite:
            return self._sqlite.get_by_session(session_id)
        history = self._read_json(LEARNING_HISTORY_FILE) or []
        return [l for l in history if l.get('session_id') == session_id]

    def get_since(self, timestamp: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get learnings since a given timestamp (ISO format)."""
        if self._sqlite:
            return self._sqlite.get_since(timestamp, limit)
        history = self._read_json(LEARNING_HISTORY_FILE) or []
        try:
            cutoff = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except ValueError:
            return []

        result = []
        for learning in history:
            try:
                learning_time = datetime.fromisoformat(
                    learning.get('timestamp', '').replace('Z', '+00:00')
                )
                if learning_time >= cutoff:
                    result.append(learning)
            except ValueError:
                continue
        return result[:limit]

    def get_by_type(self, learning_type: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get learnings of a specific type."""
        if self._sqlite:
            return self._sqlite.get_by_type(learning_type, limit)
        history = self._read_json(LEARNING_HISTORY_FILE) or []
        return [l for l in history if l.get('type') == learning_type][:limit]

    def get_by_id(self, learning_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific learning by ID."""
        if self._sqlite:
            row = self._sqlite.get_by_id(learning_id)
            return self._sqlite._row_to_dict(row) if row else None
        history = self._read_json(LEARNING_HISTORY_FILE) or []
        for learning in history:
            if learning.get('id') == learning_id:
                return learning
        return None

    def get_stats(self) -> Dict[str, Any]:
        """Get storage statistics."""
        if self._sqlite:
            return self._sqlite.get_stats()
        # Fallback to JSON stats
        history = self._read_json(LEARNING_HISTORY_FILE) or []
        by_type = {}
        for l in history:
            t = l.get('type', 'unknown')
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total": len(history),
            "wins": by_type.get("win", 0),
            "fixes": by_type.get("fix", 0),
            "patterns": by_type.get("pattern", 0),
            "by_type": by_type,
        }

    def query(self, search: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Full-text search (requires SQLite)."""
        if self._sqlite:
            return self._sqlite.query(search, limit)
        # Fallback: simple substring search
        history = self._read_json(LEARNING_HISTORY_FILE) or []
        search_lower = search.lower()
        results = []
        for l in history:
            if search_lower in (l.get('title', '') + l.get('content', '')).lower():
                results.append(l)
                if len(results) >= limit:
                    break
        return results

    def associate_with_injection(self, learning_id: str, injection_id: str) -> bool:
        """Associate a learning with an injection."""
        history = self._read_json(LEARNING_HISTORY_FILE) or []
        for i, learning in enumerate(history):
            if learning.get('id') == learning_id:
                learning['injection_id'] = injection_id
                self._write_json(LEARNING_HISTORY_FILE, history)
                return True
        return False

    def get_for_injection(self, injection_id: str) -> List[Dict[str, Any]]:
        """Get all learnings associated with an injection."""
        history = self._read_json(LEARNING_HISTORY_FILE) or []
        return [l for l in history if l.get('injection_id') == injection_id]


# Singleton instance
_learning_store: Optional[LearningStore] = None


def get_learning_store() -> LearningStore:
    """Get the singleton learning store instance."""
    global _learning_store
    if _learning_store is None:
        _learning_store = LearningStore()
    return _learning_store


def build_learning_data(
    learning_type: str,
    title: str,
    content: str,
    tags: List[str] = None,
    session_id: str = "",
    injection_id: str = "",
    source: str = "manual",
    metadata: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Build the full learning data structure for storage.

    Args:
        learning_type: win, fix, pattern, insight, gotcha
        title: Short description of the learning
        content: Full details
        tags: Relevant keywords for search
        session_id: Associated injection session
        injection_id: Associated injection ID
        source: Where the learning came from (hook, manual, auto)
        metadata: Any extra data to include

    Returns:
        Full learning data structure ready for storage
    """
    return {
        'type': learning_type,
        'title': title,
        'content': content,
        'tags': tags or [],
        'session_id': session_id,
        'injection_id': injection_id,
        'source': source,
        'metadata': metadata or {},
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
    }


if __name__ == "__main__":
    # Test the store
    store = get_learning_store()

    # Store a test learning
    test_learning = build_learning_data(
        learning_type="win",
        title="Fixed injection store import path",
        content="Changed from absolute to relative import to work from shell hooks",
        tags=["python", "imports", "hooks"],
        session_id="test-session-123",
        source="manual"
    )

    stored = store.store_learning(test_learning)
    print(f"Stored learning: {stored['id']}")

    # Get recent
    recent = store.get_recent(limit=5)
    print(f"Recent learnings: {len(recent)}")
    for l in recent:
        print(f"  - [{l['type']}] {l['title']}")
