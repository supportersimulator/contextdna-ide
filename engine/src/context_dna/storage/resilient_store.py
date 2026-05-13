"""
Resilient Profile Store - Never Fully Broken, Always Adaptive.

This is the "graceful degradation" layer that ensures Context DNA
ALWAYS works, regardless of PostgreSQL availability.

Hierarchy:
1. PostgreSQL (source of truth, distributed)
2. SQLite cache (local fast access)
3. Fresh scan (ultimate fallback)

Features:
- Automatic failover
- Cache synchronization
- Project context switching
- Cross-platform support (Mac, Windows, Linux)

Usage:
    from context_dna.storage.resilient_store import ResilientProfileStore

    store = ResilientProfileStore()
    profile = await store.get_profile()  # Always returns something!
"""

import os
import json
import asyncio
import platform
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any
from dataclasses import dataclass


# =============================================================================
# PLATFORM DETECTION
# =============================================================================

@dataclass
class PlatformInfo:
    """Cross-platform system information."""
    os: str           # 'darwin', 'windows', 'linux'
    arch: str         # 'arm64', 'x86_64', 'amd64'
    is_apple_silicon: bool
    is_windows: bool
    home_dir: Path
    config_dir: Path
    cache_dir: Path

    @classmethod
    def detect(cls) -> "PlatformInfo":
        """Detect current platform."""
        os_name = platform.system().lower()
        arch = platform.machine().lower()

        # Normalize architecture names
        if arch in ("amd64", "x64"):
            arch = "x86_64"

        is_apple_silicon = os_name == "darwin" and arch == "arm64"
        is_windows = os_name == "windows"

        home = Path.home()

        # Platform-specific config directories
        if is_windows:
            config_dir = Path(os.environ.get("APPDATA", "")) / "ContextDNA"
            cache_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "ContextDNA" / "Cache"
        elif os_name == "darwin":
            config_dir = home / ".context-dna"
            cache_dir = home / "Library" / "Caches" / "ContextDNA"
        else:  # Linux
            config_dir = home / ".config" / "context-dna"
            cache_dir = home / ".cache" / "context-dna"

        return cls(
            os=os_name,
            arch=arch,
            is_apple_silicon=is_apple_silicon,
            is_windows=is_windows,
            home_dir=home,
            config_dir=config_dir,
            cache_dir=cache_dir,
        )


# =============================================================================
# PROJECT CONTEXT MANAGER
# =============================================================================

@dataclass
class ProjectContext:
    """
    Tracks which project the user is currently working in.

    This enables webhook injection to provide project-specific context.
    """
    project_id: str
    project_name: str
    project_path: Path
    project_type: str  # 'backend', 'frontend', 'service', etc.
    last_active: datetime
    metadata: Dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "project_path": str(self.project_path),
            "project_type": self.project_type,
            "last_active": self.last_active.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectContext":
        return cls(
            project_id=data["project_id"],
            project_name=data["project_name"],
            project_path=Path(data["project_path"]),
            project_type=data["project_type"],
            last_active=datetime.fromisoformat(data["last_active"]),
            metadata=data.get("metadata", {}),
        )


class ProjectContextManager:
    """
    Manages project context switching for webhook injection.

    This is what makes Context DNA adapt to which project you're working in,
    providing relevant context based on your current focus.
    """

    def __init__(self, config_dir: Path = None):
        platform_info = PlatformInfo.detect()
        self.config_dir = config_dir or platform_info.config_dir
        self.context_file = self.config_dir / "current_context.json"
        self.history_file = self.config_dir / "context_history.json"
        self._current: Optional[ProjectContext] = None

    def set_current_project(self, context: ProjectContext):
        """Set the current working project."""
        self._current = context
        self._save_context()
        self._add_to_history(context)

    def get_current_project(self) -> Optional[ProjectContext]:
        """Get the current working project."""
        if self._current:
            return self._current
        return self._load_context()

    def detect_from_path(self, path: Path) -> Optional[ProjectContext]:
        """
        Detect project context from a file path.

        This is called by webhook injection to determine which
        project the user is currently working in.
        """
        path = Path(path).resolve()

        # Walk up to find project root indicators
        for parent in [path] + list(path.parents):
            # Check for project markers
            markers = [
                "package.json", "pyproject.toml", "Cargo.toml",
                "go.mod", "pom.xml", "build.gradle", ".git"
            ]

            for marker in markers:
                if (parent / marker).exists():
                    # Found a project root
                    return ProjectContext(
                        project_id=self._hash_path(parent),
                        project_name=parent.name,
                        project_path=parent,
                        project_type=self._detect_type(parent),
                        last_active=datetime.now(),
                        metadata=self._gather_metadata(parent),
                    )

        return None

    def _detect_type(self, path: Path) -> str:
        """Detect project type from markers."""
        if (path / "package.json").exists():
            pkg = json.loads((path / "package.json").read_text())
            if "next" in pkg.get("dependencies", {}):
                return "nextjs"
            if "react" in pkg.get("dependencies", {}):
                return "react"
            if "express" in pkg.get("dependencies", {}):
                return "node_backend"
            return "node"

        if (path / "pyproject.toml").exists() or (path / "setup.py").exists():
            if (path / "manage.py").exists():
                return "django"
            if (path / "app.py").exists() or (path / "main.py").exists():
                return "python_backend"
            return "python"

        if (path / "Cargo.toml").exists():
            return "rust"

        if (path / "go.mod").exists():
            return "go"

        return "unknown"

    def _gather_metadata(self, path: Path) -> dict:
        """Gather project metadata for context."""
        metadata = {}

        # Python project
        if (path / "pyproject.toml").exists():
            try:
                import tomllib
                with open(path / "pyproject.toml", "rb") as f:
                    toml = tomllib.load(f)
                    metadata["python_project"] = toml.get("project", {}).get("name")
            except Exception as e:
                print(f"[WARN] Failed to parse pyproject.toml: {e}")

        # Node project
        if (path / "package.json").exists():
            try:
                pkg = json.loads((path / "package.json").read_text())
                metadata["node_project"] = pkg.get("name")
                metadata["node_version"] = pkg.get("version")
            except Exception as e:
                print(f"[WARN] Failed to parse package.json: {e}")

        return metadata

    def _hash_path(self, path: Path) -> str:
        """Create stable hash for a path."""
        import hashlib
        return hashlib.sha256(str(path).encode()).hexdigest()[:12]

    def _save_context(self):
        """Save current context to disk."""
        if not self._current:
            return

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.context_file.write_text(json.dumps(self._current.to_dict(), indent=2))

    def _load_context(self) -> Optional[ProjectContext]:
        """Load context from disk."""
        if self.context_file.exists():
            try:
                data = json.loads(self.context_file.read_text())
                self._current = ProjectContext.from_dict(data)
                return self._current
            except Exception as e:
                print(f"[WARN] Failed to load project context from {self.context_file}: {e}")
        return None

    def _add_to_history(self, context: ProjectContext):
        """Add context switch to history."""
        history = []
        if self.history_file.exists():
            try:
                history = json.loads(self.history_file.read_text())
            except Exception as e:
                print(f"[WARN] Failed to load context history: {e}")

        # Add new entry
        history.append({
            **context.to_dict(),
            "switched_at": datetime.now().isoformat(),
        })

        # Keep last 100 entries
        history = history[-100:]

        self.history_file.write_text(json.dumps(history, indent=2))


# =============================================================================
# RESILIENT PROFILE STORE
# =============================================================================

class ResilientProfileStore:
    """
    Never-fail profile storage with automatic failover.

    Provides:
    1. PostgreSQL (distributed, version history)
    2. SQLite (local cache, fast access)
    3. Fresh scan (ultimate fallback)
    """

    def __init__(self, root_path: Path = None):
        self.platform = PlatformInfo.detect()
        self.root = root_path or Path.cwd()

        # Storage backends
        self._postgres = None
        self._sqlite_path = self.platform.cache_dir / "profile_cache.db"
        self._json_fallback = self.platform.config_dir / "hierarchy_profile.json"

        # Project context
        self.context_manager = ProjectContextManager(self.platform.config_dir)

        # Sync state
        self._last_postgres_sync: Optional[datetime] = None
        self._postgres_available = None  # None = untested

    async def get_profile(self) -> Optional[dict]:
        """
        Get profile - ALWAYS returns something if possible.

        Tries in order:
        1. PostgreSQL (source of truth)
        2. SQLite cache (fast local)
        3. JSON file (simple fallback)
        4. Fresh scan (ultimate fallback)
        """
        # Try PostgreSQL
        profile = await self._try_postgres()
        if profile:
            await self._update_cache(profile)
            return profile

        # Try SQLite cache
        profile = self._try_sqlite()
        if profile:
            profile["_source"] = "cache"
            profile["_warning"] = "Using cached profile - may be stale"
            return profile

        # Try JSON fallback
        profile = self._try_json()
        if profile:
            profile["_source"] = "json_fallback"
            profile["_warning"] = "Using JSON fallback - limited features"
            return profile

        # Ultimate fallback: fresh scan
        return await self._fresh_scan()

    async def save_profile(self, profile_data: dict, **kwargs) -> bool:
        """
        Save profile with redundancy.

        Always saves to local (SQLite/JSON) first, then tries PostgreSQL.
        """
        # Always save locally first (guaranteed to work)
        self._save_json(profile_data)
        self._save_sqlite(profile_data)

        # Try PostgreSQL
        try:
            await self._save_postgres(profile_data, **kwargs)
            return True
        except Exception as e:
            print(f"PostgreSQL save failed (local cache used): {e}")
            return True  # Local save succeeded

    def get_current_project_context(self) -> Optional[ProjectContext]:
        """Get current project context for webhook injection."""
        return self.context_manager.get_current_project()

    def detect_project_from_file(self, file_path: str) -> Optional[ProjectContext]:
        """Detect and set project context from a file path."""
        context = self.context_manager.detect_from_path(Path(file_path))
        if context:
            self.context_manager.set_current_project(context)
        return context

    # -------------------------------------------------------------------------
    # PostgreSQL Operations
    # -------------------------------------------------------------------------

    async def _try_postgres(self) -> Optional[dict]:
        """Try to get profile from PostgreSQL."""
        try:
            from .profile_store import ProfileStore
            store = ProfileStore()
            await store.connect()

            if store._using_fallback:
                # ProfileStore fell back to SQLite, so no PostgreSQL
                self._postgres_available = False
                return None

            self._postgres_available = True
            profile = await store.get_active_profile()
            await store.close()

            if profile:
                self._last_postgres_sync = datetime.now()
                return profile.get("data") if isinstance(profile, dict) else profile

        except Exception as e:
            self._postgres_available = False
            print(f"PostgreSQL unavailable: {e}")

        return None

    async def _save_postgres(self, profile_data: dict, **kwargs):
        """Save to PostgreSQL."""
        from .profile_store import ProfileStore
        store = ProfileStore()
        await store.connect()
        await store.save_profile(profile_data, **kwargs)
        await store.close()

    # -------------------------------------------------------------------------
    # SQLite Cache Operations
    # -------------------------------------------------------------------------

    def _try_sqlite(self) -> Optional[dict]:
        """Try to get profile from SQLite cache."""
        import sqlite3

        if not self._sqlite_path.exists():
            return None

        try:
            conn = sqlite3.connect(str(self._sqlite_path))
            cursor = conn.cursor()

            cursor.execute("""
                SELECT profile_data, cached_at FROM profile_cache
                ORDER BY cached_at DESC LIMIT 1
            """)

            row = cursor.fetchone()
            conn.close()

            if row:
                return json.loads(row[0])

        except Exception as e:
            print(f"SQLite cache read failed: {e}")

        return None

    def _save_sqlite(self, profile_data: dict):
        """Save to SQLite cache."""
        import sqlite3

        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(self._sqlite_path))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profile_cache (
                id INTEGER PRIMARY KEY,
                profile_data TEXT NOT NULL,
                cached_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute(
            "INSERT INTO profile_cache (profile_data) VALUES (?)",
            (json.dumps(profile_data),)
        )

        # Keep only last 5 cached profiles
        cursor.execute("""
            DELETE FROM profile_cache WHERE id NOT IN (
                SELECT id FROM profile_cache ORDER BY cached_at DESC LIMIT 5
            )
        """)

        conn.commit()
        conn.close()

    async def _update_cache(self, profile: dict):
        """Update local cache from PostgreSQL."""
        self._save_sqlite(profile)
        self._save_json(profile)

    # -------------------------------------------------------------------------
    # JSON Fallback Operations
    # -------------------------------------------------------------------------

    def _try_json(self) -> Optional[dict]:
        """Try to get profile from JSON file."""
        if self._json_fallback.exists():
            try:
                return json.loads(self._json_fallback.read_text())
            except Exception as e:
                print(f"[WARN] Failed to read JSON fallback {self._json_fallback}: {e}")
        return None

    def _save_json(self, profile_data: dict):
        """Save to JSON file."""
        self._json_fallback.parent.mkdir(parents=True, exist_ok=True)
        self._json_fallback.write_text(json.dumps(profile_data, indent=2))

    # -------------------------------------------------------------------------
    # Fresh Scan Fallback
    # -------------------------------------------------------------------------

    async def _fresh_scan(self) -> Optional[dict]:
        """Ultimate fallback: run fresh analysis."""
        try:
            from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer

            analyzer = HierarchyAnalyzer(self.root)
            profile = analyzer.analyze()

            if profile:
                result = profile.to_dict()
                result["_source"] = "fresh_scan"
                result["_warning"] = "Generated from fresh scan - not persisted"

                # Cache the result
                self._save_json(result)
                self._save_sqlite(result)

                return result

        except Exception as e:
            print(f"Fresh scan failed: {e}")

        return None

    # -------------------------------------------------------------------------
    # Status & Health
    # -------------------------------------------------------------------------

    async def health_check(self) -> dict:
        """Check health of all storage backends."""
        return {
            "platform": {
                "os": self.platform.os,
                "arch": self.platform.arch,
                "is_apple_silicon": self.platform.is_apple_silicon,
                "is_windows": self.platform.is_windows,
            },
            "postgres": {
                "available": self._postgres_available,
                "last_sync": self._last_postgres_sync.isoformat() if self._last_postgres_sync else None,
            },
            "sqlite_cache": {
                "exists": self._sqlite_path.exists(),
                "path": str(self._sqlite_path),
            },
            "json_fallback": {
                "exists": self._json_fallback.exists(),
                "path": str(self._json_fallback),
            },
            "current_project": self.context_manager.get_current_project().to_dict()
                if self.context_manager.get_current_project() else None,
        }


# =============================================================================
# CROSS-PLATFORM COMPATIBILITY CHECK
# =============================================================================

def check_platform_compatibility() -> dict:
    """
    Check if the current platform is fully supported.

    Returns compatibility report with any issues or recommendations.
    """
    platform_info = PlatformInfo.detect()

    issues = []
    recommendations = []

    # Check Python version
    import sys
    py_version = sys.version_info
    if py_version < (3, 9):
        issues.append(f"Python {py_version.major}.{py_version.minor} may have limited support")
        recommendations.append("Upgrade to Python 3.9+")

    # Check config directory writability
    try:
        platform_info.config_dir.mkdir(parents=True, exist_ok=True)
        test_file = platform_info.config_dir / ".write_test"
        test_file.write_text("test")
        test_file.unlink()
    except Exception as e:
        issues.append(f"Cannot write to config directory: {e}")
        recommendations.append(f"Check permissions for {platform_info.config_dir}")

    # Platform-specific checks
    if platform_info.is_windows:
        # Windows-specific checks
        if not os.environ.get("APPDATA"):
            issues.append("APPDATA environment variable not set")

    elif platform_info.os == "darwin":
        # macOS-specific checks
        if platform_info.is_apple_silicon:
            recommendations.append("MLX backend available for optimal performance")
        else:
            recommendations.append("Ollama backend recommended for Intel Macs")

    else:
        # Linux checks
        recommendations.append("Ollama backend recommended")

    return {
        "platform": platform_info.os,
        "arch": platform_info.arch,
        "python_version": f"{py_version.major}.{py_version.minor}.{py_version.micro}",
        "config_dir": str(platform_info.config_dir),
        "cache_dir": str(platform_info.cache_dir),
        "compatible": len(issues) == 0,
        "issues": issues,
        "recommendations": recommendations,
    }


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    async def main():
        if len(sys.argv) > 1:
            cmd = sys.argv[1]

            if cmd == "check":
                # Platform compatibility check
                result = check_platform_compatibility()
                print("\n=== Platform Compatibility Check ===")
                print(f"Platform: {result['platform']} ({result['arch']})")
                print(f"Python: {result['python_version']}")
                print(f"Config: {result['config_dir']}")
                print(f"Cache: {result['cache_dir']}")
                print()

                if result["compatible"]:
                    print("Compatible")
                else:
                    print("Issues:")
                    for issue in result["issues"]:
                        print(f"  - {issue}")

                if result["recommendations"]:
                    print("\nRecommendations:")
                    for rec in result["recommendations"]:
                        print(f"  + {rec}")

            elif cmd == "health":
                # Full health check
                store = ResilientProfileStore()
                health = await store.health_check()

                print("\n=== Storage Health Check ===")
                print(json.dumps(health, indent=2))

            elif cmd == "profile":
                # Get current profile
                store = ResilientProfileStore()
                profile = await store.get_profile()

                if profile:
                    source = profile.get("_source", "unknown")
                    warning = profile.get("_warning")

                    print(f"\n=== Profile (source: {source}) ===")
                    if warning:
                        print(f"WARNING: {warning}")
                    print(f"Keys: {list(profile.keys())}")
                else:
                    print("No profile found")

            else:
                print(f"Unknown command: {cmd}")
                print("Usage: python -m context_dna.storage.resilient_store [check|health|profile]")
        else:
            print("Usage: python -m context_dna.storage.resilient_store [check|health|profile]")

    asyncio.run(main())
