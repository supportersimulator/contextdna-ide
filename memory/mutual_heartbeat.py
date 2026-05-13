#!/usr/bin/env python3
"""
MUTUAL HEARTBEAT SYSTEM - "Who Watches the Watchdog?"

Implements mutual monitoring between:
- agent_service.py (the main brain)
- synaptic_watchdog_daemon.py (the health monitor)

Each service writes heartbeats and monitors the other.
If either goes down → macOS system notification with debug info.

ENHANCED: Docker/Container Awareness
- Detects lite vs heavy mode automatically
- Monitors Docker daemon status
- Tracks individual container health
- Notifies when mode transitions or containers fail

Architecture:
    ┌─────────────────┐         ┌─────────────────┐
    │  agent_service  │◄───────►│    watchdog     │
    │  (writes pulse) │ monitor │  (writes pulse) │
    └────────┬────────┘         └────────┬────────┘
             │                           │
             ▼                           ▼
    .agent_heartbeat.json      .watchdog_heartbeat.json
             │                           │
             └─────────┬─────────────────┘
                       ▼
              macOS Notification
              (if either stale OR Docker/containers down)

Usage:
    from memory.mutual_heartbeat import MutualHeartbeat

    # In agent_service.py
    heartbeat = MutualHeartbeat("agent_service")
    await heartbeat.start()  # Starts writing + monitoring

    # In synaptic_watchdog_daemon.py
    heartbeat = MutualHeartbeat("watchdog")
    heartbeat.start_sync()  # Synchronous version for daemon
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
import logging

logger = logging.getLogger(__name__)

# Observability store - delegate to canonical singleton
def get_observability_store():
    """Get the singleton observability store (delegates to canonical factory)."""
    try:
        from memory.observability_store import get_observability_store as _canonical
        return _canonical()
    except Exception as e:
        logger.warning(f"Failed to get observability store: {e}")
        return None

# Configuration
HEARTBEAT_DIR = Path(__file__).parent
HEARTBEAT_INTERVAL = 5  # Write heartbeat every 5 seconds
STALE_THRESHOLD = 15    # Consider dead if no heartbeat for 15 seconds
CHECK_INTERVAL = 10     # Check other service every 10 seconds

# Service names
SERVICE_AGENT = "agent_service"
SERVICE_WATCHDOG = "watchdog"


@dataclass
class Heartbeat:
    """Heartbeat data structure."""
    service: str
    pid: int
    timestamp: str
    uptime_seconds: float
    status: str
    debug_info: Dict[str, Any]


@dataclass
class DockerStatus:
    """Docker and container status for lite/heavy mode awareness."""
    docker_running: bool
    mode: str  # "lite" or "heavy"
    containers: Dict[str, str]  # container_name -> status
    healthy_count: int
    unhealthy_count: int
    postgres_available: bool
    redis_available: bool
    blocking_issues: List[str]  # What's preventing heavy mode


@dataclass
class InfraStatus:
    """Full infrastructure status including all services for both modes."""
    # Docker/Container status
    docker: DockerStatus

    # Local servers (both modes)
    llm_server_running: bool
    llm_server_port: int
    llm_server_model: str
    voice_server_running: bool
    agent_service_running: bool

    # Heavy mode services
    redis_running: bool
    rabbitmq_running: bool
    celery_workers_running: bool
    celery_worker_count: int
    celery_beat_running: bool
    seaweedfs_running: bool
    ollama_running: bool

    # Status summary
    all_services_healthy: bool
    heavy_mode_ready: bool      # All heavy services available
    lite_mode_ready: bool       # All lite services available
    recovery_recommendation: str  # "stay_lite", "try_heavy", "manual_required"
    missing_services: List[str]   # Services that are down


# Recovery mode preferences
class RecoveryMode:
    """Recovery behavior settings."""
    AGGRESSIVE = "aggressive"   # Auto-recover everything, always try heavy mode
    CONSERVATIVE = "conservative"  # Only restart essential services, prefer lite mode
    MANUAL = "manual"           # Only notify, never auto-restart

    # Current setting - can be changed at runtime
    current = AGGRESSIVE


# =============================================================================
# INTENDED MODE SYSTEM - Smart Recovery to User's Preferred Configuration
# =============================================================================

CONFIG_FILE = HEARTBEAT_DIR / ".heartbeat_config.json"


def load_config() -> dict:
    """Load persistent heartbeat configuration."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError as e:
            print(f"[WARN] Heartbeat config JSON parse failed: {e}")
    return {
        "intended_mode": "heavy",           # What user wants to be running
        "accepted_fallback": False,         # User accepted lite mode as OK
        "recovery_mode": RecoveryMode.AGGRESSIVE,
        "max_recovery_attempts": 5,
        "recovery_attempt_count": 0,
        "last_recovery_attempt": None,
    }


def save_config(config: dict):
    """Save persistent configuration."""
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def get_intended_mode() -> str:
    """Get the user's intended/preferred mode."""
    return load_config().get("intended_mode", "heavy")


def set_intended_mode(mode: str):
    """Set the user's intended mode and reset recovery state."""
    config = load_config()
    config["intended_mode"] = mode
    config["accepted_fallback"] = False
    config["recovery_attempt_count"] = 0
    save_config(config)

    send_macos_notification(
        title=f"Context DNA: Target Mode Set",
        message=f"System will maintain: {mode.upper()} mode",
        subtitle="Recovery will restore this mode",
        sound="Pop"
    )


def accept_current_mode():
    """Accept the current fallback mode - stop trying to recover to intended."""
    config = load_config()
    config["accepted_fallback"] = True
    config["recovery_attempt_count"] = 0
    save_config(config)

    docker = check_docker_status()
    send_macos_notification(
        title=f"Context DNA: Accepted {docker.mode.upper()} Mode",
        message="Recovery paused - running in current mode",
        subtitle="Use 'mode heavy' to resume recovery",
        sound="Pop"
    )


def should_attempt_recovery() -> tuple[bool, str]:
    """
    Determine if we should attempt recovery based on intended mode.

    Returns:
        (should_recover, reason)
    """
    config = load_config()
    docker = check_docker_status()

    # Already in intended mode
    if docker.mode == config["intended_mode"]:
        return False, "already in intended mode"

    # User accepted fallback
    if config["accepted_fallback"]:
        return False, "user accepted fallback"

    # Recovery mode is manual
    if config["recovery_mode"] == RecoveryMode.MANUAL:
        return False, "manual mode - no auto-recovery"

    # Max attempts reached
    if config["recovery_attempt_count"] >= config["max_recovery_attempts"]:
        return False, f"max attempts ({config['max_recovery_attempts']}) reached"

    return True, f"recovering to {config['intended_mode']}"


def record_recovery_attempt(success: bool):
    """Record a recovery attempt."""
    config = load_config()
    config["recovery_attempt_count"] = config.get("recovery_attempt_count", 0) + 1
    config["last_recovery_attempt"] = datetime.now().isoformat()
    save_config(config)

    # If max attempts reached, notify user with option to accept
    if config["recovery_attempt_count"] >= config["max_recovery_attempts"]:
        docker = check_docker_status()
        send_macos_notification(
            title="Context DNA: Recovery Paused",
            message=f"Max attempts reached. Currently in {docker.mode.upper()} mode.",
            subtitle="Run: python mutual_heartbeat.py accept",
            sound="Sosumi"
        )


def reset_recovery_attempts():
    """Reset recovery attempt counter (called on successful recovery)."""
    config = load_config()
    config["recovery_attempt_count"] = 0
    config["accepted_fallback"] = False
    save_config(config)


# =============================================================================
# SERVICE HEALTH CHECKS
# =============================================================================

def check_redis(host: str = "127.0.0.1", port: int = 6379) -> bool:
    """Check if Redis is responding."""
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def check_rabbitmq(host: str = "127.0.0.1", port: int = 15672) -> bool:
    """Check if RabbitMQ is responding (management port)."""
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def check_celery_workers() -> tuple[bool, int]:
    """
    Check if Celery workers are running.

    Returns:
        (any_running, worker_count)
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "celery.*worker"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            pids = [p for p in result.stdout.strip().split('\n') if p]
            return True, len(pids)
        return False, 0
    except Exception:
        return False, 0


def check_celery_beat() -> bool:
    """Check if Celery beat scheduler is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "celery.*beat"],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def check_seaweedfs(port: int = 9333) -> bool:
    """Check if SeaweedFS master is responding."""
    try:
        import urllib.request
        req = urllib.request.Request(f"http://localhost:{port}/cluster/status", method='GET')
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_ollama(port: int = 11434) -> bool:
    """Check if Ollama is responding."""
    try:
        import urllib.request
        req = urllib.request.Request(f"http://localhost:{port}/api/tags", method='GET')
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_llm_server(port: int = 8000) -> tuple[bool, str]:
    """
    Check if the local LLM server (mlx_lm.server) is running.

    Returns:
        (is_running, model_name or error)
    """
    try:
        import urllib.request
        import urllib.error

        # Try common LLM server health endpoints
        for endpoint in [f"http://127.0.0.1:{port}/health",
                        f"http://127.0.0.1:{port}/v1/models",
                        f"http://127.0.0.1:{port}/"]:
            try:
                req = urllib.request.Request(endpoint, method='GET')
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        data = resp.read().decode()
                        # Try to extract model name
                        if "model" in data.lower():
                            return True, data[:100]
                        return True, "running"
            except urllib.error.URLError:
                continue
            except Exception:
                continue

        return False, "not responding"
    except Exception as e:
        return False, str(e)


def check_voice_server(port: int = 8888) -> bool:
    """Check if the voice/synaptic chat server is running."""
    try:
        import urllib.request
        req = urllib.request.Request(f"http://localhost:{port}/health", method='GET')
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_agent_service(port: int = 8080) -> bool:
    """Check if agent_service is responding."""
    try:
        import urllib.request
        req = urllib.request.Request(f"http://localhost:{port}/health", method='GET')
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_infra_status() -> InfraStatus:
    """Get full infrastructure status for all services in both modes."""
    docker = check_docker_status()

    # Local servers (both modes)
    llm_running, llm_info = check_llm_server()
    voice_running = check_voice_server()
    agent_running = check_agent_service()

    # Heavy mode services
    redis_running = check_redis()
    rabbitmq_running = check_rabbitmq()
    celery_running, celery_count = check_celery_workers()
    beat_running = check_celery_beat()
    seaweedfs_running = check_seaweedfs()
    ollama_running = check_ollama()

    # Track missing services
    missing = []

    # Core services (both modes)
    if not agent_running:
        missing.append("agent_service")
    if not llm_running:
        missing.append("llm_server")

    # Heavy mode services
    config = load_config()
    if config["intended_mode"] == "heavy":
        if not docker.postgres_available:
            missing.append("postgresql")
        if not redis_running:
            missing.append("redis")
        if not celery_running:
            missing.append("celery_workers")
        if not beat_running:
            missing.append("celery_beat")

    # Determine mode readiness
    heavy_mode_ready = (
        docker.docker_running and
        docker.postgres_available and
        redis_running and
        celery_running and
        beat_running
    )

    lite_mode_ready = agent_running  # SQLite is always available

    # Determine recovery recommendation
    intended = config["intended_mode"]
    if intended == "heavy":
        if heavy_mode_ready:
            recommendation = "stay_heavy"
        elif docker.docker_running:
            recommendation = "try_heavy"  # Docker up, just need services
        else:
            recommendation = "start_docker"  # Need to start Docker first
    else:  # intended == "lite"
        if lite_mode_ready:
            recommendation = "stay_lite"
        else:
            recommendation = "restart_agent"

    # Manual mode override
    if RecoveryMode.current == RecoveryMode.MANUAL:
        recommendation = "manual_required"

    # All services healthy check
    all_healthy = (
        agent_running and
        llm_running and
        (not config["intended_mode"] == "heavy" or heavy_mode_ready)
    )

    return InfraStatus(
        docker=docker,
        llm_server_running=llm_running,
        llm_server_port=8000,
        llm_server_model=llm_info if llm_running else "",
        voice_server_running=voice_running,
        agent_service_running=agent_running,
        redis_running=redis_running,
        rabbitmq_running=rabbitmq_running,
        celery_workers_running=celery_running,
        celery_worker_count=celery_count,
        celery_beat_running=beat_running,
        seaweedfs_running=seaweedfs_running,
        ollama_running=ollama_running,
        all_services_healthy=all_healthy,
        heavy_mode_ready=heavy_mode_ready,
        lite_mode_ready=lite_mode_ready,
        recovery_recommendation=recommendation,
        missing_services=missing
    )


# Import existing Docker MCP if available
try:
    from memory.docker_mcp import get_health_snapshot, HealthSnapshot
    DOCKER_MCP_AVAILABLE = True
except ImportError:
    DOCKER_MCP_AVAILABLE = False
    get_health_snapshot = None
    HealthSnapshot = None


def check_docker_status() -> DockerStatus:
    """
    Check Docker daemon and container health.
    Uses docker_mcp.py if available for comprehensive monitoring.
    Returns status for lite/heavy mode awareness.
    """
    containers = {}
    blocking_issues = []
    docker_running = False
    postgres_available = False
    redis_available = False
    healthy_count = 0
    unhealthy_count = 0

    # Use existing DockerMCP for comprehensive monitoring
    if DOCKER_MCP_AVAILABLE:
        try:
            snapshot = get_health_snapshot(project="contextdna")
            docker_running = snapshot.total_containers > 0 or snapshot.running > 0

            # Extract container info
            for c in snapshot.containers:
                containers[c.name] = c.state
                if 'postgres' in c.name.lower():
                    postgres_available = c.is_running()
                if 'redis' in c.name.lower():
                    redis_available = c.is_running()

            healthy_count = snapshot.healthy
            unhealthy_count = snapshot.unhealthy

            # Use alerts from snapshot
            blocking_issues = snapshot.alerts.copy()

            # Add blocking issues if key services missing
            if not postgres_available:
                blocking_issues.append("PostgreSQL not running - heavy mode unavailable")
            if not redis_available:
                blocking_issues.append("Redis not running - caching disabled")

        except Exception as e:
            blocking_issues.append(f"Docker check failed: {e}")
            docker_running = False
    else:
        # Fallback: Basic Docker check
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5
            )
            docker_running = result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            docker_running = False
            blocking_issues.append("Docker daemon not running")

        if docker_running:
            # Get container status
            try:
                result = subprocess.run(
                    ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.State}}"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n'):
                        if line:
                            parts = line.split('\t')
                            if len(parts) >= 3:
                                name, status, state = parts[0], parts[1], parts[2]
                                containers[name] = state

                                if 'postgres' in name.lower():
                                    postgres_available = state == 'running'
                                    if not postgres_available:
                                        blocking_issues.append(f"PostgreSQL not running: {status}")
                                if 'redis' in name.lower():
                                    redis_available = state == 'running'
                                    if not redis_available:
                                        blocking_issues.append(f"Redis not running: {status}")

                healthy_count = sum(1 for s in containers.values() if s == 'running')
                unhealthy_count = len(containers) - healthy_count
            except Exception as e:
                blocking_issues.append(f"Failed to list containers: {e}")

    # Determine mode
    mode = "heavy" if (docker_running and postgres_available) else "lite"

    return DockerStatus(
        docker_running=docker_running,
        mode=mode,
        containers=containers,
        healthy_count=healthy_count,
        unhealthy_count=unhealthy_count,
        postgres_available=postgres_available,
        redis_available=redis_available,
        blocking_issues=blocking_issues
    )


def get_heartbeat_path(service: str) -> Path:
    """Get path to heartbeat file for a service."""
    return HEARTBEAT_DIR / f".{service}_heartbeat.json"


def send_macos_notification(title: str, message: str, subtitle: str = None, sound: str = "Basso", category: str = None):
    """
    Send macOS system notification using osascript.

    NOW WITH PREFERENCE CHECKING - checks notification_manager before sending.

    Args:
        title: Notification title
        message: Main message body
        subtitle: Optional subtitle
        sound: Sound name (Basso, Blow, Bottle, Frog, Funk, Glass, Hero, etc.)
        category: Notification category for filtering (llm_status, voice_status, etc.)
    """
    # Check notification preferences before sending
    try:
        from memory.notification_manager import should_notify, NotificationCategory, record_notification_sent

        # Auto-detect category from title/message if not provided
        if category is None:
            title_lower = title.lower()
            message_lower = message.lower()

            if "critical" in title_lower or "critical" in message_lower:
                category = "critical"
            elif "llm" in title_lower and "down" in title_lower:
                category = "llm_status"
            elif "llm" in title_lower and "recover" in title_lower:
                category = "llm_recovered"
            elif "voice" in title_lower and "down" in title_lower:
                category = "voice_status"
            elif "voice" in title_lower and "recover" in title_lower:
                category = "voice_recovered"
            elif "scheduler" in title_lower and "down" in title_lower:
                category = "scheduler_status"
            elif "scheduler" in title_lower and "recover" in title_lower:
                category = "scheduler_status"
            elif "auto-recovery" in title_lower:
                category = "auto_recovery_success"
            elif "mode" in title_lower and ("set" in title_lower or "accepted" in title_lower):
                category = "mode_change"
            elif "recovery" in title_lower:
                category = "recovery"
            elif "down" in title_lower or "down" in message_lower:
                category = "service_down"
            else:
                category = "info"

        # Convert string to enum
        try:
            cat_enum = NotificationCategory(category)
        except ValueError:
            cat_enum = NotificationCategory.UNKNOWN

        # Check if we should send
        if not should_notify(cat_enum):
            logger.debug(f"Notification blocked by preferences: {title} [{category}]")
            return

    except ImportError:
        # notification_manager not available, send anyway
        pass
    except Exception as e:
        logger.debug(f"Preference check failed, sending anyway: {e}")

    # Build and send the notification
    script_parts = [f'display notification "{message}" with title "{title}"']
    if subtitle:
        script_parts[0] = f'display notification "{message}" with title "{title}" subtitle "{subtitle}"'
    script_parts[0] += f' sound name "{sound}"'

    script = script_parts[0]

    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5
        )
        logger.info(f"Sent notification: {title}")

        # Record for cooldown tracking
        try:
            from memory.notification_manager import record_notification_sent
            record_notification_sent(category or "unknown")
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Failed to send notification: {e}")


def send_critical_alert(service_down: str, last_seen: str, debug_info: dict):
    """
    Send critical alert when a service goes down.
    Includes debug information for troubleshooting.
    """
    title = f"Context DNA Alert"
    subtitle = f"{service_down} is DOWN"

    # Format debug info
    debug_str = "\n".join(f"{k}: {v}" for k, v in debug_info.items()) if debug_info else "No debug info"
    message = f"Last seen: {last_seen}\n\nDebug:\n{debug_str}"

    # Send notification
    send_macos_notification(
        title=title,
        message=f"Last seen: {last_seen}",
        subtitle=subtitle,
        sound="Basso"  # Alert sound
    )

    # Also log to file for debugging
    alert_log = HEARTBEAT_DIR / ".service_alerts.log"
    with open(alert_log, "a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"ALERT: {service_down} DOWN at {datetime.now().isoformat()}\n")
        f.write(f"Last seen: {last_seen}\n")
        f.write(f"Debug info:\n{debug_str}\n")


def send_recovery_notification(service: str):
    """Send notification when a service recovers."""
    send_macos_notification(
        title="Context DNA Recovery",
        message=f"{service} is back online",
        subtitle="Service restored",
        sound="Glass"  # Positive sound
    )


# =============================================================================
# AUTO-RECOVERY FUNCTIONS
# =============================================================================

def restart_service(service: str) -> tuple[bool, str]:
    """
    Attempt to restart a Context DNA service.

    Returns:
        (success, message) tuple
    """
    repo_root = Path(__file__).parent.parent

    # Ensure logs directory exists
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(exist_ok=True)

    if service == SERVICE_AGENT:
        # Restart agent_service via uvicorn
        try:
            # Kill existing if running
            subprocess.run(
                ["pkill", "-f", "uvicorn.*agent_service"],
                capture_output=True,
                timeout=5
            )
            time.sleep(1)

            # Start new instance
            subprocess.Popen(
                [
                    str(repo_root / ".venv" / "bin" / "python"), "-m", "uvicorn",
                    "memory.agent_service:app",
                    "--host", "0.0.0.0",
                    "--port", "8080"
                ],
                cwd=str(repo_root),
                stdout=open(repo_root / "logs" / "agent_service.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            return True, "agent_service restarted"
        except Exception as e:
            return False, f"Failed to restart agent_service: {e}"

    elif service == SERVICE_WATCHDOG:
        # Restart watchdog daemon
        try:
            # Kill existing if running
            subprocess.run(
                ["pkill", "-f", "synaptic_watchdog_daemon"],
                capture_output=True,
                timeout=5
            )
            time.sleep(1)

            # Start new instance
            subprocess.Popen(
                [
                    str(repo_root / ".venv" / "bin" / "python"),
                    str(repo_root / "memory" / "synaptic_watchdog_daemon.py")
                ],
                cwd=str(repo_root),
                stdout=open(repo_root / "logs" / "watchdog.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            return True, "watchdog restarted"
        except Exception as e:
            return False, f"Failed to restart watchdog: {e}"

    return False, f"Unknown service: {service}"


# =============================================================================
# HEAVY MODE SERVICE RESTARTS
# =============================================================================

def restart_celery_worker(queues: List[str] = None) -> tuple[bool, str]:
    """
    Restart Celery workers.

    Args:
        queues: Specific queues to start workers for, or None for all

    Returns:
        (success, message) tuple
    """
    repo_root = Path(__file__).parent.parent
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(exist_ok=True)

    try:
        # Kill existing workers
        subprocess.run(
            ["pkill", "-f", "celery.*worker"],
            capture_output=True,
            timeout=10
        )
        time.sleep(2)

        # Default queues from celery_config.py
        if queues is None:
            queues = ["scanner", "distiller", "relevance", "llm", "brain", "celery"]

        # Start workers for each queue
        for queue in queues:
            subprocess.Popen(
                [
                    str(repo_root / ".venv" / "bin" / "celery"),
                    "-A", "memory.celery_config",
                    "worker",
                    "-Q", queue,
                    "--loglevel=info",
                    "-c", "2",  # 2 concurrent workers per queue
                ],
                cwd=str(repo_root),
                stdout=open(logs_dir / f"celery_worker_{queue}.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True
            )

        time.sleep(3)

        # Verify workers started
        running, count = check_celery_workers()
        if running:
            return True, f"Celery workers started ({count} processes)"
        else:
            return False, "Celery workers failed to start"

    except Exception as e:
        return False, f"Failed to restart Celery workers: {e}"


def restart_celery_beat() -> tuple[bool, str]:
    """
    Restart Celery beat scheduler.

    Returns:
        (success, message) tuple
    """
    repo_root = Path(__file__).parent.parent
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(exist_ok=True)

    try:
        # Kill existing beat
        subprocess.run(
            ["pkill", "-f", "celery.*beat"],
            capture_output=True,
            timeout=5
        )
        time.sleep(1)

        # Start beat
        subprocess.Popen(
            [
                str(repo_root / ".venv" / "bin" / "celery"),
                "-A", "memory.celery_config",
                "beat",
                "--loglevel=info",
            ],
            cwd=str(repo_root),
            stdout=open(logs_dir / "celery_beat.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
        time.sleep(2)

        # Verify beat started
        if check_celery_beat():
            return True, "Celery beat scheduler started"
        else:
            return False, "Celery beat failed to start"

    except Exception as e:
        return False, f"Failed to restart Celery beat: {e}"


def restart_heavy_services() -> tuple[bool, str]:
    """
    Restart all heavy mode services (Celery workers + beat).
    Docker containers should be restarted via restart_docker_containers().

    Returns:
        (success, message) tuple
    """
    results = []

    # Restart workers
    success, msg = restart_celery_worker()
    results.append(("workers", success, msg))

    # Restart beat
    success, msg = restart_celery_beat()
    results.append(("beat", success, msg))

    all_success = all(r[1] for r in results)
    summary = "; ".join(f"{r[0]}: {r[2]}" for r in results)

    return all_success, summary


# =============================================================================
# LOCAL SERVER RESTARTS (Both Modes)
# =============================================================================

def restart_llm_server(port: int = 8000) -> tuple[bool, str]:
    """
    Restart the local LLM server (mlx_lm.server via launchd).

    Returns:
        (success, message) tuple
    """
    repo_root = Path(__file__).parent.parent
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(exist_ok=True)

    try:
        # Kill existing LLM server on the port
        subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5
        )
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
            time.sleep(2)

        # Try to find and start the LLM server
        # Check for common LLM server scripts/configs
        llm_scripts = [
            repo_root / "scripts" / "start-llm.sh",
            repo_root / "scripts" / "start-llm.sh",
            repo_root / "start-llm.sh",
            Path.home() / ".local" / "bin" / "mlx_lm",
        ]

        for script in llm_scripts:
            if script.exists():
                subprocess.Popen(
                    [str(script)],
                    cwd=str(repo_root),
                    stdout=open(logs_dir / "llm_server.log", "a"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True
                )
                time.sleep(3)

                # Verify it started
                running, _ = check_llm_server(port)
                if running:
                    return True, f"LLM server started via {script.name}"

        # Fallback: Try mlx_lm.server if available
        try:
            subprocess.Popen(
                ["python", "-m", "mlx_lm.server", "--port", str(port)],
                cwd=str(repo_root),
                stdout=open(logs_dir / "llm_server.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            time.sleep(5)
            running, _ = check_llm_server(port)
            if running:
                return True, "LLM server started via mlx_lm.server"
        except Exception as e:
            print(f"[WARN] LLM server restart via mlx_lm.server failed: {e}")

        return False, "No LLM server script found - manual start required"

    except Exception as e:
        return False, f"Failed to restart LLM server: {e}"


def restart_voice_server(port: int = 8888) -> tuple[bool, str]:
    """
    Restart the voice/synaptic chat server.

    Returns:
        (success, message) tuple
    """
    repo_root = Path(__file__).parent.parent
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(exist_ok=True)

    try:
        # Kill existing voice server
        subprocess.run(
            ["pkill", "-f", "synaptic_chat_server"],
            capture_output=True,
            timeout=5
        )
        # Also kill by port
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
        time.sleep(2)

        # Start voice server via uvicorn
        subprocess.Popen(
            [
                str(repo_root / ".venv" / "bin" / "python"), "-m", "uvicorn",
                "memory.synaptic_chat_server:app",
                "--host", "0.0.0.0",
                "--port", str(port)
            ],
            cwd=str(repo_root),
            stdout=open(logs_dir / "synaptic_chat_server.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
        time.sleep(3)

        # Verify it started
        if check_voice_server(port):
            return True, "Voice server restarted"
        else:
            return False, "Voice server started but not responding"

    except Exception as e:
        return False, f"Failed to restart voice server: {e}"


def restart_docker_containers(containers: List[str] = None) -> tuple[bool, str]:
    """
    Restart Docker containers.

    Args:
        containers: Specific containers to restart, or None for all contextdna containers

    Returns:
        (success, message) tuple
    """
    repo_root = Path(__file__).parent.parent

    try:
        if containers:
            # Restart specific containers
            for container in containers:
                subprocess.run(
                    ["docker", "restart", container],
                    capture_output=True,
                    timeout=30
                )
            return True, f"Restarted containers: {', '.join(containers)}"
        else:
            # Use docker-compose to restart all
            compose_file = repo_root / "docker-compose.yml"
            if compose_file.exists():
                subprocess.run(
                    ["docker-compose", "up", "-d"],
                    cwd=str(repo_root),
                    capture_output=True,
                    timeout=60
                )
                return True, "docker-compose up -d completed"
            else:
                return False, "No docker-compose.yml found"
    except subprocess.TimeoutExpired:
        return False, "Docker restart timed out"
    except Exception as e:
        return False, f"Docker restart failed: {e}"


def start_docker_daemon() -> tuple[bool, str]:
    """
    Attempt to start Docker daemon using DockerRecovery multi-strategy launch.

    Returns:
        (success, message) tuple
    """
    try:
        from context_dna.storage.docker_recovery import get_docker_recovery
        recovery = get_docker_recovery()
        # Use smart_recover which tries multi-strategy launch
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Already in async context - run synchronously as fallback
            if recovery.is_docker_daemon_running():
                return True, "Docker daemon already running"
            # Fall back to basic launch in async context
            subprocess.run(["open", "-a", "Docker"], capture_output=True, timeout=10)
            time.sleep(5)
            result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
            if result.returncode == 0:
                return True, "Docker daemon started"
            return False, "Docker daemon starting (may take a moment)"
        else:
            result = asyncio.run(recovery.smart_recover(timeout=30))
            if result['success']:
                strategy = result.get('launch_strategy', result['action_taken'])
                return True, f"Docker daemon started (strategy: {strategy})"
            return False, f"Docker recovery failed: {result['details']}"
    except ImportError:
        # Fallback if DockerRecovery not available
        try:
            subprocess.run(["open", "-a", "Docker"], capture_output=True, timeout=10)
            time.sleep(5)
            result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
            if result.returncode == 0:
                return True, "Docker daemon started"
            return False, "Docker daemon starting (may take a moment)"
        except Exception as e:
            return False, f"Failed to start Docker: {e}"
    except Exception as e:
        return False, f"Failed to start Docker: {e}"


def send_docker_alert(docker_status: DockerStatus, previous_status: Optional[DockerStatus] = None):
    """
    Send notification about Docker/container status changes.

    Triggered when:
    - Docker goes down
    - Mode transitions (lite ↔ heavy)
    - Containers become unhealthy
    """
    # Docker just went down
    if not docker_status.docker_running:
        send_macos_notification(
            title="Context DNA: Docker DOWN",
            message=f"Mode: {docker_status.mode.upper()}\n{', '.join(docker_status.blocking_issues[:2])}",
            subtitle="Containers unavailable",
            sound="Sosumi"
        )
        return

    # Mode changed
    if previous_status and previous_status.mode != docker_status.mode:
        if docker_status.mode == "heavy":
            send_macos_notification(
                title="Context DNA: Heavy Mode",
                message=f"PostgreSQL + Redis active\n{docker_status.healthy_count} containers running",
                subtitle="Full features enabled",
                sound="Glass"
            )
        else:
            blocking = docker_status.blocking_issues[0] if docker_status.blocking_issues else "Unknown issue"
            send_macos_notification(
                title="Context DNA: Lite Mode",
                message=f"Reason: {blocking}",
                subtitle="Running on SQLite",
                sound="Funk"
            )
        return

    # Containers became unhealthy
    if docker_status.unhealthy_count > 0:
        unhealthy_names = [name for name, state in docker_status.containers.items() if state != 'running']
        send_macos_notification(
            title="Context DNA: Container Alert",
            message=f"Unhealthy: {', '.join(unhealthy_names[:3])}",
            subtitle=f"{docker_status.unhealthy_count} container(s) down",
            sound="Basso"
        )


class MutualHeartbeat:
    """
    Mutual heartbeat monitor for Context DNA services.

    Each service:
    1. Writes its own heartbeat every HEARTBEAT_INTERVAL seconds
    2. Monitors the other service's heartbeat
    3. Sends macOS notification if other service goes stale
    """

    def __init__(self, service_name: str):
        """
        Initialize heartbeat monitor.

        Args:
            service_name: Either "agent_service" or "watchdog"
        """
        if service_name not in (SERVICE_AGENT, SERVICE_WATCHDOG):
            raise ValueError(f"service_name must be '{SERVICE_AGENT}' or '{SERVICE_WATCHDOG}'")

        self.service_name = service_name
        self.other_service = SERVICE_WATCHDOG if service_name == SERVICE_AGENT else SERVICE_AGENT

        self.heartbeat_path = get_heartbeat_path(service_name)
        self.other_heartbeat_path = get_heartbeat_path(self.other_service)

        self.start_time = datetime.now()
        self._running = False
        self._other_was_down = False  # Track state for recovery notification

        # Docker/mode tracking
        self._last_docker_status: Optional[DockerStatus] = None
        self._docker_check_interval = 30  # Check Docker every 30 seconds
        self._last_docker_check = 0

        # Auto-recovery settings
        self.auto_restart_services = True   # Auto-restart other service if down
        self.auto_restart_docker = True     # Auto-restart Docker/containers if down
        self.auto_restart_servers = True    # Auto-restart LLM/voice servers if down
        self._restart_cooldown = 60         # Seconds between restart attempts
        self._last_service_restart = 0
        self._last_docker_restart = 0
        self._last_server_restart = 0
        self._restart_attempts = {}         # Track restart attempts per target

        # Server monitoring
        self._last_server_check = 0
        self._server_check_interval = 60    # Check servers every 60 seconds
        self._last_llm_status = None
        self._last_voice_status = None

        # Debug info to include in heartbeat
        self.debug_info: Dict[str, Any] = {}

        # Observability store for historical tracking
        self._observability_store = None
        self._observability_check_interval = 60  # Record status every 60 seconds
        self._last_observability_record = 0

    def set_debug_info(self, key: str, value: Any):
        """Set debug info to include in heartbeat."""
        self.debug_info[key] = value

    def _get_obs_store(self):
        """Get observability store (lazy init)."""
        if self._observability_store is None:
            self._observability_store = get_observability_store()
        return self._observability_store

    def _record_dependency_status(
        self,
        dependency_name: str,
        is_healthy: bool,
        latency_ms: int = None,
        reason_code: str = None,
        details: Dict[str, Any] = None
    ):
        """Record a dependency health check to observability store."""
        store = self._get_obs_store()
        if store:
            try:
                store.record_dependency_status(
                    dependency_name=dependency_name,
                    status="healthy" if is_healthy else "down",
                    latency_ms=latency_ms,
                    reason_code=reason_code,
                    details=details
                )
            except Exception as e:
                logger.debug(f"Failed to record dependency status: {e}")

    def _record_recovery_attempt(
        self,
        service_name: str,
        success: bool,
        duration_ms: int,
        message: str
    ):
        """Record a recovery attempt to observability store."""
        store = self._get_obs_store()
        if store:
            try:
                store.record_recovery_attempt(
                    service_name=service_name,
                    success=success,
                    duration_ms=duration_ms,
                    message=message
                )
            except Exception as e:
                logger.debug(f"Failed to record recovery attempt: {e}")

    def write_heartbeat(self):
        """Write current heartbeat to file."""
        heartbeat = Heartbeat(
            service=self.service_name,
            pid=os.getpid(),
            timestamp=datetime.now().isoformat(),
            uptime_seconds=(datetime.now() - self.start_time).total_seconds(),
            status="running",
            debug_info=self.debug_info.copy()
        )

        # Atomic write (write to temp, then rename)
        temp_path = self.heartbeat_path.with_suffix('.tmp')
        with open(temp_path, 'w') as f:
            json.dump(asdict(heartbeat), f, indent=2)
        temp_path.rename(self.heartbeat_path)

    def read_heartbeat(self, service: str) -> Optional[Heartbeat]:
        """Read heartbeat from a service."""
        path = get_heartbeat_path(service)
        if not path.exists():
            return None

        try:
            with open(path, 'r') as f:
                data = json.load(f)
            return Heartbeat(**data)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"Failed to read heartbeat for {service}: {e}")
            return None

    def is_service_alive(self, service: str) -> tuple[bool, Optional[Heartbeat]]:
        """
        Check if a service is alive based on its heartbeat.

        Returns:
            (is_alive, heartbeat) - is_alive is True if heartbeat is fresh
        """
        heartbeat = self.read_heartbeat(service)
        if not heartbeat:
            return False, None

        try:
            last_beat = datetime.fromisoformat(heartbeat.timestamp)
            age = (datetime.now() - last_beat).total_seconds()
            is_alive = age < STALE_THRESHOLD
            return is_alive, heartbeat
        except (ValueError, TypeError):
            return False, heartbeat

    def check_other_service(self):
        """Check if the other service is alive and alert/restart if not."""
        is_alive, heartbeat = self.is_service_alive(self.other_service)

        if not is_alive:
            if not self._other_was_down:
                # Just went down - send alert
                last_seen = heartbeat.timestamp if heartbeat else "never"
                debug_info = heartbeat.debug_info if heartbeat else {}
                debug_info["pid"] = heartbeat.pid if heartbeat else "unknown"

                logger.warning(f"{self.other_service} appears DOWN (last seen: {last_seen})")
                send_critical_alert(self.other_service, last_seen, debug_info)
                self._other_was_down = True

            # Attempt auto-restart if enabled
            if self.auto_restart_services:
                self._attempt_service_restart(self.other_service)
        else:
            if self._other_was_down:
                # Was down, now back - send recovery notification
                logger.info(f"{self.other_service} has recovered")
                send_recovery_notification(self.other_service)
                self._other_was_down = False
                # Reset restart attempts on recovery
                self._restart_attempts[self.other_service] = 0

    def _attempt_service_restart(self, service: str):
        """Attempt to restart a service with cooldown and retry limits."""
        now = time.time()

        # Check cooldown
        if now - self._last_service_restart < self._restart_cooldown:
            return  # Still in cooldown

        # Check retry limit (max 3 attempts before giving up)
        attempts = self._restart_attempts.get(service, 0)
        if attempts >= 3:
            logger.warning(f"Max restart attempts reached for {service}")
            return

        # Attempt restart
        logger.info(f"Attempting to restart {service} (attempt {attempts + 1}/3)...")
        success, message = restart_service(service)

        self._last_service_restart = now
        self._restart_attempts[service] = attempts + 1

        if success:
            send_macos_notification(
                title="Context DNA: Auto-Recovery",
                message=message,
                subtitle=f"Restarting {service}",
                sound="Purr"
            )
        else:
            logger.error(f"Auto-restart failed: {message}")

    def check_docker_status(self):
        """
        Check Docker/container status and alert on mode changes.
        Includes all containers in heartbeat debug info.
        """
        current_status = check_docker_status()

        # Update debug info with Docker status
        self.debug_info["mode"] = current_status.mode
        self.debug_info["docker_running"] = current_status.docker_running
        self.debug_info["containers_healthy"] = current_status.healthy_count
        self.debug_info["containers_unhealthy"] = current_status.unhealthy_count
        self.debug_info["postgres"] = current_status.postgres_available
        self.debug_info["redis"] = current_status.redis_available

        if current_status.blocking_issues:
            self.debug_info["blocking"] = current_status.blocking_issues[:3]

        # Record Docker dependencies to observability (periodic)
        now = time.time()
        if (now - self._last_observability_record) >= self._observability_check_interval:
            self._record_dependency_status(
                "docker", current_status.docker_running,
                reason_code="DOCKER_DOWN" if not current_status.docker_running else None,
                details={"mode": current_status.mode, "healthy": current_status.healthy_count}
            )
            self._record_dependency_status(
                "postgres", current_status.postgres_available,
                reason_code="POSTGRES_UNAVAILABLE" if not current_status.postgres_available else None
            )
            self._record_dependency_status(
                "redis_docker", current_status.redis_available,
                reason_code="REDIS_UNAVAILABLE" if not current_status.redis_available else None
            )

        # Check for changes that need notification
        if self._last_docker_status:
            # Docker went down
            if self._last_docker_status.docker_running and not current_status.docker_running:
                send_docker_alert(current_status, self._last_docker_status)

            # Mode changed
            elif self._last_docker_status.mode != current_status.mode:
                send_docker_alert(current_status, self._last_docker_status)

            # Containers became unhealthy (only alert on increase)
            elif current_status.unhealthy_count > self._last_docker_status.unhealthy_count:
                send_docker_alert(current_status, self._last_docker_status)

            # Docker recovered
            elif not self._last_docker_status.docker_running and current_status.docker_running:
                send_macos_notification(
                    title="Context DNA: Docker Recovered",
                    message=f"Mode: {current_status.mode.upper()}\n{current_status.healthy_count} containers running",
                    subtitle="Docker back online",
                    sound="Glass"
                )

        # Attempt auto-recovery if enabled
        if self.auto_restart_docker:
            self._attempt_docker_recovery(current_status)

        self._last_docker_status = current_status
        return current_status

    def _attempt_docker_recovery(self, docker_status: DockerStatus):
        """Attempt to recover Docker/containers if down, respecting intended mode."""
        now = time.time()

        # Check cooldown
        if now - self._last_docker_restart < self._restart_cooldown:
            return  # Still in cooldown

        # Check intended mode system - should we even try?
        should_recover, reason = should_attempt_recovery()
        if not should_recover:
            logger.debug(f"Skipping recovery: {reason}")
            return

        config = load_config()
        intended = config["intended_mode"]

        # If we're in lite mode but want heavy - try to recover to heavy
        if docker_status.mode == "lite" and intended == "heavy":
            # Docker daemon not running - try to start it first
            if not docker_status.docker_running:
                attempts = self._restart_attempts.get("docker_daemon", 0)
                if attempts < 3:
                    logger.info(f"Recovering to HEAVY mode: Starting Docker daemon (attempt {attempts + 1}/3)...")
                    start_time = time.time()
                    success, message = start_docker_daemon()
                    duration_ms = int((time.time() - start_time) * 1000)
                    self._last_docker_restart = now
                    self._restart_attempts["docker_daemon"] = attempts + 1
                    record_recovery_attempt(success)

                    # Record to observability
                    self._record_recovery_attempt("docker_daemon", success, duration_ms, message)

                    if success:
                        send_macos_notification(
                            title="Context DNA: Recovering to Heavy",
                            message=message,
                            subtitle="Starting Docker daemon",
                            sound="Purr"
                        )
                return

            # Docker running but PostgreSQL not available - start containers
            if not docker_status.postgres_available:
                attempts = self._restart_attempts.get("containers", 0)
                if attempts < 3:
                    logger.info(f"Recovering to HEAVY mode: Starting containers (attempt {attempts + 1}/3)...")
                    start_time = time.time()
                    success, message = restart_docker_containers()
                    duration_ms = int((time.time() - start_time) * 1000)
                    self._last_docker_restart = now
                    self._restart_attempts["containers"] = attempts + 1
                    record_recovery_attempt(success)

                    # Record to observability
                    self._record_recovery_attempt("docker_containers", success, duration_ms, message)

                    if success:
                        send_macos_notification(
                            title="Context DNA: Recovering to Heavy",
                            message=message,
                            subtitle="Starting PostgreSQL + Redis",
                            sound="Purr"
                        )
                return

        # If in heavy mode but some containers unhealthy - try to fix
        if docker_status.mode == "heavy" and docker_status.unhealthy_count > 0:
            unhealthy = [name for name, state in docker_status.containers.items()
                        if state != 'running']

            if unhealthy:
                attempts = self._restart_attempts.get("containers", 0)
                if attempts < 3:
                    logger.info(f"Maintaining HEAVY mode: Restarting unhealthy containers: {unhealthy}")
                    start_time = time.time()
                    success, message = restart_docker_containers(unhealthy)
                    duration_ms = int((time.time() - start_time) * 1000)
                    self._last_docker_restart = now
                    self._restart_attempts["containers"] = attempts + 1

                    # Record to observability
                    self._record_recovery_attempt(
                        "docker_containers", success, duration_ms,
                        f"{message} (unhealthy: {unhealthy})"
                    )

                    if success:
                        send_macos_notification(
                            title="Context DNA: Maintaining Heavy",
                            message=message,
                            subtitle="Restarting containers",
                            sound="Purr"
                        )

        # Successfully in intended mode - reset recovery tracking
        if docker_status.mode == intended:
            if config.get("recovery_attempt_count", 0) > 0:
                # We recovered successfully!
                reset_recovery_attempts()
                send_macos_notification(
                    title="Context DNA: Recovery Complete",
                    message=f"Now running in {intended.upper()} mode",
                    subtitle="All systems operational",
                    sound="Glass"
                )
            self._restart_attempts["docker_daemon"] = 0
            self._restart_attempts["containers"] = 0

    def check_server_status(self):
        """
        Check all server status based on intended mode and attempt restarts.
        Updates debug info with comprehensive server status.
        """
        config = load_config()
        intended_mode = config["intended_mode"]

        # Local servers (both modes)
        llm_running, llm_info = check_llm_server()
        voice_running = check_voice_server()

        # Update debug info - local servers
        self.debug_info["llm_server"] = "running" if llm_running else "down"
        self.debug_info["voice_server"] = "running" if voice_running else "down"

        # Record to observability store (periodic)
        now = time.time()
        should_record = (now - self._last_observability_record) >= self._observability_check_interval
        if should_record:
            self._record_dependency_status(
                "llm_server", llm_running,
                reason_code="LLM_UNREACHABLE" if not llm_running else None,
                details={"model": llm_info} if llm_running else None
            )
            self._record_dependency_status(
                "voice_server", voice_running,
                reason_code="VOICE_UNREACHABLE" if not voice_running else None
            )
            self._last_observability_record = now

        # Heavy mode services
        celery_running, celery_count = False, 0
        beat_running = False
        if intended_mode == "heavy":
            celery_running, celery_count = check_celery_workers()
            beat_running = check_celery_beat()
            self.debug_info["celery_workers"] = f"{celery_count}" if celery_running else "down"
            self.debug_info["celery_beat"] = "running" if beat_running else "down"

            # Record heavy mode services to observability
            if should_record:
                self._record_dependency_status(
                    "celery_workers", celery_running,
                    reason_code="CELERY_DOWN" if not celery_running else None,
                    details={"count": celery_count} if celery_running else None
                )
                self._record_dependency_status(
                    "celery_beat", beat_running,
                    reason_code="BEAT_DOWN" if not beat_running else None
                )

        # Detect state changes and notify - LLM
        if self._last_llm_status is not None:
            if self._last_llm_status and not llm_running:
                send_macos_notification(
                    title="Context DNA: LLM Server DOWN",
                    message="Local LLM server stopped responding",
                    subtitle="Port 8000 unavailable",
                    sound="Basso"
                )
            elif not self._last_llm_status and llm_running:
                send_macos_notification(
                    title="Context DNA: LLM Server Recovered",
                    message="LLM server back online",
                    subtitle="Port 8000 responding",
                    sound="Glass"
                )

        # Detect state changes and notify - Voice
        if self._last_voice_status is not None:
            if self._last_voice_status and not voice_running:
                send_macos_notification(
                    title="Context DNA: Voice Server DOWN",
                    message="Synaptic chat server stopped",
                    subtitle="Port 8888 unavailable",
                    sound="Funk"
                )
            elif not self._last_voice_status and voice_running:
                send_macos_notification(
                    title="Context DNA: Voice Server Recovered",
                    message="Voice server back online",
                    subtitle="Port 8888 responding",
                    sound="Glass"
                )

        # Detect state changes - Celery (heavy mode only)
        if intended_mode == "heavy":
            last_celery = self._restart_attempts.get("_last_celery_status")
            if last_celery is not None:
                if last_celery and not celery_running:
                    send_macos_notification(
                        title="Context DNA: Celery Workers DOWN",
                        message="Background task processing stopped",
                        subtitle="Heavy mode degraded",
                        sound="Basso"
                    )
                elif not last_celery and celery_running:
                    send_macos_notification(
                        title="Context DNA: Celery Recovered",
                        message=f"{celery_count} workers back online",
                        subtitle="Background processing restored",
                        sound="Glass"
                    )
            self._restart_attempts["_last_celery_status"] = celery_running

        self._last_llm_status = llm_running
        self._last_voice_status = voice_running

        # Attempt auto-restart if enabled
        if self.auto_restart_servers:
            self._attempt_server_recovery(
                llm_running, voice_running,
                celery_running, beat_running,
                intended_mode
            )

        return llm_running, voice_running

    def _attempt_server_recovery(
        self,
        llm_running: bool,
        voice_running: bool,
        celery_running: bool = True,
        beat_running: bool = True,
        intended_mode: str = "lite"
    ):
        """
        Attempt to restart down servers with cooldown and retry limits.
        Mode-aware: restarts appropriate services based on intended mode.
        """
        now = time.time()

        # Check cooldown
        if now - self._last_server_restart < self._restart_cooldown:
            return

        # Check recovery mode - manual mode skips auto-restart
        config = load_config()
        if config.get("recovery_mode") == RecoveryMode.MANUAL:
            return

        # Priority order:
        # 1. LLM server (both modes - critical for AI features)
        # 2. Celery workers (heavy mode - background processing)
        # 3. Celery beat (heavy mode - scheduled tasks)
        # 4. Voice server (both modes - optional but useful)

        # LLM server recovery (both modes)
        if not llm_running:
            attempts = self._restart_attempts.get("llm_server", 0)
            if attempts < 3:
                logger.info(f"Attempting to restart LLM server (attempt {attempts + 1}/3)...")
                start_time = time.time()
                success, message = restart_llm_server()
                duration_ms = int((time.time() - start_time) * 1000)
                self._last_server_restart = now
                self._restart_attempts["llm_server"] = attempts + 1

                # Record recovery attempt to observability
                self._record_recovery_attempt("llm_server", success, duration_ms, message)

                if success:
                    send_macos_notification(
                        title="Context DNA: Auto-Recovery",
                        message=message,
                        subtitle="Restarting LLM server",
                        sound="Purr"
                    )
                else:
                    logger.warning(f"LLM server restart failed: {message}")
            return  # Only try one restart per cycle

        # Celery workers recovery (heavy mode only)
        if intended_mode == "heavy" and not celery_running:
            attempts = self._restart_attempts.get("celery_workers", 0)
            if attempts < 3:
                logger.info(f"Attempting to restart Celery workers (attempt {attempts + 1}/3)...")
                start_time = time.time()
                success, message = restart_celery_worker()
                duration_ms = int((time.time() - start_time) * 1000)
                self._last_server_restart = now
                self._restart_attempts["celery_workers"] = attempts + 1

                # Record recovery attempt to observability
                self._record_recovery_attempt("celery_workers", success, duration_ms, message)

                if success:
                    send_macos_notification(
                        title="Context DNA: Auto-Recovery",
                        message=message,
                        subtitle="Restarting Celery workers",
                        sound="Purr"
                    )
                else:
                    logger.warning(f"Celery worker restart failed: {message}")
            return

        # Celery beat recovery (heavy mode only)
        if intended_mode == "heavy" and not beat_running:
            attempts = self._restart_attempts.get("celery_beat", 0)
            if attempts < 3:
                logger.info(f"Attempting to restart Celery beat (attempt {attempts + 1}/3)...")
                start_time = time.time()
                success, message = restart_celery_beat()
                duration_ms = int((time.time() - start_time) * 1000)
                self._last_server_restart = now
                self._restart_attempts["celery_beat"] = attempts + 1

                # Record recovery attempt to observability
                self._record_recovery_attempt("celery_beat", success, duration_ms, message)

                if success:
                    send_macos_notification(
                        title="Context DNA: Auto-Recovery",
                        message=message,
                        subtitle="Restarting Celery beat",
                        sound="Purr"
                    )
                else:
                    logger.warning(f"Celery beat restart failed: {message}")
            return

        # Voice server recovery (both modes - lower priority)
        if not voice_running:
            attempts = self._restart_attempts.get("voice_server", 0)
            if attempts < 3:
                logger.info(f"Attempting to restart voice server (attempt {attempts + 1}/3)...")
                start_time = time.time()
                success, message = restart_voice_server()
                duration_ms = int((time.time() - start_time) * 1000)
                self._last_server_restart = now
                self._restart_attempts["voice_server"] = attempts + 1

                # Record recovery attempt to observability
                self._record_recovery_attempt("voice_server", success, duration_ms, message)

                if success:
                    send_macos_notification(
                        title="Context DNA: Auto-Recovery",
                        message=message,
                        subtitle="Restarting voice server",
                        sound="Purr"
                    )
                else:
                    logger.warning(f"Voice server restart failed: {message}")

        # Reset attempts when services are healthy
        if llm_running:
            self._restart_attempts["llm_server"] = 0
        if voice_running:
            self._restart_attempts["voice_server"] = 0
        if celery_running:
            self._restart_attempts["celery_workers"] = 0
        if beat_running:
            self._restart_attempts["celery_beat"] = 0

    # =========================================================================
    # ASYNC VERSION (for agent_service.py)
    # =========================================================================

    async def start(self):
        """Start async heartbeat writer and monitor."""
        self._running = True

        # Start both tasks
        asyncio.create_task(self._heartbeat_writer_loop())
        asyncio.create_task(self._monitor_loop())

        logger.info(f"Mutual heartbeat started for {self.service_name}")

    async def stop(self):
        """Stop the heartbeat system."""
        self._running = False

        # Write final heartbeat with stopped status
        heartbeat = Heartbeat(
            service=self.service_name,
            pid=os.getpid(),
            timestamp=datetime.now().isoformat(),
            uptime_seconds=(datetime.now() - self.start_time).total_seconds(),
            status="stopped",
            debug_info=self.debug_info.copy()
        )
        with open(self.heartbeat_path, 'w') as f:
            json.dump(asdict(heartbeat), f, indent=2)

    async def _heartbeat_writer_loop(self):
        """Async loop that writes heartbeat periodically."""
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                await loop.run_in_executor(None, self.write_heartbeat)
            except Exception as e:
                logger.error(f"Failed to write heartbeat: {e}")

            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _monitor_loop(self):
        """Async loop that monitors the other service, Docker, and servers.

        IMPORTANT: All check methods contain blocking I/O (subprocess.run,
        urllib.request.urlopen) and MUST run in an executor to avoid freezing
        the uvicorn event loop. This was the root cause of FD exhaustion and
        health endpoint timeouts (fixed 2026-02-06).
        """
        # Wait a bit before starting to monitor (let other service start)
        await asyncio.sleep(CHECK_INTERVAL)

        loop = asyncio.get_event_loop()

        while self._running:
            try:
                # Check other service (blocking I/O - run in executor)
                await loop.run_in_executor(None, self.check_other_service)

                now = time.time()

                # Check Docker status periodically (blocking subprocess - run in executor)
                if now - self._last_docker_check >= self._docker_check_interval:
                    await loop.run_in_executor(None, self.check_docker_status)
                    self._last_docker_check = now

                # Check server status periodically (blocking HTTP - run in executor)
                if now - self._last_server_check >= self._server_check_interval:
                    await loop.run_in_executor(None, self.check_server_status)
                    self._last_server_check = now

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

            await asyncio.sleep(CHECK_INTERVAL)

    # =========================================================================
    # SYNC VERSION (for synaptic_watchdog_daemon.py)
    # =========================================================================

    def start_sync(self):
        """Start synchronous heartbeat writer and monitor (for daemon)."""
        self._running = True
        logger.info(f"Mutual heartbeat started for {self.service_name} (sync mode)")

        last_write = 0
        last_check = 0

        while self._running:
            now = time.time()

            # Write heartbeat
            if now - last_write >= HEARTBEAT_INTERVAL:
                try:
                    self.write_heartbeat()
                    last_write = now
                except Exception as e:
                    logger.error(f"Failed to write heartbeat: {e}")

            # Check other service
            if now - last_check >= CHECK_INTERVAL:
                try:
                    self.check_other_service()
                    last_check = now
                except Exception as e:
                    logger.error(f"Failed to check other service: {e}")

            # Check Docker status periodically
            if now - self._last_docker_check >= self._docker_check_interval:
                try:
                    self.check_docker_status()
                    self._last_docker_check = now
                except Exception as e:
                    logger.error(f"Failed to check Docker: {e}")

            # Check server status periodically
            if now - self._last_server_check >= self._server_check_interval:
                try:
                    self.check_server_status()
                    self._last_server_check = now
                except Exception as e:
                    logger.error(f"Failed to check servers: {e}")

            time.sleep(1)  # Sleep 1 second between checks

    def stop_sync(self):
        """Stop the synchronous heartbeat system."""
        self._running = False


# =============================================================================
# CLI INTERFACE
# =============================================================================

def get_status() -> dict:
    """Get status of both services."""
    status = {}

    for service in (SERVICE_AGENT, SERVICE_WATCHDOG):
        path = get_heartbeat_path(service)
        if not path.exists():
            status[service] = {"status": "no_heartbeat", "alive": False}
            continue

        try:
            with open(path, 'r') as f:
                data = json.load(f)

            last_beat = datetime.fromisoformat(data["timestamp"])
            age = (datetime.now() - last_beat).total_seconds()

            status[service] = {
                "status": data.get("status", "unknown"),
                "alive": age < STALE_THRESHOLD,
                "age_seconds": round(age, 1),
                "pid": data.get("pid"),
                "uptime_seconds": round(data.get("uptime_seconds", 0), 1),
                "debug_info": data.get("debug_info", {}),
            }
        except Exception as e:
            status[service] = {"status": "error", "alive": False, "error": str(e)}

    return status


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Mutual Heartbeat System - Who Watches the Watchdog?")
        print()
        print("Usage:")
        print("  python mutual_heartbeat.py status                # Show status of all services")
        print("  python mutual_heartbeat.py infra                 # Full infrastructure status (Docker + LLM)")
        print("  python mutual_heartbeat.py recover               # Auto-recover all down services")
        print("  python mutual_heartbeat.py restart agent         # Restart agent_service")
        print("  python mutual_heartbeat.py restart watchdog      # Restart watchdog daemon")
        print("  python mutual_heartbeat.py restart docker        # Restart Docker containers")
        print("  python mutual_heartbeat.py restart llm           # Restart LLM server (port 8000)")
        print("  python mutual_heartbeat.py restart voice         # Restart voice server (port 8888)")
        print("  python mutual_heartbeat.py restart celery        # Restart Celery workers (heavy mode)")
        print("  python mutual_heartbeat.py restart beat          # Restart Celery beat scheduler")
        print("  python mutual_heartbeat.py restart heavy         # Restart all heavy mode services")
        print("  python mutual_heartbeat.py mode [mode]           # Set recovery mode behavior")
        print("  python mutual_heartbeat.py intended [heavy|lite] # Set/show intended mode")
        print("  python mutual_heartbeat.py accept                # Accept current mode, stop recovery")
        print("  python mutual_heartbeat.py test                  # Send test notification")
        print()
        print("Intended Mode System:")
        print("  The system remembers your preferred mode and auto-recovers to it.")
        print("  Use 'intended heavy' to set Heavy as target (auto-start Docker).")
        print("  Use 'intended lite' to set Lite as target (no auto-start).")
        print("  Use 'accept' to stop recovery and stay in current mode.")
        print()
        print("Recovery Modes (behavior when recovering):")
        print("  aggressive   - Auto-recover everything, always try intended mode")
        print("  conservative - Only restart essential services")
        print("  manual       - Only notify, never auto-restart")
        print()
        print("Auto-Recovery:")
        print("  When services run, they automatically attempt to restart each other")
        print("  and Docker containers to reach your intended mode.")
        print()
        print(f"Heartbeat files:")
        print(f"  {get_heartbeat_path(SERVICE_AGENT)}")
        print(f"  {get_heartbeat_path(SERVICE_WATCHDOG)}")
        print(f"  {CONFIG_FILE}")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "status":
        status = get_status()
        docker = check_docker_status()

        print("=" * 60)
        print("MUTUAL HEARTBEAT STATUS - Who Watches the Watchdog?")
        print("=" * 60)

        # Services
        for service, info in status.items():
            alive_str = "ALIVE" if info.get("alive") else "DOWN"
            color = "\033[92m" if info.get("alive") else "\033[91m"
            reset = "\033[0m"

            print(f"\n{service}:")
            print(f"  Status: {color}{alive_str}{reset}")
            if info.get("age_seconds") is not None:
                print(f"  Last heartbeat: {info['age_seconds']}s ago")
            if info.get("pid"):
                print(f"  PID: {info['pid']}")
            if info.get("uptime_seconds"):
                print(f"  Uptime: {info['uptime_seconds']}s")
            if info.get("debug_info"):
                debug = info['debug_info']
                if debug.get("mode"):
                    print(f"  Mode: {debug['mode']}")
                if debug.get("brain_cycles"):
                    print(f"  Brain cycles: {debug['brain_cycles']}")

        # Docker Status
        print(f"\n{'=' * 60}")
        print("DOCKER / CONTAINER STATUS")
        print("=" * 60)

        docker_color = "\033[92m" if docker.docker_running else "\033[91m"
        mode_color = "\033[92m" if docker.mode == "heavy" else "\033[93m"
        reset = "\033[0m"

        print(f"\n  Docker: {docker_color}{'RUNNING' if docker.docker_running else 'DOWN'}{reset}")
        print(f"  Mode: {mode_color}{docker.mode.upper()}{reset}")
        print(f"  PostgreSQL: {'✓' if docker.postgres_available else '✗'}")
        print(f"  Redis: {'✓' if docker.redis_available else '✗'}")

        if docker.containers:
            print(f"\n  Containers ({docker.healthy_count} healthy, {docker.unhealthy_count} unhealthy):")
            for name, state in docker.containers.items():
                state_color = "\033[92m" if state == "running" else "\033[91m"
                print(f"    {name}: {state_color}{state}{reset}")

        if docker.blocking_issues:
            print(f"\n  ⚠️  Blocking issues:")
            for issue in docker.blocking_issues:
                print(f"    - {issue}")

        # Intended Mode Info
        config = load_config()
        print(f"\n{'=' * 60}")
        print("INTENDED MODE")
        print("=" * 60)
        intended_color = "\033[92m" if docker.mode == config["intended_mode"] else "\033[93m"
        print(f"\n  Target: {config['intended_mode'].upper()}")
        print(f"  Current: {intended_color}{docker.mode.upper()}{reset}")
        if config["accepted_fallback"]:
            print(f"  Status: \033[93mFallback accepted (recovery paused)\033[0m")
        elif docker.mode != config["intended_mode"]:
            print(f"  Status: \033[93mRecovering ({config['recovery_attempt_count']}/{config['max_recovery_attempts']} attempts)\033[0m")
        else:
            print(f"  Status: \033[92m✓ Running in intended mode\033[0m")

    elif cmd == "test":
        print("Sending test notification...")
        send_macos_notification(
            title="Context DNA Test",
            message="Mutual heartbeat notification test",
            subtitle="This is a test",
            sound="Glass"
        )
        print("Done! Check your notifications.")

    elif cmd == "recover":
        print("=" * 60)
        print("MANUAL RECOVERY - Attempting to restart down services")
        print("=" * 60)

        # Check what's down
        status = get_status()
        docker = check_docker_status()

        # Restart down services
        for service, info in status.items():
            if not info.get("alive"):
                print(f"\n{service} is DOWN - attempting restart...")
                success, message = restart_service(service)
                print(f"  {'✓' if success else '✗'} {message}")

        # Handle Docker
        if not docker.docker_running:
            print(f"\nDocker daemon is DOWN - attempting start...")
            success, message = start_docker_daemon()
            print(f"  {'✓' if success else '✗'} {message}")
        elif docker.unhealthy_count > 0:
            unhealthy = [name for name, state in docker.containers.items()
                        if state != 'running']
            print(f"\nUnhealthy containers: {unhealthy}")
            print("Attempting restart...")
            success, message = restart_docker_containers(unhealthy)
            print(f"  {'✓' if success else '✗'} {message}")
        else:
            print(f"\nDocker: OK ({docker.healthy_count} containers running)")

        print("\n" + "=" * 60)
        print("Recovery complete. Run 'status' to verify.")

    elif cmd == "restart":
        # Restart specific service
        if len(sys.argv) < 3:
            print("Usage: python mutual_heartbeat.py restart [agent|watchdog|docker|llm|voice|celery|beat|heavy]")
            sys.exit(1)

        target = sys.argv[2].lower()
        if target in ("agent", "agent_service"):
            success, message = restart_service(SERVICE_AGENT)
        elif target in ("watchdog", "daemon"):
            success, message = restart_service(SERVICE_WATCHDOG)
        elif target == "docker":
            success, message = restart_docker_containers()
        elif target == "llm":
            print("Restarting LLM server...")
            success, message = restart_llm_server()
        elif target == "voice":
            print("Restarting voice server...")
            success, message = restart_voice_server()
        elif target in ("celery", "workers"):
            print("Restarting Celery workers...")
            success, message = restart_celery_worker()
        elif target == "beat":
            print("Restarting Celery beat scheduler...")
            success, message = restart_celery_beat()
        elif target == "heavy":
            print("Restarting all heavy mode services...")
            success, message = restart_heavy_services()
        else:
            print(f"Unknown target: {target}")
            print("Valid targets: agent, watchdog, docker, llm, voice, celery, beat, heavy")
            sys.exit(1)

        print(f"{'✓' if success else '✗'} {message}")

    elif cmd == "mode":
        # Set recovery mode
        if len(sys.argv) < 3:
            print(f"Current recovery mode: {RecoveryMode.current}")
            print()
            print("Usage: python mutual_heartbeat.py mode [aggressive|conservative|manual]")
            print()
            print("Modes:")
            print("  aggressive   - Auto-recover everything, always try heavy mode")
            print("  conservative - Only restart essential services, prefer lite mode")
            print("  manual       - Only notify, never auto-restart")
            sys.exit(0)

        mode = sys.argv[2].lower()
        if mode == "aggressive":
            RecoveryMode.current = RecoveryMode.AGGRESSIVE
        elif mode == "conservative":
            RecoveryMode.current = RecoveryMode.CONSERVATIVE
        elif mode == "manual":
            RecoveryMode.current = RecoveryMode.MANUAL
        else:
            print(f"Unknown mode: {mode}")
            sys.exit(1)

        print(f"Recovery mode set to: {RecoveryMode.current}")
        send_macos_notification(
            title="Context DNA: Recovery Mode",
            message=f"Mode: {RecoveryMode.current.upper()}",
            subtitle="Recovery behavior updated",
            sound="Pop"
        )

    elif cmd == "infra":
        # Full infrastructure status
        print("=" * 60)
        print("FULL INFRASTRUCTURE STATUS")
        print("=" * 60)

        infra = get_infra_status()
        docker = infra.docker
        config = load_config()
        reset = "\033[0m"

        # Mode summary
        intended_mode = config["intended_mode"]
        mode_color = "\033[92m" if docker.mode == intended_mode else "\033[93m"
        print(f"\nIntended Mode: {intended_mode.upper()}")
        print(f"Current Mode:  {mode_color}{docker.mode.upper()}{reset}")

        # Docker / Containers
        print(f"\n{'=' * 60}")
        print("DOCKER / CONTAINERS")
        print("=" * 60)

        docker_color = "\033[92m" if docker.docker_running else "\033[91m"
        print(f"\nDocker: {docker_color}{'RUNNING' if docker.docker_running else 'DOWN'}{reset}")
        print(f"PostgreSQL: {'✓' if docker.postgres_available else '✗'}")
        print(f"Redis (Docker): {'✓' if docker.redis_available else '✗'}")

        if docker.containers:
            print(f"\nContainers ({docker.healthy_count} healthy, {docker.unhealthy_count} unhealthy):")
            for name, state in docker.containers.items():
                state_color = "\033[92m" if state == "running" else "\033[91m"
                print(f"  {name}: {state_color}{state}{reset}")

        # Heavy Mode Services
        print(f"\n{'=' * 60}")
        print("HEAVY MODE SERVICES")
        print("=" * 60)

        redis_color = "\033[92m" if infra.redis_running else "\033[91m"
        print(f"\nRedis: {redis_color}{'RUNNING' if infra.redis_running else 'DOWN'}{reset}")

        celery_color = "\033[92m" if infra.celery_workers_running else "\033[91m"
        print(f"Celery Workers: {celery_color}{'RUNNING (' + str(infra.celery_worker_count) + ')' if infra.celery_workers_running else 'DOWN'}{reset}")
        if not infra.celery_workers_running and intended_mode == "heavy":
            print(f"  → Auto-restart enabled | Manual: python mutual_heartbeat.py restart celery")

        beat_color = "\033[92m" if infra.celery_beat_running else "\033[91m"
        print(f"Celery Beat: {beat_color}{'RUNNING' if infra.celery_beat_running else 'DOWN'}{reset}")
        if not infra.celery_beat_running and intended_mode == "heavy":
            print(f"  → Auto-restart enabled | Manual: python mutual_heartbeat.py restart beat")

        seaweed_color = "\033[92m" if infra.seaweedfs_running else "\033[93m"
        print(f"SeaweedFS: {seaweed_color}{'RUNNING' if infra.seaweedfs_running else 'NOT RUNNING'}{reset}")

        ollama_color = "\033[92m" if infra.ollama_running else "\033[93m"
        print(f"Ollama: {ollama_color}{'RUNNING' if infra.ollama_running else 'NOT RUNNING'}{reset}")

        heavy_ready = "\033[92m✓ READY" if infra.heavy_mode_ready else "\033[91m✗ NOT READY"
        print(f"\nHeavy Mode Status: {heavy_ready}{reset}")

        # Local Servers (Both Modes)
        print(f"\n{'=' * 60}")
        print("LOCAL SERVERS (BOTH MODES)")
        print("=" * 60)

        agent_color = "\033[92m" if infra.agent_service_running else "\033[91m"
        print(f"\nagent_service (8080): {agent_color}{'RUNNING' if infra.agent_service_running else 'DOWN'}{reset}")
        if not infra.agent_service_running:
            print(f"  → Manual: python mutual_heartbeat.py restart agent")

        llm_color = "\033[92m" if infra.llm_server_running else "\033[91m"
        print(f"LLM Server ({infra.llm_server_port}): {llm_color}{'RUNNING' if infra.llm_server_running else 'DOWN'}{reset}")
        if infra.llm_server_running and infra.llm_server_model:
            print(f"  Model: {infra.llm_server_model[:50]}...")
        elif not infra.llm_server_running:
            print(f"  → Auto-restart enabled | Manual: python mutual_heartbeat.py restart llm")

        voice_color = "\033[92m" if infra.voice_server_running else "\033[93m"
        print(f"Voice Server (8888): {voice_color}{'RUNNING' if infra.voice_server_running else 'NOT RUNNING'}{reset}")
        if not infra.voice_server_running:
            print(f"  → Auto-restart enabled | Manual: python mutual_heartbeat.py restart voice")

        lite_ready = "\033[92m✓ READY" if infra.lite_mode_ready else "\033[91m✗ NOT READY"
        print(f"\nLite Mode Status: {lite_ready}{reset}")

        # Recovery Info
        print(f"\n{'=' * 60}")
        print("RECOVERY")
        print("=" * 60)
        print(f"\nRecovery Mode: {RecoveryMode.current}")
        print(f"Recommendation: {infra.recovery_recommendation}")

        if infra.missing_services:
            print(f"\n⚠️  Missing services:")
            for svc in infra.missing_services:
                print(f"  - {svc}")

        if docker.blocking_issues:
            print(f"\n⚠️  Blocking issues:")
            for issue in docker.blocking_issues:
                print(f"  - {issue}")

    elif cmd == "intended":
        # Set or show intended mode
        if len(sys.argv) < 3:
            config = load_config()
            docker = check_docker_status()
            print("=" * 60)
            print("INTENDED MODE CONFIGURATION")
            print("=" * 60)
            print(f"\n  Intended mode:     {config['intended_mode'].upper()}")
            print(f"  Current mode:      {docker.mode.upper()}")
            print(f"  Fallback accepted: {'Yes' if config['accepted_fallback'] else 'No'}")
            print(f"  Recovery attempts: {config['recovery_attempt_count']}/{config['max_recovery_attempts']}")
            print()
            print("Set intended mode:")
            print("  python mutual_heartbeat.py intended heavy   # Target Heavy mode")
            print("  python mutual_heartbeat.py intended lite    # Target Lite mode")
        else:
            mode = sys.argv[2].lower()
            if mode in ("heavy", "lite"):
                set_intended_mode(mode)
                print(f"✓ Intended mode set to: {mode.upper()}")
                print("  System will auto-recover to this mode.")
            else:
                print(f"Unknown mode: {mode}")
                print("Use 'heavy' or 'lite'")
                sys.exit(1)

    elif cmd == "accept":
        # Accept current fallback mode
        docker = check_docker_status()
        config = load_config()

        print("=" * 60)
        print("ACCEPTING CURRENT MODE")
        print("=" * 60)
        print(f"\n  Intended was: {config['intended_mode'].upper()}")
        print(f"  Current is:   {docker.mode.upper()}")

        if docker.mode == config["intended_mode"]:
            print("\n✓ Already in intended mode - nothing to accept.")
        else:
            accept_current_mode()
            print(f"\n✓ Accepted {docker.mode.upper()} mode")
            print("  Auto-recovery paused.")
            print("  Use 'intended heavy' to resume recovery to Heavy mode.")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
