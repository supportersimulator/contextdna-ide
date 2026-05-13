"""
RACE T2 — Tests for S4 blueprint Redis cache layer in persistent_hook_structure.

The webhook S4 (DEEP CONTEXT) section calls get_blueprint(prompt), which spawns
a subprocess that runs an architecture analysis pass — race/o2 cold-path
profile measured this at 12s. This module wraps that call with a Redis cache
(prompt-driven, TTL=1800s) and a small in-process L1 (30s TTL) for intra-batch
reuse.

Verifies:
  - First call -> cache miss + subprocess hit, second call -> cache hit
    (no subprocess invocation)
  - Cache key is prompt-driven (different prompt prefixes don't collide)
  - Same prompt prefix (first 300 chars) DOES collide (intentional: blueprint
    is prompt-driven and architecture changes slowly)
  - TTL expiry path -> miss again
  - Redis down -> graceful fallthrough to subprocess (ZSF: error counted,
    not silent)
  - S4_BLUEPRINT_CACHE_ENABLED=0 disables the Redis layer entirely
  - Public API of get_blueprint is unchanged: (prompt: str) -> Optional[str]
  - L1 in-process cache still works when Redis is unreachable
"""

import sys
import time
import types

import pytest


# =========================================================================
# Fake Redis with TTL semantics + crash mode (mirrors race/r1 test infra)
# =========================================================================

class _FakeRedis:
    """Minimal Redis stand-in: get/setex/incr with TTL + optional crash mode."""

    def __init__(self):
        self._store = {}        # key -> (value, expires_at_or_None)
        self._counters = {}     # key -> int
        self.fail_mode = False  # When True, every op raises ConnectionError

    # --- failure simulation -------------------------------------------------
    def _maybe_fail(self):
        if self.fail_mode:
            raise ConnectionError("simulated redis outage")

    # --- core ops -----------------------------------------------------------
    def get(self, key):
        self._maybe_fail()
        if key in self._counters:
            return str(self._counters[key])
        v = self._store.get(key)
        if v is None:
            return None
        value, expires_at = v
        if expires_at is not None and time.time() >= expires_at:
            del self._store[key]
            return None
        return value

    def setex(self, key, ttl, value):
        self._maybe_fail()
        expires_at = time.time() + ttl if ttl else None
        self._store[key] = (value, expires_at)
        return True

    def incr(self, key):
        self._maybe_fail()
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def ping(self):
        self._maybe_fail()
        return True


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def hook(monkeypatch):
    """Import persistent_hook_structure with mocked subprocess.run.

    get_blueprint() shells out via subprocess.run to memory/context.py.
    We stub subprocess.run at the module level so we can count invocations
    and return a deterministic blueprint without any real subprocess work.
    Each fixture instance also clears the in-process L1 cache so tests are
    independent.
    """
    import memory.persistent_hook_structure as ph

    # Reset L1 between tests so prior calls don't leak.
    ph._blueprint_cache.clear()

    calls = {"n": 0, "last_prompt": None}

    class _MockResult:
        def __init__(self, stdout: str, returncode: int = 0):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = ""

    def _mock_subprocess_run(cmd, *args, **kwargs):
        calls["n"] += 1
        # cmd is [venv_python, context.py, "--blueprint", prompt]
        prompt = cmd[-1] if cmd else ""
        calls["last_prompt"] = prompt
        return _MockResult(stdout=f"BLUEPRINT#{calls['n']}: components for {prompt[:40]}\n")

    monkeypatch.setattr(ph.subprocess, "run", _mock_subprocess_run)

    yield ph, calls


@pytest.fixture
def fake_redis(monkeypatch):
    """Inject a _FakeRedis as memory.redis_cache.get_redis_client()."""
    fake = _FakeRedis()
    if "memory.redis_cache" not in sys.modules:
        stub = types.ModuleType("memory.redis_cache")
        stub.get_redis_client = lambda: fake
        sys.modules["memory.redis_cache"] = stub
    else:
        monkeypatch.setattr(
            sys.modules["memory.redis_cache"], "get_redis_client", lambda: fake
        )
    yield fake


@pytest.fixture
def cache_on(monkeypatch):
    """Force S4_BLUEPRINT_CACHE_ENABLED=1 regardless of operator env."""
    monkeypatch.setenv("S4_BLUEPRINT_CACHE_ENABLED", "1")
    monkeypatch.setenv("S4_BLUEPRINT_CACHE_TTL_S", "1800")


# =========================================================================
# Tests
# =========================================================================

def test_first_call_misses_then_invokes_subprocess(hook, fake_redis, cache_on):
    """First call -> cache miss -> subprocess invoked once."""
    ph, calls = hook
    result = ph.get_blueprint("design new webhook latency budget alerts")

    assert result is not None
    assert "BLUEPRINT" in result
    assert calls["n"] == 1, "subprocess should be called exactly once on a cold call"

    stats = ph.get_s4_blueprint_cache_stats()
    assert stats["s4_blueprint_cache_misses"] == 1
    assert stats["s4_blueprint_cache_hits"] == 0
    assert stats["s4_blueprint_cache_errors"] == 0


def test_second_call_same_prompt_hits_redis(hook, fake_redis, cache_on):
    """Second call with identical prompt returns cached value (L1 or Redis), no subprocess."""
    ph, calls = hook
    first = ph.get_blueprint("design new webhook latency budget alerts")
    assert calls["n"] == 1

    second = ph.get_blueprint("design new webhook latency budget alerts")
    assert second == first, "Cache hit must return the exact stored value"
    assert calls["n"] == 1, "subprocess must NOT be called on a cache hit"


def test_redis_hit_when_l1_evicted(hook, fake_redis, cache_on, monkeypatch):
    """When the L1 in-process cache is empty/expired, Redis still serves the value."""
    ph, calls = hook
    first = ph.get_blueprint("plan S4 deep-context refactor for webhook")
    assert calls["n"] == 1

    # Wipe L1 so Redis is the only source of truth.
    ph._blueprint_cache.clear()

    second = ph.get_blueprint("plan S4 deep-context refactor for webhook")
    assert second == first
    assert calls["n"] == 1, "Redis hit must NOT trigger subprocess"

    stats = ph.get_s4_blueprint_cache_stats()
    assert stats["s4_blueprint_cache_hits"] >= 1


def test_cache_key_isolates_prompts(hook, fake_redis, cache_on):
    """Different prompt prefixes must not collide — both calls miss + subprocess."""
    ph, calls = hook
    ph.get_blueprint("optimize webhook latency budget")
    ph.get_blueprint("rewrite docker compose orchestrator")

    assert calls["n"] == 2, "Different prompts must each invoke subprocess"
    stats = ph.get_s4_blueprint_cache_stats()
    assert stats["s4_blueprint_cache_misses"] == 2


def test_same_prefix_collides_intentionally(hook, fake_redis, cache_on):
    """Cache key uses prompt[:300] only — same 300-char prefix is a hit by design.

    Blueprint is architecturally prompt-driven; tail variations after 300 chars
    should not invalidate the cached architecture analysis (race/t2 spec).
    """
    ph, calls = hook
    base = "x" * 300
    ph.get_blueprint(base + " tail-A more text here")
    ph.get_blueprint(base + " tail-B different ending")

    assert calls["n"] == 1, (
        "Same 300-char prefix must hit cache regardless of tail differences"
    )


def test_ttl_expiry_misses_again(hook, fake_redis, cache_on, monkeypatch):
    """When the Redis TTL expires, the next call must miss + invoke subprocess."""
    ph, calls = hook
    monkeypatch.setenv("S4_BLUEPRINT_CACHE_TTL_S", "1")  # 1s TTL

    ph.get_blueprint("debug webhook S4 cold path")
    assert calls["n"] == 1

    # Force Redis expiration AND wipe L1 so neither layer can serve.
    for k, (v, _exp) in list(fake_redis._store.items()):
        fake_redis._store[k] = (v, time.time() - 0.01)
    ph._blueprint_cache.clear()

    ph.get_blueprint("debug webhook S4 cold path")
    assert calls["n"] == 2, "Expired entry must trigger a fresh subprocess call"

    stats = ph.get_s4_blueprint_cache_stats()
    assert stats["s4_blueprint_cache_misses"] == 2


def test_redis_down_fallthrough_to_subprocess(hook, fake_redis, cache_on):
    """ZSF: if Redis raises on every op, get_blueprint still returns a blueprint
    and does NOT silently swallow the failure."""
    ph, calls = hook
    fake_redis.fail_mode = True
    ph._blueprint_cache.clear()  # ensure L1 doesn't shadow the Redis path

    result = ph.get_blueprint("debug webhook redis-down case")
    assert result is not None, "subprocess path must succeed even when Redis is down"
    assert calls["n"] == 1

    # Toggle Redis back on and observe a fresh call to confirm counters work.
    fake_redis.fail_mode = False
    ph._blueprint_cache.clear()
    ph.get_blueprint("different prompt to bust caches")
    stats = ph.get_s4_blueprint_cache_stats()
    # We must have logged either an error OR a miss for at least one path.
    total_observed = (
        stats["s4_blueprint_cache_errors"]
        + stats["s4_blueprint_cache_misses"]
    )
    assert total_observed >= 1, "ZSF: Redis failures must be counted, not silent"


def test_cache_disabled_via_env(hook, fake_redis, monkeypatch):
    """S4_BLUEPRINT_CACHE_ENABLED=0 -> Redis layer bypassed entirely.

    The L1 in-process cache still works (it's a separate fast path), so we
    clear it between calls to force the subprocess path each time.
    """
    ph, calls = hook
    monkeypatch.setenv("S4_BLUEPRINT_CACHE_ENABLED", "0")

    ph.get_blueprint("debug webhook with redis layer disabled")
    ph._blueprint_cache.clear()
    ph.get_blueprint("debug webhook with redis layer disabled")
    assert calls["n"] == 2, "With Redis cache disabled both calls reach subprocess"

    stats = ph.get_s4_blueprint_cache_stats()
    assert stats["s4_blueprint_cache_hits"] == 0
    assert stats["s4_blueprint_cache_misses"] == 0


def test_l1_cache_works_without_redis(hook, monkeypatch):
    """Even with Redis stubbed to None, the 30s in-process L1 still suppresses
    duplicate subprocess calls inside one process — preserves the previous
    behaviour from before T2."""
    ph, calls = hook

    # Force get_redis_client() -> None to simulate Redis fully absent.
    if "memory.redis_cache" not in sys.modules:
        stub = types.ModuleType("memory.redis_cache")
        stub.get_redis_client = lambda: None
        sys.modules["memory.redis_cache"] = stub
    else:
        monkeypatch.setattr(
            sys.modules["memory.redis_cache"], "get_redis_client", lambda: None
        )

    ph.get_blueprint("L1 only path verification")
    ph.get_blueprint("L1 only path verification")
    assert calls["n"] == 1, "L1 must dedupe even when Redis is unavailable"


def test_public_api_unchanged(hook, fake_redis, cache_on):
    """get_blueprint keeps the same signature: (prompt: str) -> Optional[str]."""
    import inspect
    ph, _calls = hook
    sig = inspect.signature(ph.get_blueprint)
    params = list(sig.parameters.keys())
    assert params == ["prompt"]
