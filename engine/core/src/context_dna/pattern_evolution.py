#!/usr/bin/env python3
"""
AUTONOMOUS PATTERN EVOLUTION ENGINE

This module enables Context DNA to AUTOMATICALLY discover and learn new
objective success patterns without human intervention.

HOW IT WORKS:
1. The helper agent monitors the work_log (DNA) continuously
2. When successes are CONFIRMED (by user or high-confidence system signals),
   the evolution engine extracts patterns from those successes
3. Patterns that appear repeatedly (across sessions/projects) get promoted
4. New patterns are added to the learned_patterns database automatically
5. The system gets smarter with every coding session

EVOLUTION TRIGGERS:
- User confirms success ("that worked!", "perfect", etc.)
- High-confidence system success (exit 0 + no subsequent error)
- Pattern appears 3+ times across different contexts
- Manual discovery via `pattern_manager.py discover`

PATTERN LIFECYCLE:
1. CANDIDATE: First seen, not yet trusted (confidence < 0.5)
2. EMERGING: Seen 2-3 times, gaining confidence (0.5-0.7)
3. ESTABLISHED: Proven pattern, used in detection (0.7+)
4. STATIC: Promoted to objective_success.py (curated)

Usage:
    from memory.pattern_evolution import PatternEvolutionEngine

    engine = PatternEvolutionEngine()
    engine.evolve()  # Run one evolution cycle
    engine.run_daemon()  # Run continuously in background
"""

import re
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

# Ensure imports work
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import config for centralized paths
try:
    from context_dna.config import get_db_path, ensure_user_data_dir
    # Ensure user data directory exists before getting db path
    ensure_user_data_dir()
    EVOLUTION_DB = get_db_path()
except ImportError:
    # Fallback for standalone usage
    EVOLUTION_DB = Path(__file__).parent / ".pattern_evolution.db"

# Import components - try context_dna first, then memory/
try:
    from context_dna.objective_success import (
        SYSTEM_SUCCESS_PATTERNS,
        discover_new_patterns,
        ObjectiveSuccessDetector,
        ObjectiveSuccess,
    )
    OBJECTIVE_SUCCESS_AVAILABLE = True
except ImportError:
    try:
        from memory.objective_success import (
            SYSTEM_SUCCESS_PATTERNS,
            discover_new_patterns,
            ObjectiveSuccessDetector,
            ObjectiveSuccess,
        )
        OBJECTIVE_SUCCESS_AVAILABLE = True
    except ImportError:
        OBJECTIVE_SUCCESS_AVAILABLE = False
        SYSTEM_SUCCESS_PATTERNS = []

try:
    from context_dna.pattern_registry import EvolvingPatternRegistry
    PATTERN_REGISTRY_AVAILABLE = True
except ImportError:
    try:
        from memory.pattern_registry import EvolvingPatternRegistry
        PATTERN_REGISTRY_AVAILABLE = True
    except ImportError:
        PATTERN_REGISTRY_AVAILABLE = False

try:
    from context_dna.architecture_enhancer import work_log
    WORK_LOG_AVAILABLE = True
except ImportError:
    try:
        from memory.architecture_enhancer import work_log
        WORK_LOG_AVAILABLE = True
    except ImportError:
        WORK_LOG_AVAILABLE = False
        work_log = None


@dataclass
class PatternCandidate:
    """A candidate pattern being evaluated for promotion."""
    regex: str
    category: str
    confidence: float
    occurrences: int
    first_seen: str
    last_seen: str
    examples: List[str]
    promoted: bool = False


class PatternEvolutionEngine:
    """
    Autonomous pattern evolution engine.

    This engine continuously analyzes the work log to discover new
    success patterns and automatically adds them to the detection system.
    """

    def __init__(self):
        self.db = self._init_db()
        self.registry = EvolvingPatternRegistry() if PATTERN_REGISTRY_AVAILABLE else None

        # Configuration
        self.min_occurrences_to_promote = 3  # Need 3+ sightings
        self.promotion_confidence = 0.7  # Confidence threshold for promotion
        self.discovery_hours = 168  # Look back 1 week

    def _init_db(self) -> sqlite3.Connection:
        """Initialize the evolution database."""
        db = sqlite3.connect(str(EVOLUTION_DB), check_same_thread=False)

        # Pattern candidates (discovery → promotion lifecycle)
        db.execute("""
            CREATE TABLE IF NOT EXISTS pattern_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                regex TEXT UNIQUE,
                category TEXT,
                confidence REAL,
                occurrences INTEGER DEFAULT 1,
                first_seen TEXT,
                last_seen TEXT,
                examples TEXT,  -- JSON array
                promoted INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Evolution event log
        db.execute("""
            CREATE TABLE IF NOT EXISTS evolution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                pattern TEXT,
                details TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Pattern exclusions (patterns removed from active detection)
        db.execute("""
            CREATE TABLE IF NOT EXISTS pattern_exclusions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                regex TEXT UNIQUE,
                reason TEXT,
                risk_score REAL,
                risk_level TEXT,
                excluded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                excluded_by TEXT DEFAULT 'user'
            )
        """)

        # User feedback for pattern quality tracking
        db.execute("""
            CREATE TABLE IF NOT EXISTS pattern_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                regex TEXT,
                was_false_positive INTEGER,
                context TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Cached risk analysis results
        db.execute("""
            CREATE TABLE IF NOT EXISTS pattern_risk_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                regex TEXT UNIQUE,
                risk_score REAL,
                risk_level TEXT,
                issues TEXT,
                analyzed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Pattern outcomes - tracks ACTUAL success/failure AFTER pattern matched
        # This enables experience-based risk weighting
        db.execute("""
            CREATE TABLE IF NOT EXISTS pattern_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                regex TEXT NOT NULL,
                matched_text TEXT,
                outcome TEXT NOT NULL,  -- 'confirmed_success', 'false_positive', 'uncertain'
                context TEXT,           -- what was happening when pattern fired
                follow_up_text TEXT,    -- what happened after (error message, success confirmation)
                session_id TEXT,        -- links related outcomes
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Index for fast outcome queries by pattern
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_outcomes_regex
            ON pattern_outcomes(regex)
        """)

        db.commit()
        return db

    def evolve(self) -> Dict:
        """
        Run one evolution cycle.

        This:
        1. Discovers new pattern candidates from work log
        2. Updates occurrence counts for existing candidates
        3. Promotes candidates that meet threshold
        4. Returns summary of evolution activity

        Returns:
            Dict with evolution results
        """
        results = {
            "candidates_discovered": 0,
            "candidates_updated": 0,
            "patterns_promoted": 0,
            "timestamp": datetime.now().isoformat()
        }

        if not WORK_LOG_AVAILABLE:
            results["error"] = "Work log not available"
            return results

        # Step 1: Get recent confirmed successes
        confirmed_successes = self._get_confirmed_successes()

        # Step 2: Extract pattern candidates from successes
        for success in confirmed_successes:
            candidates = self._extract_candidates(success)
            for candidate in candidates:
                if self._is_new_candidate(candidate):
                    self._add_candidate(candidate)
                    results["candidates_discovered"] += 1
                else:
                    self._update_candidate(candidate)
                    results["candidates_updated"] += 1

        # Step 3: Discover patterns from raw work log
        discovered = discover_new_patterns(
            work_log.get_recent_entries(hours=self.discovery_hours, include_processed=True),
            min_frequency=2
        )

        for d in discovered:
            candidate = PatternCandidate(
                regex=d["suggested_regex"],
                category="discovered",
                confidence=0.5,  # Start low
                occurrences=d["count"],
                first_seen=datetime.now().isoformat(),
                last_seen=datetime.now().isoformat(),
                examples=d.get("examples", [])[:3]
            )
            if self._is_new_candidate(candidate):
                self._add_candidate(candidate)
                results["candidates_discovered"] += 1

        # Step 4: Promote qualifying candidates
        promoted = self._promote_candidates()
        results["patterns_promoted"] = len(promoted)

        # Log evolution event
        self._log_event("evolution_cycle", None, json.dumps(results))

        return results

    def _get_confirmed_successes(self) -> List[ObjectiveSuccess]:
        """Get confirmed successes from recent work log."""
        if not OBJECTIVE_SUCCESS_AVAILABLE:
            return []

        entries = work_log.get_recent_entries(hours=24, include_processed=True)
        detector = ObjectiveSuccessDetector()
        return detector.analyze_entries(entries)

    def _extract_candidates(self, success: ObjectiveSuccess) -> List[PatternCandidate]:
        """Extract pattern candidates from a confirmed success."""
        candidates = []
        task = success.task.lower()

        # Look for success-indicating phrases
        success_indicators = [
            (r'\b(success|succeeded|successful)\w*\b', 'success_variant'),
            (r'\b(complete|completed|completion)\b', 'completion_variant'),
            (r'\b(pass|passed|passing)\b', 'pass_variant'),
            (r'\b(done|finished|ready)\b', 'done_variant'),
            (r'\b(built|compiled|bundled)\b', 'build_variant'),
            (r'\b(deployed|published|released)\b', 'deploy_variant'),
            (r'\b(created|generated|produced)\b', 'create_variant'),
            (r'\b(installed|configured|enabled)\b', 'setup_variant'),
            (r'\b(healthy|running|active)\b', 'health_variant'),
            (r'\b(200|201|202|204)\b', 'http_success_code'),
            (r'\bexit[_ ]?code[:\s]*0\b', 'exit_success'),
            (r'✓|✅|🎉|👍|🚀', 'emoji_success'),
        ]

        for pattern, category in success_indicators:
            if re.search(pattern, task, re.I):
                # Extract the specific phrase that matched
                match = re.search(pattern, task, re.I)
                if match:
                    # Create a candidate pattern from the context
                    context_start = max(0, match.start() - 20)
                    context_end = min(len(task), match.end() + 20)
                    context = task[context_start:context_end]

                    # Generate regex from context
                    regex = self._generate_regex_from_context(context, match.group())

                    if regex and not self._matches_existing_pattern(regex):
                        candidates.append(PatternCandidate(
                            regex=regex,
                            category=category,
                            confidence=success.confidence * 0.8,  # Discount slightly
                            occurrences=1,
                            first_seen=success.timestamp,
                            last_seen=success.timestamp,
                            examples=[task[:100]]
                        ))

        return candidates

    def _generate_regex_from_context(self, context: str, match: str) -> Optional[str]:
        """Generate a regex pattern from context around a match."""
        # Escape special characters
        escaped = re.escape(match.lower())

        # Add word boundaries
        regex = r'\b' + escaped + r'\b'

        # Validate it's a reasonable pattern
        if len(regex) < 5 or len(regex) > 100:
            return None

        return regex

    def _matches_existing_pattern(self, regex: str) -> bool:
        """Check if pattern matches existing static patterns."""
        for existing, _, _ in SYSTEM_SUCCESS_PATTERNS:
            if existing == regex:
                return True
            # Check if they're functionally equivalent
            try:
                test_text = "test success test"
                existing_matches = bool(re.search(existing, test_text, re.I))
                new_matches = bool(re.search(regex, test_text, re.I))
                if existing_matches and new_matches:
                    return True
            except re.error:
                continue
        return False

    def _is_new_candidate(self, candidate: PatternCandidate) -> bool:
        """Check if this is a new candidate."""
        cursor = self.db.execute(
            "SELECT id FROM pattern_candidates WHERE regex = ?",
            (candidate.regex,)
        )
        return cursor.fetchone() is None

    def _add_candidate(self, candidate: PatternCandidate):
        """Add a new candidate to the database."""
        self.db.execute("""
            INSERT INTO pattern_candidates
            (regex, category, confidence, occurrences, first_seen, last_seen, examples)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            candidate.regex,
            candidate.category,
            candidate.confidence,
            candidate.occurrences,
            candidate.first_seen,
            candidate.last_seen,
            json.dumps(candidate.examples)
        ))
        self.db.commit()

        self._log_event("candidate_added", candidate.regex, f"Category: {candidate.category}")

    def _update_candidate(self, candidate: PatternCandidate):
        """Update an existing candidate's occurrence count and confidence."""
        self.db.execute("""
            UPDATE pattern_candidates
            SET occurrences = occurrences + 1,
                confidence = MIN(0.95, confidence + 0.1),
                last_seen = ?
            WHERE regex = ?
        """, (candidate.last_seen, candidate.regex))
        self.db.commit()

    def _promote_candidates(self) -> List[str]:
        """Promote candidates that meet the threshold."""
        promoted = []

        cursor = self.db.execute("""
            SELECT regex, category, confidence, occurrences
            FROM pattern_candidates
            WHERE promoted = 0
              AND occurrences >= ?
              AND confidence >= ?
        """, (self.min_occurrences_to_promote, self.promotion_confidence))

        for row in cursor.fetchall():
            regex, category, confidence, occurrences = row

            # Add to pattern registry
            if self.registry:
                try:
                    self.registry._store_pattern(regex, confidence, category, "evolved")
                    promoted.append(regex)

                    # Mark as promoted
                    self.db.execute(
                        "UPDATE pattern_candidates SET promoted = 1 WHERE regex = ?",
                        (regex,)
                    )
                    self.db.commit()

                    self._log_event("pattern_promoted", regex,
                        f"Category: {category}, Confidence: {confidence:.2f}, Occurrences: {occurrences}")

                    print(f"🧬 EVOLVED: New pattern promoted - [{confidence:.2f}] {regex[:50]}...")

                except Exception as e:
                    self._log_event("promotion_failed", regex, str(e))

        return promoted

    def _log_event(self, event_type: str, pattern: Optional[str], details: str):
        """Log an evolution event."""
        self.db.execute("""
            INSERT INTO evolution_log (event_type, pattern, details)
            VALUES (?, ?, ?)
        """, (event_type, pattern, details))
        self.db.commit()

    def get_candidates(self, min_occurrences: int = 1) -> List[Dict]:
        """Get current pattern candidates."""
        cursor = self.db.execute("""
            SELECT regex, category, confidence, occurrences, first_seen, last_seen, promoted
            FROM pattern_candidates
            WHERE occurrences >= ?
            ORDER BY occurrences DESC, confidence DESC
        """, (min_occurrences,))

        return [
            {
                "regex": row[0],
                "category": row[1],
                "confidence": row[2],
                "occurrences": row[3],
                "first_seen": row[4],
                "last_seen": row[5],
                "promoted": bool(row[6])
            }
            for row in cursor.fetchall()
        ]

    def get_evolution_stats(self) -> Dict:
        """Get evolution statistics."""
        cursor = self.db.execute("""
            SELECT
                COUNT(*) as total_candidates,
                SUM(CASE WHEN promoted = 1 THEN 1 ELSE 0 END) as promoted,
                SUM(CASE WHEN occurrences >= 3 THEN 1 ELSE 0 END) as qualifying,
                AVG(confidence) as avg_confidence
            FROM pattern_candidates
        """)
        row = cursor.fetchone()

        # Count evolution cycles
        cycles = self.db.execute(
            "SELECT COUNT(*) FROM evolution_log WHERE event_type = 'evolution_cycle'"
        ).fetchone()[0]

        # Count exclusions
        exclusions = self.db.execute(
            "SELECT COUNT(*) FROM pattern_exclusions"
        ).fetchone()[0]

        return {
            "total_candidates": row[0] or 0,
            "promoted_patterns": row[1] or 0,
            "qualifying_candidates": row[2] or 0,
            "avg_confidence": row[3] or 0,
            "evolution_cycles": cycles,
            "excluded_patterns": exclusions,
            "static_patterns": len(SYSTEM_SUCCESS_PATTERNS),
            "learned_patterns": len(self.registry.get_learned_patterns()) if self.registry else 0,
        }

    # =========================================================================
    # PATTERN EXCLUSION MANAGEMENT
    # =========================================================================

    def exclude_pattern(self, regex: str, reason: str, risk_score: float = 0.0,
                       risk_level: str = "medium") -> bool:
        """
        Exclude a pattern from active detection.

        Exclusion is reversible - patterns can be restored later.

        Args:
            regex: The regex pattern to exclude
            reason: Why this pattern is being excluded
            risk_score: Risk score from analysis (0.0-1.0)
            risk_level: Risk level ('low', 'medium', 'high', 'critical')

        Returns:
            True if excluded successfully
        """
        try:
            self.db.execute("""
                INSERT OR REPLACE INTO pattern_exclusions
                (regex, reason, risk_score, risk_level, excluded_at)
                VALUES (?, ?, ?, ?, ?)
            """, (regex, reason, risk_score, risk_level, datetime.now().isoformat()))
            self.db.commit()

            self._log_event("pattern_excluded", regex,
                f"Reason: {reason}, Risk: {risk_score:.2f} ({risk_level})")

            print(f"🚫 Excluded pattern: {regex[:50]}...")
            return True

        except Exception as e:
            print(f"Exclusion error: {e}")
            return False

    def restore_pattern(self, regex: str) -> bool:
        """
        Restore a previously excluded pattern.

        Args:
            regex: The regex pattern to restore

        Returns:
            True if restored successfully
        """
        try:
            self.db.execute(
                "DELETE FROM pattern_exclusions WHERE regex = ?",
                (regex,)
            )
            self.db.commit()

            self._log_event("pattern_restored", regex, "Pattern restored to active")
            print(f"✅ Restored pattern: {regex[:50]}...")
            return True

        except Exception:
            return False

    def get_exclusions(self) -> List[Dict]:
        """Get all excluded patterns."""
        cursor = self.db.execute("""
            SELECT regex, reason, risk_score, risk_level, excluded_at, excluded_by
            FROM pattern_exclusions
            ORDER BY excluded_at DESC
        """)

        return [
            {
                "regex": row[0],
                "reason": row[1],
                "risk_score": row[2],
                "risk_level": row[3],
                "excluded_at": row[4],
                "excluded_by": row[5],
            }
            for row in cursor.fetchall()
        ]

    def is_excluded(self, regex: str) -> bool:
        """Check if a pattern is excluded."""
        cursor = self.db.execute(
            "SELECT 1 FROM pattern_exclusions WHERE regex = ?",
            (regex,)
        )
        return cursor.fetchone() is not None

    def record_feedback(self, regex: str, was_false_positive: bool, context: str = ""):
        """
        Record user feedback about a pattern match.

        This helps the system learn which patterns need review.
        """
        try:
            self.db.execute("""
                INSERT INTO pattern_feedback (regex, was_false_positive, context)
                VALUES (?, ?, ?)
            """, (regex, 1 if was_false_positive else 0, context))
            self.db.commit()
        except Exception:
            pass

    def get_false_positive_stats(self) -> Dict[str, Dict]:
        """Get false positive feedback counts by pattern."""
        stats = {}
        cursor = self.db.execute("""
            SELECT regex, SUM(was_false_positive), COUNT(*)
            FROM pattern_feedback
            GROUP BY regex
            ORDER BY SUM(was_false_positive) DESC
        """)
        for row in cursor.fetchall():
            stats[row[0]] = {
                "false_positives": row[1] or 0,
                "total_feedback": row[2] or 0,
                "fp_rate": (row[1] or 0) / (row[2] or 1)
            }
        return stats

    def cache_risk_analysis(self, regex: str, risk_score: float, risk_level: str, issues: List[str]):
        """Cache risk analysis results for a pattern."""
        try:
            self.db.execute("""
                INSERT OR REPLACE INTO pattern_risk_cache
                (regex, risk_score, risk_level, issues, analyzed_at)
                VALUES (?, ?, ?, ?, ?)
            """, (regex, risk_score, risk_level, json.dumps(issues), datetime.now().isoformat()))
            self.db.commit()
        except Exception:
            pass

    def get_cached_risk(self, regex: str) -> Optional[Dict]:
        """Get cached risk analysis for a pattern."""
        cursor = self.db.execute(
            "SELECT risk_score, risk_level, issues, analyzed_at FROM pattern_risk_cache WHERE regex = ?",
            (regex,)
        )
        row = cursor.fetchone()
        if row:
            return {
                "risk_score": row[0],
                "risk_level": row[1],
                "issues": json.loads(row[2]) if row[2] else [],
                "analyzed_at": row[3]
            }
        return None

    # =========================================================================
    # OUTCOME TRACKING - Experience-based pattern evaluation
    # =========================================================================

    def record_outcome(self, regex: str, outcome: str, matched_text: str = "",
                      context: str = "", follow_up_text: str = "",
                      session_id: str = "") -> bool:
        """
        Record what ACTUALLY happened after a pattern matched.

        This is the key to experience-based risk weighting. If a pattern
        frequently fires but doesn't lead to actual success, it becomes
        riskier over time.

        Args:
            regex: The pattern that matched
            outcome: 'confirmed_success', 'false_positive', or 'uncertain'
            matched_text: The text that triggered the pattern
            context: What was happening when pattern fired
            follow_up_text: What happened after (error message, etc.)
            session_id: Optional session identifier to link outcomes

        Returns:
            True if recorded successfully
        """
        valid_outcomes = ('confirmed_success', 'false_positive', 'uncertain')
        if outcome not in valid_outcomes:
            print(f"Invalid outcome: {outcome}. Use one of: {valid_outcomes}")
            return False

        try:
            self.db.execute("""
                INSERT INTO pattern_outcomes
                (regex, matched_text, outcome, context, follow_up_text, session_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (regex, matched_text, outcome, context, follow_up_text, session_id))
            self.db.commit()

            self._log_event("outcome_recorded", regex,
                f"Outcome: {outcome}, Context: {context[:50] if context else 'none'}")

            return True

        except Exception as e:
            print(f"Failed to record outcome: {e}")
            return False

    def get_outcome_stats(self, regex: str = None) -> Dict:
        """
        Get outcome statistics for a pattern or all patterns.

        Args:
            regex: Specific pattern to check, or None for all patterns

        Returns:
            Dict with success_rate, false_positive_rate, total_outcomes, etc.
        """
        if regex:
            cursor = self.db.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'confirmed_success' THEN 1 ELSE 0 END) as successes,
                    SUM(CASE WHEN outcome = 'false_positive' THEN 1 ELSE 0 END) as false_positives,
                    SUM(CASE WHEN outcome = 'uncertain' THEN 1 ELSE 0 END) as uncertain
                FROM pattern_outcomes
                WHERE regex = ?
            """, (regex,))
        else:
            cursor = self.db.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'confirmed_success' THEN 1 ELSE 0 END) as successes,
                    SUM(CASE WHEN outcome = 'false_positive' THEN 1 ELSE 0 END) as false_positives,
                    SUM(CASE WHEN outcome = 'uncertain' THEN 1 ELSE 0 END) as uncertain
                FROM pattern_outcomes
            """)

        row = cursor.fetchone()
        total = row[0] or 0
        successes = row[1] or 0
        false_positives = row[2] or 0
        uncertain = row[3] or 0

        return {
            "total_outcomes": total,
            "confirmed_successes": successes,
            "false_positives": false_positives,
            "uncertain": uncertain,
            "success_rate": successes / total if total > 0 else None,
            "false_positive_rate": false_positives / total if total > 0 else None,
            "experience_level": self._get_experience_level(total),
        }

    def _get_experience_level(self, total_outcomes: int) -> str:
        """Categorize how much experience we have with a pattern."""
        if total_outcomes == 0:
            return "no_data"
        elif total_outcomes < 3:
            return "limited"
        elif total_outcomes < 10:
            return "moderate"
        elif total_outcomes < 50:
            return "good"
        else:
            return "extensive"

    def get_all_outcome_stats(self) -> Dict[str, Dict]:
        """Get outcome stats for all patterns that have outcome data."""
        cursor = self.db.execute("""
            SELECT DISTINCT regex FROM pattern_outcomes
        """)

        stats = {}
        for row in cursor.fetchall():
            regex = row[0]
            stats[regex] = self.get_outcome_stats(regex)

        return stats

    def get_experience_risk_modifier(self, regex: str) -> Tuple[float, str]:
        """
        Calculate risk modifier based on actual outcome history.

        Returns:
            Tuple of (risk_modifier, explanation)
            - risk_modifier: -0.3 to +0.5 adjustment to base risk score
            - explanation: Human-readable reason for the modifier
        """
        stats = self.get_outcome_stats(regex)

        if stats["total_outcomes"] == 0:
            return 0.0, "No outcome data (using heuristic risk only)"

        total = stats["total_outcomes"]
        fp_rate = stats["false_positive_rate"]
        success_rate = stats["success_rate"]
        experience = stats["experience_level"]

        # Weight the modifier based on how much data we have
        experience_weight = {
            "limited": 0.3,      # 1-2 outcomes - low confidence
            "moderate": 0.6,    # 3-9 outcomes - medium confidence
            "good": 0.85,       # 10-49 outcomes - high confidence
            "extensive": 1.0,   # 50+ outcomes - full confidence
        }.get(experience, 0.0)

        # Calculate base modifier from false positive rate
        # High FP rate = increased risk, high success rate = decreased risk
        if fp_rate >= 0.5:
            # Pattern is wrong more than half the time - major red flag
            base_modifier = 0.5
            explanation = f"High false positive rate ({fp_rate:.0%} of {total} outcomes)"
        elif fp_rate >= 0.3:
            # Significant false positive rate
            base_modifier = 0.3
            explanation = f"Moderate false positive rate ({fp_rate:.0%} of {total} outcomes)"
        elif fp_rate >= 0.1:
            # Some false positives
            base_modifier = 0.15
            explanation = f"Some false positives ({fp_rate:.0%} of {total} outcomes)"
        elif success_rate >= 0.9:
            # Very reliable pattern
            base_modifier = -0.3
            explanation = f"Highly reliable ({success_rate:.0%} success rate over {total} outcomes)"
        elif success_rate >= 0.7:
            # Reliable pattern
            base_modifier = -0.15
            explanation = f"Reliable ({success_rate:.0%} success rate over {total} outcomes)"
        else:
            base_modifier = 0.0
            explanation = f"Mixed results ({success_rate:.0%} success rate over {total} outcomes)"

        # Apply experience weight
        final_modifier = base_modifier * experience_weight

        if experience in ("limited", "moderate"):
            explanation += f" [{experience} data]"

        return final_modifier, explanation

    def get_recent_outcomes(self, regex: str = None, limit: int = 20) -> List[Dict]:
        """Get recent outcomes for a pattern or all patterns."""
        if regex:
            cursor = self.db.execute("""
                SELECT regex, outcome, matched_text, context, follow_up_text, timestamp
                FROM pattern_outcomes
                WHERE regex = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (regex, limit))
        else:
            cursor = self.db.execute("""
                SELECT regex, outcome, matched_text, context, follow_up_text, timestamp
                FROM pattern_outcomes
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))

        return [
            {
                "regex": row[0],
                "outcome": row[1],
                "matched_text": row[2],
                "context": row[3],
                "follow_up_text": row[4],
                "timestamp": row[5],
            }
            for row in cursor.fetchall()
        ]

    def get_worst_performing_patterns(self, min_outcomes: int = 3, limit: int = 10) -> List[Dict]:
        """
        Get patterns with the highest false positive rates.

        Only includes patterns with at least min_outcomes for statistical relevance.
        """
        cursor = self.db.execute("""
            SELECT
                regex,
                COUNT(*) as total,
                SUM(CASE WHEN outcome = 'false_positive' THEN 1 ELSE 0 END) as fp,
                CAST(SUM(CASE WHEN outcome = 'false_positive' THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as fp_rate
            FROM pattern_outcomes
            GROUP BY regex
            HAVING COUNT(*) >= ?
            ORDER BY fp_rate DESC, total DESC
            LIMIT ?
        """, (min_outcomes, limit))

        return [
            {
                "regex": row[0],
                "total_outcomes": row[1],
                "false_positives": row[2],
                "false_positive_rate": row[3],
            }
            for row in cursor.fetchall()
        ]

    def run_daemon(self, interval_minutes: int = 30):
        """
        Run the evolution engine as a daemon.

        This continuously evolves patterns in the background.
        Typically called by the helper agent.
        """
        import time

        print(f"🧬 Pattern Evolution Engine started (interval: {interval_minutes}m)")

        while True:
            try:
                results = self.evolve()
                if results.get("candidates_discovered", 0) > 0 or results.get("patterns_promoted", 0) > 0:
                    print(f"🧬 Evolution cycle: {results['candidates_discovered']} discovered, "
                          f"{results['patterns_promoted']} promoted")
            except Exception as e:
                print(f"Evolution error: {e}")

            time.sleep(interval_minutes * 60)


# Singleton for easy access
_evolution_engine = None

def get_evolution_engine() -> PatternEvolutionEngine:
    """Get the singleton evolution engine."""
    global _evolution_engine
    if _evolution_engine is None:
        _evolution_engine = PatternEvolutionEngine()
    return _evolution_engine


def evolve_patterns() -> Dict:
    """Convenience function to run one evolution cycle."""
    return get_evolution_engine().evolve()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Pattern Evolution Engine - Autonomous pattern learning")
        print("")
        print("Commands:")
        print("  evolve              - Run one evolution cycle")
        print("  status              - Show evolution statistics")
        print("  candidates          - List pattern candidates")
        print("  daemon [minutes]    - Run as background daemon")
        print("")
        print("The engine automatically:")
        print("  - Discovers new patterns from work log")
        print("  - Tracks pattern occurrences")
        print("  - Promotes patterns that appear 3+ times")
        print("  - Adds promoted patterns to detection system")
        sys.exit(0)

    cmd = sys.argv[1]
    engine = PatternEvolutionEngine()

    if cmd == "evolve":
        print("Running evolution cycle...")
        results = engine.evolve()
        print(f"\nResults:")
        print(f"  Candidates discovered: {results['candidates_discovered']}")
        print(f"  Candidates updated: {results['candidates_updated']}")
        print(f"  Patterns promoted: {results['patterns_promoted']}")

    elif cmd == "status":
        stats = engine.get_evolution_stats()
        print("=== Pattern Evolution Status ===")
        print(f"  Static patterns:     {stats['static_patterns']}")
        print(f"  Learned patterns:    {stats['learned_patterns']}")
        print(f"  Candidates tracking: {stats['total_candidates']}")
        print(f"  Qualifying (3+):     {stats['qualifying_candidates']}")
        print(f"  Already promoted:    {stats['promoted_patterns']}")
        print(f"  Avg confidence:      {stats['avg_confidence']:.2%}")
        print(f"  Evolution cycles:    {stats['evolution_cycles']}")

    elif cmd == "candidates":
        min_occ = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        candidates = engine.get_candidates(min_occ)
        if candidates:
            print(f"=== Pattern Candidates (min {min_occ} occurrences) ===\n")
            for c in candidates[:20]:
                status = "✅ PROMOTED" if c["promoted"] else f"👀 {c['occurrences']}x"
                print(f"  [{c['confidence']:.2f}] {status}")
                print(f"      Pattern: {c['regex'][:60]}...")
                print(f"      Category: {c['category']}")
                print()
        else:
            print("No candidates yet. Run 'evolve' to discover patterns.")

    elif cmd == "daemon":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        engine.run_daemon(interval)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
