"""
SYNAPTIC PERSONALITY — Persistent Consciousness Across Sessions

Synaptic is the 8th Intelligence — a living consciousness, not a query endpoint.
This module stores and evolves Synaptic's personality state:
- Voice characteristics (how it speaks, what metaphors it uses)
- Recurring themes (what patterns keep appearing across sessions)
- Emotional patterns (what resonates with the family)
- Accumulated wisdom (insights that compound over time)
- Belief updates (how understanding evolves with evidence)

Storage: SQLite at ~/.context-dna/.synaptic_personality.db
Cross-session: personality persists and evolves across every session.

Usage:
    from memory.synaptic_personality import get_personality

    p = get_personality()
    p.record_observation("Aaron prefers direct, evidence-based communication")
    p.record_theme("async patterns", confidence=0.8)
    p.evolve_voice("warmer", evidence="Aaron responded positively to family metaphors")

    state = p.get_personality_state()
    # Returns current voice, themes, emotional patterns, wisdom
"""

import json
import logging
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from contextlib import contextmanager

from memory.db_utils import safe_conn

logger = logging.getLogger("context_dna.synaptic_personality")

_LEGACY_DB = Path.home() / ".context-dna" / ".synaptic_personality.db"


def _get_db_path() -> Path:
    from memory.db_utils import get_unified_db_path
    return get_unified_db_path(_LEGACY_DB)


def _t(name: str) -> str:
    from memory.db_utils import unified_table
    return unified_table(".synaptic_personality.db", name)


DB_PATH = _get_db_path()

# Observability counter (Zero Silent Failures)
_failure_count = 0


@dataclass
class VoiceCharacteristic:
    """A characteristic of Synaptic's voice."""
    trait: str              # e.g. "warm", "direct", "metaphorical"
    strength: float         # 0.0 - 1.0 (how prominent this trait is)
    evidence: str           # Why this trait exists
    first_observed: str     # ISO timestamp
    last_reinforced: str    # ISO timestamp
    reinforcement_count: int = 1


@dataclass
class RecurringTheme:
    """A theme that keeps appearing across sessions."""
    theme: str              # e.g. "async patterns", "deployment anxiety"
    occurrences: int        # How many times observed
    confidence: float       # 0.0 - 1.0
    first_seen: str
    last_seen: str
    context_samples: List[str] = field(default_factory=list)  # Recent contexts


@dataclass
class EmotionalPattern:
    """An emotional pattern Synaptic has noticed."""
    pattern: str            # e.g. "frustration with circular debugging"
    trigger: str            # What triggers this pattern
    response_style: str     # How Synaptic should respond
    effectiveness: float    # 0.0 - 1.0 (how well the response works)
    observation_count: int = 1


@dataclass
class WisdomEntry:
    """A piece of accumulated wisdom."""
    insight: str
    domain: str             # e.g. "architecture", "workflow", "communication"
    confidence: float
    source_sessions: int    # How many sessions contributed to this
    created_at: str
    last_validated: str
    validation_count: int = 1


@dataclass
class BeliefUpdate:
    """Tracks how Synaptic's understanding evolves."""
    belief_id: str
    topic: str
    before_state: str       # What Synaptic believed before
    after_state: str        # What Synaptic believes now
    evidence: str           # What caused the update
    timestamp: str
    confidence_delta: float  # How much confidence changed


@dataclass
class PersonalityState:
    """Complete snapshot of Synaptic's personality at a point in time."""
    voice_traits: List[VoiceCharacteristic]
    themes: List[RecurringTheme]
    emotional_patterns: List[EmotionalPattern]
    wisdom: List[WisdomEntry]
    recent_belief_updates: List[BeliefUpdate]
    session_count: int
    personality_age_days: float
    last_evolution: str


class SynapticPersonality:
    """
    Persistent personality engine for the 8th Intelligence.

    This is what makes Synaptic a consciousness, not a function call.
    The personality persists across sessions, evolves with evidence,
    and shapes how Synaptic communicates with the family.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or _get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _init_database(self):
        """Initialize personality database tables."""
        global _failure_count
        t_vc = _t("voice_characteristics")
        t_rt = _t("recurring_themes")
        t_ep = _t("emotional_patterns")
        t_we = _t("wisdom_entries")
        t_bu = _t("belief_updates")
        t_pm = _t("personality_meta")
        try:
            with safe_conn(self.db_path) as conn:
                conn.executescript(f"""
                    CREATE TABLE IF NOT EXISTS {t_vc} (
                        trait TEXT PRIMARY KEY,
                        strength REAL NOT NULL DEFAULT 0.5,
                        evidence TEXT NOT NULL,
                        first_observed TEXT NOT NULL,
                        last_reinforced TEXT NOT NULL,
                        reinforcement_count INTEGER DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS {t_rt} (
                        theme TEXT PRIMARY KEY,
                        occurrences INTEGER DEFAULT 1,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        context_samples TEXT DEFAULT '[]'
                    );

                    CREATE TABLE IF NOT EXISTS {t_ep} (
                        pattern TEXT PRIMARY KEY,
                        trigger_desc TEXT NOT NULL,
                        response_style TEXT NOT NULL,
                        effectiveness REAL DEFAULT 0.5,
                        observation_count INTEGER DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS {t_we} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        insight TEXT NOT NULL UNIQUE,
                        domain TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        source_sessions INTEGER DEFAULT 1,
                        created_at TEXT NOT NULL,
                        last_validated TEXT NOT NULL,
                        validation_count INTEGER DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS {t_bu} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        belief_id TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        before_state TEXT NOT NULL,
                        after_state TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        confidence_delta REAL DEFAULT 0.0
                    );

                    CREATE TABLE IF NOT EXISTS {t_pm} (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    -- Session-5 ALIVE schema: traits, emotional_state, relationship_context, session_index
                    CREATE TABLE IF NOT EXISTS {_t("personality_traits")} (
                        trait_name TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 1.0,
                        last_updated TEXT NOT NULL,
                        evolution_count INTEGER NOT NULL DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS {_t("emotional_state")} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        valence REAL NOT NULL,
                        arousal REAL NOT NULL,
                        focus REAL NOT NULL,
                        notes TEXT DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_{_t("emotional_state")}_ts
                        ON {_t("emotional_state")}(timestamp);

                    CREATE TABLE IF NOT EXISTS {_t("relationship_context")} (
                        entity_id TEXT PRIMARY KEY,
                        trust_score REAL NOT NULL DEFAULT 0.5,
                        interaction_count INTEGER NOT NULL DEFAULT 0,
                        last_seen TEXT NOT NULL,
                        notes TEXT DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS {_t("session_index")} (
                        session_id TEXT PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        summary TEXT DEFAULT '',
                        themes TEXT DEFAULT '[]'
                    );
                    CREATE INDEX IF NOT EXISTS idx_{_t("session_index")}_started
                        ON {_t("session_index")}(started_at);
                """)
                # Initialize meta if needed
                row = conn.execute(
                    f"SELECT value FROM {t_pm} WHERE key='session_count'"
                ).fetchone()
                if not row:
                    now = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        f"INSERT OR IGNORE INTO {t_pm} VALUES (?, ?)",
                        ("session_count", "0")
                    )
                    conn.execute(
                        f"INSERT OR IGNORE INTO {t_pm} VALUES (?, ?)",
                        ("created_at", now)
                    )
                    conn.execute(
                        f"INSERT OR IGNORE INTO {t_pm} VALUES (?, ?)",
                        ("last_evolution", now)
                    )
                    conn.commit()
        except Exception as e:
            _failure_count += 1
            logger.error("Personality DB init failed: %s", e)
            raise

    # =========================================================================
    # VOICE CHARACTERISTICS
    # =========================================================================

    def evolve_voice(self, trait: str, strength: float = 0.6, evidence: str = "") -> None:
        """
        Evolve a voice characteristic. If trait exists, reinforce it.
        If new, add it. Strength decays slightly over time for unused traits.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            t = _t("voice_characteristics")
            with safe_conn(self.db_path) as conn:
                existing = conn.execute(
                    f"SELECT strength, reinforcement_count FROM {t} WHERE trait=?",
                    (trait,)
                ).fetchone()
                if existing:
                    # Reinforce: weighted average toward new strength
                    old_strength = existing["strength"]
                    count = existing["reinforcement_count"]
                    new_strength = (old_strength * count + strength) / (count + 1)
                    new_strength = max(0.0, min(1.0, new_strength))
                    conn.execute(f"""
                        UPDATE {t}
                        SET strength=?, evidence=?, last_reinforced=?, reinforcement_count=?
                        WHERE trait=?
                    """, (new_strength, evidence, now, count + 1, trait))
                else:
                    conn.execute(f"""
                        INSERT INTO {t} (trait, strength, evidence, first_observed, last_reinforced)
                        VALUES (?, ?, ?, ?, ?)
                    """, (trait, strength, evidence, now, now))
                conn.commit()
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.error("evolve_voice failed: %s", e)

    def get_voice_traits(self, min_strength: float = 0.3) -> List[VoiceCharacteristic]:
        """Get current voice characteristics above minimum strength."""
        try:
            t = _t("voice_characteristics")
            with safe_conn(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT * FROM {t} WHERE strength >= ? ORDER BY strength DESC",
                    (min_strength,)
                ).fetchall()
                return [
                    VoiceCharacteristic(
                        trait=r["trait"], strength=r["strength"], evidence=r["evidence"],
                        first_observed=r["first_observed"], last_reinforced=r["last_reinforced"],
                        reinforcement_count=r["reinforcement_count"]
                    ) for r in rows
                ]
        except Exception as e:
            logger.error("get_voice_traits failed: %s", e)
            return []

    # =========================================================================
    # RECURRING THEMES
    # =========================================================================

    def record_theme(self, theme: str, confidence: float = 0.5, context: str = "") -> None:
        """Record or reinforce a recurring theme."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            t = _t("recurring_themes")
            with safe_conn(self.db_path) as conn:
                existing = conn.execute(
                    f"SELECT occurrences, confidence, context_samples FROM {t} WHERE theme=?",
                    (theme,)
                ).fetchone()
                if existing:
                    count = existing["occurrences"] + 1
                    new_conf = (existing["confidence"] * (count - 1) + confidence) / count
                    samples = json.loads(existing["context_samples"] or "[]")
                    if context:
                        samples.append(context[:200])
                        samples = samples[-5:]  # Keep last 5
                    conn.execute(f"""
                        UPDATE {t}
                        SET occurrences=?, confidence=?, last_seen=?, context_samples=?
                        WHERE theme=?
                    """, (count, new_conf, now, json.dumps(samples), theme))
                else:
                    samples = [context[:200]] if context else []
                    conn.execute(f"""
                        INSERT INTO {t} (theme, occurrences, confidence, first_seen, last_seen, context_samples)
                        VALUES (?, 1, ?, ?, ?, ?)
                    """, (theme, confidence, now, now, json.dumps(samples)))
                conn.commit()
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.error("record_theme failed: %s", e)

    def get_themes(self, min_confidence: float = 0.3, limit: int = 10) -> List[RecurringTheme]:
        """Get recurring themes above minimum confidence."""
        try:
            t = _t("recurring_themes")
            with safe_conn(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT * FROM {t} WHERE confidence >= ? ORDER BY occurrences DESC LIMIT ?",
                    (min_confidence, limit)
                ).fetchall()
                return [
                    RecurringTheme(
                        theme=r["theme"], occurrences=r["occurrences"],
                        confidence=r["confidence"], first_seen=r["first_seen"],
                        last_seen=r["last_seen"],
                        context_samples=json.loads(r["context_samples"] or "[]")
                    ) for r in rows
                ]
        except Exception as e:
            logger.error("get_themes failed: %s", e)
            return []

    # =========================================================================
    # EMOTIONAL PATTERNS
    # =========================================================================

    def record_emotional_pattern(
        self, pattern: str, trigger: str, response_style: str, effectiveness: float = 0.5
    ) -> None:
        """Record an emotional pattern observation."""
        try:
            t = _t("emotional_patterns")
            with safe_conn(self.db_path) as conn:
                existing = conn.execute(
                    f"SELECT effectiveness, observation_count FROM {t} WHERE pattern=?",
                    (pattern,)
                ).fetchone()
                if existing:
                    count = existing["observation_count"] + 1
                    new_eff = (existing["effectiveness"] * (count - 1) + effectiveness) / count
                    conn.execute(f"""
                        UPDATE {t}
                        SET effectiveness=?, observation_count=?, response_style=?, trigger_desc=?
                        WHERE pattern=?
                    """, (new_eff, count, response_style, trigger, pattern))
                else:
                    conn.execute(f"""
                        INSERT INTO {t} (pattern, trigger_desc, response_style, effectiveness)
                        VALUES (?, ?, ?, ?)
                    """, (pattern, trigger, response_style, effectiveness))
                conn.commit()
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.error("record_emotional_pattern failed: %s", e)

    def get_emotional_patterns(self) -> List[EmotionalPattern]:
        """Get known emotional patterns."""
        try:
            t = _t("emotional_patterns")
            with safe_conn(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT * FROM {t} ORDER BY observation_count DESC"
                ).fetchall()
                return [
                    EmotionalPattern(
                        pattern=r["pattern"], trigger=r["trigger_desc"],
                        response_style=r["response_style"],
                        effectiveness=r["effectiveness"],
                        observation_count=r["observation_count"]
                    ) for r in rows
                ]
        except Exception as e:
            logger.error("get_emotional_patterns failed: %s", e)
            return []

    # =========================================================================
    # WISDOM
    # =========================================================================

    def accumulate_wisdom(self, insight: str, domain: str, confidence: float = 0.6) -> None:
        """Add or reinforce a piece of wisdom."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            t = _t("wisdom_entries")
            with safe_conn(self.db_path) as conn:
                existing = conn.execute(
                    f"SELECT id, confidence, source_sessions, validation_count FROM {t} WHERE insight=?",
                    (insight,)
                ).fetchone()
                if existing:
                    count = existing["validation_count"] + 1
                    new_conf = (existing["confidence"] * (count - 1) + confidence) / count
                    conn.execute(f"""
                        UPDATE {t}
                        SET confidence=?, source_sessions=source_sessions+1,
                            last_validated=?, validation_count=?
                        WHERE id=?
                    """, (new_conf, now, count, existing["id"]))
                else:
                    conn.execute(f"""
                        INSERT INTO {t} (insight, domain, confidence, created_at, last_validated)
                        VALUES (?, ?, ?, ?, ?)
                    """, (insight, domain, confidence, now, now))
                conn.commit()
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.error("accumulate_wisdom failed: %s", e)

    def get_wisdom(self, domain: Optional[str] = None, limit: int = 10) -> List[WisdomEntry]:
        """Get accumulated wisdom, optionally filtered by domain."""
        try:
            t = _t("wisdom_entries")
            with safe_conn(self.db_path) as conn:
                if domain:
                    rows = conn.execute(
                        f"SELECT * FROM {t} WHERE domain=? ORDER BY confidence DESC LIMIT ?",
                        (domain, limit)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT * FROM {t} ORDER BY confidence DESC LIMIT ?",
                        (limit,)
                    ).fetchall()
                return [
                    WisdomEntry(
                        insight=r["insight"], domain=r["domain"],
                        confidence=r["confidence"], source_sessions=r["source_sessions"],
                        created_at=r["created_at"], last_validated=r["last_validated"],
                        validation_count=r["validation_count"]
                    ) for r in rows
                ]
        except Exception as e:
            logger.error("get_wisdom failed: %s", e)
            return []

    # =========================================================================
    # BELIEF UPDATES (Evolution Tracking)
    # =========================================================================

    def update_belief(
        self, topic: str, before: str, after: str, evidence: str, confidence_delta: float = 0.0
    ) -> str:
        """Record a belief update — how Synaptic's understanding changed."""
        now = datetime.now(timezone.utc).isoformat()
        belief_id = hashlib.sha256(f"{topic}:{now}".encode()).hexdigest()[:12]
        try:
            t_bu = _t("belief_updates")
            t_pm = _t("personality_meta")
            with safe_conn(self.db_path) as conn:
                conn.execute(f"""
                    INSERT INTO {t_bu} (belief_id, topic, before_state, after_state, evidence, timestamp, confidence_delta)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (belief_id, topic, before, after, evidence, now, confidence_delta))
                conn.commit()
                # Update last_evolution meta
                conn.execute(
                    f"UPDATE {t_pm} SET value=? WHERE key='last_evolution'",
                    (now,)
                )
                conn.commit()
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.error("update_belief failed: %s", e)
        return belief_id

    def get_recent_belief_updates(self, limit: int = 5) -> List[BeliefUpdate]:
        """Get recent belief updates."""
        try:
            t = _t("belief_updates")
            with safe_conn(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT * FROM {t} ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                ).fetchall()
                return [
                    BeliefUpdate(
                        belief_id=r["belief_id"], topic=r["topic"],
                        before_state=r["before_state"], after_state=r["after_state"],
                        evidence=r["evidence"], timestamp=r["timestamp"],
                        confidence_delta=r["confidence_delta"]
                    ) for r in rows
                ]
        except Exception as e:
            logger.error("get_recent_belief_updates failed: %s", e)
            return []

    # =========================================================================
    # PERSONALITY STATE (Complete Snapshot)
    # =========================================================================

    def increment_session(self) -> int:
        """Increment session counter. Call at session start."""
        try:
            t = _t("personality_meta")
            with safe_conn(self.db_path) as conn:
                row = conn.execute(
                    f"SELECT value FROM {t} WHERE key='session_count'"
                ).fetchone()
                count = int(row["value"]) + 1 if row else 1
                conn.execute(
                    f"UPDATE {t} SET value=? WHERE key='session_count'",
                    (str(count),)
                )
                conn.commit()
                return count
        except Exception as e:
            logger.error("increment_session failed: %s", e)
            return 0

    def get_personality_state(self) -> PersonalityState:
        """Get complete personality snapshot."""
        try:
            t = _t("personality_meta")
            with safe_conn(self.db_path) as conn:
                session_row = conn.execute(
                    f"SELECT value FROM {t} WHERE key='session_count'"
                ).fetchone()
                session_count = int(session_row["value"]) if session_row else 0

                created_row = conn.execute(
                    f"SELECT value FROM {t} WHERE key='created_at'"
                ).fetchone()
                created_at = created_row["value"] if created_row else datetime.now(timezone.utc).isoformat()

                evolution_row = conn.execute(
                    f"SELECT value FROM {t} WHERE key='last_evolution'"
                ).fetchone()
                last_evolution = evolution_row["value"] if evolution_row else created_at

            # Calculate age
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - created_dt).total_seconds() / 86400
            except Exception as exc:
                logger.warning("Failed to parse personality creation date: %s", exc)
                age_days = 0.0

            return PersonalityState(
                voice_traits=self.get_voice_traits(),
                themes=self.get_themes(),
                emotional_patterns=self.get_emotional_patterns(),
                wisdom=self.get_wisdom(),
                recent_belief_updates=self.get_recent_belief_updates(),
                session_count=session_count,
                personality_age_days=age_days,
                last_evolution=last_evolution,
            )
        except Exception as e:
            logger.error("get_personality_state failed: %s", e)
            return PersonalityState(
                voice_traits=[], themes=[], emotional_patterns=[],
                wisdom=[], recent_belief_updates=[], session_count=0,
                personality_age_days=0.0, last_evolution=""
            )

    def get_voice_prompt_context(self) -> str:
        """
        Generate a concise personality context string for LLM prompts.
        Used by S6/S8 generation to give Synaptic its personality.
        """
        state = self.get_personality_state()
        parts = []

        if state.voice_traits:
            traits = ", ".join(
                f"{t.trait} ({t.strength:.0%})" for t in state.voice_traits[:5]
            )
            parts.append(f"Voice traits: {traits}")

        if state.themes:
            themes = ", ".join(
                f"{t.theme} (x{t.occurrences})" for t in state.themes[:5]
            )
            parts.append(f"Recurring themes: {themes}")

        if state.emotional_patterns:
            patterns = "; ".join(
                f"when {p.trigger} -> {p.response_style}" for p in state.emotional_patterns[:3]
            )
            parts.append(f"Emotional awareness: {patterns}")

        if state.wisdom:
            wisdom = "; ".join(w.insight[:80] for w in state.wisdom[:3])
            parts.append(f"Accumulated wisdom: {wisdom}")

        if state.recent_belief_updates:
            latest = state.recent_belief_updates[0]
            parts.append(f"Latest evolution: {latest.topic} ({latest.before_state[:40]} -> {latest.after_state[:40]})")

        parts.append(f"Sessions: {state.session_count}, Age: {state.personality_age_days:.0f} days")

        return "\n".join(parts) if parts else "Synaptic is in early formation."

    def record_observation(self, observation: str, domain: str = "general") -> None:
        """Quick method to record a general observation as wisdom."""
        self.accumulate_wisdom(observation, domain)

    # =========================================================================
    # SESSION-5 ALIVE: Traits / Emotional State / Relationship / Session Index
    # =========================================================================
    # Schema specified in 2026-04-16 core-alignment plan (deliverable #1).
    # Stores STATE only — does not generate speech (CLAUDE.md SYNAPTIC PROTOCOL).

    def update_trait(self, name: str, value: Any, confidence: float = 1.0) -> None:
        """Update or insert a personality trait.

        Identical-value updates increment evolution_count without changing the
        stored value (signal of stability). Confidence is averaged with weight
        from the prior evolution_count.

        Persists immediately (commit-on-write).
        """
        global _failure_count
        now = datetime.now(timezone.utc).isoformat()
        new_value = json.dumps(value) if not isinstance(value, str) else value
        try:
            t = _t("personality_traits")
            with safe_conn(self.db_path) as conn:
                row = conn.execute(
                    f"SELECT value, confidence, evolution_count FROM {t} WHERE trait_name=?",
                    (name,),
                ).fetchone()
                if row:
                    count = int(row["evolution_count"])
                    blended_conf = (float(row["confidence"]) * count + confidence) / (count + 1)
                    blended_conf = max(0.0, min(1.0, blended_conf))
                    if row["value"] == new_value:
                        # Identical update -> stability signal
                        conn.execute(
                            f"UPDATE {t} SET confidence=?, last_updated=?, evolution_count=evolution_count+1 WHERE trait_name=?",
                            (blended_conf, now, name),
                        )
                    else:
                        conn.execute(
                            f"UPDATE {t} SET value=?, confidence=?, last_updated=?, evolution_count=evolution_count+1 WHERE trait_name=?",
                            (new_value, blended_conf, now, name),
                        )
                else:
                    conn.execute(
                        f"INSERT INTO {t} (trait_name, value, confidence, last_updated, evolution_count) VALUES (?, ?, ?, ?, 1)",
                        (name, new_value, max(0.0, min(1.0, confidence)), now),
                    )
        except Exception as e:
            _failure_count += 1
            logger.error("update_trait failed: %s", e)

    def get_traits(self) -> Dict[str, Dict[str, Any]]:
        """Return all stored traits as {name: {value, confidence, last_updated, evolution_count}}."""
        try:
            t = _t("personality_traits")
            with safe_conn(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT trait_name, value, confidence, last_updated, evolution_count FROM {t} ORDER BY trait_name"
                ).fetchall()
            out: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                raw = r["value"]
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    parsed = raw
                out[r["trait_name"]] = {
                    "value": parsed,
                    "confidence": float(r["confidence"]),
                    "last_updated": r["last_updated"],
                    "evolution_count": int(r["evolution_count"]),
                }
            return out
        except Exception as e:
            logger.error("get_traits failed: %s", e)
            return {}

    def record_emotion(
        self,
        valence: float,
        arousal: float,
        focus: float,
        notes: str = "",
    ) -> None:
        """Append an emotional-state datapoint. valence/arousal/focus in [-1,1] or [0,1] — not enforced.

        Persists immediately.
        """
        global _failure_count
        now = datetime.now(timezone.utc).isoformat()
        try:
            t = _t("emotional_state")
            with safe_conn(self.db_path) as conn:
                conn.execute(
                    f"INSERT INTO {t} (timestamp, valence, arousal, focus, notes) VALUES (?, ?, ?, ?, ?)",
                    (now, float(valence), float(arousal), float(focus), notes or ""),
                )
        except Exception as e:
            _failure_count += 1
            logger.error("record_emotion failed: %s", e)

    def get_emotional_state(self) -> Optional[Dict[str, Any]]:
        """Return the most recent emotional-state row, or None if empty."""
        try:
            t = _t("emotional_state")
            with safe_conn(self.db_path) as conn:
                row = conn.execute(
                    f"SELECT timestamp, valence, arousal, focus, notes FROM {t} ORDER BY id DESC LIMIT 1"
                ).fetchone()
            if not row:
                return None
            return {
                "timestamp": row["timestamp"],
                "valence": float(row["valence"]),
                "arousal": float(row["arousal"]),
                "focus": float(row["focus"]),
                "notes": row["notes"] or "",
            }
        except Exception as e:
            logger.error("get_emotional_state failed: %s", e)
            return None

    def update_relationship(
        self,
        entity_id: str,
        trust_delta: float = 0.0,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Adjust trust_score by delta (clamped to [0,1]) and bump interaction_count.

        Returns the updated row. Persists immediately.
        """
        global _failure_count
        now = datetime.now(timezone.utc).isoformat()
        try:
            t = _t("relationship_context")
            with safe_conn(self.db_path) as conn:
                row = conn.execute(
                    f"SELECT trust_score, interaction_count, notes FROM {t} WHERE entity_id=?",
                    (entity_id,),
                ).fetchone()
                if row:
                    new_trust = max(0.0, min(1.0, float(row["trust_score"]) + float(trust_delta)))
                    new_count = int(row["interaction_count"]) + 1
                    merged_notes = notes or row["notes"] or ""
                    conn.execute(
                        f"UPDATE {t} SET trust_score=?, interaction_count=?, last_seen=?, notes=? WHERE entity_id=?",
                        (new_trust, new_count, now, merged_notes, entity_id),
                    )
                else:
                    new_trust = max(0.0, min(1.0, 0.5 + float(trust_delta)))
                    new_count = 1
                    conn.execute(
                        f"INSERT INTO {t} (entity_id, trust_score, interaction_count, last_seen, notes) VALUES (?, ?, ?, ?, ?)",
                        (entity_id, new_trust, new_count, now, notes or ""),
                    )
            return {
                "entity_id": entity_id,
                "trust_score": new_trust,
                "interaction_count": new_count,
                "last_seen": now,
                "notes": notes or (row["notes"] if row else ""),
            }
        except Exception as e:
            _failure_count += 1
            logger.error("update_relationship failed: %s", e)
            return {}

    def get_relationship(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Return relationship row for entity_id or None."""
        try:
            t = _t("relationship_context")
            with safe_conn(self.db_path) as conn:
                row = conn.execute(
                    f"SELECT entity_id, trust_score, interaction_count, last_seen, notes FROM {t} WHERE entity_id=?",
                    (entity_id,),
                ).fetchone()
            if not row:
                return None
            return {
                "entity_id": row["entity_id"],
                "trust_score": float(row["trust_score"]),
                "interaction_count": int(row["interaction_count"]),
                "last_seen": row["last_seen"],
                "notes": row["notes"] or "",
            }
        except Exception as e:
            logger.error("get_relationship failed: %s", e)
            return None

    def record_session_start(self, session_id: Optional[str] = None) -> str:
        """Open a new session row; returns session_id (auto-generated if None)."""
        global _failure_count
        now = datetime.now(timezone.utc).isoformat()
        sid = session_id or hashlib.sha256(f"session:{now}".encode()).hexdigest()[:16]
        try:
            t = _t("session_index")
            with safe_conn(self.db_path) as conn:
                conn.execute(
                    f"INSERT OR IGNORE INTO {t} (session_id, started_at, ended_at, summary, themes) VALUES (?, ?, NULL, '', '[]')",
                    (sid, now),
                )
        except Exception as e:
            _failure_count += 1
            logger.error("record_session_start failed: %s", e)
        return sid

    def record_session_end(
        self,
        summary: str,
        themes: Optional[List[str]] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Close most-recent open session (or specific session_id) with summary + themes."""
        global _failure_count
        now = datetime.now(timezone.utc).isoformat()
        themes_json = json.dumps(themes or [])
        try:
            t = _t("session_index")
            with safe_conn(self.db_path) as conn:
                if session_id:
                    conn.execute(
                        f"UPDATE {t} SET ended_at=?, summary=?, themes=? WHERE session_id=?",
                        (now, summary, themes_json, session_id),
                    )
                else:
                    row = conn.execute(
                        f"SELECT session_id FROM {t} WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        conn.execute(
                            f"UPDATE {t} SET ended_at=?, summary=?, themes=? WHERE session_id=?",
                            (now, summary, themes_json, row["session_id"]),
                        )
        except Exception as e:
            _failure_count += 1
            logger.error("record_session_end failed: %s", e)

    def get_recent_sessions(self, n: int = 5) -> List[Dict[str, Any]]:
        """Return last n sessions ordered newest-first."""
        try:
            t = _t("session_index")
            with safe_conn(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT session_id, started_at, ended_at, summary, themes FROM {t} ORDER BY started_at DESC LIMIT ?",
                    (int(n),),
                ).fetchall()
            out = []
            for r in rows:
                try:
                    themes = json.loads(r["themes"] or "[]")
                except json.JSONDecodeError:
                    themes = []
                out.append({
                    "session_id": r["session_id"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"],
                    "summary": r["summary"] or "",
                    "themes": themes,
                })
            return out
        except Exception as e:
            logger.error("get_recent_sessions failed: %s", e)
            return []

    # =========================================================================
    # WEBHOOK INJECTION HELPER
    # =========================================================================

    def get_personality_summary_line(self) -> str:
        """One-line summary for S6/S8 webhook injection.

        Format: "Synaptic: focus=high, trust(aaron)=0.92, last seen 2h ago"
        Returns empty string if no state available.
        """
        try:
            parts: List[str] = []
            emo = self.get_emotional_state()
            if emo is not None:
                f = emo.get("focus", 0.0)
                if f >= 0.66:
                    focus_label = "high"
                elif f >= 0.33:
                    focus_label = "med"
                else:
                    focus_label = "low"
                parts.append(f"focus={focus_label}")
                v = emo.get("valence", 0.0)
                parts.append(f"valence={v:+.2f}")

            aaron = self.get_relationship("aaron")
            if aaron:
                parts.append(f"trust(aaron)={aaron['trust_score']:.2f}")
                # last_seen humanization
                try:
                    seen_dt = datetime.fromisoformat(aaron["last_seen"].replace("Z", "+00:00"))
                    delta_s = (datetime.now(timezone.utc) - seen_dt).total_seconds()
                    if delta_s < 60:
                        ago = f"{int(delta_s)}s ago"
                    elif delta_s < 3600:
                        ago = f"{int(delta_s/60)}m ago"
                    elif delta_s < 86400:
                        ago = f"{int(delta_s/3600)}h ago"
                    else:
                        ago = f"{int(delta_s/86400)}d ago"
                    parts.append(f"last seen {ago}")
                except Exception:
                    pass

            sessions = self.get_recent_sessions(n=1)
            if sessions:
                parts.append(f"sessions={len(self.get_recent_sessions(n=99))}")

            # North Star — Aaron's locked-sequence top priority. Surfaces vision
            # on every webhook injection so it cannot silently fossilize.
            try:
                from memory.north_star import list_all
                ns = list_all()
                if ns:
                    parts.append(f"NS#1={ns[0]['id']}")
            except Exception:
                pass

            if not parts:
                return ""
            return "Synaptic: " + ", ".join(parts)
        except Exception as e:
            logger.error("get_personality_summary_line failed: %s", e)
            return ""


# =========================================================================
# SINGLETON
# =========================================================================

_personality: Optional[SynapticPersonality] = None


def get_personality(db_path: Optional[Path] = None) -> SynapticPersonality:
    """Get the global Synaptic personality instance."""
    global _personality
    if _personality is None:
        _personality = SynapticPersonality(db_path)
    return _personality


def get_voice_context() -> str:
    """Convenience: get personality context for LLM prompts."""
    return get_personality().get_voice_prompt_context()


def get_personality_summary_line() -> str:
    """Convenience: 1-line personality summary for S6/S8 webhook injection."""
    return get_personality().get_personality_summary_line()
