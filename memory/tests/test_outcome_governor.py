"""Tests for memory.outcome_governor — TrialBench -> Ledger handoff stub.

stdlib + pytest only. No scipy / pandas / requests.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

# Make ``memory`` importable when tests run from arbitrary cwd.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory import outcome_governor as og  # noqa: E402


class _GovernorTestBase(unittest.TestCase):
    """Redirects LEDGER_PATH + TRIALBENCH_ROOT to a tmp dir per test."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = pathlib.Path(self._tmp.name)

        self._ledger_orig = og.LEDGER_PATH
        self._tb_orig = og.TRIALBENCH_ROOT

        og.LEDGER_PATH = self._tmp_path / "ledger.jsonl"
        og.TRIALBENCH_ROOT = self._tmp_path / "trialbench"
        og.TRIALBENCH_ROOT.mkdir(parents=True, exist_ok=True)

        # Reset error counters so each test starts clean.
        for k in list(og.INGEST_ERRORS.keys()):
            og.INGEST_ERRORS[k] = 0

    def tearDown(self) -> None:
        og.LEDGER_PATH = self._ledger_orig
        og.TRIALBENCH_ROOT = self._tb_orig
        self._tmp.cleanup()

    def _write_scored_trial(
        self,
        trial_id: str,
        scored: list[dict],
    ) -> pathlib.Path:
        d = og.TRIALBENCH_ROOT / trial_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "scored_outcomes.json").write_text(json.dumps(scored), encoding="utf-8")
        return d

    def _write_runs_only_trial(self, trial_id: str, runs: list[dict]) -> pathlib.Path:
        d = og.TRIALBENCH_ROOT / trial_id
        d.mkdir(parents=True, exist_ok=True)
        for i, r in enumerate(runs):
            (d / f"run_{i:03d}.json").write_text(json.dumps(r), encoding="utf-8")
        return d


class TestIngestListSummarize(_GovernorTestBase):
    def test_ingest_list_summarize_full_loop(self) -> None:
        trial_id = "trial_synth_001"
        scored = [
            {
                "task_id": "arch_001",
                "arm": "A_raw",
                "node_id": "mac2",
                "tests_pass": True,
                "no_invariant_violation": True,
                "primary_endpoint_success": True,
                "task_complete": True,
                "blinded_reviewer_approved": True,
                "timed_out": False,
                "source_file": "run_000.json",
            },
            {
                "task_id": "arch_001",
                "arm": "C_contextdna_packet",
                "node_id": "mac2",
                "tests_pass": True,
                "no_invariant_violation": True,
                "primary_endpoint_success": True,
                "task_complete": True,
                "blinded_reviewer_approved": True,
                "timed_out": False,
                "source_file": "run_001.json",
            },
        ]
        self._write_scored_trial(trial_id, scored)

        n = og.ingest_trial(trial_id)
        self.assertEqual(n, 2)

        entries = og.list_outcomes(limit=10)
        self.assertEqual(len(entries), 2)
        seqs = [e.sequence_id for e in entries]
        # most-recent first, monotonic sequence ids
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        self.assertEqual({e.outcome_record.arm for e in entries}, {"A_raw", "C_contextdna_packet"})

        a_only = og.list_outcomes(limit=10, arm="A_raw")
        self.assertEqual(len(a_only), 1)
        self.assertEqual(a_only[0].outcome_record.arm, "A_raw")

        summary = og.summarize_recent(window_hours=24)
        self.assertEqual(summary["total"], 2)
        self.assertIn("A_raw", summary["arms"])
        self.assertIn("C_contextdna_packet", summary["arms"])
        self.assertEqual(summary["arms"]["A_raw"]["count"], 1)
        self.assertEqual(summary["arms"]["A_raw"]["success"], 1)
        self.assertEqual(summary["arms"]["A_raw"]["success_rate"], 1.0)

    def test_idempotency_second_ingest_is_zero(self) -> None:
        trial_id = "trial_synth_idem"
        scored = [
            {
                "task_id": "arch_001",
                "arm": "A_raw",
                "node_id": "mac2",
                "tests_pass": True,
                "no_invariant_violation": True,
                "primary_endpoint_success": True,
                "task_complete": True,
                "blinded_reviewer_approved": True,
                "timed_out": False,
            },
        ]
        self._write_scored_trial(trial_id, scored)
        first = og.ingest_trial(trial_id)
        self.assertEqual(first, 1)
        second = og.ingest_trial(trial_id)
        self.assertEqual(second, 0)
        self.assertEqual(len(og.list_outcomes(limit=10)), 1)

    def test_arm_alias_normalization(self) -> None:
        trial_id = "trial_alias"
        scored = [
            {
                "task_id": "arch_002",
                "arm": "C_governed",  # alias -> C_contextdna_packet
                "node_id": "mac2",
                "tests_pass": True,
                "no_invariant_violation": True,
                "primary_endpoint_success": True,
                "task_complete": True,
                "blinded_reviewer_approved": True,
                "timed_out": False,
            },
        ]
        self._write_scored_trial(trial_id, scored)
        og.ingest_trial(trial_id)
        entries = og.list_outcomes(limit=10)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].outcome_record.arm, "C_contextdna_packet")

    def test_unknown_arm_skipped(self) -> None:
        trial_id = "trial_bad_arm"
        scored = [
            {
                "task_id": "arch_001",
                "arm": "D_chaos",
                "node_id": "mac2",
                "tests_pass": True,
            }
        ]
        self._write_scored_trial(trial_id, scored)
        n = og.ingest_trial(trial_id)
        self.assertEqual(n, 0)
        self.assertGreaterEqual(og.INGEST_ERRORS["skipped_unknown_arm"], 1)
        self.assertGreaterEqual(og.INGEST_ERRORS["outcome_ledger_ingest_errors"], 1)

    def test_timeout_outcome(self) -> None:
        trial_id = "trial_timeout"
        scored = [
            {
                "task_id": "arch_001",
                "arm": "A_raw",
                "node_id": "mac2",
                "tests_pass": False,
                "no_invariant_violation": True,
                "primary_endpoint_success": False,
                "task_complete": False,
                "timed_out": True,
            }
        ]
        self._write_scored_trial(trial_id, scored)
        og.ingest_trial(trial_id)
        entries = og.list_outcomes(limit=10)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].outcome_record.outcome, og.OUTCOME_TIMEOUT)
        self.assertEqual(entries[0].outcome_record.score, 0.0)

    def test_fallback_to_raw_runs_when_scored_missing(self) -> None:
        trial_id = "trial_raw_only"
        runs = [
            {
                "task_id": "arch_001",
                "node": "mac2",
                "arm": "A_raw",
                "exit_code": 0,
                "http_status": 200,
                "started_at": "2026-05-06T17:38:57+00:00",
                "finished_at": "2026-05-06T17:39:14+00:00",
                "tests_pass": True,
                "no_invariant_violation": True,
                "primary_endpoint_success": True,
                "task_complete": True,
            },
            {
                "task_id": "arch_001",
                "node": "mac2",
                "arm": "C_governed",
                "exit_code": 0,
                "http_status": 200,
                "tests_pass": True,
                "no_invariant_violation": True,
                "primary_endpoint_success": True,
                "task_complete": True,
            },
        ]
        self._write_runs_only_trial(trial_id, runs)
        n = og.ingest_trial(trial_id)
        self.assertEqual(n, 2)
        entries = og.list_outcomes(limit=10)
        arms = {e.outcome_record.arm for e in entries}
        self.assertEqual(arms, {"A_raw", "C_contextdna_packet"})
        for e in entries:
            self.assertEqual(e.outcome_record.score_method, "heuristic_v0_raw")

    def test_path_traversal_rejected(self) -> None:
        # ``..`` in trial_id must not escape the allowlist.
        n = og.ingest_trial("../etc")
        self.assertEqual(n, 0)
        n = og.ingest_trial("/absolute/path")
        self.assertEqual(n, 0)
        self.assertGreaterEqual(og.INGEST_ERRORS["missing_artifact_dir"], 1)

    def test_missing_trial_dir(self) -> None:
        n = og.ingest_trial("does_not_exist")
        self.assertEqual(n, 0)

    def test_summarize_empty_ledger(self) -> None:
        s = og.summarize_recent(window_hours=24)
        self.assertEqual(s["total"], 0)
        # all known arms present with zero counts
        for arm in og.KNOWN_ARMS:
            self.assertEqual(s["arms"][arm]["count"], 0)
            self.assertEqual(s["arms"][arm]["success_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
