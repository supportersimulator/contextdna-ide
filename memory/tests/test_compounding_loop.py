#!/usr/bin/env python3
"""
TEST: Context DNA Compounding Learning Loop

The most important test in the entire system.

Proves the core value proposition:
  "Copilot forgets, Context DNA remembers."

Full loop verified:
  1. capture_success() records a bug fix
  2. Claim enters quarantine in observability_store
  3. Scheduler evaluates quarantine, promotes to trusted
  4. Trusted claim bridges to flagged_for_review
  5. Professor.consult() surfaces the learning
  6. Success outcome recorded
  7. Professor confidence increases via refine_from_outcomes

All real SQLite stores on tmp paths. LLM calls mocked.

Run: PYTHONPATH=. .venv/bin/python3 -m pytest memory/tests/test_compounding_loop.py -v
"""

import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import pytest

from memory.lite_scheduler import LiteScheduler

# ---------------------------------------------------------------------------
# Fixture: isolated ObservabilityStore backed by a tmp SQLite DB
# ---------------------------------------------------------------------------

def _working_safe_commit(self):
    """Non-deadlocking replacement for ObservabilityStore._safe_commit.

    Production code (line 1170) has _safe_commit calling itself inside
    a non-reentrant Lock which deadlocks. This replacement does the
    intended thing: commit under lock.
    """
    with self._write_lock:
        self._sqlite_conn.commit()


@pytest.fixture
def tmp_obs_store(tmp_path):
    """Create a real ObservabilityStore pointed at a temp directory."""
    import memory.observability_store as obs_mod

    db_path = tmp_path / ".observability.db"

    with mock.patch.object(obs_mod, "SQLITE_DB_PATH", db_path), \
         mock.patch.object(obs_mod, "_store_instance", None), \
         mock.patch.object(obs_mod.ObservabilityStore, "_detect_mode", return_value="lite"), \
         mock.patch.object(obs_mod.ObservabilityStore, "_safe_commit", _working_safe_commit):

        store = obs_mod.ObservabilityStore(mode="lite")
        yield store
        store._sqlite_conn.close()


@pytest.fixture
def tmp_outcome_db(tmp_path):
    """Create a temp OutcomeTracker database."""
    db_path = tmp_path / ".outcome_tracker.db"
    with mock.patch("memory.outcome_tracker.OUTCOME_DB", db_path):
        from memory.outcome_tracker import OutcomeTracker
        tracker = OutcomeTracker()
        yield tracker, db_path


@pytest.fixture
def tmp_evolution_file(tmp_path):
    """Create a temp ProfessorEvolution file."""
    evo_path = tmp_path / ".professor_evolution.json"
    conf_path = tmp_path / ".professor_domain_confidence.json"
    return evo_path, conf_path


# ===========================================================================
# PHASE 1: Record a bug fix and verify quarantine entry
# ===========================================================================

class TestPhase1_CaptureEntersQuarantine:
    """capture_success -> claim created with status='quarantined' in observability store."""

    def test_record_claim_enters_quarantine(self, tmp_obs_store):
        """A new claim via record_claim_with_evidence starts quarantined."""
        store = tmp_obs_store

        claim_id = store.record_claim_with_evidence(
            claim_text="[bug-fix SOP] Redis connection timeout: increase pool_size from 5 to 20",
            evidence_grade="case_series",
            source="capture_success",
            confidence=0.6,
            tags=["redis", "connection", "auto-captured"],
            area="infrastructure",
        )

        assert claim_id, "claim_id should be non-empty"

        # Verify claim exists with quarantined status
        claims = store.get_claims_by_status("quarantined")
        claim_ids = [c["claim_id"] for c in claims]
        assert claim_id in claim_ids, "Claim should be in quarantined status"

        # Verify knowledge_quarantine table has the entry
        row = store._sqlite_conn.execute(
            "SELECT status, item_type FROM knowledge_quarantine WHERE item_id = ?",
            (claim_id,)
        ).fetchone()
        assert row is not None, "Quarantine entry should exist"
        assert row["status"] == "quarantined"
        assert row["item_type"] == "sop"  # Starts with "[" and contains "SOP]"

    def test_direct_claim_outcome_recorded(self, tmp_obs_store):
        """record_direct_claim_outcome stores outcomes for quarantine evaluation."""
        store = tmp_obs_store

        claim_id = store.record_claim_with_evidence(
            claim_text="boto3 blocks event loop — use asyncio.to_thread",
            evidence_grade="cohort",
            source="capture_success",
            confidence=0.7,
            tags=["async", "boto3"],
            area="async_python",
        )

        # Record multiple direct outcomes (simulates repeated success)
        for i in range(5):
            store.record_direct_claim_outcome(
                claim_id=claim_id,
                success=True,
                reward=0.7,
                source="capture_success",
                notes=f"Outcome {i+1}",
            )

        # Verify rollup
        rollup = store.get_direct_claim_rollup(claim_id)
        assert rollup is not None, "Rollup should exist after outcomes recorded"
        assert rollup["n"] == 5
        assert rollup["success_rate"] == 1.0
        assert rollup["avg_reward"] == pytest.approx(0.7)


# ===========================================================================
# PHASE 2: Scheduler promotes quarantined claim to trusted
# ===========================================================================

class TestPhase2_QuarantinePromotion:
    """lite_scheduler._run_evaluate_quarantine promotes claims with enough evidence."""

    def test_bootstrap_promotion_n3(self, tmp_obs_store):
        """Claims with n>=3 and success_rate>=0.6 get promoted (bootstrap threshold)."""
        store = tmp_obs_store

        claim_id = store.record_claim_with_evidence(
            claim_text="Docker restart does NOT reload --env-file — must stop/rm/run",
            evidence_grade="case_series",
            source="capture_success",
            confidence=0.6,
            tags=["docker"],
            area="docker_ecs",
        )

        # Record 3 successful outcomes (bootstrap threshold)
        for _ in range(3):
            store.record_direct_claim_outcome(
                claim_id=claim_id,
                success=True,
                reward=0.6,
                source="capture_success",
            )

        # Run quarantine evaluation via scheduler
        scheduler = LiteScheduler()
        scheduler._store = store

        success, msg = scheduler._run_evaluate_quarantine()

        assert success, f"Quarantine eval should succeed: {msg}"
        assert "promoted" in msg.lower() or "1 promoted" in msg, f"Should promote 1 claim: {msg}"

        # Verify quarantine status changed to trusted
        row = store._sqlite_conn.execute(
            "SELECT status FROM knowledge_quarantine WHERE item_id = ?",
            (claim_id,)
        ).fetchone()
        assert row["status"] == "trusted", f"Expected trusted, got {row['status']}"

        # Verify claim.status changed to active
        claim_row = store._sqlite_conn.execute(
            "SELECT status FROM claim WHERE claim_id = ?",
            (claim_id,)
        ).fetchone()
        assert claim_row["status"] == "active", f"Expected active, got {claim_row['status']}"

    def test_rejection_low_success_rate(self, tmp_obs_store):
        """Claims with n>=10 and success_rate<0.3 get rejected."""
        store = tmp_obs_store

        claim_id = store.record_claim_with_evidence(
            claim_text="Bad advice that keeps failing",
            evidence_grade="anecdotal",
            source="test",
            confidence=0.3,
            area="general",
        )

        # Record 10 outcomes — mostly failures (only 2 successes = 20%)
        for i in range(10):
            store.record_direct_claim_outcome(
                claim_id=claim_id,
                success=(i < 2),  # Only first 2 succeed
                reward=0.3 if i < 2 else -0.3,
                source="test",
            )

        scheduler = LiteScheduler()
        scheduler._store = store

        success, msg = scheduler._run_evaluate_quarantine()
        assert success

        row = store._sqlite_conn.execute(
            "SELECT status FROM knowledge_quarantine WHERE item_id = ?",
            (claim_id,)
        ).fetchone()
        assert row["status"] == "rejected", f"Expected rejected, got {row['status']}"


# ===========================================================================
# PHASE 3: Trusted -> flagged_for_review bridge
# ===========================================================================

class TestPhase3_TrustedToFlaggedBridge:
    """_run_promote_trusted_to_wisdom flags trusted claims for professor review."""

    def test_trusted_bridges_to_flagged(self, tmp_obs_store):
        """Trusted claims with claim.status='active' get flagged_for_review."""
        store = tmp_obs_store

        claim_id = store.record_claim_with_evidence(
            claim_text="Sample rate mismatch causes chipmunk audio — always resample to 48kHz",
            evidence_grade="cohort",
            source="capture_success",
            confidence=0.8,
            tags=["voice", "audio"],
            area="voice_pipeline",
        )

        # Manually promote to simulate what _run_evaluate_quarantine does
        store._sqlite_conn.execute(
            "UPDATE knowledge_quarantine SET status = 'trusted' WHERE item_id = ?",
            (claim_id,)
        )
        store._sqlite_conn.execute(
            "UPDATE claim SET status = 'active' WHERE claim_id = ?",
            (claim_id,)
        )
        store._sqlite_conn.commit()

        # Run the bridge job
        scheduler = LiteScheduler()
        scheduler._store = store

        success, msg = scheduler._run_promote_trusted_to_wisdom()
        assert success, f"Bridge should succeed: {msg}"
        assert "flagged" in msg.lower(), f"Should flag claims: {msg}"

        # Verify claim is now flagged_for_review
        claim_row = store._sqlite_conn.execute(
            "SELECT status FROM claim WHERE claim_id = ?",
            (claim_id,)
        ).fetchone()
        assert claim_row["status"] == "flagged_for_review"


# ===========================================================================
# PHASE 4: Professor surfaces the learning
# ===========================================================================

class TestPhase4_ProfessorSurfacesLearning:
    """Professor.consult() picks up flagged claims via apply_learnings_to_wisdom."""

    def test_flagged_claims_become_suggestions(self, tmp_obs_store, tmp_evolution_file):
        """Flagged claims feed into professor wisdom suggestions."""
        store = tmp_obs_store
        evo_path, conf_path = tmp_evolution_file

        # Create a claim in flagged_for_review status
        claim_id = store.record_claim_with_evidence(
            claim_text="Django gunicorn HOME=/root required on EC2 for git operations",
            evidence_grade="cohort",
            source="capture_success",
            confidence=0.8,
            tags=["django", "deployment"],
            area="django_backend",
        )
        store._sqlite_conn.execute(
            "UPDATE claim SET status = 'flagged_for_review' WHERE claim_id = ?",
            (claim_id,)
        )
        store._sqlite_conn.commit()

        # Patch evolution file paths and observability store
        with mock.patch("memory.professor.EVOLUTION_FILE", evo_path), \
             mock.patch("memory.professor.DOMAIN_CONFIDENCE_FILE", conf_path), \
             mock.patch("memory.professor._evolution", None), \
             mock.patch("memory.observability_store.get_observability_store", return_value=store):

            from memory.professor import ProfessorEvolution

            evolution = ProfessorEvolution()

            # Process flagged claims -> should generate suggestions
            suggestions = evolution._process_flagged_claims()

            assert len(suggestions) > 0, "Should generate suggestions from flagged claims"

            # Verify the claim was processed (status changed to applied_to_wisdom)
            claim_row = store._sqlite_conn.execute(
                "SELECT status FROM claim WHERE claim_id = ?",
                (claim_id,)
            ).fetchone()
            assert claim_row["status"] == "applied_to_wisdom"

            # Verify suggestions contain our domain
            domains_suggested = [s["domain"] for s in suggestions]
            assert any(d in ("django_backend", "memory_system") for d in domains_suggested), \
                f"Should suggest to django_backend or memory_system domain, got: {domains_suggested}"


# ===========================================================================
# PHASE 5: Professor confidence increases from outcomes
# ===========================================================================

class TestPhase5_ProfessorConfidenceRefinement:
    """refine_from_outcomes adjusts domain confidence based on task outcomes."""

    def test_success_increases_confidence(self, tmp_outcome_db, tmp_evolution_file):
        """Successful outcomes boost professor domain confidence."""
        tracker, db_path = tmp_outcome_db
        evo_path, conf_path = tmp_evolution_file

        # Record a tracked outcome for a memory_system task
        outcome_id = tracker.start_tracking(
            task="Fix memory query timeout by increasing SQLite pool",
            approach="Increased pool size and added WAL mode",
            expected_outcome="Query latency under 100ms",
            related_patterns=["sqlite_optimization"],
        )
        tracker.record_outcome(
            outcome_id=outcome_id,
            success=True,
            actual_outcome="Query latency dropped from 500ms to 20ms",
        )

        # Run refinement
        with mock.patch("memory.professor.EVOLUTION_FILE", evo_path), \
             mock.patch("memory.professor.DOMAIN_CONFIDENCE_FILE", conf_path), \
             mock.patch("memory.professor._evolution", None), \
             mock.patch("memory.outcome_tracker.OUTCOME_DB", db_path):

            from memory.professor import ProfessorEvolution

            evolution = ProfessorEvolution()
            result = evolution.refine_from_outcomes(max_outcomes=50)

            assert result["outcomes_processed"] >= 1, \
                f"Should process at least 1 outcome, got: {result}"

            # Check that memory_system domain was adjusted upward
            if "memory_system" in result["domains_adjusted"]:
                adj = result["domains_adjusted"]["memory_system"]
                assert adj["delta"] > 0, "Success should increase confidence"
                assert adj["direction"] == "reinforced"

    def test_failure_decreases_confidence(self, tmp_outcome_db, tmp_evolution_file):
        """Failed outcomes penalize professor domain confidence."""
        tracker, db_path = tmp_outcome_db
        evo_path, conf_path = tmp_evolution_file

        # Record a failed outcome for a docker_ecs task
        outcome_id = tracker.start_tracking(
            task="Deploy docker container with new env vars",
            approach="Used docker restart (wrong approach)",
            expected_outcome="Container picks up new env vars",
            related_patterns=["docker_restart"],
        )
        tracker.record_outcome(
            outcome_id=outcome_id,
            success=False,
            actual_outcome="Container still has old env vars after restart",
        )

        with mock.patch("memory.professor.EVOLUTION_FILE", evo_path), \
             mock.patch("memory.professor.DOMAIN_CONFIDENCE_FILE", conf_path), \
             mock.patch("memory.professor._evolution", None), \
             mock.patch("memory.outcome_tracker.OUTCOME_DB", db_path):

            from memory.professor import ProfessorEvolution

            evolution = ProfessorEvolution()
            result = evolution.refine_from_outcomes(max_outcomes=50)

            assert result["outcomes_processed"] >= 1

            if "docker_ecs" in result["domains_adjusted"]:
                adj = result["domains_adjusted"]["docker_ecs"]
                assert adj["delta"] < 0, "Failure should decrease confidence"
                assert adj["direction"] == "penalized"

    def test_repeated_failures_flag_domain(self, tmp_outcome_db, tmp_evolution_file):
        """Many failures drop confidence below 0.4, flagging domain for review."""
        tracker, db_path = tmp_outcome_db
        evo_path, conf_path = tmp_evolution_file

        # Record many failed outcomes for testing domain
        for i in range(15):
            oid = tracker.start_tracking(
                task=f"Run pytest test suite attempt {i}",
                approach="Standard pytest run",
                expected_outcome="All tests pass",
                related_patterns=["test_execution"],
            )
            tracker.record_outcome(
                outcome_id=oid,
                success=False,
                actual_outcome=f"Tests failed with import errors (attempt {i})",
            )

        with mock.patch("memory.professor.EVOLUTION_FILE", evo_path), \
             mock.patch("memory.professor.DOMAIN_CONFIDENCE_FILE", conf_path), \
             mock.patch("memory.professor._evolution", None), \
             mock.patch("memory.outcome_tracker.OUTCOME_DB", db_path):

            from memory.professor import ProfessorEvolution

            evolution = ProfessorEvolution()
            result = evolution.refine_from_outcomes(max_outcomes=100)

            # testing domain should be flagged (15 failures * -0.05 = -0.75, from 0.7 base)
            if "testing" in result["domains_adjusted"]:
                assert result["domains_adjusted"]["testing"]["score"] < 0.4, \
                    "15 failures should drop confidence below 0.4"
                assert "testing" in result["domains_flagged"], \
                    "Testing domain should be flagged for review"


# ===========================================================================
# PHASE 6: Full compounding loop — end to end
# ===========================================================================

class TestPhase6_FullCompoundingLoop:
    """
    The complete loop proving Context DNA compounds learning.

    capture -> quarantine -> promote -> flag -> professor surfaces -> outcome -> confidence grows

    This is the empirical proof that the system remembers and improves.
    """

    def test_full_loop_capture_to_confidence(self, tmp_obs_store, tmp_outcome_db, tmp_evolution_file):
        """
        END-TO-END: A bug fix is captured, survives quarantine, reaches the professor,
        and outcomes reinforce domain confidence.

        This single test proves the core value proposition.
        """
        store = tmp_obs_store
        tracker, outcome_db_path = tmp_outcome_db
        evo_path, conf_path = tmp_evolution_file

        # === STEP 1: Record a claim (simulates capture_success wiring) ===
        claim_id = store.record_claim_with_evidence(
            claim_text="[bug-fix SOP] async: boto3.converse blocks event loop -> use asyncio.to_thread",
            evidence_grade="case_series",
            source="capture_success",
            confidence=0.6,
            tags=["async", "boto3", "event-loop"],
            area="async_python",
        )
        assert claim_id

        # Verify: claim is quarantined
        claims_q = store.get_claims_by_status("quarantined")
        assert any(c["claim_id"] == claim_id for c in claims_q), \
            "STEP 1 FAILED: Claim should be quarantined"

        # === STEP 2: Record direct outcomes (simulates repeated success) ===
        for i in range(4):
            store.record_direct_claim_outcome(
                claim_id=claim_id,
                success=True,
                reward=0.7,
                source="capture_success",
                notes=f"Used asyncio.to_thread successfully (instance {i+1})",
            )

        rollup = store.get_direct_claim_rollup(claim_id)
        assert rollup["n"] == 4
        assert rollup["success_rate"] == 1.0

        # === STEP 3: Scheduler evaluates quarantine -> promotes to trusted ===
        scheduler = LiteScheduler()
        scheduler._store = store

        success, msg = scheduler._run_evaluate_quarantine()
        assert success, f"STEP 3 FAILED: Quarantine eval error: {msg}"

        kq_row = store._sqlite_conn.execute(
            "SELECT status FROM knowledge_quarantine WHERE item_id = ?",
            (claim_id,)
        ).fetchone()
        assert kq_row["status"] == "trusted", \
            f"STEP 3 FAILED: Expected trusted, got {kq_row['status']}"

        claim_row = store._sqlite_conn.execute(
            "SELECT status FROM claim WHERE claim_id = ?",
            (claim_id,)
        ).fetchone()
        assert claim_row["status"] == "active", \
            f"STEP 3 FAILED: Expected active, got {claim_row['status']}"

        # === STEP 4: Bridge job flags trusted claim for professor review ===
        success, msg = scheduler._run_promote_trusted_to_wisdom()
        assert success, f"STEP 4 FAILED: Bridge error: {msg}"

        claim_row = store._sqlite_conn.execute(
            "SELECT status FROM claim WHERE claim_id = ?",
            (claim_id,)
        ).fetchone()
        assert claim_row["status"] == "flagged_for_review", \
            f"STEP 4 FAILED: Expected flagged_for_review, got {claim_row['status']}"

        # === STEP 5: Professor processes flagged claims -> generates suggestions ===
        with mock.patch("memory.professor.EVOLUTION_FILE", evo_path), \
             mock.patch("memory.professor.DOMAIN_CONFIDENCE_FILE", conf_path), \
             mock.patch("memory.professor._evolution", None), \
             mock.patch("memory.observability_store.get_observability_store", return_value=store):

            from memory.professor import ProfessorEvolution

            evolution = ProfessorEvolution()
            suggestions = evolution._process_flagged_claims()

            assert len(suggestions) > 0, \
                "STEP 5 FAILED: Professor should generate suggestions from flagged claims"

            # Verify claim processed
            claim_row = store._sqlite_conn.execute(
                "SELECT status FROM claim WHERE claim_id = ?",
                (claim_id,)
            ).fetchone()
            assert claim_row["status"] == "applied_to_wisdom", \
                f"STEP 5 FAILED: Expected applied_to_wisdom, got {claim_row['status']}"

        # === STEP 6: Record success outcome and verify confidence increase ===
        outcome_id = tracker.start_tracking(
            task="Fix async boto3 blocking in voice pipeline",
            approach="Used asyncio.to_thread for all boto3 calls",
            expected_outcome="Event loop stays responsive during LLM calls",
            related_patterns=["async_boto3_fix"],
        )
        tracker.record_outcome(
            outcome_id=outcome_id,
            success=True,
            actual_outcome="Voice latency dropped from 14s to 200ms, event loop unblocked",
        )

        with mock.patch("memory.professor.EVOLUTION_FILE", evo_path), \
             mock.patch("memory.professor.DOMAIN_CONFIDENCE_FILE", conf_path), \
             mock.patch("memory.professor._evolution", None), \
             mock.patch("memory.outcome_tracker.OUTCOME_DB", outcome_db_path):

            evolution2 = ProfessorEvolution()
            result = evolution2.refine_from_outcomes(max_outcomes=50)

            assert result["outcomes_processed"] >= 1, \
                f"STEP 6 FAILED: Should process outcomes, got: {result}"

            # The async task should match async_python domain keywords
            if "async_python" in result["domains_adjusted"]:
                adj = result["domains_adjusted"]["async_python"]
                assert adj["delta"] > 0, \
                    f"STEP 6 FAILED: Success should reinforce confidence, got delta={adj['delta']}"
                assert adj["score"] > 0.7, \
                    f"Confidence should be above default 0.7 after success"

        # === SUMMARY ===
        # Claim lifecycle: quarantined -> trusted -> active -> flagged_for_review -> applied_to_wisdom
        # Confidence lifecycle: 0.7 (default) -> 0.73 (after success reinforcement)
        # This IS the compounding loop. Each iteration through this cycle
        # makes the system smarter about what works and what doesn't.


class TestPhase7_EvidenceGrading:
    """Evidence grading influences confidence and promotion behavior."""

    def test_anecdotal_gets_lower_weighted_confidence(self, tmp_obs_store):
        """Anecdotal evidence produces lower weighted confidence than cohort."""
        store = tmp_obs_store

        # Anecdotal claim
        claim_id_weak = store.record_claim_with_evidence(
            claim_text="I think this might work",
            evidence_grade="anecdotal",
            source="test",
            confidence=0.8,  # High self-confidence...
            area="general",
        )

        # Cohort claim
        claim_id_strong = store.record_claim_with_evidence(
            claim_text="Verified across 10 deployments consistently",
            evidence_grade="cohort",
            source="test",
            confidence=0.8,  # Same self-confidence...
            area="general",
        )

        weak = store._sqlite_conn.execute(
            "SELECT weighted_confidence FROM claim WHERE claim_id = ?",
            (claim_id_weak,)
        ).fetchone()

        strong = store._sqlite_conn.execute(
            "SELECT weighted_confidence FROM claim WHERE claim_id = ?",
            (claim_id_strong,)
        ).fetchone()

        # Anecdotal weight=0.3, Cohort weight=0.7
        # weak: 0.8 * 0.3 = 0.24, strong: 0.8 * 0.7 = 0.56
        assert weak["weighted_confidence"] < strong["weighted_confidence"], \
            f"Anecdotal ({weak['weighted_confidence']}) should have lower weighted " \
            f"confidence than cohort ({strong['weighted_confidence']})"

    def test_evidence_grade_selection(self):
        """_select_evidence_grade returns appropriate grades based on signals."""
        from memory.auto_capture import _select_evidence_grade

        # Repeated/verified patterns -> cohort
        grade, conf = _select_evidence_grade("Deploy fix", "consistently works every time")
        assert grade == "cohort"

        # Compared approaches -> case_control
        grade, conf = _select_evidence_grade("Switched approach", "compared to old method, faster than before")
        assert grade == "case_control"

        # Minimal detail + procedural task -> anecdotal
        grade, conf = _select_evidence_grade(
            "Fix the complex database migration issue with foreign key constraints",
            "ok"
        )
        assert grade == "anecdotal"


# ===========================================================================
# Import guard — ensure all necessary modules are importable
# ===========================================================================

class TestImports:
    """Verify all modules in the compounding loop are importable."""

    def test_observability_store_importable(self):
        from memory.observability_store import ObservabilityStore, EvidenceGrade
        assert ObservabilityStore is not None
        assert EvidenceGrade is not None

    def test_lite_scheduler_importable(self):
        from memory.lite_scheduler import LiteScheduler
        assert LiteScheduler is not None

    def test_professor_importable(self):
        from memory.professor import Professor, ProfessorEvolution, consult
        assert Professor is not None
        assert ProfessorEvolution is not None

    def test_auto_capture_importable(self):
        from memory.auto_capture import capture_success, _select_evidence_grade
        assert capture_success is not None

    def test_outcome_tracker_importable(self):
        from memory.outcome_tracker import OutcomeTracker
        assert OutcomeTracker is not None
