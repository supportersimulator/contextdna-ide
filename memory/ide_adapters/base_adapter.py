"""
Base IDE Adapter for Context DNA Webhook Installation

Abstract base class for IDE-specific webhook configuration.

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Optional, List
from dataclasses import dataclass


@dataclass
class VerificationResult:
    """Result of verifying IDE hook installation."""
    success: bool
    message: str
    details: Optional[Dict] = None


@dataclass
class InstallResult:
    """Result of installing IDE hooks."""
    success: bool
    message: str
    files_created: List[str] = None
    files_modified: List[str] = None
    error: Optional[str] = None

    def __post_init__(self):
        if self.files_created is None:
            self.files_created = []
        if self.files_modified is None:
            self.files_modified = []


class IDEAdapter(ABC):
    """
    Abstract base for IDE-specific webhook configuration.

    Each IDE adapter knows how to:
    1. Detect if the IDE is installed
    2. Install webhook hooks for that IDE
    3. Verify hooks are correctly configured
    4. Get the injection method (how injections reach the IDE)
    """

    def __init__(self, project_root: Optional[Path] = None):
        """Initialize the adapter with optional project root."""
        self.project_root = project_root or Path.cwd()
        self.memory_dir = self._find_memory_dir()
        self.scripts_dir = self._find_scripts_dir()

    def _find_memory_dir(self) -> Path:
        """Find the memory directory."""
        candidates = [
            self.project_root / "memory",
            Path(__file__).parent.parent,
        ]
        for path in candidates:
            if path.exists():
                return path
        return self.project_root / "memory"

    def _find_scripts_dir(self) -> Path:
        """Find the scripts directory."""
        candidates = [
            self.project_root / "scripts",
            Path(__file__).parent.parent.parent / "scripts",
        ]
        for path in candidates:
            if path.exists():
                return path
        return self.project_root / "scripts"

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the IDE name."""
        pass

    @property
    @abstractmethod
    def config_path(self) -> Path:
        """Return path to the IDE's configuration file."""
        pass

    @abstractmethod
    def is_installed(self) -> bool:
        """Check if this IDE is installed/configured on the system."""
        pass

    @abstractmethod
    def install_hooks(self) -> InstallResult:
        """Install webhook hooks for this IDE."""
        pass

    @abstractmethod
    def verify_hooks(self) -> VerificationResult:
        """Verify hooks are correctly installed."""
        pass

    @abstractmethod
    def get_injection_method(self) -> str:
        """Return how this IDE receives injections."""
        pass

    def get_hook_script_path(self) -> Path:
        """Return the path to the hook script."""
        return self.scripts_dir / "auto-memory-query.sh"

    def is_hook_script_available(self) -> bool:
        """Check if the hook script exists."""
        return self.get_hook_script_path().exists()

    def get_status(self) -> Dict:
        """Get complete status for this IDE adapter."""
        is_installed = self.is_installed()
        verification = self.verify_hooks() if is_installed else None

        return {
            "ide": self.name,
            "installed": is_installed,
            "config_path": str(self.config_path),
            "hooks_verified": verification.success if verification else False,
            "verification_message": verification.message if verification else "IDE not installed",
            "injection_method": self.get_injection_method() if is_installed else None,
        }
