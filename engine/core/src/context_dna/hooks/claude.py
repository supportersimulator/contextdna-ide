#!/usr/bin/env python3
"""
Claude Code Hook - CLAUDE.md Protocol Injection

This module injects the MANDATORY Context DNA protocol into CLAUDE.md files.

The protocol ensures that:
1. The AI consults memory BEFORE every task
2. Context injection happens BEFORE user messages reach the AI
3. Success capture is triggered automatically

The injected protocol includes:
- MANDATORY memory consultation requirement
- Task checklist with memory verification
- Success capture commands
- Gotcha search commands

Usage:
    from context_dna.hooks.claude import install_claude_hook

    # Install in current project
    install_claude_hook()

    # Install in specific directory
    install_claude_hook("/path/to/project")

    # Check if hook is installed
    is_installed = check_claude_hook()
"""

import os
from pathlib import Path
from datetime import datetime
from typing import Optional


# =============================================================================
# CLAUDE.md PROTOCOL TEMPLATE
# =============================================================================

CLAUDE_HOOK_TEMPLATE = '''
## Context DNA Protocol (MANDATORY)

**BEFORE doing ANYTHING on a task:**

0. **CONSULT MEMORY FIRST** - MANDATORY
   ```bash
   context-dna consult "what you're about to do"
   ```
   - The 10 seconds of querying saves hours of repeating mistakes
   - This is NOT optional - skipping this is a protocol violation

1. **Read existing code FIRST**
   - Check if function already exists
   - Check if similar pattern exists
   - Check what naming conventions are used

2. **Understand complete flow BEFORE fixing**
   - What triggers this?
   - What calls what?
   - What's the data flow?

3. **Fix root cause, not symptoms**
   - If something doesn't work -> why doesn't it work?
   - Don't patch, understand

**Task Checklist (Use on EVERY task):**
```
Before writing ANY code:
[ ] QUERIED MEMORY: context-dna consult "<task>"
[ ] Read related existing code
[ ] Grep for similar patterns
[ ] Check if function/feature already exists
[ ] Understand complete data flow
[ ] Identify root cause (not symptom)

Only THEN:
[ ] Write minimal fix
[ ] Test against actual behavior
```

**After SUCCESS (user confirms "that worked", "perfect", etc.):**
```bash
context-dna win "What worked" "How it worked - key insight"
```

**After FIXING an error:**
```bash
context-dna fix "What went wrong" "How you fixed it"
```

**Search for gotchas before risky operations:**
```bash
context-dna query "docker deploy" --type fix
```

---
*Context DNA Protocol v1.0 - Auto-injected by context-dna hooks install claude*
'''

# Marker to identify Context DNA section
HOOK_START_MARKER = "## Context DNA Protocol (MANDATORY)"
HOOK_END_MARKER = "*Context DNA Protocol v1.0"


# =============================================================================
# INSTALLATION FUNCTIONS
# =============================================================================

def find_claude_md(project_dir: Optional[str] = None) -> Optional[Path]:
    """
    Find CLAUDE.md in the project.

    Looks in:
    1. Project root
    2. .claude/ directory
    3. docs/ directory

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        Path to CLAUDE.md if found, None otherwise
    """
    project_dir = Path(project_dir or os.getcwd())

    # Check common locations
    locations = [
        project_dir / "CLAUDE.md",
        project_dir / ".claude" / "CLAUDE.md",
        project_dir / "docs" / "CLAUDE.md",
    ]

    for path in locations:
        if path.exists():
            return path

    return None


def check_claude_hook(project_dir: Optional[str] = None) -> bool:
    """
    Check if Context DNA hook is installed in CLAUDE.md.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        True if hook is installed
    """
    claude_md = find_claude_md(project_dir)
    if not claude_md:
        return False

    content = claude_md.read_text()
    return HOOK_START_MARKER in content


def install_claude_hook(project_dir: Optional[str] = None, force: bool = False) -> dict:
    """
    Install Context DNA protocol into CLAUDE.md.

    If CLAUDE.md doesn't exist, creates it.
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
    if check_claude_hook(project_dir) and not force:
        result["message"] = "Context DNA hook already installed in CLAUDE.md"
        result["success"] = True
        result["action"] = "already_installed"
        return result

    # Find or create CLAUDE.md
    claude_md = find_claude_md(project_dir)

    if claude_md is None:
        # Create new CLAUDE.md in project root
        claude_md = project_dir / "CLAUDE.md"
        content = f"""# Project Instructions for Claude

This file provides guidance for Claude Code when working in this repository.

{CLAUDE_HOOK_TEMPLATE}

---

## Project-Specific Instructions

Add your project-specific instructions below...

"""
        claude_md.write_text(content)
        result["action"] = "created"
        result["message"] = f"Created new CLAUDE.md with Context DNA protocol"

    else:
        # Append to existing CLAUDE.md
        content = claude_md.read_text()

        # Remove old hook if present (for reinstall)
        if HOOK_START_MARKER in content:
            # Find and remove existing hook section
            start_idx = content.find(HOOK_START_MARKER)
            end_idx = content.find(HOOK_END_MARKER)
            if end_idx != -1:
                end_idx = content.find("\n", end_idx) + 1
                content = content[:start_idx] + content[end_idx:]

        # Append new hook
        if not content.endswith("\n"):
            content += "\n"
        content += CLAUDE_HOOK_TEMPLATE

        claude_md.write_text(content)
        result["action"] = "updated"
        result["message"] = f"Added Context DNA protocol to existing CLAUDE.md"

    result["success"] = True
    result["path"] = str(claude_md)
    return result


def uninstall_claude_hook(project_dir: Optional[str] = None) -> dict:
    """
    Remove Context DNA protocol from CLAUDE.md.

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

    claude_md = find_claude_md(project_dir)
    if not claude_md:
        result["message"] = "CLAUDE.md not found"
        return result

    content = claude_md.read_text()

    if HOOK_START_MARKER not in content:
        result["message"] = "Context DNA hook not found in CLAUDE.md"
        result["success"] = True
        result["action"] = "not_installed"
        return result

    # Find and remove hook section
    start_idx = content.find(HOOK_START_MARKER)
    end_idx = content.find(HOOK_END_MARKER)

    if end_idx != -1:
        # Include the rest of the line after the marker
        end_idx = content.find("\n", end_idx)
        if end_idx == -1:
            end_idx = len(content)
        else:
            end_idx += 1

        content = content[:start_idx] + content[end_idx:]

        # Clean up extra newlines
        while "\n\n\n" in content:
            content = content.replace("\n\n\n", "\n\n")

        claude_md.write_text(content)

        result["success"] = True
        result["action"] = "removed"
        result["path"] = str(claude_md)
        result["message"] = "Removed Context DNA protocol from CLAUDE.md"
    else:
        result["message"] = "Could not find end of hook section"

    return result


def get_claude_hook_status(project_dir: Optional[str] = None) -> dict:
    """
    Get detailed status of Claude hook installation.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        Status dictionary
    """
    project_dir = Path(project_dir or os.getcwd())
    claude_md = find_claude_md(project_dir)

    status = {
        "installed": False,
        "claude_md_exists": claude_md is not None,
        "claude_md_path": str(claude_md) if claude_md else None,
        "protocol_version": None,
    }

    if claude_md and claude_md.exists():
        content = claude_md.read_text()
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
        print("Claude Code Hook - CLAUDE.md Protocol Injection")
        print("")
        print("Commands:")
        print("  install    - Install Context DNA protocol into CLAUDE.md")
        print("  uninstall  - Remove Context DNA protocol from CLAUDE.md")
        print("  status     - Check hook installation status")
        print("  show       - Show the protocol template")
        print("")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "install":
        project_dir = sys.argv[2] if len(sys.argv) > 2 else None
        result = install_claude_hook(project_dir)
        print(result["message"])
        if result["path"]:
            print(f"Path: {result['path']}")

    elif cmd == "uninstall":
        project_dir = sys.argv[2] if len(sys.argv) > 2 else None
        result = uninstall_claude_hook(project_dir)
        print(result["message"])

    elif cmd == "status":
        project_dir = sys.argv[2] if len(sys.argv) > 2 else None
        status = get_claude_hook_status(project_dir)
        print("=== Claude Hook Status ===")
        print(f"CLAUDE.md exists: {'Yes' if status['claude_md_exists'] else 'No'}")
        if status["claude_md_path"]:
            print(f"Path: {status['claude_md_path']}")
        print(f"Hook installed: {'Yes' if status['installed'] else 'No'}")
        if status["protocol_version"]:
            print(f"Protocol version: {status['protocol_version']}")

    elif cmd == "show":
        print(CLAUDE_HOOK_TEMPLATE)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
