#!/usr/bin/env python3
"""
Evolving Pattern Registry - Learns Success Patterns from Confirmed Wins

This module implements a learning system that discovers NEW success detection
patterns from confirmed wins and merges them with static patterns.

ADDITIVE to existing objective_success.py - does NOT replace existing patterns.

Storage: SQLite locally, PostgreSQL when Docker infrastructure available.

Usage:
    from memory.pattern_registry import EvolvingPatternRegistry

    registry = EvolvingPatternRegistry()

    # Learn from a confirmed success
    registry.learn_from_confirmed(success, context_entries)

    # Get all patterns (static + learned)
    patterns = registry.get_all_patterns()

    # Get only learned patterns
    learned = registry.get_learned_patterns()
"""

import os
import re
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Optional, Any
from dataclasses import dataclass, asdict
from contextlib import contextmanager

# Import static patterns from existing objective_success.py
try:
    from memory.objective_success import (
        USER_CONFIRMATION_PATTERNS,
        SYSTEM_SUCCESS_PATTERNS,
    )
    STATIC_PATTERNS_AVAILABLE = True
except ImportError:
    USER_CONFIRMATION_PATTERNS = []
    SYSTEM_SUCCESS_PATTERNS = []
    STATIC_PATTERNS_AVAILABLE = False


def _resolve_db_path(raw: Path) -> Path:
    """Resolve symlinks; handle broken ones inside Docker containers.

    Host symlinks use absolute macOS paths (e.g.
    /Users/X/.context-dna/file.db) that don't exist in containers.
    Falls back to Docker-mapped /root/.context-dna/ or /tmp.
    """
    if not raw.is_symlink():
        try:
            raw.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return raw

    target = os.readlink(str(raw))
    resolved = Path(target)
    if resolved.parent.exists():
        return resolved

    # Docker remaps ~/.context-dna -> /root/.context-dna
    if "/.context-dna/" in target:
        docker_path = Path("/root/.context-dna") / resolved.name
        if docker_path.parent.exists():
            return docker_path

    # Last resort: writable temp location
    return Path("/tmp") / raw.name


# Database path - resolve symlinks for Docker compatibility
DB_PATH = _resolve_db_path(Path(__file__).parent / ".pattern_registry.db")


@dataclass
class LearnedPattern:
    """A pattern learned from confirmed wins."""
    id: Optional[int]
    pattern: str  # Regex pattern
    confidence: float  # 0.0 - 1.0
    evidence_type: str  # Category of evidence
    source_task: str  # Task that generated this pattern
    times_matched: int  # How often this pattern has matched
    times_confirmed: int  # How often matches were confirmed as wins
    created_at: str
    last_matched: Optional[str]

    @property
    def accuracy(self) -> float:
        """Calculate pattern accuracy based on matches vs confirmations."""
        if self.times_matched == 0:
            return 0.0
        return self.times_confirmed / self.times_matched


class PatternDatabase:
    """SQLite database for storing learned patterns."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = _resolve_db_path(db_path)
        self._init_schema()

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        """Initialize database schema."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learned_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern TEXT NOT NULL UNIQUE,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    evidence_type TEXT NOT NULL,
                    source_task TEXT,
                    times_matched INTEGER DEFAULT 0,
                    times_confirmed INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_matched TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS pattern_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_id INTEGER NOT NULL,
                    matched_text TEXT NOT NULL,
                    was_confirmed INTEGER DEFAULT 0,
                    matched_at TEXT NOT NULL,
                    FOREIGN KEY (pattern_id) REFERENCES learned_patterns(id)
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pattern_confidence
                ON learned_patterns(confidence DESC)
            """)

    def add_pattern(self, pattern: LearnedPattern) -> int:
        """Add a new learned pattern."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO learned_patterns
                (pattern, confidence, evidence_type, source_task,
                 times_matched, times_confirmed, created_at, last_matched)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pattern.pattern,
                pattern.confidence,
                pattern.evidence_type,
                pattern.source_task,
                pattern.times_matched,
                pattern.times_confirmed,
                pattern.created_at,
                pattern.last_matched
            ))
            return cursor.lastrowid

    def get_all_patterns(self, min_confidence: float = 0.3) -> List[LearnedPattern]:
        """Get all patterns above minimum confidence."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM learned_patterns
                WHERE confidence >= ?
                ORDER BY confidence DESC
            """, (min_confidence,)).fetchall()

            return [self._row_to_pattern(row) for row in rows]

    def update_pattern_stats(self, pattern_id: int, matched: bool, confirmed: bool):
        """Update pattern match/confirmation statistics."""
        with self._get_connection() as conn:
            if matched:
                conn.execute("""
                    UPDATE learned_patterns
                    SET times_matched = times_matched + 1,
                        last_matched = ?
                    WHERE id = ?
                """, (datetime.now().isoformat(), pattern_id))

            if confirmed:
                conn.execute("""
                    UPDATE learned_patterns
                    SET times_confirmed = times_confirmed + 1
                    WHERE id = ?
                """, (pattern_id,))

                # Recalculate confidence based on accuracy
                row = conn.execute("""
                    SELECT times_matched, times_confirmed FROM learned_patterns
                    WHERE id = ?
                """, (pattern_id,)).fetchone()

                if row and row['times_matched'] > 0:
                    accuracy = row['times_confirmed'] / row['times_matched']
                    # Blend accuracy with initial confidence
                    new_confidence = min(0.95, (accuracy * 0.7) + 0.3)
                    conn.execute("""
                        UPDATE learned_patterns SET confidence = ? WHERE id = ?
                    """, (new_confidence, pattern_id))

    def find_pattern_by_regex(self, pattern: str) -> Optional[LearnedPattern]:
        """Find pattern by regex string."""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM learned_patterns WHERE pattern = ?
            """, (pattern,)).fetchone()
            return self._row_to_pattern(row) if row else None

    def _row_to_pattern(self, row: sqlite3.Row) -> LearnedPattern:
        """Convert database row to LearnedPattern."""
        return LearnedPattern(
            id=row['id'],
            pattern=row['pattern'],
            confidence=row['confidence'],
            evidence_type=row['evidence_type'],
            source_task=row['source_task'],
            times_matched=row['times_matched'],
            times_confirmed=row['times_confirmed'],
            created_at=row['created_at'],
            last_matched=row['last_matched']
        )


class PatternLearner:
    """Extracts generalizable patterns from confirmed successes."""

    # Common success phrases to look for
    SUCCESS_INDICATORS = [
        # Direct confirmations
        r"that (worked|fixed|solved|did it)",
        r"(it|this) (works|is working|fixed)",
        r"(finally|now) (working|works|fixed)",
        r"(got it|nailed it|figured it out)",
        # Technical confirmations
        r"(build|test|deploy) (passed|succeeded|complete)",
        r"(service|server|container) (is |)(up|running|healthy|started)",
        r"(no|zero) (errors?|failures?|issues?)",
        r"(all|tests?) (green|passing|passed)",
        # Completion markers
        r"(done|finished|completed|shipped)",
        r"(merged|pushed|deployed) (to|successfully)",
    ]

    def extract_pattern_candidates(
        self,
        success_text: str,
        context_entries: List[Dict]
    ) -> List[Tuple[str, float, str]]:
        """
        Extract potential patterns from a confirmed success.

        Returns list of (pattern, confidence, evidence_type) tuples.
        """
        candidates = []
        text_lower = success_text.lower()

        # Check for matches against known indicators
        for indicator in self.SUCCESS_INDICATORS:
            match = re.search(indicator, text_lower, re.IGNORECASE)
            if match:
                # Generalize the matched text into a pattern
                matched_text = match.group(0)
                generalized = self._generalize_pattern(matched_text)
                if generalized:
                    candidates.append((generalized, 0.6, "user_confirmation"))

        # Look for technical patterns in context
        for entry in context_entries:
            content = entry.get("content", "").lower()

            # Exit codes
            if "exit" in content and ("0" in content or "code" in content):
                if re.search(r"exit[_ ]?(code)?[:\s]*0", content):
                    candidates.append((
                        r"exit[_ ]?(code)?[:\s]*0",
                        0.75,
                        "exit_zero"
                    ))

            # HTTP success
            if re.search(r"(200|201|204)\s*(ok|created|no content)?", content, re.I):
                candidates.append((
                    r"(200|201|204)\s*(ok|created|no content)?",
                    0.7,
                    "http_success"
                ))

            # Health checks
            if "healthy" in content or "health" in content:
                candidates.append((
                    r"\bhealthy\b",
                    0.8,
                    "health_check"
                ))

        return candidates

    def _generalize_pattern(self, matched_text: str) -> Optional[str]:
        """
        Generalize a specific matched text into a reusable regex pattern.

        Examples:
            "that worked" -> r"that (worked|fixed|solved)"
            "finally working" -> r"finally (working|works|fixed)"
        """
        text = matched_text.lower().strip()

        # Common generalizations
        generalizations = {
            "worked": r"(worked|fixed|solved|succeeded)",
            "working": r"(working|works|functioning)",
            "passed": r"(passed|succeeded|completed)",
            "done": r"(done|finished|completed)",
            "deployed": r"(deployed|shipped|released)",
            "merged": r"(merged|pushed|committed)",
        }

        result = text
        for specific, general in generalizations.items():
            if specific in result:
                result = result.replace(specific, general)
                break

        # Escape special regex chars except our generalizations
        if result == text:
            result = re.escape(text)

        # Add word boundaries
        if not result.startswith(r"\b"):
            result = r"\b" + result
        if not result.endswith(r"\b"):
            result = result + r"\b"

        return result


class EvolvingPatternRegistry:
    """
    Main interface for the evolving pattern system.

    Merges learned patterns with static patterns from objective_success.py.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db = PatternDatabase(db_path)
        self.learner = PatternLearner()

    def learn_from_confirmed(
        self,
        success_task: str,
        success_details: str,
        context_entries: List[Dict]
    ) -> int:
        """
        Learn new patterns from a confirmed success.

        Args:
            success_task: Description of the successful task
            success_details: Details of what worked
            context_entries: Surrounding work log entries

        Returns:
            Number of new patterns learned
        """
        # Combine task and details for pattern extraction
        full_text = f"{success_task} {success_details}"

        # Extract candidate patterns
        candidates = self.learner.extract_pattern_candidates(
            full_text, context_entries
        )

        patterns_added = 0
        for pattern, confidence, evidence_type in candidates:
            # Check if pattern already exists
            existing = self.db.find_pattern_by_regex(pattern)
            if existing:
                # Update confirmation stats
                self.db.update_pattern_stats(
                    existing.id, matched=True, confirmed=True
                )
            else:
                # Add new pattern
                new_pattern = LearnedPattern(
                    id=None,
                    pattern=pattern,
                    confidence=confidence,
                    evidence_type=evidence_type,
                    source_task=success_task[:200],
                    times_matched=1,
                    times_confirmed=1,
                    created_at=datetime.now().isoformat(),
                    last_matched=datetime.now().isoformat()
                )
                self.db.add_pattern(new_pattern)
                patterns_added += 1

        return patterns_added

    def get_learned_patterns(
        self,
        min_confidence: float = 0.4
    ) -> List[Tuple[str, float, str]]:
        """
        Get learned patterns in same format as static patterns.

        Returns list of (pattern, confidence, evidence_type) tuples.
        """
        patterns = self.db.get_all_patterns(min_confidence)
        return [(p.pattern, p.confidence, p.evidence_type) for p in patterns]

    def get_all_patterns(self) -> List[Tuple[str, float, str]]:
        """
        Get ALL patterns: static (from objective_success.py) + learned.

        Static patterns take precedence on conflicts.
        """
        all_patterns = []
        seen_patterns = set()

        # Add static patterns first (they take precedence)
        if STATIC_PATTERNS_AVAILABLE:
            for pattern, confidence, evidence_type in USER_CONFIRMATION_PATTERNS:
                all_patterns.append((pattern, confidence, evidence_type))
                seen_patterns.add(pattern)

            for pattern, confidence, evidence_type in SYSTEM_SUCCESS_PATTERNS:
                if pattern not in seen_patterns:
                    all_patterns.append((pattern, confidence, evidence_type))
                    seen_patterns.add(pattern)

        # Add learned patterns (skip duplicates)
        for pattern, confidence, evidence_type in self.get_learned_patterns():
            if pattern not in seen_patterns:
                all_patterns.append((pattern, confidence, evidence_type))
                seen_patterns.add(pattern)

        return all_patterns

    def record_pattern_match(
        self,
        pattern: str,
        matched_text: str,
        was_confirmed: bool
    ):
        """Record that a pattern matched (and whether it was a true positive)."""
        existing = self.db.find_pattern_by_regex(pattern)
        if existing:
            self.db.update_pattern_stats(
                existing.id,
                matched=True,
                confirmed=was_confirmed
            )

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the pattern registry."""
        learned = self.db.get_all_patterns(min_confidence=0.0)

        high_confidence = [p for p in learned if p.confidence >= 0.7]
        medium_confidence = [p for p in learned if 0.4 <= p.confidence < 0.7]
        low_confidence = [p for p in learned if p.confidence < 0.4]

        return {
            "total_learned": len(learned),
            "high_confidence": len(high_confidence),
            "medium_confidence": len(medium_confidence),
            "low_confidence": len(low_confidence),
            "static_patterns": (
                len(USER_CONFIRMATION_PATTERNS) + len(SYSTEM_SUCCESS_PATTERNS)
                if STATIC_PATTERNS_AVAILABLE else 0
            ),
            "total_available": len(self.get_all_patterns()),
        }


# CLI interface
if __name__ == "__main__":
    import sys

    registry = EvolvingPatternRegistry()

    if len(sys.argv) < 2:
        print("Evolving Pattern Registry - Learn success patterns from wins")
        print("")
        print("Commands:")
        print("  list                    - List all patterns")
        print("  learned                 - List only learned patterns")
        print("  stats                   - Show registry statistics")
        print("  learn <task> <details>  - Learn from a success")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "list":
        patterns = registry.get_all_patterns()
        print(f"Total patterns: {len(patterns)}")
        print("\nPatterns (confidence descending):")
        for pattern, confidence, evidence_type in sorted(
            patterns, key=lambda x: x[1], reverse=True
        ):
            print(f"  [{confidence:.2f}] {evidence_type}: {pattern[:60]}")

    elif cmd == "learned":
        patterns = registry.get_learned_patterns()
        print(f"Learned patterns: {len(patterns)}")
        for pattern, confidence, evidence_type in patterns:
            print(f"  [{confidence:.2f}] {evidence_type}: {pattern[:60]}")

    elif cmd == "stats":
        stats = registry.get_stats()
        print("Pattern Registry Statistics:")
        print(f"  Static patterns:    {stats['static_patterns']}")
        print(f"  Learned patterns:   {stats['total_learned']}")
        print(f"    High confidence:  {stats['high_confidence']}")
        print(f"    Medium conf:      {stats['medium_confidence']}")
        print(f"    Low confidence:   {stats['low_confidence']}")
        print(f"  Total available:    {stats['total_available']}")

    elif cmd == "learn":
        if len(sys.argv) < 4:
            print("Usage: learn <task> <details>")
            sys.exit(1)
        task = sys.argv[2]
        details = " ".join(sys.argv[3:])
        count = registry.learn_from_confirmed(task, details, [])
        print(f"Learned {count} new patterns from: {task}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
