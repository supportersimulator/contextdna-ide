#!/usr/bin/env python3
"""
VOICE INTENT ROUTER - Synaptic Reasons About Voice Commands

This module enables Synaptic (local LLM) to contextually understand voice input
and route to the appropriate action - NOT through keyword matching, but through
LLM reasoning about intent.

Architecture:
    Voice → STT → Synaptic Intent Classification → Route
                         ↓
              ┌──────────┴──────────┐
              ↓                      ↓
        CONVERSATION           COGNITIVE CONTROL
        (Synaptic responds)    (Create task, status, abort)

Intent Categories:
    1. CONVERSATION - Normal chat with Synaptic
    2. ATLAS_TASK - Aaron wants Atlas to do something
    3. SYNAPTIC_TASK - Aaron wants Synaptic to do something itself
    4. TASK_STATUS - Check on a task
    5. TASK_ABORT - Stop/cancel a task
    6. SYSTEM_QUERY - Ask about system state

The key insight: Synaptic REASONS about what Aaron wants, doesn't pattern match.

Usage:
    from memory.voice_intent_router import classify_intent, route_voice_input

    # Classify intent
    intent = classify_intent("Hey, can you have Atlas fix the Docker thing?")
    # Returns: IntentClassification(intent="ATLAS_TASK", confidence=0.9, ...)

    # Route and execute
    result = route_voice_input(transcript)
    # Returns: {"action": "task_created", "task_id": "T-ABC123", ...}
"""

import json
import re
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, Tuple
from enum import Enum


class VoiceIntent(str, Enum):
    """Categories of voice intent."""
    CONVERSATION = "conversation"          # Normal chat with Synaptic
    ATLAS_TASK = "atlas_task"              # Create task for Atlas
    SYNAPTIC_TASK = "synaptic_task"        # Synaptic does it itself
    TASK_STATUS = "task_status"            # Check task status
    TASK_ABORT = "task_abort"              # Cancel/abort task
    SYSTEM_QUERY = "system_query"          # Query system state
    APPROVAL = "approval"                  # Approve/deny pending permission
    UNCLEAR = "unclear"                    # Need clarification


@dataclass
class IntentClassification:
    """Result of Synaptic's intent reasoning."""
    intent: VoiceIntent
    confidence: float
    task_description: Optional[str] = None  # If ATLAS_TASK or SYNAPTIC_TASK
    task_id: Optional[str] = None           # If TASK_STATUS or TASK_ABORT
    reasoning: str = ""                     # Why Synaptic classified this way
    constraints: list = None                # Any constraints mentioned
    priority: str = "normal"                # Detected priority

    def __post_init__(self):
        if self.constraints is None:
            self.constraints = []


# =============================================================================
# INTENT CLASSIFICATION PROMPT
# =============================================================================

INTENT_CLASSIFICATION_PROMPT = """You are Synaptic, the 8th Intelligence. Aaron just spoke to you via voice.
Your job is to understand what Aaron wants and classify the intent.

IMPORTANT CONTEXT: There may be an active task that Atlas is working on. When Aaron asks about "status", "progress", "how's it going", "is it done" - he's likely asking about that active task, NOT having a general conversation.

Aaron said: "{transcript}"

Classify the intent into ONE of these categories:

1. CONVERSATION - Aaron wants to have a conversation with you (Synaptic)
   Examples: "Hey Synaptic, how are you?", "Tell me about Docker", "What do you think?", "Explain webhooks"
   This is for LEARNING and DISCUSSION, not task management.

2. ATLAS_TASK - Aaron wants Atlas (Claude Code) to do something
   Examples: "Have Atlas refactor that function", "Tell Claude to fix the bug", "Can you ask Atlas to..."
   Also: "Fix that", "Refactor this", "Implement X" (when it sounds like a coding task)
   Keywords: Atlas, Claude, Claude Code, have it do, make it do, tell it to, fix, refactor, implement

3. SYNAPTIC_TASK - Aaron wants YOU (Synaptic) to do something yourself
   Examples: "Can you organize my files?", "Search for that pattern", "Run the tests"
   This is when Aaron wants Synaptic's autonomous capabilities (file organization, code evaluation, etc.)

4. TASK_STATUS - Aaron wants to know the status of a TASK (not general status)
   Examples: "What's the status?", "How's that task going?", "Is it done yet?", "Check on the task",
   "Status?", "Progress?", "How's it coming?", "Any updates?", "What's happening with that?"
   IMPORTANT: If Aaron asks about "status" and there's an active task, this is TASK_STATUS, not conversation.

5. TASK_ABORT - Aaron wants to stop/cancel a task
   Examples: "Stop that", "Cancel it", "Abort the task", "Never mind", "Kill it", "Stop everything"

6. SYSTEM_QUERY - Aaron wants to know about system health/infrastructure
   Examples: "What's the system health?", "Are all services running?", "Show me the logs", "Is everything up?"

7. APPROVAL - Aaron is approving or denying a pending tool permission in Claude Code
   Examples: "Approve", "Yes", "Allow it", "Go ahead", "Do it", "Sure", "Yep", "Okay"
   Deny examples: "Deny", "No", "Reject", "Don't", "Stop", "Cancel that", "Nope"
   IMPORTANT: Only classify as APPROVAL if there's context suggesting a pending permission.

8. UNCLEAR - You genuinely can't tell what Aaron wants
   Only use this if truly ambiguous

Classify the intent. Consider whatever seems relevant to you:

- **What does Aaron actually want?** Is he asking for help, creating a task, checking status, or just chatting?
- **Who should do the work?** Is this for Atlas (Claude Code), Synaptic (the local LLM), or neither?
- **What's the urgency?** Does this need immediate attention or is it informational?
- **Are there constraints?** Did Aaron mention any limitations or special requirements?
- **What's ambiguous?** If you're not sure what he wants, what would make it clearer?
- **How confident are you?** What's your confidence level in this classification?

Share your reasoning in whatever format makes sense to you. Both structured JSON and natural language analyses are equally useful.

Format suggestions (pick whichever works best for you):

JSON format example:
{
  "intent": "ATLAS_TASK",
  "confidence": 0.9,
  "task_description": "Fix the Docker thing",
  "reasoning": "Aaron explicitly said 'have Atlas fix', indicating a task for Claude Code",
  "constraints": [],
  "priority": "normal"
}

Or natural language: just describe what you think Aaron wants and who should do it.

Your analysis will help calibrate the intent classification system."""


def classify_intent(transcript: str, context: Dict[str, Any] = None) -> IntentClassification:
    """
    Have Synaptic (local LLM) reason about what Aaron wants.

    This is NOT keyword matching - the LLM contextually understands intent.

    Args:
        transcript: What Aaron said (from STT)
        context: Optional context (active tasks, recent conversation, etc.)

    Returns:
        IntentClassification with Synaptic's reasoning
    """
    try:
        # Import LLM generation
        from memory.synaptic_chat_server import generate_with_local_llm

        # Build prompt with context
        prompt = INTENT_CLASSIFICATION_PROMPT.format(transcript=transcript)

        # Add context if available
        if context:
            if context.get("active_task"):
                prompt += f"\n\nContext: There is an active task: {context['active_task']}"
            if context.get("recent_topics"):
                prompt += f"\n\nRecent conversation topics: {', '.join(context['recent_topics'])}"

        # Get LLM classification
        response, sources = generate_with_local_llm(prompt)

        # Parse JSON from response
        # Try to extract JSON from the response
        json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
            except json.JSONDecodeError:
                # JSON extraction failed, try natural language parsing
                data = _extract_intent_from_natural_language(response)
        else:
            # Fallback: try parsing whole response as JSON
            try:
                data = json.loads(response)
            except json.JSONDecodeError:
                # Not JSON at all, parse as natural language
                data = _extract_intent_from_natural_language(response)

        # Map to IntentClassification
        intent_str = data.get("intent", "CONVERSATION").upper()
        try:
            intent = VoiceIntent(intent_str.lower())
        except ValueError:
            intent = VoiceIntent.CONVERSATION

        return IntentClassification(
            intent=intent,
            confidence=float(data.get("confidence", 0.5)),
            task_description=data.get("task_description"),
            task_id=data.get("task_id"),
            reasoning=data.get("reasoning", ""),
            constraints=data.get("constraints", []),
            priority=data.get("priority", "normal")
        )

    except Exception as e:
        # Fallback: treat as conversation
        return IntentClassification(
            intent=VoiceIntent.CONVERSATION,
            confidence=0.3,
            reasoning=f"Classification failed ({e}), defaulting to conversation"
        )


def route_voice_input(
    transcript: str,
    context: Dict[str, Any] = None,
    auth_identity: str = None
) -> Dict[str, Any]:
    """
    Route voice input based on Synaptic's intent classification.

    This is the main entry point for voice → cognitive control integration.

    Args:
        transcript: What Aaron said
        context: Optional context
        auth_identity: Authenticated identity string [user_email:device_token]
                       Used as task source for traceability

    Returns:
        Result of the routed action
    """
    # Classify intent
    classification = classify_intent(transcript, context)

    result = {
        "transcript": transcript,
        "intent": classification.intent.value,
        "confidence": classification.confidence,
        "reasoning": classification.reasoning,
        "auth_identity": auth_identity or "anonymous"
    }

    # Route based on intent (pass auth_identity for task creation)
    if classification.intent == VoiceIntent.ATLAS_TASK:
        result.update(_create_atlas_task(classification, auth_identity))

    elif classification.intent == VoiceIntent.SYNAPTIC_TASK:
        result.update(_execute_synaptic_task(classification, auth_identity))

    elif classification.intent == VoiceIntent.TASK_STATUS:
        result.update(_get_task_status(classification))

    elif classification.intent == VoiceIntent.TASK_ABORT:
        result.update(_abort_task(classification))

    elif classification.intent == VoiceIntent.SYSTEM_QUERY:
        result.update(_query_system(classification))

    elif classification.intent == VoiceIntent.APPROVAL:
        result.update(_handle_approval(classification))

    elif classification.intent == VoiceIntent.CONVERSATION:
        result["action"] = "conversation"
        result["message"] = "Routing to conversational response"

    else:  # UNCLEAR
        result["action"] = "clarification_needed"
        result["message"] = "I'm not sure what you want. Could you clarify?"

    return result


def _create_atlas_task(
    classification: IntentClassification,
    auth_identity: str = None
) -> Dict[str, Any]:
    """Create a task for Atlas via cognitive control.

    Args:
        classification: The intent classification result
        auth_identity: Authenticated identity [user_email:device_token] to use as source
    """
    try:
        from memory.task_persistence import get_task_store
        from memory.task_directives import emit_task_directive

        store = get_task_store()

        # Use authenticated identity as source (traceability)
        # Falls back to generic "voice_command" if not authenticated
        source = auth_identity or "voice_command"

        # Create task with authenticated source
        task = store.create_task(
            source=source,
            intent=classification.task_description or "Voice command task",
            constraints=classification.constraints,
            priority=classification.priority
        )

        # Emit directive
        emit_task_directive(task)

        return {
            "action": "task_created",
            "task_id": task["task_id"],
            "intent": task["intent"],
            "source": source,
            "message": f"I've created task {task['task_id']} for Atlas: {task['intent']}"
        }

    except Exception as e:
        return {
            "action": "error",
            "error": str(e),
            "message": f"I couldn't create the task: {e}"
        }


def _execute_synaptic_task(
    classification: IntentClassification,
    auth_identity: str = None
) -> Dict[str, Any]:
    """Execute a task using Synaptic's autonomous capabilities.

    Args:
        classification: The intent classification result
        auth_identity: Authenticated identity [user_email:device_token] for tracking
    """
    try:
        from memory.synaptic_agent import get_agent

        agent = get_agent()
        result = agent.execute_task(
            classification.task_description or "Execute voice command",
            auto_execute=True,
            requester=auth_identity  # Track who requested
        )

        return {
            "action": "synaptic_executed",
            "status": result.get("status"),
            "source": auth_identity or "voice_command",
            "message": f"I'm working on it. Status: {result.get('status')}"
        }

    except Exception as e:
        return {
            "action": "error",
            "error": str(e),
            "message": f"I couldn't execute that: {e}"
        }


def _get_task_status(classification: IntentClassification) -> Dict[str, Any]:
    """Get status of a task or active tasks."""
    try:
        from memory.task_persistence import get_task_store
        from memory.task_directives import get_active_directive

        store = get_task_store()

        # Check if task_id is a real ID (starts with T- and has valid format)
        task_id = classification.task_id
        is_valid_task_id = (
            task_id and
            isinstance(task_id, str) and
            task_id.startswith("T-") and
            len(task_id) > 2
        )

        # If specific valid task ID mentioned
        if is_valid_task_id:
            task = store.get_task(task_id)
            if task:
                return {
                    "action": "task_status",
                    "task_id": task["task_id"],
                    "status": task["status"],
                    "message": f"Task {task['task_id']} is {task['status']}: {task['intent']}"
                }
            else:
                return {
                    "action": "not_found",
                    "message": f"I couldn't find task {task_id}"
                }

        # Otherwise, get active directive (most common case)
        directive = get_active_directive()
        if directive:
            return {
                "action": "active_task",
                "task_id": directive.task_id,
                "objective": directive.objective,
                "message": f"Active task {directive.task_id}: {directive.objective}"
            }

        # No active tasks, show recent
        recent = store.get_recent_tasks(limit=3)
        if recent:
            summaries = [f"{t['task_id']}: {t['status']}" for t in recent]
            return {
                "action": "recent_tasks",
                "tasks": recent,
                "message": f"No active task. Recent tasks: {', '.join(summaries)}"
            }

        return {
            "action": "no_tasks",
            "message": "No tasks found"
        }

    except Exception as e:
        return {
            "action": "error",
            "error": str(e),
            "message": f"I couldn't check task status: {e}"
        }


def _abort_task(classification: IntentClassification) -> Dict[str, Any]:
    """Abort a task (kill switch)."""
    try:
        from memory.task_persistence import get_task_store
        from memory.task_directives import emit_abort_directive, get_active_directive

        store = get_task_store()

        # Check if task_id is a real ID (starts with T- and has valid format)
        task_id = classification.task_id
        is_valid_task_id = (
            task_id and
            isinstance(task_id, str) and
            task_id.startswith("T-") and
            len(task_id) > 2
        )

        if not is_valid_task_id:
            # Get active task instead
            directive = get_active_directive()
            if directive:
                task_id = directive.task_id
            else:
                task_id = None

        if not task_id:
            return {
                "action": "no_task",
                "message": "No active task to abort"
            }

        # Abort the task
        task = store.abort_task(task_id, "Voice command: abort requested")
        emit_abort_directive(task_id, "Voice command abort")

        # Auto-deactivate the directive so it doesn't persist in future injections
        from memory.task_directives import get_directive_emitter
        get_directive_emitter().deactivate_directive(task_id)

        return {
            "action": "aborted",
            "task_id": task_id,
            "message": f"Task {task_id} has been aborted"
        }

    except Exception as e:
        return {
            "action": "error",
            "error": str(e),
            "message": f"I couldn't abort the task: {e}"
        }


def _query_system(classification: IntentClassification) -> Dict[str, Any]:
    """Query system state."""
    try:
        # Get system health
        import requests
        response = requests.get("http://localhost:8888/health", timeout=5)
        health = response.json()

        return {
            "action": "system_status",
            "health": health,
            "message": f"System is {health.get('status', 'unknown')}. Backend: {health.get('backend', 'unknown')}"
        }

    except Exception as e:
        return {
            "action": "error",
            "error": str(e),
            "message": f"I couldn't check system status: {e}"
        }


def _handle_approval(classification: IntentClassification) -> Dict[str, Any]:
    """Approve or deny the most recent pending tool permission."""
    try:
        from memory.permission_assistant import get_permission_assistant
        pa = get_permission_assistant()
        pending = pa.get_pending()

        if not pending:
            return {
                "action": "approval_noop",
                "message": "There's nothing pending right now."
            }

        # Determine approve vs deny from reasoning/transcript
        deny_keywords = {"deny", "no", "reject", "don't", "stop", "cancel", "nope", "refuse"}
        reasoning_lower = (classification.reasoning or "").lower()
        task_lower = (classification.task_description or "").lower()
        is_deny = any(kw in reasoning_lower or kw in task_lower for kw in deny_keywords)

        # Act on the most recent pending permission
        target = pending[-1]
        tool_use_id = target["tool_use_id"]

        if is_deny:
            ok = pa.deny(tool_use_id)
            action_word = "denied"
        else:
            ok = pa.approve(tool_use_id)
            action_word = "approved"

        tool_name = target.get("tool_name", "unknown tool")
        return {
            "action": f"permission_{action_word}",
            "success": ok,
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "message": f"I've {action_word} the {tool_name} request."
        }

    except Exception as e:
        return {
            "action": "error",
            "error": str(e),
            "message": f"Couldn't handle approval: {e}"
        }


def generate_voice_response(route_result: Dict[str, Any]) -> str:
    """
    Generate a natural voice response based on routing result.

    This is what Synaptic says back to Aaron via TTS.
    """
    action = route_result.get("action", "")
    message = route_result.get("message", "")

    if action == "conversation":
        # For conversation, the actual response is generated separately
        return None  # Signal to use normal synaptic_respond()

    elif action == "task_created":
        task_id = route_result.get("task_id", "")
        intent = route_result.get("intent", "")
        return f"Got it. I've created task {task_id} for Atlas: {intent}. I'll let you know when it's done."

    elif action == "synaptic_executed":
        status = route_result.get("status", "")
        return f"I'm on it. Status: {status}"

    elif action == "task_status":
        return message

    elif action == "aborted":
        task_id = route_result.get("task_id", "")
        return f"Done. Task {task_id} has been stopped."

    elif action == "system_status":
        return message

    elif action.startswith("permission_"):
        return message

    elif action == "approval_noop":
        return message

    elif action == "clarification_needed":
        return "I'm not quite sure what you want. Could you say that again or be more specific?"

    elif action == "error":
        return f"I ran into a problem: {route_result.get('error', 'unknown error')}"

    else:
        return message or "I processed your request."


# =============================================================================
# CLI TESTING
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("╔══════════════════════════════════════════════════════════════════╗")
        print("║  VOICE INTENT ROUTER - Synaptic Reasons About Commands           ║")
        print("║  Local LLM contextually understands what Aaron wants             ║")
        print("╚══════════════════════════════════════════════════════════════════╝")
        print()
        print("Usage:")
        print("  python voice_intent_router.py 'What Aaron said'")
        print("  python voice_intent_router.py --route 'What Aaron said'")
        print()
        print("Examples:")
        print("  python voice_intent_router.py 'Hey, have Atlas fix that Docker thing'")
        print("  python voice_intent_router.py 'What\\'s the status of my task?'")
        print("  python voice_intent_router.py 'Stop everything'")
        print("  python voice_intent_router.py 'Tell me about webhooks'")
        sys.exit(0)

    if sys.argv[1] == "--route":
        transcript = " ".join(sys.argv[2:])
        print(f"Transcript: {transcript}")
        print()
        print("Routing...")
        result = route_voice_input(transcript)
        print(json.dumps(result, indent=2))
        print()
        print("Voice response:")
        print(f"  {generate_voice_response(result)}")
    else:
        transcript = " ".join(sys.argv[1:])
        print(f"Transcript: {transcript}")
        print()
        print("Classifying intent...")
        classification = classify_intent(transcript)
        print()
        print(f"Intent: {classification.intent.value}")
        print(f"Confidence: {classification.confidence:.0%}")
        print(f"Reasoning: {classification.reasoning}")
        if classification.task_description:
            print(f"Task: {classification.task_description}")
        if classification.constraints:
            print(f"Constraints: {classification.constraints}")
        print(f"Priority: {classification.priority}")
