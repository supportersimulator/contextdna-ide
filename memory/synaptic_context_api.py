#!/usr/bin/env python3
"""
Synaptic Context API - Single Source of Truth

ALL interfaces query this for Synaptic's contextual understanding:
- Webhook (IDE injection)
- Chat UI
- Memory UI
- Voice
- Future interfaces

INVARIANCE: Same context everywhere.
"""

import json
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict


@dataclass
class SynapticContext:
    """Unified context structure - same everywhere."""
    # Current work
    current_intent: Optional[str] = None
    related_sessions: List[Dict] = None

    # Memory
    relevant_learnings: List[Dict] = None
    active_patterns: List[str] = None

    # State
    brain_state_summary: Optional[str] = None
    confidence: float = 0.0
    sources: List[str] = None

    # Meta
    timestamp: str = None

    def __post_init__(self):
        if self.related_sessions is None:
            self.related_sessions = []
        if self.relevant_learnings is None:
            self.relevant_learnings = []
        if self.active_patterns is None:
            self.active_patterns = []
        if self.sources is None:
            self.sources = []
        if self.timestamp is None:
            self.timestamp = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_display_text(self) -> str:
        """Format for human display (Chat UI, etc.)"""
        lines = []

        if self.current_intent:
            lines.append(f"**Current Work:** {self.current_intent}")

        if self.related_sessions:
            lines.append("\n**Related Sessions:**")
            for s in self.related_sessions[:3]:
                outcome = f" → {s.get('outcome')}" if s.get('outcome') else ""
                lines.append(f"- {s.get('intent', 'Unknown')}{outcome}")

        if self.relevant_learnings:
            lines.append("\n**Relevant Learnings:**")
            for l in self.relevant_learnings[:3]:
                lines.append(f"- {l.get('title', str(l))[:80]}")

        if self.active_patterns:
            lines.append("\n**Active Patterns:**")
            for p in self.active_patterns[:3]:
                lines.append(f"- {p[:80]}")

        if not lines:
            return "No relevant context found."

        return "\n".join(lines)


def get_synaptic_context(query: str = None) -> SynapticContext:
    """
    Get Synaptic's unified context.

    This is THE function that all interfaces should call.
    Same context, same understanding, everywhere.
    """
    ctx = SynapticContext()

    # 1. Session Intent
    try:
        from memory.session_intent import get_tracker, find_relevant
        tracker = get_tracker()
        current = tracker.get_current_intent()
        if current:
            ctx.current_intent = current
            ctx.sources.append("session_intent")

        if query:
            relevant = find_relevant(query)
            ctx.related_sessions = [
                {"intent": s.intent, "outcome": s.outcome}
                for s in relevant[:5]
            ]
    except Exception as e:
        print(f"[WARN] Session context lookup failed: {e}")

    # 2. SynapticVoice (learnings, patterns, brain state)
    try:
        from memory.synaptic_voice import get_voice
        voice = get_voice()
        response = voice.consult(query or "current context")

        ctx.confidence = response.confidence

        if response.relevant_learnings:
            ctx.relevant_learnings = response.relevant_learnings[:5]
            ctx.sources.append("learnings")

        if response.relevant_patterns:
            ctx.active_patterns = response.relevant_patterns[:5]
            ctx.sources.append("patterns")

        if response.synaptic_perspective:
            ctx.brain_state_summary = response.synaptic_perspective[:500]
            ctx.sources.append("brain_state")
    except Exception as e:
        print(f"[WARN] SynapticVoice consultation failed: {e}")

    return ctx


def get_context_for_display(query: str = None) -> Dict[str, Any]:
    """Get context formatted for UI display."""
    ctx = get_synaptic_context(query)
    return {
        "context": ctx.to_dict(),
        "display_text": ctx.to_display_text(),
        "confidence": ctx.confidence,
        "sources": ctx.sources
    }


# FastAPI endpoint (can be imported by other servers)
def create_context_router():
    """Create FastAPI router for context API."""
    from fastapi import APIRouter

    router = APIRouter(prefix="/synaptic", tags=["synaptic"])

    @router.get("/context")
    async def api_context(q: str = None):
        return get_context_for_display(q)

    @router.get("/context/raw")
    async def api_context_raw(q: str = None):
        return get_synaptic_context(q).to_dict()

    return router


if __name__ == "__main__":
    # Test
    import sys
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None

    print("Synaptic Context API - Testing")
    print("=" * 50)

    ctx = get_synaptic_context(query)
    print(f"\nQuery: {query or '(none)'}")
    print(f"Confidence: {ctx.confidence:.0%}")
    print(f"Sources: {', '.join(ctx.sources)}")
    print("\n" + ctx.to_display_text())
