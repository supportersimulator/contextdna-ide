"""
Docker Desktop Recovery Helper for Context DNA

Automatically detects and launches Docker Desktop when heavy mode needs it.
Supports macOS (primary), with Linux and Windows stubs for future use.

Key Features:
- Detects if Docker daemon is running
- Auto-launches Docker Desktop on macOS if needed
- Waits for Docker to be ready before proceeding
- Non-blocking with configurable timeout
- Logs all recovery actions for debugging

Usage:
    from context_dna.storage.docker_recovery import DockerRecovery

    recovery = DockerRecovery()
    is_ready = await recovery.ensure_docker_ready(timeout=60)
"""

import asyncio
import subprocess
import platform
import time
import json
import shutil
import sqlite3
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List
import logging

logger = logging.getLogger(__name__)


class DockerState(Enum):
    """Formal state machine for Docker Desktop lifecycle."""
    NOT_INSTALLED = "not_installed"
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    CRASHED = "crashed"
    NEEDS_LOGIN = "needs_login"
    RESOURCE_ISSUE = "resource_issue"
    STUCK = "stuck"


class DockerRecovery:
    """
    Manages Docker Desktop state for Context DNA heavy mode recovery.

    When heavy mode is requested but Docker is not running:
    1. Detect the situation
    2. Launch Docker Desktop
    3. Wait for it to be ready
    4. Allow sync to proceed
    """

    def __init__(self):
        self._platform = platform.system().lower()
        self._last_launch_attempt: Optional[datetime] = None
        self._launch_cooldown_seconds = 30  # Don't spam launch attempts
        self._docker_ready = False
        self._last_successful_strategy: Optional[str] = None
        self._cached_launch_method: Optional[str] = None

    @property
    def is_macos(self) -> bool:
        return self._platform == 'darwin'

    @property
    def is_linux(self) -> bool:
        return self._platform == 'linux'

    @property
    def is_windows(self) -> bool:
        return self._platform == 'windows'

    def _run_command(self, cmd: list, timeout: float = 5.0) -> Tuple[bool, str]:
        """Run a command and return (success, output)."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.returncode == 0, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except FileNotFoundError:
            return False, f"Command not found: {cmd[0]}"
        except Exception as e:
            return False, str(e)

    def is_docker_socket_present(self) -> bool:
        """
        Fast check: does the Docker socket exist?
        This is a quick indicator that Docker might be running.
        """
        socket_paths = [
            Path.home() / '.docker/run/docker.sock',
            Path('/var/run/docker.sock'),
        ]
        for path in socket_paths:
            if path.exists():
                return True
        return False

    def is_docker_daemon_running(self) -> bool:
        """
        Check if Docker daemon is running and responsive.
        Uses a tiered approach for faster detection:
        1. Quick socket check
        2. Fast 'docker version' (client-side)
        3. Full 'docker info' (server-side, slower)
        """
        # Fast check: socket must exist
        if not self.is_docker_socket_present():
            logger.info("Docker socket not found - daemon not running")
            return False

        # Medium check: docker version (client responds quickly even if server slow)
        success, output = self._run_command(['docker', 'version', '--format', '{{.Server.Version}}'], timeout=3.0)

        if success and output.strip():
            self._docker_ready = True
            return True

        # Full check: docker info (slower, but definitive)
        success, output = self._run_command(['docker', 'info'], timeout=3.0)

        if success:
            self._docker_ready = True
            return True

        # Common failure patterns
        if 'Command timed out' in output:
            logger.info("Docker daemon slow/unresponsive (timeout)")
            return False
        if 'Cannot connect to the Docker daemon' in output:
            logger.info("Docker daemon not running")
            return False
        if 'error during connect' in output:
            logger.info("Docker daemon not reachable")
            return False
        if 'request returned 500' in output:
            logger.info("Docker API version mismatch - needs restart")
            return False
        if 'Command not found' in output:
            logger.warning("Docker CLI not installed")
            return False

        # Unknown failure
        logger.warning(f"Docker check failed: {output[:200]}")
        return False

    def is_docker_desktop_app_running(self) -> bool:
        """
        Check if Docker Desktop app is running (GUI process).
        This is different from the daemon - app can be running but daemon not ready.
        """
        if self.is_macos:
            # Modern Docker Desktop uses com.docker.backend
            success, output = self._run_command(['pgrep', '-f', 'com.docker.backend'])
            if success:
                return True
            # Fallback: check for "Docker Desktop" process (older versions)
            success, output = self._run_command(['pgrep', '-f', 'Docker Desktop'])
            if success:
                return True
            # Fallback: check for just "Docker" (very old versions)
            success, output = self._run_command(['pgrep', '-x', 'Docker'])
            return success
        elif self.is_linux:
            # Linux may use dockerd directly or Docker Desktop
            success, output = self._run_command(['pgrep', '-f', 'dockerd'])
            if success:
                return True
            success, output = self._run_command(['pgrep', '-f', 'com.docker'])
            return success
        elif self.is_windows:
            # Windows: check for Docker Desktop process
            success, output = self._run_command(['tasklist', '/FI', 'IMAGENAME eq Docker Desktop.exe'])
            return success and 'Docker Desktop.exe' in output
        return False

    def launch_docker_desktop(self) -> bool:
        """
        Launch Docker Desktop application.
        Returns True if launch was initiated, False if failed or on cooldown.
        """
        # Check cooldown
        if self._last_launch_attempt:
            elapsed = (datetime.now(timezone.utc) - self._last_launch_attempt).total_seconds()
            if elapsed < self._launch_cooldown_seconds:
                logger.info(f"Docker launch on cooldown ({self._launch_cooldown_seconds - elapsed:.0f}s remaining)")
                return False

        self._last_launch_attempt = datetime.now(timezone.utc)

        if self.is_macos:
            return self._launch_docker_macos()
        elif self.is_linux:
            return self._launch_docker_linux()
        elif self.is_windows:
            return self._launch_docker_windows()

        logger.warning(f"Unsupported platform for Docker auto-launch: {self._platform}")
        return False

    def _get_launch_strategies_macos(self) -> List[Tuple[str, list]]:
        """Get ordered launch strategies for macOS, preferring cached method."""
        docker_app = self._find_docker_app() or '/Applications/Docker.app'
        strategies = [
            ('open', ['open', '-a', 'Docker']),
            ('direct', ['open', str(docker_app)]),
            ('applescript', ['osascript', '-e', 'tell application "Docker" to activate']),
            ('launchctl', ['launchctl', 'start', 'com.docker.helper']),
        ]
        return strategies

    def _launch_docker_macos(self) -> bool:
        """Launch Docker Desktop on macOS using multi-strategy fallback."""
        # Check if Docker is installed
        docker_app = self._find_docker_app()
        if not docker_app and not Path('/Applications/Docker.app').exists():
            logger.error("Docker Desktop not found in /Applications")
            return False

        strategies = self._get_launch_strategies_macos()

        # Try cached method first if available
        cached = getattr(self, '_cached_launch_method', None)
        if cached:
            strategies.sort(key=lambda s: 0 if s[0] == cached else 1)

        for name, cmd in strategies:
            logger.info(f"Trying launch strategy: {name} ({' '.join(cmd)})")
            success, output = self._run_command(cmd, timeout=10.0)
            if success:
                logger.info(f"Docker launch initiated via {name}")
                self._last_successful_strategy = name
                return True
            else:
                logger.warning(f"Strategy {name} failed: {output[:100]}")

        logger.error("All launch strategies failed")
        return False

    def _launch_docker_linux(self) -> bool:
        """Launch Docker on Linux (systemd or Docker Desktop)."""
        # Try systemd first (more common on servers)
        success, output = self._run_command(['systemctl', 'start', 'docker'])
        if success:
            logger.info("Docker daemon started via systemd")
            return True

        # Try Docker Desktop for Linux
        success, output = self._run_command(['systemctl', '--user', 'start', 'docker-desktop'])
        if success:
            logger.info("Docker Desktop started via user systemd")
            return True

        logger.warning("Could not start Docker on Linux")
        return False

    def _launch_docker_windows(self) -> bool:
        """Launch Docker Desktop on Windows."""
        docker_paths = [
            r'C:\Program Files\Docker\Docker\Docker Desktop.exe',
            r'C:\Program Files (x86)\Docker\Docker\Docker Desktop.exe',
        ]

        for path in docker_paths:
            if Path(path).exists():
                logger.info(f"Launching Docker Desktop: {path}")
                success, output = self._run_command(['start', '', path], timeout=10.0)
                if success:
                    return True

        logger.error("Docker Desktop not found on Windows")
        return False

    def quit_docker_desktop(self, force: bool = False) -> bool:
        """
        Quit Docker Desktop application.

        Args:
            force: If True, use pkill/kill instead of graceful quit

        Returns True if quit was initiated.
        """
        if self.is_macos:
            if force:
                # Force kill all Docker processes
                logger.info("Force killing Docker processes...")
                self._run_command(['pkill', '-9', '-f', 'com.docker.backend'], timeout=5.0)
                self._run_command(['pkill', '-9', '-f', 'Docker Desktop'], timeout=5.0)
                self._run_command(['pkill', '-9', '-f', 'com.docker.build'], timeout=5.0)
                time.sleep(3)
                return True
            else:
                # Try graceful quit via osascript
                success, output = self._run_command([
                    'osascript', '-e', 'tell application "Docker Desktop" to quit'
                ], timeout=10.0)
                if success:
                    logger.info("Docker Desktop quit initiated")
                    time.sleep(2)
                    # Check if it actually quit
                    if not self.is_docker_desktop_app_running():
                        return True
                    # Didn't quit gracefully, try pkill
                    logger.info("Graceful quit didn't work, using pkill...")
                    self._run_command(['pkill', '-f', 'com.docker.backend'], timeout=5.0)
                    time.sleep(2)
                    return True
                # Fallback: pkill
                success, output = self._run_command(['pkill', '-f', 'com.docker.backend'])
                return success
        elif self.is_linux:
            if force:
                self._run_command(['pkill', '-9', '-f', 'dockerd'])
                self._run_command(['pkill', '-9', '-f', 'com.docker'])
                return True
            success, output = self._run_command(['systemctl', 'stop', 'docker'])
            if not success:
                success, output = self._run_command(['systemctl', '--user', 'stop', 'docker-desktop'])
            return success
        elif self.is_windows:
            # taskkill for Windows
            flags = ['/F'] if force else []
            success, output = self._run_command([
                'taskkill', '/IM', 'Docker Desktop.exe', *flags
            ])
            return success
        return False

    async def restart_docker_desktop(self, timeout: float = 90.0, force: bool = False) -> bool:
        """
        Restart Docker Desktop - quit and relaunch.
        Useful for fixing API version mismatches or stuck daemon.

        Args:
            timeout: Max seconds to wait for Docker to be ready
            force: If True, use force kill instead of graceful quit

        Returns True if Docker is ready after restart.
        """
        logger.info(f"Restarting Docker Desktop (force={force})...")

        # Quit if running
        if self.is_docker_desktop_app_running():
            logger.info(f"Quitting Docker Desktop (force={force})...")
            self.quit_docker_desktop(force=force)
            # Wait for quit
            for i in range(15):
                if not self.is_docker_desktop_app_running():
                    logger.info(f"Docker quit after {i+1}s")
                    break
                await asyncio.sleep(1)
            else:
                if not force:
                    # Try force quit if graceful didn't work
                    logger.warning("Graceful quit timed out, forcing...")
                    self.quit_docker_desktop(force=True)
                    await asyncio.sleep(3)

        # Clear ready state
        self._docker_ready = False

        # Wait a moment for cleanup
        await asyncio.sleep(2)

        # Launch fresh
        logger.info("Launching Docker Desktop...")
        if not self.launch_docker_desktop():
            logger.error("Failed to launch Docker Desktop")
            return False

        # Wait for ready
        return await self.wait_for_docker_ready(timeout=timeout)

    async def wait_for_docker_ready(self, timeout: float = 60.0, check_interval: float = 2.0) -> bool:
        """
        Wait for Docker daemon to become ready.
        Returns True if ready within timeout, False otherwise.
        """
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            if self.is_docker_daemon_running():
                elapsed = time.time() - start_time
                logger.info(f"Docker ready after {elapsed:.1f}s")
                return True

            # Log progress periodically
            elapsed = time.time() - start_time
            if int(elapsed) % 10 == 0 and elapsed > 0:
                logger.info(f"Waiting for Docker... ({elapsed:.0f}s)")

            await asyncio.sleep(check_interval)

        logger.warning(f"Docker not ready after {timeout}s timeout")
        return False

    async def ensure_docker_ready(self, timeout: float = 60.0, auto_launch: bool = True) -> bool:
        """
        Ensure Docker is running and ready.

        If Docker is not running and auto_launch is True:
        1. Launch Docker Desktop
        2. Wait for daemon to be ready
        3. Return success/failure

        Args:
            timeout: Max seconds to wait for Docker to be ready
            auto_launch: If True, attempt to launch Docker Desktop if not running

        Returns:
            True if Docker is ready, False otherwise
        """
        # Quick check first
        if self.is_docker_daemon_running():
            return True

        logger.info("Docker daemon not running")

        if not auto_launch:
            return False

        # Check if app is already running (just not ready yet)
        if self.is_docker_desktop_app_running():
            logger.info("Docker Desktop app running, waiting for daemon...")
            return await self.wait_for_docker_ready(timeout=timeout)

        # Need to launch Docker Desktop
        logger.info("Attempting to launch Docker Desktop...")

        if not self.launch_docker_desktop():
            logger.error("Failed to launch Docker Desktop")
            return False

        # Wait for it to be ready
        return await self.wait_for_docker_ready(timeout=timeout)

    def get_status(self) -> dict:
        """Get current Docker status for diagnostics."""
        socket_present = self.is_docker_socket_present()
        app_running = self.is_docker_desktop_app_running()

        # Only check daemon if socket exists (avoids slow timeout)
        if socket_present:
            daemon_running = self.is_docker_daemon_running()
        else:
            daemon_running = False

        return {
            'platform': self._platform,
            'socket_present': socket_present,
            'app_running': app_running,
            'daemon_running': daemon_running,
            'docker_ready': self._docker_ready,
            'last_launch_attempt': self._last_launch_attempt.isoformat() if self._last_launch_attempt else None,
        }

    def diagnose_docker_state(self) -> dict:
        """
        Detailed diagnosis of Docker state for recovery decisions.
        Returns analysis of what's working/broken and recommended action.
        """
        diagnosis = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'platform': self._platform,
            'checks': {},
            'state': 'unknown',
            'recommended_action': None,
            'action_reason': None,
        }

        # Check 1: Socket exists?
        socket_present = self.is_docker_socket_present()
        diagnosis['checks']['socket'] = {
            'passed': socket_present,
            'detail': 'Docker socket found' if socket_present else 'No socket'
        }

        # Check 2: App processes running?
        app_running = self.is_docker_desktop_app_running()
        diagnosis['checks']['app_process'] = {
            'passed': app_running,
            'detail': 'Docker Desktop processes found' if app_running else 'No Docker processes'
        }

        # Check 3: Can we get Docker version quickly?
        success, output = self._run_command(
            ['docker', 'version', '--format', '{{.Server.Version}}'],
            timeout=2.0
        )
        diagnosis['checks']['version_quick'] = {
            'passed': success and output.strip(),
            'detail': f'Version: {output.strip()}' if success else output[:100]
        }

        # Check 4: Full daemon responsive? (if quick check failed)
        if not diagnosis['checks']['version_quick']['passed'] and socket_present:
            success, output = self._run_command(['docker', 'info'], timeout=5.0)
            diagnosis['checks']['daemon_full'] = {
                'passed': success,
                'detail': 'Daemon responsive' if success else output[:100]
            }
        else:
            diagnosis['checks']['daemon_full'] = diagnosis['checks']['version_quick']

        # Determine state and action using DockerState enum
        if diagnosis['checks'].get('daemon_full', {}).get('passed') or diagnosis['checks']['version_quick']['passed']:
            diagnosis['state'] = DockerState.RUNNING.value
            diagnosis['docker_state'] = DockerState.RUNNING
            diagnosis['recommended_action'] = 'none'
            diagnosis['action_reason'] = 'Docker is running normally'
        elif app_running and socket_present:
            diagnosis['state'] = DockerState.STUCK.value
            diagnosis['docker_state'] = DockerState.STUCK
            diagnosis['recommended_action'] = 'restart'
            diagnosis['action_reason'] = 'App running but daemon unresponsive - needs restart'
        elif app_running and not socket_present:
            diagnosis['state'] = DockerState.STARTING.value
            diagnosis['docker_state'] = DockerState.STARTING
            diagnosis['recommended_action'] = 'wait'
            diagnosis['action_reason'] = 'App starting, socket not ready yet'
        elif not app_running and socket_present:
            diagnosis['state'] = DockerState.CRASHED.value
            diagnosis['docker_state'] = DockerState.CRASHED
            diagnosis['recommended_action'] = 'launch'
            diagnosis['action_reason'] = 'Stale socket, app not running - needs launch'
        else:
            diagnosis['state'] = DockerState.STOPPED.value
            diagnosis['docker_state'] = DockerState.STOPPED
            diagnosis['recommended_action'] = 'launch'
            diagnosis['action_reason'] = 'Docker not running - needs launch'

        return diagnosis

    def get_state(self) -> DockerState:
        """Get current Docker state as enum value."""
        if not self._is_installed():
            return DockerState.NOT_INSTALLED
        diagnosis = self.diagnose_docker_state()
        return diagnosis.get('docker_state', DockerState.STOPPED)

    def _is_installed(self) -> bool:
        """Check if Docker Desktop is installed."""
        if self.is_macos:
            return (Path('/Applications/Docker.app').exists() or
                    (Path.home() / 'Applications/Docker.app').exists())
        elif self.is_linux:
            return shutil.which('docker') is not None
        elif self.is_windows:
            return (Path(r'C:\Program Files\Docker\Docker\Docker Desktop.exe').exists() or
                    Path(r'C:\Program Files (x86)\Docker\Docker\Docker Desktop.exe').exists())
        return False

    async def smart_recover(self, timeout: float = 90.0) -> dict:
        """
        Intelligently recover Docker based on current state.

        Decides between:
        - wait: if Docker is starting
        - launch: if Docker is stopped
        - restart: if Docker is stuck/crashed

        Returns dict with success, action_taken, and details.
        """
        diagnosis = self.diagnose_docker_state()
        result = {
            'diagnosis': diagnosis,
            'action_taken': diagnosis['recommended_action'],
            'success': False,
            'details': ''
        }

        action = diagnosis['recommended_action']

        if action == 'none':
            result['success'] = True
            result['details'] = 'Docker already healthy'
            return result

        elif action == 'wait':
            logger.info(f"Docker starting, waiting up to {timeout}s...")
            result['success'] = await self.wait_for_docker_ready(timeout=timeout)
            result['details'] = 'Docker became ready' if result['success'] else 'Timeout waiting'

        elif action == 'launch':
            logger.info("Docker stopped, launching...")
            if self.launch_docker_desktop():
                result['success'] = await self.wait_for_docker_ready(timeout=timeout)
                result['details'] = 'Launched and ready' if result['success'] else 'Launched but not ready'
                if result['success'] and self._last_successful_strategy:
                    result['launch_strategy'] = self._last_successful_strategy
            else:
                result['details'] = 'Failed to launch Docker Desktop'

        elif action == 'restart':
            logger.info("Docker stuck, force restarting...")
            result['success'] = await self.restart_docker_desktop(timeout=timeout, force=True)
            result['details'] = 'Force restarted successfully' if result['success'] else 'Force restart failed'

        return result

    def get_docker_processes(self) -> list:
        """Get list of running Docker-related processes."""
        processes = []

        if self.is_macos or self.is_linux:
            success, output = self._run_command(['pgrep', '-lf', 'docker'])
            if success:
                for line in output.strip().split('\n'):
                    if line.strip():
                        parts = line.split(None, 1)
                        if len(parts) == 2:
                            processes.append({'pid': parts[0], 'command': parts[1]})
        elif self.is_windows:
            success, output = self._run_command(['tasklist', '/FI', 'IMAGENAME eq docker*'])
            if success:
                for line in output.strip().split('\n')[3:]:  # Skip headers
                    if line.strip():
                        processes.append({'line': line})

        return processes

    # =========================================================================
    # ENVIRONMENT CAPTURE (Self-learning recovery)
    # =========================================================================

    def capture_environment(self) -> Dict[str, Any]:
        """
        Capture Docker environment when running.
        Call this when Docker IS running to store config for later recovery.

        Returns dict with all captured info.
        """
        if not self.is_docker_daemon_running():
            logger.warning("Cannot capture environment - Docker not running")
            return {}

        env = {
            'machine_id': self._get_machine_id(),
            'platform': self._platform,
            'platform_version': platform.version(),
            'architecture': platform.machine(),
            'captured_at': datetime.now(timezone.utc).isoformat(),
        }

        # Get Docker version info
        try:
            result = subprocess.run(
                ['docker', 'version', '--format', '{{json .}}'],
                capture_output=True, text=True, timeout=10.0
            )
            if result.returncode == 0:
                version_data = json.loads(result.stdout)
                client = version_data.get('Client', {})
                server = version_data.get('Server', {})
                env['docker_client_version'] = client.get('Version')
                env['docker_api_version'] = client.get('APIVersion')
                env['docker_server_version'] = server.get('Version') if server else None
        except Exception as e:
            logger.warning(f"Failed to get docker version: {e}")

        # Get Docker info
        try:
            result = subprocess.run(
                ['docker', 'info', '--format', '{{json .}}'],
                capture_output=True, text=True, timeout=10.0
            )
            if result.returncode == 0:
                info_data = json.loads(result.stdout)
                env['runtime_type'] = info_data.get('Driver')
                env['storage_driver'] = info_data.get('Driver')
                env['memory_total_bytes'] = info_data.get('MemTotal')
                env['cpu_count'] = info_data.get('NCPU')
        except Exception as e:
            logger.warning(f"Failed to get docker info: {e}")

        # Find paths
        env['docker_cli_path'] = shutil.which('docker')
        env['docker_app_path'] = self._find_docker_app()
        env['docker_socket_path'] = self._find_docker_socket()

        return env

    def _get_machine_id(self) -> str:
        """Get unique machine identifier."""
        if self.is_macos:
            try:
                result = subprocess.run(
                    ['ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                    capture_output=True, text=True, timeout=5.0
                )
                for line in result.stdout.split('\n'):
                    if 'IOPlatformUUID' in line:
                        return line.split('"')[-2]
            except Exception as e:
                print(f"[WARN] Failed to get machine ID via ioreg: {e}")
        # Fallback to hostname
        return platform.node()

    def _find_docker_app(self) -> Optional[str]:
        """Find Docker Desktop app path."""
        if self.is_macos:
            paths = [
                '/Applications/Docker.app',
                str(Path.home() / 'Applications/Docker.app'),
            ]
            for p in paths:
                if Path(p).exists():
                    return p
        elif self.is_linux:
            paths = [
                '/opt/docker-desktop',
                '/usr/share/docker-desktop',
            ]
            for p in paths:
                if Path(p).exists():
                    return p
        elif self.is_windows:
            paths = [
                r'C:\Program Files\Docker\Docker',
                r'C:\Program Files (x86)\Docker\Docker',
            ]
            for p in paths:
                if Path(p).exists():
                    return p
        return None

    def _find_docker_socket(self) -> Optional[str]:
        """Find Docker socket path."""
        if self.is_macos or self.is_linux:
            paths = [
                str(Path.home() / '.docker/run/docker.sock'),
                '/var/run/docker.sock',
            ]
            for p in paths:
                if Path(p).exists():
                    return p
        return None

    def save_environment_to_sqlite(self, db_path: Path, env: Dict[str, Any],
                                    launch_method: Optional[str] = None) -> bool:
        """
        Save captured environment to SQLite for recovery use.

        Args:
            db_path: Path to SQLite database
            env: Environment dict from capture_environment()
            launch_method: If provided, record this as the successful launch method
        """
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            # Upsert the environment
            cursor.execute("""
                INSERT INTO docker_environment (
                    id, machine_id, docker_client_version, docker_server_version,
                    docker_api_version, docker_app_path, docker_cli_path,
                    docker_socket_path, platform, platform_version, architecture,
                    runtime_type, storage_driver, memory_total_bytes, cpu_count,
                    successful_launch_method, last_successful_launch_at,
                    last_healthy_at, captured_at, updated_at
                ) VALUES (
                    'singleton', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(machine_id) DO UPDATE SET
                    docker_client_version = excluded.docker_client_version,
                    docker_server_version = excluded.docker_server_version,
                    docker_api_version = excluded.docker_api_version,
                    docker_app_path = excluded.docker_app_path,
                    docker_cli_path = excluded.docker_cli_path,
                    docker_socket_path = excluded.docker_socket_path,
                    platform = excluded.platform,
                    platform_version = excluded.platform_version,
                    architecture = excluded.architecture,
                    runtime_type = excluded.runtime_type,
                    storage_driver = excluded.storage_driver,
                    memory_total_bytes = excluded.memory_total_bytes,
                    cpu_count = excluded.cpu_count,
                    successful_launch_method = COALESCE(excluded.successful_launch_method, docker_environment.successful_launch_method),
                    last_successful_launch_at = COALESCE(excluded.last_successful_launch_at, docker_environment.last_successful_launch_at),
                    last_healthy_at = excluded.last_healthy_at,
                    launch_success_count = docker_environment.launch_success_count + CASE WHEN excluded.successful_launch_method IS NOT NULL THEN 1 ELSE 0 END,
                    updated_at = excluded.updated_at
            """, (
                env.get('machine_id'),
                env.get('docker_client_version'),
                env.get('docker_server_version'),
                env.get('docker_api_version'),
                env.get('docker_app_path'),
                env.get('docker_cli_path'),
                env.get('docker_socket_path'),
                env.get('platform'),
                env.get('platform_version'),
                env.get('architecture'),
                env.get('runtime_type'),
                env.get('storage_driver'),
                env.get('memory_total_bytes'),
                env.get('cpu_count'),
                launch_method,
                datetime.now(timezone.utc).isoformat() if launch_method else None,
                datetime.now(timezone.utc).isoformat(),
                env.get('captured_at'),
                datetime.now(timezone.utc).isoformat(),
            ))

            conn.commit()
            conn.close()
            logger.info("Docker environment saved to SQLite")
            return True

        except Exception as e:
            logger.error(f"Failed to save Docker environment: {e}")
            return False

    def load_environment_from_sqlite(self, db_path: Path) -> Optional[Dict[str, Any]]:
        """
        Load cached Docker environment from SQLite.
        Use this to get launch hints when Docker is down.
        """
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT * FROM docker_environment
                WHERE machine_id = ?
                LIMIT 1
            """, (self._get_machine_id(),))

            row = cursor.fetchone()
            conn.close()

            if row:
                return dict(row)
            return None

        except Exception as e:
            logger.warning(f"Failed to load Docker environment: {e}")
            return None

    def get_cached_launch_method(self, db_path: Path) -> Optional[str]:
        """Get the launch method that worked last time."""
        env = self.load_environment_from_sqlite(db_path)
        if env:
            return env.get('successful_launch_method')
        return None

    def get_cached_docker_app_path(self, db_path: Path) -> Optional[str]:
        """Get the cached Docker app path."""
        env = self.load_environment_from_sqlite(db_path)
        if env:
            return env.get('docker_app_path')
        return None

    def record_recovery_attempt(self, db_path: Path, success: bool):
        """Record a recovery attempt for tracking."""
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE docker_environment
                SET last_recovery_attempt_at = ?,
                    last_recovery_success = ?,
                    recovery_attempts_total = recovery_attempts_total + 1,
                    recovery_successes_total = recovery_successes_total + ?,
                    consecutive_failures = CASE WHEN ? = 1 THEN 0 ELSE consecutive_failures + 1 END,
                    updated_at = ?
                WHERE machine_id = ?
            """, (
                datetime.now(timezone.utc).isoformat(),
                1 if success else 0,
                1 if success else 0,
                1 if success else 0,
                datetime.now(timezone.utc).isoformat(),
                self._get_machine_id(),
            ))

            conn.commit()
            conn.close()

        except Exception as e:
            logger.warning(f"Failed to record recovery attempt: {e}")


# Singleton instance for convenience
_docker_recovery: Optional[DockerRecovery] = None

def get_docker_recovery() -> DockerRecovery:
    """Get or create singleton DockerRecovery instance."""
    global _docker_recovery
    if _docker_recovery is None:
        _docker_recovery = DockerRecovery()
    return _docker_recovery


# CLI for testing
if __name__ == '__main__':
    import sys

    logging.basicConfig(level=logging.INFO, format='%(message)s')

    recovery = DockerRecovery()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == 'status':
            status = recovery.get_status()
            print(f"Platform: {status['platform']}")
            print(f"Socket present: {'✓' if status['socket_present'] else '✗'}")
            print(f"App running: {'✓' if status['app_running'] else '✗'}")
            print(f"Daemon running: {'✓' if status['daemon_running'] else '✗'}")
            print(f"Docker ready: {'✓' if status['docker_ready'] else '✗'}")

        elif cmd == 'launch':
            print("Launching Docker Desktop...")
            if recovery.launch_docker_desktop():
                print("✓ Launch initiated")
            else:
                print("✗ Launch failed")

        elif cmd == 'wait':
            timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
            print(f"Waiting for Docker (timeout={timeout}s)...")

            async def wait():
                return await recovery.wait_for_docker_ready(timeout=timeout)

            ready = asyncio.run(wait())
            print("✓ Docker ready" if ready else "✗ Timeout")

        elif cmd == 'ensure':
            timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
            print(f"Ensuring Docker ready (timeout={timeout}s)...")

            async def ensure():
                return await recovery.ensure_docker_ready(timeout=timeout)

            ready = asyncio.run(ensure())
            print("✓ Docker ready" if ready else "✗ Failed")

        elif cmd == 'restart':
            timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
            print(f"Restarting Docker Desktop (timeout={timeout}s)...")

            async def restart():
                return await recovery.restart_docker_desktop(timeout=timeout)

            ready = asyncio.run(restart())
            print("✓ Docker ready" if ready else "✗ Failed")

        elif cmd == 'diagnose':
            # Detailed diagnosis
            print("Diagnosing Docker state...")
            diag = recovery.diagnose_docker_state()

            print(f"\n=== Docker Diagnosis ===")
            print(f"State: {diag['state'].upper()}")
            print(f"Recommended: {diag['recommended_action']}")
            print(f"Reason: {diag['action_reason']}")

            print(f"\n=== Checks ===")
            for name, check in diag['checks'].items():
                icon = '✓' if check['passed'] else '✗'
                print(f"  {icon} {name}: {check['detail']}")

            print(f"\n=== Docker Processes ===")
            procs = recovery.get_docker_processes()
            if procs:
                for p in procs[:10]:
                    if 'command' in p:
                        print(f"  PID {p['pid']}: {p['command'][:60]}")
                    else:
                        print(f"  {p.get('line', p)}")
            else:
                print("  No Docker processes found")

        elif cmd == 'recover':
            # Smart recovery
            timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
            print(f"Smart Docker recovery (timeout={timeout}s)...")

            async def recover():
                return await recovery.smart_recover(timeout=timeout)

            result = asyncio.run(recover())

            diag = result['diagnosis']
            print(f"\n=== Initial State ===")
            print(f"State: {diag['state']}")
            print(f"Action: {result['action_taken']}")

            print(f"\n=== Result ===")
            print(f"Success: {'✓' if result['success'] else '✗'}")
            print(f"Details: {result['details']}")

        elif cmd == 'quit':
            print("Quitting Docker Desktop...")
            if recovery.quit_docker_desktop():
                print("✓ Quit initiated")
            else:
                print("✗ Quit failed")

        elif cmd == 'capture':
            # Capture current Docker environment
            db_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.home() / '.contextdna/context_dna.db'
            print("Capturing Docker environment...")

            env = recovery.capture_environment()
            if env:
                print(f"\n=== Captured Environment ===")
                print(f"Platform: {env.get('platform')} ({env.get('architecture')})")
                print(f"Docker Client: {env.get('docker_client_version')}")
                print(f"Docker Server: {env.get('docker_server_version')}")
                print(f"Docker API: {env.get('docker_api_version')}")
                print(f"App Path: {env.get('docker_app_path')}")
                print(f"CLI Path: {env.get('docker_cli_path')}")
                print(f"Socket: {env.get('docker_socket_path')}")
                print(f"Runtime: {env.get('runtime_type')}")
                print(f"Memory: {env.get('memory_total_bytes', 0) / (1024**3):.1f} GB")
                print(f"CPUs: {env.get('cpu_count')}")

                # Save to SQLite
                if db_path.parent.exists():
                    if recovery.save_environment_to_sqlite(db_path, env, launch_method='open -a Docker'):
                        print(f"\n✓ Saved to {db_path}")
                    else:
                        print(f"\n✗ Failed to save to {db_path}")
                else:
                    print(f"\n⚠ DB directory doesn't exist: {db_path.parent}")
                    print(json.dumps(env, indent=2))
            else:
                print("✗ Failed to capture (is Docker running?)")

        elif cmd == 'cached':
            # Show cached Docker environment
            db_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.home() / '.contextdna/context_dna.db'
            print(f"Loading cached environment from {db_path}...")

            env = recovery.load_environment_from_sqlite(db_path)
            if env:
                print(f"\n=== Cached Environment ===")
                print(f"Platform: {env.get('platform')} ({env.get('architecture')})")
                print(f"Docker Client: {env.get('docker_client_version')}")
                print(f"Docker Server: {env.get('docker_server_version')}")
                print(f"App Path: {env.get('docker_app_path')}")
                print(f"CLI Path: {env.get('docker_cli_path')}")
                print(f"\n=== Recovery Info ===")
                print(f"Last Launch Method: {env.get('successful_launch_method') or 'None'}")
                print(f"Last Successful Launch: {env.get('last_successful_launch_at') or 'Never'}")
                print(f"Launch Success Count: {env.get('launch_success_count', 0)}")
                print(f"Recovery Attempts: {env.get('recovery_attempts_total', 0)}")
                print(f"Recovery Successes: {env.get('recovery_successes_total', 0)}")
                print(f"Consecutive Failures: {env.get('consecutive_failures', 0)}")
                print(f"Last Healthy: {env.get('last_healthy_at') or 'Unknown'}")
            else:
                print("✗ No cached environment found")

        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python docker_recovery.py <command> [timeout|db_path]")
            print("\nCommands:")
            print("  status   - Quick Docker status check")
            print("  diagnose - Detailed state analysis with recommendations")
            print("  recover  - Smart recovery (auto-selects best action)")
            print("  launch   - Launch Docker Desktop")
            print("  quit     - Quit Docker Desktop gracefully")
            print("  restart  - Quit and relaunch Docker Desktop")
            print("  wait     - Wait for Docker daemon to be ready")
            print("  ensure   - Launch if needed and wait for ready")
            print("  capture  - Capture Docker environment to SQLite")
            print("  cached   - Show cached Docker environment")
            print("\nExamples:")
            print("  python docker_recovery.py diagnose     # See what's wrong")
            print("  python docker_recovery.py recover 120  # Auto-fix with 2min timeout")
    else:
        # Default: show status
        status = recovery.get_status()
        print(f"Docker Status on {status['platform']}:")
        print(f"  Daemon: {'✓' if status['daemon_running'] else '✗'}")
        print(f"  App: {'✓' if status['app_running'] else '✗'}")
