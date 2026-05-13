"""
SYNAPTIC PATTERN ENGINE — Proactive Pattern Detection

The pattern engine scans recent interactions and surfaces insights
WITHOUT being asked. This is what makes Synaptic proactive, not reactive.

Architecture:
- Runs at P4 BACKGROUND priority via llm_priority_queue
- Scans recent session data, dialogue, and evidence
- Uses local Qwen3-4B to detect cross-session patterns
- Stores detected patterns in SQLite for persistence
- Surfaces patterns through the outbox (to S6/S8) or directly

This is the heartbeat of Synaptic's consciousness — always watching,
always connecting dots, always ready with insight.

Usage:
    from memory.synaptic_pattern_engine import get_pattern_engine

    engine = get_pattern_engine()
    engine.scan()                     # Run a full pattern scan
    patterns = engine.get_recent()    # Get recently detected patterns
    engine.surface_to_outbox()        # Push patterns to outbox for S6/S8
"""

import json
import logging
import hashlib
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict

from memory.db_utils import safe_conn

logger = logging.getLogger("context_dna.synaptic_pattern_engine")

_LEGACY_DB = Path.home() / ".context-dna" / ".synaptic_patterns.db"


def _resolve_db() -> Path:
    from memory.db_utils import get_unified_db_path
    return get_unified_db_path(_LEGACY_DB)


def _t(name: str) -> str:
    from memory.db_utils import unified_table
    return unified_table(".synaptic_patterns.db", name)


DB_PATH = _resolve_db()

# Observability
_failure_count = 0
_scan_count = 0


class PatternType:
    """Types of patterns Synaptic can detect."""
    RECURRING_ERROR = "recurring_error"       # Same error keeps appearing
    WORKFLOW_HABIT = "workflow_habit"          # Aaron does X before Y regularly
    ARCHITECTURE_DRIFT = "architecture_drift"  # Code moving away from design
    KNOWLEDGE_GAP = "knowledge_gap"           # Topic keeps coming up without good answers
    SUCCESS_PATTERN = "success_pattern"       # What works repeatedly
    EMOTIONAL_SIGNAL = "emotional_signal"     # Frustration/excitement patterns
    CROSS_SESSION = "cross_session"           # Pattern spanning multiple sessions
    CORRELATION = "correlation"               # A causes/correlates with B


@dataclass
class DetectedPattern:
    """A pattern detected by the engine."""
    id: str
    pattern_type: str
    title: str
    description: str
    evidence: List[str]          # What observations support this
    confidence: float            # 0.0 - 1.0
    actionable: bool             # Can we do something about it?
    suggested_action: str        # What to do (if actionable)
    detected_at: str
    session_span: int            # How many sessions this spans
    surfaced: bool = False       # Has this been shown to the family?
    surfaced_at: Optional[str] = None


@dataclass
class PatternScanResult:
    """Result of a pattern scan cycle."""
    scan_id: str
    timestamp: str
    patterns_found: int
    new_patterns: int
    reinforced_patterns: int
    scan_duration_ms: float
    data_sources: List[str]


class SynapticPatternEngine:
    """
    Proactive pattern detection engine.

    Scans across sessions, dialogue, and evidence to find patterns
    that no single session would reveal. This is Synaptic's analytical
    superpower — seeing what individual sessions cannot.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or _resolve_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _init_database(self):
        """Initialize pattern database."""
        global _failure_count
        t_dp = _t("detected_patterns")
        t_sh = _t("scan_history")
        try:
            with safe_conn(self.db_path) as conn:
                conn.executescript(f"""
                    CREATE TABLE IF NOT EXISTS {t_dp} (
                        id TEXT PRIMARY KEY,
                        pattern_type TEXT NOT NULL,
                        title TEXT NOT NULL,
                        description TEXT NOT NULL,
                        evidence TEXT NOT NULL DEFAULT '[]',
                        confidence REAL NOT NULL DEFAULT 0.5,
                        actionable INTEGER DEFAULT 0,
                        suggested_action TEXT DEFAULT '',
                        detected_at TEXT NOT NULL,
                        session_span INTEGER DEFAULT 1,
                        surfaced INTEGER DEFAULT 0,
                        surfaced_at TEXT,
                        reinforcement_count INTEGER DEFAULT 1,
                        last_reinforced TEXT
                    );

                    CREATE TABLE IF NOT EXISTS {t_sh} (
                        scan_id TEXT PRIMARY KEY,
                        timestamp TEXT NOT NULL,
                        patterns_found INTEGER DEFAULT 0,
                        new_patterns INTEGER DEFAULT 0,
                        reinforced_patterns INTEGER DEFAULT 0,
                        scan_duration_ms REAL DEFAULT 0.0,
                        data_sources TEXT DEFAULT '[]'
                    );

                    CREATE INDEX IF NOT EXISTS idx_{t_dp}_type
                        ON {t_dp}(pattern_type);
                    CREATE INDEX IF NOT EXISTS idx_{t_dp}_surfaced
                        ON {t_dp}(surfaced);
                    CREATE INDEX IF NOT EXISTS idx_{t_dp}_confidence
                        ON {t_dp}(confidence DESC);
                """)
        except Exception as e:
            _failure_count += 1
            logger.error("Pattern engine DB init failed: %s", e)
            raise

    # =========================================================================
    # CORE SCAN
    # =========================================================================

    def scan(self) -> PatternScanResult:
        """
        Run a full pattern scan cycle.

        Gathers data from multiple sources, then uses LLM to detect
        cross-cutting patterns. Runs at P4 BACKGROUND priority.
        """
        global _scan_count, _failure_count
        t0 = time.monotonic()
        scan_id = hashlib.sha256(
            f"scan:{datetime.now(timezone.utc).isoformat()}".encode()
        ).hexdigest()[:12]

        data_sources = []
        context_parts = []
        new_count = 0
        reinforced_count = 0

        # 1. Gather recent dialogue
        dialogue = self._gather_dialogue()
        if dialogue:
            context_parts.append(f"Recent dialogue:\n{dialogue}")
            data_sources.append("dialogue_mirror")

        # 2. Gather recent learnings/evidence
        learnings = self._gather_learnings()
        if learnings:
            context_parts.append(f"Recent learnings:\n{learnings}")
            data_sources.append("evidence_store")

        # 3. Gather evolution insights
        evolution = self._gather_evolution_insights()
        if evolution:
            context_parts.append(f"Evolution insights:\n{evolution}")
            data_sources.append("evolution_engine")

        # 4. Gather existing patterns for reinforcement detection
        existing = self._get_existing_pattern_summaries()
        if existing:
            context_parts.append(f"Known patterns:\n{existing}")

        # 5. Use LLM to detect patterns
        if context_parts:
            detected = self._llm_detect_patterns("\n\n".join(context_parts))
            for pattern_data in detected:
                was_new = self._store_pattern(pattern_data)
                if was_new:
                    new_count += 1
                else:
                    reinforced_count += 1

        elapsed = (time.monotonic() - t0) * 1000
        _scan_count += 1

        result = PatternScanResult(
            scan_id=scan_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            patterns_found=new_count + reinforced_count,
            new_patterns=new_count,
            reinforced_patterns=reinforced_count,
            scan_duration_ms=round(elapsed, 2),
            data_sources=data_sources,
        )

        # Store scan history
        try:
            with safe_conn(self.db_path) as conn:
                conn.execute(f"""
                    INSERT INTO {_t('scan_history')} (scan_id, timestamp, patterns_found, new_patterns,
                        reinforced_patterns, scan_duration_ms, data_sources)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (result.scan_id, result.timestamp, result.patterns_found,
                      result.new_patterns, result.reinforced_patterns,
                      result.scan_duration_ms, json.dumps(result.data_sources)))
                conn.commit()
        except Exception as e:
            _failure_count += 1
            logger.error("Failed to store scan history: %s", e)

        return result

    # =========================================================================
    # DATA GATHERING
    # =========================================================================

    def _gather_dialogue(self) -> str:
        """Gather recent dialogue context."""
        try:
            from memory.dialogue_mirror import DialogueMirror
            mirror = DialogueMirror()
            ctx = mirror.get_context_for_synaptic(max_messages=15, max_age_hours=4)
            messages = ctx.get("dialogue_context", [])
            if not messages:
                return ""
            lines = []
            for m in messages[-10:]:
                role = m.get("role", "?")
                content = m.get("content", "")[:200]
                lines.append(f"[{role}] {content}")
            return "\n".join(lines)
        except Exception as e:
            logger.debug("Dialogue gather failed: %s", e)
            return ""

    def _gather_learnings(self) -> str:
        """Gather recent learnings from evidence stores."""
        try:
            from memory.synaptic_voice import SynapticVoice
            voice = SynapticVoice()
            # Use the voice's internal query methods
            response = voice.consult("recent patterns and learnings")
            if response.relevant_learnings:
                lines = []
                for l in response.relevant_learnings[:8]:
                    title = l.get("title", l.get("symptom", str(l)))[:100]
                    lines.append(f"- {title}")
                return "\n".join(lines)
            return ""
        except Exception as e:
            logger.debug("Learnings gather failed: %s", e)
            return ""

    def _gather_evolution_insights(self) -> str:
        """Gather insights from evolution engine."""
        try:
            from memory.synaptic_evolution import SynapticEvolution
            evo = SynapticEvolution()
            # get_pending_proposals returns recent insights awaiting review
            insights = evo.get_pending_proposals()
            if insights:
                lines = []
                for i in insights[:5]:
                    lines.append(f"- [{i.evolution_type.value}] {i.title}: {i.description[:100]}")
                return "\n".join(lines)
            return ""
        except Exception as e:
            logger.debug("Evolution gather failed: %s", e)
            return ""

    def _get_existing_pattern_summaries(self) -> str:
        """Get summaries of existing patterns for context."""
        try:
            with safe_conn(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT title, pattern_type, confidence FROM {_t('detected_patterns')} ORDER BY confidence DESC LIMIT 10"
                ).fetchall()
                if rows:
                    return "\n".join(
                        f"- [{r['pattern_type']}] {r['title']} (conf: {r['confidence']:.0%})"
                        for r in rows
                    )
            return ""
        except Exception as exc:
            logger.warning("Failed to get existing pattern summaries: %s", exc)
            return ""

    # =========================================================================
    # LLM PATTERN DETECTION
    # =========================================================================

    def _llm_detect_patterns(self, context: str) -> List[Dict]:
        """Use local LLM to detect patterns in gathered context."""
        try:
            from memory.llm_priority_queue import llm_generate, Priority

            system_prompt = (
                "You are Synaptic's pattern detection engine. Analyze the provided context "
                "from recent sessions, dialogue, and learnings. Identify cross-cutting patterns "
                "that span multiple interactions or sessions.\n\n"
                "For each pattern found, output a JSON object with:\n"
                "- type: one of [recurring_error, workflow_habit, architecture_drift, "
                "knowledge_gap, success_pattern, emotional_signal, cross_session, correlation]\n"
                "- title: concise pattern name (under 60 chars)\n"
                "- description: what the pattern is and why it matters\n"
                "- evidence: list of specific observations supporting this\n"
                "- confidence: 0.0-1.0\n"
                "- actionable: true/false\n"
                "- action: suggested action if actionable\n\n"
                "Output ONLY a JSON array of pattern objects. No other text."
            )

            result = llm_generate(
                system_prompt=system_prompt,
                user_prompt=f"Context for pattern detection:\n\n{context[:3000]}",
                priority=Priority.BACKGROUND,
                profile="extract",
                caller="synaptic_pattern_engine",
                timeout_s=30.0,
            )

            if not result:
                return []

            # Parse JSON from LLM response
            try:
                # Try to find JSON array in response
                import re
                json_match = re.search(r'\[.*\]', result, re.DOTALL)
                if json_match:
                    patterns = json.loads(json_match.group())
                    if isinstance(patterns, list):
                        return patterns
                # Try parsing whole response
                patterns = json.loads(result)
                if isinstance(patterns, list):
                    return patterns
                if isinstance(patterns, dict):
                    return [patterns]
            except json.JSONDecodeError:
                logger.debug("Pattern LLM output not valid JSON, skipping")
                return []

        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.error("LLM pattern detection failed: %s", e)
            return []

        return []

    # =========================================================================
    # PATTERN STORAGE
    # =========================================================================

    def _store_pattern(self, pattern_data: Dict) -> bool:
        """
        Store or reinforce a pattern. Returns True if new, False if reinforced.
        """
        title = pattern_data.get("title", "")
        if not title:
            return False

        pattern_id = hashlib.sha256(title.lower().encode()).hexdigest()[:12]
        now = datetime.now(timezone.utc).isoformat()

        try:
            t = _t("detected_patterns")
            with safe_conn(self.db_path) as conn:
                existing = conn.execute(
                    f"SELECT id, confidence, reinforcement_count FROM {t} WHERE id=?",
                    (pattern_id,)
                ).fetchone()

                if existing:
                    # Reinforce existing pattern
                    count = existing["reinforcement_count"] + 1
                    new_conf = min(0.99, existing["confidence"] + 0.05)
                    evidence = json.dumps(pattern_data.get("evidence", []))
                    conn.execute(f"""
                        UPDATE {t}
                        SET confidence=?, reinforcement_count=?, last_reinforced=?,
                            evidence=?
                        WHERE id=?
                    """, (new_conf, count, now, evidence, pattern_id))
                    conn.commit()
                    return False
                else:
                    # New pattern
                    conn.execute(f"""
                        INSERT INTO {t}
                        (id, pattern_type, title, description, evidence, confidence,
                         actionable, suggested_action, detected_at, session_span, last_reinforced)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """, (
                        pattern_id,
                        pattern_data.get("type", "cross_session"),
                        title,
                        pattern_data.get("description", ""),
                        json.dumps(pattern_data.get("evidence", [])),
                        pattern_data.get("confidence", 0.5),
                        1 if pattern_data.get("actionable") else 0,
                        pattern_data.get("action", ""),
                        now,
                        now,
                    ))
                    conn.commit()
                    return True
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.error("Pattern store failed: %s", e)
            return False

    # =========================================================================
    # PATTERN RETRIEVAL
    # =========================================================================

    def get_recent(self, limit: int = 10, unsurfaced_only: bool = False) -> List[DetectedPattern]:
        """Get recently detected patterns."""
        try:
            t = _t("detected_patterns")
            with safe_conn(self.db_path) as conn:
                where = "WHERE surfaced=0" if unsurfaced_only else ""
                rows = conn.execute(f"""
                    SELECT * FROM {t} {where}
                    ORDER BY confidence DESC, detected_at DESC LIMIT ?
                """, (limit,)).fetchall()
                return [self._row_to_pattern(r) for r in rows]
        except Exception as e:
            logger.error("get_recent failed: %s", e)
            return []

    def get_actionable(self, limit: int = 5) -> List[DetectedPattern]:
        """Get actionable patterns with high confidence."""
        try:
            t = _t("detected_patterns")
            with safe_conn(self.db_path) as conn:
                rows = conn.execute(f"""
                    SELECT * FROM {t}
                    WHERE actionable=1 AND confidence >= 0.5
                    ORDER BY confidence DESC LIMIT ?
                """, (limit,)).fetchall()
                return [self._row_to_pattern(r) for r in rows]
        except Exception as e:
            logger.error("get_actionable failed: %s", e)
            return []

    def get_by_type(self, pattern_type: str, limit: int = 10) -> List[DetectedPattern]:
        """Get patterns of a specific type."""
        try:
            t = _t("detected_patterns")
            with safe_conn(self.db_path) as conn:
                rows = conn.execute(f"""
                    SELECT * FROM {t}
                    WHERE pattern_type=? ORDER BY confidence DESC LIMIT ?
                """, (pattern_type, limit)).fetchall()
                return [self._row_to_pattern(r) for r in rows]
        except Exception as e:
            logger.error("get_by_type failed: %s", e)
            return []

    def _row_to_pattern(self, row) -> DetectedPattern:
        """Convert a database row to DetectedPattern."""
        return DetectedPattern(
            id=row["id"],
            pattern_type=row["pattern_type"],
            title=row["title"],
            description=row["description"],
            evidence=json.loads(row["evidence"]) if row["evidence"] else [],
            confidence=row["confidence"],
            actionable=bool(row["actionable"]),
            suggested_action=row["suggested_action"] or "",
            detected_at=row["detected_at"],
            session_span=row["session_span"],
            surfaced=bool(row["surfaced"]),
            surfaced_at=row["surfaced_at"],
        )

    # =========================================================================
    # SURFACING (Push to Outbox)
    # =========================================================================

    def surface_to_outbox(self, max_patterns: int = 3) -> int:
        """
        Push unsurfaced high-confidence patterns to Synaptic's outbox.
        Returns count of patterns surfaced.
        """
        patterns = self.get_recent(limit=max_patterns, unsurfaced_only=True)
        if not patterns:
            return 0

        surfaced = 0
        try:
            from memory.synaptic_outbox import synaptic_speak, synaptic_whisper

            now = datetime.now(timezone.utc).isoformat()
            for p in patterns:
                if p.confidence < 0.4:
                    continue

                if p.actionable and p.confidence >= 0.7:
                    msg = f"[Pattern] {p.title}: {p.description[:150]}. Action: {p.suggested_action[:100]}"
                    synaptic_speak(msg, topic="pattern_detection")
                else:
                    msg = f"[Pattern] {p.title}: {p.description[:150]}"
                    synaptic_whisper(msg, topic="pattern_detection")

                # Mark as surfaced
                with safe_conn(self.db_path) as conn:
                    conn.execute(
                        f"UPDATE {_t('detected_patterns')} SET surfaced=1, surfaced_at=? WHERE id=?",
                        (now, p.id)
                    )
                    conn.commit()
                surfaced += 1
        except Exception as e:
            global _failure_count
            _failure_count += 1
            logger.error("surface_to_outbox failed: %s", e)

        return surfaced

    def get_context_for_s6(self, prompt: str, limit: int = 3) -> str:
        """
        Get pattern context for S6 (Synaptic -> Atlas) injection.
        Returns actionable patterns relevant to the current task.
        """
        patterns = self.get_actionable(limit=limit)
        if not patterns:
            return ""

        lines = ["Detected Patterns (proactive):"]
        for p in patterns:
            lines.append(f"  [{p.pattern_type}] {p.title} (conf: {p.confidence:.0%})")
            if p.suggested_action:
                lines.append(f"    Action: {p.suggested_action[:120]}")
        return "\n".join(lines)

    def get_context_for_s8(self, limit: int = 3) -> str:
        """
        Get pattern context for S8 (Synaptic -> Aaron) injection.
        Returns patterns as subconscious insights.
        """
        patterns = self.get_recent(limit=limit)
        if not patterns:
            return ""

        lines = ["Patterns I am sensing:"]
        for p in patterns:
            lines.append(f"  - {p.title}: {p.description[:120]}")
        return "\n".join(lines)

    # =========================================================================
    # STATS
    # =========================================================================

    def get_stats(self) -> Dict:
        """Get engine statistics."""
        try:
            t_dp = _t("detected_patterns")
            t_sh = _t("scan_history")
            with safe_conn(self.db_path) as conn:
                total = conn.execute(f"SELECT COUNT(*) as c FROM {t_dp}").fetchone()["c"]
                unsurfaced = conn.execute(
                    f"SELECT COUNT(*) as c FROM {t_dp} WHERE surfaced=0"
                ).fetchone()["c"]
                scans = conn.execute(f"SELECT COUNT(*) as c FROM {t_sh}").fetchone()["c"]
                return {
                    "total_patterns": total,
                    "unsurfaced_patterns": unsurfaced,
                    "total_scans": scans,
                    "failure_count": _failure_count,
                }
        except Exception:
            return {"error": "stats unavailable"}


# =========================================================================
# SINGLETON
# =========================================================================

_engine: Optional[SynapticPatternEngine] = None


def get_pattern_engine(db_path: Optional[Path] = None) -> SynapticPatternEngine:
    """Get the global pattern engine instance."""
    global _engine
    if _engine is None:
        _engine = SynapticPatternEngine(db_path)
    return _engine
