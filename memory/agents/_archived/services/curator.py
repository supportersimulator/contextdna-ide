"""
Curator Agent - Data Curation Pipeline

The Curator manages data quality - cleaning, validating, and organizing
information before it enters the memory system.

Anatomical Label: Data Curation Service
"""

from __future__ import annotations
import json
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class CuratorAgent(Agent):
    """
    Curator Agent - Data quality and curation.

    Responsibilities:
    - Validate incoming data
    - Clean and normalize content
    - Detect duplicates
    - Organize for storage
    """

    NAME = "curator"
    CATEGORY = AgentCategory.SERVICES
    DESCRIPTION = "Data curation and quality management"
    ANATOMICAL_LABEL = "Data Curation Service"
    IS_VITAL = False

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._seen_hashes: set = set()
        self._curation_stats: Dict[str, int] = {
            "processed": 0,
            "validated": 0,
            "rejected": 0,
            "duplicates": 0
        }

    def _on_start(self):
        """Initialize curator."""
        pass

    def _on_stop(self):
        """Shutdown curator."""
        self._seen_hashes.clear()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check curator health."""
        return {
            "healthy": True,
            "score": 1.0,
            "message": f"Curated {self._curation_stats['processed']} items",
            "metrics": self._curation_stats
        }

    def process(self, input_data: Any) -> Any:
        """Process curation operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "curate")
            if op == "curate":
                return self.curate(input_data.get("data"))
            elif op == "validate":
                return self.validate(input_data.get("data"), input_data.get("schema"))
            elif op == "deduplicate":
                return self.is_duplicate(input_data.get("content"))
            elif op == "stats":
                return self._curation_stats
        return self.curate(input_data)

    def curate(self, data: Any) -> Dict[str, Any]:
        """
        Curate data for storage.

        Returns curated data with metadata.
        """
        self._curation_stats["processed"] += 1
        self._last_active = datetime.utcnow()

        if data is None:
            self._curation_stats["rejected"] += 1
            return {"status": "rejected", "reason": "null_data"}

        # Convert to standard format
        if isinstance(data, str):
            curated = {"content": data, "type": "text"}
        elif isinstance(data, dict):
            curated = data.copy()
        else:
            curated = {"content": str(data), "type": "unknown"}

        # Check for duplicate
        content_str = json.dumps(curated, sort_keys=True)
        content_hash = hashlib.sha256(content_str.encode()).hexdigest()[:16]

        if content_hash in self._seen_hashes:
            self._curation_stats["duplicates"] += 1
            return {"status": "duplicate", "hash": content_hash}

        self._seen_hashes.add(content_hash)

        # Clean and normalize
        curated = self._clean(curated)
        curated = self._normalize(curated)

        # Validate
        if not self._is_valid(curated):
            self._curation_stats["rejected"] += 1
            return {"status": "rejected", "reason": "validation_failed"}

        # Add metadata
        curated["_curated_at"] = datetime.utcnow().isoformat()
        curated["_hash"] = content_hash

        self._curation_stats["validated"] += 1

        return {
            "status": "curated",
            "data": curated,
            "hash": content_hash
        }

    def validate(self, data: Any, schema: Dict[str, Any] = None) -> Dict[str, Any]:
        """Validate data against optional schema."""
        if data is None:
            return {"valid": False, "reason": "null_data"}

        if schema:
            # Basic schema validation
            required = schema.get("required", [])
            if isinstance(data, dict):
                missing = [f for f in required if f not in data]
                if missing:
                    return {"valid": False, "reason": f"missing_fields: {missing}"}

        return {"valid": self._is_valid(data)}

    def is_duplicate(self, content: Any) -> bool:
        """Check if content is a duplicate."""
        content_str = json.dumps(content, sort_keys=True) if isinstance(content, dict) else str(content)
        content_hash = hashlib.sha256(content_str.encode()).hexdigest()[:16]
        return content_hash in self._seen_hashes

    def _clean(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Clean data by removing problematic content."""
        # Remove null values
        cleaned = {k: v for k, v in data.items() if v is not None}

        # Trim strings
        for key, value in cleaned.items():
            if isinstance(value, str):
                cleaned[key] = value.strip()

        return cleaned

    def _normalize(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize data format."""
        normalized = data.copy()

        # Ensure content field exists
        if "content" not in normalized and "text" in normalized:
            normalized["content"] = normalized.pop("text")

        # Ensure type field
        if "type" not in normalized:
            normalized["type"] = "general"

        return normalized

    def _is_valid(self, data: Any) -> bool:
        """Check if data is valid for storage."""
        if data is None:
            return False

        if isinstance(data, dict):
            # Must have some content
            content = data.get("content", data.get("text", data.get("value", "")))
            if not content:
                return False

        return True

    def get_stats(self) -> Dict[str, Any]:
        """Get curation statistics."""
        return {
            **self._curation_stats,
            "duplicate_rate": (
                self._curation_stats["duplicates"] / self._curation_stats["processed"]
                if self._curation_stats["processed"] > 0 else 0
            )
        }

    def reset_seen(self):
        """Reset seen hashes (for testing or new session)."""
        self._seen_hashes.clear()
