"""Tests for memory.mission_envelope — typed multi-action autonomy contract."""

from __future__ import annotations

import datetime as _dt
import unittest

from memory.mission_envelope import (
    EnvelopeReport,
    MissionEnvelope,
    check_action,
    get_counters,
)


def _make_envelope(**overrides) -> MissionEnvelope:
    base = dict(
        id="env-test-001",
        user_goal="test envelope",
    )
    base.update(overrides)
    return MissionEnvelope(**base)


class TestExpiry(unittest.TestCase):
    def test_expired_envelope_blocks(self):
        past = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)).isoformat()
        env = _make_envelope(expires_at=past)
        r = check_action(env, action_class="memory_change")
        self.assertEqual(r.decision, "block_expired")

    def test_future_expiry_allows(self):
        future = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)).isoformat()
        env = _make_envelope(expires_at=future)
        r = check_action(env, action_class="memory_change")
        self.assertEqual(r.decision, "allow")

    def test_no_expiry_allows(self):
        env = _make_envelope(expires_at=None)
        r = check_action(env, action_class="memory_change")
        self.assertEqual(r.decision, "allow")

    def test_unparseable_expiry_blocks_failclosed(self):
        env = _make_envelope(expires_at="not-a-date")
        r = check_action(env, action_class="memory_change")
        self.assertEqual(r.decision, "block_expired")


class TestAllowList(unittest.TestCase):
    def test_empty_allowed_means_no_whitelist(self):
        env = _make_envelope(allowed_actions=[])
        r = check_action(env, action_class="anything")
        self.assertEqual(r.decision, "allow")

    def test_action_in_allow_list_passes(self):
        env = _make_envelope(allowed_actions=["memory_change", "inject_context"])
        r = check_action(env, action_class="memory_change")
        self.assertEqual(r.decision, "allow")

    def test_action_not_in_allow_list_blocks(self):
        env = _make_envelope(allowed_actions=["memory_change"])
        r = check_action(env, action_class="permission_change")
        self.assertEqual(r.decision, "block_out_of_scope")


class TestForbiddenList(unittest.TestCase):
    def test_forbidden_takes_precedence_over_allowed(self):
        env = _make_envelope(
            allowed_actions=["memory_change"],
            forbidden_actions=["memory_change"],
        )
        r = check_action(env, action_class="memory_change")
        self.assertEqual(r.decision, "block_forbidden")


class TestRiskLane(unittest.TestCase):
    def test_lane_at_ceiling_passes(self):
        env = _make_envelope(risk_lane_ceiling=2)
        r = check_action(env, action_class="x", risk_lane=2)
        self.assertEqual(r.decision, "allow")

    def test_lane_above_ceiling_blocks(self):
        env = _make_envelope(risk_lane_ceiling=2)
        r = check_action(env, action_class="x", risk_lane=4)
        self.assertEqual(r.decision, "block_lane_too_high")


class TestScope(unittest.TestCase):
    def test_target_node_outside_scope_blocks(self):
        env = _make_envelope(scope_nodes=["mac2"])
        r = check_action(env, action_class="x", target_node="mac3")
        self.assertEqual(r.decision, "block_out_of_scope")

    def test_target_repo_outside_scope_blocks(self):
        env = _make_envelope(scope_repos=["er-simulator-superrepo"])
        r = check_action(env, action_class="x", target_repo="other-repo")
        self.assertEqual(r.decision, "block_out_of_scope")

    def test_empty_scope_means_no_restriction(self):
        env = _make_envelope(scope_nodes=[], scope_repos=[])
        r = check_action(env, action_class="x", target_node="mac3", target_repo="other")
        self.assertEqual(r.decision, "allow")


class TestEvidenceRequirement(unittest.TestCase):
    def test_evidence_required_with_refs_passes(self):
        env = _make_envelope(evidence_required=True)
        r = check_action(env, action_class="x", evidence_refs=["commit:abc"])
        self.assertEqual(r.decision, "allow")

    def test_evidence_required_without_refs_blocks(self):
        env = _make_envelope(evidence_required=True)
        r = check_action(env, action_class="x", evidence_refs=[])
        self.assertEqual(r.decision, "block_evidence_missing")


class TestRollbackRequirement(unittest.TestCase):
    def test_rollback_required_with_reversible_passes(self):
        env = _make_envelope(rollback_required=True)
        r = check_action(env, action_class="x", reversible=True)
        self.assertEqual(r.decision, "allow")

    def test_rollback_required_with_rollback_evidence_passes(self):
        env = _make_envelope(rollback_required=True)
        r = check_action(env, action_class="x", reversible=False, rollback_evidence=["snapshot:s1"])
        self.assertEqual(r.decision, "allow")

    def test_rollback_required_neither_blocks(self):
        env = _make_envelope(rollback_required=True)
        r = check_action(env, action_class="x", reversible=False)
        self.assertEqual(r.decision, "block_rollback_missing")


class TestZSFCounters(unittest.TestCase):
    def test_counters_increment(self):
        env = _make_envelope(allowed_actions=["x"])
        before = get_counters()["envelope_action_blocked"]
        check_action(env, action_class="not_x")
        after = get_counters()["envelope_action_blocked"]
        self.assertEqual(after, before + 1)


if __name__ == "__main__":
    unittest.main()
