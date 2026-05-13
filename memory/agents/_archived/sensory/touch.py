"""
Touch Agent - Somatosensory Cortex / Interaction Sensors

The Touch agent detects user interactions - keystrokes patterns,
prompt submissions, IDE context switches, and engagement signals.

Anatomical Label: Somatosensory Cortex (Interaction Sensors)
"""

from __future__ import annotations
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from collections import deque

from ..base import Agent, AgentCategory, AgentState


class TouchAgent(Agent):
    """
    Touch Agent - User interaction sensing.

    Responsibilities:
    - Detect prompt submissions
    - Track IDE context switches
    - Monitor engagement patterns
    - Sense user activity levels
    """

    NAME = "touch"
    CATEGORY = AgentCategory.SENSORY
    DESCRIPTION = "User interaction and engagement sensing"
    ANATOMICAL_LABEL = "Somatosensory Cortex (Interaction Sensors)"
    IS_VITAL = False

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._interaction_buffer: deque = deque(maxlen=500)
        self._last_ide: Optional[str] = None
        self._session_start: Optional[datetime] = None
        self._interaction_count: int = 0

    def _on_start(self):
        """Start interaction sensing."""
        self._session_start = datetime.utcnow()

    def _on_stop(self):
        """Stop interaction sensing."""
        self._interaction_buffer.clear()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check touch health."""
        return {
            "healthy": True,
            "score": 1.0,
            "message": f"Sensing with {self._interaction_count} interactions",
            "metrics": {
                "interaction_count": self._interaction_count,
                "buffer_size": len(self._interaction_buffer),
                "current_ide": self._last_ide
            }
        }

    def process(self, input_data: Any) -> Any:
        """Process touch events."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "sense")
            if op == "sense":
                return self.sense_interaction(input_data)
            elif op == "get_activity":
                return self.get_activity_level()
            elif op == "get_session":
                return self.get_session_info()
        return None

    def sense_interaction(self, interaction: Dict[str, Any]) -> bool:
        """
        Sense a user interaction.

        Interactions include:
        - Prompt submissions
        - File opens/edits
        - IDE switches
        - Command executions
        """
        try:
            interaction["timestamp"] = datetime.utcnow().isoformat()

            # Track IDE switches
            ide = interaction.get("ide") or self._detect_ide()
            if ide and ide != self._last_ide:
                interaction["ide_switch"] = True
                interaction["from_ide"] = self._last_ide
                self._last_ide = ide

            # Buffer interaction
            self._interaction_buffer.append(interaction)
            self._interaction_count += 1
            self._last_active = datetime.utcnow()

            return True
        except Exception as e:
            self._handle_error(e)
            return False

    def sense_prompt(self, prompt: str, ide: str = None) -> bool:
        """Sense a prompt submission."""
        return self.sense_interaction({
            "type": "prompt",
            "content_length": len(prompt),
            "ide": ide or self._detect_ide(),
            "has_question": "?" in prompt,
            "has_code": "```" in prompt or "def " in prompt or "function " in prompt
        })

    def sense_file_open(self, file_path: str) -> bool:
        """Sense a file being opened."""
        return self.sense_interaction({
            "type": "file_open",
            "file_path": file_path,
            "extension": file_path.split(".")[-1] if "." in file_path else None
        })

    def _detect_ide(self) -> Optional[str]:
        """Detect current IDE from environment."""
        if os.environ.get("CURSOR_FILE_PATH"):
            return "cursor"
        elif os.environ.get("VSCODE_FILE_PATH"):
            return "vscode"
        elif os.environ.get("WINDSURF_FILE_PATH"):
            return "windsurf"
        elif os.environ.get("PYCHARM_FILE_PATH"):
            return "pycharm"
        return None

    def get_activity_level(self) -> Dict[str, Any]:
        """Get current activity level metrics."""
        now = datetime.utcnow()

        # Count recent interactions
        recent_cutoff = now - timedelta(minutes=5)
        recent_count = sum(
            1 for i in self._interaction_buffer
            if datetime.fromisoformat(i["timestamp"]) > recent_cutoff
        )

        # Determine activity level
        if recent_count > 20:
            level = "high"
        elif recent_count > 5:
            level = "moderate"
        elif recent_count > 0:
            level = "low"
        else:
            level = "idle"

        return {
            "level": level,
            "recent_5min": recent_count,
            "total_session": self._interaction_count,
            "current_ide": self._last_ide
        }

    def get_session_info(self) -> Dict[str, Any]:
        """Get current session information."""
        now = datetime.utcnow()
        duration = (now - self._session_start) if self._session_start else timedelta()

        return {
            "started_at": self._session_start.isoformat() if self._session_start else None,
            "duration_minutes": duration.total_seconds() / 60,
            "interaction_count": self._interaction_count,
            "current_ide": self._last_ide,
            "activity": self.get_activity_level()
        }
