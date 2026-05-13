#!/usr/bin/env python3
"""
NOTIFICATION MANAGER - Centralized Notification Preferences System

Controls ALL macOS notifications from Context DNA with:
- Category-based enable/disable
- Quiet hours support
- Cooldown between repeated notifications
- Granular control per notification type

Usage:
    from memory.notification_manager import should_notify, send_notification, NotificationCategory

    # Check before sending
    if should_notify(NotificationCategory.LLM_STATUS):
        send_notification("Title", "Message", category=NotificationCategory.LLM_STATUS)

    # Or use the wrapper that checks automatically
    send_notification("Title", "Message", category=NotificationCategory.LLM_STATUS)

CLI:
    python memory/notification_manager.py status          # Show current settings
    python memory/notification_manager.py enable llm_status
    python memory/notification_manager.py disable llm_status
    python memory/notification_manager.py quiet-hours on  # Enable quiet hours
    python memory/notification_manager.py quiet-hours off # Disable quiet hours
    python memory/notification_manager.py test           # Send test notification
"""

import json
import subprocess
import time
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).parent
PREFERENCES_FILE = MEMORY_DIR / ".notification_preferences.json"
NOTIFICATION_STATE_FILE = MEMORY_DIR / ".notification_state.json"


class NotificationCategory(str, Enum):
    """Notification categories for granular control."""

    # Critical - always enabled by default
    CRITICAL = "critical"                      # Service completely down
    SERVICE_DOWN = "service_down"              # A service stopped responding
    RECOVERY_FAILED = "recovery_failed"        # Max recovery attempts reached

    # Status changes - disabled by default (the annoying ones)
    LLM_STATUS = "llm_status"                  # LLM server status changes
    LLM_RECOVERED = "llm_recovered"            # LLM server recovered
    VOICE_STATUS = "voice_status"              # Voice server status
    VOICE_RECOVERED = "voice_recovered"        # Voice server recovered
    SCHEDULER_STATUS = "scheduler_status"        # Scheduler (lite_scheduler) status

    # Informational - disabled by default
    CHAT_STATS = "chat_stats"                  # Chat usage statistics
    AUTO_RECOVERY_SUCCESS = "auto_recovery_success"  # Successful auto-recovery
    MODE_CHANGE = "mode_change"                # Mode changes (lite/heavy)
    INFO = "info"                              # General informational

    # Recovery notifications
    RECOVERY = "recovery"                      # Service recovered

    # Autonomous A/B testing
    AB_TEST_PROPOSED = "ab_test_proposed"       # Test designed + consensus reached
    AB_TEST_ACTIVATED = "ab_test_activated"     # Test activated after grace period
    AB_TEST_DEGRADED = "ab_test_degraded"       # Degradation detected during test
    AB_TEST_REVERTED = "ab_test_reverted"       # Test auto-reverted due to degradation
    AB_TEST_CONCLUDED = "ab_test_concluded"     # Test completed with results

    # Uncategorized (legacy)
    UNKNOWN = "unknown"


# Default preferences if file doesn't exist
DEFAULT_PREFERENCES = {
    "enabled_notifications": [
        "critical",
        "service_down",
        "recovery_failed",
        "llm_status",
        "llm_recovered",
        "ab_test_proposed",
        "ab_test_activated",
        "ab_test_degraded",
        "ab_test_reverted",
        "ab_test_concluded"
    ],
    "disabled_notifications": [
        "chat_stats",
        "info",
        "voice_status",
        "voice_recovered",
        "scheduler_status",
        "auto_recovery_success",
        "mode_change"
    ],
    "cooldown_minutes": 30,
    "quiet_hours": {
        "enabled": True,
        "start": "22:00",
        "end": "08:00"
    },
    "version": 1
}


def load_preferences() -> Dict[str, Any]:
    """Load notification preferences from file."""
    if PREFERENCES_FILE.exists():
        try:
            return json.loads(PREFERENCES_FILE.read_text())
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse preferences: {e}")
    return DEFAULT_PREFERENCES.copy()


def save_preferences(prefs: Dict[str, Any]):
    """Save notification preferences to file."""
    prefs["last_updated"] = datetime.now(timezone.utc).isoformat()
    PREFERENCES_FILE.write_text(json.dumps(prefs, indent=2))


def load_notification_state() -> Dict[str, Any]:
    """Load notification state (cooldown tracking)."""
    if NOTIFICATION_STATE_FILE.exists():
        try:
            return json.loads(NOTIFICATION_STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_sent": {}}


def save_notification_state(state: Dict[str, Any]):
    """Save notification state."""
    NOTIFICATION_STATE_FILE.write_text(json.dumps(state, indent=2))


def is_in_quiet_hours() -> bool:
    """Check if current time is within quiet hours."""
    prefs = load_preferences()
    quiet = prefs.get("quiet_hours", {})

    if not quiet.get("enabled", False):
        return False

    try:
        now = datetime.now()
        start_str = quiet.get("start", "22:00")
        end_str = quiet.get("end", "08:00")

        start_time = datetime.strptime(start_str, "%H:%M").time()
        end_time = datetime.strptime(end_str, "%H:%M").time()
        current_time = now.time()

        # Handle overnight quiet hours (e.g., 22:00 - 08:00)
        if start_time > end_time:
            return current_time >= start_time or current_time < end_time
        else:
            return start_time <= current_time < end_time
    except Exception as e:
        logger.warning(f"Failed to check quiet hours: {e}")
        return False


def is_in_cooldown(category: str) -> bool:
    """Check if a category is in cooldown."""
    prefs = load_preferences()
    state = load_notification_state()

    cooldown_minutes = prefs.get("cooldown_minutes", 30)
    last_sent = state.get("last_sent", {}).get(category, 0)

    elapsed_minutes = (time.time() - last_sent) / 60
    return elapsed_minutes < cooldown_minutes


def record_notification_sent(category: str):
    """Record that a notification was sent for cooldown tracking."""
    state = load_notification_state()
    if "last_sent" not in state:
        state["last_sent"] = {}
    state["last_sent"][category] = time.time()
    save_notification_state(state)


def is_category_enabled(category: NotificationCategory) -> bool:
    """Check if a notification category is enabled."""
    prefs = load_preferences()

    cat_str = category.value if isinstance(category, NotificationCategory) else str(category)

    # Explicit enable list takes precedence
    enabled = prefs.get("enabled_notifications", [])
    disabled = prefs.get("disabled_notifications", [])

    if cat_str in enabled:
        return True
    if cat_str in disabled:
        return False

    # Default: critical always on, everything else off
    return cat_str in ["critical", "service_down", "recovery_failed"]


def should_notify(category: NotificationCategory, force: bool = False) -> bool:
    """
    Check if a notification should be sent.

    Args:
        category: The notification category
        force: If True, bypass all checks (emergency only)

    Returns:
        True if notification should be sent
    """
    if force:
        return True

    # Check if category is enabled
    if not is_category_enabled(category):
        logger.debug(f"Notification blocked: category {category.value} disabled")
        return False

    # Check quiet hours (but allow critical)
    if category != NotificationCategory.CRITICAL and is_in_quiet_hours():
        logger.debug(f"Notification blocked: quiet hours active")
        return False

    # Check cooldown
    if is_in_cooldown(category.value):
        logger.debug(f"Notification blocked: category {category.value} in cooldown")
        return False

    return True


def send_notification(
    title: str,
    message: str,
    category: NotificationCategory = NotificationCategory.UNKNOWN,
    subtitle: str = "",
    sound: str = "default",
    force: bool = False
) -> bool:
    """
    Send a macOS notification with preference checking.

    Args:
        title: Notification title
        message: Main message body
        category: Notification category for filtering
        subtitle: Optional subtitle
        sound: Sound name (Basso, Glass, Purr, Sosumi, etc.) or "default"
        force: If True, bypass preference checks

    Returns:
        True if notification was sent, False if blocked
    """
    # Check if we should send
    if not should_notify(category, force):
        return False

    # Determine sound based on category if default
    if sound == "default":
        if category == NotificationCategory.CRITICAL:
            sound = "Sosumi"
        elif category in [NotificationCategory.SERVICE_DOWN, NotificationCategory.RECOVERY_FAILED]:
            sound = "Basso"
        elif category in [NotificationCategory.RECOVERY, NotificationCategory.LLM_RECOVERED, NotificationCategory.VOICE_RECOVERED]:
            sound = "Glass"
        elif category in [NotificationCategory.AB_TEST_DEGRADED, NotificationCategory.AB_TEST_REVERTED]:
            sound = "Sosumi"
        elif category in [NotificationCategory.AB_TEST_PROPOSED, NotificationCategory.AB_TEST_ACTIVATED]:
            sound = "Glass"
        elif category == NotificationCategory.AB_TEST_CONCLUDED:
            sound = "Purr"
        else:
            sound = "Purr"

    # Build osascript command
    # Escape quotes in strings
    title_escaped = title.replace('"', '\\"').replace("'", "\\'")[:100]
    message_escaped = message.replace('"', '\\"').replace("'", "\\'")[:200]
    subtitle_escaped = subtitle.replace('"', '\\"').replace("'", "\\'")[:100] if subtitle else ""

    script_parts = [f'display notification "{message_escaped}" with title "{title_escaped}"']
    if subtitle_escaped:
        script_parts[0] = f'display notification "{message_escaped}" with title "{title_escaped}" subtitle "{subtitle_escaped}"'
    script_parts[0] += f' sound name "{sound}"'

    script = script_parts[0]

    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5
        )

        # Record for cooldown
        record_notification_sent(category.value)

        logger.info(f"Notification sent [{category.value}]: {title}")
        return True

    except subprocess.TimeoutExpired:
        logger.warning("Notification timed out")
        return False
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")
        return False


def enable_category(category: str):
    """Enable a notification category."""
    prefs = load_preferences()

    enabled = prefs.get("enabled_notifications", [])
    disabled = prefs.get("disabled_notifications", [])

    if category not in enabled:
        enabled.append(category)
    if category in disabled:
        disabled.remove(category)

    prefs["enabled_notifications"] = enabled
    prefs["disabled_notifications"] = disabled
    save_preferences(prefs)

    print(f"Enabled notification category: {category}")


def disable_category(category: str):
    """Disable a notification category."""
    prefs = load_preferences()

    enabled = prefs.get("enabled_notifications", [])
    disabled = prefs.get("disabled_notifications", [])

    if category in enabled:
        enabled.remove(category)
    if category not in disabled:
        disabled.append(category)

    prefs["enabled_notifications"] = enabled
    prefs["disabled_notifications"] = disabled
    save_preferences(prefs)

    print(f"Disabled notification category: {category}")


def set_quiet_hours(enabled: bool, start: str = None, end: str = None):
    """Set quiet hours configuration."""
    prefs = load_preferences()

    quiet = prefs.get("quiet_hours", {})
    quiet["enabled"] = enabled
    if start:
        quiet["start"] = start
    if end:
        quiet["end"] = end

    prefs["quiet_hours"] = quiet
    save_preferences(prefs)

    if enabled:
        print(f"Quiet hours enabled: {quiet.get('start', '22:00')} - {quiet.get('end', '08:00')}")
    else:
        print("Quiet hours disabled")


def set_cooldown(minutes: int):
    """Set cooldown period between notifications."""
    prefs = load_preferences()
    prefs["cooldown_minutes"] = minutes
    save_preferences(prefs)
    print(f"Cooldown set to {minutes} minutes")


def print_status():
    """Print current notification preferences status."""
    prefs = load_preferences()

    print("=" * 60)
    print("NOTIFICATION PREFERENCES")
    print("=" * 60)

    print(f"\nCooldown: {prefs.get('cooldown_minutes', 30)} minutes")

    quiet = prefs.get("quiet_hours", {})
    if quiet.get("enabled"):
        in_quiet = is_in_quiet_hours()
        status = " (ACTIVE NOW)" if in_quiet else ""
        print(f"Quiet Hours: {quiet.get('start', '22:00')} - {quiet.get('end', '08:00')}{status}")
    else:
        print("Quiet Hours: Disabled")

    print("\nEnabled Categories:")
    for cat in prefs.get("enabled_notifications", []):
        print(f"  + {cat}")

    print("\nDisabled Categories:")
    for cat in prefs.get("disabled_notifications", []):
        print(f"  - {cat}")

    # Show available categories
    print("\nAll Available Categories:")
    for cat in NotificationCategory:
        enabled = is_category_enabled(cat)
        icon = "+" if enabled else "-"
        print(f"  {icon} {cat.value}")

    print("\n" + "=" * 60)


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print_status()
        print("\nCommands:")
        print("  status                    - Show current settings")
        print("  enable <category>         - Enable a category")
        print("  disable <category>        - Disable a category")
        print("  quiet-hours on|off        - Toggle quiet hours")
        print("  quiet-hours set HH:MM HH:MM - Set quiet hours range")
        print("  cooldown <minutes>        - Set cooldown period")
        print("  test                      - Send test notification")
        print("  test-force                - Send test (bypass checks)")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "status":
        print_status()

    elif cmd == "enable" and len(sys.argv) > 2:
        enable_category(sys.argv[2].lower())

    elif cmd == "disable" and len(sys.argv) > 2:
        disable_category(sys.argv[2].lower())

    elif cmd == "quiet-hours":
        if len(sys.argv) > 2:
            action = sys.argv[2].lower()
            if action == "on":
                set_quiet_hours(True)
            elif action == "off":
                set_quiet_hours(False)
            elif action == "set" and len(sys.argv) > 4:
                set_quiet_hours(True, sys.argv[3], sys.argv[4])
            else:
                print("Usage: quiet-hours on|off|set HH:MM HH:MM")
        else:
            print("Usage: quiet-hours on|off|set HH:MM HH:MM")

    elif cmd == "cooldown" and len(sys.argv) > 2:
        try:
            minutes = int(sys.argv[2])
            set_cooldown(minutes)
        except ValueError:
            print("Error: cooldown must be a number")

    elif cmd == "test":
        sent = send_notification(
            title="Context DNA Test",
            message="This is a test notification",
            category=NotificationCategory.INFO,
            subtitle="Testing notification system"
        )
        if sent:
            print("Test notification sent!")
        else:
            print("Test notification blocked by preferences")

    elif cmd == "test-force":
        sent = send_notification(
            title="Context DNA Test (Forced)",
            message="This notification bypassed preferences",
            category=NotificationCategory.INFO,
            subtitle="Force-sent notification",
            force=True
        )
        print("Force notification sent!" if sent else "Failed to send notification")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
