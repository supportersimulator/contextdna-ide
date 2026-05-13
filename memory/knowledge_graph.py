#!/usr/bin/env python3
"""
Knowledge Graph - Hierarchical Architecture Organization

This module provides hierarchical organization of architecture knowledge using
Context DNA's block system. Instead of flat storage, knowledge is organized in
a tree structure for better navigation and discovery.

HIERARCHY STRUCTURE:
```
ersim-voice-learnings (space)
├── Infrastructure (block)
│   ├── AWS
│   │   ├── EC2 → [django-deployment, voice-gpu-setup]
│   │   ├── ECS → [task-definitions, service-updates]
│   │   └── Lambda → [gpu-toggle, api-handlers]
│   ├── Docker → [compose-patterns, networking]
│   ├── Terraform → [vpc-setup, security-groups]
│   └── Networking → [nlb-config, cloudflare-dns]
├── Voice Pipeline
│   ├── STT → [whisper-config, async-patterns]
│   ├── LLM → [bedrock-setup, boto3-async]
│   └── TTS → [kyutai-config, sample-rate-fix]
├── Frontend
│   ├── Admin Dashboard → [v0-sync-protocol, livekit-integration]
│   └── Landing Page → [submodule-commit-workflow]
└── Protocols
    ├── Deployment → [django-deploy, ecs-update, terraform-apply]
    ├── Debugging → [log-patterns, health-checks]
    └── Sync → [v0-dev, git-submodule]
```

BENEFITS:
- Navigate architecture like a filesystem
- Find related learnings by browsing hierarchy
- Auto-categorize new learnings by content analysis
- Visual representation in dashboard

Usage:
    from memory.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph()

    # Create hierarchy
    kg.create_hierarchy()

    # Auto-categorize a learning
    path = kg.categorize("Fixed boto3 blocking in LLM service")
    # Returns: "Voice Pipeline/LLM"

    # Get all learnings in a category
    learnings = kg.get_learnings("Infrastructure/AWS/EC2")

    # Browse structure
    structure = kg.get_structure()
"""

import os
import sys
import re
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from acontext import AcontextClient
    ACONTEXT_AVAILABLE = True
except ImportError:
    ACONTEXT_AVAILABLE = False


# =============================================================================
# HIERARCHY DEFINITION
# =============================================================================

# Define the knowledge hierarchy structure
HIERARCHY = {
    "Infrastructure": {
        "description": "Deployment and infrastructure details",
        "children": {
            "AWS": {
                "description": "Amazon Web Services resources",
                "children": {
                    "EC2": {"description": "EC2 instances and configurations"},
                    "ECS": {"description": "Container services and tasks"},
                    "Lambda": {"description": "Serverless functions"},
                    "RDS": {"description": "Database services"},
                    "S3": {"description": "Object storage"},
                    "VPC": {"description": "Networking and VPCs"},
                }
            },
            "Docker": {
                "description": "Container configurations",
                "children": {
                    "Compose": {"description": "Docker Compose patterns"},
                    "Networking": {"description": "Container networking"},
                    "Registry": {"description": "Image registries"},
                }
            },
            "Terraform": {
                "description": "Infrastructure as code",
                "children": {
                    "Modules": {"description": "Terraform modules"},
                    "State": {"description": "State management"},
                    "Variables": {"description": "Variable definitions"},
                }
            },
            "Networking": {
                "description": "Network configuration",
                "children": {
                    "NLB": {"description": "Network load balancers"},
                    "DNS": {"description": "DNS and domain configuration"},
                    "SSL": {"description": "SSL/TLS certificates"},
                    "Cloudflare": {"description": "Cloudflare settings"},
                }
            },
        }
    },
    "Memory_System": {
        "description": "Architecture memory and learning system (THIS system)",
        "children": {
            "Acontext": {
                "description": "Vector database backend for semantic search",
                "children": {
                    "Sessions": {"description": "Session management and SOP extraction"},
                    "Spaces": {"description": "Knowledge space organization"},
                    "Search": {"description": "Semantic search patterns"},
                }
            },
            "Brain": {
                "description": "Architecture Brain orchestration layer",
                "children": {
                    "Capture": {"description": "Auto-capture mechanisms (win, fix, command)"},
                    "Consolidate": {"description": "Pattern detection and insight generation"},
                    "Distribute": {"description": "Context injection and brain state"},
                }
            },
            "Hooks": {
                "description": "Claude Code integration hooks",
                "children": {
                    "UserPromptSubmit": {"description": "Pre-prompt context injection"},
                    "GitPostCommit": {"description": "Auto-learn from git commits"},
                    "SuccessCapture": {"description": "Objective success detection"},
                }
            },
            "SOP_Types": {
                "description": "Typed learning categories",
                "children": {
                    "SOP": {"description": "Standard Operating Procedures"},
                    "Gotcha": {"description": "Warnings and edge cases"},
                    "Pattern": {"description": "Recurring code patterns"},
                    "Protocol": {"description": "Development workflows"},
                }
            },
            "Knowledge_Graph": {
                "description": "Hierarchical organization (this structure)",
                "children": {
                    "Categories": {"description": "Top-level categories"},
                    "Auto_Categorize": {"description": "Content-based categorization"},
                    "Cross_Reference": {"description": "Related learning links"},
                }
            },
        }
    },
    "Voice_Pipeline": {
        "description": "Real-time voice processing pipeline",
        "children": {
            "STT": {"description": "Speech-to-text (Whisper)"},
            "LLM": {"description": "Language model integration (Bedrock)"},
            "TTS": {"description": "Text-to-speech (Kyutai)"},
            "LiveKit": {"description": "WebRTC media server"},
        }
    },
    "Frontend": {
        "description": "User interface applications",
        "children": {
            "Admin": {"description": "Admin dashboard (admin.ersimulator.com)"},
            "Landing": {"description": "Landing page (ersimulator.com)"},
            "Mobile": {"description": "Mobile application (React Native)"},
            "Monitor": {"description": "ER Sim Monitor (vitals display)"},
        }
    },
    "Backend": {
        "description": "Server-side services",
        "children": {
            "Django": {"description": "Django REST API"},
            "API": {"description": "API endpoints and contracts"},
            "Database": {"description": "PostgreSQL and data models"},
            "Auth": {"description": "Authentication and authorization"},
        }
    },
    "Protocols": {
        "description": "Standard operating procedures and workflows",
        "children": {
            "Deployment": {"description": "Deployment procedures"},
            "Debugging": {"description": "Debugging patterns and tools"},
            "Sync": {"description": "Repository sync workflows (v0-dev, submodules)"},
            "Testing": {"description": "Testing strategies"},
        }
    },
    "Gotchas": {
        "description": "Cross-cutting warnings and edge cases",
        "children": {
            "AWS": {"description": "AWS-specific gotchas"},
            "Docker": {"description": "Docker-specific gotchas"},
            "Async": {"description": "Async/await pitfalls"},
            "Git": {"description": "Git and submodule gotchas"},
        }
    },
}


# =============================================================================
# KEYWORD MAPPING FOR AUTO-CATEGORIZATION
# =============================================================================

CATEGORY_KEYWORDS = {
    # Memory System (THIS system - index learnings about learnings)
    "Memory_System/Acontext/Sessions": ["acontext", "session", "sop extraction", "flush"],
    "Memory_System/Acontext/Spaces": ["space", "knowledge space", "vector db"],
    "Memory_System/Acontext/Search": ["semantic search", "experience search", "query"],
    "Memory_System/Brain/Capture": ["capture", "win", "fix", "brain.py", "auto_capture"],
    "Memory_System/Brain/Consolidate": ["consolidate", "pattern detection", "insight"],
    "Memory_System/Brain/Distribute": ["context injection", "brain state", "distribute"],
    "Memory_System/Hooks/UserPromptSubmit": ["userpromptsubmit", "pre-prompt", "hook"],
    "Memory_System/Hooks/GitPostCommit": ["post-commit", "auto_learn", "git hook"],
    "Memory_System/Hooks/SuccessCapture": ["objective success", "success detection", "system confirmed"],
    "Memory_System/SOP_Types/SOP": ["sop", "standard operating procedure", "record_sop"],
    "Memory_System/SOP_Types/Gotcha": ["gotcha", "warning", "record_gotcha"],
    "Memory_System/SOP_Types/Pattern": ["pattern", "recurring", "record_pattern"],
    "Memory_System/SOP_Types/Protocol": ["protocol", "workflow", "record_protocol"],
    "Memory_System/Knowledge_Graph/Categories": ["hierarchy", "category", "knowledge graph"],
    "Memory_System/Knowledge_Graph/Auto_Categorize": ["auto-categorize", "keyword mapping"],
    "Memory_System/Knowledge_Graph/Cross_Reference": ["cross-reference", "related", "link"],

    # Infrastructure
    "Infrastructure/AWS/EC2": ["ec2", "instance", "ami", "ssh", "t3", "g5", "c6i"],
    "Infrastructure/AWS/ECS": ["ecs", "task", "service", "container", "fargate"],
    "Infrastructure/AWS/Lambda": ["lambda", "serverless", "function", "api gateway"],
    "Infrastructure/AWS/RDS": ["rds", "database", "postgresql", "mysql"],
    "Infrastructure/AWS/S3": ["s3", "bucket", "object storage"],
    "Infrastructure/AWS/VPC": ["vpc", "subnet", "security group", "cidr"],
    "Infrastructure/Docker/Compose": ["docker-compose", "compose", "docker compose"],
    "Infrastructure/Docker/Networking": ["docker network", "bridge", "host network"],
    "Infrastructure/Terraform/Modules": ["terraform", ".tf", "module", "provider"],
    "Infrastructure/Networking/NLB": ["nlb", "load balancer", "target group"],
    "Infrastructure/Networking/DNS": ["dns", "cloudflare", "route53", "domain"],
    "Infrastructure/Networking/SSL": ["ssl", "tls", "certificate", "https"],

    # Voice Pipeline
    "Voice Pipeline/STT/Config": ["whisper", "faster-whisper", "stt config"],
    "Voice Pipeline/STT/Streaming": ["streaming transcription", "realtime stt"],
    "Voice Pipeline/LLM/Bedrock": ["bedrock", "claude", "converse api"],
    "Voice Pipeline/LLM/Async": ["asyncio", "to_thread", "async boto", "event loop"],
    "Voice Pipeline/TTS/Config": ["kyutai", "moshi", "tts config", "sample rate"],
    "Voice Pipeline/TTS/Audio": ["audio", "pcm", "wav", "48khz", "24khz"],
    "Voice Pipeline/Agent/Plugins": ["livekit agent", "plugin", "voice agent"],

    # Frontend
    "Frontend/Admin/Components": ["react", "component", "shadcn", "tailwind"],
    "Frontend/Admin/LiveKit": ["livekit sdk", "room", "participant", "track"],
    "Frontend/Admin/v0-Sync": ["v0", "v0.dev", "prototype"],
    "Frontend/Landing/Deployment": ["landing page", "submodule"],

    # Backend
    "Backend/API": ["django", "rest", "api", "endpoint", "view"],
    "Backend/Models": ["model", "migration", "database", "orm"],
    "Backend/Auth": ["auth", "jwt", "token", "session"],
    "Backend/Deployment": ["gunicorn", "systemd", "wsgi"],

    # Protocols
    "Protocols/Deployment/Django": ["deploy django", "restart gunicorn"],
    "Protocols/Deployment/ECS": ["deploy ecs", "update service"],
    "Protocols/Deployment/Terraform": ["terraform apply", "terraform plan"],
    "Protocols/Debugging/Logs": ["journalctl", "docker logs", "cloudwatch"],
    "Protocols/Debugging/Health": ["health check", "/health", "healthcheck"],
    "Protocols/Git/Submodules": ["submodule", "git submodule"],
    "Protocols/Git/Commits": ["commit", "git commit", "commit message"],

    # Gotchas
    "Gotchas/Async": ["blocking", "event loop", "asyncio", "sync in async"],
    "Gotchas/Docker": ["docker restart", "env vars", "container"],
    "Gotchas/AWS": ["ip changes", "asg", "instance id"],
    "Gotchas/WebRTC": ["webrtc", "udp", "stun", "turn", "ice"],
}


# =============================================================================
# KNOWLEDGE GRAPH CLASS
# =============================================================================

class KnowledgeGraph:
    """
    Hierarchical organization of architecture knowledge.

    Uses Acontext blocks to create a navigable tree structure
    for all learnings and SOPs.
    """

    DEFAULT_BASE_URL = "http://localhost:8029/api/v1"
    DEFAULT_API_KEY = "sk-ac-your-root-api-bearer-token"

    def __init__(
        self,
        base_url: str = None,
        api_key: str = None,
        space_id: str = None
    ):
        """Initialize knowledge graph.

        Keyword-only operations (categorize, list_categories, etc.) work
        without the acontext SDK.  Network-dependent operations
        (create_hierarchy, link_learning_to_category) lazily initialize
        the AcontextClient and raise only if the SDK is unavailable.

        Args:
            base_url: Context DNA API URL
            api_key: API key
            space_id: Space ID
        """
        self.base_url = base_url or os.getenv("ACONTEXT_BASE_URL", self.DEFAULT_BASE_URL)
        self.api_key = api_key or os.getenv("ACONTEXT_API_KEY", self.DEFAULT_API_KEY)
        self.space_id = space_id or os.getenv("ACONTEXT_SPACE_ID")

        # Lazy-init: only created when network operations need it
        self._client = None

        # Cache for block IDs
        self._block_cache = {}
        self._load_block_cache()

    @property
    def client(self):
        """Lazy-init AcontextClient on first network access.

        Raises RuntimeError only when a caller actually needs the network
        client (create_hierarchy, link_learning_to_category).
        Keyword-only operations like categorize() never touch this.
        """
        if self._client is None:
            if not ACONTEXT_AVAILABLE:
                raise RuntimeError(
                    "acontext package not installed or shadowed by local acontext/ directory"
                )
            self._client = AcontextClient(
                base_url=self.base_url,
                api_key=self.api_key,
            )
            if not self.space_id:
                self._ensure_space()
        return self._client

    def _ensure_space(self):
        """Find or create the default space."""
        spaces = self.client.spaces.list()
        for space in spaces.items:
            if space.configs.get("name") == "ersim-voice-learnings":
                self.space_id = space.id
                return

        space = self.client.spaces.create(
            user="ersim-voice-agent",
            configs={
                "name": "ersim-voice-learnings",
                "description": "ER Simulator learnings with hierarchical organization"
            }
        )
        self.space_id = space.id

    def _get_cache_file(self) -> Path:
        """Get path to block cache file."""
        return Path(__file__).parent / ".knowledge_graph_cache.json"

    def _load_block_cache(self):
        """Load block cache from file."""
        cache_file = self._get_cache_file()
        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    self._block_cache = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.debug(f"Block cache load failed, resetting: {e}")
                self._block_cache = {}

    def _save_block_cache(self):
        """Save block cache to file."""
        cache_file = self._get_cache_file()
        with open(cache_file, "w") as f:
            json.dump(self._block_cache, f, indent=2)

    def create_hierarchy(self, force: bool = False):
        """
        Create the full knowledge hierarchy as blocks.

        This creates the tree structure in Context DNA.
        Only needs to be run once, or when hierarchy changes.

        Args:
            force: If True, recreate even if exists
        """
        if self._block_cache and not force:
            print("Hierarchy already exists. Use force=True to recreate.")
            return

        print("Creating knowledge hierarchy...")
        self._create_blocks_recursive(HIERARCHY, parent_path="")
        self._save_block_cache()
        print(f"Created {len(self._block_cache)} hierarchy nodes")

    def _create_blocks_recursive(self, structure: dict, parent_path: str, parent_id: str = None):
        """Recursively create blocks for hierarchy."""
        for name, info in structure.items():
            path = f"{parent_path}/{name}" if parent_path else name
            description = info.get("description", name)

            # Create block
            try:
                block = self.client.blocks.create({
                    "space_id": self.space_id,
                    "parent_id": parent_id,
                    "title": name,
                    "type": "category",
                    "props": {
                        "description": description,
                        "path": path,
                        "created_at": datetime.now().isoformat()
                    }
                })
                block_id = block.id
                print(f"   Created: {path}")
            except Exception as e:
                # Block creation may fail if API doesn't support it
                # Fall back to using path as pseudo-ID
                block_id = f"path:{path}"
                print(f"   Note: Using path-based ID for {path} ({e})")

            self._block_cache[path] = {
                "id": block_id,
                "description": description,
                "parent_path": parent_path
            }

            # Recurse to children
            children = info.get("children", {})
            if children:
                self._create_blocks_recursive(children, path, block_id)

    def categorize(self, content: str) -> str:
        """
        Auto-categorize content based on keywords.

        Analyzes the content and returns the best matching
        category path in the hierarchy.

        Args:
            content: Text content to categorize

        Returns:
            Category path (e.g., "Voice Pipeline/LLM/Async")
        """
        content_lower = content.lower()

        # Score each category by keyword matches
        scores = {}
        for path, keywords in CATEGORY_KEYWORDS.items():
            score = 0
            for keyword in keywords:
                if keyword.lower() in content_lower:
                    # Weight longer matches higher
                    score += len(keyword.split())
            if score > 0:
                scores[path] = score

        if not scores:
            return "Gotchas"  # Default category

        # Return highest scoring category
        return max(scores.items(), key=lambda x: x[1])[0]

    def get_block_id(self, path: str) -> Optional[str]:
        """Get block ID for a path."""
        info = self._block_cache.get(path)
        return info["id"] if info else None

    def get_structure(self, root: str = None) -> dict:
        """
        Get the hierarchy structure.

        Args:
            root: Optional root path to start from

        Returns:
            Dict representing the tree structure
        """
        if root:
            # Filter to just paths under root
            filtered = {
                k: v for k, v in self._block_cache.items()
                if k.startswith(root)
            }
            return filtered

        return self._block_cache

    def list_categories(self, parent: str = None) -> list[str]:
        """
        List available categories.

        Args:
            parent: Optional parent path to filter by

        Returns:
            List of category paths
        """
        categories = []
        for path in self._block_cache.keys():
            if parent:
                if path.startswith(parent + "/"):
                    categories.append(path)
            else:
                categories.append(path)
        return sorted(categories)

    def get_category_description(self, path: str) -> str:
        """Get description for a category path."""
        info = self._block_cache.get(path)
        return info["description"] if info else "Unknown category"

    def link_learning_to_category(self, session_id: str, content: str):
        """
        Link a learning session to its category in the hierarchy.

        Args:
            session_id: The learning session ID
            content: Content of the learning (for categorization)
        """
        # Auto-categorize
        category = self.categorize(content)
        block_id = self.get_block_id(category)

        if block_id and not block_id.startswith("path:"):
            try:
                # Try to move the session to the block
                self.client.blocks.move(session_id, parent_id=block_id)
                print(f"   Linked to category: {category}")
            except Exception as e:
                print(f"[WARN] Session category link failed: {e}")

        return category

    def print_tree(self, indent: int = 0, parent: str = None):
        """
        Print the hierarchy as a tree.

        Args:
            indent: Current indentation level
            parent: Parent path (for recursion)
        """
        for path, info in sorted(self._block_cache.items()):
            # Check if this is a direct child
            if parent:
                if not path.startswith(parent + "/"):
                    continue
                # Must be immediate child
                remainder = path[len(parent) + 1:]
                if "/" in remainder:
                    continue
            else:
                # Root level
                if "/" in path:
                    continue

            name = path.split("/")[-1] if "/" in path else path
            desc = info.get("description", "")
            print("  " * indent + f"├── {name}: {desc}")

            # Recurse
            self.print_tree(indent + 1, path)

    # =========================================================================
    # INTELLIGENT BREADTH-BASED SEARCH
    # =========================================================================

    def search_with_breadth(
        self,
        query: str,
        risk_level: str = "moderate",
        max_results: int = 10
    ) -> dict:
        """
        Search hierarchy with breadth based on risk level.

        Lower success likelihood = wider search across more branches.

        Args:
            query: The search query
            risk_level: "critical" (5%), "high" (30%), "moderate" (60%), "low" (90%)
            max_results: Maximum results to return

        Returns:
            Dict with:
                - primary_category: Best matching category
                - primary_results: Results from primary branch
                - related_categories: Related branches searched
                - related_results: Results from related branches
                - cross_references: Learnings that span multiple categories
        """
        # Determine search breadth based on risk
        BREADTH_CONFIG = {
            "critical": {"depth": 3, "related_branches": 5, "parent_levels": 2},
            "high":     {"depth": 2, "related_branches": 3, "parent_levels": 1},
            "moderate": {"depth": 1, "related_branches": 2, "parent_levels": 1},
            "low":      {"depth": 1, "related_branches": 1, "parent_levels": 0},
        }
        config = BREADTH_CONFIG.get(risk_level, BREADTH_CONFIG["moderate"])

        # 1. Find primary category
        primary_category = self.categorize(query)

        # 2. Get parent categories (for broader context)
        parent_categories = self._get_parent_categories(primary_category, config["parent_levels"])

        # 3. Find related categories (by shared keywords)
        related_categories = self._find_related_categories(query, primary_category, config["related_branches"])

        # 4. Build search paths
        search_paths = [primary_category] + parent_categories + related_categories

        return {
            "primary_category": primary_category,
            "search_paths": search_paths,
            "config": config,
            "risk_level": risk_level,
        }

    def _get_parent_categories(self, category: str, levels: int) -> list:
        """Get parent categories up to N levels."""
        parents = []
        parts = category.split("/")

        for i in range(1, min(levels + 1, len(parts))):
            parent = "/".join(parts[:-i])
            if parent:
                parents.append(parent)

        return parents

    def _find_related_categories(self, query: str, primary: str, max_related: int) -> list:
        """Find categories related to query that aren't the primary."""
        query_lower = query.lower()
        scores = {}

        for path, keywords in CATEGORY_KEYWORDS.items():
            if path == primary or path.startswith(primary + "/"):
                continue  # Skip primary and children

            score = 0
            for keyword in keywords:
                if keyword.lower() in query_lower:
                    score += len(keyword.split())

            if score > 0:
                scores[path] = score

        # Return top N related
        sorted_paths = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [path for path, _ in sorted_paths[:max_related]]

    # =========================================================================
    # CROSS-REFERENCE LINKING
    # =========================================================================

    # Define which categories are commonly related
    CROSS_REFERENCES = {
        "Infrastructure/AWS/ECS": ["Voice_Pipeline/Agent", "Infrastructure/Docker", "Backend/Deployment"],
        "Infrastructure/AWS/Lambda": ["Backend/API", "Infrastructure/AWS/VPC"],
        "Infrastructure/Docker": ["Voice_Pipeline", "Backend/Deployment", "Infrastructure/AWS/ECS"],
        "Voice_Pipeline/STT": ["Voice_Pipeline/Agent", "Infrastructure/AWS/ECS"],
        "Voice_Pipeline/LLM": ["Voice_Pipeline/Agent", "Backend/API", "Gotchas/Async"],
        "Voice_Pipeline/TTS": ["Voice_Pipeline/Agent", "Gotchas/Async"],
        "Backend/API": ["Frontend/Admin", "Infrastructure/AWS/Lambda", "Backend/Auth"],
        "Backend/Deployment": ["Infrastructure/AWS/EC2", "Protocols/Deployment"],
        "Frontend/Admin": ["Backend/API", "Voice_Pipeline/Agent"],
        "Memory_System/Acontext": ["Memory_System/Brain", "Memory_System/SOP_Types"],
        "Memory_System/Brain": ["Memory_System/Hooks", "Memory_System/Acontext"],
        "Memory_System/Hooks": ["Memory_System/Brain", "Memory_System/SOP_Types"],
    }

    def get_cross_references(self, category: str) -> list:
        """Get cross-referenced categories for a given category."""
        # Direct references
        refs = list(self.CROSS_REFERENCES.get(category, []))

        # Also check if this category is referenced by others
        for cat, xrefs in self.CROSS_REFERENCES.items():
            if category in xrefs and cat not in refs:
                refs.append(cat)

        return refs

    def get_full_context(self, query: str, risk_level: str = "moderate") -> dict:
        """
        Get full context for a query including cross-references.

        This is the main entry point for intelligent hierarchy search.

        Args:
            query: The search query
            risk_level: Risk level for breadth

        Returns:
            Comprehensive context dict
        """
        # Get breadth-based search paths
        search_info = self.search_with_breadth(query, risk_level)

        # Add cross-references
        primary = search_info["primary_category"]
        cross_refs = self.get_cross_references(primary)

        # Also get cross-refs for related categories
        for related in search_info["search_paths"]:
            for xref in self.get_cross_references(related):
                if xref not in cross_refs and xref not in search_info["search_paths"]:
                    cross_refs.append(xref)

        return {
            **search_info,
            "cross_references": cross_refs,
            "all_paths": list(set(search_info["search_paths"] + cross_refs)),
        }


# =============================================================================
# CROSS-CATEGORY KEYWORDS (span multiple hierarchies)
# =============================================================================
# These keywords indicate content that spans multiple categories
CROSS_CATEGORY_KEYWORDS = {
    "api": ["Backend/API", "Infrastructure/AWS/Lambda", "Voice_Pipeline/Agent"],
    "async": ["Gotchas/Async", "Voice_Pipeline/LLM", "Voice_Pipeline/STT"],
    "deployment": ["Protocols/Deployment", "Backend/Deployment", "Infrastructure/AWS/ECS"],
    "docker": ["Infrastructure/Docker", "Voice_Pipeline", "Backend/Deployment"],
    "livekit": ["Voice_Pipeline/Agent", "Frontend/Admin", "Infrastructure/Networking"],
    "health": ["Protocols/Debugging/Health", "Infrastructure/AWS/ECS", "Backend/API"],
    "config": ["Infrastructure", "Voice_Pipeline", "Backend"],
    "env": ["Infrastructure/Docker", "Backend/Deployment", "Gotchas/Docker"],
}


# =============================================================================
# CONFIG TRACKING
# =============================================================================

CONFIG_FILE_PATTERNS = {
    ".env": "Environment configuration",
    ".env.*": "Environment configuration (variant)",
    "*.tf": "Terraform infrastructure",
    "*.tfvars": "Terraform variables",
    "docker-compose*.yml": "Docker Compose configuration",
    "docker-compose*.yaml": "Docker Compose configuration",
    "Dockerfile*": "Docker image definition",
    "*.json": "JSON configuration",
    "requirements*.txt": "Python dependencies",
    "package*.json": "Node.js dependencies",
    "tsconfig*.json": "TypeScript configuration",
    "CLAUDE.md": "Atlas instructions",
    "settings*.py": "Django settings",
}


def detect_config_file(filepath: str) -> Optional[str]:
    """Detect if a file is a configuration file."""
    import fnmatch
    filename = os.path.basename(filepath)

    for pattern, description in CONFIG_FILE_PATTERNS.items():
        if fnmatch.fnmatch(filename, pattern):
            return description

    return None


# =============================================================================
# VERSION-AWARE TAGGING
# =============================================================================

def get_current_git_info() -> dict:
    """Get current git commit info for version tagging."""
    import subprocess

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()[:8]

        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()

        return {
            "commit": commit,
            "branch": branch,
            "timestamp": datetime.now().isoformat()
        }
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug(f"Git info retrieval failed: {e}")
        return {"commit": "unknown", "branch": "unknown", "timestamp": datetime.now().isoformat()}


# =============================================================================
# STALE DETECTION
# =============================================================================

STALE_THRESHOLDS = {
    "Infrastructure": 90,  # 90 days
    "Voice_Pipeline": 60,  # 60 days
    "Frontend": 45,        # 45 days
    "Backend": 60,         # 60 days
    "Protocols": 120,      # 120 days
    "Memory_System": 30,   # 30 days (evolving fast)
    "Gotchas": 180,        # 180 days (gotchas are more stable)
}


def check_stale(category: str, created_date: str) -> Optional[str]:
    """Check if a learning might be stale."""
    from datetime import datetime, timedelta

    try:
        created = datetime.fromisoformat(created_date.replace("Z", "+00:00"))
        now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()

        # Get threshold for this category's root
        root = category.split("/")[0]
        threshold_days = STALE_THRESHOLDS.get(root, 90)

        age_days = (now - created).days

        if age_days > threshold_days:
            return f"Learning is {age_days} days old (threshold: {threshold_days} days)"

    except Exception as e:
        print(f"[WARN] Learning age check failed: {e}")

    return None


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Knowledge Graph CLI")
        print("")
        print("Commands:")
        print("  init                    - Initialize hierarchy structure")
        print("  tree                    - Print hierarchy tree")
        print("  list [parent]           - List categories")
        print("  categorize <text>       - Auto-categorize text")
        print("  describe <path>         - Get category description")
        print("")
        print("Examples:")
        print("  python knowledge_graph.py init")
        print("  python knowledge_graph.py tree")
        print("  python knowledge_graph.py list 'Voice Pipeline'")
        print("  python knowledge_graph.py categorize 'Fixed boto3 blocking in asyncio'")
        sys.exit(0)

    cmd = sys.argv[1]

    try:
        kg = KnowledgeGraph()
    except Exception as e:
        print(f"Failed to initialize knowledge graph: {e}")
        sys.exit(1)

    if cmd == "init":
        force = "--force" in sys.argv
        kg.create_hierarchy(force=force)

    elif cmd == "tree":
        print("\nKnowledge Hierarchy:")
        print("=" * 40)
        kg.print_tree()

    elif cmd == "list":
        parent = sys.argv[2] if len(sys.argv) > 2 else None
        categories = kg.list_categories(parent)
        print(f"Categories{f' under {parent}' if parent else ''}:")
        for cat in categories:
            print(f"  {cat}")

    elif cmd == "categorize":
        if len(sys.argv) < 3:
            print("Usage: categorize <text>")
            sys.exit(1)
        text = " ".join(sys.argv[2:])
        category = kg.categorize(text)
        desc = kg.get_category_description(category)
        print(f"Category: {category}")
        print(f"Description: {desc}")

    elif cmd == "describe":
        if len(sys.argv) < 3:
            print("Usage: describe <path>")
            sys.exit(1)
        path = sys.argv[2]
        desc = kg.get_category_description(path)
        print(f"{path}: {desc}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
