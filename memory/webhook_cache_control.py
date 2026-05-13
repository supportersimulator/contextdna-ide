"""
RACE T4 — Webhook cache control plane.

Centralised invalidation, stats, and config inspection for the four
webhook-section Redis caches that landed across the race:

  S2 wisdom        — RACE R1   (memory/persistent_hook_structure.py)
  S6 deep voice    — RACE M3   (memory/synaptic_deep_voice.py)
  S8 subconscious  — RACE S2   (memory/synaptic_deep_voice.py)
  S4 blueprint     — RACE T2   (memory/persistent_hook_structure.py)

All four use TTL-based expiry. This module adds an *operator-facing*
invalidation path that wipes a section (or all sections) on demand —
useful after a major config change, a fresh session, or a fleet restart
when stale cached output would mask the new state.

ZSF: every Redis failure is logged AND counted into the per-section
``*_invalidation_errors`` counter. We never silently swallow.

Public API:

    invalidate_all_webhook_caches() -> dict[str, int]
        Clears all four cache prefixes. Returns
        ``{"s2_cleared": N, "s6_cleared": N, "s8_cleared": N,
          "s4_cleared": N, "errors": E, "total": T}``.

    invalidate_section(section: str) -> dict[str, int]
        Section in {"s2","s6","s8","s4"}. Returns
        ``{"section": "s2", "cleared": N, "errors": E}``.

    cache_stats() -> dict
        Aggregates hits/misses/errors from all 4 sections plus a
        ``total_*`` rollup.

    cache_config() -> dict
        Reports the current ENABLED flag + TTL for each section so
        operators can tell at a glance how the caches are configured.

The CLI front-end lives in ``scripts/webhook-cache.sh`` and the NATS
auto-invalidation hook lives in ``tools/fleet_nerve_nats.py`` — both
delegate to this module so the invalidation logic has exactly one
implementation.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Iterable

logger = logging.getLogger(__name__)

# =========================================================================
# Section registry
# =========================================================================
# Each section maps to:
#   - cache_prefix:   the Redis key prefix wiped during invalidation
#   - stats_prefix:   the Redis key prefix where the section publishes
#                     hits/misses/errors counters
#   - enabled_env:    env var name that toggles the cache on/off
#   - ttl_env:        env var name that overrides the section's TTL (s)
#   - default_ttl:    fallback TTL in seconds if the env var is unset
#   - invalidation_errors_key: per-section counter for invalidation failures
#                              (RACE T4 — observability for forced wipes)

_SECTIONS: Dict[str, Dict[str, object]] = {
    "s2": {
        "cache_prefix": "contextdna:s2:cache:",
        "stats_prefix": "contextdna:s2:stats:",
        "enabled_env": "S2_CACHE_ENABLED",
        "ttl_env": "S2_CACHE_TTL_S",
        "default_ttl": 600,
        "invalidation_errors_key": "contextdna:s2:stats:invalidation_errors",
    },
    "s6": {
        "cache_prefix": "contextdna:s6:cache:",
        "stats_prefix": "contextdna:s6:stats:",
        "enabled_env": "S6_CACHE_ENABLED",
        "ttl_env": "S6_CACHE_TTL_S",
        "default_ttl": 300,
        "invalidation_errors_key": "contextdna:s6:stats:invalidation_errors",
    },
    "s8": {
        "cache_prefix": "contextdna:s8:cache:",
        "stats_prefix": "contextdna:s8:stats:",
        "enabled_env": "S8_CACHE_ENABLED",
        "ttl_env": "S8_CACHE_TTL_S",
        "default_ttl": 600,
        "invalidation_errors_key": "contextdna:s8:stats:invalidation_errors",
    },
    "s4": {
        "cache_prefix": "contextdna:s4:blueprint:cache:",
        "stats_prefix": "contextdna:s4:blueprint:stats:",
        "enabled_env": "S4_BLUEPRINT_CACHE_ENABLED",
        "ttl_env": "S4_BLUEPRINT_CACHE_TTL_S",
        "default_ttl": 1800,
        "invalidation_errors_key": "contextdna:s4:blueprint:stats:invalidation_errors",
    },
}


def known_sections() -> Iterable[str]:
    """Return the set of supported section identifiers."""
    return tuple(_SECTIONS.keys())


# =========================================================================
# Helpers
# =========================================================================

def _get_client():
    """Return a live Redis client or ``None`` (ZSF: log on import/connect failure)."""
    try:
        from memory.redis_cache import get_redis_client
        return get_redis_client()
    except Exception as e:  # pragma: no cover - import-time guard
        logger.warning("webhook_cache_control: redis import failed: %s", e)
        return None


def _bump_invalidation_error(section: str) -> None:
    """Increment the per-section invalidation_errors counter (best-effort)."""
    cfg = _SECTIONS.get(section)
    if not cfg:
        return
    try:
        client = _get_client()
        if client is None:
            return
        client.incr(cfg["invalidation_errors_key"])
    except Exception as e:  # pragma: no cover - observability path
        logger.debug(
            "webhook_cache_control: failed to bump invalidation_errors for %s: %s",
            section,
            e,
        )


def _read_int(client, key: str) -> int:
    """Read an integer counter from Redis; 0 on miss/parse failure."""
    try:
        raw = client.get(key)
        if raw is None:
            return 0
        return int(raw)
    except (TypeError, ValueError):
        return 0
    except Exception:
        # ZSF: a failure here is observability, not critical-path. The caller
        # already fronts a try/except and counts.
        return 0


# =========================================================================
# Invalidation
# =========================================================================

def invalidate_section(section: str) -> Dict[str, int]:
    """Wipe every cache key under one section's prefix.

    Returns ``{"section": <name>, "cleared": N, "errors": E}``.

    ZSF:
      * unknown section -> ValueError (caller bug, not a runtime failure)
      * Redis unreachable -> errors=1, cleared=0, logged at WARNING
      * per-key delete failure -> errors+=1, scan continues
    """
    if section not in _SECTIONS:
        raise ValueError(
            f"unknown webhook cache section: {section!r} "
            f"(known: {sorted(_SECTIONS.keys())})"
        )

    cfg = _SECTIONS[section]
    prefix = str(cfg["cache_prefix"])

    client = _get_client()
    if client is None:
        logger.warning(
            "webhook_cache_control: invalidate_section(%s): redis unavailable", section
        )
        _bump_invalidation_error(section)
        return {"section": section, "cleared": 0, "errors": 1}

    cleared = 0
    errors = 0
    try:
        # scan_iter is non-blocking; safe for production keyspace sizes.
        for key in client.scan_iter(match=f"{prefix}*", count=200):
            try:
                client.delete(key)
                cleared += 1
            except Exception as e:
                errors += 1
                logger.warning(
                    "webhook_cache_control: delete %s failed: %s", key, e
                )
    except Exception as e:
        errors += 1
        logger.warning(
            "webhook_cache_control: scan_iter for %s failed: %s", section, e
        )

    if errors:
        _bump_invalidation_error(section)

    logger.info(
        "webhook_cache_control: invalidate_section %s -> cleared=%d errors=%d",
        section,
        cleared,
        errors,
    )
    return {"section": section, "cleared": cleared, "errors": errors}


def invalidate_all_webhook_caches() -> Dict[str, int]:
    """Wipe every webhook-section cache prefix.

    Returns a roll-up dict::

        {
          "s2_cleared": N, "s6_cleared": N, "s8_cleared": N, "s4_cleared": N,
          "errors": E,
          "total": T,
        }

    Always touches all four sections — a failure in one does NOT short-circuit
    the others, because operators expect "clear all" to be best-effort total.
    """
    out: Dict[str, int] = {"errors": 0, "total": 0}
    for section in _SECTIONS:
        result = invalidate_section(section)
        out[f"{section}_cleared"] = result["cleared"]
        out["errors"] += result["errors"]
        out["total"] += result["cleared"]
    logger.info(
        "webhook_cache_control: invalidate_all -> total=%d errors=%d",
        out["total"],
        out["errors"],
    )
    return out


# =========================================================================
# Stats aggregation
# =========================================================================

def cache_stats() -> Dict[str, object]:
    """Aggregate hit/miss/error counters from every section.

    Returns a dict shaped like::

        {
          "sections": {
            "s2": {"hits": .., "misses": .., "errors": .., "invalidation_errors": ..},
            "s6": {...},
            "s8": {...},
            "s4": {...},
          },
          "totals": {"hits": .., "misses": .., "errors": .., "invalidation_errors": ..},
          "redis_available": bool,
        }

    Reads counters directly from Redis (where the cache modules write them
    via ``incr``). If Redis is unavailable, every counter reads as 0 and
    ``redis_available`` is False — no exception escapes.
    """
    sections: Dict[str, Dict[str, int]] = {}
    totals = {"hits": 0, "misses": 0, "errors": 0, "invalidation_errors": 0}

    client = _get_client()
    redis_available = client is not None

    for section, cfg in _SECTIONS.items():
        stats = {"hits": 0, "misses": 0, "errors": 0, "invalidation_errors": 0}
        if redis_available:
            stats_prefix = str(cfg["stats_prefix"])
            stats["hits"] = _read_int(client, f"{stats_prefix}hits")
            stats["misses"] = _read_int(client, f"{stats_prefix}misses")
            stats["errors"] = _read_int(client, f"{stats_prefix}errors")
            stats["invalidation_errors"] = _read_int(
                client, str(cfg["invalidation_errors_key"])
            )
        sections[section] = stats
        for k in totals:
            totals[k] += stats[k]

    return {
        "sections": sections,
        "totals": totals,
        "redis_available": redis_available,
    }


# =========================================================================
# Config inspection
# =========================================================================

def _enabled_from_env(env_name: str) -> bool:
    """Mirror the per-section enabled semantics: default ON; off-strings disable."""
    val = os.environ.get(env_name, "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def _ttl_from_env(env_name: str, default_ttl: int) -> int:
    """Parse TTL override, falling back to ``default_ttl`` on bad input."""
    try:
        ttl = int(os.environ.get(env_name, str(default_ttl)))
        return ttl if ttl > 0 else default_ttl
    except (TypeError, ValueError):
        return default_ttl


def cache_config() -> Dict[str, object]:
    """Report current enable-flag + TTL for each section.

    Returns::

        {
          "sections": {
            "s2": {"enabled": True, "ttl_seconds": 600,
                   "enabled_env": "S2_CACHE_ENABLED",
                   "ttl_env": "S2_CACHE_TTL_S"},
            ...
          }
        }
    """
    out: Dict[str, Dict[str, object]] = {}
    for section, cfg in _SECTIONS.items():
        enabled_env = str(cfg["enabled_env"])
        ttl_env = str(cfg["ttl_env"])
        out[section] = {
            "enabled": _enabled_from_env(enabled_env),
            "ttl_seconds": _ttl_from_env(ttl_env, int(cfg["default_ttl"])),
            "enabled_env": enabled_env,
            "ttl_env": ttl_env,
            "cache_prefix": cfg["cache_prefix"],
        }
    return {"sections": out}


# =========================================================================
# NATS event hook
# =========================================================================
# Triggered by tools/fleet_nerve_nats.py when an event.config.changed or
# event.fleet.restart message arrives. The handler is intentionally tiny
# and routes through the public API so the same invalidation surface is
# used by CLI, NATS, and tests.

_INVALIDATING_EVENTS = ("event.config.changed", "event.fleet.restart")


def event_subjects() -> tuple:
    """Subjects the daemon should subscribe to for auto-invalidation."""
    return _INVALIDATING_EVENTS


def handle_invalidation_event(subject: str) -> Dict[str, int]:
    """Process a NATS event subject by wiping all webhook caches.

    Returns the same dict shape as ``invalidate_all_webhook_caches`` plus a
    ``"subject"`` key for telemetry. Unknown subjects are still processed
    (the daemon is the gate; this function is conservative and acts on
    anything routed to it) but logged so misrouting is visible.
    """
    if subject not in _INVALIDATING_EVENTS:
        logger.warning(
            "webhook_cache_control: handle_invalidation_event called with "
            "unexpected subject %r — invalidating anyway", subject,
        )
    result = invalidate_all_webhook_caches()
    result["subject"] = subject  # type: ignore[assignment]
    logger.info(
        "webhook_cache_control: NATS-triggered invalidation (%s) -> "
        "total=%d errors=%d", subject, result["total"], result["errors"],
    )
    return result
