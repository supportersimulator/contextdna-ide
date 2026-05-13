#!/usr/bin/env python3
"""
AGENT QUERY - Simple Synaptic Query Interface for Any Agent

This module provides a dead-simple interface for any agent (Atlas sub-agents,
research agents, etc.) to query Synaptic for contextual understanding.

Usage:
    # In any Python code
    from memory.agent_query import ask_synaptic, get_context_for

    # Quick question
    response = ask_synaptic("How does the webhook injection work?")
    print(response.perspective)

    # Context for a specific task
    context = get_context_for("implementing Docker health checks")
    print(context.learnings)
    print(context.patterns)

CLI Usage:
    python memory/agent_query.py "your question here"
    python memory/agent_query.py --task "what you're working on"
"""

import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

# Ensure imports work
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class AgentContext:
    """Context response for an agent."""
    question: str
    confidence: float
    perspective: str
    learnings: List[str] = field(default_factory=list)
    patterns: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)


def ask_synaptic(question: str, context: Dict[str, Any] = None) -> AgentContext:
    """
    Ask Synaptic a question and get contextual understanding.

    This is the simplest way for any agent to query Synaptic's memory.

    Args:
        question: What you want to know
        context: Optional additional context (e.g., {"file": "path/to/file.py"})

    Returns:
        AgentContext with perspective, learnings, patterns, warnings

    Example:
        response = ask_synaptic("How does the webhook injection work?")
        print(f"Confidence: {response.confidence}")
        print(f"Perspective: {response.perspective}")
        for learning in response.learnings:
            print(f"  - {learning}")
    """
    try:
        from memory.synaptic_voice import get_voice

        voice = get_voice()
        response = voice.consult(question, context)

        # Extract learnings as strings
        learnings = []
        for learning in (response.relevant_learnings or []):
            if isinstance(learning, dict):
                title = learning.get('title', learning.get('symptom', ''))
                if title:
                    learnings.append(title)
            elif isinstance(learning, str):
                learnings.append(learning)

        # Extract patterns as strings
        patterns = []
        for pattern in (response.relevant_patterns or []):
            if isinstance(pattern, str):
                patterns.append(pattern)
            elif isinstance(pattern, dict):
                patterns.append(str(pattern))

        return AgentContext(
            question=question,
            confidence=response.confidence,
            perspective=response.synaptic_perspective or "",
            learnings=learnings[:5],  # Top 5
            patterns=patterns[:3],  # Top 3
            warnings=[],  # Future: extract warnings from patterns
            suggestions=response.improvement_proposals or [],
            sources=response.context_sources or []
        )

    except Exception as e:
        return AgentContext(
            question=question,
            confidence=0.0,
            perspective=f"Error querying Synaptic: {e}",
            warnings=[str(e)]
        )


def get_context_for(task: str) -> AgentContext:
    """
    Get context relevant to a specific task.

    Similar to ask_synaptic but framed as "I'm working on X, what should I know?"

    Args:
        task: Description of what you're working on

    Returns:
        AgentContext with task-relevant information

    Example:
        context = get_context_for("implementing Docker health checks")
        for warning in context.warnings:
            print(f"⚠️ {warning}")
    """
    framed_question = f"""
    I'm about to work on: {task}

    What should I know? Include:
    - Relevant past learnings
    - Active patterns
    - Potential gotchas or warnings
    - Related work that's been done before
    """

    return ask_synaptic(framed_question, context={"task": task})


def quick_context(keywords: str) -> str:
    """
    Get a quick one-liner context for keywords.

    Fastest way to check if Synaptic has relevant info.

    Args:
        keywords: Space-separated keywords

    Returns:
        Single string summary or empty if no context

    Example:
        print(quick_context("docker health retry"))
        # "Docker health checks need retry logic with exponential backoff..."
    """
    response = ask_synaptic(keywords)

    if response.confidence < 0.2:
        return ""

    # Return first learning or perspective excerpt
    if response.learnings:
        return response.learnings[0]

    if response.perspective:
        return response.perspective[:200]

    return ""


def get_active_task() -> Optional[Dict[str, Any]]:
    """
    Get the currently active task from cognitive control.

    Returns the active directive if one exists.

    Returns:
        Task dict or None

    Example:
        task = get_active_task()
        if task:
            print(f"Working on: {task['objective']}")
    """
    try:
        from memory.task_directives import get_active_directive

        directive = get_active_directive()
        if directive:
            return {
                "task_id": directive.task_id,
                "objective": directive.objective,
                "constraints": directive.constraints,
                "priority": directive.priority,
                "acknowledged": directive.acknowledged
            }
        return None

    except Exception:
        return None


def acknowledge_task(task_id: str) -> bool:
    """
    Acknowledge that you're working on a task.

    Should be called when an agent starts work on an assigned task.

    Args:
        task_id: The task ID to acknowledge

    Returns:
        True if acknowledged successfully
    """
    try:
        from memory.task_directives import acknowledge_directive
        return acknowledge_directive(task_id)
    except Exception:
        return False


def report_output(task_id: str, output: str) -> bool:
    """
    Report task output back to Synaptic.

    Should be called when an agent completes work on a task.

    Args:
        task_id: The task ID
        output: The work output

    Returns:
        True if recorded successfully
    """
    try:
        from memory.task_persistence import get_task_store
        store = get_task_store()
        store.add_atlas_output(task_id, output)
        return True
    except Exception:
        return False


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Query Synaptic for contextual understanding",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python agent_query.py "How does webhook injection work?"
    python agent_query.py --task "implementing Docker health checks"
    python agent_query.py --quick "docker health retry"
    python agent_query.py --active  # Show active task
        """
    )
    parser.add_argument("question", nargs="?", help="Question to ask Synaptic")
    parser.add_argument("--task", "-t", help="Get context for a specific task")
    parser.add_argument("--quick", "-q", help="Quick one-liner context for keywords")
    parser.add_argument("--active", "-a", action="store_true", help="Show active task")

    args = parser.parse_args()

    if args.active:
        task = get_active_task()
        if task:
            print("╔══════════════════════════════════════════════════════════════════╗")
            print("║  📋 ACTIVE TASK                                                   ║")
            print("╚══════════════════════════════════════════════════════════════════╝")
            print(f"  task_id: {task['task_id']}")
            print(f"  objective: {task['objective']}")
            print(f"  priority: {task['priority']}")
            print(f"  constraints: {task['constraints']}")
            print(f"  acknowledged: {task['acknowledged']}")
        else:
            print("No active task")

    elif args.quick:
        result = quick_context(args.quick)
        if result:
            print(result)
        else:
            print("(no relevant context)")

    elif args.task:
        ctx = get_context_for(args.task)
        print("╔══════════════════════════════════════════════════════════════════╗")
        print(f"║  🎯 CONTEXT FOR: {args.task[:45]:45s} ║")
        print("╚══════════════════════════════════════════════════════════════════╝")
        print()
        print(f"Confidence: {ctx.confidence:.0%}")
        print(f"Sources: {', '.join(ctx.sources) if ctx.sources else 'none'}")
        print()

        if ctx.learnings:
            print("📚 Relevant Learnings:")
            for learning in ctx.learnings:
                print(f"   • {learning}")
            print()

        if ctx.patterns:
            print("🔄 Active Patterns:")
            for pattern in ctx.patterns:
                print(f"   • {pattern}")
            print()

        if ctx.perspective:
            print("💭 Synaptic's Perspective:")
            for line in ctx.perspective.split('\n')[:10]:
                print(f"   {line}")

    elif args.question:
        ctx = ask_synaptic(args.question)
        print("╔══════════════════════════════════════════════════════════════════╗")
        print("║  🧠 SYNAPTIC RESPONSE                                             ║")
        print("╚══════════════════════════════════════════════════════════════════╝")
        print()
        print(f"Confidence: {ctx.confidence:.0%}")
        print()

        if ctx.perspective:
            print("💭 Perspective:")
            for line in ctx.perspective.split('\n')[:15]:
                print(f"   {line}")
            print()

        if ctx.learnings:
            print("📚 Relevant Learnings:")
            for learning in ctx.learnings:
                print(f"   • {learning}")

    else:
        parser.print_help()
