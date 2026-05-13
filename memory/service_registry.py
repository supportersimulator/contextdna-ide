#!/usr/bin/env python3
"""
Context DNA Service Registry — Single Source of Truth for service URLs.

Every Python module that needs a service URL MUST use this module:

    from memory.service_registry import get_service_url, get_service_ws_url

    url = get_service_url("helper_agent")     # "http://127.0.0.1:8080"
    ws  = get_service_ws_url("injection_ws")  # "ws://127.0.0.1:8080/ws/injections"

RULES:
    1. Never hardcode ports or hosts in Python code
    2. All service definitions live in scripts/contextdna-services.yaml
    3. Environment variables override YAML defaults (e.g., CONTEXTDNA_HELPER_AGENT_PORT=9090)
    4. This module is the ONLY gateway to service URLs

Architecture:
    Current: TCP ports on 127.0.0.1
    Future (Electron): Unix domain sockets via future_socket field
"""

import os
from pathlib import Path
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Lazy YAML loading (only on first access)
# ---------------------------------------------------------------------------
_manifest: Optional[Dict] = None
_MANIFEST_PATH = Path(__file__).parent.parent / "scripts" / "contextdna-services.yaml"


def _load_manifest() -> Dict:
    """Load the service manifest YAML. Cached after first load."""
    global _manifest
    if _manifest is not None:
        return _manifest

    try:
        import yaml
        with open(_MANIFEST_PATH, 'r') as f:
            _manifest = yaml.safe_load(f) or {}
    except ImportError:
        # PyYAML not installed — use simple parser
        _manifest = _parse_yaml_simple(_MANIFEST_PATH)
    except FileNotFoundError:
        _manifest = {"services": _FALLBACK_SERVICES}

    return _manifest


def _parse_yaml_simple(path: Path) -> Dict:
    """
    Minimal YAML parser for the service manifest.
    Handles only the flat key-value structure we need.
    Falls back to hardcoded defaults if parsing fails.
    """
    services = {}
    websockets = {}
    current_service = None
    current_section = None

    try:
        with open(path, 'r') as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue

                # Top-level sections
                if stripped == 'services:':
                    current_section = 'services'
                    continue
                elif stripped == 'websockets:':
                    current_section = 'websockets'
                    continue
                elif stripped == 'deprecated:':
                    current_section = 'deprecated'
                    continue

                if current_section == 'deprecated':
                    continue

                # Service name (2-space indent, ends with colon)
                indent = len(line) - len(line.lstrip())
                if indent == 2 and stripped.endswith(':') and not stripped.startswith('-'):
                    name = stripped.rstrip(':')
                    if current_section == 'services':
                        services[name] = {}
                        current_service = ('services', name)
                    elif current_section == 'websockets':
                        websockets[name] = {}
                        current_service = ('websockets', name)
                    continue

                # Property (4-space indent)
                if indent == 4 and ':' in stripped and current_service:
                    key, _, value = stripped.partition(':')
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")

                    # Type conversions
                    if value == 'null' or value == 'None':
                        value = None
                    elif value == 'true' or value == 'True':
                        value = True
                    elif value == 'false' or value == 'False':
                        value = False
                    elif value.isdigit():
                        value = int(value)

                    section_type, service_name = current_service
                    if section_type == 'services':
                        services[service_name][key] = value
                    elif section_type == 'websockets':
                        websockets[service_name][key] = value
    except Exception:
        services = _FALLBACK_SERVICES

    return {"services": services, "websockets": websockets}


# ---------------------------------------------------------------------------
# Hardcoded fallback (if YAML missing/corrupt)
# ---------------------------------------------------------------------------
_FALLBACK_SERVICES = {
    "helper_agent": {"host": "127.0.0.1", "port": 8080, "protocol": "http", "health_path": "/health"},
    "local_llm": {"host": "127.0.0.1", "port": 5044, "protocol": "http", "health_path": "/v1/models"},
    "memory_api": {"host": "127.0.0.1", "port": 3456, "protocol": "http", "health_path": "/health"},
    "redis": {"host": "127.0.0.1", "port": 6379, "protocol": "redis", "auth": False},
    "postgres": {"host": "127.0.0.1", "port": 5432, "protocol": "postgresql", "database": "context_dna"},
    "postgres_upstream": {"host": "127.0.0.1", "port": 15432, "protocol": "postgresql", "database": "acontext"},
    "admin_dashboard": {"host": "127.0.0.1", "port": 3000, "protocol": "http"},
    "django_backend": {"host": "127.0.0.1", "port": 8000, "protocol": "http"},
    "synaptic_chat": {"host": "127.0.0.1", "port": 8888, "protocol": "http", "health_path": "/health"},
    # ER Simulator voice pipeline (separate product)
    "ersim_livekit": {"host": "127.0.0.1", "port": 7880, "protocol": "http"},
    "ersim_stt": {"host": "127.0.0.1", "port": 8081, "protocol": "http"},
    "ersim_tts": {"host": "127.0.0.1", "port": 8082, "protocol": "http"},
    "ersim_llm_proxy": {"host": "127.0.0.1", "port": 8090, "protocol": "http"},
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_service_url(name: str) -> str:
    """
    Get the full URL for a named service.

    Args:
        name: Service name from contextdna-services.yaml
              (e.g., "helper_agent", "local_llm", "redis")

    Returns:
        Full URL string (e.g., "http://127.0.0.1:8080")

    Environment override:
        Set CONTEXTDNA_{NAME}_HOST and/or CONTEXTDNA_{NAME}_PORT
        e.g., CONTEXTDNA_HELPER_AGENT_PORT=9090
    """
    manifest = _load_manifest()
    services = manifest.get("services", {})
    service = services.get(name)

    if not service:
        raise ValueError(
            f"Unknown service '{name}'. Available: {list(services.keys())}"
        )

    # Environment overrides (uppercase name)
    env_prefix = f"CONTEXTDNA_{name.upper()}"
    host = os.environ.get(f"{env_prefix}_HOST", service.get("host", "127.0.0.1"))
    port = int(os.environ.get(f"{env_prefix}_PORT", service.get("port", 0)))
    protocol = service.get("protocol", "http")

    if protocol in ("http", "https"):
        return f"{protocol}://{host}:{port}"
    elif protocol == "redis":
        return f"redis://{host}:{port}"
    elif protocol == "postgresql":
        db = service.get("database", "")
        return f"postgresql://{host}:{port}/{db}"
    else:
        return f"{protocol}://{host}:{port}"


def get_service_host_port(name: str) -> tuple:
    """
    Get (host, port) tuple for a named service.

    Useful when you need host and port separately (e.g., Redis client).
    """
    manifest = _load_manifest()
    services = manifest.get("services", {})
    service = services.get(name)

    if not service:
        raise ValueError(f"Unknown service '{name}'")

    env_prefix = f"CONTEXTDNA_{name.upper()}"
    host = os.environ.get(f"{env_prefix}_HOST", service.get("host", "127.0.0.1"))
    port = int(os.environ.get(f"{env_prefix}_PORT", service.get("port", 0)))

    return (host, port)


def get_service_ws_url(name: str) -> str:
    """
    Get a WebSocket URL for a named WebSocket endpoint.

    Args:
        name: WebSocket name from contextdna-services.yaml
              (e.g., "injection_ws", "events_ws")

    Returns:
        Full WebSocket URL (e.g., "ws://127.0.0.1:8080/ws/injections")
    """
    manifest = _load_manifest()
    ws_config = manifest.get("websockets", {}).get(name)

    if not ws_config:
        raise ValueError(f"Unknown WebSocket '{name}'")

    service_name = ws_config.get("service", "helper_agent")
    path = ws_config.get("path", "/ws")

    base = get_service_url(service_name)
    # Convert http:// to ws://
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")

    return f"{ws_base}{path}"


def get_health_url(name: str) -> Optional[str]:
    """Get the health check URL for a service, or None if not applicable."""
    manifest = _load_manifest()
    service = manifest.get("services", {}).get(name)

    if not service or not service.get("health_path"):
        return None

    base = get_service_url(name)
    return f"{base}{service['health_path']}"


def list_services() -> Dict[str, Dict]:
    """List all registered services with their config."""
    manifest = _load_manifest()
    return manifest.get("services", {})


def check_service_health(name: str, timeout: float = 3.0) -> bool:
    """
    Quick health check for a service.

    Returns True if service responds, False otherwise.
    """
    import urllib.request
    import urllib.error

    url = get_health_url(name)
    if not url:
        return False

    try:
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        name = sys.argv[1]
        try:
            print(get_service_url(name))
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Context DNA Service Registry")
        print("=" * 50)
        for name, config in list_services().items():
            url = get_service_url(name)
            desc = config.get("description", "")
            health = get_health_url(name) or "N/A"
            print(f"  {name:20s} {url:35s} health={health}")
        print()
        print("Usage: python -m memory.service_registry <service_name>")
