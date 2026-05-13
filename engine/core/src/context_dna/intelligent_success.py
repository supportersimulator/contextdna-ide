#!/usr/bin/env python3
"""
INTELLIGENT SUCCESS DETECTION - Beyond Keywords to True Understanding

The problem with keyword detection:
- "success" in error message: "Error: success handler not found" → FALSE POSITIVE
- Novel success: "The patient responded to treatment" → MISSED
- Context matters: "yes" alone vs "yes, but there's a problem" → DIFFERENT
- Domain-specific: "green build" in CI, "200" in API, "healthy" in docker → VARIES

THE SOLUTION: Multi-layer intelligent detection with evolving patterns

ARCHITECTURE:
┌─────────────────────────────────────────────────────────────────────────┐
│                    INTELLIGENT SUCCESS DETECTION                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Layer 1: STRUCTURAL ANALYSIS (Fast, No LLM)                            │
│  ├── Exit codes, HTTP status, health checks                             │
│  ├── Git commit hashes, file creation confirmations                     │
│  ├── Test pass/fail counts, deployment status                           │
│  └── These are IRREFUTABLE - system-level truth                         │
│                                                                          │
│  Layer 2: CONTEXTUAL KEYWORD ANALYSIS (Fast, No LLM)                    │
│  ├── Keywords WITH surrounding context                                  │
│  ├── Negation detection ("not success", "success failed")               │
│  ├── Sequence analysis (success → error = not success)                  │
│  └── Confidence scoring based on context quality                        │
│                                                                          │
│  Layer 3: LLM SEMANTIC ANALYSIS (Accurate, Uses LLM)                    │
│  ├── Understands: "that did the trick" = success                        │
│  ├── Understands: "we're cooking now" = success                         │
│  ├── Understands domain-specific success patterns                       │
│  ├── Extracts: WHAT succeeded, HOW it succeeded, WHY it matters         │
│  └── Returns structured success object                                  │
│                                                                          │
│  Layer 4: EVOLVING PATTERN REGISTRY                                     │
│  ├── Learns new success patterns from LLM confirmations                 │
│  ├── Promotes frequent patterns to Layer 2 (faster detection)           │
│  ├── Domain-specific pattern libraries (DevOps, API, Frontend)          │
│  └── Self-improving: patterns get confidence scores over time           │
│                                                                          │
│  Layer 5: TEMPORAL SUCCESS VALIDATION                                   │
│  ├── Success must persist (no immediate rollback/fix)                   │
│  ├── Window-based validation (5 min, 1 hour, 1 day)                    │
│  ├── State change detection (before → after comparison)                 │
│  └── Confidence increases with time without reversal                    │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘

EVOLVING SUCCESS DEFINITION:
- We don't hardcode what success looks like
- We learn from every confirmed success
- We detect patterns we couldn't anticipate
- The system gets smarter with every interaction

Usage:
    from context_dna.intelligent_success import IntelligentSuccessDetector

    detector = IntelligentSuccessDetector()

    # Analyze work stream
    results = detector.analyze(work_entries)

    # Get detected successes with confidence
    for success in results.successes:
        print(f"{success.description} (confidence: {success.confidence})")
        print(f"  Evidence: {success.evidence}")
        print(f"  Pattern: {success.pattern_id}")  # For learning
"""

import re
import json
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field, asdict
from pathlib import Path
from enum import Enum


# =============================================================================
# SUCCESS TYPES AND STRUCTURES
# =============================================================================

class SuccessType(Enum):
    """Types of objective success."""
    STRUCTURAL = "structural"      # Exit codes, HTTP status, etc.
    USER_CONFIRMED = "user"        # User explicitly confirmed
    SYSTEM_CONFIRMED = "system"    # System output indicates success
    LLM_DETECTED = "llm"          # LLM understood semantic success
    PATTERN_MATCHED = "pattern"    # Matched learned pattern
    TEMPORAL_VALIDATED = "temporal"  # Validated by time/persistence


class SuccessCategory(Enum):
    """Categories of work that succeeded."""
    DEPLOYMENT = "deployment"
    CODE_CHANGE = "code"
    CONFIGURATION = "config"
    DEBUGGING = "debug"
    FEATURE = "feature"
    INFRASTRUCTURE = "infra"
    DOCUMENTATION = "docs"
    TESTING = "testing"
    GENERAL = "general"


@dataclass
class DetectedSuccess:
    """A detected success with full context."""
    description: str
    confidence: float  # 0.0 to 1.0
    success_type: SuccessType
    category: SuccessCategory
    evidence: List[str]
    timestamp: str
    pattern_id: Optional[str] = None  # For pattern learning
    raw_content: Optional[str] = None
    metadata: Dict = field(default_factory=dict)

    # Temporal validation
    validated: bool = False
    validation_window: Optional[str] = None  # "5min", "1hour", etc.

    def to_dict(self) -> dict:
        result = asdict(self)
        result['success_type'] = self.success_type.value
        result['category'] = self.category.value
        return result

    def get_pattern_signature(self) -> str:
        """Generate a signature for pattern learning."""
        # Normalize and hash the evidence patterns
        normalized = sorted([e.lower().strip() for e in self.evidence])
        content = "|".join(normalized)
        return hashlib.md5(content.encode()).hexdigest()[:12]


@dataclass
class SuccessPattern:
    """A learned success pattern."""
    pattern_id: str
    pattern_type: str  # "regex", "semantic", "structural"
    pattern_data: Dict  # Pattern-specific data
    confidence_boost: float  # How much to boost confidence when matched
    hit_count: int = 0
    last_hit: Optional[str] = None
    false_positive_count: int = 0
    category: SuccessCategory = SuccessCategory.GENERAL

    def to_dict(self) -> dict:
        result = asdict(self)
        result['category'] = self.category.value
        return result


# =============================================================================
# LAYER 1: STRUCTURAL ANALYSIS (Irrefutable Evidence)
# =============================================================================

class StructuralAnalyzer:
    """
    Analyzes structural/system-level success signals.
    These are IRREFUTABLE - they're facts from the system.
    """

    # Structural success patterns with confidence
    STRUCTURAL_PATTERNS = [
        # Exit codes
        (r'exit[_ ]?code[:\s]*0\b', 0.95, 'exit_zero', SuccessCategory.GENERAL),
        (r'exited with code 0', 0.95, 'exit_zero', SuccessCategory.GENERAL),
        (r'return[ed]?\s+0\b', 0.7, 'return_zero', SuccessCategory.CODE_CHANGE),

        # HTTP status
        (r'\b200\s*OK\b', 0.9, 'http_200', SuccessCategory.GENERAL),
        (r'\b201\s*Created\b', 0.9, 'http_201', SuccessCategory.GENERAL),
        (r'\b204\s*No Content\b', 0.85, 'http_204', SuccessCategory.GENERAL),
        (r'HTTP/[\d.]+ 2\d{2}', 0.9, 'http_2xx', SuccessCategory.GENERAL),

        # Container/Service health
        (r'\bhealthy\b', 0.9, 'healthy', SuccessCategory.INFRASTRUCTURE),
        (r'\bactive\s*\(running\)', 0.9, 'service_running', SuccessCategory.INFRASTRUCTURE),
        (r'container.*started', 0.85, 'container_started', SuccessCategory.INFRASTRUCTURE),
        (r'all\s+\d+\s+containers?\s+(are\s+)?healthy', 0.95, 'all_healthy', SuccessCategory.INFRASTRUCTURE),

        # Git operations
        (r'\[[\w-]+\s+[\da-f]{7,}\]', 0.9, 'git_commit', SuccessCategory.CODE_CHANGE),
        (r'Successfully rebased', 0.9, 'git_rebase', SuccessCategory.CODE_CHANGE),
        (r'Already up to date', 0.8, 'git_uptodate', SuccessCategory.CODE_CHANGE),
        (r'pushed to .+/\w+', 0.9, 'git_push', SuccessCategory.CODE_CHANGE),

        # Test results
        (r'\d+\s+passed', 0.85, 'tests_passed', SuccessCategory.TESTING),
        (r'All tests passed', 0.95, 'all_tests_passed', SuccessCategory.TESTING),
        (r'✓\s*\d+\s+tests?', 0.85, 'tests_checkmark', SuccessCategory.TESTING),
        (r'0 failed', 0.8, 'zero_failed', SuccessCategory.TESTING),

        # Build/Deploy
        (r'Build succeeded', 0.95, 'build_succeeded', SuccessCategory.DEPLOYMENT),
        (r'Deployment.*complete', 0.9, 'deploy_complete', SuccessCategory.DEPLOYMENT),
        (r'Successfully deployed', 0.95, 'deploy_success', SuccessCategory.DEPLOYMENT),
        (r'terraform apply.*complete', 0.9, 'terraform_complete', SuccessCategory.INFRASTRUCTURE),

        # File operations
        (r'File created successfully', 0.9, 'file_created', SuccessCategory.CODE_CHANGE),
        (r'Successfully wrote', 0.9, 'file_written', SuccessCategory.CODE_CHANGE),
        (r'Saved to', 0.8, 'file_saved', SuccessCategory.CODE_CHANGE),

        # Memory system specific
        (r'Recorded SOP:', 0.95, 'sop_recorded', SuccessCategory.GENERAL),
        (r'Agent success recorded', 0.95, 'success_recorded', SuccessCategory.GENERAL),
        (r'SOP extraction triggered', 0.9, 'sop_triggered', SuccessCategory.GENERAL),
        (r'Win captured', 0.95, 'win_captured', SuccessCategory.GENERAL),
    ]

    # Structural failure patterns (reduce confidence)
    STRUCTURAL_FAILURES = [
        (r'exit[_ ]?code[:\s]*[1-9]\d*', 'nonzero_exit'),
        (r'\b[45]\d{2}\b.*error', 'http_error'),
        (r'\bunhealthy\b', 'unhealthy'),
        (r'\bfailed\b', 'failed'),
        (r'\berror\b(?!.*handler)', 'error'),  # "error" but not "error handler"
        (r'\bexception\b', 'exception'),
        (r'\btimeout\b', 'timeout'),
        (r'\bdenied\b', 'denied'),
        (r'\d+\s+failed', 'tests_failed'),
    ]

    def analyze(self, content: str) -> List[Tuple[float, str, SuccessCategory]]:
        """
        Analyze content for structural success signals.

        Returns list of (confidence, evidence, category) tuples.
        """
        results = []
        content_lower = content.lower()

        # Check for structural successes
        for pattern, confidence, evidence, category in self.STRUCTURAL_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                # Check if there's a failure signal nearby that invalidates this
                if not self._has_nearby_failure(content, pattern):
                    results.append((confidence, evidence, category))

        return results

    def _has_nearby_failure(self, content: str, success_pattern: str) -> bool:
        """Check if failure signal appears near the success signal."""
        # Find success match position
        match = re.search(success_pattern, content, re.IGNORECASE)
        if not match:
            return False

        # Check 200 chars around the match for failure signals
        start = max(0, match.start() - 100)
        end = min(len(content), match.end() + 100)
        context = content[start:end]

        for failure_pattern, _ in self.STRUCTURAL_FAILURES:
            if re.search(failure_pattern, context, re.IGNORECASE):
                return True

        return False


# =============================================================================
# LAYER 2: CONTEXTUAL KEYWORD ANALYSIS
# =============================================================================

class ContextualKeywordAnalyzer:
    """
    Analyzes keywords WITH context - not just presence, but meaning.
    """

    # Positive signals with context requirements
    CONTEXTUAL_PATTERNS = [
        # User confirmations - need to be standalone or at sentence start
        {
            'pattern': r'(?:^|\.\s*)(?:that\s+)?worked',
            'confidence': 0.9,
            'evidence': 'worked_confirmation',
            'negations': ['didn\'t work', 'not work', 'won\'t work'],
        },
        {
            'pattern': r'(?:^|\.\s*)perfect',
            'confidence': 0.85,
            'evidence': 'perfect_confirmation',
            'negations': ['not perfect', 'isn\'t perfect'],
        },
        {
            'pattern': r'(?:^|\.\s*)(?:that\'s\s+)?(?:it|exactly)',
            'confidence': 0.8,
            'evidence': 'exact_confirmation',
            'negations': ['that\'s not it', 'not exactly'],
        },
        {
            'pattern': r'(?:^|\.\s*)(?:yes|yep|yeah)(?:\s*[!.]|$)',
            'confidence': 0.7,
            'evidence': 'affirmative',
            'negations': ['yes but', 'yes however', 'yes, but'],
        },
        {
            'pattern': r'(?:^|\.\s*)(?:nice|great|awesome|excellent)',
            'confidence': 0.75,
            'evidence': 'positive_exclamation',
            'negations': ['not nice', 'not great', 'not awesome'],
        },
        # Technical success phrases
        {
            'pattern': r'(?:is|are)\s+(?:now\s+)?(?:working|running|live|up)',
            'confidence': 0.8,
            'evidence': 'operational_status',
            'negations': ['not working', 'not running', 'isn\'t working'],
        },
        {
            'pattern': r'(?:all|everything)\s+(?:looks?\s+)?good',
            'confidence': 0.8,
            'evidence': 'all_good',
            'negations': ['not all good', 'doesn\'t look good'],
        },
        # Completion signals
        {
            'pattern': r'done(?:\s*[!.]|$)',
            'confidence': 0.6,  # Lower - "done" alone is weak
            'evidence': 'done_signal',
            'negations': ['not done', 'isn\'t done', 'when done'],
        },
        {
            'pattern': r'complete[d]?(?:\s+successfully)?',
            'confidence': 0.75,
            'evidence': 'completed',
            'negations': ['not complete', 'incomplete'],
        },
    ]

    def analyze(self, content: str, source: str = "system") -> List[Tuple[float, str]]:
        """
        Analyze content with context awareness.

        Args:
            content: Text to analyze
            source: 'user', 'agent', or 'system'

        Returns list of (confidence, evidence) tuples.
        """
        results = []
        content_lower = content.lower()

        for pattern_info in self.CONTEXTUAL_PATTERNS:
            pattern = pattern_info['pattern']
            confidence = pattern_info['confidence']
            evidence = pattern_info['evidence']
            negations = pattern_info.get('negations', [])

            # Check if pattern matches
            if re.search(pattern, content_lower):
                # Check for negations
                negated = False
                for neg in negations:
                    if neg in content_lower:
                        negated = True
                        break

                if not negated:
                    # Boost confidence if from user (vs system/agent)
                    if source == 'user':
                        confidence = min(1.0, confidence + 0.1)

                    results.append((confidence, evidence))

        return results


# =============================================================================
# LAYER 3: LLM SEMANTIC ANALYSIS
# =============================================================================

class LLMSemanticAnalyzer:
    """
    Uses LLM to understand semantic success that keywords can't catch.

    Examples this catches:
    - "that did the trick"
    - "we're cooking now"
    - "ship it"
    - "money"
    - Domain-specific: "the patient recovered"
    """

    ANALYSIS_PROMPT = """Analyze this work stream excerpt for OBJECTIVE SUCCESS signals.

OBJECTIVE SUCCESS means:
1. A task was completed successfully (not just attempted)
2. There's evidence it worked (output, confirmation, state change)
3. No immediate reversal or fix was needed

Work Stream:
```
{content}
```

Respond with JSON:
{{
  "is_success": true/false,
  "confidence": 0.0-1.0,
  "description": "What succeeded (if anything)",
  "evidence": ["list", "of", "evidence"],
  "category": "deployment|code|config|debug|feature|infra|docs|testing|general",
  "novel_pattern": "If this represents a new success pattern not in common lists, describe it"
}}

If no success is detected, return {{"is_success": false, "confidence": 0.0}}"""

    def __init__(self, llm_provider=None):
        """
        Initialize with optional LLM provider.

        If no provider, falls back to local analysis.
        """
        self.llm_provider = llm_provider

    def analyze(self, content: str, context: Optional[str] = None) -> Optional[Dict]:
        """
        Analyze content using LLM for semantic understanding.

        Args:
            content: The content to analyze
            context: Optional surrounding context

        Returns:
            Dict with analysis results or None if LLM unavailable
        """
        if not self.llm_provider:
            return None

        # Build prompt
        analysis_content = content
        if context:
            analysis_content = f"Context: {context}\n\nContent: {content}"

        prompt = self.ANALYSIS_PROMPT.format(content=analysis_content[:2000])

        try:
            response = self.llm_provider.generate(prompt)
            return json.loads(response)
        except Exception as e:
            return None

    def extract_success_description(self, content: str) -> Optional[str]:
        """
        Use LLM to extract a clean success description.

        Turns: "yeah that docker thing worked perfectly now the containers are healthy"
        Into: "Docker containers configured and running healthy"
        """
        if not self.llm_provider:
            return None

        prompt = f"""Extract a concise, professional description of what succeeded.

Content: "{content}"

Return just the description (1 sentence, no quotes):"""

        try:
            return self.llm_provider.generate(prompt).strip()
        except:
            return None


# =============================================================================
# LAYER 4: EVOLVING PATTERN REGISTRY
# =============================================================================

class EvolvingPatternRegistry:
    """
    Learns and stores success patterns that evolve over time.

    - New patterns start with low confidence
    - Confirmed patterns get boosted
    - Patterns can be domain-specific
    - Promotes frequent patterns to fast-path detection
    """

    def __init__(self, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or Path.home() / ".context-dna" / "patterns.json"
        self.patterns: Dict[str, SuccessPattern] = {}
        self._load_patterns()

    def _load_patterns(self):
        """Load patterns from storage."""
        if self.storage_path.exists():
            try:
                data = json.loads(self.storage_path.read_text())
                for pid, pdata in data.items():
                    pdata['category'] = SuccessCategory(pdata.get('category', 'general'))
                    self.patterns[pid] = SuccessPattern(**pdata)
            except Exception:
                pass

    def _save_patterns(self):
        """Save patterns to storage."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        data = {pid: p.to_dict() for pid, p in self.patterns.items()}
        self.storage_path.write_text(json.dumps(data, indent=2))

    def register_pattern(self, success: DetectedSuccess) -> str:
        """
        Register a new success pattern or update existing.

        Returns pattern_id.
        """
        pattern_id = success.get_pattern_signature()

        if pattern_id in self.patterns:
            # Update existing pattern
            pattern = self.patterns[pattern_id]
            pattern.hit_count += 1
            pattern.last_hit = datetime.now().isoformat()
            # Boost confidence slightly with each hit
            pattern.confidence_boost = min(0.3, pattern.confidence_boost + 0.02)
        else:
            # Create new pattern
            pattern = SuccessPattern(
                pattern_id=pattern_id,
                pattern_type="learned",
                pattern_data={
                    "evidence": success.evidence,
                    "description": success.description,
                    "example": success.raw_content[:200] if success.raw_content else None,
                },
                confidence_boost=0.1,  # Start low
                hit_count=1,
                last_hit=datetime.now().isoformat(),
                category=success.category,
            )
            self.patterns[pattern_id] = pattern

        self._save_patterns()
        return pattern_id

    def mark_false_positive(self, pattern_id: str):
        """Mark a pattern as false positive (reduce confidence)."""
        if pattern_id in self.patterns:
            pattern = self.patterns[pattern_id]
            pattern.false_positive_count += 1
            pattern.confidence_boost = max(0, pattern.confidence_boost - 0.1)
            self._save_patterns()

    def get_high_confidence_patterns(self, min_hits: int = 3) -> List[SuccessPattern]:
        """Get patterns with high confidence for fast-path detection."""
        return [
            p for p in self.patterns.values()
            if p.hit_count >= min_hits and p.confidence_boost > 0.1
        ]

    def match_pattern(self, content: str, evidence: List[str]) -> Optional[Tuple[str, float]]:
        """
        Check if content matches any learned pattern.

        Returns (pattern_id, confidence_boost) or None.
        """
        # Generate signature for this content
        normalized = sorted([e.lower().strip() for e in evidence])
        test_sig = hashlib.md5("|".join(normalized).encode()).hexdigest()[:12]

        if test_sig in self.patterns:
            pattern = self.patterns[test_sig]
            return (pattern.pattern_id, pattern.confidence_boost)

        return None


# =============================================================================
# LAYER 5: TEMPORAL VALIDATION
# =============================================================================

class TemporalValidator:
    """
    Validates success by checking if it persists over time.

    A true success should:
    - Not be immediately followed by a fix/retry
    - Not be rolled back
    - State should persist
    """

    # Reversal indicators
    REVERSAL_PATTERNS = [
        r'\btry(?:ing)?\s+again\b',
        r'\blet\s+me\s+fix\b',
        r'\bretry\b',
        r'\brollback\b',
        r'\brevert\b',
        r'\bundo\b',
        r'\bactually[,]?\s+(?:no|wait|that)',
        r'\bstill\s+(?:not|broken|failing)',
        r'\bsame\s+error\b',
    ]

    def __init__(self):
        self.pending_validations: Dict[str, Dict] = {}

    def queue_for_validation(self, success: DetectedSuccess, window: str = "5min"):
        """
        Queue a success for temporal validation.

        Args:
            success: The detected success
            window: Validation window ("5min", "1hour", "1day")
        """
        key = success.get_pattern_signature()
        self.pending_validations[key] = {
            "success": success,
            "queued_at": datetime.now().isoformat(),
            "window": window,
            "entries_after": [],  # Will collect entries after this success
        }

    def check_for_reversal(self, content: str) -> List[str]:
        """
        Check if content indicates reversal of previous success.

        Returns list of pattern signatures that should be invalidated.
        """
        invalidated = []
        content_lower = content.lower()

        for pattern in self.REVERSAL_PATTERNS:
            if re.search(pattern, content_lower):
                # Invalidate recent pending validations
                for key in list(self.pending_validations.keys()):
                    invalidated.append(key)
                break

        return invalidated

    def validate_pending(self) -> List[DetectedSuccess]:
        """
        Check and validate any pending successes whose window has elapsed.

        Returns list of validated successes.
        """
        validated = []
        now = datetime.now()

        for key, data in list(self.pending_validations.items()):
            queued = datetime.fromisoformat(data["queued_at"])
            window = data["window"]

            # Calculate window duration
            if window == "5min":
                delta = timedelta(minutes=5)
            elif window == "1hour":
                delta = timedelta(hours=1)
            elif window == "1day":
                delta = timedelta(days=1)
            else:
                delta = timedelta(minutes=5)

            # Check if window has elapsed
            if now - queued >= delta:
                success = data["success"]
                success.validated = True
                success.validation_window = window
                # Boost confidence for validated successes
                success.confidence = min(1.0, success.confidence + 0.1)
                validated.append(success)
                del self.pending_validations[key]

        return validated


# =============================================================================
# MAIN INTELLIGENT DETECTOR
# =============================================================================

class IntelligentSuccessDetector:
    """
    Multi-layer intelligent success detection.

    Combines all layers for accurate, evolving success detection.
    """

    def __init__(self, llm_provider=None, storage_path: Optional[Path] = None):
        self.structural = StructuralAnalyzer()
        self.contextual = ContextualKeywordAnalyzer()
        self.semantic = LLMSemanticAnalyzer(llm_provider)
        self.patterns = EvolvingPatternRegistry(storage_path)
        self.temporal = TemporalValidator()

        self.detected_successes: List[DetectedSuccess] = []

    def analyze(self, entries: List[Dict]) -> 'AnalysisResult':
        """
        Analyze a sequence of work entries for successes.

        Args:
            entries: List of work log entries with 'content', 'source', 'timestamp'

        Returns:
            AnalysisResult with detected successes and metrics
        """
        self.detected_successes = []

        for i, entry in enumerate(entries):
            content = entry.get('content', '')
            source = entry.get('source', 'system')
            timestamp = entry.get('timestamp', datetime.now().isoformat())

            # Check for reversal of pending successes
            invalidated = self.temporal.check_for_reversal(content)
            for key in invalidated:
                if key in self.temporal.pending_validations:
                    del self.temporal.pending_validations[key]

            # Layer 1: Structural analysis
            structural_results = self.structural.analyze(content)

            # Layer 2: Contextual keyword analysis
            contextual_results = self.contextual.analyze(content, source)

            # Combine evidence
            all_evidence = []
            max_confidence = 0.0
            category = SuccessCategory.GENERAL

            for conf, evidence, cat in structural_results:
                all_evidence.append(evidence)
                max_confidence = max(max_confidence, conf)
                category = cat

            for conf, evidence in contextual_results:
                all_evidence.append(evidence)
                max_confidence = max(max_confidence, conf)

            # If we have evidence, check patterns and potentially use LLM
            if all_evidence and max_confidence >= 0.5:
                # Layer 4: Check learned patterns
                pattern_match = self.patterns.match_pattern(content, all_evidence)
                if pattern_match:
                    pattern_id, boost = pattern_match
                    max_confidence = min(1.0, max_confidence + boost)
                else:
                    pattern_id = None

                # Layer 3: LLM analysis for semantic understanding (if available)
                llm_result = None
                if self.semantic.llm_provider and max_confidence < 0.8:
                    llm_result = self.semantic.analyze(content)
                    if llm_result and llm_result.get('is_success'):
                        max_confidence = max(max_confidence, llm_result.get('confidence', 0))

                # Create detected success
                description = (
                    llm_result.get('description') if llm_result
                    else self._extract_description(content, all_evidence)
                )

                success = DetectedSuccess(
                    description=description,
                    confidence=max_confidence,
                    success_type=self._determine_type(structural_results, contextual_results, source),
                    category=category,
                    evidence=all_evidence,
                    timestamp=timestamp,
                    pattern_id=pattern_id,
                    raw_content=content[:500],
                )

                self.detected_successes.append(success)

                # Queue for temporal validation
                self.temporal.queue_for_validation(success)

        # Check for any successes that have passed temporal validation
        validated = self.temporal.validate_pending()

        return AnalysisResult(
            successes=self.detected_successes,
            validated=validated,
            pending_validation=len(self.temporal.pending_validations),
        )

    def _determine_type(self, structural, contextual, source) -> SuccessType:
        """Determine the primary success type."""
        if structural:
            return SuccessType.STRUCTURAL
        if source == 'user' and contextual:
            return SuccessType.USER_CONFIRMED
        if contextual:
            return SuccessType.SYSTEM_CONFIRMED
        return SuccessType.PATTERN_MATCHED

    def _extract_description(self, content: str, evidence: List[str]) -> str:
        """Extract a description from content and evidence."""
        # Try to get first meaningful line
        lines = content.strip().split('\n')
        for line in lines:
            line = line.strip()
            if len(line) > 10 and len(line) < 200:
                return line

        # Fall back to evidence summary
        return f"Success detected: {', '.join(evidence[:3])}"

    def learn_from_confirmation(self, success: DetectedSuccess):
        """
        Learn from a confirmed success (human or temporal validation).

        This updates the pattern registry to improve future detection.
        """
        self.patterns.register_pattern(success)

    def report_false_positive(self, success: DetectedSuccess):
        """Report that a detected success was actually not a success."""
        if success.pattern_id:
            self.patterns.mark_false_positive(success.pattern_id)

    def get_uncaptured_wins(self, captured_descriptions: List[str]) -> List[DetectedSuccess]:
        """
        Compare detected successes against actually captured wins.

        Returns successes that were detected but not captured.
        """
        uncaptured = []
        captured_lower = [d.lower() for d in captured_descriptions]

        for success in self.detected_successes:
            # Check if this success was captured
            captured = False
            desc_lower = success.description.lower()
            for cap in captured_lower:
                # Fuzzy match - check if key words overlap
                if self._descriptions_match(desc_lower, cap):
                    captured = True
                    break

            if not captured:
                uncaptured.append(success)

        return uncaptured

    def _descriptions_match(self, desc1: str, desc2: str) -> bool:
        """Check if two descriptions likely refer to the same success."""
        # Simple word overlap check
        words1 = set(desc1.split())
        words2 = set(desc2.split())
        common = words1 & words2

        # If more than 40% of words overlap, consider it a match
        min_words = min(len(words1), len(words2))
        if min_words == 0:
            return False

        return len(common) / min_words > 0.4


@dataclass
class AnalysisResult:
    """Result of success analysis."""
    successes: List[DetectedSuccess]
    validated: List[DetectedSuccess]  # Temporally validated
    pending_validation: int

    @property
    def high_confidence(self) -> List[DetectedSuccess]:
        """Get successes with confidence >= 0.8."""
        return [s for s in self.successes if s.confidence >= 0.8]

    @property
    def should_auto_capture(self) -> List[DetectedSuccess]:
        """Get successes that should be auto-captured (high confidence + structural)."""
        return [
            s for s in self.successes
            if s.confidence >= 0.85 and s.success_type == SuccessType.STRUCTURAL
        ]


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Intelligent Success Detection - Context DNA")
        print("")
        print("Multi-layer success detection that learns and evolves.")
        print("")
        print("Commands:")
        print("  analyze <text>    - Analyze text for success signals")
        print("  patterns          - Show learned patterns")
        print("  test              - Run test analysis")
        print("")
        sys.exit(0)

    cmd = sys.argv[1]
    detector = IntelligentSuccessDetector()

    if cmd == "analyze":
        text = " ".join(sys.argv[2:])
        entries = [{"content": text, "source": "user", "timestamp": datetime.now().isoformat()}]
        result = detector.analyze(entries)

        if result.successes:
            for s in result.successes:
                bar = "█" * int(s.confidence * 10) + "░" * (10 - int(s.confidence * 10))
                print(f"[{bar}] {s.confidence:.0%} - {s.success_type.value}")
                print(f"  {s.description}")
                print(f"  Evidence: {', '.join(s.evidence)}")
                print(f"  Category: {s.category.value}")
        else:
            print("No success signals detected")

    elif cmd == "patterns":
        patterns = detector.patterns.get_high_confidence_patterns(min_hits=1)
        if patterns:
            print(f"Learned patterns ({len(patterns)}):")
            for p in sorted(patterns, key=lambda x: -x.hit_count):
                print(f"  [{p.pattern_id}] hits={p.hit_count} boost={p.confidence_boost:.1%}")
                print(f"    {p.pattern_data.get('description', 'No description')}")
        else:
            print("No learned patterns yet")

    elif cmd == "test":
        print("=== Testing Intelligent Success Detection ===\n")

        test_entries = [
            {"content": "Running docker-compose up -d", "source": "agent", "timestamp": "2024-01-15T10:00:00"},
            {"content": "Creating network... done\nStarting container_1... done\nAll 3 containers healthy", "source": "system", "timestamp": "2024-01-15T10:00:30"},
            {"content": "that did the trick! we're cooking now", "source": "user", "timestamp": "2024-01-15T10:01:00"},
        ]

        print("Input stream:")
        for e in test_entries:
            print(f"  [{e['source']}] {e['content'][:60]}...")

        print("\nAnalysis:")
        result = detector.analyze(test_entries)

        for s in result.successes:
            bar = "█" * int(s.confidence * 10) + "░" * (10 - int(s.confidence * 10))
            print(f"  [{bar}] {s.confidence:.0%} - {s.success_type.value}")
            print(f"    {s.description}")
            print(f"    Evidence: {', '.join(s.evidence)}")

        if result.should_auto_capture:
            print(f"\n  → {len(result.should_auto_capture)} success(es) should be auto-captured")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
