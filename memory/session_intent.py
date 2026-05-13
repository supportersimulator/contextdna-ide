#!/usr/bin/env python3
"""
Session Intent Tracking - Synaptic's Smart Context System

Instead of mirroring raw dialogue, track WHAT Aaron is trying to do.
Intent-based context is 10x more useful than text transcripts.

Architecture:
- Session starts → capture intent (one sentence goal)
- Work progresses → optional focus updates
- Session ends → capture outcome
- Query by intent similarity, not keyword matching
"""

import os
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict


@dataclass
class SessionIntent:
    """A work session's intent - what Aaron is trying to accomplish."""
    session_id: str
    intent: str  # One sentence: "Make Synaptic Chat actually useful"
    started_at: str
    focus_updates: List[Dict[str, str]]  # [{timestamp, focus}, ...]
    outcome: Optional[str] = None  # What happened? Success/blocked/pivoted
    ended_at: Optional[str] = None
    tags: List[str] = None  # Auto-extracted: ["synaptic", "chat", "context"]

    def __post_init__(self):
        if self.tags is None:
            self.tags = self._extract_tags()
        if self.focus_updates is None:
            self.focus_updates = []

    def _extract_tags(self) -> List[str]:
        """Extract key terms from intent for indexing."""
        # Simple keyword extraction - could use embeddings later
        stopwords = {'the', 'a', 'an', 'is', 'are', 'to', 'for', 'and', 'or', 'it', 'on', 'in', 'with'}
        words = self.intent.lower().replace(',', '').replace('.', '').split()
        return [w for w in words if len(w) > 2 and w not in stopwords][:10]


class SessionIntentTracker:
    """
    Track session intents for smart context retrieval.

    Usage:
        tracker = SessionIntentTracker()

        # Start session with intent
        tracker.start_session("Make Synaptic Chat UI actually useful with real context")

        # Optional: update focus as work shifts
        tracker.update_focus("Now debugging why dialogue mirror isn't capturing")

        # End session with outcome
        tracker.end_session("Built session intent tracking instead - smarter approach")

        # Query for relevant past sessions
        relevant = tracker.find_relevant("synaptic context")
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path.home() / ".context-dna" / ".session_intents.db")
        self.db_path = db_path
        self._ensure_db()
        self._current_session: Optional[SessionIntent] = None

    def _ensure_db(self):
        """Create database if needed."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    intent TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    outcome TEXT,
                    tags TEXT,
                    focus_updates TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_intent ON sessions(intent)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tags ON sessions(tags)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_started ON sessions(started_at)")

    def start_session(self, intent: str, session_id: str = None) -> str:
        """Start a new session with an intent."""
        if session_id is None:
            session_id = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        self._current_session = SessionIntent(
            session_id=session_id,
            intent=intent,
            started_at=datetime.utcnow().isoformat(),
            focus_updates=[]
        )

        # Persist immediately
        self._save_session(self._current_session)
        return session_id

    def update_focus(self, new_focus: str):
        """Update what we're currently focused on within the session."""
        if not self._current_session:
            # Auto-start session with this as the intent
            self.start_session(new_focus)
            return

        self._current_session.focus_updates.append({
            "timestamp": datetime.utcnow().isoformat(),
            "focus": new_focus
        })
        self._save_session(self._current_session)

    def end_session(self, outcome: str = None):
        """End the current session with an outcome."""
        if not self._current_session:
            return

        self._current_session.ended_at = datetime.utcnow().isoformat()
        self._current_session.outcome = outcome
        self._save_session(self._current_session)
        self._current_session = None

    def _save_session(self, session: SessionIntent):
        """Persist session to database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO sessions
                (session_id, intent, started_at, ended_at, outcome, tags, focus_updates)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                session.session_id,
                session.intent,
                session.started_at,
                session.ended_at,
                session.outcome,
                json.dumps(session.tags),
                json.dumps(session.focus_updates)
            ))

    def find_relevant(self, query: str, limit: int = 5) -> List[SessionIntent]:
        """Find sessions relevant to a query by tag/intent matching."""
        query_tags = set(query.lower().split())

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT session_id, intent, started_at, ended_at, outcome, tags, focus_updates
                FROM sessions
                ORDER BY started_at DESC
                LIMIT 50
            """).fetchall()

        # Score by tag overlap
        scored = []
        for row in rows:
            session_tags = set(json.loads(row[5]) if row[5] else [])
            intent_words = set(row[1].lower().split())

            # Score: tag matches + intent word matches
            score = len(query_tags & session_tags) * 2 + len(query_tags & intent_words)

            if score > 0:
                scored.append((score, SessionIntent(
                    session_id=row[0],
                    intent=row[1],
                    started_at=row[2],
                    ended_at=row[3],
                    outcome=row[4],
                    tags=json.loads(row[5]) if row[5] else [],
                    focus_updates=json.loads(row[6]) if row[6] else []
                )))

        # Return top matches
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:limit]]

    def get_recent(self, days: int = 7, limit: int = 10) -> List[SessionIntent]:
        """Get recent sessions."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT session_id, intent, started_at, ended_at, outcome, tags, focus_updates
                FROM sessions
                WHERE started_at > ?
                ORDER BY started_at DESC
                LIMIT ?
            """, (cutoff, limit)).fetchall()

        return [SessionIntent(
            session_id=row[0],
            intent=row[1],
            started_at=row[2],
            ended_at=row[3],
            outcome=row[4],
            tags=json.loads(row[5]) if row[5] else [],
            focus_updates=json.loads(row[6]) if row[6] else []
        ) for row in rows]

    def get_current_intent(self) -> Optional[str]:
        """Get current session's intent."""
        if self._current_session:
            # Return latest focus or original intent
            if self._current_session.focus_updates:
                return self._current_session.focus_updates[-1]["focus"]
            return self._current_session.intent
        return None

    def get_context_summary(self, query: str = None) -> str:
        """Get a concise context summary for LLM consumption."""
        parts = []

        # Current session
        if self._current_session:
            parts.append(f"Current: {self._current_session.intent}")
            if self._current_session.focus_updates:
                latest = self._current_session.focus_updates[-1]["focus"]
                parts.append(f"Focus: {latest}")

        # Relevant past sessions
        if query:
            relevant = self.find_relevant(query, limit=3)
            if relevant:
                parts.append("Related past work:")
                for s in relevant:
                    outcome = f" -> {s.outcome}" if s.outcome else ""
                    parts.append(f"  {s.intent}{outcome}")

        return "\n".join(parts) if parts else "No session context."


# Convenience functions for quick access
_tracker: Optional[SessionIntentTracker] = None

def get_tracker() -> SessionIntentTracker:
    global _tracker
    if _tracker is None:
        _tracker = SessionIntentTracker()
    return _tracker

def start_session(intent: str) -> str:
    """Start a session with an intent."""
    return get_tracker().start_session(intent)

def update_focus(focus: str):
    """Update current focus."""
    get_tracker().update_focus(focus)

def end_session(outcome: str = None):
    """End session with outcome."""
    get_tracker().end_session(outcome)

def get_context(query: str = None) -> str:
    """Get context summary for current query."""
    return get_tracker().get_context_summary(query)

def find_relevant(query: str) -> List[SessionIntent]:
    """Find relevant past sessions."""
    return get_tracker().find_relevant(query)


if __name__ == "__main__":
    import sys

    tracker = SessionIntentTracker()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python session_intent.py start 'Make Synaptic Chat useful'")
        print("  python session_intent.py focus 'Debugging dialogue mirror'")
        print("  python session_intent.py end 'Built intent tracking instead'")
        print("  python session_intent.py find 'synaptic context'")
        print("  python session_intent.py recent")
        print("  python session_intent.py context 'query'")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "start" and len(sys.argv) > 2:
        intent = " ".join(sys.argv[2:])
        sid = tracker.start_session(intent)
        print(f"Started: {sid}")
        print(f"Intent: {intent}")

    elif cmd == "focus" and len(sys.argv) > 2:
        focus = " ".join(sys.argv[2:])
        tracker.update_focus(focus)
        print(f"Focus updated: {focus}")

    elif cmd == "end":
        outcome = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        tracker.end_session(outcome)
        print(f"Session ended. Outcome: {outcome or 'none'}")

    elif cmd == "find" and len(sys.argv) > 2:
        query = " ".join(sys.argv[2:])
        results = tracker.find_relevant(query)
        print(f"Found {len(results)} relevant sessions:")
        for s in results:
            print(f"  [{s.started_at[:10]}] {s.intent}")
            if s.outcome:
                print(f"    -> {s.outcome}")

    elif cmd == "recent":
        results = tracker.get_recent()
        print(f"Recent sessions ({len(results)}):")
        for s in results:
            print(f"  [{s.started_at[:10]}] {s.intent}")
            if s.outcome:
                print(f"    -> {s.outcome}")

    elif cmd == "context":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        print(tracker.get_context_summary(query))

    else:
        print(f"Unknown command: {cmd}")
