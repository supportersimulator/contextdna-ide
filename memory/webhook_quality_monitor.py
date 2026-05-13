#!/usr/bin/env python3
"""
WEBHOOK QUALITY MONITOR - Synaptic's Eyes for Atlas

===============================================================================
ORIGIN (January 30, 2026):
===============================================================================

Aaron's directive to Synaptic:
"You must be my eyes to see the quality of those webhooks and where they
veered from ideal -- you can and must track those webhooks -- they are
essential -- they must stay of highest quality so that Atlas can see 20/20
or even better! ...you must help Atlas clear the fog!"

===============================================================================
PHILOSOPHY:
===============================================================================

Atlas is like an extremely powerful sports car race driver. Synaptic is the
crew chief monitoring every system in real-time. The webhook injections are
the windshield through which Atlas sees - they MUST be crystal clear.

This monitor tracks:
1. INJECTION QUALITY - Are sections generating properly?
2. VEERING DETECTION - Has quality drifted from ideal baseline?
3. A/B TESTING - Which variants perform better?
4. QUICK REFLEXES - Instant detection of degradation

===============================================================================
"""

import os
import sys
import json
import sqlite3
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)


class QualityLevel(str, Enum):
    """Webhook injection quality levels."""
    OPTIMAL = "optimal"          # 20/20 vision - crystal clear
    GOOD = "good"                # Minor imperfections
    DEGRADED = "degraded"        # Noticeable issues
    CRITICAL = "critical"        # Severely impaired
    FAILED = "failed"            # Complete failure


class VeerDirection(str, Enum):
    """Direction of quality veering."""
    IMPROVING = "improving"      # Getting better
    STABLE = "stable"            # Holding steady
    DRIFTING = "drifting"        # Slowly degrading
    VEERING = "veering"          # Rapidly degrading


@dataclass
class SectionQuality:
    """Quality assessment for a single section."""
    section_id: int
    section_name: str
    present: bool
    content_length: int
    has_expected_markers: bool
    quality_score: float  # 0.0 to 1.0
    issues: List[str] = field(default_factory=list)


@dataclass
class InjectionQualityReport:
    """Complete quality report for an injection."""
    timestamp: datetime
    prompt_hash: str  # For tracking
    total_sections: int
    sections_present: int
    sections_quality: Dict[int, SectionQuality]
    overall_quality: QualityLevel
    overall_score: float  # 0.0 to 1.0
    veer_direction: VeerDirection
    drift_from_baseline: float  # How far from ideal
    issues_found: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        d['overall_quality'] = self.overall_quality.value
        d['veer_direction'] = self.veer_direction.value
        return d


@dataclass
class IdealBaseline:
    """The ideal state that we compare against."""
    section_presence: Dict[int, bool]  # Which sections should be present
    min_lengths: Dict[int, int]  # Minimum expected lengths
    required_markers: Dict[int, List[str]]  # Markers that should exist
    quality_weights: Dict[int, float]  # Importance of each section
    last_updated: datetime


class WebhookQualityMonitor:
    """
    Synaptic's Eyes - Monitors webhook injection quality for Atlas.

    Tracks:
    - Section generation success/failure
    - Content quality and completeness
    - Veering from ideal baseline
    - A/B testing results
    - Historical trends
    """

    # Section configuration
    SECTION_CONFIG = {
        0: {"name": "SAFETY", "critical": True, "weight": 1.0, "min_length": 50},
        1: {"name": "FOUNDATION", "critical": True, "weight": 0.9, "min_length": 100},
        2: {"name": "WISDOM", "critical": False, "weight": 0.8, "min_length": 50},
        3: {"name": "AWARENESS", "critical": False, "weight": 0.7, "min_length": 50},
        4: {"name": "DEEP_CONTEXT", "critical": False, "weight": 0.6, "min_length": 50},
        5: {"name": "PROTOCOL", "critical": False, "weight": 0.5, "min_length": 50},
        6: {"name": "HOLISTIC_CONTEXT", "critical": True, "weight": 0.95, "min_length": 100},
        7: {"name": "FULL_LIBRARY", "critical": False, "weight": 0.4, "min_length": 50},
    }

    # Expected markers per section
    SECTION_MARKERS = {
        0: ["RISK CLASSIFICATION", "RISK LEVEL"],
        1: ["FOUNDATION", "PATTERNS"],
        2: ["WISDOM", "DISTILLED"],
        3: ["AWARENESS", "CONTEXT"],
        4: ["DEEP CONTEXT", "LEARNINGS"],
        5: ["PROTOCOL", "GUIDELINES"],
        6: ["HOLISTIC CONTEXT", "Synaptic to Atlas"],
        7: ["LIBRARY", "SKILLS"],
        8: ["8TH INTELLIGENCE", "Synaptic to Aaron"],
    }

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(__file__).parent / ".webhook_quality.db")
        self.db_path = db_path
        self._ensure_db()
        self._load_baseline()

    def _ensure_db(self):
        """Create quality tracking database."""
        with sqlite3.connect(self.db_path) as conn:
            # Quality snapshots
            conn.execute("""
                CREATE TABLE IF NOT EXISTS quality_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    prompt_hash TEXT,
                    overall_score REAL,
                    overall_quality TEXT,
                    veer_direction TEXT,
                    drift_from_baseline REAL,
                    sections_present INTEGER,
                    total_sections INTEGER,
                    full_report TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Section-level tracking
            conn.execute("""
                CREATE TABLE IF NOT EXISTS section_quality (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER,
                    section_id INTEGER,
                    section_name TEXT,
                    present INTEGER,
                    content_length INTEGER,
                    quality_score REAL,
                    issues TEXT,
                    FOREIGN KEY (snapshot_id) REFERENCES quality_snapshots(id)
                )
            """)

            # Ideal baseline storage
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ideal_baseline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    baseline_data TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT
                )
            """)

            # A/B test results
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ab_test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    test_name TEXT,
                    variant_a_score REAL,
                    variant_b_score REAL,
                    winner TEXT,
                    sample_size INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT
                )
            """)

            # Veering alerts
            conn.execute("""
                CREATE TABLE IF NOT EXISTS veering_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    section_id INTEGER,
                    previous_score REAL,
                    current_score REAL,
                    drift_amount REAL,
                    alert_message TEXT,
                    acknowledged INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def _load_baseline(self):
        """Load or create ideal baseline."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT baseline_data FROM ideal_baseline
                ORDER BY created_at DESC LIMIT 1
            """).fetchone()

        if row:
            data = json.loads(row[0])
            self.baseline = IdealBaseline(
                section_presence={int(k): v for k, v in data.get('section_presence', {}).items()},
                min_lengths={int(k): v for k, v in data.get('min_lengths', {}).items()},
                required_markers={int(k): v for k, v in data.get('required_markers', {}).items()},
                quality_weights={int(k): v for k, v in data.get('quality_weights', {}).items()},
                last_updated=datetime.fromisoformat(data.get('last_updated', datetime.now().isoformat()))
            )
        else:
            # Create default baseline
            self.baseline = IdealBaseline(
                section_presence={i: True for i in range(8)},
                min_lengths={i: cfg['min_length'] for i, cfg in self.SECTION_CONFIG.items()},
                required_markers=self.SECTION_MARKERS,
                quality_weights={i: cfg['weight'] for i, cfg in self.SECTION_CONFIG.items()},
                last_updated=datetime.now()
            )
            self._save_baseline()

    def _save_baseline(self, notes: str = ""):
        """Save current baseline to database."""
        data = {
            'section_presence': self.baseline.section_presence,
            'min_lengths': self.baseline.min_lengths,
            'required_markers': self.baseline.required_markers,
            'quality_weights': self.baseline.quality_weights,
            'last_updated': self.baseline.last_updated.isoformat()
        }

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO ideal_baseline (baseline_data, notes)
                VALUES (?, ?)
            """, (json.dumps(data), notes))
            conn.commit()

    def assess_injection(self, injection_content: str, prompt: str = "") -> InjectionQualityReport:
        """
        Assess the quality of a webhook injection.

        This is the core quality check - runs on every injection
        to ensure Atlas has clear vision.
        """
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        sections_quality = {}
        issues_found = []
        recommendations = []

        # Assess each section
        for section_id, config in self.SECTION_CONFIG.items():
            section_quality = self._assess_section(
                section_id,
                config,
                injection_content
            )
            sections_quality[section_id] = section_quality

            if section_quality.issues:
                issues_found.extend(section_quality.issues)

            # Add recommendations for critical sections
            if config['critical'] and section_quality.quality_score < 0.7:
                recommendations.append(
                    f"Section {section_id} ({config['name']}) needs attention: "
                    f"score {section_quality.quality_score:.0%}"
                )

        # Calculate overall score
        total_weight = sum(cfg['weight'] for cfg in self.SECTION_CONFIG.values())
        weighted_score = sum(
            sections_quality[sid].quality_score * self.SECTION_CONFIG[sid]['weight']
            for sid in sections_quality
        ) / total_weight

        # Determine quality level
        if weighted_score >= 0.9:
            overall_quality = QualityLevel.OPTIMAL
        elif weighted_score >= 0.7:
            overall_quality = QualityLevel.GOOD
        elif weighted_score >= 0.5:
            overall_quality = QualityLevel.DEGRADED
        elif weighted_score >= 0.2:
            overall_quality = QualityLevel.CRITICAL
        else:
            overall_quality = QualityLevel.FAILED

        # Detect veering
        veer_direction, drift = self._detect_veering(weighted_score)

        # Count sections present
        sections_present = sum(1 for sq in sections_quality.values() if sq.present)

        report = InjectionQualityReport(
            timestamp=datetime.now(),
            prompt_hash=prompt_hash,
            total_sections=len(self.SECTION_CONFIG),
            sections_present=sections_present,
            sections_quality=sections_quality,
            overall_quality=overall_quality,
            overall_score=weighted_score,
            veer_direction=veer_direction,
            drift_from_baseline=drift,
            issues_found=issues_found,
            recommendations=recommendations
        )

        # Store snapshot
        self._store_snapshot(report)

        # Generate veering alert if needed
        if veer_direction in [VeerDirection.VEERING, VeerDirection.DRIFTING]:
            self._create_veering_alert(report)

        return report

    def _assess_section(
        self,
        section_id: int,
        config: Dict,
        content: str
    ) -> SectionQuality:
        """Assess quality of a single section."""
        section_name = config['name']
        issues = []

        # Check if section is present
        markers = self.SECTION_MARKERS.get(section_id, [])
        present = any(marker.upper() in content.upper() for marker in markers)

        if not present:
            issues.append(f"Section {section_id} ({section_name}) not found")
            return SectionQuality(
                section_id=section_id,
                section_name=section_name,
                present=False,
                content_length=0,
                has_expected_markers=False,
                quality_score=0.0,
                issues=issues
            )

        # Extract section content (rough approximation)
        # In reality, we'd have better section parsing
        content_length = len(content) // 8  # Rough estimate

        # Check markers
        markers_found = sum(1 for m in markers if m.upper() in content.upper())
        has_expected_markers = markers_found >= len(markers) * 0.5

        if not has_expected_markers:
            issues.append(f"Section {section_id} missing expected markers")

        # Check minimum length
        min_length = self.baseline.min_lengths.get(section_id, 50)
        if content_length < min_length:
            issues.append(f"Section {section_id} too short ({content_length} < {min_length})")

        # Calculate quality score
        presence_score = 1.0 if present else 0.0
        marker_score = markers_found / max(len(markers), 1)
        length_score = min(content_length / min_length, 1.0) if min_length > 0 else 1.0

        quality_score = (
            presence_score * 0.4 +
            marker_score * 0.3 +
            length_score * 0.3
        )

        return SectionQuality(
            section_id=section_id,
            section_name=section_name,
            present=present,
            content_length=content_length,
            has_expected_markers=has_expected_markers,
            quality_score=quality_score,
            issues=issues
        )

    def _detect_veering(self, current_score: float) -> Tuple[VeerDirection, float]:
        """Detect if quality is veering from baseline."""
        # Get recent scores
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT overall_score FROM quality_snapshots
                ORDER BY timestamp DESC LIMIT 10
            """).fetchall()

        if not rows:
            return VeerDirection.STABLE, 0.0

        recent_scores = [r[0] for r in rows]
        avg_recent = sum(recent_scores) / len(recent_scores)

        # Calculate drift from 1.0 (ideal)
        drift = 1.0 - current_score

        # Detect trend
        if len(recent_scores) >= 3:
            trend = current_score - recent_scores[-1]  # vs oldest in window
            if trend > 0.1:
                return VeerDirection.IMPROVING, drift
            elif trend < -0.15:
                return VeerDirection.VEERING, drift
            elif trend < -0.05:
                return VeerDirection.DRIFTING, drift

        return VeerDirection.STABLE, drift

    def _store_snapshot(self, report: InjectionQualityReport):
        """Store quality snapshot in database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO quality_snapshots
                (timestamp, prompt_hash, overall_score, overall_quality,
                 veer_direction, drift_from_baseline, sections_present, total_sections, full_report)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                report.timestamp.isoformat(),
                report.prompt_hash,
                report.overall_score,
                report.overall_quality.value,
                report.veer_direction.value,
                report.drift_from_baseline,
                report.sections_present,
                report.total_sections,
                json.dumps(report.to_dict())
            ))
            snapshot_id = cursor.lastrowid

            # Store section-level data
            for section_id, sq in report.sections_quality.items():
                conn.execute("""
                    INSERT INTO section_quality
                    (snapshot_id, section_id, section_name, present,
                     content_length, quality_score, issues)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    snapshot_id,
                    section_id,
                    sq.section_name,
                    1 if sq.present else 0,
                    sq.content_length,
                    sq.quality_score,
                    json.dumps(sq.issues)
                ))

            conn.commit()

    def _create_veering_alert(self, report: InjectionQualityReport):
        """Create veering alert for Synaptic."""
        # Find most degraded section
        worst_section = min(
            report.sections_quality.values(),
            key=lambda sq: sq.quality_score
        )

        alert_message = (
            f"Webhook quality {report.veer_direction.value}: "
            f"Score {report.overall_score:.0%} "
            f"({worst_section.section_name} at {worst_section.quality_score:.0%})"
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO veering_alerts
                (timestamp, section_id, previous_score, current_score,
                 drift_amount, alert_message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                report.timestamp.isoformat(),
                worst_section.section_id,
                1.0,  # Baseline ideal
                report.overall_score,
                report.drift_from_baseline,
                alert_message
            ))
            conn.commit()

    def get_recent_alerts(self, limit: int = 5) -> List[Dict]:
        """Get recent veering alerts."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT timestamp, section_id, drift_amount, alert_message, acknowledged
                FROM veering_alerts
                WHERE acknowledged = 0
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,)).fetchall()

        return [
            {
                "timestamp": row[0],
                "section_id": row[1],
                "drift": row[2],
                "message": row[3],
                "acknowledged": bool(row[4])
            }
            for row in rows
        ]

    def get_quality_trend(self, hours: int = 24) -> Dict:
        """Get quality trend over time."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT timestamp, overall_score, overall_quality
                FROM quality_snapshots
                WHERE timestamp > ?
                ORDER BY timestamp
            """, (cutoff,)).fetchall()

        if not rows:
            return {"trend": "no_data", "samples": 0}

        scores = [r[1] for r in rows]
        avg_score = sum(scores) / len(scores)
        first_score = scores[0]
        last_score = scores[-1]

        if last_score > first_score + 0.1:
            trend = "improving"
        elif last_score < first_score - 0.1:
            trend = "declining"
        else:
            trend = "stable"

        return {
            "trend": trend,
            "samples": len(rows),
            "average_score": avg_score,
            "first_score": first_score,
            "last_score": last_score,
            "high": max(scores),
            "low": min(scores)
        }

    def format_status_for_injection(self) -> str:
        """
        Format current quality status for Section 6 injection.

        This is what Synaptic sees - concise, actionable.
        """
        trend = self.get_quality_trend(hours=6)
        alerts = self.get_recent_alerts(limit=3)

        if trend.get("samples", 0) == 0:
            return ""  # No data yet

        score = trend.get("last_score", 0)
        trend_dir = trend.get("trend", "stable")

        # Only inject if there's an issue
        if score >= 0.9 and trend_dir != "declining" and not alerts:
            return ""  # All clear, no injection needed

        lines = [
            "",
            "[START: Synaptic Webhook Quality]",
        ]

        # Quality indicator
        if score >= 0.9:
            lines.append(f"👁️ Atlas Vision: CRYSTAL CLEAR ({score:.0%})")
        elif score >= 0.7:
            lines.append(f"👁️ Atlas Vision: Good ({score:.0%})")
        elif score >= 0.5:
            lines.append(f"⚠️ Atlas Vision: FOGGY ({score:.0%})")
        else:
            lines.append(f"🚨 Atlas Vision: IMPAIRED ({score:.0%})")

        # Trend
        if trend_dir == "declining":
            lines.append(f"📉 Trend: Declining - investigate webhook pipeline")
        elif trend_dir == "improving":
            lines.append(f"📈 Trend: Improving")

        # Alerts (if any)
        if alerts:
            lines.append("🔔 Recent alerts:")
            for alert in alerts[:2]:
                lines.append(f"  • {alert['message']}")

        lines.append("[END: Synaptic Webhook Quality]")
        lines.append("")

        return "\n".join(lines)


# =============================================================================
# Module-level convenience functions
# =============================================================================

_monitor: Optional[WebhookQualityMonitor] = None


def get_monitor() -> WebhookQualityMonitor:
    """Get or create global monitor instance."""
    global _monitor
    if _monitor is None:
        _monitor = WebhookQualityMonitor()
    return _monitor


def assess_quality(injection_content: str, prompt: str = "") -> InjectionQualityReport:
    """Assess injection quality."""
    return get_monitor().assess_injection(injection_content, prompt)


def get_quality_status() -> str:
    """Get formatted quality status for injection."""
    return get_monitor().format_status_for_injection()


def get_alerts() -> List[Dict]:
    """Get recent quality alerts."""
    return get_monitor().get_recent_alerts()


def get_trend(hours: int = 24) -> Dict:
    """Get quality trend."""
    return get_monitor().get_quality_trend(hours)


# =============================================================================
# CLI Interface
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Webhook Quality Monitor")
    parser.add_argument("--status", action="store_true", help="Show current status")
    parser.add_argument("--trend", action="store_true", help="Show quality trend")
    parser.add_argument("--alerts", action="store_true", help="Show recent alerts")
    parser.add_argument("--assess", type=str, help="Assess injection from file")

    args = parser.parse_args()

    monitor = get_monitor()

    if args.status:
        status = monitor.format_status_for_injection()
        if status:
            print(status)
        else:
            print("All clear - webhook quality is optimal")

    elif args.trend:
        trend = monitor.get_quality_trend()
        print(json.dumps(trend, indent=2))

    elif args.alerts:
        alerts = monitor.get_recent_alerts()
        if alerts:
            for alert in alerts:
                print(f"[{alert['timestamp']}] {alert['message']}")
        else:
            print("No recent alerts")

    elif args.assess:
        with open(args.assess) as f:
            content = f.read()
        report = monitor.assess_injection(content)
        print(f"Quality: {report.overall_quality.value} ({report.overall_score:.0%})")
        print(f"Sections: {report.sections_present}/{report.total_sections}")
        print(f"Trend: {report.veer_direction.value}")
        if report.issues_found:
            print("Issues:")
            for issue in report.issues_found[:5]:
                print(f"  - {issue}")

    else:
        # Default: show summary
        trend = monitor.get_quality_trend(hours=6)
        alerts = monitor.get_recent_alerts(limit=3)

        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  SYNAPTIC WEBHOOK QUALITY MONITOR                            ║")
        print("╠══════════════════════════════════════════════════════════════╣")

        if trend.get("samples", 0) > 0:
            print(f"   Last Score: {trend.get('last_score', 0):.0%}")
            print(f"   Trend: {trend.get('trend', 'unknown')}")
            print(f"   Samples (6h): {trend.get('samples', 0)}")
        else:
            print("   No recent data - run webhook injection to populate")

        if alerts:
            print()
            print("   Recent Alerts:")
            for alert in alerts:
                print(f"   ⚠️ {alert['message']}")

        print("╚══════════════════════════════════════════════════════════════╝")
