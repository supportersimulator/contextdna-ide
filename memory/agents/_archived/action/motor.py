"""
Motor Agent - Conscious Coding Agent

The Motor cortex executes actions - this is where thinking becomes
doing. It tracks coding actions and their outcomes.

Anatomical Label: Motor Cortex (Conscious Coding Agent)
"""

from __future__ import annotations
from datetime import datetime
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class MotorAgent(Agent):
    """
    Motor Agent - Code action execution tracking.

    Responsibilities:
    - Track coding actions
    - Monitor execution outcomes
    - Coordinate with cerebellum for learning
    - Provide action history
    """

    NAME = "motor"
    CATEGORY = AgentCategory.ACTION
    DESCRIPTION = "Code action execution and tracking"
    ANATOMICAL_LABEL = "Motor Cortex (Conscious Coding Agent)"
    IS_VITAL = False

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._action_history: List[Dict[str, Any]] = []
        self._current_action: Optional[Dict[str, Any]] = None
        self._action_stats: Dict[str, int] = {
            "total": 0,
            "success": 0,
            "failed": 0
        }

    def _on_start(self):
        """Initialize motor agent."""
        pass

    def _on_stop(self):
        """Shutdown motor agent."""
        pass

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check motor health."""
        success_rate = (
            self._action_stats["success"] / self._action_stats["total"]
            if self._action_stats["total"] > 0 else 1.0
        )
        return {
            "healthy": True,
            "score": 1.0,
            "message": f"Executed {self._action_stats['total']} actions ({success_rate:.1%} success)",
            "metrics": self._action_stats
        }

    def process(self, input_data: Any) -> Any:
        """Process motor operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "record")
            if op == "start":
                return self.start_action(input_data)
            elif op == "complete":
                return self.complete_action(input_data.get("action_id"), input_data.get("success", True))
            elif op == "record":
                return self.record_action(input_data)
            elif op == "history":
                return self.get_history(input_data.get("limit", 50))
        return None

    def start_action(self, action: Dict[str, Any]) -> str:
        """
        Start tracking an action.

        Returns action ID.
        """
        import hashlib

        action_id = f"act_{hashlib.sha256(str(datetime.utcnow()).encode()).hexdigest()[:12]}"

        self._current_action = {
            "id": action_id,
            "type": action.get("type", "unknown"),
            "description": action.get("description", ""),
            "file_path": action.get("file_path"),
            "started_at": datetime.utcnow().isoformat(),
            "completed_at": None,
            "success": None
        }

        self._action_stats["total"] += 1
        self._last_active = datetime.utcnow()

        return action_id

    def complete_action(self, action_id: str, success: bool = True) -> bool:
        """Complete a tracked action."""
        if self._current_action and self._current_action["id"] == action_id:
            self._current_action["completed_at"] = datetime.utcnow().isoformat()
            self._current_action["success"] = success

            if success:
                self._action_stats["success"] += 1
            else:
                self._action_stats["failed"] += 1

            # Add to history
            self._action_history.append(self._current_action)
            if len(self._action_history) > 500:
                self._action_history = self._action_history[-250:]

            # Notify cerebellum for learning
            self.send_message("cerebellum", "action_completed", self._current_action)

            self._current_action = None
            return True

        return False

    def record_action(self, action: Dict[str, Any]) -> str:
        """Record a completed action directly."""
        action_id = self.start_action(action)
        self.complete_action(action_id, action.get("success", True))
        return action_id

    def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get action history."""
        return self._action_history[-limit:]

    def get_current(self) -> Optional[Dict[str, Any]]:
        """Get currently active action."""
        return self._current_action

    def get_success_rate(self) -> float:
        """Get overall success rate."""
        if self._action_stats["total"] == 0:
            return 1.0
        return self._action_stats["success"] / self._action_stats["total"]

    def get_recent_failures(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent failed actions."""
        return [a for a in self._action_history if not a.get("success")][-limit:]
