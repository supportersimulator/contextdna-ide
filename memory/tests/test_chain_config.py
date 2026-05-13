"""Tests for chain_config."""
import pytest
from memory.chain_config import ChainConfig, ConsultationConfig, TelemetryConfig


def test_chain_config_defaults():
    cfg = ChainConfig()
    assert cfg.default_mode == "lightweight"
    assert cfg.auto_suggest is True


def test_consultation_config_defaults():
    cfg = ConsultationConfig()
    assert cfg.cadence == 20
    assert cfg.community_sync is True
    assert cfg.community_branch == "community-chains"
    assert cfg.auto_accept_threshold == 0.90
    assert cfg.budget_per_consultation_usd == 0.02


def test_telemetry_config_defaults():
    cfg = TelemetryConfig()
    assert cfg.enabled is True
    assert cfg.retention_days == 90
    assert cfg.min_observations_for_pattern == 5
    assert cfg.min_frequency_for_pattern == 0.75
    assert cfg.min_observations_for_dependency == 20
    assert cfg.min_correlation_for_dependency == 0.80


def test_chain_config_custom():
    cfg = ChainConfig(default_mode="full-3s", auto_suggest=False)
    assert cfg.default_mode == "full-3s"
    assert cfg.auto_suggest is False


def test_config_from_dict():
    cfg = ChainConfig.from_dict({"default_mode": "plan-review", "auto_suggest": True})
    assert cfg.default_mode == "plan-review"


def test_config_from_dict_ignores_extra_keys():
    cfg = ChainConfig.from_dict({"default_mode": "lightweight", "unknown_key": True})
    assert cfg.default_mode == "lightweight"


def test_telemetry_config_from_dict():
    cfg = TelemetryConfig.from_dict({"enabled": False, "retention_days": 30})
    assert cfg.enabled is False
    assert cfg.retention_days == 30
    assert cfg.min_observations_for_pattern == 5
