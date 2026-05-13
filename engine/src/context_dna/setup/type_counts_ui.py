#!/usr/bin/env python3
"""
Context DNA Type Counts UI

Interactive UI component for displaying and editing project type counts
at the top of the gradient slider view. Allows user input for main project
names and creates an enjoyable back-and-forth feedback experience.

Features:
- Editable type counts with real-time feedback
- User input for main project names
- Dropdown selectors for adding types to search
- Deployment context badges
- Smart relationship detection display

Usage:
    from context_dna.setup.type_counts_ui import TypeCountsPanel
    panel = TypeCountsPanel(detector, projects)
    panel.render()
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Callable
from dataclasses import dataclass, field
from enum import Enum

try:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.box import ROUNDED, SIMPLE, DOUBLE
    from rich.columns import Columns
    from rich.prompt import Prompt, Confirm, IntPrompt
    from rich.layout import Layout
    from rich.live import Live
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from context_dna.setup.project_types import (
    ProjectType, ProjectCategory, DeploymentContext,
    IntelligentTypeDetector, PROJECT_TYPES,
    get_all_categories, get_types_by_category, get_all_contexts,
    ProjectTypeCount, UserProjectInput, RELATIONSHIP_PATTERNS,
)
from context_dna.setup.hierarchy_analyzer import DetectedProject


# =============================================================================
# TYPE COUNT SUMMARY BADGE
# =============================================================================

@dataclass
class TypeSummaryBadge:
    """A compact badge showing type count."""
    category: ProjectCategory
    count: int
    prod_count: int = 0
    dev_count: int = 0
    user_edited: bool = False


# =============================================================================
# TYPE COUNTS PANEL
# =============================================================================

class TypeCountsPanel:
    """
    Interactive panel showing project type counts.

    Displays at the top of the gradient slider view with:
    - Compact badges for each category with counts
    - Editable number fields
    - User input for main project names
    - Dropdown for adding new types
    """

    # Badge colors by category
    CATEGORY_COLORS = {
        ProjectCategory.MOBILE: "bright_cyan",
        ProjectCategory.WEBAPP: "bright_blue",
        ProjectCategory.BACKEND: "bright_magenta",
        ProjectCategory.DESKTOP: "yellow",
        ProjectCategory.WEBSITE: "bright_green",
        ProjectCategory.INFRASTRUCTURE: "yellow",
        ProjectCategory.LIBRARY: "cyan",
        ProjectCategory.CLI: "white",
        ProjectCategory.AI_ML: "bright_red",
        ProjectCategory.DATA: "green",
        ProjectCategory.TESTING: "magenta",
        ProjectCategory.DOCS: "blue",
        ProjectCategory.CONFIG: "dim",
        ProjectCategory.MONOREPO: "bright_yellow",
    }

    def __init__(
        self,
        detector: Optional[IntelligentTypeDetector] = None,
        projects: Optional[List[DetectedProject]] = None,
        console: Optional[Console] = None,
    ):
        self.detector = detector or IntelligentTypeDetector()
        self.projects = projects or []
        self.console = console or (Console() if RICH_AVAILABLE else None)

        # State
        self.type_counts: Dict[str, ProjectTypeCount] = {}
        self.category_summaries: Dict[ProjectCategory, TypeSummaryBadge] = {}
        self.user_main_projects: List[str] = []
        self.user_edits: Dict[str, int] = {}  # User manual count overrides
        self.show_detailed: bool = False  # Toggle between compact/detailed

        # Dynamic threshold support (syncs with slider)
        self.current_threshold: float = 0.5  # Default 50%
        self.visible_projects: List[DetectedProject] = []  # Projects at current threshold
        self.total_hidden: int = 0  # Count of projects below threshold

        # Callbacks
        self.on_counts_changed: Optional[Callable] = None
        self.on_main_projects_changed: Optional[Callable] = None

        # Analyze projects if provided
        if projects:
            self._analyze_projects()

    def update_at_threshold(self, threshold: float):
        """
        Update type counts based on new sensitivity threshold.

        Called when slider changes - only counts projects that are
        visible at the current threshold.
        """
        self.current_threshold = threshold

        # Filter to visible projects
        self.visible_projects = [
            p for p in self.projects
            if p.confidence >= threshold or getattr(p, 'is_submodule', False)
        ]
        self.total_hidden = len(self.projects) - len(self.visible_projects)

        # Re-analyze with only visible projects
        self._analyze_visible_projects()

    def _analyze_visible_projects(self):
        """Analyze only projects visible at current threshold."""
        self.type_counts = {}

        for project in self.visible_projects:
            # Detect type
            ptype, confidence, related_to = self.detector.detect_project_type(
                path=project.path,
                name=project.name,
                markers=project.markers_found,
                description=project.description,
            )

            # Detect deployment context
            context = self.detector.detect_deployment_context(
                name=project.name,
                path=project.path,
                description=project.description,
            )

            # Add to counts
            type_id = ptype.id
            if type_id not in self.type_counts:
                self.type_counts[type_id] = ProjectTypeCount(
                    type_id=type_id,
                    type_info=ptype,
                    count=0,
                    projects=[],
                    deployment_contexts={},
                )

            tc = self.type_counts[type_id]
            tc.count += 1
            tc.projects.append(project.path)

            # Track deployment contexts
            ctx_id = context.id
            if ctx_id not in tc.deployment_contexts:
                tc.deployment_contexts[ctx_id] = 0
            tc.deployment_contexts[ctx_id] += 1

        # Update detector's detected_types
        self.detector.detected_types = self.type_counts

        # Build category summaries
        self._build_category_summaries()

    def _analyze_projects(self):
        """Analyze all projects and populate type counts."""
        # Initialize with all projects visible
        self.visible_projects = self.projects
        self.total_hidden = 0
        self.type_counts = {}

        for project in self.projects:
            # Detect type
            ptype, confidence, related_to = self.detector.detect_project_type(
                path=project.path,
                name=project.name,
                markers=project.markers_found,
                description=project.description,
            )

            # Detect deployment context
            context = self.detector.detect_deployment_context(
                name=project.name,
                path=project.path,
                description=project.description,
            )

            # Add to counts
            type_id = ptype.id
            if type_id not in self.type_counts:
                self.type_counts[type_id] = ProjectTypeCount(
                    type_id=type_id,
                    type_info=ptype,
                    count=0,
                    projects=[],
                    deployment_contexts={},
                )

            tc = self.type_counts[type_id]
            tc.count += 1
            tc.projects.append(project.path)

            # Track deployment contexts
            ctx_id = context.id
            if ctx_id not in tc.deployment_contexts:
                tc.deployment_contexts[ctx_id] = 0
            tc.deployment_contexts[ctx_id] += 1

        # Update detector's detected_types
        self.detector.detected_types = self.type_counts

        # Build category summaries
        self._build_category_summaries()

    def _build_category_summaries(self):
        """Build compact summaries by category."""
        self.category_summaries = {}

        for cat in ProjectCategory:
            badge = TypeSummaryBadge(
                category=cat,
                count=0,
                prod_count=0,
                dev_count=0,
            )

            for tc in self.type_counts.values():
                if tc.type_info.category == cat:
                    badge.count += tc.count

                    # Count prod vs dev
                    for ctx_id, ctx_count in tc.deployment_contexts.items():
                        if ctx_id in ("prod_client", "prod_internal"):
                            badge.prod_count += ctx_count
                        else:
                            badge.dev_count += ctx_count

            if badge.count > 0:
                self.category_summaries[cat] = badge

    def set_main_projects(self, names: List[str]):
        """Set user's main project names."""
        self.user_main_projects = names
        self.detector.set_user_main_projects(names)

        # Re-analyze with new context
        self._analyze_projects()

        if self.on_main_projects_changed:
            self.on_main_projects_changed(names)

    def update_type_count(self, type_id: str, count: int):
        """User manually sets a type count."""
        self.user_edits[type_id] = count

        if type_id in self.type_counts:
            self.type_counts[type_id].user_override = count

            # Recalculate category summaries
            self._build_category_summaries()

            if self.on_counts_changed:
                self.on_counts_changed(type_id, count)

    def render_compact_badges(self) -> Text:
        """Render compact category badges as a single line."""
        if not RICH_AVAILABLE:
            return self._render_plain_badges()

        text = Text()
        text.append("📊 ", style="bold")

        # Show visible count
        visible_count = len(self.visible_projects)
        total_count = len(self.projects)

        if visible_count < total_count:
            text.append(f"[bright_green]{visible_count}[/][dim]/{total_count}[/] ", style="bold")
        else:
            text.append(f"[bright_green]{visible_count}[/] ", style="bold")

        badges = []
        for cat, badge in sorted(
            self.category_summaries.items(),
            key=lambda x: -x[1].count
        ):
            if badge.count == 0:
                continue

            color = self.CATEGORY_COLORS.get(cat, "white")

            # Build badge
            badge_text = f"{cat.icon}{badge.count}"

            # Add prod/dev breakdown if mixed
            if badge.prod_count > 0 and badge.dev_count > 0:
                badge_text += f"(🚀{badge.prod_count}/🔧{badge.dev_count})"

            badges.append(f"[{color}]{badge_text}[/]")

        text.append(" ".join(badges))

        # Show hidden count if any
        if self.total_hidden > 0:
            text.append(f" [dim]+{self.total_hidden} hidden[/]")

        # Show threshold indicator
        threshold_pct = int(self.current_threshold * 100)
        text.append(f" [dim]@ ≥{threshold_pct}%[/]")

        # Show edit hint
        if not self.show_detailed:
            text.append("\n[dim]Press [bold]E[/] to edit counts, [bold]M[/] to set main projects[/]")

        return text

    def _render_plain_badges(self) -> str:
        """Plain text badge rendering."""
        parts = []
        for cat, badge in sorted(
            self.category_summaries.items(),
            key=lambda x: -x[1].count
        ):
            parts.append(f"{cat.icon}{badge.count}")
        return " ".join(parts)

    def render_detailed_table(self) -> Panel:
        """Render detailed table with editable counts."""
        if not RICH_AVAILABLE:
            return self._render_plain_table()

        table = Table(
            show_header=True,
            header_style="bold",
            box=SIMPLE,
            expand=False,
        )

        table.add_column("Type", style="bold", width=25)
        table.add_column("Count", justify="center", width=8)
        table.add_column("Deploy", width=15)
        table.add_column("Edit", width=8, style="dim")

        # Group by category
        for cat in ProjectCategory:
            cat_types = [
                tc for tc in self.type_counts.values()
                if tc.type_info.category == cat and tc.count > 0
            ]

            if not cat_types:
                continue

            # Category header
            table.add_row(
                f"[bold]{cat.icon} {cat.label}[/]",
                "",
                "",
                "",
            )

            for tc in sorted(cat_types, key=lambda x: -x.count):
                ptype = tc.type_info

                # Count with user override indicator
                count_str = str(tc.count)
                if tc.user_override is not None:
                    count_str = f"[yellow]{tc.user_override}[/] [dim]({tc.count})[/]"

                # Deployment context badges
                ctx_badges = []
                for ctx_id, ctx_count in tc.deployment_contexts.items():
                    ctx = next((c for c in DeploymentContext if c.id == ctx_id), None)
                    if ctx:
                        ctx_badges.append(f"{ctx.icon}{ctx_count}")

                table.add_row(
                    f"  {ptype.icon} {ptype.name}",
                    count_str,
                    " ".join(ctx_badges[:3]),
                    "[dim][#][/]",
                )

        return Panel(
            table,
            title="[bold]📊 PROJECT TYPE COUNTS[/]",
            subtitle="[dim]Enter row # to edit count[/]",
            border_style="bright_blue",
            box=ROUNDED,
        )

    def _render_plain_table(self) -> str:
        """Plain text table rendering."""
        lines = ["PROJECT TYPE COUNTS", "=" * 40]

        for cat in ProjectCategory:
            cat_types = [
                tc for tc in self.type_counts.values()
                if tc.type_info.category == cat and tc.count > 0
            ]

            if not cat_types:
                continue

            lines.append(f"\n{cat.icon} {cat.label}:")

            for tc in sorted(cat_types, key=lambda x: -x.count):
                ptype = tc.type_info
                lines.append(f"  {ptype.icon} {ptype.name}: {tc.count}")

        return "\n".join(lines)

    def render_main_projects_input(self) -> Panel:
        """Render the main projects input section."""
        if not RICH_AVAILABLE:
            return "Main Projects: " + ", ".join(self.user_main_projects)

        content = Text()

        if self.user_main_projects:
            content.append("🎯 Main Projects: ", style="bold bright_green")
            content.append(", ".join(self.user_main_projects))
            content.append("\n[dim]These help identify landing pages, admin panels, etc.[/]")
        else:
            content.append("🎯 No main projects specified\n", style="yellow")
            content.append("[dim]Enter your main project names to help identify relationships[/]\n")
            content.append("[dim]Example: 'ER Simulator, Context DNA'[/]")

        return Panel(
            content,
            title="[bold]🎯 YOUR MAIN PROJECTS[/]",
            border_style="bright_green" if self.user_main_projects else "yellow",
            box=ROUNDED,
        )

    def render_relationships_detected(self) -> Optional[Panel]:
        """Render detected relationships between projects."""
        if not RICH_AVAILABLE:
            return None

        relationships = []

        for tc in self.type_counts.values():
            ptype = tc.type_info

            # Check if this type typically relates to main projects
            if ptype.id in ("landing_page", "admin_panel", "test_lab", "branch_sandbox"):
                for proj_path in tc.projects:
                    # Check if related to user's main projects
                    for main in self.user_main_projects:
                        main_lower = main.lower().replace(" ", "").replace("-", "")
                        if main_lower in proj_path.lower().replace("-", "").replace("_", ""):
                            relationships.append({
                                "type": ptype,
                                "project": proj_path,
                                "related_to": main,
                            })

        if not relationships:
            return None

        content = Text()
        for rel in relationships[:5]:  # Show max 5
            content.append(f"{rel['type'].icon} ", style="bold")
            content.append(f"{Path(rel['project']).name}")
            content.append(f" → ", style="dim")
            content.append(f"{rel['related_to']}\n", style="bright_green")

        if len(relationships) > 5:
            content.append(f"[dim]+{len(relationships) - 5} more relationships[/]")

        return Panel(
            content,
            title="[bold]🔗 DETECTED RELATIONSHIPS[/]",
            border_style="cyan",
            box=ROUNDED,
        )

    def render_add_type_dropdown(self) -> Panel:
        """Render dropdown for adding new types to search for."""
        if not RICH_AVAILABLE:
            return "Add Type: (select from list)"

        content = Text()
        content.append("Add a project type to search for:\n\n", style="bold")

        # Show categories as options
        for i, cat in enumerate(ProjectCategory, 1):
            types_in_cat = get_types_by_category(cat)
            detected = sum(
                1 for tc in self.type_counts.values()
                if tc.type_info.category == cat and tc.count > 0
            )

            content.append(f"[{i}] {cat.icon} {cat.label}")
            content.append(f" ({detected}/{len(types_in_cat)} detected)\n", style="dim")

        return Panel(
            content,
            title="[bold]➕ ADD TYPE TO DETECT[/]",
            border_style="bright_magenta",
            box=ROUNDED,
        )

    def render_full_panel(self) -> Group:
        """Render the complete type counts panel for the slider view."""
        if not RICH_AVAILABLE:
            return self._render_plain_badges()

        components = []

        # Compact badges (always shown)
        badge_panel = Panel(
            self.render_compact_badges(),
            border_style="bright_blue",
            box=SIMPLE,
            padding=(0, 1),
        )
        components.append(badge_panel)

        # Main projects input
        if self.user_main_projects:
            main_text = Text()
            main_text.append("🎯 ", style="bold")
            main_text.append(", ".join(self.user_main_projects), style="bright_green")
            components.append(Panel(main_text, box=SIMPLE, padding=(0, 1)))

        # Relationships (if detected and main projects set)
        if self.user_main_projects:
            rel_panel = self.render_relationships_detected()
            if rel_panel:
                components.append(rel_panel)

        return Group(*components)

    # =========================================================================
    # INTERACTIVE EDITING
    # =========================================================================

    def prompt_main_projects(self) -> List[str]:
        """Interactive prompt for main project names."""
        if not RICH_AVAILABLE:
            return self._prompt_main_projects_plain()

        self.console.print(self.render_main_projects_input())

        self.console.print("\n[bold]Enter your main project names[/]")
        self.console.print("[dim]Separate multiple with commas. Press Enter to skip.[/]")

        current = ", ".join(self.user_main_projects) if self.user_main_projects else ""

        raw = Prompt.ask("Main projects", default=current)

        if raw:
            names = [n.strip() for n in raw.split(",") if n.strip()]
            self.set_main_projects(names)
            self.console.print(f"[green]✓[/] Set {len(names)} main project(s)")
            return names

        return self.user_main_projects

    def _prompt_main_projects_plain(self) -> List[str]:
        """Plain text prompt for main projects."""
        print("\nEnter your main project names (comma-separated):")
        raw = input("> ").strip()
        if raw:
            names = [n.strip() for n in raw.split(",") if n.strip()]
            self.set_main_projects(names)
            return names
        return self.user_main_projects

    def prompt_edit_count(self, type_id: str) -> Optional[int]:
        """Interactive prompt to edit a type count."""
        if type_id not in self.type_counts:
            return None

        tc = self.type_counts[type_id]

        if RICH_AVAILABLE:
            self.console.print(f"\n[bold]Editing count for: {tc.type_info.icon} {tc.type_info.name}[/]")
            self.console.print(f"Current count: {tc.count}")
            if tc.user_override is not None:
                self.console.print(f"[yellow]User override: {tc.user_override}[/]")

            try:
                new_count = IntPrompt.ask(
                    "New count",
                    default=tc.user_override or tc.count
                )
                self.update_type_count(type_id, new_count)
                self.console.print(f"[green]✓[/] Updated to {new_count}")
                return new_count
            except ValueError:
                self.console.print("[red]Invalid number[/]")
                return None
        else:
            print(f"\nEditing count for: {tc.type_info.name}")
            print(f"Current count: {tc.count}")
            try:
                new_count = int(input("New count: "))
                self.update_type_count(type_id, new_count)
                return new_count
            except ValueError:
                print("Invalid number")
                return None

    def prompt_add_type(self) -> Optional[str]:
        """Interactive prompt to add a type to search for."""
        if not RICH_AVAILABLE:
            return self._prompt_add_type_plain()

        self.console.print(self.render_add_type_dropdown())

        # Get category choice
        cat_choice = Prompt.ask(
            "Select category",
            choices=[str(i) for i in range(1, len(list(ProjectCategory)) + 1)],
            default="1"
        )

        cat_idx = int(cat_choice) - 1
        category = list(ProjectCategory)[cat_idx]

        # Show types in category
        types_in_cat = get_types_by_category(category)

        self.console.print(f"\n[bold]{category.icon} {category.label} Types:[/]")
        for i, ptype in enumerate(types_in_cat, 1):
            detected = ptype.id in self.type_counts and self.type_counts[ptype.id].count > 0
            marker = "[green]✓[/]" if detected else "[dim]○[/]"
            self.console.print(f"  [{i}] {marker} {ptype.icon} {ptype.name}")

        type_choice = Prompt.ask(
            "Select type to add",
            choices=[str(i) for i in range(1, len(types_in_cat) + 1)],
            default="1"
        )

        type_idx = int(type_choice) - 1
        selected_type = types_in_cat[type_idx]

        self.console.print(f"\n[green]✓[/] Added {selected_type.icon} {selected_type.name} to detection")

        # Add to type counts with 0 count (user wants to track it)
        if selected_type.id not in self.type_counts:
            self.type_counts[selected_type.id] = ProjectTypeCount(
                type_id=selected_type.id,
                type_info=selected_type,
                count=0,
                projects=[],
                deployment_contexts={},
            )

        return selected_type.id

    def _prompt_add_type_plain(self) -> Optional[str]:
        """Plain text prompt for adding type."""
        print("\nCategories:")
        for i, cat in enumerate(ProjectCategory, 1):
            print(f"  [{i}] {cat.icon} {cat.label}")

        try:
            cat_idx = int(input("Select category: ")) - 1
            category = list(ProjectCategory)[cat_idx]

            types_in_cat = get_types_by_category(category)
            print(f"\n{category.label} Types:")
            for i, ptype in enumerate(types_in_cat, 1):
                print(f"  [{i}] {ptype.icon} {ptype.name}")

            type_idx = int(input("Select type: ")) - 1
            selected_type = types_in_cat[type_idx]

            if selected_type.id not in self.type_counts:
                self.type_counts[selected_type.id] = ProjectTypeCount(
                    type_id=selected_type.id,
                    type_info=selected_type,
                    count=0,
                    projects=[],
                    deployment_contexts={},
                )

            return selected_type.id
        except (ValueError, IndexError):
            print("Invalid selection")
            return None

    def run_interactive_edit_mode(self):
        """Run full interactive editing mode."""
        if not RICH_AVAILABLE:
            self._run_plain_edit_mode()
            return

        self.console.clear()

        self.console.print(Panel(
            "[bold bright_blue]📊 PROJECT TYPE EDITOR[/]\n\n"
            "Adjust detected project types, set main projects,\n"
            "and help the system understand your codebase better.",
            border_style="bright_blue",
            box=DOUBLE,
        ))

        while True:
            self.console.print("\n")
            self.console.print(self.render_full_panel())

            if self.show_detailed:
                self.console.print(self.render_detailed_table())

            self.console.print("\n[bold]Options:[/]")
            self.console.print("  [M] Set main projects")
            self.console.print("  [E] Toggle detailed view")
            self.console.print("  [A] Add type to search")
            self.console.print("  [#] Edit specific type count")
            self.console.print("  [Q] Done editing")

            choice = Prompt.ask("Choice", default="q").lower()

            if choice == "q":
                break
            elif choice == "m":
                self.prompt_main_projects()
            elif choice == "e":
                self.show_detailed = not self.show_detailed
            elif choice == "a":
                self.prompt_add_type()
            elif choice.isdigit():
                # Get type by row number
                type_ids = list(self.type_counts.keys())
                if 1 <= int(choice) <= len(type_ids):
                    self.prompt_edit_count(type_ids[int(choice) - 1])

        self.console.print("\n[green]✓[/] Type configuration saved")

    def _run_plain_edit_mode(self):
        """Plain text edit mode."""
        print("\n" + "=" * 40)
        print("PROJECT TYPE EDITOR")
        print("=" * 40)

        while True:
            print(self._render_plain_badges())
            print(self._render_plain_table())

            print("\nOptions: [M]ain projects, [A]dd type, [Q]uit")
            choice = input("Choice: ").lower()

            if choice == "q":
                break
            elif choice == "m":
                self.prompt_main_projects()
            elif choice == "a":
                self.prompt_add_type()


# =============================================================================
# DEPLOYMENT CONTEXT SELECTOR
# =============================================================================

class DeploymentContextSelector:
    """
    UI for selecting deployment context for projects.

    Shows context options with descriptions and allows
    batch assignment to multiple projects.
    """

    def __init__(self, console: Optional[Console] = None):
        self.console = console or (Console() if RICH_AVAILABLE else None)

    def render_context_options(self) -> Panel:
        """Render all deployment context options."""
        if not RICH_AVAILABLE:
            return self._render_plain_contexts()

        table = Table(show_header=True, header_style="bold", box=ROUNDED)
        table.add_column("#", width=3)
        table.add_column("Context", width=25)
        table.add_column("Description")

        for i, ctx in enumerate(DeploymentContext, 1):
            table.add_row(
                str(i),
                f"{ctx.icon} {ctx.label}",
                ctx.description,
            )

        return Panel(
            table,
            title="[bold]🎭 DEPLOYMENT CONTEXTS[/]",
            border_style="bright_magenta",
        )

    def _render_plain_contexts(self) -> str:
        """Plain text context rendering."""
        lines = ["DEPLOYMENT CONTEXTS", "=" * 40]
        for i, ctx in enumerate(DeploymentContext, 1):
            lines.append(f"  [{i}] {ctx.icon} {ctx.label}: {ctx.description}")
        return "\n".join(lines)

    def prompt_select_context(self) -> Optional[DeploymentContext]:
        """Prompt user to select a deployment context."""
        if RICH_AVAILABLE:
            self.console.print(self.render_context_options())

            choice = Prompt.ask(
                "Select context",
                choices=[str(i) for i in range(1, len(list(DeploymentContext)) + 1)],
                default="1"
            )

            return list(DeploymentContext)[int(choice) - 1]
        else:
            print(self._render_plain_contexts())
            try:
                choice = int(input("Select context: "))
                return list(DeploymentContext)[choice - 1]
            except (ValueError, IndexError):
                return None


# =============================================================================
# CLI TEST
# =============================================================================

def test_type_counts_ui():
    """Test the type counts UI."""
    from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer

    print("=" * 60)
    print("🧪 TYPE COUNTS UI TEST")
    print("=" * 60)

    # Create mock projects for testing
    mock_projects = [
        DetectedProject(
            path="backend",
            name="backend",
            confidence=0.95,
            description="Django backend service",
            markers_found=["manage.py", "wsgi.py"],
            framework="django",
            language="python",
        ),
        DetectedProject(
            path="landing-page",
            name="landing-page",
            confidence=0.8,
            description="Marketing landing page",
            markers_found=["index.html"],
            framework="html",
            language="html",
        ),
        DetectedProject(
            path="admin.contextdna.io",
            name="admin.contextdna.io",
            confidence=0.85,
            description="Admin dashboard",
            markers_found=["next.config.js"],
            framework="nextjs",
            language="javascript",
        ),
        DetectedProject(
            path="sim-frontend",
            name="sim-frontend",
            confidence=0.9,
            description="React Native mobile app",
            markers_found=["app.json", "metro.config.js"],
            framework="react-native",
            language="javascript",
        ),
    ]

    # Create panel
    panel = TypeCountsPanel(projects=mock_projects)

    # Test setting main projects
    panel.set_main_projects(["ER Simulator", "Context DNA"])

    if RICH_AVAILABLE:
        console = Console()
        console.print(panel.render_full_panel())
        console.print("\n")
        console.print(panel.render_detailed_table())
        console.print("\n")
        console.print(panel.render_main_projects_input())

        rel_panel = panel.render_relationships_detected()
        if rel_panel:
            console.print(rel_panel)
    else:
        print(panel._render_plain_badges())
        print(panel._render_plain_table())

    print("\n✅ Test complete!")


if __name__ == "__main__":
    test_type_counts_ui()
