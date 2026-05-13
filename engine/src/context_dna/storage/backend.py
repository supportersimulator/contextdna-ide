"""Abstract storage backend interface for Context DNA."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from enum import Enum


class LearningType(Enum):
    """Types of learnings that can be recorded."""
    WIN = "win"           # Something that worked
    FIX = "fix"           # Bug fix / gotcha
    PATTERN = "pattern"   # Reusable pattern
    INSIGHT = "insight"   # High-level insight
    SOP = "sop"           # Standard operating procedure
    GOTCHA = "gotcha"     # Warning / edge case
    PROTOCOL = "protocol" # Development workflow / process
    ARCHITECTURE = "architecture"  # System design
    BUG_FIX = "bug_fix"   # Bug diagnosis and resolution
    PERFORMANCE = "performance"    # Performance optimization


@dataclass
class Learning:
    """A single learning/memory entry."""
    id: str
    type: LearningType
    title: str
    content: str
    tags: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "id": self.id,
            "type": self.type.value,
            "title": self.title,
            "content": self.content,
            "tags": self.tags,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Learning":
        """Create from dictionary."""
        try:
            learning_type = LearningType(data["type"])
        except ValueError:
            learning_type = LearningType.INSIGHT
        return cls(
            id=data["id"],
            type=learning_type,
            title=data["title"],
            content=data["content"],
            tags=data.get("tags", []),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            metadata=data.get("metadata", {}),
        )


class StorageBackend(ABC):
    """Abstract storage backend for Context DNA.

    Implementations must provide:
    - record(): Store a new learning
    - query(): Search learnings by text
    - get_recent(): Get recent learnings
    - get_by_id(): Get a specific learning
    - get_stats(): Get storage statistics
    - health_check(): Verify backend is working
    """

    @abstractmethod
    def record(self, learning: Learning) -> str:
        """Record a learning and return its ID."""
        pass

    @abstractmethod
    def query(self, search: str, limit: int = 10,
              learning_type: Optional[LearningType] = None) -> List[Learning]:
        """Search learnings by text. Returns matching learnings."""
        pass

    @abstractmethod
    def get_recent(self, hours: int = 24, limit: int = 20) -> List[Learning]:
        """Get learnings from the last N hours."""
        pass

    @abstractmethod
    def get_by_id(self, learning_id: str) -> Optional[Learning]:
        """Get a specific learning by ID."""
        pass

    @abstractmethod
    def get_by_type(self, learning_type: LearningType, limit: int = 50) -> List[Learning]:
        """Get learnings by type."""
        pass

    @abstractmethod
    def get_stats(self) -> dict:
        """Get storage statistics (counts, etc)."""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """Check if the backend is healthy and accessible."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close any connections."""
        pass
