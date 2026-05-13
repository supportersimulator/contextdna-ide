#!/usr/bin/env python3
"""
Context DNA Celery Tasks - The Background Agents

These are the "subconscious" workers that run autonomously:
- Scanner: watches repo tree, detects changes, updates project map
- Distiller: turns work logs into skills/SOPs
- Relevance: updates context packs based on activity
- LLM Manager: coordinates with local LLM (Ollama)
- Brain: orchestrates consolidation cycles

PHILOSOPHY (from ChatGPT's Synaptic design):
"Think of Celery as the nervous system, not the brain.
 Agents = brains (scanner, distiller, relevance finder, LLM manager)
 Celery = scheduling, routing, backpressure, retries, isolation"

Each agent runs in its own worker process for isolation:
- Scanner crashing ≠ injector crashing
- Local LLM choking ≠ UI freezing
- Runaway loop won't lock your Electron app
"""

import os
import sys
import json
import logging
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import Celery app
from memory.celery_config import app

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('contextdna.tasks')

# =============================================================================
# CONSTANTS
# =============================================================================

MEMORY_DIR = Path(__file__).parent
PROJECT_ROOT = MEMORY_DIR.parent

# =============================================================================
# FAST-FAIL CELERY UTILITIES
# =============================================================================
# These utilities prevent webhook timeouts when Celery/Redis is unavailable.
# The problem: .delay() can block for 20+ seconds if Redis is down (retries).
# The solution: Check availability first, use fire-and-forget with timeout.

_celery_available = None  # Cached availability status
_celery_last_check = 0    # Timestamp of last check

def is_celery_available(force_check: bool = False) -> bool:
    """
    Check if Celery backend (Redis) is available.

    Caches result for 30 seconds to avoid repeated connection attempts.
    Used by webhook generation to avoid blocking on unavailable Celery.

    Args:
        force_check: If True, bypass cache and check immediately

    Returns:
        True if Celery is likely available, False otherwise
    """
    global _celery_available, _celery_last_check
    import time

    now = time.time()

    # Use cached result if checked within last 30 seconds
    if not force_check and _celery_available is not None and (now - _celery_last_check) < 30:
        return _celery_available

    # Try to ping Redis (Celery backend)
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client:
            # Use PING with short timeout
            client.ping()
            _celery_available = True
        else:
            _celery_available = False
    except Exception:
        _celery_available = False

    _celery_last_check = now
    return _celery_available


def fire_and_forget(task_func, *args, **kwargs) -> bool:
    """
    Call a Celery task without blocking if Celery is unavailable.

    This is a wrapper for .delay() that:
    1. Checks if Celery is available first (fast, cached)
    2. Uses apply_async with ignore_result=True (no waiting)
    3. Returns False immediately if Celery unavailable

    Args:
        task_func: The Celery task to call
        *args: Positional arguments for the task
        **kwargs: Keyword arguments for the task

    Returns:
        True if task was queued, False if Celery unavailable
    """
    if not is_celery_available():
        logger.debug(f"Celery unavailable, skipping task: {task_func.name}")
        return False

    try:
        # Fire and forget - don't wait for result
        task_func.apply_async(
            args=args,
            kwargs=kwargs,
            ignore_result=True,
            expires=300,  # Task expires after 5 minutes if not picked up
        )
        return True
    except Exception as e:
        logger.debug(f"Failed to queue task {task_func.name}: {e}")
        return False

# Redis keys for coordination
REDIS_KEY_PROJECT_FINGERPRINT = "contextdna:project:fingerprint"
REDIS_KEY_LAST_SCAN = "contextdna:scanner:last_scan"
REDIS_KEY_CONTEXT_PACK = "contextdna:context:pack:{project_id}"
REDIS_KEY_LLM_STATUS = "contextdna:llm:status"


# =============================================================================
# SCANNER AGENT TASKS
# =============================================================================
# "A background scanning agent that learns the hierarchy without annoying me"

@app.task(bind=True, name='memory.celery_tasks.scan_project')
def scan_project(self, project_path: str = None):
    """
    Scanner Agent: Watches repo tree, detects changes, updates project map.

    Runs every 30 seconds. Detects:
    - File tree changes
    - Git status changes
    - Dependency graph deltas
    - "Hot zones" (recently modified areas)

    Triggers hierarchy detection if significant changes found.
    """
    try:
        from memory.redis_cache import get_redis_client

        project_path = project_path or str(PROJECT_ROOT)
        logger.info(f"Scanner: Scanning {project_path}")

        # Calculate project fingerprint
        fingerprint = _calculate_project_fingerprint(project_path)

        # Check if fingerprint changed
        redis_client = get_redis_client()
        if redis_client:
            old_fingerprint = redis_client.get(REDIS_KEY_PROJECT_FINGERPRINT)
            # Handle both bytes (Python redis default) and str (redis-py with decode_responses=True)
            if old_fingerprint:
                old_fp_str = old_fingerprint.decode() if isinstance(old_fingerprint, bytes) else old_fingerprint
                if old_fp_str == fingerprint:
                    logger.debug("Scanner: No changes detected")
                    return {"status": "unchanged", "fingerprint": fingerprint}

            # Store new fingerprint (atomic batch, with TTL to match redis_cache.py)
            pipe = redis_client.pipeline()
            pipe.setex(REDIS_KEY_PROJECT_FINGERPRINT, 60, fingerprint)  # TTL_FINGERPRINT = 60s
            pipe.setex(REDIS_KEY_LAST_SCAN, 3600, datetime.utcnow().isoformat())  # 1h expiry
            pipe.execute()

        # Changes detected - trigger hierarchy detection
        logger.info("Scanner: Changes detected, triggering hierarchy detection")
        detect_hierarchy.delay(project_path)

        return {
            "status": "changed",
            "fingerprint": fingerprint,
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        logger.error(f"Scanner error: {e}")
        raise self.retry(exc=e, countdown=10)


@app.task(bind=True, name='memory.celery_tasks.detect_hierarchy')
def detect_hierarchy(self, project_path: str, force_rescan: bool = False):
    """
    Hierarchy Detector: Uses comprehensive HierarchyAnalyzer for deep codebase analysis.

    This is a Celery-wrapped version of the Adaptive Hierarchy Intelligence system.
    It performs:
    - Deep repo type detection (monorepo, submodules, polyrepo)
    - Service boundary detection (backend, frontend, infra, memory)
    - Framework detection (Django, Next.js, React, etc.)
    - Naming convention analysis
    - Config pattern detection
    - Platform detection (for LLM backend recommendations)

    Results are stored in PostgreSQL with version history and broadcast via Redis.
    """
    try:
        logger.info(f"Hierarchy Detector: Deep analysis of {project_path}")
        project_path_obj = Path(project_path)

        # Try to use the comprehensive HierarchyAnalyzer
        try:
            from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer
            from context_dna.setup.models import PlatformInfo

            # Run deep analysis
            analyzer = HierarchyAnalyzer(project_path_obj)
            profile = analyzer.analyze()

            # Add platform info
            profile.platform = PlatformInfo.detect()

            # Convert to dict for storage/broadcast
            profile_dict = profile.to_dict()

            # Also create legacy format for backward compatibility
            hierarchy = {
                "type": profile.repo_type.value,
                "boundaries": [
                    {"name": loc.path.split("/")[-1], "path": loc.path}
                    for loc in profile.locations.values()
                ],
                "stack_hints": list(set(
                    loc.framework or loc.category
                    for loc in profile.locations.values()
                    if loc.framework or loc.category
                )),
                "entrypoints": [],
                "timestamp": datetime.utcnow().isoformat(),
                "profile_id": profile.id,
                "profile_version": profile.version,
                "platform": {
                    "os": profile.platform.os if profile.platform else "unknown",
                    "arch": profile.platform.arch if profile.platform else "unknown",
                    "recommended_backend": (
                        profile.platform.recommended_backend.value
                        if profile.platform else "cloud"
                    ),
                }
            }

            logger.info(
                f"Hierarchy Detector: Analyzed {profile.repo_type.value} with "
                f"{len(profile.locations)} service locations"
            )

        except ImportError as e:
            logger.warning(f"HierarchyAnalyzer not available ({e}), using basic detection")
            # Fallback to basic detection
            profile_dict = None
            hierarchy = _basic_hierarchy_detection(project_path_obj)

        # Store in PostgreSQL with version history
        try:
            if profile_dict:
                # Use new hierarchy profile storage
                save_hierarchy_profile.delay(project_path, profile_dict)
            else:
                # Legacy storage
                from memory.postgres_storage import store_hierarchy
                store_hierarchy(str(project_path), hierarchy)
        except Exception as e:
            logger.warning(f"Could not store hierarchy in PostgreSQL: {e}")

        # Broadcast hierarchy update via Redis
        try:
            from memory.redis_cache import publish_event
            publish_event("hierarchy_updated", hierarchy)
        except Exception as e:
            logger.warning(f"Could not publish hierarchy event: {e}")

        return hierarchy

    except Exception as e:
        logger.error(f"Hierarchy detection error: {e}")
        raise self.retry(exc=e, countdown=30)


def _basic_hierarchy_detection(project_path: Path) -> dict:
    """Basic hierarchy detection fallback when HierarchyAnalyzer unavailable."""
    hierarchy = {
        "type": "unknown",
        "boundaries": [],
        "stack_hints": [],
        "entrypoints": [],
        "timestamp": datetime.utcnow().isoformat()
    }

    # Detect monorepo patterns
    if (project_path / "pnpm-workspace.yaml").exists():
        hierarchy["type"] = "pnpm_monorepo"
    elif (project_path / "lerna.json").exists():
        hierarchy["type"] = "lerna_monorepo"
    elif (project_path / "turbo.json").exists():
        hierarchy["type"] = "turbo_monorepo"
    elif (project_path / ".git").is_dir():
        if (project_path / ".gitmodules").exists():
            hierarchy["type"] = "superrepo_with_submodules"
        else:
            hierarchy["type"] = "single_repo"

    # Detect stack hints
    stack_files = {
        "package.json": "node", "requirements.txt": "python",
        "pyproject.toml": "python", "Cargo.toml": "rust",
        "go.mod": "go", "docker-compose.yaml": "docker",
        "docker-compose.yml": "docker", "main.tf": "terraform",
        "Dockerfile": "docker",
    }

    for filename, stack in stack_files.items():
        if list(project_path.rglob(filename)):
            hierarchy["stack_hints"].append(stack)

    # Detect boundaries
    for subdir in project_path.iterdir():
        if subdir.is_dir() and not subdir.name.startswith('.'):
            if any((subdir / f).exists() for f in ["package.json", "pyproject.toml", "Cargo.toml"]):
                hierarchy["boundaries"].append({
                    "name": subdir.name,
                    "path": str(subdir.relative_to(project_path))
                })

    return hierarchy


@app.task(bind=True, name='memory.celery_tasks.save_hierarchy_profile')
def save_hierarchy_profile(self, project_path: str, profile_dict: dict, notes: str = None):
    """
    Save hierarchy profile to PostgreSQL with version history.

    Automatically backs up current profile before saving new one.
    Uses the hierarchy_profiles table with full version tracking.
    """
    try:
        import hashlib
        from memory.postgres_storage import get_postgres_connection

        conn = get_postgres_connection()
        if not conn:
            logger.warning("PostgreSQL not available, skipping profile save")
            return {"status": "skipped", "reason": "no_connection"}

        # Generate machine ID from project path (deterministic)
        machine_id = hashlib.sha256(project_path.encode()).hexdigest()[:16]

        # Check if we have the save_hierarchy_profile function
        with conn.cursor() as cur:
            # First, check if the function exists
            cur.execute("""
                SELECT 1 FROM pg_proc WHERE proname = 'save_hierarchy_profile'
            """)
            if not cur.fetchone():
                # Function doesn't exist - use basic insert
                logger.warning("save_hierarchy_profile function not found, using basic insert")
                cur.execute("""
                    INSERT INTO hierarchy_profiles (machine_id, profile_version, profile_data, source, notes)
                    VALUES (%s, 1, %s, 'celery_task', %s)
                    ON CONFLICT (machine_id, is_active) WHERE is_active = TRUE
                    DO UPDATE SET profile_data = EXCLUDED.profile_data,
                                  profile_version = hierarchy_profiles.profile_version + 1,
                                  updated_at = NOW()
                    RETURNING id
                """, (machine_id, json.dumps(profile_dict), notes))
            else:
                # Use the proper versioning function
                cur.execute("""
                    SELECT save_hierarchy_profile(%s, %s, %s, %s)
                """, (machine_id, json.dumps(profile_dict), 'celery_task', notes))

            result = cur.fetchone()
            conn.commit()

        logger.info(f"Hierarchy profile saved for {project_path[:30]}... (machine_id: {machine_id[:8]}...)")
        return {"status": "saved", "machine_id": machine_id, "profile_id": str(result[0]) if result else None}

    except Exception as e:
        logger.error(f"Failed to save hierarchy profile: {e}")
        # Don't retry - not critical
        return {"status": "error", "error": str(e)}


# =============================================================================
# DISTILLER AGENT TASKS
# =============================================================================
# "Turn raw signals + mirrored conversations into Decisions, Conventions,
#  Known landmines, Golden paths / SOPs"

@app.task(bind=True, name='memory.celery_tasks.distill_skills')
def distill_skills(self, session_id: str = None):
    """
    Distiller Agent: Converts completed successful runs into reusable skills.

    This is what makes Acontext [ContextDNA] feel "intelligent":
    - Extracts patterns from successful work
    - Creates searchable SOP/skill blocks
    - Links skills to specific scopes/projects

    Runs every 30 minutes or triggered after user approval.
    """
    try:
        logger.info("Distiller: Starting skill extraction")

        # Read recent work dialogue log
        work_log_path = MEMORY_DIR / ".work_dialogue_log.jsonl"
        if not work_log_path.exists():
            logger.info("Distiller: No work log found")
            return {"status": "no_data", "skills_extracted": 0}

        # Get entries from last 4 hours
        cutoff = datetime.utcnow() - timedelta(hours=4)
        recent_entries = []

        with open(work_log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    entry_time = datetime.fromisoformat(entry.get("timestamp", "2000-01-01"))
                    if entry_time > cutoff:
                        recent_entries.append(entry)
                except (json.JSONDecodeError, ValueError, KeyError) as e:
                    logger.debug(f"Skipping malformed work log entry: {e}")
                    continue

        if not recent_entries:
            logger.info("Distiller: No recent entries")
            return {"status": "no_recent_data", "skills_extracted": 0}

        # Look for success patterns
        skills_extracted = 0
        for entry in recent_entries:
            content = entry.get("content", "")
            source = entry.get("source", "")

            # Detect success indicators
            success_indicators = ["success", "worked", "fixed", "deployed", "completed"]
            if any(ind in content.lower() for ind in success_indicators):
                # Extract skill
                skill = {
                    "title": f"Skill from {source}",
                    "content": content[:500],
                    "tags": _extract_tags(content),
                    "timestamp": entry.get("timestamp"),
                    "confidence": 0.7
                }

                # Store skill
                try:
                    from memory.postgres_storage import store_skill
                    store_skill(skill)
                    skills_extracted += 1
                except Exception as e:
                    logger.warning(f"Could not store skill: {e}")

        logger.info(f"Distiller: Extracted {skills_extracted} skills")
        return {"status": "success", "skills_extracted": skills_extracted}

    except Exception as e:
        logger.error(f"Distiller error: {e}")
        raise self.retry(exc=e, countdown=120)


@app.task(bind=True, name='memory.celery_tasks.consolidate_patterns')
def consolidate_patterns(self):
    """
    Pattern Consolidator: Runs the brain's consolidation cycle.

    This is the "memory consolidation" phase - like sleep for the brain:
    - Merges similar patterns
    - Prunes stale data
    - Generates insights
    - Updates brain_state.md
    """
    try:
        logger.info("Pattern Consolidator: Starting consolidation")

        # Try to use the brain module
        try:
            from memory.brain import brain
            result = brain.run_cycle()
            logger.info(f"Pattern Consolidator: Cycle complete - {result}")
            return {"status": "success", "result": str(result)}
        except ImportError:
            logger.warning("Brain module not available")
            return {"status": "brain_unavailable"}

    except Exception as e:
        logger.error(f"Pattern consolidation error: {e}")
        raise self.retry(exc=e, countdown=300)


# =============================================================================
# RELEVANCE AGENT TASKS
# =============================================================================
# "An agent who continually finds relevant based on what I'm typing"

@app.task(bind=True, name='memory.celery_tasks.refresh_relevance')
def refresh_relevance(self, project_id: str = None):
    """
    Relevance Agent: Updates context packs based on recent activity.

    Builds the "injection payload" that gets sent to Claude Code:
    - Current task state
    - Constraints + preferences
    - Repo map (ultra short)
    - Recent decisions + next steps
    - Relevant SOP blocks

    Runs every 2 minutes or triggered by significant activity.
    """
    try:
        logger.info("Relevance Agent: Refreshing context packs")

        project_id = project_id or "default"

        # Build context pack
        context_pack = {
            "project_id": project_id,
            "timestamp": datetime.utcnow().isoformat(),
            "sections": {}
        }

        # Get recent patterns from brain
        try:
            from memory.brain import brain
            patterns = brain.state.get("active_patterns", [])
            context_pack["sections"]["patterns"] = patterns[:10]
        except Exception as e:
            logger.debug(f"Brain patterns unavailable: {e}")
            context_pack["sections"]["patterns"] = []

        # Get recent insights
        try:
            from memory.brain import brain
            insights = brain.state.get("insights_generated", [])
            context_pack["sections"]["insights"] = insights[:5]
        except Exception as e:
            logger.debug(f"Brain insights unavailable: {e}")
            context_pack["sections"]["insights"] = []

        # Get relevant SOPs (top 5 most relevant)
        try:
            from memory.persistent_hook_structure import get_foundation_sops
            sop_result = get_foundation_sops("", 5)
            context_pack["sections"]["sops"] = [
                {"title": s.title, "type": s.sop_type}
                for s in sop_result.sops
            ]
        except Exception as e:
            logger.debug(f"SOPs unavailable: {e}")
            context_pack["sections"]["sops"] = []

        # Cache in Redis
        try:
            from memory.redis_cache import cache_context_pack
            cache_context_pack(project_id, context_pack)
        except Exception as e:
            logger.warning(f"Could not cache context pack: {e}")

        logger.info(f"Relevance Agent: Context pack updated for {project_id}")
        return context_pack

    except Exception as e:
        logger.error(f"Relevance refresh error: {e}")
        raise self.retry(exc=e, countdown=60)


@app.task(bind=True, name='memory.celery_tasks.update_context_pack')
def update_context_pack(self, project_id: str, prompt: str):
    """
    Update context pack based on a specific prompt.

    Called when user types something - updates the "hot" context
    to be maximally relevant to what they're about to do.
    """
    try:
        logger.info(f"Updating context pack for prompt: {prompt[:50]}...")

        # Use persistent_hook_structure to generate relevant context
        from memory.persistent_hook_structure import generate_context_injection

        result = generate_context_injection(prompt, mode="hybrid")

        # Cache the result
        try:
            from memory.redis_cache import cache_context_pack
            cache_context_pack(project_id, {
                "prompt": prompt,
                "injection": result.content,
                "sections": result.sections_included,
                "timestamp": datetime.utcnow().isoformat()
            })
        except Exception as e:
            print(f"[WARN] Context pack caching failed: {e}")

        return {
            "status": "success",
            "sections": result.sections_included,
            "volume_tier": result.volume_tier
        }

    except Exception as e:
        logger.error(f"Context pack update error: {e}")
        return {"status": "error", "error": str(e)}


# =============================================================================
# LLM MANAGER TASKS
# =============================================================================
# "Agents for helping manage the local LLM"

@app.task(bind=True, name='memory.celery_tasks.llm_health_check')
def llm_health_check(self):
    """
    LLM Manager: Verify Ollama is responsive and models are loaded.

    Runs every 5 minutes to ensure local LLM is available:
    - Checks Ollama API health
    - Verifies default model is loaded
    - Warms up model cache if needed
    """
    try:
        import httpx

        ollama_url = os.environ.get('OLLAMA_URL', 'http://localhost:11434')

        # Check Ollama health
        try:
            response = httpx.get(f"{ollama_url}/api/tags", timeout=10)
            if response.status_code == 200:
                models = response.json().get("models", [])
                model_names = [m.get("name", "") for m in models]

                status = {
                    "status": "healthy",
                    "models": model_names,
                    "timestamp": datetime.utcnow().isoformat()
                }

                # Cache status in Redis
                try:
                    from memory.redis_cache import get_redis_client
                    redis = get_redis_client()
                    if redis:
                        redis.setex(REDIS_KEY_LLM_STATUS, 600, json.dumps(status))
                except Exception as e:
                    print(f"[WARN] Redis LLM status cache failed: {e}")

                logger.info(f"LLM Health: OK - {len(models)} models available")
                return status
            else:
                logger.warning(f"LLM Health: Unhealthy - status {response.status_code}")
                return {"status": "unhealthy", "error": f"HTTP {response.status_code}"}

        except httpx.RequestError as e:
            logger.warning(f"LLM Health: Unreachable - {e}")
            return {"status": "unreachable", "error": str(e)}

    except Exception as e:
        logger.error(f"LLM health check error: {e}")
        return {"status": "error", "error": str(e)}


@app.task(bind=True, name='memory.celery_tasks.llm_analyze')
def llm_analyze(self, text: str, analysis_type: str = "summary"):
    """
    LLM Analyzer: Use local LLM for analysis tasks.

    Types:
    - summary: Summarize text
    - extract_skills: Extract skills/SOPs from text
    - detect_success: Detect success patterns
    - generate_insight: Generate insight from patterns
    """
    try:
        import httpx

        ollama_url = os.environ.get('OLLAMA_URL', 'http://localhost:11434')
        model = os.environ.get('OLLAMA_MODEL', 'qwen2.5:3b')

        prompts = {
            "summary": f"Summarize the following in 2-3 sentences:\n\n{text}",
            "extract_skills": f"Extract any learnable skills or procedures from:\n\n{text}\n\nFormat as bullet points.",
            "detect_success": f"Did this text indicate success or failure? Explain briefly:\n\n{text}",
            "generate_insight": f"What insight can be derived from this pattern?\n\n{text}"
        }

        prompt = prompts.get(analysis_type, prompts["summary"])

        response = httpx.post(
            f"{ollama_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=60
        )

        if response.status_code == 200:
            result = response.json().get("response", "")
            return {
                "status": "success",
                "analysis_type": analysis_type,
                "result": result
            }
        else:
            return {"status": "error", "error": f"HTTP {response.status_code}"}

    except Exception as e:
        logger.error(f"LLM analyze error: {e}")
        raise self.retry(exc=e, countdown=30)


# =============================================================================
# BRAIN ORCHESTRATION TASKS
# =============================================================================
# The conscious coordination layer

@app.task(bind=True, name='memory.celery_tasks.brain_cycle')
def brain_cycle(self):
    """
    Brain Cycle: Run the main consolidation cycle.

    This is the orchestrator that:
    - Processes work log
    - Extracts patterns
    - Generates insights
    - Updates brain_state.md
    """
    try:
        logger.info("Brain: Starting consolidation cycle")

        try:
            from memory.brain import brain
            result = brain.run_cycle()
            logger.info(f"Brain: Cycle complete")
            return {"status": "success", "result": str(result)}
        except ImportError as e:
            logger.warning(f"Brain module not available: {e}")
            return {"status": "unavailable", "error": str(e)}

    except Exception as e:
        logger.error(f"Brain cycle error: {e}")
        raise self.retry(exc=e, countdown=60)


@app.task(bind=True, name='memory.celery_tasks.success_detection')
def success_detection(self):
    """
    Success Detector: Monitor work log for success patterns.

    Runs every 60 seconds to:
    - Scan recent work log entries
    - Detect success indicators
    - Auto-capture high-confidence wins
    """
    try:
        logger.info("Success Detector: Scanning for wins")

        try:
            from memory.enhanced_success_detector import EnhancedSuccessDetector
            detector = EnhancedSuccessDetector()

            # Get recent work log entries
            work_log_path = MEMORY_DIR / ".work_dialogue_log.jsonl"
            if not work_log_path.exists():
                return {"status": "no_work_log"}

            # Read last 50 lines
            with open(work_log_path) as f:
                lines = f.readlines()[-50:]

            successes_found = 0
            for line in lines:
                try:
                    entry = json.loads(line.strip())
                    result = detector.detect(entry.get("content", ""))
                    if result.get("is_success", False) and result.get("confidence", 0) > 0.7:
                        successes_found += 1
                        # Auto-capture if high confidence
                        if result.get("confidence", 0) > 0.85:
                            try:
                                from memory.auto_capture import capture_success
                                capture_success(
                                    task=result.get("task", "Unknown task"),
                                    details=result.get("details", ""),
                                    area="auto-detected"
                                )
                            except Exception as e:
                                print(f"[WARN] Auto-capture success failed: {e}")
                except Exception as e:
                    print(f"[WARN] Success detector LLM analysis failed: {e}")
                    continue

            logger.info(f"Success Detector: Found {successes_found} potential wins")
            return {"status": "success", "wins_found": successes_found}

        except ImportError as e:
            logger.warning(f"Success detector not available: {e}")
            return {"status": "unavailable", "error": str(e)}

    except Exception as e:
        logger.error(f"Success detection error: {e}")
        return {"status": "error", "error": str(e)}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _calculate_project_fingerprint(project_path: str) -> str:
    """Calculate a fingerprint of the project state."""
    project_path = Path(project_path)

    # Include: git status, key config files, directory structure
    fingerprint_data = []

    # Git HEAD
    git_head = project_path / ".git" / "HEAD"
    if git_head.exists():
        fingerprint_data.append(git_head.read_text())

    # Key config files modification times
    config_files = [
        "package.json", "pyproject.toml", "docker-compose.yaml",
        "CLAUDE.md", "requirements.txt"
    ]
    for cf in config_files:
        cf_path = project_path / cf
        if cf_path.exists():
            fingerprint_data.append(f"{cf}:{cf_path.stat().st_mtime}")

    # Top-level directory names
    for item in project_path.iterdir():
        if item.is_dir() and not item.name.startswith('.'):
            fingerprint_data.append(item.name)

    return hashlib.md5("".join(fingerprint_data).encode()).hexdigest()


def _extract_tags(content: str) -> List[str]:
    """Extract relevant tags from content."""
    # Common keywords to look for
    keywords = [
        "deploy", "fix", "error", "async", "docker", "aws", "terraform",
        "database", "api", "frontend", "backend", "test", "migration",
        "config", "env", "redis", "postgres", "celery"
    ]

    tags = []
    content_lower = content.lower()
    for kw in keywords:
        if kw in content_lower:
            tags.append(kw)

    return tags[:10]  # Max 10 tags


# =============================================================================
# BOUNDARY INTELLIGENCE TASKS
# =============================================================================
# "A/B testing feedback loop for project boundary detection"
# These tasks learn keyword→project associations from feedback

@app.task(bind=True, name='memory.celery_tasks.record_boundary_decision')
def record_boundary_decision(
    self,
    decision_id: str,
    injection_id: str,
    primary_project: str,
    confidence: float,
    keywords: List[str],
    signals: List[Dict],
    session_id: str = None
):
    """
    Record a boundary decision for the feedback loop.

    Called after every boundary intelligence analysis to:
    - Store decision in PostgreSQL for learning
    - Cache in Redis for quick feedback attribution (24h TTL)
    - Update project recency tracker

    Args:
        decision_id: Unique ID for this decision
        injection_id: The injection this decision is for
        primary_project: Detected project (may be None if uncertain)
        confidence: 0.0-1.0 confidence score
        keywords: Keywords extracted from prompt
        signals: List of ProjectSignal dicts that contributed
        session_id: Session ID for recency tracking
    """
    try:
        logger.info(f"Recording boundary decision: {decision_id} -> {primary_project} ({confidence:.1%})")

        # 1. Store in SQLite via boundary_feedback
        try:
            from memory.boundary_feedback import get_boundary_learner
            learner = get_boundary_learner()
            learner.record_injection(
                injection_id=injection_id,
                prompt="",  # Not stored redundantly
                detected_project=primary_project,
                ab_variant="control",  # Default, can be passed later
                keywords=keywords,
                learnings_included=[],
                risk_level="moderate",
                session_id=session_id or ""
            )
        except Exception as e:
            logger.warning(f"Failed to record to boundary_feedback: {e}")

        # 2. Cache in Redis for quick feedback attribution
        try:
            from memory.redis_cache import get_redis_client
            redis_client = get_redis_client()
            if redis_client:
                cache_key = f"contextdna:boundary:decision:{injection_id}"
                redis_client.setex(
                    cache_key,
                    86400,  # 24 hours
                    json.dumps({
                        "decision_id": decision_id,
                        "injection_id": injection_id,
                        "primary_project": primary_project,
                        "confidence": confidence,
                        "keywords": keywords,
                        "signals": signals,
                        "session_id": session_id,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                )
        except Exception as e:
            logger.warning(f"Failed to cache boundary decision: {e}")

        # 3. Update project recency
        if primary_project:
            try:
                from memory.project_recency import get_recency_tracker
                tracker = get_recency_tracker()
                tracker.record_activity(primary_project, session_id=session_id)
            except Exception as e:
                logger.warning(f"Failed to update recency: {e}")

        # 4. Bridge to injection_store session tracking for feedback attribution.
        # Without this bridge, auto_capture cannot find the bi_ injection_id
        # to record feedback against (the 0-feedback bug).
        if session_id:
            try:
                from memory.injection_store import get_injection_store
                store = get_injection_store()
                store.track_session_injection(
                    session_id=session_id,
                    injection_id=injection_id,
                    ab_variant="control"
                )
            except Exception as e:
                logger.debug(f"Session-injection bridge failed (non-blocking): {e}")

        return {
            "status": "recorded",
            "decision_id": decision_id,
            "project": primary_project,
            "confidence": confidence
        }

    except Exception as e:
        logger.error(f"Failed to record boundary decision: {e}")
        return {"status": "error", "error": str(e)}


@app.task(bind=True, name='memory.celery_tasks.process_boundary_feedback')
def process_boundary_feedback(
    self,
    injection_id: str,
    was_helpful: bool,
    project_was_correct: bool = True,
    correct_project: str = None,
    feedback_type: str = "task_success",
    user_explicit: bool = False,
    confidence: float = 0.5
):
    """
    Process feedback about a boundary decision and learn from it.

    This is the core learning mechanism:
    - Adjusts keyword→project association weights
    - Records feedback for A/B analysis
    - Updates project boundaries

    Args:
        injection_id: The injection this feedback is for
        was_helpful: Whether the injected context was helpful
        project_was_correct: Whether the project detection was correct
        correct_project: If wrong, what project should it have been?
        feedback_type: Type of feedback signal (task_success, user_correction, etc.)
        user_explicit: True if user explicitly provided this feedback
        confidence: How confident we are in this feedback (0.0-1.0)
    """
    try:
        logger.info(f"Processing boundary feedback for {injection_id}: helpful={was_helpful}, correct={project_was_correct}")

        # Get decision from cache
        decision_data = None
        try:
            from memory.redis_cache import get_redis_client
            redis_client = get_redis_client()
            if redis_client:
                cache_key = f"contextdna:boundary:decision:{injection_id}"
                cached = redis_client.get(cache_key)
                if cached:
                    decision_data = json.loads(cached)
        except Exception as e:
            logger.warning(f"Failed to get cached decision: {e}")

        # Record feedback via boundary_feedback learner
        try:
            from memory.boundary_feedback import get_boundary_learner
            learner = get_boundary_learner()
            learner.record_feedback(
                injection_id=injection_id,
                was_helpful=was_helpful,
                project_was_correct=project_was_correct,
                confidence=confidence,
                correction_project=correct_project,
                signals=[feedback_type],
                user_explicit=user_explicit
            )
        except Exception as e:
            logger.warning(f"Failed to record feedback: {e}")

        # If project was wrong and we have the correct one, strengthen that association
        if not project_was_correct and correct_project:
            try:
                from memory.project_recency import get_recency_tracker
                tracker = get_recency_tracker()
                # Record strong activity for correct project
                tracker.record_activity(correct_project)
            except Exception as e:
                logger.warning(f"Failed to update recency for correct project: {e}")

        # Publish feedback event for dashboard
        try:
            from memory.redis_cache import publish_event
            publish_event("boundary_feedback", {
                "injection_id": injection_id,
                "was_helpful": was_helpful,
                "project_was_correct": project_was_correct,
                "correct_project": correct_project,
                "feedback_type": feedback_type
            })
        except Exception as e:
            logger.warning(f"Failed to publish feedback event: {e}")

        return {
            "status": "processed",
            "injection_id": injection_id,
            "was_helpful": was_helpful,
            "project_was_correct": project_was_correct
        }

    except Exception as e:
        logger.error(f"Failed to process boundary feedback: {e}")
        return {"status": "error", "error": str(e)}


@app.task(bind=True, name='memory.celery_tasks.decay_boundary_associations')
def decay_boundary_associations(self):
    """
    Periodic decay of boundary associations.

    Runs every 5 minutes to:
    - Decay recency weights for all projects
    - Clean up stale Redis entries
    - Consolidate keyword associations periodically

    This ensures:
    - Recent projects have higher priority
    - Old, unused associations fade away
    - The system stays responsive to changing work patterns
    """
    try:
        logger.info("Decaying boundary associations")
        results = {
            "recency_cleaned": 0,
            "associations_consolidated": False
        }

        # 1. Decay project recency
        try:
            from memory.project_recency import get_recency_tracker
            tracker = get_recency_tracker()
            cleaned = tracker.apply_decay()
            results["recency_cleaned"] = cleaned
        except Exception as e:
            logger.warning(f"Failed to decay recency: {e}")

        # 2. Decay SQLite associations
        try:
            from memory.boundary_feedback import get_boundary_learner
            learner = get_boundary_learner()
            learner.decay_recency_weights(decay_factor=0.95)
        except Exception as e:
            logger.warning(f"Failed to decay SQLite associations: {e}")

        # 3. Consolidate associations (every 5th run = ~25 minutes)
        # Use Redis to track consolidation timing
        try:
            from memory.redis_cache import get_redis_client
            redis_client = get_redis_client()
            if redis_client:
                consolidate_counter_key = "contextdna:boundary:consolidate_counter"
                pipe = redis_client.pipeline()
                pipe.incr(consolidate_counter_key)
                pipe.expire(consolidate_counter_key, 3600)
                results = pipe.execute()
                counter = results[0]

                if counter % 5 == 0:
                    from memory.boundary_feedback import get_boundary_learner
                    learner = get_boundary_learner()
                    learner.consolidate_keyword_associations()
                    results["associations_consolidated"] = True
                    logger.info("Consolidated keyword associations")
        except Exception as e:
            logger.warning(f"Failed to consolidate associations: {e}")

        logger.info(f"Decay complete: {results}")
        return results

    except Exception as e:
        logger.error(f"Failed to decay boundary associations: {e}")
        return {"status": "error", "error": str(e)}


@app.task(bind=True, name='memory.celery_tasks.record_clarification_response')
def record_clarification_response(
    self,
    injection_id: str,
    selected_project: str,
    session_id: str = None
):
    """
    Record user's response to a clarification prompt.

    This is a strong learning signal - user explicitly told us which project.

    Args:
        injection_id: The injection that had clarification
        selected_project: The project user selected
        session_id: Session ID for recency tracking
    """
    try:
        logger.info(f"Recording clarification response: {injection_id} -> {selected_project}")

        # Get original decision from cache
        original_project = None
        keywords = []
        try:
            from memory.redis_cache import get_redis_client
            redis_client = get_redis_client()
            if redis_client:
                cache_key = f"contextdna:boundary:decision:{injection_id}"
                cached = redis_client.get(cache_key)
                if cached:
                    data = json.loads(cached)
                    original_project = data.get("primary_project")
                    keywords = data.get("keywords", [])
        except Exception as e:
            logger.warning(f"Failed to get cached decision: {e}")

        # Determine if original was correct
        was_correct = original_project == selected_project

        # Process as feedback with high confidence
        process_boundary_feedback.delay(
            injection_id=injection_id,
            was_helpful=True,  # User engaged = helpful
            project_was_correct=was_correct,
            correct_project=selected_project if not was_correct else None,
            feedback_type="clarification_response",
            user_explicit=True,
            confidence=1.0  # Maximum confidence for explicit user input
        )

        # Strong recency update
        try:
            from memory.project_recency import get_recency_tracker
            tracker = get_recency_tracker()
            tracker.record_activity(selected_project, session_id=session_id)
        except Exception as e:
            logger.warning(f"Failed to update recency: {e}")

        return {
            "status": "recorded",
            "injection_id": injection_id,
            "selected_project": selected_project,
            "was_original_correct": was_correct
        }

    except Exception as e:
        logger.error(f"Failed to record clarification response: {e}")
        return {"status": "error", "error": str(e)}


@app.task(bind=True, name='memory.celery_tasks.track_file_activity')
def track_file_activity(
    self,
    file_path: str,
    project: str = None,
    session_id: str = None
):
    """
    Track file activity for project inference.

    Called when user works on a file to:
    - Update file→project mapping
    - Update project recency
    - Support future file-based project detection

    Args:
        file_path: The file being worked on
        project: Project name (if known)
        session_id: Session ID
    """
    try:
        # Infer project from path if not provided
        if not project:
            try:
                from memory.boundary_intelligence import infer_project_from_path
                project = infer_project_from_path(file_path)
            except Exception as e:
                print(f"[WARN] Project inference from path failed: {e}")

        if not project:
            return {"status": "no_project", "file_path": file_path}

        # Update recency with file path
        try:
            from memory.project_recency import get_recency_tracker
            tracker = get_recency_tracker()
            tracker.record_activity(project, file_path=file_path, session_id=session_id)
        except Exception as e:
            logger.warning(f"Failed to track file activity: {e}")

        return {
            "status": "tracked",
            "file_path": file_path,
            "project": project
        }

    except Exception as e:
        logger.error(f"Failed to track file activity: {e}")
        return {"status": "error", "error": str(e)}


# =============================================================================
# DIALOGUE MIRROR TASKS (Option B - Full Celery Integration)
# =============================================================================
# Synaptic's eyes and ears - capturing and analyzing the Aaron-Atlas dialogue

@app.task(bind=True, name='memory.celery_tasks.mirror_dialogue')
def mirror_dialogue(
    self,
    session_id: str,
    role: str,
    content: str,
    source: str = "vscode",
    project: str = None,
    file_path: str = None
):
    """
    Mirror a dialogue message to Synaptic's awareness.

    This task queues dialogue mirroring for async processing,
    ensuring webhook injection isn't blocked by database writes.

    Args:
        session_id: The conversation session ID
        role: "aaron", "atlas", or "synaptic"
        content: The message content
        source: IDE source (vscode, cursor, windsurf, etc.)
        project: Project context
        file_path: Active file path if any
    """
    try:
        from memory.dialogue_mirror import (
            get_dialogue_mirror,
            MessageRole,
            DialogueSource
        )

        mirror = get_dialogue_mirror()

        # Map role string to enum
        role_map = {
            "aaron": MessageRole.AARON,
            "atlas": MessageRole.ATLAS,
            "synaptic": MessageRole.SYNAPTIC,
            "system": MessageRole.SYSTEM
        }
        msg_role = role_map.get(role.lower(), MessageRole.AARON)

        # Map source string to enum
        try:
            msg_source = DialogueSource(source.lower())
        except ValueError:
            msg_source = DialogueSource.UNKNOWN

        # Mirror the message
        message = mirror.mirror_message(
            session_id=session_id,
            role=msg_role,
            content=content,
            source=msg_source,
            project=project,
            file_path=file_path
        )

        logger.info(f"Mirrored {role} message in session {session_id[:16]}...")
        return {
            "status": "mirrored",
            "message_id": message.id,
            "session_id": session_id,
            "role": role
        }

    except Exception as e:
        logger.error(f"Failed to mirror dialogue: {e}")
        return {"status": "error", "error": str(e)}


@app.task(bind=True, name='memory.celery_tasks.analyze_dialogue_patterns')
def analyze_dialogue_patterns(self, session_id: str = None, max_messages: int = 100):
    """
    Analyze dialogue patterns for Synaptic's learning.

    This task runs periodically to:
    - Identify successful conversation patterns
    - Detect repeated questions (suggests missing documentation)
    - Find project context switches
    - Extract learnable insights

    Args:
        session_id: Optional specific session to analyze
        max_messages: Maximum messages to analyze
    """
    try:
        from memory.dialogue_mirror import get_dialogue_mirror, MessageRole

        mirror = get_dialogue_mirror()
        context = mirror.get_context_for_synaptic(
            session_id=session_id,
            max_messages=max_messages,
            max_age_hours=24
        )

        if not context.get("dialogue_context"):
            return {"status": "no_data", "patterns": []}

        patterns = []
        messages = context["dialogue_context"]

        # Pattern 1: Question repetition (same question asked multiple times)
        aaron_questions = [
            m["content"] for m in messages
            if m["role"] == "aaron" and "?" in m["content"]
        ]
        # Simple duplicate detection (could use embeddings for semantic similarity)
        seen_questions = {}
        for q in aaron_questions:
            # Normalize question
            normalized = q.lower().strip()[:100]
            if normalized in seen_questions:
                seen_questions[normalized] += 1
            else:
                seen_questions[normalized] = 1

        repeated = [q for q, count in seen_questions.items() if count > 1]
        if repeated:
            patterns.append({
                "type": "repeated_questions",
                "count": len(repeated),
                "samples": repeated[:5],
                "insight": "These questions are asked repeatedly - consider adding to documentation or SOPs"
            })

        # Pattern 2: Project context switches
        projects_seen = [m.get("project") for m in messages if m.get("project")]
        if len(set(projects_seen)) > 1:
            patterns.append({
                "type": "multi_project_session",
                "projects": list(set(projects_seen)),
                "insight": "Session spans multiple projects - context switching detected"
            })

        # Pattern 3: Long exchanges (might indicate complex tasks)
        conversation_length = len(messages)
        if conversation_length > 20:
            patterns.append({
                "type": "long_conversation",
                "length": conversation_length,
                "insight": "Extended conversation - complex task or iterative refinement"
            })

        # Pattern 4: IDE diversity
        sources = context.get("sources", [])
        if len(sources) > 1:
            patterns.append({
                "type": "multi_ide",
                "sources": sources,
                "insight": "User switching between multiple IDEs"
            })

        logger.info(f"Analyzed dialogue: {len(patterns)} patterns found")
        return {
            "status": "analyzed",
            "message_count": len(messages),
            "patterns": patterns,
            "time_range": context.get("time_range", {})
        }

    except Exception as e:
        logger.error(f"Failed to analyze dialogue patterns: {e}")
        return {"status": "error", "error": str(e)}


@app.task(bind=True, name='memory.celery_tasks.cleanup_old_dialogue')
def cleanup_old_dialogue(self, days: int = 30):
    """
    Clean up old dialogue data to maintain efficient storage.

    Runs daily to:
    - Delete messages older than threshold
    - Remove empty threads
    - Compact database if needed

    Args:
        days: Delete messages older than this many days
    """
    try:
        from memory.dialogue_mirror import get_dialogue_mirror

        mirror = get_dialogue_mirror()
        mirror.cleanup_old(days=days)

        logger.info(f"Cleaned up dialogue older than {days} days")
        return {"status": "cleaned", "threshold_days": days}

    except Exception as e:
        logger.error(f"Failed to cleanup dialogue: {e}")
        return {"status": "error", "error": str(e)}


@app.task(bind=True, name='memory.celery_tasks.sync_dialogue_to_synaptic')
def sync_dialogue_to_synaptic(self, session_id: str = None):
    """
    Sync dialogue context to Synaptic's awareness file.

    This task updates the Synaptic awareness JSON with recent dialogue,
    enabling Section 6/8 to include conversation context.

    Args:
        session_id: Optional specific session to sync
    """
    try:
        from memory.dialogue_mirror import get_dialogue_mirror
        import json
        from pathlib import Path

        mirror = get_dialogue_mirror()
        context = mirror.get_context_for_synaptic(
            session_id=session_id,
            max_messages=20,
            max_age_hours=4
        )

        # Update Synaptic awareness file
        awareness_file = MEMORY_DIR / ".synaptic_system_awareness.json"
        awareness = {}

        if awareness_file.exists():
            try:
                awareness = json.loads(awareness_file.read_text())
            except Exception as e:
                print(f"[WARN] System awareness file read failed: {e}")

        # Add dialogue context
        awareness["dialogue_mirror"] = {
            "recent_message_count": context.get("message_count", 0),
            "active_projects": context.get("projects", []),
            "active_sources": context.get("sources", []),
            "time_range": context.get("time_range", {}),
            "last_sync": datetime.utcnow().isoformat()
        }

        # Store recent messages summary (not full content for privacy)
        if context.get("dialogue_context"):
            recent = context["dialogue_context"][-5:]
            awareness["dialogue_mirror"]["recent_summary"] = [
                {
                    "role": m.get("role"),
                    "timestamp": m.get("timestamp"),
                    "project": m.get("project"),
                    "preview": m.get("content", "")[:50] + "..."
                }
                for m in recent
            ]

        awareness_file.write_text(json.dumps(awareness, indent=2))

        logger.info("Synced dialogue context to Synaptic awareness")
        return {
            "status": "synced",
            "message_count": context.get("message_count", 0)
        }

    except Exception as e:
        logger.error(f"Failed to sync dialogue to Synaptic: {e}")
        return {"status": "error", "error": str(e)}


@app.task(bind=True, name='memory.celery_tasks.get_dialogue_context')
def get_dialogue_context(
    self,
    session_id: str = None,
    max_messages: int = 20,
    format_for_injection: bool = False
):
    """
    Get dialogue context for injection or analysis.

    This is a utility task that can be called synchronously or async
    to retrieve conversation context for Synaptic.

    Args:
        session_id: Optional specific session
        max_messages: Max messages to retrieve
        format_for_injection: If True, format for webhook injection
    """
    try:
        from memory.dialogue_mirror import get_dialogue_mirror

        mirror = get_dialogue_mirror()

        if format_for_injection and session_id:
            # Get formatted context string
            context_str = mirror.get_synaptic_response_context(session_id)
            return {
                "status": "success",
                "format": "injection",
                "context": context_str
            }
        else:
            # Get structured context
            context = mirror.get_context_for_synaptic(
                session_id=session_id,
                max_messages=max_messages,
                max_age_hours=24
            )
            return {
                "status": "success",
                "format": "structured",
                "context": context
            }

    except Exception as e:
        logger.error(f"Failed to get dialogue context: {e}")
        return {"status": "error", "error": str(e)}


# =============================================================================
# EVIDENCE PIPELINE TASKS
# =============================================================================
# Critical bridge tasks that keep the evidence pipeline flowing:
# quarantine → trusted → flagged_for_review → wisdom

@app.task(name='memory.celery_tasks.promote_trusted_to_wisdom')
def promote_trusted_to_wisdom():
    """
    Bridge: trusted claims → flagged_for_review → applied_to_wisdom.

    The evidence pipeline gap:
      evaluate_quarantine promotes quarantined → trusted (claim.status='active')
      professor.apply_learnings_to_wisdom processes 'flagged_for_review' only
      NO CODE transitions 'active' (from quarantine promotion) → 'flagged_for_review'

    This task finds claims that:
      1. Have a knowledge_quarantine entry with status='trusted'
      2. Have claim.status='active'
    And flags them for professor review, completing the pipeline.
    """
    try:
        from memory.observability_store import get_observability_store
        store = get_observability_store()

        cursor = store._sqlite_conn.execute("""
            SELECT kq.item_id, c.statement, c.area
            FROM knowledge_quarantine kq
            JOIN claim c ON c.claim_id = kq.item_id
            WHERE kq.status = 'trusted'
              AND c.status = 'active'
            LIMIT 50
        """)
        trusted_claims = cursor.fetchall()

        if not trusted_claims:
            return {"status": "success", "flagged": 0, "message": "no trusted claims pending promotion"}

        flagged = 0
        for row in trusted_claims:
            claim_id = row[0]
            try:
                store._sqlite_conn.execute("""
                    UPDATE claim SET status = 'flagged_for_review'
                    WHERE claim_id = ? AND status = 'active'
                """, (claim_id,))
                flagged += 1
                logger.info(f"TRUSTED→FLAGGED: {claim_id}")
            except Exception as e:
                logger.warning(f"Failed to flag claim {claim_id}: {e}")

        if flagged > 0:
            store._sqlite_conn.commit()
            logger.info(
                f"PROMOTION BRIDGE: {flagged} trusted claims → flagged_for_review"
            )

        return {"status": "success", "flagged": flagged, "total": len(trusted_claims)}

    except Exception as e:
        logger.error(f"TRUSTED→WISDOM PROMOTION ERROR: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.evaluate_quarantine')
def evaluate_quarantine():
    """
    Evaluate quarantine status for claims/learnings/SOPs.

    Promotes quarantined → trusted when outcome data meets thresholds:
    - Bootstrap: n>=1, success_rate>=0.5
    - Moderate: n>=3, success_rate>=0.6
    - Mature: n>=10, success_rate>=0.7
    - Age-based: >24h with no outcomes → auto-promote (bootstrap trust)
    Rejects when n>=10 and success_rate<0.3.
    """
    try:
        from memory.observability_store import get_observability_store
        store = get_observability_store()

        cursor = store._sqlite_conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='knowledge_quarantine'
        """)
        if not cursor.fetchone():
            return {"status": "success", "message": "knowledge_quarantine table not present"}

        store._sqlite_conn.execute("""
            CREATE TABLE IF NOT EXISTS direct_claim_outcome (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id          TEXT NOT NULL,
                timestamp_utc     TEXT NOT NULL,
                success           INTEGER NOT NULL CHECK (success IN (0, 1)),
                reward            REAL NOT NULL DEFAULT 0.0,
                source            TEXT NOT NULL DEFAULT 'auto',
                notes             TEXT
            )
        """)

        cursor = store._sqlite_conn.execute("""
            SELECT item_id, item_type, promotion_rules_json, created_at_utc
            FROM knowledge_quarantine
            WHERE status IN ('quarantined', 'validating')
        """)

        promoted = 0
        rejected = 0
        evaluated = 0

        for row in cursor.fetchall():
            item_id = row["item_id"]
            item_type = row["item_type"]
            created_at = row["created_at_utc"]
            evaluated += 1

            rollup = None
            try:
                rollup = store.get_direct_claim_rollup(item_id)
            except Exception:
                pass

            if not rollup and item_type == "claim":
                try:
                    outcome_cursor = store._sqlite_conn.execute("""
                        SELECT n, avg_reward, success_rate
                        FROM claim_outcome_rollup
                        WHERE claim_id = ?
                        ORDER BY window_end_utc DESC
                        LIMIT 1
                    """, (item_id,))
                    outcome = outcome_cursor.fetchone()
                    if outcome and outcome["n"] > 0:
                        rollup = {
                            "n": outcome["n"],
                            "success_rate": outcome["success_rate"],
                            "avg_reward": outcome["avg_reward"],
                        }
                except Exception:
                    pass

            if rollup:
                n = rollup["n"]
                success_rate = rollup["success_rate"]

                if n >= 10:
                    promote_n, promote_rate = 10, 0.7
                elif n >= 3:
                    promote_n, promote_rate = 3, 0.6
                else:
                    promote_n, promote_rate = 1, 0.5

                if n >= promote_n:
                    if success_rate >= promote_rate:
                        _update_quarantine_status(store, item_id, "trusted",
                                                 {"n": n, "success_rate": success_rate, "path": "outcome_data"})
                        promoted += 1
                    elif n >= 10 and success_rate < 0.3:
                        _update_quarantine_status(store, item_id, "rejected",
                                                 {"n": n, "success_rate": success_rate, "path": "low_success_rate"})
                        rejected += 1
            else:
                # Age-based promotion: >24h with no outcomes → bootstrap trust
                if created_at:
                    try:
                        age_cursor = store._sqlite_conn.execute("""
                            SELECT (julianday('now') - julianday(?)) * 24 AS hours_old
                        """, (created_at,))
                        age_row = age_cursor.fetchone()
                        hours_old = age_row[0] if age_row and age_row[0] else 0
                        if hours_old > 24:
                            _update_quarantine_status(store, item_id, "trusted",
                                                     {"n": 0, "success_rate": 0.0,
                                                      "path": "age_based_24h",
                                                      "hours_old": round(hours_old, 1)})
                            promoted += 1
                    except Exception:
                        pass

        store._sqlite_conn.commit()

        return {
            "status": "success",
            "evaluated": evaluated,
            "promoted": promoted,
            "rejected": rejected,
        }

    except Exception as e:
        logger.error(f"QUARANTINE EVAL ERROR: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.compute_rollups')
def compute_rollups():
    """
    Compute metrics rollups for A/B testing analytics + SOP outcome scoring.

    Populates SQLite rollup tables (variant, section, claim rollups).
    Also computes SOP reliability scores from outcome data.
    """
    try:
        from memory.observability_store import get_observability_store
        store = get_observability_store()

        results = store.compute_all_rollups(window_minutes=60)

        sop_updated = 0
        try:
            sop_updated = store.compute_sop_outcome_rollup()
        except Exception:
            pass

        total_rows = sum(results.values())
        return {
            "status": "success",
            "total_rows": total_rows,
            "rollups": results,
            "sop_scored": sop_updated,
        }

    except Exception as e:
        logger.error(f"Rollup compute error: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.ttl_decay')
def ttl_decay():
    """
    Expire claims past their TTL.

    Transitions active/quarantined claims to 'expired' when past ttl_seconds.
    """
    try:
        from memory.observability_store import get_observability_store
        store = get_observability_store()

        result = store.enforce_ttl_decay()
        return {"status": "success", "expired": result["expired"], "kept": result["kept"]}

    except Exception as e:
        logger.error(f"TTL decay error: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.injection_health')
def injection_health():
    """
    Monitor webhook injection health (CRITICAL).

    Checks injection frequency, section health (0-8),
    and 8th Intelligence status.
    """
    try:
        from memory.injection_health_monitor import run_injection_health_check
        success, message = run_injection_health_check()
        return {"status": "success" if success else "unhealthy", "message": message}

    except ImportError:
        return {"status": "success", "message": "injection_health_monitor not available"}
    except Exception as e:
        logger.error(f"Injection health error: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.hindsight_check')
def hindsight_check():
    """
    Butler's hindsight validation via dialogue mirror.

    Verifies recorded wins by scanning dialogue mirror for subsequent
    error patterns. Emits negative outcome_events for miswirings
    (reward=-0.3) to feed the evidence pipeline's discrimination power.
    """
    try:
        from memory.hindsight_validator import HindsightValidator, VerificationStatus
        validator = HindsightValidator()
        results = validator.run_hindsight_check()

        verified = sum(1 for r in results if r.status == VerificationStatus.VERIFIED)
        suspects = sum(1 for r in results if r.status == VerificationStatus.SUSPECT)
        miswirings = sum(1 for r in results if r.status == VerificationStatus.MISWIRING)

        return {
            "status": "success",
            "checked": len(results),
            "verified": verified,
            "suspects": suspects,
            "miswirings": miswirings,
        }

    except ImportError:
        return {"status": "success", "message": "hindsight_validator not available"}
    except Exception as e:
        logger.error(f"Hindsight check error: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.failure_pattern_analysis')
def failure_pattern_analysis():
    """
    Butler's failure pattern analysis via unified sources.

    Scans DialogueMirror + TemporalValidator + HindsightValidator for
    high-yield failure patterns. Generates LANDMINE warnings for
    Section 2 (WISDOM) webhook injection.
    """
    try:
        from memory.failure_pattern_analyzer import FailurePatternAnalyzer
        analyzer = FailurePatternAnalyzer()
        patterns = analyzer.analyze_for_patterns(hours_back=24)

        high_yield = [p for p in patterns if p.occurrence_count >= 3]
        domains = set(p.domain for p in high_yield if p.domain)

        return {
            "status": "success",
            "patterns_found": len(patterns),
            "high_yield": len(high_yield),
            "domains": list(domains),
        }

    except ImportError:
        return {"status": "success", "message": "failure_pattern_analyzer not available"}
    except Exception as e:
        logger.error(f"Failure pattern analysis error: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.skeletal_integrity')
def skeletal_integrity():
    """
    Butler's skeletal integrity check — self-healing DB repair.

    Checks all 11 SQLite databases for corruption.
    Self-heals via: sqlite3 .recover → PG restore → recreate empty.
    """
    try:
        from memory.butler_db_repair import ButlerDBRepair, RepairOutcome
        repair = ButlerDBRepair()
        results = repair.run_integrity_sweep()

        healthy = sum(1 for r in results.values() if r.outcome == RepairOutcome.NOT_NEEDED)
        repaired = sum(1 for r in results.values() if r.outcome == RepairOutcome.SUCCESS)
        failed = sum(1 for r in results.values() if r.outcome == RepairOutcome.FAILED)

        return {
            "status": "success",
            "total": len(results),
            "healthy": healthy,
            "repaired": repaired,
            "failed": failed,
        }

    except ImportError:
        return {"status": "success", "message": "butler_db_repair not available"}
    except Exception as e:
        logger.error(f"Skeletal integrity error: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.mmotw_repair_mining')
def mmotw_repair_mining():
    """
    MMOTW: Mine dialogue mirror for repair patterns Atlas performed.

    Extracts repair SOPs from dialogue mirror conversations,
    validates against outcomes.
    """
    try:
        from memory.butler_repair_miner import MMOTWMiner
        miner = MMOTWMiner()
        results = miner.run_mining_sweep()

        return {
            "status": "success",
            "sessions_mined": results.get("sessions_mined", 0),
            "new_sops": results.get("new_sops", 0),
            "updated_sops": results.get("updated_sops", 0),
            "validated": results.get("validated", 0),
        }

    except ImportError:
        return {"status": "success", "message": "butler_repair_miner not available"}
    except Exception as e:
        logger.error(f"MMOTW mining error: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.sop_dedup_analysis')
def sop_dedup_analysis():
    """
    Butler's SOP deduplication sweep.

    Scans the hook evolution library for exact/near duplicates,
    category imbalances, and unused patterns. Flags for review only.
    """
    try:
        from memory.dedup_detector import DuplicateDetector
        detector = DuplicateDetector()

        exact = detector.find_exact_duplicates()
        similar = detector.find_similar_patterns(threshold=0.7)
        unused = detector.find_unused_patterns(min_outcomes=0)

        return {
            "status": "success",
            "exact_duplicates": len(exact),
            "similar_patterns": len(similar),
            "unused_patterns": len(unused),
        }

    except ImportError:
        return {"status": "success", "message": "dedup_detector not available"}
    except Exception as e:
        logger.error(f"SOP dedup analysis error: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.post_session_meta_analysis')
def post_session_meta_analysis():
    """
    Post-session meta-analysis (Evidence-Based Section 11).

    4-phase pipeline: Summarize → Cross-ref → Synthesize → Feed back.
    Only runs when session gap > 30min detected.
    """
    try:
        from memory.meta_analysis import PostSessionMetaAnalysis
        analyzer = PostSessionMetaAnalysis()

        if not analyzer.should_run():
            return {"status": "success", "message": "no session end detected"}

        result = analyzer.run_analysis()
        if not result:
            return {"status": "success", "message": "no sessions to analyze"}

        return {
            "status": "success",
            "sessions_analyzed": result.sessions_analyzed,
            "total_messages": result.total_messages,
            "insights": len(result.insights),
            "concerns": len(result.concerns),
            "sop_candidates": len(result.sop_candidates),
            "llm_used": result.llm_used,
            "duration_ms": result.duration_ms,
        }

    except ImportError:
        return {"status": "success", "message": "meta_analysis not available"}
    except Exception as e:
        logger.error(f"Post-session meta-analysis error: {e}")
        return {"status": "error", "error": str(e)}


@app.task(name='memory.celery_tasks.codebase_map_refresh')
def codebase_map_refresh():
    """Incremental rebuild of architecture graph cache for Section 4 injection."""
    try:
        from memory.codebase_map import refresh
        refresh()
        return {"status": "success", "message": "codebase map refreshed"}

    except ImportError:
        return {"status": "success", "message": "codebase_map not available"}
    except Exception as e:
        logger.error(f"Codebase map refresh error: {e}")
        return {"status": "error", "error": str(e)}


# =============================================================================
# EVIDENCE PIPELINE HELPERS
# =============================================================================

def _update_quarantine_status(store, item_id: str, new_status: str, stats: dict):
    """Update both knowledge_quarantine.status and claim.status on promotion/rejection."""
    now = datetime.utcnow().isoformat()
    stats_json = json.dumps(stats)

    store._sqlite_conn.execute("""
        UPDATE knowledge_quarantine
        SET status = ?,
            updated_at_utc = ?,
            validation_stats_json = ?
        WHERE item_id = ?
    """, (new_status, now, stats_json, item_id))

    claim_status = "active" if new_status == "trusted" else "rejected"
    store._sqlite_conn.execute("""
        UPDATE claim SET status = ?
        WHERE claim_id = ? AND status = 'quarantined'
    """, (claim_status, item_id))
