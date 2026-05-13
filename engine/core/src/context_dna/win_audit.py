#!/usr/bin/env python3
"""
Win Audit System - Compare Detected vs Captured Wins

This module audits the success detection pipeline by comparing:
1. What successes were DETECTED (found by the detector)
2. What successes were CAPTURED (recorded to memory)

THE DNA PRINCIPLE:
The work_log (DNA) contains all interactions.
The detector reads the DNA and finds successes.
The brain captures those successes to permanent memory.
This audit ensures nothing falls through the cracks.

PURPOSE:
- Identify gaps in the capture pipeline
- Tune detection thresholds
- Find false positives (detected but shouldn't have been)
- Find false negatives (missed by detector)
- Generate recommendations for improvement

Usage:
    from memory.win_audit import WinAuditSystem

    audit = WinAuditSystem()
    report = audit.run_audit(hours=24)

    print(f"Detection accuracy: {report.accuracy:.2%}")
    print(f"Missed captures: {len(report.detected_not_captured)}")
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field, asdict

# Import components
try:
    from memory.enhanced_success_detector import (
        EnhancedSuccessDetector, EnhancedSuccess
    )
    ENHANCED_DETECTOR_AVAILABLE = True
except ImportError:
    ENHANCED_DETECTOR_AVAILABLE = False

try:
    from memory.objective_success import ObjectiveSuccessDetector
    BASIC_DETECTOR_AVAILABLE = True
except ImportError:
    BASIC_DETECTOR_AVAILABLE = False

try:
    from memory.architecture_enhancer import work_log
    WORK_LOG_AVAILABLE = True
except ImportError:
    WORK_LOG_AVAILABLE = False
    work_log = None

try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False

# Cache file for audit history
AUDIT_CACHE_FILE = Path(__file__).parent / ".win_audit_cache.json"


@dataclass
class DetectedSuccess:
    """A success that was detected."""
    task: str
    confidence: float
    timestamp: str
    evidence: List[str]
    detector_type: str  # 'enhanced' or 'basic'


@dataclass
class CapturedSuccess:
    """A success that was captured to memory."""
    task: str
    timestamp: str
    source: str  # Where it was captured from


@dataclass
class AuditReport:
    """Result of a win audit."""
    timestamp: str
    hours_analyzed: int
    detected_count: int
    captured_count: int
    detected_not_captured: List[Dict]  # Wins detected but not in memory
    captured_not_detected: List[Dict]  # Wins in memory but not detected
    matched: List[Dict]  # Detected AND captured
    accuracy: float  # Match rate
    precision: float  # Captured that were detected
    recall: float  # Detected that were captured
    recommendations: List[str]

    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return asdict(self)


class WinAuditSystem:
    """
    Audits the success detection and capture pipeline.

    Compares what was detected vs what was captured to identify
    gaps and tune the system.
    """

    def __init__(self):
        self.cache = self._load_cache()

        # Initialize detector
        if ENHANCED_DETECTOR_AVAILABLE:
            self.detector = EnhancedSuccessDetector()
            self.detector_type = "enhanced"
        elif BASIC_DETECTOR_AVAILABLE:
            self.detector = ObjectiveSuccessDetector()
            self.detector_type = "basic"
        else:
            self.detector = None
            self.detector_type = None

    def _load_cache(self) -> Dict:
        """Load audit cache."""
        if AUDIT_CACHE_FILE.exists():
            try:
                with open(AUDIT_CACHE_FILE) as f:
                    return json.load(f)
            except:
                pass
        return {"audits": [], "last_audit": None}

    def _save_cache(self):
        """Save audit cache."""
        with open(AUDIT_CACHE_FILE, "w") as f:
            json.dump(self.cache, f, indent=2, default=str)

    def run_audit(self, hours: int = 24) -> AuditReport:
        """
        Run a full win audit.

        Compares detected successes to captured successes.

        Args:
            hours: Hours of history to analyze

        Returns:
            AuditReport with full analysis
        """
        timestamp = datetime.now().isoformat()

        # Get detected successes
        detected = self._get_detected_successes(hours)

        # Get captured successes
        captured = self._get_captured_successes(hours)

        # Compare
        matched, detected_not_captured, captured_not_detected = self._compare(
            detected, captured
        )

        # Calculate metrics
        total_detected = len(detected)
        total_captured = len(captured)
        total_matched = len(matched)

        accuracy = total_matched / max(1, max(total_detected, total_captured))
        precision = total_matched / max(1, total_captured) if total_captured > 0 else 0
        recall = total_matched / max(1, total_detected) if total_detected > 0 else 0

        # Generate recommendations
        recommendations = self._generate_recommendations(
            detected_not_captured, captured_not_detected, accuracy
        )

        report = AuditReport(
            timestamp=timestamp,
            hours_analyzed=hours,
            detected_count=total_detected,
            captured_count=total_captured,
            detected_not_captured=[self._success_to_dict(s) for s in detected_not_captured],
            captured_not_detected=captured_not_detected,
            matched=[self._success_to_dict(s) for s in matched],
            accuracy=accuracy,
            precision=precision,
            recall=recall,
            recommendations=recommendations,
        )

        # Cache report
        self.cache["audits"].append({
            "timestamp": timestamp,
            "accuracy": accuracy,
            "detected": total_detected,
            "captured": total_captured,
        })
        self.cache["audits"] = self.cache["audits"][-50:]  # Keep last 50
        self.cache["last_audit"] = timestamp
        self._save_cache()

        return report

    def _get_detected_successes(self, hours: int) -> List[DetectedSuccess]:
        """Get successes that would be detected now."""
        if not WORK_LOG_AVAILABLE or not self.detector:
            return []

        entries = work_log.get_recent_entries(hours=hours, include_processed=True)

        if ENHANCED_DETECTOR_AVAILABLE:
            successes = self.detector.analyze_entries(entries)
            return [
                DetectedSuccess(
                    task=s.task,
                    confidence=s.confidence,
                    timestamp=s.timestamp,
                    evidence=s.evidence,
                    detector_type="enhanced"
                )
                for s in successes
                if s.confidence >= 0.5  # Include lower confidence for audit
            ]
        else:
            successes = self.detector.analyze_entries(entries)
            return [
                DetectedSuccess(
                    task=s.task,
                    confidence=s.confidence,
                    timestamp=s.timestamp,
                    evidence=s.evidence,
                    detector_type="basic"
                )
                for s in successes
            ]

    def _get_captured_successes(self, hours: int) -> List[CapturedSuccess]:
        """Get successes that were captured to memory."""
        captured = []

        # Get from work_log success entries
        if WORK_LOG_AVAILABLE:
            entries = work_log.get_recent_entries(hours=hours, include_processed=True)
            for entry in entries:
                if entry.get("entry_type") == "success":
                    captured.append(CapturedSuccess(
                        task=entry.get("content", ""),
                        timestamp=entry.get("timestamp", ""),
                        source="work_log"
                    ))

        # Get from Acontext if available
        if CONTEXT_DNA_AVAILABLE:
            try:
                memory = ContextDNAClient()
                # Query recent wins from Acontext
                results = memory.query(
                    "recent wins successes",
                    limit=50,
                    learning_type="win"
                )
                for r in results:
                    captured.append(CapturedSuccess(
                        task=r.get("title", r.get("content", ""))[:100],
                        timestamp=r.get("created_at", ""),
                        source="acontext"
                    ))
            except Exception:
                pass

        return captured

    def _compare(
        self,
        detected: List[DetectedSuccess],
        captured: List[CapturedSuccess]
    ) -> tuple:
        """Compare detected vs captured successes."""
        matched = []
        detected_not_captured = []

        # Build lookup for captured tasks
        captured_tasks = set()
        for c in captured:
            # Normalize task text for comparison
            task_key = self._normalize_task(c.task)
            captured_tasks.add(task_key)

        # Check each detected success
        for d in detected:
            task_key = self._normalize_task(d.task)
            if task_key in captured_tasks:
                matched.append(d)
            else:
                detected_not_captured.append(d)

        # Find captured but not detected
        detected_tasks = set(self._normalize_task(d.task) for d in detected)
        captured_not_detected = [
            {"task": c.task, "timestamp": c.timestamp, "source": c.source}
            for c in captured
            if self._normalize_task(c.task) not in detected_tasks
        ]

        return matched, detected_not_captured, captured_not_detected

    def _normalize_task(self, task: str) -> str:
        """Normalize task text for comparison."""
        # Lowercase, remove punctuation, take first 50 chars
        import re
        normalized = task.lower()
        normalized = re.sub(r'[^\w\s]', '', normalized)
        normalized = ' '.join(normalized.split())  # Normalize whitespace
        return normalized[:50]

    def _generate_recommendations(
        self,
        detected_not_captured: List,
        captured_not_detected: List,
        accuracy: float
    ) -> List[str]:
        """Generate recommendations based on audit results."""
        recommendations = []

        # Low accuracy
        if accuracy < 0.5:
            recommendations.append(
                "CRITICAL: Less than 50% match rate between detection and capture. "
                "Review the brain.run_cycle() capture logic."
            )

        # High false negatives (detected but not captured)
        if len(detected_not_captured) > 5:
            recommendations.append(
                f"HIGH: {len(detected_not_captured)} detected successes were not captured. "
                "Consider lowering capture threshold or running brain.run_cycle() more frequently."
            )

        # High false positives (captured but not detected)
        if len(captured_not_detected) > 5:
            recommendations.append(
                f"MEDIUM: {len(captured_not_detected)} captured successes were not detected. "
                "These may be manual captures or detector may need new patterns."
            )

        # Low confidence detections not captured
        low_conf_missed = [d for d in detected_not_captured if d.confidence < 0.7]
        if low_conf_missed:
            recommendations.append(
                f"INFO: {len(low_conf_missed)} low-confidence detections (0.5-0.7) were not captured. "
                "This is expected - only high-confidence (>=0.7) are auto-captured."
            )

        # Good performance
        if accuracy >= 0.8 and not recommendations:
            recommendations.append(
                "GOOD: High match rate between detection and capture. System is working well."
            )

        return recommendations

    def _success_to_dict(self, success: DetectedSuccess) -> Dict:
        """Convert DetectedSuccess to dict."""
        return {
            "task": success.task,
            "confidence": success.confidence,
            "timestamp": success.timestamp,
            "evidence": success.evidence,
            "detector_type": success.detector_type,
        }

    def get_historical_accuracy(self, count: int = 10) -> List[Dict]:
        """Get historical accuracy trend."""
        return self.cache.get("audits", [])[-count:]


def run_win_audit(hours: int = 24) -> AuditReport:
    """Convenience function to run an audit."""
    audit = WinAuditSystem()
    return audit.run_audit(hours)


# CLI interface
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Win Audit System - Compare detected vs captured wins")
        print("")
        print("Commands:")
        print("  audit [hours]           - Run audit (default 24 hours)")
        print("  history                 - Show historical accuracy")
        print("  status                  - Check system status")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "audit":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        print(f"Running win audit for last {hours} hours...")

        audit = WinAuditSystem()
        report = audit.run_audit(hours)

        print(f"\n{'='*60}")
        print("WIN AUDIT REPORT")
        print(f"{'='*60}")
        print(f"Timestamp: {report.timestamp}")
        print(f"Hours analyzed: {report.hours_analyzed}")
        print(f"\nMetrics:")
        print(f"  Detected: {report.detected_count}")
        print(f"  Captured: {report.captured_count}")
        print(f"  Matched:  {len(report.matched)}")
        print(f"\n  Accuracy:  {report.accuracy:.2%}")
        print(f"  Precision: {report.precision:.2%}")
        print(f"  Recall:    {report.recall:.2%}")

        if report.detected_not_captured:
            print(f"\nDetected but NOT captured ({len(report.detected_not_captured)}):")
            for d in report.detected_not_captured[:5]:
                print(f"  [{d['confidence']:.2f}] {d['task'][:60]}")
            if len(report.detected_not_captured) > 5:
                print(f"  ... and {len(report.detected_not_captured) - 5} more")

        if report.captured_not_detected:
            print(f"\nCaptured but NOT detected ({len(report.captured_not_detected)}):")
            for c in report.captured_not_detected[:5]:
                print(f"  [{c['source']}] {c['task'][:60]}")
            if len(report.captured_not_detected) > 5:
                print(f"  ... and {len(report.captured_not_detected) - 5} more")

        print(f"\nRecommendations:")
        for r in report.recommendations:
            print(f"  - {r}")

    elif cmd == "history":
        audit = WinAuditSystem()
        history = audit.get_historical_accuracy()

        if not history:
            print("No audit history yet")
        else:
            print("Historical Accuracy:")
            for h in history:
                print(f"  {h['timestamp'][:10]}: {h['accuracy']:.2%} "
                      f"(D:{h['detected']}, C:{h['captured']})")

    elif cmd == "status":
        print("Win Audit System Status:")
        print(f"  Work Log: {'Available' if WORK_LOG_AVAILABLE else 'Not available'}")
        print(f"  Enhanced Detector: {'Available' if ENHANCED_DETECTOR_AVAILABLE else 'Not available'}")
        print(f"  Basic Detector: {'Available' if BASIC_DETECTOR_AVAILABLE else 'Not available'}")
        print(f"  Context DNA API: {'Available' if CONTEXT_DNA_AVAILABLE else 'Not available'}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
