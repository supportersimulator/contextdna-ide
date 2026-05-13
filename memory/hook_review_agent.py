#!/usr/bin/env python3
"""
HOOK REVIEW AGENT
=================

Agent-driven meta-learning layer for hook evolution.

Performs periodic holistic analysis of hook effectiveness and proposes
improvements that raw statistical A/B testing cannot reach on its own.

PHILOSOPHY:
Raw A/B testing can tell us WHICH variant performs better.
Agent analysis can tell us WHY and propose WHAT to try next.

CAPABILITIES:
1. Holistic effectiveness analysis across all hooks
2. Qualitative review of hook script contents
3. User workflow pattern consideration
4. Improvement hypothesis generation
5. New variant proposal and registration
6. Structured reporting for user review

SCHEDULED CHECK-INS:
- Recommended interval: Every 5 hours of active use
- Trigger: Can be manual, cron, or hook-based
- Output: Analysis report + optional new A/B test

Usage:
    from memory.hook_review_agent import HookReviewAgent

    agent = HookReviewAgent()
    report = agent.perform_review()
    print(report.summary)

    # Or run as script
    python memory/hook_review_agent.py review
    python memory/hook_review_agent.py status
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field, asdict

# Add repo root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.hook_evolution import (
    HookEvolutionEngine,
    get_hook_evolution_engine,
    HookVariant,
    VariantStats,
)

# Review tracking database (same as hook evolution for consolidation)
REVIEW_DB = Path(__file__).parent / ".pattern_evolution.db"
REPO_ROOT = Path(__file__).parent.parent


@dataclass
class HookAnalysis:
    """Analysis of a single hook type."""
    hook_type: str
    current_variants: List[Dict]
    active_test: Optional[Dict]
    stats_summary: Dict
    effectiveness_assessment: str  # "excellent", "good", "needs_improvement", "poor"
    issues_identified: List[str]
    improvement_opportunities: List[str]


@dataclass
class ImprovementProposal:
    """A proposed improvement to test."""
    hook_type: str
    proposal_name: str
    hypothesis: str
    proposed_changes: List[str]
    expected_impact: str
    magnitude: str  # "minor" (A) or "major" (B)
    implementation_notes: str
    confidence: float  # 0.0-1.0


@dataclass
class ReviewReport:
    """Complete hook review report."""
    review_id: str
    timestamp: str
    hours_since_last_review: float
    overall_health: str  # "healthy", "attention_needed", "critical"

    # Analysis per hook type
    hook_analyses: List[HookAnalysis]

    # Cross-cutting insights
    patterns_observed: List[str]
    systemic_issues: List[str]

    # Proposals
    improvement_proposals: List[ImprovementProposal]
    auto_created_tests: List[Dict]  # Tests created by this review

    # Summary
    summary: str
    recommendations: List[str]
    next_review_suggested: str


class HookReviewAgent:
    """
    Agent that performs holistic hook effectiveness analysis.

    This layer sits ABOVE the raw A/B testing system, adding:
    - Qualitative analysis of WHY things work or don't
    - Cross-hook pattern recognition
    - User workflow consideration
    - Improvement hypothesis generation
    """

    def __init__(self, auto_create_tests: bool = False):
        """
        Initialize the review agent.

        Args:
            auto_create_tests: If True, automatically create proposed A/B tests.
                              If False, just generate proposals for user review.
        """
        self.engine = get_hook_evolution_engine()
        self.auto_create_tests = auto_create_tests
        self._init_review_tracking()

    def _init_review_tracking(self):
        """Initialize review tracking table."""
        self.engine.db.execute("""
            CREATE TABLE IF NOT EXISTS hook_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id TEXT UNIQUE NOT NULL,
                timestamp TEXT NOT NULL,
                hours_since_last REAL,
                overall_health TEXT,
                report_json TEXT,
                proposals_count INTEGER,
                tests_created INTEGER,
                created_by TEXT DEFAULT 'agent'
            )
        """)
        self.engine.db.commit()

    def get_last_review(self) -> Optional[Dict]:
        """Get the most recent review."""
        row = self.engine.db.execute("""
            SELECT review_id, timestamp, overall_health, proposals_count
            FROM hook_reviews
            ORDER BY timestamp DESC
            LIMIT 1
        """).fetchone()

        if row:
            return {
                "review_id": row[0],
                "timestamp": row[1],
                "overall_health": row[2],
                "proposals_count": row[3]
            }
        return None

    def hours_since_last_review(self) -> float:
        """Calculate hours since last review."""
        last = self.get_last_review()
        if not last:
            return float('inf')  # Never reviewed

        last_time = datetime.fromisoformat(last["timestamp"])
        now = datetime.now()
        delta = now - last_time
        return delta.total_seconds() / 3600

    def should_review(self, min_hours: float = 5.0) -> Tuple[bool, str]:
        """
        Determine if a review should be performed.

        Args:
            min_hours: Minimum hours between reviews

        Returns:
            Tuple of (should_review, reason)
        """
        hours = self.hours_since_last_review()

        if hours == float('inf'):
            return True, "No previous review found - initial review recommended"

        if hours >= min_hours:
            return True, f"Last review was {hours:.1f} hours ago (threshold: {min_hours}h)"

        return False, f"Last review was {hours:.1f} hours ago (next review in {min_hours - hours:.1f}h)"

    def perform_review(self, force: bool = False) -> ReviewReport:
        """
        Perform a comprehensive hook review.

        Args:
            force: If True, perform review even if minimum time hasn't passed

        Returns:
            ReviewReport with complete analysis
        """
        should, reason = self.should_review()
        if not should and not force:
            # Return minimal report explaining why
            return self._create_skip_report(reason)

        review_id = f"review_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        hours = self.hours_since_last_review()

        # Analyze each hook type
        hook_analyses = []
        for hook_type in ["UserPromptSubmit", "PostToolUse", "SessionEnd", "GitPostCommit"]:
            analysis = self._analyze_hook_type(hook_type)
            hook_analyses.append(analysis)

        # Cross-cutting pattern analysis
        patterns = self._identify_cross_patterns(hook_analyses)
        systemic_issues = self._identify_systemic_issues(hook_analyses)

        # Generate improvement proposals
        proposals = self._generate_proposals(hook_analyses, patterns)

        # Auto-create tests if enabled
        created_tests = []
        if self.auto_create_tests and proposals:
            created_tests = self._create_proposed_tests(proposals)

        # Generate summary and recommendations
        overall_health = self._assess_overall_health(hook_analyses)
        summary = self._generate_summary(hook_analyses, patterns, proposals)
        recommendations = self._generate_recommendations(hook_analyses, proposals)

        # Build report
        report = ReviewReport(
            review_id=review_id,
            timestamp=datetime.now().isoformat(),
            hours_since_last_review=hours if hours != float('inf') else -1,
            overall_health=overall_health,
            hook_analyses=hook_analyses,
            patterns_observed=patterns,
            systemic_issues=systemic_issues,
            improvement_proposals=proposals,
            auto_created_tests=created_tests,
            summary=summary,
            recommendations=recommendations,
            next_review_suggested=self._suggest_next_review(overall_health)
        )

        # Store review
        self._store_review(report)

        return report

    def _analyze_hook_type(self, hook_type: str) -> HookAnalysis:
        """Analyze a single hook type comprehensively."""
        # Get variants for this hook type
        variants = self.engine.db.execute("""
            SELECT variant_id, variant_name, description, change_magnitude,
                   is_default, is_active, config_json
            FROM hook_variants
            WHERE hook_type = ?
            ORDER BY created_at
        """, (hook_type,)).fetchall()

        variant_data = []
        total_positive = 0
        total_negative = 0
        total_outcomes = 0

        for v in variants:
            stats = self.engine.get_variant_stats(v[0])
            total_positive += stats.positive_count
            total_negative += stats.negative_count
            total_outcomes += stats.total_outcomes

            variant_data.append({
                "variant_id": v[0],
                "name": v[1],
                "description": v[2],
                "magnitude": v[3],
                "is_default": bool(v[4]),
                "is_active": bool(v[5]),
                "config": json.loads(v[6]) if v[6] else {},
                "stats": {
                    "total": stats.total_outcomes,
                    "positive_rate": stats.positive_rate,
                    "negative_rate": stats.negative_rate,
                    "avg_retries": stats.avg_retry_count,
                    "experience": stats.experience_level
                }
            })

        # Get active A/B test
        active_test = self.engine.db.execute("""
            SELECT test_id, test_name, status, started_at
            FROM hook_ab_tests
            WHERE hook_type = ? AND status = 'running'
            ORDER BY started_at DESC
            LIMIT 1
        """, (hook_type,)).fetchone()

        test_data = None
        if active_test:
            test_data = {
                "test_id": active_test[0],
                "name": active_test[1],
                "status": active_test[2],
                "started": active_test[3]
            }

        # Assess effectiveness
        if total_outcomes == 0:
            effectiveness = "no_data"
            issues = ["No outcome data collected yet"]
            opportunities = ["Start collecting outcome data"]
        else:
            pos_rate = total_positive / total_outcomes
            neg_rate = total_negative / total_outcomes

            if pos_rate >= 0.7 and neg_rate < 0.1:
                effectiveness = "excellent"
                issues = []
                opportunities = ["Consider minor optimizations"]
            elif pos_rate >= 0.5 and neg_rate < 0.2:
                effectiveness = "good"
                issues = ["Room for improvement"]
                opportunities = self._identify_opportunities(hook_type, variant_data)
            elif neg_rate >= 0.3:
                effectiveness = "poor"
                issues = [f"High negative rate ({neg_rate:.0%})", "Hooks may be hurting more than helping"]
                opportunities = ["Major redesign needed", "Consider disabling underperforming variants"]
            else:
                effectiveness = "needs_improvement"
                issues = ["Inconsistent outcomes"]
                opportunities = self._identify_opportunities(hook_type, variant_data)

        return HookAnalysis(
            hook_type=hook_type,
            current_variants=variant_data,
            active_test=test_data,
            stats_summary={
                "total_outcomes": total_outcomes,
                "positive_rate": total_positive / total_outcomes if total_outcomes > 0 else 0,
                "negative_rate": total_negative / total_outcomes if total_outcomes > 0 else 0,
                "variant_count": len(variants)
            },
            effectiveness_assessment=effectiveness,
            issues_identified=issues,
            improvement_opportunities=opportunities
        )

    def _identify_opportunities(self, hook_type: str, variants: List[Dict]) -> List[str]:
        """Identify specific improvement opportunities for a hook type."""
        opportunities = []

        # Analyze based on hook type specifics
        if hook_type == "UserPromptSubmit":
            # Check if we have concise variant
            has_concise = any("concise" in v["name"].lower() for v in variants)
            if not has_concise:
                opportunities.append("Test concise output format (reduce cognitive load)")

            # Check for keyword extraction
            for v in variants:
                config = v.get("config", {})
                script = config.get("script", "")
                if "auto-memory-query.sh" in script and "concise" not in script:
                    opportunities.append("Add keyword extraction (current uses raw prompts)")
                    opportunities.append("Add relevance threshold filtering")
                    break

            opportunities.append("Test different risk threshold categorizations")
            opportunities.append("Experiment with professor guidance formatting")

        elif hook_type == "PostToolUse":
            opportunities.append("Test win capture prompt variations")
            opportunities.append("Experiment with success detection triggers")

        elif hook_type == "SessionEnd":
            opportunities.append("Test summary verbosity levels")
            opportunities.append("Experiment with pattern extraction prompts")

        return opportunities[:5]  # Limit to top 5

    def _identify_cross_patterns(self, analyses: List[HookAnalysis]) -> List[str]:
        """Identify patterns across all hook types."""
        patterns = []

        # Check if all hooks have consistent data quality
        no_data = [a.hook_type for a in analyses if a.effectiveness_assessment == "no_data"]
        if no_data:
            patterns.append(f"Data collection missing for: {', '.join(no_data)}")

        # Check for consistent issues
        all_issues = []
        for a in analyses:
            all_issues.extend(a.issues_identified)

        if all_issues.count("Room for improvement") >= 2:
            patterns.append("Multiple hooks have optimization opportunities")

        # Check if any hooks are excellent
        excellent = [a.hook_type for a in analyses if a.effectiveness_assessment == "excellent"]
        if excellent:
            patterns.append(f"High performers: {', '.join(excellent)} - study their patterns")

        return patterns

    def _identify_systemic_issues(self, analyses: List[HookAnalysis]) -> List[str]:
        """Identify systemic issues across the hook system."""
        issues = []

        poor_hooks = [a for a in analyses if a.effectiveness_assessment == "poor"]
        if len(poor_hooks) >= 2:
            issues.append("Multiple hooks underperforming - may indicate systemic problem")

        # Check for stale tests
        for a in analyses:
            if a.active_test:
                started = datetime.fromisoformat(a.active_test["started"])
                age_days = (datetime.now() - started).days
                if age_days > 14:
                    issues.append(f"{a.hook_type} A/B test running for {age_days} days - consider concluding")

        return issues

    def _generate_proposals(
        self,
        analyses: List[HookAnalysis],
        patterns: List[str]
    ) -> List[ImprovementProposal]:
        """Generate improvement proposals based on analysis."""
        proposals = []

        for analysis in analyses:
            if analysis.effectiveness_assessment in ["needs_improvement", "poor"]:
                # Generate proposals for underperforming hooks
                for opp in analysis.improvement_opportunities[:2]:  # Top 2 per hook
                    proposal = self._opportunity_to_proposal(analysis.hook_type, opp)
                    if proposal:
                        proposals.append(proposal)

        return proposals[:5]  # Limit total proposals

    def _opportunity_to_proposal(self, hook_type: str, opportunity: str) -> Optional[ImprovementProposal]:
        """Convert an opportunity description to a concrete proposal."""
        # Map common opportunities to concrete proposals
        if "concise" in opportunity.lower():
            return ImprovementProposal(
                hook_type=hook_type,
                proposal_name="Concise Output Format",
                hypothesis="Reducing output verbosity will lower cognitive load and improve task success",
                proposed_changes=[
                    "Remove ASCII box formatting",
                    "Use bullet points instead of sections",
                    "Limit output to essential information only"
                ],
                expected_impact="10-20% improvement in task success rate",
                magnitude="major",  # B variant - significant change
                implementation_notes="Create new hook script with minimal formatting",
                confidence=0.7
            )

        elif "keyword" in opportunity.lower():
            return ImprovementProposal(
                hook_type=hook_type,
                proposal_name="Keyword-Based Queries",
                hypothesis="Extracting keywords will improve memory search relevance",
                proposed_changes=[
                    "Add stopword filtering",
                    "Extract meaningful terms only",
                    "Limit to top 8 keywords"
                ],
                expected_impact="Better memory matches, higher relevance scores",
                magnitude="minor",  # A variant - focused change
                implementation_notes="Add keyword extraction before query formation",
                confidence=0.8
            )

        elif "relevance" in opportunity.lower():
            return ImprovementProposal(
                hook_type=hook_type,
                proposal_name="Relevance Threshold Filtering",
                hypothesis="Showing only high-relevance results reduces noise",
                proposed_changes=[
                    "Filter results below 40% relevance",
                    "Don't show section if no results pass threshold"
                ],
                expected_impact="Less noise, more focused context",
                magnitude="minor",
                implementation_notes="Add relevance check in output formatting",
                confidence=0.75
            )

        return None

    def _create_proposed_tests(self, proposals: List[ImprovementProposal]) -> List[Dict]:
        """Create A/B tests for proposals (if auto_create_tests is enabled)."""
        created = []

        for proposal in proposals:
            # Check if similar test already running
            existing = self.engine.db.execute("""
                SELECT test_id FROM hook_ab_tests
                WHERE hook_type = ? AND status = 'running'
            """, (proposal.hook_type,)).fetchone()

            if existing:
                continue  # Don't create competing tests

            # Create variant
            variant_id = self.engine.create_variant(
                hook_type=proposal.hook_type,
                name=proposal.proposal_name,
                config={"proposed_changes": proposal.proposed_changes},
                description=proposal.hypothesis,
                magnitude=proposal.magnitude
            )

            # Get control variant
            control = self.engine.db.execute("""
                SELECT variant_id FROM hook_variants
                WHERE hook_type = ? AND is_default = 1
            """, (proposal.hook_type,)).fetchone()

            if control:
                # Create test
                if proposal.magnitude == "major":
                    test_id = self.engine.create_ab_test(
                        test_name=f"Agent Proposed: {proposal.proposal_name}",
                        hook_type=proposal.hook_type,
                        control_variant_id=control[0],
                        variant_b_id=variant_id
                    )
                else:
                    test_id = self.engine.create_ab_test(
                        test_name=f"Agent Proposed: {proposal.proposal_name}",
                        hook_type=proposal.hook_type,
                        control_variant_id=control[0],
                        variant_a_id=variant_id
                    )

                self.engine.start_ab_test(test_id)

                created.append({
                    "test_id": test_id,
                    "proposal": proposal.proposal_name,
                    "variant_id": variant_id,
                    "hypothesis": proposal.hypothesis
                })

        return created

    def _assess_overall_health(self, analyses: List[HookAnalysis]) -> str:
        """Assess overall hook system health."""
        effectiveness_scores = {
            "excellent": 4,
            "good": 3,
            "needs_improvement": 2,
            "poor": 1,
            "no_data": 2  # Neutral
        }

        scores = [effectiveness_scores.get(a.effectiveness_assessment, 2) for a in analyses]
        avg_score = sum(scores) / len(scores) if scores else 2

        if avg_score >= 3.5:
            return "healthy"
        elif avg_score >= 2.5:
            return "attention_needed"
        else:
            return "critical"

    def _generate_summary(
        self,
        analyses: List[HookAnalysis],
        patterns: List[str],
        proposals: List[ImprovementProposal]
    ) -> str:
        """Generate human-readable summary."""
        lines = []

        # Overall status
        excellent = sum(1 for a in analyses if a.effectiveness_assessment == "excellent")
        good = sum(1 for a in analyses if a.effectiveness_assessment == "good")
        needs_work = sum(1 for a in analyses if a.effectiveness_assessment in ["needs_improvement", "poor"])

        lines.append(f"Hook System Status: {excellent} excellent, {good} good, {needs_work} need attention")

        # Key findings
        if patterns:
            lines.append(f"Key Patterns: {patterns[0]}")

        # Proposals
        if proposals:
            lines.append(f"Improvement Proposals: {len(proposals)} generated")
            lines.append(f"  Top proposal: {proposals[0].proposal_name}")

        return "\n".join(lines)

    def _generate_recommendations(
        self,
        analyses: List[HookAnalysis],
        proposals: List[ImprovementProposal]
    ) -> List[str]:
        """Generate actionable recommendations."""
        recommendations = []

        # Prioritize poor performers
        poor = [a for a in analyses if a.effectiveness_assessment == "poor"]
        if poor:
            recommendations.append(f"PRIORITY: Address {poor[0].hook_type} - currently hurting more than helping")

        # Suggest running proposals
        if proposals and not self.auto_create_tests:
            recommendations.append(f"Consider running A/B test for: {proposals[0].proposal_name}")

        # Data collection
        no_data = [a for a in analyses if a.effectiveness_assessment == "no_data"]
        if no_data:
            recommendations.append(f"Enable outcome tracking for: {', '.join(a.hook_type for a in no_data)}")

        return recommendations

    def _suggest_next_review(self, overall_health: str) -> str:
        """Suggest when to run next review."""
        if overall_health == "critical":
            return "Review again in 2 hours after implementing fixes"
        elif overall_health == "attention_needed":
            return "Review again in 4 hours"
        else:
            return "Review again in 6-8 hours"

    def _create_skip_report(self, reason: str) -> ReviewReport:
        """Create a minimal report when review is skipped."""
        return ReviewReport(
            review_id="skipped",
            timestamp=datetime.now().isoformat(),
            hours_since_last_review=self.hours_since_last_review(),
            overall_health="not_reviewed",
            hook_analyses=[],
            patterns_observed=[],
            systemic_issues=[],
            improvement_proposals=[],
            auto_created_tests=[],
            summary=f"Review skipped: {reason}",
            recommendations=["Wait for minimum interval before next review"],
            next_review_suggested=reason
        )

    def _store_review(self, report: ReviewReport):
        """Store review in database."""
        self.engine.db.execute("""
            INSERT INTO hook_reviews (
                review_id, timestamp, hours_since_last, overall_health,
                report_json, proposals_count, tests_created
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            report.review_id,
            report.timestamp,
            report.hours_since_last_review,
            report.overall_health,
            json.dumps(asdict(report)),
            len(report.improvement_proposals),
            len(report.auto_created_tests)
        ))
        self.engine.db.commit()

    def format_report(self, report: ReviewReport) -> str:
        """Format report for display."""
        lines = []
        lines.append("=" * 70)
        lines.append("HOOK REVIEW AGENT REPORT")
        lines.append("=" * 70)
        lines.append(f"Review ID: {report.review_id}")
        lines.append(f"Timestamp: {report.timestamp}")
        if report.hours_since_last_review > 0:
            lines.append(f"Hours since last review: {report.hours_since_last_review:.1f}")
        lines.append(f"Overall Health: {report.overall_health.upper()}")
        lines.append("")

        # Hook analyses
        lines.append("-" * 70)
        lines.append("HOOK TYPE ANALYSIS")
        lines.append("-" * 70)
        for analysis in report.hook_analyses:
            status_emoji = {
                "excellent": "🟢",
                "good": "🟡",
                "needs_improvement": "🟠",
                "poor": "🔴",
                "no_data": "⚪"
            }.get(analysis.effectiveness_assessment, "⚪")

            lines.append(f"\n{status_emoji} {analysis.hook_type}: {analysis.effectiveness_assessment}")
            lines.append(f"   Variants: {len(analysis.current_variants)}")
            lines.append(f"   Outcomes: {analysis.stats_summary.get('total_outcomes', 0)}")
            if analysis.stats_summary.get('total_outcomes', 0) > 0:
                lines.append(f"   Positive Rate: {analysis.stats_summary.get('positive_rate', 0):.0%}")
            if analysis.active_test:
                lines.append(f"   Active Test: {analysis.active_test['name']}")
            if analysis.issues_identified:
                lines.append(f"   Issues: {', '.join(analysis.issues_identified[:2])}")

        # Patterns
        if report.patterns_observed:
            lines.append("")
            lines.append("-" * 70)
            lines.append("PATTERNS OBSERVED")
            lines.append("-" * 70)
            for pattern in report.patterns_observed:
                lines.append(f"  • {pattern}")

        # Proposals
        if report.improvement_proposals:
            lines.append("")
            lines.append("-" * 70)
            lines.append("IMPROVEMENT PROPOSALS")
            lines.append("-" * 70)
            for i, proposal in enumerate(report.improvement_proposals, 1):
                lines.append(f"\n{i}. {proposal.proposal_name} ({proposal.magnitude} change)")
                lines.append(f"   Hook: {proposal.hook_type}")
                lines.append(f"   Hypothesis: {proposal.hypothesis}")
                lines.append(f"   Confidence: {proposal.confidence:.0%}")
                lines.append(f"   Changes:")
                for change in proposal.proposed_changes[:3]:
                    lines.append(f"     - {change}")

        # Auto-created tests
        if report.auto_created_tests:
            lines.append("")
            lines.append("-" * 70)
            lines.append("TESTS AUTO-CREATED")
            lines.append("-" * 70)
            for test in report.auto_created_tests:
                lines.append(f"  ✓ {test['proposal']}")
                lines.append(f"    Test ID: {test['test_id']}")

        # Recommendations
        lines.append("")
        lines.append("-" * 70)
        lines.append("RECOMMENDATIONS")
        lines.append("-" * 70)
        for rec in report.recommendations:
            lines.append(f"  → {rec}")

        # Summary
        lines.append("")
        lines.append("-" * 70)
        lines.append("SUMMARY")
        lines.append("-" * 70)
        lines.append(report.summary)
        lines.append("")
        lines.append(f"Next review: {report.next_review_suggested}")
        lines.append("=" * 70)

        return "\n".join(lines)


def run_scheduled_review(min_hours: float = 5.0, auto_create: bool = False) -> Optional[str]:
    """
    Run a scheduled hook review if enough time has passed.

    This function is designed to be called from:
    - A cron job
    - A Claude hook (e.g., SessionEnd)
    - Manual trigger

    Args:
        min_hours: Minimum hours between reviews
        auto_create: Whether to auto-create proposed A/B tests

    Returns:
        Formatted report string, or None if review was skipped
    """
    agent = HookReviewAgent(auto_create_tests=auto_create)

    should, reason = agent.should_review(min_hours)
    if not should:
        return None

    report = agent.perform_review(force=False)

    if report.review_id == "skipped":
        return None

    return agent.format_report(report)


def main():
    """CLI entry point."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python hook_review_agent.py [review|status|force]")
        print("")
        print("Commands:")
        print("  review  - Run review if enough time has passed (5h default)")
        print("  status  - Show current hook system status")
        print("  force   - Force a review regardless of timing")
        print("  auto    - Run review and auto-create proposed tests")
        sys.exit(1)

    command = sys.argv[1]

    if command == "status":
        agent = HookReviewAgent()
        last = agent.get_last_review()
        hours = agent.hours_since_last_review()

        print("Hook Review Agent Status")
        print("=" * 40)
        if last:
            print(f"Last review: {last['timestamp']}")
            print(f"Health: {last['overall_health']}")
            print(f"Proposals: {last['proposals_count']}")
            print(f"Hours ago: {hours:.1f}")
        else:
            print("No previous reviews found")

        should, reason = agent.should_review()
        print(f"\nShould review: {'Yes' if should else 'No'}")
        print(f"Reason: {reason}")

    elif command == "review":
        result = run_scheduled_review(min_hours=5.0, auto_create=False)
        if result:
            print(result)
        else:
            print("Review skipped (not enough time since last review)")
            print("Use 'force' command to override")

    elif command == "force":
        agent = HookReviewAgent(auto_create_tests=False)
        report = agent.perform_review(force=True)
        print(agent.format_report(report))

    elif command == "auto":
        result = run_scheduled_review(min_hours=5.0, auto_create=True)
        if result:
            print(result)
        else:
            print("Review skipped - use 'force' to override")

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
