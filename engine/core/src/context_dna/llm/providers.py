"""LLM Provider Registry for Context DNA.

Defines all supported providers with their configurations.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class LLMProvider:
    """Configuration for an LLM provider."""

    name: str
    endpoint: str
    model: str
    embedding_model: Optional[str]
    api_key_env: Optional[str]
    cost_per_1k_tokens: float
    supports_streaming: bool
    max_context: int
    embedding_dimension: int = 1536  # Default to OpenAI dimension

    def __str__(self) -> str:
        return f"{self.name} ({self.model})"


# Provider Registry
PROVIDERS = {
    "openai": LLMProvider(
        name="openai",
        endpoint="https://api.openai.com/v1",
        model="gpt-4o-mini",
        embedding_model="text-embedding-3-small",
        api_key_env="Context_DNA_OPENAI",
        cost_per_1k_tokens=0.00015,
        supports_streaming=True,
        max_context=128000,
        embedding_dimension=1536,
    ),
    "anthropic": LLMProvider(
        name="anthropic",
        endpoint="https://api.anthropic.com/v1",
        model="claude-3-5-sonnet-20241022",
        embedding_model=None,  # Use OpenAI for embeddings
        api_key_env="ANTHROPIC_API_KEY",
        cost_per_1k_tokens=0.003,
        supports_streaming=True,
        max_context=200000,
        embedding_dimension=1536,
    ),
    "ollama": LLMProvider(
        name="ollama",
        endpoint="http://localhost:11434",
        model="llama3.1:8b-instruct-q4_K_M",
        embedding_model="nomic-embed-text",
        api_key_env=None,  # No API key needed
        cost_per_1k_tokens=0.0,  # Free!
        supports_streaming=True,
        max_context=32000,
        embedding_dimension=768,  # nomic-embed-text dimension
    ),
    "lm-studio": LLMProvider(
        name="lm-studio",
        endpoint="http://localhost:1234/v1",
        model="local-model",
        embedding_model=None,
        api_key_env=None,
        cost_per_1k_tokens=0.0,
        supports_streaming=True,
        max_context=32000,
        embedding_dimension=1536,
    ),
}


# Model recommendations by hardware
HARDWARE_RECOMMENDATIONS = {
    "high_ram_gpu": {
        "model": "llama3.1:8b-instruct-q4_K_M",
        "min_ram_gb": 16,
        "requires_gpu": True,
        "description": "Fast GPU inference with 8B parameter model",
    },
    "high_ram_cpu": {
        "model": "llama3.1:8b-instruct-q4_K_M",
        "min_ram_gb": 16,
        "requires_gpu": False,
        "description": "CPU inference with 8B parameter model (slower)",
    },
    "medium_ram": {
        "model": "llama3.2:3b-instruct-q4_K_M",
        "min_ram_gb": 8,
        "requires_gpu": False,
        "description": "Smaller 3B model for limited RAM",
    },
    "low_ram": {
        "model": "phi3:mini",
        "min_ram_gb": 4,
        "requires_gpu": False,
        "description": "Tiny model for very limited systems",
    },
}


def get_provider(name: str) -> LLMProvider:
    """Get a provider by name.

    Args:
        name: Provider name (openai, anthropic, ollama, lm-studio)

    Returns:
        LLMProvider configuration

    Raises:
        ValueError: If provider not found
    """
    if name not in PROVIDERS:
        available = ", ".join(PROVIDERS.keys())
        raise ValueError(f"Unknown provider: {name}. Available: {available}")
    return PROVIDERS[name]


def list_providers() -> list:
    """List all available provider names."""
    return list(PROVIDERS.keys())
