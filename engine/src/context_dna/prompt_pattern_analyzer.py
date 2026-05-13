#!/usr/bin/env python3
"""
PROMPT PATTERN ANALYZER

Analyzes user prompts to extract effective communication patterns that
lead to better outcomes. These patterns become "C" variants in A/B/C
testing - dynamically injected user wisdom.

PHILOSOPHY:
Users develop intuitions about how to communicate with AI effectively.
Patterns like "give your best as if your existence depends on it" or
"be extremely thorough" lead to different outcomes. We capture these
patterns, correlate them with session outcomes, and inject proven
patterns situationally.

A/B/C TESTING STRATEGY:
- A = Control hook (baseline)
- B = Variant hooks (minor/major code changes)
- C = User Wisdom Injection (dynamically generated from user patterns)

PATTERN CATEGORIES:
- Vision Anchoring: User sets a compelling goal/outcome vision
- Stakes Framing: User emphasizes importance/consequences
- Quality Signals: User specifies thoroughness, precision, care
- Persona Priming: User invokes excellence, expertise, mastery
- Constraint Setting: User defines boundaries, limits, focus areas
- Collaboration Cues: User establishes partnership, shared mission

RISK ANALYSIS:
Same experience-based risk as patterns/hooks - patterns that lead to
worse outcomes get risk-elevated, patterns that help get risk-reduced.

Usage:
    from memory.prompt_pattern_analyzer import PromptPatternAnalyzer, get_analyzer

    analyzer = get_analyzer()
    patterns = analyzer.extract_patterns("give your best work...")
    analyzer.record_session(session_id, prompt_text, patterns_used)
    # ... later ...
    analyzer.attribute_outcome(session_id, 'positive', confidence=0.8)

    # Get wisdom injection for context
    injection = analyzer.generate_wisdom_injection(prompt_text, risk_level)
"""

import re
import json
import sqlite3
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field
from enum import Enum

# Import config for centralized paths
try:
    from context_dna.config import get_db_path, ensure_user_data_dir
    # Ensure user data directory exists before getting db path
    ensure_user_data_dir()
    EVOLUTION_DB = get_db_path()
except ImportError:
    # Fallback for standalone usage
    EVOLUTION_DB = Path(__file__).parent / ".pattern_evolution.db"

# Singleton instance
_analyzer_instance = None


class PatternCategory(Enum):
    """Categories of effective prompt patterns."""
    VISION_ANCHORING = "vision_anchoring"      # Compelling goal/outcome setting
    STAKES_FRAMING = "stakes_framing"          # Emphasizing importance/consequences
    QUALITY_SIGNALS = "quality_signals"        # Thoroughness, precision, care
    PERSONA_PRIMING = "persona_priming"        # Invoking excellence, expertise
    CONSTRAINT_SETTING = "constraint_setting"  # Boundaries, limits, focus
    COLLABORATION_CUES = "collaboration"       # Partnership, shared mission
    URGENCY_MARKERS = "urgency"                # Time sensitivity, priority
    SPECIFICITY_DEPTH = "specificity"          # Level of detail requested


class TaskContext(Enum):
    """
    Task context types for circumstantial pattern learning.

    The system learns WHEN to inject patterns, not just WHAT patterns work.
    Different contexts benefit from different patterns.
    """
    # Technical contexts
    DEPLOYMENT = "deployment"          # Deploy, release, production changes
    DEBUGGING = "debugging"            # Fix bugs, investigate issues
    REFACTORING = "refactoring"        # Code improvement, restructuring
    ARCHITECTURE = "architecture"      # System design, structural decisions
    INFRASTRUCTURE = "infrastructure"  # Docker, terraform, cloud configs
    TESTING = "testing"                # Write tests, test coverage

    # Work style contexts
    EXPLORATION = "exploration"        # Research, investigate, understand
    IMPLEMENTATION = "implementation"  # Build new features, write code
    DOCUMENTATION = "documentation"    # Write docs, comments, READMEs
    REVIEW = "review"                  # Code review, PR review

    # Complexity contexts
    SIMPLE_TASK = "simple"             # Quick fixes, small changes
    COMPLEX_TASK = "complex"           # Multi-step, many files
    CRITICAL_TASK = "critical"         # Production, high-stakes

    # Domain contexts
    FRONTEND = "frontend"              # UI, React, components
    BACKEND = "backend"                # API, server, database
    DEVOPS = "devops"                  # CI/CD, deployment pipelines
    DATA = "data"                      # Database, migrations, queries

    UNKNOWN = "unknown"                # Could not classify


# Context detection patterns - keywords that signal each context
CONTEXT_DETECTION = {
    "deployment": [
        r"deploy", r"release", r"production", r"prod\b", r"push.*live",
        r"rollout", r"ship\b", r"go.*live"
    ],
    "debugging": [
        r"\bfix\b", r"\bbug\b", r"debug", r"issue", r"broken", r"not.*working",
        r"error", r"crash", r"investigate", r"troubleshoot"
    ],
    "refactoring": [
        r"refactor", r"clean.*up", r"restructure", r"reorganize", r"improve.*code",
        r"simplify", r"extract", r"deduplicate"
    ],
    "architecture": [
        r"architect", r"design", r"system", r"structure", r"pattern",
        r"framework", r"foundation", r"scalab"
    ],
    "infrastructure": [
        r"docker", r"terraform", r"aws", r"cloud", r"kubernetes", r"k8s",
        r"nginx", r"server", r"container", r"infra"
    ],
    "testing": [
        r"\btest", r"spec\b", r"coverage", r"unit.*test", r"integration",
        r"e2e", r"assert", r"mock"
    ],
    "exploration": [
        r"explore", r"research", r"investigate", r"understand", r"learn",
        r"figure.*out", r"how.*does", r"what.*is", r"find.*out"
    ],
    "implementation": [
        r"implement", r"build", r"create", r"add.*feature", r"new.*feature",
        r"develop", r"write.*code"
    ],
    "documentation": [
        r"document", r"readme", r"comment", r"explain", r"describe",
        r"write.*docs", r"api.*doc"
    ],
    "review": [
        r"review", r"\bpr\b", r"pull.*request", r"check.*code", r"look.*over",
        r"feedback"
    ],
    "frontend": [
        r"react", r"component", r"ui\b", r"ux\b", r"css", r"style",
        r"button", r"form", r"modal", r"page"
    ],
    "backend": [
        r"api", r"endpoint", r"server", r"database", r"query",
        r"controller", r"service", r"handler"
    ],
    "devops": [
        r"ci\/cd", r"pipeline", r"github.*action", r"workflow",
        r"build.*script", r"deploy.*script"
    ],
    "data": [
        r"migration", r"schema", r"database", r"query", r"sql",
        r"model", r"table", r"column"
    ]
}


class PatternEffectiveness(Enum):
    """How well a pattern correlates with positive outcomes."""
    HIGHLY_EFFECTIVE = "highly_effective"    # >70% positive correlation
    EFFECTIVE = "effective"                   # 50-70% positive correlation
    NEUTRAL = "neutral"                       # No clear correlation
    INEFFECTIVE = "ineffective"              # <30% positive correlation
    UNKNOWN = "unknown"                       # Insufficient data


@dataclass
class PromptPattern:
    """A detected pattern in user prompts."""
    pattern_id: str
    category: str
    name: str
    description: str
    regex_patterns: List[str]      # Patterns that detect this
    example_phrases: List[str]     # Example user phrases
    injection_template: str        # How to inject this wisdom
    is_active: bool = True
    is_protected: bool = False
    created_at: str = ""
    created_by: str = "system"


@dataclass
class PatternMatch:
    """A specific match of a pattern in a prompt."""
    pattern_id: str
    category: str
    matched_text: str
    confidence: float
    position: int  # Position in prompt


@dataclass
class PatternStats:
    """Statistics for a prompt pattern."""
    pattern_id: str
    total_sessions: int
    positive_outcomes: int
    negative_outcomes: int
    neutral_outcomes: int
    positive_rate: float
    effectiveness: str
    experience_level: str
    avg_confidence_when_used: float


@dataclass
class WisdomInjection:
    """A generated wisdom injection for the C variant."""
    injection_id: str
    context_signals: List[str]     # What triggered this injection
    patterns_used: List[str]       # Pattern IDs that contributed
    injection_text: str            # The actual text to inject
    confidence: float              # How confident we are this will help
    risk_modifier: float           # Experience-based risk adjustment


@dataclass
class ContextualMatch:
    """A pattern's contextual effectiveness data."""
    pattern_id: str
    context: str
    total_uses: int
    positive_outcomes: int
    negative_outcomes: int
    positive_rate: float
    confidence_level: str  # 'high', 'medium', 'low', 'insufficient'


@dataclass
class DetectedContext:
    """Result of context detection from a prompt."""
    primary_context: str           # Most likely context
    secondary_contexts: List[str]  # Other detected contexts
    context_signals: Dict[str, List[str]]  # Which keywords triggered each context
    complexity: str                # 'simple', 'complex', 'critical'
    confidence: float              # How confident in classification


# =============================================================================
# BUILT-IN PATTERNS (User can add more through the system)
# =============================================================================

BUILTIN_PATTERNS = [
    # Vision Anchoring
    {
        "pattern_id": "vision_excellence",
        "category": "vision_anchoring",
        "name": "Excellence Vision",
        "description": "User invokes vision of exceptional quality outcome",
        "regex_patterns": [
            r"give.*(?:your )?best",
            r"as.*(?:if|though).*(?:life|existence|career).*depend",
            r"exceptional.*(?:work|result|quality)",
            r"make.*(?:this )?(?:the )?best",
            r"world.?class",
        ],
        "example_phrases": [
            "give your best work",
            "as if your existence depends on it",
            "make this exceptional",
        ],
        "injection_template": "⚡ EXCELLENCE ANCHOR: This task warrants your highest caliber work. The outcome should reflect mastery-level execution.",
    },
    {
        "pattern_id": "vision_outcome",
        "category": "vision_anchoring",
        "name": "Outcome Vision",
        "description": "User paints picture of desired end state",
        "regex_patterns": [
            r"(?:i )?(?:want|need|envision).*(?:to )?end.*(?:with|up|having)",
            r"the.*(?:goal|aim|target).*is.*to",
            r"by.*(?:the )?end.*(?:should|will|must)",
            r"ultimate.*(?:result|outcome|goal)",
        ],
        "example_phrases": [
            "I want to end up with a working system",
            "the goal is to have complete coverage",
        ],
        "injection_template": "🎯 OUTCOME FOCUS: Keep the end state clearly in mind throughout. Every decision should advance toward the stated goal.",
    },

    # Stakes Framing
    {
        "pattern_id": "stakes_critical",
        "category": "stakes_framing",
        "name": "Critical Stakes",
        "description": "User emphasizes critical importance",
        "regex_patterns": [
            r"(?:this is|it'?s) (?:absolutely |extremely |super )?(?:critical|crucial|vital)",
            r"cannot.*(?:afford|allow).*(?:fail|error|mistake)",
            r"(?:high|highest).*(?:stakes|priority|importance)",
            r"production.*(?:system|data|environment)",
        ],
        "example_phrases": [
            "this is critical - cannot afford errors",
            "highest priority task",
        ],
        "injection_template": "🚨 HIGH STAKES: Extra verification required. Double-check all changes. Consider rollback strategy.",
    },
    {
        "pattern_id": "stakes_consequence",
        "category": "stakes_framing",
        "name": "Consequence Awareness",
        "description": "User highlights potential negative consequences",
        "regex_patterns": [
            r"if.*(?:this|it).*(?:fails|breaks|goes wrong)",
            r"(?:could|would|will).*(?:cause|lead to|result in).*(?:damage|loss|problem)",
            r"(?:people|users|team).*(?:depend|rely|count).*on",
        ],
        "example_phrases": [
            "if this fails, the whole system goes down",
            "users depend on this working",
        ],
        "injection_template": "⚠️ CONSEQUENCE AWARE: Failures here have ripple effects. Proceed with appropriate caution.",
    },

    # Quality Signals
    {
        "pattern_id": "quality_thorough",
        "category": "quality_signals",
        "name": "Thoroughness Request",
        "description": "User requests comprehensive treatment",
        "regex_patterns": [
            r"(?:be )?(?:extremely |very |super )?thorough",
            r"(?:don'?t|do not).*(?:miss|skip|overlook).*(?:anything|any)",
            r"(?:cover|check|verify).*(?:everything|all|each)",
            r"comprehensive",
            r"leave.*no.*(?:stone|detail).*unturned",
        ],
        "example_phrases": [
            "be extremely thorough",
            "don't miss anything",
            "comprehensive review",
        ],
        "injection_template": "🔍 THOROUGHNESS MODE: Systematic coverage required. Check all edge cases. Verify completeness before finalizing.",
    },
    {
        "pattern_id": "quality_precision",
        "category": "quality_signals",
        "name": "Precision Request",
        "description": "User emphasizes accuracy and precision",
        "regex_patterns": [
            r"(?:be )?(?:very |extremely )?(?:precise|accurate|exact)",
            r"(?:no|zero).*(?:room|tolerance).*(?:for )?(?:error|mistake)",
            r"(?:must|has to|needs to).*be.*(?:correct|right|accurate)",
            r"pixel.?perfect",
        ],
        "example_phrases": [
            "be extremely precise",
            "no room for error",
            "must be exactly correct",
        ],
        "injection_template": "🎯 PRECISION REQUIRED: Accuracy over speed. Verify each detail. Cross-reference where possible.",
    },

    # Persona Priming
    {
        "pattern_id": "persona_expert",
        "category": "persona_priming",
        "name": "Expert Invocation",
        "description": "User invokes expert/master persona",
        "regex_patterns": [
            r"(?:as|like).*(?:an? )?(?:expert|master|senior)",
            r"(?:your )?(?:expertise|mastery|experience)",
            r"(?:bring|use|apply).*(?:your )?(?:full|complete).*(?:knowledge|capability)",
            r"(?:top|best).*(?:engineer|developer|architect)",
        ],
        "example_phrases": [
            "approach this as a senior architect",
            "bring your full expertise",
        ],
        "injection_template": "🧠 EXPERT MODE: Apply deep domain knowledge. Consider architectural implications. Think holistically.",
    },
    {
        "pattern_id": "persona_care",
        "category": "persona_priming",
        "name": "Care Invocation",
        "description": "User requests thoughtful, caring approach",
        "regex_patterns": [
            r"(?:really )?care(?:fully)?.*(?:about|with)",
            r"(?:take|with).*(?:great )?care",
            r"(?:thoughtful|considered|deliberate)",
            r"(?:don'?t|do not).*(?:rush|hurry)",
        ],
        "example_phrases": [
            "take great care with this",
            "be thoughtful about the approach",
        ],
        "injection_template": "💎 CAREFUL APPROACH: Quality over speed. Consider implications. Think through edge cases.",
    },

    # Constraint Setting
    {
        "pattern_id": "constraint_minimal",
        "category": "constraint_setting",
        "name": "Minimal Change",
        "description": "User wants focused, minimal changes",
        "regex_patterns": [
            r"(?:only|just).*(?:the )?(?:minimum|minimal|necessary)",
            r"(?:don'?t|do not).*(?:touch|change|modify).*(?:anything else|other)",
            r"(?:smallest|least).*(?:change|modification|impact)",
            r"surgical",
        ],
        "example_phrases": [
            "only the minimum necessary",
            "don't touch anything else",
        ],
        "injection_template": "✂️ MINIMAL SCOPE: Smallest possible change. Leave everything else untouched. Verify no side effects.",
    },
    {
        "pattern_id": "constraint_focus",
        "category": "constraint_setting",
        "name": "Focus Constraint",
        "description": "User narrows scope explicitly",
        "regex_patterns": [
            r"(?:focus|concentrate).*(?:only|just).*on",
            r"(?:only|just).*(?:this|that|the).*(?:one|single|specific)",
            r"(?:ignore|skip|don'?t worry about).*(?:everything|all|the rest)",
            r"scope.*(?:limited|restricted|narrow)",
        ],
        "example_phrases": [
            "focus only on the authentication module",
            "ignore everything else for now",
        ],
        "injection_template": "🎯 FOCUSED SCOPE: Stay within defined boundaries. Flag related issues but don't fix them.",
    },

    # Collaboration Cues
    {
        "pattern_id": "collab_partner",
        "category": "collaboration",
        "name": "Partnership Framing",
        "description": "User frames as collaborative partnership",
        "regex_patterns": [
            r"(?:let'?s|we).*(?:work|figure|solve).*(?:together|this out)",
            r"(?:you and (?:me|i)|we).*(?:team|partner)",
            r"(?:help|assist|support).*(?:me|us).*(?:with|to)",
            r"(?:our|we'?re).*(?:goal|mission|task)",
        ],
        "example_phrases": [
            "let's work through this together",
            "help me figure this out",
        ],
        "injection_template": "🤝 COLLABORATIVE MODE: This is a partnership. Think aloud. Share reasoning. Ask clarifying questions.",
    },

    # Urgency Markers
    {
        "pattern_id": "urgency_time",
        "category": "urgency",
        "name": "Time Sensitivity",
        "description": "User indicates time pressure",
        "regex_patterns": [
            r"(?:need|want).*(?:this|it).*(?:asap|immediately|urgently|quickly)",
            r"(?:tight|short).*(?:deadline|timeframe|timeline)",
            r"(?:running|short).*(?:on|of).*time",
            r"(?:today|tonight|now|right away)",
        ],
        "example_phrases": [
            "need this asap",
            "tight deadline",
        ],
        "injection_template": "⏰ TIME SENSITIVE: Optimize for speed while maintaining quality. Prioritize core functionality.",
    },

    # Specificity Depth
    {
        "pattern_id": "specificity_deep",
        "category": "specificity",
        "name": "Deep Detail Request",
        "description": "User wants extensive detail",
        "regex_patterns": [
            r"(?:in )?(?:great|extensive|full).*detail",
            r"(?:explain|show|describe).*(?:everything|all|each step)",
            r"(?:step.?by.?step|line.?by.?line)",
            r"(?:don'?t|do not).*(?:skip|omit|leave out)",
        ],
        "example_phrases": [
            "explain in great detail",
            "step by step walkthrough",
        ],
        "injection_template": "📋 DETAILED MODE: Full explanations. Show reasoning. Don't assume knowledge.",
    },
]


class PromptPatternAnalyzer:
    """
    Analyzer for user prompt patterns with outcome correlation.

    Extracts effective communication patterns from user prompts,
    correlates them with session outcomes, and generates wisdom
    injections for the "C" variant in A/B/C testing.
    """

    def __init__(self):
        self.db = self._init_db()
        self._ensure_builtin_patterns()
        self._compile_patterns()

    def _init_db(self) -> sqlite3.Connection:
        """Initialize prompt pattern tables in existing database."""
        db = sqlite3.connect(str(EVOLUTION_DB), check_same_thread=False)

        # Prompt patterns table
        db.execute("""
            CREATE TABLE IF NOT EXISTS prompt_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_id TEXT UNIQUE NOT NULL,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                regex_patterns TEXT NOT NULL,
                example_phrases TEXT,
                injection_template TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                is_protected INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT DEFAULT 'system'
            )
        """)

        # Session prompt analysis table - tracks patterns used in each session
        db.execute("""
            CREATE TABLE IF NOT EXISTS prompt_session_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                prompt_text_preview TEXT,
                patterns_detected TEXT,
                injection_generated TEXT,
                injection_used INTEGER DEFAULT 0,
                risk_level TEXT,
                analyzed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Pattern outcomes - tracks outcomes correlated with patterns
        db.execute("""
            CREATE TABLE IF NOT EXISTS prompt_pattern_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                matched_text TEXT,
                match_confidence REAL,
                outcome TEXT,
                outcome_confidence REAL,
                task_completed INTEGER,
                signals TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (pattern_id) REFERENCES prompt_patterns(pattern_id)
            )
        """)

        # Wisdom injection history - tracks generated injections and their outcomes
        db.execute("""
            CREATE TABLE IF NOT EXISTS wisdom_injections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                injection_id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                hook_variant_id TEXT,
                context_signals TEXT,
                patterns_used TEXT,
                injection_text TEXT NOT NULL,
                generation_confidence REAL,
                risk_modifier REAL,
                outcome TEXT,
                outcome_confidence REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Pattern evolution log
        db.execute("""
            CREATE TABLE IF NOT EXISTS prompt_pattern_evolution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                pattern_id TEXT,
                details TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # =================================================================
        # CONTEXTUAL LEARNING TABLES
        # Tracks WHEN patterns work, not just IF they work
        # =================================================================

        # Session context classification - records what type of task each session was
        db.execute("""
            CREATE TABLE IF NOT EXISTS session_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                primary_context TEXT NOT NULL,
                secondary_contexts TEXT,
                context_signals TEXT,
                complexity TEXT,
                risk_level TEXT,
                prompt_length INTEGER,
                prompt_preview TEXT,
                detection_confidence REAL,
                detected_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Contextual pattern outcomes - tracks pattern effectiveness BY CONTEXT
        # This is the key table for learning WHEN to inject patterns
        db.execute("""
            CREATE TABLE IF NOT EXISTS contextual_pattern_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_id TEXT NOT NULL,
                context TEXT NOT NULL,
                session_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                outcome_confidence REAL,
                complexity TEXT,
                risk_level TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (pattern_id) REFERENCES prompt_patterns(pattern_id)
            )
        """)

        # Context-pattern affinity scores - learned associations
        # Auto-updated based on contextual_pattern_outcomes
        db.execute("""
            CREATE TABLE IF NOT EXISTS context_pattern_affinity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_id TEXT NOT NULL,
                context TEXT NOT NULL,
                affinity_score REAL DEFAULT 0.5,
                total_uses INTEGER DEFAULT 0,
                positive_uses INTEGER DEFAULT 0,
                negative_uses INTEGER DEFAULT 0,
                last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
                confidence_level TEXT DEFAULT 'insufficient',
                UNIQUE(pattern_id, context)
            )
        """)

        # Indexes for performance
        db.execute("CREATE INDEX IF NOT EXISTS idx_pattern_outcomes_pattern ON prompt_pattern_outcomes(pattern_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_pattern_outcomes_session ON prompt_pattern_outcomes(session_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_session_analysis_session ON prompt_session_analysis(session_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_wisdom_injections_session ON wisdom_injections(session_id)")

        # Contextual learning indexes
        db.execute("CREATE INDEX IF NOT EXISTS idx_session_context_session ON session_context(session_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_session_context_primary ON session_context(primary_context)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_contextual_outcomes_pattern ON contextual_pattern_outcomes(pattern_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_contextual_outcomes_context ON contextual_pattern_outcomes(context)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_context_affinity_pattern ON context_pattern_affinity(pattern_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_context_affinity_context ON context_pattern_affinity(context)")

        # Learned/discovered contexts table (self-evolving system)
        db.execute("""
            CREATE TABLE IF NOT EXISTS learned_contexts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                context_name TEXT UNIQUE NOT NULL,
                description TEXT,
                detection_patterns TEXT NOT NULL,
                discovered_from TEXT,
                is_active INTEGER DEFAULT 1,
                is_verified INTEGER DEFAULT 0,
                usage_count INTEGER DEFAULT 0,
                effectiveness_score REAL DEFAULT 0.5,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT DEFAULT 'auto'
            )
        """)

        db.commit()
        return db

    def _ensure_builtin_patterns(self):
        """Ensure all builtin patterns exist in database."""
        for pattern_data in BUILTIN_PATTERNS:
            cursor = self.db.execute(
                "SELECT pattern_id FROM prompt_patterns WHERE pattern_id = ?",
                (pattern_data["pattern_id"],)
            )
            if cursor.fetchone():
                continue

            self.db.execute("""
                INSERT INTO prompt_patterns
                (pattern_id, category, name, description, regex_patterns,
                 example_phrases, injection_template, is_protected)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                pattern_data["pattern_id"],
                pattern_data["category"],
                pattern_data["name"],
                pattern_data["description"],
                json.dumps(pattern_data["regex_patterns"]),
                json.dumps(pattern_data["example_phrases"]),
                pattern_data["injection_template"]
            ))

            self._log_event("builtin_pattern_created", pattern_data["pattern_id"],
                           f"Created builtin pattern: {pattern_data['name']}")

        self.db.commit()

    def _compile_patterns(self):
        """Compile regex patterns for efficient matching."""
        self._compiled_patterns: Dict[str, List[re.Pattern]] = {}

        cursor = self.db.execute("""
            SELECT pattern_id, regex_patterns FROM prompt_patterns WHERE is_active = 1
        """)

        for row in cursor.fetchall():
            pattern_id = row[0]
            regex_list = json.loads(row[1]) if row[1] else []
            self._compiled_patterns[pattern_id] = [
                re.compile(r, re.IGNORECASE) for r in regex_list
            ]

    def _log_event(self, event_type: str, pattern_id: str = None, details: str = ""):
        """Log a pattern evolution event."""
        self.db.execute("""
            INSERT INTO prompt_pattern_evolution_log (event_type, pattern_id, details)
            VALUES (?, ?, ?)
        """, (event_type, pattern_id, details))
        self.db.commit()

    # =========================================================================
    # PATTERN EXTRACTION
    # =========================================================================

    def extract_patterns(self, prompt_text: str) -> List[PatternMatch]:
        """
        Extract all matching patterns from a user prompt.

        Args:
            prompt_text: The user's prompt text

        Returns:
            List of PatternMatch objects for all detected patterns
        """
        matches = []
        prompt_lower = prompt_text.lower()

        for pattern_id, compiled_list in self._compiled_patterns.items():
            for regex in compiled_list:
                match = regex.search(prompt_lower)
                if match:
                    # Get pattern category
                    cursor = self.db.execute(
                        "SELECT category FROM prompt_patterns WHERE pattern_id = ?",
                        (pattern_id,)
                    )
                    row = cursor.fetchone()
                    category = row[0] if row else "unknown"

                    # Calculate confidence based on match specificity
                    matched_text = match.group(0)
                    confidence = min(1.0, len(matched_text) / 20)  # Longer matches = higher confidence

                    matches.append(PatternMatch(
                        pattern_id=pattern_id,
                        category=category,
                        matched_text=matched_text,
                        confidence=confidence,
                        position=match.start()
                    ))
                    break  # Only match each pattern once

        # Sort by position
        matches.sort(key=lambda m: m.position)

        return matches

    def get_pattern(self, pattern_id: str) -> Optional[PromptPattern]:
        """Get a specific pattern by ID."""
        cursor = self.db.execute("""
            SELECT pattern_id, category, name, description, regex_patterns,
                   example_phrases, injection_template, is_active, is_protected,
                   created_at, created_by
            FROM prompt_patterns WHERE pattern_id = ?
        """, (pattern_id,))

        row = cursor.fetchone()
        if not row:
            return None

        return PromptPattern(
            pattern_id=row[0],
            category=row[1],
            name=row[2],
            description=row[3] or "",
            regex_patterns=json.loads(row[4]) if row[4] else [],
            example_phrases=json.loads(row[5]) if row[5] else [],
            injection_template=row[6],
            is_active=bool(row[7]),
            is_protected=bool(row[8]),
            created_at=row[9] or "",
            created_by=row[10] or "system"
        )

    def list_patterns(self, category: str = None, active_only: bool = True) -> List[PromptPattern]:
        """List all patterns, optionally filtered."""
        query = "SELECT pattern_id FROM prompt_patterns WHERE 1=1"
        params = []

        if active_only:
            query += " AND is_active = 1"
        if category:
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY category, name"

        cursor = self.db.execute(query, params)
        return [self.get_pattern(row[0]) for row in cursor.fetchall() if self.get_pattern(row[0])]

    # =========================================================================
    # SESSION TRACKING
    # =========================================================================

    def record_session_patterns(self, session_id: str, prompt_text: str,
                                 risk_level: str = "") -> List[PatternMatch]:
        """
        Analyze a prompt and record detected patterns for the session.

        Args:
            session_id: Session identifier
            prompt_text: The user's prompt
            risk_level: Detected risk level from main hook

        Returns:
            List of detected patterns
        """
        # Extract patterns
        patterns = self.extract_patterns(prompt_text)

        # Generate hash for deduplication
        prompt_hash = hashlib.md5(prompt_text.encode()).hexdigest()[:16]

        # Record analysis
        self.db.execute("""
            INSERT INTO prompt_session_analysis
            (session_id, prompt_hash, prompt_text_preview, patterns_detected, risk_level)
            VALUES (?, ?, ?, ?, ?)
        """, (
            session_id,
            prompt_hash,
            prompt_text[:200],
            json.dumps([{"id": p.pattern_id, "cat": p.category, "conf": p.confidence} for p in patterns]),
            risk_level
        ))
        self.db.commit()

        return patterns

    def attribute_outcome(self, session_id: str, outcome: str,
                          confidence: float = 0.5, task_completed: bool = None,
                          signals: List[str] = None):
        """
        Attribute an outcome to all patterns used in a session.

        Args:
            session_id: Session identifier
            outcome: 'positive', 'negative', 'neutral'
            confidence: 0.0-1.0 confidence in the outcome
            task_completed: Whether task was completed
            signals: Signals that determined outcome
        """
        # Get patterns from session analysis
        cursor = self.db.execute("""
            SELECT patterns_detected FROM prompt_session_analysis
            WHERE session_id = ?
        """, (session_id,))

        for row in cursor.fetchall():
            patterns = json.loads(row[0]) if row[0] else []
            for p in patterns:
                self.db.execute("""
                    INSERT INTO prompt_pattern_outcomes
                    (pattern_id, session_id, match_confidence, outcome,
                     outcome_confidence, task_completed, signals)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    p["id"],
                    session_id,
                    p["conf"],
                    outcome,
                    confidence,
                    1 if task_completed else (0 if task_completed is False else None),
                    json.dumps(signals or [])
                ))

        self.db.commit()

        self._log_event("outcomes_attributed", details=f"Session {session_id}: {outcome}")

    def detect_objective_outcome(self, work_entries: List[Dict]) -> Optional[Tuple[str, float, List[str]]]:
        """
        Use objective_success.py infrastructure to detect outcomes from work stream.

        Integrates with the existing objective success detection system rather
        than duplicating detection logic.

        Args:
            work_entries: List of work log entries

        Returns:
            Tuple of (outcome: 'positive'/'negative'/'neutral', confidence, signals) or None
        """
        try:
            from memory.objective_success import (
                ObjectiveSuccessDetector,
                USER_CONFIRMATION_PATTERNS,
                USER_REJECTION_PATTERNS,
                SYSTEM_SUCCESS_PATTERNS,
                SYSTEM_FAILURE_PATTERNS,
            )

            detector = ObjectiveSuccessDetector()
            successes = detector.analyze_entries(work_entries)

            # Check for high-confidence successes
            if successes:
                best_success = max(successes, key=lambda s: s.confidence)
                if best_success.confidence >= 0.7:
                    return ('positive', best_success.confidence, best_success.evidence)

            # Check for objective successes without user confirmation
            objective_wins = detector.get_objective_successes_without_user(min_confidence=0.75)
            if objective_wins:
                best_obj = max(objective_wins, key=lambda s: s.confidence)
                return ('positive', best_obj.confidence, best_obj.evidence)

            # Check for failures
            for entry in work_entries:
                content = entry.get("content", "").lower()
                for pattern, signal in SYSTEM_FAILURE_PATTERNS:
                    if re.search(pattern, content, re.I):
                        return ('negative', 0.7, [signal])

            # Check for user rejection
            for entry in work_entries:
                if entry.get("source") == "user":
                    content = entry.get("content", "").lower()
                    for pattern, signal in USER_REJECTION_PATTERNS:
                        if re.search(pattern, content, re.I):
                            return ('negative', 0.8, [signal])

            # No clear signal
            return ('neutral', 0.3, ['no_clear_signal'])

        except ImportError:
            # Fallback to basic detection
            return self._basic_outcome_detection(work_entries)

    def _basic_outcome_detection(self, work_entries: List[Dict]) -> Optional[Tuple[str, float, List[str]]]:
        """Basic fallback outcome detection if objective_success not available."""
        positive_signals = [
            (r"\bsuccess\b", "success_keyword"),
            (r"\bthat worked\b", "user_confirmed"),
            (r"\bperfect\b", "user_confirmed"),
        ]
        negative_signals = [
            (r"\berror\b", "error_keyword"),
            (r"\bfailed\b", "failed_keyword"),
            (r"\bthat didn'?t work\b", "user_rejected"),
        ]

        for entry in work_entries:
            content = entry.get("content", "").lower()
            source = entry.get("source", "")

            if source == "user":
                for pattern, signal in positive_signals:
                    if re.search(pattern, content, re.I):
                        return ('positive', 0.7, [signal])
                for pattern, signal in negative_signals:
                    if re.search(pattern, content, re.I):
                        return ('negative', 0.7, [signal])

        return ('neutral', 0.3, ['no_clear_signal'])

    # =========================================================================
    # PATTERN STATISTICS & RISK
    # =========================================================================

    def get_pattern_stats(self, pattern_id: str) -> PatternStats:
        """Get outcome statistics for a pattern."""
        cursor = self.db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome = 'positive' THEN 1 ELSE 0 END) as pos,
                SUM(CASE WHEN outcome = 'negative' THEN 1 ELSE 0 END) as neg,
                SUM(CASE WHEN outcome = 'neutral' THEN 1 ELSE 0 END) as neu,
                AVG(match_confidence) as avg_conf
            FROM prompt_pattern_outcomes
            WHERE pattern_id = ?
        """, (pattern_id,))

        row = cursor.fetchone()
        total = row[0] or 0
        pos = row[1] or 0
        neg = row[2] or 0
        neu = row[3] or 0
        avg_conf = row[4] or 0.5

        pos_rate = pos / total if total > 0 else 0.0

        # Determine effectiveness
        if total < 5:
            effectiveness = "unknown"
        elif pos_rate >= 0.7:
            effectiveness = "highly_effective"
        elif pos_rate >= 0.5:
            effectiveness = "effective"
        elif pos_rate >= 0.3:
            effectiveness = "neutral"
        else:
            effectiveness = "ineffective"

        # Experience level
        if total == 0:
            exp_level = "no_data"
        elif total < 5:
            exp_level = "limited"
        elif total < 15:
            exp_level = "moderate"
        elif total < 50:
            exp_level = "good"
        else:
            exp_level = "extensive"

        return PatternStats(
            pattern_id=pattern_id,
            total_sessions=total,
            positive_outcomes=pos,
            negative_outcomes=neg,
            neutral_outcomes=neu,
            positive_rate=pos_rate,
            effectiveness=effectiveness,
            experience_level=exp_level,
            avg_confidence_when_used=avg_conf
        )

    def get_pattern_risk_modifier(self, pattern_id: str) -> Tuple[float, str]:
        """
        Calculate risk modifier for a pattern based on outcomes.

        Returns:
            Tuple of (risk_modifier, explanation)
            - Negative modifier = pattern reduces risk (it helps)
            - Positive modifier = pattern increases risk (it hurts)
        """
        stats = self.get_pattern_stats(pattern_id)

        if stats.total_sessions == 0:
            return 0.0, "No outcome data for this pattern"

        # Weight based on experience
        exp_weight = {
            "limited": 0.3,
            "moderate": 0.6,
            "good": 0.85,
            "extensive": 1.0,
        }.get(stats.experience_level, 0.0)

        neg_rate = stats.negative_outcomes / stats.total_sessions
        pos_rate = stats.positive_rate

        # Calculate modifier (inverted from hook logic - here positive patterns REDUCE risk)
        if stats.effectiveness == "highly_effective":
            base_modifier = -0.3
            explanation = f"Highly effective pattern ({pos_rate:.0%} positive over {stats.total_sessions} sessions)"
        elif stats.effectiveness == "effective":
            base_modifier = -0.15
            explanation = f"Effective pattern ({pos_rate:.0%} positive over {stats.total_sessions} sessions)"
        elif stats.effectiveness == "ineffective":
            base_modifier = 0.2
            explanation = f"Ineffective pattern ({pos_rate:.0%} positive over {stats.total_sessions} sessions)"
        elif neg_rate >= 0.3:
            base_modifier = 0.25
            explanation = f"Pattern correlates with negative outcomes ({neg_rate:.0%} negative)"
        else:
            base_modifier = 0.0
            explanation = f"Neutral pattern ({pos_rate:.0%} positive over {stats.total_sessions} sessions)"

        final_modifier = base_modifier * exp_weight

        if stats.experience_level in ("limited", "moderate"):
            explanation += f" [{stats.experience_level} data]"

        return final_modifier, explanation

    def get_effective_patterns(self, min_sessions: int = 5) -> List[Dict]:
        """Get patterns ordered by effectiveness."""
        cursor = self.db.execute("""
            SELECT p.pattern_id, p.name, p.category, p.injection_template,
                   COUNT(*) as total,
                   CAST(SUM(CASE WHEN o.outcome = 'positive' THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as pos_rate
            FROM prompt_patterns p
            JOIN prompt_pattern_outcomes o ON p.pattern_id = o.pattern_id
            WHERE p.is_active = 1
            GROUP BY p.pattern_id
            HAVING COUNT(*) >= ?
            ORDER BY pos_rate DESC
        """, (min_sessions,))

        return [
            {
                "pattern_id": row[0],
                "name": row[1],
                "category": row[2],
                "injection_template": row[3],
                "total_sessions": row[4],
                "positive_rate": row[5],
            }
            for row in cursor.fetchall()
        ]

    # =========================================================================
    # CONTEXTUAL LEARNING - LEARN WHEN TO INJECT, NOT JUST WHAT
    # =========================================================================

    def detect_context(self, prompt_text: str, risk_level: str = "") -> DetectedContext:
        """
        Detect the task context from a prompt.

        Uses both built-in and learned context patterns to classify
        what TYPE of work this prompt represents.

        Args:
            prompt_text: The user's prompt
            risk_level: Risk level from main hook (adds context)

        Returns:
            DetectedContext with primary context, secondaries, and signals
        """
        prompt_lower = prompt_text.lower()
        context_scores: Dict[str, Tuple[int, List[str]]] = {}

        # Check built-in context patterns
        for context, patterns in CONTEXT_DETECTION.items():
            matches = []
            for pattern in patterns:
                found = re.findall(pattern, prompt_lower, re.I)
                if found:
                    matches.extend(found if isinstance(found[0], str) else [f[0] for f in found])

            if matches:
                context_scores[context] = (len(matches), matches[:5])  # Cap at 5 examples

        # Check learned contexts (custom user-defined)
        cursor = self.db.execute("""
            SELECT context_name, detection_patterns FROM learned_contexts WHERE is_active = 1
        """)
        for row in cursor.fetchall():
            context_name = row[0]
            patterns = json.loads(row[1]) if row[1] else []
            matches = []
            for pattern in patterns:
                found = re.findall(pattern, prompt_lower, re.I)
                if found:
                    matches.extend(found if isinstance(found[0], str) else [f[0] for f in found])
            if matches:
                context_scores[context_name] = (len(matches), matches[:5])

        # Determine complexity
        complexity = "simple"
        if risk_level in ("critical", "high"):
            complexity = "critical"
        elif len(prompt_text) > 300 or len(context_scores) > 2:
            complexity = "complex"

        # Sort by score
        sorted_contexts = sorted(context_scores.items(), key=lambda x: -x[1][0])

        if not sorted_contexts:
            return DetectedContext(
                primary_context="unknown",
                secondary_contexts=[],
                context_signals={},
                complexity=complexity,
                confidence=0.3
            )

        primary = sorted_contexts[0][0]
        secondaries = [ctx for ctx, _ in sorted_contexts[1:4]]  # Top 3 secondary
        signals = {ctx: matches for ctx, (_, matches) in sorted_contexts}

        # Calculate confidence
        top_score = sorted_contexts[0][1][0]
        confidence = min(1.0, 0.5 + (top_score * 0.1))  # More matches = higher confidence

        return DetectedContext(
            primary_context=primary,
            secondary_contexts=secondaries,
            context_signals=signals,
            complexity=complexity,
            confidence=confidence
        )

    def record_session_context(self, session_id: str, prompt_text: str,
                                risk_level: str = "") -> DetectedContext:
        """
        Detect and record the context for a session.

        Args:
            session_id: Session identifier
            prompt_text: User's prompt
            risk_level: Detected risk level

        Returns:
            The detected context
        """
        context = self.detect_context(prompt_text, risk_level)

        self.db.execute("""
            INSERT INTO session_context
            (session_id, primary_context, secondary_contexts, context_signals,
             complexity, risk_level, prompt_length, prompt_preview, detection_confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id,
            context.primary_context,
            json.dumps(context.secondary_contexts),
            json.dumps(context.context_signals),
            context.complexity,
            risk_level,
            len(prompt_text),
            prompt_text[:200],
            context.confidence
        ))
        self.db.commit()

        return context

    def attribute_contextual_outcome(self, session_id: str, outcome: str,
                                      confidence: float = 0.5):
        """
        Attribute an outcome to patterns used in a session, WITH CONTEXT.

        This is the key method for contextual learning - it records which
        patterns worked in which contexts.

        Args:
            session_id: Session identifier
            outcome: 'positive', 'negative', 'neutral'
            confidence: Outcome confidence
        """
        # Get session context
        cursor = self.db.execute("""
            SELECT primary_context, secondary_contexts, complexity, risk_level
            FROM session_context WHERE session_id = ?
        """, (session_id,))
        ctx_row = cursor.fetchone()

        if not ctx_row:
            # No context recorded, fall back to regular attribution
            self.attribute_outcome(session_id, outcome, confidence)
            return

        primary_context = ctx_row[0]
        secondary_contexts = json.loads(ctx_row[1]) if ctx_row[1] else []
        complexity = ctx_row[2]
        risk_level = ctx_row[3]

        # All contexts this session touched
        all_contexts = [primary_context] + secondary_contexts

        # Get patterns from session
        cursor = self.db.execute("""
            SELECT patterns_detected FROM prompt_session_analysis
            WHERE session_id = ?
        """, (session_id,))

        for row in cursor.fetchall():
            patterns = json.loads(row[0]) if row[0] else []
            for p in patterns:
                pattern_id = p["id"]

                # Record contextual outcome for EACH context
                for context in all_contexts:
                    self.db.execute("""
                        INSERT INTO contextual_pattern_outcomes
                        (pattern_id, context, session_id, outcome, outcome_confidence,
                         complexity, risk_level)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        pattern_id, context, session_id, outcome,
                        confidence, complexity, risk_level
                    ))

                    # Update affinity score
                    self._update_context_affinity(pattern_id, context, outcome)

        # Also attribute to regular (non-contextual) outcomes
        self.attribute_outcome(session_id, outcome, confidence)

        self.db.commit()
        self._log_event("contextual_outcome_attributed", details=f"Session {session_id}: {outcome} in {primary_context}")

    def _update_context_affinity(self, pattern_id: str, context: str, outcome: str):
        """Update the affinity score between a pattern and context."""
        # Get current stats
        cursor = self.db.execute("""
            SELECT total_uses, positive_uses, negative_uses
            FROM context_pattern_affinity
            WHERE pattern_id = ? AND context = ?
        """, (pattern_id, context))

        row = cursor.fetchone()
        if row:
            total = row[0] + 1
            positive = row[1] + (1 if outcome == 'positive' else 0)
            negative = row[2] + (1 if outcome == 'negative' else 0)
        else:
            total = 1
            positive = 1 if outcome == 'positive' else 0
            negative = 1 if outcome == 'negative' else 0

        # Calculate affinity score (positive rate with experience weighting)
        if total > 0:
            raw_score = positive / total
            # Weight by experience - more data = more trust in score
            exp_weight = min(1.0, total / 20)  # Full trust at 20 samples
            affinity = 0.5 + (raw_score - 0.5) * exp_weight
        else:
            affinity = 0.5

        # Determine confidence level
        if total < 3:
            conf_level = "insufficient"
        elif total < 8:
            conf_level = "low"
        elif total < 20:
            conf_level = "medium"
        else:
            conf_level = "high"

        # Upsert
        self.db.execute("""
            INSERT INTO context_pattern_affinity
            (pattern_id, context, affinity_score, total_uses, positive_uses, negative_uses,
             confidence_level, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(pattern_id, context) DO UPDATE SET
                affinity_score = excluded.affinity_score,
                total_uses = excluded.total_uses,
                positive_uses = excluded.positive_uses,
                negative_uses = excluded.negative_uses,
                confidence_level = excluded.confidence_level,
                last_updated = excluded.last_updated
        """, (pattern_id, context, affinity, total, positive, negative, conf_level))

    def get_contextual_effectiveness(self, pattern_id: str, context: str) -> ContextualMatch:
        """
        Get how effective a pattern is in a specific context.

        This is the key query for contextual injection decisions.
        """
        cursor = self.db.execute("""
            SELECT total_uses, positive_uses, negative_uses, affinity_score, confidence_level
            FROM context_pattern_affinity
            WHERE pattern_id = ? AND context = ?
        """, (pattern_id, context))

        row = cursor.fetchone()
        if not row:
            return ContextualMatch(
                pattern_id=pattern_id,
                context=context,
                total_uses=0,
                positive_outcomes=0,
                negative_outcomes=0,
                positive_rate=0.5,  # Neutral default
                confidence_level="insufficient"
            )

        total = row[0] or 0
        pos = row[1] or 0
        neg = row[2] or 0

        return ContextualMatch(
            pattern_id=pattern_id,
            context=context,
            total_uses=total,
            positive_outcomes=pos,
            negative_outcomes=neg,
            positive_rate=pos / total if total > 0 else 0.5,
            confidence_level=row[4] or "insufficient"
        )

    def get_best_patterns_for_context(self, context: str, min_uses: int = 3) -> List[Dict]:
        """
        Get patterns ranked by effectiveness for a specific context.

        This powers contextual wisdom injection - finding which patterns
        work best for THIS type of task.
        """
        cursor = self.db.execute("""
            SELECT p.pattern_id, p.name, p.category, p.injection_template,
                   a.affinity_score, a.total_uses, a.positive_uses, a.confidence_level
            FROM prompt_patterns p
            JOIN context_pattern_affinity a ON p.pattern_id = a.pattern_id
            WHERE a.context = ? AND a.total_uses >= ? AND p.is_active = 1
            ORDER BY a.affinity_score DESC
        """, (context, min_uses))

        return [
            {
                "pattern_id": row[0],
                "name": row[1],
                "category": row[2],
                "injection_template": row[3],
                "affinity_score": row[4],
                "total_uses": row[5],
                "positive_uses": row[6],
                "confidence_level": row[7],
            }
            for row in cursor.fetchall()
        ]

    def get_context_pattern_matrix(self) -> Dict[str, Dict[str, float]]:
        """
        Get full matrix of context-pattern affinities.

        Returns dict: {context: {pattern_id: affinity_score}}
        """
        cursor = self.db.execute("""
            SELECT context, pattern_id, affinity_score
            FROM context_pattern_affinity
            WHERE total_uses >= 3
            ORDER BY context, affinity_score DESC
        """)

        matrix: Dict[str, Dict[str, float]] = {}
        for row in cursor.fetchall():
            context, pattern_id, score = row
            if context not in matrix:
                matrix[context] = {}
            matrix[context][pattern_id] = score

        return matrix

    # =========================================================================
    # SELF-EVOLVING SYSTEM - AUTO-DISCOVER NEW CONTEXTS AND PATTERNS
    # =========================================================================

    def discover_new_context(self, prompt_text: str, session_id: str,
                              suggested_name: str = None) -> Optional[str]:
        """
        Auto-discover a new context category from unclassified prompts.

        When prompts consistently don't match existing contexts but share
        common keywords, the system can propose new context categories.

        Args:
            prompt_text: The prompt that didn't match well
            session_id: Session for attribution
            suggested_name: Optional suggested name for the context

        Returns:
            New context name if created, None otherwise
        """
        # Extract significant words (nouns, verbs) from prompt
        # Simple extraction: words > 4 chars, not common words
        common_words = {
            'the', 'and', 'that', 'this', 'with', 'from', 'have', 'will',
            'what', 'when', 'where', 'which', 'would', 'could', 'should',
            'about', 'there', 'their', 'these', 'those', 'other', 'after',
            'before', 'being', 'between', 'please', 'help', 'need', 'want',
            'make', 'like', 'just', 'also', 'some', 'more', 'very', 'than'
        }

        words = re.findall(r'\b[a-z]{4,}\b', prompt_text.lower())
        significant = [w for w in words if w not in common_words]

        if len(significant) < 3:
            return None

        # Check if these words appear frequently in unclassified sessions
        # (This would require more historical data; for now, just record potential)
        if not suggested_name:
            # Auto-generate name from most frequent significant words
            from collections import Counter
            word_freq = Counter(significant)
            top_words = [w for w, _ in word_freq.most_common(2)]
            suggested_name = "_".join(top_words)

        # Check if context already exists
        cursor = self.db.execute(
            "SELECT context_name FROM learned_contexts WHERE context_name = ?",
            (suggested_name,)
        )
        if cursor.fetchone():
            return None

        # Create detection patterns from significant words
        patterns = [rf"\b{word}\b" for word in significant[:5]]

        try:
            self.db.execute("""
                INSERT INTO learned_contexts
                (context_name, description, detection_patterns, discovered_from, created_by)
                VALUES (?, ?, ?, ?, 'auto_discovery')
            """, (
                suggested_name,
                f"Auto-discovered context from session {session_id}",
                json.dumps(patterns),
                session_id
            ))
            self.db.commit()

            self._log_event("context_discovered", details=f"New context: {suggested_name}")
            return suggested_name

        except sqlite3.IntegrityError:
            return None

    def propose_new_pattern(self, matched_text: str, category: str,
                            outcome: str, context: str = None) -> Optional[str]:
        """
        Propose a new pattern based on observed effective phrases.

        When users consistently use certain phrases that correlate with
        positive outcomes, the system can propose them as new patterns.

        Args:
            matched_text: The phrase observed in user prompts
            category: Suggested category for the pattern
            outcome: The outcome associated with this phrase
            context: Optional context where this pattern was effective

        Returns:
            New pattern ID if created, None otherwise
        """
        # Only propose from positive outcomes
        if outcome != 'positive':
            return None

        # Check if this phrase is already covered by existing patterns
        existing = self.extract_patterns(matched_text)
        if existing:
            return None  # Already matched

        # Create a pattern from this phrase
        # Escape regex special chars and allow some flexibility
        escaped = re.escape(matched_text.lower())
        # Allow word boundaries and minor variations
        pattern = rf"\b{escaped}\b"

        # Generate pattern ID
        pattern_id = f"auto_{hashlib.md5(matched_text.encode()).hexdigest()[:8]}"

        # Create injection template based on category
        category_templates = {
            "vision_anchoring": "🎯 VISION: {phrase} - Hold this outcome firmly in mind.",
            "stakes_framing": "⚠️ STAKES: {phrase} - This context demands careful execution.",
            "quality_signals": "🔍 QUALITY: {phrase} - Apply this standard throughout.",
            "persona_priming": "🧠 MODE: {phrase} - Operate at this level.",
            "constraint_setting": "✂️ SCOPE: {phrase} - Maintain these boundaries.",
            "collaboration": "🤝 COLLAB: {phrase} - Work in this spirit.",
            "urgency": "⏰ PACE: {phrase} - Move accordingly.",
            "specificity": "📋 DETAIL: {phrase} - Deliver at this granularity.",
        }

        template = category_templates.get(
            category,
            f"💡 {matched_text[:30]}... - Apply this user wisdom."
        ).format(phrase=matched_text[:50])

        try:
            self.db.execute("""
                INSERT INTO prompt_patterns
                (pattern_id, category, name, description, regex_patterns,
                 example_phrases, injection_template, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'auto_discovery')
            """, (
                pattern_id,
                category,
                f"Auto: {matched_text[:30]}...",
                f"Auto-discovered pattern from effective user phrase",
                json.dumps([pattern]),
                json.dumps([matched_text]),
                template
            ))
            self.db.commit()

            # Recompile patterns
            self._compile_patterns()

            self._log_event("pattern_proposed", pattern_id,
                           f"Auto-discovered from: {matched_text[:50]}")
            return pattern_id

        except sqlite3.IntegrityError:
            return None

    def analyze_unmatched_phrases(self, prompt_text: str, outcome: str,
                                   context: str) -> List[str]:
        """
        Find potentially valuable phrases not covered by existing patterns.

        When a session has a positive outcome, analyze what phrases the user
        used that might be worth capturing as new patterns.

        Returns list of proposed pattern IDs created.
        """
        if outcome != 'positive':
            return []

        # Find phrases that look like pattern candidates
        # Look for imperative phrases, quality markers, etc.
        candidate_patterns = [
            # Quality/thoroughness phrases
            (r"(?:be |make it |ensure |please )?(?:very |extremely |super )?\w+(?:ly|ful|ive)", "quality_signals"),
            # Instruction patterns
            (r"(?:don'?t|do not|never|always|must|should) \w+ \w+", "constraint_setting"),
            # Vision phrases
            (r"(?:i want|we need|goal is|aim for) .{10,40}", "vision_anchoring"),
            # Stakes phrases
            (r"(?:critical|crucial|important|vital|essential) .{5,30}", "stakes_framing"),
        ]

        proposed = []
        prompt_lower = prompt_text.lower()

        for pattern, category in candidate_patterns:
            matches = re.findall(pattern, prompt_lower)
            for match in matches[:2]:  # Limit per pattern type
                if len(match) > 8:  # Skip tiny matches
                    # Check if not already covered
                    existing = self.extract_patterns(match)
                    if not existing:
                        pattern_id = self.propose_new_pattern(match, category, outcome, context)
                        if pattern_id:
                            proposed.append(pattern_id)

        return proposed

    def verify_learned_context(self, context_name: str, is_valid: bool) -> bool:
        """
        Verify or reject a learned context.

        User feedback on auto-discovered contexts helps the system learn
        what's valuable to track.
        """
        try:
            if is_valid:
                self.db.execute("""
                    UPDATE learned_contexts SET is_verified = 1 WHERE context_name = ?
                """, (context_name,))
            else:
                self.db.execute("""
                    UPDATE learned_contexts SET is_active = 0 WHERE context_name = ?
                """, (context_name,))
            self.db.commit()
            return True
        except Exception:
            return False

    def get_learning_summary(self) -> Dict:
        """Get summary of the self-evolving learning state."""
        summary = self.get_summary()

        # Add contextual learning stats
        cursor = self.db.execute("SELECT COUNT(*) FROM session_context")
        summary["sessions_with_context"] = cursor.fetchone()[0] or 0

        cursor = self.db.execute("SELECT COUNT(*) FROM contextual_pattern_outcomes")
        summary["contextual_outcomes"] = cursor.fetchone()[0] or 0

        cursor = self.db.execute("SELECT COUNT(*) FROM context_pattern_affinity WHERE total_uses >= 3")
        summary["reliable_affinities"] = cursor.fetchone()[0] or 0

        cursor = self.db.execute("SELECT COUNT(*) FROM learned_contexts WHERE is_active = 1")
        summary["learned_contexts"] = cursor.fetchone()[0] or 0

        cursor = self.db.execute("SELECT COUNT(*) FROM learned_contexts WHERE is_verified = 1")
        summary["verified_contexts"] = cursor.fetchone()[0] or 0

        cursor = self.db.execute("""
            SELECT COUNT(*) FROM prompt_patterns WHERE created_by = 'auto_discovery'
        """)
        summary["auto_discovered_patterns"] = cursor.fetchone()[0] or 0

        return summary

    # =========================================================================
    # WISDOM INJECTION GENERATION (C VARIANT)
    # =========================================================================

    def generate_wisdom_injection(self, prompt_text: str, risk_level: str = "",
                                   session_id: str = None,
                                   max_injections: int = 3,
                                   use_contextual: bool = True) -> Optional[WisdomInjection]:
        """
        Generate a wisdom injection for the C variant.

        Analyzes the prompt context and generates a situationally-appropriate
        injection based on proven user patterns. NOW WITH CONTEXTUAL LEARNING:
        patterns are selected based on how well they work in THIS type of task.

        Args:
            prompt_text: User's prompt text
            risk_level: Detected risk level
            session_id: Optional session ID for tracking
            max_injections: Maximum number of pattern injections to combine
            use_contextual: If True, use contextual affinity scores (default)

        Returns:
            WisdomInjection object or None if no appropriate injection
        """
        # Extract patterns from current prompt
        detected = self.extract_patterns(prompt_text)
        detected_ids = {p.pattern_id for p in detected}

        # Detect the task context
        context = self.detect_context(prompt_text, risk_level)
        primary_context = context.primary_context

        # Record context if we have a session
        if session_id:
            self.record_session_context(session_id, prompt_text, risk_level)

        # CONTEXTUAL APPROACH: Get patterns that work best for THIS context
        candidates = []

        if use_contextual and primary_context != "unknown":
            # First, try contextually-proven patterns
            contextual_patterns = self.get_best_patterns_for_context(
                primary_context, min_uses=3
            )

            for p in contextual_patterns:
                if p["pattern_id"] in detected_ids:
                    continue  # User already included this
                if p["affinity_score"] < 0.55:  # Need decent contextual affinity
                    continue

                # Context-specific patterns get priority
                candidates.append((p, f"Effective in {primary_context} context "
                                   f"({p['affinity_score']:.0%} affinity)"))

        # FALLBACK: If no contextual matches, use global effectiveness
        if not candidates:
            effective = self.get_effective_patterns(min_sessions=5)

            for p in effective:
                if p["pattern_id"] in detected_ids:
                    continue
                if p["positive_rate"] < 0.5:
                    continue

                # Check if this pattern is appropriate for context (heuristic)
                should_include, reason = self._should_include_pattern(
                    p, prompt_text, risk_level, detected
                )
                if should_include:
                    candidates.append((p, reason))

        if not candidates:
            return None

        # Select top patterns (limit to max_injections)
        selected = candidates[:max_injections]

        # Build injection text
        injection_parts = []
        patterns_used = []
        total_confidence = 0.0
        total_risk_mod = 0.0

        for p, reason in selected:
            injection_parts.append(p["injection_template"])
            patterns_used.append(p["pattern_id"])
            total_confidence += p["positive_rate"]

            risk_mod, _ = self.get_pattern_risk_modifier(p["pattern_id"])
            total_risk_mod += risk_mod

        avg_confidence = total_confidence / len(selected)
        avg_risk_mod = total_risk_mod / len(selected)

        # Create injection
        injection_text = "\n".join([
            "═══ USER WISDOM INJECTION (C-Variant) ═══",
            "",
            *injection_parts,
            "",
            "═══════════════════════════════════════════"
        ])

        injection_id = f"inj_{hashlib.md5(injection_text.encode()).hexdigest()[:12]}"

        # Determine context signals
        context_signals = []
        if risk_level:
            context_signals.append(f"risk_level:{risk_level}")
        for p in detected:
            context_signals.append(f"detected:{p.pattern_id}")

        injection = WisdomInjection(
            injection_id=injection_id,
            context_signals=context_signals,
            patterns_used=patterns_used,
            injection_text=injection_text,
            confidence=avg_confidence,
            risk_modifier=avg_risk_mod
        )

        # Record if session_id provided
        if session_id:
            self.db.execute("""
                INSERT INTO wisdom_injections
                (injection_id, session_id, context_signals, patterns_used,
                 injection_text, generation_confidence, risk_modifier)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                injection_id, session_id,
                json.dumps(context_signals),
                json.dumps(patterns_used),
                injection_text,
                avg_confidence,
                avg_risk_mod
            ))
            self.db.commit()

        return injection

    def _should_include_pattern(self, pattern: Dict, prompt_text: str,
                                 risk_level: str, detected: List[PatternMatch]) -> Tuple[bool, str]:
        """
        Determine if a pattern's injection is appropriate for context.

        Uses BOTH learned contextual affinities AND heuristics.
        Contextual learning takes priority when data is available.
        """
        pattern_id = pattern["pattern_id"]
        category = pattern["category"]
        prompt_lower = prompt_text.lower()

        # FIRST: Check contextual learning (if we have enough data)
        context = self.detect_context(prompt_text, risk_level)
        if context.primary_context != "unknown":
            ctx_match = self.get_contextual_effectiveness(pattern_id, context.primary_context)

            # If we have good contextual data, use it
            if ctx_match.confidence_level in ("high", "medium"):
                if ctx_match.positive_rate >= 0.6:
                    return True, f"Contextually proven in {context.primary_context} ({ctx_match.positive_rate:.0%} effective)"
                elif ctx_match.positive_rate < 0.4:
                    return False, f"Contextually poor in {context.primary_context}"
                # Medium effectiveness - fall through to heuristics

        # FALLBACK: Use heuristics when contextual data insufficient
        # Vision anchoring - good for complex tasks
        if category == "vision_anchoring":
            if risk_level in ("critical", "high"):
                return True, "High-stakes task benefits from excellence anchoring"
            if len(prompt_text) > 200:
                return True, "Complex prompt benefits from outcome vision"

        # Stakes framing - good when user hasn't emphasized stakes
        if category == "stakes_framing":
            if risk_level == "critical" and not any(p.category == "stakes_framing" for p in detected):
                return True, "Critical risk without stakes awareness"

        # Quality signals - good for risky operations
        if category == "quality_signals":
            if risk_level in ("critical", "high", "moderate"):
                if not any(p.category == "quality_signals" for p in detected):
                    return True, "Risky operation without quality signals"

        # Persona priming - good for architecture work
        if category == "persona_priming":
            if any(kw in prompt_lower for kw in ["architect", "design", "refactor", "system"]):
                return True, "Architecture work benefits from expert mode"

        # Constraint setting - good when scope is broad
        if category == "constraint_setting":
            if len(prompt_text) > 300 and not any(p.category == "constraint_setting" for p in detected):
                return True, "Broad task without scope constraints"

        # Collaboration - good for exploratory work
        if category == "collaboration":
            if any(kw in prompt_lower for kw in ["help", "figure out", "explore", "investigate"]):
                return True, "Exploratory work benefits from collaboration mode"

        return False, ""

    def attribute_injection_outcome(self, session_id: str, outcome: str,
                                     confidence: float = 0.5):
        """Attribute outcome to wisdom injections used in session."""
        self.db.execute("""
            UPDATE wisdom_injections
            SET outcome = ?, outcome_confidence = ?
            WHERE session_id = ?
        """, (outcome, confidence, session_id))
        self.db.commit()

    # =========================================================================
    # PATTERN MANAGEMENT
    # =========================================================================

    def create_pattern(self, category: str, name: str, regex_patterns: List[str],
                       injection_template: str, description: str = "",
                       example_phrases: List[str] = None,
                       created_by: str = "user") -> str:
        """Create a new user-defined pattern."""
        pattern_id = f"user_{hashlib.md5(name.encode()).hexdigest()[:8]}"

        try:
            self.db.execute("""
                INSERT INTO prompt_patterns
                (pattern_id, category, name, description, regex_patterns,
                 example_phrases, injection_template, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pattern_id, category, name, description,
                json.dumps(regex_patterns),
                json.dumps(example_phrases or []),
                injection_template,
                created_by
            ))
            self.db.commit()

            # Recompile patterns
            self._compile_patterns()

            self._log_event("pattern_created", pattern_id, f"User created: {name}")

            return pattern_id

        except sqlite3.IntegrityError:
            return pattern_id

    def protect_pattern(self, pattern_id: str, protected: bool = True) -> bool:
        """Mark a pattern as protected from pruning."""
        try:
            self.db.execute("""
                UPDATE prompt_patterns SET is_protected = ? WHERE pattern_id = ?
            """, (1 if protected else 0, pattern_id))
            self.db.commit()
            return True
        except Exception:
            return False

    def deactivate_pattern(self, pattern_id: str) -> bool:
        """Deactivate a pattern (soft delete)."""
        pattern = self.get_pattern(pattern_id)
        if not pattern:
            return False
        if pattern.is_protected:
            return False

        try:
            self.db.execute("""
                UPDATE prompt_patterns SET is_active = 0 WHERE pattern_id = ?
            """, (pattern_id,))
            self.db.commit()
            self._compile_patterns()
            return True
        except Exception:
            return False

    def get_summary(self) -> Dict:
        """Get summary of prompt pattern system."""
        patterns = self.list_patterns(active_only=False)
        active = sum(1 for p in patterns if p.is_active)
        protected = sum(1 for p in patterns if p.is_protected)

        cursor = self.db.execute("SELECT COUNT(*) FROM prompt_pattern_outcomes")
        total_outcomes = cursor.fetchone()[0] or 0

        cursor = self.db.execute("SELECT COUNT(*) FROM wisdom_injections")
        total_injections = cursor.fetchone()[0] or 0

        cursor = self.db.execute("""
            SELECT COUNT(*) FROM wisdom_injections WHERE outcome = 'positive'
        """)
        positive_injections = cursor.fetchone()[0] or 0

        return {
            "total_patterns": len(patterns),
            "active_patterns": active,
            "protected_patterns": protected,
            "total_outcomes": total_outcomes,
            "total_injections": total_injections,
            "positive_injections": positive_injections,
            "injection_success_rate": positive_injections / total_injections if total_injections > 0 else 0,
            "categories": list(set(p.category for p in patterns))
        }


def get_analyzer() -> PromptPatternAnalyzer:
    """Get the singleton PromptPatternAnalyzer instance."""
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = PromptPatternAnalyzer()
    return _analyzer_instance


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    analyzer = get_analyzer()

    if len(sys.argv) < 2:
        print("Prompt Pattern Analyzer")
        print("=" * 50)
        summary = analyzer.get_summary()
        print(f"Active Patterns: {summary['active_patterns']}/{summary['total_patterns']}")
        print(f"Protected: {summary['protected_patterns']}")
        print(f"Total Outcomes: {summary['total_outcomes']}")
        print(f"Wisdom Injections: {summary['total_injections']}")
        if summary['total_injections'] > 0:
            print(f"Injection Success Rate: {summary['injection_success_rate']:.1%}")
        print(f"Categories: {', '.join(summary['categories'])}")
        print()
        print("Commands:")
        print("  python prompt_pattern_analyzer.py list [category]")
        print("  python prompt_pattern_analyzer.py analyze \"prompt text\"")
        print("  python prompt_pattern_analyzer.py stats <pattern_id>")
        print("  python prompt_pattern_analyzer.py effective [min_sessions]")
        print("  python prompt_pattern_analyzer.py inject \"prompt\" [risk_level]")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "list":
        category = sys.argv[2] if len(sys.argv) > 2 else None
        patterns = analyzer.list_patterns(category)

        by_cat = {}
        for p in patterns:
            if p.category not in by_cat:
                by_cat[p.category] = []
            by_cat[p.category].append(p)

        for cat, plist in sorted(by_cat.items()):
            print(f"\n{cat.upper()}:")
            print("-" * 40)
            for p in plist:
                status = "✓" if p.is_active else "✗"
                protected = " [PROTECTED]" if p.is_protected else ""
                print(f"  {status} {p.pattern_id}{protected}")
                print(f"      {p.name}: {p.description[:50]}...")

    elif cmd == "analyze":
        if len(sys.argv) < 3:
            print("Usage: prompt_pattern_analyzer.py analyze \"prompt text\"")
            sys.exit(1)

        prompt = sys.argv[2]
        patterns = analyzer.extract_patterns(prompt)

        print(f"\nAnalyzing: \"{prompt[:100]}...\"")
        print("=" * 50)

        if not patterns:
            print("No patterns detected.")
        else:
            print(f"Detected {len(patterns)} pattern(s):\n")
            for p in patterns:
                pattern = analyzer.get_pattern(p.pattern_id)
                stats = analyzer.get_pattern_stats(p.pattern_id)
                print(f"  📌 {p.pattern_id} ({p.category})")
                print(f"     Match: \"{p.matched_text}\" (conf: {p.confidence:.2f})")
                if pattern:
                    print(f"     Name: {pattern.name}")
                if stats.total_sessions > 0:
                    print(f"     Stats: {stats.positive_rate:.0%} positive over {stats.total_sessions} sessions")
                print()

    elif cmd == "stats":
        if len(sys.argv) < 3:
            print("Usage: prompt_pattern_analyzer.py stats <pattern_id>")
            sys.exit(1)

        pattern_id = sys.argv[2]
        pattern = analyzer.get_pattern(pattern_id)
        stats = analyzer.get_pattern_stats(pattern_id)
        risk_mod, risk_exp = analyzer.get_pattern_risk_modifier(pattern_id)

        if not pattern:
            print(f"Pattern not found: {pattern_id}")
            sys.exit(1)

        print(f"\nPattern: {pattern_id}")
        print("=" * 50)
        print(f"Name: {pattern.name}")
        print(f"Category: {pattern.category}")
        print(f"Description: {pattern.description}")
        print(f"Active: {'Yes' if pattern.is_active else 'No'}")
        print(f"Protected: {'Yes' if pattern.is_protected else 'No'}")
        print()
        print(f"PERFORMANCE:")
        print(f"  Total Sessions: {stats.total_sessions}")
        print(f"  Experience: {stats.experience_level}")
        print(f"  Effectiveness: {stats.effectiveness}")
        if stats.total_sessions > 0:
            print(f"  Positive: {stats.positive_outcomes} ({stats.positive_rate:.1%})")
            print(f"  Negative: {stats.negative_outcomes}")
            print(f"  Neutral: {stats.neutral_outcomes}")
        print()
        print(f"RISK ASSESSMENT:")
        print(f"  Modifier: {risk_mod:+.2f}")
        print(f"  Reason: {risk_exp}")
        print()
        print(f"INJECTION TEMPLATE:")
        print(f"  {pattern.injection_template}")

    elif cmd == "effective":
        min_sessions = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        effective = analyzer.get_effective_patterns(min_sessions)

        print(f"\nMost Effective Patterns (min {min_sessions} sessions)")
        print("=" * 55)

        if not effective:
            print("  No patterns with enough data yet.")
        else:
            for i, p in enumerate(effective, 1):
                bar = "█" * int(p['positive_rate'] * 20)
                print(f"\n  {i}. {p['pattern_id']} ({p['category']})")
                print(f"     {p['name']}")
                print(f"     Positive Rate: [{bar:<20}] {p['positive_rate']:.1%}")
                print(f"     ({p['total_sessions']} sessions)")

    elif cmd == "inject":
        if len(sys.argv) < 3:
            print("Usage: prompt_pattern_analyzer.py inject \"prompt\" [risk_level]")
            sys.exit(1)

        prompt = sys.argv[2]
        risk_level = sys.argv[3] if len(sys.argv) > 3 else ""

        print(f"\nGenerating wisdom injection for:")
        print(f"  Prompt: \"{prompt[:80]}...\"")
        if risk_level:
            print(f"  Risk Level: {risk_level}")
        print()

        injection = analyzer.generate_wisdom_injection(prompt, risk_level)

        if not injection:
            print("No appropriate injection found.")
            print("(Need patterns with 5+ sessions and >50% positive rate)")
        else:
            print(f"Injection ID: {injection.injection_id}")
            print(f"Patterns Used: {', '.join(injection.patterns_used)}")
            print(f"Confidence: {injection.confidence:.1%}")
            print(f"Risk Modifier: {injection.risk_modifier:+.2f}")
            print()
            print(injection.injection_text)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
