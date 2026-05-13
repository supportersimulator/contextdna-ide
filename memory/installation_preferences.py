"""
Installation Preferences for Context DNA Vibe Coder Launch

Defines required vs optional components and Aaron's preferred baseline setup.
User can customize these preferences while ensuring required components are installed.

Created: January 29, 2026
Part of: Vibe Coder Launch Install Initiative
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from enum import Enum
from pathlib import Path


class InstallPriority(str, Enum):
    """Installation priority levels."""
    REQUIRED = "required"          # Must install - Context DNA won't work without it
    RECOMMENDED = "recommended"    # Aaron's preferred setup - emphasized as baseline
    OPTIONAL = "optional"          # User can choose - nice to have


class ComponentCategory(str, Enum):
    """Categories of installable components."""
    CORE_TOOLS = "core_tools"      # Python, Git - absolute requirements
    IDE = "ide"                     # VS Code, Cursor - need at least one
    EXTENSIONS = "extensions"       # Claude Code, Python extension
    INFRASTRUCTURE = "infrastructure"  # Docker, xbar
    LLM_BACKEND = "llm_backend"    # MLX, Ollama


@dataclass
class InstallableComponent:
    """
    A component that can be installed during vibe coder setup.

    Attributes:
        id: Unique identifier (e.g., "python", "vscode", "docker")
        name: Human-readable name
        description: What this component does and why it's needed
        category: Which category this belongs to
        priority: Required, recommended, or optional
        install_command: Platform-specific install command
        detection_key: Key in EnvironmentDetector.tools
        requires: List of component IDs this depends on
        conflicts_with: List of component IDs that conflict
        notes: Additional context for the user
    """
    id: str
    name: str
    description: str
    category: ComponentCategory
    priority: InstallPriority
    install_commands: Dict[str, str]  # platform -> command
    detection_key: Optional[str] = None  # Key in environment_detector tools
    requires: List[str] = field(default_factory=list)
    conflicts_with: List[str] = field(default_factory=list)
    notes: Optional[str] = None
    install_time_estimate: Optional[str] = None  # e.g., "2-5 minutes"

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "category": self.category.value,
            "priority": self.priority.value,
        }


@dataclass
class UserInstallPreferences:
    """
    User's selected installation preferences.

    Tracks which components the user wants to install,
    allowing customization while ensuring required components are included.
    """
    # Components user explicitly enabled
    enabled_components: List[str] = field(default_factory=list)

    # Components user explicitly disabled (only optional ones)
    disabled_components: List[str] = field(default_factory=list)

    # Custom install paths (if user wants non-default locations)
    custom_paths: Dict[str, str] = field(default_factory=dict)

    # Preferred IDE (vscode or cursor)
    preferred_ide: str = "vscode"

    # Preferred LLM backend (mlx or ollama)
    preferred_llm_backend: str = "mlx"  # MLX for Apple Silicon, Ollama for others

    # Whether to use Aaron's recommended baseline
    use_baseline: bool = True

    # Skip certain prompts (for advanced users)
    skip_confirmations: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserInstallPreferences':
        return cls(**data)


# =============================================================================
# COMPONENT DEFINITIONS
# =============================================================================

# Aaron's Preferred Baseline Setup (emphasized in UI)
AARONS_BASELINE = {
    "ide": "vscode",
    "llm_backend": "mlx",  # Apple Silicon native for best performance
    "extensions": ["anthropic.claude-code", "ms-python.python"],
    "infrastructure": ["docker", "xbar"],  # xbar for menubar status
}

# All installable components
INSTALL_COMPONENTS: List[InstallableComponent] = [
    # ==========================================================================
    # CORE TOOLS (Required)
    # ==========================================================================
    InstallableComponent(
        id="python",
        name="Python 3.10+",
        description="Required for Context DNA's learning engine and automation scripts. "
                   "The brain of the system that processes and stores learnings.",
        category=ComponentCategory.CORE_TOOLS,
        priority=InstallPriority.REQUIRED,
        detection_key="python",
        install_commands={
            "Darwin": "brew install python@3.12",
            "Windows": "winget install Python.Python.3.12",
            "Linux": "sudo apt install python3 python3-pip",
        },
        notes="Python 3.10 or higher required. We recommend 3.12 for best performance.",
        install_time_estimate="1-2 minutes",
    ),
    InstallableComponent(
        id="git",
        name="Git",
        description="Required for version control and tracking your coding journey. "
                   "Context DNA uses git hooks to capture learnings automatically.",
        category=ComponentCategory.CORE_TOOLS,
        priority=InstallPriority.REQUIRED,
        detection_key="git",
        install_commands={
            "Darwin": "xcode-select --install",
            "Windows": "winget install Git.Git",
            "Linux": "sudo apt install git",
        },
        notes="Git is essential for Context DNA's learning capture system.",
        install_time_estimate="1-3 minutes",
    ),
    InstallableComponent(
        id="node",
        name="Node.js",
        description="Required for Claude Code CLI installation and running Context DNA's "
                   "dashboard interface.",
        category=ComponentCategory.CORE_TOOLS,
        priority=InstallPriority.REQUIRED,
        detection_key="node",
        install_commands={
            "Darwin": "brew install node",
            "Windows": "winget install OpenJS.NodeJS.LTS",
            "Linux": "sudo apt install nodejs npm",
        },
        notes="Node.js LTS version recommended for stability.",
        install_time_estimate="1-2 minutes",
    ),

    # ==========================================================================
    # IDE (Need at least one)
    # ==========================================================================
    InstallableComponent(
        id="vscode",
        name="VS Code",
        description="Aaron's recommended IDE for Context DNA. Full Claude Code integration, "
                   "extensive extension ecosystem, and excellent debugging tools.",
        category=ComponentCategory.IDE,
        priority=InstallPriority.RECOMMENDED,  # Aaron's preference
        detection_key="vscode",
        install_commands={
            "Darwin": "brew install --cask visual-studio-code",
            "Windows": "winget install Microsoft.VisualStudioCode",
            "Linux": "sudo snap install code --classic",
        },
        notes="✨ RECOMMENDED: Aaron's preferred IDE with best Context DNA integration.",
        install_time_estimate="2-5 minutes",
    ),
    InstallableComponent(
        id="cursor",
        name="Cursor",
        description="AI-native IDE built on VS Code. Great alternative if you prefer "
                   "built-in AI features. Supports Claude Code extension.",
        category=ComponentCategory.IDE,
        priority=InstallPriority.OPTIONAL,
        detection_key="cursor",
        install_commands={
            "Darwin": "brew install --cask cursor",
            "Windows": "Download from cursor.com",
            "Linux": "Download from cursor.com",
        },
        notes="Good alternative to VS Code with native AI features.",
        install_time_estimate="2-5 minutes",
    ),

    # ==========================================================================
    # EXTENSIONS (Recommended)
    # ==========================================================================
    InstallableComponent(
        id="claude_code_cli",
        name="Claude Code CLI",
        description="Command-line interface for Claude Code. Powers Context DNA's webhook "
                   "injection system that provides contextual guidance in every prompt.",
        category=ComponentCategory.EXTENSIONS,
        priority=InstallPriority.REQUIRED,
        detection_key="claude_code",
        requires=["node"],
        install_commands={
            "Darwin": "npm install -g @anthropic-ai/claude-code",
            "Windows": "npm install -g @anthropic-ai/claude-code",
            "Linux": "npm install -g @anthropic-ai/claude-code",
        },
        notes="The heart of Context DNA's intelligent context injection.",
        install_time_estimate="1-2 minutes",
    ),
    InstallableComponent(
        id="claude_code_ext",
        name="Claude Code VS Code Extension",
        description="VS Code extension for Claude Code. Provides integrated AI assistance "
                   "directly in your editor with full Context DNA support.",
        category=ComponentCategory.EXTENSIONS,
        priority=InstallPriority.RECOMMENDED,
        requires=["vscode"],
        install_commands={
            "Darwin": "code --install-extension anthropic.claude-code",
            "Windows": "code --install-extension anthropic.claude-code",
            "Linux": "code --install-extension anthropic.claude-code",
        },
        notes="✨ RECOMMENDED: Best experience for beginners - AI assistance right in VS Code.",
        install_time_estimate="1 minute",
    ),
    InstallableComponent(
        id="python_ext",
        name="Python VS Code Extension",
        description="Microsoft's Python extension for VS Code. Provides IntelliSense, "
                   "debugging, and code navigation for Python projects.",
        category=ComponentCategory.EXTENSIONS,
        priority=InstallPriority.RECOMMENDED,
        requires=["vscode", "python"],
        install_commands={
            "Darwin": "code --install-extension ms-python.python",
            "Windows": "code --install-extension ms-python.python",
            "Linux": "code --install-extension ms-python.python",
        },
        notes="Essential for Python development in VS Code.",
        install_time_estimate="1 minute",
    ),

    # ==========================================================================
    # INFRASTRUCTURE (Optional but recommended)
    # ==========================================================================
    InstallableComponent(
        id="docker",
        name="Docker",
        description="Container platform for running Context DNA's distributed services "
                   "(PostgreSQL, Redis, OpenSearch). Enables advanced features like "
                   "team learning sharing and scalable infrastructure.",
        category=ComponentCategory.INFRASTRUCTURE,
        priority=InstallPriority.RECOMMENDED,
        detection_key="docker",
        install_commands={
            "Darwin": "brew install --cask docker",
            "Windows": "winget install Docker.DockerDesktop",
            "Linux": "sudo apt install docker.io && sudo systemctl enable docker",
        },
        notes="✨ RECOMMENDED: Enables full Context DNA feature set. "
              "Without Docker, Context DNA runs in local-only mode.",
        install_time_estimate="5-10 minutes",
    ),
    InstallableComponent(
        id="xbar",
        name="xbar (macOS only)",
        description="Menubar app that shows Context DNA status at a glance. "
                   "See learning captures, injection status, and system health "
                   "without opening the dashboard.",
        category=ComponentCategory.INFRASTRUCTURE,
        priority=InstallPriority.RECOMMENDED,
        detection_key=None,  # Custom detection
        install_commands={
            "Darwin": "brew install --cask xbar",
            "Windows": "",  # Not available
            "Linux": "",  # Not available
        },
        notes="✨ RECOMMENDED for macOS: Convenient status monitoring in menubar.",
        install_time_estimate="1-2 minutes",
    ),
    InstallableComponent(
        id="homebrew",
        name="Homebrew (macOS)",
        description="The missing package manager for macOS. Makes installing other "
                   "tools much easier. Used by install script for other components.",
        category=ComponentCategory.INFRASTRUCTURE,
        priority=InstallPriority.RECOMMENDED,
        detection_key="homebrew",
        install_commands={
            "Darwin": '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
            "Windows": "",
            "Linux": "",
        },
        notes="Essential for macOS - simplifies installation of all other tools.",
        install_time_estimate="2-5 minutes",
    ),

    # ==========================================================================
    # LLM BACKEND (Choose one)
    # ==========================================================================
    InstallableComponent(
        id="mlx",
        name="MLX (Apple Silicon)",
        description="Apple's native machine learning framework. Provides fastest local "
                   "LLM inference on M1/M2/M3/M4/M5 Macs using Metal GPU acceleration.",
        category=ComponentCategory.LLM_BACKEND,
        priority=InstallPriority.RECOMMENDED,  # Recommended for Apple Silicon
        requires=["python"],
        conflicts_with=[],  # Can coexist with Ollama
        install_commands={
            "Darwin": "pip3 install mlx mlx-lm",
            "Windows": "",  # Not available
            "Linux": "",  # Not available
        },
        notes="✨ RECOMMENDED for Apple Silicon Macs: Fastest local LLM inference.",
        install_time_estimate="2-5 minutes",
    ),
    InstallableComponent(
        id="ollama",
        name="Ollama",
        description="Easy-to-use local LLM runner. Works on all platforms and provides "
                   "good performance for Context DNA's analysis features.",
        category=ComponentCategory.LLM_BACKEND,
        priority=InstallPriority.OPTIONAL,  # Fallback for non-Apple Silicon
        install_commands={
            "Darwin": "brew install ollama",
            "Windows": "winget install Ollama.Ollama",
            "Linux": "curl -fsSL https://ollama.com/install.sh | sh",
        },
        notes="Good fallback option for non-Apple Silicon systems or if MLX unavailable.",
        install_time_estimate="2-5 minutes",
    ),
]


def get_component_by_id(component_id: str) -> Optional[InstallableComponent]:
    """Get a component by its ID."""
    for component in INSTALL_COMPONENTS:
        if component.id == component_id:
            return component
    return None


def get_required_components() -> List[InstallableComponent]:
    """Get all required components (must install)."""
    return [c for c in INSTALL_COMPONENTS if c.priority == InstallPriority.REQUIRED]


def get_recommended_components() -> List[InstallableComponent]:
    """Get all recommended components (Aaron's baseline)."""
    return [c for c in INSTALL_COMPONENTS if c.priority == InstallPriority.RECOMMENDED]


def get_optional_components() -> List[InstallableComponent]:
    """Get all optional components (user choice)."""
    return [c for c in INSTALL_COMPONENTS if c.priority == InstallPriority.OPTIONAL]


def get_components_by_category(category: ComponentCategory) -> List[InstallableComponent]:
    """Get all components in a category."""
    return [c for c in INSTALL_COMPONENTS if c.category == category]


def get_aarons_baseline_ids() -> List[str]:
    """Get IDs of all components in Aaron's recommended baseline."""
    baseline_ids = []

    # Required components
    for c in get_required_components():
        baseline_ids.append(c.id)

    # Aaron's specific choices
    baseline_ids.append(AARONS_BASELINE["ide"])  # vscode
    baseline_ids.extend(["claude_code_ext", "python_ext"])  # extensions
    baseline_ids.extend(AARONS_BASELINE["infrastructure"])  # docker, xbar
    baseline_ids.append(AARONS_BASELINE["llm_backend"])  # mlx

    return list(set(baseline_ids))


def generate_install_plan(
    preferences: UserInstallPreferences,
    detected_environment: Dict[str, Any],
    platform: str = "Darwin"
) -> Dict[str, Any]:
    """
    Generate a complete installation plan based on user preferences and detected environment.

    Args:
        preferences: User's installation preferences
        detected_environment: Output from EnvironmentDetector.detect_all()
        platform: Current platform (Darwin, Windows, Linux)

    Returns:
        Installation plan with steps, commands, and estimates
    """
    plan = {
        "platform": platform,
        "steps": [],
        "total_components": 0,
        "already_installed": 0,
        "to_install": 0,
        "estimated_time": "0 minutes",
    }

    tools = detected_environment.get("tools", {})

    # Determine which components to install
    if preferences.use_baseline:
        target_ids = get_aarons_baseline_ids()
    else:
        target_ids = list(set(preferences.enabled_components))

    # Remove explicitly disabled components (only optional ones)
    for disabled in preferences.disabled_components:
        component = get_component_by_id(disabled)
        if component and component.priority != InstallPriority.REQUIRED:
            target_ids = [id for id in target_ids if id != disabled]

    # Handle IDE choice
    if preferences.preferred_ide == "cursor":
        target_ids = [id for id in target_ids if id != "vscode"]
        if "cursor" not in target_ids:
            target_ids.append("cursor")
        # Adjust extension requirements
        target_ids = [id for id in target_ids if id not in ["claude_code_ext", "python_ext"]]

    # Handle LLM backend choice
    if preferences.preferred_llm_backend == "ollama":
        target_ids = [id for id in target_ids if id != "mlx"]
        if "ollama" not in target_ids:
            target_ids.append("ollama")

    # Platform-specific filtering
    if platform != "Darwin":
        # Remove macOS-only components
        target_ids = [id for id in target_ids if id not in ["xbar", "homebrew", "mlx"]]

    # Build installation steps
    total_time_min = 0

    for component_id in target_ids:
        component = get_component_by_id(component_id)
        if not component:
            continue

        # Check if already installed
        is_installed = False
        if component.detection_key and component.detection_key in tools:
            tool_status = tools[component.detection_key]
            if isinstance(tool_status, dict):
                is_installed = tool_status.get("installed", False)
            else:
                is_installed = getattr(tool_status, "installed", False)

        # Get install command for platform
        install_cmd = component.install_commands.get(platform, "")

        step = {
            "id": component.id,
            "name": component.name,
            "description": component.description,
            "category": component.category.value,
            "priority": component.priority.value,
            "is_installed": is_installed,
            "install_command": install_cmd,
            "notes": component.notes,
            "requires": component.requires,
            "is_required": component.priority == InstallPriority.REQUIRED,
            "is_recommended": component.priority == InstallPriority.RECOMMENDED,
            "is_in_baseline": component.id in get_aarons_baseline_ids(),
        }

        plan["steps"].append(step)
        plan["total_components"] += 1

        if is_installed:
            plan["already_installed"] += 1
        elif install_cmd:
            plan["to_install"] += 1
            # Parse time estimate
            if component.install_time_estimate:
                try:
                    time_parts = component.install_time_estimate.split("-")
                    total_time_min += int(time_parts[-1].replace(" minutes", "").strip())
                except Exception:
                    total_time_min += 3  # Default estimate

    plan["estimated_time"] = f"{total_time_min}-{total_time_min + 5} minutes"

    return plan


def save_preferences(preferences: UserInstallPreferences, path: Optional[Path] = None) -> Path:
    """Save user preferences to file."""
    if path is None:
        path = Path.home() / ".context-dna" / "install_preferences.json"

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(preferences.to_dict(), f, indent=2)

    return path


def load_preferences(path: Optional[Path] = None) -> Optional[UserInstallPreferences]:
    """Load user preferences from file."""
    if path is None:
        path = Path.home() / ".context-dna" / "install_preferences.json"

    if not path.exists():
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
        return UserInstallPreferences.from_dict(data)
    except Exception:
        return None


# =============================================================================
# RUNTIME MODE PREFERENCES (Lite vs Heavy)
# =============================================================================

class RuntimeMode(str, Enum):
    """Runtime resource usage modes."""
    LITE = "lite"      # Low RAM - hash-based voice, subprocess LLM
    HEAVY = "heavy"    # High RAM - ML voice, cached LLM
    AUTO = "auto"      # Auto-detect based on system resources


# Thresholds for auto-detection
RAM_THRESHOLD_GB = 12  # Below this = lite mode
DISK_THRESHOLD_GB = 20  # Below this = warn about space


@dataclass
class RuntimeModePreferences:
    """
    Runtime mode preferences for resource usage.

    Lite mode: For machines with <12GB RAM
    - Voice auth uses hash-based embeddings (less secure but functional)
    - LLM runs via subprocess (slower, doesn't cache model)
    - Smaller STT model

    Heavy mode: For machines with 12GB+ RAM
    - Voice auth uses ML embeddings (resemblyzer/PyTorch)
    - LLM runs via API server (fast, cached model)
    - Full Whisper model
    """
    mode: str = "auto"  # lite, heavy, or auto
    effective_mode: Optional[str] = None  # Computed if mode=auto

    # Feature toggles (can be overridden regardless of mode)
    voice_auth_ml: bool = True        # Use ML-based voice embeddings
    llm_api_server: bool = True       # Use cached LLM server
    whisper_full: bool = True         # Use full Whisper model
    edge_tts: bool = True             # Use edge-tts
    background_health: bool = True    # Run background health checks

    # Resource limits
    max_memory_mb: Optional[int] = None
    llm_timeout_seconds: int = 120

    # Auto-detected system info
    detected_ram_gb: Optional[float] = None
    detected_disk_gb: Optional[float] = None
    detected_is_apple_silicon: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RuntimeModePreferences':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _detect_system_resources() -> Dict[str, Any]:
    """Detect system resources for auto mode."""
    import shutil
    import os
    import subprocess as sp

    resources = {
        "ram_gb": None,
        "disk_free_gb": None,
        "is_apple_silicon": False,
    }

    # RAM detection (try multiple methods)
    try:
        import psutil
        resources["ram_gb"] = round(psutil.virtual_memory().total / (1024**3), 1)
    except ImportError:
        # Fallback for macOS
        try:
            if os.uname().sysname == "Darwin":
                result = sp.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
                if result.returncode == 0:
                    resources["ram_gb"] = round(int(result.stdout.strip()) / (1024**3), 1)
        except Exception as e:
            print(f"[WARN] macOS RAM detection failed: {e}")

    # Disk space
    try:
        context_dna_dir = Path.home() / ".context-dna"
        if context_dna_dir.exists():
            total, used, free = shutil.disk_usage(context_dna_dir)
            resources["disk_free_gb"] = round(free / (1024**3), 1)
    except Exception as e:
        print(f"[WARN] Disk space detection failed: {e}")

    # Apple Silicon detection
    try:
        import platform
        if platform.system() == "Darwin" and platform.processor() == "arm":
            resources["is_apple_silicon"] = True
    except Exception as e:
        print(f"[WARN] Apple Silicon detection failed: {e}")

    return resources


def _auto_detect_runtime_mode() -> str:
    """Auto-detect appropriate runtime mode based on system resources."""
    resources = _detect_system_resources()

    # Apple Silicon with decent RAM = heavy mode
    if resources.get("is_apple_silicon") and (resources.get("ram_gb") or 0) >= RAM_THRESHOLD_GB:
        return "heavy"

    # High RAM = heavy mode
    if (resources.get("ram_gb") or 0) >= RAM_THRESHOLD_GB:
        return "heavy"

    # Otherwise default to lite
    return "lite"


RUNTIME_PREFERENCES_FILE = Path.home() / ".context-dna" / "runtime_mode.json"


def get_runtime_preferences() -> RuntimeModePreferences:
    """Load runtime mode preferences."""
    prefs = RuntimeModePreferences()

    # Load saved preferences
    if RUNTIME_PREFERENCES_FILE.exists():
        try:
            with open(RUNTIME_PREFERENCES_FILE, "r") as f:
                saved = json.load(f)
                prefs = RuntimeModePreferences.from_dict(saved)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[WARN] Runtime preferences file read failed: {e}")

    # Auto-detect system resources
    resources = _detect_system_resources()
    prefs.detected_ram_gb = resources.get("ram_gb")
    prefs.detected_disk_gb = resources.get("disk_free_gb")
    prefs.detected_is_apple_silicon = resources.get("is_apple_silicon", False)

    # Compute effective mode if auto
    if prefs.mode == "auto":
        prefs.effective_mode = _auto_detect_runtime_mode()
    else:
        prefs.effective_mode = prefs.mode

    # Apply mode defaults (unless explicitly overridden)
    if prefs.effective_mode == "lite":
        # Lite mode defaults
        prefs.voice_auth_ml = False
        prefs.llm_api_server = False
        prefs.whisper_full = False

    return prefs


def save_runtime_preferences(prefs: RuntimeModePreferences) -> bool:
    """Save runtime mode preferences."""
    try:
        RUNTIME_PREFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RUNTIME_PREFERENCES_FILE, "w") as f:
            json.dump(prefs.to_dict(), f, indent=2)
        return True
    except IOError:
        return False


def set_runtime_mode(mode: str) -> RuntimeModePreferences:
    """Set runtime mode (lite, heavy, or auto)."""
    if mode not in ["lite", "heavy", "auto"]:
        raise ValueError(f"Invalid mode: {mode}. Must be lite, heavy, or auto.")

    prefs = get_runtime_preferences()
    prefs.mode = mode

    # Apply mode defaults
    if mode == "lite":
        prefs.voice_auth_ml = False
        prefs.llm_api_server = False
        prefs.whisper_full = False
    elif mode == "heavy":
        prefs.voice_auth_ml = True
        prefs.llm_api_server = True
        prefs.whisper_full = True

    save_runtime_preferences(prefs)
    return get_runtime_preferences()


def is_runtime_feature_enabled(feature: str) -> bool:
    """Check if a runtime feature is enabled."""
    prefs = get_runtime_preferences()
    return getattr(prefs, feature, True)


def should_health_check_feature(feature: str) -> bool:
    """
    Check if health should report on this feature.

    Returns False if feature is intentionally disabled,
    so health check should NOT report it as a failure.
    """
    return is_runtime_feature_enabled(feature)


def get_effective_runtime_mode() -> str:
    """Get effective runtime mode (resolves 'auto')."""
    return get_runtime_preferences().effective_mode or "auto"


# =============================================================================
# CLI for testing
# =============================================================================

if __name__ == "__main__":
    import sys

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  🧬 Context DNA Installation Preferences                     ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print()

    print("  REQUIRED Components (Must Install):")
    print("  " + "-" * 50)
    for c in get_required_components():
        print(f"    🔴 {c.name}: {c.description[:50]}...")

    print()
    print("  RECOMMENDED Components (Aaron's Baseline):")
    print("  " + "-" * 50)
    for c in get_recommended_components():
        emoji = "✨" if c.id in get_aarons_baseline_ids() else "🟡"
        print(f"    {emoji} {c.name}: {c.description[:50]}...")

    print()
    print("  OPTIONAL Components (User Choice):")
    print("  " + "-" * 50)
    for c in get_optional_components():
        print(f"    🔵 {c.name}: {c.description[:50]}...")

    print()
    print("  Aaron's Full Baseline:")
    print("  " + "-" * 50)
    for component_id in get_aarons_baseline_ids():
        c = get_component_by_id(component_id)
        if c:
            print(f"    ✨ {c.name}")

    print()
    print("╚══════════════════════════════════════════════════════════════╝")
