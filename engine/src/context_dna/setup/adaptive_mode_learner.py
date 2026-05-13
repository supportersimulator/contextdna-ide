#!/usr/bin/env python3
"""
Context DNA: Adaptive Mode Learning System

The 5 hierarchy sorting philosophies are STARTING POINTS, not rigid rules.
Real developers have their own unique organization methods that evolve over time.

This module learns from user behavior to adapt the sorting within any mode:
- Observes when users reorder projects
- Learns preferred groupings and naming
- Adapts to custom categories the user creates
- Remembers project relationships the user defines
- Evolves with the codebase over time

Philosophy: Context DNA adapts to YOU, not the other way around.
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Set
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from collections import defaultdict

# Rich library for UI
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.table import Table
    from rich.prompt import Prompt, Confirm
    from rich.box import ROUNDED
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


# =============================================================================
# USER PREFERENCE TYPES
# =============================================================================

@dataclass
class ProjectPreference:
    """User's preference for a specific project."""
    project_name: str
    custom_group: Optional[str] = None      # User's custom group name
    custom_order: Optional[int] = None      # User's preferred position
    custom_type: Optional[str] = None       # User's type override
    pinned: bool = False                    # Always show at top
    hidden: bool = False                    # User chose to hide
    related_to: List[str] = field(default_factory=list)  # User-defined relationships
    notes: str = ""                         # User's notes about this project
    last_modified: str = ""                 # When preference was set


@dataclass
class CustomGroup:
    """A user-defined grouping of projects."""
    name: str
    icon: str = "📁"
    color: str = "white"
    projects: List[str] = field(default_factory=list)
    order: int = 0                          # Position among groups
    description: str = ""
    created_at: str = ""


@dataclass
class LearnedPattern:
    """A pattern learned from user behavior."""
    pattern_type: str                       # "reorder", "group", "rename", "relate"
    evidence: List[str] = field(default_factory=list)  # What we observed
    confidence: float = 0.0                 # How confident we are
    times_observed: int = 0                 # How often we've seen this
    last_observed: str = ""


@dataclass
class UserOrganizationProfile:
    """Complete profile of a user's organization preferences."""
    # Base mode preference (1-5, or 0 for fully custom)
    preferred_mode: int = 1

    # Project-level preferences
    project_preferences: Dict[str, ProjectPreference] = field(default_factory=dict)

    # Custom groups the user has created
    custom_groups: Dict[str, CustomGroup] = field(default_factory=dict)

    # Learned patterns from observation
    learned_patterns: List[LearnedPattern] = field(default_factory=list)

    # User's custom sorting rules
    sort_rules: List[Dict[str, Any]] = field(default_factory=list)

    # Vocabulary the user uses (for matching their mental model)
    vocabulary: Dict[str, List[str]] = field(default_factory=dict)

    # Statistics
    total_interactions: int = 0
    profile_version: int = 1
    created_at: str = ""
    last_updated: str = ""


# =============================================================================
# ADAPTIVE MODE LEARNER
# =============================================================================

class AdaptiveModeLearner:
    """
    Learns and adapts to user's actual organization preferences.

    The 5 modes are starting points. This system observes how users
    actually organize their projects and evolves to match their mental model.

    Learning signals:
    - When user reorders projects (drag-drop, manual sort)
    - When user creates custom groups
    - When user renames/relabels things
    - When user pins or hides projects
    - When user defines relationships ("this goes with that")
    - File access patterns from IDE (what files are opened together)
    """

    # Default profile storage location
    PROFILE_PATH = ".context-dna/user_organization.json"

    # Confidence thresholds
    CONFIDENCE_SUGGEST = 0.6    # Suggest but don't auto-apply
    CONFIDENCE_AUTO = 0.85      # Auto-apply (user can override)

    def __init__(self, root_path: Path = None, console: Console = None):
        self.root = root_path or Path.cwd()
        self.console = console or (Console() if RICH_AVAILABLE else None)

        # Load or create user profile
        self.profile = self._load_or_create_profile()

        # Session tracking (not persisted)
        self.session_changes: List[Dict[str, Any]] = []
        self.session_start = datetime.utcnow().isoformat()

    def _get_profile_path(self) -> Path:
        """Get path to user's organization profile."""
        return self.root / self.PROFILE_PATH

    def _load_or_create_profile(self) -> UserOrganizationProfile:
        """Load existing profile or create new one."""
        profile_path = self._get_profile_path()

        if profile_path.exists():
            try:
                with open(profile_path) as f:
                    data = json.load(f)
                return self._dict_to_profile(data)
            except (json.JSONDecodeError, KeyError) as e:
                # Corrupted profile - start fresh but warn
                if self.console:
                    self.console.print(f"[yellow]Note: Reset organization profile (was corrupted)[/]")

        # Create new profile
        return UserOrganizationProfile(
            created_at=datetime.utcnow().isoformat(),
            last_updated=datetime.utcnow().isoformat(),
        )

    def _dict_to_profile(self, data: Dict) -> UserOrganizationProfile:
        """Convert dict to UserOrganizationProfile."""
        profile = UserOrganizationProfile()
        profile.preferred_mode = data.get('preferred_mode', 1)
        profile.total_interactions = data.get('total_interactions', 0)
        profile.profile_version = data.get('profile_version', 1)
        profile.created_at = data.get('created_at', '')
        profile.last_updated = data.get('last_updated', '')

        # Load project preferences
        for name, pref_data in data.get('project_preferences', {}).items():
            profile.project_preferences[name] = ProjectPreference(
                project_name=name,
                custom_group=pref_data.get('custom_group'),
                custom_order=pref_data.get('custom_order'),
                custom_type=pref_data.get('custom_type'),
                pinned=pref_data.get('pinned', False),
                hidden=pref_data.get('hidden', False),
                related_to=pref_data.get('related_to', []),
                notes=pref_data.get('notes', ''),
                last_modified=pref_data.get('last_modified', ''),
            )

        # Load custom groups
        for name, group_data in data.get('custom_groups', {}).items():
            profile.custom_groups[name] = CustomGroup(
                name=name,
                icon=group_data.get('icon', '📁'),
                color=group_data.get('color', 'white'),
                projects=group_data.get('projects', []),
                order=group_data.get('order', 0),
                description=group_data.get('description', ''),
                created_at=group_data.get('created_at', ''),
            )

        # Load learned patterns
        for pattern_data in data.get('learned_patterns', []):
            profile.learned_patterns.append(LearnedPattern(
                pattern_type=pattern_data.get('pattern_type', ''),
                evidence=pattern_data.get('evidence', []),
                confidence=pattern_data.get('confidence', 0.0),
                times_observed=pattern_data.get('times_observed', 0),
                last_observed=pattern_data.get('last_observed', ''),
            ))

        profile.sort_rules = data.get('sort_rules', [])
        profile.vocabulary = data.get('vocabulary', {})

        return profile

    def _profile_to_dict(self) -> Dict:
        """Convert profile to dict for saving."""
        return {
            'preferred_mode': self.profile.preferred_mode,
            'total_interactions': self.profile.total_interactions,
            'profile_version': self.profile.profile_version,
            'created_at': self.profile.created_at,
            'last_updated': self.profile.last_updated,
            'project_preferences': {
                name: {
                    'custom_group': pref.custom_group,
                    'custom_order': pref.custom_order,
                    'custom_type': pref.custom_type,
                    'pinned': pref.pinned,
                    'hidden': pref.hidden,
                    'related_to': pref.related_to,
                    'notes': pref.notes,
                    'last_modified': pref.last_modified,
                }
                for name, pref in self.profile.project_preferences.items()
            },
            'custom_groups': {
                name: {
                    'icon': group.icon,
                    'color': group.color,
                    'projects': group.projects,
                    'order': group.order,
                    'description': group.description,
                    'created_at': group.created_at,
                }
                for name, group in self.profile.custom_groups.items()
            },
            'learned_patterns': [
                {
                    'pattern_type': p.pattern_type,
                    'evidence': p.evidence,
                    'confidence': p.confidence,
                    'times_observed': p.times_observed,
                    'last_observed': p.last_observed,
                }
                for p in self.profile.learned_patterns
            ],
            'sort_rules': self.profile.sort_rules,
            'vocabulary': self.profile.vocabulary,
        }

    def save_profile(self):
        """Persist profile to disk."""
        profile_path = self._get_profile_path()
        profile_path.parent.mkdir(parents=True, exist_ok=True)

        self.profile.last_updated = datetime.utcnow().isoformat()

        with open(profile_path, 'w') as f:
            json.dump(self._profile_to_dict(), f, indent=2)

    # =========================================================================
    # LEARNING FROM USER ACTIONS
    # =========================================================================

    def observe_reorder(self, project_name: str, new_position: int, context: str = ""):
        """
        User reordered a project - learn from this.

        Args:
            project_name: Which project was moved
            new_position: Where it was moved to (0-indexed)
            context: What mode/view this happened in
        """
        self.profile.total_interactions += 1

        # Update project preference
        if project_name not in self.profile.project_preferences:
            self.profile.project_preferences[project_name] = ProjectPreference(
                project_name=project_name
            )

        pref = self.profile.project_preferences[project_name]
        pref.custom_order = new_position
        pref.last_modified = datetime.utcnow().isoformat()

        # Record for pattern learning
        self.session_changes.append({
            'action': 'reorder',
            'project': project_name,
            'position': new_position,
            'context': context,
            'timestamp': datetime.utcnow().isoformat(),
        })

        # Look for patterns
        self._learn_from_reorders()

    def observe_grouping(self, project_name: str, group_name: str, is_custom: bool = False):
        """
        User put a project in a group - learn from this.

        Args:
            project_name: Which project
            group_name: Which group it was put in
            is_custom: Whether user created this group
        """
        self.profile.total_interactions += 1

        # Update project preference
        if project_name not in self.profile.project_preferences:
            self.profile.project_preferences[project_name] = ProjectPreference(
                project_name=project_name
            )

        pref = self.profile.project_preferences[project_name]
        pref.custom_group = group_name
        pref.last_modified = datetime.utcnow().isoformat()

        # If custom group, ensure it exists
        if is_custom and group_name not in self.profile.custom_groups:
            self.profile.custom_groups[group_name] = CustomGroup(
                name=group_name,
                created_at=datetime.utcnow().isoformat(),
            )

        if group_name in self.profile.custom_groups:
            if project_name not in self.profile.custom_groups[group_name].projects:
                self.profile.custom_groups[group_name].projects.append(project_name)

        self.session_changes.append({
            'action': 'group',
            'project': project_name,
            'group': group_name,
            'is_custom': is_custom,
            'timestamp': datetime.utcnow().isoformat(),
        })

    def observe_relationship(self, project_a: str, project_b: str, relationship: str = "related"):
        """
        User indicated two projects are related.

        Args:
            project_a: First project
            project_b: Second project
            relationship: Type of relationship (e.g., "related", "depends_on", "tests")
        """
        self.profile.total_interactions += 1

        # Update both projects
        for proj in [project_a, project_b]:
            if proj not in self.profile.project_preferences:
                self.profile.project_preferences[proj] = ProjectPreference(project_name=proj)

        # Add bidirectional relationship
        pref_a = self.profile.project_preferences[project_a]
        pref_b = self.profile.project_preferences[project_b]

        if project_b not in pref_a.related_to:
            pref_a.related_to.append(project_b)
        if project_a not in pref_b.related_to:
            pref_b.related_to.append(project_a)

        pref_a.last_modified = datetime.utcnow().isoformat()
        pref_b.last_modified = datetime.utcnow().isoformat()

        self.session_changes.append({
            'action': 'relate',
            'project_a': project_a,
            'project_b': project_b,
            'relationship': relationship,
            'timestamp': datetime.utcnow().isoformat(),
        })

    def observe_pin(self, project_name: str, pinned: bool = True):
        """User pinned/unpinned a project."""
        self.profile.total_interactions += 1

        if project_name not in self.profile.project_preferences:
            self.profile.project_preferences[project_name] = ProjectPreference(
                project_name=project_name
            )

        self.profile.project_preferences[project_name].pinned = pinned
        self.profile.project_preferences[project_name].last_modified = datetime.utcnow().isoformat()

    def observe_hide(self, project_name: str, hidden: bool = True):
        """User hid/unhid a project."""
        self.profile.total_interactions += 1

        if project_name not in self.profile.project_preferences:
            self.profile.project_preferences[project_name] = ProjectPreference(
                project_name=project_name
            )

        self.profile.project_preferences[project_name].hidden = hidden
        self.profile.project_preferences[project_name].last_modified = datetime.utcnow().isoformat()

    def observe_type_override(self, project_name: str, custom_type: str):
        """User specified a custom type for a project."""
        self.profile.total_interactions += 1

        if project_name not in self.profile.project_preferences:
            self.profile.project_preferences[project_name] = ProjectPreference(
                project_name=project_name
            )

        pref = self.profile.project_preferences[project_name]
        pref.custom_type = custom_type
        pref.last_modified = datetime.utcnow().isoformat()

        # Learn vocabulary
        if 'types' not in self.profile.vocabulary:
            self.profile.vocabulary['types'] = []
        if custom_type not in self.profile.vocabulary['types']:
            self.profile.vocabulary['types'].append(custom_type)

    def observe_mode_preference(self, mode: int):
        """User selected a preferred mode."""
        self.profile.preferred_mode = mode
        self.profile.total_interactions += 1

    # =========================================================================
    # PATTERN LEARNING
    # =========================================================================

    def _learn_from_reorders(self):
        """Analyze reorders to find patterns."""
        reorders = [c for c in self.session_changes if c['action'] == 'reorder']

        if len(reorders) < 2:
            return

        # Pattern: Same projects always ordered together
        # (e.g., user always puts frontend right after backend)
        ordered_projects = [r['project'] for r in reorders]

        # Look for consistent adjacency
        for i in range(len(ordered_projects) - 1):
            pair = (ordered_projects[i], ordered_projects[i + 1])

            # Check if we've seen this pair before
            existing = None
            for pattern in self.profile.learned_patterns:
                if pattern.pattern_type == 'adjacency' and set(pattern.evidence) == set(pair):
                    existing = pattern
                    break

            if existing:
                existing.times_observed += 1
                existing.confidence = min(0.95, existing.confidence + 0.1)
                existing.last_observed = datetime.utcnow().isoformat()
            else:
                self.profile.learned_patterns.append(LearnedPattern(
                    pattern_type='adjacency',
                    evidence=list(pair),
                    confidence=0.3,
                    times_observed=1,
                    last_observed=datetime.utcnow().isoformat(),
                ))

    # =========================================================================
    # APPLYING LEARNED PREFERENCES
    # =========================================================================

    def apply_preferences(
        self,
        projects: List[Any],
        base_groups: Dict[str, List[Any]],
    ) -> Dict[str, List[Any]]:
        """
        Apply user preferences to a project grouping.

        Takes the base grouping from any mode and adapts it based on
        what we've learned about the user's preferences.

        Args:
            projects: List of detected projects
            base_groups: Grouping from the base mode (e.g., confidence mode)

        Returns:
            Adapted grouping with user preferences applied
        """
        adapted_groups = {}
        project_lookup = {p.name: p for p in projects}

        # Step 1: Handle pinned projects (always at top)
        pinned_projects = []
        for name, pref in self.profile.project_preferences.items():
            if pref.pinned and name in project_lookup:
                pinned_projects.append(project_lookup[name])

        if pinned_projects:
            adapted_groups["📌 Pinned"] = pinned_projects

        # Step 2: Apply custom groups
        for group_name, custom_group in self.profile.custom_groups.items():
            group_projects = []
            for proj_name in custom_group.projects:
                if proj_name in project_lookup:
                    proj = project_lookup[proj_name]
                    # Don't duplicate pinned projects
                    if proj not in pinned_projects:
                        group_projects.append(proj)

            if group_projects:
                display_name = f"{custom_group.icon} {group_name}"
                adapted_groups[display_name] = group_projects

        # Step 3: Apply base groups with custom ordering
        for group_name, group_projects in base_groups.items():
            remaining = []
            for proj in group_projects:
                # Skip if already in pinned or custom group
                if proj in pinned_projects:
                    continue
                if any(proj.name in g.projects for g in self.profile.custom_groups.values()):
                    continue
                # Skip hidden
                if proj.name in self.profile.project_preferences:
                    if self.profile.project_preferences[proj.name].hidden:
                        continue
                remaining.append(proj)

            if remaining:
                # Apply custom ordering within group
                remaining = self._apply_custom_order(remaining)
                adapted_groups[group_name] = remaining

        # Step 4: Handle hidden projects (show in collapsed section if any)
        hidden = []
        for name, pref in self.profile.project_preferences.items():
            if pref.hidden and name in project_lookup:
                hidden.append(project_lookup[name])

        if hidden:
            adapted_groups["👁️ Hidden (click to show)"] = hidden

        return adapted_groups

    def _apply_custom_order(self, projects: List[Any]) -> List[Any]:
        """Apply user's custom ordering to a list of projects."""
        def sort_key(proj):
            if proj.name in self.profile.project_preferences:
                order = self.profile.project_preferences[proj.name].custom_order
                if order is not None:
                    return (0, order, proj.name)  # Custom order first
            return (1, 0, proj.name)  # Then alphabetical

        return sorted(projects, key=sort_key)

    def get_suggested_group(self, project_name: str) -> Optional[str]:
        """Get suggested group for a project based on learned patterns."""
        # Check if we have a learned pattern
        for pattern in self.profile.learned_patterns:
            if pattern.pattern_type == 'adjacency' and project_name in pattern.evidence:
                if pattern.confidence >= self.CONFIDENCE_SUGGEST:
                    # Suggest grouping with the related project
                    related = [p for p in pattern.evidence if p != project_name]
                    if related:
                        related_proj = related[0]
                        if related_proj in self.profile.project_preferences:
                            return self.profile.project_preferences[related_proj].custom_group

        return None

    # =========================================================================
    # CUSTOM GROUP MANAGEMENT
    # =========================================================================

    def create_custom_group(
        self,
        name: str,
        icon: str = "📁",
        color: str = "white",
        description: str = "",
    ) -> CustomGroup:
        """Create a new custom group."""
        group = CustomGroup(
            name=name,
            icon=icon,
            color=color,
            description=description,
            order=len(self.profile.custom_groups),
            created_at=datetime.utcnow().isoformat(),
        )
        self.profile.custom_groups[name] = group
        return group

    def add_to_group(self, project_name: str, group_name: str):
        """Add a project to a custom group."""
        if group_name not in self.profile.custom_groups:
            self.create_custom_group(group_name)

        if project_name not in self.profile.custom_groups[group_name].projects:
            self.profile.custom_groups[group_name].projects.append(project_name)

        self.observe_grouping(project_name, group_name, is_custom=True)

    def remove_from_group(self, project_name: str, group_name: str):
        """Remove a project from a custom group."""
        if group_name in self.profile.custom_groups:
            if project_name in self.profile.custom_groups[group_name].projects:
                self.profile.custom_groups[group_name].projects.remove(project_name)

    # =========================================================================
    # UI RENDERING
    # =========================================================================

    def render_learning_status(self) -> Panel:
        """Render a panel showing what we've learned about the user."""
        if not RICH_AVAILABLE:
            return None

        content = Text()

        # Profile stats
        content.append("📊 ", style="bold")
        content.append("Learning Progress\n\n", style="bold bright_blue")

        content.append(f"Total interactions: ", style="dim")
        content.append(f"{self.profile.total_interactions}\n", style="bright_green")

        content.append(f"Preferred mode: ", style="dim")
        content.append(f"{self.profile.preferred_mode}\n", style="bright_cyan")

        content.append(f"Custom groups: ", style="dim")
        content.append(f"{len(self.profile.custom_groups)}\n", style="bright_magenta")

        content.append(f"Project preferences: ", style="dim")
        content.append(f"{len(self.profile.project_preferences)}\n", style="bright_yellow")

        content.append(f"Learned patterns: ", style="dim")
        content.append(f"{len(self.profile.learned_patterns)}\n\n", style="bright_green")

        # Custom groups
        if self.profile.custom_groups:
            content.append("📁 Your Custom Groups:\n", style="bold")
            for name, group in self.profile.custom_groups.items():
                content.append(f"  {group.icon} {name}", style="bright_white")
                content.append(f" ({len(group.projects)} projects)\n", style="dim")

        # High-confidence patterns
        high_conf = [p for p in self.profile.learned_patterns if p.confidence >= self.CONFIDENCE_SUGGEST]
        if high_conf:
            content.append("\n🧠 Learned Patterns:\n", style="bold")
            for pattern in high_conf[:5]:  # Show top 5
                if pattern.pattern_type == 'adjacency':
                    content.append(f"  • ", style="dim")
                    content.append(f"{' ↔ '.join(pattern.evidence)}", style="bright_cyan")
                    content.append(f" ({pattern.confidence:.0%} confident)\n", style="dim")

        return Panel(
            content,
            title="[bold]🧬 Adaptive Learning Status[/]",
            border_style="bright_blue",
            box=ROUNDED,
        )

    def render_preferences_editor(self) -> Panel:
        """Render an interactive preferences editor."""
        if not RICH_AVAILABLE:
            return None

        table = Table(show_header=True, header_style="bold", box=ROUNDED)
        table.add_column("Project", style="bright_white")
        table.add_column("Custom Group", style="cyan")
        table.add_column("Order", style="yellow", justify="center")
        table.add_column("Pinned", style="green", justify="center")
        table.add_column("Hidden", style="red", justify="center")

        for name, pref in sorted(self.profile.project_preferences.items()):
            pinned = "📌" if pref.pinned else ""
            hidden = "👁️" if pref.hidden else ""
            order = str(pref.custom_order) if pref.custom_order is not None else "-"
            group = pref.custom_group or "-"

            table.add_row(name, group, order, pinned, hidden)

        return Panel(
            table,
            title="[bold]🎨 Your Project Preferences[/]",
            border_style="bright_magenta",
            box=ROUNDED,
        )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_learner(root_path: Path = None) -> AdaptiveModeLearner:
    """Get or create the adaptive learner for a project."""
    return AdaptiveModeLearner(root_path or Path.cwd())


def quick_learn_grouping(project: str, group: str, root_path: Path = None):
    """Quick helper to record a grouping preference."""
    learner = get_learner(root_path)
    learner.observe_grouping(project, group, is_custom=True)
    learner.save_profile()


def quick_learn_order(project: str, position: int, root_path: Path = None):
    """Quick helper to record an ordering preference."""
    learner = get_learner(root_path)
    learner.observe_reorder(project, position)
    learner.save_profile()


# =============================================================================
# CLI DEMO
# =============================================================================

def demo():
    """Demonstrate adaptive learning."""
    console = Console()
    console.clear()

    console.print(Panel(
        "[bold bright_blue]🧬 ADAPTIVE MODE LEARNER DEMO[/]\n\n"
        "This system learns YOUR organization preferences.\n"
        "The 5 modes are starting points - we adapt to how YOU actually work.\n\n"
        "[dim]Watch as we simulate learning from your actions...[/]",
        border_style="bright_blue",
    ))

    # Create learner
    learner = AdaptiveModeLearner()

    # Simulate some user actions
    console.print("\n[bold]Simulating user actions...[/]\n")

    # User pins their main project
    console.print("  → User pins 'backend' project")
    learner.observe_pin("backend", True)

    # User creates custom group
    console.print("  → User creates 'My Services' group")
    learner.create_custom_group("My Services", icon="⚙️", color="cyan")

    # User adds projects to group
    console.print("  → User adds 'backend' and 'api' to group")
    learner.add_to_group("backend", "My Services")
    learner.add_to_group("api", "My Services")

    # User reorders
    console.print("  → User moves 'frontend' to position 2")
    learner.observe_reorder("frontend", 2)

    # User defines relationship
    console.print("  → User indicates 'tests' relates to 'backend'")
    learner.observe_relationship("tests", "backend")

    # User sets custom type
    console.print("  → User labels 'scripts' as 'DevOps Tools'")
    learner.observe_type_override("scripts", "DevOps Tools")

    # User hides something
    console.print("  → User hides 'node_modules' equivalent")
    learner.observe_hide("vendor", True)

    console.print()

    # Show what we learned
    console.print(learner.render_learning_status())
    console.print(learner.render_preferences_editor())

    console.print("\n[dim]Profile saved to: .context-dna/user_organization.json[/]")
    learner.save_profile()


if __name__ == "__main__":
    demo()
