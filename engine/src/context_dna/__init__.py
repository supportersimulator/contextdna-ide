"""
Context DNA - Autonomous Learning System for Developers

Captures wins, fixes, and patterns automatically.
Works with any IDE: Claude Code, Cursor, VS Code, or terminal.

Quick Start:
    pip install context-dna
    context-dna init
    context-dna win "Fixed bug" "Used async pattern"

Python API:
    from context_dna import brain
    brain.init()
    brain.win("Fixed bug", "Used async pattern")
    results = brain.query("async")

Hook Evolution (A/B/C Testing):
    from context_dna import HookEvolutionEngine
    engine = HookEvolutionEngine()
    variant = engine.get_active_variant('UserPromptSubmit', session_id)
    engine.record_outcome(variant.variant_id, session_id, 'positive', ...)
"""

__version__ = "0.1.0"
__author__ = "Aaron Tjomsland"

# Core brain (ArchitectureBrain is the actual class name)
from context_dna.brain import ArchitectureBrain, brain

# Configuration management
from context_dna.config import (
    Config,
    get_config,
    get_user_data_dir,
    get_db_path,
    init_user_data,
    ensure_user_data_dir,
)

# Hook evolution system
from context_dna.hook_evolution import HookEvolutionEngine

# Pattern analysis
from context_dna.prompt_pattern_analyzer import PromptPatternAnalyzer

# Deduplication (class is called DuplicateDetector)
from context_dna.dedup_detector import DuplicateDetector

# Alias for backwards compatibility
Brain = ArchitectureBrain
DedupDetector = DuplicateDetector  # Alias for convenience

__all__ = [
    # Version
    "__version__",
    # Core
    "brain",
    "Brain",
    "ArchitectureBrain",
    # Config
    "Config",
    "get_config",
    "get_user_data_dir",
    "get_db_path",
    "init_user_data",
    "ensure_user_data_dir",
    # Hook Evolution
    "HookEvolutionEngine",
    # Pattern Analysis
    "PromptPatternAnalyzer",
    # Deduplication
    "DuplicateDetector",
    "DedupDetector",  # Alias
]
