#!/usr/bin/env python3
"""
Context DNA Adaptive Installer - Enhanced Interactive Experience

Full adaptive installation with:
- Recommended setup + 3+ variations
- Sensitivity slider for detection
- "Scan deeper" for more thorough analysis
- Project renaming for clarity
- Back-and-forth feedback loop

Usage:
    # Full interactive install
    python -m context_dna.setup.adaptive_installer

    # Auto mode (for testing)
    python -m context_dna.setup.adaptive_installer --auto

    # Dry run
    python -m context_dna.setup.adaptive_installer --dry-run
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

from context_dna.setup.models import HierarchyProfile
from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer, DetectedProject


# =============================================================================
# SENSITIVITY LEVELS
# =============================================================================

class SensitivityLevel(Enum):
    """Detection sensitivity levels."""
    LOW = 1         # Only obvious projects
    NORMAL = 2      # Standard detection (default)
    HIGH = 3        # More aggressive detection
    MAXIMUM = 4     # Find everything possible


@dataclass
class DetectionSettings:
    """Current detection settings."""
    sensitivity: SensitivityLevel = SensitivityLevel.NORMAL
    max_depth: int = 2
    include_hidden: bool = False
    include_archives: bool = False


# =============================================================================
# SETUP VARIATIONS
# =============================================================================

@dataclass
class SetupVariation:
    """A configuration variation for user selection."""
    id: str
    name: str
    description: str
    emoji: str
    primary_focus: str
    included_projects: List[str]
    excluded_projects: List[str]
    reasoning: str
    is_recommended: bool = False
    confidence: float = 0.0


@dataclass
class EnhancedProject:
    """Project with user modifications."""
    original: DetectedProject
    display_name: str  # User can rename
    include_in_tracking: bool = True
    user_notes: str = ""
    category: str = ""  # User-assigned category


# =============================================================================
# ADAPTIVE INSTALLER
# =============================================================================

class AdaptiveInstaller:
    """
    Enhanced interactive installer with feedback loops.

    Key features:
    - Sensitivity adjustment
    - Scan deeper option
    - Project renaming
    - Multiple setup variations
    - Back and forth with user
    """

    def __init__(
        self,
        root_path: str | Path = None,
        auto_mode: bool = False,
        dry_run: bool = False,
    ):
        self.root = Path(root_path or '.').resolve()
        self.auto_mode = auto_mode
        self.dry_run = dry_run

        # State
        self.settings = DetectionSettings()
        self.detected_projects: List[DetectedProject] = []
        self.enhanced_projects: Dict[str, EnhancedProject] = {}
        self.variations: List[SetupVariation] = []
        self.selected_variation: Optional[SetupVariation] = None
        self.profile: Optional[HierarchyProfile] = None

    def run(self) -> bool:
        """Run the full adaptive installation."""
        self._print_header()

        while True:  # Main feedback loop
            # Phase 1: Scan with current settings
            self._print_phase("Phase 1: Scanning Your Codebase")
            self._run_scan()

            # Phase 2: Show what we found
            self._print_phase("Phase 2: What I Found")
            self._show_detected_projects()

            # Phase 3: Offer adjustment options
            action = self._offer_adjustments()

            if action == 'continue':
                break
            elif action == 'scan_deeper':
                self.settings.sensitivity = SensitivityLevel(min(self.settings.sensitivity.value + 1, 4))
                self.settings.max_depth += 1
                self._print(f"\n   ⬆️ Increasing sensitivity to {self.settings.sensitivity.name}")
                continue
            elif action == 'scan_lighter':
                self.settings.sensitivity = SensitivityLevel(max(self.settings.sensitivity.value - 1, 1))
                self._print(f"\n   ⬇️ Decreasing sensitivity to {self.settings.sensitivity.name}")
                continue
            elif action == 'rename':
                self._rename_projects()
                continue

        # Phase 4: Generate setup variations
        self._print_phase("Phase 3: Setup Options")
        self._generate_variations()
        self._show_variations()

        # Phase 5: Select configuration
        self._print_phase("Phase 4: Choose Your Setup")
        self._select_variation()

        # Phase 6: Confirm and install
        self._print_phase("Phase 5: Confirm & Install")
        return self._confirm_and_install()

    # -------------------------------------------------------------------------
    # Phase 1: Scanning
    # -------------------------------------------------------------------------

    def _run_scan(self):
        """Run scan with current settings."""
        self._print(f"   🔍 Scanning with sensitivity: {self.settings.sensitivity.name}")
        self._print(f"      Max depth: {self.settings.max_depth}")

        analyzer = HierarchyAnalyzer(self.root, max_depth=self.settings.max_depth)

        # Adjust detection based on sensitivity
        depth = {
            SensitivityLevel.LOW: 1,
            SensitivityLevel.NORMAL: 2,
            SensitivityLevel.HIGH: 3,
            SensitivityLevel.MAXIMUM: 4,
        }[self.settings.sensitivity]

        self.detected_projects = analyzer.detect_all_projects(max_depth=depth)
        self.profile = analyzer.analyze()

        # Create enhanced projects
        for p in self.detected_projects:
            if p.path not in self.enhanced_projects:
                self.enhanced_projects[p.path] = EnhancedProject(
                    original=p,
                    display_name=p.name,
                    include_in_tracking=True,
                )

        self._print(f"\n   ✓ Found {len(self.detected_projects)} potential projects")

    # -------------------------------------------------------------------------
    # Phase 2: Show Results
    # -------------------------------------------------------------------------

    def _show_detected_projects(self):
        """Display detected projects with grouping."""
        # Group by confidence level
        high_conf = [p for p in self.detected_projects if p.confidence >= 0.9]
        med_conf = [p for p in self.detected_projects if 0.6 <= p.confidence < 0.9]
        low_conf = [p for p in self.detected_projects if p.confidence < 0.6]

        if high_conf:
            self._print("\n   🟢 DEFINITELY PROJECTS (high confidence):")
            for p in high_conf:
                ep = self.enhanced_projects.get(p.path)
                name = ep.display_name if ep else p.name
                self._print(f"      [{self._short_id(p.path)}] {name}")
                self._print(f"          └─ {p.description} ({p.confidence:.0%})")

        if med_conf:
            self._print("\n   🟡 LIKELY PROJECTS (medium confidence):")
            for p in med_conf:
                ep = self.enhanced_projects.get(p.path)
                name = ep.display_name if ep else p.name
                self._print(f"      [{self._short_id(p.path)}] {name}")
                self._print(f"          └─ {p.description} ({p.confidence:.0%})")

        if low_conf:
            self._print("\n   🟠 MAYBE PROJECTS (low confidence):")
            for p in low_conf:
                ep = self.enhanced_projects.get(p.path)
                name = ep.display_name if ep else p.name
                self._print(f"      [{self._short_id(p.path)}] {name}")
                self._print(f"          └─ {p.description} ({p.confidence:.0%})")

        # Show sensitivity gauge
        self._print("\n   ──────────────────────────────────────────────")
        self._print("   SENSITIVITY GAUGE:")
        gauge = "█" * self.settings.sensitivity.value + "░" * (4 - self.settings.sensitivity.value)
        self._print(f"   [{gauge}] {self.settings.sensitivity.name}")
        self._print("   ──────────────────────────────────────────────")

    def _short_id(self, path: str) -> str:
        """Generate short ID for a project."""
        # Use first 3 chars of path hash
        import hashlib
        return hashlib.sha256(path.encode()).hexdigest()[:3].upper()

    # -------------------------------------------------------------------------
    # Phase 3: Adjustment Options
    # -------------------------------------------------------------------------

    def _offer_adjustments(self) -> str:
        """Offer user options to adjust detection."""
        self._print("\n   ═══════════════════════════════════════════════")
        self._print("   📊 ADJUSTMENT OPTIONS:")
        self._print("   ═══════════════════════════════════════════════")
        self._print("")
        self._print("   [1] ✅ Looks good - CONTINUE to setup options")
        self._print("   [2] 🔍 Scan DEEPER (find more projects)")
        self._print("   [3] 🔅 Scan LIGHTER (fewer false positives)")
        self._print("   [4] ✏️  RENAME projects for clarity")
        self._print("   [5] ➕ ADD a project I don't see")
        self._print("   [6] ➖ EXCLUDE a detected project")
        self._print("")

        if self.auto_mode:
            return 'continue'

        choice = input("   Your choice [1]: ").strip() or "1"

        actions = {
            "1": "continue",
            "2": "scan_deeper",
            "3": "scan_lighter",
            "4": "rename",
            "5": "add",
            "6": "exclude",
        }

        return actions.get(choice, "continue")

    def _rename_projects(self):
        """Allow user to rename projects."""
        self._print("\n   ✏️ RENAME PROJECTS")
        self._print("   Enter the ID and new name (or press Enter to skip)")
        self._print("")

        for p in self.detected_projects:
            ep = self.enhanced_projects.get(p.path)
            short_id = self._short_id(p.path)
            current_name = ep.display_name if ep else p.name

            self._print(f"   [{short_id}] Current: {current_name}")

            if not self.auto_mode:
                new_name = input(f"         New name (Enter to keep): ").strip()
                if new_name:
                    self.enhanced_projects[p.path].display_name = new_name
                    self._print(f"         ✓ Renamed to: {new_name}")

    # -------------------------------------------------------------------------
    # Phase 4: Generate Variations
    # -------------------------------------------------------------------------

    def _generate_variations(self):
        """Generate setup variations for user selection."""
        self.variations = []

        projects = self.detected_projects
        submodules = [p for p in projects if p.is_submodule]
        apps = [p for p in projects if p.framework in ('django', 'nextjs', 'flask', 'fastapi', 'rails')]
        infra = [p for p in projects if 'infra' in p.description.lower() or 'docker' in p.description.lower()]

        # Variation 1: RECOMMENDED - Smart default
        main_app = apps[0] if apps else projects[0] if projects else None
        if main_app:
            recommended_included = [p.path for p in projects if not p.is_submodule]
            self.variations.append(SetupVariation(
                id="recommended",
                name="🌟 RECOMMENDED: Smart Workspace",
                description="Focus on your main codebase, submodules tracked separately",
                emoji="🌟",
                primary_focus=main_app.path,
                included_projects=recommended_included,
                excluded_projects=[p.path for p in submodules],
                reasoning="Submodules have their own repos - best to track them there",
                is_recommended=True,
                confidence=0.9,
            ))

        # Variation 2: Full Everything
        self.variations.append(SetupVariation(
            id="full",
            name="📦 Full Workspace",
            description="Track EVERYTHING as one unified project",
            emoji="📦",
            primary_focus=self.root.name,
            included_projects=[p.path for p in projects],
            excluded_projects=[],
            reasoning="Best for small teams where all code is interconnected",
            confidence=0.7,
        ))

        # Variation 3: Apps Only (no infra)
        if apps:
            apps_only = [p.path for p in apps]
            self.variations.append(SetupVariation(
                id="apps_only",
                name="💻 Apps Only",
                description="Focus on applications, skip infrastructure",
                emoji="💻",
                primary_focus=apps[0].path,
                included_projects=apps_only,
                excluded_projects=[p.path for p in infra + submodules],
                reasoning="Infrastructure has its own patterns - focus on app code",
                confidence=0.75,
            ))

        # Variation 4: Backend Focus
        backend = [p for p in projects if p.framework == 'django' or 'backend' in p.path.lower()]
        if backend:
            self.variations.append(SetupVariation(
                id="backend",
                name="🔧 Backend Focus",
                description=f"Focus on {backend[0].name} backend development",
                emoji="🔧",
                primary_focus=backend[0].path,
                included_projects=[p.path for p in backend],
                excluded_projects=[p.path for p in projects if p not in backend],
                reasoning=f"Best when {backend[0].name} is your primary work",
                confidence=0.8,
            ))

        # Variation 5: Minimal (just the essentials)
        high_conf = [p for p in projects if p.confidence >= 0.9]
        if high_conf:
            self.variations.append(SetupVariation(
                id="minimal",
                name="🎯 Minimal",
                description="Only track high-confidence, definite projects",
                emoji="🎯",
                primary_focus=high_conf[0].path if high_conf else '.',
                included_projects=[p.path for p in high_conf],
                excluded_projects=[p.path for p in projects if p.confidence < 0.9],
                reasoning="Start small, expand later as needed",
                confidence=0.95,
            ))

    def _show_variations(self):
        """Display setup variations."""
        self._print("\n   I've generated these configuration options:\n")

        for i, var in enumerate(self.variations, 1):
            rec = " ⬅️ RECOMMENDED" if var.is_recommended else ""
            self._print(f"   ──────────────────────────────────────────────")
            self._print(f"   [{i}] {var.name}{rec}")
            self._print(f"       {var.description}")
            self._print(f"       📁 Includes: {len(var.included_projects)} projects")
            self._print(f"       ⏭️  Excludes: {len(var.excluded_projects)} projects")
            self._print(f"       💡 {var.reasoning}")

        self._print(f"   ──────────────────────────────────────────────")
        self._print(f"   [C] CUSTOM - I'll pick exactly what to include")
        self._print(f"   ──────────────────────────────────────────────")

    # -------------------------------------------------------------------------
    # Phase 5: Select Variation
    # -------------------------------------------------------------------------

    def _select_variation(self):
        """Let user select final configuration."""
        self._print("")

        if self.auto_mode:
            # Select recommended or first
            self.selected_variation = next((v for v in self.variations if v.is_recommended), self.variations[0])
            self._print(f"   Auto-selected: {self.selected_variation.name}")
            return

        choice = input("   Your choice [1]: ").strip() or "1"

        if choice.upper() == 'C':
            self._custom_selection()
        elif choice.isdigit() and 1 <= int(choice) <= len(self.variations):
            self.selected_variation = self.variations[int(choice) - 1]
            self._print(f"\n   ✓ Selected: {self.selected_variation.name}")

    def _custom_selection(self):
        """Allow custom project selection."""
        self._print("\n   📋 CUSTOM SELECTION")
        self._print("   Mark projects to include (Y/n for each):")
        self._print("")

        included = []
        for p in self.detected_projects:
            ep = self.enhanced_projects.get(p.path)
            name = ep.display_name if ep else p.name

            if self.auto_mode:
                include = True
            else:
                response = input(f"   Include {name}? [Y/n]: ").strip().lower()
                include = response != 'n'

            if include:
                included.append(p.path)

        self.selected_variation = SetupVariation(
            id="custom",
            name="Custom Selection",
            description="User-selected projects",
            emoji="🎨",
            primary_focus=included[0] if included else '.',
            included_projects=included,
            excluded_projects=[p.path for p in self.detected_projects if p.path not in included],
            reasoning="Custom configuration based on your selections",
        )

    # -------------------------------------------------------------------------
    # Phase 6: Confirm and Install
    # -------------------------------------------------------------------------

    def _confirm_and_install(self) -> bool:
        """Confirm selections and run installation."""
        if not self.selected_variation:
            self._print("   ❌ No configuration selected")
            return False

        self._print(f"\n   📋 FINAL CONFIGURATION:")
        self._print(f"   ════════════════════════════════════════════")
        self._print(f"   Setup: {self.selected_variation.name}")
        self._print(f"   Primary: {self.selected_variation.primary_focus}")
        self._print(f"   ")
        self._print(f"   ✅ WILL TRACK ({len(self.selected_variation.included_projects)}):")
        for path in self.selected_variation.included_projects[:5]:
            ep = self.enhanced_projects.get(path)
            name = ep.display_name if ep else path
            self._print(f"      • {name}")
        if len(self.selected_variation.included_projects) > 5:
            self._print(f"      ... and {len(self.selected_variation.included_projects) - 5} more")

        if self.selected_variation.excluded_projects:
            self._print(f"   ")
            self._print(f"   ⏭️  EXCLUDED ({len(self.selected_variation.excluded_projects)}):")
            for path in self.selected_variation.excluded_projects[:3]:
                ep = self.enhanced_projects.get(path)
                name = ep.display_name if ep else path
                self._print(f"      • {name}")
            if len(self.selected_variation.excluded_projects) > 3:
                self._print(f"      ... and {len(self.selected_variation.excluded_projects) - 3} more")

        self._print(f"   ════════════════════════════════════════════")

        if self.auto_mode:
            proceed = True
        else:
            proceed = input("\n   Proceed with installation? [Y/n]: ").strip().lower() != 'n'

        if not proceed:
            self._print("   Installation cancelled")
            return False

        if self.dry_run:
            self._print("\n   [DRY RUN] Would install with this configuration")
            return True

        # Run actual installation
        return self._execute_install()

    def _execute_install(self) -> bool:
        """Execute the actual installation."""
        from context_dna.setup.safe_installer import SafeInstaller

        self._print("\n   🚀 Installing Context DNA...")

        installer = SafeInstaller(
            root_path=self.root,
            dry_run=self.dry_run,
            quiet=True,
            auto_yes=True,
        )

        # Run installation
        result = installer.install()

        if result.success:
            self._print("\n   ════════════════════════════════════════════")
            self._print("   🎉 INSTALLATION COMPLETE!")
            self._print("   ════════════════════════════════════════════")
            self._print(f"   ✓ Completed: {result.steps_completed} steps")
            self._print(f"   ⏭️ Skipped: {result.steps_skipped} (already configured)")
            self._print("")
            self._print("   Next steps:")
            self._print("      context-dna status    # Check status")
            self._print("      context-dna analyze   # Re-analyze anytime")
        else:
            self._print("\n   ⚠️ Installation incomplete")
            self._print(f"   Failed: {result.steps_failed} steps")

        return result.success

    # -------------------------------------------------------------------------
    # Output Utilities
    # -------------------------------------------------------------------------

    def _print(self, msg: str):
        """Print message."""
        print(msg)

    def _print_header(self):
        """Print installer header."""
        print()
        print("╔" + "═" * 62 + "╗")
        print("║" + "                                                              " + "║")
        print("║" + "      🧬 CONTEXT DNA - ADAPTIVE INSTALLATION WIZARD           " + "║")
        print("║" + "                                                              " + "║")
        mode = "[DRY RUN] " if self.dry_run else ""
        print("║" + f"      {mode}Making AI understand YOUR codebase                   " + "║")
        print("║" + "                                                              " + "║")
        print("╚" + "═" * 62 + "╝")
        print()

    def _print_phase(self, phase: str):
        """Print phase header."""
        print()
        print(f"   ━━━ {phase} ━━━")


# =============================================================================
# CLI
# =============================================================================

def run_adaptive_install(
    path: str = '.',
    auto: bool = False,
    dry_run: bool = False,
) -> bool:
    """Run the adaptive installer."""
    installer = AdaptiveInstaller(
        root_path=path,
        auto_mode=auto,
        dry_run=dry_run,
    )
    return installer.run()


if __name__ == '__main__':
    args = sys.argv[1:]

    auto = '--auto' in args
    dry_run = '--dry-run' in args
    path = [a for a in args if not a.startswith('-')]
    path = path[0] if path else '.'

    success = run_adaptive_install(path=path, auto=auto, dry_run=dry_run)
    sys.exit(0 if success else 1)
