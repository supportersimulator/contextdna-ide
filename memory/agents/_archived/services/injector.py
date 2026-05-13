"""
Injector Agent - Context Injection Service

The Injector delivers context to the coding agent - formatting
and delivering the right information at the right time.

Anatomical Label: Context Injection Service
"""

from __future__ import annotations
import os
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class InjectorAgent(Agent):
    """
    Injector Agent - Context delivery to coding agent.

    Responsibilities:
    - Format context for injection
    - Deliver via webhook
    - Track injection outcomes
    - Optimize delivery timing
    """

    NAME = "injector"
    CATEGORY = AgentCategory.SERVICES
    DESCRIPTION = "Context injection delivery to coding agent"
    ANATOMICAL_LABEL = "Context Injection Service"
    IS_VITAL = True

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._injection_log: List[Dict[str, Any]] = []
        self._injection_stats: Dict[str, int] = {
            "total": 0,
            "delivered": 0,
            "failed": 0
        }
        self._last_injection: Optional[Dict[str, Any]] = None

    def _on_start(self):
        """Initialize injector."""
        pass

    def _on_stop(self):
        """Shutdown injector."""
        self._injection_log.clear()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check injector health."""
        success_rate = (
            self._injection_stats["delivered"] / self._injection_stats["total"]
            if self._injection_stats["total"] > 0 else 1.0
        )
        return {
            "healthy": success_rate > 0.8,
            "score": success_rate,
            "message": f"Delivered {self._injection_stats['delivered']}/{self._injection_stats['total']} injections",
            "metrics": self._injection_stats
        }

    def process(self, input_data: Any) -> Any:
        """Process injector operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "inject")
            if op == "inject":
                return self.inject(input_data.get("content"), input_data.get("context", {}))
            elif op == "format":
                return self.format_injection(input_data.get("sections", []))
            elif op == "get_last":
                return self._last_injection
            elif op == "stats":
                return self._injection_stats
        return None

    def inject(self, content: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Inject context content.

        This delivers the formatted context to the coding agent.
        """
        self._injection_stats["total"] += 1

        try:
            injection = {
                "content": content,
                "context": context or {},
                "timestamp": datetime.utcnow().isoformat(),
                "content_length": len(content) if content else 0
            }

            # Validate content
            if not content or len(content) < 10:
                self._injection_stats["failed"] += 1
                return {"status": "failed", "reason": "empty_or_short_content"}

            # Log injection
            self._injection_log.append(injection)
            if len(self._injection_log) > 100:
                self._injection_log = self._injection_log[-50:]

            self._last_injection = injection
            self._injection_stats["delivered"] += 1
            self._last_active = datetime.utcnow()

            # Notify observer
            self.send_message("observer", "injection_event", {
                "type": "injection",
                "length": len(content),
                "sections": context.get("sections", [])
            })

            return {
                "status": "delivered",
                "content_length": len(content),
                "timestamp": injection["timestamp"]
            }

        except Exception as e:
            self._injection_stats["failed"] += 1
            self._handle_error(e)
            return {"status": "failed", "error": str(e)}

    def format_injection(self, sections: List[Dict[str, Any]]) -> str:
        """
        Format sections into injection content.

        Sections are assembled according to the 9-section architecture.
        """
        output_parts = []

        for section in sections:
            section_id = section.get("id", "unknown")
            content = section.get("content", "")
            title = section.get("title", f"Section {section_id}")

            if content:
                output_parts.append(f"{'═' * 50}")
                output_parts.append(f"║ {title}")
                output_parts.append(f"{'═' * 50}")
                output_parts.append(content)
                output_parts.append("")

        return "\n".join(output_parts)

    def get_recent_injections(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent injection log."""
        return self._injection_log[-limit:]

    def get_injection_stats(self) -> Dict[str, Any]:
        """Get injection statistics."""
        return {
            **self._injection_stats,
            "success_rate": (
                self._injection_stats["delivered"] / self._injection_stats["total"]
                if self._injection_stats["total"] > 0 else 1.0
            ),
            "average_length": (
                sum(i.get("content_length", 0) for i in self._injection_log) / len(self._injection_log)
                if self._injection_log else 0
            )
        }

    def optimize_content(self, content: str, max_length: int = 10000) -> str:
        """Optimize content length while preserving important information."""
        if len(content) <= max_length:
            return content

        # Simple truncation with marker
        # In production, this would use smarter summarization
        return content[:max_length - 50] + "\n\n[Content truncated for length...]"

    def validate_injection(self, content: str) -> Dict[str, Any]:
        """Validate injection content before delivery."""
        issues = []

        if not content:
            issues.append("empty_content")

        if len(content) > 50000:
            issues.append("content_too_long")

        # Check for required markers
        if "NEVER DO" not in content and len(content) > 100:
            issues.append("missing_safety_section")

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "content_length": len(content) if content else 0
        }
