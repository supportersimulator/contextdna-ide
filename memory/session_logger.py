#!/usr/bin/env python3
"""
Claude Code Session Logger for Acontext

This module provides automatic session logging for Claude Code / Cursor interactions.
It captures the conversation trace and logs it to Context DNA for learning.

The key insight from GPT-5: "Context DNA can observe everything passed through the agent
interface... It's basically reading the full trace of your session."

Usage:
    # At session start (in CLAUDE.md or agent startup)
    from memory.session_logger import SessionLogger
    logger = SessionLogger.start("atlas", "Implementing GPU toggle health check")

    # Log key steps as work progresses
    logger.step("Read gpu_toggle.py")
    logger.step("Found missing ECS health", result="Need to add ecs.describe_tasks")
    logger.step("Added get_ecs_containers_health()")

    # At session end - THIS IS CRITICAL for SOP extraction
    logger.complete(
        success=True,
        summary="Added ECS container health checking to Lambda",
        learnings=["ECS health is more reliable than curl", "Container states: HEALTHY, UNHEALTHY, UNKNOWN"]
    )

For Claude Code automatic integration:
    # Start of session
    python -c "from memory.session_logger import start_session; start_session('atlas', 'task description')"

    # End of session (success)
    python -c "from memory.session_logger import complete_session; complete_session(True, 'summary', ['learning1', 'learning2'])"
"""

import sys
import os
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False

# Session state file for persistence across CLI calls
SESSION_STATE_FILE = Path(__file__).parent / ".current_session.json"


class SessionLogger:
    """
    Logs Claude Code / Cursor sessions to Context DNA for learning.

    This captures the full trace of a work session:
    - What task was attempted
    - What steps were taken
    - What was the outcome
    - What were the key learnings

    Only SUCCESSFUL completions trigger SOP extraction in Context DNA.
    """

    def __init__(self, session_id: str, agent_name: str, task: str, memory: ContextDNAClient):
        self.session_id = session_id
        self.agent_name = agent_name
        self.task = task
        self.memory = memory
        self.steps = []
        self.started_at = datetime.now().isoformat()

    @classmethod
    def start(cls, agent_name: str, task: str) -> "SessionLogger":
        """
        Start a new logging session.

        Args:
            agent_name: Which agent is working (atlas, hermes, navigator, etc.)
            task: What the agent is trying to accomplish

        Returns:
            SessionLogger instance for logging steps
        """
        if not CONTEXT_DNA_AVAILABLE:
            print("Warning: Context DNA not available, session will not be logged")
            return None

        try:
            memory = ContextDNAClient()
            session_id = memory.start_agent_session(task, agent_name)

            logger = cls(session_id, agent_name, task, memory)

            # Save state for CLI persistence
            logger._save_state()

            print(f"📝 Session started: {session_id}")
            print(f"   Agent: {agent_name}")
            print(f"   Task: {task}")

            return logger

        except Exception as e:
            print(f"Warning: Failed to start session: {e}")
            return None

    def step(self, description: str, result: str = None):
        """
        Log a step in the session.

        Args:
            description: What was done
            result: Optional result or observation
        """
        step_data = {
            "description": description,
            "result": result,
            "timestamp": datetime.now().isoformat()
        }
        self.steps.append(step_data)

        # Log to Context DNA
        self.memory.log_agent_step(self.session_id, description, result)

        # Update saved state
        self._save_state()

        print(f"   → {description}")
        if result:
            print(f"     Result: {result}")

    def complete(self, success: bool, summary: str, learnings: list[str] = None):
        """
        Complete the session and trigger SOP extraction.

        CRITICAL: This is what makes Acontext learn from the session.
        Only successful completions generate SOPs.

        Args:
            success: Whether the task was successful
            summary: Summary of what was accomplished
            learnings: Key learnings to highlight for future sessions
        """
        self.memory.complete_task(
            self.session_id,
            success=success,
            summary=summary,
            learnings=learnings
        )

        # Clean up state file
        self._clear_state()

        if success:
            print(f"✅ Session completed successfully")
            print(f"   Summary: {summary}")
            if learnings:
                print(f"   Learnings recorded: {len(learnings)}")
            print(f"   SOP extraction: TRIGGERED")
        else:
            print(f"❌ Session failed")
            print(f"   Summary: {summary}")
            print(f"   SOP extraction: SKIPPED (only successful sessions generate SOPs)")

    def _save_state(self):
        """Save session state for CLI persistence."""
        state = {
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "task": self.task,
            "started_at": self.started_at,
            "steps": self.steps
        }
        with open(SESSION_STATE_FILE, "w") as f:
            json.dump(state, f)

    def _clear_state(self):
        """Clear session state file."""
        if SESSION_STATE_FILE.exists():
            SESSION_STATE_FILE.unlink()

    @classmethod
    def resume(cls) -> Optional["SessionLogger"]:
        """
        Resume a session from saved state.

        Returns:
            SessionLogger if a session exists, None otherwise
        """
        if not SESSION_STATE_FILE.exists():
            return None

        try:
            with open(SESSION_STATE_FILE) as f:
                state = json.load(f)

            memory = ContextDNAClient()
            logger = cls(
                state["session_id"],
                state["agent_name"],
                state["task"],
                memory
            )
            logger.started_at = state["started_at"]
            logger.steps = state["steps"]

            print(f"📝 Session resumed: {logger.session_id}")
            print(f"   Steps so far: {len(logger.steps)}")

            return logger

        except Exception as e:
            print(f"Warning: Failed to resume session: {e}")
            return None


# Convenience functions for CLI usage

def start_session(agent_name: str, task: str):
    """Start a new session from CLI."""
    SessionLogger.start(agent_name, task)


def log_step(description: str, result: str = None):
    """Log a step from CLI."""
    logger = SessionLogger.resume()
    if logger:
        logger.step(description, result)
    else:
        print("No active session. Start one with: start_session('atlas', 'task')")


def complete_session(success: bool, summary: str, learnings: list = None):
    """Complete session from CLI."""
    logger = SessionLogger.resume()
    if logger:
        logger.complete(success, summary, learnings or [])
    else:
        print("No active session to complete.")


def get_session_status():
    """Get current session status."""
    if not SESSION_STATE_FILE.exists():
        print("No active session")
        return None

    with open(SESSION_STATE_FILE) as f:
        state = json.load(f)

    print(f"Active session: {state['session_id']}")
    print(f"Agent: {state['agent_name']}")
    print(f"Task: {state['task']}")
    print(f"Started: {state['started_at']}")
    print(f"Steps: {len(state['steps'])}")

    return state


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Session Logger CLI")
        print("")
        print("Commands:")
        print("  start <agent> <task>     - Start a new session")
        print("  step <description>       - Log a step")
        print("  complete <success> <summary> [learnings...]  - Complete session")
        print("  status                   - Show current session")
        print("")
        print("Examples:")
        print("  python session_logger.py start atlas 'Fix GPU toggle health'")
        print("  python session_logger.py step 'Read gpu_toggle.py'")
        print("  python session_logger.py complete true 'Added ECS health' 'learning1' 'learning2'")
        print("  python session_logger.py status")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "start":
        if len(sys.argv) < 4:
            print("Usage: start <agent> <task>")
            sys.exit(1)
        start_session(sys.argv[2], " ".join(sys.argv[3:]))

    elif cmd == "step":
        if len(sys.argv) < 3:
            print("Usage: step <description>")
            sys.exit(1)
        log_step(" ".join(sys.argv[2:]))

    elif cmd == "complete":
        if len(sys.argv) < 4:
            print("Usage: complete <true/false> <summary> [learnings...]")
            sys.exit(1)
        success = sys.argv[2].lower() in ("true", "1", "yes")
        summary = sys.argv[3]
        learnings = sys.argv[4:] if len(sys.argv) > 4 else []
        complete_session(success, summary, learnings)

    elif cmd == "status":
        get_session_status()

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
