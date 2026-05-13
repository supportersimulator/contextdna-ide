"""Tests for memory.governance_kill_switch — emergency rollback (T3 v4).

Required coverage:
  1. default state is enabled=False (governance ON)
  2. activate sets enabled=True
  3. evaluate() returns allow when killed regardless of inputs
  4. deactivate restores normal evaluation
  5. file persistence — restart simulation (state file survives module reload)
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional


def _reload_kill_switch_with_path(tmp_path: Path):
    """Repoint KILL_SWITCH_PATH to a per-test tempdir without reloading.

    Reloading the module would invalidate class identity (e.g.
    test_invariants imports InvariantReport at module-top); instead mutate
    attributes on the already-imported module and reset its cache.
    """
    import memory.governance_kill_switch as ks_mod  # noqa: WPS433
    ks_mod.KILL_SWITCH_PATH = tmp_path / ".governance_kill_switch.json"
    ks_mod._cached_state = None  # noqa: SLF001
    return ks_mod


def _import_invariants():
    """Return the already-imported memory.invariants module (no reload)."""
    import memory.invariants as inv_mod  # noqa: WPS433
    return inv_mod


class _KillSwitchTestBase(unittest.TestCase):
    """Reset kill-switch state between tests so leakage cannot affect
    sibling test suites (test_invariants etc.). No module reloads."""

    _original_path: Optional[Path] = None

    def setUp(self) -> None:
        import memory.governance_kill_switch as ks_mod  # noqa: WPS433
        # Snapshot original module path so tearDown can restore it.
        self._original_path = ks_mod.KILL_SWITCH_PATH

    def tearDown(self) -> None:
        import memory.governance_kill_switch as ks_mod  # noqa: WPS433
        # Restore production path and clear any lingering enabled=True state
        # so the committed on-disk file reflects the OFF default.
        if self._original_path is not None:
            ks_mod.KILL_SWITCH_PATH = self._original_path
        ks_mod._cached_state = None  # noqa: SLF001
        # If a test left the production state file kill-engaged, write it OFF.
        try:
            state = ks_mod.get_state()
            if state.enabled:
                ks_mod.deactivate(by="test-teardown")
        except Exception:  # noqa: BLE001 — tearDown ZSF: log but never raise
            import logging
            logging.getLogger(__name__).exception("kill_switch teardown reset failed")


class TestDefaultState(_KillSwitchTestBase):
    def test_default_state_disabled(self) -> None:
        with TemporaryDirectory() as td:
            ks = _reload_kill_switch_with_path(Path(td))
            state = ks.get_state()
            self.assertFalse(state.enabled, "default state must be enabled=False (governance ON)")
            self.assertIsNone(state.reason)
            self.assertIsNone(state.set_at)
            self.assertIsNone(state.set_by)
            self.assertFalse(ks.is_killed())


class TestActivateDeactivate(_KillSwitchTestBase):
    def test_activate_sets_enabled(self) -> None:
        with TemporaryDirectory() as td:
            ks = _reload_kill_switch_with_path(Path(td))
            ks.activate(reason="bad invariant blocking dispatches", by="aaron")
            state = ks.get_state()
            self.assertTrue(state.enabled)
            self.assertEqual(state.reason, "bad invariant blocking dispatches")
            self.assertEqual(state.set_by, "aaron")
            self.assertIsNotNone(state.set_at)
            self.assertTrue(ks.is_killed())

    def test_activate_rejects_empty_reason(self) -> None:
        with TemporaryDirectory() as td:
            ks = _reload_kill_switch_with_path(Path(td))
            with self.assertRaises(ValueError):
                ks.activate(reason="", by="aaron")
            with self.assertRaises(ValueError):
                ks.activate(reason="   ", by="aaron")
            with self.assertRaises(ValueError):
                ks.activate(reason="ok", by="")

    def test_deactivate_restores(self) -> None:
        with TemporaryDirectory() as td:
            ks = _reload_kill_switch_with_path(Path(td))
            ks.activate(reason="rollback experiment", by="atlas")
            self.assertTrue(ks.is_killed())
            ks.deactivate(by="atlas")
            state = ks.get_state()
            self.assertFalse(state.enabled)
            self.assertEqual(state.set_by, "atlas")
            self.assertFalse(ks.is_killed())

    def test_deactivate_rejects_empty_actor(self) -> None:
        with TemporaryDirectory() as td:
            ks = _reload_kill_switch_with_path(Path(td))
            with self.assertRaises(ValueError):
                ks.deactivate(by="")


class TestEvaluateBypass(_KillSwitchTestBase):
    """When kill-switch is engaged, evaluate() must return allow regardless of inputs."""

    def test_evaluate_bypasses_all_invariants_when_killed(self) -> None:
        with TemporaryDirectory() as td:
            ks = _reload_kill_switch_with_path(Path(td))
            inv = _import_invariants()

            # Construct a proposal that would normally HARD-BLOCK
            # (fleet_wide blast_radius with no evidence -> INV-007 fatal).
            bad = inv.ActionProposal(
                action_class="run_tool",
                blast_radius="fleet_wide",
                evidence_refs=[],
            )

            # Sanity: not killed -> blocks.
            pre = inv.evaluate(bad)
            self.assertEqual(pre.decision, "block")

            # Engage kill switch.
            ks.activate(reason="emergency rollback test", by="test")

            post = inv.evaluate(bad)
            self.assertEqual(post.decision, "allow")
            self.assertEqual(post.triggered, [])
            self.assertTrue(any("KILL_SWITCH_ACTIVE" in r for r in post.reasons))
            self.assertTrue(
                any("emergency rollback test" in r for r in post.reasons),
                f"expected reason text propagated, got {post.reasons!r}",
            )

            # Even a schema mismatch (which is normally a fatal block) is bypassed.
            bad_schema = inv.ActionProposal(
                action_class="memory_change",
                blast_radius="low",
                evidence_refs=["ev:1"],
                schema_version=inv.SCHEMA_VERSION + 99,
            )
            bypassed = inv.evaluate(bad_schema)
            self.assertEqual(bypassed.decision, "allow")
            self.assertTrue(any("KILL_SWITCH_ACTIVE" in r for r in bypassed.reasons))

    def test_deactivate_restores_normal_evaluation(self) -> None:
        with TemporaryDirectory() as td:
            ks = _reload_kill_switch_with_path(Path(td))
            inv = _import_invariants()

            ks.activate(reason="briefly disable", by="test")
            bad = inv.ActionProposal(
                action_class="run_tool",
                blast_radius="fleet_wide",
                evidence_refs=[],
            )
            self.assertEqual(inv.evaluate(bad).decision, "allow")

            ks.deactivate(by="test")
            # Normal enforcement back online.
            self.assertEqual(inv.evaluate(bad).decision, "block")

            # Clean proposal still allowed.
            clean = inv.ActionProposal(
                action_class="memory_change",
                blast_radius="low",
                evidence_refs=["ev:1"],
            )
            self.assertEqual(inv.evaluate(clean).decision, "allow")


class TestPersistence(_KillSwitchTestBase):
    """Restart simulation: state file written on activate must be read by a fresh module load."""

    def test_state_persists_across_module_reload(self) -> None:
        with TemporaryDirectory() as td:
            tmp_path = Path(td)
            ks1 = _reload_kill_switch_with_path(tmp_path)
            ks1.activate(reason="persisting across restart", by="aaron")
            self.assertTrue(ks1.is_killed())
            self.assertTrue(ks1.KILL_SWITCH_PATH.exists())

            # Verify file contents directly.
            payload = json.loads(ks1.KILL_SWITCH_PATH.read_text(encoding="utf-8"))
            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["reason"], "persisting across restart")
            self.assertEqual(payload["set_by"], "aaron")

            # Simulate restart by reloading the module and re-pointing path.
            ks2 = _reload_kill_switch_with_path(tmp_path)
            state = ks2.get_state()
            self.assertTrue(state.enabled, "kill-switch state must survive restart")
            self.assertEqual(state.reason, "persisting across restart")
            self.assertEqual(state.set_by, "aaron")
            self.assertTrue(ks2.is_killed())


class TestCounters(_KillSwitchTestBase):
    def test_activation_and_passthrough_counters_increment(self) -> None:
        with TemporaryDirectory() as td:
            ks = _reload_kill_switch_with_path(Path(td))
            before = ks.get_counters()
            ks.activate(reason="counter test", by="t")
            # Two reads through is_killed -> two passes_throughs.
            self.assertTrue(ks.is_killed())
            self.assertTrue(ks.is_killed())
            after = ks.get_counters()
            self.assertEqual(
                after["governance_kill_switch_activations"]
                - before["governance_kill_switch_activations"],
                1,
            )
            self.assertGreaterEqual(
                after["governance_kill_switch_passes_throughs"]
                - before["governance_kill_switch_passes_throughs"],
                2,
            )


if __name__ == "__main__":
    unittest.main()
