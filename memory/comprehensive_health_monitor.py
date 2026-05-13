#!/usr/bin/env python3
"""
COMPREHENSIVE HEALTH MONITOR - Nothing Silently Fails

Unified health monitoring for ALL Context DNA subsystems with macOS notifications.

Usage:
    python memory/comprehensive_health_monitor.py check
    python memory/comprehensive_health_monitor.py status
    python memory/comprehensive_health_monitor.py test-notify
"""

import json, logging, os, subprocess, sys, time, urllib.request, urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
MEMORY_DIR = Path(__file__).parent
NOTIFICATION_STATE_FILE = MEMORY_DIR / ".health_notification_state.json"
HEALTH_STATE_FILE = MEMORY_DIR / ".comprehensive_health_state.json"
NOTIFICATION_COOLDOWN_SECONDS = 900

SUBSYSTEMS = {
    "evidence_pipeline": {"display": "Evidence Pipeline", "critical": True},
    "webhook_sections": {"display": "Webhook Sections (0-8)", "critical": True},
    "auto_capture": {"display": "Auto-Capture System", "critical": False},
    "professor": {"display": "Professor (Wisdom)", "critical": False},
    "llm_endpoint": {"display": "LLM Endpoint (mlx_lm)", "critical": False},
    "learning_queries": {"display": "Section 1 Learning Queries", "critical": True},
    "injection_system": {"display": "Injection System", "critical": True},
    "observability_store": {"display": "Observability Store", "critical": True},
    "service_heartbeats": {"display": "Service Heartbeats", "critical": True},
    "process_integrity": {"display": "Process Integrity", "critical": True},
}

@dataclass
class SubsystemHealth:
    name: str
    display_name: str
    status: str
    message: str
    details: Dict = field(default_factory=dict)
    alerts: List[str] = field(default_factory=list)
    checked_at: str = ""

@dataclass
class ComprehensiveHealth:
    status: str
    timestamp: str
    subsystems: Dict[str, SubsystemHealth] = field(default_factory=dict)
    alerts: List[str] = field(default_factory=list)
    healthy_count: int = 0
    degraded_count: int = 0
    down_count: int = 0
    unknown_count: int = 0

class NotificationManager:
    """
    Notification manager for comprehensive health monitor.
    Now integrates with the central notification_manager.py for preference checking.
    """
    def __init__(self):
        self._state = self._load_state()

    def _load_state(self):
        try:
            if NOTIFICATION_STATE_FILE.exists():
                return json.loads(NOTIFICATION_STATE_FILE.read_text())
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to load notification state: {e}")
        return {}

    def _save_state(self):
        try: NOTIFICATION_STATE_FILE.write_text(json.dumps(self._state, indent=2))
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to save notification state: {e}")

    def _check_central_preferences(self, subsystem: str, is_critical: bool = False) -> bool:
        """Check central notification preferences."""
        try:
            from memory.notification_manager import should_notify, NotificationCategory

            # Map subsystem to category
            if is_critical or subsystem in ["evidence_pipeline", "webhook_sections", "injection_system"]:
                cat = NotificationCategory.CRITICAL
            elif "llm" in subsystem.lower():
                cat = NotificationCategory.LLM_STATUS
            elif "professor" in subsystem.lower():
                cat = NotificationCategory.INFO
            else:
                cat = NotificationCategory.SERVICE_DOWN

            return should_notify(cat)
        except ImportError:
            return True  # Allow if module not available
        except Exception:
            return True

    def can_notify(self, subsystem):
        last = self._state.get(subsystem, {}).get("last_notified", 0)
        return (time.time() - last) >= NOTIFICATION_COOLDOWN_SECONDS

    def send_notification(self, subsystem, title, message, subtitle="", sound="Sosumi", force=False):
        # Check central preferences first
        is_critical = "CRITICAL" in title.upper() or SUBSYSTEMS.get(subsystem, {}).get("critical", False)
        if not force and not self._check_central_preferences(subsystem, is_critical):
            logger.debug(f"Notification blocked by central preferences: {title}")
            return False

        if not force and not self.can_notify(subsystem): return False
        t = title.replace(chr(34),"").replace(chr(39),"")[:100]
        m = message.replace(chr(34),"").replace(chr(39),"")[:200]
        s = subtitle.replace(chr(34),"").replace(chr(39),"")[:100]
        scr = chr(39)+"display notification "+chr(34)+m+chr(34)+" with title "+chr(34)+t+chr(34)+chr(39)
        if s: scr = chr(39)+"display notification "+chr(34)+m+chr(34)+" with title "+chr(34)+t+chr(34)+" subtitle "+chr(34)+s+chr(34)+chr(39)
        scr_inner = scr.strip(chr(39)) + " sound name "+chr(34)+sound+chr(34)
        try:
            subprocess.run(["osascript","-e",scr_inner], capture_output=True, timeout=5)
            self._state[subsystem] = {"last_notified": time.time(), "last_title": t}
            self._save_state()
            return True
        except Exception: return False

    def send_recovery(self, subsystem, display_name):
        # Recovery notifications check central preferences too
        try:
            from memory.notification_manager import should_notify, NotificationCategory
            if not should_notify(NotificationCategory.RECOVERY):
                logger.debug(f"Recovery notification blocked: {display_name}")
                return False
        except Exception:
            pass
        return self.send_notification(subsystem, "Context DNA Recovery", display_name+" is back online", sound="Glass", force=True)

def _now_utc():
    return datetime.now(timezone.utc).isoformat()


_SQL_24H = "datetime('now','-24 hours')"
_SQL_6H = "datetime('now','-6 hours')"


def check_evidence_pipeline():
    name = "evidence_pipeline"
    display = SUBSYSTEMS[name]["display"]
    now = _now_utc()
    try:
        from memory.observability_store import get_observability_store
        store = get_observability_store()
        details = {}
        alerts = []
        try:
            r = store._sqlite_conn.execute("SELECT COUNT(*) FROM outcome_event").fetchone()
            details["total_outcomes"] = r[0] if r else 0
        except Exception as e:
            alerts.append("outcome_event query failed: " + str(e))
        try:
            sql = "SELECT COUNT(*) FROM outcome_event WHERE timestamp_utc > " + _SQL_24H
            r = store._sqlite_conn.execute(sql).fetchone()
            details["recent_outcomes_24h"] = r[0] if r else 0
        except Exception:
            pass
        try:
            r = store._sqlite_conn.execute("SELECT COUNT(*) FROM knowledge_quarantine").fetchone()
            details["quarantine_items"] = r[0] if r else 0
        except Exception:
            pass
        if alerts:
            return SubsystemHealth(name, display, "degraded", "; ".join(alerts),
                                   details, alerts, now)
        total = details.get("total_outcomes", 0)
        recent = details.get("recent_outcomes_24h", 0)
        return SubsystemHealth(name, display, "healthy",
                               str(total) + " outcomes, " + str(recent) + " in 24h",
                               details, [], now)
    except Exception as e:
        return SubsystemHealth(name, display, "down", "Cannot access: " + str(e),
                               {}, [str(e)], now)


def check_webhook_sections():
    name = "webhook_sections"
    display = SUBSYSTEMS[name]["display"]
    now = _now_utc()
    try:
        from memory.injection_health_monitor import get_webhook_monitor
        monitor = get_webhook_monitor()
        health = monitor.check_health()
        sects = health.sections if health.sections else {}
        s_healthy = sum(1 for s in sects.values() if s.status == "healthy")
        s_total = len(sects)
        details = {
            "total_injections_24h": health.total_injections_24h,
            "sections_healthy": s_healthy,
            "sections_total": s_total,
            "eighth_intelligence": health.eighth_intelligence_status,
        }
        if health.status == "healthy":
            msg = (str(s_healthy) + "/" + str(s_total) +
                   " sections OK, 8th=" + health.eighth_intelligence_status)
            return SubsystemHealth(name, display, "healthy", msg, details, [], now)
        elif health.status == "warning":
            msg = health.alerts[0] if health.alerts else "Warning"
            return SubsystemHealth(name, display, "degraded", msg,
                                   details, list(health.alerts), now)
        else:
            msg = health.alerts[0] if health.alerts else "Critical"
            return SubsystemHealth(name, display, "down", msg,
                                   details, list(health.alerts), now)
    except ImportError:
        return SubsystemHealth(name, display, "down",
                               "InjectionHealthMonitor not importable",
                               {}, ["import_error"], now)
    except Exception as e:
        return SubsystemHealth(name, display, "down", str(e), {}, [str(e)], now)


def check_auto_capture():
    name = "auto_capture"
    display = SUBSYSTEMS[name]["display"]
    now = _now_utc()
    try:
        failures_file = MEMORY_DIR / ".auto_capture_failures.json"
        details = {}
        alerts = []
        if failures_file.exists():
            data = json.loads(failures_file.read_text())
            details["failure_count"] = len(data)
            if len(data) > 5:
                alerts.append(str(len(data)) + " capture failures recorded")
        try:
            from memory.auto_capture import scan_for_changes
            details["module_loadable"] = True
        except ImportError:
            details["module_loadable"] = False
            alerts.append("auto_capture module not importable")
        if alerts:
            return SubsystemHealth(name, display, "degraded", "; ".join(alerts),
                                   details, alerts, now)
        return SubsystemHealth(name, display, "healthy", "Auto-capture operational",
                               details, [], now)
    except Exception as e:
        return SubsystemHealth(name, display, "down", str(e), {}, [str(e)], now)


def check_professor():
    name = "professor"
    display = SUBSYSTEMS[name]["display"]
    now = _now_utc()
    try:
        from memory.persistent_hook_structure import get_professor_wisdom
        result = get_professor_wisdom("health check ping", depth="minimal")
        if result and len(str(result)) > 10:
            rlen = len(str(result))
            return SubsystemHealth(name, display, "healthy",
                                   "Professor responded (" + str(rlen) + " chars)",
                                   {"response_length": rlen}, [], now)
        else:
            rlen = len(str(result)) if result else 0
            return SubsystemHealth(name, display, "degraded",
                                   "Professor returned empty/short response",
                                   {"response_length": rlen},
                                   ["empty_response"], now)
    except ImportError:
        return SubsystemHealth(name, display, "degraded",
                               "Professor module not importable",
                               {}, ["import_error"], now)
    except Exception as e:
        return SubsystemHealth(name, display, "degraded",
                               "Professor error: " + str(e), {}, [str(e)], now)


def check_llm_endpoint():
    name = "llm_endpoint"
    display = SUBSYSTEMS[name]["display"]
    now = _now_utc()
    try:
        # ALL LLM access routes through priority queue — NO direct HTTP to port 5044
        from memory.llm_priority_queue import check_llm_health
        healthy = check_llm_health()
        if healthy:
            return SubsystemHealth(name, display, "healthy",
                                   "LLM endpoint active (via Redis health cache)",
                                   {}, [], now)
        else:
            return SubsystemHealth(name, display, "down",
                                   "LLM endpoint unhealthy (via Redis health cache)",
                                   {}, ["llm_health_false"], now)
    except Exception as e:
        return SubsystemHealth(name, display, "down",
                               "LLM check failed: " + str(e), {}, [str(e)], now)


def check_learning_queries():
    name = "learning_queries"
    display = SUBSYSTEMS[name]["display"]
    now = _now_utc()
    try:
        from memory.observability_store import get_observability_store
        store = get_observability_store()
        details = {}
        alerts = []
        try:
            r = store._sqlite_conn.execute(
                "SELECT COUNT(*) FROM injection_section WHERE section_id = 1"
            ).fetchone()
            details["section1_records"] = r[0] if r else 0
        except Exception as e:
            alerts.append("section1 query: " + str(e))
        try:
            sql = "SELECT COUNT(*) FROM injection_event WHERE timestamp_utc > " + _SQL_24H
            r = store._sqlite_conn.execute(sql).fetchone()
            details["injections_24h"] = r[0] if r else 0
        except Exception:
            pass
        if alerts:
            return SubsystemHealth(name, display, "degraded", "; ".join(alerts),
                                   details, alerts, now)
        s1 = details.get("section1_records", 0)
        return SubsystemHealth(name, display, "healthy",
                               "Section 1 has " + str(s1) + " records",
                               details, [], now)
    except Exception as e:
        return SubsystemHealth(name, display, "down", str(e), {}, [str(e)], now)


def check_injection_system():
    name = "injection_system"
    display = SUBSYSTEMS[name]["display"]
    now = _now_utc()
    try:
        latest_file = MEMORY_DIR / ".injection_latest.tmp"
        details = {}
        alerts = []
        if latest_file.exists():
            stat = latest_file.stat()
            age_seconds = time.time() - stat.st_mtime
            details["last_injection_age_s"] = round(age_seconds)
            details["last_injection_size"] = stat.st_size
            if age_seconds > 7200:
                hrs = age_seconds / 3600
                alerts.append("Last injection " + str(round(hrs, 1)) + "h ago (stale)")
        else:
            alerts.append("No injection_latest.tmp found")
        try:
            from memory.observability_store import get_observability_store
            store = get_observability_store()
            sql = "SELECT COUNT(*) FROM injection_event WHERE timestamp_utc > " + _SQL_6H
            r = store._sqlite_conn.execute(sql).fetchone()
            details["injections_6h"] = r[0] if r else 0
            if details["injections_6h"] == 0:
                alerts.append("No injections in last 6 hours")
        except Exception as e:
            alerts.append("injection_event query: " + str(e))
        if alerts:
            return SubsystemHealth(name, display, "degraded", "; ".join(alerts),
                                   details, alerts, now)
        inj6 = details.get("injections_6h", "?")
        return SubsystemHealth(name, display, "healthy",
                               "Injection active, " + str(inj6) + " in 6h",
                               details, [], now)
    except Exception as e:
        return SubsystemHealth(name, display, "down", str(e), {}, [str(e)], now)


def check_observability_store():
    name = "observability_store"
    display = SUBSYSTEMS[name]["display"]
    now = _now_utc()
    try:
        from memory.observability_store import get_observability_store
        store = get_observability_store()
        details = {}
        alerts = []
        tables_to_check = ["outcome_event", "injection_event", "injection_section",
                           "knowledge_quarantine", "claim", "job_schedule"]
        for table in tables_to_check:
            try:
                r = store._sqlite_conn.execute(
                    "SELECT COUNT(*) FROM " + table
                ).fetchone()
                details[table + "_count"] = r[0] if r else 0
            except Exception as e:
                alerts.append("Table " + table + ": " + str(e))
        db_path = MEMORY_DIR / ".observability_store.db"
        if db_path.exists():
            details["db_size_mb"] = round(db_path.stat().st_size / (1024 * 1024), 2)
        if alerts:
            return SubsystemHealth(name, display, "degraded", "; ".join(alerts),
                                   details, alerts, now)
        return SubsystemHealth(name, display, "healthy",
                               "All " + str(len(tables_to_check)) + " tables accessible",
                               details, [], now)
    except Exception as e:
        return SubsystemHealth(name, display, "down", "Store unavailable: " + str(e),
                               {}, [str(e)], now)


def check_service_heartbeats():
    name = "service_heartbeats"
    display = SUBSYSTEMS[name]["display"]
    now = _now_utc()
    try:
        details = {}
        alerts = []
        watchdog_hb = MEMORY_DIR / ".watchdog_heartbeat.json"
        if watchdog_hb.exists():
            try:
                data = json.loads(watchdog_hb.read_text())
                age = time.time() - data.get("timestamp", 0)
                details["watchdog_age_s"] = round(age)
                if age > 600:
                    alerts.append("Watchdog heartbeat stale (" + str(round(age / 60)) + "m)")
            except Exception:
                alerts.append("Watchdog heartbeat unreadable")
        else:
            details["watchdog_present"] = False
        agent_hb = MEMORY_DIR / ".agent_service_heartbeat.json"
        if agent_hb.exists():
            try:
                data = json.loads(agent_hb.read_text())
                age = time.time() - data.get("timestamp", 0)
                details["agent_service_age_s"] = round(age)
                if age > 600:
                    alerts.append("Agent service heartbeat stale (" + str(round(age / 60)) + "m)")
            except Exception:
                alerts.append("Agent service heartbeat unreadable")
        else:
            details["agent_service_present"] = False
        hb_config = MEMORY_DIR / ".heartbeat_config.json"
        if hb_config.exists():
            try:
                cfg = json.loads(hb_config.read_text())
                details["heartbeat_interval"] = cfg.get("interval_seconds", "?")
            except Exception:
                pass
        if alerts:
            return SubsystemHealth(name, display, "degraded", "; ".join(alerts),
                                   details, alerts, now)
        return SubsystemHealth(name, display, "healthy", "Heartbeats current",
                               details, [], now)
    except Exception as e:
        return SubsystemHealth(name, display, "down", str(e), {}, [str(e)], now)


# ---------------------------------------------------------------
# ORCHESTRATION
# ---------------------------------------------------------------

def check_process_integrity():
    """
    Check for architecture failures discovered 2026-02-14:
    1. Wrong Python version (Xcode 3.9 vs 3.14)
    2. Scheduler CPU runaway (>50% sustained)
    3. Duplicate agent_service instances
    4. PG corrupt indexes (contextdna-api crash loop)
    """
    name = "process_integrity"
    display = SUBSYSTEMS[name]["display"]
    now = _now_utc()
    alerts = []
    details = {}

    try:
        import subprocess

        # 1. Check for wrong Python (Xcode 3.9) running our scripts
        try:
            result = subprocess.run(
                ["pgrep", "-fl", "Xcode.*Python.*memory/"],
                capture_output=True, text=True, timeout=5
            )
            wrong_python_count = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
            details["wrong_python_procs"] = wrong_python_count
            if wrong_python_count > 0:
                alerts.append(f"{wrong_python_count} processes using Xcode Python 3.9 (should be 3.14)")
        except Exception:
            pass

        # 2. Check scheduler CPU
        try:
            result = subprocess.run(
                ["pgrep", "-f", "scheduler_coordinator"],
                capture_output=True, text=True, timeout=3
            )
            if result.stdout.strip():
                pid = result.stdout.strip().split("\n")[0]
                ps_result = subprocess.run(
                    ["ps", "-o", "pcpu=", "-p", pid],
                    capture_output=True, text=True, timeout=3
                )
                cpu = float(ps_result.stdout.strip()) if ps_result.stdout.strip() else 0
                details["scheduler_cpu"] = cpu
                if cpu > 50:
                    alerts.append(f"Scheduler CPU at {cpu:.0f}% (stampede likely)")
        except Exception:
            pass

        # 3. Check for duplicate agent_service
        try:
            result = subprocess.run(
                ["pgrep", "-f", "uvicorn.*agent_service"],
                capture_output=True, text=True, timeout=3
            )
            agent_count = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
            details["agent_service_count"] = agent_count
            if agent_count > 1:
                alerts.append(f"{agent_count} agent_service instances (should be 1)")
            elif agent_count == 0:
                alerts.append("agent_service not running")
        except Exception:
            pass

        # 4. Check contextdna-api health (PG corruption indicator)
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}:{{.RestartCount}}",
                 "contextdna-api"],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                status, restarts = result.stdout.strip().split(":")
                details["contextdna_api_status"] = status
                details["contextdna_api_restarts"] = int(restarts)
                if status == "restarting" or int(restarts) > 5:
                    alerts.append(f"contextdna-api {status} (restarts={restarts}, PG corruption likely)")
        except Exception:
            pass

        if alerts:
            return SubsystemHealth(name, display, "degraded",
                                   "; ".join(alerts), details, alerts, now)
        return SubsystemHealth(name, display, "healthy",
                               "All processes correct", details, [], now)
    except Exception as e:
        return SubsystemHealth(name, display, "down", str(e), {}, [str(e)], now)


CHECK_FUNCTIONS = {
    "evidence_pipeline": check_evidence_pipeline,
    "webhook_sections": check_webhook_sections,
    "auto_capture": check_auto_capture,
    "professor": check_professor,
    "llm_endpoint": check_llm_endpoint,
    "learning_queries": check_learning_queries,
    "injection_system": check_injection_system,
    "observability_store": check_observability_store,
    "service_heartbeats": check_service_heartbeats,
    "process_integrity": check_process_integrity,
}


def _load_previous_health():
    try:
        if HEALTH_STATE_FILE.exists():
            return json.loads(HEALTH_STATE_FILE.read_text())
    except Exception:
        pass
    return None


def _save_health_state(health):
    try:
        data = {
            "status": health.status,
            "timestamp": health.timestamp,
            "healthy_count": health.healthy_count,
            "degraded_count": health.degraded_count,
            "down_count": health.down_count,
            "alerts": health.alerts,
            "subsystems": {},
        }
        for k, v in health.subsystems.items():
            data["subsystems"][k] = {
                "name": v.name,
                "display_name": v.display_name,
                "status": v.status,
                "message": v.message,
                "alerts": v.alerts,
                "checked_at": v.checked_at,
            }
        HEALTH_STATE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning("Failed to save health state: " + str(e))


def run_all_checks():
    now = _now_utc()
    health = ComprehensiveHealth(status="healthy", timestamp=now)
    for name, check_fn in CHECK_FUNCTIONS.items():
        try:
            sub = check_fn()
        except Exception as e:
            sub = SubsystemHealth(name, SUBSYSTEMS[name]["display"],
                                  "down", "Check crashed: " + str(e),
                                  {}, [str(e)], now)
        health.subsystems[name] = sub
        if sub.status == "healthy":
            health.healthy_count += 1
        elif sub.status == "degraded":
            health.degraded_count += 1
        elif sub.status == "down":
            health.down_count += 1
        else:
            health.unknown_count += 1
        for alert in sub.alerts:
            health.alerts.append("[" + sub.display_name + "] " + alert)
    if health.down_count > 0:
        critical_down = any(
            health.subsystems[n].status == "down"
            for n in health.subsystems
            if SUBSYSTEMS.get(n, {}).get("critical", False)
        )
        health.status = "critical" if critical_down else "degraded"
    elif health.degraded_count > 0:
        health.status = "degraded"
    else:
        health.status = "healthy"
    return health


def process_notifications(health, notifier):
    """Send macOS notifications for failures and recoveries."""
    previous = _load_previous_health()
    prev_subsystems = previous.get("subsystems", {}) if previous else {}
    for name, sub in health.subsystems.items():
        prev_status = prev_subsystems.get(name, {}).get("status", "unknown")
        is_critical = SUBSYSTEMS.get(name, {}).get("critical", False)
        # FAILURE notification
        if sub.status in ("down", "degraded") and prev_status not in ("down", "degraded"):
            sound = "Sosumi" if is_critical else "Purr"
            if sub.status == "down" and is_critical:
                severity = "CRITICAL"
            else:
                severity = "WARNING"
            notifier.send_notification(
                name,
                "Context DNA " + severity,
                sub.message[:200],
                subtitle=sub.display_name,
                sound=sound,
            )
            logger.warning("NOTIFICATION [" + severity + "] " +
                           sub.display_name + ": " + sub.message)
        # RECOVERY notification
        elif sub.status == "healthy" and prev_status in ("down", "degraded"):
            notifier.send_recovery(name, sub.display_name)
            logger.info("RECOVERY " + sub.display_name + ": back online")


def run_comprehensive_health_check():
    """Entry point for lite_scheduler and watchdog. Returns (success, message) tuple."""
    try:
        health = run_all_checks()
        notifier = NotificationManager()
        process_notifications(health, notifier)
        _save_health_state(health)
        msg = ("H:" + str(health.healthy_count) + " D:" + str(health.degraded_count) +
               " X:" + str(health.down_count) + " => " + health.status)
        logger.info("Comprehensive health: " + msg)
        return (health.status != "critical", msg)
    except Exception as e:
        logger.error("Comprehensive health check failed: " + str(e))
        return (False, "Check crashed: " + str(e))


# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------

def _print_status():
    """Print current health state from saved file."""
    data = _load_previous_health()
    if not data:
        print("No health state found. Run check first.")
        return
    status = data.get("status", "unknown")
    status_icon = {"healthy": "OK", "degraded": "WARN", "critical": "CRIT"}.get(status, "??")
    print("")
    print("Comprehensive Health: [" + status_icon + "] " + status.upper())
    print("Timestamp: " + str(data.get("timestamp", "?")))
    print("Healthy: " + str(data.get("healthy_count", 0)) + " | " +
          "Degraded: " + str(data.get("degraded_count", 0)) + " | " +
          "Down: " + str(data.get("down_count", 0)))
    subs = data.get("subsystems", {})
    if subs:
        print("")
        print("Subsystems:")
        for name, info in subs.items():
            s = info.get("status", "?")
            icon = {"healthy": " OK ", "degraded": "WARN", "down": "DOWN"}.get(s, " ?? ")
            critical = " [CRITICAL]" if SUBSYSTEMS.get(name, {}).get("critical") else ""
            dname = info.get("display_name", name)
            msg = info.get("message", "")
            print("  [" + icon + "] " + dname + critical + ": " + msg)
    alerts = data.get("alerts", [])
    if alerts:
        print("")
        print("Alerts (" + str(len(alerts)) + "):")
        for a in alerts:
            print("  - " + a)


def main():
    if len(sys.argv) < 2:
        print("Usage: comprehensive_health_monitor <check|status|json|test-notify>")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd=="check":
        s,m=run_comprehensive_health_check()
        print(("PASS" if s else "FAIL")+" - "+m)
        _print_status()
    elif cmd=="status":
        _print_status()
    elif cmd=="json":
        data=_load_previous_health()
        print(json.dumps(data,indent=2) if data else "{}")
    elif cmd=="test-notify":
        nm=NotificationManager()
        sent=nm.send_notification("test","Health Test","Test notification",subtitle="Test",sound="Glass",force=True)
        print("Notification sent: "+str(sent))
    else:
        print("Unknown command: "+cmd)
        sys.exit(1)


if __name__=="__main__":
    main()
