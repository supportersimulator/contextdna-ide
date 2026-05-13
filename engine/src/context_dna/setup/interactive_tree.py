#!/usr/bin/env python3
"""
Context DNA Interactive Hierarchy Tree

Real-time dynamic visualization of detected projects that responds
to sensitivity slider adjustments. Shows projects appearing/disappearing
like file trees expanding/collapsing.

Features:
- Workspace-first scanning (IDE explorer scope preferred)
- Option to expand outside workspace
- Real-time sensitivity slider
- Dynamic hierarchy tree visualization

Usage:
    python -m context_dna.setup.interactive_tree /path/to/repo
"""

import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from context_dna.setup.type_counts_ui import TypeCountsPanel
from dataclasses import dataclass
from enum import Enum

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
    from rich.box import ROUNDED, DOUBLE
    from rich.prompt import Prompt, Confirm
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    print("Note: Install 'rich' for enhanced visualization: pip install rich")

from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer, DetectedProject

# Import type classification system (additive feature)
try:
    from context_dna.setup.project_types import (
        IntelligentTypeDetector, ProjectCategory, DeploymentContext,
        PROJECT_TYPES, get_all_categories, get_types_by_category,
    )
    from context_dna.setup.type_counts_ui import TypeCountsPanel, DeploymentContextSelector
    TYPE_CLASSIFICATION_AVAILABLE = True
except ImportError:
    TYPE_CLASSIFICATION_AVAILABLE = False


# =============================================================================
# WORKSPACE SCOPE DETECTION
# =============================================================================

# =============================================================================
# HIERARCHY SORTING PHILOSOPHIES (5 Methodologies)
# =============================================================================

class HierarchySortMode(Enum):
    """
    5 Different philosophies for how coders organize their projects.

    Each represents a common mental model for file system organization.
    """

    CONFIDENCE = (1, "By Confidence", "🎯",
        "Sort by detection confidence - most certain projects first",
        "Best for: Validating what was detected correctly")

    TYPE = (2, "By Project Type", "🗂️",
        "Group by type: Backend → Frontend → Infrastructure → Tools",
        "Best for: Full-stack developers, clear separation of concerns")

    ALPHABETICAL = (3, "Alphabetical", "🔤",
        "Simple A-Z sorting for quick scanning",
        "Best for: Large codebases, finding specific projects fast")

    FRAMEWORK = (4, "By Framework", "⚡",
        "Group by technology: Django, Next.js, Docker, etc.",
        "Best for: Multi-stack environments, tech-focused teams")

    DEPENDENCY = (5, "By Dependency", "🔗",
        "Core → Services → Apps → Tools (dependency flow)",
        "Best for: Understanding architecture, build order")

    @property
    def number(self) -> int:
        return self.value[0]

    @property
    def label(self) -> str:
        return self.value[1]

    @property
    def icon(self) -> str:
        return self.value[2]

    @property
    def description(self) -> str:
        return self.value[3]

    @property
    def best_for(self) -> str:
        return self.value[4]


# =============================================================================
# WORKSPACE SCOPE DETECTION
# =============================================================================

class WorkspaceScope(Enum):
    """Scope for project detection."""
    IDE_WORKSPACE = "ide_workspace"       # Current IDE workspace only
    PARENT_DIRECTORY = "parent_dir"       # One level up
    CUSTOM_PATH = "custom_path"           # User-specified path
    FULL_SCAN = "full_scan"               # Scan everything possible


def detect_ide_workspace() -> Optional[Path]:
    """
    Detect the current IDE workspace root.

    Checks for:
    - VSCode: .vscode folder
    - Cursor: .cursor folder
    - Git root
    - package.json / pyproject.toml
    """
    cwd = Path.cwd()

    # Check for IDE markers going up the tree
    current = cwd
    for _ in range(10):  # Max 10 levels up
        # VSCode workspace
        if (current / ".vscode").is_dir():
            return current
        # Cursor workspace
        if (current / ".cursor").is_dir():
            return current
        # Git root (most common)
        if (current / ".git").is_dir():
            return current
        # Package roots
        if (current / "package.json").is_file():
            return current
        if (current / "pyproject.toml").is_file():
            return current

        parent = current.parent
        if parent == current:  # Reached filesystem root
            break
        current = parent

    # Fallback to current working directory
    return cwd


def get_parent_directories(workspace: Path, levels: int = 3) -> List[Path]:
    """Get parent directories for scope expansion options."""
    parents = []
    current = workspace.parent

    for _ in range(levels):
        if current == current.parent:  # Reached root
            break
        parents.append(current)
        current = current.parent

    return parents


# =============================================================================
# SENSITIVITY LEVELS WITH VISUAL THRESHOLDS
# =============================================================================

class SensitivityLevel(Enum):
    """Detection sensitivity with confidence thresholds."""
    MINIMAL = (1, 0.90, "Only obvious projects")      # ≥90% confidence
    LOW = (2, 0.70, "High confidence projects")       # ≥70% confidence
    NORMAL = (3, 0.50, "Standard detection")          # ≥50% confidence
    HIGH = (4, 0.30, "Sensitive detection")           # ≥30% confidence
    MAXIMUM = (5, 0.10, "Everything possible")        # ≥10% confidence

    @property
    def value_num(self) -> int:
        return self.value[0]

    @property
    def threshold(self) -> float:
        return self.value[1]

    @property
    def description(self) -> str:
        return self.value[2]


# =============================================================================
# GRADIENT SLIDER (Continuous sensitivity control)
# =============================================================================

class GradientSlider:
    """
    Fine-tuned sensitivity slider using 1-100% scale.

    Instead of discrete levels, allows precise control
    from 1% (minimum sensitivity, very strict) to 100% (maximum, show everything).

    Higher sensitivity = lower confidence threshold = more projects shown

    Examples:
        1%   → threshold 99% → Only 100% confidence projects
        25%  → threshold 75% → High confidence projects
        50%  → threshold 50% → Standard detection
        75%  → threshold 25% → Sensitive detection
        100% → threshold 0%  → Show everything
    """

    # Preset positions (percentage)
    PRESETS = {
        'minimal': 10,     # 90% threshold - very strict
        'low': 30,         # 70% threshold
        'normal': 50,      # 50% threshold
        'high': 75,        # 25% threshold
        'maximum': 100,    # 0% threshold - show all
    }

    def __init__(self, initial_percent: int = 50):
        """Initialize slider at percentage (1-100)."""
        self._percent = max(1, min(100, initial_percent))

    @property
    def percent(self) -> int:
        """Current sensitivity percentage (1-100)."""
        return self._percent

    @percent.setter
    def percent(self, v: int):
        """Set sensitivity percentage with bounds checking."""
        self._percent = max(1, min(100, int(v)))

    @property
    def value(self) -> float:
        """Normalized value (0.0 to 1.0) for internal calculations."""
        return self._percent / 100.0

    @property
    def threshold(self) -> float:
        """
        Confidence threshold based on sensitivity.

        Higher sensitivity → Lower threshold → More projects shown
        """
        # Linear: 1% sensitivity → 0.99 threshold, 100% → 0.00 threshold
        return 1.0 - self.value

    @property
    def threshold_percent(self) -> int:
        """Threshold as percentage for display."""
        return int(self.threshold * 100)

    @property
    def nearest_level(self) -> SensitivityLevel:
        """Get the nearest discrete level for the current position."""
        if self._percent <= 20:
            return SensitivityLevel.MINIMAL
        elif self._percent <= 40:
            return SensitivityLevel.LOW
        elif self._percent <= 60:
            return SensitivityLevel.NORMAL
        elif self._percent <= 80:
            return SensitivityLevel.HIGH
        else:
            return SensitivityLevel.MAXIMUM

    @property
    def level_name(self) -> str:
        """Human-readable name for current sensitivity range."""
        if self._percent <= 15:
            return "Very Strict"
        elif self._percent <= 30:
            return "Strict"
        elif self._percent <= 45:
            return "Conservative"
        elif self._percent <= 55:
            return "Normal"
        elif self._percent <= 70:
            return "Sensitive"
        elif self._percent <= 85:
            return "Very Sensitive"
        else:
            return "Maximum"

    def set_preset(self, preset: str):
        """Set to a named preset position."""
        if preset.lower() in self.PRESETS:
            self._percent = self.PRESETS[preset.lower()]

    def increment(self, step: int = 5):
        """Increase sensitivity (show more projects)."""
        self.percent = self._percent + step

    def decrement(self, step: int = 5):
        """Decrease sensitivity (show fewer projects)."""
        self.percent = self._percent - step

    def render_bar(self, width: int = 20) -> str:
        """
        Render a gradient bar showing current position.

        Returns a string with color gradient from red (strict) to green (sensitive)
        """
        filled = int(self.value * width)

        # Build gradient using block characters
        bar = ""
        for i in range(width):
            pos = i / width
            if i < filled:
                # Filled section - gradient from red to yellow to green
                if pos < 0.33:
                    bar += "[red]▓[/]"
                elif pos < 0.66:
                    bar += "[yellow]▓[/]"
                else:
                    bar += "[green]▓[/]"
            else:
                bar += "[dim]░[/]"

        return bar

    def render_with_labels(self, width: int = 30) -> str:
        """
        Render slider with percentage labels.

        Returns:
        1% ░░░░░░░░░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░ 100%
                            ▲ 50%
        """
        bar = self.render_bar(width)
        marker_pos = int(self.value * width)

        # Build marker line
        marker_line = " " * (4 + marker_pos) + f"▲ {self._percent}%"

        return f"1% {bar} 100%\n{marker_line}"

    def render_compact(self) -> str:
        """Compact rendering with percentage value."""
        bar = self.render_bar(15)
        return f"[{bar}] {self._percent}% ({self.level_name})"


# =============================================================================
# PROJECT VISIBILITY CALCULATOR
# =============================================================================

@dataclass
class VisibleProject:
    """A project that's visible at current sensitivity."""
    project: DetectedProject
    visible: bool
    fade_in: bool = False   # Just appeared
    fade_out: bool = False  # About to disappear


class ProjectVisibilityManager:
    """Manages which projects are visible at each sensitivity level."""

    def __init__(self, all_projects: List[DetectedProject]):
        self.all_projects = sorted(all_projects, key=lambda p: -p.confidence)
        self.previous_visible: set = set()

    def get_visible_at_level(
        self,
        level: SensitivityLevel
    ) -> List[VisibleProject]:
        """Get projects visible at given sensitivity, with fade states."""
        threshold = level.threshold

        current_visible = set()
        results = []

        for project in self.all_projects:
            is_visible = project.confidence >= threshold or project.is_submodule

            if is_visible:
                current_visible.add(project.path)

            # Determine fade state
            fade_in = is_visible and project.path not in self.previous_visible
            fade_out = not is_visible and project.path in self.previous_visible

            results.append(VisibleProject(
                project=project,
                visible=is_visible,
                fade_in=fade_in,
                fade_out=fade_out,
            ))

        self.previous_visible = current_visible
        return results


# =============================================================================
# INTERACTIVE TREE VISUALIZER
# =============================================================================

class InteractiveTreeVisualizer:
    """
    Real-time dynamic hierarchy tree visualization.

    Shows projects appearing/disappearing as sensitivity changes,
    like file trees expanding and collapsing.

    Features workspace-first scanning with option to expand scope.
    """

    # Color scheme
    COLORS = {
        'high_confidence': 'bright_green',
        'medium_confidence': 'yellow',
        'low_confidence': 'bright_red',
        'submodule': 'bright_cyan',
        'fade_in': 'bright_white',
        'fade_out': 'dim',
        'header': 'bold bright_blue',
        'slider_active': 'bright_green',
        'slider_inactive': 'dim',
        'workspace': 'bright_magenta',
        'outside': 'bright_yellow',
    }

    # Icons for project types
    ICONS = {
        'submodule': '📦',
        'backend': '⚙️',
        'frontend': '🎨',
        'infra': '🏗️',
        'app': '📱',
        'library': '📚',
        'config': '⚡',
        'unknown': '📁',
        'workspace': '🏠',
        'outside': '🌍',
    }

    def __init__(self, root_path: Path = None, skip_scope_selection: bool = False, use_gradient: bool = False):
        self.console = Console() if RICH_AVAILABLE else None
        self.current_level = SensitivityLevel.NORMAL
        self.workspace_scope = WorkspaceScope.IDE_WORKSPACE

        # Gradient slider support
        self.use_gradient = use_gradient
        self.gradient_slider = GradientSlider(initial_percent=50)  # Start at NORMAL

        # Hierarchy sorting mode (1-5 philosophies)
        self.sort_mode = HierarchySortMode.CONFIDENCE  # Default

        # Type classification system (additive feature)
        self.show_type_counts = True  # Show type counts at top
        self.type_detector = IntelligentTypeDetector() if TYPE_CLASSIFICATION_AVAILABLE else None
        self.type_counts_panel: Optional['TypeCountsPanel'] = None
        self.user_main_projects: List[str] = []  # User's specified main project names

        # Detect IDE workspace
        self.ide_workspace = detect_ide_workspace()

        # Root path handling - prefer IDE workspace, fallback to cwd or provided path
        if root_path:
            self.root = root_path
            self.is_within_workspace = self.ide_workspace and root_path == self.ide_workspace
        elif self.ide_workspace:
            # IDE workspace found - use it
            self.root = self.ide_workspace
            self.is_within_workspace = True
        else:
            # No IDE workspace - fallback to current directory
            self.root = Path.cwd()
            self.is_within_workspace = False  # We're "outside" since no IDE was found

        self.scan_depth = 2  # Default depth

        # Don't scan yet if we want scope selection first
        self.all_projects = []
        self.visibility_manager = None

        if skip_scope_selection:
            self._perform_scan()

    @property
    def current_threshold(self) -> float:
        """Get current confidence threshold (works with both modes)."""
        if self.use_gradient:
            return self.gradient_slider.threshold
        else:
            return self.current_level.threshold

    def set_sort_mode(self, mode: int):
        """Set hierarchy sorting mode (1-5)."""
        for m in HierarchySortMode:
            if m.number == mode:
                self.sort_mode = m
                return
        # Default to confidence if invalid
        self.sort_mode = HierarchySortMode.CONFIDENCE

    def next_sort_mode(self):
        """Cycle to next sorting mode (wraps around)."""
        modes = list(HierarchySortMode)
        current_idx = modes.index(self.sort_mode)
        next_idx = (current_idx + 1) % len(modes)
        self.sort_mode = modes[next_idx]

    def prev_sort_mode(self):
        """Cycle to previous sorting mode (wraps around)."""
        modes = list(HierarchySortMode)
        current_idx = modes.index(self.sort_mode)
        prev_idx = (current_idx - 1) % len(modes)
        self.sort_mode = modes[prev_idx]

    def render_mode_selector(self) -> Text:
        """
        Render a compact mode selector showing all 5 philosophies.

        Format: 🎯1 | 🗂️2 | 🔤3 | ⚡4 | 🔗5  ← current highlighted
        """
        text = Text()
        text.append("Sort: ")

        modes = list(HierarchySortMode)
        for i, mode in enumerate(modes):
            is_current = mode == self.sort_mode

            if is_current:
                # Highlighted current mode
                text.append(f"[{mode.icon}{mode.number}]", style="bold bright_green reverse")
            else:
                # Inactive mode
                text.append(f" {mode.icon}{mode.number} ", style="dim")

            # Separator
            if i < len(modes) - 1:
                text.append("│", style="dim")

        # Show current mode name
        text.append(f"  → {self.sort_mode.label}", style="bold")

        return text

    def render_mode_detail_panel(self) -> Panel:
        """
        Render a detailed panel showing all 5 sorting philosophies.

        Shows which is selected and what each mode does.
        """
        if not RICH_AVAILABLE:
            return None

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Key", style="bold", width=3)
        table.add_column("Icon", width=3)
        table.add_column("Name", width=15)
        table.add_column("Description")

        for mode in HierarchySortMode:
            is_current = mode == self.sort_mode
            key_style = "bold bright_green" if is_current else "dim"
            name_style = "bold bright_green" if is_current else "white"
            desc_style = "bright_green" if is_current else "dim"
            marker = "→" if is_current else " "

            table.add_row(
                f"[{key_style}]{marker}{mode.number}[/]",
                mode.icon,
                f"[{name_style}]{mode.label}[/]",
                f"[{desc_style}]{mode.description}[/]",
            )

        return Panel(
            table,
            title="[bold]🗂️ HIERARCHY SORTING MODES[/] [dim](press 1-5 or N/P)[/]",
            border_style="bright_magenta",
            box=ROUNDED,
        )

    def _sort_projects(self, projects: List[DetectedProject]) -> Dict[str, List[DetectedProject]]:
        """
        Sort and group projects according to current sort mode.

        Returns dict of category → list of projects.
        """
        if self.sort_mode == HierarchySortMode.CONFIDENCE:
            return self._sort_by_confidence(projects)
        elif self.sort_mode == HierarchySortMode.TYPE:
            return self._sort_by_type(projects)
        elif self.sort_mode == HierarchySortMode.ALPHABETICAL:
            return self._sort_alphabetical(projects)
        elif self.sort_mode == HierarchySortMode.FRAMEWORK:
            return self._sort_by_framework(projects)
        elif self.sort_mode == HierarchySortMode.DEPENDENCY:
            return self._sort_by_dependency(projects)
        else:
            return self._sort_by_confidence(projects)

    def _sort_by_confidence(self, projects: List[DetectedProject]) -> Dict[str, List[DetectedProject]]:
        """Sort by confidence level (default mode)."""
        groups = {
            "📦 Git Submodules": [],
            "🟢 High Confidence (≥90%)": [],
            "🟡 Medium Confidence (60-90%)": [],
            "🟠 Low Confidence (<60%)": [],
        }
        for p in sorted(projects, key=lambda x: -x.confidence):
            if p.is_submodule:
                groups["📦 Git Submodules"].append(p)
            elif p.confidence >= 0.90:
                groups["🟢 High Confidence (≥90%)"].append(p)
            elif p.confidence >= 0.60:
                groups["🟡 Medium Confidence (60-90%)"].append(p)
            else:
                groups["🟠 Low Confidence (<60%)"].append(p)
        return {k: v for k, v in groups.items() if v}

    def _sort_by_type(self, projects: List[DetectedProject]) -> Dict[str, List[DetectedProject]]:
        """Sort by project type: Backend → Frontend → Infrastructure → Tools."""
        groups = {
            "⚙️ Backend Services": [],
            "🎨 Frontend Apps": [],
            "🏗️ Infrastructure": [],
            "📚 Libraries & Tools": [],
            "📦 Git Submodules": [],
            "📁 Other": [],
        }
        for p in sorted(projects, key=lambda x: x.name.lower()):
            if p.is_submodule:
                groups["📦 Git Submodules"].append(p)
            elif any(kw in p.description.lower() for kw in ['django', 'backend', 'api', 'server']):
                groups["⚙️ Backend Services"].append(p)
            elif any(kw in p.description.lower() for kw in ['react', 'next', 'frontend', 'vue', 'angular']):
                groups["🎨 Frontend Apps"].append(p)
            elif any(kw in p.description.lower() for kw in ['docker', 'terraform', 'infra', 'kubernetes']):
                groups["🏗️ Infrastructure"].append(p)
            elif any(kw in p.description.lower() for kw in ['library', 'util', 'tool', 'script']):
                groups["📚 Libraries & Tools"].append(p)
            else:
                groups["📁 Other"].append(p)
        return {k: v for k, v in groups.items() if v}

    def _sort_alphabetical(self, projects: List[DetectedProject]) -> Dict[str, List[DetectedProject]]:
        """Simple alphabetical sorting."""
        sorted_projects = sorted(projects, key=lambda x: x.name.lower())

        # Group by first letter
        groups = {}
        for p in sorted_projects:
            first_letter = p.name[0].upper()
            if first_letter.isalpha():
                key = f"🔤 {first_letter}"
            else:
                key = "🔢 #"

            if key not in groups:
                groups[key] = []
            groups[key].append(p)

        return groups

    def _sort_by_framework(self, projects: List[DetectedProject]) -> Dict[str, List[DetectedProject]]:
        """Group by technology/framework."""
        groups = {
            "🐍 Python (Django, Flask)": [],
            "⚛️ JavaScript (Next.js, React)": [],
            "🐳 Docker & Containers": [],
            "☁️ Cloud Infrastructure": [],
            "📦 Git Submodules": [],
            "🔧 Other": [],
        }
        for p in sorted(projects, key=lambda x: x.name.lower()):
            if p.is_submodule:
                groups["📦 Git Submodules"].append(p)
            elif p.framework in ('django', 'flask') or p.language == 'python':
                groups["🐍 Python (Django, Flask)"].append(p)
            elif p.framework in ('nextjs', 'react', 'vue', 'angular') or p.language == 'javascript':
                groups["⚛️ JavaScript (Next.js, React)"].append(p)
            elif 'docker' in p.description.lower():
                groups["🐳 Docker & Containers"].append(p)
            elif 'terraform' in p.description.lower() or 'aws' in p.description.lower():
                groups["☁️ Cloud Infrastructure"].append(p)
            else:
                groups["🔧 Other"].append(p)
        return {k: v for k, v in groups.items() if v}

    def _sort_by_dependency(self, projects: List[DetectedProject]) -> Dict[str, List[DetectedProject]]:
        """Sort by typical dependency flow: Core → Services → Apps → Tools."""
        groups = {
            "🧠 Core & Shared": [],        # Libraries, shared code
            "🔌 Services": [],              # Backend APIs, microservices
            "📱 Applications": [],          # Frontend apps, mobile
            "🔧 DevOps & Tools": [],        # Infrastructure, scripts
            "📦 External (Submodules)": [], # Submodules
        }

        for p in sorted(projects, key=lambda x: x.name.lower()):
            if p.is_submodule:
                groups["📦 External (Submodules)"].append(p)
            elif any(kw in p.name.lower() for kw in ['core', 'common', 'shared', 'lib', 'util']):
                groups["🧠 Core & Shared"].append(p)
            elif any(kw in p.description.lower() for kw in ['api', 'backend', 'server', 'service']):
                groups["🔌 Services"].append(p)
            elif any(kw in p.description.lower() for kw in ['app', 'frontend', 'mobile', 'web', 'client']):
                groups["📱 Applications"].append(p)
            else:
                groups["🔧 DevOps & Tools"].append(p)

        return {k: v for k, v in groups.items() if v}

    def _perform_scan(self):
        """Perform the actual project detection scan."""
        self.analyzer = HierarchyAnalyzer(self.root)
        self.all_projects = self.analyzer.detect_all_projects(max_depth=self.scan_depth)
        self.visibility_manager = ProjectVisibilityManager(self.all_projects)

        # Intelligently set initial sensitivity based on detected projects
        self._calculate_ideal_sensitivity()

        # Initialize type classification (additive feature)
        self._initialize_type_classification()

    def _initialize_type_classification(self):
        """Initialize the type classification system after scanning."""
        if not TYPE_CLASSIFICATION_AVAILABLE or not self.all_projects:
            return

        # Create type counts panel with detected projects
        self.type_counts_panel = TypeCountsPanel(
            detector=self.type_detector,
            projects=self.all_projects,
            console=self.console,
        )

        # Apply user main projects if set
        if self.user_main_projects:
            self.type_counts_panel.set_main_projects(self.user_main_projects)

    def set_main_projects(self, names: List[str]):
        """Set user's main project names for relationship detection."""
        self.user_main_projects = names

        if self.type_detector:
            self.type_detector.set_user_main_projects(names)

        if self.type_counts_panel:
            self.type_counts_panel.set_main_projects(names)

        # Re-analyze if already scanned
        if self.all_projects:
            self._initialize_type_classification()

    def toggle_type_counts(self):
        """Toggle visibility of type counts panel."""
        self.show_type_counts = not self.show_type_counts

    def prompt_main_projects(self):
        """Interactive prompt for main project names."""
        if self.type_counts_panel and RICH_AVAILABLE:
            names = self.type_counts_panel.prompt_main_projects()
            self.user_main_projects = names
        else:
            # Fallback prompt
            print("\nEnter your main project names (comma-separated):")
            raw = input("> ").strip()
            if raw:
                names = [n.strip() for n in raw.split(",") if n.strip()]
                self.set_main_projects(names)

    def edit_type_counts(self):
        """Launch interactive type counts editor."""
        if self.type_counts_panel and RICH_AVAILABLE:
            self.type_counts_panel.run_interactive_edit_mode()
            # Re-build category summaries after editing
            self.type_counts_panel._build_category_summaries()

    def _calculate_ideal_sensitivity(self):
        """
        Intelligently determine the ideal initial sensitivity.

        Strategy:
        1. Find natural breaks in confidence distribution
        2. Target showing meaningful projects without noise
        3. Aim for 5-15 visible projects as "sweet spot"
        4. Prefer showing all submodules + high confidence projects
        """
        if not self.all_projects:
            # Default to normal if no projects
            self.gradient_slider.percent = 50
            self.current_level = SensitivityLevel.NORMAL
            return

        # Get confidence distribution
        confidences = sorted(
            [p.confidence for p in self.all_projects if not p.is_submodule],
            reverse=True
        )

        if not confidences:
            # Only submodules - use strict sensitivity
            self.gradient_slider.percent = 30
            self.current_level = SensitivityLevel.LOW
            return

        # Count projects at different thresholds
        def count_at_threshold(thresh):
            return sum(
                1 for p in self.all_projects
                if p.confidence >= thresh or p.is_submodule
            )

        # Find optimal threshold
        # Goal: Show 4-12 meaningful projects, not too few, not too many

        # Calculate counts at various thresholds
        thresholds = [0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20]
        counts = [(t, count_at_threshold(t)) for t in thresholds]

        # Find the "sweet spot" threshold
        ideal_threshold = 0.50  # Default
        ideal_count = 0

        for thresh, count in counts:
            # Sweet spot: 4-15 projects
            if 4 <= count <= 15:
                ideal_threshold = thresh
                ideal_count = count
                break
            # If we have very few high-confidence, keep going
            if count < 4:
                continue
            # If we have many, use stricter threshold
            if count > 15 and ideal_count == 0:
                # Use previous threshold
                ideal_threshold = thresh
                ideal_count = count

        # Look for natural breaks (gaps) in confidence
        if len(confidences) >= 3:
            gaps = []
            for i in range(len(confidences) - 1):
                gap = confidences[i] - confidences[i + 1]
                if gap > 0.15:  # Significant gap
                    gaps.append((confidences[i + 1], gap, i + 1))

            # If there's a significant gap, use it as threshold
            if gaps:
                # Use the confidence just above the biggest gap
                biggest_gap = max(gaps, key=lambda x: x[1])
                gap_threshold = biggest_gap[0] + 0.01
                gap_count = count_at_threshold(gap_threshold)

                # Only use gap threshold if it gives reasonable count
                if 3 <= gap_count <= 20:
                    ideal_threshold = gap_threshold

        # Convert threshold to sensitivity percentage
        # threshold 0.9 → sensitivity 10%
        # threshold 0.5 → sensitivity 50%
        # threshold 0.1 → sensitivity 90%
        ideal_sensitivity = int((1.0 - ideal_threshold) * 100)
        ideal_sensitivity = max(10, min(90, ideal_sensitivity))  # Clamp 10-90%

        # Set the values
        self.gradient_slider.percent = ideal_sensitivity
        self.current_level = self.gradient_slider.nearest_level

        # Store reasoning for display
        self._sensitivity_reason = f"Detected {len(self.all_projects)} projects, " \
                                   f"showing {count_at_threshold(ideal_threshold)} " \
                                   f"at {ideal_sensitivity}% sensitivity"

    def select_workspace_scope(self) -> Path:
        """
        Initial scope selection - workspace-first approach.

        Returns the selected scan root path.
        """
        if not RICH_AVAILABLE:
            return self._select_scope_fallback()

        self.console.clear()

        # Header
        self.console.print(Panel(
            "[bold bright_blue]🧬 CONTEXT DNA - WORKSPACE SCOPE SELECTION[/]\n\n"
            "Where should I look for projects?\n"
            "[dim]Starting with your IDE workspace is recommended.[/]",
            border_style="bright_blue",
            box=DOUBLE,
        ))

        # Show detected workspace
        if self.ide_workspace:
            self.console.print(f"\n[bright_magenta]🏠 Detected IDE Workspace:[/] {self.ide_workspace}")
        else:
            self.console.print(f"\n[yellow]⚠️  No IDE workspace detected.[/] Using: {Path.cwd()}")

        # Build options table
        table = Table(show_header=True, header_style="bold", box=ROUNDED)
        table.add_column("#", style="dim", width=3)
        table.add_column("Scope", style="bold")
        table.add_column("Path")
        table.add_column("Description")

        options = []

        # Option 1: IDE Workspace (recommended)
        ws_path = self.ide_workspace or Path.cwd()
        options.append(("IDE Workspace ⭐", ws_path, "Scan within your current project"))
        table.add_row("1", "IDE Workspace ⭐", str(ws_path.name) + "/", "Scan within your current project")

        # Option 2: Parent directory
        if ws_path.parent != ws_path:
            parent = ws_path.parent
            options.append(("Parent Directory", parent, "Expand to include sibling projects"))
            table.add_row("2", "Parent Directory", str(parent.name) + "/", "Expand to include sibling projects")

        # Option 3: Grandparent (if exists)
        grandparent = ws_path.parent.parent if ws_path.parent.parent != ws_path.parent else None
        if grandparent:
            options.append(("Grandparent", grandparent, "Scan entire superrepo or workspace"))
            table.add_row("3", "Grandparent", str(grandparent.name) + "/", "Scan entire superrepo or workspace")

        # Option 4: Custom path
        options.append(("Custom Path", None, "Specify a different directory"))
        table.add_row(str(len(options)), "Custom Path", "...", "Specify a different directory")

        self.console.print("\n")
        self.console.print(table)
        self.console.print("")

        # Get user choice
        choice = Prompt.ask(
            "[bold]Select scope[/]",
            choices=[str(i) for i in range(1, len(options) + 1)],
            default="1"
        )

        idx = int(choice) - 1
        selected_name, selected_path, _ = options[idx]

        if selected_path is None:
            # Custom path
            custom = Prompt.ask("[bold]Enter path[/]", default=str(Path.cwd()))
            selected_path = Path(custom).resolve()
            if not selected_path.exists():
                self.console.print(f"[red]Path does not exist: {selected_path}[/]")
                return self.select_workspace_scope()  # Retry

        # Update state
        self.root = selected_path
        self.is_within_workspace = selected_path == ws_path

        self.console.print(f"\n[green]✓[/] Selected: [bold]{selected_path}[/]")

        # Ask about scan depth
        depth_choice = Prompt.ask(
            "\n[bold]Scan depth[/]",
            choices=["shallow", "normal", "deep"],
            default="normal"
        )

        self.scan_depth = {"shallow": 1, "normal": 2, "deep": 4}[depth_choice]

        self.console.print(f"[green]✓[/] Scan depth: {depth_choice} ({self.scan_depth} levels)")

        # Now perform the scan
        self.console.print("\n[dim]Scanning...[/]")
        self._perform_scan()

        return selected_path

    def _select_scope_fallback(self) -> Path:
        """Fallback scope selection without rich library."""
        print("\n" + "=" * 60)
        print("🧬 CONTEXT DNA - WORKSPACE SCOPE SELECTION")
        print("=" * 60)

        ws_path = self.ide_workspace or Path.cwd()
        print(f"\nDetected workspace: {ws_path}")

        print("\nOptions:")
        print(f"  1. IDE Workspace (recommended): {ws_path}")
        if ws_path.parent != ws_path:
            print(f"  2. Parent Directory: {ws_path.parent}")
        print(f"  3. Custom Path")

        choice = input("\nSelect [1]: ").strip() or "1"

        if choice == "1":
            self.root = ws_path
        elif choice == "2":
            self.root = ws_path.parent
        else:
            custom = input("Enter path: ").strip()
            self.root = Path(custom).resolve()

        self._perform_scan()
        return self.root

    def offer_scope_expansion(self) -> bool:
        """
        After initial scan, offer to expand outside workspace.

        Returns True if user wants to expand, False otherwise.
        """
        if not RICH_AVAILABLE or not self.is_within_workspace:
            return False

        visible = self.visibility_manager.get_visible_at_level(self.current_level)
        visible_count = sum(1 for vp in visible if vp.visible)

        self.console.print(Panel(
            f"[bright_green]Found {visible_count} projects within workspace.[/]\n\n"
            "[dim]Would you like to expand the scan to look for more projects outside?[/]",
            title="[bold]🌍 EXPAND SCOPE?[/]",
            border_style="bright_yellow",
        ))

        expand = Confirm.ask("Expand outside workspace?", default=False)

        if expand:
            # Offer parent directories
            parents = get_parent_directories(self.root, levels=2)

            if parents:
                self.console.print("\n[bold]Available expansion scopes:[/]")
                for i, parent in enumerate(parents, 1):
                    self.console.print(f"  {i}. {parent}")

                choice = Prompt.ask(
                    "Select scope",
                    choices=[str(i) for i in range(1, len(parents) + 1)],
                    default="1"
                )

                new_root = parents[int(choice) - 1]
                self.root = new_root
                self.is_within_workspace = False

                self.console.print(f"\n[dim]Rescanning from {new_root}...[/]")
                self._perform_scan()

                return True

        return False

    def _get_project_icon(self, project: DetectedProject) -> str:
        """Get appropriate icon for project type."""
        if project.is_submodule:
            return self.ICONS['submodule']

        desc_lower = project.description.lower()
        if 'backend' in desc_lower or 'django' in desc_lower:
            return self.ICONS['backend']
        elif 'frontend' in desc_lower or 'react' in desc_lower or 'next' in desc_lower:
            return self.ICONS['frontend']
        elif 'infra' in desc_lower or 'terraform' in desc_lower or 'docker' in desc_lower:
            return self.ICONS['infra']
        elif 'app' in desc_lower or 'mobile' in desc_lower:
            return self.ICONS['app']
        elif 'lib' in desc_lower or 'util' in desc_lower or 'core' in desc_lower:
            return self.ICONS['library']

        return self.ICONS['unknown']

    def _get_confidence_style(self, confidence: float, fade_state: str = None) -> str:
        """Get style based on confidence and fade state."""
        if fade_state == 'fade_in':
            return self.COLORS['fade_in']
        elif fade_state == 'fade_out':
            return self.COLORS['fade_out']

        if confidence >= 0.90:
            return self.COLORS['high_confidence']
        elif confidence >= 0.60:
            return self.COLORS['medium_confidence']
        else:
            return self.COLORS['low_confidence']

    def _build_sensitivity_slider(self) -> Panel:
        """Build the interactive sensitivity slider display."""
        slider_parts = []

        for level in SensitivityLevel:
            if level == self.current_level:
                # Active position
                marker = "●"
                style = self.COLORS['slider_active']
            else:
                # Inactive position
                marker = "○"
                style = self.COLORS['slider_inactive']

            slider_parts.append(f"[{style}]{marker}[/]")

        # Build the slider line
        slider_line = "─".join(slider_parts)

        # Labels
        labels = "MINIMAL    LOW    NORMAL    HIGH   MAXIMUM"

        # Current level info
        level_info = f"[bold]{self.current_level.name}[/]: {self.current_level.description}"

        # Workspace indicator
        if self.ide_workspace and self.is_within_workspace:
            ws_indicator = f"🏠 IDE Workspace: [bright_magenta]{self.root.name}/[/]"
        elif self.ide_workspace:
            ws_indicator = f"🌍 Expanded: [bright_yellow]{self.root.name}/[/]"
        else:
            ws_indicator = f"📁 Directory: [bright_yellow]{self.root.name}/[/] [dim](no IDE detected)[/]"

        content = f"""
{ws_indicator}  [dim](expand in preferences)[/]

{slider_line}
{labels}

Current: {level_info}
Threshold: Shows projects with ≥{self.current_level.threshold:.0%} confidence
"""

        return Panel(
            content.strip(),
            title="[bold]🎚️ SENSITIVITY SLIDER[/]",
            border_style="bright_blue",
            box=ROUNDED,
        )

    def _build_hierarchy_tree(self) -> Tree:
        """Build the dynamic hierarchy tree using current sort mode."""
        # Get visible projects based on threshold
        threshold = self.current_threshold
        visible_projects = [
            p for p in self.all_projects
            if p.confidence >= threshold or p.is_submodule
        ]
        hidden_count = len(self.all_projects) - len(visible_projects)

        # Root of tree
        tree = Tree(
            f"[bold bright_blue]🧬 {self.root.name}[/] "
            f"[dim]({len(visible_projects)} projects)[/]",
            guide_style="bright_blue",
        )

        # Sort and group using current mode
        groups = self._sort_projects(visible_projects)

        # Add each group as a branch
        for group_name, projects in groups.items():
            if not projects:
                continue

            # Determine color based on group name
            if "High" in group_name or "🟢" in group_name:
                style = "bright_green"
            elif "Medium" in group_name or "🟡" in group_name:
                style = "yellow"
            elif "Low" in group_name or "🟠" in group_name:
                style = "bright_red"
            elif "Submodule" in group_name or "📦" in group_name:
                style = "bright_cyan"
            elif "Backend" in group_name or "⚙️" in group_name or "Service" in group_name:
                style = "bright_magenta"
            elif "Frontend" in group_name or "🎨" in group_name or "App" in group_name:
                style = "bright_blue"
            elif "Infra" in group_name or "🏗️" in group_name or "Docker" in group_name:
                style = "bright_yellow"
            else:
                style = "white"

            branch = tree.add(f"[{style}]{group_name}[/] ({len(projects)})")

            for project in projects:
                self._add_project_node_simple(branch, project)

        # Show hidden count if any
        if hidden_count > 0:
            tree.add(
                f"[dim]... {hidden_count} more projects hidden "
                f"(increase sensitivity to reveal)[/]"
            )

        return tree

    def _add_project_node_simple(self, branch: Tree, project: DetectedProject):
        """Add a project node with simpler formatting."""
        icon = self._get_project_icon(project)
        style = self._get_confidence_style(project.confidence)

        node_text = f"[{style}]{icon} {project.name}[/] [dim]({project.confidence:.0%})[/]"
        node = branch.add(node_text)

        if project.description:
            node.add(f"[dim]{project.description}[/]")

    def _add_project_node(self, branch: Tree, vp: VisibleProject):
        """Add a project node to the tree with appropriate styling."""
        project = vp.project

        # Determine fade state
        fade_state = None
        if vp.fade_in:
            fade_state = 'fade_in'
        elif vp.fade_out:
            fade_state = 'fade_out'

        style = self._get_confidence_style(project.confidence, fade_state)
        icon = self._get_project_icon(project)

        # Build node text
        confidence_pct = f"{project.confidence:.0%}"

        if fade_state == 'fade_in':
            prefix = "✨ "  # Just appeared
        elif fade_state == 'fade_out':
            prefix = "💨 "  # Fading out
        else:
            prefix = ""

        node_text = (
            f"[{style}]{prefix}{icon} {project.name}[/] "
            f"[dim]({confidence_pct})[/]"
        )

        node = branch.add(node_text)

        # Add details as sub-nodes
        if project.description:
            node.add(f"[dim]{project.description}[/]")

        if project.markers_found:
            markers = ", ".join(project.markers_found[:3])
            if len(project.markers_found) > 3:
                markers += f" +{len(project.markers_found) - 3} more"
            node.add(f"[dim]Found: {markers}[/]")

    def _build_setup_variations(self) -> Panel:
        """Build setup variations panel that changes with sensitivity."""
        visible = self.visibility_manager.get_visible_at_level(self.current_level)
        visible_projects = [vp.project for vp in visible if vp.visible]

        # Generate variations based on what's visible
        variations = self._generate_variations(visible_projects)

        table = Table(show_header=True, header_style="bold", box=ROUNDED)
        table.add_column("#", style="dim", width=3)
        table.add_column("Setup Name", style="bold")
        table.add_column("Projects", justify="center")
        table.add_column("Description")

        for i, (name, desc, count, recommended) in enumerate(variations, 1):
            rec_marker = " ⭐" if recommended else ""
            table.add_row(
                str(i),
                f"{name}{rec_marker}",
                str(count),
                desc,
            )

        return Panel(
            table,
            title="[bold]📋 SETUP VARIATIONS[/]",
            border_style="bright_magenta",
            box=ROUNDED,
        )

    def _generate_variations(
        self,
        projects: List[DetectedProject]
    ) -> List[Tuple[str, str, int, bool]]:
        """Generate setup variations based on visible projects."""
        variations = []

        total = len(projects)
        submodules = [p for p in projects if p.is_submodule]
        non_submodules = [p for p in projects if not p.is_submodule]
        high_conf = [p for p in projects if p.confidence >= 0.90]

        # Variation 1: Recommended (smart default)
        if high_conf:
            variations.append((
                "Recommended",
                f"Track {len(high_conf)} high-confidence projects",
                len(high_conf),
                True,  # Recommended
            ))

        # Variation 2: Full Workspace
        variations.append((
            "Full Workspace",
            f"Track all {total} detected projects",
            total,
            False,
        ))

        # Variation 3: Submodules Only
        if submodules:
            variations.append((
                "Submodules Only",
                f"Track {len(submodules)} git submodules independently",
                len(submodules),
                False,
            ))

        # Variation 4: Superrepo Focus
        if non_submodules and submodules:
            variations.append((
                "Superrepo Focus",
                f"Track superrepo ({len(non_submodules)} projects), exclude submodules",
                len(non_submodules),
                False,
            ))

        # Variation 5: Minimal
        if len(high_conf) > 1:
            variations.append((
                "Minimal",
                "Track only the primary project",
                1,
                False,
            ))

        return variations[:5]  # Max 5 variations

    def _build_display(self) -> Panel:
        """Build the complete display as a single panel with slider and tree."""
        # Workspace indicator
        if self.ide_workspace and self.is_within_workspace:
            ws_indicator = f"🏠 IDE Workspace: [bright_magenta]{self.root.name}/[/]"
        elif self.ide_workspace:
            ws_indicator = f"🌍 Expanded: [bright_yellow]{self.root.name}/[/]"
        else:
            ws_indicator = f"📁 Directory: [bright_yellow]{self.root.name}/[/] [dim](no IDE detected)[/]"

        # Get visible count using current threshold
        threshold = self.current_threshold
        visible_count = sum(
            1 for p in self.all_projects
            if p.confidence >= threshold or p.is_submodule
        )

        # Update type counts panel to sync with current threshold (additive feature)
        if self.type_counts_panel and TYPE_CLASSIFICATION_AVAILABLE:
            self.type_counts_panel.update_at_threshold(threshold)

        slider_text = Text()

        # Type counts badges at the top (additive feature)
        if self.show_type_counts and self.type_counts_panel and TYPE_CLASSIFICATION_AVAILABLE:
            type_badges = self.type_counts_panel.render_compact_badges()
            slider_text.append_text(type_badges)

            # Show main projects if set
            if self.user_main_projects:
                slider_text.append("\n")
                slider_text.append("🎯 ", style="bold")
                slider_text.append(", ".join(self.user_main_projects), style="bright_green")

            slider_text.append("\n\n")

        slider_text.append(f"{ws_indicator}  [dim](expand in preferences)[/]\n\n")

        if self.use_gradient:
            # Gradient slider (1-100%)
            slider_bar = self.gradient_slider.render_bar(30)
            slider_text.append(f"SENSITIVITY:  1% {slider_bar} 100%\n")
            slider_text.append(f"              {' ' * int(self.gradient_slider.value * 28)}▲ {self.gradient_slider.percent}%\n\n")
            slider_text.append(f"[bold]{self.gradient_slider.level_name}[/] ({self.gradient_slider.percent}%)\n")
            slider_text.append(f"Showing projects with ≥{self.gradient_slider.threshold_percent}% confidence\n")
            title_level = f"{self.gradient_slider.percent}%"
        else:
            # Discrete level slider
            slider_parts = []
            for level in SensitivityLevel:
                if level == self.current_level:
                    slider_parts.append("[bright_green]●[/]")
                else:
                    slider_parts.append("[dim]○[/]")
            slider_line = "─".join(slider_parts)

            slider_text.append(f"{slider_line}\n")
            slider_text.append("MINIMAL   LOW   NORMAL   HIGH   MAXIMUM\n\n")
            slider_text.append(f"[bold]{self.current_level.name}[/]: {self.current_level.description}\n")
            slider_text.append(f"Threshold: ≥{self.current_level.threshold:.0%} confidence\n")
            title_level = self.current_level.name

        slider_text.append(f"Projects: [bright_green]{visible_count}[/]\n\n")

        # Mode selector with all 5 philosophies visible
        mode_selector = self.render_mode_selector()
        slider_text.append_text(mode_selector)
        slider_text.append("\n")
        slider_text.append(f"[dim]{self.sort_mode.best_for}[/]\n")

        # Show intelligent detection reasoning if available
        if hasattr(self, '_sensitivity_reason') and self._sensitivity_reason:
            slider_text.append(f"\n[dim]💡 {self._sensitivity_reason}[/]\n")

        # Build the tree
        tree = self._build_hierarchy_tree()

        # Combine using Group
        return Panel(
            Group(slider_text, tree),
            title=f"[bold]🧬 CONTEXT DNA - SENSITIVITY {title_level}[/]",
            border_style="bright_blue",
            box=DOUBLE,
        )

    def run_interactive(self):
        """Run the interactive visualization with keyboard controls."""
        if not RICH_AVAILABLE:
            print("Rich library required for interactive mode.")
            print("Install with: pip install rich")
            return

        self.console.clear()

        # Workspace status line
        if self.ide_workspace and self.is_within_workspace:
            ws_status = f"[bright_magenta]🏠 IDE Workspace:[/] {self.root}"
        elif self.ide_workspace:
            ws_status = f"[bright_yellow]🌍 Expanded Scope:[/] {self.root}"
        else:
            ws_status = f"[bright_yellow]📁 No IDE detected - scanning:[/] {self.root}"

        # Build controls text based on available features
        controls = "[dim]Controls: [/]"
        controls += "[bold]←/→[/] or [bold]H/L[/] sensitivity  "
        controls += "[bold]1-5[/] or [bold]N/P[/] sort mode  "
        if TYPE_CLASSIFICATION_AVAILABLE:
            controls += "[bold]M[/] main  "
            controls += "[bold]E[/] edit  "
            controls += "[bold]T[/] types  "
        controls += "[bold]Q[/] quit  "
        controls += "[bold]R[/] rescan"

        # Header
        self.console.print(Panel(
            f"[bold bright_blue]🧬 CONTEXT DNA - ADAPTIVE HIERARCHY VISUALIZER[/]\n\n"
            f"{ws_status}\n\n"
            "Watch the hierarchy tree respond in real-time as you adjust sensitivity!\n"
            "Projects appear and disappear based on confidence thresholds.\n"
            "Type counts update dynamically with the slider!\n\n"
            f"{controls}",
            border_style="bright_blue",
            box=DOUBLE,
        ))

        # Show initial state
        self.console.print(self._build_display())

        # Instructions
        self.console.print("\n[dim]Press keys to interact (requires terminal with keyboard support)[/]")
        self.console.print("[dim]Or run with --demo to see animation[/]")

    def run_demo(self, cycles: int = 2):
        """Run an animated demo showing sensitivity changes."""
        if not RICH_AVAILABLE:
            self._run_fallback_demo()
            return

        self.console.clear()

        # Workspace status
        if self.ide_workspace and self.is_within_workspace:
            ws_status = f"🏠 IDE Workspace: {self.root.name}/"
        elif self.ide_workspace:
            ws_status = f"🌍 Expanded: {self.root.name}/"
        else:
            ws_status = f"📁 Scanning: {self.root.name}/ (no IDE detected)"

        # Build feature list
        features = "Watch the hierarchy tree respond as sensitivity changes!\n"
        features += "Projects appear ✨ and disappear 💨 based on thresholds.\n"
        if TYPE_CLASSIFICATION_AVAILABLE:
            features += "Type counts [bold]adapt dynamically[/] with the slider!\n"
        features += "\n[dim](Expand scope available in preferences)[/]"

        # Header
        self.console.print(Panel(
            f"[bold bright_blue]🧬 CONTEXT DNA - ADAPTIVE HIERARCHY DEMO[/]\n\n"
            f"[dim]{ws_status}[/]\n\n"
            f"{features}",
            border_style="bright_blue",
            box=DOUBLE,
        ))

        levels = list(SensitivityLevel)

        with Live(self._build_display(), console=self.console, refresh_per_second=2) as live:
            for _ in range(cycles):
                # Go from MINIMAL to MAXIMUM
                for level in levels:
                    self.current_level = level
                    live.update(self._build_display())
                    time.sleep(1.5)

                # Then back down
                for level in reversed(levels):
                    self.current_level = level
                    live.update(self._build_display())
                    time.sleep(1.5)

        self.console.print("\n[bold green]✅ Demo complete![/]")
        self.console.print("[dim]Run with --interactive for keyboard controls[/]")

    def run_gradient_demo(self, cycles: int = 2, step: int = 5):
        """
        Run animated demo with smooth gradient slider movement.

        Shows type counts dynamically updating as sensitivity changes.
        """
        if not RICH_AVAILABLE:
            self._run_fallback_demo()
            return

        self.use_gradient = True  # Enable gradient mode for this demo
        self.console.clear()

        # Workspace status
        if self.ide_workspace and self.is_within_workspace:
            ws_status = f"🏠 IDE Workspace: {self.root.name}/"
        elif self.ide_workspace:
            ws_status = f"🌍 Expanded: {self.root.name}/"
        else:
            ws_status = f"📁 Scanning: {self.root.name}/ (no IDE detected)"

        # Build feature list
        features = "Watch sensitivity sweep from 1% to 100% and back!\n"
        features += "Type counts and projects update in real-time.\n"
        if TYPE_CLASSIFICATION_AVAILABLE:
            features += "[bold bright_cyan]📊 Type counts adapt dynamically with the slider![/]\n"

        # Header
        self.console.print(Panel(
            f"[bold bright_blue]🧬 CONTEXT DNA - GRADIENT SENSITIVITY DEMO[/]\n\n"
            f"[dim]{ws_status}[/]\n\n"
            f"{features}",
            border_style="bright_blue",
            box=DOUBLE,
        ))

        with Live(self._build_display(), console=self.console, refresh_per_second=4) as live:
            for _ in range(cycles):
                # Sweep from 10% to 100%
                for pct in range(10, 101, step):
                    self.gradient_slider.percent = pct
                    live.update(self._build_display())
                    time.sleep(0.15)

                # Pause at max
                time.sleep(0.5)

                # Sweep back down to 10%
                for pct in range(100, 9, -step):
                    self.gradient_slider.percent = pct
                    live.update(self._build_display())
                    time.sleep(0.15)

                # Pause at min
                time.sleep(0.5)

        self.console.print("\n[bold green]✅ Gradient demo complete![/]")
        self.console.print("[dim]Run with --interactive for keyboard controls[/]")

    def run_full_adaptive_demo(self, cycles: int = 1):
        """
        Run comprehensive demo showing ALL adaptive features:
        - Sensitivity slider movement
        - Mode cycling through all 5 philosophies
        - Type counts adapting dynamically

        This showcases the full power of the adaptive hierarchy system.
        """
        if not RICH_AVAILABLE:
            self._run_fallback_demo()
            return

        self.use_gradient = True
        self.console.clear()

        # Workspace status
        if self.ide_workspace and self.is_within_workspace:
            ws_status = f"🏠 IDE Workspace: {self.root.name}/"
        elif self.ide_workspace:
            ws_status = f"🌍 Expanded: {self.root.name}/"
        else:
            ws_status = f"📁 Scanning: {self.root.name}/ (no IDE detected)"

        # Header
        self.console.print(Panel(
            f"[bold bright_blue]🧬 FULL ADAPTIVE HIERARCHY DEMO[/]\n\n"
            f"[dim]{ws_status}[/]\n\n"
            "This demo shows ALL adaptive features working together:\n"
            "  • [bright_cyan]Sensitivity slider[/] - projects appear/disappear\n"
            "  • [bright_magenta]5 Sorting modes[/] - different mental models\n"
            "  • [bright_green]Type counts[/] - adapt to both in real-time!\n\n"
            "[dim]Watch as the hierarchy transforms...[/]",
            border_style="bright_blue",
            box=DOUBLE,
        ))

        modes = list(HierarchySortMode)

        with Live(self._build_display(), console=self.console, refresh_per_second=4) as live:
            for _ in range(cycles):
                # For each sort mode...
                for mode in modes:
                    self.sort_mode = mode

                    # Show mode transition
                    live.update(self._build_display())
                    time.sleep(0.8)

                    # Sweep sensitivity while in this mode
                    for pct in range(30, 81, 10):  # 30% to 80%
                        self.gradient_slider.percent = pct
                        live.update(self._build_display())
                        time.sleep(0.2)

                    # Pause to show result
                    time.sleep(0.5)

                # Return to mode 1
                self.sort_mode = HierarchySortMode.CONFIDENCE

                # Final sweep showing confidence mode
                for pct in range(80, 29, -10):
                    self.gradient_slider.percent = pct
                    live.update(self._build_display())
                    time.sleep(0.2)

        self.console.print("\n[bold green]✅ Full adaptive demo complete![/]")
        self.console.print(f"[dim]Showcased all 5 hierarchy philosophies:[/]")
        for mode in modes:
            self.console.print(f"  {mode.icon} {mode.label}: {mode.description}")
        self.console.print("\n[dim]Run with --interactive for full control[/]")

    def _run_fallback_demo(self):
        """Fallback demo without rich library."""
        print("\n" + "=" * 60)
        print("🧬 CONTEXT DNA - HIERARCHY SENSITIVITY DEMO")
        print("=" * 60)

        for level in SensitivityLevel:
            print(f"\n--- Sensitivity: {level.name} (≥{level.threshold:.0%}) ---")

            visible = self.visibility_manager.get_visible_at_level(level)
            visible_count = sum(1 for vp in visible if vp.visible)

            print(f"Visible projects: {visible_count}")

            for vp in visible:
                if vp.visible:
                    marker = "✨ NEW" if vp.fade_in else "   "
                    print(f"  {marker} {vp.project.name} ({vp.project.confidence:.0%})")
                elif vp.fade_out:
                    print(f"  💨 HIDDEN {vp.project.name}")

            time.sleep(1)

        print("\n" + "=" * 60)
        print("Demo complete!")


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    """CLI entry point."""
    args = sys.argv[1:]

    # Parse arguments
    path = None  # Default to auto-detect
    mode = "demo"  # Default to demo mode
    skip_scope = False
    use_gradient = False
    main_projects = []

    for arg in args:
        if arg == "--interactive":
            mode = "interactive"
        elif arg == "--demo":
            mode = "demo"
        elif arg == "--gradient":
            mode = "gradient"  # Gradient slider demo
            use_gradient = True
        elif arg == "--full" or arg == "--adaptive":
            mode = "full"  # Full adaptive demo (sensitivity + modes + types)
        elif arg == "--scope":
            mode = "scope"  # Just scope selection
        elif arg == "--quick":
            skip_scope = True  # Skip scope selection, auto-scan
        elif arg == "--types":
            mode = "types"  # Type counts editor
        elif arg.startswith("--main="):
            # Parse main project names: --main="ER Simulator,Context DNA"
            main_str = arg.split("=", 1)[1].strip('"\'')
            main_projects = [n.strip() for n in main_str.split(",") if n.strip()]
        elif not arg.startswith("-"):
            path = Path(arg).resolve()
            if not path.exists():
                print(f"Error: Path does not exist: {path}")
                sys.exit(1)

    # Create visualizer
    visualizer = InteractiveTreeVisualizer(
        root_path=path,
        skip_scope_selection=skip_scope or mode in ("demo", "gradient"),
        use_gradient=use_gradient or mode == "gradient",
    )

    # Apply main projects if specified
    if main_projects:
        visualizer.set_main_projects(main_projects)

    if mode == "scope":
        # Just show scope selection
        visualizer.select_workspace_scope()
        print(f"\nSelected: {visualizer.root}")
        print(f"Found: {len(visualizer.all_projects)} projects")
    elif mode == "interactive":
        # Full interactive mode - start with scope selection
        if not skip_scope:
            visualizer.select_workspace_scope()
        else:
            visualizer._perform_scan()
        visualizer.run_interactive()
    elif mode == "gradient":
        # Gradient demo - shows smooth slider with type counts
        if not visualizer.all_projects:
            visualizer._perform_scan()
        visualizer.run_gradient_demo()
    elif mode == "full":
        # Full adaptive demo - sensitivity + modes + types all together
        if not visualizer.all_projects:
            visualizer._perform_scan()
        visualizer.run_full_adaptive_demo()
    elif mode == "types":
        # Type counts editor
        if not visualizer.all_projects:
            visualizer._perform_scan()
        visualizer.edit_type_counts()
    else:
        # Demo mode - auto-scan from detected workspace
        if not visualizer.all_projects:
            visualizer._perform_scan()
        visualizer.run_demo()


if __name__ == "__main__":
    main()
