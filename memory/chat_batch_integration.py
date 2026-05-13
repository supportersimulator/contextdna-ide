"""
Chat Batch Integration - Batch chat responses with webhook generation

This module provides integration between chat server and batch multiplexer,
allowing user responses to be batched with webhook Section 2 + Section 8
generation for maximum performance.

Architecture:
- User message triggers 3 concurrent LLM requests (if webhook needed)
- Priority 1: User chat response (CRITICAL - returns first)
- Priority 2: Section 8 (VISIBLE - user sees in webhook)
- Priority 3: Section 2 (BACKGROUND - can be cached)

Benefits:
- User response unblocked by webhook generation
- Expected 4-6x faster user responses (5-15s vs 60-90s)
- Single HTTP round-trip for all 3 requests
- vLLM continuous batching schedules them efficiently
"""

import asyncio
from typing import Dict, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


def batch_chat_with_webhook(
    chat_system: str,
    chat_user: str,
    chat_profile: str = "chat",
    webhook_prompt: Optional[str] = None,
    webhook_session_id: Optional[str] = None,
    generate_webhook: bool = True
) -> Dict[str, any]:
    """
    Batch chat response with optional webhook generation (S2 + S8).
    
    This is the main entry point for Phase 3 optimization.
    
    Args:
        chat_system: System prompt for chat response
        chat_user: User prompt for chat response
        chat_profile: Generation profile ("chat", "fast", etc.)
        webhook_prompt: Task description for webhook (if generating webhook)
        webhook_session_id: Session ID for webhook context
        generate_webhook: Whether to generate webhook sections (S2 + S8)
    
    Returns:
        {
            "chat_response": "user-facing response text",
            "chat_latency_ms": 5000,
            "webhook_sections": {
                "section_2": "professor wisdom" or None,
                "section_8": "synaptic voice" or None
            },
            "total_latency_ms": 8000
        }
    
    Priority scheduling ensures user response returns FIRST,
    even if webhook sections still generating.
    """
    
    try:
        from memory.llm_batch_multiplexer import batch_llm_requests, Priority
        from memory.webhook_message_builders import prepare_section_2_messages, prepare_section_8_messages
        from memory.webhook_batch_helper import extract_thinking_chain
        
        # Get generation params for chat
        from memory.synaptic_chat_server import get_generation_params
        params = get_generation_params(chat_profile)
        
        # Build request list
        requests = []
        
        # 1. Chat response (HIGHEST PRIORITY)
        requests.append({
            "id": "chat_response",
            "priority": Priority.CRITICAL_USER_RESPONSE,
            "messages": [
                {"role": "system", "content": chat_system},
                {"role": "user", "content": chat_user}
            ],
            "max_tokens": params.get("max_tokens", 512),
            "temperature": params.get("temperature", 0.7)
        })
        
        # 2. Section 8 (if webhook needed)
        if generate_webhook and webhook_prompt:
            try:
                s8_system, s8_user = prepare_section_8_messages(webhook_prompt, webhook_session_id)
                if s8_system and s8_user:
                    requests.append({
                        "id": "section_8",
                        "priority": Priority.VISIBLE_CONTEXT,
                        "messages": [
                            {"role": "system", "content": s8_system},
                            {"role": "user", "content": s8_user}
                        ],
                        "max_tokens": 1500,
                        "temperature": 0.7
                    })
            except Exception as e:
                logger.warning(f"Section 8 message prep failed: {e}")
        
        # 3. Section 2 (if webhook needed and not cached)
        if generate_webhook and webhook_prompt:
            try:
                # Check cache first
                from memory.persistent_hook_structure import is_redis_available, _make_cache_key
                
                s2_cached = False
                if is_redis_available():
                    from memory.redis_cache import get_cached_section_content
                    # Use simple hash for cache key (risk level not critical for batching)
                    cache_key = _make_cache_key("s2", webhook_prompt, "medium")
                    if get_cached_section_content(cache_key):
                        s2_cached = True
                        logger.info("Section 2 cached, skipping batch")
                
                if not s2_cached:
                    s2_system, s2_user = prepare_section_2_messages(webhook_prompt, None)
                    if s2_system and s2_user:
                        requests.append({
                            "id": "section_2",
                            "priority": Priority.BACKGROUND_WISDOM,
                            "messages": [
                                {"role": "system", "content": s2_system},
                                {"role": "user", "content": s2_user}
                            ],
                            "max_tokens": 700,
                            "temperature": 0.6
                        })
            except Exception as e:
                logger.warning(f"Section 2 message prep failed: {e}")
        
        # Run async batch in sync context
        loop = None
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        logger.info(f"🚀 Batching {len(requests)} requests: {[r['id'] for r in requests]}")
        results = loop.run_until_complete(batch_llm_requests(requests))
        
        # Extract results
        response = {
            "chat_response": None,
            "chat_latency_ms": None,
            "webhook_sections": {
                "section_2": None,
                "section_8": None
            },
            "total_latency_ms": None
        }
        
        # Chat response (highest priority - should be first)
        if "chat_response" in results:
            chat_result = results["chat_response"]
            if chat_result and not chat_result.get("error"):
                response["chat_response"] = chat_result.get("content")
                response["chat_latency_ms"] = chat_result.get("latency_ms")
                logger.info(f"✓ Chat response: {chat_result.get('latency_ms', 0):.0f}ms, {chat_result.get('tokens', 0)} tokens")
        
        # Section 8
        if "section_8" in results:
            s8_result = results["section_8"]
            if s8_result and not s8_result.get("error"):
                s8_content = extract_thinking_chain(s8_result.get("content", ""))
                response["webhook_sections"]["section_8"] = s8_content
                logger.info(f"✓ Section 8: {s8_result.get('latency_ms', 0):.0f}ms, {s8_result.get('tokens', 0)} tokens")
        
        # Section 2
        if "section_2" in results:
            s2_result = results["section_2"]
            if s2_result and not s2_result.get("error"):
                s2_content = extract_thinking_chain(s2_result.get("content", ""))
                if s2_content:
                    s2_content = f"[Professor via local LLM — reasoning]\n{s2_content}"
                    response["webhook_sections"]["section_2"] = s2_content
                    logger.info(f"✓ Section 2: {s2_result.get('latency_ms', 0):.0f}ms, {s2_result.get('tokens', 0)} tokens")
                    
                    # Cache Section 2 for future requests
                    try:
                        if is_redis_available() and webhook_prompt:
                            from memory.redis_cache import cache_section_content
                            cache_key = _make_cache_key("s2", webhook_prompt, "medium")
                            cache_section_content(cache_key, s2_content, ttl_seconds=600)
                    except Exception:
                        pass
        
        # Calculate total latency (max of all)
        latencies = [r.get('latency_ms', 0) for r in results.values() if r and not r.get('error')]
        if latencies:
            response["total_latency_ms"] = max(latencies)
        
        return response
        
    except Exception as e:
        logger.error(f"Batch chat failed: {e}")
        import traceback
        traceback.print_exc()
        
        # Return error response
        return {
            "chat_response": None,
            "chat_latency_ms": None,
            "webhook_sections": {"section_2": None, "section_8": None},
            "total_latency_ms": None,
            "error": str(e)
        }


# Convenience wrapper for simple chat (no webhook)
def generate_chat_only(system_prompt: str, user_prompt: str, profile: str = "chat") -> Tuple[Optional[str], int]:
    """
    Generate chat response only (no webhook batching).
    
    This is used when webhook generation not needed.
    Falls back to normal single request.
    
    Returns:
        Tuple of (response_text, latency_ms)
    """
    result = batch_chat_with_webhook(
        chat_system=system_prompt,
        chat_user=user_prompt,
        chat_profile=profile,
        generate_webhook=False
    )
    
    return result.get("chat_response"), result.get("chat_latency_ms", 0)
