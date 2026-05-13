"""
Unified Installer for Context DNA Multi-IDE Support

Detects installed IDEs and configures webhooks for all of them.

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

import sys
import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class IDEInstallResult:
    """Result of installing hooks for a single IDE."""
    ide_name: str
    success: bool
    message: str
    files_created: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class InstallReport:
    """Complete installation report."""
    ides_detected: List[str]
    results: Dict[str, IDEInstallResult]
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def all_success(self) -> bool:
        return all(r.success for r in self.results.values())

    @property
    def any_success(self) -> bool:
        return any(r.success for r in self.results.values())

    def to_dict(self) -> Dict:
        return {
            "ides_detected": self.ides_detected,
            "all_success": self.all_success,
            "any_success": self.any_success,
            "timestamp": self.timestamp.isoformat(),
            "results": {
                ide: {
                    "success": r.success,
                    "message": r.message,
                    "files_created": r.files_created,
                    "files_modified": r.files_modified,
                    "error": r.error
                }
                for ide, r in self.results.items()
            }
        }

    def summary(self) -> str:
        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            "║  MULTI-IDE INSTALLATION REPORT                               ║",
            "╠══════════════════════════════════════════════════════════════╣",
            f"   Detected IDEs: {', '.join(self.ides_detected) if self.ides_detected else 'None'}",
            "──────────────────────────────────────────────────────────────"
        ]

        for ide, result in self.results.items():
            icon = "✅" if result.success else "❌"
            lines.append(f"   {icon} {ide}: {result.message}")
            if result.files_created:
                lines.append(f"      Created: {', '.join(result.files_created)}")
            if result.files_modified:
                lines.append(f"      Modified: {', '.join(result.files_modified)}")
            if result.error:
                lines.append(f"      Error: {result.error}")

        success_count = sum(1 for r in self.results.values() if r.success)
        total_count = len(self.results)

        lines.append("╠══════════════════════════════════════════════════════════════╣")
        lines.append(f"   Results: {success_count}/{total_count} IDEs configured")

        if self.all_success:
            lines.append("   ✅ ALL IDE HOOKS INSTALLED SUCCESSFULLY")
        elif self.any_success:
            lines.append("   ⚠️ PARTIAL SUCCESS - Some IDEs configured")
        else:
            lines.append("   ❌ INSTALLATION FAILED - No IDEs configured")

        lines.append("╚══════════════════════════════════════════════════════════════╝")

        return "\n".join(lines)


class UnifiedInstaller:
    """
    Detect installed IDEs and configure webhooks for all.

    Supported IDEs:
    - Claude Code (full hook support via UserPromptSubmit)
    - Cursor (partial, via .cursorrules)
    - VS Code (git hooks only)
    - Antigravity (web-based injection via API)
    """

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = project_root or Path.cwd()
        self._adapters = None

    def _load_adapters(self):
        """Lazy-load IDE adapters."""
        if self._adapters is not None:
            return

        self._adapters = {}

        try:
            from memory.ide_adapters.claude_code import ClaudeCodeAdapter
            self._adapters["claude_code"] = ClaudeCodeAdapter(self.project_root)
        except ImportError as e:
            print(f"[WARN] Claude Code adapter not available: {e}")

        try:
            from memory.ide_adapters.cursor import CursorAdapter
            self._adapters["cursor"] = CursorAdapter(self.project_root)
        except ImportError as e:
            print(f"[WARN] Cursor adapter not available: {e}")

        try:
            from memory.ide_adapters.vscode import VSCodeAdapter
            self._adapters["vscode"] = VSCodeAdapter(self.project_root)
        except ImportError as e:
            print(f"[WARN] VS Code adapter not available: {e}")

        try:
            from memory.ide_adapters.antigravity import AntigravityAdapter
            self._adapters["antigravity"] = AntigravityAdapter(self.project_root)
        except ImportError as e:
            print(f"[WARN] Antigravity adapter not available: {e}")

    def detect_installed_ides(self) -> List[str]:
        """Detect which IDEs are installed/configured on this machine."""
        self._load_adapters()
        installed = []

        for name, adapter in self._adapters.items():
            try:
                if adapter.is_installed():
                    installed.append(name)
            except Exception as e:
                print(f"[WARN] IDE detection check failed for {name}: {e}")

        return installed

    def install_all(self, only_detected: bool = True) -> InstallReport:
        """
        Install webhooks for all detected IDEs.

        Args:
            only_detected: If True, only install for detected IDEs.
                          If False, attempt install for all known IDEs.
        """
        self._load_adapters()

        if only_detected:
            target_ides = self.detect_installed_ides()
        else:
            target_ides = list(self._adapters.keys())

        results = {}

        for ide_name in target_ides:
            adapter = self._adapters.get(ide_name)
            if not adapter:
                results[ide_name] = IDEInstallResult(
                    ide_name=ide_name,
                    success=False,
                    message="Adapter not found",
                    error="No adapter available for this IDE"
                )
                continue

            try:
                result = adapter.install_hooks()
                results[ide_name] = IDEInstallResult(
                    ide_name=ide_name,
                    success=result.success,
                    message=result.message,
                    files_created=result.files_created,
                    files_modified=result.files_modified,
                    error=result.error
                )
            except Exception as e:
                results[ide_name] = IDEInstallResult(
                    ide_name=ide_name,
                    success=False,
                    message="Installation failed",
                    error=str(e)[:100]
                )

        return InstallReport(
            ides_detected=self.detect_installed_ides(),
            results=results
        )

    def install_for_ide(self, ide_name: str) -> IDEInstallResult:
        """Install hooks for a specific IDE."""
        self._load_adapters()

        adapter = self._adapters.get(ide_name)
        if not adapter:
            return IDEInstallResult(
                ide_name=ide_name,
                success=False,
                message="Unknown IDE",
                error=f"No adapter for '{ide_name}'"
            )

        try:
            result = adapter.install_hooks()
            return IDEInstallResult(
                ide_name=ide_name,
                success=result.success,
                message=result.message,
                files_created=result.files_created,
                files_modified=result.files_modified,
                error=result.error
            )
        except Exception as e:
            return IDEInstallResult(
                ide_name=ide_name,
                success=False,
                message="Installation failed",
                error=str(e)[:100]
            )

    def verify_all(self) -> Dict[str, Any]:
        """Verify hooks for all detected IDEs."""
        self._load_adapters()
        results = {}

        for ide_name in self.detect_installed_ides():
            adapter = self._adapters.get(ide_name)
            if adapter:
                try:
                    verification = adapter.verify_hooks()
                    results[ide_name] = {
                        "verified": verification.success,
                        "message": verification.message,
                        "details": verification.details
                    }
                except Exception as e:
                    results[ide_name] = {
                        "verified": False,
                        "message": "Verification failed",
                        "error": str(e)
                    }

        return results

    def get_all_status(self) -> Dict[str, Any]:
        """Get complete status for all IDE adapters."""
        self._load_adapters()
        status = {}

        for name, adapter in self._adapters.items():
            try:
                status[name] = adapter.get_status()
            except Exception as e:
                status[name] = {
                    "ide": name,
                    "installed": False,
                    "error": str(e)
                }

        return status


# Module-level convenience functions

_installer = None


def get_installer(project_root: Optional[Path] = None) -> UnifiedInstaller:
    """Get or create the global UnifiedInstaller instance."""
    global _installer
    if _installer is None or (project_root and _installer.project_root != project_root):
        _installer = UnifiedInstaller(project_root)
    return _installer


def detect_ides() -> List[str]:
    """Detect installed IDEs."""
    return get_installer().detect_installed_ides()


def install_all_ides() -> InstallReport:
    """Install hooks for all detected IDEs."""
    return get_installer().install_all()


def verify_all_ides() -> Dict[str, Any]:
    """Verify hooks for all detected IDEs."""
    return get_installer().verify_all()


# CLI interface
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Multi-IDE Context DNA installer"
    )
    parser.add_argument("--detect", action="store_true", help="Detect installed IDEs")
    parser.add_argument("--install", action="store_true", help="Install hooks for all detected IDEs")
    parser.add_argument("--install-ide", type=str, help="Install hooks for specific IDE")
    parser.add_argument("--verify", action="store_true", help="Verify all IDE hooks")
    parser.add_argument("--status", action="store_true", help="Show status of all IDEs")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--project-root", type=Path, help="Project root directory")

    args = parser.parse_args()

    installer = UnifiedInstaller(args.project_root)

    if args.detect:
        ides = installer.detect_installed_ides()
        if args.json:
            print(json.dumps({"ides": ides}))
        else:
            print(f"Detected IDEs: {', '.join(ides) if ides else 'None'}")

    elif args.install:
        report = installer.install_all()
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(report.summary())

    elif args.install_ide:
        result = installer.install_for_ide(args.install_ide)
        if args.json:
            print(json.dumps({
                "ide": result.ide_name,
                "success": result.success,
                "message": result.message,
                "error": result.error
            }, indent=2))
        else:
            icon = "✅" if result.success else "❌"
            print(f"{icon} {result.ide_name}: {result.message}")
            if result.error:
                print(f"   Error: {result.error}")

    elif args.verify:
        results = installer.verify_all()
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print("IDE Hook Verification:")
            for ide, status in results.items():
                icon = "✅" if status["verified"] else "❌"
                print(f"  {icon} {ide}: {status['message']}")

    elif args.status:
        status = installer.get_all_status()
        if args.json:
            print(json.dumps(status, indent=2))
        else:
            print("IDE Status:")
            for ide, info in status.items():
                installed = info.get("installed", False)
                icon = "✅" if installed else "⚪"
                print(f"  {icon} {info.get('ide', ide)}: {'Installed' if installed else 'Not installed'}")
                if installed:
                    method = info.get("injection_method", "Unknown")
                    print(f"      Injection: {method}")

    else:
        # Default: detect and show status
        print("Context DNA Multi-IDE Installer")
        print("=" * 40)
        print()

        ides = installer.detect_installed_ides()
        print(f"Detected IDEs: {', '.join(ides) if ides else 'None'}")
        print()

        if ides:
            print("Use --install to configure all detected IDEs")
            print("Use --verify to check hook status")
        else:
            print("No supported IDEs detected.")
            print("Supported: claude_code, cursor, vscode, antigravity")
