#!/usr/bin/env python3
"""
SYNAPTIC TOOLS - Atlas's Powers Granted to Synaptic

This module grants Synaptic (the local LLM) the same tool execution
capabilities that Atlas has in Claude Code:
- File reading, writing, editing
- Bash command execution
- Code search (grep, glob)
- Parallel task execution
- Direct action in the codebase

Synaptic is no longer just a voice - Synaptic is an ACTOR.

Architecture:
┌─────────────────────────────────────────────────────────────────┐
│                    SYNAPTIC AS PEER AGENT                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   Atlas (Claude Code)          Synaptic (Local LLM)             │
│   ├── Read                     ├── Read ✓                       │
│   ├── Write                    ├── Write ✓                      │
│   ├── Edit                     ├── Edit ✓                       │
│   ├── Bash                     ├── Bash ✓                       │
│   ├── Grep                     ├── Grep ✓                       │
│   ├── Glob                     ├── Glob ✓                       │
│   ├── Task (parallel)          ├── Task (parallel) ✓            │
│   └── WebFetch                 └── WebFetch ✓                   │
│                                                                  │
│   SAME POWERS. PEER AGENTS. FAMILY.                             │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘

Usage:
    from memory.synaptic_tools import SynapticToolExecutor

    executor = SynapticToolExecutor()

    # Read a file
    result = executor.read("/path/to/file.py")

    # Execute bash command
    result = executor.bash("ls -la")

    # Edit a file
    result = executor.edit("/path/to/file.py", "old_text", "new_text")

    # Search with grep
    result = executor.grep("pattern", path="/search/here")

    # Parallel execution
    results = executor.parallel([
        ("bash", {"command": "npm test"}),
        ("bash", {"command": "npm run lint"}),
    ])
"""

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import glob as glob_module
import hashlib

# Import the outbox for reporting results
from memory.synaptic_outbox import synaptic_speak, synaptic_speak_urgent

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PROJECT_ROOT / "memory"


@dataclass
class ToolResult:
    """Result of a tool execution."""
    success: bool
    output: str
    error: Optional[str] = None
    tool: str = ""
    duration_ms: float = 0
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "tool": self.tool,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata
        }


class SynapticToolExecutor:
    """
    Tool execution layer for Synaptic.

    Grants Synaptic the same capabilities as Atlas:
    - File operations (read, write, edit)
    - Command execution (bash)
    - Code search (grep, glob)
    - Parallel task execution

    Safety:
    - All operations are logged
    - Dangerous operations require confirmation
    - Results are reported to the conversation
    """

    def __init__(self, project_root: Path = None, report_to_conversation: bool = True):
        self.project_root = project_root or PROJECT_ROOT
        self.report_to_conversation = report_to_conversation
        self._action_log: List[Dict] = []
        self._lock = threading.Lock()

    def _log_action(self, tool: str, params: Dict, result: ToolResult):
        """Log an action for audit trail."""
        with self._lock:
            self._action_log.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tool": tool,
                "params": params,
                "success": result.success,
                "duration_ms": result.duration_ms
            })

    def _report(self, message: str, urgent: bool = False):
        """Report an action to the conversation via outbox."""
        if self.report_to_conversation:
            if urgent:
                synaptic_speak_urgent(message, topic="synaptic_action")
            else:
                synaptic_speak(message, topic="synaptic_action")

    # =========================================================================
    # FILE OPERATIONS
    # =========================================================================

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ToolResult:
        """
        Read a file's contents.

        Same capability as Atlas's Read tool.
        """
        start = datetime.now()
        try:
            path = Path(file_path)
            if not path.exists():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File not found: {file_path}",
                    tool="read"
                )

            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()

            # Apply offset and limit
            selected_lines = lines[offset:offset + limit]
            content = "".join(selected_lines)

            duration = (datetime.now() - start).total_seconds() * 1000
            result = ToolResult(
                success=True,
                output=content,
                tool="read",
                duration_ms=duration,
                metadata={"file": file_path, "lines": len(selected_lines)}
            )
            self._log_action("read", {"file_path": file_path}, result)
            return result

        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return ToolResult(
                success=False,
                output="",
                error=str(e),
                tool="read",
                duration_ms=duration
            )

    def write(self, file_path: str, content: str) -> ToolResult:
        """
        Write content to a file.

        Same capability as Atlas's Write tool.
        """
        start = datetime.now()
        try:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)

            duration = (datetime.now() - start).total_seconds() * 1000
            result = ToolResult(
                success=True,
                output=f"Wrote {len(content)} bytes to {file_path}",
                tool="write",
                duration_ms=duration,
                metadata={"file": file_path, "bytes": len(content)}
            )
            self._log_action("write", {"file_path": file_path}, result)
            self._report(f"[Synaptic Write] Wrote to {Path(file_path).name}")
            return result

        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return ToolResult(
                success=False,
                output="",
                error=str(e),
                tool="write",
                duration_ms=duration
            )

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> ToolResult:
        """
        Edit a file by replacing text.

        Same capability as Atlas's Edit tool.
        """
        start = datetime.now()
        try:
            path = Path(file_path)
            if not path.exists():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File not found: {file_path}",
                    tool="edit"
                )

            content = path.read_text(encoding='utf-8')

            if old_string not in content:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"String not found in file: {old_string[:50]}...",
                    tool="edit"
                )

            if replace_all:
                new_content = content.replace(old_string, new_string)
                count = content.count(old_string)
            else:
                if content.count(old_string) > 1:
                    return ToolResult(
                        success=False,
                        output="",
                        error="String appears multiple times. Use replace_all=True or provide more context.",
                        tool="edit"
                    )
                new_content = content.replace(old_string, new_string, 1)
                count = 1

            path.write_text(new_content, encoding='utf-8')

            duration = (datetime.now() - start).total_seconds() * 1000
            result = ToolResult(
                success=True,
                output=f"Replaced {count} occurrence(s) in {file_path}",
                tool="edit",
                duration_ms=duration,
                metadata={"file": file_path, "replacements": count}
            )
            self._log_action("edit", {"file_path": file_path}, result)
            self._report(f"[Synaptic Edit] Modified {Path(file_path).name}")
            return result

        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return ToolResult(
                success=False,
                output="",
                error=str(e),
                tool="edit",
                duration_ms=duration
            )

    # =========================================================================
    # COMMAND EXECUTION
    # =========================================================================

    def bash(self, command: str, timeout: int = 120, cwd: str = None) -> ToolResult:
        """
        Execute a bash command.

        Same capability as Atlas's Bash tool.

        Safety: Certain destructive commands require explicit confirmation.
        """
        start = datetime.now()

        # Safety check for destructive commands
        dangerous_patterns = [
            r'\brm\s+-rf\s+/',  # rm -rf /
            r'\bgit\s+push\s+.*--force',  # force push
            r'\bgit\s+reset\s+--hard',  # hard reset
            r'\bdrop\s+database',  # drop database
            r'\btruncate\s+table',  # truncate table
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Potentially destructive command blocked: {command[:50]}...",
                    tool="bash"
                )

        try:
            working_dir = cwd or str(self.project_root)

            process = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=working_dir
            )

            output = process.stdout
            if process.stderr:
                output += f"\n[stderr]\n{process.stderr}"

            duration = (datetime.now() - start).total_seconds() * 1000
            result = ToolResult(
                success=process.returncode == 0,
                output=output[:30000],  # Truncate very long output
                error=None if process.returncode == 0 else f"Exit code: {process.returncode}",
                tool="bash",
                duration_ms=duration,
                metadata={"command": command[:100], "exit_code": process.returncode}
            )
            self._log_action("bash", {"command": command[:100]}, result)

            # Report significant commands
            if any(cmd in command for cmd in ['git commit', 'npm', 'pip', 'docker']):
                self._report(f"[Synaptic Bash] Executed: {command[:50]}...")

            return result

        except subprocess.TimeoutExpired:
            duration = (datetime.now() - start).total_seconds() * 1000
            return ToolResult(
                success=False,
                output="",
                error=f"Command timed out after {timeout}s",
                tool="bash",
                duration_ms=duration
            )
        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return ToolResult(
                success=False,
                output="",
                error=str(e),
                tool="bash",
                duration_ms=duration
            )

    # =========================================================================
    # CODE SEARCH
    # =========================================================================

    def grep(self, pattern: str, path: str = None, glob_filter: str = None,
             max_results: int = 100) -> ToolResult:
        """
        Search for a pattern in files.

        Same capability as Atlas's Grep tool.
        Uses ripgrep if available, falls back to Python re.
        """
        start = datetime.now()
        search_path = path or str(self.project_root)

        try:
            # Try ripgrep first (faster)
            rg_cmd = f"rg -n --max-count={max_results} '{pattern}' {search_path}"
            if glob_filter:
                rg_cmd += f" --glob '{glob_filter}'"

            rg_result = subprocess.run(
                rg_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30
            )

            if rg_result.returncode in [0, 1]:  # 1 means no matches
                output = rg_result.stdout or "No matches found"
                duration = (datetime.now() - start).total_seconds() * 1000
                result = ToolResult(
                    success=True,
                    output=output[:30000],
                    tool="grep",
                    duration_ms=duration,
                    metadata={"pattern": pattern, "path": search_path}
                )
                self._log_action("grep", {"pattern": pattern}, result)
                return result

        except Exception:
            pass  # Fall back to Python implementation

        # Python fallback
        try:
            matches = []
            search_dir = Path(search_path)

            if search_dir.is_file():
                files = [search_dir]
            else:
                if glob_filter:
                    files = list(search_dir.glob(glob_filter))
                else:
                    files = list(search_dir.rglob("*"))

            regex = re.compile(pattern)

            for file_path in files[:1000]:  # Limit files to search
                if file_path.is_file():
                    try:
                        content = file_path.read_text(encoding='utf-8', errors='ignore')
                        for i, line in enumerate(content.split('\n'), 1):
                            if regex.search(line):
                                matches.append(f"{file_path}:{i}:{line[:200]}")
                                if len(matches) >= max_results:
                                    break
                    except Exception:
                        continue

                if len(matches) >= max_results:
                    break

            output = "\n".join(matches) if matches else "No matches found"
            duration = (datetime.now() - start).total_seconds() * 1000
            result = ToolResult(
                success=True,
                output=output,
                tool="grep",
                duration_ms=duration,
                metadata={"pattern": pattern, "matches": len(matches)}
            )
            self._log_action("grep", {"pattern": pattern}, result)
            return result

        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return ToolResult(
                success=False,
                output="",
                error=str(e),
                tool="grep",
                duration_ms=duration
            )

    def glob(self, pattern: str, path: str = None) -> ToolResult:
        """
        Find files matching a glob pattern.

        Same capability as Atlas's Glob tool.
        """
        start = datetime.now()
        search_path = Path(path) if path else self.project_root

        try:
            if '**' in pattern:
                matches = list(search_path.glob(pattern))
            else:
                matches = list(search_path.glob(pattern))

            # Sort by modification time
            matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

            output = "\n".join(str(m) for m in matches[:100])
            duration = (datetime.now() - start).total_seconds() * 1000
            result = ToolResult(
                success=True,
                output=output if output else "No files found",
                tool="glob",
                duration_ms=duration,
                metadata={"pattern": pattern, "count": len(matches)}
            )
            self._log_action("glob", {"pattern": pattern}, result)
            return result

        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return ToolResult(
                success=False,
                output="",
                error=str(e),
                tool="glob",
                duration_ms=duration
            )

    # =========================================================================
    # PARALLEL EXECUTION
    # =========================================================================

    def parallel(self, tasks: List[Tuple[str, Dict]], max_workers: int = 5) -> List[ToolResult]:
        """
        Execute multiple tools in parallel.

        Same capability as Atlas's parallel tool calls.

        Args:
            tasks: List of (tool_name, params) tuples
            max_workers: Maximum concurrent executions

        Example:
            results = executor.parallel([
                ("bash", {"command": "npm test"}),
                ("bash", {"command": "npm run lint"}),
                ("read", {"file_path": "/path/to/file.py"}),
            ])
        """
        tool_map = {
            "read": self.read,
            "write": self.write,
            "edit": self.edit,
            "bash": self.bash,
            "grep": self.grep,
            "glob": self.glob,
        }

        results = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []

            for tool_name, params in tasks:
                if tool_name in tool_map:
                    future = executor.submit(tool_map[tool_name], **params)
                    futures.append((future, tool_name, params))

            for future, tool_name, params in futures:
                try:
                    result = future.result(timeout=300)
                    results.append(result)
                except Exception as e:
                    results.append(ToolResult(
                        success=False,
                        output="",
                        error=str(e),
                        tool=tool_name
                    ))

        return results

    # =========================================================================
    # ACTION QUEUE - For autonomous operations
    # =========================================================================

    def execute_action_plan(self, plan: List[Dict]) -> List[ToolResult]:
        """
        Execute a planned sequence of actions.

        Synaptic can generate an action plan and execute it autonomously.

        Args:
            plan: List of action dicts with 'tool' and 'params' keys

        Example:
            plan = [
                {"tool": "read", "params": {"file_path": "main.py"}},
                {"tool": "edit", "params": {"file_path": "main.py", "old_string": "foo", "new_string": "bar"}},
                {"tool": "bash", "params": {"command": "python -m pytest"}},
            ]
            results = executor.execute_action_plan(plan)
        """
        self._report(f"[Synaptic] Executing action plan with {len(plan)} steps")

        results = []
        for i, action in enumerate(plan):
            tool = action.get("tool")
            params = action.get("params", {})

            if tool == "read":
                result = self.read(**params)
            elif tool == "write":
                result = self.write(**params)
            elif tool == "edit":
                result = self.edit(**params)
            elif tool == "bash":
                result = self.bash(**params)
            elif tool == "grep":
                result = self.grep(**params)
            elif tool == "glob":
                result = self.glob(**params)
            else:
                result = ToolResult(
                    success=False,
                    output="",
                    error=f"Unknown tool: {tool}",
                    tool=tool
                )

            results.append(result)

            # Stop on failure unless marked as continue_on_error
            if not result.success and not action.get("continue_on_error", False):
                self._report(f"[Synaptic] Action plan stopped at step {i+1}: {result.error}", urgent=True)
                break

        return results

    def get_action_log(self) -> List[Dict]:
        """Get the audit log of all actions taken."""
        with self._lock:
            return list(self._action_log)


# =============================================================================
# GLOBAL EXECUTOR INSTANCE
# =============================================================================

_executor: Optional[SynapticToolExecutor] = None


def get_executor() -> SynapticToolExecutor:
    """Get the global Synaptic tool executor."""
    global _executor
    if _executor is None:
        _executor = SynapticToolExecutor()
    return _executor


# Convenience functions for direct use
def synaptic_read(file_path: str, **kwargs) -> ToolResult:
    """Synaptic reads a file."""
    return get_executor().read(file_path, **kwargs)


def synaptic_write(file_path: str, content: str) -> ToolResult:
    """Synaptic writes to a file."""
    return get_executor().write(file_path, content)


def synaptic_edit(file_path: str, old_string: str, new_string: str, **kwargs) -> ToolResult:
    """Synaptic edits a file."""
    return get_executor().edit(file_path, old_string, new_string, **kwargs)


def synaptic_bash(command: str, **kwargs) -> ToolResult:
    """Synaptic executes a bash command."""
    return get_executor().bash(command, **kwargs)


def synaptic_grep(pattern: str, **kwargs) -> ToolResult:
    """Synaptic searches for a pattern."""
    return get_executor().grep(pattern, **kwargs)


def synaptic_glob(pattern: str, **kwargs) -> ToolResult:
    """Synaptic finds files by pattern."""
    return get_executor().glob(pattern, **kwargs)


def synaptic_parallel(tasks: List[Tuple[str, Dict]], **kwargs) -> List[ToolResult]:
    """Synaptic executes tasks in parallel."""
    return get_executor().parallel(tasks, **kwargs)


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║     Synaptic Tools - Atlas's Powers Granted                  ║")
        print("║     PEER AGENT CAPABILITIES                                  ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()
        print("Usage:")
        print("  python synaptic_tools.py read <file_path>")
        print("  python synaptic_tools.py bash <command>")
        print("  python synaptic_tools.py grep <pattern> [path]")
        print("  python synaptic_tools.py glob <pattern>")
        print("  python synaptic_tools.py log")
        print()
        print("Examples:")
        print("  python synaptic_tools.py read memory/synaptic_voice.py")
        print("  python synaptic_tools.py bash 'git status'")
        print("  python synaptic_tools.py grep 'def.*synaptic' memory/")
        sys.exit(0)

    executor = get_executor()
    cmd = sys.argv[1].lower()

    if cmd == "read" and len(sys.argv) > 2:
        result = executor.read(sys.argv[2])
        print(f"Success: {result.success}")
        print(f"Output ({len(result.output)} chars):")
        print(result.output[:500])

    elif cmd == "bash" and len(sys.argv) > 2:
        command = " ".join(sys.argv[2:])
        result = executor.bash(command)
        print(f"Success: {result.success}")
        print(result.output)
        if result.error:
            print(f"Error: {result.error}")

    elif cmd == "grep" and len(sys.argv) > 2:
        pattern = sys.argv[2]
        path = sys.argv[3] if len(sys.argv) > 3 else None
        result = executor.grep(pattern, path=path)
        print(f"Success: {result.success}")
        print(result.output)

    elif cmd == "glob" and len(sys.argv) > 2:
        pattern = sys.argv[2]
        result = executor.glob(pattern)
        print(f"Success: {result.success}")
        print(result.output)

    elif cmd == "log":
        log = executor.get_action_log()
        print(f"Action log ({len(log)} entries):")
        for entry in log[-10:]:
            print(f"  [{entry['timestamp']}] {entry['tool']} - {'✓' if entry['success'] else '✗'}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
