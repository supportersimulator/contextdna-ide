#!/usr/bin/env python3
"""
SYNAPTIC WATCHDOG DAEMON

Background daemon that monitors system health and reports to Synaptic.
Can be run as a systemd service, launchd daemon, or standalone process.

Key Features:
- Periodic health checks (configurable interval)
- Memory source monitoring
- Auto-recovery for known issues
- Logs to file for debugging
- Graceful shutdown on SIGTERM/SIGINT

Usage:
    # Run as daemon
    python memory/synaptic_watchdog_daemon.py

    # Run as background process
    python memory/synaptic_watchdog_daemon.py &

    # Check if running
    pgrep -f synaptic_watchdog_daemon

Installation (macOS launchd):
    cp scripts/launchd/com.contextdna.watchdog.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.contextdna.watchdog.plist

Installation (Linux systemd):
    sudo cp scripts/systemd/synaptic-watchdog.service /etc/systemd/system/
    sudo systemctl enable synaptic-watchdog
    sudo systemctl start synaptic-watchdog
"""

import signal
import sys
import time
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
import json

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.synaptic_health_alerts import get_health_system, check_health

# Mutual heartbeat - monitors agent_service, agent_service monitors us
try:
    from memory.mutual_heartbeat import MutualHeartbeat, SERVICE_WATCHDOG, check_docker_status
    MUTUAL_HEARTBEAT_AVAILABLE = True
except ImportError:
    MUTUAL_HEARTBEAT_AVAILABLE = False
    MutualHeartbeat = None

# Scheduler coordinator - manages lite/heavy mode scheduler handoffs
try:
    from memory.scheduler_coordinator import SchedulerCoordinator
    SCHEDULER_COORDINATOR_AVAILABLE = True
except ImportError:
    SCHEDULER_COORDINATOR_AVAILABLE = False
    SchedulerCoordinator = None

# Configuration
CHECK_INTERVAL_SECONDS = 300  # 5 minutes
LOG_FILE = Path(__file__).parent / ".watchdog.log"
PID_FILE = Path(__file__).parent / ".watchdog.pid"
STATUS_FILE = Path(__file__).parent / ".watchdog_status.json"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class WatchdogDaemon:
    """
    Background watchdog daemon for Synaptic health monitoring.
    """

    def __init__(self, check_interval: int = CHECK_INTERVAL_SECONDS):
        self.check_interval = check_interval
        self.running = False
        self.health_system = get_health_system()
        self.start_time: Optional[datetime] = None
        self.check_count = 0
        self.alert_count = 0

        # Mutual heartbeat - monitors agent_service, agent_service monitors us
        self.mutual_heartbeat = None
        if MUTUAL_HEARTBEAT_AVAILABLE:
            self.mutual_heartbeat = MutualHeartbeat(SERVICE_WATCHDOG)

        # Scheduler coordinator - manages lite/heavy mode scheduler handoffs
        self.scheduler_coordinator = None
        if SCHEDULER_COORDINATOR_AVAILABLE:
            self.scheduler_coordinator = SchedulerCoordinator()

    def _write_pid(self):
        """Write PID file for process management."""
        PID_FILE.write_text(str(os.getpid()))
        logger.info(f"PID file written: {PID_FILE}")

    def _remove_pid(self):
        """Remove PID file on shutdown."""
        if PID_FILE.exists():
            PID_FILE.unlink()
            logger.info("PID file removed")

    def _write_status(self, status: str, health: dict = None):
        """Write current status to file for external monitoring."""
        status_data = {
            "status": status,
            "pid": os.getpid(),
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "uptime_seconds": (datetime.now() - self.start_time).total_seconds() if self.start_time else 0,
            "check_count": self.check_count,
            "alert_count": self.alert_count,
            "last_check": datetime.now().isoformat(),
            "check_interval_seconds": self.check_interval,
            "health_summary": health if health else {}
        }
        STATUS_FILE.write_text(json.dumps(status_data, indent=2))

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def _handle_alert(self, alert):
        """Callback for health alerts."""
        self.alert_count += 1
        logger.warning(f"ALERT [{alert.severity}] {alert.source}: {alert.message}")

        # Future: Send to Synaptic chat, notification system, etc.

    def _run_check(self):
        """Run a single health check cycle."""
        self.check_count += 1
        logger.info(f"Running health check #{self.check_count}...")

        try:
            health = check_health()
            self._write_status("running", health)

            if health["status"] == "healthy":
                logger.info(f"✅ Health check passed: {health['sources_healthy']} sources healthy")
            elif health["status"] == "warning":
                logger.warning(f"⚠️  Health check warning: {health['warning_alerts']} warnings")
            else:
                logger.error(f"❌ Health check critical: {health['critical_alerts']} critical alerts")

            # Also check injection health (CRITICAL for webhook payloads)
            self._check_injection_health()

            # Comprehensive health check (all 9 subsystems + macOS notifications)
            self._check_comprehensive_health()

        except Exception as e:
            logger.error(f"Health check failed: {e}")
            self._write_status("error")

    def _check_injection_health(self):
        """
        Check webhook injection health (FOUNDATIONAL).

        Monitors:
        - Injection frequency (alert if stale)
        - Section health (0-8)
        - 8th Intelligence (NEVER SLEEPS)
        """
        try:
            from memory.injection_health_monitor import get_webhook_monitor

            monitor = get_webhook_monitor()
            health = monitor.check_health()

            # Log status
            if health.status == "healthy":
                logger.info(f"🧬 Injection health: OK ({health.total_injections_24h} inj/24h, 8th={health.eighth_intelligence_status})")
            elif health.status == "warning":
                logger.warning(f"⚠️  Injection health: WARNING - {health.alerts[0] if health.alerts else 'unknown'}")
                monitor.send_notification(health)
            else:
                logger.error(f"🚨 Injection health: CRITICAL - {health.alerts[0] if health.alerts else 'unknown'}")
                monitor.send_notification(health)

            # Special alert for 8th Intelligence
            if health.eighth_intelligence_status in ("down", "missing"):
                logger.error("🚨 8TH INTELLIGENCE DOWN - Synaptic subconscious unavailable!")
                self.alert_count += 1

        except ImportError:
            logger.debug("Injection health monitor not available")
        except Exception as e:
            logger.warning(f"Injection health check failed: {e}")

    def _check_comprehensive_health(self):
        """Run comprehensive health check across all 9 subsystems."""
        try:
            from memory.comprehensive_health_monitor import run_comprehensive_health_check
            success, msg = run_comprehensive_health_check()
            if success:
                logger.info("Comprehensive health: " + msg)
            else:
                logger.error("Comprehensive health CRITICAL: " + msg)
                self.alert_count += 1
        except ImportError:
            logger.debug("Comprehensive health monitor not available")
        except Exception as e:
            logger.warning("Comprehensive health check failed: " + str(e))

    def start(self):
        """Start the watchdog daemon."""
        import threading

        logger.info("=" * 60)
        logger.info("SYNAPTIC WATCHDOG DAEMON STARTING")
        logger.info(f"Check interval: {self.check_interval} seconds")
        logger.info("=" * 60)

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # Register alert callback
        self.health_system.register_alert_callback(self._handle_alert)

        # Write PID and initial status
        self._write_pid()
        self.start_time = datetime.now()
        self._write_status("starting")
        self.running = True

        # Start mutual heartbeat in background thread (monitors agent_service)
        heartbeat_thread = None
        if self.mutual_heartbeat:
            def run_heartbeat():
                # Set debug info
                self.mutual_heartbeat.set_debug_info("check_count", 0)
                self.mutual_heartbeat.set_debug_info("alert_count", 0)
                self.mutual_heartbeat.start_sync()

            heartbeat_thread = threading.Thread(target=run_heartbeat, daemon=True)
            heartbeat_thread.start()
            logger.info("🤝 Mutual heartbeat active (watching agent_service)")

        # Start scheduler coordinator in background thread (manages lite/heavy handoffs)
        scheduler_thread = None
        if self.scheduler_coordinator:
            def run_scheduler():
                self.scheduler_coordinator.start()

            scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
            scheduler_thread.start()
            logger.info("📅 Scheduler coordinator active (lite↔heavy handoffs)")

        # Initial check
        self._run_check()

        # Main loop
        while self.running:
            try:
                time.sleep(self.check_interval)
                if self.running:  # Check again after sleep
                    self._run_check()

                    # Update heartbeat debug info
                    if self.mutual_heartbeat:
                        self.mutual_heartbeat.set_debug_info("check_count", self.check_count)
                        self.mutual_heartbeat.set_debug_info("alert_count", self.alert_count)

            except InterruptedError:
                break

        # Cleanup
        logger.info("Watchdog daemon shutting down...")
        if self.scheduler_coordinator:
            logger.info("Stopping scheduler coordinator...")
            self.scheduler_coordinator.stop()
        if self.mutual_heartbeat:
            self.mutual_heartbeat.stop_sync()
        self._write_status("stopped")
        self._remove_pid()
        logger.info("Watchdog daemon stopped")


def is_running() -> bool:
    """Check if watchdog daemon is already running."""
    if not PID_FILE.exists():
        return False

    try:
        pid = int(PID_FILE.read_text().strip())
        # Check if process exists
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        # Process doesn't exist or invalid PID
        PID_FILE.unlink()
        return False


def get_status() -> dict:
    """Get current watchdog status."""
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except json.JSONDecodeError as e:
            print(f"[WARN] Watchdog status file parse failed: {e}")
    return {"status": "unknown", "running": is_running()}


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "status":
            status = get_status()
            print(f"Watchdog Status: {status.get('status', 'unknown')}")
            if status.get('pid'):
                print(f"  PID: {status['pid']}")
            if status.get('uptime_seconds'):
                print(f"  Uptime: {status['uptime_seconds']:.0f} seconds")
            if status.get('check_count'):
                print(f"  Health checks: {status['check_count']}")

            # Show scheduler coordinator status
            if SCHEDULER_COORDINATOR_AVAILABLE:
                from memory.scheduler_coordinator import get_scheduler_state
                sched_state = get_scheduler_state()
                print(f"\nScheduler Coordinator:")
                print(f"  Mode: {sched_state.get('mode', 'unknown')}")
                print(f"  Active: {sched_state.get('active_scheduler', 'unknown')}")

            sys.exit(0)
        elif cmd == "stop":
            if PID_FILE.exists():
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                print(f"Sent SIGTERM to watchdog (PID {pid})")
            else:
                print("Watchdog is not running")
            sys.exit(0)

    if is_running():
        print("Watchdog daemon is already running!")
        sys.exit(1)

    daemon = WatchdogDaemon()
    daemon.start()
