#!/usr/bin/env python3
"""GG4 — Tests for additive cost-tier / role-class / context-limit primitives.

Coverage:
1. Enum membership for ``CostTier`` and ``RoleClass``.
2. ``cost_tier_downgrade_total`` ZSF counter increments on cap violation.
3. ``LLM_MAX_COST_TIER`` env-var contract (LOW/MID/HIGH + unrecognized + empty).
4. Smoke: ``llm_generate`` signature + ``Priority`` shape unchanged
   (existing-call-site invariant).
5. ``memory.context_limit.model_context_limit`` lookup table + overflow helper.
6. Profile annotations exist for every shipped profile (no silent gaps).

Pure-Python tests — no LLM calls, no Redis, no NATS.

Run:  PYTHONPATH=. .venv/bin/python3 -m pytest memory/test_cost_tier_annotations.py -q
"""
from __future__ import annotations

import inspect
import os
from unittest import mock

import pytest

from memory import context_limit
from memory.context_limit import (
    DEFAULT_CONTEXT_LIMIT,
    MODEL_CONTEXT_LIMITS,
    model_context_limit,
    would_overflow,
)
from memory.llm_priority_queue import (
    CostTier,
    PROFILE_DEFAULT_COST_TIER,
    Priority,
    RoleClass,
    _cost_tier_rank,
    _resolve_max_cost_tier_env,
    _zsf_counters,
    apply_cost_tier_cap,
    llm_generate,
    llm_generate_typed,
)


# ── 1. Enum membership ─────────────────────────────────────────────────────

def test_cost_tier_members():
    names = {m.name for m in CostTier}
    assert names == {"LOW", "MID", "HIGH"}
    # String-valued so additions stay non-breaking.
    assert CostTier.LOW.value == "low"
    assert CostTier.MID.value == "mid"
    assert CostTier.HIGH.value == "high"


def test_role_class_members():
    names = {m.name for m in RoleClass}
    assert names == {"GENERALIST", "SPECIALIST", "JUDGE", "MEMORY"}


def test_cost_tier_rank_monotonic():
    assert _cost_tier_rank(CostTier.LOW) < _cost_tier_rank(CostTier.MID)
    assert _cost_tier_rank(CostTier.MID) < _cost_tier_rank(CostTier.HIGH)


# ── 2. Downgrade counter ───────────────────────────────────────────────────

def _counter() -> int:
    return _zsf_counters.get("cost_tier_downgrade_total", 0)


def test_apply_cost_tier_cap_no_downgrade_when_under_cap():
    before = _counter()
    out = apply_cost_tier_cap(CostTier.LOW, cap=CostTier.HIGH, caller="t", profile="t")
    assert out is CostTier.LOW
    assert _counter() == before


def test_apply_cost_tier_cap_no_downgrade_when_equal_cap():
    before = _counter()
    out = apply_cost_tier_cap(CostTier.MID, cap=CostTier.MID, caller="t", profile="t")
    assert out is CostTier.MID
    assert _counter() == before


def test_apply_cost_tier_cap_downgrades_and_increments():
    before = _counter()
    out = apply_cost_tier_cap(CostTier.HIGH, cap=CostTier.LOW, caller="t", profile="t")
    assert out is CostTier.LOW
    assert _counter() == before + 1


def test_apply_cost_tier_cap_no_cap_means_passthrough():
    before = _counter()
    out = apply_cost_tier_cap(CostTier.HIGH, cap=None, caller="t", profile="t")
    # With no env override either, expect identity.
    with mock.patch.dict(os.environ, {"LLM_MAX_COST_TIER": ""}, clear=False):
        out = apply_cost_tier_cap(CostTier.HIGH, cap=None, caller="t", profile="t")
    assert out is CostTier.HIGH
    assert _counter() == before


# ── 3. Env-var contract ────────────────────────────────────────────────────

def test_resolve_env_unset():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LLM_MAX_COST_TIER", None)
        assert _resolve_max_cost_tier_env() is None


def test_resolve_env_empty_string():
    with mock.patch.dict(os.environ, {"LLM_MAX_COST_TIER": ""}, clear=False):
        assert _resolve_max_cost_tier_env() is None


@pytest.mark.parametrize(
    "raw,expected",
    [("low", CostTier.LOW), ("LOW", CostTier.LOW),
     ("mid", CostTier.MID), ("HIGH", CostTier.HIGH)],
)
def test_resolve_env_recognised(raw, expected):
    with mock.patch.dict(os.environ, {"LLM_MAX_COST_TIER": raw}, clear=False):
        assert _resolve_max_cost_tier_env() is expected


def test_resolve_env_unrecognised_falls_open(capsys):
    with mock.patch.dict(os.environ, {"LLM_MAX_COST_TIER": "ultra"}, clear=False):
        assert _resolve_max_cost_tier_env() is None
    err = capsys.readouterr().err
    assert "LLM_MAX_COST_TIER" in err
    assert "ultra" in err


def test_env_var_caps_request():
    before = _counter()
    with mock.patch.dict(os.environ, {"LLM_MAX_COST_TIER": "low"}, clear=False):
        out = apply_cost_tier_cap(CostTier.HIGH, cap=None, caller="t", profile="t")
    assert out is CostTier.LOW
    assert _counter() == before + 1


# ── 4. Existing-call-site invariant (no signature drift) ───────────────────

def test_llm_generate_signature_unchanged():
    sig = inspect.signature(llm_generate)
    params = list(sig.parameters.keys())
    # Frozen by GG4 — adding params here would break MavKa-additive contract.
    assert params == [
        "system_prompt",
        "user_prompt",
        "priority",
        "profile",
        "caller",
        "timeout_s",
        "enable_thinking",
        "raise_on_preempt",
    ]


def test_priority_enum_values_unchanged():
    assert Priority.AARON.value == 1
    assert Priority.ATLAS.value == 2
    assert Priority.EXTERNAL.value == 3
    assert Priority.BACKGROUND.value == 4


def test_llm_generate_typed_delegates_without_touching_signature():
    """`llm_generate_typed` must call through to `llm_generate` with only
    the original kwargs — no leakage of cost_tier/role into the inner call.
    """
    captured = {}

    def fake_inner(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "ok"

    with mock.patch(
        "memory.llm_priority_queue.llm_generate", side_effect=fake_inner
    ):
        out = llm_generate_typed(
            "sys",
            "user",
            priority=Priority.ATLAS,
            profile="classify",
            caller="test",
            cost_tier=CostTier.LOW,
            role=RoleClass.JUDGE,
            max_cost_tier=CostTier.HIGH,
        )
    assert out == "ok"
    assert captured["args"] == ("sys", "user")
    assert "cost_tier" not in captured["kwargs"]
    assert "role" not in captured["kwargs"]
    assert "max_cost_tier" not in captured["kwargs"]
    assert captured["kwargs"]["priority"] is Priority.ATLAS
    assert captured["kwargs"]["profile"] == "classify"
    assert captured["kwargs"]["caller"] == "test"


def test_llm_generate_typed_applies_cap(capsys):
    before = _counter()
    with mock.patch(
        "memory.llm_priority_queue.llm_generate", return_value="ok"
    ):
        llm_generate_typed(
            "s", "u",
            profile="s8_synaptic",
            caller="cap-test",
            cost_tier=CostTier.HIGH,
            max_cost_tier=CostTier.LOW,
        )
    assert _counter() == before + 1
    err = capsys.readouterr().err
    assert "cost_tier_downgrade" in err


# ── 5. Profile annotations cover every shipped profile ─────────────────────

def test_profile_default_cost_tier_covers_known_profiles():
    # Hand-mirrored from `_get_generation_params` — bumps if a new profile
    # ships without a tier annotation, surfacing the gap loudly.
    expected = {
        "classify", "extract", "extract_deep",
        "coding", "explore", "voice", "deep", "reasoning",
        "summarize",
        "s2_professor", "s2_professor_brief", "s8_synaptic",
        "synaptic_chat", "post_analysis",
        "chain_narrow", "chain_extract", "chain_creative", "chain_thinking",
    }
    missing = expected - set(PROFILE_DEFAULT_COST_TIER)
    assert missing == set(), f"profiles missing cost_tier annotation: {missing}"


# ── 6. context_limit module ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "name,limit",
    [
        ("deepseek-chat", 64_000),
        ("gpt-4.1-mini", 128_000),
        ("qwen3-4b", 32_768),
        ("claude-haiku", 200_000),
        ("claude-opus", 200_000),
        ("claude-sonnet", 200_000),
    ],
)
def test_model_context_limit_known(name, limit):
    assert model_context_limit(name) == limit


def test_model_context_limit_case_insensitive():
    assert model_context_limit("DeepSeek-Chat") == 64_000


def test_model_context_limit_prefix_match():
    # E.g. "claude-opus-4.7" should resolve via prefix.
    assert model_context_limit("claude-opus-4.7") == 200_000


def test_model_context_limit_unknown_returns_default():
    assert model_context_limit("totally-made-up-model") == DEFAULT_CONTEXT_LIMIT
    assert model_context_limit("") == DEFAULT_CONTEXT_LIMIT


def test_would_overflow_basic():
    # qwen3-4b = 32_768; well under should be False, well over True.
    assert would_overflow(1_000, "qwen3-4b") is False
    assert would_overflow(50_000, "qwen3-4b") is True


def test_would_overflow_negative_or_none_safe():
    assert would_overflow(-1, "qwen3-4b") is False
    assert would_overflow(0, "qwen3-4b") is False


def test_context_limits_table_has_all_required_models():
    # GG4 spec: these six MUST be present.
    required = {
        "deepseek-chat", "gpt-4.1-mini", "qwen3-4b",
        "claude-haiku", "claude-opus", "claude-sonnet",
    }
    assert required.issubset(set(MODEL_CONTEXT_LIMITS))


# ── 7. Module-import smoke (no side effects) ───────────────────────────────

def test_context_limit_module_pure_data():
    # Importing must not have changed env or filesystem state in ways that
    # affect the rest of the suite. Sanity check key attribute existence.
    assert hasattr(context_limit, "MODEL_CONTEXT_LIMITS")
    assert hasattr(context_limit, "model_context_limit")
    assert hasattr(context_limit, "would_overflow")
    assert hasattr(context_limit, "DEFAULT_CONTEXT_LIMIT")
