#!/usr/bin/env python3
"""
INJECTION HEALTH MONITOR - Critical Webhook Payload Monitoring

Monitors the health of Context DNA's webhook injection system:
- Injection frequency (alerts if no injections in X minutes)
- Section health (0-8 sections individually)
- 8th Intelligence special monitoring (NEVER sleeps)
- Payload blob storage health
- Webhook destination tracking (vs_code_claude_code, synaptic_chat, etc.)
- Webhook phase tracking (pre_message, post_message)

This is FOUNDATIONAL infrastructure - if injections stop, Context DNA is broken.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                   INJECTION HEALTH MONITOR                       │
    │                                                                  │
    │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
    │  │ Webhook Dest     │  │ Section Health   │  │ 8th Intel    │  │
    │  │ Monitor          │  │ (0-8)            │  │ Watchdog     │  │
    │  │                  │  │                  │  │ ⚠️ CRITICAL  │  │
    │  │ Destinations:    │  │ Track per-section│  │              │  │
    │  │ - vs_code_claude │  │ - Latency        │  │ NEVER SLEEPS │  │
    │  │ - synaptic_chat  │  │ - Tokens         │  │ Alert if     │  │
    │  │ - chatgpt_app    │  │ - Health status  │  │ missing/down │  │
    │  │ - cursor_overseer│  └──────────────────┘  └──────────────┘  │
    │  │ - antigravity    │                                          │
    │  └──────────────────┘                                          │
    │                                                                  │
    │  ┌──────────────────────────────────────────────────────────┐   │
    │  │                    NOTIFICATIONS                          │   │
    │  │  macOS: "⚠️ vs_code_claude_code pre_message failing"      │   │
    │  │  Specifies: destination + phase + issue                   │   │
    │  └──────────────────────────────────────────────────────────┘   │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    from memory.injection_health_monitor import InjectionHealthMonitor, WebhookDestination, WebhookPhase

    monitor = InjectionHealthMonitor()

    # Record injection with destination and phase
    monitor.record_injection(
        injection_id="inj_abc123",
        sections_included=[0, 1, 6, 8],
        total_latency_ms=150,
        total_tokens=2500,
        eighth_intelligence_present=True,
        destination=WebhookDestination.VS_CODE_CLAUDE_CODE,
        phase=WebhookPhase.PRE_MESSAGE
    )

    # Check health
    health = monitor.check_health()

    # CLI
    python memory/injection_health_monitor.py status
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Literal
from dataclasses import dataclass, asdict, field
from enum import Enum

logger = logging.getLogger(__name__)


# =============================================================================
# WEBHOOK DESTINATION & PHASE ENUMS
# =============================================================================

class WebhookDestination(str, Enum):
    """
    Webhook destination/connection identifiers.

    Each represents a specific agent or tool receiving Context DNA injections.
    """
    VS_CODE_CLAUDE_CODE = "vs_code_claude_code"  # Claude Code in VS Code
    SYNAPTIC_CHAT = "synaptic_chat"              # Synaptic Chat interface
    CHATGPT_APP = "chatgpt_app"                  # ChatGPT app integration
    CURSOR_OVERSEER = "cursor_overseer"          # Cursor AI overseer
    ANTIGRAVITY = "antigravity"                  # Antigravity system
    HERMES = "hermes"                            # Hermes agent (ledger sync)
    WINDSURF = "windsurf"                        # Windsurf editor
    UNKNOWN = "unknown"                          # Fallback for unidentified

    @classmethod
    def from_string(cls, s: str) -> "WebhookDestination":
        """Parse destination from string, with fallback to UNKNOWN."""
        try:
            return cls(s.lower().replace("-", "_").replace(" ", "_"))
        except ValueError:
            return cls.UNKNOWN


class WebhookPhase(str, Enum):
    """
    Webhook injection phase.

    PRE_MESSAGE: Injected before user message sent to LLM (currently active)
    POST_MESSAGE: Injected after LLM response (outcome feedback via handle_post_message_outcome)
    """
    PRE_MESSAGE = "pre_message"    # Before user message → LLM
    POST_MESSAGE = "post_message"  # After LLM response (outcome feedback loop)
    UNKNOWN = "unknown"


# Human-readable names for notifications
DESTINATION_DISPLAY_NAMES = {
    WebhookDestination.VS_CODE_CLAUDE_CODE: "Claude Code (VS Code)",
    WebhookDestination.SYNAPTIC_CHAT: "Synaptic Chat",
    WebhookDestination.CHATGPT_APP: "ChatGPT App",
    WebhookDestination.CURSOR_OVERSEER: "Cursor Overseer",
    WebhookDestination.ANTIGRAVITY: "Antigravity",
    WebhookDestination.HERMES: "Hermes",
    WebhookDestination.WINDSURF: "Windsurf",
    WebhookDestination.UNKNOWN: "Unknown Destination",
}


# Section definitions per 9-SECTION WEBHOOK ARCHITECTURE
SECTIONS = {
    0: {"name": "SAFETY", "description": "Never Do prohibitions", "critical": True},
    1: {"name": "FOUNDATION", "description": "File context, SOPs, patterns", "critical": True},
    2: {"name": "WISDOM", "description": "Professor wisdom distillation", "critical": False},
    3: {"name": "AWARENESS", "description": "Recent changes, ripple effects", "critical": False},
    4: {"name": "DEEP_CONTEXT", "description": "Blueprint, brain state", "critical": False},
    5: {"name": "PROTOCOL", "description": "First-try likelihood, success capture", "critical": False},
    6: {"name": "HOLISTIC_CONTEXT", "description": "Synaptic → Atlas guidance", "critical": False},
    7: {"name": "FULL_LIBRARY", "description": "Extended context (Tier 3)", "critical": False},
    8: {"name": "8TH_INTELLIGENCE", "description": "Synaptic subconscious", "critical": True, "never_sleeps": True},
}

# Thresholds
INJECTION_STALE_MINUTES = 5  # Alert if no injection in this time
INJECTION_RATE_DROP_PCT = 50  # Alert if rate drops by this percentage
SECTION_8_MAX_LATENCY_MS = 500  # 8th Intelligence must be fast


@dataclass
class SectionHealth:
    """Health status for a single section."""
    section_id: int
    name: str
    status: Literal["healthy", "degraded", "down", "missing"]
    last_seen_utc: Optional[str]
    avg_latency_ms: Optional[float]
    avg_tokens: Optional[int]
    healthy_rate: Optional[float]
    injection_count: int
    notes: Optional[str] = None


@dataclass
class DestinationHealth:
    """Health status for a single webhook destination."""
    destination: str
    display_name: str
    status: Literal["healthy", "degraded", "down", "missing"]
    last_seen_utc: Optional[str]
    injection_count_24h: int
    avg_latency_ms: Optional[float]
    pre_message_count: int = 0
    post_message_count: int = 0  # Will be 0 until post_message implemented
    alerts: List[str] = field(default_factory=list)


@dataclass
class InjectionHealth:
    """Overall injection system health."""
    status: Literal["healthy", "warning", "critical"]
    timestamp_utc: str
    last_injection_utc: Optional[str]
    minutes_since_last: Optional[float]
    injection_rate_per_hour: Optional[float]
    total_injections_24h: int
    sections: Dict[int, SectionHealth]
    destinations: Dict[str, DestinationHealth]  # Health per destination
    eighth_intelligence_status: Literal["active", "degraded", "down", "missing"]
    alerts: List[str]
    payload_blob_count: int
    section_blob_count: int


class InjectionHealthMonitor:
    """
    Monitors webhook injection health with special attention to 8th Intelligence.
    """

    def __init__(self):
        self._store = None

    def _get_store(self):
        """Get observability store (lazy init)."""
        if self._store is None:
            try:
                from memory.observability_store import get_observability_store
                self._store = get_observability_store()
            except Exception as e:
                logger.warning(f"Failed to get observability store: {e}")
        return self._store

    def _utc_now(self) -> str:
        """Get current UTC timestamp."""
        return datetime.now(timezone.utc).isoformat()

    def check_health(self) -> InjectionHealth:
        """
        Comprehensive health check of the injection system.

        Returns:
            InjectionHealth with detailed status
        """
        store = self._get_store()
        if not store:
            return InjectionHealth(
                status="critical",
                timestamp_utc=self._utc_now(),
                last_injection_utc=None,
                minutes_since_last=None,
                injection_rate_per_hour=None,
                total_injections_24h=0,
                sections={},
                destinations={},
                eighth_intelligence_status="down",
                alerts=["Observability store unavailable"],
                payload_blob_count=0,
                section_blob_count=0
            )

        now = datetime.now(timezone.utc)
        alerts = []

        # Check last injection time
        last_injection_utc = None
        minutes_since_last = None

        cursor = store._sqlite_conn.execute("""
            SELECT timestamp_utc FROM injection_event
            ORDER BY timestamp_utc DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            last_injection_utc = row["timestamp_utc"]
            last_dt = datetime.fromisoformat(last_injection_utc.replace("Z", "+00:00"))
            minutes_since_last = (now - last_dt).total_seconds() / 60

            if minutes_since_last > INJECTION_STALE_MINUTES:
                alerts.append(f"⚠️ No injection in {minutes_since_last:.1f} minutes (threshold: {INJECTION_STALE_MINUTES})")

        # Count injections in last 24 hours
        cutoff_24h = (now - timedelta(hours=24)).isoformat()
        cursor = store._sqlite_conn.execute("""
            SELECT COUNT(*) as cnt FROM injection_event
            WHERE timestamp_utc >= ?
        """, (cutoff_24h,))
        total_injections_24h = cursor.fetchone()["cnt"]

        # Calculate injection rate
        injection_rate_per_hour = total_injections_24h / 24.0 if total_injections_24h else 0

        # Check section health
        sections = {}
        for section_id, section_info in SECTIONS.items():
            section_health = self._check_section_health(store, section_id, section_info, now)
            sections[section_id] = section_health

            # Alert for critical sections
            if section_info.get("critical") and section_health.status in ("down", "missing"):
                alerts.append(f"🚨 CRITICAL: Section {section_id} ({section_info['name']}) is {section_health.status}")

        # Special 8th Intelligence check
        eighth_intel = sections.get(8)
        eighth_intelligence_status = "missing"
        if eighth_intel:
            if eighth_intel.status == "healthy":
                eighth_intelligence_status = "active"
            elif eighth_intel.status == "degraded":
                eighth_intelligence_status = "degraded"
                alerts.append("⚠️ 8th Intelligence is DEGRADED - subconscious patterns may be affected")
            else:
                eighth_intelligence_status = "down"
                alerts.append("🚨 CRITICAL: 8th Intelligence is DOWN - Synaptic subconscious unavailable")
        else:
            alerts.append("🚨 CRITICAL: 8th Intelligence has NEVER been seen - check webhook configuration")

        # Check payload blob counts
        cursor = store._sqlite_conn.execute("SELECT COUNT(*) as cnt FROM payload_blob")
        payload_blob_count = cursor.fetchone()["cnt"]

        cursor = store._sqlite_conn.execute("SELECT COUNT(*) as cnt FROM section_blob")
        section_blob_count = cursor.fetchone()["cnt"]

        # Check destination health
        destinations = self._check_destination_health(store, now, alerts)

        # Determine overall status
        if any("CRITICAL" in alert for alert in alerts):
            status = "critical"
        elif alerts:
            status = "warning"
        else:
            status = "healthy"

        return InjectionHealth(
            status=status,
            timestamp_utc=self._utc_now(),
            last_injection_utc=last_injection_utc,
            minutes_since_last=minutes_since_last,
            injection_rate_per_hour=injection_rate_per_hour,
            total_injections_24h=total_injections_24h,
            sections=sections,
            destinations=destinations,
            eighth_intelligence_status=eighth_intelligence_status,
            alerts=alerts,
            payload_blob_count=payload_blob_count,
            section_blob_count=section_blob_count
        )

    def _check_section_health(
        self,
        store,
        section_id: int,
        section_info: Dict,
        now: datetime
    ) -> SectionHealth:
        """Check health of a single section."""

        # Get section stats from injection_section
        cursor = store._sqlite_conn.execute("""
            SELECT
                COUNT(*) as injection_count,
                MAX(ie.timestamp_utc) as last_seen,
                AVG(isec.latency_ms) as avg_latency,
                AVG(isec.tokens) as avg_tokens,
                AVG(CASE WHEN isec.health = 'healthy' THEN 1.0 ELSE 0.0 END) as healthy_rate
            FROM injection_section isec
            JOIN injection_event ie ON ie.injection_id = isec.injection_id
            WHERE isec.section_id = ?
              AND ie.timestamp_utc >= ?
        """, (str(section_id), (now - timedelta(hours=24)).isoformat()))

        row = cursor.fetchone()

        if not row or row["injection_count"] == 0:
            return SectionHealth(
                section_id=section_id,
                name=section_info["name"],
                status="missing",
                last_seen_utc=None,
                avg_latency_ms=None,
                avg_tokens=None,
                healthy_rate=None,
                injection_count=0,
                notes="No data in last 24h"
            )

        # Determine status based on healthy_rate and latency
        healthy_rate = row["healthy_rate"] or 0
        avg_latency = row["avg_latency"] or 0

        if healthy_rate >= 0.95:
            status = "healthy"
        elif healthy_rate >= 0.7:
            status = "degraded"
        else:
            status = "down"

        # Special check for 8th Intelligence latency
        if section_id == 8 and avg_latency > SECTION_8_MAX_LATENCY_MS:
            status = "degraded"

        return SectionHealth(
            section_id=section_id,
            name=section_info["name"],
            status=status,
            last_seen_utc=row["last_seen"],
            avg_latency_ms=row["avg_latency"],
            avg_tokens=int(row["avg_tokens"]) if row["avg_tokens"] else None,
            healthy_rate=healthy_rate,
            injection_count=row["injection_count"]
        )

    def _check_destination_health(
        self,
        store,
        now: datetime,
        alerts: List[str]
    ) -> Dict[str, DestinationHealth]:
        """Check health of all webhook destinations."""
        destinations = {}
        cutoff_24h = (now - timedelta(hours=24)).isoformat()

        # Get stats per destination from dependency_status_event
        for dest in WebhookDestination:
            if dest == WebhookDestination.UNKNOWN:
                continue

            dest_str = dest.value
            display_name = DESTINATION_DISPLAY_NAMES.get(dest, dest_str)

            # Query for this destination's pre_message stats
            pre_key = f"webhook_{dest_str}_pre_message"
            cursor = store._sqlite_conn.execute("""
                SELECT
                    COUNT(*) as cnt,
                    MAX(timestamp_utc) as last_seen,
                    AVG(latency_ms) as avg_latency
                FROM dependency_status_event
                WHERE dependency_name = ?
                  AND timestamp_utc >= ?
            """, (pre_key, cutoff_24h))
            pre_row = cursor.fetchone()

            # Query for post_message (will be 0 until implemented)
            post_key = f"webhook_{dest_str}_post_message"
            cursor = store._sqlite_conn.execute("""
                SELECT COUNT(*) as cnt FROM dependency_status_event
                WHERE dependency_name = ?
                  AND timestamp_utc >= ?
            """, (post_key, cutoff_24h))
            post_row = cursor.fetchone()

            pre_count = pre_row["cnt"] if pre_row else 0
            post_count = post_row["cnt"] if post_row else 0
            total_count = pre_count + post_count

            # Determine status
            if total_count == 0:
                status = "missing"
            elif pre_count == 0 and post_count > 0:
                # Only post_message (unexpected - pre should exist)
                status = "degraded"
            elif pre_row and pre_row["avg_latency"] and pre_row["avg_latency"] > 1000:
                status = "degraded"  # High latency
            else:
                status = "healthy"

            dest_alerts = []
            if status == "missing" and dest in [
                WebhookDestination.VS_CODE_CLAUDE_CODE,
                WebhookDestination.SYNAPTIC_CHAT
            ]:
                # Primary destinations should always have data
                alert = f"⚠️ {display_name}: No injections in 24h"
                dest_alerts.append(alert)
                alerts.append(alert)

            destinations[dest_str] = DestinationHealth(
                destination=dest_str,
                display_name=display_name,
                status=status,
                last_seen_utc=pre_row["last_seen"] if pre_row else None,
                injection_count_24h=total_count,
                avg_latency_ms=pre_row["avg_latency"] if pre_row else None,
                pre_message_count=pre_count,
                post_message_count=post_count,
                alerts=dest_alerts
            )

        return destinations

    def record_injection(
        self,
        injection_id: str,
        sections_included: List[int],
        total_latency_ms: int,
        total_tokens: int,
        eighth_intelligence_present: bool = False,
        destination: Optional[WebhookDestination] = None,
        phase: WebhookPhase = WebhookPhase.PRE_MESSAGE
    ) -> str:
        """
        Record an injection event for monitoring.

        Call this from the webhook injection code.

        Args:
            injection_id: Unique injection ID
            sections_included: List of section IDs included (0-8)
            total_latency_ms: Total injection latency
            total_tokens: Total tokens in payload
            eighth_intelligence_present: Whether 8th Intelligence was included
            destination: Which webhook connection (vs_code_claude_code, synaptic_chat, etc.)
            phase: pre_message or post_message (post_message not yet implemented)

        Returns:
            Recorded injection ID

        Example:
            monitor.record_injection(
                injection_id="inj_abc123",
                sections_included=[0, 1, 6, 8],
                total_latency_ms=150,
                total_tokens=2500,
                eighth_intelligence_present=True,
                destination=WebhookDestination.VS_CODE_CLAUDE_CODE,
                phase=WebhookPhase.PRE_MESSAGE
            )
        """
        store = self._get_store()
        if not store:
            logger.warning("Cannot record injection - store unavailable")
            return injection_id

        dest_str = destination.value if destination else "unknown"
        phase_str = phase.value if phase else "unknown"
        dest_display = DESTINATION_DISPLAY_NAMES.get(destination, dest_str)

        # Record to dependency_status_event as a health signal
        store.record_dependency_status(
            dependency_name=f"webhook_{dest_str}_{phase_str}",
            status="healthy",
            latency_ms=total_latency_ms,
            details={
                "injection_id": injection_id,
                "sections": sections_included,
                "tokens": total_tokens,
                "eighth_intelligence": eighth_intelligence_present,
                "destination": dest_str,
                "destination_display": dest_display,
                "phase": phase_str
            }
        )

        # Also record to general webhook_injection for aggregate stats
        store.record_dependency_status(
            dependency_name="webhook_injection",
            status="healthy",
            latency_ms=total_latency_ms,
            details={
                "injection_id": injection_id,
                "sections": sections_included,
                "tokens": total_tokens,
                "destination": dest_str,
                "phase": phase_str
            }
        )

        # Special tracking for 8th Intelligence
        if 8 in sections_included:
            store.record_dependency_status(
                dependency_name="8th_intelligence",
                status="healthy" if eighth_intelligence_present else "degraded",
                details={
                    "injection_id": injection_id,
                    "destination": dest_str,
                    "phase": phase_str
                }
            )

        logger.info(f"📡 Recorded injection: {dest_display} ({phase_str}) - {len(sections_included)} sections, {total_tokens} tokens")
        return injection_id

    def send_notification(self, health: InjectionHealth):
        """
        Send macOS notification for health issues.

        Notifications are specific about:
        - Which webhook destination has issues (vs_code_claude_code, synaptic_chat, etc.)
        - Which phase (pre_message vs post_message)
        - What the specific issue is
        """
        if health.status == "healthy":
            return

        try:
            from memory.mutual_heartbeat import send_macos_notification

            # Build destination-specific message
            failing_destinations = [
                d for d in health.destinations.values()
                if d.status in ("degraded", "down", "missing") and d.alerts
            ]

            if health.status == "critical":
                # Find the most critical issue for subtitle
                critical_alerts = [a for a in health.alerts if "CRITICAL" in a]
                subtitle = critical_alerts[0] if critical_alerts else (health.alerts[0] if health.alerts else "Unknown error")

                # Include destination info in message
                if failing_destinations:
                    dest_names = ", ".join(d.display_name for d in failing_destinations[:3])
                    message = f"Failing: {dest_names}"
                else:
                    message = "Injection system failure detected"

                send_macos_notification(
                    title="🚨 Context DNA CRITICAL",
                    message=message,
                    subtitle=subtitle,
                    sound="Sosumi"  # Alert sound
                )

            else:
                # Warning level - be specific about which destination
                if failing_destinations:
                    dest = failing_destinations[0]
                    subtitle = f"{dest.display_name}: {dest.alerts[0] if dest.alerts else 'degraded'}"
                    message = f"pre_message: {dest.pre_message_count}/24h, post_message: {dest.post_message_count}/24h"
                else:
                    subtitle = health.alerts[0] if health.alerts else "Check dashboard"
                    message = "Injection health degraded"

                send_macos_notification(
                    title="⚠️ Context DNA Warning",
                    message=message,
                    subtitle=subtitle,
                    sound="Purr"
                )

        except Exception as e:
            logger.warning(f"Failed to send notification: {e}")

    def send_destination_notification(
        self,
        destination: WebhookDestination,
        phase: WebhookPhase,
        issue: str,
        severity: Literal["warning", "critical"] = "warning"
    ):
        """
        Send notification for a specific destination/phase issue.

        Use this for immediate notifications when a webhook fails.
        """
        try:
            from memory.mutual_heartbeat import send_macos_notification

            display_name = DESTINATION_DISPLAY_NAMES.get(destination, destination.value)
            phase_str = phase.value.replace("_", "-")

            if severity == "critical":
                send_macos_notification(
                    title=f"🚨 {display_name} CRITICAL",
                    message=f"{phase_str} webhook failure",
                    subtitle=issue,
                    sound="Sosumi"
                )
            else:
                send_macos_notification(
                    title=f"⚠️ {display_name} Warning",
                    message=f"{phase_str} webhook issue",
                    subtitle=issue,
                    sound="Purr"
                )

        except Exception as e:
            logger.warning(f"Failed to send destination notification: {e}")


    def handle_post_message_outcome(
        self,
        injection_id: str,
        outcome: str,
        signals: List[str],
        session_id: str = "",
        confidence: float = 0.5
    ) -> bool:
        """
        Handle POST_MESSAGE phase outcome feedback.

        Closes the feedback loop: injection -> outcome -> hook optimization.
        Looks up which hook variant was active for the given injection,
        then records the outcome via hook_evolution.record_outcome().

        Args:
            injection_id: The injection ID from the original PRE_MESSAGE phase
            outcome: 'positive', 'negative', or 'neutral'
            signals: List of signals that determined the outcome
                     (e.g., ['task_completed', 'no_retries'] or ['user_frustrated', 'multiple_retries'])
            session_id: Session identifier (falls back to injection_id if empty)
            confidence: 0.0-1.0 confidence in the outcome classification

        Returns:
            True if outcome was recorded successfully, False otherwise

        Example:
            monitor = get_webhook_monitor()
            monitor.handle_post_message_outcome(
                injection_id="inj_abc123",
                outcome="positive",
                signals=["task_completed", "user_confirmed"],
                session_id="session_xyz",
                confidence=0.8
            )
        """
        try:
            from memory.hook_evolution import get_hook_evolution_engine

            engine = get_hook_evolution_engine()
            effective_session = session_id or injection_id

            # Look up which hook variant was active for this injection's session.
            # The hook_firings table tracks variant_id -> session_id mappings.
            # We query for the most recent firing in this session.
            variant_id = None
            trigger_context = ""
            risk_level = ""

            try:
                cursor = engine.db.execute("""
                    SELECT variant_id, trigger_context, risk_level
                    FROM hook_firings
                    WHERE session_id = ?
                    ORDER BY fired_at DESC
                    LIMIT 1
                """, (effective_session,))
                row = cursor.fetchone()
                if row:
                    variant_id = row[0]
                    trigger_context = row[1] or ""
                    risk_level = row[2] or ""
            except Exception as e:
                logger.warning(f"Failed to look up hook firing for session {effective_session}: {e}")

            if not variant_id:
                # No hook firing found for this session - try injection_id as session
                try:
                    cursor = engine.db.execute("""
                        SELECT variant_id, trigger_context, risk_level
                        FROM hook_firings
                        WHERE session_id = ?
                        ORDER BY fired_at DESC
                        LIMIT 1
                    """, (injection_id,))
                    row = cursor.fetchone()
                    if row:
                        variant_id = row[0]
                        trigger_context = row[1] or ""
                        risk_level = row[2] or ""
                except Exception as e:
                    print(f"[WARN] Hook firing lookup failed: {e}")

            if not variant_id:
                # Fallback: use default UserPromptSubmit variant
                variant_id = "userpromptsubmit_default"
                logger.info(f"No hook firing found for {effective_session}, using default variant")

            # Record the outcome via hook evolution engine
            success = engine.record_outcome(
                variant_id=variant_id,
                session_id=effective_session,
                outcome=outcome,
                signals=signals,
                confidence=confidence,
                trigger_context=trigger_context,
                risk_level=risk_level
            )

            if success:
                logger.info(
                    f"POST_MESSAGE outcome recorded: {outcome} for variant={variant_id} "
                    f"session={effective_session} (confidence={confidence:.2f})"
                )
            else:
                logger.warning(
                    f"Failed to record POST_MESSAGE outcome for variant={variant_id} "
                    f"session={effective_session}"
                )

            return success

        except ImportError:
            logger.warning("hook_evolution module not available - outcome not recorded")
            return False
        except Exception as e:
            logger.warning(f"handle_post_message_outcome failed: {e}")
            return False


# =============================================================================
# WEBHOOK INTEGRATION HELPER
# =============================================================================

# Singleton monitor for webhook handlers to use
_webhook_monitor: Optional[InjectionHealthMonitor] = None


def get_webhook_monitor() -> InjectionHealthMonitor:
    """Get singleton InjectionHealthMonitor for webhook handlers."""
    global _webhook_monitor
    if _webhook_monitor is None:
        _webhook_monitor = InjectionHealthMonitor()
    return _webhook_monitor


def record_webhook_injection(
    injection_id: str,
    sections_included: List[int],
    total_latency_ms: int,
    total_tokens: int,
    destination: str,
    phase: str = "pre_message",
    eighth_intelligence_present: bool = False
) -> str:
    """
    Convenience function for webhook handlers to record injections.

    This is the function to call from your webhook code.

    Args:
        injection_id: Unique ID for this injection
        sections_included: List of section IDs included (0-8)
        total_latency_ms: Total injection latency in milliseconds
        total_tokens: Total tokens in payload
        destination: Webhook destination (vs_code_claude_code, synaptic_chat, etc.)
        phase: "pre_message" or "post_message"
        eighth_intelligence_present: Whether section 8 content was included

    Returns:
        The injection_id

    Example usage in webhook handler:

        from memory.injection_health_monitor import record_webhook_injection

        # In your webhook handler:
        record_webhook_injection(
            injection_id=f"inj_{uuid4().hex[:8]}",
            sections_included=[0, 1, 6, 8],
            total_latency_ms=150,
            total_tokens=2500,
            destination="vs_code_claude_code",
            phase="pre_message",
            eighth_intelligence_present=True
        )
    """
    monitor = get_webhook_monitor()

    # Parse destination and phase
    dest_enum = WebhookDestination.from_string(destination)
    try:
        phase_enum = WebhookPhase(phase)
    except ValueError:
        phase_enum = WebhookPhase.UNKNOWN

    return monitor.record_injection(
        injection_id=injection_id,
        sections_included=sections_included,
        total_latency_ms=total_latency_ms,
        total_tokens=total_tokens,
        eighth_intelligence_present=eighth_intelligence_present,
        destination=dest_enum,
        phase=phase_enum
    )



def handle_post_message_outcome(
    injection_id: str,
    outcome: str,
    signals: List[str],
    session_id: str = "",
    confidence: float = 0.5
) -> bool:
    """
    Convenience function to handle POST_MESSAGE outcome feedback.

    Creates the feedback loop: injection -> outcome -> hook optimization.

    Args:
        injection_id: The injection ID from the original PRE_MESSAGE phase
        outcome: 'positive', 'negative', or 'neutral'
        signals: List of signals determining the outcome
        session_id: Session identifier (falls back to injection_id)
        confidence: 0.0-1.0 confidence in the outcome

    Returns:
        True if outcome was recorded successfully

    Example:
        from memory.injection_health_monitor import handle_post_message_outcome

        handle_post_message_outcome(
            injection_id="inj_abc123",
            outcome="positive",
            signals=["task_completed", "user_confirmed"],
            session_id="session_xyz"
        )
    """
    monitor = get_webhook_monitor()
    return monitor.handle_post_message_outcome(
        injection_id=injection_id,
        outcome=outcome,
        signals=signals,
        session_id=session_id,
        confidence=confidence
    )


# =============================================================================
# AUTO-RECOVERY INTEGRATION (M3.6+)
# =============================================================================

def _invoke_ts_recovery(gate: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call Node.js CLI bridge to run TypeScript recovery kernel.

    Args:
        gate: Quality gate dict from Redis

    Returns:
        Recovery result dict with keys: recovered, new_status, recordId, actions
    """
    import subprocess
    import uuid
    import os
    import tempfile

    # Write gate to temp file
    gate_path = os.path.join(tempfile.gettempdir(), f'quality_gate_{uuid.uuid4().hex}.json')
    try:
        with open(gate_path, 'w') as f:
            json.dump(gate, f)

        # Call TypeScript recovery bridge via tsx (timeout 10s)
        tsx_path = Path(__file__).parent.parent / 'context-dna/engine/node_modules/.bin/tsx'
        result = subprocess.run(
            [str(tsx_path), 'context-dna/engine/scripts/cli-recovery.ts', gate_path],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=Path(__file__).parent.parent,  # superrepo root
        )

        if result.returncode == 0:
            return json.loads(result.stdout)
        else:
            logger.error(f"Recovery bridge failed: {result.stderr}")
            return {'error': result.stderr, 'recovered': False}

    except subprocess.TimeoutExpired:
        logger.error("Recovery bridge timed out after 10s")
        return {'error': 'timeout', 'recovered': False}
    except Exception as e:
        logger.error(f"Recovery bridge exception: {e}")
        return {'error': str(e), 'recovered': False}
    finally:
        # Clean up temp file
        if os.path.exists(gate_path):
            os.remove(gate_path)


def trigger_auto_recovery_if_needed() -> Dict[str, Any]:
    """
    Check last quality gate, trigger recovery if FAIL.

    Called by scheduler (every 60s via run_injection_health_check).

    Returns:
        Dict with keys: triggered (bool), reason (str), result (dict if triggered)
    """
    try:
        import redis
        r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)

        # Read last quality gate (populated by agent_service on each injection)
        gate_json = r.get('contextdna:quality:last_gate')
        if not gate_json:
            return {'triggered': False, 'reason': 'No recent quality gate in Redis'}

        gate = json.loads(gate_json)
        if gate.get('status') != 'FAIL':
            return {'triggered': False, 'reason': f"Status: {gate.get('status', 'unknown')}"}

        # Invoke recovery via Node.js CLI bridge
        logger.info(f"Auto-recovery triggered for FAIL gate: {gate.get('blockedReason', 'unknown')}")
        result = _invoke_ts_recovery(gate)

        # Record auto-recovery event
        r.incr('contextdna:recovery:auto_triggered:total')

        return {'triggered': True, 'result': result}

    except Exception as e:
        logger.error(f"Auto-recovery check failed: {e}")
        return {'triggered': False, 'reason': f'error: {str(e)}'}


# =============================================================================
# SCHEDULED JOB FUNCTION (for lite_scheduler)
# =============================================================================

def run_injection_health_check() -> tuple[bool, str]:
    """
    Scheduled job function for injection health monitoring.

    Returns:
        (success, message) tuple for lite_scheduler
    """
    try:
        monitor = InjectionHealthMonitor()
        health = monitor.check_health()

        # Send notification if unhealthy
        if health.status != "healthy":
            monitor.send_notification(health)

        # Record to observability
        store = monitor._get_store()
        if store:
            store.record_dependency_status(
                dependency_name="injection_system",
                status="healthy" if health.status == "healthy" else "degraded",
                details={
                    "status": health.status,
                    "eighth_intelligence": health.eighth_intelligence_status,
                    "injections_24h": health.total_injections_24h,
                    "alerts_count": len(health.alerts)
                }
            )

        status_msg = f"{health.status}: {health.total_injections_24h} injections/24h, 8th={health.eighth_intelligence_status}"
        if health.alerts:
            status_msg += f", {len(health.alerts)} alerts"

        # Auto-recovery trigger (M3.6+)
        auto_result = trigger_auto_recovery_if_needed()
        if auto_result['triggered']:
            logger.info(f"Auto-recovery triggered: {auto_result}")
            status_msg += f", auto-recovery: {auto_result['result'].get('new_status', 'unknown')}"

        # Only fail on critical — warnings are informational, not failures
        return health.status != "critical", status_msg

    except Exception as e:
        return False, str(e)


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    monitor = InjectionHealthMonitor()

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()

        if cmd == "status":
            health = monitor.check_health()

            print("=" * 70)
            print("INJECTION HEALTH MONITOR")
            print("=" * 70)

            # Overall status with color
            status_icon = {"healthy": "✅", "warning": "⚠️", "critical": "🚨"}[health.status]
            print(f"\nOverall Status: {status_icon} {health.status.upper()}")
            print(f"Timestamp: {health.timestamp_utc[:19]}")

            # Injection stats
            print(f"\n📊 INJECTION STATS")
            print(f"  Last injection: {health.last_injection_utc[:19] if health.last_injection_utc else 'NEVER'}")
            if health.minutes_since_last:
                print(f"  Minutes since last: {health.minutes_since_last:.1f}")
            print(f"  Rate: {health.injection_rate_per_hour:.1f}/hour")
            print(f"  Total (24h): {health.total_injections_24h}")

            # 8th Intelligence
            eighth_icon = {"active": "🧠", "degraded": "⚠️", "down": "🚨", "missing": "❓"}[health.eighth_intelligence_status]
            print(f"\n🧠 8TH INTELLIGENCE: {eighth_icon} {health.eighth_intelligence_status.upper()}")

            # Section health
            print(f"\n📑 SECTION HEALTH (0-8)")
            for section_id in sorted(health.sections.keys()):
                sec = health.sections[section_id]
                icon = {"healthy": "✅", "degraded": "⚠️", "down": "🚨", "missing": "❓"}[sec.status]
                latency = f"{sec.avg_latency_ms:.0f}ms" if sec.avg_latency_ms else "N/A"
                print(f"  {icon} Section {section_id} ({sec.name}): {sec.status} | {latency} | {sec.injection_count} inj")

            # Destination health
            print(f"\n📡 WEBHOOK DESTINATIONS")
            for dest_id in sorted(health.destinations.keys()):
                dest = health.destinations[dest_id]
                icon = {"healthy": "✅", "degraded": "⚠️", "down": "🚨", "missing": "❓"}[dest.status]
                latency = f"{dest.avg_latency_ms:.0f}ms" if dest.avg_latency_ms else "N/A"
                print(f"  {icon} {dest.display_name}")
                print(f"      Status: {dest.status} | Latency: {latency}")
                print(f"      pre_message: {dest.pre_message_count}/24h | post_message: {dest.post_message_count}/24h")

            # Storage
            print(f"\n💾 BLOB STORAGE")
            print(f"  Payload blobs: {health.payload_blob_count}")
            print(f"  Section blobs: {health.section_blob_count}")

            # Alerts
            if health.alerts:
                print(f"\n🚨 ALERTS ({len(health.alerts)})")
                for alert in health.alerts:
                    print(f"  {alert}")

        elif cmd == "check":
            success, message = run_injection_health_check()
            print(f"{'✅' if success else '❌'} {message}")
            sys.exit(0 if success else 1)

        elif cmd == "notify":
            health = monitor.check_health()
            if health.status != "healthy":
                monitor.send_notification(health)
                print(f"Sent notification for {health.status} status")
            else:
                print("System healthy, no notification needed")

        else:
            print(f"Unknown command: {cmd}")
            print("Commands: status, check, notify")
            sys.exit(1)
    else:
        # Default: show status
        health = monitor.check_health()
        print(f"Injection Health: {health.status}")
        print(f"8th Intelligence: {health.eighth_intelligence_status}")
        print(f"Injections (24h): {health.total_injections_24h}")
        if health.alerts:
            print(f"Alerts: {len(health.alerts)}")
            for alert in health.alerts[:3]:
                print(f"  - {alert}")
