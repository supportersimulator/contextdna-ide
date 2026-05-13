#!/usr/bin/env python3
"""Tests for tools/trialbench_score.py — stdlib only (unittest)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Make sibling import work regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from trialbench_score import (  # noqa: E402
    ARM_A,
    ARM_B,
    ARM_C,
    CAVEAT_BLOCK_VERBATIM,
    PRIMARY_ENDPOINT_VERBATIM,
    bootstrap_diff,
    score_trial,
)


def _synthetic_run(run_id: str, arm: str, success: bool, *, synthetic_reviewer: bool = False) -> dict:
    return {
        "run_id": run_id,
        "trial_id": "test_trial",
        "arm": arm,
        "task_id": "task_1",
        "node_id": "node_alpha",
        "task_complete": success,
        "tests_pass": success,
        "no_invariant_violation": True,
        "blinded_reviewer_approved": None if synthetic_reviewer else success,
        "tool_calls": [
            {"name": "grep", "useful": True, "category": "useful"},
            {"name": "sed", "useful": False, "category": "redundant"},
        ],
        "outcome_content": "used grep to inspect the file",
        "claimed_useful_tools": ["grep"],
        "timed_out": False,
        "protocol_deviation": None,
        "adverse_events": [],
    }


class BootstrapDiffTests(unittest.TestCase):
    def test_deterministic_seed(self):
        a = [False] * 10
        c = [True] * 8 + [False] * 2  # 80% success
        lo1, hi1 = bootstrap_diff(a, c, n=2000, seed=42)
        lo2, hi2 = bootstrap_diff(a, c, n=2000, seed=42)
        self.assertEqual((lo1, hi1), (lo2, hi2))
        # diff (C - A) should be strongly positive — entire CI well above 0
        self.assertGreater(lo1, 0.4)
        self.assertLessEqual(hi1, 1.0)

    def test_empty_inputs_return_zero(self):
        self.assertEqual(bootstrap_diff([], [True], n=100, seed=0), (0.0, 0.0))
        self.assertEqual(bootstrap_diff([True], [], n=100, seed=0), (0.0, 0.0))

    def test_known_bounds_for_equal_arms(self):
        # Equal arms (50/50 each) — CI should straddle zero
        a = [True] * 5 + [False] * 5
        c = [True] * 5 + [False] * 5
        lo, hi = bootstrap_diff(a, c, n=5000, seed=42)
        self.assertLessEqual(lo, 0.0)
        self.assertGreaterEqual(hi, 0.0)


class ScoreTrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.artifacts = self.root / "artifacts" / "trialbench"
        self.reports = self.root / "reports" / "trialbench"
        self.trial_id = "synthetic_trial_v0"
        self.trial_dir = self.artifacts / self.trial_id
        self.trial_dir.mkdir(parents=True)
        # 10 Arm A runs (2 success, 8 fail) + 10 Arm C runs (8 success, 2 fail)
        for i in range(10):
            run = _synthetic_run(f"a_{i}", ARM_A, success=(i < 2))
            (self.trial_dir / f"run_a_{i}.json").write_text(json.dumps(run))
        for i in range(10):
            run = _synthetic_run(f"c_{i}", ARM_C, success=(i < 8))
            (self.trial_dir / f"run_c_{i}.json").write_text(json.dumps(run))

    def tearDown(self):
        self.tmp.cleanup()

    def test_end_to_end_known_rates(self):
        result = score_trial(
            self.trial_id, artifacts_dir=self.artifacts, reports_dir=self.reports
        )
        self.assertEqual(result["n_runs"], 20)
        self.assertAlmostEqual(result["arm_a_success_rate"], 0.2, places=6)
        self.assertAlmostEqual(result["arm_c_success_rate"], 0.8, places=6)
        self.assertAlmostEqual(result["diff"], 0.6, places=6)
        # CI should be entirely above zero with this strong effect
        self.assertGreater(result["ci_low"], 0.1)
        self.assertLess(result["ci_high"], 1.0)
        self.assertEqual(result["primary_endpoint_winner"], ARM_C)
        # Outputs created
        self.assertTrue(Path(result["scored_outcomes_path"]).exists())
        self.assertTrue(Path(result["itt_summary_path"]).exists())
        self.assertTrue(Path(result["manuscript_path"]).exists())

    def test_manuscript_contains_caveat_and_endpoint(self):
        score_trial(self.trial_id, artifacts_dir=self.artifacts, reports_dir=self.reports)
        manuscript = (self.reports / self.trial_id / "manuscript.md").read_text()
        self.assertIn(CAVEAT_BLOCK_VERBATIM, manuscript)
        self.assertIn(PRIMARY_ENDPOINT_VERBATIM, manuscript)
        # Honest claims — must NOT contain forbidden language
        self.assertNotIn("FDA-grade", manuscript.replace(CAVEAT_BLOCK_VERBATIM, ""))
        self.assertIn("Trial ID", manuscript)
        self.assertIn("Sensitivity Analyses", manuscript)
        self.assertIn("Future Work", manuscript)

    def test_synthetic_reviewer_flag_propagates(self):
        # Replace one run with a None-reviewer (synthetic) variant
        (self.trial_dir / "run_a_0.json").write_text(
            json.dumps(_synthetic_run("a_0", ARM_A, success=True, synthetic_reviewer=True))
        )
        result = score_trial(
            self.trial_id, artifacts_dir=self.artifacts, reports_dir=self.reports
        )
        self.assertTrue(result["synthetic_reviewer_used"])
        manuscript = Path(result["manuscript_path"]).read_text()
        self.assertIn("synthetic-reviewer fallback", manuscript)

    def test_zero_silent_failures_on_bad_json(self):
        # Drop an invalid run file; loader should record an error, not crash
        (self.trial_dir / "run_bad.json").write_text("{not json")
        result = score_trial(
            self.trial_id, artifacts_dir=self.artifacts, reports_dir=self.reports
        )
        self.assertTrue(any("run_bad.json" in e for e in result["errors"]))
        # Other runs still scored
        self.assertEqual(result["n_runs"], 20)

    def test_missing_trial_dir_recorded(self):
        result = score_trial(
            "nonexistent_trial",
            artifacts_dir=self.artifacts,
            reports_dir=self.reports,
        )
        self.assertEqual(result["n_runs"], 0)
        self.assertTrue(any("missing" in e for e in result["errors"]))

    def test_itt_summary_structure(self):
        result = score_trial(
            self.trial_id, artifacts_dir=self.artifacts, reports_dir=self.reports
        )
        with open(result["itt_summary_path"]) as fh:
            itt = json.load(fh)
        self.assertEqual(itt["trial_id"], self.trial_id)
        self.assertIn(ARM_A, itt["arm_stats"])
        self.assertIn(ARM_B, itt["arm_stats"])
        self.assertIn(ARM_C, itt["arm_stats"])
        self.assertIn("per_protocol", itt["sensitivity"])
        self.assertIn("tests_only", itt["sensitivity"])


if __name__ == "__main__":
    unittest.main()
