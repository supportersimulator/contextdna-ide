"""QueryAdapter implementations for 3-Surgeons -> ContextDNA IDE integration.

Bridges the open-source 3-Surgeons LLMProvider to ContextDNA's priority queue
GPU scheduling, Redis-backed state, and hybrid routing (local + external fallback).

Usage:
    from context_dna.adapters import priority_queue_adapter
    from three_surgeons.core.models import LLMProvider

    provider = LLMProvider(config, query_adapter=priority_queue_adapter)

Known limitations:
    - temperature: The priority queue uses profile-based temperature (e.g. classify=0.2,
      deep=0.5). Caller-supplied temperature is logged but not forwarded. If precise
      temperature control is needed, use the raw HTTP path (no adapter).
    - tokens_in/tokens_out/cost_usd: Not surfaced in LLMResponse (llm_generate returns
      str only). Costs are tracked server-side in Redis llm:costs:{date}.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from three_surgeons.core.models import LLMResponse

logger = logging.getLogger("context_dna.adapters")

# Ensure superrepo root is on PYTHONPATH for memory.llm_priority_queue import
_SUPERREPO_ROOT = os.environ.get(
    "REPO_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )))),
)
if not os.path.isdir(os.path.join(_SUPERREPO_ROOT, "memory")):
    logger.warning(
        f"SUPERREPO_ROOT={_SUPERREPO_ROOT} has no memory/ dir. "
        "Set REPO_ROOT env var to the superrepo root."
    )
if _SUPERREPO_ROOT not in sys.path:
    sys.path.insert(0, _SUPERREPO_ROOT)


# Cached lazy imports — resolved once, reused on every call
_llm_generate = None
_Priority = None
_LLMResponse = None


def _get_queue():
    """Lazy import + cache for llm_priority_queue."""
    global _llm_generate, _Priority
    if _llm_generate is None:
        from memory.llm_priority_queue import llm_generate, Priority
        _llm_generate, _Priority = llm_generate, Priority
    return _llm_generate, _Priority


def _get_llm_response():
    """Lazy import + cache for LLMResponse."""
    global _LLMResponse
    if _LLMResponse is None:
        from three_surgeons.core.models import LLMResponse
        _LLMResponse = LLMResponse
    return _LLMResponse


# Profile mapping: max_tokens -> best-fit profile for priority queue token budgets.
# Maps to the SMALLEST profile whose budget >= requested max_tokens (never truncates).
# Profiles carry their own temperature (see llm_priority_queue.py).
# Think-mode: model decides naturally — never forced into /think or /no_think.
# Only uses general-purpose profiles -- avoids s2_professor/s8_synaptic which carry
# webhook-specific directives that would interfere with surgeon reasoning.
_TOKEN_TO_PROFILE = [
    (64, "classify"),       # temp=0.2
    (512, "voice"),         # temp=0.6
    (768, "extract"),       # temp=0.3
    (1024, "extract_deep"), # temp=0.4
    (2048, "deep"),         # temp=0.5
]

# Profile default temperatures for mismatch detection
_PROFILE_TEMPS = {
    "classify": 0.2, "voice": 0.6, "extract": 0.3,
    "extract_deep": 0.4, "deep": 0.5,
}


def _pick_profile(max_tokens: int) -> str:
    """Map max_tokens to the smallest profile whose budget >= requested tokens.

    Never maps to a profile with fewer tokens than requested (no truncation).
    Avoids webhook-specific profiles (s2_professor, s8_synaptic) that carry
    think-mode directives incompatible with general surgeon tasks.
    """
    for threshold, profile in _TOKEN_TO_PROFILE:
        if max_tokens <= threshold:
            return profile
    return "deep"


class PriorityQueueAdapter:
    """Routes 3-Surgeon LLM calls through ContextDNA's priority queue.

    - Acquires Redis GPU lock (prevents stampeding on shared GPU)
    - Respects 4-tier priority: P1 AARON > P2 ATLAS > P3 EXTERNAL > P4 BACKGROUND
    - Hybrid routing: local-first with external fallback for eligible profiles
    - Cost tracking via Redis llm:costs:{date}

    Temperature note: The priority queue uses profile-based temperatures.
    Caller-supplied temperature is accepted (protocol compliance) but the
    profile temperature takes precedence. A warning is logged when they differ
    by more than 0.15.
    """

    def __init__(
        self,
        priority: Optional[str] = None,
        caller: str = "3surgeons",
    ):
        self._priority_name = priority or os.environ.get(
            "CONTEXTDNA_LLM_PRIORITY", "ATLAS"
        )
        self._caller = caller

    def __call__(
        self,
        system: str,
        prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        timeout_s: float = 300.0,
    ) -> "LLMResponse":
        """QueryAdapter protocol -- routes through llm_priority_queue.

        Note: timeout_s default (300s) matches the LLMProvider protocol.
        The priority queue may involve queuing delays that raw HTTP does not,
        so the longer default is intentional.
        """
        LLMResponse = _get_llm_response()
        llm_generate, Priority = _get_queue()

        priority = getattr(Priority, self._priority_name, Priority.ATLAS)
        profile = _pick_profile(max_tokens)

        # Log temperature mismatch -- caller thinks they control it but profile does
        profile_temp = _PROFILE_TEMPS.get(profile, 0.7)
        if abs(temperature - profile_temp) > 0.15:
            logger.info(
                f"3surgeons temperature={temperature} differs from "
                f"profile '{profile}' default={profile_temp}. "
                f"Profile temperature takes precedence."
            )

        t0 = time.monotonic()
        try:
            result = llm_generate(
                system_prompt=system,
                user_prompt=prompt,
                priority=priority,
                profile=profile,
                caller=self._caller,
                timeout_s=timeout_s,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)

            if result is None:
                return LLMResponse(
                    ok=False,
                    content="LLM queue returned None (timeout or preempted)",
                    latency_ms=latency_ms,
                    model=f"priority_queue:{profile}",
                )

            return LLMResponse(
                ok=True,
                content=result,
                latency_ms=latency_ms,
                model=f"priority_queue:{profile}",
            )

        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.error(f"PriorityQueueAdapter error: {exc}")
            return LLMResponse(
                ok=False,
                content=f"Priority queue error: {exc}",
                latency_ms=latency_ms,
                model=f"priority_queue:{profile}",
            )


# Singleton instance -- the import path referenced in docs and README:
#   from context_dna.adapters import priority_queue_adapter
priority_queue_adapter = PriorityQueueAdapter()


def create_adapter(
    priority: str = "ATLAS",
    caller: str = "3surgeons",
) -> PriorityQueueAdapter:
    """Create a PriorityQueueAdapter with custom priority and caller name."""
    return PriorityQueueAdapter(priority=priority, caller=caller)
