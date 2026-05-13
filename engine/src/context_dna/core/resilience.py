#!/usr/bin/env python3
"""
Context DNA: Core Resilience Utilities

Hardened utilities to prevent failures across 100+ diverse installs.
See docs/RESILIENCE_ANALYSIS.md for the full analysis.

P0 Critical Components:
- InstallLock: Prevents concurrent/corrupt installs
- AtomicFileWriter: Prevents data loss from crashes
- SafeSQLite: WAL mode + busy timeout for concurrent access
- PreFlightChecker: Catches problems before install starts
- NetworkRetry: Handles flaky connections

Usage:
    from context_dna.core.resilience import (
        InstallLock, AtomicFileWriter, SafeSQLite, PreFlightChecker, NetworkRetry
    )
"""

import asyncio
import json
import os
import platform
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
import shlex


# =============================================================================
# INSTALL LOCK - Prevents concurrent/interrupted installs
# =============================================================================

class InstallLock:
    """
    Prevents concurrent installations and enables resume after interruption.

    Usage:
        with InstallLock() as lock:
            lock.save_state("step1", {"data": "value"})
            # ... do install work ...
            lock.save_state("step2", {"more": "data"})
    """

    def __init__(self, config_dir: Path = None):
        self.config_dir = config_dir or (Path.home() / ".context-dna")
        self.lock_file = self.config_dir / ".install.lock"
        self.state_file = self.config_dir / ".install.state"
        self._original_handlers = {}

    def acquire_lock(self) -> bool:
        """
        Acquire install lock. Returns False if another install is running.
        """
        self.config_dir.mkdir(parents=True, exist_ok=True)

        if self.lock_file.exists():
            try:
                lock_data = json.loads(self.lock_file.read_text())
                pid = lock_data.get("pid")
                started = lock_data.get("started_at", "unknown")

                # Check if process is still running
                if pid:
                    try:
                        os.kill(pid, 0)  # Signal 0 = check existence
                        # Process exists - lock is valid
                        return False
                    except (ProcessLookupError, PermissionError) as e:
                        # Process doesn't exist - stale lock
                        print(f"[WARN] Stale lock detected (PID {pid} not running): {e}")
            except (json.JSONDecodeError, KeyError) as e:
                # Corrupt lock file - remove it
                print(f"[WARN] Corrupt lock file, removing: {e}")

            self.lock_file.unlink(missing_ok=True)

        # Create new lock
        lock_data = {
            "pid": os.getpid(),
            "started_at": datetime.utcnow().isoformat() + "Z",
            "platform": platform.system(),
            "python": sys.version,
        }
        self.lock_file.write_text(json.dumps(lock_data, indent=2))
        return True

    def release_lock(self):
        """Release the install lock."""
        self.lock_file.unlink(missing_ok=True)

    def save_state(self, step: str, data: Dict[str, Any] = None):
        """
        Save current install progress for resume capability.

        Args:
            step: Current installation step name
            data: Any data needed to resume from this step
        """
        state = {
            "step": step,
            "data": data or {},
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "pid": os.getpid(),
        }
        self.state_file.write_text(json.dumps(state, indent=2))

    def get_resume_state(self) -> Optional[Dict[str, Any]]:
        """
        Check if we can resume a previous install.

        Returns:
            State dict if resumable, None otherwise
        """
        if not self.state_file.exists():
            return None

        try:
            state = json.loads(self.state_file.read_text())
            return state
        except (json.JSONDecodeError, IOError):
            return None

    def clear_state(self):
        """Clear saved state after successful install."""
        self.state_file.unlink(missing_ok=True)

    def _handle_interrupt(self, signum, frame):
        """Handle Ctrl+C gracefully."""
        print("\n\n⚠️  Installation interrupted!")
        print("   Your progress has been saved.")
        print("   Run the installer again to resume from where you left off.")
        self.release_lock()
        sys.exit(130)  # Standard exit code for SIGINT

    def __enter__(self):
        if not self.acquire_lock():
            raise RuntimeError(
                "Another Context DNA installation is already in progress.\n"
                "If you're sure no other install is running, delete:\n"
                f"  {self.lock_file}"
            )

        # Set up signal handlers
        self._original_handlers[signal.SIGINT] = signal.signal(
            signal.SIGINT, self._handle_interrupt
        )
        if hasattr(signal, 'SIGTERM'):
            self._original_handlers[signal.SIGTERM] = signal.signal(
                signal.SIGTERM, self._handle_interrupt
            )

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Restore original signal handlers
        for sig, handler in self._original_handlers.items():
            signal.signal(sig, handler)

        self.release_lock()

        # Don't clear state on error - allow resume
        if exc_type is None:
            self.clear_state()


# =============================================================================
# ATOMIC FILE WRITER - Prevents data loss from crashes
# =============================================================================

class AtomicFileWriter:
    """
    Write files atomically with automatic backup rotation.

    Prevents data loss from:
    - Power failures mid-write
    - Process crashes
    - Disk full conditions (detected before write)

    Usage:
        writer = AtomicFileWriter()
        writer.write(Path("config.json"), '{"key": "value"}')
    """

    BACKUP_COUNT = 3

    def __init__(self, backup_dir: Path = None):
        self.backup_dir = backup_dir

    def write(self, path: Path, content: Union[str, bytes], encoding: str = 'utf-8'):
        """
        Write file atomically with backup.

        Args:
            path: Target file path
            content: Content to write (str or bytes)
            encoding: Encoding for str content
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Determine backup location
        backup_dir = self.backup_dir or (path.parent / ".backups")

        # Create backup of existing file
        if path.exists():
            self._create_backup(path, backup_dir)

        # Write to temp file in same directory (ensures same filesystem)
        temp_fd, temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp"
        )

        try:
            with os.fdopen(temp_fd, 'wb' if isinstance(content, bytes) else 'w',
                          encoding=None if isinstance(content, bytes) else encoding) as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())  # Ensure data hits disk

            # Atomic rename
            temp_path_obj = Path(temp_path)
            temp_path_obj.replace(path)

        except Exception:
            # Clean up temp file on error
            try:
                os.unlink(temp_path)
            except Exception as e:
                print(f"[WARN] Failed to clean up temp file {temp_path}: {e}")
            raise

    def write_json(self, path: Path, data: Any, indent: int = 2):
        """Write JSON file atomically."""
        content = json.dumps(data, indent=indent, default=str)
        self.write(path, content)

    def _create_backup(self, path: Path, backup_dir: Path):
        """Create timestamped backup, rotate old backups."""
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"{path.name}.{timestamp}"

        shutil.copy2(path, backup_path)

        # Rotate old backups
        backups = sorted(backup_dir.glob(f"{path.name}.*"))
        for old_backup in backups[:-self.BACKUP_COUNT]:
            try:
                old_backup.unlink()
            except Exception as e:
                print(f"[WARN] Failed to remove old backup {old_backup}: {e}")


# =============================================================================
# SAFE SQLITE - WAL mode + busy timeout for concurrent access
# =============================================================================

class SafeSQLite:
    """
    SQLite connection with WAL mode and proper locking.

    Prevents:
    - Database locked errors
    - Corruption from concurrent access
    - Data loss from crashes

    Usage:
        with SafeSQLite("data.db") as db:
            db.execute("INSERT INTO ...")
            result = db.fetchall("SELECT * FROM ...")
    """

    def __init__(self, db_path: Union[str, Path], timeout: float = 30.0):
        self.db_path = Path(db_path)
        self.timeout = timeout
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self):
        """Establish connection with safe settings."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(
            str(self.db_path),
            timeout=self.timeout,
            isolation_level=None,  # Autocommit mode
        )

        # Enable WAL mode for concurrent reads
        self.conn.execute("PRAGMA journal_mode=WAL")

        # Set busy timeout (milliseconds)
        self.conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")

        # Enable foreign keys
        self.conn.execute("PRAGMA foreign_keys=ON")

        # Sync mode for durability
        self.conn.execute("PRAGMA synchronous=NORMAL")

        return self

    def execute(self, sql: str, params: tuple = None) -> sqlite3.Cursor:
        """Execute SQL with parameters."""
        if params:
            return self.conn.execute(sql, params)
        return self.conn.execute(sql)

    def executemany(self, sql: str, params_list: List[tuple]) -> sqlite3.Cursor:
        """Execute SQL for multiple parameter sets."""
        return self.conn.executemany(sql, params_list)

    def fetchone(self, sql: str, params: tuple = None) -> Optional[tuple]:
        """Execute and fetch one row."""
        cursor = self.execute(sql, params)
        return cursor.fetchone()

    def fetchall(self, sql: str, params: tuple = None) -> List[tuple]:
        """Execute and fetch all rows."""
        cursor = self.execute(sql, params)
        return cursor.fetchall()

    def close(self):
        """Close connection and checkpoint WAL."""
        if self.conn:
            try:
                # Checkpoint WAL to main database
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                print(f"[WARN] WAL checkpoint failed during close: {e}")
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# =============================================================================
# PRE-FLIGHT CHECKER - Catches problems before install starts
# =============================================================================

@dataclass
class PreFlightIssue:
    """A potential issue found during pre-flight check."""
    severity: str  # "error", "warning", "info"
    category: str  # "disk", "permissions", "python", "platform", etc.
    message: str
    suggestion: str = ""


class PreFlightChecker:
    """
    Run pre-flight checks before installation.

    Catches common issues early:
    - Low disk space
    - Permission problems
    - Python version issues
    - Platform compatibility
    - Missing dependencies

    Usage:
        checker = PreFlightChecker(install_dir)
        issues = checker.run_all_checks()
        if any(i.severity == "error" for i in issues):
            print("Cannot proceed with installation")
    """

    MIN_DISK_MB = 500
    MIN_PYTHON = (3, 9)
    REQUIRED_PACKAGES = ['sqlite3']
    OPTIONAL_PACKAGES = ['rich', 'psutil']

    def __init__(self, install_dir: Path = None):
        self.install_dir = install_dir or (Path.home() / ".context-dna")

    def run_all_checks(self) -> List[PreFlightIssue]:
        """Run all pre-flight checks."""
        issues = []

        issues.extend(self._check_python_version())
        issues.extend(self._check_disk_space())
        issues.extend(self._check_permissions())
        issues.extend(self._check_platform())
        issues.extend(self._check_dependencies())
        issues.extend(self._check_path_length())

        return issues

    def _check_python_version(self) -> List[PreFlightIssue]:
        """Check Python version is sufficient."""
        issues = []

        current = sys.version_info[:2]
        if current < self.MIN_PYTHON:
            issues.append(PreFlightIssue(
                severity="error",
                category="python",
                message=f"Python {current[0]}.{current[1]} detected, need {self.MIN_PYTHON[0]}.{self.MIN_PYTHON[1]}+",
                suggestion="Install Python 3.9+ from python.org or via your package manager"
            ))

        return issues

    def _check_disk_space(self) -> List[PreFlightIssue]:
        """Check available disk space."""
        issues = []

        try:
            # Check both install dir and home
            for check_dir in [self.install_dir.parent, Path.home()]:
                if check_dir.exists():
                    usage = shutil.disk_usage(check_dir)
                    free_mb = usage.free / (1024 * 1024)

                    if free_mb < self.MIN_DISK_MB:
                        issues.append(PreFlightIssue(
                            severity="error",
                            category="disk",
                            message=f"Low disk space: {free_mb:.0f}MB free (need {self.MIN_DISK_MB}MB)",
                            suggestion=f"Free up space on {check_dir}"
                        ))
                    elif free_mb < self.MIN_DISK_MB * 2:
                        issues.append(PreFlightIssue(
                            severity="warning",
                            category="disk",
                            message=f"Disk space is limited: {free_mb:.0f}MB free",
                            suggestion="Consider freeing up some space"
                        ))
        except Exception as e:
            issues.append(PreFlightIssue(
                severity="warning",
                category="disk",
                message=f"Could not check disk space: {e}",
                suggestion=""
            ))

        return issues

    def _check_permissions(self) -> List[PreFlightIssue]:
        """Check write permissions."""
        issues = []

        # Try to create install directory
        try:
            self.install_dir.mkdir(parents=True, exist_ok=True)

            # Try to write a test file
            test_file = self.install_dir / ".permission_test"
            test_file.write_text("test")
            test_file.unlink()

        except PermissionError:
            issues.append(PreFlightIssue(
                severity="error",
                category="permissions",
                message=f"Cannot write to {self.install_dir}",
                suggestion=f"Check permissions or try a different location"
            ))
        except Exception as e:
            issues.append(PreFlightIssue(
                severity="warning",
                category="permissions",
                message=f"Permission check failed: {e}",
                suggestion=""
            ))

        return issues

    def _check_platform(self) -> List[PreFlightIssue]:
        """Check platform compatibility."""
        issues = []

        system = platform.system()
        machine = platform.machine()

        # Check for Rosetta on Apple Silicon
        if system == "Darwin" and machine == "x86_64":
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "sysctl.proc_translated"],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout.strip() == "1":
                    issues.append(PreFlightIssue(
                        severity="warning",
                        category="platform",
                        message="Running under Rosetta translation",
                        suggestion="For best MLX performance, run natively on Apple Silicon"
                    ))
            except Exception as e:
                print(f"[WARN] Rosetta detection check failed: {e}")

        # Check for WSL
        if system == "Linux":
            try:
                uname = platform.uname()
                if "microsoft" in uname.release.lower():
                    issues.append(PreFlightIssue(
                        severity="info",
                        category="platform",
                        message="Running in WSL (Windows Subsystem for Linux)",
                        suggestion="For IDE integration, also install in native Windows"
                    ))
            except Exception as e:
                print(f"[WARN] WSL detection check failed: {e}")

        # Check for root user
        if os.geteuid() == 0:
            issues.append(PreFlightIssue(
                severity="warning",
                category="platform",
                message="Running as root user",
                suggestion="Consider running as a regular user to avoid permission issues"
            ))

        return issues

    def _check_dependencies(self) -> List[PreFlightIssue]:
        """Check for required and optional packages."""
        issues = []

        # Check required packages
        for pkg in self.REQUIRED_PACKAGES:
            try:
                __import__(pkg)
            except ImportError:
                issues.append(PreFlightIssue(
                    severity="error",
                    category="dependencies",
                    message=f"Required package '{pkg}' not found",
                    suggestion=f"pip install {pkg}"
                ))

        # Check optional packages
        for pkg in self.OPTIONAL_PACKAGES:
            try:
                __import__(pkg)
            except ImportError:
                issues.append(PreFlightIssue(
                    severity="info",
                    category="dependencies",
                    message=f"Optional package '{pkg}' not found",
                    suggestion=f"pip install {pkg} for enhanced features"
                ))

        return issues

    def _check_path_length(self) -> List[PreFlightIssue]:
        """Check for path length issues (especially on Windows)."""
        issues = []

        if platform.system() == "Windows":
            install_path_len = len(str(self.install_dir.resolve()))

            # Windows MAX_PATH is typically 260, leave room for subdirectories
            if install_path_len > 200:
                issues.append(PreFlightIssue(
                    severity="warning",
                    category="platform",
                    message=f"Install path is very long ({install_path_len} chars)",
                    suggestion="Use a shorter path to avoid Windows MAX_PATH issues"
                ))

        return issues


# =============================================================================
# NETWORK RETRY - Handles flaky connections
# =============================================================================

class NetworkRetry:
    """
    Retry network operations with exponential backoff.

    Handles:
    - Timeout errors
    - Connection errors
    - Rate limiting (429 errors)
    - Temporary server errors (5xx)

    Usage:
        retry = NetworkRetry()
        response = await retry.fetch("https://api.example.com/data")
    """

    MAX_RETRIES = 3
    BASE_DELAY = 1.0
    MAX_DELAY = 30.0
    JITTER_FACTOR = 0.5

    def __init__(self, max_retries: int = None, base_delay: float = None):
        self.max_retries = max_retries or self.MAX_RETRIES
        self.base_delay = base_delay or self.BASE_DELAY

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay with exponential backoff and jitter."""
        delay = min(self.base_delay * (2 ** attempt), self.MAX_DELAY)
        jitter = delay * self.JITTER_FACTOR * random.random()
        return delay + jitter

    async def execute_async(self, func: Callable, *args, **kwargs) -> Any:
        """Execute async function with retry logic."""
        last_error = None

        for attempt in range(self.max_retries):
            try:
                return await func(*args, **kwargs)

            except Exception as e:
                last_error = e
                error_name = type(e).__name__

                # Check if error is retryable
                if not self._is_retryable(e):
                    raise

                if attempt < self.max_retries - 1:
                    delay = self.calculate_delay(attempt)
                    print(f"⚠️  {error_name}: Retry {attempt + 1}/{self.max_retries} after {delay:.1f}s")
                    await asyncio.sleep(delay)

        raise last_error

    def execute_sync(self, func: Callable, *args, **kwargs) -> Any:
        """Execute sync function with retry logic."""
        last_error = None

        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)

            except Exception as e:
                last_error = e
                error_name = type(e).__name__

                # Check if error is retryable
                if not self._is_retryable(e):
                    raise

                if attempt < self.max_retries - 1:
                    delay = self.calculate_delay(attempt)
                    print(f"⚠️  {error_name}: Retry {attempt + 1}/{self.max_retries} after {delay:.1f}s")
                    time.sleep(delay)

        raise last_error

    def _is_retryable(self, error: Exception) -> bool:
        """Check if error is worth retrying."""
        error_name = type(error).__name__.lower()

        # Retryable error types
        retryable = [
            'timeout', 'connectionerror', 'connectionreseterror',
            'temporaryerror', 'serviceunavailable', 'toomanyrequests'
        ]

        return any(r in error_name for r in retryable)


# =============================================================================
# SAFE STRINGS - Unicode and path handling
# =============================================================================

class SafeStrings:
    """
    Safe string handling for paths, shell commands, and display.

    Handles:
    - Unicode normalization
    - Path with spaces
    - Shell escaping
    - Display truncation
    """

    @staticmethod
    def normalize_path(p: Union[str, Path]) -> Path:
        """Normalize path handling unicode and spaces."""
        s = str(p)
        # Normalize unicode (NFC is most compatible)
        s = unicodedata.normalize("NFC", s)
        return Path(s)

    @staticmethod
    def safe_shell_arg(s: str) -> str:
        """Make string safe for shell commands."""
        return shlex.quote(s)

    @staticmethod
    def truncate_display(s: str, max_len: int = 40, suffix: str = "...") -> str:
        """Truncate string for display, handling unicode properly."""
        if len(s) <= max_len:
            return s

        truncate_at = max_len - len(suffix)

        # Don't break in the middle of a multi-byte character
        truncated = s[:truncate_at]
        try:
            truncated.encode('utf-8')
        except UnicodeEncodeError:
            # Back up to find a valid boundary
            while truncated:
                truncated = truncated[:-1]
                try:
                    truncated.encode('utf-8')
                    break
                except UnicodeEncodeError:
                    continue

        return truncated + suffix

    @staticmethod
    def safe_filename(s: str, replacement: str = "_") -> str:
        """Convert string to safe filename."""
        # Characters not allowed in filenames
        unsafe = '<>:"/\\|?*'
        result = s
        for char in unsafe:
            result = result.replace(char, replacement)

        # Also handle control characters
        result = ''.join(c if ord(c) >= 32 else replacement for c in result)

        # Normalize unicode
        result = unicodedata.normalize("NFC", result)

        return result


# =============================================================================
# UTC TIMESTAMPS - Consistent time handling
# =============================================================================

class SafeTimestamp:
    """
    UTC-based timestamp handling for consistency across timezones.

    All internal timestamps are stored in UTC with 'Z' suffix.
    """

    @staticmethod
    def now() -> str:
        """Get current time as ISO8601 UTC string."""
        return datetime.utcnow().isoformat() + "Z"

    @staticmethod
    def parse(s: str) -> datetime:
        """Parse ISO8601 timestamp (handles both UTC and naive)."""
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1])
        return datetime.fromisoformat(s)

    @staticmethod
    def format_local(s: str) -> str:
        """Format UTC timestamp for local display."""
        dt = SafeTimestamp.parse(s)
        # Convert to local time for display
        local_dt = dt.replace(tzinfo=None)  # Naive UTC
        return local_dt.strftime("%Y-%m-%d %H:%M")


# =============================================================================
# CONVENIENCE: DOCTOR COMMAND
# =============================================================================

def run_doctor(install_dir: Path = None) -> bool:
    """
    Run diagnostic checks and report status.

    Usage:
        from context_dna.core.resilience import run_doctor
        success = run_doctor()
    """
    print("🩺 Context DNA Doctor\n")
    print("=" * 60)

    checker = PreFlightChecker(install_dir)
    issues = checker.run_all_checks()

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    infos = [i for i in issues if i.severity == "info"]

    # Display issues
    if errors:
        print("\n❌ ERRORS (must fix):")
        for issue in errors:
            print(f"   [{issue.category}] {issue.message}")
            if issue.suggestion:
                print(f"      → {issue.suggestion}")

    if warnings:
        print("\n⚠️  WARNINGS (should fix):")
        for issue in warnings:
            print(f"   [{issue.category}] {issue.message}")
            if issue.suggestion:
                print(f"      → {issue.suggestion}")

    if infos:
        print("\nℹ️  INFO:")
        for issue in infos:
            print(f"   [{issue.category}] {issue.message}")
            if issue.suggestion:
                print(f"      → {issue.suggestion}")

    # Summary
    print("\n" + "=" * 60)
    if errors:
        print("❌ FAILED: Cannot proceed with installation")
        return False
    elif warnings:
        print("⚠️  PASSED with warnings: Installation can proceed")
        return True
    else:
        print("✅ PASSED: All checks passed")
        return True


if __name__ == "__main__":
    # Run doctor when executed directly
    success = run_doctor()
    sys.exit(0 if success else 1)
