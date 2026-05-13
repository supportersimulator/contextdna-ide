#!/usr/bin/env python3
"""
Bash History Architecture Hook

This module monitors bash history and automatically extracts architecture
from successful infrastructure commands.

HOW IT WORKS:
1. Monitors ~/.bash_history or ~/.zsh_history
2. Detects infrastructure commands (aws, docker, ssh, terraform, etc.)
3. Extracts architecture details
4. Records to Context DNA automatically

SETUP:
Add to your shell profile (~/.zshrc or ~/.bashrc):

    # Auto-architecture discovery
    export PROMPT_COMMAND='python3 /path/to/bash_hook.py capture 2>/dev/null'

Or for zsh, add to ~/.zshrc:

    precmd() { python3 /path/to/bash_hook.py capture 2>/dev/null }

This runs after every command, capturing architecture automatically.
"""

import os
import sys
import json
import hashlib
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

# State file to track what we've already processed
STATE_FILE = Path(__file__).parent / ".bash_hook_state.json"

# Find history file
HISTORY_FILES = [
    Path.home() / ".zsh_history",
    Path.home() / ".bash_history",
]


def get_history_file() -> Path:
    """Find the active history file."""
    for hf in HISTORY_FILES:
        if hf.exists():
            return hf
    return None


def load_state() -> dict:
    """Load processing state."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to load bash_hook state: {e}")
    return {"last_hash": None, "processed_count": 0}


def save_state(state: dict):
    """Save processing state."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def get_recent_commands(history_file: Path, count: int = 10) -> list[str]:
    """Get the most recent commands from history."""
    try:
        with open(history_file, "rb") as f:
            # Read last ~10KB of history
            f.seek(0, 2)  # End
            size = f.tell()
            f.seek(max(0, size - 10240))
            content = f.read().decode("utf-8", errors="ignore")

        lines = content.strip().split("\n")

        # Handle zsh history format (: timestamp:0;command)
        commands = []
        for line in lines[-count:]:
            if line.startswith(":"):
                # zsh format
                parts = line.split(";", 1)
                if len(parts) > 1:
                    commands.append(parts[1])
            else:
                commands.append(line)

        return commands
    except (OSError, UnicodeDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f"Failed to read bash history: {e}")
        return []


def capture():
    """Capture architecture from recent bash history."""
    history_file = get_history_file()
    if not history_file:
        return

    state = load_state()
    commands = get_recent_commands(history_file, count=5)

    if not commands:
        return

    # Hash recent commands to detect changes
    current_hash = hashlib.md5("\n".join(commands).encode()).hexdigest()

    if current_hash == state.get("last_hash"):
        # No new commands
        return

    # Process new commands
    try:
        from memory.auto_architecture import discover_from_output, _is_infra_command

        for cmd in commands:
            if _is_infra_command(cmd):
                # We don't have the output, but we can still extract from the command itself
                discover_from_output(cmd, "", success=True)

    except ImportError as e:
        print(f"[WARN] auto_capture import failed in bash_hook: {e}")

    # Update state
    state["last_hash"] = current_hash
    state["processed_count"] = state.get("processed_count", 0) + len(commands)
    save_state(state)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "capture":
        capture()
    else:
        print("Bash History Architecture Hook")
        print("")
        print("Add to ~/.zshrc or ~/.bashrc:")
        print(f"  precmd() {{ python3 {Path(__file__).absolute()} capture 2>/dev/null }}")
        print("")
        print("This will auto-capture architecture from infrastructure commands.")
