"""Migrate4 ``llm_priority_queue`` package — circuit-breaker wrapper.

The legacy single-file priority queue lives at ``memory.llm_priority_queue``
in the mothership. This migrate4 package adds resilience layers on top
without modifying it.
"""
from .circuit_breaker import (
    COUNTERS,
    BreakerResult,
    BreakerState,
    CircuitBreaker,
    get_counters,
    get_default_breaker,
)

__all__ = [
    "BreakerResult",
    "BreakerState",
    "CircuitBreaker",
    "COUNTERS",
    "get_counters",
    "get_default_breaker",
]
