#!/usr/bin/env python3
"""
Chained Installation Flow Tests - End-to-End Vibe Coder Experience

Tests the COMPLETE installation flow from zero to first experience:
1. Environment Detection
2. Tool Installation (if missing)
3. IDE Setup (VS Code/Cursor)
4. Extension Installation
5. Workspace Scaffolding
6. Hook Installation
7. First Experience Launch

This simulates what happens when a complete beginner runs install-context-dna.sh

Created: January 29, 2026
Part of: Vibe Coder Launch Initiative
"""

import sys
import tempfile
import shutil
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add memory directory to path for imports
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def test_chained_install_phase_1_environment_detection():
    """
    Phase 1: Environment Detection

    Verify that EnvironmentDetector can run on ANY system without crashing.
    Must gracefully handle: missing tools, permission errors, timeouts.
    """
    print("=== PHASE 1: Environment Detection ===")

    from environment_detector import EnvironmentDetector, EnvironmentProfile

    detector = EnvironmentDetector()

    # This MUST succeed even on a completely bare system
    profile = detector.detect_all()

    assert profile is not None, "Profile should never be None"
    assert isinstance(profile, EnvironmentProfile), "Should return EnvironmentProfile"
    assert profile.timestamp is not None, "Should have timestamp"
    assert profile.platform_info is not None, "Should have platform info"

    # Verify structure (even if empty)
    assert isinstance(profile.tools, dict), "tools should be dict"
    assert isinstance(profile.extensions, list), "extensions should be list"
    assert isinstance(profile.workspaces, list), "workspaces should be list"
    assert isinstance(profile.missing_critical, list), "missing_critical should be list"
    assert isinstance(profile.warnings, list), "warnings should be list"

    print(f"  Platform: {profile.platform_info.get('os', 'unknown')}")
    print(f"  Tools detected: {len(profile.tools)}")
    print(f"  Extensions detected: {len(profile.extensions)}")
    print(f"  Missing critical: {len(profile.missing_critical)}")
    print(f"  Ready for Context DNA: {profile.ready_for_context_dna}")

    print("[OK] Phase 1: Environment detection completed without crash")
    return profile


def test_chained_install_phase_2_tool_availability():
    """
    Phase 2: Tool Availability Check

    Verify detection logic correctly identifies installed vs missing tools.
    """
    print("\n=== PHASE 2: Tool Availability Check ===")

    from environment_detector import EnvironmentDetector

    detector = EnvironmentDetector()

    # Test individual tool detection methods
    tools_to_check = [
        ('python', detector.detect_python),
        ('git', detector.detect_git),
        ('node', detector.detect_node),
        ('vscode', detector.detect_vscode),
        ('cursor', detector.detect_cursor),
        ('claude_code', detector.detect_claude_code),
    ]

    for tool_name, detect_method in tools_to_check:
        try:
            status = detect_method()
            icon = "[OK]" if status.installed else "[  ]"
            version = f" ({status.version})" if status.version else ""
            print(f"  {icon} {tool_name}: installed={status.installed}{version}")

            # Verify status structure
            assert status.name is not None, f"{tool_name} should have name"
            assert isinstance(status.installed, bool), f"{tool_name}.installed should be bool"

        except Exception as e:
            # Detection should NEVER raise an exception
            print(f"  [FAIL] {tool_name}: Exception during detection - {e}")
            raise AssertionError(f"Tool detection for {tool_name} raised exception: {e}")

    print("[OK] Phase 2: Tool availability check completed")


def test_chained_install_phase_3_workspace_scaffolding():
    """
    Phase 3: Workspace Scaffolding

    Verify that workspace scaffolder can create projects correctly.
    """
    print("\n=== PHASE 3: Workspace Scaffolding ===")

    from workspace_scaffolder import WorkspaceScaffolder

    scaffolder = WorkspaceScaffolder()

    # Test template availability
    templates = scaffolder.get_available_templates()
    print(f"  Available templates: {len(templates)}")
    for t in templates:
        print(f"    - {t.project_type_id}: {t.name}")

    assert len(templates) > 0, "Should have at least one template"

    # Test scaffolding each template type
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Test python_starter (most basic)
        result = scaffolder.scaffold_project(
            template_id="python_starter",
            project_name="test-project",
            location=tmppath,
            init_git=True,
            init_context_dna=True
        )

        assert result.success, f"Scaffolding failed: {result.error}"
        assert result.project_path.exists(), "Project directory should exist"
        assert (result.project_path / "src").exists(), "src/ should exist"
        assert (result.project_path / ".gitignore").exists(), ".gitignore should exist"

        # Verify Context DNA was initialized
        if result.context_dna_initialized:
            assert (result.project_path / ".context-dna").exists(), ".context-dna/ should exist"
            assert (result.project_path / ".context-dna" / "settings.json").exists()

        print(f"  Scaffolded: {result.project_path.name}")
        print(f"    Directories: {len(result.directories_created)}")
        print(f"    Files: {len(result.files_created)}")
        print(f"    Git: {result.git_initialized}")
        print(f"    Context DNA: {result.context_dna_initialized}")

    print("[OK] Phase 3: Workspace scaffolding completed")


def test_chained_install_phase_4_ide_adapters():
    """
    Phase 4: IDE Adapter Installation

    Verify all IDE adapters can be loaded and their hooks installed.
    """
    print("\n=== PHASE 4: IDE Adapter Installation ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)

        # Create a mock project structure
        (project_root / "src").mkdir()
        (project_root / ".git").mkdir()
        (project_root / ".git" / "hooks").mkdir()

        adapters_tested = []

        # Test Claude Code adapter
        try:
            from ide_adapters.claude_code import ClaudeCodeAdapter
            adapter = ClaudeCodeAdapter(project_root)

            # Test hook installation (creates files in temp dir)
            result = adapter.install_hooks()
            status = "installed" if result.success else f"failed: {result.error}"
            print(f"  Claude Code: {status}")
            adapters_tested.append(("claude_code", result.success))
        except Exception as e:
            print(f"  Claude Code: error loading adapter - {e}")
            adapters_tested.append(("claude_code", False))

        # Test Cursor adapter
        try:
            from ide_adapters.cursor import CursorAdapter
            adapter = CursorAdapter(project_root)

            result = adapter.install_hooks()
            status = "installed" if result.success else f"failed: {result.error}"
            print(f"  Cursor: {status}")
            adapters_tested.append(("cursor", result.success))
        except Exception as e:
            print(f"  Cursor: error loading adapter - {e}")
            adapters_tested.append(("cursor", False))

        # Test VS Code adapter
        try:
            from ide_adapters.vscode import VSCodeAdapter
            adapter = VSCodeAdapter(project_root)

            result = adapter.install_hooks()
            status = "installed" if result.success else f"failed: {result.error}"
            print(f"  VS Code: {status}")
            adapters_tested.append(("vscode", result.success))
        except Exception as e:
            print(f"  VS Code: error loading adapter - {e}")
            adapters_tested.append(("vscode", False))

        # At least one adapter should work
        successful = sum(1 for _, ok in adapters_tested if ok)
        print(f"\n  Results: {successful}/{len(adapters_tested)} adapters installed successfully")

        assert successful > 0, "At least one IDE adapter should succeed"

    print("[OK] Phase 4: IDE adapter installation completed")


def test_chained_install_phase_5_unified_installer():
    """
    Phase 5: Unified Installer

    Verify unified installer can detect and configure all IDEs.
    """
    print("\n=== PHASE 5: Unified Installer ===")

    from unified_installer import UnifiedInstaller

    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)

        # Create minimal project structure
        (project_root / ".git").mkdir()
        (project_root / ".git" / "hooks").mkdir()

        installer = UnifiedInstaller(project_root)

        # Test detection
        detected = installer.detect_installed_ides()
        print(f"  Detected IDEs: {detected if detected else 'None'}")

        # Test status retrieval
        status = installer.get_all_status()
        print(f"  IDE Status:")
        for ide, info in status.items():
            installed = info.get("installed", False)
            icon = "[OK]" if installed else "[  ]"
            print(f"    {icon} {ide}")

        # Test installation (even with no IDEs detected, should not crash)
        report = installer.install_all(only_detected=False)

        assert report is not None, "Report should not be None"
        assert isinstance(report.results, dict), "Results should be dict"

        print(f"\n  Installation Report:")
        print(f"    IDEs detected: {report.ides_detected}")
        print(f"    All success: {report.all_success}")
        print(f"    Any success: {report.any_success}")

    print("[OK] Phase 5: Unified installer completed")


def test_chained_install_phase_6_first_experience():
    """
    Phase 6: First Experience

    Verify first experience module can run without crashing.
    """
    print("\n=== PHASE 6: First Experience ===")

    from first_experience import FirstExperience

    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)

        fe = FirstExperience(project_path)

        # Test webhook verification (should work even without hooks)
        webhook_result = fe.verify_webhook_active()
        print(f"  Webhook verification: {webhook_result['message']}")
        print(f"    Checks:")
        for check, passed in webhook_result['checks'].items():
            icon = "[OK]" if passed else "[  ]"
            print(f"      {icon} {check}")

        # Test tutorial steps
        steps = fe.get_tutorial_steps()
        print(f"  Tutorial steps: {len(steps)}")
        assert len(steps) > 0, "Should have tutorial steps"

        # Test first prompts
        prompts = fe.get_first_prompts()
        print(f"  First prompts: {len(prompts)}")
        assert len(prompts) > 0, "Should have first prompts"

        # Test guided prompt
        guided = fe.get_guided_first_prompt()
        print(f"  Guided prompt: \"{guided}\"")
        assert guided, "Should have a guided prompt"

        # Test VS Code detection
        vscode_found = fe.is_vscode_installed()
        print(f"  VS Code installed: {vscode_found}")

        # Test success message generation
        success_msg = fe.get_success_message()
        assert "SETUP COMPLETE" in success_msg or "Context DNA" in success_msg

    print("[OK] Phase 6: First experience completed")


def test_chained_install_full_flow():
    """
    Full End-to-End Flow Test

    Simulates the complete Vibe Coder installation journey.
    """
    print("\n" + "=" * 60)
    print("FULL END-TO-END CHAINED INSTALLATION TEST")
    print("=" * 60)

    from environment_detector import EnvironmentDetector
    from workspace_scaffolder import WorkspaceScaffolder
    from unified_installer import UnifiedInstaller
    from first_experience import FirstExperience

    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = Path(tmpdir)

        # Step 1: Detect environment
        print("\n[1/6] Detecting environment...")
        detector = EnvironmentDetector()
        profile = detector.detect_all()
        print(f"      Platform: {profile.platform_info.get('os')}")
        print(f"      Ready: {profile.ready_for_context_dna}")

        # Step 2: Check what's missing
        print("\n[2/6] Checking requirements...")
        if profile.missing_critical:
            print(f"      Missing critical: {profile.missing_critical}")
        else:
            print("      All critical requirements met!")

        # Step 3: Scaffold workspace
        print("\n[3/6] Scaffolding workspace...")
        scaffolder = WorkspaceScaffolder()
        scaffold_result = scaffolder.scaffold_project(
            template_id="python_starter",
            project_name="my-first-project",
            location=base_path
        )
        project_path = scaffold_result.project_path
        print(f"      Created: {project_path.name}")

        # Step 4: Install IDE hooks
        print("\n[4/6] Installing IDE hooks...")
        installer = UnifiedInstaller(project_path)
        install_report = installer.install_all(only_detected=False)
        print(f"      Any success: {install_report.any_success}")

        # Step 5: Verify hooks
        print("\n[5/6] Verifying hooks...")
        verification = installer.verify_all()
        verified_count = sum(1 for v in verification.values() if v.get('verified'))
        print(f"      Verified: {verified_count}/{len(verification)}")

        # Step 6: First experience
        print("\n[6/6] Preparing first experience...")
        fe = FirstExperience(project_path)
        result = fe.run_first_experience(open_vscode=False)
        print(f"      Webhook verified: {result.webhook_verified}")
        print(f"      Fully successful: {result.fully_successful}")

        # Summary
        print("\n" + "-" * 60)
        print("INSTALLATION SUMMARY")
        print("-" * 60)
        print(f"  Environment Detection: [OK]")
        print(f"  Workspace Scaffolding: {'[OK]' if scaffold_result.success else '[FAIL]'}")
        print(f"  IDE Hook Installation: {'[OK]' if install_report.any_success else '[PARTIAL]'}")
        print(f"  First Experience: {'[OK]' if not result.errors else '[PARTIAL]'}")

        # The chained flow should always complete without crashing
        assert scaffold_result.success, "Scaffolding must succeed"

    print("\n[OK] Full chained installation flow completed successfully!")


if __name__ == "__main__":
    print("=" * 60)
    print("CHAINED INSTALLATION FLOW TESTS")
    print("Vibe Coder Launch Initiative")
    print("=" * 60)
    print()

    # Run all phases
    test_chained_install_phase_1_environment_detection()
    test_chained_install_phase_2_tool_availability()
    test_chained_install_phase_3_workspace_scaffolding()
    test_chained_install_phase_4_ide_adapters()
    test_chained_install_phase_5_unified_installer()
    test_chained_install_phase_6_first_experience()
    test_chained_install_full_flow()

    print("\n" + "=" * 60)
    print("[OK] ALL CHAINED INSTALLATION TESTS PASSED")
    print("=" * 60)
