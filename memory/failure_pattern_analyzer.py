#!/usr/bin/env python3
"""
FAILURE PATTERN ANALYZER - Intelligent Hindsight for Webhook Injection

This module unifies DialogueMirror, TemporalValidator, and HindsightValidator
to detect high-yield failure patterns and generate LANDMINE warnings for webhooks.

The Core Insight:
- Not all failures are worth remembering
- HIGH-YIELD failures are: repeated attempts, same task retried 3+ times, reversals
- These become LANDMINE warnings injected via Section 2 (WISDOM) of webhooks

Architecture:
┌─────────────────────────────────────────────────────────────────┐
│                   FAILURE PATTERN ANALYZER                       │
├─────────────────────────────────────────────────────────────────┤
│  INPUT SOURCES:                                                  │
│  1. DialogueMirror    → Repeated task attempts, conversation     │
│  2. TemporalValidator → Reversal patterns, failed confirmations  │
│  3. HindsightValidator → Miswiring learnings, demoted patterns   │
├─────────────────────────────────────────────────────────────────┤
│  OUTPUT:                                                         │
│  - LANDMINE warnings for webhook injection                       │
│  - High-confidence failure patterns                              │
│  - Domain-specific gotchas from real failures                    │
└─────────────────────────────────────────────────────────────────┘

Usage:
    from memory.failure_pattern_analyzer import FailurePatternAnalyzer

    analyzer = FailurePatternAnalyzer()

    # Get LANDMINE warnings for a specific domain/task
    landmines = analyzer.get_landmines_for_task("deploy Django to production")

    # Analyze dialogue for failure patterns
    patterns = analyzer.analyze_for_patterns(session_id="abc123")

    # Get all high-yield failure patterns
    all_patterns = analyzer.get_high_yield_failures(limit=10)

Created: February 3, 2026
Author: Atlas (for Synaptic's intelligent hindsight)
Purpose: Generate LANDMINE warnings from real failure patterns for webhook injection
"""

import logging
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class FailurePattern:
    """A detected failure pattern worth warning about."""
    pattern_id: str
    domain: str
    description: str
    occurrence_count: int
    confidence: float  # 0.0 to 1.0
    landmine_text: str  # The formatted LANDMINE warning
    keywords: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)  # dialogue_mirror, hindsight, temporal
    last_seen: str = ""

    def to_dict(self) -> Dict:
        return {
            "pattern_id": self.pattern_id,
            "domain": self.domain,
            "description": self.description,
            "occurrence_count": self.occurrence_count,
            "confidence": self.confidence,
            "landmine_text": self.landmine_text,
            "keywords": self.keywords,
            "sources": self.sources,
            "last_seen": self.last_seen,
        }


def _t_fail(name: str) -> str:
    from memory.db_utils import unified_table
    return unified_table(".failure_patterns.db", name)


class FailurePatternAnalyzer:
    """
    Analyzes dialogue and validation data to find high-yield failure patterns.

    "High-yield" means:
    - Repeated 3+ times (same task attempted multiple times)
    - Had temporal reversal (success followed by failure)
    - Caused miswiring (win that later caused problems)
    """

    # Minimum occurrences to be considered high-yield
    MIN_OCCURRENCES = 2

    # Confidence thresholds
    HIGH_CONFIDENCE = 0.8
    MEDIUM_CONFIDENCE = 0.6
    LOW_CONFIDENCE = 0.4

    def __init__(self, db_path: Optional[Path] = None):
        self.memory_dir = Path(__file__).parent
        if db_path is None:
            from memory.db_utils import get_unified_db_path
            db_path = get_unified_db_path(self.memory_dir / ".failure_patterns.db")
        self.db_path = db_path
        self._init_db()

        # Lazy-loaded validators
        self._dialogue_mirror = None
        self._temporal_validator = None
        self._hindsight_validator = None

    def _init_db(self):
        """Initialize SQLite database for failure pattern storage."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS {_t_fail('failure_patterns')} (
                    pattern_id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    description TEXT NOT NULL,
                    occurrence_count INTEGER DEFAULT 1,
                    confidence REAL DEFAULT 0.5,
                    landmine_text TEXT NOT NULL,
                    keywords_json TEXT,
                    sources_json TEXT,
                    last_seen TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_patterns_domain
                    ON {_t_fail('failure_patterns')}(domain);
                CREATE INDEX IF NOT EXISTS idx_patterns_confidence
                    ON {_t_fail('failure_patterns')}(confidence DESC);
                CREATE INDEX IF NOT EXISTS idx_patterns_count
                    ON {_t_fail('failure_patterns')}(occurrence_count DESC);
            """)

    @property
    def dialogue_mirror(self):
        """Lazy load DialogueMirror."""
        if self._dialogue_mirror is None:
            try:
                from memory.dialogue_mirror import DialogueMirror
                self._dialogue_mirror = DialogueMirror()
            except ImportError:
                logger.warning("DialogueMirror not available")
        return self._dialogue_mirror

    @property
    def temporal_validator(self):
        """Lazy load TemporalValidator."""
        if self._temporal_validator is None:
            try:
                from memory.temporal_validator import TemporalValidator
                self._temporal_validator = TemporalValidator()
            except ImportError:
                logger.warning("TemporalValidator not available")
        return self._temporal_validator

    @property
    def hindsight_validator(self):
        """Lazy load HindsightValidator."""
        if self._hindsight_validator is None:
            try:
                from memory.hindsight_validator import HindsightValidator
                self._hindsight_validator = HindsightValidator()
            except ImportError:
                logger.warning("HindsightValidator not available")
        return self._hindsight_validator

    # =========================================================================
    # CORE ANALYSIS METHODS
    # =========================================================================

    def analyze_for_patterns(self, session_id: str = None, hours_back: int = 24) -> List[FailurePattern]:
        """
        Analyze recent dialogue for failure patterns.

        Steps:
        1. Get recent dialogue from DialogueMirror
        2. Identify repeated task attempts
        3. Find reversal patterns from TemporalValidator
        4. Include miswiring learnings from HindsightValidator
        5. Generate high-yield failure patterns

        Args:
            session_id: Optional filter by session
            hours_back: How far back to look (default 24h)

        Returns:
            List of detected FailurePattern objects
        """
        patterns = []

        # 1. Analyze dialogue for repeated attempts
        dialogue_patterns = self._analyze_dialogue_repetition(session_id, hours_back)
        patterns.extend(dialogue_patterns)

        # 2. Get miswiring learnings from HindsightValidator
        miswiring_patterns = self._get_miswiring_patterns()
        patterns.extend(miswiring_patterns)

        # 3. Deduplicate and merge similar patterns
        merged_patterns = self._merge_similar_patterns(patterns)

        # 4. Filter to high-yield only
        high_yield = [p for p in merged_patterns if self._is_high_yield(p)]

        # 5. Store discovered patterns
        for pattern in high_yield:
            self._store_pattern(pattern)

        return high_yield

    def get_landmines_for_task(self, task: str, limit: int = 3) -> List[str]:
        """
        Get relevant LANDMINE warnings for a task.

        This is the primary interface for webhook injection (Section 2: WISDOM).

        Args:
            task: The task description (prompt)
            limit: Maximum warnings to return

        Returns:
            List of LANDMINE warning strings
        """
        # Extract keywords from task
        keywords = self._extract_keywords(task)
        domain = self._detect_domain(task)

        landmines = []

        # 1. Check stored patterns
        stored = self._get_relevant_patterns(keywords, domain, limit * 2)
        for pattern in stored:
            if pattern.confidence >= self.MEDIUM_CONFIDENCE:
                landmines.append(pattern.landmine_text)

        # 2. Check miswiring learnings for this domain
        if self.hindsight_validator:
            try:
                misw = self._get_miswiring_for_domain(domain)
                for m in misw[:limit]:
                    if m not in landmines:
                        landmines.append(m)
            except Exception as e:
                logger.debug(f"Error getting miswiring: {e}")

        # 3. Return top landmines by relevance (already sorted)
        return landmines[:limit]

    def get_high_yield_failures(self, limit: int = 10, domain: str = None) -> List[FailurePattern]:
        """
        Get all high-yield failure patterns.

        Args:
            limit: Maximum patterns to return
            domain: Optional filter by domain

        Returns:
            List of FailurePattern objects sorted by confidence and count
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if domain:
                rows = conn.execute(f"""
                    SELECT * FROM {_t_fail('failure_patterns')}
                    WHERE domain = ? AND confidence >= ?
                    ORDER BY confidence DESC, occurrence_count DESC
                    LIMIT ?
                """, (domain, self.MEDIUM_CONFIDENCE, limit)).fetchall()
            else:
                rows = conn.execute(f"""
                    SELECT * FROM {_t_fail('failure_patterns')}
                    WHERE confidence >= ?
                    ORDER BY confidence DESC, occurrence_count DESC
                    LIMIT ?
                """, (self.MEDIUM_CONFIDENCE, limit)).fetchall()

        import json
        patterns = []
        for row in rows:
            patterns.append(FailurePattern(
                pattern_id=row['pattern_id'],
                domain=row['domain'],
                description=row['description'],
                occurrence_count=row['occurrence_count'],
                confidence=row['confidence'],
                landmine_text=row['landmine_text'],
                keywords=json.loads(row['keywords_json'] or '[]'),
                sources=json.loads(row['sources_json'] or '[]'),
                last_seen=row['last_seen'] or '',
            ))

        return patterns

    # =========================================================================
    # DIALOGUE ANALYSIS
    # =========================================================================

    def _analyze_dialogue_repetition(self, session_id: str = None, hours_back: int = 24) -> List[FailurePattern]:
        """
        Analyze dialogue for repeated task attempts.

        A task attempted 3+ times in a short period is a strong failure signal.
        """
        patterns = []

        if not self.dialogue_mirror:
            return patterns

        try:
            # Get recent messages
            cutoff = (datetime.utcnow() - timedelta(hours=hours_back)).isoformat()

            from memory.db_utils import unified_table
            _t_msgs = unified_table(".dialogue_mirror.db", "dialogue_messages")
            with sqlite3.connect(self.dialogue_mirror.db_path) as conn:
                conn.row_factory = sqlite3.Row

                # Get Aaron's messages (tasks/requests)
                if session_id:
                    messages = conn.execute(f"""
                        SELECT content, timestamp, project_context
                        FROM {_t_msgs}
                        WHERE role = 'aaron' AND timestamp > ? AND session_id = ?
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """, (cutoff, session_id)).fetchall()
                else:
                    messages = conn.execute(f"""
                        SELECT content, timestamp, project_context
                        FROM {_t_msgs}
                        WHERE role = 'aaron' AND timestamp > ?
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """, (cutoff,)).fetchall()

            # Find repeated similar tasks
            task_attempts = defaultdict(list)

            for msg in messages:
                content = msg['content']
                # Skip IDE metadata events (file opens, selections — not real tasks)
                if content and re.match(r'^\s*<(ide_\w+|system-reminder)>', content):
                    continue
                # Normalize content for grouping
                normalized = self._normalize_task(content)
                if normalized:
                    task_attempts[normalized].append({
                        'content': content,
                        'timestamp': msg['timestamp'],
                        'project': msg['project_context'],
                    })

            # Find tasks with multiple attempts
            for normalized, attempts in task_attempts.items():
                if len(attempts) >= self.MIN_OCCURRENCES:
                    # This task was attempted multiple times - likely a failure pattern
                    domain = self._detect_domain(attempts[0]['content'])
                    keywords = self._extract_keywords(attempts[0]['content'])

                    # Calculate confidence based on retry count
                    confidence = min(0.95, self.LOW_CONFIDENCE + (len(attempts) * 0.15))

                    # Generate landmine text
                    landmine = f"⚠️ This task failed {len(attempts)} times recently: {normalized[:60]}..."

                    pattern = FailurePattern(
                        pattern_id=f"dialogue_{hash(normalized) % 1000000:06d}",
                        domain=domain,
                        description=f"Task attempted {len(attempts)} times: {normalized}",
                        occurrence_count=len(attempts),
                        confidence=confidence,
                        landmine_text=landmine,
                        keywords=keywords,
                        sources=["dialogue_mirror"],
                        last_seen=attempts[0]['timestamp'],
                    )
                    patterns.append(pattern)

        except Exception as e:
            logger.warning(f"Error analyzing dialogue: {e}")

        return patterns

    def _get_miswiring_patterns(self) -> List[FailurePattern]:
        """Get failure patterns from HindsightValidator's miswiring learnings."""
        patterns = []

        if not self.hindsight_validator:
            return patterns

        try:
            from memory.db_utils import unified_table as _ut
            _t_mw = _ut(".hindsight_validator.db", "miswiring_learnings")
            with sqlite3.connect(self.hindsight_validator.db_path) as conn:
                conn.row_factory = sqlite3.Row

                # Get recent miswiring learnings
                misw = conn.execute(f"""
                    SELECT * FROM {_t_mw}
                    ORDER BY created_at DESC
                    LIMIT 50
                """).fetchall()

            import json
            for m in misw:
                # Convert miswiring to failure pattern
                title = m['title']
                symptom = m['symptom']
                correct = m['correct_approach']

                # Generate landmine from miswiring
                landmine = f"💣 MISWIRING: {symptom} → Correct: {correct}"

                domain = self._detect_domain(f"{title} {symptom}")
                keywords = self._extract_keywords(f"{title} {symptom} {correct}")

                pattern = FailurePattern(
                    pattern_id=f"miswiring_{m['original_win_id']}",
                    domain=domain,
                    description=f"Miswiring: {title}",
                    occurrence_count=1,
                    confidence=self.HIGH_CONFIDENCE,  # Miswirings are confirmed failures
                    landmine_text=landmine,
                    keywords=keywords,
                    sources=["hindsight_validator"],
                    last_seen=m['created_at'],
                )
                patterns.append(pattern)

        except Exception as e:
            logger.debug(f"Error getting miswiring patterns: {e}")

        return patterns

    def _get_miswiring_for_domain(self, domain: str) -> List[str]:
        """Get miswiring landmine texts for a specific domain."""
        if not self.hindsight_validator:
            return []

        try:
            from memory.db_utils import unified_table as _ut
            _t_mw = _ut(".hindsight_validator.db", "miswiring_learnings")
            with sqlite3.connect(self.hindsight_validator.db_path) as conn:
                rows = conn.execute(f"""
                    SELECT symptom, root_cause, correct_approach
                    FROM {_t_mw}
                    ORDER BY created_at DESC
                    LIMIT 20
                """).fetchall()

            # Filter by domain keywords
            domain_keywords = self._get_domain_keywords(domain)
            landmines = []

            for row in rows:
                text = f"{row[0]} {row[1]} {row[2]}".lower()
                if any(kw in text for kw in domain_keywords):
                    landmines.append(f"💣 {row[0]} → {row[2]}")

            return landmines

        except Exception:
            return []

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def _normalize_task(self, content: str) -> str:
        """Normalize task content for grouping similar requests."""
        if not content:
            return ""

        # Skip IDE metadata events — these are file-open notifications, not task attempts
        if re.match(r'^\s*<ide_\w+>', content):
            return ""

        # Skip system reminders
        if re.match(r'^\s*<system-reminder>', content):
            return ""

        # Skip short/trivial messages — not real task attempts
        # "test", "ok", "go ahead", "yes" etc. are conversational, not tasks
        stripped = content.strip()
        if len(stripped) < 20:
            return ""

        # Remove timestamps, IDs, specific values
        normalized = content.lower()
        normalized = re.sub(r'\d+', 'N', normalized)  # Replace numbers
        normalized = re.sub(r'[a-f0-9]{8,}', 'ID', normalized)  # Replace hex IDs
        normalized = re.sub(r'\s+', ' ', normalized)  # Normalize whitespace

        # Extract key action words
        actions = re.findall(r'\b(fix|deploy|add|create|update|implement|configure|debug|test|check)\b', normalized)
        objects = re.findall(r'\b(bug|error|issue|feature|api|database|server|config|test|function|component)\b', normalized)

        if actions and objects:
            result = f"{' '.join(actions[:2])} {' '.join(objects[:3])}"
            # Require at least 2 distinct words to avoid "test test" false grouping
            unique_words = set(result.split())
            if len(unique_words) < 2:
                return ""
            return result

        # No action+object = not a task attempt (conversational, feedback, etc.)
        # Only real task requests should be tracked as potential failures
        return ""

    def _extract_keywords(self, text: str) -> List[str]:
        """Extract significant keywords from text."""
        if not text:
            return []

        # Common technical keywords
        keywords = re.findall(r'\b(async|await|docker|deploy|api|database|db|redis|celery|webhook|django|react|aws|ecs|lambda|terraform|production|staging)\b', text.lower())

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                unique.append(kw)

        return unique[:10]

    def _detect_domain(self, text: str) -> str:
        """Detect domain from text using keyword matching."""
        text_lower = text.lower()

        domain_keywords = {
            "async_python": ["async", "await", "asyncio", "coroutine", "to_thread"],
            "docker_ecs": ["docker", "container", "ecs", "fargate", "compose"],
            "aws_infrastructure": ["aws", "terraform", "lambda", "ec2", "s3", "iam"],
            "database": ["database", "postgres", "mysql", "redis", "query", "migration"],
            "django_backend": ["django", "celery", "gunicorn", "wsgi", "drf"],
            "webrtc_livekit": ["webrtc", "livekit", "turn", "stun", "websocket"],
            "frontend_react": ["react", "component", "hook", "state", "redux"],
            "voice_pipeline": ["voice", "tts", "stt", "whisper", "audio"],
            "memory_system": ["memory", "context", "synaptic", "webhook", "injection"],
        }

        for domain, keywords in domain_keywords.items():
            if any(kw in text_lower for kw in keywords):
                return domain

        return "general"

    def _get_domain_keywords(self, domain: str) -> List[str]:
        """Get keywords for a domain."""
        domain_keywords = {
            "async_python": ["async", "await", "asyncio", "coroutine"],
            "docker_ecs": ["docker", "container", "ecs", "fargate"],
            "aws_infrastructure": ["aws", "terraform", "lambda", "ec2"],
            "database": ["database", "postgres", "mysql", "redis"],
            "django_backend": ["django", "celery", "gunicorn"],
            "webrtc_livekit": ["webrtc", "livekit", "turn"],
            "frontend_react": ["react", "component", "hook"],
            "voice_pipeline": ["voice", "tts", "stt"],
            "memory_system": ["memory", "context", "synaptic"],
        }
        return domain_keywords.get(domain, [domain])

    def _is_high_yield(self, pattern: FailurePattern) -> bool:
        """Check if pattern is high-yield (worth warning about)."""
        # High yield if:
        # 1. High confidence (>= 0.8) - confirmed failures
        if pattern.confidence >= self.HIGH_CONFIDENCE:
            return True

        # 2. Multiple occurrences with medium confidence
        if pattern.occurrence_count >= 3 and pattern.confidence >= self.MEDIUM_CONFIDENCE:
            return True

        # 3. From miswiring (always high yield)
        if "hindsight_validator" in pattern.sources:
            return True

        return False

    def _merge_similar_patterns(self, patterns: List[FailurePattern]) -> List[FailurePattern]:
        """Merge similar patterns to avoid duplicates."""
        if not patterns:
            return []

        # Group by domain and similar keywords
        groups = defaultdict(list)
        for p in patterns:
            key = (p.domain, tuple(sorted(p.keywords[:3])))
            groups[key].append(p)

        merged = []
        for (domain, _), group in groups.items():
            if len(group) == 1:
                merged.append(group[0])
            else:
                # Merge into highest confidence pattern
                best = max(group, key=lambda x: (x.confidence, x.occurrence_count))

                # Aggregate counts and sources
                total_count = sum(p.occurrence_count for p in group)
                all_sources = list(set(s for p in group for s in p.sources))

                merged.append(FailurePattern(
                    pattern_id=best.pattern_id,
                    domain=domain,
                    description=best.description,
                    occurrence_count=total_count,
                    confidence=best.confidence,
                    landmine_text=best.landmine_text,
                    keywords=best.keywords,
                    sources=all_sources,
                    last_seen=best.last_seen,
                ))

        return merged

    def _store_pattern(self, pattern: FailurePattern):
        """Store or update a failure pattern in the database."""
        import json

        now = datetime.utcnow().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            # Check if exists
            existing = conn.execute(
                f"SELECT occurrence_count, confidence FROM {_t_fail('failure_patterns')} WHERE pattern_id = ?",
                (pattern.pattern_id,)
            ).fetchone()

            if existing:
                # Update
                new_count = max(existing[0], pattern.occurrence_count)
                new_confidence = max(existing[1], pattern.confidence)

                conn.execute(f"""
                    UPDATE {_t_fail('failure_patterns')} SET
                        occurrence_count = ?,
                        confidence = ?,
                        sources_json = ?,
                        last_seen = ?,
                        updated_at = ?
                    WHERE pattern_id = ?
                """, (
                    new_count,
                    new_confidence,
                    json.dumps(pattern.sources),
                    pattern.last_seen or now,
                    now,
                    pattern.pattern_id,
                ))
            else:
                # Insert
                conn.execute(f"""
                    INSERT INTO {_t_fail('failure_patterns')} (
                        pattern_id, domain, description, occurrence_count,
                        confidence, landmine_text, keywords_json, sources_json,
                        last_seen, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pattern.pattern_id,
                    pattern.domain,
                    pattern.description,
                    pattern.occurrence_count,
                    pattern.confidence,
                    pattern.landmine_text,
                    json.dumps(pattern.keywords),
                    json.dumps(pattern.sources),
                    pattern.last_seen or now,
                    now,
                    now,
                ))

    def _get_relevant_patterns(self, keywords: List[str], domain: str, limit: int) -> List[FailurePattern]:
        """Get patterns relevant to keywords and domain."""
        import json

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # First try domain match
            rows = conn.execute(f"""
                SELECT * FROM {_t_fail('failure_patterns')}
                WHERE domain = ?
                ORDER BY confidence DESC, occurrence_count DESC
                LIMIT ?
            """, (domain, limit)).fetchall()

            patterns = []
            for row in rows:
                patterns.append(FailurePattern(
                    pattern_id=row['pattern_id'],
                    domain=row['domain'],
                    description=row['description'],
                    occurrence_count=row['occurrence_count'],
                    confidence=row['confidence'],
                    landmine_text=row['landmine_text'],
                    keywords=json.loads(row['keywords_json'] or '[]'),
                    sources=json.loads(row['sources_json'] or '[]'),
                    last_seen=row['last_seen'] or '',
                ))

            # If not enough, try keyword search
            if len(patterns) < limit and keywords:
                keyword_pattern = '%' + '%'.join(keywords[:3]) + '%'
                more_rows = conn.execute(f"""
                    SELECT * FROM {_t_fail('failure_patterns')}
                    WHERE keywords_json LIKE ? AND pattern_id NOT IN (
                        SELECT pattern_id FROM {_t_fail('failure_patterns')} WHERE domain = ?
                    )
                    ORDER BY confidence DESC
                    LIMIT ?
                """, (keyword_pattern, domain, limit - len(patterns))).fetchall()

                for row in more_rows:
                    patterns.append(FailurePattern(
                        pattern_id=row['pattern_id'],
                        domain=row['domain'],
                        description=row['description'],
                        occurrence_count=row['occurrence_count'],
                        confidence=row['confidence'],
                        landmine_text=row['landmine_text'],
                        keywords=json.loads(row['keywords_json'] or '[]'),
                        sources=json.loads(row['sources_json'] or '[]'),
                        last_seen=row['last_seen'] or '',
                    ))

            return patterns


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_analyzer: Optional[FailurePatternAnalyzer] = None


def get_failure_pattern_analyzer() -> FailurePatternAnalyzer:
    """Get the singleton FailurePatternAnalyzer instance."""
    global _analyzer
    if _analyzer is None:
        _analyzer = FailurePatternAnalyzer()
    return _analyzer


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("=" * 60)
        print("FAILURE PATTERN ANALYZER")
        print("Intelligent hindsight for webhook LANDMINE warnings")
        print("=" * 60)
        print()
        print("Usage:")
        print("  python failure_pattern_analyzer.py analyze [hours]   # Analyze recent dialogue")
        print("  python failure_pattern_analyzer.py landmines <task>  # Get landmines for task")
        print("  python failure_pattern_analyzer.py list [domain]     # List high-yield patterns")
        print("  python failure_pattern_analyzer.py status            # Show system status")
        sys.exit(0)

    cmd = sys.argv[1].lower()
    analyzer = get_failure_pattern_analyzer()

    if cmd == "analyze":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        print(f"Analyzing dialogue for failure patterns (last {hours}h)...")
        patterns = analyzer.analyze_for_patterns(hours_back=hours)
        print(f"Found {len(patterns)} high-yield failure patterns:")
        for p in patterns:
            print(f"  [{p.confidence:.0%}] {p.domain}: {p.description[:60]}...")
            print(f"    LANDMINE: {p.landmine_text[:80]}...")
            print()

    elif cmd == "landmines":
        if len(sys.argv) < 3:
            print("Usage: python failure_pattern_analyzer.py landmines <task>")
            sys.exit(1)
        task = " ".join(sys.argv[2:])
        print(f"Getting LANDMINE warnings for: {task[:50]}...")
        landmines = analyzer.get_landmines_for_task(task)
        if landmines:
            print(f"\nFound {len(landmines)} LANDMINE warnings:")
            for lm in landmines:
                print(f"  {lm}")
        else:
            print("No relevant LANDMINE warnings found")

    elif cmd == "list":
        domain = sys.argv[2] if len(sys.argv) > 2 else None
        patterns = analyzer.get_high_yield_failures(domain=domain)
        print(f"High-yield failure patterns{f' for {domain}' if domain else ''}:")
        for p in patterns:
            print(f"  [{p.confidence:.0%}] x{p.occurrence_count} | {p.domain}")
            print(f"    {p.landmine_text[:80]}...")
            print()

    elif cmd == "status":
        print("FAILURE PATTERN ANALYZER STATUS")
        print("-" * 40)
        print(f"Database: {analyzer.db_path}")
        print(f"DialogueMirror: {'Available' if analyzer.dialogue_mirror else 'Not available'}")
        print(f"TemporalValidator: {'Available' if analyzer.temporal_validator else 'Not available'}")
        print(f"HindsightValidator: {'Available' if analyzer.hindsight_validator else 'Not available'}")

        patterns = analyzer.get_high_yield_failures()
        print(f"\nStored patterns: {len(patterns)}")
        if patterns:
            domains = Counter(p.domain for p in patterns)
            print("By domain:")
            for d, count in domains.most_common():
                print(f"  {d}: {count}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
