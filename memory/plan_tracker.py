"""
plan_tracker.py - Active plan file tracking for Synaptic review alignment

Watches ~/.claude/plans/*.md for the most recently modified plan file.
Provides plan content and checklist progress for Synaptic reviewer.

Usage:
    from memory.plan_tracker import get_active_plan, get_plan_progress

Architecture:
    Plan files are created by Claude Code's plan mode.
    Synaptic reviewer uses active plan as alignment reference.
    Progress is tracked via [ ] vs [x] checklist parsing.
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("context_dna.plan_tracker")

PLANS_DIR = Path(os.path.expanduser("~/.claude/plans"))


def get_active_plan() -> Optional[dict]:
    """Get the most recently modified plan file.

    Returns dict with: path, name, content, modified_at
    Or None if no plans exist.
    """
    if not PLANS_DIR.exists():
        return None

    plans = list(PLANS_DIR.glob("*.md"))
    if not plans:
        return None

    # Most recently modified = active plan
    latest = max(plans, key=lambda p: p.stat().st_mtime)

    try:
        content = latest.read_text()
    except Exception as e:
        logger.error(f"Failed to read plan {latest}: {e}")
        return None

    return {
        "path": str(latest),
        "name": latest.stem,
        "content": content,
        "modified_at": latest.stat().st_mtime,
    }


def get_plan_progress(plan_path: Optional[str] = None) -> dict:
    """Parse checklist progress from a plan file.

    Returns: { total, completed, pending, percentage, items }
    """
    if plan_path:
        path = Path(plan_path)
    else:
        active = get_active_plan()
        if not active:
            return {"total": 0, "completed": 0, "pending": 0, "percentage": 0, "items": []}
        path = Path(active["path"])

    try:
        content = path.read_text()
    except Exception:
        return {"total": 0, "completed": 0, "pending": 0, "percentage": 0, "items": []}

    items = []

    # Match Markdown checklists: - [ ] text or - [x] text
    for match in re.finditer(r"^[\s]*[-*]\s+\[([ xX])\]\s+(.+)$", content, re.MULTILINE):
        checked = match.group(1).lower() == "x"
        text = match.group(2).strip()
        items.append({"text": text, "completed": checked})

    total = len(items)
    completed = sum(1 for i in items if i["completed"])

    return {
        "total": total,
        "completed": completed,
        "pending": total - completed,
        "percentage": round((completed / total * 100) if total > 0 else 0),
        "items": items,
    }


def get_plan_summary(max_chars: int = 2000) -> Optional[str]:
    """Get a condensed version of the active plan for LLM context.

    Extracts: title, approach/implementation sections, checklist items.
    Truncated to max_chars to stay within token budgets.
    """
    plan = get_active_plan()
    if not plan:
        return None

    content = plan["content"]
    progress = get_plan_progress(plan["path"])

    # Extract key sections
    lines = content.split("\n")
    summary_parts = []

    # Title (first # heading)
    for line in lines:
        if line.startswith("# "):
            summary_parts.append(line)
            break

    # Progress bar
    if progress["total"] > 0:
        summary_parts.append(
            f"\nProgress: {progress['completed']}/{progress['total']} "
            f"({progress['percentage']}%)"
        )

    # Key sections (## headings + first few lines under each)
    current_section = None
    section_lines = 0
    for line in lines:
        if line.startswith("## "):
            current_section = line
            section_lines = 0
            summary_parts.append(f"\n{line}")
        elif current_section and section_lines < 8:
            # Include first 8 lines of each section
            if line.strip():
                summary_parts.append(line)
                section_lines += 1

    summary = "\n".join(summary_parts)

    # Truncate to budget
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n... (truncated)"

    return summary


def list_plans() -> list[dict]:
    """List all plan files with basic metadata."""
    if not PLANS_DIR.exists():
        return []

    plans = []
    for p in sorted(PLANS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            content = p.read_text()
            # Extract title from first # heading
            title = p.stem
            for line in content.split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()
                    break

            progress = get_plan_progress(str(p))

            plans.append({
                "path": str(p),
                "name": p.stem,
                "title": title,
                "modified_at": p.stat().st_mtime,
                "progress": progress["percentage"],
                "total_tasks": progress["total"],
            })
        except Exception:
            continue

    return plans
