#!/usr/bin/env python3
"""
Session Recovery Directive - Top of Webhook Injection

Detects if this is the first message in a session and directs agent
to session rehydration BEFORE proceeding.

Critical for Context DNA projects where webhook/architecture context is foundational.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def is_new_session(session_id: str = None) -> bool:
    """
    Detect if this is likely a new or recovered session.
    
    Heuristics:
    1. No session_id provided (new session)
    2. Session not in recent activity (>10 min since last message)
    3. First message indicators in prompt
    """
    if not session_id:
        return True
    
    # Check dialogue mirror for recent activity
    try:
        from memory.db_utils import get_unified_db_path
        db_path = get_unified_db_path(
            Path(__file__).parent / ".dialogue_mirror.db"
        )
        if not db_path.exists():
            return True

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        # Check if we have recent messages for this session
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        
        cursor.execute("""
            SELECT COUNT(*) FROM threads 
            WHERE session_id = ? AND last_activity > ?
        """, (session_id, cutoff))
        
        count = cursor.fetchone()[0]
        conn.close()
        
        # If no recent activity, likely new/recovered session
        return count == 0
    
    except Exception:
        # If can't determine, assume new (safer to rehydrate)
        return True


def generate_session_recovery_directive(prompt: str, session_id: str = None) -> str:
    """
    Generate session recovery directive for top of webhook.
    
    Only shows on:
    - First message in session
    - Session recovery after crash
    - Context DNA project work (webhooks, architecture, foundational)
    
    Returns:
        Formatted directive or empty string if not needed
    """
    # Check if this is Context DNA foundational work
    is_contextdna_work = any(keyword in prompt.lower() for keyword in [
        'webhook', 'context dna', 'contextdna', 'section', 'payload',
        'injection', 'hook', 'mcp', 'destination', 'architecture',
        'persistent_hook', 'session historian', 'brain', 'memory system'
    ])
    
    # Always show for new sessions on Context DNA work
    # Or if prompt mentions session/crash/recovery
    should_show = (
        is_contextdna_work and is_new_session(session_id)
    ) or any(keyword in prompt.lower() for keyword in [
        'session crash', 'rehydrate', 'context loss', 'recovered session',
        'continued from previous'
    ])
    
    if not should_show:
        return ""
    
    # Generate directive
    lines = []
    lines.append("╔══════════════════════════════════════════════════════════════════════╗")
    lines.append("║  🧬 CONTEXT DNA PROJECT - SESSION RECOVERY PROTOCOL                  ║")
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("║                                                                      ║")
    lines.append("║  ⚠️  CRITICAL: Context DNA webhooks/architecture is FOUNDATIONAL     ║")
    lines.append("║                                                                      ║")
    lines.append("║  If this is your FIRST MESSAGE in this session, you MUST run:       ║")
    lines.append("║                                                                      ║")
    lines.append(f"║    cd {os.getcwd():<54}║")
    lines.append("║    PYTHONPATH=. .venv/bin/python3 memory/session_historian.py \\     ║")
    lines.append("║      rehydrate --project context-dna                                ║")
    lines.append("║                                                                      ║")
    lines.append("║  This recovers:                                                      ║")
    lines.append("║  • Aaron's last 5 messages (exact priorities)                       ║")
    lines.append("║  • Your last 5 outputs (where you left off)                         ║")
    lines.append("║  • Spawned agents that may have completed                           ║")
    lines.append("║  • Critical architectural context                                    ║")
    lines.append("║                                                                      ║")
    lines.append("║  WHY: Context DNA sessions often crash from prompt length.          ║")
    lines.append("║       Session Historian captures gold every 2 minutes.              ║")
    lines.append("║       Rehydration prevents re-explaining everything.                ║")
    lines.append("║                                                                      ║")
    lines.append("║  If NOT first message: This is a reminder of contextual             ║")
    lines.append("║  awareness importance before acting on foundational systems.        ║")
    lines.append("║                                                                      ║")
    lines.append("╚══════════════════════════════════════════════════════════════════════╝")
    lines.append("")
    
    return "\n".join(lines)


def _generate_session_recovery_directive(prompt: str, session_id: str = None) -> str:
    """Wrapper for session recovery directive generation."""
    try:
        return generate_session_recovery_directive(prompt, session_id)
    except Exception:
        # Non-critical - if this fails, webhook continues without it
        return ""
