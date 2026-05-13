"""
Project-SOP Integration for Context DNA.

Connects the SOP learning system with the project hierarchy detector,
enabling:
- Project-scoped SOPs (tagged by project)
- Context-aware webhook injection
- Cross-project pattern discovery
- Project-specific learning retrieval

This is what makes Context DNA truly adaptive - SOPs aren't just stored,
they're organized by the project context they came from.

Usage:
    from context_dna.project_sop_integration import ProjectSOPManager

    manager = ProjectSOPManager()

    # Record an SOP with project context
    manager.record_sop_for_project(
        title="Deploy Django to Production",
        steps=["git pull", "restart gunicorn"],
        project_path="/path/to/backend"
    )

    # Get SOPs relevant to current project
    sopS = manager.get_sops_for_project(project_path="/path/to/backend")

    # Get SOPs for webhook injection
    context = manager.get_injection_context(
        file_path="/path/to/backend/models.py",
        query="how to add a new model"
    )
"""

import json
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

# Import SOP system
try:
    from context_dna.sop_types import SOPRegistry, LearningType, Learning
    SOP_AVAILABLE = True
except ImportError:
    try:
        from sop_types import SOPRegistry, LearningType, Learning
        SOP_AVAILABLE = True
    except ImportError:
        SOP_AVAILABLE = False

# Import project context
try:
    from context_dna.storage.resilient_store import (
        ProjectContextManager,
        ProjectContext,
        PlatformInfo,
    )
    PROJECT_CONTEXT_AVAILABLE = True
except ImportError:
    PROJECT_CONTEXT_AVAILABLE = False


# =============================================================================
# PROJECT-SCOPED SOP
# =============================================================================

@dataclass
class ProjectScopedSOP:
    """An SOP with project context."""
    sop_id: str
    title: str
    sop_type: str  # LearningType value
    content: str
    tags: List[str]
    project_id: Optional[str]
    project_name: Optional[str]
    project_type: Optional[str]
    project_path: Optional[str]
    created_at: datetime
    relevance_score: float = 1.0  # How relevant to current context

    def to_dict(self) -> dict:
        return {
            "sop_id": self.sop_id,
            "title": self.title,
            "sop_type": self.sop_type,
            "content": self.content,
            "tags": self.tags,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "project_type": self.project_type,
            "project_path": self.project_path,
            "created_at": self.created_at.isoformat(),
            "relevance_score": self.relevance_score,
        }


# =============================================================================
# PROJECT SOP MANAGER
# =============================================================================

class ProjectSOPManager:
    """
    Manages SOPs with project context awareness.

    Key capabilities:
    - Tag SOPs with project metadata
    - Filter SOPs by project
    - Score relevance for webhook injection
    - Cross-project pattern discovery
    """

    def __init__(self, config_dir: Path = None):
        self.platform = PlatformInfo.detect() if PROJECT_CONTEXT_AVAILABLE else None
        self.config_dir = config_dir or (
            self.platform.config_dir if self.platform else Path.home() / ".context-dna"
        )

        # SOP storage
        self.sop_index_file = self.config_dir / "project_sops.json"
        self._sop_index: Dict[str, Dict] = self._load_index()

        # Project context
        self.context_manager = (
            ProjectContextManager(self.config_dir)
            if PROJECT_CONTEXT_AVAILABLE else None
        )

        # SOP registry
        self.sop_registry = SOPRegistry() if SOP_AVAILABLE else None

    # -------------------------------------------------------------------------
    # Recording SOPs with Project Context
    # -------------------------------------------------------------------------

    def record_sop_for_project(
        self,
        title: str,
        steps: List[str] = None,
        content: str = None,
        sop_type: str = "sop",
        tags: List[str] = None,
        project_path: str = None,
        **kwargs,
    ) -> Optional[str]:
        """
        Record an SOP with project context.

        Args:
            title: SOP title
            steps: Steps for procedural SOPs
            content: Full content for non-procedural SOPs
            sop_type: Type of learning (sop, gotcha, pattern, etc.)
            tags: Additional tags
            project_path: Path to project (auto-detected if not provided)

        Returns:
            SOP ID if successful
        """
        # Detect project context
        project_context = None
        if project_path:
            project_context = self._detect_project(project_path)
        elif self.context_manager:
            project_context = self.context_manager.get_current_project()

        # Build tags with project info
        all_tags = list(tags or [])
        if project_context:
            all_tags.append(f"project:{project_context.project_name}")
            all_tags.append(f"type:{project_context.project_type}")

        # Record via SOP registry
        sop_id = None
        if self.sop_registry:
            if sop_type == "sop" and steps:
                sop_id = self.sop_registry.record_sop(
                    title=title,
                    steps=steps,
                    tags=all_tags,
                    **kwargs,
                )
            elif sop_type == "gotcha":
                sop_id = self.sop_registry.record_gotcha(
                    title=title,
                    when_it_happens=kwargs.get("when_it_happens", ""),
                    consequence=kwargs.get("consequence", ""),
                    solution=content or kwargs.get("solution", ""),
                    tags=all_tags,
                )
            elif sop_type == "pattern":
                sop_id = self.sop_registry.record_pattern(
                    name=title,
                    pattern=content or kwargs.get("pattern", ""),
                    use_case=kwargs.get("use_case", ""),
                    example=kwargs.get("example"),
                    tags=all_tags,
                )
            else:
                # Generic learning
                sop_id = self.sop_registry.record_learning(
                    title=title,
                    content=content or str(steps),
                    learning_type=LearningType(sop_type),
                    tags=all_tags,
                )

        # Index for project lookup
        if sop_id:
            self._index_sop(
                sop_id=sop_id,
                title=title,
                sop_type=sop_type,
                tags=all_tags,
                project_context=project_context,
            )

        return sop_id

    def record_gotcha_for_project(
        self,
        title: str,
        when_it_happens: str,
        consequence: str,
        solution: str,
        project_path: str = None,
        tags: List[str] = None,
    ) -> Optional[str]:
        """Record a gotcha with project context."""
        return self.record_sop_for_project(
            title=title,
            sop_type="gotcha",
            content=solution,
            tags=tags,
            project_path=project_path,
            when_it_happens=when_it_happens,
            consequence=consequence,
            solution=solution,
        )

    def record_pattern_for_project(
        self,
        name: str,
        pattern: str,
        use_case: str,
        project_path: str = None,
        tags: List[str] = None,
        example: str = None,
    ) -> Optional[str]:
        """Record a pattern with project context."""
        return self.record_sop_for_project(
            title=name,
            sop_type="pattern",
            content=pattern,
            tags=tags,
            project_path=project_path,
            pattern=pattern,
            use_case=use_case,
            example=example,
        )

    # -------------------------------------------------------------------------
    # Querying SOPs by Project
    # -------------------------------------------------------------------------

    def get_sops_for_project(
        self,
        project_path: str = None,
        project_type: str = None,
        sop_type: str = None,
        limit: int = 20,
    ) -> List[ProjectScopedSOP]:
        """
        Get SOPs relevant to a project.

        Args:
            project_path: Path to project
            project_type: Filter by project type (python, nextjs, etc.)
            sop_type: Filter by SOP type
            limit: Max results

        Returns:
            List of project-scoped SOPs
        """
        results = []

        # Determine project context
        project_context = None
        if project_path:
            project_context = self._detect_project(project_path)

        # Filter index
        for sop_id, entry in self._sop_index.items():
            # Filter by project
            if project_context and entry.get("project_id") != project_context.project_id:
                # Different project - lower relevance but might still match type
                if project_type and entry.get("project_type") == project_type:
                    pass  # Keep it, same type
                else:
                    continue

            # Filter by SOP type
            if sop_type and entry.get("sop_type") != sop_type:
                continue

            # Build result
            sop = ProjectScopedSOP(
                sop_id=sop_id,
                title=entry.get("title", ""),
                sop_type=entry.get("sop_type", "sop"),
                content=entry.get("content", ""),
                tags=entry.get("tags", []),
                project_id=entry.get("project_id"),
                project_name=entry.get("project_name"),
                project_type=entry.get("project_type"),
                project_path=entry.get("project_path"),
                created_at=datetime.fromisoformat(entry.get("created_at", datetime.now().isoformat())),
                relevance_score=self._calculate_relevance(entry, project_context),
            )
            results.append(sop)

        # Sort by relevance
        results.sort(key=lambda x: x.relevance_score, reverse=True)
        return results[:limit]

    def get_gotchas_for_project(
        self,
        project_path: str = None,
        search_query: str = None,
        limit: int = 10,
    ) -> List[ProjectScopedSOP]:
        """Get gotchas relevant to a project."""
        sops = self.get_sops_for_project(
            project_path=project_path,
            sop_type="gotcha",
            limit=limit * 2,  # Get more, then filter
        )

        if search_query:
            # Score by query relevance
            query_lower = search_query.lower()
            for sop in sops:
                if query_lower in sop.title.lower() or query_lower in sop.content.lower():
                    sop.relevance_score *= 2.0  # Boost matching SOPs
                for tag in sop.tags:
                    if query_lower in tag.lower():
                        sop.relevance_score *= 1.5

            sops.sort(key=lambda x: x.relevance_score, reverse=True)

        return sops[:limit]

    # -------------------------------------------------------------------------
    # Webhook Injection Context
    # -------------------------------------------------------------------------

    def get_injection_context(
        self,
        file_path: str,
        query: str = None,
        max_sops: int = 5,
        include_cross_project: bool = True,
    ) -> Dict[str, Any]:
        """
        Get context for webhook injection.

        This is called by the webhook system to provide relevant SOPs
        based on the current file and query.

        Args:
            file_path: Current file being edited
            query: User's query/question
            max_sops: Max SOPs to include
            include_cross_project: Include SOPs from similar project types

        Returns:
            Context dict ready for injection
        """
        # Detect project
        project_context = self._detect_project(file_path)

        context = {
            "project": None,
            "sops": [],
            "gotchas": [],
            "patterns": [],
            "cross_project_relevant": [],
        }

        if project_context:
            context["project"] = {
                "name": project_context.project_name,
                "type": project_context.project_type,
                "path": str(project_context.project_path),
            }

        # Get project-specific SOPs
        sops = self.get_sops_for_project(
            project_path=file_path,
            limit=max_sops * 2,
        )

        # Categorize
        for sop in sops[:max_sops]:
            entry = sop.to_dict()
            if sop.sop_type == "gotcha":
                context["gotchas"].append(entry)
            elif sop.sop_type == "pattern":
                context["patterns"].append(entry)
            else:
                context["sops"].append(entry)

        # Get cross-project relevant (same type, different project)
        if include_cross_project and project_context:
            cross_sops = self.get_sops_for_project(
                project_type=project_context.project_type,
                limit=max_sops,
            )
            for sop in cross_sops:
                if sop.project_id != project_context.project_id:
                    context["cross_project_relevant"].append(sop.to_dict())

        return context

    def format_injection_prompt(
        self,
        context: Dict[str, Any],
        max_length: int = 2000,
    ) -> str:
        """
        Format injection context as a prompt addition.

        This creates the text that gets injected into the AI prompt
        to provide project-specific context.
        """
        parts = []

        # Project context
        if context.get("project"):
            proj = context["project"]
            parts.append(f"## Current Project: {proj['name']} ({proj['type']})")

        # Relevant gotchas (most important for avoiding mistakes)
        if context.get("gotchas"):
            parts.append("\n### Project Gotchas (Watch Out!):")
            for gotcha in context["gotchas"][:3]:
                parts.append(f"- **{gotcha['title']}**: {gotcha.get('content', '')[:200]}")

        # Relevant patterns
        if context.get("patterns"):
            parts.append("\n### Established Patterns:")
            for pattern in context["patterns"][:2]:
                parts.append(f"- **{pattern['title']}**")

        # Relevant SOPs
        if context.get("sops"):
            parts.append("\n### Relevant Procedures:")
            for sop in context["sops"][:2]:
                parts.append(f"- {sop['title']}")

        result = "\n".join(parts)

        # Truncate if needed
        if len(result) > max_length:
            result = result[:max_length - 50] + "\n... (context truncated)"

        return result

    # -------------------------------------------------------------------------
    # Cross-Project Pattern Discovery
    # -------------------------------------------------------------------------

    def discover_shared_patterns(
        self,
        project_type: str = None,
        min_occurrences: int = 2,
    ) -> List[Dict[str, Any]]:
        """
        Discover patterns that appear across multiple projects.

        These are candidates for promotion to global best practices.
        """
        pattern_occurrences = {}

        for sop_id, entry in self._sop_index.items():
            if entry.get("sop_type") != "pattern":
                continue

            if project_type and entry.get("project_type") != project_type:
                continue

            # Key by title (simplified matching)
            title = entry.get("title", "").lower()
            if title not in pattern_occurrences:
                pattern_occurrences[title] = {
                    "title": entry.get("title"),
                    "projects": [],
                    "entries": [],
                }

            pattern_occurrences[title]["projects"].append(entry.get("project_name"))
            pattern_occurrences[title]["entries"].append(entry)

        # Filter by occurrence threshold
        shared = []
        for title, data in pattern_occurrences.items():
            unique_projects = set(data["projects"])
            if len(unique_projects) >= min_occurrences:
                shared.append({
                    "pattern": data["title"],
                    "used_in": list(unique_projects),
                    "occurrence_count": len(data["entries"]),
                    "entries": data["entries"],
                })

        shared.sort(key=lambda x: x["occurrence_count"], reverse=True)
        return shared

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _detect_project(self, path: str) -> Optional[ProjectContext]:
        """Detect project from a file path."""
        if not self.context_manager:
            return None
        return self.context_manager.detect_from_path(Path(path))

    def _index_sop(
        self,
        sop_id: str,
        title: str,
        sop_type: str,
        tags: List[str],
        project_context: Optional[ProjectContext],
    ):
        """Index an SOP for project lookup."""
        entry = {
            "title": title,
            "sop_type": sop_type,
            "tags": tags,
            "created_at": datetime.now().isoformat(),
        }

        if project_context:
            entry["project_id"] = project_context.project_id
            entry["project_name"] = project_context.project_name
            entry["project_type"] = project_context.project_type
            entry["project_path"] = str(project_context.project_path)

        self._sop_index[sop_id] = entry
        self._save_index()

    def _calculate_relevance(
        self,
        entry: dict,
        project_context: Optional[ProjectContext],
    ) -> float:
        """Calculate relevance score for an SOP."""
        score = 1.0

        if not project_context:
            return score

        # Same project = highest relevance
        if entry.get("project_id") == project_context.project_id:
            score *= 2.0

        # Same project type = good relevance
        if entry.get("project_type") == project_context.project_type:
            score *= 1.5

        return score

    def _load_index(self) -> Dict[str, Dict]:
        """Load SOP index from disk."""
        if self.sop_index_file.exists():
            try:
                return json.loads(self.sop_index_file.read_text())
            except Exception as e:
                print(f"[WARN] Failed to load SOP index from {self.sop_index_file}: {e}")
        return {}

    def _save_index(self):
        """Save SOP index to disk."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.sop_index_file.write_text(json.dumps(self._sop_index, indent=2))


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    manager = ProjectSOPManager()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "test":
            print("=== Project-SOP Integration Test ===")
            print()

            # Test recording
            sop_id = manager.record_sop_for_project(
                title="Test SOP",
                steps=["Step 1", "Step 2", "Step 3"],
                tags=["test"],
                project_path=".",
            )
            print(f"Recorded SOP: {sop_id}")

            # Test retrieval
            sops = manager.get_sops_for_project(project_path=".")
            print(f"Retrieved {len(sops)} SOPs for current project")

            # Test injection context
            context = manager.get_injection_context(file_path="./test.py")
            print(f"Injection context keys: {list(context.keys())}")

            print()
            print("Test PASSED")

        elif cmd == "inject":
            # Get injection context for a file
            file_path = sys.argv[2] if len(sys.argv) > 2 else "."
            context = manager.get_injection_context(file_path=file_path)
            prompt = manager.format_injection_prompt(context)
            print(prompt)

        elif cmd == "discover":
            # Discover shared patterns
            shared = manager.discover_shared_patterns()
            print(f"Found {len(shared)} shared patterns across projects:")
            for pattern in shared[:5]:
                print(f"  - {pattern['pattern']} (used in {len(pattern['used_in'])} projects)")

        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python project_sop_integration.py [test|inject <file>|discover]")
    else:
        print("Usage: python project_sop_integration.py [test|inject <file>|discover]")
