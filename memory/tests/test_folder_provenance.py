"""Tests for memory.folder_provenance — R2 retroactive 4-folder audit.

stdlib + pytest only. No scipy / pandas / requests.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory import folder_provenance as fp  # noqa: E402


def _run(cwd: pathlib.Path, *cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@x",
        },
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name).resolve()
        self.docs = self.repo / "docs"
        for folder in ("inbox", "vision", "reflect", "dao"):
            (self.docs / folder).mkdir(parents=True, exist_ok=True)
        self.index_path = self.repo / "memory" / "folder_provenance.json"
        (self.repo / "memory").mkdir(exist_ok=True)
        fp.reset_counters()

    def tearDown(self) -> None:
        fp.reset_counters()
        self._tmp.cleanup()

    def _init_git(self) -> None:
        _run(self.repo, "git", "init", "-q", "-b", "main")
        _run(self.repo, "git", "config", "user.email", "t@x")
        _run(self.repo, "git", "config", "user.name", "test")


class TestScanCurrentSnapshot(_Base):
    def test_one_doc_per_folder_yields_four_entries(self) -> None:
        for folder, level in fp.FOLDER_LEVELS.items():
            (self.docs / folder / f"{folder}-doc.md").write_text(
                f"# {level} doc\n"
            )
        out = fp.scan_folders(docs_root=self.docs, repo_root=self.repo)
        self.assertEqual(len(out), 4)
        levels = sorted(v["current_level"] for v in out.values())
        self.assertEqual(levels, ["L1", "L2", "L3", "L4"])
        # ever_in always contains the current level.
        for entry in out.values():
            self.assertIn(entry["current_level"], entry["ever_in"])
            self.assertEqual(entry["transitions"], [])
        self.assertEqual(fp.get_counters()["total_scanned"], 4)

    def test_missing_folder_bumps_counter_not_crash(self) -> None:
        # Wipe one of the four folders entirely.
        (self.docs / "dao").rmdir()
        (self.docs / "inbox" / "a.md").write_text("# a\n")
        out = fp.scan_folders(docs_root=self.docs, repo_root=self.repo)
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(fp.get_counters()["folder_missing"], 1)


class TestRebuildIdempotency(_Base):
    def test_rebuild_same_inputs_same_output(self) -> None:
        (self.docs / "vision" / "x.md").write_text("# x\n")
        (self.docs / "reflect" / "y.md").write_text("# y\n")
        first = fp.update_provenance_index(
            docs_root=self.docs,
            output_path=self.index_path,
            repo_root=self.repo,
        )
        first_docs = first["docs"]
        second = fp.update_provenance_index(
            docs_root=self.docs,
            output_path=self.index_path,
            repo_root=self.repo,
        )
        self.assertEqual(first_docs, second["docs"])
        self.assertEqual(first["total_indexed"], 2)
        self.assertEqual(first["level_counts"]["L2"], 1)
        self.assertEqual(first["level_counts"]["L3"], 1)
        self.assertTrue(self.index_path.exists())
        on_disk = json.loads(self.index_path.read_text())
        self.assertEqual(on_disk["docs"], second["docs"])


class TestBogusRoot(_Base):
    def test_bogus_docs_root_returns_empty_and_bumps_counter(self) -> None:
        bogus = self.repo / "does-not-exist"
        out = fp.scan_folders(docs_root=bogus, repo_root=self.repo)
        self.assertEqual(out, {})
        self.assertEqual(fp.get_counters()["bogus_root"], 1)


class TestRenameTransitionViaGit(_Base):
    def test_l1_to_l2_rename_recorded_as_transition(self) -> None:
        # Skip if git missing (CI sometimes lacks it).
        try:
            subprocess.run(
                ["git", "--version"], check=True, capture_output=True
            )
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git not available")

        self._init_git()
        l1 = self.docs / "inbox" / "wandering.md"
        l1.write_text("# wandering\n")
        _run(self.repo, "git", "add", "docs/inbox/wandering.md")
        _run(self.repo, "git", "commit", "-q", "-m", "land in inbox")

        # Move (git mv) into vision/ — simulates an illegal promotion.
        _run(
            self.repo,
            "git",
            "mv",
            "docs/inbox/wandering.md",
            "docs/vision/wandering.md",
        )
        _run(
            self.repo,
            "git",
            "commit",
            "-q",
            "-m",
            "promote inbox -> vision",
        )

        out = fp.scan_folders(docs_root=self.docs, repo_root=self.repo)
        key = "docs/vision/wandering.md"
        self.assertIn(key, out)
        entry = out[key]
        self.assertEqual(entry["current_level"], "L2")
        self.assertEqual(entry["ever_in"], ["L1", "L2"])
        self.assertEqual(len(entry["transitions"]), 1)
        trans = entry["transitions"][0]
        self.assertEqual(trans["from"], "L1")
        self.assertEqual(trans["to"], "L2")
        self.assertTrue(trans["commit"])
        self.assertTrue(trans["ts"])


class TestGitFailureFallsBackToSnapshot(_Base):
    def test_no_git_history_still_returns_current_level(self) -> None:
        # Not a git repo at all — git log fails but scan must still produce
        # the current-snapshot entry. Counter should bump.
        (self.docs / "dao" / "alone.md").write_text("# alone\n")
        out = fp.scan_folders(docs_root=self.docs, repo_root=self.repo)
        self.assertIn("docs/dao/alone.md", out)
        entry = out["docs/dao/alone.md"]
        self.assertEqual(entry["current_level"], "L4")
        self.assertEqual(entry["transitions"], [])
        self.assertGreaterEqual(fp.get_counters()["git_log_failures"], 1)


class TestHealthSummary(_Base):
    def test_summarize_before_index_built(self) -> None:
        out = fp.summarize_for_health(index_path=self.index_path)
        self.assertFalse(out["available"])

    def test_summarize_after_index_built(self) -> None:
        (self.docs / "inbox" / "a.md").write_text("# a\n")
        fp.update_provenance_index(
            docs_root=self.docs,
            output_path=self.index_path,
            repo_root=self.repo,
        )
        out = fp.summarize_for_health(index_path=self.index_path)
        self.assertTrue(out["available"])
        self.assertEqual(out["total_indexed"], 1)
        self.assertEqual(out["level_counts"]["L1"], 1)
        self.assertIn("counter_snapshot", out)


class TestIndexCurrency(_Base):
    def test_stale_index_detected_when_new_doc_added(self) -> None:
        (self.docs / "inbox" / "first.md").write_text("# first\n")
        fp.update_provenance_index(
            docs_root=self.docs,
            output_path=self.index_path,
            repo_root=self.repo,
        )
        # Add a newer doc after the scan.
        import time

        time.sleep(0.05)
        newer = self.docs / "vision" / "second.md"
        newer.write_text("# second\n")
        # Force mtime well into the future to dodge filesystem resolution.
        future = newer.stat().st_mtime + 60
        os.utime(newer, (future, future))
        self.assertFalse(
            fp.is_index_current(
                docs_root=self.docs, index_path=self.index_path
            )
        )

    def test_fresh_index_is_current(self) -> None:
        (self.docs / "reflect" / "r.md").write_text("# r\n")
        fp.update_provenance_index(
            docs_root=self.docs,
            output_path=self.index_path,
            repo_root=self.repo,
        )
        self.assertTrue(
            fp.is_index_current(
                docs_root=self.docs, index_path=self.index_path
            )
        )


if __name__ == "__main__":
    unittest.main()
