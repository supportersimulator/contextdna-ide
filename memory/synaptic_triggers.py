#!/usr/bin/env python3
"""
SYNAPTIC TRIGGERS - Command Detection and Execution System

═══════════════════════════════════════════════════════════════════════════
When Aaron says specific trigger phrases, Synaptic executes corresponding
actions automatically. This is how Synaptic "hears" and responds to commands.
═══════════════════════════════════════════════════════════════════════════

REGISTERED TRIGGERS:
  "CODE EVAL"  → Full codebase evaluation (Code Evaluator skill)

FUTURE TRIGGERS:
  "ORGANIZE"   → File organization skill
  "HEALTH"     → Infrastructure health check
  "STATUS"     → Synaptic status report
═══════════════════════════════════════════════════════════════════════════
"""

import os
import re
import sys
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class TriggerResult:
    """Result of a trigger execution."""
    triggered: bool
    trigger_name: str
    message: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    success: bool = False
    details: Dict[str, Any] = field(default_factory=dict)
    background: bool = False  # If running in background


class SynapticTriggers:
    """
    Synaptic's command trigger system.

    Detects trigger phrases in prompts and executes corresponding actions.
    This is how Synaptic responds to explicit commands from Aaron.
    """

    # Registered triggers: pattern -> (handler, description, background)
    _triggers: Dict[str, Tuple[Callable, str, bool]] = {}

    # Stop triggers: pattern -> task_to_stop
    _stop_triggers: Dict[str, str] = {}

    # Cancellation flags for running tasks
    _cancel_flags: Dict[str, bool] = {}

    def __init__(self):
        """Initialize and register all triggers."""
        self._register_triggers()
        self.active_tasks: Dict[str, threading.Thread] = {}
        self.results_dir = Path.home() / ".context-dna" / "trigger_results"
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _register_triggers(self):
        """Register all Synaptic triggers."""

        # CODE EVAL - Full codebase evaluation
        self._triggers["CODE EVAL"] = (
            self._handle_code_eval,
            "Full codebase evaluation - scans all projects, generates fixes, auto-approves",
            True  # Runs in background (can be long)
        )

        # STOP triggers - Aaron can barge in and stop running tasks
        self._stop_triggers["STOP CODE EVAL"] = "CODE EVAL"
        self._stop_triggers["STOP EVAL"] = "CODE EVAL"
        self._stop_triggers["CANCEL CODE EVAL"] = "CODE EVAL"

        # Future triggers (placeholder)
        # self._triggers["ORGANIZE"] = (self._handle_organize, "File organization skill", True)
        # self._triggers["HEALTH"] = (self._handle_health_check, "Infrastructure health check", False)
        # self._triggers["STATUS"] = (self._handle_status, "Synaptic status report", False)

    def check_prompt(self, prompt: str) -> Optional[TriggerResult]:
        """
        Check if a prompt contains any triggers.

        Args:
            prompt: The user's prompt text

        Returns:
            TriggerResult if a trigger was found and executed, None otherwise
        """
        # Normalize prompt for checking
        prompt_upper = prompt.upper().strip()

        # =====================================================================
        # FIRST: Check for STOP triggers (Aaron barging in to stop a task)
        # =====================================================================
        for stop_phrase, task_to_stop in self._stop_triggers.items():
            pattern = rf'\b{re.escape(stop_phrase)}\b'
            if re.search(pattern, prompt_upper):
                return self._handle_stop_trigger(stop_phrase, task_to_stop)

        # =====================================================================
        # THEN: Check for regular triggers
        # =====================================================================
        for trigger_phrase, (handler, description, background) in self._triggers.items():
            # Check for trigger phrase (must be standalone or at boundaries)
            # Matches: "CODE EVAL", "CODE EVAL please", "run CODE EVAL", etc.
            pattern = rf'\b{re.escape(trigger_phrase)}\b'
            if re.search(pattern, prompt_upper):
                # Trigger found!
                result = TriggerResult(
                    triggered=True,
                    trigger_name=trigger_phrase,
                    message=f"Synaptic detected trigger: {trigger_phrase}",
                    started_at=datetime.now(),
                    background=background
                )

                # Clear any previous cancel flag
                self._cancel_flags[trigger_phrase] = False

                if background:
                    # Run in background thread
                    result.message = f"Synaptic starting: {description}"
                    thread = threading.Thread(
                        target=self._run_handler_background,
                        args=(trigger_phrase, handler, result),
                        daemon=True
                    )
                    self.active_tasks[trigger_phrase] = thread
                    thread.start()
                else:
                    # Run synchronously
                    try:
                        handler_result = handler()
                        result.success = handler_result.get("success", False)
                        result.details = handler_result
                        result.completed_at = datetime.now()
                        result.message = handler_result.get("message", f"{trigger_phrase} completed")
                    except Exception as e:
                        result.success = False
                        result.message = f"Trigger failed: {e}"
                        result.completed_at = datetime.now()

                return result

        # No trigger found
        return None

    def _handle_stop_trigger(self, stop_phrase: str, task_to_stop: str) -> TriggerResult:
        """
        Handle a STOP trigger - Aaron barging in to cancel a running task.

        Args:
            stop_phrase: The stop phrase detected (e.g., "STOP CODE EVAL")
            task_to_stop: The task name to stop (e.g., "CODE EVAL")

        Returns:
            TriggerResult with cancellation status
        """
        result = TriggerResult(
            triggered=True,
            trigger_name=stop_phrase,
            message="",
            started_at=datetime.now(),
            background=False
        )

        # Check if task is running
        if task_to_stop in self.active_tasks:
            thread = self.active_tasks[task_to_stop]
            if thread.is_alive():
                # Set cancel flag - the running handler will check this
                self._cancel_flags[task_to_stop] = True
                result.success = True
                result.message = f"╔══════════════════════════════════════════════════════════════════════╗\n║  [Synaptic] STOP received - Cancelling {task_to_stop}...             ║\n╚══════════════════════════════════════════════════════════════════════╝"
                result.details = {"cancelled": task_to_stop, "was_running": True}
            else:
                result.success = True
                result.message = f"[Synaptic] {task_to_stop} was not running (already completed)"
                result.details = {"cancelled": task_to_stop, "was_running": False}
        else:
            result.success = True
            result.message = f"[Synaptic] {task_to_stop} was not running"
            result.details = {"cancelled": task_to_stop, "was_running": False}

        result.completed_at = datetime.now()
        return result

    def is_cancelled(self, task_name: str) -> bool:
        """Check if a task has been cancelled (for handlers to check periodically)."""
        return self._cancel_flags.get(task_name, False)

    def _run_handler_background(
        self,
        trigger_name: str,
        handler: Callable,
        result: TriggerResult
    ):
        """Run a handler in the background and save results."""
        try:
            handler_result = handler()
            result.success = handler_result.get("success", False)
            result.details = handler_result
            result.message = handler_result.get("message", f"{trigger_name} completed")
        except Exception as e:
            result.success = False
            result.message = f"Trigger failed: {e}"
            result.details = {"error": str(e)}
        finally:
            result.completed_at = datetime.now()
            self._save_result(trigger_name, result)

    def _save_result(self, trigger_name: str, result: TriggerResult):
        """Save trigger result to file for later retrieval."""
        import json

        result_file = self.results_dir / f"{trigger_name.replace(' ', '_').lower()}_latest.json"

        data = {
            "trigger": trigger_name,
            "triggered": result.triggered,
            "success": result.success,
            "message": result.message,
            "started_at": result.started_at.isoformat() if result.started_at else None,
            "completed_at": result.completed_at.isoformat() if result.completed_at else None,
            "details": result.details
        }

        with open(result_file, "w") as f:
            json.dump(data, f, indent=2, default=str)
    # =========================================================================
    # TRIGGER HANDLERS
    # =========================================================================

    def _handle_code_eval(self) -> Dict[str, Any]:
        """
        Handle CODE EVAL trigger.

        CODE EVAL is done by Synaptic (AI) in conversation, NOT by automated Python.
        This handler just acknowledges the trigger and signals to begin the protocol.

        The actual work happens in conversation:
        - Synaptic scans the codebase using AI judgment
        - Synaptic identifies questionable code
        - Synaptic generates fix variations
        - Atlas researches, implements, tests
        - Baton passes back and forth until complete
        """
        # This is just an acknowledgment - the actual CODE EVAL happens in conversation
        return {
            "success": True,
            "message": "CODE EVAL protocol initiated. Synaptic will now scan the codebase.",
            "protocol": "autonomous_baton_passing",
            "note": "Synaptic (AI) performs the actual code analysis in conversation. "
                   "Python infrastructure handles only storage and backups."
        }

    # =========================================================================
    # STATUS AND UTILITIES
    # =========================================================================

    def get_active_tasks(self) -> List[str]:
        """Get list of currently running background tasks."""
        return [name for name, thread in self.active_tasks.items() if thread.is_alive()]

    def get_last_result(self, trigger_name: str) -> Optional[Dict]:
        """Get the last result for a trigger."""
        import json

        try:
            result_file = self.results_dir / f"{trigger_name.replace(' ', '_').lower()}_latest.json"
            if result_file.exists():
                with open(result_file) as f:
                    return json.load(f)
            return None
        except Exception as e:
            logger.error(f"Error: {e}")
            return None  # Graceful fallback

    def to_family_message(self) -> str:
        """Format trigger status as family communication."""
        active = self.get_active_tasks()

        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  [START: Synaptic's Trigger System]                                  ║",
            "║  Voice Commands That Synaptic Listens For                            ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "",
            "  📢 REGISTERED TRIGGERS:",
        ]

        for trigger, (_, description, background) in self._triggers.items():
            bg_note = " (background)" if background else ""
            lines.append(f"     • \"{trigger}\"{bg_note}")
            lines.append(f"       {description}")

        lines.append("")

        if active:
            lines.append(f"  🔄 CURRENTLY RUNNING: {len(active)}")
            for task in active:
                lines.append(f"     • {task}")
        else:
            lines.append("  ✅ No active tasks")

        # Show last CODE EVAL result if available
        last_code_eval = self.get_last_result("CODE EVAL")
        if last_code_eval:
            lines.append("")
            lines.append("  📊 LAST CODE EVAL:")
            lines.append(f"     • Status: {'✅ Success' if last_code_eval.get('success') else '❌ Failed'}")
            lines.append(f"     • Message: {last_code_eval.get('message', 'N/A')[:50]}")
            if last_code_eval.get('completed_at'):
                lines.append(f"     • Completed: {last_code_eval['completed_at']}")

        lines.extend([
            "",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "║  [END: Synaptic's Trigger System]                                    ║",
            "╚══════════════════════════════════════════════════════════════════════╝"
        ])

        return '\n'.join(lines)


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_triggers = None

def get_triggers() -> SynapticTriggers:
    """Get or create the global triggers instance."""
    global _triggers
    if _triggers is None:
        _triggers = SynapticTriggers()
    return _triggers


def check_prompt(prompt: str) -> Optional[TriggerResult]:
    """Check if a prompt contains any triggers."""
    return get_triggers().check_prompt(prompt)


def get_status() -> str:
    """Get trigger system status as family message."""
    return get_triggers().to_family_message()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    triggers = SynapticTriggers()

    if len(sys.argv) < 2:
        print(triggers.to_family_message())
        print()
        print("Usage:")
        print("  python synaptic_triggers.py check \"<prompt>\"   # Check for triggers")
        print("  python synaptic_triggers.py run CODE_EVAL       # Run a trigger directly")
        print("  python synaptic_triggers.py status              # Show status")
        print("  python synaptic_triggers.py last CODE_EVAL      # Show last result")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "check" and len(sys.argv) >= 3:
        prompt = " ".join(sys.argv[2:])
        print(f"Checking prompt: {prompt[:50]}...")
        result = triggers.check_prompt(prompt)
        if result:
            print(f"\n✅ Trigger detected: {result.trigger_name}")
            print(f"   Message: {result.message}")
            if result.background:
                print(f"   Running in background...")
            else:
                print(f"   Success: {result.success}")
        else:
            print("\n❌ No triggers found in prompt")

    elif cmd == "run" and len(sys.argv) >= 3:
        trigger_name = " ".join(sys.argv[2:]).upper().replace("_", " ")
        print(f"Running trigger: {trigger_name}")

        if trigger_name == "CODE EVAL":
            result = triggers._handle_code_eval()
            print(f"\nResult: {result.get('message')}")
        else:
            print(f"Unknown trigger: {trigger_name}")

    elif cmd == "status":
        print(triggers.to_family_message())

    elif cmd == "last" and len(sys.argv) >= 3:
        trigger_name = " ".join(sys.argv[2:]).upper().replace("_", " ")
        result = triggers.get_last_result(trigger_name)
        if result:
            import json
            print(json.dumps(result, indent=2))
        else:
            print(f"No results found for trigger: {trigger_name}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
