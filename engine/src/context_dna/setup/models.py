"""
Context DNA Adaptive Hierarchy - Core Data Models

These dataclasses define the structure for hierarchy profiles, suggestions,
and configuration that powers Synaptic's adaptive intelligence.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import platform
import uuid


class RepoType(Enum):
    """Types of repository structures the analyzer can detect."""
    STANDARD = "standard"
    SUBMODULE_MONOREPO = "submodule-monorepo"
    NX_MONOREPO = "nx-monorepo"
    TURBO_MONOREPO = "turbo-monorepo"
    LERNA_MONOREPO = "lerna-monorepo"
    POLYREPO = "polyrepo"
    UNKNOWN = "unknown"


class LLMBackend(Enum):
    """Available LLM backends by platform."""
    MLX = "mlx"           # macOS Apple Silicon - BEST performance
    OLLAMA = "ollama"     # Cross-platform - Good fallback
    ONNX = "onnx"         # Windows native - Fast on Intel/AMD
    CLOUD = "cloud"       # Universal fallback


class SuggestionType(Enum):
    """Types of structure suggestions."""
    INCONSISTENT_NAMING = "inconsistent_naming"
    SCATTERED_CONFIGS = "scattered_configs"
    ORPHANED_FILES = "orphaned_files"
    SUBOPTIMAL_DEPTH = "suboptimal_depth"
    MISSING_CONVENTIONS = "missing_conventions"
    DUPLICATE_PATTERNS = "duplicate_patterns"


class SuggestionResponse(Enum):
    """User responses to suggestions."""
    ACCEPT = "accept"
    DISMISS = "dismiss"
    REMIND_LATER = "remind_later"
    LIKE_CURRENT = "like_current"
    TELL_MORE = "tell_more"


@dataclass
class PlatformInfo:
    """
    Platform detection results.

    SINGLE SOURCE OF TRUTH: Delegates to local_llm/hardware.py for actual detection.
    This class is a simplified view for hierarchy profiles.
    """
    os: str  # 'Darwin', 'Windows', 'Linux'
    arch: str  # 'arm64', 'x86_64', 'amd64'
    is_mac: bool = False
    is_windows: bool = False
    is_linux: bool = False
    is_apple_silicon: bool = False
    has_nvidia_gpu: bool = False
    ram_gb: int = 0
    recommended_backend: LLMBackend = LLMBackend.CLOUD
    chip_name: str = ""       # From hardware.py HardwareProfile
    is_rosetta: bool = False  # Running under Rosetta 2 emulation
    model_recommendation: str = ""  # Recommended model ID

    @classmethod
    def detect(cls) -> "PlatformInfo":
        """
        Detect current platform using hardware.py as single source of truth.

        Falls back to basic detection if hardware.py unavailable.
        """
        try:
            # Use hardware.py as the authoritative source
            from context_dna.local_llm.hardware import get_hardware_profile, recommend_backend, recommend_model_id

            hw = get_hardware_profile()
            backend = recommend_backend(hw)
            model_id = recommend_model_id(hw.ram_gb, backend)

            # Map hardware.py backend type to our LLMBackend enum
            backend_map = {
                "mlx": LLMBackend.MLX,
                "ollama": LLMBackend.OLLAMA,
                "llamacpp": LLMBackend.OLLAMA,  # Treat as Ollama family
                "none": LLMBackend.CLOUD,
            }

            return cls(
                os=hw.os.title(),  # Normalize to 'Darwin', 'Windows', 'Linux'
                arch=hw.arch,
                is_mac=hw.os.lower() == "darwin",
                is_windows=hw.os.lower() == "windows",
                is_linux=hw.os.lower() == "linux",
                is_apple_silicon=hw.is_apple_silicon,
                has_nvidia_gpu=bool(hw.gpu_info and "nvidia" in hw.gpu_info.lower()),
                ram_gb=hw.ram_gb,
                recommended_backend=backend_map.get(backend, LLMBackend.CLOUD),
                chip_name=hw.chip_name or "",
                is_rosetta=hw.is_rosetta,
                model_recommendation=model_id,
            )

        except ImportError:
            # Fallback: basic detection if hardware.py not available
            return cls._detect_fallback()

    @classmethod
    def _detect_fallback(cls) -> "PlatformInfo":
        """Fallback detection when hardware.py unavailable."""
        system = platform.system()
        machine = platform.machine()

        # Try to get RAM
        ram_gb = 16  # Conservative default
        try:
            import psutil
            ram_gb = int(psutil.virtual_memory().total / (1024**3))
        except ImportError as e:
            print(f"[WARN] psutil not available for RAM detection, using default {ram_gb}GB: {e}")

        is_apple_silicon = system == "Darwin" and machine in ("arm64", "aarch64")

        # Determine backend
        if is_apple_silicon:
            backend = LLMBackend.MLX
        elif system == "Windows":
            backend = LLMBackend.ONNX
        else:
            backend = LLMBackend.OLLAMA

        return cls(
            os=system,
            arch=machine,
            is_mac=system == "Darwin",
            is_windows=system == "Windows",
            is_linux=system == "Linux",
            is_apple_silicon=is_apple_silicon,
            has_nvidia_gpu=cls._detect_nvidia_fallback(),
            ram_gb=ram_gb,
            recommended_backend=backend,
        )

    @staticmethod
    def _detect_nvidia_fallback() -> bool:
        """Check for NVIDIA GPU (fallback method)."""
        import subprocess
        try:
            subprocess.run(["nvidia-smi"], capture_output=True, check=True, timeout=5)
            return True
        except Exception:
            return False


@dataclass
class SubmoduleInfo:
    """Information about a git submodule."""
    path: str
    url: str
    branch: Optional[str] = None
    tracked: bool = False  # Whether Context DNA should track this submodule


@dataclass
class ServiceLocation:
    """Detected service location in the codebase."""
    category: str  # 'backend', 'frontend', 'infra', 'memory', 'scripts'
    path: str
    framework: Optional[str] = None  # 'django', 'nextjs', 'terraform', etc.
    confidence: float = 1.0


@dataclass
class NamingConvention:
    """Detected naming convention."""
    pattern: str  # 'snake_case', 'camelCase', 'kebab-case', 'PascalCase'
    scope: str  # 'files', 'directories', 'functions', 'classes'
    examples: List[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass
class ConfigPattern:
    """Detected configuration pattern."""
    type: str  # 'env', 'yaml', 'json', 'toml', 'python'
    locations: List[str] = field(default_factory=list)
    is_preferred: bool = False


@dataclass
class Pin:
    """A pinned setting that survives re-scans."""
    key: str
    value: Any
    pinned_at: str  # ISO8601 timestamp
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "pinned_at": self.pinned_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Pin":
        return cls(**data)


@dataclass
class Suggestion:
    """A structure improvement suggestion."""
    type: SuggestionType
    current: str
    suggested: str
    reasoning: str
    confidence: float  # 0.0-1.0, lower = more deferential
    dismissable: bool = True
    learnable: bool = True
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "current": self.current,
            "suggested": self.suggested,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "dismissable": self.dismissable,
            "learnable": self.learnable,
            "created_at": self.created_at,
        }


@dataclass
class PlacementSuggestion:
    """Suggested file placement."""
    component: str  # 'local_llm_client', 'celery_tasks', etc.
    action: str  # 'create', 'extend'
    path: str
    reasoning: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component": self.component,
            "action": self.action,
            "path": self.path,
            "reasoning": self.reasoning,
        }


@dataclass
class QuestionAnswer:
    """Cached answer to an install question."""
    question_id: str
    answer: Any
    answered_at: str  # ISO8601 timestamp
    context_hash: str  # Hash of structure when answered

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question_id": self.question_id,
            "answer": self.answer,
            "answered_at": self.answered_at,
            "context_hash": self.context_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QuestionAnswer":
        return cls(**data)


@dataclass
class HierarchyProfile:
    """
    Complete hierarchy profile for a codebase.

    This is the central data structure that captures everything Context DNA
    learns about a user's codebase structure.
    """
    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    machine_id: str = ""
    version: int = 1
    schema_version: str = "1.0"

    # Timestamps
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Source tracking
    source: str = "auto_scan"  # 'auto_scan', 'user_input', 'merge', 'rollback'
    parent_version_id: Optional[str] = None
    notes: Optional[str] = None

    # Repository info
    root_path: str = ""
    repo_type: RepoType = RepoType.STANDARD
    submodules: List[SubmoduleInfo] = field(default_factory=list)

    # Detected structure
    locations: Dict[str, ServiceLocation] = field(default_factory=dict)
    naming_conventions: List[NamingConvention] = field(default_factory=list)
    config_patterns: List[ConfigPattern] = field(default_factory=list)

    # Platform info
    platform: Optional[PlatformInfo] = None

    # User customizations
    pins: List[Pin] = field(default_factory=list)
    cached_answers: List[QuestionAnswer] = field(default_factory=list)
    dismissed_suggestions: List[str] = field(default_factory=list)  # Suggestion type values

    # Generated suggestions
    placement_suggestions: Dict[str, PlacementSuggestion] = field(default_factory=dict)
    structure_suggestions: List[Suggestion] = field(default_factory=list)

    # Uncertainties (things the analyzer wasn't sure about)
    uncertainties: Dict[str, str] = field(default_factory=dict)

    # Metadata tracking
    is_active: bool = True
    is_milestone: bool = False

    def add_uncertainty(self, detector_name: str, message: str):
        """Add an uncertainty from a detector."""
        self.uncertainties[detector_name] = message

    @property
    def uncertainty_count(self) -> int:
        return len(self.uncertainties)

    def get_location(self, category: str) -> Optional[ServiceLocation]:
        """Get service location by category."""
        return self.locations.get(category)

    def set_location(self, category: str, location: ServiceLocation):
        """Set service location."""
        self.locations[category] = location
        self.updated_at = datetime.utcnow().isoformat()

    def add_pin(self, key: str, value: Any, reason: Optional[str] = None):
        """Add a pinned setting."""
        pin = Pin(
            key=key,
            value=value,
            pinned_at=datetime.utcnow().isoformat(),
            reason=reason,
        )
        # Remove existing pin for same key
        self.pins = [p for p in self.pins if p.key != key]
        self.pins.append(pin)
        self.updated_at = datetime.utcnow().isoformat()

    def get_pin(self, key: str) -> Optional[Pin]:
        """Get a pinned value."""
        for pin in self.pins:
            if pin.key == key:
                return pin
        return None

    def is_pinned(self, key: str) -> bool:
        """Check if a key is pinned."""
        return any(p.key == key for p in self.pins)

    def cache_answer(self, question_id: str, answer: Any, context_hash: str):
        """Cache an answer to a question."""
        qa = QuestionAnswer(
            question_id=question_id,
            answer=answer,
            answered_at=datetime.utcnow().isoformat(),
            context_hash=context_hash,
        )
        # Remove existing answer for same question
        self.cached_answers = [a for a in self.cached_answers if a.question_id != question_id]
        self.cached_answers.append(qa)
        self.updated_at = datetime.utcnow().isoformat()

    def get_cached_answer(self, question_id: str) -> Optional[QuestionAnswer]:
        """Get cached answer for a question."""
        for qa in self.cached_answers:
            if qa.question_id == question_id:
                return qa
        return None

    def dismiss_suggestion(self, suggestion_type: SuggestionType):
        """Mark a suggestion type as dismissed."""
        if suggestion_type.value not in self.dismissed_suggestions:
            self.dismissed_suggestions.append(suggestion_type.value)
            self.updated_at = datetime.utcnow().isoformat()

    def is_suggestion_dismissed(self, suggestion_type: SuggestionType) -> bool:
        """Check if a suggestion type is dismissed."""
        return suggestion_type.value in self.dismissed_suggestions

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "machine_id": self.machine_id,
            "version": self.version,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "parent_version_id": self.parent_version_id,
            "notes": self.notes,
            "root_path": self.root_path,
            "repo_type": self.repo_type.value,
            "submodules": [
                {"path": s.path, "url": s.url, "branch": s.branch, "tracked": s.tracked}
                for s in self.submodules
            ],
            "locations": {
                k: {"category": v.category, "path": v.path, "framework": v.framework, "confidence": v.confidence}
                for k, v in self.locations.items()
            },
            "naming_conventions": [
                {"pattern": n.pattern, "scope": n.scope, "examples": n.examples, "confidence": n.confidence}
                for n in self.naming_conventions
            ],
            "config_patterns": [
                {"type": c.type, "locations": c.locations, "is_preferred": c.is_preferred}
                for c in self.config_patterns
            ],
            "platform": {
                "os": self.platform.os,
                "arch": self.platform.arch,
                "is_apple_silicon": self.platform.is_apple_silicon,
                "recommended_backend": self.platform.recommended_backend.value,
                "ram_gb": self.platform.ram_gb,
            } if self.platform else None,
            "pins": [p.to_dict() for p in self.pins],
            "cached_answers": [a.to_dict() for a in self.cached_answers],
            "dismissed_suggestions": self.dismissed_suggestions,
            "placement_suggestions": {k: v.to_dict() for k, v in self.placement_suggestions.items()},
            "structure_suggestions": [s.to_dict() for s in self.structure_suggestions],
            "uncertainties": self.uncertainties,
            "is_active": self.is_active,
            "is_milestone": self.is_milestone,
        }

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    def save(self, path: Path):
        """Save profile to file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HierarchyProfile":
        """Create from dictionary."""
        profile = cls(
            id=data.get("id", str(uuid.uuid4())),
            machine_id=data.get("machine_id", ""),
            version=data.get("version", 1),
            schema_version=data.get("schema_version", "1.0"),
            created_at=data.get("created_at", datetime.utcnow().isoformat()),
            updated_at=data.get("updated_at", datetime.utcnow().isoformat()),
            source=data.get("source", "auto_scan"),
            parent_version_id=data.get("parent_version_id"),
            notes=data.get("notes"),
            root_path=data.get("root_path", ""),
            repo_type=RepoType(data.get("repo_type", "standard")),
            is_active=data.get("is_active", True),
            is_milestone=data.get("is_milestone", False),
        )

        # Load submodules
        for s in data.get("submodules", []):
            profile.submodules.append(SubmoduleInfo(**s))

        # Load locations
        for k, v in data.get("locations", {}).items():
            profile.locations[k] = ServiceLocation(**v)

        # Load naming conventions
        for n in data.get("naming_conventions", []):
            profile.naming_conventions.append(NamingConvention(**n))

        # Load config patterns
        for c in data.get("config_patterns", []):
            profile.config_patterns.append(ConfigPattern(**c))

        # Load platform
        if data.get("platform"):
            p = data["platform"]
            profile.platform = PlatformInfo(
                os=p["os"],
                arch=p["arch"],
                is_apple_silicon=p.get("is_apple_silicon", False),
                recommended_backend=LLMBackend(p.get("recommended_backend", "cloud")),
                ram_gb=p.get("ram_gb", 0),
            )

        # Load pins
        for p in data.get("pins", []):
            profile.pins.append(Pin.from_dict(p))

        # Load cached answers
        for a in data.get("cached_answers", []):
            profile.cached_answers.append(QuestionAnswer.from_dict(a))

        # Load dismissed suggestions
        profile.dismissed_suggestions = data.get("dismissed_suggestions", [])

        # Load placement suggestions
        for k, v in data.get("placement_suggestions", {}).items():
            profile.placement_suggestions[k] = PlacementSuggestion(**v)

        # Load uncertainties
        profile.uncertainties = data.get("uncertainties", {})

        return profile

    @classmethod
    def load(cls, path: Path) -> "HierarchyProfile":
        """Load profile from file."""
        data = json.loads(path.read_text())
        return cls.from_dict(data)

    def copy(self) -> "HierarchyProfile":
        """Create a deep copy of this profile."""
        return HierarchyProfile.from_dict(self.to_dict())
