"""
Cerebellum Agent - Outcome Evaluator

The Cerebellum evaluates outcomes - learning from successes and failures
to improve future performance.

Anatomical Label: Cerebellum (Outcome Evaluator)
"""

from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState, AgentMessage


class CerebellumAgent(Agent):
    """
    Cerebellum Agent - Outcome evaluation and learning.

    Responsibilities:
    - Evaluate action outcomes
    - Learn from successes and failures
    - Update confidence scores
    - Provide performance feedback
    """

    NAME = "cerebellum"
    CATEGORY = AgentCategory.LEARNING
    DESCRIPTION = "Outcome evaluation and continuous learning"
    ANATOMICAL_LABEL = "Cerebellum (Outcome Evaluator)"
    IS_VITAL = True

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._db_path = Path.home() / ".context-dna" / ".cerebellum.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._recent_outcomes: List[Dict[str, Any]] = []

    def _on_start(self):
        """Initialize cerebellum."""
        self._ensure_db()
        # Register for action completion messages
        self.register_handler("action_completed", self._handle_action_completed)
        self.register_handler("error_event", self._handle_error_event)

    def _on_stop(self):
        """Shutdown cerebellum."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_db(self):
        """Create cerebellum database."""
        self._conn = sqlite3.connect(str(self._db_path))

        # Outcomes table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT,
                description TEXT,
                success INTEGER,
                context TEXT,
                learned TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Pattern effectiveness table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS pattern_effectiveness (
                pattern TEXT PRIMARY KEY,
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0,
                last_used TEXT,
                effectiveness_score REAL DEFAULT 0.5
            )
        """)

        self._conn.commit()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check cerebellum health."""
        try:
            if not self._conn:
                return {"healthy": False, "message": "Not connected", "score": 0.0}

            outcomes = self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
            patterns = self._conn.execute("SELECT COUNT(*) FROM pattern_effectiveness").fetchone()[0]

            return {
                "healthy": True,
                "score": 1.0,
                "message": f"Learned from {outcomes} outcomes, tracking {patterns} patterns",
                "metrics": {
                    "outcomes_recorded": outcomes,
                    "patterns_tracked": patterns
                }
            }
        except Exception as e:
            return {"healthy": False, "message": str(e), "score": 0.0}

    def process(self, input_data: Any) -> Any:
        """Process learning operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "evaluate")
            if op == "evaluate":
                return self.evaluate_outcome(input_data)
            elif op == "learn":
                return self.record_learning(input_data)
            elif op == "get_effectiveness":
                return self.get_pattern_effectiveness(input_data.get("pattern"))
            elif op == "get_insights":
                return self.get_learning_insights()
        return None

    def _handle_action_completed(self, message: AgentMessage):
        """Handle action completion messages from motor cortex."""
        action = message.payload
        self.evaluate_outcome({
            "action_type": action.get("type", "unknown"),
            "description": action.get("description", ""),
            "success": action.get("success", True),
            "context": {}
        })

    def _handle_error_event(self, message: AgentMessage):
        """Handle error events from ears."""
        error = message.payload
        self.evaluate_outcome({
            "action_type": "error",
            "description": error.get("content", str(error)),
            "success": False,
            "context": error.get("context", {})
        })

    def evaluate_outcome(self, outcome: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate an outcome and learn from it.

        Returns insights about the outcome.
        """
        try:
            if not self._conn:
                self._ensure_db()

            action_type = outcome.get("action_type", "unknown")
            success = outcome.get("success", False)
            description = outcome.get("description", "")

            # Record outcome
            self._conn.execute("""
                INSERT INTO outcomes (action_type, description, success, context, learned)
                VALUES (?, ?, ?, ?, ?)
            """, (
                action_type,
                description,
                1 if success else 0,
                json.dumps(outcome.get("context", {})),
                ""  # To be filled by learning
            ))

            # Update pattern effectiveness
            self._update_pattern_effectiveness(action_type, success)

            # Extract and record learnings
            learning = self._extract_learning(outcome)
            if learning:
                # Notify neocortex to store learning
                self.send_message("neocortex", "new_learning", learning)

            self._conn.commit()
            self._last_active = datetime.utcnow()

            # Add to recent outcomes
            outcome["timestamp"] = datetime.utcnow().isoformat()
            self._recent_outcomes.append(outcome)
            if len(self._recent_outcomes) > 100:
                self._recent_outcomes = self._recent_outcomes[-50:]

            return {
                "evaluated": True,
                "action_type": action_type,
                "success": success,
                "learning": learning
            }

        except Exception as e:
            self._handle_error(e)
            return {"evaluated": False, "error": str(e)}

    def _update_pattern_effectiveness(self, pattern: str, success: bool):
        """Update effectiveness score for a pattern."""
        try:
            # Get current stats
            cursor = self._conn.execute(
                "SELECT success_count, failure_count FROM pattern_effectiveness WHERE pattern = ?",
                (pattern,)
            )
            row = cursor.fetchone()

            if row:
                success_count = row[0] + (1 if success else 0)
                failure_count = row[1] + (0 if success else 1)
                total = success_count + failure_count
                effectiveness = success_count / total if total > 0 else 0.5

                self._conn.execute("""
                    UPDATE pattern_effectiveness
                    SET success_count = ?, failure_count = ?, effectiveness_score = ?, last_used = ?
                    WHERE pattern = ?
                """, (success_count, failure_count, effectiveness, datetime.utcnow().isoformat(), pattern))
            else:
                self._conn.execute("""
                    INSERT INTO pattern_effectiveness (pattern, success_count, failure_count, effectiveness_score, last_used)
                    VALUES (?, ?, ?, ?, ?)
                """, (pattern, 1 if success else 0, 0 if success else 1, 1.0 if success else 0.0, datetime.utcnow().isoformat()))

        except Exception as e:
            print(f"[WARN] Pattern effectiveness recording failed: {e}")

    def _extract_learning(self, outcome: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract a learning from an outcome."""
        if not outcome.get("description"):
            return None

        # Only extract learnings from notable outcomes
        success = outcome.get("success", False)
        description = outcome.get("description", "")

        # Success learnings
        if success and any(kw in description.lower() for kw in ["fixed", "worked", "deployed", "success"]):
            return {
                "title": f"Success: {description[:50]}",
                "content": description,
                "category": "success_pattern",
                "confidence": 0.7,
                "tags": [outcome.get("action_type", "unknown")]
            }

        # Failure learnings (often more valuable!)
        if not success:
            return {
                "title": f"Failure: {description[:50]}",
                "content": description,
                "category": "failure_pattern",
                "confidence": 0.8,  # High confidence in failure learnings
                "tags": [outcome.get("action_type", "unknown"), "failure"]
            }

        return None

    def record_learning(self, learning: Dict[str, Any]) -> bool:
        """Directly record a learning."""
        try:
            self.send_message("neocortex", "new_learning", learning)
            return True
        except Exception:
            return False

    def get_pattern_effectiveness(self, pattern: str = None) -> Dict[str, Any]:
        """Get effectiveness data for patterns."""
        try:
            if not self._conn:
                self._ensure_db()

            if pattern:
                cursor = self._conn.execute(
                    "SELECT * FROM pattern_effectiveness WHERE pattern = ?",
                    (pattern,)
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "pattern": row[0],
                        "success_count": row[1],
                        "failure_count": row[2],
                        "last_used": row[3],
                        "effectiveness": row[4]
                    }
                return {"pattern": pattern, "effectiveness": 0.5, "no_data": True}
            else:
                cursor = self._conn.execute(
                    "SELECT * FROM pattern_effectiveness ORDER BY effectiveness_score DESC LIMIT 20"
                )
                return {
                    "patterns": [
                        {
                            "pattern": row[0],
                            "success_count": row[1],
                            "failure_count": row[2],
                            "effectiveness": row[4]
                        }
                        for row in cursor.fetchall()
                    ]
                }
        except Exception as e:
            return {"error": str(e)}

    def get_learning_insights(self) -> Dict[str, Any]:
        """Get insights from recent learning."""
        try:
            if not self._conn:
                self._ensure_db()

            # Recent success rate
            recent_cursor = self._conn.execute("""
                SELECT success, COUNT(*) FROM outcomes
                WHERE timestamp > datetime('now', '-7 days')
                GROUP BY success
            """)
            recent = dict(recent_cursor.fetchall())
            recent_success_rate = recent.get(1, 0) / (recent.get(0, 0) + recent.get(1, 0) + 1)

            # Top performing patterns
            top_patterns = self._conn.execute("""
                SELECT pattern, effectiveness_score FROM pattern_effectiveness
                ORDER BY effectiveness_score DESC LIMIT 5
            """).fetchall()

            # Struggling patterns
            struggling = self._conn.execute("""
                SELECT pattern, effectiveness_score FROM pattern_effectiveness
                WHERE effectiveness_score < 0.5 AND (success_count + failure_count) > 3
                ORDER BY effectiveness_score ASC LIMIT 5
            """).fetchall()

            return {
                "recent_7day_success_rate": recent_success_rate,
                "top_patterns": [{"pattern": p[0], "effectiveness": p[1]} for p in top_patterns],
                "struggling_patterns": [{"pattern": p[0], "effectiveness": p[1]} for p in struggling],
                "recent_outcomes_count": len(self._recent_outcomes)
            }

        except Exception as e:
            return {"error": str(e)}
