"""
permission_assistant.py — Synaptic explains and acts on Claude Code tool approvals.

Detection: JSONL tool_use without matching tool_result after 2s = pending approval
Explanation: Qwen3-14B generates plain-language description
Action: osascript sends keystroke to Claude Code (VS Code or Terminal)

Architecture:
  session_file_watcher detects tool_use → record_tool_use()
  2s timer → if no tool_result → LLM explain → Redis broadcast
  User approves via voice/UI → approve() → osascript keystroke
"""

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, asdict, field
from typing import Optional

logger = logging.getLogger("context_dna.permission_assistant")

PERMISSION_CHANNEL = "session:permission:pending"
PERMISSION_RESOLVED_CHANNEL = "session:permission:resolved"
PENDING_TIMEOUT = 60  # seconds before auto-expiry
ANNOUNCE_DELAY = 2.0  # seconds to wait before announcing (filter auto-approvals)


@dataclass
class PendingPermission:
    tool_use_id: str
    tool_name: str
    tool_input: dict
    session_id: str
    detected_at: float
    explanation: str = ""
    status: str = "pending"  # pending | approved | denied | expired


def _get_redis():
    """Get Redis connection (same pattern as session_file_watcher)."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


class PermissionAssistant:
    """Thread-safe permission detection, explanation, and action."""

    def __init__(self):
        self._pending: dict[str, PendingPermission] = {}
        self._lock = threading.Lock()
        self._cleanup_timer = None
        self._start_cleanup_loop()

    def record_tool_use(self, tool_id: str, tool_name: str, tool_input: dict, session_id: str):
        """Record a tool_use block from JSONL. Starts 2s timer before announcing."""
        perm = PendingPermission(
            tool_use_id=tool_id,
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=session_id,
            detected_at=time.time(),
        )
        with self._lock:
            self._pending[tool_id] = perm

        # 2s delay: if tool_result arrives within 2s, it was auto-approved
        timer = threading.Timer(ANNOUNCE_DELAY, self._announce_if_still_pending, args=[tool_id])
        timer.daemon = True
        timer.start()

    def record_tool_result(self, tool_use_id: str):
        """Record that a tool_result arrived — remove from pending."""
        with self._lock:
            removed = self._pending.pop(tool_use_id, None)

        if removed:
            # Broadcast resolution
            r = _get_redis()
            if r:
                try:
                    r.publish(PERMISSION_RESOLVED_CHANNEL, json.dumps({
                        "tool_use_id": tool_use_id,
                        "tool_name": removed.tool_name,
                    }))
                except Exception:
                    pass

    def _announce_if_still_pending(self, tool_id: str):
        """After 2s delay, if still pending, generate explanation and broadcast."""
        with self._lock:
            perm = self._pending.get(tool_id)
            if not perm or perm.status != "pending":
                return  # Resolved within 2s (auto-approved)

        # Generate LLM explanation
        explanation = self._explain_tool(perm.tool_name, perm.tool_input)
        perm.explanation = explanation

        # Broadcast via Redis
        self._broadcast(perm)
        logger.info(f"Permission pending: {perm.tool_name} ({tool_id[:20]}...)")

    def _explain_tool(self, tool_name: str, tool_input: dict) -> str:
        """Ask Qwen3-14B to explain what this tool does in plain language."""
        try:
            from memory.llm_priority_queue import butler_query
            input_summary = json.dumps(tool_input)[:400]
            explanation = butler_query(
                system_prompt=(
                    "You explain Claude Code tool actions in 1 plain-language sentence. "
                    "Be specific about what will happen. No jargon."
                ),
                user_prompt=f"Tool: {tool_name}\nArguments: {input_summary}",
                profile="coding",
            )
            return (explanation or "").strip() or f"Claude wants to use {tool_name}"
        except Exception as e:
            logger.debug(f"LLM explain failed: {e}")
            # Fallback: construct from tool name + key args
            return self._fallback_explain(tool_name, tool_input)

    def _fallback_explain(self, tool_name: str, tool_input: dict) -> str:
        """Non-LLM fallback explanation based on tool name heuristics."""
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")[:100]
            return f"Claude wants to run a terminal command: {cmd}"
        elif tool_name == "Write":
            path = tool_input.get("file_path", "unknown")
            return f"Claude wants to create/overwrite file: {path}"
        elif tool_name == "Edit":
            path = tool_input.get("file_path", "unknown")
            return f"Claude wants to edit file: {path}"
        elif tool_name == "Read":
            path = tool_input.get("file_path", "unknown")
            return f"Claude wants to read file: {path}"
        elif tool_name == "Task":
            desc = tool_input.get("description", "")[:80]
            return f"Claude wants to spawn a sub-agent: {desc}"
        else:
            return f"Claude wants to use tool: {tool_name}"

    def _broadcast(self, perm: PendingPermission):
        """Push permission request to Redis for all consumers."""
        r = _get_redis()
        if not r:
            return
        try:
            r.publish(PERMISSION_CHANNEL, json.dumps(asdict(perm), default=str))
        except Exception as e:
            logger.debug(f"Redis broadcast failed: {e}")

    def get_pending(self) -> list[dict]:
        """Return all currently pending permissions."""
        with self._lock:
            return [
                asdict(p) for p in self._pending.values()
                if p.status == "pending"
            ]

    def approve(self, tool_use_id: str) -> bool:
        """Approve a pending tool and send keystroke."""
        return self._respond(tool_use_id, approve=True)

    def deny(self, tool_use_id: str) -> bool:
        """Deny a pending tool and send keystroke."""
        return self._respond(tool_use_id, approve=False)

    def _respond(self, tool_use_id: str, approve: bool) -> bool:
        """Send approval/denial keystroke to Claude Code process via osascript."""
        with self._lock:
            perm = self._pending.get(tool_use_id)
            if not perm or perm.status != "pending":
                return False
            perm.status = "approved" if approve else "denied"

        key = "y" if approve else "n"
        action = "Approved" if approve else "Denied"

        try:
            # Bring VS Code to front, then send keystroke
            script = (
                f'tell application "Code" to activate\n'
                f'delay 0.3\n'
                f'tell application "System Events" to keystroke "{key}"\n'
                f'tell application "System Events" to keystroke return'
            )
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"{action} {perm.tool_name} ({tool_use_id[:20]}...)")
            return True
        except Exception as e:
            logger.error(f"osascript failed: {e}")
            return False

    def _start_cleanup_loop(self):
        """Periodically expire stale pending permissions."""
        def _cleanup():
            now = time.time()
            with self._lock:
                expired = [
                    tid for tid, p in self._pending.items()
                    if p.status == "pending" and (now - p.detected_at) > PENDING_TIMEOUT
                ]
                for tid in expired:
                    self._pending[tid].status = "expired"
                    del self._pending[tid]

            if expired:
                logger.debug(f"Expired {len(expired)} stale permissions")

            # Reschedule
            self._cleanup_timer = threading.Timer(10.0, _cleanup)
            self._cleanup_timer.daemon = True
            self._cleanup_timer.start()

        self._cleanup_timer = threading.Timer(10.0, _cleanup)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_assistant: Optional[PermissionAssistant] = None


def get_permission_assistant() -> PermissionAssistant:
    global _assistant
    if _assistant is None:
        _assistant = PermissionAssistant()
    return _assistant
