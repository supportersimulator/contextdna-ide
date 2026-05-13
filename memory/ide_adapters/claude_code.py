"""
Claude Code IDE Adapter for Context DNA

Configures webhooks via .claude/settings.local.json

Claude Code supports UserPromptSubmit hooks which are ideal for
context injection.

IMPORTANT: Claude Code exists as BOTH a VS Code extension AND a CLI tool.
The VS Code extension is PREFERRED for new vibe coders - it's less intimidating
and provides a familiar IDE-integrated experience. Both methods are fully supported.

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .base_adapter import IDEAdapter, VerificationResult, InstallResult


class ClaudeCodeAdapter(IDEAdapter):
    """
    Claude Code: Full webhook support via settings.local.json

    Hook configuration location:
    - Global: ~/.claude/settings.local.json
    - Project: .claude/settings.local.json

    Hook event: UserPromptSubmit
    """

    @property
    def name(self) -> str:
        return "Claude Code"

    @property
    def config_path(self) -> Path:
        """Return path to Claude Code config."""
        # Prefer global config
        global_config = Path.home() / ".claude" / "settings.local.json"
        if global_config.exists():
            return global_config

        # Check project-level config
        project_config = self.project_root / ".claude" / "settings.local.json"
        if project_config.exists():
            return project_config

        # Default to global location
        return global_config

    def is_installed(self) -> bool:
        """
        Check if Claude Code is installed/configured.

        Detection methods (in order):
        1. .claude directory exists (user has already run Claude Code)
        2. 'claude' CLI command is in PATH (CLI installed)
        3. Claude Code VS Code extension is installed (PREFERRED for vibe coders)

        This multi-method detection solves the chicken-and-egg problem where
        new users may have Claude Code installed but haven't run it yet
        (so no .claude directory exists).
        """
        # Method 1: .claude directory exists (most reliable - user has used it)
        global_claude = Path.home() / ".claude"
        project_claude = self.project_root / ".claude"
        if global_claude.exists() or project_claude.exists():
            return True

        # Method 2: 'claude' CLI command in PATH
        if shutil.which("claude"):
            return True

        # Method 3: Check npm global packages for CLI
        try:
            result = subprocess.run(
                ["npm", "list", "-g", "@anthropic-ai/claude-code", "--json"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0 and "@anthropic-ai/claude-code" in result.stdout:
                return True
        except Exception as e:
            print(f"[WARN] npm claude-code detection failed: {e}")

        # Method 4: Check for VS Code extension (PREFERRED method for vibe coders)
        # Check extension directories for anthropic.claude-code
        vscode_ext_dirs = [
            Path.home() / ".vscode" / "extensions",
            Path.home() / ".vscode-insiders" / "extensions",
        ]
        for ext_dir in vscode_ext_dirs:
            if ext_dir.exists():
                try:
                    for item in ext_dir.iterdir():
                        if item.is_dir() and "anthropic.claude-code" in item.name.lower():
                            return True
                except Exception as e:
                    print(f"[WARN] VS Code extension dir scan failed: {e}")

        return False

    def _get_hook_command(self) -> str:
        """Get the hook command to execute."""
        script_path = self.get_hook_script_path()
        return f'bash {script_path} "$PROMPT"'

    def install_hooks(self) -> InstallResult:
        """Install Claude Code webhook hooks."""
        config_path = self.config_path
        files_created = []
        files_modified = []

        try:
            # Create .claude directory if needed
            config_path.parent.mkdir(parents=True, exist_ok=True)

            # Read existing config or create new
            if config_path.exists():
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                    files_modified.append(str(config_path))
                except json.JSONDecodeError:
                    config = {}
                    files_modified.append(str(config_path))
            else:
                config = {}
                files_created.append(str(config_path))

            # Ensure hooks section exists
            if "hooks" not in config:
                config["hooks"] = {}

            # Add UserPromptSubmit hook
            hook_command = self._get_hook_command()
            if "UserPromptSubmit" not in config["hooks"]:
                config["hooks"]["UserPromptSubmit"] = []

            # Check if our hook is already there
            existing_hooks = config["hooks"]["UserPromptSubmit"]
            if hook_command not in existing_hooks:
                # Add our hook
                config["hooks"]["UserPromptSubmit"].append(hook_command)

            # Write config
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)

            return InstallResult(
                success=True,
                message=f"Claude Code hooks installed at {config_path}",
                files_created=files_created,
                files_modified=files_modified
            )

        except Exception as e:
            return InstallResult(
                success=False,
                message=f"Failed to install Claude Code hooks",
                error=str(e)
            )

    def verify_hooks(self) -> VerificationResult:
        """Verify Claude Code hooks are correctly installed."""
        config_path = self.config_path

        if not config_path.exists():
            return VerificationResult(
                success=False,
                message="Config file does not exist",
                details={"path": str(config_path)}
            )

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # Check hooks section
            if "hooks" not in config:
                return VerificationResult(
                    success=False,
                    message="No hooks section in config",
                    details={"path": str(config_path)}
                )

            # Check UserPromptSubmit
            if "UserPromptSubmit" not in config["hooks"]:
                return VerificationResult(
                    success=False,
                    message="No UserPromptSubmit hooks configured",
                    details={"path": str(config_path)}
                )

            hooks = config["hooks"]["UserPromptSubmit"]
            if not hooks:
                return VerificationResult(
                    success=False,
                    message="UserPromptSubmit hooks list is empty",
                    details={"path": str(config_path)}
                )

            # Check if our hook script is referenced
            hook_script = str(self.get_hook_script_path())
            hook_found = any(hook_script in h or "auto-memory-query" in h for h in hooks)

            if hook_found:
                return VerificationResult(
                    success=True,
                    message="Context DNA hooks configured",
                    details={
                        "path": str(config_path),
                        "hooks_count": len(hooks)
                    }
                )
            else:
                return VerificationResult(
                    success=False,
                    message="Context DNA hook not found in UserPromptSubmit",
                    details={
                        "path": str(config_path),
                        "hooks": hooks
                    }
                )

        except json.JSONDecodeError as e:
            return VerificationResult(
                success=False,
                message=f"Invalid JSON in config: {str(e)[:50]}",
                details={"path": str(config_path)}
            )
        except Exception as e:
            return VerificationResult(
                success=False,
                message=f"Error verifying hooks: {str(e)[:50]}",
                details={"path": str(config_path)}
            )

    def get_injection_method(self) -> str:
        """Return how Claude Code receives injections."""
        return "UserPromptSubmit hook → stdout injection before prompt"

    def get_hook_config(self) -> Optional[Dict]:
        """Get the current hook configuration."""
        config_path = self.config_path
        if not config_path.exists():
            return None

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None
