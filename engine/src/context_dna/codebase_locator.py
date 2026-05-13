#!/usr/bin/env python3
"""
CODEBASE LOCATOR - Contextual File Path Suggestions

Provides "where to consider looking" breadcrumbs based on prompt context.
These are SUGGESTIONS, not guarantees - the index may be incomplete or outdated.

Output format (injected into hooks):
━━━ WHERE TO CONSIDER LOOKING ━━━
View 1: memory/ → brain.py → win(), success()
View 2: memory/ → sop_title_router.py → generate_sop_title() [MAIN ENTRY]
View 3: scripts/ → auto-memory-query.sh

Usage:
    from memory.codebase_locator import get_location_suggestions
    suggestions = get_location_suggestions("enhance SOP titles with better formatting")
    # Returns list of breadcrumb strings

CLI:
    python memory/codebase_locator.py "your prompt here"
"""

import sys
import re
from pathlib import Path
from typing import List, Dict, Set, Tuple

# =============================================================================
# SEMANTIC SYNONYMS - Expand keyword matching
# =============================================================================
#
# Maps semantic concepts to their keyword equivalents.
# "containerize" → matches "docker" entries
# "performance" → matches "async" entries
#
# This enables semantic understanding without full NLP.

SEMANTIC_SYNONYMS: Dict[str, List[str]] = {
    # Container concepts → docker
    "containerize": ["docker", "compose"],
    "container": ["docker", "ecs"],
    "orchestrate": ["docker", "compose", "ecs"],

    # Performance concepts → async
    "performance": ["async", "boto3", "llm"],
    "slow": ["async", "boto3"],
    "latency": ["async", "llm", "boto3"],
    "concurrency": ["async", "llm"],
    "blocking": ["async", "boto3"],

    # Infrastructure concepts
    "infrastructure": ["terraform", "aws", "ecs"],
    "infra": ["terraform", "aws", "docker"],
    "cloud": ["aws", "terraform"],
    "server": ["aws", "ec2", "ecs", "backend"],
    "network": ["nlb", "vpc", "aws"],

    # Voice/AI concepts
    "ai": ["llm", "voice", "bedrock"],
    "speech": ["voice", "tts", "stt"],
    "transcription": ["stt", "voice"],
    "synthesis": ["tts", "voice"],

    # Learning/Memory concepts
    "learning": ["memory", "brain", "sop"],
    "remember": ["memory", "sop", "brain"],
    "knowledge": ["memory", "architecture", "graph"],
    "wisdom": ["professor", "memory"],

    # Code organization
    "test": ["hook", "script"],
    "automation": ["hook", "script"],
    "trigger": ["hook"],
    "cli": ["script"],
}

# =============================================================================
# CODEBASE MAP - Concept → File Path Breadcrumbs
# =============================================================================
#
# Format: "keyword": ["breadcrumb1", "breadcrumb2", ...]
# Breadcrumb format: "dir/ → file.py → function()" or "dir/ → file.py"
#
# MAINTENANCE: Update this when adding new files or moving code.
# This is intentionally semi-static - better to be slightly stale than complex.

CODEBASE_MAP: Dict[str, List[str]] = {
    # === MEMORY SYSTEM ===
    "sop": [
        "memory/ → sop_title_router.py → generate_sop_title() [MAIN ENTRY]",
        "memory/ → bugfix_sop_enhancer.py → generate_bugfix_sop_title()",
        "memory/ → process_sop_enhancer.py → generate_process_sop_title()",
        "memory/ → brain.py → success()",
        "memory/ → context_dna_client.py → record_sop()",
    ],
    "title": [
        "memory/ → sop_title_router.py → generate_sop_title() [MAIN ENTRY]",
        "memory/ → bugfix_sop_enhancer.py → generate_bugfix_sop_title()",
        "memory/ → process_sop_enhancer.py → generate_process_sop_title()",
    ],
    "enhance": [
        "memory/ → sop_title_router.py (routes to appropriate enhancer)",
        "memory/ → bugfix_sop_enhancer.py (bug-fix SOPs)",
        "memory/ → process_sop_enhancer.py (process SOPs)",
        "memory/ → architecture_enhancer.py",
    ],
    "process": [
        "memory/ → process_sop_enhancer.py → generate_process_sop_title()",
        "memory/ → process_sop_enhancer.py → extract_process_zones()",
    ],
    "router": [
        "memory/ → sop_title_router.py → generate_sop_title() [MAIN ENTRY]",
        "memory/ → sop_title_router.py → study_sop_type() (scoring-based)",
        "memory/ → sop_title_router.py → detect_format_state()",
    ],
    "bugfix": [
        "memory/ → bugfix_sop_enhancer.py → generate_bugfix_sop_title()",
        "memory/ → bugfix_sop_enhancer.py → extract_bugfix_zones()",
    ],
    "brain": [
        "memory/ → brain.py → win(), success(), fix()",
        "memory/ → brain.py → init(), cycle()",
        "memory/ → brain_state.md (auto-generated)",
    ],
    "capture": [
        "memory/ → brain.py → win()",
        "scripts/ → auto-capture-results.sh",
        "memory/ → auto_capture.py → capture_success()",
    ],
    "memory": [
        "memory/ → brain.py (orchestrator)",
        "memory/ → context_dna_client.py (API client)",
        "memory/ → query.py (CLI queries)",
    ],
    "query": [
        "memory/ → query.py",
        "memory/ → professor.py (distilled wisdom)",
        "memory/ → context.py (full blueprint)",
    ],
    "professor": [
        "memory/ → professor.py",
    ],
    "pattern": [
        "memory/ → pattern_manager.py",
        "memory/ → pattern_evolution.py",
        "context-dna/ → core/ → src/ → context_dna/ → pattern_evolution.py",
    ],
    "dedup": [
        "memory/ → bugfix_sop_enhancer.py → SOPDeduplicator",
        "memory/ → dedup_detector.py",
        "context-dna/ → core/ → src/ → context_dna/ → dedup_detector.py",
    ],

    # === HOOKS ===
    "hook": [
        "scripts/ → auto-memory-query.sh (UserPromptSubmit)",
        "scripts/ → auto-capture-results.sh (PostToolUse)",
        "scripts/ → auto-session-summary.sh (Stop)",
        ".claude/ → settings.local.json → hooks config",
    ],
    "prompt": [
        "scripts/ → auto-memory-query.sh",
        "memory/ → codebase_locator.py (this file)",
    ],
    "session": [
        "memory/ → session_logger.py",
        "scripts/ → auto-session-summary.sh",
    ],
    "review": [
        "memory/ → hook_review_agent.py",
        "scripts/ → hook-review-checkin.sh",
    ],
    "evolution": [
        "memory/ → hook_evolution.py",
        "memory/ → pattern_evolution.py",
    ],

    # === ARCHITECTURE ===
    "architecture": [
        "memory/ → architecture.py → ArchitectureMemory",
        "memory/ → architecture_enhancer.py",
        "memory/ → knowledge_graph.py",
    ],
    "knowledge": [
        "memory/ → knowledge_graph.py → KnowledgeGraph",
        "memory/ → knowledge_graph.py → categorize()",
    ],
    "graph": [
        "memory/ → knowledge_graph.py",
    ],
    "artifact": [
        "memory/ → artifact_store.py",
        "memory/ → bugfix_sop_enhancer.py → ArtifactDeduplicator",
    ],
    "context": [
        "memory/ → context.py (full blueprint)",
        "memory/ → auto_context.py",
        "memory/ → context_dna_client.py",
    ],

    # === CONTEXT DNA INFRASTRUCTURE ===
    "docker": [
        "context-dna/ → infra/ → docker-compose.yaml",
        "context-dna/ → infra/ → resource-profiles.yaml",
    ],
    "compose": [
        "context-dna/ → infra/ → docker-compose.yaml",
    ],
    "contextdna": [
        "context-dna/ → core/ → src/ → context_dna/ (Python package)",
        "context-dna/ → infra/ (Docker runtime)",
        "scripts/ → context-dna (CLI)",
    ],
    "acontext": [
        "memory/ → context_dna_client.py → AcontextClient",
        "context-dna/ → infra/ → docker-compose.yaml → contextdna-*",
    ],

    # === SCRIPTS ===
    "script": [
        "scripts/ → context-dna (main CLI)",
        "scripts/ → auto-*.sh (hooks)",
        "scripts/ → install-context-dna.sh",
    ],
    "install": [
        "scripts/ → install-context-dna.sh",
    ],
    "deploy": [
        "scripts/ → deploy-landing.sh",
        "infra/ → aws/ → terraform/",
    ],

    # === VOICE STACK ===
    "voice": [
        "ersim-voice-stack/ → services/ → agent/",
        "ersim-voice-stack/ → services/ → llm/",
    ],
    "llm": [
        "ersim-voice-stack/ → services/ → llm/ → app/ → main.py",
        "memory/ → knowledge_graph.py → Voice_Pipeline/LLM",
    ],
    "async": [
        "ersim-voice-stack/ → services/ → llm/ → app/ → main.py",
        "memory/ → (SOP) blocking → asyncio.to_thread",
    ],
    "boto3": [
        "ersim-voice-stack/ → services/ → llm/ → app/ → main.py",
        "memory/ → (SOP) async boto3 → asyncio.to_thread",
    ],

    # === INFRASTRUCTURE ===
    "terraform": [
        "infra/ → aws/ → terraform/ → main.tf",
        "infra/ → aws/ → terraform-livekit/",
    ],
    "aws": [
        "infra/ → aws/ → terraform/",
        "memory/ → knowledge_graph.py → Infrastructure/AWS/*",
    ],
    "ecs": [
        "infra/ → aws/ → terraform/ → main.tf",
        "memory/ → (SOP) ECS health checking",
    ],
    "nlb": [
        "infra/ → aws/ → terraform/ → nlb-internal.tf",
    ],

    # === BACKEND ===
    "backend": [
        "backend/ → ersim_backend/",
        "backend/ → ersim_backend/ → settings/ → base.py",
    ],
    "django": [
        "backend/ → ersim_backend/",
        "memory/ → (SOP) gunicorn systemctl",
    ],

    # === FRONTEND ===
    "landing": [
        "landing-page/ (submodule)",
        "scripts/ → deploy-landing.sh",
    ],
    "dashboard": [
        "context-dna/ → dashboard/ (v0 submodule)",
    ],
    "xbar": [
        "context-dna/ → core/ → clients/ → xbar/",
    ],

    # === CONFIG ===
    "config": [
        ".claude/ → settings.local.json",
        "context-dna/ → infra/ → .env",
        "CLAUDE.md",
    ],
    "settings": [
        ".claude/ → settings.local.json",
        "backend/ → ersim_backend/ → settings/",
    ],
    "env": [
        "context-dna/ → infra/ → .env",
        ".env (root)",
    ],

    # === GIT ===
    "git": [
        ".gitmodules (submodules)",
        "CLAUDE.md → git commit protocol",
    ],
    "submodule": [
        ".gitmodules",
        "landing-page/ (submodule)",
        "context-dna/ → dashboard/ (submodule)",
    ],
    "commit": [
        "CLAUDE.md → git commit protocol",
        "memory/ → auto_learn.py (post-commit)",
    ],
}

# =============================================================================
# COMPOUND KEYWORDS - Multi-word concepts
# =============================================================================

COMPOUND_KEYWORDS: Dict[str, List[str]] = {
    "sop title": [
        "memory/ → sop_title_router.py → generate_sop_title() [MAIN ENTRY]",
        "memory/ → bugfix_sop_enhancer.py → generate_bugfix_sop_title()",
        "memory/ → process_sop_enhancer.py → generate_process_sop_title()",
    ],
    "three zone": [
        "memory/ → bugfix_sop_enhancer.py → order_descriptors_arrows()",
    ],
    "arrow format": [
        "memory/ → bugfix_sop_enhancer.py → order_descriptors_arrows()",
    ],
    "memory system": [
        "memory/ → brain.py (orchestrator)",
        "memory/ → context_dna_client.py",
        "context-dna/ → infra/ → docker-compose.yaml",
    ],
    "context dna": [
        "context-dna/ → core/ (Python package)",
        "context-dna/ → infra/ (Docker runtime)",
        "scripts/ → context-dna",
    ],
    "hook review": [
        "memory/ → hook_review_agent.py",
        "scripts/ → hook-review-checkin.sh",
    ],
    "knowledge graph": [
        "memory/ → knowledge_graph.py",
    ],
    "file path": [
        "memory/ → codebase_locator.py (this file)",
    ],
    "where to look": [
        "memory/ → codebase_locator.py (this file)",
    ],
    "voice stack": [
        "ersim-voice-stack/ → services/",
    ],
    "event loop": [
        "ersim-voice-stack/ → services/ → llm/ → app/ → main.py",
        "memory/ → (SOP) blocking → asyncio.to_thread",
    ],
}

# =============================================================================
# FILE-SPECIFIC LEARNINGS - Hard-won lessons for specific files
# =============================================================================
#
# When these files are read, surface the associated warnings/learnings.
# Format: "file_path_pattern": ["learning1", "learning2", ...]
#
# These are the "if you touch this file, remember THIS" notes.

FILE_LEARNINGS: Dict[str, List[str]] = {
    # Voice stack - critical async patterns
    "ersim-voice-stack/services/llm/app/main.py": [
        "⚠️ ASYNC: boto3/bedrock calls BLOCK event loop → wrap in asyncio.to_thread()",
        "⚠️ ASYNC: soundfile read/write is synchronous → wrap in asyncio.to_thread()",
        "💡 Non-streaming bedrock is FASTER than streaming for single requests",
    ],
    "ersim-voice-stack/services/agent": [
        "⚠️ Dockerfile needs HOME=/root for Python packages",
        "💡 Check requirements.txt for version conflicts",
    ],

    # Infrastructure - terraform gotchas
    "infra/aws/terraform/main.tf": [
        "⚠️ ECS tasks need execution_role_arn AND task_role_arn",
        "⚠️ CloudWatch log group must exist before ECS task starts",
        "💡 Use terraform plan before apply - always",
    ],
    "infra/aws/terraform/nlb-internal.tf": [
        "⚠️ NLB idle timeout is 350s default - may close WebSocket",
        "💡 For WebSocket: set idle_timeout to 3600 or use keepalive",
    ],

    # Context DNA
    "context-dna/infra/docker-compose.yaml": [
        "⚠️ docker restart doesn't reload env vars → must docker compose down/up",
        "💡 Check resource-profiles.yaml for memory limits",
    ],
    "memory/bugfix_sop_enhancer.py": [
        "💡 generate_bugfix_sop_title() has 7 candidates - V7 is arrow format",
        "💡 PROBLEM_WORDS go to Zone 1 (symptom) in arrow format",
        "💡 Returns None for process SOPs → falls back to process_sop_enhancer",
    ],
    "memory/process_sop_enhancer.py": [
        "💡 Chain format: via (tools) → step1 → step2 → ✓ verification",
        "💡 Goal heart preserved from task (simpler than bug-fix heart)",
        "💡 Returns None for bug-fix SOPs → falls back to bugfix_sop_enhancer",
    ],
    "memory/sop_title_router.py": [
        "💡 MAIN ENTRY POINT for SOP title generation",
        "💡 Study-first approach: analyze → detect format → route to creator/enhancer",
        "💡 Scoring-based type detection (not keyword forcing)",
        "💡 Separated: create_*_title() vs enhance_*_title() functions",
    ],
    "memory/brain.py": [
        "💡 brain.py success() triggers SOP extraction to Context DNA",
        "💡 Use win() for quick captures, success() for detailed ones",
    ],

    # Backend Django
    "backend/ersim_backend": [
        "⚠️ After code changes: sudo systemctl restart gunicorn",
        "💡 Logs at: journalctl -u gunicorn -f",
    ],
}

# =============================================================================
# NEVER DO LIST - Critical prohibitions
# =============================================================================
#
# These are ABSOLUTE PROHIBITIONS - actions that have caused major issues.
# They should be surfaced prominently in hook output.
#
# Format: {"keyword": ["🚫 NEVER: description"]}

NEVER_DO: Dict[str, List[str]] = {
    # Docker
    "docker": [
        "🚫 NEVER: docker restart to reload env vars - must docker compose down/up",
        "🚫 NEVER: docker rm -f running containers without checking volume mounts",
    ],
    "container": [
        "🚫 NEVER: docker restart to reload env vars - must docker compose down/up",
    ],

    # Git
    "git": [
        "🚫 NEVER: git push --force to main/master",
        "🚫 NEVER: git reset --hard without stash or backup",
        "🚫 NEVER: commit .env files or secrets",
    ],
    "commit": [
        "🚫 NEVER: commit .env files or secrets",
        "🚫 NEVER: --no-verify to skip pre-commit hooks without explicit user request",
    ],

    # AWS/Infrastructure
    "terraform": [
        "🚫 NEVER: terraform destroy in production without explicit confirmation",
        "🚫 NEVER: skip terraform plan before apply",
    ],
    "aws": [
        "🚫 NEVER: hardcode AWS credentials in code",
        "🚫 NEVER: use root account credentials",
    ],
    "ecs": [
        "🚫 NEVER: delete ECS services without checking for dependent load balancers",
    ],

    # Async Python
    "async": [
        "🚫 NEVER: call synchronous boto3/soundfile in async without asyncio.to_thread()",
        "🚫 NEVER: block the event loop with sync I/O in async code",
    ],
    "boto3": [
        "🚫 NEVER: call boto3 directly in async functions - wrap in asyncio.to_thread()",
    ],
    "asyncio": [
        "🚫 NEVER: call synchronous I/O in asyncio event loop",
    ],

    # Database
    "database": [
        "🚫 NEVER: DROP TABLE without backup",
        "🚫 NEVER: ALTER TABLE in production without testing in staging",
    ],
    "postgres": [
        "🚫 NEVER: DROP TABLE without backup",
    ],

    # WebRTC/Networking
    "cloudflare": [
        "🚫 NEVER: proxy WebRTC/UDP traffic through Cloudflare - set proxied=false",
    ],
    "webrtc": [
        "🚫 NEVER: route WebRTC through Cloudflare proxy - breaks UDP",
    ],
}


# =============================================================================
# DEPENDENCY GRAPH - Architectural wiring (A → B → C)
# =============================================================================
#
# Shows how components are connected. When touching A, know it affects B and C.
# Format: "component": {"depends_on": [...], "triggers": [...], "notes": "..."}

DEPENDENCY_GRAPH: Dict[str, Dict] = {
    # Voice Stack
    "llm_service": {
        "path": "ersim-voice-stack/services/llm/",
        "depends_on": ["bedrock_api", "asyncio_event_loop"],
        "triggers": ["voice_agent", "tts_service"],
        "notes": "LLM responses feed TTS, must not block event loop",
    },
    "voice_agent": {
        "path": "ersim-voice-stack/services/agent/",
        "depends_on": ["llm_service", "stt_service", "livekit"],
        "triggers": ["patient_simulation"],
        "notes": "Orchestrates voice pipeline, sensitive to latency",
    },

    # Context DNA
    "brain": {
        "path": "memory/brain.py",
        "depends_on": ["context_dna_api", "work_log"],
        "triggers": ["sop_extraction", "pattern_detection"],
        "notes": "Master orchestrator for learning capture",
    },
    "bugfix_sop_enhancer": {
        "path": "memory/bugfix_sop_enhancer.py",
        "depends_on": ["context_dna_db", "process_sop_enhancer"],
        "triggers": ["enhanced_bugfix_titles", "deduplication"],
        "notes": "6-zone format for bug-fix SOPs, falls back to process enhancer",
    },
    "process_sop_enhancer": {
        "path": "memory/process_sop_enhancer.py",
        "depends_on": ["context_dna_db"],
        "triggers": ["enhanced_process_titles"],
        "notes": "Chain format for process SOPs: via (tools) → steps → verification",
    },
    "sop_title_router": {
        "path": "memory/sop_title_router.py",
        "depends_on": ["bugfix_sop_enhancer", "process_sop_enhancer"],
        "triggers": ["sop_title_creation", "sop_title_enhancement"],
        "notes": "Main entry point: studies content → routes to creator or enhancer",
    },
    "hook_system": {
        "path": "scripts/auto-memory-query.sh",
        "depends_on": ["professor", "brain", "codebase_locator"],
        "triggers": ["context_injection", "success_capture"],
        "notes": "All memory context flows through this",
    },

    # Infrastructure
    "terraform_main": {
        "path": "infra/aws/terraform/main.tf",
        "depends_on": ["aws_credentials", "vpc_config"],
        "triggers": ["ecs_services", "load_balancers", "security_groups"],
        "notes": "Core infrastructure - changes cascade to all services",
    },
    "docker_compose": {
        "path": "context-dna/infra/docker-compose.yaml",
        "depends_on": ["docker_daemon", "network_ports"],
        "triggers": ["contextdna_services", "postgres_db", "opensearch"],
        "notes": "Local dev infrastructure - env vars require full restart",
    },
}


def get_dependency_context(file_path: str) -> str:
    """Get dependency graph context for a file."""
    normalized = file_path.replace('$HOME/dev/er-simulator-superrepo/', '')
    normalized = normalized.replace('$HOME/Documents/er-simulator-superrepo/', '')
    normalized = normalized.lstrip('./')

    for component, info in DEPENDENCY_GRAPH.items():
        if info['path'] in normalized or normalized.startswith(info['path']):
            depends = ' → '.join(info['depends_on']) if info['depends_on'] else 'none'
            triggers = ' → '.join(info['triggers']) if info['triggers'] else 'none'

            return f"""━━━ DEPENDENCY GRAPH: {component} ━━━
  Depends on: {depends}
  Triggers: {triggers}
  ⚠️ {info['notes']}
"""
    return ""


def get_never_do_warnings(prompt: str) -> List[str]:
    """Get NEVER DO warnings relevant to the prompt."""
    warnings = []
    prompt_lower = prompt.lower()

    for keyword, prohibitions in NEVER_DO.items():
        if keyword in prompt_lower:
            for prohibition in prohibitions:
                if prohibition not in warnings:
                    warnings.append(prohibition)

    return warnings


def format_never_do(prompt: str) -> str:
    """Format NEVER DO warnings for hook output."""
    warnings = get_never_do_warnings(prompt)
    if not warnings:
        return ""

    lines = ["🚨 CRITICAL PROHIBITIONS 🚨"]
    for warning in warnings[:5]:  # Max 5 warnings
        lines.append(warning)
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# LOCATOR FUNCTIONS
# =============================================================================

def extract_keywords(prompt: str) -> Set[str]:
    """Extract relevant keywords from a prompt, including semantic synonyms."""
    # Normalize
    prompt_lower = prompt.lower()

    keywords = set()

    # Check compound keywords first (higher priority)
    for compound in COMPOUND_KEYWORDS:
        if compound in prompt_lower:
            keywords.add(f"__compound__{compound}")

    # Extract single words
    words = re.findall(r'\b[a-z]+\b', prompt_lower)
    for word in words:
        # Direct match
        if word in CODEBASE_MAP and len(word) > 2:
            keywords.add(word)

        # Semantic synonym expansion
        # "containerize" → adds "docker", "compose" to keywords
        if word in SEMANTIC_SYNONYMS:
            for synonym_keyword in SEMANTIC_SYNONYMS[word]:
                if synonym_keyword in CODEBASE_MAP:
                    keywords.add(synonym_keyword)

    return keywords


def get_location_suggestions(prompt: str, max_views: int = 3) -> List[str]:
    """
    Get file path suggestions based on prompt context.

    Args:
        prompt: The user's prompt text
        max_views: Maximum number of views to return (default 3)

    Returns:
        List of breadcrumb strings, most relevant first
    """
    keywords = extract_keywords(prompt)

    if not keywords:
        return []

    # Collect all breadcrumbs with scores
    breadcrumb_scores: Dict[str, int] = {}

    for kw in keywords:
        if kw.startswith("__compound__"):
            # Compound keyword - higher weight
            compound = kw.replace("__compound__", "")
            for breadcrumb in COMPOUND_KEYWORDS.get(compound, []):
                breadcrumb_scores[breadcrumb] = breadcrumb_scores.get(breadcrumb, 0) + 3
        else:
            # Single keyword
            for breadcrumb in CODEBASE_MAP.get(kw, []):
                breadcrumb_scores[breadcrumb] = breadcrumb_scores.get(breadcrumb, 0) + 1

    # Sort by score (descending) and return top N
    sorted_breadcrumbs = sorted(
        breadcrumb_scores.items(),
        key=lambda x: (-x[1], x[0])  # Score desc, then alphabetical
    )

    return [bc for bc, score in sorted_breadcrumbs[:max_views]]


def format_for_hook(suggestions: List[str]) -> str:
    """
    Format suggestions for hook injection.

    Returns empty string if no suggestions (don't clutter output).
    """
    if not suggestions:
        return ""

    lines = ["━━━ WHERE TO CONSIDER LOOKING ━━━"]
    for i, suggestion in enumerate(suggestions, 1):
        lines.append(f"View {i}: {suggestion}")
    lines.append("")  # Trailing newline

    return "\n".join(lines)


def get_file_learnings(file_path: str) -> List[str]:
    """
    Get learnings specific to a file.

    Args:
        file_path: Path to the file being read

    Returns:
        List of learning strings for that file
    """
    learnings = []

    # Normalize path (remove leading ./ or absolute path components)
    normalized = file_path.replace('$HOME/dev/er-simulator-superrepo/', '')
    normalized = normalized.replace('$HOME/Documents/er-simulator-superrepo/', '')
    normalized = normalized.lstrip('./')

    # Check for exact match first
    if normalized in FILE_LEARNINGS:
        learnings.extend(FILE_LEARNINGS[normalized])

    # Check for partial matches (directory-level learnings)
    for pattern, file_learnings in FILE_LEARNINGS.items():
        if pattern in normalized or normalized.startswith(pattern):
            for learning in file_learnings:
                if learning not in learnings:
                    learnings.append(learning)

    return learnings


def format_file_learnings(file_path: str) -> str:
    """
    Format file-specific learnings for display.

    Returns empty string if no learnings.
    """
    learnings = get_file_learnings(file_path)
    if not learnings:
        return ""

    lines = [f"━━━ LEARNINGS FOR THIS FILE ━━━"]
    for learning in learnings:
        lines.append(learning)
    lines.append("")

    return "\n".join(lines)


def get_hook_output(prompt: str) -> str:
    """
    Main entry point for hook integration.

    Returns formatted string ready to inject, or empty if no suggestions.
    """
    suggestions = get_location_suggestions(prompt)
    return format_for_hook(suggestions)


# =============================================================================
# CLI
# =============================================================================

def main():
    if len(sys.argv) < 2:
        print("Codebase Locator - Contextual file path suggestions")
        print("")
        print("Usage:")
        print('  python codebase_locator.py "your prompt here"')
        print("")
        print("Examples:")
        print('  python codebase_locator.py "enhance SOP titles"')
        print('  python codebase_locator.py "fix async blocking in LLM"')
        print('  python codebase_locator.py "configure docker compose"')
        print("")
        print(f"Keywords indexed: {len(CODEBASE_MAP)}")
        print(f"Compound phrases: {len(COMPOUND_KEYWORDS)}")
        sys.exit(0)

    prompt = " ".join(sys.argv[1:])

    print(f"Prompt: {prompt}")
    print("")

    keywords = extract_keywords(prompt)
    print(f"Keywords found: {keywords}")
    print("")

    output = get_hook_output(prompt)
    if output:
        print(output)
    else:
        print("No location suggestions for this prompt.")


if __name__ == "__main__":
    main()
