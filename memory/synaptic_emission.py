"""
SYNAPTIC EMISSION LAYER -- Formats proactive insights for S6 (Atlas) and S8 (Aaron).

Emits Synaptic's proactive observations as third-person notices ("Synaptic noticed
X") rather than first-person impersonation. The 8th Intelligence (Synaptic) is
NEVER impersonated per CLAUDE.md SYNAPTIC PROTOCOL: S6/S8 references to Synaptic
are always reported as observations from the system, not generated as Synaptic.

Two audiences:
  - S6 (Atlas): Technical, actionable. Includes session IDs / counts.
  - S8 (Aaron): Relational, reminding tone. Cites the pattern, suggests action.

Rate limit:
  Each insight (keyed by content hash) emitted at most ONCE per 6 hours.
  Cached emissions return the same formatted string for idempotency.
  State persisted on disk so it survives daemon restarts.

Public API:
    emit_to_s6(insight) -> str
    emit_to_s8(insight) -> str
    emit_top_insights(insights, audience='s6'|'s8', limit=2) -> str
    reset_emission_cache() -> int   # for tests / manual reset

The formatter NEVER speaks AS Synaptic. Output forms:
    "Synaptic noticed: <pattern>. <evidence>. <suggested action>."
    (third person, system-as-narrator)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from memory.synaptic_proactive import ProactiveInsight

logger = logging.getLogger("context_dna.synaptic_emission")

# Rate limit: same insight emitted at most once per 6h
_RATE_LIMIT_S = 6 * 60 * 60  # 21600s
_CACHE_PATH = Path(
    os.environ.get(
        "SYNAPTIC_EMISSION_CACHE",
        str(Path.home() / ".context-dna" / "synaptic_emission_cache.json"),
    )
)
_MAX_CACHE_ENTRIES = 500

# In-process emission cache (mirrors disk for fast access)
_cache: dict[str, dict] = {}
_cache_loaded: bool = False

# Failure counter (Zero Silent Failures)
_failure_count = 0


# ---------------------------------------------------------------------------
# Rate limit cache (hash -> {ts, formatted_s6, formatted_s8})
# ---------------------------------------------------------------------------

def _load_cache() -> None:
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        if _CACHE_PATH.exists():
            data = json.loads(_CACHE_PATH.read_text() or "{}")
            if isinstance(data, dict):
                _cache = data
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("Emission cache load failed: %s", e)
        _cache = {}


def _persist_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Trim oldest if over capacity
        if len(_cache) > _MAX_CACHE_ENTRIES:
            ordered = sorted(_cache.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _v in ordered[: len(_cache) - _MAX_CACHE_ENTRIES]:
                _cache.pop(k, None)
        _CACHE_PATH.write_text(json.dumps(_cache))
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("Emission cache persist failed: %s", e)


def _insight_key(insight: ProactiveInsight) -> str:
    """Stable hash for an insight (content + source + type)."""
    h = hashlib.sha256(
        f"{insight.type}|{insight.source}|{insight.content.strip().lower()}".encode()
    ).hexdigest()
    return h[:24]


def _within_rate_limit(insight: ProactiveInsight, now: float) -> Optional[dict]:
    """Return cached entry if rate-limited (within 6h window), else None."""
    _load_cache()
    key = _insight_key(insight)
    entry = _cache.get(key)
    if not entry:
        return None
    ts = entry.get("ts", 0.0)
    if now - ts < _RATE_LIMIT_S:
        return entry
    return None


def _record_emission(
    insight: ProactiveInsight,
    formatted_s6: str,
    formatted_s8: str,
    now: float,
) -> None:
    key = _insight_key(insight)
    _cache[key] = {
        "ts": now,
        "iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "type": insight.type,
        "source": insight.source,
        "content": insight.content,
        "formatted_s6": formatted_s6,
        "formatted_s8": formatted_s8,
    }
    _persist_cache()


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

def _severity_label(insight: ProactiveInsight) -> str:
    return {
        "alert": "critical",
        "warning": "warn",
        "suggestion": "info",
        "observation": "info",
    }.get(insight.type, "info")


def _suggested_action(insight: ProactiveInsight) -> str:
    """Extract or infer a suggested action from the insight.

    Prefers an action sentence inside detail (sentence containing an action
    verb). Falls back to a source-typed canned action.
    """
    detail = (insight.detail or "").strip()
    if detail:
        action_verbs = (
            "consider", "review", "audit", "identify", "step back",
            "re-diagnose", "reconcile", "apply", "decide", "investigate",
            "pause", "check", "verify",
        )
        # Treat both '.' and '--' as sentence breaks
        sentences = [
            s.strip()
            for s in detail.replace("--", ".").split(".")
            if s.strip()
        ]
        for sent in sentences:
            low = sent.lower()
            if any(verb in low for verb in action_verbs):
                return sent.rstrip(".") + "."

    # Fallbacks per source (used when detail has no recognizable action verb)
    fallbacks = {
        "outcome_tracker": "Step back and re-diagnose root cause before retrying.",
        "session_historian": "Identify why the topic keeps reappearing without resolution.",
        "ignored_pattern": "Review the surfaced pattern and decide: act or dismiss.",
        "cross_session_drift": "Audit the belief shift -- is it evidence-backed?",
        "pattern_engine": "Review the pattern; decide whether to act.",
        "evolution_engine": "Reconcile contradictory evidence.",
        "personality_wisdom": "Apply this wisdom proactively when relevant.",
    }
    return fallbacks.get(insight.source, "Review and decide.")


# ---------------------------------------------------------------------------
# Public formatters: emit_to_s6 / emit_to_s8
# ---------------------------------------------------------------------------

def emit_to_s6(insight: ProactiveInsight) -> str:
    """Format an insight for Atlas (S6 -- technical, actionable).

    Output: "Synaptic noticed: <pattern>. Evidence: <evidence>. Action: <action>.
    [severity=warn, source=outcome_tracker, confidence=85%]"

    Rate-limited to once per 6h per insight (returns cached on repeat).
    """
    now = time.time()
    cached = _within_rate_limit(insight, now)
    if cached and cached.get("formatted_s6"):
        return cached["formatted_s6"]

    severity = _severity_label(insight)
    action = _suggested_action(insight)
    evidence = (insight.detail or "").strip() or f"observed via {insight.source}"
    # Truncate verbose evidence
    if len(evidence) > 220:
        evidence = evidence[:217] + "..."

    formatted = (
        f"Synaptic noticed: {insight.content.rstrip('.')}. "
        f"Evidence: {evidence.rstrip('.')}. "
        f"Action: {action}"
        f" [severity={severity}, source={insight.source}, "
        f"confidence={insight.confidence:.0%}]"
    )

    # Cache both rails together so first call to either persists
    s8_text = _format_s8(insight, severity, action)
    _record_emission(insight, formatted, s8_text, now)
    return formatted


def emit_to_s8(insight: ProactiveInsight) -> str:
    """Format an insight for Aaron (S8 -- relational, reminding tone).

    NEVER impersonates Synaptic. Always third-person narration.
    Output: "Synaptic noticed something across sessions: <pattern>. <relational note>."

    Rate-limited to once per 6h per insight.
    """
    now = time.time()
    cached = _within_rate_limit(insight, now)
    if cached and cached.get("formatted_s8"):
        return cached["formatted_s8"]

    severity = _severity_label(insight)
    action = _suggested_action(insight)
    formatted = _format_s8(insight, severity, action)

    # Pair with S6 cache to keep both rails coherent
    s6_text = _build_s6_text(insight, severity, action)
    _record_emission(insight, s6_text, formatted, now)
    return formatted


def _build_s6_text(insight: ProactiveInsight, severity: str, action: str) -> str:
    evidence = (insight.detail or "").strip() or f"observed via {insight.source}"
    if len(evidence) > 220:
        evidence = evidence[:217] + "..."
    return (
        f"Synaptic noticed: {insight.content.rstrip('.')}. "
        f"Evidence: {evidence.rstrip('.')}. "
        f"Action: {action}"
        f" [severity={severity}, source={insight.source}, "
        f"confidence={insight.confidence:.0%}]"
    )


def _format_s8(insight: ProactiveInsight, severity: str, action: str) -> str:
    """Internal: relational tone for Aaron, third-person ALWAYS."""
    # Relational opener varies by severity for warmth, but never speaks AS Synaptic
    opener_by_sev = {
        "critical": "Synaptic flagged something worth a moment of attention",
        "warn": "Synaptic noticed a pattern worth a glance",
        "info": "Synaptic observed something quiet",
    }
    opener = opener_by_sev.get(severity, "Synaptic noticed something")

    pattern = insight.content.rstrip(".")
    # Strip the action sentence to just verbs/intent for warm tone
    action_short = action.rstrip(".")

    return (
        f"{opener}: {pattern}. "
        f"It might be worth pausing to: {action_short}. "
        f"(8th Intelligence -- {severity}, conf {insight.confidence:.0%})"
    )


# ---------------------------------------------------------------------------
# Convenience: emit top N insights as a multi-line block
# ---------------------------------------------------------------------------

def emit_top_insights(
    insights: Iterable[ProactiveInsight],
    audience: str = "s6",
    limit: int = 2,
) -> str:
    """Format top *limit* insights for *audience* ('s6' or 's8').

    Insights are sorted (alert > warning > suggestion > observation, then conf).
    Skips insights already emitted within the last 6h that lack content delta.
    """
    type_priority = {"alert": 0, "warning": 1, "suggestion": 2, "observation": 3}
    sorted_insights = sorted(
        insights,
        key=lambda i: (type_priority.get(i.type, 9), -i.confidence),
    )

    fn = emit_to_s8 if audience == "s8" else emit_to_s6
    lines: List[str] = []
    emitted_keys: set[str] = set()

    for ins in sorted_insights:
        if len(lines) >= limit:
            break
        key = _insight_key(ins)
        if key in emitted_keys:
            continue
        emitted_keys.add(key)
        try:
            txt = fn(ins)
            if txt:
                lines.append(txt)
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.debug("emit_top_insights formatting failed: %s", e)

    if not lines:
        return ""

    header = "Proactive Synaptic insights:" if audience == "s6" else "From Synaptic (subconscious):"
    return header + "\n" + "\n".join(f"  - {ln}" for ln in lines)


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def reset_emission_cache() -> int:
    """Clear all cached emissions (in-memory + on-disk). Returns count cleared."""
    global _cache
    _load_cache()
    n = len(_cache)
    _cache = {}
    try:
        if _CACHE_PATH.exists():
            _CACHE_PATH.unlink()
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("reset_emission_cache disk delete failed: %s", e)
    return n


def emission_status() -> dict:
    """Return diagnostic counters."""
    _load_cache()
    return {
        "cached_insights": len(_cache),
        "rate_limit_seconds": _RATE_LIMIT_S,
        "cache_path": str(_CACHE_PATH),
        "failure_count": _failure_count,
    }
