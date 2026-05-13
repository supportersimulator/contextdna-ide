#!/usr/bin/env python3
"""
HEARTBEAT WATCHDOG - Stall Detection for Cognitive Control Tasks

This module monitors active tasks for missing heartbeats and marks them as stalled.

Design (addressing Synaptic's gap analysis):
- Runs as background asyncio task
- Checks every 10 seconds for tasks without recent heartbeats
- Marks tasks as "stalled" if no heartbeat in STALL_THRESHOLD seconds
- Broadcasts stall events to WebSocket subscribers

Configuration:
- STALL_THRESHOLD: 30 seconds (task considered stalled if no heartbeat)
- CHECK_INTERVAL: 10 seconds (how often to check)

Usage:
    # Start watchdog (typically called from synaptic_chat_server.py startup)
    from memory.heartbeat_watchdog import start_watchdog, stop_watchdog

    # In server startup:
    await start_watchdog()

    # In server shutdown:
    await stop_watchdog()
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from memory.task_persistence import get_task_store

logger = logging.getLogger(__name__)

# Configuration
STALL_THRESHOLD_SECONDS = 30  # Task stalled if no heartbeat in 30s
CHECK_INTERVAL_SECONDS = 10   # Check every 10s

# Watchdog state
_watchdog_task: Optional[asyncio.Task] = None
_running = False


def get_stalled_tasks() -> List[Dict[str, Any]]:
    """
    Find tasks that are executing but haven't sent a heartbeat recently.

    Returns:
        List of stalled task dictionaries
    """
    store = get_task_store()
    stalled = []

    # Get all executing tasks
    executing_tasks = store.get_tasks_by_status("executing", limit=100)
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(seconds=STALL_THRESHOLD_SECONDS)

    for task in executing_tasks:
        # Check latest progress event
        latest = store.get_latest_progress(task["task_id"])

        if latest:
            # Parse timestamp
            try:
                event_time = datetime.fromisoformat(latest["timestamp"].replace("Z", "+00:00"))
                if event_time < threshold:
                    stalled.append({
                        **task,
                        "last_heartbeat": latest["timestamp"],
                        "seconds_since_heartbeat": (now - event_time).total_seconds()
                    })
            except (ValueError, KeyError):
                # Can't parse timestamp - consider stalled
                stalled.append({
                    **task,
                    "last_heartbeat": None,
                    "seconds_since_heartbeat": STALL_THRESHOLD_SECONDS + 1
                })
        else:
            # No progress events at all - check task updated_at
            try:
                updated = datetime.fromisoformat(task["updated_at"].replace("Z", "+00:00"))
                if updated < threshold:
                    stalled.append({
                        **task,
                        "last_heartbeat": None,
                        "seconds_since_heartbeat": (now - updated).total_seconds()
                    })
            except (ValueError, KeyError):
                stalled.append({
                    **task,
                    "last_heartbeat": None,
                    "seconds_since_heartbeat": STALL_THRESHOLD_SECONDS + 1
                })

    return stalled


def mark_task_stalled(task_id: str) -> Dict[str, Any]:
    """
    Mark a task as stalled due to missing heartbeats.

    Args:
        task_id: The task to mark

    Returns:
        Updated task dictionary
    """
    store = get_task_store()
    return store.update_task(task_id, {
        "status": "stalled",
        "stall_reason": f"No heartbeat received in {STALL_THRESHOLD_SECONDS} seconds"
    })


async def _watchdog_loop():
    """
    Main watchdog loop - checks for stalled tasks periodically.
    """
    global _running
    logger.info(f"Heartbeat watchdog started (threshold: {STALL_THRESHOLD_SECONDS}s, interval: {CHECK_INTERVAL_SECONDS}s)")

    while _running:
        try:
            stalled = get_stalled_tasks()

            for task in stalled:
                task_id = task["task_id"]
                seconds = task.get("seconds_since_heartbeat", 0)

                logger.warning(f"Task {task_id} stalled - no heartbeat for {seconds:.0f}s")

                # Mark as stalled
                mark_task_stalled(task_id)

                # Broadcast stall event to WebSocket subscribers
                try:
                    from memory.progress_broadcaster import broadcast_terminal
                    await broadcast_terminal(task_id, {
                        "outcome": "stalled",
                        "summary": f"Task stalled - no heartbeat for {seconds:.0f} seconds",
                        "files_changed": [],
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
                except ImportError:
                    pass  # Broadcaster not available

            # Wait before next check
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            logger.info("Heartbeat watchdog cancelled")
            break
        except Exception as e:
            logger.error(f"Heartbeat watchdog error: {e}")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    logger.info("Heartbeat watchdog stopped")


async def start_watchdog():
    """
    Start the heartbeat watchdog background task.
    """
    global _watchdog_task, _running

    if _running:
        logger.warning("Heartbeat watchdog already running")
        return

    _running = True
    _watchdog_task = asyncio.create_task(_watchdog_loop())
    logger.info("Heartbeat watchdog scheduled")


async def stop_watchdog():
    """
    Stop the heartbeat watchdog.
    """
    global _watchdog_task, _running

    _running = False

    if _watchdog_task:
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass
        _watchdog_task = None

    logger.info("Heartbeat watchdog stopped")


def is_watchdog_running() -> bool:
    """Check if watchdog is running."""
    return _running


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Heartbeat Watchdog - Stall Detection")
        print()
        print("Usage:")
        print("  python heartbeat_watchdog.py check    # Check for stalled tasks")
        print("  python heartbeat_watchdog.py status   # Show watchdog status")
        print()
        print(f"Configuration:")
        print(f"  STALL_THRESHOLD: {STALL_THRESHOLD_SECONDS}s")
        print(f"  CHECK_INTERVAL: {CHECK_INTERVAL_SECONDS}s")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "check":
        stalled = get_stalled_tasks()
        if stalled:
            print(f"Found {len(stalled)} stalled task(s):")
            for task in stalled:
                print(f"  {task['task_id']}: {task['intent'][:50]}...")
                print(f"    Last heartbeat: {task.get('last_heartbeat', 'never')}")
                print(f"    Stalled for: {task.get('seconds_since_heartbeat', 0):.0f}s")
        else:
            print("No stalled tasks found")

    elif cmd == "status":
        print(f"Watchdog running: {is_watchdog_running()}")
        print(f"Stall threshold: {STALL_THRESHOLD_SECONDS}s")
        print(f"Check interval: {CHECK_INTERVAL_SECONDS}s")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
