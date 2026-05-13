"""
Chat-First, Enrich-Later Architecture

REPLACES: chat_batch_integration.py (batched concurrent requests)
REASON: Batching multiple requests to a single GPU makes EACH request slower.
        Continuous batching increases throughput but hurts per-request latency.

Architecture:
    1. CHAT FIRST  → Send user's chat request via priority queue (P1 AARON)
                   → Simulate streaming by yielding response in chunks
                   → Full response in ~15s (vs 45s when batched)

    2. ENRICH LATER → AFTER chat response delivered to user
                    → Fire background S2/S8 requests via queue (P4 BACKGROUND)
                    → Cache results for next webhook injection
                    → User never waits for this

ALL LLM access routes through llm_priority_queue — NO direct HTTP to port 5044.
"""

import asyncio
import logging
import threading
import time
from typing import AsyncGenerator, Optional, Dict, Tuple

logger = logging.getLogger(__name__)

# Background enrichment cache (simple in-memory, replaced each cycle)
_enrichment_cache: Dict[str, str] = {}
_enrichment_lock = threading.Lock()


# =========================================================================
# PART 1: CHAT GENERATION (user-facing, P1 AARON priority)
# =========================================================================

async def stream_chat_response(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.7,
) -> AsyncGenerator[str, None]:
    """
    Generate chat response via priority queue then yield in chunks.

    Routes through llm_priority_queue at P1 AARON priority.
    Simulates streaming by yielding the complete response in word-sized chunks.
    """
    start = time.monotonic()
    try:
        from memory.llm_priority_queue import synaptic_chat_query
        result = synaptic_chat_query(
            system_prompt=f"/no_think\n{system_prompt}",
            user_prompt=user_prompt,
            profile="synaptic_chat",
        )
        if result:
            latency = (time.monotonic() - start) * 1000
            logger.info(f"Chat via queue: {latency:.0f}ms")
            # Yield in word-sized chunks to simulate streaming
            words = result.split(' ')
            for i, word in enumerate(words):
                chunk = word if i == 0 else ' ' + word
                yield chunk
                await asyncio.sleep(0)  # Yield control to event loop
        else:
            yield "[No response from LLM]"
    except Exception as e:
        logger.error(f"Stream chat via queue failed: {e}")
        yield f"[Error: {str(e)[:80]}]"


async def generate_chat_response(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.7,
) -> Tuple[Optional[str], float]:
    """
    Non-streaming chat response via priority queue (P1 AARON).

    Returns:
        Tuple of (response_text, latency_ms)
    """
    start = time.monotonic()
    try:
        from memory.llm_priority_queue import synaptic_chat_query
        result = synaptic_chat_query(
            system_prompt=f"/no_think\n{system_prompt}",
            user_prompt=user_prompt,
            profile="synaptic_chat",
        )
        latency = (time.monotonic() - start) * 1000
        if result:
            logger.info(f"Chat response via queue: {latency:.0f}ms")
            return result, latency
        return None, latency
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        logger.error(f"Chat request via queue failed after {latency:.0f}ms: {e}")
        return None, latency


def generate_chat_sync(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.7,
) -> Tuple[Optional[str], float]:
    """
    Synchronous chat generation via priority queue (P1 AARON).
    """
    start = time.monotonic()
    try:
        from memory.llm_priority_queue import synaptic_chat_query
        result = synaptic_chat_query(
            system_prompt=f"/no_think\n{system_prompt}",
            user_prompt=user_prompt,
            profile="synaptic_chat",
        )
        latency = (time.monotonic() - start) * 1000
        if result:
            logger.info(f"Sync chat via queue: {latency:.0f}ms")
            return result, latency
        return None, latency
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        logger.error(f"Sync chat generation failed: {e}")
        return None, latency


# =========================================================================
# PART 2: BACKGROUND ENRICHMENT (fire-and-forget, after user gets response)
# =========================================================================

def _run_enrichment_sync(prompt: str, session_id: Optional[str] = None):
    """
    Generate Section 8 and Section 2 SEQUENTIALLY via priority queue (P4 BACKGROUND).

    Runs AFTER the user has already received their chat response.
    """
    logger.info(f"Background enrichment starting for: {prompt[:50]}...")

    try:
        from memory.webhook_message_builders import prepare_section_2_messages, prepare_section_8_messages
        from memory.llm_priority_queue import llm_generate, Priority
    except ImportError as e:
        logger.warning(f"Cannot import for enrichment: {e}")
        return

    # Run Section 8 first (higher priority - user-visible in webhook)
    try:
        s8_system, s8_user = prepare_section_8_messages(prompt, session_id)
        if s8_system and s8_user:
            start = time.monotonic()
            s8_text = llm_generate(
                system_prompt=s8_system,
                user_prompt=s8_user,
                priority=Priority.BACKGROUND,
                profile="s8_synaptic",
                caller="enrichment_s8",
                timeout_s=90.0,
            )
            latency = (time.monotonic() - start) * 1000
            if s8_text:
                with _enrichment_lock:
                    _enrichment_cache["section_8"] = s8_text
                logger.info(f"Section 8 enriched: {latency:.0f}ms")
    except Exception as e:
        logger.warning(f"Section 8 enrichment failed: {e}")

    # Then Section 2 (lower priority - background wisdom)
    try:
        s2_system, s2_user = prepare_section_2_messages(prompt, None)
        if s2_system and s2_user:
            start = time.monotonic()
            s2_text = llm_generate(
                system_prompt=s2_system,
                user_prompt=s2_user,
                priority=Priority.BACKGROUND,
                profile="s2_professor",
                caller="enrichment_s2",
                timeout_s=60.0,
            )
            latency = (time.monotonic() - start) * 1000
            if s2_text:
                s2_text = f"[Professor via local LLM — reasoning]\n{s2_text}"
                with _enrichment_lock:
                    _enrichment_cache["section_2"] = s2_text
                logger.info(f"Section 2 enriched: {latency:.0f}ms")

                # Cache in Redis if available
                try:
                    from memory.persistent_hook_structure import is_redis_available, _make_cache_key
                    if is_redis_available():
                        from memory.redis_cache import cache_section_content
                        cache_key = _make_cache_key("s2", prompt, "medium")
                        cache_section_content(cache_key, s2_text, ttl_seconds=600)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Section 2 enrichment failed: {e}")

    logger.info("Background enrichment complete")


def fire_background_enrichment(prompt: str, session_id: Optional[str] = None):
    """
    Fire-and-forget background enrichment via priority queue.

    Call this AFTER returning the chat response to the user.
    """
    def _run():
        try:
            _run_enrichment_sync(prompt, session_id)
        except Exception as e:
            logger.error(f"Background enrichment thread failed: {e}")

    thread = threading.Thread(target=_run, daemon=True, name="enrichment")
    thread.start()
    logger.info(f"Background enrichment fired (thread: {thread.name})")


def get_cached_enrichment() -> Dict[str, Optional[str]]:
    """
    Get cached enrichment results from the last background run.

    Returns:
        {"section_2": "..." or None, "section_8": "..." or None}
    """
    with _enrichment_lock:
        return {
            "section_2": _enrichment_cache.get("section_2"),
            "section_8": _enrichment_cache.get("section_8"),
        }


# =========================================================================
# PART 3: SIMPLE QUESTION DETECTION (reused from existing code)
# =========================================================================

def is_simple_question(prompt: str) -> bool:
    """Detect simple questions that don't need background enrichment."""
    prompt_lower = prompt.lower().strip()
    word_count = len(prompt_lower.split())

    # Very short messages
    if word_count <= 5:
        return True

    # Greetings / simple social
    simple_patterns = [
        "hi", "hello", "hey", "thanks", "thank you", "ok", "okay",
        "yes", "no", "sure", "got it", "cool", "nice", "great",
        "what is your name", "who are you", "how are you",
    ]
    for pattern in simple_patterns:
        if prompt_lower == pattern or prompt_lower.startswith(pattern + " "):
            return True

    return False
