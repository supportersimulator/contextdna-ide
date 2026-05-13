#!/usr/bin/env python3
"""
MCP Server for Synaptic - The 8th Intelligence.

This server provides a tool for Claude Code agents to query Synaptic's
context/intelligence mid-task for patterns, intuitions, and memory context.

Uses DIRECT PYTHON IMPORT of SynapticVoice for fastest response (no HTTP overhead).
Synaptic queries: learnings, patterns, brain state, major skills, family journal.
"""

import json
import sys
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

# Add memory module to path for direct import
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Initialize the MCP server
mcp = FastMCP("synaptic_mcp")


class ContextQueryInput(BaseModel):
    """Input model for querying Synaptic context intelligence."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra='forbid'
    )

    task_description: str = Field(
        ...,
        description="Description of the current sub-task or search context the agent is working on",
        min_length=1,
        max_length=2000
    )
    query: Optional[str] = Field(
        default=None,
        description="Optional specific query to send to the intelligence endpoint",
        max_length=1000
    )


def _get_synaptic_voice():
    """Lazily import and get SynapticVoice instance."""
    try:
        from memory.synaptic_voice import SynapticVoice
        return SynapticVoice()
    except ImportError as e:
        return None, f"Cannot import SynapticVoice: {e}"


@mcp.tool(
    name="synaptic_get_context",
    annotations={
        "title": "Get Synaptic Context Intelligence",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def synaptic_get_context(params: ContextQueryInput) -> str:
    """Query Synaptic's 8th Intelligence for patterns, intuitions, and memory context.

    DIRECT PYTHON integration with SynapticVoice (no HTTP overhead).
    Queries: learnings, patterns, brain state, major skills, family journal.

    Use this tool mid-search or mid-task to get additional context, patterns,
    or intuitions from Synaptic that may be relevant to the current sub-task.

    Args:
        params (ContextQueryInput): Input parameters containing:
            - task_description (str): What the agent is currently working on
            - query (Optional[str]): Specific query to send

    Returns:
        str: Synaptic's response with context, patterns, and intuitions

    Examples:
        - Use when: Agent is searching for code and needs additional context
        - Use when: Agent needs pattern suggestions for the current task
        - Use when: Mid-task intelligence augmentation is helpful
    """
    try:
        # Direct Python import - fastest possible response
        from memory.synaptic_voice import SynapticVoice, SynapticResponse

        voice = SynapticVoice()

        # Build context dict
        context = {}
        if params.query:
            context["specific_query"] = params.query

        # Consult Synaptic directly
        response: SynapticResponse = voice.consult(params.task_description, context)

        # Format response as structured JSON
        result = {
            "has_context": response.has_context,
            "confidence": response.confidence,
            "context_sources": response.context_sources,
            "relevant_learnings": response.relevant_learnings[:5] if response.relevant_learnings else [],
            "relevant_patterns": response.relevant_patterns[:5] if response.relevant_patterns else [],
            "synaptic_perspective": response.synaptic_perspective,
            "improvement_proposals": response.improvement_proposals[:3] if response.improvement_proposals else []
        }

        return json.dumps(result, indent=2, default=str)

    except ImportError as e:
        return json.dumps({
            "error": "SynapticVoice import failed",
            "detail": str(e),
            "hint": "Ensure memory/synaptic_voice.py exists and dependencies are installed"
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "error": f"Synaptic query failed: {type(e).__name__}",
            "detail": str(e)
        }, indent=2)


@mcp.tool(
    name="synaptic_health_check",
    annotations={
        "title": "Check Synaptic Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def synaptic_health_check() -> str:
    """Check if Synaptic (8th Intelligence) is available.

    Verifies that SynapticVoice can be imported and memory systems are accessible.
    Uses direct Python import (no server required).

    Returns:
        str: Status message indicating Synaptic availability
    """
    try:
        from memory.synaptic_voice import SynapticVoice

        voice = SynapticVoice()

        # Check memory paths
        memory_dir = voice.memory_dir
        config_dir = voice.config_dir

        status = {
            "status": "healthy",
            "mode": "direct_python_import",
            "memory_dir_exists": memory_dir.exists(),
            "config_dir_exists": config_dir.exists(),
            "brain_state_exists": (memory_dir / "brain_state.md").exists(),
        }

        # Quick test query
        test_response = voice.consult("health check")
        status["sources_available"] = test_response.context_sources
        status["confidence"] = test_response.confidence

        return json.dumps(status, indent=2)

    except ImportError as e:
        return json.dumps({
            "status": "error",
            "error": "Cannot import SynapticVoice",
            "detail": str(e),
            "hint": "Check that memory/synaptic_voice.py exists"
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": f"{type(e).__name__}: {str(e)}"
        }, indent=2)


if __name__ == "__main__":
    mcp.run()
