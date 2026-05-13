"""
Antigravity IDE Adapter for Context DNA

Web-based injection via API for Antigravity (Synaptic's web interface).

Antigravity uses a REST API for context injection rather than
file-based hooks like traditional IDEs.

Created: January 29, 2026
Part of: Webhook Hardening Initiative + Vibe Coder Launch
"""

import os
import json
from pathlib import Path
from typing import Dict, Optional

from .base_adapter import IDEAdapter, VerificationResult, InstallResult


class AntigravityAdapter(IDEAdapter):
    """
    Antigravity: Web-based context injection via API.

    Unlike file-based IDEs, Antigravity connects to Context DNA's
    API server to receive context injections in real-time.

    Configuration:
    - ~/.context-dna/antigravity.json - API connection settings
    - Environment variable: CONTEXTDNA_API_URL

    Injection method:
    - Antigravity queries Context DNA API before each prompt
    - Context is returned as JSON and rendered in the web UI
    """

    DEFAULT_API_URL = "http://127.0.0.1:8029"

    @property
    def name(self) -> str:
        return "Antigravity"

    @property
    def config_path(self) -> Path:
        """Return path to Antigravity config."""
        return Path.home() / ".context-dna" / "antigravity.json"

    def is_installed(self) -> bool:
        """
        Check if Antigravity is configured.

        Antigravity is considered installed if:
        1. Config file exists with API URL, OR
        2. CONTEXTDNA_API_URL environment variable is set, OR
        3. Context DNA API is running on default port
        """
        # Check for config file
        if self.config_path.exists():
            return True

        # Check for environment variable
        if os.environ.get("CONTEXTDNA_API_URL"):
            return True

        # Check if API is running on default port
        if self._check_api_available():
            return True

        return False

    def _check_api_available(self) -> bool:
        """Check if Context DNA API is available."""
        try:
            import urllib.request
            api_url = self._get_api_url()
            req = urllib.request.Request(
                f"{api_url}/health",
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status == 200
        except Exception:
            return False

    def _get_api_url(self) -> str:
        """Get the API URL from config or environment."""
        # Try environment variable first
        env_url = os.environ.get("CONTEXTDNA_API_URL")
        if env_url:
            return env_url.rstrip("/")

        # Try config file
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    config = json.load(f)
                    return config.get("api_url", self.DEFAULT_API_URL).rstrip("/")
            except Exception as e:
                print(f"[WARN] Antigravity config read failed: {e}")

        return self.DEFAULT_API_URL

    def install_hooks(self) -> InstallResult:
        """
        Install Antigravity configuration.

        For Antigravity, installation means:
        1. Creating the config file with API URL
        2. Registering with the Context DNA API (if available)
        """
        files_created = []
        files_modified = []

        try:
            # Create .context-dna directory if needed
            self.config_path.parent.mkdir(parents=True, exist_ok=True)

            # Create or update config
            if self.config_path.exists():
                with open(self.config_path) as f:
                    config = json.load(f)
                files_modified.append(str(self.config_path))
            else:
                config = {}
                files_created.append(str(self.config_path))

            # Set API URL
            config["api_url"] = self._get_api_url()
            config["enabled"] = True
            config["injection_mode"] = "api"

            # Add webhook endpoint for push-based injection
            config["webhook_endpoint"] = "/api/antigravity/inject"

            # Save config
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)

            # Try to register with API
            registration_status = self._register_with_api()

            return InstallResult(
                success=True,
                message=f"Antigravity configured. API: {config['api_url']}. Registration: {registration_status}",
                files_created=files_created,
                files_modified=files_modified
            )

        except Exception as e:
            return InstallResult(
                success=False,
                message="Failed to configure Antigravity",
                error=str(e)[:100]
            )

    def _register_with_api(self) -> str:
        """Register this Antigravity instance with Context DNA API."""
        try:
            import urllib.request
            api_url = self._get_api_url()

            # Create registration payload
            payload = json.dumps({
                "client_type": "antigravity",
                "config_path": str(self.config_path),
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{api_url}/api/clients/register",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    return "registered"
                return f"status:{response.status}"

        except urllib.error.URLError:
            return "api_unavailable"
        except Exception as e:
            return f"error:{str(e)[:30]}"

    def verify_hooks(self) -> VerificationResult:
        """Verify Antigravity configuration is correct."""
        # Check config exists
        if not self.config_path.exists():
            return VerificationResult(
                success=False,
                message="Antigravity config file does not exist",
                details={"expected_path": str(self.config_path)}
            )

        # Read and validate config
        try:
            with open(self.config_path) as f:
                config = json.load(f)

            if not config.get("api_url"):
                return VerificationResult(
                    success=False,
                    message="No API URL configured",
                    details={"config": config}
                )

            if not config.get("enabled", False):
                return VerificationResult(
                    success=False,
                    message="Antigravity is disabled in config",
                    details={"config": config}
                )

            # Check API connectivity
            api_available = self._check_api_available()

            return VerificationResult(
                success=True,
                message=f"Antigravity configured. API {'available' if api_available else 'unavailable'}",
                details={
                    "api_url": config["api_url"],
                    "api_available": api_available,
                    "injection_mode": config.get("injection_mode", "unknown")
                }
            )

        except json.JSONDecodeError as e:
            return VerificationResult(
                success=False,
                message=f"Invalid JSON in config: {str(e)[:50]}",
                details={"path": str(self.config_path)}
            )
        except Exception as e:
            return VerificationResult(
                success=False,
                message=f"Error verifying config: {str(e)[:50]}",
                details={"path": str(self.config_path)}
            )

    def get_injection_method(self) -> str:
        """Return how Antigravity receives injections."""
        return "REST API query before each prompt (real-time)"

    def get_status(self) -> Dict:
        """Get complete status for Antigravity adapter."""
        base_status = super().get_status()

        # Add Antigravity-specific status
        base_status["api_url"] = self._get_api_url()
        base_status["api_available"] = self._check_api_available()

        return base_status
