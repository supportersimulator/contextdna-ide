#!/usr/bin/env python3
"""
WEBHOOK SECTION NOTIFICATIONS - macOS Notification System for Webhook Health

Provides granular notifications for webhook injection failures:
- Per-destination notifications (Claude Code, Cursor, Synaptic Chat, etc.)
- Per-section notifications (Sections 0-8)
- User-mutable notifications via dashboard
- Integration with ecosystem_health daemon

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │                WEBHOOK SECTION NOTIFICATIONS                         │
    │                                                                      │
    │  ┌──────────────────────────────────────────────────────────────┐   │
    │  │  NOTIFICATION PREFERENCES (SQLite)                            │   │
    │  │  ┌───────────────────────────────────────────────────────┐   │   │
    │  │  │ destination_id | section_id | muted | muted_until_utc │   │   │
    │  │  │ vs_code_claude | 0          | false | null            │   │   │
    │  │  │ vs_code_claude | 8          | true  | 2026-02-05T...  │   │   │
    │  │  └───────────────────────────────────────────────────────┘   │   │
    │  └──────────────────────────────────────────────────────────────┘   │
    │                                                                      │
    │  ┌──────────────────────────────────────────────────────────────┐   │
    │  │  HEALTH TRACKER                                               │   │
    │  │  Per destination × section:                                   │   │
    │  │    - Last healthy timestamp                                   │   │
    │  │    - Consecutive failures                                     │   │
    │  │    - Last notification sent                                   │   │
    │  └──────────────────────────────────────────────────────────────┘   │
    │                                                                      │
    │  ┌──────────────────────────────────────────────────────────────┐   │
    │  │  macOS NOTIFICATIONS                                          │   │
    │  │                                                               │   │
    │  │  "⚠️ Claude Code Section 8 (8TH_INTELLIGENCE)"                │   │
    │  │  "pre_message webhook: Section missing for 5 minutes"         │   │
    │  │  [Mute] [Investigate]                                         │   │
    │  └──────────────────────────────────────────────────────────────┘   │
    └─────────────────────────────────────────────────────────────────────┘

Usage:
    from memory.webhook_section_notifications import (
        WebhookSectionNotifier,
        record_section_health,
        mute_notification,
        get_notification_preferences
    )

    # Record section health
    record_section_health(
        destination="vs_code_claude_code",
        section_id=8,
        healthy=True,
        phase="pre_message"
    )

    # Mute notifications for a specific destination/section
    mute_notification("vs_code_claude_code", section_id=8, duration_hours=24)

    # Get mute preferences for dashboard
    prefs = get_notification_preferences()
"""

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, List, Any, Literal

logger = logging.getLogger(__name__)

# Database paths
MEMORY_DIR = Path(__file__).parent
NOTIFICATION_DB = MEMORY_DIR / ".webhook_notification_prefs.db"
BLOB_STORE_DB = MEMORY_DIR / ".payload_blob_store.db"

# Notification cooldown (don't spam same notification)
NOTIFICATION_COOLDOWN_MINUTES = 15

# Failure threshold before notifying
CONSECUTIVE_FAILURES_THRESHOLD = 2

# Check interval (recommended for daemon)
CHECK_INTERVAL_SECONDS = 60


# =============================================================================
# SECTION AND DESTINATION DEFINITIONS
# =============================================================================

SECTIONS = {
    0: {"name": "SAFETY", "critical": True, "description": "Risk classification"},
    1: {"name": "FOUNDATION", "critical": True, "description": "File context, SOPs"},
    2: {"name": "WISDOM", "critical": False, "description": "Professor wisdom"},
    3: {"name": "AWARENESS", "critical": False, "description": "Recent changes"},
    4: {"name": "DEEP_CONTEXT", "critical": False, "description": "Blueprint, brain state"},
    5: {"name": "PROTOCOL", "critical": False, "description": "Success capture reminder"},
    6: {"name": "HOLISTIC_CONTEXT", "critical": False, "description": "Synaptic → Atlas"},
    7: {"name": "FULL_LIBRARY", "critical": False, "description": "Extended context (escalation)"},
    8: {"name": "8TH_INTELLIGENCE", "critical": True, "description": "Synaptic → Aaron (NEVER SLEEPS)"},
}

# Default/known destinations - system will auto-register new ones dynamically
DEFAULT_DESTINATIONS = {
    "vs_code_claude_code": {"display": "Claude Code (VS Code)", "primary": True, "category": "ide"},
    "synaptic_chat": {"display": "Synaptic Chat", "primary": True, "category": "chat"},
    "chatgpt_app": {"display": "ChatGPT App", "primary": False, "category": "chat"},
    "cursor_overseer": {"display": "Cursor Overseer", "primary": False, "category": "ide"},
    "antigravity": {"display": "Antigravity", "primary": False, "category": "ide"},
    "hermes": {"display": "Hermes", "primary": False, "category": "agent"},
    "windsurf": {"display": "Windsurf", "primary": False, "category": "ide"},
}

# For backwards compatibility (will be replaced by dynamic destinations)
DESTINATIONS = DEFAULT_DESTINATIONS


@dataclass
class SectionHealthEvent:
    """A single section health observation."""
    destination: str
    section_id: int
    phase: str  # pre_message or post_message
    healthy: bool
    timestamp_utc: str
    latency_ms: Optional[int] = None
    error_message: Optional[str] = None
    payload_sha256: Optional[str] = None  # Reference to blob store for auditability
    section_sha256: Optional[str] = None  # Reference to per-section blob


@dataclass
class NotificationPreference:
    """User's notification preference for a destination/section."""
    destination: str
    section_id: Optional[int]  # None = all sections for this destination
    muted: bool
    muted_until_utc: Optional[str] = None
    muted_reason: Optional[str] = None


@dataclass
class SectionHealthStatus:
    """Current health status for a destination/section/phase combination."""
    destination: str
    section_id: int
    phase: str
    status: Literal["healthy", "degraded", "failing", "unknown"]
    consecutive_failures: int
    last_healthy_utc: Optional[str]
    last_failure_utc: Optional[str]
    last_notification_utc: Optional[str]
    failure_reason: Optional[str] = None
    last_payload_sha256: Optional[str] = None  # For "what failed" auditability
    last_section_sha256: Optional[str] = None  # Per-section content at failure time


# =============================================================================
# NOTIFICATION PREFERENCES STORE
# =============================================================================

class NotificationPreferencesStore:
    """
    SQLite-backed store for notification muting preferences.

    Allows users to mute specific notifications via dashboard.
    """

    def __init__(self, db_path: Path = NOTIFICATION_DB):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        """Initialize database tables."""
        from memory.db_utils import connect_wal
        self._conn = connect_wal(self._db_path, check_same_thread=False)

        self._conn.executescript("""
            -- Notification preferences (mute settings)
            CREATE TABLE IF NOT EXISTS notification_preference (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                destination TEXT NOT NULL,
                section_id INTEGER,  -- NULL means all sections for destination
                phase TEXT,          -- NULL means all phases
                muted INTEGER NOT NULL DEFAULT 0,
                muted_until_utc TEXT,
                muted_reason TEXT,
                created_utc TEXT NOT NULL,
                updated_utc TEXT NOT NULL,
                UNIQUE(destination, section_id, phase)
            );

            -- Section health tracking (with blob store references)
            CREATE TABLE IF NOT EXISTS section_health_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                destination TEXT NOT NULL,
                section_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_healthy_utc TEXT,
                last_failure_utc TEXT,
                last_notification_utc TEXT,
                failure_reason TEXT,
                last_payload_sha256 TEXT,   -- Reference to payload_blob for "what failed"
                last_section_sha256 TEXT,   -- Reference to section_blob for "what was in this section"
                updated_utc TEXT NOT NULL,
                UNIQUE(destination, section_id, phase)
            );

            -- Notification history (for analytics, with blob references)
            CREATE TABLE IF NOT EXISTS notification_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                destination TEXT NOT NULL,
                section_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_sha256 TEXT,  -- What payload was injected when failure occurred
                section_sha256 TEXT,  -- What section content was at failure time
                sent_utc TEXT NOT NULL
            );

            -- Payload blob store (immutable, keyed by SHA256)
            -- Stores exact injected payloads for deterministic replay
            CREATE TABLE IF NOT EXISTS payload_blob (
                payload_sha256 TEXT PRIMARY KEY,
                created_at_utc TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text/markdown',
                compressed INTEGER NOT NULL DEFAULT 0,
                compression_algo TEXT,
                byte_size_raw INTEGER,
                byte_size_stored INTEGER,
                payload_text TEXT,
                payload_json TEXT,
                destination TEXT,     -- Which destination received this payload
                notes TEXT,
                CHECK (
                    (payload_text IS NOT NULL AND payload_json IS NULL)
                    OR
                    (payload_text IS NULL AND payload_json IS NOT NULL)
                )
            );

            -- Section blob store (immutable, keyed by SHA256)
            -- Stores per-section content for "first divergence" diffing
            CREATE TABLE IF NOT EXISTS section_blob (
                content_sha256 TEXT PRIMARY KEY,
                created_at_utc TEXT NOT NULL,
                section_id INTEGER NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text/markdown',
                compressed INTEGER NOT NULL DEFAULT 0,
                compression_algo TEXT,
                byte_size_raw INTEGER,
                byte_size_stored INTEGER,
                content_text TEXT,
                content_json TEXT,
                destination TEXT,     -- Which destination this section was for
                notes TEXT,
                CHECK (
                    (content_text IS NOT NULL AND content_json IS NULL)
                    OR
                    (content_text IS NULL AND content_json IS NOT NULL)
                )
            );

            -- Dynamic destination registry (evolves as users add tools)
            CREATE TABLE IF NOT EXISTS destination_registry (
                destination_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                category TEXT DEFAULT 'other',   -- ide, chat, agent, mcp, other
                primary_dest INTEGER DEFAULT 0,   -- 1 = primary, 0 = secondary
                auto_registered INTEGER DEFAULT 0, -- 1 = auto-discovered, 0 = predefined
                webhook_url TEXT,                 -- Optional webhook endpoint
                enabled INTEGER DEFAULT 1,        -- Whether to monitor this destination
                created_utc TEXT NOT NULL,
                last_seen_utc TEXT,
                total_injections INTEGER DEFAULT 0,
                successful_injections INTEGER DEFAULT 0,
                failed_injections INTEGER DEFAULT 0,
                notes TEXT
            );

            -- OS Platform tracking (per destination)
            -- Tracks same destination across different OS platforms
            CREATE TABLE IF NOT EXISTS destination_platform (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                destination_id TEXT NOT NULL,
                os_platform TEXT NOT NULL,        -- darwin, linux, windows, ios, android, web
                os_version TEXT,                  -- e.g., "14.2", "24.04", "11"
                arch TEXT,                        -- arm64, x86_64, arm, x86
                device_name TEXT,                 -- Optional device identifier
                computer_number INTEGER,          -- Which computer this is (1, 2, 3, etc.)
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                total_injections INTEGER DEFAULT 0,
                successful_injections INTEGER DEFAULT 0,
                failed_injections INTEGER DEFAULT 0,
                avg_latency_ms INTEGER,
                UNIQUE(destination_id, os_platform, arch)
            );

            -- Account/Organization registry (enterprise support)
            -- Supports: personal accounts, team accounts, enterprise accounts
            CREATE TABLE IF NOT EXISTS account_registry (
                account_id TEXT PRIMARY KEY,      -- Unique account identifier
                account_type TEXT NOT NULL DEFAULT 'personal', -- personal, team, enterprise
                account_name TEXT,                -- Display name (e.g., "Acme Corp")
                max_users INTEGER DEFAULT 1,      -- Max users allowed (1 for personal, N for enterprise)
                max_computers_per_user INTEGER DEFAULT 5, -- Max computers per user
                subscription_tier TEXT DEFAULT 'free', -- free, pro, team, enterprise
                created_utc TEXT NOT NULL,
                last_activity_utc TEXT,
                is_active INTEGER DEFAULT 1,
                settings_json TEXT,               -- Account-level settings (JSON)
                notes TEXT
            );

            -- User registry (users within accounts)
            -- For personal accounts: 1 user. For enterprise: many users.
            CREATE TABLE IF NOT EXISTS user_registry (
                user_id TEXT PRIMARY KEY,         -- Unique user identifier
                account_id TEXT NOT NULL,         -- Which account this user belongs to
                user_number INTEGER NOT NULL,     -- Sequential within account: 1, 2, 3
                email TEXT,                       -- User email (for identification)
                display_name TEXT,                -- User's display name
                role TEXT DEFAULT 'user',         -- owner, admin, user
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                preferences_json TEXT,            -- User-level preferences (JSON)
                UNIQUE(account_id, user_number)
            );

            -- Computer registry (tracks all computers per user)
            -- Supports multi-computer setups with shared DB
            CREATE TABLE IF NOT EXISTS computer_registry (
                computer_id TEXT PRIMARY KEY,     -- Unique identifier (UUID or hash)
                user_id TEXT,                     -- Which user this computer belongs to
                account_id TEXT,                  -- Which account (for direct lookup)
                computer_number INTEGER NOT NULL, -- Sequential within account: 1, 2, 3, etc.
                hostname TEXT,                    -- Machine hostname
                os_platform TEXT,                 -- darwin, linux, windows
                os_version TEXT,                  -- OS version
                arch TEXT,                        -- arm64, x86_64
                friendly_name TEXT,               -- User-assigned name (e.g., "Work MacBook")
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                total_sessions INTEGER DEFAULT 1,
                is_active INTEGER DEFAULT 1,      -- Can be deactivated if sold/retired
                notes TEXT,
                UNIQUE(account_id, computer_number)
            );

            -- Webhook phase tracking per destination
            CREATE TABLE IF NOT EXISTS destination_phase_config (
                destination_id TEXT NOT NULL,
                phase TEXT NOT NULL,              -- pre_message, post_message
                enabled INTEGER DEFAULT 1,
                last_execution_utc TEXT,
                avg_latency_ms INTEGER,
                notes TEXT,
                PRIMARY KEY (destination_id, phase)
            );

            -- Create indexes
            CREATE INDEX IF NOT EXISTS idx_pref_dest ON notification_preference(destination);
            CREATE INDEX IF NOT EXISTS idx_health_dest ON section_health_state(destination, section_id);
            CREATE INDEX IF NOT EXISTS idx_notif_sent ON notification_history(sent_utc);
            CREATE INDEX IF NOT EXISTS idx_payload_blob_created ON payload_blob(created_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_section_blob_created ON section_blob(created_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_section_blob_section ON section_blob(section_id);
            CREATE INDEX IF NOT EXISTS idx_dest_category ON destination_registry(category);
        """)
        self._conn.commit()

        # Seed default destinations
        self._seed_default_destinations()

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _seed_default_destinations(self):
        """Seed default destinations on first init."""
        now = self._utc_now()
        for dest_id, info in DEFAULT_DESTINATIONS.items():
            self._conn.execute("""
                INSERT OR IGNORE INTO destination_registry
                    (destination_id, display_name, category, primary_dest, auto_registered, created_utc)
                VALUES (?, ?, ?, ?, 0, ?)
            """, (dest_id, info["display"], info.get("category", "other"),
                  1 if info.get("primary") else 0, now))
        self._conn.commit()

    # -------------------------------------------------------------------------
    # Dynamic Destination Registry
    # -------------------------------------------------------------------------

    def register_destination(
        self,
        destination_id: str,
        display_name: Optional[str] = None,
        category: str = "other",
        webhook_url: Optional[str] = None,
        primary: bool = False
    ) -> Dict[str, Any]:
        """
        Register a new destination or update existing one.

        Called automatically when destinations are first seen in webhooks.
        Users can also manually register via dashboard.

        Args:
            destination_id: Unique ID (e.g., "my_custom_ide")
            display_name: Human-readable name (defaults to destination_id)
            category: ide, chat, agent, mcp, other
            webhook_url: Optional webhook endpoint for this destination
            primary: Whether this is a primary destination

        Returns:
            Destination info dict
        """
        now = self._utc_now()
        display = display_name or destination_id.replace("_", " ").title()

        # Check if exists
        cursor = self._conn.execute("""
            SELECT * FROM destination_registry WHERE destination_id = ?
        """, (destination_id,))
        existing = cursor.fetchone()

        if existing:
            # Update last_seen
            self._conn.execute("""
                UPDATE destination_registry
                SET last_seen_utc = ?
                WHERE destination_id = ?
            """, (now, destination_id))
            self._conn.commit()

            return {
                "destination_id": existing["destination_id"],
                "display_name": existing["display_name"],
                "category": existing["category"],
                "primary": bool(existing["primary_dest"]),
                "auto_registered": bool(existing["auto_registered"]),
                "enabled": bool(existing["enabled"]),
                "newly_registered": False
            }

        # Insert new destination (auto-registered)
        self._conn.execute("""
            INSERT INTO destination_registry
                (destination_id, display_name, category, primary_dest,
                 auto_registered, webhook_url, created_utc, last_seen_utc)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
        """, (destination_id, display, category, 1 if primary else 0,
              webhook_url, now, now))
        self._conn.commit()

        logger.info(f"Auto-registered new destination: {destination_id} ({display})")

        return {
            "destination_id": destination_id,
            "display_name": display,
            "category": category,
            "primary": primary,
            "auto_registered": True,
            "enabled": True,
            "newly_registered": True
        }

    def get_all_destinations(self) -> List[Dict[str, Any]]:
        """Get all registered destinations for dashboard."""
        cursor = self._conn.execute("""
            SELECT * FROM destination_registry
            ORDER BY primary_dest DESC, category, display_name
        """)

        destinations = []
        for row in cursor:
            destinations.append({
                "destination_id": row["destination_id"],
                "display_name": row["display_name"],
                "category": row["category"],
                "primary": bool(row["primary_dest"]),
                "auto_registered": bool(row["auto_registered"]),
                "enabled": bool(row["enabled"]),
                "webhook_url": row["webhook_url"],
                "created_utc": row["created_utc"],
                "last_seen_utc": row["last_seen_utc"],
                "total_injections": row["total_injections"] or 0,
                "successful_injections": row["successful_injections"] or 0,
                "failed_injections": row["failed_injections"] or 0,
                "notes": row["notes"]
            })

        return destinations

    def update_destination_stats(
        self,
        destination_id: str,
        success: bool
    ):
        """Update injection stats for a destination."""
        now = self._utc_now()
        if success:
            self._conn.execute("""
                UPDATE destination_registry
                SET total_injections = total_injections + 1,
                    successful_injections = successful_injections + 1,
                    last_seen_utc = ?
                WHERE destination_id = ?
            """, (now, destination_id))
        else:
            self._conn.execute("""
                UPDATE destination_registry
                SET total_injections = total_injections + 1,
                    failed_injections = failed_injections + 1,
                    last_seen_utc = ?
                WHERE destination_id = ?
            """, (now, destination_id))
        self._conn.commit()

    def set_destination_enabled(self, destination_id: str, enabled: bool):
        """Enable or disable monitoring for a destination."""
        self._conn.execute("""
            UPDATE destination_registry
            SET enabled = ?
            WHERE destination_id = ?
        """, (1 if enabled else 0, destination_id))
        self._conn.commit()

    # -------------------------------------------------------------------------
    # OS Platform Tracking
    # -------------------------------------------------------------------------

    def record_platform_injection(
        self,
        destination_id: str,
        os_platform: str,
        success: bool,
        os_version: Optional[str] = None,
        arch: Optional[str] = None,
        device_name: Optional[str] = None,
        latency_ms: Optional[int] = None
    ):
        """
        Record an injection event for a specific OS platform.

        This tracks the same destination (e.g., "vs_code_claude_code") across
        different platforms (macOS, Windows, Linux).

        Args:
            destination_id: The destination ID
            os_platform: darwin, linux, windows, ios, android, web
            success: Whether injection succeeded
            os_version: OS version (e.g., "14.2", "11")
            arch: Architecture (arm64, x86_64)
            device_name: Optional device identifier
            latency_ms: Injection latency in milliseconds
        """
        now = self._utc_now()
        arch = arch or "unknown"

        # Check if exists
        cursor = self._conn.execute("""
            SELECT id, total_injections, successful_injections, failed_injections, avg_latency_ms
            FROM destination_platform
            WHERE destination_id = ? AND os_platform = ? AND arch = ?
        """, (destination_id, os_platform, arch))
        existing = cursor.fetchone()

        if existing:
            # Update existing record
            total = existing["total_injections"] + 1
            successful = existing["successful_injections"] + (1 if success else 0)
            failed = existing["failed_injections"] + (0 if success else 1)

            # Update rolling average latency
            if latency_ms is not None:
                old_avg = existing["avg_latency_ms"] or latency_ms
                new_avg = int((old_avg * existing["total_injections"] + latency_ms) / total)
            else:
                new_avg = existing["avg_latency_ms"]

            self._conn.execute("""
                UPDATE destination_platform
                SET last_seen_utc = ?,
                    total_injections = ?,
                    successful_injections = ?,
                    failed_injections = ?,
                    avg_latency_ms = ?,
                    os_version = COALESCE(?, os_version),
                    device_name = COALESCE(?, device_name)
                WHERE id = ?
            """, (now, total, successful, failed, new_avg, os_version, device_name, existing["id"]))
        else:
            # Insert new platform record
            self._conn.execute("""
                INSERT INTO destination_platform
                    (destination_id, os_platform, os_version, arch, device_name,
                     first_seen_utc, last_seen_utc, total_injections,
                     successful_injections, failed_injections, avg_latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """, (destination_id, os_platform, os_version, arch, device_name,
                  now, now, 1 if success else 0, 0 if success else 1, latency_ms))

            logger.info(f"New platform detected: {destination_id} on {os_platform}/{arch}")

        self._conn.commit()

    def get_platform_stats(self, destination_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get platform statistics for dashboard."""
        if destination_id:
            cursor = self._conn.execute("""
                SELECT dp.*, dr.display_name
                FROM destination_platform dp
                JOIN destination_registry dr ON dp.destination_id = dr.destination_id
                WHERE dp.destination_id = ?
                ORDER BY dp.total_injections DESC
            """, (destination_id,))
        else:
            cursor = self._conn.execute("""
                SELECT dp.*, dr.display_name
                FROM destination_platform dp
                JOIN destination_registry dr ON dp.destination_id = dr.destination_id
                ORDER BY dp.destination_id, dp.total_injections DESC
            """)

        platforms = []
        for row in cursor:
            success_rate = 0
            if row["total_injections"] > 0:
                success_rate = (row["successful_injections"] / row["total_injections"]) * 100

            platforms.append({
                "destination_id": row["destination_id"],
                "display_name": row["display_name"],
                "os_platform": row["os_platform"],
                "os_version": row["os_version"],
                "arch": row["arch"],
                "device_name": row["device_name"],
                "first_seen_utc": row["first_seen_utc"],
                "last_seen_utc": row["last_seen_utc"],
                "total_injections": row["total_injections"],
                "successful_injections": row["successful_injections"],
                "failed_injections": row["failed_injections"],
                "success_rate": round(success_rate, 1),
                "avg_latency_ms": row["avg_latency_ms"]
            })

        return platforms

    def get_platform_summary(self) -> Dict[str, Any]:
        """Get high-level platform summary for dashboard."""
        cursor = self._conn.execute("""
            SELECT
                os_platform,
                COUNT(DISTINCT destination_id) as destinations,
                SUM(total_injections) as total,
                SUM(successful_injections) as successful,
                SUM(failed_injections) as failed,
                AVG(avg_latency_ms) as avg_latency
            FROM destination_platform
            GROUP BY os_platform
            ORDER BY total DESC
        """)

        platforms = {}
        for row in cursor:
            success_rate = 0
            if row["total"] > 0:
                success_rate = (row["successful"] / row["total"]) * 100

            platforms[row["os_platform"]] = {
                "destinations": row["destinations"],
                "total_injections": row["total"],
                "successful_injections": row["successful"],
                "failed_injections": row["failed"],
                "success_rate": round(success_rate, 1),
                "avg_latency_ms": int(row["avg_latency"]) if row["avg_latency"] else None
            }

        return platforms

    # -------------------------------------------------------------------------
    # Computer Registry (Multi-Computer Support)
    # -------------------------------------------------------------------------

    def _get_computer_id(self) -> str:
        """
        Generate a unique identifier for this computer.

        Uses a combination of:
        - Platform UUID (if available)
        - MAC address
        - Hostname

        This ID remains stable across reboots and Context DNA restarts.
        """
        import platform
        import uuid as uuid_lib

        # Try to get a hardware-based UUID
        try:
            # On macOS, use system UUID
            if platform.system() == "Darwin":
                result = subprocess.run(
                    ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        if "IOPlatformUUID" in line:
                            uuid_str = line.split('"')[-2]
                            return f"mac-{uuid_str[:16]}"
        except Exception as e:
            print(f"[WARN] IOPlatformUUID detection failed: {e}")

        # Fallback: use MAC address + hostname hash
        try:
            mac = uuid_lib.getnode()
            hostname = platform.node()
            combined = f"{mac}-{hostname}"
            return f"gen-{hashlib.sha256(combined.encode()).hexdigest()[:16]}"
        except Exception:
            # Last resort: random UUID (will change on reinstall)
            return f"rnd-{uuid_lib.uuid4().hex[:16]}"

    def _get_current_platform_info(self) -> Dict[str, str]:
        """Get current platform information."""
        import platform as plat
        return {
            "os_platform": plat.system().lower(),  # darwin, linux, windows
            "os_version": plat.release(),
            "arch": plat.machine(),  # arm64, x86_64
            "hostname": plat.node(),
        }

    def register_computer(
        self,
        friendly_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Register this computer in the computer registry.

        Called automatically on first use. Returns computer info including
        the assigned computer_number.

        Args:
            friendly_name: Optional user-friendly name (e.g., "Work MacBook Pro")

        Returns:
            Dict with computer_id, computer_number, and other info
        """
        computer_id = self._get_computer_id()
        platform_info = self._get_current_platform_info()
        now = self._utc_now()

        # Check if already registered
        cursor = self._conn.execute("""
            SELECT * FROM computer_registry WHERE computer_id = ?
        """, (computer_id,))
        existing = cursor.fetchone()

        if existing:
            # Update last_seen and session count
            self._conn.execute("""
                UPDATE computer_registry
                SET last_seen_utc = ?,
                    total_sessions = total_sessions + 1,
                    hostname = ?,
                    os_version = ?
                WHERE computer_id = ?
            """, (now, platform_info["hostname"], platform_info["os_version"], computer_id))
            self._conn.commit()

            return {
                "computer_id": existing["computer_id"],
                "computer_number": existing["computer_number"],
                "hostname": existing["hostname"],
                "friendly_name": existing["friendly_name"],
                "os_platform": existing["os_platform"],
                "is_new": False,
            }

        # Get next computer number
        cursor = self._conn.execute("""
            SELECT MAX(computer_number) as max_num FROM computer_registry
        """)
        row = cursor.fetchone()
        next_number = (row["max_num"] or 0) + 1

        # Auto-generate friendly name if not provided
        if not friendly_name:
            friendly_name = f"{platform_info['hostname']} ({platform_info['os_platform'].title()} #{next_number})"

        # Insert new computer
        self._conn.execute("""
            INSERT INTO computer_registry
                (computer_id, computer_number, hostname, os_platform, os_version,
                 arch, friendly_name, first_seen_utc, last_seen_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (computer_id, next_number, platform_info["hostname"],
              platform_info["os_platform"], platform_info["os_version"],
              platform_info["arch"], friendly_name, now, now))
        self._conn.commit()

        logger.info(f"Registered new computer #{next_number}: {friendly_name} ({computer_id})")

        return {
            "computer_id": computer_id,
            "computer_number": next_number,
            "hostname": platform_info["hostname"],
            "friendly_name": friendly_name,
            "os_platform": platform_info["os_platform"],
            "is_new": True,
        }

    def get_current_computer(self) -> Dict[str, Any]:
        """Get info about the current computer, registering if needed."""
        return self.register_computer()

    def get_all_computers(self) -> List[Dict[str, Any]]:
        """Get all registered computers for dashboard."""
        cursor = self._conn.execute("""
            SELECT * FROM computer_registry
            ORDER BY computer_number
        """)

        computers = []
        current_id = self._get_computer_id()

        for row in cursor:
            computers.append({
                "computer_id": row["computer_id"],
                "computer_number": row["computer_number"],
                "hostname": row["hostname"],
                "friendly_name": row["friendly_name"],
                "os_platform": row["os_platform"],
                "os_version": row["os_version"],
                "arch": row["arch"],
                "first_seen_utc": row["first_seen_utc"],
                "last_seen_utc": row["last_seen_utc"],
                "total_sessions": row["total_sessions"],
                "is_active": bool(row["is_active"]),
                "is_current": row["computer_id"] == current_id,
                "notes": row["notes"],
            })

        return computers

    def set_computer_name(self, computer_number: int, friendly_name: str):
        """Set a friendly name for a computer."""
        now = self._utc_now()
        self._conn.execute("""
            UPDATE computer_registry
            SET friendly_name = ?, last_seen_utc = ?
            WHERE computer_number = ?
        """, (friendly_name, now, computer_number))
        self._conn.commit()

    def deactivate_computer(self, computer_number: int, reason: Optional[str] = None):
        """Deactivate a computer (e.g., when sold/retired)."""
        self._conn.execute("""
            UPDATE computer_registry
            SET is_active = 0, notes = ?
            WHERE computer_number = ?
        """, (reason or "Deactivated", computer_number))
        self._conn.commit()

    # -------------------------------------------------------------------------
    # Account & User Registry (Enterprise Support)
    # -------------------------------------------------------------------------

    def get_or_create_default_account(self) -> Dict[str, Any]:
        """
        Get or create the default account for this installation.

        For personal use, creates a personal account automatically.
        For enterprise, account should be pre-created via admin.
        """
        now = self._utc_now()

        # Check for existing account
        cursor = self._conn.execute("""
            SELECT * FROM account_registry LIMIT 1
        """)
        existing = cursor.fetchone()

        if existing:
            return {
                "account_id": existing["account_id"],
                "account_type": existing["account_type"],
                "account_name": existing["account_name"],
                "max_users": existing["max_users"],
                "subscription_tier": existing["subscription_tier"],
                "is_new": False,
            }

        # Create default personal account
        import uuid as uuid_lib
        account_id = f"acct-{uuid_lib.uuid4().hex[:12]}"

        self._conn.execute("""
            INSERT INTO account_registry
                (account_id, account_type, account_name, max_users, subscription_tier, created_utc)
            VALUES (?, 'personal', 'My Context DNA', 1, 'free', ?)
        """, (account_id, now))
        self._conn.commit()

        logger.info(f"Created default personal account: {account_id}")

        return {
            "account_id": account_id,
            "account_type": "personal",
            "account_name": "My Context DNA",
            "max_users": 1,
            "subscription_tier": "free",
            "is_new": True,
        }

    def get_or_create_default_user(self, account_id: str) -> Dict[str, Any]:
        """
        Get or create the default user for this account.

        For personal accounts, auto-creates the single user.
        For enterprise, users should be created via admin.
        """
        now = self._utc_now()

        # Check for existing user
        cursor = self._conn.execute("""
            SELECT * FROM user_registry WHERE account_id = ? LIMIT 1
        """, (account_id,))
        existing = cursor.fetchone()

        if existing:
            # Update last seen
            self._conn.execute("""
                UPDATE user_registry SET last_seen_utc = ? WHERE user_id = ?
            """, (now, existing["user_id"]))
            self._conn.commit()

            return {
                "user_id": existing["user_id"],
                "account_id": existing["account_id"],
                "user_number": existing["user_number"],
                "display_name": existing["display_name"],
                "role": existing["role"],
                "is_new": False,
            }

        # Create default user
        import uuid as uuid_lib
        import os as os_module
        user_id = f"user-{uuid_lib.uuid4().hex[:12]}"
        display_name = os_module.environ.get("USER", "Default User")

        self._conn.execute("""
            INSERT INTO user_registry
                (user_id, account_id, user_number, display_name, role, first_seen_utc, last_seen_utc)
            VALUES (?, ?, 1, ?, 'owner', ?, ?)
        """, (user_id, account_id, display_name, now, now))
        self._conn.commit()

        logger.info(f"Created default user: {display_name} ({user_id})")

        return {
            "user_id": user_id,
            "account_id": account_id,
            "user_number": 1,
            "display_name": display_name,
            "role": "owner",
            "is_new": True,
        }

    def register_computer(
        self,
        friendly_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Register this computer in the computer registry.

        Called automatically on first use. Returns computer info including
        the assigned computer_number.

        Hierarchy: Account → User → Computer

        Args:
            friendly_name: Optional user-friendly name (e.g., "Work MacBook Pro")

        Returns:
            Dict with computer_id, computer_number, and other info
        """
        computer_id = self._get_computer_id()
        platform_info = self._get_current_platform_info()
        now = self._utc_now()

        # Ensure account and user exist
        account = self.get_or_create_default_account()
        user = self.get_or_create_default_user(account["account_id"])

        # Check if computer already registered
        cursor = self._conn.execute("""
            SELECT * FROM computer_registry WHERE computer_id = ?
        """, (computer_id,))
        existing = cursor.fetchone()

        if existing:
            # Update last_seen and session count
            self._conn.execute("""
                UPDATE computer_registry
                SET last_seen_utc = ?,
                    total_sessions = total_sessions + 1,
                    hostname = ?,
                    os_version = ?
                WHERE computer_id = ?
            """, (now, platform_info["hostname"], platform_info["os_version"], computer_id))
            self._conn.commit()

            return {
                "computer_id": existing["computer_id"],
                "computer_number": existing["computer_number"],
                "hostname": existing["hostname"],
                "friendly_name": existing["friendly_name"],
                "os_platform": existing["os_platform"],
                "user_id": existing["user_id"],
                "account_id": existing["account_id"],
                "account_name": account.get("account_name"),
                "is_new": False,
            }

        # Get next computer number for this account
        cursor = self._conn.execute("""
            SELECT MAX(computer_number) as max_num FROM computer_registry
            WHERE account_id = ?
        """, (account["account_id"],))
        row = cursor.fetchone()
        next_number = (row["max_num"] or 0) + 1

        # Auto-generate friendly name if not provided
        if not friendly_name:
            friendly_name = f"{platform_info['hostname']} ({platform_info['os_platform'].title()} #{next_number})"

        # Insert new computer
        self._conn.execute("""
            INSERT INTO computer_registry
                (computer_id, user_id, account_id, computer_number, hostname,
                 os_platform, os_version, arch, friendly_name, first_seen_utc, last_seen_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (computer_id, user["user_id"], account["account_id"], next_number,
              platform_info["hostname"], platform_info["os_platform"],
              platform_info["os_version"], platform_info["arch"],
              friendly_name, now, now))
        self._conn.commit()

        logger.info(f"Registered computer #{next_number}: {friendly_name} (account: {account['account_name']})")

        return {
            "computer_id": computer_id,
            "computer_number": next_number,
            "hostname": platform_info["hostname"],
            "friendly_name": friendly_name,
            "os_platform": platform_info["os_platform"],
            "user_id": user["user_id"],
            "account_id": account["account_id"],
            "account_name": account.get("account_name"),
            "is_new": True,
        }

    def get_account_info(self) -> Dict[str, Any]:
        """Get full account hierarchy info for dashboard."""
        account = self.get_or_create_default_account()

        # Get all users for this account
        cursor = self._conn.execute("""
            SELECT * FROM user_registry WHERE account_id = ? ORDER BY user_number
        """, (account["account_id"],))
        users = [dict(row) for row in cursor]

        # Get all computers for this account
        computers = self.get_all_computers()

        return {
            "account": account,
            "users": users,
            "computers": computers,
            "total_users": len(users),
            "total_computers": len(computers),
        }

    def upgrade_account(
        self,
        account_type: str,
        account_name: str,
        max_users: int,
        subscription_tier: str
    ):
        """
        Upgrade account to team/enterprise tier.

        This would be called after payment/license activation.
        """
        account = self.get_or_create_default_account()
        now = self._utc_now()

        self._conn.execute("""
            UPDATE account_registry
            SET account_type = ?,
                account_name = ?,
                max_users = ?,
                subscription_tier = ?,
                last_activity_utc = ?
            WHERE account_id = ?
        """, (account_type, account_name, max_users, subscription_tier,
              now, account["account_id"]))
        self._conn.commit()

        logger.info(f"Account upgraded to {subscription_tier}: {account_name} (max {max_users} users)")

    def add_user_to_account(
        self,
        email: str,
        display_name: str,
        role: str = "user"
    ) -> Dict[str, Any]:
        """
        Add a new user to the account (for team/enterprise).

        Returns the new user info or error if max users reached.
        """
        account = self.get_or_create_default_account()
        now = self._utc_now()

        # Check user limit
        cursor = self._conn.execute("""
            SELECT COUNT(*) as user_count FROM user_registry WHERE account_id = ?
        """, (account["account_id"],))
        current_count = cursor.fetchone()["user_count"]

        if current_count >= account["max_users"]:
            return {
                "error": f"User limit reached ({account['max_users']} users max)",
                "suggestion": "Upgrade to enterprise for more users",
            }

        # Get next user number
        cursor = self._conn.execute("""
            SELECT MAX(user_number) as max_num FROM user_registry WHERE account_id = ?
        """, (account["account_id"],))
        next_number = (cursor.fetchone()["max_num"] or 0) + 1

        # Create user
        import uuid as uuid_lib
        user_id = f"user-{uuid_lib.uuid4().hex[:12]}"

        self._conn.execute("""
            INSERT INTO user_registry
                (user_id, account_id, user_number, email, display_name, role,
                 first_seen_utc, last_seen_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, account["account_id"], next_number, email,
              display_name, role, now, now))
        self._conn.commit()

        logger.info(f"Added user #{next_number}: {display_name} ({email})")

        return {
            "user_id": user_id,
            "user_number": next_number,
            "email": email,
            "display_name": display_name,
            "role": role,
            "account_id": account["account_id"],
        }

    # -------------------------------------------------------------------------
    # Notification Preferences (Mute Settings)
    # -------------------------------------------------------------------------

    def set_mute(
        self,
        destination: str,
        section_id: Optional[int] = None,
        phase: Optional[str] = None,
        muted: bool = True,
        duration_hours: Optional[int] = None,
        reason: Optional[str] = None
    ):
        """
        Set mute preference for a destination/section/phase.

        Args:
            destination: Webhook destination (vs_code_claude_code, etc.)
            section_id: Specific section (0-8) or None for all sections
            phase: pre_message, post_message, or None for all phases
            muted: Whether to mute
            duration_hours: If set, auto-unmute after this many hours
            reason: Why muted (for dashboard display)
        """
        now = self._utc_now()
        muted_until = None
        if duration_hours and muted:
            muted_until = (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat()

        self._conn.execute("""
            INSERT INTO notification_preference
                (destination, section_id, phase, muted, muted_until_utc, muted_reason, created_utc, updated_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(destination, section_id, phase) DO UPDATE SET
                muted = excluded.muted,
                muted_until_utc = excluded.muted_until_utc,
                muted_reason = excluded.muted_reason,
                updated_utc = excluded.updated_utc
        """, (destination, section_id, phase, 1 if muted else 0, muted_until, reason, now, now))
        self._conn.commit()

        logger.info(f"Mute preference set: {destination}/{section_id}/{phase} = {muted}")

    def is_muted(
        self,
        destination: str,
        section_id: int,
        phase: str
    ) -> bool:
        """
        Check if notifications are muted for this destination/section/phase.

        Checks in order:
        1. Exact match (destination + section + phase)
        2. Destination + section (any phase)
        3. Destination only (all sections)
        """
        now = self._utc_now()

        # Check for exact match or broader mute
        cursor = self._conn.execute("""
            SELECT muted, muted_until_utc FROM notification_preference
            WHERE destination = ?
              AND (section_id IS NULL OR section_id = ?)
              AND (phase IS NULL OR phase = ?)
            ORDER BY
                CASE WHEN section_id IS NOT NULL AND phase IS NOT NULL THEN 0
                     WHEN section_id IS NOT NULL THEN 1
                     ELSE 2 END
            LIMIT 1
        """, (destination, section_id, phase))

        row = cursor.fetchone()
        if not row:
            return False

        if not row["muted"]:
            return False

        # Check if mute has expired
        if row["muted_until_utc"]:
            if row["muted_until_utc"] < now:
                # Auto-unmute expired entries
                self._conn.execute("""
                    UPDATE notification_preference
                    SET muted = 0, updated_utc = ?
                    WHERE destination = ? AND muted_until_utc < ?
                """, (now, destination, now))
                self._conn.commit()
                return False

        return True

    def get_all_preferences(self) -> List[Dict[str, Any]]:
        """Get all notification preferences for dashboard."""
        cursor = self._conn.execute("""
            SELECT
                destination, section_id, phase, muted,
                muted_until_utc, muted_reason, updated_utc
            FROM notification_preference
            ORDER BY destination, section_id, phase
        """)

        prefs = []
        for row in cursor:
            prefs.append({
                "destination": row["destination"],
                "destination_display": DESTINATIONS.get(row["destination"], {}).get("display", row["destination"]),
                "section_id": row["section_id"],
                "section_name": SECTIONS.get(row["section_id"], {}).get("name") if row["section_id"] is not None else "All Sections",
                "phase": row["phase"] or "All Phases",
                "muted": bool(row["muted"]),
                "muted_until_utc": row["muted_until_utc"],
                "muted_reason": row["muted_reason"],
                "updated_utc": row["updated_utc"]
            })

        return prefs

    def unmute_all(self, destination: Optional[str] = None):
        """Unmute all notifications, optionally for a specific destination."""
        now = self._utc_now()
        if destination:
            self._conn.execute("""
                UPDATE notification_preference
                SET muted = 0, updated_utc = ?
                WHERE destination = ?
            """, (now, destination))
        else:
            self._conn.execute("""
                UPDATE notification_preference
                SET muted = 0, updated_utc = ?
            """, (now,))
        self._conn.commit()

    # -------------------------------------------------------------------------
    # Payload Blob Store (Immutable, SHA256-keyed)
    # -------------------------------------------------------------------------

    def _compute_sha256(self, content: str) -> str:
        """Compute SHA256 hash of content."""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def store_payload_blob(
        self,
        payload: str,
        destination: str,
        content_type: str = "text/markdown",
        notes: Optional[str] = None
    ) -> str:
        """
        Store a payload blob (immutable, append-only).

        Args:
            payload: The full injected payload text
            destination: Which destination received this payload
            content_type: Content type (text/markdown, application/json)
            notes: Optional notes

        Returns:
            SHA256 hash of the payload (the key)
        """
        payload_sha256 = self._compute_sha256(payload)
        now = self._utc_now()

        # Idempotent insert (ON CONFLICT DO NOTHING per spec)
        self._conn.execute("""
            INSERT OR IGNORE INTO payload_blob
                (payload_sha256, created_at_utc, content_type, byte_size_raw,
                 payload_text, destination, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (payload_sha256, now, content_type, len(payload.encode('utf-8')),
              payload, destination, notes))
        self._conn.commit()

        return payload_sha256

    def store_section_blob(
        self,
        content: str,
        section_id: int,
        destination: str,
        content_type: str = "text/markdown",
        notes: Optional[str] = None
    ) -> str:
        """
        Store a section blob (immutable, append-only).

        Args:
            content: The section's content output
            section_id: Which section (0-8)
            destination: Which destination this was for
            content_type: Content type
            notes: Optional notes

        Returns:
            SHA256 hash of the content (the key)
        """
        content_sha256 = self._compute_sha256(content)
        now = self._utc_now()

        # Idempotent insert
        self._conn.execute("""
            INSERT OR IGNORE INTO section_blob
                (content_sha256, created_at_utc, section_id, content_type,
                 byte_size_raw, content_text, destination, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (content_sha256, now, section_id, content_type,
              len(content.encode('utf-8')), content, destination, notes))
        self._conn.commit()

        return content_sha256

    def get_payload_blob(self, payload_sha256: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve a payload blob by SHA256.

        Returns:
            Dict with payload data or None if not found
        """
        cursor = self._conn.execute("""
            SELECT * FROM payload_blob WHERE payload_sha256 = ?
        """, (payload_sha256,))

        row = cursor.fetchone()
        if not row:
            return None

        return {
            "payload_sha256": row["payload_sha256"],
            "created_at_utc": row["created_at_utc"],
            "content_type": row["content_type"],
            "byte_size_raw": row["byte_size_raw"],
            "payload_text": row["payload_text"],
            "destination": row["destination"],
            "notes": row["notes"]
        }

    def get_section_blob(self, content_sha256: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve a section blob by SHA256.

        Returns:
            Dict with section data or None if not found
        """
        cursor = self._conn.execute("""
            SELECT * FROM section_blob WHERE content_sha256 = ?
        """, (content_sha256,))

        row = cursor.fetchone()
        if not row:
            return None

        section_info = SECTIONS.get(row["section_id"], {})
        return {
            "content_sha256": row["content_sha256"],
            "created_at_utc": row["created_at_utc"],
            "section_id": row["section_id"],
            "section_name": section_info.get("name", f"Section {row['section_id']}"),
            "content_type": row["content_type"],
            "byte_size_raw": row["byte_size_raw"],
            "content_text": row["content_text"],
            "destination": row["destination"],
            "notes": row["notes"]
        }

    def get_recent_payloads(
        self,
        destination: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Get recent payloads for dashboard/debugging."""
        if destination:
            cursor = self._conn.execute("""
                SELECT payload_sha256, created_at_utc, byte_size_raw, destination, notes
                FROM payload_blob
                WHERE destination = ?
                ORDER BY created_at_utc DESC
                LIMIT ?
            """, (destination, limit))
        else:
            cursor = self._conn.execute("""
                SELECT payload_sha256, created_at_utc, byte_size_raw, destination, notes
                FROM payload_blob
                ORDER BY created_at_utc DESC
                LIMIT ?
            """, (limit,))

        return [{
            "payload_sha256": row["payload_sha256"][:16] + "...",  # Truncate for display
            "payload_sha256_full": row["payload_sha256"],
            "created_at_utc": row["created_at_utc"],
            "byte_size_raw": row["byte_size_raw"],
            "destination": row["destination"],
            "notes": row["notes"]
        } for row in cursor]

    # -------------------------------------------------------------------------
    # Section Health State Tracking
    # -------------------------------------------------------------------------

    def get_section_state(
        self,
        destination: str,
        section_id: int,
        phase: str
    ) -> Optional[SectionHealthStatus]:
        """Get current health state for a destination/section/phase."""
        cursor = self._conn.execute("""
            SELECT * FROM section_health_state
            WHERE destination = ? AND section_id = ? AND phase = ?
        """, (destination, section_id, phase))

        row = cursor.fetchone()
        if not row:
            return None

        # Handle potentially missing columns gracefully
        try:
            last_payload_sha256 = row["last_payload_sha256"]
        except (IndexError, KeyError):
            last_payload_sha256 = None

        try:
            last_section_sha256 = row["last_section_sha256"]
        except (IndexError, KeyError):
            last_section_sha256 = None

        return SectionHealthStatus(
            destination=row["destination"],
            section_id=row["section_id"],
            phase=row["phase"],
            status=row["status"],
            consecutive_failures=row["consecutive_failures"],
            last_healthy_utc=row["last_healthy_utc"],
            last_failure_utc=row["last_failure_utc"],
            last_notification_utc=row["last_notification_utc"],
            failure_reason=row["failure_reason"],
            last_payload_sha256=last_payload_sha256,
            last_section_sha256=last_section_sha256
        )

    def update_section_state(
        self,
        destination: str,
        section_id: int,
        phase: str,
        healthy: bool,
        failure_reason: Optional[str] = None,
        payload_sha256: Optional[str] = None,
        section_sha256: Optional[str] = None
    ) -> SectionHealthStatus:
        """
        Update section health state and return the new state.

        Tracks consecutive failures for notification throttling.
        Stores blob references for auditability ("what failed").
        """
        now = self._utc_now()

        # Ensure destination is registered
        self.register_destination(destination)

        # Get current state
        current = self.get_section_state(destination, section_id, phase)

        if healthy:
            # Reset on healthy
            new_status = "healthy"
            consecutive_failures = 0
            last_healthy = now
            last_failure = current.last_failure_utc if current else None
            reason = None
            last_payload = None
            last_section = None
        else:
            # Increment failures
            consecutive_failures = (current.consecutive_failures + 1) if current else 1
            last_healthy = current.last_healthy_utc if current else None
            last_failure = now
            reason = failure_reason
            # Store blob references for "what failed"
            last_payload = payload_sha256
            last_section = section_sha256

            if consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD:
                new_status = "failing"
            else:
                new_status = "degraded"

        # Upsert state
        self._conn.execute("""
            INSERT INTO section_health_state
                (destination, section_id, phase, status, consecutive_failures,
                 last_healthy_utc, last_failure_utc, failure_reason,
                 last_payload_sha256, last_section_sha256, updated_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(destination, section_id, phase) DO UPDATE SET
                status = excluded.status,
                consecutive_failures = excluded.consecutive_failures,
                last_healthy_utc = COALESCE(excluded.last_healthy_utc, section_health_state.last_healthy_utc),
                last_failure_utc = COALESCE(excluded.last_failure_utc, section_health_state.last_failure_utc),
                failure_reason = excluded.failure_reason,
                last_payload_sha256 = COALESCE(excluded.last_payload_sha256, section_health_state.last_payload_sha256),
                last_section_sha256 = COALESCE(excluded.last_section_sha256, section_health_state.last_section_sha256),
                updated_utc = excluded.updated_utc
        """, (destination, section_id, phase, new_status, consecutive_failures,
              last_healthy if healthy else None, last_failure if not healthy else None,
              reason, last_payload, last_section, now))
        self._conn.commit()

        return SectionHealthStatus(
            destination=destination,
            section_id=section_id,
            phase=phase,
            status=new_status,
            consecutive_failures=consecutive_failures,
            last_healthy_utc=last_healthy,
            last_failure_utc=last_failure,
            last_notification_utc=current.last_notification_utc if current else None,
            failure_reason=reason,
            last_payload_sha256=last_payload,
            last_section_sha256=last_section
        )

    def mark_notification_sent(
        self,
        destination: str,
        section_id: int,
        phase: str
    ):
        """Mark that a notification was sent for this combination."""
        now = self._utc_now()
        self._conn.execute("""
            UPDATE section_health_state
            SET last_notification_utc = ?
            WHERE destination = ? AND section_id = ? AND phase = ?
        """, (now, destination, section_id, phase))
        self._conn.commit()

    def should_notify(
        self,
        destination: str,
        section_id: int,
        phase: str,
        state: SectionHealthStatus
    ) -> bool:
        """
        Determine if we should send a notification.

        Returns True if:
        - State is 'failing' (consecutive failures >= threshold)
        - Not muted
        - Cooldown period has passed since last notification
        """
        if state.status != "failing":
            return False

        if self.is_muted(destination, section_id, phase):
            return False

        # Check cooldown
        if state.last_notification_utc:
            last_notif = datetime.fromisoformat(state.last_notification_utc.replace("Z", "+00:00"))
            cooldown_end = last_notif + timedelta(minutes=NOTIFICATION_COOLDOWN_MINUTES)
            if datetime.now(timezone.utc) < cooldown_end:
                return False

        return True

    def record_notification(
        self,
        destination: str,
        section_id: int,
        phase: str,
        severity: str,
        title: str,
        message: str
    ):
        """Record a sent notification for history/analytics."""
        now = self._utc_now()
        self._conn.execute("""
            INSERT INTO notification_history
                (destination, section_id, phase, severity, title, message, sent_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (destination, section_id, phase, severity, title, message, now))
        self._conn.commit()

    def get_notification_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent notification history for dashboard."""
        cursor = self._conn.execute("""
            SELECT * FROM notification_history
            ORDER BY sent_utc DESC
            LIMIT ?
        """, (limit,))

        history = []
        for row in cursor:
            history.append({
                "destination": row["destination"],
                "destination_display": DESTINATIONS.get(row["destination"], {}).get("display", row["destination"]),
                "section_id": row["section_id"],
                "section_name": SECTIONS.get(row["section_id"], {}).get("name", f"Section {row['section_id']}"),
                "phase": row["phase"],
                "severity": row["severity"],
                "title": row["title"],
                "message": row["message"],
                "sent_utc": row["sent_utc"]
            })

        return history

    def get_all_section_states(self) -> List[Dict[str, Any]]:
        """Get all section health states for dashboard."""
        cursor = self._conn.execute("""
            SELECT * FROM section_health_state
            ORDER BY destination, section_id, phase
        """)

        states = []
        for row in cursor:
            section_info = SECTIONS.get(row["section_id"], {})
            dest_info = DESTINATIONS.get(row["destination"], {})

            states.append({
                "destination": row["destination"],
                "destination_display": dest_info.get("display", row["destination"]),
                "section_id": row["section_id"],
                "section_name": section_info.get("name", f"Section {row['section_id']}"),
                "section_critical": section_info.get("critical", False),
                "phase": row["phase"],
                "status": row["status"],
                "consecutive_failures": row["consecutive_failures"],
                "last_healthy_utc": row["last_healthy_utc"],
                "last_failure_utc": row["last_failure_utc"],
                "last_notification_utc": row["last_notification_utc"],
                "failure_reason": row["failure_reason"],
                "is_muted": self.is_muted(row["destination"], row["section_id"], row["phase"])
            })

        return states


# =============================================================================
# WEBHOOK SECTION NOTIFIER
# =============================================================================

class WebhookSectionNotifier:
    """
    Main class for sending webhook section health notifications.

    Integrates with:
    - NotificationPreferencesStore for mute settings
    - macOS notification system
    - Ecosystem health daemon
    """

    def __init__(self):
        self._store = NotificationPreferencesStore()

    def send_macos_notification(
        self,
        title: str,
        message: str,
        subtitle: Optional[str] = None,
        sound: str = "Purr"
    ) -> bool:
        """Send a native macOS notification."""
        try:
            script_parts = [f'display notification "{message}"']
            script_parts.append(f'with title "{title}"')
            if subtitle:
                script_parts.append(f'subtitle "{subtitle}"')
            script_parts.append(f'sound name "{sound}"')

            script = " ".join(script_parts)
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
            return True
        except Exception as e:
            logger.warning(f"Failed to send macOS notification: {e}")
            return False

    def record_and_notify(
        self,
        destination: str,
        section_id: int,
        phase: str,
        healthy: bool,
        failure_reason: Optional[str] = None
    ) -> Optional[str]:
        """
        Record section health and send notification if needed.

        Returns:
            Notification message if sent, None otherwise
        """
        # Update state
        state = self._store.update_section_state(
            destination=destination,
            section_id=section_id,
            phase=phase,
            healthy=healthy,
            failure_reason=failure_reason
        )

        # Check if we should notify
        if not self._store.should_notify(destination, section_id, phase, state):
            return None

        # Build notification
        section_info = SECTIONS.get(section_id, {"name": f"Section {section_id}", "critical": False})
        dest_info = DESTINATIONS.get(destination, {"display": destination})

        severity = "critical" if section_info.get("critical") else "warning"
        icon = "🚨" if severity == "critical" else "⚠️"

        title = f"{icon} {dest_info['display']} Section {section_id}"
        subtitle = section_info["name"]

        # Calculate time since healthy
        time_msg = ""
        if state.last_healthy_utc:
            last_healthy = datetime.fromisoformat(state.last_healthy_utc.replace("Z", "+00:00"))
            mins_down = (datetime.now(timezone.utc) - last_healthy).total_seconds() / 60
            time_msg = f" ({mins_down:.0f}min)"

        message = f"{phase} webhook: {state.failure_reason or 'Section failing'}{time_msg}"

        # Send notification
        sound = "Sosumi" if severity == "critical" else "Purr"
        sent = self.send_macos_notification(
            title=title,
            message=message,
            subtitle=subtitle,
            sound=sound
        )

        if sent:
            # Mark sent and record history
            self._store.mark_notification_sent(destination, section_id, phase)
            self._store.record_notification(
                destination=destination,
                section_id=section_id,
                phase=phase,
                severity=severity,
                title=f"{title}: {subtitle}",
                message=message
            )

            logger.info(f"Notification sent: {title} - {message}")
            return message

        return None

    def check_injection_health_and_notify(self) -> Dict[str, Any]:
        """
        Check injection health monitor and send notifications for failures.

        This is the main method to call from ecosystem_health daemon.

        Returns:
            Summary of health check and notifications sent
        """
        try:
            from memory.injection_health_monitor import get_webhook_monitor

            monitor = get_webhook_monitor()
            health = monitor.check_health()

            notifications_sent = []
            section_summaries = []

            # Check each destination
            for dest_id, dest_health in health.destinations.items():
                # Check section health within destination
                # Note: injection_health_monitor tracks overall destination health
                # We need to check per-section from section data
                pass

            # Check each section's health
            for section_id, section_health in health.sections.items():
                section_info = SECTIONS.get(section_id, {})

                # Skip "missing" sections - no data doesn't mean failing.
                # "missing" means injection_section table has no records,
                # which is a data gap (now fixed), not a health failure.
                # Only record actual health states: healthy, degraded, down.
                if section_health.status == "missing":
                    section_summaries.append({
                        "section_id": section_id,
                        "name": section_info.get("name", f"Section {section_id}"),
                        "healthy": None,  # Unknown, not failing
                        "status": "missing"
                    })
                    continue

                # Determine if section is healthy
                # "degraded" means section is working but slow - not a failure.
                # Only "down" status should be recorded as unhealthy.
                is_healthy = section_health.status in ("healthy", "degraded")
                failure_reason = None

                if not is_healthy:
                    failure_reason = f"Status: {section_health.status}"
                    if hasattr(section_health, 'notes') and section_health.notes:
                        failure_reason += f" - {section_health.notes}"

                # Only record for vs_code_claude_code (the actual injection target)
                # Don't blindly mark all primary destinations as failing
                result = self.record_and_notify(
                    destination="vs_code_claude_code",
                    section_id=section_id,
                    phase="pre_message",
                    healthy=is_healthy,
                    failure_reason=failure_reason
                )

                if result:
                    notifications_sent.append({
                        "destination": "vs_code_claude_code",
                        "section_id": section_id,
                        "message": result
                    })

                section_summaries.append({
                    "section_id": section_id,
                    "name": section_info.get("name", f"Section {section_id}"),
                    "healthy": is_healthy,
                    "status": section_health.status
                })

            return {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "overall_status": health.status,
                "eighth_intelligence_status": health.eighth_intelligence_status,
                "notifications_sent": notifications_sent,
                "section_summaries": section_summaries,
                "alerts": health.alerts
            }

        except ImportError as e:
            logger.warning(f"InjectionHealthMonitor not available: {e}")
            return {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "overall_status": "unknown",
                "error": str(e)
            }
        except Exception as e:
            logger.error(f"Error checking injection health: {e}")
            return {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "overall_status": "error",
                "error": str(e)
            }

    def get_dashboard_data(self) -> Dict[str, Any]:
        """
        Get all data needed for dashboard display.

        Returns:
            Dict containing:
            - section_states: Current health of all sections by destination
            - notification_preferences: User mute settings
            - notification_history: Recent notifications sent
            - sections: Section definitions
            - destinations: Dynamic destination registry
            - platforms: OS platform breakdown
            - computers: All registered computers
            - current_computer: This computer's info
            - recent_payloads: Recent payload blobs for debugging
        """
        current_computer = self._store.get_current_computer()
        account_info = self._store.get_account_info()

        return {
            "section_states": self._store.get_all_section_states(),
            "notification_preferences": self._store.get_all_preferences(),
            "notification_history": self._store.get_notification_history(limit=50),
            "sections": SECTIONS,
            "destinations": self._store.get_all_destinations(),
            "platforms": self._store.get_platform_summary(),
            "platform_details": self._store.get_platform_stats(),
            "account": account_info["account"],
            "users": account_info["users"],
            "computers": account_info["computers"],
            "current_computer": current_computer,
            "recent_payloads": self._store.get_recent_payloads(limit=10)
        }

    def mute(
        self,
        destination: str,
        section_id: Optional[int] = None,
        phase: Optional[str] = None,
        duration_hours: Optional[int] = None,
        reason: Optional[str] = None
    ):
        """Mute notifications for a destination/section/phase."""
        self._store.set_mute(
            destination=destination,
            section_id=section_id,
            phase=phase,
            muted=True,
            duration_hours=duration_hours,
            reason=reason
        )

    def unmute(
        self,
        destination: str,
        section_id: Optional[int] = None,
        phase: Optional[str] = None
    ):
        """Unmute notifications for a destination/section/phase."""
        self._store.set_mute(
            destination=destination,
            section_id=section_id,
            phase=phase,
            muted=False
        )

    def unmute_all(self, destination: Optional[str] = None):
        """Unmute all notifications."""
        self._store.unmute_all(destination)


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_notifier: Optional[WebhookSectionNotifier] = None


def get_notifier() -> WebhookSectionNotifier:
    """Get singleton notifier instance."""
    global _notifier
    if _notifier is None:
        _notifier = WebhookSectionNotifier()
    return _notifier


def record_section_health(
    destination: str,
    section_id: int,
    healthy: bool,
    phase: str = "pre_message",
    failure_reason: Optional[str] = None
) -> Optional[str]:
    """
    Record section health and send notification if needed.

    Args:
        destination: Webhook destination (vs_code_claude_code, synaptic_chat, etc.)
        section_id: Section ID (0-8)
        healthy: Whether the section is healthy
        phase: pre_message or post_message
        failure_reason: Reason for failure (if not healthy)

    Returns:
        Notification message if sent, None otherwise
    """
    return get_notifier().record_and_notify(
        destination=destination,
        section_id=section_id,
        phase=phase,
        healthy=healthy,
        failure_reason=failure_reason
    )


def mute_notification(
    destination: str,
    section_id: Optional[int] = None,
    phase: Optional[str] = None,
    duration_hours: Optional[int] = None,
    reason: Optional[str] = None
):
    """
    Mute notifications for a destination/section/phase.

    Args:
        destination: Webhook destination
        section_id: Specific section or None for all
        phase: Specific phase or None for all
        duration_hours: Auto-unmute after this many hours
        reason: Reason for muting (displayed in dashboard)
    """
    get_notifier().mute(
        destination=destination,
        section_id=section_id,
        phase=phase,
        duration_hours=duration_hours,
        reason=reason
    )


def unmute_notification(
    destination: str,
    section_id: Optional[int] = None,
    phase: Optional[str] = None
):
    """Unmute notifications for a destination/section/phase."""
    get_notifier().unmute(
        destination=destination,
        section_id=section_id,
        phase=phase
    )


def get_notification_preferences() -> List[Dict[str, Any]]:
    """Get all notification preferences for dashboard."""
    return get_notifier()._store.get_all_preferences()


def get_dashboard_data() -> Dict[str, Any]:
    """Get all data for dashboard display."""
    return get_notifier().get_dashboard_data()


def record_platform_injection(
    destination: str,
    os_platform: str,
    success: bool,
    os_version: Optional[str] = None,
    arch: Optional[str] = None,
    latency_ms: Optional[int] = None
):
    """
    Record an injection for a specific OS platform.

    Call this from webhook handlers to track platform distribution.
    """
    get_notifier()._store.record_platform_injection(
        destination_id=destination,
        os_platform=os_platform,
        success=success,
        os_version=os_version,
        arch=arch,
        latency_ms=latency_ms
    )


def store_injection_payload(
    payload: str,
    destination: str,
    content_type: str = "text/markdown"
) -> str:
    """
    Store a payload blob and return its SHA256.

    Call this from webhook handlers to enable replay/audit.
    """
    return get_notifier()._store.store_payload_blob(
        payload=payload,
        destination=destination,
        content_type=content_type
    )


def store_section_content(
    content: str,
    section_id: int,
    destination: str
) -> str:
    """
    Store a section blob and return its SHA256.

    Call this from webhook handlers for per-section diffing.
    """
    return get_notifier()._store.store_section_blob(
        content=content,
        section_id=section_id,
        destination=destination
    )


def get_platform_summary() -> Dict[str, Any]:
    """Get platform summary for dashboard."""
    return get_notifier()._store.get_platform_summary()


def get_current_computer() -> Dict[str, Any]:
    """Get current computer info (registers if new)."""
    return get_notifier()._store.get_current_computer()


def get_all_computers() -> List[Dict[str, Any]]:
    """Get all registered computers for dashboard."""
    return get_notifier()._store.get_all_computers()


def set_computer_name(computer_number: int, friendly_name: str):
    """Set a friendly name for a computer."""
    get_notifier()._store.set_computer_name(computer_number, friendly_name)


def run_health_check_and_notify() -> Dict[str, Any]:
    """
    Run health check and send notifications.

    This is the function to call from ecosystem_health daemon.
    """
    return get_notifier().check_injection_health_and_notify()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    notifier = WebhookSectionNotifier()

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()

        if cmd == "check":
            # Run health check and notify
            result = notifier.check_injection_health_and_notify()
            print(json.dumps(result, indent=2))

        elif cmd == "status":
            # Show dashboard data
            data = notifier.get_dashboard_data()

            print("=" * 70)
            print("WEBHOOK SECTION NOTIFICATIONS STATUS")
            print("=" * 70)

            # Account & User Info
            account = data.get("account", {})
            print(f"\n🏢 ACCOUNT: {account.get('account_name', 'Unknown')}")
            print(f"   Type: {account.get('account_type', 'personal')} | Tier: {account.get('subscription_tier', 'free')}")
            print(f"   Max Users: {account.get('max_users', 1)}")

            print("\n👥 USERS")
            for user in data.get("users", []):
                role_icon = "👑" if user.get("role") == "owner" else ("🔧" if user.get("role") == "admin" else "👤")
                print(f"  {role_icon} User #{user.get('user_number', 1)}: {user.get('display_name', 'Unknown')} [{user.get('role', 'user')}]")

            print("\n💻 COMPUTERS")
            for comp in data.get("computers", []):
                current = "⬅️ (this)" if comp.get("is_current") else ""
                active = "🟢" if comp.get("is_active") else "⚪"
                print(f"  {active} #{comp.get('computer_number', '?')}: {comp.get('friendly_name', 'Unknown')} {current}")
                print(f"      {comp.get('os_platform', '?')} | {comp.get('hostname', '?')} | Sessions: {comp.get('total_sessions', 1)}")

            print("\n🎯 REGISTERED DESTINATIONS")
            for dest in data["destinations"]:
                icon = "🟢" if dest["enabled"] else "⚪"
                auto = "(auto)" if dest["auto_registered"] else ""
                primary = "⭐" if dest["primary"] else ""
                success_rate = ""
                if dest["total_injections"] > 0:
                    rate = (dest["successful_injections"] / dest["total_injections"]) * 100
                    success_rate = f" [{rate:.0f}% success]"
                print(f"  {icon} {primary} {dest['display_name']} [{dest['category']}] {auto}{success_rate}")

            print("\n💻 OS PLATFORMS")
            if data["platforms"]:
                for platform, stats in data["platforms"].items():
                    print(f"  {platform}: {stats['destinations']} dest, {stats['total_injections']} inj, {stats['success_rate']}% success")
                    if stats["avg_latency_ms"]:
                        print(f"      Avg latency: {stats['avg_latency_ms']}ms")
            else:
                print("  No platform data yet")

            print("\n📊 SECTION HEALTH STATES")
            if data["section_states"]:
                for state in data["section_states"]:
                    icon = {"healthy": "✅", "degraded": "⚠️", "failing": "🚨", "unknown": "❓"}.get(state["status"], "❓")
                    muted = "🔇" if state["is_muted"] else ""
                    crit = "[CRITICAL]" if state["section_critical"] else ""
                    print(f"  {icon} {state['destination_display']} / Section {state['section_id']} ({state['section_name']}) {crit} {muted}")
                    print(f"      Status: {state['status']} | Failures: {state['consecutive_failures']}")
            else:
                print("  No section health data yet (run 'check' first)")

            print("\n🔇 MUTE PREFERENCES")
            if data["notification_preferences"]:
                for pref in data["notification_preferences"]:
                    muted_str = "MUTED" if pref["muted"] else "active"
                    until = f" until {pref['muted_until_utc'][:16]}" if pref["muted_until_utc"] else ""
                    print(f"  {pref['destination_display']} / {pref['section_name']} / {pref['phase']}: {muted_str}{until}")
            else:
                print("  No custom mute preferences set")

            print("\n📦 RECENT PAYLOADS")
            if data["recent_payloads"]:
                for payload in data["recent_payloads"][:5]:
                    print(f"  {payload['payload_sha256']} | {payload['destination']} | {payload['byte_size_raw']} bytes")
            else:
                print("  No payloads stored yet")

            print("\n📜 RECENT NOTIFICATIONS")
            if data["notification_history"]:
                for notif in data["notification_history"][:5]:
                    print(f"  [{notif['sent_utc'][:16]}] {notif['title']}")
                    print(f"      {notif['message']}")
            else:
                print("  No notifications sent yet")

        elif cmd == "mute":
            # Mute a destination/section
            if len(sys.argv) < 3:
                print("Usage: python webhook_section_notifications.py mute <destination> [section_id] [hours]")
                sys.exit(1)

            destination = sys.argv[2]
            section_id = int(sys.argv[3]) if len(sys.argv) > 3 else None
            hours = int(sys.argv[4]) if len(sys.argv) > 4 else None

            notifier.mute(destination, section_id=section_id, duration_hours=hours, reason="Manual mute via CLI")
            print(f"✅ Muted: {destination}" + (f" / Section {section_id}" if section_id else "") + (f" for {hours}h" if hours else ""))

        elif cmd == "unmute":
            # Unmute a destination/section
            if len(sys.argv) < 3:
                print("Usage: python webhook_section_notifications.py unmute <destination> [section_id]")
                sys.exit(1)

            destination = sys.argv[2]
            section_id = int(sys.argv[3]) if len(sys.argv) > 3 else None

            notifier.unmute(destination, section_id=section_id)
            print(f"✅ Unmuted: {destination}" + (f" / Section {section_id}" if section_id else ""))

        elif cmd == "test":
            # Send a test notification
            notifier.send_macos_notification(
                title="🧪 Test: Webhook Section Notification",
                message="This is a test notification from webhook_section_notifications.py",
                subtitle="Testing notification system",
                sound="Purr"
            )
            print("✅ Test notification sent")

        else:
            print(f"Unknown command: {cmd}")
            print("\nCommands:")
            print("  check          - Run health check and notify")
            print("  status         - Show current status")
            print("  mute <dest>    - Mute destination [section_id] [hours]")
            print("  unmute <dest>  - Unmute destination [section_id]")
            print("  test           - Send test notification")
            sys.exit(1)

    else:
        # Default: show quick status
        result = notifier.check_injection_health_and_notify()
        print(f"Overall: {result.get('overall_status', 'unknown')}")
        print(f"8th Intelligence: {result.get('eighth_intelligence_status', 'unknown')}")
        if result.get('notifications_sent'):
            print(f"Notifications sent: {len(result['notifications_sent'])}")
