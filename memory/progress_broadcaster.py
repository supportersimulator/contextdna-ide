#!/usr/bin/env python3
"""
PROGRESS BROADCASTER - WebSocket Broadcasting for Task Progress

This module provides a clean, import-safe broadcasting system for task progress events.
Extracted to avoid circular imports between cognitive_control_api.py and synaptic_chat_server.py.

Design:
- Maintains its own subscriber registry
- Provides async broadcast functions
- Can be safely imported from any module

Usage:
    from memory.progress_broadcaster import (
        subscribe_to_task,
        unsubscribe_from_task,
        broadcast_progress,
        broadcast_terminal
    )

    # In WebSocket handler:
    await subscribe_to_task(task_id, websocket)

    # In API endpoint:
    await broadcast_progress(task_id, event_data)
"""

import asyncio
import logging
from typing import Dict, Set, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Subscriber registry: task_id -> set of WebSockets
_progress_subscribers: Dict[str, Set["WebSocket"]] = {}


def subscribe_to_task(task_id: str, websocket: "WebSocket") -> None:
    """
    Subscribe a WebSocket to receive progress events for a task.

    Args:
        task_id: Task to subscribe to
        websocket: WebSocket connection to receive events
    """
    if task_id not in _progress_subscribers:
        _progress_subscribers[task_id] = set()
    _progress_subscribers[task_id].add(websocket)
    logger.debug(f"WebSocket subscribed to task {task_id}")


def unsubscribe_from_task(task_id: str, websocket: "WebSocket") -> None:
    """
    Unsubscribe a WebSocket from a task's progress events.

    Args:
        task_id: Task to unsubscribe from
        websocket: WebSocket connection to remove
    """
    if task_id in _progress_subscribers:
        _progress_subscribers[task_id].discard(websocket)
        if not _progress_subscribers[task_id]:
            del _progress_subscribers[task_id]
    logger.debug(f"WebSocket unsubscribed from task {task_id}")


def get_subscriber_count(task_id: str) -> int:
    """Get number of subscribers for a task."""
    return len(_progress_subscribers.get(task_id, set()))


def get_all_subscribed_tasks() -> list:
    """Get list of tasks with active subscribers."""
    return list(_progress_subscribers.keys())


async def broadcast_progress(task_id: str, event_data: dict) -> int:
    """
    Broadcast a progress event to all subscribers of a task.

    Args:
        task_id: Task ID
        event_data: Progress event data to broadcast

    Returns:
        Number of successful broadcasts
    """
    if task_id not in _progress_subscribers:
        return 0

    disconnected = set()
    success_count = 0

    for ws in _progress_subscribers[task_id]:
        try:
            await ws.send_json({
                "type": "progress",
                "task_id": task_id,
                **event_data
            })
            success_count += 1
        except Exception as e:
            logger.debug(f"Failed to broadcast to subscriber: {e}")
            disconnected.add(ws)

    # Clean up disconnected WebSockets
    for ws in disconnected:
        _progress_subscribers[task_id].discard(ws)

    if not _progress_subscribers[task_id]:
        del _progress_subscribers[task_id]

    return success_count


async def broadcast_terminal(task_id: str, terminal_data: dict) -> int:
    """
    Broadcast a terminal event (completion/failure/stalled) to all subscribers.

    Args:
        task_id: Task ID
        terminal_data: Terminal event data including outcome, summary, etc.

    Returns:
        Number of successful broadcasts
    """
    if task_id not in _progress_subscribers:
        return 0

    disconnected = set()
    success_count = 0

    for ws in _progress_subscribers[task_id]:
        try:
            await ws.send_json({
                "type": "terminal",
                "task_id": task_id,
                **terminal_data
            })
            success_count += 1
        except Exception as e:
            logger.debug(f"Failed to broadcast terminal to subscriber: {e}")
            disconnected.add(ws)

    # Clean up disconnected WebSockets
    for ws in disconnected:
        _progress_subscribers[task_id].discard(ws)

    # Note: We don't delete the subscription here in case client wants to reconnect
    # The WebSocket endpoint should handle cleanup

    return success_count


async def broadcast_to_all(event_type: str, data: dict) -> int:
    """
    Broadcast an event to ALL subscribers across all tasks.

    Useful for system-wide notifications.

    Args:
        event_type: Type of event
        data: Event data

    Returns:
        Number of successful broadcasts
    """
    all_websockets = set()
    for subscribers in _progress_subscribers.values():
        all_websockets.update(subscribers)

    success_count = 0
    for ws in all_websockets:
        try:
            await ws.send_json({
                "type": event_type,
                **data
            })
            success_count += 1
        except Exception as e:
            print(f"[WARN] WebSocket broadcast failed: {e}")

    return success_count


# =============================================================================
# BACKWARDS COMPATIBILITY
# =============================================================================
# These provide direct access to the subscriber dict for synaptic_chat_server.py
# which may need to access it directly for the WebSocket endpoint

def get_subscribers() -> Dict[str, Set["WebSocket"]]:
    """
    Get the subscriber registry (for backwards compatibility).

    Prefer using the subscribe/unsubscribe/broadcast functions instead.
    """
    return _progress_subscribers
