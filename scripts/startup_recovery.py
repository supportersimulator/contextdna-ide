#!/usr/bin/env python3
"""
Context DNA Startup Recovery Agent
===================================

Runs at system startup/login to recover Context DNA critical apps.
Designed to be run via launchd agent.

Features:
- Checks which apps are marked as auto_recovery_enabled
- Recovers apps in startup_order (lower = earlier)
- Logs all recovery attempts
- Respects machine_id for multi-machine support
- Graceful degradation if database not available

Installation:
    # Copy plist to LaunchAgents
    cp scripts/launchd/com.contextdna.recovery.plist ~/Library/LaunchAgents/

    # Load the agent
    launchctl load ~/Library/LaunchAgents/com.contextdna.recovery.plist

    # Check status
    launchctl list | grep contextdna

Uninstall:
    launchctl unload ~/Library/LaunchAgents/com.contextdna.recovery.plist
    rm ~/Library/LaunchAgents/com.contextdna.recovery.plist
"""

import json
import logging
import os
import platform
import sqlite3
import subprocess
import sys
import time
import urllib.request
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

# Setup logging
LOG_FILE = Path("/tmp/contextdna-recovery.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Paths
DB_PATH = Path.home() / ".context-dna" / "context_dna.db"
SUPERREPO = Path(__file__).parent.parent
PREFS_FILE = Path.home() / ".context-dna" / "recovery_prefs.json"

# Docker Desktop download URLs
DOCKER_DOWNLOADS = {
    "darwin_arm64": "https://desktop.docker.com/mac/main/arm64/Docker.dmg",
    "darwin_x86_64": "https://desktop.docker.com/mac/main/amd64/Docker.dmg",
    "windows_amd64": "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe",
}

# Default preferences
DEFAULT_PREFS = {
    "enabled": True,
    "auto_start_docker": True,
    "auto_download_docker": True,  # Download Docker if not installed
    "auto_start_critical_apps": True,
    "startup_delay_seconds": 10,  # Wait for system to stabilize
    "max_recovery_attempts": 3,
    "recovery_timeout_seconds": 120,
    "notify_on_failure": True,
    "apps_to_skip": [],  # User can disable specific apps
    "check_system_requirements": True,  # Check for required apps on startup
    "first_run_completed": False,  # Track if first run check done
}

# Required components for Context DNA
REQUIRED_COMPONENTS = {
    "python": {
        "name": "Python 3.10+",
        "check_cmd": ["python3", "--version"],
        "min_version": "3.10",
        "install_cmd_macos": "brew install python@3.12",
        "install_cmd_windows": "winget install Python.Python.3.12",
        "critical": True,
    },
    "git": {
        "name": "Git",
        "check_cmd": ["git", "--version"],
        "install_cmd_macos": "brew install git",
        "install_cmd_windows": "winget install Git.Git",
        "critical": True,
    },
    "docker": {
        "name": "Docker Desktop",
        "check_func": "is_docker_installed",
        "install_url_macos": "https://desktop.docker.com/mac/main/arm64/Docker.dmg",
        "critical": False,
    },
    "node": {
        "name": "Node.js 18+",
        "check_cmd": ["node", "--version"],
        "min_version": "18",
        "install_cmd_macos": "brew install node@20",
        "install_cmd_windows": "winget install OpenJS.NodeJS.LTS",
        "critical": False,
    },
    "homebrew": {
        "name": "Homebrew",
        "check_cmd": ["brew", "--version"],
        "install_url": "https://brew.sh",
        "macos_only": True,
        "critical": False,
    },
    "xbar": {
        "name": "xbar (menu bar)",
        "check_path_macos": "/Applications/xbar.app",
        "install_cmd_macos": "brew install --cask xbar",
        "macos_only": True,
        "critical": False,
    },
}


def ask_permission_dialog(title: str, message: str) -> bool:
    """Show macOS dialog asking for permission. Returns True if user clicks OK."""
    if platform.system() != "Darwin":
        return True  # Auto-grant on non-macOS

    try:
        script = f'''
        tell application "System Events"
            display dialog "{message}" with title "{title}" buttons {{"Skip", "Check Now"}} default button "Check Now"
            set userChoice to button returned of result
            if userChoice is "Check Now" then
                return "yes"
            else
                return "no"
            end if
        end tell
        '''
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=60
        )
        return result.stdout.strip() == "yes"
    except Exception as e:
        logger.warning(f"Permission dialog failed: {e}")
        return True  # Default to checking if dialog fails


def get_system_info() -> Dict[str, Any]:
    """Get comprehensive system information."""
    info = {
        "platform": platform.system(),
        "platform_version": platform.version(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "hostname": platform.node(),
    }

    # RAM (macOS)
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ['sysctl', '-n', 'hw.memsize'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                ram_bytes = int(result.stdout.strip())
                info["ram_gb"] = round(ram_bytes / (1024**3), 1)
        except Exception as e:
            logger.debug(f"Failed to get RAM info: {e}")

        # Chip info
        try:
            result = subprocess.run(
                ['sysctl', '-n', 'machdep.cpu.brand_string'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                info["cpu"] = result.stdout.strip()
            else:
                # Apple Silicon
                result = subprocess.run(
                    ['sysctl', '-n', 'hw.optional.arm64'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip() == "1":
                    info["cpu"] = "Apple Silicon"
                    info["is_apple_silicon"] = True
        except Exception as e:
            logger.debug(f"Failed to get CPU info: {e}")

    return info


def check_component(name: str, config: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
    """
    Check if a component is installed.
    Returns: (is_installed, version_or_path, error_message)
    """
    # Skip if platform-specific and not matching
    if config.get("macos_only") and platform.system() != "Darwin":
        return True, "N/A (not macOS)", None
    if config.get("windows_only") and platform.system() != "Windows":
        return True, "N/A (not Windows)", None

    # Check by path
    check_path = config.get(f"check_path_{platform.system().lower()}")
    if check_path:
        path = Path(check_path)
        if path.exists():
            return True, str(path), None
        return False, None, f"Not found at {check_path}"

    # Check by function
    if config.get("check_func") == "is_docker_installed":
        installed, path = is_docker_installed()
        if installed:
            return True, path, None
        return False, None, "Docker Desktop not installed"

    # Check by command
    check_cmd = config.get("check_cmd")
    if check_cmd:
        try:
            result = subprocess.run(
                check_cmd,
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                version = result.stdout.strip() or result.stderr.strip()
                # Check minimum version if specified
                min_version = config.get("min_version")
                if min_version:
                    import re
                    version_match = re.search(r'(\d+)\.(\d+)', version)
                    if version_match:
                        major, minor = int(version_match.group(1)), int(version_match.group(2))
                        min_major, min_minor = map(int, min_version.split('.'))
                        if major < min_major or (major == min_major and minor < min_minor):
                            return False, version, f"Version {version} < required {min_version}"
                return True, version, None
            return False, None, f"Command failed: {result.stderr.strip()}"
        except FileNotFoundError:
            return False, None, "Command not found"
        except Exception as e:
            return False, None, str(e)

    return True, "Unknown", None


def check_all_requirements() -> Dict[str, Dict[str, Any]]:
    """Check all required components and return results."""
    results = {}

    for name, config in REQUIRED_COMPONENTS.items():
        installed, version, error = check_component(name, config)
        results[name] = {
            "name": config["name"],
            "installed": installed,
            "version": version,
            "error": error,
            "critical": config.get("critical", False),
            "install_cmd": config.get(f"install_cmd_{platform.system().lower()}"),
            "install_url": config.get(f"install_url_{platform.system().lower()}") or config.get("install_url"),
        }

    return results


def save_requirements_check_to_db(machine_id: str, system_info: Dict[str, Any], requirements: Dict[str, Dict[str, Any]]):
    """Save system requirements check to SQLite."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        # Create system_info table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_requirements_check (
                id INTEGER PRIMARY KEY,
                machine_id TEXT NOT NULL UNIQUE,
                platform TEXT,
                platform_version TEXT,
                architecture TEXT,
                ram_gb REAL,
                cpu TEXT,
                is_apple_silicon INTEGER,
                python_installed INTEGER,
                python_version TEXT,
                git_installed INTEGER,
                git_version TEXT,
                docker_installed INTEGER,
                docker_path TEXT,
                node_installed INTEGER,
                node_version TEXT,
                homebrew_installed INTEGER,
                xbar_installed INTEGER,
                xbar_path TEXT,
                all_critical_met INTEGER,
                check_timestamp TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Check if all critical requirements are met
        all_critical_met = all(
            r["installed"] for r in requirements.values() if r["critical"]
        )

        cursor.execute("""
            INSERT INTO system_requirements_check (
                machine_id, platform, platform_version, architecture, ram_gb, cpu, is_apple_silicon,
                python_installed, python_version, git_installed, git_version,
                docker_installed, docker_path, node_installed, node_version,
                homebrew_installed, xbar_installed, xbar_path,
                all_critical_met, check_timestamp, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(machine_id) DO UPDATE SET
                platform = excluded.platform,
                platform_version = excluded.platform_version,
                architecture = excluded.architecture,
                ram_gb = excluded.ram_gb,
                cpu = excluded.cpu,
                is_apple_silicon = excluded.is_apple_silicon,
                python_installed = excluded.python_installed,
                python_version = excluded.python_version,
                git_installed = excluded.git_installed,
                git_version = excluded.git_version,
                docker_installed = excluded.docker_installed,
                docker_path = excluded.docker_path,
                node_installed = excluded.node_installed,
                node_version = excluded.node_version,
                homebrew_installed = excluded.homebrew_installed,
                xbar_installed = excluded.xbar_installed,
                xbar_path = excluded.xbar_path,
                all_critical_met = excluded.all_critical_met,
                check_timestamp = excluded.check_timestamp,
                updated_at = excluded.updated_at
        """, (
            machine_id,
            system_info.get("platform"),
            system_info.get("platform_version"),
            system_info.get("architecture"),
            system_info.get("ram_gb"),
            system_info.get("cpu"),
            1 if system_info.get("is_apple_silicon") else 0,
            1 if requirements.get("python", {}).get("installed") else 0,
            requirements.get("python", {}).get("version"),
            1 if requirements.get("git", {}).get("installed") else 0,
            requirements.get("git", {}).get("version"),
            1 if requirements.get("docker", {}).get("installed") else 0,
            requirements.get("docker", {}).get("version"),
            1 if requirements.get("node", {}).get("installed") else 0,
            requirements.get("node", {}).get("version"),
            1 if requirements.get("homebrew", {}).get("installed") else 0,
            1 if requirements.get("xbar", {}).get("installed") else 0,
            requirements.get("xbar", {}).get("version"),
            1 if all_critical_met else 0,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ))

        conn.commit()
        conn.close()
        logger.info("✓ Saved system requirements check to SQLite")
        return True
    except Exception as e:
        logger.warning(f"Failed to save requirements check: {e}")
        return False


def show_requirements_report(system_info: Dict[str, Any], requirements: Dict[str, Dict[str, Any]]) -> bool:
    """Show a report dialog with system requirements status. Returns True if all critical met."""
    # Build report
    report_lines = [
        f"System: {system_info.get('platform')} {system_info.get('architecture')}",
        f"RAM: {system_info.get('ram_gb', 'Unknown')} GB",
        f"CPU: {system_info.get('cpu', 'Unknown')}",
        "",
        "Components:",
    ]

    missing_critical = []
    missing_optional = []

    for name, result in requirements.items():
        if result["installed"]:
            status = "✓"
            version = result["version"] if result["version"] else ""
            report_lines.append(f"  {status} {result['name']}: {version}")
        else:
            status = "✗"
            report_lines.append(f"  {status} {result['name']}: NOT INSTALLED")
            if result["critical"]:
                missing_critical.append(result["name"])
            else:
                missing_optional.append(result["name"])

    # Log the report
    logger.info("\n=== System Requirements Check ===")
    for line in report_lines:
        logger.info(line)

    if missing_critical:
        logger.warning(f"Missing CRITICAL: {', '.join(missing_critical)}")
    if missing_optional:
        logger.info(f"Missing optional: {', '.join(missing_optional)}")

    # Show notification summary
    if missing_critical:
        send_notification(
            "Context DNA - Missing Requirements",
            f"Missing critical: {', '.join(missing_critical)}"
        )
        return False
    elif missing_optional:
        send_notification(
            "Context DNA",
            f"Optional: {', '.join(missing_optional)} not installed"
        )
        return True
    else:
        send_notification(
            "Context DNA",
            "All system requirements met ✓"
        )
        return True


def run_system_requirements_check(machine_id: str, prefs: Dict[str, Any], force: bool = False) -> bool:
    """
    Run system requirements check if enabled.
    Returns True if all critical requirements are met.
    """
    # Skip if disabled
    if not prefs.get("check_system_requirements", True):
        logger.info("System requirements check disabled in preferences")
        return True

    # Skip if already done (unless forced)
    if prefs.get("first_run_completed", False) and not force:
        logger.info("System requirements check already completed (use force=True to re-run)")
        return True

    logger.info("=== System Requirements Check ===")

    # Ask permission (first run only)
    if not prefs.get("first_run_completed", False):
        if not ask_permission_dialog(
            "Context DNA Setup",
            "Context DNA would like to check your computer for required apps (Python, Docker, Git, etc.).\n\nThis helps ensure everything is set up correctly."
        ):
            logger.info("User skipped requirements check")
            return True

    # Get system info
    logger.info("Gathering system information...")
    system_info = get_system_info()
    logger.info(f"Platform: {system_info.get('platform')} {system_info.get('architecture')}")
    logger.info(f"RAM: {system_info.get('ram_gb', 'Unknown')} GB")

    # Check all requirements
    logger.info("Checking required components...")
    requirements = check_all_requirements()

    # Save to SQLite
    save_requirements_check_to_db(machine_id, system_info, requirements)

    # Show report
    all_critical_met = show_requirements_report(system_info, requirements)

    # Mark first run as completed
    if not prefs.get("first_run_completed", False):
        prefs["first_run_completed"] = True
        save_preferences(prefs)

    return all_critical_met


def is_docker_installed() -> Tuple[bool, Optional[str]]:
    """Check if Docker Desktop is installed and return its location."""
    # macOS: Check for Docker.app
    if platform.system() == "Darwin":
        app_paths = [
            Path("/Applications/Docker.app"),
            Path.home() / "Applications" / "Docker.app",
        ]
        for path in app_paths:
            if path.exists():
                return True, str(path)

    # Windows: Check common install locations
    elif platform.system() == "Windows":
        win_paths = [
            Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Docker" / "Docker Desktop.exe",
        ]
        for path in win_paths:
            if path.exists():
                return True, str(path)

    # Check if docker CLI is available (fallback)
    try:
        result = subprocess.run(['which', 'docker'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return True, result.stdout.strip()
    except Exception as e:
        logger.debug(f"Failed to check docker CLI via 'which': {e}")

    return False, None


def save_docker_location_to_db(machine_id: str, docker_path: str):
    """Save Docker installation location to SQLite for recovery use."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        # Create table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS docker_environment (
                id INTEGER PRIMARY KEY,
                machine_id TEXT NOT NULL UNIQUE,
                docker_app_path TEXT,
                docker_cli_path TEXT,
                platform TEXT,
                architecture TEXT,
                last_successful_launch_at TEXT,
                launch_success_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Get docker CLI path
        docker_cli = None
        try:
            result = subprocess.run(['which', 'docker'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                docker_cli = result.stdout.strip()
        except Exception as e:
            logger.debug(f"Failed to find docker CLI path: {e}")

        # Upsert the record
        cursor.execute("""
            INSERT INTO docker_environment (machine_id, docker_app_path, docker_cli_path, platform, architecture, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(machine_id) DO UPDATE SET
                docker_app_path = excluded.docker_app_path,
                docker_cli_path = excluded.docker_cli_path,
                platform = excluded.platform,
                architecture = excluded.architecture,
                updated_at = excluded.updated_at
        """, (
            machine_id,
            docker_path,
            docker_cli,
            platform.system(),
            platform.machine(),
            datetime.now(timezone.utc).isoformat()
        ))

        conn.commit()
        conn.close()
        logger.info(f"✓ Saved Docker location to SQLite: {docker_path}")
        return True
    except Exception as e:
        logger.warning(f"Failed to save Docker location to DB: {e}")
        return False


def get_docker_download_key() -> Optional[str]:
    """Get the download key for current platform."""
    system = platform.system().lower()
    arch = platform.machine()

    if system == "darwin":
        if arch == "arm64":
            return "darwin_arm64"
        else:
            return "darwin_x86_64"
    elif system == "windows":
        return "windows_amd64"

    return None


def download_docker_desktop() -> bool:
    """Download and install Docker Desktop if not already installed."""
    installed, path = is_docker_installed()
    if installed:
        logger.info(f"✓ Docker Desktop already installed at: {path}")
        return True

    download_key = get_docker_download_key()
    if not download_key or download_key not in DOCKER_DOWNLOADS:
        logger.warning(f"Docker Desktop download not available for this platform ({platform.system()} {platform.machine()})")
        return False

    url = DOCKER_DOWNLOADS[download_key]
    logger.info(f"Docker Desktop not found. Downloading from: {url}")

    try:
        # Create temp directory for download
        temp_dir = Path(tempfile.mkdtemp())

        if platform.system() == "Darwin":
            # Download DMG
            dmg_path = temp_dir / "Docker.dmg"
            logger.info("Downloading Docker Desktop for macOS...")

            # Use curl for better progress/resume support
            result = subprocess.run([
                'curl', '-L', '-o', str(dmg_path),
                '--progress-bar', url
            ], timeout=600)  # 10 min timeout for download

            if result.returncode != 0 or not dmg_path.exists():
                logger.error("Docker download failed")
                return False

            logger.info("Download complete. Mounting DMG...")

            # Mount DMG
            result = subprocess.run([
                'hdiutil', 'attach', str(dmg_path), '-quiet'
            ], capture_output=True, timeout=60)

            if result.returncode != 0:
                logger.error("Failed to mount Docker DMG")
                return False

            logger.info("Installing Docker Desktop to /Applications...")

            # Copy Docker.app to Applications
            result = subprocess.run([
                'cp', '-R', '/Volumes/Docker/Docker.app', '/Applications/'
            ], timeout=120)

            # Unmount
            subprocess.run(['hdiutil', 'detach', '/Volumes/Docker', '-quiet'], timeout=30)

            # Clean up
            dmg_path.unlink()

            if result.returncode == 0:
                logger.info("✓ Docker Desktop installed successfully")
                send_notification("Context DNA", "Docker Desktop installed successfully")
                return True
            else:
                logger.error("Failed to copy Docker.app to Applications")
                return False

        elif platform.system() == "Windows":
            # Download installer
            exe_path = temp_dir / "Docker Desktop Installer.exe"
            logger.info("Downloading Docker Desktop for Windows...")

            urllib.request.urlretrieve(url, str(exe_path))

            if not exe_path.exists():
                logger.error("Docker download failed")
                return False

            logger.info("Running Docker Desktop installer...")

            # Run installer silently
            result = subprocess.run([
                str(exe_path), 'install', '--quiet', '--accept-license'
            ], timeout=600)

            # Clean up
            exe_path.unlink()

            if result.returncode == 0:
                logger.info("✓ Docker Desktop installed successfully")
                send_notification("Context DNA", "Docker Desktop installed successfully")
                return True
            else:
                logger.error("Docker installer failed")
                return False

    except subprocess.TimeoutExpired:
        logger.error("Docker download/install timed out")
        return False
    except Exception as e:
        logger.error(f"Docker download/install error: {e}")
        return False

    return False


def load_preferences() -> Dict[str, Any]:
    """Load recovery preferences, creating defaults if needed."""
    if PREFS_FILE.exists():
        try:
            return {**DEFAULT_PREFS, **json.loads(PREFS_FILE.read_text())}
        except Exception as e:
            logger.warning(f"Failed to load preferences: {e}")
    return DEFAULT_PREFS


def save_preferences(prefs: Dict[str, Any]):
    """Save recovery preferences."""
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(prefs, indent=2))


def get_machine_id() -> str:
    """Get unique machine identifier."""
    try:
        result = subprocess.run(
            ['ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split('\n'):
            if 'IOPlatformUUID' in line:
                return line.split('"')[-2]
    except Exception as e:
        logger.debug(f"Failed to get IOPlatformUUID, falling back to hostname: {e}")
    import platform
    return platform.node()


def get_critical_apps(machine_id: str) -> List[Dict[str, Any]]:
    """Get apps marked for auto-recovery from SQLite."""
    if not DB_PATH.exists():
        logger.info(f"Database not found: {DB_PATH}")
        return []

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Get apps that are enabled for auto-recovery
        cursor.execute("""
            SELECT * FROM app_recovery_configs
            WHERE machine_id = ?
              AND auto_recovery_enabled = 1
            ORDER BY startup_order ASC
        """, (machine_id,))

        apps = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return apps

    except Exception as e:
        logger.warning(f"Failed to query apps: {e}")
        return []


def check_app_health(app: Dict[str, Any]) -> bool:
    """Check if an app is healthy."""
    method = app.get('health_check_method', 'process')
    endpoint = app.get('health_check_endpoint')
    timeout = (app.get('health_check_timeout_ms') or 3000) / 1000

    if method == 'http' and endpoint:
        try:
            import urllib.request
            req = urllib.request.Request(endpoint, method='GET')
            urllib.request.urlopen(req, timeout=timeout)
            return True
        except Exception as e:
            logger.debug(f"HTTP health check failed for {endpoint}: {e}")
            return False

    elif method == 'tcp' and endpoint:
        try:
            import socket
            host, port = endpoint.split(':')
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, int(port)))
            sock.close()
            return result == 0
        except Exception as e:
            logger.debug(f"TCP health check failed for {endpoint}: {e}")
            return False

    else:
        # Process check - look for app name
        app_name = app.get('app_name', '')
        try:
            result = subprocess.run(
                ['pgrep', '-f', app_name],
                capture_output=True, timeout=5
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Process health check failed for {app_name}: {e}")
            return False


def recover_app(app: Dict[str, Any]) -> bool:
    """Attempt to recover an app using cached config."""
    app_name = app.get('app_name', 'unknown')
    launch_command = app.get('launch_command')

    if not launch_command:
        logger.warning(f"No launch command for {app_name}")
        return False

    logger.info(f"Recovering {app_name}: {launch_command}")

    try:
        # Execute launch command
        if launch_command.startswith('open'):
            subprocess.Popen(launch_command, shell=True)
        else:
            subprocess.Popen(launch_command.split(), start_new_session=True)

        # Wait for app to start
        startup_timeout = (app.get('startup_timeout_ms') or 60000) / 1000
        start_time = time.time()

        while time.time() - start_time < startup_timeout:
            if check_app_health(app):
                logger.info(f"✓ {app_name} recovered successfully")
                record_recovery_result(app_name, True)
                return True
            time.sleep(2)

        logger.warning(f"✗ {app_name} failed to start within {startup_timeout}s")
        record_recovery_result(app_name, False)
        return False

    except Exception as e:
        logger.error(f"✗ {app_name} recovery error: {e}")
        record_recovery_result(app_name, False)
        return False


def record_recovery_result(app_name: str, success: bool):
    """Record recovery attempt to database."""
    if not DB_PATH.exists():
        return

    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE app_recovery_configs
            SET last_recovery_at = ?,
                recovery_attempts = recovery_attempts + 1,
                recovery_successes = recovery_successes + ?,
                consecutive_failures = CASE WHEN ? = 1 THEN 0 ELSE consecutive_failures + 1 END,
                last_healthy_at = CASE WHEN ? = 1 THEN ? ELSE last_healthy_at END,
                updated_at = ?
            WHERE app_name = ?
        """, (
            datetime.now(timezone.utc).isoformat(),
            1 if success else 0,
            1 if success else 0,
            1 if success else 0,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            app_name
        ))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to record recovery result: {e}")


def recover_docker(machine_id: str, prefs: Dict[str, Any]) -> bool:
    """Special handling for Docker Desktop recovery with download capability."""
    logger.info("Checking Docker status...")

    # First check if Docker is installed
    installed, docker_path = is_docker_installed()

    if not installed:
        logger.warning("Docker Desktop not installed")

        # Check if auto-download is enabled
        if prefs.get('auto_download_docker', True):
            logger.info("Attempting to download and install Docker Desktop...")
            if download_docker_desktop():
                installed, docker_path = is_docker_installed()
            else:
                logger.error("✗ Failed to download Docker Desktop")
                send_notification("Context DNA", "Failed to download Docker Desktop. Please install manually from docker.com")
                return False
        else:
            logger.info("Docker auto-download disabled in preferences")
            return False

    # Save Docker location to SQLite for future recovery use
    if docker_path:
        save_docker_location_to_db(machine_id, docker_path)

    # Check if Docker daemon is already running
    try:
        result = subprocess.run(
            ['docker', 'info'],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            logger.info("✓ Docker already running")
            # Update successful launch timestamp
            update_docker_success(machine_id)
            return True
    except subprocess.TimeoutExpired:
        logger.info("Docker daemon slow/unresponsive")
    except FileNotFoundError:
        logger.warning("Docker CLI not found")
        return False
    except Exception as e:
        logger.info(f"Docker check failed: {e}")

    # Try to launch Docker Desktop
    logger.info("Launching Docker Desktop...")
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(['open', '-a', 'Docker'])
        elif platform.system() == "Windows":
            # Windows: Launch Docker Desktop
            if docker_path:
                subprocess.Popen([docker_path])
            else:
                subprocess.Popen(['start', 'Docker Desktop'], shell=True)

        # Wait for Docker to be ready
        for i in range(60):  # Wait up to 2 minutes
            time.sleep(2)
            try:
                result = subprocess.run(
                    ['docker', 'info'],
                    capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    logger.info(f"✓ Docker ready after {(i+1)*2}s")
                    update_docker_success(machine_id)
                    return True
            except Exception as e:
                logger.debug(f"Docker not ready yet (attempt {i+1}): {e}")

            if i % 10 == 9:
                logger.info(f"Still waiting for Docker... ({(i+1)*2}s)")

        logger.warning("✗ Docker failed to start within 2 minutes")
        return False

    except Exception as e:
        logger.error(f"✗ Docker launch error: {e}")
        return False


def update_docker_success(machine_id: str):
    """Update Docker success metrics in SQLite."""
    if not DB_PATH.exists():
        return

    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE docker_environment
            SET last_successful_launch_at = ?,
                launch_success_count = launch_success_count + 1,
                updated_at = ?
            WHERE machine_id = ?
        """, (
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            machine_id
        ))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to update Docker success metrics: {e}")


def send_notification(title: str, message: str):
    """Send macOS notification."""
    try:
        subprocess.run([
            'osascript', '-e',
            f'display notification "{message}" with title "{title}"'
        ], timeout=5)
    except Exception as e:
        logger.debug(f"Failed to send notification '{title}': {e}")


def main():
    """Main startup recovery routine."""
    logger.info("=" * 60)
    logger.info("Context DNA Startup Recovery Agent")
    logger.info("=" * 60)

    # Load preferences
    prefs = load_preferences()

    if not prefs.get('enabled', True):
        logger.info("Recovery agent disabled in preferences")
        return 0

    # Wait for system to stabilize
    delay = prefs.get('startup_delay_seconds', 10)
    logger.info(f"Waiting {delay}s for system to stabilize...")
    time.sleep(delay)

    machine_id = get_machine_id()
    logger.info(f"Machine ID: {machine_id}")

    # Step 0: System requirements check (first run or forced)
    if prefs.get('check_system_requirements', True):
        all_critical_met = run_system_requirements_check(machine_id, prefs)
        if not all_critical_met:
            logger.warning("Critical requirements not met - recovery may be limited")
            send_notification(
                "Context DNA",
                "Some critical components are missing. Please install them for full functionality."
            )

    results = {"success": [], "failed": [], "skipped": []}
    skip_apps = prefs.get('apps_to_skip', [])

    # Step 1: Docker recovery (if enabled)
    if prefs.get('auto_start_docker', True):
        if 'docker' not in skip_apps:
            if recover_docker(machine_id, prefs):
                results['success'].append('docker')
            else:
                results['failed'].append('docker')
        else:
            logger.info("Docker skipped (in skip list)")
            results['skipped'].append('docker')
    else:
        logger.info("Docker auto-start disabled")

    # Step 2: Recover other critical apps
    if prefs.get('auto_start_critical_apps', True):
        apps = get_critical_apps(machine_id)
        logger.info(f"Found {len(apps)} apps configured for recovery")

        for app in apps:
            app_name = app.get('app_name', 'unknown')

            if app_name in skip_apps:
                logger.info(f"Skipping {app_name} (in skip list)")
                results['skipped'].append(app_name)
                continue

            if app_name == 'docker':
                # Already handled above
                continue

            if check_app_health(app):
                logger.info(f"✓ {app_name} already healthy")
                results['success'].append(app_name)
                continue

            if recover_app(app):
                results['success'].append(app_name)
            else:
                results['failed'].append(app_name)

    # Summary
    logger.info("")
    logger.info("=" * 40)
    logger.info("RECOVERY SUMMARY")
    logger.info("=" * 40)
    logger.info(f"Success: {len(results['success'])} - {', '.join(results['success']) or 'none'}")
    logger.info(f"Failed:  {len(results['failed'])} - {', '.join(results['failed']) or 'none'}")
    logger.info(f"Skipped: {len(results['skipped'])} - {', '.join(results['skipped']) or 'none'}")

    # Notify on failure if enabled
    if results['failed'] and prefs.get('notify_on_failure', True):
        send_notification(
            "Context DNA Recovery",
            f"Failed to start: {', '.join(results['failed'])}"
        )

    return 1 if results['failed'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        logger.error(f"Startup recovery failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
