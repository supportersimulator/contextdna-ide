#!/usr/bin/env python3
"""
Setup Checker - Intelligent Configuration Validation for Context DNA

This module provides comprehensive setup validation:
1. Checks all required configurations
2. Detects missing or incomplete setup
3. Generates fix commands for each issue
4. Provides click-to-configure URLs for dashboard

ADAPTIVE SETUP:
The checker adapts to each user's project:
- Detects which LLM providers they might want
- Suggests appropriate Docker configs
- Checks for IDE-specific hooks
- Validates storage backends

Usage:
    from context_dna.setup import check_all, get_missing_configs

    # Full status check
    status = check_all()
    print(f"Setup complete: {status.is_complete}")
    print(f"Issues: {status.issues}")

    # Get fix commands
    for config in get_missing_configs():
        print(f"Missing: {config['name']}")
        print(f"Fix: {config['fix_command']}")
"""

import os
import json
import shutil
import subprocess
import platform
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum


# =============================================================================
# SETUP STATUS
# =============================================================================

class ConfigStatus(Enum):
    """Status of a configuration item."""
    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass
class ConfigItem:
    """A single configuration item to check."""
    name: str
    description: str
    status: ConfigStatus
    value: Optional[str] = None
    fix_command: Optional[str] = None
    fix_url: Optional[str] = None
    fix_description: Optional[str] = None
    priority: int = 1  # 1=critical, 2=recommended, 3=optional


@dataclass
class SetupStatus:
    """Overall setup status."""
    is_complete: bool
    completion_percent: int
    issues: List[ConfigItem]
    warnings: List[ConfigItem]
    ok_items: List[ConfigItem]
    next_step: Optional[str] = None
    dashboard_url: str = "http://localhost:3456"

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_complete": self.is_complete,
            "completion_percent": self.completion_percent,
            "issues_count": len(self.issues),
            "warnings_count": len(self.warnings),
            "issues": [
                {
                    "name": i.name,
                    "description": i.description,
                    "fix_command": i.fix_command,
                    "fix_url": i.fix_url,
                    "priority": i.priority,
                }
                for i in self.issues
            ],
            "warnings": [
                {
                    "name": w.name,
                    "description": w.description,
                    "fix_command": w.fix_command,
                }
                for w in self.warnings
            ],
            "next_step": self.next_step,
            "dashboard_url": self.dashboard_url,
        }


# =============================================================================
# SETUP CHECKER CLASS
# =============================================================================

class SetupChecker:
    """
    Comprehensive setup validator for Context DNA.

    Checks:
    - Project initialization (context-dna.toml)
    - Storage backends (SQLite, PostgreSQL)
    - LLM providers (Ollama, OpenAI, Anthropic)
    - Docker services (if using pgvector)
    - IDE hooks (Claude, Cursor, Git)
    - xbar/menu bar plugins
    """

    def __init__(self, project_dir: str = None):
        """
        Initialize checker.

        Args:
            project_dir: Project directory (defaults to cwd)
        """
        self.project_dir = Path(project_dir or os.getcwd())
        self.config_dir = self.project_dir / ".context-dna"
        self.home_config = Path.home() / ".context-dna"

    def check_all(self) -> SetupStatus:
        """
        Run all configuration checks.

        Returns:
            Complete setup status with issues and recommendations
        """
        all_items = []

        # Core checks
        all_items.append(self.check_initialization())
        all_items.append(self.check_storage())
        all_items.append(self.check_llm_provider())
        all_items.append(self.check_lite_mode_api_requirement())  # Safeguard: ensure API if no local LLM

        # Optional but recommended
        all_items.append(self.check_docker())
        all_items.append(self.check_ide_hooks())
        all_items.append(self.check_xbar())

        # Environment
        all_items.extend(self.check_environment_variables())

        # Categorize
        issues = [i for i in all_items if i.status in (ConfigStatus.MISSING, ConfigStatus.INVALID) and i.priority == 1]
        warnings = [i for i in all_items if i.status in (ConfigStatus.MISSING, ConfigStatus.INVALID, ConfigStatus.PARTIAL) and i.priority > 1]
        ok_items = [i for i in all_items if i.status == ConfigStatus.OK]

        # Calculate completion
        total = len(all_items)
        complete = len(ok_items)
        completion_percent = int((complete / total) * 100) if total > 0 else 0

        # Determine next step
        next_step = None
        if issues:
            next_step = f"Fix critical issue: {issues[0].name} - {issues[0].fix_command or issues[0].fix_description}"
        elif warnings:
            next_step = f"Recommended: {warnings[0].name} - {warnings[0].fix_description}"

        return SetupStatus(
            is_complete=len(issues) == 0,
            completion_percent=completion_percent,
            issues=issues,
            warnings=warnings,
            ok_items=ok_items,
            next_step=next_step,
        )

    def check_initialization(self) -> ConfigItem:
        """Check if Context DNA is initialized in this project."""
        config_file = self.config_dir / "config.json"
        toml_file = self.project_dir / "context-dna.toml"

        if config_file.exists() or toml_file.exists():
            return ConfigItem(
                name="Project Initialization",
                description="Context DNA is initialized",
                status=ConfigStatus.OK,
                value=str(config_file if config_file.exists() else toml_file),
                priority=1,
            )

        return ConfigItem(
            name="Project Initialization",
            description="Context DNA not initialized in this project",
            status=ConfigStatus.MISSING,
            fix_command="context-dna init",
            fix_url="/setup/init",
            fix_description="Initialize Context DNA in this project",
            priority=1,
        )

    def check_storage(self) -> ConfigItem:
        """Check storage backend configuration."""
        # Check for SQLite (default)
        sqlite_path = self.config_dir / "memory.db"
        if sqlite_path.exists():
            return ConfigItem(
                name="Storage Backend",
                description="SQLite storage configured",
                status=ConfigStatus.OK,
                value=str(sqlite_path),
                priority=1,
            )

        # Check for PostgreSQL
        pg_url = os.getenv("CONTEXT_DNA_POSTGRES_URL")
        if pg_url:
            return ConfigItem(
                name="Storage Backend",
                description="PostgreSQL configured",
                status=ConfigStatus.OK,
                value="PostgreSQL (env)",
                priority=1,
            )

        # No storage - will be created on first use
        return ConfigItem(
            name="Storage Backend",
            description="No storage configured (will use SQLite on first use)",
            status=ConfigStatus.PARTIAL,
            fix_command="context-dna init",
            fix_description="Storage will be created automatically",
            priority=2,
        )

    def check_llm_provider(self) -> ConfigItem:
        """Check LLM provider configuration."""
        # Check for local Ollama
        ollama_running = self._check_ollama()
        if ollama_running:
            return ConfigItem(
                name="LLM Provider",
                description="Ollama running locally (free!)",
                status=ConfigStatus.OK,
                value="ollama",
                priority=1,
            )

        # Check for cloud providers
        openai_key = os.getenv("Context_DNA_OPENAI")
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")

        if openai_key:
            return ConfigItem(
                name="LLM Provider",
                description="OpenAI API configured",
                status=ConfigStatus.OK,
                value="openai",
                priority=1,
            )

        if anthropic_key:
            return ConfigItem(
                name="LLM Provider",
                description="Anthropic API configured",
                status=ConfigStatus.OK,
                value="anthropic",
                priority=1,
            )

        # No provider - need at least one
        return ConfigItem(
            name="LLM Provider",
            description="No LLM provider configured",
            status=ConfigStatus.MISSING,
            fix_command="context-dna setup --provider ollama",
            fix_url="/setup/llm",
            fix_description="Install Ollama for free local AI, or add Context_DNA_OPENAI to .env",
            priority=1,
        )

    def check_lite_mode_api_requirement(self) -> ConfigItem:
        """
        SAFEGUARD: Ensure external API is configured if local LLM unavailable.

        When running in lite mode (no Docker/local LLM), the system MUST have
        an external API (OpenAI/Anthropic) configured. This prevents scenarios
        where Synaptic's voice becomes unavailable.

        Returns:
            ConfigItem with OK if at least one provider available, MISSING otherwise
        """
        # Check local LLM (Ollama)
        ollama_running = self._check_ollama()

        # Check cloud APIs
        openai_key = os.getenv("Context_DNA_OPENAI")
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")

        has_external_api = bool(openai_key or anthropic_key)

        if ollama_running and has_external_api:
            return ConfigItem(
                name="LLM Redundancy",
                description="Local LLM + Cloud API configured (fully redundant)",
                status=ConfigStatus.OK,
                value="ollama + cloud",
                priority=1,
            )

        if ollama_running:
            return ConfigItem(
                name="LLM Redundancy",
                description="Local LLM only - add Context_DNA_OPENAI for redundancy",
                status=ConfigStatus.PARTIAL,
                fix_command='export Context_DNA_OPENAI="sk-..."',
                fix_description="Add cloud API as fallback when local LLM unavailable",
                priority=2,
            )

        if has_external_api:
            return ConfigItem(
                name="LLM Redundancy",
                description="Cloud API configured (lite mode ready)",
                status=ConfigStatus.OK,
                value="cloud api",
                priority=1,
            )

        # CRITICAL: No LLM available at all
        return ConfigItem(
            name="LLM Redundancy",
            description="⚠️ NO LLM PROVIDER CONFIGURED - Synaptic voice will be unavailable!",
            status=ConfigStatus.MISSING,
            fix_command='export Context_DNA_OPENAI="sk-..." # or export ANTHROPIC_API_KEY="sk-ant-..."',
            fix_url="/setup/llm",
            fix_description="REQUIRED: Configure at least one LLM provider (local Ollama or cloud API)",
            priority=1,
        )

    def check_docker(self) -> ConfigItem:
        """Check Docker availability (needed for pgvector)."""
        docker_available = shutil.which("docker") is not None

        if docker_available:
            # Check if Docker daemon is running
            try:
                result = subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    return ConfigItem(
                        name="Docker",
                        description="Docker running (enables pgvector semantic search)",
                        status=ConfigStatus.OK,
                        value="running",
                        priority=2,
                    )
            except Exception as e:
                print(f"[WARN] Failed to check Docker info: {e}")

            return ConfigItem(
                name="Docker",
                description="Docker installed but not running",
                status=ConfigStatus.PARTIAL,
                fix_command="open -a Docker" if platform.system() == "Darwin" else "systemctl start docker",
                fix_description="Start Docker Desktop for advanced features",
                priority=2,
            )

        return ConfigItem(
            name="Docker",
            description="Docker not installed (optional for advanced features)",
            status=ConfigStatus.MISSING,
            fix_url="https://docker.com/get-started",
            fix_description="Install Docker Desktop for pgvector semantic search",
            priority=3,
        )

    def check_ide_hooks(self) -> ConfigItem:
        """Check IDE hook installation."""
        hooks_installed = []

        # Check Claude Code hook
        claude_md = self.project_dir / "CLAUDE.md"
        if claude_md.exists():
            content = claude_md.read_text()
            if "Context DNA Protocol" in content or "context-dna" in content.lower():
                hooks_installed.append("claude")

        # Check Cursor hook
        cursorrules = self.project_dir / ".cursorrules"
        if cursorrules.exists():
            content = cursorrules.read_text()
            if "Context DNA" in content or "context-dna" in content.lower():
                hooks_installed.append("cursor")

        # Check Git hook
        git_hook = self.project_dir / ".git" / "hooks" / "post-commit"
        if git_hook.exists():
            content = git_hook.read_text()
            if "context-dna" in content.lower():
                hooks_installed.append("git")

        if len(hooks_installed) >= 2:
            return ConfigItem(
                name="IDE Hooks",
                description=f"Installed: {', '.join(hooks_installed)}",
                status=ConfigStatus.OK,
                value=",".join(hooks_installed),
                priority=2,
            )
        elif hooks_installed:
            return ConfigItem(
                name="IDE Hooks",
                description=f"Partial: {', '.join(hooks_installed)}",
                status=ConfigStatus.PARTIAL,
                fix_command="context-dna hooks install",
                fix_url="/setup/hooks",
                fix_description="Install additional IDE hooks for automatic learning",
                priority=2,
            )

        return ConfigItem(
            name="IDE Hooks",
            description="No IDE hooks installed",
            status=ConfigStatus.MISSING,
            fix_command="context-dna hooks install",
            fix_url="/setup/hooks",
            fix_description="Install IDE hooks for automatic context injection",
            priority=2,
        )

    def check_xbar(self) -> ConfigItem:
        """Check xbar/menu bar plugin installation."""
        # macOS xbar
        xbar_dir = Path.home() / "Library" / "Application Support" / "xbar" / "plugins"
        if xbar_dir.exists():
            plugins = list(xbar_dir.glob("context-dna*"))
            if plugins:
                return ConfigItem(
                    name="Menu Bar Plugin",
                    description="xbar plugin installed",
                    status=ConfigStatus.OK,
                    value=str(plugins[0]),
                    priority=3,
                )

        # Linux (if applicable)
        if platform.system() == "Linux":
            # Check for common status bar plugin locations
            return ConfigItem(
                name="Menu Bar Plugin",
                description="Status bar plugin not installed (optional)",
                status=ConfigStatus.MISSING,
                fix_description="Install xbar/SwiftBar for menu bar integration",
                priority=3,
            )

        return ConfigItem(
            name="Menu Bar Plugin",
            description="xbar plugin not installed (optional)",
            status=ConfigStatus.MISSING,
            fix_command="context-dna extras install-xbar",
            fix_url="/setup/xbar",
            fix_description="Install menu bar widget for quick access",
            priority=3,
        )

    def check_environment_variables(self) -> List[ConfigItem]:
        """Check important environment variables."""
        items = []

        # OpenAI (optional but common)
        openai_key = os.getenv("Context_DNA_OPENAI")
        if openai_key:
            # Validate format
            if openai_key.startswith("sk-") and len(openai_key) > 20:
                items.append(ConfigItem(
                    name="Context_DNA_OPENAI",
                    description="Valid OpenAI key configured",
                    status=ConfigStatus.OK,
                    value=f"{openai_key[:8]}...{openai_key[-4:]}",
                    priority=3,
                ))
            else:
                items.append(ConfigItem(
                    name="Context_DNA_OPENAI",
                    description="Invalid OpenAI key format",
                    status=ConfigStatus.INVALID,
                    fix_description="Get a valid key from platform.openai.com",
                    fix_url="https://platform.openai.com/api-keys",
                    priority=2,
                ))

        # Anthropic (optional)
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            if anthropic_key.startswith("sk-ant-"):
                items.append(ConfigItem(
                    name="ANTHROPIC_API_KEY",
                    description="Valid Anthropic key configured",
                    status=ConfigStatus.OK,
                    value=f"{anthropic_key[:12]}...",
                    priority=3,
                ))

        return items

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def _check_ollama(self) -> bool:
        """Check if Ollama is running."""
        try:
            import urllib.request
            req = urllib.request.Request("http://localhost:11434/api/tags")
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def check_all(project_dir: str = None) -> SetupStatus:
    """
    Run all setup checks.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        Complete setup status
    """
    checker = SetupChecker(project_dir)
    return checker.check_all()


def get_missing_configs(project_dir: str = None) -> List[Dict]:
    """
    Get list of missing configurations with fix commands.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        List of missing config dicts with fix info
    """
    status = check_all(project_dir)
    return [
        {
            "name": issue.name,
            "description": issue.description,
            "fix_command": issue.fix_command,
            "fix_url": issue.fix_url,
            "priority": issue.priority,
        }
        for issue in status.issues + status.warnings
    ]


def get_setup_commands(project_dir: str = None) -> List[str]:
    """
    Get list of commands to complete setup.

    Args:
        project_dir: Project directory (defaults to cwd)

    Returns:
        List of shell commands to run
    """
    missing = get_missing_configs(project_dir)
    return [
        m["fix_command"]
        for m in missing
        if m.get("fix_command")
    ]


def print_setup_status(project_dir: str = None) -> None:
    """Print formatted setup status to terminal."""
    status = check_all(project_dir)

    print()
    print("=" * 50)
    print("       CONTEXT DNA SETUP STATUS")
    print("=" * 50)
    print()
    print(f"  Completion: {status.completion_percent}%")
    print(f"  Status: {'✅ Ready' if status.is_complete else '⚠️ Setup needed'}")
    print()

    if status.issues:
        print("❌ Critical Issues:")
        for issue in status.issues:
            print(f"   • {issue.name}: {issue.description}")
            if issue.fix_command:
                print(f"     Fix: {issue.fix_command}")
        print()

    if status.warnings:
        print("⚠️ Recommendations:")
        for warning in status.warnings:
            print(f"   • {warning.name}: {warning.description}")
            if warning.fix_command:
                print(f"     Run: {warning.fix_command}")
        print()

    if status.ok_items:
        print("✅ Configured:")
        for ok in status.ok_items:
            print(f"   • {ok.name}: {ok.description}")
        print()

    if status.next_step:
        print(f"➡️ Next step: {status.next_step}")
        print()

    print(f"Dashboard: {status.dashboard_url}")
    print()


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        status = check_all()
        print(json.dumps(status.to_dict(), indent=2))
    else:
        print_setup_status()
