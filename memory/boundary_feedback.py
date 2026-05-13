#!/usr/bin/env python3
"""
PROJECT BOUNDARY FEEDBACK LOOP

Learns which learnings/patterns belong to which projects by tracking:
1. Keywords in prompts → Project associations
2. Injection relevance feedback (was the injected context helpful?)
3. A/B variant performance per project context

This creates an adaptive boundary system that improves over time.

PHILOSOPHY:
Context DNA shouldn't mix ER Simulator voice stack learnings when you're
working on Context DNA's webhook system. This module learns those boundaries
from feedback signals.

Usage:
    from memory.boundary_feedback import BoundaryFeedbackLearner, get_boundary_learner

    learner = get_boundary_learner()

    # Record an injection with project context
    learner.record_injection(
        injection_id="inj_123",
        prompt="fix async boto3 in voice stack",
        detected_project="ersim-voice-stack",
        ab_variant="control",
        keywords=["async", "boto3", "voice"],
        learnings_included=["learning_456", "learning_789"]
    )

    # Later, record feedback
    learner.record_feedback(
        injection_id="inj_123",
        was_helpful=True,  # or False if wrong project context
        confidence=0.9
    )

    # Get learned boundaries
    associations = learner.get_keyword_project_associations("async boto3")
"""

import json
import sqlite3
import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field, asdict
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)

# Database for boundary feedback (separate from pattern_evolution to stay focused)
BOUNDARY_DB = Path(__file__).parent / ".context_ab_tracking.db"

# Singleton instance
_learner_instance = None


@dataclass
class InjectionRecord:
    """Record of a context injection with project boundary info."""
    injection_id: str
    prompt: str
    prompt_hash: str
    detected_project: Optional[str]
    detected_workspace: Optional[str]
    ab_variant: str
    keywords: List[str]
    learnings_included: List[str]
    risk_level: str
    session_id: str
    timestamp: str = ""
    feedback_recorded: bool = False


@dataclass
class BoundaryFeedback:
    """Feedback about whether an injection had correct project context."""
    injection_id: str
    was_helpful: bool
    project_was_correct: bool
    confidence: float
    correction_project: Optional[str] = None  # If wrong, what should it have been?
    signals: List[str] = field(default_factory=list)
    user_explicit: bool = False  # True if user explicitly said "wrong context"
    timestamp: str = ""


@dataclass
class KeywordProjectAssociation:
    """Learned association between a keyword and a project."""
    keyword: str
    project: str
    weight: float  # 0.0 to 1.0
    positive_count: int
    negative_count: int
    last_updated: str
    confidence: str  # "low", "moderate", "high", "confirmed"


@dataclass
class ProjectBoundary:
    """Learned boundary for a project."""
    project_path: str
    project_name: str
    strong_keywords: List[str]  # Keywords strongly associated
    weak_keywords: List[str]    # Keywords with weak association
    excluded_keywords: List[str]  # Keywords that indicate NOT this project
    recency_weight: float  # How recently worked on (0.0 to 1.0)
    total_injections: int
    positive_feedback_rate: float


class BoundaryFeedbackLearner:
    """
    Learns project boundaries from injection feedback.

    Creates an adaptive system that improves boundary detection over time.
    """

    # Keywords that are project-specific vs general
    GENERAL_KEYWORDS = {
        "fix", "bug", "error", "add", "update", "refactor", "test",
        "deploy", "config", "env", "async", "sync", "performance"
    }

    # Minimum feedback before considering association "confirmed"
    MIN_FEEDBACK_FOR_CONFIDENCE = {
        "low": 1,
        "moderate": 5,
        "high": 15,
        "confirmed": 30
    }

    def __init__(self):
        self.db = self._init_db()

    def _init_db(self) -> sqlite3.Connection:
        """Initialize boundary feedback tables."""
        from memory.db_utils import connect_wal
        db = connect_wal(BOUNDARY_DB, check_same_thread=False)

        # Injection records with project context
        db.execute("""
            CREATE TABLE IF NOT EXISTS boundary_injections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                injection_id TEXT UNIQUE NOT NULL,
                prompt TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                detected_project TEXT,
                detected_workspace TEXT,
                ab_variant TEXT NOT NULL,
                keywords_json TEXT,
                learnings_included_json TEXT,
                risk_level TEXT,
                session_id TEXT,
                feedback_recorded INTEGER DEFAULT 0,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feedback on injections
        db.execute("""
            CREATE TABLE IF NOT EXISTS boundary_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                injection_id TEXT NOT NULL,
                was_helpful INTEGER NOT NULL,
                project_was_correct INTEGER NOT NULL,
                confidence REAL DEFAULT 0.5,
                correction_project TEXT,
                signals_json TEXT,
                user_explicit INTEGER DEFAULT 0,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (injection_id) REFERENCES boundary_injections(injection_id)
            )
        """)

        # Learned keyword → project associations
        db.execute("""
            CREATE TABLE IF NOT EXISTS keyword_project_associations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                project TEXT NOT NULL,
                weight REAL DEFAULT 0.5,
                positive_count INTEGER DEFAULT 0,
                negative_count INTEGER DEFAULT 0,
                last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
                confidence_level TEXT DEFAULT 'low',
                UNIQUE(keyword, project)
            )
        """)

        # Project boundaries (aggregated view)
        db.execute("""
            CREATE TABLE IF NOT EXISTS project_boundaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_path TEXT UNIQUE NOT NULL,
                project_name TEXT NOT NULL,
                strong_keywords_json TEXT,
                weak_keywords_json TEXT,
                excluded_keywords_json TEXT,
                recency_weight REAL DEFAULT 0.5,
                total_injections INTEGER DEFAULT 0,
                positive_feedback_rate REAL DEFAULT 0.5,
                last_activity TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # A/B variant performance per project
        db.execute("""
            CREATE TABLE IF NOT EXISTS variant_project_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ab_variant TEXT NOT NULL,
                project TEXT NOT NULL,
                positive_count INTEGER DEFAULT 0,
                negative_count INTEGER DEFAULT 0,
                neutral_count INTEGER DEFAULT 0,
                avg_confidence REAL DEFAULT 0.5,
                last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ab_variant, project)
            )
        """)

        # Learning events log
        db.execute("""
            CREATE TABLE IF NOT EXISTS boundary_learning_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                keyword TEXT,
                project TEXT,
                old_weight REAL,
                new_weight REAL,
                reason TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes for performance
        db.execute("CREATE INDEX IF NOT EXISTS idx_bi_project ON boundary_injections(detected_project)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_bi_session ON boundary_injections(session_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_bi_prompt_hash ON boundary_injections(prompt_hash)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_kpa_keyword ON keyword_project_associations(keyword)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_kpa_project ON keyword_project_associations(project)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_bf_injection ON boundary_feedback(injection_id)")

        db.commit()
        return db

    # =========================================================================
    # RECORDING
    # =========================================================================

    def record_injection(
        self,
        injection_id: str,
        prompt: str,
        detected_project: Optional[str],
        ab_variant: str,
        keywords: List[str],
        learnings_included: List[str],
        risk_level: str = "low",
        session_id: str = "",
        detected_workspace: Optional[str] = None
    ) -> bool:
        """
        Record a context injection with its project boundary info.

        Called when an injection is generated, before feedback is known.
        """
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()

        try:
            self.db.execute("""
                INSERT OR REPLACE INTO boundary_injections
                (injection_id, prompt, prompt_hash, detected_project, detected_workspace,
                 ab_variant, keywords_json, learnings_included_json, risk_level, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                injection_id,
                prompt[:1000],  # Truncate long prompts
                prompt_hash,
                detected_project,
                detected_workspace,
                ab_variant,
                json.dumps(keywords),
                json.dumps(learnings_included),
                risk_level,
                session_id
            ))
            self.db.commit()

            # Update project recency
            if detected_project:
                self._update_project_recency(detected_project)

            return True

        except Exception as e:
            logger.error(f"Failed to record injection: {e}")
            return False

    def record_feedback(
        self,
        injection_id: str,
        was_helpful: bool,
        project_was_correct: bool = True,
        confidence: float = 0.5,
        correction_project: Optional[str] = None,
        signals: List[str] = None,
        user_explicit: bool = False
    ) -> bool:
        """
        Record feedback about an injection's relevance.

        Args:
            injection_id: The injection this feedback is for
            was_helpful: Whether the injected context was helpful
            project_was_correct: Whether the project detection was correct
            confidence: 0.0-1.0 how confident we are in this feedback
            correction_project: If wrong, what project should it have been?
            signals: What signals led to this feedback
            user_explicit: True if user explicitly provided this feedback

        Returns:
            True if recorded successfully
        """
        try:
            # Get the injection record
            cursor = self.db.execute("""
                SELECT detected_project, keywords_json, ab_variant
                FROM boundary_injections
                WHERE injection_id = ?
            """, (injection_id,))

            row = cursor.fetchone()
            if not row:
                logger.warning(f"Injection not found: {injection_id}")
                return False

            detected_project, keywords_json, ab_variant = row
            keywords = json.loads(keywords_json) if keywords_json else []

            # Record feedback
            self.db.execute("""
                INSERT INTO boundary_feedback
                (injection_id, was_helpful, project_was_correct, confidence,
                 correction_project, signals_json, user_explicit)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                injection_id,
                1 if was_helpful else 0,
                1 if project_was_correct else 0,
                confidence,
                correction_project,
                json.dumps(signals or []),
                1 if user_explicit else 0
            ))

            # Mark injection as having feedback
            self.db.execute("""
                UPDATE boundary_injections SET feedback_recorded = 1
                WHERE injection_id = ?
            """, (injection_id,))

            self.db.commit()

            # Trigger learning from this feedback
            self._learn_from_feedback(
                keywords=keywords,
                detected_project=detected_project,
                correction_project=correction_project,
                was_helpful=was_helpful,
                project_was_correct=project_was_correct,
                confidence=confidence,
                ab_variant=ab_variant
            )

            return True

        except Exception as e:
            logger.error(f"Failed to record feedback: {e}")
            return False

    # =========================================================================
    # LEARNING
    # =========================================================================

    def _learn_from_feedback(
        self,
        keywords: List[str],
        detected_project: Optional[str],
        correction_project: Optional[str],
        was_helpful: bool,
        project_was_correct: bool,
        confidence: float,
        ab_variant: str
    ):
        """
        Update learned associations based on feedback.

        This is where the magic happens - we adjust keyword→project weights
        based on whether the context was helpful.
        """
        # Calculate weight adjustment based on confidence and outcome
        if project_was_correct and was_helpful:
            adjustment = 0.1 * confidence  # Strengthen association
            outcome = "positive"
        elif not project_was_correct:
            adjustment = -0.15 * confidence  # Weaken incorrect association
            outcome = "negative"
        elif project_was_correct and not was_helpful:
            adjustment = -0.05 * confidence  # Slight weakening
            outcome = "neutral"
        else:
            adjustment = 0.0
            outcome = "neutral"

        # Update keyword associations for detected project
        if detected_project:
            for keyword in keywords:
                # Skip very general keywords
                if keyword.lower() in self.GENERAL_KEYWORDS:
                    continue

                self._update_keyword_association(
                    keyword=keyword,
                    project=detected_project,
                    adjustment=adjustment,
                    is_positive=project_was_correct and was_helpful
                )

        # If correction was provided, strengthen that association instead
        if correction_project and correction_project != detected_project:
            for keyword in keywords:
                if keyword.lower() in self.GENERAL_KEYWORDS:
                    continue

                # Strengthen correct project association
                self._update_keyword_association(
                    keyword=keyword,
                    project=correction_project,
                    adjustment=0.15 * confidence,
                    is_positive=True
                )

                # Add to excluded keywords for wrong project
                if detected_project:
                    self._add_exclusion(keyword, detected_project)

        # Update A/B variant performance for this project
        self._update_variant_performance(
            ab_variant=ab_variant,
            project=detected_project or "unknown",
            outcome=outcome,
            confidence=confidence
        )

    def _update_keyword_association(
        self,
        keyword: str,
        project: str,
        adjustment: float,
        is_positive: bool
    ):
        """Update a single keyword→project association."""
        keyword = keyword.lower().strip()

        try:
            # Get current association
            cursor = self.db.execute("""
                SELECT weight, positive_count, negative_count, confidence_level
                FROM keyword_project_associations
                WHERE keyword = ? AND project = ?
            """, (keyword, project))

            row = cursor.fetchone()

            if row:
                old_weight, pos_count, neg_count, conf_level = row
                new_weight = max(0.0, min(1.0, old_weight + adjustment))
                new_pos = pos_count + (1 if is_positive else 0)
                new_neg = neg_count + (0 if is_positive else 1)

                # Update confidence level based on total feedback
                total = new_pos + new_neg
                if total >= self.MIN_FEEDBACK_FOR_CONFIDENCE["confirmed"]:
                    new_conf = "confirmed"
                elif total >= self.MIN_FEEDBACK_FOR_CONFIDENCE["high"]:
                    new_conf = "high"
                elif total >= self.MIN_FEEDBACK_FOR_CONFIDENCE["moderate"]:
                    new_conf = "moderate"
                else:
                    new_conf = "low"

                self.db.execute("""
                    UPDATE keyword_project_associations
                    SET weight = ?, positive_count = ?, negative_count = ?,
                        confidence_level = ?, last_updated = ?
                    WHERE keyword = ? AND project = ?
                """, (new_weight, new_pos, new_neg, new_conf,
                      datetime.now().isoformat(), keyword, project))

            else:
                # Create new association
                old_weight = 0.5
                new_weight = max(0.0, min(1.0, 0.5 + adjustment))

                self.db.execute("""
                    INSERT INTO keyword_project_associations
                    (keyword, project, weight, positive_count, negative_count)
                    VALUES (?, ?, ?, ?, ?)
                """, (keyword, project, new_weight,
                      1 if is_positive else 0, 0 if is_positive else 1))

            self.db.commit()

            # Log significant changes
            if abs(adjustment) >= 0.1:
                self._log_learning(
                    "association_updated",
                    keyword=keyword,
                    project=project,
                    old_weight=old_weight,
                    new_weight=new_weight,
                    reason="feedback" if is_positive else "negative_feedback"
                )

        except Exception as e:
            logger.error(f"Failed to update keyword association: {e}")

    def _add_exclusion(self, keyword: str, project: str):
        """Add a keyword to a project's exclusion list."""
        try:
            cursor = self.db.execute("""
                SELECT excluded_keywords_json FROM project_boundaries
                WHERE project_path = ? OR project_name = ?
            """, (project, project))

            row = cursor.fetchone()
            if row:
                exclusions = json.loads(row[0]) if row[0] else []
                if keyword not in exclusions:
                    exclusions.append(keyword)
                    self.db.execute("""
                        UPDATE project_boundaries
                        SET excluded_keywords_json = ?
                        WHERE project_path = ? OR project_name = ?
                    """, (json.dumps(exclusions), project, project))
                    self.db.commit()
        except Exception as e:
            logger.error(f"Failed to add exclusion: {e}")

    def _update_variant_performance(
        self,
        ab_variant: str,
        project: str,
        outcome: str,
        confidence: float
    ):
        """Update A/B variant performance for a project."""
        try:
            cursor = self.db.execute("""
                SELECT positive_count, negative_count, neutral_count, avg_confidence
                FROM variant_project_performance
                WHERE ab_variant = ? AND project = ?
            """, (ab_variant, project))

            row = cursor.fetchone()

            if row:
                pos, neg, neu, avg_conf = row
                if outcome == "positive":
                    pos += 1
                elif outcome == "negative":
                    neg += 1
                else:
                    neu += 1

                total = pos + neg + neu
                # Running average of confidence
                new_avg = ((avg_conf * (total - 1)) + confidence) / total

                self.db.execute("""
                    UPDATE variant_project_performance
                    SET positive_count = ?, negative_count = ?, neutral_count = ?,
                        avg_confidence = ?, last_updated = ?
                    WHERE ab_variant = ? AND project = ?
                """, (pos, neg, neu, new_avg, datetime.now().isoformat(),
                      ab_variant, project))
            else:
                self.db.execute("""
                    INSERT INTO variant_project_performance
                    (ab_variant, project, positive_count, negative_count, neutral_count, avg_confidence)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (ab_variant, project,
                      1 if outcome == "positive" else 0,
                      1 if outcome == "negative" else 0,
                      1 if outcome == "neutral" else 0,
                      confidence))

            self.db.commit()

        except Exception as e:
            logger.error(f"Failed to update variant performance: {e}")

    def _update_project_recency(self, project: str):
        """Update the recency weight for a project."""
        try:
            # Check if project exists
            cursor = self.db.execute("""
                SELECT id, total_injections FROM project_boundaries
                WHERE project_path = ? OR project_name = ?
            """, (project, project))

            row = cursor.fetchone()

            if row:
                self.db.execute("""
                    UPDATE project_boundaries
                    SET recency_weight = 1.0, total_injections = total_injections + 1,
                        last_activity = ?
                    WHERE project_path = ? OR project_name = ?
                """, (datetime.now().isoformat(), project, project))
            else:
                # Create new project boundary entry
                project_name = project.split("/")[-1] if "/" in project else project
                self.db.execute("""
                    INSERT INTO project_boundaries
                    (project_path, project_name, recency_weight, total_injections)
                    VALUES (?, ?, 1.0, 1)
                """, (project, project_name))

            self.db.commit()

        except Exception as e:
            logger.error(f"Failed to update project recency: {e}")

    def _log_learning(
        self,
        event_type: str,
        keyword: str = None,
        project: str = None,
        old_weight: float = None,
        new_weight: float = None,
        reason: str = ""
    ):
        """Log a learning event."""
        try:
            self.db.execute("""
                INSERT INTO boundary_learning_log
                (event_type, keyword, project, old_weight, new_weight, reason)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (event_type, keyword, project, old_weight, new_weight, reason))
            self.db.commit()
        except Exception:
            pass  # Non-critical

    # =========================================================================
    # QUERYING
    # =========================================================================

    def get_keyword_project_associations(self, text: str) -> Dict[str, float]:
        """
        Get project associations for keywords in the given text.

        Returns dict of {project: combined_weight}
        """
        # Extract keywords from text
        words = set(re.findall(r'\b\w+\b', text.lower()))

        project_scores = defaultdict(float)

        for word in words:
            if len(word) < 3 or word in self.GENERAL_KEYWORDS:
                continue

            cursor = self.db.execute("""
                SELECT project, weight, confidence_level
                FROM keyword_project_associations
                WHERE keyword = ?
            """, (word,))

            for row in cursor.fetchall():
                project, weight, confidence = row
                # Weight by confidence level
                conf_multiplier = {
                    "confirmed": 1.0,
                    "high": 0.85,
                    "moderate": 0.65,
                    "low": 0.4
                }.get(confidence, 0.4)

                project_scores[project] += weight * conf_multiplier

        return dict(project_scores)

    def get_best_project_for_prompt(self, prompt: str) -> Tuple[Optional[str], float]:
        """
        Get the best project match for a prompt based on learned associations.

        Returns (project_name, confidence_score)
        """
        associations = self.get_keyword_project_associations(prompt)

        if not associations:
            return None, 0.0

        # Find highest scoring project
        best_project = max(associations.items(), key=lambda x: x[1])

        # Normalize score to 0-1
        max_score = sum(associations.values())
        confidence = best_project[1] / max_score if max_score > 0 else 0.0

        return best_project[0], confidence

    def get_project_boundary(self, project: str) -> Optional[ProjectBoundary]:
        """Get learned boundary information for a project."""
        cursor = self.db.execute("""
            SELECT project_path, project_name, strong_keywords_json, weak_keywords_json,
                   excluded_keywords_json, recency_weight, total_injections, positive_feedback_rate
            FROM project_boundaries
            WHERE project_path = ? OR project_name = ?
        """, (project, project))

        row = cursor.fetchone()
        if not row:
            return None

        return ProjectBoundary(
            project_path=row[0],
            project_name=row[1],
            strong_keywords=json.loads(row[2]) if row[2] else [],
            weak_keywords=json.loads(row[3]) if row[3] else [],
            excluded_keywords=json.loads(row[4]) if row[4] else [],
            recency_weight=row[5] or 0.5,
            total_injections=row[6] or 0,
            positive_feedback_rate=row[7] or 0.5
        )

    def get_variant_performance_by_project(self, project: str) -> Dict[str, Dict]:
        """Get A/B variant performance statistics for a project."""
        cursor = self.db.execute("""
            SELECT ab_variant, positive_count, negative_count, neutral_count, avg_confidence
            FROM variant_project_performance
            WHERE project = ?
        """, (project,))

        result = {}
        for row in cursor.fetchall():
            variant, pos, neg, neu, avg_conf = row
            total = pos + neg + neu
            result[variant] = {
                "positive_count": pos,
                "negative_count": neg,
                "neutral_count": neu,
                "total": total,
                "positive_rate": pos / total if total > 0 else 0.0,
                "avg_confidence": avg_conf
            }

        return result

    # =========================================================================
    # PERIODIC LEARNING (Called by Celery)
    # =========================================================================

    def decay_recency_weights(self, decay_factor: float = 0.95):
        """
        Decay recency weights for all projects.

        Called periodically to give more recent projects higher priority.
        """
        try:
            self.db.execute("""
                UPDATE project_boundaries
                SET recency_weight = recency_weight * ?
                WHERE recency_weight > 0.01
            """, (decay_factor,))
            self.db.commit()

            logger.info(f"Decayed project recency weights by {decay_factor}")

        except Exception as e:
            logger.error(f"Failed to decay recency weights: {e}")

    def consolidate_keyword_associations(self, min_confidence: str = "moderate"):
        """
        Consolidate keyword associations into project boundaries.

        Called periodically to update the project_boundaries table with
        learned keyword associations.
        """
        min_conf_level = self.MIN_FEEDBACK_FOR_CONFIDENCE.get(min_confidence, 5)

        # Get all projects with associations
        cursor = self.db.execute("""
            SELECT DISTINCT project FROM keyword_project_associations
        """)

        projects = [row[0] for row in cursor.fetchall()]

        for project in projects:
            # Get strong associations (weight > 0.7)
            cursor = self.db.execute("""
                SELECT keyword FROM keyword_project_associations
                WHERE project = ? AND weight > 0.7 AND (positive_count + negative_count) >= ?
            """, (project, min_conf_level))
            strong = [row[0] for row in cursor.fetchall()]

            # Get weak associations (0.4 < weight <= 0.7)
            cursor = self.db.execute("""
                SELECT keyword FROM keyword_project_associations
                WHERE project = ? AND weight > 0.4 AND weight <= 0.7 AND (positive_count + negative_count) >= ?
            """, (project, min_conf_level))
            weak = [row[0] for row in cursor.fetchall()]

            # Calculate positive feedback rate
            cursor = self.db.execute("""
                SELECT
                    SUM(positive_count) as pos,
                    SUM(negative_count) as neg
                FROM keyword_project_associations
                WHERE project = ?
            """, (project,))

            row = cursor.fetchone()
            pos_rate = row[0] / (row[0] + row[1]) if row and (row[0] + row[1]) > 0 else 0.5

            # Update project boundary
            project_name = project.split("/")[-1] if "/" in project else project

            self.db.execute("""
                INSERT INTO project_boundaries
                (project_path, project_name, strong_keywords_json, weak_keywords_json, positive_feedback_rate)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_path) DO UPDATE SET
                    strong_keywords_json = excluded.strong_keywords_json,
                    weak_keywords_json = excluded.weak_keywords_json,
                    positive_feedback_rate = excluded.positive_feedback_rate
            """, (project, project_name, json.dumps(strong), json.dumps(weak), pos_rate))

        self.db.commit()
        logger.info(f"Consolidated keyword associations for {len(projects)} projects")

    def get_learning_summary(self) -> Dict:
        """Get a summary of boundary learning state."""
        # Total injections
        cursor = self.db.execute("SELECT COUNT(*) FROM boundary_injections")
        total_injections = cursor.fetchone()[0] or 0

        # Total feedback
        cursor = self.db.execute("SELECT COUNT(*) FROM boundary_feedback")
        total_feedback = cursor.fetchone()[0] or 0

        # Feedback rate
        feedback_rate = total_feedback / total_injections if total_injections > 0 else 0.0

        # Total associations
        cursor = self.db.execute("SELECT COUNT(*) FROM keyword_project_associations")
        total_associations = cursor.fetchone()[0] or 0

        # High confidence associations
        cursor = self.db.execute("""
            SELECT COUNT(*) FROM keyword_project_associations
            WHERE confidence_level IN ('high', 'confirmed')
        """)
        high_conf_associations = cursor.fetchone()[0] or 0

        # Projects tracked
        cursor = self.db.execute("SELECT COUNT(DISTINCT project) FROM keyword_project_associations")
        projects_tracked = cursor.fetchone()[0] or 0

        # Recent learning events
        cursor = self.db.execute("""
            SELECT COUNT(*) FROM boundary_learning_log
            WHERE timestamp > datetime('now', '-24 hours')
        """)
        recent_events = cursor.fetchone()[0] or 0

        return {
            "total_injections": total_injections,
            "total_feedback": total_feedback,
            "feedback_rate": feedback_rate,
            "total_associations": total_associations,
            "high_confidence_associations": high_conf_associations,
            "projects_tracked": projects_tracked,
            "learning_events_24h": recent_events
        }


def get_boundary_learner() -> BoundaryFeedbackLearner:
    """Get the singleton BoundaryFeedbackLearner instance."""
    global _learner_instance
    if _learner_instance is None:
        _learner_instance = BoundaryFeedbackLearner()
    return _learner_instance


# =========================================================================
# CLI INTERFACE
# =========================================================================

if __name__ == "__main__":
    import sys

    learner = get_boundary_learner()

    if len(sys.argv) < 2:
        print("Boundary Feedback Learner")
        print("=" * 50)
        summary = learner.get_learning_summary()
        print(f"Total Injections: {summary['total_injections']}")
        print(f"Total Feedback: {summary['total_feedback']}")
        print(f"Feedback Rate: {summary['feedback_rate']:.1%}")
        print(f"Keyword→Project Associations: {summary['total_associations']}")
        print(f"  High Confidence: {summary['high_confidence_associations']}")
        print(f"Projects Tracked: {summary['projects_tracked']}")
        print(f"Learning Events (24h): {summary['learning_events_24h']}")
        print()
        print("Commands:")
        print("  python boundary_feedback.py lookup <text>")
        print("  python boundary_feedback.py project <project_name>")
        print("  python boundary_feedback.py variant <project_name>")
        print("  python boundary_feedback.py consolidate")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "lookup":
        if len(sys.argv) < 3:
            print("Usage: python boundary_feedback.py lookup <text>")
            sys.exit(1)

        text = " ".join(sys.argv[2:])
        associations = learner.get_keyword_project_associations(text)
        best, confidence = learner.get_best_project_for_prompt(text)

        print(f"Text: {text}")
        print(f"\nBest Project: {best or 'Unknown'} (confidence: {confidence:.2f})")
        print("\nAll Associations:")
        for project, score in sorted(associations.items(), key=lambda x: -x[1]):
            print(f"  {project}: {score:.3f}")

    elif cmd == "project":
        if len(sys.argv) < 3:
            print("Usage: python boundary_feedback.py project <project_name>")
            sys.exit(1)

        project = sys.argv[2]
        boundary = learner.get_project_boundary(project)

        if boundary:
            print(f"Project: {boundary.project_name}")
            print(f"Path: {boundary.project_path}")
            print(f"Recency Weight: {boundary.recency_weight:.2f}")
            print(f"Total Injections: {boundary.total_injections}")
            print(f"Positive Feedback Rate: {boundary.positive_feedback_rate:.1%}")
            print(f"\nStrong Keywords: {', '.join(boundary.strong_keywords) or 'None'}")
            print(f"Weak Keywords: {', '.join(boundary.weak_keywords) or 'None'}")
            print(f"Excluded Keywords: {', '.join(boundary.excluded_keywords) or 'None'}")
        else:
            print(f"No boundary data for project: {project}")

    elif cmd == "variant":
        if len(sys.argv) < 3:
            print("Usage: python boundary_feedback.py variant <project_name>")
            sys.exit(1)

        project = sys.argv[2]
        performance = learner.get_variant_performance_by_project(project)

        if performance:
            print(f"A/B Variant Performance for: {project}")
            print("-" * 40)
            for variant, stats in sorted(performance.items()):
                print(f"\n{variant}:")
                print(f"  Total: {stats['total']}")
                print(f"  Positive: {stats['positive_count']} ({stats['positive_rate']:.1%})")
                print(f"  Negative: {stats['negative_count']}")
                print(f"  Neutral: {stats['neutral_count']}")
                print(f"  Avg Confidence: {stats['avg_confidence']:.2f}")
        else:
            print(f"No variant performance data for project: {project}")

    elif cmd == "consolidate":
        print("Consolidating keyword associations...")
        learner.consolidate_keyword_associations()
        print("Done!")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
