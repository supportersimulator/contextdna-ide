#!/usr/bin/env python3
"""
BOUNDARY INTELLIGENCE - Core Data Models and Orchestration

Synaptic's project boundary detection system that learns from feedback.

ARCHITECTURE:
┌─────────────────────────────────────────────────────────────────────────┐
│                    PROJECT BOUNDARY INTELLIGENCE                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  5 INPUT SIGNALS                                                        │
│  ├─ User Prompt ──────────► Keyword extraction                         │
│  ├─ Active File Path ────► Directory/project inference                 │
│  ├─ Hierarchy Profile ───► Workspace structure                         │
│  ├─ Recent Projects ─────► Redis recency cache                         │
│  └─ Mirrored Dialogue ───► Conversation context                        │
│         │                                                               │
│         ▼                                                               │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │              LLM BOUNDARY ANALYZER                               │   │
│  │   Semantic analysis → keyword→project associations               │   │
│  │   Confidence scoring → filtering decisions                       │   │
│  └──────────────────────────────────┬──────────────────────────────┘   │
│                                     │                                   │
│                                     ▼                                   │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │              CONFIDENCE THRESHOLDS                               │   │
│  │   ≥80% → Full filtering (high confidence)                        │   │
│  │   60-79% → Soft filter with note                                 │   │
│  │   40-59% → Clarification prompt                                  │   │
│  │   <40% → Broad context with uncertainty note                     │   │
│  └──────────────────────────────────┬──────────────────────────────┘   │
│                                     │                                   │
│                                     ▼                                   │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │              FEEDBACK LOOP (Celery background tasks)             │   │
│  │   • Records all boundary decisions                               │   │
│  │   • Learns keyword→project from corrections                      │   │
│  │   • Reinforcement: correct=strengthen, wrong=decay               │   │
│  │   • Signal weights: clarification(strong), helpful(medium)       │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘

STORAGE:
- Redis: Hot data (recent projects, session context, confidence cache)
- SQLite: Warm data (keyword associations, decision history)
- PostgreSQL: Cold data (long-term boundaries, cross-machine sync)

Usage:
    from memory.boundary_intelligence import (
        BoundaryIntelligence,
        BoundaryContext,
        BoundaryDecision,
        get_boundary_intelligence
    )

    bi = get_boundary_intelligence()

    # Analyze prompt for project boundaries
    context = BoundaryContext(
        user_prompt="fix async boto3 in voice stack",
        active_file_path="/Users/.../ersim-voice-stack/services/llm/main.py",
        hierarchy_profile=profile,
        session_id="sess_123"
    )

    decision = bi.analyze_and_decide(context)

    if decision.should_filter:
        filtered_learnings = bi.filter_learnings(learnings, decision)
"""

import json
import hashlib
import re
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
import logging

logger = logging.getLogger(__name__)

# =============================================================================
# ENUMS
# =============================================================================

class ConfidenceLevel(Enum):
    """Confidence levels for boundary decisions."""
    HIGH = "high"           # ≥80% → Full filtering
    MODERATE = "moderate"   # 60-79% → Soft filter with note
    LOW = "low"             # 40-59% → Clarification prompt
    UNCERTAIN = "uncertain" # <40% → Broad context with uncertainty note


class FilterAction(Enum):
    """What action to take based on confidence."""
    FULL_FILTER = "full_filter"         # Only show project-specific
    SOFT_FILTER = "soft_filter"         # Prioritize project, include some general
    CLARIFY = "clarify"                 # Ask user to clarify project context
    BROAD_CONTEXT = "broad_context"     # Show broad context with note


class FeedbackSignal(Enum):
    """Types of feedback signals and their weights."""
    CLARIFICATION_RESPONSE = "clarification_response"  # User explicitly clarified (strong)
    USER_CORRECTION = "user_correction"                # User said "wrong project" (strong)
    TASK_SUCCESS = "task_success"                      # Task completed (medium)
    TASK_FAILURE = "task_failure"                      # Task failed (medium)
    FILE_CONTEXT = "file_context"                      # Inferred from file (weak)
    SESSION_PATTERN = "session_pattern"                # Inferred from session (weak)


# Signal strength multipliers for learning
SIGNAL_WEIGHTS = {
    FeedbackSignal.CLARIFICATION_RESPONSE: 1.0,
    FeedbackSignal.USER_CORRECTION: 1.0,
    FeedbackSignal.TASK_SUCCESS: 0.6,
    FeedbackSignal.TASK_FAILURE: 0.5,
    FeedbackSignal.FILE_CONTEXT: 0.3,
    FeedbackSignal.SESSION_PATTERN: 0.2,
}


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class ProjectSignal:
    """A signal indicating a specific project."""
    project: str
    source: str          # "prompt", "file_path", "hierarchy", "recency", "dialogue"
    confidence: float    # 0.0-1.0
    keywords: List[str]  # Keywords that contributed to this signal
    weight: float = 1.0  # Source weight multiplier


@dataclass
class BoundaryContext:
    """
    All input signals for boundary analysis.

    Represents the 5 input signals plus metadata.
    """
    # Primary signals
    user_prompt: str
    active_file_path: Optional[str] = None
    hierarchy_profile: Optional[Dict] = None
    recent_projects: Optional[List[str]] = None  # From Redis
    mirrored_dialogue: Optional[str] = None      # Last N messages

    # Metadata
    session_id: str = ""
    injection_id: str = ""
    timestamp: str = ""

    # Derived (populated during analysis)
    extracted_keywords: List[str] = field(default_factory=list)
    detected_projects: List[ProjectSignal] = field(default_factory=list)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()
        if not self.injection_id:
            self.injection_id = f"bi_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(self.user_prompt.encode()).hexdigest()[:8]}"


@dataclass
class BoundaryDecision:
    """
    The decision made about project boundaries.

    Captures the analysis result and recommended action.
    """
    # Core decision
    primary_project: Optional[str]
    confidence: float
    confidence_level: ConfidenceLevel
    action: FilterAction

    # Details
    all_signals: List[ProjectSignal]
    keywords_analyzed: List[str]
    reasoning: str

    # Metadata
    injection_id: str = ""
    decision_id: str = ""
    timestamp: str = ""

    # Clarification (if needed)
    clarification_prompt: Optional[str] = None
    clarification_options: Optional[List[str]] = None

    # Notes for user
    filter_note: Optional[str] = None  # e.g., "Context filtered to ersim-voice-stack"

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()
        if not self.decision_id:
            self.decision_id = f"bd_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    @property
    def should_filter(self) -> bool:
        """Whether to filter learnings by project."""
        return self.action in (FilterAction.FULL_FILTER, FilterAction.SOFT_FILTER)

    @property
    def should_clarify(self) -> bool:
        """Whether to ask user for clarification."""
        return self.action == FilterAction.CLARIFY


@dataclass
class BoundaryFeedback:
    """
    Feedback about a boundary decision.

    Used for learning from corrections.
    """
    decision_id: str
    signal_type: FeedbackSignal
    was_correct: bool
    correct_project: Optional[str] = None  # If wrong, what should it have been?
    user_explicit: bool = False            # Did user explicitly provide this?
    confidence: float = 0.5
    context: str = ""
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class KeywordAssociation:
    """
    Learned association between a keyword and a project.

    Updated through feedback loop.
    """
    keyword: str
    project: str
    weight: float           # 0.0-1.0, adjusted through learning
    positive_signals: int   # Count of positive feedback
    negative_signals: int   # Count of negative feedback
    last_updated: str
    confidence_tier: str    # "emerging", "established", "confirmed"

    @property
    def net_signals(self) -> int:
        return self.positive_signals - self.negative_signals

    @property
    def total_signals(self) -> int:
        return self.positive_signals + self.negative_signals

    @property
    def positive_rate(self) -> float:
        if self.total_signals == 0:
            return 0.5
        return self.positive_signals / self.total_signals


# =============================================================================
# CONFIGURATION
# =============================================================================

# Confidence thresholds
CONFIDENCE_THRESHOLDS = {
    ConfidenceLevel.HIGH: 0.80,
    ConfidenceLevel.MODERATE: 0.60,
    ConfidenceLevel.LOW: 0.40,
    ConfidenceLevel.UNCERTAIN: 0.0,
}

# Actions for each confidence level
CONFIDENCE_ACTIONS = {
    ConfidenceLevel.HIGH: FilterAction.FULL_FILTER,
    ConfidenceLevel.MODERATE: FilterAction.SOFT_FILTER,
    ConfidenceLevel.LOW: FilterAction.CLARIFY,
    ConfidenceLevel.UNCERTAIN: FilterAction.BROAD_CONTEXT,
}

# Source weight multipliers (how much to trust each signal source)
SOURCE_WEIGHTS = {
    "file_path": 1.0,      # File path is most reliable
    "llm": 0.9,            # LLM semantic analysis is strong when confident
    "prompt": 0.8,         # Prompt keywords are strong
    "hierarchy": 0.7,      # Hierarchy profile is structural
    "recency": 0.6,        # Recent projects have bias
    "dialogue": 0.5,       # Dialogue context is implicit
    "cwd": 0.6,            # CWD fallback when no other signals
}

# Keywords that are too general to indicate a project
GENERAL_KEYWORDS = {
    "fix", "bug", "error", "add", "update", "refactor", "test", "check",
    "deploy", "config", "env", "async", "sync", "performance", "issue",
    "problem", "help", "please", "need", "want", "how", "what", "why",
    "function", "class", "method", "variable", "import", "export",
    "file", "folder", "directory", "path", "code", "script"
}

# Minimum signals before considering an association "established"
CONFIDENCE_TIERS = {
    "emerging": 1,
    "established": 10,
    "confirmed": 30,
}

# Redis keys
REDIS_KEYS = {
    "recent_projects": "contextdna:boundary:recent_projects:{session_id}",
    "session_context": "contextdna:boundary:session:{session_id}",
    "keyword_cache": "contextdna:boundary:keywords:{hash}",
    "decision_cache": "contextdna:boundary:decision:{injection_id}",
}

# Redis TTLs (in seconds)
REDIS_TTLS = {
    "recent_projects": 3600,      # 1 hour
    "session_context": 7200,      # 2 hours
    "keyword_cache": 300,         # 5 minutes (hot cache)
    "decision_cache": 86400,      # 24 hours
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def extract_keywords(text: str, min_length: int = 3) -> List[str]:
    """
    Extract meaningful keywords from text.

    Filters out general/common words and returns unique keywords.
    """
    # Extract words
    words = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', text.lower())

    # Filter
    keywords = []
    seen = set()
    for word in words:
        if (len(word) >= min_length and
            word not in GENERAL_KEYWORDS and
            word not in seen):
            keywords.append(word)
            seen.add(word)

    return keywords


def infer_project_from_path(file_path: str, hierarchy: Optional[Dict] = None) -> Optional[str]:
    """
    Infer the project name from a file path.

    Uses hierarchy profile if available, otherwise infers from path structure.
    """
    if not file_path:
        return None

    path = Path(file_path)
    path_str = str(path).lower()

    # Check against hierarchy profile projects
    if hierarchy and "projects" in hierarchy:
        for project in hierarchy.get("projects", []):
            project_path = project.get("path", "")
            if project_path and project_path in str(path):
                return project.get("name", project_path)

    # Known top-level project directories (these ARE the projects)
    TOP_LEVEL_PROJECTS = {
        "backend", "frontend", "memory", "infra", "scripts",
        "context-dna", "simulator-core", "landing-page",
        "admin.ersimulator.com", "admin.contextdna.io", "sim-frontend",
    }

    # Patterns that indicate a project by suffix
    PROJECT_SUFFIXES = ["-stack", "-service", "-app", "-core"]

    # Get path parts for analysis
    parts = path.parts

    # Strategy 1: Look for known top-level projects
    for part in parts:
        part_lower = part.lower()
        if part_lower in TOP_LEVEL_PROJECTS or part in TOP_LEVEL_PROJECTS:
            return part

    # Strategy 2: Look for project-indicating suffixes
    for part in parts:
        if any(part.lower().endswith(suffix) for suffix in PROJECT_SUFFIXES):
            return part

    # Strategy 3: Check for nested project structures (e.g., services/llm, packages/ui)
    CONTAINER_DIRS = {"services", "packages", "apps", "modules", "libs"}
    for i, part in enumerate(parts):
        if part.lower() in CONTAINER_DIRS and i + 1 < len(parts):
            # Return the parent project + service name
            # e.g., ersim-voice-stack/services/llm -> ersim-voice-stack
            # First, look back for a project name
            for j in range(i - 1, -1, -1):
                prev_part = parts[j]
                if any(prev_part.lower().endswith(suffix) for suffix in PROJECT_SUFFIXES):
                    return prev_part
                if prev_part.lower() in TOP_LEVEL_PROJECTS or prev_part in TOP_LEVEL_PROJECTS:
                    return prev_part

    # Strategy 4: For paths in superrepo, find the project root
    # Look for common superrepo markers and extract the project after them
    SUPERREPO_MARKERS = ["er-simulator-superrepo", "superrepo", "monorepo"]
    for i, part in enumerate(parts):
        if part.lower() in SUPERREPO_MARKERS and i + 1 < len(parts):
            # The next part is likely the project
            next_part = parts[i + 1]
            # Skip if it's a common non-project directory
            if next_part.lower() not in {"src", "lib", "dist", "build", "node_modules", ".git"}:
                return next_part

    # Fall back to parent directory of file (but not if it's src/lib/app)
    SKIP_DIRS = {"src", "lib", "app", "dist", "build", "components", "utils", "helpers"}
    if len(parts) >= 2:
        parent = parts[-2]
        if parent.lower() not in SKIP_DIRS:
            return parent
        # Go one more level up
        if len(parts) >= 3 and parts[-3].lower() not in SKIP_DIRS:
            return parts[-3]

    return None


def confidence_to_level(confidence: float) -> ConfidenceLevel:
    """Convert a confidence score to a confidence level."""
    if confidence >= CONFIDENCE_THRESHOLDS[ConfidenceLevel.HIGH]:
        return ConfidenceLevel.HIGH
    elif confidence >= CONFIDENCE_THRESHOLDS[ConfidenceLevel.MODERATE]:
        return ConfidenceLevel.MODERATE
    elif confidence >= CONFIDENCE_THRESHOLDS[ConfidenceLevel.LOW]:
        return ConfidenceLevel.LOW
    else:
        return ConfidenceLevel.UNCERTAIN


def generate_clarification(projects: List[ProjectSignal], prompt: str) -> Tuple[str, List[str]]:
    """
    Generate a clarification prompt and options.

    Called when confidence is low and we need user input.
    """
    project_names = list(set(p.project for p in projects))

    if len(project_names) <= 1:
        return None, None

    # Build clarification message
    clarification = "I detected multiple possible project contexts. Which project is this for?"

    options = project_names[:4]  # Max 4 options
    if "general" not in options:
        options.append("general")  # Always offer general context option

    return clarification, options


# =============================================================================
# HIERARCHY PROFILE RETRIEVAL
# =============================================================================

def get_cached_hierarchy(project_path: str = None) -> Optional[Dict]:
    """
    Get the cached hierarchy profile for boundary context.

    Retrieves from:
    1. Redis cache (hot, fast)
    2. PostgreSQL (if Redis unavailable)
    3. Celery task queue (trigger fresh scan if stale)

    Args:
        project_path: Optional path to project root. Defaults to current working directory.

    Returns:
        Hierarchy profile dict or None if unavailable
    """
    import os
    project_path = project_path or os.getcwd()

    # Try Redis first
    try:
        from memory.redis_cache import get_redis_client
        redis_client = get_redis_client()
        if redis_client:
            # Check for cached hierarchy event
            cached = redis_client.get("contextdna:hierarchy:latest")
            if cached:
                import json
                data = json.loads(cached)
                if data.get("type") in ("superrepo_with_submodules", "single_repo", "pnpm_monorepo"):
                    return data
    except Exception as e:
        logger.debug(f"Redis hierarchy lookup failed: {e}")

    # Try PostgreSQL
    try:
        from memory.postgres_storage import get_hierarchy
        hierarchy = get_hierarchy(project_path)
        if hierarchy:
            return hierarchy
    except ImportError as e:
        print(f"[WARN] postgres_storage not available for hierarchy lookup: {e}")
    except Exception as e:
        logger.debug(f"PostgreSQL hierarchy lookup failed: {e}")

    return None


def get_hierarchy_projects(hierarchy: Dict = None) -> List[str]:
    """
    Extract project names from a hierarchy profile.

    Args:
        hierarchy: Hierarchy dict (fetches if None)

    Returns:
        List of project names
    """
    if not hierarchy:
        hierarchy = get_cached_hierarchy()

    if not hierarchy:
        return []

    projects = []

    # From boundaries
    for boundary in hierarchy.get("boundaries", []):
        name = boundary.get("name")
        if name:
            projects.append(name)

    # From projects list (new format)
    for project in hierarchy.get("projects", []):
        name = project.get("name")
        if name and name not in projects:
            projects.append(name)

    return projects


def get_recent_projects_for_context(session_id: str = None, limit: int = 5) -> List[str]:
    """
    Get recent projects for a BoundaryContext.

    Combines:
    1. Redis recency tracker
    2. Hierarchy profile projects

    Args:
        session_id: Session ID for session-specific recency
        limit: Maximum projects to return

    Returns:
        List of project names, ordered by recency
    """
    projects = []

    # Get from recency tracker
    try:
        from memory.project_recency import get_recency_tracker
        tracker = get_recency_tracker()
        recent = tracker.get_recent_projects(session_id=session_id, limit=limit)
        projects = [p[0] for p in recent]  # Extract project names
    except Exception as e:
        logger.debug(f"Recency tracker lookup failed: {e}")

    # If no recent projects, try hierarchy
    if not projects:
        projects = get_hierarchy_projects()[:limit]

    return projects


# =============================================================================
# MAIN CLASS
# =============================================================================

class BoundaryIntelligence:
    """
    Orchestrates project boundary detection and learning.

    Combines all 5 input signals, applies LLM analysis, and manages
    the feedback loop for continuous learning.
    """

    def __init__(self, redis_client=None, use_llm: bool = True):
        """
        Initialize BoundaryIntelligence.

        Args:
            redis_client: Optional Redis client for caching
            use_llm: Whether to use LLM for semantic analysis
        """
        self.redis = redis_client
        self.use_llm = use_llm

        # Lazy imports for components
        self._feedback_learner = None
        self._llm_analyzer = None
        self._recency_tracker = None
        self._dialogue_analyzer = None

    @property
    def feedback_learner(self):
        """Lazy load feedback learner."""
        if self._feedback_learner is None:
            from memory.boundary_feedback import get_boundary_learner
            self._feedback_learner = get_boundary_learner()
        return self._feedback_learner

    @property
    def llm_analyzer(self):
        """Lazy load LLM analyzer."""
        if self._llm_analyzer is None and self.use_llm:
            try:
                from memory.llm_boundary_analyzer import get_llm_boundary_analyzer
                self._llm_analyzer = get_llm_boundary_analyzer()
            except ImportError:
                logger.warning("LLM boundary analyzer not available")
        return self._llm_analyzer

    @property
    def recency_tracker(self):
        """Lazy load recency tracker."""
        if self._recency_tracker is None:
            try:
                from memory.project_recency import get_recency_tracker
                self._recency_tracker = get_recency_tracker(self.redis)
            except ImportError:
                logger.warning("Project recency tracker not available")
            except RecursionError:
                logger.warning("Recency tracker hit recursion limit (Python 3.14 + redis compatibility)")
                self._recency_tracker = None
        return self._recency_tracker

    @property
    def dialogue_analyzer(self):
        """Lazy load dialogue analyzer."""
        if self._dialogue_analyzer is None:
            try:
                from memory.dialogue_analyzer import get_dialogue_analyzer
                self._dialogue_analyzer = get_dialogue_analyzer()
            except ImportError:
                logger.warning("Dialogue analyzer not available")
        return self._dialogue_analyzer

    # =========================================================================
    # CORE ANALYSIS
    # =========================================================================

    def analyze_and_decide(self, context: BoundaryContext) -> BoundaryDecision:
        """
        Analyze all input signals and make a boundary decision.

        This is the main entry point for boundary detection.
        """
        try:
            return self._analyze_and_decide_impl(context)
        except RecursionError:
            logger.warning("Boundary analysis failed: maximum recursion depth exceeded")
            # Return a safe default decision that allows all learnings
            return BoundaryDecision(
                primary_project=None,
                confidence=0.0,
                confidence_level=ConfidenceLevel.UNCERTAIN,
                action=FilterAction.BROAD_CONTEXT,
                all_signals=[],
                keywords_analyzed=extract_keywords(context.user_prompt),
                reasoning="Analysis unavailable (recursion limit)",
                injection_id=context.injection_id,
                filter_note="⚠️ Project boundary detection temporarily unavailable",
            )

    def _analyze_and_decide_impl(self, context: BoundaryContext) -> BoundaryDecision:
        """Internal implementation of analyze_and_decide."""
        # Step 1: Extract keywords from prompt
        context.extracted_keywords = extract_keywords(context.user_prompt)

        # Step 2: Gather signals from all sources
        signals = self._gather_all_signals(context)
        context.detected_projects = signals

        # Step 3: Calculate combined confidence
        primary_project, confidence = self._calculate_confidence(signals)

        # Step 4: Determine action based on confidence
        confidence_level = confidence_to_level(confidence)
        action = CONFIDENCE_ACTIONS[confidence_level]

        # Step 5: Generate clarification if needed
        clarification_prompt = None
        clarification_options = None
        if action == FilterAction.CLARIFY:
            clarification_prompt, clarification_options = generate_clarification(
                signals, context.user_prompt
            )

        # Step 6: Generate filter note
        filter_note = self._generate_filter_note(
            primary_project, confidence_level, action
        )

        # Step 7: Build reasoning
        reasoning = self._build_reasoning(signals, primary_project, confidence)

        decision = BoundaryDecision(
            primary_project=primary_project,
            confidence=confidence,
            confidence_level=confidence_level,
            action=action,
            all_signals=signals,
            keywords_analyzed=context.extracted_keywords,
            reasoning=reasoning,
            injection_id=context.injection_id,
            clarification_prompt=clarification_prompt,
            clarification_options=clarification_options,
            filter_note=filter_note,
        )

        # Step 8: Cache decision in Redis (for later feedback attribution)
        self._cache_decision(decision, context)

        # Step 9: Record for learning (async via Celery)
        self._queue_for_learning(decision, context)

        return decision

    def _gather_all_signals(self, context: BoundaryContext) -> List[ProjectSignal]:
        """Gather project signals from all 5 sources."""
        signals = []

        # Signal 1: User Prompt (keywords → learned associations)
        prompt_signals = self._analyze_prompt(context)
        signals.extend(prompt_signals)

        # Signal 2: Active File Path
        if context.active_file_path:
            file_signal = self._analyze_file_path(context)
            if file_signal:
                signals.append(file_signal)

        # Signal 3: Hierarchy Profile
        if context.hierarchy_profile:
            hierarchy_signals = self._analyze_hierarchy(context)
            signals.extend(hierarchy_signals)

        # Signal 4: Recent Projects (from Redis)
        recency_signals = self._analyze_recency(context)
        signals.extend(recency_signals)

        # Signal 5: Mirrored Dialogue
        if context.mirrored_dialogue:
            dialogue_signals = self._analyze_dialogue(context)
            signals.extend(dialogue_signals)

        # Signal 6: CWD-based project detection (fallback when other signals empty)
        # Fixes 87% NULL detected_project: most injections lack active_file_path,
        # hierarchy, or recency data. CWD provides baseline project context.
        if not signals:
            cwd_signal = self._analyze_cwd(context)
            if cwd_signal:
                signals.append(cwd_signal)

        return signals

    def _analyze_cwd(self, context: BoundaryContext) -> Optional[ProjectSignal]:
        """
        Analyze current working directory for project context.

        Fallback signal when no other signals available.
        Prevents NULL detected_project by detecting superrepo or sub-project from CWD.

        Priority: KNOWN_REPOS first (reliable), then infer_project_from_path (heuristic).
        """
        try:
            cwd = os.getcwd()
            cwd_lower = cwd.lower()

            # Priority 1: Check for known repo root directories (most reliable)
            KNOWN_REPOS = {
                "er-simulator-superrepo": "er-simulator-superrepo",
                "er-sim-monitor": "er-sim-monitor",
                "ersim-voice-stack": "ersim-voice-stack",
                "context-dna": "context-dna",
            }

            for marker, name in KNOWN_REPOS.items():
                if marker in cwd_lower:
                    parts = Path(cwd).parts
                    # Find the marker in path parts
                    for i, part in enumerate(parts):
                        if part.lower() == marker:
                            # Check if there is a sub-project after the marker
                            if i + 1 < len(parts):
                                sub_project = parts[i + 1]
                                skip_dirs = {"src", "lib", "dist", ".git", "node_modules"}
                                if sub_project.lower() not in skip_dirs:
                                    return ProjectSignal(
                                        project=sub_project,
                                        source="cwd",
                                        confidence=0.7,
                                        keywords=[sub_project, marker],
                                        weight=SOURCE_WEIGHTS.get("cwd", 0.6)
                                    )
                            # At repo root (no sub-project) - return repo name
                            return ProjectSignal(
                                project=name,
                                source="cwd",
                                confidence=0.5,
                                keywords=[name],
                                weight=SOURCE_WEIGHTS.get("cwd", 0.6)
                            )

            # Priority 2: Try generic project inference from CWD path
            project = infer_project_from_path(cwd, context.hierarchy_profile)
            if project:
                return ProjectSignal(
                    project=project,
                    source="cwd",
                    confidence=0.6,
                    keywords=[project],
                    weight=SOURCE_WEIGHTS.get("cwd", 0.6)
                )

        except Exception as e:
            logger.debug(f"CWD analysis failed: {e}")

        return None

    def _analyze_prompt(self, context: BoundaryContext) -> List[ProjectSignal]:
        """Analyze prompt keywords against learned associations."""
        signals = []

        # Get known projects for LLM context
        known_projects = self._get_known_projects(context)

        # Use LLM analyzer if available
        if self.llm_analyzer:
            try:
                llm_signals = self.llm_analyzer.analyze_prompt(
                    prompt=context.user_prompt,
                    known_projects=known_projects,
                    file_context=context.active_file_path
                )
                signals.extend(llm_signals)
            except Exception as e:
                logger.warning(f"LLM prompt analysis failed: {e}")

        # Also check learned keyword associations
        try:
            associations = self.feedback_learner.get_keyword_project_associations(
                context.user_prompt
            )

            for project, score in associations.items():
                # Normalize score and create signal
                keywords = [k for k in context.extracted_keywords
                           if self._keyword_matches_project(k, project)]

                signals.append(ProjectSignal(
                    project=project,
                    source="prompt",
                    confidence=min(score / 10, 1.0),  # Normalize
                    keywords=keywords,
                    weight=SOURCE_WEIGHTS["prompt"]
                ))
        except Exception as e:
            logger.warning(f"Keyword association lookup failed: {e}")

        return signals

    def _get_known_projects(self, context: BoundaryContext) -> List[str]:
        """Get list of known projects for analysis context."""
        projects = []

        # From hierarchy profile
        if context.hierarchy_profile:
            projects.extend(get_hierarchy_projects(context.hierarchy_profile))

        # From recent projects
        if context.recent_projects:
            for p in context.recent_projects:
                if p not in projects:
                    projects.append(p)

        # Fallback: get from cached hierarchy
        if not projects:
            projects = get_hierarchy_projects()

        return projects

    def _analyze_file_path(self, context: BoundaryContext) -> Optional[ProjectSignal]:
        """Analyze file path for project context."""
        project = infer_project_from_path(
            context.active_file_path,
            context.hierarchy_profile
        )

        if project:
            return ProjectSignal(
                project=project,
                source="file_path",
                confidence=0.9,  # File path is highly reliable
                keywords=[project],
                weight=SOURCE_WEIGHTS["file_path"]
            )

        return None

    def _analyze_hierarchy(self, context: BoundaryContext) -> List[ProjectSignal]:
        """Analyze hierarchy profile for matching projects."""
        signals = []

        if not context.hierarchy_profile:
            return signals

        projects = context.hierarchy_profile.get("projects", [])

        for project in projects:
            project_name = project.get("name", "")
            project_path = project.get("path", "")

            # Check if any keywords match project name
            matching_keywords = [
                k for k in context.extracted_keywords
                if k.lower() in project_name.lower() or
                   k.lower() in project_path.lower()
            ]

            if matching_keywords:
                signals.append(ProjectSignal(
                    project=project_name,
                    source="hierarchy",
                    confidence=0.7 * (len(matching_keywords) / len(context.extracted_keywords)),
                    keywords=matching_keywords,
                    weight=SOURCE_WEIGHTS["hierarchy"]
                ))

        return signals

    def _analyze_recency(self, context: BoundaryContext) -> List[ProjectSignal]:
        """Analyze recent project history from Redis."""
        signals = []

        # Try to get scored recency data from tracker
        if self.recency_tracker:
            try:
                # Get projects with their decay-weighted scores
                recent_with_scores = self.recency_tracker.get_recent_projects(
                    session_id=context.session_id,
                    limit=5,
                    include_global=True,
                    apply_decay=True
                )

                for project, score in recent_with_scores:
                    signals.append(ProjectSignal(
                        project=project,
                        source="recency",
                        confidence=min(score, 0.9),  # Cap at 0.9
                        keywords=[],
                        weight=SOURCE_WEIGHTS["recency"] * score
                    ))

                # Record current file activity for future recency tracking
                if context.active_file_path:
                    try:
                        project_from_path = infer_project_from_path(
                            context.active_file_path,
                            context.hierarchy_profile
                        )
                        if project_from_path:
                            self.recency_tracker.record_activity(
                                project=project_from_path,
                                file_path=context.active_file_path,
                                session_id=context.session_id
                            )
                    except Exception as e:
                        logger.debug(f"Failed to record file activity: {e}")

                if signals:
                    return signals

            except Exception as e:
                logger.warning(f"Recency tracker query failed: {e}")

        # Fallback to context-provided recent projects
        recent = context.recent_projects
        if not recent:
            return signals

        # Recent projects get recency-weighted signals
        for i, project in enumerate(recent[:5]):  # Top 5 recent
            recency_weight = 1.0 - (i * 0.15)  # Decay with recency

            signals.append(ProjectSignal(
                project=project,
                source="recency",
                confidence=0.5 * recency_weight,
                keywords=[],
                weight=SOURCE_WEIGHTS["recency"] * recency_weight
            ))

        return signals

    def _analyze_dialogue(self, context: BoundaryContext) -> List[ProjectSignal]:
        """Analyze mirrored dialogue for project context."""
        signals = []

        if not context.mirrored_dialogue:
            return signals

        # Get known projects for context
        known_projects = self._get_known_projects(context)

        # Use dialogue analyzer if available
        if self.dialogue_analyzer:
            try:
                # Parse dialogue into messages if it's a string
                messages = self._parse_dialogue_to_messages(context.mirrored_dialogue)

                # Get signals from dialogue analyzer
                dialogue_signals = self.dialogue_analyzer.analyze(
                    messages=messages,
                    known_projects=known_projects
                )

                # Add dialogue signals with source attribution
                for sig in dialogue_signals:
                    signals.append(ProjectSignal(
                        project=sig.project,
                        source="dialogue",
                        confidence=sig.confidence * 0.8,  # Cap dialogue confidence
                        keywords=sig.keywords,
                        weight=SOURCE_WEIGHTS["dialogue"] * sig.weight
                    ))

                return signals
            except Exception as e:
                logger.warning(f"Dialogue analyzer failed: {e}")
                # Fall through to legacy analysis

        # Legacy analysis: extract keywords and check associations
        dialogue_keywords = extract_keywords(context.mirrored_dialogue)

        try:
            associations = self.feedback_learner.get_keyword_project_associations(
                context.mirrored_dialogue
            )

            for project, score in associations.items():
                signals.append(ProjectSignal(
                    project=project,
                    source="dialogue",
                    confidence=min(score / 15, 0.8),  # Cap at 0.8 for dialogue
                    keywords=dialogue_keywords[:5],
                    weight=SOURCE_WEIGHTS["dialogue"]
                ))
        except Exception as e:
            logger.warning(f"Dialogue keyword lookup failed: {e}")

        return signals

    def _parse_dialogue_to_messages(self, dialogue: str) -> List[Dict]:
        """
        Parse a dialogue string into message format for DialogueAnalyzer.

        Handles various formats:
        - JSON array of messages
        - Plain text (treated as single user message)
        - Formatted conversation (User: ... Assistant: ...)
        """
        if not dialogue:
            return []

        # Try JSON first
        try:
            import json
            messages = json.loads(dialogue)
            if isinstance(messages, list):
                return messages
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[WARN] Dialogue JSON parse failed, falling back to text parse: {e}")

        # Try to parse formatted conversation
        messages = []
        lines = dialogue.split('\n')
        current_role = "user"
        current_content = []

        for line in lines:
            line_lower = line.lower().strip()
            if line_lower.startswith(('user:', 'human:')):
                if current_content:
                    messages.append({
                        "role": current_role,
                        "content": '\n'.join(current_content).strip()
                    })
                current_role = "user"
                current_content = [line.split(':', 1)[1].strip() if ':' in line else '']
            elif line_lower.startswith(('assistant:', 'claude:', 'ai:')):
                if current_content:
                    messages.append({
                        "role": current_role,
                        "content": '\n'.join(current_content).strip()
                    })
                current_role = "assistant"
                current_content = [line.split(':', 1)[1].strip() if ':' in line else '']
            else:
                current_content.append(line)

        # Add final message
        if current_content:
            messages.append({
                "role": current_role,
                "content": '\n'.join(current_content).strip()
            })

        # If no structured conversation found, treat as single message
        if not messages:
            messages = [{"role": "user", "content": dialogue}]

        return messages

    def _keyword_matches_project(self, keyword: str, project: str) -> bool:
        """Check if a keyword likely relates to a project."""
        # Simple heuristic - keyword in project name or vice versa
        return (keyword.lower() in project.lower() or
                project.lower() in keyword.lower())

    # =========================================================================
    # CONFIDENCE CALCULATION
    # =========================================================================

    def _calculate_confidence(self, signals: List[ProjectSignal]) -> Tuple[Optional[str], float]:
        """
        Calculate combined confidence and determine primary project.

        Uses weighted voting across all signals.
        """
        if not signals:
            return None, 0.0

        # Aggregate scores by project
        project_scores = {}
        for signal in signals:
            score = signal.confidence * signal.weight
            if signal.project in project_scores:
                project_scores[signal.project] += score
            else:
                project_scores[signal.project] = score

        if not project_scores:
            return None, 0.0

        # Find primary project
        primary = max(project_scores.items(), key=lambda x: x[1])
        primary_project, primary_score = primary

        # Calculate confidence as ratio of primary to total
        total_score = sum(project_scores.values())
        confidence = primary_score / total_score if total_score > 0 else 0.0

        # Adjust confidence based on signal agreement
        unique_projects = len(project_scores)
        if unique_projects == 1:
            # All signals agree - boost confidence
            confidence = min(confidence * 1.2, 1.0)
        elif unique_projects > 3:
            # Many conflicting signals - reduce confidence
            confidence = confidence * 0.8

        return primary_project, confidence

    # =========================================================================
    # OUTPUT GENERATION
    # =========================================================================

    def _generate_filter_note(
        self,
        project: Optional[str],
        level: ConfidenceLevel,
        action: FilterAction
    ) -> Optional[str]:
        """Generate a note for the user about filtering applied."""
        if action == FilterAction.FULL_FILTER:
            return f"📂 Context filtered to: {project}"
        elif action == FilterAction.SOFT_FILTER:
            return f"📂 Prioritizing context for: {project} (may include general patterns)"
        elif action == FilterAction.CLARIFY:
            return "❓ Multiple projects detected - please clarify context"
        elif action == FilterAction.BROAD_CONTEXT:
            return "📚 Providing broad context (project uncertain)"
        return None

    def _build_reasoning(
        self,
        signals: List[ProjectSignal],
        project: Optional[str],
        confidence: float
    ) -> str:
        """Build human-readable reasoning for the decision."""
        if not signals:
            return "No project signals detected"

        reasons = []

        # Group by source
        by_source = {}
        for signal in signals:
            if signal.source not in by_source:
                by_source[signal.source] = []
            by_source[signal.source].append(signal)

        for source, source_signals in by_source.items():
            projects = list(set(s.project for s in source_signals))
            if len(projects) == 1:
                reasons.append(f"{source}: {projects[0]}")
            else:
                reasons.append(f"{source}: multiple ({', '.join(projects[:3])})")

        return f"Primary: {project} ({confidence:.0%}) | {' | '.join(reasons)}"

    # =========================================================================
    # CACHING & QUEUING
    # =========================================================================

    def _cache_decision(self, decision: BoundaryDecision, context: BoundaryContext):
        """Cache decision in Redis for later feedback attribution."""
        if not self.redis:
            return

        try:
            key = REDIS_KEYS["decision_cache"].format(injection_id=context.injection_id)
            self.redis.setex(
                key,
                REDIS_TTLS["decision_cache"],
                json.dumps(asdict(decision), default=str)
            )
        except Exception as e:
            logger.warning(f"Failed to cache decision: {e}")

    def _queue_for_learning(self, decision: BoundaryDecision, context: BoundaryContext):
        """Queue decision for background learning via Celery.

        Uses fire_and_forget to avoid blocking on Redis unavailability.
        Falls back to sync recording if Celery is unavailable.
        """
        try:
            from memory.celery_tasks import record_boundary_decision, fire_and_forget

            # Use fire_and_forget - returns False immediately if Celery unavailable
            queued = fire_and_forget(
                record_boundary_decision,
                decision_id=decision.decision_id,
                injection_id=context.injection_id,
                primary_project=decision.primary_project,
                confidence=decision.confidence,
                keywords=context.extracted_keywords,
                signals=[asdict(s) for s in decision.all_signals],
                session_id=context.session_id
            )

            if not queued:
                # Celery unavailable, record directly
                self._record_decision_sync(decision, context)

        except ImportError:
            # Celery tasks not available, record directly
            self._record_decision_sync(decision, context)
        except RecursionError:
            logger.warning("Failed to queue for learning: maximum recursion depth exceeded")
        except Exception as e:
            logger.warning(f"Failed to queue for learning: {e}")
            try:
                self._record_decision_sync(decision, context)
            except RecursionError:
                logger.warning("Sync recording also hit recursion limit")

    def _record_decision_sync(self, decision: BoundaryDecision, context: BoundaryContext):
        """Synchronously record decision (fallback when Celery unavailable)."""
        self.feedback_learner.record_injection(
            injection_id=context.injection_id,
            prompt=context.user_prompt,
            detected_project=decision.primary_project,
            ab_variant="control",  # Default when not in A/B test
            keywords=context.extracted_keywords,
            learnings_included=[],
            risk_level="moderate",
            session_id=context.session_id
        )

        # Bridge to injection_store session tracking for feedback attribution.
        # Without this, auto_capture.py cannot find the boundary injection_id
        # to record feedback against (the 0-feedback bug).
        try:
            session_id = context.session_id or os.environ.get("CLAUDE_SESSION_ID", "")
            if session_id:
                from memory.injection_store import get_injection_store
                store = get_injection_store()
                store.track_session_injection(
                    session_id=session_id,
                    injection_id=context.injection_id,
                    ab_variant="control"
                )
        except Exception as e:
            logger.debug(f"Session-injection bridge failed (non-blocking): {e}")

    # =========================================================================
    # FILTERING
    # =========================================================================

    def filter_learnings(
        self,
        learnings: List[Dict],
        decision: BoundaryDecision
    ) -> List[Dict]:
        """
        Filter learnings based on boundary decision.

        Args:
            learnings: List of learning dicts with 'tags', 'source', etc.
            decision: The boundary decision

        Returns:
            Filtered list of learnings
        """
        if not decision.should_filter:
            return learnings

        if not decision.primary_project:
            return learnings

        filtered = []
        project_lower = decision.primary_project.lower()

        for learning in learnings:
            # Check various fields for project match
            tags = learning.get("tags", [])
            source = learning.get("source", "")
            title = learning.get("title", "")
            content = learning.get("content", "")

            # Check if learning matches project
            matches = (
                project_lower in str(tags).lower() or
                project_lower in source.lower() or
                project_lower in title.lower() or
                any(k in content.lower() for k in decision.keywords_analyzed if len(k) > 4)
            )

            if decision.action == FilterAction.FULL_FILTER:
                # Only include exact matches
                if matches:
                    filtered.append(learning)
            else:
                # Soft filter - include matches first, then general
                learning["_boundary_match"] = matches
                filtered.append(learning)

        # For soft filter, sort by match status
        if decision.action == FilterAction.SOFT_FILTER:
            filtered.sort(key=lambda x: not x.get("_boundary_match", False))
            # Clean up temporary field
            for l in filtered:
                l.pop("_boundary_match", None)

        return filtered

    # =========================================================================
    # FEEDBACK HANDLING
    # =========================================================================

    def record_feedback(
        self,
        injection_id: str,
        was_helpful: bool,
        project_was_correct: bool = True,
        correct_project: Optional[str] = None,
        signal_type: FeedbackSignal = FeedbackSignal.TASK_SUCCESS,
        user_explicit: bool = False
    ):
        """
        Record feedback about a boundary decision.

        This triggers learning to improve future predictions.
        """
        self.feedback_learner.record_feedback(
            injection_id=injection_id,
            was_helpful=was_helpful,
            project_was_correct=project_was_correct,
            correction_project=correct_project,
            confidence=SIGNAL_WEIGHTS.get(signal_type, 0.5),
            signals=[signal_type.value],
            user_explicit=user_explicit
        )

    def record_clarification_response(
        self,
        injection_id: str,
        selected_project: str
    ):
        """
        Record user's response to a clarification prompt.

        This is a strong learning signal.
        """
        # Get cached decision
        decision = self._get_cached_decision(injection_id)

        if decision:
            was_correct = decision.get("primary_project") == selected_project

            self.feedback_learner.record_feedback(
                injection_id=injection_id,
                was_helpful=True,  # User engaged with clarification
                project_was_correct=was_correct,
                correction_project=selected_project if not was_correct else None,
                confidence=1.0,  # Strong signal
                signals=[FeedbackSignal.CLARIFICATION_RESPONSE.value],
                user_explicit=True
            )

    def _get_cached_decision(self, injection_id: str) -> Optional[Dict]:
        """Get a cached decision from Redis."""
        if not self.redis:
            return None

        try:
            key = REDIS_KEYS["decision_cache"].format(injection_id=injection_id)
            data = self.redis.get(key)
            if data:
                return json.loads(data)
        except Exception as e:
            print(f"[WARN] Decision cache read failed: {e}")

        return None


# =============================================================================
# SINGLETON
# =============================================================================

_instance = None


def get_boundary_intelligence(redis_client=None, use_llm: bool = True) -> BoundaryIntelligence:
    """Get the singleton BoundaryIntelligence instance."""
    global _instance
    if _instance is None:
        _instance = BoundaryIntelligence(redis_client=redis_client, use_llm=use_llm)
    return _instance


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    bi = get_boundary_intelligence(use_llm=False)  # No LLM for CLI testing

    if len(sys.argv) < 2:
        print("Boundary Intelligence")
        print("=" * 50)
        print("\nCommands:")
        print("  python boundary_intelligence.py analyze <prompt> [file_path]")
        print("  python boundary_intelligence.py filter <prompt> <learning_json>")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "analyze":
        prompt = sys.argv[2] if len(sys.argv) > 2 else "fix the bug"
        file_path = sys.argv[3] if len(sys.argv) > 3 else None

        context = BoundaryContext(
            user_prompt=prompt,
            active_file_path=file_path,
            session_id="cli_test"
        )

        decision = bi.analyze_and_decide(context)

        print(f"\nPrompt: {prompt}")
        if file_path:
            print(f"File: {file_path}")
        print(f"\n{'='*50}")
        print(f"Primary Project: {decision.primary_project or 'Unknown'}")
        print(f"Confidence: {decision.confidence:.1%} ({decision.confidence_level.value})")
        print(f"Action: {decision.action.value}")
        print(f"Reasoning: {decision.reasoning}")
        if decision.filter_note:
            print(f"\nNote: {decision.filter_note}")
        if decision.should_clarify:
            print(f"\nClarification: {decision.clarification_prompt}")
            print(f"Options: {decision.clarification_options}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
