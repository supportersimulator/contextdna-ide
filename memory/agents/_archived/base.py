"""
Base Agent Framework for Context DNA

All 20 agents inherit from this base class which provides:
- Lifecycle management (start, stop, health check)
- State persistence
- Inter-agent communication via message bus
- Celery task integration
- Health reporting for synaptic_anatomy
"""

from __future__ import annotations
import os
import sys
import json
import logging
import hashlib
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Callable, Type
from enum import Enum

# Add parent paths
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

logger = logging.getLogger(__name__)


class AgentState(str, Enum):
    """Agent lifecycle states."""
    INITIALIZING = "initializing"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"
    STOPPED = "stopped"


class AgentCategory(str, Enum):
    """Anatomical categories for agents."""
    BRAIN = "brain"
    SENSORY = "sensory"
    NERVOUS = "nervous"
    MEMORY = "memory"
    COGNITION = "cognition"
    CONTROL = "control"
    ACTION = "action"
    LEARNING = "learning"
    SAFETY = "safety"
    POLICY = "policy"
    SERVICES = "services"


@dataclass
class AgentHealth:
    """Health report for an agent."""
    agent_name: str
    state: AgentState
    healthy: bool
    score: float  # 0.0 to 1.0
    last_active: Optional[str] = None
    error_count: int = 0
    message: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['state'] = self.state.value
        return d


@dataclass
class AgentMessage:
    """Message for inter-agent communication."""
    sender: str
    recipient: str  # Agent name or "*" for broadcast
    message_type: str
    payload: Dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    correlation_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'AgentMessage':
        return cls(**data)


class Agent(ABC):
    """
    Base class for all Context DNA agents.

    Each agent is a semi-autonomous unit with:
    - Its own state and health monitoring
    - Ability to run periodic tasks
    - Communication with other agents
    - Integration with Celery for background work

    Lifecycle:
        init -> start -> run -> (pause/resume) -> stop
    """

    # Class-level metadata (override in subclasses)
    NAME: str = "base"
    CATEGORY: AgentCategory = AgentCategory.BRAIN
    DESCRIPTION: str = "Base agent class"
    ANATOMICAL_LABEL: str = "Unknown"
    IS_VITAL: bool = False  # If True, system degrades without this agent

    def __init__(self, config: Dict[str, Any] = None):
        """Initialize agent with optional configuration."""
        self.config = config or {}
        self._state = AgentState.INITIALIZING
        self._lock = threading.Lock()
        self._error_count = 0
        self._last_error: Optional[str] = None
        self._last_active: Optional[datetime] = None
        self._metrics: Dict[str, Any] = {}
        self._message_handlers: Dict[str, Callable] = {}

        # State persistence
        self._state_file = Path.home() / ".context-dna" / "agents" / f".{self.NAME}_state.json"
        self._state_file.parent.mkdir(parents=True, exist_ok=True)

        # Load persisted state
        self._load_state()

        # Register with global registry
        _registry.register(self)

        logger.info(f"Agent {self.NAME} initialized")

    # =========================================================================
    # LIFECYCLE METHODS
    # =========================================================================

    def start(self) -> bool:
        """Start the agent. Returns True if successful."""
        try:
            with self._lock:
                if self._state in [AgentState.RUNNING, AgentState.PAUSED]:
                    return True

                self._state = AgentState.RUNNING
                self._last_active = datetime.utcnow()

                # Call subclass startup
                self._on_start()

                self._save_state()
                logger.info(f"Agent {self.NAME} started")
                return True

        except Exception as e:
            self._handle_error(e)
            return False

    def stop(self) -> bool:
        """Stop the agent. Returns True if successful."""
        try:
            with self._lock:
                if self._state == AgentState.STOPPED:
                    return True

                # Call subclass cleanup
                self._on_stop()

                self._state = AgentState.STOPPED
                self._save_state()
                logger.info(f"Agent {self.NAME} stopped")
                return True

        except Exception as e:
            self._handle_error(e)
            return False

    def pause(self) -> bool:
        """Pause the agent temporarily."""
        with self._lock:
            if self._state == AgentState.RUNNING:
                self._state = AgentState.PAUSED
                self._save_state()
                return True
            return False

    def resume(self) -> bool:
        """Resume a paused agent."""
        with self._lock:
            if self._state == AgentState.PAUSED:
                self._state = AgentState.RUNNING
                self._last_active = datetime.utcnow()
                self._save_state()
                return True
            return False

    # =========================================================================
    # HEALTH & MONITORING
    # =========================================================================

    def health_check(self) -> AgentHealth:
        """
        Return current health status.

        Subclasses should override _check_health() for custom checks.
        """
        try:
            # Base health score
            score = 1.0
            message = "OK"
            healthy = True

            # Deduct for errors
            if self._error_count > 0:
                score -= min(0.3, self._error_count * 0.05)

            # Deduct for bad state
            if self._state == AgentState.ERROR:
                score -= 0.5
                healthy = False
                message = self._last_error or "Unknown error"
            elif self._state == AgentState.STOPPED:
                score -= 0.2
                message = "Agent stopped"
            elif self._state == AgentState.PAUSED:
                score -= 0.1
                message = "Agent paused"

            # Deduct for inactivity (>5 minutes)
            if self._last_active:
                inactive_time = datetime.utcnow() - self._last_active
                if inactive_time > timedelta(minutes=5):
                    score -= 0.1

            # Run custom health check
            custom_health = self._check_health()
            if custom_health:
                score = min(score, custom_health.get('score', score))
                if not custom_health.get('healthy', True):
                    healthy = False
                    message = custom_health.get('message', message)
                self._metrics.update(custom_health.get('metrics', {}))

            return AgentHealth(
                agent_name=self.NAME,
                state=self._state,
                healthy=healthy,
                score=max(0.0, min(1.0, score)),
                last_active=self._last_active.isoformat() if self._last_active else None,
                error_count=self._error_count,
                message=message,
                metrics=self._metrics
            )

        except Exception as e:
            return AgentHealth(
                agent_name=self.NAME,
                state=AgentState.ERROR,
                healthy=False,
                score=0.0,
                error_count=self._error_count,
                message=f"Health check failed: {str(e)[:100]}"
            )

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """
        Override in subclass for custom health checks.

        Returns dict with: score (0-1), healthy (bool), message (str), metrics (dict)
        """
        return None

    # =========================================================================
    # MESSAGING
    # =========================================================================

    def send_message(self, recipient: str, message_type: str, payload: Dict[str, Any]) -> bool:
        """Send a message to another agent."""
        try:
            msg = AgentMessage(
                sender=self.NAME,
                recipient=recipient,
                message_type=message_type,
                payload=payload
            )

            # Use Redis pub/sub if available
            try:
                from memory.redis_cache import publish_event
                publish_event(f"agent:{recipient}", msg.to_dict())
                return True
            except ImportError as e:
                print(f"[WARN] Redis pub/sub not available for message to {recipient}: {e}")

            # Fallback: direct delivery via registry
            if recipient == "*":
                for agent in _registry.get_all():
                    if agent.NAME != self.NAME:
                        agent.receive_message(msg)
            else:
                target = _registry.get(recipient)
                if target:
                    target.receive_message(msg)
                    return True

            return False

        except Exception as e:
            logger.warning(f"Failed to send message to {recipient}: {e}")
            return False

    def receive_message(self, message: AgentMessage):
        """Handle incoming message."""
        try:
            handler = self._message_handlers.get(message.message_type)
            if handler:
                handler(message)
            else:
                self._on_message(message)
        except Exception as e:
            logger.warning(f"Error handling message: {e}")

    def register_handler(self, message_type: str, handler: Callable[[AgentMessage], None]):
        """Register a handler for a specific message type."""
        self._message_handlers[message_type] = handler

    def _on_message(self, message: AgentMessage):
        """Override to handle messages without explicit handler."""
        pass

    # =========================================================================
    # TASK EXECUTION
    # =========================================================================

    def run_task(self, task_name: str, **kwargs) -> Any:
        """
        Run a task (either sync or via Celery).

        If Celery is available and task is defined, queues it.
        Otherwise runs synchronously.
        """
        self._last_active = datetime.utcnow()

        # Get task method
        method = getattr(self, f"task_{task_name}", None)
        if not method:
            raise ValueError(f"Unknown task: {task_name}")

        # Try Celery first
        try:
            from memory.celery_tasks import is_celery_available
            if is_celery_available():
                # Look for Celery-wrapped version
                celery_task = getattr(self, f"_celery_{task_name}", None)
                if celery_task:
                    return celery_task.delay(**kwargs)
        except ImportError as e:
            print(f"[WARN] Celery not available for task {task_name}: {e}")

        # Run synchronously
        return method(**kwargs)

    # =========================================================================
    # STATE PERSISTENCE
    # =========================================================================

    def _save_state(self):
        """Persist agent state to disk."""
        try:
            state = {
                'state': self._state.value,
                'error_count': self._error_count,
                'last_error': self._last_error,
                'last_active': self._last_active.isoformat() if self._last_active else None,
                'metrics': self._metrics,
                'config': self.config,
                'custom': self._get_custom_state()
            }
            self._state_file.write_text(json.dumps(state, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save state for {self.NAME}: {e}")

    def _load_state(self):
        """Load persisted state from disk."""
        try:
            if self._state_file.exists():
                state = json.loads(self._state_file.read_text())
                self._state = AgentState(state.get('state', 'initializing'))
                self._error_count = state.get('error_count', 0)
                self._last_error = state.get('last_error')
                if state.get('last_active'):
                    self._last_active = datetime.fromisoformat(state['last_active'])
                self._metrics = state.get('metrics', {})
                self.config.update(state.get('config', {}))
                self._set_custom_state(state.get('custom', {}))
        except Exception as e:
            logger.warning(f"Failed to load state for {self.NAME}: {e}")

    def _get_custom_state(self) -> Dict[str, Any]:
        """Override to add custom state for persistence."""
        return {}

    def _set_custom_state(self, state: Dict[str, Any]):
        """Override to restore custom state."""
        pass

    # =========================================================================
    # ERROR HANDLING
    # =========================================================================

    def _handle_error(self, error: Exception):
        """Handle and record an error."""
        self._error_count += 1
        self._last_error = str(error)[:200]
        self._state = AgentState.ERROR
        logger.error(f"Agent {self.NAME} error: {error}")
        self._save_state()

    def reset_errors(self):
        """Reset error state."""
        self._error_count = 0
        self._last_error = None
        if self._state == AgentState.ERROR:
            self._state = AgentState.READY
        self._save_state()

    # =========================================================================
    # ABSTRACT METHODS (Implement in subclasses)
    # =========================================================================

    @abstractmethod
    def _on_start(self):
        """Called when agent starts. Initialize resources."""
        pass

    @abstractmethod
    def _on_stop(self):
        """Called when agent stops. Clean up resources."""
        pass

    @abstractmethod
    def process(self, input_data: Any) -> Any:
        """
        Main processing method for the agent.

        What this does depends on agent type:
        - Sensory agents: Process raw input
        - Memory agents: Store/retrieve data
        - Control agents: Filter/route data
        - etc.
        """
        pass


class AgentRegistry:
    """
    Global registry of all agents.

    Provides:
    - Agent lookup by name
    - Category-based filtering
    - Health aggregation
    - Lifecycle management for all agents
    """

    def __init__(self):
        self._agents: Dict[str, Agent] = {}
        self._lock = threading.Lock()

    def register(self, agent: Agent):
        """Register an agent."""
        with self._lock:
            self._agents[agent.NAME] = agent

    def unregister(self, name: str):
        """Unregister an agent."""
        with self._lock:
            self._agents.pop(name, None)

    def get(self, name: str) -> Optional[Agent]:
        """Get agent by name."""
        return self._agents.get(name)

    def get_all(self) -> List[Agent]:
        """Get all registered agents."""
        return list(self._agents.values())

    def get_by_category(self, category: AgentCategory) -> List[Agent]:
        """Get agents by category."""
        return [a for a in self._agents.values() if a.CATEGORY == category]

    def get_vital_agents(self) -> List[Agent]:
        """Get all vital agents."""
        return [a for a in self._agents.values() if a.IS_VITAL]

    def start_all(self) -> Dict[str, bool]:
        """Start all agents."""
        results = {}
        for name, agent in self._agents.items():
            results[name] = agent.start()
        return results

    def stop_all(self) -> Dict[str, bool]:
        """Stop all agents."""
        results = {}
        for name, agent in self._agents.items():
            results[name] = agent.stop()
        return results

    def health_report(self) -> Dict[str, Any]:
        """Get aggregated health report."""
        reports = {}
        total_score = 0.0
        vital_healthy = True

        for name, agent in self._agents.items():
            health = agent.health_check()
            reports[name] = health.to_dict()
            total_score += health.score
            if agent.IS_VITAL and not health.healthy:
                vital_healthy = False

        return {
            'timestamp': datetime.utcnow().isoformat(),
            'agent_count': len(self._agents),
            'average_score': total_score / len(self._agents) if self._agents else 0.0,
            'vital_systems_healthy': vital_healthy,
            'agents': reports
        }


# Global registry instance
_registry = AgentRegistry()


def get_registry() -> AgentRegistry:
    """Get the global agent registry."""
    return _registry
