#!/usr/bin/env python3
"""
OBJECTIVE SUCCESS PATTERN MANAGER

This module provides a management interface for objective success patterns -
both the static patterns (built-in) and learned patterns (discovered from use).

PHILOSOPHY:
Context DNA learns from EVERYONE's coding experience. As developers use the
system across different projects, languages, and frameworks, it discovers
new objective success patterns and shares them back to improve the product.

The system also PRUNES itself - identifying patterns that cause false positives
or aren't helpful. Patterns go through a lifecycle:
  ACTIVE → UNDER REVIEW → EXCLUDED (or back to ACTIVE)

PATTERN TYPES:
1. STATIC PATTERNS - Built into objective_success.py (curated, versioned)
2. LEARNED PATTERNS - Discovered from your work (stored in pattern_registry.db)
3. COMMUNITY PATTERNS - Aggregated from all users (opt-in, anonymized)
4. EXCLUDED PATTERNS - Previously active patterns now excluded (reversible)

PATTERN HEALTH ANALYSIS:
The system automatically analyzes patterns for potential issues:
- Too broad (matches too much, high false positive risk)
- Too specific (rarely matches, limited value)
- Ambiguous (matches both success and failure contexts)
- Conflicting (overlaps with other patterns)

COMMANDS:
    python pattern_manager.py list              - List all patterns
    python pattern_manager.py discover          - Discover new patterns from work log
    python pattern_manager.py add <pattern>     - Add a new learned pattern
    python pattern_manager.py test <text>       - Test text against patterns
    python pattern_manager.py export            - Export patterns for sharing
    python pattern_manager.py stats             - Show pattern statistics
    python pattern_manager.py analyze           - Analyze pattern health/risks
    python pattern_manager.py exclude <pattern> - Exclude a risky pattern
    python pattern_manager.py menu              - Interactive menu

Usage:
    from memory.pattern_manager import PatternManager

    manager = PatternManager()
    manager.discover_patterns()  # Find new patterns from work
    manager.add_pattern(regex, confidence, category)  # Add learned pattern
    manager.test_text("Container started successfully")  # Test detection
    manager.analyze_risks()  # Find potentially problematic patterns
    manager.exclude_pattern(regex, reason)  # Exclude a pattern
"""

import json
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum

# Ensure imports work from both context_dna package and memory/ locations
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import config for centralized paths
try:
    from context_dna.config import get_db_path, get_user_data_dir, ensure_user_data_dir
    # Ensure user data directory exists before getting paths
    ensure_user_data_dir()
    CONFIG_AVAILABLE = True
except ImportError:
    CONFIG_AVAILABLE = False
    get_db_path = lambda: Path(__file__).parent / ".pattern_evolution.db"
    get_user_data_dir = lambda: Path(__file__).parent

# Import existing components - try context_dna first, then memory/
try:
    from context_dna.objective_success import (
        SYSTEM_SUCCESS_PATTERNS,
        USER_CONFIRMATION_PATTERNS,
        discover_new_patterns,
        ObjectiveSuccessDetector,
    )
    OBJECTIVE_SUCCESS_AVAILABLE = True
except ImportError:
    try:
        from memory.objective_success import (
            SYSTEM_SUCCESS_PATTERNS,
            USER_CONFIRMATION_PATTERNS,
            discover_new_patterns,
            ObjectiveSuccessDetector,
        )
        OBJECTIVE_SUCCESS_AVAILABLE = True
    except ImportError:
        OBJECTIVE_SUCCESS_AVAILABLE = False
        SYSTEM_SUCCESS_PATTERNS = []
        USER_CONFIRMATION_PATTERNS = []

try:
    from context_dna.pattern_registry import EvolvingPatternRegistry
    PATTERN_REGISTRY_AVAILABLE = True
except ImportError:
    try:
        from memory.pattern_registry import EvolvingPatternRegistry
        PATTERN_REGISTRY_AVAILABLE = True
    except ImportError:
        PATTERN_REGISTRY_AVAILABLE = False

try:
    from context_dna.architecture_enhancer import work_log
    WORK_LOG_AVAILABLE = True
except ImportError:
    try:
        from memory.architecture_enhancer import work_log
        WORK_LOG_AVAILABLE = True
    except ImportError:
        WORK_LOG_AVAILABLE = False
        work_log = None

try:
    from context_dna.pattern_evolution import get_evolution_engine, PatternEvolutionEngine
    EVOLUTION_AVAILABLE = True
except ImportError:
    try:
        from memory.pattern_evolution import get_evolution_engine, PatternEvolutionEngine
        EVOLUTION_AVAILABLE = True
    except ImportError:
        EVOLUTION_AVAILABLE = False
        get_evolution_engine = None


class RiskLevel(Enum):
    """Risk level for pattern analysis."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class PatternRisk:
    """Risk assessment for a pattern."""
    regex: str
    risk_level: RiskLevel
    risk_score: float  # 0.0 to 1.0
    issues: List[str]
    recommendation: str
    false_positive_indicators: List[str]
    # Experience-based fields
    experience_modifier: float = 0.0  # -0.3 to +0.5
    experience_explanation: str = ""
    outcome_stats: Optional[Dict] = None
    heuristic_score: float = 0.0  # Base score before experience adjustment


@dataclass
class PatternInfo:
    """Information about a pattern."""
    regex: str
    confidence: float
    category: str
    source: str  # 'static', 'learned', 'community'
    hits: int = 0
    last_matched: Optional[str] = None
    added_at: Optional[str] = None
    excluded: bool = False
    exclusion_reason: Optional[str] = None


class PatternManager:
    """
    Manages objective success patterns - discovery, learning, testing, and pruning.

    This is the central interface for working with success patterns,
    combining static patterns from objective_success.py with learned
    patterns from the pattern registry.

    The system also analyzes pattern health and allows excluding risky patterns
    that cause false positives or aren't helpful to the ecosystem.
    """

    # Known false positive indicators - phrases that often appear WITH success
    # patterns but actually indicate FAILURE
    FALSE_POSITIVE_INDICATORS = [
        "failed", "error", "exception", "traceback", "not found",
        "unable to", "cannot", "couldn't", "can't", "denied",
        "refused", "timeout", "timed out", "rejected", "invalid",
        "missing", "no such", "doesn't exist", "does not exist",
        "403", "404", "500", "502", "503", "504",
        "ENOENT", "EACCES", "EPERM", "ECONNREFUSED",
        "null", "undefined", "NaN", "nil",
        "panic", "fatal", "critical", "abort",
        "rollback", "reverted", "cancelled", "canceled",
    ]

    # Patterns that are too broad and likely to cause false positives
    OVERLY_BROAD_PATTERNS = [
        r"^ok$",  # Too short, matches many contexts
        r"^\d+$",  # Just numbers, too generic
        r"^done$",  # Too short
        r"^yes$",  # Too generic
        r"^true$",  # Too generic
        r"^success$",  # Without context, ambiguous
    ]

    def __init__(self):
        # Use evolution engine for exclusion management (consolidated DB)
        self.evolution_engine = get_evolution_engine() if EVOLUTION_AVAILABLE else None
        self.excluded_patterns = self._load_exclusions()
        self.static_patterns = self._load_static_patterns()
        self.learned_patterns = self._load_learned_patterns()

    def _load_exclusions(self) -> Dict[str, str]:
        """Load excluded patterns from evolution database."""
        exclusions = {}
        if self.evolution_engine:
            for exc in self.evolution_engine.get_exclusions():
                exclusions[exc["regex"]] = exc["reason"]
        return exclusions

    def _load_static_patterns(self) -> List[PatternInfo]:
        """Load static patterns from objective_success.py."""
        patterns = []
        for regex, confidence, category in SYSTEM_SUCCESS_PATTERNS:
            patterns.append(PatternInfo(
                regex=regex,
                confidence=confidence,
                category=category,
                source="static"
            ))
        return patterns

    def _load_learned_patterns(self) -> List[PatternInfo]:
        """Load learned patterns from pattern registry."""
        patterns = []
        if PATTERN_REGISTRY_AVAILABLE:
            try:
                registry = EvolvingPatternRegistry()
                learned = registry.get_learned_patterns()
                for regex, confidence, category in learned:
                    patterns.append(PatternInfo(
                        regex=regex,
                        confidence=confidence,
                        category=category,
                        source="learned"
                    ))
            except Exception:
                pass
        return patterns

    def get_all_patterns(self) -> List[PatternInfo]:
        """Get all patterns (static + learned)."""
        return self.static_patterns + self.learned_patterns

    def get_patterns_by_category(self) -> Dict[str, List[PatternInfo]]:
        """Group patterns by category."""
        categories = {}
        for pattern in self.get_all_patterns():
            cat = self._categorize(pattern.category)
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(pattern)
        return categories

    def _categorize(self, category: str) -> str:
        """Map category to higher-level grouping."""
        category_map = {
            # HTTP
            "http": "HTTP/API",
            "json": "HTTP/API",
            "response": "HTTP/API",

            # Exit codes
            "exit": "Process",
            "return": "Process",
            "status": "Process",
            "bash": "Process",

            # Docker/Container
            "container": "Docker",
            "docker": "Docker",
            "service": "Docker",
            "image": "Docker",
            "pod": "Docker",

            # Git
            "git": "Git",

            # Build/Test
            "build": "Build",
            "compile": "Build",
            "webpack": "Build",
            "vite": "Build",
            "esbuild": "Build",
            "typescript": "Build",
            "test": "Test",
            "pytest": "Test",
            "mocha": "Test",
            "coverage": "Test",

            # Deployment
            "deploy": "Deployment",
            "rollout": "Deployment",
            "release": "Deployment",
            "vercel": "Deployment",
            "netlify": "Deployment",
            "aws": "Cloud",

            # Database
            "migration": "Database",
            "database": "Database",
            "rows": "Database",
            "insert": "Database",
            "mysql": "Database",
            "table": "Database",

            # Package managers
            "npm": "Packages",
            "pip": "Packages",
            "cargo": "Packages",
            "yarn": "Packages",
            "pnpm": "Packages",
            "packages": "Packages",
            "deps": "Packages",

            # Cloud/Infra
            "terraform": "Infrastructure",
            "gcloud": "Cloud",
            "azure": "Cloud",
            "k8s": "Infrastructure",
            "lb": "Infrastructure",
            "resources": "Infrastructure",

            # SSL/Security
            "cert": "Security",
            "ssl": "Security",
            "https": "Security",
            "certbot": "Security",
            "key": "Security",

            # Memory system
            "sop": "Context DNA",
            "gotcha": "Context DNA",
            "pattern": "Context DNA",
            "agent": "Context DNA",
            "brain": "Context DNA",
            "automation": "Context DNA",
            "wins": "Context DNA",
            "win": "Context DNA",

            # File system
            "file": "File System",
            "directory": "File System",
            "chmod": "File System",
            "chown": "File System",

            # Success keywords
            "success": "Generic",
            "successfully": "Generic",
            "completed": "Generic",
            "update": "Generic",
            "done": "Generic",
            "finished": "Generic",
            "ready": "Generic",
            "ok": "Generic",
            "emoji": "Generic",
            "check": "Generic",
            "checkmark": "Generic",
        }

        # Find matching category
        cat_lower = category.lower()
        for key, value in category_map.items():
            if key in cat_lower:
                return value

        return "Other"

    def test_text(self, text: str) -> List[Tuple[PatternInfo, re.Match]]:
        """
        Test text against all patterns and return matches.

        Returns list of (pattern, match) tuples for all matching patterns.
        """
        matches = []
        text_lower = text.lower()

        for pattern in self.get_all_patterns():
            try:
                match = re.search(pattern.regex, text_lower, re.I)
                if match:
                    matches.append((pattern, match))
            except re.error:
                continue

        # Sort by confidence
        matches.sort(key=lambda x: x[0].confidence, reverse=True)
        return matches

    def discover_patterns(self, hours: int = 168) -> List[Dict]:
        """
        Discover potential new patterns from work log.

        Args:
            hours: Hours of history to analyze (default 1 week)

        Returns:
            List of potential patterns with suggested regex
        """
        if not WORK_LOG_AVAILABLE:
            return []

        entries = work_log.get_recent_entries(hours=hours, include_processed=True)
        if not entries:
            return []

        return discover_new_patterns(entries)

    def add_pattern(self, regex: str, confidence: float, category: str) -> bool:
        """
        Add a new learned pattern.

        Args:
            regex: The regex pattern
            confidence: Confidence score (0.0-1.0)
            category: Category name

        Returns:
            True if added successfully
        """
        if not PATTERN_REGISTRY_AVAILABLE:
            return False

        try:
            registry = EvolvingPatternRegistry()
            # Store as a learned pattern
            registry._store_pattern(regex, confidence, category, "manual")
            self.learned_patterns.append(PatternInfo(
                regex=regex,
                confidence=confidence,
                category=category,
                source="learned",
                added_at=datetime.now().isoformat()
            ))
            return True
        except Exception:
            return False

    def export_patterns(self, include_static: bool = False) -> Dict:
        """
        Export patterns for sharing/backup.

        Args:
            include_static: Whether to include built-in static patterns

        Returns:
            Dictionary with patterns and metadata
        """
        patterns = []

        if include_static:
            for p in self.static_patterns:
                patterns.append(asdict(p))

        for p in self.learned_patterns:
            patterns.append(asdict(p))

        return {
            "version": "1.0",
            "exported_at": datetime.now().isoformat(),
            "pattern_count": len(patterns),
            "patterns": patterns
        }

    def get_stats(self) -> Dict:
        """Get pattern statistics."""
        all_patterns = self.get_all_patterns()
        by_category = self.get_patterns_by_category()

        # Count active (non-excluded) patterns
        active_patterns = [p for p in all_patterns if not p.excluded]

        return {
            "total_patterns": len(all_patterns),
            "active_patterns": len(active_patterns),
            "excluded_patterns": len(self.excluded_patterns),
            "static_patterns": len(self.static_patterns),
            "learned_patterns": len(self.learned_patterns),
            "categories": {cat: len(patterns) for cat, patterns in by_category.items()},
            "avg_confidence": sum(p.confidence for p in active_patterns) / len(active_patterns) if active_patterns else 0,
        }

    # =========================================================================
    # PATTERN RISK ANALYSIS
    # =========================================================================

    def analyze_pattern_risk(self, pattern: PatternInfo) -> PatternRisk:
        """
        Analyze a single pattern for potential risks.

        Evaluates TWO TYPES OF RISK:
        1. HEURISTIC RISK - Based on pattern characteristics:
           - Broadness (how much does it match?)
           - Specificity (does it match real success scenarios?)
           - Ambiguity (does it also match failure scenarios?)
           - Complexity (is the regex too complex or too simple?)

        2. EXPERIENCE RISK - Based on actual outcome history:
           - False positive rate from recorded outcomes
           - Success rate from confirmed successes
           - Weighted by amount of data we have

        The final risk score combines both, with experience heavily weighted
        when sufficient data exists.
        """
        issues = []
        heuristic_score = 0.0
        fp_indicators = []

        regex = pattern.regex.lower()

        # =====================================================================
        # HEURISTIC ANALYSIS (pattern characteristics)
        # =====================================================================

        # Check 1: Overly broad patterns
        if len(regex) < 10:
            issues.append("Pattern is very short (may match too broadly)")
            heuristic_score += 0.2

        for broad in self.OVERLY_BROAD_PATTERNS:
            try:
                if re.match(broad, regex, re.I):
                    issues.append(f"Pattern matches known overly-broad pattern")
                    heuristic_score += 0.3
                    break
            except re.error:
                continue

        # Check 2: Low confidence patterns
        if pattern.confidence < 0.6:
            issues.append(f"Low confidence ({pattern.confidence:.0%}) indicates uncertainty")
            heuristic_score += 0.15

        # Check 3: Check for false positive overlap
        # Test pattern against known failure indicators
        for indicator in self.FALSE_POSITIVE_INDICATORS:
            test_text = f"something {indicator} something"
            try:
                if re.search(pattern.regex, test_text, re.I):
                    fp_indicators.append(indicator)
            except re.error:
                continue

        if fp_indicators:
            issues.append(f"Pattern may match failure contexts: {', '.join(fp_indicators[:3])}")
            heuristic_score += 0.1 * len(fp_indicators)

        # Check 4: Regex complexity (too complex = maintenance burden)
        special_chars = sum(1 for c in regex if c in r'[]{}()|*+?^$\\')
        if special_chars > 15:
            issues.append("Complex regex pattern (harder to maintain)")
            heuristic_score += 0.1

        # Check 5: Generic single-word patterns
        if re.match(r'^\\b\w+\\b$', regex):
            issues.append("Single-word pattern without context")
            heuristic_score += 0.2

        # Check 6: No word boundaries
        if r'\b' not in regex and r'\s' not in regex:
            issues.append("No word boundaries (may match partial words)")
            heuristic_score += 0.15

        heuristic_score = min(1.0, heuristic_score)

        # =====================================================================
        # EXPERIENCE-BASED ANALYSIS (actual outcome history)
        # =====================================================================

        experience_modifier = 0.0
        experience_explanation = ""
        outcome_stats = None

        if self.evolution_engine:
            # Get the experience-based risk modifier
            experience_modifier, experience_explanation = \
                self.evolution_engine.get_experience_risk_modifier(pattern.regex)

            # Get full outcome stats for reporting
            outcome_stats = self.evolution_engine.get_outcome_stats(pattern.regex)

            if outcome_stats and outcome_stats["total_outcomes"] > 0:
                issues.append(f"Experience: {experience_explanation}")

        # =====================================================================
        # COMBINED RISK SCORE
        # =====================================================================

        # Experience-based data is weighted MORE heavily than heuristics
        # when we have sufficient outcome data
        if outcome_stats and outcome_stats["total_outcomes"] >= 3:
            # With good experience data, weight experience 60%, heuristics 40%
            experience_weight = 0.6
            heuristic_weight = 0.4
            risk_score = (heuristic_score * heuristic_weight) + \
                        max(0, min(1, 0.5 + experience_modifier)) * experience_weight
        elif outcome_stats and outcome_stats["total_outcomes"] > 0:
            # With limited data, weight experience 30%, heuristics 70%
            experience_weight = 0.3
            heuristic_weight = 0.7
            risk_score = (heuristic_score * heuristic_weight) + \
                        max(0, min(1, 0.5 + experience_modifier)) * experience_weight
        else:
            # No experience data - use heuristics only
            risk_score = heuristic_score

        risk_score = min(1.0, max(0.0, risk_score))

        # Determine risk level
        if risk_score >= 0.7:
            risk_level = RiskLevel.CRITICAL
            recommendation = "EXCLUDE: High false positive risk"
        elif risk_score >= 0.5:
            risk_level = RiskLevel.HIGH
            recommendation = "REVIEW: Consider excluding or refining"
        elif risk_score >= 0.3:
            risk_level = RiskLevel.MEDIUM
            recommendation = "MONITOR: May need adjustment"
        else:
            risk_level = RiskLevel.LOW
            recommendation = "OK: Pattern appears healthy"

        # Adjust recommendation based on experience
        if outcome_stats and outcome_stats["total_outcomes"] >= 3:
            if outcome_stats["false_positive_rate"] and outcome_stats["false_positive_rate"] >= 0.5:
                recommendation = "EXCLUDE: High FP rate from actual experience"
            elif outcome_stats["success_rate"] and outcome_stats["success_rate"] >= 0.9:
                recommendation = "EXCELLENT: Proven reliable from experience"

        return PatternRisk(
            regex=pattern.regex,
            risk_level=risk_level,
            risk_score=risk_score,
            issues=issues,
            recommendation=recommendation,
            false_positive_indicators=fp_indicators,
            experience_modifier=experience_modifier,
            experience_explanation=experience_explanation,
            outcome_stats=outcome_stats,
            heuristic_score=heuristic_score,
        )

    def analyze_all_risks(self) -> List[PatternRisk]:
        """
        Analyze all patterns and return risk assessments.

        Returns patterns sorted by risk score (highest first).
        """
        risks = []
        for pattern in self.get_all_patterns():
            if not pattern.excluded:  # Skip already excluded
                risk = self.analyze_pattern_risk(pattern)
                risks.append(risk)

        # Sort by risk score (highest first)
        risks.sort(key=lambda r: r.risk_score, reverse=True)
        return risks

    def get_risky_patterns(self, min_risk_score: float = 0.5) -> List[PatternRisk]:
        """Get patterns above the risk threshold."""
        return [r for r in self.analyze_all_risks() if r.risk_score >= min_risk_score]

    def get_risk_summary(self) -> Dict:
        """Get a summary of pattern health across the ecosystem."""
        risks = self.analyze_all_risks()

        by_level = {
            RiskLevel.CRITICAL: [],
            RiskLevel.HIGH: [],
            RiskLevel.MEDIUM: [],
            RiskLevel.LOW: [],
        }

        for risk in risks:
            by_level[risk.risk_level].append(risk)

        return {
            "total_analyzed": len(risks),
            "critical_count": len(by_level[RiskLevel.CRITICAL]),
            "high_count": len(by_level[RiskLevel.HIGH]),
            "medium_count": len(by_level[RiskLevel.MEDIUM]),
            "low_count": len(by_level[RiskLevel.LOW]),
            "avg_risk_score": sum(r.risk_score for r in risks) / len(risks) if risks else 0,
            "top_risks": risks[:10],  # Top 10 riskiest
            "excluded_count": len(self.excluded_patterns),
        }

    # =========================================================================
    # PATTERN EXCLUSION MANAGEMENT (delegates to evolution engine)
    # =========================================================================

    def exclude_pattern(self, regex: str, reason: str, risk_score: float = 0.0,
                       risk_level: str = "medium") -> bool:
        """
        Exclude a pattern from active detection.

        Args:
            regex: The regex pattern to exclude
            reason: Why this pattern is being excluded
            risk_score: Optional risk score from analysis
            risk_level: Risk level ('low', 'medium', 'high', 'critical')

        Returns:
            True if excluded successfully
        """
        if not self.evolution_engine:
            print("Evolution engine not available - cannot exclude patterns")
            return False

        success = self.evolution_engine.exclude_pattern(regex, reason, risk_score, risk_level)

        if success:
            # Update in-memory state
            self.excluded_patterns[regex] = reason

            # Update pattern info if present
            for p in self.static_patterns + self.learned_patterns:
                if p.regex == regex:
                    p.excluded = True
                    p.exclusion_reason = reason
                    break

        return success

    def restore_pattern(self, regex: str) -> bool:
        """
        Restore a previously excluded pattern.

        Args:
            regex: The regex pattern to restore

        Returns:
            True if restored successfully
        """
        if not self.evolution_engine:
            return False

        success = self.evolution_engine.restore_pattern(regex)

        if success:
            # Update in-memory state
            if regex in self.excluded_patterns:
                del self.excluded_patterns[regex]

            # Update pattern info
            for p in self.static_patterns + self.learned_patterns:
                if p.regex == regex:
                    p.excluded = False
                    p.exclusion_reason = None
                    break

        return success

    def get_exclusions(self) -> List[Dict]:
        """Get all excluded patterns with details."""
        if not self.evolution_engine:
            return []
        return self.evolution_engine.get_exclusions()

    def record_feedback(self, regex: str, was_false_positive: bool, context: str = ""):
        """
        Record user feedback about a pattern match.

        This helps the system learn which patterns need review.
        """
        if self.evolution_engine:
            self.evolution_engine.record_feedback(regex, was_false_positive, context)

    def get_false_positive_stats(self) -> Dict[str, Dict]:
        """Get false positive counts by pattern."""
        if not self.evolution_engine:
            return {}
        return self.evolution_engine.get_false_positive_stats()

    # =========================================================================
    # OUTCOME TRACKING (delegates to evolution engine)
    # =========================================================================

    def record_outcome(self, regex: str, outcome: str, matched_text: str = "",
                      context: str = "", follow_up_text: str = "",
                      session_id: str = "") -> bool:
        """
        Record what actually happened after a pattern matched.

        Args:
            regex: The pattern that matched
            outcome: 'confirmed_success', 'false_positive', or 'uncertain'
            matched_text: The text that triggered the pattern
            context: What was happening when pattern fired
            follow_up_text: What happened after
            session_id: Optional session identifier

        Returns:
            True if recorded successfully
        """
        if not self.evolution_engine:
            print("Evolution engine not available - cannot record outcomes")
            return False
        return self.evolution_engine.record_outcome(
            regex, outcome, matched_text, context, follow_up_text, session_id
        )

    def get_outcome_stats(self, regex: str = None) -> Dict:
        """Get outcome statistics for a pattern or all patterns."""
        if not self.evolution_engine:
            return {}
        return self.evolution_engine.get_outcome_stats(regex)

    def get_worst_performing_patterns(self, min_outcomes: int = 3, limit: int = 10) -> List[Dict]:
        """Get patterns with highest false positive rates."""
        if not self.evolution_engine:
            return []
        return self.evolution_engine.get_worst_performing_patterns(min_outcomes, limit)

    def get_experience_summary(self) -> Dict:
        """Get a summary of experience-based pattern evaluation."""
        if not self.evolution_engine:
            return {"error": "Evolution engine not available"}

        all_stats = self.evolution_engine.get_all_outcome_stats()
        total_outcomes = sum(s["total_outcomes"] for s in all_stats.values())
        patterns_with_data = len(all_stats)

        worst = self.evolution_engine.get_worst_performing_patterns(min_outcomes=2, limit=5)

        return {
            "patterns_with_outcome_data": patterns_with_data,
            "total_outcomes_recorded": total_outcomes,
            "worst_performing": worst,
            "by_experience_level": self._count_by_experience_level(all_stats),
        }

    def _count_by_experience_level(self, all_stats: Dict) -> Dict[str, int]:
        """Count patterns by experience level."""
        counts = {"no_data": 0, "limited": 0, "moderate": 0, "good": 0, "extensive": 0}
        for stats in all_stats.values():
            level = stats.get("experience_level", "no_data")
            counts[level] = counts.get(level, 0) + 1
        return counts

    def print_patterns(self, category_filter: Optional[str] = None):
        """Print patterns in a readable format."""
        by_category = self.get_patterns_by_category()

        for category, patterns in sorted(by_category.items()):
            if category_filter and category.lower() != category_filter.lower():
                continue

            print(f"\n{'='*60}")
            print(f"  {category.upper()} ({len(patterns)} patterns)")
            print(f"{'='*60}")

            # Sort by confidence
            for p in sorted(patterns, key=lambda x: x.confidence, reverse=True):
                source_icon = "📦" if p.source == "static" else "🧠"
                print(f"  {source_icon} [{p.confidence:.2f}] {p.category}")
                print(f"      Pattern: {p.regex[:60]}{'...' if len(p.regex) > 60 else ''}")


def _show_config_section(manager: PatternManager, section: str):
    """Show configuration options for a specific section."""
    if section == "1":
        print("\n  ═══════════════════════════════════════════════════════")
        print("  PATTERN EVOLUTION SETTINGS")
        print("  ═══════════════════════════════════════════════════════")
        print("\n  Current settings:")
        print("    • Min occurrences to promote: 3")
        print("    • Promotion confidence threshold: 0.70")
        print("    • Discovery lookback: 168 hours (1 week)")
        print("\n  To modify: Edit memory/pattern_evolution.py")
        print("    Line ~105: self.min_occurrences_to_promote = 3")
        print("    Line ~106: self.promotion_confidence = 0.7")
        print("    Line ~107: self.discovery_hours = 168")

    elif section == "2":
        print("\n  ═══════════════════════════════════════════════════════")
        print("  RISK ANALYSIS THRESHOLDS")
        print("  ═══════════════════════════════════════════════════════")
        print("\n  Risk Level Thresholds:")
        print("    • CRITICAL: ≥ 0.70 risk score")
        print("    • HIGH:     ≥ 0.50 risk score")
        print("    • MEDIUM:   ≥ 0.30 risk score")
        print("    • LOW:      < 0.30 risk score")
        print("\n  Experience Weighting:")
        print("    • 3+ outcomes: 60% experience, 40% heuristic")
        print("    • 1-2 outcomes: 30% experience, 70% heuristic")
        print("    • 0 outcomes: 100% heuristic")
        print("\n  To modify: Edit memory/pattern_manager.py")
        print("    Search for 'experience_weight' or 'risk_score >='")

    elif section == "3":
        print("\n  ═══════════════════════════════════════════════════════")
        print("  EXCLUSION RULES")
        print("  ═══════════════════════════════════════════════════════")
        exclusions = manager.get_exclusions()
        print(f"\n  Currently excluded: {len(exclusions)} patterns")
        if exclusions:
            for exc in exclusions[:5]:
                print(f"    • {exc['regex'][:40]}... ({exc['risk_level']})")
            if len(exclusions) > 5:
                print(f"    ... and {len(exclusions) - 5} more")
        print("\n  Exclusion storage: memory/.pattern_evolution.db")
        print("  Table: pattern_exclusions")
        print("\n  Commands:")
        print("    • Exclude: python pattern_manager.py exclude <regex> <reason>")
        print("    • Restore: python pattern_manager.py restore <regex>")
        print("    • List:    python pattern_manager.py exclusions")

    elif section == "4":
        print("\n  ═══════════════════════════════════════════════════════")
        print("  OUTCOME TRACKING CONFIG")
        print("  ═══════════════════════════════════════════════════════")
        summary = manager.get_experience_summary()
        print(f"\n  Patterns with outcome data: {summary.get('patterns_with_outcome_data', 0)}")
        print(f"  Total outcomes recorded: {summary.get('total_outcomes_recorded', 0)}")
        print("\n  Outcome types:")
        print("    • confirmed_success - Pattern correctly identified success")
        print("    • false_positive - Pattern matched but wasn't success")
        print("    • uncertain - Unclear/mixed result")
        print("\n  Outcome storage: memory/.pattern_evolution.db")
        print("  Table: pattern_outcomes")
        print("\n  Commands:")
        print("    • Record: python pattern_manager.py outcome <regex> <type> [context]")
        print("    • Stats:  python pattern_manager.py experience")
        print("    • Worst:  python pattern_manager.py worst [min_outcomes]")

    else:
        print("\n  ═══════════════════════════════════════════════════════")
        print("  CONTEXT DNA - FULL CONFIGURATION OVERVIEW")
        print("  ═══════════════════════════════════════════════════════")
        print("\n  📁 Configuration Files:")
        print("    • memory/pattern_evolution.py - Evolution engine settings")
        print("    • memory/pattern_manager.py - Risk analysis + management")
        print("    • memory/objective_success.py - Core success patterns")
        print("    • memory/.pattern_evolution.db - SQLite database")
        print("\n  🗄️  Database Tables:")
        print("    • pattern_candidates - Patterns being evaluated")
        print("    • pattern_exclusions - Excluded patterns")
        print("    • pattern_feedback - User feedback")
        print("    • pattern_outcomes - Success/failure tracking")
        print("    • pattern_risk_cache - Cached risk analysis")
        print("    • evolution_log - Event history")
        print("\n  🔧 Key Configuration Points:")
        print("    [1] Pattern Evolution Settings")
        print("    [2] Risk Analysis Thresholds")
        print("    [3] Exclusion Rules")
        print("    [4] Outcome Tracking Config")


def interactive_menu():
    """Interactive pattern management menu."""
    manager = PatternManager()

    while True:
        print("\n" + "="*60)
        print("  OBJECTIVE SUCCESS PATTERN MANAGER")
        print("="*60)
        stats = manager.get_stats()
        print(f"\n  Total Patterns: {stats['total_patterns']} (Active: {stats['active_patterns']}, Excluded: {stats['excluded_patterns']})")
        print(f"  Static: {stats['static_patterns']} | Learned: {stats['learned_patterns']}")

        # Show risk warning if there are high-risk patterns
        risk_summary = manager.get_risk_summary()
        risky_count = risk_summary["critical_count"] + risk_summary["high_count"]
        if risky_count > 0:
            print(f"\n  ⚠️  PATTERN HEALTH: {risky_count} HIGH-RISK patterns need review")

        print("\n  PATTERN COMMANDS:")
        print("  ──────────────────")
        print("  1. List all patterns")
        print("  2. List patterns by category")
        print("  3. Test text against patterns")
        print("  4. Discover new patterns from work log")
        print("  5. Add a new pattern manually")
        print("  6. Export patterns")
        print("  7. Show statistics")

        print("\n  PATTERN HEALTH (Self-Analysis):")
        print("  ─────────────────────────────────")
        print("  8. Analyze pattern health/risks")
        print("  9. View risky patterns (needs review)")
        print("  10. Exclude a pattern")
        print("  11. View excluded patterns")
        print("  12. Restore excluded pattern")

        print("\n  EXPERIENCE TRACKING (Outcome Learning):")
        print("  ─────────────────────────────────────────")
        print("  13. Record pattern outcome (success/false positive)")
        print("  14. View experience summary")
        print("  15. View worst-performing patterns")
        print("\n  q. Quit")

        choice = input("\n  Enter choice: ").strip().lower()

        if choice == "q":
            print("\n  Goodbye!")
            break

        elif choice == "1":
            manager.print_patterns()

        elif choice == "2":
            print("\n  Available categories:")
            for cat in sorted(manager.get_patterns_by_category().keys()):
                print(f"    - {cat}")
            cat = input("\n  Enter category (or 'all'): ").strip()
            if cat.lower() != "all":
                manager.print_patterns(cat)
            else:
                manager.print_patterns()

        elif choice == "3":
            text = input("\n  Enter text to test: ").strip()
            if text:
                matches = manager.test_text(text)
                if matches:
                    print(f"\n  Found {len(matches)} matching pattern(s):")
                    for pattern, match in matches:
                        print(f"\n  ✓ [{pattern.confidence:.2f}] {pattern.category}")
                        print(f"    Pattern: {pattern.regex[:50]}...")
                        print(f"    Matched: \"{match.group()}\"")
                else:
                    print("\n  No patterns matched this text.")
                    print("  Consider adding it as a new pattern if it indicates success.")

        elif choice == "4":
            print("\n  Discovering patterns from work log...")
            potential = manager.discover_patterns()
            if potential:
                print(f"\n  Found {len(potential)} potential new patterns:\n")
                for i, p in enumerate(potential[:10], 1):
                    print(f"  {i}. \"{p['text'][:50]}...\"")
                    print(f"     Frequency: {p['count']} | Suggested: {p['suggested_regex'][:40]}...")
            else:
                print("\n  No new patterns discovered.")

        elif choice == "5":
            print("\n  ADD NEW PATTERN")
            print("  ───────────────")
            regex = input("  Enter regex pattern: ").strip()
            if not regex:
                continue

            # Validate regex
            try:
                re.compile(regex)
            except re.error as e:
                print(f"\n  Invalid regex: {e}")
                continue

            conf = input("  Enter confidence (0.0-1.0) [0.75]: ").strip()
            confidence = float(conf) if conf else 0.75

            category = input("  Enter category [generic]: ").strip() or "generic"

            if manager.add_pattern(regex, confidence, category):
                print(f"\n  ✓ Pattern added successfully!")
            else:
                print(f"\n  ✗ Failed to add pattern (pattern registry not available)")

        elif choice == "6":
            include_static = input("\n  Include static patterns? (y/N): ").strip().lower() == "y"
            export = manager.export_patterns(include_static)
            filename = f"patterns_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(filename, "w") as f:
                json.dump(export, f, indent=2)
            print(f"\n  ✓ Exported {export['pattern_count']} patterns to {filename}")

        elif choice == "7":
            stats = manager.get_stats()
            print("\n  PATTERN STATISTICS")
            print("  ──────────────────")
            print(f"  Total patterns:    {stats['total_patterns']}")
            print(f"  Active patterns:   {stats['active_patterns']}")
            print(f"  Excluded patterns: {stats['excluded_patterns']}")
            print(f"  Static patterns:   {stats['static_patterns']}")
            print(f"  Learned patterns:  {stats['learned_patterns']}")
            print(f"  Avg confidence:    {stats['avg_confidence']:.2%}")
            print("\n  By category:")
            for cat, count in sorted(stats['categories'].items(), key=lambda x: -x[1]):
                bar = "█" * (count // 5) + "░" * (20 - count // 5)
                print(f"    {cat:20} [{bar}] {count}")

        elif choice == "8":
            # Analyze pattern health/risks
            print("\n  PATTERN HEALTH ANALYSIS")
            print("  ────────────────────────")
            print("  Analyzing all patterns for potential risks...")

            risk_summary = manager.get_risk_summary()
            print(f"\n  📊 SUMMARY")
            print(f"     Patterns analyzed: {risk_summary['total_analyzed']}")
            print(f"     Average risk score: {risk_summary['avg_risk_score']:.1%}")
            print(f"\n  📈 BY RISK LEVEL:")
            print(f"     🔴 CRITICAL: {risk_summary['critical_count']}")
            print(f"     🟠 HIGH:     {risk_summary['high_count']}")
            print(f"     🟡 MEDIUM:   {risk_summary['medium_count']}")
            print(f"     🟢 LOW:      {risk_summary['low_count']}")
            print(f"     🚫 Excluded: {risk_summary['excluded_count']}")

            if risk_summary['critical_count'] + risk_summary['high_count'] > 0:
                print("\n  ⚠️  Recommendation: Review risky patterns (option 9)")

        elif choice == "9":
            # View risky patterns
            print("\n  RISKY PATTERNS (needs review)")
            print("  ──────────────────────────────")

            risky = manager.get_risky_patterns(min_risk_score=0.3)
            if not risky:
                print("\n  ✅ No risky patterns found! All patterns appear healthy.")
                continue

            # Separate by risk level for batch actions
            critical_high = [r for r in risky if r.risk_level in (RiskLevel.CRITICAL, RiskLevel.HIGH)]

            print(f"\n  Found {len(risky)} patterns with risk score ≥ 0.3")
            if critical_high:
                print(f"  🔴 {len(critical_high)} CRITICAL/HIGH risk patterns suggested for exclusion\n")

            for i, risk in enumerate(risky[:15], 1):
                level_icon = {
                    RiskLevel.CRITICAL: "🔴",
                    RiskLevel.HIGH: "🟠",
                    RiskLevel.MEDIUM: "🟡",
                    RiskLevel.LOW: "🟢",
                }[risk.risk_level]

                print(f"  {i}. {level_icon} [{risk.risk_score:.0%}] {risk.risk_level.value.upper()}")
                print(f"     Pattern: {risk.regex[:50]}{'...' if len(risk.regex) > 50 else ''}")

                # Show breakdown of heuristic vs experience scores
                if risk.outcome_stats and risk.outcome_stats.get("total_outcomes", 0) > 0:
                    stats = risk.outcome_stats
                    print(f"     📊 Heuristic: {risk.heuristic_score:.0%} | Experience: {risk.experience_modifier:+.0%}")
                    print(f"        ({stats['total_outcomes']} outcomes: {stats['success_rate']:.0%} success rate)" if stats['success_rate'] else f"        ({stats['total_outcomes']} outcomes)")
                else:
                    print(f"     📊 Heuristic only: {risk.heuristic_score:.0%} (no outcome data)")

                if risk.issues:
                    # Filter to show most relevant issues
                    non_experience_issues = [i for i in risk.issues if not i.startswith("Experience:")][:2]
                    if non_experience_issues:
                        print(f"     Issues: {'; '.join(non_experience_issues)}")

                print(f"     {risk.recommendation}")
                print()

            if len(risky) > 15:
                print(f"  ... and {len(risky) - 15} more risky patterns")

            # Get experience-based recommendations
            experience_based = [r for r in risky if r.outcome_stats and
                               r.outcome_stats.get("total_outcomes", 0) >= 3 and
                               r.outcome_stats.get("false_positive_rate", 0) >= 0.3]

            # Quick actions menu
            print("\n  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            print("  QUICK ACTIONS:")
            print("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            if critical_high:
                print(f"  a. Accept ALL suggested exclusions ({len(critical_high)} CRITICAL/HIGH)")
                print(f"  c. Accept CRITICAL only ({len([r for r in critical_high if r.risk_level == RiskLevel.CRITICAL])} patterns)")
            if experience_based:
                print(f"  x. Accept EXPERIENCE-BASED exclusions ({len(experience_based)} proven bad performers)")
            print("  #. Exclude specific pattern by number")
            print("  e. Edit pattern in Context DNA config")
            print("  b. Back to main menu")

            action = input("\n  Action: ").strip().lower()

            if action == "a" and critical_high:
                # Accept all critical/high exclusions
                print(f"\n  Excluding {len(critical_high)} CRITICAL/HIGH risk patterns...")
                excluded_count = 0
                for risk in critical_high:
                    reason = f"Auto-excluded: {risk.risk_level.value} risk ({risk.risk_score:.0%})"
                    if manager.exclude_pattern(risk.regex, reason, risk.risk_score, risk.risk_level.value):
                        excluded_count += 1
                        print(f"    ✅ {risk.regex[:40]}...")
                    else:
                        print(f"    ❌ Failed: {risk.regex[:40]}...")
                print(f"\n  ✅ Excluded {excluded_count}/{len(critical_high)} patterns")

            elif action == "c" and critical_high:
                # Accept critical only
                critical_only = [r for r in critical_high if r.risk_level == RiskLevel.CRITICAL]
                if not critical_only:
                    print("\n  No CRITICAL patterns to exclude")
                else:
                    print(f"\n  Excluding {len(critical_only)} CRITICAL risk patterns...")
                    excluded_count = 0
                    for risk in critical_only:
                        reason = f"Auto-excluded: CRITICAL risk ({risk.risk_score:.0%})"
                        if manager.exclude_pattern(risk.regex, reason, risk.risk_score, "critical"):
                            excluded_count += 1
                            print(f"    ✅ {risk.regex[:40]}...")
                    print(f"\n  ✅ Excluded {excluded_count}/{len(critical_only)} patterns")

            elif action == "x":
                # Accept experience-based exclusions (proven bad performers)
                if not experience_based:
                    print("\n  No patterns with sufficient negative experience data")
                    print("  (Need 3+ outcomes with ≥30% false positive rate)")
                else:
                    print(f"\n  🧪 EXPERIENCE-BASED EXCLUSION")
                    print(f"  ────────────────────────────────")
                    print(f"  These {len(experience_based)} patterns have PROVEN poor performance:\n")

                    for risk in experience_based:
                        stats = risk.outcome_stats
                        print(f"    • {risk.regex[:40]}...")
                        print(f"      FP Rate: {stats['false_positive_rate']:.0%} ({stats['false_positives']}/{stats['total_outcomes']} outcomes)")

                    confirm = input(f"\n  Exclude all {len(experience_based)} proven bad performers? (y/N): ").strip().lower()
                    if confirm == "y":
                        excluded_count = 0
                        for risk in experience_based:
                            stats = risk.outcome_stats
                            reason = f"Experience-based exclusion: {stats['false_positive_rate']:.0%} FP rate over {stats['total_outcomes']} outcomes"
                            if manager.exclude_pattern(risk.regex, reason, risk.risk_score, risk.risk_level.value):
                                excluded_count += 1
                                print(f"    ✅ {risk.regex[:40]}...")
                        print(f"\n  ✅ Excluded {excluded_count}/{len(experience_based)} experience-proven bad patterns")
                    else:
                        print("\n  Cancelled")

            elif action == "e":
                # Open config deep-link
                print("\n  CONTEXT DNA CONFIGURATION")
                print("  ──────────────────────────")
                print("\n  Pattern configuration can be accessed via:")
                print(f"    • CLI: python memory/pattern_manager.py menu")
                print(f"    • API: http://localhost:3456/patterns/config")
                print(f"    • File: memory/.pattern_evolution.db (SQLite)")
                print("\n  Configuration sections:")
                print("    [1] Pattern Evolution Settings")
                print("    [2] Risk Analysis Thresholds")
                print("    [3] Exclusion Rules")
                print("    [4] Outcome Tracking Config")

                config_section = input("\n  Open section (1-4, or enter for full): ").strip()
                _show_config_section(manager, config_section)

            elif action.isdigit():
                # Exclude specific pattern
                idx = int(action) - 1
                if 0 <= idx < len(risky[:15]):
                    risk = risky[idx]
                    reason = f"Manual exclusion: {risk.recommendation}"
                    if manager.exclude_pattern(risk.regex, reason, risk.risk_score, risk.risk_level.value):
                        print(f"\n  ✅ Excluded: {risk.regex[:50]}...")
                    else:
                        print(f"\n  ❌ Failed to exclude")

        elif choice == "10":
            # Exclude a pattern
            print("\n  EXCLUDE A PATTERN")
            print("  ──────────────────")

            # Show risky patterns as suggestions
            risky = manager.get_risky_patterns(min_risk_score=0.5)
            if risky:
                print("\n  Suggested patterns to exclude (high risk):")
                for i, risk in enumerate(risky[:5], 1):
                    print(f"    {i}. [{risk.risk_score:.0%}] {risk.regex[:40]}...")

            print("\n  Enter pattern to exclude (or number from above):")
            pattern_input = input("  Pattern: ").strip()

            if not pattern_input:
                continue

            # Check if user entered a number
            try:
                idx = int(pattern_input) - 1
                if 0 <= idx < len(risky):
                    pattern_to_exclude = risky[idx].regex
                    suggested_reason = f"Auto-suggested: {risky[idx].recommendation}"
                else:
                    print("  Invalid number")
                    continue
            except ValueError:
                pattern_to_exclude = pattern_input
                suggested_reason = ""

            # Get reason
            reason = input(f"  Reason [{suggested_reason[:40] or 'Manual exclusion'}]: ").strip()
            if not reason:
                reason = suggested_reason or "Manual exclusion"

            # Analyze risk for the pattern if not from suggestions
            risk_score = 0.0
            risk_level = "medium"
            for risk in manager.analyze_all_risks():
                if risk.regex == pattern_to_exclude:
                    risk_score = risk.risk_score
                    risk_level = risk.risk_level.value
                    break

            if manager.exclude_pattern(pattern_to_exclude, reason, risk_score, risk_level):
                print(f"\n  ✅ Pattern excluded successfully!")
            else:
                print(f"\n  ❌ Failed to exclude pattern")

        elif choice == "11":
            # View excluded patterns
            print("\n  EXCLUDED PATTERNS")
            print("  ──────────────────")

            exclusions = manager.get_exclusions()
            if not exclusions:
                print("\n  No patterns are currently excluded.")
                continue

            print(f"\n  {len(exclusions)} pattern(s) excluded:\n")
            for i, exc in enumerate(exclusions, 1):
                print(f"  {i}. {exc['regex'][:50]}{'...' if len(exc['regex']) > 50 else ''}")
                print(f"     Reason: {exc['reason']}")
                print(f"     Risk: {exc.get('risk_score', 0):.0%} ({exc.get('risk_level', 'unknown')})")
                print(f"     Excluded: {exc['excluded_at'][:10]}")
                print()

        elif choice == "12":
            # Restore excluded pattern
            print("\n  RESTORE EXCLUDED PATTERN")
            print("  ─────────────────────────")

            exclusions = manager.get_exclusions()
            if not exclusions:
                print("\n  No patterns are currently excluded.")
                continue

            print("\n  Currently excluded patterns:")
            for i, exc in enumerate(exclusions, 1):
                print(f"    {i}. {exc['regex'][:40]}...")

            pattern_input = input("\n  Enter number or pattern to restore: ").strip()

            if not pattern_input:
                continue

            # Check if user entered a number
            try:
                idx = int(pattern_input) - 1
                if 0 <= idx < len(exclusions):
                    pattern_to_restore = exclusions[idx]["regex"]
                else:
                    print("  Invalid number")
                    continue
            except ValueError:
                pattern_to_restore = pattern_input

            if manager.restore_pattern(pattern_to_restore):
                print(f"\n  ✅ Pattern restored successfully!")
            else:
                print(f"\n  ❌ Failed to restore pattern (may not be excluded)")

        elif choice == "13":
            # Record pattern outcome
            print("\n  RECORD PATTERN OUTCOME")
            print("  ───────────────────────")
            print("\n  This helps the system learn from experience.")
            print("  When a pattern matches, record whether it was actually a success.\n")

            # First, test some text to find matching patterns
            text = input("  Enter text that triggered the pattern (or pattern regex): ").strip()
            if not text:
                continue

            # Check if it's a pattern or text to test
            matches = manager.test_text(text)
            if matches:
                print(f"\n  Found {len(matches)} matching pattern(s):")
                for i, (pattern, match) in enumerate(matches[:5], 1):
                    print(f"    {i}. [{pattern.confidence:.2f}] {pattern.regex[:40]}...")
                    print(f"       Matched: \"{match.group()}\"")

                print("\n  Select pattern to record outcome for (or enter pattern regex):")
                pattern_input = input("  Choice: ").strip()

                try:
                    idx = int(pattern_input) - 1
                    if 0 <= idx < len(matches):
                        selected_regex = matches[idx][0].regex
                    else:
                        print("  Invalid number")
                        continue
                except ValueError:
                    selected_regex = pattern_input
            else:
                selected_regex = text
                print(f"\n  No matches found - using input as pattern regex")

            print(f"\n  Recording outcome for: {selected_regex[:50]}...")
            print("\n  What was the actual outcome?")
            print("    1. confirmed_success - It was actually a success")
            print("    2. false_positive - It matched but wasn't a real success")
            print("    3. uncertain - Not sure / mixed results")

            outcome_input = input("\n  Outcome (1/2/3): ").strip()
            outcome_map = {"1": "confirmed_success", "2": "false_positive", "3": "uncertain"}
            outcome = outcome_map.get(outcome_input, outcome_input)

            if outcome not in ("confirmed_success", "false_positive", "uncertain"):
                print("  Invalid outcome")
                continue

            context = input("  Context (what was happening, optional): ").strip()
            follow_up = input("  Follow-up (what happened after, optional): ").strip()

            if manager.record_outcome(selected_regex, outcome, text, context, follow_up):
                print(f"\n  ✅ Outcome recorded: {outcome}")

                # Show updated stats
                stats = manager.get_outcome_stats(selected_regex)
                if stats["total_outcomes"] > 0:
                    print(f"\n  Updated pattern stats:")
                    print(f"    Total outcomes: {stats['total_outcomes']}")
                    print(f"    Success rate: {stats['success_rate']:.0%}" if stats['success_rate'] else "    Success rate: N/A")
                    print(f"    Experience level: {stats['experience_level']}")
            else:
                print(f"\n  ❌ Failed to record outcome")

        elif choice == "14":
            # View experience summary
            print("\n  EXPERIENCE SUMMARY")
            print("  ───────────────────")

            summary = manager.get_experience_summary()
            if "error" in summary:
                print(f"\n  {summary['error']}")
                continue

            print(f"\n  📊 OUTCOME DATA OVERVIEW")
            print(f"     Patterns with outcome data: {summary['patterns_with_outcome_data']}")
            print(f"     Total outcomes recorded: {summary['total_outcomes_recorded']}")

            if summary.get("by_experience_level"):
                print(f"\n  📈 BY EXPERIENCE LEVEL:")
                levels = summary["by_experience_level"]
                for level, count in sorted(levels.items(), key=lambda x: -x[1]):
                    if count > 0:
                        bar = "█" * min(20, count) + "░" * (20 - min(20, count))
                        print(f"     {level:12} [{bar}] {count}")

            if summary.get("worst_performing"):
                print(f"\n  ⚠️  WORST PERFORMING PATTERNS:")
                for i, wp in enumerate(summary["worst_performing"], 1):
                    fp_rate = wp["false_positive_rate"]
                    print(f"     {i}. [{fp_rate:.0%} FP] {wp['regex'][:40]}...")
                    print(f"        ({wp['false_positives']}/{wp['total_outcomes']} false positives)")

            if summary['patterns_with_outcome_data'] == 0:
                print("\n  💡 TIP: Record outcomes (option 13) to enable experience-based risk analysis")

        elif choice == "15":
            # View worst-performing patterns
            print("\n  WORST-PERFORMING PATTERNS")
            print("  ──────────────────────────")
            print("\n  Patterns with highest false positive rates from actual experience:\n")

            worst = manager.get_worst_performing_patterns(min_outcomes=2, limit=15)

            if not worst:
                print("  No patterns have enough outcome data yet.")
                print("  Record outcomes (option 13) to track pattern performance.")
                continue

            for i, wp in enumerate(worst, 1):
                fp_rate = wp["false_positive_rate"]

                # Color indicator based on FP rate
                if fp_rate >= 0.5:
                    indicator = "🔴"
                elif fp_rate >= 0.3:
                    indicator = "🟠"
                elif fp_rate >= 0.1:
                    indicator = "🟡"
                else:
                    indicator = "🟢"

                print(f"  {i}. {indicator} [{fp_rate:.0%}] False Positive Rate")
                print(f"     Pattern: {wp['regex'][:50]}{'...' if len(wp['regex']) > 50 else ''}")
                print(f"     Data: {wp['false_positives']} FPs out of {wp['total_outcomes']} outcomes")
                print()

            if worst and worst[0]["false_positive_rate"] >= 0.5:
                print("\n  ⚠️  Consider excluding patterns with >50% false positive rate")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Default to interactive menu
        interactive_menu()
        sys.exit(0)

    cmd = sys.argv[1]
    manager = PatternManager()

    if cmd == "menu":
        interactive_menu()

    elif cmd == "list":
        category = sys.argv[2] if len(sys.argv) > 2 else None
        manager.print_patterns(category)

    elif cmd == "discover":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 168
        print(f"Discovering patterns from last {hours} hours...")
        potential = manager.discover_patterns(hours)
        if potential:
            print(f"\nFound {len(potential)} potential new patterns:\n")
            for p in potential[:20]:
                print(f"  \"{p['text'][:60]}...\"")
                print(f"    Freq: {p['count']} | Regex: {p['suggested_regex'][:50]}...")
                print()
        else:
            print("No new patterns discovered")

    elif cmd == "test":
        if len(sys.argv) < 3:
            print("Usage: pattern_manager.py test <text>")
            sys.exit(1)
        text = " ".join(sys.argv[2:])
        matches = manager.test_text(text)
        if matches:
            print(f"Found {len(matches)} match(es):")
            for pattern, match in matches:
                print(f"  [{pattern.confidence:.2f}] {pattern.category}: {match.group()}")
        else:
            print("No patterns matched")

    elif cmd == "add":
        if len(sys.argv) < 4:
            print("Usage: pattern_manager.py add <regex> <confidence> [category]")
            sys.exit(1)
        regex = sys.argv[2]
        confidence = float(sys.argv[3])
        category = sys.argv[4] if len(sys.argv) > 4 else "learned"
        if manager.add_pattern(regex, confidence, category):
            print(f"✓ Added pattern: {regex}")
        else:
            print("✗ Failed to add pattern")

    elif cmd == "export":
        include_static = "--static" in sys.argv
        export = manager.export_patterns(include_static)
        print(json.dumps(export, indent=2))

    elif cmd == "stats":
        stats = manager.get_stats()
        print(f"Total patterns: {stats['total_patterns']}")
        print(f"Active: {stats['active_patterns']}, Excluded: {stats['excluded_patterns']}")
        print(f"Static: {stats['static_patterns']}, Learned: {stats['learned_patterns']}")
        print(f"Categories: {', '.join(f'{k}({v})' for k,v in stats['categories'].items())}")

    elif cmd == "analyze":
        # Analyze pattern health
        print("Analyzing pattern health...")
        summary = manager.get_risk_summary()
        print(f"\nRisk Summary:")
        print(f"  Critical: {summary['critical_count']}")
        print(f"  High: {summary['high_count']}")
        print(f"  Medium: {summary['medium_count']}")
        print(f"  Low: {summary['low_count']}")
        print(f"  Avg Risk Score: {summary['avg_risk_score']:.1%}")

        if summary['critical_count'] + summary['high_count'] > 0:
            print(f"\n⚠️  {summary['critical_count'] + summary['high_count']} patterns need review")
            print("\nTop risky patterns:")
            for risk in summary['top_risks'][:5]:
                print(f"  [{risk.risk_score:.0%}] {risk.regex[:50]}...")

    elif cmd == "exclude":
        if len(sys.argv) < 4:
            print("Usage: pattern_manager.py exclude <regex> <reason>")
            sys.exit(1)
        regex = sys.argv[2]
        reason = " ".join(sys.argv[3:])

        # Analyze risk
        risk_score = 0.0
        risk_level = "medium"
        for risk in manager.analyze_all_risks():
            if risk.regex == regex:
                risk_score = risk.risk_score
                risk_level = risk.risk_level.value
                break

        if manager.exclude_pattern(regex, reason, risk_score, risk_level):
            print(f"✅ Excluded: {regex[:50]}...")
        else:
            print("❌ Failed to exclude pattern")

    elif cmd == "exclusions":
        exclusions = manager.get_exclusions()
        if exclusions:
            print(f"Excluded patterns ({len(exclusions)}):")
            for exc in exclusions:
                print(f"  [{exc.get('risk_score', 0):.0%}] {exc['regex'][:40]}...")
                print(f"    Reason: {exc['reason']}")
        else:
            print("No patterns excluded")

    elif cmd == "restore":
        if len(sys.argv) < 3:
            print("Usage: pattern_manager.py restore <regex>")
            sys.exit(1)
        regex = sys.argv[2]
        if manager.restore_pattern(regex):
            print(f"✅ Restored: {regex[:50]}...")
        else:
            print("❌ Failed to restore pattern")

    elif cmd == "outcome":
        # Record a pattern outcome
        if len(sys.argv) < 4:
            print("Usage: pattern_manager.py outcome <regex> <confirmed_success|false_positive|uncertain> [context]")
            sys.exit(1)
        regex = sys.argv[2]
        outcome = sys.argv[3]
        context = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""

        if manager.record_outcome(regex, outcome, "", context):
            print(f"✅ Recorded {outcome} for: {regex[:50]}...")
            stats = manager.get_outcome_stats(regex)
            if stats["total_outcomes"] > 0:
                print(f"   Now has {stats['total_outcomes']} outcomes, {stats['success_rate']:.0%} success rate" if stats['success_rate'] else f"   Now has {stats['total_outcomes']} outcomes")
        else:
            print("❌ Failed to record outcome")

    elif cmd == "experience":
        # Show experience summary
        summary = manager.get_experience_summary()
        if "error" in summary:
            print(f"Error: {summary['error']}")
            sys.exit(1)

        print(f"Experience Summary:")
        print(f"  Patterns with data: {summary['patterns_with_outcome_data']}")
        print(f"  Total outcomes: {summary['total_outcomes_recorded']}")

        if summary.get("worst_performing"):
            print(f"\nWorst Performing:")
            for wp in summary["worst_performing"]:
                print(f"  [{wp['false_positive_rate']:.0%} FP] {wp['regex'][:40]}...")

    elif cmd == "worst":
        # Show worst performing patterns
        min_outcomes = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        worst = manager.get_worst_performing_patterns(min_outcomes=min_outcomes, limit=10)

        if worst:
            print(f"Worst Performing Patterns (min {min_outcomes} outcomes):")
            for wp in worst:
                print(f"  [{wp['false_positive_rate']:.0%}] {wp['regex'][:50]}...")
                print(f"      {wp['false_positives']}/{wp['total_outcomes']} false positives")
        else:
            print(f"No patterns with {min_outcomes}+ outcomes yet")

    elif cmd == "auto-prune":
        # Automatic pruning based on experience data
        # Usage: pattern_manager.py auto-prune [--dry-run] [--min-fp-rate 0.5] [--min-outcomes 3]
        dry_run = "--dry-run" in sys.argv
        min_fp_rate = 0.5  # Default: 50% false positive rate
        min_outcomes = 3   # Default: need 3+ outcomes

        # Parse optional args
        for i, arg in enumerate(sys.argv):
            if arg == "--min-fp-rate" and i + 1 < len(sys.argv):
                min_fp_rate = float(sys.argv[i + 1])
            elif arg == "--min-outcomes" and i + 1 < len(sys.argv):
                min_outcomes = int(sys.argv[i + 1])

        print(f"{'[DRY RUN] ' if dry_run else ''}Auto-Pruning Patterns")
        print(f"  Min FP Rate: {min_fp_rate:.0%}")
        print(f"  Min Outcomes: {min_outcomes}")
        print()

        # Get risky patterns
        risky = manager.get_risky_patterns(min_risk_score=0.3)

        # Filter to experience-proven bad performers
        to_prune = []
        for risk in risky:
            if risk.outcome_stats and risk.outcome_stats.get("total_outcomes", 0) >= min_outcomes:
                fp_rate = risk.outcome_stats.get("false_positive_rate", 0)
                if fp_rate and fp_rate >= min_fp_rate:
                    to_prune.append(risk)

        if not to_prune:
            print(f"No patterns meet pruning criteria (>={min_fp_rate:.0%} FP, >={min_outcomes} outcomes)")
            sys.exit(0)

        print(f"Found {len(to_prune)} patterns to prune:\n")
        for risk in to_prune:
            stats = risk.outcome_stats
            print(f"  [{stats['false_positive_rate']:.0%}] {risk.regex[:50]}...")
            print(f"      {stats['false_positives']}/{stats['total_outcomes']} false positives")

        if dry_run:
            print(f"\n[DRY RUN] Would exclude {len(to_prune)} patterns")
            print("Run without --dry-run to actually exclude")
        else:
            print(f"\nExcluding {len(to_prune)} patterns...")
            excluded = 0
            for risk in to_prune:
                stats = risk.outcome_stats
                reason = f"Auto-pruned: {stats['false_positive_rate']:.0%} FP rate ({stats['false_positives']}/{stats['total_outcomes']} outcomes)"
                if manager.exclude_pattern(risk.regex, reason, risk.risk_score, risk.risk_level.value):
                    excluded += 1
                    print(f"  ✅ Excluded: {risk.regex[:40]}...")
            print(f"\n✅ Excluded {excluded}/{len(to_prune)} patterns")

    elif cmd == "config":
        # Show configuration
        section = sys.argv[2] if len(sys.argv) > 2 else ""
        _show_config_section(manager, section)

    else:
        print(f"Unknown command: {cmd}")
        print("Commands:")
        print("  Pattern Management:")
        print("    menu          - Interactive menu")
        print("    list          - List all patterns")
        print("    discover      - Discover new patterns from work log")
        print("    test <text>   - Test text against patterns")
        print("    add           - Add a new pattern")
        print("    export        - Export patterns to JSON")
        print("    stats         - Show statistics")
        print("")
        print("  Risk Analysis:")
        print("    analyze       - Analyze pattern health/risks")
        print("    exclude       - Exclude a pattern")
        print("    exclusions    - View excluded patterns")
        print("    restore       - Restore an excluded pattern")
        print("")
        print("  Experience Tracking:")
        print("    outcome       - Record pattern outcome")
        print("    experience    - View experience summary")
        print("    worst         - View worst-performing patterns")
        print("    auto-prune    - Auto-exclude bad performers (use --dry-run first)")
        print("")
        print("  Configuration:")
        print("    config [1-4]  - View/edit configuration")
        sys.exit(1)
