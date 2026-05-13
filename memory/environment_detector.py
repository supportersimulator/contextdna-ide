"""
Environment Detector for Context DNA Vibe Coder Launch

Detects installed development tools, IDE extensions, and workspaces
for seamless beginner onboarding.

Created: January 29, 2026
Updated: January 29, 2026 - Added comprehensive edge case handling
Part of: Vibe Coder Launch Install Initiative

DESIGN PRINCIPLES:
1. GRACEFUL FALLBACKS: Every detection method has multiple fallback paths
2. NEVER CRASH: All subprocess calls are wrapped in try/except with timeouts
3. PREFER DEFAULTS: When uncertain, use safe defaults that work for most users
4. CLEAR MESSAGING: Always tell user what we found and what's missing
5. OPT-IN INVASIVE: History reading requires explicit flag
"""

from __future__ import annotations
import os
import sys
import json
import platform
import subprocess
import shutil
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
from collections import Counter
import re

# Configure logging for debugging edge cases
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("environment_detector")

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

# ============================================================================
# CONSTANTS & DEFAULTS
# ============================================================================

# Timeout for subprocess calls (seconds) - fail fast, don't hang
SUBPROCESS_TIMEOUT = 15

# Default install commands by platform
DEFAULT_INSTALL_COMMANDS = {
    "Darwin": {
        "vscode": "brew install --cask visual-studio-code",
        "python": "brew install python@3.12",
        "git": "xcode-select --install",
        "node": "brew install node",
        "docker": "brew install --cask docker",
        "claude_code": "npm install -g @anthropic-ai/claude-code",
        "cursor": "brew install --cask cursor",
    },
    "Windows": {
        "vscode": "winget install Microsoft.VisualStudioCode",
        "python": "winget install Python.Python.3.12",
        "git": "winget install Git.Git",
        "node": "winget install OpenJS.NodeJS.LTS",
        "docker": "winget install Docker.DockerDesktop",
        "claude_code": "npm install -g @anthropic-ai/claude-code",
        "cursor": "Download from cursor.com",
    },
    "Linux": {
        "vscode": "sudo snap install code --classic",
        "python": "sudo apt install python3",
        "git": "sudo apt install git",
        "node": "sudo apt install nodejs npm",
        "docker": "sudo apt install docker.io",
        "claude_code": "npm install -g @anthropic-ai/claude-code",
        "cursor": "Download from cursor.com",
    },
}


def safe_subprocess_run(
    cmd: List[str],
    timeout: int = SUBPROCESS_TIMEOUT,
    capture_output: bool = True
) -> Tuple[bool, str, str]:
    """
    Safely run a subprocess with comprehensive error handling.

    Returns: (success, stdout, stderr)

    NEVER raises an exception - always returns gracefully.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            # Don't let subprocess inherit weird environment
            env={**os.environ, "LANG": "en_US.UTF-8"}
        )
        return (
            result.returncode == 0,
            result.stdout.strip() if result.stdout else "",
            result.stderr.strip() if result.stderr else ""
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        return (False, "", f"Timeout after {timeout}s")
    except FileNotFoundError:
        logger.debug(f"Command not found: {cmd[0]}")
        return (False, "", f"Command not found: {cmd[0]}")
    except PermissionError:
        logger.warning(f"Permission denied: {' '.join(cmd)}")
        return (False, "", "Permission denied")
    except OSError as e:
        logger.warning(f"OS error running {cmd}: {e}")
        return (False, "", str(e))
    except Exception as e:
        logger.error(f"Unexpected error running {cmd}: {e}")
        return (False, "", str(e))


def safe_path_exists(path: Path) -> bool:
    """Safely check if a path exists, handling permission errors."""
    try:
        return path.exists()
    except (PermissionError, OSError):
        return False


def safe_path_iterdir(path: Path) -> List[Path]:
    """Safely iterate directory contents, handling permission errors."""
    try:
        if path.exists() and path.is_dir():
            return list(path.iterdir())
    except (PermissionError, OSError) as e:
        print(f"[WARN] Directory iteration failed for {path}: {e}")
    return []


@dataclass
class ToolStatus:
    """
    Status of a development tool.

    detection_method: How we found this (command, app, registry, etc.)
    fallback_used: True if we used a fallback detection method
    """
    name: str
    installed: bool
    version: Optional[str] = None
    path: Optional[str] = None
    install_command: Optional[str] = None
    notes: Optional[str] = None
    detection_method: Optional[str] = None
    fallback_used: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ExtensionStatus:
    """Status of a VS Code extension."""
    id: str
    name: str
    installed: bool
    required: bool = False
    install_command: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class WorkspaceInfo:
    """Information about a detected workspace."""
    path: str
    name: str
    has_git: bool = False
    has_context_dna: bool = False
    project_type: Optional[str] = None
    last_modified: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class EnvironmentProfile:
    """
    Complete environment profile for vibe coder setup.

    Includes:
    - Detection results for all tools
    - What's missing and what to install
    - Recommended preferences for new users
    - Platform-specific guidance
    """
    tools: Dict[str, ToolStatus] = field(default_factory=dict)
    extensions: List[ExtensionStatus] = field(default_factory=list)
    workspaces: List[WorkspaceInfo] = field(default_factory=list)
    workspace_status: str = "unknown"  # "none_found" | "found" | "multiple"
    missing_critical: List[str] = field(default_factory=list)
    missing_recommended: List[str] = field(default_factory=list)
    install_recommendations: List[Dict] = field(default_factory=list)
    ready_for_context_dna: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    # NEW: Enhanced profile information
    platform_info: Dict[str, str] = field(default_factory=dict)
    preferred_ide: Optional[str] = None  # "vscode" | "cursor" | None
    preferred_package_manager: Optional[str] = None  # "homebrew" | "apt" | "winget"
    warnings: List[str] = field(default_factory=list)  # Non-critical issues
    quick_start_command: Optional[str] = None  # One-liner to get started

    def to_dict(self) -> Dict:
        return {
            "tools": {k: v.to_dict() for k, v in self.tools.items()},
            "extensions": [e.to_dict() for e in self.extensions],
            "workspaces": [w.to_dict() for w in self.workspaces],
            "workspace_status": self.workspace_status,
            "missing_critical": self.missing_critical,
            "missing_recommended": self.missing_recommended,
            "install_recommendations": self.install_recommendations,
            "ready_for_context_dna": self.ready_for_context_dna,
            "timestamp": self.timestamp,
            "platform_info": self.platform_info,
            "preferred_ide": self.preferred_ide,
            "preferred_package_manager": self.preferred_package_manager,
            "warnings": self.warnings,
            "quick_start_command": self.quick_start_command,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def get_summary(self) -> str:
        """Human-readable summary of environment status."""
        lines = []

        if self.ready_for_context_dna:
            lines.append("✅ Environment is READY for Context DNA!")
        else:
            lines.append("⚠️ Environment needs setup before Context DNA")

        if self.preferred_ide:
            lines.append(f"Recommended IDE: {self.preferred_ide}")

        if self.missing_critical:
            lines.append(f"Missing critical: {', '.join(self.missing_critical)}")

        if self.quick_start_command:
            lines.append(f"Quick start: {self.quick_start_command}")

        return "\n".join(lines)


class EnvironmentDetector:
    """
    Detect installed development tools, IDE extensions, and workspaces.

    Used by:
    - Dashboard "Analyze System" button
    - install-context-dna.sh --vibe-coder mode
    - First-run setup wizard
    """

    # Critical tools required for Context DNA
    # Note: IDE requirement (vscode OR cursor) handled separately below
    CRITICAL_TOOLS = ["python", "git"]

    # IDE requirement: VS Code is recommended, but Cursor is also acceptable
    IDE_REQUIRED = True

    # Claude Code can be satisfied by CLI OR extension - special handling below
    CLAUDE_CODE_REQUIRED = True

    # Recommended tools (nice to have but not required)
    RECOMMENDED_TOOLS = ["node", "docker"]

    # VS Code extensions
    # IMPORTANT: Claude Code exists as BOTH a VS Code extension AND a CLI tool.
    # The VS Code extension (anthropic.claude-code) is PREFERRED for new vibe coders
    # because it's less intimidating than CLI tools. Both methods are valid.
    # DO NOT REMOVE the Claude Code extension from this list - it is the recommended
    # entry point for beginners working within VS Code.
    VSCODE_EXTENSIONS = [
        {"id": "anthropic.claude-code", "name": "Claude Code", "required": False},  # PREFERRED for vibe coders
        {"id": "ms-python.python", "name": "Python", "required": False},
        {"id": "eamodio.gitlens", "name": "GitLens", "required": False},
        {"id": "esbenp.prettier-vscode", "name": "Prettier", "required": False},
    ]

    # Common workspace locations to search
    WORKSPACE_SEARCH_PATHS = [
        Path.home() / "Documents",
        Path.home() / "Projects",
        Path.home() / "Developer",
        Path.home() / "Code",
        Path.home() / "repos",
        Path.home() / "workspace",
        Path.home() / "dev",
    ]

    def __init__(self):
        self.platform = platform.system()
        self.is_macos = self.platform == "Darwin"
        self.is_windows = self.platform == "Windows"
        self.is_linux = self.platform == "Linux"

    def detect_all(self) -> EnvironmentProfile:
        """
        Run complete environment detection with comprehensive edge case handling.

        This method NEVER throws exceptions - all errors are captured as warnings.
        """
        profile = EnvironmentProfile()

        # =====================================================================
        # PHASE 1: Platform Detection (Always succeeds)
        # =====================================================================
        profile.platform_info = {
            "os": self.platform,
            "os_version": platform.version(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "home_dir": str(Path.home()),
        }

        # Detect preferred package manager based on platform
        profile.preferred_package_manager = self._detect_preferred_package_manager()

        # =====================================================================
        # PHASE 2: Tool Detection (Each detection is isolated)
        # =====================================================================
        # Detect tools - order matters for preference detection
        profile.tools["claude_code"] = self.detect_claude_code()
        profile.tools["vscode"] = self.detect_vscode()
        profile.tools["cursor"] = self.detect_cursor()
        profile.tools["python"] = self.detect_python()
        profile.tools["git"] = self.detect_git()
        profile.tools["node"] = self.detect_node()
        profile.tools["docker"] = self.detect_docker()

        # Platform-specific tool detection
        if self.is_macos:
            profile.tools["homebrew"] = self.detect_homebrew()
        elif self.is_windows:
            profile.tools["winget"] = self._detect_winget()

        # =====================================================================
        # PHASE 3: Extension Detection (with fallback)
        # =====================================================================
        # Try to detect extensions from either VS Code or Cursor
        if profile.tools["vscode"].installed:
            profile.extensions = self.detect_extensions()
        elif profile.tools["cursor"].installed:
            # Cursor uses similar extension system
            profile.extensions = self.detect_extensions(for_cursor=True)
        else:
            # No IDE installed - skip extension detection
            profile.warnings.append("No IDE detected - skipping extension detection")

        # =====================================================================
        # PHASE 4: Workspace Detection (with timeout protection)
        # =====================================================================
        try:
            profile.workspaces = self.find_workspaces()
        except Exception as e:
            logger.warning(f"Workspace detection failed: {e}")
            profile.workspaces = []
            profile.warnings.append(f"Workspace scan incomplete: {str(e)[:50]}")

        if not profile.workspaces:
            profile.workspace_status = "none_found"
        elif len(profile.workspaces) == 1:
            profile.workspace_status = "found"
        else:
            profile.workspace_status = "multiple"

        # =====================================================================
        # PHASE 5: Requirements Evaluation
        # =====================================================================
        # Calculate missing critical items
        for tool_name in self.CRITICAL_TOOLS:
            tool = profile.tools.get(tool_name)
            if tool and not tool.installed:
                profile.missing_critical.append(tool_name)

        # Calculate missing recommended items
        for tool_name in self.RECOMMENDED_TOOLS:
            tool = profile.tools.get(tool_name)
            if tool and not tool.installed:
                profile.missing_recommended.append(tool_name)

        # Special handling: IDE - either VS Code OR Cursor satisfies requirement
        if self.IDE_REQUIRED:
            has_vscode = profile.tools.get("vscode", ToolStatus("", False)).installed
            has_cursor = profile.tools.get("cursor", ToolStatus("", False)).installed
            if not has_vscode and not has_cursor:
                profile.missing_critical.append("IDE (VS Code recommended, or Cursor)")
            else:
                # Set preferred IDE based on what's installed
                profile.preferred_ide = "vscode" if has_vscode else "cursor"

        # Special handling: Claude Code - either CLI OR extension satisfies requirement
        if self.CLAUDE_CODE_REQUIRED:
            has_claude_cli = profile.tools.get("claude_code", ToolStatus("", False)).installed
            has_claude_ext = any(
                ext.id == "anthropic.claude-code" and ext.installed
                for ext in profile.extensions
            )
            if not has_claude_cli and not has_claude_ext:
                profile.missing_critical.append("claude_code (CLI or VS Code extension)")

        # Check other required extensions
        for ext in profile.extensions:
            if ext.required and not ext.installed and ext.id != "anthropic.claude-code":
                profile.missing_critical.append(f"ext:{ext.id}")

        # =====================================================================
        # PHASE 6: Warnings Collection (non-critical issues)
        # =====================================================================
        self._collect_warnings(profile)

        # =====================================================================
        # PHASE 7: Readiness Check (MUST come before quick start generation)
        # =====================================================================
        profile.ready_for_context_dna = len(profile.missing_critical) == 0

        # =====================================================================
        # PHASE 8: Install Recommendations & Quick Start
        # =====================================================================
        profile.install_recommendations = self.get_install_plan(profile)

        # Generate quick start command (depends on ready_for_context_dna being set)
        profile.quick_start_command = self._generate_quick_start(profile)

        return profile

    def _detect_preferred_package_manager(self) -> Optional[str]:
        """Detect the user's preferred package manager."""
        if self.is_macos:
            if shutil.which("brew"):
                return "homebrew"
            return "homebrew"  # Recommend even if not installed
        elif self.is_windows:
            if shutil.which("winget"):
                return "winget"
            return "winget"
        elif self.is_linux:
            if shutil.which("apt"):
                return "apt"
            elif shutil.which("dnf"):
                return "dnf"
            elif shutil.which("pacman"):
                return "pacman"
            return "apt"  # Default recommendation
        return None

    def _detect_winget(self) -> ToolStatus:
        """Detect Windows winget package manager."""
        status = ToolStatus(
            name="winget",
            installed=False,
            install_command="Built into Windows 10/11",
        )
        winget_path = shutil.which("winget")
        if winget_path:
            status.installed = True
            status.path = winget_path
            success, stdout, _ = safe_subprocess_run(["winget", "--version"])
            if success:
                status.version = stdout
        return status

    def _collect_warnings(self, profile: EnvironmentProfile) -> None:
        """Collect non-critical warnings about the environment."""
        # Check for VS Code 'code' command not in PATH
        vscode = profile.tools.get("vscode")
        if vscode and vscode.installed and vscode.notes and "not in PATH" in vscode.notes:
            profile.warnings.append(
                "VS Code is installed but 'code' command not in PATH. "
                "Open VS Code > Cmd+Shift+P > 'Shell Command: Install code command'"
            )

        # Check for outdated Python
        python = profile.tools.get("python")
        if python and python.installed and python.version:
            try:
                major, minor = map(int, python.version.split(".")[:2])
                if major < 3 or (major == 3 and minor < 10):
                    profile.warnings.append(
                        f"Python {python.version} detected. Python 3.10+ recommended."
                    )
            except (ValueError, AttributeError) as e:
                print(f"[WARN] Python version parsing failed: {e}")

        # Check for Node.js (needed for Claude Code CLI install)
        node = profile.tools.get("node")
        if not node or not node.installed:
            profile.warnings.append(
                "Node.js not detected. Required for Claude Code CLI installation."
            )

        # Check for existing .claude directory without CLI
        claude = profile.tools.get("claude_code")
        if claude and not claude.installed:
            claude_dir = Path.home() / ".claude"
            if safe_path_exists(claude_dir):
                profile.warnings.append(
                    "~/.claude directory exists but 'claude' command not found. "
                    "Try: npm install -g @anthropic-ai/claude-code"
                )

    def _generate_quick_start(self, profile: EnvironmentProfile) -> Optional[str]:
        """
        Generate a one-liner quick start command.

        Handles edge cases:
        - VS Code installed but 'code' command not in PATH -> use cursor or manual open
        - Cursor installed but 'cursor' command not in PATH -> use manual open
        - Neither IDE has working command -> just run install script
        """
        install_script = "./scripts/install-context-dna.sh --vibe-coder"

        if profile.ready_for_context_dna:
            # Check if preferred IDE has a working command
            vscode = profile.tools.get("vscode")
            cursor = profile.tools.get("cursor")

            # Does VS Code have 'code' command working?
            vscode_cmd_works = vscode and vscode.installed and shutil.which("code")

            # Does Cursor have 'cursor' command working?
            cursor_cmd_works = cursor and cursor.installed and shutil.which("cursor")

            if profile.preferred_ide == "vscode":
                if vscode_cmd_works:
                    return f"code . && {install_script}"
                elif cursor_cmd_works:
                    # Fallback to cursor if vscode command doesn't work
                    return f"cursor . && {install_script}"
                else:
                    # Neither IDE command works - suggest manual open
                    return f"# Open VS Code manually, then run: {install_script}"

            elif profile.preferred_ide == "cursor":
                if cursor_cmd_works:
                    return f"cursor . && {install_script}"
                elif vscode_cmd_works:
                    return f"code . && {install_script}"
                else:
                    return f"# Open Cursor manually, then run: {install_script}"

            return install_script

        # Not ready - show install command for first missing critical item
        if profile.missing_critical:
            first_missing = profile.missing_critical[0]

            # Handle compound requirements like "IDE (VS Code recommended, or Cursor)"
            if "IDE" in first_missing:
                install_cmd = DEFAULT_INSTALL_COMMANDS.get(self.platform, {}).get("vscode")
            elif "claude_code" in first_missing:
                install_cmd = DEFAULT_INSTALL_COMMANDS.get(self.platform, {}).get("claude_code")
            else:
                install_cmd = DEFAULT_INSTALL_COMMANDS.get(self.platform, {}).get(first_missing)

            if install_cmd:
                return f"First: {install_cmd}"

        return install_script  # Always return something useful

    def detect_claude_code(self) -> ToolStatus:
        """
        Detect Claude Code CLI installation.

        IMPORTANT: Claude Code exists as BOTH a VS Code extension AND a CLI tool.

        PREFERRED: The VS Code extension (anthropic.claude-code) is the recommended
        entry point for new vibe coders - it's less intimidating than CLI tools
        and provides a familiar IDE-integrated experience.

        SECONDARY: The CLI tool (npm install -g @anthropic-ai/claude-code) is also
        valid and detected here for power users who prefer terminal workflows.

        Both methods are fully supported. DO NOT remove either detection method.

        Detection priority for CLI:
        1. 'claude' command in PATH (best case)
        2. npx claude (works without global install)
        3. npm global packages list
        4. ~/.claude directory (partial detection)

        Edge cases:
        - Node.js not installed (can't use claude CLI)
        - npm cache issues
        - Permission problems
        - PATH not set correctly
        """
        status = ToolStatus(
            name="Claude Code CLI",
            installed=False,
            install_command=DEFAULT_INSTALL_COMMANDS.get(self.platform, {}).get(
                "claude_code", "npm install -g @anthropic-ai/claude-code"
            ),
            detection_method=None
        )

        # =====================================================================
        # Method 1: 'claude' command in PATH (best case)
        # =====================================================================
        claude_path = shutil.which("claude")
        if claude_path:
            status.installed = True
            status.path = claude_path
            status.detection_method = "PATH"

            success, stdout, _ = safe_subprocess_run(["claude", "--version"])
            if success:
                status.version = stdout
            return status

        # =====================================================================
        # Method 2: npx claude (works without global install)
        # =====================================================================
        if shutil.which("npx"):
            success, stdout, _ = safe_subprocess_run(
                ["npx", "--yes", "claude", "--version"],
                timeout=30  # npx can be slow on first run
            )
            if success:
                status.installed = True
                status.path = "npx claude"
                status.version = stdout
                status.detection_method = "npx"
                status.fallback_used = True
                status.notes = "Available via npx (not globally installed)"
                return status

        # =====================================================================
        # Method 3: npm global packages list
        # =====================================================================
        if shutil.which("npm"):
            success, stdout, _ = safe_subprocess_run(
                ["npm", "list", "-g", "@anthropic-ai/claude-code", "--json"]
            )
            if success and "@anthropic-ai/claude-code" in stdout:
                status.installed = True
                status.detection_method = "npm-global"
                status.fallback_used = True
                status.notes = "Found in npm global packages but 'claude' not in PATH"
                return status

        # =====================================================================
        # Method 4: Check for ~/.claude directory (partial detection)
        # =====================================================================
        claude_dir = Path.home() / ".claude"
        if safe_path_exists(claude_dir):
            status.notes = (
                "~/.claude directory exists but 'claude' command not found. "
                "Try reinstalling: npm install -g @anthropic-ai/claude-code"
            )

        # =====================================================================
        # Method 5: Check if Node.js is even available
        # =====================================================================
        if not shutil.which("node") and not shutil.which("npm"):
            status.notes = (
                "Node.js not detected. Install Node.js first, then: "
                "npm install -g @anthropic-ai/claude-code"
            )

        return status

    def detect_vscode(self) -> ToolStatus:
        """
        Detect VS Code installation.

        VS Code is the PRIMARY recommended IDE for Context DNA beginners.
        This detection is thorough to catch all installation scenarios.
        """
        status = ToolStatus(
            name="VS Code",
            installed=False,
            install_command=self._get_vscode_install_command()
        )

        # Priority 1: Check for VS Code binary in PATH (best case)
        code_path = shutil.which("code")
        if code_path:
            status.installed = True
            status.path = code_path
            status.version = self._get_command_version(["code", "--version"])
            return status

        # Priority 2: Check for app installation (macOS)
        if self.is_macos:
            # Check for VS Code with various naming conventions
            app_paths = [
                Path("/Applications/Visual Studio Code.app"),
                Path.home() / "Applications" / "Visual Studio Code.app",
            ]

            # Also check for numbered versions (e.g., "Visual Studio Code 3.app")
            # and Cursor (which is a VS Code fork that supports Claude Code extension)
            try:
                for app_dir in [Path("/Applications"), Path.home() / "Applications"]:
                    if app_dir.exists():
                        for app in app_dir.iterdir():
                            app_name = app.name.lower()
                            if ("visual studio code" in app_name or "vscode" in app_name) and app.suffix == ".app":
                                app_paths.insert(0, app)  # Prioritize found variants
            except Exception as e:
                print(f"[WARN] VS Code app search failed: {e}")
            for app_path in app_paths:
                if app_path.exists():
                    status.installed = True
                    status.path = str(app_path)
                    # Try to get version from app bundle
                    try:
                        info_plist = app_path / "Contents" / "Info.plist"
                        if info_plist.exists():
                            import plistlib
                            with open(info_plist, 'rb') as f:
                                plist = plistlib.load(f)
                                status.version = plist.get("CFBundleShortVersionString")
                    except Exception as e:
                        print(f"[WARN] VS Code plist version read failed: {e}")

                    # Check if 'code' command is in PATH
                    if not shutil.which("code"):
                        status.notes = (
                            "VS Code app found but 'code' command not in PATH. "
                            "Run: Open VS Code > Cmd+Shift+P > 'Shell Command: Install code command'"
                        )
                    return status

        # Priority 3: Check Windows paths
        if self.is_windows:
            win_paths = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe",
                Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft VS Code" / "Code.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft VS Code" / "Code.exe",
            ]
            for win_path in win_paths:
                if win_path.exists():
                    status.installed = True
                    status.path = str(win_path)
                    # Check if 'code' command is in PATH
                    if not shutil.which("code"):
                        status.notes = (
                            "VS Code found but 'code' command not in PATH. "
                            "Restart your terminal or add VS Code to PATH."
                        )
                    return status

        # Priority 4: Check Linux paths
        if self.is_linux:
            linux_paths = [
                Path("/usr/share/code/code"),
                Path("/snap/bin/code"),
                Path.home() / ".local/share/code/code",
            ]
            for linux_path in linux_paths:
                if linux_path.exists():
                    status.installed = True
                    status.path = str(linux_path)
                    return status

        return status

    def detect_python(self) -> ToolStatus:
        """Detect Python installation."""
        status = ToolStatus(
            name="Python",
            installed=False,
            install_command=self._get_python_install_command()
        )

        # Try python3 first
        python_path = shutil.which("python3")
        if python_path:
            status.installed = True
            status.path = python_path
            status.version = self._get_command_version(["python3", "--version"])
            return status

        # Fallback to python
        python_path = shutil.which("python")
        if python_path:
            version = self._get_command_version(["python", "--version"])
            # Make sure it's Python 3
            if version and version.startswith("3"):
                status.installed = True
                status.path = python_path
                status.version = version
                return status

        return status

    def detect_git(self) -> ToolStatus:
        """Detect Git installation."""
        status = ToolStatus(
            name="Git",
            installed=False,
            install_command=self._get_git_install_command()
        )

        git_path = shutil.which("git")
        if git_path:
            status.installed = True
            status.path = git_path
            status.version = self._get_command_version(["git", "--version"])

        return status

    def detect_node(self) -> ToolStatus:
        """Detect Node.js installation."""
        status = ToolStatus(
            name="Node.js",
            installed=False,
            install_command=self._get_node_install_command()
        )

        node_path = shutil.which("node")
        if node_path:
            status.installed = True
            status.path = node_path
            status.version = self._get_command_version(["node", "--version"])

        return status

    def detect_docker(self) -> ToolStatus:
        """Detect Docker installation."""
        status = ToolStatus(
            name="Docker",
            installed=False,
            install_command=self._get_docker_install_command()
        )

        docker_path = shutil.which("docker")
        if docker_path:
            status.installed = True
            status.path = docker_path
            status.version = self._get_command_version(["docker", "--version"])

        return status

    def detect_cursor(self) -> ToolStatus:
        """
        Detect Cursor IDE installation.

        Cursor is a VS Code fork with built-in AI features.
        It's a valid alternative to VS Code for Context DNA.
        """
        status = ToolStatus(
            name="Cursor",
            installed=False,
            install_command="brew install --cask cursor" if self.is_macos else "Download from cursor.com"
        )

        # Check for cursor command
        cursor_path = shutil.which("cursor")
        if cursor_path:
            status.installed = True
            status.path = cursor_path
            return status

        # Check for Cursor app (macOS)
        if self.is_macos:
            cursor_paths = [
                Path("/Applications/Cursor.app"),
                Path.home() / "Applications" / "Cursor.app",
            ]
            for cursor_app in cursor_paths:
                if cursor_app.exists():
                    status.installed = True
                    status.path = str(cursor_app)
                    if not shutil.which("cursor"):
                        status.notes = "Cursor app found but 'cursor' command not in PATH"
                    return status

        return status

    def detect_homebrew(self) -> ToolStatus:
        """Detect Homebrew (macOS/Linux)."""
        status = ToolStatus(
            name="Homebrew",
            installed=False,
            install_command='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        )

        brew_path = shutil.which("brew")
        if brew_path:
            status.installed = True
            status.path = brew_path
            status.version = self._get_command_version(["brew", "--version"])

        return status

    def detect_extensions(self, for_cursor: bool = False) -> List[ExtensionStatus]:
        """
        Detect installed VS Code/Cursor extensions.

        Supports both VS Code and Cursor (they share similar extension systems).
        Uses multiple detection methods with graceful fallbacks.
        """
        extensions = []
        installed_ids = self._get_installed_extensions(for_cursor=for_cursor)

        # Determine the correct install command prefix
        cmd_prefix = "cursor" if for_cursor else "code"

        for ext_info in self.VSCODE_EXTENSIONS:
            ext = ExtensionStatus(
                id=ext_info["id"],
                name=ext_info["name"],
                installed=ext_info["id"].lower() in [e.lower() for e in installed_ids],
                required=ext_info.get("required", False),
                install_command=f'{cmd_prefix} --install-extension {ext_info["id"]}'
            )
            extensions.append(ext)

        return extensions

    def _get_installed_extensions(self, for_cursor: bool = False) -> List[str]:
        """
        Get list of installed VS Code/Cursor extensions.

        Detection priority:
        1. CLI command (code/cursor --list-extensions) - most reliable
        2. Extensions directory scan - fallback when CLI unavailable
        3. Empty list - graceful fallback, never crashes

        Edge cases handled:
        - CLI command not in PATH
        - Extensions directory permission denied
        - Malformed extension directory names
        - Timeout on slow file systems
        """
        installed = []

        # =====================================================================
        # Method 1: CLI command (most reliable)
        # =====================================================================
        cli_cmd = "cursor" if for_cursor else "code"
        if shutil.which(cli_cmd):
            success, stdout, stderr = safe_subprocess_run(
                [cli_cmd, "--list-extensions"],
                timeout=30
            )
            if success and stdout:
                installed = [ext.strip() for ext in stdout.split("\n") if ext.strip()]
                if installed:
                    logger.debug(f"Found {len(installed)} extensions via {cli_cmd} CLI")
                    return installed

        # =====================================================================
        # Method 2: Scan extensions directory (fallback)
        # =====================================================================
        extensions_dirs = []

        if for_cursor:
            # Cursor extension directories
            if self.is_macos:
                extensions_dirs = [
                    Path.home() / ".cursor" / "extensions",
                    Path.home() / "Library" / "Application Support" / "Cursor" / "extensions",
                ]
            elif self.is_windows:
                extensions_dirs = [
                    Path(os.environ.get("USERPROFILE", "")) / ".cursor" / "extensions",
                    Path(os.environ.get("APPDATA", "")) / "Cursor" / "extensions",
                ]
            else:
                extensions_dirs = [
                    Path.home() / ".cursor" / "extensions",
                    Path.home() / ".config" / "Cursor" / "extensions",
                ]
        else:
            # VS Code extension directories
            if self.is_macos:
                extensions_dirs = [
                    Path.home() / ".vscode" / "extensions",
                    Path.home() / ".vscode-insiders" / "extensions",
                ]
            elif self.is_windows:
                extensions_dirs = [
                    Path(os.environ.get("USERPROFILE", "")) / ".vscode" / "extensions",
                    Path(os.environ.get("USERPROFILE", "")) / ".vscode-insiders" / "extensions",
                ]
            else:
                extensions_dirs = [
                    Path.home() / ".vscode" / "extensions",
                    Path.home() / ".vscode-server" / "extensions",
                ]

        for ext_dir in extensions_dirs:
            if safe_path_exists(ext_dir):
                try:
                    for item in safe_path_iterdir(ext_dir):
                        if item.is_dir() and not item.name.startswith("."):
                            # Extension folder format: publisher.name-version
                            # We want: publisher.name
                            ext_id = self._parse_extension_dir_name(item.name)
                            installed.append(ext_id)
                except Exception as e:
                    logger.debug(f"Error scanning {ext_dir}: {e}")
                    continue

        if installed:
            # Remove duplicates while preserving order
            seen = set()
            unique = []
            for ext in installed:
                if ext.lower() not in seen:
                    seen.add(ext.lower())
                    unique.append(ext)
            logger.debug(f"Found {len(unique)} extensions via directory scan")
            return unique

        # =====================================================================
        # Method 3: Empty list (graceful fallback)
        # =====================================================================
        logger.debug("No extensions detected via any method")
        return []

    def find_workspaces(self, max_depth: int = 2) -> List[WorkspaceInfo]:
        """Find existing development workspaces."""
        workspaces = []
        seen_paths = set()

        for search_path in self.WORKSPACE_SEARCH_PATHS:
            if not search_path.exists():
                continue

            # Search for git repositories
            workspaces.extend(self._find_git_repos(search_path, max_depth, seen_paths))

        # Sort by last modified (most recent first)
        workspaces.sort(key=lambda w: w.last_modified or "", reverse=True)

        return workspaces[:20]  # Limit to 20 most recent

    def _find_git_repos(self, base_path: Path, max_depth: int, seen: set) -> List[WorkspaceInfo]:
        """Find git repositories in a directory."""
        repos = []

        if max_depth < 0:
            return repos

        try:
            for item in base_path.iterdir():
                if not item.is_dir() or item.name.startswith("."):
                    continue

                full_path = str(item.resolve())
                if full_path in seen:
                    continue

                git_dir = item / ".git"
                if git_dir.exists():
                    seen.add(full_path)
                    repos.append(self._create_workspace_info(item))
                elif max_depth > 0:
                    repos.extend(self._find_git_repos(item, max_depth - 1, seen))
        except PermissionError as e:
            print(f"[WARN] Permission denied scanning for git repos: {e}")

        return repos

    def _create_workspace_info(self, path: Path) -> WorkspaceInfo:
        """Create WorkspaceInfo for a directory."""
        # Detect project type
        project_type = self._detect_project_type(path)

        # Get last modified time
        try:
            mtime = path.stat().st_mtime
            last_modified = datetime.fromtimestamp(mtime).isoformat()
        except Exception:
            last_modified = None

        return WorkspaceInfo(
            path=str(path),
            name=path.name,
            has_git=(path / ".git").exists(),
            has_context_dna=(path / ".context-dna").exists(),
            project_type=project_type,
            last_modified=last_modified
        )

    def _detect_project_type(self, path: Path) -> Optional[str]:
        """Detect project type from files."""
        indicators = {
            "package.json": "nodejs",
            "requirements.txt": "python",
            "Cargo.toml": "rust",
            "go.mod": "go",
            "pom.xml": "java",
            "build.gradle": "java",
            "Gemfile": "ruby",
            "composer.json": "php",
            "pyproject.toml": "python",
            "setup.py": "python",
            "next.config.js": "nextjs",
            "next.config.ts": "nextjs",
            "app.py": "flask",
            "manage.py": "django",
            "Dockerfile": "docker",
        }

        for filename, project_type in indicators.items():
            if (path / filename).exists():
                return project_type

        return None

    def get_install_plan(self, profile: Optional[EnvironmentProfile] = None) -> List[Dict]:
        """Generate installation plan for missing components."""
        if profile is None:
            profile = self.detect_all()

        plan = []

        # Add missing tools
        for tool_name in self.CRITICAL_TOOLS:
            tool = profile.tools.get(tool_name)
            if tool and not tool.installed and tool.install_command:
                plan.append({
                    "type": "tool",
                    "name": tool.name,
                    "id": tool_name,
                    "command": tool.install_command,
                    "priority": "critical"
                })

        for tool_name in self.RECOMMENDED_TOOLS:
            tool = profile.tools.get(tool_name)
            if tool and not tool.installed and tool.install_command:
                plan.append({
                    "type": "tool",
                    "name": tool.name,
                    "id": tool_name,
                    "command": tool.install_command,
                    "priority": "recommended"
                })

        # Add missing extensions
        for ext in profile.extensions:
            if not ext.installed and ext.install_command:
                plan.append({
                    "type": "extension",
                    "name": ext.name,
                    "id": ext.id,
                    "command": ext.install_command,
                    "priority": "critical" if ext.required else "recommended"
                })

        return plan

    def _get_command_version(self, cmd: List[str]) -> Optional[str]:
        """Run command and extract version string."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                output = result.stdout.strip()
                # Extract version number (first line, first version-like string)
                for line in output.split("\n"):
                    line = line.strip()
                    if line:
                        # Remove common prefixes
                        for prefix in ["Python ", "git version ", "v", "node "]:
                            if line.startswith(prefix):
                                line = line[len(prefix):]
                        # Return first word that looks like version
                        for word in line.split():
                            if word[0].isdigit():
                                return word
                        return line
        except Exception as e:
            print(f"[WARN] Tool version detection failed: {e}")
        return None

    def _get_vscode_install_command(self) -> str:
        """Get VS Code install command for current platform."""
        if self.is_macos:
            return "brew install --cask visual-studio-code"
        elif self.is_windows:
            return "winget install Microsoft.VisualStudioCode"
        else:
            return "sudo snap install code --classic"

    def _get_python_install_command(self) -> str:
        """Get Python install command for current platform."""
        if self.is_macos:
            return "brew install python@3.12"
        elif self.is_windows:
            return "winget install Python.Python.3.12"
        else:
            return "sudo apt install python3"

    def _get_git_install_command(self) -> str:
        """Get Git install command for current platform."""
        if self.is_macos:
            return "xcode-select --install"
        elif self.is_windows:
            return "winget install Git.Git"
        else:
            return "sudo apt install git"

    def _get_node_install_command(self) -> str:
        """Get Node.js install command for current platform."""
        if self.is_macos:
            return "brew install node"
        elif self.is_windows:
            return "winget install OpenJS.NodeJS.LTS"
        else:
            return "sudo apt install nodejs npm"

    def _get_docker_install_command(self) -> str:
        """Get Docker install command for current platform."""
        if self.is_macos:
            return "brew install --cask docker"
        elif self.is_windows:
            return "winget install Docker.DockerDesktop"
        else:
            return "sudo apt install docker.io"

    def _parse_extension_dir_name(self, dir_name: str) -> str:
        """
        Parse extension ID from directory name.

        Extension directories are formatted as: publisher.name-version
        Examples:
            ms-python.python-2024.1.0      -> ms-python.python
            anthropic.claude-code-1.0.0    -> anthropic.claude-code
            one-more-thing-0.1.0-beta      -> one-more-thing
            eamodio.gitlens-15.0.0         -> eamodio.gitlens
            my-cool-ext-1.2.3-preview.1    -> my-cool-ext

        The version can include:
            - Simple: 1.0.0, 2024.1.0
            - Pre-release: 0.1.0-beta, 1.0.0-alpha.1, 2.0.0-rc.1
            - Build metadata: 1.0.0+build.123

        Strategy: Use regex to find version pattern at the end, everything before is the ID.
        """
        # Pattern matches version at end: -X.Y.Z with optional pre-release/build
        # Examples: -1.0.0, -2024.1.0, -0.1.0-beta, -1.0.0-alpha.1, -1.0.0+build
        version_pattern = r'-(\d+\.\d+\.\d+(?:[-+][\w.]+)?)$'
        match = re.search(version_pattern, dir_name)

        if match:
            # Found version, return everything before it
            return dir_name[:match.start()]

        # Fallback: try simpler pattern (just digits and dots at end)
        simple_pattern = r'-(\d+(?:\.\d+)+)$'
        match = re.search(simple_pattern, dir_name)
        if match:
            return dir_name[:match.start()]

        # No version found, return full name
        return dir_name


class TerminalHistoryLearner:
    """
    Learn from user's terminal history to improve detection accuracy.

    Philosophy: The user's past commands reveal their actual setup process.
    This is GOLD for automation - we can see exactly what worked for them.

    Privacy: Only reads LOCAL history files, never phones home.
    Consent: User must explicitly opt-in via --learn-from-history flag.
    """

    # Patterns we're looking for in history
    INSTALL_PATTERNS = {
        "homebrew": [
            r"brew install (.+)",
            r"brew cask install (.+)",
            r"brew install --cask (.+)",
        ],
        "apt": [
            r"sudo apt install (.+)",
            r"apt-get install (.+)",
        ],
        "npm": [
            r"npm install -g (.+)",
            r"npm i -g (.+)",
        ],
        "pip": [
            r"pip install (.+)",
            r"pip3 install (.+)",
        ],
        "vscode": [
            r"code --install-extension (.+)",
        ],
        "winget": [
            r"winget install (.+)",
        ],
        "claude": [
            r"claude (.+)",  # Claude Code CLI usage
            r"npx claude (.+)",
        ],
    }

    # Commands that indicate successful setup
    SUCCESS_INDICATORS = [
        r"^code \.?$",  # User successfully opened VS Code
        r"^python3? .+\.py",  # User ran Python
        r"^npm (run|start|test)",  # User ran npm scripts
        r"^docker (compose|run)",  # User ran Docker
        r"^git (commit|push|pull)",  # User used Git
        r"^claude ",  # User ran Claude CLI
    ]

    def __init__(self):
        self.platform = platform.system()
        self.history_file = self._get_history_file()

    def _get_history_file(self) -> Optional[Path]:
        """Find the user's shell history file."""
        home = Path.home()

        # Check which shell is being used
        shell = os.environ.get("SHELL", "")

        if "zsh" in shell:
            history_file = home / ".zsh_history"
        elif "bash" in shell:
            history_file = home / ".bash_history"
        elif self.platform == "Windows":
            # PowerShell history location
            history_file = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "PowerShell" / "PSReadLine" / "ConsoleHost_history.txt"
        else:
            # Default to bash
            history_file = home / ".bash_history"

        if history_file.exists():
            return history_file
        return None

    def learn_from_history(self, max_lines: int = 5000) -> Dict[str, Any]:
        """
        Analyze terminal history to learn about user's setup journey.

        Returns insights about:
        - What tools they installed and how
        - What package managers they prefer
        - What commands they ran successfully
        - Failed attempts (repeated similar commands)
        """
        if not self.history_file:
            return {"error": "No history file found", "insights": {}}

        insights = {
            "history_file": str(self.history_file),
            "commands_analyzed": 0,
            "installations_found": [],
            "vscode_extensions_installed": [],
            "preferred_package_manager": None,
            "claude_cli_detected": False,
            "success_indicators": [],
            "repeated_commands": [],  # Might indicate struggles
            "install_commands_for_replay": [],  # Commands that could be re-run
        }

        try:
            # Read history file
            history_lines = self._read_history_file(max_lines)
            insights["commands_analyzed"] = len(history_lines)

            # Analyze patterns
            pkg_manager_counts = Counter()

            for line in history_lines:
                line = line.strip()
                if not line:
                    continue

                # Check for install commands
                for pkg_manager, patterns in self.INSTALL_PATTERNS.items():
                    for pattern in patterns:
                        match = re.search(pattern, line, re.IGNORECASE)
                        if match:
                            pkg_manager_counts[pkg_manager] += 1
                            installed_item = match.group(1).split()[0] if match.group(1) else ""

                            if pkg_manager == "vscode" and installed_item:
                                insights["vscode_extensions_installed"].append(installed_item)
                            else:
                                insights["installations_found"].append({
                                    "manager": pkg_manager,
                                    "package": installed_item,
                                    "command": line
                                })

                            # Save for potential replay
                            insights["install_commands_for_replay"].append(line)
                            break

                # Check for success indicators
                for indicator in self.SUCCESS_INDICATORS:
                    if re.match(indicator, line, re.IGNORECASE):
                        insights["success_indicators"].append(line[:50])
                        break

                # Check for Claude CLI usage
                if re.match(r"^claude ", line):
                    insights["claude_cli_detected"] = True

            # Determine preferred package manager
            if pkg_manager_counts:
                insights["preferred_package_manager"] = pkg_manager_counts.most_common(1)[0][0]

            # Find repeated commands (might indicate struggles)
            command_counts = Counter(history_lines)
            insights["repeated_commands"] = [
                cmd for cmd, count in command_counts.items()
                if count > 3 and any(kw in cmd for kw in ["install", "brew", "npm", "pip"])
            ][:5]  # Top 5 repeated install commands

        except Exception as e:
            insights["error"] = str(e)

        return insights

    def _read_history_file(self, max_lines: int) -> List[str]:
        """Read and parse history file."""
        if not self.history_file:
            return []

        try:
            with open(self.history_file, 'r', errors='ignore') as f:
                lines = f.readlines()[-max_lines:]  # Get last N lines

            # Parse based on format
            parsed = []
            for line in lines:
                # Zsh history has timestamp prefix: : 1234567890:0;command
                if line.startswith(": ") and ";" in line:
                    parsed.append(line.split(";", 1)[1].strip())
                else:
                    parsed.append(line.strip())

            return parsed
        except Exception:
            return []

    def generate_guided_install_script(self, insights: Dict) -> str:
        """
        Generate a personalized install script based on user's history.

        This respects their preferences (brew vs apt, etc.) and fills gaps.
        """
        lines = [
            "#!/bin/bash",
            "# Auto-generated Context DNA setup script",
            "# Based on your terminal history preferences",
            "",
            f"# Detected preferred package manager: {insights.get('preferred_package_manager', 'unknown')}",
            "",
        ]

        pkg_manager = insights.get("preferred_package_manager", "homebrew")

        # Add missing tools based on their preferred manager
        if pkg_manager == "homebrew":
            lines.append("# Using Homebrew (your preference)")
            lines.append("brew install python@3.12 || echo 'Python already installed'")
            lines.append("brew install git || echo 'Git already installed'")
            lines.append("brew install node || echo 'Node already installed'")
        elif pkg_manager == "apt":
            lines.append("# Using apt (your preference)")
            lines.append("sudo apt update")
            lines.append("sudo apt install -y python3 git nodejs npm")

        lines.append("")
        lines.append("# Install Claude Code CLI")
        lines.append("npm install -g @anthropic-ai/claude-code")

        # Add VS Code extensions they might be missing
        lines.append("")
        lines.append("# VS Code extensions")
        existing_exts = set(insights.get("vscode_extensions_installed", []))
        recommended = ["ms-python.python", "eamodio.gitlens"]
        for ext in recommended:
            if ext not in existing_exts:
                lines.append(f"code --install-extension {ext}")

        lines.append("")
        lines.append("echo '✅ Context DNA setup complete!'")

        return "\n".join(lines)

    def get_recommendation(self) -> Dict[str, Any]:
        """
        Get personalized setup recommendation based on history analysis.
        """
        insights = self.learn_from_history()

        recommendation = {
            "claude_cli_already_used": insights.get("claude_cli_detected", False),
            "vscode_ready": len(insights.get("vscode_extensions_installed", [])) > 0,
            "preferred_install_method": insights.get("preferred_package_manager"),
            "suggested_script": self.generate_guided_install_script(insights) if not insights.get("error") else None,
            "raw_insights": insights
        }

        return recommendation


# Convenience functions
def detect_environment() -> EnvironmentProfile:
    """Detect all environment components."""
    return EnvironmentDetector().detect_all()


def get_install_plan() -> List[Dict]:
    """Get installation plan for missing components."""
    return EnvironmentDetector().get_install_plan()


def is_ready_for_context_dna() -> bool:
    """Check if environment is ready for Context DNA installation."""
    profile = EnvironmentDetector().detect_all()
    return profile.ready_for_context_dna


# CLI interface
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Environment Detector for Context DNA"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--tools-only", action="store_true", help="Only show tools")
    parser.add_argument("--extensions-only", action="store_true", help="Only show extensions")
    parser.add_argument("--workspaces-only", action="store_true", help="Only show workspaces")
    parser.add_argument("--install-plan", action="store_true", help="Show install plan")
    parser.add_argument("--ready-check", action="store_true", help="Check if ready for Context DNA")
    parser.add_argument("--learn-from-history", action="store_true",
                        help="Analyze terminal history to learn setup preferences (opt-in)")
    parser.add_argument("--generate-script", action="store_true",
                        help="Generate personalized install script based on history")

    args = parser.parse_args()

    detector = EnvironmentDetector()
    profile = detector.detect_all()

    if args.json:
        print(profile.to_json())
    elif args.tools_only:
        print("Development Tools:")
        print("-" * 50)
        for name, tool in profile.tools.items():
            icon = "✅" if tool.installed else "❌"
            version = f" ({tool.version})" if tool.version else ""
            print(f"  {icon} {tool.name}{version}")
    elif args.extensions_only:
        print("VS Code Extensions:")
        print("-" * 50)
        for ext in profile.extensions:
            icon = "✅" if ext.installed else "❌"
            req = " (required)" if ext.required else ""
            print(f"  {icon} {ext.name}{req}")
    elif args.workspaces_only:
        print(f"Workspaces Found: {len(profile.workspaces)}")
        print("-" * 50)
        for ws in profile.workspaces:
            ctx = " [Context DNA]" if ws.has_context_dna else ""
            ptype = f" ({ws.project_type})" if ws.project_type else ""
            print(f"  📁 {ws.name}{ptype}{ctx}")
            print(f"     {ws.path}")
    elif args.install_plan:
        plan = profile.install_recommendations
        if not plan:
            print("✅ All required components installed!")
        else:
            print("Installation Plan:")
            print("-" * 50)
            for item in plan:
                priority = "🔴" if item["priority"] == "critical" else "🟡"
                print(f"  {priority} {item['name']} ({item['type']})")
                print(f"     $ {item['command']}")
    elif args.ready_check:
        if profile.ready_for_context_dna:
            print("✅ Environment is ready for Context DNA!")
        else:
            print("❌ Missing required components:")
            for item in profile.missing_critical:
                print(f"   - {item}")
    elif args.learn_from_history:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  📜 Terminal History Analysis (Opt-In)                       ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print()
        learner = TerminalHistoryLearner()
        insights = learner.learn_from_history()

        if insights.get("error"):
            print(f"  ⚠️  {insights['error']}")
        else:
            print(f"  📁 History file: {insights['history_file']}")
            print(f"  📊 Commands analyzed: {insights['commands_analyzed']}")
            print()
            print(f"  🛠️  Preferred package manager: {insights['preferred_package_manager'] or 'unknown'}")
            print(f"  🤖 Claude CLI detected: {'✅ Yes' if insights['claude_cli_detected'] else '❌ No'}")
            print()
            print(f"  📦 Installations found: {len(insights['installations_found'])}")
            for item in insights['installations_found'][:10]:
                print(f"      - {item['manager']}: {item['package']}")
            if len(insights['installations_found']) > 10:
                print(f"      ... and {len(insights['installations_found']) - 10} more")
            print()
            print(f"  🔌 VS Code extensions installed: {len(insights['vscode_extensions_installed'])}")
            for ext in insights['vscode_extensions_installed'][:5]:
                print(f"      - {ext}")
            print()
            if insights['repeated_commands']:
                print("  🔄 Repeated install commands (might indicate issues):")
                for cmd in insights['repeated_commands'][:3]:
                    print(f"      - {cmd[:60]}...")
        print()
        print("╚══════════════════════════════════════════════════════════════╝")
    elif args.generate_script:
        learner = TerminalHistoryLearner()
        insights = learner.learn_from_history()
        script = learner.generate_guided_install_script(insights)
        print(script)
    else:
        # Default: show summary
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  🔍 Environment Analysis                                     ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print()

        print("  Development Tools:")
        for name, tool in profile.tools.items():
            icon = "✅" if tool.installed else "❌"
            version = f" ({tool.version})" if tool.version else ""
            print(f"    {icon} {tool.name}{version}")

        print()
        print("  VS Code Extensions:")
        for ext in profile.extensions:
            icon = "✅" if ext.installed else "❌"
            req = " [required]" if ext.required else ""
            print(f"    {icon} {ext.name}{req}")

        print()
        print(f"  Workspaces: {profile.workspace_status} ({len(profile.workspaces)} found)")

        print()
        if profile.ready_for_context_dna:
            print("  ✅ Ready for Context DNA installation!")
        else:
            print("  ❌ Missing components:")
            for item in profile.missing_critical:
                print(f"     - {item}")

        # Show warnings if any
        if profile.warnings:
            print()
            print("  ⚠️  Warnings:")
            for warning in profile.warnings[:3]:  # Show max 3 warnings
                # Wrap long warnings
                if len(warning) > 60:
                    print(f"     - {warning[:60]}...")
                else:
                    print(f"     - {warning}")

        # Show quick start command
        if profile.quick_start_command:
            print()
            print("  🚀 Quick Start:")
            print(f"     $ {profile.quick_start_command}")

        print()
        print("╚══════════════════════════════════════════════════════════════╝")
