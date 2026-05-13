#!/usr/bin/env python3
"""
Cursor Hook - .cursorrules Protocol Injection

This module creates/updates .cursorrules with Context DNA integration.

The .cursorrules file tells Cursor IDE to:
1. Consult memory before starting tasks
2. Search for gotchas before risky operations
3. Record wins and fixes after completing work

Usage:
    from context_dna.hooks.cursor import install_cursor_hook

    # Install in current project
    install_cursor_hook()

    # Install in specific directory
    install_cursor_hook("/path/to/project")
"""

import os
from pathlib import Path
from datetime import datetime
from typing import Optional


# =============================================================================
# .cursorrules TEMPLATE
# =============================================================================

CURSOR_RULES_TEMPLATE = '''# Context DNA Integration

## Before Starting ANY Task

MANDATORY: Run this before every task:
```bash
context-dna consult "<task description>"
```

This gives you:
- Relevant past learnings
- Known gotchas to avoid
- Patterns that worked before

## During Work

Search for specific gotchas:
```bash
context-dna query "docker" --type fix
context-dna query "async python" --type pattern
```

## After Success

When user confirms success ("that worked", "perfect", etc.):
```bash
context-dna win "What worked" "How it worked - key insight"
```

## After Fixing Errors

Record fixes for future reference:
```bash
context-dna fix "What went wrong" "How you fixed it"
```

## Task Checklist

Before writing ANY code:
- [ ] Ran `context-dna consult "<task>"`
- [ ] Read related existing code
- [ ] Checked for similar patterns
- [ ] Identified root cause (not symptom)

After completing work:
- [ ] User confirmed success
- [ ] Recorded win with `context-dna win`
- [ ] (If fixed error) Recorded fix with `context-dna fix`

---
Context DNA Protocol v1.0
'''

# Marker to identify Context DNA section
HOOK_START_MARKER = "# Context DNA Integration"
HOOK_END_MARKER = "Context DNA Protocol v1.0"


# =============================================================================
# INSTALLATION FUNCTIONS
# =============================================================================

def find_cursorrules(project_dir: Optional[str] = None) -> Optional[Path]:
    """
    Find .cursorrules in the project.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        Path to .cursorrules if found, None otherwise
    """
    project_dir = Path(project_dir or os.getcwd())
    cursorrules = project_dir / ".cursorrules"

    if cursorrules.exists():
        return cursorrules

    return None


def check_cursor_hook(project_dir: Optional[str] = None) -> bool:
    """
    Check if Context DNA hook is installed in .cursorrules.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        True if hook is installed
    """
    cursorrules = find_cursorrules(project_dir)
    if not cursorrules:
        return False

    content = cursorrules.read_text()
    return HOOK_START_MARKER in content


def install_cursor_hook(project_dir: Optional[str] = None, force: bool = False) -> dict:
    """
    Install Context DNA protocol into .cursorrules.

    If .cursorrules doesn't exist, creates it.
    If it exists, appends the protocol section.

    Args:
        project_dir: Project directory (defaults to cwd)
        force: If True, reinstall even if already present

    Returns:
        Result dictionary with status and details
    """
    project_dir = Path(project_dir or os.getcwd())
    result = {
        "success": False,
        "action": None,
        "path": None,
        "message": "",
    }

    # Check if already installed
    if check_cursor_hook(project_dir) and not force:
        result["message"] = "Context DNA hook already installed in .cursorrules"
        result["success"] = True
        result["action"] = "already_installed"
        return result

    cursorrules = project_dir / ".cursorrules"

    if cursorrules.exists():
        # Append to existing .cursorrules
        content = cursorrules.read_text()

        # Remove old hook if present (for reinstall)
        if HOOK_START_MARKER in content:
            start_idx = content.find(HOOK_START_MARKER)
            end_idx = content.find(HOOK_END_MARKER)
            if end_idx != -1:
                end_idx = content.find("\n", end_idx) + 1
                content = content[:start_idx] + content[end_idx:]

        # Append new hook
        if not content.endswith("\n"):
            content += "\n"
        content += "\n" + CURSOR_RULES_TEMPLATE

        cursorrules.write_text(content)
        result["action"] = "updated"
        result["message"] = "Added Context DNA protocol to existing .cursorrules"

    else:
        # Create new .cursorrules
        cursorrules.write_text(CURSOR_RULES_TEMPLATE)
        result["action"] = "created"
        result["message"] = "Created new .cursorrules with Context DNA protocol"

    result["success"] = True
    result["path"] = str(cursorrules)
    return result


def uninstall_cursor_hook(project_dir: Optional[str] = None) -> dict:
    """
    Remove Context DNA protocol from .cursorrules.

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

    cursorrules = find_cursorrules(project_dir)
    if not cursorrules:
        result["message"] = ".cursorrules not found"
        return result

    content = cursorrules.read_text()

    if HOOK_START_MARKER not in content:
        result["message"] = "Context DNA hook not found in .cursorrules"
        result["success"] = True
        result["action"] = "not_installed"
        return result

    # Find and remove hook section
    start_idx = content.find(HOOK_START_MARKER)
    end_idx = content.find(HOOK_END_MARKER)

    if end_idx != -1:
        end_idx = content.find("\n", end_idx)
        if end_idx == -1:
            end_idx = len(content)
        else:
            end_idx += 1

        content = content[:start_idx] + content[end_idx:]

        # Clean up extra newlines
        while "\n\n\n" in content:
            content = content.replace("\n\n\n", "\n\n")

        content = content.strip()

        if content:
            cursorrules.write_text(content + "\n")
        else:
            # If file is now empty, delete it
            cursorrules.unlink()

        result["success"] = True
        result["action"] = "removed"
        result["path"] = str(cursorrules)
        result["message"] = "Removed Context DNA protocol from .cursorrules"
    else:
        result["message"] = "Could not find end of hook section"

    return result


def get_cursor_hook_status(project_dir: Optional[str] = None) -> dict:
    """
    Get detailed status of Cursor hook installation.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        Status dictionary
    """
    project_dir = Path(project_dir or os.getcwd())
    cursorrules = find_cursorrules(project_dir)

    status = {
        "installed": False,
        "cursorrules_exists": cursorrules is not None,
        "cursorrules_path": str(cursorrules) if cursorrules else None,
        "protocol_version": None,
    }

    if cursorrules and cursorrules.exists():
        content = cursorrules.read_text()
        status["installed"] = HOOK_START_MARKER in content

        # Try to extract version
        if "Protocol v" in content:
            import re
            match = re.search(r"Protocol v(\d+\.\d+)", content)
            if match:
                status["protocol_version"] = match.group(1)

    return status


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Cursor Hook - .cursorrules Protocol Injection")
        print("")
        print("Commands:")
        print("  install    - Install Context DNA protocol into .cursorrules")
        print("  uninstall  - Remove Context DNA protocol from .cursorrules")
        print("  status     - Check hook installation status")
        print("  show       - Show the protocol template")
        print("")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "install":
        project_dir = sys.argv[2] if len(sys.argv) > 2 else None
        result = install_cursor_hook(project_dir)
        print(result["message"])
        if result["path"]:
            print(f"Path: {result['path']}")

    elif cmd == "uninstall":
        project_dir = sys.argv[2] if len(sys.argv) > 2 else None
        result = uninstall_cursor_hook(project_dir)
        print(result["message"])

    elif cmd == "status":
        project_dir = sys.argv[2] if len(sys.argv) > 2 else None
        status = get_cursor_hook_status(project_dir)
        print("=== Cursor Hook Status ===")
        print(f".cursorrules exists: {'Yes' if status['cursorrules_exists'] else 'No'}")
        if status["cursorrules_path"]:
            print(f"Path: {status['cursorrules_path']}")
        print(f"Hook installed: {'Yes' if status['installed'] else 'No'}")
        if status["protocol_version"]:
            print(f"Protocol version: {status['protocol_version']}")

    elif cmd == "show":
        print(CURSOR_RULES_TEMPLATE)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
