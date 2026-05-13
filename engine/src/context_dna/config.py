"""
Context DNA Configuration Module

Handles user data directory (~/.context-dna/) and configuration management.
All user-specific data (databases, sessions, configs) lives in ~/.context-dna/
while the product code can be installed anywhere.

Directory Structure:
    ~/.context-dna/
    ├── config.yaml          # User configuration
    ├── .pattern_evolution.db # Main learning database
    ├── sessions/            # Session logs
    ├── backups/             # Automatic backups
    └── cache/               # Temporary cache files
"""

import os
import yaml
import json
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, field, asdict

# User data directory (created on first use)
USER_DATA_DIR = Path.home() / ".context-dna"

# Default configuration
DEFAULT_CONFIG = {
    "version": "0.1.0",
    "database": {
        "path": "~/.context-dna/.pattern_evolution.db",
        "backup_enabled": True,
        "backup_interval_hours": 24,
    },
    "hooks": {
        "user_prompt_submit": {"enabled": True},
        "post_tool_use": {"enabled": True},
        "session_end": {"enabled": True},
        "git_post_commit": {"enabled": True},
    },
    "ab_testing": {
        "enabled": True,
        "default_distribution": {
            "control": 50,
            "variant_a": 25,
            "variant_b": 15,
            "variant_c": 10,
        },
    },
    "wisdom_injection": {
        "enabled": True,
        "min_pattern_sessions": 5,
        "min_positive_rate": 0.5,
        "max_injections_per_prompt": 3,
    },
    "contextual_learning": {
        "enabled": True,
        "auto_discover_contexts": True,
        "auto_propose_patterns": True,
    },
    "deduplication": {
        "similarity_threshold": 0.7,
        "auto_merge": False,
    },
}


def get_user_data_dir() -> Path:
    """Get the user data directory (~/.context-dna/)."""
    return USER_DATA_DIR


def get_db_path() -> Path:
    """Get the path to the pattern evolution database."""
    return USER_DATA_DIR / ".pattern_evolution.db"


def get_config_path() -> Path:
    """Get the path to the user configuration file."""
    return USER_DATA_DIR / "config.yaml"


def get_sessions_dir() -> Path:
    """Get the sessions directory."""
    return USER_DATA_DIR / "sessions"


def get_backups_dir() -> Path:
    """Get the backups directory."""
    return USER_DATA_DIR / "backups"


def get_cache_dir() -> Path:
    """Get the cache directory."""
    return USER_DATA_DIR / "cache"


def ensure_user_data_dir() -> Path:
    """
    Ensure the user data directory exists with proper structure.

    Creates:
        ~/.context-dna/
        ~/.context-dna/sessions/
        ~/.context-dna/backups/
        ~/.context-dna/cache/

    Returns:
        Path to user data directory
    """
    # Create main directory
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    get_sessions_dir().mkdir(exist_ok=True)
    get_backups_dir().mkdir(exist_ok=True)
    get_cache_dir().mkdir(exist_ok=True)

    # Create default config if not exists
    config_path = get_config_path()
    if not config_path.exists():
        with open(config_path, "w") as f:
            yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, sort_keys=False)

    return USER_DATA_DIR


@dataclass
class Config:
    """Context DNA configuration wrapper."""

    version: str = "0.1.0"
    database: Dict[str, Any] = field(default_factory=lambda: DEFAULT_CONFIG["database"].copy())
    hooks: Dict[str, Any] = field(default_factory=lambda: DEFAULT_CONFIG["hooks"].copy())
    ab_testing: Dict[str, Any] = field(default_factory=lambda: DEFAULT_CONFIG["ab_testing"].copy())
    wisdom_injection: Dict[str, Any] = field(default_factory=lambda: DEFAULT_CONFIG["wisdom_injection"].copy())
    contextual_learning: Dict[str, Any] = field(default_factory=lambda: DEFAULT_CONFIG["contextual_learning"].copy())
    deduplication: Dict[str, Any] = field(default_factory=lambda: DEFAULT_CONFIG["deduplication"].copy())

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from user data directory."""
        ensure_user_data_dir()
        config_path = get_config_path()

        if config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        return cls()

    def save(self) -> None:
        """Save configuration to user data directory."""
        ensure_user_data_dir()
        config_path = get_config_path()

        with open(config_path, "w") as f:
            yaml.dump(asdict(self), f, default_flow_style=False, sort_keys=False)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value by dot-notation key."""
        parts = key.split(".")
        value = asdict(self)
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value

    def set(self, key: str, value: Any) -> None:
        """Set a configuration value by dot-notation key and save."""
        parts = key.split(".")
        data = asdict(self)
        current = data
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

        # Update self and save
        for k, v in data.items():
            if hasattr(self, k):
                setattr(self, k, v)
        self.save()


# Singleton config instance
_config_instance: Optional[Config] = None


def get_config() -> Config:
    """Get the singleton configuration instance."""
    global _config_instance
    if _config_instance is None:
        _config_instance = Config.load()
    return _config_instance


def init_user_data() -> Dict[str, Any]:
    """
    Initialize user data directory and return status.

    This is the main entry point for first-time setup.

    Returns:
        Dict with:
            - created: bool - whether new directory was created
            - path: str - path to user data directory
            - config_path: str - path to config file
            - db_path: str - path to database
    """
    already_exists = USER_DATA_DIR.exists()
    path = ensure_user_data_dir()

    return {
        "created": not already_exists,
        "path": str(path),
        "config_path": str(get_config_path()),
        "db_path": str(get_db_path()),
        "sessions_dir": str(get_sessions_dir()),
        "backups_dir": str(get_backups_dir()),
    }


if __name__ == "__main__":
    # Quick test
    result = init_user_data()
    print(f"User data directory: {result['path']}")
    print(f"Created: {result['created']}")

    config = get_config()
    print(f"Config version: {config.version}")
    print(f"AB testing enabled: {config.get('ab_testing.enabled')}")
