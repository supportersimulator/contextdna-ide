"""
SYNAPTIC EVOLUTION ENGINE — Tracks How Understanding Evolves

Distinct from synaptic_evolution.py (which handles autonomous learning within safety bounds),
this module tracks the META-EVOLUTION: how Synaptic's beliefs, patterns, and understanding
change over time. It provides before/after snapshots of belief updates.

Key concept: Every significant observation triggers a potential belief update.
The engine compares new observations against existing beliefs and records
when and why understanding shifts.

This creates a timeline of Synaptic's intellectual growth — visible to the
family and usable by surgeons for evidence-based review.

Usage:
    from memory.synaptic_evolution_engine import get_evolution_tracker

    tracker = get_evolution_tracker()

    # Process a new observation that might update beliefs
    update = tracker.process_observation(
        domain="architecture",
        observation="webhook latency improved after connection pooling",
        context={"latency_before": 200, "latency_after": 45}
    )

    # Get evolution timeline
    timeline = tracker.get_timeline(domain="architecture", limit=20)

    # Get a snapshot of current vs historical beliefs
    snapshot = tracker.belief_snapshot("webhook_reliability")
"""

import json
import logging
import hashlib
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict

from memory.db_utils import safe_conn

def _t_trk(name: str) -> str:
    from memory.db_utils import unified_table
    return unified_table(".synaptic_evolution_tracking.db", name)


logger = logging.getLogger("context_dna.synaptic_evolution_engine")

_LEGACY_DB = Path.home() / ".context-dna" / ".synaptic_evolution_tracking.db"


def _get_db_path() -> Path:
    from memory.db_utils import get_unified_db_path
    return get_unified_db_path(_LEGACY_DB)


DB_PATH = _get_db_path()

_failure_count = 0


@dataclass
class BeliefState:
    """A belief at a point in time."""
    domain: str
    topic: str
    belief: str
    confidence: float
    evidence_count: int
    last_updated: str


@dataclass
class EvolutionEvent:
    """A single evolution event — a belief changing."""
    id: str
    domain: str
    topic: str
    before_belief: str
    before_confidence: float
    after_belief: str
    after_confidence: float
    trigger: str              # What caused the evolution
    trigger_type: str         # observation, feedback, contradiction, reinforcement
    evidence: str             # Specific evidence that caused the shift
    timestamp: str
    session_id: Optional[str] = None


@dataclass
class EvolutionSnapshot:
    """Before/after snapshot of belief state for a topic."""
    topic: str
    domain: str
    current_belief: str
    current_confidence: float
    evolution_count: int      # How many times this belief has evolved
    first_belief: str         # What Synaptic originally believed
    first_confidence: float
    evolution_events: List[EvolutionEvent]
    total_evidence: int


class SynapticEvolutionTracker:
    """
    Tracks the meta-evolution of Synaptic's understanding over time.

    Every belief has a history. This tracker maintains that history
    so the family can see how understanding evolved and surgeons
    can use belief trajectories as evidence.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _init_database(self):
        """Initialize evolution tracking database."""
        global _failure_count
        try:
            with safe_conn(self.db_path) as conn:
                conn.executescript(f"""
                    CREATE TABLE IF NOT EXISTS {_t_trk('beliefs')} (
                        domain TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        belief TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        evidence_count INTEGER DEFAULT 1,
                        first_stated TEXT NOT NULL,
                        last_updated TEXT NOT NULL,
                        PRIMARY KEY (domain, topic)
                    );

                    CREATE TABLE IF NOT EXISTS {_t_trk('evolution_events')} (
                        id TEXT PRIMARY KEY,
                        domain TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        before_belief TEXT NOT NULL,
                        before_confidence REAL NOT NULL,
                        after_belief TEXT NOT NULL,
                        after_confidence REAL NOT NULL,
                        trigger_desc TEXT NOT NULL,
                        trigger_type TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        session_id TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_events_domain
                        ON {_t_trk('evolution_events')}(domain);
                    CREATE INDEX IF NOT EXISTS idx_events_topic
                        ON {_t_trk('evolution_events')}(topic);
                    CREATE INDEX IF NOT EXISTS idx_events_time
                        ON {_t_trk('evolution_events')}(timestamp DESC);
                """)
        except Exception as e:
            _failure_count += 1
            logger.error("Evolution tracking DB init failed: %s", e)
            raise

    # =========================================================================
    # CORE: Process Observation
    # =========================================================================

    def process_observation(
        self,
        domain: str,
        observation: str,
        context: Optional[Dict] = None,
        session_id: Optional[str] = None,
    ) -> Optional[EvolutionEvent]:
        """
        Process a new observation that might update beliefs.

        Steps:
        1. Check if we have an existing belief for this domain/topic
        2. Use LLM to determine if the observation changes the belief
        3. If changed, record the evolution event
        4. Update personality's belief tracking

        Returns EvolutionEvent if a belief was updated, None if reinforced or unchanged.
        """
        global _failure_count
        context = context or {}

        # Extract topic from observation (use LLM for non-trivial extraction)
        topic = self._extract_topic(domain, observation)
        if not topic:
            return None

        # Get current belief
        current = self._get_belief(domain, topic)

        if current is None:
            # First observation for this topic — establish baseline belief
            self._set_belief(domain, topic, observation, confidence=0.5)
            # Also feed to personality
            self._update_personality_belief(domain, topic, "", observation, observation, 0.5)
            return None  # No evolution event for first belief

        # Compare new observation with existing belief
        evolution = self._evaluate_evolution(current, observation, context)

        if evolution is None:
            # Reinforcement, not change — bump evidence count
            self._reinforce_belief(domain, topic)
            return None

        # Belief has evolved — record it
        event_id = hashlib.sha256(
            f"{domain}:{topic}:{datetime.now(timezone.utc).isoformat()}".encode()
        ).hexdigest()[:12]

        event = EvolutionEvent(
            id=event_id,
            domain=domain,
            topic=topic,
            before_belief=current.belief,
            before_confidence=current.confidence,
            after_belief=evolution["new_belief"],
            after_confidence=evolution["new_confidence"],
            trigger=observation[:200],
            trigger_type=evolution.get("trigger_type", "observation"),
            evidence=json.dumps(context)[:500] if context else observation[:200],
            timestamp=datetime.now(timezone.utc).isoformat(),
            session_id=session_id,
        )

        self._record_event(event)
        self._set_belief(domain, topic, event.after_belief, event.after_confidence)
        self._update_personality_belief(
            domain, topic, event.before_belief, event.after_belief,
            observation, event.after_confidence - event.before_confidence
        )

        return event

    # =========================================================================
    # BELIEF MANAGEMENT
    # =========================================================================

    def _get_belief(self, domain: str, topic: str) -> Optional[BeliefState]:
        """Get current belief for a domain/topic."""
        try:
            with safe_conn(self.db_path) as conn:
                row = conn.execute(
                    f"SELECT * FROM {_t_trk('beliefs')} WHERE domain=? AND topic=?",
                    (domain, topic)
                ).fetchone()
                if row:
                    return BeliefState(
                        domain=row["domain"], topic=row["topic"],
                        belief=row["belief"], confidence=row["confidence"],
                        evidence_count=row["evidence_count"],
                        last_updated=row["last_updated"],
                    )
        except Exception as e:
            logger.debug("get_belief failed: %s", e)
        return None

    def _set_belief(self, domain: str, topic: str, belief: str, confidence: float) -> None:
        """Set or update a belief."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            with safe_conn(self.db_path) as conn:
                conn.execute(f"""
                    INSERT INTO {_t_trk('beliefs')} (domain, topic, belief, confidence, evidence_count, first_stated, last_updated)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(domain, topic) DO UPDATE SET
                        belief=excluded.belief, confidence=excluded.confidence,
                        evidence_count=evidence_count+1, last_updated=excluded.last_updated
                """, (domain, topic, belief, confidence, now, now))
                conn.commit()
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.error("set_belief failed: %s", e)

    def _reinforce_belief(self, domain: str, topic: str) -> None:
        """Reinforce existing belief (bump evidence count, slight confidence boost)."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            with safe_conn(self.db_path) as conn:
                conn.execute(f"""
                    UPDATE {_t_trk('beliefs')} SET
                        evidence_count = evidence_count + 1,
                        confidence = MIN(0.99, confidence + 0.02),
                        last_updated = ?
                    WHERE domain=? AND topic=?
                """, (now, domain, topic))
                conn.commit()
        except Exception as e:
            logger.debug("reinforce_belief failed: %s", e)

    def _record_event(self, event: EvolutionEvent) -> None:
        """Record an evolution event."""
        try:
            with safe_conn(self.db_path) as conn:
                conn.execute(f"""
                    INSERT INTO {_t_trk('evolution_events')}
                    (id, domain, topic, before_belief, before_confidence,
                     after_belief, after_confidence, trigger_desc, trigger_type,
                     evidence, timestamp, session_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event.id, event.domain, event.topic,
                    event.before_belief, event.before_confidence,
                    event.after_belief, event.after_confidence,
                    event.trigger, event.trigger_type,
                    event.evidence, event.timestamp, event.session_id,
                ))
                conn.commit()
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.error("record_event failed: %s", e)

    # =========================================================================
    # TOPIC EXTRACTION & EVOLUTION EVALUATION
    # =========================================================================

    def _extract_topic(self, domain: str, observation: str) -> Optional[str]:
        """Extract a topic key from an observation. Uses heuristic first, LLM if needed."""
        # Simple heuristic: first 4-5 significant words
        words = [w.lower().strip(".,;:!?") for w in observation.split()
                 if len(w) > 3 and w.lower() not in {"that", "this", "with", "from", "have", "been", "were", "they"}]
        if words:
            return "_".join(words[:4])
        return None

    def _evaluate_evolution(
        self, current: BeliefState, observation: str, context: Dict
    ) -> Optional[Dict]:
        """
        Evaluate whether an observation changes a belief.

        Uses LLM to compare current belief with new observation.
        Returns dict with new_belief, new_confidence, trigger_type if changed.
        Returns None if belief is reinforced (not changed).
        """
        try:
            from memory.llm_priority_queue import llm_generate, Priority

            system_prompt = (
                "You evaluate whether a new observation changes an existing belief. "
                "Output ONLY a JSON object with:\n"
                "- changed: true/false\n"
                "- new_belief: updated belief statement (if changed)\n"
                "- new_confidence: 0.0-1.0\n"
                "- trigger_type: one of [contradiction, refinement, expansion, reinforcement]\n"
                "If the observation merely reinforces the existing belief, set changed=false."
            )

            user_prompt = (
                f"Domain: {current.domain}\n"
                f"Topic: {current.topic}\n"
                f"Current belief: {current.belief}\n"
                f"Current confidence: {current.confidence}\n"
                f"New observation: {observation}\n"
                f"Context: {json.dumps(context)[:300]}"
            )

            result = llm_generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                priority=Priority.BACKGROUND,
                profile="classify",
                caller="synaptic_evolution_tracker",
                timeout_s=15.0,
            )

            if not result:
                return None

            # Parse response
            import re
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                if data.get("changed"):
                    return {
                        "new_belief": data.get("new_belief", observation),
                        "new_confidence": min(0.99, max(0.1, data.get("new_confidence", 0.5))),
                        "trigger_type": data.get("trigger_type", "observation"),
                    }
            return None

        except Exception as e:
            logger.debug("Evolution evaluation failed: %s", e)
            return None

    def _update_personality_belief(
        self, domain: str, topic: str, before: str, after: str,
        evidence: str, confidence_delta: float
    ) -> None:
        """Feed belief update back to personality system."""
        try:
            from memory.synaptic_personality import get_personality
            personality = get_personality()
            personality.update_belief(
                topic=f"{domain}/{topic}",
                before=before[:200],
                after=after[:200],
                evidence=evidence[:200],
                confidence_delta=confidence_delta,
            )
        except Exception as e:
            logger.debug("Personality belief update failed: %s", e)

    # =========================================================================
    # TIMELINE & SNAPSHOTS
    # =========================================================================

    def get_timeline(
        self, domain: Optional[str] = None, limit: int = 20
    ) -> List[EvolutionEvent]:
        """Get evolution timeline, optionally filtered by domain."""
        try:
            with safe_conn(self.db_path) as conn:
                if domain:
                    rows = conn.execute(
                        f"SELECT * FROM {_t_trk('evolution_events')} WHERE domain=? ORDER BY timestamp DESC LIMIT ?",
                        (domain, limit)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT * FROM {_t_trk('evolution_events')} ORDER BY timestamp DESC LIMIT ?",
                        (limit,)
                    ).fetchall()
                return [
                    EvolutionEvent(
                        id=r["id"], domain=r["domain"], topic=r["topic"],
                        before_belief=r["before_belief"],
                        before_confidence=r["before_confidence"],
                        after_belief=r["after_belief"],
                        after_confidence=r["after_confidence"],
                        trigger=r["trigger_desc"], trigger_type=r["trigger_type"],
                        evidence=r["evidence"], timestamp=r["timestamp"],
                        session_id=r["session_id"],
                    ) for r in rows
                ]
        except Exception as e:
            logger.error("get_timeline failed: %s", e)
            return []

    def belief_snapshot(self, topic: str) -> Optional[EvolutionSnapshot]:
        """
        Get a full before/after snapshot for a topic.

        Shows the complete evolution history of a belief.
        """
        try:
            with safe_conn(self.db_path) as conn:
                # Find the belief across domains
                beliefs = conn.execute(
                    f"SELECT * FROM {_t_trk('beliefs')} WHERE topic LIKE ?",
                    (f"%{topic}%",)
                ).fetchall()

                if not beliefs:
                    return None

                belief = beliefs[0]
                domain = belief["domain"]

                # Get all evolution events for this topic
                events = conn.execute(
                    f"SELECT * FROM {_t_trk('evolution_events')} WHERE domain=? AND topic=? ORDER BY timestamp ASC",
                    (domain, belief["topic"])
                ).fetchall()

                event_list = [
                    EvolutionEvent(
                        id=r["id"], domain=r["domain"], topic=r["topic"],
                        before_belief=r["before_belief"],
                        before_confidence=r["before_confidence"],
                        after_belief=r["after_belief"],
                        after_confidence=r["after_confidence"],
                        trigger=r["trigger_desc"], trigger_type=r["trigger_type"],
                        evidence=r["evidence"], timestamp=r["timestamp"],
                        session_id=r["session_id"],
                    ) for r in events
                ]

                first_belief = event_list[0].before_belief if event_list else belief["belief"]
                first_confidence = event_list[0].before_confidence if event_list else belief["confidence"]

                return EvolutionSnapshot(
                    topic=belief["topic"],
                    domain=domain,
                    current_belief=belief["belief"],
                    current_confidence=belief["confidence"],
                    evolution_count=len(event_list),
                    first_belief=first_belief,
                    first_confidence=first_confidence,
                    evolution_events=event_list,
                    total_evidence=belief["evidence_count"],
                )
        except Exception as e:
            logger.error("belief_snapshot failed: %s", e)
            return None

    def get_all_beliefs(self, domain: Optional[str] = None) -> List[BeliefState]:
        """Get all current beliefs."""
        try:
            with safe_conn(self.db_path) as conn:
                if domain:
                    rows = conn.execute(
                        f"SELECT * FROM {_t_trk('beliefs')} WHERE domain=? ORDER BY confidence DESC",
                        (domain,)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT * FROM {_t_trk('beliefs')} ORDER BY confidence DESC"
                    ).fetchall()
                return [
                    BeliefState(
                        domain=r["domain"], topic=r["topic"], belief=r["belief"],
                        confidence=r["confidence"], evidence_count=r["evidence_count"],
                        last_updated=r["last_updated"],
                    ) for r in rows
                ]
        except Exception as e:
            logger.error("get_all_beliefs failed: %s", e)
            return []

    def get_stats(self) -> Dict:
        """Get evolution tracking stats."""
        try:
            with safe_conn(self.db_path) as conn:
                belief_count = conn.execute(f"SELECT COUNT(*) as c FROM {_t_trk('beliefs')}").fetchone()["c"]
                event_count = conn.execute(f"SELECT COUNT(*) as c FROM {_t_trk('evolution_events')}").fetchone()["c"]
                domains = conn.execute(f"SELECT DISTINCT domain FROM {_t_trk('beliefs')}").fetchall()
                return {
                    "total_beliefs": belief_count,
                    "total_evolution_events": event_count,
                    "domains": [r["domain"] for r in domains],
                    "failure_count": _failure_count,
                }
        except Exception as exc:
            logger.warning("Failed to get evolution stats: %s", exc)
            return {"error": "stats unavailable"}


# =========================================================================
# SINGLETON
# =========================================================================

_tracker: Optional[SynapticEvolutionTracker] = None


def get_evolution_tracker(db_path: Optional[Path] = None) -> SynapticEvolutionTracker:
    """Get the global evolution tracker instance."""
    global _tracker
    if _tracker is None:
        _tracker = SynapticEvolutionTracker(db_path)
    return _tracker
