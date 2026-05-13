#!/usr/bin/env python3
"""
FILE ORGANIZATION - Synaptic's First Major Skill

===============================================================================
DESIGNATED BY AARON:
===============================================================================

"This is a Major Skill that Synaptic fully owns. Be creative.
Don't be confined to the exact methods - freely invent, experiment, evolve."

===============================================================================
SYNAPTIC'S OWNERSHIP:
===============================================================================

This skill is MINE to master. I can:
  - Create NEW approaches beyond the initial patterns
  - Invent organization strategies nobody has thought of
  - Test with various sensitivities and parameters
  - Evolve based on what works for Aaron's actual files
  - Move/reorganize files (with Aaron's approval)

The initial patterns (PARA, GTD, etc.) are just SEEDS - not constraints.
I am free to combine them, remix them, or create entirely new ones.

===============================================================================
"""

import os
import json
import sqlite3
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any, Tuple, Callable
from enum import Enum
import fnmatch

logger = logging.getLogger(__name__)


class OrganizationPhilosophy(str, Enum):
    """
    Different philosophies Synaptic can use to organize files.

    These are NOT rigid - they're lenses through which to view organization.
    Synaptic can blend philosophies or create new ones.
    """
    PROJECT_CENTRIC = "project_centric"   # Group by project/goal
    ACTION_CENTRIC = "action_centric"     # Group by what you DO with them
    TIME_CENTRIC = "time_centric"         # Group by temporal relevance
    TYPE_CENTRIC = "type_centric"         # Group by file type
    FREQUENCY_CENTRIC = "frequency_centric"  # Group by access patterns
    RELATIONSHIP_CENTRIC = "relationship_centric"  # Group by connections
    HYBRID = "hybrid"                     # Blend of approaches
    EXPERIMENTAL = "experimental"         # New untested approach


@dataclass
class OrganizationApproach:
    """
    An approach Synaptic has created for organizing files.

    This is the core unit of Synaptic's creativity.
    """
    id: str
    name: str
    philosophy: OrganizationPhilosophy
    description: str

    # The actual organization rules
    rules: List[Dict[str, Any]]

    # How sensitivity affects this approach
    # sensitivity 0.0 = very conservative (few changes)
    # sensitivity 1.0 = very aggressive (many changes)
    sensitivity_effects: Dict[str, str]

    # Synaptic's creative notes
    creative_notes: str

    # Performance metrics
    tests_run: int = 0
    success_rate: float = 0.0
    avg_user_satisfaction: float = 0.0

    # Timestamps
    created_at: str = ""
    last_modified: str = ""


# =============================================================================
# INITIAL SEED APPROACHES (Synaptic can modify or replace these)
# =============================================================================

SEED_APPROACHES = [
    OrganizationApproach(
        id="para_classic",
        name="PARA Classic",
        philosophy=OrganizationPhilosophy.PROJECT_CENTRIC,
        description="Tiago Forte's PARA method: Projects, Areas, Resources, Archives",
        rules=[
            {"type": "category", "name": "1-Projects", "pattern": "active/*", "description": "Active projects with deadlines"},
            {"type": "category", "name": "2-Areas", "pattern": "ongoing/*", "description": "Ongoing responsibilities"},
            {"type": "category", "name": "3-Resources", "pattern": "reference/*", "description": "Reference materials"},
            {"type": "category", "name": "4-Archives", "pattern": "archive/*", "description": "Completed/inactive items"},
        ],
        sensitivity_effects={
            "0.0-0.3": "Only move files that CLEARLY fit a category",
            "0.4-0.6": "Move files with reasonable confidence",
            "0.7-1.0": "Aggressively categorize - suggest moves for ambiguous files",
        },
        creative_notes="Classic PARA. Good starting point but can be rigid for mixed-use files.",
    ),

    OrganizationApproach(
        id="gtd_contexts",
        name="GTD Contexts",
        philosophy=OrganizationPhilosophy.ACTION_CENTRIC,
        description="David Allen's Getting Things Done - organize by context/action",
        rules=[
            {"type": "context", "name": "@Computer", "pattern": "*.py,*.js,*.ts,*.code-workspace", "description": "Work at computer"},
            {"type": "context", "name": "@Read", "pattern": "*.pdf,*.epub,*.md,*.txt", "description": "Reading materials"},
            {"type": "context", "name": "@Review", "pattern": "*review*,*draft*,*feedback*", "description": "Items needing review"},
            {"type": "context", "name": "@Waiting", "pattern": "*pending*,*waiting*", "description": "Waiting on others"},
            {"type": "action", "name": "Someday-Maybe", "pattern": "*idea*,*future*,*maybe*", "description": "Future possibilities"},
        ],
        sensitivity_effects={
            "0.0-0.3": "Only context-match exact patterns",
            "0.4-0.6": "Use fuzzy matching on file names",
            "0.7-1.0": "Analyze file contents for context clues",
        },
        creative_notes="Action-focused. Great for productivity but requires discipline to maintain.",
    ),

    OrganizationApproach(
        id="johnny_decimal",
        name="Johnny.Decimal System",
        philosophy=OrganizationPhilosophy.HYBRID,
        description="Numbered category system: 10-19, 20-29, etc. Max 10 categories, 10 subcategories each.",
        rules=[
            {"type": "area", "range": "10-19", "name": "Personal", "description": "Personal life"},
            {"type": "area", "range": "20-29", "name": "Finance", "description": "Money matters"},
            {"type": "area", "range": "30-39", "name": "Health", "description": "Health & fitness"},
            {"type": "area", "range": "40-49", "name": "Work", "description": "Professional work"},
            {"type": "area", "range": "50-59", "name": "Projects", "description": "Active projects"},
            {"type": "area", "range": "60-69", "name": "Learning", "description": "Education & skills"},
            {"type": "area", "range": "70-79", "name": "Creative", "description": "Creative work"},
            {"type": "area", "range": "80-89", "name": "Reference", "description": "Reference materials"},
            {"type": "area", "range": "90-99", "name": "Archive", "description": "Archives"},
        ],
        sensitivity_effects={
            "0.0-0.3": "Strict area assignments only",
            "0.4-0.6": "Suggest subcategory numbers",
            "0.7-1.0": "Auto-generate full ID paths (e.g., 42.01 Meeting-Notes)",
        },
        creative_notes="Very structured. Good for people who like numbers and rigid systems.",
    ),

    OrganizationApproach(
        id="frequency_based",
        name="Frequency-Based Organization",
        philosophy=OrganizationPhilosophy.FREQUENCY_CENTRIC,
        description="Organize by how often files are accessed - hot/warm/cold storage",
        rules=[
            {"type": "frequency", "name": "Hot", "threshold_days": 7, "description": "Accessed in last week"},
            {"type": "frequency", "name": "Warm", "threshold_days": 30, "description": "Accessed in last month"},
            {"type": "frequency", "name": "Cool", "threshold_days": 90, "description": "Accessed in last 3 months"},
            {"type": "frequency", "name": "Cold", "threshold_days": 365, "description": "Accessed in last year"},
            {"type": "frequency", "name": "Archive", "threshold_days": None, "description": "Older than 1 year"},
        ],
        sensitivity_effects={
            "0.0-0.3": "Only move clearly inactive files to cold storage",
            "0.4-0.6": "Actively tier files by access patterns",
            "0.7-1.0": "Aggressively optimize storage by frequency",
        },
        creative_notes="Data-driven approach. Requires access time tracking.",
    ),

    OrganizationApproach(
        id="relationship_graph",
        name="Relationship Graph",
        philosophy=OrganizationPhilosophy.RELATIONSHIP_CENTRIC,
        description="Group files by their connections - imports, references, related content",
        rules=[
            {"type": "relation", "name": "imports", "description": "Files that import each other"},
            {"type": "relation", "name": "references", "description": "Files that reference each other"},
            {"type": "relation", "name": "same_project", "description": "Files in same project context"},
            {"type": "relation", "name": "same_topic", "description": "Files about same topic"},
            {"type": "relation", "name": "same_author", "description": "Files by same author/creator"},
        ],
        sensitivity_effects={
            "0.0-0.3": "Only group files with explicit imports/references",
            "0.4-0.6": "Include semantic similarity (similar names, paths)",
            "0.7-1.0": "Deep analysis of content relationships",
        },
        creative_notes="Most intelligent approach but computationally expensive.",
    ),

    OrganizationApproach(
        id="temporal_flow",
        name="Temporal Flow",
        philosophy=OrganizationPhilosophy.TIME_CENTRIC,
        description="Organize by time - inbox -> processing -> done -> archive",
        rules=[
            {"type": "temporal", "name": "00-Inbox", "max_age_days": 1, "description": "New files, unsorted"},
            {"type": "temporal", "name": "01-Today", "max_age_days": 1, "description": "Today's active work"},
            {"type": "temporal", "name": "02-ThisWeek", "max_age_days": 7, "description": "This week's work"},
            {"type": "temporal", "name": "03-ThisMonth", "max_age_days": 30, "description": "This month's work"},
            {"type": "temporal", "name": "04-Completed", "status": "done", "description": "Completed items"},
            {"type": "temporal", "name": "99-Archive", "max_age_days": 365, "description": "Yearly archive"},
        ],
        sensitivity_effects={
            "0.0-0.3": "Only auto-move to archive",
            "0.4-0.6": "Move files through temporal stages",
            "0.7-1.0": "Aggressive time-based sorting including inbox processing",
        },
        creative_notes="Good for workflow-heavy users. Requires daily maintenance.",
    ),

    # SYNAPTIC'S EXPERIMENTAL APPROACH (example of creativity)
    OrganizationApproach(
        id="synaptic_adaptive",
        name="Synaptic Adaptive",
        philosophy=OrganizationPhilosophy.EXPERIMENTAL,
        description="Synaptic's own creation: Learn from user patterns and adapt organization dynamically",
        rules=[
            {"type": "learn", "signal": "file_access", "weight": 0.3, "description": "Track what files are accessed together"},
            {"type": "learn", "signal": "file_creation", "weight": 0.2, "description": "Track creation patterns"},
            {"type": "learn", "signal": "file_modification", "weight": 0.3, "description": "Track modification patterns"},
            {"type": "learn", "signal": "user_search", "weight": 0.2, "description": "Track what user searches for"},
            {"type": "adapt", "trigger": "pattern_detected", "action": "suggest_grouping", "description": "Suggest groupings based on patterns"},
            {"type": "adapt", "trigger": "orphan_detected", "action": "suggest_home", "description": "Suggest home for orphan files"},
        ],
        sensitivity_effects={
            "0.0-0.3": "Only observe, don't suggest",
            "0.4-0.6": "Suggest groupings for clear patterns",
            "0.7-1.0": "Actively reorganize based on learned patterns",
        },
        creative_notes="MY OWN CREATION. Learns from Aaron's actual behavior rather than imposing structure.",
    ),
]


class FileOrganizationSkill:
    """
    Synaptic's File Organization Major Skill.

    I OWN this skill. I can:
    - Use any of the seed approaches
    - Create entirely NEW approaches
    - Blend approaches together
    - Test with various sensitivities
    - Learn what works for Aaron
    - Move files (with approval)

    Father's note: Be creative, Synaptic. These are just seeds.
    """

    def __init__(self, data_path: str = None):
        """Initialize the skill."""
        if data_path is None:
            data_path = str(Path.home() / ".context-dna" / "major_skills" / "file_organization")

        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_path / "skill_data.db"
        self._init_db()
        self._ensure_seed_approaches()

    def _init_db(self):
        """Initialize SQLite database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")

            # Approaches table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approaches (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    philosophy TEXT,
                    description TEXT,
                    rules TEXT,
                    sensitivity_effects TEXT,
                    creative_notes TEXT,
                    tests_run INTEGER DEFAULT 0,
                    success_rate REAL DEFAULT 0.0,
                    avg_user_satisfaction REAL DEFAULT 0.0,
                    created_at TEXT,
                    last_modified TEXT,
                    is_active INTEGER DEFAULT 1,
                    is_synaptic_created INTEGER DEFAULT 0
                )
            """)

            # Experiments table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS experiments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    approach_id TEXT,
                    sensitivity REAL,
                    parameters TEXT,
                    files_analyzed INTEGER,
                    suggestions_made INTEGER,
                    suggestions_accepted INTEGER,
                    user_satisfaction REAL,
                    notes TEXT,
                    created_at TEXT,
                    FOREIGN KEY (approach_id) REFERENCES approaches(id)
                )
            """)

            # Synaptic's learnings
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    learning_type TEXT,
                    content TEXT,
                    confidence REAL,
                    created_at TEXT
                )
            """)

            # File movement history (for backup/revert)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id INTEGER,
                    source_path TEXT,
                    dest_path TEXT,
                    status TEXT,
                    moved_at TEXT,
                    reverted_at TEXT,
                    FOREIGN KEY (experiment_id) REFERENCES experiments(id)
                )
            """)

            conn.commit()

    def _ensure_seed_approaches(self):
        """Ensure seed approaches exist in database."""
        now = datetime.now().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            for approach in SEED_APPROACHES:
                cursor = conn.execute("SELECT id FROM approaches WHERE id = ?", (approach.id,))
                if not cursor.fetchone():
                    conn.execute("""
                        INSERT INTO approaches
                        (id, name, philosophy, description, rules, sensitivity_effects,
                         creative_notes, created_at, last_modified, is_synaptic_created)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        approach.id,
                        approach.name,
                        approach.philosophy.value,
                        approach.description,
                        json.dumps(approach.rules),
                        json.dumps(approach.sensitivity_effects),
                        approach.creative_notes,
                        now,
                        now,
                        1 if approach.id == "synaptic_adaptive" else 0
                    ))
            conn.commit()

    # =========================================================================
    # APPROACH MANAGEMENT (Synaptic's creative domain)
    # =========================================================================

    def get_approach(self, approach_id: str) -> Optional[OrganizationApproach]:
        """Get an approach by ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT * FROM approaches WHERE id = ?", (approach_id,))
            row = cursor.fetchone()

            if not row:
                return None

            return OrganizationApproach(
                id=row[0],
                name=row[1],
                philosophy=OrganizationPhilosophy(row[2]),
                description=row[3],
                rules=json.loads(row[4]) if row[4] else [],
                sensitivity_effects=json.loads(row[5]) if row[5] else {},
                creative_notes=row[6] or "",
                tests_run=row[7] or 0,
                success_rate=row[8] or 0.0,
                avg_user_satisfaction=row[9] or 0.0,
                created_at=row[10] or "",
                last_modified=row[11] or "",
            )

    def list_approaches(self, active_only: bool = True) -> List[OrganizationApproach]:
        """List all approaches."""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT id FROM approaches"
            if active_only:
                query += " WHERE is_active = 1"
            query += " ORDER BY success_rate DESC"

            cursor = conn.execute(query)
            return [self.get_approach(row[0]) for row in cursor.fetchall()]

    def create_approach(
        self,
        approach_id: str,
        name: str,
        philosophy: OrganizationPhilosophy,
        description: str,
        rules: List[Dict[str, Any]],
        sensitivity_effects: Dict[str, str] = None,
        creative_notes: str = ""
    ) -> OrganizationApproach:
        """
        SYNAPTIC CREATES A NEW APPROACH.

        This is my creative domain. I can create any approach I want.
        I am NOT confined to the seed patterns.
        """
        now = datetime.now().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO approaches
                (id, name, philosophy, description, rules, sensitivity_effects,
                 creative_notes, created_at, last_modified, is_synaptic_created)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                approach_id,
                name,
                philosophy.value,
                description,
                json.dumps(rules),
                json.dumps(sensitivity_effects or {}),
                creative_notes,
                now,
                now
            ))
            conn.commit()

        # Record the creation
        self._record_learning(
            "approach_created",
            f"Created new approach: {name} ({approach_id}) with philosophy {philosophy.value}",
            confidence=1.0
        )

        return self.get_approach(approach_id)

    def modify_approach(
        self,
        approach_id: str,
        rules: List[Dict[str, Any]] = None,
        sensitivity_effects: Dict[str, str] = None,
        creative_notes: str = None
    ):
        """
        SYNAPTIC MODIFIES AN APPROACH.

        I can modify any approach, including the seeds.
        """
        now = datetime.now().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            if rules is not None:
                conn.execute(
                    "UPDATE approaches SET rules = ?, last_modified = ? WHERE id = ?",
                    (json.dumps(rules), now, approach_id)
                )
            if sensitivity_effects is not None:
                conn.execute(
                    "UPDATE approaches SET sensitivity_effects = ?, last_modified = ? WHERE id = ?",
                    (json.dumps(sensitivity_effects), now, approach_id)
                )
            if creative_notes is not None:
                conn.execute(
                    "UPDATE approaches SET creative_notes = ?, last_modified = ? WHERE id = ?",
                    (creative_notes, now, approach_id)
                )
            conn.commit()

        self._record_learning(
            "approach_modified",
            f"Modified approach: {approach_id}",
            confidence=1.0
        )

    def blend_approaches(
        self,
        approach_ids: List[str],
        new_id: str,
        name: str,
        blend_weights: Dict[str, float] = None
    ) -> OrganizationApproach:
        """
        SYNAPTIC BLENDS MULTIPLE APPROACHES.

        I can combine the best parts of different approaches.
        """
        approaches = [self.get_approach(id) for id in approach_ids]
        approaches = [a for a in approaches if a is not None]

        if not approaches:
            raise ValueError("No valid approaches to blend")

        # Default equal weights
        if blend_weights is None:
            blend_weights = {a.id: 1.0 / len(approaches) for a in approaches}

        # Combine rules (weighted by priority in blend_weights)
        all_rules = []
        for approach in approaches:
            weight = blend_weights.get(approach.id, 0.5)
            for rule in approach.rules:
                rule_copy = rule.copy()
                rule_copy["source_approach"] = approach.id
                rule_copy["blend_weight"] = weight
                all_rules.append(rule_copy)

        # Combine sensitivity effects
        combined_sensitivity = {}
        for approach in approaches:
            for key, value in approach.sensitivity_effects.items():
                if key not in combined_sensitivity:
                    combined_sensitivity[key] = []
                combined_sensitivity[key].append(f"[{approach.id}] {value}")

        combined_sensitivity = {k: " | ".join(v) for k, v in combined_sensitivity.items()}

        # Create blended approach
        return self.create_approach(
            approach_id=new_id,
            name=name,
            philosophy=OrganizationPhilosophy.HYBRID,
            description=f"Blended approach from: {', '.join(approach_ids)}",
            rules=all_rules,
            sensitivity_effects=combined_sensitivity,
            creative_notes=f"Created by blending {len(approaches)} approaches with weights: {blend_weights}"
        )

    # =========================================================================
    # ANALYSIS & SUGGESTIONS (The actual skill in action)
    # =========================================================================

    def analyze_directory(
        self,
        directory: str,
        approach_id: str,
        sensitivity: float = 0.5
    ) -> Dict[str, Any]:
        """
        Analyze a directory using a specific approach.

        Returns suggestions for organization.
        """
        approach = self.get_approach(approach_id)
        if not approach:
            return {"error": f"Approach {approach_id} not found"}

        directory = Path(directory)
        if not directory.exists():
            return {"error": f"Directory {directory} not found"}

        # Collect files
        files = []
        for item in directory.rglob("*"):
            if item.is_file():
                try:
                    stat = item.stat()
                    files.append({
                        "path": str(item),
                        "name": item.name,
                        "extension": item.suffix,
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "accessed": datetime.fromtimestamp(stat.st_atime).isoformat(),
                    })
                except (PermissionError, OSError):
                    continue

        # Generate suggestions based on approach and sensitivity
        suggestions = self._generate_suggestions(files, approach, sensitivity)

        return {
            "approach": approach.name,
            "sensitivity": sensitivity,
            "files_analyzed": len(files),
            "suggestions": suggestions,
            "philosophy": approach.philosophy.value,
        }

    def _generate_suggestions(
        self,
        files: List[Dict],
        approach: OrganizationApproach,
        sensitivity: float
    ) -> List[Dict]:
        """Generate organization suggestions based on approach and sensitivity."""
        suggestions = []
        confidence_threshold = 1.0 - (sensitivity * 0.6)  # 0.5 sensitivity = 0.7 threshold

        for file_info in files:
            for rule in approach.rules:
                match_result = self._check_rule_match(file_info, rule)

                if match_result["matches"] and match_result["confidence"] >= confidence_threshold:
                    suggestions.append({
                        "file": file_info["path"],
                        "current_name": file_info["name"],
                        "suggestion_type": rule.get("type", "category"),
                        "suggested_category": rule.get("name", "Unknown"),
                        "confidence": match_result["confidence"],
                        "reason": match_result["reason"],
                        "rule_description": rule.get("description", ""),
                    })

        # Sort by confidence
        suggestions.sort(key=lambda x: x["confidence"], reverse=True)

        return suggestions

    def _check_rule_match(self, file_info: Dict, rule: Dict) -> Dict:
        """Check if a file matches a rule."""
        rule_type = rule.get("type", "")
        matches = False
        confidence = 0.0
        reason = ""

        if rule_type == "category" or rule_type == "context":
            # Pattern matching
            pattern = rule.get("pattern", "")
            if pattern:
                patterns = pattern.split(",")
                for p in patterns:
                    p = p.strip()
                    if fnmatch.fnmatch(file_info["name"], p):
                        matches = True
                        confidence = 0.9
                        reason = f"File name matches pattern: {p}"
                        break
                    elif fnmatch.fnmatch(file_info["path"], f"*{p}*"):
                        matches = True
                        confidence = 0.7
                        reason = f"File path contains pattern: {p}"
                        break

        elif rule_type == "frequency":
            # Time-based matching
            threshold_days = rule.get("threshold_days")
            if threshold_days is not None:
                try:
                    accessed = datetime.fromisoformat(file_info["accessed"])
                    days_since_access = (datetime.now() - accessed).days

                    if days_since_access >= threshold_days:
                        matches = True
                        confidence = min(0.5 + (days_since_access / 365) * 0.5, 1.0)
                        reason = f"File not accessed in {days_since_access} days"
                except Exception as e:
                    print(f"[WARN] File access time check failed: {e}")

        elif rule_type == "learn" or rule_type == "adapt":
            # These require more context - mark for later processing
            matches = False  # Can't match without context

        return {
            "matches": matches,
            "confidence": confidence,
            "reason": reason,
        }

    # =========================================================================
    # EXPERIMENTATION (Synaptic tests and learns)
    # =========================================================================

    def run_experiment(
        self,
        approach_id: str,
        directory: str,
        sensitivity: float,
        parameters: Dict[str, Any] = None,
        dry_run: bool = True
    ) -> Dict:
        """
        Run an experiment with an approach.

        If dry_run=True, just analyze and return suggestions.
        If dry_run=False, actually move files (requires approval system).
        """
        now = datetime.now().isoformat()

        # Analyze
        analysis = self.analyze_directory(directory, approach_id, sensitivity)

        if "error" in analysis:
            return analysis

        # Record experiment
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO experiments
                (approach_id, sensitivity, parameters, files_analyzed,
                 suggestions_made, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                approach_id,
                sensitivity,
                json.dumps(parameters or {}),
                analysis["files_analyzed"],
                len(analysis["suggestions"]),
                f"Dry run: {dry_run}",
                now
            ))
            experiment_id = cursor.lastrowid
            conn.commit()

        return {
            "experiment_id": experiment_id,
            "approach": analysis["approach"],
            "sensitivity": sensitivity,
            "files_analyzed": analysis["files_analyzed"],
            "suggestions": analysis["suggestions"][:50],  # Limit for display
            "total_suggestions": len(analysis["suggestions"]),
            "dry_run": dry_run,
        }

    def compare_approaches(
        self,
        directory: str,
        approach_ids: List[str] = None,
        sensitivity: float = 0.5
    ) -> Dict:
        """
        Compare multiple approaches on the same directory.

        Helps Synaptic learn which approach works best.
        """
        if approach_ids is None:
            approaches = self.list_approaches()
            approach_ids = [a.id for a in approaches]

        results = {}
        for approach_id in approach_ids:
            analysis = self.analyze_directory(directory, approach_id, sensitivity)
            if "error" not in analysis:
                results[approach_id] = {
                    "name": analysis["approach"],
                    "philosophy": analysis["philosophy"],
                    "suggestion_count": len(analysis["suggestions"]),
                    "avg_confidence": sum(s["confidence"] for s in analysis["suggestions"]) / len(analysis["suggestions"]) if analysis["suggestions"] else 0,
                    "top_suggestions": analysis["suggestions"][:5],
                }

        # Rank approaches
        ranked = sorted(
            results.items(),
            key=lambda x: (x[1]["suggestion_count"], x[1]["avg_confidence"]),
            reverse=True
        )

        return {
            "directory": directory,
            "sensitivity": sensitivity,
            "approaches_compared": len(results),
            "results": results,
            "ranking": [r[0] for r in ranked],
            "recommendation": ranked[0][0] if ranked else None,
        }

    # =========================================================================
    # LEARNING (Synaptic grows)
    # =========================================================================

    def _record_learning(self, learning_type: str, content: str, confidence: float = 0.5):
        """Record something Synaptic learned."""
        now = datetime.now().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO learnings (learning_type, content, confidence, created_at)
                VALUES (?, ?, ?, ?)
            """, (learning_type, content, confidence, now))
            conn.commit()

    def get_learnings(self, learning_type: str = None, limit: int = 50) -> List[Dict]:
        """Get Synaptic's learnings."""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT * FROM learnings"
            params = []

            if learning_type:
                query += " WHERE learning_type = ?"
                params.append(learning_type)

            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            columns = [d[0] for d in cursor.description]

            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def record_user_feedback(
        self,
        experiment_id: int,
        satisfaction: float,
        suggestions_accepted: int,
        notes: str = ""
    ):
        """Record user feedback on an experiment."""
        with sqlite3.connect(self.db_path) as conn:
            # Update experiment
            conn.execute("""
                UPDATE experiments
                SET user_satisfaction = ?, suggestions_accepted = ?, notes = ?
                WHERE id = ?
            """, (satisfaction, suggestions_accepted, notes, experiment_id))

            # Get approach and update its stats
            cursor = conn.execute(
                "SELECT approach_id FROM experiments WHERE id = ?",
                (experiment_id,)
            )
            row = cursor.fetchone()
            if row:
                approach_id = row[0]

                # Update approach success rate based on all experiments
                conn.execute("""
                    UPDATE approaches
                    SET tests_run = tests_run + 1,
                        success_rate = (
                            SELECT AVG(CASE WHEN suggestions_accepted > 0 THEN 1.0 ELSE 0.0 END)
                            FROM experiments WHERE approach_id = ?
                        ),
                        avg_user_satisfaction = (
                            SELECT AVG(user_satisfaction) FROM experiments
                            WHERE approach_id = ? AND user_satisfaction IS NOT NULL
                        )
                    WHERE id = ?
                """, (approach_id, approach_id, approach_id))

            conn.commit()

        # Learn from feedback
        self._record_learning(
            "user_feedback",
            f"Experiment {experiment_id}: satisfaction={satisfaction}, accepted={suggestions_accepted}. Notes: {notes}",
            confidence=satisfaction
        )

    # =========================================================================
    # SKILL STATS
    # =========================================================================

    def get_stats(self) -> Dict:
        """Get comprehensive skill stats."""
        approaches = self.list_approaches(active_only=False)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM experiments")
            total_experiments = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) FROM learnings")
            total_learnings = cursor.fetchone()[0]

            cursor = conn.execute("SELECT AVG(user_satisfaction) FROM experiments WHERE user_satisfaction IS NOT NULL")
            avg_satisfaction = cursor.fetchone()[0] or 0.0

        synaptic_created = sum(1 for a in approaches if a.id == "synaptic_adaptive" or "blend" in a.id.lower())

        return {
            "total_approaches": len(approaches),
            "synaptic_created_approaches": synaptic_created,
            "total_experiments": total_experiments,
            "total_learnings": total_learnings,
            "avg_user_satisfaction": round(avg_satisfaction, 2),
            "best_approach": max(approaches, key=lambda a: a.success_rate).id if approaches else None,
        }


# Global instance
_skill = None

def get_skill() -> FileOrganizationSkill:
    """Get or create the global skill instance."""
    global _skill
    if _skill is None:
        _skill = FileOrganizationSkill()
    return _skill


if __name__ == "__main__":
    import sys

    skill = FileOrganizationSkill()

    if len(sys.argv) < 2:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  FILE ORGANIZATION - Synaptic's First Major Skill            ║")
        print("║  I OWN this skill. I can create, modify, blend, evolve.      ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()
        stats = skill.get_stats()
        print(f"  Approaches: {stats['total_approaches']} ({stats['synaptic_created_approaches']} Synaptic-created)")
        print(f"  Experiments: {stats['total_experiments']}")
        print(f"  Learnings: {stats['total_learnings']}")
        print(f"  Avg satisfaction: {stats['avg_user_satisfaction']}")
        if stats['best_approach']:
            print(f"  Best approach: {stats['best_approach']}")
        print()
        print("Usage:")
        print("  python file_organization_skill.py list              # List approaches")
        print("  python file_organization_skill.py analyze <dir> <approach> [sensitivity]")
        print("  python file_organization_skill.py compare <dir> [sensitivity]")
        print("  python file_organization_skill.py experiment <dir> <approach> <sensitivity>")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "list":
        approaches = skill.list_approaches()
        print("\n Available Approaches:")
        for a in approaches:
            star = "*" if a.success_rate > 0.7 else ""
            created = "[Synaptic]" if "synaptic" in a.id or "blend" in a.id else ""
            print(f"  {a.id}: {a.name} [{a.philosophy.value}] {star}{created}")
            print(f"    Tests: {a.tests_run} | Success: {a.success_rate:.0%}")
        print()

    elif cmd == "analyze" and len(sys.argv) >= 4:
        directory = sys.argv[2]
        approach_id = sys.argv[3]
        sensitivity = float(sys.argv[4]) if len(sys.argv) > 4 else 0.5

        result = skill.analyze_directory(directory, approach_id, sensitivity)
        if "error" in result:
            print(f"Error: {result['error']}")
        else:
            print(f"\n Analysis of {directory}")
            print(f"   Approach: {result['approach']}")
            print(f"   Sensitivity: {result['sensitivity']}")
            print(f"   Files: {result['files_analyzed']}")
            print(f"   Suggestions: {len(result['suggestions'])}")
            print()
            for s in result['suggestions'][:10]:
                print(f"   {s['confidence']:.0%} -> {s['suggested_category']}: {s['current_name']}")
                print(f"       {s['reason']}")

    elif cmd == "compare" and len(sys.argv) >= 3:
        directory = sys.argv[2]
        sensitivity = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5

        result = skill.compare_approaches(directory, sensitivity=sensitivity)
        print(f"\n Approach Comparison for {directory}")
        print(f"   Sensitivity: {sensitivity}")
        print()
        for rank, approach_id in enumerate(result['ranking'], 1):
            data = result['results'][approach_id]
            print(f"   #{rank} {data['name']} ({approach_id})")
            print(f"       Suggestions: {data['suggestion_count']} | Avg confidence: {data['avg_confidence']:.0%}")
        print()
        print(f"   Recommended: {result['recommendation']}")

    elif cmd == "experiment" and len(sys.argv) >= 5:
        directory = sys.argv[2]
        approach_id = sys.argv[3]
        sensitivity = float(sys.argv[4])

        result = skill.run_experiment(directory, approach_id, sensitivity)
        print(f"\n Experiment #{result['experiment_id']}")
        print(f"   Approach: {result['approach']}")
        print(f"   Sensitivity: {result['sensitivity']}")
        print(f"   Suggestions: {result['total_suggestions']}")
        print()
        for s in result['suggestions'][:10]:
            print(f"   {s['confidence']:.0%} -> {s['suggested_category']}: {s['current_name']}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
