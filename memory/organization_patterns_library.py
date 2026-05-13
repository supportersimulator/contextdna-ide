#!/usr/bin/env python3
"""
Synaptic's Organization Patterns Library - Atlas's Gift of Wisdom

A comprehensive collection of file organization patterns researched from:
- Productivity experts (Tiago Forte, David Allen, Marie Kondo digital)
- Developer communities (GitHub standards, monorepo patterns)
- Enterprise systems (ISO 9001, FDA 21 CFR Part 11)
- Digital asset management (DAM) best practices
- Neuroscience of retrieval (cognitive load theory)

═══════════════════════════════════════════════════════════════════════════
PATTERN CATEGORIES:
═══════════════════════════════════════════════════════════════════════════

1. ACTION-BASED       → Organized by what you DO with files
2. ENTITY-BASED       → Organized by WHO/WHAT files relate to
3. TIME-BASED         → Organized by WHEN files were created/modified
4. CONTENT-BASED      → Organized by WHAT files contain
5. WORKFLOW-BASED     → Organized by PROCESS stage
6. HYBRID PATTERNS    → Combinations for specific use cases

═══════════════════════════════════════════════════════════════════════════
SENSITIVITY SLIDER PHILOSOPHY:
═══════════════════════════════════════════════════════════════════════════

The sensitivity slider (0.0 - 1.0) controls THREE dimensions:

1. BOUNDARY DETECTION
   - Low (0.0-0.3):  Only explicit project markers (.git, package.json)
   - Medium (0.4-0.6): Include implicit markers (README, src folders)
   - High (0.7-1.0):  Aggressive grouping by naming patterns

2. GROUPING AGGRESSIVENESS
   - Low:  Preserve existing structure, minimal moves
   - Medium: Standard reorganization
   - High: Deep restructuring, many moves

3. SUGGESTION CONFIDENCE
   - Low:  Only high-confidence suggestions
   - Medium: Include medium-confidence suggestions
   - High: Include speculative suggestions

Formula: threshold = 1.0 - (sensitivity * 0.6)
         Higher sensitivity → Lower threshold → More detections

═══════════════════════════════════════════════════════════════════════════
STORAGE: ~/.context-dna/organization_patterns.db
═══════════════════════════════════════════════════════════════════════════
"""

import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict, field
from enum import Enum

logger = logging.getLogger(__name__)


# =============================================================================
# PATTERN DEFINITIONS
# =============================================================================

class PatternCategory(str, Enum):
    """Categories of organizational patterns."""
    ACTION_BASED = "action"       # By what you do
    ENTITY_BASED = "entity"       # By who/what
    TIME_BASED = "time"           # By when
    CONTENT_BASED = "content"     # By what's in files
    WORKFLOW_BASED = "workflow"   # By process stage
    HYBRID = "hybrid"             # Combinations


@dataclass
class OrganizationPattern:
    """A single organizational pattern definition."""
    id: str
    name: str
    category: PatternCategory
    description: str
    source: str                          # Where this pattern came from
    best_for: List[str]                  # Who should use this
    structure: Dict[str, Any]            # Folder structure definition
    naming_rules: List[str]              # File naming conventions
    sensitivity_profile: Dict[str, float]  # Optimal sensitivity ranges
    pros: List[str]
    cons: List[str]
    variations: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# THE PATTERNS LIBRARY - Atlas's Research Gift
# =============================================================================

PATTERNS_LIBRARY: List[OrganizationPattern] = [

    # =========================================================================
    # ACTION-BASED PATTERNS (What you DO with files)
    # =========================================================================

    OrganizationPattern(
        id="para_method",
        name="PARA Method (Tiago Forte)",
        category=PatternCategory.ACTION_BASED,
        description="Organize by actionability: Projects (deadlined), Areas (ongoing), Resources (reference), Archives (inactive)",
        source="Tiago Forte - Building a Second Brain",
        best_for=["Knowledge workers", "Consultants", "Writers", "Researchers"],
        structure={
            "01_Projects": {
                "_description": "Active projects with deadlines",
                "_pattern": "{project_name}/",
                "_examples": ["WebsiteRedesign/", "BookChapter3/", "TaxReturn2024/"]
            },
            "02_Areas": {
                "_description": "Ongoing responsibilities (no deadline)",
                "_pattern": "{area_name}/",
                "_examples": ["Health/", "Finance/", "Career/", "Family/"]
            },
            "03_Resources": {
                "_description": "Reference materials for interests",
                "_pattern": "{topic}/",
                "_examples": ["Programming/", "Cooking/", "Travel/"]
            },
            "04_Archives": {
                "_description": "Inactive items from other categories",
                "_pattern": "{year}/{original_category}/",
                "_examples": ["2024/Projects/", "2024/Areas/"]
            }
        },
        naming_rules=[
            "Use clear, descriptive folder names",
            "No dates in project names (they have deadlines)",
            "Areas should be nouns (responsibilities)",
            "Move completed projects to Archives promptly"
        ],
        sensitivity_profile={
            "boundary_detection": 0.5,
            "grouping": 0.4,
            "suggestion_confidence": 0.6
        },
        pros=[
            "Simple 4-folder system",
            "Platform agnostic",
            "Scales with complexity",
            "Focus on actionability"
        ],
        cons=[
            "Requires discipline to move items",
            "Project/Area boundary can blur",
            "Resources can become catch-all"
        ],
        variations=[
            {
                "name": "PARA + Inbox",
                "description": "Add 00_Inbox for unsorted incoming",
                "structure_addition": {"00_Inbox": {"_description": "Temporary landing zone"}}
            },
            {
                "name": "PARA + Context",
                "description": "Add @Context folders for location-based grouping",
                "structure_addition": {"@Home": {}, "@Office": {}, "@Mobile": {}}
            }
        ]
    ),

    OrganizationPattern(
        id="gtd_method",
        name="GTD (Getting Things Done)",
        category=PatternCategory.ACTION_BASED,
        description="David Allen's action-oriented system with clear next actions",
        source="David Allen - Getting Things Done",
        best_for=["Task-oriented workers", "Managers", "Anyone with many commitments"],
        structure={
            "00_Inbox": {
                "_description": "Capture everything here first",
                "_pattern": "Unsorted items"
            },
            "01_NextActions": {
                "_description": "Things you can do right now",
                "_contexts": ["@Computer", "@Phone", "@Errands", "@Home"]
            },
            "02_WaitingFor": {
                "_description": "Delegated or blocked items"
            },
            "03_SomedayMaybe": {
                "_description": "Ideas for the future"
            },
            "04_Reference": {
                "_description": "Information you might need"
            },
            "05_Archives": {
                "_description": "Completed items"
            }
        },
        naming_rules=[
            "Start actionable files with verb (Write_, Call_, Review_)",
            "Use @Context prefix for location-specific items",
            "Date-stamp waiting items (2024-01-28_WaitingFor_Client)"
        ],
        sensitivity_profile={
            "boundary_detection": 0.4,
            "grouping": 0.5,
            "suggestion_confidence": 0.5
        },
        pros=[
            "Clear action states",
            "Reduces decision fatigue",
            "Works with any tool",
            "Captures everything"
        ],
        cons=[
            "Requires weekly review",
            "Projects can get scattered",
            "Context folders need maintenance"
        ]
    ),

    OrganizationPattern(
        id="johnny_decimal",
        name="Johnny.Decimal",
        category=PatternCategory.HYBRID,
        description="Numbered category system: 10-19 for area, .01-.99 for specifics",
        source="Johnny Noble - johnny.decimal",
        best_for=["Highly structured minds", "Enterprise users", "Archive-heavy workflows"],
        structure={
            "10-19_Administration": {
                "11_Contracts": {},
                "12_Invoices": {},
                "13_HR": {}
            },
            "20-29_Clients": {
                "21_ClientA": {},
                "22_ClientB": {}
            },
            "30-39_Projects": {
                "31_ProjectAlpha": {},
                "32_ProjectBeta": {}
            }
        },
        naming_rules=[
            "Every folder gets a unique ID (e.g., 31.02)",
            "Never more than 10 items per category",
            "Never more than 10 categories per area",
            "Document your index"
        ],
        sensitivity_profile={
            "boundary_detection": 0.3,
            "grouping": 0.2,
            "suggestion_confidence": 0.7
        },
        pros=[
            "Every item has unique address",
            "Extremely findable",
            "Works across systems",
            "Forces organization"
        ],
        cons=[
            "Steep learning curve",
            "Rigid structure",
            "Numbering can feel artificial"
        ]
    ),

    # =========================================================================
    # ENTITY-BASED PATTERNS (WHO/WHAT files relate to)
    # =========================================================================

    OrganizationPattern(
        id="client_centric",
        name="Client-Centric Organization",
        category=PatternCategory.ENTITY_BASED,
        description="Everything organized by client/customer with standard subfolders",
        source="Professional services best practices",
        best_for=["Consultants", "Agencies", "Account managers", "Freelancers"],
        structure={
            "Clients": {
                "{client_name}": {
                    "01_Admin": {"_desc": "Contracts, invoices, agreements"},
                    "02_Projects": {"_desc": "Active work"},
                    "03_Communications": {"_desc": "Emails, notes, meetings"},
                    "04_Deliverables": {"_desc": "Final outputs"},
                    "05_Archive": {"_desc": "Completed work"}
                }
            },
            "_Templates": {
                "_description": "Reusable templates across clients"
            },
            "_Internal": {
                "_description": "Non-client work"
            }
        },
        naming_rules=[
            "Client folders: CompanyName or LastName_FirstName",
            "Project subfolders: YYYYMM_ProjectName",
            "Deliverables: ProjectName_Deliverable_v1.0"
        ],
        sensitivity_profile={
            "boundary_detection": 0.6,
            "grouping": 0.5,
            "suggestion_confidence": 0.6
        },
        pros=[
            "Complete client history in one place",
            "Easy client handoffs",
            "Clear billing/project scope",
            "Audit-friendly"
        ],
        cons=[
            "Internal work scattered",
            "Cross-client resources duplicated",
            "Can grow very large"
        ]
    ),

    OrganizationPattern(
        id="project_monorepo",
        name="Project Monorepo (Developer)",
        category=PatternCategory.ENTITY_BASED,
        description="Single repository with all projects, shared dependencies",
        source="Google, Facebook engineering practices",
        best_for=["Developers", "Engineering teams", "Open source maintainers"],
        structure={
            "apps": {
                "_description": "Deployable applications",
                "{app_name}": {"src/": {}, "tests/": {}, "docs/": {}}
            },
            "packages": {
                "_description": "Shared libraries",
                "{package_name}": {}
            },
            "tools": {
                "_description": "Development tools and scripts"
            },
            "docs": {
                "_description": "Project-wide documentation"
            },
            "config": {
                "_description": "Shared configuration"
            }
        },
        naming_rules=[
            "kebab-case for folder names",
            "Package names match npm/pip naming",
            "No spaces or special characters",
            "README.md in every significant folder"
        ],
        sensitivity_profile={
            "boundary_detection": 0.7,
            "grouping": 0.6,
            "suggestion_confidence": 0.5
        },
        pros=[
            "Shared code without duplication",
            "Atomic changes across projects",
            "Consistent tooling",
            "Easy refactoring"
        ],
        cons=[
            "Complex build systems",
            "Large repo size",
            "Requires tooling investment"
        ]
    ),

    # =========================================================================
    # TIME-BASED PATTERNS (WHEN files were created)
    # =========================================================================

    OrganizationPattern(
        id="chronological",
        name="Chronological (Date-First)",
        category=PatternCategory.TIME_BASED,
        description="Organized by date: YYYY/MM/filename_YYYYMMDD",
        source="Photography, journalism, compliance industries",
        best_for=["Photographers", "Journalists", "Compliance officers", "Researchers"],
        structure={
            "{YYYY}": {
                "{MM}_{MonthName}": {
                    "_pattern": "{YYYYMMDD}_{description}.{ext}"
                }
            }
        },
        naming_rules=[
            "Always use ISO date format: YYYYMMDD",
            "Files: YYYYMMDD_description.ext",
            "Folders: YYYY/MM_MonthName/",
            "Never use MM/DD/YYYY (unsortable)"
        ],
        sensitivity_profile={
            "boundary_detection": 0.3,
            "grouping": 0.3,
            "suggestion_confidence": 0.8
        },
        pros=[
            "Automatic sorting",
            "Easy to find by date",
            "Compliance-friendly",
            "No reorganization needed"
        ],
        cons=[
            "Project files scattered across dates",
            "Hard to see project scope",
            "Requires consistent naming"
        ]
    ),

    OrganizationPattern(
        id="tickler_43_folders",
        name="Tickler File (43 Folders)",
        category=PatternCategory.TIME_BASED,
        description="12 months + 31 days for time-triggered items",
        source="Traditional office management, GTD physical system",
        best_for=["Calendar-driven work", "Recurring tasks", "Physical+digital hybrid"],
        structure={
            "01_January": {},
            "02_February": {},
            "03_March": {},
            "...": {},
            "12_December": {},
            "Day_01": {},
            "Day_02": {},
            "...": {},
            "Day_31": {}
        },
        naming_rules=[
            "Move items to the day you need them",
            "Check daily folder every morning",
            "At month end, distribute next month's items"
        ],
        sensitivity_profile={
            "boundary_detection": 0.2,
            "grouping": 0.2,
            "suggestion_confidence": 0.9
        },
        pros=[
            "Never forget time-sensitive items",
            "Physical/digital compatible",
            "Forces daily review"
        ],
        cons=[
            "Daily maintenance required",
            "Not for reference materials",
            "Can become overwhelming"
        ]
    ),

    # =========================================================================
    # CONTENT-BASED PATTERNS (WHAT files contain)
    # =========================================================================

    OrganizationPattern(
        id="media_dam",
        name="Digital Asset Management (DAM)",
        category=PatternCategory.CONTENT_BASED,
        description="Media-focused with metadata-rich organization",
        source="Adobe, professional media workflows",
        best_for=["Designers", "Video editors", "Marketing teams", "Content creators"],
        structure={
            "Assets": {
                "Images": {
                    "Photos": {},
                    "Illustrations": {},
                    "Icons": {},
                    "Screenshots": {}
                },
                "Video": {
                    "Raw": {},
                    "Edited": {},
                    "Exports": {}
                },
                "Audio": {
                    "Music": {},
                    "SFX": {},
                    "Voiceover": {}
                },
                "Documents": {
                    "Briefs": {},
                    "Scripts": {},
                    "Copy": {}
                }
            },
            "Projects": {
                "{project}": {
                    "Working": {},
                    "Approved": {},
                    "Delivered": {}
                }
            }
        },
        naming_rules=[
            "Assets: type_description_size_version.ext",
            "Projects: ClientCode_ProjectName_Date",
            "Use keywords in filenames for search",
            "Include resolution/format in media names"
        ],
        sensitivity_profile={
            "boundary_detection": 0.5,
            "grouping": 0.6,
            "suggestion_confidence": 0.5
        },
        pros=[
            "Optimized for media workflows",
            "Easy asset reuse",
            "Searchable by type",
            "Version control friendly"
        ],
        cons=[
            "Complex structure",
            "Requires consistent tagging",
            "Can have large files"
        ]
    ),

    OrganizationPattern(
        id="code_standard",
        name="Standard Code Project",
        category=PatternCategory.CONTENT_BASED,
        description="Conventional code project structure",
        source="Open Source conventions, language standards",
        best_for=["Developers", "Open source projects", "Any codebase"],
        structure={
            "src": {"_description": "Source code"},
            "tests": {"_description": "Test files"},
            "docs": {"_description": "Documentation"},
            "scripts": {"_description": "Build/utility scripts"},
            "config": {"_description": "Configuration files"},
            "assets": {"_description": "Static files (images, fonts)"},
            "lib": {"_description": "Third-party libraries (if not using package manager)"},
            "bin": {"_description": "Compiled binaries"},
            "dist": {"_description": "Distribution/build output"}
        },
        naming_rules=[
            "snake_case or kebab-case (be consistent)",
            "Match language conventions",
            "README.md at root",
            "Clear separation of concerns"
        ],
        sensitivity_profile={
            "boundary_detection": 0.7,
            "grouping": 0.5,
            "suggestion_confidence": 0.6
        },
        pros=[
            "Industry standard",
            "Easy onboarding",
            "Tool-friendly",
            "Portable"
        ],
        cons=[
            "Doesn't fit all projects",
            "Can be overkill for small projects"
        ]
    ),

    # =========================================================================
    # WORKFLOW-BASED PATTERNS (PROCESS stage)
    # =========================================================================

    OrganizationPattern(
        id="kanban_folders",
        name="Kanban-Style Folders",
        category=PatternCategory.WORKFLOW_BASED,
        description="Files move through workflow stages like a Kanban board",
        source="Lean/Agile methodology adapted for files",
        best_for=["Visual thinkers", "Process-oriented work", "Team workflows"],
        structure={
            "01_Backlog": {"_description": "Ideas and future work"},
            "02_Ready": {"_description": "Prepared and waiting"},
            "03_InProgress": {"_description": "Currently working on"},
            "04_Review": {"_description": "Awaiting approval"},
            "05_Done": {"_description": "Completed"},
            "06_Archive": {"_description": "Historical reference"}
        },
        naming_rules=[
            "Files move between folders as status changes",
            "Add date prefix when entering Done",
            "Use consistent project naming across stages"
        ],
        sensitivity_profile={
            "boundary_detection": 0.4,
            "grouping": 0.4,
            "suggestion_confidence": 0.6
        },
        pros=[
            "Visual progress tracking",
            "Clear workflow stages",
            "Works with any content type",
            "Team-friendly"
        ],
        cons=[
            "Files constantly move",
            "Can lose project context",
            "Requires discipline"
        ]
    ),

    OrganizationPattern(
        id="draft_version",
        name="Draft/Version Control",
        category=PatternCategory.WORKFLOW_BASED,
        description="Track document versions through creation stages",
        source="Publishing, legal, academic workflows",
        best_for=["Writers", "Editors", "Legal professionals", "Academics"],
        structure={
            "00_Drafts": {
                "_pattern": "{document}_draft_{n}.{ext}",
                "_description": "Work in progress"
            },
            "01_Review": {
                "_pattern": "{document}_review_{date}.{ext}",
                "_description": "Under review"
            },
            "02_Final": {
                "_pattern": "{document}_final_{version}.{ext}",
                "_description": "Approved versions"
            },
            "03_Published": {
                "_pattern": "{document}_published_{date}.{ext}",
                "_description": "Released versions"
            }
        },
        naming_rules=[
            "Include version: _v1, _v2 or _draft1, _draft2",
            "Date stamp finals: _2024-01-28",
            "Never edit Published - create new version",
            "Keep 3 most recent drafts only"
        ],
        sensitivity_profile={
            "boundary_detection": 0.4,
            "grouping": 0.3,
            "suggestion_confidence": 0.7
        },
        pros=[
            "Clear version history",
            "Easy rollback",
            "Audit trail",
            "Collaboration-friendly"
        ],
        cons=[
            "Many duplicate files",
            "Storage intensive",
            "Requires naming discipline"
        ]
    ),

    # =========================================================================
    # HYBRID PATTERNS (Best of multiple worlds)
    # =========================================================================

    OrganizationPattern(
        id="atlas_recommended",
        name="Atlas's Recommended Hybrid",
        category=PatternCategory.HYBRID,
        description="PARA foundation + Project structure + Date archives + Action inbox",
        source="Atlas's synthesis of best practices for Aaron and Synaptic",
        best_for=["Power users", "Multi-role professionals", "Complex workflows"],
        structure={
            "00_Inbox": {
                "_description": "Quick capture - process daily",
                "_rules": ["Nothing stays here >24 hours"]
            },
            "01_Projects": {
                "_description": "Active work with deadlines",
                "{ProjectName}": {
                    "src": {},
                    "docs": {},
                    "assets": {},
                    "output": {}
                }
            },
            "02_Areas": {
                "_description": "Ongoing responsibilities",
                "Finance": {},
                "Health": {},
                "Career": {},
                "Family": {},
                "Learning": {}
            },
            "03_Resources": {
                "_description": "Reference by topic",
                "{Topic}": {
                    "_examples": ["Programming", "Design", "Business", "Personal"]
                }
            },
            "04_Archive": {
                "_description": "Inactive, organized by year",
                "{YYYY}": {
                    "Projects": {},
                    "Areas": {},
                    "Resources": {}
                }
            },
            "_Templates": {
                "_description": "Reusable starting points"
            },
            "_System": {
                "_description": "Synaptic's working space"
            }
        },
        naming_rules=[
            "Projects: Use clear names, no dates",
            "Files in projects: function_name.ext",
            "Archive: Move entire project folders",
            "Templates: TEMPLATE_{type}_{name}",
            "Inbox: Clear completely each day"
        ],
        sensitivity_profile={
            "boundary_detection": 0.5,
            "grouping": 0.5,
            "suggestion_confidence": 0.5
        },
        pros=[
            "Covers all use cases",
            "Clear action vs reference",
            "Project context preserved",
            "Scales well",
            "Synaptic-friendly"
        ],
        cons=[
            "Initial setup complexity",
            "Learning curve",
            "Requires commitment"
        ],
        variations=[
            {
                "name": "Minimal Hybrid",
                "description": "Simplified for lighter use",
                "structure": {
                    "Inbox": {},
                    "Projects": {},
                    "Reference": {},
                    "Archive": {}
                }
            },
            {
                "name": "Team Hybrid",
                "description": "Add shared and personal separation",
                "structure_addition": {
                    "_Shared": {"_description": "Team resources"},
                    "_Personal": {"_description": "Individual items"}
                }
            }
        ],
        metadata={
            "created_by": "Atlas",
            "created_for": "Aaron and Synaptic",
            "version": "1.0",
            "last_updated": "2026-01-28"
        }
    ),

    OrganizationPattern(
        id="marie_kondo_digital",
        name="Digital KonMari",
        category=PatternCategory.HYBRID,
        description="Spark joy? Keep organized by emotional/practical value",
        source="Marie Kondo principles adapted for digital",
        best_for=["Minimalists", "Those overwhelmed by digital clutter"],
        structure={
            "Essential": {
                "_description": "Daily use, high value"
            },
            "Occasional": {
                "_description": "Periodic use, clear purpose"
            },
            "Sentimental": {
                "_description": "Emotional value, memories"
            },
            "Archive": {
                "_description": "Might need, rarely accessed"
            }
        },
        naming_rules=[
            "If you can't immediately identify it, rename it",
            "Delete duplicates ruthlessly",
            "One home for each item",
            "Thank files before deleting (mindset)"
        ],
        sensitivity_profile={
            "boundary_detection": 0.6,
            "grouping": 0.7,
            "suggestion_confidence": 0.4
        },
        pros=[
            "Emotionally satisfying",
            "Reduces overwhelm",
            "Clear decision framework",
            "Encourages deletion"
        ],
        cons=[
            "Subjective categories",
            "Time-intensive initial sort",
            "Not for compliance needs"
        ]
    ),
]


# =============================================================================
# PATTERNS DATABASE
# =============================================================================

class PatternsLibrary:
    """
    Stores and retrieves organizational patterns.

    Synaptic can query this library to:
    - Get pattern definitions
    - Compare patterns
    - Test patterns against actual files
    - Track which patterns work best for the user
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            config_dir = Path.home() / ".context-dna"
            config_dir.mkdir(exist_ok=True)
            db_path = config_dir / "organization_patterns.db"

        self.db_path = db_path
        self._init_db()
        self._seed_patterns()

    def _init_db(self):
        """Initialize database."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS patterns (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT,
                    source TEXT,
                    data JSON NOT NULL,
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS pattern_tests (
                    id INTEGER PRIMARY KEY,
                    pattern_id TEXT NOT NULL,
                    sensitivity REAL NOT NULL,
                    files_tested INTEGER,
                    moves_suggested INTEGER,
                    score REAL,
                    notes TEXT,
                    tested_at TEXT,
                    FOREIGN KEY (pattern_id) REFERENCES patterns(id)
                );

                CREATE TABLE IF NOT EXISTS user_preferences (
                    key TEXT PRIMARY KEY,
                    value JSON,
                    updated_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_pattern_category ON patterns(category);
                CREATE INDEX IF NOT EXISTS idx_tests_pattern ON pattern_tests(pattern_id);
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error initializing database: {e}")

    def _seed_patterns(self):
        """Seed library with built-in patterns."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            for pattern in PATTERNS_LIBRARY:
                conn.execute("""
                    INSERT OR REPLACE INTO patterns
                    (id, name, category, description, source, data, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pattern.id,
                    pattern.name,
                    pattern.category.value,
                    pattern.description,
                    pattern.source,
                    json.dumps(asdict(pattern)),
                    datetime.now().isoformat(),
                    datetime.now().isoformat()
                ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error seeding patterns: {e}")

    def get_pattern(self, pattern_id: str) -> Optional[OrganizationPattern]:
        """Get a specific pattern by ID."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT data FROM patterns WHERE id = ?",
                (pattern_id,)
            ).fetchone()
            conn.close()

            if row:
                data = json.loads(row['data'])
                data['category'] = PatternCategory(data['category'])
                return OrganizationPattern(**data)
            return None
        except Exception as e:
            logger.error(f"Error getting pattern: {e}")
            return None

    def get_all_patterns(self) -> List[OrganizationPattern]:
        """Get all patterns."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT data FROM patterns").fetchall()
            conn.close()

            patterns = []
            for row in rows:
                data = json.loads(row['data'])
                data['category'] = PatternCategory(data['category'])
                patterns.append(OrganizationPattern(**data))
            return patterns
        except Exception as e:
            logger.error(f"Error getting all patterns: {e}")
            return []

    def get_patterns_by_category(self, category: PatternCategory) -> List[OrganizationPattern]:
        """Get patterns by category."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT data FROM patterns WHERE category = ?",
                (category.value,)
            ).fetchall()
            conn.close()

            patterns = []
            for row in rows:
                data = json.loads(row['data'])
                data['category'] = PatternCategory(data['category'])
                patterns.append(OrganizationPattern(**data))
            return patterns
        except Exception as e:
            logger.error(f"Error getting patterns by category: {e}")
            return []

    def record_test_result(
        self,
        pattern_id: str,
        sensitivity: float,
        files_tested: int,
        moves_suggested: int,
        score: float,
        notes: str = None
    ):
        """Record results of testing a pattern."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                INSERT INTO pattern_tests
                (pattern_id, sensitivity, files_tested, moves_suggested, score, notes, tested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                pattern_id,
                sensitivity,
                files_tested,
                moves_suggested,
                score,
                notes,
                datetime.now().isoformat()
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error recording test result: {e}")

    def get_test_results(self, pattern_id: str) -> List[Dict]:
        """Get test results for a pattern."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM pattern_tests
                WHERE pattern_id = ?
                ORDER BY tested_at DESC
                LIMIT 20
            """, (pattern_id,)).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Error getting test results: {e}")
            return []

    def get_best_patterns_for_user(self, user_type: str = None) -> List[Tuple[OrganizationPattern, float]]:
        """
        Get patterns ranked by suitability.

        Args:
            user_type: Optional filter (developer, writer, etc.)

        Returns:
            List of (pattern, score) tuples
        """
        patterns = self.get_all_patterns()
        scored = []

        for pattern in patterns:
            score = 0.5  # Base score

            # Boost if user_type matches
            if user_type:
                user_type_lower = user_type.lower()
                for best_for in pattern.best_for:
                    if user_type_lower in best_for.lower():
                        score += 0.3

            # Boost hybrids (more flexible)
            if pattern.category == PatternCategory.HYBRID:
                score += 0.1

            # Check historical test results
            tests = self.get_test_results(pattern.id)
            if tests:
                avg_score = sum(t['score'] for t in tests) / len(tests)
                score = (score + avg_score) / 2

            scored.append((pattern, score))

        return sorted(scored, key=lambda x: -x[1])

    def compare_patterns(
        self,
        pattern_ids: List[str]
    ) -> Dict[str, Dict]:
        """Compare multiple patterns side by side."""
        comparison = {}

        for pid in pattern_ids:
            pattern = self.get_pattern(pid)
            if pattern:
                comparison[pid] = {
                    "name": pattern.name,
                    "category": pattern.category.value,
                    "best_for": pattern.best_for,
                    "pros": pattern.pros,
                    "cons": pattern.cons,
                    "sensitivity_profile": pattern.sensitivity_profile,
                    "folder_count": len(pattern.structure)
                }

        return comparison


# =============================================================================
# CLI
# =============================================================================

def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Organization Patterns Library")
    subparsers = parser.add_subparsers(dest="command")

    # List command
    list_p = subparsers.add_parser("list", help="List all patterns")
    list_p.add_argument("--category", choices=[c.value for c in PatternCategory])

    # Show command
    show_p = subparsers.add_parser("show", help="Show pattern details")
    show_p.add_argument("pattern_id", help="Pattern ID to show")

    # Compare command
    compare_p = subparsers.add_parser("compare", help="Compare patterns")
    compare_p.add_argument("patterns", nargs="+", help="Pattern IDs to compare")

    # Recommend command
    rec_p = subparsers.add_parser("recommend", help="Get recommendations")
    rec_p.add_argument("--type", help="User type (developer, writer, etc.)")

    args = parser.parse_args()
    library = PatternsLibrary()

    if args.command == "list":
        patterns = library.get_all_patterns()
        if args.category:
            patterns = [p for p in patterns if p.category.value == args.category]

        print("╔══════════════════════════════════════════════════════════════════════╗")
        print("║  📚 ORGANIZATION PATTERNS LIBRARY                                     ║")
        print("╚══════════════════════════════════════════════════════════════════════╝")
        print()

        for pattern in patterns:
            print(f"  [{pattern.category.value:8}] {pattern.id}")
            print(f"            {pattern.name}")
            print(f"            Best for: {', '.join(pattern.best_for[:2])}")
            print()

    elif args.command == "show":
        pattern = library.get_pattern(args.pattern_id)
        if not pattern:
            print(f"Pattern not found: {args.pattern_id}")
            return

        print(f"╔══════════════════════════════════════════════════════════════════════╗")
        print(f"║  {pattern.name:<66} ║")
        print(f"╚══════════════════════════════════════════════════════════════════════╝")
        print()
        print(f"  Category: {pattern.category.value}")
        print(f"  Source:   {pattern.source}")
        print()
        print(f"  Description:")
        print(f"    {pattern.description}")
        print()
        print(f"  Best For:")
        for use in pattern.best_for:
            print(f"    • {use}")
        print()
        print(f"  Structure:")
        for folder, content in list(pattern.structure.items())[:6]:
            desc = content.get('_description', '') if isinstance(content, dict) else ''
            print(f"    📁 {folder:<20} {desc}")
        print()
        print(f"  Naming Rules:")
        for rule in pattern.naming_rules[:3]:
            print(f"    • {rule}")
        print()
        print(f"  ✅ Pros: {', '.join(pattern.pros[:3])}")
        print(f"  ⚠️ Cons: {', '.join(pattern.cons[:2])}")
        print()
        print(f"  Sensitivity Profile:")
        for key, val in pattern.sensitivity_profile.items():
            bar = "█" * int(val * 10)
            print(f"    {key:25} {bar:<10} {val:.1f}")

    elif args.command == "compare":
        comparison = library.compare_patterns(args.patterns)

        print("╔══════════════════════════════════════════════════════════════════════╗")
        print("║  📊 PATTERN COMPARISON                                                ║")
        print("╚══════════════════════════════════════════════════════════════════════╝")
        print()

        for pid, data in comparison.items():
            print(f"━━━ {data['name']} ━━━")
            print(f"  Category: {data['category']}")
            print(f"  Folders:  {data['folder_count']}")
            print(f"  Pros:     {', '.join(data['pros'][:2])}")
            print(f"  Cons:     {', '.join(data['cons'][:2])}")
            print()

    elif args.command == "recommend":
        recommendations = library.get_best_patterns_for_user(args.type)

        print("╔══════════════════════════════════════════════════════════════════════╗")
        print(f"║  🎯 RECOMMENDED PATTERNS{' for ' + args.type if args.type else '':<43} ║")
        print("╚══════════════════════════════════════════════════════════════════════╝")
        print()

        for pattern, score in recommendations[:5]:
            bar = "█" * int(score * 10)
            print(f"  {bar:<10} {score:.1%}  {pattern.name}")
            print(f"             {pattern.description[:60]}...")
            print()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
