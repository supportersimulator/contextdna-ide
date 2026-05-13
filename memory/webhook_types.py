"""
Webhook Injection Types — extracted from persistent_hook_structure.py (Phase 0 decomposition).

All type definitions, enums, dataclasses, and constants used by the 9-section
webhook injection system. Extracted to reduce persistent_hook_structure.py's
4,933-line surface area and enable clean imports without pulling the entire module.

Usage:
    from memory.webhook_types import InjectionConfig, RiskLevel, InjectionResult
    # OR (backward compat — re-exported from persistent_hook_structure)
    from memory.persistent_hook_structure import InjectionConfig, RiskLevel
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# =============================================================================
# ENUMS
# =============================================================================

class InjectionMode(Enum):
    """Available injection modes."""
    LAYERED = "layered"   # Existing 6-layer system
    GREEDY = "greedy"     # Agent's wishlist focused
    HYBRID = "hybrid"     # Combination of both
    MINIMAL = "minimal"   # Safety + Foundation only


class RiskLevel(Enum):
    """Risk levels for context depth."""
    CRITICAL = "critical"  # 5% first-try likelihood
    HIGH = "high"          # 30% first-try likelihood
    MODERATE = "moderate"  # 60% first-try likelihood
    LOW = "low"            # 90% first-try likelihood


class VolumeEscalationTier(Enum):
    """Volume escalation tiers for context injection."""
    SILVER_PLATTER = 1   # Default curated context (~5KB)
    EXPANDED = 2         # After 1 failure OR <40% confidence (~15KB)
    FULL_LIBRARY = 3     # After 2+ failures OR <20% confidence (~50KB)


class ExclusionReason(Enum):
    """Why a section/ref was excluded from injection."""
    EMPTY_CONTENT = "empty_content"       # Generator returned empty/None
    ABBREVIATED = "abbreviated"           # Truncated by injection depth cycling
    SHORT_PROMPT = "short_prompt"         # ≤5 words, injection skipped
    GENERATOR_FAILED = "generator_failed" # Section generator threw exception
    DISABLED = "disabled"                 # Config toggled off
    SIZE_BUDGET = "size_budget"           # Exceeded token budget
    STALENESS = "staleness"              # Content too old to be useful
    SCOPE_MISMATCH = "scope_mismatch"    # Wrong scope for this injection


# =============================================================================
# DATACLASSES
# =============================================================================

@dataclass
class InjectionConfig:
    """Configuration for context injection."""
    mode: InjectionMode = InjectionMode.HYBRID

    # Section toggles (for A/B testing format, not presence)
    section_0_enabled: bool = True   # SAFETY - should ALWAYS be True
    section_1_enabled: bool = True   # FOUNDATION
    section_2_enabled: bool = True   # WISDOM
    section_3_enabled: bool = True   # AWARENESS
    section_4_enabled: bool = True   # DEEP CONTEXT (conditional)
    section_5_enabled: bool = True   # PROTOCOL

    # A/B test variables
    sop_count: int = 3               # How many SOPs to include
    professor_depth: str = "full"    # "full", "summary", "one_thing_only"
    awareness_depth: str = "full"    # "full", "changes_only", "none"
    emoji_enabled: bool = True       # Whether to use emoji markers
    verbose_protocol: bool = False   # Verbose vs minimal protocol reminder

    # Wisdom injection C variant
    wisdom_injection_enabled: bool = False
    wisdom_text: Optional[str] = None

    # Session state (for smart SOP reading)
    session_has_failures: bool = False  # If True, MUST READ all SOPs

    # Section toggles for sections not originally controlled via config
    section_6_enabled: bool = True   # HOLISTIC (Synaptic→Atlas) - skip in chat mode
    section_7_enabled: bool = True   # ACONTEXT_LIBRARY (FULL_LIBRARY) - escalation
    section_10_enabled: bool = True  # VISION (Strategic Planner) - requires LLM

    # Chat/Synaptic overrides (skip slow operations for conversational use)
    skip_boundary_intelligence: bool = False  # Skip BoundaryIntelligence LLM call
    skip_short_prompt_bypass: bool = False    # Allow injection even for ≤5 word prompts


@dataclass
class SOPEntry:
    """A single SOP with title and full content."""
    title: str
    content: str
    sop_type: str  # "bug-fix", "process", "gotcha"
    use_when: str = ""
    relevance: float = 0.0


@dataclass
class ManifestRef:
    """Single included or excluded item in a PayloadManifest."""
    section: str          # "section_0", "section_1", etc.
    label: str            # "safety", "foundation", "wisdom", etc.
    included: bool        # True=in payload, False=excluded
    tokens_est: int = 0   # Estimated token count (chars/4 rough)
    source: str = ""      # Where content came from (e.g., "professor.py", "redis_cache")
    exclusion_reason: Optional[str] = None  # ExclusionReason.value if excluded
    latency_ms: int = 0   # Generation time for this section


@dataclass
class PayloadManifest:
    """Audit trail for a single context injection — what was included, excluded, and why."""
    injection_id: str                    # Unique ID for this injection
    timestamp: str                       # ISO 8601
    injection_count: int                 # Turn number in session
    depth: str                           # "FULL" or "ABBREV"
    risk_level: str                      # RiskLevel.value
    prompt_hash: str                     # SHA256 of prompt[:200] for correlation
    generation_time_ms: int = 0
    included: List[ManifestRef] = field(default_factory=list)
    excluded: List[ManifestRef] = field(default_factory=list)
    total_tokens_est: int = 0           # Sum of included tokens
    boundary_project: Optional[str] = None
    ab_variant: Optional[str] = None

    def to_dict(self) -> dict:
        """Serialize for JSON emission."""
        return {
            "injection_id": self.injection_id,
            "timestamp": self.timestamp,
            "injection_count": self.injection_count,
            "depth": self.depth,
            "risk_level": self.risk_level,
            "prompt_hash": self.prompt_hash,
            "generation_time_ms": self.generation_time_ms,
            "boundary_project": self.boundary_project,
            "ab_variant": self.ab_variant,
            "total_tokens_est": self.total_tokens_est,
            "included": [
                {"section": r.section, "label": r.label, "tokens_est": r.tokens_est,
                 "source": r.source, "latency_ms": r.latency_ms}
                for r in self.included
            ],
            "excluded": [
                {"section": r.section, "label": r.label,
                 "reason": r.exclusion_reason, "latency_ms": r.latency_ms}
                for r in self.excluded
            ],
        }


@dataclass
class InjectionResult:
    """Result of context injection generation."""
    content: str
    sections_included: List[str]
    risk_level: RiskLevel
    first_try_likelihood: str
    mode: InjectionMode
    ab_variant: Optional[str] = None
    generation_time_ms: int = 0
    volume_tier: int = 1          # 1=Silver Platter, 2=Expanded, 3=Full Library
    failure_count: int = 0        # Number of failures triggering escalation
    section_timings: Optional[dict] = None  # Per-section latency_ms {label: ms}

    # Boundary Intelligence fields
    boundary_project: Optional[str] = None          # Detected primary project
    boundary_confidence: float = 0.0                # Confidence in project detection
    boundary_action: Optional[str] = None           # Filter action taken
    boundary_filter_note: Optional[str] = None      # Note about filtering applied
    needs_clarification: bool = False               # Whether to ask user for clarification
    clarification_prompt: Optional[str] = None      # Clarification question
    clarification_options: Optional[List[str]] = None  # Project options for user

    # Per-learning outcome attribution
    learning_ids: List[str] = field(default_factory=list)  # FTS5 learning IDs injected

    # Payload manifest (Movement 3)
    manifest: Optional['PayloadManifest'] = None


@dataclass
class FoundationResult:
    """Result of foundation SOPs fetch with diagnostics."""
    sops: List[SOPEntry]
    roadblocks: List[str] = field(default_factory=list)
    learning_ids: List[str] = field(default_factory=list)  # Per-learning attribution


# =============================================================================
# CONSTANTS
# =============================================================================

FIRST_TRY_LIKELIHOOD: Dict[RiskLevel, str] = {
    RiskLevel.CRITICAL: "5%",
    RiskLevel.HIGH: "30%",
    RiskLevel.MODERATE: "60%",
    RiskLevel.LOW: "90%"
}

RISK_KEYWORDS: Dict[RiskLevel, List[str]] = {
    RiskLevel.CRITICAL: [
        # Destructive operations
        r"destroy", r"delete.*all", r"drop.*table", r"drop.*database", r"truncate",
        r"rm\s+-rf", r"rm\s+-r", r"force.*push", r"reset.*hard", r"wipe", r"purge",
        r"nuke", r"obliterate", r"erase.*all", r"remove.*all", r"clear.*all",
        # Production operations
        r"migration.*prod", r"prod.*migration", r"schema.*change", r"alter.*table",
        r"force.*push.*main", r"force.*push.*master", r"push.*--force.*main",
        r"rollback.*prod", r"revert.*prod", r"downgrade.*prod",
        # Security operations
        r"auth.*system", r"authentication.*change", r"permission.*change",
        r"credentials.*rotate", r"secret.*rotation", r"root.*access",
        r"admin.*password", r"master.*key", r"encryption.*key",
        # Data operations
        r"backup.*delete", r"restore.*prod", r"data.*migration.*prod",
        # FIX 3.1: Expanded critical patterns
        r"production.*database", r"prod.*db", r"user.*data.*delete",
        r"payment", r"billing.*system", r"stripe.*key", r"payment.*gateway",
        r"credential.*leak", r"credential.*expose", r"security.*vulnerability",
        r"cve", r"exploit", r"injection.*attack", r"xss", r"csrf",
        r"api.*key.*rotate", r"api.*key.*revoke", r"token.*revoke",
        r"ssh.*key.*prod", r"private.*key.*prod",
    ],
    RiskLevel.HIGH: [
        # Infrastructure changes
        r"deploy", r"terraform", r"pulumi", r"cloudformation", r"ansible",
        r"kubernetes", r"k8s", r"helm", r"ecs.*service", r"eks", r"fargate",
        r"lambda.*deploy", r"serverless.*deploy", r"cdk.*deploy",
        # Production operations
        r"production", r"prod(?:uction)?(?:\s|$|-|_)", r"live", r"main.*branch",
        r"master.*branch", r"release", r"hotfix",
        # Database operations
        r"migration", r"migrate", r"schema", r"database", r"rds", r"dynamodb",
        r"postgres", r"mysql", r"mongodb", r"redis.*cluster", r"elasticsearch",
        # Security operations
        r"security", r"ssl", r"cert", r"tls", r"credentials", r"secrets",
        r"api.*key", r"access.*key", r"token", r"oauth", r"jwt", r"password",
        r"iam", r"rbac", r"acl", r"firewall", r"security.*group",
        # Scaling/infrastructure
        r"scale", r"autoscal", r"load.*balanc", r"cdn", r"cloudfront",
        r"dns", r"route53", r"domain", r"ingress", r"egress",
        # AWS services
        r"aws", r"ec2", r"s3.*bucket", r"sqs", r"sns", r"kinesis",
        # Refactoring
        r"refactor", r"restructur", r"rewrite", r"overhaul",
        # FIX 3.1: Expanded high-risk patterns
        r"infrastructure", r"infra.*change", r"network.*config",
        r"iam.*role", r"iam.*policy", r"permission.*grant", r"permission.*revoke",
        r"secret.*manager", r"ssm.*parameter", r"env.*var.*prod",
        r"vpc", r"subnet", r"nat.*gateway", r"internet.*gateway",
        r"certificate.*renew", r"ssl.*expire", r"domain.*transfer",
        r"ci.*cd", r"pipeline", r"github.*action", r"jenkins", r"circleci",
    ],
    RiskLevel.MODERATE: [
        # Container operations
        r"docker", r"container", r"compose", r"dockerfile", r"image.*build",
        r"registry", r"ecr", r"gcr",
        # Configuration
        r"config", r"configuration", r"env", r"environment", r"\.env",
        r"settings", r"parameter", r"variable",
        # Health/monitoring
        r"toggle", r"feature.*flag", r"health", r"monitor", r"metric",
        r"logging", r"observability", r"alert", r"alarm",
        # Sync/update operations (word boundaries prevent false positives like "async")
        r"\bsync\b", r"synchroniz", r"update.*config", r"update.*api", r"upgrade",
        r"patch\b", r"change.*config", r"modify.*config", r"alter\b", r"adjust.*setting",
        # Bug fixes (actual bugs, not typo fixes)
        r"fix.*bug", r"fix.*issue", r"fix.*error", r"bug\b", r"issue\b", r"debug", r"troubleshoot",
        # Testing
        r"test.*env", r"staging", r"uat", r"qa",
        # Backup/restore (non-prod)
        r"backup", r"snapshot", r"restore(?!.*prod)",
        # FIX 3.1: Expanded moderate patterns
        r"feature", r"implement.*feature", r"add.*feature",
        r"component", r"react.*component", r"vue.*component",
        r"hook", r"use[A-Z]", r"custom.*hook",
        r"state", r"state.*management", r"redux", r"zustand", r"context",
        r"provider", r"context.*provider", r"wrapper",
        r"api", r"endpoint", r"route", r"handler", r"controller",
        r"service", r"module", r"util", r"helper",
        r"cache", r"caching", r"memoiz", r"optimize",
    ],
    RiskLevel.LOW: [
        # UI/display
        r"admin", r"dashboard", r"display", r"show", r"view", r"list",
        r"add.*button", r"remove.*button", r"ui", r"ux", r"frontend",
        # Styling
        r"style", r"css", r"scss", r"tailwind", r"theme", r"color",
        r"font", r"layout", r"margin", r"padding", r"responsive",
        # Documentation
        r"format", r"comment", r"readme", r"doc", r"documentation",
        r"changelog", r"contributing", r"license",
        # Code quality
        r"lint", r"prettier", r"eslint", r"formatting", r"typo",
        r"fix.*typo", r"fix.*spelling", r"fix.*grammar", r"fix.*format",
        r"rename", r"cleanup", r"refine", r"polish",
        # Local development
        r"local", r"local.host", r"dev.*server", r"hot.*reload",
        # FIX 3.1: Expanded low-risk patterns
        r"docstring", r"jsdoc", r"typedoc", r"sphinx",
        r"test", r"spec", r"unit.*test", r"integration.*test", r"e2e",
        r"mock", r"stub", r"fixture", r"snapshot.*test",
        r"log", r"logger", r"print", r"console\.log", r"console\.debug",
        r"debug.*statement", r"debug.*mode", r"verbose",
        r"lint.*fix", r"format.*code", r"autoformat",
        r"spelling", r"grammar", r"wording", r"text.*change",
        r"readme.*update", r"doc.*update", r"comment.*update",
    ]
}


def detect_risk_level(prompt: str) -> RiskLevel:
    """Detect risk level from prompt text."""
    prompt_lower = prompt.lower()

    for level in [RiskLevel.CRITICAL, RiskLevel.HIGH, RiskLevel.MODERATE, RiskLevel.LOW]:
        for pattern in RISK_KEYWORDS[level]:
            if re.search(pattern, prompt_lower):
                return level

    return RiskLevel.MODERATE  # Default
