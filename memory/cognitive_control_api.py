#!/usr/bin/env python3
"""
COGNITIVE CONTROL API - Phone → Synaptic → Atlas Remote Control

This module provides the API endpoints for the cognitive control architecture
designed by Logos.

Key Principle (Logos):
    "You are not controlling VS Code. You are controlling cognition,
     with VS Code as the execution surface."

Endpoints:
    POST /command  - Receive intent from phone, create task, emit directive
    GET  /report   - Get task status for phone reporting
    POST /abort    - Emergency kill switch
    GET  /tasks    - List active and recent tasks

Architecture:
    Phone → /command → TaskStore.create_task() → DirectiveEmitter.emit()
                                                           ↓
                                               Section 6 Webhook Injection
                                                           ↓
                                                     Atlas Executes
                                                           ↓
    Phone ← /report ← TaskStore.get_task() ← SynapticReview.evaluate()

Usage:
    These endpoints are registered with the main Synaptic chat server.

    from memory.cognitive_control_api import register_cognitive_control_routes
    register_cognitive_control_routes(app)
"""

from datetime import datetime, timezone
from typing import Optional, List
from pydantic import BaseModel

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from memory.task_persistence import get_task_store, TaskStore
from memory.task_directives import (
    emit_task_directive,
    emit_revision_directive,
    emit_abort_directive,
    get_active_directive,
    acknowledge_directive,
    get_directive_webhook_content,
)
from memory.auto_reviewer import trigger_review


# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================

class CommandRequest(BaseModel):
    """Request to create a new task from phone.

    source should be the authenticated identity [user_id:device_token]
    from the voice session token. If not authenticated, use "api_direct".
    user_id is the Supabase UUID (stable, no PII in task logs).
    """
    source: str  # Required - [user_id:device_token] from auth, no default
    intent: str
    priority: str = "normal"
    constraints: List[str] = []


class CommandResponse(BaseModel):
    """Response after creating a task."""
    task_id: str
    status: str
    message: str


class ReportResponse(BaseModel):
    """Task report for phone."""
    task_id: str
    status: str
    intent: str
    created_at: str
    updated_at: str
    output_preview: Optional[str] = None
    synaptic_review: Optional[dict] = None


class AbortRequest(BaseModel):
    """Request to abort a task."""
    task_id: str
    reason: str = "User requested abort"


class AtlasOutputRequest(BaseModel):
    """Request from Atlas to record task output."""
    task_id: str
    output: str


class ReviewRequest(BaseModel):
    """Request for Synaptic to review Atlas's work."""
    task_id: str
    outcome: str  # pass | needs_revision | fail
    confidence: float = 0.5
    notes: str = ""
    followups: List[str] = []


class ProgressRequest(BaseModel):
    """Request from Atlas to report progress on a task.

    Three-tier model (Logos Priority 1):
    - stage: Current work stage (reading_code, planning, implementing, testing)
    - percentage: 0-100 for measurable progress
    - heartbeat: Regular pings (~2.5s) to show activity
    """
    task_id: str
    event_type: str  # stage | percentage | heartbeat
    stage: Optional[str] = None
    percentage: Optional[int] = None
    message: Optional[str] = None


class TerminalRequest(BaseModel):
    """Request from Atlas to mark task completion/failure/blocked.

    outcome types:
    - completed: Task finished, ready for Synaptic review
    - failed: Task could not be completed
    - blocked: Needs user input to proceed
    """
    task_id: str
    outcome: str  # completed | failed | blocked
    summary: str
    files_changed: List[str] = []
    blocker_reason: Optional[str] = None


# =============================================================================
# ENDPOINT HANDLERS
# =============================================================================

def register_cognitive_control_routes(app: FastAPI):
    """
    Register cognitive control endpoints with the FastAPI app.

    This is called from synaptic_chat_server.py to add these routes.
    """

    @app.post("/command", response_model=CommandResponse)
    async def command(request: CommandRequest):
        """
        POST /command - Receive intent from phone, create task, emit directive.

        This is the primary intake endpoint for remote cognitive control.

        Process:
        1. Create task in TaskStore (durable)
        2. Emit Section 6 directive (Atlas sees it)
        3. Return task_id for tracking

        Example:
            POST /command
            {
                "source": "[550e8400:abc12345]",  # Authenticated identity (user_id:device_token)
                "intent": "Refactor Docker health check logic",
                "priority": "normal",
                "constraints": ["Do not modify production files"]
            }

            Response:
            {
                "task_id": "T-ABC123",
                "status": "pending",
                "message": "Task created and directive emitted"
            }
        """
        store = get_task_store()

        # Create the task
        task = store.create_task(
            source=request.source,
            intent=request.intent,
            constraints=request.constraints,
            priority=request.priority
        )

        # Emit directive to Atlas via Section 6
        emit_task_directive(task)

        return CommandResponse(
            task_id=task["task_id"],
            status="pending",
            message="Task created and directive emitted to Atlas"
        )

    @app.get("/report")
    async def report(task_id: str):
        """
        GET /report?task_id=T-ABC123 - Get task status for phone reporting.

        This closes the loop back to the phone. Returns:
        - Current status
        - Output preview (if Atlas completed work)
        - Synaptic's review (if reviewed)

        Example:
            GET /report?task_id=T-ABC123

            Response:
            {
                "task_id": "T-ABC123",
                "status": "completed",
                "intent": "Refactor Docker health check logic",
                "output_preview": "Refactored with retry logic...",
                "synaptic_review": {
                    "outcome": "pass",
                    "confidence": 0.93,
                    "notes": "Clean implementation"
                }
            }
        """
        store = get_task_store()

        summary = store.get_task_summary(task_id)
        if not summary:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

        return summary

    @app.post("/abort")
    async def abort(request: AbortRequest):
        """
        POST /abort - Emergency kill switch.

        Immediately:
        1. Updates task status to "aborted"
        2. Emits abort directive to Atlas
        3. Deactivates all directives for this task

        Example:
            POST /abort
            {
                "task_id": "T-ABC123",
                "reason": "User requested abort"
            }
        """
        store = get_task_store()

        try:
            # Abort in TaskStore
            task = store.abort_task(request.task_id, request.reason)

            # Emit abort directive
            emit_abort_directive(request.task_id, request.reason)

            return {
                "status": "aborted",
                "task_id": request.task_id,
                "reason": request.reason,
                "message": "Task aborted and kill directive emitted"
            }
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/tasks")
    async def list_tasks(status: Optional[str] = None, limit: int = 20):
        """
        GET /tasks - List tasks.

        Optional filters:
        - status: Filter by status (pending, executing, reviewing, completed, failed, aborted)
        - limit: Maximum number of tasks (default 20)

        Example:
            GET /tasks?status=pending
            GET /tasks?limit=5
        """
        store = get_task_store()

        if status:
            tasks = store.get_tasks_by_status(status, limit=limit)
        else:
            tasks = store.get_recent_tasks(limit=limit)

        return {
            "count": len(tasks),
            "tasks": [
                {
                    "task_id": t["task_id"],
                    "status": t["status"],
                    "intent": t["intent"][:100],
                    "source": t.get("source"),
                    "created_at": t["created_at"]
                }
                for t in tasks
            ]
        }

    @app.get("/tasks/active")
    async def active_tasks():
        """
        GET /tasks/active - Get currently active tasks (pending or executing).
        """
        store = get_task_store()
        tasks = store.get_active_tasks()

        return {
            "count": len(tasks),
            "tasks": tasks
        }

    @app.post("/atlas/output")
    async def atlas_output(request: AtlasOutputRequest):
        """
        POST /atlas/output - Atlas records its work output.

        This is called by Atlas (or by the webhook system) when Atlas
        completes work on a task. It:
        1. Records the output
        2. Moves task to "reviewing" status
        3. (Optionally) triggers Synaptic review

        Example:
            POST /atlas/output
            {
                "task_id": "T-ABC123",
                "output": "Refactored the Docker health check with retry logic..."
            }
        """
        store = get_task_store()

        try:
            task = store.add_atlas_output(request.task_id, request.output)
            return {
                "status": "recorded",
                "task_id": request.task_id,
                "new_status": task["status"],
                "message": "Output recorded, task moved to reviewing"
            }
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/atlas/progress")
    async def atlas_progress(request: ProgressRequest):
        """
        POST /atlas/progress - Atlas reports progress on a task.

        Three-tier progress model (Logos Priority 1):
        - stage: "reading_code", "planning", "implementing", "testing"
        - percentage: 0-100 for measurable operations
        - heartbeat: Regular pings (~2.5s) to show activity

        This enables:
        - Real-time progress streaming to voice
        - Task monitoring dashboard
        - Timeout detection (no heartbeat = stalled)

        Example:
            POST /atlas/progress
            {
                "task_id": "T-ABC123",
                "event_type": "stage",
                "stage": "implementing",
                "message": "Modifying task_persistence.py"
            }
        """
        store = get_task_store()

        try:
            event = store.add_progress_event(
                task_id=request.task_id,
                event_type=request.event_type,
                stage=request.stage,
                percentage=request.percentage,
                message=request.message
            )

            # Broadcast to WebSocket subscribers (Logos Priority 3)
            try:
                from memory.progress_broadcaster import broadcast_progress
                await broadcast_progress(request.task_id, {
                    "event_type": event.event_type,
                    "stage": event.stage,
                    "percentage": event.percentage,
                    "message": event.message,
                    "timestamp": event.timestamp
                })
            except ImportError:
                pass  # Broadcaster not available

            return {
                "status": "recorded",
                "task_id": request.task_id,
                "event_type": event.event_type,
                "timestamp": event.timestamp,
                "message": "Progress event recorded"
            }
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/atlas/terminal")
    async def atlas_terminal(request: TerminalRequest):
        """
        POST /atlas/terminal - Atlas marks task as completed/failed/blocked.

        This is the definitive end of Atlas's work on a task:
        - completed: Ready for Synaptic review (auto-review triggered)
        - failed: Could not complete (with reason)
        - blocked: Needs user input to proceed

        Example:
            POST /atlas/terminal
            {
                "task_id": "T-ABC123",
                "outcome": "completed",
                "summary": "Added progress tracking to cognitive control API",
                "files_changed": ["memory/task_persistence.py", "memory/cognitive_control_api.py"]
            }
        """
        store = get_task_store()

        try:
            event = store.set_terminal_event(
                task_id=request.task_id,
                outcome=request.outcome,
                summary=request.summary,
                files_changed=request.files_changed,
                blocker_reason=request.blocker_reason
            )

            response = {
                "status": event.outcome,
                "task_id": request.task_id,
                "new_status": "reviewing" if event.outcome == "completed" else event.outcome,
                "timestamp": event.timestamp,
                "message": f"Task marked as {event.outcome}"
            }

            # Trigger auto-review for completed tasks (Logos Priority 2)
            if request.outcome == "completed":
                try:
                    review = await trigger_review(request.task_id)
                    response["auto_review"] = {
                        "outcome": review.outcome,
                        "confidence": review.confidence,
                        "notes": review.notes,
                        "review_method": review.review_method
                    }
                    response["new_status"] = "completed" if review.outcome == "pass" else "pending"
                except Exception as e:
                    # Log but don't fail - review can be done manually
                    response["auto_review_error"] = str(e)

            # Broadcast terminal event to WebSocket subscribers (Logos Priority 3)
            try:
                from memory.progress_broadcaster import broadcast_terminal
                await broadcast_terminal(request.task_id, {
                    "outcome": event.outcome,
                    "summary": event.summary,
                    "files_changed": event.files_changed,
                    "timestamp": event.timestamp,
                    "auto_review": response.get("auto_review")
                })
            except ImportError:
                pass  # Broadcaster not available

            return response
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/atlas/progress/{task_id}")
    async def get_progress(task_id: str, limit: int = 20):
        """
        GET /atlas/progress/{task_id} - Get progress events for a task.

        Returns most recent progress events for monitoring/streaming.

        Example:
            GET /atlas/progress/T-ABC123?limit=10
        """
        store = get_task_store()

        # Verify task exists
        task = store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

        events = store.get_progress_events(task_id, limit=limit)
        latest = events[0] if events else None

        return {
            "task_id": task_id,
            "task_status": task["status"],
            "event_count": len(events),
            "latest": latest,
            "events": events
        }

    @app.post("/review")
    async def synaptic_review(request: ReviewRequest):
        """
        POST /review - Synaptic reviews Atlas's work.

        This is the review gate. Synaptic evaluates Atlas's output and:
        - "pass" → Task completed successfully
        - "needs_revision" → Task goes back to pending with revision directive
        - "fail" → Task marked as failed

        Example:
            POST /review
            {
                "task_id": "T-ABC123",
                "outcome": "pass",
                "confidence": 0.93,
                "notes": "Clean implementation with proper error handling"
            }
        """
        store = get_task_store()

        try:
            task = store.add_synaptic_review(
                request.task_id,
                {
                    "outcome": request.outcome,
                    "confidence": request.confidence,
                    "notes": request.notes,
                    "followups": request.followups
                }
            )

            # If needs revision, emit revision directive
            if request.outcome == "needs_revision":
                emit_revision_directive(task, request.notes)
                return {
                    "status": "revision_requested",
                    "task_id": request.task_id,
                    "message": "Revision directive emitted to Atlas"
                }

            return {
                "status": task["status"],
                "task_id": request.task_id,
                "outcome": request.outcome,
                "message": f"Task reviewed: {request.outcome}"
            }
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/directive/active")
    async def active_directive():
        """
        GET /directive/active - Get the currently active directive.

        This shows what Atlas should currently be working on.
        """
        directive = get_active_directive()

        if not directive:
            return {"active": False, "message": "No active directive"}

        return {
            "active": True,
            "task_id": directive.task_id,
            "directive_type": directive.directive_type,
            "objective": directive.objective,
            "constraints": directive.constraints,
            "priority": directive.priority,
            "acknowledged": directive.acknowledged
        }

    @app.post("/directive/acknowledge")
    async def ack_directive(task_id: str):
        """
        POST /directive/acknowledge?task_id=T-ABC123 - Atlas acknowledges directive.

        Atlas should call this when it starts work on a task.
        """
        if acknowledge_directive(task_id):
            return {"status": "acknowledged", "task_id": task_id}
        else:
            raise HTTPException(status_code=404, detail=f"No active directive for {task_id}")

    @app.get("/directive/webhook")
    async def directive_webhook_content():
        """
        GET /directive/webhook - Get formatted directive for webhook injection.

        This returns the Section 6 content that should be injected into
        Atlas's context.
        """
        content = get_directive_webhook_content()

        if not content:
            return {"has_directive": False, "content": ""}

        return {"has_directive": True, "content": content}


# =============================================================================
# STANDALONE MODE
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    # Create a standalone app for testing
    app = FastAPI(
        title="Cognitive Control API",
        description="Phone → Synaptic → Atlas Remote Control (Logos Architecture)",
        version="1.0.0"
    )

    register_cognitive_control_routes(app)

    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║  COGNITIVE CONTROL API - Standalone Mode                         ║")
    print("║  Phone → Synaptic → Atlas (Logos Architecture)                   ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print()
    print("Endpoints:")
    print("  POST /command        - Create task from phone intent")
    print("  GET  /report         - Get task status for phone")
    print("  POST /abort          - Kill switch")
    print("  GET  /tasks          - List tasks")
    print("  GET  /tasks/active   - List active tasks")
    print("  POST /atlas/output   - Record Atlas output")
    print("  POST /review         - Synaptic review")
    print("  GET  /directive/*    - Directive management")
    print()

    uvicorn.run(app, host="127.0.0.1", port=8889)
