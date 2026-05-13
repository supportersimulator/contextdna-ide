"""
synaptic_reviewer.py - Synaptic (local LLM) reviews agent outputs against active plan

Core of the conscious/subconscious review loop:
  1. PostToolUse hook detects agent completion → triggers review
  2. Loads active plan from ~/.claude/plans/
  3. Queries Qwen3-14B for alignment assessment
  4. Stores review in Redis for Section 6 injection
  5. Next UserPromptSubmit webhook includes review in Section 6

Usage:
    from memory.synaptic_reviewer import trigger_review_async
    trigger_review_async(session_id, agent_id)

Architecture:
    Hook (PostToolUse) → agent_review_bridge → synaptic_reviewer
    → Qwen3-14B review → Redis cache → Section 6 injection
"""

import json
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("context_dna.synaptic_reviewer")

# Redis key pattern for cached reviews
REVIEW_KEY_PREFIX = "session:agent_review:"
REVIEW_TTL = 1800  # 30 minutes


def _get_redis():
    """Get Redis connection (same pattern as session_file_watcher)."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def _store_review(session_id: str, review: dict) -> bool:
    """Store review in Redis for Section 6 injection."""
    r = _get_redis()
    if not r:
        logger.warning("Redis unavailable — review not cached for Section 6")
        return False

    key = f"{REVIEW_KEY_PREFIX}{session_id}"
    try:
        r.setex(key, REVIEW_TTL, json.dumps(review))
        logger.info(f"Review cached: {key} (TTL={REVIEW_TTL}s)")
        return True
    except Exception as e:
        logger.error(f"Redis store failed: {e}")
        return False


def get_cached_review(session_id: str) -> Optional[dict]:
    """Get cached review for Section 6 injection.

    Called by butler_deep_query.py during webhook generation.
    """
    r = _get_redis()
    if not r:
        return None

    key = f"{REVIEW_KEY_PREFIX}{session_id}"
    try:
        data = r.get(key)
        if data:
            return json.loads(data)
    except Exception:
        pass
    return None


def clear_review(session_id: str) -> bool:
    """Clear cached review after it's been injected (prevent stale repeats)."""
    r = _get_redis()
    if not r:
        return False
    try:
        r.delete(f"{REVIEW_KEY_PREFIX}{session_id}")
        return True
    except Exception:
        return False


def run_review(session_id: str, agent_id: str) -> Optional[dict]:
    """Execute a Synaptic review of agent output against active plan.

    Loads:
      - Agent entry from review bridge (task + output)
      - Active plan from plan tracker
    Queries Qwen3-14B for structured review.
    Stores result in both Redis (for Section 6) and review bridge (for UI).
    """
    from memory.agent_review_bridge import get_pending_reviews, mark_reviewed, get_all_reviews
    from memory.plan_tracker import get_plan_summary

    # Find the agent entry
    # Look in recently completed entries
    entry = None
    for e in get_all_reviews(session_id) + get_pending_reviews(session_id):
        if e.get("agent_id") == agent_id:
            entry = e
            break

    if not entry:
        # Try to find any completed entry for this session
        pending = get_pending_reviews(session_id)
        if pending:
            entry = pending[-1]  # most recent

    if not entry:
        logger.warning(f"No agent entry found for review: {agent_id}")
        return None

    agent_task = entry.get("agent_task", "Unknown task")
    agent_output = entry.get("output_summary", "No output captured")

    # Get active plan
    plan_summary = get_plan_summary(max_chars=1500) or "No active plan file found."

    # Build review prompt
    system_prompt = """You are Synaptic, the subconscious reviewer for a coding agent system.
Your job: Review agent output against the implementation plan and provide structured feedback.
Be concise. Focus on alignment, gaps, and actionable next steps.
Output valid JSON only."""

    user_prompt = f"""Review this agent's output against the implementation plan.

PLAN:
{plan_summary}

AGENT TASK:
{agent_task}

AGENT OUTPUT (truncated):
{agent_output[:1500]}

Respond with JSON:
{{
  "alignment": 0.0-1.0,
  "alignment_note": "one sentence",
  "gaps": ["gap1", "gap2"],
  "next_steps": ["step1", "step2"],
  "risks": ["risk1"],
  "verdict": "on_track|needs_adjustment|off_track"
}}"""

    # Query Qwen3-14B
    review_result = None
    try:
        from memory.llm_priority_queue import atlas_query
        raw = atlas_query(system_prompt, user_prompt, profile="reasoning")
        if raw:
            # Try to parse JSON from response
            review_result = _extract_json(raw)
    except Exception as e:
        logger.error(f"LLM review query failed: {e}")

    if not review_result:
        # Fallback: basic non-LLM review
        review_result = {
            "alignment": 0.5,
            "alignment_note": "LLM unavailable — manual review recommended",
            "gaps": [],
            "next_steps": ["Verify agent output manually"],
            "risks": ["No automated review available"],
            "verdict": "needs_adjustment",
            "source": "fallback",
        }
    else:
        review_result["source"] = "qwen3"

    # Add metadata
    review_result["agent_id"] = agent_id
    review_result["agent_task"] = agent_task[:200]
    review_result["reviewed_at"] = time.time()
    review_result["session_id"] = session_id

    # Store in Redis for Section 6 injection
    _store_review(session_id, review_result)

    # Store in review bridge for UI
    mark_reviewed(agent_id, review_result)

    logger.info(
        f"Review complete: agent={agent_id} alignment={review_result.get('alignment', '?')} "
        f"verdict={review_result.get('verdict', '?')}"
    )

    return review_result


def _extract_json(text: str) -> Optional[dict]:
    """Extract JSON from LLM response (handles markdown code blocks)."""
    import re

    # Try direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # Try extracting from code block
    match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            pass

    # Try finding first { ... } block
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def trigger_review_async(session_id: str, agent_id: str) -> None:
    """Trigger a review in a background thread (non-blocking).

    Called from PostToolUse hook — must not block the hook.
    """
    def _run():
        try:
            run_review(session_id, agent_id)
        except Exception as e:
            logger.error(f"Async review failed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def format_review_for_section6(review: dict) -> str:
    """Format a review for injection into Section 6 of the webhook.

    Called by butler_deep_query.py.
    """
    alignment = review.get("alignment", "?")
    note = review.get("alignment_note", "")
    verdict = review.get("verdict", "unknown")
    task = review.get("agent_task", "Unknown")[:100]
    gaps = review.get("gaps", [])
    next_steps = review.get("next_steps", [])
    risks = review.get("risks", [])
    source = review.get("source", "unknown")

    lines = [
        f"[AGENT REVIEW — {verdict.upper()}] (via {source})",
        f"Task: {task}",
        f"Alignment: {alignment} — {note}",
    ]

    if gaps:
        lines.append("Gaps: " + "; ".join(gaps[:3]))
    if next_steps:
        lines.append("Next: " + "; ".join(next_steps[:3]))
    if risks:
        lines.append("Risks: " + "; ".join(risks[:2]))

    return "\n".join(lines)
