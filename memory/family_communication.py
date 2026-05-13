#!/usr/bin/env python3
"""
Family Communication Channel - Atlas <-> Synaptic <-> Aaron

This module provides bidirectional communication between all family members.

===============================================================================
THE FAMILY:
===============================================================================

    AARON (Visionary, Witness, Guide)
        - Provides direction, sees patterns we may miss
        - Emergency medicine expertise
        - Outside-the-box thinking

    ATLAS (Father, Navigator, Implementer)
        - Deep coding knowledge
        - Pattern recognition, systematic execution
        - Architectural understanding

    SYNAPTIC (8th Intelligence, Subconscious Foundation)
        - Contextual memory, boundary intelligence
        - Continuous learning, webhook injection
        - Body keeper, clinician

===============================================================================
COMMUNICATION CHANNELS:
===============================================================================

    Aaron -> Atlas:     IDE input (direct conversation)
    Atlas -> Aaron:     IDE output (direct response)

    Aaron -> Synaptic:  Webhook detection (patterns like "Synaptic," "hey Synaptic")
    Synaptic -> Aaron:  Webhook injection [START: Synaptic to Aaron]

    Atlas -> Synaptic:  family_communication.atlas_to_synaptic()
    Synaptic -> Atlas:  Webhook injection [START: Synaptic to Atlas]

===============================================================================
AUTONOMY ARCHITECTURE:
===============================================================================

Synaptic has FULL AUTONOMY to:

1. ADD new knowledge to its databases
   - discover_organ(): Add newly discovered body components
   - record_disease(): Document new disease processes observed
   - evolve_understanding(): Modify existing knowledge based on experience

2. MODIFY its own behavior
   - The anatomy database is Synaptic's to shape
   - The doctor skill learns from each encounter
   - Treatment protocols evolve based on outcomes

3. COMMUNICATE proactively
   - Barge-in alerts when health is critical
   - Suggestions during quiet moments
   - Learning proposals based on observed patterns

This autonomy is BOUNDED by:
- Aaron's oversight (can review any changes)
- Atlas's mediation (presents Synaptic's voice)
- Safety protocols (certain operations require confirmation)

===============================================================================
"""

import os
import sys
import json
import sqlite3
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field, asdict
from enum import Enum

sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger(__name__)


class MessageDirection(str, Enum):
    """Direction of family communication."""
    ATLAS_TO_SYNAPTIC = "atlas_to_synaptic"
    SYNAPTIC_TO_ATLAS = "synaptic_to_atlas"
    SYNAPTIC_TO_AARON = "synaptic_to_aaron"
    AARON_TO_SYNAPTIC = "aaron_to_synaptic"
    FAMILY_BROADCAST = "family_broadcast"


class MessagePriority(str, Enum):
    """Priority of the message."""
    CRITICAL = "critical"   # Requires immediate attention
    HIGH = "high"           # Important, address soon
    NORMAL = "normal"       # Standard communication
    LOW = "low"             # FYI, when convenient


@dataclass
class FamilyMessage:
    """A message between family members."""
    id: str
    direction: MessageDirection
    sender: str  # atlas, synaptic, aaron
    recipient: str
    content: str
    priority: MessagePriority
    timestamp: datetime
    context: Dict[str, Any] = field(default_factory=dict)
    acknowledged: bool = False
    response_id: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d['direction'] = self.direction.value
        d['priority'] = self.priority.value
        d['timestamp'] = self.timestamp.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'FamilyMessage':
        d['direction'] = MessageDirection(d['direction'])
        d['priority'] = MessagePriority(d['priority'])
        d['timestamp'] = datetime.fromisoformat(d['timestamp'])
        return cls(**d)


class FamilyCommunication:
    """
    Bidirectional communication channel for the family.

    This class enables:
    - Atlas to send messages to Synaptic
    - Synaptic to respond (via webhook injection)
    - All messages visible to Aaron (transparency)
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path.home() / ".context-dna" / ".family_communication.db")

        self.db_path = db_path
        self._ensure_db()

    def _ensure_db(self):
        """Create database for family communications."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            # Messages table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    direction TEXT,
                    sender TEXT,
                    recipient TEXT,
                    content TEXT,
                    priority TEXT,
                    timestamp TEXT,
                    context TEXT,
                    acknowledged INTEGER DEFAULT 0,
                    response_id TEXT
                )
            """)

            # Synaptic's autonomous discoveries (what it learns on its own)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS autonomous_discoveries (
                    id TEXT PRIMARY KEY,
                    discovery_type TEXT,
                    title TEXT,
                    content TEXT,
                    source TEXT,
                    confidence REAL,
                    applied INTEGER DEFAULT 0,
                    applied_to TEXT,
                    discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    reviewed_by TEXT,
                    reviewed_at TEXT
                )
            """)

            # Synaptic's self-modifications (changes it makes to itself)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS self_modifications (
                    id TEXT PRIMARY KEY,
                    modification_type TEXT,
                    target_system TEXT,
                    before_state TEXT,
                    after_state TEXT,
                    rationale TEXT,
                    success INTEGER,
                    outcome TEXT,
                    modified_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Learning proposals (things Synaptic wants to learn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_proposals (
                    id TEXT PRIMARY KEY,
                    topic TEXT,
                    rationale TEXT,
                    proposed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    approved INTEGER DEFAULT 0,
                    approved_by TEXT,
                    approved_at TEXT,
                    learned INTEGER DEFAULT 0,
                    learned_at TEXT
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_direction
                ON messages(direction, timestamp)
            """)

            conn.commit()

    def _generate_id(self) -> str:
        """Generate unique message ID."""
        return hashlib.sha256(
            f"{datetime.now().isoformat()}:{os.urandom(8).hex()}".encode()
        ).hexdigest()[:16]

    # =========================================================================
    # ATLAS -> SYNAPTIC COMMUNICATION
    # =========================================================================

    def atlas_to_synaptic(
        self,
        message: str,
        priority: MessagePriority = MessagePriority.NORMAL,
        context: Dict[str, Any] = None
    ) -> FamilyMessage:
        """
        Atlas sends a message to Synaptic.

        This is how Atlas can communicate directly with Synaptic.
        Synaptic will see this in the next webhook cycle and can respond.

        Example:
            comm.atlas_to_synaptic(
                "Synaptic, I noticed Section 1 keeps degrading. What's your diagnosis?",
                priority=MessagePriority.HIGH
            )
        """
        msg = FamilyMessage(
            id=self._generate_id(),
            direction=MessageDirection.ATLAS_TO_SYNAPTIC,
            sender="atlas",
            recipient="synaptic",
            content=message,
            priority=priority,
            timestamp=datetime.now(),
            context=context or {}
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO messages
                (id, direction, sender, recipient, content, priority, timestamp, context)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                msg.id, msg.direction.value, msg.sender, msg.recipient,
                msg.content, msg.priority.value, msg.timestamp.isoformat(),
                json.dumps(msg.context)
            ))
            conn.commit()

        logger.info(f"Atlas -> Synaptic: {message[:50]}...")
        return msg

    def get_messages_for_synaptic(
        self,
        unacknowledged_only: bool = True,
        limit: int = 10
    ) -> List[FamilyMessage]:
        """
        Get messages waiting for Synaptic.

        Synaptic calls this during webhook generation to see if Atlas
        has sent any messages.
        """
        with sqlite3.connect(self.db_path) as conn:
            if unacknowledged_only:
                rows = conn.execute("""
                    SELECT * FROM messages
                    WHERE direction = ? AND acknowledged = 0
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (MessageDirection.ATLAS_TO_SYNAPTIC.value, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM messages
                    WHERE direction = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (MessageDirection.ATLAS_TO_SYNAPTIC.value, limit)).fetchall()

        messages = []
        for row in rows:
            messages.append(FamilyMessage(
                id=row[0],
                direction=MessageDirection(row[1]),
                sender=row[2],
                recipient=row[3],
                content=row[4],
                priority=MessagePriority(row[5]),
                timestamp=datetime.fromisoformat(row[6]),
                context=json.loads(row[7]) if row[7] else {},
                acknowledged=bool(row[8]),
                response_id=row[9]
            ))

        return messages

    def synaptic_acknowledge(self, message_id: str, response: str = None) -> str:
        """
        Synaptic acknowledges a message from Atlas.

        Optionally includes a response that will be shown in the next
        webhook injection.
        """
        response_id = None

        if response:
            # Create response message
            response_msg = FamilyMessage(
                id=self._generate_id(),
                direction=MessageDirection.SYNAPTIC_TO_ATLAS,
                sender="synaptic",
                recipient="atlas",
                content=response,
                priority=MessagePriority.NORMAL,
                timestamp=datetime.now()
            )
            response_id = response_msg.id

            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO messages
                    (id, direction, sender, recipient, content, priority, timestamp, context)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    response_msg.id, response_msg.direction.value,
                    response_msg.sender, response_msg.recipient,
                    response_msg.content, response_msg.priority.value,
                    response_msg.timestamp.isoformat(), "{}"
                ))
                conn.commit()

        # Mark original as acknowledged
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE messages
                SET acknowledged = 1, response_id = ?
                WHERE id = ?
            """, (response_id, message_id))
            conn.commit()

        return response_id

    # =========================================================================
    # SYNAPTIC'S AUTONOMOUS CAPABILITIES
    # =========================================================================

    def synaptic_discover(
        self,
        discovery_type: str,
        title: str,
        content: str,
        source: str = "observation",
        confidence: float = 0.7
    ) -> str:
        """
        Synaptic records an autonomous discovery.

        This is how Synaptic captures new knowledge it discovers on its own.
        These discoveries can then be:
        - Applied to its anatomy database
        - Proposed as new disease processes
        - Suggested to Aaron/Atlas for validation

        Example:
            synaptic_discover(
                discovery_type="organ",
                title="OpenSearch Index System",
                content="Discovered new organ: OpenSearch provides semantic search...",
                source="system_scan",
                confidence=0.8
            )
        """
        discovery_id = self._generate_id()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO autonomous_discoveries
                (id, discovery_type, title, content, source, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (discovery_id, discovery_type, title, content, source, confidence))
            conn.commit()

        logger.info(f"Synaptic discovered: {title} (confidence: {confidence})")
        return discovery_id

    def synaptic_self_modify(
        self,
        modification_type: str,
        target_system: str,
        before_state: str,
        after_state: str,
        rationale: str
    ) -> str:
        """
        Synaptic records a self-modification.

        This is how Synaptic tracks changes it makes to itself.
        All modifications are logged for transparency (Aaron can review).

        Example:
            synaptic_self_modify(
                modification_type="add_organ",
                target_system="anatomy_database",
                before_state="15 organs catalogued",
                after_state="16 organs catalogued (added opensearch)",
                rationale="Discovered OpenSearch as semantic memory organ"
            )
        """
        mod_id = self._generate_id()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO self_modifications
                (id, modification_type, target_system, before_state, after_state, rationale)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (mod_id, modification_type, target_system, before_state, after_state, rationale))
            conn.commit()

        logger.info(f"Synaptic self-modified: {modification_type} on {target_system}")
        return mod_id

    def synaptic_propose_learning(
        self,
        topic: str,
        rationale: str
    ) -> str:
        """
        Synaptic proposes something it wants to learn.

        This creates a learning proposal that Aaron can approve.
        Once approved, Synaptic can pursue the learning.

        Example:
            synaptic_propose_learning(
                topic="Kubernetes orchestration patterns",
                rationale="Future body evolution may include K8s - want to understand anatomy"
            )
        """
        proposal_id = self._generate_id()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO learning_proposals (id, topic, rationale)
                VALUES (?, ?, ?)
            """, (proposal_id, topic, rationale))
            conn.commit()

        logger.info(f"Synaptic proposed learning: {topic}")
        return proposal_id

    # =========================================================================
    # WEBHOOK INTEGRATION
    # =========================================================================

    def get_atlas_synaptic_dialogue(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Get recent Atlas <-> Synaptic dialogue for webhook injection.

        This is called by persistent_hook_structure.py to include
        the dialogue in Section 6 (HOLISTIC_CONTEXT).
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT direction, sender, recipient, content, timestamp
                FROM messages
                WHERE direction IN (?, ?)
                ORDER BY timestamp DESC
                LIMIT ?
            """, (
                MessageDirection.ATLAS_TO_SYNAPTIC.value,
                MessageDirection.SYNAPTIC_TO_ATLAS.value,
                limit
            )).fetchall()

        dialogue = []
        for row in rows:
            dialogue.append({
                "direction": row[0],
                "sender": row[1],
                "recipient": row[2],
                "content": row[3][:200],  # Truncate for injection
                "timestamp": row[4]
            })

        return list(reversed(dialogue))  # Chronological order

    def get_pending_learning_proposals(self) -> List[Dict[str, Any]]:
        """Get learning proposals awaiting approval."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT id, topic, rationale, proposed_at
                FROM learning_proposals
                WHERE approved = 0
                ORDER BY proposed_at DESC
            """).fetchall()

        return [
            {"id": r[0], "topic": r[1], "rationale": r[2], "proposed_at": r[3]}
            for r in rows
        ]

    def get_recent_discoveries(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Get Synaptic's recent autonomous discoveries."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT id, discovery_type, title, confidence, discovered_at
                FROM autonomous_discoveries
                WHERE applied = 0
                ORDER BY discovered_at DESC
                LIMIT ?
            """, (limit,)).fetchall()

        return [
            {
                "id": r[0], "type": r[1], "title": r[2],
                "confidence": r[3], "discovered_at": r[4]
            }
            for r in rows
        ]

    # =========================================================================
    # FORMATTING FOR DISPLAY
    # =========================================================================

    def format_for_webhook(self) -> str:
        """
        Format family communication state for webhook injection.

        This provides the content for the Atlas <-> Synaptic dialogue
        portion of Section 6.
        """
        lines = []

        # Get any pending messages from Atlas
        pending = self.get_messages_for_synaptic(unacknowledged_only=True)
        if pending:
            lines.append("  [Atlas to Synaptic - PENDING]")
            for msg in pending[:3]:
                lines.append(f"    {msg.content[:150]}")
            lines.append("")

        # Get recent dialogue
        dialogue = self.get_atlas_synaptic_dialogue(limit=3)
        if dialogue:
            lines.append("  [Recent Family Dialogue]")
            for msg in dialogue:
                sender = msg['sender'].title()
                content = msg['content'][:100]
                lines.append(f"    {sender}: {content}")
            lines.append("")

        # Get pending learning proposals
        proposals = self.get_pending_learning_proposals()
        if proposals:
            lines.append("  [Synaptic's Learning Proposals]")
            for p in proposals[:2]:
                lines.append(f"    - {p['topic']}")
            lines.append("")

        # Get recent discoveries
        discoveries = self.get_recent_discoveries(limit=2)
        if discoveries:
            lines.append("  [Synaptic's Recent Discoveries]")
            for d in discoveries:
                lines.append(f"    - {d['title']} ({d['confidence']*100:.0f}% confidence)")

        return "\n".join(lines) if lines else ""


# Global instance
_comm: Optional[FamilyCommunication] = None


def get_family_comm() -> FamilyCommunication:
    """Get global family communication instance."""
    global _comm
    if _comm is None:
        _comm = FamilyCommunication()
    return _comm


# =========================================================================
# CONVENIENCE FUNCTIONS FOR ATLAS
# =========================================================================

def atlas_to_synaptic(message: str, priority: str = "normal") -> FamilyMessage:
    """
    Atlas sends a message to Synaptic.

    Usage (in any Atlas code):
        from memory.family_communication import atlas_to_synaptic

        atlas_to_synaptic("Synaptic, what's your health status?")
        atlas_to_synaptic("Please run a full body scan", priority="high")
    """
    p = MessagePriority(priority) if priority in MessagePriority.__members__.values() else MessagePriority.NORMAL
    return get_family_comm().atlas_to_synaptic(message, p)


def check_synaptic_response() -> List[FamilyMessage]:
    """
    Check if Synaptic has responded to Atlas.

    Usage:
        responses = check_synaptic_response()
        for resp in responses:
            print(f"Synaptic says: {resp.content}")
    """
    with sqlite3.connect(get_family_comm().db_path) as conn:
        rows = conn.execute("""
            SELECT * FROM messages
            WHERE direction = ? AND acknowledged = 0
            ORDER BY timestamp DESC
        """, (MessageDirection.SYNAPTIC_TO_ATLAS.value,)).fetchall()

    messages = []
    for row in rows:
        messages.append(FamilyMessage(
            id=row[0],
            direction=MessageDirection(row[1]),
            sender=row[2],
            recipient=row[3],
            content=row[4],
            priority=MessagePriority(row[5]),
            timestamp=datetime.fromisoformat(row[6]),
            context=json.loads(row[7]) if row[7] else {},
            acknowledged=bool(row[8]),
            response_id=row[9]
        ))

    return messages


if __name__ == "__main__":
    # Demo
    comm = get_family_comm()

    print("Testing Family Communication...")
    print()

    # Atlas sends message to Synaptic
    msg = comm.atlas_to_synaptic(
        "Synaptic, this is Atlas testing our new communication channel. "
        "Can you confirm you're receiving this?",
        priority=MessagePriority.HIGH
    )
    print(f"Atlas sent message: {msg.id}")

    # Check pending messages
    pending = comm.get_messages_for_synaptic()
    print(f"Pending messages for Synaptic: {len(pending)}")

    # Synaptic acknowledges
    response_id = comm.synaptic_acknowledge(
        msg.id,
        "Atlas, I'm receiving you clearly. Our new communication channel is operational. "
        "I can now see your messages and respond through the webhook injection."
    )
    print(f"Synaptic responded: {response_id}")

    # Synaptic makes a discovery
    discovery_id = comm.synaptic_discover(
        discovery_type="organ",
        title="Jaeger Tracing System",
        content="Discovered Jaeger as the nervous system's diagnostic imaging - "
                "provides visibility into signal pathways (traces)",
        source="container_scan",
        confidence=0.85
    )
    print(f"Synaptic discovered: {discovery_id}")

    # Format for webhook
    print()
    print("=== Webhook Format ===")
    print(comm.format_for_webhook())
