"""Ollama Provider Implementation for Context DNA.

Supports:
- Local LLM inference (llama3.1, mistral, phi3, etc.)
- Local embeddings (nomic-embed-text, all-minilm, etc.)
- Streaming responses
- Zero cost, fully offline operation
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
)
from context_dna.llm.providers import PROVIDERS


class OllamaProvider(BaseLLMProvider):
    """Ollama local LLM provider.

    Runs LLMs locally using Ollama. Supports:
    - Any Ollama-compatible model (llama3.1, mistral, phi3, etc.)
    - Local embeddings (nomic-embed-text)
    - Zero cost operation
    - Fully offline capable
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        embedding_model: Optional[str] = None,
    ):
        """Initialize Ollama provider.

        Args:
            endpoint: Ollama API endpoint (defaults to http://localhost:11434)
            model: Model to use (defaults to llama3.1:8b-instruct-q4_K_M)
            embedding_model: Embedding model (defaults to nomic-embed-text)
        """
        if requests is None:
            raise ImportError(
                "requests package required for Ollama provider. "
                "Install with: pip install requests"
            )

        config = PROVIDERS["ollama"]
        self._endpoint = endpoint or os.getenv("OLLAMA_HOST", config.endpoint)
        self._model = model or config.model
        self._embedding_model = embedding_model or config.embedding_model

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def supports_embeddings(self) -> bool:
        return self._embedding_model is not None

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate text using Ollama API."""
        try:
            payload = {
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            }

            if system_prompt:
                payload["system"] = system_prompt

            # Add any additional options
            if kwargs.get("options"):
                payload["options"].update(kwargs["options"])

            response = requests.post(
                f"{self._endpoint}/api/generate",
                json=payload,
                timeout=300,  # Longer timeout for local inference
            )

            if response.status_code != 200:
                raise ProviderUnavailableError(
                    f"Ollama API error: {response.status_code} - {response.text}"
                )

            data = response.json()

            # Calculate approximate token counts
            # Ollama provides eval_count (output tokens) and prompt_eval_count (input)
            input_tokens = data.get("prompt_eval_count", 0)
            output_tokens = data.get("eval_count", 0)

            return LLMResponse(
                content=data.get("response", ""),
                model=self._model,
                provider=self.name,
                usage={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "eval_duration_ms": data.get("eval_duration", 0) / 1_000_000,
                },
                cost=0.0,  # Local = free!
            )

        except requests.RequestException as e:
            raise ProviderUnavailableError(f"Ollama API request failed: {e}")

    def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> Generator[str, None, None]:
        """Generate text with streaming."""
        try:
            payload = {
                "model": self._model,
                "prompt": prompt,
                "stream": True,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            }

            if system_prompt:
                payload["system"] = system_prompt

            response = requests.post(
                f"{self._endpoint}/api/generate",
                json=payload,
                stream=True,
                timeout=300,
            )

            if response.status_code != 200:
                raise ProviderUnavailableError(
                    f"Ollama API error: {response.status_code}"
                )

            import json

            for line in response.iter_lines():
                if line:
                    try:
                        data = json.loads(line)
                        if "response" in data:
                            yield data["response"]
                        if data.get("done", False):
                            break
                    except json.JSONDecodeError:
                        continue

        except requests.RequestException as e:
            raise ProviderUnavailableError(f"Ollama streaming failed: {e}")

    def embed(self, text: str) -> EmbeddingResponse:
        """Generate embedding using Ollama API."""
        if not self._embedding_model:
            raise ProviderUnavailableError(
                "No embedding model configured for Ollama"
            )

        try:
            response = requests.post(
                f"{self._endpoint}/api/embeddings",
                json={
                    "model": self._embedding_model,
                    "prompt": text,
                },
                timeout=60,
            )

            if response.status_code != 200:
                raise ProviderUnavailableError(
                    f"Ollama embedding error: {response.status_code} - {response.text}"
                )

            data = response.json()
            embedding = data.get("embedding", [])

            return EmbeddingResponse(
                embedding=embedding,
                model=self._embedding_model,
                provider=self.name,
                dimension=len(embedding),
            )

        except requests.RequestException as e:
            raise ProviderUnavailableError(f"Ollama embedding failed: {e}")

    def embed_batch(self, texts: List[str]) -> List[EmbeddingResponse]:
        """Generate embeddings for multiple texts.

        Note: Ollama doesn't have native batch embedding, so we call
        embed() for each text. Override if Ollama adds batch support.
        """
        return [self.embed(text) for text in texts]

    def health_check(self) -> bool:
        """Check if Ollama is running and accessible."""
        try:
            response = requests.get(
                f"{self._endpoint}/api/tags",
                timeout=5,
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def list_models(self) -> List[str]:
        """List available models in Ollama."""
        try:
            response = requests.get(
                f"{self._endpoint}/api/tags",
                timeout=10,
            )

            if response.status_code == 200:
                data = response.json()
                return [model["name"] for model in data.get("models", [])]
            return []

        except requests.RequestException:
            return []

    def pull_model(self, model_name: str) -> Generator[dict, None, None]:
        """Pull a model from Ollama registry.

        Args:
            model_name: Name of model to pull (e.g., "llama3.1:8b")

        Yields:
            Progress updates as dictionaries
        """
        try:
            response = requests.post(
                f"{self._endpoint}/api/pull",
                json={"name": model_name, "stream": True},
                stream=True,
                timeout=3600,  # 1 hour timeout for large models
            )

            if response.status_code != 200:
                raise ProviderUnavailableError(
                    f"Failed to pull model: {response.status_code}"
                )

            import json

            for line in response.iter_lines():
                if line:
                    try:
                        data = json.loads(line)
                        yield data
                    except json.JSONDecodeError:
                        continue

        except requests.RequestException as e:
            raise ProviderUnavailableError(f"Model pull failed: {e}")

    def model_info(self, model_name: str) -> Optional[dict]:
        """Get information about a specific model."""
        try:
            response = requests.post(
                f"{self._endpoint}/api/show",
                json={"name": model_name},
                timeout=10,
            )

            if response.status_code == 200:
                return response.json()
            return None

        except requests.RequestException:
            return None

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Local inference is free!"""
        return 0.0
