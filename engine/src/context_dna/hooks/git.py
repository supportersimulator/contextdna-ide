#!/usr/bin/env python3
"""
Git Hook - Post-Commit Auto-Learning

This module installs a git post-commit hook that:
1. Analyzes commit messages for learnable patterns
2. Auto-extracts infrastructure changes (docker, terraform, etc.)
3. Records significant commits as learnings

The hook runs automatically after every commit, making learning 100% automatic.

Usage:
    from context_dna.hooks.git import install_git_hook

    # Install in current project
    install_git_hook()

    # Install in specific directory
    install_git_hook("/path/to/project")
"""

import os
import stat
from pathlib import Path
from typing import Optional


# =============================================================================
# GIT POST-COMMIT HOOK TEMPLATE
# =============================================================================

GIT_HOOK_TEMPLATE = '''#!/bin/bash
# Context DNA Git Post-Commit Hook
# Auto-learns from infrastructure commits

# Run context-dna auto-learn silently
# This analyzes the commit and records relevant learnings
context-dna auto-learn git-commit 2>/dev/null || true

# Context DNA Hook v1.0
'''

# Marker to identify Context DNA hook
HOOK_MARKER = "# Context DNA Git Post-Commit Hook"


# =============================================================================
# INSTALLATION FUNCTIONS
# =============================================================================

def find_git_hooks_dir(project_dir: Optional[str] = None) -> Optional[Path]:
    """
    Find .git/hooks directory in the project.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        Path to .git/hooks if found, None otherwise
    """
    project_dir = Path(project_dir or os.getcwd())

    # Check for .git directory
    git_dir = project_dir / ".git"
    if not git_dir.exists():
        # Check if we're in a subdirectory of a git repo
        current = project_dir
        while current != current.parent:
            git_dir = current / ".git"
            if git_dir.exists():
                break
            current = current.parent

        if not git_dir.exists():
            return None

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    return hooks_dir


def check_git_hook(project_dir: Optional[str] = None) -> bool:
    """
    Check if Context DNA git hook is installed.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        True if hook is installed
    """
    hooks_dir = find_git_hooks_dir(project_dir)
    if not hooks_dir:
        return False

    post_commit = hooks_dir / "post-commit"
    if not post_commit.exists():
        return False

    content = post_commit.read_text()
    return HOOK_MARKER in content


def install_git_hook(project_dir: Optional[str] = None, force: bool = False) -> dict:
    """
    Install Context DNA post-commit hook.

    If post-commit doesn't exist, creates it.
    If it exists, appends Context DNA section.

    Args:
        project_dir: Project directory (defaults to cwd)
        force: If True, reinstall even if already present

    Returns:
        Result dictionary with status and details
    """
    result = {
        "success": False,
        "action": None,
        "path": None,
        "message": "",
    }

    hooks_dir = find_git_hooks_dir(project_dir)
    if not hooks_dir:
        result["message"] = "Not a git repository (no .git directory found)"
        return result

    # Check if already installed
    if check_git_hook(project_dir) and not force:
        result["message"] = "Context DNA git hook already installed"
        result["success"] = True
        result["action"] = "already_installed"
        return result

    post_commit = hooks_dir / "post-commit"

    if post_commit.exists():
        # Append to existing post-commit
        content = post_commit.read_text()

        # Remove old hook if present (for reinstall)
        if HOOK_MARKER in content:
            lines = content.split("\n")
            new_lines = []
            skip = False
            for line in lines:
                if HOOK_MARKER in line:
                    skip = True
                    continue
                if skip and line.startswith("# Context DNA Hook"):
                    skip = False
                    continue
                if not skip:
                    new_lines.append(line)
            content = "\n".join(new_lines)

        # Append Context DNA section
        if not content.endswith("\n"):
            content += "\n"
        content += "\n" + GIT_HOOK_TEMPLATE

        post_commit.write_text(content)
        result["action"] = "updated"
        result["message"] = "Added Context DNA hook to existing post-commit"

    else:
        # Create new post-commit
        post_commit.write_text(GIT_HOOK_TEMPLATE)
        result["action"] = "created"
        result["message"] = "Created new post-commit hook with Context DNA"

    # Make executable
    current_mode = post_commit.stat().st_mode
    post_commit.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    result["success"] = True
    result["path"] = str(post_commit)
    return result


def uninstall_git_hook(project_dir: Optional[str] = None) -> dict:
    """
    Remove Context DNA from post-commit hook.

    If the hook only contained Context DNA, removes the file.
    Otherwise, removes only the Context DNA section.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        Result dictionary with status and details
    """
    result = {
        "success": False,
        "action": None,
        "path": None,
        "message": "",
    }

    hooks_dir = find_git_hooks_dir(project_dir)
    if not hooks_dir:
        result["message"] = "Not a git repository"
        return result

    post_commit = hooks_dir / "post-commit"
    if not post_commit.exists():
        result["message"] = "post-commit hook not found"
        result["success"] = True
        result["action"] = "not_installed"
        return result

    content = post_commit.read_text()

    if HOOK_MARKER not in content:
        result["message"] = "Context DNA hook not found in post-commit"
        result["success"] = True
        result["action"] = "not_installed"
        return result

    # Remove Context DNA section
    lines = content.split("\n")
    new_lines = []
    skip = False

    for line in lines:
        if HOOK_MARKER in line:
            skip = True
            continue
        if skip and "# Context DNA Hook" in line:
            skip = False
            continue
        if not skip:
            new_lines.append(line)

    new_content = "\n".join(new_lines).strip()

    if new_content and new_content != "#!/bin/bash":
        # Keep the file with remaining content
        post_commit.write_text(new_content + "\n")
        result["message"] = "Removed Context DNA section from post-commit"
    else:
        # Remove the file if it's now empty
        post_commit.unlink()
        result["message"] = "Removed post-commit hook (was only Context DNA)"

    result["success"] = True
    result["action"] = "removed"
    result["path"] = str(post_commit)
    return result


def get_git_hook_status(project_dir: Optional[str] = None) -> dict:
    """
    Get detailed status of git hook installation.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        Status dictionary
    """
    project_dir = Path(project_dir or os.getcwd())
    hooks_dir = find_git_hooks_dir(project_dir)

    status = {
        "is_git_repo": hooks_dir is not None,
        "installed": False,
        "hooks_dir": str(hooks_dir) if hooks_dir else None,
        "post_commit_exists": False,
        "post_commit_path": None,
        "is_executable": False,
    }

    if hooks_dir:
        post_commit = hooks_dir / "post-commit"
        status["post_commit_exists"] = post_commit.exists()
        status["post_commit_path"] = str(post_commit)

        if post_commit.exists():
            content = post_commit.read_text()
            status["installed"] = HOOK_MARKER in content
            status["is_executable"] = os.access(post_commit, os.X_OK)

    return status


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Git Hook - Post-Commit Auto-Learning")
        print("")
        print("Commands:")
        print("  install    - Install Context DNA post-commit hook")
        print("  uninstall  - Remove Context DNA from post-commit hook")
        print("  status     - Check hook installation status")
        print("  show       - Show the hook template")
        print("")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "install":
        project_dir = sys.argv[2] if len(sys.argv) > 2 else None
        result = install_git_hook(project_dir)
        print(result["message"])
        if result["path"]:
            print(f"Path: {result['path']}")

    elif cmd == "uninstall":
        project_dir = sys.argv[2] if len(sys.argv) > 2 else None
        result = uninstall_git_hook(project_dir)
        print(result["message"])

    elif cmd == "status":
        project_dir = sys.argv[2] if len(sys.argv) > 2 else None
        status = get_git_hook_status(project_dir)
        print("=== Git Hook Status ===")
        print(f"Is git repository: {'Yes' if status['is_git_repo'] else 'No'}")
        if status["hooks_dir"]:
            print(f"Hooks directory: {status['hooks_dir']}")
        print(f"post-commit exists: {'Yes' if status['post_commit_exists'] else 'No'}")
        print(f"Hook installed: {'Yes' if status['installed'] else 'No'}")
        print(f"Is executable: {'Yes' if status['is_executable'] else 'No'}")

    elif cmd == "show":
        print(GIT_HOOK_TEMPLATE)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
