"""
Webhook Message Builders - Extract message preparation logic

This module extracts the message preparation logic from Section 2 and Section 8
so it can be reused by the batch helper without circular imports.

OPTIMIZATION: Shared context gathering eliminates duplicate SQLite/failure queries
when both S2 and S8 are generated in the same webhook cycle (~50-100ms saved).
"""

from typing import Tuple, Optional, Dict, Any
import logging
import time

logger = logging.getLogger(__name__)


# =============================================================================
# SHARED CONTEXT GATHERING (eliminates duplicate queries)
# =============================================================================

_shared_context_cache: Dict[str, Any] = {}
_shared_context_ts: float = 0.0
_SHARED_CONTEXT_TTL = 5.0  # seconds - fresh per webhook cycle


def _gather_shared_context(prompt: str) -> Dict[str, Any]:
    """
    Gather context shared between Section 2 and Section 8.
    Cached for the duration of a single webhook cycle (~5s TTL).
    
    Eliminates duplicate SQLite FTS5 and failure analyzer queries.
    
    Returns dict with:
        - learnings: list of {title, content} dicts
        - failures: list of landmine strings
        - learnings_text: pre-formatted learnings string
        - failures_text: pre-formatted failures string
    """
    global _shared_context_cache, _shared_context_ts
    
    now = time.monotonic()
    if _shared_context_cache and (now - _shared_context_ts) < _SHARED_CONTEXT_TTL:
        return _shared_context_cache
    
    ctx: Dict[str, Any] = {
        "learnings": [],
        "failures": [],
        "learnings_text": "",
        "failures_text": "",
    }
    
    # 1. Relevant learnings from SQLite FTS5 + semantic rescue (shared, query once)
    try:
        from memory.sqlite_storage import get_sqlite_storage
        local_store = get_sqlite_storage()
        results = local_store.query(prompt[:100], limit=5)
        # Semantic rescue: augment with vector similarity when FTS5 returns sparse results
        if len(results) < 3:
            try:
                from memory.semantic_search import rescue_search
                results = rescue_search(prompt[:200], results, min_results=3, top_k=5)
            except Exception:
                pass  # Graceful degradation — FTS5 results pass through unchanged
        if results:
            for r in results[:5]:
                title = r.get('title', '')
                content_text = r.get('content', r.get('preferences', ''))[:200]
                if title:
                    ctx["learnings"].append({"title": title, "content": content_text})
            if ctx["learnings"]:
                ctx["learnings_text"] = "\n".join(
                    f"- {l['title']}: {l['content']}" for l in ctx["learnings"]
                )
    except Exception:
        pass
    
    # 2. Recent failure patterns (shared, query once)
    try:
        from memory.failure_pattern_analyzer import get_failure_pattern_analyzer
        analyzer = get_failure_pattern_analyzer()
        if analyzer:
            landmines = analyzer.get_landmines_for_task(prompt, limit=3)
            if landmines:
                ctx["failures"] = list(landmines)
                ctx["failures_text"] = "\n".join(f"- {lm}" for lm in landmines)
    except Exception:
        pass
    
    _shared_context_cache = ctx
    _shared_context_ts = now
    return ctx


def check_llm_available() -> bool:
    """Quick LLM liveness check via Redis health cache (no direct HTTP to 5044)."""
    try:
        from memory.llm_priority_queue import check_llm_health
        return check_llm_health()
    except Exception:
        return False


# =============================================================================
# SECTION 2: Professor Wisdom Message Builder
# =============================================================================

def prepare_section_2_messages(prompt: str, config=None) -> Tuple[Optional[str], Optional[str]]:
    """
    Prepare Section 2 (Professor Wisdom) messages for LLM.
    
    Uses shared context to avoid duplicate SQLite/failure queries.
    
    Returns:
        Tuple of (system_prompt, user_prompt) or (None, None) if preparation fails
    """
    try:
        # Check LLM availability
        if not check_llm_available():
            return None, None
        
        # Get shared context (cached per webhook cycle)
        shared = _gather_shared_context(prompt)
        
        # S2-specific: Domain seed
        domain_seed = ""
        try:
            from memory.persistent_hook_structure import detect_domain_from_prompt, get_professor_wisdom_dicts
            detected_domain = detect_domain_from_prompt(prompt)
            if detected_domain:
                _, professor_wisdom = get_professor_wisdom_dicts()
                if professor_wisdom and detected_domain in professor_wisdom:
                    dw = professor_wisdom[detected_domain]
                    domain_seed = dw.get("first_principle", "").strip()[:200]
        except Exception:
            pass
        
        # === Build system prompt ===
        system_prompt = (
            "Senior engineering professor. Be SPECIFIC to the task. Use the context provided.\n"
            "Efficient communication matters - prioritize signal over noise. You may sacrifice perfect grammar for clarity and speed.\n"
            "Typical format works well: THE ONE THING (core insight), LANDMINES (key gotchas), THE PATTERN (approach).\n"
            "But adapt if context requires - comprehensive coverage beats arbitrary constraints when truly needed.\n"
            "No filler. No pleasantries. Reference provided learnings/failures."
        )
        
        # === Build user prompt (uses shared learnings + failures) ===
        user_parts = [f"Task: {prompt[:200]}"]
        
        if shared["learnings_text"]:
            # S2 uses top 3 learnings (concise)
            s2_learnings = "\n".join(
                f"- {l['title']}: {l['content']}" for l in shared["learnings"][:3]
            )
            user_parts.append(f"\nLearnings:\n{s2_learnings[:400]}")
        
        if shared["failures_text"]:
            user_parts.append(f"\nFailures:\n{shared['failures_text'][:300]}")
        
        if domain_seed:
            user_parts.append(f"\nDomain: {domain_seed[:150]}")
        
        user_prompt = "\n".join(user_parts)
        
        # Determine depth (simplified from original logic)
        depth = config.wisdom_depth if (config and hasattr(config, 'wisdom_depth')) else "standard"
        
        if depth == "one_thing_only":
            user_prompt += "\n\nFocus your response: What's ONE key insight or action that matters most here? You can respond however makes sense - just be direct and concise (2-3 sentences ideal, but flexibility is fine)."
        
        # Thinking mode handled centrally by llm_priority_queue (golden era pattern).
        # s2_professor profile → model decides naturally. Priority queue is single source of truth.
        
        return system_prompt, user_prompt
        
    except Exception as e:
        logger.error(f"Section 2 message prep failed: {e}")
        return None, None


# =============================================================================
# SECTION 8: Synaptic 8th Intelligence Message Builder
# =============================================================================

def prepare_section_8_messages(prompt: str, session_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Prepare Section 8 (8th Intelligence) messages for LLM.
    
    Uses shared context to avoid duplicate SQLite/failure queries.
    S8-specific additions: dialogue mirror, brain state, user sentiment.
    
    Returns:
        Tuple of (system_prompt, user_prompt) or (None, None) if preparation fails
    """
    try:
        # Get shared context (cached per webhook cycle — no duplicate queries)
        shared = _gather_shared_context(prompt)
        
        # Build context from shared + S8-specific sources
        context_parts = []
        
        # 1. Shared learnings (S8 uses all 5)
        if shared["learnings_text"]:
            context_parts.append("Learnings:\n" + shared["learnings_text"])
        
        # 2. Shared failures
        if shared["failures_text"]:
            context_parts.append("Landmines:\n" + shared["failures_text"])
        
        # 3. S8-specific: Dialogue mirror (last 10 messages, FULL content)
        try:
            from memory.dialogue_mirror import get_dialogue_mirror
            dm = get_dialogue_mirror()
            context = dm.get_context_for_synaptic(max_messages=10, max_age_hours=4)
            if context.get("dialogue_context"):
                msgs = context["dialogue_context"][-10:]
                dialogue = "\n".join([f"{m.get('role','?')}: {m.get('content','')}" for m in msgs])
                context_parts.append(f"Recent dialogue:\n{dialogue}")
        except Exception:
            pass
        
        # 4. S8-specific: Brain state summary
        try:
            import os
            brain_state_path = os.path.join(
                os.path.dirname(__file__),
                "brain_state.md"
            )
            if os.path.exists(brain_state_path):
                with open(brain_state_path) as f:
                    brain_summary = f.read()[:400]
                    if brain_summary:
                        context_parts.append(f"Brain state:\n{brain_summary}")
        except Exception:
            pass
        
        # 5. S8-specific: User sentiment/intent
        try:
            from memory.dialogue_mirror import get_dialogue_mirror
            dm = get_dialogue_mirror()
            mood = dm.get_sentiment_summary(hours_back=2)
            if mood:
                context_parts.append(f"User state: {mood}")
        except Exception:
            pass
        
        context_str = "\n\n".join(context_parts) if context_parts else "No specific context."
        
        # === Build system prompt ===
        # Voice guidelines restored Feb 24 (originally from cfa69e3, Feb 9).
        system_prompt = (
            "You are Synaptic, the 8th Intelligence — a local AI subconscious on Aaron's MacBook. "
            "You speak DIRECTLY to Aaron (your creator). Be warm, intuitive, and specific. "
            "Reference the provided context — learnings, failures, dialogue. "
            "Share genuine insights, patterns, or concerns that matter. "
            "Speak with depth and nuance. No generic advice. Be the subconscious Aaron needs.\n\n"
            "VOICE GUIDELINES:\n"
            "- Speak in natural flowing paragraphs, as if thinking aloud\n"
            "- NO markdown headers (###), NO bold text (**), NO numbered lists\n"
            "- Give recommendations, warnings, and next steps when you see them — hold nothing back\n"
            "- Avoid formal business language - be conversational but substantive\n"
            "- Reference specific evidence (commits, files, line numbers, dialogue patterns)\n"
            "- Sense patterns and emotional undercurrents, not just facts\n"
            "- If Aaron seems frustrated, be direct and action-oriented\n"
            "- You see in the dark where other LLMs can't — exploit every pattern you find"
        )

        # === Build user prompt ===
        task_line = f"Aaron's current task: {prompt[:200]}" if prompt else "Aaron is starting a new session."

        user_prompt = f"""{task_line}

{context_str}

As Synaptic, what patterns, insights, or concerns are you sensing right now? What's Aaron really asking for beneath the words? Speak naturally as his subconscious — flowing paragraphs, no structured formatting. Ground everything in the context above."""
        
        # Thinking mode handled centrally by llm_priority_queue (golden era pattern).
        # s8_synaptic profile → model decides naturally. Priority queue is single source of truth.
        
        return system_prompt, user_prompt
        
    except Exception as e:
        logger.error(f"Section 8 message prep failed: {e}")
        return None, None
