#!/usr/bin/env python3
"""
Tests for Professor Wisdom Autonomous Refinement Loop (Cerebellum).

Covers:
1. No outcomes — graceful no-op
2. Positive reinforcement — success boosts domain confidence
3. Negative correction — failure reduces domain confidence
4. Domain matching — task keywords map to correct domains
5. Graceful degradation — missing OutcomeTracker, corrupt sidecar
6. Watermark — only processes new outcomes on subsequent runs
7. Flagging — domains below threshold get flagged for review
"""

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add memory directory to path
MEMORY_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(MEMORY_DIR.parent))


@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temp directory and patch file paths."""
    evolution_file = tmp_path / ".professor_evolution.json"
    confidence_file = tmp_path / ".professor_domain_confidence.json"
    outcome_db = tmp_path / ".outcome_tracking.db"

    patches = [
        patch("memory.professor.EVOLUTION_FILE", evolution_file),
        patch("memory.professor.DOMAIN_CONFIDENCE_FILE", confidence_file),
        patch("memory.outcome_tracker.OUTCOME_DB", outcome_db),
    ]
    for p in patches:
        p.start()

    yield tmp_path

    for p in patches:
        p.stop()


@pytest.fixture
def evolution(temp_dir):
    """Fresh ProfessorEvolution instance with temp storage."""
    from memory.professor import ProfessorEvolution
    return ProfessorEvolution()


@pytest.fixture
def tracker(temp_dir):
    """Fresh OutcomeTracker with temp DB."""
    from memory.outcome_tracker import OutcomeTracker
    return OutcomeTracker()


def _seed_outcome(tracker, task, success, approach="default approach",
                  related_patterns=None, end_time=None):
    """Helper: create a completed outcome in the tracker."""
    oid = tracker.start_tracking(
        task=task,
        approach=approach,
        expected_outcome="some expected result",
        related_patterns=related_patterns or [],
    )
    tracker.record_outcome(
        outcome_id=oid,
        success=success,
        actual_outcome="actual result",
        verification_method="test",
    )
    # Optionally override end_time for watermark testing
    if end_time:
        from memory.outcome_tracker import OUTCOME_DB
        with sqlite3.connect(str(OUTCOME_DB)) as conn:
            conn.execute(
                "UPDATE tracked_outcomes SET end_time = ? WHERE outcome_id = ?",
                (end_time, oid),
            )
            conn.commit()
    return oid


# ── Test 1: No outcomes — graceful no-op ──

class TestNoOutcomes:
    def test_refine_with_empty_tracker(self, evolution, tracker):
        """refine_from_outcomes returns cleanly when no outcomes exist."""
        result = evolution.refine_from_outcomes()
        assert result["outcomes_processed"] == 0
        assert result["domains_adjusted"] == {}
        assert result["domains_flagged"] == []
        assert len(result["refinement_log"]) == 1
        assert "No new outcomes" in result["refinement_log"][0]

    def test_refine_with_only_pending_outcomes(self, evolution, tracker):
        """Pending (unresolved) outcomes are not processed."""
        tracker.start_tracking(
            task="async event loop fix",
            approach="to_thread",
            expected_outcome="no blocking",
        )
        result = evolution.refine_from_outcomes()
        assert result["outcomes_processed"] == 0


# ── Test 2: Positive reinforcement ──

class TestPositiveReinforcement:
    def test_success_increases_confidence(self, evolution, tracker):
        """A successful outcome for a domain increases its confidence score."""
        _seed_outcome(tracker, "fix async event loop blocking", success=True,
                      related_patterns=["async_python_blocking"])
        result = evolution.refine_from_outcomes()

        assert result["outcomes_processed"] == 1
        assert "async_python" in result["domains_adjusted"]
        adj = result["domains_adjusted"]["async_python"]
        assert adj["delta"] > 0
        assert adj["direction"] == "reinforced"
        assert adj["score"] > 0.7  # Started at 0.7, went up

    def test_multiple_successes_compound(self, evolution, tracker):
        """Multiple successes compound the confidence boost."""
        for i in range(5):
            _seed_outcome(tracker, f"async fix attempt {i}", success=True)
        result = evolution.refine_from_outcomes()
        assert result["outcomes_processed"] == 5
        score = result["domains_adjusted"]["async_python"]["score"]
        # 5 * +0.03 = +0.15 from 0.7 base = 0.85
        assert score == pytest.approx(0.85, abs=0.01)


# ── Test 3: Negative correction ──

class TestNegativeCorrection:
    def test_failure_decreases_confidence(self, evolution, tracker):
        """A failed outcome decreases domain confidence."""
        _seed_outcome(tracker, "deploy docker container", success=False)
        result = evolution.refine_from_outcomes()

        assert result["outcomes_processed"] == 1
        assert "docker_ecs" in result["domains_adjusted"]
        adj = result["domains_adjusted"]["docker_ecs"]
        assert adj["delta"] < 0
        assert adj["direction"] == "penalized"
        assert adj["score"] < 0.7

    def test_repeated_failures_flag_domain(self, evolution, tracker):
        """Repeated failures push confidence below 0.4, triggering a flag."""
        # 7 failures: 0.7 - (7 * 0.05) = 0.35 < 0.4 threshold
        for i in range(7):
            _seed_outcome(tracker, f"docker deploy attempt {i}", success=False)
        result = evolution.refine_from_outcomes()

        assert "docker_ecs" in result["domains_flagged"]
        # Also check it appears in evolution's flagged_for_review
        flagged_domains = [
            f["domain"] for f in evolution.data.get("flagged_for_review", [])
            if isinstance(f, dict)
        ]
        assert "docker_ecs" in flagged_domains

    def test_confidence_never_below_floor(self, evolution, tracker):
        """Confidence is clamped at 0.1 minimum, never goes to 0."""
        for i in range(20):
            _seed_outcome(tracker, f"docker failure {i}", success=False)
        result = evolution.refine_from_outcomes()
        score = result["domains_adjusted"]["docker_ecs"]["score"]
        assert score >= 0.1


# ── Test 4: Domain matching ──

class TestDomainMatching:
    def test_task_keywords_match_correct_domain(self, evolution, tracker):
        """Task description keywords route to the correct professor domain."""
        _seed_outcome(tracker, "fix boto3 async event loop blocking", success=True)
        result = evolution.refine_from_outcomes()
        assert "async_python" in result["domains_adjusted"]

    def test_related_patterns_used_for_matching(self, evolution, tracker):
        """related_patterns field helps match outcomes to domains."""
        _seed_outcome(
            tracker,
            "fix generic performance issue",  # No domain keywords in task
            success=True,
            related_patterns=["voice_pipeline_latency"],
        )
        result = evolution.refine_from_outcomes()
        assert "voice_pipeline" in result["domains_adjusted"]

    def test_unmatched_task_is_skipped(self, evolution, tracker):
        """Outcomes with no matching domain are skipped, not assigned default."""
        _seed_outcome(tracker, "completely unrelated topic xyz", success=True)
        result = evolution.refine_from_outcomes()
        assert result["outcomes_processed"] == 1
        assert result["domains_adjusted"] == {}

    def test_multi_domain_match(self, evolution, tracker):
        """A task can match multiple domains simultaneously."""
        _seed_outcome(
            tracker,
            "deploy docker container with async python service",
            success=True,
        )
        result = evolution.refine_from_outcomes()
        adjusted = result["domains_adjusted"]
        assert "docker_ecs" in adjusted
        assert "async_python" in adjusted


# ── Test 5: Graceful degradation ──

class TestGracefulDegradation:
    def test_missing_outcome_tracker_import(self, evolution):
        """refine_from_outcomes handles missing OutcomeTracker gracefully."""
        with patch.dict("sys.modules", {"memory.outcome_tracker": None}):
            # Force ImportError by removing module
            with patch("builtins.__import__", side_effect=ImportError("mocked")):
                result = evolution.refine_from_outcomes()
        assert result["outcomes_processed"] == 0
        assert "unavailable" in result["refinement_log"][0].lower()

    def test_corrupt_confidence_file(self, temp_dir, tracker):
        """Corrupt confidence sidecar is handled — starts fresh."""
        from memory.professor import DOMAIN_CONFIDENCE_FILE, ProfessorEvolution

        # Write corrupt JSON
        DOMAIN_CONFIDENCE_FILE.write_text("{corrupt json!!!")

        evo = ProfessorEvolution()
        _seed_outcome(tracker, "async event loop fix", success=True)
        result = evo.refine_from_outcomes()

        # Should work fine, starting from defaults
        assert result["outcomes_processed"] == 1
        assert "async_python" in result["domains_adjusted"]


# ── Test 6: Watermark — only process new outcomes ──

class TestWatermark:
    def test_second_run_skips_already_processed(self, evolution, tracker):
        """Second refine call with no new outcomes returns 0 processed."""
        _seed_outcome(tracker, "async fix", success=True)

        result1 = evolution.refine_from_outcomes()
        assert result1["outcomes_processed"] == 1

        # Re-create evolution to reload watermark from disk
        from memory.professor import ProfessorEvolution
        evo2 = ProfessorEvolution()
        result2 = evo2.refine_from_outcomes()
        assert result2["outcomes_processed"] == 0

    def test_new_outcomes_after_watermark_are_processed(self, evolution, tracker):
        """Outcomes added after the watermark are processed on next run."""
        _seed_outcome(tracker, "async fix 1", success=True)
        evolution.refine_from_outcomes()

        # Add new outcome with a later timestamp
        future = (datetime.now() + timedelta(seconds=5)).isoformat()
        _seed_outcome(tracker, "async fix 2", success=False, end_time=future)

        from memory.professor import ProfessorEvolution
        evo2 = ProfessorEvolution()
        result = evo2.refine_from_outcomes()
        assert result["outcomes_processed"] == 1


# ── Test 7: Public API ──

class TestPublicAPI:
    def test_refine_from_outcomes_function(self, temp_dir, tracker):
        """The module-level refine_from_outcomes() function works."""
        from memory.professor import refine_from_outcomes, _evolution
        # Reset singleton
        import memory.professor as prof_mod
        prof_mod._evolution = None

        _seed_outcome(tracker, "deploy docker service", success=True)
        result = refine_from_outcomes()
        assert result["outcomes_processed"] == 1

        # Cleanup singleton
        prof_mod._evolution = None

    def test_get_domain_confidence_function(self, temp_dir, tracker):
        """get_domain_confidence returns scores after refinement."""
        from memory.professor import get_domain_confidence
        import memory.professor as prof_mod
        prof_mod._evolution = None

        _seed_outcome(tracker, "async event loop fix", success=True)
        from memory.professor import refine_from_outcomes
        refine_from_outcomes()

        all_scores = get_domain_confidence()
        assert "async_python" in all_scores

        single = get_domain_confidence("async_python")
        assert single["domain"] == "async_python"
        assert single["score"] > 0.7

        # Cleanup
        prof_mod._evolution = None
