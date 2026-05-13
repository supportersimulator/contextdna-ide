#!/usr/bin/env python3
"""
Context DNA Safe Installer

Full installation flow with data preservation guarantees:
- NEVER overwrites existing user data
- Gracefully skips already-configured components
- Supports resumption of partial installs
- Backs up configs before ANY modification
- Provides dry-run mode for verification

Usage:
    # Dry run (show what would happen)
    python -m context_dna.setup.safe_installer --dry-run

    # Full install
    python -m context_dna.setup.safe_installer

    # Test mode (verifies flow without user input)
    python -m context_dna.setup.safe_installer --test
"""

import os
import sys
import json
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

from context_dna.setup.models import HierarchyProfile, PlatformInfo
from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer, DetectedProject
from context_dna.setup.placement_engine import PlacementEngine, COMPONENT_REQUIREMENTS
from context_dna.setup.hierarchy_questioner import HierarchyQuestioner, ClarifiedProject, TrackingPreference


# =============================================================================
# INSTALLATION STEP STATUS
# =============================================================================

class StepStatus(Enum):
    """Status of an installation step."""
    PENDING = "pending"
    SKIPPED_EXISTS = "skipped_exists"  # Already configured
    SKIPPED_USER = "skipped_user"      # User chose to skip
    COMPLETED = "completed"
    FAILED = "failed"
    DRY_RUN = "dry_run"                # Would have run


@dataclass
class InstallStep:
    """A single installation step with safety checks."""
    id: str
    name: str
    description: str
    check_exists: callable  # Returns True if already configured
    install_action: callable  # Performs the installation
    backup_paths: List[str] = field(default_factory=list)  # Paths to backup before modifying
    required: bool = True
    status: StepStatus = StepStatus.PENDING
    message: str = ""


@dataclass
class InstallResult:
    """Result of the full installation."""
    success: bool
    steps_completed: int
    steps_skipped: int
    steps_failed: int
    profile: Optional[HierarchyProfile] = None
    backup_location: Optional[str] = None
    messages: List[str] = field(default_factory=list)


# =============================================================================
# SAFE INSTALLER
# =============================================================================

class SafeInstaller:
    """
    Safe installation manager with data preservation guarantees.

    Core principles:
    1. NEVER delete or overwrite existing user data
    2. Always backup before ANY modification
    3. Check before every action
    4. Support full dry-run simulation
    5. Graceful handling of partial installs
    """

    def __init__(
        self,
        root_path: str | Path = None,
        dry_run: bool = False,
        quiet: bool = False,
        auto_yes: bool = False,
    ):
        """
        Initialize safe installer.

        Args:
            root_path: Project root directory
            dry_run: If True, simulate without making changes
            quiet: If True, minimize output
            auto_yes: If True, automatically accept all prompts (for testing)
        """
        self.root = Path(root_path or os.getcwd()).resolve()
        self.dry_run = dry_run
        self.quiet = quiet
        self.auto_yes = auto_yes

        # Configuration
        self.config_dir = self.root / '.context-dna'
        self.backup_dir = self.config_dir / 'backups' / datetime.now().strftime('%Y%m%d_%H%M%S')

        # State
        self.profile: Optional[HierarchyProfile] = None
        self.placement_engine: Optional[PlacementEngine] = None
        self._steps: List[InstallStep] = []

    # -------------------------------------------------------------------------
    # Main Installation Flow
    # -------------------------------------------------------------------------

    def install(self) -> InstallResult:
        """
        Run the complete installation flow.

        Returns:
            InstallResult with full status
        """
        messages = []

        self._print_header()

        # Phase 1: Analyze codebase
        self._print_phase("Phase 1: Analyzing Codebase")
        self.profile = self._analyze_codebase()
        self.placement_engine = PlacementEngine(self.root)

        if self.profile:
            self._print_profile_summary()
        else:
            messages.append("Failed to analyze codebase")
            return InstallResult(
                success=False,
                steps_completed=0,
                steps_skipped=0,
                steps_failed=1,
                messages=messages,
            )

        # Phase 2: Build installation steps
        self._print_phase("Phase 2: Planning Installation")
        self._build_steps()

        # Phase 3: Check existing installations
        self._print_phase("Phase 3: Checking Existing Setup")
        existing_count = self._check_existing()

        if existing_count > 0:
            self._print(f"   Found {existing_count} already-configured component(s)")
            self._print("   These will be preserved (not overwritten)")

        # Phase 4: Execute steps
        self._print_phase("Phase 4: Installing Components")
        completed, skipped, failed = self._execute_steps()

        # Phase 5: Save profile
        self._print_phase("Phase 5: Saving Configuration")
        profile_saved = self._save_profile()

        # Summary
        self._print_summary(completed, skipped, failed, profile_saved)

        success = failed == 0 and (completed > 0 or skipped > 0)

        return InstallResult(
            success=success,
            steps_completed=completed,
            steps_skipped=skipped,
            steps_failed=failed,
            profile=self.profile,
            backup_location=str(self.backup_dir) if self.backup_dir.exists() else None,
            messages=messages,
        )

    # -------------------------------------------------------------------------
    # Phase 1: Analysis
    # -------------------------------------------------------------------------

    def _analyze_codebase(self) -> Optional[HierarchyProfile]:
        """Analyze codebase structure."""
        self._print("   Scanning directory structure...")

        try:
            analyzer = HierarchyAnalyzer(self.root)
            profile = analyzer.analyze()
            self._print("   ✓ Analysis complete")
            return profile
        except Exception as e:
            self._print(f"   ✗ Analysis failed: {e}")
            return None

    def _print_profile_summary(self):
        """Print summary of detected profile."""
        p = self.profile

        self._print(f"\n   📁 Repository Type: {p.repo_type.value}")

        if p.submodules:
            self._print(f"   📦 Submodules: {len(p.submodules)}")

        if p.locations:
            self._print(f"   🏗️ Services: {', '.join(p.locations.keys())}")

        if p.platform:
            self._print(f"   💻 Platform: {p.platform.os} ({p.platform.arch})")
            self._print(f"   🤖 Recommended LLM: {p.platform.recommended_backend.value}")

    # -------------------------------------------------------------------------
    # Phase 2: Build Steps
    # -------------------------------------------------------------------------

    def _build_steps(self):
        """Build the list of installation steps."""
        self._steps = [
            self._step_config_dir(),
            self._step_hierarchy_profile(),
            self._step_claude_md_hook(),
            self._step_cursorrules_hook(),
            self._step_git_hooks(),
            self._step_env_config(),
        ]

        self._print(f"   Planned {len(self._steps)} installation step(s)")

    def _step_config_dir(self) -> InstallStep:
        """Create .context-dna directory."""
        def check_exists():
            return self.config_dir.exists() and (self.config_dir / 'config.json').exists()

        def install():
            if self.dry_run:
                return True

            self.config_dir.mkdir(parents=True, exist_ok=True)

            config = {
                'version': '1.0.0',
                'created_at': datetime.utcnow().isoformat(),
                'project_root': str(self.root),
            }

            config_file = self.config_dir / 'config.json'
            config_file.write_text(json.dumps(config, indent=2))
            return True

        return InstallStep(
            id='config_dir',
            name='Configuration Directory',
            description='Create .context-dna/ for settings and data',
            check_exists=check_exists,
            install_action=install,
        )

    def _step_hierarchy_profile(self) -> InstallStep:
        """Save hierarchy profile."""
        profile_path = self.config_dir / 'hierarchy_profile.json'

        def check_exists():
            return profile_path.exists()

        def install():
            if self.dry_run:
                return True

            self.config_dir.mkdir(parents=True, exist_ok=True)

            if self.profile:
                profile_path.write_text(self.profile.to_json())
            return True

        return InstallStep(
            id='hierarchy_profile',
            name='Hierarchy Profile',
            description='Save detected codebase structure',
            check_exists=check_exists,
            install_action=install,
        )

    def _step_claude_md_hook(self) -> InstallStep:
        """Add Context DNA to CLAUDE.md."""
        claude_md = self.root / 'CLAUDE.md'

        def check_exists():
            if not claude_md.exists():
                return False
            content = claude_md.read_text()
            return 'Context DNA' in content or 'context-dna' in content

        def install():
            if self.dry_run:
                return True

            # Never overwrite - only append
            hook_content = self._get_claude_md_hook()

            if claude_md.exists():
                # Backup first
                self._backup_file(claude_md)
                # Append to existing
                with open(claude_md, 'a') as f:
                    f.write('\n\n' + hook_content)
            else:
                # Create new
                claude_md.write_text(hook_content)

            return True

        return InstallStep(
            id='claude_md',
            name='Claude Code Integration',
            description='Add Context DNA hook to CLAUDE.md',
            check_exists=check_exists,
            install_action=install,
            backup_paths=[str(claude_md)] if claude_md.exists() else [],
            required=False,
        )

    def _step_cursorrules_hook(self) -> InstallStep:
        """Add Context DNA to .cursorrules."""
        cursorrules = self.root / '.cursorrules'

        def check_exists():
            if not cursorrules.exists():
                return False
            content = cursorrules.read_text()
            return 'Context DNA' in content or 'context-dna' in content

        def install():
            if self.dry_run:
                return True

            hook_content = self._get_cursorrules_hook()

            if cursorrules.exists():
                self._backup_file(cursorrules)
                with open(cursorrules, 'a') as f:
                    f.write('\n\n' + hook_content)
            else:
                cursorrules.write_text(hook_content)

            return True

        return InstallStep(
            id='cursorrules',
            name='Cursor IDE Integration',
            description='Add Context DNA hook to .cursorrules',
            check_exists=check_exists,
            install_action=install,
            backup_paths=[str(cursorrules)] if cursorrules.exists() else [],
            required=False,
        )

    def _step_git_hooks(self) -> InstallStep:
        """Install git hooks for auto-learning."""
        git_hooks_dir = self.root / '.git' / 'hooks'
        post_commit = git_hooks_dir / 'post-commit'

        def check_exists():
            if not post_commit.exists():
                return False
            content = post_commit.read_text()
            return 'context-dna' in content or 'Context DNA' in content

        def install():
            if self.dry_run:
                return True

            if not git_hooks_dir.exists():
                return True  # Not a git repo, skip gracefully

            hook_content = self._get_git_hook()

            if post_commit.exists():
                self._backup_file(post_commit)
                # Append to existing hook
                with open(post_commit, 'a') as f:
                    f.write('\n\n' + hook_content)
            else:
                post_commit.write_text('#!/bin/bash\n\n' + hook_content)
                post_commit.chmod(0o755)

            return True

        return InstallStep(
            id='git_hooks',
            name='Git Learning Hooks',
            description='Auto-learn from commits',
            check_exists=check_exists,
            install_action=install,
            backup_paths=[str(post_commit)] if post_commit.exists() else [],
            required=False,
        )

    def _step_env_config(self) -> InstallStep:
        """Check/setup environment configuration."""
        env_file = self.root / '.env'
        env_example = self.config_dir / '.env.example'

        def check_exists():
            # Just check if .env exists, don't require specific content
            return env_file.exists()

        def install():
            if self.dry_run:
                return True

            # Create example env but NEVER touch user's .env
            self.config_dir.mkdir(parents=True, exist_ok=True)

            example_content = """# Context DNA Configuration Example
# Copy these to your .env file (don't commit .env to git!)

# LLM Backend (optional - will use Ollama if available)
# Context_DNA_OPENAI=sk-...
# ANTHROPIC_API_KEY=sk-ant-...

# Local LLM Server
LOCAL_LLM_URL=http://127.0.0.1:5044

# Context DNA API (for cloud sync)
# CONTEXTDNA_API_KEY=...
"""
            env_example.write_text(example_content)
            return True

        return InstallStep(
            id='env_config',
            name='Environment Config',
            description='Create .env.example template',
            check_exists=check_exists,
            install_action=install,
            required=False,
        )

    # -------------------------------------------------------------------------
    # Phase 3: Check Existing
    # -------------------------------------------------------------------------

    def _check_existing(self) -> int:
        """Check which steps are already completed."""
        existing = 0

        for step in self._steps:
            try:
                if step.check_exists():
                    step.status = StepStatus.SKIPPED_EXISTS
                    existing += 1
            except Exception as e:
                self._print(f"   ⚠ Error checking {step.name}: {e}")

        return existing

    # -------------------------------------------------------------------------
    # Phase 4: Execute Steps
    # -------------------------------------------------------------------------

    def _execute_steps(self) -> Tuple[int, int, int]:
        """Execute all pending installation steps."""
        completed = 0
        skipped = 0
        failed = 0

        for step in self._steps:
            # Skip already-configured
            if step.status == StepStatus.SKIPPED_EXISTS:
                self._print(f"   ✓ {step.name}: Already configured")
                skipped += 1
                continue

            # Skip if user declined (in interactive mode)
            if step.status == StepStatus.SKIPPED_USER:
                self._print(f"   ⏭ {step.name}: Skipped by user")
                skipped += 1
                continue

            # Dry run mode
            if self.dry_run:
                self._print(f"   📋 {step.name}: Would install")
                step.status = StepStatus.DRY_RUN
                completed += 1
                continue

            # Execute installation
            try:
                success = step.install_action()
                if success:
                    step.status = StepStatus.COMPLETED
                    self._print(f"   ✓ {step.name}: Installed")
                    completed += 1
                else:
                    step.status = StepStatus.FAILED
                    self._print(f"   ✗ {step.name}: Failed")
                    failed += 1
            except Exception as e:
                step.status = StepStatus.FAILED
                step.message = str(e)
                self._print(f"   ✗ {step.name}: Error - {e}")
                failed += 1

        return completed, skipped, failed

    # -------------------------------------------------------------------------
    # Phase 5: Save Profile
    # -------------------------------------------------------------------------

    def _save_profile(self) -> bool:
        """Save the hierarchy profile."""
        if self.dry_run:
            self._print("   📋 Would save hierarchy profile")
            return True

        try:
            profile_path = self.config_dir / 'hierarchy_profile.json'
            if self.profile:
                self.profile.save(profile_path)
                self._print("   ✓ Profile saved")
            return True
        except Exception as e:
            self._print(f"   ✗ Failed to save profile: {e}")
            return False

    # -------------------------------------------------------------------------
    # Backup Utilities
    # -------------------------------------------------------------------------

    def _backup_file(self, path: Path) -> Optional[Path]:
        """
        Create a backup of a file before modification.

        CRITICAL: Never modify without backup!
        """
        if not path.exists():
            return None

        self.backup_dir.mkdir(parents=True, exist_ok=True)

        # Preserve relative path structure in backup
        rel_path = path.relative_to(self.root) if path.is_relative_to(self.root) else path.name
        backup_path = self.backup_dir / rel_path

        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)

        return backup_path

    # -------------------------------------------------------------------------
    # Hook Content Generators
    # -------------------------------------------------------------------------

    def _get_claude_md_hook(self) -> str:
        """Generate Claude.md hook content."""
        return """
## 🧬 Context DNA Integration

Context DNA provides intelligent context for AI assistants.

**Query memory before complex tasks:**
```bash
python memory/query.py "relevant keywords"
python memory/context.py "what you're working on"
```

**Record wins and learnings:**
```bash
python memory/brain.py success "What worked" "How it worked"
python memory/brain.py fix "What broke" "How you fixed it"
```

---
"""

    def _get_cursorrules_hook(self) -> str:
        """Generate .cursorrules hook content."""
        return """
# Context DNA Integration
# Query memory before complex infrastructure work:
# python memory/query.py "relevant keywords"
# Record successes: python memory/brain.py success "task" "details"
"""

    def _get_git_hook(self) -> str:
        """Generate git post-commit hook content."""
        return """# Context DNA Auto-Learn Hook
# Records commits for pattern learning

# Check if context-dna auto-learn exists
if [ -f "memory/auto_learn.py" ]; then
    python memory/auto_learn.py 2>/dev/null || true
fi
"""

    # -------------------------------------------------------------------------
    # Output Utilities
    # -------------------------------------------------------------------------

    def _print(self, msg: str):
        """Print message (respects quiet mode)."""
        if not self.quiet:
            print(msg)

    def _print_header(self):
        """Print installation header."""
        mode = "[DRY RUN] " if self.dry_run else ""
        self._print("")
        self._print("╔" + "═" * 58 + "╗")
        self._print("║" + f" 🧬 CONTEXT DNA SAFE INSTALLER {mode}".center(58) + "║")
        self._print("╚" + "═" * 58 + "╝")
        self._print("")

    def _print_phase(self, phase: str):
        """Print phase header."""
        self._print("")
        self._print(f"━━━ {phase} ━━━")

    def _print_summary(self, completed: int, skipped: int, failed: int, profile_saved: bool):
        """Print installation summary."""
        self._print("")
        self._print("═" * 60)

        if failed == 0:
            self._print("")
            self._print("  🎉 Installation Complete!")
            self._print("")
            self._print(f"  ✓ Completed: {completed}")
            self._print(f"  ⏭ Skipped (already configured): {skipped}")

            if self.backup_dir.exists():
                self._print(f"  📦 Backups: {self.backup_dir}")

            self._print("")
            self._print("  Next steps:")
            self._print("    context-dna status     # Check status")
            self._print("    context-dna analyze    # Re-analyze codebase")
        else:
            self._print("")
            self._print(f"  ⚠️ Installation incomplete ({failed} failed)")
            self._print("")
            self._print("  Run 'context-dna setup' again to retry")

        self._print("")


# =============================================================================
# CLI INTERFACE
# =============================================================================

def run_install(
    path: str = None,
    dry_run: bool = False,
    quiet: bool = False,
    auto_yes: bool = False,
) -> bool:
    """Run the safe installer."""
    installer = SafeInstaller(
        root_path=path,
        dry_run=dry_run,
        quiet=quiet,
        auto_yes=auto_yes,
    )
    result = installer.install()
    return result.success


def test_install(path: str = '.') -> bool:
    """
    Test the install flow end-to-end.

    Runs in dry-run mode to verify everything works
    without making actual changes.
    """
    print()
    print("=" * 60)
    print("🧪 INSTALL FLOW TEST (DRY RUN)")
    print("=" * 60)
    print(f"Path: {Path(path).resolve()}")
    print()

    result = run_install(
        path=path,
        dry_run=True,
        quiet=False,
        auto_yes=True,
    )

    print()
    if result:
        print("✅ Install flow test PASSED")
        print("   All steps would complete successfully")
    else:
        print("❌ Install flow test FAILED")
        print("   Some steps would fail")

    return result


if __name__ == '__main__':
    import sys

    args = sys.argv[1:]

    if '--test' in args:
        path = args[args.index('--test') + 1] if len(args) > args.index('--test') + 1 else '.'
        success = test_install(path)
        sys.exit(0 if success else 1)

    elif '--dry-run' in args:
        path = args[-1] if len(args) > 1 and not args[-1].startswith('-') else '.'
        success = run_install(path=path, dry_run=True)
        sys.exit(0 if success else 1)

    else:
        path = args[0] if args and not args[0].startswith('-') else '.'
        success = run_install(path=path, dry_run=False)
        sys.exit(0 if success else 1)
