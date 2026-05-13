"""
Thalamus Agent - Relevance Filter/Gate

The Thalamus filters and routes information - deciding what's relevant
for the current context and what should be passed through to higher
processing or stored for later.

Anatomical Label: Thalamus (Relevance Filter)
"""

from __future__ import annotations
from datetime import datetime
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class ThalamusAgent(Agent):
    """
    Thalamus Agent - Relevance filtering and routing.

    Responsibilities:
    - Filter relevant information
    - Route data to appropriate agents
    - Gate information flow
    - Prioritize based on context
    """

    NAME = "thalamus"
    CATEGORY = AgentCategory.CONTROL
    DESCRIPTION = "Relevance filtering and information routing"
    ANATOMICAL_LABEL = "Thalamus (Relevance Filter)"
    IS_VITAL = True

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._relevance_cache: Dict[str, float] = {}
        self._current_context: Dict[str, Any] = {}
        self._filter_rules: List[Dict[str, Any]] = []

    def _on_start(self):
        """Initialize filtering rules."""
        self._load_default_rules()

    def _on_stop(self):
        """Shutdown thalamus."""
        self._relevance_cache.clear()

    def _load_default_rules(self):
        """Load default filtering rules."""
        self._filter_rules = [
            # High priority: errors and failures
            {"pattern": "error", "boost": 2.0, "route": "cerebellum"},
            {"pattern": "fail", "boost": 2.0, "route": "cerebellum"},
            {"pattern": "exception", "boost": 2.0, "route": "cerebellum"},

            # High priority: success signals
            {"pattern": "success", "boost": 1.5, "route": "neocortex"},
            {"pattern": "worked", "boost": 1.5, "route": "neocortex"},
            {"pattern": "fixed", "boost": 1.5, "route": "neocortex"},

            # Context signals
            {"pattern": "deploy", "boost": 1.3, "context": "deployment"},
            {"pattern": "test", "boost": 1.2, "context": "testing"},
            {"pattern": "debug", "boost": 1.2, "context": "debugging"},

            # Low priority: noise
            {"pattern": "todo", "boost": 0.5},
            {"pattern": "fixme", "boost": 0.5},
        ]

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check thalamus health."""
        return {
            "healthy": True,
            "score": 1.0,
            "message": f"Filtering with {len(self._filter_rules)} rules",
            "metrics": {
                "filter_rules": len(self._filter_rules),
                "cache_size": len(self._relevance_cache),
                "current_context": self._current_context.get("type", "general")
            }
        }

    def process(self, input_data: Any) -> Any:
        """Process filtering operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "filter")
            if op == "filter":
                return self.filter(input_data.get("content"), input_data.get("context"))
            elif op == "set_context":
                return self.set_context(input_data)
            elif op == "assess_relevance":
                return self.assess_relevance(input_data.get("content"))
        return None

    def filter(self, content: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Filter content for relevance.

        Returns:
            {
                "relevant": bool,
                "score": float (0-1),
                "route": optional target agent,
                "context_updates": any context changes
            }
        """
        if not content:
            return {"relevant": False, "score": 0.0}

        # Update context if provided
        if context:
            self._current_context.update(context)

        # Calculate relevance score
        score = 0.5  # Base score
        route = None
        context_updates = {}
        content_lower = content.lower()

        for rule in self._filter_rules:
            pattern = rule["pattern"]
            if pattern in content_lower:
                score *= rule.get("boost", 1.0)
                if "route" in rule:
                    route = rule["route"]
                if "context" in rule:
                    context_updates["type"] = rule["context"]

        # Clamp score
        score = max(0.0, min(1.0, score))

        # Cache result
        import hashlib
        cache_key = hashlib.sha256(content[:100].encode()).hexdigest()[:16]
        self._relevance_cache[cache_key] = score

        self._last_active = datetime.utcnow()

        return {
            "relevant": score > 0.3,
            "score": score,
            "route": route,
            "context_updates": context_updates
        }

    def assess_relevance(self, content: str) -> float:
        """Quick relevance assessment without full filtering."""
        result = self.filter(content)
        return result.get("score", 0.5)

    def set_context(self, context: Dict[str, Any]) -> bool:
        """Set the current filtering context."""
        self._current_context = context
        return True

    def get_context(self) -> Dict[str, Any]:
        """Get the current filtering context."""
        return self._current_context.copy()

    def add_rule(self, pattern: str, boost: float = 1.0, route: str = None, context: str = None):
        """Add a filtering rule."""
        rule = {"pattern": pattern, "boost": boost}
        if route:
            rule["route"] = route
        if context:
            rule["context"] = context
        self._filter_rules.append(rule)

    def route_to(self, content: str) -> Optional[str]:
        """Determine where content should be routed."""
        result = self.filter(content)
        return result.get("route")
