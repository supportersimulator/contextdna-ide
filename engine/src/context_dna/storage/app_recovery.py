"""
App Recovery Configuration System
=================================

Captures and restores configurations for various apps used by Context DNA.
Local LLM can use this data to assist in recovery configuration attempts.

Supported Apps:
- Docker (container_runtime) - via docker_recovery.py
- Ollama (llm_runtime) - local LLM inference
- PostgreSQL (database) - via container or native
- Redis (database) - caching
- SeaweedFS (database) - blob storage
- LiveKit (ai_service) - real-time voice
- Whisper (ai_service) - speech-to-text

Usage:
    from context_dna.storage.app_recovery import AppRecoveryManager

    manager = AppRecoveryManager(db_path)

    # Discover and capture all running apps
    manager.capture_all_running_apps()

    # Get recovery config for specific app
    config = manager.get_app_config("ollama")

    # Attempt recovery
    success = manager.recover_app("ollama")

CLI:
    python -m context_dna.storage.app_recovery discover
    python -m context_dna.storage.app_recovery capture ollama
    python -m context_dna.storage.app_recovery recover ollama
    python -m context_dna.storage.app_recovery status
"""

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple


# =============================================================================
# APP DEFINITIONS
# =============================================================================

@dataclass
class AppDefinition:
    """Definition of an app that can be discovered and recovered."""
    name: str
    app_type: str
    display_name: str

    # Discovery patterns
    process_names: List[str] = field(default_factory=list)  # ps aux patterns
    macos_bundle_ids: List[str] = field(default_factory=list)  # com.docker.docker
    executable_names: List[str] = field(default_factory=list)  # docker, ollama

    # Common paths (platform-specific)
    macos_app_paths: List[str] = field(default_factory=list)
    linux_bin_paths: List[str] = field(default_factory=list)
    config_paths: List[str] = field(default_factory=list)
    data_paths: List[str] = field(default_factory=list)

    # Health check
    health_check_method: str = "process"  # process, http, tcp, command
    health_check_endpoint: Optional[str] = None
    health_check_command: Optional[str] = None

    # Launch
    default_launch_method: str = "direct"  # open, systemctl, launchctl, direct
    default_ports: List[int] = field(default_factory=list)

    # Dependencies
    depends_on: List[str] = field(default_factory=list)
    startup_order: int = 100
    is_critical: bool = False


# Known apps that Context DNA may use
KNOWN_APPS: Dict[str, AppDefinition] = {
    "ollama": AppDefinition(
        name="ollama",
        app_type="llm_runtime",
        display_name="Ollama",
        process_names=["ollama", "ollama serve", "ollama_llama_server"],
        macos_bundle_ids=["com.ollama.ollama"],
        executable_names=["ollama"],
        macos_app_paths=["/Applications/Ollama.app"],
        linux_bin_paths=["/usr/local/bin/ollama", "/usr/bin/ollama"],
        config_paths=["~/.ollama"],
        data_paths=["~/.ollama/models"],
        health_check_method="http",
        health_check_endpoint="http://localhost:11434/api/version",
        default_launch_method="open",
        default_ports=[11434],
        startup_order=50,
    ),
    "redis": AppDefinition(
        name="redis",
        app_type="database",
        display_name="Redis",
        process_names=["redis-server"],
        executable_names=["redis-server", "redis-cli"],
        linux_bin_paths=["/usr/local/bin/redis-server", "/usr/bin/redis-server"],
        config_paths=["/etc/redis", "~/.redis"],
        data_paths=["/var/lib/redis"],
        health_check_method="tcp",
        health_check_endpoint="localhost:6379",
        default_launch_method="systemctl",
        default_ports=[6379],
        startup_order=30,
        is_critical=True,
    ),
    "postgres": AppDefinition(
        name="postgres",
        app_type="database",
        display_name="PostgreSQL",
        process_names=["postgres", "postgresql"],
        executable_names=["postgres", "psql"],
        macos_app_paths=["/Applications/Postgres.app"],
        linux_bin_paths=["/usr/lib/postgresql/*/bin/postgres"],
        config_paths=["/etc/postgresql", "~/.postgres"],
        data_paths=["/var/lib/postgresql"],
        health_check_method="tcp",
        health_check_endpoint="localhost:5432",
        default_launch_method="systemctl",
        default_ports=[5432],
        startup_order=20,
        is_critical=True,
    ),
    "seaweedfs": AppDefinition(
        name="seaweedfs",
        app_type="database",
        display_name="SeaweedFS",
        process_names=["weed", "seaweedfs"],
        executable_names=["weed"],
        linux_bin_paths=["/usr/local/bin/weed"],
        config_paths=["~/.seaweedfs"],
        health_check_method="http",
        health_check_endpoint="http://localhost:9333/cluster/status",
        default_ports=[9333, 8333],
        startup_order=40,
    ),
    "livekit": AppDefinition(
        name="livekit",
        app_type="ai_service",
        display_name="LiveKit Server",
        process_names=["livekit-server"],
        executable_names=["livekit-server"],
        linux_bin_paths=["/usr/local/bin/livekit-server"],
        config_paths=["~/.livekit", "/etc/livekit"],
        health_check_method="http",
        health_check_endpoint="http://localhost:7880/",
        default_ports=[7880, 7881],
        depends_on=["redis"],
        startup_order=60,
    ),
    "whisper": AppDefinition(
        name="whisper",
        app_type="ai_service",
        display_name="Whisper STT",
        process_names=["whisper", "whisper-server"],
        executable_names=["whisper"],
        linux_bin_paths=["/usr/local/bin/whisper"],
        config_paths=["~/.cache/whisper"],
        health_check_method="process",
        startup_order=70,
    ),
}


# =============================================================================
# APP RECOVERY MANAGER
# =============================================================================

class AppRecoveryManager:
    """
    Manages discovery, capture, and recovery of Context DNA apps.

    Follows the same pattern as docker_recovery.py but generalized
    for any supported application.
    """

    def __init__(self, db_path: Optional[Path] = None):
        """
        Initialize manager with SQLite database path.

        Args:
            db_path: Path to SQLite database. Defaults to ~/.context-dna/context_dna.db
        """
        if db_path is None:
            db_path = Path.home() / ".context-dna" / "context_dna.db"
        self.db_path = Path(db_path)
        self.platform = platform.system().lower()
        self._machine_id = self._get_machine_id()

    def _get_machine_id(self) -> str:
        """Get stable machine identifier."""
        if self.platform == "darwin":
            try:
                result = subprocess.run(
                    ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                    capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.split("\n"):
                    if "IOPlatformUUID" in line:
                        return line.split('"')[-2]
            except Exception as e:
                print(f"[WARN] Failed to get machine ID via ioreg: {e}")
        elif self.platform == "linux":
            try:
                machine_id_path = Path("/etc/machine-id")
                if machine_id_path.exists():
                    return machine_id_path.read_text().strip()
            except Exception as e:
                print(f"[WARN] Failed to read /etc/machine-id: {e}")
        return f"{self.platform}-{uuid.uuid4().hex[:8]}"

    # =========================================================================
    # DISCOVERY
    # =========================================================================

    def discover_app(self, app_name: str) -> Optional[Dict[str, Any]]:
        """
        Discover information about an installed app.

        Returns dict with paths, version, running status, etc.
        Returns None if app not found.
        """
        if app_name not in KNOWN_APPS:
            return None

        defn = KNOWN_APPS[app_name]
        info = {
            "app_name": app_name,
            "app_type": defn.app_type,
            "display_name": defn.display_name,
            "is_running": False,
            "executable_path": None,
            "app_bundle_path": None,
            "config_path": None,
            "data_path": None,
            "version": None,
            "pid": None,
        }

        # Check if running
        info["is_running"], info["pid"] = self._check_process_running(defn.process_names)

        # Find executable
        info["executable_path"] = self._find_executable(defn.executable_names)

        # Find app bundle (macOS)
        if self.platform == "darwin":
            info["app_bundle_path"] = self._find_app_bundle(defn.macos_app_paths)

        # Find config path
        info["config_path"] = self._find_existing_path(defn.config_paths)

        # Find data path
        info["data_path"] = self._find_existing_path(defn.data_paths)

        # Get version if executable found
        if info["executable_path"]:
            info["version"] = self._get_app_version(app_name, info["executable_path"])

        return info

    def discover_all_apps(self) -> Dict[str, Dict[str, Any]]:
        """Discover all known apps and their status."""
        results = {}
        for app_name in KNOWN_APPS:
            info = self.discover_app(app_name)
            if info:
                results[app_name] = info
        return results

    def _check_process_running(self, process_names: List[str]) -> Tuple[bool, Optional[int]]:
        """Check if any process with given names is running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "|".join(process_names)],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = result.stdout.strip().split("\n")
                return True, int(pids[0])
        except Exception as e:
            print(f"[WARN] pgrep process check failed: {e}")

        # Fallback: use ps
        try:
            result = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                for name in process_names:
                    if name in line and "grep" not in line:
                        parts = line.split()
                        if len(parts) > 1:
                            return True, int(parts[1])
        except Exception as e:
            print(f"[WARN] ps aux process check failed: {e}")

        return False, None

    def _find_executable(self, names: List[str]) -> Optional[str]:
        """Find executable in PATH."""
        for name in names:
            path = shutil.which(name)
            if path:
                return path
        return None

    def _find_app_bundle(self, paths: List[str]) -> Optional[str]:
        """Find macOS .app bundle."""
        for path in paths:
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                return expanded
        return None

    def _find_existing_path(self, paths: List[str]) -> Optional[str]:
        """Find first existing path from list."""
        for path in paths:
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                return expanded
        return None

    def _get_app_version(self, app_name: str, executable_path: str) -> Optional[str]:
        """Get version string for an app."""
        version_commands = {
            "ollama": [executable_path, "--version"],
            "redis": [executable_path, "--version"],
            "postgres": [executable_path, "--version"],
            "weed": [executable_path, "version"],
        }

        cmd = version_commands.get(app_name, [executable_path, "--version"])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip().split("\n")[0]
        except Exception as e:
            print(f"[WARN] Failed to get version for {app_name}: {e}")
        return None

    # =========================================================================
    # CAPTURE
    # =========================================================================

    def capture_app_config(self, app_name: str) -> Optional[Dict[str, Any]]:
        """
        Capture full configuration for a running app.

        Only captures when app is running to ensure accurate data.
        """
        info = self.discover_app(app_name)
        if not info:
            return None

        if not info["is_running"]:
            print(f"  [SKIP] {app_name} not running - cannot capture accurate config")
            return None

        defn = KNOWN_APPS[app_name]

        config = {
            "id": str(uuid.uuid4()),
            "machine_id": self._machine_id,
            "app_name": app_name,
            "app_type": defn.app_type,
            "app_version": info["version"],
            "executable_path": info["executable_path"],
            "app_bundle_path": info["app_bundle_path"],
            "config_path": info["config_path"],
            "data_path": info["data_path"],
            "socket_path": None,
            "pid_file_path": None,
            "launch_method": defn.default_launch_method,
            "launch_command": self._determine_launch_command(app_name, info),
            "launch_args": json.dumps([]),
            "environment_vars": json.dumps({}),
            "working_directory": None,
            "required_ports": json.dumps(defn.default_ports),
            "depends_on": json.dumps(defn.depends_on),
            "startup_order": defn.startup_order,
            "health_check_method": defn.health_check_method,
            "health_check_endpoint": defn.health_check_endpoint,
            "health_check_command": defn.health_check_command,
            "health_check_interval_ms": 5000,
            "health_check_timeout_ms": 3000,
            "startup_timeout_ms": 60000,
            "last_healthy_at": datetime.now(timezone.utc).isoformat(),
            "auto_recovery_enabled": 1,
            "is_critical": 1 if defn.is_critical else 0,
            "is_optional": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        return config

    def _determine_launch_command(self, app_name: str, info: Dict[str, Any]) -> str:
        """Determine the best launch command for an app."""
        defn = KNOWN_APPS[app_name]

        if self.platform == "darwin":
            # macOS: prefer 'open -a' for .app bundles
            if info["app_bundle_path"]:
                return f"open -a '{info['app_bundle_path']}'"
            elif info["executable_path"]:
                return info["executable_path"]
        else:
            # Linux: prefer systemctl if available
            if defn.default_launch_method == "systemctl":
                return f"systemctl start {app_name}"
            elif info["executable_path"]:
                return info["executable_path"]

        return ""

    def capture_all_running_apps(self) -> Dict[str, Dict[str, Any]]:
        """Capture configs for all running apps."""
        results = {}
        for app_name in KNOWN_APPS:
            config = self.capture_app_config(app_name)
            if config:
                results[app_name] = config
        return results

    # =========================================================================
    # SQLITE STORAGE
    # =========================================================================

    def save_config_to_sqlite(self, config: Dict[str, Any]) -> bool:
        """Save app config to SQLite."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()

            # Ensure table exists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS app_recovery_configs (
                    id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    app_type TEXT NOT NULL,
                    app_version TEXT,
                    executable_path TEXT,
                    app_bundle_path TEXT,
                    config_path TEXT,
                    data_path TEXT,
                    socket_path TEXT,
                    pid_file_path TEXT,
                    launch_method TEXT,
                    launch_command TEXT,
                    launch_args TEXT DEFAULT '[]',
                    environment_vars TEXT DEFAULT '{}',
                    working_directory TEXT,
                    required_ports TEXT DEFAULT '[]',
                    depends_on TEXT DEFAULT '[]',
                    startup_order INTEGER DEFAULT 100,
                    health_check_method TEXT,
                    health_check_endpoint TEXT,
                    health_check_command TEXT,
                    health_check_interval_ms INTEGER DEFAULT 5000,
                    health_check_timeout_ms INTEGER DEFAULT 3000,
                    startup_timeout_ms INTEGER DEFAULT 60000,
                    last_healthy_at TEXT,
                    last_recovery_at TEXT,
                    recovery_attempts INTEGER DEFAULT 0,
                    recovery_successes INTEGER DEFAULT 0,
                    consecutive_failures INTEGER DEFAULT 0,
                    last_error TEXT,
                    last_error_at TEXT,
                    successful_recovery_commands TEXT DEFAULT '[]',
                    failed_recovery_commands TEXT DEFAULT '[]',
                    recovery_notes TEXT,
                    auto_recovery_enabled INTEGER DEFAULT 1,
                    is_critical INTEGER DEFAULT 0,
                    is_optional INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    synced_to_postgres INTEGER DEFAULT 0,
                    synced_at TEXT,
                    postgres_id TEXT,
                    UNIQUE(machine_id, app_name)
                )
            """)

            # Upsert
            cursor.execute("""
                INSERT INTO app_recovery_configs (
                    id, machine_id, app_name, app_type, app_version,
                    executable_path, app_bundle_path, config_path, data_path,
                    socket_path, pid_file_path, launch_method, launch_command,
                    launch_args, environment_vars, working_directory, required_ports,
                    depends_on, startup_order, health_check_method, health_check_endpoint,
                    health_check_command, health_check_interval_ms, health_check_timeout_ms,
                    startup_timeout_ms, last_healthy_at, auto_recovery_enabled,
                    is_critical, is_optional, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(machine_id, app_name) DO UPDATE SET
                    app_version = excluded.app_version,
                    executable_path = excluded.executable_path,
                    app_bundle_path = excluded.app_bundle_path,
                    config_path = excluded.config_path,
                    data_path = excluded.data_path,
                    launch_command = excluded.launch_command,
                    last_healthy_at = excluded.last_healthy_at,
                    updated_at = excluded.updated_at,
                    synced_to_postgres = 0
            """, (
                config["id"], config["machine_id"], config["app_name"],
                config["app_type"], config["app_version"],
                config["executable_path"], config["app_bundle_path"],
                config["config_path"], config["data_path"],
                config["socket_path"], config["pid_file_path"],
                config["launch_method"], config["launch_command"],
                config["launch_args"], config["environment_vars"],
                config["working_directory"], config["required_ports"],
                config["depends_on"], config["startup_order"],
                config["health_check_method"], config["health_check_endpoint"],
                config["health_check_command"], config["health_check_interval_ms"],
                config["health_check_timeout_ms"], config["startup_timeout_ms"],
                config["last_healthy_at"], config["auto_recovery_enabled"],
                config["is_critical"], config["is_optional"],
                config["created_at"], config["updated_at"]
            ))

            conn.commit()
            conn.close()
            return True

        except Exception as e:
            print(f"  [ERROR] SQLite save failed: {e}")
            return False

    def load_config_from_sqlite(self, app_name: str) -> Optional[Dict[str, Any]]:
        """Load app config from SQLite."""
        if not self.db_path.exists():
            return None

        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT * FROM app_recovery_configs
                WHERE machine_id = ? AND app_name = ?
            """, (self._machine_id, app_name))

            row = cursor.fetchone()
            conn.close()

            if row:
                return dict(row)
            return None

        except Exception:
            return None

    def load_all_configs(self) -> List[Dict[str, Any]]:
        """Load all app configs for this machine."""
        if not self.db_path.exists():
            return []

        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT * FROM app_recovery_configs
                WHERE machine_id = ?
                ORDER BY startup_order ASC
            """, (self._machine_id,))

            rows = cursor.fetchall()
            conn.close()

            return [dict(row) for row in rows]

        except Exception:
            return []

    # =========================================================================
    # HEALTH CHECK
    # =========================================================================

    def check_app_health(self, app_name: str) -> Tuple[bool, str]:
        """
        Check if an app is healthy.

        Returns (is_healthy, status_message)
        """
        config = self.load_config_from_sqlite(app_name)
        if not config:
            # Fall back to discovery
            info = self.discover_app(app_name)
            if info and info["is_running"]:
                return True, "running (no cached config)"
            return False, "not found"

        method = config.get("health_check_method", "process")
        endpoint = config.get("health_check_endpoint")
        timeout = config.get("health_check_timeout_ms", 3000) / 1000

        if method == "http" and endpoint:
            try:
                import urllib.request
                req = urllib.request.Request(endpoint, method='GET')
                urllib.request.urlopen(req, timeout=timeout)
                return True, "healthy (http)"
            except Exception as e:
                return False, f"http check failed: {e}"

        elif method == "tcp" and endpoint:
            try:
                import socket
                host, port = endpoint.split(":")
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex((host, int(port)))
                sock.close()
                if result == 0:
                    return True, "healthy (tcp)"
                return False, f"tcp check failed: port {port} not listening"
            except Exception as e:
                return False, f"tcp check failed: {e}"

        else:
            # Default: process check
            defn = KNOWN_APPS.get(app_name)
            if defn:
                is_running, _ = self._check_process_running(defn.process_names)
                if is_running:
                    return True, "healthy (process)"
            return False, "not running"

    # =========================================================================
    # RECOVERY
    # =========================================================================

    def recover_app(self, app_name: str) -> bool:
        """
        Attempt to recover/start an app using cached configuration.

        Returns True if recovery succeeded.
        """
        config = self.load_config_from_sqlite(app_name)
        if not config:
            print(f"  [ERROR] No cached config for {app_name}")
            return False

        # Check dependencies first
        depends_on = json.loads(config.get("depends_on", "[]"))
        for dep in depends_on:
            is_healthy, _ = self.check_app_health(dep)
            if not is_healthy:
                print(f"  [WARN] Dependency {dep} not healthy, attempting recovery...")
                if not self.recover_app(dep):
                    print(f"  [ERROR] Failed to recover dependency {dep}")
                    return False

        launch_command = config.get("launch_command")
        if not launch_command:
            print(f"  [ERROR] No launch command for {app_name}")
            return False

        print(f"  Launching {app_name}: {launch_command}")

        try:
            # Execute launch command
            if self.platform == "darwin" and launch_command.startswith("open"):
                subprocess.Popen(launch_command, shell=True)
            else:
                subprocess.Popen(launch_command.split(), start_new_session=True)

            # Wait for startup
            timeout = config.get("startup_timeout_ms", 60000) / 1000
            import time
            start = time.time()

            while time.time() - start < timeout:
                is_healthy, status = self.check_app_health(app_name)
                if is_healthy:
                    print(f"  [OK] {app_name} recovered: {status}")
                    self._record_recovery_success(app_name, launch_command)
                    return True
                time.sleep(2)

            print(f"  [TIMEOUT] {app_name} did not become healthy within {timeout}s")
            self._record_recovery_failure(app_name, "startup timeout")
            return False

        except Exception as e:
            print(f"  [ERROR] Recovery failed: {e}")
            self._record_recovery_failure(app_name, str(e))
            return False

    def _record_recovery_success(self, app_name: str, command: str):
        """Record successful recovery to SQLite."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE app_recovery_configs
                SET
                    last_recovery_at = ?,
                    recovery_successes = recovery_successes + 1,
                    consecutive_failures = 0,
                    last_healthy_at = ?,
                    updated_at = ?
                WHERE machine_id = ? AND app_name = ?
            """, (
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                self._machine_id, app_name
            ))

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[WARN] Failed to record recovery success for {app_name}: {e}")

    def _record_recovery_failure(self, app_name: str, error: str):
        """Record failed recovery to SQLite."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE app_recovery_configs
                SET
                    last_recovery_at = ?,
                    recovery_attempts = recovery_attempts + 1,
                    consecutive_failures = consecutive_failures + 1,
                    last_error = ?,
                    last_error_at = ?,
                    updated_at = ?
                WHERE machine_id = ? AND app_name = ?
            """, (
                datetime.now(timezone.utc).isoformat(),
                error,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                self._machine_id, app_name
            ))

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[WARN] Failed to record recovery failure for {app_name}: {e}")

    # =========================================================================
    # ORCHESTRATION
    # =========================================================================

    def recover_all_critical_apps(self) -> Dict[str, bool]:
        """
        Recover all critical apps in dependency order.

        Returns dict of app_name -> success
        """
        configs = self.load_all_configs()
        critical = [c for c in configs if c.get("is_critical")]

        # Sort by startup_order
        critical.sort(key=lambda c: c.get("startup_order", 100))

        results = {}
        for config in critical:
            app_name = config["app_name"]
            is_healthy, _ = self.check_app_health(app_name)

            if is_healthy:
                print(f"  [OK] {app_name} already healthy")
                results[app_name] = True
            else:
                print(f"  [RECOVER] {app_name}...")
                results[app_name] = self.recover_app(app_name)

        return results

    def get_llm_recovery_context(self, app_name: str) -> Dict[str, Any]:
        """
        Get context for local LLM to assist with recovery.

        Returns structured data the LLM can use to suggest recovery steps.
        """
        config = self.load_config_from_sqlite(app_name)
        info = self.discover_app(app_name)
        is_healthy, status = self.check_app_health(app_name)

        return {
            "app_name": app_name,
            "platform": self.platform,
            "is_healthy": is_healthy,
            "health_status": status,
            "cached_config": config,
            "discovered_info": info,
            "known_definition": asdict(KNOWN_APPS.get(app_name)) if app_name in KNOWN_APPS else None,
            "recovery_history": {
                "attempts": config.get("recovery_attempts", 0) if config else 0,
                "successes": config.get("recovery_successes", 0) if config else 0,
                "consecutive_failures": config.get("consecutive_failures", 0) if config else 0,
                "last_error": config.get("last_error") if config else None,
            } if config else None,
        }


# =============================================================================
# CLI
# =============================================================================

def main():
    import sys

    db_path = Path.home() / ".context-dna" / "context_dna.db"
    manager = AppRecoveryManager(db_path)

    if len(sys.argv) < 2:
        print("Usage: python -m context_dna.storage.app_recovery <command> [args]")
        print("\nCommands:")
        print("  discover [app]     - Discover installed apps")
        print("  capture [app]      - Capture config for running app(s)")
        print("  recover <app>      - Attempt to recover/start app")
        print("  health [app]       - Check health of app(s)")
        print("  status             - Show all cached configs")
        print("  llm-context <app>  - Get LLM recovery context")
        return

    cmd = sys.argv[1]

    if cmd == "discover":
        app = sys.argv[2] if len(sys.argv) > 2 else None
        if app:
            info = manager.discover_app(app)
            if info:
                print(f"\n{app}:")
                for k, v in info.items():
                    print(f"  {k}: {v}")
            else:
                print(f"Unknown app: {app}")
        else:
            print("\nDiscovered Apps:")
            for name, info in manager.discover_all_apps().items():
                status = "RUNNING" if info["is_running"] else "stopped"
                print(f"  {name}: {status}")
                if info["version"]:
                    print(f"    version: {info['version']}")
                if info["executable_path"]:
                    print(f"    path: {info['executable_path']}")

    elif cmd == "capture":
        app = sys.argv[2] if len(sys.argv) > 2 else None
        if app:
            config = manager.capture_app_config(app)
            if config:
                if manager.save_config_to_sqlite(config):
                    print(f"[OK] Captured {app} config to SQLite")
                else:
                    print(f"[ERROR] Failed to save {app} config")
            else:
                print(f"[SKIP] Could not capture {app} (not running?)")
        else:
            print("\nCapturing all running apps...")
            configs = manager.capture_all_running_apps()
            for name, config in configs.items():
                if manager.save_config_to_sqlite(config):
                    print(f"  [OK] {name}")
                else:
                    print(f"  [ERROR] {name}")

    elif cmd == "recover":
        if len(sys.argv) < 3:
            print("Usage: ... recover <app>")
            return
        app = sys.argv[2]
        success = manager.recover_app(app)
        print(f"\nRecovery {'succeeded' if success else 'FAILED'}")

    elif cmd == "health":
        app = sys.argv[2] if len(sys.argv) > 2 else None
        if app:
            is_healthy, status = manager.check_app_health(app)
            print(f"{app}: {'HEALTHY' if is_healthy else 'UNHEALTHY'} - {status}")
        else:
            print("\nHealth Status:")
            for name in KNOWN_APPS:
                is_healthy, status = manager.check_app_health(name)
                mark = "[OK]" if is_healthy else "[!!]"
                print(f"  {mark} {name}: {status}")

    elif cmd == "status":
        configs = manager.load_all_configs()
        if not configs:
            print("No cached configs found")
            return
        print(f"\nCached App Configs ({len(configs)}):")
        for c in configs:
            critical = " [CRITICAL]" if c.get("is_critical") else ""
            print(f"\n  {c['app_name']}{critical}")
            print(f"    type: {c['app_type']}")
            print(f"    launch: {c.get('launch_command', 'N/A')}")
            print(f"    last_healthy: {c.get('last_healthy_at', 'never')}")
            print(f"    recovery: {c.get('recovery_successes', 0)}/{c.get('recovery_attempts', 0)} attempts")

    elif cmd == "llm-context":
        if len(sys.argv) < 3:
            print("Usage: ... llm-context <app>")
            return
        app = sys.argv[2]
        context = manager.get_llm_recovery_context(app)
        print(json.dumps(context, indent=2, default=str))

    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
