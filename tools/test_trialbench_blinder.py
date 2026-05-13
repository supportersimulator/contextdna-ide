#!/usr/bin/env python3
"""Tests for tools/trialbench_blinder.py — stdlib only (unittest)."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

# Make sibling import work regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from trialbench_blinder import (  # noqa: E402
    BLINDED_DIR_TEMPLATE,
    BlindedPackage,
    BlindingPolicy,
    COUNTERS,
    create_blinded_package,
    materialize_packages,
)


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

_ARM_LEAKAGE_TOKENS = ("A_raw", "C_governed", "C_contextdna_packet")


def _synthetic_run(
    *,
    arm: str,
    task_id: str = "arch_001",
    node: str = "mac2",
    content: str | None = None,
    include_arm_in_content: bool = False,
) -> dict:
    if content is None:
        echo = f" (arm tag was {arm})" if include_arm_in_content else ""
        content = f"summary text{echo}"
    return {
        "task_id": task_id,
        "node": node,
        "arm": arm,
        "trial_id": "test_trial",
        "protocol_hash": "deadbeefcafe",
        "packet_stub": False,
        "request_payload": {
            "prompt": "[GOVERNED] solve the puzzle" if arm.startswith("C") else "solve the puzzle",
            "arm": arm,
            "task_id": task_id,
            "task_world": {
                "title": "Memory promotion rule without DAO bypass",
                "family": "architecture_sensitive",
            },
        },
        "started_at": "2026-05-06T17:52:08.268259+00:00",
        "latency_ms": 1234,
        "exit_code": 0,
        "http_status": 200,
        "content": content,
        "error": None,
        "finished_at": "2026-05-06T17:52:32.373247+00:00",
        "attempts": 1,
    }


# ---------------------------------------------------------------------------
# create_blinded_package
# ---------------------------------------------------------------------------

class CreateBlindedPackageTests(unittest.TestCase):
    def test_strips_arm_label_from_metadata(self):
        run = _synthetic_run(arm="C_governed")
        pkg = create_blinded_package(run)
        self.assertIsInstance(pkg, BlindedPackage)
        as_dict = pkg.to_dict()
        # No raw arm string anywhere in the serialised package
        for token in _ARM_LEAKAGE_TOKENS:
            self.assertNotIn(token, json.dumps(as_dict, default=str))

    def test_strips_arm_token_from_outcome_content(self):
        run = _synthetic_run(arm="C_governed", include_arm_in_content=True)
        pkg = create_blinded_package(run)
        for token in _ARM_LEAKAGE_TOKENS:
            self.assertNotIn(token, pkg.outcome_content)
        self.assertIn("[REDACTED_ARM]", pkg.outcome_content)

    def test_task_hash_is_deterministic_and_short(self):
        a = create_blinded_package(_synthetic_run(arm="A_raw", task_id="t1"))
        b = create_blinded_package(_synthetic_run(arm="C_governed", task_id="t1"))
        # same task_id -> same task_hash regardless of arm
        self.assertEqual(a.task_hash, b.task_hash)
        self.assertEqual(len(a.task_hash), 8)

    def test_blind_id_is_unique_per_package(self):
        ids = {
            create_blinded_package(_synthetic_run(arm="A_raw", task_id=f"t{i}")).blind_id
            for i in range(20)
        }
        self.assertEqual(len(ids), 20)

    def test_preserves_replay_fields(self):
        pkg = create_blinded_package(_synthetic_run(arm="A_raw"))
        self.assertEqual(pkg.latency_ms, 1234)
        self.assertEqual(pkg.exit_code, 0)
        self.assertEqual(pkg.http_status, 200)
        self.assertEqual(pkg.finished_at, "2026-05-06T17:52:32.373247+00:00")

    def test_invalid_run_input_does_not_raise(self):
        before = COUNTERS.get("blinded_package_errors", 0)
        pkg = create_blinded_package("not a dict")  # type: ignore[arg-type]
        self.assertIsInstance(pkg, BlindedPackage)
        self.assertGreater(COUNTERS.get("blinded_package_errors", 0), before)

    def test_policy_protocol_hash_default_keeps_metadata_anchor(self):
        run = _synthetic_run(arm="A_raw")
        # Default: strip_protocol_hash=False — fingerprint differs across runs
        # with different protocol hashes.
        run2 = dict(run)
        run2["protocol_hash"] = "different_hash"
        a = create_blinded_package(run)
        b = create_blinded_package(run2)
        self.assertNotEqual(a.metadata_hash, b.metadata_hash)


# ---------------------------------------------------------------------------
# materialize_packages
# ---------------------------------------------------------------------------

class MaterializePackagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.artifacts = self.root / "artifacts" / "trialbench"
        self.trial_id = "synthetic_trial"
        self.trial_dir = self.artifacts / self.trial_id
        self.trial_dir.mkdir(parents=True)
        # 5 A_raw + 5 C_governed runs, half with arm-token leakage in content
        for i in range(5):
            (self.trial_dir / f"run_a_{i:03d}.json").write_text(
                json.dumps(
                    _synthetic_run(
                        arm="A_raw",
                        task_id=f"task_{i}",
                        include_arm_in_content=(i % 2 == 0),
                    )
                )
            )
            (self.trial_dir / f"run_c_{i:03d}.json").write_text(
                json.dumps(
                    _synthetic_run(
                        arm="C_governed",
                        task_id=f"task_{i}",
                        include_arm_in_content=(i % 2 == 1),
                    )
                )
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_produces_n_packages_for_n_runs(self):
        pkgs = materialize_packages(self.trial_id, artifacts_root=self.artifacts)
        self.assertEqual(len(pkgs), 10)

    def test_blinded_packages_have_no_arm_label_strings(self):
        pkgs = materialize_packages(self.trial_id, artifacts_root=self.artifacts)
        blinded_dir = self.trial_dir / "blinded"
        # Every per-package file
        package_files = sorted(blinded_dir.glob("*.json"))
        self.assertGreaterEqual(
            len(package_files), len(pkgs)
        )  # +manifest + unblinding_map possibly
        for p in package_files:
            if p.name in ("manifest.json", "unblinding_map.json"):
                continue
            text = p.read_text(encoding="utf-8")
            for token in _ARM_LEAKAGE_TOKENS:
                self.assertNotIn(
                    token, text, f"arm token {token!r} leaked into {p.name}"
                )

    def test_manifest_lists_all_blind_ids(self):
        pkgs = materialize_packages(self.trial_id, artifacts_root=self.artifacts)
        manifest = json.loads(
            (self.trial_dir / "blinded" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["package_count"], len(pkgs))
        self.assertEqual(set(manifest["blind_ids"]), {p.blind_id for p in pkgs})
        # Manifest itself MUST NOT contain arm labels
        manifest_text = json.dumps(manifest)
        for token in _ARM_LEAKAGE_TOKENS:
            self.assertNotIn(token, manifest_text)

    def test_unblinding_map_is_separate_with_restricted_permissions(self):
        materialize_packages(self.trial_id, artifacts_root=self.artifacts)
        unblinding_path = self.trial_dir / "blinded" / "unblinding_map.json"
        self.assertTrue(unblinding_path.exists())
        # Separate file from manifest
        manifest_path = self.trial_dir / "blinded" / "manifest.json"
        self.assertNotEqual(unblinding_path, manifest_path)
        # Permissions: chmod 600 on POSIX
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(unblinding_path).st_mode)
            self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")
        # Unblinding map IS allowed to contain arm labels — that's the
        # whole point. Just sanity-check the structure.
        ub = json.loads(unblinding_path.read_text(encoding="utf-8"))
        self.assertEqual(len(ub["entries"]), 10)
        for entry in ub["entries"]:
            self.assertIn(entry["arm"], ("A_raw", "C_governed"))

    def test_missing_trial_dir_returns_empty_no_raise(self):
        pkgs = materialize_packages("nonexistent", artifacts_root=self.artifacts)
        self.assertEqual(pkgs, [])

    def test_zsf_on_bad_run_json(self):
        (self.trial_dir / "run_bad.json").write_text("{not json")
        before = COUNTERS.get("blinded_package_errors", 0)
        pkgs = materialize_packages(self.trial_id, artifacts_root=self.artifacts)
        # Other 10 runs still produced; bad one didn't crash
        self.assertEqual(len(pkgs), 10)
        self.assertGreater(COUNTERS.get("blinded_package_errors", 0), before)
        manifest = json.loads(
            (self.trial_dir / "blinded" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertTrue(any("run_bad.json" in e["file"] for e in manifest["parse_errors"]))

    def test_blinded_dir_template_constant(self):
        self.assertEqual(
            BLINDED_DIR_TEMPLATE.format(trial_id="abc"),
            "artifacts/trialbench/abc/blinded",
        )

    def test_custom_output_dir(self):
        custom = self.root / "custom_out"
        pkgs = materialize_packages(
            self.trial_id, output_dir=custom, artifacts_root=self.artifacts
        )
        self.assertEqual(len(pkgs), 10)
        self.assertTrue((custom / "manifest.json").exists())
        self.assertTrue((custom / "unblinding_map.json").exists())


if __name__ == "__main__":
    unittest.main()
