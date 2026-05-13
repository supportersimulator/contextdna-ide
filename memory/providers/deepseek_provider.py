"""
DeepSeek Provider — Async HTTP client for cheap swarm agents.

DeepSeek V3.x offers frontier-class reasoning at commodity prices:
  deepseek-chat:     $0.28/1M input, $0.42/1M output (default)
  deepseek-reasoner: $0.55/1M input, $2.19/1M output (CoT reasoning)

Uses OpenAI-compatible API at https://api.deepseek.com/v1/chat/completions.

Usage:
    from memory.providers.deepseek_provider import DeepSeekProvider

    provider = DeepSeekProvider()
    result = await provider.generate([
        {"role": "system", "content": "You are a code archaeologist."},
        {"role": "user", "content": "Find all error handling in auth.py"},
    ])
    print(result["content"])
    print(f"Cost: ${result['cost_estimate']:.6f}")

Swarm Usage:
    from memory.providers.deepseek_provider import (
        DeepSeekProvider, SwarmAgentRole, create_swarm_injection
    )

    messages = create_swarm_injection(
        role=SwarmAgentRole.CODE_ARCHAEOLOGIST,
        task="Find all retry logic in the codebase",
        context_payload={"file_tree": [...], "conventions": {...}},
    )
    result = await provider.generate(messages)

Created: February 10, 2026
Purpose: Cheap swarm agent inference ($0.28/1M input)
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

logger = logging.getLogger("context_dna.providers.deepseek")

# --- Pricing (USD per 1M tokens) ---
DEEPSEEK_PRICING = {
    "deepseek-chat": {"input": 0.28, "output": 0.42},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
}

DEFAULT_MODEL = "deepseek-chat"
API_BASE = "https://api.deepseek.com/v1"


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    model: str = DEFAULT_MODEL,
) -> float:
    """Estimate cost in USD for a given token count.

    Args:
        input_tokens: Number of input/prompt tokens.
        output_tokens: Number of output/completion tokens.
        model: Model name (deepseek-chat or deepseek-reasoner).

    Returns:
        Estimated cost in USD.
    """
    pricing = DEEPSEEK_PRICING.get(model, DEEPSEEK_PRICING[DEFAULT_MODEL])
    return (input_tokens / 1_000_000) * pricing["input"] + \
           (output_tokens / 1_000_000) * pricing["output"]


# ---------------------------------------------------------------------------
# Swarm Agent Roles
# ---------------------------------------------------------------------------

class SwarmAgentRole(enum.Enum):
    """Predefined roles for the swarm pattern."""
    CODE_ARCHAEOLOGIST = "Find relevant code locations"
    PATCH_DRAFTER = "Propose code diffs"
    TEST_WRITER = "Generate test scaffolds"
    RISK_REVIEWER = "Review for security/risk issues"
    PERFORMANCE_REVIEWER = "Review for performance/edge cases"


# Per-role output contracts — tells the agent exactly what shape to return.
_ROLE_OUTPUT_CONTRACTS: Dict[SwarmAgentRole, str] = {
    SwarmAgentRole.CODE_ARCHAEOLOGIST: (
        "Return a JSON array of objects: "
        '[{"file": "<path>", "line": <n>, "symbol": "<name>", "relevance": "<why>"}]'
    ),
    SwarmAgentRole.PATCH_DRAFTER: (
        "Return unified diff blocks (```diff ... ```) with file paths. "
        "Each hunk must include 3 lines of context."
    ),
    SwarmAgentRole.TEST_WRITER: (
        "Return complete test file content in a fenced code block. "
        "Use pytest conventions. Include imports."
    ),
    SwarmAgentRole.RISK_REVIEWER: (
        "Return a JSON object: "
        '{"risks": [{"severity": "high|medium|low", "location": "<file:line>", '
        '"description": "<what>", "recommendation": "<fix>"}]}'
    ),
    SwarmAgentRole.PERFORMANCE_REVIEWER: (
        "Return a JSON object: "
        '{"issues": [{"type": "perf|edge_case|memory", "location": "<file:line>", '
        '"description": "<what>", "suggestion": "<fix>"}]}'
    ),
}

# Per-role invariant rules — things the agent must NEVER violate.
_ROLE_RULES: Dict[SwarmAgentRole, str] = {
    SwarmAgentRole.CODE_ARCHAEOLOGIST: (
        "- NEVER modify code. Read-only analysis.\n"
        "- NEVER fabricate file paths. Only report files you can confirm exist.\n"
        "- Prefer exact symbol names over descriptions."
    ),
    SwarmAgentRole.PATCH_DRAFTER: (
        "- NEVER remove existing tests or error handling.\n"
        "- NEVER introduce new dependencies without explicit mention.\n"
        "- Preserve existing code style and naming conventions."
    ),
    SwarmAgentRole.TEST_WRITER: (
        "- NEVER import from test files into production code.\n"
        "- NEVER use real API keys, secrets, or external services.\n"
        "- Use fixtures and mocks for external dependencies."
    ),
    SwarmAgentRole.RISK_REVIEWER: (
        "- NEVER dismiss a potential vulnerability without justification.\n"
        "- Flag ALL hardcoded secrets, even if they look like placeholders.\n"
        "- Consider both authenticated and unauthenticated attack vectors."
    ),
    SwarmAgentRole.PERFORMANCE_REVIEWER: (
        "- NEVER suggest premature optimization without evidence.\n"
        "- Consider both hot path and cold start scenarios.\n"
        "- Flag unbounded growth (lists, caches, connections)."
    ),
}


def create_swarm_injection(
    role: SwarmAgentRole,
    task: str,
    context_payload: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    token_budget: int = 2000,
) -> List[Dict[str, str]]:
    """Create the message list for a swarm agent.

    Follows the 8-section injection template:
      1. RULES           -- invariants, never-break constraints
      2. FACTS           -- system map, conventions
      3. YOUR_TASK       -- role assignment + output contract
      4. CODEBASE_CONTEXT -- file tree, symbols (from context_payload)
      5. ARCHITECTURE_CONTEXT -- decisions, patterns (from context_payload)
      6. MID_TASK_INJECTION -- empty initially (filled during execution)
      7. HARMONIZER_GATE -- self-check checklist before returning
      8. METADATA        -- run_id, agent_id, model, token budget

    Args:
        role: SwarmAgentRole defining agent purpose.
        task: Natural-language task description.
        context_payload: Dict with optional keys:
            file_tree, symbols, conventions, architecture_decisions,
            patterns, related_learnings.
        run_id: Unique run identifier (auto-generated if None).
        agent_id: Agent identifier (auto-generated from role if None).
        model: Model to record in metadata.
        token_budget: Max output tokens to record in metadata.

    Returns:
        OpenAI-compatible messages array (system + user).
    """
    ctx = context_payload or {}
    run_id = run_id or uuid.uuid4().hex[:12]
    agent_id = agent_id or f"swarm-{role.name.lower()}-{run_id[:6]}"

    rules = _ROLE_RULES.get(role, "Follow best practices.")
    output_contract = _ROLE_OUTPUT_CONTRACTS.get(role, "Return your analysis as structured text.")

    # --- Build system prompt from 8 sections ---
    sections = []

    # 1. RULES
    sections.append(
        "## 1. RULES (NEVER VIOLATE)\n"
        f"{rules}\n"
        "- Stay within your assigned role. Do not perform tasks outside your scope.\n"
        "- Be concise. Every token must earn its place."
    )

    # 2. FACTS
    conventions = ctx.get("conventions", "Not provided.")
    if isinstance(conventions, dict):
        conventions = "\n".join(f"- {k}: {v}" for k, v in conventions.items())
    sections.append(
        "## 2. FACTS\n"
        f"Codebase conventions:\n{conventions}"
    )

    # 3. YOUR_TASK
    sections.append(
        f"## 3. YOUR_TASK\n"
        f"Role: {role.value}\n"
        f"Task: {task}\n\n"
        f"Output contract:\n{output_contract}"
    )

    # 4. CODEBASE_CONTEXT
    file_tree = ctx.get("file_tree", "Not provided.")
    symbols = ctx.get("symbols", "Not provided.")
    if isinstance(file_tree, list):
        file_tree = "\n".join(f"  {f}" for f in file_tree)
    if isinstance(symbols, list):
        symbols = "\n".join(f"  {s}" for s in symbols)
    sections.append(
        f"## 4. CODEBASE_CONTEXT\n"
        f"File tree:\n{file_tree}\n\n"
        f"Symbols:\n{symbols}"
    )

    # 5. ARCHITECTURE_CONTEXT
    arch_decisions = ctx.get("architecture_decisions", "Not provided.")
    patterns = ctx.get("patterns", "Not provided.")
    if isinstance(arch_decisions, list):
        arch_decisions = "\n".join(f"- {d}" for d in arch_decisions)
    if isinstance(patterns, list):
        patterns = "\n".join(f"- {p}" for p in patterns)
    sections.append(
        f"## 5. ARCHITECTURE_CONTEXT\n"
        f"Decisions:\n{arch_decisions}\n\n"
        f"Patterns:\n{patterns}"
    )

    # 6. MID_TASK_INJECTION (empty — filled during execution by orchestrator)
    sections.append(
        "## 6. MID_TASK_INJECTION\n"
        "(None — will be injected if needed during multi-turn execution.)"
    )

    # 7. HARMONIZER_GATE
    sections.append(
        "## 7. HARMONIZER_GATE (Self-check before returning)\n"
        "Before you return your response, verify:\n"
        "- [ ] Output matches the contract format exactly\n"
        "- [ ] No hallucinated file paths or symbols\n"
        "- [ ] No rule violations\n"
        "- [ ] Response is within token budget\n"
        "- [ ] Task is fully addressed (not partial)"
    )

    # 8. METADATA
    sections.append(
        f"## 8. METADATA\n"
        f"run_id: {run_id}\n"
        f"agent_id: {agent_id}\n"
        f"model: {model}\n"
        f"token_budget: {token_budget}\n"
        f"role: {role.name}"
    )

    system_prompt = "\n\n".join(sections)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]


# ---------------------------------------------------------------------------
# DeepSeek Provider
# ---------------------------------------------------------------------------

class DeepSeekProvider:
    """Async HTTP client for DeepSeek API.

    Uses httpx for non-blocking requests. OpenAI-compatible endpoint.

    Args:
        api_key: DeepSeek API key. Falls back to Context_DNA_Deepseek env var.
        api_base: Base URL override (default: https://api.deepseek.com/v1).
        timeout: Request timeout in seconds (default: 120).
        max_retries: Number of retry attempts on transient failure (default: 3).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: str = API_BASE,
        timeout: float = 120.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("Context_DNA_Deepseek", "")
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init async HTTP client."""
        if self._client is None or self._client.is_closed:
            if not self.api_key:
                raise ValueError(
                    "DeepSeek API key required. "
                    "Set Context_DNA_Deepseek env var or pass api_key to constructor."
                )
            self._client = httpx.AsyncClient(
                base_url=self.api_base,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def generate(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> Dict[str, Any]:
        """Send a chat completion request to DeepSeek.

        Args:
            messages: OpenAI-compatible messages array.
            model: Model name (default: deepseek-chat).
            max_tokens: Maximum output tokens.
            temperature: Sampling temperature.

        Returns:
            Dict with keys: content, usage, model, cost_estimate, latency_ms, request_id.

        Raises:
            ValueError: If API key is missing.
            httpx.HTTPStatusError: On non-retryable HTTP errors.
        """
        model = model or DEFAULT_MODEL
        client = await self._get_client()

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            start = time.monotonic()
            try:
                resp = await client.post("/chat/completions", json=payload)

                # Rate limit — back off and retry
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", str(2 ** attempt)))
                    logger.warning(
                        f"DeepSeek 429 rate limited, retry after {retry_after}s "
                        f"(attempt {attempt}/{self.max_retries})"
                    )
                    await asyncio.sleep(retry_after)
                    last_error = httpx.HTTPStatusError(
                        f"429 Too Many Requests", request=resp.request, response=resp
                    )
                    continue

                resp.raise_for_status()
                data = resp.json()
                latency_ms = int((time.monotonic() - start) * 1000)

                content = ""
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")

                usage_raw = data.get("usage", {})
                input_tokens = usage_raw.get("prompt_tokens", 0)
                output_tokens = usage_raw.get("completion_tokens", 0)

                return {
                    "content": content,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                    "model": data.get("model", model),
                    "cost_estimate": estimate_cost(input_tokens, output_tokens, model),
                    "latency_ms": latency_ms,
                    "request_id": data.get("id", ""),
                }

            except httpx.HTTPStatusError as e:
                latency_ms = int((time.monotonic() - start) * 1000)
                last_error = e
                status = e.response.status_code

                # Don't retry on client errors (except 429 handled above)
                if 400 <= status < 500 and status != 429:
                    logger.error(f"DeepSeek {status} client error (no retry): {e}")
                    raise

                # Server errors — retry with backoff
                delay = 2 ** attempt
                logger.warning(
                    f"DeepSeek {status} server error, retry in {delay}s "
                    f"(attempt {attempt}/{self.max_retries})"
                )
                await asyncio.sleep(delay)

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
                latency_ms = int((time.monotonic() - start) * 1000)
                last_error = e
                delay = 2 ** attempt
                logger.warning(
                    f"DeepSeek connection error, retry in {delay}s "
                    f"(attempt {attempt}/{self.max_retries}): {e}"
                )
                await asyncio.sleep(delay)

        # All retries exhausted
        raise RuntimeError(
            f"DeepSeek API failed after {self.max_retries} attempts. "
            f"Last error: {last_error}"
        )

    async def generate_stream(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream a chat completion response from DeepSeek.

        Yields dicts with keys: content (delta text), done (bool).
        Final chunk includes: usage, model, cost_estimate, latency_ms.

        Args:
            messages: OpenAI-compatible messages array.
            model: Model name (default: deepseek-chat).
            max_tokens: Maximum output tokens.
            temperature: Sampling temperature.

        Yields:
            Dict with incremental content and metadata on final chunk.
        """
        model = model or DEFAULT_MODEL
        client = await self._get_client()

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            start = time.monotonic()
            try:
                async with client.stream("POST", "/chat/completions", json=payload) as resp:
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("Retry-After", str(2 ** attempt)))
                        logger.warning(f"DeepSeek stream 429, retry after {retry_after}s")
                        await asyncio.sleep(retry_after)
                        last_error = Exception("429 rate limited")
                        continue

                    resp.raise_for_status()

                    usage_data = {}
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break

                        import json
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        # Capture usage from final chunk
                        if "usage" in chunk and chunk["usage"]:
                            usage_data = chunk["usage"]

                        choices = chunk.get("choices", [])
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        finish = choices[0].get("finish_reason")

                        if content:
                            yield {"content": content, "done": False}

                        if finish:
                            latency_ms = int((time.monotonic() - start) * 1000)
                            input_tokens = usage_data.get("prompt_tokens", 0)
                            output_tokens = usage_data.get("completion_tokens", 0)
                            yield {
                                "content": "",
                                "done": True,
                                "usage": {
                                    "input_tokens": input_tokens,
                                    "output_tokens": output_tokens,
                                },
                                "model": chunk.get("model", model),
                                "cost_estimate": estimate_cost(
                                    input_tokens, output_tokens, model
                                ),
                                "latency_ms": latency_ms,
                            }
                    return  # Success — exit retry loop

            except httpx.HTTPStatusError as e:
                last_error = e
                if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                    raise
                await asyncio.sleep(2 ** attempt)

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
                last_error = e
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(
            f"DeepSeek stream failed after {self.max_retries} attempts. "
            f"Last error: {last_error}"
        )

    async def __aenter__(self) -> DeepSeekProvider:
        """Support async context manager."""
        await self._get_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    async def _test():
        if not os.environ.get("Context_DNA_Deepseek"):
            print("Set Context_DNA_Deepseek to run smoke test.")
            sys.exit(1)

        provider = DeepSeekProvider()
        try:
            # Test basic generation
            result = await provider.generate(
                messages=[
                    {"role": "system", "content": "Reply in exactly 5 words."},
                    {"role": "user", "content": "Hello."},
                ],
                max_tokens=50,
            )
            print(f"Response: {result['content']}")
            print(f"Usage: {result['usage']}")
            print(f"Cost: ${result['cost_estimate']:.6f}")
            print(f"Latency: {result['latency_ms']}ms")

            # Test swarm injection
            messages = create_swarm_injection(
                role=SwarmAgentRole.CODE_ARCHAEOLOGIST,
                task="Find all error handling patterns",
                context_payload={"conventions": {"lang": "Python", "style": "PEP 8"}},
            )
            print(f"\nSwarm injection sections: {len(messages)} messages")
            print(f"System prompt length: {len(messages[0]['content'])} chars")
        finally:
            await provider.close()

    asyncio.run(_test())
