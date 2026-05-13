#!/usr/bin/env python3
"""Tests for tools/trialbench_batch.py — stdlib only (unittest)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Make sibling import work regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from trialbench_batch import _describe, _discover_trial_ids, _quantile, run_batch  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_scored_outcome(trial_dir: Path, run_id: str, arm: str, task_id: str,
                           node_id: str, success: bool) -> None:
    trial_dir.mkdir(parents=True, exist_ok=True)
    # Write stub run_*.json so discover picks it up
    run_path = trial_dir / f"run_{run_id}.json"
    run_path.write_text(json.dumps({"run_id": run_id, "arm": arm}))
    # Write/append to scored_outcomes.json
    so_path = trial_dir / "scored_outcomes.json"
    existing: list = []
    if so_path.exists():
        existing = json.loads(so_path.read_text())
    existing.append({
        "run_id": run_id,
        "arm": arm,
        "task_id": task_id,
        "node_id": node_id,
        "primary_endpoint_success": success,
    })
    so_path.write_text(json.dumps(existing))


# ---------------------------------------------------------------------------
# Unit tests: stats helpers
# ---------------------------------------------------------------------------


class QuantileTests(unittest.TestCase):
    def test_median_even(self):
        self.assertAlmostEqual(_quantile([1.0, 2.0, 3.0, 4.0], 0.5), 2.5)

    def test_median_odd(self):
        self.assertAlmostEqual(_quantile([1.0, 2.0, 3.0], 0.5), 2.0)

    def test_p25(self):
        # [0.0, 0.0, 1.0, 1.0]: pos = 0.25 * 3 = 0.75 → interpolate between idx0(0.0) and idx1(0.0) = 0.0
        vals = [0.0, 0.0, 1.0, 1.0]
        self.assertAlmostEqual(_quantile(vals, 0.25), 0.0)
        # Verify p25 is distinct from p75 for a more varied dataset
        vals2 = [0.0, 1.0, 2.0, 3.0, 4.0]
        self.assertLess(_quantile(vals2, 0.25), _quantile(vals2, 0.75))

    def test_single_element(self):
        self.assertAlmostEqual(_quantile([0.7], 0.5), 0.7)

    def test_empty_returns_nan(self):
        import math
        result = _quantile([], 0.5)
        self.assertTrue(math.isnan(result))


class DescribeTests(unittest.TestCase):
    def test_empty(self):
        d = _describe([])
        self.assertEqual(d["n"], 0)
        self.assertIsNone(d["mean"])

    def test_all_successes(self):
        d = _describe([1.0, 1.0, 1.0, 1.0])
        self.assertEqual(d["n"], 4)
        self.assertAlmostEqual(d["mean"], 1.0)
        self.assertAlmostEqual(d["p25"], 1.0)
        self.assertAlmostEqual(d["p75"], 1.0)

    def test_mixed(self):
        d = _describe([0.0, 0.0, 1.0, 1.0])
        self.assertEqual(d["n"], 4)
        self.assertAlmostEqual(d["mean"], 0.5)
        self.assertAlmostEqual(d["median"], 0.5)


# ---------------------------------------------------------------------------
# Unit tests: discover
# ---------------------------------------------------------------------------


class DiscoverTests(unittest.TestCase):
    def test_discovers_dirs_with_run_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t1 = root / "trial_001"
            t1.mkdir()
            (t1 / "run_000.json").write_text("{}")
            t2 = root / "trial_002"
            t2.mkdir()
            # no run files — should be excluded
            ids = _discover_trial_ids(root)
        self.assertEqual(ids, ["trial_001"])

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ids = _discover_trial_ids(Path(tmp))
        self.assertEqual(ids, [])

    def test_nonexistent_dir_returns_empty(self):
        ids = _discover_trial_ids(Path("/nonexistent_xyz_abc"))
        self.assertEqual(ids, [])


# ---------------------------------------------------------------------------
# Integration tests: run_batch
# ---------------------------------------------------------------------------


class RunBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.artifacts = self.root / "artifacts" / "trialbench"
        self.reports = self.root / "reports" / "trialbench"

    def tearDown(self):
        self.tmp.cleanup()

    def _make_trial(self, trial_id: str, runs: list[dict]) -> None:
        td = self.artifacts / trial_id
        for r in runs:
            _write_scored_outcome(
                td, r["run_id"], r["arm"], r["task_id"], r["node_id"], r["success"]
            )

    def test_no_trials_returns_error(self):
        cohort = run_batch(artifacts_dir=self.artifacts, reports_dir=self.reports)
        self.assertEqual(cohort["trial_count"], 0)
        self.assertTrue(any("no trial" in e for e in cohort["errors"]))

    def test_single_trial_success(self):
        self._make_trial("t1", [
            {"run_id": "0", "arm": "A_raw", "task_id": "ecg_1", "node_id": "mac1", "success": True},
            {"run_id": "1", "arm": "A_raw", "task_id": "ecg_1", "node_id": "mac2", "success": False},
            {"run_id": "2", "arm": "C_contextdna_packet", "task_id": "ecg_1", "node_id": "mac1", "success": True},
        ])
        cohort = run_batch(artifacts_dir=self.artifacts, reports_dir=self.reports)
        self.assertEqual(cohort["trial_count"], 1)
        self.assertEqual(cohort["run_count"], 3)
        self.assertIn("ecg_1", cohort["per_case"])
        self.assertIn("mac1", cohort["per_learner"])
        # mac1: 2 runs, 2 successes
        self.assertEqual(cohort["per_learner"]["mac1"]["n_runs"], 2)
        self.assertEqual(cohort["per_learner"]["mac1"]["n_successes"], 2)
        # mac2: 1 run, 0 successes
        self.assertEqual(cohort["per_learner"]["mac2"]["n_runs"], 1)
        self.assertEqual(cohort["per_learner"]["mac2"]["n_successes"], 0)

    def test_per_case_has_all_arms_key(self):
        self._make_trial("t1", [
            {"run_id": "0", "arm": "A_raw", "task_id": "ecg_1", "node_id": "n1", "success": True},
            {"run_id": "1", "arm": "C_contextdna_packet", "task_id": "ecg_1", "node_id": "n1", "success": False},
        ])
        cohort = run_batch(artifacts_dir=self.artifacts, reports_dir=self.reports)
        task_stats = cohort["per_case"]["ecg_1"]
        self.assertIn("all_arms", task_stats)
        self.assertEqual(task_stats["all_arms"]["n"], 2)
        self.assertAlmostEqual(task_stats["all_arms"]["mean"], 0.5)

    def test_multi_trial_aggregation(self):
        self._make_trial("t1", [
            {"run_id": "0", "arm": "A_raw", "task_id": "task_x", "node_id": "n1", "success": True},
        ])
        self._make_trial("t2", [
            {"run_id": "0", "arm": "A_raw", "task_id": "task_x", "node_id": "n1", "success": False},
        ])
        cohort = run_batch(artifacts_dir=self.artifacts, reports_dir=self.reports)
        self.assertEqual(cohort["trial_count"], 2)
        self.assertEqual(cohort["run_count"], 2)
        arm_stat = cohort["per_arm"]["A_raw"]
        self.assertEqual(arm_stat["n"], 2)
        self.assertAlmostEqual(arm_stat["mean"], 0.5)

    def test_missing_scored_outcomes_logged_not_crashed(self):
        # Trial dir exists + run JSON exists, but no scored_outcomes.json
        td = self.artifacts / "bare_trial"
        td.mkdir(parents=True)
        (td / "run_000.json").write_text("{}")
        cohort = run_batch(artifacts_dir=self.artifacts, reports_dir=self.reports)
        # Should still return without crashing; error recorded
        self.assertTrue(any("scored_outcomes.json" in e for e in cohort["errors"]))

    def test_writes_json_and_md(self):
        self._make_trial("t1", [
            {"run_id": "0", "arm": "A_raw", "task_id": "t1", "node_id": "n1", "success": True},
        ])
        cohort = run_batch(artifacts_dir=self.artifacts, reports_dir=self.reports)
        json_path = Path(cohort["cohort_report_json_path"])
        md_path = Path(cohort["cohort_report_md_path"])
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())
        md_text = md_path.read_text()
        self.assertIn("TrialBench Cohort Report", md_text)

    def test_explicit_trial_ids_filter(self):
        self._make_trial("t1", [
            {"run_id": "0", "arm": "A_raw", "task_id": "tk", "node_id": "n1", "success": True},
        ])
        self._make_trial("t2", [
            {"run_id": "0", "arm": "A_raw", "task_id": "tk", "node_id": "n1", "success": False},
        ])
        cohort = run_batch(
            artifacts_dir=self.artifacts,
            reports_dir=self.reports,
            trial_ids=["t1"],
        )
        self.assertEqual(cohort["trial_count"], 1)
        self.assertEqual(cohort["run_count"], 1)

    def test_success_rate_zero_runs_trial(self):
        """Trial with scored_outcomes.json = [] (empty) shouldn't crash."""
        td = self.artifacts / "empty_trial"
        td.mkdir(parents=True)
        (td / "run_000.json").write_text("{}")
        (td / "scored_outcomes.json").write_text("[]")
        cohort = run_batch(artifacts_dir=self.artifacts, reports_dir=self.reports)
        ts = next(t for t in cohort["trial_summaries"] if t["trial_id"] == "empty_trial")
        self.assertEqual(ts["n_runs"], 0)
        self.assertIsNone(ts["success_rate"])


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


class CLISmokeTest(unittest.TestCase):
    def test_help_exits_zero(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "trialbench_batch.py"), "--help"],
            capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("cohort", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
