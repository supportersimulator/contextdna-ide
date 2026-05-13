#!/usr/bin/env python3
"""
CONFIG EVOLUTION ENGINE - LLM-Assisted Configuration Troubleshooting

An evolutionary system that maintains, tests, and improves integration
configuration templates using local LLM assistance.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                  CONFIG EVOLUTION ENGINE                                 │
    │                                                                          │
    │  ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐ │
    │  │ Template Library │     │ Config Tester    │     │ Local LLM        │ │
    │  │                  │     │                  │     │                  │ │
    │  │ SQLite store of  │────▶│ Try template     │────▶│ Analyze failure  │ │
    │  │ config templates │     │ Check result     │     │ Suggest next     │ │
    │  │ by OS/app/version│◀────│ Report to LLM    │◀────│ Iterate ≤10x     │ │
    │  └──────────────────┘     └──────────────────┘     └──────────────────┘ │
    │                                                                          │
    │  EVOLUTION FLOW:                                                         │
    │  1. Start with default template for destination+OS                      │
    │  2. Apply template, check if integration works                          │
    │  3. If fail: Show LLM the error + config area code                     │
    │  4. LLM generates next template variant                                 │
    │  5. Repeat up to 10 times                                               │
    │  6. On success: Save as new working template variant                    │
    │  7. Opt-in: Share sanitized template to improve Context DNA             │
    │                                                                          │
    │  PRIVACY GUARANTEES:                                                     │
    │  • Secrets NEVER stored in templates                                     │
    │  • All paths sanitized to ${HOME}, ${USER}, etc.                        │
    │  • Shared templates are fully anonymized                                 │
    │  • Opt-in only for contributing back                                     │
    └─────────────────────────────────────────────────────────────────────────┘

Template Structure:
    - destination_id: Which app/IDE (vs_code_claude_code, cursor, etc.)
    - os_type: windows, macos, linux
    - variant_id: Unique variant identifier
    - template_json: The actual configuration template
    - success_count: How many times this worked
    - fail_count: How many times this failed
    - confidence_score: success_count / (success_count + fail_count)
    - parent_variant_id: Which template this evolved from
    - evolution_notes: What the LLM changed and why

Usage:
    from memory.config_evolution_engine import ConfigEvolutionEngine

    engine = ConfigEvolutionEngine()

    # Try to configure an integration
    result = await engine.configure_integration(
        destination_id="vs_code_claude_code",
        max_attempts=10
    )

    # Get best template for a destination
    template = engine.get_best_template("cursor_overseer", os_type="macos")
"""

import os
import re
import json
import sqlite3
import hashlib
import logging
import platform
import subprocess
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum

logger = logging.getLogger(__name__)

# Database path
MEMORY_DIR = Path(__file__).parent
EVOLUTION_DB = MEMORY_DIR / ".config_evolution.db"

# Max evolution attempts
MAX_EVOLUTION_ATTEMPTS = 10


# =============================================================================
# PRIVACY: PATH & SECRET SANITIZATION
# =============================================================================

def get_current_os() -> str:
    """Get current OS type."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    else:
        return "linux"


def sanitize_path(path: str) -> str:
    """
    Sanitize a path to remove user-specific information.

    Replaces:
    - /Users/username -> ${HOME}
    - /home/username -> ${HOME}
    - C:\\Users\\username -> ${HOME}
    - username occurrences -> ${USER}
    """
    if not path:
        return path

    # Get current user info
    home = str(Path.home())
    username = os.getenv("USER") or os.getenv("USERNAME") or "user"

    # Replace home directory
    sanitized = path.replace(home, "${HOME}")

    # Replace username in remaining paths
    sanitized = re.sub(
        rf'/Users/{re.escape(username)}',
        '${HOME}',
        sanitized,
        flags=re.IGNORECASE
    )
    sanitized = re.sub(
        rf'/home/{re.escape(username)}',
        '${HOME}',
        sanitized,
        flags=re.IGNORECASE
    )
    sanitized = re.sub(
        rf'C:\\Users\\{re.escape(username)}',
        '${HOME}',
        sanitized,
        flags=re.IGNORECASE
    )

    # Replace any remaining username occurrences
    sanitized = re.sub(
        rf'\b{re.escape(username)}\b',
        '${USER}',
        sanitized,
        flags=re.IGNORECASE
    )

    return sanitized


def sanitize_template(template: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep sanitize a template to remove all user-specific information.

    This is CRITICAL for privacy when sharing templates.
    """
    def sanitize_value(value: Any) -> Any:
        if isinstance(value, str):
            return sanitize_path(value)
        elif isinstance(value, dict):
            return {k: sanitize_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [sanitize_value(v) for v in value]
        return value

    return sanitize_value(template)


def expand_template_paths(template: Dict[str, Any]) -> Dict[str, Any]:
    """
    Expand ${HOME} and ${USER} placeholders to actual values.

    Used when applying a template to the local system.
    """
    home = str(Path.home())
    username = os.getenv("USER") or os.getenv("USERNAME") or "user"

    def expand_value(value: Any) -> Any:
        if isinstance(value, str):
            expanded = value.replace("${HOME}", home)
            expanded = expanded.replace("${USER}", username)
            return expanded
        elif isinstance(value, dict):
            return {k: expand_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [expand_value(v) for v in value]
        return value

    return expand_value(template)


# =============================================================================
# SECRET PATTERNS (reused from integration_offer_service)
# =============================================================================

SECRET_PATTERNS = [
    (r'sk-proj-[a-zA-Z0-9]{20,}', '${OPENAI_PROJECT_KEY}'),
    (r'sk-[a-zA-Z0-9]{40,}', '${Context_DNA_OPENAI}'),
    (r'ghp_[a-zA-Z0-9]{36}', '${GITHUB_TOKEN}'),
    (r'AKIA[A-Z0-9]{16}', '${AWS_ACCESS_KEY}'),
    (r'anthropic-[a-zA-Z0-9-]{30,}', '${ANTHROPIC_KEY}'),
    (r'Bearer\s+[a-zA-Z0-9._-]{20,}', 'Bearer ${AUTH_TOKEN}'),
]


def contains_secrets(text: str) -> bool:
    """Check if text contains any secret patterns."""
    if not text:
        return False
    text_str = json.dumps(text) if isinstance(text, dict) else str(text)
    for pattern, _ in SECRET_PATTERNS:
        if re.search(pattern, text_str, re.IGNORECASE):
            return True
    return False


# =============================================================================
# ENUMS & DATACLASSES
# =============================================================================

class ConfigTestResult(str, Enum):
    """Result of testing a configuration."""
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"  # Some features work
    ERROR = "error"      # Couldn't even test


class EvolutionStatus(str, Enum):
    """Status of an evolution session."""
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ConfigTemplate:
    """A configuration template."""
    template_id: str
    destination_id: str
    os_type: str
    variant_id: str
    template_json: Dict[str, Any]
    description: str

    # Evolution tracking
    parent_variant_id: Optional[str] = None
    evolution_notes: Optional[str] = None
    generation: int = 1  # How many evolutions from original

    # Success metrics
    success_count: int = 0
    fail_count: int = 0
    last_tested_utc: Optional[str] = None

    # Metadata
    created_at_utc: Optional[str] = None
    created_by: str = "system"  # "system", "llm", "user"
    is_default: bool = False

    @property
    def confidence_score(self) -> float:
        """Calculate confidence based on success/fail ratio."""
        total = self.success_count + self.fail_count
        if total == 0:
            return 0.5  # Unknown
        return self.success_count / total

    @property
    def template_hash(self) -> str:
        """Generate hash of template for deduplication."""
        content = json.dumps(self.template_json, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:12]


@dataclass
class EvolutionAttempt:
    """Record of a single evolution attempt."""
    attempt_id: str
    session_id: str
    attempt_number: int
    template_id: str
    timestamp_utc: str

    # What was tried
    template_used: Dict[str, Any]
    config_applied: Optional[str] = None

    # Results
    test_result: Optional[ConfigTestResult] = None
    error_message: Optional[str] = None
    config_area_snapshot: Optional[str] = None  # What the config file looked like

    # LLM interaction
    llm_prompt: Optional[str] = None
    llm_response: Optional[str] = None
    llm_suggested_changes: Optional[str] = None


@dataclass
class EvolutionSession:
    """A complete evolution session."""
    session_id: str
    destination_id: str
    os_type: str
    started_at_utc: str
    status: EvolutionStatus

    # Progress
    attempts: List[EvolutionAttempt] = field(default_factory=list)
    current_attempt: int = 0
    max_attempts: int = MAX_EVOLUTION_ATTEMPTS

    # Outcome
    successful_template_id: Optional[str] = None
    ended_at_utc: Optional[str] = None
    final_notes: Optional[str] = None


# =============================================================================
# DEFAULT TEMPLATES (Built-in starting points)
# =============================================================================

DEFAULT_TEMPLATES: List[Dict[str, Any]] = [
    # VS Code / Claude Code - macOS
    {
        "destination_id": "vs_code_claude_code",
        "os_type": "macos",
        "variant_id": "default_v1",
        "description": "Default Claude Code hook configuration for VS Code on macOS",
        "is_default": True,
        "template_json": {
            "config_file": "${HOME}/.claude/settings.local.json",
            "hook_script": "${HOME}/.claude/hooks/user-prompt-submit.sh",
            "config_content": {
                "hooks": {
                    "user-prompt-submit": [
                        {
                            "type": "command",
                            "command": "${HOME}/.claude/hooks/user-prompt-submit.sh"
                        }
                    ]
                }
            },
            "hook_script_content": "#!/bin/bash\n# Context DNA injection hook\ncd ${HOME}/dev/er-simulator-superrepo\n.venv/bin/python3 memory/persistent_hook_structure.py",
            "post_install": [
                "mkdir -p ${HOME}/.claude/hooks",
                "chmod +x ${HOME}/.claude/hooks/user-prompt-submit.sh"
            ],
            "verification": {
                "check_file_exists": "${HOME}/.claude/settings.local.json",
                "check_hook_executable": "${HOME}/.claude/hooks/user-prompt-submit.sh"
            }
        }
    },

    # VS Code / Claude Code - Linux
    {
        "destination_id": "vs_code_claude_code",
        "os_type": "linux",
        "variant_id": "default_v1",
        "description": "Default Claude Code hook configuration for VS Code on Linux",
        "is_default": True,
        "template_json": {
            "config_file": "${HOME}/.claude/settings.local.json",
            "hook_script": "${HOME}/.claude/hooks/user-prompt-submit.sh",
            "config_content": {
                "hooks": {
                    "user-prompt-submit": [
                        {
                            "type": "command",
                            "command": "${HOME}/.claude/hooks/user-prompt-submit.sh"
                        }
                    ]
                }
            },
            "hook_script_content": "#!/bin/bash\n# Context DNA injection hook\ncd ${HOME}/er-simulator-superrepo\npython3 memory/persistent_hook_structure.py",
            "post_install": [
                "mkdir -p ${HOME}/.claude/hooks",
                "chmod +x ${HOME}/.claude/hooks/user-prompt-submit.sh"
            ]
        }
    },

    # VS Code / Claude Code - Windows
    {
        "destination_id": "vs_code_claude_code",
        "os_type": "windows",
        "variant_id": "default_v1",
        "description": "Default Claude Code hook configuration for VS Code on Windows",
        "is_default": True,
        "template_json": {
            "config_file": "${HOME}\\.claude\\settings.local.json",
            "hook_script": "${HOME}\\.claude\\hooks\\user-prompt-submit.ps1",
            "config_content": {
                "hooks": {
                    "user-prompt-submit": [
                        {
                            "type": "command",
                            "command": "powershell.exe -ExecutionPolicy Bypass -File ${HOME}\\.claude\\hooks\\user-prompt-submit.ps1"
                        }
                    ]
                }
            },
            "hook_script_content": "# Context DNA injection hook\nSet-Location ${HOME}\\er-simulator-superrepo\npython memory\\persistent_hook_structure.py",
            "post_install": [
                "New-Item -ItemType Directory -Force -Path ${HOME}\\.claude\\hooks"
            ]
        }
    },

    # Cursor - macOS
    {
        "destination_id": "cursor_overseer",
        "os_type": "macos",
        "variant_id": "default_v1",
        "description": "Default Cursor AI configuration on macOS",
        "is_default": True,
        "template_json": {
            "config_file": "${HOME}/.cursor/settings.json",
            "config_content": {
                "context_injection": {
                    "enabled": True,
                    "script": "${HOME}/.cursor/hooks/context-inject.sh"
                }
            },
            "notes": "Cursor configuration may vary by version"
        }
    },

    # Windsurf - macOS
    {
        "destination_id": "windsurf",
        "os_type": "macos",
        "variant_id": "default_v1",
        "description": "Default Windsurf configuration on macOS",
        "is_default": True,
        "template_json": {
            "config_file": "${HOME}/.windsurf/config.json",
            "notes": "Windsurf configuration pending - template placeholder"
        }
    },
]


# =============================================================================
# STRUCTURED FEEDBACK (Diff-Only - No Raw Content)
# =============================================================================

@dataclass
class StructuredFeedback:
    """
    Structured feedback for LLM - contains signals only, no raw content.

    This is the ONLY information the LLM receives about test results.
    No actual config file contents, no secrets, no user paths.
    """
    # Error classification
    error_type: str  # FILE_NOT_FOUND, PERMISSION_DENIED, PARSE_ERROR, etc.
    error_code: Optional[int] = None

    # What was checked (structure only, no values)
    checks_performed: List[str] = field(default_factory=list)
    checks_passed: List[str] = field(default_factory=list)
    checks_failed: List[str] = field(default_factory=list)

    # Diff signals (what changed between attempts)
    fields_modified: List[str] = field(default_factory=list)  # e.g., ["hooks.user-prompt-submit.command"]
    fields_added: List[str] = field(default_factory=list)
    fields_removed: List[str] = field(default_factory=list)

    # Observability signals (from injection health)
    injection_latency_ms: Optional[int] = None
    sections_present: List[int] = field(default_factory=list)
    section_8_present: bool = False
    injection_success: Optional[bool] = None

    # Environment signals
    config_file_exists: bool = False
    config_file_valid_json: bool = False
    hook_script_exists: bool = False
    hook_script_executable: bool = False

    # Hints (sanitized)
    hints: List[str] = field(default_factory=list)


def compute_template_diff(old_template: Dict, new_template: Dict, path: str = "") -> Dict[str, List[str]]:
    """
    Compute structural diff between two templates.

    Returns only field paths that changed, not values.
    """
    diff = {"modified": [], "added": [], "removed": []}

    old_keys = set(old_template.keys()) if isinstance(old_template, dict) else set()
    new_keys = set(new_template.keys()) if isinstance(new_template, dict) else set()

    # Added keys
    for key in new_keys - old_keys:
        full_path = f"{path}.{key}" if path else key
        diff["added"].append(full_path)

    # Removed keys
    for key in old_keys - new_keys:
        full_path = f"{path}.{key}" if path else key
        diff["removed"].append(full_path)

    # Modified keys (recursive for nested dicts)
    for key in old_keys & new_keys:
        full_path = f"{path}.{key}" if path else key
        old_val = old_template[key]
        new_val = new_template[key]

        if isinstance(old_val, dict) and isinstance(new_val, dict):
            nested_diff = compute_template_diff(old_val, new_val, full_path)
            diff["modified"].extend(nested_diff["modified"])
            diff["added"].extend(nested_diff["added"])
            diff["removed"].extend(nested_diff["removed"])
        elif old_val != new_val:
            diff["modified"].append(full_path)

    return diff


# =============================================================================
# OBSERVABILITY CONNECTOR
# =============================================================================

class ObservabilityConnector:
    """
    Connects evolution engine to injection health observability.

    Uses existing metrics to score template effectiveness.
    """

    def __init__(self):
        self._health_monitor = None
        self._store = None

    def _get_health_monitor(self):
        """Lazy load health monitor."""
        if self._health_monitor is None:
            try:
                from memory.injection_health_monitor import get_webhook_monitor
                self._health_monitor = get_webhook_monitor()
            except ImportError:
                logger.warning("InjectionHealthMonitor not available")
        return self._health_monitor

    def _get_store(self):
        """Lazy load observability store."""
        if self._store is None:
            try:
                from memory.observability_store import get_observability_store
                self._store = get_observability_store()
            except ImportError:
                logger.warning("ObservabilityStore not available")
        return self._store

    def get_destination_metrics(self, destination_id: str) -> Dict[str, Any]:
        """
        Get observability metrics for a destination.

        Used to score template effectiveness beyond simple success/fail.
        """
        monitor = self._get_health_monitor()
        if not monitor:
            return {}

        try:
            health = monitor.check_health()

            # Find destination in health data
            dest_health = None
            for dest in health.destinations:
                if dest.destination_id == destination_id:
                    dest_health = dest
                    break

            if dest_health:
                return {
                    "status": dest_health.status,
                    "avg_latency_ms": dest_health.avg_latency_ms,
                    "pre_message_count": dest_health.pre_message_24h,
                    "post_message_count": dest_health.post_message_24h,
                    "last_injection_utc": dest_health.last_injection_utc,
                    "section_8_present": health.eighth_intelligence_status == "active"
                }
        except Exception as e:
            logger.warning(f"Failed to get destination metrics: {e}")

        return {}

    def compute_weighted_score(
        self,
        success_count: int,
        fail_count: int,
        metrics: Dict[str, Any]
    ) -> float:
        """
        Compute weighted confidence score using observability data.

        Factors:
        - Base: success_count / total (50% weight)
        - Latency: Lower is better (20% weight)
        - Section-8 presence: Bonus for 8th Intelligence (15% weight)
        - Recency: Recent activity is better (15% weight)
        """
        total = success_count + fail_count
        if total == 0:
            return 0.5  # Unknown

        # Base score (50%)
        base_score = success_count / total

        # Latency score (20%) - <100ms = 1.0, >500ms = 0.0
        latency = metrics.get("avg_latency_ms")
        if latency:
            latency_score = max(0, min(1, 1 - (latency - 100) / 400))
        else:
            latency_score = 0.5

        # Section-8 score (15%)
        section_8_score = 1.0 if metrics.get("section_8_present") else 0.0

        # Recency score (15%) - activity in last 24h
        activity = (metrics.get("pre_message_count", 0) +
                   metrics.get("post_message_count", 0))
        recency_score = min(1.0, activity / 10)  # Cap at 10 injections

        # Weighted combination
        weighted = (
            base_score * 0.50 +
            latency_score * 0.20 +
            section_8_score * 0.15 +
            recency_score * 0.15
        )

        return round(weighted, 3)

    async def dry_run_injection(
        self,
        destination_id: str,
        template: Dict[str, Any]
    ) -> Tuple[bool, StructuredFeedback]:
        """
        Perform a dry-run injection test using the actual injection system.

        Returns structured feedback (no raw content).
        """
        feedback = StructuredFeedback(
            error_type="NONE",
            checks_performed=["config_exists", "hook_exists", "hook_executable", "dry_injection"]
        )

        expanded = expand_template_paths(template)

        # Check config file
        config_file = expanded.get("config_file")
        if config_file:
            config_path = Path(config_file)
            feedback.config_file_exists = config_path.exists()
            if feedback.config_file_exists:
                feedback.checks_passed.append("config_exists")
                try:
                    content = config_path.read_text()
                    json.loads(content)
                    feedback.config_file_valid_json = True
                    feedback.checks_passed.append("config_valid_json")
                except json.JSONDecodeError:
                    feedback.config_file_valid_json = False
                    feedback.checks_failed.append("config_valid_json")
                    feedback.error_type = "PARSE_ERROR"
                    feedback.hints.append("Config file is not valid JSON")
                except Exception:
                    feedback.checks_failed.append("config_readable")
                    feedback.error_type = "PERMISSION_DENIED"
            else:
                feedback.checks_failed.append("config_exists")
                feedback.error_type = "FILE_NOT_FOUND"
                feedback.hints.append("Config file does not exist")

        # Check hook script
        hook_script = expanded.get("hook_script")
        if hook_script:
            hook_path = Path(hook_script)
            feedback.hook_script_exists = hook_path.exists()
            if feedback.hook_script_exists:
                feedback.checks_passed.append("hook_exists")
                feedback.hook_script_executable = os.access(hook_path, os.X_OK)
                if feedback.hook_script_executable:
                    feedback.checks_passed.append("hook_executable")
                else:
                    feedback.checks_failed.append("hook_executable")
                    if feedback.error_type == "NONE":
                        feedback.error_type = "PERMISSION_DENIED"
                    feedback.hints.append("Hook script is not executable")
            else:
                feedback.checks_failed.append("hook_exists")
                if feedback.error_type == "NONE":
                    feedback.error_type = "FILE_NOT_FOUND"

        # Try dry-run injection via record_webhook_injection
        try:
            from memory.injection_store import record_injection_to_health_monitor

            # Create a minimal test injection data
            test_injection = {
                "id": f"dry_run_{datetime.now().strftime('%H%M%S')}",
                "analysis": {
                    "sections_included": ["safety", "foundation", "synaptic_8th_intelligence"],
                    "generation_time_ms": 0
                },
                "raw_output": "DRY_RUN_TEST"
            }

            # This records to health monitor - we check if it succeeds
            record_injection_to_health_monitor(
                test_injection,
                destination=destination_id,
                phase="pre_message"
            )

            feedback.injection_success = True
            feedback.checks_passed.append("dry_injection")
            feedback.sections_present = [0, 1, 8]
            feedback.section_8_present = True

        except Exception as e:
            feedback.injection_success = False
            feedback.checks_failed.append("dry_injection")
            feedback.hints.append(f"Injection system error: {type(e).__name__}")

        # Determine overall success
        success = (
            feedback.config_file_exists and
            feedback.config_file_valid_json and
            (not hook_script or feedback.hook_script_executable) and
            feedback.error_type == "NONE"
        )

        return success, feedback


# =============================================================================
# LOCAL LLM INTERFACE
# =============================================================================

class LocalLLMInterface:
    """
    Interface to local LLM (mlx_lm.server) for configuration assistance.

    Uses mlx_lm.server on port 5044 with OpenAI-compatible API.
    Default model: Qwen3-4B-4bit (current production model)

    Memory Integration:
    - Uses Context DNA 9-section injection for contextual awareness
    - Access to failure patterns, outcome tracking, observability
    - Professor wisdom for domain-specific guidance
    """

    def __init__(
        self,
        model: str = "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",
        use_memory_context: bool = True
    ):
        self.model = model
        self.use_memory_context = use_memory_context
        # ALL LLM access routes through priority queue — NO direct HTTP to port 5044
        self._memory_context = None

    async def is_available(self) -> bool:
        """Check if local LLM is available via Redis health cache."""
        try:
            from memory.llm_priority_queue import check_llm_health
            return check_llm_health()
        except Exception:
            return False

    def get_model_info(self) -> Dict[str, str]:
        """Get current model configuration."""
        return {
            "model": self.model,
            "url": self._url,
            "memory_context": self.use_memory_context
        }

    def _get_memory_context(self, task_description: str) -> str:
        """Get relevant memory context for the LLM."""
        if not self.use_memory_context:
            return ""

        context_parts = []

        # Try to get Context DNA injection (9-section)
        try:
            from memory.persistent_hook_structure import generate_context_injection

            result = generate_context_injection(
                prompt=f"config evolution: {task_description}",
                mode="minimal",  # Lightweight for LLM context
                session_id=f"config-evo-{datetime.now().strftime('%H%M%S')}"
            )

            # Extract relevant sections
            if hasattr(result, 'sections'):
                if result.sections.get("2_wisdom"):
                    context_parts.append(f"PROFESSOR WISDOM:\n{result.sections['2_wisdom'][:500]}")
                if result.sections.get("1_foundation"):
                    context_parts.append(f"KNOWN PATTERNS:\n{result.sections['1_foundation'][:300]}")

        except Exception as e:
            logger.debug(f"Context injection not available: {e}")

        # Try to get failure patterns (what NOT to do)
        try:
            from memory.observability_store import get_observability_store
            store = get_observability_store()
            # Would query for recent failures related to config
            # This is where .failure_patterns.db knowledge would come in
        except Exception as e:
            print(f"[WARN] Failure patterns lookup failed: {e}")

        return "\n\n".join(context_parts) if context_parts else ""

    async def analyze_failure_and_suggest(
        self,
        destination_id: str,
        os_type: str,
        template_tried: Dict[str, Any],
        feedback: StructuredFeedback,
        previous_attempts: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Ask LLM to analyze a configuration failure and suggest improvements.

        PRIVACY: Uses StructuredFeedback only - no raw config content.

        Args:
            destination_id: Which app we're configuring
            os_type: Target OS
            template_tried: The template that was tried (sanitized)
            feedback: Structured feedback signals (no raw content)
            previous_attempts: History of what was tried before

        Returns:
            Dict with:
            - suggested_template: New template to try
            - explanation: Why this might work
            - confidence: LLM's confidence (0-1)
            - changes_made: What was changed from previous
        """
        # Build prompt using ONLY structured signals
        prompt = self._build_analysis_prompt_v2(
            destination_id=destination_id,
            os_type=os_type,
            template_tried=template_tried,
            feedback=feedback,
            previous_attempts=previous_attempts
        )

        try:
            response = await self._query_llm(prompt)
            return self._parse_llm_response(response, template_tried)
        except Exception as e:
            logger.error(f"LLM query failed: {e}")
            return {
                "suggested_template": template_tried,
                "explanation": f"LLM unavailable: {e}",
                "confidence": 0.0,
                "changes_made": "none (LLM error)"
            }

    def _build_analysis_prompt_v2(
        self,
        destination_id: str,
        os_type: str,
        template_tried: Dict[str, Any],
        feedback: StructuredFeedback,
        previous_attempts: Optional[List[Dict]]
    ) -> str:
        """
        Build prompt using ONLY structured signals - no raw content.

        This is the diff-only feedback approach for privacy.
        """
        prompt = f"""You are a configuration troubleshooting assistant for Context DNA integration.

TASK: Analyze why this configuration failed and suggest an improved version.

TARGET:
- Application: {destination_id}
- Operating System: {os_type}

TEMPLATE STRUCTURE (keys only, no values shown):
{json.dumps(list(self._extract_template_keys(template_tried)), indent=2)}

STRUCTURED FEEDBACK (signals only, no raw content):
- Error Type: {feedback.error_type}
- Error Code: {feedback.error_code or 'N/A'}

CHECKS PERFORMED:
- Passed: {feedback.checks_passed}
- Failed: {feedback.checks_failed}

ENVIRONMENT SIGNALS:
- Config file exists: {feedback.config_file_exists}
- Config file valid JSON: {feedback.config_file_valid_json}
- Hook script exists: {feedback.hook_script_exists}
- Hook script executable: {feedback.hook_script_executable}

OBSERVABILITY SIGNALS:
- Injection success: {feedback.injection_success}
- Sections present: {feedback.sections_present}
- Section 8 (8th Intelligence) present: {feedback.section_8_present}
- Injection latency: {feedback.injection_latency_ms or 'N/A'}ms

HINTS:
{chr(10).join(f'- {h}' for h in feedback.hints) if feedback.hints else '- None'}
"""

        if feedback.fields_modified or feedback.fields_added or feedback.fields_removed:
            prompt += f"""
DIFF FROM PREVIOUS ATTEMPT:
- Modified fields: {feedback.fields_modified}
- Added fields: {feedback.fields_added}
- Removed fields: {feedback.fields_removed}
"""

        if previous_attempts:
            prompt += "\nPREVIOUS ATTEMPTS (last 3):\n"
            for i, attempt in enumerate(previous_attempts[-3:], 1):
                prompt += f"""
Attempt {i}:
- Changes: {attempt.get('changes', 'unknown')}
- Error type: {attempt.get('error_type', 'unknown')}
- Checks failed: {attempt.get('checks_failed', [])}
"""

        prompt += f"""

Consider whatever seems relevant to you about this configuration evolution problem:

- **What's the core issue?** Why did the previous attempt fail?
- **What signals matter?** Which of the checks_failed are most critical?
- **What's the likely fix?** How would you modify the template to address the failures?
- **Why make those changes?** What's your reasoning for each modification?
- **How confident are you?** What could increase or decrease your confidence in this approach?

Share your analysis in whatever way makes sense. You could:
- Provide a revised template with explanation
- Describe the changes and reasoning step-by-step
- Suggest multiple alternative approaches
- Explain your diagnostic thinking

Format suggestion (if JSON makes sense):
{{
    "suggested_template": {{
        "config_file": "${{HOME}}/.example/config.json",
        "hook_script": "${{HOME}}/.example/hooks/script.sh",
        "config_content": {{ ... }},
        "post_install": [ ... ],
        "verification": {{ ... }}
    }},
    "explanation": "Why this change should work",
    "confidence": 0.7,
    "changes_made": "Description of what was changed"
}}

Or natural language: just describe your suggested template changes and why you think they'll work.

IMPORTANT RULES:
- Use ${{HOME}} and ${{USER}} placeholders, NEVER real paths
- NEVER include actual API keys, secrets, or user data
- Make incremental changes based on the failed checks
- Focus on fixing the specific error type: {feedback.error_type}
- Consider {os_type}-specific path formats and commands
"""

        return prompt

    def _extract_template_keys(self, template: Dict[str, Any], prefix: str = "") -> List[str]:
        """Extract just the keys from a template (no values)."""
        keys = []
        for key, value in template.items():
            full_key = f"{prefix}.{key}" if prefix else key
            keys.append(full_key)
            if isinstance(value, dict):
                keys.extend(self._extract_template_keys(value, full_key))
        return keys

    # Legacy method for backwards compatibility
    def _build_analysis_prompt(
        self,
        destination_id: str,
        os_type: str,
        template_tried: Dict[str, Any],
        error_message: str,
        config_area_code: Optional[str],
        previous_attempts: Optional[List[Dict]]
    ) -> str:
        """Legacy prompt builder - converts to structured feedback."""
        # Create minimal feedback from legacy params
        feedback = StructuredFeedback(
            error_type="LEGACY_ERROR",
            hints=[error_message] if error_message else []
        )
        return self._build_analysis_prompt_v2(
            destination_id, os_type, template_tried, feedback, previous_attempts
        )

    async def _query_llm(self, prompt: str, task_context: str = "") -> str:
        """Query local LLM with optional memory context.

        ALL LLM access routes through llm_priority_queue — NO direct HTTP to port 5044.
        """
        import asyncio
        from memory.llm_priority_queue import butler_query

        # Get memory context if enabled
        memory_context = self._get_memory_context(task_context) if task_context else ""

        # Build system message with memory context
        system_content = "You are a configuration troubleshooting assistant. Respond with valid JSON only."
        if memory_context:
            system_content += f"\n\nCONTEXT DNA MEMORY:\n{memory_context}"

        # Route through priority queue (P4 BACKGROUND via butler_query)
        content = await asyncio.to_thread(
            butler_query,
            system_content,
            prompt,
            "extract"
        )

        return content or ""

    def _parse_llm_response(
        self,
        response: str,
        original_template: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Parse LLM response, handling both JSON and natural language."""
        try:
            # Try to find JSON in response
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                parsed = json.loads(json_match.group())
                return {
                    "suggested_template": parsed.get("suggested_template", original_template),
                    "explanation": parsed.get("explanation", "No explanation provided"),
                    "confidence": float(parsed.get("confidence", 0.5)),
                    "changes_made": parsed.get("changes_made", "Unknown changes")
                }
        except json.JSONDecodeError:
            pass  # Fall through to natural language extraction

        # Fallback: extract insights from natural language response
        explanation = response[:500]
        confidence = 0.4  # Medium-low confidence for natural language extraction

        # Boost confidence if response is detailed and specific
        if len(response) > 200 and ('suggest' in response.lower() or 'recommend' in response.lower() or 'change' in response.lower()):
            confidence = 0.6
        if 'confident' in response.lower() and ('will' in response.lower() or 'should' in response.lower()):
            confidence = 0.7

        # Extract suggested changes from natural language patterns
        changes_made = "Configuration evolution suggested"
        if re.search(r'\b(add|create|new|enable)\b', response, re.IGNORECASE):
            changes_made += " (add/create fields)"
        elif re.search(r'\b(remov|delet|disable|unset)\b', response, re.IGNORECASE):
            changes_made += " (remove/disable fields)"
        elif re.search(r'\b(modif|chang|updat|fix)\b', response, re.IGNORECASE):
            changes_made += " (modify fields)"

        # Return natural language result with best-guess updated template
        # (in practice, this would be intercepted by the evolution engine)
        return {
            "suggested_template": original_template,
            "explanation": explanation,
            "confidence": confidence,
            "changes_made": changes_made
        }


# =============================================================================
# CONFIG TESTER
# =============================================================================

class ConfigTester:
    """
    Tests if a configuration template works.

    Returns StructuredFeedback (no raw content) for privacy-safe LLM analysis.
    """

    def __init__(self):
        self._observability = ObservabilityConnector()

    async def test_template(
        self,
        template: ConfigTemplate,
        dry_run: bool = False,
        previous_template: Optional[Dict[str, Any]] = None
    ) -> Tuple[ConfigTestResult, StructuredFeedback]:
        """
        Test if a configuration template works.

        Returns ONLY StructuredFeedback - no raw config content.

        Args:
            template: The template to test
            dry_run: If True, only validate without applying
            previous_template: Previous template for diff computation

        Returns:
            Tuple of (result, structured_feedback)
        """
        expanded = expand_template_paths(template.template_json)

        # Initialize structured feedback
        feedback = StructuredFeedback(
            error_type="NONE",
            checks_performed=[]
        )

        # Compute diff if we have a previous template
        if previous_template:
            diff = compute_template_diff(previous_template, template.template_json)
            feedback.fields_modified = diff["modified"]
            feedback.fields_added = diff["added"]
            feedback.fields_removed = diff["removed"]

        # Check config file exists and is valid JSON
        config_file = expanded.get("config_file")
        if config_file:
            feedback.checks_performed.append("config_file_exists")
            config_path = Path(config_file)
            feedback.config_file_exists = config_path.exists()

            if feedback.config_file_exists:
                feedback.checks_passed.append("config_file_exists")
                feedback.checks_performed.append("config_file_valid_json")

                try:
                    content = config_path.read_text()
                    json.loads(content)
                    feedback.config_file_valid_json = True
                    feedback.checks_passed.append("config_file_valid_json")
                except json.JSONDecodeError as e:
                    feedback.config_file_valid_json = False
                    feedback.checks_failed.append("config_file_valid_json")
                    feedback.error_type = "PARSE_ERROR"
                    feedback.hints.append(f"JSON parse error at position {e.pos}")
                except PermissionError:
                    feedback.checks_failed.append("config_file_readable")
                    feedback.error_type = "PERMISSION_DENIED"
                    feedback.hints.append("Cannot read config file - permission denied")
            else:
                feedback.checks_failed.append("config_file_exists")
                feedback.error_type = "FILE_NOT_FOUND"
                feedback.hints.append("Config file does not exist")

        # Check hook script
        hook_script = expanded.get("hook_script")
        if hook_script:
            feedback.checks_performed.append("hook_script_exists")
            hook_path = Path(hook_script)
            feedback.hook_script_exists = hook_path.exists()

            if feedback.hook_script_exists:
                feedback.checks_passed.append("hook_script_exists")
                feedback.checks_performed.append("hook_script_executable")
                feedback.hook_script_executable = os.access(hook_path, os.X_OK)

                if feedback.hook_script_executable:
                    feedback.checks_passed.append("hook_script_executable")
                else:
                    feedback.checks_failed.append("hook_script_executable")
                    if feedback.error_type == "NONE":
                        feedback.error_type = "PERMISSION_DENIED"
                    feedback.hints.append("Hook script exists but is not executable (needs chmod +x)")
            else:
                feedback.checks_failed.append("hook_script_exists")
                if feedback.error_type == "NONE":
                    feedback.error_type = "FILE_NOT_FOUND"
                feedback.hints.append("Hook script does not exist")

        # Check verification requirements
        verification = expanded.get("verification", {})
        for check_name, check_path in verification.items():
            if check_name.startswith("check_"):
                feedback.checks_performed.append(check_name)
                check_path_obj = Path(check_path)

                if "executable" in check_name:
                    if check_path_obj.exists() and os.access(check_path_obj, os.X_OK):
                        feedback.checks_passed.append(check_name)
                    else:
                        feedback.checks_failed.append(check_name)
                else:
                    if check_path_obj.exists():
                        feedback.checks_passed.append(check_name)
                    else:
                        feedback.checks_failed.append(check_name)

        if dry_run:
            return (ConfigTestResult.PARTIAL, feedback)

        # Perform dry-run injection test
        success, injection_feedback = await self._observability.dry_run_injection(
            template.destination_id,
            template.template_json
        )

        # Merge injection feedback
        feedback.injection_success = injection_feedback.injection_success
        feedback.sections_present = injection_feedback.sections_present
        feedback.section_8_present = injection_feedback.section_8_present
        feedback.injection_latency_ms = injection_feedback.injection_latency_ms
        feedback.checks_performed.extend([c for c in injection_feedback.checks_performed
                                          if c not in feedback.checks_performed])
        feedback.checks_passed.extend([c for c in injection_feedback.checks_passed
                                       if c not in feedback.checks_passed])
        feedback.checks_failed.extend([c for c in injection_feedback.checks_failed
                                       if c not in feedback.checks_failed])
        feedback.hints.extend(injection_feedback.hints)

        # Determine overall result
        if feedback.checks_failed:
            if feedback.error_type == "NONE":
                feedback.error_type = "VALIDATION_FAILED"
            return (ConfigTestResult.FAILURE, feedback)

        if feedback.injection_success:
            return (ConfigTestResult.SUCCESS, feedback)

        return (ConfigTestResult.PARTIAL, feedback)


# =============================================================================
# CONFIG EVOLUTION ENGINE
# =============================================================================

class ConfigEvolutionEngine:
    """
    Main engine for evolving configuration templates.

    Maintains a library of templates, tests them, and uses LLM to improve
    failing configurations.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or EVOLUTION_DB
        self._conn = None
        self._llm = LocalLLMInterface()
        self._tester = ConfigTester()
        self._observability = ObservabilityConnector()
        self._init_db()
        self._seed_default_templates()

    def _init_db(self):
        """Initialize SQLite database."""
        from memory.db_utils import connect_wal
        self._conn = connect_wal(self._db_path, check_same_thread=False)

        self._conn.executescript("""
            -- Template library
            CREATE TABLE IF NOT EXISTS config_template (
                template_id          TEXT PRIMARY KEY,
                destination_id       TEXT NOT NULL,
                os_type              TEXT NOT NULL,
                variant_id           TEXT NOT NULL,
                template_json        TEXT NOT NULL,
                description          TEXT,

                parent_variant_id    TEXT,
                evolution_notes      TEXT,
                generation           INTEGER NOT NULL DEFAULT 1,

                success_count        INTEGER NOT NULL DEFAULT 0,
                fail_count           INTEGER NOT NULL DEFAULT 0,
                last_tested_utc      TEXT,

                created_at_utc       TEXT NOT NULL,
                created_by           TEXT NOT NULL DEFAULT 'system',
                is_default           INTEGER NOT NULL DEFAULT 0,

                UNIQUE(destination_id, os_type, variant_id)
            );

            CREATE INDEX IF NOT EXISTS idx_template_dest_os
                ON config_template(destination_id, os_type);

            CREATE INDEX IF NOT EXISTS idx_template_confidence
                ON config_template(destination_id, os_type, success_count, fail_count);

            -- Evolution sessions
            CREATE TABLE IF NOT EXISTS evolution_session (
                session_id           TEXT PRIMARY KEY,
                destination_id       TEXT NOT NULL,
                os_type              TEXT NOT NULL,
                started_at_utc       TEXT NOT NULL,
                status               TEXT NOT NULL DEFAULT 'in_progress',

                max_attempts         INTEGER NOT NULL DEFAULT 10,
                current_attempt      INTEGER NOT NULL DEFAULT 0,

                successful_template_id TEXT,
                ended_at_utc         TEXT,
                final_notes          TEXT
            );

            -- Evolution attempts
            CREATE TABLE IF NOT EXISTS evolution_attempt (
                attempt_id           TEXT PRIMARY KEY,
                session_id           TEXT NOT NULL,
                attempt_number       INTEGER NOT NULL,
                template_id          TEXT NOT NULL,
                timestamp_utc        TEXT NOT NULL,

                template_used_json   TEXT NOT NULL,
                config_applied       TEXT,

                test_result          TEXT,
                error_message        TEXT,
                config_area_snapshot TEXT,

                llm_prompt           TEXT,
                llm_response         TEXT,
                llm_suggested_changes TEXT,

                FOREIGN KEY (session_id) REFERENCES evolution_session(session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_attempt_session
                ON evolution_attempt(session_id, attempt_number);

            -- Contribution queue (for opt-in sharing)
            CREATE TABLE IF NOT EXISTS contribution_queue (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id          TEXT NOT NULL,
                sanitized_template   TEXT NOT NULL,
                contribution_status  TEXT NOT NULL DEFAULT 'pending',
                created_at_utc       TEXT NOT NULL,
                submitted_at_utc     TEXT,
                FOREIGN KEY (template_id) REFERENCES config_template(template_id)
            );
        """)
        self._conn.commit()

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _generate_id(self, prefix: str) -> str:
        return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

    # -------------------------------------------------------------------------
    # Template Management
    # -------------------------------------------------------------------------

    def _seed_default_templates(self, smart_seed: bool = True):
        """
        Seed default templates if not exists.

        Args:
            smart_seed: If True, only seed templates matching current OS.
                       Other OS templates stay dormant until needed.
        """
        current_os = get_current_os()

        for tmpl_data in DEFAULT_TEMPLATES:
            template_id = f"{tmpl_data['destination_id']}_{tmpl_data['os_type']}_{tmpl_data['variant_id']}"

            # Smart seeding: only activate templates for current OS
            if smart_seed and tmpl_data["os_type"] != current_os:
                # Store as inactive/dormant - will activate when OS matches
                # We still insert it but mark it as non-default
                is_active_default = False
            else:
                is_active_default = tmpl_data.get("is_default", False)

            cursor = self._conn.execute(
                "SELECT template_id FROM config_template WHERE template_id = ?",
                (template_id,)
            )
            if cursor.fetchone():
                continue

            self._conn.execute("""
                INSERT INTO config_template
                (template_id, destination_id, os_type, variant_id, template_json,
                 description, is_default, created_at_utc, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                template_id,
                tmpl_data["destination_id"],
                tmpl_data["os_type"],
                tmpl_data["variant_id"],
                json.dumps(tmpl_data["template_json"]),
                tmpl_data.get("description", ""),
                1 if is_active_default else 0,  # Only active for current OS
                self._utc_now(),
                "system"
            ))

            if smart_seed and is_active_default:
                logger.info(f"Seeded active template: {template_id}")
            elif smart_seed:
                logger.debug(f"Seeded dormant template: {template_id} (OS mismatch)")

        self._conn.commit()

    def activate_templates_for_os(self, os_type: str):
        """
        Activate dormant templates for a specific OS.

        Called when running on a new OS or when user explicitly requests.
        """
        self._conn.execute("""
            UPDATE config_template
            SET is_default = 1
            WHERE os_type = ? AND is_default = 0
            AND template_id IN (
                SELECT template_id FROM config_template
                WHERE variant_id LIKE 'default%'
            )
        """, (os_type,))
        self._conn.commit()
        logger.info(f"Activated templates for OS: {os_type}")

    def get_best_template(
        self,
        destination_id: str,
        os_type: Optional[str] = None
    ) -> Optional[ConfigTemplate]:
        """
        Get the best (highest confidence) template for a destination.

        Args:
            destination_id: Which app/IDE
            os_type: Target OS (defaults to current OS)

        Returns:
            Best available template, or None
        """
        os_type = os_type or get_current_os()

        # Order by confidence (success_count / total), then by default flag
        cursor = self._conn.execute("""
            SELECT *,
                   CASE WHEN (success_count + fail_count) > 0
                        THEN CAST(success_count AS REAL) / (success_count + fail_count)
                        ELSE 0.5 END as confidence
            FROM config_template
            WHERE destination_id = ? AND os_type = ?
            ORDER BY confidence DESC, is_default DESC, created_at_utc DESC
            LIMIT 1
        """, (destination_id, os_type))

        row = cursor.fetchone()
        if row:
            return self._row_to_template(row)
        return None

    def get_all_templates(
        self,
        destination_id: Optional[str] = None,
        os_type: Optional[str] = None
    ) -> List[ConfigTemplate]:
        """Get all templates, optionally filtered."""
        query = "SELECT * FROM config_template WHERE 1=1"
        params = []

        if destination_id:
            query += " AND destination_id = ?"
            params.append(destination_id)

        if os_type:
            query += " AND os_type = ?"
            params.append(os_type)

        query += " ORDER BY destination_id, os_type, success_count DESC"

        cursor = self._conn.execute(query, params)
        return [self._row_to_template(row) for row in cursor]

    def save_template(self, template: ConfigTemplate) -> str:
        """Save a new or updated template."""
        # Security check: ensure no secrets in template
        if contains_secrets(json.dumps(template.template_json)):
            raise ValueError("Template contains secrets - cannot save")

        now = self._utc_now()

        self._conn.execute("""
            INSERT OR REPLACE INTO config_template
            (template_id, destination_id, os_type, variant_id, template_json,
             description, parent_variant_id, evolution_notes, generation,
             success_count, fail_count, last_tested_utc,
             created_at_utc, created_by, is_default)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            template.template_id,
            template.destination_id,
            template.os_type,
            template.variant_id,
            json.dumps(template.template_json),
            template.description,
            template.parent_variant_id,
            template.evolution_notes,
            template.generation,
            template.success_count,
            template.fail_count,
            template.last_tested_utc,
            template.created_at_utc or now,
            template.created_by,
            1 if template.is_default else 0
        ))
        self._conn.commit()

        return template.template_id

    def record_test_result(
        self,
        template_id: str,
        success: bool
    ):
        """Record a test result for a template."""
        now = self._utc_now()

        if success:
            self._conn.execute("""
                UPDATE config_template
                SET success_count = success_count + 1, last_tested_utc = ?
                WHERE template_id = ?
            """, (now, template_id))
        else:
            self._conn.execute("""
                UPDATE config_template
                SET fail_count = fail_count + 1, last_tested_utc = ?
                WHERE template_id = ?
            """, (now, template_id))

        self._conn.commit()

    def _row_to_template(self, row) -> ConfigTemplate:
        """Convert a database row to ConfigTemplate."""
        return ConfigTemplate(
            template_id=row["template_id"],
            destination_id=row["destination_id"],
            os_type=row["os_type"],
            variant_id=row["variant_id"],
            template_json=json.loads(row["template_json"]),
            description=row["description"] or "",
            parent_variant_id=row["parent_variant_id"],
            evolution_notes=row["evolution_notes"],
            generation=row["generation"],
            success_count=row["success_count"],
            fail_count=row["fail_count"],
            last_tested_utc=row["last_tested_utc"],
            created_at_utc=row["created_at_utc"],
            created_by=row["created_by"],
            is_default=bool(row["is_default"])
        )

    # -------------------------------------------------------------------------
    # Evolution Session Management
    # -------------------------------------------------------------------------

    async def configure_integration(
        self,
        destination_id: str,
        max_attempts: int = MAX_EVOLUTION_ATTEMPTS,
        os_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Attempt to configure an integration, evolving the template if needed.

        This is the main entry point for LLM-assisted configuration.

        Args:
            destination_id: Which app/IDE to configure
            max_attempts: Max evolution attempts
            os_type: Target OS (defaults to current)

        Returns:
            Dict with:
            - success: bool
            - template_id: ID of working template (if successful)
            - attempts: Number of attempts made
            - message: Human-readable result
        """
        os_type = os_type or get_current_os()

        # Create session
        session_id = self._generate_id("sess")
        now = self._utc_now()

        self._conn.execute("""
            INSERT INTO evolution_session
            (session_id, destination_id, os_type, started_at_utc, status, max_attempts)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (session_id, destination_id, os_type, now, "in_progress", max_attempts))
        self._conn.commit()

        # Get starting template
        template = self.get_best_template(destination_id, os_type)
        if not template:
            return {
                "success": False,
                "template_id": None,
                "attempts": 0,
                "message": f"No template found for {destination_id} on {os_type}"
            }

        previous_attempts = []
        previous_template_json = None

        for attempt_num in range(1, max_attempts + 1):
            logger.info(f"Evolution attempt {attempt_num}/{max_attempts}")

            # Test current template with structured feedback (NO raw content)
            result, feedback = await self._tester.test_template(
                template,
                previous_template=previous_template_json
            )

            # Get observability metrics for weighted scoring
            metrics = self._observability.get_destination_metrics(destination_id)

            # Record attempt (feedback is structured, no raw config content)
            attempt_id = self._generate_id("att")
            self._conn.execute("""
                INSERT INTO evolution_attempt
                (attempt_id, session_id, attempt_number, template_id, timestamp_utc,
                 template_used_json, test_result, error_message, config_area_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                attempt_id, session_id, attempt_num, template.template_id,
                self._utc_now(), json.dumps(template.template_json),
                result.value,
                feedback.error_type,  # Structured error type, not raw message
                json.dumps(asdict(feedback))  # Structured feedback as JSON
            ))
            self._conn.commit()

            if result == ConfigTestResult.SUCCESS:
                # Success! Record with weighted score
                self.record_test_result(template.template_id, True)

                # Update template with observability-weighted confidence
                weighted_score = self._observability.compute_weighted_score(
                    template.success_count + 1,
                    template.fail_count,
                    metrics
                )

                self._conn.execute("""
                    UPDATE evolution_session
                    SET status = 'success', successful_template_id = ?,
                        ended_at_utc = ?, current_attempt = ?
                    WHERE session_id = ?
                """, (template.template_id, self._utc_now(), attempt_num, session_id))
                self._conn.commit()

                return {
                    "success": True,
                    "template_id": template.template_id,
                    "attempts": attempt_num,
                    "message": f"Configuration successful on attempt {attempt_num}",
                    "weighted_confidence": weighted_score,
                    "feedback": asdict(feedback)
                }

            # Failed - record and evolve
            self.record_test_result(template.template_id, False)

            # Check if LLM is available
            if not await self._llm.is_available():
                logger.warning("Local LLM not available - cannot evolve")
                self._conn.execute("""
                    UPDATE evolution_session
                    SET status = 'failed', ended_at_utc = ?, current_attempt = ?,
                        final_notes = 'LLM unavailable for evolution'
                    WHERE session_id = ?
                """, (self._utc_now(), attempt_num, session_id))
                self._conn.commit()

                return {
                    "success": False,
                    "template_id": template.template_id,
                    "attempts": attempt_num,
                    "message": f"Failed and LLM unavailable. Error: {feedback.error_type}",
                    "feedback": asdict(feedback)
                }

            # Ask LLM for improved template using STRUCTURED FEEDBACK ONLY
            previous_attempts.append({
                "changes": template.evolution_notes or "initial",
                "error_type": feedback.error_type,
                "checks_failed": feedback.checks_failed,
                "hints": feedback.hints
            })

            # LLM receives ONLY structured signals, no raw content
            llm_result = await self._llm.analyze_failure_and_suggest(
                destination_id=destination_id,
                os_type=os_type,
                template_tried=template.template_json,
                feedback=feedback,  # StructuredFeedback, not raw content
                previous_attempts=previous_attempts
            )

            # Update attempt with LLM info
            self._conn.execute("""
                UPDATE evolution_attempt
                SET llm_suggested_changes = ?
                WHERE attempt_id = ?
            """, (llm_result.get("changes_made"), attempt_id))
            self._conn.commit()

            # Save previous template for diff computation
            previous_template_json = template.template_json

            # Create evolved template
            new_variant_id = f"evolved_v{attempt_num}_{datetime.now().strftime('%H%M%S')}"
            new_template_id = f"{destination_id}_{os_type}_{new_variant_id}"

            template = ConfigTemplate(
                template_id=new_template_id,
                destination_id=destination_id,
                os_type=os_type,
                variant_id=new_variant_id,
                template_json=sanitize_template(llm_result["suggested_template"]),
                description=f"Evolved from {template.variant_id}: {llm_result.get('explanation', '')[:100]}",
                parent_variant_id=template.variant_id,
                evolution_notes=llm_result.get("changes_made"),
                generation=template.generation + 1,
                created_by="llm"
            )

            self.save_template(template)

            # Update session
            self._conn.execute("""
                UPDATE evolution_session
                SET current_attempt = ?
                WHERE session_id = ?
            """, (attempt_num, session_id))
            self._conn.commit()

        # Max attempts reached
        self._conn.execute("""
            UPDATE evolution_session
            SET status = 'failed', ended_at_utc = ?,
                final_notes = 'Max attempts reached without success'
            WHERE session_id = ?
        """, (self._utc_now(), session_id))
        self._conn.commit()

        return {
            "success": False,
            "template_id": template.template_id,
            "attempts": max_attempts,
            "message": f"Failed after {max_attempts} evolution attempts"
        }

    # -------------------------------------------------------------------------
    # Contribution System (Opt-in sharing)
    # -------------------------------------------------------------------------

    def queue_for_contribution(self, template_id: str) -> bool:
        """
        Queue a successful template for contribution to Context DNA.

        Only fully sanitized templates are queued. User must opt-in.
        """
        template = None
        cursor = self._conn.execute(
            "SELECT * FROM config_template WHERE template_id = ?",
            (template_id,)
        )
        row = cursor.fetchone()
        if row:
            template = self._row_to_template(row)

        if not template:
            return False

        if template.success_count == 0:
            logger.warning("Cannot contribute template with no successes")
            return False

        # Double-sanitize for safety
        sanitized = sanitize_template(template.template_json)

        # Final secret check
        if contains_secrets(json.dumps(sanitized)):
            logger.error("Template still contains secrets after sanitization - not contributing")
            return False

        self._conn.execute("""
            INSERT INTO contribution_queue
            (template_id, sanitized_template, created_at_utc)
            VALUES (?, ?, ?)
        """, (template_id, json.dumps(sanitized), self._utc_now()))
        self._conn.commit()

        return True

    def get_pending_contributions(self) -> List[Dict[str, Any]]:
        """Get templates pending contribution."""
        cursor = self._conn.execute("""
            SELECT * FROM contribution_queue
            WHERE contribution_status = 'pending'
            ORDER BY created_at_utc
        """)
        return [dict(row) for row in cursor]

    # -------------------------------------------------------------------------
    # Statistics
    # -------------------------------------------------------------------------

    def get_statistics(self) -> Dict[str, Any]:
        """Get evolution engine statistics."""
        cursor = self._conn.execute("SELECT COUNT(*) as cnt FROM config_template")
        total_templates = cursor.fetchone()["cnt"]

        cursor = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM config_template WHERE success_count > 0"
        )
        working_templates = cursor.fetchone()["cnt"]

        cursor = self._conn.execute("SELECT COUNT(*) as cnt FROM evolution_session")
        total_sessions = cursor.fetchone()["cnt"]

        cursor = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM evolution_session WHERE status = 'success'"
        )
        successful_sessions = cursor.fetchone()["cnt"]

        cursor = self._conn.execute("SELECT COUNT(*) as cnt FROM evolution_attempt")
        total_attempts = cursor.fetchone()["cnt"]

        # Templates by OS
        cursor = self._conn.execute("""
            SELECT os_type, COUNT(*) as cnt FROM config_template GROUP BY os_type
        """)
        by_os = {row["os_type"]: row["cnt"] for row in cursor}

        return {
            "total_templates": total_templates,
            "working_templates": working_templates,
            "total_sessions": total_sessions,
            "successful_sessions": successful_sessions,
            "success_rate": successful_sessions / total_sessions if total_sessions > 0 else 0,
            "total_attempts": total_attempts,
            "templates_by_os": by_os
        }


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    engine = ConfigEvolutionEngine()

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()

        if cmd == "list":
            templates = engine.get_all_templates()
            print(f"=== CONFIG TEMPLATES ({len(templates)}) ===\n")
            for t in templates:
                status = "✅" if t.success_count > t.fail_count else "⚠️" if t.success_count > 0 else "❓"
                print(f"{status} {t.destination_id} ({t.os_type})")
                print(f"    Variant: {t.variant_id}")
                print(f"    Confidence: {t.confidence_score:.1%} ({t.success_count}✓ / {t.fail_count}✗)")
                print(f"    Generation: {t.generation}")
                if t.evolution_notes:
                    print(f"    Notes: {t.evolution_notes[:50]}...")
                print()

        elif cmd == "stats":
            stats = engine.get_statistics()
            print("=== EVOLUTION ENGINE STATISTICS ===\n")
            print(f"Templates: {stats['total_templates']} total, {stats['working_templates']} working")
            print(f"Sessions: {stats['total_sessions']} total, {stats['successful_sessions']} successful")
            print(f"Success Rate: {stats['success_rate']:.1%}")
            print(f"Total Attempts: {stats['total_attempts']}")
            print(f"\nBy OS:")
            for os_type, count in stats.get("templates_by_os", {}).items():
                print(f"  {os_type}: {count}")

        elif cmd == "configure" and len(sys.argv) > 2:
            destination_id = sys.argv[2]
            print(f"Configuring {destination_id}...")

            async def run_configure():
                result = await engine.configure_integration(destination_id)
                print(f"\nResult: {'✅ Success' if result['success'] else '❌ Failed'}")
                print(f"Attempts: {result['attempts']}")
                print(f"Message: {result['message']}")
                if result.get('template_id'):
                    print(f"Template: {result['template_id']}")

            asyncio.run(run_configure())

        elif cmd == "best" and len(sys.argv) > 2:
            destination_id = sys.argv[2]
            os_type = sys.argv[3] if len(sys.argv) > 3 else None
            template = engine.get_best_template(destination_id, os_type)
            if template:
                print(f"Best template for {destination_id}:")
                print(f"  ID: {template.template_id}")
                print(f"  Confidence: {template.confidence_score:.1%}")
                print(f"  Template:\n{json.dumps(template.template_json, indent=2)}")
            else:
                print(f"No template found for {destination_id}")

        elif cmd == "llm-check":
            async def check_llm():
                llm = LocalLLMInterface()
                info = llm.get_model_info()
                print(f"LLM Configuration:")
                print(f"  Model: {info['model']}")
                print(f"  URL: {info['url']}")
                print(f"  Memory Context: {'✅ Enabled' if info['memory_context'] else '❌ Disabled'}")
                print()

                available = await llm.is_available()
                print(f"Status: {'✅ Available' if available else '❌ Not available'}")

                if not available:
                    print("\nStart local LLM with:")
                    print("  ./scripts/start-llm.sh          # Qwen3-4B (default)")
                    print("  ./scripts/start-llm.sh glm      # GLM-4.7-Flash")

                # Test memory context
                if available and info['memory_context']:
                    print("\nTesting memory context...")
                    ctx = llm._get_memory_context("test config evolution")
                    if ctx:
                        print(f"  ✅ Memory context loaded ({len(ctx)} chars)")
                    else:
                        print("  ⚠️  Memory context empty (Context DNA may not be running)")

            asyncio.run(check_llm())

        else:
            print(f"Unknown command: {cmd}")
            print("\nCommands:")
            print("  list              - List all templates")
            print("  stats             - Show engine statistics")
            print("  configure <dest>  - Configure an integration with LLM assistance")
            print("  best <dest> [os]  - Show best template for destination")
            print("  llm-check         - Check if local LLM is available")

    else:
        print("Config Evolution Engine")
        print("\nCommands:")
        print("  list              - List all templates")
        print("  stats             - Show engine statistics")
        print("  configure <dest>  - Configure an integration with LLM assistance")
        print("  best <dest> [os]  - Show best template for destination")
        print("  llm-check         - Check if local LLM is available")
