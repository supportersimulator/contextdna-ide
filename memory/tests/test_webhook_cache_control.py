"""
RACE T4 — Tests for the webhook cache control plane.

Covers:
  * invalidate_section: clears only the named prefix, leaves others intact
  * invalidate_section: unknown section -> ValueError
  * invalidate_section: Redis unavailable -> errors=1, ZSF (counted, not silent)
  * invalidate_section: per-key delete failure -> errors+=1, scan continues
  * invalidate_all_webhook_caches: wipes every prefix; failure in one
    section does NOT short-circuit the others
  * cache_stats: aggregates across all 4 sections + reports redis_available
  * cache_config: respects per-section enable/TTL env vars (default + override)
  * event_subjects + handle_invalidation_event: NATS hook routes through the
    public invalidation path

A FakeRedis with optional fail-on-delete + fail-on-scan modes is used so no
real Redis is touched.
"""

from __future__ import annotations

import sys
import types

import pytest


# =========================================================================
# FakeRedis (scan_iter + delete + get/incr; programmable failures)
# =========================================================================

class _FakeRedis:
    """Minimal Redis stand-in with the surface webhook_cache_control needs."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self.fail_scan: bool = False
        # Set of full key names that should raise on delete()
        self.fail_delete_keys: set[str] = set()
        # If True, every op raises (full Redis outage)
        self.fail_all: bool = False

    # ---- helpers --------------------------------------------------------
    def _maybe_fail(self) -> None:
        if self.fail_all:
            raise ConnectionError("simulated redis outage")

    def _set(self, key: str, value: str) -> None:
        """Test-only seeding helper."""
        self._store[key] = value

    # ---- Redis API ------------------------------------------------------
    def get(self, key: str):
        self._maybe_fail()
        if key in self._counters:
            return str(self._counters[key])
        return self._store.get(key)

    def incr(self, key: str) -> int:
        self._maybe_fail()
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def delete(self, key: str) -> int:
        self._maybe_fail()
        if key in self.fail_delete_keys:
            raise ConnectionError(f"simulated delete failure for {key}")
        existed = key in self._store
        self._store.pop(key, None)
        return 1 if existed else 0

    def scan_iter(self, match: str, count: int = 200):  # noqa: ARG002
        self._maybe_fail()
        if self.fail_scan:
            raise ConnectionError("simulated scan failure")
        # Prefix-match (the only form webhook_cache_control uses: "<prefix>*")
        if not match.endswith("*"):
            raise ValueError(f"FakeRedis only supports prefix*: {match}")
        prefix = match[:-1]
        # snapshot the keys to allow safe deletion during iteration
        for k in list(self._store.keys()):
            if k.startswith(prefix):
                yield k

    def ping(self) -> bool:
        self._maybe_fail()
        return True


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def fake_redis(monkeypatch):
    """Inject a FakeRedis as memory.redis_cache.get_redis_client()."""
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
def wcc(fake_redis):
    """Import (or reload) the module under test against the fake Redis."""
    import importlib
    import memory.webhook_cache_control as mod
    importlib.reload(mod)
    return mod


def _seed_all_sections(fake: _FakeRedis) -> None:
    """Put one cache key under every section prefix so we can prove isolation."""
    fake._set("contextdna:s2:cache:abc", "wisdom-1")
    fake._set("contextdna:s2:cache:def", "wisdom-2")
    fake._set("contextdna:s6:cache:xyz", "voice-1")
    fake._set("contextdna:s8:cache:111", "subc-1")
    fake._set("contextdna:s4:blueprint:cache:222", "bp-1")
    fake._set("contextdna:s4:blueprint:cache:333", "bp-2")


# =========================================================================
# invalidate_section
# =========================================================================

def test_invalidate_section_clears_only_named_prefix(wcc, fake_redis):
    _seed_all_sections(fake_redis)

    result = wcc.invalidate_section("s2")
    assert result == {"section": "s2", "cleared": 2, "errors": 0}

    # S2 keys gone, others still present
    assert "contextdna:s2:cache:abc" not in fake_redis._store
    assert "contextdna:s2:cache:def" not in fake_redis._store
    assert fake_redis._store["contextdna:s6:cache:xyz"] == "voice-1"
    assert fake_redis._store["contextdna:s8:cache:111"] == "subc-1"
    assert fake_redis._store["contextdna:s4:blueprint:cache:222"] == "bp-1"


def test_invalidate_section_unknown_raises(wcc):
    with pytest.raises(ValueError):
        wcc.invalidate_section("s99")


def test_invalidate_section_redis_unavailable_counts_error(wcc, fake_redis, monkeypatch):
    # Force get_redis_client() to return None
    monkeypatch.setattr(
        sys.modules["memory.redis_cache"], "get_redis_client", lambda: None
    )
    result = wcc.invalidate_section("s6")
    assert result == {"section": "s6", "cleared": 0, "errors": 1}


def test_invalidate_section_per_key_delete_failure_continues(wcc, fake_redis):
    fake_redis._set("contextdna:s8:cache:keep1", "v1")
    fake_redis._set("contextdna:s8:cache:fail1", "v2")
    fake_redis._set("contextdna:s8:cache:keep2", "v3")
    fake_redis.fail_delete_keys = {"contextdna:s8:cache:fail1"}

    result = wcc.invalidate_section("s8")
    # Two succeed, one fails
    assert result["cleared"] == 2
    assert result["errors"] == 1
    # The failing key is still in the store; successful ones are gone
    assert "contextdna:s8:cache:fail1" in fake_redis._store
    assert "contextdna:s8:cache:keep1" not in fake_redis._store
    assert "contextdna:s8:cache:keep2" not in fake_redis._store
    # invalidation_errors counter bumped (ZSF)
    assert fake_redis._counters.get(
        "contextdna:s8:stats:invalidation_errors", 0
    ) == 1


def test_invalidate_section_scan_failure_counted(wcc, fake_redis):
    fake_redis._set("contextdna:s4:blueprint:cache:a", "x")
    fake_redis.fail_scan = True

    result = wcc.invalidate_section("s4")
    assert result["cleared"] == 0
    assert result["errors"] == 1
    # invalidation_errors bumped
    assert fake_redis._counters.get(
        "contextdna:s4:blueprint:stats:invalidation_errors", 0
    ) == 1


# =========================================================================
# invalidate_all_webhook_caches
# =========================================================================

def test_invalidate_all_clears_every_prefix(wcc, fake_redis):
    _seed_all_sections(fake_redis)
    # Put a non-cache key that must NOT be touched
    fake_redis._set("contextdna:other:thing", "do-not-touch")

    out = wcc.invalidate_all_webhook_caches()
    assert out["s2_cleared"] == 2
    assert out["s6_cleared"] == 1
    assert out["s8_cleared"] == 1
    assert out["s4_cleared"] == 2
    assert out["total"] == 6
    assert out["errors"] == 0

    # Every cache prefix is empty
    for k in list(fake_redis._store.keys()):
        assert not k.startswith("contextdna:s2:cache:")
        assert not k.startswith("contextdna:s6:cache:")
        assert not k.startswith("contextdna:s8:cache:")
        assert not k.startswith("contextdna:s4:blueprint:cache:")
    # Untouched key still there
    assert fake_redis._store["contextdna:other:thing"] == "do-not-touch"


def test_invalidate_all_does_not_short_circuit_on_section_failure(wcc, fake_redis):
    _seed_all_sections(fake_redis)
    # Make S6 deletes fail; S2/S8/S4 should still be wiped
    fake_redis.fail_delete_keys = {"contextdna:s6:cache:xyz"}

    out = wcc.invalidate_all_webhook_caches()
    assert out["s2_cleared"] == 2
    assert out["s8_cleared"] == 1
    assert out["s4_cleared"] == 2
    # s6 hit the failure path
    assert out["s6_cleared"] == 0
    assert out["errors"] >= 1


# =========================================================================
# cache_stats
# =========================================================================

def test_cache_stats_aggregates_all_sections(wcc, fake_redis):
    # Seed counters per section
    fake_redis._counters["contextdna:s2:stats:hits"] = 10
    fake_redis._counters["contextdna:s2:stats:misses"] = 4
    fake_redis._counters["contextdna:s2:stats:errors"] = 1
    fake_redis._counters["contextdna:s6:stats:hits"] = 5
    fake_redis._counters["contextdna:s6:stats:misses"] = 2
    fake_redis._counters["contextdna:s8:stats:hits"] = 3
    fake_redis._counters["contextdna:s4:blueprint:stats:hits"] = 7
    fake_redis._counters["contextdna:s4:blueprint:stats:invalidation_errors"] = 2

    stats = wcc.cache_stats()
    assert stats["redis_available"] is True

    sections = stats["sections"]
    assert sections["s2"]["hits"] == 10
    assert sections["s2"]["misses"] == 4
    assert sections["s2"]["errors"] == 1
    assert sections["s6"]["hits"] == 5
    assert sections["s6"]["misses"] == 2
    assert sections["s8"]["hits"] == 3
    assert sections["s4"]["hits"] == 7
    assert sections["s4"]["invalidation_errors"] == 2

    totals = stats["totals"]
    assert totals["hits"] == 10 + 5 + 3 + 7
    assert totals["misses"] == 4 + 2
    assert totals["errors"] == 1
    assert totals["invalidation_errors"] == 2


def test_cache_stats_redis_unavailable_returns_zeros(wcc, fake_redis, monkeypatch):
    monkeypatch.setattr(
        sys.modules["memory.redis_cache"], "get_redis_client", lambda: None
    )
    stats = wcc.cache_stats()
    assert stats["redis_available"] is False
    for sec in ("s2", "s6", "s8", "s4"):
        assert stats["sections"][sec] == {
            "hits": 0, "misses": 0, "errors": 0, "invalidation_errors": 0
        }
    assert stats["totals"] == {
        "hits": 0, "misses": 0, "errors": 0, "invalidation_errors": 0
    }


# =========================================================================
# cache_config
# =========================================================================

def test_cache_config_default_enabled_and_default_ttl(wcc, monkeypatch):
    # Strip any operator overrides
    for env in (
        "S2_CACHE_ENABLED", "S2_CACHE_TTL_S",
        "S6_CACHE_ENABLED", "S6_CACHE_TTL_S",
        "S8_CACHE_ENABLED", "S8_CACHE_TTL_S",
        "S4_BLUEPRINT_CACHE_ENABLED", "S4_BLUEPRINT_CACHE_TTL_S",
    ):
        monkeypatch.delenv(env, raising=False)

    cfg = wcc.cache_config()["sections"]
    assert cfg["s2"]["enabled"] is True
    assert cfg["s2"]["ttl_seconds"] == 600
    assert cfg["s6"]["ttl_seconds"] == 300
    assert cfg["s8"]["ttl_seconds"] == 600
    assert cfg["s4"]["ttl_seconds"] == 1800
    assert cfg["s4"]["enabled_env"] == "S4_BLUEPRINT_CACHE_ENABLED"


def test_cache_config_respects_disable_and_ttl_override(wcc, monkeypatch):
    monkeypatch.setenv("S2_CACHE_ENABLED", "0")
    monkeypatch.setenv("S6_CACHE_TTL_S", "120")
    monkeypatch.setenv("S8_CACHE_ENABLED", "false")
    monkeypatch.setenv("S4_BLUEPRINT_CACHE_TTL_S", "3600")

    cfg = wcc.cache_config()["sections"]
    assert cfg["s2"]["enabled"] is False
    assert cfg["s6"]["ttl_seconds"] == 120
    assert cfg["s8"]["enabled"] is False
    assert cfg["s4"]["ttl_seconds"] == 3600


def test_cache_config_invalid_ttl_falls_back_to_default(wcc, monkeypatch):
    monkeypatch.setenv("S6_CACHE_TTL_S", "not-an-int")
    cfg = wcc.cache_config()["sections"]
    assert cfg["s6"]["ttl_seconds"] == 300


# =========================================================================
# NATS event hook
# =========================================================================

def test_event_subjects_includes_config_and_restart(wcc):
    subjects = wcc.event_subjects()
    assert "event.config.changed" in subjects
    assert "event.fleet.restart" in subjects


def test_handle_invalidation_event_wipes_everything(wcc, fake_redis):
    _seed_all_sections(fake_redis)
    out = wcc.handle_invalidation_event("event.config.changed")
    assert out["subject"] == "event.config.changed"
    assert out["total"] == 6
    # Every cache prefix is empty
    for k in list(fake_redis._store.keys()):
        assert "cache" not in k or not (
            k.startswith("contextdna:s2:cache:")
            or k.startswith("contextdna:s6:cache:")
            or k.startswith("contextdna:s8:cache:")
            or k.startswith("contextdna:s4:blueprint:cache:")
        )


def test_handle_invalidation_event_unknown_subject_still_invalidates(wcc, fake_redis, caplog):
    _seed_all_sections(fake_redis)
    with caplog.at_level("WARNING"):
        out = wcc.handle_invalidation_event("event.something.else")
    assert out["subject"] == "event.something.else"
    assert out["total"] == 6
    # Warned about the unexpected subject (visibility, not silence)
    assert any("unexpected subject" in r.message for r in caplog.records)


# =========================================================================
# Public API surface
# =========================================================================

def test_known_sections_lists_all_four(wcc):
    sections = set(wcc.known_sections())
    assert sections == {"s2", "s6", "s8", "s4"}
