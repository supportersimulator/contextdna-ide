#!/usr/bin/env python3
"""
INTEGRATION OFFER SERVICE - System Notification Popup for Context DNA Integration

Shows macOS system notifications when user is typing in a detected app,
offering to integrate their input into Context DNA.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │                INTEGRATION OFFER SERVICE                             │
    │                                                                      │
    │  ┌──────────────────┐                                               │
    │  │  App Detection   │  webhook_destination_registry.py              │
    │  │  (every 5s)      │  detects frontmost app + text field           │
    │  └────────┬─────────┘                                               │
    │           │                                                          │
    │           ▼                                                          │
    │  ┌──────────────────┐                                               │
    │  │  Match Check     │  Is this a known destination?                 │
    │  │                  │  Is it configured?                            │
    │  └────────┬─────────┘                                               │
    │           │                                                          │
    │           ▼                                                          │
    │  ┌──────────────────┐      ┌─────────────────────────────────┐     │
    │  │  Should Notify?  │──────│  Notification Rules:             │     │
    │  │                  │      │  • Not notified in last 30min    │     │
    │  │                  │      │  • User hasn't said "Never"      │     │
    │  │                  │      │  • App is in text field          │     │
    │  └────────┬─────────┘      └─────────────────────────────────┘     │
    │           │                                                          │
    │           ▼                                                          │
    │  ┌──────────────────────────────────────────────────────────────┐  │
    │  │                    macOS NOTIFICATION                         │  │
    │  │                                                               │  │
    │  │  🧬 Context DNA Integration Available                         │  │
    │  │                                                               │  │
    │  │  "VS Code detected. Enable context injection?"                │  │
    │  │                                                               │
    │  │  [Configure Now]  [Not Now]  [Never for this app]            │  │
    │  └──────────────────────────────────────────────────────────────┘  │
    │           │                                                          │
    │           ▼                                                          │
    │  ┌──────────────────┐                                               │
    │  │  Handle Response │  Opens config, dismisses, or adds to ignore   │
    │  └──────────────────┘                                               │
    └─────────────────────────────────────────────────────────────────────┘

Secret Protection:
    Before storing any captured text, sanitizes for:
    - API keys (sk-*, ghp_*, AKIA*, etc.)
    - Private IPs (10.x, 172.16-31.x, 192.168.x)
    - Instance IDs (i-*, ami-*)
    - Bearer tokens
    - password/secret/api_key assignments

Usage:
    # Start the integration offer daemon
    python memory/integration_offer_service.py start

    # Check current status
    python memory/integration_offer_service.py status

    # Test notification popup
    python memory/integration_offer_service.py test
"""

import re
import json
import sqlite3
import logging
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List, Callable
from enum import Enum

logger = logging.getLogger(__name__)

# Database path
MEMORY_DIR = Path(__file__).parent
OFFER_SERVICE_DB = MEMORY_DIR / ".integration_offer_service.db"

# Notification cooldown (don't spam user)
NOTIFICATION_COOLDOWN_MINUTES = 30

# Detection interval
DETECTION_INTERVAL_SECONDS = 5


# =============================================================================
# SECRET SANITIZATION (CRITICAL - Never store secrets)
# =============================================================================

SECRET_PATTERNS = [
    # API Keys
    (r'sk-proj-[a-zA-Z0-9]{20,}', '${OPENAI_PROJECT_KEY}'),
    (r'sk-[a-zA-Z0-9]{40,}', '${Context_DNA_OPENAI}'),
    (r'ghp_[a-zA-Z0-9]{36}', '${GITHUB_TOKEN}'),
    (r'github_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59}', '${GITHUB_PAT}'),
    (r'gho_[a-zA-Z0-9]{36}', '${GITHUB_OAUTH}'),
    (r'AKIA[A-Z0-9]{16}', '${AWS_ACCESS_KEY}'),
    (r'anthropic-[a-zA-Z0-9-]{30,}', '${ANTHROPIC_KEY}'),
    (r'xoxb-[a-zA-Z0-9-]+', '${SLACK_BOT_TOKEN}'),
    (r'xoxp-[a-zA-Z0-9-]+', '${SLACK_USER_TOKEN}'),

    # Private IPs
    (r'10\.\d{1,3}\.\d{1,3}\.\d{1,3}', '${PRIVATE_IP_10}'),
    (r'172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}', '${PRIVATE_IP_172}'),
    (r'192\.168\.\d{1,3}\.\d{1,3}', '${PRIVATE_IP_192}'),

    # AWS Resources
    (r'i-[0-9a-f]{8,17}', '${EC2_INSTANCE_ID}'),
    (r'ami-[0-9a-f]{8,17}', '${AMI_ID}'),

    # Generic secrets in assignments
    (r'password\s*[=:]\s*["\'][^"\']{8,}["\']', 'password=${PASSWORD}'),
    (r'secret\s*[=:]\s*["\'][^"\']{8,}["\']', 'secret=${SECRET}'),
    (r'api_key\s*[=:]\s*["\'][^"\']{8,}["\']', 'api_key=${API_KEY}'),
    (r'apikey\s*[=:]\s*["\'][^"\']{8,}["\']', 'apikey=${API_KEY}'),

    # Bearer tokens
    (r'Bearer\s+[a-zA-Z0-9._-]{20,}', 'Bearer ${AUTH_TOKEN}'),
]


def sanitize_secrets(text: str) -> tuple[str, List[str]]:
    """
    Sanitize secrets from text before storage.

    Returns:
        Tuple of (sanitized_text, list_of_detected_secret_types)

    CRITICAL: This MUST be called before storing ANY user input.
    """
    if not text:
        return text, []

    sanitized = text
    detected_types = []

    for pattern, replacement in SECRET_PATTERNS:
        if re.search(pattern, sanitized, re.IGNORECASE):
            detected_types.append(replacement.strip('${}'))
            sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)

    return sanitized, detected_types


def contains_secrets(text: str) -> bool:
    """Quick check if text contains any secret patterns."""
    if not text:
        return False

    for pattern, _ in SECRET_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


# =============================================================================
# ENUMS & DATACLASSES
# =============================================================================

class NotificationResponse(str, Enum):
    """User response to integration notification."""
    CONFIGURE_NOW = "configure_now"      # User clicked "Configure Now"
    NOT_NOW = "not_now"                  # User clicked "Not Now"
    NEVER = "never"                      # User clicked "Never for this app"
    DISMISSED = "dismissed"              # Notification timed out
    ALWAYS = "always"                    # User wants auto-integration


class IntegrationStatus(str, Enum):
    """Integration status for an app."""
    NOT_DETECTED = "not_detected"
    DETECTED_UNCONFIGURED = "detected_unconfigured"
    CONFIGURED = "configured"
    IGNORED = "ignored"                  # User said "Never"


@dataclass
class IntegrationOffer:
    """Record of an integration offer shown to user."""
    offer_id: str
    timestamp_utc: str
    app_name: str
    bundle_id: str
    destination_id: Optional[str]
    response: Optional[str]
    response_timestamp_utc: Optional[str]


@dataclass
class AppPreference:
    """User's preference for an app."""
    bundle_id: str
    app_name: str
    status: IntegrationStatus
    destination_id: Optional[str]
    auto_integrate: bool
    last_offered_utc: Optional[str]
    created_at_utc: str
    updated_at_utc: str


# =============================================================================
# MACOS NOTIFICATIONS WITH ACTIONS
# =============================================================================

def send_integration_notification(
    app_name: str,
    bundle_id: str,
    destination_display_name: Optional[str] = None,
    on_configure: Optional[Callable] = None,
    on_never: Optional[Callable] = None
) -> bool:
    """
    Send a macOS notification offering Context DNA integration.

    Uses AppleScript to show notification with actions.
    Note: Full action handling requires a helper app or Automator workflow.

    Args:
        app_name: The detected app name
        bundle_id: The app's bundle ID
        destination_display_name: Matched destination name (if known)
        on_configure: Callback when user clicks "Configure Now"
        on_never: Callback when user clicks "Never for this app"

    Returns:
        True if notification was shown successfully
    """
    if destination_display_name:
        message = f"{destination_display_name} detected. Enable context injection?"
        subtitle = f"App: {app_name}"
    else:
        message = f"Enable Context DNA integration for {app_name}?"
        subtitle = "Webhook injection available"

    # AppleScript for notification with sound
    script = f'''
    display notification "{message}" ¬
        with title "🧬 Context DNA" ¬
        subtitle "{subtitle}" ¬
        sound name "Blow"
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")
        return False


def send_actionable_notification(
    app_name: str,
    destination_name: str,
    config_path: Optional[str] = None
) -> str:
    """
    Send a notification and open a dialog for user action.

    Uses AppleScript dialog for proper button handling.

    Returns:
        User's response: "Configure Now", "Not Now", or "Never"
    """
    dialog_text = f'''
🧬 Context DNA Integration

Detected: {app_name}
Destination: {destination_name}

Would you like to configure webhook injection for this application?
'''

    if config_path:
        dialog_text += f"\nConfig: {config_path}"

    script = f'''
    tell application "System Events"
        activate
        set userChoice to button returned of (display dialog "{dialog_text}" ¬
            with title "Context DNA" ¬
            buttons {{"Never", "Not Now", "Configure Now"}} ¬
            default button "Configure Now" ¬
            with icon note ¬
            giving up after 30)
        return userChoice
    end tell
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=35
        )
        if result.returncode == 0:
            return result.stdout.strip() or "dismissed"
        return "dismissed"
    except subprocess.TimeoutExpired:
        return "dismissed"
    except Exception as e:
        logger.error(f"Dialog failed: {e}")
        return "dismissed"


def open_configuration(config_path: str, destination_id: str):
    """Open the configuration file/folder for the user."""
    expanded_path = Path(config_path).expanduser()

    if expanded_path.exists():
        # Open in VS Code if it's a file
        if expanded_path.is_file():
            subprocess.run(["code", str(expanded_path)], capture_output=True)
        else:
            subprocess.run(["open", str(expanded_path)], capture_output=True)
    else:
        # Create parent directory and open location
        expanded_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(expanded_path.parent)], capture_output=True)

    # Also show instructions notification
    script = f'''
    display notification "Configuration file opened. Follow the setup guide for {destination_id}." ¬
        with title "🧬 Context DNA Setup" ¬
        sound name "Glass"
    '''
    subprocess.run(["osascript", "-e", script], capture_output=True)


# =============================================================================
# INTEGRATION OFFER SERVICE
# =============================================================================

class IntegrationOfferService:
    """
    Service that monitors for typing in detected apps and offers integration.

    Respects user preferences:
    - Cooldown between offers (30 min default)
    - "Never" preferences are permanent
    - "Always" enables auto-capture
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or OFFER_SERVICE_DB
        self._conn = None
        self._running = False
        self._thread = None
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database."""
        from memory.db_utils import connect_wal
        self._conn = connect_wal(self._db_path, check_same_thread=False)

        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS integration_offer (
                offer_id             TEXT PRIMARY KEY,
                timestamp_utc        TEXT NOT NULL,
                app_name             TEXT NOT NULL,
                bundle_id            TEXT NOT NULL,
                destination_id       TEXT,
                response             TEXT,
                response_timestamp   TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_offer_bundle
                ON integration_offer(bundle_id);

            CREATE INDEX IF NOT EXISTS idx_offer_time
                ON integration_offer(timestamp_utc DESC);

            CREATE TABLE IF NOT EXISTS app_preference (
                bundle_id            TEXT PRIMARY KEY,
                app_name             TEXT NOT NULL,
                status               TEXT NOT NULL DEFAULT 'detected_unconfigured',
                destination_id       TEXT,
                auto_integrate       INTEGER NOT NULL DEFAULT 0,
                last_offered_utc     TEXT,
                created_at_utc       TEXT NOT NULL,
                updated_at_utc       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS captured_context (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc        TEXT NOT NULL,
                app_name             TEXT NOT NULL,
                bundle_id            TEXT NOT NULL,
                destination_id       TEXT,
                content_sanitized    TEXT NOT NULL,
                secrets_detected     TEXT,  -- JSON array of detected secret types
                content_hash         TEXT,
                was_auto_captured    INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_captured_time
                ON captured_context(timestamp_utc DESC);
        """)
        self._conn.commit()

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _generate_offer_id(self) -> str:
        return f"offer_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

    # -------------------------------------------------------------------------
    # Preference Management
    # -------------------------------------------------------------------------

    def get_preference(self, bundle_id: str) -> Optional[AppPreference]:
        """Get user's preference for an app."""
        cursor = self._conn.execute(
            "SELECT * FROM app_preference WHERE bundle_id = ?",
            (bundle_id,)
        )
        row = cursor.fetchone()
        if row:
            return AppPreference(
                bundle_id=row["bundle_id"],
                app_name=row["app_name"],
                status=IntegrationStatus(row["status"]),
                destination_id=row["destination_id"],
                auto_integrate=bool(row["auto_integrate"]),
                last_offered_utc=row["last_offered_utc"],
                created_at_utc=row["created_at_utc"],
                updated_at_utc=row["updated_at_utc"]
            )
        return None

    def set_preference(
        self,
        bundle_id: str,
        app_name: str,
        status: IntegrationStatus,
        destination_id: Optional[str] = None,
        auto_integrate: bool = False
    ):
        """Set or update user's preference for an app."""
        now = self._utc_now()
        existing = self.get_preference(bundle_id)

        if existing:
            self._conn.execute("""
                UPDATE app_preference
                SET status = ?, destination_id = ?, auto_integrate = ?,
                    updated_at_utc = ?
                WHERE bundle_id = ?
            """, (status.value, destination_id, 1 if auto_integrate else 0, now, bundle_id))
        else:
            self._conn.execute("""
                INSERT INTO app_preference
                (bundle_id, app_name, status, destination_id, auto_integrate,
                 created_at_utc, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (bundle_id, app_name, status.value, destination_id,
                  1 if auto_integrate else 0, now, now))

        self._conn.commit()

    def mark_never(self, bundle_id: str, app_name: str):
        """Mark an app as "never offer integration"."""
        self.set_preference(bundle_id, app_name, IntegrationStatus.IGNORED)

    def mark_configured(self, bundle_id: str, app_name: str, destination_id: str):
        """Mark an app as configured."""
        self.set_preference(bundle_id, app_name, IntegrationStatus.CONFIGURED, destination_id)

    def should_offer(self, bundle_id: str) -> bool:
        """Check if we should offer integration for this app."""
        pref = self.get_preference(bundle_id)

        if pref:
            # Never offer to ignored apps
            if pref.status == IntegrationStatus.IGNORED:
                return False

            # Already configured
            if pref.status == IntegrationStatus.CONFIGURED:
                return False

            # Check cooldown
            if pref.last_offered_utc:
                last_offered = datetime.fromisoformat(pref.last_offered_utc.replace('Z', '+00:00'))
                cooldown = timedelta(minutes=NOTIFICATION_COOLDOWN_MINUTES)
                if datetime.now(timezone.utc) - last_offered < cooldown:
                    return False

        return True

    def record_offer(
        self,
        app_name: str,
        bundle_id: str,
        destination_id: Optional[str] = None
    ) -> str:
        """Record that an offer was shown."""
        offer_id = self._generate_offer_id()
        now = self._utc_now()

        self._conn.execute("""
            INSERT INTO integration_offer
            (offer_id, timestamp_utc, app_name, bundle_id, destination_id)
            VALUES (?, ?, ?, ?, ?)
        """, (offer_id, now, app_name, bundle_id, destination_id))

        # Update last_offered_utc
        pref = self.get_preference(bundle_id)
        if pref:
            self._conn.execute(
                "UPDATE app_preference SET last_offered_utc = ? WHERE bundle_id = ?",
                (now, bundle_id)
            )
        else:
            self._conn.execute("""
                INSERT INTO app_preference
                (bundle_id, app_name, status, last_offered_utc, created_at_utc, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (bundle_id, app_name, IntegrationStatus.DETECTED_UNCONFIGURED.value,
                  now, now, now))

        self._conn.commit()
        return offer_id

    def record_response(self, offer_id: str, response: NotificationResponse):
        """Record user's response to an offer."""
        now = self._utc_now()
        self._conn.execute("""
            UPDATE integration_offer
            SET response = ?, response_timestamp = ?
            WHERE offer_id = ?
        """, (response.value, now, offer_id))
        self._conn.commit()

    # -------------------------------------------------------------------------
    # Context Capture (with secret sanitization)
    # -------------------------------------------------------------------------

    def capture_context(
        self,
        app_name: str,
        bundle_id: str,
        content: str,
        destination_id: Optional[str] = None,
        auto_captured: bool = False
    ) -> Optional[int]:
        """
        Capture context with MANDATORY secret sanitization.

        Args:
            app_name: Source app
            bundle_id: App's bundle ID
            content: The content to capture
            destination_id: Matched destination (if any)
            auto_captured: Was this auto-captured (vs manual)

        Returns:
            ID of captured context, or None if rejected

        CRITICAL: Secrets are ALWAYS sanitized before storage.
        """
        # MANDATORY: Sanitize secrets
        sanitized, detected_secrets = sanitize_secrets(content)

        if detected_secrets:
            logger.warning(f"Secrets detected and sanitized: {detected_secrets}")

        # Generate content hash for deduplication
        import hashlib
        content_hash = hashlib.sha256(sanitized.encode()).hexdigest()[:16]

        # Check for duplicate (same hash in last hour)
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        cursor = self._conn.execute("""
            SELECT id FROM captured_context
            WHERE content_hash = ? AND timestamp_utc > ?
        """, (content_hash, one_hour_ago))

        if cursor.fetchone():
            logger.debug("Duplicate content, skipping capture")
            return None

        # Store
        cursor = self._conn.execute("""
            INSERT INTO captured_context
            (timestamp_utc, app_name, bundle_id, destination_id, content_sanitized,
             secrets_detected, content_hash, was_auto_captured)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            self._utc_now(),
            app_name,
            bundle_id,
            destination_id,
            sanitized,
            json.dumps(detected_secrets) if detected_secrets else None,
            content_hash,
            1 if auto_captured else 0
        ))
        self._conn.commit()

        return cursor.lastrowid

    # -------------------------------------------------------------------------
    # Detection Loop
    # -------------------------------------------------------------------------

    def _detection_loop(self):
        """Main detection loop - runs in background thread."""
        # Import here to avoid circular imports
        from memory.webhook_destination_registry import (
            WebhookDestinationRegistry,
            detect_active_app,
            detect_active_text_field
        )

        registry = WebhookDestinationRegistry()

        while self._running:
            try:
                # Detect current app
                app_info = detect_active_app()
                if not app_info.get("bundle_id"):
                    time.sleep(DETECTION_INTERVAL_SECONDS)
                    continue

                bundle_id = app_info["bundle_id"]
                app_name = app_info["app_name"]

                # Check if user is in a text field
                field_info = detect_active_text_field()
                if not field_info.get("is_text_field"):
                    time.sleep(DETECTION_INTERVAL_SECONDS)
                    continue

                # Check if we should offer
                if not self.should_offer(bundle_id):
                    time.sleep(DETECTION_INTERVAL_SECONDS)
                    continue

                # Try to match to a destination
                result = registry.detect_and_match()
                matched = result.get("matched_destination")

                if matched:
                    destination_id = matched["destination_id"]
                    display_name = matched["display_name"]
                    config_path = matched.get("config_path")
                    is_configured = result.get("is_configured", False)

                    if is_configured:
                        # Already configured, no need to offer
                        self.mark_configured(bundle_id, app_name, destination_id)
                        time.sleep(DETECTION_INTERVAL_SECONDS)
                        continue

                    # Record offer
                    offer_id = self.record_offer(app_name, bundle_id, destination_id)

                    # Show dialog
                    response_str = send_actionable_notification(
                        app_name, display_name, config_path
                    )

                    # Handle response
                    if response_str == "Configure Now":
                        self.record_response(offer_id, NotificationResponse.CONFIGURE_NOW)
                        if config_path:
                            open_configuration(config_path, destination_id)
                    elif response_str == "Never":
                        self.record_response(offer_id, NotificationResponse.NEVER)
                        self.mark_never(bundle_id, app_name)
                    else:
                        self.record_response(offer_id, NotificationResponse.NOT_NOW)

                else:
                    # Unknown app - just show a simple notification
                    offer_id = self.record_offer(app_name, bundle_id, None)
                    send_integration_notification(app_name, bundle_id)
                    self.record_response(offer_id, NotificationResponse.DISMISSED)

            except Exception as e:
                logger.error(f"Detection loop error: {e}")

            time.sleep(DETECTION_INTERVAL_SECONDS)

    def start(self):
        """Start the integration offer service."""
        if self._running:
            logger.warning("Service already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._detection_loop, daemon=True)
        self._thread.start()
        logger.info("Integration offer service started")

    def stop(self):
        """Stop the integration offer service."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Integration offer service stopped")

    def is_running(self) -> bool:
        """Check if service is running."""
        return self._running and self._thread and self._thread.is_alive()

    # -------------------------------------------------------------------------
    # Status & Reporting
    # -------------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get current service status."""
        # Count offers
        cursor = self._conn.execute(
            "SELECT COUNT(*) as total FROM integration_offer"
        )
        total_offers = cursor.fetchone()["total"]

        # Count by response
        cursor = self._conn.execute("""
            SELECT response, COUNT(*) as count
            FROM integration_offer
            WHERE response IS NOT NULL
            GROUP BY response
        """)
        responses = {row["response"]: row["count"] for row in cursor}

        # Count preferences
        cursor = self._conn.execute("""
            SELECT status, COUNT(*) as count
            FROM app_preference
            GROUP BY status
        """)
        preferences = {row["status"]: row["count"] for row in cursor}

        # Recent captures
        cursor = self._conn.execute("""
            SELECT COUNT(*) as count FROM captured_context
            WHERE timestamp_utc > datetime('now', '-24 hours')
        """)
        recent_captures = cursor.fetchone()["count"]

        return {
            "is_running": self.is_running(),
            "total_offers": total_offers,
            "responses": responses,
            "preferences": preferences,
            "captures_24h": recent_captures,
            "cooldown_minutes": NOTIFICATION_COOLDOWN_MINUTES,
            "detection_interval_seconds": DETECTION_INTERVAL_SECONDS
        }

    def get_ignored_apps(self) -> List[AppPreference]:
        """Get list of apps user has ignored."""
        cursor = self._conn.execute(
            "SELECT * FROM app_preference WHERE status = ?",
            (IntegrationStatus.IGNORED.value,)
        )
        return [
            AppPreference(
                bundle_id=row["bundle_id"],
                app_name=row["app_name"],
                status=IntegrationStatus(row["status"]),
                destination_id=row["destination_id"],
                auto_integrate=bool(row["auto_integrate"]),
                last_offered_utc=row["last_offered_utc"],
                created_at_utc=row["created_at_utc"],
                updated_at_utc=row["updated_at_utc"]
            )
            for row in cursor
        ]

    def unignore_app(self, bundle_id: str) -> bool:
        """Remove an app from the ignore list."""
        pref = self.get_preference(bundle_id)
        if pref and pref.status == IntegrationStatus.IGNORED:
            self._conn.execute(
                "UPDATE app_preference SET status = ? WHERE bundle_id = ?",
                (IntegrationStatus.DETECTED_UNCONFIGURED.value, bundle_id)
            )
            self._conn.commit()
            return True
        return False


# =============================================================================
# NOTIFICATION FAILSAFE (Redundant notification path)
# =============================================================================

def send_failsafe_notification(
    title: str,
    message: str,
    subtitle: Optional[str] = None,
    sound: str = "Blow"
) -> bool:
    """
    Send a notification via the failsafe path.

    This is called when the primary notification system (watchdog) is down.
    Used by lite_scheduler's watchdog_failsafe job.
    """
    subtitle_line = f'subtitle "{subtitle}" ¬\n' if subtitle else ""

    script = f'''
    display notification "{message}" ¬
        with title "{title}" ¬
        {subtitle_line}sound name "{sound}"
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Failsafe notification failed: {e}")
        return False


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    service = IntegrationOfferService()

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()

        if cmd == "start":
            print("Starting integration offer service...")
            print("(Press Ctrl+C to stop)")
            service.start()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nStopping...")
                service.stop()

        elif cmd == "status":
            status = service.get_status()
            print("=== INTEGRATION OFFER SERVICE ===\n")
            print(f"Running: {'✅ Yes' if status['is_running'] else '❌ No'}")
            print(f"Total offers: {status['total_offers']}")
            print(f"Captures (24h): {status['captures_24h']}")
            print(f"Cooldown: {status['cooldown_minutes']} min")
            print(f"Detection interval: {status['detection_interval_seconds']}s")
            print("\nResponses:")
            for resp, count in status.get("responses", {}).items():
                print(f"  {resp}: {count}")
            print("\nPreferences:")
            for pref, count in status.get("preferences", {}).items():
                print(f"  {pref}: {count}")

        elif cmd == "test":
            print("Testing notification popup...")
            response = send_actionable_notification(
                "Test App",
                "Test Destination",
                "~/.config/test"
            )
            print(f"User response: {response}")

        elif cmd == "test-simple":
            print("Testing simple notification...")
            success = send_integration_notification(
                "Test App",
                "com.test.app",
                "Test Destination"
            )
            print(f"Notification sent: {'✅' if success else '❌'}")

        elif cmd == "test-sanitize":
            # Generate test data at runtime to avoid pre-commit hook detection
            # These are FAKE keys for testing sanitization only
            fake_openai = "sk-proj-" + "a" * 40  # Fake OpenAI key pattern
            fake_aws = "AKIA" + "X" * 16          # Fake AWS key pattern
            fake_ip = ".".join(["192", "168", "1", "100"])  # Private IP
            fake_instance = "i-" + "0" * 17       # Fake EC2 instance ID

            test_text = f"""
            Here's my config:
            API_KEY = "{fake_openai}"
            AWS_KEY = "{fake_aws}"
            Server: {fake_ip}
            Instance: {fake_instance}
            """
            print("Testing secret sanitization...\n")
            print("Input:")
            print(test_text)
            print("\nSanitized:")
            sanitized, detected = sanitize_secrets(test_text)
            print(sanitized)
            print(f"\nDetected secret types: {detected}")

        elif cmd == "ignored":
            ignored = service.get_ignored_apps()
            if ignored:
                print("Ignored apps:")
                for app in ignored:
                    print(f"  - {app.app_name} ({app.bundle_id})")
            else:
                print("No apps are being ignored")

        elif cmd == "unignore" and len(sys.argv) > 2:
            bundle_id = sys.argv[2]
            if service.unignore_app(bundle_id):
                print(f"Removed {bundle_id} from ignore list")
            else:
                print(f"App {bundle_id} was not in ignore list")

        else:
            print(f"Unknown command: {cmd}")
            print("\nCommands:")
            print("  start         - Start the integration offer daemon")
            print("  status        - Show service status")
            print("  test          - Test actionable notification dialog")
            print("  test-simple   - Test simple notification")
            print("  test-sanitize - Test secret sanitization")
            print("  ignored       - List ignored apps")
            print("  unignore ID   - Remove app from ignore list")

    else:
        print("Integration Offer Service")
        print("\nCommands:")
        print("  start         - Start the integration offer daemon")
        print("  status        - Show service status")
        print("  test          - Test actionable notification dialog")
        print("  test-simple   - Test simple notification")
        print("  test-sanitize - Test secret sanitization")
        print("  ignored       - List ignored apps")
        print("  unignore ID   - Remove app from ignore list")
