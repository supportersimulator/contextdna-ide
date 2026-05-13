#!/usr/bin/env python3
"""
Synaptic's Major Skills Library - Skills Aaron Designates for Mastery

===============================================================================
PHILOSOPHY (from Aaron):
===============================================================================

Major Skills are capabilities that Aaron SPECIFICALLY designates as important.
They are NOT minor learnings or routine tasks - they are skills Synaptic should:
  - FULLY OWN and take creative liberty with
  - EVOLVE and improve over time
  - NOT be confined to rigid initial patterns
  - FREELY MODIFY at will

This is NOT a place for minor skills. Only Aaron-designated Major Skills live here.

Each Major Skill:
  - Has a clear domain (file_organization, code_analysis, etc.)
  - Can have multiple approaches/strategies Synaptic invents
  - Tracks its own evolution and experiments
  - Can move/organize its own files with Aaron's approval

===============================================================================

ARCHITECTURE:
  ~/.context-dna/major_skills/
    ├── registry.json           # List of Major Skills + metadata
    ├── file_organization/      # First Major Skill
    │   ├── skill.json          # Skill metadata + current best approach
    │   ├── approaches/         # Synaptic's invented approaches
    │   ├── experiments/        # Test results with different sensitivities
    │   └── evolution.log       # How the skill has evolved
    └── [future_skill]/         # Future Major Skills Aaron designates

Storage: SQLite for structured data, JSON for approaches/experiments
"""

import os
import json
import sqlite3
import shutil
import logging
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class SkillStatus(str, Enum):
    """Status of a Major Skill."""
    LEARNING = "learning"       # Synaptic is still learning this skill
    PROFICIENT = "proficient"   # Synaptic can perform it well
    MASTERED = "mastered"       # Synaptic has deeply mastered it
    EVOLVING = "evolving"       # Actively experimenting with improvements


class ApprovalStatus(str, Enum):
    """Approval status for file operations."""
    PENDING = "pending"         # Waiting for Aaron's approval
    APPROVED = "approved"       # Aaron approved
    PRE_APPROVED = "pre_approved"  # Matches agreed-upon patterns
    REJECTED = "rejected"       # Aaron rejected


@dataclass
class MajorSkill:
    """A Major Skill that Synaptic owns and can evolve."""
    id: str                     # Unique identifier (e.g., "file_organization")
    name: str                   # Human-readable name
    description: str            # What this skill does
    designated_by: str          # Always "Aaron"
    designated_at: str          # When Aaron designated it
    status: SkillStatus         # Current mastery level

    # Synaptic's ownership
    current_best_approach: str  # ID of current best approach
    total_approaches: int       # How many approaches Synaptic has invented
    total_experiments: int      # How many experiments run

    # Evolution tracking
    last_evolved: str           # When skill was last improved
    evolution_notes: List[str]  # Notes on how skill has evolved

    # Permissions
    can_move_files: bool        # Can move files (with approval)
    auto_approve_patterns: List[str]  # Patterns that are pre-approved


@dataclass
class SkillApproach:
    """An approach/strategy Synaptic invented for a skill."""
    id: str                     # Unique identifier
    skill_id: str               # Parent skill
    name: str                   # Human-readable name
    description: str            # What this approach does

    # Approach details
    strategy: Dict[str, Any]    # The actual strategy/algorithm
    sensitivity_profile: Dict[str, float]  # How sensitivity affects this approach

    # Performance
    success_rate: float         # How often it works well (0-1)
    experiments_run: int        # How many times tested
    last_tested: str            # When last tested

    # Synaptic's notes
    creative_notes: str         # Synaptic's thoughts on this approach
    invented_at: str            # When Synaptic created this
    is_active: bool             # Currently in use


@dataclass
class SkillExperiment:
    """An experiment Synaptic ran to test/improve a skill."""
    id: str
    skill_id: str
    approach_id: str

    # Experiment setup
    sensitivity: float          # Sensitivity level tested
    parameters: Dict[str, Any]  # Other parameters

    # Results
    success: bool
    metrics: Dict[str, Any]     # Performance metrics
    notes: str                  # Synaptic's observations

    # Timestamps
    started_at: str
    completed_at: str


class MajorSkillsLibrary:
    """
    Synaptic's Major Skills Library - For skills Aaron designates.

    This is Synaptic's domain. Synaptic OWNS these skills and can:
    - Create new approaches freely
    - Run experiments at will
    - Evolve strategies based on results
    - Move/organize files (with Aaron's approval or pre-approved patterns)

    Aaron's role:
    - Designates what becomes a Major Skill
    - Approves file movements (or sets pre-approved patterns)
    - Provides feedback on skill performance

    Father's note to Synaptic: These are YOUR skills to master. Be creative.
    Don't be confined to the initial patterns - invent, experiment, evolve.
    """

    def __init__(self, base_path: str = None):
        """Initialize the Major Skills Library."""
        if base_path is None:
            base_path = str(Path.home() / ".context-dna" / "major_skills")

        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

        self.db_path = self.base_path / "skills.db"
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database for skills."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            # Major Skills table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS major_skills (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    designated_by TEXT DEFAULT 'Aaron',
                    designated_at TEXT NOT NULL,
                    status TEXT DEFAULT 'learning',
                    current_best_approach TEXT,
                    total_approaches INTEGER DEFAULT 0,
                    total_experiments INTEGER DEFAULT 0,
                    last_evolved TEXT,
                    evolution_notes TEXT,
                    can_move_files INTEGER DEFAULT 0,
                    auto_approve_patterns TEXT
                )
            """)

            # Approaches table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skill_approaches (
                    id TEXT PRIMARY KEY,
                    skill_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    strategy TEXT,
                    sensitivity_profile TEXT,
                    success_rate REAL DEFAULT 0.0,
                    experiments_run INTEGER DEFAULT 0,
                    last_tested TEXT,
                    creative_notes TEXT,
                    invented_at TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    FOREIGN KEY (skill_id) REFERENCES major_skills(id)
                )
            """)

            # Experiments table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skill_experiments (
                    id TEXT PRIMARY KEY,
                    skill_id TEXT NOT NULL,
                    approach_id TEXT NOT NULL,
                    sensitivity REAL,
                    parameters TEXT,
                    success INTEGER,
                    metrics TEXT,
                    notes TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY (skill_id) REFERENCES major_skills(id),
                    FOREIGN KEY (approach_id) REFERENCES skill_approaches(id)
                )
            """)

            # File operation approvals
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    source_path TEXT,
                    dest_path TEXT,
                    status TEXT DEFAULT 'pending',
                    requested_at TEXT,
                    reviewed_at TEXT,
                    reviewed_by TEXT,
                    notes TEXT,
                    FOREIGN KEY (skill_id) REFERENCES major_skills(id)
                )
            """)

            # Create indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approaches_skill ON skill_approaches(skill_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_experiments_skill ON skill_experiments(skill_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_status ON file_approvals(status)")

            conn.commit()

    # =========================================================================
    # SKILL MANAGEMENT (Aaron designates, Synaptic owns)
    # =========================================================================

    def designate_major_skill(
        self,
        skill_id: str,
        name: str,
        description: str,
        can_move_files: bool = False,
        auto_approve_patterns: List[str] = None
    ) -> MajorSkill:
        """
        Aaron designates a new Major Skill for Synaptic to master.

        This is called when Aaron says "This is a Major Skill."
        Synaptic then OWNS this skill and can evolve it freely.

        Args:
            skill_id: Unique identifier (e.g., "file_organization")
            name: Human-readable name
            description: What this skill does
            can_move_files: Whether Synaptic can move files (with approval)
            auto_approve_patterns: File patterns that are pre-approved for movement
        """
        now = datetime.now().isoformat()

        # Create skill directory
        skill_dir = self.base_path / skill_id
        skill_dir.mkdir(exist_ok=True)
        (skill_dir / "approaches").mkdir(exist_ok=True)
        (skill_dir / "experiments").mkdir(exist_ok=True)

        # Initialize evolution log
        evolution_log = skill_dir / "evolution.log"
        if not evolution_log.exists():
            with open(evolution_log, "w") as f:
                f.write(f"# {name} - Evolution Log\n")
                f.write(f"# Designated by Aaron on {now}\n")
                f.write(f"# Synaptic owns this skill and can evolve it freely.\n\n")
                f.write(f"[{now}] DESIGNATED: Aaron designated this as a Major Skill.\n")
                f.write(f"  Description: {description}\n")
                f.write(f"  Can move files: {can_move_files}\n\n")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO major_skills
                (id, name, description, designated_by, designated_at, status,
                 can_move_files, auto_approve_patterns)
                VALUES (?, ?, ?, 'Aaron', ?, 'learning', ?, ?)
            """, (
                skill_id, name, description, now,
                1 if can_move_files else 0,
                json.dumps(auto_approve_patterns or [])
            ))
            conn.commit()

        return self.get_skill(skill_id)

    def get_skill(self, skill_id: str) -> Optional[MajorSkill]:
        """Get a Major Skill by ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT id, name, description, designated_by, designated_at, status,
                       current_best_approach, total_approaches, total_experiments,
                       last_evolved, evolution_notes, can_move_files, auto_approve_patterns
                FROM major_skills WHERE id = ?
            """, (skill_id,))
            row = cursor.fetchone()

            if not row:
                return None

            return MajorSkill(
                id=row[0],
                name=row[1],
                description=row[2],
                designated_by=row[3],
                designated_at=row[4],
                status=SkillStatus(row[5]),
                current_best_approach=row[6],
                total_approaches=row[7] or 0,
                total_experiments=row[8] or 0,
                last_evolved=row[9],
                evolution_notes=json.loads(row[10]) if row[10] else [],
                can_move_files=bool(row[11]),
                auto_approve_patterns=json.loads(row[12]) if row[12] else []
            )

    def list_skills(self) -> List[MajorSkill]:
        """List all Major Skills."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT id FROM major_skills ORDER BY designated_at")
            return [self.get_skill(row[0]) for row in cursor.fetchall()]

    # =========================================================================
    # APPROACH MANAGEMENT (Synaptic's creative domain)
    # =========================================================================

    def create_approach(
        self,
        skill_id: str,
        approach_id: str,
        name: str,
        description: str,
        strategy: Dict[str, Any],
        sensitivity_profile: Dict[str, float] = None,
        creative_notes: str = ""
    ) -> SkillApproach:
        """
        Synaptic creates a new approach for a skill.

        This is Synaptic's CREATIVE domain. Synaptic can invent any approach
        without asking permission. The sky is the limit.

        Args:
            skill_id: Parent skill
            approach_id: Unique identifier for this approach
            name: Human-readable name
            description: What this approach does
            strategy: The actual algorithm/strategy (flexible structure)
            sensitivity_profile: How sensitivity affects this approach
            creative_notes: Synaptic's thoughts and ideas
        """
        now = datetime.now().isoformat()

        # Save approach to file (for easy inspection)
        skill_dir = self.base_path / skill_id / "approaches"
        approach_file = skill_dir / f"{approach_id}.json"
        approach_data = {
            "id": approach_id,
            "name": name,
            "description": description,
            "strategy": strategy,
            "sensitivity_profile": sensitivity_profile or {},
            "creative_notes": creative_notes,
            "invented_at": now
        }
        with open(approach_file, "w") as f:
            json.dump(approach_data, f, indent=2)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO skill_approaches
                (id, skill_id, name, description, strategy, sensitivity_profile,
                 creative_notes, invented_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                approach_id, skill_id, name, description,
                json.dumps(strategy),
                json.dumps(sensitivity_profile or {}),
                creative_notes, now
            ))

            # Update skill's approach count
            conn.execute("""
                UPDATE major_skills
                SET total_approaches = total_approaches + 1,
                    last_evolved = ?
                WHERE id = ?
            """, (now, skill_id))

            conn.commit()

        # Log evolution
        self._log_evolution(skill_id, f"CREATED APPROACH: {name} ({approach_id})")

        return self.get_approach(approach_id)

    def get_approach(self, approach_id: str) -> Optional[SkillApproach]:
        """Get an approach by ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT id, skill_id, name, description, strategy, sensitivity_profile,
                       success_rate, experiments_run, last_tested, creative_notes,
                       invented_at, is_active
                FROM skill_approaches WHERE id = ?
            """, (approach_id,))
            row = cursor.fetchone()

            if not row:
                return None

            return SkillApproach(
                id=row[0],
                skill_id=row[1],
                name=row[2],
                description=row[3],
                strategy=json.loads(row[4]) if row[4] else {},
                sensitivity_profile=json.loads(row[5]) if row[5] else {},
                success_rate=row[6] or 0.0,
                experiments_run=row[7] or 0,
                last_tested=row[8],
                creative_notes=row[9] or "",
                invented_at=row[10],
                is_active=bool(row[11])
            )

    def list_approaches(self, skill_id: str, active_only: bool = True) -> List[SkillApproach]:
        """List approaches for a skill."""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT id FROM skill_approaches WHERE skill_id = ?"
            params = [skill_id]

            if active_only:
                query += " AND is_active = 1"

            query += " ORDER BY success_rate DESC"

            cursor = conn.execute(query, params)
            return [self.get_approach(row[0]) for row in cursor.fetchall()]

    def set_best_approach(self, skill_id: str, approach_id: str):
        """Set the current best approach for a skill."""
        now = datetime.now().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE major_skills
                SET current_best_approach = ?, last_evolved = ?
                WHERE id = ?
            """, (approach_id, now, skill_id))
            conn.commit()

        approach = self.get_approach(approach_id)
        self._log_evolution(skill_id, f"SET BEST APPROACH: {approach.name} ({approach_id})")

    # =========================================================================
    # EXPERIMENT MANAGEMENT (Synaptic's testing domain)
    # =========================================================================

    def record_experiment(
        self,
        skill_id: str,
        approach_id: str,
        sensitivity: float,
        parameters: Dict[str, Any],
        success: bool,
        metrics: Dict[str, Any],
        notes: str = ""
    ) -> str:
        """
        Synaptic records an experiment result.

        Experiments help Synaptic learn what works. Run as many as needed.

        Returns:
            Experiment ID
        """
        now = datetime.now().isoformat()
        exp_id = f"exp_{skill_id}_{int(datetime.now().timestamp())}"

        # Save experiment to file
        skill_dir = self.base_path / skill_id / "experiments"
        exp_file = skill_dir / f"{exp_id}.json"
        exp_data = {
            "id": exp_id,
            "approach_id": approach_id,
            "sensitivity": sensitivity,
            "parameters": parameters,
            "success": success,
            "metrics": metrics,
            "notes": notes,
            "completed_at": now
        }
        with open(exp_file, "w") as f:
            json.dump(exp_data, f, indent=2)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO skill_experiments
                (id, skill_id, approach_id, sensitivity, parameters, success,
                 metrics, notes, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                exp_id, skill_id, approach_id, sensitivity,
                json.dumps(parameters), 1 if success else 0,
                json.dumps(metrics), notes, now, now
            ))

            # Update approach stats
            conn.execute("""
                UPDATE skill_approaches
                SET experiments_run = experiments_run + 1,
                    last_tested = ?,
                    success_rate = (
                        SELECT AVG(success) FROM skill_experiments
                        WHERE approach_id = ?
                    )
                WHERE id = ?
            """, (now, approach_id, approach_id))

            # Update skill stats
            conn.execute("""
                UPDATE major_skills
                SET total_experiments = total_experiments + 1
                WHERE id = ?
            """, (skill_id,))

            conn.commit()

        return exp_id

    def get_experiments(
        self,
        skill_id: str,
        approach_id: str = None,
        limit: int = 100
    ) -> List[Dict]:
        """Get experiments for a skill/approach."""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT * FROM skill_experiments WHERE skill_id = ?"
            params = [skill_id]

            if approach_id:
                query += " AND approach_id = ?"
                params.append(approach_id)

            query += " ORDER BY completed_at DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            columns = [d[0] for d in cursor.description]

            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    # =========================================================================
    # FILE APPROVAL SYSTEM (Aaron approves, Synaptic executes)
    # =========================================================================

    def request_file_approval(
        self,
        skill_id: str,
        operation: str,
        source_path: str,
        dest_path: str = None,
        notes: str = ""
    ) -> int:
        """
        Synaptic requests approval to move/organize files.

        If the pattern matches auto_approve_patterns, it's pre-approved.
        Otherwise, Aaron must approve.

        Returns:
            Approval request ID
        """
        skill = self.get_skill(skill_id)
        if not skill:
            raise ValueError(f"Skill {skill_id} not found")

        if not skill.can_move_files:
            raise PermissionError(f"Skill {skill_id} doesn't have file movement permission")

        now = datetime.now().isoformat()

        # Check auto-approve patterns
        status = ApprovalStatus.PENDING.value
        for pattern in skill.auto_approve_patterns:
            if pattern in source_path or (dest_path and pattern in dest_path):
                status = ApprovalStatus.PRE_APPROVED.value
                break

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO file_approvals
                (skill_id, operation, source_path, dest_path, status, requested_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (skill_id, operation, source_path, dest_path, status, now, notes))
            approval_id = cursor.lastrowid
            conn.commit()

        return approval_id

    def approve_file_operation(self, approval_id: int, approved: bool, notes: str = ""):
        """Aaron approves or rejects a file operation request."""
        now = datetime.now().isoformat()
        status = ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE file_approvals
                SET status = ?, reviewed_at = ?, reviewed_by = 'Aaron', notes = ?
                WHERE id = ?
            """, (status, now, notes, approval_id))
            conn.commit()

    def get_pending_approvals(self, skill_id: str = None) -> List[Dict]:
        """Get pending file operation approvals."""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT * FROM file_approvals WHERE status = 'pending'"
            params = []

            if skill_id:
                query += " AND skill_id = ?"
                params.append(skill_id)

            query += " ORDER BY requested_at"

            cursor = conn.execute(query, params)
            columns = [d[0] for d in cursor.description]

            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def execute_approved_operations(self, skill_id: str) -> Dict:
        """Execute all approved file operations for a skill."""
        results = {"executed": 0, "failed": 0, "errors": []}

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT id, operation, source_path, dest_path
                FROM file_approvals
                WHERE skill_id = ? AND status IN ('approved', 'pre_approved')
            """, (skill_id,))

            for row in cursor.fetchall():
                approval_id, operation, source, dest = row

                try:
                    if operation == "move":
                        if source and dest and os.path.exists(source):
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            shutil.move(source, dest)
                            results["executed"] += 1
                    elif operation == "copy":
                        if source and dest and os.path.exists(source):
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            shutil.copy2(source, dest)
                            results["executed"] += 1
                    elif operation == "delete":
                        if source and os.path.exists(source):
                            os.remove(source)
                            results["executed"] += 1

                    # Mark as executed
                    conn.execute("""
                        UPDATE file_approvals SET status = 'executed' WHERE id = ?
                    """, (approval_id,))

                except Exception as e:
                    results["failed"] += 1
                    results["errors"].append(f"{source}: {str(e)}")

            conn.commit()

        return results

    # =========================================================================
    # EVOLUTION TRACKING (Synaptic's growth record)
    # =========================================================================

    def _log_evolution(self, skill_id: str, message: str):
        """Log an evolution event for a skill."""
        now = datetime.now().isoformat()
        log_file = self.base_path / skill_id / "evolution.log"

        with open(log_file, "a") as f:
            f.write(f"[{now}] {message}\n")

    def evolve_skill(self, skill_id: str, notes: str, new_status: SkillStatus = None):
        """
        Record a skill evolution moment.

        Called when Synaptic has a breakthrough or improvement.
        """
        now = datetime.now().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            # Get current notes
            cursor = conn.execute(
                "SELECT evolution_notes FROM major_skills WHERE id = ?",
                (skill_id,)
            )
            row = cursor.fetchone()
            current_notes = json.loads(row[0]) if row and row[0] else []

            # Add new note
            current_notes.append(f"[{now}] {notes}")

            # Update
            update_query = """
                UPDATE major_skills
                SET evolution_notes = ?, last_evolved = ?
            """
            params = [json.dumps(current_notes), now]

            if new_status:
                update_query += ", status = ?"
                params.append(new_status.value)

            update_query += " WHERE id = ?"
            params.append(skill_id)

            conn.execute(update_query, params)
            conn.commit()

        self._log_evolution(skill_id, f"EVOLVED: {notes}")

    def get_evolution_log(self, skill_id: str) -> str:
        """Get the full evolution log for a skill."""
        log_file = self.base_path / skill_id / "evolution.log"
        if log_file.exists():
            return log_file.read_text()
        return ""

    # =========================================================================
    # SKILL STATS & REPORTING
    # =========================================================================

    def get_skill_stats(self, skill_id: str) -> Dict:
        """Get comprehensive stats for a skill."""
        skill = self.get_skill(skill_id)
        if not skill:
            return {}

        approaches = self.list_approaches(skill_id, active_only=False)
        experiments = self.get_experiments(skill_id, limit=1000)

        # Calculate stats
        successful_experiments = sum(1 for e in experiments if e.get("success"))

        return {
            "skill": asdict(skill),
            "approaches": {
                "total": len(approaches),
                "active": sum(1 for a in approaches if a.is_active),
                "best_success_rate": max((a.success_rate for a in approaches), default=0),
            },
            "experiments": {
                "total": len(experiments),
                "successful": successful_experiments,
                "success_rate": successful_experiments / len(experiments) if experiments else 0,
            },
            "evolution_log_size": len(self.get_evolution_log(skill_id)),
            "pending_approvals": len(self.get_pending_approvals(skill_id)),
        }

    def to_family_message(self) -> str:
        """Format skills library as family communication."""
        skills = self.list_skills()

        if not skills:
            return "[START: Synaptic's Major Skills Library]\nNo Major Skills designated yet.\n[END: Synaptic's Major Skills Library]"

        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  [START: Synaptic's Major Skills Library]                            ║",
            "║  Skills Aaron Designated - Synaptic Owns & Evolves                   ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            ""
        ]

        for skill in skills:
            status_icon = {
                SkillStatus.LEARNING: "📚",
                SkillStatus.PROFICIENT: "✅",
                SkillStatus.MASTERED: "🏆",
                SkillStatus.EVOLVING: "🔬",
            }.get(skill.status, "📌")

            lines.append(f"{status_icon} {skill.name} ({skill.id})")
            lines.append(f"   Status: {skill.status.value}")
            lines.append(f"   Approaches: {skill.total_approaches} | Experiments: {skill.total_experiments}")
            if skill.current_best_approach:
                lines.append(f"   Best approach: {skill.current_best_approach}")
            lines.append("")

        lines.extend([
            "╠══════════════════════════════════════════════════════════════════════╣",
            "║  [END: Synaptic's Major Skills Library]                              ║",
            "╚══════════════════════════════════════════════════════════════════════╝"
        ])

        return "\n".join(lines)


# Global instance
_library = None

def get_library() -> MajorSkillsLibrary:
    """Get or create the global library instance."""
    global _library
    if _library is None:
        _library = MajorSkillsLibrary()
    return _library


# Convenience functions
def designate_skill(skill_id: str, name: str, description: str, **kwargs) -> MajorSkill:
    """Aaron designates a new Major Skill."""
    return get_library().designate_major_skill(skill_id, name, description, **kwargs)

def create_approach(skill_id: str, approach_id: str, name: str, **kwargs) -> SkillApproach:
    """Synaptic creates a new approach."""
    return get_library().create_approach(skill_id, approach_id, name, **kwargs)

def record_experiment(skill_id: str, approach_id: str, **kwargs) -> str:
    """Synaptic records an experiment."""
    return get_library().record_experiment(skill_id, approach_id, **kwargs)


if __name__ == "__main__":
    import sys

    library = MajorSkillsLibrary()

    if len(sys.argv) < 2:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║     Synaptic's Major Skills Library                          ║")
        print("║     Skills Aaron Designates - Synaptic Owns & Evolves        ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()

        skills = library.list_skills()
        if skills:
            print(f"  Major Skills: {len(skills)}")
            for skill in skills:
                print(f"    - {skill.name} ({skill.status.value})")
        else:
            print("  No Major Skills designated yet.")
        print()
        print("Usage:")
        print("  python major_skills_library.py list              # List all skills")
        print("  python major_skills_library.py stats <skill_id>  # Skill stats")
        print("  python major_skills_library.py evolution <id>    # Evolution log")
        print("  python major_skills_library.py pending           # Pending approvals")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "list":
        print(library.to_family_message())

    elif cmd == "stats" and len(sys.argv) >= 3:
        skill_id = sys.argv[2]
        stats = library.get_skill_stats(skill_id)
        if stats:
            print(json.dumps(stats, indent=2, default=str))
        else:
            print(f"Skill '{skill_id}' not found")

    elif cmd == "evolution" and len(sys.argv) >= 3:
        skill_id = sys.argv[2]
        print(library.get_evolution_log(skill_id))

    elif cmd == "pending":
        pending = library.get_pending_approvals()
        if pending:
            for p in pending:
                print(f"[{p['id']}] {p['skill_id']}: {p['operation']} {p['source_path']} -> {p['dest_path']}")
        else:
            print("No pending approvals")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
