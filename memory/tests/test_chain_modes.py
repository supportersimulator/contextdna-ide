"""Tests for chain_modes — ModeAuthority with presets and adaptive suggestions."""
import pytest
from memory.chain_modes import ModeAuthority, PRESETS, Suggestion


def test_presets_exist():
    assert "full-3s" in PRESETS
    assert "lightweight" in PRESETS
    assert "plan-review" in PRESETS
    assert "evidence-dive" in PRESETS


def test_full_3s_has_10_segments():
    assert len(PRESETS["full-3s"]) == 10


def test_lightweight_has_3_segments():
    assert len(PRESETS["lightweight"]) == 3


def test_resolve_returns_segment_list():
    ma = ModeAuthority()
    assert ma.resolve("lightweight") == PRESETS["lightweight"]


def test_resolve_with_overrides():
    ma = ModeAuthority()
    segments = ma.resolve("lightweight", overrides={"add": ["risk-scan"]})
    assert "risk-scan" in segments
    for s in PRESETS["lightweight"]:
        assert s in segments


def test_resolve_with_remove_override():
    ma = ModeAuthority()
    segments = ma.resolve("full-3s", overrides={"remove": ["doc-flow"]})
    assert "doc-flow" not in segments
    assert "pre-flight" in segments


def test_resolve_unknown_preset_raises():
    ma = ModeAuthority()
    with pytest.raises(KeyError):
        ma.resolve("nonexistent")


def test_suggest_plan_file_trigger():
    ma = ModeAuthority()
    s = ma.suggest(trigger="plan_file_detected")
    assert s is not None
    assert isinstance(s, Suggestion)
    assert s.mode == "plan-review"


def test_suggest_large_task_trigger():
    s = ModeAuthority().suggest(trigger="large_task")
    assert s is not None
    assert s.mode == "full-3s"


def test_suggest_unknown_trigger():
    assert ModeAuthority().suggest(trigger="unknown_trigger") is None


def test_suggest_safety_critical():
    s = ModeAuthority().suggest(trigger="safety_critical")
    assert s is not None
    assert s.mode == "full-3s"


def test_record_preference():
    ma = ModeAuthority()
    ma.record_preference("plan-review", accepted=True)
    ma.record_preference("plan-review", accepted=True)
    ma.record_preference("plan-review", accepted=False)
    stats = ma.get_preference_stats("plan-review")
    assert stats["accepted"] == 2
    assert stats["rejected"] == 1


def test_suggest_backoff_after_ignores():
    ma = ModeAuthority()
    for _ in range(5):
        ma.record_preference("plan-review", accepted=False)
    assert ma.suggest(trigger="plan_file_detected") is None


def test_custom_presets():
    ma = ModeAuthority(custom_presets={"my-chain": ["step-a", "step-b"]})
    assert ma.resolve("my-chain") == ["step-a", "step-b"]
