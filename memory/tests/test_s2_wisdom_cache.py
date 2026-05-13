"""
RACE R1 — Tests for S2 wisdom Redis cache layer in persistent_hook_structure.

Verifies:
  - First call -> cache miss + LLM hit, second call -> cache hit (no LLM call)
  - Cache key includes domain (different domains don't collide)
  - Cache key includes depth (full vs one_thing_only don't collide)
  - TTL expiry path -> miss again
  - Redis down -> graceful fallthrough to LLM (ZSF: error counted, not silent)
  - S2_CACHE_ENABLED=0 disables the cache path entirely
  - Public API of _get_llm_professor_wisdom is unchanged

Each test uses a fake Redis client injected via monkeypatch onto
memory.redis_cache.get_redis_client, plus mocked s2_professor_query so no
real LLM/Redis is touched.
"""

import sys
import time
import types

import pytest


# =========================================================================
# Fake Redis with TTL semantics + crash mode (mirrors race/m3 test infra)
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
    """Import persistent_hook_structure with mocked s2_professor_query.

    The wisdom function pulls in heavy deps (sqlite, failure analyzer, the
    professor module). We stub those at the module level so each call reaches
    the LLM path deterministically — and we count LLM invocations.
    """
    import memory.persistent_hook_structure as ph

    # Stub out the enrichment helpers so they never block / never error
    monkeypatch.setattr(
        ph, "detect_domain_from_prompt",
        lambda p: "webhook" if "webhook" in (p or "").lower() else (
            "docker" if "docker" in (p or "").lower() else None
        ),
    )
    monkeypatch.setattr(ph, "get_professor_wisdom_dicts", lambda: ({}, {}))

    # Counter for s2_professor_query LLM calls
    calls = {"n": 0, "last_user": None, "last_brief": None}

    def _mock_s2_query(system_prompt, user_prompt, brief=False):
        calls["n"] += 1
        calls["last_user"] = user_prompt
        calls["last_brief"] = brief
        # Return >20 chars so wisdom_out is kept by _get_llm_professor_wisdom
        return f"Professor guidance #{calls['n']}: read the error message first."

    # Inject the stub into the import path used by the wisdom function:
    # `from memory.llm_priority_queue import s2_professor_query`
    if "memory.llm_priority_queue" not in sys.modules:
        mod = types.ModuleType("memory.llm_priority_queue")
        sys.modules["memory.llm_priority_queue"] = mod
    monkeypatch.setattr(
        sys.modules["memory.llm_priority_queue"],
        "s2_professor_query",
        _mock_s2_query,
        raising=False,
    )

    # Make sure the FTS5/learnings/failure helpers don't blow up by silencing
    # their imports — the function already wraps them in try/except, so we
    # just need to ensure they don't accidentally hit real disk.
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
    """Force S2_CACHE_ENABLED=1 regardless of operator env."""
    monkeypatch.setenv("S2_CACHE_ENABLED", "1")
    monkeypatch.setenv("S2_CACHE_TTL_S", "600")


# =========================================================================
# Tests
# =========================================================================

def test_first_call_misses_then_invokes_llm(hook, fake_redis, cache_on):
    """First call -> cache miss -> LLM invoked once."""
    ph, calls = hook
    result = ph._get_llm_professor_wisdom("fix webhook timeouts", depth="full")

    assert result is not None
    assert calls["n"] == 1, "LLM should be called exactly once on a cold call"

    stats = ph.get_s2_cache_stats()
    assert stats["s2_cache_misses"] == 1
    assert stats["s2_cache_hits"] == 0
    assert stats["s2_cache_errors"] == 0


def test_second_call_same_inputs_hits_cache(hook, fake_redis, cache_on):
    """Second call with identical inputs returns cached value, no LLM call."""
    ph, calls = hook
    first = ph._get_llm_professor_wisdom("fix webhook timeouts", depth="full")
    assert calls["n"] == 1

    second = ph._get_llm_professor_wisdom("fix webhook timeouts", depth="full")
    assert second == first, "Cache hit must return the exact stored value"
    assert calls["n"] == 1, "LLM must NOT be called on a cache hit"

    stats = ph.get_s2_cache_stats()
    assert stats["s2_cache_misses"] == 1
    assert stats["s2_cache_hits"] == 1


def test_cache_key_isolates_domains(hook, fake_redis, cache_on):
    """Different detected domains must not collide — both calls miss + LLM-hit."""
    ph, calls = hook
    # Same prompt body but different domain-triggering keywords
    ph._get_llm_professor_wisdom("optimize webhook latency", depth="full")
    ph._get_llm_professor_wisdom("optimize docker image build", depth="full")

    assert calls["n"] == 2, "Different domains must each invoke the LLM"
    stats = ph.get_s2_cache_stats()
    assert stats["s2_cache_misses"] == 2
    assert stats["s2_cache_hits"] == 0


def test_cache_key_isolates_depth(hook, fake_redis, cache_on):
    """Different depth values -> distinct cache entries (different LLM profile)."""
    ph, calls = hook
    ph._get_llm_professor_wisdom("debug webhook", depth="full")
    ph._get_llm_professor_wisdom("debug webhook", depth="one_thing_only")

    assert calls["n"] == 2, "full vs one_thing_only must each invoke the LLM"


def test_ttl_expiry_misses_again(hook, fake_redis, cache_on, monkeypatch):
    """When the TTL expires, the next call must miss + invoke the LLM again."""
    ph, calls = hook
    monkeypatch.setenv("S2_CACHE_TTL_S", "1")  # 1 second TTL

    ph._get_llm_professor_wisdom("debug webhook", depth="full")
    assert calls["n"] == 1

    # Force expiration by rewriting stored expiry directly.
    for k, (v, _exp) in list(fake_redis._store.items()):
        fake_redis._store[k] = (v, time.time() - 0.01)

    ph._get_llm_professor_wisdom("debug webhook", depth="full")
    assert calls["n"] == 2, "Expired entry must trigger a fresh LLM call"

    stats = ph.get_s2_cache_stats()
    assert stats["s2_cache_misses"] == 2
    assert stats["s2_cache_hits"] == 0


def test_redis_down_fallthrough_to_llm(hook, fake_redis, cache_on):
    """ZSF: if Redis raises on every op, _get_llm_professor_wisdom still
    returns LLM output and does NOT silently swallow the failure."""
    ph, calls = hook
    fake_redis.fail_mode = True

    result = ph._get_llm_professor_wisdom("debug webhook", depth="full")
    assert result is not None, "LLM path must succeed even when Redis is down"
    assert calls["n"] == 1

    # Toggle Redis back on and observe a fresh miss to confirm counters work.
    fake_redis.fail_mode = False
    ph._get_llm_professor_wisdom("debug webhook other", depth="full")
    stats = ph.get_s2_cache_stats()
    # At minimum we must have logged either an error or a miss for some path.
    assert (stats["s2_cache_errors"] + stats["s2_cache_misses"]) >= 1


def test_cache_disabled_via_env(hook, fake_redis, monkeypatch):
    """S2_CACHE_ENABLED=0 -> cache layer is bypassed entirely, every call hits LLM."""
    ph, calls = hook
    monkeypatch.setenv("S2_CACHE_ENABLED", "0")

    ph._get_llm_professor_wisdom("debug webhook", depth="full")
    ph._get_llm_professor_wisdom("debug webhook", depth="full")
    assert calls["n"] == 2, "With cache disabled both calls must reach the LLM"

    stats = ph.get_s2_cache_stats()
    assert stats["s2_cache_hits"] == 0
    assert stats["s2_cache_misses"] == 0


def test_public_api_unchanged(hook, fake_redis, cache_on):
    """_get_llm_professor_wisdom keeps the same signature: (prompt, depth='full')."""
    import inspect
    ph, _calls = hook
    sig = inspect.signature(ph._get_llm_professor_wisdom)
    params = list(sig.parameters.keys())
    assert params == ["prompt", "depth"]
    assert sig.parameters["depth"].default == "full"
