"""
Cursor IDE Adapter for Context DNA

Configures webhooks via .cursorrules file

Cursor uses .cursorrules for system-level instructions that are
prepended to every conversation.

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

import os
from pathlib import Path
from typing import Optional

from .base_adapter import IDEAdapter, VerificationResult, InstallResult


class CursorAdapter(IDEAdapter):
    """
    Cursor: Partial support via .cursorrules

    Cursor reads .cursorrules from the project root and includes
    its content as system-level context for all conversations.

    Unlike Claude Code, Cursor doesn't have a hook system, so
    injection is static (rules file) rather than dynamic (per-prompt).
    """

    CONTEXT_DNA_MARKER = "# === CONTEXT DNA INJECTION ==="
    CONTEXT_DNA_END_MARKER = "# === END CONTEXT DNA ==="

    @property
    def name(self) -> str:
        return "Cursor"

    @property
    def config_path(self) -> Path:
        """Return path to Cursor rules file."""
        return self.project_root / ".cursorrules"

    def is_installed(self) -> bool:
        """
        Check if Cursor is being used for this project.

        Detection strategy (strict to avoid false positives on CI):
        1. Project-level: .cursorrules or .cursor/ directory
        2. App-level: Cursor.app on macOS, cursor binary on Linux/Windows

        NOTE: We do NOT check for ~/.cursor alone because that directory
        might exist for other reasons and causes false positives on clean CI.
        """
        import shutil

        # Project-level indicators (most reliable)
        cursorrules = self.project_root / ".cursorrules"
        cursor_dir = self.project_root / ".cursor"

        if cursorrules.exists() or cursor_dir.exists():
            return True

        # macOS: Check for Cursor.app
        cursor_app_paths = [
            Path.home() / "Applications" / "Cursor.app",
            Path("/Applications/Cursor.app"),
        ]
        if any(p.exists() for p in cursor_app_paths):
            return True

        # Cross-platform: Check for 'cursor' command in PATH
        if shutil.which("cursor"):
            return True

        return False

    def _generate_injection_content(self) -> str:
        """Generate the Context DNA injection content for .cursorrules."""
        return f'''
{self.CONTEXT_DNA_MARKER}
# Context DNA is active for this project.
# For dynamic context injection, run the following before working:
#   python memory/auto_context.py "your task description"
#
# Or use the professor for guided context:
#   python memory/professor.py "what you're about to do"
#
# Key memory files:
# - memory/brain_state.md - Current system state
# - memory/query.py - Quick memory queries
# - memory/context.py - Full context retrieval
{self.CONTEXT_DNA_END_MARKER}
'''

    def install_hooks(self) -> InstallResult:
        """Install Cursor webhook hooks (via .cursorrules)."""
        config_path = self.config_path
        files_created = []
        files_modified = []

        try:
            injection_content = self._generate_injection_content()

            if config_path.exists():
                # Read existing content
                with open(config_path, 'r', encoding='utf-8') as f:
                    existing = f.read()

                # Check if we already have Context DNA section
                if self.CONTEXT_DNA_MARKER in existing:
                    # Update existing section
                    import re
                    pattern = f"{re.escape(self.CONTEXT_DNA_MARKER)}.*?{re.escape(self.CONTEXT_DNA_END_MARKER)}"
                    updated = re.sub(
                        pattern,
                        injection_content.strip(),
                        existing,
                        flags=re.DOTALL
                    )
                    if updated != existing:
                        with open(config_path, 'w', encoding='utf-8') as f:
                            f.write(updated)
                        files_modified.append(str(config_path))
                else:
                    # Append Context DNA section
                    with open(config_path, 'a', encoding='utf-8') as f:
                        f.write("\n" + injection_content)
                    files_modified.append(str(config_path))
            else:
                # Create new .cursorrules file
                with open(config_path, 'w', encoding='utf-8') as f:
                    f.write(f"# Cursor Rules for this project\n")
                    f.write(injection_content)
                files_created.append(str(config_path))

            return InstallResult(
                success=True,
                message=f"Cursor rules updated at {config_path}",
                files_created=files_created,
                files_modified=files_modified
            )

        except Exception as e:
            return InstallResult(
                success=False,
                message="Failed to install Cursor hooks",
                error=str(e)
            )

    def verify_hooks(self) -> VerificationResult:
        """Verify Cursor hooks are correctly installed."""
        config_path = self.config_path

        if not config_path.exists():
            return VerificationResult(
                success=False,
                message=".cursorrules file does not exist",
                details={"path": str(config_path)}
            )

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read()

            if self.CONTEXT_DNA_MARKER in content:
                return VerificationResult(
                    success=True,
                    message="Context DNA section found in .cursorrules",
                    details={
                        "path": str(config_path),
                        "file_size": len(content)
                    }
                )
            else:
                return VerificationResult(
                    success=False,
                    message="Context DNA section not found in .cursorrules",
                    details={
                        "path": str(config_path),
                        "file_size": len(content)
                    }
                )

        except Exception as e:
            return VerificationResult(
                success=False,
                message=f"Error reading .cursorrules: {str(e)[:50]}",
                details={"path": str(config_path)}
            )

    def get_injection_method(self) -> str:
        """Return how Cursor receives injections."""
        return "Static .cursorrules file (manual refresh required for dynamic context)"

    def get_rules_content(self) -> Optional[str]:
        """Get the current .cursorrules content."""
        config_path = self.config_path
        if not config_path.exists():
            return None

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return None
