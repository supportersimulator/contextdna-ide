#!/usr/bin/env python3
"""
OUTCOME TRACKING COMPLETION

Closes the loop between evidence capture and learning verification.
This module connects the evidence system to actually track whether fixes work.

The Problem:
- Evidence is captured (what we tried)
- But we don't track if it actually worked
- Learning loop is incomplete

The Solution:
- Capture evidence + expected outcome
- Track actual outcome
- Feed results back to pattern confidence scores
- Complete the learning loop

Usage:
    from memory.outcome_tracker import OutcomeTracker

    tracker = OutcomeTracker()

    # When starting a fix
    outcome_id = tracker.start_tracking(
        task="Fix async boto3 blocking",
        approach="Wrap in asyncio.to_thread()",
        expected_outcome="No more event loop blocking",
        related_patterns=["async_python_blocking"]
    )

    # After verifying the fix worked
    tracker.record_outcome(
        outcome_id=outcome_id,
        success=True,
        actual_outcome="Voice pipeline runs without blocking",
        verification_method="Load test with 10 concurrent requests"
    )
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
import logging
import uuid

logger = logging.getLogger(__name__)

# Outcome tracking database
_LEGACY_OUTCOME_DB = Path(__file__).parent / ".outcome_tracking.db"


def _get_outcome_db() -> Path:
    from memory.db_utils import get_unified_db_path
    return get_unified_db_path(_LEGACY_OUTCOME_DB)


OUTCOME_DB = _get_outcome_db()

def _t_out(name: str) -> str:
    from memory.db_utils import unified_table
    return unified_table(".outcome_tracking.db", name)



@dataclass
class TrackedOutcome:
    """A tracked outcome for learning verification."""
    outcome_id: str
    task: str
    approach: str
    expected_outcome: str
    related_patterns: List[str]
    start_time: str
    end_time: Optional[str] = None
    success: Optional[bool] = None
    actual_outcome: Optional[str] = None
    verification_method: Optional[str] = None
    feedback_applied: bool = False


class OutcomeTracker:
    """
    Tracks outcomes to complete the learning loop.
    """

    def __init__(self):
        self._init_db()

    def _init_db(self):
        """Initialize the outcome tracking database."""
        with sqlite3.connect(str(OUTCOME_DB)) as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {_t_out('tracked_outcomes')} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outcome_id TEXT UNIQUE NOT NULL,
                    task TEXT NOT NULL,
                    approach TEXT NOT NULL,
                    expected_outcome TEXT NOT NULL,
                    related_patterns_json TEXT,
                    start_time TEXT DEFAULT CURRENT_TIMESTAMP,
                    end_time TEXT,
                    success INTEGER,
                    actual_outcome TEXT,
                    verification_method TEXT,
                    feedback_applied INTEGER DEFAULT 0
                )
            """)

            # Pattern confidence adjustments
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {_t_out('pattern_confidence')} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_id TEXT NOT NULL,
                    outcome_id TEXT NOT NULL,
                    adjustment REAL NOT NULL,
                    reason TEXT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(pattern_id, outcome_id)
                )
            """)

            # Success/failure statistics
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {_t_out('outcome_statistics')} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    total_tracked INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    pending_count INTEGER DEFAULT 0,
                    last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(domain)
                )
            """)
            conn.commit()

    def start_tracking(
        self,
        task: str,
        approach: str,
        expected_outcome: str,
        related_patterns: List[str] = None
    ) -> str:
        """Start tracking an outcome. Returns outcome_id."""
        outcome_id = f"out_{uuid.uuid4().hex[:12]}"

        with sqlite3.connect(str(OUTCOME_DB)) as conn:
            conn.execute(f"""
                INSERT INTO {_t_out('tracked_outcomes')}
                (outcome_id, task, approach, expected_outcome, related_patterns_json)
                VALUES (?, ?, ?, ?, ?)
            """, (
                outcome_id,
                task,
                approach,
                expected_outcome,
                json.dumps(related_patterns or [])
            ))
            conn.commit()

        logger.info(f"Started tracking outcome: {outcome_id}")
        return outcome_id

    def record_outcome(
        self,
        outcome_id: str,
        success: bool,
        actual_outcome: str,
        verification_method: str = "manual verification"
    ):
        """Record the actual outcome and update pattern confidence."""
        with sqlite3.connect(str(OUTCOME_DB)) as conn:
            # Update the outcome record
            conn.execute(f"""
                UPDATE {_t_out('tracked_outcomes')}
                SET end_time = ?, success = ?, actual_outcome = ?, verification_method = ?
                WHERE outcome_id = ?
            """, (
                datetime.now().isoformat(),
                1 if success else 0,
                actual_outcome,
                verification_method,
                outcome_id
            ))

            # Get related patterns
            row = conn.execute(f"""
                SELECT related_patterns_json FROM {_t_out('tracked_outcomes')} WHERE outcome_id = ?
            """, (outcome_id,)).fetchone()

            if row:
                patterns = json.loads(row[0]) if row[0] else []

                # Adjust pattern confidence
                adjustment = 0.05 if success else -0.03  # Success boosts more than failure hurts
                for pattern_id in patterns:
                    conn.execute(f"""
                        INSERT OR REPLACE INTO {_t_out('pattern_confidence')}
                        (pattern_id, outcome_id, adjustment, reason)
                        VALUES (?, ?, ?, ?)
                    """, (
                        pattern_id,
                        outcome_id,
                        adjustment,
                        f"{'Success' if success else 'Failure'}: {actual_outcome[:100]}"
                    ))

                # Mark feedback_applied = True now that pattern confidence has been updated.
                # This closes the learning loop: the system knows it acted on its learnings.
                conn.execute(f"""
                    UPDATE {_t_out('tracked_outcomes')}
                    SET feedback_applied = 1
                    WHERE outcome_id = ?
                """, (outcome_id,))

            conn.commit()

        logger.info(f"Recorded outcome {outcome_id}: {'SUCCESS' if success else 'FAILURE'}")

    def get_pending_outcomes(self) -> List[Dict]:
        """Get outcomes that haven't been verified yet."""
        with sqlite3.connect(str(OUTCOME_DB)) as conn:
            rows = conn.execute(f"""
                SELECT outcome_id, task, approach, expected_outcome, start_time
                FROM {_t_out('tracked_outcomes')}
                WHERE success IS NULL
                ORDER BY start_time DESC
            """).fetchall()

        return [
            {
                "outcome_id": r[0],
                "task": r[1],
                "approach": r[2],
                "expected_outcome": r[3],
                "start_time": r[4]
            }
            for r in rows
        ]

    def get_pattern_confidence(self, pattern_id: str) -> Dict:
        """Get confidence statistics for a pattern."""
        with sqlite3.connect(str(OUTCOME_DB)) as conn:
            rows = conn.execute(f"""
                SELECT adjustment, reason, timestamp
                FROM {_t_out('pattern_confidence')}
                WHERE pattern_id = ?
                ORDER BY timestamp DESC
            """, (pattern_id,)).fetchall()

        if not rows:
            return {"pattern_id": pattern_id, "confidence": 0.5, "sample_size": 0}

        total_adjustment = sum(r[0] for r in rows)
        base_confidence = 0.5
        confidence = max(0.1, min(0.95, base_confidence + total_adjustment))

        return {
            "pattern_id": pattern_id,
            "confidence": round(confidence, 3),
            "sample_size": len(rows),
            "recent_outcomes": [
                {"adjustment": r[0], "reason": r[1], "timestamp": r[2]}
                for r in rows[:5]
            ]
        }

    def get_completed_outcomes(self, limit: int = 100, since: str = None) -> List[Dict]:
        """Get completed outcomes (success or failure) for refinement loops.

        Args:
            limit: Max outcomes to return.
            since: ISO timestamp — only return outcomes completed after this time.

        Returns:
            List of outcome dicts with task, approach, success, actual_outcome,
            related_patterns, feedback_applied, end_time.
        """
        query = f"""
            SELECT outcome_id, task, approach, expected_outcome,
                   related_patterns_json, end_time, success,
                   actual_outcome, verification_method, feedback_applied
            FROM {_t_out('tracked_outcomes')}
            WHERE success IS NOT NULL
        """
        params: list = []
        if since:
            query += " AND end_time > ?"
            params.append(since)
        query += " ORDER BY end_time DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(str(OUTCOME_DB)) as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            {
                "outcome_id": r[0],
                "task": r[1],
                "approach": r[2],
                "expected_outcome": r[3],
                "related_patterns": json.loads(r[4]) if r[4] else [],
                "end_time": r[5],
                "success": bool(r[6]),
                "actual_outcome": r[7],
                "verification_method": r[8],
                "feedback_applied": bool(r[9]),
            }
            for r in rows
        ]

    def get_statistics(self) -> Dict:
        """Get overall outcome tracking statistics."""
        with sqlite3.connect(str(OUTCOME_DB)) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM {_t_out('tracked_outcomes')}").fetchone()[0]
            success = conn.execute(f"SELECT COUNT(*) FROM {_t_out('tracked_outcomes')} WHERE success = 1").fetchone()[0]
            failure = conn.execute(f"SELECT COUNT(*) FROM {_t_out('tracked_outcomes')} WHERE success = 0").fetchone()[0]
            pending = conn.execute(f"SELECT COUNT(*) FROM {_t_out('tracked_outcomes')} WHERE success IS NULL").fetchone()[0]

        success_rate = success / max(1, success + failure)

        return {
            "total_tracked": total,
            "success_count": success,
            "failure_count": failure,
            "pending_count": pending,
            "success_rate": round(success_rate, 3),
            "learning_loop_active": total > 0
        }


# Singleton instance
_tracker: Optional[OutcomeTracker] = None


def get_outcome_tracker() -> OutcomeTracker:
    """Get the singleton outcome tracker."""
    global _tracker
    if _tracker is None:
        _tracker = OutcomeTracker()
    return _tracker


if __name__ == "__main__":
    print("🎯 Outcome Tracking System Status")
    print("=" * 50)

    tracker = get_outcome_tracker()
    stats = tracker.get_statistics()

    print(f"Total Tracked: {stats['total_tracked']}")
    print(f"Success: {stats['success_count']}")
    print(f"Failure: {stats['failure_count']}")
    print(f"Pending: {stats['pending_count']}")
    print(f"Success Rate: {stats['success_rate']*100:.1f}%")
    print(f"Learning Loop: {'ACTIVE' if stats['learning_loop_active'] else 'INACTIVE'}")

    pending = tracker.get_pending_outcomes()
    if pending:
        print(f"\n⏳ Pending Verifications ({len(pending)}):")
        for p in pending[:5]:
            print(f"   - {p['task'][:50]}...")
