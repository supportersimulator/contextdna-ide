#!/usr/bin/env python3
"""
Context DNA Adaptive Placement Engine

Suggests optimal file locations based on the detected hierarchy profile.
Follows existing patterns rather than imposing new structure.

Usage:
    # CLI test mode
    python -m context_dna.setup.placement_engine --test /path/to/repo

    # Python
    from context_dna.setup.placement_engine import PlacementEngine
    engine = PlacementEngine()
    suggestion = engine.suggest(profile, 'local_llm_client')
"""

from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from context_dna.setup.models import (
    HierarchyProfile,
    PlacementSuggestion,
)


# =============================================================================
# COMPONENT REQUIREMENTS
# =============================================================================

@dataclass
class ComponentRequirement:
    """Requirements for placing a component."""
    type: str  # 'python_module', 'javascript', 'config', 'docker_compose'
    needs: List[str] = field(default_factory=list)  # Dependencies
    prefers: List[str] = field(default_factory=list)  # Preferred locations
    avoids: List[str] = field(default_factory=list)  # Locations to avoid
    extends_existing: bool = False  # If true, extend existing file instead of creating
    filename: str = ""  # Default filename
    description: str = ""  # What this component does


# Component catalog - what we might need to place
COMPONENT_REQUIREMENTS: Dict[str, ComponentRequirement] = {
    # LLM Integration
    'local_llm_client': ComponentRequirement(
        type='python_module',
        needs=['requests', 'typing'],
        prefers=['memory/', 'lib/', 'utils/', 'core/', 'services/'],
        avoids=['frontend/', 'static/', 'templates/', 'tests/'],
        extends_existing=False,
        filename='local_llm_client.py',
        description='Client for Local LLM API with cloud fallback',
    ),

    # Celery Tasks
    'celery_tasks': ComponentRequirement(
        type='python_module',
        needs=['celery', 'redis'],
        prefers=['memory/', 'tasks/', 'workers/', 'core/', 'backend/'],
        avoids=['frontend/', 'static/', 'templates/'],
        extends_existing=True,  # Extend if exists
        filename='celery_tasks.py',
        description='Celery task definitions for async processing',
    ),

    'celery_config': ComponentRequirement(
        type='python_module',
        needs=['celery'],
        prefers=['memory/', 'config/', 'settings/', 'core/'],
        avoids=['frontend/', 'tests/'],
        extends_existing=True,
        filename='celery_config.py',
        description='Celery configuration and broker settings',
    ),

    # Docker Infrastructure
    'docker_service': ComponentRequirement(
        type='docker_compose',
        needs=['docker-compose'],
        prefers=['infra/', 'docker/', 'context-dna/infra/', 'deploy/'],
        avoids=['frontend/', 'src/'],
        extends_existing=True,
        filename='docker-compose.yaml',
        description='Docker service definitions',
    ),

    # Config Files
    'env_config': ComponentRequirement(
        type='config',
        needs=[],
        prefers=['./', 'config/'],
        avoids=['node_modules/', 'dist/', 'build/'],
        extends_existing=True,
        filename='.env',
        description='Environment variables configuration',
    ),

    # Hierarchy Profile Storage
    'hierarchy_profile': ComponentRequirement(
        type='json',
        needs=[],
        prefers=['.context-dna/', 'config/', '.config/'],
        avoids=['src/', 'frontend/', 'node_modules/'],
        extends_existing=False,
        filename='hierarchy_profile.json',
        description='Stores adaptive hierarchy configuration',
    ),

    # Git Hooks
    'git_hook_post_commit': ComponentRequirement(
        type='shell_script',
        needs=['git'],
        prefers=['.git/hooks/'],
        avoids=[],
        extends_existing=True,
        filename='post-commit',
        description='Git hook for auto-learning from commits',
    ),

    # IDE Hooks
    'claude_md_hook': ComponentRequirement(
        type='markdown',
        needs=[],
        prefers=['./'],
        avoids=['docs/', 'documentation/'],
        extends_existing=True,
        filename='CLAUDE.md',
        description='Claude Code integration hook',
    ),

    'cursorrules_hook': ComponentRequirement(
        type='config',
        needs=[],
        prefers=['./'],
        avoids=['docs/'],
        extends_existing=True,
        filename='.cursorrules',
        description='Cursor IDE integration hook',
    ),
}


# =============================================================================
# PLACEMENT ENGINE
# =============================================================================

class PlacementEngine:
    """
    Suggests optimal file locations based on hierarchy profile.

    Design principles:
    - Extend existing files where possible
    - Follow user's established patterns
    - Explain reasoning for all suggestions
    - Never impose arbitrary structure
    """

    def __init__(self, root_path: Path = None):
        """
        Initialize placement engine.

        Args:
            root_path: Root directory (for file existence checks)
        """
        self.root = Path(root_path) if root_path else Path.cwd()

    def suggest(
        self,
        profile: HierarchyProfile,
        component: str
    ) -> PlacementSuggestion:
        """
        Generate placement suggestion with reasoning.

        Args:
            profile: Analyzed hierarchy profile
            component: Component to place (key from COMPONENT_REQUIREMENTS)

        Returns:
            PlacementSuggestion with path and reasoning
        """
        if component not in COMPONENT_REQUIREMENTS:
            return PlacementSuggestion(
                component=component,
                action='unknown',
                path='',
                reasoning=f"Unknown component: {component}",
            )

        req = COMPONENT_REQUIREMENTS[component]

        # Strategy 1: Check if we should extend an existing file
        if req.extends_existing:
            existing = self._find_existing_file(profile, req)
            if existing:
                return PlacementSuggestion(
                    component=component,
                    action='extend',
                    path=existing,
                    reasoning=f"Extend existing {existing} to maintain consistency",
                )

        # Strategy 2: Match to detected service locations
        matched_location = self._match_to_service(profile, req)
        if matched_location:
            return PlacementSuggestion(
                component=component,
                action='create',
                path=f"{matched_location}/{req.filename}",
                reasoning=f"Matches your '{matched_location}/' service pattern",
            )

        # Strategy 3: Use preferred locations that exist
        for preferred in req.prefers:
            preferred_path = Path(profile.root_path) / preferred.rstrip('/')
            if preferred_path.is_dir():
                return PlacementSuggestion(
                    component=component,
                    action='create',
                    path=f"{preferred.rstrip('/')}/{req.filename}",
                    reasoning=f"Found existing '{preferred}' directory matching preference",
                )

        # Strategy 4: Create in first preferred location
        if req.prefers:
            first_preferred = req.prefers[0].rstrip('/')
            return PlacementSuggestion(
                component=component,
                action='create',
                path=f"{first_preferred}/{req.filename}",
                reasoning=f"Using default location '{first_preferred}/' (no existing pattern found)",
            )

        # Fallback
        return PlacementSuggestion(
            component=component,
            action='create',
            path=req.filename,
            reasoning="No matching pattern found, placing in root",
        )

    def suggest_all(self, profile: HierarchyProfile) -> Dict[str, PlacementSuggestion]:
        """
        Generate suggestions for all known components.

        Returns:
            Dict mapping component name to suggestion
        """
        suggestions = {}
        for component in COMPONENT_REQUIREMENTS:
            suggestions[component] = self.suggest(profile, component)
        return suggestions

    def _find_existing_file(
        self,
        profile: HierarchyProfile,
        req: ComponentRequirement
    ) -> Optional[str]:
        """Find an existing file that should be extended."""
        root = Path(profile.root_path)

        # Check preferred locations first
        for preferred in req.prefers:
            candidate = root / preferred.rstrip('/') / req.filename
            if candidate.exists():
                return str(candidate.relative_to(root))

        # Check all service locations
        for category, location in profile.locations.items():
            if location.path in req.avoids:
                continue

            candidate = root / location.path / req.filename
            if candidate.exists():
                return str(candidate.relative_to(root))

        # Check root
        root_candidate = root / req.filename
        if root_candidate.exists():
            return req.filename

        return None

    def _match_to_service(
        self,
        profile: HierarchyProfile,
        req: ComponentRequirement
    ) -> Optional[str]:
        """Match component to a detected service location."""
        # Map component types to service categories
        type_to_category = {
            'python_module': ['backend', 'memory', 'core'],
            'javascript': ['frontend', 'web'],
            'docker_compose': ['infra', 'docker'],
            'config': ['config', 'settings'],
        }

        preferred_categories = type_to_category.get(req.type, [])

        # First, try preferred categories
        for category in preferred_categories:
            if category in profile.locations:
                location = profile.locations[category]
                if location.path not in req.avoids:
                    return location.path

        # Then, try any service location not in avoids
        for category, location in profile.locations.items():
            if location.path not in req.avoids:
                if any(pref.rstrip('/') == location.path for pref in req.prefers):
                    return location.path

        return None

    def explain(self, suggestion: PlacementSuggestion) -> str:
        """
        Generate human-readable explanation of a suggestion.

        Returns:
            Formatted explanation string
        """
        req = COMPONENT_REQUIREMENTS.get(suggestion.component)
        desc = req.description if req else suggestion.component

        lines = [
            f"📦 Component: {suggestion.component}",
            f"   {desc}",
            "",
            f"🎯 Action: {suggestion.action.upper()}",
            f"   Path: {suggestion.path}",
            "",
            f"💡 Why: {suggestion.reasoning}",
        ]

        if req and req.extends_existing and suggestion.action == 'create':
            lines.extend([
                "",
                "⚠️  Note: This component can extend existing files.",
                "   Run 'context-dna analyze' to detect if you already have one.",
            ])

        return "\n".join(lines)


# =============================================================================
# CLI INTERFACE
# =============================================================================

def test_placement(path: str, verbose: bool = True):
    """
    Test the placement engine on a given path.
    """
    from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer

    path = Path(path).resolve()

    print()
    print("=" * 60)
    print("🎯 PLACEMENT ENGINE TEST")
    print("=" * 60)
    print(f"Path: {path}")
    print()

    if not path.exists():
        print(f"❌ Error: Path does not exist: {path}")
        return False

    # First analyze the hierarchy
    print("Analyzing hierarchy...")
    analyzer = HierarchyAnalyzer(path)
    profile = analyzer.analyze()
    print(f"✓ Detected: {profile.repo_type.value}")

    # Generate suggestions
    print("\nGenerating placement suggestions...")
    engine = PlacementEngine(path)
    suggestions = engine.suggest_all(profile)

    print()
    print("─" * 60)
    print("PLACEMENT SUGGESTIONS")
    print("─" * 60)

    for component, suggestion in suggestions.items():
        action_emoji = "📝" if suggestion.action == 'extend' else "✨"
        print(f"\n{action_emoji} {component}")
        print(f"   Action: {suggestion.action}")
        print(f"   Path:   {suggestion.path}")
        print(f"   Why:    {suggestion.reasoning}")

    print()
    print("=" * 60)
    print("✅ Placement Analysis Complete")
    print("=" * 60)

    return True


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m context_dna.setup.placement_engine <path>")
        print("       python -m context_dna.setup.placement_engine --test <path>")
        sys.exit(1)

    args = sys.argv[1:]

    if args[0] == '--test':
        path = args[1] if len(args) > 1 else '.'
        success = test_placement(path)
        sys.exit(0 if success else 1)
    else:
        path = args[0]
        success = test_placement(path)
        sys.exit(0 if success else 1)
