#!/usr/bin/env python3
"""
SOP Types and Categories - Typed Learning System for Architecture Brain

This module provides the SAME level of structure as Context DNA's SOPs:
- Typed categories (SOP, Gotcha, Pattern, Protocol, Architecture)
- Automatic extraction from successful operations
- Semantic search via Context DNA backend
- Verified/unverified status tracking

ACONTEXT PARITY:
Context DNA automatically extracts SOPs via GPT-4o-mini when sessions flush.
This module ensures our Architecture Brain records learnings with the same
structure so they're equally searchable and useful.

Types of Learnings:
    SOP         - Standard Operating Procedure (step-by-step what to do)
    GOTCHA      - Things that go wrong / edge cases / warnings
    PATTERN     - Recurring code or infrastructure patterns
    PROTOCOL    - Development workflows / processes
    ARCHITECTURE- System design / component relationships
    BUG_FIX     - Bug diagnosis and resolution
    PERFORMANCE - Performance optimization learnings

Usage:
    from memory.sop_types import SOPRegistry, LearningType

    registry = SOPRegistry()

    # Record a gotcha
    registry.record_gotcha(
        title="Docker restart doesn't reload env vars",
        when_it_happens="Using 'docker restart' after changing .env",
        consequence="Service uses old environment variables",
        solution="Must stop, rm, and run container (or docker-compose up -d --force-recreate)",
        tags=["docker", "env", "ecs"]
    )

    # Record an SOP
    registry.record_sop(
        title="Deploy Django to Production",
        steps=[
            "SSH to ec2 instance",
            "cd /var/www/ersim/app",
            "git pull origin main",
            "sudo systemctl restart gunicorn"
        ],
        prerequisites=["SSH access", "sudo permissions"],
        warnings=["HOME=/root required for git", "No virtualenv - uses system Python"],
        tags=["django", "deployment", "ec2"]
    )

    # Query for gotchas before doing something
    gotchas = registry.get_gotchas("docker ecs")
"""

import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict, field
from enum import Enum
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False

try:
    from memory.bugfix_sop_enhancer import generate_bugfix_sop_title, extract_key_insight
    BUGFIX_ENHANCER_AVAILABLE = True
except ImportError:
    BUGFIX_ENHANCER_AVAILABLE = False

try:
    from memory.process_sop_enhancer import generate_process_sop_title
    PROCESS_ENHANCER_AVAILABLE = True
except ImportError:
    PROCESS_ENHANCER_AVAILABLE = False

# Combined flag for backwards compatibility
SOP_ENHANCER_AVAILABLE = BUGFIX_ENHANCER_AVAILABLE


class LearningType(Enum):
    """Types of learnings that can be recorded."""
    SOP = "sop"                      # Standard Operating Procedure
    GOTCHA = "gotcha"                # Warning / edge case / thing that goes wrong
    PATTERN = "pattern"              # Recurring code or infrastructure pattern
    PROTOCOL = "protocol"            # Development workflow / process
    ARCHITECTURE = "architecture"    # System design / component relationship
    BUG_FIX = "bug_fix"              # Bug diagnosis and resolution
    PERFORMANCE = "performance"      # Performance optimization


@dataclass
class Learning:
    """A typed learning record."""
    title: str
    learning_type: LearningType
    content: str
    tags: List[str] = field(default_factory=list)
    verified: bool = False
    created_at: str = ""
    session_id: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def to_acontext_format(self) -> str:
        """Format for Acontext storage (semantic search friendly)."""
        return f"""## [{self.learning_type.value.upper()}] {self.title}

**Type:** {self.learning_type.value}
**Created:** {self.created_at}
**Verified:** {"Yes" if self.verified else "No"}
**Tags:** {', '.join(self.tags)}

{self.content}

---
Search tags: {self.learning_type.value}, {', '.join(self.tags)}
"""


@dataclass
class SOP(Learning):
    """Standard Operating Procedure."""
    steps: List[str] = field(default_factory=list)
    prerequisites: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    estimated_time: str = ""
    learning_type: LearningType = field(default=LearningType.SOP)
    content: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        self.learning_type = LearningType.SOP
        self._build_content()

    def _build_content(self):
        content = []

        if self.prerequisites:
            content.append("### Prerequisites")
            for p in self.prerequisites:
                content.append(f"- {p}")
            content.append("")

        if self.warnings:
            content.append("### Warnings")
            for w in self.warnings:
                content.append(f"⚠️ {w}")
            content.append("")

        content.append("### Steps")
        for i, step in enumerate(self.steps, 1):
            content.append(f"{i}. {step}")

        if self.estimated_time:
            content.append(f"\n**Estimated Time:** {self.estimated_time}")

        self.content = "\n".join(content)


@dataclass
class Gotcha(Learning):
    """A warning / edge case / thing that goes wrong."""
    when_it_happens: str = ""
    consequence: str = ""
    solution: str = ""
    severity: str = "medium"  # low, medium, high, critical
    learning_type: LearningType = field(default=LearningType.GOTCHA)
    content: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        self.learning_type = LearningType.GOTCHA
        self._build_content()

    def _build_content(self):
        severity_icon = {
            "low": "ℹ️",
            "medium": "⚠️",
            "high": "🔶",
            "critical": "🚨"
        }.get(self.severity, "⚠️")

        content = [
            f"{severity_icon} **Severity:** {self.severity.upper()}",
            "",
            "### When It Happens",
            self.when_it_happens,
            "",
            "### Consequence",
            self.consequence,
            "",
            "### Solution",
            self.solution
        ]
        self.content = "\n".join(content)


@dataclass
class Pattern(Learning):
    """A recurring code or infrastructure pattern."""
    problem: str = ""
    solution: str = ""
    example_code: str = ""
    applies_to: List[str] = field(default_factory=list)
    learning_type: LearningType = field(default=LearningType.PATTERN)
    content: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        self.learning_type = LearningType.PATTERN
        self._build_content()

    def _build_content(self):
        content = [
            "### Problem",
            self.problem,
            "",
            "### Solution Pattern",
            self.solution
        ]

        if self.example_code:
            content.extend([
                "",
                "### Example",
                "```python",
                self.example_code,
                "```"
            ])

        if self.applies_to:
            content.extend([
                "",
                "### Applies To",
                ", ".join(self.applies_to)
            ])

        self.content = "\n".join(content)


@dataclass
class Protocol(Learning):
    """A development workflow or process."""
    description: str = ""
    steps: List[str] = field(default_factory=list)
    guardrails: List[str] = field(default_factory=list)
    when_to_use: str = ""
    learning_type: LearningType = field(default=LearningType.PROTOCOL)
    content: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        self.learning_type = LearningType.PROTOCOL
        self._build_content()

    def _build_content(self):
        content = [
            "### Description",
            self.description,
            "",
            "### When to Use",
            self.when_to_use,
            "",
            "### Steps"
        ]
        for i, step in enumerate(self.steps, 1):
            content.append(f"{i}. {step}")

        if self.guardrails:
            content.extend([
                "",
                "### Guardrails (MUST Follow)"
            ])
            for g in self.guardrails:
                content.append(f"🛡️ {g}")

        self.content = "\n".join(content)


class SOPRegistry:
    """
    Registry for all typed learnings.

    Stores learnings in Context DNA for semantic search while
    maintaining the typed structure locally for quick access.
    """

    LOCAL_CACHE_FILE = Path(__file__).parent / ".sop_registry_cache.json"

    def __init__(self):
        if not CONTEXT_DNA_AVAILABLE:
            raise RuntimeError("Context DNA not available")

        self.memory = ContextDNAClient()
        self.cache = self._load_cache()

    def _load_cache(self) -> dict:
        """Load local cache of recorded learnings."""
        if self.LOCAL_CACHE_FILE.exists():
            try:
                with open(self.LOCAL_CACHE_FILE) as f:
                    return json.load(f)
            except Exception as e:
                print(f"[WARN] Failed to load SOP cache from {self.LOCAL_CACHE_FILE}: {e}")
        return {
            "learnings": [],
            "stats": {
                "sop": 0,
                "gotcha": 0,
                "pattern": 0,
                "protocol": 0,
                "architecture": 0,
                "bug_fix": 0,
                "performance": 0
            }
        }

    def _save_cache(self):
        """Save local cache."""
        with open(self.LOCAL_CACHE_FILE, "w") as f:
            json.dump(self.cache, f, indent=2, default=str)

    def _store_to_acontext(self, learning: Learning) -> str:
        """Store learning to Context DNA and return session ID."""
        # Create a session that will trigger SOP extraction
        session_id = self.memory._create_session(f"{learning.learning_type.value}-{learning.title[:30]}")

        # Store the learning as a completed task
        messages = [
            {
                "role": "user",
                "content": f"Document this {learning.learning_type.value}: {learning.title}"
            },
            {
                "role": "assistant",
                "content": learning.to_acontext_format()
            }
        ]

        for msg in messages:
            self.memory.client.sessions.store_message(
                session_id,
                blob=msg,
                format="openai"
            )

        # Flush to trigger SOP extraction
        self.memory.client.sessions.flush(session_id)

        return session_id

    def _record(self, learning: Learning) -> str:
        """Record any learning type."""
        # Store to Context DNA
        session_id = self._store_to_acontext(learning)
        learning.session_id = session_id

        # Update local cache
        self.cache["learnings"].append({
            "title": learning.title,
            "type": learning.learning_type.value,
            "tags": learning.tags,
            "session_id": session_id,
            "created_at": learning.created_at
        })
        self.cache["stats"][learning.learning_type.value] += 1
        self._save_cache()

        return session_id

    # ==========================================================================
    # PUBLIC API - Record Different Learning Types
    # ==========================================================================

    def record_sop(
        self,
        title: str,
        steps: List[str],
        prerequisites: List[str] = None,
        warnings: List[str] = None,
        estimated_time: str = None,
        tags: List[str] = None,
        verified: bool = True
    ) -> str:
        """Record a Standard Operating Procedure."""
        sop = SOP(
            title=title,
            steps=steps,
            prerequisites=prerequisites or [],
            warnings=warnings or [],
            estimated_time=estimated_time or "",
            tags=tags or [],
            verified=verified
        )
        session_id = self._record(sop)
        print(f"✅ Recorded SOP: {title}")
        return session_id

    def record_gotcha(
        self,
        title: str,
        when_it_happens: str,
        consequence: str,
        solution: str,
        severity: str = "medium",
        tags: List[str] = None,
        verified: bool = True
    ) -> str:
        """Record a gotcha/warning."""
        gotcha = Gotcha(
            title=title,
            when_it_happens=when_it_happens,
            consequence=consequence,
            solution=solution,
            severity=severity,
            tags=tags or [],
            verified=verified
        )
        session_id = self._record(gotcha)
        print(f"⚠️ Recorded Gotcha: {title}")
        return session_id

    def record_pattern(
        self,
        title: str,
        problem: str,
        solution: str,
        example_code: str = None,
        applies_to: List[str] = None,
        tags: List[str] = None,
        verified: bool = True
    ) -> str:
        """Record a recurring pattern."""
        pattern = Pattern(
            title=title,
            problem=problem,
            solution=solution,
            example_code=example_code or "",
            applies_to=applies_to or [],
            tags=tags or [],
            verified=verified
        )
        session_id = self._record(pattern)
        print(f"🔄 Recorded Pattern: {title}")
        return session_id

    def record_protocol(
        self,
        title: str,
        description: str,
        steps: List[str],
        guardrails: List[str] = None,
        when_to_use: str = None,
        tags: List[str] = None,
        verified: bool = True
    ) -> str:
        """Record a development protocol."""
        protocol = Protocol(
            title=title,
            description=description,
            steps=steps,
            guardrails=guardrails or [],
            when_to_use=when_to_use or "",
            tags=tags or [],
            verified=verified
        )
        session_id = self._record(protocol)
        print(f"📋 Recorded Protocol: {title}")
        return session_id

    def record_bug_fix(
        self,
        title: str,
        symptom: str,
        root_cause: str,
        fix: str,
        tags: List[str] = None,
        file_path: str = None
    ) -> str:
        """Record a bug fix (delegates to Context DNA helper)."""
        session_id = self.memory.record_bug_fix(
            symptom=symptom,
            root_cause=root_cause,
            fix=fix,
            tags=tags,
            file_path=file_path
        )

        # Update local cache
        self.cache["learnings"].append({
            "title": title,
            "type": "bug_fix",
            "tags": tags or [],
            "session_id": session_id,
            "created_at": datetime.now().isoformat()
        })
        self.cache["stats"]["bug_fix"] += 1
        self._save_cache()

        print(f"🐛 Recorded Bug Fix: {title}")
        return session_id

    def record_performance(
        self,
        title: str,
        metric: str,
        before: str,
        after: str,
        technique: str,
        tags: List[str] = None,
        file_path: str = None
    ) -> str:
        """Record a performance optimization."""
        session_id = self.memory.record_performance_lesson(
            metric=metric,
            before=before,
            after=after,
            technique=technique,
            file_path=file_path,
            tags=tags
        )

        # Update local cache
        self.cache["learnings"].append({
            "title": title,
            "type": "performance",
            "tags": tags or [],
            "session_id": session_id,
            "created_at": datetime.now().isoformat()
        })
        self.cache["stats"]["performance"] += 1
        self._save_cache()

        print(f"⚡ Recorded Performance: {title}")
        return session_id

    # ==========================================================================
    # PUBLIC API - Query Learnings
    # ==========================================================================

    def get_learnings(
        self,
        query: str,
        learning_type: LearningType = None,
        limit: int = 5
    ) -> List[dict]:
        """
        Get relevant learnings for a query.

        Args:
            query: What to search for
            learning_type: Optional filter by type (sop, gotcha, etc.)
            limit: Max results

        Returns:
            List of relevant learnings
        """
        # Build query with type filter if specified
        search_query = query
        if learning_type:
            search_query = f"{learning_type.value} {query}"

        return self.memory.get_relevant_learnings(search_query, limit=limit)

    def get_sops(self, query: str, limit: int = 5) -> List[dict]:
        """Get SOPs relevant to query."""
        return self.get_learnings(f"sop procedure {query}", limit=limit)

    def get_gotchas(self, query: str, limit: int = 5) -> List[dict]:
        """Get gotchas/warnings relevant to query."""
        return self.get_learnings(f"gotcha warning avoid {query}", limit=limit)

    def get_patterns(self, query: str, limit: int = 5) -> List[dict]:
        """Get patterns relevant to query."""
        return self.get_learnings(f"pattern {query}", limit=limit)

    def get_protocols(self, query: str, limit: int = 5) -> List[dict]:
        """Get protocols relevant to query."""
        return self.get_learnings(f"protocol workflow {query}", limit=limit)

    def get_stats(self) -> dict:
        """Get registry statistics."""
        return {
            "total_learnings": len(self.cache["learnings"]),
            "by_type": self.cache["stats"],
            "recent": self.cache["learnings"][-5:]
        }


# =============================================================================
# AUTO-EXTRACTION FROM SUCCESS PATTERNS
# =============================================================================

def auto_extract_sop_from_success(
    task: str,
    commands_used: List[str],
    observations: List[str],
    tags: List[str] = None
) -> str:
    """
    Automatically create an SOP from a successful task.

    Call this after completing infrastructure work successfully.
    The system extracts the pattern and records it as an SOP.

    Uses 6-ZONE FORMAT for bug-fix SOPs:
    Zone 1: [HEART]     - Descriptive core title (preserved, not shortened)
    Zone 2: bad_sign    - Observable symptom
    Zone 3: (antecedent)- Contributing factors
    Zone 4: fix         - Treatment action
    Zone 5: (stack)     - Tools involved
    Zone 6: outcome     - Desired state

    Args:
        task: What was accomplished
        commands_used: Commands that were run
        observations: Any observations made
        tags: Keywords for search

    Returns:
        Session ID of recorded SOP
    """
    registry = SOPRegistry()

    # Build steps from commands
    steps = []
    for cmd in commands_used:
        # Clean up command for readability
        if cmd.startswith("cd "):
            steps.append(f"Navigate to directory: {cmd[3:]}")
        elif "git " in cmd:
            steps.append(f"Git: {cmd}")
        elif "systemctl" in cmd:
            steps.append(f"Service management: {cmd}")
        elif "docker" in cmd:
            steps.append(f"Docker: {cmd}")
        elif "terraform" in cmd:
            steps.append(f"Terraform: {cmd}")
        elif "aws " in cmd:
            steps.append(f"AWS CLI: {cmd}")
        else:
            steps.append(cmd)

    # Extract warnings from observations
    warnings = []
    regular_obs = []
    for obs in observations:
        obs_lower = obs.lower()
        if any(w in obs_lower for w in ["warning", "careful", "must", "don't", "never", "always", "critical"]):
            warnings.append(obs)
        else:
            regular_obs.append(obs)

    # Add observations as final steps
    if regular_obs:
        steps.extend([f"Note: {obs}" for obs in regular_obs[:3]])

    # === ENHANCED TITLE GENERATION ===
    # Try bug-fix enhancer first, fall back to process enhancer
    context = f"{task}. Steps: {', '.join(steps[:3])}. Observations: {', '.join(observations[:2])}"

    if BUGFIX_ENHANCER_AVAILABLE:
        enhanced_title = generate_bugfix_sop_title(task, context)
        # Handle None return (not a bug-fix SOP)
        if enhanced_title is None and PROCESS_ENHANCER_AVAILABLE:
            # Use process SOP enhancer for chain format
            enhanced_title = generate_process_sop_title(task, context)
        elif enhanced_title is None:
            # Fallback if no process enhancer
            enhanced_title = f"[process SOP] {task}"
        # Ensure we preserve the descriptive heart - if enhanced title is too short, keep original
        elif len(enhanced_title) < len(task) and ':' not in task:
            enhanced_title = f"{task}: {enhanced_title.split(': ', 1)[-1] if ': ' in enhanced_title else enhanced_title}"
    elif PROCESS_ENHANCER_AVAILABLE:
        # No bug-fix enhancer, try process enhancer
        enhanced_title = generate_process_sop_title(task, context)
        if enhanced_title is None:
            enhanced_title = f"SOP: {task}"
    else:
        enhanced_title = f"SOP: {task}"

    return registry.record_sop(
        title=enhanced_title,
        steps=steps,
        warnings=warnings,
        tags=tags or [],
        verified=True
    )


def auto_extract_gotcha_from_error(
    error: str,
    what_caused_it: str,
    how_fixed: str,
    severity: str = "medium",
    tags: List[str] = None
) -> str:
    """
    Automatically create a gotcha from an error resolution.

    Call this after fixing a non-trivial error (>5 min to diagnose).

    Uses 6-ZONE FORMAT for gotcha titles:
    Zone 1: [HEART]     - Descriptive error summary (preserved, not shortened)
    Zone 2: bad_sign    - Observable symptom/error
    Zone 3: (antecedent)- What caused it
    Zone 4: fix         - How it was resolved
    Zone 5: (stack)     - Tools/context involved
    Zone 6: outcome     - Desired state achieved

    Args:
        error: The error that occurred
        what_caused_it: What triggered the error
        how_fixed: How it was resolved
        severity: low/medium/high/critical
        tags: Keywords for search

    Returns:
        Session ID of recorded gotcha
    """
    registry = SOPRegistry()

    # === ENHANCED TITLE GENERATION (6-zone format) ===
    # Gotchas are inherently bug-fix related, so use bugfix enhancer
    error_first_line = error.split('\n')[0][:80]
    if BUGFIX_ENHANCER_AVAILABLE:
        # Combine error info for richer title generation
        context = f"Error: {error}. Cause: {what_caused_it}. Fix: {how_fixed}"
        enhanced_title = generate_bugfix_sop_title(error, context)
        # Handle None return (unlikely for gotchas since they're bug-fix by nature)
        if enhanced_title is None:
            enhanced_title = f"[bug-fix SOP] Gotcha: {error_first_line}"
        # Ensure we preserve the descriptive heart - if enhanced title is generic, keep error info
        elif len(enhanced_title) < 40 or enhanced_title.lower().startswith('gotcha'):
            if ':' in enhanced_title:
                zones_part = enhanced_title.split(': ', 1)[-1]
                enhanced_title = f"{error_first_line}: {zones_part}"
            else:
                enhanced_title = f"{error_first_line}: {enhanced_title}"
    else:
        # Fallback to simple title
        enhanced_title = f"Gotcha: {error_first_line}"

    return registry.record_gotcha(
        title=enhanced_title,
        when_it_happens=what_caused_it,
        consequence=error,
        solution=how_fixed,
        severity=severity,
        tags=tags or [],
        verified=True
    )


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("SOP Types - Typed Learning System for Architecture Brain")
        print("")
        print("Commands:")
        print("  stats                          - Show learning statistics")
        print("  search <query>                 - Search all learnings")
        print("  gotchas <query>               - Search gotchas specifically")
        print("  sops <query>                  - Search SOPs specifically")
        print("")
        print("Recording (usually done programmatically):")
        print("  record-gotcha <title> <when> <consequence> <solution> [tags...]")
        print("  record-sop <title> [steps separated by |] [tags...]")
        print("")
        print("Examples:")
        print("  python sop_types.py stats")
        print("  python sop_types.py gotchas 'docker ecs'")
        print("  python sop_types.py search 'terraform deployment'")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "stats":
        registry = SOPRegistry()
        stats = registry.get_stats()
        print("=== SOP Registry Statistics ===")
        print(f"Total learnings: {stats['total_learnings']}")
        print("\nBy type:")
        for t, count in stats['by_type'].items():
            print(f"  {t}: {count}")
        if stats['recent']:
            print("\nRecent learnings:")
            for l in stats['recent']:
                print(f"  [{l['type']}] {l['title']}")

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: search <query>")
            sys.exit(1)
        registry = SOPRegistry()
        query = " ".join(sys.argv[2:])
        results = registry.get_learnings(query, limit=5)
        print(f"\n=== Search Results for: {query} ===\n")
        for i, r in enumerate(results, 1):
            print(f"{i}. {r.get('title', 'No title')}")
            print(f"   Type: {r.get('type', 'unknown')}")
            print(f"   Relevance: {1 - r.get('distance', 1):.0%}")
            if r.get('preferences'):
                print(f"   Preview: {r['preferences'][:200]}...")
            print()

    elif cmd == "gotchas":
        if len(sys.argv) < 3:
            print("Usage: gotchas <query>")
            sys.exit(1)
        registry = SOPRegistry()
        query = " ".join(sys.argv[2:])
        results = registry.get_gotchas(query, limit=5)
        print(f"\n=== Gotchas for: {query} ===\n")
        for i, r in enumerate(results, 1):
            print(f"{i}. {r.get('title', 'No title')}")
            if r.get('preferences'):
                print(f"   {r['preferences'][:300]}...")
            print()

    elif cmd == "sops":
        if len(sys.argv) < 3:
            print("Usage: sops <query>")
            sys.exit(1)
        registry = SOPRegistry()
        query = " ".join(sys.argv[2:])
        results = registry.get_sops(query, limit=5)
        print(f"\n=== SOPs for: {query} ===\n")
        for i, r in enumerate(results, 1):
            print(f"{i}. {r.get('title', 'No title')}")
            if r.get('preferences'):
                print(f"   {r['preferences'][:300]}...")
            print()

    elif cmd == "record-gotcha":
        if len(sys.argv) < 6:
            print("Usage: record-gotcha <title> <when> <consequence> <solution> [tags...]")
            sys.exit(1)
        registry = SOPRegistry()
        title = sys.argv[2]
        when = sys.argv[3]
        consequence = sys.argv[4]
        solution = sys.argv[5]
        tags = sys.argv[6:] if len(sys.argv) > 6 else []
        registry.record_gotcha(
            title=title,
            when_it_happens=when,
            consequence=consequence,
            solution=solution,
            tags=tags
        )

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
