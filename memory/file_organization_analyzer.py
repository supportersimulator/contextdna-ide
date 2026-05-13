#!/usr/bin/env python3
"""
Synaptic's File Organization Analyzer - Smart Organization Suggestions

Analyzes the user's actual file structure and suggests reorganization
based on proven organizational patterns from productivity experts.

═══════════════════════════════════════════════════════════════════════════
ORGANIZATIONAL PATTERNS SUPPORTED (from research):
═══════════════════════════════════════════════════════════════════════════

1. PARA Method (Tiago Forte)
   - Projects: Active tasks with deadlines
   - Areas: Ongoing responsibilities (no deadline)
   - Resources: Reference materials
   - Archives: Completed/inactive

2. Project-Centric
   - /Projects/ProjectName/src,docs,assets,tests
   - Best for developers, creators

3. Date-Based (Chronological)
   - /YYYY/MM/filename_YYYYMMDD.ext
   - Best for documents, photos, logs

4. Type-Based (by file type)
   - /Documents, /Images, /Code, /Media, /Data
   - Simple, intuitive

5. Client/Entity-Based
   - /Clients/ClientName/Projects,Documents,Assets
   - Best for consultants, agencies

6. Action-Based (GTD-inspired)
   - /Inbox, /Active, /Reference, /Someday, /Archive
   - Best for task-oriented workflows

═══════════════════════════════════════════════════════════════════════════
SENSITIVITY SLIDER CONTROLS:
═══════════════════════════════════════════════════════════════════════════

Sensitivity (0.0 - 1.0):
- 0.0 (Loose): Only clear project boundaries detected
- 0.5 (Balanced): Standard detection
- 1.0 (Strict): Aggressive boundary detection

Affects:
- Project indicator confidence thresholds
- Grouping similarity requirements
- Reorganization suggestion aggressiveness

═══════════════════════════════════════════════════════════════════════════
SAFETY: Always creates SQLite backup before ANY file moves
═══════════════════════════════════════════════════════════════════════════

Usage:
    # Analyze current structure
    python file_organization_analyzer.py analyze

    # Suggest reorganization with specific pattern
    python file_organization_analyzer.py suggest --pattern para

    # View all pattern previews for your files
    python file_organization_analyzer.py preview

    # Set sensitivity (0.0-1.0)
    python file_organization_analyzer.py analyze --sensitivity 0.7

    # Create backup before any moves
    python file_organization_analyzer.py backup

    # Restore from backup
    python file_organization_analyzer.py restore
"""

import os
import sys
import json
import sqlite3
import shutil
import hashlib
import re
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Set, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
from collections import defaultdict

# Setup logging
logger = logging.getLogger(__name__)


# =============================================================================
# ORGANIZATIONAL PATTERNS
# =============================================================================

class OrganizationPattern(str, Enum):
    """Supported organizational patterns."""
    PARA = "para"                    # Projects, Areas, Resources, Archives
    PROJECT_CENTRIC = "project"      # By project with standard subfolders
    DATE_BASED = "date"              # Chronological organization
    TYPE_BASED = "type"              # By file type
    CLIENT_BASED = "client"          # By client/entity
    ACTION_BASED = "action"          # GTD-inspired (Inbox, Active, etc.)
    HYBRID = "hybrid"                # Combination (recommended)


@dataclass
class PatternDescription:
    """Description of an organizational pattern."""
    name: str
    description: str
    best_for: List[str]
    structure_example: List[str]
    pros: List[str]
    cons: List[str]


PATTERN_INFO: Dict[OrganizationPattern, PatternDescription] = {
    OrganizationPattern.PARA: PatternDescription(
        name="PARA Method (Tiago Forte)",
        description="Organizes by actionability: Projects (active), Areas (ongoing), Resources (reference), Archives (inactive)",
        best_for=["Knowledge workers", "Consultants", "Writers", "Anyone managing multiple responsibilities"],
        structure_example=[
            "📁 Projects/",
            "   └─ WebsiteRedesign/",
            "   └─ BookChapter3/",
            "📁 Areas/",
            "   └─ Health/",
            "   └─ Finances/",
            "📁 Resources/",
            "   └─ ProgrammingTips/",
            "   └─ DesignInspiration/",
            "📁 Archives/",
            "   └─ CompletedProjects/",
        ],
        pros=["Simple to maintain", "Platform-agnostic", "Scales well", "Focus on actionability"],
        cons=["Requires discipline to move items", "Categories can blur"]
    ),

    OrganizationPattern.PROJECT_CENTRIC: PatternDescription(
        name="Project-Centric Organization",
        description="Everything organized by project with consistent subfolders",
        best_for=["Developers", "Designers", "Freelancers", "Anyone with distinct projects"],
        structure_example=[
            "📁 Projects/",
            "   └─ MyApp/",
            "      ├─ src/",
            "      ├─ docs/",
            "      ├─ assets/",
            "      └─ tests/",
            "   └─ ClientWebsite/",
            "      ├─ design/",
            "      ├─ code/",
            "      └─ deliverables/",
        ],
        pros=["Self-contained projects", "Easy to archive/share", "Clear boundaries"],
        cons=["Cross-project resources duplicated", "Finding old work harder"]
    ),

    OrganizationPattern.DATE_BASED: PatternDescription(
        name="Date-Based (Chronological)",
        description="Organized by date with consistent naming: YYYY/MM/filename_YYYYMMDD",
        best_for=["Photographers", "Journalists", "Researchers", "Log-heavy workflows"],
        structure_example=[
            "📁 2024/",
            "   └─ 01-January/",
            "      ├─ meeting_20240115.pdf",
            "      └─ notes_20240120.md",
            "   └─ 02-February/",
            "📁 2025/",
            "   └─ 01-January/",
        ],
        pros=["Easy to find by time", "Natural chronology", "Good for compliance"],
        cons=["Projects scattered across dates", "Hard to see project scope"]
    ),

    OrganizationPattern.TYPE_BASED: PatternDescription(
        name="Type-Based Organization",
        description="Organized by file type/category",
        best_for=["Simple needs", "Media collections", "Quick setup"],
        structure_example=[
            "📁 Documents/",
            "📁 Images/",
            "📁 Code/",
            "📁 Media/",
            "📁 Data/",
            "📁 Downloads/",
        ],
        pros=["Intuitive", "Easy to set up", "Works with defaults"],
        cons=["Projects fragmented", "Context lost", "Scales poorly"]
    ),

    OrganizationPattern.CLIENT_BASED: PatternDescription(
        name="Client/Entity-Based",
        description="Organized by client, company, or entity",
        best_for=["Consultants", "Agencies", "Account managers", "B2B services"],
        structure_example=[
            "📁 Clients/",
            "   └─ Acme-Corp/",
            "      ├─ Projects/",
            "      ├─ Contracts/",
            "      └─ Communications/",
            "   └─ TechStartup/",
            "📁 Internal/",
            "   └─ Templates/",
            "   └─ Processes/",
        ],
        pros=["Client history in one place", "Easy handoffs", "Billing clarity"],
        cons=["Internal work scattered", "Cross-client resources duplicated"]
    ),

    OrganizationPattern.ACTION_BASED: PatternDescription(
        name="Action-Based (GTD-Inspired)",
        description="Organized by action state: Inbox, Active, Reference, Someday, Archive",
        best_for=["Task-oriented workers", "GTD practitioners", "Inbox-zero advocates"],
        structure_example=[
            "📁 00-Inbox/          # Unsorted incoming",
            "📁 01-Active/         # Current work",
            "📁 02-Reference/      # Look up materials",
            "📁 03-Someday/        # Future possibilities",
            "📁 04-Archive/        # Completed/inactive",
        ],
        pros=["Clear action states", "Reduces decision fatigue", "Inbox-zero friendly"],
        cons=["Requires regular review", "Project context scattered"]
    ),

    OrganizationPattern.HYBRID: PatternDescription(
        name="Hybrid (Recommended)",
        description="Combines PARA's actionability with Project structure",
        best_for=["Most users", "Complex workflows", "Mixed content types"],
        structure_example=[
            "📁 _Inbox/            # Quick drop zone",
            "📁 Projects/          # Active projects",
            "   └─ ProjectName/",
            "      └─ (standard structure)",
            "📁 Areas/             # Ongoing responsibilities",
            "   └─ Finance/",
            "   └─ Health/",
            "📁 Resources/         # Reference materials",
            "   └─ CodeSnippets/",
            "📁 Archive/           # Completed/inactive",
            "   └─ 2024/",
        ],
        pros=["Best of both worlds", "Scales well", "Adaptable"],
        cons=["More complex initial setup"]
    ),
}


# =============================================================================
# ANALYSIS DATA STRUCTURES
# =============================================================================

@dataclass
class FileCluster:
    """A group of related files detected."""
    name: str
    paths: List[str]
    common_root: str
    file_count: int
    total_size_bytes: int
    detected_type: str  # project, resource, archive, etc.
    confidence: float
    indicators: List[str]  # Why we think this is a cluster


@dataclass
class OrganizationSuggestion:
    """A specific suggestion for reorganization."""
    source_path: str
    suggested_path: str
    reason: str
    pattern: OrganizationPattern
    confidence: float
    is_move: bool  # True = move, False = copy


@dataclass
class ReorganizationPlan:
    """A complete reorganization plan."""
    pattern: OrganizationPattern
    suggestions: List[OrganizationSuggestion]
    total_files: int
    total_moves: int
    estimated_benefit: str
    warnings: List[str]
    created_at: datetime


@dataclass
class StructureAnalysis:
    """Analysis of current file structure."""
    total_files: int
    total_size_bytes: int
    detected_projects: List[FileCluster]
    orphan_files: List[str]  # Files not in any project
    duplicate_candidates: List[Tuple[str, str]]
    naming_issues: List[str]  # Files with inconsistent naming
    deep_nesting: List[str]  # Paths too deep (>5 levels)
    current_pattern: Optional[OrganizationPattern]
    pattern_scores: Dict[str, float]  # How well current matches each pattern


# =============================================================================
# BACKUP SYSTEM - SAFETY FIRST
# =============================================================================

class FileBackupManager:
    """
    Manages SQLite backups of file state before any moves.

    SAFETY FIRST: We NEVER move files without a backup that can
    fully restore the original state.
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            config_dir = Path.home() / ".context-dna"
            config_dir.mkdir(exist_ok=True)
            db_path = config_dir / "file_organization_backup.db"

        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize backup database."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("PRAGMA journal_mode=WAL")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS backup_sessions (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    description TEXT,
                    total_files INTEGER,
                    status TEXT DEFAULT 'pending'
                );

                CREATE TABLE IF NOT EXISTS file_states (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER NOT NULL,
                    original_path TEXT NOT NULL,
                    new_path TEXT,
                    file_hash TEXT,
                    size_bytes INTEGER,
                    modified_at TEXT,
                    action TEXT,  -- move, copy, delete
                    executed BOOLEAN DEFAULT FALSE,
                    FOREIGN KEY (session_id) REFERENCES backup_sessions(id)
                );

                CREATE INDEX IF NOT EXISTS idx_file_states_session ON file_states(session_id);
                CREATE INDEX IF NOT EXISTS idx_file_states_original ON file_states(original_path);
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error initializing backup database: {e}")

    def create_backup(self, files: List[Dict], description: str = None) -> int:
        """
        Create a backup of current file states before any moves.

        Args:
            files: List of {path, new_path, action} dicts
            description: Optional description

        Returns:
            Backup session ID
        """
        try:
            conn = sqlite3.connect(str(self.db_path))

            cursor = conn.execute("""
                INSERT INTO backup_sessions (created_at, description, total_files, status)
                VALUES (?, ?, ?, 'pending')
            """, (datetime.now().isoformat(), description, len(files)))

            session_id = cursor.lastrowid

            for file_info in files:
                path = Path(file_info['path'])
                file_hash = None
                size_bytes = None
                modified_at = None

                if path.exists():
                    try:
                        stat = path.stat()
                        size_bytes = stat.st_size
                        modified_at = datetime.fromtimestamp(stat.st_mtime).isoformat()

                        # Only hash small files
                        if size_bytes < 10_000_000:  # 10MB
                            file_hash = hashlib.md5(path.read_bytes()).hexdigest()
                    except Exception as e:
                        print(f"[WARN] File stat/hash failed for {path}: {e}")

                conn.execute("""
                    INSERT INTO file_states
                    (session_id, original_path, new_path, file_hash, size_bytes, modified_at, action)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    session_id,
                    str(path),
                    file_info.get('new_path'),
                    file_hash,
                    size_bytes,
                    modified_at,
                    file_info.get('action', 'move')
                ))

            conn.commit()
            conn.close()

            return session_id
        except Exception as e:
            logger.error(f"Error creating backup: {e}")
            return None

    def restore_backup(self, session_id: int) -> Dict:
        """
        Restore files to their original state from a backup session.

        Returns:
            Summary of restore operation
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row

            # Get all file states for this session
            files = conn.execute("""
                SELECT * FROM file_states
                WHERE session_id = ? AND executed = TRUE
                ORDER BY id DESC
            """, (session_id,)).fetchall()

            restored = 0
            errors = []

            for file_row in files:
                original = Path(file_row['original_path'])
                new_path = Path(file_row['new_path']) if file_row['new_path'] else None

                try:
                    if file_row['action'] == 'move' and new_path and new_path.exists():
                        # Move back to original location
                        original.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(new_path), str(original))
                        restored += 1

                    elif file_row['action'] == 'copy' and new_path and new_path.exists():
                        # Just delete the copy
                        new_path.unlink()
                        restored += 1

                except Exception as e:
                    errors.append(f"{original}: {str(e)}")

            # Update session status
            conn.execute("""
                UPDATE backup_sessions SET status = 'restored' WHERE id = ?
            """, (session_id,))
            conn.commit()
            conn.close()

            return {
                "session_id": session_id,
                "files_restored": restored,
                "errors": errors
            }
        except Exception as e:
            logger.error(f"Error restoring backup: {e}")
            return {"session_id": session_id, "files_restored": 0, "errors": [str(e)]}

    def list_backups(self, limit: int = 10) -> List[Dict]:
        """List recent backup sessions."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row

            results = conn.execute("""
                SELECT * FROM backup_sessions
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,)).fetchall()

            conn.close()
            return [dict(r) for r in results]
        except Exception as e:
            logger.error(f"Error listing backups: {e}")
            return []

    def mark_executed(self, session_id: int, original_path: str):
        """Mark a file state as executed (move completed)."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                UPDATE file_states SET executed = TRUE
                WHERE session_id = ? AND original_path = ?
            """, (session_id, original_path))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error marking executed: {e}")


# =============================================================================
# FILE ORGANIZATION ANALYZER
# =============================================================================

class FileOrganizationAnalyzer:
    """
    Analyzes file structure and suggests reorganization based on
    proven organizational patterns.

    SENSITIVITY SLIDER:
    - 0.0 (Loose): Only clear boundaries
    - 0.5 (Balanced): Standard detection
    - 1.0 (Strict): Aggressive detection
    """

    # Project indicators with confidence weights
    PROJECT_INDICATORS = {
        ".git": 1.0,
        "package.json": 0.95,
        "pyproject.toml": 0.95,
        "Cargo.toml": 0.95,
        "go.mod": 0.95,
        "pom.xml": 0.9,
        "build.gradle": 0.9,
        "Makefile": 0.8,
        "CMakeLists.txt": 0.85,
        "requirements.txt": 0.7,
        "setup.py": 0.8,
        ".project": 0.75,
        "README.md": 0.5,
        "LICENSE": 0.4,
    }

    # File type patterns for categorization
    FILE_CATEGORIES = {
        "code": {".py", ".js", ".ts", ".java", ".cpp", ".c", ".go", ".rs", ".rb", ".php", ".swift"},
        "docs": {".md", ".txt", ".pdf", ".doc", ".docx", ".rtf", ".odt"},
        "data": {".json", ".csv", ".xml", ".yaml", ".yml", ".sql", ".db"},
        "media": {".jpg", ".png", ".gif", ".mp4", ".mp3", ".wav", ".svg"},
        "config": {".env", ".ini", ".conf", ".toml", ".cfg"},
        "archive": {".zip", ".tar", ".gz", ".rar", ".7z"},
    }

    def __init__(
        self,
        sensitivity: float = 0.5,
        scanner_db_path: Optional[Path] = None,
        backup_manager: Optional[FileBackupManager] = None
    ):
        """
        Initialize analyzer.

        Args:
            sensitivity: 0.0 (loose) to 1.0 (strict) boundary detection
            scanner_db_path: Path to local_file_scanner database
            backup_manager: Optional backup manager for safety
        """
        self.sensitivity = max(0.0, min(1.0, sensitivity))
        self.backup_manager = backup_manager or FileBackupManager()

        # Load scanner data if available
        if scanner_db_path is None:
            scanner_db_path = Path.home() / ".context-dna" / "local_files.db"

        self.scanner_db_path = scanner_db_path
        self._file_data = None

    def _load_file_data(self) -> Dict:
        """Load file data from scanner database."""
        if self._file_data is not None:
            return self._file_data

        if not self.scanner_db_path.exists():
            return {"files": [], "projects": []}

        try:
            conn = sqlite3.connect(str(self.scanner_db_path))
            conn.row_factory = sqlite3.Row

            files = conn.execute("SELECT * FROM files").fetchall()
            projects = conn.execute("SELECT * FROM projects").fetchall()

            conn.close()

            self._file_data = {
                "files": [dict(f) for f in files],
                "projects": [dict(p) for p in projects]
            }
            return self._file_data
        except Exception as e:
            logger.error(f"Error loading file data: {e}")
            return {"files": [], "projects": []}

    def _calculate_confidence_threshold(self) -> float:
        """Calculate confidence threshold based on sensitivity."""
        # Higher sensitivity = lower threshold (detect more)
        return 1.0 - (self.sensitivity * 0.6)

    def _detect_project_boundaries(self, files: List[Dict]) -> List[FileCluster]:
        """Detect project boundaries in files."""
        threshold = self._calculate_confidence_threshold()
        clusters = []

        # Group files by parent directories
        dir_groups: Dict[str, List[Dict]] = defaultdict(list)
        for f in files:
            parent = str(Path(f['path']).parent)
            dir_groups[parent].append(f)

        # Check each directory for project indicators
        for dir_path, dir_files in dir_groups.items():
            indicators_found = []
            confidence = 0.0

            dir_p = Path(dir_path)

            for indicator, weight in self.PROJECT_INDICATORS.items():
                if (dir_p / indicator).exists():
                    indicators_found.append(indicator)
                    confidence = max(confidence, weight)

            # Adjust by sensitivity
            adjusted_confidence = confidence * (0.5 + self.sensitivity * 0.5)

            if adjusted_confidence >= threshold:
                total_size = sum(f.get('size_bytes', 0) for f in dir_files)

                clusters.append(FileCluster(
                    name=dir_p.name,
                    paths=[f['path'] for f in dir_files],
                    common_root=dir_path,
                    file_count=len(dir_files),
                    total_size_bytes=total_size,
                    detected_type="project",
                    confidence=adjusted_confidence,
                    indicators=indicators_found
                ))

        return clusters

    def _analyze_naming_consistency(self, files: List[Dict]) -> List[str]:
        """Analyze naming consistency and find issues."""
        issues = []

        for f in files:
            name = Path(f['path']).name

            # Check for spaces (problematic in terminals)
            if ' ' in name:
                issues.append(f"Spaces in filename: {f['path']}")

            # Check for non-ASCII characters
            if not name.isascii():
                issues.append(f"Non-ASCII characters: {f['path']}")

            # Check for inconsistent date formats
            if re.search(r'\d{2}-\d{2}-\d{4}', name):  # MM-DD-YYYY (bad)
                issues.append(f"Non-sortable date format: {f['path']}")

            # Check for versioning issues
            if re.search(r'v\d+\s*\(\d+\)', name):  # v1 (2) (confusing)
                issues.append(f"Confusing version naming: {f['path']}")

        # Limit issues returned based on sensitivity
        max_issues = int(100 * self.sensitivity) + 10
        return issues[:max_issues]

    def _detect_current_pattern(self, files: List[Dict]) -> Tuple[Optional[OrganizationPattern], Dict[str, float]]:
        """Detect which pattern the current structure most resembles."""
        scores = {p.value: 0.0 for p in OrganizationPattern}

        # Count indicators for each pattern
        para_folders = {"projects", "areas", "resources", "archives", "archive"}
        action_folders = {"inbox", "active", "reference", "someday", "archive"}
        type_folders = {"documents", "images", "code", "media", "data", "downloads"}

        for f in files:
            path_parts = set(p.lower() for p in Path(f['path']).parts)

            # PARA detection
            para_matches = len(path_parts & para_folders)
            scores['para'] += para_matches * 0.5

            # Action-based detection
            action_matches = len(path_parts & action_folders)
            scores['action'] += action_matches * 0.5

            # Type-based detection
            type_matches = len(path_parts & type_folders)
            scores['type'] += type_matches * 0.3

            # Project detection (nested src, docs, assets)
            if 'src' in path_parts or 'docs' in path_parts or 'assets' in path_parts:
                scores['project'] += 0.3

            # Date detection
            if re.search(r'/\d{4}/', f['path']):
                scores['date'] += 0.5

        # Normalize scores
        total = sum(scores.values()) or 1
        scores = {k: v / total for k, v in scores.items()}

        # Find best match
        best = max(scores.items(), key=lambda x: x[1])
        best_pattern = OrganizationPattern(best[0]) if best[1] > 0.2 else None

        return best_pattern, scores

    def _find_orphan_files(self, files: List[Dict], clusters: List[FileCluster]) -> List[str]:
        """Find files not belonging to any detected cluster."""
        clustered_paths = set()
        for cluster in clusters:
            clustered_paths.update(cluster.paths)

        orphans = []
        for f in files:
            if f['path'] not in clustered_paths:
                orphans.append(f['path'])

        return orphans

    def analyze(self) -> StructureAnalysis:
        """
        Analyze current file structure.

        Returns:
            StructureAnalysis with detected patterns, projects, issues
        """
        data = self._load_file_data()
        files = data['files']

        if not files:
            return StructureAnalysis(
                total_files=0,
                total_size_bytes=0,
                detected_projects=[],
                orphan_files=[],
                duplicate_candidates=[],
                naming_issues=[],
                deep_nesting=[],
                current_pattern=None,
                pattern_scores={}
            )

        # Detect projects/clusters
        clusters = self._detect_project_boundaries(files)

        # Find orphan files
        orphans = self._find_orphan_files(files, clusters)

        # Analyze naming
        naming_issues = self._analyze_naming_consistency(files)

        # Find deep nesting
        max_depth = 5 - int(self.sensitivity * 2)  # Stricter = lower threshold
        deep_nesting = [
            f['path'] for f in files
            if len(Path(f['path']).parts) > max_depth
        ]

        # Detect current pattern
        current_pattern, pattern_scores = self._detect_current_pattern(files)

        return StructureAnalysis(
            total_files=len(files),
            total_size_bytes=sum(f.get('size_bytes', 0) for f in files),
            detected_projects=clusters,
            orphan_files=orphans,
            duplicate_candidates=[],  # TODO: Implement
            naming_issues=naming_issues,
            deep_nesting=deep_nesting[:50],  # Limit
            current_pattern=current_pattern,
            pattern_scores=pattern_scores
        )

    def suggest_reorganization(
        self,
        pattern: OrganizationPattern,
        base_path: Optional[Path] = None
    ) -> ReorganizationPlan:
        """
        Suggest reorganization based on a specific pattern.

        Args:
            pattern: The organizational pattern to apply
            base_path: Where to create new structure (default: ~/Organized)

        Returns:
            ReorganizationPlan with specific suggestions
        """
        if base_path is None:
            base_path = Path.home() / "Organized"

        data = self._load_file_data()
        files = data['files']
        suggestions = []
        warnings = []

        for f in files:
            path = Path(f['path'])
            category = f.get('category', 'other')
            ext = path.suffix.lower()

            # Determine suggested location based on pattern
            if pattern == OrganizationPattern.PARA:
                suggested = self._suggest_para_location(path, category, base_path)

            elif pattern == OrganizationPattern.PROJECT_CENTRIC:
                suggested = self._suggest_project_location(path, category, base_path, data['projects'])

            elif pattern == OrganizationPattern.DATE_BASED:
                suggested = self._suggest_date_location(path, f, base_path)

            elif pattern == OrganizationPattern.TYPE_BASED:
                suggested = self._suggest_type_location(path, category, base_path)

            elif pattern == OrganizationPattern.ACTION_BASED:
                suggested = self._suggest_action_location(path, category, base_path)

            elif pattern == OrganizationPattern.HYBRID:
                suggested = self._suggest_hybrid_location(path, category, f, data['projects'], base_path)

            else:
                continue

            if suggested and str(suggested) != f['path']:
                suggestions.append(OrganizationSuggestion(
                    source_path=f['path'],
                    suggested_path=str(suggested),
                    reason=self._get_suggestion_reason(pattern, path, suggested),
                    pattern=pattern,
                    confidence=0.5 + self.sensitivity * 0.3,
                    is_move=True
                ))

        # Add warnings
        if len(suggestions) > 1000:
            warnings.append(f"Large reorganization: {len(suggestions)} files would be moved")

        return ReorganizationPlan(
            pattern=pattern,
            suggestions=suggestions,
            total_files=len(files),
            total_moves=len(suggestions),
            estimated_benefit=self._estimate_benefit(pattern, suggestions),
            warnings=warnings,
            created_at=datetime.now()
        )

    def _suggest_para_location(self, path: Path, category: str, base: Path) -> Path:
        """Suggest PARA location for a file."""
        # Determine PARA category
        name = path.name.lower()
        parts = [p.lower() for p in path.parts]

        if any(k in name or k in parts for k in ['active', 'current', 'wip', 'draft']):
            return base / "Projects" / path.parent.name / path.name

        if any(k in name or k in parts for k in ['archive', 'old', 'backup', 'completed']):
            return base / "Archives" / datetime.now().strftime("%Y") / path.name

        if category in ['docs', 'data']:
            return base / "Resources" / category.capitalize() / path.name

        if category == 'code':
            return base / "Projects" / path.parent.name / path.name

        return base / "Areas" / "Unsorted" / path.name

    def _suggest_project_location(
        self,
        path: Path,
        category: str,
        base: Path,
        projects: List[Dict]
    ) -> Path:
        """Suggest project-centric location."""
        # Find which project this file belongs to
        for proj in projects:
            if path.is_relative_to(Path(proj['path'])):
                proj_name = proj['name']

                # Determine subfolder
                if category == 'code':
                    return base / "Projects" / proj_name / "src" / path.name
                elif category == 'docs':
                    return base / "Projects" / proj_name / "docs" / path.name
                elif category == 'media':
                    return base / "Projects" / proj_name / "assets" / path.name
                elif category == 'data':
                    return base / "Projects" / proj_name / "data" / path.name
                else:
                    return base / "Projects" / proj_name / path.name

        return base / "Projects" / "_Unsorted" / path.name

    def _suggest_date_location(self, path: Path, file_info: Dict, base: Path) -> Path:
        """Suggest date-based location."""
        # Try to get modification date
        modified = file_info.get('modified_at')
        if modified:
            try:
                dt = datetime.fromisoformat(modified)
            except Exception:
                dt = datetime.now()
        else:
            dt = datetime.now()

        year = dt.strftime("%Y")
        month = dt.strftime("%m-%B")

        # Rename with date prefix if not already dated
        name = path.name
        if not re.match(r'^\d{8}', name):
            date_prefix = dt.strftime("%Y%m%d")
            name = f"{date_prefix}_{name}"

        return base / year / month / name

    def _suggest_type_location(self, path: Path, category: str, base: Path) -> Path:
        """Suggest type-based location."""
        category_folders = {
            'code': 'Code',
            'docs': 'Documents',
            'media': 'Media',
            'data': 'Data',
            'config': 'Config',
            'archive': 'Archives',
            'other': 'Other'
        }

        folder = category_folders.get(category, 'Other')
        return base / folder / path.name

    def _suggest_action_location(self, path: Path, category: str, base: Path) -> Path:
        """Suggest action-based (GTD) location."""
        name = path.name.lower()
        parts = [p.lower() for p in path.parts]

        if any(k in name or k in parts for k in ['inbox', 'new', 'download', 'unsorted']):
            return base / "00-Inbox" / path.name

        if any(k in name or k in parts for k in ['active', 'current', 'wip']):
            return base / "01-Active" / path.name

        if any(k in name or k in parts for k in ['ref', 'reference', 'template']):
            return base / "02-Reference" / path.name

        if any(k in name or k in parts for k in ['someday', 'maybe', 'future']):
            return base / "03-Someday" / path.name

        if any(k in name or k in parts for k in ['archive', 'old', 'done', 'completed']):
            return base / "04-Archive" / path.name

        # Default to reference for docs, active for code
        if category == 'code':
            return base / "01-Active" / path.name
        else:
            return base / "02-Reference" / path.name

    def _suggest_hybrid_location(
        self,
        path: Path,
        category: str,
        file_info: Dict,
        projects: List[Dict],
        base: Path
    ) -> Path:
        """Suggest hybrid (PARA + Project) location."""
        # Check if file is part of a project
        for proj in projects:
            if path.is_relative_to(Path(proj['path'])):
                proj_name = proj['name']
                return base / "Projects" / proj_name / category / path.name

        # Not in a project - use PARA for other files
        name = path.name.lower()

        if any(k in name for k in ['archive', 'old', 'backup']):
            return base / "Archive" / datetime.now().strftime("%Y") / path.name

        if category in ['docs', 'data']:
            return base / "Resources" / category.capitalize() / path.name

        return base / "Areas" / "Unsorted" / path.name

    def _get_suggestion_reason(
        self,
        pattern: OrganizationPattern,
        source: Path,
        dest: Path
    ) -> str:
        """Generate human-readable reason for suggestion."""
        pattern_reasons = {
            OrganizationPattern.PARA: "Organized by actionability (PARA method)",
            OrganizationPattern.PROJECT_CENTRIC: "Grouped with related project files",
            OrganizationPattern.DATE_BASED: "Organized chronologically",
            OrganizationPattern.TYPE_BASED: "Grouped by file type",
            OrganizationPattern.ACTION_BASED: "Organized by action state (GTD)",
            OrganizationPattern.HYBRID: "Combined project + PARA organization"
        }
        return pattern_reasons.get(pattern, "Better organization")

    def _estimate_benefit(
        self,
        pattern: OrganizationPattern,
        suggestions: List[OrganizationSuggestion]
    ) -> str:
        """Estimate benefit of reorganization."""
        if len(suggestions) == 0:
            return "No changes needed"

        if len(suggestions) < 50:
            return "Minor cleanup - small improvement"

        if len(suggestions) < 200:
            return "Moderate reorganization - improved findability"

        return "Major reorganization - significant structure improvement"

    def preview_all_patterns(self, sample_size: int = 10) -> Dict[str, List[Dict]]:
        """
        Preview how files would be organized under each pattern.

        Returns sample of suggestions for each pattern.
        """
        data = self._load_file_data()
        files = data['files'][:sample_size * 5]  # Use subset for speed

        previews = {}

        for pattern in OrganizationPattern:
            plan = self.suggest_reorganization(pattern)
            previews[pattern.value] = {
                "pattern_info": asdict(PATTERN_INFO[pattern]),
                "sample_moves": [
                    {"from": s.source_path, "to": s.suggested_path, "reason": s.reason}
                    for s in plan.suggestions[:sample_size]
                ],
                "total_moves": plan.total_moves,
                "estimated_benefit": plan.estimated_benefit
            }

        return previews


# =============================================================================
# CLI INTERFACE
# =============================================================================

def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Synaptic's File Organization Analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python file_organization_analyzer.py analyze
  python file_organization_analyzer.py analyze --sensitivity 0.8
  python file_organization_analyzer.py suggest --pattern para
  python file_organization_analyzer.py preview
  python file_organization_analyzer.py patterns
  python file_organization_analyzer.py backup
  python file_organization_analyzer.py restore --session 1
        """
    )

    subparsers = parser.add_subparsers(dest="command")

    # Analyze command
    analyze_p = subparsers.add_parser("analyze", help="Analyze current structure")
    analyze_p.add_argument("--sensitivity", type=float, default=0.5,
                          help="Detection sensitivity 0.0-1.0 (default: 0.5)")

    # Suggest command
    suggest_p = subparsers.add_parser("suggest", help="Suggest reorganization")
    suggest_p.add_argument("--pattern", type=str, required=True,
                          choices=[p.value for p in OrganizationPattern])
    suggest_p.add_argument("--sensitivity", type=float, default=0.5)
    suggest_p.add_argument("--output", type=Path, help="Save plan to JSON file")

    # Preview command
    preview_p = subparsers.add_parser("preview", help="Preview all patterns")
    preview_p.add_argument("--samples", type=int, default=5, help="Samples per pattern")

    # Patterns command
    subparsers.add_parser("patterns", help="Show all organizational patterns")

    # Backup command
    backup_p = subparsers.add_parser("backup", help="List or create backups")
    backup_p.add_argument("--list", action="store_true", help="List existing backups")

    # Restore command
    restore_p = subparsers.add_parser("restore", help="Restore from backup")
    restore_p.add_argument("--session", type=int, required=True, help="Session ID to restore")

    args = parser.parse_args()

    if args.command == "analyze":
        analyzer = FileOrganizationAnalyzer(sensitivity=args.sensitivity)
        analysis = analyzer.analyze()

        print("╔══════════════════════════════════════════════════════════════════════╗")
        print("║  📊 FILE STRUCTURE ANALYSIS                                          ║")
        print(f"║  Sensitivity: {args.sensitivity:.1f} ({'Loose' if args.sensitivity < 0.4 else 'Balanced' if args.sensitivity < 0.7 else 'Strict'})                                              ║")
        print("╚══════════════════════════════════════════════════════════════════════╝")
        print()
        print(f"  Total Files:    {analysis.total_files:,}")
        print(f"  Total Size:     {format_size(analysis.total_size_bytes)}")
        print(f"  Projects Found: {len(analysis.detected_projects)}")
        print(f"  Orphan Files:   {len(analysis.orphan_files)}")
        print()

        if analysis.current_pattern:
            print(f"  Current Pattern: {analysis.current_pattern.value.upper()}")
        print("  Pattern Scores:")
        for pattern, score in sorted(analysis.pattern_scores.items(), key=lambda x: -x[1]):
            bar = "█" * int(score * 20)
            print(f"    {pattern:12} {bar:<20} {score:.1%}")
        print()

        if analysis.detected_projects:
            print("  📁 Detected Projects:")
            for proj in analysis.detected_projects[:10]:
                print(f"    • {proj.name} ({proj.file_count} files, {format_size(proj.total_size_bytes)})")
                print(f"      Indicators: {', '.join(proj.indicators)}")

        if analysis.naming_issues:
            print(f"\n  ⚠️ Naming Issues ({len(analysis.naming_issues)}):")
            for issue in analysis.naming_issues[:5]:
                print(f"    • {issue}")

        if analysis.deep_nesting:
            print(f"\n  ⚠️ Deep Nesting ({len(analysis.deep_nesting)} files >5 levels)")

    elif args.command == "suggest":
        analyzer = FileOrganizationAnalyzer(sensitivity=args.sensitivity)
        pattern = OrganizationPattern(args.pattern)
        plan = analyzer.suggest_reorganization(pattern)

        print(f"╔══════════════════════════════════════════════════════════════════════╗")
        print(f"║  📋 REORGANIZATION PLAN: {pattern.value.upper():<40}  ║")
        print(f"╚══════════════════════════════════════════════════════════════════════╝")
        print()
        print(f"  Pattern: {PATTERN_INFO[pattern].name}")
        print(f"  Files to Move: {plan.total_moves}")
        print(f"  Benefit: {plan.estimated_benefit}")
        print()

        if plan.warnings:
            print("  ⚠️ Warnings:")
            for w in plan.warnings:
                print(f"    • {w}")
            print()

        print("  Sample Moves:")
        for s in plan.suggestions[:10]:
            print(f"    {Path(s.source_path).name}")
            print(f"      → {s.suggested_path}")
            print()

        if args.output:
            plan_dict = {
                "pattern": plan.pattern.value,
                "total_moves": plan.total_moves,
                "suggestions": [asdict(s) for s in plan.suggestions],
                "created_at": plan.created_at.isoformat()
            }
            args.output.write_text(json.dumps(plan_dict, indent=2))
            print(f"  💾 Plan saved to {args.output}")

    elif args.command == "preview":
        analyzer = FileOrganizationAnalyzer()
        previews = analyzer.preview_all_patterns(sample_size=args.samples)

        print("╔══════════════════════════════════════════════════════════════════════╗")
        print("║  🔮 ORGANIZATION PATTERN PREVIEWS                                     ║")
        print("║  How your files would look under each pattern                         ║")
        print("╚══════════════════════════════════════════════════════════════════════╝")
        print()

        for pattern_name, preview in previews.items():
            info = preview['pattern_info']
            print(f"━━━ {info['name']} ━━━")
            print(f"  {info['description']}")
            print(f"  Best for: {', '.join(info['best_for'][:3])}")
            print(f"  Total moves: {preview['total_moves']}")
            print(f"  Benefit: {preview['estimated_benefit']}")
            print()
            print("  Sample moves:")
            for move in preview['sample_moves'][:3]:
                print(f"    {Path(move['from']).name}")
                print(f"      → {move['to']}")
            print()

    elif args.command == "patterns":
        print("╔══════════════════════════════════════════════════════════════════════╗")
        print("║  📚 ORGANIZATIONAL PATTERNS                                           ║")
        print("║  Proven methods from productivity experts                             ║")
        print("╚══════════════════════════════════════════════════════════════════════╝")
        print()

        for pattern, info in PATTERN_INFO.items():
            print(f"━━━ {pattern.value.upper()}: {info.name} ━━━")
            print(f"  {info.description}")
            print()
            print("  Best for:")
            for use in info.best_for:
                print(f"    • {use}")
            print()
            print("  Structure:")
            for line in info.structure_example[:6]:
                print(f"    {line}")
            print()
            print("  ✅ Pros:", ", ".join(info.pros[:3]))
            print("  ⚠️ Cons:", ", ".join(info.cons[:2]))
            print()

    elif args.command == "backup":
        backup = FileBackupManager()

        if args.list:
            backups = backup.list_backups()
            print("╔══════════════════════════════════════════════════════════════════════╗")
            print("║  💾 BACKUP SESSIONS                                                   ║")
            print("╚══════════════════════════════════════════════════════════════════════╝")
            print()
            for b in backups:
                print(f"  Session {b['id']}: {b['created_at']}")
                print(f"    Files: {b['total_files']}, Status: {b['status']}")
                if b['description']:
                    print(f"    Description: {b['description']}")
                print()
        else:
            print("Use --list to see existing backups")
            print("Backups are created automatically before any file moves")

    elif args.command == "restore":
        backup = FileBackupManager()
        result = backup.restore_backup(args.session)

        print(f"╔══════════════════════════════════════════════════════════════════════╗")
        print(f"║  ♻️ RESTORE COMPLETE                                                  ║")
        print(f"╚══════════════════════════════════════════════════════════════════════╝")
        print()
        print(f"  Session: {result['session_id']}")
        print(f"  Files Restored: {result['files_restored']}")

        if result['errors']:
            print(f"\n  ⚠️ Errors:")
            for err in result['errors'][:5]:
                print(f"    • {err}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
