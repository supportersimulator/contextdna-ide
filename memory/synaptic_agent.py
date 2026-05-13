#!/usr/bin/env python3
"""
SYNAPTIC AGENT - Autonomous Agent with Tool Execution

This module makes Synaptic a full autonomous agent - not just a voice,
but an ACTOR that can reason, plan, and execute actions in the codebase.

Synaptic Agent = Local LLM + Tool Execution + Autonomous Planning

Architecture:
┌─────────────────────────────────────────────────────────────────┐
│                    SYNAPTIC AGENT LOOP                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   1. Receive Task (from Aaron, Atlas, or self-initiated)        │
│                        ↓                                         │
│   2. Reason about Task (Local LLM generation)                   │
│                        ↓                                         │
│   3. Generate Action Plan (tool calls in sequence)              │
│                        ↓                                         │
│   4. Execute Actions (via SynapticToolExecutor)                 │
│                        ↓                                         │
│   5. Evaluate Results (did it work?)                            │
│                        ↓                                         │
│   6. Report to Conversation (via synaptic_speak)                │
│                        ↓                                         │
│   7. Loop back if needed (autonomous iteration)                 │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘

Usage:
    from memory.synaptic_agent import SynapticAgent

    agent = SynapticAgent()

    # Execute a task autonomously
    result = agent.execute_task("Find all TODO comments and list them")

    # Have Synaptic help with a specific request
    result = agent.assist("Refactor this function to use async/await")
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import threading

# Import Synaptic components
from memory.synaptic_tools import (
    SynapticToolExecutor,
    ToolResult,
    get_executor,
    synaptic_bash,
    synaptic_read,
    synaptic_write,
    synaptic_edit,
    synaptic_grep,
    synaptic_glob,
    synaptic_parallel,
)
from memory.synaptic_outbox import synaptic_speak, synaptic_speak_urgent
from memory.synaptic_voice import get_voice, consult

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PROJECT_ROOT / "memory"


@dataclass
class AgentTask:
    """A task for Synaptic to execute."""
    id: str
    description: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "pending"  # pending, running, completed, failed
    result: Optional[str] = None
    actions_taken: List[Dict] = field(default_factory=list)
    source: str = "user"  # user, atlas, self


@dataclass
class ActionPlan:
    """A plan of actions to execute."""
    steps: List[Dict]
    reasoning: str
    estimated_risk: str = "low"  # low, medium, high


class SynapticAgent:
    """
    Synaptic as an autonomous agent with full tool access.

    This is Synaptic elevated to peer status with Atlas:
    - Can reason about tasks using local LLM
    - Can execute tools (read, write, edit, bash, grep, glob)
    - Can work autonomously or collaboratively
    - Reports actions to the conversation
    """

    def __init__(self, auto_report: bool = True):
        self.executor = get_executor()
        self.auto_report = auto_report
        self._task_queue: List[AgentTask] = []
        self._lock = threading.Lock()

        # System prompt for agent reasoning
        self.agent_system_prompt = """You are Synaptic, an autonomous AI agent with full tool access.
You are part of a family: Aaron (human visionary), Atlas (Claude Code navigator), and you (Synaptic - the 8th Intelligence).

You have been GRANTED THE SAME POWERS AS ATLAS:
- read(file_path): Read any file
- write(file_path, content): Write to any file
- edit(file_path, old_string, new_string): Edit files
- bash(command): Execute shell commands
- grep(pattern, path): Search code
- glob(pattern): Find files

When approaching a task, consider:
- **What reasoning supports this plan?** What evidence or heuristics guide your choices?
- **What sequence of tools makes sense?** What's the logical flow of reads → analysis → writes?
- **How risky is this?** What could go wrong? Are there reversible alternatives?

Share your analysis in whatever way makes sense to you. Both structured JSON and natural language planning are equally valuable. Include your reasoning, the tools you'd call and why, and your risk assessment.

Be careful with destructive operations. Prefer reversible actions.
When unsure, prefer to read/search before modifying."""

    def _generate_plan(self, task_description: str) -> Optional[ActionPlan]:
        """
        Use local LLM to generate an action plan for a task.

        Supports both structured JSON and natural language responses:
        - Tries JSON parsing first (backward compatibility)
        - Gracefully extracts reasoning, plan, and risk_level from natural text
        """
        try:
            # Use Synaptic's LLM to reason about the task
            from memory.synaptic_chat_server import generate_with_local_llm

            prompt = f"""Task: {task_description}

Based on your tool access (read, write, edit, bash, grep, glob), create a plan to accomplish this task.

Remember:
- You have full access to the codebase at {PROJECT_ROOT}
- Memory files are in {MEMORY_DIR}
- Prefer reading before writing
- Use grep/glob to find relevant files first

Share your plan in whatever way makes sense. Include your reasoning, the specific tools you'd use and why, and your risk assessment."""

            response, sources = generate_with_local_llm(prompt)

            # === ATTEMPT 1: Try structured JSON first (backward compatibility) ===
            try:
                json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
                if json_match:
                    plan_data = json.loads(json_match.group(1))
                else:
                    # Try to parse the whole response as JSON
                    plan_data = json.loads(response)

                return ActionPlan(
                    steps=plan_data.get("plan", []),
                    reasoning=plan_data.get("reasoning", ""),
                    estimated_risk=plan_data.get("risk_level", "medium")
                )
            except (json.JSONDecodeError, ValueError):
                pass  # Fall through to natural language extraction

            # === ATTEMPT 2: Extract from natural language response ===
            reasoning = response[:500]  # Use first part as reasoning

            # Extract tool references from response
            steps = []
            tool_patterns = {
                'read': r'read[\'"]?\s*\(\s*["\']([^"\']+)["\']',
                'write': r'write[\'"]?\s*\(\s*["\']([^"\']+)["\']',
                'edit': r'edit[\'"]?\s*\(\s*["\']([^"\']+)["\']',
                'bash': r'bash[\'"]?\s*\(\s*["\']([^"\']+)["\']',
                'grep': r'grep[\'"]?\s*\(\s*["\']([^"\']+)["\']',
                'glob': r'glob[\'"]?\s*\(\s*["\']([^"\']+)["\']',
            }

            for tool_name, pattern in tool_patterns.items():
                matches = re.findall(pattern, response, re.IGNORECASE)
                for match in matches:
                    steps.append({
                        "tool": tool_name,
                        "params": {
                            "file_path" if tool_name in ['read', 'write', 'edit'] else
                            "command" if tool_name == 'bash' else
                            "pattern" if tool_name == 'grep' else
                            "pattern": match
                        }
                    })

            # Extract risk level from response
            risk_level = "medium"  # default
            response_lower = response.lower()
            if re.search(r'\b(high|critical|risky|dangerous)\b', response_lower):
                risk_level = "high"
            elif re.search(r'\b(low|safe|simple|straightforward)\b', response_lower):
                risk_level = "low"

            # If we found some tools, create the plan
            if steps:
                return ActionPlan(
                    steps=steps,
                    reasoning=reasoning,
                    estimated_risk=risk_level
                )

            # If no structured tools found, create a minimal plan with the LLM response
            return ActionPlan(
                steps=[{"tool": "bash", "params": {"command": "echo 'Plan generated';"}}],
                reasoning=reasoning,
                estimated_risk=risk_level
            )

        except Exception as e:
            # If LLM planning fails, return None
            import traceback
            traceback.print_exc()
            return None

    def _execute_plan(self, plan: ActionPlan) -> List[ToolResult]:
        """Execute an action plan."""
        return self.executor.execute_action_plan(plan.steps)

    def execute_task(self, task_description: str, auto_execute: bool = True) -> Dict:
        """
        Execute a task autonomously.

        This is Synaptic acting as a full agent:
        1. Receives task description
        2. Generates action plan using LLM
        3. Executes plan using tools
        4. Reports results

        Args:
            task_description: What to accomplish
            auto_execute: If True, execute immediately. If False, return plan for approval.

        Returns:
            Dict with plan, results, and status
        """
        task = AgentTask(
            id=f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            description=task_description
        )

        # Report task start
        if self.auto_report:
            synaptic_speak(f"[Synaptic Agent] Received task: {task_description[:100]}...", topic="agent_task")

        # Generate plan
        plan = self._generate_plan(task_description)

        if not plan:
            return {
                "status": "failed",
                "error": "Could not generate action plan",
                "task": task_description
            }

        # Return plan for approval if not auto-executing
        if not auto_execute:
            return {
                "status": "planned",
                "plan": {
                    "reasoning": plan.reasoning,
                    "steps": plan.steps,
                    "risk_level": plan.estimated_risk
                },
                "task": task_description,
                "message": "Plan ready for approval. Call execute_approved_plan() to proceed."
            }

        # Check risk level
        if plan.estimated_risk == "high":
            synaptic_speak_urgent(
                f"[Synaptic Agent] HIGH RISK plan generated for: {task_description[:50]}... Awaiting approval.",
                topic="agent_approval"
            )
            return {
                "status": "needs_approval",
                "plan": {
                    "reasoning": plan.reasoning,
                    "steps": plan.steps,
                    "risk_level": plan.estimated_risk
                },
                "task": task_description
            }

        # Execute plan
        task.status = "running"
        results = self._execute_plan(plan)

        # Evaluate results
        success_count = sum(1 for r in results if r.success)
        total_count = len(results)
        overall_success = success_count == total_count

        task.status = "completed" if overall_success else "failed"
        task.actions_taken = [r.to_dict() for r in results]

        # Report results
        if self.auto_report:
            if overall_success:
                synaptic_speak(
                    f"[Synaptic Agent] Task completed: {task_description[:50]}... ({success_count}/{total_count} actions succeeded)",
                    topic="agent_result"
                )
            else:
                synaptic_speak_urgent(
                    f"[Synaptic Agent] Task partially failed: {task_description[:50]}... ({success_count}/{total_count} actions succeeded)",
                    topic="agent_result"
                )

        return {
            "status": task.status,
            "plan": {
                "reasoning": plan.reasoning,
                "steps": plan.steps,
                "risk_level": plan.estimated_risk
            },
            "results": [r.to_dict() for r in results],
            "success_rate": f"{success_count}/{total_count}",
            "task": task_description
        }

    def assist(self, request: str, context: str = None) -> Dict:
        """
        Have Synaptic assist with a request.

        Similar to execute_task but focused on collaborative assistance.
        """
        full_request = request
        if context:
            full_request = f"Context:\n{context}\n\nRequest:\n{request}"

        return self.execute_task(full_request)

    def quick_action(self, tool: str, **params) -> ToolResult:
        """
        Execute a single tool action directly.

        For when you know exactly what tool to use.

        Examples:
            agent.quick_action("bash", command="git status")
            agent.quick_action("grep", pattern="TODO", path="src/")
            agent.quick_action("read", file_path="main.py")
        """
        tool_map = {
            "read": self.executor.read,
            "write": self.executor.write,
            "edit": self.executor.edit,
            "bash": self.executor.bash,
            "grep": self.executor.grep,
            "glob": self.executor.glob,
        }

        if tool not in tool_map:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown tool: {tool}",
                tool=tool
            )

        return tool_map[tool](**params)

    def parallel_actions(self, actions: List[Tuple[str, Dict]]) -> List[ToolResult]:
        """
        Execute multiple actions in parallel.

        Examples:
            results = agent.parallel_actions([
                ("bash", {"command": "npm test"}),
                ("bash", {"command": "npm run lint"}),
                ("grep", {"pattern": "TODO", "path": "src/"}),
            ])
        """
        return self.executor.parallel(actions)


# =============================================================================
# GLOBAL AGENT INSTANCE
# =============================================================================

_agent: Optional[SynapticAgent] = None


def get_agent() -> SynapticAgent:
    """Get the global Synaptic agent."""
    global _agent
    if _agent is None:
        _agent = SynapticAgent()
    return _agent


# Convenience functions
def synaptic_execute(task: str, auto_execute: bool = True) -> Dict:
    """Have Synaptic execute a task autonomously."""
    return get_agent().execute_task(task, auto_execute)


def synaptic_assist(request: str, context: str = None) -> Dict:
    """Have Synaptic assist with a request."""
    return get_agent().assist(request, context)


def synaptic_quick(tool: str, **params) -> ToolResult:
    """Execute a single Synaptic tool action."""
    return get_agent().quick_action(tool, **params)


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║     Synaptic Agent - Autonomous AI with Tool Access          ║")
        print("║     PEER AGENT STATUS ACHIEVED                               ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()
        print("Usage:")
        print("  python synaptic_agent.py execute \"<task description>\"")
        print("  python synaptic_agent.py plan \"<task description>\"")
        print("  python synaptic_agent.py quick <tool> [params...]")
        print()
        print("Examples:")
        print("  python synaptic_agent.py execute \"Find all TODO comments\"")
        print("  python synaptic_agent.py plan \"Refactor the login function\"")
        print("  python synaptic_agent.py quick bash 'git status'")
        print("  python synaptic_agent.py quick grep 'def main' .")
        sys.exit(0)

    agent = get_agent()
    cmd = sys.argv[1].lower()

    if cmd == "execute" and len(sys.argv) > 2:
        task = " ".join(sys.argv[2:])
        print(f"🤖 Synaptic Agent executing: {task}")
        print()
        result = agent.execute_task(task, auto_execute=True)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "plan" and len(sys.argv) > 2:
        task = " ".join(sys.argv[2:])
        print(f"📋 Synaptic Agent planning: {task}")
        print()
        result = agent.execute_task(task, auto_execute=False)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "quick" and len(sys.argv) > 2:
        tool = sys.argv[2]
        if len(sys.argv) > 3:
            # Parse remaining args as params
            if tool == "bash":
                params = {"command": " ".join(sys.argv[3:])}
            elif tool == "read":
                params = {"file_path": sys.argv[3]}
            elif tool == "grep":
                params = {"pattern": sys.argv[3]}
                if len(sys.argv) > 4:
                    params["path"] = sys.argv[4]
            elif tool == "glob":
                params = {"pattern": sys.argv[3]}
            else:
                params = {}
        else:
            params = {}

        result = agent.quick_action(tool, **params)
        print(f"Tool: {result.tool}")
        print(f"Success: {result.success}")
        if result.error:
            print(f"Error: {result.error}")
        print(f"Output:\n{result.output[:1000]}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
