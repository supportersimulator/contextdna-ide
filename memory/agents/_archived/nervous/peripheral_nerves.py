"""
Peripheral Nerves Agent - Signal Emitters

The Peripheral Nerves send signals throughout the system - broadcasting
events, notifications, and updates to all listening components.

Anatomical Label: Peripheral Nerves (Signal Emitters)
"""

from __future__ import annotations
import json
from datetime import datetime
from typing import Dict, Any, Optional, List, Callable
from collections import defaultdict

from ..base import Agent, AgentCategory, AgentState, AgentMessage


class PeripheralNervesAgent(Agent):
    """
    Peripheral Nerves Agent - Event broadcasting and signaling.

    Responsibilities:
    - Broadcast events to subscribers
    - Emit notifications
    - Signal state changes
    - Coordinate inter-agent communication
    """

    NAME = "peripheral_nerves"
    CATEGORY = AgentCategory.NERVOUS
    DESCRIPTION = "Event broadcasting and signal emission"
    ANATOMICAL_LABEL = "Peripheral Nerves (Signal Emitters)"
    IS_VITAL = False

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._event_history: List[Dict[str, Any]] = []
        self._redis_available: bool = False

    def _on_start(self):
        """Initialize signal system."""
        self._check_redis()

    def _on_stop(self):
        """Shutdown signal system."""
        self._subscribers.clear()

    def _check_redis(self) -> bool:
        """Check if Redis pub/sub is available."""
        try:
            from memory.redis_cache import get_redis_client
            client = get_redis_client()
            if client:
                client.ping()
                self._redis_available = True
                return True
        except Exception as e:
            print(f"[WARN] Redis availability check failed: {e}")
        self._redis_available = False
        return False

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check peripheral nerves health."""
        return {
            "healthy": True,
            "score": 1.0 if self._redis_available else 0.8,
            "message": "Redis pub/sub available" if self._redis_available else "Local signaling only",
            "metrics": {
                "redis_available": self._redis_available,
                "subscriber_count": sum(len(v) for v in self._subscribers.values()),
                "event_channels": len(self._subscribers)
            }
        }

    def process(self, input_data: Any) -> Any:
        """Process signal operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "emit")
            if op == "emit":
                return self.emit(
                    input_data.get("channel"),
                    input_data.get("data")
                )
            elif op == "subscribe":
                return {"status": "use_subscribe_method"}
            elif op == "get_history":
                return self.get_event_history(input_data.get("limit", 100))
        return None

    def emit(self, channel: str, data: Any) -> bool:
        """
        Emit a signal on a channel.

        Args:
            channel: The event channel (e.g., "file_changes", "error", "success")
            data: The event data to broadcast
        """
        try:
            event = {
                "channel": channel,
                "data": data,
                "timestamp": datetime.utcnow().isoformat()
            }

            # Record in history
            self._event_history.append(event)
            if len(self._event_history) > 1000:
                self._event_history = self._event_history[-500:]

            # Local subscribers
            for callback in self._subscribers.get(channel, []):
                try:
                    callback(data)
                except Exception as e:
                    print(f"[WARN] Subscriber callback failed on channel {channel}: {e}")

            # Redis pub/sub
            if self._redis_available:
                try:
                    from memory.redis_cache import publish_event
                    publish_event(channel, data)
                except Exception as e:
                    print(f"[WARN] Redis publish failed on channel {channel}: {e}")

            self._last_active = datetime.utcnow()
            return True

        except Exception as e:
            self._handle_error(e)
            return False

    def subscribe(self, channel: str, callback: Callable) -> bool:
        """Subscribe to a channel with a callback."""
        self._subscribers[channel].append(callback)
        return True

    def unsubscribe(self, channel: str, callback: Callable) -> bool:
        """Unsubscribe from a channel."""
        if callback in self._subscribers[channel]:
            self._subscribers[channel].remove(callback)
            return True
        return False

    def get_event_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent event history."""
        return self._event_history[-limit:]

    def emit_file_change(self, file_path: str, change_type: str) -> bool:
        """Emit a file change event."""
        return self.emit("file_changes", {
            "file_path": file_path,
            "change_type": change_type
        })

    def emit_error(self, error: str, context: Dict = None) -> bool:
        """Emit an error event."""
        return self.emit("error", {
            "error": error,
            "context": context or {}
        })

    def emit_success(self, task: str, details: str = None) -> bool:
        """Emit a success event."""
        return self.emit("success", {
            "task": task,
            "details": details
        })

    def emit_health_update(self, component: str, status: str) -> bool:
        """Emit a health status update."""
        return self.emit("health_update", {
            "component": component,
            "status": status
        })
