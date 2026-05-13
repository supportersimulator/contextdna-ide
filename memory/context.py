#!/usr/bin/env python3
"""
Unified Context Provider - Blueprints in Hand

THE SINGLE SOURCE OF TRUTH for agent context.

This module provides ONE function that gives agents EVERYTHING they need:
- Verified procedures (sandbox-tested SOPs)
- Executable artifacts (terraform, scripts, configs)
- Knowledge graph position (where this fits in architecture)
- Critical warnings (hard-won gotchas)
- Related patterns (similar past work)
- NO SECRETS (all sanitized)

Usage is DEAD SIMPLE:

    from memory.context import before_work, get_blueprint

    # Get ALL relevant context for what you're about to do
    context = before_work("configuring LiveKit TURN server")

    # Or get a full blueprint with artifacts
    blueprint = get_blueprint("deploy Django to production")
    print(blueprint.procedures)   # Verified SOPs
    print(blueprint.artifacts)    # Executable files
    print(blueprint.warnings)     # Critical gotchas

---

WHAT AGENTS RECEIVE:

┌─────────────────────────────────────────────────────────────────────┐
│                  ARCHITECTURE BLUEPRINT                              │
├─────────────────────────────────────────────────────────────────────┤
│  VERIFIED PROCEDURES (sandbox-tested):                              │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │ 1. LiveKit CPU Instance Setup (verified: 2024-01-15)          │ │
│  │    Steps: Create security group → Launch EC2 → Install Docker │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  EXECUTABLE ARTIFACTS:                                               │
│  ├── infra/aws/terraform/livekit.tf                                 │
│  ├── scripts/setup-livekit.sh                                       │
│  └── config/turnserver.conf                                         │
│                                                                      │
│  KNOWLEDGE GRAPH POSITION:                                           │
│  Infrastructure > AWS > EC2 > LiveKit                                │
│                                                                      │
│  CRITICAL WARNINGS:                                                  │
│  ⚠️ DNS must NOT be Cloudflare proxied (WebRTC needs direct IP)     │
│                                                                      │
│  🔒 SECRETS: All values sanitized. Use ${VAR} placeholders.         │
└─────────────────────────────────────────────────────────────────────┘

---

HOW LEARNING HAPPENS (100% AUTOMATIC):

1. Agent commits code with good message:
   git commit -m "fix: wrap boto3 in asyncio.to_thread for LLM service"

2. Post-commit hook runs auto_learn.py automatically

3. auto_learn.py:
   - Detects "fix:" prefix → learning-worthy
   - Extracts infrastructure artifacts
   - Verifies artifacts in sandbox
   - Stores verified artifacts in SeaweedFS
   - Records SOP to Context DNA

4. Next time ANY agent calls before_work("llm"):
   - Gets this learning + artifacts in the context
   - Has verified procedures ready to execute
   - Doesn't repeat the same mistake

NO MANUAL INTERVENTION. NO EXPLICIT RECORDING CALLS.
Just commit with good messages and query before working.
"""

import logging
import sys
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent))

# Optional imports
try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False

try:
    from memory.artifact_store import ArtifactStore
    ARTIFACT_STORE_AVAILABLE = True
except ImportError:
    ARTIFACT_STORE_AVAILABLE = False

try:
    from memory.knowledge_graph import KnowledgeGraph
    KNOWLEDGE_GRAPH_AVAILABLE = True
except ImportError:
    KNOWLEDGE_GRAPH_AVAILABLE = False

try:
    from memory.architecture import ArchitectureMemory
    ARCHITECTURE_AVAILABLE = True
except ImportError:
    ARCHITECTURE_AVAILABLE = False


# =============================================================================
# BLUEPRINT DATA STRUCTURE
# =============================================================================

@dataclass
class Procedure:
    """A verified procedure/SOP."""
    title: str
    content: str
    verified: bool = False
    verified_at: str = ""
    disk_id: str = ""
    distance: float = 1.0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "content": self.content,
            "verified": self.verified,
            "disk_id": self.disk_id
        }


@dataclass
class ArchitectureBlueprint:
    """
    Complete blueprint for any architecture work.

    This is what agents receive when calling get_blueprint().
    Contains everything needed to execute infrastructure work.
    """
    procedures: list = field(default_factory=list)      # Verified SOPs
    artifacts: dict = field(default_factory=dict)       # Executable files {path: content}
    hierarchy: list = field(default_factory=list)       # Position in knowledge graph
    warnings: list = field(default_factory=list)        # Critical gotchas
    related: list = field(default_factory=list)         # Similar patterns
    secrets_note: str = "All values sanitized. Use env vars for actual deployment."

    def format(self) -> str:
        """Format blueprint as readable text."""
        output = []
        output.append("=" * 70)
        output.append("                    ARCHITECTURE BLUEPRINT")
        output.append("=" * 70)
        output.append("")

        # Procedures
        if self.procedures:
            output.append("## VERIFIED PROCEDURES")
            for i, proc in enumerate(self.procedures, 1):
                status = "✓ verified" if proc.verified else "○ unverified"
                output.append(f"\n### {i}. {proc.title} ({status})")
                # Show first 500 chars of content
                content_preview = proc.content[:500] if len(proc.content) > 500 else proc.content
                output.append(content_preview)
            output.append("")

        # Artifacts
        if self.artifacts:
            output.append("## EXECUTABLE ARTIFACTS")
            for path, content in self.artifacts.items():
                output.append(f"\n### {path}")
                # Show first 300 chars
                output.append("```")
                output.append(content[:300] + "..." if len(content) > 300 else content)
                output.append("```")
            output.append("")

        # Hierarchy
        if self.hierarchy:
            output.append("## KNOWLEDGE GRAPH POSITION")
            output.append(" > ".join(self.hierarchy))
            output.append("")

        # Warnings
        if self.warnings:
            output.append("## CRITICAL WARNINGS")
            for warning in self.warnings:
                output.append(f"⚠️  {warning}")
            output.append("")

        # Related
        if self.related:
            output.append("## RELATED PATTERNS")
            for rel in self.related[:3]:
                output.append(f"  → {rel}")
            output.append("")

        # Secrets note
        output.append("-" * 70)
        output.append(f"🔒 SECRETS: {self.secrets_note}")
        output.append("=" * 70)

        return "\n".join(output)


# =============================================================================
# MAIN API
# =============================================================================

def before_work(task: str = None, file: str = None) -> str:
    """
    Get all relevant context before starting work.

    This is THE function agents should call before any significant work.
    It returns everything they need to know to avoid past mistakes and
    understand the architecture.

    Args:
        task: Description of what you're about to do
              e.g., "fixing LLM latency", "deploying Django", "configuring LiveKit"
        file: Path to file you're about to modify
              e.g., "ersim-voice-stack/services/llm/app/main.py"

    Returns:
        Formatted context string with all relevant knowledge

    Example:
        >>> context = before_work("updating GPU toggle Lambda")
        >>> print(context)
        # CONTEXT FOR: updating GPU toggle Lambda

        ## Critical Learnings:
        - GPU IP changes on ASG restart → use Internal NLB
        - ECS health check takes ~2 min to stabilize
        ...

        ## Architecture:
        - GPU services run on ECS (g5.xlarge)
        - Agent runs on CPU instance (NOT GPU!)
        ...
    """
    if not task and not file:
        return "# No task or file specified. Call with task='description' or file='path'.\n"

    # Detect relevant areas from task/file
    areas = _detect_areas(task, file)

    # Build unified context
    output = []

    query = task or file
    output.append(f"# CONTEXT FOR: {query}")
    output.append(f"# Detected areas: {', '.join(areas)}")
    output.append("")

    # Get learnings from Context DNA
    learnings = _get_learnings(areas, query)
    if learnings:
        output.append("## Relevant Learnings:")
        output.append(learnings)
        output.append("")

    # Get architecture context
    architecture = _get_architecture(areas, query)
    if architecture:
        output.append("## Architecture Context:")
        output.append(architecture)
        output.append("")

    # Get artifacts if available
    artifacts_info = _get_artifacts(areas)
    if artifacts_info:
        output.append("## Available Artifacts:")
        output.append(artifacts_info)
        output.append("")

    # Get hierarchy position
    hierarchy = _get_hierarchy_position(areas)
    if hierarchy:
        output.append("## Knowledge Graph Position:")
        output.append(hierarchy)
        output.append("")

    # Add area-specific warnings
    warnings = _get_warnings(areas)
    if warnings:
        output.append("## Critical Warnings:")
        output.append(warnings)
        output.append("")

    output.append("-" * 50)
    output.append("🔒 All values sanitized. Use env vars for actual deployment.")
    output.append("# END CONTEXT")

    return "\n".join(output)


def get_blueprint(task: str = None, file: str = None) -> ArchitectureBlueprint:
    """
    Get a full architecture blueprint with artifacts.

    This returns a structured object with all the components:
    - Verified procedures
    - Executable artifacts
    - Knowledge graph position
    - Warnings
    - Related patterns

    Args:
        task: Description of what you're about to do
        file: Path to file you're about to modify

    Returns:
        ArchitectureBlueprint with all components
    """
    blueprint = ArchitectureBlueprint()

    if not task and not file:
        return blueprint

    # Detect areas
    areas = _detect_areas(task, file)
    query = task or file

    # Get procedures
    blueprint.procedures = _get_procedures(areas, query)

    # Get artifacts
    blueprint.artifacts = _get_artifacts_dict(areas)

    # Get hierarchy
    blueprint.hierarchy = _get_hierarchy_list(areas)

    # Get warnings
    blueprint.warnings = _get_warnings_list(areas)

    # Get related patterns
    blueprint.related = _get_related(areas, query)

    return blueprint


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _detect_areas(task: str = None, file: str = None) -> list[str]:
    """Detect relevant knowledge areas from task/file.

    EXPANDED: Now detects ALL types of knowledge areas, not just infrastructure.
    This enables retrieval of ANY type of Context DNA win.
    """
    areas = []
    combined = ((task or "") + " " + (file or "")).lower()

    # EXPANDED: Covers ALL types of wins, not just infrastructure
    area_keywords = {
        # Infrastructure areas
        "async": ["async", "asyncio", "await", "to_thread", "event loop", "blocking", "concurrent"],
        "boto3": ["boto3", "bedrock", "aws sdk", "converse", "aws client"],
        "docker": ["docker", "container", "ecs", "dockerfile", "compose", "image"],
        "livekit": ["livekit", "webrtc", "rtc", "room", "participant", "turn", "stun"],
        "tts": ["tts", "kyutai", "moshi", "text-to-speech", "audio output", "speech synthesis"],
        "stt": ["stt", "whisper", "transcription", "speech-to-text", "audio input"],
        "llm": ["llm", "bedrock", "claude", "anthropic", "converse", "language model", "gpt", "ai"],
        "django": ["django", "gunicorn", "backend", "wsgi", "manage.py", "orm"],
        "terraform": ["terraform", ".tf", "infra/", "infrastructure", "hcl"],
        "lambda": ["lambda", "serverless", "gpu_toggle", "function", "invoke"],
        "gpu": ["gpu", "g5", "nvidia", "cuda", "inference", "graphics"],
        "networking": ["nlb", "dns", "cloudflare", "nginx", "proxy", "load balancer", "ssl", "tls"],
        "deployment": ["deploy", "restart", "update", "rollout", "release"],
        "ec2": ["ec2", "instance", "ami", "ssh", "server"],

        # Code/development areas (NEW)
        "python": ["python", "pip", "venv", "requirements", "pyproject", "poetry"],
        "react": ["react", "next", "nextjs", "typescript", "jsx", "tsx", "component"],
        "frontend": ["frontend", "ui", "css", "html", "tailwind", "styling"],
        "backend": ["api", "endpoint", "rest", "graphql", "server", "route"],
        "database": ["database", "sql", "postgres", "mysql", "mongodb", "supabase", "query"],
        "testing": ["test", "jest", "pytest", "spec", "unit test", "integration"],
        "git": ["git", "commit", "branch", "merge", "pr", "pull request"],

        # Memory/Architecture areas (NEW)
        "memory": ["acontext", "memory", "brain", "sop", "learning", "pattern"],
        "professor": ["professor", "wisdom", "guidance", "mental model", "landmine"],
        "architecture": ["architecture", "design", "structure", "system", "flow"],

        # Operational areas (NEW)
        "bugfix": ["fix", "bug", "error", "issue", "problem", "debug"],
        "performance": ["performance", "optimize", "speed", "latency", "fast"],
        "security": ["security", "auth", "token", "credential", "permission"],

        # Voice pipeline (specific)
        "voice": ["voice", "audio", "sound", "speech", "conversation"],
        "agent": ["agent", "assistant", "chat", "conversation", "dialogue"],
    }

    for area, keywords in area_keywords.items():
        if any(kw in combined for kw in keywords):
            areas.append(area)

    return areas if areas else ["general"]


def _get_learnings(areas: list[str], query: str) -> str:
    """Get relevant learnings from Context DNA."""
    if not CONTEXT_DNA_AVAILABLE:
        return ""

    try:
        memory = ContextDNAClient()

        if not memory.ping():
            return ""

        # Query for each area
        all_learnings = []
        seen = set()

        for area in areas[:3]:  # Limit to avoid too many queries
            learnings = memory.get_relevant_learnings(area, limit=3)
            for l in learnings:
                key = l.get('title', '')[:50]
                if key not in seen and l.get('distance', 1.0) < 0.6:
                    seen.add(key)
                    pref = l.get('preferences', '')
                    if pref:
                        # Extract key info
                        lines = pref.split('\n')[:5]
                        summary = '\n'.join(f"  {line}" for line in lines if line.strip())
                        if summary:
                            all_learnings.append(f"- **{l.get('title', 'Learning')}**\n{summary}")

        return "\n".join(all_learnings) if all_learnings else ""

    except Exception:
        return ""


def _get_architecture(areas: list[str], query: str) -> str:
    """Get architecture context."""
    if not ARCHITECTURE_AVAILABLE:
        if not CONTEXT_DNA_AVAILABLE:
            return ""
        # Fall back to direct Context DNA query
        try:
            memory = ContextDNAClient()
            if not memory.ping():
                return ""

            learnings = memory.get_relevant_learnings(f"architecture {query}", limit=3)
            architecture = []
            for l in learnings:
                if l.get('distance', 1.0) < 0.7:
                    pref = l.get('preferences', '')
                    if pref and 'architecture' in pref.lower():
                        lines = pref.split('\n')[:8]
                        summary = '\n'.join(f"  {line}" for line in lines if line.strip())
                        if summary:
                            architecture.append(summary)
            return "\n".join(architecture) if architecture else ""
        except Exception as e:
            logger.debug(f"Architecture context fallback failed: {e}")
            return ""

    try:
        arch = ArchitectureMemory()
        return arch.get_architecture_context(query, limit=3)
    except Exception as e:
        logger.debug(f"ArchitectureMemory unavailable: {e}")
        return ""


def _get_artifacts(areas: list[str]) -> str:
    """Get artifact info as formatted string."""
    if not ARTIFACT_STORE_AVAILABLE:
        return ""

    try:
        store = ArtifactStore()
        info = []

        for area in areas[:3]:
            artifacts = store.list_artifacts_by_area(area)
            for art in artifacts[:2]:  # Limit per area
                files = art.get('files', [])
                if files:
                    info.append(f"- [{area}] {', '.join(files[:3])}")

        return "\n".join(info) if info else ""
    except Exception as e:
        logger.debug(f"Artifact retrieval failed: {e}")
        return ""


def _get_artifacts_dict(areas: list[str]) -> dict[str, str]:
    """Get artifacts as dictionary."""
    if not ARTIFACT_STORE_AVAILABLE:
        return {}

    try:
        store = ArtifactStore()
        artifacts = {}

        for area in areas[:3]:
            area_artifacts = store.list_artifacts_by_area(area)
            for art in area_artifacts[:2]:
                disk_id = art.get('disk_id')
                if disk_id:
                    try:
                        disk_artifacts = store.get_procedure_artifacts(disk_id)
                        artifacts.update(disk_artifacts)
                    except Exception as e:
                        print(f"[WARN] Artifact retrieval for {disk_id} failed: {e}")

        return artifacts
    except Exception as e:
        # Artifacts available via API, not pip package - silence warning
        # Log at DEBUG level instead of printing to avoid noise in webhook output
        import logging
        logging.getLogger(__name__).debug(f"Artifact lookup skipped: {e}")
        return {}


def _get_hierarchy_position(areas: list[str]) -> str:
    """Get knowledge graph position as string."""
    if not KNOWLEDGE_GRAPH_AVAILABLE:
        return ""

    try:
        kg = KnowledgeGraph()
        positions = []

        for area in areas[:2]:
            category = kg.categorize(area)
            if category and category != "general":
                positions.append(f"  {area} → {category}")

        return "\n".join(positions) if positions else ""
    except Exception as e:
        logger.debug(f"Hierarchy position lookup failed: {e}")
        return ""


def _get_hierarchy_list(areas: list[str]) -> list[str]:
    """Get knowledge graph position as list."""
    if not KNOWLEDGE_GRAPH_AVAILABLE:
        return []

    try:
        kg = KnowledgeGraph()
        category = kg.categorize(" ".join(areas))
        return category.split("/") if category else []
    except Exception as e:
        logger.debug(f"Hierarchy list lookup failed: {e}")
        return []


def _get_procedures(areas: list[str], query: str) -> list[Procedure]:
    """Get verified procedures."""
    if not CONTEXT_DNA_AVAILABLE:
        return []

    try:
        memory = ContextDNAClient()
        if not memory.ping():
            return []

        procedures = []
        learnings = memory.get_relevant_learnings(f"procedure {query}", limit=5)

        for l in learnings:
            if l.get('distance', 1.0) < 0.7:
                pref = l.get('preferences', '')
                if pref:
                    # Check for artifact reference
                    disk_id = ""
                    if "[Artifacts stored in disk:" in pref:
                        try:
                            disk_id = pref.split("[Artifacts stored in disk:")[1].split("]")[0].strip()
                        except Exception as e:
                            print(f"[WARN] Disk ID extraction failed: {e}")

                    procedures.append(Procedure(
                        title=l.get('title', 'Procedure'),
                        content=pref,
                        verified="verified" in pref.lower() or "success" in pref.lower(),
                        disk_id=disk_id,
                        distance=l.get('distance', 1.0)
                    ))

        return procedures
    except Exception as e:
        logger.debug(f"Procedure retrieval failed: {e}")
        return []


def _get_warnings(areas: list[str]) -> str:
    """Get critical warnings as formatted string."""
    warnings = _get_warnings_list(areas)
    return "\n".join(f"- {w}" for w in warnings) if warnings else ""


def _get_warnings_list(areas: list[str]) -> list[str]:
    """Get critical warnings as list.

    EXPANDED: Now includes warnings for ALL types of areas, not just infrastructure.
    These are hard-won lessons that MUST be surfaced for any type of work.
    """
    # These are the hard-won lessons that MUST be surfaced
    warning_map = {
        # Infrastructure warnings
        "async": "boto3/whisper/soundfile are SYNC - wrap in asyncio.to_thread()",
        "docker": "docker restart does NOT reload env vars - must stop/rm/run",
        "livekit": "WebRTC requires direct IP - DNS must NOT be Cloudflare proxied",
        "gpu": "GPU IP changes on ASG restart - use Internal NLB, not direct IP",
        "tts": "KYUTAI_TTS_SAMPLE_RATE=24000 (not 48000)",
        "llm": "boto3 streaming is sync per-token - non-streaming with to_thread is faster",
        "networking": "WebRTC needs UDP ports 50000-60000 open",
        "django": "HOME=/root needed for git operations on EC2",
        "boto3": "All boto3 calls block asyncio - always use to_thread()",
        "ec2": "ASG-launched instances get new IPs - never hardcode IPs",
        "terraform": "Always run terraform plan before apply - check for destruction",
        "deployment": "Wait for health checks before declaring success (~2 min for ECS)",

        # Code/development warnings (NEW)
        "python": "Always use virtual environment - system Python can cause conflicts",
        "react": "useEffect cleanup is critical - prevent memory leaks on unmount",
        "frontend": "Test on multiple browsers - CSS behaves differently",
        "backend": "Validate ALL user input at API boundary - never trust client data",
        "database": "Always use parameterized queries - prevent SQL injection",
        "testing": "Test the failure path - happy path tests miss real bugs",
        "git": "Never force push to main - use feature branches and PRs",

        # Memory/Architecture warnings (NEW)
        "memory": "Query memory BEFORE starting work - don't repeat past mistakes",
        "professor": "Trust Professor guidance - it's distilled from hard-won experience",
        "architecture": "Read existing code FIRST - check if pattern already exists",

        # Operational warnings (NEW)
        "bugfix": "Fix root cause, not symptoms - understand complete flow first",
        "performance": "Profile before optimizing - premature optimization is the root of evil",
        "security": "Never log credentials or tokens - even in debug mode",

        # Voice pipeline warnings
        "voice": "Audio sample rates must match - 24000 for TTS, check STT input rate",
        "agent": "Handle conversation interrupts gracefully - voice AI needs special flow",
        "stt": "Whisper model loading is expensive - keep model loaded in memory",
    }

    relevant = []
    for area in areas:
        if area in warning_map:
            relevant.append(warning_map[area])

    # Also query Context DNA for dynamic gotchas
    if CONTEXT_DNA_AVAILABLE:
        try:
            memory = ContextDNAClient()
            if memory.ping():
                for area in areas[:2]:  # Limit queries
                    learnings = memory.get_relevant_learnings(f"gotcha warning {area}", limit=2)
                    for l in learnings:
                        if l.get('distance', 1.0) < 0.5:  # High relevance only
                            pref = l.get('preferences', '')
                            if pref and len(pref) < 200:
                                relevant.append(f"[{area}] {pref[:150]}")
        except Exception as e:
            print(f"[WARN] Gotcha/warning lookup failed: {e}")

    return relevant


def _get_related(areas: list[str], query: str) -> list[str]:
    """Get related patterns."""
    if not CONTEXT_DNA_AVAILABLE:
        return []

    try:
        memory = ContextDNAClient()
        if not memory.ping():
            return []

        related = []
        learnings = memory.get_relevant_learnings(query, limit=5)

        for l in learnings:
            if 0.5 < l.get('distance', 1.0) < 0.8:  # Related but not exact match
                title = l.get('title', '')
                if title:
                    related.append(title)

        return related[:3]
    except Exception as e:
        logger.debug(f"Related topics lookup failed: {e}")
        return []


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Unified Context Provider - Blueprints in Hand")
        print("")
        print("Usage:")
        print("  python context.py <task description>")
        print("  python context.py --file <filepath>")
        print("  python context.py --blueprint <task description>")
        print("")
        print("Examples:")
        print("  python context.py 'fixing LLM latency issues'")
        print("  python context.py --file ersim-voice-stack/services/llm/app/main.py")
        print("  python context.py --blueprint 'deploy Django to production'")
        print("")
        print("This provides ALL relevant context for the work you're about to do.")
        sys.exit(0)

    if sys.argv[1] == "--file":
        if len(sys.argv) < 3:
            print("Usage: --file <filepath>")
            sys.exit(1)
        print(before_work(file=sys.argv[2]))

    elif sys.argv[1] == "--blueprint":
        if len(sys.argv) < 3:
            print("Usage: --blueprint <task description>")
            sys.exit(1)
        task = " ".join(sys.argv[2:])
        blueprint = get_blueprint(task=task)
        print(blueprint.format())

    else:
        task = " ".join(sys.argv[1:])
        print(before_work(task=task))
