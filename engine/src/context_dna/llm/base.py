"""Base LLM Provider Interface for Context DNA.

All provider implementations must inherit from BaseLLMProvider.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Generator, Any


@dataclass
class LLMResponse:
    """Response from an LLM call."""

    content: str
    model: str
    provider: str
    usage: dict  # tokens used
    cost: float  # estimated cost in USD


@dataclass
class EmbeddingResponse:
    """Response from an embedding call."""

    embedding: List[float]
    model: str
    provider: str
    dimension: int


class BaseLLMProvider(ABC):
    """Abstract base class for LLM providers.

    All provider implementations must implement:
    - generate(): Generate text completion
    - embed(): Generate embeddings (optional)
    - health_check(): Check if provider is available
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name."""
        pass

    @property
    @abstractmethod
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embeddings."""
        pass

    @abstractmethod
    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate text completion.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            temperature: Sampling temperature (0-2)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional provider-specific options

        Returns:
            LLMResponse with generated text
        """
        pass

    def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> Generator[str, None, None]:
        """Generate text completion with streaming.

        Default implementation falls back to non-streaming.
        Override in subclasses for true streaming support.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens
            **kwargs: Additional options

        Yields:
            Text chunks as they're generated
        """
        response = self.generate(
            prompt, system_prompt, temperature, max_tokens, **kwargs
        )
        yield response.content

    def embed(self, text: str) -> EmbeddingResponse:
        """Generate embedding for text.

        Args:
            text: Text to embed

        Returns:
            EmbeddingResponse with embedding vector

        Raises:
            NotImplementedError: If provider doesn't support embeddings
        """
        raise NotImplementedError(
            f"Provider {self.name} does not support embeddings"
        )

    def embed_batch(self, texts: List[str]) -> List[EmbeddingResponse]:
        """Generate embeddings for multiple texts.

        Default implementation calls embed() for each text.
        Override for batch optimization.

        Args:
            texts: List of texts to embed

        Returns:
            List of EmbeddingResponses
        """
        return [self.embed(text) for text in texts]

    @abstractmethod
    def health_check(self) -> bool:
        """Check if provider is available and working.

        Returns:
            True if provider is healthy
        """
        pass

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost for a request.

        Args:
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens

        Returns:
            Estimated cost in USD
        """
        # Default: no cost (for local providers)
        return 0.0


class ProviderError(Exception):
    """Base exception for provider errors."""

    pass


class ProviderUnavailableError(ProviderError):
    """Raised when provider is not available."""

    pass


class ProviderAuthError(ProviderError):
    """Raised when authentication fails."""

    pass


class ProviderRateLimitError(ProviderError):
    """Raised when rate limit is hit."""

    pass


class NoProviderAvailableError(ProviderError):
    """Raised when no provider is available."""

    pass
