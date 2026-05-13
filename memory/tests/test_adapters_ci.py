#!/usr/bin/env python3
"""
CI Test Script for IDE Adapters

Tests that all IDE adapters correctly report "not installed" when run
in a clean environment without any IDE configuration files.

This script creates a temporary directory and tests from there,
ensuring we don't pick up any .cursorrules, .vscode/, .claude/ etc
from the actual repo.
"""

import sys
import tempfile
from pathlib import Path

# Add memory directory to path for imports
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def test_adapters_in_clean_environment():
    """Test all adapters in a clean temp directory."""

    print("=== IDE ADAPTER DETECTION TEST (Clean Environment) ===")
    print()

    # Create a clean temp directory - no IDE files present
    with tempfile.TemporaryDirectory() as tmpdir:
        clean_dir = Path(tmpdir)
        print(f"Testing from clean directory: {clean_dir}")
        print()

        adapters_tested = []

        # Claude Code Adapter
        try:
            from ide_adapters.claude_code import ClaudeCodeAdapter
            adapter = ClaudeCodeAdapter(clean_dir)
            is_installed = adapter.is_installed()
            status = "Detected" if is_installed else "Not detected"
            print(f"Claude Code: {status} (expected: Not detected)")
            adapters_tested.append(("claude_code", not is_installed))
        except Exception as e:
            print(f"Claude Code: Error - {e}")
            adapters_tested.append(("claude_code", False))

        # Cursor Adapter
        try:
            from ide_adapters.cursor import CursorAdapter
            adapter = CursorAdapter(clean_dir)
            is_installed = adapter.is_installed()
            status = "Detected" if is_installed else "Not detected"
            print(f"Cursor: {status} (expected: Not detected)")
            adapters_tested.append(("cursor", not is_installed))
        except Exception as e:
            print(f"Cursor: Error - {e}")
            adapters_tested.append(("cursor", False))

        # VS Code Adapter
        try:
            from ide_adapters.vscode import VSCodeAdapter
            adapter = VSCodeAdapter(clean_dir)
            is_installed = adapter.is_installed()
            status = "Detected" if is_installed else "Not detected"
            print(f"VS Code: {status} (expected: Not detected)")
            adapters_tested.append(("vscode", not is_installed))
        except Exception as e:
            print(f"VS Code: Error - {e}")
            adapters_tested.append(("vscode", False))

        # Antigravity Adapter
        try:
            from ide_adapters.antigravity import AntigravityAdapter
            adapter = AntigravityAdapter(clean_dir)
            is_installed = adapter.is_installed()
            status = "Detected" if is_installed else "Not detected"
            print(f"Antigravity: {status} (expected: Not detected)")
            adapters_tested.append(("antigravity", not is_installed))
        except Exception as e:
            print(f"Antigravity: Error - {e}")
            adapters_tested.append(("antigravity", False))

        print()
        passed = sum(1 for _, ok in adapters_tested if ok)
        total = len(adapters_tested)
        print(f"Results: {passed}/{total} adapters correctly report 'not installed' on clean system")

        # Report failures for debugging
        failures = [name for name, ok in adapters_tested if not ok]
        if failures:
            print(f"\nFailed adapters: {', '.join(failures)}")

        # Assert all passed
        assert all(ok for _, ok in adapters_tested), \
            f"Some adapters incorrectly detected on clean system: {failures}"

        print("\n[OK] All adapters correctly handle clean environment")


if __name__ == "__main__":
    test_adapters_in_clean_environment()
