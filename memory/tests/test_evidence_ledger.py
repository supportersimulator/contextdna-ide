"""Tests for memory.evidence_ledger — content-addressed Evidence Ledger v1.

stdlib + pytest/unittest only. No scipy / pandas / requests.

Covers:
  1. Idempotence (100x same content = 1 row, dedupe counter at 99)
  2. Determinism (same content -> same record_id, sha256 stable)
  3. DAG lineage (child references parent_ids; lineage walk returns ancestors)
  4. Brain bridge round-trip (record -> brain.context returns it back; if
     brain unavailable, bridge errors counter increments and returns False —
     test still proves the bridge is wired)
  5. Outcome ledger backfill (Q3 entries become EvidenceRecord(kind="outcome"))
  6. Negative: write failure -> counter increments, exception NOT swallowed
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

# Make ``memory`` importable when tests run from arbitrary cwd.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory import evidence_ledger as el  # noqa: E402


class _LedgerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = pathlib.Path(self._tmp.name)
        self._db = self._tmp_path / "evidence.db"
        # Reset counters for test isolation
        for k in list(el.COUNTERS.keys()):
            el.COUNTERS[k] = 0
        self.ledger = el.EvidenceLedger(db_path=self._db)

    def tearDown(self) -> None:
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# 1. Idempotence
# ---------------------------------------------------------------------------

class TestIdempotence(_LedgerTestBase):
    def test_same_content_100x_yields_one_row_99_dedupes(self) -> None:
        content = {"experiment_id": "exp-001", "metric": "auc", "score": 0.92}

        first = self.ledger.record(content=content, kind="experiment")
        for _ in range(99):
            again = self.ledger.record(content=content, kind="experiment")
            self.assertEqual(first.record_id, again.record_id)

        rows = self.ledger.query(kind="experiment")
        self.assertEqual(len(rows), 1, "idempotent record should produce exactly 1 row")
        self.assertEqual(el.COUNTERS["evidence_records_total"], 1)
        self.assertEqual(el.COUNTERS["evidence_dedupe_hits_total"], 99)


# ---------------------------------------------------------------------------
# 2. Determinism
# ---------------------------------------------------------------------------

class TestDeterminism(_LedgerTestBase):
    def test_same_content_produces_stable_record_id(self) -> None:
        # Two distinct ledgers (different DBs) — same content -> same id.
        content = {"trial_id": "tb-2026-05-06", "arm": "A_raw", "node": "mac2"}

        rec1 = self.ledger.record(content=content, kind="trial")

        other_db = self._tmp_path / "other.db"
        other = el.EvidenceLedger(db_path=other_db)
        rec2 = other.record(content=content, kind="trial")

        self.assertEqual(rec1.record_id, rec2.record_id, "sha256 of canonical content must be deterministic across machines")
        # And length is sha256 hex (64)
        self.assertEqual(len(rec1.record_id), 64)

    def test_different_kind_yields_different_id(self) -> None:
        content = {"k": "v"}
        a = self.ledger.record(content=content, kind="experiment")
        b = self.ledger.record(content=content, kind="trial")
        self.assertNotEqual(a.record_id, b.record_id, "kind is part of canonical hash input")

    def test_different_parents_yield_different_id(self) -> None:
        content = {"k": "v"}
        a = self.ledger.record(content=content, kind="audit")
        b = self.ledger.record(content={"k": "different"}, kind="audit")
        c = self.ledger.record(content=content, kind="audit", parent_ids=[b.record_id])
        # a and c have same content+kind but different parent_ids -> different record_id
        self.assertNotEqual(a.record_id, c.record_id)


# ---------------------------------------------------------------------------
# 3. DAG lineage
# ---------------------------------------------------------------------------

class TestLineage(_LedgerTestBase):
    def test_lineage_walks_parent_ids(self) -> None:
        # Build DAG: gp -> p -> child
        gp = self.ledger.record(content={"step": 0, "tag": "genesis"}, kind="audit")
        p = self.ledger.record(
            content={"step": 1, "tag": "intermediate"},
            kind="audit",
            parent_ids=[gp.record_id],
        )
        child = self.ledger.record(
            content={"step": 2, "tag": "leaf"},
            kind="audit",
            parent_ids=[p.record_id],
        )

        ancestors = self.ledger.lineage(child.record_id)
        ancestor_ids = {r.record_id for r in ancestors}
        self.assertIn(p.record_id, ancestor_ids)
        self.assertIn(gp.record_id, ancestor_ids)
        self.assertNotIn(child.record_id, ancestor_ids, "lineage excludes the root walked-from node")

    def test_lineage_handles_diamond(self) -> None:
        # Diamond: a, b -> c (both parents of c)
        a = self.ledger.record(content={"x": 1}, kind="decision")
        b = self.ledger.record(content={"x": 2}, kind="decision")
        c = self.ledger.record(
            content={"x": 3},
            kind="decision",
            parent_ids=[a.record_id, b.record_id],
        )
        ancestors = self.ledger.lineage(c.record_id)
        ids = {r.record_id for r in ancestors}
        self.assertSetEqual(ids, {a.record_id, b.record_id})


# ---------------------------------------------------------------------------
# 4. Brain bridge round-trip
# ---------------------------------------------------------------------------

class TestBrainBridge(_LedgerTestBase):
    def test_brain_bridge_round_trip_when_brain_available(self) -> None:
        """Round-trip via mocked brain: record -> bridge writes -> we can read back.

        The real context_dna.brain may or may not be importable. We mock it
        with a minimal stub that records win() calls into a dict; then verify
        that to_brain wrote the record_id-keyed entry and we can fetch it.
        """
        captured: dict[str, dict] = {}

        fake_brain = mock.MagicMock()

        def _win(task: str, details: str | None = None, area: str | None = None) -> bool:
            captured[task] = {"details": details, "area": area}
            return True

        def _context(task: str) -> str:
            # Return whatever we captured for that task (as the brain would)
            return json.dumps(captured.get(task, {}))

        fake_brain.win = _win
        fake_brain.context = _context

        rec = self.ledger.record(
            content={"key": "round-trip", "score": 0.42},
            kind="experiment",
        )

        # Inject the mock module so the lazy import inside to_brain finds it.
        with mock.patch.dict(sys.modules, {"context_dna": mock.MagicMock(brain=fake_brain), "context_dna.brain": fake_brain}):
            ok = self.ledger.to_brain(rec)

        self.assertTrue(ok, "to_brain should return True when brain.win returns True")
        # The brain "task" key our bridge uses
        task_key = f"evidence:{rec.kind}:{rec.record_id[:16]}"
        self.assertIn(task_key, captured, "brain should have recorded the evidence entry")

        # Round trip: simulate brain.context lookup returning the stored details.
        retrieved = json.loads(fake_brain.context(task_key))
        self.assertEqual(retrieved["area"], "evidence_ledger")
        details = json.loads(retrieved["details"])
        self.assertEqual(details["record_id"], rec.record_id)
        self.assertEqual(details["kind"], rec.kind)

    def test_brain_bridge_errors_increment_counter_when_unavailable(self) -> None:
        """Bridge ZSF: missing brain -> counter increments, returns False, no raise."""
        rec = self.ledger.record(content={"k": "v"}, kind="audit")
        # Force ImportError by removing context_dna from sys.modules and
        # making fresh import fail.
        before = el.COUNTERS["evidence_brain_bridge_errors_total"]
        with mock.patch.dict(sys.modules, {}, clear=False):
            sys.modules.pop("context_dna", None)
            sys.modules.pop("context_dna.brain", None)
            # Make the import fail by inserting a sentinel that raises on attribute access
            with mock.patch("builtins.__import__", side_effect=ImportError("test-induced")):
                ok = self.ledger.to_brain(rec)
        self.assertFalse(ok)
        self.assertGreater(
            el.COUNTERS["evidence_brain_bridge_errors_total"],
            before,
            "bridge unavailability MUST increment counter (ZSF)",
        )


# ---------------------------------------------------------------------------
# 5. Outcome ledger backfill
# ---------------------------------------------------------------------------

class TestOutcomeBackfill(_LedgerTestBase):
    def _write_outcome_jsonl(self, path: pathlib.Path, entries: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_backfill_imports_q3_entries_as_kind_outcome(self) -> None:
        outcome_path = self._tmp_path / "outcome_ledger.jsonl"
        entries = [
            {
                "sequence_id": 1,
                "write_timestamp": "2026-05-06T17:53:10.473541+00:00",
                "outcome_record": {
                    "task_id": "arch_001",
                    "arm": "A_raw",
                    "node": "mac2",
                    "outcome": "success",
                    "score": 1.0,
                    "evidence_refs": ["foo.json"],
                    "timestamp": "2026-05-06T17:53:10.472921+00:00",
                    "trial_id": "ctxdna_trialbench_20260506T173856Z",
                    "score_method": "heuristic_v0_scored",
                },
            },
            {
                "sequence_id": 2,
                "write_timestamp": "2026-05-06T17:53:10.473541+00:00",
                "outcome_record": {
                    "task_id": "arch_001",
                    "arm": "C_contextdna_packet",
                    "node": "mac2",
                    "outcome": "success",
                    "score": 1.0,
                    "evidence_refs": ["bar.json"],
                    "timestamp": "2026-05-06T17:53:10.473516+00:00",
                    "trial_id": "ctxdna_trialbench_20260506T173856Z",
                    "score_method": "heuristic_v0_scored",
                },
            },
        ]
        self._write_outcome_jsonl(outcome_path, entries)

        n = self.ledger.from_outcome_ledger(ledger_path=outcome_path)
        self.assertEqual(n, 2, "should import 2 new outcome records")

        outcomes = self.ledger.query(kind="outcome")
        self.assertEqual(len(outcomes), 2)
        for r in outcomes:
            self.assertEqual(r.kind, "outcome")
            self.assertEqual(r.content.get("source"), "outcome_ledger.jsonl")

        # Re-run is idempotent (no NEW records, dedupe counter rises)
        n2 = self.ledger.from_outcome_ledger(ledger_path=outcome_path)
        self.assertEqual(n2, 0, "second backfill must be idempotent")
        self.assertGreaterEqual(el.COUNTERS["evidence_dedupe_hits_total"], 2)


# ---------------------------------------------------------------------------
# 6. Negative: write failure -> counter, exception NOT swallowed
# ---------------------------------------------------------------------------

class TestWriteFailureNotSilenced(_LedgerTestBase):
    def test_write_error_increments_counter_and_raises(self) -> None:
        """Force a sqlite write error and verify ZSF: counter++, exception raised."""
        rec_content = {"break": "me"}

        # Patch the ledger's _conn to return a connection whose execute raises
        # on INSERT (but not on SELECT, so the dedupe check still works).
        real_conn = self.ledger._conn

        class _BoomConn:
            def __init__(self, c: sqlite3.Connection):
                self._c = c
                self._calls = 0

            def execute(self, sql: str, *args, **kw):
                # Allow PRAGMA/SELECT/BEGIN — break only on INSERT to evidence_records.
                if "INSERT INTO EVIDENCE_RECORDS" in sql.upper():
                    raise sqlite3.OperationalError("test-induced write failure")
                return self._c.execute(sql, *args, **kw)

            def __getattr__(self, name):
                return getattr(self._c, name)

        def _patched_conn():
            return _BoomConn(real_conn())

        before = el.COUNTERS["evidence_write_errors_total"]
        with mock.patch.object(self.ledger, "_conn", _patched_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.ledger.record(content=rec_content, kind="experiment")

        self.assertGreater(
            el.COUNTERS["evidence_write_errors_total"], before,
            "write failure MUST bump evidence_write_errors_total (ZSF)",
        )


# ---------------------------------------------------------------------------
# Misc surface checks
# ---------------------------------------------------------------------------

class TestSurface(_LedgerTestBase):
    def test_get_returns_record_or_none(self) -> None:
        rec = self.ledger.record(content={"a": 1}, kind="audit")
        got = self.ledger.get(rec.record_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.record_id, rec.record_id)
        self.assertIsNone(self.ledger.get("not-a-real-id"))

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.ledger.record(content={"a": 1}, kind="not_a_kind")

    def test_content_must_be_dict(self) -> None:
        with self.assertRaises(TypeError):
            self.ledger.record(content="not-a-dict", kind="audit")  # type: ignore[arg-type]

    def test_stats_shape(self) -> None:
        self.ledger.record(content={"a": 1}, kind="audit")
        s = self.ledger.stats()
        self.assertIn("total_records", s)
        self.assertIn("by_kind", s)
        self.assertIn("counters", s)
        self.assertEqual(s["schema_version"], el.SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# 7. W1.b — post-hoc REDACT tombstone API
# ---------------------------------------------------------------------------

class TestRedactRecord(_LedgerTestBase):
    """5 cases for redact_record() — surgical removal that preserves chain.

    Idempotence policy (documented):
    -------------------------------
    Calling redact_record() on an already-redacted target is treated as a
    no-op that returns the existing tombstone metadata with
    ``already_redacted=True``. This avoids creating chains of tombstone
    records when the IDE re-clicks the Redact button. ``ledger_redact_ok_total``
    is bumped exactly once per unique tombstone (never twice for the same
    target).
    """

    # -- 1. happy path ------------------------------------------------------
    def test_happy_path_redacts_target_and_writes_tombstone(self) -> None:
        target = self.ledger.record(
            content={"secret": "leak_xyz", "task_id": "t-001"},
            kind="audit",
        )
        before_ok = el.COUNTERS["ledger_redact_ok_total"]

        result = self.ledger.redact_record(
            record_id=target.record_id,
            reason="manual",
            actor="atlas-ui",
        )

        # Result shape
        self.assertFalse(result["already_redacted"])
        self.assertEqual(result["redacted_target"], target.record_id)
        self.assertEqual(len(result["tombstone_record_id"]), 64)
        self.assertTrue(result["redacted_at"])

        # Counter bumped exactly once
        self.assertEqual(
            el.COUNTERS["ledger_redact_ok_total"], before_ok + 1
        )

        # Target row payload is now the marker; record_id (=sha256) untouched
        refreshed = self.ledger.get(target.record_id)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.record_id, target.record_id)
        self.assertTrue(refreshed.is_redacted)
        self.assertEqual(refreshed.redacted_by_record_id, result["tombstone_record_id"])
        self.assertEqual(
            refreshed.content.get("__redacted__"), "[REDACTED]"
        )

        # Tombstone exists and points at the target
        tomb = self.ledger.get(result["tombstone_record_id"])
        self.assertIsNotNone(tomb)
        self.assertEqual(tomb.kind, "redaction")
        self.assertEqual(tomb.content["subject"], target.record_id)
        self.assertEqual(tomb.content["reason"], "manual")
        self.assertEqual(tomb.content["actor"], "atlas-ui")
        self.assertIn(target.record_id, tomb.parent_ids)

    # -- 2. target missing --------------------------------------------------
    def test_target_missing_raises_evidence_ledger_error(self) -> None:
        before_missing = el.COUNTERS["ledger_redact_target_missing_total"]
        before_errors = el.COUNTERS["ledger_redact_errors_total"]

        with self.assertRaises(el.EvidenceLedgerError):
            self.ledger.redact_record(
                record_id="0" * 64,
                reason="testing",
                actor="atlas-test",
            )

        self.assertGreater(
            el.COUNTERS["ledger_redact_target_missing_total"],
            before_missing,
            "target_missing counter MUST bump on unknown record_id (ZSF)",
        )
        self.assertGreater(
            el.COUNTERS["ledger_redact_errors_total"],
            before_errors,
            "redact_errors_total MUST bump on every failure (ZSF)",
        )

    # -- 3. double-redact: idempotent no-op ---------------------------------
    def test_double_redact_is_idempotent_no_op(self) -> None:
        target = self.ledger.record(content={"k": "v"}, kind="audit")
        first = self.ledger.redact_record(
            record_id=target.record_id,
            reason="first",
            actor="atlas",
        )
        ok_after_first = el.COUNTERS["ledger_redact_ok_total"]

        # Second call must NOT create a 2nd tombstone, NOT bump the counter,
        # and MUST return the same tombstone_record_id with already_redacted=True.
        second = self.ledger.redact_record(
            record_id=target.record_id,
            reason="should be ignored",
            actor="atlas",
        )
        self.assertTrue(second["already_redacted"])
        self.assertEqual(
            second["tombstone_record_id"], first["tombstone_record_id"]
        )
        self.assertEqual(
            el.COUNTERS["ledger_redact_ok_total"],
            ok_after_first,
            "ok counter must NOT increment on idempotent re-redact",
        )

        # And only ONE tombstone exists for that target.
        redactions = self.ledger.query(kind="redaction")
        matching = [
            r for r in redactions if r.content.get("subject") == target.record_id
        ]
        self.assertEqual(len(matching), 1)

    # -- 4. unicode reason --------------------------------------------------
    def test_unicode_reason_round_trips(self) -> None:
        target = self.ledger.record(content={"a": 1}, kind="audit")
        unicode_reason = "leak GDPR ​ — 中文 \U0001f512"

        result = self.ledger.redact_record(
            record_id=target.record_id,
            reason=unicode_reason,
            actor="atlas",
        )
        tomb = self.ledger.get(result["tombstone_record_id"])
        self.assertIsNotNone(tomb)
        self.assertEqual(tomb.content["reason"], unicode_reason)

    # -- 5. hash chain unchanged after redact -------------------------------
    def test_hash_chain_unchanged_after_redact(self) -> None:
        # Build a small chain: a -> b -> c (each parents the previous).
        a = self.ledger.record(content={"step": "a"}, kind="audit")
        b = self.ledger.record(
            content={"step": "b"}, kind="audit", parent_ids=[a.record_id]
        )
        c = self.ledger.record(
            content={"step": "c", "secret": "leak_xyz"},
            kind="audit",
            parent_ids=[b.record_id],
        )

        # Verify chain is healthy BEFORE redact.
        v0 = self.ledger.verify_chain()
        self.assertTrue(v0["ok"])
        self.assertEqual(v0["mismatches"], [])

        # Redact c (the leaf).
        result = self.ledger.redact_record(
            record_id=c.record_id, reason="remove leak", actor="atlas"
        )

        # Target's record_id (= sha256) is unchanged.
        c_after = self.ledger.get(c.record_id)
        self.assertIsNotNone(c_after)
        self.assertEqual(c_after.record_id, c.record_id)
        self.assertTrue(c_after.is_redacted)

        # And every non-redacted ancestor still re-hashes to its record_id.
        v1 = self.ledger.verify_chain()
        self.assertTrue(
            v1["ok"],
            f"chain MUST verify post-redact; got {v1!r}",
        )
        # No mismatches; redacted leaf accounted for separately.
        self.assertEqual(v1["mismatches"], [])
        self.assertEqual(v1["missing_tombstone"], [])
        self.assertGreaterEqual(v1["redacted"], 1)
        # Tombstone is now itself a record on the chain.
        self.assertGreaterEqual(v1["recomputed"], 3)  # a, b, tombstone
        self.assertIsNotNone(self.ledger.get(result["tombstone_record_id"]))


if __name__ == "__main__":
    unittest.main()
