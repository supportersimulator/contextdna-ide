"""Setup module for Context DNA - Configuration checking and guided setup."""

from context_dna.setup.checker import (
    SetupChecker,
    SetupStatus,
    check_all,
    get_missing_configs,
    get_setup_commands,
)

from context_dna.setup.notifications import (
    notify,
    notify_setup_needed,
    notify_success,
    notify_error,
    NotificationManager,
)

__all__ = [
    # Checker
    "SetupChecker",
    "SetupStatus",
    "check_all",
    "get_missing_configs",
    "get_setup_commands",
    # Notifications
    "notify",
    "notify_setup_needed",
    "notify_success",
    "notify_error",
    "NotificationManager",
]
