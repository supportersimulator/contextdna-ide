"""Tests for scripts/north_star.py — TBG North Star CLI seed.

Run from repo root:
    python3 -m pytest scripts/tests/test_north_star.py -v
or directly:
    python3 scripts/tests/test_north_star.py
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

# Make scripts/ importable.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import north_star  # noqa: E402  (import after sys.path tweak)


class TestPriorities(unittest.TestCase):
    def test_priorities_have_locked_order(self):
        names = [p.name for p in north_star.PRIORITIES]
        self.assertEqual(
            names,
            ["Multi-Fleet", "3-Surgeons", "ContextDNA IDE", "Full Local Ops", "ER Simulator"],
        )

    def test_ranks_are_1_to_5(self):
        ranks = [p.rank for p in north_star.PRIORITIES]
        self.assertEqual(ranks, [1, 2, 3, 4, 5])

    def test_slugs_are_unique(self):
        slugs = [p.slug for p in north_star.PRIORITIES]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_find_priority_by_slug_and_name(self):
        self.assertEqual(north_star.find_priority("multi-fleet").rank, 1)
        self.assertEqual(north_star.find_priority("3-Surgeons").rank, 2)
        self.assertIsNone(north_star.find_priority("nonexistent"))


class TestShow(unittest.TestCase):
    def test_show_prints_all_five_in_order(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = north_star._print_show(as_json=False)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # All five must appear, and Multi-Fleet must come before ER Simulator.
        for name in ("Multi-Fleet", "3-Surgeons", "ContextDNA IDE", "Full Local Ops", "ER Simulator"):
            self.assertIn(name, out)
        self.assertLess(out.index("Multi-Fleet"), out.index("3-Surgeons"))
        self.assertLess(out.index("3-Surgeons"), out.index("ContextDNA IDE"))
        self.assertLess(out.index("ContextDNA IDE"), out.index("Full Local Ops"))
        self.assertLess(out.index("Full Local Ops"), out.index("ER Simulator"))

    def test_show_json_round_trip(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = north_star._print_show(as_json=True)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload), 5)
        self.assertEqual(payload[0]["rank"], 1)
        self.assertEqual(payload[0]["slug"], "multi-fleet")


class TestClassify(unittest.TestCase):
    def test_fleet_keyword_maps_to_multi_fleet(self):
        result = north_star.classify("fix fleet daemon NATS reconnect")
        self.assertIsNotNone(result.priority)
        self.assertEqual(result.priority.slug, "multi-fleet")
        self.assertGreater(result.score, 0)

    def test_discord_bug_maps_to_multi_fleet(self):
        # Discord bot lives in multi-fleet — see CLAUDE.md fleet section.
        result = north_star.classify("fix discord bug in fleet relay")
        self.assertEqual(result.priority.slug, "multi-fleet")

    def test_surgeon_consult_maps_to_3_surgeons(self):
        result = north_star.classify("add cardiologist cross-examine to consensus tool")
        self.assertEqual(result.priority.slug, "3-surgeons")

    def test_webhook_maps_to_contextdna(self):
        result = north_star.classify("debug context-dna webhook injection")
        self.assertEqual(result.priority.slug, "contextdna-ide")

    def test_gains_gate_maps_to_local_ops(self):
        result = north_star.classify("add new check to gains-gate memory probe")
        self.assertEqual(result.priority.slug, "full-local-ops")

    def test_ecg_maps_to_er_simulator(self):
        result = north_star.classify("fix ECG audio stripping in vitals telemetry")
        self.assertEqual(result.priority.slug, "er-simulator")

    def test_orphan_returns_none(self):
        result = north_star.classify("update README with weekend BBQ recipes")
        self.assertIsNone(result.priority)
        self.assertEqual(result.score, 0)

    def test_empty_input_returns_none(self):
        self.assertIsNone(north_star.classify("").priority)
        self.assertIsNone(north_star.classify("   ").priority)

    def test_tie_break_prefers_lower_rank(self):
        # Both 3-surgeons (cardio) and full-local-ops (cardio-gate) match
        # "cardio". Lower rank (3-Surgeons = rank 2) should win.
        result = north_star.classify("cardio")
        self.assertEqual(result.priority.rank, 2)

    def test_classify_to_dict_serializable(self):
        result = north_star.classify("fix fleet daemon")
        d = result.to_dict()
        # Must round-trip through json.
        json.dumps(d)
        self.assertEqual(d["slug"], "multi-fleet")


class TestDriftCheck(unittest.TestCase):
    def test_drift_check_with_injected_log(self):
        rows = [
            ("a" * 40, "2026-05-01T10:00:00+00:00", "fix fleet NATS reconnect"),
            ("b" * 40, "2026-05-02T10:00:00+00:00", "add cardiologist consult"),
            ("c" * 40, "2026-05-03T10:00:00+00:00", "weekend BBQ recipes"),  # drift
            ("d" * 40, "2026-05-03T11:00:00+00:00", "tweak office plant"),    # drift
        ]
        drift = north_star.drift_check(log_rows=rows)
        subjects = [d.subject for d in drift]
        self.assertEqual(len(drift), 2)
        self.assertIn("weekend BBQ recipes", subjects)
        self.assertIn("tweak office plant", subjects)

    def test_drift_check_empty_log(self):
        self.assertEqual(north_star.drift_check(log_rows=[]), [])

    def test_drift_check_all_aligned(self):
        rows = [
            ("a" * 40, "2026-05-01T10:00:00+00:00", "feat(fleet): NATS bus"),
            ("b" * 40, "2026-05-02T10:00:00+00:00", "feat(3-surgeons): consensus"),
            ("c" * 40, "2026-05-03T10:00:00+00:00", "fix(ecg): vitals audio"),
        ]
        self.assertEqual(north_star.drift_check(log_rows=rows), [])

    def test_drift_commit_truncates_sha(self):
        rows = [("0123456789abcdef" * 2 + "ff" * 4, "2026-05-01T10:00:00+00:00", "BBQ")]
        drift = north_star.drift_check(log_rows=rows)
        self.assertEqual(len(drift), 1)
        self.assertEqual(len(drift[0].sha), 12)


class TestCLI(unittest.TestCase):
    def test_main_show_default(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = north_star.main(["show"])
        self.assertEqual(rc, 0)
        self.assertIn("Multi-Fleet", buf.getvalue())

    def test_main_classify_match(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = north_star.main(["classify", "fix", "fleet", "NATS"])
        self.assertEqual(rc, 0)
        self.assertIn("Multi-Fleet", buf.getvalue())

    def test_main_classify_drift_returns_1(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = north_star.main(["classify", "weekend", "BBQ", "recipes"])
        self.assertEqual(rc, 1)
        self.assertIn("DRIFT", buf.getvalue())

    def test_main_json_show(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = north_star.main(["--json", "show"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload), 5)


if __name__ == "__main__":
    unittest.main()
