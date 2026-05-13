#!/usr/bin/env python3
"""
Automatic Context Injection for AI Agents

This module provides automatic pre-execution context injection from Context DNA.
Instead of manually querying, agents import this and get relevant learnings
automatically injected based on the work area they're about to touch.

Usage in agent prompts or scripts:
    from memory.auto_context import get_context_for_task

    # Get relevant learnings automatically
    context = get_context_for_task("modifying async boto3 code in LLM service")
    print(context)  # Inject this into the agent's context

For Claude Code / Cursor integration:
    # Add to system prompt or run before each task
    python -c "from memory.auto_context import inject_context; inject_context('async boto3')"
"""

import sys
import os
import re
from pathlib import Path
from typing import Optional

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False


# Keyword patterns that trigger specific queries
AREA_PATTERNS = {
    "async": ["async", "asyncio", "await", "to_thread", "event loop", "concurrent", "parallel"],
    "boto3": ["boto3", "bedrock", "aws sdk", "converse", "invoke_model"],
    "docker": ["docker", "container", "ecs", "fargate", "task definition"],
    "gpu": ["gpu", "g5", "cuda", "nvidia", "asg", "auto scaling"],
    "livekit": ["livekit", "webrtc", "room", "participant", "track"],
    "tts": ["tts", "text to speech", "kyutai", "audio output", "speech synthesis"],
    "stt": ["stt", "speech to text", "whisper", "transcription", "audio input"],
    "lambda": ["lambda", "api gateway", "serverless", "function url"],
    "cloudflare": ["cloudflare", "dns", "proxy", "cdn", "waf"],
    "networking": ["ip", "vpc", "subnet", "security group", "nlb", "alb"],
    "websocket": ["websocket", "ws://", "wss://", "realtime", "connection"],
}

# Query templates for each area
AREA_QUERIES = {
    "async": "async asyncio boto3 event loop blocking",
    "boto3": "boto3 bedrock aws sdk synchronous",
    "docker": "docker container ecs restart env reload",
    "gpu": "gpu asg ip networking private address",
    "livekit": "livekit webrtc websocket connection",
    "tts": "tts kyutai audio sample rate streaming",
    "stt": "stt whisper transcription async",
    "lambda": "lambda api gateway timeout cors",
    "cloudflare": "cloudflare dns proxy webrtc udp",
    "networking": "vpc subnet security group nlb",
    "websocket": "websocket connection close timeout",
}


def detect_areas(task_description: str) -> list[str]:
    """Detect which technical areas a task touches based on keywords."""
    task_lower = task_description.lower()
    detected = []

    for area, patterns in AREA_PATTERNS.items():
        for pattern in patterns:
            if pattern in task_lower:
                if area not in detected:
                    detected.append(area)
                break

    return detected


def get_context_for_task(task_description: str, max_learnings: int = 5) -> str:
    """
    Automatically get relevant context for a task description.

    This analyzes the task description, detects relevant areas,
    queries Context DNA for each area, and returns formatted context.

    Args:
        task_description: Natural language description of what the agent is about to do
        max_learnings: Maximum learnings to include per area

    Returns:
        Formatted context string ready for prompt injection
    """
    if not CONTEXT_DNA_AVAILABLE:
        return "# Context DNA not available - install with: pip install acontext\n"

    try:
        memory = ContextDNAClient()
        if not memory.ping():
            return "# Context DNA server not running - start with: ~/.acontext/bin/acontext docker up -d\n"
    except Exception as e:
        return f"# Context DNA connection failed: {e}\n"

    # Detect which areas this task touches
    areas = detect_areas(task_description)

    if not areas:
        # Default to a general query based on the task description
        areas = ["general"]
        AREA_QUERIES["general"] = task_description

    # Collect learnings from each area
    all_learnings = []

    for area in areas:
        query = AREA_QUERIES.get(area, area)
        learnings = memory.get_relevant_learnings(query, limit=max_learnings)

        for learning in learnings:
            if learning.get("distance", 1.0) < 0.5:  # Only include relevant results
                all_learnings.append({
                    "area": area,
                    "title": learning.get("title", ""),
                    "use_when": learning.get("use_when", ""),
                    "preferences": learning.get("preferences", ""),
                    "relevance": 1 - learning.get("distance", 0)
                })

    # Sort by relevance
    all_learnings.sort(key=lambda x: x["relevance"], reverse=True)

    # Format output
    if not all_learnings:
        return f"# No relevant learnings found for: {task_description}\n"

    output = [
        "# ACONTEXT: Relevant Project Learnings",
        f"# Task: {task_description}",
        f"# Areas detected: {', '.join(areas)}",
        "",
        "## APPLY THESE LEARNINGS BEFORE PROCEEDING:",
        ""
    ]

    for i, learning in enumerate(all_learnings[:10], 1):
        output.append(f"### {i}. {learning['title']}")
        if learning['use_when']:
            output.append(f"**When:** {learning['use_when']}")
        if learning['preferences']:
            pref = learning['preferences']
            output.append(f"**Do:** {pref[:300]}..." if len(pref) > 300 else f"**Do:** {pref}")
        output.append(f"_Relevance: {learning['relevance']:.0%}_")
        output.append("")

    output.append("---")
    output.append("# END ACONTEXT CONTEXT - Proceed with task")

    return "\n".join(output)


def inject_context(keywords: str = None, task: str = None):
    """
    Print context to stdout for shell pipeline injection.

    Usage:
        python -c "from memory.auto_context import inject_context; inject_context('async boto3')"

        # Or with a task description
        python -c "from memory.auto_context import inject_context; inject_context(task='fixing LLM latency')"
    """
    if task:
        context = get_context_for_task(task)
    elif keywords:
        context = get_context_for_task(keywords)
    else:
        context = "# No keywords or task provided\n"

    print(context)


def should_query_for_file(file_path: str) -> tuple[bool, str]:
    """
    Determine if a file modification should trigger a Context DNA query.

    Returns (should_query, suggested_query)
    """
    path_lower = file_path.lower()

    # Map file paths to queries
    if "async" in path_lower or "main.py" in path_lower:
        return True, "async asyncio event loop"
    if "docker" in path_lower or "ecs" in path_lower:
        return True, "docker ecs container"
    if "lambda" in path_lower:
        return True, "lambda api gateway"
    if "livekit" in path_lower or "agent" in path_lower:
        return True, "livekit webrtc agent"
    if "tts" in path_lower or "kyutai" in path_lower:
        return True, "tts kyutai streaming"
    if "stt" in path_lower or "whisper" in path_lower:
        return True, "stt whisper transcription"
    if "llm" in path_lower or "bedrock" in path_lower:
        return True, "llm boto3 bedrock"
    if "terraform" in path_lower or "infra" in path_lower:
        return True, "infrastructure gpu asg networking"
    if "cloudflare" in path_lower:
        return True, "cloudflare dns proxy"

    return False, ""


def get_file_context(file_path: str) -> str:
    """
    Get context relevant to a specific file before modifying it.

    Usage:
        context = get_file_context("ersim-voice-stack/services/llm/app/main.py")
    """
    should_query, query = should_query_for_file(file_path)

    if not should_query:
        return f"# No specific learnings for {file_path}\n"

    return get_context_for_task(f"modifying {file_path}: {query}")


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python auto_context.py 'task description'")
        print("  python auto_context.py --file path/to/file.py")
        print("")
        print("Examples:")
        print("  python auto_context.py 'fixing async boto3 performance'")
        print("  python auto_context.py --file ersim-voice-stack/services/llm/app/main.py")
        sys.exit(0)

    if sys.argv[1] == "--file":
        if len(sys.argv) < 3:
            print("Error: --file requires a path argument")
            sys.exit(1)
        print(get_file_context(sys.argv[2]))
    else:
        task = " ".join(sys.argv[1:])
        print(get_context_for_task(task))
