#!/usr/bin/env python3
"""
WEBHOOK DESTINATION REGISTRY - Auto-Detection & Configuration Templates

Provides:
1. Auto-detection of IDE/app where text is being entered (via macOS accessibility)
2. Registry of known webhook destination configurations
3. Pre-hook and post-hook templates for each destination
4. Local LLM-assisted configuration troubleshooting
5. Computer fingerprinting for optimal LLM recommendations

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                WEBHOOK DESTINATION REGISTRY                      │
    │                                                                  │
    │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
    │  │ Auto-Detection   │  │ Config Templates │  │ LLM Helper   │  │
    │  │                  │  │                  │  │              │  │
    │  │ macOS Accessibi- │  │ Pre-hook configs │  │ Troubleshoot │  │
    │  │ lity APIs detect │  │ Post-hook configs│  │ configs via  │  │
    │  │ active app/field │  │ SQLite stored    │  │ local Qwen   │  │
    │  └──────────────────┘  └──────────────────┘  └──────────────┘  │
    │                                                                  │
    │  ┌──────────────────────────────────────────────────────────┐   │
    │  │              COMPUTER FINGERPRINT                         │   │
    │  │  RAM → LLM recommendation | GPU → MLX capability          │   │
    │  │  OS version → feature availability                        │   │
    │  └──────────────────────────────────────────────────────────┘   │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    from memory.webhook_destination_registry import (
        WebhookDestinationRegistry,
        detect_active_app,
        get_computer_fingerprint
    )

    registry = WebhookDestinationRegistry()

    # Auto-detect current app
    app_info = detect_active_app()
    # Returns: {"app_name": "Code", "bundle_id": "com.microsoft.VSCode", ...}

    # Check if we have a config for this app
    config = registry.get_config_for_app(app_info["bundle_id"])

    # Get computer fingerprint for LLM recommendation
    fingerprint = get_computer_fingerprint()
    # Returns: {"ram_gb": 16, "recommended_llm": "qwen2.5-coder:7b", ...}
"""

import json
import logging
import subprocess
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Literal
from dataclasses import dataclass, asdict, field
from enum import Enum

logger = logging.getLogger(__name__)

# Database path
MEMORY_DIR = Path(__file__).parent
REGISTRY_DB = MEMORY_DIR / ".webhook_destination_registry.db"


# =============================================================================
# ENUMS & DATACLASSES
# =============================================================================

class DestinationType(str, Enum):
    """Type of webhook destination."""
    IDE = "ide"                    # VS Code, Cursor, Windsurf, etc.
    CHAT_APP = "chat_app"          # ChatGPT, Claude.ai, Synaptic Chat
    TERMINAL = "terminal"          # iTerm, Terminal.app
    BROWSER = "browser"            # Chrome, Safari (web-based tools)
    CUSTOM = "custom"              # User-defined


class HookPhase(str, Enum):
    """When the hook fires."""
    PRE_MESSAGE = "pre_message"    # Before user message sent
    POST_MESSAGE = "post_message"  # After LLM response received
    ON_SAVE = "on_save"            # When file saved (for IDEs)
    ON_COMMIT = "on_commit"        # When git commit made


@dataclass
class WebhookDestinationConfig:
    """Configuration for a webhook destination."""
    destination_id: str
    name: str
    display_name: str
    destination_type: DestinationType
    bundle_ids: List[str]          # macOS bundle IDs (can match multiple)
    process_names: List[str]       # Process names to match

    # Hook configurations
    pre_hook_enabled: bool = True
    post_hook_enabled: bool = False  # Most don't have this yet

    # Hook templates (JSON strings)
    pre_hook_template: Optional[str] = None
    post_hook_template: Optional[str] = None

    # Configuration details
    config_path: Optional[str] = None      # Where config file lives
    hook_script_path: Optional[str] = None # Hook script location

    # Status
    is_configured: bool = False
    last_verified_utc: Optional[str] = None
    configuration_notes: Optional[str] = None

    # Metadata
    created_at_utc: Optional[str] = None
    updated_at_utc: Optional[str] = None


@dataclass
class ComputerFingerprint:
    """Computer capability fingerprint for LLM recommendations."""
    fingerprint_id: str
    created_at_utc: str

    # Hardware
    ram_gb: float
    cpu_cores: int
    cpu_model: str
    has_gpu: bool
    gpu_model: Optional[str]
    has_apple_silicon: bool
    apple_silicon_model: Optional[str]  # M1, M2, M3, M4, etc.

    # OS
    os_name: str
    os_version: str

    # Disk
    disk_free_gb: float

    # LLM Recommendations
    recommended_llm: str
    recommended_backend: str  # ollama, mlx, etc.
    can_run_7b: bool
    can_run_14b: bool
    can_run_32b: bool

    # Feature availability
    has_accessibility_permission: bool
    has_notification_permission: bool


# =============================================================================
# BUILT-IN DESTINATION TEMPLATES
# =============================================================================

BUILTIN_DESTINATIONS: List[Dict[str, Any]] = [
    {
        "destination_id": "vs_code_claude_code",
        "name": "vs_code_claude_code",
        "display_name": "Claude Code (VS Code)",
        "destination_type": DestinationType.IDE,
        "bundle_ids": ["com.microsoft.VSCode", "com.microsoft.VSCodeInsiders"],
        "process_names": ["Code", "Code - Insiders", "Electron"],
        "pre_hook_enabled": True,
        "post_hook_enabled": False,
        "config_path": "~/.claude/settings.local.json",
        "hook_script_path": "~/.claude/hooks/user-prompt-submit.sh",
        "pre_hook_template": json.dumps({
            "hooks": {
                "user-prompt-submit": [
                    {
                        "type": "command",
                        "command": "~/.claude/hooks/user-prompt-submit.sh"
                    }
                ]
            }
        }, indent=2),
        "configuration_notes": "Requires ~/.claude/settings.local.json with hooks configured"
    },
    {
        "destination_id": "cursor_overseer",
        "name": "cursor_overseer",
        "display_name": "Cursor AI",
        "destination_type": DestinationType.IDE,
        "bundle_ids": ["com.todesktop.230313mzl4w4u92"],
        "process_names": ["Cursor"],
        "pre_hook_enabled": True,
        "post_hook_enabled": False,
        "config_path": "~/.cursor/settings.json",
        "configuration_notes": "Cursor uses similar hook system to VS Code"
    },
    {
        "destination_id": "windsurf",
        "name": "windsurf",
        "display_name": "Windsurf Editor",
        "destination_type": DestinationType.IDE,
        "bundle_ids": ["com.codeium.windsurf"],
        "process_names": ["Windsurf"],
        "pre_hook_enabled": True,
        "post_hook_enabled": False,
        "configuration_notes": "Windsurf configuration pending"
    },
    {
        "destination_id": "chatgpt_app",
        "name": "chatgpt_app",
        "display_name": "ChatGPT Desktop",
        "destination_type": DestinationType.CHAT_APP,
        "bundle_ids": ["com.openai.chat"],
        "process_names": ["ChatGPT"],
        "pre_hook_enabled": False,  # No hook support yet
        "post_hook_enabled": False,
        "configuration_notes": "ChatGPT desktop app - webhook injection via clipboard or API"
    },
    {
        "destination_id": "synaptic_chat",
        "name": "synaptic_chat",
        "display_name": "Synaptic Chat",
        "destination_type": DestinationType.CHAT_APP,
        "bundle_ids": [],  # Web-based
        "process_names": [],
        "pre_hook_enabled": True,
        "post_hook_enabled": True,
        "configuration_notes": "Internal Synaptic chat interface - full webhook support"
    },
    {
        "destination_id": "claude_web",
        "name": "claude_web",
        "display_name": "Claude.ai (Web)",
        "destination_type": DestinationType.BROWSER,
        "bundle_ids": ["com.google.Chrome", "com.apple.Safari", "org.mozilla.firefox"],
        "process_names": ["Google Chrome", "Safari", "Firefox"],
        "pre_hook_enabled": False,  # Browser extension needed
        "post_hook_enabled": False,
        "configuration_notes": "Requires browser extension for injection"
    },
    {
        "destination_id": "iterm",
        "name": "iterm",
        "display_name": "iTerm2",
        "destination_type": DestinationType.TERMINAL,
        "bundle_ids": ["com.googlecode.iterm2"],
        "process_names": ["iTerm2"],
        "pre_hook_enabled": False,
        "post_hook_enabled": False,
        "configuration_notes": "Terminal - context via shell integration"
    },
    {
        "destination_id": "terminal_app",
        "name": "terminal_app",
        "display_name": "Terminal.app",
        "destination_type": DestinationType.TERMINAL,
        "bundle_ids": ["com.apple.Terminal"],
        "process_names": ["Terminal"],
        "pre_hook_enabled": False,
        "post_hook_enabled": False,
        "configuration_notes": "macOS Terminal - context via shell integration"
    },
    {
        "destination_id": "antigravity",
        "name": "antigravity",
        "display_name": "Antigravity",
        "destination_type": DestinationType.CUSTOM,
        "bundle_ids": [],
        "process_names": [],
        "pre_hook_enabled": True,
        "post_hook_enabled": True,
        "configuration_notes": "Antigravity system - custom webhook integration"
    },
    {
        "destination_id": "hermes",
        "name": "hermes",
        "display_name": "Hermes Agent",
        "destination_type": DestinationType.CUSTOM,
        "bundle_ids": [],
        "process_names": [],
        "pre_hook_enabled": True,
        "post_hook_enabled": True,
        "configuration_notes": "Hermes ledger sync agent"
    },
]


# =============================================================================
# MACOS APP DETECTION (Accessibility APIs)
# =============================================================================

def detect_active_app() -> Dict[str, Any]:
    """
    Detect the currently active (frontmost) application on macOS.

    Uses AppleScript to query the system for the active app.

    Returns:
        Dict with app info: name, bundle_id, process_name, window_title
        Returns empty dict if detection fails.

    Note: Requires accessibility permissions for full functionality.
    """
    try:
        # Get frontmost app info via AppleScript
        script = '''
        tell application "System Events"
            set frontApp to first application process whose frontmost is true
            set appName to name of frontApp
            set bundleID to bundle identifier of frontApp
            set windowTitle to ""
            try
                set windowTitle to name of first window of frontApp
            end try
            return appName & "|" & bundleID & "|" & windowTitle
        end tell
        '''

        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0:
            parts = result.stdout.strip().split("|")
            return {
                "app_name": parts[0] if len(parts) > 0 else "",
                "bundle_id": parts[1] if len(parts) > 1 else "",
                "window_title": parts[2] if len(parts) > 2 else "",
                "detected_at_utc": datetime.now(timezone.utc).isoformat()
            }
        else:
            logger.warning(f"AppleScript failed: {result.stderr}")
            return {}

    except subprocess.TimeoutExpired:
        logger.warning("App detection timed out")
        return {}
    except Exception as e:
        logger.warning(f"App detection failed: {e}")
        return {}


def detect_active_text_field() -> Dict[str, Any]:
    """
    Detect if user is in a text input field and get field info.

    Uses macOS Accessibility APIs via AppleScript.

    Returns:
        Dict with field info: is_text_field, field_role, field_value_length

    Note: Requires accessibility permissions.
    """
    try:
        script = '''
        tell application "System Events"
            set frontApp to first application process whose frontmost is true
            try
                set focusedElement to focused UI element of frontApp
                set elementRole to role of focusedElement
                set isTextField to (elementRole is "AXTextArea" or elementRole is "AXTextField" or elementRole is "AXWebArea")
                set valueLength to 0
                try
                    set valueLength to length of (value of focusedElement as text)
                end try
                return (isTextField as text) & "|" & elementRole & "|" & (valueLength as text)
            on error
                return "false|unknown|0"
            end try
        end tell
        '''

        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0:
            parts = result.stdout.strip().split("|")
            return {
                "is_text_field": parts[0].lower() == "true" if len(parts) > 0 else False,
                "field_role": parts[1] if len(parts) > 1 else "unknown",
                "field_value_length": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
                "detected_at_utc": datetime.now(timezone.utc).isoformat()
            }
        else:
            return {"is_text_field": False, "field_role": "unknown", "field_value_length": 0}

    except Exception as e:
        logger.warning(f"Text field detection failed: {e}")
        return {"is_text_field": False, "field_role": "unknown", "field_value_length": 0}


# =============================================================================
# COMPUTER FINGERPRINT
# =============================================================================

def get_computer_fingerprint() -> ComputerFingerprint:
    """
    Generate a fingerprint of this computer's capabilities.

    Used during install to recommend optimal LLM configuration.

    Returns:
        ComputerFingerprint with hardware info and LLM recommendations
    """
    import platform
    import shutil
    import os

    # RAM
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5
        )
        ram_bytes = int(result.stdout.strip())
        ram_gb = ram_bytes / (1024**3)
    except Exception:
        ram_gb = 8.0  # Default assumption

    # CPU
    try:
        cpu_cores = os.cpu_count() or 4
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=5
        )
        cpu_model = result.stdout.strip() or "Unknown"
    except Exception:
        cpu_cores = 4
        cpu_model = "Unknown"

    # Apple Silicon detection
    has_apple_silicon = platform.processor() == "arm" or "Apple" in cpu_model
    apple_silicon_model = None
    if has_apple_silicon:
        if "M4" in cpu_model:
            apple_silicon_model = "M4"
        elif "M3" in cpu_model:
            apple_silicon_model = "M3"
        elif "M2" in cpu_model:
            apple_silicon_model = "M2"
        elif "M1" in cpu_model:
            apple_silicon_model = "M1"
        else:
            apple_silicon_model = "Apple Silicon"

    # GPU (Apple Silicon unified memory = "has GPU")
    has_gpu = has_apple_silicon
    gpu_model = apple_silicon_model if has_apple_silicon else None

    # Disk
    try:
        disk = shutil.disk_usage(Path.home())
        disk_free_gb = disk.free / (1024**3)
    except Exception:
        disk_free_gb = 50.0

    # OS
    os_name = platform.system()
    os_version = platform.mac_ver()[0] if os_name == "Darwin" else platform.release()

    # LLM Recommendations based on RAM
    if ram_gb >= 64:
        recommended_llm = "qwen2.5-coder:32b"
        can_run_32b = True
        can_run_14b = True
        can_run_7b = True
    elif ram_gb >= 32:
        recommended_llm = "qwen2.5-coder:14b"
        can_run_32b = False
        can_run_14b = True
        can_run_7b = True
    elif ram_gb >= 16:
        recommended_llm = "qwen2.5-coder:7b"
        can_run_32b = False
        can_run_14b = False
        can_run_7b = True
    else:
        recommended_llm = "qwen2.5-coder:3b"
        can_run_32b = False
        can_run_14b = False
        can_run_7b = False

    # Backend recommendation
    if has_apple_silicon:
        recommended_backend = "mlx"  # Prefer MLX on Apple Silicon
    else:
        recommended_backend = "ollama"

    # Permission checks
    has_accessibility = _check_accessibility_permission()
    has_notification = _check_notification_permission()

    return ComputerFingerprint(
        fingerprint_id=f"fp_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        ram_gb=round(ram_gb, 1),
        cpu_cores=cpu_cores,
        cpu_model=cpu_model,
        has_gpu=has_gpu,
        gpu_model=gpu_model,
        has_apple_silicon=has_apple_silicon,
        apple_silicon_model=apple_silicon_model,
        os_name=os_name,
        os_version=os_version,
        disk_free_gb=round(disk_free_gb, 1),
        recommended_llm=recommended_llm,
        recommended_backend=recommended_backend,
        can_run_7b=can_run_7b,
        can_run_14b=can_run_14b,
        can_run_32b=can_run_32b,
        has_accessibility_permission=has_accessibility,
        has_notification_permission=has_notification
    )


def _check_accessibility_permission() -> bool:
    """Check if accessibility permission is granted."""
    try:
        # Try a simple AppleScript that requires accessibility
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to return name of first application process'],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def _check_notification_permission() -> bool:
    """Check if notification permission is likely granted."""
    try:
        # Try sending a silent notification
        result = subprocess.run(
            ["osascript", "-e", 'display notification "" with title ""'],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


# =============================================================================
# WEBHOOK DESTINATION REGISTRY
# =============================================================================

class WebhookDestinationRegistry:
    """
    Registry of webhook destinations with auto-detection and configuration.

    Stores configurations in SQLite for persistence.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or REGISTRY_DB
        self._conn = None
        self._init_db()
        self._seed_builtin_destinations()

    def _init_db(self):
        """Initialize SQLite database."""
        from memory.db_utils import connect_wal
        self._conn = connect_wal(str(self._db_path))

        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS webhook_destination (
                destination_id       TEXT PRIMARY KEY,
                name                 TEXT NOT NULL,
                display_name         TEXT NOT NULL,
                destination_type     TEXT NOT NULL,
                bundle_ids_json      TEXT NOT NULL DEFAULT '[]',
                process_names_json   TEXT NOT NULL DEFAULT '[]',
                pre_hook_enabled     INTEGER NOT NULL DEFAULT 1,
                post_hook_enabled    INTEGER NOT NULL DEFAULT 0,
                pre_hook_template    TEXT,
                post_hook_template   TEXT,
                config_path          TEXT,
                hook_script_path     TEXT,
                is_configured        INTEGER NOT NULL DEFAULT 0,
                last_verified_utc    TEXT,
                configuration_notes  TEXT,
                created_at_utc       TEXT NOT NULL,
                updated_at_utc       TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_webhook_dest_type
                ON webhook_destination(destination_type);

            CREATE TABLE IF NOT EXISTS computer_fingerprint (
                fingerprint_id       TEXT PRIMARY KEY,
                created_at_utc       TEXT NOT NULL,
                fingerprint_json     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS destination_detection_log (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc        TEXT NOT NULL,
                app_name             TEXT,
                bundle_id            TEXT,
                matched_destination  TEXT,
                is_text_field        INTEGER,
                field_role           TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_detection_log_time
                ON destination_detection_log(timestamp_utc DESC);
        """)
        self._conn.commit()

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _seed_builtin_destinations(self):
        """Seed built-in destination templates."""
        for dest in BUILTIN_DESTINATIONS:
            cursor = self._conn.execute(
                "SELECT destination_id FROM webhook_destination WHERE destination_id = ?",
                (dest["destination_id"],)
            )
            if not cursor.fetchone():
                now = self._utc_now()
                self._conn.execute("""
                    INSERT INTO webhook_destination
                    (destination_id, name, display_name, destination_type,
                     bundle_ids_json, process_names_json, pre_hook_enabled, post_hook_enabled,
                     pre_hook_template, post_hook_template, config_path, hook_script_path,
                     is_configured, configuration_notes, created_at_utc, updated_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    dest["destination_id"],
                    dest["name"],
                    dest["display_name"],
                    dest["destination_type"].value if isinstance(dest["destination_type"], DestinationType) else dest["destination_type"],
                    json.dumps(dest.get("bundle_ids", [])),
                    json.dumps(dest.get("process_names", [])),
                    1 if dest.get("pre_hook_enabled", True) else 0,
                    1 if dest.get("post_hook_enabled", False) else 0,
                    dest.get("pre_hook_template"),
                    dest.get("post_hook_template"),
                    dest.get("config_path"),
                    dest.get("hook_script_path"),
                    0,  # Not configured by default
                    dest.get("configuration_notes"),
                    now,
                    now
                ))
        self._conn.commit()

    def get_all_destinations(self) -> List[WebhookDestinationConfig]:
        """Get all registered destinations."""
        cursor = self._conn.execute("SELECT * FROM webhook_destination ORDER BY display_name")
        results = []
        for row in cursor:
            results.append(self._row_to_config(row))
        return results

    def get_destination(self, destination_id: str) -> Optional[WebhookDestinationConfig]:
        """Get a specific destination by ID."""
        cursor = self._conn.execute(
            "SELECT * FROM webhook_destination WHERE destination_id = ?",
            (destination_id,)
        )
        row = cursor.fetchone()
        return self._row_to_config(row) if row else None

    def get_config_for_app(self, bundle_id: str) -> Optional[WebhookDestinationConfig]:
        """Find a destination config matching the given bundle ID."""
        cursor = self._conn.execute("SELECT * FROM webhook_destination")
        for row in cursor:
            bundle_ids = json.loads(row["bundle_ids_json"])
            if bundle_id in bundle_ids:
                return self._row_to_config(row)
        return None

    def get_config_for_process(self, process_name: str) -> Optional[WebhookDestinationConfig]:
        """Find a destination config matching the given process name."""
        cursor = self._conn.execute("SELECT * FROM webhook_destination")
        for row in cursor:
            process_names = json.loads(row["process_names_json"])
            if process_name in process_names:
                return self._row_to_config(row)
        return None

    def detect_and_match(self) -> Dict[str, Any]:
        """
        Detect active app and match to a destination.

        Returns:
            Dict with detection results and matched destination (if any)
        """
        app_info = detect_active_app()
        text_field_info = detect_active_text_field()

        result = {
            "app_info": app_info,
            "text_field_info": text_field_info,
            "matched_destination": None,
            "is_configured": False,
            "configuration_needed": None
        }

        if not app_info.get("bundle_id"):
            return result

        # Try to match by bundle ID
        config = self.get_config_for_app(app_info["bundle_id"])

        # If not found, try by process name
        if not config and app_info.get("app_name"):
            config = self.get_config_for_process(app_info["app_name"])

        if config:
            result["matched_destination"] = asdict(config)
            result["is_configured"] = config.is_configured

            if not config.is_configured:
                result["configuration_needed"] = {
                    "destination_id": config.destination_id,
                    "config_path": config.config_path,
                    "notes": config.configuration_notes
                }

        # Log detection
        self._log_detection(app_info, text_field_info, config)

        return result

    def _log_detection(
        self,
        app_info: Dict[str, Any],
        text_field_info: Dict[str, Any],
        matched_config: Optional[WebhookDestinationConfig]
    ):
        """Log a detection event."""
        self._conn.execute("""
            INSERT INTO destination_detection_log
            (timestamp_utc, app_name, bundle_id, matched_destination, is_text_field, field_role)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            self._utc_now(),
            app_info.get("app_name"),
            app_info.get("bundle_id"),
            matched_config.destination_id if matched_config else None,
            1 if text_field_info.get("is_text_field") else 0,
            text_field_info.get("field_role")
        ))
        self._conn.commit()

    def mark_configured(self, destination_id: str, is_configured: bool = True):
        """Mark a destination as configured."""
        self._conn.execute("""
            UPDATE webhook_destination
            SET is_configured = ?, last_verified_utc = ?, updated_at_utc = ?
            WHERE destination_id = ?
        """, (1 if is_configured else 0, self._utc_now(), self._utc_now(), destination_id))
        self._conn.commit()

    def add_custom_destination(
        self,
        destination_id: str,
        name: str,
        display_name: str,
        destination_type: DestinationType,
        bundle_ids: List[str] = None,
        process_names: List[str] = None,
        **kwargs
    ) -> WebhookDestinationConfig:
        """Add a custom destination configuration."""
        now = self._utc_now()

        self._conn.execute("""
            INSERT OR REPLACE INTO webhook_destination
            (destination_id, name, display_name, destination_type,
             bundle_ids_json, process_names_json, pre_hook_enabled, post_hook_enabled,
             pre_hook_template, post_hook_template, config_path, hook_script_path,
             is_configured, configuration_notes, created_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            destination_id,
            name,
            display_name,
            destination_type.value,
            json.dumps(bundle_ids or []),
            json.dumps(process_names or []),
            1 if kwargs.get("pre_hook_enabled", True) else 0,
            1 if kwargs.get("post_hook_enabled", False) else 0,
            kwargs.get("pre_hook_template"),
            kwargs.get("post_hook_template"),
            kwargs.get("config_path"),
            kwargs.get("hook_script_path"),
            1 if kwargs.get("is_configured", False) else 0,
            kwargs.get("configuration_notes"),
            now,
            now
        ))
        self._conn.commit()

        return self.get_destination(destination_id)

    def save_fingerprint(self, fingerprint: ComputerFingerprint):
        """Save computer fingerprint to database."""
        self._conn.execute("""
            INSERT OR REPLACE INTO computer_fingerprint
            (fingerprint_id, created_at_utc, fingerprint_json)
            VALUES (?, ?, ?)
        """, (
            fingerprint.fingerprint_id,
            fingerprint.created_at_utc,
            json.dumps(asdict(fingerprint))
        ))
        self._conn.commit()

    def get_latest_fingerprint(self) -> Optional[ComputerFingerprint]:
        """Get the most recent computer fingerprint."""
        cursor = self._conn.execute("""
            SELECT fingerprint_json FROM computer_fingerprint
            ORDER BY created_at_utc DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            data = json.loads(row["fingerprint_json"])
            return ComputerFingerprint(**data)
        return None

    def _row_to_config(self, row) -> WebhookDestinationConfig:
        """Convert a database row to WebhookDestinationConfig."""
        return WebhookDestinationConfig(
            destination_id=row["destination_id"],
            name=row["name"],
            display_name=row["display_name"],
            destination_type=DestinationType(row["destination_type"]),
            bundle_ids=json.loads(row["bundle_ids_json"]),
            process_names=json.loads(row["process_names_json"]),
            pre_hook_enabled=bool(row["pre_hook_enabled"]),
            post_hook_enabled=bool(row["post_hook_enabled"]),
            pre_hook_template=row["pre_hook_template"],
            post_hook_template=row["post_hook_template"],
            config_path=row["config_path"],
            hook_script_path=row["hook_script_path"],
            is_configured=bool(row["is_configured"]),
            last_verified_utc=row["last_verified_utc"],
            configuration_notes=row["configuration_notes"],
            created_at_utc=row["created_at_utc"],
            updated_at_utc=row["updated_at_utc"]
        )


# =============================================================================
# LLM-ASSISTED CONFIGURATION (Future)
# =============================================================================

async def troubleshoot_configuration_with_llm(
    destination_id: str,
    error_message: str,
    current_config: Optional[str] = None
) -> Dict[str, Any]:
    """
    Use memory system to troubleshoot configuration issues.

    Queries the professor/memory for similar past errors and known fixes.
    Falls back to pattern-based suggestions if LLM unavailable.

    Args:
        destination_id: Which destination has the issue
        error_message: The error encountered
        current_config: Current configuration content

    Returns:
        Dict with suggested fixes and explanations
    """
    suggestions = []

    # Query memory for similar past errors
    try:
        from memory.query import query_memory
        results = query_memory(f"fix {error_message} {destination_id}", limit=3)
        if results:
            for r in results:
                suggestions.append({
                    "source": "memory",
                    "suggestion": r.get("statement", r.get("text", str(r))),
                    "confidence": r.get("confidence", 0.5),
                })
    except Exception as e:
        print(f"[WARN] Memory-based diagnostic lookup failed: {e}")

    # Pattern-based diagnostics
    error_lower = error_message.lower()
    if "connection refused" in error_lower or "timeout" in error_lower:
        suggestions.append({
            "source": "pattern",
            "suggestion": f"Check if {destination_id} service is running. Verify port/host config.",
            "confidence": 0.8,
        })
    if "permission" in error_lower or "403" in error_lower or "401" in error_lower:
        suggestions.append({
            "source": "pattern",
            "suggestion": "Check API keys/tokens. Verify OAuth scopes and token expiry.",
            "confidence": 0.7,
        })
    if "ssl" in error_lower or "certificate" in error_lower:
        suggestions.append({
            "source": "pattern",
            "suggestion": "Check SSL certificate validity. Try with verify=False for local dev.",
            "confidence": 0.7,
        })

    if not suggestions:
        suggestions.append({
            "source": "fallback",
            "suggestion": f"No known fix for '{error_message[:80]}'. Check logs and config.",
            "confidence": 0.3,
        })

    return {
        "status": "ok",
        "destination_id": destination_id,
        "error": error_message,
        "suggestions": suggestions,
        "suggestion_count": len(suggestions),
    }


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()

        if cmd == "detect":
            print("Detecting active app...")
            app_info = detect_active_app()
            print(f"  App: {app_info.get('app_name', 'Unknown')}")
            print(f"  Bundle ID: {app_info.get('bundle_id', 'Unknown')}")
            print(f"  Window: {app_info.get('window_title', 'Unknown')}")

            print("\nChecking text field...")
            field_info = detect_active_text_field()
            print(f"  Is text field: {field_info.get('is_text_field', False)}")
            print(f"  Field role: {field_info.get('field_role', 'Unknown')}")

            print("\nMatching to destination...")
            registry = WebhookDestinationRegistry()
            result = registry.detect_and_match()
            if result["matched_destination"]:
                dest = result["matched_destination"]
                print(f"  Matched: {dest['display_name']}")
                print(f"  Configured: {result['is_configured']}")
                if result["configuration_needed"]:
                    print(f"  Config needed: {result['configuration_needed']['notes']}")
            else:
                print("  No matching destination found")

        elif cmd == "fingerprint":
            print("Generating computer fingerprint...")
            fp = get_computer_fingerprint()
            print(f"\n=== COMPUTER FINGERPRINT ===")
            print(f"  RAM: {fp.ram_gb} GB")
            print(f"  CPU: {fp.cpu_model} ({fp.cpu_cores} cores)")
            print(f"  Apple Silicon: {fp.apple_silicon_model or 'No'}")
            print(f"  Disk Free: {fp.disk_free_gb} GB")
            print(f"  OS: {fp.os_name} {fp.os_version}")
            print(f"\n=== LLM RECOMMENDATIONS ===")
            print(f"  Recommended LLM: {fp.recommended_llm}")
            print(f"  Recommended Backend: {fp.recommended_backend}")
            print(f"  Can run 7B: {fp.can_run_7b}")
            print(f"  Can run 14B: {fp.can_run_14b}")
            print(f"  Can run 32B: {fp.can_run_32b}")
            print(f"\n=== PERMISSIONS ===")
            print(f"  Accessibility: {'✅' if fp.has_accessibility_permission else '❌'}")
            print(f"  Notifications: {'✅' if fp.has_notification_permission else '❌'}")

            # Save to registry
            registry = WebhookDestinationRegistry()
            registry.save_fingerprint(fp)
            print(f"\nFingerprint saved: {fp.fingerprint_id}")

        elif cmd == "list":
            registry = WebhookDestinationRegistry()
            destinations = registry.get_all_destinations()

            print("=== REGISTERED WEBHOOK DESTINATIONS ===\n")
            for dest in destinations:
                status = "✅" if dest.is_configured else "❌"
                hooks = []
                if dest.pre_hook_enabled:
                    hooks.append("pre")
                if dest.post_hook_enabled:
                    hooks.append("post")
                hooks_str = ",".join(hooks) if hooks else "none"

                print(f"{status} {dest.display_name}")
                print(f"    ID: {dest.destination_id}")
                print(f"    Type: {dest.destination_type.value}")
                print(f"    Hooks: {hooks_str}")
                if dest.bundle_ids:
                    print(f"    Bundle IDs: {', '.join(dest.bundle_ids[:2])}")
                print()

        elif cmd == "status":
            registry = WebhookDestinationRegistry()
            destinations = registry.get_all_destinations()

            configured = sum(1 for d in destinations if d.is_configured)
            total = len(destinations)

            print(f"Destinations: {configured}/{total} configured")

            # Check latest fingerprint
            fp = registry.get_latest_fingerprint()
            if fp:
                print(f"Fingerprint: {fp.fingerprint_id} ({fp.ram_gb}GB RAM)")
                print(f"Recommended: {fp.recommended_llm} via {fp.recommended_backend}")
            else:
                print("Fingerprint: Not generated (run 'fingerprint' command)")

        else:
            print(f"Unknown command: {cmd}")
            print("\nCommands:")
            print("  detect      - Detect active app and match to destination")
            print("  fingerprint - Generate computer capability fingerprint")
            print("  list        - List all registered destinations")
            print("  status      - Show registry status")

    else:
        print("Webhook Destination Registry")
        print("\nCommands:")
        print("  detect      - Detect active app and match to destination")
        print("  fingerprint - Generate computer capability fingerprint")
        print("  list        - List all registered destinations")
        print("  status      - Show registry status")
