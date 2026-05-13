"""
Eyes Agent - Visual Cortex / Code Observers

The Eyes watch the codebase - monitoring file changes, git activity,
and code structure to keep Synaptic aware of the development landscape.

Anatomical Label: Visual Cortex (Code Observers)
"""

from __future__ import annotations
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState, AgentMessage


class EyesAgent(Agent):
    """
    Eyes Agent - Code observation and file monitoring.

    Responsibilities:
    - Monitor file system changes
    - Track git activity
    - Detect code structure changes
    - Observe active file context
    """

    NAME = "eyes"
    CATEGORY = AgentCategory.SENSORY
    DESCRIPTION = "Code observation and file change monitoring"
    ANATOMICAL_LABEL = "Visual Cortex (Code Observers)"
    IS_VITAL = False

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._watch_paths: List[Path] = []
        self._last_scan: Dict[str, float] = {}  # path -> mtime

    def _on_start(self):
        """Start file observation."""
        # Add default watch paths
        project_root = Path.home() / "Documents" / "er-simulator-superrepo"
        if project_root.exists():
            self._watch_paths.append(project_root)

    def _on_stop(self):
        """Stop file observation."""
        self._watch_paths.clear()
        self._last_scan.clear()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check eyes health."""
        return {
            "healthy": True,
            "score": 1.0,
            "message": f"Watching {len(self._watch_paths)} paths",
            "metrics": {"watch_paths": len(self._watch_paths)}
        }

    def process(self, input_data: Any) -> Any:
        """Process observation requests."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "scan")
            if op == "scan":
                return self.scan_changes()
            elif op == "git_status":
                return self.get_git_status(input_data.get("path"))
            elif op == "watch":
                return self.add_watch_path(input_data.get("path"))
        return self.scan_changes()

    def scan_changes(self) -> Dict[str, Any]:
        """Scan for file changes in watched paths."""
        changes = {"modified": [], "new": [], "deleted": []}

        for watch_path in self._watch_paths:
            if not watch_path.exists():
                continue

            # Check key files
            key_patterns = ["*.py", "*.ts", "*.tsx", "*.js", "*.json", "*.md"]
            for pattern in key_patterns:
                for file_path in watch_path.rglob(pattern):
                    # Skip hidden and node_modules
                    if any(p.startswith('.') or p == 'node_modules' for p in file_path.parts):
                        continue

                    try:
                        mtime = file_path.stat().st_mtime
                        path_str = str(file_path)

                        if path_str in self._last_scan:
                            if mtime > self._last_scan[path_str]:
                                changes["modified"].append(path_str)
                        else:
                            changes["new"].append(path_str)

                        self._last_scan[path_str] = mtime
                    except (OSError, IOError):
                        continue

        self._last_active = datetime.utcnow()

        # Notify other agents if significant changes
        if changes["modified"] or changes["new"]:
            self.send_message("hippocampus", "file_changes", changes)

        return changes

    def get_git_status(self, path: str = None) -> Dict[str, Any]:
        """Get git status for a repository."""
        try:
            cwd = path or str(self._watch_paths[0] if self._watch_paths else Path.cwd())

            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return {"error": "Not a git repository"}

            changes = {
                "modified": [],
                "added": [],
                "deleted": [],
                "untracked": []
            }

            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                status = line[:2]
                file = line[3:]

                if 'M' in status:
                    changes["modified"].append(file)
                elif 'A' in status:
                    changes["added"].append(file)
                elif 'D' in status:
                    changes["deleted"].append(file)
                elif '?' in status:
                    changes["untracked"].append(file)

            return changes
        except Exception as e:
            return {"error": str(e)}

    def add_watch_path(self, path: str) -> bool:
        """Add a path to watch."""
        p = Path(path)
        if p.exists() and p not in self._watch_paths:
            self._watch_paths.append(p)
            return True
        return False

    def get_active_file(self) -> Optional[str]:
        """Get the currently active file from IDE context."""
        # Check environment variables for IDE file context
        for env_var in ["CURSOR_FILE_PATH", "VSCODE_FILE_PATH", "PYCHARM_FILE_PATH"]:
            path = os.environ.get(env_var)
            if path:
                return path
        return None
