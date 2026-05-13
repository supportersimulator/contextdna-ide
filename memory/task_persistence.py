 #!/usr/bin/env python3
"""
TASK PERSISTENCE - SQLite Storage Layer for Cognitive Control Tasks

This module provides durable task storage for the Phone → Synaptic → Atlas
cognitive control architecture designed by Logos.

Architecture:
- Single-user SQLite (zero infra overhead)
- Task identity survives compaction, crashes, restarts
- Authority is explicit and traceable

Task Spine (Logos' design):
    Task {
        task_id: "T-ABC123",
        source: "[user_id:device_token]",  # Authenticated identity from voice session
        intent: "Refactor Docker health check logic",
        status: "pending | executing | reviewing | completed | failed | aborted",
        created_at: timestamp,
        updated_at: timestamp,
        directives: { objective, constraints, tools_allowed },
        atlas_output: null | string,
        synaptic_review: null | { outcome, confidence, notes, followups }
    }

Source Identity Format:
    - Authenticated voice: "[550e8400:abc12345]" (user_id_prefix:device_token_prefix)
    - Dev mode: "[dev-user-:dev_devi]"
    - API direct: "api_direct"
    - Legacy: "voice_command" (unauthenticated)

Note: user_id is the Supabase UUID (stable, no PII). First 8 chars used for readable identity.

Usage:
    from memory.task_persistence import TaskStore

    store = TaskStore()

    # Create a task with authenticated source
    task = store.create_task(
        source="[550e8400:device12]",  # From voice session auth (user_id:device_token)
        intent="Refactor Docker health check",
        constraints=["Don't modify production", "Explain reasoning"]
    )

    # Update task status
    store.update_task_status(task["task_id"], "executing")

    # Add Atlas output
    store.add_atlas_output(task["task_id"], "Refactored with retry logic...")

    # Add Synaptic review
    store.add_synaptic_review(task["task_id"], {
        "outcome": "pass",
        "confidence": 0.93,
        "notes": "Clean implementation"
    })

    # Get task by ID
    task = store.get_task("T-ABC123")
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any
from uuid import uuid4
from dataclasses import dataclass, asdict
import threading


# Database location
CONTEXT_DNA_DIR = Path.home() / ".context-dna"
_LEGACY_TASK_DB = CONTEXT_DNA_DIR / "contextdna_tasks.db"


def _get_task_db() -> Path:
    from memory.db_utils import get_unified_db_path
    return get_unified_db_path(_LEGACY_TASK_DB)


TASK_DB = _get_task_db()

def _t_task(name: str) -> str:
    from memory.db_utils import unified_table
    return unified_table("contextdna_tasks.db", name)



@dataclass
class TaskDirectives:
    """Directives that guide Atlas's execution."""
    objective: str
    constraints: List[str] = None
    tools_allowed: List[str] = None
    priority: str = "normal"

    def __post_init__(self):
        if self.constraints is None:
            self.constraints = []
        if self.tools_allowed is None:
            self.tools_allowed = []


@dataclass
class SynapticReview:
    """Synaptic's review of Atlas's work."""
    outcome: str  # pass | needs_revision | fail
    confidence: float
    notes: str
    followups: List[str] = None
    reviewed_at: str = None

    def __post_init__(self):
        if self.followups is None:
            self.followups = []
        if self.reviewed_at is None:
            self.reviewed_at = datetime.now(timezone.utc).isoformat()


@dataclass
class ProgressEvent:
    """Progress event from Atlas during task execution.

    Three-tier model (Logos design):
    - Stage markers: "reading_code", "planning", "implementing", "testing"
    - Percentage: 0-100 (optional, for measurable progress)
    - Heartbeats: Regular pings (every ~2.5s) to show activity
    """
    event_type: str  # stage | percentage | heartbeat
    stage: Optional[str] = None  # reading_code, planning, implementing, testing, etc.
    percentage: Optional[int] = None  # 0-100
    message: Optional[str] = None  # Human-readable status
    timestamp: str = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        # Validate event_type
        valid_types = {"stage", "percentage", "heartbeat"}
        if self.event_type not in valid_types:
            raise ValueError(f"Invalid event_type: {self.event_type}. Must be one of {valid_types}")


@dataclass
class TerminalEvent:
    """Terminal event marking task completion/failure.

    outcome: completed | failed | blocked
    - completed: Task finished successfully, ready for review
    - failed: Task could not be completed (with reason)
    - blocked: Task needs user input/decision to proceed
    """
    outcome: str  # completed | failed | blocked
    summary: str  # What was accomplished or why it failed
    files_changed: List[str] = None  # List of modified files
    blocker_reason: Optional[str] = None  # If blocked, what's needed
    timestamp: str = None

    def __post_init__(self):
        if self.files_changed is None:
            self.files_changed = []
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        # Validate outcome
        valid_outcomes = {"completed", "failed", "blocked"}
        if self.outcome not in valid_outcomes:
            raise ValueError(f"Invalid outcome: {self.outcome}. Must be one of {valid_outcomes}")


class TaskStore:
    """
    SQLite-backed task storage for cognitive control.

    Thread-safe, durable, and simple by design.
    """

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or TASK_DB
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        CONTEXT_DNA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initialize the database schema."""
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {_t_task('tasks')} (
                    task_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            # Index for fast status queries
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_tasks_status
                ON {_t_task('tasks')}(status)
            """)
            # Index for fast source queries
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_tasks_created
                ON {_t_task('tasks')}(created_at DESC)
            """)
            # Progress events table (Logos Priority 1)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {_t_task('progress_events')} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT,
                    percentage INTEGER,
                    message TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                )
            """)
            # Index for fast progress queries by task
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_progress_task_id
                ON {_t_task('progress_events')}(task_id, timestamp DESC)
            """)
            conn.commit()
            conn.close()

    def _generate_task_id(self) -> str:
        """Generate a unique task ID."""
        return f"T-{uuid4().hex[:8].upper()}"

    def create_task(
        self,
        source: str,
        intent: str,
        constraints: List[str] = None,
        tools_allowed: List[str] = None,
        priority: str = "normal"
    ) -> Dict[str, Any]:
        """
        Create a new task.

        Args:
            source: Origin of the task - authenticated identity [user_id:device_token]
            intent: What the task should accomplish
            constraints: Boundaries for execution
            tools_allowed: Specific tools Atlas can use
            priority: normal | high | urgent

        Returns:
            The created task as a dictionary
        """
        now = datetime.now(timezone.utc).isoformat()
        task_id = self._generate_task_id()

        directives = TaskDirectives(
            objective=intent,
            constraints=constraints or [],
            tools_allowed=tools_allowed or [],
            priority=priority
        )

        task = {
            "task_id": task_id,
            "source": source,
            "intent": intent,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "directives": asdict(directives),
            "atlas_output": None,
            "synaptic_review": None
        }

        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO {_t_task('tasks')} (task_id, data, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, json.dumps(task), "pending", now, now)
            )
            conn.commit()
            conn.close()

        return task

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a task by ID.

        Args:
            task_id: The task identifier

        Returns:
            Task dictionary or None if not found
        """
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(f"SELECT data FROM {_t_task('tasks')} WHERE task_id = ?", (task_id,))
            row = cur.fetchone()
            conn.close()

            if not row:
                return None
            return json.loads(row["data"])

    def update_task(self, task_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update a task with arbitrary fields.

        Args:
            task_id: The task identifier
            updates: Fields to update

        Returns:
            Updated task dictionary

        Raises:
            ValueError: If task not found
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()

            cur.execute(f"SELECT data FROM {_t_task('tasks')} WHERE task_id = ?", (task_id,))
            row = cur.fetchone()

            if not row:
                conn.close()
                raise ValueError(f"Task not found: {task_id}")

            task = json.loads(row["data"])
            task.update(updates)
            task["updated_at"] = now

            # Also update status column if status changed
            status = task.get("status", "pending")

            cur.execute(
                f"UPDATE {_t_task('tasks')} SET data = ?, status = ?, updated_at = ? WHERE task_id = ?",
                (json.dumps(task), status, now, task_id)
            )
            conn.commit()
            conn.close()

            return task

    def update_task_status(
        self,
        task_id: str,
        status: str,
        reason: str = None
    ) -> Dict[str, Any]:
        """
        Update a task's status.

        Valid statuses: pending, executing, reviewing, completed, failed, aborted

        Args:
            task_id: The task identifier
            status: New status
            reason: Optional reason for status change

        Returns:
            Updated task dictionary
        """
        valid_statuses = {"pending", "executing", "reviewing", "completed", "failed", "aborted", "blocked", "stalled"}
        if status not in valid_statuses:
            raise ValueError(f"Invalid status: {status}. Must be one of {valid_statuses}")

        updates = {"status": status}
        if reason:
            updates["status_reason"] = reason

        return self.update_task(task_id, updates)

    def add_atlas_output(self, task_id: str, output: str) -> Dict[str, Any]:
        """
        Add Atlas's output to a task.

        Args:
            task_id: The task identifier
            output: Atlas's work output

        Returns:
            Updated task dictionary
        """
        return self.update_task(task_id, {
            "atlas_output": output,
            "status": "reviewing"  # Automatically move to review
        })

    def add_synaptic_review(
        self,
        task_id: str,
        review: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Add Synaptic's review to a task.

        Args:
            task_id: The task identifier
            review: Review data with outcome, confidence, notes, followups

        Returns:
            Updated task dictionary
        """
        # Validate review structure
        synaptic_review = SynapticReview(
            outcome=review.get("outcome", "pass"),
            confidence=review.get("confidence", 0.0),
            notes=review.get("notes", ""),
            followups=review.get("followups", [])
        )

        # Determine final status based on review
        outcome = synaptic_review.outcome
        if outcome == "pass":
            new_status = "completed"
        elif outcome == "needs_revision":
            new_status = "pending"  # Goes back to pending for revision
        else:  # fail
            new_status = "failed"

        return self.update_task(task_id, {
            "synaptic_review": asdict(synaptic_review),
            "status": new_status
        })

    def abort_task(self, task_id: str, reason: str = "User requested abort") -> Dict[str, Any]:
        """
        Abort a task (kill switch).

        Args:
            task_id: The task identifier
            reason: Why the task was aborted

        Returns:
            Updated task dictionary
        """
        return self.update_task_status(task_id, "aborted", reason)

    def get_tasks_by_status(self, status: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get tasks by status.

        Args:
            status: Status to filter by
            limit: Maximum number of tasks to return

        Returns:
            List of task dictionaries
        """
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                f"SELECT data FROM {_t_task('tasks')} WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            )
            rows = cur.fetchall()
            conn.close()

            return [json.loads(row["data"]) for row in rows]

    def get_recent_tasks(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Get the most recent tasks.

        Args:
            limit: Maximum number of tasks to return

        Returns:
            List of task dictionaries
        """
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                f"SELECT data FROM {_t_task('tasks')} ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
            rows = cur.fetchall()
            conn.close()

            return [json.loads(row["data"]) for row in rows]

    def get_active_tasks(self) -> List[Dict[str, Any]]:
        """
        Get all currently active tasks (pending or executing).

        Returns:
            List of active task dictionaries
        """
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                f"SELECT data FROM {_t_task('tasks')} WHERE status IN ('pending', 'executing') ORDER BY created_at DESC"
            )
            rows = cur.fetchall()
            conn.close()

            return [json.loads(row["data"]) for row in rows]

    def get_task_summary(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a summary of a task suitable for phone reporting.

        Args:
            task_id: The task identifier

        Returns:
            Summary dictionary or None if not found
        """
        task = self.get_task(task_id)
        if not task:
            return None

        # Build summary
        summary = {
            "task_id": task["task_id"],
            "status": task["status"],
            "intent": task["intent"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"]
        }

        # Add output summary if exists
        if task.get("atlas_output"):
            output = task["atlas_output"]
            summary["output_preview"] = output[:200] + "..." if len(output) > 200 else output

        # Add review if exists
        if task.get("synaptic_review"):
            summary["synaptic_review"] = task["synaptic_review"]

        return summary

    # =========================================================================
    # PROGRESS TRACKING (Logos Priority 1)
    # =========================================================================

    def add_progress_event(
        self,
        task_id: str,
        event_type: str,
        stage: str = None,
        percentage: int = None,
        message: str = None
    ) -> ProgressEvent:
        """
        Add a progress event for a task.

        Three-tier model:
        - stage: "reading_code", "planning", "implementing", "testing"
        - percentage: 0-100 for measurable progress
        - heartbeat: Regular pings to show activity

        Args:
            task_id: The task identifier
            event_type: stage | percentage | heartbeat
            stage: Current stage name (if event_type == "stage")
            percentage: Progress 0-100 (if event_type == "percentage")
            message: Human-readable status message

        Returns:
            The created ProgressEvent
        """
        # Validate task exists
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        # Create event
        event = ProgressEvent(
            event_type=event_type,
            stage=stage,
            percentage=percentage,
            message=message
        )

        # Store in database
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(f"""
                INSERT INTO {_t_task('progress_events')}
                (task_id, event_type, stage, percentage, message, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (task_id, event.event_type, event.stage, event.percentage,
                  event.message, event.timestamp))
            conn.commit()
            conn.close()

        # Update task status to executing if still pending
        if task["status"] == "pending":
            self.update_task_status(task_id, "executing")

        return event

    def get_progress_events(self, task_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get progress events for a task.

        Args:
            task_id: The task identifier
            limit: Maximum events to return (most recent first)

        Returns:
            List of progress event dictionaries
        """
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(f"""
                SELECT event_type, stage, percentage, message, timestamp
                FROM {_t_task('progress_events')}
                WHERE task_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (task_id, limit))
            rows = cur.fetchall()
            conn.close()

            return [
                {
                    "event_type": row[0],
                    "stage": row[1],
                    "percentage": row[2],
                    "message": row[3],
                    "timestamp": row[4]
                }
                for row in rows
            ]

    def get_latest_progress(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the most recent progress event for a task.

        Args:
            task_id: The task identifier

        Returns:
            Latest progress event or None
        """
        events = self.get_progress_events(task_id, limit=1)
        return events[0] if events else None

    def set_terminal_event(
        self,
        task_id: str,
        outcome: str,
        summary: str,
        files_changed: List[str] = None,
        blocker_reason: str = None
    ) -> TerminalEvent:
        """
        Set the terminal event for a task (completion/failure/blocked).

        This marks the end of Atlas's work on the task:
        - completed: Ready for Synaptic review
        - failed: Could not complete, with reason
        - blocked: Needs user input to proceed

        Args:
            task_id: The task identifier
            outcome: completed | failed | blocked
            summary: What was accomplished or why it failed
            files_changed: List of modified files
            blocker_reason: If blocked, what's needed

        Returns:
            The created TerminalEvent
        """
        # Create terminal event
        event = TerminalEvent(
            outcome=outcome,
            summary=summary,
            files_changed=files_changed,
            blocker_reason=blocker_reason
        )

        # Update task with terminal event
        updates = {
            "terminal_event": asdict(event)
        }

        # Set appropriate status based on outcome
        if outcome == "completed":
            updates["status"] = "reviewing"  # Ready for Synaptic review
            updates["atlas_output"] = summary  # Store summary as output
        elif outcome == "failed":
            updates["status"] = "failed"
        elif outcome == "blocked":
            updates["status"] = "blocked"

        self.update_task(task_id, updates)

        return event


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_store: Optional[TaskStore] = None


def get_task_store() -> TaskStore:
    """Get the global task store instance."""
    global _store
    if _store is None:
        _store = TaskStore()
    return _store


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    store = get_task_store()

    if len(sys.argv) < 2:
        print("╔══════════════════════════════════════════════════════════════════╗")
        print("║  TASK PERSISTENCE - Cognitive Control Task Storage               ║")
        print("║  Designed by Logos for Phone → Synaptic → Atlas architecture     ║")
        print("╚══════════════════════════════════════════════════════════════════╝")
        print()
        print("Usage:")
        print("  python task_persistence.py create <source> <intent>")
        print("  python task_persistence.py get <task_id>")
        print("  python task_persistence.py status <task_id> <new_status>")
        print("  python task_persistence.py list [status]")
        print("  python task_persistence.py abort <task_id> [reason]")
        print()
        print("Examples:")
        print("  python task_persistence.py create '[550e8400:device12]' 'Refactor Docker health check'")
        print("  python task_persistence.py get T-ABC123")
        print("  python task_persistence.py status T-ABC123 executing")
        print("  python task_persistence.py list pending")
        print("  python task_persistence.py abort T-ABC123 'User requested'")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "create" and len(sys.argv) >= 4:
        source = sys.argv[2]
        intent = " ".join(sys.argv[3:])
        task = store.create_task(source, intent)
        print(f"✅ Created task: {task['task_id']}")
        print(json.dumps(task, indent=2))

    elif cmd == "get" and len(sys.argv) >= 3:
        task_id = sys.argv[2]
        task = store.get_task(task_id)
        if task:
            print(json.dumps(task, indent=2))
        else:
            print(f"❌ Task not found: {task_id}")

    elif cmd == "status" and len(sys.argv) >= 4:
        task_id = sys.argv[2]
        status = sys.argv[3]
        try:
            task = store.update_task_status(task_id, status)
            print(f"✅ Updated {task_id} to {status}")
            print(json.dumps(task, indent=2))
        except ValueError as e:
            print(f"❌ {e}")

    elif cmd == "list":
        status = sys.argv[2] if len(sys.argv) > 2 else None
        if status:
            tasks = store.get_tasks_by_status(status)
            print(f"Tasks with status '{status}':")
        else:
            tasks = store.get_recent_tasks()
            print("Recent tasks:")

        for task in tasks:
            print(f"  {task['task_id']} [{task['status']}] {task['intent'][:50]}...")

    elif cmd == "abort" and len(sys.argv) >= 3:
        task_id = sys.argv[2]
        reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "User requested abort"
        try:
            task = store.abort_task(task_id, reason)
            print(f"🛑 Aborted task: {task_id}")
            print(json.dumps(task, indent=2))
        except ValueError as e:
            print(f"❌ {e}")

    else:
        print(f"Unknown command or missing arguments: {cmd}")
        sys.exit(1)
