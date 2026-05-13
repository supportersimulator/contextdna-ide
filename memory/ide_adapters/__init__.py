"""
IDE Adapters for Context DNA Webhook Installation

Provides IDE-specific hook configuration for:
- Claude Code (.claude/settings.local.json)
- Cursor (.cursorrules)
- VS Code (.vscode/settings.json + git hooks)
- Windsurf (Windsurf-specific config)
- Antigravity (web-based injection via API)

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

from .base_adapter import IDEAdapter, VerificationResult, InstallResult
from .claude_code import ClaudeCodeAdapter
from .cursor import CursorAdapter
from .vscode import VSCodeAdapter
from .antigravity import AntigravityAdapter

__all__ = [
    "IDEAdapter",
    "VerificationResult",
    "InstallResult",
    "ClaudeCodeAdapter",
    "CursorAdapter",
    "VSCodeAdapter",
    "AntigravityAdapter",
]
