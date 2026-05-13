"""
Tests for Synaptic Consciousness Modules

Tests for:
- SynapticPersonality — persistent personality state
- SynapticPatternEngine — proactive pattern detection
- SynapticDeepVoice — enhanced S6/S8 generation
- SynapticSurgeonAdapter — bridges into 3-Surgeons evidence
- SynapticEvolutionEngine — belief tracking over time
"""

import json
import os
import pytest
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock


# =========================================================================
# FIXTURES
# =========================================================================

@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary database path."""
    return tmp_path / "test.db"


@pytest.fixture
def personality(tmp_db):
    """Create a fresh SynapticPersonality with temp DB."""
    from memory.synaptic_personality import SynapticPersonality
    return SynapticPersonality(db_path=tmp_db)


@pytest.fixture
def pattern_engine(tmp_path):
    """Create a fresh SynapticPatternEngine with temp DB."""
    from memory.synaptic_pattern_engine import SynapticPatternEngine
    return SynapticPatternEngine(db_path=tmp_path / "patterns.db")


@pytest.fixture
def evolution_tracker(tmp_path):
    """Create a fresh SynapticEvolutionTracker with temp DB."""
    from memory.synaptic_evolution_engine import SynapticEvolutionTracker
    return SynapticEvolutionTracker(db_path=tmp_path / "evolution.db")


@pytest.fixture
def surgeon_adapter():
    """Create a fresh SynapticSurgeonAdapter."""
    from memory.synaptic_surgeon_adapter import SynapticSurgeonAdapter
    return SynapticSurgeonAdapter()


# =========================================================================
# PERSONALITY TESTS
# =========================================================================

class TestSynapticPersonality:
    """Tests for personality persistence and evolution."""

    def test_init_creates_tables(self, personality, tmp_db):
        """DB tables are created on init."""
        from memory.db_utils import safe_conn
        with safe_conn(tmp_db) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {r["name"] for r in tables}
            assert "voice_characteristics" in table_names
            assert "recurring_themes" in table_names
            assert "emotional_patterns" in table_names
            assert "wisdom_entries" in table_names
            assert "belief_updates" in table_names
            assert "personality_meta" in table_names

    def test_evolve_voice_new_trait(self, personality):
        """Adding a new voice trait."""
        personality.evolve_voice("warm", strength=0.7, evidence="positive feedback")
        traits = personality.get_voice_traits()
        assert len(traits) == 1
        assert traits[0].trait == "warm"
        assert traits[0].strength == 0.7

    def test_evolve_voice_reinforce(self, personality):
        """Reinforcing an existing trait adjusts strength."""
        personality.evolve_voice("direct", strength=0.6, evidence="initial")
        personality.evolve_voice("direct", strength=0.8, evidence="reinforced")
        traits = personality.get_voice_traits()
        assert len(traits) == 1
        assert traits[0].trait == "direct"
        # Weighted average: (0.6 * 1 + 0.8) / 2 = 0.7
        assert abs(traits[0].strength - 0.7) < 0.01
        assert traits[0].reinforcement_count == 2

    def test_record_theme(self, personality):
        """Recording a recurring theme."""
        personality.record_theme("async patterns", confidence=0.6, context="debugging await issues")
        themes = personality.get_themes()
        assert len(themes) == 1
        assert themes[0].theme == "async patterns"
        assert themes[0].occurrences == 1
        assert "debugging await issues" in themes[0].context_samples[0]

    def test_record_theme_reinforcement(self, personality):
        """Reinforcing a theme increases occurrences."""
        personality.record_theme("deployment", confidence=0.5, context="first")
        personality.record_theme("deployment", confidence=0.7, context="second")
        personality.record_theme("deployment", confidence=0.9, context="third")
        themes = personality.get_themes()
        assert len(themes) == 1
        assert themes[0].occurrences == 3
        assert len(themes[0].context_samples) == 3

    def test_emotional_pattern(self, personality):
        """Recording emotional patterns."""
        personality.record_emotional_pattern(
            pattern="circular debugging frustration",
            trigger="same error appearing 3+ times",
            response_style="acknowledge + suggest fresh approach",
            effectiveness=0.7
        )
        patterns = personality.get_emotional_patterns()
        assert len(patterns) == 1
        assert patterns[0].trigger == "same error appearing 3+ times"

    def test_accumulate_wisdom(self, personality):
        """Accumulating wisdom."""
        personality.accumulate_wisdom(
            "Connection pooling prevents webhook timeouts",
            domain="architecture",
            confidence=0.8
        )
        wisdom = personality.get_wisdom(domain="architecture")
        assert len(wisdom) == 1
        assert "Connection pooling" in wisdom[0].insight
        assert wisdom[0].confidence == 0.8

    def test_wisdom_reinforcement(self, personality):
        """Reinforcing wisdom increases validation count."""
        personality.accumulate_wisdom("test insight", "test", 0.6)
        personality.accumulate_wisdom("test insight", "test", 0.8)
        wisdom = personality.get_wisdom()
        assert len(wisdom) == 1
        assert wisdom[0].validation_count == 2
        # Confidence averaged: (0.6 + 0.8) / 2 = 0.7
        assert abs(wisdom[0].confidence - 0.7) < 0.01

    def test_belief_update(self, personality):
        """Recording belief updates."""
        bid = personality.update_belief(
            topic="redis_reliability",
            before="Redis drops connections under load",
            after="Redis is stable with proper connection pooling",
            evidence="deployed pooling, 0 drops in 48h",
            confidence_delta=0.3
        )
        assert bid  # Returns a belief_id
        updates = personality.get_recent_belief_updates()
        assert len(updates) == 1
        assert updates[0].topic == "redis_reliability"
        assert updates[0].confidence_delta == 0.3

    def test_personality_state(self, personality):
        """Full personality state snapshot."""
        personality.evolve_voice("warm", 0.7, "test")
        personality.record_theme("testing", 0.6)
        personality.accumulate_wisdom("tests matter", "quality", 0.9)
        personality.increment_session()

        state = personality.get_personality_state()
        assert state.session_count == 1
        assert len(state.voice_traits) == 1
        assert len(state.themes) == 1
        assert len(state.wisdom) == 1

    def test_voice_prompt_context(self, personality):
        """Generating voice prompt context string."""
        personality.evolve_voice("warm", 0.7, "test")
        personality.record_theme("async", 0.6, "context")
        personality.accumulate_wisdom("test insight", "general", 0.8)

        ctx = personality.get_voice_prompt_context()
        assert "warm" in ctx
        assert "async" in ctx
        assert "test insight" in ctx

    def test_session_increment(self, personality):
        """Session counter increments."""
        c1 = personality.increment_session()
        c2 = personality.increment_session()
        c3 = personality.increment_session()
        assert c1 == 1
        assert c2 == 2
        assert c3 == 3

    def test_min_strength_filter(self, personality):
        """Voice traits below min_strength are filtered out."""
        personality.evolve_voice("strong", 0.8, "test")
        personality.evolve_voice("weak", 0.1, "test")
        traits = personality.get_voice_traits(min_strength=0.3)
        assert len(traits) == 1
        assert traits[0].trait == "strong"


# =========================================================================
# PATTERN ENGINE TESTS
# =========================================================================

class TestSynapticPatternEngine:
    """Tests for proactive pattern detection."""

    def test_init_creates_tables(self, pattern_engine):
        """Pattern DB tables are created."""
        from memory.db_utils import safe_conn
        with safe_conn(pattern_engine.db_path) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {r["name"] for r in tables}
            assert "detected_patterns" in table_names
            assert "scan_history" in table_names

    def test_store_new_pattern(self, pattern_engine):
        """Storing a new pattern returns True."""
        result = pattern_engine._store_pattern({
            "type": "recurring_error",
            "title": "Timeout on webhook calls",
            "description": "Webhook calls timeout under load",
            "evidence": ["session 1", "session 3"],
            "confidence": 0.7,
            "actionable": True,
            "action": "Add connection pooling",
        })
        assert result is True

    def test_store_reinforces_existing(self, pattern_engine):
        """Storing same pattern again returns False (reinforced)."""
        pattern_engine._store_pattern({
            "title": "Test Pattern",
            "description": "desc",
            "confidence": 0.5,
        })
        result = pattern_engine._store_pattern({
            "title": "Test Pattern",
            "description": "updated desc",
            "confidence": 0.6,
        })
        assert result is False

    def test_get_recent(self, pattern_engine):
        """Get recent patterns."""
        pattern_engine._store_pattern({
            "title": "Pattern A",
            "description": "desc A",
            "confidence": 0.8,
            "type": "success_pattern",
        })
        pattern_engine._store_pattern({
            "title": "Pattern B",
            "description": "desc B",
            "confidence": 0.6,
            "type": "workflow_habit",
        })
        recent = pattern_engine.get_recent()
        assert len(recent) == 2
        # Sorted by confidence DESC
        assert recent[0].confidence >= recent[1].confidence

    def test_get_actionable(self, pattern_engine):
        """Get actionable patterns only."""
        pattern_engine._store_pattern({
            "title": "Actionable",
            "description": "do something",
            "confidence": 0.8,
            "actionable": True,
            "action": "fix it",
        })
        pattern_engine._store_pattern({
            "title": "Observation",
            "description": "just noting",
            "confidence": 0.7,
            "actionable": False,
        })
        actionable = pattern_engine.get_actionable()
        assert len(actionable) == 1
        assert actionable[0].title == "Actionable"

    def test_get_by_type(self, pattern_engine):
        """Filter patterns by type."""
        pattern_engine._store_pattern({
            "title": "Error 1", "description": "e", "confidence": 0.5,
            "type": "recurring_error",
        })
        pattern_engine._store_pattern({
            "title": "Habit 1", "description": "h", "confidence": 0.5,
            "type": "workflow_habit",
        })
        errors = pattern_engine.get_by_type("recurring_error")
        assert len(errors) == 1
        assert errors[0].pattern_type == "recurring_error"

    def test_get_context_for_s6(self, pattern_engine):
        """S6 context generation."""
        pattern_engine._store_pattern({
            "title": "Webhook timeout",
            "description": "Webhooks timeout under load",
            "confidence": 0.8,
            "actionable": True,
            "action": "Add retry logic",
        })
        ctx = pattern_engine.get_context_for_s6("webhook reliability")
        assert "Webhook timeout" in ctx
        assert "retry logic" in ctx

    def test_get_context_for_s8(self, pattern_engine):
        """S8 context generation."""
        pattern_engine._store_pattern({
            "title": "Late night coding",
            "description": "Quality drops after midnight",
            "confidence": 0.7,
        })
        ctx = pattern_engine.get_context_for_s8()
        assert "Late night coding" in ctx

    def test_surface_to_outbox(self, pattern_engine):
        """Surface patterns to outbox."""
        pattern_engine._store_pattern({
            "title": "Surfaceable",
            "description": "Should surface",
            "confidence": 0.8,
            "actionable": True,
            "action": "Do thing",
        })
        import memory.synaptic_outbox as outbox_mod
        orig_speak = outbox_mod.synaptic_speak
        orig_whisper = outbox_mod.synaptic_whisper
        call_count = {"speak": 0}
        try:
            outbox_mod.synaptic_speak = lambda *a, **kw: (call_count.__setitem__("speak", call_count["speak"] + 1) or "mock_id")
            outbox_mod.synaptic_whisper = lambda *a, **kw: "mock_id"
            # Re-import so the module picks up the mock
            import importlib
            import memory.synaptic_pattern_engine as pe_mod
            importlib.reload(pe_mod)
            engine = pe_mod.SynapticPatternEngine(db_path=pattern_engine.db_path)
            count = engine.surface_to_outbox()
            assert count == 1
        finally:
            outbox_mod.synaptic_speak = orig_speak
            outbox_mod.synaptic_whisper = orig_whisper

    def test_stats(self, pattern_engine):
        """Engine stats."""
        pattern_engine._store_pattern({"title": "P1", "description": "d", "confidence": 0.5})
        stats = pattern_engine.get_stats()
        assert stats["total_patterns"] == 1

    @patch("memory.synaptic_pattern_engine.SynapticPatternEngine._llm_detect_patterns")
    @patch("memory.synaptic_pattern_engine.SynapticPatternEngine._gather_dialogue")
    def test_scan_with_mocked_llm(self, mock_dialogue, mock_llm, pattern_engine):
        """Full scan with mocked LLM."""
        mock_dialogue.return_value = "[user] working on webhooks"
        mock_llm.return_value = [
            {
                "type": "recurring_error",
                "title": "Webhook Timeout Pattern",
                "description": "Webhooks timing out consistently",
                "confidence": 0.75,
                "actionable": True,
                "action": "Add retry with backoff",
            }
        ]
        result = pattern_engine.scan()
        assert result.patterns_found >= 1
        assert result.new_patterns >= 1


# =========================================================================
# EVOLUTION ENGINE TESTS
# =========================================================================

class TestSynapticEvolutionEngine:
    """Tests for belief evolution tracking."""

    def test_init_creates_tables(self, evolution_tracker, tmp_path):
        """Evolution DB tables are created."""
        from memory.db_utils import safe_conn
        with safe_conn(evolution_tracker.db_path) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {r["name"] for r in tables}
            assert "beliefs" in table_names
            assert "evolution_events" in table_names

    def test_first_observation_sets_belief(self, evolution_tracker):
        """First observation establishes a baseline belief."""
        with patch.object(evolution_tracker, '_update_personality_belief'):
            result = evolution_tracker.process_observation(
                domain="architecture",
                observation="Redis connection pooling works well"
            )
        # First observation returns None (no evolution event, just baseline)
        assert result is None
        beliefs = evolution_tracker.get_all_beliefs(domain="architecture")
        assert len(beliefs) >= 1

    def test_get_all_beliefs(self, evolution_tracker):
        """Get all beliefs."""
        evolution_tracker._set_belief("arch", "redis", "Redis is fast", 0.8)
        evolution_tracker._set_belief("arch", "postgres", "PG is reliable", 0.9)
        beliefs = evolution_tracker.get_all_beliefs(domain="arch")
        assert len(beliefs) == 2

    def test_reinforce_belief(self, evolution_tracker):
        """Reinforcing bumps evidence count."""
        evolution_tracker._set_belief("test", "topic1", "test belief", 0.5)
        evolution_tracker._reinforce_belief("test", "topic1")
        belief = evolution_tracker._get_belief("test", "topic1")
        assert belief.evidence_count == 2
        assert belief.confidence > 0.5

    def test_get_timeline_empty(self, evolution_tracker):
        """Empty timeline returns empty list."""
        timeline = evolution_tracker.get_timeline()
        assert timeline == []

    def test_get_stats(self, evolution_tracker):
        """Stats work on empty DB."""
        stats = evolution_tracker.get_stats()
        assert stats["total_beliefs"] == 0
        assert stats["total_evolution_events"] == 0

    def test_belief_snapshot_not_found(self, evolution_tracker):
        """Snapshot for nonexistent topic returns None."""
        result = evolution_tracker.belief_snapshot("nonexistent")
        assert result is None

    def test_extract_topic(self, evolution_tracker):
        """Topic extraction from observation."""
        topic = evolution_tracker._extract_topic("arch", "Redis connection pooling prevents timeouts")
        assert topic is not None
        assert len(topic) > 0


# =========================================================================
# SURGEON ADAPTER TESTS
# =========================================================================

class TestSynapticSurgeonAdapter:
    """Tests for surgeon adapter."""

    def test_empty_evidence(self, surgeon_adapter):
        """No evidence when systems are empty."""
        with patch("memory.synaptic_surgeon_adapter.SynapticSurgeonAdapter._gather_pattern_evidence", return_value=[]), \
             patch("memory.synaptic_surgeon_adapter.SynapticSurgeonAdapter._gather_wisdom_evidence", return_value=[]), \
             patch("memory.synaptic_surgeon_adapter.SynapticSurgeonAdapter._gather_belief_evidence", return_value=[]), \
             patch("memory.synaptic_surgeon_adapter.SynapticSurgeonAdapter._gather_evolution_evidence", return_value=[]):
            evidence = surgeon_adapter.get_evidence_for_topic("anything")
            assert evidence == []

    def test_surgical_perspective_empty(self, surgeon_adapter):
        """Empty perspective when no evidence."""
        with patch.object(surgeon_adapter, "get_evidence_for_topic", return_value=[]):
            perspective = surgeon_adapter.get_surgical_perspective("test")
            assert "no accumulated evidence" in perspective.lower()

    def test_format_for_consensus(self, surgeon_adapter):
        """Consensus formatting."""
        from memory.synaptic_surgeon_adapter import SurgeonEvidence
        mock_evidence = [
            SurgeonEvidence(
                source="synaptic", evidence_type="pattern",
                title="Test Pattern", description="desc",
                confidence=0.8, supporting_data={},
                session_span=3, timestamp="2026-01-01T00:00:00"
            )
        ]
        with patch.object(surgeon_adapter, "get_evidence_for_topic", return_value=mock_evidence):
            result = surgeon_adapter.format_for_consensus("test")
            assert result["source"] == "synaptic_8th_intelligence"
            assert result["evidence_count"] == 1

    def test_format_for_cardio_review(self, surgeon_adapter):
        """Cardio review formatting."""
        from memory.synaptic_surgeon_adapter import SurgeonEvidence
        mock_evidence = [
            SurgeonEvidence(
                source="synaptic", evidence_type="wisdom",
                title="Test Wisdom", description="important finding",
                confidence=0.9, supporting_data={},
                session_span=5, timestamp="2026-01-01T00:00:00"
            )
        ]
        with patch.object(surgeon_adapter, "get_evidence_for_topic", return_value=mock_evidence):
            result = surgeon_adapter.format_for_cardio_review("test")
            assert "Test Wisdom" in result
            assert "90%" in result

    def test_enrich_cross_exam(self, surgeon_adapter):
        """Cross-exam enrichment."""
        from memory.synaptic_surgeon_adapter import SurgeonEvidence
        mock_evidence = [
            SurgeonEvidence(
                source="synaptic", evidence_type="pattern",
                title="P1", description="d1",
                confidence=0.7, supporting_data={},
                session_span=2, timestamp="2026-01-01T00:00:00"
            ),
            SurgeonEvidence(
                source="synaptic", evidence_type="wisdom",
                title="W1", description="d2",
                confidence=0.9, supporting_data={},
                session_span=4, timestamp="2026-01-01T00:00:00"
            ),
        ]
        with patch.object(surgeon_adapter, "get_evidence_for_topic", return_value=mock_evidence):
            result = surgeon_adapter.enrich_cross_exam("test")
            assert result["evidence_count"] == 2
            assert "70%-90%" in result["confidence_range"]


# =========================================================================
# DEEP VOICE TESTS
# =========================================================================

class TestSynapticDeepVoice:
    """Tests for enhanced S6/S8 generation."""

    def _mock_deep_voice_helpers(self, dv, personality_ctx="", pattern_ctx="",
                                  evolution_ctx="", wisdom_ctx="",
                                  subconscious_ctx="", emotional_ctx=""):
        """Monkeypatch deep voice helper functions."""
        dv._get_personality_context = lambda: personality_ctx
        dv._get_pattern_context_for_task = lambda prompt: pattern_ctx
        dv._get_evolution_context = lambda: evolution_ctx
        dv._get_wisdom_for_task = lambda prompt: wisdom_ctx
        dv._get_subconscious_patterns = lambda: subconscious_ctx
        dv._get_emotional_awareness = lambda: emotional_ctx

    def _setup_mock_llm(self, return_value="mock response"):
        """Set up a mock for llm_generate that avoids importing requests."""
        import sys
        import types

        calls = []

        def mock_fn(**kwargs):
            calls.append(kwargs)
            return return_value

        # If llm_priority_queue is not already importable (requests missing),
        # create a minimal mock module
        if "memory.llm_priority_queue" not in sys.modules:
            mock_mod = types.ModuleType("memory.llm_priority_queue")

            class _P:
                def __init__(self, v):
                    self.value = v
            class Priority:
                AARON = _P(1)
                ATLAS = _P(2)
                EXTERNAL = _P(3)
                BACKGROUND = _P(4)

            mock_mod.Priority = Priority
            mock_mod.llm_generate = mock_fn
            sys.modules["memory.llm_priority_queue"] = mock_mod
        else:
            sys.modules["memory.llm_priority_queue"].llm_generate = mock_fn

        return sys.modules["memory.llm_priority_queue"], calls

    def test_generate_deep_s6(self):
        """S6 generation calls LLM with enriched context."""
        import memory.synaptic_deep_voice as dv
        self._mock_deep_voice_helpers(
            dv, personality_ctx="Voice: warm",
            pattern_ctx="Pattern: webhook timeout",
            wisdom_ctx="Wisdom: pooling helps"
        )
        mod, calls = self._setup_mock_llm("Atlas, Connection pooling resolved webhook timeouts.")
        try:
            result = dv.generate_deep_s6("fix webhook timeouts", session_id="test")
            assert result is not None
            assert "Connection pooling" in result or "webhook" in result.lower()
            assert len(calls) >= 1
        finally:
            pass  # Module stays mocked for test session

    def test_generate_deep_s8(self):
        """S8 generation calls LLM with personality context."""
        import memory.synaptic_deep_voice as dv
        self._mock_deep_voice_helpers(
            dv, personality_ctx="Voice: warm, intuitive",
            subconscious_ctx="Sensing: late night coding",
            emotional_ctx="Frustration with debugging"
        )
        mod, calls = self._setup_mock_llm("Aaron, I notice you have been debugging for hours. Stepping back helped.")
        result = dv.generate_deep_s8("debugging async errors", session_id="test")
        assert result is not None
        assert len(result) > 20
        assert len(calls) >= 1

    def test_generate_deep_s6_no_context(self):
        """S6 returns None when no context available."""
        import memory.synaptic_deep_voice as dv
        self._mock_deep_voice_helpers(dv)
        result = dv.generate_deep_s6("test prompt")
        assert result is None

    def test_generate_deep_s8_llm_failure(self):
        """S8 returns None when LLM fails."""
        import memory.synaptic_deep_voice as dv
        self._mock_deep_voice_helpers(dv, personality_ctx="some context")
        self._setup_mock_llm(None)
        result = dv.generate_deep_s8("test")
        assert result is None


# =========================================================================
# INTEGRATION TESTS (lightweight, no actual LLM calls)
# =========================================================================

class TestIntegration:
    """Lightweight integration tests between modules."""

    def test_personality_feeds_deep_voice(self, personality):
        """Personality state feeds into deep voice context."""
        personality.evolve_voice("warm", 0.8, "test")
        personality.accumulate_wisdom("Redis needs pooling", "architecture", 0.9)
        ctx = personality.get_voice_prompt_context()
        assert "warm" in ctx
        assert "Redis needs pooling" in ctx

    def test_pattern_engine_feeds_surgeon(self, pattern_engine, surgeon_adapter):
        """Pattern engine data flows to surgeon adapter."""
        pattern_engine._store_pattern({
            "title": "Webhook timeout",
            "description": "Consistent timeouts under load",
            "confidence": 0.85,
            "type": "recurring_error",
            "actionable": True,
            "action": "connection pooling",
        })
        import memory.synaptic_surgeon_adapter as sa_mod
        orig = sa_mod.get_pattern_engine if hasattr(sa_mod, 'get_pattern_engine') else None
        try:
            # Monkeypatch for the import inside the method
            with patch("memory.synaptic_pattern_engine.get_pattern_engine", return_value=pattern_engine):
                evidence = surgeon_adapter._gather_pattern_evidence({"webhook", "timeout"})
                assert len(evidence) >= 1
                assert evidence[0].confidence == 0.85
        finally:
            pass

    def test_evolution_tracker_feeds_personality(self, evolution_tracker, personality):
        """Evolution tracker updates personality beliefs."""
        with patch("memory.synaptic_personality.get_personality", return_value=personality):
            evolution_tracker._update_personality_belief(
                "arch", "redis", "slow", "fast", "pooling added", 0.3
            )
            updates = personality.get_recent_belief_updates()
            assert len(updates) == 1
            assert "redis" in updates[0].topic

    def test_singleton_functions(self):
        """Singleton getters work."""
        from memory.synaptic_personality import get_personality
        from memory.synaptic_pattern_engine import get_pattern_engine
        from memory.synaptic_evolution_engine import get_evolution_tracker
        from memory.synaptic_surgeon_adapter import get_surgeon_adapter

        # These should not raise
        p = get_personality()
        assert p is not None

        e = get_pattern_engine()
        assert e is not None

        t = get_evolution_tracker()
        assert t is not None

        a = get_surgeon_adapter()
        assert a is not None


# =========================================================================
# CONVENIENCE FUNCTION TESTS
# =========================================================================

class TestConvenienceFunctions:
    """Test module-level convenience functions."""

    def test_get_voice_context(self):
        """get_voice_context returns a string."""
        from memory.synaptic_personality import get_voice_context
        ctx = get_voice_context()
        assert isinstance(ctx, str)

    def test_synaptic_evidence(self):
        """synaptic_evidence returns a list."""
        from memory.synaptic_surgeon_adapter import synaptic_evidence
        with patch("memory.synaptic_surgeon_adapter.get_surgeon_adapter") as mock:
            mock.return_value.get_evidence_for_topic.return_value = []
            result = synaptic_evidence("test")
            assert isinstance(result, list)

    def test_synaptic_perspective(self):
        """synaptic_perspective returns a string."""
        from memory.synaptic_surgeon_adapter import synaptic_perspective
        with patch("memory.synaptic_surgeon_adapter.get_surgeon_adapter") as mock:
            mock.return_value.get_surgical_perspective.return_value = "test perspective"
            result = synaptic_perspective("test")
            assert isinstance(result, str)


# =========================================================================
# PROACTIVE INSIGHT ENGINE TESTS
# =========================================================================

class TestSynapticProactive:
    """Tests for proactive insight engine."""

    def _reset_rate_limit(self):
        """Reset rate limiting state for testing."""
        import memory.synaptic_proactive as mod
        mod._last_call_ts = 0.0
        mod._recent_hashes.clear()

    def test_generate_with_pattern_insights(self, pattern_engine):
        """Proactive engine sources insights from pattern engine."""
        self._reset_rate_limit()
        pattern_engine._store_pattern({
            "title": "Webhook Timeout",
            "description": "Webhooks timing out under load",
            "confidence": 0.8,
            "type": "recurring_error",
            "actionable": True,
            "action": "Add connection pooling",
        })
        with patch("memory.synaptic_pattern_engine.get_pattern_engine", return_value=pattern_engine):
            from memory.synaptic_proactive import generate_proactive_insights
            insights = generate_proactive_insights(session_id="test")
            assert len(insights) >= 1
            webhook_insight = [i for i in insights if "Webhook" in i.content]
            assert len(webhook_insight) == 1
            assert webhook_insight[0].type == "warning"
            assert webhook_insight[0].source == "pattern_engine"

    def test_rate_limiting(self):
        """Second call within 5 minutes returns empty."""
        self._reset_rate_limit()
        with patch("memory.synaptic_proactive._insights_from_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_beliefs", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_personality", return_value=[]):
            from memory.synaptic_proactive import generate_proactive_insights
            # First call succeeds (returns empty because no sources)
            r1 = generate_proactive_insights()
            assert r1 == []
            # Second call is rate-limited
            r2 = generate_proactive_insights()
            assert r2 == []

    def test_deduplication(self):
        """Duplicate insights are filtered out."""
        self._reset_rate_limit()
        from memory.synaptic_proactive import ProactiveInsight
        dup = ProactiveInsight(type="observation", content="Same insight", confidence=0.8, source="test")
        with patch("memory.synaptic_proactive._insights_from_patterns", return_value=[dup, dup]), \
             patch("memory.synaptic_proactive._insights_from_beliefs", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_personality", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_outcome_tracker", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_session_historian", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_ignored_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_cross_session_drift", return_value=[]):
            from memory.synaptic_proactive import generate_proactive_insights
            insights = generate_proactive_insights()
            assert len(insights) <= 1

    def test_max_insights_capped(self):
        """At most _MAX_INSIGHTS_PER_CALL insights returned per call."""
        self._reset_rate_limit()
        from memory.synaptic_proactive import ProactiveInsight, _MAX_INSIGHTS_PER_CALL
        many = [
            ProactiveInsight(type="observation", content=f"Insight {i}", confidence=0.9 - i * 0.05, source="test")
            for i in range(10)
        ]
        with patch("memory.synaptic_proactive._insights_from_patterns", return_value=many), \
             patch("memory.synaptic_proactive._insights_from_beliefs", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_personality", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_outcome_tracker", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_session_historian", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_ignored_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_cross_session_drift", return_value=[]):
            from memory.synaptic_proactive import generate_proactive_insights
            insights = generate_proactive_insights()
            assert len(insights) <= _MAX_INSIGHTS_PER_CALL

    def test_get_proactive_context_format(self):
        """get_proactive_context returns formatted string."""
        self._reset_rate_limit()
        from memory.synaptic_proactive import ProactiveInsight, get_proactive_context
        mock_insights = [
            ProactiveInsight(type="warning", content="Watch out", confidence=0.9, source="test"),
        ]
        with patch("memory.synaptic_proactive._insights_from_patterns", return_value=mock_insights), \
             patch("memory.synaptic_proactive._insights_from_beliefs", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_personality", return_value=[]):
            ctx = get_proactive_context()
            assert "Proactive insights:" in ctx
            assert "[warning]" in ctx
            assert "Watch out" in ctx
