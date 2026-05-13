#!/usr/bin/env python3
"""
CI Test Script for Workspace Scaffolder

Tests that the workspace scaffolder can create project templates correctly.
"""

import sys
import tempfile
from pathlib import Path

# Add memory directory to path for imports
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def test_workspace_scaffolder():
    """Test workspace scaffolder functionality."""

    from workspace_scaffolder import WorkspaceScaffolder

    print("=== WORKSPACE SCAFFOLDER TEST ===")
    print()

    scaffolder = WorkspaceScaffolder()

    # Test available templates
    templates = scaffolder.get_available_templates()
    print(f"Available templates: {len(templates)}")
    for t in templates:
        print(f"  - {t.project_type_id}: {t.name}")

    assert len(templates) > 0, "Should have at least one template"
    print()

    # Test scaffolding in temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        result = scaffolder.scaffold_project(
            template_id="python_starter",
            project_name="test-project",
            location=Path(tmpdir)
        )

        print(f"Scaffolding result: success={result.success}")
        if result.error:
            print(f"Error: {result.error}")
        print(f"Project path: {result.project_path}")

        # Check result status
        assert result.success, f"Scaffolding should succeed: {result.error}"

        # Get the actual project path from the result
        project_path = result.project_path

        print(f"Scaffolded project at: {project_path}")

        # Verify structure
        assert project_path.exists(), "Project directory should exist"
        assert (project_path / "src").exists(), "src/ should exist"
        assert (project_path / ".gitignore").exists(), ".gitignore should exist"

        # List what was created
        print()
        print("Directories created:")
        for d in result.directories_created[:5]:  # First 5
            print(f"  - {d}")
        if len(result.directories_created) > 5:
            print(f"  ... and {len(result.directories_created) - 5} more")

        print()
        print("Files created:")
        for f in result.files_created[:5]:  # First 5
            print(f"  - {f}")
        if len(result.files_created) > 5:
            print(f"  ... and {len(result.files_created) - 5} more")

        print()
        print("[OK] Workspace scaffolding works correctly")


if __name__ == "__main__":
    test_workspace_scaffolder()
