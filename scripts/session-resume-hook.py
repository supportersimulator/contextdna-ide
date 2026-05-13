#!/usr/bin/env python3
"""Session resume hook — queries episodic-memory for recent context at SessionStart.

Outputs a compact "Resume Brief" so new sessions know where you left off.
Designed for <2s execution. Pure SQL, no LLM calls.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


DB_PATH = os.environ.get(
    "EPISODIC_MEMORY_DB_PATH",
    str(Path.home() / ".config" / "superpowers" / "conversation-index" / "db.sqlite"),
)

# Derive project key from CWD (episodic-memory format: path with / replaced by -)
CWD = os.environ.get("CWD", os.getcwd())


def _project_key(cwd: str) -> str:
    """Convert /Users/foo/dev/bar to -Users-foo-dev-bar (episodic-memory convention)."""
    return cwd.replace("/", "-")


def _truncate(text: str, limit: int = 200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _is_noise(msg: str) -> bool:
    """Filter out fleet probes, task notifications, and other noise."""
    if not msg:
        return True
    noise_prefixes = [
        "Fleet task from",
        "<task-notification>",
        "This session is being continued",
    ]
    for prefix in noise_prefixes:
        if msg.strip().startswith(prefix):
            return True
    return False


def query_recent_sessions(db_path: str, project: str, limit: int = 3) -> list[dict]:
    """Get recent non-noise sessions with their key exchanges."""
    if not Path(db_path).exists():
        return []

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Get distinct recent sessions (non-sidechain, non-noise)
        rows = conn.execute("""
            SELECT session_id, COUNT(*) as exchange_count,
                   MIN(timestamp) as started, MAX(timestamp) as ended
            FROM exchanges
            WHERE project = ?
              AND (is_sidechain = 0 OR is_sidechain IS NULL)
            GROUP BY session_id
            HAVING exchange_count > 1
            ORDER BY ended DESC
            LIMIT ?
        """, (project, limit + 5)).fetchall()  # fetch extra to filter noise

        sessions = []
        for row in rows:
            if len(sessions) >= limit:
                break
            sid = row["session_id"]

            # Get last few human messages from this session
            msgs = conn.execute("""
                SELECT user_message, timestamp
                FROM exchanges
                WHERE session_id = ?
                  AND (is_sidechain = 0 OR is_sidechain IS NULL)
                ORDER BY timestamp DESC
                LIMIT 10
            """, (sid,)).fetchall()

            # Filter noise
            human_msgs = [
                {"text": _truncate(m["user_message"], 180), "ts": m["timestamp"]}
                for m in msgs
                if not _is_noise(m["user_message"])
            ]

            if not human_msgs:
                continue

            sessions.append({
                "session_id": sid[:8],
                "exchanges": row["exchange_count"],
                "started": row["started"],
                "ended": row["ended"],
                "last_messages": human_msgs[:3],  # top 3 non-noise
            })

        return sessions
    except Exception:
        return []
    finally:
        conn.close()


def query_recent_tool_patterns(db_path: str, project: str) -> list[str]:
    """Get most-used tools from recent sessions for context."""
    if not Path(db_path).exists():
        return []

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows = conn.execute("""
            SELECT tc.tool_name, COUNT(*) as cnt
            FROM tool_calls tc
            JOIN exchanges e ON tc.exchange_id = e.id
            WHERE e.project = ?
              AND e.timestamp > ?
            GROUP BY tc.tool_name
            ORDER BY cnt DESC
            LIMIT 5
        """, (project, cutoff)).fetchall()
        return [f"{r[0]}({r[1]})" for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def format_resume_brief(sessions: list[dict], tools: list[str]) -> str:
    """Format compact resume brief for SessionStart injection."""
    if not sessions:
        return ""

    parts = [
        "",
        "================================================================",
        "[SESSION RESUME: episodic-memory brief]",
        "================================================================",
        "",
    ]

    for i, s in enumerate(sessions):
        age = _format_age(s["ended"])
        label = "LAST SESSION" if i == 0 else f"SESSION -{i + 1}"
        parts.append(f"## {label} ({s['session_id']}.. | {s['exchanges']} exchanges | {age})")
        for msg in s["last_messages"]:
            parts.append(f"  - {msg['text']}")
        parts.append("")

    if tools:
        parts.append(f"Recent tools (24h): {', '.join(tools)}")
        parts.append("")

    parts.append("Use episodic-memory search for deeper context if needed.")
    parts.append("")

    return "\n".join(parts)


def _format_age(iso_ts: str) -> str:
    """Format timestamp as human-readable age."""
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m ago"
        if hours < 24:
            return f"{int(hours)}h ago"
        return f"{int(hours / 24)}d ago"
    except Exception:
        return "unknown"


def main():
    project = _project_key(CWD)
    sessions = query_recent_sessions(DB_PATH, project, limit=2)

    if not sessions:
        sys.exit(0)

    tools = query_recent_tool_patterns(DB_PATH, project)
    brief = format_resume_brief(sessions, tools)

    if brief:
        print(brief)


if __name__ == "__main__":
    main()
