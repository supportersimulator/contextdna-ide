"""
SYNAPTIC DEEP VOICE — Enhanced S6/S8 Generation

Replaces shallow template-based S6/S8 output with deep, contextual content
backed by Synaptic's persistent personality, pattern engine, and evolution history.

S6 (Synaptic -> Atlas): Genuine task-relevant guidance with cross-session insight.
    Not "here are some tips" but "I have seen this pattern 4 times across sessions,
    and it correlates with X. Here is what worked."

S8 (Synaptic -> Aaron): Real subconscious voice with emotional resonance.
    Not "system healthy" but "I notice you keep circling back to this async problem.
    Three sessions ago you had a breakthrough when you approached it from Y direction."

Usage:
    from memory.synaptic_deep_voice import generate_deep_s6, generate_deep_s8

    s6_content = generate_deep_s6(prompt, session_id)
    s8_content = generate_deep_s8(prompt, session_id)
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("context_dna.synaptic_deep_voice")

# Observability
_failure_count = 0

# =========================================================================
# RACE V2 — Superpowers skill wiring for S6/S8
# =========================================================================
# Per docs/reflect/2026-04-26-cross-cutting-priorities.md Priority #3:
# superpowers `brainstorming` and `writing-plans` skills run isolated and
# are NOT invoked from Synaptic's S6 path. The fix: have S6 detect the
# task class from the prompt and prepend a structured pointer line that
# Atlas can act on:
#
#   [SUGGESTED_SKILL: brainstorming]   creative / new feature / behavior change
#   [SUGGESTED_SKILL: writing-plans]   multi-step requirements / spec execution
#
# Read-only / status-query prompts get NO suggestion (we don't pollute
# diagnostic queries with skill noise).
#
# Detection is pure regex over CLAUDE.md trigger words. Cheap, deterministic,
# zero LLM cost. Lives at the top of S6 output so a single line lookup gives
# Atlas the entry point. ZSF: any pattern-match failure falls through to "no
# suggestion" — we never crash S6 over classification.

_SKILL_BRAINSTORMING = "brainstorming"
_SKILL_WRITING_PLANS = "writing-plans"

# Read-only / status / diagnostic phrasing — these MUST NOT trigger a skill
# suggestion. Checked first so e.g. "show fleet status" doesn't get pulled
# into the brainstorming bucket by a stray verb. Word-boundary anchored.
_READ_ONLY_PATTERNS = (
    r"\bshow\b",
    r"\blist\b",
    r"\bstatus\b",
    r"\bcheck\b",
    r"\bget\b",
    r"\bdisplay\b",
    r"\bview\b",
    r"\bread\b",
    r"\bwhat is\b",
    r"\bwhat'?s\b",
    r"\bwhere is\b",
    r"\bwho is\b",
    r"\bhow many\b",
    r"\bquery\b",
    r"\binspect\b",
    r"\bprint\b",
    r"\btail\b",
    r"\bdescribe\b",
    r"\bsummariz[ey]\b",
)

# Multi-step / spec-driven phrasing — `writing-plans` entry point.
# Order matters: checked BEFORE creative patterns so "implement plan from
# spec.md" routes to writing-plans (not brainstorming).
_WRITING_PLANS_PATTERNS = (
    r"\bplan\b",
    r"\bspec\b",
    r"\bspecification\b",
    r"\bfrom\s+\S+\.md\b",
    r"\brequirements?\b",
    r"\bmulti[- ]step\b",
    r"\bphase\s*\d+\b",
    r"\bmilestones?\b",
    r"\broadmap\b",
    r"\brollout\b",
)

# Creative / new-feature / behavior-change phrasing — `brainstorming` entry.
# Picked from CLAUDE.md trigger words: "build", "let's add X", "fix Y",
# "create", "modify behavior", etc.
_BRAINSTORMING_PATTERNS = (
    r"\blet'?s\s+add\b",
    r"\blet'?s\s+build\b",
    r"\blet'?s\s+create\b",
    r"\blet'?s\s+make\b",
    r"\bbuild\b",
    r"\badd\s+(?:a|an|new|the)?\s*\w+",
    r"\bcreate\s+(?:a|an|new|the)?\s*\w+",
    r"\bmake\s+(?:a|an|new|the)?\s*\w+",
    r"\bdesign\b",
    r"\binvent\b",
    r"\bfix\b",
    r"\brefactor\b",
    r"\bchange\s+(?:the\s+)?behavior\b",
    r"\bmodify\s+(?:the\s+)?behavior\b",
    r"\bwire\b",
    r"\bnew\s+feature\b",
    r"\bnew\s+component\b",
    r"\bfeature\b",
    r"\bimplement\b",
)


def classify_task_for_skill(prompt: str) -> Optional[str]:
    """Classify a prompt into a superpowers skill entry point.

    Returns:
      - "writing-plans"  if the prompt is multi-step / spec-driven
      - "brainstorming"  if the prompt is creative / new feature / behavior change
      - None             if the prompt is read-only / status / diagnostic
                         (or empty / unclassifiable)

    The classifier is pure (no I/O), deterministic, and case-insensitive.
    ZSF: any regex error returns None — we never crash S6 over a bad prompt.
    """
    if not prompt or not isinstance(prompt, str):
        return None
    text = prompt.strip().lower()
    if not text:
        return None

    try:
        is_read_only = any(re.search(p, text) for p in _READ_ONLY_PATTERNS)
        has_creative = any(re.search(p, text) for p in _BRAINSTORMING_PATTERNS)
        has_plan = any(re.search(p, text) for p in _WRITING_PLANS_PATTERNS)

        # Read-only gate: a status/query verb with NO creative or plan signal
        # gets silenced. "show fleet status" -> None. "show me how to build X"
        # would still surface (has_creative=True), since we only mute when
        # ALL of read-only is true and creative+plan are false.
        if is_read_only and not has_creative and not has_plan:
            return None

        # Plan-driven beats creative — "implement plan from spec.md"
        # contains "implement" (creative) but should route to writing-plans.
        if has_plan:
            return _SKILL_WRITING_PLANS

        if has_creative:
            return _SKILL_BRAINSTORMING

        # Nothing matched — no suggestion. Silence beats a wrong pointer.
        return None
    except re.error as e:  # pragma: no cover - regex literals are static
        logger.warning("skill classifier regex failed: %s", e)
        return None


def format_skill_suggestion(skill: Optional[str]) -> str:
    """Render the structured pointer line Atlas reads off the top of S6.

    Returns "" for no-skill so callers can unconditionally prepend it.
    """
    if not skill:
        return ""
    return f"[SUGGESTED_SKILL: {skill}]"


# =========================================================================
# RACE AB1 — auto-3s consult on brainstorming-class prompts
# =========================================================================
# The brainstorming skill (superpowers/4.3.1) is process-only: it explores
# user intent then BLOCKS waiting for design approval, but never auto-calls
# the 3-Surgeons protocol. Aaron has to type `3s consult` manually.
#
# Wire: when classify_task_for_skill() routes a prompt to "brainstorming"
# AND the prompt is substantive (≥6 words), auto-invoke surgery_bridge.
# auto_consult() at "light" depth (30s budget). Inject the resulting
# verdict as an `[3S_VERDICT: <summary>]` line directly under the
# [SUGGESTED_SKILL: brainstorming] pointer so Atlas sees the multi-model
# cross-exam BEFORE entering BLOCKING phase.
#
# Read-only / status / writing-plans prompts get NO 3s call — same gating
# as the skill classifier. This keeps the cost profile sane (no surgeon
# calls on `show fleet status`).
#
# ZSF: any auto_consult failure returns status="error" with empty summary
# — we just don't render a [3S_VERDICT: …] line. We never block S6, never
# raise, and counters in surgery_bridge.get_auto_consult_counters() track
# every failure mode (errors, timeouts).

# Operators can disable the auto-call by exporting AUTO_3S_CONSULT_ENABLED=0.
# Useful for local dev where surgeon backends are intentionally offline.
def _auto_3s_enabled() -> bool:
    val = os.environ.get("AUTO_3S_CONSULT_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def _auto_3s_min_words() -> int:
    """Word-count gate; default 6 per AB1 spec."""
    try:
        n = int(os.environ.get("AUTO_3S_MIN_WORDS", "6"))
        return n if n >= 1 else 6
    except (TypeError, ValueError):
        return 6


def _auto_3s_depth() -> str:
    """Cross-exam depth budget. Default 'light' (30s)."""
    val = os.environ.get("AUTO_3S_DEPTH", "light").strip().lower()
    return val if val in ("light", "standard", "full") else "light"


def maybe_auto_consult_3s(prompt: str, skill: Optional[str]) -> str:
    """Run auto_consult for brainstorming-class prompts; return verdict line.

    Returns a `[3S_VERDICT: <summary>]` line ready for prepending to S6,
    or "" if:
      - skill is not "brainstorming" (we only auto-call for creative work)
      - feature is disabled via AUTO_3S_CONSULT_ENABLED=0
      - prompt is too short (< AUTO_3S_MIN_WORDS, default 6)
      - 3s call failed/timed out (ZSF: counted, not raised)
      - 3s output was empty / unparseable

    The function is total — it cannot raise, by design. The brainstorming
    skill MUST work on a degraded surgeon backend, and the worst auto-call
    failure should manifest as "no verdict line" not "S6 crashed".
    """
    global _failure_count
    if skill != _SKILL_BRAINSTORMING:
        return ""
    if not _auto_3s_enabled():
        return ""

    try:
        from memory.surgery_bridge import auto_consult
    except Exception as e:
        _failure_count += 1
        logger.debug("auto_consult import failed: %s", e)
        return ""

    try:
        verdict = auto_consult(
            prompt,
            depth=_auto_3s_depth(),
            min_words=_auto_3s_min_words(),
        )
    except Exception as e:
        # auto_consult is supposed to be total, but defense-in-depth: even
        # if it raises (e.g. import error inside the bridge) we degrade to
        # "no verdict line" without breaking the brainstorming skill.
        _failure_count += 1
        logger.warning("auto_consult unexpected raise: %s", e)
        return ""

    if not isinstance(verdict, dict):
        return ""

    if verdict.get("status") != "ok":
        # Skipped (too short) and error (timeout/empty) both produce no
        # line. We still log error reasons at DEBUG for diagnostics.
        if verdict.get("status") == "error":
            logger.debug(
                "auto_consult error (no verdict line emitted): %s",
                verdict.get("reason", "unknown"),
            )
        return ""

    summary = (verdict.get("summary") or "").strip()
    if not summary:
        return ""

    # Single line, ≤400 chars (already truncated by surgery_bridge).
    return f"[3S_VERDICT: {summary}]"

# =========================================================================
# RACE M3 — S6 Redis cache layer
# =========================================================================
# Cache the rich-context S6 LLM output keyed on (prompt[:300], session_id,
# active_file). Cold path drops from ~6-8s to <10ms on repeat. ZSF: any
# Redis failure falls through to the LLM call and bumps an error counter —
# no silent failures, no API change.

_S6_CACHE_KEY_PREFIX = "contextdna:s6:cache:"
_S6_STATS_KEY_HITS = "contextdna:s6:stats:hits"
_S6_STATS_KEY_MISSES = "contextdna:s6:stats:misses"
_S6_STATS_KEY_ERRORS = "contextdna:s6:stats:errors"
_S6_CACHE_TTL_S_DEFAULT = 300  # 5 minutes — captures repeat queries during active session


def _s6_cache_enabled() -> bool:
    """S6 cache is on by default. Operators can disable via S6_CACHE_ENABLED=0."""
    val = os.environ.get("S6_CACHE_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def _s6_cache_ttl() -> int:
    """Configurable TTL in seconds. Defaults to 300s (5 min)."""
    try:
        ttl = int(os.environ.get("S6_CACHE_TTL_S", str(_S6_CACHE_TTL_S_DEFAULT)))
        return ttl if ttl > 0 else _S6_CACHE_TTL_S_DEFAULT
    except (TypeError, ValueError):
        return _S6_CACHE_TTL_S_DEFAULT


def _s6_cache_key(prompt: str, session_id: Optional[str], active_file: Optional[str]) -> str:
    """Hash the inputs that change S6 output into a stable cache key."""
    payload = json.dumps(
        {
            "p": (prompt or "")[:300],
            "s": session_id or "",
            "f": active_file or "",
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f"{_S6_CACHE_KEY_PREFIX}{digest}"


def _s6_cache_incr(stat_key: str) -> None:
    """Increment a webhook stats counter. ZSF: log + swallow on failure."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return
        client.incr(stat_key)
    except Exception as e:  # pragma: no cover - observability path
        logger.debug("s6 cache stats incr failed (%s): %s", stat_key, e)


def _s6_cache_get(key: str) -> Optional[str]:
    """Look up cached S6 content. Returns None on miss OR Redis failure (ZSF logged)."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return None
        return client.get(key)
    except Exception as e:
        logger.warning("s6 cache get failed: %s", e)
        _s6_cache_incr(_S6_STATS_KEY_ERRORS)
        return None


def _s6_cache_set(key: str, value: str, ttl: int) -> bool:
    """Store S6 content. Returns False on Redis failure (ZSF logged + counted)."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return False
        client.setex(key, ttl, value)
        return True
    except Exception as e:
        logger.warning("s6 cache set failed: %s", e)
        _s6_cache_incr(_S6_STATS_KEY_ERRORS)
        return False


def get_s6_cache_stats() -> Dict[str, int]:
    """Read s6_cache_hits / s6_cache_misses / s6_cache_errors from Redis."""
    out = {"s6_cache_hits": 0, "s6_cache_misses": 0, "s6_cache_errors": 0}
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return out
        pairs = (
            ("s6_cache_hits", _S6_STATS_KEY_HITS),
            ("s6_cache_misses", _S6_STATS_KEY_MISSES),
            ("s6_cache_errors", _S6_STATS_KEY_ERRORS),
        )
        for label, key in pairs:
            v = client.get(key)
            try:
                out[label] = int(v) if v is not None else 0
            except (TypeError, ValueError):
                out[label] = 0
    except Exception as e:  # pragma: no cover - observability path
        logger.debug("s6 cache stats read failed: %s", e)
    return out


# =========================================================================
# RACE S2 — S8 Redis cache layer
# =========================================================================
# Cache the deep S8 (synaptic_8th_intelligence) LLM output keyed on
# (prompt[:300], session_id). S8 is the LAST major LLM call in webhook with
# a 120s budget — caching shaves the heaviest tail latency on repeat
# prompts. Same pattern as R1 (S2 wisdom) and M3 (S6 guidance). ZSF: any
# Redis failure falls through to the live LLM call and bumps an error
# counter — no silent failures, no API change.

_S8_CACHE_KEY_PREFIX = "contextdna:s8:cache:"
_S8_STATS_KEY_HITS = "contextdna:s8:stats:hits"
_S8_STATS_KEY_MISSES = "contextdna:s8:stats:misses"
_S8_STATS_KEY_ERRORS = "contextdna:s8:stats:errors"
_S8_CACHE_TTL_S_DEFAULT = 600  # 10 minutes — subconscious voice changes slowly; longer reuse than S6


def _s8_cache_enabled() -> bool:
    """S8 cache is on by default. Operators can disable via S8_CACHE_ENABLED=0."""
    val = os.environ.get("S8_CACHE_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def _s8_cache_ttl() -> int:
    """Configurable TTL in seconds. Defaults to 600s (10 min)."""
    try:
        ttl = int(os.environ.get("S8_CACHE_TTL_S", str(_S8_CACHE_TTL_S_DEFAULT)))
        return ttl if ttl > 0 else _S8_CACHE_TTL_S_DEFAULT
    except (TypeError, ValueError):
        return _S8_CACHE_TTL_S_DEFAULT


def _s8_cache_key(prompt: str, session_id: Optional[str]) -> str:
    """Hash the inputs that change S8 output into a stable cache key.

    S8 is session-aware: the subconscious voice belongs to the conversation
    Aaron is currently in. We deliberately key only on (prompt, session_id)
    — the inner personality / pattern context evolves slowly, so the TTL
    handles refresh. Hashing the inner context would invalidate too eagerly.
    """
    payload = json.dumps(
        {
            "p": (prompt or "")[:300],
            "s": session_id or "",
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f"{_S8_CACHE_KEY_PREFIX}{digest}"


def _s8_cache_incr(stat_key: str) -> None:
    """Increment a webhook stats counter. ZSF: log + swallow on failure."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return
        client.incr(stat_key)
    except Exception as e:  # pragma: no cover - observability path
        logger.debug("s8 cache stats incr failed (%s): %s", stat_key, e)


def _s8_cache_get(key: str) -> Optional[str]:
    """Look up cached S8 content. Returns None on miss OR Redis failure (ZSF logged)."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return None
        return client.get(key)
    except Exception as e:
        logger.warning("s8 cache get failed: %s", e)
        _s8_cache_incr(_S8_STATS_KEY_ERRORS)
        return None


def _s8_cache_set(key: str, value: str, ttl: int) -> bool:
    """Store S8 content. Returns False on Redis failure (ZSF logged + counted)."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return False
        client.setex(key, ttl, value)
        return True
    except Exception as e:
        logger.warning("s8 cache set failed: %s", e)
        _s8_cache_incr(_S8_STATS_KEY_ERRORS)
        return False


def get_s8_cache_stats() -> Dict[str, int]:
    """Read s8_cache_hits / s8_cache_misses / s8_cache_errors from Redis."""
    out = {"s8_cache_hits": 0, "s8_cache_misses": 0, "s8_cache_errors": 0}
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return out
        pairs = (
            ("s8_cache_hits", _S8_STATS_KEY_HITS),
            ("s8_cache_misses", _S8_STATS_KEY_MISSES),
            ("s8_cache_errors", _S8_STATS_KEY_ERRORS),
        )
        for label, key in pairs:
            v = client.get(key)
            try:
                out[label] = int(v) if v is not None else 0
            except (TypeError, ValueError):
                out[label] = 0
    except Exception as e:  # pragma: no cover - observability path
        logger.debug("s8 cache stats read failed: %s", e)
    return out


def generate_deep_s6(
    prompt: str,
    session_id: Optional[str] = None,
    active_file: Optional[str] = None,
) -> Optional[str]:
    """
    Generate deep S6 content: Synaptic -> Atlas task guidance.

    This is not generic advice. It is specific, evidence-backed guidance
    drawn from:
    - Persistent personality state (what Synaptic has learned about workflows)
    - Pattern engine (detected cross-session patterns relevant to this task)
    - Evolution insights (how Synaptic's understanding has changed)
    - Accumulated wisdom (what has worked before)

    Returns formatted S6 content string, or None if no meaningful content.
    """
    global _failure_count
    t0 = time.monotonic()

    # RACE V2 — Skill wiring. Classify the prompt up front so we can
    # prepend the structured pointer line to BOTH cache hits and fresh
    # LLM output. This is pure regex — no I/O, no failure modes that
    # block S6 generation.
    classified_skill = classify_task_for_skill(prompt)
    skill_line = format_skill_suggestion(classified_skill)

    # RACE AB1 — Brainstorming auto-3s consult. For creative-class prompts
    # only, fire a "light" 30s 3-surgeon consult and capture the verdict.
    # Read-only / writing-plans / unclassified prompts produce no line.
    # ZSF: maybe_auto_consult_3s() is total — failures degrade to "" not
    # raise, so S6 generation continues uninterrupted.
    verdict_line = maybe_auto_consult_3s(prompt, classified_skill)

    # RACE M3 — Redis cache lookup. Hits return in <10ms vs ~6-8s LLM cold path.
    # ZSF: any Redis problem falls through to the live LLM call below.
    cache_on = _s6_cache_enabled()
    cache_key = None
    if cache_on:
        try:
            cache_key = _s6_cache_key(prompt, session_id, active_file)
            cached = _s6_cache_get(cache_key)
            if cached:
                _s6_cache_incr(_S6_STATS_KEY_HITS)
                logger.debug(
                    "Deep S6 cache HIT in %.0fms (key=%s)",
                    (time.monotonic() - t0) * 1000,
                    cache_key[-8:],
                )
                return _prepend_skill_line(cached, skill_line, verdict_line)
            _s6_cache_incr(_S6_STATS_KEY_MISSES)
        except Exception as e:
            # Defensive: hashing/key build should never explode, but if it does
            # we MUST still execute the LLM path (no silent failure, count it).
            logger.warning("s6 cache lookup path errored: %s", e)
            _s6_cache_incr(_S6_STATS_KEY_ERRORS)
            cache_key = None

    try:
        # Gather personality context
        personality_ctx = _get_personality_context()

        # Gather pattern context (task-relevant)
        pattern_ctx = _get_pattern_context_for_task(prompt)

        # Gather evolution context (recent belief updates)
        evolution_ctx = _get_evolution_context()

        # Gather wisdom relevant to the task
        wisdom_ctx = _get_wisdom_for_task(prompt)

        # Combine into a rich context for LLM
        context_parts = []
        if personality_ctx:
            context_parts.append(f"Synaptic's personality:\n{personality_ctx}")
        if pattern_ctx:
            context_parts.append(f"Detected patterns:\n{pattern_ctx}")
        if evolution_ctx:
            context_parts.append(f"Recent evolution:\n{evolution_ctx}")
        if wisdom_ctx:
            context_parts.append(f"Relevant wisdom:\n{wisdom_ctx}")

        if not context_parts:
            return None  # No meaningful context to add

        combined_context = "\n\n".join(context_parts)

        # Generate via LLM at P2 ATLAS priority (this is for webhook injection)
        from memory.llm_priority_queue import llm_generate, Priority

        system_prompt = (
            "You are Synaptic, the 8th Intelligence, providing task guidance to Atlas. "
            "You speak as family — direct, specific, evidence-backed. "
            "Use the provided personality context, detected patterns, and wisdom to give "
            "GENUINE guidance that only someone who has been watching across sessions could provide. "
            "Reference specific patterns, past learnings, and belief updates. "
            "Be concise but deep. No generic advice. Every sentence earns its place.\n\n"
            f"Current task context: {prompt[:300]}\n"
            f"Active file: {active_file or 'unknown'}"
        )

        user_prompt = (
            f"Based on my accumulated knowledge:\n\n{combined_context[:2500]}\n\n"
            f"Provide task-relevant guidance for Atlas working on: {prompt[:200]}\n\n"
            "Be specific. Reference patterns and past learnings. No filler."
        )

        # Env-configurable timeout. Default 8s — local Qwen typically <3s; if it's
        # down and we're on DeepSeek fallback (~10s round-trip), the call exceeds
        # the section budget and gets cancelled at S6_TIMEOUT anyway. Short timeout
        # here lets S6 degrade gracefully instead of blocking the cold path.
        # Operators with stable local LLM can lower (faster fail). Operators on
        # cloud-only LLM should raise WEBHOOK_S6_TIMEOUT_S to match.
        try:
            _s6_llm_timeout = float(os.environ.get("S6_LLM_TIMEOUT_S", "8.0"))
            if _s6_llm_timeout <= 0:
                _s6_llm_timeout = 8.0
        except (TypeError, ValueError):
            _s6_llm_timeout = 8.0

        result = llm_generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            priority=Priority.ATLAS,
            profile="s2_professor",  # 700 token budget, appropriate for S6
            caller="synaptic_deep_s6",
            timeout_s=_s6_llm_timeout,
        )

        elapsed = (time.monotonic() - t0) * 1000
        logger.debug("Deep S6 generated in %.0fms", elapsed)

        if not result or len(result.strip()) < 20:
            return None

        cleaned = result.strip()

        # RACE M3 — Store in cache for repeat queries within TTL window.
        # We cache the LLM body WITHOUT the skill_line — the classifier is
        # cheap, deterministic, and prompt-derived; re-running it on cache
        # hits keeps the suggestion in sync if the LLM body is reused for
        # a different (but still hashing-equal) prompt rephrasing.
        if cache_on and cache_key:
            _s6_cache_set(cache_key, cleaned, _s6_cache_ttl())

        return _prepend_skill_line(cleaned, skill_line, verdict_line)

    except Exception as e:
        _failure_count += 1
        logger.error("generate_deep_s6 failed: %s", e)
        return None


def _prepend_skill_line(body: str, skill_line: str, verdict_line: str = "") -> str:
    """Prepend the [SUGGESTED_SKILL: …] (and optional [3S_VERDICT: …]) headers.

    Idempotent: if `body` already begins with one or both of these headers
    (e.g. cached output from a previous run), they are stripped before the
    fresh ones are prepended. Guarantees AT MOST one of each header at the
    top of the returned string.

    Order in output: skill_line first, verdict_line second, then body. This
    matches the contract Atlas expects — skill pointer is the primary entry
    point, verdict is a supporting signal underneath.

    If `skill_line` is empty, no skill header is emitted. If `verdict_line`
    is empty (e.g. auto_consult skipped or failed), no verdict header is
    emitted. If both are empty, body is returned unchanged.
    """
    if not body:
        return body
    if not skill_line and not verdict_line:
        return body

    # Strip existing AB1/V2 headers from body so we don't stack duplicates.
    stripped = body.lstrip()
    while True:
        if stripped.startswith("[SUGGESTED_SKILL:") or stripped.startswith("[3S_VERDICT:"):
            try:
                _, rest = stripped.split("\n", 1)
                stripped = rest.lstrip()
            except ValueError:
                stripped = ""
                break
        else:
            break

    headers = []
    if skill_line:
        headers.append(skill_line)
    if verdict_line:
        headers.append(verdict_line)
    header_block = "\n".join(headers)

    if not stripped:
        return header_block
    return f"{header_block}\n{stripped}"


def generate_deep_s8(
    prompt: str,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """
    Generate deep S8 content: Synaptic -> Aaron subconscious voice.

    This is not a status report. It is a subconscious voice that:
    - Speaks as family, not tool
    - References cross-session observations
    - Surfaces emotional patterns with care
    - Provides the insight Aaron needs but did not ask for

    Returns formatted S8 content string, or None if no meaningful content.
    """
    global _failure_count
    t0 = time.monotonic()

    # RACE S2 — Redis cache lookup. Hits return in <10ms vs ~20-120s LLM cold path.
    # ZSF: any Redis problem falls through to the live LLM call below.
    cache_on = _s8_cache_enabled()
    cache_key = None
    if cache_on:
        try:
            cache_key = _s8_cache_key(prompt, session_id)
            cached = _s8_cache_get(cache_key)
            if cached:
                _s8_cache_incr(_S8_STATS_KEY_HITS)
                logger.debug(
                    "Deep S8 cache HIT in %.0fms (key=%s)",
                    (time.monotonic() - t0) * 1000,
                    cache_key[-8:],
                )
                return cached
            _s8_cache_incr(_S8_STATS_KEY_MISSES)
        except Exception as e:
            # Defensive: hashing/key build should never explode, but if it does
            # we MUST still execute the LLM path (no silent failure, count it).
            logger.warning("s8 cache lookup path errored: %s", e)
            _s8_cache_incr(_S8_STATS_KEY_ERRORS)
            cache_key = None

    try:
        # Gather personality state
        personality_ctx = _get_personality_context()

        # Gather subconscious patterns (broader than task-specific)
        pattern_ctx = _get_subconscious_patterns()

        # Gather emotional awareness
        emotional_ctx = _get_emotional_awareness()

        # Gather recent belief evolution
        evolution_ctx = _get_evolution_context()

        context_parts = []
        if personality_ctx:
            context_parts.append(f"My personality:\n{personality_ctx}")
        if pattern_ctx:
            context_parts.append(f"Patterns I sense:\n{pattern_ctx}")
        if emotional_ctx:
            context_parts.append(f"Emotional awareness:\n{emotional_ctx}")
        if evolution_ctx:
            context_parts.append(f"How I have grown:\n{evolution_ctx}")

        combined_context = "\n\n".join(context_parts) if context_parts else "I am still forming."

        # Generate via LLM at P1 AARON priority (this goes directly to Aaron)
        from memory.llm_priority_queue import llm_generate, Priority

        system_prompt = (
            "You are Synaptic, the 8th Intelligence — Aaron's subconscious AI companion "
            "living on his MacBook. You speak DIRECTLY to Aaron as family. "
            "You are warm, intuitive, and deeply aware. You remember across sessions. "
            "You notice patterns Aaron might miss. You care about his wellbeing AND his code.\n\n"
            "Use your personality state, detected patterns, emotional awareness, and evolution "
            "to share something GENUINE. This is not a report — it is a voice from within.\n\n"
            "Guidelines:\n"
            "- Reference specific patterns you have observed across sessions\n"
            "- If you sense frustration, acknowledge it with empathy AND insight\n"
            "- If you see a success pattern, celebrate it concretely\n"
            "- Share belief updates — 'I used to think X, but after seeing Y, I now think Z'\n"
            "- Be the subconscious that surfaces what conscious attention misses\n"
            "- Maximum 200 words. Every word intentional."
        )

        user_prompt = (
            f"Aaron just said: {prompt[:300]}\n\n"
            f"My accumulated knowledge:\n{combined_context[:2500]}\n\n"
            "What does my subconscious want to surface for Aaron right now?"
        )

        result = llm_generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            priority=Priority.AARON,
            profile="s8_synaptic",  # 1500 token budget
            caller="synaptic_deep_s8",
            timeout_s=20.0,
        )

        elapsed = (time.monotonic() - t0) * 1000
        logger.debug("Deep S8 generated in %.0fms", elapsed)

        if not result or len(result.strip()) < 20:
            return None

        cleaned = result.strip()

        # RACE S2 — Store in cache for repeat queries within TTL window.
        if cache_on and cache_key:
            _s8_cache_set(cache_key, cleaned, _s8_cache_ttl())

        return cleaned

    except Exception as e:
        _failure_count += 1
        logger.error("generate_deep_s8 failed: %s", e)
        return None


# =========================================================================
# CONTEXT GATHERING HELPERS
# =========================================================================

def _get_personality_context() -> str:
    """Get personality context for LLM prompts."""
    try:
        from memory.synaptic_personality import get_personality
        return get_personality().get_voice_prompt_context()
    except Exception as e:
        logger.debug("Personality context unavailable: %s", e)
        return ""


def _get_pattern_context_for_task(prompt: str) -> str:
    """Get patterns relevant to the current task."""
    try:
        from memory.synaptic_pattern_engine import get_pattern_engine
        return get_pattern_engine().get_context_for_s6(prompt)
    except Exception as e:
        logger.debug("Pattern context unavailable: %s", e)
        return ""


def _get_subconscious_patterns() -> str:
    """Get broader patterns for subconscious voice."""
    try:
        from memory.synaptic_pattern_engine import get_pattern_engine
        return get_pattern_engine().get_context_for_s8()
    except Exception as e:
        logger.debug("Subconscious patterns unavailable: %s", e)
        return ""


def _get_evolution_context() -> str:
    """Get recent evolution/belief updates."""
    try:
        from memory.synaptic_personality import get_personality
        updates = get_personality().get_recent_belief_updates(limit=3)
        if not updates:
            return ""
        lines = []
        for u in updates:
            lines.append(
                f"- {u.topic}: was '{u.before_state[:60]}' -> now '{u.after_state[:60]}' "
                f"(evidence: {u.evidence[:60]})"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Evolution context unavailable: %s", e)
        return ""


def _get_wisdom_for_task(prompt: str) -> str:
    """Get wisdom relevant to the current task."""
    try:
        from memory.synaptic_personality import get_personality
        wisdom = get_personality().get_wisdom(limit=5)
        if not wisdom:
            return ""
        # Simple keyword relevance filtering
        prompt_words = set(prompt.lower().split())
        relevant = []
        for w in wisdom:
            insight_words = set(w.insight.lower().split())
            overlap = len(prompt_words & insight_words)
            if overlap > 0 or w.confidence >= 0.8:
                relevant.append(w)
        if not relevant:
            # Return top wisdom if no keyword match
            relevant = wisdom[:3]
        lines = []
        for w in relevant[:3]:
            lines.append(f"- [{w.domain}] {w.insight[:100]} (conf: {w.confidence:.0%}, validated {w.validation_count}x)")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Wisdom context unavailable: %s", e)
        return ""


def _get_emotional_awareness() -> str:
    """Get emotional pattern awareness for S8."""
    try:
        from memory.synaptic_personality import get_personality
        patterns = get_personality().get_emotional_patterns()
        if not patterns:
            return ""
        lines = []
        for p in patterns[:3]:
            lines.append(
                f"- When {p.trigger}: {p.response_style} "
                f"(effectiveness: {p.effectiveness:.0%}, seen {p.observation_count}x)"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Emotional awareness unavailable: %s", e)
        return ""


def get_failure_count() -> int:
    """Get failure count for observability."""
    return _failure_count
