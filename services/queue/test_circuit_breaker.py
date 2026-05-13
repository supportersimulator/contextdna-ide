"""Stress and unit tests for the LLM-priority-queue circuit breaker.

Covers Synaptic's 2026-05-13 directive:
  * 10 consecutive timeouts must trip the breaker (assert OPEN with the
    default ``BREAKER_MAX_TIMEOUTS=3`` — so the first 3 trip it, the rest
    short-circuit).
  * After ``BREAKER_RECOVERY_S`` elapses, a probe call must transition
    HALF_OPEN -> CLOSED on success.
  * A failed probe must re-open the breaker (HALF_OPEN -> OPEN).
  * Fallback A (cache) and Fallback B (heuristic) must each fire and
    bump their counter.
  * ZSF: every state transition bumps a counter.

The test injects a fake ``llm_generate`` and a fake clock so it runs
deterministically in <0.1 s — no real LLM calls, no real sleeps.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make the migrate4 package importable without installing it.
_THIS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = _THIS_DIR.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from llm_priority_queue.circuit_breaker import (  # noqa: E402
    COUNTERS,
    BreakerResult,
    BreakerState,
    CircuitBreaker,
    get_counters,
)


def _reset_counters() -> None:
    """COUNTERS is a module-level singleton; reset between tests."""
    for k, v in list(COUNTERS.items()):
        if isinstance(v, int):
            COUNTERS[k] = 0


class _FakeClock:
    """Monotonic clock the test can advance by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedGenerator:
    """Replays a list of outcomes as ``llm_generate`` would return them.

    Each entry is one of:
      * a string  -> returned as the response
      * ``None``  -> simulates a timeout
      * an Exception instance -> raised
    """

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, system_prompt, user_prompt, **kwargs):
        self.calls.append(
            {"system": system_prompt, "user": user_prompt, **kwargs}
        )
        if not self._outcomes:
            return None
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class CircuitBreakerStressTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset_counters()

    def _make(self, outcomes, **overrides):
        clock = _FakeClock()
        gen = _ScriptedGenerator(outcomes)
        defaults: dict = {
            "max_timeouts": 3,
            "latency_threshold_ms": 30_000,
            "recovery_s": 30.0,
            "cache_ttl_s": 300.0,
            "heuristic_enabled": True,
            "time_fn": clock,
            "wall_clock_fn": clock,
            "llm_generate_fn": gen,
        }
        defaults.update(overrides)
        cb = CircuitBreaker(**defaults)
        return cb, gen, clock

    def test_ten_timeouts_trip_then_short_circuit(self):
        """10 consecutive timeouts: first 3 trip, remaining 7 short-circuit."""
        cb, gen, clock = self._make([None] * 10)

        results: list[BreakerResult] = []
        for _ in range(10):
            results.append(cb.call("sys", "user", profile="classify"))
            clock.advance(0.01)

        # Breaker should be OPEN after the third timeout.
        self.assertIs(cb.state, BreakerState.OPEN)
        # The wrapped generator should only have been invoked 3 times —
        # subsequent calls are short-circuited at the breaker.
        self.assertEqual(len(gen.calls), 3)

        counters = get_counters()
        self.assertGreaterEqual(counters["breaker_trips_total"], 1)
        self.assertGreaterEqual(counters["breaker_timeout_trips_total"], 1)
        self.assertEqual(counters["calls_total"], 10)
        # 7 calls were rejected at the breaker (calls 4..10).
        self.assertEqual(counters["calls_short_circuited_total"], 7)
        # 3 timeouts were actually attempted upstream.
        self.assertEqual(counters["calls_timeout_total"], 3)
        # No live success -> no cache, so all 10 returned heuristic.
        self.assertEqual(counters["breaker_fallback_b_total"], 10)
        # Every result should be degraded.
        self.assertTrue(all(r.degraded for r in results))
        self.assertTrue(all(r.source == "heuristic" for r in results))
        # Gauge reflects current state.
        self.assertEqual(counters["breaker_state_now"], int(BreakerState.OPEN))

    def test_recovery_after_30s_with_success_probe(self):
        """After recovery_s elapses, a HALF_OPEN probe succeeds -> CLOSED."""
        outcomes = [None, None, None, "recovered-response"]
        cb, gen, clock = self._make(outcomes)

        # Trip the breaker.
        for _ in range(3):
            cb.call("sys", "user", profile="classify")
        self.assertIs(cb.state, BreakerState.OPEN)
        opened_at_change = cb.last_state_change_at

        # Calls during the recovery window are short-circuited.
        mid = cb.call("sys", "user", profile="classify")
        self.assertIs(cb.state, BreakerState.OPEN)
        self.assertTrue(mid.degraded)

        # Advance past recovery window and probe.
        clock.advance(31.0)
        probe = cb.call("sys", "user", profile="classify")

        self.assertEqual(probe.response, "recovered-response")
        self.assertEqual(probe.source, "live")
        self.assertFalse(probe.degraded)
        self.assertIs(cb.state, BreakerState.CLOSED)
        self.assertNotEqual(cb.last_state_change_at, opened_at_change)

        counters = get_counters()
        self.assertGreaterEqual(counters["breaker_recoveries_total"], 1)
        self.assertGreaterEqual(counters["breaker_half_open_attempts_total"], 1)
        self.assertEqual(counters["breaker_state_now"], int(BreakerState.CLOSED))

    def test_failed_probe_reopens_breaker(self):
        """HALF_OPEN probe that fails immediately re-opens the breaker."""
        outcomes = [None, None, None, None]  # 3 to trip + 1 failed probe
        cb, gen, clock = self._make(outcomes)

        for _ in range(3):
            cb.call("sys", "user", profile="classify")
        self.assertIs(cb.state, BreakerState.OPEN)

        clock.advance(31.0)
        probe = cb.call("sys", "user", profile="classify")
        self.assertTrue(probe.degraded)
        self.assertIs(cb.state, BreakerState.OPEN)

        counters = get_counters()
        self.assertGreaterEqual(counters["breaker_reopens_total"], 1)

    def test_fallback_a_serves_cached_response_when_open(self):
        """A successful call seeds the cache; later OPEN calls hit it."""
        # Success seeds cache; then 3 timeouts trip; cache must serve.
        outcomes = ["cached-answer"] + [None] * 3
        cb, gen, clock = self._make(outcomes)

        first = cb.call("sys-A", "user-A", profile="classify")
        self.assertEqual(first.response, "cached-answer")
        self.assertEqual(first.source, "live")

        # Trip with DIFFERENT prompt so no cache for that key.
        for _ in range(3):
            cb.call("sys-B", "user-B", profile="classify")
        self.assertIs(cb.state, BreakerState.OPEN)

        # Now ask the original prompt while OPEN: cache hit.
        cached = cb.call("sys-A", "user-A", profile="classify")
        self.assertEqual(cached.response, "cached-answer")
        self.assertEqual(cached.source, "cache")
        self.assertTrue(cached.degraded)
        self.assertAlmostEqual(cached.confidence, 0.6)

        # The unrelated prompt while OPEN gets heuristic.
        heur = cb.call("sys-B", "user-B", profile="classify")
        self.assertEqual(heur.source, "heuristic")

        counters = get_counters()
        self.assertGreaterEqual(counters["breaker_fallback_a_total"], 1)
        self.assertGreaterEqual(counters["breaker_fallback_b_total"], 1)

    def test_unavailable_when_heuristic_disabled_and_no_cache(self):
        """OPEN + no cache + heuristic_enabled=False -> response=None."""
        cb, gen, clock = self._make([None] * 3, heuristic_enabled=False)
        for _ in range(3):
            cb.call("sys", "user", profile="classify")
        self.assertIs(cb.state, BreakerState.OPEN)

        out = cb.call("sys", "user", profile="classify")
        self.assertIsNone(out.response)
        self.assertEqual(out.source, "unavailable")
        self.assertFalse(out.ok)
        self.assertEqual(out.confidence, 0.0)

        counters = get_counters()
        self.assertGreaterEqual(counters["breaker_fallback_unavailable_total"], 1)

    def test_latency_trip_independent_of_timeout_count(self):
        """High latency on successful calls still trips via the percentile threshold.

        Synaptic's risk: the LLM responds, but every response takes 60 s.
        No timeouts fire, yet the queue silently piles up. The breaker
        must trip on the latency percentile alone.
        """
        # 25 successes, each "slow" — exceeds the 20-call window so the
        # percentile check has full signal.
        outcomes = ["ok"] * 25
        # Latency window=20 (default), threshold=10 ms, generator advances
        # the clock 50 ms per call -> p95 latency = 50 ms.
        cb, gen, clock = self._make(outcomes, latency_threshold_ms=10)

        # Custom generator wrapping ``gen`` that advances the clock by
        # 50 ms before returning — simulating a slow LLM.
        original_gen = cb._llm_generate

        def slow_gen(*args, **kwargs):
            clock.advance(0.05)  # 50 ms — well above 10 ms threshold
            return original_gen(*args, **kwargs)

        cb._llm_generate = slow_gen

        for _ in range(25):
            cb.call("sys", "user", profile="classify")
            if cb.state is BreakerState.OPEN:
                break

        self.assertIs(cb.state, BreakerState.OPEN)
        counters = get_counters()
        self.assertGreaterEqual(counters["breaker_latency_trips_total"], 1)

    def test_reset_restores_closed_state(self):
        cb, gen, clock = self._make([None] * 3)
        for _ in range(3):
            cb.call("sys", "user", profile="classify")
        self.assertIs(cb.state, BreakerState.OPEN)
        cb.reset()
        self.assertIs(cb.state, BreakerState.CLOSED)
        self.assertEqual(cb.consecutive_timeouts, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
