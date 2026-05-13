"""
Observer Agent - Event Observation Service

The Observer monitors system events - aggregating signals from
multiple sources and providing a unified event stream.

Anatomical Label: Event Observation Service
"""

from __future__ import annotations
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Callable
from collections import deque
from threading import Lock

from ..base import Agent, AgentCategory, AgentState


class ObserverAgent(Agent):
    """
    Observer Agent - Event aggregation and observation.

    Responsibilities:
    - Aggregate events from multiple sources
    - Provide unified event stream
    - Support event filtering
    - Enable event replay
    """

    NAME = "observer"
    CATEGORY = AgentCategory.SERVICES
    DESCRIPTION = "Event aggregation and unified observation"
    ANATOMICAL_LABEL = "Event Observation Service"
    IS_VITAL = False

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._event_buffer: deque = deque(maxlen=5000)
        self._subscribers: Dict[str, List[Callable]] = {}
        self._event_counts: Dict[str, int] = {}
        self._lock = Lock()

    def _on_start(self):
        """Initialize observer."""
        pass

    def _on_stop(self):
        """Shutdown observer."""
        self._event_buffer.clear()
        self._subscribers.clear()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check observer health."""
        return {
            "healthy": True,
            "score": 1.0,
            "message": f"Observing {len(self._event_buffer)} events, {len(self._subscribers)} subscribers",
            "metrics": {
                "buffer_size": len(self._event_buffer),
                "event_types": len(self._event_counts),
                "subscribers": len(self._subscribers)
            }
        }

    def process(self, input_data: Any) -> Any:
        """Process observer operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "observe")
            if op == "observe":
                return self.observe(input_data.get("event"))
            elif op == "get_events":
                return self.get_events(
                    input_data.get("event_type"),
                    input_data.get("limit", 100),
                    input_data.get("since")
                )
            elif op == "subscribe":
                return {"status": "use_subscribe_method"}
            elif op == "stats":
                return self.get_stats()
        return None

    def observe(self, event: Dict[str, Any]) -> bool:
        """
        Observe an event.

        Events are buffered and broadcast to subscribers.
        """
        if not event:
            return False

        with self._lock:
            # Add metadata
            event["_observed_at"] = datetime.utcnow().isoformat()
            event["_sequence"] = len(self._event_buffer)

            # Buffer event
            self._event_buffer.append(event)

            # Track counts
            event_type = event.get("type", "unknown")
            self._event_counts[event_type] = self._event_counts.get(event_type, 0) + 1

        # Notify subscribers
        event_type = event.get("type", "unknown")
        for callback in self._subscribers.get(event_type, []):
            try:
                callback(event)
            except Exception as e:
                print(f"[WARN] Observer subscriber callback failed for {event_type}: {e}")

        # Also notify "all" subscribers
        for callback in self._subscribers.get("*", []):
            try:
                callback(event)
            except Exception as e:
                print(f"[WARN] Observer wildcard subscriber callback failed: {e}")

        self._last_active = datetime.utcnow()
        return True

    def get_events(
        self,
        event_type: str = None,
        limit: int = 100,
        since: str = None
    ) -> List[Dict[str, Any]]:
        """
        Get events from buffer.

        Args:
            event_type: Filter by event type (optional)
            limit: Maximum events to return
            since: Only events after this ISO timestamp
        """
        events = list(self._event_buffer)

        # Filter by type
        if event_type:
            events = [e for e in events if e.get("type") == event_type]

        # Filter by time
        if since:
            since_dt = datetime.fromisoformat(since)
            events = [
                e for e in events
                if datetime.fromisoformat(e.get("_observed_at", "2000-01-01")) > since_dt
            ]

        # Return most recent
        return events[-limit:]

    def subscribe(self, event_type: str, callback: Callable) -> bool:
        """Subscribe to events of a specific type (or "*" for all)."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
        return True

    def unsubscribe(self, event_type: str, callback: Callable) -> bool:
        """Unsubscribe from events."""
        if event_type in self._subscribers and callback in self._subscribers[event_type]:
            self._subscribers[event_type].remove(callback)
            return True
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Get observation statistics."""
        return {
            "total_events": len(self._event_buffer),
            "event_counts": self._event_counts,
            "subscriber_count": sum(len(subs) for subs in self._subscribers.values()),
            "buffer_capacity": self._event_buffer.maxlen
        }

    def get_recent(self, minutes: int = 5) -> List[Dict[str, Any]]:
        """Get events from the last N minutes."""
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        return [
            e for e in self._event_buffer
            if datetime.fromisoformat(e.get("_observed_at", "2000-01-01")) > cutoff
        ]

    def get_by_source(self, source: str) -> List[Dict[str, Any]]:
        """Get events from a specific source."""
        return [e for e in self._event_buffer if e.get("source") == source]

    def clear_buffer(self):
        """Clear the event buffer (use with caution)."""
        self._event_buffer.clear()
        self._event_counts.clear()
