#!/usr/bin/env python3
"""GG4 — Context-window limit lookup (additive, MavKa-inspired).

PURE-DATA module. No I/O, no LLM calls, no side effects on import.

Purpose
-------
Encode each known model's hard input context window so callers can avoid
silent upstream truncation. Today ContextDNA's profile system caps OUTPUT
tokens (`max_tokens`) but never compares INPUT size against the model's
context window — overflow is silently truncated by the provider. This
module is the typed, lookup-table foundation for an "escalate-instead-of-
truncate" path (FF3 Part D, item 2).

Extension contract
------------------
- Add new models by inserting new entries into ``MODEL_CONTEXT_LIMITS``.
- ``model_context_limit()`` MUST stay backward-compatible: unknown models
  return the conservative ``DEFAULT_CONTEXT_LIMIT`` so callers never crash
  on a typo. Do NOT raise for unknown names — emit a one-shot stderr WARN
  via the parent module's ZSF counter when wired in.
- ``would_overflow()`` does naive ``ceil(chars/4)`` token estimation. If
  you need provider-accurate counting, branch by ``model_name`` rather
  than rewriting this function — keep the simple path simple.
- This module is intentionally dependency-free (stdlib only) so it can be
  imported from any test path without dragging the priority-queue module.

References
----------
- ``.fleet/audits/2026-05-07-FF3-multi-provider-abstraction-diff.md``
- ``.fleet/audits/2026-05-07-GG4-cost-tier-role-class-context-limit.md``
"""
from __future__ import annotations

from typing import Dict


# Conservative default for unknown models (matches the smallest known
# limit in the table so we err on the side of "would_overflow=True").
DEFAULT_CONTEXT_LIMIT: int = 32_768

# Known model context windows (input tokens).
# Sources: vendor docs as of 2026-05-07. Values are conservative — vendor
# may advertise larger windows for paying tiers; we encode the contract
# we actually rely on.
MODEL_CONTEXT_LIMITS: Dict[str, int] = {
    # DeepSeek (cardiologist + ATLAS/AARON external default)
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    # OpenAI (optional fallback for OSS users)
    "gpt-4.1-mini": 128_000,
    "gpt-4.1": 128_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    # Local MLX
    "qwen3-4b": 32_768,
    # Anthropic (Claude family — never invoked by llm_priority_queue today,
    # tracked here so future Claude-via-API path has a typed limit).
    "claude-haiku": 200_000,
    "claude-opus": 200_000,
    "claude-sonnet": 200_000,
}


def model_context_limit(model_name: str) -> int:
    """Return the input-context-window cap (tokens) for ``model_name``.

    Unknown names fall back to ``DEFAULT_CONTEXT_LIMIT`` (conservative).
    Lookup is case-insensitive on the prefix so ``"deepseek-chat"`` and
    ``"DeepSeek-Chat"`` both resolve.
    """
    if not model_name:
        return DEFAULT_CONTEXT_LIMIT
    key = model_name.strip().lower()
    if key in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[key]
    # Prefix match (e.g. "claude-opus-4.7" → "claude-opus")
    for known, limit in MODEL_CONTEXT_LIMITS.items():
        if key.startswith(known):
            return limit
    return DEFAULT_CONTEXT_LIMIT


def would_overflow(prompt_tokens: int, model_name: str) -> bool:
    """Return True if ``prompt_tokens`` exceeds ``model_name``'s window.

    Naive comparison — caller is expected to add expected output tokens
    (i.e. the profile's ``max_tokens``) before calling. This function does
    not double-count.
    """
    if prompt_tokens is None or prompt_tokens < 0:
        return False
    return prompt_tokens > model_context_limit(model_name)


__all__ = [
    "DEFAULT_CONTEXT_LIMIT",
    "MODEL_CONTEXT_LIMITS",
    "model_context_limit",
    "would_overflow",
]
