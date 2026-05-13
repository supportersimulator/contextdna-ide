"""
Context DNA Setup Module - Adaptive Hierarchy Intelligence

This module provides:
- HierarchyAnalyzer: Deep codebase scanning
- PlacementEngine: Adaptive file placement suggestions
- PlatformInfo: Cross-platform detection (delegates to local_llm/hardware.py)
- HierarchyProfile: Complete codebase structure profile

The setup module is the foundation of Synaptic's adaptive intelligence,
enabling Context DNA to learn and adapt to any user's codebase structure.
"""

from context_dna.setup.checker import (
    SetupChecker,
    SetupStatus,
    check_all,
    get_missing_configs,
    get_setup_commands,
)

from context_dna.setup.notifications import (
    notify,
    notify_setup_needed,
    notify_success,
    notify_error,
    NotificationManager,
)

from context_dna.setup.models import (
    HierarchyProfile,
    PlatformInfo,
    LLMBackend,
    RepoType,
    SubmoduleInfo,
    ServiceLocation,
    NamingConvention,
    ConfigPattern,
    Pin,
    Suggestion,
    SuggestionType,
    SuggestionResponse,
    PlacementSuggestion,
    QuestionAnswer,
)

# Import analyzer and engine (may have additional deps)
try:
    from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer
    from context_dna.setup.placement_engine import PlacementEngine
    from context_dna.setup.hierarchy_questioner import HierarchyQuestioner
except ImportError:
    # These may require additional dependencies not installed yet
    HierarchyAnalyzer = None
    PlacementEngine = None
    HierarchyQuestioner = None

__all__ = [
    # Checker
    "SetupChecker",
    "SetupStatus",
    "check_all",
    "get_missing_configs",
    "get_setup_commands",
    # Notifications
    "notify",
    "notify_setup_needed",
    "notify_success",
    "notify_error",
    "NotificationManager",
    # Models (Adaptive Hierarchy Intelligence)
    "HierarchyProfile",
    "PlatformInfo",
    "LLMBackend",
    "RepoType",
    "SubmoduleInfo",
    "ServiceLocation",
    "NamingConvention",
    "ConfigPattern",
    "Pin",
    "Suggestion",
    "SuggestionType",
    "SuggestionResponse",
    "PlacementSuggestion",
    "QuestionAnswer",
    # Analyzers (if available)
    "HierarchyAnalyzer",
    "PlacementEngine",
    "HierarchyQuestioner",
]
