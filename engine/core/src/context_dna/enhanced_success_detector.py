#!/usr/bin/env python3
"""
Enhanced Success Detector - Aggregates All Detection Layers

This is the "subconscious mind" of success detection - combining multiple
layers of analysis to determine what truly constitutes a win.

THE DNA PRINCIPLE:
Every interaction is recorded in the work_dialogue_log (the DNA).
This detector reads that DNA and extracts the successful gene sequences.

LAYERS (in order):
1. Regex patterns (existing ObjectiveSuccessDetector) - Fast, reliable
2. Learned patterns (EvolvingPatternRegistry) - Grows over time
3. LLM semantic analysis (LLMSuccessAnalyzer) - Understands context
4. Temporal validation (TemporalValidator) - Ensures persistence

ADDITIVE: Wraps existing ObjectiveSuccessDetector, doesn't replace it.

Usage:
    from memory.enhanced_success_detector import EnhancedSuccessDetector

    detector = EnhancedSuccessDetector()

    # Analyze work log entries
    successes = detector.analyze_entries(entries)

    # Each success has enhanced confidence
    for s in successes:
        print(f"{s.task}: {s.confidence:.2f} ({s.detection_layers})")
"""

from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field

# Import existing objective success detector
try:
    from memory.objective_success import (
        ObjectiveSuccessDetector,
        ObjectiveSuccess,
        get_objective_successes,
    )
    OBJECTIVE_SUCCESS_AVAILABLE = True
except ImportError:
    OBJECTIVE_SUCCESS_AVAILABLE = False
    ObjectiveSuccessDetector = None
    ObjectiveSuccess = None

# Import new layers
try:
    from memory.pattern_registry import EvolvingPatternRegistry
    PATTERN_REGISTRY_AVAILABLE = True
except ImportError:
    PATTERN_REGISTRY_AVAILABLE = False
    EvolvingPatternRegistry = None

try:
    from memory.llm_success_analyzer import LLMSuccessAnalyzer
    LLM_ANALYZER_AVAILABLE = True
except ImportError:
    LLM_ANALYZER_AVAILABLE = False
    LLMSuccessAnalyzer = None

try:
    from memory.temporal_validator import TemporalValidator, ValidationResult
    TEMPORAL_VALIDATOR_AVAILABLE = True
except ImportError:
    TEMPORAL_VALIDATOR_AVAILABLE = False
    TemporalValidator = None


@dataclass
class EnhancedSuccess:
    """A success detected with enhanced confidence from multiple layers."""
    task: str
    details: str
    confidence: float  # 0.0 - 1.0 (enhanced)
    base_confidence: float  # Original regex confidence
    area: str
    timestamp: str
    evidence: List[str]
    detection_layers: List[str]  # Which layers contributed
    layer_modifiers: Dict[str, float]  # Modifier from each layer
    is_validated: bool  # Passed temporal validation

    @property
    def high_confidence(self) -> bool:
        """Is this a high-confidence success (>=0.7)?"""
        return self.confidence >= 0.7

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "task": self.task,
            "details": self.details,
            "confidence": self.confidence,
            "base_confidence": self.base_confidence,
            "area": self.area,
            "timestamp": self.timestamp,
            "evidence": self.evidence,
            "detection_layers": self.detection_layers,
            "layer_modifiers": self.layer_modifiers,
            "is_validated": self.is_validated,
            "high_confidence": self.high_confidence,
        }


class EnhancedSuccessDetector:
    """
    The aggregator - combines all detection layers.

    This is the subconscious processor that reads the DNA (work_log)
    and extracts verified successes using multiple analysis techniques.
    """

    def __init__(
        self,
        use_llm: bool = True,
        validation_window: int = 300,  # 5 minutes
        min_confidence_for_llm: float = 0.5,  # Only LLM analyze if above this
    ):
        """
        Initialize enhanced detector.

        Args:
            use_llm: Whether to use LLM semantic analysis
            validation_window: Seconds for temporal validation
            min_confidence_for_llm: Minimum base confidence to trigger LLM analysis
        """
        self.use_llm = use_llm
        self.validation_window = validation_window
        self.min_confidence_for_llm = min_confidence_for_llm

        # Initialize layers
        self._init_layers()

    def _init_layers(self):
        """Initialize all detection layers."""
        # Layer 1: Existing regex detector
        self.regex_detector = None
        if OBJECTIVE_SUCCESS_AVAILABLE:
            self.regex_detector = ObjectiveSuccessDetector()

        # Layer 2: Evolving pattern registry
        self.pattern_registry = None
        if PATTERN_REGISTRY_AVAILABLE:
            self.pattern_registry = EvolvingPatternRegistry()

        # Layer 3: LLM semantic analyzer
        self.llm_analyzer = None
        if LLM_ANALYZER_AVAILABLE and self.use_llm:
            self.llm_analyzer = LLMSuccessAnalyzer()

        # Layer 4: Temporal validator
        self.temporal_validator = None
        if TEMPORAL_VALIDATOR_AVAILABLE:
            self.temporal_validator = TemporalValidator(
                window_seconds=self.validation_window
            )

    def analyze_entries(self, entries: List[Dict]) -> List[EnhancedSuccess]:
        """
        Analyze work log entries for successes using all layers.

        Args:
            entries: Work log entries from WorkDialogueLog

        Returns:
            List of EnhancedSuccess with full confidence analysis
        """
        if not entries:
            return []

        enhanced_successes = []

        # LAYER 1: Run existing regex detection
        regex_successes = self._run_regex_detection(entries)

        # LAYER 2: Add learned pattern detection
        learned_successes = self._run_learned_pattern_detection(entries)

        # Merge successes (avoid duplicates based on timestamp)
        all_candidates = self._merge_candidates(regex_successes, learned_successes)

        # Process each candidate through remaining layers
        for candidate in all_candidates:
            enhanced = self._enhance_candidate(candidate, entries)
            if enhanced:
                enhanced_successes.append(enhanced)

        # LAYER 3 (Optional): Find implicit successes via LLM
        if self.llm_analyzer and self.llm_analyzer.available:
            implicit = self._find_implicit_successes(entries)
            enhanced_successes.extend(implicit)

        # Sort by confidence (highest first)
        enhanced_successes.sort(key=lambda x: x.confidence, reverse=True)

        return enhanced_successes

    def _run_regex_detection(self, entries: List[Dict]) -> List[Dict]:
        """Run Layer 1: Existing regex pattern detection."""
        if not self.regex_detector:
            return []

        # The existing detector expects a specific format
        successes = self.regex_detector.analyze_entries(entries)

        return [
            {
                "task": s.task,
                "details": ", ".join(s.evidence),
                "confidence": s.confidence,
                "area": s.area,
                "timestamp": s.timestamp,
                "evidence": s.evidence,
                "source": "regex",
            }
            for s in successes
        ]

    def _run_learned_pattern_detection(self, entries: List[Dict]) -> List[Dict]:
        """Run Layer 2: Learned pattern detection."""
        if not self.pattern_registry:
            return []

        successes = []
        learned_patterns = self.pattern_registry.get_learned_patterns()

        for entry in entries:
            content = entry.get("content", "")
            timestamp = entry.get("timestamp", datetime.now().isoformat())

            for pattern, confidence, evidence_type in learned_patterns:
                import re
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    successes.append({
                        "task": self._extract_task_from_content(content),
                        "details": f"Matched learned pattern: {match.group(0)}",
                        "confidence": confidence,
                        "area": entry.get("area", "general"),
                        "timestamp": timestamp,
                        "evidence": [f"learned_pattern:{evidence_type}"],
                        "source": "learned",
                    })
                    # Record match for pattern accuracy tracking
                    self.pattern_registry.record_pattern_match(
                        pattern, match.group(0), was_confirmed=False
                    )

        return successes

    def _merge_candidates(
        self,
        regex_successes: List[Dict],
        learned_successes: List[Dict]
    ) -> List[Dict]:
        """Merge candidates, avoiding duplicates."""
        all_candidates = []
        seen_timestamps = set()

        # Add regex successes first (they take priority)
        for s in regex_successes:
            ts = s.get("timestamp", "")[:19]  # Truncate to second
            if ts not in seen_timestamps:
                seen_timestamps.add(ts)
                all_candidates.append(s)

        # Add learned successes if not duplicates
        for s in learned_successes:
            ts = s.get("timestamp", "")[:19]
            if ts not in seen_timestamps:
                seen_timestamps.add(ts)
                all_candidates.append(s)

        return all_candidates

    def _enhance_candidate(
        self,
        candidate: Dict,
        entries: List[Dict]
    ) -> Optional[EnhancedSuccess]:
        """
        Enhance a candidate through LLM and temporal validation.

        This is where the subconscious processing happens.
        """
        base_confidence = candidate.get("confidence", 0.5)
        layers_used = [candidate.get("source", "unknown")]
        modifiers = {candidate.get("source", "unknown"): 0.0}

        final_confidence = base_confidence

        # LAYER 3: LLM semantic analysis (if confidence warrants it)
        llm_modifier = 0.0
        if (
            self.llm_analyzer
            and self.llm_analyzer.available
            and base_confidence >= self.min_confidence_for_llm
        ):
            # Find the entry index
            entry_idx = self._find_entry_index(entries, candidate.get("timestamp"))
            if entry_idx >= 0:
                result = self.llm_analyzer.analyze_context(
                    entries,
                    candidate.get("details", ""),
                    success_index=entry_idx
                )
                llm_modifier = result.confidence_modifier

                if not result.is_success:
                    # LLM says this isn't a genuine success
                    return None

                layers_used.append("llm")
                modifiers["llm"] = llm_modifier
                final_confidence += llm_modifier

        # LAYER 4: Temporal validation
        is_validated = True
        temporal_modifier = 0.0
        if self.temporal_validator:
            # Get entries after this success
            entry_idx = self._find_entry_index(entries, candidate.get("timestamp"))
            entries_after = entries[entry_idx + 1:] if entry_idx >= 0 else []

            validation = self.temporal_validator.validate_persistence(
                candidate.get("timestamp", ""),
                candidate.get("task", ""),
                entries_after
            )

            is_validated = validation.is_valid
            temporal_modifier = validation.confidence_modifier

            if not is_validated:
                # Temporal validation failed - this was reversed
                return None

            layers_used.append("temporal")
            modifiers["temporal"] = temporal_modifier
            final_confidence += temporal_modifier

        # Clamp confidence to [0.0, 1.0]
        final_confidence = max(0.0, min(1.0, final_confidence))

        return EnhancedSuccess(
            task=candidate.get("task", "Unknown"),
            details=candidate.get("details", ""),
            confidence=final_confidence,
            base_confidence=base_confidence,
            area=candidate.get("area", "general"),
            timestamp=candidate.get("timestamp", datetime.now().isoformat()),
            evidence=candidate.get("evidence", []),
            detection_layers=layers_used,
            layer_modifiers=modifiers,
            is_validated=is_validated,
        )

    def _find_implicit_successes(self, entries: List[Dict]) -> List[EnhancedSuccess]:
        """Find successes that regex missed using LLM."""
        if not self.llm_analyzer or not self.llm_analyzer.available:
            return []

        implicit = self.llm_analyzer.detect_implicit_successes(entries)

        return [
            EnhancedSuccess(
                task=s.task,
                details=s.evidence,
                confidence=s.confidence,
                base_confidence=s.confidence,
                area="general",  # LLM doesn't detect area
                timestamp=s.timestamp,
                evidence=[f"implicit:{s.evidence[:50]}"],
                detection_layers=["llm_implicit"],
                layer_modifiers={"llm_implicit": 0.0},
                is_validated=True,  # Assumed validated by LLM analysis
            )
            for s in implicit
            if s.confidence >= 0.5
        ]

    def _find_entry_index(self, entries: List[Dict], timestamp: str) -> int:
        """Find index of entry with given timestamp."""
        ts = timestamp[:19] if timestamp else ""
        for i, entry in enumerate(entries):
            entry_ts = entry.get("timestamp", "")[:19]
            if entry_ts == ts:
                return i
        return -1

    def _extract_task_from_content(self, content: str) -> str:
        """Extract task description from content."""
        # Take first sentence or first 100 chars
        first_line = content.split("\n")[0]
        if len(first_line) > 100:
            return first_line[:97] + "..."
        return first_line

    def learn_from_confirmed(
        self,
        success: EnhancedSuccess,
        entries: List[Dict]
    ):
        """
        Feed confirmed success back to pattern registry for learning.

        Call this when a success is confirmed (e.g., auto-captured or
        user validated).
        """
        if not self.pattern_registry:
            return

        # Get context entries around the success
        entry_idx = self._find_entry_index(entries, success.timestamp)
        start = max(0, entry_idx - 3)
        end = min(len(entries), entry_idx + 3)
        context = entries[start:end]

        # Learn from this success
        self.pattern_registry.learn_from_confirmed(
            success.task,
            success.details,
            context
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get detector statistics."""
        stats = {
            "layers_available": [],
            "layers_unavailable": [],
        }

        if self.regex_detector:
            stats["layers_available"].append("regex")
        else:
            stats["layers_unavailable"].append("regex")

        if self.pattern_registry:
            stats["layers_available"].append("learned")
            stats["learned_patterns"] = self.pattern_registry.get_stats()
        else:
            stats["layers_unavailable"].append("learned")

        if self.llm_analyzer and self.llm_analyzer.available:
            stats["layers_available"].append("llm")
            stats["llm_status"] = self.llm_analyzer.get_status()
        else:
            stats["layers_unavailable"].append("llm")

        if self.temporal_validator:
            stats["layers_available"].append("temporal")
        else:
            stats["layers_unavailable"].append("temporal")

        return stats


def analyze_work_log_enhanced(hours: int = 24) -> List[EnhancedSuccess]:
    """
    Convenience function to analyze recent work log with enhanced detection.

    Returns list of EnhancedSuccess objects.
    """
    try:
        from memory.architecture_enhancer import work_log
        entries = work_log.get_recent_entries(hours=hours)
        detector = EnhancedSuccessDetector()
        return detector.analyze_entries(entries)
    except ImportError:
        return []


# CLI interface
if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Enhanced Success Detector - Multi-layer success detection")
        print("")
        print("Commands:")
        print("  analyze [hours]         - Analyze recent work log")
        print("  status                  - Show detector status")
        print("  high-confidence [hours] - Show only high-confidence successes")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status":
        detector = EnhancedSuccessDetector()
        stats = detector.get_stats()
        print("Enhanced Success Detector Status:")
        print(f"  Layers available: {', '.join(stats['layers_available']) or 'None'}")
        print(f"  Layers unavailable: {', '.join(stats['layers_unavailable']) or 'None'}")

        if "learned_patterns" in stats:
            lp = stats["learned_patterns"]
            print(f"\nLearned Patterns:")
            print(f"  Total: {lp['total_learned']}")
            print(f"  High confidence: {lp['high_confidence']}")

        if "llm_status" in stats:
            llm = stats["llm_status"]
            print(f"\nLLM Status:")
            print(f"  Available: {llm['available']}")
            print(f"  Endpoint: {llm.get('llm_endpoint', 'None')}")

    elif cmd == "analyze":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        print(f"Analyzing last {hours} hours of work log...")

        successes = analyze_work_log_enhanced(hours)

        if not successes:
            print("No successes detected")
        else:
            print(f"\nFound {len(successes)} success(es):\n")
            for s in successes:
                print(f"[{s.confidence:.2f}] {s.task}")
                print(f"        Layers: {', '.join(s.detection_layers)}")
                print(f"        Area: {s.area}")
                print(f"        Validated: {s.is_validated}")
                print()

    elif cmd == "high-confidence":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        print(f"Finding high-confidence successes (last {hours} hours)...")

        successes = analyze_work_log_enhanced(hours)
        high_conf = [s for s in successes if s.high_confidence]

        if not high_conf:
            print("No high-confidence successes found")
        else:
            print(f"\nFound {len(high_conf)} high-confidence success(es):\n")
            for s in high_conf:
                print(f"[{s.confidence:.2f}] {s.task}")
                print(f"        Layers: {', '.join(s.detection_layers)}")
                print()

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
