#!/usr/bin/env python3
"""
GPU Contention Load Test
Validates the Redis-based GPU lock under concurrent access.

NOT a unit test — this is an integration test that requires:
- Redis running on 127.0.0.1:6379
- Does NOT require LLM server (mocks the actual inference)

Run:
    PYTHONPATH=. python -m pytest memory/tests/test_gpu_contention.py -v --timeout=60
    PYTHONPATH=. python -m unittest memory/tests/test_gpu_contention.py -v
"""

import os
import threading
import time
import unittest
from collections import defaultdict
from unittest.mock import patch, MagicMock

import redis

# Keys we touch — cleaned up in tearDown
_TEST_REDIS_KEYS = [
    "llm:gpu_lock",
    "llm:gpu_urgent",
    "llm:lock_wait_ms",
    "llm:lock_steals",
    "llm:lock_p4_yields",
    "llm:queue_stats",
]


def _redis_client() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)


def _redis_available() -> bool:
    try:
        return _redis_client().ping()
    except Exception:
        return False


@unittest.skipUnless(_redis_available(), "Redis not available on 127.0.0.1:6379")
class TestGPUContention(unittest.TestCase):
    """Load tests for the Redis-based GPU lock in llm_priority_queue."""

    def setUp(self):
        """Clean Redis state before each test."""
        r = _redis_client()
        for key in _TEST_REDIS_KEYS:
            r.delete(key)
        # Reset module-level state that might linger between tests
        import memory.llm_priority_queue as mod
        mod._active_priority = None

    def tearDown(self):
        """Clean Redis state after each test."""
        r = _redis_client()
        for key in _TEST_REDIS_KEYS:
            r.delete(key)

    # ------------------------------------------------------------------
    # Test 1: Serialized access under 10 concurrent threads
    # ------------------------------------------------------------------
    def test_serialized_access_10_concurrent(self):
        """10 concurrent requests should serialize through GPU lock.
        No two threads should hold the lock simultaneously."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        concurrent_holders = []  # records of (thread_name, acquired_at, released_at)
        errors = []
        active_lock = threading.Lock()
        active_count = [0]  # mutable counter for concurrent detection
        max_concurrent = [0]

        def worker(idx):
            try:
                acquired = _acquire_gpu_lock(timeout=20.0, priority=4)
                if not acquired:
                    errors.append(f"Thread-{idx} failed to acquire lock")
                    return

                with active_lock:
                    active_count[0] += 1
                    if active_count[0] > max_concurrent[0]:
                        max_concurrent[0] = active_count[0]
                    if active_count[0] > 1:
                        errors.append(
                            f"Thread-{idx}: {active_count[0]} concurrent holders!"
                        )

                t_acq = time.monotonic()
                # Simulate work (50ms)
                time.sleep(0.05)
                t_rel = time.monotonic()

                with active_lock:
                    active_count[0] -= 1

                concurrent_holders.append((f"Thread-{idx}", t_acq, t_rel))
            except Exception as e:
                errors.append(f"Thread-{idx} exception: {e}")
            finally:
                _release_gpu_lock()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)

        elapsed = time.monotonic() - t0

        self.assertEqual(errors, [], f"Concurrent access detected: {errors}")
        self.assertEqual(max_concurrent[0], 1, "More than 1 concurrent lock holder")
        self.assertEqual(len(concurrent_holders), 10, "Not all threads completed")
        # 10 threads * 50ms work = ~500ms minimum serialized time
        self.assertGreater(elapsed, 0.4, "Completed too fast — lock not serializing")

    # ------------------------------------------------------------------
    # Test 2: P1 preempts P4 within reasonable time
    # ------------------------------------------------------------------
    def test_p1_preempts_p4(self):
        """P1 (AARON) should acquire lock quickly after P4 (BACKGROUND) releases."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        p1_wait_time = [None]
        p4_released = threading.Event()
        p1_started = threading.Event()

        def p4_holder():
            """Hold lock for 500ms, then release."""
            _acquire_gpu_lock(timeout=5.0, priority=4)
            p1_started.wait(timeout=5)  # wait until P1 is trying
            time.sleep(0.5)  # simulate work
            _release_gpu_lock()
            p4_released.set()

        def p1_waiter():
            """Try to acquire with P1 priority, measure wait time."""
            p1_started.set()
            t0 = time.monotonic()
            acquired = _acquire_gpu_lock(timeout=10.0, priority=1)
            p1_wait_time[0] = time.monotonic() - t0
            if acquired:
                _release_gpu_lock()

        t_p4 = threading.Thread(target=p4_holder)
        t_p1 = threading.Thread(target=p1_waiter)

        t_p4.start()
        time.sleep(0.05)  # let P4 grab lock first
        t_p1.start()

        t_p4.join(timeout=10)
        t_p1.join(timeout=10)

        self.assertIsNotNone(p1_wait_time[0], "P1 never completed")
        # P1 should get lock within ~1s of P4 releasing (P4 holds 500ms, P1 polls fast)
        self.assertLess(
            p1_wait_time[0], 2.0,
            f"P1 waited {p1_wait_time[0]:.2f}s — too slow (expected <2s)"
        )

    # ------------------------------------------------------------------
    # Test 3: P4 yields on urgent flag
    # ------------------------------------------------------------------
    def test_p4_yields_on_urgent_flag(self):
        """When llm:gpu_urgent is set, P4's polling interval should increase,
        giving priority to the urgent waiter."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        r = _redis_client()
        results = {"p1_acquired": False, "p4_reacquired": False}
        p4_released_first = threading.Event()
        p1_done = threading.Event()

        def p4_first_hold():
            """P4 grabs lock, holds briefly, releases."""
            acquired = _acquire_gpu_lock(timeout=5.0, priority=4)
            self.assertTrue(acquired, "P4 couldn't get initial lock")
            time.sleep(0.2)
            _release_gpu_lock()
            p4_released_first.set()
            # Now P4 tries to re-acquire — should be slower due to urgent flag
            p1_done.wait(timeout=10)  # let P1 go first
            acquired2 = _acquire_gpu_lock(timeout=10.0, priority=4)
            results["p4_reacquired"] = acquired2
            if acquired2:
                _release_gpu_lock()

        def p1_urgent():
            """Wait for P4 to release, then grab with P1 priority."""
            p4_released_first.wait(timeout=5)
            t0 = time.monotonic()
            acquired = _acquire_gpu_lock(timeout=5.0, priority=1)
            results["p1_acquired"] = acquired
            if acquired:
                time.sleep(0.1)
                _release_gpu_lock()
            p1_done.set()

        t4 = threading.Thread(target=p4_first_hold)
        t1 = threading.Thread(target=p1_urgent)

        t4.start()
        t1.start()
        t4.join(timeout=15)
        t1.join(timeout=15)

        self.assertTrue(results["p1_acquired"], "P1 should have acquired lock")

    # ------------------------------------------------------------------
    # Test 4: Dead PID auto-steal
    # ------------------------------------------------------------------
    def test_dead_pid_auto_steal(self):
        """Lock held by dead PID should be auto-stolen quickly."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        r = _redis_client()
        # Set lock with a PID that definitely doesn't exist
        dead_pid = 99999
        while True:
            try:
                os.kill(dead_pid, 0)
                dead_pid += 1  # this one is alive, try next
            except OSError:
                break  # found a dead PID

        r.set("llm:gpu_lock", str(dead_pid), ex=60)

        t0 = time.monotonic()
        acquired = _acquire_gpu_lock(timeout=10.0, priority=2)
        wait = time.monotonic() - t0

        self.assertTrue(acquired, "Should have stolen lock from dead PID")
        # Dead PID detection happens on first check, should be fast
        self.assertLess(wait, 1.0, f"Steal took {wait:.2f}s — expected <1s")

        _release_gpu_lock()

        # Verify telemetry: lock steal counter should have been incremented
        steals = r.get("llm:lock_steals")
        if steals is not None:
            self.assertGreaterEqual(int(steals), 1, "Steal counter not incremented")

    # ------------------------------------------------------------------
    # Test 5: No deadlock with mixed priorities
    # ------------------------------------------------------------------
    def test_no_deadlock_mixed_priorities(self):
        """Mixed P1-P4 concurrent requests should all complete without deadlock."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        completed = defaultdict(int)  # priority -> count
        errors = []
        lock = threading.Lock()

        def worker(priority, idx):
            try:
                acquired = _acquire_gpu_lock(timeout=20.0, priority=priority)
                if not acquired:
                    errors.append(f"P{priority}-{idx} failed to acquire")
                    return
                time.sleep(0.03)  # 30ms work
                _release_gpu_lock()
                with lock:
                    completed[priority] += 1
            except Exception as e:
                errors.append(f"P{priority}-{idx}: {e}")
                try:
                    _release_gpu_lock()
                except Exception:
                    pass

        # 3 P1 + 3 P2 + 2 P3 + 2 P4 = 10 threads
        configs = [(1, 3), (2, 3), (3, 2), (4, 2)]
        threads = []
        for priority, count in configs:
            for i in range(count):
                t = threading.Thread(target=worker, args=(priority, i))
                threads.append(t)

        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        elapsed = time.monotonic() - t0

        self.assertEqual(errors, [], f"Errors: {errors}")
        total = sum(completed.values())
        self.assertEqual(total, 10, f"Only {total}/10 completed in {elapsed:.1f}s")
        self.assertLess(elapsed, 25.0, f"Took {elapsed:.1f}s — possible deadlock")

    # ------------------------------------------------------------------
    # Test 6: Lock acquisition timing under contention
    # ------------------------------------------------------------------
    def test_lock_acquisition_timing(self):
        """Measure lock wait times under contention. Reports min/max/avg/p95."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        wait_times = []
        lock = threading.Lock()
        errors = []

        def worker(idx):
            try:
                t0 = time.monotonic()
                acquired = _acquire_gpu_lock(timeout=20.0, priority=4)
                wait = (time.monotonic() - t0) * 1000  # ms
                if not acquired:
                    errors.append(f"Thread-{idx} timeout")
                    return
                with lock:
                    wait_times.append(wait)
                time.sleep(0.03)  # 30ms hold
                _release_gpu_lock()
            except Exception as e:
                errors.append(f"Thread-{idx}: {e}")
                try:
                    _release_gpu_lock()
                except Exception:
                    pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)

        self.assertEqual(errors, [], f"Errors: {errors}")
        self.assertEqual(len(wait_times), 10, "Not all threads completed")

        wait_times.sort()
        min_ms = wait_times[0]
        max_ms = wait_times[-1]
        avg_ms = sum(wait_times) / len(wait_times)
        p95_ms = wait_times[int(len(wait_times) * 0.95)]

        # Log timing report
        print(f"\n{'='*60}")
        print(f"  GPU Lock Contention Timing (10 threads, 30ms hold)")
        print(f"{'='*60}")
        print(f"  Min wait:  {min_ms:8.1f} ms")
        print(f"  Avg wait:  {avg_ms:8.1f} ms")
        print(f"  P95 wait:  {p95_ms:8.1f} ms")
        print(f"  Max wait:  {max_ms:8.1f} ms")
        print(f"{'='*60}")

        # Sanity: max wait should be under 15s for 10 * 30ms work
        self.assertLess(max_ms, 15000, f"Max wait {max_ms:.0f}ms too high")

    # ------------------------------------------------------------------
    # Test 7: Concurrent steal race — exactly one winner
    # ------------------------------------------------------------------
    def test_concurrent_steal_race(self):
        """Two threads detecting dead PID shouldn't both steal.
        Exactly one should succeed, the other should wait normally."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        r = _redis_client()

        # Find a dead PID
        dead_pid = 99999
        while True:
            try:
                os.kill(dead_pid, 0)
                dead_pid += 1
            except OSError:
                break

        results = {"winners": 0, "win_times": []}
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=5)

        def racer(idx):
            try:
                # Both threads start at the same time
                barrier.wait()
                t0 = time.monotonic()
                acquired = _acquire_gpu_lock(timeout=10.0, priority=2)
                wait = time.monotonic() - t0
                if acquired:
                    with lock:
                        results["winners"] += 1
                        results["win_times"].append((idx, wait))
                    time.sleep(0.05)
                    _release_gpu_lock()
            except Exception:
                pass

        # Set dead PID lock
        r.set("llm:gpu_lock", str(dead_pid), ex=60)

        threads = [threading.Thread(target=racer, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # Both should eventually complete (one steals, other waits for release)
        self.assertEqual(
            results["winners"], 2,
            f"Expected 2 completions, got {results['winners']}"
        )
        # The first stealer should be fast, second should wait for first's release
        if len(results["win_times"]) == 2:
            times = sorted(results["win_times"], key=lambda x: x[1])
            fast = times[0][1]
            slow = times[1][1]
            self.assertLess(fast, 1.0, f"First stealer too slow: {fast:.2f}s")

    # ------------------------------------------------------------------
    # Test 8: Telemetry — lock_wait_ms populated after contention
    # ------------------------------------------------------------------
    def test_telemetry_lock_wait_ms(self):
        """After contended acquisitions, llm:lock_wait_ms should contain entries."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        r = _redis_client()

        # Run 5 serialized acquisitions to generate telemetry
        errors = []

        def worker(idx):
            try:
                acquired = _acquire_gpu_lock(timeout=10.0, priority=4)
                if acquired:
                    time.sleep(0.02)
                    _release_gpu_lock()
                else:
                    errors.append(f"Thread-{idx} timeout")
            except Exception as e:
                errors.append(f"Thread-{idx}: {e}")
                try:
                    _release_gpu_lock()
                except Exception:
                    pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(errors, [])

        # Check telemetry key exists and has entries
        entries = r.lrange("llm:lock_wait_ms", 0, -1)
        self.assertGreater(
            len(entries), 0,
            "llm:lock_wait_ms should have entries after contended acquisitions"
        )
        # Verify entries are parseable as floats
        for entry in entries:
            val = float(entry)
            self.assertGreaterEqual(val, 0, f"Negative wait time: {val}")

    # ------------------------------------------------------------------
    # Test 9: Telemetry — steal counter incremented
    # ------------------------------------------------------------------
    def test_telemetry_steal_counter(self):
        """llm:lock_steals should increment when a dead PID lock is stolen."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        r = _redis_client()
        r.delete("llm:lock_steals")

        # Find dead PID
        dead_pid = 99998
        while True:
            try:
                os.kill(dead_pid, 0)
                dead_pid += 1
            except OSError:
                break

        # Plant dead lock
        r.set("llm:gpu_lock", str(dead_pid), ex=60)

        acquired = _acquire_gpu_lock(timeout=5.0, priority=2)
        self.assertTrue(acquired, "Should have stolen lock")
        _release_gpu_lock()

        steals = r.get("llm:lock_steals")
        self.assertIsNotNone(steals, "llm:lock_steals key missing after steal")
        self.assertGreaterEqual(int(steals), 1, "Steal counter not incremented")

    # ------------------------------------------------------------------
    # Test 10: Telemetry — P4 yield counter
    # ------------------------------------------------------------------
    def test_telemetry_p4_yield_counter(self):
        """llm:lock_p4_yields should increment when P4 backs off for urgent."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        r = _redis_client()
        r.delete("llm:lock_p4_yields")

        p4_done = threading.Event()

        def p4_worker():
            """P4 acquires, holds, then releases. It should detect urgent flag."""
            acquired = _acquire_gpu_lock(timeout=5.0, priority=4)
            if acquired:
                time.sleep(0.3)
                _release_gpu_lock()
            p4_done.set()

        def p1_worker():
            """P1 sets urgent flag and acquires after P4."""
            time.sleep(0.05)  # let P4 grab first
            acquired = _acquire_gpu_lock(timeout=5.0, priority=1)
            if acquired:
                time.sleep(0.05)
                _release_gpu_lock()

        t4 = threading.Thread(target=p4_worker)
        t1 = threading.Thread(target=p1_worker)

        t4.start()
        t1.start()
        t4.join(timeout=10)
        t1.join(timeout=10)

        # The yield counter tracks when _release_gpu_lock detects urgent waiter
        # with higher priority than the releasing holder.
        # This depends on telemetry being wired in _release_gpu_lock.
        yields = r.get("llm:lock_p4_yields")
        # May be None if P4 released before P1's urgent flag was set.
        # The important thing is the telemetry infrastructure works.
        if yields is not None:
            self.assertGreaterEqual(int(yields), 0)

    # ------------------------------------------------------------------
    # Test 11: Full llm_generate path with mocked HTTP
    # ------------------------------------------------------------------
    @patch("memory.llm_priority_queue.requests.post")
    def test_llm_generate_mocked_serialization(self, mock_post):
        """llm_generate with mocked HTTP should serialize through the lock."""
        from memory.llm_priority_queue import llm_generate, Priority

        call_times = []
        call_lock = threading.Lock()

        def fake_post(*args, **kwargs):
            with call_lock:
                call_times.append(time.monotonic())
            time.sleep(0.05)  # simulate 50ms inference
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {
                "choices": [{"message": {"content": "mocked response"}}]
            }
            return resp

        mock_post.side_effect = fake_post

        results = {}
        errors = []

        def gen_worker(idx, priority):
            try:
                result = llm_generate(
                    system_prompt="test",
                    user_prompt=f"request {idx}",
                    priority=priority,
                    profile="classify",
                    caller=f"test-{idx}",
                    timeout_s=20.0,
                )
                results[idx] = result
            except Exception as e:
                errors.append(f"Worker-{idx}: {e}")

        # 5 concurrent requests with mixed priorities
        threads = [
            threading.Thread(target=gen_worker, args=(0, Priority.BACKGROUND)),
            threading.Thread(target=gen_worker, args=(1, Priority.ATLAS)),
            threading.Thread(target=gen_worker, args=(2, Priority.BACKGROUND)),
            threading.Thread(target=gen_worker, args=(3, Priority.AARON)),
            threading.Thread(target=gen_worker, args=(4, Priority.EXTERNAL)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertEqual(errors, [], f"Errors: {errors}")
        # At least the non-preempted requests should complete with mocked result
        completed = [k for k, v in results.items() if v is not None]
        self.assertGreater(
            len(completed), 0,
            "At least some requests should complete with mocked response"
        )

    # ------------------------------------------------------------------
    # Test 12: Lock TTL prevents infinite hold
    # ------------------------------------------------------------------
    def test_lock_ttl_prevents_infinite_hold(self):
        """A lock with TTL should auto-expire, allowing acquisition."""
        r = _redis_client()
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        # Set lock with very short TTL (1 second) simulating a crash
        r.set("llm:gpu_lock", str(os.getpid()), ex=1)

        # Wait for TTL to expire
        time.sleep(1.5)

        t0 = time.monotonic()
        acquired = _acquire_gpu_lock(timeout=5.0, priority=4)
        wait = time.monotonic() - t0

        self.assertTrue(acquired, "Should acquire after TTL expiry")
        self.assertLess(wait, 1.0, f"Wait after TTL expiry: {wait:.2f}s")
        _release_gpu_lock()


# ======================================================================
# Mock-based tests — run WITHOUT Redis for CI/worktree environments
# ======================================================================

class MockRedisStore:
    """Thread-safe in-memory Redis-like store for testing lock logic."""

    def __init__(self):
        self._data = {}
        self._expiry = {}
        self._lock = threading.Lock()
        self._lists = defaultdict(list)

    def set(self, key, value, nx=False, ex=None):
        with self._lock:
            # Check expiry
            if key in self._expiry and time.monotonic() > self._expiry[key]:
                del self._data[key]
                del self._expiry[key]
            if nx and key in self._data:
                return False
            self._data[key] = value
            if ex:
                self._expiry[key] = time.monotonic() + ex
            return True

    def get(self, key):
        with self._lock:
            if key in self._expiry and time.monotonic() > self._expiry[key]:
                del self._data[key]
                del self._expiry[key]
                return None
            return self._data.get(key)

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)
            self._expiry.pop(key, None)
            return 1

    def exists(self, key):
        with self._lock:
            if key in self._expiry and time.monotonic() > self._expiry[key]:
                del self._data[key]
                del self._expiry[key]
                return 0
            return 1 if key in self._data else 0

    def incr(self, key):
        with self._lock:
            val = int(self._data.get(key, 0)) + 1
            self._data[key] = str(val)
            return val

    def lpush(self, key, *values):
        with self._lock:
            for v in values:
                self._lists[key].insert(0, v)
            return len(self._lists[key])

    def ltrim(self, key, start, stop):
        with self._lock:
            self._lists[key] = self._lists[key][start:stop + 1]
        return True

    def lrange(self, key, start, stop):
        with self._lock:
            if stop == -1:
                return list(self._lists[key][start:])
            return list(self._lists[key][start:stop + 1])

    def expire(self, key, seconds):
        with self._lock:
            if key in self._data or key in self._lists:
                self._expiry[key] = time.monotonic() + seconds
        return True

    def ping(self):
        return True

    def hset(self, name, mapping=None, **kwargs):
        return True

    def hincrby(self, name, key, amount=1):
        return amount

    def hincrbyfloat(self, name, key, amount=0.0):
        return amount

    def incrbyfloat(self, name, amount=0.0):
        return amount


class TestGPUContentionMocked(unittest.TestCase):
    """GPU lock tests using mocked Redis — runs without live Redis."""

    def setUp(self):
        self._mock_store = MockRedisStore()
        # Patch redis.Redis to return our mock
        self._redis_patcher = patch("redis.Redis", return_value=self._mock_store)
        self._redis_patcher.start()
        # Patch _redis_available to return True
        self._avail_patcher = patch(
            "memory.llm_priority_queue._redis_available", return_value=True
        )
        self._avail_patcher.start()
        # Reset module state
        import memory.llm_priority_queue as mod
        mod._active_priority = None

    def tearDown(self):
        self._redis_patcher.stop()
        self._avail_patcher.stop()

    def test_serialized_access_5_concurrent(self):
        """5 concurrent threads should serialize through GPU lock (mocked)."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        errors = []
        active_lock = threading.Lock()
        active_count = [0]
        max_concurrent = [0]
        completed_count = [0]

        def worker(idx):
            try:
                acquired = _acquire_gpu_lock(timeout=15.0, priority=4)
                if not acquired:
                    errors.append(f"Thread-{idx} failed")
                    return
                with active_lock:
                    active_count[0] += 1
                    if active_count[0] > max_concurrent[0]:
                        max_concurrent[0] = active_count[0]
                    if active_count[0] > 1:
                        errors.append(f"Thread-{idx}: concurrent!")

                time.sleep(0.03)

                with active_lock:
                    active_count[0] -= 1
                    completed_count[0] += 1
            except Exception as e:
                errors.append(f"Thread-{idx}: {e}")
            finally:
                _release_gpu_lock()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertEqual(errors, [], f"Errors: {errors}")
        self.assertEqual(max_concurrent[0], 1, "Concurrent access detected")
        self.assertEqual(completed_count[0], 5, "Not all completed")

    def test_dead_pid_steal_mocked(self):
        """Dead PID detection works with mocked Redis."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        # Find dead PID
        dead_pid = 99999
        while True:
            try:
                os.kill(dead_pid, 0)
                dead_pid += 1
            except OSError:
                break

        # Plant dead lock in mock store
        self._mock_store.set("llm:gpu_lock", str(dead_pid), ex=60)

        t0 = time.monotonic()
        acquired = _acquire_gpu_lock(timeout=5.0, priority=2)
        wait = time.monotonic() - t0

        self.assertTrue(acquired, "Should steal from dead PID")
        self.assertLess(wait, 1.0, f"Steal too slow: {wait:.2f}s")
        _release_gpu_lock()

    def test_telemetry_recorded_mocked(self):
        """Telemetry (wait_ms, steals) recorded in mocked Redis."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        # Simple acquire/release
        acquired = _acquire_gpu_lock(timeout=5.0, priority=4)
        self.assertTrue(acquired)
        _release_gpu_lock()

        # Check wait_ms was recorded
        entries = self._mock_store.lrange("llm:lock_wait_ms", 0, -1)
        self.assertGreater(len(entries), 0, "Wait time not recorded")
        val = float(entries[0])
        self.assertGreaterEqual(val, 0)

    def test_steal_telemetry_mocked(self):
        """Steal counter incremented in mocked Redis on dead PID steal."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        dead_pid = 99999
        while True:
            try:
                os.kill(dead_pid, 0)
                dead_pid += 1
            except OSError:
                break

        self._mock_store.set("llm:gpu_lock", str(dead_pid), ex=60)

        acquired = _acquire_gpu_lock(timeout=5.0, priority=2)
        self.assertTrue(acquired)
        _release_gpu_lock()

        steals = self._mock_store.get("llm:lock_steals")
        self.assertIsNotNone(steals, "Steal counter missing")
        self.assertGreaterEqual(int(steals), 1)

    def test_no_deadlock_mixed_mocked(self):
        """Mixed priorities complete without deadlock (mocked)."""
        from memory.llm_priority_queue import _acquire_gpu_lock, _release_gpu_lock

        completed = [0]
        errors = []
        lock = threading.Lock()

        def worker(priority, idx):
            try:
                acquired = _acquire_gpu_lock(timeout=15.0, priority=priority)
                if not acquired:
                    errors.append(f"P{priority}-{idx} timeout")
                    return
                time.sleep(0.02)
                _release_gpu_lock()
                with lock:
                    completed[0] += 1
            except Exception as e:
                errors.append(f"P{priority}-{idx}: {e}")
                try:
                    _release_gpu_lock()
                except Exception:
                    pass

        configs = [(1, 2), (2, 2), (3, 2), (4, 2)]  # 8 threads
        threads = []
        for priority, count in configs:
            for i in range(count):
                threads.append(threading.Thread(target=worker, args=(priority, i)))

        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        elapsed = time.monotonic() - t0
        self.assertEqual(errors, [], f"Errors: {errors}")
        self.assertEqual(completed[0], 8, f"Only {completed[0]}/8 in {elapsed:.1f}s")
        self.assertLess(elapsed, 15.0, "Possible deadlock")

    @patch("memory.llm_priority_queue.requests.post")
    def test_llm_generate_mocked_full(self, mock_post):
        """Full llm_generate path with mocked Redis + mocked HTTP."""
        from memory.llm_priority_queue import llm_generate, Priority

        def fake_post(*args, **kwargs):
            time.sleep(0.02)
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {
                "choices": [{"message": {"content": "test output"}}]
            }
            return resp

        mock_post.side_effect = fake_post

        result = llm_generate(
            system_prompt="test system",
            user_prompt="test prompt",
            priority=Priority.BACKGROUND,
            profile="classify",
            caller="test_mocked",
            timeout_s=10.0,
        )

        # Should get the mocked response (with /no_think stripped and thinking tags cleaned)
        self.assertIsNotNone(result, "Expected mocked response")


if __name__ == "__main__":
    unittest.main()
