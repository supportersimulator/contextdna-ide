"""
Self-Reference Suppression Filter (Movement 6)

When product_mode=true, strips references to ContextDNA internals from injection output.
During dogfood (product_mode=false), passes everything through unchanged.

Terms list sourced from .projectdna/manifest.yaml → memory.self_reference_terms
"""
import re
import yaml
from pathlib import Path
from functools import lru_cache

REPO_ROOT = Path(__file__).parent.parent

# Fallback terms if manifest unreadable
_DEFAULT_TERMS = [
    "contextdna", "context_dna", "projectdna", "synaptic", "atlas",
    "webhook injection", "gold mining", "evidence grading",
    "observability_store", "session_historian", "payload_manifest",
]


@lru_cache(maxsize=1)
def _load_config() -> dict:
    """Load filter config from manifest.yaml. Cached — restart to refresh."""
    manifest_path = REPO_ROOT / ".projectdna" / "manifest.yaml"
    try:
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        memory_cfg = manifest.get("memory", {})
        return {
            "enabled": memory_cfg.get("self_reference_filter", True),
            "terms": memory_cfg.get("self_reference_terms", _DEFAULT_TERMS),
            "product_mode": manifest.get("engine", {}).get("product_mode", False),
            "developer_mode": manifest.get("engine", {}).get("developer_mode", True),
        }
    except Exception:
        return {
            "enabled": True,
            "terms": _DEFAULT_TERMS,
            "product_mode": False,
            "developer_mode": True,
        }


def _build_pattern(terms: list[str]) -> re.Pattern:
    """Build compiled regex pattern from terms list. Case-insensitive."""
    # Sort by length descending so longer terms match first
    sorted_terms = sorted(terms, key=len, reverse=True)
    escaped = [re.escape(t) for t in sorted_terms]
    return re.compile(r'\b(' + '|'.join(escaped) + r')\b', re.IGNORECASE)


def is_active() -> bool:
    """Check if filter is currently active (product_mode=true)."""
    cfg = _load_config()
    return cfg["enabled"] and cfg["product_mode"]


def filter_content(content: str, force: bool = False) -> str:
    """
    Filter self-referential terms from injection content.

    Args:
        content: Raw injection content
        force: If True, filter regardless of product_mode (for testing)

    Returns:
        Filtered content (or unchanged if product_mode=false and force=false)
    """
    if not content:
        return content

    cfg = _load_config()

    # During dogfood: pass through unless forced
    if not force and (not cfg["product_mode"] or not cfg["enabled"]):
        return content

    pattern = _build_pattern(cfg["terms"])

    # Replace self-referential terms with generic equivalents
    def _replace(match):
        term = match.group(0).lower()
        # Map to generic product-facing terms
        replacements = {
            "contextdna": "the system",
            "context_dna": "the system",
            "projectdna": "workspace config",
            "synaptic": "assistant",
            "atlas": "assistant",
            "webhook injection": "context loading",
            "gold mining": "knowledge extraction",
            "evidence grading": "quality assessment",
            "observability_store": "metrics store",
            "session_historian": "session manager",
            "payload_manifest": "delivery manifest",
        }
        return replacements.get(term, "the system")

    return pattern.sub(_replace, content)


def filter_lines(lines: list[str], force: bool = False) -> list[str]:
    """Filter self-referential terms from a list of lines."""
    if not is_active() and not force:
        return lines
    return [filter_content(line, force=force) for line in lines]


def get_terms() -> list[str]:
    """Return current self-reference terms list."""
    return _load_config()["terms"]


def get_status() -> dict:
    """Return filter status for diagnostics."""
    cfg = _load_config()
    return {
        "enabled": cfg["enabled"],
        "product_mode": cfg["product_mode"],
        "developer_mode": cfg["developer_mode"],
        "active": cfg["enabled"] and cfg["product_mode"],
        "terms_count": len(cfg["terms"]),
    }
