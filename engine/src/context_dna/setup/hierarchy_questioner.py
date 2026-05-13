#!/usr/bin/env python3
"""
Context DNA Hierarchy Questioner

Interactive clarification and free text input for complex codebase structures.
Asks smart questions to understand what detected projects actually ARE and
suggests potential configurations based on user answers.

Usage:
    # CLI test mode
    python -m context_dna.setup.hierarchy_questioner --test /path/to/repo

    # Python
    from context_dna.setup.hierarchy_questioner import HierarchyQuestioner
    questioner = HierarchyQuestioner(projects)
    clarified = questioner.run_interactive()
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer, DetectedProject


# =============================================================================
# PROJECT RELATIONSHIP TYPES
# =============================================================================

class ProjectRelationship(Enum):
    """How projects relate to each other."""
    STANDALONE = "standalone"           # Independent project
    MONOREPO_PACKAGE = "monorepo_pkg"   # Part of monorepo
    SHARED_LIBRARY = "shared_lib"       # Shared code
    INFRASTRUCTURE = "infrastructure"    # Infra/DevOps, not code
    LANDING_PAGE = "landing_page"        # Marketing/docs site
    VERSION_BRANCH = "version_branch"    # Different version of same project
    ARCHIVED = "archived"                # Old/deprecated
    SUBMODULE = "submodule"              # Git submodule
    EXAMPLE = "example"                  # Demo/example code


class TrackingPreference(Enum):
    """How Context DNA should track this project."""
    FULL_TRACKING = "full"              # Full context learning
    LIGHT_TRACKING = "light"            # Basic patterns only
    NO_TRACKING = "none"                # Don't track
    ASK_LATER = "ask_later"             # Decide later


@dataclass
class ClarifiedProject:
    """A project with user clarification."""
    project: DetectedProject
    relationship: ProjectRelationship
    tracking: TrackingPreference
    user_description: str = ""          # Free text from user
    suggested_purpose: str = ""         # Our suggestion
    parent_project: Optional[str] = None  # If part of another project
    related_projects: List[str] = field(default_factory=list)


# =============================================================================
# SUGGESTED CONFIGURATIONS
# =============================================================================

@dataclass
class SuggestedSetup:
    """A potential configuration based on detected patterns."""
    name: str
    description: str
    primary_project: str
    included_projects: List[str]
    excluded_projects: List[str]
    reasoning: str


# =============================================================================
# QUESTIONER
# =============================================================================

class HierarchyQuestioner:
    """
    Interactive questioner for complex codebases.

    Core philosophy:
    - Ask only when necessary (don't annoy)
    - Present clear options
    - Always allow free text override
    - Suggest configurations based on patterns
    """

    def __init__(
        self,
        projects: List[DetectedProject],
        auto_mode: bool = False,
        quiet: bool = False,
    ):
        """
        Initialize questioner.

        Args:
            projects: List of detected projects
            auto_mode: If True, use smart defaults without asking
            quiet: If True, minimize output
        """
        self.projects = projects
        self.auto_mode = auto_mode
        self.quiet = quiet
        self.clarified: Dict[str, ClarifiedProject] = {}
        self.suggested_setups: List[SuggestedSetup] = []

    def run_interactive(self) -> List[ClarifiedProject]:
        """
        Run interactive clarification.

        Returns:
            List of clarified projects with user input
        """
        self._print_header()

        # Phase 1: Show what we detected
        self._print_phase("Phase 1: What I Detected")
        self._show_detected_projects()

        # Phase 2: Generate suggested configurations
        self._print_phase("Phase 2: Suggested Configurations")
        self.suggested_setups = self._generate_suggested_setups()
        self._show_suggested_setups()

        # Phase 3: Ask about uncertain projects
        self._print_phase("Phase 3: Clarification Questions")
        uncertain = [p for p in self.projects if p.needs_clarification]
        if uncertain:
            self._clarify_uncertain_projects(uncertain)
        else:
            self._print("   ✓ No clarification needed - all projects identified")

        # Phase 4: Free text input
        self._print_phase("Phase 4: Additional Context")
        self._get_free_text_input()

        # Phase 5: Choose configuration
        self._print_phase("Phase 5: Choose Your Setup")
        self._select_configuration()

        return list(self.clarified.values())

    # -------------------------------------------------------------------------
    # Phase 1: Show Detected Projects
    # -------------------------------------------------------------------------

    def _show_detected_projects(self):
        """Display all detected projects."""
        # Group by type
        submodules = [p for p in self.projects if p.is_submodule]
        standalone = [p for p in self.projects if not p.is_submodule and not p.needs_clarification]
        uncertain = [p for p in self.projects if p.needs_clarification]

        self._print(f"\n   Found {len(self.projects)} potential projects:\n")

        if submodules:
            self._print("   📦 GIT SUBMODULES (definitely separate):")
            for p in submodules:
                self._print(f"      • {p.name} → {p.description}")

        if standalone:
            self._print("\n   📁 IDENTIFIED PROJECTS:")
            for p in standalone:
                self._print(f"      • {p.name}: {p.description} [{p.confidence:.0%}]")

        if uncertain:
            self._print("\n   ⚠️ NEEDS CLARIFICATION:")
            for p in uncertain:
                self._print(f"      • {p.name}: {p.description} (uncertain)")

    # -------------------------------------------------------------------------
    # Phase 2: Generate Suggested Setups
    # -------------------------------------------------------------------------

    def _generate_suggested_setups(self) -> List[SuggestedSetup]:
        """Generate potential configurations based on detected patterns."""
        setups = []

        # Find potential main projects
        main_candidates = [
            p for p in self.projects
            if p.framework in ('django', 'nextjs', 'rails', 'spring')
            or p.language == 'python' and p.confidence > 0.8
        ]

        # Setup 1: Full Monorepo (track everything)
        setups.append(SuggestedSetup(
            name="Full Workspace",
            description="Track ALL projects as one unified workspace",
            primary_project=self.projects[0].path if self.projects else '.',
            included_projects=[p.path for p in self.projects],
            excluded_projects=[],
            reasoning="Best for teams where everything is interconnected",
        ))

        # Setup 2: Main Project Focus
        if main_candidates:
            main = main_candidates[0]
            infra_projects = [p.path for p in self.projects if 'infra' in p.description.lower()]
            setups.append(SuggestedSetup(
                name="Main Project Focus",
                description=f"Focus on {main.name} with supporting infrastructure",
                primary_project=main.path,
                included_projects=[main.path] + infra_projects,
                excluded_projects=[p.path for p in self.projects if p.path not in [main.path] + infra_projects],
                reasoning=f"Best when {main.name} is your primary development focus",
            ))

        # Setup 3: Backend + Frontend Split
        backend_projects = [p for p in self.projects if p.framework == 'django' or 'backend' in p.path.lower()]
        frontend_projects = [p for p in self.projects if p.framework in ('nextjs', 'vue', 'react', 'angular') or 'frontend' in p.path.lower()]

        if backend_projects and frontend_projects:
            setups.append(SuggestedSetup(
                name="Backend + Frontend",
                description="Separate tracking for backend and frontend",
                primary_project=backend_projects[0].path,
                included_projects=[p.path for p in backend_projects + frontend_projects],
                excluded_projects=[p.path for p in self.projects if p not in backend_projects + frontend_projects],
                reasoning="Best for full-stack development with separate concerns",
            ))

        # Setup 4: Exclude submodules (they're independent)
        non_submodules = [p for p in self.projects if not p.is_submodule]
        submodules = [p for p in self.projects if p.is_submodule]
        if submodules and non_submodules:
            setups.append(SuggestedSetup(
                name="Superrepo Only",
                description="Track superrepo, let submodules be independent",
                primary_project='.',
                included_projects=[p.path for p in non_submodules],
                excluded_projects=[p.path for p in submodules],
                reasoning="Submodules have their own repos - track them there",
            ))

        return setups

    def _show_suggested_setups(self):
        """Display suggested configurations."""
        if not self.suggested_setups:
            return

        self._print(f"\n   Based on your structure, here are potential setups:\n")

        for i, setup in enumerate(self.suggested_setups, 1):
            self._print(f"   [{i}] {setup.name}")
            self._print(f"       {setup.description}")
            self._print(f"       💡 {setup.reasoning}")
            self._print("")

    # -------------------------------------------------------------------------
    # Phase 3: Clarify Uncertain Projects
    # -------------------------------------------------------------------------

    def _clarify_uncertain_projects(self, uncertain: List[DetectedProject]):
        """Ask about projects needing clarification."""
        if self.auto_mode:
            for p in uncertain:
                self._auto_classify(p)
            return

        self._print(f"\n   I found {len(uncertain)} projects I'm not sure about.\n")

        for p in uncertain:
            self._ask_about_project(p)

    def _ask_about_project(self, project: DetectedProject):
        """Ask user about a specific project."""
        self._print(f"\n   ❓ About: {project.name}")
        self._print(f"      Path: {project.path}")
        self._print(f"      Found: {', '.join(project.markers_found[:3])}")
        self._print("")

        options = [
            ("1", "Infrastructure/DevOps (Terraform, Docker, etc.)", ProjectRelationship.INFRASTRUCTURE),
            ("2", "Standalone application", ProjectRelationship.STANDALONE),
            ("3", "Shared library/package", ProjectRelationship.SHARED_LIBRARY),
            ("4", "Landing page / marketing site", ProjectRelationship.LANDING_PAGE),
            ("5", "Old/archived version (don't track)", ProjectRelationship.ARCHIVED),
            ("6", "Let me explain (free text)", None),
        ]

        for key, label, _ in options:
            self._print(f"      [{key}] {label}")

        self._print("")

        if self.auto_mode:
            choice = "1" if "docker" in project.description.lower() else "2"
        else:
            choice = input("      Your choice [1-6]: ").strip() or "2"

        # Handle free text
        if choice == "6":
            description = input("      Please describe this project: ").strip()
            relationship = self._infer_relationship(description)
        else:
            relationship = options[int(choice) - 1][2] if choice.isdigit() and 1 <= int(choice) <= 5 else ProjectRelationship.STANDALONE
            description = ""

        # Determine tracking preference
        tracking = TrackingPreference.NO_TRACKING if relationship == ProjectRelationship.ARCHIVED else TrackingPreference.FULL_TRACKING

        self.clarified[project.path] = ClarifiedProject(
            project=project,
            relationship=relationship,
            tracking=tracking,
            user_description=description,
        )

        self._print(f"      ✓ Classified as: {relationship.value}")

    def _infer_relationship(self, description: str) -> ProjectRelationship:
        """Infer relationship from free text description."""
        desc_lower = description.lower()

        if any(word in desc_lower for word in ['old', 'archived', 'deprecated', 'backup']):
            return ProjectRelationship.ARCHIVED
        if any(word in desc_lower for word in ['infra', 'terraform', 'docker', 'devops', 'deploy']):
            return ProjectRelationship.INFRASTRUCTURE
        if any(word in desc_lower for word in ['landing', 'marketing', 'website', 'docs']):
            return ProjectRelationship.LANDING_PAGE
        if any(word in desc_lower for word in ['shared', 'common', 'library', 'utils']):
            return ProjectRelationship.SHARED_LIBRARY

        return ProjectRelationship.STANDALONE

    def _auto_classify(self, project: DetectedProject):
        """Auto-classify without user input."""
        desc_lower = project.description.lower()

        if 'docker' in desc_lower or 'terraform' in desc_lower:
            relationship = ProjectRelationship.INFRASTRUCTURE
        elif 'old' in project.name.lower() or 'backup' in project.name.lower():
            relationship = ProjectRelationship.ARCHIVED
        else:
            relationship = ProjectRelationship.STANDALONE

        tracking = TrackingPreference.NO_TRACKING if relationship == ProjectRelationship.ARCHIVED else TrackingPreference.LIGHT_TRACKING

        self.clarified[project.path] = ClarifiedProject(
            project=project,
            relationship=relationship,
            tracking=tracking,
            suggested_purpose=f"Auto-classified as {relationship.value}",
        )

    # -------------------------------------------------------------------------
    # Phase 4: Free Text Input
    # -------------------------------------------------------------------------

    def _get_free_text_input(self):
        """Allow user to provide additional context."""
        if self.auto_mode:
            return

        self._print("\n   📝 Is there anything else I should know about your codebase?")
        self._print("      (Press Enter to skip, or type your notes)")
        self._print("")

        additional = input("      > ").strip()

        if additional:
            self._print(f"\n      ✓ Got it! I'll factor that in.")
            # Store for later use in configuration
            self._additional_context = additional
        else:
            self._print("      (Skipped)")

    # -------------------------------------------------------------------------
    # Phase 5: Select Configuration
    # -------------------------------------------------------------------------

    def _select_configuration(self):
        """Let user select final configuration."""
        if not self.suggested_setups:
            return

        if self.auto_mode:
            choice = 1
        else:
            self._print("\n   Which setup would you like to use?\n")

            for i, setup in enumerate(self.suggested_setups, 1):
                self._print(f"   [{i}] {setup.name}")

            self._print(f"   [C] Custom (I'll configure manually)")
            self._print("")

            raw_choice = input("   Your choice [1]: ").strip() or "1"
            choice = int(raw_choice) if raw_choice.isdigit() else 0

        if 1 <= choice <= len(self.suggested_setups):
            selected = self.suggested_setups[choice - 1]
            self._print(f"\n   ✓ Selected: {selected.name}")
            self._print(f"     Primary: {selected.primary_project}")
            self._print(f"     Included: {len(selected.included_projects)} projects")
            self._print(f"     Excluded: {len(selected.excluded_projects)} projects")

            # Mark projects based on selection
            for p in self.projects:
                if p.path not in self.clarified:
                    tracking = TrackingPreference.FULL_TRACKING if p.path in selected.included_projects else TrackingPreference.NO_TRACKING
                    self.clarified[p.path] = ClarifiedProject(
                        project=p,
                        relationship=ProjectRelationship.SUBMODULE if p.is_submodule else ProjectRelationship.STANDALONE,
                        tracking=tracking,
                    )

    # -------------------------------------------------------------------------
    # Output Utilities
    # -------------------------------------------------------------------------

    def _print(self, msg: str):
        """Print message (respects quiet mode)."""
        if not self.quiet:
            print(msg)

    def _print_header(self):
        """Print questioner header."""
        self._print("")
        self._print("╔" + "═" * 58 + "╗")
        self._print("║" + " 🧬 CONTEXT DNA - CODEBASE CLARIFICATION ".center(58) + "║")
        self._print("╚" + "═" * 58 + "╝")
        self._print("")

    def _print_phase(self, phase: str):
        """Print phase header."""
        self._print("")
        self._print(f"━━━ {phase} ━━━")


# =============================================================================
# CLI INTERFACE
# =============================================================================

def test_questioner(path: str = '.', auto: bool = False):
    """
    Test the questioner on a given path.
    """
    path = Path(path).resolve()

    print()
    print("=" * 70)
    print("🧪 HIERARCHY QUESTIONER TEST")
    print("=" * 70)
    print(f"Path: {path}")
    print()

    # First detect projects
    analyzer = HierarchyAnalyzer(path)
    projects = analyzer.detect_all_projects(max_depth=2)

    if not projects:
        print("❌ No projects detected")
        return False

    print(f"Detected {len(projects)} projects")

    # Run questioner
    questioner = HierarchyQuestioner(projects, auto_mode=auto)
    clarified = questioner.run_interactive()

    print()
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)

    for cp in clarified:
        track_emoji = "✅" if cp.tracking == TrackingPreference.FULL_TRACKING else ("💡" if cp.tracking == TrackingPreference.LIGHT_TRACKING else "⏭️")
        print(f"\n{track_emoji} {cp.project.name}")
        print(f"   Type: {cp.relationship.value}")
        print(f"   Tracking: {cp.tracking.value}")
        if cp.user_description:
            print(f"   User note: {cp.user_description}")

    return True


if __name__ == '__main__':
    args = sys.argv[1:]

    auto = '--auto' in args
    path = [a for a in args if not a.startswith('-')]
    path = path[0] if path else '.'

    if '--test' in args:
        success = test_questioner(path, auto=True)
        sys.exit(0 if success else 1)
    else:
        success = test_questioner(path, auto=auto)
        sys.exit(0 if success else 1)
