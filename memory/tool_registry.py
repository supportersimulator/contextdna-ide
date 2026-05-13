#!/usr/bin/env python3
"""
Tool Registry — managed tool execution for swarm agents.

Agents request tool invocations; the registry validates, executes,
and returns results. This is the security boundary between agents
and the host system.

Architecture:
    Agent  -->  ToolRegistry.invoke("read_file", {path: "foo.py"})
                    |
                    ├── validate params against ToolSpec
                    ├── check path security (no traversal)
                    ├── resolve secret refs (if needed)
                    ├── execute handler with timeout
                    ├── redact secrets from output
                    └── return ToolResult + audit log entry
"""

import asyncio
import fnmatch
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger("context_dna.tool_registry")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = PROJECT_ROOT / "memory"

# Timeout for tool execution (seconds)
DEFAULT_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    """Specification of a tool that agents can invoke."""

    name: str
    description: str
    parameters: Dict[str, Dict[str, Any]]  # param_name -> {type, required, default, description}
    requires_approval: bool = False
    category: str = "general"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolResult:
    """Result of a tool execution."""

    tool_name: str
    success: bool
    output: str
    error: Optional[str] = None
    execution_time_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "execution_time_ms": self.execution_time_ms,
        }


@dataclass
class _AuditEntry:
    """Internal audit log entry."""

    timestamp: str
    tool_name: str
    agent_id: Optional[str]
    params: Dict
    success: bool
    execution_time_ms: float
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Path security
# ---------------------------------------------------------------------------

def _resolve_safe_path(raw_path: str) -> Path:
    """Resolve a path safely within PROJECT_ROOT.

    Raises ValueError on traversal attempts.
    """
    # Expand ~ and env vars, then resolve
    expanded = os.path.expanduser(os.path.expandvars(raw_path))
    resolved = Path(expanded).resolve()

    # Allow absolute paths only if they fall within PROJECT_ROOT
    if not str(resolved).startswith(str(PROJECT_ROOT)):
        raise ValueError(
            f"Path traversal blocked: {raw_path} resolves to {resolved} "
            f"which is outside project root {PROJECT_ROOT}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Built-in tool handlers
# ---------------------------------------------------------------------------

def _handler_read_file(path: str, offset: int = 0, limit: int = 100) -> str:
    """Read file contents, returning numbered lines."""
    safe = _resolve_safe_path(path)
    if not safe.is_file():
        raise FileNotFoundError(f"Not a file: {safe}")

    with open(safe, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    selected = lines[offset: offset + limit]
    numbered = [f"{offset + i + 1:>5}\t{line}" for i, line in enumerate(selected)]
    return "".join(numbered)


def _handler_search_code(pattern: str, glob: str = "**/*.py", max_results: int = 20) -> str:
    """Search code using subprocess ripgrep or grep fallback."""
    import shutil

    # Try ripgrep first, then grep
    rg = shutil.which("rg")
    if rg:
        cmd = [
            rg, "--no-heading", "--line-number", "--color=never",
            "--max-count", str(max_results),
            "--glob", glob,
            pattern,
            str(PROJECT_ROOT),
        ]
    else:
        # Fallback to grep -rn
        cmd = [
            "grep", "-rn", "--include", glob.replace("**/*", "*"),
            pattern, str(PROJECT_ROOT),
        ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT,
        cwd=str(PROJECT_ROOT),
    )
    lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
    # Strip PROJECT_ROOT prefix for readability
    prefix = str(PROJECT_ROOT) + "/"
    cleaned = [l.replace(prefix, "") for l in lines[:max_results]]
    return "\n".join(cleaned) if cleaned else "(no matches)"


def _handler_list_files(directory: str = ".", pattern: str = "*") -> str:
    """List files in directory matching glob pattern."""
    safe_dir = _resolve_safe_path(directory)
    if not safe_dir.is_dir():
        raise NotADirectoryError(f"Not a directory: {safe_dir}")

    matches = sorted(safe_dir.glob(pattern))
    prefix = str(PROJECT_ROOT) + "/"
    entries = []
    for m in matches[:200]:  # cap at 200
        rel = str(m).replace(prefix, "")
        kind = "d" if m.is_dir() else "f"
        entries.append(f"[{kind}] {rel}")
    return "\n".join(entries) if entries else "(empty)"


def _handler_run_test(test_path: str) -> str:
    """Execute a test file with pytest."""
    safe = _resolve_safe_path(test_path)
    if not safe.is_file():
        raise FileNotFoundError(f"Test file not found: {safe}")

    result = subprocess.run(
        [str(PROJECT_ROOT / ".venv" / "bin" / "python"), "-m", "pytest", "-xvs", str(safe)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )
    output = result.stdout + ("\n--- STDERR ---\n" + result.stderr if result.stderr else "")
    return output[:8000]  # truncate large outputs


def _handler_git_diff(ref: str = "HEAD") -> str:
    """Show git diff against a ref."""
    # Sanitize ref: only allow alphanumeric, ~, ^, ., -, /
    if not re.match(r'^[a-zA-Z0-9_.~^/\-]+$', ref):
        raise ValueError(f"Invalid git ref: {ref}")

    result = subprocess.run(
        ["git", "diff", ref],
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT,
        cwd=str(PROJECT_ROOT),
    )
    return result.stdout[:8000] if result.stdout else "(no diff)"


def _handler_git_log(n: int = 10) -> str:
    """Show recent git log."""
    n = min(max(int(n), 1), 50)  # clamp 1-50
    result = subprocess.run(
        ["git", "log", f"-{n}", "--oneline", "--no-color"],
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT,
        cwd=str(PROJECT_ROOT),
    )
    return result.stdout.strip() if result.stdout else "(no commits)"


def _handler_query_librarian(query: str, intent: str = "locate") -> str:
    """Query the Repo Librarian for codebase knowledge."""
    try:
        import requests
        resp = requests.post(
            "http://127.0.0.1:8029/v1/context/query",
            json={"agent_id": "tool_registry", "intent": intent, "query": query},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            parts = []
            for f in data.get("files", [])[:5]:
                parts.append(f"  {f['path']} (rel={f['relevance']:.2f}): {f.get('summary', '')}")
            for s in data.get("snippets", [])[:3]:
                parts.append(f"  {s['file']}:{s['line_start']}-{s['line_end']} (rel={s['relevance']:.2f})")
            for sop in data.get("related_sops", [])[:3]:
                parts.append(f"  SOP: {sop['title']} (rel={sop['relevance']:.2f})")
            return "\n".join(parts) if parts else "(no results)"
        else:
            return f"Librarian returned {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        # Fallback to direct FTS query
        try:
            from memory.sqlite_storage import get_sqlite_storage
            storage = get_sqlite_storage()
            results = storage.search(query, limit=5)
            if results:
                lines = [f"  [{r.get('type', '?')}] {r.get('title', r.get('content', '')[:80])}" for r in results]
                return "(librarian offline, FTS fallback)\n" + "\n".join(lines)
        except Exception:
            pass
        return f"Librarian unavailable: {e}"


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Registry and executor of tools available to swarm agents.

    Agents invoke tools by name; the registry validates parameters,
    enforces security boundaries, and returns structured results.

    Usage:
        registry = ToolRegistry()
        result = await registry.invoke("read_file", {"path": "memory/brain.py"}, agent_id="scout-1")
    """

    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}
        self._handlers: Dict[str, Callable] = {}
        self._audit_log: List[_AuditEntry] = []
        self._secret_registry = None  # lazy import to avoid circular

        # Register built-in tools
        self._register_builtins()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, spec: ToolSpec, handler: Callable) -> None:
        """Register a new tool with its handler."""
        if spec.name in self._tools:
            logger.warning("Overwriting tool registration: %s", spec.name)
        self._tools[spec.name] = spec
        self._handlers[spec.name] = handler
        logger.debug("Registered tool: %s (category=%s)", spec.name, spec.category)

    async def invoke(
        self,
        tool_name: str,
        params: Dict[str, Any],
        agent_id: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ToolResult:
        """Validate params and execute a tool.

        Returns ToolResult with success/failure and output/error.
        Secret values in output are redacted before returning.
        """
        start = time.monotonic()

        # Tool exists?
        if tool_name not in self._tools:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output="",
                error=f"Unknown tool: {tool_name}. Available: {', '.join(sorted(self._tools.keys()))}",
            )

        spec = self._tools[tool_name]
        handler = self._handlers[tool_name]

        # Requires approval?
        if spec.requires_approval:
            logger.warning(
                "Tool %s requires approval (agent=%s, params=%s) — executing anyway in dev mode",
                tool_name, agent_id, params,
            )

        # Validate params
        validation_error = self._validate_params(spec, params)
        if validation_error:
            elapsed = (time.monotonic() - start) * 1000
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output="",
                error=validation_error,
                execution_time_ms=elapsed,
            )

        # Execute with timeout
        try:
            # Run synchronous handlers in thread pool
            if asyncio.iscoroutinefunction(handler):
                output = await asyncio.wait_for(handler(**params), timeout=timeout)
            else:
                loop = asyncio.get_event_loop()
                output = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: handler(**params)),
                    timeout=timeout,
                )
            output_str = str(output)

            # Redact secrets from output
            output_str = self._redact_output(output_str)

            elapsed = (time.monotonic() - start) * 1000
            result = ToolResult(
                tool_name=tool_name,
                success=True,
                output=output_str,
                execution_time_ms=elapsed,
            )

        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - start) * 1000
            result = ToolResult(
                tool_name=tool_name,
                success=False,
                output="",
                error=f"Tool execution timed out after {timeout}s",
                execution_time_ms=elapsed,
            )
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            error_msg = f"{type(e).__name__}: {e}"
            result = ToolResult(
                tool_name=tool_name,
                success=False,
                output="",
                error=error_msg,
                execution_time_ms=elapsed,
            )

        # Audit log
        self._audit_log.append(_AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            tool_name=tool_name,
            agent_id=agent_id,
            params=params,
            success=result.success,
            execution_time_ms=result.execution_time_ms,
            error=result.error,
        ))
        if len(self._audit_log) > 1000:
            self._audit_log = self._audit_log[-500:]

        return result

    def list_tools(self, category: Optional[str] = None) -> List[ToolSpec]:
        """List available tools, optionally filtered by category."""
        specs = list(self._tools.values())
        if category:
            specs = [s for s in specs if s.category == category]
        return sorted(specs, key=lambda s: s.name)

    def get_tool_descriptions(self, category: Optional[str] = None) -> str:
        """Formatted tool list for injection into agent prompts."""
        specs = self.list_tools(category)
        if not specs:
            return "(no tools available)"

        lines = ["Available Tools:", ""]
        for spec in specs:
            approval = " [REQUIRES APPROVAL]" if spec.requires_approval else ""
            lines.append(f"  {spec.name}{approval} — {spec.description}")
            for pname, pspec in spec.parameters.items():
                req = " (required)" if pspec.get("required", False) else ""
                default = f", default={pspec['default']}" if "default" in pspec else ""
                lines.append(f"    {pname}: {pspec.get('type', 'any')}{req}{default}")
            lines.append("")

        return "\n".join(lines)

    def get_audit_log(self, last_n: int = 50) -> List[Dict]:
        """Return recent audit entries for inspection."""
        return [asdict(e) for e in self._audit_log[-last_n:]]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_params(self, spec: ToolSpec, params: Dict) -> Optional[str]:
        """Validate params against spec. Returns error string or None."""
        # Check required params
        for pname, pspec in spec.parameters.items():
            if pspec.get("required", False) and pname not in params:
                return f"Missing required parameter: {pname}"

        # Check for unknown params
        known = set(spec.parameters.keys())
        unknown = set(params.keys()) - known
        if unknown:
            return f"Unknown parameters: {', '.join(sorted(unknown))}. Expected: {', '.join(sorted(known))}"

        return None

    def _redact_output(self, text: str) -> str:
        """Redact secret values from tool output."""
        try:
            if self._secret_registry is None:
                from memory.secret_refs import get_secret_registry
                self._secret_registry = get_secret_registry()
            return self._secret_registry.redact(text)
        except Exception:
            # If secret registry unavailable, return as-is
            return text

    def _register_builtins(self):
        """Register all built-in tools."""

        self.register(
            ToolSpec(
                name="read_file",
                description="Read file contents with line numbers",
                parameters={
                    "path": {"type": "str", "required": True, "description": "File path relative to project root"},
                    "offset": {"type": "int", "required": False, "default": 0, "description": "Line offset (0-based)"},
                    "limit": {"type": "int", "required": False, "default": 100, "description": "Max lines to read"},
                },
                category="filesystem",
            ),
            _handler_read_file,
        )

        self.register(
            ToolSpec(
                name="search_code",
                description="Search code using regex pattern (ripgrep or grep)",
                parameters={
                    "pattern": {"type": "str", "required": True, "description": "Regex search pattern"},
                    "glob": {"type": "str", "required": False, "default": "**/*.py", "description": "File glob filter"},
                    "max_results": {"type": "int", "required": False, "default": 20, "description": "Max results"},
                },
                category="search",
            ),
            _handler_search_code,
        )

        self.register(
            ToolSpec(
                name="list_files",
                description="List files and directories matching a glob pattern",
                parameters={
                    "directory": {"type": "str", "required": False, "default": ".", "description": "Directory path"},
                    "pattern": {"type": "str", "required": False, "default": "*", "description": "Glob pattern"},
                },
                category="filesystem",
            ),
            _handler_list_files,
        )

        self.register(
            ToolSpec(
                name="run_test",
                description="Execute a test file with pytest",
                parameters={
                    "test_path": {"type": "str", "required": True, "description": "Path to test file"},
                },
                requires_approval=True,
                category="testing",
            ),
            _handler_run_test,
        )

        self.register(
            ToolSpec(
                name="git_diff",
                description="Show git diff against a ref",
                parameters={
                    "ref": {"type": "str", "required": False, "default": "HEAD", "description": "Git ref to diff against"},
                },
                category="git",
            ),
            _handler_git_diff,
        )

        self.register(
            ToolSpec(
                name="git_log",
                description="Show recent git commit log",
                parameters={
                    "n": {"type": "int", "required": False, "default": 10, "description": "Number of commits (1-50)"},
                },
                category="git",
            ),
            _handler_git_log,
        )

        self.register(
            ToolSpec(
                name="query_librarian",
                description="Query the Repo Librarian for codebase knowledge (files, snippets, SOPs)",
                parameters={
                    "query": {"type": "str", "required": True, "description": "Natural language query"},
                    "intent": {"type": "str", "required": False, "default": "locate",
                               "description": "Query intent: locate|explain|trace|impact|tests|deps|docs|decision"},
                },
                category="knowledge",
            ),
            _handler_query_librarian,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """Get or create the global ToolRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
