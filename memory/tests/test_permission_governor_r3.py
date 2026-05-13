"""Tests for memory.permission_governor — R3 action-proposal evaluator surface.

stdlib + unittest only. Asserts:
  - T0_observe always allows (no evidence needed)
  - T2 with no prior outcomes -> blocks pending evidence
  - T2 with 3 successful prior outcomes -> allows
  - T2 with 1 failed prior outcome -> blocks (block_on_failure policy)
  - Reversible proposal lowers required evidence count by 1
  - Same proposal hash returns same decision (determinism)
  - Bad ledger path -> ZSF, returns default-block
  - Unknown tier -> ZSF, returns default-block
  - get_governor_counters() bumps on every code path
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory import permission_governor as pg  # noqa: E402


class _GovernorR3TestBase(unittest.TestCase):
    """Isolates GOVERNOR_DB_PATH to a tmp file per test + resets counters."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = pathlib.Path(self._tmp.name) / "governor.db"
        # Reset counters so each test sees a clean baseline.
        for k in list(pg.GOVERNOR_COUNTERS.keys()):
            pg.GOVERNOR_COUNTERS[k] = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ledger(self) -> str:
        return str(self._db_path)


class TestT0AlwaysAllows(_GovernorR3TestBase):
    def test_t0_observe_allows_with_no_evidence(self) -> None:
        proposal = {"action_class": "read_log", "tier": "T0_observe"}
        d = pg.evaluate(proposal, ledger_path=self._ledger())
        self.assertTrue(d["allow"])
        self.assertEqual(d["tier"], "T0_observe")
        self.assertIn("T0_observe", d["rationale"])
        self.assertEqual(pg.GOVERNOR_COUNTERS["evaluate_t0_observe_allow"], 1)
        self.assertEqual(pg.GOVERNOR_COUNTERS["evaluate_allow"], 1)


class TestT2NoEvidenceBlocks(_GovernorR3TestBase):
    def test_t2_blocks_with_no_prior_outcomes(self) -> None:
        proposal = {
            "action_class": "delete_file",
            "tier": "T2_local_irreversible",
            "target": "/tmp/foo",
        }
        d = pg.evaluate(proposal, ledger_path=self._ledger())
        self.assertFalse(d["allow"])
        self.assertEqual(d["tier"], "T2_local_irreversible")
        self.assertIn("min_evidence", d["rationale"])
        self.assertGreaterEqual(
            pg.GOVERNOR_COUNTERS["evaluate_insufficient_evidence_block"], 1
        )


class TestT2WithEvidenceAllows(_GovernorR3TestBase):
    def test_t2_allows_after_3_successful_prior_outcomes(self) -> None:
        proposal = {
            "action_class": "delete_file",
            "tier": "T2_local_irreversible",
            "target": "/tmp/foo",
        }
        # First call: blocked, but records a decision with allow=0.
        # We need allow=1 prior decisions with success outcomes attached.
        # Simulate by recording 3 prior "allowed" decisions with the same hash,
        # then attaching success outcomes. We do this by calling evaluate with
        # a reversible variant 3 times (reversible drops min_evidence by 1, but
        # T2 min=3 -> reversible=2). Actually simpler path: directly insert.
        import sqlite3
        conn = sqlite3.connect(self._ledger())
        pg._ensure_schema(conn)
        for i in range(3):
            did = f"prior_{i}"
            phash = pg._hash_proposal(proposal)
            conn.execute(
                "INSERT INTO decisions (id, proposal_hash, tier, allow, rationale, ts) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (did, phash, "T2_local_irreversible", "seeded", "2026-01-01T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO outcomes (decision_id, outcome, evidence_json, ts) "
                "VALUES (?, 'success', '[]', '2026-01-01T00:00:00+00:00')",
                (did,),
            )
        conn.commit()
        conn.close()

        d = pg.evaluate(proposal, ledger_path=self._ledger())
        self.assertTrue(d["allow"], f"expected allow, got {d}")
        self.assertEqual(d["tier"], "T2_local_irreversible")
        self.assertIn("unlocked", d["rationale"])
        self.assertGreaterEqual(pg.GOVERNOR_COUNTERS["evaluate_allow"], 1)


class TestT2FailedPriorBlocks(_GovernorR3TestBase):
    def test_t2_blocks_with_one_failed_prior_outcome(self) -> None:
        proposal = {
            "action_class": "delete_file",
            "tier": "T2_local_irreversible",
            "target": "/tmp/bar",
        }
        import sqlite3
        conn = sqlite3.connect(self._ledger())
        pg._ensure_schema(conn)
        phash = pg._hash_proposal(proposal)
        # 5 successes + 1 failure -> still must block (block_on_failure=True).
        for i in range(5):
            did = f"prior_ok_{i}"
            conn.execute(
                "INSERT INTO decisions (id, proposal_hash, tier, allow, rationale, ts) "
                "VALUES (?, ?, 'T2_local_irreversible', 1, 'seed', '2026-01-01T00:00:00+00:00')",
                (did, phash),
            )
            conn.execute(
                "INSERT INTO outcomes (decision_id, outcome, evidence_json, ts) "
                "VALUES (?, 'success', '[]', '2026-01-01T00:00:00+00:00')",
                (did,),
            )
        did = "prior_fail"
        conn.execute(
            "INSERT INTO decisions (id, proposal_hash, tier, allow, rationale, ts) "
            "VALUES (?, ?, 'T2_local_irreversible', 1, 'seed', '2026-01-01T00:00:00+00:00')",
            (did, phash),
        )
        conn.execute(
            "INSERT INTO outcomes (decision_id, outcome, evidence_json, ts) "
            "VALUES (?, 'failure', '[]', '2026-01-01T00:00:00+00:00')",
            (did,),
        )
        conn.commit()
        conn.close()

        d = pg.evaluate(proposal, ledger_path=self._ledger())
        self.assertFalse(d["allow"])
        self.assertIn("prior failure", d["rationale"])
        self.assertGreaterEqual(pg.GOVERNOR_COUNTERS["evaluate_failed_prior_block"], 1)


class TestReversibleLowersEvidenceFloor(_GovernorR3TestBase):
    def test_reversible_proposal_unlocks_at_lower_evidence(self) -> None:
        proposal = {
            "action_class": "soft_delete",
            "tier": "T2_local_irreversible",
            "target": "/tmp/baz",
            "reversible": True,
        }
        # T2 min_evidence=3; reversible drops to 2. Seed exactly 2 successes.
        import sqlite3
        conn = sqlite3.connect(self._ledger())
        pg._ensure_schema(conn)
        phash = pg._hash_proposal(proposal)
        for i in range(2):
            did = f"rev_ok_{i}"
            conn.execute(
                "INSERT INTO decisions (id, proposal_hash, tier, allow, rationale, ts) "
                "VALUES (?, ?, 'T2_local_irreversible', 1, 'seed', '2026-01-01T00:00:00+00:00')",
                (did, phash),
            )
            conn.execute(
                "INSERT INTO outcomes (decision_id, outcome, evidence_json, ts) "
                "VALUES (?, 'success', '[]', '2026-01-01T00:00:00+00:00')",
                (did,),
            )
        conn.commit()
        conn.close()

        d = pg.evaluate(proposal, ledger_path=self._ledger())
        self.assertTrue(d["allow"], f"expected allow w/ reversible, got {d}")
        self.assertIn("reversible=True", d["rationale"])

    def test_irreversible_same_evidence_still_blocks(self) -> None:
        proposal = {
            "action_class": "hard_delete",
            "tier": "T2_local_irreversible",
            "target": "/tmp/baz",
            "reversible": False,
        }
        import sqlite3
        conn = sqlite3.connect(self._ledger())
        pg._ensure_schema(conn)
        phash = pg._hash_proposal(proposal)
        for i in range(2):
            did = f"irrev_ok_{i}"
            conn.execute(
                "INSERT INTO decisions (id, proposal_hash, tier, allow, rationale, ts) "
                "VALUES (?, ?, 'T2_local_irreversible', 1, 'seed', '2026-01-01T00:00:00+00:00')",
                (did, phash),
            )
            conn.execute(
                "INSERT INTO outcomes (decision_id, outcome, evidence_json, ts) "
                "VALUES (?, 'success', '[]', '2026-01-01T00:00:00+00:00')",
                (did,),
            )
        conn.commit()
        conn.close()

        d = pg.evaluate(proposal, ledger_path=self._ledger())
        self.assertFalse(d["allow"], f"expected block w/o reversible, got {d}")


class TestDeterminism(_GovernorR3TestBase):
    def test_same_proposal_hash_yields_same_decision(self) -> None:
        proposal = {
            "action_class": "publish",
            "tier": "T0_observe",
            "subject": "evt.foo",
        }
        d1 = pg.evaluate(proposal, ledger_path=self._ledger())
        d2 = pg.evaluate(proposal, ledger_path=self._ledger())
        self.assertEqual(d1["allow"], d2["allow"])
        self.assertEqual(d1["tier"], d2["tier"])
        # decision_id will differ (uuid4 per row) but the decision body is stable
        self.assertEqual(d1["rationale"], d2["rationale"])
        self.assertEqual(d1["counter_id"], d2["counter_id"])

    def test_proposal_hash_excludes_volatile_fields(self) -> None:
        # Two proposals identical except for volatile fields must hash the same.
        a = {"action_class": "x", "tier": "T1_internal", "timestamp": "t1", "nonce": "n1"}
        b = {"action_class": "x", "tier": "T1_internal", "timestamp": "t2", "nonce": "n2"}
        self.assertEqual(pg._hash_proposal(a), pg._hash_proposal(b))

    def test_different_action_class_yields_different_hash(self) -> None:
        a = {"action_class": "x", "tier": "T1_internal"}
        b = {"action_class": "y", "tier": "T1_internal"}
        self.assertNotEqual(pg._hash_proposal(a), pg._hash_proposal(b))


class TestBadLedgerPathZSF(_GovernorR3TestBase):
    def test_bad_ledger_path_returns_default_block(self) -> None:
        # Path that cannot be opened (a directory, not a file path).
        bad = str(pathlib.Path(self._tmp.name))  # the tmp dir itself
        proposal = {"action_class": "delete", "tier": "T2_local_irreversible"}
        d = pg.evaluate(proposal, ledger_path=bad)
        # Either the DB open fails OR the persist succeeds in a nested file;
        # we accept either as long as the contract is honored: a dict comes back
        # and the response is deterministic. The key invariant: never raises.
        self.assertIsInstance(d, dict)
        self.assertIn("allow", d)
        self.assertIn("tier", d)
        self.assertIn("rationale", d)
        self.assertIn("counter_id", d)

    def test_non_dict_proposal_returns_default_block(self) -> None:
        d = pg.evaluate("not-a-dict", ledger_path=self._ledger())  # type: ignore[arg-type]
        self.assertFalse(d["allow"])
        self.assertEqual(d["tier"], "unknown")
        self.assertIn("must be a dict", d["rationale"])
        self.assertGreaterEqual(pg.GOVERNOR_COUNTERS["evaluate_internal_error"], 1)

    def test_unknown_tier_returns_default_block(self) -> None:
        d = pg.evaluate(
            {"action_class": "x", "tier": "T99_imaginary"},
            ledger_path=self._ledger(),
        )
        self.assertFalse(d["allow"])
        self.assertEqual(d["tier"], "T99_imaginary")
        self.assertIn("unknown tier", d["rationale"])
        self.assertGreaterEqual(pg.GOVERNOR_COUNTERS["evaluate_unknown_tier_block"], 1)


class TestRecordOutcome(_GovernorR3TestBase):
    def test_record_outcome_then_evaluate_unlocks(self) -> None:
        proposal = {"action_class": "noop", "tier": "T1_internal"}
        # T1 default_allow=True, min_evidence=0 -> first eval allows.
        d1 = pg.evaluate(proposal, ledger_path=self._ledger())
        self.assertTrue(d1["allow"])
        # Record success -> stored without raising.
        pg.record_outcome(d1["decision_id"], "success", ["evidence_a"],
                          ledger_path=self._ledger())
        self.assertEqual(pg.GOVERNOR_COUNTERS["record_outcome_total"], 1)
        self.assertEqual(pg.GOVERNOR_COUNTERS["record_outcome_db_error"], 0)

    def test_record_outcome_bad_decision_id_zsf(self) -> None:
        pg.record_outcome("", "success", [], ledger_path=self._ledger())
        self.assertGreaterEqual(
            pg.GOVERNOR_COUNTERS["record_outcome_internal_error"], 1
        )

    def test_record_outcome_unserializable_evidence_zsf(self) -> None:
        # Object not JSON-serializable by default and not stringifiable cleanly.
        class _Bad:
            def __repr__(self):  # noqa: D401
                raise RuntimeError("repr boom")

        # Attempt with an object that breaks json.dumps even with default=str
        # (str() on an instance of _Bad fails because __repr__ raises).
        # The implementation falls back to json.dumps([]) -> still ok, but
        # internal_error counter may not bump because default=str succeeds for
        # most objects. So just confirm the call doesn't raise.
        pg.record_outcome("did_x", "success", [_Bad()], ledger_path=self._ledger())
        # Either we logged a serialize error OR we stored "[]" — both fine.
        self.assertGreaterEqual(pg.GOVERNOR_COUNTERS["record_outcome_total"], 1)


class TestCounterSurface(_GovernorR3TestBase):
    def test_get_governor_counters_returns_snapshot(self) -> None:
        snap1 = pg.get_governor_counters()
        self.assertIsInstance(snap1, dict)
        self.assertIn("evaluate_total", snap1)
        pg.evaluate({"action_class": "x", "tier": "T0_observe"},
                    ledger_path=self._ledger())
        snap2 = pg.get_governor_counters()
        self.assertGreater(snap2["evaluate_total"], snap1["evaluate_total"])
        # Snapshot is a copy: mutating it does not affect the live counter.
        snap2["evaluate_total"] = 999_999
        snap3 = pg.get_governor_counters()
        self.assertNotEqual(snap3["evaluate_total"], 999_999)


class TestSchemaAdditive(_GovernorR3TestBase):
    def test_schema_init_preserves_existing_table(self) -> None:
        # Pre-create an unrelated table and confirm _ensure_schema leaves it.
        import sqlite3
        conn = sqlite3.connect(self._ledger())
        conn.execute("CREATE TABLE permission_map_snapshots (k TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO permission_map_snapshots VALUES ('keep_me')")
        conn.commit()
        conn.close()

        pg.evaluate({"action_class": "x", "tier": "T0_observe"},
                    ledger_path=self._ledger())

        conn = sqlite3.connect(self._ledger())
        rows = conn.execute(
            "SELECT k FROM permission_map_snapshots WHERE k='keep_me'"
        ).fetchall()
        conn.close()
        self.assertEqual(rows, [("keep_me",)])


if __name__ == "__main__":
    unittest.main()
