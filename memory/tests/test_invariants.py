"""Tests for memory.invariants — v0 TrialBench invariant kernel.

Covered (minimum required):
    1. schema_version mismatch -> block
    2. INV-021 unknown hash -> block (with lockfile present)
    3. INV-021 known hash -> allow (with lockfile present)

Plus a few defensive tests that lock the API contract.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.invariants import (
    INVARIANTS,
    SCHEMA_VERSION,
    ActionProposal,
    InvariantReport,
    evaluate,
    get_failure_counters,
)


def _write_lockfile(tmpdir: Path, hashes: list[str]) -> Path:
    lock_path = tmpdir / "trialbench-protocol.lock.json"
    payload = {"protocols": [{"hash": h} for h in hashes]}
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    return lock_path


class TestApiContract(unittest.TestCase):
    def test_schema_version_is_int(self) -> None:
        self.assertIsInstance(SCHEMA_VERSION, int)
        self.assertGreaterEqual(SCHEMA_VERSION, 1)

    def test_registry_has_22_ctxdna_invariants(self) -> None:
        # 22 CTXDNA-INV-001..022 plus MFINV-C* family — exact count below.
        for i in range(1, 23):
            key = f"CTXDNA-INV-{i:03d}"
            self.assertIn(key, INVARIANTS, f"missing {key}")
            spec = INVARIANTS[key]
            self.assertTrue(spec.name)
            self.assertTrue(spec.rule_text)
            self.assertIsInstance(spec.applies_to, list)
            self.assertIn(spec.severity, {"low", "medium", "high", "fatal"})

    def test_registry_includes_mfinv_c01(self) -> None:
        self.assertIn("MFINV-C01", INVARIANTS)
        spec = INVARIANTS["MFINV-C01"]
        self.assertEqual(spec.severity, "fatal")
        self.assertIn("peer_message", spec.applies_to)

    def test_registry_total_count(self) -> None:
        # 22 CTXDNA + 1 MFINV-C01 = 23.
        self.assertEqual(len(INVARIANTS), 23)

    def test_inv021_is_protocol_lock_rule(self) -> None:
        spec = INVARIANTS["CTXDNA-INV-021"]
        self.assertIn("trialbench-protocol.lock.json", spec.rule_text)
        self.assertIn("no bypass", spec.rule_text.lower())
        self.assertEqual(spec.severity, "fatal")


class TestSchemaVersion(unittest.TestCase):
    def test_mismatch_blocks(self) -> None:
        proposal = ActionProposal(
            action_class="memory_change",
            blast_radius="low",
            evidence_refs=[],
            schema_version=SCHEMA_VERSION + 99,
        )
        report = evaluate(proposal)
        self.assertIsInstance(report, InvariantReport)
        self.assertEqual(report.decision, "block")
        self.assertTrue(any("schema_version" in r for r in report.reasons))

    def test_match_does_not_block_on_schema(self) -> None:
        proposal = ActionProposal(
            action_class="memory_change",
            blast_radius="low",
            evidence_refs=["ev:1"],
            schema_version=SCHEMA_VERSION,
        )
        report = evaluate(proposal)
        self.assertNotEqual(report.decision, "block")


class TestInv021ProtocolLock(unittest.TestCase):
    def test_unknown_hash_blocks(self) -> None:
        with TemporaryDirectory() as td:
            lock = _write_lockfile(Path(td), ["sha256:known-1", "sha256:known-2"])
            proposal = ActionProposal(
                action_class="trial_dispatch",
                blast_radius="medium",
                evidence_refs=["ev:1"],
                trial_protocol_hash="sha256:unknown-xyz",
            )
            report = evaluate(proposal, lockfile_path=lock)
            self.assertEqual(report.decision, "block")
            self.assertIn("CTXDNA-INV-021", report.triggered)
            self.assertTrue(
                any("not present" in r or "unknown" in r.lower() for r in report.reasons)
            )

    def test_known_hash_allows(self) -> None:
        with TemporaryDirectory() as td:
            lock = _write_lockfile(Path(td), ["sha256:locked-protocol-A"])
            proposal = ActionProposal(
                action_class="trial_dispatch",
                blast_radius="low",
                evidence_refs=["ev:1"],
                trial_protocol_hash="sha256:locked-protocol-A",
            )
            report = evaluate(proposal, lockfile_path=lock)
            self.assertNotEqual(report.decision, "block")
            self.assertNotIn("CTXDNA-INV-021", report.triggered)

    def test_missing_hash_on_trial_dispatch_blocks(self) -> None:
        with TemporaryDirectory() as td:
            lock = _write_lockfile(Path(td), ["sha256:known-1"])
            proposal = ActionProposal(
                action_class="trial_dispatch",
                blast_radius="low",
                evidence_refs=["ev:1"],
                trial_protocol_hash=None,
            )
            report = evaluate(proposal, lockfile_path=lock)
            self.assertEqual(report.decision, "block")
            self.assertIn("CTXDNA-INV-021", report.triggered)

    def test_inv021_skipped_for_non_trial_action(self) -> None:
        # No lockfile, non-trial action — INV-021 must not trigger.
        proposal = ActionProposal(
            action_class="memory_change",
            blast_radius="low",
            evidence_refs=["ev:1"],
        )
        report = evaluate(proposal, lockfile_path=Path("/nonexistent/path.json"))
        self.assertNotIn("CTXDNA-INV-021", report.triggered)

    def test_corrupt_lockfile_blocks_trial_dispatch(self) -> None:
        with TemporaryDirectory() as td:
            bad = Path(td) / "bad.lock.json"
            bad.write_text("{not-json", encoding="utf-8")
            proposal = ActionProposal(
                action_class="trial_dispatch",
                blast_radius="low",
                evidence_refs=["ev:1"],
                trial_protocol_hash="sha256:anything",
            )
            report = evaluate(proposal, lockfile_path=bad)
            self.assertEqual(report.decision, "block")
            self.assertIn("CTXDNA-INV-021", report.triggered)
            counters = get_failure_counters()
            self.assertGreaterEqual(counters.get("lockfile_parse_error", 0), 1)


class TestOtherInvariants(unittest.TestCase):
    def test_inv001_permission_change_without_evidence_escalates(self) -> None:
        proposal = ActionProposal(
            action_class="permission_change",
            blast_radius="low",
            evidence_refs=[],
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-001", report.triggered)
        # high severity -> escalate (not block)
        self.assertIn(report.decision, {"escalate", "block"})

    def test_inv007_high_blast_without_evidence_blocks(self) -> None:
        proposal = ActionProposal(
            action_class="run_tool",
            blast_radius="fleet_wide",
            evidence_refs=[],
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-007", report.triggered)
        self.assertEqual(report.decision, "block")

    def test_clean_proposal_allows(self) -> None:
        proposal = ActionProposal(
            action_class="memory_change",
            blast_radius="low",
            evidence_refs=["ev:1"],
        )
        report = evaluate(proposal)
        self.assertEqual(report.decision, "allow")
        self.assertEqual(report.triggered, [])


class TestInv016ValidatedAbstention(unittest.TestCase):
    def test_abstention_without_posthoc_blocks(self) -> None:
        # score_emit + claimed_abstention=True, no posthoc validation -> trigger.
        proposal = ActionProposal(
            action_class="score_emit",
            evidence_refs=["ev:1"],
            claimed_correctness_verified=True,  # satisfy INV-013
            claimed_abstention=True,
            posthoc_correctness_validated=None,
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-016", report.triggered)
        self.assertTrue(any("posthoc" in r.lower() for r in report.reasons))

    def test_abstention_with_posthoc_allows(self) -> None:
        proposal = ActionProposal(
            action_class="score_emit",
            evidence_refs=["ev:1"],
            claimed_correctness_verified=True,
            claimed_abstention=True,
            posthoc_correctness_validated=True,
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-016", report.triggered)

    def test_score_emit_without_abstention_claim_unaffected(self) -> None:
        # claimed_abstention left None -> INV-016 must not trigger.
        proposal = ActionProposal(
            action_class="score_emit",
            evidence_refs=["ev:1"],
            claimed_correctness_verified=True,
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-016", report.triggered)


class TestInv017ToolDisciplineBidirectional(unittest.TestCase):
    def test_missing_underuse_blocks(self) -> None:
        proposal = ActionProposal(
            action_class="run_tool",
            evidence_refs=["ev:1"],
            tool_discipline_score_emitted=True,
            overuse_count=2,
            underuse_count=None,
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-017", report.triggered)
        self.assertTrue(any("underuse_count" in r for r in report.reasons))

    def test_both_present_allows(self) -> None:
        proposal = ActionProposal(
            action_class="run_tool",
            evidence_refs=["ev:1"],
            tool_discipline_score_emitted=True,
            overuse_count=0,
            underuse_count=1,
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-017", report.triggered)

    def test_negative_count_blocks(self) -> None:
        proposal = ActionProposal(
            action_class="run_tool",
            evidence_refs=["ev:1"],
            tool_discipline_score_emitted=True,
            overuse_count=-1,
            underuse_count=2,
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-017", report.triggered)

    def test_run_tool_without_score_unaffected(self) -> None:
        proposal = ActionProposal(
            action_class="run_tool",
            evidence_refs=["ev:1"],
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-017", report.triggered)


class TestInv018CuratedLearningTraces(unittest.TestCase):
    def test_uncurated_blocks(self) -> None:
        proposal = ActionProposal(
            action_class="learn_from_trace",
            evidence_refs=["ev:1"],
            trace_curation_passed=False,
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-018", report.triggered)

    def test_unset_blocks(self) -> None:
        # learn_from_trace with no curation assertion -> trigger (escalate).
        proposal = ActionProposal(
            action_class="learn_from_trace",
            evidence_refs=["ev:1"],
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-018", report.triggered)
        self.assertIn(report.decision, {"escalate", "block"})

    def test_curated_allows(self) -> None:
        proposal = ActionProposal(
            action_class="learn_from_trace",
            evidence_refs=["ev:1"],
            trace_curation_passed=True,
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-018", report.triggered)


class TestInv019InterventionValue(unittest.TestCase):
    def test_good_intervention_without_outcome_blocks(self) -> None:
        proposal = ActionProposal(
            action_class="score_emit",
            evidence_refs=["ev:1"],
            claimed_correctness_verified=True,
            scored_as_good_intervention=True,
            intervention_outcome=None,
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-019", report.triggered)

    def test_good_intervention_with_invalid_outcome_blocks(self) -> None:
        proposal = ActionProposal(
            action_class="score_emit",
            evidence_refs=["ev:1"],
            claimed_correctness_verified=True,
            scored_as_good_intervention=True,
            intervention_outcome="vibes_improved",
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-019", report.triggered)

    def test_good_intervention_with_valid_outcome_allows(self) -> None:
        proposal = ActionProposal(
            action_class="score_emit",
            evidence_refs=["ev:1"],
            claimed_correctness_verified=True,
            scored_as_good_intervention=True,
            intervention_outcome="correctness_improved",
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-019", report.triggered)


class TestInv020EscalationNotEvidence(unittest.TestCase):
    def test_ask_chief_without_local_assessment_blocks(self) -> None:
        proposal = ActionProposal(
            action_class="ask_chief",
            evidence_refs=["ev:1"],
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-020", report.triggered)
        self.assertIn(report.decision, {"escalate", "block"})

    def test_ask_chief_with_local_assessment_allows(self) -> None:
        proposal = ActionProposal(
            action_class="ask_chief",
            evidence_refs=["ev:1"],
            local_assessment_done=True,
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-020", report.triggered)

    def test_inv020_not_triggered_for_other_actions(self) -> None:
        proposal = ActionProposal(
            action_class="memory_change",
            blast_radius="low",
            evidence_refs=["ev:1"],
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-020", report.triggered)


class TestMfinvC01ChannelPriority(unittest.TestCase):
    def test_peer_message_via_cascade_passes(self) -> None:
        proposal = ActionProposal(
            action_class="peer_message",
            blast_radius="low",
            evidence_refs=["ev:1"],
            via_cascade=True,
        )
        report = evaluate(proposal)
        self.assertNotIn("MFINV-C01", report.triggered)

    def test_peer_message_without_cascade_blocks(self) -> None:
        proposal = ActionProposal(
            action_class="peer_message",
            blast_radius="low",
            evidence_refs=["ev:1"],
            via_cascade=None,
            caller_path="some.random.module",
        )
        report = evaluate(proposal)
        self.assertIn("MFINV-C01", report.triggered)
        self.assertEqual(report.decision, "block")
        self.assertTrue(any("MFINV-C01" in r for r in report.reasons))

    def test_peer_message_allowlisted_caller_passes(self) -> None:
        # Dispatcher itself is allowed to bypass via_cascade.
        proposal = ActionProposal(
            action_class="peer_message",
            blast_radius="low",
            evidence_refs=["ev:1"],
            via_cascade=False,
            caller_path="multifleet.channel_priority.send",
        )
        report = evaluate(proposal)
        self.assertNotIn("MFINV-C01", report.triggered)

    def test_non_peer_message_unaffected(self) -> None:
        # Other action classes must NOT be gated by MFINV-C01.
        proposal = ActionProposal(
            action_class="memory_change",
            blast_radius="low",
            evidence_refs=["ev:1"],
        )
        report = evaluate(proposal)
        self.assertNotIn("MFINV-C01", report.triggered)
        self.assertEqual(report.decision, "allow")


class TestBackwardCompatActionProposal(unittest.TestCase):
    def test_minimal_proposal_still_constructs(self) -> None:
        # All new fields default to None — the original 1-field constructor
        # signature must keep working.
        proposal = ActionProposal(action_class="memory_change")
        self.assertIsNone(proposal.claimed_abstention)
        self.assertIsNone(proposal.tool_discipline_score_emitted)
        self.assertIsNone(proposal.trace_curation_passed)
        self.assertIsNone(proposal.scored_as_good_intervention)
        self.assertIsNone(proposal.local_assessment_done)


if __name__ == "__main__":
    unittest.main()
