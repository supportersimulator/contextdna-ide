"""
Prefrontal Agent - Injection Orchestrator

The Prefrontal orchestrates context injection - assembling the right
context, in the right format, at the right time for the coding agent.

Anatomical Label: Prefrontal Cortex (Injection Orchestrator)
"""

from __future__ import annotations
import os
from datetime import datetime
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class PrefrontalAgent(Agent):
    """
    Prefrontal Agent - Context injection orchestration.

    Responsibilities:
    - Assemble injection payloads
    - Coordinate section generation
    - Manage injection timing
    - Optimize context delivery
    """

    NAME = "prefrontal"
    CATEGORY = AgentCategory.CONTROL
    DESCRIPTION = "Context injection orchestration and assembly"
    ANATOMICAL_LABEL = "Prefrontal Cortex (Injection Orchestrator)"
    IS_VITAL = True

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._injection_stats: Dict[str, int] = {
            "total": 0,
            "success": 0,
            "partial": 0,
            "failed": 0
        }
        self._last_injection: Optional[Dict[str, Any]] = None

    def _on_start(self):
        """Initialize orchestrator."""
        pass

    def _on_stop(self):
        """Shutdown orchestrator."""
        pass

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check prefrontal health."""
        success_rate = (
            self._injection_stats["success"] / self._injection_stats["total"]
            if self._injection_stats["total"] > 0 else 1.0
        )
        return {
            "healthy": success_rate > 0.8,
            "score": success_rate,
            "message": f"Injection success rate: {success_rate:.1%}",
            "metrics": self._injection_stats
        }

    def process(self, input_data: Any) -> Any:
        """Process orchestration operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "orchestrate")
            if op == "orchestrate":
                return self.orchestrate_injection(
                    input_data.get("prompt"),
                    input_data.get("context", {})
                )
            elif op == "get_stats":
                return self.get_stats()
        return None

    def orchestrate_injection(self, prompt: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Orchestrate a context injection.

        This is the main coordination point that calls the actual
        injection system and tracks results.
        """
        self._injection_stats["total"] += 1

        try:
            # Use the actual injection system
            from memory.persistent_hook_structure import generate_context_injection

            session_id = context.get("session_id") if context else None
            mode = context.get("mode", "hybrid") if context else "hybrid"

            result = generate_context_injection(
                prompt=prompt,
                mode=mode,
                session_id=session_id
            )

            # Track results
            self._last_injection = {
                "timestamp": datetime.utcnow().isoformat(),
                "prompt_preview": prompt[:100],
                "sections": result.sections_included,
                "volume_tier": result.volume_tier,
                "content_length": len(result.content)
            }

            if len(result.sections_included) >= 3:
                self._injection_stats["success"] += 1
                status = "success"
            else:
                self._injection_stats["partial"] += 1
                status = "partial"

            self._last_active = datetime.utcnow()

            return {
                "status": status,
                "content": result.content,
                "sections": result.sections_included,
                "volume_tier": result.volume_tier
            }

        except Exception as e:
            self._injection_stats["failed"] += 1
            self._handle_error(e)
            return {
                "status": "failed",
                "error": str(e)
            }

    def get_stats(self) -> Dict[str, Any]:
        """Get injection statistics."""
        return {
            "stats": self._injection_stats,
            "last_injection": self._last_injection
        }

    def get_injection_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent injection history."""
        # This would integrate with the injection_store
        try:
            from memory.injection_store import get_recent_injections
            return get_recent_injections(limit)
        except ImportError:
            return []

    def suggest_mode(self, prompt: str) -> str:
        """Suggest the best injection mode for a prompt."""
        prompt_lower = prompt.lower()

        # High complexity prompts
        if any(kw in prompt_lower for kw in ["refactor", "architect", "design", "implement"]):
            return "layered"

        # Simple queries
        if any(kw in prompt_lower for kw in ["what is", "how do", "explain", "?"]):
            return "minimal"

        # Default
        return "hybrid"
