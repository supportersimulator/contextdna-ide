"""
Webhook Installation Verifier for Context DNA

Verifies that webhook installation is complete and functional.
Run after install to catch issues early.

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

# Add parent directory and grandparent to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class CheckResult:
    """Result of a single verification check."""
    check_id: str
    description: str
    passed: bool
    message: str = ""
    details: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return {
            "check_id": self.check_id,
            "description": self.description,
            "passed": self.passed,
            "message": self.message,
            "details": self.details or {}
        }


@dataclass
class VerificationReport:
    """Complete verification report."""
    passed: bool
    results: List[CheckResult]
    timestamp: datetime = field(default_factory=datetime.now)
    total_checks: int = 0
    passed_checks: int = 0
    failed_checks: int = 0

    def __post_init__(self):
        self.total_checks = len(self.results)
        self.passed_checks = sum(1 for r in self.results if r.passed)
        self.failed_checks = self.total_checks - self.passed_checks

    def to_dict(self) -> Dict:
        return {
            "passed": self.passed,
            "timestamp": self.timestamp.isoformat(),
            "total_checks": self.total_checks,
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "results": [r.to_dict() for r in self.results]
        }

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            "║  WEBHOOK INSTALLATION VERIFICATION                          ║",
            "╠══════════════════════════════════════════════════════════════╣"
        ]

        for result in self.results:
            icon = "✅" if result.passed else "❌"
            lines.append(f"   {icon} {result.description}")
            if not result.passed and result.message:
                lines.append(f"      └─ {result.message}")

        lines.append("╠══════════════════════════════════════════════════════════════╣")
        lines.append(f"   Results: {self.passed_checks}/{self.total_checks} checks passed")

        if self.passed:
            lines.append("   ✅ VERIFICATION PASSED - Webhooks are operational")
        else:
            lines.append("   ❌ VERIFICATION FAILED - Run 'context-dna verify --fix'")

        lines.append("╚══════════════════════════════════════════════════════════════╝")

        return "\n".join(lines)


class WebhookVerifier:
    """Verify webhook installation is complete and functional."""

    # Verification checks in order of importance
    VERIFICATION_CHECKS = [
        ("hook_file_exists", "Check hook configuration file exists"),
        ("hook_syntax_valid", "Validate hook file syntax"),
        ("python_venv_active", "Check Python venv is accessible"),
        ("memory_module_importable", "Verify memory module imports"),
        ("section_0_works", "Test SAFETY section (Section 0)"),
        ("section_6_works", "Test HOLISTIC_CONTEXT section (Section 6)"),
        ("full_injection_works", "Test complete injection pipeline"),
        ("auto_memory_script_exists", "Check auto-memory-query.sh exists"),
        ("auto_memory_script_executable", "Check auto-memory-query.sh is executable"),
    ]

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = project_root or Path(__file__).parent.parent
        self.memory_dir = self.project_root / "memory"
        self.scripts_dir = self.project_root / "scripts"

    def verify_installation(self) -> VerificationReport:
        """Run all verification checks."""
        results = []

        for check_id, description in self.VERIFICATION_CHECKS:
            method = getattr(self, f"_check_{check_id}", None)
            if method:
                try:
                    passed, message, details = method()
                    results.append(CheckResult(
                        check_id=check_id,
                        description=description,
                        passed=passed,
                        message=message,
                        details=details
                    ))
                except Exception as e:
                    results.append(CheckResult(
                        check_id=check_id,
                        description=description,
                        passed=False,
                        message=f"Check failed with error: {str(e)[:100]}"
                    ))
            else:
                results.append(CheckResult(
                    check_id=check_id,
                    description=description,
                    passed=False,
                    message=f"Check method not implemented: _check_{check_id}"
                ))

        return VerificationReport(
            passed=all(r.passed for r in results),
            results=results
        )

    def fix_issues(self) -> List[str]:
        """Attempt to auto-fix common issues."""
        fixes_applied = []

        # Fix 1: Make auto-memory-query.sh executable
        script_path = self.scripts_dir / "auto-memory-query.sh"
        if script_path.exists() and not os.access(script_path, os.X_OK):
            os.chmod(script_path, 0o755)
            fixes_applied.append(f"Made {script_path} executable")

        # Fix 2: Create missing .claude directory
        claude_dir = Path.home() / ".claude"
        if not claude_dir.exists():
            claude_dir.mkdir(parents=True, exist_ok=True)
            fixes_applied.append(f"Created {claude_dir}")

        # Fix 3: Create default hook configuration if missing
        hook_file = claude_dir / "settings.local.json"
        if not hook_file.exists():
            default_config = {
                "hooks": {
                    "UserPromptSubmit": [
                        f"bash {self.scripts_dir / 'auto-memory-query.sh'} \"$PROMPT\""
                    ]
                }
            }
            hook_file.write_text(json.dumps(default_config, indent=2))
            fixes_applied.append(f"Created default hook configuration at {hook_file}")

        return fixes_applied

    # Individual check methods

    def _check_hook_file_exists(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check if .claude/settings.json or settings.local.json exists."""
        hook_paths = [
            Path.home() / ".claude" / "settings.json",
            Path.home() / ".claude" / "settings.local.json",
            self.project_root / ".claude" / "settings.json",
            self.project_root / ".claude" / "settings.local.json",
        ]

        for path in hook_paths:
            if path.exists():
                return True, f"Found at {path}", {"path": str(path)}

        return False, "Hook file not found in standard locations", {
            "searched": [str(p) for p in hook_paths]
        }

    def _check_hook_syntax_valid(self) -> Tuple[bool, str, Optional[Dict]]:
        """Validate hook file JSON syntax."""
        hook_paths = [
            Path.home() / ".claude" / "settings.json",
            Path.home() / ".claude" / "settings.local.json",
            self.project_root / ".claude" / "settings.json",
            self.project_root / ".claude" / "settings.local.json",
        ]

        for path in hook_paths:
            if path.exists():
                try:
                    content = json.loads(path.read_text())
                    if "hooks" in content:
                        return True, "Valid JSON with hooks section", {"path": str(path)}
                    else:
                        return False, "Missing 'hooks' section in config", {"path": str(path)}
                except json.JSONDecodeError as e:
                    return False, f"Invalid JSON: {str(e)[:50]}", {"path": str(path)}

        return False, "No hook file found to validate", None

    def _check_python_venv_active(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check if Python venv is accessible."""
        venv_paths = [
            self.project_root / ".venv" / "bin" / "python",
            self.project_root / ".venv" / "bin" / "python3",
            self.project_root / "venv" / "bin" / "python",
        ]

        for path in venv_paths:
            if path.exists():
                # Try to run it
                try:
                    result = subprocess.run(
                        [str(path), "-c", "import sys; print(sys.version)"],
                        capture_output=True,
                        timeout=5
                    )
                    if result.returncode == 0:
                        return True, f"venv at {path}", {"path": str(path)}
                except Exception as e:
                    print(f"[WARN] Python venv check failed for {path}: {e}")

        # Check system python as fallback
        try:
            result = subprocess.run(
                ["python3", "-c", "import sys; print(sys.version)"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                return True, "Using system Python (no venv)", {"path": "python3"}
        except Exception as e:
            print(f"[WARN] System Python check failed: {e}")

        return False, "No Python interpreter found", None

    def _check_memory_module_importable(self) -> Tuple[bool, str, Optional[Dict]]:
        """Verify memory module can be imported."""
        try:
            # Try importing key modules
            from memory import persistent_hook_structure
            return True, "memory module imports successfully", None
        except ImportError as e:
            return False, f"Import failed: {str(e)[:50]}", None

    def _check_section_0_works(self) -> Tuple[bool, str, Optional[Dict]]:
        """Test SAFETY section (Section 0) generation."""
        try:
            from memory.persistent_hook_structure import generate_section_0, InjectionConfig
            config = InjectionConfig()
            result = generate_section_0("deploy to production", config)

            if result and ("SAFETY" in result.upper() or "RISK" in result.upper()):
                return True, "Section 0 generates safety/risk classification", {
                    "output_length": len(result)
                }
            elif result:
                return True, "Section 0 generates output (format may vary)", {
                    "output_length": len(result)
                }
            else:
                return False, "Section 0 returned empty content", None
        except ImportError as e:
            return False, f"Could not import: {str(e)[:40]}", None
        except Exception as e:
            return False, f"Section 0 failed: {str(e)[:50]}", None

    def _check_section_6_works(self) -> Tuple[bool, str, Optional[Dict]]:
        """Test HOLISTIC_CONTEXT section (Section 6) - Synaptic's voice."""
        try:
            from memory.persistent_hook_structure import generate_section_6, InjectionConfig
            config = InjectionConfig()
            # Section 6 signature: (prompt, session_id=None, config=None)
            result = generate_section_6("test Synaptic voice", None, config)

            if result and "HOLISTIC" in result.upper():
                return True, "Section 6 generates holistic context communications", {
                    "output_length": len(result)
                }
            elif result:
                return True, "Section 6 generates output (Synaptic responding)", {
                    "output_length": len(result)
                }
            else:
                return False, "Section 6 returned empty content", None
        except ImportError as e:
            return False, f"Could not import: {str(e)[:40]}", None
        except AttributeError:
            return False, "generate_section_6 not defined (may need update)", None
        except Exception as e:
            return False, f"Section 6 failed: {str(e)[:50]}", None

    def _check_full_injection_works(self) -> Tuple[bool, str, Optional[Dict]]:
        """Test complete injection pipeline."""
        try:
            from memory.persistent_hook_structure import generate_context_injection
            # Note: parameter is 'mode' not 'volume', and result is an InjectionResult object
            result = generate_context_injection("test full injection", mode="hybrid")

            # Result is an InjectionResult object with .content attribute
            content = result.content if hasattr(result, 'content') else str(result)

            if content and len(content) > 100:
                return True, f"Full injection generates {len(content)} chars", {
                    "output_length": len(content)
                }
            elif content:
                return True, "Full injection works (short output)", {
                    "output_length": len(content)
                }
            else:
                return False, "Full injection returned empty content", None
        except ImportError as e:
            return False, f"Could not import: {str(e)[:40]}", None
        except Exception as e:
            return False, f"Full injection failed: {str(e)[:50]}", None

    def _check_auto_memory_script_exists(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check auto-memory-query.sh exists."""
        script_path = self.scripts_dir / "auto-memory-query.sh"
        if script_path.exists():
            return True, f"Script found at {script_path}", {"path": str(script_path)}

        # Check alternative locations
        alt_paths = [
            self.project_root / "auto-memory-query.sh",
            Path.home() / ".context-dna" / "auto-memory-query.sh",
        ]
        for path in alt_paths:
            if path.exists():
                return True, f"Script found at {path}", {"path": str(path)}

        return False, "auto-memory-query.sh not found", {
            "searched": [str(self.scripts_dir / "auto-memory-query.sh")]
        }

    def _check_auto_memory_script_executable(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check auto-memory-query.sh is executable."""
        script_path = self.scripts_dir / "auto-memory-query.sh"
        if not script_path.exists():
            return False, "Script does not exist", None

        if os.access(script_path, os.X_OK):
            return True, "Script is executable", {"path": str(script_path)}
        else:
            return False, "Script is not executable (run chmod +x)", {
                "path": str(script_path),
                "fix": f"chmod +x {script_path}"
            }


# CLI interface
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Verify webhook installation")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--fix", action="store_true", help="Attempt to fix issues")
    parser.add_argument("--project-root", type=Path, help="Project root directory")

    args = parser.parse_args()

    verifier = WebhookVerifier(project_root=args.project_root)

    if args.fix:
        print("Attempting to fix issues...")
        fixes = verifier.fix_issues()
        if fixes:
            print("Applied fixes:")
            for fix in fixes:
                print(f"  ✓ {fix}")
        else:
            print("No fixes needed or no fixes available")
        print()

    report = verifier.verify_installation()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary())

    # Exit with appropriate code
    sys.exit(0 if report.passed else 1)
