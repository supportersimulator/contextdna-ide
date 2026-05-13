"""
Webhook Batch Helper - S2+S8 LLM calls via Priority Queue

Architecture (revised 2026-02-18):
- Routes ALL LLM calls through llm_priority_queue (P2 ATLAS priority)
- GPU lock prevents concurrent Metal operations that caused abort() crashes
- S2+S8 run SEQUENTIALLY through queue (GPU can only do one at a time anyway)
- Priority ordering ensures webhook calls preempt background gold mining

Previous architecture (pre-2026-02-18):
- Used ThreadPoolExecutor for concurrent requests — but mlx_lm.server serializes
  internally, so concurrency only saved HTTP overhead while risking GPU contention
- Direct requests.post bypassed priority queue entirely

Thinking mode: handled centrally by llm_priority_queue (golden era pattern).
S2/S8 profiles → model decides naturally. No manual /think or /no_think injection here.
"""

import re
import time
from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)


def _extract_system_user(messages: list) -> Tuple[str, str]:
    """Extract system and user prompts from OpenAI-format messages list."""
    system_prompt = ""
    user_prompt = ""
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_prompt = content
        elif role == "user":
            user_prompt = content
    return system_prompt, user_prompt


def batch_section_2_and_8_llm_calls(
    section_2_messages: Optional[list] = None,
    section_8_messages: Optional[list] = None,
    section_2_max_tokens: int = 700,
    section_8_max_tokens: int = 1500,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Run Section 2 and Section 8 LLM calls via priority queue (P2 ATLAS).

    Calls are sequential (GPU serializes anyway) but jump ahead of background
    tasks in the queue. GPU lock prevents cross-process Metal contention.

    Args:
        section_2_messages: Messages for Professor wisdom (or None to skip)
        section_8_messages: Messages for Synaptic voice (or None to skip)
        section_2_max_tokens: Token limit for Section 2
        section_8_max_tokens: Token limit for Section 8

    Returns:
        Tuple of (section_2_content, section_8_content)
        Either can be None if skipped or failed
    """
    if not section_2_messages and not section_8_messages:
        return None, None

    try:
        from memory.llm_priority_queue import s2_professor_query, s8_synaptic_query
    except ImportError:
        logger.error("Cannot import llm_priority_queue — S2/S8 generation unavailable")
        return None, None

    t0 = time.monotonic()
    s2_content = None
    s8_content = None

    # S2: Professor wisdom (thinking mode handled by priority queue)
    if section_2_messages:
        system_prompt, user_prompt = _extract_system_user(section_2_messages)
        brief = section_2_max_tokens <= 400
        s2_content = s2_professor_query(system_prompt, user_prompt, brief=brief)
        if s2_content:
            # Strip any residual thinking chain
            s2_content = re.sub(r"<think>.*?</think>", "", s2_content, flags=re.DOTALL).strip()
            s2_content = re.sub(r"<think>.*", "", s2_content, flags=re.DOTALL).strip()
            if len(s2_content) < 30:
                s2_content = None

    # S8: Synaptic voice (thinking mode handled by priority queue)
    if section_8_messages:
        system_prompt, user_prompt = _extract_system_user(section_8_messages)
        response, _thinking = s8_synaptic_query(system_prompt, user_prompt)
        if response and len(response) > 30:
            s8_content = response

    total_ms = int((time.monotonic() - t0) * 1000)
    s2_status = "ok" if s2_content else "miss"
    s8_status = "ok" if s8_content else "miss"
    logger.info(f"S2+S8 via queue: S2={s2_status} S8={s8_status} | {total_ms}ms")

    return s2_content, s8_content


def extract_thinking_chain(raw_text: str) -> str:
    """
    Extract and discard Qwen3 thinking chain (<think>...</think>).
    Returns only the final content.
    """
    if not raw_text:
        return ""

    # Remove thinking chain
    content = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    return content if content else raw_text.strip()
