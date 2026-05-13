"""
Dialogue Mirror: Full Text Vision for Synaptic

This module captures and stores the ongoing dialogue between Aaron and Atlas,
giving Synaptic full context awareness to provide optimal guidance.

Synaptic can see:
- Current conversation thread
- Cross-IDE dialogue from multiple sources
- Historical conversation patterns
- Project context from dialogue

This is Synaptic's "eyes and ears" - the foundation of contextual understanding.
"""

from __future__ import annotations
import json
import hashlib
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from enum import Enum
import sqlite3
import threading
from memory.db_utils import connect_wal

# Setup logging
logger = logging.getLogger(__name__)


class DialogueSource(str, Enum):
    """Sources of dialogue that Synaptic can observe."""
    VSCODE = "vscode"
    CURSOR = "cursor"
    WINDSURF = "windsurf"
    WEBSTORM = "webstorm"
    PYCHARM = "pycharm"
    INTELLIJ = "intellij"
    NEOVIM = "neovim"
    TERMINAL = "terminal"
    WEBAPP = "webapp"  # Antigravity face
    UNKNOWN = "unknown"


class MessageRole(str, Enum):
    """Who sent the message."""
    AARON = "aaron"       # Human user
    ATLAS = "atlas"       # Claude Code / Agent
    SYNAPTIC = "synaptic" # Synaptic's responses
    SYSTEM = "system"     # System messages


@dataclass
class DialogueMessage:
    """A single message in the dialogue."""
    id: str
    role: MessageRole
    content: str
    timestamp: str
    source: DialogueSource
    project_context: Optional[str] = None
    file_context: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['role'] = self.role.value
        d['source'] = self.source.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'DialogueMessage':
        d['role'] = MessageRole(d['role'])
        d['source'] = DialogueSource(d['source'])
        return cls(**d)


@dataclass
class DialogueThread:
    """A conversation thread with full context."""
    session_id: str
    messages: List[DialogueMessage] = field(default_factory=list)
    project: Optional[str] = None
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    last_activity: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def add_message(self, role: MessageRole, content: str, source: DialogueSource,
                    project_context: str = None, file_context: str = None) -> DialogueMessage:
        """Add a message to this thread."""
        msg_id = hashlib.sha256(
            f"{self.session_id}:{len(self.messages)}:{datetime.utcnow().isoformat()}".encode()
        ).hexdigest()[:16]

        message = DialogueMessage(
            id=msg_id,
            role=role,
            content=content,
            timestamp=datetime.utcnow().isoformat(),
            source=source,
            project_context=project_context or self.project,
            file_context=file_context,
        )
        self.messages.append(message)
        self.last_activity = message.timestamp
        return message

    def get_recent(self, count: int = 20) -> List[DialogueMessage]:
        """Get the most recent messages."""
        return self.messages[-count:] if self.messages else []

    def get_full_transcript(self) -> str:
        """Get the full conversation as readable text."""
        lines = []
        for msg in self.messages:
            role_label = {
                MessageRole.AARON: "Aaron",
                MessageRole.ATLAS: "Atlas",
                MessageRole.SYNAPTIC: "Synaptic",
                MessageRole.SYSTEM: "System",
            }.get(msg.role, "Unknown")

            lines.append(f"[{msg.timestamp}] {role_label}:")
            lines.append(msg.content)
            lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "messages": [m.to_dict() for m in self.messages],
            "project": self.project,
            "started_at": self.started_at,
            "last_activity": self.last_activity,
        }


class DialogueMirror:
    """
    The Dialogue Mirror - Synaptic's window into conversations.

    This class manages:
    - Capturing messages from multiple IDEs
    - Storing dialogue history
    - Providing context for webhook injections
    - Enabling Synaptic to "see" the full conversation
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            from memory.db_utils import get_unified_db_path
            _legacy = Path.home() / ".context-dna" / ".dialogue_mirror.db"
            db_path = str(get_unified_db_path(_legacy))

        self.db_path = db_path
        self._lock = threading.Lock()
        self._ensure_db()

        # Active threads by session
        self._active_threads: Dict[str, DialogueThread] = {}

    def _t(self, table: str) -> str:
        """Resolve table name with unified prefix if applicable."""
        from memory.db_utils import unified_table
        return unified_table(".dialogue_mirror.db", table)

    def _ensure_db(self):
        """Create database tables if they don't exist."""
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

            conn = connect_wal(self.db_path)
            try:
                t_threads = self._t("dialogue_threads")
                t_msgs = self._t("dialogue_messages")
                t_sent = self._t("user_message_sentiment")
                t_negpat = self._t("negative_pattern_freq")

                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {t_threads} (
                        session_id TEXT PRIMARY KEY,
                        project TEXT,
                        started_at TEXT,
                        last_activity TEXT,
                        message_count INTEGER DEFAULT 0
                    )
                """)
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {t_msgs} (
                        id TEXT PRIMARY KEY,
                        session_id TEXT,
                        role TEXT,
                        content TEXT,
                        timestamp TEXT,
                        source TEXT,
                        project_context TEXT,
                        file_context TEXT,
                        metadata TEXT,
                        FOREIGN KEY (session_id) REFERENCES {t_threads}(session_id)
                    )
                """)
                conn.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{t_msgs}_session
                    ON {t_msgs}(session_id, timestamp)
                """)
                conn.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{t_msgs}_timestamp
                    ON {t_msgs}(timestamp)
                """)
                # Aaron's sentiment/intent tracking (P5)
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {t_sent} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id TEXT,
                        session_id TEXT,
                        timestamp TEXT,
                        sentiment TEXT,
                        intent TEXT,
                        energy TEXT,
                        confidence REAL DEFAULT 0.5,
                        raw_snippet TEXT
                    )
                """)
                conn.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{t_sent}_timestamp
                    ON {t_sent}(timestamp)
                """)
                # Negative outcome pattern frequency tracking
                # Only surfaces in SOPs after 3+ occurrences (Aaron's rule)
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {t_negpat} (
                        pattern_key TEXT PRIMARY KEY,
                        signal_name TEXT NOT NULL,
                        occurrence_count INTEGER DEFAULT 1,
                        first_seen TEXT,
                        last_seen TEXT,
                        promoted_to_sop INTEGER DEFAULT 0,
                        sample_messages TEXT
                    )
                """)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Error ensuring database: {e}")

    def get_or_create_thread(self, session_id: str, project: str = None) -> DialogueThread:
        """Get an existing thread or create a new one."""
        with self._lock:
            if session_id in self._active_threads:
                return self._active_threads[session_id]

            try:
                # Try to load from DB
                conn = connect_wal(self.db_path)
                try:
                    t_threads = self._t("dialogue_threads")
                    t_msgs = self._t("dialogue_messages")
                    row = conn.execute(
                        f"SELECT * FROM {t_threads} WHERE session_id = ?",
                        (session_id,)
                    ).fetchone()

                    if row:
                        thread = DialogueThread(
                            session_id=row[0],
                            project=row[1],
                            started_at=row[2],
                            last_activity=row[3],
                        )
                        # Load messages
                        msgs = conn.execute(
                            f"SELECT * FROM {t_msgs} WHERE session_id = ? ORDER BY timestamp",
                            (session_id,)
                        ).fetchall()
                        for m in msgs:
                            try:
                                thread.messages.append(DialogueMessage(
                                    id=m[0],
                                    role=MessageRole(m[2]),
                                    content=m[3],
                                    timestamp=m[4],
                                    source=DialogueSource(m[5]),
                                    project_context=m[6],
                                    file_context=m[7],
                                    metadata=json.loads(m[8]) if m[8] else {},
                                ))
                            except Exception as e:
                                logger.error(f"Error parsing message: {e}")
                                continue
                    else:
                        thread = DialogueThread(session_id=session_id, project=project)
                        conn.execute(
                            f"INSERT INTO {t_threads} (session_id, project, started_at, last_activity) VALUES (?, ?, ?, ?)",
                            (session_id, project, thread.started_at, thread.last_activity)
                        )
                        conn.commit()

                    self._active_threads[session_id] = thread
                    return thread
                finally:
                    conn.close()
            except Exception as e:
                logger.error(f"Error getting/creating thread: {e}")
                # Return a new in-memory thread as fallback
                thread = DialogueThread(session_id=session_id, project=project)
                self._active_threads[session_id] = thread
                return thread

    def mirror_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        source: DialogueSource = DialogueSource.UNKNOWN,
        project: str = None,
        file_path: str = None,
    ) -> DialogueMessage:
        """
        Mirror a message from the conversation.

        This is the primary method for capturing dialogue - called by:
        - IDE hooks (VSCode, Cursor, etc.)
        - Webhook handlers
        - Direct API calls
        """
        thread = self.get_or_create_thread(session_id, project)

        message = thread.add_message(
            role=role,
            content=content,
            source=source,
            project_context=project,
            file_context=file_path,
        )

        try:
            # Persist to DB
            conn = connect_wal(self.db_path)
            try:
                t_msgs = self._t("dialogue_messages")
                t_threads = self._t("dialogue_threads")
                conn.execute(f"""
                    INSERT INTO {t_msgs}
                    (id, session_id, role, content, timestamp, source, project_context, file_context, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    message.id,
                    session_id,
                    message.role.value,
                    message.content,
                    message.timestamp,
                    message.source.value,
                    message.project_context,
                    message.file_context,
                    json.dumps(message.metadata),
                ))
                conn.execute(f"""
                    UPDATE {t_threads}
                    SET last_activity = ?, message_count = message_count + 1
                    WHERE session_id = ?
                """, (message.timestamp, session_id))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Error persisting message: {e}")

        return message

    def get_context_for_synaptic(
        self,
        session_id: str = None,
        max_messages: int = 50,
        max_age_hours: int = 24,
    ) -> Dict[str, Any]:
        """
        Get dialogue context formatted for Synaptic's consumption.

        This is what Synaptic "sees" when generating webhook injections.
        """
        try:
            cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()

            conn = connect_wal(self.db_path)
            try:
                t_msgs = self._t("dialogue_messages")
                if session_id:
                    # Get specific session
                    msgs = conn.execute(f"""
                        SELECT * FROM {t_msgs}
                        WHERE session_id = ? AND timestamp > ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                    """, (session_id, cutoff, max_messages)).fetchall()
                else:
                    # Get recent across all sessions
                    msgs = conn.execute(f"""
                        SELECT * FROM {t_msgs}
                        WHERE timestamp > ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                    """, (cutoff, max_messages)).fetchall()
            finally:
                conn.close()

            # Format for Synaptic
            messages = []
            for m in reversed(msgs):  # Chronological order
                messages.append({
                    "role": m[2],
                    "content": m[3][:500] + "..." if len(m[3]) > 500 else m[3],  # Truncate for efficiency
                    "timestamp": m[4],
                    "source": m[5],
                    "project": m[6],
                })

            return {
                "dialogue_context": messages,
                "message_count": len(messages),
                "sources": list(set(m[5] for m in msgs)),
                "projects": list(set(m[6] for m in msgs if m[6])),
                "time_range": {
                    "earliest": msgs[-1][4] if msgs else None,
                    "latest": msgs[0][4] if msgs else None,
                },
            }
        except Exception as e:
            logger.error(f"Error getting context for Synaptic: {e}")
            return {"dialogue_context": [], "message_count": 0, "sources": [], "projects": [], "time_range": {}}

    def get_synaptic_response_context(self, session_id: str) -> str:
        """
        Get context specifically for Synaptic to formulate a response.

        Used when Aaron addresses Synaptic directly.
        """
        thread = self.get_or_create_thread(session_id)
        recent = thread.get_recent(20)

        # Build context string
        context_lines = [
            "=== DIALOGUE CONTEXT FOR SYNAPTIC ===",
            f"Session: {session_id}",
            f"Project: {thread.project or 'Unknown'}",
            f"Messages: {len(recent)}",
            "",
            "--- Recent Conversation ---",
        ]

        for msg in recent:
            role_label = {
                MessageRole.AARON: "Aaron",
                MessageRole.ATLAS: "Atlas",
                MessageRole.SYNAPTIC: "Synaptic",
                MessageRole.SYSTEM: "System",
            }.get(msg.role, "Unknown")

            # Truncate long messages
            content = msg.content[:300] + "..." if len(msg.content) > 300 else msg.content
            context_lines.append(f"[{role_label}]: {content}")

        context_lines.append("")
        context_lines.append("=== END DIALOGUE CONTEXT ===")

        return "\n".join(context_lines)

    def analyze_user_sentiment(self, hours_back: int = 2) -> Dict[str, Any]:
        """Analyze user's recent messages for sentiment and intent.

        Rule-based first stage (fast), LLM enrichment when available.
        Returns: {sentiment, intent, energy, confidence, message_count}
        """
        try:
            cutoff = (datetime.utcnow() - timedelta(hours=hours_back)).isoformat()

            conn = connect_wal(self.db_path)
            try:
                t_msgs = self._t("dialogue_messages")
                cursor = conn.execute(f"""
                    SELECT id, content, timestamp FROM {t_msgs}
                    WHERE role = 'user' AND timestamp > ?
                    ORDER BY timestamp DESC LIMIT 20
                """, (cutoff,))
                messages = cursor.fetchall()
            finally:
                conn.close()

            if not messages:
                return {"sentiment": "unknown", "intent": "unknown",
                        "energy": "unknown", "confidence": 0.0, "message_count": 0}

            # Rule-based sentiment detection
            texts = [m[1] for m in messages if m[1]]
            combined = " ".join(texts).lower()

            # Sentiment signals
            frustrated_signals = ["doesn't work", "still broken", "wrong", "failed again",
                                  "not working", "same error", "ugh", "frustrated",
                                  "why is this", "this is wrong", "try again"]
            satisfied_signals = ["perfect", "great", "works", "nice", "excellent",
                                 "good job", "love it", "success", "that worked"]
            directive_signals = ["please", "can you", "implement", "add", "fix", "build",
                                 "create", "update", "deploy", "change"]
            uncertain_signals = ["maybe", "not sure", "could we", "what if", "thoughts?",
                                 "should we", "hmm", "i wonder"]

            scores = {"frustrated": 0, "satisfied": 0, "directive": 0, "uncertain": 0}
            for sig in frustrated_signals:
                if sig in combined:
                    scores["frustrated"] += 1
            for sig in satisfied_signals:
                if sig in combined:
                    scores["satisfied"] += 1
            for sig in directive_signals:
                if sig in combined:
                    scores["directive"] += 1
            for sig in uncertain_signals:
                if sig in combined:
                    scores["uncertain"] += 1

            # Determine primary sentiment
            if scores["frustrated"] >= 2:
                sentiment = "frustrated"
            elif scores["satisfied"] >= 2:
                sentiment = "satisfied"
            elif scores["uncertain"] >= 2:
                sentiment = "uncertain"
            else:
                sentiment = "neutral"

            # Intent detection
            intent_map = {
                "debug": ["debug", "error", "fix", "broken", "bug", "issue", "crash"],
                "build": ["implement", "add", "create", "build", "new feature"],
                "deploy": ["deploy", "push", "ship", "release", "commit"],
                "refactor": ["refactor", "clean", "optimize", "improve", "reorganize"],
                "plan": ["plan", "design", "architecture", "approach", "strategy"],
                "test": ["test", "verify", "check", "validate", "confirm"],
            }
            intent_scores = {}
            for intent_name, keywords in intent_map.items():
                intent_scores[intent_name] = sum(1 for kw in keywords if kw in combined)
            intent = max(intent_scores, key=intent_scores.get) if any(intent_scores.values()) else "general"

            # Energy level (message length + frequency)
            avg_len = sum(len(t) for t in texts) / len(texts)
            msg_rate = len(messages) / max(hours_back, 0.5)
            if avg_len > 200 or msg_rate > 5:
                energy = "high"
            elif avg_len < 50 and msg_rate < 2:
                energy = "low"
            else:
                energy = "medium"

            confidence = min(0.9, 0.3 + (len(messages) * 0.05))

            result = {
                "sentiment": sentiment,
                "intent": intent,
                "energy": energy,
                "confidence": confidence,
                "message_count": len(messages),
                "scores": scores,
            }

            # Store the analysis
            now = datetime.utcnow().isoformat()
            conn = connect_wal(self.db_path)
            try:
                t_sent = self._t("user_message_sentiment")
                for msg_id, content, ts in messages[:5]:
                    conn.execute(f"""
                        INSERT OR IGNORE INTO {t_sent}
                        (message_id, timestamp, sentiment, intent, energy, confidence, raw_snippet)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (msg_id, ts, sentiment, intent, energy, confidence,
                          (content or "")[:200]))
                conn.commit()
            finally:
                conn.close()

            return result

        except Exception as e:
            logger.warning(f"Sentiment analysis failed: {e}")
            return {"sentiment": "unknown", "intent": "unknown",
                    "energy": "unknown", "confidence": 0.0, "message_count": 0}

    def get_sentiment_summary(self, hours_back: int = 2) -> str:
        """One-liner sentiment summary for injection into Section 8."""
        result = self.analyze_user_sentiment(hours_back)
        if result["message_count"] == 0:
            return ""
        return (f"User mood: {result['sentiment']} | "
                f"intent: {result['intent']} | "
                f"energy: {result['energy']} "
                f"({result['message_count']} msgs, conf={result['confidence']:.1f})")

    def cleanup_old(self, days: int = 30):
        """Clean up old dialogue data to maintain efficient storage."""
        try:
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

            conn = connect_wal(self.db_path)
            try:
                t_msgs = self._t("dialogue_messages")
                t_threads = self._t("dialogue_threads")
                # Delete old messages
                conn.execute(
                    f"DELETE FROM {t_msgs} WHERE timestamp < ?",
                    (cutoff,)
                )
                # Delete empty threads
                conn.execute(f"""
                    DELETE FROM {t_threads}
                    WHERE session_id NOT IN (SELECT DISTINCT session_id FROM {t_msgs})
                """)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Error cleaning up old dialogue: {e}")


# Global instance for easy access
_mirror: Optional[DialogueMirror] = None


def get_dialogue_mirror() -> DialogueMirror:
    """Get the global dialogue mirror instance."""
    global _mirror
    if _mirror is None:
        _mirror = DialogueMirror()
    return _mirror


# Convenience functions for hook integration
def mirror_aaron_message(session_id: str, content: str, source: str = "vscode", project: str = None):
    """Mirror a message from Aaron, then detect outcomes for the evidence pipeline."""
    message = get_dialogue_mirror().mirror_message(
        session_id=session_id,
        role=MessageRole.AARON,
        content=content,
        source=DialogueSource(source) if source in DialogueSource.__members__.values() else DialogueSource.UNKNOWN,
        project=project,
    )

    # =====================================================================
    # OUTCOME DETECTION — Evidence Pipeline Wiring
    # =====================================================================
    # After mirroring, detect if this message signals success/failure
    # for the previous injection. Feeds hook_evolution A/B testing.
    # MUST be non-blocking — never fail the mirror because detection broke.
    try:
        _detect_and_record_outcome(content, session_id)
    except Exception:
        pass  # Non-blocking: outcome detection is observability, not critical path

    return message


def _detect_and_record_outcome(content: str, session_id: str):
    """
    Run outcome detection on a mirrored message and wire results into the evidence pipeline.

    Positive outcomes → resolve pending hook fires + capture success
    Negative outcomes → resolve pending hook fires + track pattern frequency
                        (only surfaces in SOPs after 3+ occurrences)
    """
    from memory.outcome_detector import detect_outcome

    result = detect_outcome(content)

    # Only act on high-confidence detections
    if result.confidence < 0.7:
        return

    # Resolve pending hook fires with the detected outcome
    try:
        from memory.hook_evolution import get_hook_evolution_engine
        engine = get_hook_evolution_engine()
        if engine:
            engine.resolve_pending_outcomes(result.outcome_type)
            logger.info(
                f"Outcome detected: {result.outcome_type} "
                f"(confidence={result.confidence:.2f}, signals={result.signals})"
            )
    except Exception as e:
        logger.debug(f"Hook evolution resolve failed: {e}")

    # POSITIVE → also capture as a success signal for SOPs (fire-and-forget)
    # capture_success makes HTTP calls to Context DNA — must not block the mirror
    if result.outcome_type == "positive":
        try:
            def _bg_capture():
                try:
                    from memory.auto_capture import capture_success
                    capture_success(
                        task="Outcome detected from user message",
                        details=f"Signals: {', '.join(result.signals)}",
                        area="outcome_detection"
                    )
                except Exception:
                    pass
            t = threading.Thread(target=_bg_capture, daemon=True)
            t.start()
        except Exception:
            pass  # Non-blocking

    # NEGATIVE → track pattern frequency (lightweight)
    # Aaron's rule: negatives only valuable after REPEATED patterns (3+)
    # So we count frequency here; sop_enhancer promotes after threshold
    elif result.outcome_type == "negative":
        try:
            _track_negative_pattern(content, result.signals, session_id)
        except Exception:
            pass  # Non-blocking


def _track_negative_pattern(content: str, signals: list, session_id: str):
    """
    Track negative outcome pattern frequency in dialogue mirror DB
    AND in the learnings DB (sqlite_storage) for SOP quality evolution.

    Only records the pattern observation — promotion to SOP happens
    in process_sop_enhancer after 3+ occurrences of the same pattern.
    """
    mirror = get_dialogue_mirror()
    now = datetime.utcnow().isoformat()
    snippet = (content or "")[:200]

    # --- Track in dialogue mirror DB (existing behavior) ---
    try:
        conn = connect_wal(mirror.db_path)
        try:
            t_negpat = mirror._t("negative_pattern_freq")
            for signal in signals:
                if signal == "low_confidence":
                    continue  # Skip meta-signals

                # Upsert: increment count or insert new
                existing = conn.execute(
                    f"SELECT occurrence_count, sample_messages FROM {t_negpat} WHERE pattern_key = ?",
                    (signal,)
                ).fetchone()

                if existing:
                    count = existing[0] + 1
                    # Keep last 3 sample messages (compact)
                    samples = existing[1] or ""
                    sample_list = [s for s in samples.split("|||") if s][-2:]
                    sample_list.append(snippet)
                    new_samples = "|||".join(sample_list)

                    conn.execute(f"""
                        UPDATE {t_negpat}
                        SET occurrence_count = ?, last_seen = ?, sample_messages = ?
                        WHERE pattern_key = ?
                    """, (count, now, new_samples, signal))

                    if count >= 3:
                        logger.info(
                            f"Negative pattern '{signal}' hit {count} occurrences — "
                            f"eligible for SOP promotion"
                        )
                else:
                    conn.execute(f"""
                        INSERT INTO {t_negpat}
                        (pattern_key, signal_name, occurrence_count, first_seen, last_seen, sample_messages)
                        VALUES (?, ?, 1, ?, ?, ?)
                    """, (signal, signal, now, now, snippet))

            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"Negative pattern tracking failed: {e}")

    # --- Also track in learnings DB (for SOP quality evolution) ---
    # This feeds the process_sop_enhancer's "what doesn't work" section
    try:
        from memory.sqlite_storage import get_sqlite_storage
        store = get_sqlite_storage()
        pattern_key = _normalize_negative_pattern(content, signals)
        if pattern_key:
            freq = store.record_negative_pattern(
                pattern_key=pattern_key,
                context=snippet,
                description=_describe_negative_pattern(content, signals),
            )
            if freq >= 3:
                logger.info(
                    f"Negative pattern '{pattern_key}' at {freq}x — "
                    f"now eligible for SOP inclusion"
                )
    except Exception as e:
        logger.debug(f"Learnings DB negative pattern tracking failed: {e}")


def _normalize_negative_pattern(content: str, signals: list) -> str:
    """
    Extract a normalized pattern key from negative outcome content.

    Tries to distill the semantic anti-pattern rather than just using
    the signal name. Falls back to most specific signal if extraction fails.

    Args:
        content: The user message content
        signals: The detected outcome signals

    Returns:
        Normalized pattern key string, or empty string if nothing useful
    """
    if not content:
        return ""

    content_lower = content.lower()

    # Try to extract the specific complaint (common patterns)
    complaint_patterns = [
        (r"(?:doesn't|doesn't|does not|didn't|did not)\s+(.{10,60})", None),
        (r"(?:still|keeps?)\s+(?:broken|failing|crashing|erroring)(?:\s+(.{5,40}))?", None),
        (r"(?:wrong|incorrect|bad)\s+(.{5,40})", None),
        (r"(?:can't|cannot|can not)\s+(.{5,40})", None),
        (r"(?:error|fail|crash|timeout)\s+(?:in|on|with|at)\s+(.{5,40})", None),
    ]

    for pattern, _ in complaint_patterns:
        match = re.search(pattern, content_lower)
        if match:
            extracted = match.group(1) if match.group(1) else match.group(0)
            # Normalize: strip, lowercase, collapse whitespace
            key = re.sub(r'\s+', ' ', extracted.strip())[:80]
            return key

    # Fall back to most specific signal (skip generic ones)
    generic_signals = {"low_confidence", "unknown", "neutral"}
    specific = [s for s in signals if s not in generic_signals]
    if specific:
        return specific[0]

    return ""


def _describe_negative_pattern(content: str, signals: list) -> str:
    """
    Generate a human-readable description of the negative pattern.

    This becomes the ✗ line in the SOP.

    Args:
        content: The user message content
        signals: The detected outcome signals

    Returns:
        Brief description suitable for SOP inclusion
    """
    if not content:
        return "Unknown negative pattern"

    # Take first meaningful sentence (up to 80 chars)
    # Strip common prefixes
    desc = content.strip()
    for prefix in ["that ", "it ", "this ", "the "]:
        if desc.lower().startswith(prefix):
            desc = desc[len(prefix):]
            break

    # Truncate at sentence boundary or 80 chars
    for end_char in ['.', '!', '\n']:
        idx = desc.find(end_char)
        if 0 < idx <= 80:
            desc = desc[:idx]
            break

    if len(desc) > 80:
        cut = desc[:80].rfind(' ')
        desc = desc[:cut] if cut > 20 else desc[:80]

    return desc.strip() or "Negative outcome observed"


def mirror_atlas_message(session_id: str, content: str, source: str = "vscode", project: str = None):
    """Mirror a message from Atlas."""
    return get_dialogue_mirror().mirror_message(
        session_id=session_id,
        role=MessageRole.ATLAS,
        content=content,
        source=DialogueSource(source) if source in DialogueSource.__members__.values() else DialogueSource.UNKNOWN,
        project=project,
    )


def mirror_synaptic_message(session_id: str, content: str, source: str = "webhook"):
    """Mirror a message from Synaptic."""
    return get_dialogue_mirror().mirror_message(
        session_id=session_id,
        role=MessageRole.SYNAPTIC,
        content=content,
        source=DialogueSource(source) if source in DialogueSource.__members__.values() else DialogueSource.UNKNOWN,
    )


# =============================================================================
# LLM-POWERED FAILURE DETECTION
# =============================================================================

def detect_failures_with_llm(
    session_id: str = None,
    hours_back: int = 4,
    use_llm: bool = True
) -> Dict[str, Any]:
    """
    Analyze recent dialogue for failure patterns using local LLM.

    This goes beyond keyword matching to understand:
    - User frustration signals (tone, repetition)
    - Implicit failures (user silently abandons approach)
    - Retry patterns (similar requests with variations)
    - Success reversals (initially positive → later negative)

    Args:
        session_id: Specific session or None for all recent
        hours_back: How far back to look
        use_llm: Whether to use LLM analysis (True) or just rules (False)

    Returns:
        Dict with detected failures, confidence, and recommendations
    """
    mirror = get_dialogue_mirror()
    context = mirror.get_context_for_synaptic(
        session_id=session_id,
        max_messages=100,
        max_age_hours=hours_back
    )

    messages = context.get("dialogue_context", [])
    if not messages:
        return {"failures": [], "analysis": "No dialogue found", "confidence": 0.0}

    # Stage 1: Rule-based detection (fast, always runs)
    rule_failures = _detect_failures_by_rules(messages)

    # Stage 2: LLM analysis (deep semantic understanding)
    if use_llm and len(messages) >= 3:
        llm_analysis = _analyze_failures_with_llm(messages)
    else:
        llm_analysis = {"failures": [], "insights": [], "confidence": 0.0}

    # Merge results
    all_failures = rule_failures + llm_analysis.get("failures", [])

    # Deduplicate and rank by confidence
    unique_failures = _deduplicate_failures(all_failures)

    return {
        "failures": unique_failures,
        "rule_detected": len(rule_failures),
        "llm_detected": len(llm_analysis.get("failures", [])),
        "insights": llm_analysis.get("insights", []),
        "confidence": max(
            max((f.get("confidence", 0) for f in unique_failures), default=0),
            llm_analysis.get("confidence", 0)
        ),
        "messages_analyzed": len(messages),
    }


def _detect_failures_by_rules(messages: List[Dict]) -> List[Dict]:
    """Rule-based failure detection (fast, keyword-based)."""
    failures = []

    # Frustration indicators
    frustration_patterns = [
        r"\bstill\s+(not|broken|failing|wrong)\b",
        r"\bagain\?[!]?\b",
        r"\bwhy\s+(is|did|does|won't)\b",
        r"\bugh\b",
        r"\bthat\s+didn'?t\s+work\b",
        r"\bsame\s+(error|issue|problem)\b",
    ]

    # Retry indicators
    retry_patterns = [
        r"\btry\s+again\b",
        r"\blet'?s\s+try\b",
        r"\bcan\s+you\s+(fix|try|redo)\b",
        r"\bactually[,:]?\s+(no|wait)\b",
    ]

    import re

    for i, msg in enumerate(messages):
        if msg.get("role") != "aaron":
            continue

        content = msg.get("content", "").lower()

        # Check frustration
        for pattern in frustration_patterns:
            if re.search(pattern, content):
                failures.append({
                    "type": "frustration",
                    "message_index": i,
                    "content_snippet": content[:100],
                    "pattern": pattern,
                    "confidence": 0.7,
                    "source": "rules"
                })
                break

        # Check retry
        for pattern in retry_patterns:
            if re.search(pattern, content):
                failures.append({
                    "type": "retry_request",
                    "message_index": i,
                    "content_snippet": content[:100],
                    "pattern": pattern,
                    "confidence": 0.75,
                    "source": "rules"
                })
                break

    return failures


def _analyze_failures_with_llm(messages: List[Dict]) -> Dict[str, Any]:
    """Use local LLM to detect subtle failure patterns."""
    try:
        import requests

        # Build conversation summary for LLM
        conversation = "\n".join([
            f"[{m.get('role', 'unknown').upper()}]: {m.get('content', '')[:200]}"
            for m in messages[-20:]  # Last 20 messages
        ])

        prompt = f"""Analyze this conversation for failure patterns. Look for:
1. User frustration (explicit or implicit)
2. Repeated similar requests (retry patterns)
3. Task abandonment signals
4. Success that turned into failure later

Conversation:
{conversation}

Return JSON with:
- "failures": list of {{"type": "...", "description": "...", "confidence": 0.0-1.0}}
- "insights": list of brief observations
- "overall_sentiment": "positive" | "negative" | "neutral"
- "confidence": overall confidence 0.0-1.0

JSON only, no markdown:"""

        from memory.llm_priority_queue import butler_query
        content = butler_query(
            "You analyze conversations for failure patterns. Return JSON only.",
            prompt,
            profile="extract"
        )

        if content:
            import json
            try:
                # Clean potential markdown
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]

                analysis = json.loads(content.strip())

                # Add source marker to each failure
                for f in analysis.get("failures", []):
                    f["source"] = "llm"

                return analysis
            except json.JSONDecodeError:
                logger.warning(f"LLM returned non-JSON: {content[:100]}")
                return {"failures": [], "insights": [], "confidence": 0.0}
        else:
            return {"failures": [], "insights": ["LLM unavailable"], "confidence": 0.0}

    except Exception as e:
        logger.warning(f"LLM analysis failed: {e}")
        return {"failures": [], "insights": [], "confidence": 0.0}


def _deduplicate_failures(failures: List[Dict]) -> List[Dict]:
    """Deduplicate failures, preferring higher confidence."""
    seen = {}
    for f in failures:
        key = f"{f.get('type', '')}_{f.get('message_index', 0)}"
        if key not in seen or f.get("confidence", 0) > seen[key].get("confidence", 0):
            seen[key] = f

    # Sort by confidence descending
    return sorted(seen.values(), key=lambda x: x.get("confidence", 0), reverse=True)


def get_recent_topic_context(hours_back: int = 2, max_topics: int = 5) -> List[str]:
    """
    Extract recent conversation topics for pre-payload context.

    Used by webhook injection to understand what user has been working on,
    enabling better context for meta-questions like "what should I do next?"

    Returns list of topic strings (most recent first).
    """
    mirror = get_dialogue_mirror()
    context = mirror.get_context_for_synaptic(
        max_messages=50,
        max_age_hours=hours_back
    )

    messages = context.get("dialogue_context", [])
    if not messages:
        return []

    # Extract topics from Aaron's messages
    topics = []
    for msg in messages:
        if msg.get("role") != "aaron":
            continue

        content = msg.get("content", "")
        if len(content) < 10:
            continue

        # Simple topic extraction: first sentence or first 100 chars
        first_sentence = content.split(".")[0].strip()
        if len(first_sentence) > 100:
            first_sentence = first_sentence[:100] + "..."

        if first_sentence and first_sentence not in topics:
            topics.append(first_sentence)
            if len(topics) >= max_topics:
                break

    return topics


# =============================================================================
# LEARNING SYSTEM FEEDBACK LOOP
# =============================================================================

def process_failures_and_learn(
    session_id: str = None,
    hours_back: int = 4,
    use_llm: bool = True
) -> Dict[str, Any]:
    """
    Detect failures in recent dialogue and feed them into the learning system.

    This closes the human feedback loop:
    - Frustration signals → recorded as 'gotcha' learnings
    - Retry patterns → trigger SOP review
    - Implicit failures → flag for UX refinement

    Called periodically or after significant dialogue activity.

    Args:
        session_id: Specific session or None for all recent
        hours_back: How far back to analyze
        use_llm: Whether to use LLM for deep analysis

    Returns:
        Summary of detected and recorded failures
    """
    # Detect failures
    detection_result = detect_failures_with_llm(
        session_id=session_id,
        hours_back=hours_back,
        use_llm=use_llm
    )

    failures = detection_result.get("failures", [])
    if not failures:
        return {
            "processed": 0,
            "recorded": 0,
            "message": "No failures detected"
        }

    # Import auto_capture for learning system integration
    try:
        from memory.auto_capture import capture_failure_signal
        AUTO_CAPTURE_AVAILABLE = True
    except ImportError:
        AUTO_CAPTURE_AVAILABLE = False
        logger.warning("auto_capture not available - failures detected but not recorded")

    recorded = 0
    for failure in failures:
        if not AUTO_CAPTURE_AVAILABLE:
            continue

        try:
            failure_type = failure.get("type", "unknown")
            description = failure.get("description") or failure.get("content_snippet", "")
            confidence = failure.get("confidence", 0.7)
            source = failure.get("source", "rules")

            learning_id = capture_failure_signal(
                failure_type=failure_type,
                description=description,
                context=f"Source: {source}, Pattern: {failure.get('pattern', 'N/A')}",
                confidence=confidence,
                session_id=session_id
            )

            if learning_id:
                recorded += 1
                logger.info(f"Recorded failure signal: {failure_type} (confidence: {confidence})")

        except Exception as e:
            logger.error(f"Error recording failure: {e}")
            continue

    return {
        "processed": len(failures),
        "recorded": recorded,
        "insights": detection_result.get("insights", []),
        "confidence": detection_result.get("confidence", 0),
        "message": f"Detected {len(failures)} failures, recorded {recorded} to learning system"
    }


def schedule_failure_analysis(interval_minutes: int = 30):
    """
    Schedule periodic failure analysis.

    This runs in the background to continuously improve the system
    based on detected frustration patterns.

    Args:
        interval_minutes: How often to run analysis
    """
    import threading
    import time

    def _analysis_loop():
        while True:
            try:
                result = process_failures_and_learn(hours_back=2, use_llm=True)
                if result.get("recorded", 0) > 0:
                    logger.info(f"Failure analysis: {result['message']}")
            except Exception as e:
                logger.error(f"Failure analysis error: {e}")

            time.sleep(interval_minutes * 60)

    thread = threading.Thread(target=_analysis_loop, daemon=True)
    thread.start()
    logger.info(f"Scheduled failure analysis every {interval_minutes} minutes")


if __name__ == "__main__":
    # Demo
    mirror = get_dialogue_mirror()

    # Simulate a conversation
    session = "demo-session-001"

    mirror.mirror_message(session, MessageRole.AARON, "Hello Synaptic!", DialogueSource.VSCODE)
    mirror.mirror_message(session, MessageRole.ATLAS, "Let me ask Synaptic for you...", DialogueSource.VSCODE)
    mirror.mirror_message(session, MessageRole.SYNAPTIC, "Hello Aaron! I am Synaptic, the 8th Intelligence.", DialogueSource.WEBAPP)

    # Get context for Synaptic
    context = mirror.get_context_for_synaptic(session)
    print(json.dumps(context, indent=2))
