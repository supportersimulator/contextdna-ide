"""
Context DNA IDE Hooks

This module provides IDE integration hooks that:
1. Inject MANDATORY memory protocol into IDE configuration files
2. Install git hooks for automatic learning
3. Configure context injection BEFORE user messages reach the AI

Supported IDEs:
- Claude Code: Injects protocol into CLAUDE.md
- Cursor: Creates .cursorrules with memory commands
- Git: Installs post-commit hook for auto-learning

Usage:
    from context_dna.hooks import install_claude_hook, install_cursor_hook, install_git_hook

    # Install all hooks
    install_claude_hook()
    install_cursor_hook()
    install_git_hook()

    # Or use the unified installer
    from context_dna.hooks.installer import HookInstaller
    installer = HookInstaller()
    installer.install_all()
"""

from context_dna.hooks.claude import install_claude_hook, uninstall_claude_hook
from context_dna.hooks.cursor import install_cursor_hook, uninstall_cursor_hook
from context_dna.hooks.git import install_git_hook, uninstall_git_hook
from context_dna.hooks.installer import HookInstaller

__all__ = [
    "install_claude_hook",
    "uninstall_claude_hook",
    "install_cursor_hook",
    "uninstall_cursor_hook",
    "install_git_hook",
    "uninstall_git_hook",
    "HookInstaller",
]
