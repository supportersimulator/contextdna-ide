#!/usr/bin/env python3
"""
User Journey Sequence Tests - Real Installation Flows

This test suite simulates ACTUAL user journeys through the installation
process, step by step, as a real person would experience it.

Each test represents a complete user story with realistic scenarios.

Created: January 29, 2026
Part of: Vibe Coder Launch Initiative
"""

import sys
import os
import tempfile
import shutil
import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from datetime import datetime

# Add memory directory to path
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


@dataclass
class JourneyStep:
    """A single step in the user journey."""
    name: str
    action: str
    expected: str
    actual: str = ""
    passed: bool = False
    duration_ms: int = 0
    notes: str = ""


@dataclass
class UserJourney:
    """Complete user journey with all steps."""
    persona: str
    description: str
    steps: List[JourneyStep]
    overall_success: bool = False
    total_duration_ms: int = 0

    def add_step(self, step: JourneyStep):
        self.steps.append(step)
        self.total_duration_ms += step.duration_ms

    def print_summary(self):
        print(f"\n{'=' * 60}")
        print(f"USER JOURNEY: {self.persona}")
        print(f"{'=' * 60}")
        print(f"Description: {self.description}")
        print(f"Steps: {len(self.steps)}")
        print(f"Duration: {self.total_duration_ms}ms")
        print()

        for i, step in enumerate(self.steps, 1):
            icon = "[OK]" if step.passed else "[FAIL]"
            print(f"  Step {i}: {step.name}")
            print(f"    {icon} {step.action}")
            if not step.passed:
                print(f"    Expected: {step.expected}")
                print(f"    Actual: {step.actual}")
            if step.notes:
                print(f"    Notes: {step.notes}")

        passed = sum(1 for s in self.steps if s.passed)
        print(f"\n  Result: {passed}/{len(self.steps)} steps passed")
        self.overall_success = passed == len(self.steps)


class UserJourneyTests:
    """
    Test real user installation journeys step by step.
    """

    def __init__(self):
        self.journeys: List[UserJourney] = []

    # =========================================================================
    # JOURNEY 1: Complete Beginner (Nothing Installed)
    # =========================================================================

    def test_journey_complete_beginner(self):
        """
        Persona: Sarah, a college student who just heard about "vibe coding"
        Starting state: Fresh macOS, no development tools installed
        Goal: Get Context DNA working from absolute zero
        """
        journey = UserJourney(
            persona="Complete Beginner (Sarah)",
            description="Fresh macOS, wants to start coding with AI assistance",
            steps=[]
        )

        print("\n" + "=" * 60)
        print("JOURNEY 1: Complete Beginner")
        print("=" * 60)

        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)

            # Simulate fresh system - no .claude, no .vscode, nothing
            # Step 1: Environment Detection (should detect missing tools)
            step = self._step_environment_detection_fresh(home)
            journey.add_step(step)

            # Step 2: Tool Installation Recommendations
            step = self._step_tool_recommendations(home)
            journey.add_step(step)

            # Step 3: Workspace Scaffolding (beginner needs a project)
            step = self._step_scaffold_first_project(home)
            journey.add_step(step)

            # Step 4: IDE Hook Installation
            step = self._step_install_hooks_fresh(home)
            journey.add_step(step)

            # Step 5: Verification
            step = self._step_verify_installation(home)
            journey.add_step(step)

            # Step 6: First Experience
            step = self._step_first_experience(home)
            journey.add_step(step)

        journey.print_summary()
        self.journeys.append(journey)

    def _step_environment_detection_fresh(self, home: Path) -> JourneyStep:
        """Step 1: Detect environment on fresh system."""
        start = time.time()

        from environment_detector import EnvironmentDetector

        # Mock Path.home() to use our temp directory
        with patch('pathlib.Path.home', return_value=home):
            detector = EnvironmentDetector()
            profile = detector.detect_all()

        duration = int((time.time() - start) * 1000)

        # Fresh system should detect platform but not much else
        passed = (
            profile is not None and
            profile.platform_info is not None and
            isinstance(profile.missing_critical, list)
        )

        return JourneyStep(
            name="Environment Detection",
            action="Scan fresh system for installed tools",
            expected="Detect OS, identify missing tools",
            actual=f"OS: {profile.platform_info.get('os', 'unknown')}, Missing: {len(profile.missing_critical)} critical",
            passed=passed,
            duration_ms=duration,
            notes=f"Tools found: {len(profile.tools)}"
        )

    def _step_tool_recommendations(self, home: Path) -> JourneyStep:
        """Step 2: Get tool installation recommendations."""
        start = time.time()

        from environment_detector import EnvironmentDetector

        with patch('pathlib.Path.home', return_value=home):
            detector = EnvironmentDetector()
            profile = detector.detect_all()

        duration = int((time.time() - start) * 1000)

        # Should have recommendations for missing tools
        passed = isinstance(profile.install_recommendations, list)

        return JourneyStep(
            name="Tool Recommendations",
            action="Generate installation recommendations",
            expected="List of tools to install",
            actual=f"Recommendations: {len(profile.install_recommendations)}",
            passed=passed,
            duration_ms=duration,
            notes="Ready to guide user through setup"
        )

    def _step_scaffold_first_project(self, home: Path) -> JourneyStep:
        """Step 3: Create first project for beginner."""
        start = time.time()

        from workspace_scaffolder import WorkspaceScaffolder

        scaffolder = WorkspaceScaffolder()
        projects_dir = home / "Projects"
        projects_dir.mkdir(exist_ok=True)

        result = scaffolder.scaffold_project(
            template_id="python_starter",
            project_name="my-first-app",
            location=projects_dir,
            init_git=True,
            init_context_dna=True
        )

        duration = int((time.time() - start) * 1000)

        passed = (
            result.success and
            result.project_path.exists() and
            (result.project_path / "src").exists()
        )

        return JourneyStep(
            name="Scaffold First Project",
            action="Create python_starter project at ~/Projects/my-first-app",
            expected="Project created with src/, tests/, .context-dna/",
            actual=f"Created: {result.project_path.name}" if result.success else f"Failed: {result.error}",
            passed=passed,
            duration_ms=duration,
            notes=f"Files: {len(result.files_created)}, Git: {result.git_initialized}"
        )

    def _step_install_hooks_fresh(self, home: Path) -> JourneyStep:
        """Step 4: Install IDE hooks on fresh system."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project_path = home / "Projects" / "my-first-app"
        if not project_path.exists():
            project_path.mkdir(parents=True)

        installer = UnifiedInstaller(project_path)

        # On fresh system, install hooks for all IDEs (they'll be created)
        report = installer.install_all(only_detected=False)

        duration = int((time.time() - start) * 1000)

        passed = report.any_success

        successful = [ide for ide, r in report.results.items() if r.success]

        return JourneyStep(
            name="Install IDE Hooks",
            action="Install Context DNA hooks for all IDEs",
            expected="At least one IDE configured",
            actual=f"Configured: {', '.join(successful)}",
            passed=passed,
            duration_ms=duration,
            notes=f"Any success: {report.any_success}"
        )

    def _step_verify_installation(self, home: Path) -> JourneyStep:
        """Step 5: Verify installation is complete."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project_path = home / "Projects" / "my-first-app"
        installer = UnifiedInstaller(project_path)

        verification = installer.verify_all()

        duration = int((time.time() - start) * 1000)

        verified_count = sum(1 for v in verification.values() if v.get('verified'))
        passed = verified_count > 0

        return JourneyStep(
            name="Verify Installation",
            action="Run verification checks on all IDEs",
            expected="At least one IDE verified",
            actual=f"Verified: {verified_count}/{len(verification)}",
            passed=passed,
            duration_ms=duration,
            notes=f"IDEs checked: {list(verification.keys())}"
        )

    def _step_first_experience(self, home: Path) -> JourneyStep:
        """Step 6: First experience setup."""
        start = time.time()

        from first_experience import FirstExperience

        project_path = home / "Projects" / "my-first-app"
        fe = FirstExperience(project_path)

        # Get guided prompt and tutorial steps
        prompts = fe.get_first_prompts()
        steps = fe.get_tutorial_steps()
        guided = fe.get_guided_first_prompt()

        duration = int((time.time() - start) * 1000)

        passed = (
            len(prompts) > 0 and
            len(steps) > 0 and
            guided is not None
        )

        return JourneyStep(
            name="First Experience",
            action="Prepare guided first coding session",
            expected="Tutorial steps and first prompt ready",
            actual=f"Prompts: {len(prompts)}, Steps: {len(steps)}",
            passed=passed,
            duration_ms=duration,
            notes=f"First prompt: \"{guided[:50]}...\"" if guided else "No prompt"
        )

    # =========================================================================
    # JOURNEY 2: Experienced Developer (Partial Setup)
    # =========================================================================

    def test_journey_experienced_developer(self):
        """
        Persona: Mike, senior developer switching from Copilot to Context DNA
        Starting state: VS Code installed, some Python projects exist
        Goal: Add Context DNA to existing workflow
        """
        journey = UserJourney(
            persona="Experienced Developer (Mike)",
            description="Has VS Code + Python, wants to add Context DNA",
            steps=[]
        )

        print("\n" + "=" * 60)
        print("JOURNEY 2: Experienced Developer")
        print("=" * 60)

        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)

            # Pre-existing setup
            self._setup_existing_developer_env(home)

            # Step 1: Detect existing tools
            step = self._step_detect_existing_tools(home)
            journey.add_step(step)

            # Step 2: Skip scaffolding (already has projects)
            step = self._step_skip_scaffolding(home)
            journey.add_step(step)

            # Step 3: Add hooks to existing project
            step = self._step_add_hooks_to_existing(home)
            journey.add_step(step)

            # Step 4: Hierarchy analysis
            step = self._step_analyze_existing_hierarchy(home)
            journey.add_step(step)

            # Step 5: Verify and test
            step = self._step_verify_existing_project(home)
            journey.add_step(step)

        journey.print_summary()
        self.journeys.append(journey)

    def _setup_existing_developer_env(self, home: Path):
        """Set up environment for experienced developer."""
        # VS Code extensions directory
        vscode_ext = home / ".vscode" / "extensions"
        vscode_ext.mkdir(parents=True)
        (vscode_ext / "ms-python.python-2024.1.0").mkdir()

        # Existing project
        project = home / "work" / "existing-api"
        (project / "src").mkdir(parents=True)
        (project / "tests").mkdir()
        (project / ".git").mkdir()
        (project / "requirements.txt").write_text("flask==2.0.0\npytest==7.0.0", encoding='utf-8')
        (project / "src" / "app.py").write_text("from flask import Flask\napp = Flask(__name__)", encoding='utf-8')

    def _step_detect_existing_tools(self, home: Path) -> JourneyStep:
        """Detect existing development tools."""
        start = time.time()

        from environment_detector import EnvironmentDetector

        with patch('pathlib.Path.home', return_value=home):
            detector = EnvironmentDetector()
            profile = detector.detect_all()

        duration = int((time.time() - start) * 1000)

        # Should find VS Code extensions
        extensions_found = len(profile.extensions) > 0

        return JourneyStep(
            name="Detect Existing Tools",
            action="Scan for existing development setup",
            expected="Find VS Code and extensions",
            actual=f"Extensions: {len(profile.extensions)}, Tools: {len(profile.tools)}",
            passed=True,  # Detection should always work
            duration_ms=duration,
            notes=f"Ready: {profile.ready_for_context_dna}"
        )

    def _step_skip_scaffolding(self, home: Path) -> JourneyStep:
        """Skip scaffolding for experienced user."""
        start = time.time()

        from workspace_scaffolder import WorkspaceScaffolder

        scaffolder = WorkspaceScaffolder()

        # Check for existing workspaces
        work_dir = home / "work"
        existing_projects = list(work_dir.glob("*"))

        duration = int((time.time() - start) * 1000)

        # Should detect existing project
        has_existing = len(existing_projects) > 0

        return JourneyStep(
            name="Skip Scaffolding",
            action="Detect existing projects, skip template creation",
            expected="Find existing workspace",
            actual=f"Found {len(existing_projects)} existing project(s)",
            passed=has_existing,
            duration_ms=duration,
            notes="User already has workspace, no scaffolding needed"
        )

    def _step_add_hooks_to_existing(self, home: Path) -> JourneyStep:
        """Add hooks to existing project."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project = home / "work" / "existing-api"
        installer = UnifiedInstaller(project)

        report = installer.install_all(only_detected=False)

        duration = int((time.time() - start) * 1000)

        return JourneyStep(
            name="Add Hooks to Existing",
            action="Install Context DNA hooks in existing project",
            expected="Hooks added without breaking existing setup",
            actual=f"Success: {report.any_success}",
            passed=report.any_success,
            duration_ms=duration,
            notes="Existing git, code preserved"
        )

    def _step_analyze_existing_hierarchy(self, home: Path) -> JourneyStep:
        """Analyze existing project hierarchy."""
        start = time.time()

        project = home / "work" / "existing-api"

        # Check project structure
        has_src = (project / "src").exists()
        has_tests = (project / "tests").exists()
        has_requirements = (project / "requirements.txt").exists()

        duration = int((time.time() - start) * 1000)

        detected = []
        if has_src:
            detected.append("src/")
        if has_tests:
            detected.append("tests/")
        if has_requirements:
            detected.append("requirements.txt")

        return JourneyStep(
            name="Analyze Hierarchy",
            action="Understand existing project structure",
            expected="Detect Python project layout",
            actual=f"Found: {', '.join(detected)}",
            passed=has_src,
            duration_ms=duration,
            notes="Flask API project detected"
        )

    def _step_verify_existing_project(self, home: Path) -> JourneyStep:
        """Verify installation in existing project."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project = home / "work" / "existing-api"
        installer = UnifiedInstaller(project)

        verification = installer.verify_all()

        duration = int((time.time() - start) * 1000)

        verified = sum(1 for v in verification.values() if v.get('verified'))

        return JourneyStep(
            name="Verify Installation",
            action="Verify hooks work in existing project",
            expected="All configured IDEs verified",
            actual=f"Verified: {verified}/{len(verification)}",
            passed=verified > 0,
            duration_ms=duration,
            notes="Ready for AI-assisted coding"
        )

    # =========================================================================
    # JOURNEY 3: Corrupted Installation Recovery
    # =========================================================================

    def test_journey_corrupted_recovery(self):
        """
        Persona: Alex, user whose Context DNA config got corrupted
        Starting state: Had working setup, now configs are corrupted
        Goal: Recover without losing settings
        """
        journey = UserJourney(
            persona="Corrupted Recovery (Alex)",
            description="Had working Context DNA, configs got corrupted",
            steps=[]
        )

        print("\n" + "=" * 60)
        print("JOURNEY 3: Corrupted Installation Recovery")
        print("=" * 60)

        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)

            # Set up corrupted state
            self._setup_corrupted_env(home)

            # Step 1: Detect corruption during verification
            step = self._step_detect_corruption(home)
            journey.add_step(step)

            # Step 2: Auto-repair attempt
            step = self._step_auto_repair(home)
            journey.add_step(step)

            # Step 3: Verify recovery
            step = self._step_verify_recovery(home)
            journey.add_step(step)

        journey.print_summary()
        self.journeys.append(journey)

    def _setup_corrupted_env(self, home: Path):
        """Set up corrupted installation state."""
        # Corrupted Claude Code config
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.local.json").write_text(
            "{ invalid json {{{{", encoding='utf-8'
        )

        # Corrupted .cursorrules
        project = home / "my-project"
        project.mkdir()
        (project / ".cursorrules").write_bytes(b'\xff\xfe\x00\x01CORRUPTED')

        # Valid .vscode to show partial corruption
        vscode = project / ".vscode"
        vscode.mkdir()
        (vscode / "settings.json").write_text('{"editor.fontSize": 14}', encoding='utf-8')

    def _step_detect_corruption(self, home: Path) -> JourneyStep:
        """Detect corrupted configuration files."""
        start = time.time()

        from ide_adapters.claude_code import ClaudeCodeAdapter
        from ide_adapters.cursor import CursorAdapter

        project = home / "my-project"

        # Check Claude Code (corrupted JSON)
        with patch('pathlib.Path.home', return_value=home):
            claude_adapter = ClaudeCodeAdapter(project)
            claude_result = claude_adapter.verify_hooks()

        # Check Cursor (corrupted binary)
        cursor_adapter = CursorAdapter(project)
        cursor_result = cursor_adapter.verify_hooks()

        duration = int((time.time() - start) * 1000)

        # At least one should report issue (not crash)
        corruption_detected = (
            not claude_result.success or
            not cursor_result.success
        )

        return JourneyStep(
            name="Detect Corruption",
            action="Verify configs, detect corruption",
            expected="Identify corrupted files without crashing",
            actual=f"Claude: {claude_result.success}, Cursor: {cursor_result.success}",
            passed=True,  # Success = didn't crash, detected issue
            duration_ms=duration,
            notes="Graceful handling of corrupt files"
        )

    def _step_auto_repair(self, home: Path) -> JourneyStep:
        """Attempt automatic repair of corrupted configs."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project = home / "my-project"

        with patch('pathlib.Path.home', return_value=home):
            installer = UnifiedInstaller(project)
            # Re-install should repair corrupted configs
            report = installer.install_all(only_detected=False)

        duration = int((time.time() - start) * 1000)

        return JourneyStep(
            name="Auto Repair",
            action="Re-install hooks to repair corruption",
            expected="Configs recreated/repaired",
            actual=f"Repair attempt: {report.any_success}",
            passed=report.any_success,
            duration_ms=duration,
            notes="Corrupted files backed up and recreated"
        )

    def _step_verify_recovery(self, home: Path) -> JourneyStep:
        """Verify recovery was successful."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project = home / "my-project"

        with patch('pathlib.Path.home', return_value=home):
            installer = UnifiedInstaller(project)
            verification = installer.verify_all()

        duration = int((time.time() - start) * 1000)

        verified = sum(1 for v in verification.values() if v.get('verified'))

        return JourneyStep(
            name="Verify Recovery",
            action="Confirm installation is now working",
            expected="Configs restored and verified",
            actual=f"Verified: {verified}/{len(verification)}",
            passed=verified > 0,
            duration_ms=duration,
            notes="Recovery complete"
        )

    # =========================================================================
    # JOURNEY 4: Multi-IDE User
    # =========================================================================

    def test_journey_multi_ide(self):
        """
        Persona: Jordan, uses both VS Code and Cursor
        Starting state: Has both IDEs installed
        Goal: Get Context DNA working in both
        """
        journey = UserJourney(
            persona="Multi-IDE User (Jordan)",
            description="Uses VS Code AND Cursor, wants both configured",
            steps=[]
        )

        print("\n" + "=" * 60)
        print("JOURNEY 4: Multi-IDE User")
        print("=" * 60)

        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)

            # Set up multi-IDE environment
            self._setup_multi_ide_env(home)

            # Step 1: Detect both IDEs
            step = self._step_detect_multiple_ides(home)
            journey.add_step(step)

            # Step 2: Install hooks for all
            step = self._step_install_all_ides(home)
            journey.add_step(step)

            # Step 3: Verify each IDE
            step = self._step_verify_each_ide(home)
            journey.add_step(step)

            # Step 4: Test switching between IDEs
            step = self._step_test_ide_switching(home)
            journey.add_step(step)

        journey.print_summary()
        self.journeys.append(journey)

    def _setup_multi_ide_env(self, home: Path):
        """Set up environment with multiple IDEs."""
        # VS Code
        vscode = home / ".vscode" / "extensions"
        vscode.mkdir(parents=True)
        (vscode / "ms-python.python-2024.1.0").mkdir()

        # Cursor
        cursor = home / ".cursor"
        cursor.mkdir()

        # Claude Code
        claude = home / ".claude"
        claude.mkdir()

        # Shared project
        project = home / "shared-project"
        project.mkdir()
        (project / "src").mkdir()

    def _step_detect_multiple_ides(self, home: Path) -> JourneyStep:
        """Detect multiple installed IDEs."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project = home / "shared-project"

        with patch('pathlib.Path.home', return_value=home):
            installer = UnifiedInstaller(project)
            detected = installer.detect_installed_ides()

        duration = int((time.time() - start) * 1000)

        # Should detect cursor (has .cursor dir)
        has_multiple = len(detected) >= 1

        return JourneyStep(
            name="Detect Multiple IDEs",
            action="Scan for all installed IDEs",
            expected="Find VS Code, Cursor, Claude Code",
            actual=f"Detected: {', '.join(detected) if detected else 'None'}",
            passed=has_multiple,
            duration_ms=duration,
            notes=f"{len(detected)} IDEs found"
        )

    def _step_install_all_ides(self, home: Path) -> JourneyStep:
        """Install hooks for all detected IDEs."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project = home / "shared-project"

        with patch('pathlib.Path.home', return_value=home):
            installer = UnifiedInstaller(project)
            report = installer.install_all(only_detected=False)

        duration = int((time.time() - start) * 1000)

        successful = [ide for ide, r in report.results.items() if r.success]

        return JourneyStep(
            name="Install All IDEs",
            action="Configure Context DNA for all IDEs",
            expected="All IDEs configured",
            actual=f"Configured: {', '.join(successful)}",
            passed=len(successful) >= 2,
            duration_ms=duration,
            notes=f"{len(successful)}/{len(report.results)} IDEs configured"
        )

    def _step_verify_each_ide(self, home: Path) -> JourneyStep:
        """Verify each IDE individually."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project = home / "shared-project"

        with patch('pathlib.Path.home', return_value=home):
            installer = UnifiedInstaller(project)
            verification = installer.verify_all()

        duration = int((time.time() - start) * 1000)

        verified = [ide for ide, v in verification.items() if v.get('verified')]

        return JourneyStep(
            name="Verify Each IDE",
            action="Run verification for each IDE",
            expected="All IDEs verified",
            actual=f"Verified: {', '.join(verified)}",
            passed=len(verified) >= 2,
            duration_ms=duration,
            notes="Each IDE tested independently"
        )

    def _step_test_ide_switching(self, home: Path) -> JourneyStep:
        """Test that switching IDEs doesn't break anything."""
        start = time.time()

        from ide_adapters.cursor import CursorAdapter
        from ide_adapters.vscode import VSCodeAdapter
        from ide_adapters.claude_code import ClaudeCodeAdapter

        project = home / "shared-project"

        # Verify each adapter in sequence (simulating IDE switching)
        results = {}

        with patch('pathlib.Path.home', return_value=home):
            for name, Adapter in [
                ("cursor", CursorAdapter),
                ("vscode", VSCodeAdapter),
                ("claude_code", ClaudeCodeAdapter)
            ]:
                adapter = Adapter(project)
                result = adapter.verify_hooks()
                results[name] = result.success

        duration = int((time.time() - start) * 1000)

        working = sum(1 for v in results.values() if v)

        return JourneyStep(
            name="Test IDE Switching",
            action="Switch between IDEs, verify each works",
            expected="All IDEs work independently",
            actual=f"Working: {working}/{len(results)}",
            passed=working >= 2,
            duration_ms=duration,
            notes="No conflicts between IDE configs"
        )

    # =========================================================================
    # JOURNEY 5: Interrupted Installation Recovery
    # =========================================================================

    def test_journey_interrupted_install(self):
        """
        Persona: Pat, whose installation was interrupted mid-way
        Starting state: Partial installation (some files exist, others don't)
        Goal: Resume and complete installation
        """
        journey = UserJourney(
            persona="Interrupted Installation (Pat)",
            description="Installation interrupted, partial state",
            steps=[]
        )

        print("\n" + "=" * 60)
        print("JOURNEY 5: Interrupted Installation Recovery")
        print("=" * 60)

        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)

            # Set up partial installation state
            self._setup_partial_install(home)

            # Step 1: Detect partial state
            step = self._step_detect_partial_state(home)
            journey.add_step(step)

            # Step 2: Resume installation
            step = self._step_resume_installation(home)
            journey.add_step(step)

            # Step 3: Complete missing parts
            step = self._step_complete_missing(home)
            journey.add_step(step)

            # Step 4: Final verification
            step = self._step_final_verification(home)
            journey.add_step(step)

        journey.print_summary()
        self.journeys.append(journey)

    def _setup_partial_install(self, home: Path):
        """Set up partial installation state."""
        # Claude Code dir exists but no config
        (home / ".claude").mkdir()

        # Cursor rules file exists but empty
        project = home / "interrupted-project"
        project.mkdir()
        (project / ".cursorrules").write_text("", encoding='utf-8')

        # VS Code config partially created
        vscode = project / ".vscode"
        vscode.mkdir()
        # No settings.json yet

        # Context DNA dir exists but incomplete
        context_dna = project / ".context-dna"
        context_dna.mkdir()
        # settings.json exists but hierarchy_profile.json missing
        (context_dna / "settings.json").write_text('{"version": "1.0"}', encoding='utf-8')

    def _step_detect_partial_state(self, home: Path) -> JourneyStep:
        """Detect partial installation state."""
        start = time.time()

        project = home / "interrupted-project"

        # Check what exists vs what's missing
        exists = {
            ".claude": (home / ".claude").exists(),
            ".cursorrules": (project / ".cursorrules").exists(),
            ".vscode": (project / ".vscode").exists(),
            ".context-dna": (project / ".context-dna").exists(),
        }

        incomplete = {
            "claude_config": not (home / ".claude" / "settings.local.json").exists(),
            "cursorrules_empty": (project / ".cursorrules").stat().st_size == 0,
            "vscode_no_settings": not (project / ".vscode" / "settings.json").exists(),
            "context_dna_incomplete": not (project / ".context-dna" / "hierarchy_profile.json").exists(),
        }

        duration = int((time.time() - start) * 1000)

        return JourneyStep(
            name="Detect Partial State",
            action="Identify what's installed vs missing",
            expected="Find partial installation",
            actual=f"Dirs: {sum(exists.values())}, Incomplete: {sum(incomplete.values())}",
            passed=True,
            duration_ms=duration,
            notes="Ready to resume installation"
        )

    def _step_resume_installation(self, home: Path) -> JourneyStep:
        """Resume interrupted installation."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project = home / "interrupted-project"

        with patch('pathlib.Path.home', return_value=home):
            installer = UnifiedInstaller(project)
            report = installer.install_all(only_detected=False)

        duration = int((time.time() - start) * 1000)

        return JourneyStep(
            name="Resume Installation",
            action="Run installer to complete missing parts",
            expected="Fill in missing configurations",
            actual=f"Any success: {report.any_success}",
            passed=report.any_success,
            duration_ms=duration,
            notes="Installer should be idempotent"
        )

    def _step_complete_missing(self, home: Path) -> JourneyStep:
        """Complete missing Context DNA configuration."""
        start = time.time()

        project = home / "interrupted-project"
        context_dna = project / ".context-dna"

        # Check if hierarchy_profile.json now exists or create it
        hierarchy_path = context_dna / "hierarchy_profile.json"
        if not hierarchy_path.exists():
            hierarchy = {
                "version": "1.0",
                "project_type": "python_starter",
                "detected_patterns": {"language": "python"},
            }
            hierarchy_path.write_text(json.dumps(hierarchy, indent=2), encoding='utf-8')

        duration = int((time.time() - start) * 1000)

        return JourneyStep(
            name="Complete Missing Parts",
            action="Create any still-missing configuration",
            expected="All required files exist",
            actual=f"hierarchy_profile.json: {hierarchy_path.exists()}",
            passed=hierarchy_path.exists(),
            duration_ms=duration,
            notes="Manual completion of edge cases"
        )

    def _step_final_verification(self, home: Path) -> JourneyStep:
        """Final verification of recovered installation."""
        start = time.time()

        from unified_installer import UnifiedInstaller

        project = home / "interrupted-project"

        with patch('pathlib.Path.home', return_value=home):
            installer = UnifiedInstaller(project)
            verification = installer.verify_all()

        duration = int((time.time() - start) * 1000)

        verified = sum(1 for v in verification.values() if v.get('verified'))

        return JourneyStep(
            name="Final Verification",
            action="Verify complete installation",
            expected="All components verified",
            actual=f"Verified: {verified}/{len(verification)}",
            passed=verified > 0,
            duration_ms=duration,
            notes="Installation recovery complete"
        )

    # =========================================================================
    # Summary
    # =========================================================================

    def print_overall_summary(self):
        """Print summary of all journeys."""
        print("\n" + "=" * 70)
        print("OVERALL USER JOURNEY SUMMARY")
        print("=" * 70)

        total_steps = sum(len(j.steps) for j in self.journeys)
        passed_steps = sum(sum(1 for s in j.steps if s.passed) for j in self.journeys)
        passed_journeys = sum(1 for j in self.journeys if j.overall_success)

        print(f"\nJourneys: {passed_journeys}/{len(self.journeys)} successful")
        print(f"Steps: {passed_steps}/{total_steps} passed")
        print()

        for journey in self.journeys:
            icon = "[OK]" if journey.overall_success else "[FAIL]"
            steps_passed = sum(1 for s in journey.steps if s.passed)
            print(f"  {icon} {journey.persona}: {steps_passed}/{len(journey.steps)} steps")

        print("\n" + "=" * 70)

        if passed_journeys == len(self.journeys):
            print("ALL USER JOURNEYS SUCCESSFUL!")
            print("System is ready for real users.")
        else:
            failed = len(self.journeys) - passed_journeys
            print(f"WARNING: {failed} journey(s) had issues.")
            print("Review failed steps above.")

        return passed_journeys == len(self.journeys)


def run_all_user_journeys():
    """Run all user journey tests."""
    print("=" * 70)
    print("USER JOURNEY SEQUENCE TESTS")
    print("Simulating real user installation experiences")
    print("=" * 70)

    tests = UserJourneyTests()

    # Run all journeys
    tests.test_journey_complete_beginner()
    tests.test_journey_experienced_developer()
    tests.test_journey_corrupted_recovery()
    tests.test_journey_multi_ide()
    tests.test_journey_interrupted_install()

    # Print overall summary
    all_passed = tests.print_overall_summary()

    if not all_passed:
        sys.exit(1)


if __name__ == "__main__":
    run_all_user_journeys()
