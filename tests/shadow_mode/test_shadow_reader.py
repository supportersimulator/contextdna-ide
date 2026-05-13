"""Tests for :mod:`services.shadow_mode.shadow_reader`.

Verifies the headline contract: a known-divergent operation (production
writes 1 row, shadow writes 2) is detected, classified, logged, and
counted. Production must NOT be perturbed.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

# Make ``services.shadow_mode.shadow_reader`` importable when running this
# test from the repo root or from CI without an install.
_THIS_DIR = Path(__file__).resolve().parent
_MIGRATE5 = _THIS_DIR.parent.parent
if str(_MIGRATE5) not in sys.path:
    sys.path.insert(0, str(_MIGRATE5))

from services.shadow_mode.shadow_reader import (  # noqa: E402
    ShadowComparator,
    ShadowStorage,
    get_counters,
    report,
    reset_counters,
)


class _FakeProdStore:
    """A tiny fake LearningStore that we can make diverge on demand."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self.divergent_mode = False

    def store_learning(self, learning_data: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
        snap = dict(learning_data)
        snap.setdefault("id", f"prod_{len(self.rows)}")
        self.rows.insert(0, snap)
        return dict(snap)

    def get_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        # Honor the divergent_mode flag: under that flag, prod silently drops
        # the most-recent row — exactly the get_recent silent-empty class of
        # bug the shadow exists to catch.
        rows = list(self.rows[:limit])
        if self.divergent_mode and rows:
            rows = rows[1:]
        return rows

    def get_by_id(self, learning_id: str):
        for r in self.rows:
            if r.get("id") == learning_id:
                return dict(r)
        return None


def _wait_for(condition, timeout: float = 2.0) -> bool:
    """Spin-wait for a condition; returns True if condition became true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class ShadowReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_counters()
        # Isolate the log path per-test so reports don't bleed.
        self.tmp = Path(
            os.environ.get("TMPDIR", "/tmp")
        ) / f"shadow_test_{os.getpid()}_{id(self)}.jsonl"
        if self.tmp.exists():
            self.tmp.unlink()
        # Force 100% sampling so the test is deterministic.
        os.environ["SHADOW_SAMPLE_RATE"] = "1.0"

    def tearDown(self) -> None:
        os.environ.pop("SHADOW_SAMPLE_RATE", None)
        os.environ.pop("SHADOW_SAMPLE_store_learning", None)
        os.environ.pop("SHADOW_SAMPLE_get_recent", None)
        if self.tmp.exists():
            try:
                self.tmp.unlink()
            except OSError:
                pass

    # -- contract: prod return value is unchanged ----------------------- #

    def test_production_result_is_returned_unchanged(self) -> None:
        prod = _FakeProdStore()
        cmp = ShadowComparator(log_path=self.tmp)
        try:
            store = cmp.wrap_store(prod)
            result = store.store_learning({"id": "abc", "type": "win", "content": "x"})
            # Wait for the shadow side to drain.
            cmp._executor.shutdown(wait=True)  # type: ignore[attr-defined]
        finally:
            cmp.close()
        self.assertEqual(result["id"], "abc")
        self.assertEqual(result["type"], "win")

    # -- headline contract: divergence is detected, classified, logged -- #

    def test_known_divergent_operation_is_detected_and_counted(self) -> None:
        """Prod writes 1 row; shadow gets a different ID under a forced
        divergence — expected outcome is a count_mismatch on the *next*
        get_recent because prod has 1 row and shadow has 2.
        """
        prod = _FakeProdStore()
        cmp = ShadowComparator(log_path=self.tmp)
        try:
            # Pre-seed the shadow with an extra row so prod (1) != shadow (2)
            # for any subsequent get_recent — the simplest forced divergence.
            cmp.shadow.seed(
                [
                    {"id": "shadow_only_1", "type": "win", "content": "phantom"},
                ]
            )
            # Now write one row to prod via the wrapped store. The shadow
            # mirrors it, so after the write: prod has 1, shadow has 2.
            store = cmp.wrap_store(prod)
            store.store_learning(
                {"id": "real_1", "type": "fix", "content": "real"}
            )
            # Trigger a get_recent — this is the divergent call.
            rows = store.get_recent(limit=10)
            # Drain the shadow executor so divergence handlers have run.
            cmp._executor.shutdown(wait=True)  # type: ignore[attr-defined]
        finally:
            cmp.close()

        # Production result must be unchanged: it still has exactly 1 row.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "real_1")

        counters = get_counters()
        # At least one divergence must be recorded against get_recent.
        flat_key = "shadow_divergence_total::get_recent::count_mismatch"
        self.assertGreaterEqual(
            counters.get(flat_key, 0), 1,
            f"expected get_recent count_mismatch divergence; counters={counters}",
        )
        self.assertGreaterEqual(counters.get("shadow_divergence_total", 0), 1)
        self.assertGreaterEqual(
            counters.get("shadow_divergence_count_mismatch_total", 0), 1
        )

        # Logfile must contain a parseable JSONL record.
        self.assertTrue(
            _wait_for(lambda: self.tmp.exists() and self.tmp.stat().st_size > 0),
            f"divergence logfile {self.tmp} was never written",
        )
        records = [
            json.loads(line)
            for line in self.tmp.read_text().splitlines()
            if line.strip()
        ]
        self.assertTrue(records, "no divergence records were written")
        kinds = {r["kind"] for r in records}
        self.assertIn("count_mismatch", kinds)
        methods = {r["method"] for r in records}
        self.assertIn("get_recent", methods)

        # Report must reflect what we saw.
        summary = report(self.tmp)
        self.assertGreaterEqual(summary["total"], 1)
        self.assertIn("count_mismatch", summary["by_kind"])

    # -- contract: shadow exceptions never leak to caller --------------- #

    def test_shadow_exception_is_captured_not_raised(self) -> None:
        class BrokenShadow(ShadowStorage):
            def get_recent(self, limit: int = 20):  # type: ignore[override]
                raise RuntimeError("shadow imploded")

        prod = _FakeProdStore()
        prod.store_learning({"id": "x", "type": "win", "content": "ok"})
        cmp = ShadowComparator(shadow=BrokenShadow(), log_path=self.tmp)
        try:
            store = cmp.wrap_store(prod)
            # Must NOT raise.
            rows = store.get_recent(limit=5)
            cmp._executor.shutdown(wait=True)  # type: ignore[attr-defined]
        finally:
            cmp.close()
        self.assertEqual(len(rows), 1)
        counters = get_counters()
        self.assertGreaterEqual(
            counters.get("shadow_divergence_exception_in_shadow_total", 0), 1
        )

    # -- contract: sampling switches the shadow off -------------------- #

    def test_zero_sample_rate_skips_shadow(self) -> None:
        os.environ["SHADOW_SAMPLE_RATE"] = "0.0"
        prod = _FakeProdStore()
        cmp = ShadowComparator(log_path=self.tmp)
        try:
            store = cmp.wrap_store(prod)
            for _ in range(5):
                store.store_learning(
                    {"id": f"id_{_}", "type": "win", "content": "x"}
                )
                store.get_recent(limit=5)
            cmp._executor.shutdown(wait=True)  # type: ignore[attr-defined]
        finally:
            cmp.close()
        counters = get_counters()
        self.assertEqual(counters.get("shadow_calls_sampled_total", 0), 0)
        self.assertGreaterEqual(
            counters.get("shadow_calls_skipped_sampling_total", 0), 10
        )

    # -- contract: per-method override beats the global rate ----------- #

    def test_per_method_override(self) -> None:
        os.environ["SHADOW_SAMPLE_RATE"] = "0.0"
        os.environ["SHADOW_SAMPLE_get_recent"] = "1.0"
        prod = _FakeProdStore()
        prod.store_learning({"id": "a", "type": "win", "content": "x"})
        cmp = ShadowComparator(log_path=self.tmp)
        try:
            store = cmp.wrap_store(prod)
            # store_learning should be skipped (global=0); get_recent should
            # run on every call (override=1).
            store.store_learning({"id": "b", "type": "win", "content": "y"})
            store.get_recent(limit=10)
            cmp._executor.shutdown(wait=True)  # type: ignore[attr-defined]
        finally:
            cmp.close()
        counters = get_counters()
        # Exactly one sampled call (get_recent).
        self.assertEqual(counters.get("shadow_calls_sampled_total", 0), 1)

    # -- contract: seed_from_store mirrors initial state ---------------- #

    def test_seed_from_store(self) -> None:
        prod = _FakeProdStore()
        for i in range(7):
            prod.store_learning(
                {"id": f"seed_{i}", "type": "win", "content": str(i)}
            )
        cmp = ShadowComparator(log_path=self.tmp)
        try:
            ingested = cmp.seed_from_store(prod, limit=100)
            self.assertEqual(ingested, 7)
            self.assertEqual(len(cmp.shadow), 7)
        finally:
            cmp.close()


if __name__ == "__main__":
    unittest.main()
