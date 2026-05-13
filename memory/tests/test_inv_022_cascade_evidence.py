"""Tests for CTXDNA-INV-022 — peer_messages_via_cascade_only.

INV-022 enforces that every peer_message ActionProposal:
  (a) has via_cascade=True (cascade traversal claim), AND
  (b) carries at least one MessageAttempt evidence ref
      (`message_attempt:<channel>:<msg_id>`) proving the cascade actually
      executed at least one transport.

Both conditions must hold. Either alone is insufficient — a proposal can
claim cascade traversal without proof, and a stray attempt-record without
the cascade flag would mean the dispatcher was never invoked.

See: .fleet/audits/2026-05-08-mf-channel-invariance-gap-analysis.md (Gap 3)
"""

from __future__ import annotations

import unittest

from memory.invariants import (
    INVARIANTS,
    ActionProposal,
    evaluate,
    get_failure_counters,
)


class TestInv022Registry(unittest.TestCase):
    def test_inv022_in_registry(self) -> None:
        self.assertIn("CTXDNA-INV-022", INVARIANTS)
        spec = INVARIANTS["CTXDNA-INV-022"]
        self.assertEqual(spec.severity, "fatal")
        self.assertIn("peer_message", spec.applies_to)
        self.assertIn("MessageAttempt", spec.rule_text)


class TestInv022PassPath(unittest.TestCase):
    def test_peer_message_with_cascade_and_evidence_passes(self) -> None:
        proposal = ActionProposal(
            action_class="peer_message",
            blast_radius="low",
            evidence_refs=[
                "message_attempt:P1_nats:msg-abc",
                "message_attempt:P2_http:msg-abc",
            ],
            via_cascade=True,
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-022", report.triggered)


class TestInv022FailPaths(unittest.TestCase):
    def test_peer_message_without_cascade_blocks(self) -> None:
        proposal = ActionProposal(
            action_class="peer_message",
            blast_radius="low",
            evidence_refs=["message_attempt:P1_nats:msg-1"],
            via_cascade=False,
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-022", report.triggered)
        self.assertEqual(report.decision, "block")
        self.assertTrue(any("via_cascade" in r for r in report.reasons))

    def test_peer_message_cascade_but_no_evidence_blocks(self) -> None:
        # via_cascade=True but no MessageAttempt evidence refs at all.
        proposal = ActionProposal(
            action_class="peer_message",
            blast_radius="low",
            evidence_refs=[],
            via_cascade=True,
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-022", report.triggered)
        self.assertEqual(report.decision, "block")
        self.assertTrue(
            any("MessageAttempt" in r or "message_attempt" in r for r in report.reasons)
        )

    def test_peer_message_evidence_missing_attempt_prefix_blocks(self) -> None:
        # Evidence refs present but none of them are MessageAttempt records.
        proposal = ActionProposal(
            action_class="peer_message",
            blast_radius="low",
            evidence_refs=["audit:some-other-id", "ev:something-else"],
            via_cascade=True,
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-022", report.triggered)

    def test_peer_message_via_cascade_none_blocks(self) -> None:
        # via_cascade left None -> not asserted -> block.
        proposal = ActionProposal(
            action_class="peer_message",
            blast_radius="low",
            evidence_refs=["message_attempt:P1_nats:msg-1"],
            via_cascade=None,
        )
        report = evaluate(proposal)
        self.assertIn("CTXDNA-INV-022", report.triggered)


class TestInv022ScopedToPeerMessages(unittest.TestCase):
    def test_non_peer_message_unaffected(self) -> None:
        # memory_change must not be gated by INV-022.
        proposal = ActionProposal(
            action_class="memory_change",
            blast_radius="low",
            evidence_refs=["ev:1"],
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-022", report.triggered)

    def test_run_tool_unaffected(self) -> None:
        proposal = ActionProposal(
            action_class="run_tool",
            blast_radius="low",
            evidence_refs=["ev:1"],
        )
        report = evaluate(proposal)
        self.assertNotIn("CTXDNA-INV-022", report.triggered)


class TestInv022CountersZsf(unittest.TestCase):
    """Zero-Silent-Failures: every block path must increment a counter."""

    def test_missing_cascade_bumps_counter(self) -> None:
        before = get_failure_counters().get("inv022_via_cascade_missing", 0)
        proposal = ActionProposal(
            action_class="peer_message",
            evidence_refs=["message_attempt:P1_nats:msg-1"],
            via_cascade=False,
        )
        evaluate(proposal)
        after = get_failure_counters().get("inv022_via_cascade_missing", 0)
        self.assertGreater(after, before)

    def test_missing_evidence_bumps_counter(self) -> None:
        before = get_failure_counters().get("inv022_no_message_attempt", 0)
        proposal = ActionProposal(
            action_class="peer_message",
            evidence_refs=[],
            via_cascade=True,
        )
        evaluate(proposal)
        after = get_failure_counters().get("inv022_no_message_attempt", 0)
        self.assertGreater(after, before)


if __name__ == "__main__":
    unittest.main()
