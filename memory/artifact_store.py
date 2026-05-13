#!/usr/bin/env python3
"""
Artifact Store - SeaweedFS-Powered Infrastructure Artifact Storage

This module provides automatic storage and retrieval of infrastructure artifacts
(terraform, docker-compose, scripts, etc.) using Context DNA's SeaweedFS backend.

KEY CAPABILITIES:
1. Store procedure artifacts with automatic secret sanitization
2. Link artifacts to learning sessions (SOPs reference their files)
3. Retrieve artifacts when querying procedures
4. Pattern search across all stored artifacts with grep_artifacts()

SECURITY:
All artifacts are automatically sanitized before storage:
- EC2 instance IDs → ${INSTANCE_ID}
- API keys → ${API_KEY}
- IP addresses → ${IP_ADDRESS}
- AWS ARNs → ${ARN}
- Connection strings → ${CONNECTION_STRING}

Actual secrets are NEVER stored - only placeholders.

Usage:
    from memory.artifact_store import ArtifactStore

    store = ArtifactStore()

    # Store procedure with its artifacts
    disk_id = store.store_with_artifacts(
        session_id="abc123",
        artifacts={
            "main.tf": terraform_content,
            "deploy.sh": script_content
        },
        area="django-deployment"
    )

    # Retrieve artifacts for a procedure
    artifacts = store.get_procedure_artifacts(disk_id)

    # Search across all artifacts
    matches = store.search_artifacts("systemctl restart gunicorn")
"""

import os
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from acontext import AcontextClient
    ACONTEXT_AVAILABLE = True
except ImportError:
    ACONTEXT_AVAILABLE = False


# =============================================================================
# SECRET SANITIZATION PATTERNS
# =============================================================================

SECRET_PATTERNS = [
    # AWS EC2 Instance IDs (i-0a1b2c3d4e5f67890)
    (r'i-[0-9a-f]{8,17}', '${INSTANCE_ID}'),

    # AWS ARNs
    (r'arn:aws:[a-zA-Z0-9\-]+:[a-z0-9\-]*:\d*:[a-zA-Z0-9\-_/:.]+', '${ARN}'),

    # API Keys (various formats)
    (r'sk-[a-zA-Z0-9]{32,}', '${OPENAI_KEY}'),
    (r'sk-proj-[a-zA-Z0-9\-_]{32,}', '${OPENAI_KEY}'),
    (r'AKIA[0-9A-Z]{16}', '${AWS_ACCESS_KEY}'),
    (r'(?<![a-zA-Z0-9])[a-zA-Z0-9]{40}(?![a-zA-Z0-9])', '${AWS_SECRET_KEY}'),

    # IP Addresses (preserve localhost/0.0.0.0)
    (r'(?<![\d.])((?!127\.0\.0\.1)(?!0\.0\.0\.0)(?!localhost)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])', '${IP_ADDRESS}'),

    # Database Connection Strings
    (r'postgres://[^@]+@[^\s]+', '${DATABASE_URL}'),
    (r'mysql://[^@]+@[^\s]+', '${DATABASE_URL}'),
    (r'mongodb://[^@]+@[^\s]+', '${DATABASE_URL}'),
    (r'redis://[^@]+@[^\s]+', '${REDIS_URL}'),

    # Generic secrets in env vars
    (r'(PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL)["\']?\s*[:=]\s*["\']?[a-zA-Z0-9\-_]{16,}["\']?', '${REDACTED_SECRET}'),

    # Bearer tokens
    (r'Bearer\s+[a-zA-Z0-9\-_.]+', 'Bearer ${TOKEN}'),

    # SSH private keys
    (r'-----BEGIN [A-Z]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z]+ PRIVATE KEY-----', '${PRIVATE_KEY}'),
]


def sanitize_secrets(content: str) -> str:
    """
    Sanitize secrets from content before storage.

    Replaces sensitive values with placeholder tokens.
    Artifacts stored with placeholders are safe to share and search.

    Args:
        content: Raw content that may contain secrets

    Returns:
        Sanitized content with placeholders
    """
    sanitized = content

    for pattern, replacement in SECRET_PATTERNS:
        sanitized = re.sub(pattern, replacement, sanitized)

    return sanitized


def detect_secrets(content: str) -> list[dict]:
    """
    Detect potential secrets in content without sanitizing.

    Useful for validation and warning generation.

    Args:
        content: Content to scan

    Returns:
        List of detected secrets with pattern info
    """
    detected = []

    for pattern, replacement in SECRET_PATTERNS:
        matches = re.finditer(pattern, content)
        for match in matches:
            detected.append({
                "pattern": replacement.replace("${", "").replace("}", ""),
                "value_preview": match.group()[:8] + "..." if len(match.group()) > 8 else match.group(),
                "position": match.start()
            })

    return detected


# =============================================================================
# ARTIFACT STORE
# =============================================================================

class ArtifactStore:
    """
    Store and retrieve infrastructure artifacts via Context DNA's SeaweedFS backend.

    Provides:
    - Automatic secret sanitization before storage
    - Linking artifacts to learning sessions
    - Pattern search across all artifacts
    - Hierarchical disk organization by area
    """

    DEFAULT_BASE_URL = "http://127.0.0.1:8029/api/v1"
    DEFAULT_API_KEY = "sk-ac-your-root-api-bearer-token"

    def __init__(
        self,
        base_url: str = None,
        api_key: str = None,
        space_id: str = None
    ):
        """Initialize artifact store.

        Args:
            base_url: Context DNA API URL
            api_key: API key
            space_id: Space ID (uses default ersim space if not provided)
        """
        if not ACONTEXT_AVAILABLE:
            raise RuntimeError("acontext package not installed. Run: pip install acontext")

        self.base_url = base_url or os.getenv("ACONTEXT_BASE_URL", self.DEFAULT_BASE_URL)
        self.api_key = api_key or os.getenv("ACONTEXT_API_KEY", self.DEFAULT_API_KEY)
        self.space_id = space_id or os.getenv("ACONTEXT_SPACE_ID")

        self.client = AcontextClient(
            base_url=self.base_url,
            api_key=self.api_key
        )

        # Ensure we have a space
        if not self.space_id:
            self._ensure_space()

        # Track created disks locally
        self._disk_cache = {}
        self._load_disk_cache()

    def _ensure_space(self):
        """Find or create the default space."""
        spaces = self.client.spaces.list()
        for space in spaces.items:
            if space.configs.get("name") == "ersim-voice-learnings":
                self.space_id = space.id
                return

        # Create if not found
        space = self.client.spaces.create(
            user="ersim-voice-agent",
            configs={
                "name": "ersim-voice-learnings",
                "description": "ER Simulator learnings and artifacts"
            }
        )
        self.space_id = space.id

    def _get_cache_file(self) -> Path:
        """Get path to disk cache file."""
        return Path(__file__).parent / ".artifact_disk_cache.json"

    def _load_disk_cache(self):
        """Load disk cache from file."""
        cache_file = self._get_cache_file()
        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    self._disk_cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._disk_cache = {}

    def _save_disk_cache(self):
        """Save disk cache to file."""
        cache_file = self._get_cache_file()
        with open(cache_file, "w") as f:
            json.dump(self._disk_cache, f, indent=2)

    def store_with_artifacts(
        self,
        session_id: str,
        artifacts: dict[str, str],
        area: str = "general",
        sanitize: bool = True
    ) -> str:
        """
        Store procedure artifacts in SeaweedFS.

        Creates a disk for the procedure and uploads all artifacts.
        Automatically sanitizes secrets before storage.

        Args:
            session_id: Learning session ID to link to
            artifacts: Dict of {file_path: content}
            area: Architecture area (django, livekit, gpu, etc.)
            sanitize: Whether to sanitize secrets (default: True)

        Returns:
            Disk ID for retrieval
        """
        # Create disk for this procedure
        disk_name = f"proc-{area}-{session_id[:8]}"

        try:
            # SDK signature: create(*, user: str | None = None) -> Disk
            disk = self.client.disks.create(user=f"ersim-{area}")
            disk_id = disk.id
        except Exception as e:
            # If disk creation fails, try to use existing or generate ID
            print(f"Warning: Disk creation failed ({e}), using session-based ID")
            disk_id = f"disk-{session_id[:16]}"

        # Upload each artifact
        for file_path, content in artifacts.items():
            # Sanitize secrets before storage
            safe_content = sanitize_secrets(content) if sanitize else content

            # Detect what was sanitized
            if sanitize:
                detected = detect_secrets(content)
                if detected:
                    print(f"   Sanitized {len(detected)} secrets from {file_path}")

            try:
                # SDK signature: upsert(disk_id, *, file: FileUpload | tuple[str, bytes], file_path: str | None = None)
                # File is a tuple of (filename, content_bytes)
                content_bytes = safe_content.encode('utf-8') if isinstance(safe_content, str) else safe_content
                self.client.artifacts.upsert(
                    disk_id,
                    file=(file_path, content_bytes),
                    file_path=file_path
                )
                print(f"   Stored: {file_path}")
            except Exception as e:
                print(f"   Warning: Failed to store {file_path}: {e}")

        # Cache the disk info
        self._disk_cache[session_id] = {
            "disk_id": disk_id,
            "area": area,
            "files": list(artifacts.keys()),
            "created_at": datetime.now().isoformat()
        }
        self._save_disk_cache()

        print(f"✅ Stored {len(artifacts)} artifacts to disk {disk_id}")
        return disk_id

    def get_procedure_artifacts(self, disk_id: str) -> dict[str, str]:
        """
        Retrieve all artifacts for a procedure.

        Args:
            disk_id: Disk ID from store_with_artifacts()

        Returns:
            Dict of {file_path: content}
        """
        results = {}

        def _list_recursive(path: str = None):
            """Recursively list artifacts in directories."""
            try:
                # Use None for root, or specific path for directories
                resp = self.client.artifacts.list(disk_id, path=path) if path else self.client.artifacts.list(disk_id)

                # Add artifacts from this directory
                for artifact in resp.artifacts:
                    full_path = f"{artifact.path}{artifact.filename}".lstrip("/")
                    # Get content if needed
                    try:
                        content_resp = self.client.artifacts.get(
                            disk_id,
                            file_path=artifact.path,
                            filename=artifact.filename,
                            with_content=True
                        )
                        # content is a FileContent object with 'raw' attribute
                        if content_resp.content:
                            if hasattr(content_resp.content, 'raw'):
                                results[full_path] = content_resp.content.raw
                            elif hasattr(content_resp.content, 'decode'):
                                results[full_path] = content_resp.content.decode('utf-8')
                            elif hasattr(content_resp.content, 'read'):
                                results[full_path] = content_resp.content.read().decode('utf-8')
                            else:
                                results[full_path] = str(content_resp.content)
                    except Exception as e:
                        print(f"Warning: Failed to get content for {full_path}: {e}")

                # Recurse into subdirectories
                for directory in resp.directories:
                    dir_path = f"/{directory}/" if not directory.startswith("/") else f"{directory}/"
                    _list_recursive(dir_path)

            except Exception as e:
                print(f"Warning: Failed to list {path}: {e}")

        try:
            _list_recursive(None)
        except Exception as e:
            print(f"Warning: Failed to retrieve artifacts: {e}")

        return results

    def get_artifact(self, disk_id: str, file_path: str) -> Optional[str]:
        """
        Retrieve a specific artifact by path.

        Args:
            disk_id: Disk ID
            file_path: Path to the artifact (e.g., "memory/local_llm_analyzer.py")

        Returns:
            Artifact content or None if not found
        """
        try:
            # Parse path and filename
            parts = file_path.rsplit("/", 1)
            if len(parts) == 2:
                path = f"/{parts[0]}/"
                filename = parts[1]
            else:
                path = "/"
                filename = file_path

            artifact = self.client.artifacts.get(
                disk_id,
                file_path=path,
                filename=filename,
                with_content=True
            )

            # content is a FileContent object with 'raw' attribute
            if artifact.content:
                if hasattr(artifact.content, 'raw'):
                    return artifact.content.raw
                elif hasattr(artifact.content, 'decode'):
                    return artifact.content.decode('utf-8')
                elif hasattr(artifact.content, 'read'):
                    return artifact.content.read().decode('utf-8')
                else:
                    return str(artifact.content)
            return None
        except Exception as e:
            print(f"Warning: Failed to get artifact {file_path}: {e}")
            return None

    def search_artifacts(self, query: str, area: str = None) -> list[dict]:
        """
        Search across all stored artifacts using grep.

        Args:
            query: Search query (regex supported)
            area: Optional area filter

        Returns:
            List of matches with disk_id, file_path, and matching lines
        """
        matches = []

        # Search through all cached disks
        for session_id, info in self._disk_cache.items():
            if area and info.get("area") != area:
                continue

            disk_id = info["disk_id"]

            try:
                result = self.client.artifacts.grep_artifacts(
                    disk_id,
                    query=query
                )

                for match in result.matches:
                    matches.append({
                        "disk_id": disk_id,
                        "session_id": session_id,
                        "area": info.get("area"),
                        "file_path": match.file_path,
                        "line_number": match.line_number,
                        "content": match.content
                    })
            except Exception as e:
                # Disk may not exist or grep may not be supported
                continue

        return matches

    def list_artifacts_by_area(self, area: str) -> list[dict]:
        """
        List all artifacts for a given architecture area.

        Args:
            area: Architecture area (django, livekit, gpu, etc.)

        Returns:
            List of artifact metadata
        """
        results = []

        for session_id, info in self._disk_cache.items():
            if info.get("area") == area:
                results.append({
                    "session_id": session_id,
                    "disk_id": info["disk_id"],
                    "files": info.get("files", []),
                    "created_at": info.get("created_at")
                })

        return results

    def get_disk_for_session(self, session_id: str) -> Optional[str]:
        """Get disk ID for a session if artifacts were stored."""
        info = self._disk_cache.get(session_id)
        return info["disk_id"] if info else None

    def delete_artifacts(self, disk_id: str):
        """
        Delete all artifacts for a disk.

        Use with caution - this removes stored infrastructure files.

        Args:
            disk_id: Disk to delete
        """
        try:
            self.client.disks.delete(disk_id)

            # Remove from cache
            for session_id, info in list(self._disk_cache.items()):
                if info.get("disk_id") == disk_id:
                    del self._disk_cache[session_id]
            self._save_disk_cache()

            print(f"   Deleted disk {disk_id}")
        except Exception as e:
            print(f"Warning: Failed to delete disk: {e}")


# =============================================================================
# INFRASTRUCTURE FILE DETECTION
# =============================================================================

# Patterns for infrastructure files worth storing
INFRA_FILE_PATTERNS = [
    r"^infra/.*\.tf$",           # Terraform
    r".*[Dd]ocker[Ff]ile.*",     # Dockerfiles
    r".*docker-compose.*\.ya?ml$",  # Docker Compose
    r"\.github/workflows/.*\.ya?ml$",  # GitHub Actions
    r"scripts/deploy.*",         # Deploy scripts
    r".*\.service$",             # Systemd services
    r".*nginx.*\.conf$",         # Nginx configs
    r".*gunicorn.*\.conf.*",     # Gunicorn configs
    r".*supervisord.*\.conf$",   # Supervisor configs
    r".*\.sh$",                  # Shell scripts (in infra dirs)
    r"requirements.*\.txt$",     # Python requirements
    r"package\.json$",           # Node dependencies
]


def is_infrastructure_file(file_path: str) -> bool:
    """Check if a file path matches infrastructure patterns."""
    for pattern in INFRA_FILE_PATTERNS:
        if re.match(pattern, file_path):
            return True

    # Also check if file is in infra-related directories
    infra_dirs = ["infra/", "deploy/", "scripts/", "config/", ".github/"]
    for dir_prefix in infra_dirs:
        if file_path.startswith(dir_prefix):
            return True

    return False


def extract_artifacts_from_commit_files(files: list[str], repo_root: str) -> dict[str, str]:
    """
    Extract artifact contents from a list of committed files.

    Args:
        files: List of file paths from git commit
        repo_root: Path to repository root

    Returns:
        Dict of {file_path: content} for infrastructure files
    """
    artifacts = {}

    for file_path in files:
        if not is_infrastructure_file(file_path):
            continue

        full_path = Path(repo_root) / file_path
        if full_path.exists() and full_path.is_file():
            try:
                content = full_path.read_text()
                artifacts[file_path] = content
            except (OSError, UnicodeDecodeError):
                continue

    return artifacts


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Artifact Store CLI")
        print("")
        print("Commands:")
        print("  store <session_id> <file_path>   - Store a single artifact")
        print("  get <disk_id> <file_path>        - Retrieve an artifact")
        print("  list <area>                      - List artifacts by area")
        print("  search <query>                   - Search across all artifacts")
        print("  sanitize <file_path>             - Preview sanitization for a file")
        print("")
        print("Examples:")
        print("  python artifact_store.py store abc123 infra/main.tf")
        print("  python artifact_store.py search 'systemctl restart'")
        print("  python artifact_store.py sanitize infra/terraform/main.tf")
        sys.exit(0)

    cmd = sys.argv[1]

    try:
        store = ArtifactStore()
    except Exception as e:
        print(f"Failed to initialize artifact store: {e}")
        sys.exit(1)

    if cmd == "store":
        if len(sys.argv) < 4:
            print("Usage: store <session_id> <file_path>")
            sys.exit(1)

        session_id = sys.argv[2]
        file_path = sys.argv[3]

        if not Path(file_path).exists():
            print(f"File not found: {file_path}")
            sys.exit(1)

        content = Path(file_path).read_text()
        disk_id = store.store_with_artifacts(
            session_id,
            {file_path: content},
            area=Path(file_path).parts[0] if len(Path(file_path).parts) > 1 else "general"
        )
        print(f"Stored to disk: {disk_id}")

    elif cmd == "get":
        if len(sys.argv) < 4:
            print("Usage: get <disk_id> <file_path>")
            sys.exit(1)

        disk_id = sys.argv[2]
        file_path = sys.argv[3]

        content = store.get_artifact(disk_id, file_path)
        if content:
            print(content)
        else:
            print("Artifact not found")
            sys.exit(1)

    elif cmd == "list":
        area = sys.argv[2] if len(sys.argv) > 2 else None

        if area:
            artifacts = store.list_artifacts_by_area(area)
            print(f"Artifacts for area '{area}':")
        else:
            artifacts = [
                {"area": info.get("area"), "files": info.get("files", []), "session_id": sid}
                for sid, info in store._disk_cache.items()
            ]
            print("All stored artifacts:")

        for art in artifacts:
            print(f"\n  Session: {art.get('session_id', 'N/A')}")
            print(f"  Area: {art.get('area', 'general')}")
            print(f"  Files: {', '.join(art.get('files', []))}")

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: search <query>")
            sys.exit(1)

        query = " ".join(sys.argv[2:])
        matches = store.search_artifacts(query)

        if matches:
            print(f"Found {len(matches)} matches for '{query}':")
            for match in matches:
                print(f"\n  {match['file_path']}:{match.get('line_number', '?')}")
                print(f"  Area: {match.get('area', 'general')}")
                print(f"  Content: {match.get('content', '')[:80]}...")
        else:
            print("No matches found")

    elif cmd == "sanitize":
        if len(sys.argv) < 3:
            print("Usage: sanitize <file_path>")
            sys.exit(1)

        file_path = sys.argv[2]

        if not Path(file_path).exists():
            print(f"File not found: {file_path}")
            sys.exit(1)

        content = Path(file_path).read_text()

        # Show what would be sanitized
        detected = detect_secrets(content)
        if detected:
            print(f"Detected {len(detected)} secrets:")
            for d in detected:
                print(f"  - {d['pattern']}: {d['value_preview']}")
            print("\nSanitized content preview (first 500 chars):")
            print("-" * 40)
            print(sanitize_secrets(content)[:500])
        else:
            print("No secrets detected in file")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
