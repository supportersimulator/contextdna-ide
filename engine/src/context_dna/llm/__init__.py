"""LLM Provider System for Context DNA.

Supports multiple providers with automatic selection:
- OpenAI (gpt-4o-mini, text-embedding-3-small)
- Anthropic (claude-3.5-sonnet)
- Ollama (llama3.1:8b, nomic-embed-text) - Local, zero cost
- LM Studio (any local model)

Usage:
    from context_dna.llm import ProviderManager

    manager = ProviderManager()
    provider = manager.get_provider()  # Auto-selects best available

    # Generate text
    response = provider.generate("Summarize this code...")

    # Generate embeddings
    embedding = manager.embed("text to embed")
"""

from context_dna.llm.providers import LLMProvider, PROVIDERS
from context_dna.llm.manager import ProviderManager
from context_dna.llm.base import BaseLLMProvider

__all__ = [
    "LLMProvider",
    "PROVIDERS",
    "ProviderManager",
    "BaseLLMProvider",
]
