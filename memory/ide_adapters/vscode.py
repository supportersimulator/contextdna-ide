"""
VS Code IDE Adapter for Context DNA

Configures webhooks via:
- .vscode/settings.json (workspace settings)
- .git/hooks (git hooks for context refresh)

VS Code doesn't have native AI hooks, so we use git hooks to
trigger context refreshes on code changes.

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

import json
import os
from pathlib import Path
from typing import Optional, Dict

from .base_adapter import IDEAdapter, VerificationResult, InstallResult


class VSCodeAdapter(IDEAdapter):
    """
    VS Code: Limited support via git hooks + workspace settings

    VS Code doesn't have Claude/Cursor-style AI hooks, but we can:
    1. Configure workspace settings for better Context DNA integration
    2. Install git hooks to auto-refresh context on commits
    3. Add recommended extensions
    """

    @property
    def name(self) -> str:
        return "VS Code"

    @property
    def config_path(self) -> Path:
        """Return path to VS Code settings."""
        return self.project_root / ".vscode" / "settings.json"

    def is_installed(self) -> bool:
        """Check if VS Code is being used for this project."""
        # VS Code is "installed" if:
        # 1. .vscode directory exists, OR
        # 2. VS Code app is installed
        vscode_dir = self.project_root / ".vscode"

        vscode_app_paths = [
            Path.home() / "Applications" / "Visual Studio Code.app",
            Path("/Applications/Visual Studio Code.app"),
            Path.home() / ".vscode",  # VS Code config directory
        ]

        return vscode_dir.exists() or any(p.exists() for p in vscode_app_paths)

    def _get_vscode_settings(self) -> Dict:
        """Get recommended VS Code settings for Context DNA."""
        return {
            "contextDna.enabled": True,
            "contextDna.memoryPath": str(self.memory_dir),
            "contextDna.scriptsPath": str(self.scripts_dir),
            "files.associations": {
                "*.md": "markdown",
                "brain_state.md": "markdown"
            },
            "editor.formatOnSave": True,
            "python.analysis.extraPaths": [
                str(self.memory_dir),
                str(self.project_root)
            ]
        }

    def _get_git_hook_content(self) -> str:
        """Get the git post-commit hook content."""
        script_path = self.get_hook_script_path()
        return f'''#!/bin/bash
# Context DNA post-commit hook
# Auto-refreshes context after commits

# Only run if the script exists
if [ -f "{script_path}" ]; then
    # Run in background to not block git
    bash "{script_path}" "post-commit context refresh" > /dev/null 2>&1 &
fi
'''

    def install_hooks(self) -> InstallResult:
        """Install VS Code and git hooks."""
        files_created = []
        files_modified = []
        errors = []

        # 1. Create/update .vscode/settings.json
        try:
            vscode_dir = self.project_root / ".vscode"
            vscode_dir.mkdir(parents=True, exist_ok=True)

            settings_path = self.config_path
            settings = self._get_vscode_settings()

            if settings_path.exists():
                try:
                    with open(settings_path, 'r', encoding='utf-8') as f:
                        existing = json.load(f)
                    # Merge settings
                    existing.update(settings)
                    settings = existing
                    files_modified.append(str(settings_path))
                except json.JSONDecodeError:
                    files_modified.append(str(settings_path))
            else:
                files_created.append(str(settings_path))

            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2)

        except Exception as e:
            errors.append(f"VS Code settings: {str(e)}")

        # 2. Install git post-commit hook
        try:
            git_hooks_dir = self.project_root / ".git" / "hooks"
            if git_hooks_dir.exists():
                post_commit_path = git_hooks_dir / "post-commit"
                hook_content = self._get_git_hook_content()

                if post_commit_path.exists():
                    with open(post_commit_path, 'r', encoding='utf-8') as f:
                        existing = f.read()

                    if "Context DNA" not in existing:
                        # Append our hook
                        with open(post_commit_path, 'a', encoding='utf-8') as f:
                            f.write("\n" + hook_content)
                        files_modified.append(str(post_commit_path))
                else:
                    with open(post_commit_path, 'w', encoding='utf-8') as f:
                        f.write(hook_content)
                    os.chmod(post_commit_path, 0o755)
                    files_created.append(str(post_commit_path))

        except Exception as e:
            errors.append(f"Git hook: {str(e)}")

        # Determine overall success
        success = len(errors) == 0

        return InstallResult(
            success=success,
            message="VS Code integration configured" if success else "Partial VS Code setup",
            files_created=files_created,
            files_modified=files_modified,
            error="; ".join(errors) if errors else None
        )

    def verify_hooks(self) -> VerificationResult:
        """Verify VS Code hooks are correctly installed."""
        issues = []
        details = {}

        # Check .vscode/settings.json
        settings_path = self.config_path
        if settings_path.exists():
            try:
                with open(settings_path, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                if settings.get("contextDna.enabled"):
                    details["vscode_settings"] = "configured"
                else:
                    issues.append("contextDna.enabled not set in settings")
            except Exception:
                issues.append("Invalid settings.json")
        else:
            issues.append(".vscode/settings.json not found")

        # Check git hook
        git_hook_path = self.project_root / ".git" / "hooks" / "post-commit"
        if git_hook_path.exists():
            try:
                with open(git_hook_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                if "Context DNA" in content:
                    details["git_hook"] = "configured"
                else:
                    issues.append("Git hook missing Context DNA section")
            except Exception:
                issues.append("Cannot read git hook")
        else:
            issues.append("Git post-commit hook not found")

        if not issues:
            return VerificationResult(
                success=True,
                message="VS Code integration verified",
                details=details
            )
        else:
            return VerificationResult(
                success=False,
                message=f"Issues: {', '.join(issues[:2])}",
                details={**details, "issues": issues}
            )

    def get_injection_method(self) -> str:
        """Return how VS Code receives injections."""
        return "Git hooks + manual context queries (no native AI hooks)"

    def get_settings(self) -> Optional[Dict]:
        """Get the current VS Code settings."""
        settings_path = self.config_path
        if not settings_path.exists():
            return None

        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None
