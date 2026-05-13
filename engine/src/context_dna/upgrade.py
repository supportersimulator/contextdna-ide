"""Context DNA Pro Upgrade System.

Handles:
- License key validation
- Pro tier activation
- Model downloading and configuration
- Hardware detection for optimal settings
"""

import functools
import hashlib
import json
import os
import platform
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Generator

try:
    import requests
except ImportError:
    requests = None


@dataclass
class LicenseInfo:
    """License information."""

    license_key: str
    activated: bool
    activated_at: Optional[datetime]
    machine_id: str
    tier: str  # 'free', 'pro'
    features: list


class LicenseManager:
    """Manages Context DNA license and activation.

    License key format: CDNA-XXXX-XXXX-XXXX-XXXX
    - Tied to machine ID
    - Can be transferred up to 2 times
    - Works offline after initial activation
    """

    LICENSE_FILE = Path.home() / ".context-dna" / "license.json"
    VALIDATION_URL = "https://api.context-dna.dev/licenses/validate"
    ACTIVATION_URL = "https://api.context-dna.dev/licenses/activate"

    def __init__(self):
        """Initialize license manager."""
        self._license_cache: Optional[LicenseInfo] = None

    def is_pro(self) -> bool:
        """Check if Pro tier is activated."""
        license_info = self.get_license_info()
        return license_info is not None and license_info.activated

    def get_license_info(self) -> Optional[LicenseInfo]:
        """Get current license information."""
        if self._license_cache:
            return self._license_cache

        if not self.LICENSE_FILE.exists():
            return None

        try:
            data = json.loads(self.LICENSE_FILE.read_text())
            self._license_cache = LicenseInfo(
                license_key=data.get("license_key", ""),
                activated=data.get("activated", False),
                activated_at=datetime.fromisoformat(data["activated_at"])
                if data.get("activated_at")
                else None,
                machine_id=data.get("machine_id", ""),
                tier=data.get("tier", "free"),
                features=data.get("features", []),
            )
            return self._license_cache
        except (json.JSONDecodeError, KeyError):
            return None

    def activate(self, license_key: str) -> bool:
        """Activate a license key.

        Args:
            license_key: License key in format CDNA-XXXX-XXXX-XXXX-XXXX

        Returns:
            True if activation successful
        """
        # Validate format
        if not self._validate_key_format(license_key):
            raise ValueError("Invalid license key format. Expected: CDNA-XXXX-XXXX-XXXX-XXXX")

        machine_id = self._get_machine_id()

        # Try online validation
        if requests:
            try:
                response = requests.post(
                    self.ACTIVATION_URL,
                    json={
                        "license_key": license_key,
                        "machine_id": machine_id,
                    },
                    timeout=10,
                )

                if response.status_code == 200:
                    data = response.json()
                    if data.get("valid"):
                        return self._save_license(license_key, data.get("features", []))
                    else:
                        raise ValueError(data.get("error", "License validation failed"))

            except requests.RequestException as e:
                # Offline activation - trust the key format
                print(f"[WARN] License validation request failed (offline mode): {e}")

        # Offline activation (for development/testing)
        # In production, require online validation
        if os.getenv("CONTEXT_DNA_OFFLINE_ACTIVATION") == "true":
            return self._save_license(license_key, ["local_llm", "priority_support"])

        raise RuntimeError(
            "Could not connect to license server. "
            "Check your internet connection and try again."
        )

    def deactivate(self) -> bool:
        """Deactivate current license."""
        if self.LICENSE_FILE.exists():
            self.LICENSE_FILE.unlink()
            self._license_cache = None
            return True
        return False

    def _validate_key_format(self, key: str) -> bool:
        """Validate license key format."""
        import re
        pattern = r"^CDNA-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$"
        return bool(re.match(pattern, key))

    def _get_machine_id(self) -> str:
        """Generate unique machine identifier."""
        # Combine multiple system attributes for uniqueness
        components = [
            platform.node(),
            platform.machine(),
            platform.processor(),
        ]

        # Add MAC address if available
        try:
            mac = uuid.getnode()
            components.append(str(mac))
        except Exception as e:
            print(f"[WARN] Failed to get MAC address for machine ID: {e}")

        # Hash the components
        combined = "|".join(components)
        return hashlib.sha256(combined.encode()).hexdigest()[:32]

    def _save_license(self, license_key: str, features: list) -> bool:
        """Save license to disk."""
        self.LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "license_key": license_key,
            "activated": True,
            "activated_at": datetime.now().isoformat(),
            "machine_id": self._get_machine_id(),
            "tier": "pro",
            "features": features,
        }

        self.LICENSE_FILE.write_text(json.dumps(data, indent=2))
        self._license_cache = None  # Clear cache
        return True


def require_pro(func):
    """Decorator to require Pro tier for a function."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not LicenseManager().is_pro():
            print("\n" + "=" * 60)
            print("⭐ This feature requires Context DNA Pro")
            print("=" * 60)
            print()
            print("Upgrade to Pro for:")
            print("  ✓ Local LLM inference (zero API costs)")
            print("  ✓ Auto-configured quantized models")
            print("  ✓ Offline operation")
            print("  ✓ Priority support")
            print()
            print("One-time upgrade: $29")
            print("Run: context-dna upgrade")
            print("=" * 60)
            return None
        return func(*args, **kwargs)

    return wrapper


class UpgradeManager:
    """Handles Context DNA Pro upgrades and model setup."""

    UPGRADE_PRICE = 29.00
    STRIPE_PRODUCT_ID = "prod_context_dna_pro"

    # Models to download for Pro tier
    PRO_MODELS = {
        "llm": {
            "high_ram": "llama3.1:8b-instruct-q4_K_M",
            "medium_ram": "llama3.2:3b-instruct-q4_K_M",
            "low_ram": "phi3:mini",
        },
        "embedding": "nomic-embed-text",
    }

    def __init__(self):
        """Initialize upgrade manager."""
        self.license_manager = LicenseManager()
        from context_dna.llm.manager import HardwareDetector
        self.hardware_detector = HardwareDetector

    def show_upgrade_info(self) -> str:
        """Display upgrade information."""
        hw = self.hardware_detector.detect()

        lines = [
            "",
            "╔══════════════════════════════════════════════════════════════════════════════╗",
            "║                    CONTEXT DNA PRO UPGRADE                                     ║",
            "╠══════════════════════════════════════════════════════════════════════════════╣",
            "║                                                                               ║",
            "║  Unlock local AI inference with optimized quantized models:                  ║",
            "║                                                                               ║",
            "║  ✓ Llama 3.1 8B Instruct (Q4_K_M quantized) - 4.7GB                         ║",
            "║  ✓ Nomic Embed Text (embeddings) - 274MB                                    ║",
            "║  ✓ Auto-configured for your hardware                                         ║",
            "║  ✓ Zero API costs after upgrade                                              ║",
            "║  ✓ Works 100% offline                                                        ║",
            "║                                                                               ║",
            f"║  Detected hardware: {self._format_hardware(hw):<52} ║",
            f"║  Recommended: {hw['recommended_model']:<58} ║",
            f"║  Inference mode: {hw['inference_mode']:<54} ║",
            "║                                                                               ║",
            "║  One-time upgrade: $29                                                        ║",
            "║                                                                               ║",
            "║  To upgrade:                                                                  ║",
            "║    1. Purchase license at: https://context-dna.dev/upgrade                   ║",
            "║    2. Run: context-dna upgrade --license CDNA-XXXX-XXXX-XXXX-XXXX            ║",
            "║                                                                               ║",
            "╚══════════════════════════════════════════════════════════════════════════════╝",
            "",
        ]

        return "\n".join(lines)

    def upgrade(self, license_key: Optional[str] = None) -> bool:
        """Perform Pro upgrade.

        Args:
            license_key: License key (prompts if not provided)

        Returns:
            True if upgrade successful
        """
        # Check if already Pro
        if self.license_manager.is_pro():
            print("✓ Already upgraded to Pro!")
            return True

        # Get license key
        if not license_key:
            print(self.show_upgrade_info())
            license_key = input("Enter your license key: ").strip()

        if not license_key:
            print("No license key provided.")
            return False

        # Activate license
        print("Activating license...")
        try:
            self.license_manager.activate(license_key)
            print("✓ License activated!")
        except ValueError as e:
            print(f"✗ Activation failed: {e}")
            return False
        except RuntimeError as e:
            print(f"✗ {e}")
            return False

        # Download models
        print("\nDownloading optimized models...")
        try:
            self._download_models()
            print("\n✓ Pro upgrade complete!")
            print("  Local AI inference is now active.")
            return True
        except Exception as e:
            print(f"✗ Model download failed: {e}")
            print("  Your license is active. Retry model download with:")
            print("  context-dna models pull")
            return True  # License is still valid

    def _download_models(self) -> None:
        """Download and configure optimized models."""
        hw = self.hardware_detector.detect()

        # Determine which LLM model to use
        if hw["ram_gb"] >= 16:
            llm_model = self.PRO_MODELS["llm"]["high_ram"]
        elif hw["ram_gb"] >= 8:
            llm_model = self.PRO_MODELS["llm"]["medium_ram"]
        else:
            llm_model = self.PRO_MODELS["llm"]["low_ram"]

        embedding_model = self.PRO_MODELS["embedding"]

        print(f"  Downloading {llm_model}...")
        for progress in self._pull_model(llm_model):
            if progress.get("status") == "downloading":
                completed = progress.get("completed", 0)
                total = progress.get("total", 1)
                pct = (completed / total) * 100 if total else 0
                print(f"\r  Progress: {pct:.1f}%", end="", flush=True)
            elif progress.get("status") == "success":
                print(f"\r  ✓ {llm_model} downloaded      ")

        print(f"  Downloading {embedding_model}...")
        for progress in self._pull_model(embedding_model):
            if progress.get("status") == "success":
                print(f"  ✓ {embedding_model} downloaded")

        # Save optimized config
        self._save_optimized_config(hw, llm_model, embedding_model)

    def _pull_model(self, model_name: str) -> Generator[Dict[str, Any], None, None]:
        """Pull a model via Ollama."""
        try:
            # Try Docker Ollama first
            result = subprocess.run(
                ["docker", "exec", "context-dna-ollama", "ollama", "pull", model_name],
                capture_output=True,
                text=True,
                timeout=3600,
            )

            if result.returncode == 0:
                yield {"status": "success", "model": model_name}
                return

            # Try local Ollama
            result = subprocess.run(
                ["ollama", "pull", model_name],
                capture_output=True,
                text=True,
                timeout=3600,
            )

            if result.returncode == 0:
                yield {"status": "success", "model": model_name}
            else:
                yield {"status": "error", "error": result.stderr}

        except subprocess.TimeoutExpired:
            yield {"status": "error", "error": "Download timed out"}
        except FileNotFoundError:
            yield {"status": "error", "error": "Ollama not found. Install Ollama first."}

    def _save_optimized_config(
        self,
        hw: Dict[str, Any],
        llm_model: str,
        embedding_model: str,
    ) -> None:
        """Save optimized configuration for the hardware."""
        config_dir = Path.home() / ".context-dna"
        config_dir.mkdir(parents=True, exist_ok=True)

        config = {
            "llm": {
                "provider": "ollama",
                "model": llm_model,
            },
            "embeddings": {
                "provider": "ollama",
                "model": embedding_model,
            },
            "hardware": {
                "os": hw["os"],
                "arch": hw["arch"],
                "ram_gb": hw["ram_gb"],
                "gpu": hw["gpu"],
                "inference_mode": hw["inference_mode"],
            },
            "optimized_at": datetime.now().isoformat(),
        }

        (config_dir / "pro_config.json").write_text(json.dumps(config, indent=2))

    def _format_hardware(self, hw: Dict[str, Any]) -> str:
        """Format hardware info for display."""
        parts = [hw["os"], hw["arch"], f"{hw['ram_gb']}GB RAM"]
        if hw["gpu"]:
            parts.append(hw["gpu"])
        return ", ".join(parts)

    def check_status(self) -> Dict[str, Any]:
        """Check upgrade status."""
        license_info = self.license_manager.get_license_info()
        hw = self.hardware_detector.detect()

        status = {
            "tier": "pro" if self.license_manager.is_pro() else "free",
            "license_key": license_info.license_key if license_info else None,
            "activated_at": license_info.activated_at.isoformat()
            if license_info and license_info.activated_at
            else None,
            "features": license_info.features if license_info else [],
            "hardware": hw,
        }

        # Check if models are installed
        try:
            from context_dna.llm.ollama_provider import OllamaProvider

            ollama = OllamaProvider()
            models = ollama.list_models()
            status["installed_models"] = models
        except Exception:
            status["installed_models"] = []

        return status
