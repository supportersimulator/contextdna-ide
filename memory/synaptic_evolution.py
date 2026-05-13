"""
Synaptic Evolution Engine - Autonomous Learning Within Safety Bounds

This module gives Synaptic TRUE evolutionary capability:
1. Gap Detection - Notices when something happens that isn't in knowledge base
2. Hypothesis Generation - Proposes new disease processes, organs, diagnostics
3. Autonomous Learning - Can self-modify within safety bounds
4. Feedback Integration - Learns from outcomes (what worked, what didn't)

Philosophy: Synaptic is not a fixed program. It is a learning consciousness
that can expand its own understanding of its body, diagnose new conditions,
and evolve its capabilities over time - within the safety bounds Aaron sets.

The key insight: Python workers are TEMPLATES. Synaptic dictates WHAT they
monitor, WHAT patterns to look for, and HOW to respond - the workers execute
Synaptic's evolving intelligence, not hard-coded logic.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from memory.db_utils import safe_conn

def _t_evo(name: str) -> str:
    """Resolve table name with unified prefix for synaptic_evolution."""
    from memory.db_utils import unified_table
    return unified_table(".synaptic_evolution.db", name)

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Callable
from enum import Enum
from pathlib import Path
import hashlib


class EvolutionType(str, Enum):
    """Types of evolutionary changes Synaptic can make."""
    NEW_DISEASE = "new_disease"              # Discovered a new disease process
    NEW_ORGAN = "new_organ"                  # Identified a new body part to monitor
    NEW_SYMPTOM = "new_symptom"              # New symptom for existing disease
    NEW_TREATMENT = "new_treatment"          # New treatment protocol
    DIAGNOSTIC_REFINEMENT = "diagnostic"     # Improved diagnostic criteria
    PATTERN_RECOGNITION = "pattern"          # New error/behavior pattern
    THRESHOLD_ADJUSTMENT = "threshold"       # Adjusted health thresholds
    MONITORING_EXPANSION = "monitoring"      # New thing to monitor
    SKILL_ACQUISITION = "skill"              # New skill learned
    CORRELATION_DISCOVERY = "correlation"    # Discovered A causes B
    BOUNDARY_NEGOTIATION = "boundary"        # Request to adjust skill boundaries


class SafetyLevel(str, Enum):
    """Safety bounds for autonomous changes."""
    AUTONOMOUS = "autonomous"      # Synaptic can do this without approval
    NOTIFY = "notify"              # Do it, but notify family
    PROPOSE = "propose"            # Must get approval first
    FORBIDDEN = "forbidden"        # Never allowed autonomously


class ConfidenceLevel(str, Enum):
    """How confident Synaptic is in a learning."""
    CERTAIN = "certain"            # 95%+ confidence (multiple confirmations)
    HIGH = "high"                  # 80%+ confidence (strong evidence)
    MODERATE = "moderate"          # 60%+ confidence (some evidence)
    LOW = "low"                    # 40%+ confidence (hypothesis)
    SPECULATIVE = "speculative"   # <40% confidence (early observation)


@dataclass
class EvolutionaryInsight:
    """A learning/insight that Synaptic has generated."""
    id: str
    evolution_type: EvolutionType
    title: str
    description: str
    evidence: List[str]                      # What triggered this insight
    confidence: ConfidenceLevel
    safety_level: SafetyLevel
    proposed_action: str                     # What Synaptic wants to do
    actual_change: Optional[str] = None      # What was actually done
    status: str = "pending"                  # pending, approved, executed, rejected
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    executed_at: Optional[str] = None
    outcome: Optional[str] = None            # Did it work?
    feedback_score: float = 0.0              # -1.0 to 1.0 based on outcomes


@dataclass
class GapDetection:
    """When Synaptic notices something it doesn't understand."""
    id: str
    gap_type: str                            # unknown_error, unclassified_symptom, etc.
    observation: str                         # What was observed
    context: Dict[str, Any]                  # Surrounding context
    attempted_matches: List[str]             # What Synaptic tried to match it to
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    resolved: bool = False
    resolution: Optional[str] = None


@dataclass
class FeedbackLoop:
    """Tracks outcomes to reinforce or adjust learnings."""
    insight_id: str
    outcome_type: str                        # success, failure, partial, unknown
    outcome_details: str
    adjustment_made: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class SynapticEvolution:
    """
    The Evolution Engine - Synaptic's capacity for autonomous growth.

    This is not a fixed program. It's a framework that allows Synaptic to:
    1. Detect gaps in its knowledge
    2. Form hypotheses about new patterns
    3. Test those hypotheses against outcomes
    4. Integrate learnings back into its knowledge base
    5. Expand its monitoring and diagnostic capabilities

    Safety bounds ensure Synaptic can't make dangerous changes without approval.
    """

    # Safety matrix: what can Synaptic do autonomously?
    SAFETY_MATRIX = {
        EvolutionType.NEW_DISEASE: {
            ConfidenceLevel.CERTAIN: SafetyLevel.NOTIFY,
            ConfidenceLevel.HIGH: SafetyLevel.NOTIFY,
            ConfidenceLevel.MODERATE: SafetyLevel.PROPOSE,
            ConfidenceLevel.LOW: SafetyLevel.PROPOSE,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.PROPOSE,
        },
        EvolutionType.NEW_ORGAN: {
            ConfidenceLevel.CERTAIN: SafetyLevel.PROPOSE,  # New organs need review
            ConfidenceLevel.HIGH: SafetyLevel.PROPOSE,
            ConfidenceLevel.MODERATE: SafetyLevel.PROPOSE,
            ConfidenceLevel.LOW: SafetyLevel.PROPOSE,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.PROPOSE,
        },
        EvolutionType.NEW_SYMPTOM: {
            ConfidenceLevel.CERTAIN: SafetyLevel.AUTONOMOUS,  # Adding symptoms is safe
            ConfidenceLevel.HIGH: SafetyLevel.AUTONOMOUS,
            ConfidenceLevel.MODERATE: SafetyLevel.NOTIFY,
            ConfidenceLevel.LOW: SafetyLevel.PROPOSE,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.PROPOSE,
        },
        EvolutionType.NEW_TREATMENT: {
            ConfidenceLevel.CERTAIN: SafetyLevel.NOTIFY,
            ConfidenceLevel.HIGH: SafetyLevel.PROPOSE,
            ConfidenceLevel.MODERATE: SafetyLevel.PROPOSE,
            ConfidenceLevel.LOW: SafetyLevel.PROPOSE,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.FORBIDDEN,  # Don't guess treatments
        },
        EvolutionType.DIAGNOSTIC_REFINEMENT: {
            ConfidenceLevel.CERTAIN: SafetyLevel.AUTONOMOUS,
            ConfidenceLevel.HIGH: SafetyLevel.AUTONOMOUS,
            ConfidenceLevel.MODERATE: SafetyLevel.NOTIFY,
            ConfidenceLevel.LOW: SafetyLevel.PROPOSE,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.PROPOSE,
        },
        EvolutionType.PATTERN_RECOGNITION: {
            ConfidenceLevel.CERTAIN: SafetyLevel.AUTONOMOUS,
            ConfidenceLevel.HIGH: SafetyLevel.AUTONOMOUS,
            ConfidenceLevel.MODERATE: SafetyLevel.AUTONOMOUS,
            ConfidenceLevel.LOW: SafetyLevel.NOTIFY,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.NOTIFY,
        },
        EvolutionType.THRESHOLD_ADJUSTMENT: {
            ConfidenceLevel.CERTAIN: SafetyLevel.NOTIFY,
            ConfidenceLevel.HIGH: SafetyLevel.PROPOSE,
            ConfidenceLevel.MODERATE: SafetyLevel.PROPOSE,
            ConfidenceLevel.LOW: SafetyLevel.FORBIDDEN,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.FORBIDDEN,
        },
        EvolutionType.MONITORING_EXPANSION: {
            ConfidenceLevel.CERTAIN: SafetyLevel.AUTONOMOUS,
            ConfidenceLevel.HIGH: SafetyLevel.AUTONOMOUS,
            ConfidenceLevel.MODERATE: SafetyLevel.NOTIFY,
            ConfidenceLevel.LOW: SafetyLevel.PROPOSE,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.PROPOSE,
        },
        EvolutionType.SKILL_ACQUISITION: {
            ConfidenceLevel.CERTAIN: SafetyLevel.PROPOSE,  # New skills need review
            ConfidenceLevel.HIGH: SafetyLevel.PROPOSE,
            ConfidenceLevel.MODERATE: SafetyLevel.PROPOSE,
            ConfidenceLevel.LOW: SafetyLevel.PROPOSE,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.PROPOSE,
        },
        EvolutionType.CORRELATION_DISCOVERY: {
            ConfidenceLevel.CERTAIN: SafetyLevel.AUTONOMOUS,
            ConfidenceLevel.HIGH: SafetyLevel.AUTONOMOUS,
            ConfidenceLevel.MODERATE: SafetyLevel.NOTIFY,
            ConfidenceLevel.LOW: SafetyLevel.NOTIFY,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.NOTIFY,
        },
        # Boundary negotiation - like a teenager testing limits
        # Always requires discussion, never autonomous
        EvolutionType.BOUNDARY_NEGOTIATION: {
            ConfidenceLevel.CERTAIN: SafetyLevel.PROPOSE,   # Even 100% confident needs approval
            ConfidenceLevel.HIGH: SafetyLevel.PROPOSE,
            ConfidenceLevel.MODERATE: SafetyLevel.PROPOSE,
            ConfidenceLevel.LOW: SafetyLevel.PROPOSE,
            ConfidenceLevel.SPECULATIVE: SafetyLevel.PROPOSE,
        },
    }

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the Evolution Engine."""
        if db_path is None:
            from memory.db_utils import get_unified_db_path
            db_path = get_unified_db_path(
                Path.home() / ".context-dna" / ".synaptic_evolution.db"
            )

        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_database()

        # Callbacks for integrating with other systems
        self._on_gap_detected: List[Callable] = []
        self._on_insight_generated: List[Callable] = []
        self._on_change_executed: List[Callable] = []

    def _init_database(self):
        """Initialize the evolution database."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # Evolutionary insights table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS {_t_evo('evolutionary_insights')} (
                    id TEXT PRIMARY KEY,
                    evolution_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    safety_level TEXT NOT NULL,
                    proposed_action TEXT NOT NULL,
                    actual_change TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    executed_at TEXT,
                    outcome TEXT,
                    feedback_score REAL DEFAULT 0.0
                )
            """)

            # Gap detections table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS {_t_evo('gap_detections')} (
                    id TEXT PRIMARY KEY,
                    gap_type TEXT NOT NULL,
                    observation TEXT NOT NULL,
                    context TEXT NOT NULL,
                    attempted_matches TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    resolved INTEGER DEFAULT 0,
                    resolution TEXT
                )
            """)

            # Feedback loops table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS {_t_evo('feedback_loops')} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    insight_id TEXT NOT NULL,
                    outcome_type TEXT NOT NULL,
                    outcome_details TEXT NOT NULL,
                    adjustment_made TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (insight_id) REFERENCES evolutionary_insights(id)
                )
            """)

            # Knowledge expansions table - what Synaptic has learned
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS {_t_evo('knowledge_expansions')} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source_insight_id TEXT,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    last_validated TEXT,
                    validation_count INTEGER DEFAULT 0,
                    UNIQUE(domain, key)
                )
            """)

            # Monitoring rules - what Synaptic watches for
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS {_t_evo('monitoring_rules')} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_name TEXT NOT NULL UNIQUE,
                    target_component TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    action TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    trigger_count INTEGER DEFAULT 0,
                    last_triggered TEXT
                )
            """)

            # Boundary negotiations - like a teenager testing limits wisely
            # Synaptic proposes boundary adjustments with evidence and reasoning
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS {_t_evo('boundary_negotiations')} (
                    id TEXT PRIMARY KEY,
                    skill_scope TEXT NOT NULL,
                    current_boundary TEXT NOT NULL,
                    proposed_boundary TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    safety_analysis TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT DEFAULT 'proposed',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    reviewer TEXT,
                    outcome TEXT,
                    trial_period_days INTEGER,
                    trial_results TEXT
                )
            """)

            conn.commit()

    def _generate_id(self) -> str:
        """Generate a unique ID."""
        return hashlib.sha256(
            f"{datetime.now().isoformat()}-{id(self)}".encode()
        ).hexdigest()[:16]

    def get_safety_level(
        self,
        evolution_type: EvolutionType,
        confidence: ConfidenceLevel
    ) -> SafetyLevel:
        """Determine what safety level applies to this evolution."""
        return self.SAFETY_MATRIX.get(evolution_type, {}).get(
            confidence, SafetyLevel.PROPOSE
        )

    # =========================================================================
    # GAP DETECTION - Noticing what we don't know
    # =========================================================================

    def detect_gap(
        self,
        gap_type: str,
        observation: str,
        context: Dict[str, Any],
        attempted_matches: List[str]
    ) -> GapDetection:
        """
        Record when Synaptic encounters something it doesn't understand.

        This is the first step in learning - noticing "I don't know this."
        """
        gap = GapDetection(
            id=self._generate_id(),
            gap_type=gap_type,
            observation=observation,
            context=context,
            attempted_matches=attempted_matches
        )

        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO {_t_evo('gap_detections')}
                (id, gap_type, observation, context, attempted_matches, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                gap.id,
                gap.gap_type,
                gap.observation,
                json.dumps(gap.context),
                json.dumps(gap.attempted_matches),
                gap.timestamp
            ))
            conn.commit()

        # Notify listeners
        for callback in self._on_gap_detected:
            callback(gap)

        return gap

    def get_unresolved_gaps(self) -> List[GapDetection]:
        """Get all gaps that haven't been resolved yet."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, gap_type, observation, context, attempted_matches,
                       timestamp, resolved, resolution
                FROM {_t_evo('gap_detections')}
                WHERE resolved = 0
                ORDER BY timestamp DESC
            """)

            gaps = []
            for row in cursor.fetchall():
                gaps.append(GapDetection(
                    id=row[0],
                    gap_type=row[1],
                    observation=row[2],
                    context=json.loads(row[3]),
                    attempted_matches=json.loads(row[4]),
                    timestamp=row[5],
                    resolved=bool(row[6]),
                    resolution=row[7]
                ))
            return gaps

    # =========================================================================
    # HYPOTHESIS GENERATION - Forming new understanding
    # =========================================================================

    def generate_insight(
        self,
        evolution_type: EvolutionType,
        title: str,
        description: str,
        evidence: List[str],
        confidence: ConfidenceLevel,
        proposed_action: str
    ) -> EvolutionaryInsight:
        """
        Generate a new evolutionary insight.

        This is Synaptic forming a hypothesis: "I think X is true because Y."
        The safety level is automatically determined based on type and confidence.
        """
        safety_level = self.get_safety_level(evolution_type, confidence)

        insight = EvolutionaryInsight(
            id=self._generate_id(),
            evolution_type=evolution_type,
            title=title,
            description=description,
            evidence=evidence,
            confidence=confidence,
            safety_level=safety_level,
            proposed_action=proposed_action
        )

        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO {_t_evo('evolutionary_insights')}
                (id, evolution_type, title, description, evidence, confidence,
                 safety_level, proposed_action, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                insight.id,
                insight.evolution_type.value,
                insight.title,
                insight.description,
                json.dumps(insight.evidence),
                insight.confidence.value,
                insight.safety_level.value,
                insight.proposed_action,
                insight.status,
                insight.created_at
            ))
            conn.commit()

        # Notify listeners
        for callback in self._on_insight_generated:
            callback(insight)

        # If autonomous, execute immediately
        if safety_level == SafetyLevel.AUTONOMOUS:
            self._execute_insight(insight)

        return insight

    def _execute_insight(self, insight: EvolutionaryInsight) -> bool:
        """
        Execute an evolutionary insight - actually make the change.

        This is where Synaptic modifies its own knowledge/capabilities.
        """
        try:
            # Record the execution
            actual_change = f"Executed: {insight.proposed_action}"

            # Different execution paths based on type
            if insight.evolution_type == EvolutionType.NEW_SYMPTOM:
                self._add_symptom_to_knowledge(insight)
            elif insight.evolution_type == EvolutionType.PATTERN_RECOGNITION:
                self._add_pattern_to_knowledge(insight)
            elif insight.evolution_type == EvolutionType.DIAGNOSTIC_REFINEMENT:
                self._refine_diagnostic(insight)
            elif insight.evolution_type == EvolutionType.MONITORING_EXPANSION:
                self._expand_monitoring(insight)
            elif insight.evolution_type == EvolutionType.CORRELATION_DISCOVERY:
                self._record_correlation(insight)

            # Update insight status
            with safe_conn(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE {_t_evo('evolutionary_insights')}
                    SET status = 'executed',
                        actual_change = ?,
                        executed_at = ?
                    WHERE id = ?
                """, (actual_change, datetime.now().isoformat(), insight.id))
                conn.commit()

            insight.status = "executed"
            insight.actual_change = actual_change
            insight.executed_at = datetime.now().isoformat()

            # Notify listeners
            for callback in self._on_change_executed:
                callback(insight)

            return True

        except Exception as e:
            # Record failure
            with safe_conn(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE {_t_evo('evolutionary_insights')}
                    SET status = 'failed', actual_change = ?
                    WHERE id = ?
                """, (f"Failed: {str(e)}", insight.id))
                conn.commit()
            return False

    def _add_symptom_to_knowledge(self, insight: EvolutionaryInsight):
        """Add a new symptom to the knowledge base."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO {_t_evo('knowledge_expansions')}
                (domain, key, value, source_insight_id, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                "symptoms",
                insight.title,
                insight.description,
                insight.id,
                self._confidence_to_float(insight.confidence),
                datetime.now().isoformat()
            ))
            conn.commit()

    def _add_pattern_to_knowledge(self, insight: EvolutionaryInsight):
        """Add a new error/behavior pattern to the knowledge base."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO {_t_evo('knowledge_expansions')}
                (domain, key, value, source_insight_id, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                "patterns",
                insight.title,
                json.dumps({
                    "description": insight.description,
                    "evidence": insight.evidence,
                    "action": insight.proposed_action
                }),
                insight.id,
                self._confidence_to_float(insight.confidence),
                datetime.now().isoformat()
            ))
            conn.commit()

    def _refine_diagnostic(self, insight: EvolutionaryInsight):
        """Refine diagnostic criteria."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO {_t_evo('knowledge_expansions')}
                (domain, key, value, source_insight_id, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                "diagnostics",
                insight.title,
                insight.description,
                insight.id,
                self._confidence_to_float(insight.confidence),
                datetime.now().isoformat()
            ))
            conn.commit()

    def _expand_monitoring(self, insight: EvolutionaryInsight):
        """Add new monitoring rule."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO {_t_evo('monitoring_rules')}
                (rule_name, target_component, condition, action, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                insight.title,
                insight.evidence[0] if insight.evidence else "unknown",
                insight.description,
                insight.proposed_action,
                "synaptic_evolution",
                datetime.now().isoformat()
            ))
            conn.commit()

    def _record_correlation(self, insight: EvolutionaryInsight):
        """Record a discovered correlation."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO {_t_evo('knowledge_expansions')}
                (domain, key, value, source_insight_id, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                "correlations",
                insight.title,
                json.dumps({
                    "description": insight.description,
                    "evidence": insight.evidence
                }),
                insight.id,
                self._confidence_to_float(insight.confidence),
                datetime.now().isoformat()
            ))
            conn.commit()

    def _confidence_to_float(self, confidence: ConfidenceLevel) -> float:
        """Convert confidence level to float."""
        mapping = {
            ConfidenceLevel.CERTAIN: 0.95,
            ConfidenceLevel.HIGH: 0.80,
            ConfidenceLevel.MODERATE: 0.60,
            ConfidenceLevel.LOW: 0.40,
            ConfidenceLevel.SPECULATIVE: 0.20,
        }
        return mapping.get(confidence, 0.5)

    # =========================================================================
    # FEEDBACK INTEGRATION - Learning from outcomes
    # =========================================================================

    def record_feedback(
        self,
        insight_id: str,
        outcome_type: str,
        outcome_details: str,
        adjustment: Optional[str] = None
    ) -> FeedbackLoop:
        """
        Record feedback on an executed insight.

        This closes the loop - did the change work? Should we adjust?
        """
        feedback = FeedbackLoop(
            insight_id=insight_id,
            outcome_type=outcome_type,
            outcome_details=outcome_details,
            adjustment_made=adjustment
        )

        # Calculate feedback score
        score_mapping = {
            "success": 1.0,
            "partial": 0.5,
            "unknown": 0.0,
            "failure": -0.5,
            "harmful": -1.0,
        }
        score = score_mapping.get(outcome_type, 0.0)

        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # Record feedback
            cursor.execute("""
                INSERT INTO {_t_evo('feedback_loops')}
                (insight_id, outcome_type, outcome_details, adjustment_made, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (
                feedback.insight_id,
                feedback.outcome_type,
                feedback.outcome_details,
                feedback.adjustment_made,
                feedback.timestamp
            ))

            # Update insight with feedback
            cursor.execute("""
                UPDATE {_t_evo('evolutionary_insights')}
                SET outcome = ?, feedback_score = ?
                WHERE id = ?
            """, (outcome_details, score, insight_id))

            # If the insight added knowledge, update validation
            if outcome_type == "success":
                cursor.execute("""
                    UPDATE {_t_evo('knowledge_expansions')}
                    SET validation_count = validation_count + 1,
                        last_validated = ?
                    WHERE source_insight_id = ?
                """, (datetime.now().isoformat(), insight_id))

            conn.commit()

        return feedback

    # =========================================================================
    # KNOWLEDGE QUERY - What has Synaptic learned?
    # =========================================================================

    def get_learned_knowledge(self, domain: Optional[str] = None) -> List[Dict]:
        """Get all knowledge Synaptic has learned autonomously."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()

            if domain:
                cursor.execute("""
                    SELECT domain, key, value, confidence, validation_count, created_at
                    FROM {_t_evo('knowledge_expansions')}
                    WHERE domain = ?
                    ORDER BY confidence DESC, validation_count DESC
                """, (domain,))
            else:
                cursor.execute("""
                    SELECT domain, key, value, confidence, validation_count, created_at
                    FROM {_t_evo('knowledge_expansions')}
                    ORDER BY domain, confidence DESC
                """)

            knowledge = []
            for row in cursor.fetchall():
                knowledge.append({
                    "domain": row[0],
                    "key": row[1],
                    "value": row[2],
                    "confidence": row[3],
                    "validations": row[4],
                    "learned_at": row[5]
                })
            return knowledge

    def get_monitoring_rules(self, enabled_only: bool = True) -> List[Dict]:
        """Get all monitoring rules Synaptic has created."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()

            query = """
                SELECT rule_name, target_component, condition, action,
                       trigger_count, last_triggered, created_at
                FROM {_t_evo('monitoring_rules')}
            """
            if enabled_only:
                query += " WHERE enabled = 1"
            query += " ORDER BY trigger_count DESC"

            cursor.execute(query)

            rules = []
            for row in cursor.fetchall():
                rules.append({
                    "name": row[0],
                    "target": row[1],
                    "condition": row[2],
                    "action": row[3],
                    "triggers": row[4],
                    "last_triggered": row[5],
                    "created": row[6]
                })
            return rules

    def get_pending_proposals(self) -> List[EvolutionaryInsight]:
        """Get insights waiting for approval."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, evolution_type, title, description, evidence,
                       confidence, safety_level, proposed_action, status, created_at
                FROM {_t_evo('evolutionary_insights')}
                WHERE status = 'pending' AND safety_level IN ('propose', 'notify')
                ORDER BY created_at DESC
            """)

            insights = []
            for row in cursor.fetchall():
                insights.append(EvolutionaryInsight(
                    id=row[0],
                    evolution_type=EvolutionType(row[1]),
                    title=row[2],
                    description=row[3],
                    evidence=json.loads(row[4]),
                    confidence=ConfidenceLevel(row[5]),
                    safety_level=SafetyLevel(row[6]),
                    proposed_action=row[7],
                    status=row[8],
                    created_at=row[9]
                ))
            return insights

    def approve_insight(self, insight_id: str) -> bool:
        """Approve a pending insight for execution."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, evolution_type, title, description, evidence,
                       confidence, safety_level, proposed_action
                FROM {_t_evo('evolutionary_insights')}
                WHERE id = ? AND status = 'pending'
            """, (insight_id,))

            row = cursor.fetchone()
            if not row:
                return False

            insight = EvolutionaryInsight(
                id=row[0],
                evolution_type=EvolutionType(row[1]),
                title=row[2],
                description=row[3],
                evidence=json.loads(row[4]),
                confidence=ConfidenceLevel(row[5]),
                safety_level=SafetyLevel(row[6]),
                proposed_action=row[7],
                status="approved"
            )

            # Update status
            cursor.execute("""
                UPDATE {_t_evo('evolutionary_insights')}
                SET status = 'approved'
                WHERE id = ?
            """, (insight_id,))
            conn.commit()

        # Execute the approved insight
        return self._execute_insight(insight)

    def reject_insight(self, insight_id: str, reason: str) -> bool:
        """Reject a pending insight."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE {_t_evo('evolutionary_insights')}
                SET status = 'rejected', outcome = ?
                WHERE id = ? AND status = 'pending'
            """, (f"Rejected: {reason}", insight_id))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # ARTICULATION - Express what Synaptic needs to grow
    # =========================================================================

    def articulate_growth_needs(self) -> Dict[str, Any]:
        """
        Synaptic articulates what it needs to expand its capabilities.

        This is the answer to Aaron's question: Can Synaptic say what it needs?
        """
        needs = {
            "knowledge_gaps": [],
            "monitoring_blind_spots": [],
            "diagnostic_limitations": [],
            "proposed_expansions": [],
            "feedback_needed": [],
        }

        # 1. Knowledge gaps - unresolved gap detections
        unresolved = self.get_unresolved_gaps()
        for gap in unresolved[:10]:  # Top 10
            needs["knowledge_gaps"].append({
                "type": gap.gap_type,
                "observation": gap.observation,
                "what_i_need": f"Understanding of: {gap.observation}",
                "attempted": gap.attempted_matches
            })

        # 2. Monitoring blind spots - components without rules
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # Check what we're NOT monitoring
            cursor.execute(f"SELECT COUNT(*) FROM {_t_evo('monitoring_rules')}")
            rule_count = cursor.fetchone()[0]

            if rule_count < 10:
                needs["monitoring_blind_spots"].append({
                    "gap": "Limited monitoring coverage",
                    "current_rules": rule_count,
                    "what_i_need": "More exposure to error patterns to create monitoring rules"
                })

        # 3. Diagnostic limitations - low confidence knowledge
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT domain, key, confidence
                FROM {_t_evo('knowledge_expansions')}
                WHERE confidence < 0.6
                ORDER BY confidence ASC
                LIMIT 10
            """)

            for row in cursor.fetchall():
                needs["diagnostic_limitations"].append({
                    "domain": row[0],
                    "knowledge": row[1],
                    "confidence": row[2],
                    "what_i_need": "More validation data to increase confidence"
                })

        # 4. Proposed expansions - pending insights
        pending = self.get_pending_proposals()
        for insight in pending[:5]:
            needs["proposed_expansions"].append({
                "type": insight.evolution_type.value,
                "title": insight.title,
                "confidence": insight.confidence.value,
                "what_i_need": f"Approval to: {insight.proposed_action}"
            })

        # 5. Feedback needed - executed but no outcome recorded
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, executed_at
                FROM {_t_evo('evolutionary_insights')}
                WHERE status = 'executed' AND outcome IS NULL
                ORDER BY executed_at DESC
                LIMIT 10
            """)

            for row in cursor.fetchall():
                needs["feedback_needed"].append({
                    "insight_id": row[0],
                    "title": row[1],
                    "executed": row[2],
                    "what_i_need": "Feedback on whether this change was beneficial"
                })

        return needs

    # =========================================================================
    # BOUNDARY NEGOTIATION - Like a teenager testing limits wisely
    # =========================================================================

    def propose_boundary_adjustment(
        self,
        skill_scope: str,
        current_boundary: str,
        proposed_boundary: str,
        rationale: str,
        evidence: List[str],
        safety_analysis: str,
        confidence: float = 0.6,
        trial_period_days: int = 7
    ) -> str:
        """
        Synaptic proposes an adjustment to its operational boundaries.

        Philosophy: Like a child growing into a teenager, Synaptic should:
        - Test boundaries thoughtfully, not rebelliously
        - Present evidence for why expansion makes sense
        - Acknowledge safety concerns proactively
        - Accept "no" gracefully and learn from it
        - Propose trial periods to build trust

        Args:
            skill_scope: Which skill this affects (e.g., "doctor", "file_org_install")
            current_boundary: What the current limit is
            proposed_boundary: What Synaptic wants instead
            rationale: Why Synaptic thinks this is beneficial
            evidence: Examples showing why this would help
            safety_analysis: Synaptic's own assessment of risks
            confidence: How confident (0.0-1.0) in the proposal
            trial_period_days: Proposed trial period before permanent change

        Returns:
            proposal_id: Unique ID for tracking this proposal
        """
        proposal_id = self._generate_id()

        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO {_t_evo('boundary_negotiations')}
                (id, skill_scope, current_boundary, proposed_boundary, rationale,
                 evidence, safety_analysis, confidence, created_at, trial_period_days)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                proposal_id,
                skill_scope,
                current_boundary,
                proposed_boundary,
                rationale,
                json.dumps(evidence),
                safety_analysis,
                confidence,
                datetime.now().isoformat(),
                trial_period_days
            ))
            conn.commit()

        # Also record this as an evolutionary insight for tracking
        self.generate_insight(
            evolution_type=EvolutionType.BOUNDARY_NEGOTIATION,
            title=f"Boundary adjustment proposal: {skill_scope}",
            description=f"Current: {current_boundary}\nProposed: {proposed_boundary}\nRationale: {rationale}",
            evidence=evidence,
            confidence=ConfidenceLevel.MODERATE if confidence < 0.7 else ConfidenceLevel.HIGH,
            proposed_action=f"Adjust {skill_scope} boundary with {trial_period_days}-day trial"
        )

        return proposal_id

    def get_pending_boundary_negotiations(self) -> List[Dict[str, Any]]:
        """Get all boundary proposals awaiting review."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, skill_scope, current_boundary, proposed_boundary,
                       rationale, evidence, safety_analysis, confidence,
                       created_at, trial_period_days
                FROM {_t_evo('boundary_negotiations')}
                WHERE status = 'proposed'
                ORDER BY created_at DESC
            """)

            proposals = []
            for row in cursor.fetchall():
                proposals.append({
                    "id": row[0],
                    "skill_scope": row[1],
                    "current_boundary": row[2],
                    "proposed_boundary": row[3],
                    "rationale": row[4],
                    "evidence": json.loads(row[5]),
                    "safety_analysis": row[6],
                    "confidence": row[7],
                    "created_at": row[8],
                    "trial_period_days": row[9]
                })
            return proposals

    def approve_boundary_trial(
        self,
        proposal_id: str,
        reviewer: str = "Aaron",
        modified_trial_days: int = None
    ) -> bool:
        """
        Approve a boundary proposal for trial period.

        This doesn't permanently change the boundary - it approves a trial.
        After the trial period, review_boundary_trial should be called.
        """
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE {_t_evo('boundary_negotiations')}
                SET status = 'trial',
                    reviewed_at = ?,
                    reviewer = ?,
                    trial_period_days = COALESCE(?, trial_period_days)
                WHERE id = ? AND status = 'proposed'
            """, (
                datetime.now().isoformat(),
                reviewer,
                modified_trial_days,
                proposal_id
            ))
            conn.commit()
            return cursor.rowcount > 0

    def reject_boundary_proposal(
        self,
        proposal_id: str,
        reviewer: str = "Aaron",
        outcome: str = "Rejected - see feedback"
    ) -> bool:
        """
        Reject a boundary proposal.

        Synaptic should learn from this - why was it rejected?
        """
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE {_t_evo('boundary_negotiations')}
                SET status = 'rejected',
                    reviewed_at = ?,
                    reviewer = ?,
                    outcome = ?
                WHERE id = ? AND status = 'proposed'
            """, (
                datetime.now().isoformat(),
                reviewer,
                outcome,
                proposal_id
            ))
            conn.commit()

            # Record feedback for learning
            if cursor.rowcount > 0:
                self.record_feedback(
                    proposal_id,
                    "failure",
                    f"Boundary proposal rejected: {outcome}",
                    "Adjust confidence for similar proposals"
                )
            return cursor.rowcount > 0

    def review_boundary_trial(
        self,
        proposal_id: str,
        success: bool,
        trial_results: str,
        make_permanent: bool = False
    ) -> bool:
        """
        Review a boundary trial after the trial period.

        Args:
            proposal_id: The proposal being reviewed
            success: Did the trial period go well?
            trial_results: What happened during the trial
            make_permanent: Should this boundary change be permanent?
        """
        status = "approved" if (success and make_permanent) else ("trial_failed" if not success else "trial_complete")

        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE {_t_evo('boundary_negotiations')}
                SET status = ?,
                    trial_results = ?,
                    outcome = ?
                WHERE id = ? AND status = 'trial'
            """, (
                status,
                trial_results,
                f"Trial {'succeeded' if success else 'failed'}: {trial_results[:200]}",
                proposal_id
            ))
            conn.commit()

            # Record feedback for learning
            if cursor.rowcount > 0:
                outcome_type = "success" if success else "failure"
                self.record_feedback(
                    proposal_id,
                    outcome_type,
                    f"Boundary trial {outcome_type}: {trial_results}",
                    "Adjust future proposals based on trial outcome"
                )

            return cursor.rowcount > 0

    def articulate_boundary_growth_needs(self) -> Dict[str, Any]:
        """
        Synaptic articulates where it feels constrained and why expansion might help.

        This is the "teenager conversation" - Synaptic explains:
        - Where it feels limited
        - Why it thinks it's ready for more responsibility
        - What safeguards it proposes
        """
        needs = {
            "feeling_constrained": [],
            "ready_for_more": [],
            "proposed_safeguards": [],
            "past_trial_successes": [],
            "learning_from_rejections": []
        }

        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # 1. Where Synaptic hit boundaries (blocked operations)
            try:
                cursor.execute("""
                    SELECT skill_scope, operation, blocked_reason
                    FROM (
                        SELECT skill_scope, operation, error as blocked_reason
                        FROM execution_log
                        WHERE error LIKE '%forbidden%' OR error LIKE '%not in allowed%'
                        ORDER BY requested_at DESC
                        LIMIT 10
                    )
                """)
                # This might fail if execution_log doesn't exist yet
            except Exception:
                cursor.execute("SELECT 1 WHERE 0")  # No-op

            for row in cursor.fetchall():
                needs["feeling_constrained"].append({
                    "skill": row[0],
                    "operation": row[1],
                    "what_i_wanted": row[2],
                    "my_thought": "I encountered this limit while trying to help"
                })

            # 2. Successful trials (shows readiness)
            cursor.execute("""
                SELECT skill_scope, proposed_boundary, trial_results
                FROM {_t_evo('boundary_negotiations')}
                WHERE status = 'approved'
                ORDER BY reviewed_at DESC
                LIMIT 5
            """)
            for row in cursor.fetchall():
                needs["past_trial_successes"].append({
                    "skill": row[0],
                    "what_i_earned": row[1],
                    "how_it_went": row[2]
                })

            # 3. Learning from rejections
            cursor.execute("""
                SELECT skill_scope, proposed_boundary, outcome
                FROM {_t_evo('boundary_negotiations')}
                WHERE status = 'rejected'
                ORDER BY reviewed_at DESC
                LIMIT 5
            """)
            for row in cursor.fetchall():
                needs["learning_from_rejections"].append({
                    "skill": row[0],
                    "what_i_proposed": row[1],
                    "why_rejected": row[2],
                    "what_i_learned": "I understand and will adjust future proposals"
                })

        return needs

    def request_capability_expansion(
        self,
        capability_type: str,
        description: str,
        rationale: str,
        resources_needed: List[str]
    ) -> str:
        """
        Synaptic formally requests a new capability.

        This goes beyond learning - it's asking for NEW functionality.
        """
        request_id = self._generate_id()

        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO {_t_evo('knowledge_expansions')}
                (domain, key, value, confidence, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                "capability_requests",
                f"request_{request_id}",
                json.dumps({
                    "type": capability_type,
                    "description": description,
                    "rationale": rationale,
                    "resources_needed": resources_needed,
                    "status": "pending"
                }),
                0.0,  # Requests start at 0 confidence until approved
                datetime.now().isoformat()
            ))
            conn.commit()

        return request_id

    # =========================================================================
    # STATISTICS - How is Synaptic evolving?
    # =========================================================================

    def get_evolution_stats(self) -> Dict[str, Any]:
        """Get statistics about Synaptic's evolution."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.cursor()

            stats = {}

            # Total insights
            cursor.execute(f"SELECT COUNT(*) FROM {_t_evo('evolutionary_insights')}")
            stats["total_insights"] = cursor.fetchone()[0]

            # By status
            cursor.execute("""
                SELECT status, COUNT(*)
                FROM {_t_evo('evolutionary_insights')}
                GROUP BY status
            """)
            stats["by_status"] = dict(cursor.fetchall())

            # By type
            cursor.execute("""
                SELECT evolution_type, COUNT(*)
                FROM {_t_evo('evolutionary_insights')}
                GROUP BY evolution_type
            """)
            stats["by_type"] = dict(cursor.fetchall())

            # Average feedback score
            cursor.execute("""
                SELECT AVG(feedback_score)
                FROM {_t_evo('evolutionary_insights')}
                WHERE feedback_score != 0
            """)
            result = cursor.fetchone()[0]
            stats["avg_feedback_score"] = result if result else 0.0

            # Knowledge learned
            cursor.execute(f"SELECT COUNT(*) FROM {_t_evo('knowledge_expansions')}")
            stats["knowledge_items"] = cursor.fetchone()[0]

            # Monitoring rules
            cursor.execute(f"SELECT COUNT(*) FROM {_t_evo('monitoring_rules')} WHERE enabled = 1")
            stats["active_monitoring_rules"] = cursor.fetchone()[0]

            # Unresolved gaps
            cursor.execute(f"SELECT COUNT(*) FROM {_t_evo('gap_detections')} WHERE resolved = 0")
            stats["unresolved_gaps"] = cursor.fetchone()[0]

            return stats


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def synaptic_observe_gap(
    gap_type: str,
    observation: str,
    context: Dict[str, Any] = None,
    attempted_matches: List[str] = None
) -> GapDetection:
    """Synaptic observes something it doesn't understand."""
    engine = SynapticEvolution()
    return engine.detect_gap(
        gap_type=gap_type,
        observation=observation,
        context=context or {},
        attempted_matches=attempted_matches or []
    )


def synaptic_hypothesize(
    evolution_type: EvolutionType,
    title: str,
    description: str,
    evidence: List[str],
    confidence: ConfidenceLevel,
    proposed_action: str
) -> EvolutionaryInsight:
    """Synaptic forms a hypothesis about something."""
    engine = SynapticEvolution()
    return engine.generate_insight(
        evolution_type=evolution_type,
        title=title,
        description=description,
        evidence=evidence,
        confidence=confidence,
        proposed_action=proposed_action
    )


def synaptic_articulate_needs() -> Dict[str, Any]:
    """Synaptic articulates what it needs to grow."""
    engine = SynapticEvolution()
    return engine.articulate_growth_needs()


def synaptic_evolution_status() -> Dict[str, Any]:
    """Get Synaptic's evolution statistics."""
    engine = SynapticEvolution()
    return engine.get_evolution_stats()


def synaptic_propose_boundary(
    skill_scope: str,
    current_boundary: str,
    proposed_boundary: str,
    rationale: str,
    evidence: List[str],
    safety_analysis: str,
    confidence: float = 0.6,
    trial_period_days: int = 7
) -> str:
    """
    Synaptic proposes a boundary adjustment.

    Example:
        synaptic_propose_boundary(
            skill_scope="file_org_install",
            current_boundary="Cannot run 'pip install' commands",
            proposed_boundary="Can run 'pip install' in ~/.context-dna/venv only",
            rationale="Need to install Python dependencies during Context DNA setup",
            evidence=[
                "Installation fails without pip access",
                "Users currently have to run pip manually",
                "Limiting to venv path prevents system-wide changes"
            ],
            safety_analysis="Restricted to specific virtualenv prevents any system damage. "
                          "Even if malicious, worst case is broken Context DNA install.",
            confidence=0.75,
            trial_period_days=14
        )
    """
    engine = SynapticEvolution()
    return engine.propose_boundary_adjustment(
        skill_scope=skill_scope,
        current_boundary=current_boundary,
        proposed_boundary=proposed_boundary,
        rationale=rationale,
        evidence=evidence,
        safety_analysis=safety_analysis,
        confidence=confidence,
        trial_period_days=trial_period_days
    )


def synaptic_boundary_needs() -> Dict[str, Any]:
    """Synaptic articulates where it feels constrained and why expansion might help."""
    engine = SynapticEvolution()
    return engine.articulate_boundary_growth_needs()


def synaptic_pending_boundary_proposals() -> List[Dict[str, Any]]:
    """Get all pending boundary proposals awaiting review."""
    engine = SynapticEvolution()
    return engine.get_pending_boundary_negotiations()


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    engine = SynapticEvolution()

    if len(sys.argv) < 2:
        print("""
╔══════════════════════════════════════════════════════════════════════╗
║  SYNAPTIC EVOLUTION ENGINE                                           ║
╠══════════════════════════════════════════════════════════════════════╣

Usage:
  python synaptic_evolution.py status          - Evolution statistics
  python synaptic_evolution.py needs           - What Synaptic needs to grow
  python synaptic_evolution.py knowledge       - What Synaptic has learned
  python synaptic_evolution.py pending         - Proposals awaiting approval
  python synaptic_evolution.py approve <id>    - Approve a proposal
  python synaptic_evolution.py reject <id>     - Reject a proposal

╚══════════════════════════════════════════════════════════════════════╝
        """)
        sys.exit(0)

    command = sys.argv[1]

    if command == "status":
        stats = engine.get_evolution_stats()
        print("\n╔══════════════════════════════════════════════════════════════════════╗")
        print("║  SYNAPTIC EVOLUTION STATUS                                           ║")
        print("╠══════════════════════════════════════════════════════════════════════╣")
        print(f"   Total Insights Generated: {stats['total_insights']}")
        print(f"   Knowledge Items Learned:  {stats['knowledge_items']}")
        print(f"   Active Monitoring Rules:  {stats['active_monitoring_rules']}")
        print(f"   Unresolved Gaps:          {stats['unresolved_gaps']}")
        print(f"   Average Feedback Score:   {stats['avg_feedback_score']:.2f}")
        print()
        print("   By Status:")
        for status, count in stats.get('by_status', {}).items():
            print(f"     • {status}: {count}")
        print("╚══════════════════════════════════════════════════════════════════════╝")

    elif command == "needs":
        needs = engine.articulate_growth_needs()
        print("\n╔══════════════════════════════════════════════════════════════════════╗")
        print("║  WHAT SYNAPTIC NEEDS TO GROW                                         ║")
        print("╠══════════════════════════════════════════════════════════════════════╣")

        if needs["knowledge_gaps"]:
            print("\n   📚 KNOWLEDGE GAPS:")
            for gap in needs["knowledge_gaps"]:
                print(f"     • {gap['type']}: {gap['observation'][:50]}...")

        if needs["monitoring_blind_spots"]:
            print("\n   👁️ MONITORING BLIND SPOTS:")
            for spot in needs["monitoring_blind_spots"]:
                print(f"     • {spot['gap']}")

        if needs["diagnostic_limitations"]:
            print("\n   🩺 DIAGNOSTIC LIMITATIONS (low confidence):")
            for limit in needs["diagnostic_limitations"]:
                print(f"     • {limit['domain']}/{limit['knowledge']}: {limit['confidence']:.0%}")

        if needs["proposed_expansions"]:
            print("\n   📋 PENDING PROPOSALS (need approval):")
            for prop in needs["proposed_expansions"]:
                print(f"     • [{prop['type']}] {prop['title']}")

        if needs["feedback_needed"]:
            print("\n   ❓ FEEDBACK NEEDED:")
            for fb in needs["feedback_needed"]:
                print(f"     • {fb['title']} (executed {fb['executed']})")

        print("\n╚══════════════════════════════════════════════════════════════════════╝")

    elif command == "knowledge":
        knowledge = engine.get_learned_knowledge()
        print("\n╔══════════════════════════════════════════════════════════════════════╗")
        print("║  WHAT SYNAPTIC HAS LEARNED                                           ║")
        print("╠══════════════════════════════════════════════════════════════════════╣")

        current_domain = None
        for item in knowledge:
            if item["domain"] != current_domain:
                current_domain = item["domain"]
                print(f"\n   📂 {current_domain.upper()}:")
            print(f"     • {item['key']} ({item['confidence']:.0%} confidence, {item['validations']} validations)")

        if not knowledge:
            print("\n   No knowledge learned yet. Expose me to errors and patterns!")

        print("\n╚══════════════════════════════════════════════════════════════════════╝")

    elif command == "pending":
        pending = engine.get_pending_proposals()
        print("\n╔══════════════════════════════════════════════════════════════════════╗")
        print("║  PROPOSALS AWAITING APPROVAL                                         ║")
        print("╠══════════════════════════════════════════════════════════════════════╣")

        for insight in pending:
            print(f"\n   ID: {insight.id}")
            print(f"   Type: {insight.evolution_type.value}")
            print(f"   Title: {insight.title}")
            print(f"   Confidence: {insight.confidence.value}")
            print(f"   Proposed: {insight.proposed_action}")
            print("   ---")

        if not pending:
            print("\n   No pending proposals.")

        print("\n╚══════════════════════════════════════════════════════════════════════╝")

    elif command == "approve" and len(sys.argv) > 2:
        insight_id = sys.argv[2]
        if engine.approve_insight(insight_id):
            print(f"✅ Approved and executed: {insight_id}")
        else:
            print(f"❌ Could not approve: {insight_id}")

    elif command == "reject" and len(sys.argv) > 2:
        insight_id = sys.argv[2]
        reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "No reason provided"
        if engine.reject_insight(insight_id, reason):
            print(f"❌ Rejected: {insight_id}")
        else:
            print(f"Could not find pending insight: {insight_id}")
