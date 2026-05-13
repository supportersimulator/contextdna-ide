"""Chain consultation — surgeon consultation cadence and community presets.

Matches macbook1's orchestration layer design (63ca4eab), Section 5.
"""
from __future__ import annotations

from dataclasses import dataclass
from memory.chain_requirements import CommandRequirements

import yaml


META_REVIEW_REQS = CommandRequirements(
    min_llms=2, needs_state=True, needs_evidence=True, recommended_llms=3,
)


def should_consult(total_executions: int, last_consultation_at: int, cadence: int = 20) -> bool:
    return (total_executions - last_consultation_at) >= cadence


@dataclass
class CommunityPreset:
    name: str
    segments: list[str]
    evidence_grade: str
    observations: int
    consensus_score: float
    description: str = ""

    def to_yaml(self) -> str:
        return yaml.dump({
            "name": self.name, "segments": self.segments,
            "evidence_grade": self.evidence_grade, "observations": self.observations,
            "consensus_score": self.consensus_score, "description": self.description,
        }, default_flow_style=False)

    @classmethod
    def from_yaml(cls, data: str) -> CommunityPreset:
        return cls(**yaml.safe_load(data))


class ChainConsultation:
    def __init__(self):
        self.last_consultation_at: int = 0

    def build_consultation_context(self, segments, presets, recent_failures) -> str:
        parts = ["## Available Segments", ", ".join(segments), "", "## Current Presets"]
        for name, segs in presets.items():
            parts.append(f"- {name}: {', '.join(segs)}")
        parts.extend(["", "## Recent Failures"])
        for f in recent_failures:
            parts.append(f"- {f}")
        return "\n".join(parts)

    def mark_consulted(self, at_execution: int) -> None:
        self.last_consultation_at = at_execution
