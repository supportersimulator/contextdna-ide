#!/usr/bin/env python3
"""
UNIFIED ARCHITECTURE BRAIN - 100% Autonomous Learning & Context Distribution

This is the master controller that makes everything fully automatic:
1. AUTO-CAPTURES all agent activity (commands, files, successes)
2. AUTO-DETECTS objective successes (not premature claims!)
3. AUTO-CONSOLIDATES learnings into architecture knowledge
4. AUTO-INJECTS blueprints into agent context
5. AUTO-CLEANS processed data

100% AUTOMATED - NO HUMAN INTERVENTION REQUIRED.

OBJECTIVE SUCCESS DETECTION:
The brain doesn't just record what agents claim as success - it detects
ACTUAL, VERIFIED successes by analyzing the work stream for:
- User confirmations ("that worked", "perfect", "yes")
- System confirmations (exit 0, "healthy", "200 OK")
- Absence of reversal (no "fix", "retry", "rollback" after)

This eliminates the 10 failures for every 1 success problem - only
OBJECTIVE successes get recorded to architecture knowledge.

The brain maintains a "current state" file that gets injected into agent
context automatically. Agents read CLAUDE.md which includes the brain state.

ARCHITECTURE:
    ┌─────────────────────────────────────────────────────────────────┐
    │                    ARCHITECTURE BRAIN (100% Automated)           │
    │                                                                  │
    │  CAPTURE ────────────────────────────────────────────────────── │
    │  │  bash commands → auto_capture.py                             │
    │  │  file changes → auto_capture.py                              │
    │  │  git commits → auto_learn.py (via hook)                      │
    │  │  user dialogue → capture_user_message()                      │
    │  │  agent dialogue → capture_agent_message()                    │
    │  └───────────────────┬───────────────────────────────────────── │
    │                      ↓                                           │
    │  VERIFY (Objective Success Detection) ─────────────────────────  │
    │  │  "done!" + error after → NOT SUCCESS (cancelled)             │
    │  │  "healthy" + user "perfect" → SUCCESS (confirmed)            │
    │  │  exit 0 + retry after → NOT SUCCESS (cancelled)              │
    │  │  Only 0.7+ confidence → recorded to knowledge                │
    │  └───────────────────┬───────────────────────────────────────── │
    │                      ↓                                           │
    │  CONSOLIDATE ────────────────────────────────────────────────── │
    │  │  verified successes → pattern detection                      │
    │  │  patterns → insight generation                               │
    │  │  insights → Context DNA SOPs                                    │
    │  │  processed entries → cleanup (self-cleaning)                 │
    │  └───────────────────┬───────────────────────────────────────── │
    │                      ↓                                           │
    │  DISTRIBUTE ─────────────────────────────────────────────────── │
    │  │  generate brain_state.md                                     │
    │  │  inject into agent context                                   │
    │  │  agents work with blueprints in hand                         │
    │  └─────────────────────────────────────────────────────────────  │
    │                                                                  │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    # Initialize brain (run once at session start)
    python memory/brain.py init

    # Capture a successful operation
    python memory/brain.py success "Deployed Django update" "Used systemctl restart"

    # Get current brain state (for context injection)
    python memory/brain.py state

    # Run full cycle (consolidate + update state)
    python memory/brain.py cycle

    # Background daemon (continuous operation)
    python memory/brain.py daemon
"""

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger('context_dna.brain')

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import all memory components
try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False

try:
    from memory.auto_capture import (
        capture_command, capture_file_change, capture_success,
        capture_error_resolution, get_capture_stats
    )
    AUTO_CAPTURE_AVAILABLE = True
except ImportError:
    AUTO_CAPTURE_AVAILABLE = False

try:
    from memory.architecture_enhancer import (
        work_log, consolidation_engine, detect_patterns_in_work,
        generate_insights_from_patterns, log_work
    )
    ENHANCER_AVAILABLE = True
except ImportError:
    ENHANCER_AVAILABLE = False

try:
    from memory.context import get_blueprint, before_work
    CONTEXT_AVAILABLE = True
except ImportError:
    CONTEXT_AVAILABLE = False

try:
    from memory.knowledge_graph import KnowledgeGraph
    KNOWLEDGE_GRAPH_AVAILABLE = True
except ImportError:
    KNOWLEDGE_GRAPH_AVAILABLE = False

try:
    from memory.objective_success import (
        analyze_for_successes, get_objective_successes,
        ObjectiveSuccess, ObjectiveSuccessDetector
    )
    OBJECTIVE_SUCCESS_AVAILABLE = True
except ImportError:
    OBJECTIVE_SUCCESS_AVAILABLE = False

# Enhanced success detection - multi-layer analysis (subconscious processing)
try:
    from memory.enhanced_success_detector import (
        EnhancedSuccessDetector, EnhancedSuccess, analyze_work_log_enhanced
    )
    ENHANCED_DETECTION_AVAILABLE = True
except ImportError:
    ENHANCED_DETECTION_AVAILABLE = False

# Pattern evolution - autonomous learning (system gets smarter automatically)
try:
    from memory.pattern_evolution import (
        PatternEvolutionEngine, get_evolution_engine, evolve_patterns
    )
    EVOLUTION_AVAILABLE = True
except ImportError:
    EVOLUTION_AVAILABLE = False

try:
    from memory.sop_types import (
        SOPRegistry, LearningType,
        auto_extract_sop_from_success, auto_extract_gotcha_from_error
    )
    SOP_TYPES_AVAILABLE = True
except ImportError:
    SOP_TYPES_AVAILABLE = False

# Hindsight validation - delayed win verification (detects false positives)
try:
    from memory.hindsight_validator import (
        HindsightValidator, record_win_for_verification,
        run_verification_cycle, get_validator
    )
    HINDSIGHT_AVAILABLE = True
except ImportError:
    HINDSIGHT_AVAILABLE = False

# Professor evolution - closes learning feedback loop
try:
    from memory.professor import ProfessorEvolution
    PROFESSOR_EVOLUTION_AVAILABLE = True
except ImportError:
    PROFESSOR_EVOLUTION_AVAILABLE = False


# =============================================================================
# BRAIN STATE - The current "consciousness" of the architecture
# =============================================================================

BRAIN_STATE_FILE = Path(__file__).parent / "brain_state.md"
BRAIN_CACHE_FILE = Path(__file__).parent / ".brain_cache.json"


@dataclass
class BrainState:
    """Current state of the architecture brain."""
    last_updated: str
    active_patterns: List[str]
    recent_insights: List[str]
    critical_warnings: List[str]
    recent_successes: List[str]
    areas_active: List[str]
    capture_stats: Dict
    consolidation_stats: Dict


class ArchitectureBrain:
    """
    The unified brain that coordinates all memory systems.

    This is the single point of control for:
    - Capturing architecture knowledge
    - Consolidating patterns and insights
    - Distributing context to agents
    """

    def __init__(self):
        self.cache = self._load_cache()

    def _load_cache(self) -> dict:
        if BRAIN_CACHE_FILE.exists():
            try:
                with open(BRAIN_CACHE_FILE) as f:
                    return json.load(f)
            except Exception as e:
                logger.debug(f"Cache load failed: {e}")
        return {
            "last_cycle": None,
            "cycles_run": 0,
            "patterns_ever_detected": [],
            "insights_ever_generated": [],
            "successes_captured": 0
        }

    def _save_cache(self):
        with open(BRAIN_CACHE_FILE, "w") as f:
            json.dump(self.cache, f, indent=2, default=str)

    # =========================================================================
    # CAPTURE LAYER - Record everything
    # =========================================================================

    def capture_bash(self, command: str, output: str, exit_code: int = 0) -> bool:
        """
        Capture a bash command execution.

        Call this after running any infrastructure command.
        """
        if not AUTO_CAPTURE_AVAILABLE:
            return False

        # Capture to Context DNA
        session_id = capture_command(command, output, exit_code)

        # Also log to work dialogue
        if ENHANCER_AVAILABLE and exit_code == 0:
            log_work("command", f"{command[:100]}", area="bash")

        return session_id is not None

    def capture_file(self, file_path: str, old_content: str, new_content: str) -> bool:
        """
        Capture a file modification.

        Call this after modifying infrastructure files.
        """
        if not AUTO_CAPTURE_AVAILABLE:
            return False

        session_id = capture_file_change(file_path, old_content, new_content)
        return session_id is not None

    def capture_win(self, task: str, details: str = None, area: str = None,
                     artifacts: dict = None, command: str = None) -> bool:
        """
        Capture a successful task completion.

        This is the most valuable capture - the 1 thing that worked.

        Args:
            task: What was accomplished
            details: How it was done / key insight
            area: Architecture area (docker, terraform, django, etc.)
            artifacts: Dict of {file_path: content} to store in SeaweedFS
            command: The command that succeeded (for extraction)
        """
        if not AUTO_CAPTURE_AVAILABLE:
            return False

        session_id = capture_success(task, details, area)

        # NEW: Register for delayed hindsight verification (24h later)
        # This adds a secondary validation layer - catches wins that become miswirings
        if HINDSIGHT_AVAILABLE and session_id:
            try:
                win_id = record_win_for_verification(
                    task=task,
                    approach=details or "No details provided",
                    expected_outcome="System continues working correctly",
                    related_patterns=[area] if area else None,
                    verification_hours=24,  # Check again tomorrow
                    confidence=0.8  # Initial confidence, hindsight may adjust
                )
                if win_id:
                    # Track correlation between session_id and win_id for later analysis
                    self.cache.setdefault("hindsight_tracking", {})[session_id] = win_id
            except Exception as e:
                # Don't let hindsight failure block win capture
                pass

        # Also log to work dialogue
        if ENHANCER_AVAILABLE:
            log_work("success", task, area=area)

        # NEW: Extract and store artifacts if available
        if session_id:
            self._extract_and_store_artifacts(session_id, task, details, area, artifacts, command)

        self.cache["successes_captured"] += 1
        self._save_cache()

        return session_id is not None

    def _extract_and_store_artifacts(self, session_id: str, task: str, details: str = None,
                                      area: str = None, artifacts: dict = None, command: str = None):
        """
        Extract and store artifacts from ANY successful operation.

        Triggers on ANY objective success - not just infrastructure:
        - Infrastructure: docker-compose.yml, .tf files, deploy scripts
        - Code: the specific file that was fixed/improved
        - Configuration: any config files mentioned
        - API: request/response examples
        - Documentation: relevant docs

        This empowers BOTH the Professor (patterns) AND the Brain (specific experiences).
        """
        try:
            from memory.artifact_store import ArtifactStore, is_infrastructure_file, sanitize_secrets
            ARTIFACT_STORE_AVAILABLE = True
        except ImportError:
            ARTIFACT_STORE_AVAILABLE = False
            return

        if not ARTIFACT_STORE_AVAILABLE:
            return

        # Combine task and details for analysis
        context = f"{task} {details or ''} {command or ''}".lower()

        extracted_artifacts = artifacts or {}
        detected_area = area

        # EXPANDED area detection - covers ALL types of wins, not just infrastructure
        if not detected_area:
            # Infrastructure areas
            if any(k in context for k in ['docker', 'compose', 'container', 'ecs']):
                detected_area = 'docker'
            elif any(k in context for k in ['terraform', '.tf', 'apply', 'plan', 'infra']):
                detected_area = 'terraform'
            elif any(k in context for k in ['deploy', 'gunicorn', 'systemctl', 'restart']):
                detected_area = 'deployment'
            elif any(k in context for k in ['nginx', 'ssl', 'cert', 'proxy', 'nlb', 'dns']):
                detected_area = 'networking'
            elif any(k in context for k in ['livekit', 'webrtc', 'turn', 'stun']):
                detected_area = 'livekit'
            # Code/development areas
            elif any(k in context for k in ['async', 'asyncio', 'await', 'thread']):
                detected_area = 'async'
            elif any(k in context for k in ['boto3', 'aws', 'bedrock', 'lambda']):
                detected_area = 'aws'
            elif any(k in context for k in ['llm', 'claude', 'gpt', 'ai', 'model']):
                detected_area = 'llm'
            elif any(k in context for k in ['tts', 'stt', 'whisper', 'audio', 'speech']):
                detected_area = 'voice'
            elif any(k in context for k in ['python', 'pip', 'venv', 'requirements']):
                detected_area = 'python'
            elif any(k in context for k in ['react', 'next', 'typescript', 'frontend']):
                detected_area = 'frontend'
            elif any(k in context for k in ['django', 'api', 'endpoint', 'backend']):
                detected_area = 'backend'
            elif any(k in context for k in ['git', 'commit', 'branch', 'merge']):
                detected_area = 'git'
            elif any(k in context for k in ['test', 'jest', 'pytest', 'spec']):
                detected_area = 'testing'
            elif any(k in context for k in ['fix', 'bug', 'error', 'issue']):
                detected_area = 'bugfix'
            else:
                detected_area = 'general'

        # Auto-extract artifacts based on area if none provided
        if not extracted_artifacts:
            import os
            from pathlib import Path
            repo_root = Path(__file__).parent.parent

            # EXPANDED: Define what files to look for based on ALL area types
            area_files = {
                # Infrastructure
                'docker': ['docker-compose.yml', 'docker-compose.yaml', 'Dockerfile'],
                'terraform': list((repo_root / 'infra').rglob('*.tf'))[:5] if (repo_root / 'infra').exists() else [],
                'deployment': ['scripts/deploy.sh', 'scripts/deploy-django.sh', 'gunicorn.conf.py'],
                'networking': ['nginx.conf', 'turnserver.conf'],
                'livekit': ['livekit.yaml', 'livekit-config.yaml'],
                # Code areas - look for recently modified files
                'async': self._find_recent_py_files(repo_root, 'async'),
                'aws': self._find_recent_py_files(repo_root, 'boto'),
                'llm': self._find_recent_files(repo_root / 'ersim-voice-stack/services/llm'),
                'voice': self._find_recent_files(repo_root / 'ersim-voice-stack'),
                'python': ['requirements.txt', 'pyproject.toml', 'setup.py'],
                'backend': self._find_recent_files(repo_root / 'backend'),
                'frontend': ['package.json', 'tsconfig.json'],
                'git': [],  # Git wins don't need file artifacts
                'testing': self._find_test_files(repo_root),
                'bugfix': [],  # Bug fixes are context-specific
                'general': [],
            }

            # Get relevant files for this area
            relevant_patterns = area_files.get(detected_area, [])

            for pattern in relevant_patterns[:3]:  # Limit to 3 files
                if isinstance(pattern, Path):
                    file_path = pattern
                else:
                    file_path = repo_root / pattern

                if isinstance(file_path, Path) and file_path.exists() and file_path.is_file():
                    try:
                        content = file_path.read_text()
                        # Sanitize before storing
                        safe_content = sanitize_secrets(content)
                        rel_path = str(file_path.relative_to(repo_root))
                        extracted_artifacts[rel_path] = safe_content
                    except Exception as e:
                        logger.debug(f"Artifact extraction failed for {file_path}: {e}")
                        continue

        # Store if we found anything OR if this is a significant win
        # (Even without artifacts, we want to track the session for later linking)
        if extracted_artifacts:
            try:
                store = ArtifactStore()
                disk_id = store.store_with_artifacts(
                    session_id=session_id[:16] if session_id else task[:16],
                    artifacts=extracted_artifacts,
                    area=detected_area,
                    sanitize=True
                )
                print(f"   📦 Stored {len(extracted_artifacts)} artifacts for [{detected_area}]: {task[:50]}")
            except Exception as e:
                print(f"   ⚠️ Artifact storage failed: {e}")
        else:
            # Log that we captured the win even without artifacts
            print(f"   ✅ Captured win [{detected_area}]: {task[:50]} (no artifacts extracted)")

    def _find_recent_py_files(self, repo_root: Path, keyword: str = None) -> list:
        """Find recently modified Python files, optionally containing keyword."""
        try:
            import subprocess
            # Get recently modified .py files (last 24 hours)
            result = subprocess.run(
                ['find', str(repo_root), '-name', '*.py', '-mtime', '-1', '-type', 'f'],
                capture_output=True, text=True, timeout=5
            )
            files = result.stdout.strip().split('\n')[:10]
            if keyword:
                # Filter to files containing keyword
                matching = []
                for f in files:
                    if f and Path(f).exists():
                        try:
                            content = Path(f).read_text()
                            if keyword.lower() in content.lower():
                                matching.append(Path(f))
                        except Exception as e:
                            logger.debug(f"File keyword search failed for {f}: {e}")
                return matching[:3]
            return [Path(f) for f in files if f][:3]
        except Exception as e:
            logger.debug(f"Recent py files search failed: {e}")
            return []

    def _find_recent_files(self, directory: Path) -> list:
        """Find recently modified files in a directory."""
        try:
            if not directory.exists():
                return []
            import subprocess
            result = subprocess.run(
                ['find', str(directory), '-type', 'f', '-mtime', '-1'],
                capture_output=True, text=True, timeout=5
            )
            files = result.stdout.strip().split('\n')
            # Filter out __pycache__, .pyc, etc.
            valid = [f for f in files if f and '__pycache__' not in f and not f.endswith('.pyc')]
            return [Path(f) for f in valid][:3]
        except Exception as e:
            logger.debug(f"Recent files search failed: {e}")
            return []

    def _find_test_files(self, repo_root: Path) -> list:
        """Find test files."""
        try:
            test_patterns = ['test_*.py', '*_test.py', '*.spec.ts', '*.test.ts']
            files = []
            for pattern in test_patterns:
                files.extend(list(repo_root.rglob(pattern))[:2])
            return files[:3]
        except Exception as e:
            logger.debug(f"Test files search failed: {e}")
            return []

    def capture_fix(self, error: str, resolution: str, area: str = None, severity: str = "medium") -> bool:
        """
        Capture an error resolution.

        These are extremely valuable - learning from mistakes.
        Automatically creates a typed Gotcha in Context DNA.
        """
        if not AUTO_CAPTURE_AVAILABLE:
            return False

        session_id = capture_error_resolution(error, resolution, area)

        # Also log to work dialogue
        if ENHANCER_AVAILABLE:
            log_work("error_resolution", f"Error: {error[:50]}... → Fixed", area=area)

        # NEW: Auto-extract as typed Gotcha for Context DNA parity
        if SOP_TYPES_AVAILABLE:
            try:
                auto_extract_gotcha_from_error(
                    error=error,
                    what_caused_it=f"Occurred while working on {area or 'infrastructure'}",
                    how_fixed=resolution,
                    severity=severity,
                    tags=[area] if area else []
                )
            except Exception as e:
                logger.debug(f"SOP extraction failed: {e}")

        return session_id is not None

    def capture_user_message(self, message: str) -> bool:
        """
        Capture user dialogue for objective success detection.

        User confirmations like "that worked" or "perfect" are
        STRONG signals that previous work was successful.
        """
        if not ENHANCER_AVAILABLE:
            return False

        work_log.log_dialogue(message, source="user")
        return True

    def capture_agent_message(self, message: str) -> bool:
        """
        Capture agent dialogue.

        This creates the full mirror of conversation for analysis.
        """
        if not ENHANCER_AVAILABLE:
            return False

        work_log.log_dialogue(message, source="atlas")
        return True

    # =========================================================================
    # CONSOLIDATE LAYER - Extract patterns and insights
    # =========================================================================

    def consolidate(self) -> Dict:
        """
        Run consolidation cycle.

        Analyzes work log, detects patterns, generates insights,
        stores to Context DNA, and cleans up processed entries.
        """
        if not ENHANCER_AVAILABLE:
            return {"success": False, "error": "Enhancer not available"}

        result = consolidation_engine.run_consolidation()

        if result.success:
            # Update cache with new patterns
            for pattern in result.areas_covered:
                if pattern not in self.cache["patterns_ever_detected"]:
                    self.cache["patterns_ever_detected"].append(pattern)

            self.cache["last_cycle"] = datetime.now().isoformat()
            self.cache["cycles_run"] += 1
            self._save_cache()

            # Cleanup processed entries
            work_log.cleanup_processed_entries()

        return asdict(result)

    # =========================================================================
    # DISTRIBUTE LAYER - Generate and inject context
    # =========================================================================

    def generate_brain_state(self) -> BrainState:
        """
        Generate current brain state for context injection.

        This creates a snapshot of what the brain knows right now.
        """
        # Get capture stats
        capture_stats = {}
        if AUTO_CAPTURE_AVAILABLE:
            capture_stats = get_capture_stats()

        # Get consolidation stats
        consolidation_stats = {}
        if ENHANCER_AVAILABLE:
            consolidation_stats = consolidation_engine.get_status()

        # Get recent patterns and insights
        patterns = []
        insights = []
        if ENHANCER_AVAILABLE:
            entries = work_log.get_successes(hours=48)
            detected = detect_patterns_in_work(entries)
            patterns = [p["pattern"] for p in detected]
            insights = generate_insights_from_patterns(detected, entries)

        # If no live patterns detected, use cached patterns (work_log may be cleaned)
        if not patterns and self.cache.get("patterns_ever_detected"):
            patterns = self.cache["patterns_ever_detected"][-10:]  # Last 10 patterns

        # If no fresh insights, fallback to cached insights from consolidation state
        # Filter out generic "Recent work has X operations" messages
        if not insights and ENHANCER_AVAILABLE:
            cached_insights = consolidation_engine.state.get("insights_generated", [])
            valuable_insights = [
                i for i in cached_insights
                if i and not i.startswith("Recent work has")
                and len(i) > 50  # Meaningful content
                and not i.endswith("preserved as working procedures.")
            ]
            insights = valuable_insights[-5:]  # Last 5 valuable insights

        # Get recent OBJECTIVE successes (verified, not premature claims)
        successes = []
        if OBJECTIVE_SUCCESS_AVAILABLE and ENHANCER_AVAILABLE:
            try:
                entries = work_log.get_recent_entries(hours=48, include_processed=True)
                detector = ObjectiveSuccessDetector()
                objective_successes = detector.analyze_entries(entries)
                # Only show high-confidence verified successes
                successes = [
                    f"[{s.confidence:.0%}] {s.task[:70]}"
                    for s in objective_successes
                    if s.confidence >= 0.6
                ][:5]
            except Exception as e:
                logger.debug(f"Objective success detection failed: {e}")

        # Fallback to basic success entries if no objective successes
        if not successes and ENHANCER_AVAILABLE:
            success_entries = work_log.get_successes(hours=24)
            successes = [e.get("content", "")[:80] for e in success_entries[-5:]]

        # If still no successes, show cached success count
        if not successes and self.cache.get("successes_captured", 0) > 0:
            successes = [f"Total successes captured: {self.cache['successes_captured']}"]

        # Get critical warnings from knowledge
        warnings = []
        if CONTEXT_DNA_AVAILABLE:
            try:
                memory = ContextDNAClient()
                warning_results = memory.query("critical warning gotcha")
                if warning_results:
                    # Extract first 3 warnings
                    warnings = warning_results.split("\n\n")[:3]
            except Exception as e:
                logger.debug(f"Context DNA warning query failed: {e}")

        # Build state
        state = BrainState(
            last_updated=datetime.now().isoformat(),
            active_patterns=patterns[:5],
            recent_insights=insights[:5],
            critical_warnings=warnings[:3],
            recent_successes=successes,
            areas_active=list(set(patterns))[:8],
            capture_stats=capture_stats,
            consolidation_stats=consolidation_stats
        )

        return state

    def write_brain_state_file(self) -> str:
        """
        Write brain state to markdown file for context injection.

        This file can be included in CLAUDE.md or read by agents.
        """
        state = self.generate_brain_state()

        content = f"""# Architecture Brain State

> Auto-generated: {state.last_updated}
> This file is automatically updated by the Architecture Brain.

## Active Patterns

"""
        if state.active_patterns:
            for pattern in state.active_patterns:
                content += f"- {pattern}\n"
        else:
            content += "_No active patterns detected_\n"

        content += """
## Recent Insights

"""
        if state.recent_insights:
            for i, insight in enumerate(state.recent_insights, 1):
                content += f"{i}. {insight}\n\n"
        else:
            content += "_No recent insights_\n"

        content += """
## Critical Warnings

"""
        if state.critical_warnings:
            for warning in state.critical_warnings:
                content += f"- {warning[:200]}...\n"
        else:
            content += "_No critical warnings_\n"

        content += """
## Recent Successes

"""
        if state.recent_successes:
            for success in state.recent_successes:
                content += f"- {success}\n"
        else:
            content += "_No recent successes logged_\n"

        content += f"""
## System Status

- Captures today: {state.capture_stats.get('captures_today', 0)}
- Consolidations: {state.consolidation_stats.get('consolidation_count', 0)}
- Patterns discovered: {len(state.areas_active)}
- Last consolidation: {state.consolidation_stats.get('last_consolidation', 'Never')}

---
*Use `python memory/brain.py cycle` to refresh this state.*
"""

        with open(BRAIN_STATE_FILE, "w") as f:
            f.write(content)

        return str(BRAIN_STATE_FILE)

    def get_context_for_task(self, task: str) -> str:
        """
        Get relevant context for a specific task.

        This is what agents should call before starting work.
        """
        context_parts = []

        # 1. Get brain state summary
        state = self.generate_brain_state()
        if state.recent_insights:
            context_parts.append("## Relevant Insights")
            for insight in state.recent_insights[:3]:
                context_parts.append(f"- {insight}")

        # 2. Get blueprint if available
        if CONTEXT_AVAILABLE:
            try:
                blueprint = get_blueprint(task)
                if blueprint:
                    context_parts.append("\n## Blueprint Available")
                    context_parts.append(f"Procedures: {len(blueprint.procedures)}")
                    if blueprint.warnings:
                        context_parts.append("Warnings:")
                        for w in blueprint.warnings[:3]:
                            context_parts.append(f"  - {w}")
            except Exception as e:
                logger.debug(f"Blueprint fetch failed: {e}")

        # 3. Get Context DNA learnings
        if CONTEXT_DNA_AVAILABLE:
            try:
                memory = ContextDNAClient()
                learnings = memory.query(task)
                if learnings and len(learnings) > 50:
                    context_parts.append("\n## Relevant Learnings")
                    context_parts.append(learnings[:500] + "...")
            except Exception as e:
                logger.debug(f"Context DNA query failed: {e}")

        return "\n".join(context_parts) if context_parts else "No relevant context found."

    # =========================================================================
    # FULL CYCLE - Run everything
    # =========================================================================

    def run_cycle(self) -> Dict:
        """
        Run a full brain cycle:
        1. Detect OBJECTIVE successes from work stream
        2. Auto-record verified successes to Context DNA
        3. Consolidate any pending work
        4. Update brain state file
        5. Return status

        This is 100% automatic - no human intervention required.
        """
        results = {
            "timestamp": datetime.now().isoformat(),
            "objective_successes": [],
            "successes_recorded": 0,
            "consolidation": None,
            "state_file": None,
            "success": False
        }

        # 1. AUTOMATIC: Detect objective successes from work stream
        # Use EnhancedSuccessDetector if available (multi-layer subconscious processing)
        # Falls back to basic ObjectiveSuccessDetector
        if (ENHANCED_DETECTION_AVAILABLE or OBJECTIVE_SUCCESS_AVAILABLE) and ENHANCER_AVAILABLE:
            try:
                # Get recent entries from work log (the DNA)
                entries = work_log.get_recent_entries(hours=24, include_processed=False)

                # Choose detector: Enhanced (multi-layer) or Basic (regex-only)
                if ENHANCED_DETECTION_AVAILABLE:
                    detector = EnhancedSuccessDetector()
                    successes = detector.analyze_entries(entries)
                    results["detector_type"] = "enhanced"
                else:
                    detector = ObjectiveSuccessDetector()
                    successes = detector.analyze_entries(entries)
                    results["detector_type"] = "basic"

                # Record high-confidence successes automatically
                for s in successes:
                    confidence = getattr(s, 'confidence', 0.5)
                    if confidence >= 0.7:  # Only high-confidence
                        # Get evidence depending on detector type
                        if ENHANCED_DETECTION_AVAILABLE:
                            evidence = getattr(s, 'evidence', [])
                            details = f"Evidence: {', '.join(evidence)}"
                            detection_layers = getattr(s, 'detection_layers', [])
                            if detection_layers:
                                details += f" (layers: {', '.join(detection_layers)})"
                        else:
                            evidence = getattr(s, 'evidence', [])
                            details = f"Evidence: {', '.join(evidence)}"

                        # Auto-record to Context DNA
                        self.capture_win(
                            task=s.task,
                            details=details,
                            area=getattr(s, 'area', 'general')
                        )
                        results["successes_recorded"] += 1
                        results["objective_successes"].append({
                            "task": s.task[:80],
                            "confidence": confidence,
                            "source": getattr(s, 'source', 'enhanced' if ENHANCED_DETECTION_AVAILABLE else 'basic')
                        })

                        # Feed back to pattern registry for learning (subconscious learning)
                        if ENHANCED_DETECTION_AVAILABLE and hasattr(detector, 'learn_from_confirmed'):
                            detector.learn_from_confirmed(s, entries)

                # ALSO capture system-confirmed objective successes (basic detector only)
                if not ENHANCED_DETECTION_AVAILABLE and hasattr(detector, 'get_objective_successes_without_user'):
                    system_wins = detector.get_objective_successes_without_user(min_confidence=0.8)
                    for s in system_wins:
                        # Don't duplicate if already captured
                        if s.task[:80] not in [r["task"] for r in results["objective_successes"]]:
                            self.capture_win(
                                task=s.task,
                                details=f"System evidence: {', '.join(s.evidence)}",
                                area=s.area
                            )
                            results["successes_recorded"] += 1
                            results["objective_successes"].append({
                                "task": s.task[:80],
                                "confidence": s.confidence,
                                "source": "system_confirmed"
                            })

            except Exception as e:
                results["objective_success_error"] = str(e)

        # 2. Consolidate patterns and insights
        if ENHANCER_AVAILABLE:
            results["consolidation"] = self.consolidate()

        # 3. AUTONOMOUS EVOLUTION: Let the system discover new patterns
        # This is where we get SMARTER with every coding session
        if EVOLUTION_AVAILABLE:
            try:
                evolution_result = evolve_patterns()
                results["evolution"] = {
                    "candidates_discovered": evolution_result.get("candidates_discovered", 0),
                    "candidates_updated": evolution_result.get("candidates_updated", 0),
                    "patterns_promoted": evolution_result.get("patterns_promoted", 0),
                }
                if evolution_result.get("patterns_promoted", 0) > 0:
                    print(f"🧬 Brain evolved: {evolution_result['patterns_promoted']} new patterns learned!")
            except Exception as e:
                results["evolution_error"] = str(e)

        # 4. HINDSIGHT VALIDATION: Verify wins from 24h ago didn't become miswirings
        # This is the SECOND validation layer - first is evidence-based, this is time-based
        if HINDSIGHT_AVAILABLE:
            try:
                # Use validator directly for detailed results
                validator = get_validator()
                hindsight_results = validator.run_hindsight_check()

                from memory.hindsight_validator import VerificationStatus
                verified = [r for r in hindsight_results if r.status == VerificationStatus.VERIFIED]
                miswirings = [r for r in hindsight_results if r.status == VerificationStatus.MISWIRING]
                suspects = [r for r in hindsight_results if r.status == VerificationStatus.SUSPECT]

                results["hindsight"] = {
                    "checked": len(hindsight_results),
                    "verified": len(verified),
                    "miswirings": len(miswirings),
                    "suspects": len(suspects),
                    "pending_remaining": validator.get_pending_count()
                }

                if miswirings:
                    print(f"⚠️  Hindsight: {len(miswirings)} wins reclassified as miswirings")
                    for m in miswirings[:3]:  # Show first 3
                        print(f"   - {m.win_id}: {m.reason[:60] if m.reason else 'No reason'}")
                if verified:
                    print(f"✅ Hindsight: {len(verified)} wins verified still working")

            except Exception as e:
                results["hindsight_error"] = str(e)

        # 5. PROFESSOR EVOLUTION: Apply learnings to wisdom domains
        if PROFESSOR_EVOLUTION_AVAILABLE:
            try:
                prof_evo = ProfessorEvolution()
                wisdom_updates = prof_evo.apply_learnings_to_wisdom()
                results["professor_evolution"] = {
                    "updates_applied": len(wisdom_updates) if wisdom_updates else 0
                }
                if wisdom_updates:
                    print(f"📚 Professor: {len(wisdom_updates)} wisdom domain(s) updated")
            except Exception as e:
                results["professor_evolution_error"] = str(e)

        # 6. Update state file
        try:
            results["state_file"] = self.write_brain_state_file()
            results["success"] = True
        except Exception as e:
            results["error"] = str(e)

        return results

    def init_session(self) -> str:
        """
        Initialize a new agent session.

        Call this at the start of any agent session to:
        1. Run a brain cycle
        2. Generate fresh state
        3. Return context summary
        """
        # Run cycle
        self.run_cycle()

        # Generate summary for agent
        state = self.generate_brain_state()

        summary = f"""## Architecture Brain Initialized

**Patterns Active:** {', '.join(state.active_patterns) or 'None'}
**Recent Successes:** {len(state.recent_successes)}
**Insights Available:** {len(state.recent_insights)}

"""
        if state.critical_warnings:
            summary += "**Warnings:**\n"
            for w in state.critical_warnings[:2]:
                summary += f"- {w[:100]}...\n"

        summary += f"\nBrain state written to: {BRAIN_STATE_FILE}"

        return summary


# =============================================================================
# GLOBAL BRAIN INSTANCE
# =============================================================================

brain = ArchitectureBrain()

# Backwards-compatible alias - some code imports Brain instead of ArchitectureBrain
Brain = ArchitectureBrain


# =============================================================================
# CONVENIENCE FUNCTIONS - For easy import
# =============================================================================

def init() -> str:
    """Initialize brain for new session."""
    return brain.init_session()


def win(task: str, details: str = None, area: str = None) -> bool:
    """Record a success."""
    return brain.capture_win(task, details, area)


def fix(error: str, resolution: str, area: str = None) -> bool:
    """Record an error resolution."""
    return brain.capture_fix(error, resolution, area)


def context(task: str) -> str:
    """Get context for a task."""
    return brain.get_context_for_task(task)


def cycle() -> Dict:
    """Run full brain cycle."""
    return brain.run_cycle()


def state() -> str:
    """Get current brain state as markdown."""
    brain.write_brain_state_file()
    return BRAIN_STATE_FILE.read_text()


def user_said(message: str) -> bool:
    """Record what the user said (for objective success detection)."""
    return brain.capture_user_message(message)


def agent_said(message: str) -> bool:
    """Record what the agent said (for conversation mirror)."""
    return brain.capture_agent_message(message)


def sop(title: str, steps: list, warnings: list = None, tags: list = None) -> bool:
    """Record a Standard Operating Procedure."""
    if not SOP_TYPES_AVAILABLE:
        return False
    try:
        registry = SOPRegistry()
        registry.record_sop(title=title, steps=steps, warnings=warnings or [], tags=tags or [])
        return True
    except Exception as e:
        logger.debug(f"SOP recording failed: {e}")
        return False


def gotcha(title: str, when: str, consequence: str, solution: str, tags: list = None) -> bool:
    """Record a gotcha/warning."""
    if not SOP_TYPES_AVAILABLE:
        return False
    try:
        registry = SOPRegistry()
        registry.record_gotcha(
            title=title,
            when_it_happens=when,
            consequence=consequence,
            solution=solution,
            tags=tags or []
        )
        return True
    except Exception as e:
        logger.debug(f"Gotcha recording failed: {e}")
        return False


def pattern(title: str, problem: str, solution: str, example: str = None, tags: list = None) -> bool:
    """Record a recurring pattern."""
    if not SOP_TYPES_AVAILABLE:
        return False
    try:
        registry = SOPRegistry()
        registry.record_pattern(
            title=title,
            problem=problem,
            solution=solution,
            example_code=example,
            tags=tags or []
        )
        return True
    except Exception as e:
        logger.debug(f"Pattern recording failed: {e}")
        return False


def search_gotchas(query: str) -> list:
    """Search for gotchas relevant to a query."""
    if not SOP_TYPES_AVAILABLE:
        return []
    try:
        registry = SOPRegistry()
        return registry.get_gotchas(query, limit=5)
    except Exception as e:
        logger.debug(f"Gotcha search failed: {e}")
        return []


def search_sops(query: str) -> list:
    """Search for SOPs relevant to a query."""
    if not SOP_TYPES_AVAILABLE:
        return []
    try:
        registry = SOPRegistry()
        return registry.get_sops(query, limit=5)
    except Exception as e:
        logger.debug(f"SOP search failed: {e}")
        return []


# =============================================================================
# ROUTE TRACKING - Multi-Route Process SOPs (ADDITIVE)
# =============================================================================

# Route tracker availability
try:
    from memory.route_tracker import (
        record_route_success,
        record_route_failure,
        format_sop_with_routes,
        get_sop_entry,
        load_routes_db
    )
    ROUTE_TRACKER_AVAILABLE = True
except ImportError:
    ROUTE_TRACKER_AVAILABLE = False


def route_success(goal: str, route_description: str, chain: str = "", is_first_try: bool = True) -> str:
    """
    Record a successful route for a process SOP.

    Args:
        goal: The SOP goal (e.g., "Deploy Django to production")
        route_description: Brief description of the route (e.g., "via systemctl restart")
        chain: Full chain format (optional)
        is_first_try: Whether this succeeded on first attempt

    Returns:
        Formatted SOP with updated routes
    """
    if not ROUTE_TRACKER_AVAILABLE:
        return f"[process SOP] {goal}: (passed) {route_description}"

    record_route_success(goal, route_description, chain, is_first_try)
    return format_sop_with_routes(goal, chain)


def route_fail(goal: str, route_description: str, failure_note: str = "") -> str:
    """
    Record a failed route for a process SOP.

    Progressive detail is automatically managed:
    - 1st failure: phrase only
    - 2nd failure: adds context
    - 3rd+ failure: full details

    Args:
        goal: The SOP goal
        route_description: Brief description of the route that failed
        failure_note: What went wrong

    Returns:
        Formatted SOP with updated routes
    """
    if not ROUTE_TRACKER_AVAILABLE:
        return f"[process SOP] {goal}: (failed) {route_description}"

    record_route_failure(goal, route_description, failure_note)
    return format_sop_with_routes(goal)


def route_show(goal: str) -> str:
    """
    Show all routes for a process SOP.

    Returns:
        Formatted SOP with all routes
    """
    if not ROUTE_TRACKER_AVAILABLE:
        return f"Route tracker not available"

    return format_sop_with_routes(goal)


def route_list() -> list:
    """
    List all tracked process SOPs with routes.

    Returns:
        List of (goal, route_count, success_count) tuples
    """
    if not ROUTE_TRACKER_AVAILABLE:
        return []

    db = load_routes_db()
    results = []
    for sop_id, sop in db.get("sops", {}).items():
        route_count = len(sop["routes"])
        success_count = sum(1 for r in sop["routes"] if r["success_count"] > 0)
        results.append((sop["goal"], route_count, success_count))

    return results


def route_generate(task: str, details: str = None) -> str:
    """
    Generate a route-aware process SOP title.

    Uses tracked routes to:
    1. Include ALL successful tools in Zone 2 via (tool1, tool2, tool3)
    2. Use the most preferred/recent route for zones 3-6

    Args:
        task: Original task description
        details: Additional details for zone extraction

    Returns:
        Route-aware process SOP title
    """
    try:
        from memory.process_sop_enhancer import generate_route_aware_process_sop
        result = generate_route_aware_process_sop(task, details)
        return result if result else f"[process SOP] {task}"
    except ImportError:
        # Fall back to basic format
        return f"[process SOP] {task}"


def route_summary(goal: str) -> str:
    """
    Get a summary of route tracking data for a process SOP.

    Args:
        goal: The SOP goal to summarize

    Returns:
        Summary string with route counts, best route, available tools
    """
    try:
        from memory.process_sop_enhancer import get_process_sop_summary
        return get_process_sop_summary(goal)
    except ImportError:
        return f"Route summary not available for: {goal}"


# =============================================================================
# BUGFIX STATUS TRACKING (ADDITIVE)
# =============================================================================

# Bugfix tracker availability
try:
    from memory.bugfix_tracker import (
        record_bugfix_passed,
        record_bugfix_failed,
        format_bugfix_with_status,
        get_bugfix_entry,
        get_regressions,
        check_for_regression,
        generate_regression_alert,
        load_bugfix_db
    )
    BUGFIX_TRACKER_AVAILABLE = True
except ImportError:
    BUGFIX_TRACKER_AVAILABLE = False


def bugfix_passed(title: str, symptom: str = "", fix_description: str = "") -> str:
    """
    Mark a bugfix as currently passing.

    Args:
        title: The bugfix SOP title
        symptom: The original symptom (optional)
        fix_description: The fix that was applied (optional)

    Returns:
        Formatted title with (passed MM/DD/YY) prefix
    """
    if not BUGFIX_TRACKER_AVAILABLE:
        return f"(passed) {title}"

    record_bugfix_passed(title, symptom, fix_description)
    return format_bugfix_with_status(title)


def bugfix_failed(title: str, failure_note: str = "") -> tuple:
    """
    Mark a bugfix as failing (regression).

    Args:
        title: The bugfix SOP title
        failure_note: What went wrong

    Returns:
        Tuple of (formatted_title, is_regression, alert_message)
    """
    if not BUGFIX_TRACKER_AVAILABLE:
        return (f"(failed) {title}", False, "")

    entry, is_regression = record_bugfix_failed(title, failure_note)
    formatted = format_bugfix_with_status(title)

    alert = ""
    if is_regression:
        alert = generate_regression_alert(entry)

    return (formatted, is_regression, alert)


def bugfix_show(title: str) -> str:
    """
    Show status of a specific bugfix.

    Returns:
        Formatted status information
    """
    if not BUGFIX_TRACKER_AVAILABLE:
        return "Bugfix tracker not available"

    entry = get_bugfix_entry(title)
    if not entry:
        return f"No bugfix tracked for: {title}"

    formatted = format_bugfix_with_status(title)
    lines = [
        f"Title: {formatted}",
        f"Status: {entry['status'].upper()}",
        f"Passed: {entry['passed_count']} times",
        f"Failed: {entry['failed_count']} times"
    ]

    if entry.get('symptom'):
        lines.append(f"Symptom: {entry['symptom'][:80]}")
    if entry.get('fix'):
        lines.append(f"Fix: {entry['fix'][:80]}")

    return "\n".join(lines)


def bugfix_list() -> list:
    """
    List all tracked bugfixes.

    Returns:
        List of (title, status, passed_count, failed_count) tuples
    """
    if not BUGFIX_TRACKER_AVAILABLE:
        return []

    db = load_bugfix_db()
    results = []
    for bugfix_id, entry in db.get("bugfixes", {}).items():
        results.append((
            entry["title"],
            entry["status"],
            entry["passed_count"],
            entry["failed_count"]
        ))

    return results


def bugfix_regressions() -> list:
    """
    Get all bugfixes that have regressed (were passing, now failing).

    Returns:
        List of bugfix entries that need attention
    """
    if not BUGFIX_TRACKER_AVAILABLE:
        return []

    return get_regressions()


def bugfix_check_symptom(symptom: str) -> tuple:
    """
    Check if a symptom matches a known bugfix.

    Args:
        symptom: The error/symptom text

    Returns:
        Tuple of (matched_entry, alert) or (None, "")
    """
    if not BUGFIX_TRACKER_AVAILABLE:
        return (None, "")

    match = check_for_regression(symptom)
    if match:
        alert = generate_regression_alert(match)
        return (match, alert)

    return (None, "")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Architecture Brain - Unified Autonomous Learning System")
        print("")
        print("Commands:")
        print("  init                       - Initialize brain for new session")
        print("  cycle                      - Run full consolidation + state update")
        print("  state                      - Show current brain state")
        print("  success <task> [details]   - Record a success")
        print("  fix <error> <resolution>   - Record an error fix")
        print("  context <task>             - Get context for a task")
        print("  status                     - Show brain status")
        print("  route <subcommand>         - Multi-route process SOP tracking")
        print("  bugfix <subcommand>        - Bugfix status tracking")
        print("")
        print("Route Subcommands:")
        print("  route success <goal> <route> [chain]  - Record successful route")
        print("  route fail <goal> <route> [note]      - Record failed route")
        print("  route show <goal>                     - Show routes for SOP")
        print("  route list                            - List all tracked SOPs")
        print("")
        print("Bugfix Subcommands:")
        print("  bugfix passed <title> [symptom] [fix] - Mark bugfix as passing")
        print("  bugfix failed <title> [note]          - Mark bugfix as failed (regression)")
        print("  bugfix show <title>                   - Show bugfix status")
        print("  bugfix list                           - List all tracked bugfixes")
        print("  bugfix regressions                    - Show bugfixes that regressed")
        print("  bugfix check <symptom>                - Check if symptom matches known bugfix")
        print("")
        print("The brain automatically:")
        print("  - Captures successful operations")
        print("  - Consolidates patterns and insights")
        print("  - Generates context for agents")
        print("  - Cleans up processed data")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "init":
        print(brain.init_session())

    elif cmd == "cycle":
        print("Running brain cycle...")
        result = brain.run_cycle()
        print(f"Success: {result['success']}")

        # Show objective successes detected
        if result.get("objective_successes"):
            print(f"\n✅ Objective Successes Detected: {result['successes_recorded']}")
            for s in result["objective_successes"]:
                print(f"  [{s['confidence']:.0%}] {s['task']}")
                print(f"       Source: {s['source']}")
        else:
            print("\nNo new objective successes detected")

        if result.get("consolidation"):
            c = result["consolidation"]
            print(f"\nConsolidation:")
            print(f"  Entries processed: {c.get('entries_processed', 0)}")
            print(f"  Patterns detected: {c.get('patterns_detected', 0)}")

        print(f"\nState file: {result.get('state_file', 'None')}")

    elif cmd == "state":
        brain.write_brain_state_file()
        print(BRAIN_STATE_FILE.read_text())

    elif cmd == "success" or cmd == "win":
        if len(sys.argv) < 3:
            print("Usage: success <task> [details] [--type=bugfix|process]")
            print("  --type flag forces SOP type when auto-detection is ambiguous")
            sys.exit(1)

        # Parse --type flag if present
        explicit_type = None
        args = sys.argv[2:]
        filtered_args = []
        for arg in args:
            if arg.startswith('--type='):
                explicit_type = arg.split('=')[1]
                if explicit_type not in ('bugfix', 'process'):
                    print(f"Invalid --type: {explicit_type}. Use 'bugfix' or 'process'")
                    sys.exit(1)
            else:
                filtered_args.append(arg)

        task = filtered_args[0] if filtered_args else ""
        details = " ".join(filtered_args[1:]) if len(filtered_args) > 1 else None

        # Add explicit type hint to details if provided
        if explicit_type:
            type_hint = f"[EXPLICIT_TYPE:{explicit_type}]"
            details = f"{type_hint} {details}" if details else type_hint

        if brain.capture_win(task, details):
            type_msg = f" (forced: {explicit_type})" if explicit_type else ""
            print(f"Recorded success{type_msg}: {task}")
        else:
            print("Failed to record (capture system not available)")

    elif cmd == "fix":
        if len(sys.argv) < 4:
            print("Usage: fix <error> <resolution>")
            sys.exit(1)
        error = sys.argv[2]
        resolution = " ".join(sys.argv[3:])
        if brain.capture_fix(error, resolution):
            print(f"Recorded fix: {error[:50]}...")
        else:
            print("Failed to record (capture system not available)")

    elif cmd == "context":
        if len(sys.argv) < 3:
            print("Usage: context <task description>")
            sys.exit(1)
        task = " ".join(sys.argv[2:])
        print(brain.get_context_for_task(task))

    elif cmd == "status":
        print("=== Architecture Brain Status ===")
        print(f"Cycles run: {brain.cache.get('cycles_run', 0)}")
        print(f"Last cycle: {brain.cache.get('last_cycle', 'Never')}")
        print(f"Successes captured: {brain.cache.get('successes_captured', 0)}")
        print(f"Patterns discovered: {len(brain.cache.get('patterns_ever_detected', []))}")
        print("")
        print("Component availability:")
        print(f"  Context DNA API: {'✅' if CONTEXT_DNA_AVAILABLE else '❌'}")
        print(f"  Auto-capture: {'✅' if AUTO_CAPTURE_AVAILABLE else '❌'}")
        print(f"  Enhancer: {'✅' if ENHANCER_AVAILABLE else '❌'}")
        print(f"  Context: {'✅' if CONTEXT_AVAILABLE else '❌'}")
        print(f"  Knowledge Graph: {'✅' if KNOWLEDGE_GRAPH_AVAILABLE else '❌'}")
        print(f"  Objective Success: {'✅' if OBJECTIVE_SUCCESS_AVAILABLE else '❌'}")
        print(f"  SOP Types: {'✅' if SOP_TYPES_AVAILABLE else '❌'}")
        print("")
        print("Automation level:")
        auto_count = sum([
            CONTEXT_DNA_AVAILABLE,
            AUTO_CAPTURE_AVAILABLE,
            ENHANCER_AVAILABLE,
            OBJECTIVE_SUCCESS_AVAILABLE,
            SOP_TYPES_AVAILABLE
        ])
        automation_pct = (auto_count / 5) * 100
        print(f"  {automation_pct:.0f}% ({auto_count}/5 core systems active)")

    elif cmd == "route":
        if len(sys.argv) < 3:
            print("Usage: route <subcommand> [args]")
            print("")
            print("Subcommands:")
            print("  success <goal> <route> [chain]  - Record successful route")
            print("  fail <goal> <route> [note]      - Record failed route")
            print("  show <goal>                     - Show routes for SOP")
            print("  list                            - List all tracked SOPs")
            print("  generate <task> [details]       - Generate route-aware SOP title")
            print("  summary <goal>                  - Show route summary for SOP")
            print("")
            print("Examples:")
            print('  route success "Deploy Django" "via systemctl" "via (systemctl) -> restart -> healthy"')
            print('  route fail "Deploy Django" "docker restart" "loses env vars"')
            print('  route show "Deploy Django"')
            print('  route generate "Deploy Django to production"')
            print('  route summary "Deploy Django to production"')
            sys.exit(1)

        subcmd = sys.argv[2]

        if subcmd == "success":
            if len(sys.argv) < 5:
                print("Usage: route success <goal> <route_description> [chain]")
                sys.exit(1)
            goal = sys.argv[3]
            route_desc = sys.argv[4]
            chain = sys.argv[5] if len(sys.argv) > 5 else ""

            result = route_success(goal, route_desc, chain)
            print(f"Recorded successful route: {route_desc}")
            print("")
            print(result)

        elif subcmd == "fail":
            if len(sys.argv) < 5:
                print("Usage: route fail <goal> <route_description> [failure_note]")
                sys.exit(1)
            goal = sys.argv[3]
            route_desc = sys.argv[4]
            note = " ".join(sys.argv[5:]) if len(sys.argv) > 5 else ""

            result = route_fail(goal, route_desc, note)
            print(f"Recorded failed route: {route_desc}")
            print("")
            print(result)

        elif subcmd == "show":
            if len(sys.argv) < 4:
                print("Usage: route show <goal>")
                sys.exit(1)
            goal = sys.argv[3]

            result = route_show(goal)
            if result:
                print(result)
            else:
                print(f"No routes tracked for: {goal}")

        elif subcmd == "list":
            results = route_list()
            if not results:
                print("No SOPs with routes tracked yet.")
            else:
                print("Tracked Process SOPs with Routes:")
                print("")
                for goal, route_count, success_count in results:
                    print(f"  {goal}")
                    print(f"    Routes: {route_count} ({success_count} successful)")
                    print("")

        elif subcmd == "generate":
            if len(sys.argv) < 4:
                print("Usage: route generate <task> [details]")
                sys.exit(1)
            task = sys.argv[3]
            details = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else None

            result = route_generate(task, details)
            print(result)

        elif subcmd == "summary":
            if len(sys.argv) < 4:
                print("Usage: route summary <goal>")
                sys.exit(1)
            goal = sys.argv[3]

            result = route_summary(goal)
            print(result)

        else:
            print(f"Unknown route subcommand: {subcmd}")
            sys.exit(1)

    elif cmd == "bugfix":
        if len(sys.argv) < 3:
            print("Usage: bugfix <subcommand> [args]")
            print("")
            print("Subcommands:")
            print("  passed <title> [symptom] [fix]  - Mark bugfix as passing")
            print("  failed <title> [note]           - Mark bugfix as failed (regression)")
            print("  show <title>                    - Show bugfix status")
            print("  list                            - List all tracked bugfixes")
            print("  regressions                     - Show bugfixes that regressed")
            print("  check <symptom>                 - Check if symptom matches known bugfix")
            print("")
            print("Examples:")
            print('  bugfix passed "Async blocking in LLM" "LLM hanging" "Use asyncio.to_thread()"')
            print('  bugfix failed "Async blocking in LLM" "Still hanging after update"')
            print('  bugfix check "LLM service hanging"')
            sys.exit(1)

        subcmd = sys.argv[2]

        if subcmd == "passed":
            if len(sys.argv) < 4:
                print("Usage: bugfix passed <title> [symptom] [fix]")
                sys.exit(1)
            title = sys.argv[3]
            symptom = sys.argv[4] if len(sys.argv) > 4 else ""
            fix_desc = sys.argv[5] if len(sys.argv) > 5 else ""

            result = bugfix_passed(title, symptom, fix_desc)
            print(f"Marked bugfix as PASSED")
            print(f"  {result}")

        elif subcmd == "failed":
            if len(sys.argv) < 4:
                print("Usage: bugfix failed <title> [failure_note]")
                sys.exit(1)
            title = sys.argv[3]
            note = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""

            formatted, is_regression, alert = bugfix_failed(title, note)

            if is_regression:
                print("REGRESSION DETECTED!")
                print("")
                print(alert)
            else:
                print(f"Marked bugfix as FAILED")

            print(f"\n{formatted}")

        elif subcmd == "show":
            if len(sys.argv) < 4:
                print("Usage: bugfix show <title>")
                sys.exit(1)
            title = sys.argv[3]

            result = bugfix_show(title)
            print(result)

        elif subcmd == "list":
            results = bugfix_list()
            if not results:
                print("No bugfixes tracked yet.")
            else:
                print("Tracked Bugfixes:")
                print("")
                for title, status, passed_count, failed_count in results:
                    status_marker = "pass" if status == "passed" else "FAIL"
                    print(f"  [{status_marker}] {title[:60]}...")
                    print(f"       Passed: {passed_count}, Failed: {failed_count}")
                    print("")

        elif subcmd == "regressions":
            regressions = bugfix_regressions()
            if not regressions:
                print("No regressions detected. All bugfixes are passing.")
            else:
                print(f"REGRESSIONS DETECTED: {len(regressions)} bugfix(es) need attention")
                print("")
                for entry in regressions:
                    from memory.bugfix_tracker import generate_regression_alert
                    print(generate_regression_alert(entry))
                    print("-" * 50)

        elif subcmd == "check":
            if len(sys.argv) < 4:
                print("Usage: bugfix check <symptom_text>")
                sys.exit(1)
            symptom = " ".join(sys.argv[3:])

            match, alert = bugfix_check_symptom(symptom)
            if match:
                print("MATCH FOUND - This may be a regression!")
                print("")
                print(alert)
            else:
                print("No matching bugfix found for this symptom.")

        else:
            print(f"Unknown bugfix subcommand: {subcmd}")
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
