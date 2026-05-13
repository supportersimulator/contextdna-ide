"""
Ears Agent - Auditory Cortex / Runtime & Console Observers

The Ears listen to runtime events - console output, logs, errors,
and webhook callbacks to keep Synaptic aware of system activity.

Anatomical Label: Auditory Cortex (Runtime Observers)
"""

from __future__ import annotations
import os
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
from collections import deque

from ..base import Agent, AgentCategory, AgentState, AgentMessage


class EarsAgent(Agent):
    """
    Ears Agent - Runtime and console observation.

    Responsibilities:
    - Monitor console output and logs
    - Listen for webhook callbacks
    - Track error patterns
    - Capture runtime events
    """

    NAME = "ears"
    CATEGORY = AgentCategory.SENSORY
    DESCRIPTION = "Runtime and console event observation"
    ANATOMICAL_LABEL = "Auditory Cortex (Runtime Observers)"
    IS_VITAL = True  # Critical for webhook reception

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._event_buffer: deque = deque(maxlen=1000)  # Recent events
        self._error_patterns: Dict[str, int] = {}  # error -> count
        self._log_watchers: List[Path] = []

    def _on_start(self):
        """Start runtime observation."""
        # Add default log paths
        memory_dir = Path(__file__).parent.parent.parent
        log_paths = [
            memory_dir / ".synaptic_chat.log",
            memory_dir / ".ecosystem_health_stdout.log",
            memory_dir / ".webhook_quality.log"
        ]
        for log_path in log_paths:
            if log_path.exists():
                self._log_watchers.append(log_path)

    def _on_stop(self):
        """Stop runtime observation."""
        self._event_buffer.clear()
        self._log_watchers.clear()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check ears health."""
        return {
            "healthy": True,
            "score": 1.0,
            "message": f"Listening with {len(self._event_buffer)} events buffered",
            "metrics": {
                "event_buffer_size": len(self._event_buffer),
                "error_patterns": len(self._error_patterns),
                "log_watchers": len(self._log_watchers)
            }
        }

    def process(self, input_data: Any) -> Any:
        """Process listening requests."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "hear")
            if op == "hear":
                return self.hear_event(input_data)
            elif op == "get_recent":
                return self.get_recent_events(input_data.get("count", 50))
            elif op == "get_errors":
                return self.get_error_patterns()
        return None

    def hear_event(self, event: Dict[str, Any]) -> bool:
        """
        Receive and process an event.

        Events can be:
        - Webhook callbacks
        - Console output
        - Error reports
        - System notifications
        """
        try:
            # Add timestamp if not present
            if "timestamp" not in event:
                event["timestamp"] = datetime.utcnow().isoformat()

            # Classify event
            event_type = event.get("type", "unknown")

            # Track errors
            if event_type == "error" or "error" in str(event.get("content", "")).lower():
                error_key = event.get("error_type", event.get("content", "unknown")[:50])
                self._error_patterns[error_key] = self._error_patterns.get(error_key, 0) + 1

            # Buffer event
            self._event_buffer.append(event)
            self._last_active = datetime.utcnow()

            # Notify cerebellum of errors for learning
            if event_type == "error":
                self.send_message("cerebellum", "error_event", event)

            return True
        except Exception as e:
            self._handle_error(e)
            return False

    def hear_webhook(self, source: str, payload: Dict[str, Any]) -> bool:
        """Process incoming webhook callback."""
        return self.hear_event({
            "type": "webhook",
            "source": source,
            "content": payload
        })

    def hear_console(self, output: str, level: str = "info") -> bool:
        """Process console output."""
        return self.hear_event({
            "type": "console",
            "level": level,
            "content": output
        })

    def hear_error(self, error: str, context: Dict[str, Any] = None) -> bool:
        """Process an error event."""
        return self.hear_event({
            "type": "error",
            "content": error,
            "context": context or {}
        })

    def get_recent_events(self, count: int = 50) -> List[Dict[str, Any]]:
        """Get recent events from buffer."""
        return list(self._event_buffer)[-count:]

    def get_error_patterns(self) -> Dict[str, int]:
        """Get error pattern counts."""
        return dict(self._error_patterns)

    def get_events_by_type(self, event_type: str) -> List[Dict[str, Any]]:
        """Get events filtered by type."""
        return [e for e in self._event_buffer if e.get("type") == event_type]

    def clear_error_patterns(self):
        """Reset error pattern tracking."""
        self._error_patterns.clear()
