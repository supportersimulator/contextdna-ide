#!/usr/bin/env python3
"""
Post-Work Analysis - Comprehensive Reflection & Forward Planning

Runs AFTER agent completes work (PostToolUse / afterFileEdit hook).
Uses local LLM thinking mode to provide:

1. Anticipated Next Steps (from Butler)
2. Agent Reflection (what was accomplished)
3. Agent Reasoning (what should be next)
4. Dependency Analysis (what needs consideration)
5. Failure Prediction (what fails first with 100 customers in 30 days)
6. Hardening Recommendations (additional redundancies)
7. Ecosystem Harmonization (fits with Context DNA systems)
8. Performance Considerations (RAM, integrations, bottlenecks)

NOT programmatic - LLM THINKS in each category using thinking mode.

Usage:
    from memory.post_work_analysis import generate_post_work_analysis
    
    analysis = generate_post_work_analysis(
        work_summary="Fixed logging bug in webhook generation",
        files_modified=["memory/persistent_hook_structure.py"],
        session_context="Working on webhook quality and LLM integration"
    )
    
    # Returns comprehensive analysis for agent to see
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).parent


def generate_post_work_analysis(
    work_summary: str,
    files_modified: List[str] = None,
    session_context: str = ""
) -> str:
    """
    Generate comprehensive post-work analysis using LLM thinking mode.
    
    Args:
        work_summary: What was just accomplished
        files_modified: List of files that were changed
        session_context: Broader context of current session
    
    Returns:
        Formatted analysis for agent to see and reflect on
    """
    files_modified = files_modified or []
    
    # Build comprehensive prompt for LLM
    prompt = _build_post_work_prompt(work_summary, files_modified, session_context)
    
    # Call LLM with thinking mode
    analysis = _llm_analyze_post_work(prompt)
    
    if not analysis:
        return ""
    
    # Format for injection
    return _format_post_work_analysis(analysis)


def _build_post_work_prompt(
    work_summary: str,
    files_modified: List[str],
    session_context: str
) -> str:
    """Build comprehensive prompt for post-work analysis."""
    
    # Get anticipated next steps from butler
    anticipated = ""
    try:
        from memory.anticipatory_butler import AnticipatorButler
        butler = AnticipatorButler()
        context = butler.get_ready_context(session_context)
        
        if context.get('anticipated'):
            anticipated = f"Butler anticipated: {list(context['anticipated'].keys())}"
        
        if context.get('big_picture'):
            anticipated += f"\nBig picture: {context['big_picture']}"
    except Exception as e:
        logger.debug(f"Butler anticipation unavailable: {e}")
    
    # Get strategic context
    strategic = ""
    try:
        from memory.strategic_planner import StrategicPlanner
        planner = StrategicPlanner()
        plans = planner._get_all_plans()
        
        if plans:
            strategic = "Major plans:\n" + "\n".join([
                f"- {p.title} ({p.status}, {p.priority})"
                for p in plans[:5]
            ])
    except Exception as e:
        logger.debug(f"Strategic planner unavailable: {e}")
    
    # Get recent work context
    recent_work = _get_recent_work_context()
    
    prompt = f"""POST-WORK ANALYSIS (use /think for deep reasoning)

WHAT WAS JUST DONE:
{work_summary}

Files modified:
{chr(10).join(files_modified) if files_modified else 'Unknown'}

SESSION CONTEXT:
{session_context}

BUTLER'S ANTICIPATION:
{anticipated or 'None yet'}

STRATEGIC ROADMAP:
{strategic or 'Unknown'}

RECENT WORK (last hour):
{recent_work}

YOUR TASK (use /think to reason deeply, then share insights):

You're a senior architect doing post-work reflection. Aaron values:
- Quick wins that impact entire ecosystem
- Testing before claiming complete
- LLM thinking freely (not programmatic responses)

Use /think to analyze what was just done and consider:
- What's most useful to mention right now?
- What would be most helpful for next steps?
- What risks should Aaron be aware of?
- How does this harmonize with the bigger system?
- What would make the biggest difference if done next?

Share whatever insights YOU think are most relevant and helpful.
Be concise but thoughtful. Prioritize quick wins with big ecosystem impact.

You can mention things like dependencies, failure risks, next steps, 
but let your reasoning guide what's actually important to say.

Don't force structure - think freely and share what matters.

/think deeply about what's most useful to share, then provide your insights"""
    
    return prompt


def _get_recent_work_context() -> str:
    """Get recent work from architecture enhancer."""
    try:
        from memory.architecture_enhancer import work_log
        
        entries = work_log.get_recent_entries(hours=1, include_processed=True)
        
        if entries:
            return "\n".join([
                f"- {e.get('content', '')[:100]}"
                for e in entries[-5:]
            ])
    except Exception as e:
        logger.debug(f"Recent work context unavailable: {e}")
    
    return "Unknown"


def _llm_analyze_post_work(prompt: str) -> Optional[Dict[str, Any]]:
    """
    Call LLM via priority queue for post-work analysis.

    Uses P4 BACKGROUND priority with post_analysis profile.
    Returns structured analysis or None if LLM unavailable.
    """
    try:
        from memory.llm_priority_queue import llm_generate, Priority

        system_prompt = "You are a senior software architect doing post-work analysis. Use /think for deep reasoning, then provide concise analysis."

        result = llm_generate(
            system_prompt=system_prompt,
            user_prompt=prompt,
            priority=Priority.BACKGROUND,
            profile="post_analysis",
            caller="post_work_analysis",
            timeout_s=45.0,
        )

        if not result:
            return None

        # Remove thinking tags (we want the analysis, not the thinking process)
        content = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()

        return {'analysis_text': content} if content else None

    except Exception as e:
        logger.error(f"Post-work LLM analysis failed: {e}")
        return None


def _format_post_work_analysis(analysis: Dict[str, Any]) -> str:
    """Format analysis for injection into post-hook."""
    
    lines = []
    lines.append("")
    lines.append("╔══════════════════════════════════════════════════════════════════════╗")
    lines.append("║  🔮 POST-WORK ANALYSIS (LLM Thinking Mode)                           ║")
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("")
    
    # Add LLM's analysis
    analysis_text = analysis.get('analysis_text', '')
    
    for line in analysis_text.split('\n'):
        if line.strip():
            lines.append(f"  {line}")
    
    lines.append("")
    lines.append("╚══════════════════════════════════════════════════════════════════════╝")
    lines.append("")
    
    return "\n".join(lines)


if __name__ == "__main__":
    print("🔮 Post-Work Analysis Engine - Test\n")
    
    # Test with example work
    analysis = generate_post_work_analysis(
        work_summary="Built anticipatory butler + strategic planning system",
        files_modified=[
            "memory/anticipatory_butler.py",
            "memory/strategic_planner.py"
        ],
        session_context="Building autonomous learning ecosystem for Context DNA"
    )
    
    if analysis:
        print(analysis)
    else:
        print("⚠️  Analysis not generated (LLM may be busy or unavailable)")
