#!/usr/bin/env python3
"""
Context DNA: Adaptive Hierarchy Hub

The UNIFIED integration point that connects:
- Sensitivity slider (projects appear/disappear dynamically)
- 5 Sorting modes (different mental models)
- Type classification (intelligent type detection)
- Adaptive learning (learns from user behavior)
- Side-by-side comparison (all 5 modes at once)

This is the DYNAMIC experience where slider movement updates ALL views
simultaneously, showing real projects adapting in real-time.

Philosophy: Everything adapts to YOU. Slider moves → projects update →
type counts change → all 5 modes recompute → user selects what fits best.
"""

import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
import json

# Rich library for beautiful terminal UI
try:
    from rich.console import Console, Group
    from rich.tree import Tree
    from rich.panel import Panel
    from rich.live import Live
    from rich.layout import Layout
    from rich.table import Table
    from rich.text import Text
    from rich.style import Style
    from rich.box import ROUNDED, DOUBLE, HEAVY, SIMPLE
    from rich.columns import Columns
    from rich.prompt import Prompt, Confirm, IntPrompt
    from rich.markdown import Markdown
    from rich.rule import Rule
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

# Import all the adaptive components
try:
    from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer, DetectedProject
    from context_dna.setup.interactive_tree import (
        HierarchySortMode, GradientSlider, InteractiveTreeVisualizer,
        SensitivityLevel,
    )
    from context_dna.setup.project_types import (
        IntelligentTypeDetector, ProjectCategory, PROJECT_TYPES,
    )
    from context_dna.setup.type_counts_ui import TypeCountsPanel
    from context_dna.setup.mode_comparison_selector import (
        ModeComparisonSelector, PROJECT_TEMPLATES, ProjectTemplate,
    )
    from context_dna.setup.adaptive_mode_learner import (
        AdaptiveModeLearner, UserOrganizationProfile, ProjectPreference,
    )
    COMPONENTS_AVAILABLE = True
except ImportError as e:
    COMPONENTS_AVAILABLE = False
    _import_error = str(e)


# =============================================================================
# DESELECTOR MODE - Manual Override ("Hail Mary Inzone Pass")
# =============================================================================

class ProjectRelationship(Enum):
    """
    Defines the relationship between a deselected item and its parent project.

    These relationships help Context DNA understand the nuanced structure
    of complex development environments.
    """
    STANDALONE = ("standalone", "Standalone - not part of any project", "📦")
    BRANCH_PRODUCTION = ("branch_production", "Branch of Production version", "🚀")
    BRANCH_DEV = ("branch_dev", "Branch of Development version", "🔧")
    DEV_ENVIRONMENT = ("dev_environment", "Dev environment (not product, but part of project)", "🧪")
    TEST_ENVIRONMENT = ("test_environment", "Test/QA environment", "🧫")
    STAGING = ("staging", "Staging environment", "🎭")
    ARCHIVE = ("archive", "Archived/legacy version", "📁")
    FORK = ("fork", "Fork of external project", "🍴")
    DEPENDENCY = ("dependency", "External dependency/vendored code", "📦")
    TEMPLATE = ("template", "Template/scaffold for new projects", "📋")
    EXPERIMENT = ("experiment", "Experimental/R&D branch", "🔬")
    CONFIG_VARIANT = ("config_variant", "Config variant (same code, different env)", "⚙️")

    def __init__(self, value: str, description: str, icon: str):
        self._value_ = value
        self.description = description
        self.icon = icon

    @classmethod
    def get_choices(cls) -> list:
        """Get list of (value, description) for selection UI."""
        return [(r.value, f"{r.icon} {r.description}") for r in cls]


@dataclass
class ManualProjectOverride:
    """
    Stores a user's manual override for a detected item.

    This is the "Deselector Mode" data - when the adaptive detector
    isn't quite right, the user can manually specify:
    - Whether an item IS actually a project
    - What relationship it has to other projects
    - Free-form notes for context
    """
    path: str                          # Relative path to the item
    is_project: bool                   # User says: this IS/ISN'T a project
    relationship: Optional[str] = None # If not a project, what is it?
    parent_project: Optional[str] = None  # Which project does it belong to?
    notes: str = ""                    # Free text for additional context
    created_at: str = ""               # When override was created

    def to_dict(self) -> dict:
        """Serialize for storage."""
        return {
            "path": self.path,
            "is_project": self.is_project,
            "relationship": self.relationship,
            "parent_project": self.parent_project,
            "notes": self.notes,
            "created_at": self.created_at or time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ManualProjectOverride":
        """Deserialize from storage."""
        return cls(
            path=data.get("path", ""),
            is_project=data.get("is_project", True),
            relationship=data.get("relationship"),
            parent_project=data.get("parent_project"),
            notes=data.get("notes", ""),
            created_at=data.get("created_at", ""),
        )


@dataclass
class DeselectorModeState:
    """
    Complete state for the Deselector Mode UI.

    This is the "hail mary inzone pass" - when adaptive detection
    needs human correction, this stores all the manual overrides.
    """
    enabled: bool = False              # Is deselector mode active?
    overrides: Dict[str, ManualProjectOverride] = field(default_factory=dict)  # path → override
    free_text_context: str = ""        # Global free text for the adaptive detector
    last_updated: str = ""             # When state was last saved

    def to_dict(self) -> dict:
        """Serialize for storage."""
        return {
            "enabled": self.enabled,
            "overrides": {k: v.to_dict() for k, v in self.overrides.items()},
            "free_text_context": self.free_text_context,
            "last_updated": self.last_updated or time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeselectorModeState":
        """Deserialize from storage."""
        overrides = {
            k: ManualProjectOverride.from_dict(v)
            for k, v in data.get("overrides", {}).items()
        }
        return cls(
            enabled=data.get("enabled", False),
            overrides=overrides,
            free_text_context=data.get("free_text_context", ""),
            last_updated=data.get("last_updated", ""),
        )


# =============================================================================
# DYNAMIC MODE CARD - Shows actual projects that adapt with slider
# =============================================================================

@dataclass
class DynamicModeView:
    """
    A single mode's view of the projects at current sensitivity.

    Updates dynamically as slider moves - projects appear/disappear.
    """
    mode: HierarchySortMode
    groups: Dict[str, List[Tuple[Any, str]]]  # group_name → [(project, type)]
    visible_count: int
    hidden_count: int
    type_counts: Dict[str, int]  # type_name → count


class AdaptiveHierarchyHub:
    """
    The unified integration hub for all adaptive hierarchy features.

    Connects:
    - GradientSlider (1-100% sensitivity)
    - HierarchySortMode (5 philosophies)
    - TypeCountsPanel (dynamic type classification)
    - AdaptiveModeLearner (learns user preferences)
    - ModeComparisonSelector (side-by-side views)

    Everything updates together when slider moves.

    INTELLIGENT DEFAULTS:
    - Analyzes codebase to recommend the BEST starting mode
    - Calculates optimal initial sensitivity
    - Learns and adapts over time
    """

    def __init__(
        self,
        root_path: Path = None,
        console: Console = None,
        auto_scan: bool = True,
    ):
        self.console = console or (Console() if RICH_AVAILABLE else None)
        self.root = root_path or Path.cwd()

        # Core state
        self.projects: List[DetectedProject] = []
        self.selected_mode: HierarchySortMode = HierarchySortMode.CONFIDENCE

        # Sensitivity slider - the driver of dynamic updates
        self.slider = GradientSlider(initial_percent=50)

        # Type classification
        self.type_detector = IntelligentTypeDetector() if COMPONENTS_AVAILABLE else None
        self.type_counts_panel: Optional[TypeCountsPanel] = None

        # Adaptive learner - learns from user behavior
        self.learner = AdaptiveModeLearner(root_path=self.root) if COMPONENTS_AVAILABLE else None

        # Mode comparison - all 5 modes simultaneously
        self.mode_views: Dict[HierarchySortMode, DynamicModeView] = {}

        # User preferences
        self.user_skill_level = "intermediate"  # beginner, intermediate, advanced
        self.show_pro_tips = True
        self.show_type_badges = True

        # Intelligent recommendation tracking
        self.recommended_mode: Optional[HierarchySortMode] = None
        self.recommended_sensitivity: Optional[int] = None
        self.recommendation_reason: str = ""

        # =====================================================================
        # DESELECTOR MODE - Manual Override State
        # =====================================================================
        self.deselector_state = DeselectorModeState()
        self._deselector_storage_path = self.root / ".context-dna" / "deselector_overrides.json"

        # Load existing overrides if any
        self._load_deselector_state()

        # Scan projects if requested
        if auto_scan:
            self._scan_projects()

    # =========================================================================
    # DESELECTOR MODE METHODS - "Hail Mary Inzone Pass"
    # =========================================================================

    def _load_deselector_state(self):
        """Load saved deselector overrides from disk."""
        try:
            if self._deselector_storage_path.exists():
                with open(self._deselector_storage_path, 'r') as f:
                    data = json.load(f)
                    self.deselector_state = DeselectorModeState.from_dict(data)
        except Exception as e:
            # Don't fail on load errors - start fresh
            print(f"[WARN] Failed to load deselector state, starting fresh: {e}")

    def _save_deselector_state(self):
        """Save deselector overrides to disk."""
        try:
            self._deselector_storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.deselector_state.last_updated = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self._deselector_storage_path, 'w') as f:
                json.dump(self.deselector_state.to_dict(), f, indent=2)
        except Exception as e:
            if self.console:
                self.console.print(f"[yellow]Warning: Could not save deselector state: {e}[/]")

    def get_all_detected_items(self) -> List[DetectedProject]:
        """
        Get ALL detected items regardless of confidence threshold.

        This is for Deselector Mode - shows everything so user can
        deselect non-projects and specify relationships.
        """
        if not COMPONENTS_AVAILABLE:
            return []

        analyzer = HierarchyAnalyzer(self.root)
        # Use threshold 0 to get everything
        all_items = analyzer.detect_all_projects(max_depth=3)
        return all_items

    def apply_manual_override(
        self,
        path: str,
        is_project: bool,
        relationship: Optional[str] = None,
        parent_project: Optional[str] = None,
        notes: str = "",
    ):
        """
        Apply a manual override for a detected item.

        Args:
            path: Relative path to the item
            is_project: User says this IS a project (True) or ISN'T (False)
            relationship: If not a project, what relationship does it have?
            parent_project: If related, which project does it belong to?
            notes: Free-form notes for context
        """
        override = ManualProjectOverride(
            path=path,
            is_project=is_project,
            relationship=relationship,
            parent_project=parent_project,
            notes=notes,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.deselector_state.overrides[path] = override
        self._save_deselector_state()

        # Rebuild views with override applied
        self._rebuild_all_mode_views()

    def remove_override(self, path: str):
        """Remove a manual override, reverting to auto-detection."""
        if path in self.deselector_state.overrides:
            del self.deselector_state.overrides[path]
            self._save_deselector_state()
            self._rebuild_all_mode_views()

    def set_global_free_text(self, text: str):
        """
        Set global free text context for the adaptive detector.

        This text helps the detector understand the user's codebase
        when automatic detection isn't sufficient.
        """
        self.deselector_state.free_text_context = text
        self._save_deselector_state()

    def _apply_overrides_to_projects(
        self,
        projects: List[DetectedProject],
    ) -> List[DetectedProject]:
        """
        Apply manual overrides to a list of projects.

        - Items marked is_project=False are removed
        - Items marked is_project=True are kept/added
        - Relationship info is attached as metadata
        """
        if not self.deselector_state.overrides:
            return projects

        result = []
        seen_paths = set()

        for p in projects:
            path_str = str(p.path.relative_to(self.root) if p.path.is_absolute() else p.path)
            seen_paths.add(path_str)

            override = self.deselector_state.overrides.get(path_str)
            if override:
                if override.is_project:
                    # Keep it, attach metadata
                    p.metadata = p.metadata or {}
                    p.metadata['manual_override'] = True
                    p.metadata['relationship'] = override.relationship
                    p.metadata['parent_project'] = override.parent_project
                    p.metadata['notes'] = override.notes
                    result.append(p)
                # else: deselected, don't add
            else:
                # No override, include normally
                result.append(p)

        return result

    def _scan_projects(self):
        """Scan the workspace for projects."""
        if not COMPONENTS_AVAILABLE:
            return

        analyzer = HierarchyAnalyzer(self.root)
        self.projects = analyzer.detect_all_projects(max_depth=2)

        # Initialize type classification
        if self.projects and self.type_detector:
            self.type_counts_panel = TypeCountsPanel(
                detector=self.type_detector,
                projects=self.projects,
                console=self.console,
            )

        # INTELLIGENT DEFAULTS - analyze codebase and recommend best mode
        self._calculate_intelligent_defaults()

        # Build initial mode views
        self._rebuild_all_mode_views()

    def _calculate_intelligent_defaults(self):
        """
        Analyze the detected projects and recommend the BEST starting mode.

        This is what makes Context DNA intelligent - it doesn't just show modes,
        it RECOMMENDS the one that best fits your actual codebase structure.

        Heuristics:
        - Many submodules → Dependency mode (shows build order)
        - Clear frontend/backend split → Type mode (separation of concerns)
        - Multiple languages/frameworks → Framework mode (tech-focused)
        - High variation in confidence → Confidence mode (validate detection)
        - Large codebase with many projects → Alphabetical mode (quick scanning)
        """
        if not self.projects:
            self.recommended_mode = HierarchySortMode.CONFIDENCE
            self.recommended_sensitivity = 50
            self.recommendation_reason = "No projects detected"
            return

        # Analyze project characteristics
        total = len(self.projects)
        submodules = [p for p in self.projects if getattr(p, 'is_submodule', False)]
        high_conf = [p for p in self.projects if p.confidence >= 0.9]
        low_conf = [p for p in self.projects if p.confidence < 0.6]

        # Detect project types for analysis
        type_distribution = {}
        framework_distribution = {}
        for p in self.projects:
            if self.type_detector:
                ptype, conf, related = self.type_detector.detect_project_type(
                    path=p.path,
                    name=p.name,
                    markers=p.markers_found,
                    description=p.description,
                )
                type_name = ptype.name if ptype else "Unknown"
            else:
                type_name = "Unknown"

            # Track type distribution
            type_distribution[type_name] = type_distribution.get(type_name, 0) + 1

            # Track framework/language distribution
            lang = getattr(p, 'language', 'unknown') or 'unknown'
            framework_distribution[lang] = framework_distribution.get(lang, 0) + 1

        # Calculate characteristics
        has_many_submodules = len(submodules) >= 3
        has_clear_type_split = len(type_distribution) >= 3
        has_multi_language = len([l for l in framework_distribution if framework_distribution[l] >= 2]) >= 2
        has_confidence_variation = len(low_conf) >= 2 and len(high_conf) >= 2
        is_large_codebase = total >= 10

        # Check for frontend/backend pattern
        has_frontend_backend = any(
            'frontend' in t.lower() or 'react' in t.lower() or 'next' in t.lower()
            for t in type_distribution
        ) and any(
            'backend' in t.lower() or 'api' in t.lower() or 'django' in t.lower()
            for t in type_distribution
        )

        # RECOMMENDATION LOGIC - order matters (most specific first)

        # 1. Many submodules → Dependency mode (shows how things connect)
        if has_many_submodules:
            self.recommended_mode = HierarchySortMode.DEPENDENCY
            self.recommendation_reason = (
                f"Detected {len(submodules)} submodules - "
                "Dependency mode shows how your projects connect"
            )

        # 2. Clear frontend/backend separation → Type mode
        elif has_frontend_backend:
            self.recommended_mode = HierarchySortMode.TYPE
            self.recommendation_reason = (
                "Detected frontend/backend separation - "
                "Type mode groups by concern"
            )

        # 3. Multiple languages/frameworks → Framework mode
        elif has_multi_language:
            langs = [l for l in framework_distribution if framework_distribution[l] >= 2]
            self.recommended_mode = HierarchySortMode.FRAMEWORK
            self.recommendation_reason = (
                f"Detected multiple technologies ({', '.join(langs[:3])}) - "
                "Framework mode groups by tech stack"
            )

        # 4. Large codebase → Alphabetical for quick scanning
        elif is_large_codebase:
            self.recommended_mode = HierarchySortMode.ALPHABETICAL
            self.recommendation_reason = (
                f"Large codebase ({total} projects) - "
                "Alphabetical mode helps find things fast"
            )

        # 5. High confidence variation → Confidence mode to validate
        elif has_confidence_variation:
            self.recommended_mode = HierarchySortMode.CONFIDENCE
            self.recommendation_reason = (
                "Mixed confidence levels detected - "
                "Confidence mode helps validate detection accuracy"
            )

        # 6. Default → Confidence mode (most neutral starting point)
        else:
            self.recommended_mode = HierarchySortMode.CONFIDENCE
            self.recommendation_reason = (
                "Standard project structure - "
                "Confidence mode shows what was detected most reliably"
            )

        # Set the selected mode to the recommendation
        self.selected_mode = self.recommended_mode

        # CALCULATE OPTIMAL SENSITIVITY
        # Goal: Show 4-15 meaningful projects, not too few, not too many
        self._calculate_optimal_sensitivity()

    def _calculate_optimal_sensitivity(self):
        """
        Calculate the optimal initial sensitivity based on project distribution.

        Strategy:
        1. Find natural breaks in confidence distribution
        2. Target showing 4-15 visible projects
        3. Prefer showing submodules + high confidence projects
        """
        if not self.projects:
            self.recommended_sensitivity = 50
            self.slider.percent = 50
            return

        # Get confidence distribution
        confidences = sorted(
            [p.confidence for p in self.projects if not getattr(p, 'is_submodule', False)],
            reverse=True
        )

        if not confidences:
            # Only submodules - use lower sensitivity (stricter)
            self.recommended_sensitivity = 30
            self.slider.percent = 30
            return

        # Count projects at different thresholds
        def count_at_threshold(thresh):
            return sum(
                1 for p in self.projects
                if p.confidence >= thresh or getattr(p, 'is_submodule', False)
            )

        # Find the "sweet spot" threshold
        thresholds = [0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20]
        ideal_threshold = 0.50

        for thresh in thresholds:
            count = count_at_threshold(thresh)
            # Sweet spot: 4-15 projects
            if 4 <= count <= 15:
                ideal_threshold = thresh
                break

        # Look for natural breaks (gaps) in confidence
        if len(confidences) >= 3:
            for i in range(len(confidences) - 1):
                gap = confidences[i] - confidences[i + 1]
                if gap > 0.15:  # Significant gap
                    gap_threshold = confidences[i + 1] + 0.01
                    gap_count = count_at_threshold(gap_threshold)
                    if 3 <= gap_count <= 20:
                        ideal_threshold = gap_threshold
                        break

        # Convert threshold to sensitivity percentage
        # threshold 0.9 → sensitivity 10%
        # threshold 0.5 → sensitivity 50%
        ideal_sensitivity = int((1.0 - ideal_threshold) * 100)
        ideal_sensitivity = max(15, min(85, ideal_sensitivity))  # Clamp 15-85%

        self.recommended_sensitivity = ideal_sensitivity
        self.slider.percent = ideal_sensitivity

    @property
    def threshold(self) -> float:
        """Current confidence threshold from slider."""
        return self.slider.threshold

    @property
    def sensitivity(self) -> int:
        """Current sensitivity percentage (1-100)."""
        return self.slider.percent

    @sensitivity.setter
    def sensitivity(self, value: int):
        """Set sensitivity and trigger updates."""
        old_value = self.slider.percent
        self.slider.percent = value

        if old_value != self.slider.percent:
            # Sensitivity changed - rebuild everything
            self._rebuild_all_mode_views()

    def _get_visible_projects(self) -> List[DetectedProject]:
        """Get projects visible at current sensitivity, with overrides applied."""
        # First filter by threshold
        filtered = [
            p for p in self.projects
            if p.confidence >= self.threshold or getattr(p, 'is_submodule', False)
        ]
        # Then apply manual overrides
        return self._apply_overrides_to_projects(filtered)

    def _get_hidden_count(self) -> int:
        """Get count of projects hidden at current sensitivity."""
        return len(self.projects) - len(self._get_visible_projects())

    # =========================================================================
    # MODE VIEW BUILDING
    # =========================================================================

    def _rebuild_all_mode_views(self):
        """Rebuild all 5 mode views based on current sensitivity."""
        visible = self._get_visible_projects()
        hidden_count = self._get_hidden_count()

        for mode in HierarchySortMode:
            self.mode_views[mode] = self._build_mode_view(mode, visible, hidden_count)

        # Update type counts panel to match threshold
        if self.type_counts_panel:
            self.type_counts_panel.update_at_threshold(self.threshold)

    def _build_mode_view(
        self,
        mode: HierarchySortMode,
        visible_projects: List[DetectedProject],
        hidden_count: int,
    ) -> DynamicModeView:
        """Build a single mode's view of the projects."""
        # Detect types for each project
        projects_with_types = []
        for p in visible_projects:
            if self.type_detector:
                ptype, conf, related = self.type_detector.detect_project_type(
                    path=p.path,
                    name=p.name,
                    markers=p.markers_found,
                    description=p.description,
                )
                type_name = ptype.name if ptype else "Unknown"
            else:
                type_name = "Project"
            projects_with_types.append((p, type_name))

        # Group based on mode
        groups = self._group_by_mode(mode, projects_with_types)

        # Count types
        type_counts = {}
        for group_projects in groups.values():
            for p, t in group_projects:
                type_counts[t] = type_counts.get(t, 0) + 1

        return DynamicModeView(
            mode=mode,
            groups=groups,
            visible_count=len(visible_projects),
            hidden_count=hidden_count,
            type_counts=type_counts,
        )

    def _group_by_mode(
        self,
        mode: HierarchySortMode,
        projects_with_types: List[Tuple[DetectedProject, str]],
    ) -> Dict[str, List[Tuple[DetectedProject, str]]]:
        """Group projects according to the mode's philosophy."""

        if mode == HierarchySortMode.CONFIDENCE:
            groups = {
                "🟢 High Confidence": [],
                "🟡 Medium Confidence": [],
                "🟠 Lower Confidence": [],
            }
            for p, t in projects_with_types:
                if p.is_submodule or p.confidence >= 0.9:
                    groups["🟢 High Confidence"].append((p, t))
                elif p.confidence >= 0.6:
                    groups["🟡 Medium Confidence"].append((p, t))
                else:
                    groups["🟠 Lower Confidence"].append((p, t))

        elif mode == HierarchySortMode.TYPE:
            groups = {
                "⚙️ Backend": [],
                "🎨 Frontend": [],
                "🏗️ Infrastructure": [],
                "📚 Libraries & Other": [],
            }
            for p, t in projects_with_types:
                t_lower = t.lower()
                if any(kw in t_lower for kw in ['backend', 'api', 'server', 'django', 'flask', 'fastapi']):
                    groups["⚙️ Backend"].append((p, t))
                elif any(kw in t_lower for kw in ['frontend', 'react', 'next', 'vue', 'web', 'ui']):
                    groups["🎨 Frontend"].append((p, t))
                elif any(kw in t_lower for kw in ['infra', 'docker', 'terraform', 'kubernetes', 'devops']):
                    groups["🏗️ Infrastructure"].append((p, t))
                else:
                    groups["📚 Libraries & Other"].append((p, t))

        elif mode == HierarchySortMode.ALPHABETICAL:
            groups = {}
            for p, t in sorted(projects_with_types, key=lambda x: x[0].name.lower()):
                letter = p.name[0].upper()
                key = f"🔤 {letter}"
                if key not in groups:
                    groups[key] = []
                groups[key].append((p, t))

        elif mode == HierarchySortMode.FRAMEWORK:
            groups = {
                "🐍 Python": [],
                "⚛️ JavaScript/TypeScript": [],
                "🐳 Docker/Containers": [],
                "🔧 Other": [],
            }
            for p, t in projects_with_types:
                if p.language == 'python' or any(kw in t.lower() for kw in ['python', 'django', 'flask']):
                    groups["🐍 Python"].append((p, t))
                elif p.language == 'javascript' or any(kw in t.lower() for kw in ['next', 'react', 'node', 'typescript']):
                    groups["⚛️ JavaScript/TypeScript"].append((p, t))
                elif 'docker' in t.lower() or 'container' in t.lower():
                    groups["🐳 Docker/Containers"].append((p, t))
                else:
                    groups["🔧 Other"].append((p, t))

        elif mode == HierarchySortMode.DEPENDENCY:
            groups = {
                "🧠 Core/Shared": [],
                "🔌 Services": [],
                "📱 Apps": [],
                "🔧 Tools": [],
            }
            for p, t in projects_with_types:
                name_lower = p.name.lower()
                desc_lower = (p.description or "").lower()
                if any(kw in name_lower for kw in ['core', 'shared', 'lib', 'common', 'utils']):
                    groups["🧠 Core/Shared"].append((p, t))
                elif any(kw in desc_lower for kw in ['api', 'backend', 'server', 'service']):
                    groups["🔌 Services"].append((p, t))
                elif any(kw in desc_lower for kw in ['app', 'frontend', 'mobile', 'web', 'client']):
                    groups["📱 Apps"].append((p, t))
                else:
                    groups["🔧 Tools"].append((p, t))

        else:
            groups = {"📁 Projects": projects_with_types}

        # Remove empty groups
        return {k: v for k, v in groups.items() if v}

    # =========================================================================
    # RENDERING - DYNAMIC SIDE-BY-SIDE VIEW
    # =========================================================================

    def _render_recommendation_badge(self) -> Text:
        """
        Render the intelligent recommendation badge.

        Shows what mode Context DNA recommends and WHY.
        """
        text = Text()

        if self.recommended_mode:
            text.append("🧠 ", style="bold")
            text.append("CONTEXT DNA RECOMMENDS: ", style="bold bright_magenta")
            text.append(f"{self.recommended_mode.icon} {self.recommended_mode.label}", style="bold bright_green")

            is_selected = self.selected_mode == self.recommended_mode
            if is_selected:
                text.append(" ✓", style="bold bright_green")
            else:
                text.append(f" (you selected {self.selected_mode.label})", style="dim")

            text.append("\n")
            text.append("   💡 ", style="dim")
            text.append(f"{self.recommendation_reason}\n\n", style="italic dim")

        return text

    def _render_slider_header(self) -> Text:
        """Render the sensitivity slider with labels."""
        text = Text()

        # Recommendation badge at the TOP
        text.append_text(self._render_recommendation_badge())

        # Title
        text.append("🎚️ ", style="bold")
        text.append("SENSITIVITY: ", style="bold bright_blue")
        text.append(f"{self.sensitivity}%", style="bold bright_green")
        text.append(f" ({self.slider.level_name})", style="dim")

        # Show if this is the recommended sensitivity
        if self.recommended_sensitivity and abs(self.sensitivity - self.recommended_sensitivity) <= 5:
            text.append(" ⭐ optimal", style="bright_yellow")
        text.append("\n")

        # Slider bar
        text.append("  1% ")
        bar = self.slider.render_bar(40)
        text.append_text(Text.from_markup(bar))
        text.append(" 100%\n")

        # Marker
        marker_pos = int(self.slider.value * 40)
        text.append("      " + " " * marker_pos + "▲\n", style="bright_green")

        # Stats
        visible = len(self._get_visible_projects())
        hidden = self._get_hidden_count()
        text.append(f"\n📊 Showing ", style="dim")
        text.append(f"{visible}", style="bold bright_green")
        text.append(f" projects", style="dim")
        if hidden > 0:
            text.append(f" (+{hidden} hidden at this sensitivity)", style="dim")
        text.append("\n")

        return text

    def _render_type_badges(self) -> Text:
        """Render type count badges that update with slider."""
        if not self.type_counts_panel:
            return Text()

        return self.type_counts_panel.render_compact_badges()

    def _render_mode_card(
        self,
        mode: HierarchySortMode,
        is_selected: bool,
        max_projects_per_group: int = 4,
        max_groups: int = 3,
    ) -> Panel:
        """
        Render a DYNAMIC mode card showing ACTUAL projects.

        This updates in real-time as the slider moves.
        """
        view = self.mode_views.get(mode)
        if not view:
            return Panel("No data", title=mode.label)

        content = Text()

        # Mode description
        content.append(f"{mode.description}\n", style="dim")
        content.append(f"Best for: ", style="dim")
        content.append(f"{mode.best_for}\n", style="italic cyan")

        # Type counts badge for this mode
        if view.type_counts:
            content.append("\n📊 ", style="bold")
            for type_name, count in sorted(view.type_counts.items(), key=lambda x: -x[1])[:4]:
                # Truncate long type names
                display_name = type_name[:10] if len(type_name) > 10 else type_name
                content.append(f"{display_name}:", style="dim")
                content.append(f"{count} ", style="bright_cyan")
            content.append("\n")

        # ACTUAL PROJECTS - this is what makes it dynamic!
        content.append("\n")
        groups_shown = 0
        for group_name, group_projects in view.groups.items():
            if groups_shown >= max_groups:
                remaining_groups = len(view.groups) - groups_shown
                if remaining_groups > 0:
                    content.append(f"[dim]... +{remaining_groups} more groups[/]\n")
                break

            content.append(f"{group_name}\n", style="bold")

            for i, (p, t) in enumerate(group_projects):
                if i >= max_projects_per_group:
                    remaining = len(group_projects) - i
                    content.append(f"    [dim]... +{remaining} more[/]\n")
                    break

                # Project icon based on type
                icon = "📦" if getattr(p, 'is_submodule', False) else "📁"

                # Confidence indicator
                if p.confidence >= 0.9:
                    conf_style = "bright_green"
                elif p.confidence >= 0.6:
                    conf_style = "yellow"
                else:
                    conf_style = "bright_red"

                content.append(f"  {icon} ", style="dim")
                content.append(f"{p.name}", style="bright_white")
                content.append(f" ", style="dim")
                content.append(f"[{t}]", style="dim cyan")
                content.append(f" ({p.confidence:.0%})\n", style=conf_style)

            groups_shown += 1

        # Selection and recommendation indicators
        is_recommended = mode == self.recommended_mode

        if is_selected and is_recommended:
            border_style = "bold bright_green"
            title_extra = " ⭐✓ RECOMMENDED"
        elif is_selected:
            border_style = "bold bright_green"
            title_extra = " ✓ SELECTED"
        elif is_recommended:
            border_style = "bright_magenta"
            title_extra = " ⭐ RECOMMENDED"
        else:
            border_style = "dim"
            title_extra = f" [{mode.number}]"

        return Panel(
            content,
            title=f"[bold]{mode.icon} {mode.number}. {mode.label}{title_extra}[/]",
            border_style=border_style,
            box=DOUBLE if is_selected else ROUNDED,
            width=42,
        )

    def _render_all_modes_side_by_side(self) -> Group:
        """
        Render ALL 5 modes side-by-side with ACTUAL projects.

        Each mode shows the same projects organized differently.
        As slider moves, projects appear/disappear in ALL modes simultaneously.
        """
        # Header with slider
        header = self._render_slider_header()

        # Type badges (update with slider)
        type_badges = self._render_type_badges()

        # Mode cards in 2 rows
        modes = list(HierarchySortMode)

        # Row 1: Modes 1-3
        row1_cards = [
            self._render_mode_card(mode, mode == self.selected_mode)
            for mode in modes[:3]
        ]

        # Row 2: Modes 4-5
        row2_cards = [
            self._render_mode_card(mode, mode == self.selected_mode)
            for mode in modes[3:]
        ]

        # Instructions
        instructions = Text()
        instructions.append("\n" + "─" * 90 + "\n", style="dim")

        # Show what ⭐ means
        instructions.append("⭐ = Context DNA's recommendation based on your codebase  ", style="bright_magenta")
        instructions.append("✓ = Your selection\n\n", style="bright_green")

        instructions.append("🎮 ", style="bold")
        instructions.append("←/→", style="bold bright_cyan")
        instructions.append(" adjust sensitivity  ", style="dim")
        instructions.append("1-5", style="bold bright_green")
        instructions.append(" select mode  ", style="dim")
        instructions.append("Enter", style="bold bright_green")
        instructions.append(" confirm  ", style="dim")
        instructions.append("Q", style="bold")
        instructions.append(" quit\n", style="dim")

        return Group(
            Panel(header, title="[bold]🧬 ADAPTIVE HIERARCHY DETECTOR[/]", border_style="bright_blue"),
            type_badges if self.show_type_badges else Text(),
            Text(""),  # Spacer
            Columns(row1_cards, equal=True, expand=True),
            Text(""),  # Spacer
            Columns(row2_cards, equal=True, expand=True),
            instructions,
        )

    def _render_pro_tips(self) -> Panel:
        """Render helpful tips based on user skill level."""
        tips = Text()

        # Branch safety tip
        tips.append("🛡️ ", style="bold yellow")
        tips.append("BRANCH SAFETY: ", style="bold yellow")

        if self.user_skill_level == "beginner":
            tips.append(
                "Create a branch before making changes:\n"
                "   git checkout -b experiment/my-changes\n"
                "If it breaks, your main code is safe!\n",
                style="dim"
            )
        else:
            tips.append("git checkout -b feature/my-changes before editing\n", style="dim")

        tips.append("\n🔄 ", style="bold bright_cyan")
        tips.append("RUN ANYTIME: ", style="bold bright_cyan")
        tips.append(
            "Re-run the Adaptive Hierarchy Detector whenever your\n"
            "   project structure changes. It learns and adapts!\n",
            style="dim"
        )

        tips.append("\n   ", style="dim")
        tips.append("context-dna analyze", style="bright_green")
        tips.append(" or ", style="dim")
        tips.append("python -m context_dna.setup.adaptive_hierarchy_hub\n", style="bright_green")

        return Panel(
            tips,
            title="[bold]💡 PRO TIPS[/]",
            border_style="bright_yellow",
            box=ROUNDED,
        )

    def render_full_view(self) -> Group:
        """Render the complete adaptive hierarchy view."""
        main_view = self._render_all_modes_side_by_side()

        if self.show_pro_tips:
            tips = self._render_pro_tips()
            return Group(main_view, tips)

        return main_view

    # =========================================================================
    # INTERACTIVE DEMO - SHOWS EVERYTHING UPDATING TOGETHER
    # =========================================================================

    def select_mode(self, mode_number: int):
        """
        Select a mode and track the selection for learning.

        If user selects different from recommendation, we learn from that.
        """
        for mode in HierarchySortMode:
            if mode.number == mode_number:
                old_mode = self.selected_mode
                self.selected_mode = mode

                # Track if user deviated from recommendation (for learning)
                if self.learner and mode != self.recommended_mode:
                    # User chose differently - this is valuable signal
                    self.learner.observe_mode_preference(mode_number)

                # Rebuild views with new mode highlighted
                self._rebuild_all_mode_views()
                return

    def run_dynamic_demo(self, cycles: int = 2, step: int = 5):
        """
        Run a demo showing ALL 5 modes updating as slider moves.

        This is the full adaptive experience:
        - Starts with Context DNA's recommended mode
        - Slider sweeps showing projects appearing/disappearing
        - All 5 mode cards update simultaneously
        - Type counts adapt dynamically
        """
        if not RICH_AVAILABLE:
            print("Rich library required for interactive mode.")
            return

        self.console.clear()

        # Build recommendation info for header
        rec_info = ""
        if self.recommended_mode:
            rec_info = (
                f"\n\n🧠 Context DNA analyzed your codebase and recommends:\n"
                f"   [bold bright_green]{self.recommended_mode.icon} {self.recommended_mode.label}[/]\n"
                f"   [dim]{self.recommendation_reason}[/]"
            )

        # Header
        self.console.print(Panel(
            "[bold bright_blue]🧬 ADAPTIVE HIERARCHY HUB - DYNAMIC DEMO[/]\n\n"
            "Watch ALL 5 organization modes update simultaneously!\n"
            "As sensitivity changes, projects appear/disappear across all views."
            f"{rec_info}\n\n"
            "[dim]This is how Synaptic learns your project structure...[/]",
            border_style="bright_blue",
            box=DOUBLE,
        ))

        with Live(self.render_full_view(), console=self.console, refresh_per_second=4) as live:
            for _ in range(cycles):
                # Sweep from 20% to 90%
                for pct in range(20, 91, step):
                    self.sensitivity = pct
                    live.update(self.render_full_view())
                    time.sleep(0.2)

                time.sleep(0.5)

                # Sweep back
                for pct in range(90, 19, -step):
                    self.sensitivity = pct
                    live.update(self.render_full_view())
                    time.sleep(0.2)

                time.sleep(0.5)

        self.console.print("\n[bold green]✅ Dynamic demo complete![/]")
        self.console.print(
            "[dim]All 5 modes showed the SAME projects organized differently,\n"
            "adapting as sensitivity changed.[/]"
        )

    def run_mode_selection(self) -> Optional[int]:
        """
        Run interactive mode selection with dynamic slider.

        Starts with Context DNA's intelligent recommendation.
        Returns selected mode number (1-5) or None if cancelled.
        """
        if not RICH_AVAILABLE:
            return self._run_fallback_selection()

        self.console.clear()

        # Build recommendation banner
        rec_banner = ""
        if self.recommended_mode:
            rec_banner = (
                f"\n\n🧠 [bold bright_magenta]INTELLIGENT RECOMMENDATION[/]\n"
                f"   Based on analyzing your {len(self.projects)} projects,\n"
                f"   Context DNA recommends: "
                f"[bold bright_green]{self.recommended_mode.icon} {self.recommended_mode.label}[/]\n"
                f"   [dim italic]{self.recommendation_reason}[/]\n"
            )

        # Title
        self.console.print(Panel(
            "[bold bright_blue]🧬 CONTEXT DNA[/]\n"
            "[dim]Adaptive Hierarchy Intelligence[/]\n\n"
            "Select how YOU want to organize your projects.\n"
            "All 5 modes show the SAME projects - just organized differently.\n"
            f"{rec_banner}\n"
            "[bright_cyan]⭐ = Recommended   ✓ = Selected   ←/→ = Adjust sensitivity[/]",
            border_style="bright_blue",
            box=DOUBLE,
        ))

        # Show the view
        self.console.print(self.render_full_view())

        # Get selection - default to recommended mode
        self.console.print("\n")
        default_choice = str(self.recommended_mode.number) if self.recommended_mode else "1"

        choice = Prompt.ask(
            f"[bold]Select organization mode[/] (recommended: {default_choice})",
            choices=["1", "2", "3", "4", "5", "q"],
            default=default_choice,
        )

        if choice.lower() == "q":
            return None

        selected = int(choice)

        # Learn from selection
        if self.learner:
            self.learner.observe_mode_preference(selected)
            self.learner.save_profile()

            # Note if they chose differently from recommendation
            if self.recommended_mode and selected != self.recommended_mode.number:
                self.console.print(
                    f"\n[dim]📝 Noted: You prefer {HierarchySortMode(list(HierarchySortMode)[selected-1]).label} "
                    f"over the recommendation. Context DNA will learn from this![/]"
                )

        return selected

    # =========================================================================
    # DESELECTOR MODE UI - "HAIL MARY INZONE PASS"
    # =========================================================================

    def _render_deselector_mode_panel(self) -> Panel:
        """
        Render the Deselector Mode panel - the manual override UI.

        This is the "hail mary inzone pass" for when adaptive detection
        needs human correction.
        """
        content = Text()

        # Header explanation
        content.append("🎯 ", style="bold bright_yellow")
        content.append("DESELECTOR MODE ", style="bold bright_yellow")
        content.append("- Manual Override\n", style="dim")
        content.append(
            "   When the adaptive detector isn't quite right, you can manually\n"
            "   specify which items ARE and AREN'T projects, and their relationships.\n\n",
            style="dim"
        )

        # Show current overrides if any
        if self.deselector_state.overrides:
            content.append("📝 ", style="bold")
            content.append(f"Current Overrides ({len(self.deselector_state.overrides)}):\n", style="bold")

            for path, override in self.deselector_state.overrides.items():
                icon = "✅" if override.is_project else "❌"
                content.append(f"   {icon} ", style="dim")
                content.append(f"{path}", style="bright_white")

                if not override.is_project and override.relationship:
                    rel = ProjectRelationship(override.relationship)
                    content.append(f" → {rel.icon} {rel.description}", style="cyan")

                if override.parent_project:
                    content.append(f" (of {override.parent_project})", style="dim")

                content.append("\n")
        else:
            content.append("[dim]No manual overrides set yet.[/]\n")

        # Show global free text if set
        if self.deselector_state.free_text_context:
            content.append("\n💬 ", style="bold")
            content.append("Global Context:\n", style="bold")
            content.append(f"   {self.deselector_state.free_text_context[:200]}", style="italic dim")
            if len(self.deselector_state.free_text_context) > 200:
                content.append("...", style="dim")
            content.append("\n")

        return Panel(
            content,
            title="[bold bright_yellow]🏈 DESELECTOR MODE - Hail Mary Inzone Pass[/]",
            border_style="bright_yellow",
            box=ROUNDED,
        )

    def run_deselector_mode(self) -> bool:
        """
        Run the interactive Deselector Mode UI.

        Returns True if changes were made, False otherwise.
        """
        if not RICH_AVAILABLE:
            return self._run_deselector_fallback()

        self.console.clear()

        # Get ALL detected items (no threshold filtering)
        all_items = self.get_all_detected_items()

        # Header
        self.console.print(Panel(
            "[bold bright_yellow]🏈 DESELECTOR MODE[/]\n"
            "[dim]The \"Hail Mary Inzone Pass\" - Manual Override for Adaptive Detection[/]\n\n"
            f"Showing [bold]{len(all_items)}[/] detected items (ALL, regardless of confidence).\n\n"
            "[bright_cyan]Actions:[/]\n"
            "  [bold]D[/]eselect - Mark item as NOT a project (and specify relationship)\n"
            "  [bold]S[/]elect   - Mark item AS a project (override low confidence)\n"
            "  [bold]T[/]ext     - Add global free text context for detector\n"
            "  [bold]C[/]lear    - Remove an override (revert to auto-detection)\n"
            "  [bold]Q[/]uit     - Exit Deselector Mode\n\n"
            "[dim italic]This is OPTIONAL - only use when adaptive detection needs correction.[/]",
            border_style="bright_yellow",
            box=DOUBLE,
        ))

        # Show current state
        self.console.print(self._render_deselector_mode_panel())

        # List all detected items with their current status
        table = Table(title="All Detected Items", box=ROUNDED)
        table.add_column("#", style="dim", width=4)
        table.add_column("Name", style="bright_white")
        table.add_column("Path", style="dim")
        table.add_column("Confidence", justify="center")
        table.add_column("Status", justify="center")

        for i, item in enumerate(all_items, 1):
            path_str = str(item.path.relative_to(self.root) if item.path.is_absolute() else item.path)
            override = self.deselector_state.overrides.get(path_str)

            # Confidence color
            if item.confidence >= 0.9:
                conf_style = "bright_green"
            elif item.confidence >= 0.6:
                conf_style = "yellow"
            else:
                conf_style = "bright_red"

            # Status
            if override:
                if override.is_project:
                    status = "[bold green]✅ Selected (manual)[/]"
                else:
                    rel = ProjectRelationship(override.relationship) if override.relationship else None
                    status = f"[bold red]❌ Deselected[/]"
                    if rel:
                        status += f"\n[dim]{rel.icon}[/]"
            else:
                status = "[dim]Auto[/]"

            table.add_row(
                str(i),
                item.name,
                path_str[:40] + "..." if len(path_str) > 40 else path_str,
                f"[{conf_style}]{item.confidence:.0%}[/]",
                status,
            )

        self.console.print(table)

        # Interactive loop
        changes_made = False
        while True:
            self.console.print("\n")
            action = Prompt.ask(
                "[bold]Action[/]",
                choices=["d", "s", "t", "c", "q"],
                default="q"
            )

            if action.lower() == "q":
                break

            elif action.lower() == "d":
                # Deselect an item
                item_num = IntPrompt.ask(
                    "Enter item number to deselect",
                    default=1
                )
                if 1 <= item_num <= len(all_items):
                    item = all_items[item_num - 1]
                    path_str = str(item.path.relative_to(self.root) if item.path.is_absolute() else item.path)

                    # Show relationship choices
                    self.console.print("\n[bold]What is this item's relationship?[/]")
                    for i, (val, desc) in enumerate(ProjectRelationship.get_choices(), 1):
                        self.console.print(f"  [{i}] {desc}")

                    rel_choice = IntPrompt.ask("Select relationship", default=1)
                    rel_list = list(ProjectRelationship)
                    relationship = rel_list[rel_choice - 1].value if 1 <= rel_choice <= len(rel_list) else None

                    # Ask for parent project if relevant
                    parent = None
                    if relationship and relationship not in ['standalone', 'fork', 'dependency', 'template']:
                        parent = Prompt.ask(
                            "Which project does this belong to? (Enter project name or path, or press Enter to skip)",
                            default=""
                        )

                    # Ask for notes
                    notes = Prompt.ask(
                        "Any additional notes? (press Enter to skip)",
                        default=""
                    )

                    self.apply_manual_override(
                        path=path_str,
                        is_project=False,
                        relationship=relationship,
                        parent_project=parent if parent else None,
                        notes=notes,
                    )
                    changes_made = True
                    self.console.print(f"[green]✓ Deselected {item.name}[/]")

            elif action.lower() == "s":
                # Select (force include) an item
                item_num = IntPrompt.ask(
                    "Enter item number to select as project",
                    default=1
                )
                if 1 <= item_num <= len(all_items):
                    item = all_items[item_num - 1]
                    path_str = str(item.path.relative_to(self.root) if item.path.is_absolute() else item.path)

                    notes = Prompt.ask(
                        "Any notes about this project? (press Enter to skip)",
                        default=""
                    )

                    self.apply_manual_override(
                        path=path_str,
                        is_project=True,
                        notes=notes,
                    )
                    changes_made = True
                    self.console.print(f"[green]✓ Selected {item.name} as project[/]")

            elif action.lower() == "t":
                # Add global free text context
                self.console.print("\n[bold]Enter global context for the adaptive detector:[/]")
                self.console.print("[dim]This text helps the detector understand your codebase structure.[/]")
                self.console.print("[dim]Example: 'This is a monorepo with separate frontend/backend. ")
                self.console.print("The /packages folder contains shared libraries, not standalone projects.'[/]\n")

                current = self.deselector_state.free_text_context
                if current:
                    self.console.print(f"[dim]Current: {current[:100]}...[/]")

                text = Prompt.ask("Enter context (or 'clear' to remove)", default="")
                if text.lower() == 'clear':
                    self.set_global_free_text("")
                    self.console.print("[green]✓ Cleared global context[/]")
                elif text:
                    self.set_global_free_text(text)
                    self.console.print("[green]✓ Set global context[/]")
                changes_made = True

            elif action.lower() == "c":
                # Clear an override
                if not self.deselector_state.overrides:
                    self.console.print("[yellow]No overrides to clear.[/]")
                    continue

                self.console.print("\n[bold]Current overrides:[/]")
                override_list = list(self.deselector_state.overrides.keys())
                for i, path in enumerate(override_list, 1):
                    self.console.print(f"  [{i}] {path}")

                idx = IntPrompt.ask("Enter number to clear (0 to cancel)", default=0)
                if 1 <= idx <= len(override_list):
                    path = override_list[idx - 1]
                    self.remove_override(path)
                    changes_made = True
                    self.console.print(f"[green]✓ Removed override for {path}[/]")

        if changes_made:
            self.console.print("\n[bold green]✅ Deselector Mode changes saved![/]")
            self.console.print(
                "[dim]The adaptive detector will now use your manual overrides.\n"
                "Run 'context-dna analyze' to see updated hierarchy.[/]"
            )

        return changes_made

    def _run_deselector_fallback(self) -> bool:
        """Fallback deselector mode without rich library."""
        print("\n" + "=" * 60)
        print("🏈 DESELECTOR MODE - Manual Override")
        print("=" * 60)
        print("\nThis mode allows manual correction of adaptive detection.")
        print("Rich library required for full interactive experience.")
        print("\nCurrent overrides saved at:", self._deselector_storage_path)

        if self.deselector_state.overrides:
            print("\nCurrent overrides:")
            for path, override in self.deselector_state.overrides.items():
                status = "IS project" if override.is_project else "NOT project"
                print(f"  {path}: {status}")

        return False

    def _run_fallback_selection(self) -> Optional[int]:
        """Fallback selection without rich library."""
        print("\n" + "=" * 60)
        print("🧬 CONTEXT DNA - HIERARCHY MODE SELECTION")
        print("=" * 60)

        visible = self._get_visible_projects()
        print(f"\nDetected {len(visible)} projects at {self.sensitivity}% sensitivity\n")

        for mode in HierarchySortMode:
            print(f"  {mode.number}. {mode.icon} {mode.label}")
            print(f"     {mode.description}")
            print()

        choice = input("\nSelect mode [1-5] or Q to quit: ").strip()

        if choice.lower() == "q":
            return None

        try:
            return int(choice)
        except ValueError:
            return 1


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    """CLI entry point."""
    args = sys.argv[1:]

    # Parse arguments
    path = None
    mode = "demo"  # Default to demo

    for arg in args:
        if arg == "--demo":
            mode = "demo"
        elif arg == "--select":
            mode = "select"
        elif arg == "--deselector":
            mode = "deselector"
        elif arg == "--help":
            print("""
🧬 Context DNA - Adaptive Hierarchy Hub

Usage:
    python -m context_dna.setup.adaptive_hierarchy_hub [options] [path]

Options:
    --demo        Run dynamic demo showing all 5 modes updating (default)
    --select      Run interactive mode selection
    --deselector  🏈 DESELECTOR MODE - Manual override ("Hail Mary Inzone Pass")
                  Shows ALL detected items regardless of confidence.
                  Allows manual deselection of non-projects and
                  specification of project relationships.
    --help        Show this help

The Adaptive Hierarchy Hub shows ALL 5 organization modes simultaneously,
with ACTUAL projects that update dynamically as you adjust sensitivity.

DESELECTOR MODE is the "hail mary" override for when adaptive detection
needs human correction. It's also foundational for the Architectural
Awareness Panel's tree map visualizations.

This is the UNIFIED experience for understanding your project structure.
""")
            return
        elif not arg.startswith("-"):
            path = Path(arg).resolve()

    # Create hub
    hub = AdaptiveHierarchyHub(
        root_path=path,
        auto_scan=True,
    )

    if not hub.projects:
        print("No projects detected. Run from a project directory or specify a path.")
        return

    if mode == "demo":
        hub.run_dynamic_demo()
    elif mode == "select":
        result = hub.run_mode_selection()
        if result:
            for m in HierarchySortMode:
                if m.number == result:
                    print(f"\n✅ Selected: {m.icon} {m.label}")
                    print(f"   {m.description}")
        else:
            print("\n❌ Selection cancelled")
    elif mode == "deselector":
        # Run the "Hail Mary Inzone Pass" - manual override mode
        changes = hub.run_deselector_mode()
        if changes:
            print("\n✅ Deselector Mode: Overrides saved")
            print("   These will be applied to future adaptive hierarchy detection.")
            print("\n🏈 The Architectural Awareness Panel will use this data")
            print("   to build accurate tree map visualizations of your project structure.")
        else:
            print("\n📝 Deselector Mode: No changes made")


if __name__ == "__main__":
    main()
