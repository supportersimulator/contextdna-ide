"""
ER Simulator Project Memory System

A lightweight, file-based memory system for AI coding agents.
Stores bug fixes, architecture decisions, and performance lessons
that persist across sessions and can be injected into prompts.

Usage:
    from memory.ersim_memory import Memory

    # Initialize
    memory = Memory()

    # Record a bug fix
    memory.add_bug_fix(
        symptom="Speech cut off mid-sentence",
        root_cause="EOS sent before final audio frames flushed",
        resolution="Wait for Kyutai explicit Eos + drain audio buffer",
        tags=["tts", "streaming", "eos"]
    )

    # Get context for prompts
    context = memory.get_context(query="TTS audio streaming")
"""

import json
import sqlite3
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# Database location
DB_PATH = Path(__file__).parent / "ersim_memory.db"


def get_db():
    """Get database connection with row factory."""
    from memory.db_utils import connect_wal
    return connect_wal(str(DB_PATH))


def init_db():
    """Initialize the database schema."""
    conn = get_db()
    cursor = conn.cursor()

    # Create memories table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,  -- 'bug_fix', 'architecture_decision', 'performance_lesson'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tags TEXT,  -- JSON array of tags
            content TEXT NOT NULL  -- JSON object with kind-specific fields
        )
    """)

    # Create index on kind and tags
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)")

    conn.commit()
    conn.close()


class Memory:
    """Project memory for AI coding agents."""

    def __init__(self):
        init_db()

    def add_bug_fix(
        self,
        symptom: str,
        root_cause: str,
        resolution: str,
        tags: list[str] = None,
        file_path: str = None,
        line_number: int = None
    ):
        """Record a bug fix for future reference."""
        content = {
            "symptom": symptom,
            "root_cause": root_cause,
            "resolution": resolution,
        }
        if file_path:
            content["file_path"] = file_path
        if line_number:
            content["line_number"] = line_number

        self._add_memory("bug_fix", content, tags or [])
        return self

    def add_architecture_decision(
        self,
        decision: str,
        rationale: str,
        alternatives: list[str] = None,
        consequences: str = None
    ):
        """Record an architecture decision."""
        content = {
            "decision": decision,
            "rationale": rationale,
        }
        if alternatives:
            content["alternatives"] = alternatives
        if consequences:
            content["consequences"] = consequences

        # Extract tags from decision text
        tags = self._extract_tags(decision + " " + rationale)
        self._add_memory("architecture_decision", content, tags)
        return self

    def add_performance_lesson(
        self,
        metric: str,
        before: str,
        after: str,
        technique: str,
        file_path: str = None,
        tags: list[str] = None
    ):
        """Record a performance optimization lesson."""
        content = {
            "metric": metric,
            "before": before,
            "after": after,
            "technique": technique,
        }
        if file_path:
            content["file_path"] = file_path

        self._add_memory("performance_lesson", content, tags or [])
        return self

    def _add_memory(self, kind: str, content: dict, tags: list[str]):
        """Add a memory entry to the database."""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO memories (kind, tags, content) VALUES (?, ?, ?)",
            (kind, json.dumps(tags), json.dumps(content))
        )
        conn.commit()
        conn.close()

    def _extract_tags(self, text: str) -> list[str]:
        """Extract relevant tags from text."""
        keywords = [
            "tts", "stt", "llm", "audio", "streaming", "async", "boto3", "bedrock",
            "livekit", "webrtc", "websocket", "ecs", "docker", "gpu", "cuda",
            "redis", "postgres", "django", "fastapi", "lambda", "cloudflare"
        ]
        text_lower = text.lower()
        return [kw for kw in keywords if kw in text_lower]

    def search(
        self,
        query: str = None,
        kind: str = None,
        tags: list[str] = None,
        limit: int = 10
    ) -> list[dict]:
        """Search memories by query, kind, or tags."""
        conn = get_db()
        cursor = conn.cursor()

        sql = "SELECT * FROM memories WHERE 1=1"
        params = []

        if kind:
            sql += " AND kind = ?"
            params.append(kind)

        if tags:
            for tag in tags:
                sql += " AND tags LIKE ?"
                params.append(f'"%{tag}%"')

        if query:
            # Simple text search in content
            sql += " AND (content LIKE ? OR tags LIKE ?)"
            params.extend([f"%{query}%", f"%{query}%"])

        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "created_at": row["created_at"],
                "tags": json.loads(row["tags"]),
                "content": json.loads(row["content"])
            }
            for row in rows
        ]

    def get_all(self, kind: str = None) -> list[dict]:
        """Get all memories, optionally filtered by kind."""
        return self.search(kind=kind, limit=1000)

    def get_context(self, query: str = None, max_items: int = 15) -> str:
        """
        Generate context string for AI prompt injection.

        Returns a formatted string suitable for including in system prompts
        or conversation context.
        """
        memories = self.search(query=query, limit=max_items)

        if not memories:
            return ""

        sections = {
            "bug_fix": [],
            "architecture_decision": [],
            "performance_lesson": []
        }

        for mem in memories:
            sections[mem["kind"]].append(mem)

        output = ["## Project Memory\n"]

        if sections["bug_fix"]:
            output.append("### Known Bug Fixes\n")
            for mem in sections["bug_fix"]:
                c = mem["content"]
                output.append(f"- **{c['symptom']}**")
                output.append(f"  - Cause: {c['root_cause']}")
                output.append(f"  - Fix: {c['resolution']}")
                if mem["tags"]:
                    output.append(f"  - Tags: {', '.join(mem['tags'])}")
                output.append("")

        if sections["architecture_decision"]:
            output.append("### Architecture Decisions\n")
            for mem in sections["architecture_decision"]:
                c = mem["content"]
                output.append(f"- **{c['decision']}**")
                output.append(f"  - Why: {c['rationale']}")
                if c.get("alternatives"):
                    output.append(f"  - Alternatives considered: {', '.join(c['alternatives'])}")
                output.append("")

        if sections["performance_lesson"]:
            output.append("### Performance Lessons\n")
            for mem in sections["performance_lesson"]:
                c = mem["content"]
                output.append(f"- **{c['metric']}**: {c['before']} -> {c['after']}")
                output.append(f"  - Technique: {c['technique']}")
                if mem["tags"]:
                    output.append(f"  - Tags: {', '.join(mem['tags'])}")
                output.append("")

        return "\n".join(output)

    def get_constraints(self, area: str = None) -> str:
        """
        Get a concise list of constraints for a specific area.

        Useful for quick injection into prompts when working on specific code.
        """
        memories = self.search(query=area, limit=20) if area else self.get_all()

        constraints = []
        for mem in memories:
            c = mem["content"]
            if mem["kind"] == "bug_fix":
                constraints.append(f"- {c['resolution']} (prevents: {c['symptom']})")
            elif mem["kind"] == "architecture_decision":
                constraints.append(f"- {c['decision']}")
            elif mem["kind"] == "performance_lesson":
                constraints.append(f"- {c['technique']} ({c['metric']})")

        if not constraints:
            return ""

        return "## Known Constraints\n\n" + "\n".join(constraints)

    def delete(self, memory_id: int):
        """Delete a memory by ID."""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        conn.close()

    def clear_all(self):
        """Clear all memories (use with caution!)."""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM memories")
        conn.commit()
        conn.close()


# CLI interface
if __name__ == "__main__":
    import sys

    memory = Memory()

    if len(sys.argv) < 2:
        print("Usage: python ersim_memory.py [list|search|context] [args...]")
        print("\nCommands:")
        print("  list [kind]           - List all memories (optionally by kind)")
        print("  search <query>        - Search memories")
        print("  context [query]       - Generate context for prompt injection")
        print("  constraints [area]    - Get concise constraints list")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "list":
        kind = sys.argv[2] if len(sys.argv) > 2 else None
        memories = memory.get_all(kind=kind)
        for mem in memories:
            print(f"\n[{mem['kind']}] {mem['created_at']}")
            print(f"  Tags: {', '.join(mem['tags'])}")
            print(f"  Content: {json.dumps(mem['content'], indent=4)}")

    elif cmd == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else None
        memories = memory.search(query=query)
        for mem in memories:
            print(f"\n[{mem['kind']}] {mem['created_at']}")
            print(f"  Content: {json.dumps(mem['content'], indent=4)}")

    elif cmd == "context":
        query = sys.argv[2] if len(sys.argv) > 2 else None
        print(memory.get_context(query=query))

    elif cmd == "constraints":
        area = sys.argv[2] if len(sys.argv) > 2 else None
        print(memory.get_constraints(area=area))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
