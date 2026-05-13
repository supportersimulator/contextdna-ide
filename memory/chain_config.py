"""Chain configuration dataclasses.

Matches macbook1's orchestration layer design (63ca4eab), Section 6.
"""
from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass
class ChainConfig:
    default_mode: str = "lightweight"
    auto_suggest: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> ChainConfig:
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid})


@dataclass
class ConsultationConfig:
    cadence: int = 20
    community_sync: bool = True
    community_repo: str = "origin"
    community_branch: str = "community-chains"
    auto_accept_threshold: float = 0.90
    budget_per_consultation_usd: float = 0.02

    @classmethod
    def from_dict(cls, d: dict) -> ConsultationConfig:
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid})


@dataclass
class TelemetryConfig:
    enabled: bool = True
    retention_days: int = 90
    min_observations_for_pattern: int = 5
    min_frequency_for_pattern: float = 0.75
    min_observations_for_dependency: int = 20
    min_correlation_for_dependency: float = 0.80

    @classmethod
    def from_dict(cls, d: dict) -> TelemetryConfig:
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid})
