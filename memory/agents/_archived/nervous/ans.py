"""
ANS Agent - Autonomic Nervous System / Celery Task Fabric

The ANS manages the background task infrastructure - scheduling,
routing, and coordinating the subconscious agents.

Anatomical Label: Autonomic Nervous System (Celery Task Fabric)
"""

from __future__ import annotations
from datetime import datetime
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class ANSAgent(Agent):
    """
    ANS Agent - Celery task fabric management.

    Responsibilities:
    - Monitor Celery worker health
    - Manage task queues
    - Coordinate background agents
    - Handle task scheduling
    """

    NAME = "ans"
    CATEGORY = AgentCategory.NERVOUS
    DESCRIPTION = "Autonomic task scheduling and coordination"
    ANATOMICAL_LABEL = "Autonomic Nervous System (Celery Task Fabric)"
    IS_VITAL = False  # System can work without Celery

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._celery_available: bool = False
        self._active_tasks: Dict[str, Any] = {}
        self._queue_stats: Dict[str, int] = {}

    def _on_start(self):
        """Initialize ANS and check Celery availability."""
        self._check_celery()

    def _on_stop(self):
        """Shutdown ANS."""
        pass

    def _check_celery(self) -> bool:
        """Check if Celery is available."""
        try:
            from memory.celery_tasks import is_celery_available
            self._celery_available = is_celery_available(force_check=True)
            return self._celery_available
        except ImportError:
            self._celery_available = False
            return False

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check ANS health."""
        celery_ok = self._check_celery()
        return {
            "healthy": True,  # ANS is always healthy, Celery is optional
            "score": 1.0 if celery_ok else 0.7,
            "message": "Celery available" if celery_ok else "Celery unavailable (using sync mode)",
            "metrics": {
                "celery_available": celery_ok,
                "active_tasks": len(self._active_tasks)
            }
        }

    def process(self, input_data: Any) -> Any:
        """Process ANS operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "status")
            if op == "status":
                return self.get_status()
            elif op == "queue_task":
                return self.queue_task(
                    input_data.get("task_name"),
                    input_data.get("args", []),
                    input_data.get("kwargs", {})
                )
            elif op == "get_queue_stats":
                return self.get_queue_stats()
        return self.get_status()

    def queue_task(self, task_name: str, args: List = None, kwargs: Dict = None) -> Dict[str, Any]:
        """Queue a Celery task."""
        if not self._celery_available:
            return {"status": "unavailable", "message": "Celery not available"}

        try:
            from memory.celery_tasks import fire_and_forget
            import importlib

            # Dynamic task import
            module = importlib.import_module("memory.celery_tasks")
            task = getattr(module, task_name, None)

            if not task:
                return {"status": "error", "message": f"Task {task_name} not found"}

            success = fire_and_forget(task, *(args or []), **(kwargs or {}))

            if success:
                self._active_tasks[task_name] = datetime.utcnow().isoformat()
                return {"status": "queued", "task": task_name}
            else:
                return {"status": "failed", "message": "Failed to queue task"}

        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_status(self) -> Dict[str, Any]:
        """Get ANS status."""
        return {
            "celery_available": self._celery_available,
            "active_tasks": list(self._active_tasks.keys()),
            "queue_stats": self._queue_stats
        }

    def get_queue_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        if not self._celery_available:
            return {"status": "unavailable"}

        try:
            from memory.celery_config import app
            inspect = app.control.inspect()

            active = inspect.active() or {}
            reserved = inspect.reserved() or {}
            scheduled = inspect.scheduled() or {}

            stats = {
                "active": sum(len(v) for v in active.values()),
                "reserved": sum(len(v) for v in reserved.values()),
                "scheduled": sum(len(v) for v in scheduled.values())
            }
            self._queue_stats = stats
            return stats

        except Exception as e:
            return {"status": "error", "error": str(e)}

    def trigger_scan(self) -> bool:
        """Trigger a project scan."""
        result = self.queue_task("scan_project")
        return result.get("status") == "queued"

    def trigger_brain_cycle(self) -> bool:
        """Trigger a brain consolidation cycle."""
        result = self.queue_task("brain_cycle")
        return result.get("status") == "queued"
