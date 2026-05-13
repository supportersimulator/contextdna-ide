#!/usr/bin/env python3
"""
Autonomous Architecture Discovery via Context DNA

This module provides FULLY AUTOMATIC architecture learning using the same
Context DNA SOP extraction flow that powers the learning system.

THE INSIGHT:
Architecture IS learning. When an agent successfully deploys Django,
that's a learned procedure. When an agent successfully configures LiveKit,
that's a learned procedure. We don't need a separate system - we need to
feed architecture tasks into the SAME Context DNA learning pipeline.

HOW IT WORKS:
1. Agent starts infrastructure task → start_architecture_task()
2. Agent works (SSH, AWS CLI, Docker, etc.)
3. Agent completes successfully → complete_architecture_task()
4. Context DNA extracts SOP from the successful task
5. Future agents query "django deployment" → get the learned procedure

This is IDENTICAL to how auto_learn.py and session_logger.py work,
just applied to infrastructure tasks.

INTEGRATION:
    from memory.auto_architecture import arch_task

    # Start working on infrastructure
    with arch_task("Deploy Django update to production"):
        # Do the work
        ssh_result = subprocess.run(["ssh", ...])
        restart_result = subprocess.run(["systemctl", ...])
        # Task auto-completes on context exit if no exception

    # Or explicit start/complete
    task_id = start_architecture_task("Configure LiveKit TURN server")
    # ... do work ...
    complete_architecture_task(task_id, success=True,
        summary="Configured coturn on port 3478",
        details="Used /etc/turnserver.conf with realm=ersimulator.com"
    )
"""

import sys
import os
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from typing import Optional
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False

# Architecture-specific tags that help Context DNA categorize
ARCHITECTURE_TAGS = [
    "architecture", "infrastructure", "deployment", "configuration",
    "aws", "docker", "terraform", "kubernetes", "systemd",
    "database", "networking", "security", "monitoring"
]

# State file for tracking active architecture tasks
STATE_FILE = Path(__file__).parent / ".arch_tasks.json"


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to load auto_architecture state: {e}")
    return {"active_tasks": {}}


def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


class ArchitectureTaskTracker:
    """
    Tracks architecture tasks and feeds them into Context DNA's learning system.

    This is essentially a specialized session logger for infrastructure work.
    """

    def __init__(self):
        if not CONTEXT_DNA_AVAILABLE:
            raise RuntimeError("Context DNA not available")

        self.memory = ContextDNAClient()

        if not self.memory.ping():
            raise RuntimeError("Context DNA server not running")

    def start_task(self, description: str, area: str = None) -> str:
        """
        Start an architecture task.

        This creates a tracked task in Context DNA that will trigger SOP
        extraction when completed successfully.

        Args:
            description: What you're doing ("Deploy Django to production")
            area: Optional area tag ("django", "livekit", "gpu", etc.)

        Returns:
            task_id for completion
        """
        # Use the same session tracking as the learning system
        session_id = self.memory.start_agent_session(
            task_description=f"[ARCHITECTURE] {description}",
            agent_name="atlas"
        )

        # Track locally for recovery
        state = _load_state()
        state["active_tasks"][session_id] = {
            "description": description,
            "area": area,
            "started_at": datetime.now().isoformat(),
            "steps": []
        }
        _save_state(state)

        print(f"   [arch] Started: {description}")
        return session_id

    def log_step(self, task_id: str, step: str, result: str = None):
        """
        Log a step in the architecture task.

        Each step becomes part of the learned procedure.

        Args:
            task_id: From start_task()
            step: What you did ("SSH to 10.0.1.5", "Ran systemctl restart gunicorn")
            result: Optional result/output
        """
        # Log to Context DNA
        self.memory.log_agent_step(task_id, step, result)

        # Track locally
        state = _load_state()
        if task_id in state["active_tasks"]:
            state["active_tasks"][task_id]["steps"].append({
                "step": step,
                "result": result[:500] if result else None,
                "at": datetime.now().isoformat()
            })
            _save_state(state)

        print(f"   [arch] Step: {step[:60]}...")

    def complete_task(self, task_id: str, success: bool, summary: str,
                      learnings: list[str] = None, details: str = None):
        """
        Complete an architecture task.

        On success, Context DNA extracts the procedure as an SOP.
        Future agents querying this area will get the learned procedure.

        Args:
            task_id: From start_task()
            success: Did it work?
            summary: Brief summary of what was done
            learnings: Key takeaways (gotchas, configurations, etc.)
            details: Optional detailed notes
        """
        # Build learnings list
        all_learnings = learnings or []
        if details:
            all_learnings.append(f"Details: {details}")

        # Get area for tagging
        state = _load_state()
        task_info = state["active_tasks"].get(task_id, {})
        area = task_info.get("area", "general")

        # Add architecture-specific learning
        if success:
            all_learnings.append(f"Architecture area: {area}")
            all_learnings.append("This procedure was verified working")

        # Complete in Context DNA - this triggers SOP extraction!
        self.memory.complete_task(
            session_id=task_id,
            success=success,
            summary=f"[ARCHITECTURE:{area}] {summary}",
            learnings=all_learnings
        )

        # Cleanup local state
        if task_id in state["active_tasks"]:
            del state["active_tasks"][task_id]
            _save_state(state)

        status = "✓" if success else "✗"
        print(f"   [arch] Completed {status}: {summary}")

    def get_architecture(self, query: str, limit: int = 5) -> str:
        """
        Get learned architecture for a query.

        This queries Context DNA for relevant architecture procedures.

        Args:
            query: What you're looking for ("django deployment", "gpu toggle")
            limit: Max results

        Returns:
            Formatted architecture context
        """
        # Query with architecture prefix to find relevant learnings
        learnings = self.memory.get_relevant_learnings(
            f"architecture {query}",
            limit=limit
        )

        if not learnings:
            return f"# No architecture learned yet for: {query}\n"

        output = [
            "# LEARNED ARCHITECTURE",
            f"# Query: {query}",
            "",
        ]

        for i, learning in enumerate(learnings, 1):
            if learning.get("distance", 1.0) < 0.7:  # Relevant results only
                output.append(f"## {i}. {learning.get('title', 'Procedure')}")

                if learning.get('preferences'):
                    # This contains the SOP/procedure
                    pref = learning['preferences']
                    output.append(pref[:2000] if len(pref) > 2000 else pref)

                output.append("")

        output.append("---")
        return "\n".join(output)


# Global tracker instance
_tracker = None


def _get_tracker() -> ArchitectureTaskTracker:
    global _tracker
    if _tracker is None:
        _tracker = ArchitectureTaskTracker()
    return _tracker


def start_architecture_task(description: str, area: str = None) -> str:
    """Start an architecture task. Returns task_id."""
    return _get_tracker().start_task(description, area)


def log_architecture_step(task_id: str, step: str, result: str = None):
    """Log a step in the architecture task."""
    _get_tracker().log_step(task_id, step, result)


def complete_architecture_task(task_id: str, success: bool, summary: str,
                               learnings: list[str] = None, details: str = None):
    """Complete an architecture task. Triggers SOP extraction on success."""
    _get_tracker().complete_task(task_id, success, summary, learnings, details)


def get_architecture(query: str) -> str:
    """Get learned architecture for a query."""
    return _get_tracker().get_architecture(query)


@contextmanager
def arch_task(description: str, area: str = None):
    """
    Context manager for architecture tasks.

    Usage:
        with arch_task("Deploy Django update", area="django"):
            # Do infrastructure work
            subprocess.run(["ssh", ...])
            subprocess.run(["systemctl", ...])
        # Automatically completes on exit

    On exception, marks task as failed.
    On clean exit, marks task as successful.
    """
    tracker = _get_tracker()
    task_id = tracker.start_task(description, area)
    steps_taken = []

    # Provide a way to log steps
    class TaskContext:
        def step(self, step: str, result: str = None):
            tracker.log_step(task_id, step, result)
            steps_taken.append(step)

    ctx = TaskContext()

    try:
        yield ctx
        # Success - clean exit
        summary = f"Completed: {description}"
        if steps_taken:
            summary += f" ({len(steps_taken)} steps)"
        tracker.complete_task(task_id, success=True, summary=summary)

    except Exception as e:
        # Failure
        tracker.complete_task(
            task_id,
            success=False,
            summary=f"Failed: {description}",
            learnings=[f"Error: {str(e)}"]
        )
        raise


# Convenience function for agents
def before_infrastructure_work(area: str) -> str:
    """
    Call this BEFORE doing any infrastructure work.

    Returns learned procedures for the area so you don't repeat mistakes.

    Args:
        area: What you're working on ("django", "gpu", "livekit", etc.)

    Returns:
        Formatted context with learned procedures
    """
    return get_architecture(area)


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Autonomous Architecture Discovery")
        print("")
        print("Commands:")
        print("  start <desc> [area]   - Start architecture task")
        print("  step <task_id> <step> - Log a step")
        print("  complete <task_id>    - Complete task successfully")
        print("  fail <task_id> <why>  - Mark task as failed")
        print("  get <query>           - Get learned architecture")
        print("  active                - Show active tasks")
        print("")
        print("Examples:")
        print("  python auto_architecture.py start 'Deploy Django' django")
        print("  python auto_architecture.py step abc123 'SSH to server'")
        print("  python auto_architecture.py complete abc123")
        print("  python auto_architecture.py get 'django deployment'")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "start":
        if len(sys.argv) < 3:
            print("Usage: start <description> [area]")
            sys.exit(1)
        desc = sys.argv[2]
        area = sys.argv[3] if len(sys.argv) > 3 else None
        task_id = start_architecture_task(desc, area)
        print(f"Task ID: {task_id}")

    elif cmd == "step":
        if len(sys.argv) < 4:
            print("Usage: step <task_id> <step>")
            sys.exit(1)
        task_id = sys.argv[2]
        step = " ".join(sys.argv[3:])
        log_architecture_step(task_id, step)

    elif cmd == "complete":
        if len(sys.argv) < 3:
            print("Usage: complete <task_id>")
            sys.exit(1)
        task_id = sys.argv[2]
        complete_architecture_task(task_id, success=True, summary="Completed successfully")

    elif cmd == "fail":
        if len(sys.argv) < 4:
            print("Usage: fail <task_id> <reason>")
            sys.exit(1)
        task_id = sys.argv[2]
        reason = " ".join(sys.argv[3:])
        complete_architecture_task(task_id, success=False, summary=f"Failed: {reason}")

    elif cmd == "get":
        if len(sys.argv) < 3:
            print("Usage: get <query>")
            sys.exit(1)
        query = " ".join(sys.argv[2:])
        print(get_architecture(query))

    elif cmd == "active":
        state = _load_state()
        tasks = state.get("active_tasks", {})
        if tasks:
            for tid, info in tasks.items():
                print(f"\n{tid[:8]}... - {info['description']}")
                print(f"  Area: {info.get('area', 'general')}")
                print(f"  Started: {info['started_at']}")
                print(f"  Steps: {len(info.get('steps', []))}")
        else:
            print("No active architecture tasks.")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
