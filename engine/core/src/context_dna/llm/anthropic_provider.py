"""Anthropic Provider Implementation for Context DNA.

Supports:
- Text generation (claude-3.5-sonnet, claude-3-opus, etc.)
- Streaming responses
- No native embeddings (falls back to OpenAI)
"""

import os
from typing import Optional, Generator, Any

try:
    import requests
except ImportError:
    requests = None

from context_dna.llm.base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderUnavailableError,
    ProviderAuthError,
    ProviderRateLimitError,
)
from context_dna.llm.providers import PROVIDERS


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude API provider.

    Supports Claude 3.5 Sonnet, Claude 3 Opus, and other Claude models.
    Does NOT support embeddings - use OpenAI or Ollama for embeddings.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """Initialize Anthropic provider.

        Args:
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
            model: Model to use (defaults to claude-3-5-sonnet-20241022)
        """
        if requests is None:
            raise ImportError(
                "requests package required for Anthropic provider. "
                "Install with: pip install requests"
            )

        config = PROVIDERS["anthropic"]
        self._api_key = api_key or os.getenv(config.api_key_env)
        self._model = model or config.model
        self._endpoint = config.endpoint
        self._cost_per_1k = config.cost_per_1k_tokens
        self._max_tokens = config.max_context

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def supports_embeddings(self) -> bool:
        return False  # Anthropic doesn't have an embeddings API

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate text using Anthropic API."""
        if not self._api_key:
            raise ProviderAuthError("Anthropic API key not configured")

        try:
            payload = {
                "model": self._model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
            }

            if system_prompt:
                payload["system"] = system_prompt

            # Add any additional kwargs
            payload.update(kwargs)

            response = requests.post(
                f"{self._endpoint}/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )

            if response.status_code == 401:
                raise ProviderAuthError("Invalid Anthropic API key")
            elif response.status_code == 429:
                raise ProviderRateLimitError("Anthropic rate limit exceeded")
            elif response.status_code != 200:
                raise ProviderUnavailableError(
                    f"Anthropic API error: {response.status_code} - {response.text}"
                )

            data = response.json()
            usage = data.get("usage", {})

            # Extract text content from response
            content = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")

            return LLMResponse(
                content=content,
                model=self._model,
                provider=self.name,
                usage={
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": (
                        usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                    ),
                },
                cost=self.estimate_cost(
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                ),
            )

        except requests.RequestException as e:
            raise ProviderUnavailableError(f"Anthropic API request failed: {e}")

    def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> Generator[str, None, None]:
        """Generate text with streaming."""
        if not self._api_key:
            raise ProviderAuthError("Anthropic API key not configured")

        try:
            payload = {
                "model": self._model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "stream": True,
            }

            if system_prompt:
                payload["system"] = system_prompt

            response = requests.post(
                f"{self._endpoint}/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
                stream=True,
                timeout=120,
            )

            if response.status_code != 200:
                raise ProviderUnavailableError(
                    f"Anthropic API error: {response.status_code}"
                )

            import json

            for line in response.iter_lines():
                if line:
                    line = line.decode("utf-8")
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip():
                            try:
                                data = json.loads(data_str)
                                if data.get("type") == "content_block_delta":
                                    delta = data.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        yield delta.get("text", "")
                            except json.JSONDecodeError:
                                continue

        except requests.RequestException as e:
            raise ProviderUnavailableError(f"Anthropic streaming failed: {e}")

    def health_check(self) -> bool:
        """Check if Anthropic API is accessible."""
        if not self._api_key:
            return False

        try:
            # Use a minimal request to check connectivity
            response = requests.post(
                f"{self._endpoint}/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=10,
            )
            # 200 or 400 (bad request) both indicate the API is reachable
            return response.status_code in [200, 400]
        except requests.RequestException:
            return False

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost for a request."""
        # Claude 3.5 Sonnet pricing (as of 2024)
        input_cost = (input_tokens / 1000) * 0.003
        output_cost = (output_tokens / 1000) * 0.015
        return input_cost + output_cost
