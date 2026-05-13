#!/usr/bin/env python3
"""
Context DNA Hierarchy Learner

Learns from various file organization patterns and adapts to different coding styles.
Especially designed for "vibe coders" - beginners without formal training.

Philosophy:
- No judgment on organization style - all patterns are valid
- Learn from every codebase encountered
- Build up knowledge of common patterns over time
- Help users understand their own organization style
- Suggest improvements without being prescriptive

Usage:
    from context_dna.setup.hierarchy_learner import HierarchyLearner

    learner = HierarchyLearner()
    learner.observe_and_learn(detected_projects)
    suggestions = learner.get_friendly_suggestions()
"""

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum

from context_dna.setup.hierarchy_analyzer import DetectedProject


# =============================================================================
# ORGANIZATION STYLE PROFILES
# =============================================================================

class OrganizationStyle(Enum):
    """
    Common organization styles we've learned.
    Each represents a valid way to structure code.
    """

    # Professional/Formal Styles
    MONOREPO = ("monorepo", "Single repo, multiple packages",
        "Common in enterprise, great for code sharing")
    MICROSERVICES = ("microservices", "Many small, independent services",
        "Good for teams, harder for solo devs")
    LAYERED = ("layered", "Frontend/Backend/Database layers",
        "Classic full-stack structure")

    # Creative/Indie Styles
    FLAT = ("flat", "Everything at root level",
        "Simple and quick to navigate - totally valid!")
    BY_FEATURE = ("by_feature", "Folders per feature, not per type",
        "Great for understanding what does what")
    CHAOS_CREATIVE = ("chaos_creative", "Organic growth, no strict rules",
        "Many successful projects start this way!")

    # Beginner-Friendly Styles
    TUTORIAL = ("tutorial", "Following a tutorial structure",
        "Great for learning - keep going!")
    EXPERIMENT = ("experiment", "Lots of test/try folders",
        "Perfect for learning by doing")
    COPY_PASTE = ("copy_paste", "Duplicated code, learning by example",
        "Normal part of learning - you'll refactor later")

    @property
    def name_id(self) -> str:
        return self.value[0]

    @property
    def label(self) -> str:
        return self.value[1]

    @property
    def encouragement(self) -> str:
        return self.value[2]


# =============================================================================
# LEARNED PATTERNS
# =============================================================================

@dataclass
class LearnedPattern:
    """A pattern learned from observing codebases."""
    pattern_id: str
    name: str
    description: str
    markers: List[str]          # File/folder markers that identify this pattern
    frequency: int = 1          # How often we've seen this
    confidence: float = 0.5     # How confident we are in detection
    user_confirmed: bool = False  # User verified this is correct
    first_seen: str = ""        # ISO timestamp
    last_seen: str = ""
    examples: List[str] = None  # Example paths where we saw this

    def __post_init__(self):
        if self.examples is None:
            self.examples = []


@dataclass
class UserPreference:
    """User's stated preference for organization."""
    preference_id: str
    user_id: str
    style: str                  # OrganizationStyle name
    sort_mode: int = 1          # Preferred sort mode (1-5)
    sensitivity: int = 50       # Preferred sensitivity (1-100)
    custom_rules: Dict = None   # User-defined groupings
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if self.custom_rules is None:
            self.custom_rules = {}


# =============================================================================
# HIERARCHY LEARNER
# =============================================================================

class HierarchyLearner:
    """
    Learns from codebases to better understand organization patterns.

    Vibe Coder Friendly:
    - Never judges organization choices
    - Offers gentle suggestions, not rules
    - Celebrates diverse coding styles
    - Helps beginners understand common patterns
    """

    def __init__(self, db_path: Path = None):
        """Initialize learner with optional database path."""
        self.db_path = db_path or Path.home() / ".context-dna" / "hierarchy_learning.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        # Pattern detection rules (learned over time)
        self.known_patterns: List[LearnedPattern] = []
        self._load_patterns()

    def _init_db(self):
        """Initialize SQLite database for learning storage."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Patterns table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS learned_patterns (
                pattern_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                markers TEXT,  -- JSON list
                frequency INTEGER DEFAULT 1,
                confidence REAL DEFAULT 0.5,
                user_confirmed INTEGER DEFAULT 0,
                first_seen TEXT,
                last_seen TEXT,
                examples TEXT  -- JSON list
            )
        """)

        # User preferences table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                preference_id TEXT PRIMARY KEY,
                user_id TEXT,
                style TEXT,
                sort_mode INTEGER DEFAULT 1,
                sensitivity INTEGER DEFAULT 50,
                custom_rules TEXT,  -- JSON
                created_at TEXT,
                updated_at TEXT
            )
        """)

        # Observation history
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                root_path TEXT,
                project_count INTEGER,
                detected_styles TEXT,  -- JSON list
                user_feedback TEXT,
                notes TEXT
            )
        """)

        conn.commit()
        conn.close()

    def _load_patterns(self):
        """Load learned patterns from database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM learned_patterns ORDER BY frequency DESC")
        rows = cursor.fetchall()

        self.known_patterns = []
        for row in rows:
            self.known_patterns.append(LearnedPattern(
                pattern_id=row[0],
                name=row[1],
                description=row[2],
                markers=json.loads(row[3]) if row[3] else [],
                frequency=row[4],
                confidence=row[5],
                user_confirmed=bool(row[6]),
                first_seen=row[7],
                last_seen=row[8],
                examples=json.loads(row[9]) if row[9] else [],
            ))

        conn.close()

        # Seed with default patterns if empty
        if not self.known_patterns:
            self._seed_default_patterns()

    def _seed_default_patterns(self):
        """Seed database with common patterns."""
        defaults = [
            LearnedPattern(
                pattern_id="monorepo",
                name="Monorepo",
                description="Multiple packages in one repo",
                markers=["packages/", "apps/", "lerna.json", "pnpm-workspace.yaml"],
                frequency=10,
                confidence=0.9,
            ),
            LearnedPattern(
                pattern_id="nextjs_fullstack",
                name="Next.js Full-Stack",
                description="Next.js with API routes",
                markers=["next.config.js", "pages/api/", "app/api/"],
                frequency=10,
                confidence=0.9,
            ),
            LearnedPattern(
                pattern_id="django_project",
                name="Django Project",
                description="Python Django web app",
                markers=["manage.py", "wsgi.py", "settings.py"],
                frequency=10,
                confidence=0.95,
            ),
            LearnedPattern(
                pattern_id="vibe_flat",
                name="Flat Structure",
                description="Everything at root - simple and valid!",
                markers=[],  # Detected by lack of deep nesting
                frequency=5,
                confidence=0.7,
            ),
            LearnedPattern(
                pattern_id="learning_project",
                name="Learning Project",
                description="Tutorial or learning code - keep going!",
                markers=["test/", "try/", "experiment/", "tutorial/", "learn/"],
                frequency=5,
                confidence=0.6,
            ),
        ]

        for pattern in defaults:
            pattern.first_seen = datetime.now().isoformat()
            pattern.last_seen = datetime.now().isoformat()
            self._save_pattern(pattern)

        self._load_patterns()

    def _save_pattern(self, pattern: LearnedPattern):
        """Save a pattern to database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO learned_patterns
            (pattern_id, name, description, markers, frequency, confidence,
             user_confirmed, first_seen, last_seen, examples)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pattern.pattern_id,
            pattern.name,
            pattern.description,
            json.dumps(pattern.markers),
            pattern.frequency,
            pattern.confidence,
            int(pattern.user_confirmed),
            pattern.first_seen,
            pattern.last_seen,
            json.dumps(pattern.examples),
        ))

        conn.commit()
        conn.close()

    # -------------------------------------------------------------------------
    # LEARNING FROM OBSERVATIONS
    # -------------------------------------------------------------------------

    def observe_and_learn(
        self,
        projects: List[DetectedProject],
        root_path: str = ""
    ) -> Dict:
        """
        Observe a codebase and learn from its organization.

        Returns analysis with detected styles and suggestions.
        """
        analysis = {
            "detected_styles": [],
            "organization_quality": "good",  # Always positive!
            "patterns_found": [],
            "suggestions": [],
            "encouragement": "",
        }

        # Detect which patterns match
        for pattern in self.known_patterns:
            match_score = self._check_pattern_match(projects, pattern)
            if match_score > 0.3:
                analysis["patterns_found"].append({
                    "pattern": pattern.name,
                    "confidence": match_score,
                    "description": pattern.description,
                })

                # Update pattern frequency
                pattern.frequency += 1
                pattern.last_seen = datetime.now().isoformat()
                if root_path:
                    pattern.examples.append(root_path)
                    pattern.examples = pattern.examples[-10:]  # Keep last 10
                self._save_pattern(pattern)

        # Detect organization style
        style = self._detect_organization_style(projects)
        analysis["detected_styles"].append(style.name_id)
        analysis["encouragement"] = style.encouragement

        # Generate friendly suggestions
        analysis["suggestions"] = self._generate_friendly_suggestions(
            projects, style
        )

        # Record observation
        self._record_observation(root_path, projects, analysis)

        return analysis

    def _check_pattern_match(
        self,
        projects: List[DetectedProject],
        pattern: LearnedPattern
    ) -> float:
        """Check how well projects match a known pattern."""
        if not pattern.markers:
            return 0.0

        all_markers = []
        for p in projects:
            all_markers.extend(p.markers_found)
            all_markers.append(p.path.lower())
            all_markers.append(p.name.lower())

        matches = 0
        for marker in pattern.markers:
            marker_lower = marker.lower().rstrip('/')
            if any(marker_lower in m.lower() for m in all_markers):
                matches += 1

        if not pattern.markers:
            return 0.0

        return matches / len(pattern.markers)

    def _detect_organization_style(
        self,
        projects: List[DetectedProject]
    ) -> OrganizationStyle:
        """Detect the overall organization style."""
        # Check for monorepo markers
        has_packages = any('package' in p.path.lower() for p in projects)
        has_apps = any('app' in p.path.lower() for p in projects)

        if has_packages or (has_apps and len(projects) > 5):
            return OrganizationStyle.MONOREPO

        # Check for microservices
        service_count = sum(1 for p in projects if 'service' in p.name.lower())
        if service_count >= 3:
            return OrganizationStyle.MICROSERVICES

        # Check for layered
        has_backend = any('backend' in p.name.lower() for p in projects)
        has_frontend = any('frontend' in p.name.lower() for p in projects)
        if has_backend and has_frontend:
            return OrganizationStyle.LAYERED

        # Check for learning patterns
        learning_markers = ['test', 'try', 'experiment', 'tutorial', 'learn', 'demo']
        learning_count = sum(
            1 for p in projects
            if any(m in p.name.lower() for m in learning_markers)
        )
        if learning_count >= 2:
            return OrganizationStyle.EXPERIMENT

        # Check depth for flat structure
        max_depth = max((p.path.count('/') for p in projects), default=0)
        if max_depth <= 1:
            return OrganizationStyle.FLAT

        # Default to creative chaos (totally valid!)
        return OrganizationStyle.CHAOS_CREATIVE

    def _generate_friendly_suggestions(
        self,
        projects: List[DetectedProject],
        style: OrganizationStyle
    ) -> List[Dict]:
        """
        Generate friendly, non-judgmental suggestions.

        These are NEVER required - just helpful ideas!
        """
        suggestions = []

        # Style-specific encouragement
        if style == OrganizationStyle.FLAT:
            suggestions.append({
                "type": "encouragement",
                "title": "Simple is Beautiful",
                "message": "Your flat structure is easy to navigate. "
                          "As your project grows, you might consider folders - "
                          "but only when YOU feel the need!",
                "priority": "low",
            })

        elif style == OrganizationStyle.CHAOS_CREATIVE:
            suggestions.append({
                "type": "encouragement",
                "title": "Organic Growth",
                "message": "Your project has grown naturally - that's great! "
                          "Many successful projects started exactly this way. "
                          "Context DNA will help you navigate it.",
                "priority": "low",
            })

        elif style == OrganizationStyle.EXPERIMENT:
            suggestions.append({
                "type": "encouragement",
                "title": "Learning Mode",
                "message": "I see you're experimenting - that's the best way to learn! "
                          "Keep those test folders, they're valuable learning records.",
                "priority": "low",
            })

        # Check for common helpful patterns
        if len(projects) > 10:
            suggestions.append({
                "type": "tip",
                "title": "Large Codebase",
                "message": "With many projects, try Sort Mode 2 (By Type) "
                          "or Sort Mode 4 (By Framework) to organize the view.",
                "priority": "medium",
            })

        # Check for mixed technologies
        frameworks = set(p.framework for p in projects if p.framework)
        if len(frameworks) > 3:
            suggestions.append({
                "type": "observation",
                "title": "Multi-Stack Project",
                "message": f"You're working with {len(frameworks)} different technologies! "
                          "That's ambitious. Sort Mode 4 (By Framework) might help.",
                "priority": "low",
            })

        return suggestions

    def _record_observation(
        self,
        root_path: str,
        projects: List[DetectedProject],
        analysis: Dict
    ):
        """Record this observation for future learning."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO observations
            (timestamp, root_path, project_count, detected_styles, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            root_path,
            len(projects),
            json.dumps(analysis["detected_styles"]),
            json.dumps(analysis.get("suggestions", [])),
        ))

        conn.commit()
        conn.close()

    # -------------------------------------------------------------------------
    # USER FEEDBACK INTEGRATION
    # -------------------------------------------------------------------------

    def record_user_feedback(
        self,
        pattern_id: str,
        feedback: str,
        is_correct: bool = True
    ):
        """
        Record user feedback to improve pattern detection.

        This is how we learn from vibe coders!
        """
        # Find pattern
        for pattern in self.known_patterns:
            if pattern.pattern_id == pattern_id:
                if is_correct:
                    pattern.confidence = min(1.0, pattern.confidence + 0.1)
                    pattern.user_confirmed = True
                else:
                    pattern.confidence = max(0.1, pattern.confidence - 0.1)

                self._save_pattern(pattern)
                break

    def learn_new_pattern(
        self,
        name: str,
        description: str,
        markers: List[str],
        example_path: str = ""
    ):
        """
        Learn a new organizational pattern from user input.

        Vibe coders teach us new patterns!
        """
        pattern_id = name.lower().replace(" ", "_")

        # Check if pattern exists
        for existing in self.known_patterns:
            if existing.pattern_id == pattern_id:
                # Update existing
                existing.markers.extend(m for m in markers if m not in existing.markers)
                existing.frequency += 1
                existing.last_seen = datetime.now().isoformat()
                if example_path:
                    existing.examples.append(example_path)
                self._save_pattern(existing)
                return

        # Create new pattern
        new_pattern = LearnedPattern(
            pattern_id=pattern_id,
            name=name,
            description=description,
            markers=markers,
            frequency=1,
            confidence=0.5,
            user_confirmed=True,
            first_seen=datetime.now().isoformat(),
            last_seen=datetime.now().isoformat(),
            examples=[example_path] if example_path else [],
        )

        self._save_pattern(new_pattern)
        self.known_patterns.append(new_pattern)

    # -------------------------------------------------------------------------
    # STATISTICS & INSIGHTS
    # -------------------------------------------------------------------------

    def get_learning_stats(self) -> Dict:
        """Get statistics about what we've learned."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM observations")
        total_observations = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM learned_patterns")
        total_patterns = cursor.fetchone()[0]

        cursor.execute("""
            SELECT detected_styles, COUNT(*) as count
            FROM observations
            GROUP BY detected_styles
            ORDER BY count DESC
            LIMIT 5
        """)
        common_styles = cursor.fetchall()

        conn.close()

        return {
            "total_codebases_observed": total_observations,
            "patterns_learned": total_patterns,
            "most_common_styles": common_styles,
            "top_patterns": [
                {"name": p.name, "frequency": p.frequency}
                for p in self.known_patterns[:5]
            ],
        }


# =============================================================================
# CLI TEST
# =============================================================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("🧠 HIERARCHY LEARNER TEST")
    print("=" * 60)

    learner = HierarchyLearner()

    # Show stats
    stats = learner.get_learning_stats()
    print(f"\nLearning Stats:")
    print(f"  Codebases Observed: {stats['total_codebases_observed']}")
    print(f"  Patterns Learned: {stats['patterns_learned']}")

    print(f"\nTop Patterns:")
    for p in stats['top_patterns']:
        print(f"  - {p['name']} (seen {p['frequency']}x)")

    print("\n✅ Learner ready!")
