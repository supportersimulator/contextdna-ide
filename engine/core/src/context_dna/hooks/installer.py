#!/usr/bin/env python3
"""
Hook Installer - Unified Installation for All IDE Hooks

This module provides a unified interface for installing all Context DNA hooks:
- Claude Code (CLAUDE.md)
- Cursor (.cursorrules)
- Git (post-commit)

It handles:
- Detection of which IDEs are in use
- Installation/uninstallation of all hooks
- Status reporting
- Configuration management

Usage:
    from context_dna.hooks.installer import HookInstaller

    installer = HookInstaller()

    # Install all hooks
    installer.install_all()

    # Install specific hooks
    installer.install("claude")
    installer.install("cursor")
    installer.install("git")

    # Check status
    status = installer.status()

    # Uninstall all
    installer.uninstall_all()
"""

import os
from pathlib import Path
from typing import Optional, List, Dict

from context_dna.hooks.claude import (
    install_claude_hook, uninstall_claude_hook, get_claude_hook_status
)
from context_dna.hooks.cursor import (
    install_cursor_hook, uninstall_cursor_hook, get_cursor_hook_status
)
from context_dna.hooks.git import (
    install_git_hook, uninstall_git_hook, get_git_hook_status
)


# =============================================================================
# HOOK INSTALLER CLASS
# =============================================================================

class HookInstaller:
    """
    Unified installer for all Context DNA IDE hooks.

    Handles installation, uninstallation, and status reporting
    for Claude Code, Cursor, and Git hooks.
    """

    SUPPORTED_HOOKS = ["claude", "cursor", "git"]

    def __init__(self, project_dir: Optional[str] = None):
        """
        Initialize the hook installer.

        Args:
            project_dir: Project directory (defaults to cwd)
        """
        self.project_dir = Path(project_dir or os.getcwd())

    def install(self, hook_name: str, force: bool = False) -> dict:
        """
        Install a specific hook.

        Args:
            hook_name: One of "claude", "cursor", "git"
            force: If True, reinstall even if already present

        Returns:
            Result dictionary
        """
        hook_name = hook_name.lower()

        if hook_name == "claude":
            return install_claude_hook(str(self.project_dir), force)
        elif hook_name == "cursor":
            return install_cursor_hook(str(self.project_dir), force)
        elif hook_name == "git":
            return install_git_hook(str(self.project_dir), force)
        else:
            return {
                "success": False,
                "message": f"Unknown hook: {hook_name}. Supported: {self.SUPPORTED_HOOKS}"
            }

    def uninstall(self, hook_name: str) -> dict:
        """
        Uninstall a specific hook.

        Args:
            hook_name: One of "claude", "cursor", "git"

        Returns:
            Result dictionary
        """
        hook_name = hook_name.lower()

        if hook_name == "claude":
            return uninstall_claude_hook(str(self.project_dir))
        elif hook_name == "cursor":
            return uninstall_cursor_hook(str(self.project_dir))
        elif hook_name == "git":
            return uninstall_git_hook(str(self.project_dir))
        else:
            return {
                "success": False,
                "message": f"Unknown hook: {hook_name}. Supported: {self.SUPPORTED_HOOKS}"
            }

    def install_all(self, force: bool = False) -> Dict[str, dict]:
        """
        Install all supported hooks.

        Args:
            force: If True, reinstall even if already present

        Returns:
            Dictionary of {hook_name: result}
        """
        results = {}
        for hook in self.SUPPORTED_HOOKS:
            results[hook] = self.install(hook, force)
        return results

    def uninstall_all(self) -> Dict[str, dict]:
        """
        Uninstall all hooks.

        Returns:
            Dictionary of {hook_name: result}
        """
        results = {}
        for hook in self.SUPPORTED_HOOKS:
            results[hook] = self.uninstall(hook)
        return results

    def status(self, hook_name: Optional[str] = None) -> Dict:
        """
        Get status of hooks.

        Args:
            hook_name: Specific hook to check, or None for all

        Returns:
            Status dictionary
        """
        if hook_name:
            hook_name = hook_name.lower()
            if hook_name == "claude":
                return get_claude_hook_status(str(self.project_dir))
            elif hook_name == "cursor":
                return get_cursor_hook_status(str(self.project_dir))
            elif hook_name == "git":
                return get_git_hook_status(str(self.project_dir))
            else:
                return {"error": f"Unknown hook: {hook_name}"}

        # Return status for all hooks
        return {
            "project_dir": str(self.project_dir),
            "claude": get_claude_hook_status(str(self.project_dir)),
            "cursor": get_cursor_hook_status(str(self.project_dir)),
            "git": get_git_hook_status(str(self.project_dir)),
        }

    def get_installed_hooks(self) -> List[str]:
        """
        Get list of installed hooks.

        Returns:
            List of hook names that are installed
        """
        status = self.status()
        installed = []

        if status.get("claude", {}).get("installed"):
            installed.append("claude")
        if status.get("cursor", {}).get("installed"):
            installed.append("cursor")
        if status.get("git", {}).get("installed"):
            installed.append("git")

        return installed

    def detect_ide(self) -> List[str]:
        """
        Detect which IDEs are likely in use based on project files.

        Returns:
            List of detected IDE names
        """
        detected = []

        # Check for Claude Code indicators
        claude_indicators = [
            self.project_dir / "CLAUDE.md",
            self.project_dir / ".claude",
        ]
        if any(p.exists() for p in claude_indicators):
            detected.append("claude")

        # Check for Cursor indicators
        cursor_indicators = [
            self.project_dir / ".cursorrules",
            self.project_dir / ".cursor",
        ]
        if any(p.exists() for p in cursor_indicators):
            detected.append("cursor")

        # Check for git
        if (self.project_dir / ".git").exists():
            detected.append("git")

        return detected

    def install_detected(self, force: bool = False) -> Dict[str, dict]:
        """
        Install hooks for detected IDEs only.

        Args:
            force: If True, reinstall even if already present

        Returns:
            Dictionary of {hook_name: result}
        """
        detected = self.detect_ide()

        # Always include git if it's a git repo
        if "git" not in detected and (self.project_dir / ".git").exists():
            detected.append("git")

        results = {}
        for hook in detected:
            results[hook] = self.install(hook, force)
        return results


# =============================================================================
# CLI INTERFACE
# =============================================================================

def main():
    import sys

    if len(sys.argv) < 2:
        print("Context DNA Hook Installer")
        print("")
        print("Commands:")
        print("  install [hook]     - Install hooks (all or specific: claude, cursor, git)")
        print("  uninstall [hook]   - Uninstall hooks (all or specific)")
        print("  status [hook]      - Show hook status (all or specific)")
        print("  detect             - Detect IDEs in use")
        print("  install-detected   - Install hooks for detected IDEs only")
        print("")
        print("Examples:")
        print("  context-dna hooks install        # Install all hooks")
        print("  context-dna hooks install claude # Install Claude hook only")
        print("  context-dna hooks status         # Show all hook status")
        print("")
        sys.exit(0)

    cmd = sys.argv[1]
    installer = HookInstaller()

    if cmd == "install":
        if len(sys.argv) > 2:
            # Install specific hook
            hook = sys.argv[2]
            result = installer.install(hook)
            print(result["message"])
            if result.get("path"):
                print(f"Path: {result['path']}")
        else:
            # Install all
            results = installer.install_all()
            for hook, result in results.items():
                status = "OK" if result["success"] else "FAIL"
                print(f"[{status}] {hook}: {result['message']}")

    elif cmd == "uninstall":
        if len(sys.argv) > 2:
            # Uninstall specific hook
            hook = sys.argv[2]
            result = installer.uninstall(hook)
            print(result["message"])
        else:
            # Uninstall all
            results = installer.uninstall_all()
            for hook, result in results.items():
                status = "OK" if result["success"] else "FAIL"
                print(f"[{status}] {hook}: {result['message']}")

    elif cmd == "status":
        if len(sys.argv) > 2:
            # Status of specific hook
            hook = sys.argv[2]
            status = installer.status(hook)
            print(f"=== {hook.title()} Hook Status ===")
            for key, value in status.items():
                print(f"{key}: {value}")
        else:
            # Status of all
            status = installer.status()
            print(f"=== Context DNA Hook Status ===")
            print(f"Project: {status['project_dir']}")
            print("")

            for hook in ["claude", "cursor", "git"]:
                hook_status = status.get(hook, {})
                installed = hook_status.get("installed", False)
                print(f"{hook.title():8} {'[INSTALLED]' if installed else '[not installed]'}")

            print("")
            installed = installer.get_installed_hooks()
            print(f"Installed hooks: {', '.join(installed) if installed else 'none'}")

    elif cmd == "detect":
        detected = installer.detect_ide()
        print("=== Detected IDEs ===")
        if detected:
            for ide in detected:
                print(f"  - {ide}")
        else:
            print("  No IDEs detected")
            print("  (This is a git repo, git hook can still be installed)")

    elif cmd == "install-detected":
        results = installer.install_detected()
        for hook, result in results.items():
            status = "OK" if result["success"] else "FAIL"
            print(f"[{status}] {hook}: {result['message']}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
