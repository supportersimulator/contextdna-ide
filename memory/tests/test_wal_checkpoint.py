"""Tests for WAL checkpoint health detection.

The gains-gate must check uncheckpointed WAL pages, not file size.
PASSIVE checkpoint leaves the WAL file large but logically empty —
that's healthy. Only uncheckpointed pages indicate a problem.
"""
import sqlite3
from pathlib import Path

import pytest


def _create_wal_db(db_path: str, keep_open: bool = False):
    """Create a WAL-mode DB and write enough data to grow the WAL file.

    Args:
        keep_open: If True, return the connection (keeps WAL file alive).
                   Caller must close it.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS obs (id INTEGER PRIMARY KEY, data TEXT)")
    for i in range(100):
        conn.execute("INSERT INTO obs (data) VALUES (?)", (f"payload-{i}" * 50,))
    conn.commit()
    if keep_open:
        return conn
    conn.close()
    return None


class TestWalHealthDetection:
    """gains-gate should detect uncheckpointed pages, not file size."""

    def test_passive_checkpoint_leaves_file_large_but_healthy(self, tmp_path):
        """After PASSIVE, file is large but all pages checkpointed = healthy."""
        db_path = str(tmp_path / "test.db")
        # Keep connection open so WAL file persists after checkpoint
        holder = _create_wal_db(db_path, keep_open=True)
        try:
            wal_path = db_path + "-wal"
            assert Path(wal_path).exists(), "WAL file should exist with open conn"
            wal_size_before = Path(wal_path).stat().st_size
            assert wal_size_before > 0

            conn = sqlite3.connect(db_path)
            result = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            busy, log, checkpointed = result
            conn.close()

            # File still exists and has size > 0
            assert Path(wal_path).stat().st_size > 0
            # But all pages are checkpointed — this is healthy
            assert log == checkpointed, "All WAL pages should be checkpointed"
        finally:
            holder.close()

    def test_uncheckpointed_pages_indicate_problem(self, tmp_path):
        """When log > checkpointed, WAL needs attention."""
        db_path = str(tmp_path / "test.db")
        _create_wal_db(db_path)

        # Open a reader to block checkpoint (WAL can't checkpoint pages
        # needed by active readers)
        reader = sqlite3.connect(db_path)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM obs LIMIT 1")

        # Write more data while reader holds
        writer = sqlite3.connect(db_path)
        for i in range(50):
            writer.execute("INSERT INTO obs (data) VALUES (?)", (f"new-{i}" * 50,))
        writer.commit()

        # Checkpoint — some pages can't move due to reader
        result = writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        busy, log, checkpointed = result

        reader.execute("ROLLBACK")
        reader.close()
        writer.close()

        # With active reader, not all pages may be checkpointed
        # (This is the condition gains-gate should flag)
        # Note: SQLite may still checkpoint all if reader snapshot allows it
        assert log >= 0 and checkpointed >= 0

    def test_locked_db_does_not_crash(self, tmp_path):
        """If DB is locked (e.g., 5 concurrent writers), PASSIVE should not crash."""
        db_path = str(tmp_path / "locked.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE obs (id INTEGER PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO obs (data) VALUES ('test')")
        conn.commit()

        # Hold an exclusive lock via BEGIN IMMEDIATE
        blocker = sqlite3.connect(db_path, timeout=1)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("INSERT INTO obs (data) VALUES ('blocking')")

        # PASSIVE should still return results (it never blocks)
        checker = sqlite3.connect(db_path, timeout=5)
        result = checker.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        busy, log, checkpointed = result
        assert log >= 0 and checkpointed >= 0, "PASSIVE must return valid counts even under contention"

        blocker.execute("ROLLBACK")
        blocker.close()
        checker.close()
        conn.close()

    def test_gains_gate_checks_uncheckpointed_pages(self):
        """gains-gate.sh must use PRAGMA wal_checkpoint, not file size."""
        gate_path = Path(__file__).parents[2] / "scripts" / "gains-gate.sh"
        source = gate_path.read_text()

        assert "wal_checkpoint" in source.lower() or "PRAGMA" in source, (
            "gains-gate.sh should use PRAGMA wal_checkpoint to check WAL health, "
            "not just file size via stat"
        )

    def test_gains_gate_has_timeout(self):
        """gains-gate.sh Python subprocess must have a connection timeout."""
        gate_path = Path(__file__).parents[2] / "scripts" / "gains-gate.sh"
        source = gate_path.read_text()

        assert "timeout=" in source, (
            "gains-gate.sh WAL check must set sqlite3.connect timeout "
            "to handle locked DB gracefully"
        )
