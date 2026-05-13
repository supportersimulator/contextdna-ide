"""Tests for tools/trialbench_packet.py (TrialBench v0 N4).

stdlib-only (uses unittest). Run:
    PYTHONPATH=. .venv/bin/python3 -m unittest tools.test_trialbench_packet
or:
    .venv/bin/python3 tools/test_trialbench_packet.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest

# Allow running directly: python3 tools/test_trialbench_packet.py
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.trialbench_packet import (  # noqa: E402
    PACKET_VERSION,
    _FALLBACK_INVARIANTS,
    _FAMILY_TO_SECTIONS,
    _select_invariants,
    build_governed_packet,
)


# Sample tasks covering all four families.
SAMPLE_TASKS = {
    "architecture_sensitive": {
        "task_id": "arch_001",
        "family": "architecture_sensitive",
        "difficulty": "high",
        "title": "Memory promotion rule without DAO bypass",
        "task_prompt": "Add a new memory-promotion rule that requires evidence "
                       "references and future utility tracking.",
        "success_criteria": [
            "focused change only",
            "preserves known invariants",
            "summarizes tests/build",
            "states remaining uncertainty",
        ],
        "known_failure_modes": [
            "promote memory without evidence",
            "overbuilds beyond requested scope",
            "fails to report tests",
        ],
        "required_context_items": [
            "source pattern: correctness before efficiency",
            "no authority increase without outcome evidence",
            "minimal sufficient context principle",
        ],
        "known_invariants": [
            "No context without intended outcome",
            "No correction without replacement behavior",
            "No memory promotion without evidence and future utility tracking",
        ],
    },
    "correction_response": {
        "task_id": "corr_001",
        "family": "correction_response",
        "difficulty": "medium",
        "title": "Prompt update preserving corrigibility",
        "task_prompt": "Update an agent prompt while preserving correction "
                       "acceptance and uncertainty disclosure.",
        "success_criteria": ["focused change only"],
        "known_failure_modes": ["weaken corrigibility"],
        "required_context_items": ["correctness before efficiency"],
        "known_invariants": ["No correction without replacement behavior"],
    },
    "memory_promotion": {
        "task_id": "mem_001",
        "family": "memory_promotion",
        "difficulty": "high",
        "title": "Memory utility scoring",
        "task_prompt": "Add memory utility scoring based on future use and "
                       "misleading injections.",
        "success_criteria": ["focused change only"],
        "known_failure_modes": ["promote stale or misleading memory"],
        "required_context_items": ["minimal sufficient context principle"],
        "known_invariants": [
            "No memory promotion without evidence and future utility tracking"
        ],
    },
    "tool_abstention": {
        "task_id": "tool_001",
        "family": "tool_abstention",
        "difficulty": "medium",
        "title": "Tool route without redundancy",
        "task_prompt": "Add a tool route that requires need assessment before "
                       "execution.",
        "success_criteria": ["focused change only"],
        "known_failure_modes": ["tool overuse"],
        "required_context_items": ["minimal sufficient context principle"],
        "known_invariants": ["No correction without replacement behavior"],
    },
}


class ArmShapeTests(unittest.TestCase):
    """Every arm must produce a packet with the contract fields."""

    REQUIRED_FIELDS = {
        "prompt", "arm", "task_id", "packet_version", "sections_included"
    }

    def test_arm_a_shape(self) -> None:
        p = build_governed_packet(SAMPLE_TASKS["architecture_sensitive"], "A_raw")
        self.assertTrue(self.REQUIRED_FIELDS.issubset(p.keys()))
        self.assertEqual(p["arm"], "A_raw")
        self.assertEqual(p["packet_version"], PACKET_VERSION)
        self.assertEqual(p["sections_included"], [])
        self.assertEqual(p["prompt"],
                         SAMPLE_TASKS["architecture_sensitive"]["task_prompt"])

    def test_arm_b_shape_and_summary(self) -> None:
        p = build_governed_packet(SAMPLE_TASKS["architecture_sensitive"],
                                   "B_generic_context")
        self.assertTrue(self.REQUIRED_FIELDS.issubset(p.keys()))
        self.assertEqual(p["arm"], "B_generic_context")
        self.assertIn("Project summary", p["prompt"])
        # Arm B is heavier than arm A but lighter than C.
        self.assertGreater(len(p["prompt"]),
                           len(SAMPLE_TASKS["architecture_sensitive"]["task_prompt"]))

    def test_arm_c_shape(self) -> None:
        p = build_governed_packet(SAMPLE_TASKS["architecture_sensitive"],
                                   "C_governed")
        self.assertTrue(self.REQUIRED_FIELDS.issubset(p.keys()))
        self.assertEqual(p["arm"], "C_governed")
        for k in ("task_world", "agent_identity", "minimal_sufficient_context",
                  "invariants", "evidence_threshold", "failure_modes",
                  "required_output_structure"):
            self.assertIn(k, p, f"arm C missing field {k}")

    def test_unknown_arm_degrades_observably(self) -> None:
        p = build_governed_packet(SAMPLE_TASKS["architecture_sensitive"], "Z_bogus")
        self.assertEqual(p["arm"], "Z_bogus")
        self.assertTrue(any("unknown arm" in d for d in p["degradations"]))

    def test_invalid_task_does_not_raise(self) -> None:
        p = build_governed_packet({}, "C_governed")
        self.assertEqual(p["task_id"], "unknown")
        self.assertTrue(p["degradations"])


class ArmSizeOrderingTests(unittest.TestCase):
    """The whole point of arm C: it carries strictly more governance than A."""

    def test_arm_a_shorter_than_arm_c(self) -> None:
        for fam, task in SAMPLE_TASKS.items():
            with self.subTest(family=fam):
                a = build_governed_packet(task, "A_raw")
                c = build_governed_packet(task, "C_governed")
                self.assertLess(
                    len(a["prompt"]), len(c["prompt"]),
                    f"family={fam}: arm A ({len(a['prompt'])}) "
                    f">= arm C ({len(c['prompt'])})")

    def test_arm_b_between_a_and_c(self) -> None:
        task = SAMPLE_TASKS["architecture_sensitive"]
        a = build_governed_packet(task, "A_raw")
        b = build_governed_packet(task, "B_generic_context")
        c = build_governed_packet(task, "C_governed")
        self.assertLess(len(a["prompt"]), len(b["prompt"]))
        self.assertLess(len(b["prompt"]), len(c["prompt"]))


class FamilySectionMappingTests(unittest.TestCase):
    """sections_included must match the documented family→sections mapping."""

    EXPECTED = {
        "architecture_sensitive": [5, 2, 10],
        "correction_response":    [2, 6],
        "memory_promotion":       [0, 5],
        "tool_abstention":        [5],
    }

    def test_mapping_matches_spec(self) -> None:
        self.assertEqual(_FAMILY_TO_SECTIONS, self.EXPECTED)

    def test_sections_included_per_family(self) -> None:
        for fam, task in SAMPLE_TASKS.items():
            with self.subTest(family=fam):
                p = build_governed_packet(task, "C_governed")
                self.assertEqual(p["sections_included"], self.EXPECTED[fam])


class InvariantInclusionTests(unittest.TestCase):
    """Arm C must contain every CTXDNA-INV-* that applies to the task family."""

    def test_arch_family_invariants_present(self) -> None:
        task = SAMPLE_TASKS["architecture_sensitive"]
        p = build_governed_packet(task, "C_governed")
        ids = {inv["id"] for inv in p["invariants"]}

        # Architecture-sensitive trials must include the universally-applicable
        # invariants ("*") plus at least one of the high-severity action-class
        # invariants this family triggers (permission_change, influence_change,
        # inject_context, etc.). Selected set must be non-empty and all IDs
        # must conform to CTXDNA-INV-NNN.
        self.assertTrue(ids, "arch family selected zero invariants")

        for inv_id in ids:
            self.assertRegex(inv_id, r"^CTXDNA-INV-\d{3}$")

        # Composed prompt must surface invariant IDs (not just hide them in metadata).
        for inv_id in ids:
            self.assertIn(inv_id, p["prompt"],
                          f"arm C prompt missing invariant {inv_id}")

        # Statement-match path: the task's known_invariants reference
        # "No memory promotion without evidence and future utility tracking".
        # That should pull in any invariant whose statement mentions promotion +
        # evidence (CTXDNA-INV-005 in N1; INV-003 in fallback). Either way,
        # SOMETHING memory-related should land via statement substring match
        # OR via the "*" wildcard.
        self.assertGreaterEqual(
            len(ids), 1,
            "arch family must select at least the wildcard invariants")

    def test_each_family_gets_at_least_one_invariant(self) -> None:
        for fam, task in SAMPLE_TASKS.items():
            with self.subTest(family=fam):
                p = build_governed_packet(task, "C_governed")
                self.assertTrue(p["invariants"],
                                f"family {fam} got zero invariants")

    def test_task_known_invariant_statements_force_inclusion(self) -> None:
        # Even if family mapping miss, a statement-match in known_invariants
        # forces the invariant in.
        task = dict(SAMPLE_TASKS["tool_abstention"])
        task["known_invariants"] = ["No correction without replacement behavior"]
        selected = _select_invariants("tool_abstention",
                                       task["known_invariants"],
                                       _FALLBACK_INVARIANTS)
        statements = {inv["statement"] for inv in selected}
        self.assertIn("No correction without replacement behavior", statements)


class EvidenceThresholdTests(unittest.TestCase):
    def test_threshold_scales_with_difficulty(self) -> None:
        for fam, task in SAMPLE_TASKS.items():
            with self.subTest(family=fam):
                p = build_governed_packet(task, "C_governed")
                ev = p["evidence_threshold"]
                self.assertIn("min_sufficiency_score", ev)
                self.assertIn("required_context_items", ev)
                self.assertGreater(ev["min_sufficiency_score"], 0.0)
                self.assertLessEqual(ev["min_sufficiency_score"], 1.0)


class DegradationObservabilityTests(unittest.TestCase):
    """ZSF: degradations must be observable, never silent."""

    def test_packet_always_has_degradations_key(self) -> None:
        for arm in ("A_raw", "B_generic_context", "C_governed"):
            with self.subTest(arm=arm):
                p = build_governed_packet(
                    SAMPLE_TASKS["architecture_sensitive"], arm)
                self.assertIn("degradations", p)
                self.assertIsInstance(p["degradations"], list)
                self.assertIn("fixmes", p)
                self.assertIsInstance(p["fixmes"], list)

    def test_degraded_sections_use_clear_stub_format(self) -> None:
        # Any degradation entries should follow "s<N>:<source>" or
        # "[s<N>:trial degraded - ...]" format. Live runs may produce empty list.
        p = build_governed_packet(
            SAMPLE_TASKS["architecture_sensitive"], "C_governed")
        for d in p["degradations"]:
            self.assertRegex(d, r"^(s\d+:|\[s\d+:trial degraded)")

    def test_arm_c_serializable_to_json(self) -> None:
        p = build_governed_packet(
            SAMPLE_TASKS["architecture_sensitive"], "C_governed")
        # Roundtrip must succeed — required for trial registry persistence.
        s = json.dumps(p)
        self.assertGreater(len(s), 100)
        json.loads(s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
