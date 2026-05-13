#!/usr/bin/env python3
"""
IDE DETECTION - Multi-IDE Workspace Recognition for Context DNA

Detects the active IDE and extracts workspace/file context from multiple
development environments. This enables webhook injections to work across:

SUPPORTED IDEs:
- VS Code (VSCODE_*, TERM_PROGRAM)
- Cursor (CURSOR_*)
- Windsurf (WINDSURF_*)
- JetBrains (IntelliJ, PyCharm, WebStorm, etc.)
- Neovim (NVIM_*)
- Vim (VIM, VIMINIT)
- Emacs (EMACS, INSIDE_EMACS)
- Sublime Text (SUBLIME_*)
- Zed (ZED_*)
- Generic terminal (fallback)

ARCHITECTURE:
┌─────────────────────────────────────────────────────────────────────────┐
│                        IDE DETECTION FLOW                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Environment Variables                                                   │
│  ├── CURSOR_FILE_PATH ───────────────► Cursor IDE detected              │
│  ├── VSCODE_FILE_PATH ───────────────► VS Code detected                 │
│  ├── WINDSURF_FILE_PATH ─────────────► Windsurf detected                │
│  ├── JETBRAINS_IDE ──────────────────► JetBrains detected               │
│  ├── NVIM_LISTEN_ADDRESS ────────────► Neovim detected                  │
│  ├── EMACS / INSIDE_EMACS ───────────► Emacs detected                   │
│  └── (multiple checks per IDE)                                          │
│                  │                                                       │
│                  ▼                                                       │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                    IDE CONTEXT EXTRACTION                         │   │
│  │  • active_file_path    (from IDE-specific env vars)              │   │
│  │  • workspace_folder    (project root)                             │   │
│  │  • project_name        (inferred or from IDE)                     │   │
│  │  • ide_name            (for analytics)                            │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                  │                                                       │
│                  ▼                                                       │
│           BoundaryContext.active_file_path                              │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘

Usage:
    from memory.ide_detection import get_ide_context, detect_active_ide

    # Get full IDE context
    ctx = get_ide_context()
    print(ctx.ide_name)            # "cursor", "vscode", "jetbrains", etc.
    print(ctx.active_file_path)    # /path/to/current/file.py
    print(ctx.workspace_folder)    # /path/to/project

    # Quick detection
    ide = detect_active_ide()      # "cursor" | "vscode" | "terminal" | ...
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from pathlib import Path
from enum import Enum

logger = logging.getLogger('contextdna.ide_detection')


# =============================================================================
# SUPPORTED IDEs
# =============================================================================

class IDE(Enum):
    """Supported IDEs for Context DNA integration."""
    CURSOR = "cursor"
    VSCODE = "vscode"
    WINDSURF = "windsurf"
    JETBRAINS = "jetbrains"
    NEOVIM = "neovim"
    VIM = "vim"
    EMACS = "emacs"
    SUBLIME = "sublime"
    ZED = "zed"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


# =============================================================================
# IDE DETECTION PATTERNS
# =============================================================================

# Environment variable patterns for each IDE
# Priority order: First match wins
IDE_ENV_PATTERNS = {
    IDE.CURSOR: [
        "CURSOR_FILE_PATH",
        "CURSOR_WORKSPACE_FOLDER",
        "CURSOR_PID",
    ],
    IDE.WINDSURF: [
        "WINDSURF_FILE_PATH",
        "WINDSURF_WORKSPACE_FOLDER",
        "WINDSURF_PID",
    ],
    IDE.VSCODE: [
        "VSCODE_FILE_PATH",
        "VSCODE_WORKSPACE_FOLDER",
        "VSCODE_PID",
        "VSCODE_GIT_IPC_HANDLE",
        "VSCODE_CLI",
        "VSCODE_CWD",
    ],
    IDE.JETBRAINS: [
        "JETBRAINS_IDE",
        "IDEA_INITIAL_DIRECTORY",
        "JB_JAVA_LAUNCH_IN_PROCESS",
        "__INTELLIJ_COMMAND_HISTFILE__",
        "TERMINAL_EMULATOR",  # JetBrains sets this to "JetBrains-JediTerm"
    ],
    IDE.NEOVIM: [
        "NVIM",
        "NVIM_LISTEN_ADDRESS",
        "NVIM_LOG_FILE",
    ],
    IDE.VIM: [
        "VIM",
        "VIMINIT",
        "VIMRUNTIME",
    ],
    IDE.EMACS: [
        "INSIDE_EMACS",
        "EMACS",
        "EMACS_SOCKET_NAME",
    ],
    IDE.SUBLIME: [
        "SUBLIME_PROJECT_PATH",
        "SUBLIME_FOLDER",
    ],
    IDE.ZED: [
        "ZED_WORKSPACE",
        "ZED_TERM",
    ],
}

# File path extraction for each IDE
IDE_FILE_PATH_VARS = {
    IDE.CURSOR: ["CURSOR_FILE_PATH", "CURSOR_CURRENT_FILE"],
    IDE.WINDSURF: ["WINDSURF_FILE_PATH", "WINDSURF_CURRENT_FILE"],
    IDE.VSCODE: ["VSCODE_FILE_PATH", "VSCODE_CURRENT_FILE"],
    IDE.JETBRAINS: ["IDEA_INITIAL_FILE", "JETBRAINS_FILE"],
    IDE.NEOVIM: ["NVIM_CURRENT_FILE"],
    IDE.SUBLIME: ["SUBLIME_FILE_PATH"],
    IDE.ZED: ["ZED_FILE_PATH"],
}

# Workspace folder extraction for each IDE
IDE_WORKSPACE_VARS = {
    IDE.CURSOR: ["CURSOR_WORKSPACE_FOLDER", "CURSOR_PROJECT_ROOT"],
    IDE.WINDSURF: ["WINDSURF_WORKSPACE_FOLDER", "WINDSURF_PROJECT_ROOT"],
    IDE.VSCODE: ["VSCODE_WORKSPACE_FOLDER", "VSCODE_CWD"],
    IDE.JETBRAINS: ["IDEA_INITIAL_DIRECTORY", "JETBRAINS_PROJECT_ROOT"],
    IDE.NEOVIM: ["NVIM_PROJECT_ROOT"],
    IDE.SUBLIME: ["SUBLIME_PROJECT_PATH", "SUBLIME_FOLDER"],
    IDE.ZED: ["ZED_WORKSPACE"],
}


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class IDEContext:
    """
    Context extracted from the active IDE.

    This standardizes IDE-specific information into a common format
    for use by the boundary intelligence system.
    """
    ide: IDE = IDE.UNKNOWN
    ide_name: str = "unknown"

    # File context
    active_file_path: Optional[str] = None
    workspace_folder: Optional[str] = None
    project_name: Optional[str] = None

    # Additional context
    shell: Optional[str] = None
    terminal_emulator: Optional[str] = None

    # Detection metadata
    detection_method: str = "unknown"
    confidence: float = 0.0
    env_vars_found: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "ide": self.ide.value,
            "ide_name": self.ide_name,
            "active_file_path": self.active_file_path,
            "workspace_folder": self.workspace_folder,
            "project_name": self.project_name,
            "shell": self.shell,
            "terminal_emulator": self.terminal_emulator,
            "detection_method": self.detection_method,
            "confidence": self.confidence,
            "env_vars_found": self.env_vars_found,
        }


# =============================================================================
# IDE DETECTION FUNCTIONS
# =============================================================================

def detect_active_ide() -> IDE:
    """
    Detect which IDE is currently active based on environment variables.

    Returns the most likely IDE, with priority given to more specific indicators.

    Returns:
        IDE enum value representing the detected IDE
    """
    # Check in priority order (most specific first)
    ide_priority = [
        IDE.CURSOR,     # Cursor-specific vars take priority
        IDE.WINDSURF,   # Windsurf next
        IDE.VSCODE,     # VS Code (many tools use VS Code extensions)
        IDE.JETBRAINS,  # JetBrains IDEs
        IDE.NEOVIM,     # Neovim before Vim
        IDE.VIM,        # Vim
        IDE.EMACS,      # Emacs
        IDE.SUBLIME,    # Sublime Text
        IDE.ZED,        # Zed
    ]

    for ide in ide_priority:
        env_vars = IDE_ENV_PATTERNS.get(ide, [])
        for var in env_vars:
            value = os.environ.get(var)
            if value:
                # Special check for JetBrains terminal emulator
                if var == "TERMINAL_EMULATOR" and "JetBrains" not in value:
                    continue
                logger.debug(f"Detected {ide.value} via {var}={value}")
                return ide

    # Check TERM_PROGRAM for additional hints
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if "cursor" in term_program:
        return IDE.CURSOR
    elif "vscode" in term_program or "code" in term_program:
        return IDE.VSCODE
    elif "windsurf" in term_program:
        return IDE.WINDSURF

    # Fallback to terminal
    return IDE.TERMINAL


def get_ide_context() -> IDEContext:
    """
    Get full context from the active IDE.

    Extracts file path, workspace, and project information from
    IDE-specific environment variables.

    Returns:
        IDEContext with all available information
    """
    ide = detect_active_ide()
    ctx = IDEContext(ide=ide, ide_name=ide.value)

    env_vars_found = []

    # Get active file path
    file_path_vars = IDE_FILE_PATH_VARS.get(ide, [])
    for var in file_path_vars:
        value = os.environ.get(var)
        if value and os.path.exists(value):
            ctx.active_file_path = value
            env_vars_found.append(var)
            break

    # Get workspace folder
    workspace_vars = IDE_WORKSPACE_VARS.get(ide, [])
    for var in workspace_vars:
        value = os.environ.get(var)
        if value and os.path.isdir(value):
            ctx.workspace_folder = value
            env_vars_found.append(var)
            break

    # Infer project name from workspace
    if ctx.workspace_folder:
        ctx.project_name = Path(ctx.workspace_folder).name
    elif ctx.active_file_path:
        # Try to find git root or fallback to parent directory
        ctx.project_name = _infer_project_from_file(ctx.active_file_path)

    # Get shell info
    ctx.shell = os.environ.get("SHELL")
    ctx.terminal_emulator = os.environ.get("TERMINAL_EMULATOR")

    # Set detection metadata
    ctx.env_vars_found = env_vars_found
    ctx.detection_method = "env_vars" if env_vars_found else "inference"
    ctx.confidence = min(1.0, len(env_vars_found) * 0.3 + 0.4)  # 0.4-1.0 based on vars found

    return ctx


def get_active_file_path() -> Optional[str]:
    """
    Get the currently active file path from any supported IDE.

    Convenience function that checks all supported IDEs and returns
    the first valid file path found.

    Returns:
        Active file path or None if not detectable
    """
    # Priority order for file path extraction
    file_path_priority = [
        # Cursor
        "CURSOR_FILE_PATH",
        "CURSOR_CURRENT_FILE",
        # Windsurf
        "WINDSURF_FILE_PATH",
        "WINDSURF_CURRENT_FILE",
        # VS Code
        "VSCODE_FILE_PATH",
        "VSCODE_CURRENT_FILE",
        # JetBrains
        "IDEA_INITIAL_FILE",
        "JETBRAINS_FILE",
        # Neovim
        "NVIM_CURRENT_FILE",
        # Sublime
        "SUBLIME_FILE_PATH",
        # Zed
        "ZED_FILE_PATH",
    ]

    for var in file_path_priority:
        value = os.environ.get(var)
        if value and os.path.exists(value):
            return value

    return None


def get_workspace_folder() -> Optional[str]:
    """
    Get the current workspace/project folder from any supported IDE.

    Returns:
        Workspace folder path or None if not detectable
    """
    workspace_priority = [
        # Cursor
        "CURSOR_WORKSPACE_FOLDER",
        "CURSOR_PROJECT_ROOT",
        # Windsurf
        "WINDSURF_WORKSPACE_FOLDER",
        "WINDSURF_PROJECT_ROOT",
        # VS Code
        "VSCODE_WORKSPACE_FOLDER",
        "VSCODE_CWD",
        # JetBrains
        "IDEA_INITIAL_DIRECTORY",
        "JETBRAINS_PROJECT_ROOT",
        # Neovim
        "NVIM_PROJECT_ROOT",
        # Sublime
        "SUBLIME_PROJECT_PATH",
        "SUBLIME_FOLDER",
        # Zed
        "ZED_WORKSPACE",
    ]

    for var in workspace_priority:
        value = os.environ.get(var)
        if value and os.path.isdir(value):
            return value

    # Fallback: try to find git root from current directory
    cwd = os.getcwd()
    git_root = _find_git_root(cwd)
    if git_root:
        return git_root

    return cwd


def _infer_project_from_file(file_path: str) -> Optional[str]:
    """Infer project name from a file path by finding git root."""
    git_root = _find_git_root(file_path)
    if git_root:
        return Path(git_root).name

    # Fallback to directory name
    return Path(file_path).parent.name


def _find_git_root(path: str) -> Optional[str]:
    """Find the git repository root from a given path."""
    current = Path(path)
    if current.is_file():
        current = current.parent

    while current != current.parent:
        if (current / ".git").exists():
            return str(current)
        current = current.parent

    return None


# =============================================================================
# IDE CONFIGURATION FOR WEBHOOK INTEGRATION
# =============================================================================

def get_webhook_env_setup(ide: IDE) -> Dict[str, str]:
    """
    Get environment variable setup instructions for a specific IDE.

    Returns instructions for setting up Context DNA webhook integration
    with the specified IDE.

    Args:
        ide: The IDE to get setup instructions for

    Returns:
        Dict with variable names and descriptions
    """
    common_vars = {
        "CONTEXT_DNA_DIALOGUE": "Full conversation history (optional)",
        "CONTEXT_DNA_SESSION_ID": "Session identifier for recency tracking",
    }

    ide_specific = {
        IDE.CURSOR: {
            "CURSOR_FILE_PATH": "Path to the currently active file",
            "CURSOR_WORKSPACE_FOLDER": "Root of the workspace/project",
        },
        IDE.VSCODE: {
            "VSCODE_FILE_PATH": "Path to the currently active file",
            "VSCODE_WORKSPACE_FOLDER": "Root of the workspace/project",
        },
        IDE.WINDSURF: {
            "WINDSURF_FILE_PATH": "Path to the currently active file",
            "WINDSURF_WORKSPACE_FOLDER": "Root of the workspace/project",
        },
        IDE.JETBRAINS: {
            "JETBRAINS_FILE": "Path to the currently active file",
            "IDEA_INITIAL_DIRECTORY": "Root of the project",
        },
        IDE.NEOVIM: {
            "NVIM_CURRENT_FILE": "Path to the currently active file (set via autocmd)",
            "NVIM_PROJECT_ROOT": "Root of the project (set via autocmd)",
        },
        IDE.SUBLIME: {
            "SUBLIME_FILE_PATH": "Path to the currently active file",
            "SUBLIME_PROJECT_PATH": "Root of the project",
        },
        IDE.ZED: {
            "ZED_FILE_PATH": "Path to the currently active file",
            "ZED_WORKSPACE": "Root of the workspace",
        },
    }

    result = {**common_vars, **ide_specific.get(ide, {})}
    return result


def generate_ide_integration_snippet(ide: IDE) -> str:
    """
    Generate a code snippet for integrating Context DNA with an IDE.

    Args:
        ide: The IDE to generate integration code for

    Returns:
        Code snippet as a string
    """
    snippets = {
        IDE.VSCODE: '''
// VS Code settings.json - Add to terminal.integrated.env.*
{
  "terminal.integrated.env.osx": {
    "VSCODE_FILE_PATH": "${file}",
    "VSCODE_WORKSPACE_FOLDER": "${workspaceFolder}"
  },
  "terminal.integrated.env.linux": {
    "VSCODE_FILE_PATH": "${file}",
    "VSCODE_WORKSPACE_FOLDER": "${workspaceFolder}"
  },
  "terminal.integrated.env.windows": {
    "VSCODE_FILE_PATH": "${file}",
    "VSCODE_WORKSPACE_FOLDER": "${workspaceFolder}"
  }
}
''',
        IDE.CURSOR: '''
// Cursor settings.json - Add to terminal.integrated.env.*
{
  "terminal.integrated.env.osx": {
    "CURSOR_FILE_PATH": "${file}",
    "CURSOR_WORKSPACE_FOLDER": "${workspaceFolder}"
  },
  "terminal.integrated.env.linux": {
    "CURSOR_FILE_PATH": "${file}",
    "CURSOR_WORKSPACE_FOLDER": "${workspaceFolder}"
  },
  "terminal.integrated.env.windows": {
    "CURSOR_FILE_PATH": "${file}",
    "CURSOR_WORKSPACE_FOLDER": "${workspaceFolder}"
  }
}
''',
        IDE.NEOVIM: '''
-- Neovim init.lua - Add autocmds to set environment variables
vim.api.nvim_create_autocmd({"BufEnter", "BufWinEnter"}, {
  callback = function()
    vim.fn.setenv("NVIM_CURRENT_FILE", vim.fn.expand("%:p"))
    -- Try to find project root (git root or cwd)
    local git_root = vim.fn.system("git rev-parse --show-toplevel 2>/dev/null"):gsub("\\n", "")
    if vim.v.shell_error == 0 then
      vim.fn.setenv("NVIM_PROJECT_ROOT", git_root)
    else
      vim.fn.setenv("NVIM_PROJECT_ROOT", vim.fn.getcwd())
    end
  end
})
''',
        IDE.EMACS: '''
;; Emacs init.el - Set environment variables for Context DNA
(add-hook 'find-file-hook
  (lambda ()
    (setenv "EMACS_CURRENT_FILE" (buffer-file-name))
    (setenv "EMACS_PROJECT_ROOT"
      (or (projectile-project-root)
          (vc-root-dir)
          default-directory))))
''',
        IDE.JETBRAINS: '''
// JetBrains IDE - Configure via Settings > Tools > Terminal
// Add to "Environment variables" field:
// JETBRAINS_FILE=${FilePath};IDEA_INITIAL_DIRECTORY=${ProjectFileDir}
//
// Or use the .idea/terminal.xml file:
<component name="TerminalOptionsProvider">
  <option name="envDataOptions">
    <option name="JETBRAINS_FILE" value="${FilePath}" />
    <option name="IDEA_INITIAL_DIRECTORY" value="${ProjectFileDir}" />
  </option>
</component>
''',
    }

    return snippets.get(ide, f"# No specific integration snippet for {ide.value}")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("IDE Detection for Context DNA")
        print("=" * 50)

        ctx = get_ide_context()
        print(f"Detected IDE: {ctx.ide_name}")
        print(f"Active File: {ctx.active_file_path or 'N/A'}")
        print(f"Workspace: {ctx.workspace_folder or 'N/A'}")
        print(f"Project: {ctx.project_name or 'N/A'}")
        print(f"Confidence: {ctx.confidence:.0%}")
        print(f"Env Vars Found: {', '.join(ctx.env_vars_found) or 'None'}")
        print()
        print("Commands:")
        print("  python ide_detection.py detect     - Detect active IDE")
        print("  python ide_detection.py context    - Full context JSON")
        print("  python ide_detection.py setup IDE  - Integration snippet for IDE")
        print()
        print("Supported IDEs: cursor, vscode, windsurf, jetbrains, neovim, vim, emacs, sublime, zed")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "detect":
        ide = detect_active_ide()
        print(ide.value)

    elif cmd == "context":
        ctx = get_ide_context()
        print(json.dumps(ctx.to_dict(), indent=2))

    elif cmd == "setup":
        if len(sys.argv) < 3:
            print("Usage: python ide_detection.py setup <IDE>")
            sys.exit(1)

        ide_name = sys.argv[2].lower()
        try:
            ide = IDE(ide_name)
        except ValueError:
            print(f"Unknown IDE: {ide_name}")
            print(f"Supported: {', '.join(i.value for i in IDE)}")
            sys.exit(1)

        print(f"# Integration snippet for {ide.value}")
        print(generate_ide_integration_snippet(ide))
        print()
        print("# Environment variables to set:")
        for var, desc in get_webhook_env_setup(ide).items():
            print(f"#   {var}: {desc}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
