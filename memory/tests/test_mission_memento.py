"""Tests for memory.evidence_ledger.record_mission_memento (Phase-13 GG5).

Mission Mementos are the OpenPath ledger record kind for every dispatched
action: ``mission_id``, ``goal``, ``decision``, ``actions_taken``,
``evidence_ids``, ``final_state``, ``unresolved``. Tests verify:

  1. Write + read-back: a memento can be written and re-fetched.
  2. Required-key validation: missing/empty required strings raise.
  3. List-typed fields enforce list type.
  4. ``extra`` cannot stomp required keys.
  5. Chain integrity: ``parent_ids`` are preserved end-to-end (lineage walk).

stdlib + unittest only. Isolated tmp DB per test.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

# Make ``memory`` importable when tests run from arbitrary cwd.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory import evidence_ledger as el  # noqa: E402


class _MementoBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db = pathlib.Path(self._tmp.name) / "ledger.db"
        # Reset module counters for test isolation.
        for k in list(el.COUNTERS.keys()):
            el.COUNTERS[k] = 0
        self.ledger = el.EvidenceLedger(db_path=self._db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _well_formed_kwargs(**overrides):
        base = dict(
            mission_id="env-001",
            goal="restart fleet daemon on mac3",
            decision="execute_and_log",
            actions_taken=["fleet.daemon.restart"],
            evidence_ids=[],
            final_state="succeeded",
            unresolved=[],
        )
        base.update(overrides)
        return base


# ---------------------------------------------------------------------------
# 1. Write + read-back
# ---------------------------------------------------------------------------


class TestWriteAndReadback(_MementoBase):
    def test_write_returns_record_with_correct_kind(self) -> None:
        rec = el.record_mission_memento(self.ledger, **self._well_formed_kwargs())

        self.assertEqual(rec.kind, "mission_memento")
        self.assertEqual(rec.content["mission_id"], "env-001")
        self.assertEqual(rec.content["decision"], "execute_and_log")
        self.assertEqual(rec.content["final_state"], "succeeded")
        self.assertEqual(rec.content["actions_taken"], ["fleet.daemon.restart"])

    def test_readback_via_get_returns_same_payload(self) -> None:
        rec = el.record_mission_memento(self.ledger, **self._well_formed_kwargs())
        fetched = self.ledger.get(rec.record_id)

        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.record_id, rec.record_id)
        self.assertEqual(fetched.content, rec.content)

    def test_query_by_kind_returns_memento(self) -> None:
        el.record_mission_memento(self.ledger, **self._well_formed_kwargs())
        rows = self.ledger.query(kind="mission_memento")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "mission_memento")

    def test_records_total_counter_increments(self) -> None:
        el.record_mission_memento(self.ledger, **self._well_formed_kwargs())

        self.assertEqual(el.COUNTERS["mission_memento_records_total"], 1)
        # Underlying ledger counter also increments.
        self.assertEqual(el.COUNTERS["evidence_records_total"], 1)


# ---------------------------------------------------------------------------
# 2. Required-key validation
# ---------------------------------------------------------------------------


class TestRequiredKeyValidation(_MementoBase):
    def test_empty_mission_id_raises(self) -> None:
        kwargs = self._well_formed_kwargs(mission_id="")
        with self.assertRaises(ValueError):
            el.record_mission_memento(self.ledger, **kwargs)
        self.assertEqual(el.COUNTERS["mission_memento_invalid_total"], 1)

    def test_whitespace_only_goal_raises(self) -> None:
        kwargs = self._well_formed_kwargs(goal="   ")
        with self.assertRaises(ValueError):
            el.record_mission_memento(self.ledger, **kwargs)
        self.assertEqual(el.COUNTERS["mission_memento_invalid_total"], 1)

    def test_empty_decision_raises(self) -> None:
        kwargs = self._well_formed_kwargs(decision="")
        with self.assertRaises(ValueError):
            el.record_mission_memento(self.ledger, **kwargs)

    def test_empty_final_state_raises(self) -> None:
        kwargs = self._well_formed_kwargs(final_state="")
        with self.assertRaises(ValueError):
            el.record_mission_memento(self.ledger, **kwargs)

    def test_actions_taken_not_a_list_raises(self) -> None:
        # str is iterable but not a list — must raise.
        kwargs = self._well_formed_kwargs(actions_taken="single.action")
        with self.assertRaises(ValueError):
            el.record_mission_memento(self.ledger, **kwargs)
        self.assertEqual(el.COUNTERS["mission_memento_invalid_total"], 1)

    def test_evidence_ids_dict_raises(self) -> None:
        kwargs = self._well_formed_kwargs(evidence_ids={"id": "x"})
        with self.assertRaises(ValueError):
            el.record_mission_memento(self.ledger, **kwargs)


# ---------------------------------------------------------------------------
# 3. Empty lists ARE allowed for list-typed fields
# ---------------------------------------------------------------------------


class TestEmptyListsAllowed(_MementoBase):
    def test_propose_envelope_memento_with_no_actions(self) -> None:
        # Realistic: lane-3 dispatch with envelope=None -> propose_envelope.
        # No action was actually taken, no evidence sub-records, no unresolved
        # arbiter cases yet.
        rec = el.record_mission_memento(
            self.ledger,
            mission_id="env-002",
            goal="propose envelope for sensitive edit",
            decision="propose_envelope",
            actions_taken=[],
            evidence_ids=[],
            final_state="in_review",
            unresolved=[],
        )
        self.assertEqual(rec.content["actions_taken"], [])
        self.assertEqual(rec.content["unresolved"], [])


# ---------------------------------------------------------------------------
# 4. ``extra`` cannot collide with required keys
# ---------------------------------------------------------------------------


class TestExtraCollision(_MementoBase):
    def test_extra_collision_with_decision_raises(self) -> None:
        kwargs = self._well_formed_kwargs()
        with self.assertRaises(ValueError) as cm:
            el.record_mission_memento(
                self.ledger, extra={"decision": "stomp_attempt"}, **kwargs
            )
        self.assertIn("collides", str(cm.exception))
        self.assertEqual(el.COUNTERS["mission_memento_invalid_total"], 1)

    def test_extra_non_collision_is_merged(self) -> None:
        rec = el.record_mission_memento(
            self.ledger,
            extra={"router_via": "chief_audit.process_findings_batch"},
            **self._well_formed_kwargs(),
        )
        self.assertEqual(
            rec.content["router_via"], "chief_audit.process_findings_batch"
        )


# ---------------------------------------------------------------------------
# 5. Chain integrity — parent_id preserved across write
# ---------------------------------------------------------------------------


class TestChainIntegrity(_MementoBase):
    def test_parent_id_preserved_in_lineage_walk(self) -> None:
        # First write a parent record (any kind — use audit since it's
        # already in the enum and exercises a different code path).
        parent_rec = self.ledger.record(
            content={"summary": "GG3 chief_audit dispatch"}, kind="audit"
        )

        # Now write a memento with that parent.
        memento = el.record_mission_memento(
            self.ledger,
            parent_ids=[parent_rec.record_id],
            **self._well_formed_kwargs(),
        )

        # Lineage walk from the memento returns the parent.
        ancestors = self.ledger.lineage(memento.record_id)
        self.assertEqual(len(ancestors), 1)
        self.assertEqual(ancestors[0].record_id, parent_rec.record_id)
        self.assertIn(parent_rec.record_id, memento.parent_ids)

    def test_two_parents_both_preserved(self) -> None:
        p1 = self.ledger.record(content={"a": 1}, kind="audit")
        p2 = self.ledger.record(content={"b": 2}, kind="audit")
        memento = el.record_mission_memento(
            self.ledger,
            parent_ids=[p1.record_id, p2.record_id],
            **self._well_formed_kwargs(),
        )
        self.assertEqual(memento.parent_ids, frozenset({p1.record_id, p2.record_id}))
        ancestors = self.ledger.lineage(memento.record_id)
        ancestor_ids = {a.record_id for a in ancestors}
        self.assertEqual(ancestor_ids, {p1.record_id, p2.record_id})

    def test_idempotent_rewrite_does_not_double_count(self) -> None:
        kwargs = self._well_formed_kwargs()
        first = el.record_mission_memento(self.ledger, **kwargs)
        second = el.record_mission_memento(self.ledger, **kwargs)

        # Same content -> same record_id -> no second row.
        self.assertEqual(first.record_id, second.record_id)
        rows = self.ledger.query(kind="mission_memento")
        self.assertEqual(len(rows), 1)
        # New-write counter only bumped once.
        self.assertEqual(el.COUNTERS["mission_memento_records_total"], 1)
        self.assertEqual(el.COUNTERS["evidence_dedupe_hits_total"], 1)


# ---------------------------------------------------------------------------
# 6. Type guard on ledger argument
# ---------------------------------------------------------------------------


class TestLedgerTypeGuard(_MementoBase):
    def test_non_ledger_raises_typeerror(self) -> None:
        with self.assertRaises(TypeError):
            el.record_mission_memento(  # type: ignore[arg-type]
                "not a ledger", **self._well_formed_kwargs()
            )


if __name__ == "__main__":
    unittest.main()
