"""
agent_review_bridge.py - Agent spawn/completion tracking for Synaptic review loop

Tracks when Atlas spawns Task agents and queues their outputs for Synaptic review.
Used by:
  - PostToolUse hook (auto-capture-results.sh) → enqueue_agent()
  - session_file_watcher.py → on agent completion → dequeue_for_review()
  - synaptic_reviewer.py → after review → mark_reviewed()

Storage: ~/.context-dna/.agent_review_queue.json (crash-resilient JSON)
"""

import json
import os
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("context_dna.agent_review")

QUEUE_PATH = Path(os.path.expanduser("~/.context-dna/.agent_review_queue.json"))


def _load_queue() -> list[dict]:
    """Load queue from disk, return empty list if missing/corrupt."""
    try:
        if QUEUE_PATH.exists():
            data = json.loads(QUEUE_PATH.read_text())
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.debug(f"Queue load failed: {e}")
    return []


def _save_queue(queue: list[dict]) -> None:
    """Atomic write to queue file."""
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, indent=2))
    tmp.rename(QUEUE_PATH)


def enqueue_agent(
    session_id: str,
    agent_task: str,
    agent_description: str = "",
    plan_file: Optional[str] = None,
) -> str:
    """Record that an agent was spawned and needs review when complete.

    Called by PostToolUse hook when Task tool is detected.
    Returns: agent_id (timestamp-based)
    """
    agent_id = f"agent_{int(time.time())}_{os.getpid()}"
    queue = _load_queue()

    entry = {
        "agent_id": agent_id,
        "session_id": session_id,
        "agent_task": agent_task[:500],  # truncate long tasks
        "agent_description": agent_description[:200],
        "plan_file": plan_file,
        "status": "pending",  # pending → completed → reviewed
        "enqueued_at": time.time(),
        "completed_at": None,
        "output_summary": None,
        "review": None,
    }

    queue.append(entry)

    # Keep only last 50 entries to prevent unbounded growth
    if len(queue) > 50:
        queue = queue[-50:]

    _save_queue(queue)
    logger.info(f"Enqueued agent {agent_id}: {agent_task[:80]}")
    return agent_id


def mark_completed(
    session_id: str,
    output_summary: str,
    agent_task_match: Optional[str] = None,
) -> Optional[dict]:
    """Mark the most recent pending agent for a session as completed.

    Called by session_file_watcher.py when agent completion is detected.
    Returns the completed entry or None.
    """
    queue = _load_queue()
    target = None

    # Find most recent pending entry for this session
    for entry in reversed(queue):
        if entry["session_id"] == session_id and entry["status"] == "pending":
            if agent_task_match:
                # If we have a task hint, prefer matching entries
                if agent_task_match.lower() in entry["agent_task"].lower():
                    target = entry
                    break
            else:
                target = entry
                break

    if not target:
        return None

    target["status"] = "completed"
    target["completed_at"] = time.time()
    target["output_summary"] = output_summary[:2000]  # cap at 2k chars

    _save_queue(queue)
    logger.info(f"Agent {target['agent_id']} completed, queued for review")
    return target


def get_pending_reviews(session_id: Optional[str] = None) -> list[dict]:
    """Get agents that completed but haven't been reviewed yet."""
    queue = _load_queue()
    return [
        e for e in queue
        if e["status"] == "completed"
        and (session_id is None or e["session_id"] == session_id)
    ]


def mark_reviewed(agent_id: str, review: dict) -> bool:
    """Store Synaptic's review for a completed agent."""
    queue = _load_queue()
    for entry in queue:
        if entry["agent_id"] == agent_id:
            entry["status"] = "reviewed"
            entry["review"] = review
            _save_queue(queue)
            logger.info(f"Agent {agent_id} reviewed: alignment={review.get('alignment', '?')}")
            return True
    return False


def get_latest_review(session_id: str) -> Optional[dict]:
    """Get the most recent review for a session (for Section 6 injection)."""
    queue = _load_queue()
    for entry in reversed(queue):
        if entry["session_id"] == session_id and entry["status"] == "reviewed":
            return entry
    return None


def get_all_reviews(session_id: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Get recent reviews for the Reviews tab."""
    queue = _load_queue()
    reviews = [
        e for e in queue
        if e["status"] == "reviewed"
        and (session_id is None or e["session_id"] == session_id)
    ]
    return reviews[-limit:]


def cleanup_old(max_age_hours: int = 24) -> int:
    """Remove entries older than max_age_hours."""
    queue = _load_queue()
    cutoff = time.time() - (max_age_hours * 3600)
    before = len(queue)
    queue = [e for e in queue if e["enqueued_at"] > cutoff]
    _save_queue(queue)
    return before - len(queue)


# CLI interface for hook scripts
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: agent_review_bridge.py enqueue <session_id> <agent_task> [description]")
        print("       agent_review_bridge.py pending [session_id]")
        print("       agent_review_bridge.py cleanup")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "enqueue" and len(sys.argv) >= 4:
        sid = sys.argv[2]
        task = sys.argv[3]
        desc = sys.argv[4] if len(sys.argv) > 4 else ""
        aid = enqueue_agent(sid, task, desc)
        print(f"Enqueued: {aid}")

    elif cmd == "pending":
        sid = sys.argv[2] if len(sys.argv) > 2 else None
        pending = get_pending_reviews(sid)
        print(json.dumps(pending, indent=2))

    elif cmd == "cleanup":
        removed = cleanup_old()
        print(f"Removed {removed} old entries")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
