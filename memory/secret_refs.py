#!/usr/bin/env python3
"""
Secret Reference System — agents never see actual secrets.

Secrets are stored in .env and accessed via references like ${Context_DNA_OPENAI}.
Agents receive placeholder markers. Only the Tool Registry resolves them
at execution time, and only for approved operations.

Architecture:
    Agent sees:     "Use ${Context_DNA_OPENAI} for the API call"
    Tool Registry:  secret_registry.resolve("Context_DNA_OPENAI") -> "sk-proj-..."
    Agent output:   "API call succeeded" (value never exposed)

    If a secret value leaks into output:
    Redactor:       "sk-proj-abc123..." -> "${Context_DNA_OPENAI}"

Auto-discovery:
    On init, scans all .env files in the project for variables matching
    secret-like patterns (*_KEY, *_SECRET, *_TOKEN, *_PASSWORD, *_CREDENTIAL).
    No manual registration needed for standard naming.
"""

import logging
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger("context_dna.secret_refs")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Patterns that indicate a variable is a secret
SECRET_PATTERNS = [
    r".*_KEY$",
    r".*_SECRET$",
    r".*_TOKEN$",
    r".*_PASSWORD$",
    r".*_CREDENTIAL$",
    r".*_CREDENTIALS$",
    r".*_API_KEY$",
    r".*_AUTH$",
    r".*_PRIVATE$",
    r".*_SIGNING$",
]
_SECRET_RE = re.compile("|".join(SECRET_PATTERNS), re.IGNORECASE)

# Categories for classification
CATEGORY_API_KEY = "api_key"
CATEGORY_CREDENTIAL = "credential"
CATEGORY_TOKEN = "token"
CATEGORY_CONNECTION_STRING = "connection_string"
CATEGORY_OTHER = "other"


def _classify_category(name: str) -> str:
    """Classify a secret variable name into a category."""
    upper = name.upper()
    if "API_KEY" in upper or upper.endswith("_KEY"):
        return CATEGORY_API_KEY
    if "TOKEN" in upper:
        return CATEGORY_TOKEN
    if "PASSWORD" in upper or "CREDENTIAL" in upper or "SECRET" in upper or "AUTH" in upper:
        return CATEGORY_CREDENTIAL
    if "URL" in upper and ("DATABASE" in upper or "REDIS" in upper or "MONGO" in upper):
        return CATEGORY_CONNECTION_STRING
    return CATEGORY_OTHER


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SecretRef:
    """Reference to a secret — agents see this, never the value."""

    name: str
    placeholder: str  # e.g. "${Context_DNA_OPENAI}"
    category: str     # api_key, credential, token, connection_string, other
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# .env parser
# ---------------------------------------------------------------------------

def _parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a .env file into a dict. Skips comments, handles quotes."""
    result = {}
    if not path.is_file():
        return result

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip surrounding quotes
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if key:
                    result[key] = value
    except Exception as e:
        logger.warning("Failed to parse env file %s: %s", path, e)

    return result


def _discover_env_files() -> List[Path]:
    """Find all .env files in the project root (non-recursive for safety)."""
    candidates = [
        PROJECT_ROOT / ".env",
        PROJECT_ROOT / "context-dna" / ".env",
        PROJECT_ROOT / "acontext" / ".env",
        PROJECT_ROOT / "context-dna" / "infra" / ".env",
    ]
    return [p for p in candidates if p.is_file()]


# ---------------------------------------------------------------------------
# Secret Registry
# ---------------------------------------------------------------------------

class SecretRegistry:
    """Registry of secret references.

    Agents interact with placeholders like ${Context_DNA_OPENAI}.
    Only the Tool Registry calls resolve() at execution time.

    Usage:
        registry = SecretRegistry()
        registry.auto_discover()

        # For agent prompts:
        refs = registry.list_refs()

        # For redaction (called by Harmonizer before output):
        safe_text = registry.redact(potentially_leaky_text)

        # ONLY called by Tool Registry:
        actual_value = registry.resolve("Context_DNA_OPENAI")
    """

    def __init__(self):
        self._refs: Dict[str, SecretRef] = {}
        self._values: Dict[str, str] = {}  # name -> actual value (from env)
        self._reverse_lookup: Dict[str, str] = {}  # value -> name (for redaction)
        self._discovered = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        env_var: str,
        category: str = "",
        description: str = "",
    ) -> None:
        """Register a secret reference.

        Args:
            name: Logical name for the secret (usually same as env_var)
            env_var: Environment variable name to read from
            category: api_key, credential, token, connection_string, other
            description: Human-readable description
        """
        if not category:
            category = _classify_category(name)
        if not description:
            description = f"{category}: {name}"

        placeholder = f"${{{name}}}"
        self._refs[name] = SecretRef(
            name=name,
            placeholder=placeholder,
            category=category,
            description=description,
        )

        # Load actual value from environment
        value = os.environ.get(env_var, "")
        if value:
            self._values[name] = value
            # Only index values long enough to be meaningful (avoid matching "a" or "1")
            if len(value) >= 6:
                self._reverse_lookup[value] = name

        logger.debug("Registered secret ref: %s (category=%s, has_value=%s)", name, category, bool(value))

    # ------------------------------------------------------------------
    # Resolution (ONLY for Tool Registry)
    # ------------------------------------------------------------------

    def resolve(self, ref_name: str) -> Optional[str]:
        """Resolve a secret reference to its actual value.

        WARNING: This returns the REAL secret. Only the Tool Registry
        should call this, never agents directly.
        """
        self._ensure_discovered()
        return self._values.get(ref_name)

    # ------------------------------------------------------------------
    # Agent-safe API
    # ------------------------------------------------------------------

    def get_placeholder(self, ref_name: str) -> str:
        """Get the placeholder string for a secret (safe for agents)."""
        self._ensure_discovered()
        ref = self._refs.get(ref_name)
        return ref.placeholder if ref else f"${{{ref_name}}}"

    def list_refs(self) -> List[SecretRef]:
        """List all registered secret refs (names + descriptions, NO values)."""
        self._ensure_discovered()
        return sorted(self._refs.values(), key=lambda r: r.name)

    def get_agent_summary(self) -> str:
        """Formatted summary for injection into agent prompts."""
        self._ensure_discovered()
        refs = self.list_refs()
        if not refs:
            return "(no secrets registered)"

        lines = ["Available Secret References (use placeholder, never actual value):", ""]
        by_category: Dict[str, List[SecretRef]] = {}
        for ref in refs:
            by_category.setdefault(ref.category, []).append(ref)

        for cat in sorted(by_category.keys()):
            lines.append(f"  [{cat}]")
            for ref in by_category[cat]:
                lines.append(f"    {ref.placeholder} — {ref.description}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Redaction
    # ------------------------------------------------------------------

    def redact(self, text: str) -> str:
        """Replace any leaked secret values in text with their placeholders.

        Called by the Harmonizer before any output is shown to agents or users.
        Uses longest-match-first to avoid partial replacements.
        """
        self._ensure_discovered()
        if not self._reverse_lookup or not text:
            return text

        # Sort by length descending to replace longest matches first
        for value in sorted(self._reverse_lookup.keys(), key=len, reverse=True):
            if value in text:
                name = self._reverse_lookup[value]
                placeholder = f"${{{name}}}"
                text = text.replace(value, placeholder)

        return text

    def scan_for_leaks(self, text: str) -> List[str]:
        """Detect if actual secret values appear in text.

        Returns list of secret names whose values were found.
        Does NOT modify the text.
        """
        self._ensure_discovered()
        leaked = []
        for value, name in self._reverse_lookup.items():
            if value in text:
                leaked.append(name)
        return leaked

    # ------------------------------------------------------------------
    # Auto-discovery
    # ------------------------------------------------------------------

    def auto_discover(self) -> int:
        """Scan .env files and environment for secret-like variables.

        Returns number of secrets discovered.
        """
        count_before = len(self._refs)

        # 1. Load all .env files into os.environ (if not already set)
        for env_path in _discover_env_files():
            env_vars = _parse_env_file(env_path)
            for key, value in env_vars.items():
                # Don't overwrite existing env vars
                if key not in os.environ:
                    os.environ[key] = value

        # 2. Register known secrets explicitly
        known_secrets = [
            ("Context_DNA_OPENAI", "OpenAI API key for GPT models"),
            ("Context_DNA_Deepseek", "DeepSeek API key"),
            ("ANTHROPIC_API_KEY", "Anthropic API key for Claude models"),
            ("GITHUB_TOKEN", "GitHub personal access token"),
            ("DATABASE_URL", "Primary database connection string"),
            ("REDIS_URL", "Redis connection string"),
            ("DATABASE_PASSWORD", "Database password"),
            ("REDIS_PASSWORD", "Redis password"),
        ]
        for name, desc in known_secrets:
            if name not in self._refs:
                self.register(name, name, description=desc)

        # 3. Auto-discover from environment: scan for secret-like names
        for key in sorted(os.environ.keys()):
            if key in self._refs:
                continue  # already registered
            if _SECRET_RE.match(key):
                self.register(key, key)

        discovered = len(self._refs) - count_before
        if discovered > 0:
            logger.info("Auto-discovered %d secret references (total: %d)", discovered, len(self._refs))
        self._discovered = True
        return discovered

    def _ensure_discovered(self):
        """Run auto-discovery once on first access."""
        if not self._discovered:
            self.auto_discover()

    # ------------------------------------------------------------------
    # Env var substitution in strings
    # ------------------------------------------------------------------

    def substitute_refs(self, text: str) -> str:
        """Replace ${VAR_NAME} placeholders with actual values.

        ONLY for use by Tool Registry when preparing execution parameters.
        Never expose the result to agents.
        """
        self._ensure_discovered()

        def _replacer(match):
            name = match.group(1)
            value = self._values.get(name)
            if value is not None:
                return value
            return match.group(0)  # leave unresolved

        return re.sub(r'\$\{(\w+)\}', _replacer, text)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: Optional[SecretRegistry] = None


def get_secret_registry() -> SecretRegistry:
    """Get or create the global SecretRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = SecretRegistry()
    return _registry
