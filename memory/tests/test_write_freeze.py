"""
Tests for memory/write_freeze.py — Write Freeze Guard.

Tests:
- is_frozen() returns False when no freeze set
- freeze() + is_frozen() returns True
- thaw() clears freeze
- check_or_raise() raises WriteFrozenError when frozen
- check_or_raise() passes when not frozen
- workspace_id scoping (freeze workspace A, workspace B unaffected)
- TTL auto-clears (set freeze with short TTL, wait, check expired)
- Redis failure = fail open (writes not blocked)
"""

import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

import sys
import os

# Ensure PYTHONPATH includes project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestWriteFreezeGuardWithRedis(unittest.TestCase):
    """Tests using a real Redis connection (integration-style).

    These tests require Redis on 127.0.0.1:6379.
    If Redis is unavailable, tests are skipped.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import redis
            r = redis.Redis(host='127.0.0.1', port=6379)
            r.ping()
            cls.redis_available = True
        except Exception:
            cls.redis_available = False

    def setUp(self):
        if not self.redis_available:
            self.skipTest("Redis not available on 127.0.0.1:6379")

        import redis
        self.redis_client = redis.Redis(host='127.0.0.1', port=6379)

        from memory.write_freeze import WriteFreezeGuard, FREEZE_KEY
        self.guard = WriteFreezeGuard(redis_client=self.redis_client)
        self.FREEZE_KEY = FREEZE_KEY

        # Clean up any leftover freeze keys from prior test runs
        for key in self.redis_client.keys(f'{self.FREEZE_KEY}*'):
            self.redis_client.delete(key)

    def tearDown(self):
        if self.redis_available:
            # Clean up freeze keys
            for key in self.redis_client.keys(f'{self.FREEZE_KEY}*'):
                self.redis_client.delete(key)

    def test_is_frozen_returns_false_when_no_freeze(self):
        """is_frozen() returns False when no freeze is set."""
        self.assertFalse(self.guard.is_frozen())

    def test_freeze_then_is_frozen_returns_true(self):
        """freeze() + is_frozen() returns True."""
        self.guard.freeze(reason='test')
        self.assertTrue(self.guard.is_frozen())

    def test_thaw_clears_freeze(self):
        """thaw() clears the freeze."""
        self.guard.freeze(reason='test')
        self.assertTrue(self.guard.is_frozen())
        self.guard.thaw()
        self.assertFalse(self.guard.is_frozen())

    def test_check_or_raise_raises_when_frozen(self):
        """check_or_raise() raises WriteFrozenError when frozen."""
        from memory.write_freeze import WriteFrozenError
        self.guard.freeze(reason='test')
        with self.assertRaises(WriteFrozenError):
            self.guard.check_or_raise()

    def test_check_or_raise_passes_when_not_frozen(self):
        """check_or_raise() does not raise when not frozen."""
        # Should not raise
        self.guard.check_or_raise()

    def test_workspace_scoping_freeze_a_not_b(self):
        """Freeze workspace A, workspace B should be unaffected."""
        self.guard.freeze(workspace_id='ws_a', reason='test')
        self.assertTrue(self.guard.is_frozen(workspace_id='ws_a'))
        self.assertFalse(self.guard.is_frozen(workspace_id='ws_b'))

    def test_workspace_scoping_global_vs_scoped(self):
        """Global freeze does not affect workspace-scoped check and vice versa."""
        self.guard.freeze(reason='global_test')
        # Global is frozen
        self.assertTrue(self.guard.is_frozen())
        # But workspace-specific is not frozen (different key)
        self.assertFalse(self.guard.is_frozen(workspace_id='ws_x'))

    def test_thaw_workspace_specific(self):
        """Thawing workspace A does not affect workspace B freeze."""
        self.guard.freeze(workspace_id='ws_a', reason='test')
        self.guard.freeze(workspace_id='ws_b', reason='test')
        self.guard.thaw(workspace_id='ws_a')
        self.assertFalse(self.guard.is_frozen(workspace_id='ws_a'))
        self.assertTrue(self.guard.is_frozen(workspace_id='ws_b'))

    def test_ttl_auto_clears(self):
        """Freeze with short TTL auto-expires."""
        self.guard.freeze(reason='test_ttl', ttl=1)
        self.assertTrue(self.guard.is_frozen())
        time.sleep(1.5)
        self.assertFalse(self.guard.is_frozen())

    def test_check_or_raise_with_workspace(self):
        """check_or_raise with workspace_id raises correctly."""
        from memory.write_freeze import WriteFrozenError
        self.guard.freeze(workspace_id='ws_locked', reason='migration')
        with self.assertRaises(WriteFrozenError):
            self.guard.check_or_raise(workspace_id='ws_locked')
        # Other workspace should pass
        self.guard.check_or_raise(workspace_id='ws_other')


class TestWriteFreezeGuardFailOpen(unittest.TestCase):
    """Tests that verify fail-open behavior when Redis is unavailable."""

    def test_is_frozen_returns_false_on_redis_failure(self):
        """When Redis is unreachable, is_frozen() returns False (fail open)."""
        from memory.write_freeze import WriteFreezeGuard

        mock_redis = MagicMock()
        mock_redis.exists.side_effect = ConnectionError("Redis down")
        guard = WriteFreezeGuard(redis_client=mock_redis)

        # Should return False (fail open), not raise
        self.assertFalse(guard.is_frozen())

    def test_check_or_raise_does_not_raise_on_redis_failure(self):
        """When Redis is unreachable, check_or_raise() does NOT raise."""
        from memory.write_freeze import WriteFreezeGuard

        mock_redis = MagicMock()
        mock_redis.exists.side_effect = ConnectionError("Redis down")
        guard = WriteFreezeGuard(redis_client=mock_redis)

        # Should not raise anything
        guard.check_or_raise()

    def test_freeze_logs_warning_on_redis_failure(self):
        """freeze() handles Redis failure gracefully."""
        from memory.write_freeze import WriteFreezeGuard

        mock_redis = MagicMock()
        mock_redis.setex.side_effect = ConnectionError("Redis down")
        guard = WriteFreezeGuard(redis_client=mock_redis)

        # Should not raise
        guard.freeze(reason='test')

    def test_thaw_handles_redis_failure(self):
        """thaw() handles Redis failure gracefully."""
        from memory.write_freeze import WriteFreezeGuard

        mock_redis = MagicMock()
        mock_redis.delete.side_effect = ConnectionError("Redis down")
        guard = WriteFreezeGuard(redis_client=mock_redis)

        # Should not raise
        guard.thaw()


class TestWriteFreezeModuleFlag(unittest.TestCase):
    """Tests for the _write_freeze_enabled module flag."""

    def test_disabled_flag_makes_is_frozen_return_false(self):
        """When _write_freeze_enabled is False, is_frozen() always returns False."""
        import memory.write_freeze as wf
        from memory.write_freeze import WriteFreezeGuard

        original = wf._write_freeze_enabled
        try:
            # Even with a mock that would return True, the flag overrides
            mock_redis = MagicMock()
            mock_redis.exists.return_value = True
            guard = WriteFreezeGuard(redis_client=mock_redis)

            wf._write_freeze_enabled = False
            self.assertFalse(guard.is_frozen())

            wf._write_freeze_enabled = True
            self.assertTrue(guard.is_frozen())
        finally:
            wf._write_freeze_enabled = original


class TestWriteFreezeSingleton(unittest.TestCase):
    """Tests for the module-level singleton getter."""

    def test_singleton_returns_same_instance(self):
        """get_write_freeze_guard() returns the same instance."""
        import memory.write_freeze as wf

        # Reset singleton for clean test
        original = wf._guard_instance
        wf._guard_instance = None
        try:
            guard1 = wf.get_write_freeze_guard()
            guard2 = wf.get_write_freeze_guard()
            self.assertIs(guard1, guard2)
        finally:
            wf._guard_instance = original


class TestDbUtilsWriteFreezeIntegration(unittest.TestCase):
    """Tests that db_utils respects write freeze."""

    @classmethod
    def setUpClass(cls):
        try:
            import redis
            r = redis.Redis(host='127.0.0.1', port=6379)
            r.ping()
            cls.redis_available = True
        except Exception:
            cls.redis_available = False

    def setUp(self):
        if not self.redis_available:
            self.skipTest("Redis not available on 127.0.0.1:6379")

        import redis
        self.redis_client = redis.Redis(host='127.0.0.1', port=6379)
        from memory.write_freeze import FREEZE_KEY
        self.FREEZE_KEY = FREEZE_KEY
        # Clean up
        for key in self.redis_client.keys(f'{self.FREEZE_KEY}*'):
            self.redis_client.delete(key)

    def tearDown(self):
        if self.redis_available:
            for key in self.redis_client.keys(f'{self.FREEZE_KEY}*'):
                self.redis_client.delete(key)

    def test_is_write_sql_detection(self):
        """_is_write_sql correctly identifies write vs read SQL."""
        from memory.db_utils import _is_write_sql

        # Write operations
        self.assertTrue(_is_write_sql("INSERT INTO foo VALUES (1)"))
        self.assertTrue(_is_write_sql("UPDATE foo SET bar=1"))
        self.assertTrue(_is_write_sql("DELETE FROM foo"))
        self.assertTrue(_is_write_sql("REPLACE INTO foo VALUES (1)"))
        self.assertTrue(_is_write_sql("  INSERT INTO foo VALUES (1)"))  # leading whitespace
        self.assertTrue(_is_write_sql("insert into foo values (1)"))  # lowercase
        self.assertTrue(_is_write_sql("CREATE TABLE foo (id INT)"))
        self.assertTrue(_is_write_sql("DROP TABLE foo"))
        self.assertTrue(_is_write_sql("ALTER TABLE foo ADD bar INT"))

        # Read operations
        self.assertFalse(_is_write_sql("SELECT * FROM foo"))
        self.assertFalse(_is_write_sql("PRAGMA journal_mode=WAL"))
        self.assertFalse(_is_write_sql("  SELECT count(*) FROM foo"))

    def test_thread_safe_connection_blocks_write_during_freeze(self):
        """ThreadSafeConnection.execute blocks write SQL during freeze."""
        import tempfile
        from memory.db_utils import ThreadSafeConnection
        from memory.write_freeze import WriteFreezeGuard, WriteFrozenError

        guard = WriteFreezeGuard(redis_client=self.redis_client)

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            conn = ThreadSafeConnection(db_path)
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")

            # Activate freeze
            guard.freeze(reason='test')

            # Write should be blocked
            with self.assertRaises(WriteFrozenError):
                conn.execute("INSERT INTO test VALUES (1, 'hello')")

            # Read should still work
            result = conn.fetchall("SELECT * FROM test")
            self.assertEqual(len(result), 0)

            # Thaw and verify write works again
            guard.thaw()
            conn.execute("INSERT INTO test VALUES (1, 'hello')")
            result = conn.fetchall("SELECT * FROM test")
            self.assertEqual(len(result), 1)

            conn.close()
        finally:
            os.unlink(db_path)


if __name__ == '__main__':
    unittest.main()
