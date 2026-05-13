"""Context DNA Doctor - Health check and diagnostics.

Provides a user-friendly way to verify installation and diagnose issues.

Usage:
    context-dna doctor           Run all health checks
    context-dna doctor --fix     Attempt to fix issues automatically
    context-dna doctor --json    Output results as JSON

Example output:
    $ context-dna doctor

    Context DNA Health Check
    ========================

    ✓ Storage (SQLite)      Connected, 47 learnings
    ✓ Secret Sanitizer      30+ patterns active
    ✓ API Server            Port 3456 available
    ✓ LLM Provider          Ollama detected (qwen2.5:7b)
    ⚠ Claude Hook           Not installed
    ✓ Git Hook              Active in 2 repos

    Summary: 5/6 checks passed
"""

import json
import os
import socket
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from rich.console import Console
    from rich.table import Table
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


class CheckStatus(Enum):
    """Status of a health check."""
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    """Result of a single health check."""
    name: str
    status: CheckStatus
    message: str
    details: Optional[str] = None
    fix_command: Optional[str] = None
    fix_hint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "fix_command": self.fix_command,
            "fix_hint": self.fix_hint,
        }


@dataclass
class DoctorReport:
    """Complete doctor report."""
    checks: List[CheckResult] = field(default_factory=list)
    version: str = "0.1.0"

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.PASS)

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.WARN)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.FAIL)

    @property
    def total(self) -> int:
        return len([c for c in self.checks if c.status != CheckStatus.SKIP])

    @property
    def healthy(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "healthy": self.healthy,
            "summary": {
                "passed": self.passed,
                "warnings": self.warnings,
                "failed": self.failed,
                "total": self.total,
            },
            "checks": [c.to_dict() for c in self.checks],
        }


class Doctor:
    """Context DNA health checker."""

    def __init__(self, project_dir: Optional[Path] = None):
        self.project_dir = project_dir or Path.cwd()
        self.user_data_dir = Path.home() / ".context-dna"
        self.report = DoctorReport()

    def run_all_checks(self) -> DoctorReport:
        """Run all health checks and return report."""
        self.report = DoctorReport()

        # Core checks
        self._check_storage()
        self._check_sanitizer()
        self._check_api_server()
        self._check_llm_provider()

        # Hook checks
        self._check_claude_hook()
        self._check_cursor_hook()
        self._check_git_hook()

        # Optional checks
        self._check_docker()

        return self.report

    def _add_result(self, result: CheckResult) -> None:
        """Add a check result to the report."""
        self.report.checks.append(result)

    # =========================================================================
    # Core Checks
    # =========================================================================

    def _check_storage(self) -> None:
        """Check SQLite storage backend."""
        try:
            from context_dna.brain import Brain
            brain = Brain(project_dir=str(self.project_dir))

            if brain._storage:
                stats = brain._storage.get_stats()
                total = stats.get("total", 0)
                self._add_result(CheckResult(
                    name="Storage (SQLite)",
                    status=CheckStatus.PASS,
                    message=f"Connected, {total} learnings",
                    details=f"Database: {brain._storage.db_path}",
                ))
            else:
                # Using fallback adapter
                self._add_result(CheckResult(
                    name="Storage (SQLite)",
                    status=CheckStatus.WARN,
                    message="Using fallback (no local DB)",
                    details="Initialize with: context-dna init",
                    fix_command="context-dna init",
                ))
        except Exception as e:
            self._add_result(CheckResult(
                name="Storage (SQLite)",
                status=CheckStatus.FAIL,
                message=f"Error: {e}",
                fix_hint="Try: context-dna init",
            ))

    def _check_sanitizer(self) -> None:
        """Check secret sanitizer is working."""
        try:
            from context_dna.security.sanitizer import (
                sanitize_secrets,
                get_all_patterns,
            )

            patterns = get_all_patterns()
            pattern_count = len(patterns)

            # Test sanitization
            test_content = "sk-proj-abc123def456ghi789jkl012mno345pqr678"
            sanitized = sanitize_secrets(test_content)

            if "${OPENAI_KEY}" in sanitized:
                self._add_result(CheckResult(
                    name="Secret Sanitizer",
                    status=CheckStatus.PASS,
                    message=f"{pattern_count} patterns active",
                    details="Secrets are protected before storage",
                ))
            else:
                self._add_result(CheckResult(
                    name="Secret Sanitizer",
                    status=CheckStatus.WARN,
                    message="Sanitization not working as expected",
                    details=f"Test input was not sanitized properly",
                ))
        except ImportError:
            self._add_result(CheckResult(
                name="Secret Sanitizer",
                status=CheckStatus.FAIL,
                message="Module not found",
                fix_hint="Reinstall: pip install -e context-dna/core",
            ))
        except Exception as e:
            self._add_result(CheckResult(
                name="Secret Sanitizer",
                status=CheckStatus.FAIL,
                message=f"Error: {e}",
            ))

    def _check_api_server(self) -> None:
        """Check API server status."""
        port = 3456

        # Check if port is in use (server running)
        if self._is_port_in_use(port):
            # Try to connect to health endpoint
            try:
                import urllib.request
                req = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health",
                    timeout=2
                )
                data = json.loads(req.read())
                if data.get("status") == "healthy":
                    self._add_result(CheckResult(
                        name="API Server",
                        status=CheckStatus.PASS,
                        message=f"Running on port {port}",
                        details=f"Version: {data.get('version', 'unknown')}",
                    ))
                    return
            except Exception:
                pass

            self._add_result(CheckResult(
                name="API Server",
                status=CheckStatus.WARN,
                message=f"Port {port} in use but not responding",
                details="Another service may be using the port",
            ))
        else:
            self._add_result(CheckResult(
                name="API Server",
                status=CheckStatus.PASS,
                message=f"Port {port} available",
                details="Start with: context-dna serve",
            ))

    def _check_llm_provider(self) -> None:
        """Check LLM provider availability."""
        providers_found = []

        # Check Ollama
        if self._check_ollama():
            model = self._get_ollama_model()
            providers_found.append(f"Ollama ({model})" if model else "Ollama")

        # Check OpenAI
        if os.environ.get("Context_DNA_OPENAI"):
            providers_found.append("OpenAI")

        # Check Anthropic
        if os.environ.get("ANTHROPIC_API_KEY"):
            providers_found.append("Anthropic")

        if providers_found:
            self._add_result(CheckResult(
                name="LLM Provider",
                status=CheckStatus.PASS,
                message=", ".join(providers_found),
                details="AI features available",
            ))
        else:
            self._add_result(CheckResult(
                name="LLM Provider",
                status=CheckStatus.WARN,
                message="No providers configured",
                details="Set Context_DNA_OPENAI or install Ollama for AI features",
                fix_hint="export Context_DNA_OPENAI=sk-...",
            ))

    # =========================================================================
    # Hook Checks
    # =========================================================================

    def _check_claude_hook(self) -> None:
        """Check Claude Code hook installation."""
        hook_dir = Path.home() / ".claude" / "hooks"
        hook_file = hook_dir / "UserPromptSubmit.md"

        if hook_file.exists():
            content = hook_file.read_text()
            if "context-dna" in content.lower() or "context_dna" in content.lower():
                self._add_result(CheckResult(
                    name="Claude Hook",
                    status=CheckStatus.PASS,
                    message="Installed and active",
                    details=str(hook_file),
                ))
            else:
                self._add_result(CheckResult(
                    name="Claude Hook",
                    status=CheckStatus.WARN,
                    message="Hook exists but may not include Context DNA",
                    details="Check hook content manually",
                ))
        else:
            self._add_result(CheckResult(
                name="Claude Hook",
                status=CheckStatus.WARN,
                message="Not installed",
                fix_command="context-dna hooks install claude",
                fix_hint="Enables automatic learning capture in Claude Code",
            ))

    def _check_cursor_hook(self) -> None:
        """Check Cursor hook installation."""
        # Cursor uses .cursorrules file
        cursorrules = self.project_dir / ".cursorrules"

        if cursorrules.exists():
            content = cursorrules.read_text()
            if "context-dna" in content.lower() or "context_dna" in content.lower():
                self._add_result(CheckResult(
                    name="Cursor Hook",
                    status=CheckStatus.PASS,
                    message="Configured in .cursorrules",
                    details=str(cursorrules),
                ))
            else:
                self._add_result(CheckResult(
                    name="Cursor Hook",
                    status=CheckStatus.SKIP,
                    message=".cursorrules exists (not Context DNA)",
                ))
        else:
            self._add_result(CheckResult(
                name="Cursor Hook",
                status=CheckStatus.SKIP,
                message="Not configured (optional)",
                fix_command="context-dna hooks install cursor",
            ))

    def _check_git_hook(self) -> None:
        """Check git hook installation."""
        git_dir = self.project_dir / ".git"
        hook_file = git_dir / "hooks" / "post-commit"

        if not git_dir.exists():
            self._add_result(CheckResult(
                name="Git Hook",
                status=CheckStatus.SKIP,
                message="Not a git repository",
            ))
            return

        if hook_file.exists():
            content = hook_file.read_text()
            if "context-dna" in content or "context_dna" in content:
                self._add_result(CheckResult(
                    name="Git Hook",
                    status=CheckStatus.PASS,
                    message="Installed (post-commit)",
                    details="Auto-captures learnings from commits",
                ))
            else:
                self._add_result(CheckResult(
                    name="Git Hook",
                    status=CheckStatus.WARN,
                    message="post-commit hook exists but doesn't use Context DNA",
                    fix_hint="Manually add Context DNA to existing hook",
                ))
        else:
            self._add_result(CheckResult(
                name="Git Hook",
                status=CheckStatus.WARN,
                message="Not installed",
                fix_command="context-dna hooks install git",
                fix_hint="Auto-captures learnings from git commits",
            ))

    # =========================================================================
    # Optional Checks
    # =========================================================================

    def _check_docker(self) -> None:
        """Check Docker infrastructure (optional)."""
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", "name=contextdna", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                containers = [c for c in result.stdout.strip().split("\n") if c]
                if containers:
                    self._add_result(CheckResult(
                        name="Docker Services",
                        status=CheckStatus.PASS,
                        message=f"{len(containers)} containers running",
                        details=", ".join(containers[:3]) + ("..." if len(containers) > 3 else ""),
                    ))
                else:
                    self._add_result(CheckResult(
                        name="Docker Services",
                        status=CheckStatus.SKIP,
                        message="No Context DNA containers (optional)",
                        fix_hint="Start with: context-dna setup",
                    ))
            else:
                self._add_result(CheckResult(
                    name="Docker Services",
                    status=CheckStatus.SKIP,
                    message="Docker not running (optional)",
                ))
        except FileNotFoundError:
            self._add_result(CheckResult(
                name="Docker Services",
                status=CheckStatus.SKIP,
                message="Docker not installed (optional)",
            ))
        except Exception:
            self._add_result(CheckResult(
                name="Docker Services",
                status=CheckStatus.SKIP,
                message="Could not check Docker",
            ))

    # =========================================================================
    # Helpers
    # =========================================================================

    def _is_port_in_use(self, port: int) -> bool:
        """Check if a port is in use."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _check_ollama(self) -> bool:
        """Check if Ollama is available."""
        try:
            import urllib.request
            req = urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
            return req.status == 200
        except Exception:
            return False

    def _get_ollama_model(self) -> Optional[str]:
        """Get the first available Ollama model."""
        try:
            import urllib.request
            req = urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
            data = json.loads(req.read())
            models = data.get("models", [])
            if models:
                return models[0].get("name", "unknown")
        except Exception:
            pass
        return None


# =============================================================================
# Output Formatters
# =============================================================================

def format_report_rich(report: DoctorReport) -> None:
    """Format report using rich library."""
    console = Console()

    console.print()
    console.print("[bold]Context DNA Health Check[/bold]")
    console.print("=" * 26)
    console.print()

    # Status icons
    icons = {
        CheckStatus.PASS: "[green]✓[/green]",
        CheckStatus.WARN: "[yellow]⚠[/yellow]",
        CheckStatus.FAIL: "[red]✗[/red]",
        CheckStatus.SKIP: "[dim]○[/dim]",
    }

    for check in report.checks:
        icon = icons[check.status]
        name = f"{check.name:<20}"
        console.print(f"{icon} {name} {check.message}")

        if check.details and check.status != CheckStatus.PASS:
            console.print(f"  [dim]{check.details}[/dim]")

        if check.fix_command and check.status in (CheckStatus.WARN, CheckStatus.FAIL):
            console.print(f"  [cyan]Fix: {check.fix_command}[/cyan]")

    console.print()

    # Summary
    if report.healthy:
        console.print(f"[green]Summary: {report.passed}/{report.total} checks passed[/green]")
    else:
        console.print(f"[red]Summary: {report.passed}/{report.total} checks passed, {report.failed} failed[/red]")

    if report.warnings > 0:
        console.print(f"[yellow]{report.warnings} warnings[/yellow]")

    console.print()


def format_report_plain(report: DoctorReport) -> None:
    """Format report using plain text."""
    print()
    print("Context DNA Health Check")
    print("=" * 26)
    print()

    # Status icons
    icons = {
        CheckStatus.PASS: "✓",
        CheckStatus.WARN: "⚠",
        CheckStatus.FAIL: "✗",
        CheckStatus.SKIP: "○",
    }

    for check in report.checks:
        icon = icons[check.status]
        name = f"{check.name:<20}"
        print(f"{icon} {name} {check.message}")

        if check.details and check.status != CheckStatus.PASS:
            print(f"    {check.details}")

        if check.fix_command and check.status in (CheckStatus.WARN, CheckStatus.FAIL):
            print(f"    Fix: {check.fix_command}")

    print()

    # Summary
    if report.healthy:
        print(f"Summary: {report.passed}/{report.total} checks passed")
    else:
        print(f"Summary: {report.passed}/{report.total} checks passed, {report.failed} failed")

    if report.warnings > 0:
        print(f"{report.warnings} warnings")

    print()


def format_report_json(report: DoctorReport) -> None:
    """Format report as JSON."""
    print(json.dumps(report.to_dict(), indent=2))


def run_doctor(
    project_dir: Optional[Path] = None,
    output_format: str = "auto",
    fix: bool = False,
) -> int:
    """Run doctor and return exit code.

    Args:
        project_dir: Project directory to check
        output_format: "auto", "rich", "plain", or "json"
        fix: Attempt to fix issues (not implemented yet)

    Returns:
        0 if healthy, 1 if issues found
    """
    doctor = Doctor(project_dir)
    report = doctor.run_all_checks()

    # Choose output format
    if output_format == "json":
        format_report_json(report)
    elif output_format == "rich" or (output_format == "auto" and RICH_AVAILABLE):
        format_report_rich(report)
    else:
        format_report_plain(report)

    return 0 if report.healthy else 1


# CLI entry point (for standalone use)
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Context DNA Health Check")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--fix", action="store_true", help="Attempt to fix issues")

    args = parser.parse_args()

    output_format = "json" if args.json else "auto"
    exit_code = run_doctor(output_format=output_format, fix=args.fix)
    exit(exit_code)
