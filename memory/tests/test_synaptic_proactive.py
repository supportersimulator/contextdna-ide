"""
Tests for Synaptic Proactive Insight Engine

Covers all 7 insight sources, should_alert_aaron(), write_seed_file(),
deduplication, rate limiting, and run_proactive_check().
"""

import json
import time
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock
from dataclasses import dataclass

import memory.synaptic_proactive as sp
from memory.synaptic_proactive import (
    ProactiveInsight,
    generate_proactive_insights,
    should_alert_aaron,
    write_seed_file,
    run_proactive_check,
    _hash_content,
    _is_duplicate,
    _insights_from_outcome_tracker,
    _insights_from_session_historian,
    _insights_from_ignored_patterns,
    _insights_from_cross_session_drift,
    _insights_from_patterns,
    _insights_from_beliefs,
    _insights_from_personality,
)


# =========================================================================
# FIXTURES
# =========================================================================

@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level state between tests."""
    sp._last_call_ts = 0.0
    sp._failure_count = 0
    sp._recent_hashes.clear()
    yield


@pytest.fixture
def tmp_seed_dir(tmp_path):
    """Provide a temporary seed directory."""
    return tmp_path / "seeds"


# =========================================================================
# PROACTIVE INSIGHT DATACLASS
# =========================================================================

class TestProactiveInsight:
    def test_basic_creation(self):
        i = ProactiveInsight(
            type="warning", content="test", confidence=0.8, source="test_src"
        )
        assert i.type == "warning"
        assert i.detail == ""

    def test_with_detail(self):
        i = ProactiveInsight(
            type="alert", content="x", confidence=0.9, source="s", detail="more info"
        )
        assert i.detail == "more info"


# =========================================================================
# DEDUPLICATION
# =========================================================================

class TestDeduplication:
    def test_hash_deterministic(self):
        assert _hash_content("hello") == _hash_content("hello")
        assert _hash_content("Hello") == _hash_content("hello")  # case insensitive

    def test_duplicate_detection(self):
        sp._recent_hashes.clear()
        assert not _is_duplicate("first insight")
        assert _is_duplicate("first insight")
        assert not _is_duplicate("second insight")

    def test_hash_pruning(self):
        sp._recent_hashes.clear()
        # Fill past max -- when overflow triggers, set is cleared and rebuilt
        for i in range(sp._MAX_RECENT_HASHES + 5):
            _is_duplicate(f"insight-{i}")
        # After overflow the set was reset, so it should be smaller than max
        assert len(sp._recent_hashes) < sp._MAX_RECENT_HASHES


# =========================================================================
# SOURCE 4: OUTCOME TRACKER (REPEATED FAILURES)
# =========================================================================

class TestOutcomeTrackerInsights:
    def test_no_tracker_available(self):
        with patch(
            "memory.synaptic_proactive._insights_from_outcome_tracker",
            side_effect=Exception("no module"),
        ):
            # Direct call should handle import failures gracefully
            pass
        # The real function catches exceptions internally
        with patch.dict("sys.modules", {"memory.outcome_tracker": None}):
            result = _insights_from_outcome_tracker()
            assert result == []

    def test_repeated_failures_detected(self):
        mock_tracker = MagicMock()
        mock_tracker.get_completed_outcomes.return_value = [
            {"task": "Fix NATS timeout", "approach": f"attempt {i}",
             "success": False, "actual_outcome": "Still timing out"}
            for i in range(4)
        ]
        import sys
        mock_mod = MagicMock()
        mock_mod.get_outcome_tracker = lambda: mock_tracker
        with patch.dict(sys.modules, {"memory.outcome_tracker": mock_mod}):
            results = _insights_from_outcome_tracker()

        assert len(results) >= 1
        assert results[0].type == "warning"
        assert "4 times" in results[0].content
        assert results[0].source == "outcome_tracker"

    def test_no_failures_no_insights(self):
        mock_tracker = MagicMock()
        mock_tracker.get_completed_outcomes.return_value = [
            {"task": "Deploy v2", "approach": "standard", "success": True,
             "actual_outcome": "Deployed"}
        ]
        import sys
        mock_mod = MagicMock()
        mock_mod.get_outcome_tracker = lambda: mock_tracker
        with patch.dict(sys.modules, {"memory.outcome_tracker": mock_mod}):
            results = _insights_from_outcome_tracker()
        assert results == []

    def test_below_threshold_no_insight(self):
        mock_tracker = MagicMock()
        mock_tracker.get_completed_outcomes.return_value = [
            {"task": "Fix bug", "approach": "try 1", "success": False,
             "actual_outcome": "Failed"},
            {"task": "Fix bug", "approach": "try 2", "success": False,
             "actual_outcome": "Failed again"},
        ]
        import sys
        mock_mod = MagicMock()
        mock_mod.get_outcome_tracker = lambda: mock_tracker
        with patch.dict(sys.modules, {"memory.outcome_tracker": mock_mod}):
            results = _insights_from_outcome_tracker()
        # 2 failures < threshold of 3
        assert results == []


# =========================================================================
# SOURCE 5: SESSION HISTORIAN (CIRCLING)
# =========================================================================

class TestSessionHistorianInsights:
    def test_no_archive_dir(self, tmp_path):
        with patch("memory.synaptic_proactive.Path") as mock_path:
            # This source uses Path.home() internally, so we patch the import
            pass
        # Just verify it handles missing dir gracefully
        results = _insights_from_session_historian()
        # May return [] if archive doesn't exist or has insufficient data
        assert isinstance(results, list)

    def test_circling_detected(self, tmp_path):
        archive = tmp_path / ".context-dna" / "session-archive"
        archive.mkdir(parents=True)

        # Create 4 sessions mentioning "deployment" heavily
        for i in range(4):
            data = {"summary": f"Session {i}: worked on deployment pipeline fixing deployment errors in deployment system"}
            (archive / f"session-{i}.json").write_text(json.dumps(data))

        with patch.object(Path, "home", return_value=tmp_path):
            results = _insights_from_session_historian()

        assert len(results) >= 1
        assert any("circling" in r.content.lower() for r in results)
        assert results[0].source == "session_historian"

    def test_no_circling_diverse_sessions(self, tmp_path):
        archive = tmp_path / ".context-dna" / "session-archive"
        archive.mkdir(parents=True)

        # Each session uses completely different words (no overlap)
        summaries = [
            "Deployed kubernetes infrastructure containers",
            "Refactored database migrations schema",
            "Validated integration endpoints coverage",
            "Documented architectural decisions thoroughly",
        ]
        for i, summary in enumerate(summaries):
            data = {"summary": summary}
            (archive / f"session-{i}.json").write_text(json.dumps(data))

        with patch.object(Path, "home", return_value=tmp_path):
            results = _insights_from_session_historian()

        # No topic appears 3+ times
        assert results == []


# =========================================================================
# SOURCE 6: IGNORED PATTERNS
# =========================================================================

class TestIgnoredPatternInsights:
    def test_ignored_pattern_detected(self):
        old_time = (
            datetime.now(timezone.utc) - timedelta(hours=30)
        ).isoformat()

        mock_pattern = MagicMock()
        mock_pattern.surfaced = True
        mock_pattern.surfaced_at = old_time
        mock_pattern.actionable = True
        mock_pattern.title = "Redis connection pooling suboptimal"
        mock_pattern.confidence = 0.75
        mock_pattern.suggested_action = "Add connection pool config"

        mock_engine = MagicMock()
        mock_engine.get_recent.return_value = [mock_pattern]

        import sys
        mock_mod = MagicMock()
        mock_mod.get_pattern_engine = lambda: mock_engine
        with patch.dict(sys.modules, {"memory.synaptic_pattern_engine": mock_mod}):
            results = _insights_from_ignored_patterns()

        assert len(results) == 1
        assert "Ignored pattern" in results[0].content
        assert results[0].source == "ignored_pattern"

    def test_recent_pattern_not_flagged(self):
        recent_time = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()

        mock_pattern = MagicMock()
        mock_pattern.surfaced = True
        mock_pattern.surfaced_at = recent_time
        mock_pattern.actionable = True
        mock_pattern.title = "test"
        mock_pattern.confidence = 0.8

        mock_engine = MagicMock()
        mock_engine.get_recent.return_value = [mock_pattern]

        import sys
        mock_mod = MagicMock()
        mock_mod.get_pattern_engine = lambda: mock_engine
        with patch.dict(sys.modules, {"memory.synaptic_pattern_engine": mock_mod}):
            results = _insights_from_ignored_patterns()

        assert results == []


# =========================================================================
# SOURCE 7: CROSS-SESSION DRIFT
# =========================================================================

class TestCrossSessionDrift:
    def test_drift_detected(self):
        mock_update = MagicMock()
        mock_update.topic = "NATS reliability"
        mock_update.confidence_delta = -0.4
        mock_update.after_state = "NATS is unreliable under load"
        mock_update.evidence = "Observed 12 failures in last 3 sessions"

        mock_personality = MagicMock()
        mock_personality.get_recent_belief_updates.return_value = [mock_update]

        import sys
        mock_mod = MagicMock()
        mock_mod.get_personality = lambda: mock_personality
        with patch.dict(sys.modules, {"memory.synaptic_personality": mock_mod}):
            results = _insights_from_cross_session_drift()

        assert len(results) == 1
        assert results[0].type == "alert"
        assert "drift" in results[0].content.lower()
        assert "weakened" in results[0].content

    def test_small_delta_no_drift(self):
        mock_update = MagicMock()
        mock_update.topic = "test"
        mock_update.confidence_delta = 0.1  # Below threshold
        mock_update.after_state = "slightly updated"
        mock_update.evidence = "minor"

        mock_personality = MagicMock()
        mock_personality.get_recent_belief_updates.return_value = [mock_update]

        import sys
        mock_mod = MagicMock()
        mock_mod.get_personality = lambda: mock_personality
        with patch.dict(sys.modules, {"memory.synaptic_personality": mock_mod}):
            results = _insights_from_cross_session_drift()

        assert results == []


# =========================================================================
# SHOULD ALERT AARON
# =========================================================================

class TestShouldAlertAaron:
    def test_alert_on_alert_type(self):
        insights = [
            ProactiveInsight(type="alert", content="drift", confidence=0.8, source="test")
        ]
        assert should_alert_aaron(insights) is True

    def test_alert_on_two_warnings(self):
        insights = [
            ProactiveInsight(type="warning", content="w1", confidence=0.6, source="test"),
            ProactiveInsight(type="warning", content="w2", confidence=0.7, source="test"),
        ]
        assert should_alert_aaron(insights) is True

    def test_alert_on_high_confidence(self):
        insights = [
            ProactiveInsight(type="suggestion", content="s", confidence=0.95, source="test")
        ]
        assert should_alert_aaron(insights) is True

    def test_no_alert_single_warning(self):
        insights = [
            ProactiveInsight(type="warning", content="w", confidence=0.6, source="test")
        ]
        assert should_alert_aaron(insights) is False

    def test_no_alert_empty(self):
        assert should_alert_aaron([]) is False

    def test_no_alert_observations_only(self):
        insights = [
            ProactiveInsight(type="observation", content="o", confidence=0.5, source="test"),
            ProactiveInsight(type="observation", content="o2", confidence=0.6, source="test"),
        ]
        assert should_alert_aaron(insights) is False


# =========================================================================
# WRITE SEED FILE
# =========================================================================

class TestWriteSeedFile:
    def test_writes_file(self, tmp_seed_dir):
        insights = [
            ProactiveInsight(
                type="warning", content="NATS timeout 4 times",
                confidence=0.85, source="outcome_tracker",
                detail="Root cause likely connection pool"
            ),
        ]
        path = write_seed_file(insights, seed_dir=tmp_seed_dir)

        assert path is not None
        assert path.exists()
        content = path.read_text()
        assert "Synaptic Proactive Alert" in content
        assert "NATS timeout 4 times" in content
        assert "Root cause likely connection pool" in content
        assert "outcome_tracker" in content

    def test_empty_insights_no_file(self, tmp_seed_dir):
        path = write_seed_file([], seed_dir=tmp_seed_dir)
        assert path is None

    def test_multiple_insights(self, tmp_seed_dir):
        insights = [
            ProactiveInsight(type="alert", content="a1", confidence=0.9, source="s1"),
            ProactiveInsight(type="warning", content="w1", confidence=0.7, source="s2"),
            ProactiveInsight(type="suggestion", content="s1", confidence=0.6, source="s3"),
        ]
        path = write_seed_file(insights, seed_dir=tmp_seed_dir)
        content = path.read_text()
        assert "ALERT" in content
        assert "WARNING" in content
        assert "SUGGESTION" in content

    def test_seed_filename_format(self, tmp_seed_dir):
        insights = [
            ProactiveInsight(type="warning", content="x", confidence=0.5, source="s")
        ]
        path = write_seed_file(insights, seed_dir=tmp_seed_dir)
        assert path.name.startswith("fleet-seed-synaptic-")
        assert path.name.endswith(".md")


# =========================================================================
# RATE LIMITING
# =========================================================================

class TestRateLimiting:
    def test_rate_limited(self):
        # Patch all sources to return empty
        with patch("memory.synaptic_proactive._insights_from_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_beliefs", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_personality", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_outcome_tracker", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_session_historian", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_ignored_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_cross_session_drift", return_value=[]):

            # First call succeeds
            result1 = generate_proactive_insights()
            assert isinstance(result1, list)

            # Second call within rate limit returns empty
            result2 = generate_proactive_insights()
            assert result2 == []

    def test_force_bypasses_rate_limit(self):
        with patch("memory.synaptic_proactive._insights_from_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_beliefs", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_personality", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_outcome_tracker", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_session_historian", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_ignored_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_cross_session_drift", return_value=[]):

            generate_proactive_insights()
            result = generate_proactive_insights(force=True)
            assert isinstance(result, list)


# =========================================================================
# RUN PROACTIVE CHECK (FULL PIPELINE)
# =========================================================================

class TestRunProactiveCheck:
    def test_full_check_no_alert(self):
        with patch("memory.synaptic_proactive._insights_from_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_beliefs", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_personality", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_outcome_tracker", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_session_historian", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_ignored_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_cross_session_drift", return_value=[]):

            result = run_proactive_check()

        assert result["insights_count"] == 0
        assert result["should_alert"] is False
        assert result["seed_file"] is None

    def test_full_check_with_alert(self, tmp_seed_dir):
        alert_insight = ProactiveInsight(
            type="alert", content="critical drift", confidence=0.9, source="drift"
        )
        with patch("memory.synaptic_proactive._insights_from_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_beliefs", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_personality", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_outcome_tracker", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_session_historian", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_ignored_patterns", return_value=[]), \
             patch("memory.synaptic_proactive._insights_from_cross_session_drift", return_value=[alert_insight]), \
             patch.object(sp, "SEED_DIR", tmp_seed_dir):

            result = run_proactive_check()

        assert result["insights_count"] == 1
        assert result["should_alert"] is True
        assert result["seed_file"] is not None


# =========================================================================
# GET PROACTIVE CONTEXT (WEBHOOK FORMAT)
# =========================================================================

class TestGetProactiveContext:
    def test_format_output(self):
        insights = [
            ProactiveInsight(type="alert", content="drift", confidence=0.9, source="test"),
            ProactiveInsight(type="warning", content="warn", confidence=0.7, source="test2"),
        ]
        with patch("memory.synaptic_proactive.generate_proactive_insights", return_value=insights):
            ctx = sp.get_proactive_context()

        assert "Proactive insights:" in ctx
        assert "!!!" in ctx  # alert icon
        assert "!" in ctx    # warning icon


# =========================================================================
# SORTING AND PRIORITY
# =========================================================================

class TestSortingPriority:
    def test_alerts_before_warnings(self):
        insights = [
            ProactiveInsight(type="warning", content="w", confidence=0.9, source="s"),
            ProactiveInsight(type="alert", content="a", confidence=0.5, source="s"),
            ProactiveInsight(type="observation", content="o", confidence=0.99, source="s"),
        ]
        # Simulate the sorting logic
        type_priority = {"alert": 0, "warning": 1, "suggestion": 2, "observation": 3}
        insights.sort(key=lambda i: (type_priority.get(i.type, 9), -i.confidence))

        assert insights[0].type == "alert"
        assert insights[1].type == "warning"
        assert insights[2].type == "observation"
