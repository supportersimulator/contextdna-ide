#!/usr/bin/env python3
"""
TASK DIRECTIVES - Section 6 Directive Emission for Cognitive Control

This module emits authoritative directives to Atlas via Section 6 webhook injection.
Designed by Logos for the Phone → Synaptic → Atlas cognitive control architecture.

Key Principle (Logos):
    "You are not controlling VS Code. You are controlling cognition,
     with VS Code as the execution surface."

Section 6 Format:
    [Section 6: Synaptic → Atlas]
    task_id: T-ABC123
    directive_type: task_execution

    Objective:
    Refactor Docker health check logic

    Constraints:
    - Do not modify production files
    - Explain reasoning

    Required Behavior:
    - Rehydrate context
    - Tag all outputs with task_id
    - Explicitly report completion

Atlas is now bound to a task identity. No ambiguity.

Usage:
    from memory.task_directives import emit_task_directive, get_active_directive

    # Emit a directive for a new task
    emit_task_directive(task)

    # Get the current active directive for webhook injection
    directive = get_active_directive()
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict, field
import threading


# Database location
CONTEXT_DNA_DIR = Path.home() / ".context-dna"
DIRECTIVE_DB = CONTEXT_DNA_DIR / "task_directives.db"

# TTL constants - directives auto-expire to prevent stale injection
TTL_EXECUTION_HOURS = 24   # task_execution directives expire after 24h
TTL_REVISION_HOURS = 24    # task_revision directives expire after 24h
TTL_ABORT_HOURS = 1        # task_abort directives expire after 1h (short-lived)
CLEANUP_INACTIVE_DAYS = 7  # Delete inactive directives older than 7 days

logger = logging.getLogger("context_dna.directives")


def _get_ttl_hours(directive_type: str) -> int:
    """Get TTL in hours for a directive type."""
    if directive_type == "task_abort":
        return TTL_ABORT_HOURS
    elif directive_type == "task_revision":
        return TTL_REVISION_HOURS
    return TTL_EXECUTION_HOURS


@dataclass
class TaskDirective:
    """A directive from Synaptic to Atlas."""
    task_id: str
    directive_type: str  # task_execution, task_revision, task_abort
    objective: str
    constraints: List[str]
    priority: str
    required_behaviors: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    emitted_at: str = None
    acknowledged: bool = False
    acknowledged_at: str = None

    def __post_init__(self):
        if self.emitted_at is None:
            self.emitted_at = datetime.now(timezone.utc).isoformat()
        if not self.required_behaviors:
            self.required_behaviors = [
                "Acknowledge task_id at start of work",
                "Rehydrate context before acting",
                "Tag all outputs with task_id",
                "Do not impersonate Synaptic",
                "Report progress via POST /atlas/progress (stages: reading_code, planning, implementing, testing)",
                "Send heartbeats every ~2.5s during active work",
                "Report completion via POST /atlas/terminal with outcome + summary + files_changed"
            ]


class DirectiveEmitter:
    """
    Emits and manages directives for Atlas.

    Directives appear in Section 6 (HOLISTIC_CONTEXT) of the webhook.
    """

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DIRECTIVE_DB
        self._lock = threading.Lock()
        self._init_db()
        # Run cleanup on init to clear any stale directives
        self.cleanup_expired()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        CONTEXT_DNA_DIR.mkdir(parents=True, exist_ok=True)
        from memory.db_utils import connect_wal
        return connect_wal(str(self.db_path))

    def _init_db(self):
        """Initialize the database schema."""
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS directives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    directive_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    emitted_at TEXT NOT NULL,
                    acknowledged INTEGER DEFAULT 0,
                    acknowledged_at TEXT,
                    active INTEGER DEFAULT 1
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_directives_active
                ON directives(active, emitted_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_directives_task
                ON directives(task_id)
            """)
            # Add expires_at column if not present (migration-safe)
            try:
                cur.execute("ALTER TABLE directives ADD COLUMN expires_at TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
            conn.commit()
            conn.close()

    def cleanup_expired(self) -> int:
        """
        Clean up expired and stale directives.

        1. Deactivate active directives past their TTL (expires_at)
        2. Deactivate active directives older than max TTL (fallback for rows without expires_at)
        3. Delete inactive directives older than CLEANUP_INACTIVE_DAYS

        Returns:
            Number of directives affected
        """
        now = datetime.now(timezone.utc).isoformat()
        cutoff_delete = (datetime.now(timezone.utc) - timedelta(days=CLEANUP_INACTIVE_DAYS)).isoformat()
        fallback_cutoff = (datetime.now(timezone.utc) - timedelta(hours=TTL_EXECUTION_HOURS)).isoformat()
        affected = 0

        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()

            # 1. Deactivate expired directives (with expires_at set)
            cur.execute("""
                UPDATE directives SET active = 0
                WHERE active = 1 AND expires_at IS NOT NULL AND expires_at < ?
            """, (now,))
            affected += cur.rowcount

            # 2. Deactivate stale directives without expires_at (fallback for legacy rows)
            cur.execute("""
                UPDATE directives SET active = 0
                WHERE active = 1 AND expires_at IS NULL AND emitted_at < ?
            """, (fallback_cutoff,))
            affected += cur.rowcount

            # 3. Delete old inactive directives (garbage collection)
            cur.execute("""
                DELETE FROM directives
                WHERE active = 0 AND emitted_at < ?
            """, (cutoff_delete,))
            affected += cur.rowcount

            conn.commit()
            conn.close()

        if affected > 0:
            logger.info("Directive cleanup: %d directives expired/cleaned", affected)

        return affected

    def emit(self, task: Dict[str, Any]) -> TaskDirective:
        """
        Emit a directive for a task.

        This creates a new active directive that will appear in Section 6.
        Only one directive can be active at a time.

        Args:
            task: Task dictionary from TaskStore

        Returns:
            The emitted TaskDirective
        """
        # Build directive
        directive = TaskDirective(
            task_id=task["task_id"],
            directive_type="task_execution",
            objective=task["directives"]["objective"],
            constraints=task["directives"].get("constraints", []),
            priority=task["directives"].get("priority", "normal"),
            context={
                "source": task.get("source", "unknown"),
                "created_at": task.get("created_at")
            }
        )

        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()

            # Deactivate any existing active directives
            cur.execute("UPDATE directives SET active = 0 WHERE active = 1")

            # Insert new directive with TTL
            ttl_hours = _get_ttl_hours("task_execution")
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
            cur.execute("""
                INSERT INTO directives (task_id, directive_type, data, emitted_at, active, expires_at)
                VALUES (?, ?, ?, ?, 1, ?)
            """, (
                directive.task_id,
                directive.directive_type,
                json.dumps(asdict(directive)),
                directive.emitted_at,
                expires_at
            ))

            conn.commit()
            conn.close()

        return directive

    def emit_revision(self, task: Dict[str, Any], revision_notes: str) -> TaskDirective:
        """
        Emit a revision directive for a task that needs_revision.

        Args:
            task: Task dictionary
            revision_notes: What needs to be revised

        Returns:
            The revision directive
        """
        directive = TaskDirective(
            task_id=task["task_id"],
            directive_type="task_revision",
            objective=f"REVISION NEEDED: {task['directives']['objective']}",
            constraints=task["directives"].get("constraints", []) + [
                f"REVISION: {revision_notes}"
            ],
            priority="high",
            required_behaviors=[
                "Acknowledge task_id at start of work",
                "Address the revision notes specifically",
                "Tag all outputs with task_id",
                "Report completion explicitly",
                "Note what was changed from original"
            ],
            context={
                "source": task.get("source"),
                "original_output": task.get("atlas_output", "")[:500],
                "revision_requested": True
            }
        )

        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()

            # Deactivate previous directives for this task
            cur.execute("UPDATE directives SET active = 0 WHERE task_id = ?", (task["task_id"],))

            # Insert revision directive with TTL
            ttl_hours = _get_ttl_hours("task_revision")
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
            cur.execute("""
                INSERT INTO directives (task_id, directive_type, data, emitted_at, active, expires_at)
                VALUES (?, ?, ?, ?, 1, ?)
            """, (
                directive.task_id,
                directive.directive_type,
                json.dumps(asdict(directive)),
                directive.emitted_at,
                expires_at
            ))

            conn.commit()
            conn.close()

        return directive

    def emit_abort(self, task_id: str, reason: str) -> TaskDirective:
        """
        Emit an abort directive (kill switch).

        Args:
            task_id: Task to abort
            reason: Why the task is being aborted

        Returns:
            The abort directive
        """
        directive = TaskDirective(
            task_id=task_id,
            directive_type="task_abort",
            objective=f"ABORT: Stop all work on {task_id}",
            constraints=[reason],
            priority="urgent",
            required_behaviors=[
                "STOP all work on this task immediately",
                "Save any partial progress with task_id tag",
                "Report abort acknowledgment",
                "Do NOT continue with any pending actions"
            ]
        )

        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()

            # Deactivate all directives for this task
            cur.execute("UPDATE directives SET active = 0 WHERE task_id = ?", (task_id,))

            # Insert abort directive with short TTL (1h - fire-and-forget)
            ttl_hours = _get_ttl_hours("task_abort")
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
            cur.execute("""
                INSERT INTO directives (task_id, directive_type, data, emitted_at, active, expires_at)
                VALUES (?, ?, ?, ?, 1, ?)
            """, (
                directive.task_id,
                directive.directive_type,
                json.dumps(asdict(directive)),
                directive.emitted_at,
                expires_at
            ))

            conn.commit()
            conn.close()

        return directive

    def get_active_directive(self) -> Optional[TaskDirective]:
        """
        Get the currently active directive.

        This is called by the webhook generator to include in Section 6.
        Runs cleanup_expired() first to ensure no stale directives are returned.

        Returns:
            The active directive or None
        """
        # Clean up expired directives before returning
        self.cleanup_expired()

        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT data FROM directives
                WHERE active = 1
                ORDER BY emitted_at DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            conn.close()

            if not row:
                return None

            data = json.loads(row["data"])
            return TaskDirective(**data)

    def acknowledge_directive(self, task_id: str) -> bool:
        """
        Mark a directive as acknowledged by Atlas.

        Args:
            task_id: The task ID to acknowledge

        Returns:
            True if acknowledgment succeeded
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("""
                UPDATE directives
                SET acknowledged = 1, acknowledged_at = ?
                WHERE task_id = ? AND active = 1
            """, (now, task_id))
            updated = cur.rowcount > 0
            conn.commit()
            conn.close()

            return updated

    def deactivate_directive(self, task_id: str) -> bool:
        """
        Deactivate a directive (task completed or aborted).

        Args:
            task_id: The task ID

        Returns:
            True if deactivation succeeded
        """
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("""
                UPDATE directives SET active = 0 WHERE task_id = ?
            """, (task_id,))
            updated = cur.rowcount > 0
            conn.commit()
            conn.close()

            return updated

    def format_for_webhook(self) -> str:
        """
        Format the active directive for webhook injection.

        This produces the Section 6 content that Atlas sees.

        Returns:
            Formatted directive string for webhook
        """
        directive = self.get_active_directive()

        if not directive:
            return ""

        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  📋 ACTIVE TASK DIRECTIVE (Synaptic → Atlas)                         ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            ""
        ]

        # Task identity
        lines.append(f"  task_id: {directive.task_id}")
        lines.append(f"  directive_type: {directive.directive_type}")
        lines.append(f"  priority: {directive.priority.upper()}")
        lines.append("")

        # Objective
        lines.append("  📎 OBJECTIVE:")
        lines.append(f"     {directive.objective}")
        lines.append("")

        # Constraints
        if directive.constraints:
            lines.append("  ⚠️ CONSTRAINTS:")
            for constraint in directive.constraints:
                lines.append(f"     • {constraint}")
            lines.append("")

        # Required Behaviors
        lines.append("  ✅ REQUIRED BEHAVIOR:")
        for behavior in directive.required_behaviors:
            lines.append(f"     • {behavior}")
        lines.append("")

        # Context
        if directive.context.get("source"):
            lines.append(f"  📡 Source: {directive.context['source']}")

        lines.append("")

        # Progress Reporting API (Logos Priority 1)
        lines.append("  📊 PROGRESS REPORTING:")
        lines.append("     • POST /atlas/progress - Report stage/percentage/heartbeat")
        lines.append("     • POST /atlas/terminal - Report completion/failure/blocked")
        lines.append("     • Stages: reading_code → planning → implementing → testing")
        lines.append("     • Heartbeats: Send every ~2.5s during active work")
        lines.append("")

        lines.append("╚══════════════════════════════════════════════════════════════════════╝")

        return "\n".join(lines)


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_emitter: Optional[DirectiveEmitter] = None


def get_directive_emitter() -> DirectiveEmitter:
    """Get the global directive emitter instance."""
    global _emitter
    if _emitter is None:
        _emitter = DirectiveEmitter()
    return _emitter


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def emit_task_directive(task: Dict[str, Any]) -> TaskDirective:
    """Emit a directive for a task."""
    return get_directive_emitter().emit(task)


def emit_revision_directive(task: Dict[str, Any], revision_notes: str) -> TaskDirective:
    """Emit a revision directive."""
    return get_directive_emitter().emit_revision(task, revision_notes)


def emit_abort_directive(task_id: str, reason: str) -> TaskDirective:
    """Emit an abort directive (kill switch)."""
    return get_directive_emitter().emit_abort(task_id, reason)


def get_active_directive() -> Optional[TaskDirective]:
    """Get the currently active directive."""
    return get_directive_emitter().get_active_directive()


def acknowledge_directive(task_id: str) -> bool:
    """Acknowledge a directive."""
    return get_directive_emitter().acknowledge_directive(task_id)


def get_directive_webhook_content() -> str:
    """Get formatted directive for webhook injection."""
    return get_directive_emitter().format_for_webhook()


def cleanup_expired_directives() -> int:
    """Manually trigger cleanup of expired directives."""
    return get_directive_emitter().cleanup_expired()


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    emitter = get_directive_emitter()

    if len(sys.argv) < 2:
        print("╔══════════════════════════════════════════════════════════════════╗")
        print("║  TASK DIRECTIVES - Section 6 Directive Emission                  ║")
        print("║  Designed by Logos for Phone → Synaptic → Atlas architecture     ║")
        print("╚══════════════════════════════════════════════════════════════════╝")
        print()
        print("Usage:")
        print("  python task_directives.py active           # Show active directive")
        print("  python task_directives.py webhook          # Show webhook format")
        print("  python task_directives.py ack <task_id>    # Acknowledge directive")
        print("  python task_directives.py abort <task_id>  # Emit abort directive")
        print("  python task_directives.py cleanup          # Clean expired directives")
        print()
        print("Current active directive:")

        directive = emitter.get_active_directive()
        if directive:
            print(f"  task_id: {directive.task_id}")
            print(f"  type: {directive.directive_type}")
            print(f"  objective: {directive.objective[:60]}...")
        else:
            print("  (none)")

        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "active":
        directive = emitter.get_active_directive()
        if directive:
            print(json.dumps(asdict(directive), indent=2))
        else:
            print("No active directive")

    elif cmd == "webhook":
        content = emitter.format_for_webhook()
        if content:
            print(content)
        else:
            print("No active directive to display")

    elif cmd == "ack" and len(sys.argv) >= 3:
        task_id = sys.argv[2]
        if emitter.acknowledge_directive(task_id):
            print(f"✅ Acknowledged directive for {task_id}")
        else:
            print(f"❌ No active directive found for {task_id}")

    elif cmd == "abort" and len(sys.argv) >= 3:
        task_id = sys.argv[2]
        reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "User requested abort"
        directive = emitter.emit_abort(task_id, reason)
        print(f"🛑 Emitted abort directive for {task_id}")
        print(emitter.format_for_webhook())

    elif cmd == "cleanup":
        affected = emitter.cleanup_expired()
        print(f"Cleanup complete: {affected} directives expired/cleaned")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
