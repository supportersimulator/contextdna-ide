"""Tests for memory.permission_governor — read-only Permission Governor.

stdlib + unittest only. Asserts:
  - empty ledger -> low_data=True, neutral score
  - 5 outcomes 80% success -> ~0.8 (within decay tolerance)
  - tier transitions restricted/advisory/trusted/lead at correct thresholds
  - idempotent: same input -> same output
  - read-only: ledger mtime unchanged after compute_*
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory import permission_governor as pg  # noqa: E402


def _now_iso(offset_hours: float = 0.0) -> str:
    t = _dt.datetime.now(tz=_dt.timezone.utc) + _dt.timedelta(hours=offset_hours)
    return t.isoformat()


def _entry(seq: int, agent_node: str, score: float, ts_offset_hours: float = 0.0,
           outcome: str = "success", arm: str = "A_raw", task_id: str = "task_x") -> dict:
    """Build a ledger-shaped entry. Uses ``node`` field (Q3 schema)."""
    ts = _now_iso(ts_offset_hours)
    return {
        "sequence_id": seq,
        "write_timestamp": ts,
        "outcome_record": {
            "task_id": task_id,
            "arm": arm,
            "node": agent_node,
            "outcome": outcome,
            "score": score,
            "evidence_refs": [],
            "timestamp": ts,
            "trial_id": f"trial_{seq:03d}",
            "score_method": "heuristic_v0_test",
        },
    }


class _GovernorTestBase(unittest.TestCase):
    """Redirects LEDGER_PATH to a tmp file per test. Read-only consumer
    means we never write through pg, but we do construct ledger files for
    fixtures."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = pathlib.Path(self._tmp.name)
        self._ledger_orig = pg.LEDGER_PATH
        pg.LEDGER_PATH = self._tmp_path / "ledger.jsonl"
        # reset error counters per test
        for k in list(pg.COMPUTE_ERRORS.keys()):
            pg.COMPUTE_ERRORS[k] = 0

    def tearDown(self) -> None:
        pg.LEDGER_PATH = self._ledger_orig
        self._tmp.cleanup()

    def _write_ledger(self, entries: list[dict]) -> None:
        with pg.LEDGER_PATH.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")


class TestEmptyLedger(_GovernorTestBase):
    def test_compute_influence_empty(self) -> None:
        infl = pg.compute_influence("any-agent")
        self.assertTrue(infl.low_data)
        self.assertEqual(infl.sample_count, 0)
        self.assertAlmostEqual(infl.score, pg.NEUTRAL_SCORE)
        self.assertIsNone(infl.last_outcome_at)

    def test_compute_permissions_empty_low_data_is_advisory(self) -> None:
        perms = pg.compute_permissions("any-agent")
        self.assertEqual(perms.tier, "advisory")
        self.assertEqual(perms.gates_relaxed, [])
        self.assertEqual(perms.gates_tightened, [])
        self.assertIsNotNone(perms.influence)
        self.assertTrue(perms.influence.low_data)

    def test_list_agent_influences_empty(self) -> None:
        self.assertEqual(pg.list_agent_influences(), [])


class TestInfluenceMath(_GovernorTestBase):
    def test_five_outcomes_80pct_success_yields_circa_0_8(self) -> None:
        # 5 fresh outcomes (small offsets so decay barely affects them):
        # 4 successes (score=1.0), 1 failure (score=0.0). Avg ~ 0.8.
        entries = [
            _entry(1, "agentA", 1.0, ts_offset_hours=-0.1),
            _entry(2, "agentA", 1.0, ts_offset_hours=-0.2),
            _entry(3, "agentA", 1.0, ts_offset_hours=-0.3),
            _entry(4, "agentA", 1.0, ts_offset_hours=-0.4),
            _entry(5, "agentA", 0.0, ts_offset_hours=-0.5, outcome="failure"),
        ]
        self._write_ledger(entries)
        infl = pg.compute_influence("agentA")
        self.assertEqual(infl.sample_count, 5)
        self.assertFalse(infl.low_data)
        # Within decay tolerance of 0.8
        self.assertAlmostEqual(infl.score, 0.8, delta=0.02)

    def test_low_data_below_threshold_returns_neutral(self) -> None:
        # 4 outcomes < LOW_DATA_THRESHOLD=5 -> low_data=True, score=neutral
        entries = [
            _entry(i, "agentB", 1.0, ts_offset_hours=-0.1 * i)
            for i in range(1, 5)
        ]
        self._write_ledger(entries)
        infl = pg.compute_influence("agentB")
        self.assertEqual(infl.sample_count, 4)
        self.assertTrue(infl.low_data)
        self.assertAlmostEqual(infl.score, pg.NEUTRAL_SCORE)

    def test_decay_older_outcomes_count_less(self) -> None:
        # 5 successes 1 week old + 5 failures fresh -> fresh failures dominate
        entries: list[dict] = []
        seq = 0
        for i in range(5):
            seq += 1
            entries.append(_entry(seq, "agentC", 1.0, ts_offset_hours=-160.0 - i))
        for i in range(5):
            seq += 1
            entries.append(_entry(seq, "agentC", 0.0, ts_offset_hours=-0.1 * i,
                                  outcome="failure"))
        self._write_ledger(entries)
        infl = pg.compute_influence("agentC")
        self.assertEqual(infl.sample_count, 10)
        self.assertFalse(infl.low_data)
        # Fresh failures weighted ~e^0=1; old successes weighted ~e^-1≈0.37
        # so avg should be well below 0.5
        self.assertLess(infl.score, 0.45)

    def test_window_excludes_ancient_outcomes(self) -> None:
        # Default window 168h; these are 200h old -> excluded entirely
        entries = [
            _entry(1, "agentD", 1.0, ts_offset_hours=-200.0),
            _entry(2, "agentD", 1.0, ts_offset_hours=-201.0),
        ]
        self._write_ledger(entries)
        infl = pg.compute_influence("agentD")
        self.assertEqual(infl.sample_count, 0)
        self.assertTrue(infl.low_data)


class TestTierTransitions(_GovernorTestBase):
    def _make_agent_with_target_score(self, agent: str, target: float, n: int = 10) -> None:
        # All same score, fresh -> avg ~= target (decay near 1.0 for fresh ts)
        entries = [
            _entry(i + 1, agent, target, ts_offset_hours=-0.1 * (i + 1))
            for i in range(n)
        ]
        self._write_ledger(entries)

    def test_restricted_tier(self) -> None:
        self._make_agent_with_target_score("rA", 0.10)
        perms = pg.compute_permissions("rA")
        self.assertEqual(perms.tier, "restricted")
        self.assertGreater(len(perms.gates_tightened), 0)
        self.assertEqual(perms.gates_relaxed, [])

    def test_advisory_tier(self) -> None:
        self._make_agent_with_target_score("rB", 0.50)
        perms = pg.compute_permissions("rB")
        self.assertEqual(perms.tier, "advisory")
        self.assertEqual(perms.gates_relaxed, [])
        self.assertEqual(perms.gates_tightened, [])

    def test_trusted_tier(self) -> None:
        self._make_agent_with_target_score("rC", 0.70)
        perms = pg.compute_permissions("rC")
        self.assertEqual(perms.tier, "trusted")
        self.assertGreater(len(perms.gates_relaxed), 0)

    def test_lead_tier(self) -> None:
        self._make_agent_with_target_score("rD", 0.95)
        perms = pg.compute_permissions("rD")
        self.assertEqual(perms.tier, "lead")
        self.assertIn("auto_promote_low_risk_memory", perms.gates_relaxed)

    def test_threshold_boundaries(self) -> None:
        # Exactly at threshold -> upper tier (>=)
        for score, expected in [
            (0.30, "advisory"),
            (0.60, "trusted"),
            (0.85, "lead"),
            (0.0, "restricted"),
        ]:
            with self.subTest(score=score):
                self._make_agent_with_target_score("agB_" + str(score), score)
                p = pg.compute_permissions("agB_" + str(score))
                self.assertEqual(p.tier, expected)


class TestIdempotency(_GovernorTestBase):
    def test_same_input_same_output(self) -> None:
        entries = [
            _entry(i + 1, "stable", 0.7, ts_offset_hours=-0.1 * (i + 1))
            for i in range(8)
        ]
        self._write_ledger(entries)
        a = pg.compute_influence("stable")
        b = pg.compute_influence("stable")
        # last_outcome_at uses real "now" only via cutoff, not output ts; the
        # output last_outcome_at is the entry's own timestamp -> stable.
        self.assertEqual(a.score, b.score)
        self.assertEqual(a.sample_count, b.sample_count)
        self.assertEqual(a.low_data, b.low_data)
        self.assertEqual(a.last_outcome_at, b.last_outcome_at)

        pa = pg.compute_permissions("stable")
        pb = pg.compute_permissions("stable")
        self.assertEqual(pa.tier, pb.tier)
        self.assertEqual(pa.gates_relaxed, pb.gates_relaxed)
        self.assertEqual(pa.gates_tightened, pb.gates_tightened)


class TestReadOnly(_GovernorTestBase):
    def test_ledger_mtime_unchanged_after_compute(self) -> None:
        entries = [
            _entry(i + 1, "ro_agent", 0.5, ts_offset_hours=-0.1 * (i + 1))
            for i in range(6)
        ]
        self._write_ledger(entries)
        mtime_before = pg.LEDGER_PATH.stat().st_mtime_ns
        size_before = pg.LEDGER_PATH.stat().st_size

        # Run all compute_ functions
        _ = pg.compute_influence("ro_agent")
        _ = pg.compute_permissions("ro_agent")
        _ = pg.list_agent_influences()
        _ = pg.summarize_for_health()

        mtime_after = pg.LEDGER_PATH.stat().st_mtime_ns
        size_after = pg.LEDGER_PATH.stat().st_size

        self.assertEqual(mtime_before, mtime_after, "ledger mtime changed — not read-only!")
        self.assertEqual(size_before, size_after, "ledger size changed — not read-only!")

    def test_compute_does_not_create_ledger_when_missing(self) -> None:
        # ledger doesn't exist
        if pg.LEDGER_PATH.exists():
            os.unlink(pg.LEDGER_PATH)
        infl = pg.compute_influence("ghost")
        perms = pg.compute_permissions("ghost")
        self.assertTrue(infl.low_data)
        self.assertEqual(perms.tier, "advisory")
        self.assertFalse(pg.LEDGER_PATH.exists(),
                         "compute_* must not create the ledger file")


class TestMultiAgent(_GovernorTestBase):
    def test_list_agent_influences_distinct_agents(self) -> None:
        entries = [
            _entry(1, "alpha", 1.0, ts_offset_hours=-0.1),
            _entry(2, "beta", 0.0, ts_offset_hours=-0.1, outcome="failure"),
            _entry(3, "alpha", 1.0, ts_offset_hours=-0.2),
        ]
        self._write_ledger(entries)
        infls = pg.list_agent_influences()
        ids = {i.agent_id for i in infls}
        self.assertEqual(ids, {"alpha", "beta"})

    def test_summarize_for_health_shape(self) -> None:
        entries = [_entry(i + 1, "h", 0.7, -0.1 * (i + 1)) for i in range(7)]
        self._write_ledger(entries)
        h = pg.summarize_for_health()
        self.assertIn("agents", h)
        self.assertIn("updated_at", h)
        self.assertIn("rules", h)
        self.assertIn("errors", h)
        self.assertEqual(len(h["agents"]), 1)
        self.assertEqual(h["agents"][0]["id"], "h")
        self.assertEqual(h["agents"][0]["tier"], "trusted")


class TestZSFCounters(_GovernorTestBase):
    def test_malformed_ledger_line_bumps_parse_counter(self) -> None:
        with pg.LEDGER_PATH.open("w", encoding="utf-8") as f:
            f.write(json.dumps(_entry(1, "x", 1.0, -0.1)) + "\n")
            f.write("{not valid json\n")
            f.write(json.dumps(_entry(2, "x", 1.0, -0.2)) + "\n")
        infl = pg.compute_influence("x")
        # 2 valid samples (still low_data, but counter must have been bumped)
        self.assertEqual(infl.sample_count, 2)
        self.assertGreaterEqual(pg.COMPUTE_ERRORS["ledger_parse_errors"], 1)


if __name__ == "__main__":
    unittest.main()
