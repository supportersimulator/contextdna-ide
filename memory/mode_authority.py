"""
Single source of truth for system mode (lite/heavy).

Precedence: env > Redis truth_pointer > manifest.yaml > 'lite'

ALL code that needs the current mode MUST call get_mode().
Do NOT read .heartbeat_config.json, Docker state, or SQLite directly.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = _REPO_ROOT / ".projectdna" / "manifest.yaml"
_REDIS_KEY = "contextdna:mode:truth_pointer"


def get_mode() -> str:
    """Return current system mode. Never raises."""
    # 1. Environment override (highest priority)
    env = os.environ.get("CONTEXTDNA_MODE")
    if env in ("lite", "heavy"):
        return env

    # 2. Redis truth pointer (set by mode_switch.py during transitions)
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        raw = r.get(_REDIS_KEY)
        if raw:
            data = json.loads(raw)
            mode = data.get("mode")
            if mode in ("lite", "heavy"):
                return mode
    except Exception:
        pass  # Redis down — fall through

    # 3. Manifest (static config)
    try:
        if _MANIFEST.exists():
            import yaml
            with open(_MANIFEST) as f:
                manifest = yaml.safe_load(f)
            mode = manifest.get("engine", {}).get("mode")
            if mode in ("lite", "heavy"):
                return mode
    except Exception:
        pass  # Manifest unreadable — fall through

    # 4. Default
    return "lite"


def is_lite() -> bool:
    return get_mode() == "lite"


def is_heavy() -> bool:
    return get_mode() == "heavy"
