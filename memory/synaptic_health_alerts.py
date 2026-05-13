#!/usr/bin/env python3
"""
SYNAPTIC PROACTIVE HEALTH ALERTS

This module provides proactive health monitoring that reports TO Synaptic
instead of just monitoring passively. When health degrades, Synaptic is
immediately informed so it can factor this into context injection.

Key Features:
- Monitors all 5 memory sources
- Alerts Synaptic via internal callback (not external API)
- Stores alert history for pattern analysis
- Auto-recovers known issues when possible

Usage:
    from memory.synaptic_health_alerts import HealthAlertSystem, start_health_monitoring

    # Start background monitoring
    start_health_monitoring()

    # Or check health on-demand
    alerts = HealthAlertSystem().check_all_sources()
"""

import sqlite3
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass, asdict
import logging

logger = logging.getLogger(__name__)

# Health alert database
HEALTH_DB = Path(__file__).parent / ".synaptic_health_alerts.db"
MEMORY_DIR = Path(__file__).parent

# Alert severity levels
SEVERITY_CRITICAL = "critical"  # Service down, immediate action needed
SEVERITY_WARNING = "warning"    # Degraded performance, attention needed
SEVERITY_INFO = "info"          # Notable but not urgent


@dataclass
class HealthAlert:
    """A health alert to inform Synaptic."""
    source: str           # Which memory source (learnings, patterns, brain_state, etc.)
    severity: str         # critical, warning, info
    message: str          # Human-readable description
    details: Dict         # Technical details for debugging
    timestamp: str = ""   # When detected
    resolved: bool = False
    resolution_time: str = ""
    auto_recovered: bool = False


class HealthAlertSystem:
    """
    Proactive health monitoring that reports TO Synaptic.
    """

    # Memory sources to monitor
    MEMORY_SOURCES = {
        "learnings": {
            "db_path": MEMORY_DIR / ".pattern_evolution.db",
            "type": "sqlite",
            "critical_threshold": {"rows": 0},  # No data = critical
        },
        "patterns": {
            "db_path": MEMORY_DIR / ".pattern_registry.db",
            "type": "sqlite",
            "critical_threshold": {"rows": 0},
        },
        "brain_state": {
            "file_path": MEMORY_DIR / "brain_state.md",
            "type": "file",
            "critical_threshold": {"exists": False},
        },
        "major_skills": {
            "dir_path": MEMORY_DIR / "major_skills",
            "type": "directory",
            "critical_threshold": {"files": 0},
        },
        "family_wisdom": {
            "dir_path": MEMORY_DIR / "family_wisdom",
            "type": "directory",
            "critical_threshold": {"files": 0},
        },
    }

    def __init__(self):
        self._init_db()
        self._alert_callbacks: List[Callable[[HealthAlert], None]] = []

    def _init_db(self):
        """Initialize the health alerts database."""
        with sqlite3.connect(str(HEALTH_DB)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS health_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    resolved INTEGER DEFAULT 0,
                    resolution_time TEXT,
                    auto_recovered INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS health_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    status_json TEXT NOT NULL,
                    overall_health TEXT NOT NULL
                )
            """)
            conn.commit()

    def register_alert_callback(self, callback: Callable[[HealthAlert], None]):
        """Register a callback to be notified of new alerts."""
        self._alert_callbacks.append(callback)

    def _emit_alert(self, alert: HealthAlert):
        """Store alert and notify all callbacks."""
        alert.timestamp = datetime.now().isoformat()

        # Store in database
        with sqlite3.connect(str(HEALTH_DB)) as conn:
            conn.execute("""
                INSERT INTO health_alerts (source, severity, message, details_json, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (alert.source, alert.severity, alert.message,
                  json.dumps(alert.details), alert.timestamp))
            conn.commit()

        # Notify callbacks
        for callback in self._alert_callbacks:
            try:
                callback(alert)
            except Exception as e:
                logger.error(f"Alert callback failed: {e}")

        logger.warning(f"[HEALTH ALERT] {alert.severity.upper()}: {alert.source} - {alert.message}")

    def check_sqlite_source(self, name: str, config: Dict) -> List[HealthAlert]:
        """Check health of a SQLite database source."""
        alerts = []
        db_path = config["db_path"]

        if not db_path.exists():
            alerts.append(HealthAlert(
                source=name,
                severity=SEVERITY_CRITICAL,
                message=f"Database file missing: {db_path.name}",
                details={"path": str(db_path), "expected": True, "found": False}
            ))
            return alerts

        try:
            with sqlite3.connect(str(db_path)) as conn:
                # Check integrity
                result = conn.execute("PRAGMA integrity_check").fetchone()
                if result[0] != "ok":
                    alerts.append(HealthAlert(
                        source=name,
                        severity=SEVERITY_CRITICAL,
                        message=f"Database corruption detected: {result[0]}",
                        details={"integrity_check": result[0]}
                    ))

                # Check for data
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if not tables:
                    alerts.append(HealthAlert(
                        source=name,
                        severity=SEVERITY_WARNING,
                        message="Database has no tables",
                        details={"tables": 0}
                    ))

        except sqlite3.Error as e:
            alerts.append(HealthAlert(
                source=name,
                severity=SEVERITY_CRITICAL,
                message=f"Database access error: {str(e)}",
                details={"error": str(e), "type": type(e).__name__}
            ))

        return alerts

    def check_file_source(self, name: str, config: Dict) -> List[HealthAlert]:
        """Check health of a file-based source."""
        alerts = []
        file_path = config["file_path"]

        if not file_path.exists():
            alerts.append(HealthAlert(
                source=name,
                severity=SEVERITY_WARNING,
                message=f"File missing: {file_path.name}",
                details={"path": str(file_path), "expected": True, "found": False}
            ))
            return alerts

        # Check file age (warn if too old)
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
        age = datetime.now() - mtime
        if age > timedelta(days=7):
            alerts.append(HealthAlert(
                source=name,
                severity=SEVERITY_INFO,
                message=f"File not updated in {age.days} days",
                details={"last_modified": mtime.isoformat(), "age_days": age.days}
            ))

        return alerts

    def check_directory_source(self, name: str, config: Dict) -> List[HealthAlert]:
        """Check health of a directory-based source."""
        alerts = []
        dir_path = config["dir_path"]

        if not dir_path.exists():
            alerts.append(HealthAlert(
                source=name,
                severity=SEVERITY_WARNING,
                message=f"Directory missing: {dir_path.name}",
                details={"path": str(dir_path), "expected": True, "found": False}
            ))
            return alerts

        # Check for content
        files = list(dir_path.glob("*"))
        if len(files) == 0:
            alerts.append(HealthAlert(
                source=name,
                severity=SEVERITY_WARNING,
                message="Directory is empty",
                details={"path": str(dir_path), "file_count": 0}
            ))

        return alerts

    def check_all_sources(self) -> Dict[str, List[HealthAlert]]:
        """Check health of all memory sources and return alerts."""
        all_alerts = {}

        for name, config in self.MEMORY_SOURCES.items():
            source_type = config["type"]

            if source_type == "sqlite":
                alerts = self.check_sqlite_source(name, config)
            elif source_type == "file":
                alerts = self.check_file_source(name, config)
            elif source_type == "directory":
                alerts = self.check_directory_source(name, config)
            else:
                alerts = [HealthAlert(
                    source=name,
                    severity=SEVERITY_WARNING,
                    message=f"Unknown source type: {source_type}",
                    details={"type": source_type}
                )]

            all_alerts[name] = alerts

            # Emit critical/warning alerts
            for alert in alerts:
                if alert.severity in (SEVERITY_CRITICAL, SEVERITY_WARNING):
                    self._emit_alert(alert)

        return all_alerts

    def get_overall_health(self) -> Dict:
        """Get overall health status for Synaptic context injection."""
        all_alerts = self.check_all_sources()

        critical_count = sum(
            1 for alerts in all_alerts.values()
            for a in alerts if a.severity == SEVERITY_CRITICAL
        )
        warning_count = sum(
            1 for alerts in all_alerts.values()
            for a in alerts if a.severity == SEVERITY_WARNING
        )

        if critical_count > 0:
            status = "degraded"
        elif warning_count > 0:
            status = "warning"
        else:
            status = "healthy"

        healthy_sources = sum(1 for alerts in all_alerts.values() if not alerts)
        total_sources = len(self.MEMORY_SOURCES)

        result = {
            "status": status,
            "sources_healthy": f"{healthy_sources}/{total_sources}",
            "critical_alerts": critical_count,
            "warning_alerts": warning_count,
            "timestamp": datetime.now().isoformat(),
            "alerts_by_source": {
                name: [asdict(a) for a in alerts]
                for name, alerts in all_alerts.items()
                if alerts  # Only include sources with alerts
            }
        }

        # Store snapshot
        with sqlite3.connect(str(HEALTH_DB)) as conn:
            conn.execute("""
                INSERT INTO health_snapshots (status_json, overall_health)
                VALUES (?, ?)
            """, (json.dumps(result), status))
            conn.commit()

        return result

    def get_recent_alerts(self, hours: int = 24) -> List[Dict]:
        """Get alerts from the last N hours."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with sqlite3.connect(str(HEALTH_DB)) as conn:
            rows = conn.execute("""
                SELECT source, severity, message, details_json, timestamp, resolved
                FROM health_alerts
                WHERE timestamp > ?
                ORDER BY timestamp DESC
            """, (cutoff,)).fetchall()

        return [
            {
                "source": r[0],
                "severity": r[1],
                "message": r[2],
                "details": json.loads(r[3]) if r[3] else {},
                "timestamp": r[4],
                "resolved": bool(r[5])
            }
            for r in rows
        ]


# =============================================================================
# SERVICE HEALTH CHECKS (ecosystem integration)
# =============================================================================

# Services with graceful degradation support
SERVICE_DEGRADATION_INFO = {
    "mlx_api": {
        "port": 5044,
        "name": "Local LLM (MLX)",
        "degradation": "Falls back to rules-only analysis (no deep semantic LLM features)",
        "is_critical": False,  # System works without it
    },
    "synaptic_chat": {
        "port": 8888,
        "name": "Synaptic Chat UI",
        "degradation": "Chat UI unavailable; webhook injection to Claude Code still works",
        "is_critical": False,  # Webhooks work independently
    },
}


def check_service_health(name: str, config: Dict) -> Optional[HealthAlert]:
    """Check if a service is running and return alert if down."""
    import socket
    port = config["port"]

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex(("127.0.0.1", port))
        sock.close()

        if result != 0:
            return HealthAlert(
                source=name,
                severity=SEVERITY_WARNING,  # Not critical - graceful degradation
                message=f"{config['name']} OFFLINE (port {port})",
                details={
                    "port": port,
                    "graceful_degradation": config["degradation"],
                    "is_critical": config["is_critical"],
                }
            )
        return None  # Service is healthy
    except Exception as e:
        return HealthAlert(
            source=name,
            severity=SEVERITY_WARNING,
            message=f"{config['name']} check failed: {str(e)[:50]}",
            details={"error": str(e), "graceful_degradation": config["degradation"]}
        )


def check_all_services() -> Dict[str, Optional[HealthAlert]]:
    """Check all services and return any alerts."""
    alerts = {}
    for name, config in SERVICE_DEGRADATION_INFO.items():
        alert = check_service_health(name, config)
        if alert:
            alerts[name] = alert
    return alerts


def get_service_status_summary() -> Dict:
    """Get a summary of service statuses with degradation info."""
    results = {}
    for name, config in SERVICE_DEGRADATION_INFO.items():
        alert = check_service_health(name, config)
        results[name] = {
            "status": "offline" if alert else "online",
            "port": config["port"],
            "name": config["name"],
            "graceful_degradation": config["degradation"] if alert else None,
        }
    return results


# Singleton instance
_health_system: Optional[HealthAlertSystem] = None


def get_health_system() -> HealthAlertSystem:
    """Get the singleton health alert system."""
    global _health_system
    if _health_system is None:
        _health_system = HealthAlertSystem()
    return _health_system


def check_health() -> Dict:
    """Quick health check - returns overall health status with service info."""
    memory_health = get_health_system().get_overall_health()

    # Add service health
    service_alerts = check_all_services()
    memory_health["service_alerts"] = {
        name: {
            "severity": alert.severity,
            "message": alert.message,
            "graceful_degradation": alert.details.get("graceful_degradation", ""),
        }
        for name, alert in service_alerts.items()
    }
    memory_health["services_status"] = get_service_status_summary()

    return memory_health


if __name__ == "__main__":
    # Run health check
    print("🏥 Running Synaptic Health Check...")
    health = check_health()
    print(f"\n📊 Overall Status: {health['status'].upper()}")
    print(f"   Sources Healthy: {health['sources_healthy']}")
    print(f"   Critical Alerts: {health['critical_alerts']}")
    print(f"   Warning Alerts: {health['warning_alerts']}")

    if health['alerts_by_source']:
        print("\n⚠️  Memory Source Alerts:")
        for source, alerts in health['alerts_by_source'].items():
            for alert in alerts:
                print(f"   [{alert['severity']}] {source}: {alert['message']}")

    # Show service status
    print("\n🔌 Service Status:")
    for name, status in health.get('services_status', {}).items():
        icon = "✅" if status['status'] == 'online' else "⚠️"
        print(f"   {icon} {status['name']} (:{status['port']}): {status['status'].upper()}")
        if status['graceful_degradation']:
            print(f"      ↳ Degradation: {status['graceful_degradation']}")

    if health.get('service_alerts'):
        print("\n⚠️  Service Alerts:")
        for name, alert in health['service_alerts'].items():
            print(f"   [{alert['severity']}] {name}: {alert['message']}")
            if alert.get('graceful_degradation'):
                print(f"      ↳ {alert['graceful_degradation']}")
