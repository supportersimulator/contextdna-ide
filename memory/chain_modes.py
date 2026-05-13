"""Mode authority — named presets, resolution, adaptive suggestions.

Matches macbook1's orchestration layer design (63ca4eab), Section 3.
"""
from __future__ import annotations

from dataclasses import dataclass, field


PRESETS: dict[str, list[str]] = {
    "full-3s": [
        "pre-flight", "contradiction-scan", "risk-scan", "arch-gate",
        "plan-review", "pre-impl", "execute", "verify", "gains-gate", "doc-flow",
    ],
    "lightweight": ["pre-flight", "execute", "verify"],
    "plan-review": ["contradiction-scan", "plan-review", "pre-impl"],
    "evidence-dive": ["research-gather", "cross-check", "post-verify"],
}

TRIGGER_MAP: dict[str, str] = {
    "plan_file_detected": "plan-review",
    "large_task": "full-3s",
    "safety_critical": "full-3s",
    "test_only": "lightweight",
    "evidence_mismatch": "evidence-dive",
}

BACKOFF_THRESHOLD = 5


@dataclass
class Suggestion:
    """A mode suggestion from trigger detection."""
    mode: str
    trigger: str
    message: str


class ModeAuthority:
    """Manages named presets and adaptive mode suggestions."""

    def __init__(self, custom_presets: dict[str, list[str]] | None = None):
        self._presets = dict(PRESETS)
        if custom_presets:
            self._presets.update(custom_presets)
        self._preferences: dict[str, dict[str, int]] = {}

    def resolve(self, mode: str, overrides: dict[str, list[str]] | None = None) -> list[str]:
        if mode not in self._presets:
            raise KeyError(f"Unknown preset: '{mode}'")
        segments = list(self._presets[mode])
        if overrides:
            if "add" in overrides:
                for s in overrides["add"]:
                    if s not in segments:
                        segments.append(s)
            if "remove" in overrides:
                segments = [s for s in segments if s not in overrides["remove"]]
        return segments

    def suggest(self, trigger: str) -> Suggestion | None:
        mode = TRIGGER_MAP.get(trigger)
        if mode is None:
            return None
        stats = self.get_preference_stats(mode)
        if stats["rejected"] >= BACKOFF_THRESHOLD:
            return None
        return Suggestion(mode=mode, trigger=trigger,
                          message=f"Detected '{trigger}' — suggesting '{mode}' mode")

    def record_preference(self, mode: str, accepted: bool) -> None:
        if mode not in self._preferences:
            self._preferences[mode] = {"accepted": 0, "rejected": 0}
        self._preferences[mode]["accepted" if accepted else "rejected"] += 1

    def get_preference_stats(self, mode: str) -> dict[str, int]:
        return self._preferences.get(mode, {"accepted": 0, "rejected": 0})
