"""OpenAI Provider Implementation for Context DNA.

Supports:
- Text generation (gpt-4o-mini, gpt-4o, etc.)
- Embeddings (text-embedding-3-small, text-embedding-3-large)
- Streaming responses
"""

import os
from typing import Optional, Generator, Any, List

try:
    import requests
except ImportError:
    requests = None

from context_dna.llm.base import (
    BaseLLMProvider,
    LLMResponse,
    EmbeddingResponse,
    ProviderUnavailableError,
    ProviderAuthError,
    ProviderRateLimitError,
)
from context_dna.llm.providers import PROVIDERS


class OpenAIProvider(BaseLLMProvider):
    """OpenAI API provider.

    Supports GPT-4, GPT-4o, GPT-4o-mini for text generation
    and text-embedding-3 for embeddings.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        embedding_model: Optional[str] = None,
    ):
        """Initialize OpenAI provider.

        Args:
            api_key: OpenAI API key (defaults to Context_DNA_OPENAI env var)
            model: Model to use (defaults to gpt-4o-mini)
            embedding_model: Embedding model (defaults to text-embedding-3-small)
        """
        if requests is None:
            raise ImportError(
                "requests package required for OpenAI provider. "
                "Install with: pip install requests"
            )

        config = PROVIDERS["openai"]
        self._api_key = api_key or os.getenv(config.api_key_env)
        self._model = model or config.model
        self._embedding_model = embedding_model or config.embedding_model
        self._endpoint = config.endpoint
        self._cost_per_1k = config.cost_per_1k_tokens

    @property
    def name(self) -> str:
        return "openai"

    @property
    def supports_embeddings(self) -> bool:
        return True

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate text using OpenAI API."""
        if not self._api_key:
            raise ProviderAuthError("OpenAI API key not configured")

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            response = requests.post(
                f"{self._endpoint}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    **kwargs,
                },
                timeout=60,
            )

            if response.status_code == 401:
                raise ProviderAuthError("Invalid OpenAI API key")
            elif response.status_code == 429:
                raise ProviderRateLimitError("OpenAI rate limit exceeded")
            elif response.status_code != 200:
                raise ProviderUnavailableError(
                    f"OpenAI API error: {response.status_code} - {response.text}"
                )

            data = response.json()
            usage = data.get("usage", {})

            return LLMResponse(
                content=data["choices"][0]["message"]["content"],
                model=self._model,
                provider=self.name,
                usage={
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
                cost=self.estimate_cost(
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                ),
            )

        except requests.RequestException as e:
            raise ProviderUnavailableError(f"OpenAI API request failed: {e}")

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
            raise ProviderAuthError("OpenAI API key not configured")

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            response = requests.post(
                f"{self._endpoint}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                    **kwargs,
                },
                stream=True,
                timeout=120,
            )

            if response.status_code != 200:
                raise ProviderUnavailableError(
                    f"OpenAI API error: {response.status_code}"
                )

            for line in response.iter_lines():
                if line:
                    line = line.decode("utf-8")
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            import json
                            chunk = json.loads(data)
                            content = chunk["choices"][0].get("delta", {}).get(
                                "content", ""
                            )
                            if content:
                                yield content
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue

        except requests.RequestException as e:
            raise ProviderUnavailableError(f"OpenAI streaming failed: {e}")

    def embed(self, text: str) -> EmbeddingResponse:
        """Generate embedding using OpenAI API."""
        if not self._api_key:
            raise ProviderAuthError("OpenAI API key not configured")

        try:
            response = requests.post(
                f"{self._endpoint}/embeddings",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._embedding_model,
                    "input": text,
                },
                timeout=30,
            )

            if response.status_code == 401:
                raise ProviderAuthError("Invalid OpenAI API key")
            elif response.status_code != 200:
                raise ProviderUnavailableError(
                    f"OpenAI embedding error: {response.status_code}"
                )

            data = response.json()
            embedding = data["data"][0]["embedding"]

            return EmbeddingResponse(
                embedding=embedding,
                model=self._embedding_model,
                provider=self.name,
                dimension=len(embedding),
            )

        except requests.RequestException as e:
            raise ProviderUnavailableError(f"OpenAI embedding failed: {e}")

    def embed_batch(self, texts: List[str]) -> List[EmbeddingResponse]:
        """Generate embeddings for multiple texts (batch optimized)."""
        if not self._api_key:
            raise ProviderAuthError("OpenAI API key not configured")

        try:
            response = requests.post(
                f"{self._endpoint}/embeddings",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._embedding_model,
                    "input": texts,
                },
                timeout=60,
            )

            if response.status_code != 200:
                raise ProviderUnavailableError(
                    f"OpenAI batch embedding error: {response.status_code}"
                )

            data = response.json()
            results = []

            for item in data["data"]:
                results.append(
                    EmbeddingResponse(
                        embedding=item["embedding"],
                        model=self._embedding_model,
                        provider=self.name,
                        dimension=len(item["embedding"]),
                    )
                )

            return results

        except requests.RequestException as e:
            raise ProviderUnavailableError(f"OpenAI batch embedding failed: {e}")

    def health_check(self) -> bool:
        """Check if OpenAI API is accessible."""
        if not self._api_key:
            return False

        try:
            response = requests.get(
                f"{self._endpoint}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=10,
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost for a request."""
        # GPT-4o-mini pricing (as of 2024)
        input_cost = (input_tokens / 1000) * 0.00015
        output_cost = (output_tokens / 1000) * 0.0006
        return input_cost + output_cost
