#!/usr/bin/env python3
"""
Injection Metrics Recording — called from persistent_hook_structure.py

Thin wrapper around redis_cache.py sorted-set metrics functions.
Provides a clean interface for recording per-section and total injection latency.

Usage (from persistent_hook_structure.py):
    from memory.injection_metrics import record_section_time, record_total_time

    # After each section:
    record_section_time(1, "foundation", elapsed_ms, injection_id)

    # After complete injection:
    record_total_time(total_ms, tier="critical")

All calls gracefully degrade if Redis is unavailable (no exceptions raised).
"""

from memory.redis_cache import record_section_latency, record_injection_total


def record_section_time(section_num: int, section_name: str, elapsed_ms: float, injection_id: str = ""):
    """Record timing for a specific webhook section.

    Args:
        section_num: Section number (0-8)
        section_name: Human-readable section name (e.g., "foundation", "wisdom")
        elapsed_ms: Time taken in milliseconds
        injection_id: Optional injection correlation ID for tracing
    """
    record_section_latency(f"s{section_num}_{section_name}", elapsed_ms, injection_id)


def record_total_time(elapsed_ms: float, tier: str = "unknown"):
    """Record total injection time, bucketed by risk tier.

    Args:
        elapsed_ms: Total injection time in milliseconds
        tier: Risk tier classification (e.g., "critical", "high", "medium", "low")
    """
    record_injection_total(elapsed_ms, tier)
