#!/usr/bin/env python3
"""
HOOK EVOLUTION ENGINE

Applies experience-based learning to Context DNA hooks themselves.
Enables A/B testing of hook variants and objective outcome tracking.

PHILOSOPHY:
The same rigor applied to success pattern detection should apply to
the hooks that inject context. If a hook variant consistently leads
to worse outcomes, we should know and adapt.

A/B TESTING STRATEGY:
- Control: Current production hook (baseline)
- Variant A: Conservative changes (minor tweaks, phrasing adjustments)
- Variant B: Experimental changes (new approaches, structural changes)

OUTCOME TRACKING:
- Positive: Task completed, user confirmed, no retries needed
- Negative: Multiple retries, user frustration, errors after hook
- Neutral: No measurable impact

HOOK TYPES:
- UserPromptSubmit: Pre-prompt context injection
- PostToolUse: Win capture after tool execution
- SessionEnd: Session summary generation
- GitPostCommit: Auto-learning from commits

Usage:
    from memory.hook_evolution import HookEvolutionEngine, get_hook_evolution_engine

    engine = get_hook_evolution_engine()
    variant = engine.get_active_variant('UserPromptSubmit', session_id)
    # ... later ...
    engine.record_outcome(variant.variant_id, session_id, 'positive', ['task_completed'])
"""

import json
import sqlite3
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field, asdict
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
_engine_instance = None


class HookType(Enum):
    """Available hook types in Context DNA."""
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    POST_TOOL_USE = "PostToolUse"
    SESSION_END = "SessionEnd"
    GIT_POST_COMMIT = "GitPostCommit"


class OutcomeType(Enum):
    """Possible outcomes after a hook fires."""
    POSITIVE = "positive"      # Hook clearly helped
    NEGATIVE = "negative"      # Hook clearly hurt (more retries, errors)
    NEUTRAL = "neutral"        # No measurable impact
    UNKNOWN = "unknown"        # Can't determine


class ChangeMagnitude(Enum):
    """How dramatic the change is from baseline."""
    BASELINE = "baseline"      # The original/control
    MINOR = "minor"            # Conservative "A" variant - small tweaks
    MAJOR = "major"            # Experimental "B" variant - dramatic changes
    WISDOM = "wisdom"          # "C" variant - user-learned wisdom injection


@dataclass
class HookVariant:
    """A specific variant of a hook configuration."""
    variant_id: str
    hook_type: str
    name: str
    description: str
    config: Dict
    is_active: bool = True
    is_default: bool = False
    is_protected: bool = False
    ab_group: Optional[str] = None
    ab_test_id: Optional[str] = None
    change_magnitude: str = "minor"
    selection_weight: float = 1.0
    parent_variant_id: Optional[str] = None
    version: int = 1
    created_at: str = ""
    created_by: str = "system"


@dataclass
class HookOutcome:
    """Recorded outcome after a hook fired."""
    variant_id: str
    session_id: str
    outcome: str
    confidence: float
    signals: List[str]
    task_completed: Optional[bool] = None
    retry_count: int = 0
    time_to_completion_ms: Optional[int] = None
    trigger_context: str = ""
    risk_level: str = ""
    area: str = ""


@dataclass
class VariantStats:
    """Performance statistics for a variant."""
    variant_id: str
    total_outcomes: int
    positive_count: int
    negative_count: int
    neutral_count: int
    positive_rate: float
    negative_rate: float
    avg_retry_count: float
    avg_time_ms: Optional[float]
    experience_level: str


@dataclass
class ABTest:
    """A/B/C test configuration."""
    test_id: str
    test_name: str
    hook_type: str
    control_variant_id: str
    variant_a_id: Optional[str]  # Minor/conservative changes
    variant_b_id: Optional[str]  # Major/experimental changes
    variant_c_id: Optional[str] = None  # Wisdom injection (dynamic)
    status: str = "draft"  # draft, running, paused, completed
    winner_variant_id: Optional[str] = None
    min_samples_per_variant: int = 30
    confidence_threshold: float = 0.95
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    wisdom_injection_enabled: bool = False  # If True, C variant uses dynamic injection


# Default hook configurations - these are the system defaults
DEFAULT_HOOK_CONFIGS = {
    "UserPromptSubmit": {
        "version": 1,
        "layers": {
            "professor": {"enabled": True, "for_risk": ["critical", "high", "moderate"]},
            "brain_learnings": {"enabled": True, "for_risk": ["critical", "high", "moderate", "low"]},
            "gotcha_check": {"enabled": True, "for_risk": ["critical", "high", "moderate"]},
            "blueprint": {"enabled": True, "for_risk": ["critical", "high"]},
            "brain_state": {"enabled": True, "for_risk": ["critical"]}
        },
        "risk_keywords": {
            "critical": ["destroy", "migration.*prod", "schema.*change", "auth.*system"],
            "high": ["deploy", "terraform", "migration", "refactor", "ecs.*service"],
            "moderate": ["docker", "config", "env", "toggle", "health", "sync"],
            "low": ["admin", "dashboard", "display", "show", "add.*button"]
        },
        "result_limits": {"critical": 60, "high": 40, "moderate": 25, "low": 10}
    },
    "PostToolUse": {
        "version": 1,
        "capture_patterns": True,
        "success_detection": True,
        "win_attribution": True
    },
    "SessionEnd": {
        "version": 1,
        "summary_generation": True,
        "pattern_extraction": True,
        "outcome_finalization": True
    },
    "GitPostCommit": {
        "version": 1,
        "infrastructure_detection": True,
        "artifact_verification": True,
        "sop_extraction": True
    }
}

# Protected aspects that cannot be auto-pruned
PROTECTED_ASPECTS = {
    "UserPromptSubmit": ["layers.gotcha_check"],   # Always warn about gotchas
    "PostToolUse": ["capture_patterns"],            # Always capture wins
    "SessionEnd": ["summary_generation"],           # Always summarize
    "GitPostCommit": ["infrastructure_detection"]   # Always detect infra
}


class HookEvolutionEngine:
    """
    Engine for hook variant evolution and A/B testing.

    Applies the same experience-based learning used for patterns
    to the hooks themselves.
    """

    def __init__(self):
        self.db = self._init_db()
        self._ensure_default_variants()

    def _init_db(self) -> sqlite3.Connection:
        """Initialize hook evolution tables in existing pattern_evolution.db."""
        db = sqlite3.connect(str(EVOLUTION_DB), check_same_thread=False)

        # Hook variants table
        db.execute("""
            CREATE TABLE IF NOT EXISTS hook_variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hook_type TEXT NOT NULL,
                variant_id TEXT UNIQUE NOT NULL,
                variant_name TEXT NOT NULL,
                description TEXT,
                config_json TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                is_default INTEGER DEFAULT 0,
                is_protected INTEGER DEFAULT 0,
                ab_group TEXT,
                ab_test_id TEXT,
                change_magnitude TEXT DEFAULT 'minor',
                selection_weight REAL DEFAULT 1.0,
                parent_variant_id TEXT,
                version INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT DEFAULT 'system'
            )
        """)

        # Hook outcomes table
        db.execute("""
            CREATE TABLE IF NOT EXISTS hook_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variant_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                trigger_context TEXT,
                hook_output_hash TEXT,
                outcome TEXT NOT NULL,
                outcome_signals TEXT,
                confidence REAL DEFAULT 0.5,
                task_completed INTEGER,
                retry_count INTEGER DEFAULT 0,
                time_to_completion_ms INTEGER,
                user_satisfaction TEXT,
                risk_level TEXT,
                area TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (variant_id) REFERENCES hook_variants(variant_id)
            )
        """)

        # Hook A/B/C tests table (extended to support wisdom injection)
        db.execute("""
            CREATE TABLE IF NOT EXISTS hook_ab_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id TEXT UNIQUE NOT NULL,
                test_name TEXT NOT NULL,
                hook_type TEXT NOT NULL,
                control_variant_id TEXT NOT NULL,
                variant_a_id TEXT,
                variant_b_id TEXT,
                variant_c_id TEXT,
                wisdom_injection_enabled INTEGER DEFAULT 0,
                status TEXT DEFAULT 'draft',
                winner_variant_id TEXT,
                min_samples_per_variant INTEGER DEFAULT 30,
                confidence_threshold REAL DEFAULT 0.95,
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Add variant_c_id column if missing (for existing databases)
        try:
            db.execute("ALTER TABLE hook_ab_tests ADD COLUMN variant_c_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Add wisdom_injection_enabled column if missing
        try:
            db.execute("ALTER TABLE hook_ab_tests ADD COLUMN wisdom_injection_enabled INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Hook defaults table
        db.execute("""
            CREATE TABLE IF NOT EXISTS hook_defaults (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hook_type TEXT UNIQUE NOT NULL,
                default_variant_id TEXT NOT NULL,
                default_config_snapshot TEXT NOT NULL,
                last_reverted_at TEXT,
                revert_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Hook evolution log
        db.execute("""
            CREATE TABLE IF NOT EXISTS hook_evolution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                hook_type TEXT,
                variant_id TEXT,
                test_id TEXT,
                details TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Hook firings table - tracks when hooks fire (for outcome attribution)
        db.execute("""
            CREATE TABLE IF NOT EXISTS hook_firings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variant_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                trigger_context TEXT,
                risk_level TEXT,
                fired_at TEXT DEFAULT CURRENT_TIMESTAMP,
                outcome_recorded INTEGER DEFAULT 0
            )
        """)

        # Indexes for performance
        db.execute("CREATE INDEX IF NOT EXISTS idx_hook_outcomes_variant ON hook_outcomes(variant_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_hook_outcomes_session ON hook_outcomes(session_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_hook_firings_session ON hook_firings(session_id)")

        db.commit()
        return db

    def _ensure_default_variants(self):
        """Ensure default variants exist for all hook types."""
        for hook_type, config in DEFAULT_HOOK_CONFIGS.items():
            variant_id = f"{hook_type.lower()}_default"

            # Check if default exists
            cursor = self.db.execute(
                "SELECT variant_id FROM hook_variants WHERE variant_id = ?",
                (variant_id,)
            )
            if cursor.fetchone():
                continue

            # Create default variant
            self.db.execute("""
                INSERT INTO hook_variants
                (hook_type, variant_id, variant_name, description, config_json,
                 is_default, is_protected, change_magnitude)
                VALUES (?, ?, ?, ?, ?, 1, 1, 'baseline')
            """, (
                hook_type,
                variant_id,
                f"{hook_type} Default",
                f"System default configuration for {hook_type} hook",
                json.dumps(config)
            ))

            # Record as default
            self.db.execute("""
                INSERT OR REPLACE INTO hook_defaults
                (hook_type, default_variant_id, default_config_snapshot)
                VALUES (?, ?, ?)
            """, (hook_type, variant_id, json.dumps(config)))

            self._log_event("default_created", hook_type, variant_id,
                           f"Created default variant for {hook_type}")

        self.db.commit()

    def _log_event(self, event_type: str, hook_type: str = None,
                   variant_id: str = None, details: str = "", test_id: str = None):
        """Log an evolution event."""
        self.db.execute("""
            INSERT INTO hook_evolution_log
            (event_type, hook_type, variant_id, test_id, details)
            VALUES (?, ?, ?, ?, ?)
        """, (event_type, hook_type, variant_id, test_id, details))
        self.db.commit()

    # =========================================================================
    # VARIANT MANAGEMENT
    # =========================================================================

    def create_variant(self, hook_type: str, name: str, config: Dict,
                      description: str = "", magnitude: str = "minor",
                      parent_id: str = None, created_by: str = "user") -> str:
        """
        Create a new hook variant.

        Args:
            hook_type: One of UserPromptSubmit, PostToolUse, SessionEnd, GitPostCommit
            name: Human-readable name for the variant
            config: Configuration dict for the hook
            description: What this variant does differently
            magnitude: 'minor' for A variants, 'major' for B variants
            parent_id: If derived from another variant
            created_by: Who created this variant

        Returns:
            The variant_id of the created variant
        """
        # Generate variant ID
        config_hash = hashlib.md5(json.dumps(config, sort_keys=True).encode()).hexdigest()[:8]
        variant_id = f"{hook_type.lower()}_{name.lower().replace(' ', '_')}_{config_hash}"

        # Determine version
        cursor = self.db.execute(
            "SELECT MAX(version) FROM hook_variants WHERE hook_type = ?",
            (hook_type,)
        )
        max_version = cursor.fetchone()[0] or 0
        version = max_version + 1

        try:
            self.db.execute("""
                INSERT INTO hook_variants
                (hook_type, variant_id, variant_name, description, config_json,
                 change_magnitude, parent_variant_id, version, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                hook_type, variant_id, name, description,
                json.dumps(config), magnitude, parent_id, version, created_by
            ))
            self.db.commit()

            self._log_event("variant_created", hook_type, variant_id,
                           f"Created {magnitude} variant: {name}")

            return variant_id

        except sqlite3.IntegrityError:
            # Variant already exists
            return variant_id

    def get_variant(self, variant_id: str) -> Optional[HookVariant]:
        """Get a specific variant by ID."""
        cursor = self.db.execute("""
            SELECT hook_type, variant_id, variant_name, description, config_json,
                   is_active, is_default, is_protected, ab_group, ab_test_id,
                   change_magnitude, selection_weight, parent_variant_id, version,
                   created_at, created_by
            FROM hook_variants WHERE variant_id = ?
        """, (variant_id,))

        row = cursor.fetchone()
        if not row:
            return None

        return HookVariant(
            variant_id=row[1],
            hook_type=row[0],
            name=row[2],
            description=row[3] or "",
            config=json.loads(row[4]) if row[4] else {},
            is_active=bool(row[5]),
            is_default=bool(row[6]),
            is_protected=bool(row[7]),
            ab_group=row[8],
            ab_test_id=row[9],
            change_magnitude=row[10] or "minor",
            selection_weight=row[11] or 1.0,
            parent_variant_id=row[12],
            version=row[13] or 1,
            created_at=row[14] or "",
            created_by=row[15] or "system"
        )

    def get_active_variant(self, hook_type: str, session_id: str = None,
                            prompt_text: str = None, risk_level: str = None) -> Tuple[HookVariant, Optional[str]]:
        """
        Get the currently active variant for a hook type.

        If an A/B/C test is running, selects variant based on session_id hash.

        DISTRIBUTION (C variant is less frequent as it's experimental):
        - Control: 50% (baseline, most traffic)
        - Variant A: 25% (minor/conservative changes)
        - Variant B: 15% (major/experimental changes)
        - Variant C: 10% (wisdom injection - least frequent, most experimental)

        Args:
            hook_type: The hook type to get variant for
            session_id: Session ID for deterministic A/B/C selection
            prompt_text: User prompt text (for C variant wisdom injection)
            risk_level: Detected risk level (for C variant context)

        Returns:
            Tuple of (HookVariant, wisdom_injection_text or None)
        """
        wisdom_injection = None

        # Check for running A/B/C test
        active_test = self.get_running_test(hook_type)

        if active_test and session_id:
            # Deterministic selection based on session hash
            hash_bucket = hash(session_id) % 100

            # Distribution: 50% control, 25% A, 15% B, 10% C
            # C variant (wisdom injection) is less frequent since it's most experimental
            if hash_bucket < 50:
                # 50% get control (baseline)
                variant = self.get_variant(active_test.control_variant_id)
            elif hash_bucket < 75 and active_test.variant_a_id:
                # 25% get variant A (minor/conservative changes)
                variant = self.get_variant(active_test.variant_a_id)
            elif hash_bucket < 90 and active_test.variant_b_id:
                # 15% get variant B (major/experimental changes)
                variant = self.get_variant(active_test.variant_b_id)
            elif active_test.wisdom_injection_enabled or active_test.variant_c_id:
                # 10% get variant C (wisdom injection - most experimental)
                if active_test.wisdom_injection_enabled and prompt_text:
                    # Dynamic wisdom injection with contextual learning
                    variant = self.get_variant(active_test.control_variant_id)
                    wisdom_injection = self._generate_wisdom_injection(
                        prompt_text, risk_level, session_id
                    )
                elif active_test.variant_c_id:
                    # Static C variant
                    variant = self.get_variant(active_test.variant_c_id)
                else:
                    variant = self.get_variant(active_test.control_variant_id)
            else:
                # Fallback to control
                variant = self.get_variant(active_test.control_variant_id)

            if variant:
                return variant, wisdom_injection

        # No A/B/C test or no session_id - return default
        return self.get_default_variant(hook_type), None

    def _generate_wisdom_injection(self, prompt_text: str, risk_level: str = None,
                                    session_id: str = None) -> Optional[str]:
        """
        Generate wisdom injection for C variant using PromptPatternAnalyzer.

        Returns the injection text or None if no appropriate injection.
        """
        try:
            from memory.prompt_pattern_analyzer import get_analyzer
            analyzer = get_analyzer()

            injection = analyzer.generate_wisdom_injection(
                prompt_text=prompt_text,
                risk_level=risk_level or "",
                session_id=session_id
            )

            if injection:
                return injection.injection_text

            return None
        except ImportError:
            return None
        except Exception:
            return None

    def get_default_variant(self, hook_type: str) -> HookVariant:
        """Get the default variant for a hook type."""
        cursor = self.db.execute("""
            SELECT default_variant_id FROM hook_defaults WHERE hook_type = ?
        """, (hook_type,))

        row = cursor.fetchone()
        if row:
            variant = self.get_variant(row[0])
            if variant:
                return variant

        # Fallback: create default if missing
        self._ensure_default_variants()
        variant_id = f"{hook_type.lower()}_default"
        return self.get_variant(variant_id)

    def list_variants(self, hook_type: str = None, active_only: bool = True) -> List[HookVariant]:
        """List all variants, optionally filtered by hook type."""
        if hook_type:
            if active_only:
                cursor = self.db.execute("""
                    SELECT variant_id FROM hook_variants
                    WHERE hook_type = ? AND is_active = 1
                    ORDER BY created_at DESC
                """, (hook_type,))
            else:
                cursor = self.db.execute("""
                    SELECT variant_id FROM hook_variants
                    WHERE hook_type = ?
                    ORDER BY created_at DESC
                """, (hook_type,))
        else:
            if active_only:
                cursor = self.db.execute("""
                    SELECT variant_id FROM hook_variants
                    WHERE is_active = 1
                    ORDER BY hook_type, created_at DESC
                """)
            else:
                cursor = self.db.execute("""
                    SELECT variant_id FROM hook_variants
                    ORDER BY hook_type, created_at DESC
                """)

        variants = []
        for row in cursor.fetchall():
            variant = self.get_variant(row[0])
            if variant:
                variants.append(variant)

        return variants

    def protect_variant(self, variant_id: str, protected: bool = True) -> bool:
        """Mark a variant as protected from auto-pruning."""
        try:
            self.db.execute("""
                UPDATE hook_variants SET is_protected = ? WHERE variant_id = ?
            """, (1 if protected else 0, variant_id))
            self.db.commit()

            self._log_event("protection_changed", variant_id=variant_id,
                           details=f"Protected: {protected}")
            return True
        except Exception:
            return False

    def deactivate_variant(self, variant_id: str, reason: str = "") -> bool:
        """Deactivate a variant (soft delete)."""
        variant = self.get_variant(variant_id)
        if not variant:
            return False

        if variant.is_protected:
            print(f"Cannot deactivate protected variant: {variant_id}")
            return False

        if variant.is_default:
            print(f"Cannot deactivate default variant: {variant_id}")
            return False

        try:
            self.db.execute("""
                UPDATE hook_variants SET is_active = 0 WHERE variant_id = ?
            """, (variant_id,))
            self.db.commit()

            self._log_event("variant_deactivated", variant.hook_type, variant_id,
                           reason or "No reason provided")
            return True
        except Exception:
            return False

    # =========================================================================
    # OUTCOME TRACKING
    # =========================================================================

    def record_hook_fired(self, variant_id: str, session_id: str,
                          trigger_context: str = "", risk_level: str = ""):
        """Record that a hook fired (for later outcome attribution)."""
        try:
            self.db.execute("""
                INSERT INTO hook_firings
                (variant_id, session_id, trigger_context, risk_level)
                VALUES (?, ?, ?, ?)
            """, (variant_id, session_id, trigger_context[:500] if trigger_context else "", risk_level))
            self.db.commit()
        except Exception:
            pass  # Non-critical

    def record_outcome(self, variant_id: str, session_id: str,
                       outcome: str, signals: List[str],
                       task_completed: bool = None, retry_count: int = 0,
                       time_ms: int = None, confidence: float = 0.5,
                       trigger_context: str = "", risk_level: str = "",
                       area: str = "") -> bool:
        """
        Record what happened after a hook fired.

        Args:
            variant_id: The hook variant that was active
            session_id: Session identifier
            outcome: 'positive', 'negative', 'neutral', 'unknown'
            signals: List of signals that determined the outcome
            task_completed: Whether the task completed successfully
            retry_count: How many retries were needed
            time_ms: Time to completion in milliseconds
            confidence: 0.0-1.0 how confident we are in this outcome
            trigger_context: What triggered the hook
            risk_level: Risk level that was detected
            area: What area of work this was

        Returns:
            True if recorded successfully
        """
        valid_outcomes = ('positive', 'negative', 'neutral', 'unknown')
        if outcome not in valid_outcomes:
            print(f"Invalid outcome: {outcome}. Use one of: {valid_outcomes}")
            return False

        try:
            self.db.execute("""
                INSERT INTO hook_outcomes
                (variant_id, session_id, trigger_context, outcome, outcome_signals,
                 confidence, task_completed, retry_count, time_to_completion_ms,
                 risk_level, area)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                variant_id, session_id, trigger_context[:500] if trigger_context else "",
                outcome, json.dumps(signals), confidence,
                1 if task_completed else (0 if task_completed is False else None),
                retry_count, time_ms, risk_level, area
            ))

            # Mark related firings as having outcome recorded
            self.db.execute("""
                UPDATE hook_firings SET outcome_recorded = 1
                WHERE variant_id = ? AND session_id = ?
            """, (variant_id, session_id))

            self.db.commit()

            self._log_event("outcome_recorded", variant_id=variant_id,
                           details=f"Outcome: {outcome}, Confidence: {confidence:.2f}")

            return True

        except Exception as e:
            print(f"Failed to record outcome: {e}")
            return False

    def attribute_session_outcome(self, session_id: str, outcome: str,
                                   signals: List[str], task_completed: bool = None,
                                   confidence: float = 0.5):
        """
        Attribute an outcome to all hooks that fired during a session.

        Called when a win is detected to credit the hooks that contributed.
        """
        # Find all hooks that fired in this session without recorded outcomes
        cursor = self.db.execute("""
            SELECT DISTINCT variant_id, trigger_context, risk_level
            FROM hook_firings
            WHERE session_id = ? AND outcome_recorded = 0
        """, (session_id,))

        for row in cursor.fetchall():
            self.record_outcome(
                variant_id=row[0],
                session_id=session_id,
                outcome=outcome,
                signals=signals,
                task_completed=task_completed,
                confidence=confidence,
                trigger_context=row[1] or "",
                risk_level=row[2] or ""
            )

    def finalize_session_outcomes(self, session_id: str, transcript: str = ""):
        """
        Finalize outcomes for all hooks in a session.

        Called at session end. Hooks without explicit outcomes are marked neutral.
        """
        # Find hooks without outcomes
        cursor = self.db.execute("""
            SELECT variant_id, trigger_context, risk_level
            FROM hook_firings
            WHERE session_id = ? AND outcome_recorded = 0
        """, (session_id,))

        for row in cursor.fetchall():
            # No explicit outcome = neutral
            self.record_outcome(
                variant_id=row[0],
                session_id=session_id,
                outcome="neutral",
                signals=["session_ended_no_explicit_outcome"],
                confidence=0.3,
                trigger_context=row[1] or "",
                risk_level=row[2] or ""
            )

    def get_variant_stats(self, variant_id: str) -> VariantStats:
        """Get performance statistics for a variant."""
        cursor = self.db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome = 'positive' THEN 1 ELSE 0 END) as positive,
                SUM(CASE WHEN outcome = 'negative' THEN 1 ELSE 0 END) as negative,
                SUM(CASE WHEN outcome = 'neutral' THEN 1 ELSE 0 END) as neutral,
                AVG(retry_count) as avg_retries,
                AVG(time_to_completion_ms) as avg_time
            FROM hook_outcomes
            WHERE variant_id = ?
        """, (variant_id,))

        row = cursor.fetchone()
        total = row[0] or 0
        positive = row[1] or 0
        negative = row[2] or 0
        neutral = row[3] or 0

        return VariantStats(
            variant_id=variant_id,
            total_outcomes=total,
            positive_count=positive,
            negative_count=negative,
            neutral_count=neutral,
            positive_rate=positive / total if total > 0 else 0.0,
            negative_rate=negative / total if total > 0 else 0.0,
            avg_retry_count=row[4] or 0.0,
            avg_time_ms=row[5],
            experience_level=self._get_experience_level(total)
        )

    def _get_experience_level(self, total_outcomes: int) -> str:
        """Categorize how much experience we have with a variant."""
        if total_outcomes == 0:
            return "no_data"
        elif total_outcomes < 5:
            return "limited"
        elif total_outcomes < 15:
            return "moderate"
        elif total_outcomes < 50:
            return "good"
        else:
            return "extensive"

    def get_experience_risk_modifier(self, variant_id: str) -> Tuple[float, str]:
        """
        Calculate risk modifier based on outcome history.

        Returns:
            Tuple of (risk_modifier, explanation)
            - risk_modifier: -0.3 to +0.5 adjustment
            - explanation: Human-readable reason
        """
        stats = self.get_variant_stats(variant_id)

        if stats.total_outcomes == 0:
            return 0.0, "No outcome data (using heuristic risk only)"

        # Weight based on data quantity
        experience_weight = {
            "limited": 0.3,
            "moderate": 0.6,
            "good": 0.85,
            "extensive": 1.0,
        }.get(stats.experience_level, 0.0)

        neg_rate = stats.negative_rate
        pos_rate = stats.positive_rate
        total = stats.total_outcomes

        # Calculate modifier
        if neg_rate >= 0.4:
            base_modifier = 0.5
            explanation = f"High negative rate ({neg_rate:.0%} of {total} outcomes)"
        elif neg_rate >= 0.25:
            base_modifier = 0.3
            explanation = f"Moderate negative rate ({neg_rate:.0%} of {total} outcomes)"
        elif neg_rate >= 0.1:
            base_modifier = 0.15
            explanation = f"Some negative outcomes ({neg_rate:.0%} of {total} outcomes)"
        elif pos_rate >= 0.8:
            base_modifier = -0.3
            explanation = f"Highly effective ({pos_rate:.0%} positive rate over {total} outcomes)"
        elif pos_rate >= 0.6:
            base_modifier = -0.15
            explanation = f"Effective ({pos_rate:.0%} positive rate over {total} outcomes)"
        else:
            base_modifier = 0.0
            explanation = f"Mixed results ({pos_rate:.0%} positive rate over {total} outcomes)"

        final_modifier = base_modifier * experience_weight

        if stats.experience_level in ("limited", "moderate"):
            explanation += f" [{stats.experience_level} data]"

        return final_modifier, explanation

    def get_worst_performing_variants(self, hook_type: str = None,
                                       min_outcomes: int = 10) -> List[Dict]:
        """Get variants with highest negative rates."""
        if hook_type:
            cursor = self.db.execute("""
                SELECT
                    v.variant_id,
                    v.variant_name,
                    v.hook_type,
                    COUNT(*) as total,
                    SUM(CASE WHEN o.outcome = 'negative' THEN 1 ELSE 0 END) as neg,
                    CAST(SUM(CASE WHEN o.outcome = 'negative' THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as neg_rate
                FROM hook_variants v
                JOIN hook_outcomes o ON v.variant_id = o.variant_id
                WHERE v.hook_type = ? AND v.is_active = 1
                GROUP BY v.variant_id
                HAVING COUNT(*) >= ?
                ORDER BY neg_rate DESC, total DESC
                LIMIT 10
            """, (hook_type, min_outcomes))
        else:
            cursor = self.db.execute("""
                SELECT
                    v.variant_id,
                    v.variant_name,
                    v.hook_type,
                    COUNT(*) as total,
                    SUM(CASE WHEN o.outcome = 'negative' THEN 1 ELSE 0 END) as neg,
                    CAST(SUM(CASE WHEN o.outcome = 'negative' THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as neg_rate
                FROM hook_variants v
                JOIN hook_outcomes o ON v.variant_id = o.variant_id
                WHERE v.is_active = 1
                GROUP BY v.variant_id
                HAVING COUNT(*) >= ?
                ORDER BY neg_rate DESC, total DESC
                LIMIT 10
            """, (min_outcomes,))

        return [
            {
                "variant_id": row[0],
                "name": row[1],
                "hook_type": row[2],
                "total_outcomes": row[3],
                "negative_count": row[4],
                "negative_rate": row[5],
            }
            for row in cursor.fetchall()
        ]

    # =========================================================================
    # A/B TESTING
    # =========================================================================

    def create_ab_test(self, test_name: str, hook_type: str,
                       control_variant_id: str,
                       variant_a_id: str = None, variant_b_id: str = None,
                       variant_c_id: str = None,
                       wisdom_injection_enabled: bool = False,
                       min_samples: int = 30,
                       confidence_threshold: float = 0.95) -> str:
        """
        Create a new A/B/C test.

        Args:
            test_name: Human-readable test name
            hook_type: Hook type to test
            control_variant_id: Control/baseline variant
            variant_a_id: Minor changes variant (optional)
            variant_b_id: Major changes variant (optional)
            variant_c_id: Static C variant (optional, mutually exclusive with wisdom_injection)
            wisdom_injection_enabled: If True, C variant uses dynamic wisdom injection
            min_samples: Minimum samples per variant before concluding
            confidence_threshold: Statistical confidence required

        Returns:
            test_id of created test
        """
        test_id = f"test_{hook_type.lower()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        try:
            self.db.execute("""
                INSERT INTO hook_ab_tests
                (test_id, test_name, hook_type, control_variant_id,
                 variant_a_id, variant_b_id, variant_c_id,
                 wisdom_injection_enabled, min_samples_per_variant,
                 confidence_threshold)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                test_id, test_name, hook_type, control_variant_id,
                variant_a_id, variant_b_id, variant_c_id,
                1 if wisdom_injection_enabled else 0,
                min_samples, confidence_threshold
            ))
            self.db.commit()

            c_desc = "wisdom injection" if wisdom_injection_enabled else (variant_c_id or "none")
            self._log_event("test_created", hook_type, test_id=test_id,
                           details=f"Test: {test_name} (C={c_desc})")

            return test_id

        except sqlite3.IntegrityError:
            return test_id

    def start_ab_test(self, test_id: str) -> bool:
        """Start an A/B/C test."""
        try:
            # Get test details
            cursor = self.db.execute(
                "SELECT control_variant_id, variant_a_id, variant_b_id, variant_c_id, hook_type, wisdom_injection_enabled FROM hook_ab_tests WHERE test_id = ?",
                (test_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False

            control_id, a_id, b_id, c_id, hook_type, wisdom_enabled = row

            # Set A/B/C groups on variants
            self.db.execute("""
                UPDATE hook_variants SET ab_group = 'control', ab_test_id = ?
                WHERE variant_id = ?
            """, (test_id, control_id))

            if a_id:
                self.db.execute("""
                    UPDATE hook_variants SET ab_group = 'variant_a', ab_test_id = ?
                    WHERE variant_id = ?
                """, (test_id, a_id))

            if b_id:
                self.db.execute("""
                    UPDATE hook_variants SET ab_group = 'variant_b', ab_test_id = ?
                    WHERE variant_id = ?
                """, (test_id, b_id))

            if c_id:
                self.db.execute("""
                    UPDATE hook_variants SET ab_group = 'variant_c', ab_test_id = ?
                    WHERE variant_id = ?
                """, (test_id, c_id))

            # Update test status
            self.db.execute("""
                UPDATE hook_ab_tests SET status = 'running', started_at = ?
                WHERE test_id = ?
            """, (datetime.now().isoformat(), test_id))

            self.db.commit()

            c_info = " (with wisdom injection)" if wisdom_enabled else ""
            self._log_event("test_started", hook_type, test_id=test_id,
                           details=f"Started A/B/C test{c_info}")
            return True

        except Exception as e:
            print(f"Failed to start test: {e}")
            return False

    def get_running_test(self, hook_type: str) -> Optional[ABTest]:
        """Get the running A/B/C test for a hook type, if any."""
        cursor = self.db.execute("""
            SELECT test_id, test_name, hook_type, control_variant_id,
                   variant_a_id, variant_b_id, variant_c_id, status, winner_variant_id,
                   min_samples_per_variant, confidence_threshold,
                   started_at, ended_at, wisdom_injection_enabled
            FROM hook_ab_tests
            WHERE hook_type = ? AND status = 'running'
            LIMIT 1
        """, (hook_type,))

        row = cursor.fetchone()
        if not row:
            return None

        return ABTest(
            test_id=row[0],
            test_name=row[1],
            hook_type=row[2],
            control_variant_id=row[3],
            variant_a_id=row[4],
            variant_b_id=row[5],
            variant_c_id=row[6],
            status=row[7],
            winner_variant_id=row[8],
            min_samples_per_variant=row[9],
            confidence_threshold=row[10],
            started_at=row[11],
            ended_at=row[12],
            wisdom_injection_enabled=bool(row[13])
        )

    def list_ab_tests(self, hook_type: str = None, status: str = None) -> List[ABTest]:
        """List A/B/C tests."""
        query = "SELECT test_id, test_name, hook_type, control_variant_id, variant_a_id, variant_b_id, variant_c_id, status, winner_variant_id, min_samples_per_variant, confidence_threshold, started_at, ended_at, wisdom_injection_enabled FROM hook_ab_tests WHERE 1=1"
        params = []

        if hook_type:
            query += " AND hook_type = ?"
            params.append(hook_type)
        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY created_at DESC"

        cursor = self.db.execute(query, params)

        return [
            ABTest(
                test_id=row[0], test_name=row[1], hook_type=row[2],
                control_variant_id=row[3], variant_a_id=row[4], variant_b_id=row[5],
                variant_c_id=row[6], status=row[7], winner_variant_id=row[8],
                min_samples_per_variant=row[9], confidence_threshold=row[10],
                started_at=row[11], ended_at=row[12],
                wisdom_injection_enabled=bool(row[13]) if row[13] is not None else False
            )
            for row in cursor.fetchall()
        ]

    def check_significance(self, test_id: str) -> Dict:
        """
        Check if A/B/C test has reached statistical significance.

        Uses a simplified significance calculation (chi-squared approximation).
        """
        cursor = self.db.execute(
            "SELECT control_variant_id, variant_a_id, variant_b_id, variant_c_id, min_samples_per_variant, confidence_threshold, wisdom_injection_enabled FROM hook_ab_tests WHERE test_id = ?",
            (test_id,)
        )
        row = cursor.fetchone()
        if not row:
            return {"error": "Test not found"}

        control_id, a_id, b_id, c_id, min_samples, threshold, wisdom_enabled = row

        # Get stats for each variant
        control_stats = self.get_variant_stats(control_id) if control_id else None
        a_stats = self.get_variant_stats(a_id) if a_id else None
        b_stats = self.get_variant_stats(b_id) if b_id else None
        c_stats = self.get_variant_stats(c_id) if c_id else None

        # For wisdom injection, get stats from wisdom_injections table
        wisdom_stats = None
        if wisdom_enabled:
            wisdom_stats = self._get_wisdom_injection_stats(test_id)

        result = {
            "test_id": test_id,
            "min_samples": min_samples,
            "confidence_threshold": threshold,
            "control": {
                "variant_id": control_id,
                "total": control_stats.total_outcomes if control_stats else 0,
                "positive_rate": control_stats.positive_rate if control_stats else None,
            } if control_id else None,
            "variant_a": {
                "variant_id": a_id,
                "total": a_stats.total_outcomes if a_stats else 0,
                "positive_rate": a_stats.positive_rate if a_stats else None,
            } if a_id else None,
            "variant_b": {
                "variant_id": b_id,
                "total": b_stats.total_outcomes if b_stats else 0,
                "positive_rate": b_stats.positive_rate if b_stats else None,
            } if b_id else None,
            "variant_c": {
                "variant_id": c_id or "wisdom_injection",
                "total": c_stats.total_outcomes if c_stats else (wisdom_stats["total"] if wisdom_stats else 0),
                "positive_rate": c_stats.positive_rate if c_stats else (wisdom_stats["positive_rate"] if wisdom_stats else None),
                "is_wisdom_injection": wisdom_enabled,
            } if c_id or wisdom_enabled else None,
        }

        # Check if we have enough samples
        has_enough = True
        for key in ["control", "variant_a", "variant_b", "variant_c"]:
            if result.get(key) and result[key]["total"] < min_samples:
                has_enough = False

        result["has_enough_samples"] = has_enough

        if not has_enough:
            result["is_significant"] = False
            result["recommendation"] = "Need more samples"
            return result

        # Simple significance: compare positive rates
        rates = []
        if result["control"] and result["control"]["positive_rate"] is not None:
            rates.append(("control", result["control"]["positive_rate"] or 0))
        if result["variant_a"] and result["variant_a"]["positive_rate"] is not None:
            rates.append(("variant_a", result["variant_a"]["positive_rate"] or 0))
        if result["variant_b"] and result["variant_b"]["positive_rate"] is not None:
            rates.append(("variant_b", result["variant_b"]["positive_rate"] or 0))
        if result["variant_c"] and result["variant_c"]["positive_rate"] is not None:
            rates.append(("variant_c", result["variant_c"]["positive_rate"] or 0))

        if len(rates) < 2:
            result["is_significant"] = False
            result["recommendation"] = "Need at least 2 variants with data"
            return result

        # Find best performer
        rates.sort(key=lambda x: x[1], reverse=True)
        best = rates[0]
        second = rates[1]

        # Calculate difference
        diff = best[1] - second[1]

        # Simplified significance (10% difference = significant)
        is_significant = diff >= 0.10

        result["is_significant"] = is_significant
        result["winner"] = best[0] if is_significant else None
        result["difference"] = diff

        if is_significant:
            winner_key = best[0]
            winner_id = result[winner_key]["variant_id"]
            wisdom_note = " (wisdom injection)" if winner_key == "variant_c" and wisdom_enabled else ""
            result["recommendation"] = f"Promote {winner_key} ({winner_id}){wisdom_note} - {diff:.1%} better"
        else:
            result["recommendation"] = f"No clear winner yet (diff: {diff:.1%})"

        return result

    def _get_wisdom_injection_stats(self, test_id: str) -> Optional[Dict]:
        """Get statistics for wisdom injections in an A/B/C test."""
        try:
            cursor = self.db.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'positive' THEN 1 ELSE 0 END) as positive,
                    SUM(CASE WHEN outcome = 'negative' THEN 1 ELSE 0 END) as negative
                FROM wisdom_injections
                WHERE hook_variant_id LIKE ? || '%'
            """, (test_id,))

            row = cursor.fetchone()
            if not row or row[0] == 0:
                return None

            total = row[0]
            positive = row[1] or 0

            return {
                "total": total,
                "positive": positive,
                "negative": row[2] or 0,
                "positive_rate": positive / total if total > 0 else 0
            }
        except Exception:
            return None

    def conclude_ab_test(self, test_id: str, winner_id: str = None,
                         promote: bool = True) -> bool:
        """
        Conclude an A/B/C test.

        Args:
            test_id: The test to conclude
            winner_id: The winning variant (auto-selects if None)
            promote: If True, set winner as new default (not for wisdom injection)
        """
        if not winner_id:
            # Auto-select based on significance check
            sig = self.check_significance(test_id)
            if sig.get("winner"):
                winner_key = sig["winner"]
                winner_id = sig[winner_key]["variant_id"]
            else:
                print("No clear winner - specify winner_id manually")
                return False

        try:
            # Get test details
            cursor = self.db.execute(
                "SELECT hook_type, control_variant_id, variant_a_id, variant_b_id, variant_c_id, wisdom_injection_enabled FROM hook_ab_tests WHERE test_id = ?",
                (test_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False

            hook_type = row[0]
            wisdom_enabled = row[5]

            # Clear A/B/C groups from all variants
            for vid in [row[1], row[2], row[3], row[4]]:
                if vid:
                    self.db.execute("""
                        UPDATE hook_variants SET ab_group = NULL, ab_test_id = NULL
                        WHERE variant_id = ?
                    """, (vid,))

            # Update test
            self.db.execute("""
                UPDATE hook_ab_tests
                SET status = 'completed', winner_variant_id = ?, ended_at = ?
                WHERE test_id = ?
            """, (winner_id, datetime.now().isoformat(), test_id))

            self.db.commit()

            is_wisdom_winner = winner_id == "wisdom_injection" or (
                winner_id == row[4] and wisdom_enabled
            )

            self._log_event("test_concluded", hook_type, winner_id, test_id,
                           f"Winner: {winner_id}" + (" (wisdom injection)" if is_wisdom_winner else ""))

            # Optionally promote winner (not for wisdom injection - that stays dynamic)
            if promote and winner_id != "wisdom_injection":
                self.set_as_default(winner_id)

            return True

        except Exception as e:
            print(f"Failed to conclude test: {e}")
            return False

    # =========================================================================
    # DEFAULT & REVERT
    # =========================================================================

    def set_as_default(self, variant_id: str) -> bool:
        """Set a variant as the new default for its hook type."""
        variant = self.get_variant(variant_id)
        if not variant:
            return False

        try:
            # Clear old default
            self.db.execute("""
                UPDATE hook_variants SET is_default = 0
                WHERE hook_type = ? AND is_default = 1
            """, (variant.hook_type,))

            # Set new default
            self.db.execute("""
                UPDATE hook_variants SET is_default = 1
                WHERE variant_id = ?
            """, (variant_id,))

            # Update defaults table
            self.db.execute("""
                UPDATE hook_defaults SET default_variant_id = ?
                WHERE hook_type = ?
            """, (variant_id, variant.hook_type))

            self.db.commit()

            self._log_event("default_changed", variant.hook_type, variant_id,
                           f"New default: {variant.name}")

            return True

        except Exception:
            return False

    def revert_to_default(self, hook_type: str) -> Optional[str]:
        """
        Revert a hook type to its original system default.

        Returns the variant_id of the restored default.
        """
        # Get original default config
        original_config = DEFAULT_HOOK_CONFIGS.get(hook_type)
        if not original_config:
            return None

        system_default_id = f"{hook_type.lower()}_default"

        try:
            # Reset system default variant to original config
            self.db.execute("""
                UPDATE hook_variants
                SET config_json = ?, is_active = 1
                WHERE variant_id = ?
            """, (json.dumps(original_config), system_default_id))

            # Set as current default
            self.set_as_default(system_default_id)

            # Update revert count
            self.db.execute("""
                UPDATE hook_defaults
                SET last_reverted_at = ?, revert_count = revert_count + 1
                WHERE hook_type = ?
            """, (datetime.now().isoformat(), hook_type))

            self.db.commit()

            self._log_event("reverted_to_default", hook_type, system_default_id,
                           "Reverted to original system default")

            return system_default_id

        except Exception as e:
            print(f"Failed to revert: {e}")
            return None

    # =========================================================================
    # AUTO-PRUNING
    # =========================================================================

    def auto_prune_underperformers(self, min_outcomes: int = 20,
                                    max_negative_rate: float = 0.35,
                                    dry_run: bool = True) -> List[str]:
        """
        Automatically deactivate variants that consistently underperform.

        Protected variants are skipped.

        Args:
            min_outcomes: Minimum outcomes required for evaluation
            max_negative_rate: Maximum acceptable negative outcome rate
            dry_run: If True, only report what would be pruned

        Returns:
            List of variant_ids that were (or would be) pruned
        """
        worst = self.get_worst_performing_variants(min_outcomes=min_outcomes)

        to_prune = []
        for entry in worst:
            if entry["negative_rate"] > max_negative_rate:
                variant = self.get_variant(entry["variant_id"])
                if variant and not variant.is_protected and not variant.is_default:
                    to_prune.append(entry["variant_id"])

        if dry_run:
            return to_prune

        for variant_id in to_prune:
            self.deactivate_variant(
                variant_id,
                f"Auto-pruned: negative rate exceeded {max_negative_rate:.0%}"
            )

        return to_prune

    def get_evolution_summary(self) -> Dict:
        """Get a summary of the hook evolution state."""
        variants = self.list_variants(active_only=False)
        tests = self.list_ab_tests()

        active_count = sum(1 for v in variants if v.is_active)
        protected_count = sum(1 for v in variants if v.is_protected)
        running_tests = sum(1 for t in tests if t.status == "running")

        # Count outcomes
        cursor = self.db.execute("SELECT COUNT(*) FROM hook_outcomes")
        total_outcomes = cursor.fetchone()[0] or 0

        return {
            "total_variants": len(variants),
            "active_variants": active_count,
            "protected_variants": protected_count,
            "total_tests": len(tests),
            "running_tests": running_tests,
            "total_outcomes": total_outcomes,
            "hook_types": list(set(v.hook_type for v in variants))
        }


def get_hook_evolution_engine() -> HookEvolutionEngine:
    """Get the singleton HookEvolutionEngine instance."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = HookEvolutionEngine()
    return _engine_instance


# =========================================================================
# CLI INTERFACE
# =========================================================================

if __name__ == "__main__":
    import sys

    engine = get_hook_evolution_engine()

    if len(sys.argv) < 2:
        print("Hook Evolution Engine")
        print("=" * 50)
        summary = engine.get_evolution_summary()
        print(f"Active Variants: {summary['active_variants']}/{summary['total_variants']}")
        print(f"Protected: {summary['protected_variants']}")
        print(f"Running Tests: {summary['running_tests']}")
        print(f"Total Outcomes: {summary['total_outcomes']}")
        print(f"Hook Types: {', '.join(summary['hook_types'])}")
        print()
        print("Commands:")
        print("  python hook_evolution.py list [hook_type]")
        print("  python hook_evolution.py stats <variant_id>")
        print("  python hook_evolution.py worst [min_outcomes]")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "list":
        hook_type = sys.argv[2] if len(sys.argv) > 2 else None
        variants = engine.list_variants(hook_type)
        for v in variants:
            status = "✓" if v.is_active else "✗"
            default = " [DEFAULT]" if v.is_default else ""
            protected = " [PROTECTED]" if v.is_protected else ""
            magnitude = f" ({v.change_magnitude})" if v.change_magnitude != "baseline" else ""
            print(f"{status} {v.variant_id}{default}{protected}{magnitude}")
            print(f"   {v.hook_type}: {v.name}")

    elif cmd == "stats":
        if len(sys.argv) < 3:
            print("Usage: python hook_evolution.py stats <variant_id>")
            sys.exit(1)

        variant_id = sys.argv[2]
        stats = engine.get_variant_stats(variant_id)
        modifier, explanation = engine.get_experience_risk_modifier(variant_id)

        print(f"Variant: {variant_id}")
        print(f"Total Outcomes: {stats.total_outcomes}")
        print(f"Positive: {stats.positive_count} ({stats.positive_rate:.1%})")
        print(f"Negative: {stats.negative_count} ({stats.negative_rate:.1%})")
        print(f"Neutral: {stats.neutral_count}")
        print(f"Avg Retries: {stats.avg_retry_count:.2f}")
        print(f"Experience Level: {stats.experience_level}")
        print(f"Risk Modifier: {modifier:+.2f}")
        print(f"  {explanation}")

    elif cmd == "worst":
        min_outcomes = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        worst = engine.get_worst_performing_variants(min_outcomes=min_outcomes)

        if not worst:
            print(f"No variants with {min_outcomes}+ outcomes to evaluate")
        else:
            print(f"Worst Performing Variants (min {min_outcomes} outcomes):")
            for w in worst:
                print(f"  {w['variant_id']}: {w['negative_rate']:.1%} negative ({w['negative_count']}/{w['total_outcomes']})")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
