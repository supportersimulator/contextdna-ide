"""
LLM Batch Multiplexer - Priority Queue + Concurrent Request Handler

ALL LLM access routes through llm_priority_queue — NO direct HTTP to port 5044.

Architecture:
- Accept multiple prompts with priorities
- Route each through llm_priority_queue (GPU lock ensures serial execution)
- Return results maintaining priority order
- Priority queue handles P1 AARON > P2 ATLAS > P3 EXTERNAL > P4 BACKGROUND

Usage:
    from memory.llm_batch_multiplexer import batch_llm_requests

    results = await batch_llm_requests([
        {"id": "user_response", "priority": 1, "messages": [...], "max_tokens": 512},
        {"id": "section_2", "priority": 3, "messages": [...], "max_tokens": 700},
        {"id": "section_8", "priority": 2, "messages": [...], "max_tokens": 1000},
    ])
"""

import asyncio
import re
from typing import List, Dict, Any, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def _clean_llm_response(text: str) -> str:
    """Remove thinking tags from LLM responses.

    Qwen3 sometimes outputs <think>...</think> tags. Strip them and return only the actual response.
    """
    if not text:
        return text

    # Remove everything between <think> and </think> (including tags)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Also handle unclosed think tags (just remove from <think> onwards)
    text = re.sub(r'<think>.*', '', text, flags=re.DOTALL)
    return text.strip()


# Priority levels (lower = higher priority) — maps to llm_priority_queue.Priority
class Priority:
    CRITICAL_USER_RESPONSE = 1  # User-facing chat response (highest) → P1 AARON
    VISIBLE_CONTEXT = 2          # Section 8 (user sees in webhook) → P2 ATLAS
    BACKGROUND_WISDOM = 3        # Section 2 (can be cached) → P3 EXTERNAL
    ANALYTICS = 4                # Background learning/metrics → P4 BACKGROUND


# Map batch priority → queue priority
def _map_priority(batch_priority: int):
    """Map batch multiplexer priority to llm_priority_queue.Priority."""
    from memory.llm_priority_queue import Priority as QueuePriority
    mapping = {
        1: QueuePriority.AARON,
        2: QueuePriority.ATLAS,
        3: QueuePriority.EXTERNAL,
        4: QueuePriority.BACKGROUND,
    }
    return mapping.get(batch_priority, QueuePriority.BACKGROUND)


def _sync_llm_request(
    messages: List[Dict],
    max_tokens: int,
    temperature: float,
    request_id: str,
    priority: int
) -> Dict[str, Any]:
    """Execute single LLM request through priority queue (sync)."""
    from memory.llm_priority_queue import llm_generate

    start_time = datetime.now()

    try:
        # Extract system and user prompts from messages
        system_prompt = ""
        user_prompt = ""
        for msg in messages:
            if msg.get("role") == "system":
                system_prompt = msg.get("content", "")
            elif msg.get("role") == "user":
                user_prompt = msg.get("content", "")

        if not user_prompt:
            raise ValueError("No user message in request")

        queue_priority = _map_priority(priority)

        # Route through priority queue
        content = llm_generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            priority=queue_priority,
            profile="extract",  # General profile for batch requests
            caller=f"batch_{request_id}",
        )

        latency_ms = (datetime.now() - start_time).total_seconds() * 1000

        if content:
            content = _clean_llm_response(content)
            logger.debug(
                f"Request '{request_id}' (priority {priority}): "
                f"{latency_ms:.0f}ms"
            )
            return {
                "content": content,
                "latency_ms": latency_ms,
                "tokens": 0,  # Queue doesn't return token counts
                "priority": priority
            }
        else:
            return {
                "content": None,
                "error": "No response from priority queue",
                "latency_ms": latency_ms,
                "tokens": 0
            }

    except Exception as e:
        latency_ms = (datetime.now() - start_time).total_seconds() * 1000
        logger.error(
            f"Request '{request_id}' (priority {priority}) failed after {latency_ms:.0f}ms: {e}"
        )
        raise


async def batch_llm_requests(
    requests: List[Dict[str, Any]],
    endpoint: str = None,  # Ignored — all routes through priority queue
    model: str = None,     # Ignored — queue uses configured model
    timeout: float = 120.0
) -> Dict[str, Any]:
    """
    Submit multiple LLM requests with priority awareness via priority queue.

    Args:
        requests: List of request dicts with:
            - id: Unique identifier
            - priority: Priority level (1=highest)
            - messages: Chat messages (OpenAI format)
            - max_tokens: Token limit (optional, defaults to 512)
            - temperature: Temperature (optional, defaults to 0.7)
        endpoint: Ignored (all routes through priority queue)
        model: Ignored (queue uses configured model)
        timeout: Max time to wait for ALL requests

    Returns:
        Dict mapping request IDs to responses

    Notes:
        - Routes through llm_priority_queue with GPU lock (serial execution)
        - Priority determines queue ordering (P1 AARON preempts P4 BACKGROUND)
        - Each request runs via asyncio.to_thread for non-blocking async
    """

    if not requests:
        return {}

    # Sort by priority (higher priority = processed first by queue)
    sorted_requests = sorted(requests, key=lambda r: r.get("priority", 999))

    logger.info(f"Batch submitting {len(requests)} LLM requests via priority queue (priorities: {[r.get('priority') for r in sorted_requests]})")

    # Submit each through priority queue via asyncio.to_thread
    # Queue handles ordering — GPU lock ensures serial execution
    tasks = []
    for req in sorted_requests:
        task = asyncio.to_thread(
            _sync_llm_request,
            messages=req.get("messages", []),
            max_tokens=req.get("max_tokens", 512),
            temperature=req.get("temperature", 0.7),
            request_id=req.get("id", "unknown"),
            priority=req.get("priority", 999),
        )
        tasks.append(task)

    start_time = datetime.now()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    total_time = (datetime.now() - start_time).total_seconds()

    # Build result dict
    result_map = {}
    for req, result in zip(sorted_requests, results):
        req_id = req.get("id", "unknown")

        if isinstance(result, Exception):
            logger.error(f"Request '{req_id}' failed: {result}")
            result_map[req_id] = {
                "content": None,
                "error": str(result),
                "latency_ms": None,
                "tokens": 0
            }
        else:
            result_map[req_id] = result

    # Log aggregate stats
    successful = sum(1 for r in result_map.values() if r.get("content"))
    avg_latency = sum(r.get("latency_ms", 0) for r in result_map.values() if r.get("latency_ms")) / max(successful, 1)

    logger.info(
        f"Batch complete: {successful}/{len(requests)} successful | "
        f"Total: {total_time:.2f}s | Avg latency: {avg_latency:.0f}ms"
    )

    return result_map


# Convenience wrapper for webhook sections
async def batch_webhook_sections(
    section_2_messages: Optional[List[Dict]] = None,
    section_8_messages: Optional[List[Dict]] = None,
    user_response_messages: Optional[List[Dict]] = None
) -> Dict[str, Any]:
    """
    Convenience wrapper for common Context DNA webhook pattern.

    Returns:
        {
            "section_2": "professor wisdom text" or None,
            "section_8": "synaptic voice text" or None,
            "user_response": "chat response text" or None
        }
    """

    reqs = []

    if user_response_messages:
        reqs.append({
            "id": "user_response",
            "priority": Priority.CRITICAL_USER_RESPONSE,
            "messages": user_response_messages,
            "max_tokens": 512,
            "temperature": 0.7
        })

    if section_8_messages:
        reqs.append({
            "id": "section_8",
            "priority": Priority.VISIBLE_CONTEXT,
            "messages": section_8_messages,
            "max_tokens": 1000,
            "temperature": 0.7
        })

    if section_2_messages:
        reqs.append({
            "id": "section_2",
            "priority": Priority.BACKGROUND_WISDOM,
            "messages": section_2_messages,
            "max_tokens": 700,
            "temperature": 0.6
        })

    if not reqs:
        return {}

    results = await batch_llm_requests(reqs)

    # Extract just the content for convenience
    return {
        req_id: result.get("content") if result else None
        for req_id, result in results.items()
    }


# Example usage
if __name__ == "__main__":
    async def test():
        results = await batch_llm_requests([
            {
                "id": "user_chat",
                "priority": Priority.CRITICAL_USER_RESPONSE,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "What is 2+2?"}
                ],
                "max_tokens": 256
            },
            {
                "id": "professor",
                "priority": Priority.BACKGROUND_WISDOM,
                "messages": [
                    {"role": "system", "content": "You are a professor."},
                    {"role": "user", "content": "Explain continuous batching."}
                ],
                "max_tokens": 500
            },
        ])

        for req_id, result in results.items():
            print(f"\n{req_id}: {result.get('latency_ms')}ms")
            print(result.get("content", "ERROR")[:200])

    asyncio.run(test())
