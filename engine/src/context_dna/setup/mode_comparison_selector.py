#!/usr/bin/env python3
"""
Context DNA: Side-by-Side Mode Comparison Selector

Shows all 5 hierarchy organization modes simultaneously so users can
visually compare and choose which mental model fits their workflow best.

Features:
- All 5 modes visible at once with project types
- Dynamic type counts for each view
- Beginner-friendly explanations with pro tips
- Branch safety hints for protecting main codebase
- "No code yet" scenario with project setup templates
- Adaptive Hierarchy Detector can run anytime guidance

This is the SELECTION experience - after this, user proceeds with chosen mode.
"""

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
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
    from rich.box import ROUNDED, DOUBLE, HEAVY, SIMPLE
    from rich.columns import Columns
    from rich.prompt import Prompt, Confirm, IntPrompt
    from rich.markdown import Markdown
    from rich.rule import Rule
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

# Import hierarchy system
try:
    from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer, DetectedProject
    from context_dna.setup.interactive_tree import (
        HierarchySortMode, GradientSlider, InteractiveTreeVisualizer,
    )
    from context_dna.setup.project_types import (
        IntelligentTypeDetector, ProjectCategory, PROJECT_TYPES,
    )
    from context_dna.setup.type_counts_ui import TypeCountsPanel
    HIERARCHY_AVAILABLE = True
except ImportError:
    HIERARCHY_AVAILABLE = False


# =============================================================================
# PROJECT SETUP TEMPLATES (For "No Code Yet" Scenarios)
# =============================================================================

@dataclass
class ProjectTemplate:
    """A recommended project structure template."""
    id: str
    name: str
    icon: str
    description: str
    best_for: str
    structure: List[str]  # Directory structure preview
    includes: List[str]   # What's included
    branch_strategy: str  # Git branch recommendation
    complexity: str       # "beginner", "intermediate", "advanced"


# 5 Ideal Project Setup Templates
PROJECT_TEMPLATES = [
    ProjectTemplate(
        id="simple_starter",
        name="Simple Starter",
        icon="🌱",
        description="Single project, flat structure - perfect for learning",
        best_for="First projects, tutorials, small scripts",
        structure=[
            "my-project/",
            "├── src/           # Your code goes here",
            "├── tests/         # Test your code",
            "├── README.md      # Explain your project",
            "└── .gitignore     # Ignore unnecessary files",
        ],
        includes=["Basic folder structure", "README template", "gitignore"],
        branch_strategy="main only - commit directly while learning",
        complexity="beginner",
    ),
    ProjectTemplate(
        id="feature_branch",
        name="Feature Branch Workflow",
        icon="🌿",
        description="Main branch stays clean, features developed separately",
        best_for="Personal projects, small teams, learning git",
        structure=[
            "my-project/",
            "├── src/",
            "├── tests/",
            "├── docs/",
            "└── .github/       # GitHub workflows",
            "",
            "Branches:",
            "  main         → stable, always works",
            "  feature/xxx  → new features (merge when ready)",
            "  fix/xxx      → bug fixes",
        ],
        includes=["Branch templates", "PR workflow", "CI basics"],
        branch_strategy="Never commit directly to main - always use feature branches",
        complexity="beginner",
    ),
    ProjectTemplate(
        id="fullstack_mono",
        name="Full-Stack Monorepo",
        icon="🏗️",
        description="Frontend + Backend in one repo, shared tooling",
        best_for="Web apps, solo developers, startups",
        structure=[
            "my-app/",
            "├── frontend/      # React/Next.js/Vue",
            "│   ├── src/",
            "│   └── package.json",
            "├── backend/       # Django/Express/FastAPI",
            "│   ├── src/",
            "│   └── requirements.txt",
            "├── shared/        # Shared types/utils",
            "├── infra/         # Docker, deployment",
            "└── package.json   # Root scripts",
        ],
        includes=["Frontend scaffold", "Backend scaffold", "Docker setup", "Shared types"],
        branch_strategy="main + develop + feature branches",
        complexity="intermediate",
    ),
    ProjectTemplate(
        id="microservices",
        name="Microservices Architecture",
        icon="🔌",
        description="Independent services, separate deployment",
        best_for="Scalable apps, teams, complex domains",
        structure=[
            "platform/",
            "├── services/",
            "│   ├── auth/      # Authentication service",
            "│   ├── api/       # Main API gateway",
            "│   └── workers/   # Background jobs",
            "├── libs/          # Shared libraries",
            "├── infra/",
            "│   ├── terraform/ # Infrastructure as code",
            "│   └── k8s/       # Kubernetes configs",
            "└── docker-compose.yml",
        ],
        includes=["Service templates", "API gateway", "Docker orchestration", "IaC"],
        branch_strategy="GitFlow: main + develop + release + feature + hotfix",
        complexity="advanced",
    ),
    ProjectTemplate(
        id="monorepo_packages",
        name="Package Monorepo",
        icon="📦",
        description="Multiple packages/apps sharing code, npm/pip workspaces",
        best_for="Libraries, design systems, multi-app platforms",
        structure=[
            "workspace/",
            "├── packages/",
            "│   ├── core/      # Shared core logic",
            "│   ├── ui/        # Component library",
            "│   └── utils/     # Utility functions",
            "├── apps/",
            "│   ├── web/       # Main web app",
            "│   ├── admin/     # Admin dashboard",
            "│   └── mobile/    # React Native app",
            "├── tools/         # Build tooling",
            "└── turbo.json     # Turborepo config",
        ],
        includes=["Workspace config", "Package linking", "Shared builds", "Version management"],
        branch_strategy="Trunk-based: main + short-lived feature branches",
        complexity="advanced",
    ),
]


# =============================================================================
# MODE COMPARISON VIEW
# =============================================================================

class ModeComparisonSelector:
    """
    Side-by-side view of all 5 hierarchy modes for user selection.

    Shows each mode with:
    - Mode name and icon
    - What it's best for
    - Preview of how projects would be organized
    - Type counts badge
    - Selection indicator
    """

    # Skill level descriptions
    SKILL_LEVELS = {
        "beginner": ("🌱", "New to coding", "bright_green"),
        "intermediate": ("🌿", "Some experience", "yellow"),
        "advanced": ("🌳", "Professional", "bright_cyan"),
    }

    def __init__(
        self,
        projects: List[DetectedProject] = None,
        root_path: Path = None,
        console: Console = None,
    ):
        self.console = console or Console()
        self.root_path = root_path or Path.cwd()
        self.projects = projects or []

        # Selected mode (1-5)
        self.selected_mode: int = 1

        # Type detection
        self.type_detector = IntelligentTypeDetector() if HIERARCHY_AVAILABLE else None
        self.type_counts_panel = None

        # Sensitivity for filtering
        self.sensitivity = 50  # Default 50%
        self.threshold = 0.5

        # User skill level (affects explanations)
        self.skill_level = "intermediate"

        # If no projects, show template selection
        self.no_code_mode = len(self.projects) == 0
        self.selected_template: Optional[str] = None

        # Initialize type counts if we have projects
        if self.projects and self.type_detector:
            self.type_counts_panel = TypeCountsPanel(
                detector=self.type_detector,
                projects=self.projects,
                console=self.console,
            )

    def _get_visible_projects(self) -> List[DetectedProject]:
        """Get projects visible at current sensitivity."""
        return [
            p for p in self.projects
            if p.confidence >= self.threshold or getattr(p, 'is_submodule', False)
        ]

    def _sort_projects_by_mode(
        self,
        mode: HierarchySortMode,
        projects: List[DetectedProject]
    ) -> Dict[str, List[Tuple[DetectedProject, str]]]:
        """
        Sort projects by mode and include detected type.

        Returns dict of group_name → list of (project, detected_type)
        """
        # Create a temporary visualizer to use its sorting
        if not HIERARCHY_AVAILABLE:
            return {"Projects": [(p, "unknown") for p in projects]}

        # Detect types for each project
        projects_with_types = []
        for p in projects:
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
                "📚 Libraries": [],
            }
            for p, t in projects_with_types:
                if any(kw in t.lower() for kw in ['backend', 'api', 'server', 'django']):
                    groups["⚙️ Backend"].append((p, t))
                elif any(kw in t.lower() for kw in ['frontend', 'react', 'next', 'vue', 'web']):
                    groups["🎨 Frontend"].append((p, t))
                elif any(kw in t.lower() for kw in ['infra', 'docker', 'terraform']):
                    groups["🏗️ Infrastructure"].append((p, t))
                else:
                    groups["📚 Libraries"].append((p, t))

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
                "⚛️ JavaScript": [],
                "🐳 Docker": [],
                "🔧 Other": [],
            }
            for p, t in projects_with_types:
                if p.language == 'python' or 'django' in t.lower() or 'python' in t.lower():
                    groups["🐍 Python"].append((p, t))
                elif p.language == 'javascript' or any(kw in t.lower() for kw in ['next', 'react', 'node']):
                    groups["⚛️ JavaScript"].append((p, t))
                elif 'docker' in t.lower():
                    groups["🐳 Docker"].append((p, t))
                else:
                    groups["🔧 Other"].append((p, t))

        elif mode == HierarchySortMode.DEPENDENCY:
            groups = {
                "🧠 Core": [],
                "🔌 Services": [],
                "📱 Apps": [],
                "🔧 Tools": [],
            }
            for p, t in projects_with_types:
                name_lower = p.name.lower()
                if any(kw in name_lower for kw in ['core', 'shared', 'lib', 'common']):
                    groups["🧠 Core"].append((p, t))
                elif any(kw in name_lower for kw in ['api', 'backend', 'service']):
                    groups["🔌 Services"].append((p, t))
                elif any(kw in name_lower for kw in ['app', 'web', 'frontend', 'mobile']):
                    groups["📱 Apps"].append((p, t))
                else:
                    groups["🔧 Tools"].append((p, t))
        else:
            groups = {"Projects": projects_with_types}

        # Remove empty groups
        return {k: v for k, v in groups.items() if v}

    def _render_mode_card(
        self,
        mode: HierarchySortMode,
        projects: List[DetectedProject],
        is_selected: bool,
        compact: bool = True,
    ) -> Panel:
        """Render a single mode as a card showing its organization."""
        # Get projects grouped by this mode
        grouped = self._sort_projects_by_mode(mode, projects)

        # Count types for this mode's badge
        type_counts = {}
        for group_projects in grouped.values():
            for p, t in group_projects:
                if t not in type_counts:
                    type_counts[t] = 0
                type_counts[t] += 1

        # Build content
        content = Text()

        # Mode description
        content.append(f"{mode.description}\n", style="dim")
        content.append(f"Best for: ", style="dim")
        content.append(f"{mode.best_for}\n\n", style="italic")

        # Type counts badge
        content.append("📊 ", style="bold")
        for type_name, count in sorted(type_counts.items(), key=lambda x: -x[1])[:4]:
            content.append(f"{type_name[:8]}:{count} ", style="cyan")
        content.append("\n\n")

        # Project preview (compact)
        for group_name, group_projects in list(grouped.items())[:3]:  # Max 3 groups
            content.append(f"{group_name}\n", style="bold")
            for p, t in group_projects[:3]:  # Max 3 projects per group
                icon = "📦" if p.is_submodule else "📁"
                content.append(f"  {icon} ", style="dim")
                content.append(f"{p.name}", style="bright_white")
                content.append(f" [{t}]\n", style="dim cyan")
            if len(group_projects) > 3:
                content.append(f"  ... +{len(group_projects) - 3} more\n", style="dim")

        # Selection indicator
        border_style = "bold bright_green" if is_selected else "dim"
        title_style = "bold bright_green reverse" if is_selected else "bold"

        selector = " ✓ SELECTED" if is_selected else f" Press {mode.number}"

        return Panel(
            content,
            title=f"[{title_style}]{mode.icon} {mode.number}. {mode.label}{selector}[/]",
            border_style=border_style,
            box=DOUBLE if is_selected else ROUNDED,
            width=40,
        )

    def _render_all_modes_comparison(self) -> Group:
        """Render all 5 modes side by side for comparison."""
        visible_projects = self._get_visible_projects()

        # Header
        header = Text()
        header.append("\n🧬 ", style="bold")
        header.append("ADAPTIVE HIERARCHY DETECTOR", style="bold bright_blue")
        header.append(" - Choose Your Organization Style\n\n", style="dim")

        header.append("All 5 modes show the ", style="dim")
        header.append("SAME projects", style="bold bright_green")
        header.append(" organized differently.\n", style="dim")
        header.append("Choose the mental model that matches how ", style="dim")
        header.append("YOU", style="bold")
        header.append(" think about your code.\n\n", style="dim")

        # Type counts summary (applies to all modes)
        header.append("📊 Detected: ", style="bold")
        header.append(f"{len(visible_projects)} projects", style="bright_green")
        if len(self.projects) > len(visible_projects):
            header.append(f" (+{len(self.projects) - len(visible_projects)} hidden at current sensitivity)", style="dim")
        header.append("\n")

        # Render mode cards in rows
        modes = list(HierarchySortMode)

        # Row 1: Modes 1-3
        row1_cards = []
        for mode in modes[:3]:
            is_selected = mode.number == self.selected_mode
            card = self._render_mode_card(mode, visible_projects, is_selected)
            row1_cards.append(card)

        # Row 2: Modes 4-5
        row2_cards = []
        for mode in modes[3:]:
            is_selected = mode.number == self.selected_mode
            card = self._render_mode_card(mode, visible_projects, is_selected)
            row2_cards.append(card)

        # Instructions
        instructions = Text()
        instructions.append("\n" + "─" * 80 + "\n", style="dim")
        instructions.append("🎮 ", style="bold")
        instructions.append("Press ", style="dim")
        instructions.append("1-5", style="bold bright_green")
        instructions.append(" to select a mode  •  ", style="dim")
        instructions.append("↑/↓", style="bold")
        instructions.append(" adjust sensitivity  •  ", style="dim")
        instructions.append("Enter", style="bold bright_green")
        instructions.append(" to confirm  •  ", style="dim")
        instructions.append("Q", style="bold")
        instructions.append(" to quit\n", style="dim")

        return Group(
            header,
            Columns(row1_cards, equal=True, expand=True),
            Text(""),  # Spacer
            Columns(row2_cards, equal=True, expand=True),
            instructions,
        )

    def _render_helpful_tips(self) -> Panel:
        """Render helpful tips based on skill level."""
        tips = Text()

        # Branch safety tip (important for all levels)
        tips.append("🛡️ ", style="bold yellow")
        tips.append("PROTECT YOUR WORK", style="bold yellow")
        tips.append(" - Consider using branches:\n", style="dim")

        if self.skill_level == "beginner":
            tips.append("""
   Your 'main' branch is your safe, working code.
   Create a branch before experimenting:

   """, style="dim")
            tips.append("   git checkout -b experiment/trying-something", style="bright_green")
            tips.append("""

   If it works → merge it back
   If it breaks → delete the branch, main is safe!
""", style="dim")
        else:
            tips.append("   ", style="dim")
            tips.append("git checkout -b feature/my-changes", style="bright_green")
            tips.append(" before making changes\n", style="dim")

        tips.append("\n")

        # Adaptive Hierarchy tip
        tips.append("🔄 ", style="bold bright_cyan")
        tips.append("RUN ANYTIME", style="bold bright_cyan")
        tips.append(": The Adaptive Hierarchy Detector can be run\n", style="dim")
        tips.append("   whenever your project structure changes. It will:\n", style="dim")
        tips.append("   • Detect new projects and services\n", style="dim")
        tips.append("   • Update type classifications\n", style="dim")
        tips.append("   • Suggest organization improvements\n\n", style="dim")

        tips.append("   Run with: ", style="dim")
        tips.append("context-dna analyze", style="bright_green")
        tips.append(" or ", style="dim")
        tips.append("python -m context_dna.setup.interactive_tree --full\n", style="bright_green")

        return Panel(
            tips,
            title="[bold]💡 PRO TIPS[/]",
            border_style="bright_yellow",
            box=ROUNDED,
        )

    def _render_no_code_mode(self) -> Group:
        """Render template selection for new projects (no code yet)."""
        header = Text()
        header.append("\n🌱 ", style="bold")
        header.append("STARTING FRESH", style="bold bright_green")
        header.append(" - Let's set up your project the right way!\n\n", style="dim")

        header.append("No existing code detected. Choose a ", style="dim")
        header.append("project structure template", style="bold")
        header.append(" that fits your goals.\n", style="dim")
        header.append("Each template includes best-practice folder organization and git workflow.\n\n", style="dim")

        # Skill level selector
        skill_text = Text()
        skill_text.append("Your experience level: ", style="dim")
        for level, (icon, label, color) in self.SKILL_LEVELS.items():
            is_selected = level == self.skill_level
            if is_selected:
                skill_text.append(f"[{icon} {label}]", style=f"bold {color} reverse")
            else:
                skill_text.append(f" {icon} {label} ", style="dim")
            skill_text.append(" ", style="dim")
        skill_text.append("\n(Press B/I/A to change)\n\n", style="dim")

        # Template cards
        template_cards = []
        for i, template in enumerate(PROJECT_TEMPLATES, 1):
            is_selected = self.selected_template == template.id or (
                self.selected_template is None and i == 1
            )

            # Skip advanced templates for beginners
            if self.skill_level == "beginner" and template.complexity == "advanced":
                continue

            content = Text()
            content.append(f"{template.description}\n\n", style="dim")
            content.append("Best for: ", style="dim")
            content.append(f"{template.best_for}\n\n", style="italic cyan")

            # Structure preview
            content.append("Structure:\n", style="bold")
            for line in template.structure[:6]:
                content.append(f"  {line}\n", style="bright_black")
            if len(template.structure) > 6:
                content.append("  ...\n", style="dim")

            # Branch strategy
            content.append(f"\n🌿 Git: ", style="bold")
            content.append(f"{template.branch_strategy}\n", style="dim")

            border_style = "bold bright_green" if is_selected else "dim"
            selector = " ✓" if is_selected else f" [{i}]"

            card = Panel(
                content,
                title=f"[bold]{template.icon} {template.name}{selector}[/]",
                border_style=border_style,
                box=DOUBLE if is_selected else ROUNDED,
                width=45,
            )
            template_cards.append(card)

        # Arrange in rows of 2
        rows = []
        for i in range(0, len(template_cards), 2):
            row_cards = template_cards[i:i+2]
            rows.append(Columns(row_cards, equal=True, expand=True))
            rows.append(Text(""))  # Spacer

        # Instructions
        instructions = Text()
        instructions.append("\n" + "─" * 80 + "\n", style="dim")
        instructions.append("🎮 ", style="bold")
        instructions.append("Press ", style="dim")
        instructions.append("1-5", style="bold bright_green")
        instructions.append(" to select template  •  ", style="dim")
        instructions.append("B/I/A", style="bold")
        instructions.append(" skill level  •  ", style="dim")
        instructions.append("Enter", style="bold bright_green")
        instructions.append(" to create  •  ", style="dim")
        instructions.append("Q", style="bold")
        instructions.append(" to quit\n", style="dim")

        return Group(header, skill_text, *rows, instructions)

    def render_full_view(self) -> Group:
        """Render the complete comparison view."""
        if self.no_code_mode:
            main_content = self._render_no_code_mode()
        else:
            main_content = self._render_all_modes_comparison()

        tips = self._render_helpful_tips()

        # Combine
        return Group(main_content, tips)

    def run_interactive_selection(self) -> Optional[int]:
        """
        Run interactive mode selection.

        Returns selected mode number (1-5) or None if cancelled.
        """
        if not RICH_AVAILABLE:
            return self._run_fallback_selection()

        self.console.clear()

        # Title banner
        self.console.print(Panel(
            "[bold bright_blue]🧬 CONTEXT DNA[/]\n"
            "[dim]Adaptive Hierarchy Intelligence[/]\n\n"
            "Your codebase, organized YOUR way.\n"
            "Choose how you want to see your projects.",
            border_style="bright_blue",
            box=DOUBLE,
        ))

        # Show the comparison view
        self.console.print(self.render_full_view())

        # Get selection
        self.console.print("\n")

        if self.no_code_mode:
            choice = Prompt.ask(
                "[bold]Select a template[/]",
                choices=["1", "2", "3", "4", "5", "q"],
                default="1"
            )
            if choice.lower() == "q":
                return None
            return int(choice)
        else:
            choice = Prompt.ask(
                "[bold]Select organization mode[/]",
                choices=["1", "2", "3", "4", "5", "q"],
                default=str(self.selected_mode)
            )
            if choice.lower() == "q":
                return None
            return int(choice)

    def _run_fallback_selection(self) -> Optional[int]:
        """Fallback selection without rich library."""
        print("\n" + "=" * 60)
        print("🧬 CONTEXT DNA - HIERARCHY MODE SELECTION")
        print("=" * 60)

        print("\nAvailable organization modes:\n")
        for mode in HierarchySortMode:
            print(f"  {mode.number}. {mode.icon} {mode.label}")
            print(f"     {mode.description}")
            print(f"     Best for: {mode.best_for}")
            print()

        choice = input("\nSelect mode [1-5] or Q to quit: ").strip()

        if choice.lower() == "q":
            return None

        try:
            return int(choice)
        except ValueError:
            return 1


# =============================================================================
# UNIFIED SELECTION EXPERIENCE
# =============================================================================

def run_mode_selection(
    root_path: Path = None,
    projects: List[DetectedProject] = None,
) -> Tuple[Optional[int], Optional[str]]:
    """
    Run the complete mode selection experience.

    Returns:
        (selected_mode, selected_template) - mode is 1-5, template is id string
        Either can be None if cancelled
    """
    console = Console()

    # Scan for projects if not provided
    if projects is None and root_path:
        analyzer = HierarchyAnalyzer(root_path)
        projects = analyzer.detect_all_projects(max_depth=2)
    elif projects is None:
        projects = []

    # Create selector
    selector = ModeComparisonSelector(
        projects=projects,
        root_path=root_path or Path.cwd(),
        console=console,
    )

    # Run selection
    result = selector.run_interactive_selection()

    if selector.no_code_mode:
        # Return template selection
        if result and 1 <= result <= len(PROJECT_TEMPLATES):
            template = PROJECT_TEMPLATES[result - 1]
            return (None, template.id)
        return (None, None)
    else:
        # Return mode selection
        return (result, None)


def show_mode_comparison_demo():
    """Demo showing all modes with sample projects."""
    console = Console()
    console.clear()

    # Create sample projects for demo
    sample_projects = [
        DetectedProject(path="backend", name="backend", language="python",
                       framework="django", confidence=0.95, markers_found=["manage.py"],
                       description="Django web application"),
        DetectedProject(path="frontend", name="web-app", language="javascript",
                       framework="nextjs", confidence=0.95, markers_found=["next.config.js"],
                       description="Next.js React app"),
        DetectedProject(path="context-dna", name="context-dna", language="python",
                       confidence=0.90, markers_found=["pyproject.toml"],
                       description="Python project"),
        DetectedProject(path="mobile", name="mobile", language="javascript",
                       confidence=0.85, markers_found=["package.json"],
                       description="React Native app"),
        DetectedProject(path="infra", name="infra", language="terraform",
                       confidence=0.80, markers_found=["main.tf"],
                       description="Terraform infrastructure"),
        DetectedProject(path="landing-page", name="landing-page", is_submodule=True,
                       confidence=1.0, markers_found=[".gitmodules"],
                       description="Git submodule"),
        DetectedProject(path="admin-panel", name="admin-panel", is_submodule=True,
                       confidence=1.0, markers_found=[".gitmodules"],
                       description="Git submodule"),
    ]

    selector = ModeComparisonSelector(
        projects=sample_projects,
        console=console,
    )

    # Show the comparison
    console.print(Panel(
        "[bold bright_blue]🧬 CONTEXT DNA - MODE COMPARISON DEMO[/]\n\n"
        "This shows all 5 hierarchy organization modes side-by-side.\n"
        "Same projects, different mental models.",
        border_style="bright_blue",
        box=DOUBLE,
    ))

    console.print(selector.render_full_view())


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    """CLI entry point."""
    args = sys.argv[1:]

    if "--demo" in args:
        show_mode_comparison_demo()
    elif "--no-code" in args:
        # Force no-code mode for testing
        selector = ModeComparisonSelector(projects=[], console=Console())
        Console().clear()
        Console().print(selector.render_full_view())
    else:
        # Normal mode - scan current directory
        root = Path.cwd()
        for arg in args:
            if not arg.startswith("-"):
                root = Path(arg).resolve()
                break

        result = run_mode_selection(root_path=root)
        mode, template = result

        if mode:
            print(f"\n✅ Selected mode: {mode}")
            for m in HierarchySortMode:
                if m.number == mode:
                    print(f"   {m.icon} {m.label}: {m.description}")
        elif template:
            print(f"\n✅ Selected template: {template}")
            for t in PROJECT_TEMPLATES:
                if t.id == template:
                    print(f"   {t.icon} {t.name}: {t.description}")
        else:
            print("\n❌ Selection cancelled")


if __name__ == "__main__":
    main()
