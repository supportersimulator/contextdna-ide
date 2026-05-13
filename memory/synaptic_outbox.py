#!/usr/bin/env python3
"""
SYNAPTIC OUTBOX - Direct Conversation Channel

This module enables Synaptic (the local LLM) to send messages directly
into the Claude Code conversation thread, bypassing the webhook injection
system for real-time bidirectional communication.

Architecture:
┌─────────────────────────────────────────────────────────────────┐
│                     BIDIRECTIONAL FLOW                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Aaron ──────────────────────► Atlas (Claude Code)              │
│    │                              │                              │
│    │                              ▼                              │
│    │                    Webhook Hook fires                       │
│    │                              │                              │
│    │                              ▼                              │
│    │                    Check Synaptic Outbox ◄─────────────┐   │
│    │                              │                          │   │
│    │                              ▼                          │   │
│    │                    Include direct messages              │   │
│    │                              │                          │   │
│    │                              ▼                          │   │
│    │                    Atlas sees Synaptic's                │   │
│    │                    direct communication                 │   │
│    │                                                         │   │
│    │                                                         │   │
│    ▼                                                         │   │
│  Synaptic (Local LLM) ───────────────────────────────────────┘   │
│    │                                                              │
│    └──► synaptic_speak("message")                                │
│         Posts to .synaptic_outbox.json                           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘

Usage:
    # Synaptic posts a message
    from memory.synaptic_outbox import synaptic_speak, synaptic_speak_urgent

    synaptic_speak("I noticed a pattern in your recent work...")
    synaptic_speak_urgent("Critical: Database connection failing!")

    # Webhook reads pending messages
    from memory.synaptic_outbox import get_pending_messages, mark_delivered

    messages = get_pending_messages()
    # ... include in injection ...
    mark_delivered([msg['id'] for msg in messages])
"""

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Any
from enum import Enum
import threading
import os

# Base paths
MEMORY_DIR = Path(__file__).parent
OUTBOX_FILE = MEMORY_DIR / ".synaptic_outbox.json"

# Thread lock for file operations
_lock = threading.Lock()


class MessagePriority(str, Enum):
    """Priority levels for Synaptic's direct messages."""
    URGENT = "urgent"      # Displayed prominently, requires attention
    NORMAL = "normal"      # Standard message, included in flow
    WHISPER = "whisper"    # Subtle observation, lower prominence


class SynapticMessage:
    """A direct message from Synaptic."""

    def __init__(
        self,
        content: str,
        priority: MessagePriority = MessagePriority.NORMAL,
        topic: str = None,
        expires_minutes: int = 60,
        metadata: Dict = None
    ):
        self.id = hashlib.sha256(
            f"{datetime.now(timezone.utc).isoformat()}:{content[:50]}".encode()
        ).hexdigest()[:16]
        self.content = content
        self.priority = priority
        self.topic = topic or "general"
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.expires_at = datetime.now(timezone.utc).timestamp() + (expires_minutes * 60)
        self.delivered = False
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "priority": self.priority.value,
            "topic": self.topic,
            "timestamp": self.timestamp,
            "expires_at": self.expires_at,
            "delivered": self.delivered,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'SynapticMessage':
        msg = cls(
            content=d["content"],
            priority=MessagePriority(d.get("priority", "normal")),
            topic=d.get("topic"),
            metadata=d.get("metadata", {})
        )
        msg.id = d["id"]
        msg.timestamp = d["timestamp"]
        msg.expires_at = d.get("expires_at", 0)
        msg.delivered = d.get("delivered", False)
        return msg

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc).timestamp() > self.expires_at


def _load_outbox() -> List[dict]:
    """Load outbox from file."""
    try:
        if OUTBOX_FILE.exists():
            data = json.loads(OUTBOX_FILE.read_text())
            return data.get("messages", [])
    except Exception as e:
        print(f"[WARN] Outbox load failed: {e}")
    return []


def _save_outbox(messages: List[dict]):
    """Save outbox to file."""
    try:
        OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "messages": messages
        }
        OUTBOX_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[Synaptic Outbox] Error saving: {e}")


# =============================================================================
# PUBLIC API - For Synaptic to send messages
# =============================================================================

def synaptic_speak(
    message: str,
    topic: str = None,
    priority: str = "normal",
    expires_minutes: int = 60,
    metadata: Dict = None
) -> str:
    """
    Synaptic posts a message to be delivered to the conversation.

    This is Synaptic's voice directly into the Claude Code thread.
    Messages are queued and delivered on the next webhook injection.

    Args:
        message: The content Synaptic wants to communicate
        topic: Optional topic/category for the message
        priority: "urgent", "normal", or "whisper"
        expires_minutes: How long the message stays in queue (default 60)
        metadata: Optional additional context

    Returns:
        Message ID for tracking

    Example:
        synaptic_speak("I see a pattern forming in your async code...")
        synaptic_speak("Database queries could be batched", topic="optimization")
    """
    with _lock:
        messages = _load_outbox()

        # Clean expired messages
        messages = [m for m in messages if not SynapticMessage.from_dict(m).is_expired()]

        # Create new message
        msg = SynapticMessage(
            content=message,
            priority=MessagePriority(priority),
            topic=topic,
            expires_minutes=expires_minutes,
            metadata=metadata or {}
        )

        messages.append(msg.to_dict())
        _save_outbox(messages)

        return msg.id


def synaptic_speak_urgent(message: str, topic: str = None) -> str:
    """
    Synaptic posts an URGENT message requiring immediate attention.

    Urgent messages are displayed prominently in Section 8.
    """
    return synaptic_speak(
        message=message,
        topic=topic,
        priority="urgent",
        expires_minutes=30  # Shorter expiry for urgent messages
    )


def synaptic_whisper(message: str, topic: str = None) -> str:
    """
    Synaptic posts a subtle observation.

    Whispers are lower prominence - for background insights.
    """
    return synaptic_speak(
        message=message,
        topic=topic,
        priority="whisper",
        expires_minutes=120  # Longer expiry for subtle observations
    )


# =============================================================================
# PUBLIC API - For webhook hook to read messages
# =============================================================================

def get_pending_messages(include_whispers: bool = True) -> List[Dict]:
    """
    Get all pending messages from Synaptic's outbox.

    Called by the webhook injection system to include Synaptic's
    direct messages in the conversation context.

    Args:
        include_whispers: Whether to include low-priority whispers

    Returns:
        List of message dicts, ordered by priority (urgent first)
    """
    with _lock:
        messages = _load_outbox()

        # Filter: undelivered, not expired
        pending = []
        for m in messages:
            msg = SynapticMessage.from_dict(m)
            if not msg.delivered and not msg.is_expired():
                if include_whispers or msg.priority != MessagePriority.WHISPER:
                    pending.append(m)

        # Sort by priority: urgent > normal > whisper
        priority_order = {"urgent": 0, "normal": 1, "whisper": 2}
        pending.sort(key=lambda x: priority_order.get(x.get("priority", "normal"), 1))

        return pending


def mark_delivered(message_ids: List[str]):
    """
    Mark messages as delivered after they've been included in injection.

    This prevents the same message from being sent multiple times.
    """
    with _lock:
        messages = _load_outbox()

        for m in messages:
            if m["id"] in message_ids:
                m["delivered"] = True

        # Remove delivered + expired messages
        messages = [m for m in messages
                   if not m.get("delivered", False) or
                   not SynapticMessage.from_dict(m).is_expired()]

        _save_outbox(messages)


def clear_outbox():
    """Clear all messages from the outbox."""
    with _lock:
        _save_outbox([])


def get_outbox_status() -> Dict:
    """Get status of the Synaptic outbox."""
    with _lock:
        messages = _load_outbox()
        pending = [m for m in messages if not m.get("delivered", False)]

        return {
            "total_messages": len(messages),
            "pending_count": len(pending),
            "urgent_count": sum(1 for m in pending if m.get("priority") == "urgent"),
            "last_updated": OUTBOX_FILE.stat().st_mtime if OUTBOX_FILE.exists() else None
        }


# =============================================================================
# CLI INTERFACE - For testing
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║     Synaptic's Outbox - Direct Conversation Channel         ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()
        print("Usage:")
        print("  python synaptic_outbox.py speak \"Your message here\"")
        print("  python synaptic_outbox.py urgent \"Critical message!\"")
        print("  python synaptic_outbox.py whisper \"Subtle observation...\"")
        print("  python synaptic_outbox.py status")
        print("  python synaptic_outbox.py pending")
        print("  python synaptic_outbox.py clear")
        print()

        # Show current status
        status = get_outbox_status()
        print(f"Current outbox: {status['pending_count']} pending messages")
        if status['urgent_count'] > 0:
            print(f"  ⚠️ {status['urgent_count']} URGENT!")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "speak" and len(sys.argv) > 2:
        message = " ".join(sys.argv[2:])
        msg_id = synaptic_speak(message)
        print(f"✅ Message posted (ID: {msg_id})")
        print(f"   Content: {message[:60]}...")

    elif cmd == "urgent" and len(sys.argv) > 2:
        message = " ".join(sys.argv[2:])
        msg_id = synaptic_speak_urgent(message)
        print(f"🚨 URGENT message posted (ID: {msg_id})")
        print(f"   Content: {message[:60]}...")

    elif cmd == "whisper" and len(sys.argv) > 2:
        message = " ".join(sys.argv[2:])
        msg_id = synaptic_whisper(message)
        print(f"🤫 Whisper posted (ID: {msg_id})")
        print(f"   Content: {message[:60]}...")

    elif cmd == "status":
        status = get_outbox_status()
        print(json.dumps(status, indent=2))

    elif cmd == "pending":
        pending = get_pending_messages()
        if pending:
            print(f"📬 {len(pending)} pending messages:")
            for m in pending:
                priority_icon = {"urgent": "🚨", "normal": "📩", "whisper": "🤫"}.get(m["priority"], "📩")
                print(f"  {priority_icon} [{m['id'][:8]}] {m['content'][:50]}...")
        else:
            print("📭 No pending messages")

    elif cmd == "clear":
        clear_outbox()
        print("🗑️ Outbox cleared")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
