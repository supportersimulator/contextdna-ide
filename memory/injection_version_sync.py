#!/usr/bin/env python3
"""
INJECTION VERSION SYNC - Auto-update Mechanism for Payload Changes

This module provides automatic synchronization when the payload structure changes.
All integrations check version before using cached payloads.

Key Features:
- Schema hash changes when payload structure changes
- Clients can poll /contextdna/unified-inject/version to check freshness
- Supports broadcast notification to connected clients
- File-based fallback for non-networked integrations

Architecture:
┌─────────────────────────────────────────────────────────────────┐
│                    Version Sync Flow                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. Code Change → Schema Hash Updates (auto on module load)     │
│                                                                 │
│  2. API Clients Poll Version Endpoint                           │
│     GET /contextdna/unified-inject/version                      │
│     → Returns: {"version": "1.0.0", "schema_hash": "abc123"}    │
│                                                                 │
│  3. Clients Compare Local Cache vs Server                       │
│     if local_hash != server_hash:                               │
│         invalidate_cache()                                      │
│         fetch_fresh_injection()                                 │
│                                                                 │
│  4. File-Based Sync (for non-networked)                         │
│     ~/.context-dna/injection_version.json                       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

Usage:
    # Check if cache is stale
    from memory.injection_version_sync import is_cache_stale, get_current_version

    if is_cache_stale(cached_hash):
        # Fetch fresh injection
        pass

    # Get current version info
    version_info = get_current_version()
    print(f"Version: {version_info['version']}, Hash: {version_info['schema_hash']}")
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any

# File-based version tracking
VERSION_FILE = Path.home() / ".context-dna" / "injection_version.json"
BROADCAST_FILE = Path.home() / ".context-dna" / "injection_broadcast.json"


def get_current_version() -> Dict[str, Any]:
    """Get current injection version and schema hash."""
    try:
        from memory.unified_injection import INJECTION_VERSION, PAYLOAD_SCHEMA_HASH, InjectionPreset

        return {
            "version": INJECTION_VERSION,
            "schema_hash": PAYLOAD_SCHEMA_HASH,
            "presets": [p.value for p in InjectionPreset],
            "timestamp": datetime.now().isoformat(),
        }
    except ImportError:
        return {
            "version": "unknown",
            "schema_hash": "unknown",
            "presets": [],
            "timestamp": datetime.now().isoformat(),
            "error": "unified_injection not available",
        }


def is_cache_stale(cached_hash: str) -> bool:
    """Check if a cached schema hash is stale."""
    current = get_current_version()
    return cached_hash != current.get("schema_hash")


def save_version_file():
    """Save current version to file for non-networked clients."""
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)

    version_info = get_current_version()
    VERSION_FILE.write_text(json.dumps(version_info, indent=2))

    return version_info


def load_version_file() -> Dict[str, Any]:
    """Load version from file (for clients that can't reach API)."""
    if VERSION_FILE.exists():
        try:
            return json.loads(VERSION_FILE.read_text())
        except Exception as e:
            print(f"[WARN] Version file read failed: {e}")

    return {"version": "unknown", "schema_hash": "unknown"}


def broadcast_version_change():
    """Broadcast version change to all listening integrations."""
    BROADCAST_FILE.parent.mkdir(parents=True, exist_ok=True)

    broadcast = {
        "event": "version_change",
        "version_info": get_current_version(),
        "timestamp": datetime.now().isoformat(),
        "message": "Payload structure updated - invalidate caches",
    }

    BROADCAST_FILE.write_text(json.dumps(broadcast, indent=2))

    # Also save version file
    save_version_file()

    return broadcast


def check_for_broadcast() -> Optional[Dict[str, Any]]:
    """Check if there's a pending broadcast (for polling clients)."""
    if not BROADCAST_FILE.exists():
        return None

    try:
        broadcast = json.loads(BROADCAST_FILE.read_text())

        # Check if broadcast is recent (within 5 minutes)
        broadcast_time = datetime.fromisoformat(broadcast.get("timestamp", "2000-01-01"))
        age_seconds = (datetime.now() - broadcast_time).total_seconds()

        if age_seconds < 300:  # 5 minutes
            return broadcast
        return None

    except Exception:
        return None


def clear_broadcast():
    """Clear the broadcast file after processing."""
    if BROADCAST_FILE.exists():
        BROADCAST_FILE.unlink()


# =============================================================================
# CLIENT SYNC HELPER
# =============================================================================

class InjectionCacheSync:
    """Helper for clients to manage injection cache synchronization."""

    def __init__(self, api_url: str = "http://localhost:8029"):
        self.api_url = api_url
        self.cached_hash: Optional[str] = None
        self.last_check: float = 0
        self.check_interval: float = 30  # seconds

    def should_refresh(self) -> bool:
        """Check if we should refresh injection (poll or file-based)."""
        now = time.time()

        # Don't check too frequently
        if now - self.last_check < self.check_interval:
            return False

        self.last_check = now

        # Try API first
        try:
            import requests
            response = requests.get(
                f"{self.api_url}/contextdna/unified-inject/version",
                timeout=2
            )
            if response.ok:
                server_info = response.json()
                server_hash = server_info.get("schema_hash")

                if self.cached_hash is None:
                    self.cached_hash = server_hash
                    return False  # First check, don't force refresh

                if server_hash != self.cached_hash:
                    self.cached_hash = server_hash
                    return True  # Hash changed, refresh needed

                return False

        except Exception as e:
            print(f"[WARN] Version sync API check failed: {e}")

        # Fallback to file-based check
        file_info = load_version_file()
        file_hash = file_info.get("schema_hash")

        if self.cached_hash is None:
            self.cached_hash = file_hash
            return False

        if file_hash != self.cached_hash:
            self.cached_hash = file_hash
            return True

        return False

    def get_injection(self, prompt: str, preset: str = "chat") -> str:
        """Get injection, refreshing if needed."""
        try:
            import requests

            response = requests.post(
                f"{self.api_url}/contextdna/unified-inject",
                json={"prompt": prompt, "preset": preset},
                timeout=5
            )

            if response.ok:
                result = response.json()
                self.cached_hash = result.get("metadata", {}).get("schema_hash")
                return result.get("payload", "")

        except Exception as e:
            print(f"[WARN] Injection payload fetch failed: {e}")

        return ""  # Graceful degradation


# =============================================================================
# CLI
# =============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Injection Version Sync")
    parser.add_argument("command", choices=["version", "save", "broadcast", "check"])

    args = parser.parse_args()

    if args.command == "version":
        info = get_current_version()
        print(json.dumps(info, indent=2))

    elif args.command == "save":
        info = save_version_file()
        print(f"Saved version to {VERSION_FILE}")
        print(json.dumps(info, indent=2))

    elif args.command == "broadcast":
        broadcast = broadcast_version_change()
        print("Broadcast sent:")
        print(json.dumps(broadcast, indent=2))

    elif args.command == "check":
        broadcast = check_for_broadcast()
        if broadcast:
            print("Pending broadcast:")
            print(json.dumps(broadcast, indent=2))
        else:
            print("No pending broadcast")


if __name__ == "__main__":
    main()
