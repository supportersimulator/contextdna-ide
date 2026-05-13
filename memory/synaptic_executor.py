"""
Synaptic Executor - The Motor Cortex of Synaptic's Nervous System

This module gives Synaptic ACTUAL execution capability within defined bounds.

HOW THIS WORKS:
═══════════════════════════════════════════════════════════════════════════

  ATLAS (Claude Code):
  - Has tools provided by Anthropic's CLI (Read, Write, Bash, etc.)
  - Invokes tools, user's computer executes them
  - Ephemeral - exists only during conversation

  SYNAPTIC (Background Service):
  - Has Executors defined here (DockerExecutor, FileExecutor, etc.)
  - Executors are SCOPED to specific Major Skills
  - Persistent - runs via Celery workers even when Atlas isn't active
  - Safety bounds enforced at executor level

THE ARCHITECTURE:
═══════════════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────────────────────────────┐
  │                    SYNAPTIC'S NERVOUS SYSTEM                        │
  ├────────────────────────────────────────────────────────────────────┤
  │                                                                     │
  │  SENSORY CORTEX (Input)           MOTOR CORTEX (Output)            │
  │  ├── HealthMonitor                ├── DockerExecutor               │
  │  ├── FileWatcher                  ├── FileExecutor                 │
  │  ├── WebhookListener              ├── BashExecutor                 │
  │  └── LogAnalyzer                  └── APIExecutor                  │
  │           │                                │                        │
  │           ▼                                ▼                        │
  │  ┌─────────────────────────────────────────────────────────────┐   │
  │  │              PREFRONTAL CORTEX (Decision Making)             │   │
  │  │              synaptic_evolution.py                           │   │
  │  │                                                              │   │
  │  │  Safety Matrix → Confidence Check → Execute or Propose      │   │
  │  └─────────────────────────────────────────────────────────────┘   │
  │           │                                │                        │
  │           ▼                                ▼                        │
  │  ┌─────────────────────────────────────────────────────────────┐   │
  │  │              SKILL-SCOPED EXECUTORS                          │   │
  │  │                                                              │   │
  │  │  Doctor Skill    → Can: restart containers, check health    │   │
  │  │                   → Cannot: delete data, modify code        │   │
  │  │                                                              │   │
  │  │  File Org Skill  → Can: organize files, create folders      │   │
  │  │                   → Cannot: delete files, run code          │   │
  │  │                                                              │   │
  │  │  Code Eval Skill → Can: analyze code, suggest fixes         │   │
  │  │                   → Cannot: auto-apply fixes                │   │
  │  └─────────────────────────────────────────────────────────────┘   │
  │                                                                     │
  └────────────────────────────────────────────────────────────────────┘

SAFETY PHILOSOPHY:
═══════════════════════════════════════════════════════════════════════════

  Each executor is SCOPED to specific operations. The scope is defined by:
  1. ALLOWED_OPERATIONS - Whitelist of what it CAN do
  2. FORBIDDEN_OPERATIONS - Blacklist of what it CANNOT do
  3. SKILL_SCOPE - Which Major Skill authorizes this executor

  Even if Synaptic "decides" to do something outside scope, the executor
  will REFUSE and log the attempt. This is defense-in-depth.

"""

import os
import json
import subprocess
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Callable, Union
from enum import Enum
from abc import ABC, abstractmethod
import hashlib
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("synaptic_executor")


class ExecutorScope(str, Enum):
    """Which Major Skill authorizes an executor."""
    DOCTOR = "doctor"              # Health/infrastructure operations
    FILE_ORG = "file_organization" # File management
    FILE_ORG_INSTALL = "file_org_install"  # File org for install/setup (more restrictive)
    CODE_EVAL = "code_evaluator"   # Code analysis
    SYSTEM = "system"              # Core system operations
    UNRESTRICTED = "unrestricted"  # Aaron-only (never auto-executed)


class OperationType(str, Enum):
    """Types of operations executors can perform."""
    # Docker operations
    DOCKER_START = "docker_start"
    DOCKER_STOP = "docker_stop"
    DOCKER_RESTART = "docker_restart"
    DOCKER_LOGS = "docker_logs"
    DOCKER_INSPECT = "docker_inspect"
    DOCKER_PS = "docker_ps"

    # File operations
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_CREATE = "file_create"
    FILE_DELETE = "file_delete"
    FILE_MOVE = "file_move"
    FILE_COPY = "file_copy"
    DIR_CREATE = "dir_create"
    DIR_DELETE = "dir_delete"
    DIR_LIST = "dir_list"

    # Bash operations
    BASH_SAFE = "bash_safe"        # Whitelisted safe commands
    BASH_DANGEROUS = "bash_dangerous"  # Requires approval

    # API operations
    API_GET = "api_get"
    API_POST = "api_post"

    # Analysis operations
    CODE_ANALYZE = "code_analyze"
    LOG_ANALYZE = "log_analyze"
    HEALTH_CHECK = "health_check"


@dataclass
class ExecutionRequest:
    """A request for Synaptic to execute something."""
    id: str
    skill_scope: ExecutorScope
    operation: OperationType
    parameters: Dict[str, Any]
    reason: str
    confidence: float
    requested_at: str = field(default_factory=lambda: datetime.now().isoformat())
    approved: bool = False
    executed: bool = False
    result: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ExecutionResult:
    """Result of an execution attempt."""
    success: bool
    operation: OperationType
    output: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    blocked_reason: Optional[str] = None


class BaseExecutor(ABC):
    """Base class for all Synaptic executors."""

    # Subclasses define these
    SKILL_SCOPE: ExecutorScope = ExecutorScope.SYSTEM
    ALLOWED_OPERATIONS: List[OperationType] = []
    FORBIDDEN_OPERATIONS: List[OperationType] = []

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the executor."""
        if db_path is None:
            db_path = Path.home() / ".context-dna" / ".synaptic_execution.db"

        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_database()

    def _init_database(self):
        """Initialize execution tracking database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS execution_log (
                    id TEXT PRIMARY KEY,
                    skill_scope TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    parameters TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    requested_at TEXT NOT NULL,
                    approved INTEGER DEFAULT 0,
                    executed INTEGER DEFAULT 0,
                    executed_at TEXT,
                    result TEXT,
                    error TEXT,
                    duration_ms REAL DEFAULT 0.0,
                    blocked_reason TEXT
                )
            """)
            conn.commit()

    def _generate_id(self) -> str:
        """Generate unique ID."""
        return hashlib.sha256(
            f"{datetime.now().isoformat()}-{id(self)}".encode()
        ).hexdigest()[:16]

    def can_execute(self, operation: OperationType) -> tuple[bool, str]:
        """Check if this executor can perform the operation."""
        # Check forbidden first (blacklist takes precedence)
        if operation in self.FORBIDDEN_OPERATIONS:
            return False, f"Operation {operation.value} is forbidden for {self.SKILL_SCOPE.value}"

        # Check allowed (whitelist)
        if operation not in self.ALLOWED_OPERATIONS:
            return False, f"Operation {operation.value} not in allowed list for {self.SKILL_SCOPE.value}"

        return True, "Allowed"

    def request_execution(
        self,
        operation: OperationType,
        parameters: Dict[str, Any],
        reason: str,
        confidence: float
    ) -> ExecutionRequest:
        """
        Request execution of an operation.

        This records the request. Whether it auto-executes depends on:
        - Is the operation allowed?
        - Is confidence high enough for auto-execution?
        """
        request = ExecutionRequest(
            id=self._generate_id(),
            skill_scope=self.SKILL_SCOPE,
            operation=operation,
            parameters=parameters,
            reason=reason,
            confidence=confidence
        )

        # Check if allowed
        can_exec, msg = self.can_execute(operation)

        if not can_exec:
            request.error = msg
            self._log_request(request)
            return request

        # Check confidence threshold for auto-execution
        # High confidence (>= 0.8) = auto-execute for allowed operations
        # Lower confidence = requires approval
        if confidence >= 0.8:
            request.approved = True
            result = self.execute(request)
            request.executed = True
            request.result = result.output if result.success else None
            request.error = result.error
        else:
            # Queue for approval
            request.approved = False

        self._log_request(request)
        return request

    def _log_request(self, request: ExecutionRequest):
        """Log execution request to database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO execution_log
                (id, skill_scope, operation, parameters, reason, confidence,
                 requested_at, approved, executed, result, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                request.id,
                request.skill_scope.value,
                request.operation.value,
                json.dumps(request.parameters),
                request.reason,
                request.confidence,
                request.requested_at,
                1 if request.approved else 0,
                1 if request.executed else 0,
                request.result,
                request.error
            ))
            conn.commit()

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute the operation. Subclasses implement this."""
        pass


# =============================================================================
# DOCTOR SKILL EXECUTOR - Health/Infrastructure Operations
# =============================================================================

class DoctorExecutor(BaseExecutor):
    """
    Executor for the Doctor Skill.

    CAN:
    - Start/restart containers
    - Check container status
    - Read logs
    - Run health checks

    CANNOT:
    - Delete containers
    - Delete volumes/data
    - Modify configuration files
    - Run arbitrary commands
    """

    SKILL_SCOPE = ExecutorScope.DOCTOR

    ALLOWED_OPERATIONS = [
        OperationType.DOCKER_START,
        OperationType.DOCKER_RESTART,
        OperationType.DOCKER_LOGS,
        OperationType.DOCKER_INSPECT,
        OperationType.DOCKER_PS,
        OperationType.HEALTH_CHECK,
        OperationType.LOG_ANALYZE,
    ]

    FORBIDDEN_OPERATIONS = [
        OperationType.DOCKER_STOP,  # Stopping could interrupt work
        OperationType.FILE_DELETE,
        OperationType.DIR_DELETE,
        OperationType.BASH_DANGEROUS,
    ]

    # Whitelist of safe Docker commands
    SAFE_DOCKER_COMMANDS = [
        "docker ps",
        "docker compose ps",
        "docker logs",
        "docker inspect",
        "docker compose up -d",
        "docker restart",
        "docker compose restart",
        "docker stats --no-stream",
    ]

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute a Doctor skill operation."""
        start_time = datetime.now()

        try:
            if request.operation == OperationType.DOCKER_PS:
                return self._docker_ps()

            elif request.operation == OperationType.DOCKER_START:
                service = request.parameters.get("service")
                return self._docker_start(service)

            elif request.operation == OperationType.DOCKER_RESTART:
                service = request.parameters.get("service")
                return self._docker_restart(service)

            elif request.operation == OperationType.DOCKER_LOGS:
                service = request.parameters.get("service")
                lines = request.parameters.get("lines", 50)
                return self._docker_logs(service, lines)

            elif request.operation == OperationType.DOCKER_INSPECT:
                service = request.parameters.get("service")
                return self._docker_inspect(service)

            elif request.operation == OperationType.HEALTH_CHECK:
                return self._health_check()

            else:
                return ExecutionResult(
                    success=False,
                    operation=request.operation,
                    error=f"Operation {request.operation.value} not implemented"
                )

        except Exception as e:
            duration = (datetime.now() - start_time).total_seconds() * 1000
            return ExecutionResult(
                success=False,
                operation=request.operation,
                error=str(e),
                duration_ms=duration
            )

    def _docker_ps(self) -> ExecutionResult:
        """List running containers."""
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=Path.home() / "dev/er-simulator-superrepo/context-dna/infra"
        )

        return ExecutionResult(
            success=result.returncode == 0,
            operation=OperationType.DOCKER_PS,
            output=result.stdout,
            error=result.stderr if result.returncode != 0 else None
        )

    def _docker_start(self, service: str) -> ExecutionResult:
        """Start a Docker service."""
        if not service:
            return ExecutionResult(
                success=False,
                operation=OperationType.DOCKER_START,
                error="Service name required"
            )

        result = subprocess.run(
            ["docker", "compose", "up", "-d", service],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=Path.home() / "dev/er-simulator-superrepo/context-dna/infra"
        )

        return ExecutionResult(
            success=result.returncode == 0,
            operation=OperationType.DOCKER_START,
            output=f"Started {service}: {result.stdout}",
            error=result.stderr if result.returncode != 0 else None
        )

    def _docker_restart(self, service: str) -> ExecutionResult:
        """Restart a Docker service."""
        if not service:
            return ExecutionResult(
                success=False,
                operation=OperationType.DOCKER_RESTART,
                error="Service name required"
            )

        result = subprocess.run(
            ["docker", "compose", "restart", service],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=Path.home() / "dev/er-simulator-superrepo/context-dna/infra"
        )

        return ExecutionResult(
            success=result.returncode == 0,
            operation=OperationType.DOCKER_RESTART,
            output=f"Restarted {service}: {result.stdout}",
            error=result.stderr if result.returncode != 0 else None
        )

    def _docker_logs(self, service: str, lines: int = 50) -> ExecutionResult:
        """Get logs from a Docker service."""
        if not service:
            return ExecutionResult(
                success=False,
                operation=OperationType.DOCKER_LOGS,
                error="Service name required"
            )

        result = subprocess.run(
            ["docker", "compose", "logs", "--tail", str(lines), service],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=Path.home() / "dev/er-simulator-superrepo/context-dna/infra"
        )

        return ExecutionResult(
            success=True,  # Logs command usually succeeds even if empty
            operation=OperationType.DOCKER_LOGS,
            output=result.stdout or result.stderr
        )

    def _docker_inspect(self, service: str) -> ExecutionResult:
        """Inspect a Docker container."""
        if not service:
            return ExecutionResult(
                success=False,
                operation=OperationType.DOCKER_INSPECT,
                error="Service name required"
            )

        result = subprocess.run(
            ["docker", "inspect", service],
            capture_output=True,
            text=True,
            timeout=30
        )

        return ExecutionResult(
            success=result.returncode == 0,
            operation=OperationType.DOCKER_INSPECT,
            output=result.stdout,
            error=result.stderr if result.returncode != 0 else None
        )

    def _health_check(self) -> ExecutionResult:
        """Run comprehensive health check."""
        checks = []

        # Check Docker daemon
        docker_check = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10
        )
        checks.append(f"Docker daemon: {'✅' if docker_check.returncode == 0 else '❌'}")

        # Check Context DNA containers
        ps_result = self._docker_ps()
        if ps_result.success:
            checks.append(f"Containers: {ps_result.output[:200] if ps_result.output else 'None'}")

        return ExecutionResult(
            success=True,
            operation=OperationType.HEALTH_CHECK,
            output="\n".join(checks)
        )


# =============================================================================
# FILE ORGANIZATION EXECUTOR - File Management Operations
# =============================================================================

class FileOrgExecutor(BaseExecutor):
    """
    Executor for the File Organization Skill.

    CAN:
    - Create directories
    - Move files (within allowed paths)
    - Copy files
    - List directories
    - Read files

    CANNOT:
    - Delete files
    - Execute code
    - Access system directories
    """

    SKILL_SCOPE = ExecutorScope.FILE_ORG

    ALLOWED_OPERATIONS = [
        OperationType.FILE_READ,
        OperationType.FILE_MOVE,
        OperationType.FILE_COPY,
        OperationType.DIR_CREATE,
        OperationType.DIR_LIST,
    ]

    FORBIDDEN_OPERATIONS = [
        OperationType.FILE_DELETE,
        OperationType.DIR_DELETE,
        OperationType.FILE_WRITE,  # Use CODE_EVAL for writing
        OperationType.BASH_SAFE,
        OperationType.BASH_DANGEROUS,
    ]

    # Allowed base paths for file operations
    ALLOWED_PATHS = [
        Path.home() / "Documents",
        Path.home() / "Downloads",
        Path.home() / ".context-dna",
    ]

    def _is_path_allowed(self, path: Path) -> bool:
        """Check if path is within allowed directories."""
        path = path.resolve()
        return any(
            str(path).startswith(str(allowed.resolve()))
            for allowed in self.ALLOWED_PATHS
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute a File Organization operation."""
        try:
            if request.operation == OperationType.DIR_LIST:
                path = Path(request.parameters.get("path", "."))
                return self._list_directory(path)

            elif request.operation == OperationType.DIR_CREATE:
                path = Path(request.parameters.get("path"))
                return self._create_directory(path)

            elif request.operation == OperationType.FILE_READ:
                path = Path(request.parameters.get("path"))
                return self._read_file(path)

            elif request.operation == OperationType.FILE_MOVE:
                src = Path(request.parameters.get("source"))
                dst = Path(request.parameters.get("destination"))
                return self._move_file(src, dst)

            elif request.operation == OperationType.FILE_COPY:
                src = Path(request.parameters.get("source"))
                dst = Path(request.parameters.get("destination"))
                return self._copy_file(src, dst)

            else:
                return ExecutionResult(
                    success=False,
                    operation=request.operation,
                    error=f"Operation {request.operation.value} not implemented"
                )

        except Exception as e:
            return ExecutionResult(
                success=False,
                operation=request.operation,
                error=str(e)
            )

    def _list_directory(self, path: Path) -> ExecutionResult:
        """List directory contents."""
        if not self._is_path_allowed(path):
            return ExecutionResult(
                success=False,
                operation=OperationType.DIR_LIST,
                blocked_reason=f"Path {path} is outside allowed directories"
            )

        if not path.exists():
            return ExecutionResult(
                success=False,
                operation=OperationType.DIR_LIST,
                error=f"Path {path} does not exist"
            )

        contents = []
        for item in path.iterdir():
            item_type = "📁" if item.is_dir() else "📄"
            contents.append(f"{item_type} {item.name}")

        return ExecutionResult(
            success=True,
            operation=OperationType.DIR_LIST,
            output="\n".join(contents) if contents else "(empty)"
        )

    def _create_directory(self, path: Path) -> ExecutionResult:
        """Create a directory."""
        if not self._is_path_allowed(path):
            return ExecutionResult(
                success=False,
                operation=OperationType.DIR_CREATE,
                blocked_reason=f"Path {path} is outside allowed directories"
            )

        path.mkdir(parents=True, exist_ok=True)
        return ExecutionResult(
            success=True,
            operation=OperationType.DIR_CREATE,
            output=f"Created directory: {path}"
        )

    def _read_file(self, path: Path) -> ExecutionResult:
        """Read a file."""
        if not self._is_path_allowed(path):
            return ExecutionResult(
                success=False,
                operation=OperationType.FILE_READ,
                blocked_reason=f"Path {path} is outside allowed directories"
            )

        if not path.exists():
            return ExecutionResult(
                success=False,
                operation=OperationType.FILE_READ,
                error=f"File {path} does not exist"
            )

        content = path.read_text()
        return ExecutionResult(
            success=True,
            operation=OperationType.FILE_READ,
            output=content[:10000]  # Limit output size
        )

    def _move_file(self, src: Path, dst: Path) -> ExecutionResult:
        """Move a file."""
        if not self._is_path_allowed(src) or not self._is_path_allowed(dst):
            return ExecutionResult(
                success=False,
                operation=OperationType.FILE_MOVE,
                blocked_reason="Source or destination outside allowed directories"
            )

        shutil.move(str(src), str(dst))
        return ExecutionResult(
            success=True,
            operation=OperationType.FILE_MOVE,
            output=f"Moved {src} → {dst}"
        )

    def _copy_file(self, src: Path, dst: Path) -> ExecutionResult:
        """Copy a file."""
        if not self._is_path_allowed(src) or not self._is_path_allowed(dst):
            return ExecutionResult(
                success=False,
                operation=OperationType.FILE_COPY,
                blocked_reason="Source or destination outside allowed directories"
            )

        shutil.copy2(str(src), str(dst))
        return ExecutionResult(
            success=True,
            operation=OperationType.FILE_COPY,
            output=f"Copied {src} → {dst}"
        )


# =============================================================================
# FILE ORGANIZATION INSTALL EXECUTOR - For Context DNA Installation (Restrictive)
# =============================================================================

class FileOrgInstallExecutor(BaseExecutor):
    """
    Executor for File Organization during Context DNA Installation.

    MORE RESTRICTIVE than standard FileOrgExecutor - designed for safe
    automated installation on new computers.

    CAN:
    - Create directories (mkdir -p ~/.context-dna)
    - Create config files
    - Copy templates
    - List directories
    - Read files (for verification)
    - Run safe system queries (uname, whoami, which)

    CANNOT:
    - Delete files (never during install)
    - Delete directories
    - Move files (might break things)
    - Run dangerous bash

    PATH RESTRICTIONS:
    - ~/.context-dna/          (Context DNA config dir)
    - ~/.claude/               (Claude hooks)
    - {workspace}/.context-dna/ (Per-project config)

    FORBIDDEN PATHS:
    - /                        (Root)
    - /etc/                    (System config)
    - /usr/                    (System binaries)
    - ~/.ssh/                  (SSH keys)
    - ~/.aws/                  (AWS credentials)
    """

    SKILL_SCOPE = ExecutorScope.FILE_ORG_INSTALL

    ALLOWED_OPERATIONS = [
        # Read operations (always safe)
        OperationType.FILE_READ,
        OperationType.DIR_LIST,

        # Create operations (additive, safe for install)
        OperationType.DIR_CREATE,
        OperationType.FILE_CREATE,
        OperationType.FILE_COPY,

        # System queries (read-only)
        OperationType.BASH_SAFE,  # Only for uname, whoami, which, etc.
    ]

    FORBIDDEN_OPERATIONS = [
        OperationType.FILE_DELETE,   # Never delete during install
        OperationType.DIR_DELETE,    # Never delete directories
        OperationType.FILE_MOVE,     # No moves during install (might break things)
        OperationType.BASH_DANGEROUS,
        OperationType.FILE_WRITE,    # Use FILE_CREATE instead (explicit)
    ]

    # Allowed paths for install operations
    ALLOWED_PATHS = [
        Path.home() / ".context-dna",
        Path.home() / ".claude",
    ]

    # Forbidden paths (system directories)
    FORBIDDEN_PATHS = [
        Path("/"),
        Path("/etc"),
        Path("/usr"),
        Path("/var"),
        Path("/System"),
        Path.home() / ".ssh",
        Path.home() / ".aws",
        Path.home() / ".gnupg",
    ]

    # Safe bash commands for system queries
    SAFE_BASH_COMMANDS = [
        "uname",
        "whoami",
        "which",
        "python3 --version",
        "python --version",
        "node --version",
        "npm --version",
        "git --version",
        "code --version",
        "sw_vers",  # macOS version
    ]

    def _is_path_allowed(self, path: Path) -> tuple[bool, str]:
        """Check if path is within allowed directories and not forbidden."""
        path = path.resolve()

        # Check forbidden first
        for forbidden in self.FORBIDDEN_PATHS:
            if str(path).startswith(str(forbidden.resolve())):
                return False, f"Path {path} is in forbidden system directory"

        # Check if in allowed paths
        is_allowed = any(
            str(path).startswith(str(allowed.resolve()))
            for allowed in self.ALLOWED_PATHS
        )

        if not is_allowed:
            return False, f"Path {path} is outside allowed install directories"

        return True, "Allowed"

    def _is_bash_command_safe(self, command: str) -> tuple[bool, str]:
        """Check if bash command is in safe whitelist."""
        cmd_start = command.split()[0] if command else ""

        for safe_cmd in self.SAFE_BASH_COMMANDS:
            if command.startswith(safe_cmd.split()[0]):
                return True, "Safe command"

        return False, f"Command '{cmd_start}' not in safe whitelist"

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute a File Organization Install operation."""
        try:
            if request.operation == OperationType.DIR_LIST:
                path = Path(request.parameters.get("path", "."))
                return self._list_directory(path)

            elif request.operation == OperationType.DIR_CREATE:
                path = Path(request.parameters.get("path"))
                return self._create_directory(path)

            elif request.operation == OperationType.FILE_READ:
                path = Path(request.parameters.get("path"))
                return self._read_file(path)

            elif request.operation == OperationType.FILE_CREATE:
                path = Path(request.parameters.get("path"))
                content = request.parameters.get("content", "")
                return self._create_file(path, content)

            elif request.operation == OperationType.FILE_COPY:
                src = Path(request.parameters.get("source"))
                dst = Path(request.parameters.get("destination"))
                return self._copy_file(src, dst)

            elif request.operation == OperationType.BASH_SAFE:
                command = request.parameters.get("command", "")
                return self._run_safe_bash(command)

            else:
                return ExecutionResult(
                    success=False,
                    operation=request.operation,
                    error=f"Operation {request.operation.value} not implemented"
                )

        except Exception as e:
            return ExecutionResult(
                success=False,
                operation=request.operation,
                error=str(e)
            )

    def _list_directory(self, path: Path) -> ExecutionResult:
        """List directory contents (read-only, always safe)."""
        if not path.exists():
            return ExecutionResult(
                success=False,
                operation=OperationType.DIR_LIST,
                error=f"Path {path} does not exist"
            )

        contents = []
        for item in path.iterdir():
            item_type = "📁" if item.is_dir() else "📄"
            contents.append(f"{item_type} {item.name}")

        return ExecutionResult(
            success=True,
            operation=OperationType.DIR_LIST,
            output="\n".join(contents) if contents else "(empty)"
        )

    def _create_directory(self, path: Path) -> ExecutionResult:
        """Create a directory within allowed paths."""
        allowed, reason = self._is_path_allowed(path)
        if not allowed:
            return ExecutionResult(
                success=False,
                operation=OperationType.DIR_CREATE,
                blocked_reason=reason
            )

        path.mkdir(parents=True, exist_ok=True)
        return ExecutionResult(
            success=True,
            operation=OperationType.DIR_CREATE,
            output=f"Created directory: {path}"
        )

    def _read_file(self, path: Path) -> ExecutionResult:
        """Read a file (for verification during install)."""
        if not path.exists():
            return ExecutionResult(
                success=False,
                operation=OperationType.FILE_READ,
                error=f"File {path} does not exist"
            )

        content = path.read_text()
        return ExecutionResult(
            success=True,
            operation=OperationType.FILE_READ,
            output=content[:10000]  # Limit output size
        )

    def _create_file(self, path: Path, content: str) -> ExecutionResult:
        """Create a new file within allowed paths."""
        allowed, reason = self._is_path_allowed(path)
        if not allowed:
            return ExecutionResult(
                success=False,
                operation=OperationType.FILE_CREATE,
                blocked_reason=reason
            )

        # Safety: Don't overwrite existing files
        if path.exists():
            return ExecutionResult(
                success=False,
                operation=OperationType.FILE_CREATE,
                error=f"File {path} already exists - use FILE_WRITE to overwrite"
            )

        # Ensure parent directory exists
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

        return ExecutionResult(
            success=True,
            operation=OperationType.FILE_CREATE,
            output=f"Created file: {path} ({len(content)} bytes)"
        )

    def _copy_file(self, src: Path, dst: Path) -> ExecutionResult:
        """Copy a file to allowed path."""
        allowed, reason = self._is_path_allowed(dst)
        if not allowed:
            return ExecutionResult(
                success=False,
                operation=OperationType.FILE_COPY,
                blocked_reason=reason
            )

        if not src.exists():
            return ExecutionResult(
                success=False,
                operation=OperationType.FILE_COPY,
                error=f"Source file {src} does not exist"
            )

        # Ensure parent directory exists
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))

        return ExecutionResult(
            success=True,
            operation=OperationType.FILE_COPY,
            output=f"Copied {src} → {dst}"
        )

    def _run_safe_bash(self, command: str) -> ExecutionResult:
        """Run only whitelisted safe bash commands."""
        safe, reason = self._is_bash_command_safe(command)
        if not safe:
            return ExecutionResult(
                success=False,
                operation=OperationType.BASH_SAFE,
                blocked_reason=reason
            )

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        )

        return ExecutionResult(
            success=result.returncode == 0,
            operation=OperationType.BASH_SAFE,
            output=result.stdout or result.stderr,
            error=result.stderr if result.returncode != 0 else None
        )


# =============================================================================
# CODE EVALUATOR EXECUTOR - Analysis Operations (Read-Only by Default)
# =============================================================================

class CodeEvalExecutor(BaseExecutor):
    """
    Executor for the Code Evaluator Skill.

    CAN:
    - Analyze code
    - Read files
    - List directories
    - Generate reports

    CANNOT:
    - Modify files (analysis only)
    - Run code
    - Delete anything
    """

    SKILL_SCOPE = ExecutorScope.CODE_EVAL

    ALLOWED_OPERATIONS = [
        OperationType.CODE_ANALYZE,
        OperationType.FILE_READ,
        OperationType.DIR_LIST,
        OperationType.LOG_ANALYZE,
    ]

    FORBIDDEN_OPERATIONS = [
        OperationType.FILE_WRITE,
        OperationType.FILE_DELETE,
        OperationType.FILE_CREATE,
        OperationType.BASH_SAFE,
        OperationType.BASH_DANGEROUS,
    ]

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute a Code Evaluator operation."""
        try:
            if request.operation == OperationType.CODE_ANALYZE:
                path = Path(request.parameters.get("path"))
                return self._analyze_code(path)

            elif request.operation == OperationType.FILE_READ:
                path = Path(request.parameters.get("path"))
                return self._read_file(path)

            elif request.operation == OperationType.DIR_LIST:
                path = Path(request.parameters.get("path", "."))
                return self._list_directory(path)

            else:
                return ExecutionResult(
                    success=False,
                    operation=request.operation,
                    error=f"Operation {request.operation.value} not implemented"
                )

        except Exception as e:
            return ExecutionResult(
                success=False,
                operation=request.operation,
                error=str(e)
            )

    def _analyze_code(self, path: Path) -> ExecutionResult:
        """Analyze code in a file or directory."""
        if not path.exists():
            return ExecutionResult(
                success=False,
                operation=OperationType.CODE_ANALYZE,
                error=f"Path {path} does not exist"
            )

        analysis = {
            "path": str(path),
            "type": "directory" if path.is_dir() else "file",
            "size_bytes": path.stat().st_size if path.is_file() else None,
        }

        if path.is_file():
            content = path.read_text()
            analysis["lines"] = len(content.splitlines())
            analysis["characters"] = len(content)

            # Basic code analysis
            if path.suffix == ".py":
                analysis["functions"] = content.count("def ")
                analysis["classes"] = content.count("class ")
                analysis["imports"] = content.count("import ")

        return ExecutionResult(
            success=True,
            operation=OperationType.CODE_ANALYZE,
            output=json.dumps(analysis, indent=2)
        )

    def _read_file(self, path: Path) -> ExecutionResult:
        """Read a file for analysis."""
        if not path.exists():
            return ExecutionResult(
                success=False,
                operation=OperationType.FILE_READ,
                error=f"File {path} does not exist"
            )

        content = path.read_text()
        return ExecutionResult(
            success=True,
            operation=OperationType.FILE_READ,
            output=content[:50000]  # Larger limit for analysis
        )

    def _list_directory(self, path: Path) -> ExecutionResult:
        """List directory for analysis."""
        if not path.exists():
            return ExecutionResult(
                success=False,
                operation=OperationType.DIR_LIST,
                error=f"Path {path} does not exist"
            )

        items = []
        for item in path.iterdir():
            items.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None
            })

        return ExecutionResult(
            success=True,
            operation=OperationType.DIR_LIST,
            output=json.dumps(items, indent=2)
        )


# =============================================================================
# UNIFIED EXECUTOR - Routes requests to appropriate skill executor
# =============================================================================

class SynapticExecutor:
    """
    The unified executor that routes requests to appropriate skill executors.

    This is the entry point for all Synaptic execution.
    """

    def __init__(self):
        """Initialize all skill executors."""
        self.executors = {
            ExecutorScope.DOCTOR: DoctorExecutor(),
            ExecutorScope.FILE_ORG: FileOrgExecutor(),
            ExecutorScope.FILE_ORG_INSTALL: FileOrgInstallExecutor(),
            ExecutorScope.CODE_EVAL: CodeEvalExecutor(),
        }

    def execute(
        self,
        skill: ExecutorScope,
        operation: OperationType,
        parameters: Dict[str, Any],
        reason: str,
        confidence: float
    ) -> ExecutionRequest:
        """
        Execute an operation through the appropriate skill executor.

        Args:
            skill: Which skill scope to use
            operation: What operation to perform
            parameters: Operation parameters
            reason: Why this is being done
            confidence: How confident (0.0 to 1.0)

        Returns:
            ExecutionRequest with result
        """
        executor = self.executors.get(skill)

        if not executor:
            return ExecutionRequest(
                id="error",
                skill_scope=skill,
                operation=operation,
                parameters=parameters,
                reason=reason,
                confidence=confidence,
                error=f"No executor for skill: {skill.value}"
            )

        return executor.request_execution(
            operation=operation,
            parameters=parameters,
            reason=reason,
            confidence=confidence
        )

    def get_execution_history(self, skill: Optional[ExecutorScope] = None) -> List[Dict]:
        """Get execution history, optionally filtered by skill."""
        # Use first executor's DB (they all share the same DB)
        executor = list(self.executors.values())[0]

        with sqlite3.connect(executor.db_path) as conn:
            cursor = conn.cursor()

            if skill:
                cursor.execute("""
                    SELECT id, skill_scope, operation, reason, confidence,
                           approved, executed, result, error, requested_at
                    FROM execution_log
                    WHERE skill_scope = ?
                    ORDER BY requested_at DESC
                    LIMIT 100
                """, (skill.value,))
            else:
                cursor.execute("""
                    SELECT id, skill_scope, operation, reason, confidence,
                           approved, executed, result, error, requested_at
                    FROM execution_log
                    ORDER BY requested_at DESC
                    LIMIT 100
                """)

            history = []
            for row in cursor.fetchall():
                history.append({
                    "id": row[0],
                    "skill": row[1],
                    "operation": row[2],
                    "reason": row[3],
                    "confidence": row[4],
                    "approved": bool(row[5]),
                    "executed": bool(row[6]),
                    "result": row[7],
                    "error": row[8],
                    "timestamp": row[9]
                })
            return history

    def get_pending_approvals(self) -> List[Dict]:
        """Get operations awaiting approval."""
        executor = list(self.executors.values())[0]

        with sqlite3.connect(executor.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, skill_scope, operation, parameters, reason, confidence, requested_at
                FROM execution_log
                WHERE approved = 0 AND executed = 0 AND error IS NULL
                ORDER BY requested_at DESC
            """)

            pending = []
            for row in cursor.fetchall():
                pending.append({
                    "id": row[0],
                    "skill": row[1],
                    "operation": row[2],
                    "parameters": json.loads(row[3]),
                    "reason": row[4],
                    "confidence": row[5],
                    "timestamp": row[6]
                })
            return pending


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def synaptic_execute(
    skill: str,
    operation: str,
    parameters: Dict[str, Any],
    reason: str,
    confidence: float = 0.8
) -> Dict[str, Any]:
    """
    Convenience function for Synaptic to execute operations.

    Example:
        synaptic_execute(
            skill="doctor",
            operation="docker_restart",
            parameters={"service": "contextdna-redis"},
            reason="Redis showing high memory, preventive restart",
            confidence=0.85
        )
    """
    executor = SynapticExecutor()

    skill_scope = ExecutorScope(skill)
    op_type = OperationType(operation)

    request = executor.execute(
        skill=skill_scope,
        operation=op_type,
        parameters=parameters,
        reason=reason,
        confidence=confidence
    )

    return {
        "id": request.id,
        "executed": request.executed,
        "approved": request.approved,
        "result": request.result,
        "error": request.error
    }


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    executor = SynapticExecutor()

    if len(sys.argv) < 2:
        print("""
╔══════════════════════════════════════════════════════════════════════╗
║  SYNAPTIC EXECUTOR - Motor Cortex of the Nervous System              ║
╠══════════════════════════════════════════════════════════════════════╣

Usage:
  python synaptic_executor.py history             - View execution history
  python synaptic_executor.py pending             - View pending approvals
  python synaptic_executor.py capabilities        - View skill capabilities
  python synaptic_executor.py test <skill>        - Test a skill executor

Skills: doctor, file_organization, code_evaluator

╚══════════════════════════════════════════════════════════════════════╝
        """)
        sys.exit(0)

    command = sys.argv[1]

    if command == "history":
        history = executor.get_execution_history()
        print("\n╔══════════════════════════════════════════════════════════════════════╗")
        print("║  EXECUTION HISTORY                                                   ║")
        print("╠══════════════════════════════════════════════════════════════════════╣")

        for item in history[:20]:
            status = "✅" if item["executed"] else ("⏳" if not item["error"] else "❌")
            print(f"   {status} [{item['skill']}] {item['operation']}")
            print(f"      Reason: {item['reason'][:50]}...")
            if item["error"]:
                print(f"      Error: {item['error'][:50]}...")
            print()

        if not history:
            print("   No execution history yet.")

        print("╚══════════════════════════════════════════════════════════════════════╝")

    elif command == "pending":
        pending = executor.get_pending_approvals()
        print("\n╔══════════════════════════════════════════════════════════════════════╗")
        print("║  PENDING APPROVALS                                                   ║")
        print("╠══════════════════════════════════════════════════════════════════════╣")

        for item in pending:
            print(f"   ID: {item['id']}")
            print(f"   Skill: {item['skill']}")
            print(f"   Operation: {item['operation']}")
            print(f"   Confidence: {item['confidence']:.0%}")
            print(f"   Reason: {item['reason']}")
            print()

        if not pending:
            print("   No pending approvals.")

        print("╚══════════════════════════════════════════════════════════════════════╝")

    elif command == "capabilities":
        print("\n╔══════════════════════════════════════════════════════════════════════╗")
        print("║  SKILL EXECUTOR CAPABILITIES                                         ║")
        print("╠══════════════════════════════════════════════════════════════════════╣")

        for skill, exec_instance in executor.executors.items():
            print(f"\n   🎯 {skill.value.upper()}")
            print("   ✅ ALLOWED:")
            for op in exec_instance.ALLOWED_OPERATIONS:
                print(f"      • {op.value}")
            print("   🚫 FORBIDDEN:")
            for op in exec_instance.FORBIDDEN_OPERATIONS:
                print(f"      • {op.value}")

        print("\n╚══════════════════════════════════════════════════════════════════════╝")

    elif command == "test" and len(sys.argv) > 2:
        skill_name = sys.argv[2]
        print(f"\nTesting {skill_name} executor...")

        if skill_name == "doctor":
            result = synaptic_execute(
                skill="doctor",
                operation="docker_ps",
                parameters={},
                reason="Test: List containers",
                confidence=0.9
            )
            print(f"Result: {json.dumps(result, indent=2)}")

        elif skill_name == "file_organization":
            result = synaptic_execute(
                skill="file_organization",
                operation="dir_list",
                parameters={"path": str(Path.home() / ".context-dna")},
                reason="Test: List context-dna directory",
                confidence=0.9
            )
            print(f"Result: {json.dumps(result, indent=2)}")

        elif skill_name == "code_evaluator":
            result = synaptic_execute(
                skill="code_evaluator",
                operation="code_analyze",
                parameters={"path": str(Path(__file__))},
                reason="Test: Analyze this file",
                confidence=0.9
            )
            print(f"Result: {json.dumps(result, indent=2)}")
