#!/usr/bin/env python3
"""
Context DNA Ecosystem Health Monitor

Continuous health monitoring for the Context DNA ecosystem.
Designed to run as a background daemon that ensures all services
are available and properly connected.

WHEN TO RUN CONTAINERS:
=======================
RECOMMENDED: PostgreSQL and Redis should ALWAYS run with Context DNA.
This is the preferred configuration for full Synaptic functionality.

Containers (PostgreSQL, Redis, RabbitMQ) provide:
1. Real-time dashboard updates (WebSocket + Redis pub/sub)
2. Synchronized state across multiple IDEs
3. Celery background workers (Scanner, Distiller, etc.)
4. Persistent storage in PostgreSQL
5. Full observability and tracing

FALLBACK MODE (containers off):
File-based fallback works for:
1. Troubleshooting container issues
2. Offline/airplane development
3. Quick single-file testing
Note: Fallback is for emergencies, not primary operation.

RUNNING THIS MONITOR:
====================
    # One-time check
    python ecosystem_health.py

    # Continuous monitoring (daemon mode)
    python ecosystem_health.py --daemon

    # With auto-recovery
    python ecosystem_health.py --daemon --auto-recover

Usage:
    from memory.ecosystem_health import (
        quick_health_check,
        full_health_check,
        is_ecosystem_ready,
        start_missing_services
    )
"""

import json
import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Health check configuration
HEALTH_CHECK_TIMEOUT = 3  # seconds
DAEMON_INTERVAL = 30  # seconds between checks

# Lite mode configuration
# When enabled, skips starting heavy services (local LLM) when memory is constrained
LITE_MODE_CONFIG_FILE = Path(__file__).parent / ".lite_mode_config.json"

# Memory thresholds (in GB)
LITE_MODE_MEMORY_THRESHOLD_GB = 8  # Below this, force lite mode
MLX_MINIMUM_MEMORY_GB = 6  # Minimum free RAM to start local LLM
MLX_LITE_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"  # Smaller model for lite mode
MLX_FULL_MODEL = "mlx-community/Qwen3-4B-4bit"  # Current production model


def get_lite_mode_config() -> dict:
    """Get lite mode configuration."""
    default_config = {
        "enabled": False,  # User can enable manually
        "auto_detect": True,  # Auto-enable when memory is low
        "skip_mlx": False,  # Skip MLX entirely in lite mode
        "use_lite_model": True,  # Use smaller model in lite mode
        "memory_threshold_gb": LITE_MODE_MEMORY_THRESHOLD_GB,
    }

    if LITE_MODE_CONFIG_FILE.exists():
        try:
            with open(LITE_MODE_CONFIG_FILE) as f:
                user_config = json.load(f)
                default_config.update(user_config)
        except Exception as e:
            print(f"[WARN] Lite mode config load failed: {e}")

    return default_config


def set_lite_mode(enabled: bool = True, skip_mlx: bool = False):
    """Set lite mode configuration."""
    config = get_lite_mode_config()
    config["enabled"] = enabled
    config["skip_mlx"] = skip_mlx

    with open(LITE_MODE_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    return config


def should_use_lite_mode() -> Tuple[bool, str]:
    """
    Determine if lite mode should be used.

    Returns:
        Tuple of (use_lite_mode, reason)
    """
    config = get_lite_mode_config()

    # User manually enabled
    if config.get("enabled"):
        return True, "User enabled"

    # Auto-detect based on memory
    if config.get("auto_detect", True):
        try:
            import psutil
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024 ** 3)

            if available_gb < config.get("memory_threshold_gb", LITE_MODE_MEMORY_THRESHOLD_GB):
                return True, f"Low memory ({available_gb:.1f}GB free)"
        except ImportError as e:
            print(f"[WARN] psutil not available for memory check: {e}")

    return False, "Normal mode"


def get_mlx_model_for_mode() -> Tuple[str, str]:
    """
    Get the appropriate MLX model based on current mode.

    Returns:
        Tuple of (model_name, tool_parser)
    """
    lite_mode, reason = should_use_lite_mode()
    config = get_lite_mode_config()

    if lite_mode and config.get("use_lite_model", True):
        # Llama uses llama tool parser
        return MLX_LITE_MODEL, "llama"
    else:
        # Qwen uses qwen tool parser
        return MLX_FULL_MODEL, "qwen"


class ServiceStatus(Enum):
    """Status of a service."""
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    NOT_RUNNING = "not_running"
    UNKNOWN = "unknown"


@dataclass
class ServiceHealth:
    """Health status of a single service."""
    name: str
    status: ServiceStatus
    port: Optional[int] = None
    message: str = ""
    response_time_ms: Optional[float] = None
    is_critical: bool = False  # If True, ecosystem can't function without it


@dataclass
class EcosystemHealth:
    """Overall ecosystem health status."""
    timestamp: str
    overall_status: ServiceStatus
    services: Dict[str, ServiceHealth]
    warnings: List[str]
    recommendations: List[str]

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "overall_status": self.overall_status.value,
            "services": {
                name: {
                    "status": svc.status.value,
                    "port": svc.port,
                    "message": svc.message,
                    "response_time_ms": svc.response_time_ms,
                    "is_critical": svc.is_critical,
                }
                for name, svc in self.services.items()
            },
            "warnings": self.warnings,
            "recommendations": self.recommendations,
        }


# =============================================================================
# SERVICE CHECKS
# =============================================================================

def check_tcp_port(host: str, port: int, timeout: float = HEALTH_CHECK_TIMEOUT) -> Tuple[bool, float]:
    """Check if a TCP port is open. Returns (is_open, response_time_ms)."""
    start = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        elapsed = (time.time() - start) * 1000
        return result == 0, elapsed
    except Exception:
        return False, 0


def check_http_endpoint(url: str, timeout: float = HEALTH_CHECK_TIMEOUT) -> Tuple[bool, float, str]:
    """Check if an HTTP endpoint responds. Returns (is_healthy, response_time_ms, message)."""
    start = time.time()
    try:
        import urllib.request
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            elapsed = (time.time() - start) * 1000
            data = response.read().decode()
            return response.status == 200, elapsed, data[:200]
    except Exception as e:
        return False, 0, str(e)[:100]


def check_docker_container_health(container_name: str) -> Optional[str]:
    """Check Docker container health status directly. Returns 'healthy', 'unhealthy', or None."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_name],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            status = result.stdout.strip()
            if status in ("healthy", "unhealthy", "starting"):
                return status
    except Exception as e:
        print(f"[WARN] Docker container health check failed: {e}")
    return None


def check_redis() -> ServiceHealth:
    """Check Redis health.

    Docker stack: context-dna (container: context-dna-redis, port 6379)
    No auth by default (appendonly mode).

    Uses 127.0.0.1 (not localhost) to avoid IPv6 resolution issues on macOS
    where Docker port mappings bind to IPv4 only.
    Fallback: checks Docker container health if TCP port check fails.
    """
    # Try 127.0.0.1 first (avoids macOS localhost->::1 IPv6 resolution)
    is_open, response_time = check_tcp_port("127.0.0.1", 6379, timeout=2)

    if is_open:
        # First, check Docker health status (most reliable)
        docker_health = check_docker_container_health("context-dna-redis")
        if docker_health == "healthy":
            return ServiceHealth(
                name="Redis",
                status=ServiceStatus.HEALTHY,
                port=6379,
                message="Running (Docker healthy)",
                response_time_ms=response_time,
                is_critical=False,
            )

        # Try direct ping as fallback
        try:
            import redis
            client = redis.Redis(
                host="127.0.0.1",
                port=6379,
                socket_timeout=HEALTH_CHECK_TIMEOUT
            )
            client.ping()
            return ServiceHealth(
                name="Redis",
                status=ServiceStatus.HEALTHY,
                port=6379,
                message="Ping successful",
                response_time_ms=response_time,
                is_critical=False,
            )
        except Exception as e:
            if docker_health == "healthy":
                return ServiceHealth(
                    name="Redis",
                    status=ServiceStatus.HEALTHY,
                    port=6379,
                    message="Running (Docker healthy, auth config mismatch)",
                    response_time_ms=response_time,
                    is_critical=False,
                )
            return ServiceHealth(
                name="Redis",
                status=ServiceStatus.UNHEALTHY,
                port=6379,
                message=str(e)[:100],
                response_time_ms=response_time,
                is_critical=False,
            )

    # TCP port check failed -- fallback: check Docker container health directly
    docker_health = check_docker_container_health("context-dna-redis")
    if docker_health == "healthy":
        return ServiceHealth(
            name="Redis",
            status=ServiceStatus.HEALTHY,
            port=6379,
            message="Running (Docker healthy, TCP probe failed)",
            response_time_ms=None,
            is_critical=False,
        )

    return ServiceHealth(
        name="Redis",
        status=ServiceStatus.NOT_RUNNING,
        port=6379,
        message="Port not open and container not healthy",
        is_critical=False,
    )


def check_postgresql() -> ServiceHealth:
    """Check PostgreSQL health.

    Docker stack: context-dna (container: context-dna-postgres, port 5432)
    Two databases on same server: context_dna (observability) + contextdna (hierarchy/profiles)
    Old stacks (contextdna, acontext-server) are orphaned — do NOT check those.
    """
    is_open, response_time = check_tcp_port("127.0.0.1", 5432)

    if is_open:
        # Check Docker health status (most reliable)
        docker_health = check_docker_container_health("context-dna-postgres")
        if docker_health == "healthy":
            return ServiceHealth(
                name="PostgreSQL",
                status=ServiceStatus.HEALTHY,
                port=5432,
                message="Running (Docker healthy)",
                response_time_ms=response_time,
                is_critical=False,
            )

        # Try direct connection as fallback
        try:
            import psycopg2
            conn = psycopg2.connect(
                host="127.0.0.1",
                port=5432,
                user=os.environ.get("DATABASE_USER", "context_dna"),
                password=os.environ.get("DATABASE_PASSWORD", "context_dna_dev"),
                database=os.environ.get("DATABASE_NAME", "context_dna"),
                connect_timeout=HEALTH_CHECK_TIMEOUT
            )
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            conn.close()
            return ServiceHealth(
                name="PostgreSQL",
                status=ServiceStatus.HEALTHY,
                port=5432,
                message="Query successful",
                response_time_ms=response_time,
                is_critical=False,
            )
        except ImportError:
            if docker_health == "healthy":
                return ServiceHealth(
                    name="PostgreSQL",
                    status=ServiceStatus.HEALTHY,
                    port=5432,
                    message="Running (Docker healthy, psycopg2 not installed)",
                    response_time_ms=response_time,
                    is_critical=False,
                )
            return ServiceHealth(
                name="PostgreSQL",
                status=ServiceStatus.UNKNOWN,
                port=5432,
                message="psycopg2 not installed",
                is_critical=False,
            )
        except Exception as e:
            if docker_health == "healthy":
                return ServiceHealth(
                    name="PostgreSQL",
                    status=ServiceStatus.HEALTHY,
                    port=5432,
                    message="Running (Docker healthy, auth config mismatch)",
                    response_time_ms=response_time,
                    is_critical=False,
                )
            return ServiceHealth(
                name="PostgreSQL",
                status=ServiceStatus.UNHEALTHY,
                port=5432,
                message=str(e)[:100],
                response_time_ms=response_time,
                is_critical=False,
            )

    return ServiceHealth(
        name="PostgreSQL",
        status=ServiceStatus.NOT_RUNNING,
        port=5432,
        message="Port not open",
        is_critical=False,
    )


def check_dual_postgresql() -> Dict[str, any]:
    """Check health of BOTH PostgreSQL instances in dual-PG architecture.

    ARCHITECTURE (2026-02-05):
    ==========================
    Stack 1: context-dna (OUR infrastructure)
      - Container: context-dna-postgres (port 5432)
      - Databases: context_dna + contextdna
      - Used by: unified_sync.py, lite_scheduler, agent_service

    Stack 2: contextdna (Acontext upstream)
      - Container: contextdna-pg (port 15432)
      - Database: acontext (GHCR image schema)
      - Used by: Acontext GHCR images (contextdna-api, contextdna-core)
      - OPTIONAL: Only needed when running full Acontext stack

    Returns dict with status of both instances for monitoring dashboards.
    """
    results = {
        "timestamp": datetime.now().isoformat(),
        "architecture": "dual-pg",
        "instances": {},
        "summary": {
            "total": 2,
            "healthy": 0,
            "optional_down": 0,
            "critical_down": 0,
        }
    }

    # =========================================================================
    # Check context-dna-postgres (5432) - PRIMARY, REQUIRED
    # =========================================================================
    is_open, response_time = check_tcp_port("127.0.0.1", 5432)

    if is_open:
        docker_health = check_docker_container_health("context-dna-postgres")

        # Try to get database list for richer info
        databases = []
        try:
            import psycopg2
            conn = psycopg2.connect(
                host="127.0.0.1",
                port=5432,
                user=os.environ.get("DATABASE_USER", "context_dna"),
                password=os.environ.get("DATABASE_PASSWORD", "context_dna_dev"),
                database="postgres",  # Connect to default to list DBs
                connect_timeout=HEALTH_CHECK_TIMEOUT
            )
            cursor = conn.cursor()
            cursor.execute("SELECT datname FROM pg_database WHERE datistemplate = false")
            databases = [row[0] for row in cursor.fetchall()]
            conn.close()
        except Exception:
            pass  # Will use Docker health as fallback

        if docker_health == "healthy" or databases:
            results["instances"]["context_dna_postgres"] = {
                "port": 5432,
                "container": "context-dna-postgres",
                "status": "healthy",
                "docker_health": docker_health,
                "response_time_ms": response_time,
                "databases": databases or ["context_dna", "contextdna"],
                "role": "primary",
                "required": True,
                "message": f"Running ({len(databases) if databases else 2} databases)"
            }
            results["summary"]["healthy"] += 1
        else:
            results["instances"]["context_dna_postgres"] = {
                "port": 5432,
                "container": "context-dna-postgres",
                "status": "unhealthy",
                "docker_health": docker_health,
                "response_time_ms": response_time,
                "role": "primary",
                "required": True,
                "message": "Port open but health check failed"
            }
            results["summary"]["critical_down"] += 1
    else:
        results["instances"]["context_dna_postgres"] = {
            "port": 5432,
            "container": "context-dna-postgres",
            "status": "down",
            "role": "primary",
            "required": True,
            "message": "Port not open - run: ./scripts/context-dna up"
        }
        results["summary"]["critical_down"] += 1

    # =========================================================================
    # Check contextdna-pg (15432) - OPTIONAL, Acontext stack
    # =========================================================================
    is_open_acontext, response_time_acontext = check_tcp_port("127.0.0.1", 15432)

    if is_open_acontext:
        docker_health_acontext = check_docker_container_health("contextdna-pg")

        # Try to verify acontext database exists
        acontext_db_exists = False
        try:
            import psycopg2
            conn = psycopg2.connect(
                host="127.0.0.1",
                port=15432,
                user=os.environ.get("ACONTEXT_DB_USER", "acontext"),
                password=os.environ.get("ACONTEXT_DB_PASSWORD", ""),
                database="acontext",
                connect_timeout=HEALTH_CHECK_TIMEOUT
            )
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            acontext_db_exists = True
            conn.close()
        except Exception:
            pass

        if docker_health_acontext == "healthy" or acontext_db_exists:
            results["instances"]["acontext_postgres"] = {
                "port": 15432,
                "container": "contextdna-pg",
                "status": "healthy",
                "docker_health": docker_health_acontext,
                "response_time_ms": response_time_acontext,
                "databases": ["acontext"],
                "role": "acontext_upstream",
                "required": False,
                "message": "Acontext GHCR stack running"
            }
            results["summary"]["healthy"] += 1
        else:
            results["instances"]["acontext_postgres"] = {
                "port": 15432,
                "container": "contextdna-pg",
                "status": "unhealthy",
                "docker_health": docker_health_acontext,
                "response_time_ms": response_time_acontext,
                "role": "acontext_upstream",
                "required": False,
                "message": "Port open but health check failed"
            }
            results["summary"]["optional_down"] += 1
    else:
        # Port 15432 not open - this is OPTIONAL, not an error
        results["instances"]["acontext_postgres"] = {
            "port": 15432,
            "container": "contextdna-pg",
            "status": "optional_down",
            "role": "acontext_upstream",
            "required": False,
            "message": "Acontext stack not running (optional)"
        }
        results["summary"]["optional_down"] += 1

    # =========================================================================
    # Determine overall status
    # =========================================================================
    if results["summary"]["critical_down"] > 0:
        results["overall_status"] = "critical"
        results["overall_message"] = "Primary PostgreSQL (5432) is DOWN"
    elif results["summary"]["healthy"] == 2:
        results["overall_status"] = "full"
        results["overall_message"] = "Both PostgreSQL instances healthy"
    elif results["summary"]["healthy"] == 1 and results["summary"]["optional_down"] == 1:
        results["overall_status"] = "partial"
        results["overall_message"] = "Primary healthy, Acontext stack not running"
    else:
        results["overall_status"] = "degraded"
        results["overall_message"] = "PostgreSQL health degraded"

    return results


def check_rabbitmq() -> ServiceHealth:
    """Check RabbitMQ health."""
    is_open, response_time = check_tcp_port("127.0.0.1", 15672)  # Management UI port
    if not is_open:
        is_open, response_time = check_tcp_port("127.0.0.1", 5672)  # AMQP port

    if is_open:
        return ServiceHealth(
            name="RabbitMQ",
            status=ServiceStatus.HEALTHY,
            port=15672,
            message="Port open",
            response_time_ms=response_time,
            is_critical=False,
        )

    return ServiceHealth(
        name="RabbitMQ",
        status=ServiceStatus.NOT_RUNNING,
        port=15672,
        message="Port not open",
        is_critical=False,
    )


def check_agent_service() -> ServiceHealth:
    """Check the Context DNA Agent Service (FastAPI on 8080)."""
    is_healthy, response_time, message = check_http_endpoint("http://127.0.0.1:8080/health")

    if is_healthy:
        return ServiceHealth(
            name="Agent Service",
            status=ServiceStatus.HEALTHY,
            port=8080,
            message="API healthy",
            response_time_ms=response_time,
            is_critical=True,  # Needed for dashboard
        )

    return ServiceHealth(
        name="Agent Service",
        status=ServiceStatus.NOT_RUNNING,
        port=8080,
        message=message,
        is_critical=True,
    )


def check_dashboard() -> ServiceHealth:
    """Check the Context DNA Dashboard (Next.js on 3001)."""
    is_open, response_time = check_tcp_port("127.0.0.1", 3001)

    if is_open:
        return ServiceHealth(
            name="Dashboard",
            status=ServiceStatus.HEALTHY,
            port=3001,
            message="Running",
            response_time_ms=response_time,
            is_critical=False,
        )

    return ServiceHealth(
        name="Dashboard",
        status=ServiceStatus.NOT_RUNNING,
        port=3001,
        message="Not running",
        is_critical=False,
    )


def check_memory_api() -> ServiceHealth:
    """Check the Memory API (Python on 3456).

    NOTE: This Python API is DEPRECATED. The Go API (contextdna-api:8029)
    handles all primary Context DNA operations. The Python API has outdated
    code that doesn't match current brain.py exports.

    Status: OPTIONAL - system functions fully without this service.
    """
    is_healthy, response_time, message = check_http_endpoint("http://localhost:3456/api/health")

    if is_healthy:
        return ServiceHealth(
            name="Memory API (DEPRECATED)",
            status=ServiceStatus.HEALTHY,
            port=3456,
            message="Python API healthy (use Go API on 8029 instead)",
            response_time_ms=response_time,
            is_critical=False,
        )

    # Return HEALTHY since this service is intentionally deprecated
    # The Go API (8029) handles all memory operations now
    return ServiceHealth(
        name="Memory API (Deprecated → Go API)",
        status=ServiceStatus.HEALTHY,
        port=3456,
        message="Superseded by Go API (8029) - all operations handled there",
        is_critical=False,
    )


def check_websocket_connection(url: str, timeout: float = 3.0) -> Tuple[bool, str]:
    """
    Check if a WebSocket endpoint can accept connections.
    Returns (is_connectable, message).
    """
    try:
        import websockets
        import asyncio

        async def _test_ws():
            try:
                async with websockets.connect(url, close_timeout=timeout) as ws:
                    return True, "WebSocket connected"
            except Exception as e:
                return False, str(e)[:100]

        return asyncio.run(_test_ws())
    except ImportError:
        # Fallback: just check if TCP port is open
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            port = parsed.port or 80
            is_open, _ = check_tcp_port(parsed.hostname or "localhost", port, timeout)
            return is_open, "TCP port open (websockets not installed)"
        except Exception as e:
            return False, str(e)[:100]
    except Exception as e:
        return False, str(e)[:100]


def check_synaptic_chat() -> ServiceHealth:
    """
    Check the REAL Synaptic Chat Server (port 8888).

    THIS IS CRITICAL - Port 8888 is where Synaptic's real LLM voice lives.
    Without this service, only memory databases respond - no reasoning.

    The chat server provides:
    - Real LLM-generated responses via MLX/Qwen
    - WebSocket chat at ws://localhost:8888/chat
    - Voice recording with VAD and speaker verification
    - All 5 memory sources injected into responses
    """
    is_healthy, response_time, message = check_http_endpoint("http://localhost:8888/health")

    if is_healthy:
        # ALSO verify WebSocket endpoint is working (the real voice channel)
        ws_healthy, ws_message = check_websocket_connection("ws://localhost:8888/chat")
        if ws_healthy:
            return ServiceHealth(
                name="Synaptic Chat Server",
                status=ServiceStatus.HEALTHY,
                port=8888,
                message="Real Synaptic voice ONLINE (HTTP + WebSocket)",
                response_time_ms=response_time,
                is_critical=True,  # CRITICAL - this is where real Synaptic lives
            )
        else:
            return ServiceHealth(
                name="Synaptic Chat Server",
                status=ServiceStatus.UNHEALTHY,
                port=8888,
                message=f"HTTP OK but WebSocket FAILED: {ws_message}",
                response_time_ms=response_time,
                is_critical=True,
            )

    return ServiceHealth(
        name="Synaptic Chat Server",
        status=ServiceStatus.NOT_RUNNING,
        port=8888,
        message=f"⚠️ SYNAPTIC OFFLINE: {message}",
        is_critical=True,
    )


def check_mlx_api() -> ServiceHealth:
    """Check the local LLM (mlx_lm.server on port 5044) - Synaptic's brain."""
    # mlx_lm.server uses OpenAI-compatible /v1/models endpoint
    llm_port = 5044
    is_healthy, response_time, message = check_http_endpoint(f"http://127.0.0.1:{llm_port}/v1/models")

    if is_healthy:
        # Parse model name from response if possible
        model_name = "Local LLM"
        try:
            import requests
            resp = requests.get(f"http://127.0.0.1:{llm_port}/v1/models", timeout=2)
            if resp.ok:
                data = resp.json()
                if data.get("data"):
                    model_name = data["data"][0].get("id", "Local LLM")
        except Exception as e:
            print(f"[WARN] LLM model info fetch failed: {e}")

        return ServiceHealth(
            name="Local LLM",
            status=ServiceStatus.HEALTHY,
            port=llm_port,
            message=f"Ready: {model_name}",
            response_time_ms=response_time,
            is_critical=False,  # Chat server can fallback to subprocess
        )

    return ServiceHealth(
        name="Local LLM",
        status=ServiceStatus.NOT_RUNNING,
        port=llm_port,
        message=message,
        is_critical=False,
    )


def check_injection_files() -> ServiceHealth:
    """Check if injection files are being written (fallback works)."""
    injection_file = Path(__file__).parent / ".injection_latest.json"

    if injection_file.exists():
        mtime = injection_file.stat().st_mtime
        age_seconds = time.time() - mtime
        age_minutes = age_seconds / 60

        if age_minutes < 60:  # Active in last hour
            return ServiceHealth(
                name="Injection Files",
                status=ServiceStatus.HEALTHY,
                message=f"Last injection {age_minutes:.0f}m ago",
                is_critical=True,  # Core functionality
            )
        else:
            return ServiceHealth(
                name="Injection Files",
                status=ServiceStatus.UNHEALTHY,
                message=f"Last injection {age_minutes:.0f}m ago (stale)",
                is_critical=True,
            )

    return ServiceHealth(
        name="Injection Files",
        status=ServiceStatus.NOT_RUNNING,
        message="No injection file found",
        is_critical=True,
    )


# =============================================================================
# AGGREGATED HEALTH CHECKS
# =============================================================================

def quick_health_check() -> Dict[str, bool]:
    """Quick health check returning simple booleans."""
    redis = check_redis()
    postgres = check_postgresql()
    agent = check_agent_service()
    files = check_injection_files()

    return {
        "redis": redis.status == ServiceStatus.HEALTHY,
        "postgresql": postgres.status == ServiceStatus.HEALTHY,
        "agent_service": agent.status == ServiceStatus.HEALTHY,
        "injection_files": files.status == ServiceStatus.HEALTHY,
        "containers_recommended": not (redis.status == ServiceStatus.HEALTHY),
    }


def full_health_check() -> EcosystemHealth:
    """Full health check with all services."""
    services = {
        "redis": check_redis(),
        "postgresql": check_postgresql(),
        "rabbitmq": check_rabbitmq(),
        "agent_service": check_agent_service(),
        # memory_api REMOVED - deprecated Python API replaced by Go API (8029)
        "dashboard": check_dashboard(),
        "injection_files": check_injection_files(),
        "synaptic_chat": check_synaptic_chat(),  # CRITICAL - Real Synaptic voice
        "mlx_api": check_mlx_api(),  # Synaptic's brain
    }

    # Check dual PostgreSQL architecture (5432 + 15432)
    dual_pg = check_dual_postgresql()

    warnings = []
    recommendations = []

    # Add system health (macOS resources)
    try:
        from memory.system_health_monitor import get_system_health
        sys_health = get_system_health()

        # Add system-level warnings
        if sys_health.memory_pressure == "critical":
            warnings.append(f"CRITICAL: System memory pressure - {sys_health.free_memory_gb:.1f}GB free")
        elif sys_health.memory_pressure == "warning":
            warnings.append(f"Low system memory - {sys_health.free_memory_gb:.1f}GB free")

        # Add system recommendations
        for rec in sys_health.recommendations:
            if rec.startswith("❌") or rec.startswith("⚠️"):
                recommendations.append(rec)
    except ImportError:
        pass  # System monitor not available

    # Check critical services
    critical_unhealthy = [
        name for name, svc in services.items()
        if svc.is_critical and svc.status != ServiceStatus.HEALTHY
    ]

    # Determine overall status
    if critical_unhealthy:
        overall = ServiceStatus.UNHEALTHY
        warnings.append(f"Critical services unhealthy: {', '.join(critical_unhealthy)}")
    elif any(svc.status != ServiceStatus.HEALTHY for svc in services.values()):
        overall = ServiceStatus.HEALTHY  # Non-critical services can be down
    else:
        overall = ServiceStatus.HEALTHY

    # Add recommendations
    if services["redis"].status != ServiceStatus.HEALTHY:
        recommendations.append("Start containers: ./scripts/context-dna up")
    if services["agent_service"].status != ServiceStatus.HEALTHY:
        recommendations.append("Start agent service: ./scripts/start-helper-agent.sh")
    if services["dashboard"].status != ServiceStatus.HEALTHY:
        recommendations.append("Start dashboard: cd admin.contextdna.io && pnpm dev")
    if services["synaptic_chat"].status != ServiceStatus.HEALTHY:
        recommendations.append("⚠️ START SYNAPTIC: .venv/bin/python3 memory/synaptic_chat_server.py")
    if services["mlx_api"].status != ServiceStatus.HEALTHY:
        recommendations.append("Start local LLM: ./scripts/start-llm.sh")

    # Dual PostgreSQL architecture warnings/recommendations
    if dual_pg["overall_status"] == "critical":
        warnings.append(f"CRITICAL: {dual_pg['overall_message']}")
    elif dual_pg["overall_status"] == "degraded":
        warnings.append(f"PostgreSQL degraded: {dual_pg['overall_message']}")

    # Add Acontext stack recommendation only if user might need it
    if dual_pg["instances"].get("acontext_postgres", {}).get("status") == "optional_down":
        # Don't add recommendation by default - Acontext stack is optional
        pass  # User can start with: cd context-dna/infra && docker compose up -d

    result = EcosystemHealth(
        timestamp=datetime.now().isoformat(),
        overall_status=overall,
        services=services,
        warnings=warnings,
        recommendations=recommendations,
    )

    # Emit to Synaptic Observatory for real-time awareness
    emit_to_observatory(result)

    return result


def is_ecosystem_ready() -> bool:
    """Quick check if ecosystem is ready for full functionality."""
    health = quick_health_check()
    return health["injection_files"] and health["agent_service"]


def emit_to_observatory(health: EcosystemHealth):
    """
    Emit health status to Synaptic Observatory.

    This bridges ecosystem_health.py → synaptic_observatory.py
    so Synaptic has real-time health awareness.
    """
    try:
        # Update the system awareness JSON file
        awareness_path = Path(__file__).parent / ".synaptic_system_awareness.json"
        awareness_data = {
            "timestamp": health.timestamp,
            "overall_status": health.overall_status.value,
            "services": {
                name: {
                    "status": svc.status.value,
                    "port": svc.port,
                    "healthy": svc.status == ServiceStatus.HEALTHY,
                }
                for name, svc in health.services.items()
            },
            "healthy_count": sum(1 for svc in health.services.values() if svc.status == ServiceStatus.HEALTHY),
            "unhealthy_count": sum(1 for svc in health.services.values() if svc.status != ServiceStatus.HEALTHY),
            "warnings": health.warnings,
            "recommendations": health.recommendations,
            "emitted_to_observatory": True,
        }
        awareness_path.write_text(json.dumps(awareness_data, indent=2))

        # Log for tracing
        logger.debug(f"Emitted to Observatory: {awareness_data['overall_status']}")

    except Exception as e:
        logger.warning(f"Failed to emit to Observatory: {e}")


# =============================================================================
# AUTO-RECOVERY WITH PROACTIVE NOTIFICATIONS
# =============================================================================

def send_macos_notification(title: str, message: str, sound: bool = True, category: str = None):
    """
    Send a native macOS notification using osascript.
    This allows Synaptic to proactively alert Aaron when services restart.

    Now checks notification_manager preferences before sending.
    """
    # Check central notification preferences
    try:
        from memory.notification_manager import should_notify, NotificationCategory

        # Auto-detect category if not provided
        if category is None:
            title_lower = title.lower()
            if "restart" in title_lower or "recovery" in title_lower:
                category = "auto_recovery_success"
            elif "llm" in title_lower or "vllm" in title_lower:
                category = "llm_status"
            elif "synaptic" in title_lower:
                category = "voice_status"
            elif "docker" in title_lower or "container" in title_lower:
                category = "service_down"
            elif "agent" in title_lower:
                category = "service_down"
            else:
                category = "info"

        try:
            cat_enum = NotificationCategory(category)
        except ValueError:
            cat_enum = NotificationCategory.INFO

        if not should_notify(cat_enum):
            return  # Blocked by preferences

    except ImportError:
        pass  # Module not available, send anyway
    except Exception:
        pass  # Error checking, send anyway

    try:
        sound_part = 'with sound name "Submarine"' if sound else ''
        script = f'display notification "{message}" with title "{title}" {sound_part}'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass  # Don't fail if notification fails


def start_missing_services(dry_run: bool = False) -> List[str]:
    """Attempt to start missing services. Returns list of actions taken."""
    actions = []
    health = full_health_check()
    project_root = Path(__file__).parent.parent

    # Start containers if Redis/PostgreSQL down
    if (health.services["redis"].status != ServiceStatus.HEALTHY or
        health.services["postgresql"].status != ServiceStatus.HEALTHY):

        cmd = "./scripts/context-dna up"
        if dry_run:
            actions.append(f"[DRY RUN] Would run: {cmd}")
        else:
            try:
                subprocess.run(cmd.split(), cwd=str(project_root), timeout=60)
                actions.append(f"Started containers: {cmd}")
                # PROACTIVE ALERT
                send_macos_notification(
                    "🐳 Synaptic Alert",
                    "Docker containers restarted (Redis/PostgreSQL)"
                )
            except Exception as e:
                actions.append(f"Failed to start containers: {e}")

    # =========================================================================
    # CRITICAL: Auto-restart Helper Agent Service (Port 8080 - Webhook Primary)
    # =========================================================================
    if health.services["agent_service"].status != ServiceStatus.HEALTHY:
        start_script = project_root / "scripts" / "start-helper-agent.sh"

        if dry_run:
            actions.append("[DRY RUN] Would start agent_service (port 8080)")
        else:
            try:
                log_file = project_root / "logs" / "agent_service.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                cmd = f"nohup /bin/bash {start_script} >> {log_file} 2>&1 &"
                subprocess.run(cmd, shell=True, cwd=str(project_root))
                actions.append(f"🚀 STARTED Agent Service: port 8080")
                time.sleep(5)
                send_macos_notification(
                    "🚀 Agent Service RESTARTED",
                    "Helper agent back online (port 8080)",
                    sound=True
                )
            except Exception as e:
                actions.append(f"Failed to start agent_service: {e}")
                send_macos_notification(
                    "⚠️ Agent Service FAILED",
                    f"Could not restart: {str(e)[:40]}",
                    sound=True
                )

    # =========================================================================
    # CRITICAL: Auto-restart Synaptic Chat Server (Real Synaptic Voice)
    # =========================================================================
    if health.services["synaptic_chat"].status != ServiceStatus.HEALTHY:
        venv_python = project_root / ".venv" / "bin" / "python3.14"
        chat_server = project_root / "memory" / "synaptic_chat_server.py"

        if dry_run:
            actions.append(f"[DRY RUN] Would start Synaptic chat server")
        else:
            try:
                # Start in background with nohup
                log_file = project_root / "memory" / ".synaptic_chat.log"
                cmd = f"nohup {venv_python} {chat_server} >> {log_file} 2>&1 &"
                subprocess.run(cmd, shell=True, cwd=str(project_root))
                actions.append(f"🧠 STARTED REAL SYNAPTIC: {chat_server.name}")
                # Wait for startup
                time.sleep(3)
                # PROACTIVE ALERT - Critical service!
                send_macos_notification(
                    "🧠 Synaptic RESTARTED",
                    "Real Synaptic voice back online (port 8888)",
                    sound=True
                )
            except Exception as e:
                actions.append(f"Failed to start Synaptic chat server: {e}")
                send_macos_notification(
                    "⚠️ Synaptic FAILED",
                    f"Could not restart: {str(e)[:40]}",
                    sound=True
                )

    # =========================================================================
    # AUTO-RESTART Local LLM Server (mlx_lm.server) - LITE MODE AWARE
    # Graceful restart: wait for current LLM job to finish before restarting.
    # =========================================================================
    _llm_restart_deferred = False
    if health.services["mlx_api"].status != ServiceStatus.HEALTHY:
        # Wait for current LLM job to complete before restarting
        try:
            from memory.llm_priority_queue import get_queue_stats
            stats = get_queue_stats()
            if stats.get("active_priority") is not None:
                active_p = stats["active_priority"]
                actions.append(f"⏳ LLM restart deferred: P{active_p} job active (will retry next cycle)")
                _llm_restart_deferred = True
        except Exception:
            pass  # Queue module not available, proceed with restart

        mlx_venv = project_root / "context-dna" / "local_llm" / ".venv-mlx"
        start_script = project_root / "scripts" / "start-llm.sh"

        # Check lite mode configuration
        lite_mode, lite_reason = should_use_lite_mode()
        lite_config = get_lite_mode_config()

        # Skip if graceful restart deferred (LLM currently processing a job)
        if _llm_restart_deferred:
            pass  # Will retry next ecosystem_health cycle
        # Skip MLX entirely if configured
        elif lite_config.get("skip_mlx") and lite_mode:
            actions.append(f"⚡ LITE MODE: Skipping local LLM ({lite_reason})")
        elif mlx_venv.exists() and start_script.exists():
            # Check if we have enough memory
            try:
                import psutil
                mem = psutil.virtual_memory()
                available_gb = mem.available / (1024 ** 3)

                if available_gb < MLX_MINIMUM_MEMORY_GB:
                    actions.append(f"⚠️ Skipping local LLM: Only {available_gb:.1f}GB free (need {MLX_MINIMUM_MEMORY_GB}GB)")
                else:
                    if dry_run:
                        actions.append(f"[DRY RUN] Would start local LLM (mlx_lm.server)")
                    else:
                        try:
                            mode_str = "LITE" if lite_mode else "FULL"
                            log_file = project_root / "memory" / ".mlx_lm.log"
                            cmd = f"nohup {start_script} >> {log_file} 2>&1 &"
                            subprocess.run(cmd, shell=True, cwd=str(project_root))
                            actions.append(f"🧠 STARTED local LLM [{mode_str}] via start-llm.sh")
                            wait_time = 5 if lite_mode else 8
                            time.sleep(wait_time)
                            send_macos_notification(
                                f"🧠 Local LLM Started [{mode_str}]",
                                f"mlx_lm.server on port 5044",
                                sound=False
                            )
                        except Exception as e:
                            actions.append(f"Failed to start local LLM: {e}")
            except ImportError:
                # psutil not available, try anyway
                if dry_run:
                    actions.append(f"[DRY RUN] Would start local LLM (mlx_lm.server)")
                else:
                    try:
                        log_file = project_root / "memory" / ".mlx_lm.log"
                        cmd = f"nohup {start_script} >> {log_file} 2>&1 &"
                        subprocess.run(cmd, shell=True, cwd=str(project_root))
                        actions.append(f"🧠 STARTED local LLM via start-llm.sh")
                        time.sleep(8)
                        send_macos_notification(
                            "🧠 Local LLM Started",
                            f"mlx_lm.server on port 5044",
                            sound=False
                        )
                    except Exception as e:
                        actions.append(f"Failed to start local LLM: {e}")
        else:
            actions.append(f"Local LLM not installed at {mlx_venv}")

    return actions


# =============================================================================
# DAEMON MODE
# =============================================================================

# Status file that Synaptic can read for full system awareness
SYNAPTIC_AWARENESS_FILE = Path(__file__).parent / ".synaptic_system_awareness.json"


def write_synaptic_awareness(health: EcosystemHealth, actions: List[str] = None):
    """
    Write system health status to a file that Synaptic can read.

    This gives Synaptic full awareness of:
    - Which services are online/offline
    - Auto-recovery actions taken
    - Recommendations for the user
    - Overall ecosystem health
    - Webhook section health (per-destination, per-section)

    Synaptic can then communicate this naturally to Aaron.
    """
    awareness = {
        "timestamp": health.timestamp,
        "overall_status": health.overall_status.value,
        "services": {},
        "online": [],
        "offline": [],
        "warnings": health.warnings,
        "recommendations": health.recommendations,
        "recent_actions": actions or [],
    }

    for name, svc in health.services.items():
        awareness["services"][name] = {
            "status": svc.status.value,
            "port": svc.port,
            "message": svc.message,
            "is_critical": svc.is_critical,
        }
        if svc.status == ServiceStatus.HEALTHY:
            awareness["online"].append(name)
        else:
            awareness["offline"].append(name)

    # Add dual PostgreSQL architecture status for Synaptic
    try:
        dual_pg = check_dual_postgresql()
        awareness["dual_postgresql"] = {
            "overall_status": dual_pg["overall_status"],
            "overall_message": dual_pg["overall_message"],
            "instances": dual_pg["instances"],
            "summary": dual_pg["summary"],
        }
    except Exception as e:
        awareness["dual_postgresql"] = {"error": str(e)}

    # Add webhook section health for Synaptic
    try:
        from memory.webhook_section_notifications import get_dashboard_data
        webhook_data = get_dashboard_data()
        awareness["webhook_health"] = {
            "destinations": len(webhook_data.get("destinations", [])),
            "platforms": webhook_data.get("platforms", {}),
            "section_states": [
                {
                    "destination": s["destination"],
                    "section_id": s["section_id"],
                    "section_name": s["section_name"],
                    "status": s["status"],
                    "is_critical": s.get("section_critical", False),
                }
                for s in webhook_data.get("section_states", [])
                if s["status"] != "healthy"  # Only include non-healthy
            ],
            "recent_notifications": len(webhook_data.get("notification_history", [])),
        }
    except Exception as e:
        awareness["webhook_health"] = {"error": str(e)}

    # Write atomically
    temp_file = SYNAPTIC_AWARENESS_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(awareness, indent=2))
    temp_file.rename(SYNAPTIC_AWARENESS_FILE)


def get_synaptic_awareness() -> dict:
    """
    Read system awareness for Synaptic to use in responses.

    Usage in Synaptic:
        from memory.ecosystem_health import get_synaptic_awareness
        awareness = get_synaptic_awareness()
        # awareness['offline'] = ['redis', 'postgresql']
        # awareness['online'] = ['synaptic_chat', 'agent_service', ...]
    """
    if SYNAPTIC_AWARENESS_FILE.exists():
        try:
            return json.loads(SYNAPTIC_AWARENESS_FILE.read_text())
        except Exception as e:
            print(f"[WARN] Synaptic awareness file read failed: {e}")
    return {"overall_status": "unknown", "online": [], "offline": [], "services": {}}


def check_webhook_section_health() -> Dict[str, any]:
    """
    Check webhook injection health and send notifications for failures.

    This integrates with webhook_section_notifications.py to:
    - Check all destination/section combinations
    - Send macOS notifications for failures
    - Track per-destination, per-section health
    - Support user muting via dashboard
    """
    try:
        from memory.webhook_section_notifications import run_health_check_and_notify
        return run_health_check_and_notify()
    except ImportError as e:
        logger.warning(f"Webhook section notifications not available: {e}")
        return {"status": "unavailable", "error": str(e)}
    except Exception as e:
        logger.error(f"Error checking webhook section health: {e}")
        return {"status": "error", "error": str(e)}


def run_daemon(auto_recover: bool = False, interval: int = DAEMON_INTERVAL):
    """Run continuous health monitoring with Synaptic awareness."""
    print(f"Starting Context DNA Ecosystem Health Monitor")
    print(f"Interval: {interval}s | Auto-recover: {auto_recover}")
    print(f"Synaptic awareness file: {SYNAPTIC_AWARENESS_FILE}")
    print(f"Webhook notifications: enabled")
    print("-" * 50)

    while True:
        try:
            health = full_health_check()
            actions = []

            # Print status
            status_icon = "✅" if health.overall_status == ServiceStatus.HEALTHY else "❌"
            print(f"\n[{health.timestamp}] {status_icon} Overall: {health.overall_status.value}")

            for name, svc in health.services.items():
                icon = "✅" if svc.status == ServiceStatus.HEALTHY else "❌"
                print(f"  {icon} {name}: {svc.status.value} {svc.message}")

            if health.warnings:
                print(f"  ⚠️  Warnings: {', '.join(health.warnings)}")

            # =========================================================
            # WEBHOOK SECTION HEALTH CHECK (per-destination, per-section)
            # =========================================================
            webhook_health = check_webhook_section_health()
            if webhook_health.get("status") != "unavailable":
                overall_webhook = webhook_health.get("overall_status", "unknown")
                eighth_intel = webhook_health.get("eighth_intelligence_status", "unknown")
                notifs_sent = webhook_health.get("notifications_sent", [])

                webhook_icon = "✅" if overall_webhook == "healthy" else "⚠️"
                print(f"  {webhook_icon} Webhook Sections: {overall_webhook}")
                print(f"      8th Intelligence: {eighth_intel}")

                if notifs_sent:
                    print(f"      📢 Notifications sent: {len(notifs_sent)}")
                    for notif in notifs_sent:
                        print(f"         → {notif['destination']}/Section {notif['section_id']}")

            # Auto-recover if enabled
            actions = []
            if auto_recover and health.overall_status != ServiceStatus.HEALTHY:
                print("  🔧 Attempting auto-recovery...")
                actions = start_missing_services()
                for action in actions:
                    print(f"    → {action}")

            # Write awareness file for Synaptic
            write_synaptic_awareness(health, actions)
            print(f"  📝 Updated Synaptic awareness file")

            time.sleep(interval)

        except KeyboardInterrupt:
            print("\nStopping health monitor...")
            break
        except Exception as e:
            print(f"Error in health check: {e}")
            time.sleep(interval)


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    if "--daemon" in sys.argv:
        auto_recover = "--auto-recover" in sys.argv
        run_daemon(auto_recover=auto_recover)
    elif "--json" in sys.argv:
        health = full_health_check()
        print(json.dumps(health.to_dict(), indent=2))
    elif "--quick" in sys.argv:
        health = quick_health_check()
        print(json.dumps(health, indent=2))
    elif "--dual-pg" in sys.argv:
        # Check dual PostgreSQL architecture status
        dual_pg = check_dual_postgresql()

        print("=" * 60)
        print("  DUAL POSTGRESQL ARCHITECTURE STATUS")
        print("=" * 60)

        # Overall status
        status_icon = {"full": "✅", "partial": "⚡", "critical": "❌", "degraded": "⚠️"}.get(dual_pg["overall_status"], "❓")
        print(f"\n{status_icon} Overall: {dual_pg['overall_status'].upper()}")
        print(f"   {dual_pg['overall_message']}")

        print("\n📊 Instances:")
        for name, inst in dual_pg["instances"].items():
            status = inst.get("status", "unknown")
            icon = {"healthy": "✅", "unhealthy": "⚠️", "down": "❌", "optional_down": "⚡"}.get(status, "❓")
            port = inst.get("port", "?")
            container = inst.get("container", "unknown")
            required = "REQUIRED" if inst.get("required") else "optional"
            role = inst.get("role", "unknown")

            print(f"\n  {icon} {name} (port {port})")
            print(f"      Container: {container}")
            print(f"      Role: {role} [{required}]")
            print(f"      Status: {status}")
            if inst.get("databases"):
                print(f"      Databases: {', '.join(inst['databases'])}")
            if inst.get("message"):
                print(f"      Message: {inst['message']}")

        print("\n" + "-" * 60)
        print("Summary:")
        print(f"  Total instances: {dual_pg['summary']['total']}")
        print(f"  Healthy: {dual_pg['summary']['healthy']}")
        print(f"  Optional down: {dual_pg['summary']['optional_down']}")
        print(f"  Critical down: {dual_pg['summary']['critical_down']}")
        print("=" * 60)

        if "--json" in sys.argv:
            print("\nJSON Output:")
            print(json.dumps(dual_pg, indent=2))

    elif "--lite-mode" in sys.argv:
        # Lite mode management
        if "--enable" in sys.argv:
            skip_mlx = "--skip-mlx" in sys.argv
            config = set_lite_mode(enabled=True, skip_mlx=skip_mlx)
            print("⚡ LITE MODE ENABLED")
            print(f"   Skip MLX: {config['skip_mlx']}")
            print(f"   Config saved to: {LITE_MODE_CONFIG_FILE}")
        elif "--disable" in sys.argv:
            config = set_lite_mode(enabled=False)
            print("🚀 FULL MODE ENABLED")
            print(f"   Config saved to: {LITE_MODE_CONFIG_FILE}")
        else:
            # Show current status
            config = get_lite_mode_config()
            lite_mode, reason = should_use_lite_mode()
            model, parser = get_mlx_model_for_mode()

            print("=" * 60)
            print("  LITE MODE CONFIGURATION")
            print("=" * 60)
            print(f"\n  Enabled (manual): {config.get('enabled', False)}")
            print(f"  Auto-detect: {config.get('auto_detect', True)}")
            print(f"  Skip MLX: {config.get('skip_mlx', False)}")
            print(f"  Use lite model: {config.get('use_lite_model', True)}")
            print(f"  Memory threshold: {config.get('memory_threshold_gb', LITE_MODE_MEMORY_THRESHOLD_GB)}GB")
            print(f"\n  Current status: {'⚡ LITE' if lite_mode else '🚀 FULL'} ({reason})")
            print(f"  MLX model: {model}")
            print(f"  Tool parser: {parser}")
            print("=" * 60)
            print("\nUsage:")
            print("  --lite-mode --enable       Enable lite mode")
            print("  --lite-mode --enable --skip-mlx  Enable + skip MLX entirely")
            print("  --lite-mode --disable      Disable lite mode (use full)")
            print("=" * 60)
    else:
        # Default: human-readable output
        health = full_health_check()

        print("=" * 60)
        print("  CONTEXT DNA ECOSYSTEM HEALTH")
        print("=" * 60)

        status_icon = "✅" if health.overall_status == ServiceStatus.HEALTHY else "❌"
        print(f"\nOverall Status: {status_icon} {health.overall_status.value.upper()}")
        print(f"Timestamp: {health.timestamp}")

        print("\n📊 Services:")
        for name, svc in health.services.items():
            icon = "✅" if svc.status == ServiceStatus.HEALTHY else ("⚠️" if svc.status == ServiceStatus.UNKNOWN else "❌")
            critical = " [CRITICAL]" if svc.is_critical else ""
            port = f":{svc.port}" if svc.port else ""
            print(f"  {icon} {name}{port}{critical}: {svc.status.value}")
            if svc.message:
                print(f"      {svc.message}")

        # Show dual PostgreSQL architecture status
        dual_pg = check_dual_postgresql()
        pg_icon = {"full": "✅", "partial": "⚡", "critical": "❌", "degraded": "⚠️"}.get(dual_pg["overall_status"], "❓")
        print(f"\n🐘 Dual PostgreSQL: {pg_icon} {dual_pg['overall_status'].upper()}")
        for name, inst in dual_pg["instances"].items():
            status = inst.get("status", "unknown")
            icon = {"healthy": "✅", "unhealthy": "⚠️", "down": "❌", "optional_down": "⚡"}.get(status, "❓")
            port = inst.get("port", "?")
            print(f"    {icon} {name}:{port} - {inst.get('message', status)}")

        if health.warnings:
            print("\n⚠️  Warnings:")
            for w in health.warnings:
                print(f"  • {w}")

        if health.recommendations:
            print("\n💡 Recommendations:")
            for r in health.recommendations:
                print(f"  • {r}")

        # Show lite mode status
        lite_mode, lite_reason = should_use_lite_mode()
        mode_icon = "⚡" if lite_mode else "🚀"
        print(f"\n{mode_icon} Mode: {'LITE' if lite_mode else 'FULL'} ({lite_reason})")

        print("\n" + "=" * 60)
        print("Run with --daemon for continuous monitoring")
        print("Run with --auto-recover to auto-start services")
        print("Run with --lite-mode to manage lite mode")
        print("Run with --dual-pg for detailed PostgreSQL status")
        print("=" * 60)
