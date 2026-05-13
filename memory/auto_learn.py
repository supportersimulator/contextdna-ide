#!/usr/bin/env python3
"""
Automatic Learning from Git Commits and Task Completion

This module automatically detects successful work patterns and records them
to Context DNA without requiring manual intervention.

The learning triggers are:
1. Git commits with specific patterns (fix, feat, perf, refactor)
2. Successful test runs
3. Deployment completions
4. PR merges

Usage:
    # After a git commit, automatically record if it's a learning-worthy fix
    python memory/auto_learn.py git-commit

    # After tests pass
    python memory/auto_learn.py test-success "pytest passed all 47 tests"

    # Manual trigger with description
    python memory/auto_learn.py record-success "Fixed async boto3 blocking" "Wrapped in to_thread"

For Git Hooks (post-commit):
    Add to .git/hooks/post-commit:
    #!/bin/bash
    python memory/auto_learn.py git-commit
"""

import sys
import os
import subprocess
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False

try:
    from memory.artifact_store import ArtifactStore, is_infrastructure_file, sanitize_secrets
    ARTIFACT_STORE_AVAILABLE = True
except ImportError:
    ARTIFACT_STORE_AVAILABLE = False

try:
    from memory.sandbox_verify import SandboxVerifier
    SANDBOX_VERIFY_AVAILABLE = True
except ImportError:
    SANDBOX_VERIFY_AVAILABLE = False

try:
    from memory.knowledge_graph import KnowledgeGraph, detect_config_file, get_current_git_info
    KNOWLEDGE_GRAPH_AVAILABLE = True
except ImportError:
    KNOWLEDGE_GRAPH_AVAILABLE = False


# Config file patterns for tracking
CONFIG_PATTERNS = {
    ".env": "environment",
    "*.tf": "terraform",
    "*.tfvars": "terraform",
    "docker-compose": "docker",
    "dockerfile": "docker",  # Case-insensitive matching
    "*settings*.py": "django",
    "CLAUDE.md": "atlas-instructions",
    "package.json": "node-deps",
    "requirements": "python-deps",
    "tsconfig": "typescript",
}


def is_config_file(filepath: str) -> tuple[bool, str]:
    """Check if a file is a tracked configuration file.

    Returns: (is_config, config_type)
    """
    import fnmatch
    filename = os.path.basename(filepath)

    for pattern, config_type in CONFIG_PATTERNS.items():
        if fnmatch.fnmatch(filename, pattern) or pattern in filename.lower():
            return True, config_type

    return False, ""


def run_cmd(cmd: str) -> Tuple[str, int]:
    """Run a shell command and return (output, exit_code)."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return result.stdout.strip(), result.returncode
    except Exception as e:
        return str(e), 1


def get_latest_commit() -> dict:
    """Get details of the latest git commit."""
    # Get commit message
    msg, _ = run_cmd("git log -1 --pretty=format:'%s'")

    # Get commit body (extended description)
    body, _ = run_cmd("git log -1 --pretty=format:'%b'")

    # Get files changed
    files, _ = run_cmd("git log -1 --name-only --pretty=format:''")

    # Get author
    author, _ = run_cmd("git log -1 --pretty=format:'%an'")

    # Get hash
    hash_short, _ = run_cmd("git log -1 --pretty=format:'%h'")

    return {
        "message": msg,
        "body": body,
        "files": [f for f in files.split("\n") if f],
        "author": author,
        "hash": hash_short
    }


def should_learn_from_commit(commit: dict) -> Tuple[bool, str, str]:
    """
    Determine if a commit represents a learning-worthy pattern.

    Returns: (should_learn, learning_type, keywords)
    """
    msg = commit["message"].lower()
    files = commit.get("files", [])
    files_str = " ".join(files).lower()

    # INFRASTRUCTURE commits - always learn (captures architecture automatically)
    infra_patterns = [
        "infra/", "terraform", ".tf", "dockerfile", "docker-compose",
        ".github/workflows/", "ecs", "kubernetes", "k8s", "nginx",
        "systemd", "gunicorn", ".service", "cloudflare", "livekit",
        "lambda", "deploy", "scripts/deploy"
    ]
    if any(pattern in files_str for pattern in infra_patterns):
        return True, "architecture", "infrastructure deployment config"

    # Infrastructure keywords in message
    infra_keywords = ["deploy", "infra", "config", "terraform", "docker", "ecs", "lambda"]
    if any(kw in msg for kw in infra_keywords):
        return True, "architecture", "infrastructure deployment config"

    # Bug fixes - very learning-worthy
    if msg.startswith("fix:") or msg.startswith("fix(") or "fix" in msg:
        return True, "bug_fix", "fix bug resolve"

    # Performance improvements - learning-worthy
    if msg.startswith("perf:") or msg.startswith("perf(") or "performance" in msg or "optimize" in msg:
        return True, "performance", "performance optimize speed"

    # New features with significant logic
    if msg.startswith("feat:") or msg.startswith("feat("):
        # Only learn from features that touch critical areas
        critical_areas = ["async", "boto3", "livekit", "tts", "stt", "llm", "docker", "ecs", "lambda"]
        if any(area in files_str for area in critical_areas):
            return True, "feature", "feature implement add"
        return False, "", ""

    # Refactoring - sometimes learning-worthy
    if msg.startswith("refactor:") or msg.startswith("refactor("):
        if "async" in msg or "performance" in msg or "clean" in msg:
            return True, "refactor", "refactor restructure improve"

    # Architecture changes
    if "architecture" in msg or "migrate" in msg or "restructure" in msg:
        return True, "architecture", "architecture decision migrate"

    return False, "", ""


def extract_learning_from_commit(commit: dict, learning_type: str) -> dict:
    """Extract structured learning from a commit."""
    msg = commit["message"]
    body = commit["body"]
    files = commit["files"]

    # Extract what was done (the fix/feature)
    action = msg.split(":", 1)[-1].strip() if ":" in msg else msg

    # Try to extract root cause from body
    root_cause = ""
    if body:
        # Look for common patterns
        for pattern in ["because", "cause:", "root cause:", "issue was", "problem was"]:
            if pattern in body.lower():
                idx = body.lower().find(pattern)
                root_cause = body[idx:idx+200].strip()
                break
        if not root_cause:
            root_cause = body[:200].strip()

    # Determine affected area from files
    areas = set()
    for f in files:
        f_lower = f.lower()
        if "async" in f_lower or "main.py" in f_lower:
            areas.add("async")
        if "llm" in f_lower:
            areas.add("llm")
        if "tts" in f_lower:
            areas.add("tts")
        if "stt" in f_lower:
            areas.add("stt")
        if "docker" in f_lower or "ecs" in f_lower:
            areas.add("docker")
        if "lambda" in f_lower:
            areas.add("lambda")
        if "livekit" in f_lower or "agent" in f_lower:
            areas.add("livekit")
        if "terraform" in f_lower or "infra" in f_lower:
            areas.add("infrastructure")

    return {
        "type": learning_type,
        "action": action,
        "root_cause": root_cause,
        "files": files,
        "areas": list(areas),
        "hash": commit["hash"],
        "author": commit["author"]
    }


def get_file_content_at_commit(commit_hash: str, file_path: str) -> Optional[str]:
    """Get file content at a specific commit."""
    content, code = run_cmd(f"git show {commit_hash}:{file_path}")
    return content if code == 0 else None


def extract_artifacts_from_commit(commit: dict) -> dict[str, str]:
    """
    Extract infrastructure artifacts from a commit.

    Reads actual file contents for infrastructure files
    and returns them for storage.

    Args:
        commit: Commit dict with 'files' and 'hash'

    Returns:
        Dict of {file_path: content} for infrastructure files
    """
    artifacts = {}

    for file_path in commit.get("files", []):
        # Check if this is an infrastructure file
        if ARTIFACT_STORE_AVAILABLE and is_infrastructure_file(file_path):
            content = get_file_content_at_commit(commit["hash"], file_path)
            if content:
                artifacts[file_path] = content

    return artifacts


def verify_artifacts(artifacts: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """
    Verify artifacts before storage.

    Only verified artifacts are stored as SOPs.

    Args:
        artifacts: Dict of {file_path: content}

    Returns:
        (verified_artifacts, error_messages)
    """
    if not SANDBOX_VERIFY_AVAILABLE:
        # No verification available, pass all
        return artifacts, []

    verifier = SandboxVerifier()
    verified = {}
    errors = []

    for file_path, content in artifacts.items():
        result = verifier.verify_file(file_path, content)

        if result.success:
            verified[file_path] = content
            if result.warnings:
                for w in result.warnings:
                    print(f"   Warning in {file_path}: {w}")
        else:
            errors.append(f"{file_path}: {result.error}")
            print(f"   Verification failed for {file_path}: {result.error}")

    return verified, errors


def store_artifacts_for_session(session_id: str, artifacts: dict[str, str], area: str) -> Optional[str]:
    """
    Store verified artifacts in SeaweedFS.

    Args:
        session_id: Learning session ID
        artifacts: Dict of {file_path: content}
        area: Architecture area

    Returns:
        Disk ID if stored, None if failed
    """
    if not ARTIFACT_STORE_AVAILABLE or not artifacts:
        return None

    try:
        store = ArtifactStore()
        disk_id = store.store_with_artifacts(
            session_id=session_id,
            artifacts=artifacts,
            area=area,
            sanitize=True  # Always sanitize secrets
        )
        return disk_id
    except Exception as e:
        print(f"   Failed to store artifacts: {e}")
        return None


def categorize_learning(content: str) -> str:
    """
    Auto-categorize learning using knowledge graph.

    Args:
        content: Learning content

    Returns:
        Category path
    """
    if not KNOWLEDGE_GRAPH_AVAILABLE:
        return "general"

    try:
        kg = KnowledgeGraph()
        return kg.categorize(content)
    except Exception as e:
        print(f"[WARN] Knowledge graph categorize failed: {e}")
        return "general"


def record_commit_learning(commit: dict, learning: dict):
    """Record a learning from a git commit to Context DNA."""
    if not CONTEXT_DNA_AVAILABLE:
        print("Context DNA not available, skipping learning")
        return

    try:
        memory = ContextDNAClient()
    except Exception as e:
        print(f"Failed to connect to Context DNA: {e}")
        return

    # Extract and verify artifacts for infrastructure commits
    session_id = None
    disk_id = None

    if learning["type"] == "architecture" and commit.get("files"):
        artifacts = extract_artifacts_from_commit(commit)
        if artifacts:
            print(f"   Found {len(artifacts)} infrastructure artifacts")

            # Verify before storing
            verified, errors = verify_artifacts(artifacts)

            if errors:
                print(f"   {len(errors)} artifacts failed verification:")
                for e in errors[:3]:  # Show first 3 errors
                    print(f"      - {e}")

            if verified:
                # Determine area from files
                area = learning["areas"][0] if learning["areas"] else "infrastructure"

                # Store verified artifacts
                disk_id = store_artifacts_for_session(
                    session_id=commit["hash"][:16],
                    artifacts=verified,
                    area=area
                )

                if disk_id:
                    print(f"   Stored {len(verified)} artifacts to disk {disk_id}")

    # Categorize the learning
    category = categorize_learning(f"{learning['action']} {' '.join(learning['files'][:5])}")

    if learning["type"] == "bug_fix":
        session_id = memory.record_bug_fix(
            symptom=f"Issue fixed in commit {learning['hash']}",
            root_cause=learning["root_cause"] or "See commit message",
            fix=learning["action"],
            tags=learning["areas"] + ["git-commit", "auto-learned", category],
            file_path=learning["files"][0] if learning["files"] else None
        )
        print(f"✅ Bug fix recorded from commit {learning['hash']}")

    elif learning["type"] == "performance":
        session_id = memory.record_performance_lesson(
            metric=f"Optimization in {', '.join(learning['areas']) or 'code'}",
            before="Previous implementation",
            after="Optimized implementation",
            technique=learning["action"],
            tags=learning["areas"] + ["git-commit", "auto-learned", category]
        )
        print(f"✅ Performance lesson recorded from commit {learning['hash']}")

    elif learning["type"] == "architecture":
        # Include artifact info in the decision if available
        decision_content = learning["action"]
        if disk_id:
            decision_content += f"\n\n[Artifacts stored in disk: {disk_id}]"

        session_id = memory.record_architecture_decision(
            decision=decision_content,
            rationale=learning["root_cause"] or "See commit message",
            alternatives=None,
            consequences=f"Category: {category}"
        )
        print(f"✅ Architecture decision recorded from commit {learning['hash']}")
        if disk_id:
            print(f"   Artifacts linked: {disk_id}")

    elif learning["type"] in ("feature", "refactor"):
        # Use the agent success method
        session_id = memory.record_agent_success(
            task=learning["action"],
            approach=f"Implementation in files: {', '.join(learning['files'][:3])}",
            result="Successfully committed and working",
            agent_name=learning["author"].lower().replace(" ", "-"),
            tags=learning["areas"] + ["git-commit", "auto-learned", category]
        )
        print(f"✅ Feature/refactor recorded from commit {learning['hash']}")

    # Feed evidence pipeline (all commit types except chore/docs)
    if learning["type"] in ("bug_fix", "performance", "architecture", "feature", "refactor"):
        try:
            from memory.auto_capture import capture_success
            capture_success(
                task=learning["action"][:200],
                details=f"git commit {learning['hash'][:8]}",
                area=category or "infrastructure",
            )
        except Exception:
            pass  # Non-blocking


def process_git_commit():
    """Process the latest git commit for potential learning."""
    commit = get_latest_commit()

    print(f"Analyzing commit: {commit['hash']} - {commit['message'][:50]}...")

    # Check for config file changes (always track these)
    config_files = []
    for filepath in commit.get("files", []):
        is_config, config_type = is_config_file(filepath)
        if is_config:
            config_files.append((filepath, config_type))

    if config_files:
        print(f"   Found {len(config_files)} config file changes")
        record_config_change(commit, config_files)

    # Check for learning-worthy commit
    should_learn, learning_type, _ = should_learn_from_commit(commit)

    if not should_learn and not config_files:
        print(f"   Skipping: Not a learning-worthy commit type")
        return

    if should_learn:
        learning = extract_learning_from_commit(commit, learning_type)
        record_commit_learning(commit, learning)


def record_manual_success(description: str, approach: str, learnings: list = None):
    """Manually record a successful task completion."""
    if not CONTEXT_DNA_AVAILABLE:
        print("Context DNA not available")
        return

    try:
        memory = ContextDNAClient()
        memory.record_agent_success(
            task=description,
            approach=approach,
            result="Successfully completed",
            agent_name="atlas",
            tags=learnings or []
        )
        print(f"✅ Success recorded: {description}")
    except Exception as e:
        print(f"Failed to record: {e}")


def record_test_success(summary: str):
    """Record a successful test run."""
    if not CONTEXT_DNA_AVAILABLE:
        print("Context DNA not available")
        return

    try:
        memory = ContextDNAClient()
        memory.record_agent_success(
            task="Run tests",
            approach="Automated test execution",
            result=summary,
            agent_name="ci",
            tags=["tests", "validation", "auto-learned"]
        )
        print(f"✅ Test success recorded: {summary}")
    except Exception as e:
        print(f"Failed to record: {e}")


def record_config_change(commit: dict, config_files: list[tuple[str, str]]):
    """
    Record configuration file changes.

    This tracks important config changes for:
    - .env files (environment configuration)
    - *.tf files (terraform infrastructure)
    - docker-compose*.yml (Docker configuration)
    - settings*.py (Django settings)
    - CLAUDE.md (Atlas instructions)

    Args:
        commit: Commit dict with message, hash, etc.
        config_files: List of (filepath, config_type) tuples
    """
    if not CONTEXT_DNA_AVAILABLE or not config_files:
        return

    try:
        memory = ContextDNAClient()

        # Get git version info
        git_info = {}
        if KNOWLEDGE_GRAPH_AVAILABLE:
            try:
                git_info = get_current_git_info()
            except (subprocess.SubprocessError, OSError) as e:
                print(f"[WARN] Git info retrieval failed: {e}")
                git_info = {"commit": commit.get("hash", "unknown"), "branch": "unknown"}
        else:
            git_info = {"commit": commit.get("hash", "unknown"), "branch": "unknown"}

        # Group by config type
        by_type = {}
        for filepath, config_type in config_files:
            if config_type not in by_type:
                by_type[config_type] = []
            by_type[config_type].append(filepath)

        # Record each config type change
        for config_type, files in by_type.items():
            files_str = ", ".join(files[:5])

            # Get the diff for context (limited)
            diff_content = ""
            for f in files[:2]:  # Only show first 2 file diffs
                diff, _ = run_cmd(f"git show {commit['hash']} -- {f} | head -50")
                if diff:
                    diff_content += f"\n--- {f} ---\n{diff[:500]}"

            session_id = memory.record_architecture_decision(
                decision=f"Config change: {config_type} ({len(files)} files)",
                rationale=f"""Configuration files updated in commit {git_info.get('commit', 'unknown')}

Files: {files_str}

Commit message: {commit['message']}

Git info:
- Commit: {git_info.get('commit', 'unknown')}
- Branch: {git_info.get('branch', 'unknown')}
- Timestamp: {git_info.get('timestamp', 'unknown')}
{diff_content[:1000] if diff_content else ''}""",
                alternatives=None,
                consequences=f"Config type: {config_type}, Version: {git_info.get('commit', 'unknown')}"
            )

            print(f"📋 Config change recorded: {config_type} ({len(files)} files) @ {git_info.get('commit', 'unknown')[:8]}")

    except Exception as e:
        print(f"Failed to record config change: {e}")


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Auto-Learning CLI")
        print("")
        print("Commands:")
        print("  git-commit              - Analyze latest commit and record if worthy")
        print("  test-success <summary>  - Record successful test run")
        print("  record-success <desc> <approach> [tags...]  - Manual recording")
        print("")
        print("Examples:")
        print("  python auto_learn.py git-commit")
        print("  python auto_learn.py test-success 'All 47 tests passed'")
        print("  python auto_learn.py record-success 'Fixed async' 'Used to_thread' async boto3")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "git-commit":
        process_git_commit()

    elif cmd == "test-success":
        if len(sys.argv) < 3:
            print("Usage: test-success <summary>")
            sys.exit(1)
        record_test_success(" ".join(sys.argv[2:]))

    elif cmd == "record-success":
        if len(sys.argv) < 4:
            print("Usage: record-success <description> <approach> [tags...]")
            sys.exit(1)
        record_manual_success(
            sys.argv[2],
            sys.argv[3],
            sys.argv[4:] if len(sys.argv) > 4 else None
        )

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
