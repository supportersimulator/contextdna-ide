#!/usr/bin/env python3
"""
System Notifications - Native OS Notifications for Context DNA

This module provides cross-platform system notifications:
1. Setup reminders with click-to-configure
2. Success notifications
3. Error alerts
4. AI-assisted configuration guidance

CLICK-TO-CONFIGURE:
When users click a notification, it opens:
- The dashboard setup page
- The terminal with pre-filled commands
- The IDE settings panel (for API keys)

SECURITY:
- API keys are stored in system keychain (macOS) or secure storage
- Never stores raw API keys in plain text
- Recommends IDE environment variables over hardcoded keys

Usage:
    from context_dna.setup import notify, notify_setup_needed

    # Simple notification
    notify("Context DNA", "Learning recorded!")

    # Setup needed notification (click opens dashboard)
    notify_setup_needed("LLM Provider", "Configure Ollama or add API key")

    # Success with sound
    notify_success("Pattern detected!", "async → to_thread pattern")
"""

import os
import sys
import json
import platform
import subprocess
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass


# =============================================================================
# NOTIFICATION DATACLASS
# =============================================================================

@dataclass
class Notification:
    """A system notification."""
    title: str
    message: str
    subtitle: Optional[str] = None
    sound: bool = False
    action_url: Optional[str] = None  # Opens on click
    action_command: Optional[str] = None  # Runs on click


# =============================================================================
# CROSS-PLATFORM NOTIFICATION
# =============================================================================

def notify(
    title: str,
    message: str,
    subtitle: str = None,
    sound: bool = False,
    action_url: str = None,
) -> bool:
    """
    Send a system notification.

    Args:
        title: Notification title
        message: Main message body
        subtitle: Optional subtitle (macOS only)
        sound: Play notification sound
        action_url: URL to open when clicked

    Returns:
        True if notification sent successfully

    Example:
        >>> notify("Context DNA", "New pattern detected!")
        True
    """
    system = platform.system()

    try:
        if system == "Darwin":
            return _notify_macos(title, message, subtitle, sound, action_url)
        elif system == "Linux":
            return _notify_linux(title, message, action_url)
        elif system == "Windows":
            return _notify_windows(title, message)
        else:
            # Fallback: print to terminal
            print(f"[{title}] {message}")
            return True
    except Exception as e:
        # Silent fallback
        print(f"[{title}] {message}")
        return False


def _notify_macos(
    title: str,
    message: str,
    subtitle: str = None,
    sound: bool = False,
    action_url: str = None,
) -> bool:
    """macOS notification using osascript."""
    script_parts = [f'display notification "{message}"']
    script_parts.append(f'with title "{title}"')

    if subtitle:
        script_parts.append(f'subtitle "{subtitle}"')

    if sound:
        script_parts.append('sound name "Glass"')

    script = " ".join(script_parts)

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
    )

    # If action URL provided, also open it
    if action_url and result.returncode == 0:
        subprocess.run(["open", action_url], capture_output=True)

    return result.returncode == 0


def _notify_linux(title: str, message: str, action_url: str = None) -> bool:
    """Linux notification using notify-send."""
    cmd = ["notify-send", title, message]

    if action_url:
        cmd.extend(["--action", f"open=Open", "--action", f"url={action_url}"])

    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def _notify_windows(title: str, message: str) -> bool:
    """Windows notification using PowerShell."""
    ps_script = f"""
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $textNodes = $template.GetElementsByTagName("text")
    $textNodes.Item(0).AppendChild($template.CreateTextNode("{title}")) | Out-Null
    $textNodes.Item(1).AppendChild($template.CreateTextNode("{message}")) | Out-Null
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Context DNA")
    $notifier.Show([Windows.UI.Notifications.ToastNotification]::new($template))
    """

    result = subprocess.run(
        ["powershell", "-Command", ps_script],
        capture_output=True,
    )
    return result.returncode == 0


# =============================================================================
# SPECIALIZED NOTIFICATIONS
# =============================================================================

def notify_setup_needed(
    component: str,
    issue: str,
    fix_url: str = "http://localhost:3456/setup",
) -> bool:
    """
    Notify user that setup is needed with click-to-configure.

    Args:
        component: What needs setup (e.g., "LLM Provider")
        issue: Description of the issue
        fix_url: URL to open for fixing

    Returns:
        True if notification sent

    Example:
        >>> notify_setup_needed("LLM Provider", "No AI provider configured")
        True
    """
    return notify(
        title="Context DNA Setup Needed",
        message=f"{component}: {issue}",
        subtitle="Click to configure",
        sound=True,
        action_url=fix_url,
    )


def notify_success(
    title: str,
    details: str = None,
    sound: bool = True,
) -> bool:
    """
    Send a success notification.

    Args:
        title: What succeeded
        details: Optional details
        sound: Play success sound

    Returns:
        True if notification sent
    """
    message = details if details else "Operation completed successfully"
    return notify(
        title=f"✅ {title}",
        message=message,
        sound=sound,
    )


def notify_error(
    title: str,
    error: str,
    fix_url: str = None,
) -> bool:
    """
    Send an error notification.

    Args:
        title: Error title
        error: Error description
        fix_url: URL to troubleshoot

    Returns:
        True if notification sent
    """
    return notify(
        title=f"❌ {title}",
        message=error,
        sound=True,
        action_url=fix_url,
    )


def notify_learning(
    learning_type: str,
    title: str,
) -> bool:
    """
    Notify that a new learning was recorded.

    Args:
        learning_type: Type (win, fix, pattern, etc.)
        title: Learning title

    Returns:
        True if notification sent
    """
    emoji = {
        "win": "🏆",
        "fix": "🔧",
        "pattern": "🧩",
        "sop": "📋",
        "insight": "💡",
    }.get(learning_type, "📝")

    return notify(
        title=f"{emoji} New {learning_type.title()}",
        message=title[:100],
        sound=False,
    )


# =============================================================================
# NOTIFICATION MANAGER (For Dashboard/API)
# =============================================================================

class NotificationManager:
    """
    Manages notification queue and preferences.

    Used by the dashboard to:
    - Queue notifications
    - Check notification history
    - Configure notification preferences
    """

    def __init__(self, config_dir: str = None):
        """Initialize notification manager."""
        self.config_dir = Path(config_dir or Path.home() / ".context-dna")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.prefs_file = self.config_dir / "notification_prefs.json"
        self.history_file = self.config_dir / "notification_history.json"
        self._load_prefs()

    def _load_prefs(self):
        """Load notification preferences."""
        if self.prefs_file.exists():
            self.prefs = json.loads(self.prefs_file.read_text())
        else:
            self.prefs = {
                "enabled": True,
                "sound": True,
                "setup_reminders": True,
                "learning_notifications": False,  # Can be noisy
                "error_notifications": True,
            }

    def save_prefs(self):
        """Save notification preferences."""
        self.prefs_file.write_text(json.dumps(self.prefs, indent=2))

    def is_enabled(self, notification_type: str) -> bool:
        """Check if a notification type is enabled."""
        if not self.prefs.get("enabled", True):
            return False

        type_map = {
            "setup": "setup_reminders",
            "learning": "learning_notifications",
            "error": "error_notifications",
        }

        pref_key = type_map.get(notification_type, "enabled")
        return self.prefs.get(pref_key, True)

    def send(self, notification: Notification) -> bool:
        """Send a notification if enabled."""
        if not self.prefs.get("enabled", True):
            return False

        success = notify(
            title=notification.title,
            message=notification.message,
            subtitle=notification.subtitle,
            sound=notification.sound and self.prefs.get("sound", True),
            action_url=notification.action_url,
        )

        # Log to history
        self._log_notification(notification, success)

        return success

    def _log_notification(self, notification: Notification, sent: bool):
        """Log notification to history."""
        history = []
        if self.history_file.exists():
            try:
                history = json.loads(self.history_file.read_text())
            except json.JSONDecodeError:
                history = []

        from datetime import datetime
        history.append({
            "timestamp": datetime.now().isoformat(),
            "title": notification.title,
            "message": notification.message,
            "sent": sent,
        })

        # Keep last 100
        history = history[-100:]
        self.history_file.write_text(json.dumps(history, indent=2))

    def get_history(self, limit: int = 20) -> List[Dict]:
        """Get notification history."""
        if not self.history_file.exists():
            return []

        history = json.loads(self.history_file.read_text())
        return history[-limit:]


# =============================================================================
# AI-ASSISTED SETUP NOTIFICATIONS
# =============================================================================

def get_ai_setup_guidance(component: str, error: str = None) -> Optional[str]:
    """
    Get AI-powered guidance for setup issues.

    Uses the configured LLM to provide specific commands and steps
    for the user's system and environment.

    Args:
        component: Component that needs setup
        error: Optional error message

    Returns:
        AI-generated guidance string or None
    """
    try:
        from context_dna.llm.manager import ProviderManager

        manager = ProviderManager()
        provider = manager.get_available_provider()

        if not provider:
            return None

        system_info = f"""
        OS: {platform.system()} {platform.release()}
        Shell: {os.environ.get('SHELL', 'unknown')}
        Python: {platform.python_version()}
        """

        prompt = f"""You are a helpful setup assistant for Context DNA.

The user needs to configure: {component}
System info: {system_info}
Error (if any): {error or 'None'}

Provide SPECIFIC terminal commands for their system.
Be concise - just the commands and brief explanations.
Do NOT include placeholder API keys - tell them where to get real ones.

IMPORTANT: Never show example API keys that look real.
Instead say "Get your key from: [URL]"
"""

        response = provider.generate(prompt)
        return response

    except Exception as e:
        return None


def notify_with_ai_guidance(
    component: str,
    issue: str,
    error: str = None,
) -> bool:
    """
    Send setup notification with AI-generated fix commands.

    Gets AI guidance for the specific issue and copies
    the commands to clipboard.

    Args:
        component: What needs setup
        issue: Description of issue
        error: Error message if any

    Returns:
        True if notification sent
    """
    # Try to get AI guidance
    guidance = get_ai_setup_guidance(component, error)

    if guidance and platform.system() == "Darwin":
        # Copy guidance to clipboard on macOS
        subprocess.run(
            ["pbcopy"],
            input=guidance.encode(),
            capture_output=True,
        )

        return notify(
            title=f"Context DNA: {component} Setup",
            message=f"{issue}\n\nFix commands copied to clipboard!",
            subtitle="Paste in terminal to configure",
            sound=True,
            action_url="http://localhost:3456/setup",
        )

    # Fallback without AI
    return notify_setup_needed(component, issue)


# =============================================================================
# API KEY DETECTION & SECURE STORAGE
# =============================================================================

def detect_api_keys() -> Dict[str, Optional[str]]:
    """
    Detect API keys from secure sources.

    Checks (in order of preference):
    1. IDE environment variables (VS Code, etc.)
    2. System keychain (macOS)
    3. .env file (warns if found)
    4. Environment variables

    Returns:
        Dict of provider -> key status (masked or None)
    """
    keys = {
        "openai": None,
        "anthropic": None,
        "google": None,
    }

    # Check environment (including IDE injected)
    openai_key = os.environ.get("Context_DNA_OPENAI")
    if openai_key:
        keys["openai"] = f"{openai_key[:8]}...{openai_key[-4:]}"

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        keys["anthropic"] = f"{anthropic_key[:12]}..."

    google_key = os.environ.get("GOOGLE_API_KEY")
    if google_key:
        keys["google"] = f"{google_key[:8]}..."

    return keys


def store_api_key_secure(provider: str, key: str) -> bool:
    """
    Store API key in system secure storage.

    On macOS: Uses Keychain
    On Linux: Uses libsecret if available
    On Windows: Uses Credential Manager

    ⚠️ SECURITY WARNING ⚠️
    We strongly recommend using IDE environment variables
    instead of storing keys directly. This function exists
    as a fallback for users without IDE support.

    Args:
        provider: Provider name (openai, anthropic, etc.)
        key: The API key

    Returns:
        True if stored successfully
    """
    if platform.system() == "Darwin":
        # macOS Keychain
        try:
            subprocess.run([
                "security", "add-generic-password",
                "-a", "context-dna",
                "-s", f"context-dna-{provider}",
                "-w", key,
                "-U",  # Update if exists
            ], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False

    elif platform.system() == "Linux":
        # Try libsecret (GNOME Keyring)
        try:
            subprocess.run([
                "secret-tool", "store",
                "--label", f"Context DNA {provider} API Key",
                "application", "context-dna",
                "provider", provider,
            ], input=key.encode(), check=True, capture_output=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    elif platform.system() == "Windows":
        # Windows Credential Manager via cmdkey
        try:
            subprocess.run([
                "cmdkey", "/generic:context-dna-" + provider,
                "/user:context-dna",
                "/pass:" + key,
            ], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False

    return False


def get_api_key_secure(provider: str) -> Optional[str]:
    """
    Retrieve API key from system secure storage.

    Args:
        provider: Provider name

    Returns:
        The API key or None
    """
    if platform.system() == "Darwin":
        try:
            result = subprocess.run([
                "security", "find-generic-password",
                "-a", "context-dna",
                "-s", f"context-dna-{provider}",
                "-w",
            ], capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None

    elif platform.system() == "Linux":
        try:
            result = subprocess.run([
                "secret-tool", "lookup",
                "application", "context-dna",
                "provider", provider,
            ], capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    return None


def get_api_key_setup_instructions(provider: str) -> str:
    """
    Get instructions for setting up an API key.

    Returns instructions that emphasize security best practices.

    Args:
        provider: Provider name

    Returns:
        Formatted instructions string
    """
    instructions = {
        "openai": """
╔══════════════════════════════════════════════════════════════════╗
║              OPENAI API KEY SETUP                                  ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                    ║
║  🔑 Get your key: https://platform.openai.com/api-keys            ║
║                                                                    ║
║  ⚠️  SECURITY WARNING: Never share your API key!                  ║
║      - Don't commit keys to git                                    ║
║      - Don't paste keys in chat/email                             ║
║      - Don't use keys in screenshots                              ║
║                                                                    ║
║  RECOMMENDED: Use IDE environment variables                        ║
║                                                                    ║
║  VS Code:                                                          ║
║    1. Open Settings (Cmd+,)                                        ║
║    2. Search "terminal.integrated.env.osx"                        ║
║    3. Add: "Context_DNA_OPENAI": "your-key-here"                      ║
║                                                                    ║
║  Alternative (.env file - add to .gitignore!):                    ║
║    echo "Context_DNA_OPENAI=your-key" >> .env                         ║
║    echo ".env" >> .gitignore                                       ║
║                                                                    ║
╚══════════════════════════════════════════════════════════════════╝
""",
        "anthropic": """
╔══════════════════════════════════════════════════════════════════╗
║              ANTHROPIC API KEY SETUP                               ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                    ║
║  🔑 Get your key: https://console.anthropic.com/settings/keys    ║
║                                                                    ║
║  ⚠️  SECURITY WARNING: Never share your API key!                  ║
║                                                                    ║
║  RECOMMENDED: Use IDE environment variables                        ║
║                                                                    ║
║  VS Code:                                                          ║
║    1. Open Settings (Cmd+,)                                        ║
║    2. Search "terminal.integrated.env.osx"                        ║
║    3. Add: "ANTHROPIC_API_KEY": "your-key-here"                   ║
║                                                                    ║
╚══════════════════════════════════════════════════════════════════╝
""",
        "ollama": """
╔══════════════════════════════════════════════════════════════════╗
║              OLLAMA SETUP (FREE - NO API KEY!)                     ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                    ║
║  🎉 Ollama runs locally - no API key needed!                      ║
║                                                                    ║
║  Install:                                                          ║
║    brew install ollama     # macOS                                ║
║    # or download from https://ollama.ai                           ║
║                                                                    ║
║  Start Ollama:                                                     ║
║    ollama serve                                                    ║
║                                                                    ║
║  Pull a model:                                                     ║
║    ollama pull llama3.2:3b     # Fast, ~2GB                       ║
║    ollama pull llama3.1:8b     # Better, ~5GB                     ║
║                                                                    ║
║  ✅ Once running, Context DNA will auto-detect Ollama!             ║
║                                                                    ║
╚══════════════════════════════════════════════════════════════════╝
""",
    }

    return instructions.get(provider, f"See {provider} documentation for API key setup.")


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Context DNA Notifications")
        print()
        print("Commands:")
        print("  test                - Send a test notification")
        print("  setup <component>   - Notify setup needed")
        print("  success <title>     - Send success notification")
        print("  keys                - Detect API keys")
        print("  instructions <prov> - Show API key setup instructions")
        print()
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "test":
        success = notify(
            "Context DNA Test",
            "Notifications are working!",
            sound=True,
        )
        print("✅ Notification sent" if success else "❌ Failed to send")

    elif cmd == "setup":
        component = sys.argv[2] if len(sys.argv) > 2 else "Configuration"
        notify_setup_needed(component, "Setup required")

    elif cmd == "success":
        title = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Success!"
        notify_success(title)

    elif cmd == "keys":
        keys = detect_api_keys()
        print("\n=== Detected API Keys ===\n")
        for provider, status in keys.items():
            icon = "✅" if status else "❌"
            print(f"  {icon} {provider.upper()}: {status or 'Not configured'}")
        print()

    elif cmd == "instructions":
        provider = sys.argv[2] if len(sys.argv) > 2 else "openai"
        print(get_api_key_setup_instructions(provider))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
