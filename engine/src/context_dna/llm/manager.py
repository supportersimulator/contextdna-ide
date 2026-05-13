"""LLM Provider Manager for Context DNA.

Handles provider selection, health checking, and automatic fallback.
Prefers local providers (zero cost) over cloud providers.
"""

import os
import platform
import subprocess
from typing import Optional, List, Dict, Any

from context_dna.llm.base import (
    BaseLLMProvider,
    LLMResponse,
    EmbeddingResponse,
    NoProviderAvailableError,
    ProviderUnavailableError,
)
from context_dna.llm.providers import PROVIDERS, LLMProvider, HARDWARE_RECOMMENDATIONS


class ProviderManager:
    """Manages LLM providers with automatic selection and fallback.

    Selection Priority:
    1. User-specified preferred provider (if available)
    2. Local provider if available (Ollama, LM Studio) - zero cost
    3. Cloud provider with required capabilities (OpenAI, Anthropic)

    Usage:
        manager = ProviderManager()

        # Auto-select best available provider
        provider = manager.get_provider()
        response = provider.generate("Hello, world!")

        # Generate embeddings (auto-selects embedding-capable provider)
        embedding = manager.embed("text to embed")

        # Force specific provider
        provider = manager.get_provider("openai")
    """

    def __init__(
        self,
        preferred_provider: Optional[str] = None,
        preferred_embedding_provider: Optional[str] = None,
    ):
        """Initialize provider manager.

        Args:
            preferred_provider: Preferred LLM provider name
            preferred_embedding_provider: Preferred embedding provider name
        """
        self._preferred_provider = preferred_provider or os.getenv(
            "CONTEXT_DNA_LLM_PROVIDER"
        )
        self._preferred_embedding_provider = preferred_embedding_provider or os.getenv(
            "CONTEXT_DNA_EMBEDDING_PROVIDER"
        )

        # Cache provider instances
        self._providers: Dict[str, BaseLLMProvider] = {}
        self._health_cache: Dict[str, bool] = {}

    def get_provider(
        self,
        provider_name: Optional[str] = None,
        require_embeddings: bool = False,
    ) -> BaseLLMProvider:
        """Get an LLM provider instance.

        Args:
            provider_name: Specific provider to use (auto-selects if None)
            require_embeddings: If True, only return providers that support embeddings

        Returns:
            BaseLLMProvider instance

        Raises:
            NoProviderAvailableError: If no provider is available
        """
        if provider_name:
            return self._get_specific_provider(provider_name)

        return self._auto_select_provider(require_embeddings)

    def get_embedding_provider(self) -> BaseLLMProvider:
        """Get a provider that supports embeddings.

        Returns:
            BaseLLMProvider instance with embedding support

        Raises:
            NoProviderAvailableError: If no embedding provider is available
        """
        # Try preferred embedding provider first
        if self._preferred_embedding_provider:
            provider = self._get_specific_provider(self._preferred_embedding_provider)
            if provider.supports_embeddings:
                return provider

        # Auto-select with embedding requirement
        return self._auto_select_provider(require_embeddings=True)

    def embed(self, text: str) -> EmbeddingResponse:
        """Generate embedding using best available provider.

        Args:
            text: Text to embed

        Returns:
            EmbeddingResponse with embedding vector
        """
        provider = self.get_embedding_provider()
        return provider.embed(text)

    def embed_batch(self, texts: List[str]) -> List[EmbeddingResponse]:
        """Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of EmbeddingResponses
        """
        provider = self.get_embedding_provider()
        return provider.embed_batch(texts)

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        provider_name: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate text using best available provider.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            provider_name: Specific provider to use
            **kwargs: Additional provider options

        Returns:
            LLMResponse with generated text
        """
        provider = self.get_provider(provider_name)
        return provider.generate(prompt, system_prompt, **kwargs)

    def health_check_all(self) -> Dict[str, bool]:
        """Check health of all providers.

        Returns:
            Dictionary mapping provider name to health status
        """
        results = {}
        for name in PROVIDERS:
            results[name] = self._check_health(name)
        return results

    def list_available(self) -> List[str]:
        """List all available (healthy) providers.

        Returns:
            List of available provider names
        """
        return [name for name in PROVIDERS if self._check_health(name)]

    def _auto_select_provider(self, require_embeddings: bool = False) -> BaseLLMProvider:
        """Auto-select the best available provider.

        Priority:
        1. Preferred provider (if set and available)
        2. Local providers (ollama, lm-studio) - zero cost
        3. Cloud providers (openai, anthropic)
        """
        # Try preferred provider first
        if self._preferred_provider:
            if self._check_health(self._preferred_provider):
                provider = self._get_specific_provider(self._preferred_provider)
                if not require_embeddings or provider.supports_embeddings:
                    return provider

        # Try local providers first (zero cost)
        for name in ["ollama", "lm-studio"]:
            if self._check_health(name):
                provider = self._get_specific_provider(name)
                if not require_embeddings or provider.supports_embeddings:
                    return provider

        # Fall back to cloud providers
        for name in ["openai", "anthropic"]:
            if self._check_health(name):
                provider = self._get_specific_provider(name)
                if not require_embeddings or provider.supports_embeddings:
                    return provider

        raise NoProviderAvailableError(
            "No LLM provider available. Please either:\n"
            "1. Start Ollama: docker-compose up -d ollama\n"
            "2. Set Context_DNA_OPENAI environment variable\n"
            "3. Set ANTHROPIC_API_KEY environment variable"
        )

    def _get_specific_provider(self, name: str) -> BaseLLMProvider:
        """Get or create a specific provider instance."""
        if name in self._providers:
            return self._providers[name]

        if name not in PROVIDERS:
            available = ", ".join(PROVIDERS.keys())
            raise ValueError(f"Unknown provider: {name}. Available: {available}")

        # Create provider instance
        if name == "openai":
            from context_dna.llm.openai_provider import OpenAIProvider
            provider = OpenAIProvider()
        elif name == "anthropic":
            from context_dna.llm.anthropic_provider import AnthropicProvider
            provider = AnthropicProvider()
        elif name == "ollama":
            from context_dna.llm.ollama_provider import OllamaProvider
            provider = OllamaProvider()
        elif name == "lm-studio":
            # LM Studio uses OpenAI-compatible API
            from context_dna.llm.openai_provider import OpenAIProvider
            config = PROVIDERS["lm-studio"]
            provider = OpenAIProvider(
                api_key="lm-studio",  # LM Studio doesn't need real key
            )
            provider._endpoint = config.endpoint
            provider._model = config.model
        else:
            raise ValueError(f"Provider {name} not implemented")

        self._providers[name] = provider
        return provider

    def _check_health(self, provider_name: str) -> bool:
        """Check if a provider is healthy (cached)."""
        # Check cache first
        if provider_name in self._health_cache:
            return self._health_cache[provider_name]

        # Check if provider can be created
        try:
            config = PROVIDERS.get(provider_name)
            if not config:
                return False

            # For cloud providers, check if API key is set
            if config.api_key_env:
                if not os.getenv(config.api_key_env):
                    self._health_cache[provider_name] = False
                    return False

            # Try to create and health check
            provider = self._get_specific_provider(provider_name)
            healthy = provider.health_check()
            self._health_cache[provider_name] = healthy
            return healthy

        except Exception:
            self._health_cache[provider_name] = False
            return False

    def clear_health_cache(self) -> None:
        """Clear the health check cache."""
        self._health_cache.clear()


class HardwareDetector:
    """Detects system hardware for model recommendations."""

    @staticmethod
    def detect() -> Dict[str, Any]:
        """Detect system hardware capabilities.

        Returns:
            Dictionary with hardware info and recommendations
        """
        info = {
            "os": platform.system(),
            "arch": platform.machine(),
            "ram_gb": HardwareDetector._get_ram_gb(),
            "gpu": HardwareDetector._detect_gpu(),
            "recommended_model": None,
            "inference_mode": "cpu",
        }

        # Determine recommended model
        if info["gpu"] and info["ram_gb"] >= 16:
            info["recommended_model"] = HARDWARE_RECOMMENDATIONS["high_ram_gpu"]["model"]
            info["inference_mode"] = "gpu"
        elif info["ram_gb"] >= 16:
            info["recommended_model"] = HARDWARE_RECOMMENDATIONS["high_ram_cpu"]["model"]
            info["inference_mode"] = "cpu"
        elif info["ram_gb"] >= 8:
            info["recommended_model"] = HARDWARE_RECOMMENDATIONS["medium_ram"]["model"]
            info["inference_mode"] = "cpu"
        else:
            info["recommended_model"] = HARDWARE_RECOMMENDATIONS["low_ram"]["model"]
            info["inference_mode"] = "cpu"

        return info

    @staticmethod
    def _get_ram_gb() -> int:
        """Get total RAM in GB."""
        try:
            if platform.system() == "Darwin":  # macOS
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    return int(result.stdout.strip()) // (1024 ** 3)
            elif platform.system() == "Linux":
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            kb = int(line.split()[1])
                            return kb // (1024 ** 2)
            elif platform.system() == "Windows":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                c_ulong = ctypes.c_ulong
                class MEMORYSTATUS(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", c_ulong),
                        ("dwMemoryLoad", c_ulong),
                        ("dwTotalPhys", c_ulong),
                        ("dwAvailPhys", c_ulong),
                        ("dwTotalPageFile", c_ulong),
                        ("dwAvailPageFile", c_ulong),
                        ("dwTotalVirtual", c_ulong),
                        ("dwAvailVirtual", c_ulong),
                    ]
                memoryStatus = MEMORYSTATUS()
                memoryStatus.dwLength = ctypes.sizeof(MEMORYSTATUS)
                kernel32.GlobalMemoryStatus(ctypes.byref(memoryStatus))
                return memoryStatus.dwTotalPhys // (1024 ** 3)
        except Exception as e:
            print(f"[WARN] Failed to detect RAM size: {e}")
        return 8  # Default assumption

    @staticmethod
    def _detect_gpu() -> Optional[str]:
        """Detect GPU availability."""
        system = platform.system()

        try:
            if system == "Darwin":  # macOS - check for Metal
                result = subprocess.run(
                    ["system_profiler", "SPDisplaysDataType"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if "Metal" in result.stdout or "Apple" in result.stdout:
                    # Check if it's Apple Silicon
                    if platform.machine() == "arm64":
                        return "apple-silicon"
                    return "metal"
            elif system == "Linux":
                # Check for NVIDIA GPU
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return f"nvidia:{result.stdout.strip().split()[0]}"
            elif system == "Windows":
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return f"nvidia:{result.stdout.strip()}"
        except Exception as e:
            print(f"[WARN] Failed to detect GPU: {e}")

        return None
