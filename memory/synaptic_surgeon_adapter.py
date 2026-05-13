"""
SYNAPTIC SURGEON ADAPTER — Bridges Synaptic into 3-Surgeons Evidence System

The 3-Surgeons protocol (Atlas/Cardiologist/Neurologist) does cross-examination
on claims and evidence. Synaptic's pattern database is a rich evidence source
that surgeons should have access to during cross-examination.

This adapter:
1. Provides Synaptic's patterns as structured evidence for surgeon queries
2. Allows surgical cross-examinations to query Synaptic's pattern database
3. Formats Synaptic findings as surgeon-compatible evidence dicts
4. Bridges the gap between Synaptic's persistent knowledge and surgical review

Usage:
    from memory.synaptic_surgeon_adapter import SynapticSurgeonAdapter

    adapter = SynapticSurgeonAdapter()

    # Get evidence for a cross-examination topic
    evidence = adapter.get_evidence_for_topic("async error handling")

    # Get Synaptic's perspective for surgical review
    perspective = adapter.get_surgical_perspective("webhook reliability")

    # Feed Synaptic evidence into a consensus call
    from memory.surgery_bridge import consensus
    synaptic_evidence = adapter.format_for_consensus("deployment stability")
    result = consensus("deployment is stable", evidence=synaptic_evidence)
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger("context_dna.synaptic_surgeon_adapter")

_failure_count = 0


@dataclass
class SurgeonEvidence:
    """Evidence formatted for the 3-Surgeons protocol."""
    source: str                  # Always "synaptic"
    evidence_type: str           # pattern, wisdom, belief_update, emotional
    title: str
    description: str
    confidence: float
    supporting_data: Dict        # Raw data backing the evidence
    session_span: int            # How many sessions this spans
    timestamp: str


class SynapticSurgeonAdapter:
    """
    Bridges Synaptic's pattern database into the 3-Surgeons evidence system.

    Surgeons get richer cross-examinations when they have access to
    Synaptic's cross-session pattern recognition and accumulated wisdom.
    """

    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._cache_ttl = 60  # seconds

    # =========================================================================
    # EVIDENCE GATHERING
    # =========================================================================

    def get_evidence_for_topic(self, topic: str, max_items: int = 10) -> List[SurgeonEvidence]:
        """
        Gather all Synaptic evidence relevant to a topic.

        Searches across:
        - Pattern engine (detected patterns)
        - Personality wisdom (accumulated insights)
        - Evolution engine (belief updates)
        - Emotional patterns (if relevant)

        Returns evidence sorted by confidence.
        """
        evidence = []
        topic_lower = topic.lower()
        topic_words = set(topic_lower.split())

        # 1. Patterns from pattern engine
        evidence.extend(self._gather_pattern_evidence(topic_words))

        # 2. Wisdom from personality
        evidence.extend(self._gather_wisdom_evidence(topic_words))

        # 3. Belief updates from personality
        evidence.extend(self._gather_belief_evidence(topic_words))

        # 4. Evolution insights
        evidence.extend(self._gather_evolution_evidence(topic_words))

        # Sort by confidence and limit
        evidence.sort(key=lambda e: e.confidence, reverse=True)
        return evidence[:max_items]

    def get_surgical_perspective(self, topic: str) -> str:
        """
        Get Synaptic's perspective on a topic, formatted for surgical review.

        Returns a narrative string that a surgeon can use as input.
        """
        evidence = self.get_evidence_for_topic(topic, max_items=5)
        if not evidence:
            return f"Synaptic has no accumulated evidence on: {topic}"

        lines = [f"Synaptic's perspective on '{topic}' (based on {len(evidence)} evidence items):"]
        lines.append("")

        for e in evidence:
            lines.append(f"  [{e.evidence_type}] {e.title}")
            lines.append(f"    {e.description[:150]}")
            lines.append(f"    Confidence: {e.confidence:.0%}, Span: {e.session_span} sessions")
            lines.append("")

        return "\n".join(lines)

    def format_for_consensus(self, topic: str) -> Dict[str, Any]:
        """
        Format Synaptic's evidence for a surgery_bridge.consensus() call.

        Returns a dict that can be passed as evidence to the consensus function.
        """
        evidence = self.get_evidence_for_topic(topic, max_items=5)
        return {
            "source": "synaptic_8th_intelligence",
            "topic": topic,
            "evidence_count": len(evidence),
            "items": [asdict(e) for e in evidence],
            "summary": self.get_surgical_perspective(topic),
            "gathered_at": datetime.now(timezone.utc).isoformat(),
        }

    def format_for_cardio_review(self, topic: str) -> str:
        """
        Format Synaptic's evidence for a cardio_review call.

        Returns a string suitable for passing to surgery_bridge.cardio_review().
        """
        evidence = self.get_evidence_for_topic(topic, max_items=5)
        if not evidence:
            return ""

        lines = [f"Evidence from Synaptic (8th Intelligence) on '{topic}':"]
        for e in evidence:
            lines.append(f"- [{e.evidence_type}] {e.title} (conf: {e.confidence:.0%}): {e.description[:100]}")
        return "\n".join(lines)

    # =========================================================================
    # EVIDENCE GATHERING INTERNALS
    # =========================================================================

    def _gather_pattern_evidence(self, topic_words: set) -> List[SurgeonEvidence]:
        """Gather evidence from pattern engine."""
        evidence = []
        try:
            from memory.synaptic_pattern_engine import get_pattern_engine
            engine = get_pattern_engine()
            patterns = engine.get_recent(limit=20)

            for p in patterns:
                # Relevance check: any word overlap between topic and pattern
                pattern_words = set(p.title.lower().split()) | set(p.description.lower().split())
                overlap = len(topic_words & pattern_words)
                if overlap > 0 or p.confidence >= 0.8:
                    evidence.append(SurgeonEvidence(
                        source="synaptic",
                        evidence_type="pattern",
                        title=p.title,
                        description=p.description,
                        confidence=p.confidence,
                        supporting_data={
                            "pattern_type": p.pattern_type,
                            "evidence": p.evidence[:3] if p.evidence else [],
                            "actionable": p.actionable,
                            "action": p.suggested_action,
                        },
                        session_span=p.session_span,
                        timestamp=p.detected_at,
                    ))
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.debug("Pattern evidence gather failed: %s", e)
        return evidence

    def _gather_wisdom_evidence(self, topic_words: set) -> List[SurgeonEvidence]:
        """Gather evidence from accumulated wisdom."""
        evidence = []
        try:
            from memory.synaptic_personality import get_personality
            personality = get_personality()
            wisdom = personality.get_wisdom(limit=15)

            for w in wisdom:
                wisdom_words = set(w.insight.lower().split()) | set(w.domain.lower().split())
                overlap = len(topic_words & wisdom_words)
                if overlap > 0 or w.confidence >= 0.8:
                    evidence.append(SurgeonEvidence(
                        source="synaptic",
                        evidence_type="wisdom",
                        title=f"Wisdom: {w.domain}",
                        description=w.insight,
                        confidence=w.confidence,
                        supporting_data={
                            "domain": w.domain,
                            "validation_count": w.validation_count,
                            "source_sessions": w.source_sessions,
                        },
                        session_span=w.source_sessions,
                        timestamp=w.last_validated,
                    ))
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.warning("Wisdom evidence gather failed: %s", e)
        return evidence

    def _gather_belief_evidence(self, topic_words: set) -> List[SurgeonEvidence]:
        """Gather evidence from belief updates."""
        evidence = []
        try:
            from memory.synaptic_personality import get_personality
            personality = get_personality()
            updates = personality.get_recent_belief_updates(limit=10)

            for u in updates:
                update_words = set(u.topic.lower().split())
                overlap = len(topic_words & update_words)
                if overlap > 0:
                    evidence.append(SurgeonEvidence(
                        source="synaptic",
                        evidence_type="belief_update",
                        title=f"Belief: {u.topic}",
                        description=f"Changed from '{u.before_state}' to '{u.after_state}' based on: {u.evidence}",
                        confidence=0.7 + abs(u.confidence_delta) * 0.3,
                        supporting_data={
                            "before": u.before_state,
                            "after": u.after_state,
                            "evidence": u.evidence,
                            "confidence_delta": u.confidence_delta,
                        },
                        session_span=1,
                        timestamp=u.timestamp,
                    ))
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.warning("Belief evidence gather failed: %s", e)
        return evidence

    def _gather_evolution_evidence(self, topic_words: set) -> List[SurgeonEvidence]:
        """Gather evidence from evolution engine."""
        evidence = []
        try:
            from memory.synaptic_evolution import SynapticEvolution
            evo = SynapticEvolution()
            insights = evo.get_pending_proposals()[:10]

            for i in insights:
                insight_words = set(i.title.lower().split()) | set(i.description.lower().split())
                overlap = len(topic_words & insight_words)
                if overlap > 0 or i.confidence.value in ("certain", "high"):
                    conf_map = {"certain": 0.95, "high": 0.8, "moderate": 0.6, "low": 0.4, "speculative": 0.2}
                    evidence.append(SurgeonEvidence(
                        source="synaptic",
                        evidence_type="evolution_insight",
                        title=i.title,
                        description=i.description,
                        confidence=conf_map.get(i.confidence.value, 0.5),
                        supporting_data={
                            "evolution_type": i.evolution_type.value,
                            "evidence": i.evidence[:3],
                            "status": i.status,
                        },
                        session_span=1,
                        timestamp=i.created_at,
                    ))
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.warning("Evolution evidence gather failed: %s", e)
        return evidence

    # =========================================================================
    # CONVENIENCE FUNCTIONS
    # =========================================================================

    def enrich_cross_exam(self, topic: str) -> Dict[str, Any]:
        """
        Prepare Synaptic evidence for a full cross-examination.

        Returns a dict suitable for injection into cross-exam context.
        """
        evidence = self.get_evidence_for_topic(topic)
        perspective = self.get_surgical_perspective(topic)

        return {
            "synaptic_evidence": [asdict(e) for e in evidence],
            "synaptic_perspective": perspective,
            "evidence_count": len(evidence),
            "confidence_range": (
                f"{min(e.confidence for e in evidence):.0%}-{max(e.confidence for e in evidence):.0%}"
                if evidence else "N/A"
            ),
        }


# =========================================================================
# SINGLETON & CONVENIENCE
# =========================================================================

_adapter: Optional[SynapticSurgeonAdapter] = None


def get_surgeon_adapter() -> SynapticSurgeonAdapter:
    """Get the global surgeon adapter instance."""
    global _adapter
    if _adapter is None:
        _adapter = SynapticSurgeonAdapter()
    return _adapter


def synaptic_evidence(topic: str) -> List[Dict]:
    """Quick: get Synaptic evidence as list of dicts."""
    adapter = get_surgeon_adapter()
    return [asdict(e) for e in adapter.get_evidence_for_topic(topic)]


def synaptic_perspective(topic: str) -> str:
    """Quick: get Synaptic's narrative perspective on a topic."""
    return get_surgeon_adapter().get_surgical_perspective(topic)
