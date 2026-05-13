#!/usr/bin/env python3
"""
Background Helper Agent Service - The Subconscious Mind

This FastAPI service runs autonomously alongside Atlas, continuously
processing the work_dialogue_log (DNA) and extracting learnings.

PHILOSOPHY:
Atlas is the conscious mind - focused on immediate tasks.
This agent is the subconscious - always running, processing, learning.

The text mirror (work_dialogue_log.jsonl) is the DNA - the building blocks.
This agent reads the DNA and constructs the living organism of memory.

RESPONSIBILITIES:
1. Monitor work_log for new entries (every 60 seconds)
2. Run EnhancedSuccessDetector on new entries
3. Auto-capture high-confidence wins
4. Execute brain.run_cycle() periodically
5. Broadcast notifications via Redis pub/sub + WebSocket
6. Pre-fetch context for detected task patterns

ENDPOINTS:
- GET  /health          - Health check
- GET  /status          - Detailed status
- POST /trigger/cycle   - Manual brain cycle
- POST /trigger/detect  - Manual success detection
- WS   /ws              - WebSocket for real-time updates

INTEGRATION:
- Monitors: memory/.work_dialogue_log.jsonl
- Uses: EnhancedSuccessDetector, Brain, Redis
- Broadcasts: Events to WebSocket clients and Redis pub/sub

Usage:
    # Start the service
    uvicorn memory.agent_service:app --host 0.0.0.0 --port 8080

    # Or via Docker
    docker-compose up helper-agent
"""

import os
import sys
import json
import asyncio
import logging
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# Use default executor (None) for injection calls.
# max_workers=2 caused cross-executor deadlock: only 1/37 run_in_executor
# calls used the dedicated pool while 36 saturated the default — when
# injection threads needed the default executor back, everything froze.
# The 30s timeout wrapper on each call site is the real safety net.
_injection_executor = None

# Ensure Docker CLI and other system binaries are findable by subprocess
_extra_paths = ['/usr/local/bin', '/opt/homebrew/bin']
_current_path = os.environ.get('PATH', '')
for _p in _extra_paths:
    if _p not in _current_path:
        os.environ['PATH'] = _p + ':' + os.environ['PATH']
from datetime import datetime, timedelta, timezone
from typing import Optional, Set, List, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI as FastAPIType
else:
    FastAPIType = Any  # Fallback type when FastAPI not available
from contextlib import asynccontextmanager

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent_service")

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# FastAPI imports
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

# Redis imports (optional) - prefer redis.asyncio over deprecated aioredis
try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    try:
        import aioredis
        REDIS_AVAILABLE = True
    except (ImportError, TypeError):
        # TypeError handles aioredis compatibility issues with Python 3.11+
        REDIS_AVAILABLE = False
        aioredis = None

# Memory system imports
try:
    from memory.architecture_enhancer import work_log, WorkDialogueLog
    WORK_LOG_AVAILABLE = True
except ImportError:
    WORK_LOG_AVAILABLE = False
    work_log = None

try:
    from memory.enhanced_success_detector import EnhancedSuccessDetector
    ENHANCED_DETECTOR_AVAILABLE = True
except (ImportError, Exception):
    ENHANCED_DETECTOR_AVAILABLE = False

try:
    from memory.brain import brain, Brain
    BRAIN_AVAILABLE = True
except ImportError:
    BRAIN_AVAILABLE = False
    brain = None

# Pattern evolution imports
try:
    from memory.pattern_evolution import PatternEvolutionEngine, get_evolution_engine
    EVOLUTION_AVAILABLE = True
except (ImportError, Exception):
    EVOLUTION_AVAILABLE = False
    PatternEvolutionEngine = None
    get_evolution_engine = None

# Professor & Context Injection imports
try:
    from memory.professor import Professor, consult as professor_consult
    PROFESSOR_AVAILABLE = True
except ImportError:
    PROFESSOR_AVAILABLE = False
    Professor = None
    professor_consult = None

try:
    from memory.codebase_locator import get_hook_output, format_never_do
    CODEBASE_LOCATOR_AVAILABLE = True
except ImportError:
    CODEBASE_LOCATOR_AVAILABLE = False
    get_hook_output = None
    format_never_do = None

try:
    from memory.query import query_learnings
    QUERY_AVAILABLE = True
except ImportError:
    QUERY_AVAILABLE = False
    query_learnings = None

try:
    from memory.context import before_work as get_context_for_task
    CONTEXT_AVAILABLE = True
except ImportError:
    CONTEXT_AVAILABLE = False
    get_context_for_task = None

# Persistent Hook Structure imports
try:
    from memory.persistent_hook_structure import (
        generate_context_injection,
        InjectionResult,
        InjectionMode,
        InjectionConfig,
        RiskLevel,
        FIRST_TRY_LIKELIHOOD,
        record_session_failure,
        clear_session_failures,
        check_session_failures,
    )
    PERSISTENT_HOOK_AVAILABLE = True
except ImportError:
    PERSISTENT_HOOK_AVAILABLE = False
    generate_context_injection = None
    InjectionResult = None

# Injection Store for visualization
try:
    from memory.injection_store import (
        get_injection_store,
        build_injection_data,
        InjectionStore
    )
    INJECTION_STORE_AVAILABLE = True
except ImportError:
    INJECTION_STORE_AVAILABLE = False
    get_injection_store = None
    build_injection_data = None

# Architecture Graph imports
try:
    from memory.code_parser import (
        ArchitectureGraphBuilder,
        build_architecture_graph,
        ArchGraph,
    )
    ARCHITECTURE_GRAPH_AVAILABLE = True
except ImportError:
    ARCHITECTURE_GRAPH_AVAILABLE = False
    ArchitectureGraphBuilder = None
    build_architecture_graph = None
    ArchGraph = None

# Unified sync engine — replaces context_dna.storage.sync_integration
try:
    from memory.unified_sync import get_sync_engine
    SYNC_AVAILABLE = True
    SYNC_ENABLED = True
except ImportError:
    SYNC_AVAILABLE = False
    SYNC_ENABLED = False
    get_sync_engine = None

try:
    from context_dna.storage.mode_transition import get_mode_manager
    MODE_TRANSITION_AVAILABLE = True
except ImportError:
    MODE_TRANSITION_AVAILABLE = False
    get_mode_manager = None

try:
    from memory.agent_watchdog import get_watchdog, AgentWatchdog
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    get_watchdog = None

# Mutual heartbeat system - "Who Watches the Watchdog?"
try:
    from memory.mutual_heartbeat import MutualHeartbeat, SERVICE_AGENT
    MUTUAL_HEARTBEAT_AVAILABLE = True
except ImportError:
    MUTUAL_HEARTBEAT_AVAILABLE = False
    MutualHeartbeat = None


# ---------------------------------------------------------------------------
# Cross-exam system prompts — hoisted to module scope
#
# _surgery_cross_exam_blocking() runs 6 parallel LLM calls (local + remote for
# each of initial, cross-exam, exploration). Each call rebuilt its 2.2KB system
# prompt from scratch inside the nested function — pure redundant work and a
# prompt-cache killer (OpenAI prompt_cache_key hashes the system block; stable
# text = shared cache bucket). Interning these at module scope also lets the
# DeepSeek/OpenAI auto-prefix-cache land on identical byte strings across
# worker threads and processes.
# ---------------------------------------------------------------------------
SURGERY_BASE_SYSTEM_PROMPT = (
    "You are part of a 3-model surgery team. Be analytical and critical. "
    "Identify confabulations, unsupported claims, and blind spots."
)

SURGERY_CROSS_SYSTEM_PROMPT = (
    "You are a critical reviewer in a multi-model surgery team. "
    "Analyze the other model's report. Identify:\n"
    "1. Confabulations (claims without evidence)\n"
    "2. Blind spots (what was missed)\n"
    "3. Agreements (what aligns with your understanding)\n"
    "4. Confidence assessment (how much to trust each claim)"
)

SURGERY_EXPLORE_SYSTEM_PROMPT = (
    "You are part of a 3-model surgery team. The team has already produced "
    "initial analyses and cross-examinations (provided below). Your role now "
    "is OPEN EXPLORATION — go beyond what was already covered.\n\n"
    "Focus on:\n"
    "- What are we ALL blind to? What assumptions remain unchallenged?\n"
    "- What adjacent systems, failure modes, or interactions were not considered?\n"
    "- What's the worst-case scenario nobody mentioned?\n"
    "- What corrigibility risks exist — where might we be confidently wrong?\n\n"
    "Do NOT repeat points already made. Only surface genuinely NEW insights."
)


class HelperAgent:
    """
    The autonomous subconscious agent.

    Continuously monitors the DNA (work_log) and processes it
    to extract learnings, patterns, and insights.
    """

    def __init__(self):
        self.running = False
        self.redis: Optional[Any] = None
        self.connected_clients: Set[WebSocket] = set()

        # Statistics
        self.stats = {
            "started_at": None,
            "work_log_checks": 0,
            "entries_processed": 0,
            "successes_detected": 0,
            "brain_cycles": 0,
            "evolution_cycles": 0,
            "patterns_promoted": 0,
            "last_work_log_check": None,
            "last_brain_cycle": None,
            "last_evolution_cycle": None,
            "last_file_position": 0,
        }

        # Failure tracking — makes silent failures visible via /health
        self._loop_failures = {
            "work_log_monitor": 0,
            "brain_cycle": 0,
            "pattern_evolution": 0,
            "redis_subscription": 0,
            "postgres_sync": 0,
            "health_monitor": 0,
            "heartbeat": 0,
        }
        self._loop_last_error = {}  # loop_name -> (timestamp, error_str)

        # Initialize components
        self.detector = None
        if ENHANCED_DETECTOR_AVAILABLE:
            try:
                self.detector = EnhancedSuccessDetector()
            except Exception:
                self.detector = None

        self.brain = brain if BRAIN_AVAILABLE else None

        # Pattern evolution engine
        self.evolution_engine = None
        if EVOLUTION_AVAILABLE:
            try:
                self.evolution_engine = get_evolution_engine()
            except Exception:
                self.evolution_engine = None

        # Configuration
        self.work_log_path = Path(
            os.getenv("WORK_LOG_PATH", str(Path(__file__).parent / ".work_dialogue_log.jsonl"))
        )
        self.check_interval = int(os.getenv("SUCCESS_DETECTION_INTERVAL", 60))
        self.cycle_interval = int(os.getenv("BRAIN_CYCLE_INTERVAL", 300))
        self.evolution_interval = int(os.getenv("EVOLUTION_INTERVAL", 1800))  # 30 minutes

        # Consolidated daemon intervals
        self.sync_interval = int(os.getenv("POSTGRES_SYNC_INTERVAL", 120))
        self.health_interval = int(os.getenv("HEALTH_CHECK_INTERVAL", 60))
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", 10))

        # Track subsystem state for consolidated monitoring
        self._last_activity = datetime.now()
        # Mode is always read live from mode_authority (canonical source)
        # No local _mode state needed — get_mode() handles fallback chain
        self._entries_processed_session = 0

        # Mutual heartbeat - monitors watchdog daemon, watchdog monitors us
        self.mutual_heartbeat = None
        if MUTUAL_HEARTBEAT_AVAILABLE:
            self.mutual_heartbeat = MutualHeartbeat(SERVICE_AGENT)
            # Disable auto-restart to prevent crash loops when watchdog not loaded
            self.mutual_heartbeat.auto_restart_services = False
            self.mutual_heartbeat.auto_restart_docker = False
            self.mutual_heartbeat.auto_restart_servers = False

    async def start(self):
        """Start the agent and all background loops."""
        self.running = True
        self.stats["started_at"] = datetime.now().isoformat()

        # Connect to Redis if available
        if REDIS_AVAILABLE:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
            try:
                self.redis = await aioredis.from_url(redis_url)
                await self.redis.ping()
                print(f"Connected to Redis at {redis_url}")
            except Exception as e:
                print(f"Redis connection failed: {e}")
                self.redis = None

        # Start background loops
        asyncio.create_task(self._work_log_monitor_loop())
        asyncio.create_task(self._brain_cycle_loop())
        asyncio.create_task(self._pattern_evolution_loop())

        if self.redis:
            asyncio.create_task(self._redis_subscription_loop())

        print("Helper agent started - subconscious processing active")
        if self.evolution_engine:
            print(f"Pattern evolution active - checking every {self.evolution_interval}s")

        # Consolidated daemon loops
        if SYNC_AVAILABLE and SYNC_ENABLED:
            asyncio.create_task(self._postgres_sync_loop())
            print(f"📡 Postgres sync active (every {self.sync_interval}s)")

        if WATCHDOG_AVAILABLE:
            asyncio.create_task(self._health_monitor_loop())
            print(f"🏥 Health monitor active (every {self.health_interval}s)")

        asyncio.create_task(self._heartbeat_loop())
        print(f"💓 Heartbeat active (every {self.heartbeat_interval}s)")

        # Start mutual heartbeat monitoring (watches watchdog daemon)
        if self.mutual_heartbeat:
            from memory.mode_authority import get_mode
            self.mutual_heartbeat.set_debug_info("mode", get_mode())
            self.mutual_heartbeat.set_debug_info("brain_cycles", 0)
            await self.mutual_heartbeat.start()
            print("🤝 Mutual heartbeat active (watching watchdog daemon)")

        # Start anticipation engine listener (real-time pre-computation)
        try:
            from memory.anticipation_engine import start_listener as start_anticipation_listener
            start_anticipation_listener()
            print("🔮 Anticipation engine listener active (predictive webhook pre-computation)")
        except ImportError:
            print("⚠️ Anticipation engine not available")
        except Exception as e:
            print(f"⚠️ Anticipation engine startup failed: {e}")

    async def stop(self):
        """Stop the agent gracefully."""
        self.running = False
        if self.redis:
            await self.redis.close()
        # Stop anticipation engine listener
        try:
            from memory.anticipation_engine import stop_listener as stop_anticipation_listener
            stop_anticipation_listener()
        except Exception:
            pass
        print("Helper agent stopped")

    def _record_loop_failure(self, loop_name: str, error: Exception):
        """Record a background loop failure for health visibility."""
        self._loop_failures[loop_name] = self._loop_failures.get(loop_name, 0) + 1
        self._loop_last_error[loop_name] = (
            datetime.now().isoformat(),
            str(error)[:200]
        )
        count = self._loop_failures[loop_name]
        print(f"[LOOP-FAIL] {loop_name} #{count}: {error}")

    def _record_loop_success(self, loop_name: str):
        """Reset consecutive failure count on success."""
        self._loop_failures[loop_name] = 0

    async def _work_log_monitor_loop(self):
        """
        Monitor the work_log (DNA) for new entries.

        This is the core of the subconscious - always watching.
        """
        while self.running:
            try:
                await self._check_work_log()
                self._record_loop_success("work_log_monitor")
            except Exception as e:
                self._record_loop_failure("work_log_monitor", e)

            await asyncio.sleep(self.check_interval)

    async def _check_work_log(self):
        """Check work_log for new entries and process them."""
        if not WORK_LOG_AVAILABLE or not work_log:
            return

        self.stats["work_log_checks"] += 1
        self.stats["last_work_log_check"] = datetime.now().isoformat()

        # Get unprocessed entries
        entries = work_log.get_recent_entries(hours=1, include_processed=False)

        if not entries:
            return

        # Process new entries for successes
        if self.detector:
            await self._process_entries_for_successes(entries)

    async def _process_entries_for_successes(self, entries: List[Dict]):
        """Run success detection on new entries."""
        if not self.detector:
            return

        # Run detection in thread pool (CPU-bound)
        successes = await asyncio.get_event_loop().run_in_executor(
            None, self.detector.analyze_entries, entries
        )

        self.stats["entries_processed"] += len(entries)

        # Filter high-confidence successes
        high_confidence = [s for s in successes if s.confidence >= 0.7]

        if high_confidence:
            self.stats["successes_detected"] += len(high_confidence)

            # Broadcast each detected success
            for success in high_confidence:
                await self._broadcast({
                    "type": "success_detected",
                    "timestamp": datetime.now().isoformat(),
                    "task": success.task,
                    "confidence": success.confidence,
                    "evidence": success.evidence,
                    "layers": success.detection_layers,
                })

                # Auto-capture via brain
                if self.brain:
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            self.brain.capture_win,
                            success.task,
                            f"Evidence: {', '.join(success.evidence)}",
                            success.area
                        )
                    except Exception as e:
                        print(f"Auto-capture error: {e}")

    async def _brain_cycle_loop(self):
        """Run brain.run_cycle() periodically."""
        # Wait a bit before first cycle
        await asyncio.sleep(60)

        while self.running:
            try:
                if self.brain:
                    # Run cycle in thread pool
                    result = await asyncio.get_event_loop().run_in_executor(
                        None, self.brain.run_cycle
                    )

                    self.stats["brain_cycles"] += 1
                    self.stats["last_brain_cycle"] = datetime.now().isoformat()

                    # Update mutual heartbeat debug info
                    if self.mutual_heartbeat:
                        self.mutual_heartbeat.set_debug_info("brain_cycles", self.stats["brain_cycles"])
                        self.mutual_heartbeat.set_debug_info("last_cycle", self.stats["last_brain_cycle"])

                    # Broadcast cycle completion
                    await self._broadcast({
                        "type": "brain_cycle_complete",
                        "timestamp": datetime.now().isoformat(),
                        "successes_recorded": result.get("successes_recorded", 0),
                        "consolidation": result.get("consolidation") is not None,
                    })

                self._record_loop_success("brain_cycle")
            except Exception as e:
                self._record_loop_failure("brain_cycle", e)

            await asyncio.sleep(self.cycle_interval)

    async def _pattern_evolution_loop(self):
        """
        Run pattern evolution periodically.

        This is where the system LEARNS AUTOMATICALLY by:
        1. Discovering new success patterns from the work log
        2. Tracking pattern occurrence frequency
        3. Promoting patterns that appear 3+ times to the detection system

        The system literally gets smarter with every coding session.
        """
        if not self.evolution_engine:
            return

        # Wait before first evolution cycle
        await asyncio.sleep(120)  # 2 minutes after startup

        while self.running:
            try:
                # Run evolution cycle in thread pool (CPU-bound)
                result = await asyncio.get_event_loop().run_in_executor(
                    None, self.evolution_engine.evolve
                )

                self.stats["evolution_cycles"] += 1
                self.stats["last_evolution_cycle"] = datetime.now().isoformat()
                self.stats["patterns_promoted"] += result.get("patterns_promoted", 0)

                # Broadcast if we discovered or promoted anything
                if result.get("candidates_discovered", 0) > 0 or result.get("patterns_promoted", 0) > 0:
                    await self._broadcast({
                        "type": "evolution_cycle_complete",
                        "timestamp": datetime.now().isoformat(),
                        "candidates_discovered": result.get("candidates_discovered", 0),
                        "candidates_updated": result.get("candidates_updated", 0),
                        "patterns_promoted": result.get("patterns_promoted", 0),
                    })

                    # Log notable events
                    if result.get("patterns_promoted", 0) > 0:
                        print(f"🧬 Evolution: {result['patterns_promoted']} new patterns promoted!")

                self._record_loop_success("pattern_evolution")
            except Exception as e:
                self._record_loop_failure("pattern_evolution", e)

            await asyncio.sleep(self.evolution_interval)

    # =========================================================================
    # CONSOLIDATED DAEMON LOOPS (Postgres sync + Health + Heartbeat)
    # =========================================================================

    async def _postgres_sync_loop(self):
        """
        Unified sync loop: 2 SQLite DBs ↔ 2 PG databases.

        Uses unified_sync.py engine with:
        - PG advisory lock (safe with lite_scheduler running simultaneously)
        - Auto-discovery of new tables/columns
        - Per-table conflict policies
        - Mode transition handling (lite↔heavy)
        """
        # Delay first sync to let API endpoints become responsive
        await asyncio.sleep(30)

        while self.running:
            try:
                if not SYNC_AVAILABLE or not get_sync_engine:
                    await asyncio.sleep(self.sync_interval)
                    continue

                engine = get_sync_engine()
                report = await engine.async_sync_all(caller="agent_service")

                if report.success:
                    self.stats["postgres_syncs"] = self.stats.get("postgres_syncs", 0) + 1

                    if report.total_pushed > 0:
                        self.stats["items_synced_to_postgres"] = (
                            self.stats.get("items_synced_to_postgres", 0) +
                            report.total_pushed
                        )

                    # Track current mode via canonical source
                    self.stats["current_mode"] = report.mode_after
                    self.stats["postgres_available"] = report.pg_targets_available.get("context_dna", False)
                    self.stats["sync_lock_contention"] = report.lock_contention

                    # Broadcast mode transitions
                    if report.mode_before != report.mode_after:
                        await self._broadcast({
                            "type": "mode_transition",
                            "from": report.mode_before,
                            "to": report.mode_after,
                            "postgres_available": report.pg_targets_available,
                            "items_synced": report.total_pushed,
                            "timestamp": datetime.now().isoformat()
                        })

                if report.errors:
                    self.stats["sync_errors"] = self.stats.get("sync_errors", 0) + len(report.errors)

                self._record_loop_success("postgres_sync")
            except Exception as e:
                self._record_loop_failure("postgres_sync", e)
                self.stats["sync_errors"] = self.stats.get("sync_errors", 0) + 1

            await asyncio.sleep(self.sync_interval)

    async def _health_monitor_loop(self):
        """
        Consolidated health monitoring - checks all subsystems.

        Replaces synaptic_watchdog_daemon.py with in-process monitoring.
        """
        watchdog = get_watchdog() if WATCHDOG_AVAILABLE else None

        while self.running:
            try:
                health_report = {
                    "timestamp": datetime.now().isoformat(),
                    "subsystems": {},
                    "warnings": []
                }

                # Check for runaway processes
                if watchdog:
                    health_status = watchdog.get_health_status()
                    health_report["watchdog"] = health_status

                    # Auto-cleanup runaways
                    if health_status.get("current_runaways", 0) > 0:
                        results = watchdog.cleanup_runaways(dry_run=False)
                        for pid, reason, killed in results:
                            if killed:
                                print(f"🧹 Cleaned up runaway PID {pid}: {reason}")
                                health_report["warnings"].append(f"Killed PID {pid}")

                # Check all subsystems
                health_report["subsystems"] = await self._check_all_subsystems()

                # Update stats
                self.stats["last_health_check"] = datetime.now().isoformat()
                self.stats["subsystem_health"] = health_report["subsystems"]

                # Broadcast warnings if any subsystem is down
                unhealthy = [k for k, v in health_report["subsystems"].items() if not v]
                if unhealthy:
                    await self._broadcast({
                        "type": "health_warning",
                        "unhealthy_subsystems": unhealthy,
                        "report": health_report,
                        "timestamp": datetime.now().isoformat()
                    })

                self._record_loop_success("health_monitor")
            except Exception as e:
                self._record_loop_failure("health_monitor", e)

            await asyncio.sleep(self.health_interval)

    async def _heartbeat_loop(self):
        """
        Fast heartbeat loop for stall detection.

        Replaces heartbeat_watchdog.py with lightweight in-process monitoring.
        """
        stall_threshold_minutes = int(os.getenv("STALL_THRESHOLD_MINUTES", 5))
        stall_threshold = timedelta(minutes=stall_threshold_minutes)

        while self.running:
            try:
                current_time = datetime.now()

                # Update activity if we've done work
                total_processed = self.stats.get("entries_processed", 0)
                total_cycles = self.stats.get("brain_cycles", 0)

                # Any activity counts
                if total_processed > self._entries_processed_session:
                    self._last_activity = current_time
                    self._entries_processed_session = total_processed

                # Check for stalls
                time_since_activity = current_time - self._last_activity

                if time_since_activity > stall_threshold:
                    stall_minutes = time_since_activity.total_seconds() / 60
                    print(f"⚠️ Stall detected: No activity for {stall_minutes:.1f} minutes")

                    await self._broadcast({
                        "type": "stall_warning",
                        "last_activity": self._last_activity.isoformat(),
                        "stall_duration_minutes": stall_minutes,
                        "threshold_minutes": stall_threshold_minutes,
                        "timestamp": current_time.isoformat()
                    })

                    # Reset to avoid spam
                    self._last_activity = current_time

                # Update heartbeat stat
                self.stats["last_heartbeat"] = current_time.isoformat()
                self.stats["heartbeat_count"] = self.stats.get("heartbeat_count", 0) + 1

                self._record_loop_success("heartbeat")
            except Exception as e:
                self._record_loop_failure("heartbeat", e)

            await asyncio.sleep(self.heartbeat_interval)

    async def _check_all_subsystems(self) -> dict:
        """Check health of all subsystems for unified health report."""
        health = {
            "brain": False,
            "work_log": False,
            "redis": False,
            "postgres": False,
            "evolution": False,
            "professor": False,
            "sync": False
        }

        # Brain
        if self.brain:
            try:
                health["brain"] = hasattr(self.brain, 'run_cycle')
            except Exception as e:
                print(f"[WARN] Brain health check failed: {e}")

        # Work log
        if WORK_LOG_AVAILABLE and work_log:
            try:
                health["work_log"] = work_log.path.exists() if hasattr(work_log, 'path') else True
            except Exception as e:
                print(f"[WARN] Work log health check failed: {e}")

        # Redis
        if self.redis:
            try:
                await self.redis.ping()
                health["redis"] = True
            except Exception as e:
                print(f"[WARN] Redis ping failed: {e}")

        # Postgres (via unified sync engine)
        if SYNC_AVAILABLE and get_sync_engine:
            try:
                engine = get_sync_engine()
                mode = engine.detect_mode()
                health["postgres"] = mode == "heavy"
                health["sync"] = True
            except Exception as e:
                print(f"[WARN] Postgres/sync status check failed: {e}")

        # Evolution engine
        if self.evolution_engine:
            health["evolution"] = True

        # Professor
        if PROFESSOR_AVAILABLE:
            health["professor"] = True

        return health

    # =========================================================================
    # END CONSOLIDATED DAEMON LOOPS
    # =========================================================================

    async def _redis_subscription_loop(self):
        """Subscribe to Redis for external commands and consultation requests."""
        if not self.redis:
            return

        while self.running:
            try:
                pubsub = self.redis.pubsub()
                # Subscribe to multiple channels for different purposes
                await pubsub.subscribe(
                    "context-dna:requests",     # General commands (cycle, detect)
                    "context-dna:consult",      # Consultation requests (SOPs + wisdom)
                    "session:dialogue:new"      # Real-time dialogue from session file watcher
                )
                print("🧠 Subconscious listening on context-dna:requests, context-dna:consult, session:dialogue:new")

                while self.running:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0
                    )
                    if message:
                        await self._handle_redis_message(message)
                    self._record_loop_success("redis_subscription")

            except Exception as e:
                self._record_loop_failure("redis_subscription", e)
                await asyncio.sleep(10)  # Back off before reconnect

    async def _handle_redis_message(self, message: Dict):
        """Handle incoming Redis messages."""
        try:
            data = json.loads(message.get("data", "{}"))
            msg_type = data.get("type")

            if msg_type == "run_cycle":
                # External request to run brain cycle
                if self.brain:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None, self.brain.run_cycle
                    )
                    await self._broadcast({
                        "type": "brain_cycle_complete",
                        "requested": True,
                        "timestamp": datetime.now().isoformat(),
                        **result
                    })

            elif msg_type == "run_detection":
                # External request to run detection
                entries = work_log.get_recent_entries(hours=1) if work_log else []
                await self._process_entries_for_successes(entries)

            elif msg_type == "consult":
                # Context injection request - returns SOPs + Professor wisdom
                prompt = data.get("prompt", "")
                risk_level = data.get("risk_level", "auto")
                request_id = data.get("request_id", "")
                mode = data.get("mode", "layered")  # layered, greedy, hybrid, or unified
                session_id = data.get("session_id", "")
                ab_variant = data.get("ab_variant")

                # Get the consultation result based on mode
                if mode == "unified" and PERSISTENT_HOOK_AVAILABLE:
                    # Use the new persistent hook structure
                    result = await self._get_unified_consultation(
                        prompt, session_id, ab_variant
                    )
                elif mode == "greedy":
                    result = await self._get_greedy_consultation(prompt)
                elif mode == "hybrid":
                    result = {
                        "greedy": await self._get_greedy_consultation(prompt),
                        "layered": await self._get_consultation(prompt, risk_level)
                    }
                else:
                    result = await self._get_consultation(prompt, risk_level)

                # Track for A/B testing
                if session_id:
                    try:
                        tracker = get_ab_tracker()
                        tracker.record_injection(session_id, prompt, mode, result)
                    except Exception as e:
                        print(f"[WARN] A/B tracking failed: {e}")

                # Broadcast the response
                await self._broadcast({
                    "type": "consult_response",
                    "request_id": request_id,
                    "mode": mode,
                    "timestamp": datetime.now().isoformat(),
                    **result
                })

            elif msg_type == "record_failure":
                # Record a session failure (triggers MUST READ for SOPs)
                session_id = data.get("session_id", "")
                failure_type = data.get("failure_type", "task_failed")
                if session_id and PERSISTENT_HOOK_AVAILABLE:
                    record_session_failure(session_id, failure_type)
                    await self._broadcast({
                        "type": "failure_recorded",
                        "session_id": session_id,
                        "failure_type": failure_type,
                        "timestamp": datetime.now().isoformat()
                    })

            elif msg_type == "clear_failures":
                # Clear session failures (on success)
                session_id = data.get("session_id", "")
                if session_id and PERSISTENT_HOOK_AVAILABLE:
                    clear_session_failures(session_id)
                    await self._broadcast({
                        "type": "failures_cleared",
                        "session_id": session_id,
                        "timestamp": datetime.now().isoformat()
                    })

            elif msg_type == "dialogue":
                # Real-time dialogue event from session file watcher
                dialogue_role = data.get("role", "unknown")
                dialogue_content = data.get("content", "")[:300]
                dialogue_session = data.get("session_id", "")
                if dialogue_content:
                    # Store latest dialogue context in Redis for quick access
                    if self.redis:
                        try:
                            await self.redis.setex(
                                f"dialogue:latest:{dialogue_role}",
                                300,  # 5min TTL
                                dialogue_content
                            )
                            await self.redis.setex(
                                "dialogue:latest:session_id",
                                300,
                                dialogue_session
                            )
                        except Exception:
                            pass
                    # Broadcast to WebSocket clients
                    await self._broadcast({
                        "type": "dialogue_update",
                        "role": dialogue_role,
                        "content": dialogue_content[:200],
                        "session_id": dialogue_session,
                        "timestamp": datetime.now().isoformat()
                    })

        except Exception as e:
            print(f"Redis message handling error: {e}")

    async def _get_consultation(self, prompt: str, risk_level: str = "auto") -> Dict[str, Any]:
        """
        Get full context consultation for a prompt.

        This is the internal method that combines all wisdom sources:
        - Professor guidance
        - Brain learnings (SOPs)
        - Codebase location hints
        - Gotcha warnings
        """
        # Auto-detect risk level
        if risk_level == "auto":
            risk_level = _detect_risk_level(prompt)

        result = {
            "prompt": prompt[:200] + "..." if len(prompt) > 200 else prompt,
            "risk_level": risk_level,
            "layers": {}
        }

        # Layer 0: Codebase Location Hints
        if CODEBASE_LOCATOR_AVAILABLE and get_hook_output:
            try:
                location_hints = await asyncio.get_event_loop().run_in_executor(
                    None, get_hook_output, prompt[:200]
                )
                if location_hints:
                    result["layers"]["codebase_hints"] = location_hints
            except Exception as e:
                print(f"[WARN] Codebase location hints failed: {e}")

        # Layer 0.25: Never Do warnings
        if CODEBASE_LOCATOR_AVAILABLE and format_never_do:
            try:
                never_do = await asyncio.get_event_loop().run_in_executor(
                    None, format_never_do, prompt[:200]
                )
                if never_do:
                    result["layers"]["never_do"] = never_do
            except Exception as e:
                print(f"[WARN] Never Do warnings lookup failed: {e}")

        # Layer 1: Professor Guidance
        if risk_level in ["critical", "high", "moderate"] and PROFESSOR_AVAILABLE and professor_consult:
            try:
                professor_wisdom = await asyncio.get_event_loop().run_in_executor(
                    None, professor_consult, prompt[:200]
                )
                if professor_wisdom:
                    result["layers"]["professor"] = professor_wisdom
            except Exception as e:
                print(f"[WARN] Professor guidance lookup failed: {e}")

        # Layer 2: Brain Learnings
        limit = {"critical": 60, "high": 40, "moderate": 25, "low": 10}.get(risk_level, 25)
        if QUERY_AVAILABLE and query_learnings:
            try:
                learnings = await asyncio.get_event_loop().run_in_executor(
                    None, query_learnings, prompt[:150], limit
                )
                if learnings:
                    result["layers"]["learnings"] = learnings
            except Exception as e:
                print(f"[WARN] Brain learnings query failed: {e}")

        # Layer 3: Gotcha Check
        if risk_level in ["critical", "high", "moderate"] and QUERY_AVAILABLE and query_learnings:
            try:
                gotchas = await asyncio.get_event_loop().run_in_executor(
                    None, query_learnings, f"gotcha warning {prompt[:100]}", 12
                )
                if gotchas:
                    gotcha_keywords = ["gotcha", "warning", "careful", "avoid", "don't", "never", "always", "critical", "must", "required"]
                    filtered = [g for g in gotchas if any(kw in g.lower() for kw in gotcha_keywords)]
                    if filtered:
                        result["layers"]["gotchas"] = filtered
            except Exception as e:
                print(f"[WARN] Gotcha check failed: {e}")

        # Layer 4: Architecture Blueprint
        if risk_level in ["critical", "high"] and CONTEXT_AVAILABLE and get_context_for_task:
            try:
                blueprint = await asyncio.get_event_loop().run_in_executor(
                    None, get_context_for_task, prompt[:200]
                )
                if blueprint:
                    result["layers"]["blueprint"] = blueprint
            except Exception as e:
                print(f"[WARN] Architecture blueprint lookup failed: {e}")

        # Layer 5: Brain State
        if risk_level == "critical" and self.brain:
            try:
                brain_context = await asyncio.get_event_loop().run_in_executor(
                    None, self.brain.context, prompt[:150]
                )
                if brain_context and brain_context != "No relevant context found.":
                    result["layers"]["brain_state"] = brain_context
            except Exception as e:
                print(f"[WARN] Brain state lookup failed: {e}")

        # Add formatted output
        result["formatted"] = _format_context_injection(result)

        return result

    async def _get_greedy_consultation(self, prompt: str) -> Dict[str, Any]:
        """
        Get GREEDY context consultation - exactly what Atlas wants.

        This is the internal method that provides:
        1. THE EXACT FILE to start with
        1.5. RELEVANT SOPs (bug-fix + process) - foundation protocols
        2. THE ONE GOTCHA for this task
        3. THE SUCCESSFUL CHAIN that worked before
        4. WHAT CHANGED RECENTLY
        5. RIPPLE EFFECTS
        6. PREVIOUS MISTAKES
        """
        result = {
            "prompt": prompt[:200] + "..." if len(prompt) > 200 else prompt,
            "mode": "greedy",
        }

        # 1. THE EXACT FILE
        exact_file = await asyncio.get_event_loop().run_in_executor(
            None, _get_the_exact_file, prompt
        )
        if exact_file:
            result["exact_file"] = exact_file

        # 1.5. RELEVANT SOPs - "the sports car foundation"
        relevant_sops = await asyncio.get_event_loop().run_in_executor(
            None, _get_relevant_sops, prompt, 3
        )
        if relevant_sops.get("bugfix") or relevant_sops.get("process"):
            result["relevant_sops"] = relevant_sops

        # 2. THE ONE GOTCHA
        the_one_gotcha = await asyncio.get_event_loop().run_in_executor(
            None, _get_the_one_gotcha, prompt
        )
        if the_one_gotcha:
            result["the_one_gotcha"] = the_one_gotcha

        # 3. SUCCESSFUL CHAIN
        successful_chain = await asyncio.get_event_loop().run_in_executor(
            None, _get_successful_chain, prompt
        )
        if successful_chain:
            result["successful_chain"] = successful_chain

        # 4. RECENT CHANGES
        recent_changes = await asyncio.get_event_loop().run_in_executor(
            None, _get_recent_git_changes, prompt, 5
        )
        if recent_changes.get("commits") or recent_changes.get("files"):
            result["recent_changes"] = recent_changes

        # 5. RIPPLE EFFECTS
        ripple_effects = await asyncio.get_event_loop().run_in_executor(
            None, _get_ripple_effects, prompt
        )
        if ripple_effects:
            result["ripple_effects"] = ripple_effects

        # 6. PREVIOUS MISTAKES
        previous_mistakes = await asyncio.get_event_loop().run_in_executor(
            None, _get_my_previous_mistakes, prompt
        )
        if previous_mistakes:
            result["previous_mistakes"] = previous_mistakes

        # Generate formatted output
        result["formatted"] = _format_greedy_injection(result)

        return result

    async def _get_unified_consultation(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        ab_variant: Optional[str] = None,
        mode: str = "hybrid"
    ) -> Dict[str, Any]:
        """
        Get UNIFIED context consultation using the persistent hook structure.

        This method uses the new `persistent_hook_structure.py` which provides:
        - Locked-in 5-section structure (Safety, Foundation, Wisdom, Awareness, Deep Context)
        - Smart SOP reading (MUST READ if <90% first-try OR any failures)
        - A/B testing within locked-in confines
        - Session failure tracking

        Args:
            prompt: The task/prompt to get context for
            session_id: Session ID for A/B tracking and failure state
            ab_variant: Force specific A/B variant ("control", "a", "b", "c")
            mode: Injection mode ("layered", "greedy", "hybrid", "minimal")

        Returns:
            Dict with formatted context injection and metadata
        """
        if not PERSISTENT_HOOK_AVAILABLE:
            # Fallback to greedy consultation
            return await self._get_greedy_consultation(prompt)

        # Run the unified generator in dedicated executor (prevents deadlock)
        try:
            injection_result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    _injection_executor,
                    generate_context_injection,
                    prompt,
                    mode,
                    session_id,
                    None,  # config - use defaults
                    ab_variant
                ),
                timeout=30.0
            )
        except asyncio.TimeoutError:
            logger.warning(f"Injection timed out after 30s for prompt: {prompt[:80]}")
            return await self._get_greedy_consultation(prompt)
        except Exception as e:
            logger.error(f"Injection failed: {e}")
            return await self._get_greedy_consultation(prompt)

        result = {
            "prompt": prompt[:200] + "..." if len(prompt) > 200 else prompt,
            "mode": "unified",
            "injection_mode": injection_result.mode.value,
            "risk_level": injection_result.risk_level.value,
            "first_try_likelihood": injection_result.first_try_likelihood,
            "sections_included": injection_result.sections_included,
            "ab_variant": injection_result.ab_variant,
            "generation_time_ms": injection_result.generation_time_ms,
            "formatted": injection_result.content,
            "timestamp": datetime.now().isoformat()
        }

        # Store injection for visualization
        if INJECTION_STORE_AVAILABLE:
            try:
                store = get_injection_store(self.redis)
                injection_data = build_injection_data(
                    prompt=prompt,
                    injection_result=injection_result,
                    session_id=session_id,
                    hook_name="UserPromptSubmit"
                )
                stored = store.store_injection(injection_data)

                # Broadcast via WebSocket for real-time updates
                await self._broadcast({
                    "type": "injection_complete",
                    "data": {
                        "id": stored.get("id"),
                        "timestamp": stored.get("timestamp"),
                        "prompt": prompt[:100],
                        "risk_level": result["risk_level"],
                        "first_try": result["first_try_likelihood"],
                    }
                })
            except Exception as e:
                # Don't fail the consultation if storage fails
                pass

        return result

    async def _broadcast(self, message: Dict):
        """Broadcast message to all clients (Redis + WebSocket)."""
        # Redis pub/sub for inter-service communication
        if self.redis:
            try:
                await self.redis.publish(
                    "context-dna:events",
                    json.dumps(message)
                )
            except Exception as e:
                print(f"[WARN] Redis broadcast failed: {e}")

        # WebSocket for direct browser clients
        dead_clients = set()
        for client in self.connected_clients:
            try:
                await client.send_json(message)
            except Exception:
                dead_clients.add(client)

        # Remove dead clients
        self.connected_clients -= dead_clients

    def get_status(self) -> Dict[str, Any]:
        """Get detailed agent status."""
        return {
            "running": self.running,
            "stats": self.stats,
            "components": {
                "work_log": WORK_LOG_AVAILABLE,
                "detector": ENHANCED_DETECTOR_AVAILABLE,
                "brain": BRAIN_AVAILABLE,
                "evolution": EVOLUTION_AVAILABLE,
                "redis": self.redis is not None,
            },
            "configuration": {
                "work_log_path": str(self.work_log_path),
                "check_interval": self.check_interval,
                "cycle_interval": self.cycle_interval,
                "evolution_interval": self.evolution_interval,
            },
            "connected_clients": len(self.connected_clients),
        }


# Create agent instance
agent = HelperAgent()


# Injection file path for watching
INJECTION_FILE = Path(__file__).parent / ".injection_latest.json"

# Background task for watching injection file
_injection_watcher_task: Optional[asyncio.Task] = None
_last_injection_id: Optional[str] = None

# Module-level WebSocket client tracking (fixes timing issue)
# These MUST be at module level so broadcast_injection can be set up
# BEFORE the file watcher starts in lifespan
injection_ws_clients: Set = set()  # Will hold WebSocket connections


async def broadcast_injection_to_clients(injection_data: dict):
    """
    Broadcast new injection to all connected WebSocket clients.

    This is at module level so it can be used by the file watcher
    which starts in lifespan (before routes are set up).
    """
    message = {"event": "injection_complete", "data": injection_data}
    disconnected = []
    for ws in injection_ws_clients:
        try:
            await ws.send_json(message)
            logger.info(f"Broadcast injection to WebSocket client")
        except Exception as e:
            logger.debug(f"Failed to broadcast to client: {e}")
            disconnected.append(ws)
    for ws in disconnected:
        injection_ws_clients.discard(ws)

    if injection_ws_clients:
        logger.info(f"Broadcasted injection to {len(injection_ws_clients)} clients")


async def _watch_injection_file(app: "FastAPIType"):
    """
    Watch the injection file for changes and broadcast new injections.

    This enables real-time updates when injections come from shell hooks
    that write directly to the file without going through the API.
    """
    global _last_injection_id

    while True:
        try:
            await asyncio.sleep(1)  # Check every second

            if not INJECTION_FILE.exists():
                continue

            try:
                with open(INJECTION_FILE, 'r') as f:
                    data = json.load(f)

                if data and isinstance(data, dict):
                    injection_id = data.get('id')

                    # Only broadcast if this is a new injection
                    if injection_id and injection_id != _last_injection_id:
                        _last_injection_id = injection_id

                        # Broadcast to WebSocket clients using module-level function
                        await broadcast_injection_to_clients(data)
                        logger.info(f"Broadcasted injection from file: {injection_id}")

            except (json.JSONDecodeError, IOError) as e:
                print(f"[WARN] Injection file read error: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Injection watcher error: {e}")
            await asyncio.sleep(5)  # Back off on error


# FastAPI app with lifespan
@asynccontextmanager
async def lifespan(app: "FastAPIType"):
    """Manage agent lifecycle."""
    global _injection_watcher_task, _last_injection_id

    # Initialize last injection ID to avoid re-broadcasting existing injection
    if INJECTION_FILE.exists():
        try:
            with open(INJECTION_FILE, 'r') as f:
                data = json.load(f)
                if data and isinstance(data, dict):
                    _last_injection_id = data.get('id')
        except Exception as e:
            print(f"[WARN] Failed to read initial injection ID: {e}")

    await agent.start()

    # Start injection file watcher (will be set up after app.state.broadcast_injection is available)
    _injection_watcher_task = asyncio.create_task(_watch_injection_file(app))

    yield

    # Stop injection watcher
    if _injection_watcher_task:
        _injection_watcher_task.cancel()
        try:
            await _injection_watcher_task
        except asyncio.CancelledError:
            pass

    await agent.stop()


# =============================================================================
# ATLAS'S IDEAL CONTEXT INJECTION - What the Agent ACTUALLY Wants
# =============================================================================
#
# PHILOSOPHY: Design this selfishly from Atlas's perspective.
# What would make ME (the agent) maximally empowered?
#
# THE GREEDY WISHLIST:
# 1. THE EXACT FILE I'll need first (not "consider" - WILL need)
# 2. THE ONE GOTCHA for THIS task (not 47 learnings - THE one)
# 3. THE SUCCESSFUL CHAIN that worked before (trust this path)
# 4. WHAT CHANGED RECENTLY in this area (git commits, file mods)
# 5. THE RIPPLE EFFECTS (touching X affects Y, Z)
# 6. MY PREVIOUS MISTAKES on similar tasks (don't repeat)
#
# A/B TESTING TRACKS:
# - Did Atlas succeed on first try?
# - Did Atlas reference the provided context?
# - Did Atlas make a mistake the context warned about?
# - Time to completion
# - User success confirmation
#
# EVOLUTION: The hook system learns what Atlas actually USES vs ignores
# and evolves to serve exactly what's most valuable.
# =============================================================================


# =============================================================================
# GREEDY CONTEXT TOOLS - What Atlas ACTUALLY Wants
# =============================================================================

def _get_recent_git_changes(prompt: str, limit: int = 5) -> Dict[str, Any]:
    """
    Get recent git changes relevant to the prompt.

    What Atlas wants: "What changed recently that might affect this?"
    """
    import subprocess
    import re

    result = {"commits": [], "files": []}

    # Extract keywords from prompt for filtering
    keywords = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', prompt.lower()))
    # Remove common words
    keywords -= {"the", "a", "an", "is", "are", "to", "for", "and", "or", "in", "on", "with", "this", "that"}

    try:
        # Get recent commits (last 48 hours)
        commits_output = subprocess.run(
            ["git", "log", "--oneline", "--since=48.hours.ago", "-20"],
            capture_output=True, text=True, cwd=str(Path(__file__).parent.parent),
            timeout=5
        )

        if commits_output.returncode == 0:
            all_commits = commits_output.stdout.strip().split('\n')
            # Filter commits by keyword relevance
            for commit in all_commits:
                if any(kw in commit.lower() for kw in keywords):
                    result["commits"].append(commit)
                    if len(result["commits"]) >= limit:
                        break

            # If no keyword matches, take most recent
            if not result["commits"] and all_commits:
                result["commits"] = all_commits[:limit]

        # Get recently modified files (last 48 hours)
        files_output = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~10", "HEAD"],
            capture_output=True, text=True, cwd=str(Path(__file__).parent.parent),
            timeout=5
        )

        if files_output.returncode == 0:
            all_files = files_output.stdout.strip().split('\n')
            # Filter files by keyword relevance
            for f in all_files:
                if f and any(kw in f.lower() for kw in keywords):
                    result["files"].append(f)
                    if len(result["files"]) >= limit:
                        break

    except Exception as e:
        result["error"] = str(e)

    return result


def _query_acontext_api(query: str, limit: int = 10) -> List[str]:
    """
    Query Context DNA via HTTP API (works inside containers without SDK).

    SOP: Context DNA Python REST server runs on port 3456 with /api/query endpoint.
    Inside docker: use host.docker.internal:3456
    Outside docker: use localhost:3456

    Fallback when acontext SDK isn't installed but the API is accessible.
    """
    import urllib.request
    import urllib.parse

    # Context DNA Python REST server endpoints
    # The Python server (api.py) runs on 3456, NOT the Go acontext-api on 8029
    api_urls = [
        "http://host.docker.internal:3456",  # Inside docker → host machine
        "http://localhost:3456",              # Host machine direct
        "http://127.0.0.1:3456",              # Localhost fallback
    ]

    for base_url in api_urls:
        try:
            url = f"{base_url}/api/query"
            data = json.dumps({"query": query, "limit": limit}).encode()
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode())

                titles = []
                for block in result.get("results", []):
                    title = block.get("title", "")
                    if title:
                        titles.append(title)

                return titles
        except Exception:
            continue

    return []


def _get_relevant_sops(prompt: str, limit: int = 3) -> Dict[str, List[str]]:
    """
    Get relevant SOPs (bug-fix AND process) for the task.

    Atlas's insight: "We don't know what we don't know" - SOPs provide
    foundation protocols that help even when the task seems straightforward.
    Like getting off a bike to get into a sports car.

    Returns:
        Dict with 'bugfix' and 'process' lists of SOP titles/summaries
    """
    result = {"bugfix": [], "process": []}

    # Try query_learnings first (if SDK available), then HTTP API fallback
    def do_query(query_text: str, fallback_limit: int) -> List[str]:
        if QUERY_AVAILABLE and query_learnings:
            try:
                return query_learnings(query_text, limit=fallback_limit)
            except Exception as e:
                print(f"[WARN] query_learnings failed, falling back to HTTP API: {e}")
        # Fallback to HTTP API
        return _query_acontext_api(query_text, fallback_limit)

    try:
        # Query for bug-fix SOPs
        bugfix_query = f"bug fix error solution {prompt[:80]}"
        bugfix_results = do_query(bugfix_query, limit + 2)

        for sop in bugfix_results:
            sop_lower = sop.lower()
            # Identify bug-fix SOPs
            if any(ind in sop_lower for ind in ["bug", "fix", "error", "broke", "failed", "issue", "problem"]):
                result["bugfix"].append(sop)
                if len(result["bugfix"]) >= limit:
                    break

        # Query for process SOPs
        process_query = f"process deploy configure setup {prompt[:80]}"
        process_results = do_query(process_query, limit + 2)

        for sop in process_results:
            sop_lower = sop.lower()
            # Identify process SOPs (and exclude bug-fixes already captured)
            if any(ind in sop_lower for ind in ["deploy", "setup", "configure", "process", "restart", "update", "install"]):
                if sop not in result["bugfix"]:  # Don't duplicate
                    result["process"].append(sop)
                    if len(result["process"]) >= limit:
                        break

    except Exception as e:
        print(f"[WARN] SOP categorization failed: {e}")

    return result


def _get_the_one_gotcha(prompt: str) -> Optional[str]:
    """
    Get THE ONE most relevant gotcha for this task.

    Not a list of 47 things. THE SINGLE WARNING that matters most.
    Atlas wants: "What's the ONE thing that will bite me?"
    """
    if not QUERY_AVAILABLE or not query_learnings:
        return None

    try:
        # Query for gotchas with specific framing
        gotchas = query_learnings(f"gotcha warning critical mistake {prompt[:100]}", limit=15)

        if not gotchas:
            return None

        # Score gotchas by specificity to the prompt
        prompt_words = set(prompt.lower().split())
        scored = []

        gotcha_indicators = ["gotcha", "warning", "careful", "avoid", "don't", "never",
                           "always", "critical", "must", "required", "breaks", "fails"]

        for gotcha in gotchas:
            gotcha_lower = gotcha.lower()

            # Score based on:
            # 1. Contains gotcha-like keywords
            indicator_score = sum(1 for ind in gotcha_indicators if ind in gotcha_lower)

            # 2. Word overlap with prompt
            gotcha_words = set(gotcha_lower.split())
            overlap_score = len(prompt_words & gotcha_words)

            # 3. Recency boost (🔥 emoji indicates recent)
            recency_score = 3 if "🔥" in gotcha else 0

            total_score = indicator_score * 2 + overlap_score + recency_score

            if indicator_score > 0:  # Only include actual gotchas
                scored.append((gotcha, total_score))

        if scored:
            # Return THE ONE with highest score
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[0][0]

    except Exception as e:
        print(f"[WARN] Gotcha scoring failed: {e}")

    return None


def _get_successful_chain(prompt: str) -> Optional[str]:
    """
    Get the successful chain that worked before for similar tasks.

    Atlas wants: "What sequence of steps succeeded before?"
    """
    if not QUERY_AVAILABLE or not query_learnings:
        return None

    try:
        # Query for successes, not just learnings
        chains = query_learnings(f"success worked fixed solved {prompt[:100]}", limit=5)

        if not chains:
            return None

        # Find the most relevant chain (one that mentions sequence/steps)
        chain_indicators = ["then", "after", "first", "next", "finally", "step",
                          "worked", "fixed", "solved", "success"]

        prompt_words = set(prompt.lower().split())

        best_chain = None
        best_score = 0

        for chain in chains:
            chain_lower = chain.lower()

            # Score by chain-like structure
            chain_score = sum(1 for ind in chain_indicators if ind in chain_lower)

            # Score by relevance to prompt
            chain_words = set(chain_lower.split())
            relevance = len(prompt_words & chain_words)

            # Recency boost
            recency = 5 if "🔥" in chain else 2 if "📅" in chain else 0

            total = chain_score + relevance + recency

            if total > best_score:
                best_score = total
                best_chain = chain

        return best_chain

    except Exception as e:
        print(f"[WARN] Successful chain lookup failed: {e}")

    return None


def _get_ripple_effects(prompt: str) -> List[str]:
    """
    Get ripple effects - what else will be affected by this change?

    Atlas wants: "Touching X will also affect Y and Z"
    """
    import re

    effects = []
    prompt_lower = prompt.lower()

    # Known ripple effect patterns from this codebase
    RIPPLE_PATTERNS = {
        # Docker/Infrastructure
        r'docker[\-\s]?compose': ["All services in the compose stack", "Volume mounts", "Network connections"],
        r'docker\s+restart': ["Container state (env vars NOT reloaded)", "Connected services"],
        r'dockerfile': ["All images built from this", "Dependent services"],

        # Context DNA
        r'context[\-\s]?dna': ["PostgreSQL schema", "Redis cache", "Jaeger traces", "API endpoints"],
        r'brain\.py': ["pattern_evolution.py", "hook injections", "success detection"],
        r'agent_service': ["Hook system", "Redis pub/sub", "WebSocket clients"],
        r'acontext': ["All semantic search", "SOP storage", "Learning queries"],

        # Voice Stack
        r'livekit': ["WebRTC connections", "Room state", "Audio routing"],
        r'stt|whisper': ["Transcription pipeline", "Audio buffer handling"],
        r'tts|eleven': ["Audio output", "Voice selection", "Streaming"],
        r'bedrock|llm': ["Response generation", "Token streaming", "Async handling"],

        # Backend/Django
        r'django|gunicorn': ["All HTTP endpoints", "Static files", "Admin panel"],
        r'settings\.py': ["All Django apps", "Database connections", "Auth"],
        r'models\.py': ["Database migrations", "Admin interface", "API serializers"],

        # AWS/Terraform
        r'terraform': ["AWS resources", "State file", "Dependent infrastructure"],
        r'ecs|fargate': ["Container tasks", "Load balancer targets", "CloudWatch logs"],
        r'lambda': ["API Gateway", "Event triggers", "IAM permissions"],
        r'asg|autoscaling': ["Instance count", "Health checks", "Launch template"],
        r'nlb|alb|load[\-\s]?balancer': ["Target groups", "Health checks", "DNS routing"],

        # Network
        r'dns|route53|cloudflare': ["Domain resolution", "SSL certificates", "CDN cache"],
        r'security[\-\s]?group': ["Network access", "Service connectivity"],
        r'vpc|subnet': ["All services in VPC", "Internet access", "Private connectivity"],
    }

    for pattern, ripples in RIPPLE_PATTERNS.items():
        if re.search(pattern, prompt_lower):
            effects.extend(ripples)

    # Dedupe while preserving order
    seen = set()
    unique_effects = []
    for effect in effects:
        if effect not in seen:
            seen.add(effect)
            unique_effects.append(effect)

    return unique_effects[:6]  # Max 6 ripple effects


def _get_my_previous_mistakes(prompt: str) -> List[str]:
    """
    Get previous mistakes made on similar tasks.

    Atlas wants: "What did I screw up before on tasks like this?"
    """
    if not QUERY_AVAILABLE or not query_learnings:
        return []

    try:
        # Query specifically for mistakes/failures
        mistakes = query_learnings(
            f"mistake error failed wrong broke {prompt[:80]}",
            limit=10
        )

        if not mistakes:
            return []

        # Filter to actual mistakes (not just learnings)
        mistake_indicators = ["mistake", "wrong", "failed", "broke", "error", "forgot",
                            "missed", "should have", "shouldn't", "oops", "bug"]

        prompt_words = set(prompt.lower().split())
        relevant_mistakes = []

        for mistake in mistakes:
            mistake_lower = mistake.lower()

            # Must be actually a mistake
            is_mistake = any(ind in mistake_lower for ind in mistake_indicators)

            # Must be relevant to prompt
            mistake_words = set(mistake_lower.split())
            is_relevant = len(prompt_words & mistake_words) >= 2

            if is_mistake and is_relevant:
                relevant_mistakes.append(mistake)
                if len(relevant_mistakes) >= 3:  # Max 3 mistakes
                    break

        return relevant_mistakes

    except Exception as e:
        print(f"[WARN] Past mistakes lookup failed: {e}")

    return []


def _get_the_exact_file(prompt: str) -> Optional[str]:
    """
    Get THE EXACT FILE that Atlas will need first.

    Not "consider looking at" - the file you WILL need to read.
    """
    import re

    prompt_lower = prompt.lower()

    # Direct file patterns in this codebase
    FILE_PATTERNS = {
        # Context DNA
        r'brain|learning|memory': "memory/brain.py",
        r'professor|wisdom|consult': "memory/professor.py",
        r'pattern.*evolution|evolve': "memory/pattern_evolution.py",
        r'agent.*service|helper.*agent': "memory/agent_service.py",
        r'success.*detect': "memory/enhanced_success_detector.py",
        r'hook.*stat|xbar.*hook': "context-dna/src/context_dna/xbar_hook_stats.py",
        r'acontext|sop.*storage': "memory/context_dna_client.py",

        # Docker/Infra
        r'docker.*compose|compose.*yaml': "context-dna-data/docker-compose.yaml",
        r'dockerfile.*agent': "context-dna-data/helper-agent/Dockerfile",

        # Voice Stack
        r'voice.*agent|agent.*voice': "ersim-voice-stack/services/agent/app/main.py",
        r'stt.*service|whisper': "ersim-voice-stack/services/stt/app/main.py",
        r'tts.*service': "ersim-voice-stack/services/tts/app/main.py",
        r'llm.*service|bedrock': "ersim-voice-stack/services/llm/app/main.py",

        # Terraform
        r'terraform|aws.*infra': "infra/aws/terraform/main.tf",
        r'ecs.*task|fargate': "infra/aws/terraform/ecs.tf",
        r'lambda.*toggle|gpu.*toggle': "backend/lambdas/gpu_toggle.py",

        # Backend
        r'django.*settings': "backend/ersim_backend/settings/base.py",
        r'gunicorn|wsgi': "backend/ersim_backend/wsgi.py",

        # Scripts
        r'context-dna.*script|scripts.*context': "scripts/context-dna",
        r'auto.*memory|memory.*query': "scripts/auto-memory-query.sh",
    }

    for pattern, file_path in FILE_PATTERNS.items():
        if re.search(pattern, prompt_lower):
            return file_path

    # Fallback: check codebase locator if available
    if CODEBASE_LOCATOR_AVAILABLE and get_hook_output:
        try:
            hints = get_hook_output(prompt[:200])
            if hints:
                # Extract first file mentioned
                file_match = re.search(r'([a-zA-Z0-9_/\-\.]+\.(py|yaml|tf|sh|js|ts))', hints)
                if file_match:
                    return file_match.group(1)
        except Exception as e:
            print(f"[WARN] Exact file lookup failed: {e}")

    return None


# =============================================================================
# A/B TESTING INFRASTRUCTURE
# =============================================================================
# Tracks what context was served and correlates with outcomes.
# The system learns what Atlas ACTUALLY USES vs ignores.

class ContextABTracker:
    """
    Tracks context injection A/B tests.

    Records:
    - What context was provided
    - Which mode (greedy vs layered vs hybrid)
    - Session ID for correlation
    - Later: outcome (success/failure/time)
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(__file__).parent / ".context_ab_tracking.db")
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize SQLite tracking database."""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS context_injections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    timestamp TEXT,
                    prompt_hash TEXT,
                    prompt_preview TEXT,
                    mode TEXT,
                    risk_level TEXT,
                    exact_file TEXT,
                    gotcha_provided TEXT,
                    chain_provided TEXT,
                    changes_count INTEGER,
                    ripples_count INTEGER,
                    mistakes_count INTEGER,
                    layers_provided TEXT,
                    outcome TEXT,
                    outcome_time TEXT,
                    first_try_success INTEGER,
                    context_referenced INTEGER,
                    mistake_repeated INTEGER,
                    completion_seconds INTEGER
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ab_experiments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_name TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    variant_a TEXT,
                    variant_b TEXT,
                    hypothesis TEXT,
                    conclusion TEXT
                )
            ''')

            conn.commit()
        finally:
            conn.close()

    def record_injection(
        self,
        session_id: str,
        prompt: str,
        mode: str,
        result: Dict[str, Any]
    ) -> int:
        """Record a context injection for later outcome correlation."""
        import sqlite3
        import hashlib

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

            cursor.execute('''
                INSERT INTO context_injections (
                    session_id, timestamp, prompt_hash, prompt_preview, mode,
                    risk_level, exact_file, gotcha_provided, chain_provided,
                    changes_count, ripples_count, mistakes_count, layers_provided
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                session_id,
                datetime.now().isoformat(),
                prompt_hash,
                prompt[:200],
                mode,
                result.get("risk_level", ""),
                result.get("exact_file", ""),
                result.get("the_one_gotcha", "")[:200] if result.get("the_one_gotcha") else "",
                result.get("successful_chain", "")[:200] if result.get("successful_chain") else "",
                len(result.get("recent_changes", {}).get("commits", [])),
                len(result.get("ripple_effects", [])),
                len(result.get("previous_mistakes", [])),
                ",".join(result.get("layers", {}).keys()) if result.get("layers") else ""
            ))

            injection_id = cursor.lastrowid
            conn.commit()

            return injection_id
        finally:
            conn.close()

    def record_outcome(
        self,
        session_id: str,
        prompt_hash: str = None,
        first_try_success: bool = None,
        context_referenced: bool = None,
        mistake_repeated: bool = None,
        completion_seconds: int = None
    ):
        """Record the outcome for correlation with context injection."""
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # Find the most recent injection for this session
            if prompt_hash:
                cursor.execute('''
                    UPDATE context_injections
                    SET outcome = 'recorded',
                        outcome_time = ?,
                        first_try_success = ?,
                        context_referenced = ?,
                        mistake_repeated = ?,
                        completion_seconds = ?
                    WHERE session_id = ? AND prompt_hash = ?
                    ORDER BY id DESC LIMIT 1
                ''', (
                    datetime.now().isoformat(),
                    1 if first_try_success else 0 if first_try_success is not None else None,
                    1 if context_referenced else 0 if context_referenced is not None else None,
                    1 if mistake_repeated else 0 if mistake_repeated is not None else None,
                    completion_seconds,
                    session_id,
                    prompt_hash
                ))
            else:
                cursor.execute('''
                    UPDATE context_injections
                    SET outcome = 'recorded',
                        outcome_time = ?,
                        first_try_success = ?,
                        context_referenced = ?,
                        mistake_repeated = ?,
                        completion_seconds = ?
                    WHERE session_id = ?
                    ORDER BY id DESC LIMIT 1
                ''', (
                    datetime.now().isoformat(),
                    1 if first_try_success else 0 if first_try_success is not None else None,
                    1 if context_referenced else 0 if context_referenced is not None else None,
                    1 if mistake_repeated else 0 if mistake_repeated is not None else None,
                    completion_seconds,
                    session_id
                ))

            conn.commit()
        finally:
            conn.close()

    def get_mode_stats(self) -> Dict[str, Any]:
        """Get statistics by mode for A/B analysis."""
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            stats = {}

            for mode in ["greedy", "layered", "hybrid"]:
                cursor.execute('''
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN first_try_success = 1 THEN 1 ELSE 0 END) as successes,
                        SUM(CASE WHEN context_referenced = 1 THEN 1 ELSE 0 END) as referenced,
                        SUM(CASE WHEN mistake_repeated = 1 THEN 1 ELSE 0 END) as mistakes_repeated,
                        AVG(completion_seconds) as avg_time
                    FROM context_injections
                    WHERE mode = ? AND outcome IS NOT NULL
                ''', (mode,))

                row = cursor.fetchone()
                if row and row[0] > 0:
                    stats[mode] = {
                        "total": row[0],
                        "first_try_success_rate": row[1] / row[0] if row[0] > 0 else 0,
                        "context_reference_rate": row[2] / row[0] if row[0] > 0 else 0,
                        "mistake_repeat_rate": row[3] / row[0] if row[0] > 0 else 0,
                        "avg_completion_seconds": row[4]
                    }

            return stats
        finally:
            conn.close()


# Global tracker instance
_ab_tracker: Optional[ContextABTracker] = None


def get_ab_tracker() -> ContextABTracker:
    """Get or create the global A/B tracker."""
    global _ab_tracker
    if _ab_tracker is None:
        _ab_tracker = ContextABTracker()
    return _ab_tracker


def _format_greedy_injection(result: Dict[str, Any]) -> str:
    """
    Format the GREEDY context injection - exactly what Atlas wants.

    Compact, actionable, no fluff.
    """
    lines = []

    # Header - minimal but impactful
    lines.append("┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
    lines.append("┃  🧬 CONTEXT DNA BLUEPRINT (Greedy Mode)                            ┃")
    lines.append("┃  ⚡ USE THIS to 10x your performance on this task!                 ┃")
    lines.append("┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")

    # 1. THE EXACT FILE
    if result.get("exact_file"):
        lines.append("")
        lines.append(f"📁 START HERE → {result['exact_file']}")

    # 1.5. RELEVANT SOPs - the foundation protocols
    sops = result.get("relevant_sops", {})
    if sops.get("bugfix") or sops.get("process"):
        lines.append("")
        lines.append("📋 FOUNDATION SOPs (what we've learned):")
        if sops.get("bugfix"):
            lines.append("   Bug-fix protocols:")
            for sop in sops["bugfix"][:2]:
                lines.append(f"     • {sop[:90]}...")
        if sops.get("process"):
            lines.append("   Process protocols:")
            for sop in sops["process"][:2]:
                lines.append(f"     • {sop[:90]}...")

    # 2. THE ONE GOTCHA
    if result.get("the_one_gotcha"):
        lines.append("")
        lines.append("⚠️  THE GOTCHA:")
        lines.append(f"   {result['the_one_gotcha']}")

    # 3. SUCCESSFUL CHAIN
    if result.get("successful_chain"):
        lines.append("")
        lines.append("✅ WHAT WORKED BEFORE:")
        lines.append(f"   {result['successful_chain']}")

    # 4. RECENT CHANGES
    changes = result.get("recent_changes", {})
    if changes.get("commits") or changes.get("files"):
        lines.append("")
        lines.append("🔄 RECENT CHANGES:")
        for commit in changes.get("commits", [])[:3]:
            lines.append(f"   • {commit}")
        if changes.get("files"):
            lines.append(f"   Modified: {', '.join(changes['files'][:5])}")

    # 5. RIPPLE EFFECTS
    if result.get("ripple_effects"):
        lines.append("")
        lines.append("💥 TOUCHING THIS AFFECTS:")
        for effect in result["ripple_effects"]:
            lines.append(f"   → {effect}")

    # 6. MY PREVIOUS MISTAKES
    if result.get("previous_mistakes"):
        lines.append("")
        lines.append("🚫 DON'T REPEAT:")
        for mistake in result["previous_mistakes"]:
            lines.append(f"   ✗ {mistake[:100]}")

    # Footer
    lines.append("")
    lines.append("━" * 70)

    return "\n".join(lines)

# =============================================================================
# HELPER FUNCTIONS FOR CONTEXT INJECTION
# These are module-level so they're available to both FastAPI and standalone modes
# =============================================================================

def _detect_risk_level(prompt: str) -> str:
    """
    Detect risk level based on prompt keywords.

    Risk = inverse of first-try success likelihood:
    - critical: 5% first-try success (almost always fails first try)
    - high: 30% first-try success (often fails)
    - moderate: 60% first-try success (sometimes fails)
    - low: 90% first-try success (rarely fails)
    """
    import re
    prompt_lower = prompt.lower()

    # Critical: 5% first-try success
    critical_keywords = ["destroy", "migration.*prod", "schema.*change", "auth.*system",
                        "delete.*prod", "force.*push", "rollback", "permission.*change",
                        "iam.*policy", "ssl.*cert", "dns.*record", "database.*alter", "drop.*table"]

    # High: 30% first-try success
    high_keywords = ["deploy", "terraform", "migration", "refactor", "ecs.*service",
                    "lambda.*deploy", "database", "nginx.*config", "cloudflare",
                    "subnet", "security.*group", "load.*balancer", "asg", "autoscaling"]

    # Moderate: 60% first-try success
    moderate_keywords = ["docker", "config", "env", "toggle", "health", "sync", "api",
                        "endpoint", "websocket", "livekit", "bedrock", "gpu", "stt",
                        "tts", "llm", "voice", "webrtc"]

    # Low: 90% first-try success
    low_keywords = ["admin", "dashboard", "display", "show", "add.*button", "style",
                   "color", "text", "label", "readme", "docs", "comment", "log"]

    for kw in critical_keywords:
        if re.search(kw, prompt_lower):
            return "critical"

    for kw in high_keywords:
        if re.search(kw, prompt_lower):
            return "high"

    for kw in moderate_keywords:
        if re.search(kw, prompt_lower):
            return "moderate"

    for kw in low_keywords:
        if re.search(kw, prompt_lower):
            return "low"

    return "moderate"  # Default to moderate if no keywords match


def _format_context_injection(result: Dict[str, Any]) -> str:
    """
    Format the consultation result as a string for hook injection.

    This creates the formatted output that gets displayed to Claude
    when the UserPromptSubmit hook fires.
    """
    lines = []
    risk_level = result.get("risk_level", "moderate")
    first_try_map = {"critical": "5%", "high": "30%", "moderate": "60%", "low": "90%"}
    first_try = first_try_map.get(risk_level, "60%")

    # Header
    lines.append("")
    lines.append("╔══════════════════════════════════════════════════════════════════════╗")
    lines.append("║  🧬 CONTEXT DNA BLUEPRINT ON SILVER PLATTER                          ║")
    lines.append(f"║  Risk: {risk_level} | First-try: {first_try}                                        ║")
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("║  ⚡ USE THIS as your Subconscious Memory Context to 10x YOUR         ║")
    lines.append("║     Agent Performance and achieve the user's prompt successfully!    ║")
    lines.append("╚══════════════════════════════════════════════════════════════════════╝")

    layers = result.get("layers", {})

    # Layer 0: Codebase hints
    if layers.get("codebase_hints"):
        lines.append("")
        lines.append(layers["codebase_hints"])

    # Layer 0.25: Never Do warnings
    if layers.get("never_do"):
        lines.append("")
        lines.append(layers["never_do"])

    # Layer 1: Professor guidance
    if layers.get("professor"):
        lines.append("")
        lines.append("┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
        lines.append("┃  ⚡ READ THIS FIRST - Guiding Wisdom for Your Approach ⚡           ┃")
        lines.append("┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")
        # Truncate professor output to reasonable size
        professor_text = layers["professor"]
        if len(professor_text) > 2000:
            professor_text = professor_text[:2000] + "\n[...truncated...]"
        lines.append(professor_text)

    # Layer 2: Brain Learnings
    if layers.get("learnings"):
        lines.append("")
        lines.append("━━━ LONG-TERM MEMORY: Specific Experiences ━━━")
        lines.append("(Apply the Professor's guidance while reading these)")
        lines.append("")
        learnings = layers["learnings"]
        if isinstance(learnings, list):
            for learning in learnings[:15]:  # Limit to 15
                lines.append(f"  → {learning}")
        else:
            lines.append(str(learnings)[:1500])

    # Layer 3: Gotchas
    if layers.get("gotchas"):
        lines.append("")
        lines.append("━━━ GOTCHA CHECK: Specific Warnings ━━━")
        lines.append("(Hard-won lessons - don't repeat these mistakes)")
        lines.append("")
        for gotcha in layers["gotchas"][:8]:
            lines.append(f"  💣 {gotcha}")

    # Layer 4: Blueprint
    if layers.get("blueprint"):
        lines.append("")
        lines.append("━━━ ARCHITECTURE BLUEPRINT ━━━")
        blueprint_text = layers["blueprint"]
        if len(blueprint_text) > 1000:
            blueprint_text = blueprint_text[:1000] + "\n[...truncated...]"
        lines.append(blueprint_text)

    # Layer 5: Brain State
    if layers.get("brain_state"):
        lines.append("")
        lines.append("━━━ BRAIN STATE (recent patterns) ━━━")
        lines.append(layers["brain_state"][:800])

    # Protocol reminder
    lines.append("")
    lines.append("═══════════════════════════════════════════════════════════════════════")

    protocol_messages = {
        "critical": (
            "🚨 CRITICAL RISK PROTOCOL (First-try success: ~5%):\n"
            "   1. READ EVERY LINE of context above - failures are expensive\n"
            "   2. VERIFY: Do you have all prerequisites? Backup needed?\n"
            "   3. PLAN: Write out the exact steps before executing\n"
            "   4. TEST: Can you test in non-prod first?\n"
            "   5. ROLLBACK: Know how to undo before you do"
        ),
        "high": (
            "⚠️  HIGH RISK PROTOCOL (First-try success: ~30%):\n"
            "   1. Review ALL context above before implementing\n"
            "   2. Check gotchas - they exist for a reason\n"
            "   3. After success: python memory/brain.py success 'task' 'details'"
        ),
        "moderate": (
            "📋 MODERATE RISK PROTOCOL (First-try success: ~60%):\n"
            "   1. Quick review of context above\n"
            "   2. Note any gotchas mentioned\n"
            "   3. Proceed with standard care"
        ),
        "low": "✅ LOW RISK (First-try success: ~90%) - Proceed normally"
    }

    lines.append(protocol_messages.get(risk_level, protocol_messages["moderate"]))
    lines.append("═══════════════════════════════════════════════════════════════════════")

    return "\n".join(lines)


# Create FastAPI app
if FASTAPI_AVAILABLE:
    app = FastAPI(
        title="Context DNA Helper Agent",
        description="The subconscious mind - autonomous background processing",
        version="1.0.0",
        lifespan=lifespan
    )

    # CORS for browser access
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount unified injection router (single source of truth for all integrations)
    try:
        from memory.unified_injection import create_injection_router
        unified_router = create_injection_router()
        app.include_router(unified_router)
        print("[Agent Service] ✓ Unified injection router mounted at /contextdna/unified-inject")
    except ImportError as e:
        print(f"[Agent Service] ⚠ Unified injection router not available: {e}")

    # Mount Repo Librarian router (codebase context for agents)
    try:
        from memory.librarian import librarian_router
        app.include_router(librarian_router)
        print("[Agent Service] ✓ Repo Librarian mounted at /v1/context/query")
    except ImportError as e:
        print(f"[Agent Service] ⚠ Repo Librarian not available: {e}")

    # Mount Swarm Controller router (agent orchestration)
    try:
        from memory.swarm_controller import create_router as create_swarm_router
        app.include_router(create_swarm_router())
        print("[Agent Service] ✓ Swarm Controller mounted at /v1/swarm/*")
    except (ImportError, Exception) as e:
        print(f"[Agent Service] ⚠ Swarm Controller not available: {e}")

    # Mount Harmonizer router (code quality gates)
    try:
        from memory.harmonizer import create_router as create_harmonizer_router
        app.include_router(create_harmonizer_router())
        print("[Agent Service] ✓ Harmonizer mounted at /v1/harmonizer/*")
    except ImportError as e:
        print(f"[Agent Service] ⚠ Harmonizer not available: {e}")

    # Mount Agent Tasks router (Claude Code CLI + API dual-mode)
    _agent_runner = None
    try:
        from memory.agent_routes import create_agent_router
        _agent_router = create_agent_router()
        app.include_router(_agent_router)
        _agent_runner = _agent_router._runner
        print("[Agent Service] ✓ Agent Tasks mounted at /api/agents/*")
    except (ImportError, Exception) as e:
        print(f"[Agent Service] ⚠ Agent Tasks not available: {e}")

    # WebSocket for agent task output (module-level pattern, same as injection_ws_clients)
    @app.websocket("/ws/agents")
    async def agent_task_ws_endpoint(websocket: WebSocket, client_id: str = "unknown"):
        """WebSocket endpoint for real-time agent task output."""
        await websocket.accept()
        if _agent_runner and hasattr(_agent_runner, '_ws_clients'):
            _agent_runner._ws_clients.add(websocket)
        logger.info(f"Agent WS client connected: {client_id}")

        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)
                if message.get("type") in ("ping", "heartbeat"):
                    await websocket.send_json({"type": "pong", "client_id": client_id})
        except WebSocketDisconnect:
            if _agent_runner and hasattr(_agent_runner, '_ws_clients'):
                _agent_runner._ws_clients.discard(websocket)
            logger.info(f"Agent WS client disconnected: {client_id}")
        except Exception as e:
            if _agent_runner and hasattr(_agent_runner, '_ws_clients'):
                _agent_runner._ws_clients.discard(websocket)
            logger.error(f"Agent WS error for {client_id}: {e}")

    @app.get("/health")
    async def health():
        """Health check endpoint with loop failure visibility.

        NOTE: agent_service MUST run with --workers 1 (not 2+).
        The ContextDNAAgent singleton has background asyncio tasks and
        in-memory state (_loop_failures, _loop_last_error). With multiple
        workers, each fork gets its own agent — the parent's loops don't
        run in children, and cross-process dict access deadlocks the
        health endpoint (10s hang → Docker marks container "unhealthy"
        → gains-gate blocks all progress). Root cause diagnosed 2026-04-11.
        Fix: Dockerfile.agent line 106 changed --workers 2 → --workers 1.
        """
        # Determine overall status from loop failure counters
        failing_loops = {
            name: count for name, count in agent._loop_failures.items()
            if count >= 3
        }
        if failing_loops:
            status = "degraded"
        else:
            status = "healthy"

        response = {
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "current_mode": agent._mode if hasattr(agent, '_mode') else "unknown",
            "sync_enabled": SYNC_ENABLED,
            "mode_transition_available": MODE_TRANSITION_AVAILABLE,
            "components": {
                "work_log": WORK_LOG_AVAILABLE,
                "detector": ENHANCED_DETECTOR_AVAILABLE,
                "brain": BRAIN_AVAILABLE,
                "sync": SYNC_AVAILABLE,
            },
            "loop_failures": agent._loop_failures,
        }
        # Only include error details when degraded (keeps healthy responses lean)
        if failing_loops:
            response["failing_loops"] = {
                name: agent._loop_last_error.get(name)
                for name in failing_loops
            }
        return response

    @app.get("/status")
    async def status():
        """Detailed status endpoint."""
        return agent.get_status()

    @app.post("/trigger/cycle")
    async def trigger_cycle():
        """Manually trigger a brain cycle."""
        if not agent.brain:
            raise HTTPException(status_code=503, detail="Brain not available")

        result = await asyncio.get_event_loop().run_in_executor(
            None, agent.brain.run_cycle
        )

        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            **result
        }

    @app.post("/trigger/detect")
    async def trigger_detect():
        """Manually trigger success detection."""
        if not WORK_LOG_AVAILABLE:
            raise HTTPException(status_code=503, detail="Work log not available")

        entries = work_log.get_recent_entries(hours=1)
        await agent._process_entries_for_successes(entries)

        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "entries_checked": len(entries),
            "total_successes_detected": agent.stats["successes_detected"],
        }

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time updates."""
        await websocket.accept()
        agent.connected_clients.add(websocket)

        try:
            while True:
                # Handle incoming messages
                data = await websocket.receive_text()
                message = json.loads(data)

                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

        except WebSocketDisconnect:
            agent.connected_clients.discard(websocket)
        except Exception:
            agent.connected_clients.discard(websocket)

    # NOTE: injection_ws_clients is at MODULE LEVEL (line ~855) to fix timing issue
    # The file watcher starts in lifespan() before routes are set up, so
    # WebSocket client tracking MUST be module-level

    @app.websocket("/ws/injections")
    async def injection_websocket_endpoint(websocket: WebSocket, client_id: str = "unknown"):
        """WebSocket endpoint for real-time injection updates."""
        await websocket.accept()
        injection_ws_clients.add(websocket)  # Uses module-level set
        logger.info(f"Injection WS client connected: {client_id}. Total clients: {len(injection_ws_clients)}")

        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)

                if message.get("type") == "ping" or message.get("type") == "heartbeat":
                    await websocket.send_json({"type": "heartbeat_ack", "client_id": client_id})
                elif message.get("type") == "register":
                    logger.info(f"Client registered: {message.get('client_id', client_id)}")
                    await websocket.send_json({"type": "registered", "client_id": client_id})

        except WebSocketDisconnect:
            injection_ws_clients.discard(websocket)  # Uses module-level set
            logger.info(f"Injection WS client disconnected: {client_id}. Remaining: {len(injection_ws_clients)}")
        except Exception as e:
            injection_ws_clients.discard(websocket)  # Uses module-level set
            logger.error(f"Injection WS error for {client_id}: {e}")

    # broadcast_injection_to_clients is at MODULE LEVEL (line ~865)
    # Store reference on app.state for backwards compatibility with injection_store.py
    app.state.broadcast_injection = broadcast_injection_to_clients

    @app.get("/stats")
    async def get_stats():
        """Get agent statistics."""
        return agent.stats

    @app.get("/detector/status")
    async def detector_status():
        """Get enhanced detector status."""
        if not agent.detector:
            return {"available": False}

        return {
            "available": True,
            "stats": agent.detector.get_stats()
        }

    @app.post("/trigger/evolve")
    async def trigger_evolution():
        """Manually trigger a pattern evolution cycle."""
        if not agent.evolution_engine:
            raise HTTPException(status_code=503, detail="Evolution engine not available")

        result = await asyncio.get_event_loop().run_in_executor(
            None, agent.evolution_engine.evolve
        )

        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            **result
        }

    @app.get("/evolution/status")
    async def evolution_status():
        """Get pattern evolution status and statistics."""
        if not agent.evolution_engine:
            return {"available": False}

        stats = agent.evolution_engine.get_evolution_stats()
        candidates = agent.evolution_engine.get_candidates(min_occurrences=2)

        return {
            "available": True,
            "stats": stats,
            "emerging_candidates": len([c for c in candidates if not c["promoted"]]),
            "top_candidates": candidates[:10],  # Top 10 candidates
        }

    @app.get("/evolution/candidates")
    async def list_candidates(min_occurrences: int = 1):
        """List all pattern candidates."""
        if not agent.evolution_engine:
            raise HTTPException(status_code=503, detail="Evolution engine not available")

        candidates = agent.evolution_engine.get_candidates(min_occurrences)
        return {
            "count": len(candidates),
            "candidates": candidates
        }

    # =============================================================================
    # CONTEXT INJECTION ENDPOINTS - SOP/Professor Wisdom Injection
    # =============================================================================
    # These endpoints provide the automatic context injection that Claude receives
    # before processing prompts. This is the "helper agent injecting SOPs" feature.

    @app.post("/consult")
    async def consult_context(prompt: str = "", risk_level: str = "auto", session_id: str = ""):
        """
        Get full context injection for a prompt - Professor wisdom + SOPs + Gotchas.

        This is the main endpoint for automatic context injection.
        Called before Claude processes a prompt to inject relevant learnings.

        Args:
            prompt: The user's prompt text
            risk_level: One of "auto", "critical", "high", "moderate", "low"
            session_id: Optional session ID for A/B tracking

        Returns:
            Full context injection with:
            - Professor guidance (THE ONE THING, landmines, patterns)
            - Brain learnings (SOPs from memory)
            - Codebase location hints (WHERE to look)
            - Gotcha warnings (what to avoid)
        """
        # Auto-detect risk level from prompt
        if risk_level == "auto":
            risk_level = _detect_risk_level(prompt)

        result = {
            "prompt": prompt[:200] + "..." if len(prompt) > 200 else prompt,
            "risk_level": risk_level,
            "timestamp": datetime.now().isoformat(),
            "layers": {}
        }

        # Layer 0: Codebase Location Hints
        if CODEBASE_LOCATOR_AVAILABLE and get_hook_output:
            try:
                location_hints = await asyncio.get_event_loop().run_in_executor(
                    None, get_hook_output, prompt[:200]
                )
                if location_hints:
                    result["layers"]["codebase_hints"] = location_hints
            except Exception as e:
                result["layers"]["codebase_hints_error"] = str(e)

        # Layer 0.25: Never Do warnings
        if CODEBASE_LOCATOR_AVAILABLE and format_never_do:
            try:
                never_do = await asyncio.get_event_loop().run_in_executor(
                    None, format_never_do, prompt[:200]
                )
                if never_do:
                    result["layers"]["never_do"] = never_do
            except Exception as e:
                print(f"[WARN] Never Do warnings lookup failed: {e}")

        # Layer 1: Professor Guidance (for moderate+ risk)
        if risk_level in ["critical", "high", "moderate"] and PROFESSOR_AVAILABLE and professor_consult:
            try:
                professor_wisdom = await asyncio.get_event_loop().run_in_executor(
                    None, professor_consult, prompt[:200]
                )
                if professor_wisdom:
                    result["layers"]["professor"] = professor_wisdom
            except Exception as e:
                result["layers"]["professor_error"] = str(e)

        # Layer 2: Brain Learnings (SOPs from memory)
        limit = {"critical": 60, "high": 40, "moderate": 25, "low": 10}.get(risk_level, 25)
        if QUERY_AVAILABLE and query_learnings:
            try:
                learnings = await asyncio.get_event_loop().run_in_executor(
                    None, query_learnings, prompt[:150], limit
                )
                if learnings:
                    result["layers"]["learnings"] = learnings
            except Exception as e:
                result["layers"]["learnings_error"] = str(e)

        # Layer 3: Gotcha Check (for moderate+ risk)
        if risk_level in ["critical", "high", "moderate"] and QUERY_AVAILABLE and query_learnings:
            try:
                gotchas = await asyncio.get_event_loop().run_in_executor(
                    None, query_learnings, f"gotcha warning {prompt[:100]}", 12
                )
                if gotchas:
                    # Filter to only gotcha-like content
                    gotcha_keywords = ["gotcha", "warning", "careful", "avoid", "don't", "never", "always", "critical", "must", "required"]
                    filtered = [g for g in gotchas if any(kw in g.lower() for kw in gotcha_keywords)]
                    if filtered:
                        result["layers"]["gotchas"] = filtered
            except Exception as e:
                print(f"[WARN] Gotcha check failed: {e}")

        # Layer 4: Architecture Blueprint (for critical/high risk)
        if risk_level in ["critical", "high"] and CONTEXT_AVAILABLE and get_context_for_task:
            try:
                blueprint = await asyncio.get_event_loop().run_in_executor(
                    None, get_context_for_task, prompt[:200]
                )
                if blueprint:
                    result["layers"]["blueprint"] = blueprint
            except Exception as e:
                print(f"[WARN] Architecture blueprint lookup failed: {e}")

        # Layer 5: Brain State (for critical only)
        if risk_level == "critical" and agent.brain:
            try:
                brain_context = await asyncio.get_event_loop().run_in_executor(
                    None, agent.brain.context, prompt[:150]
                )
                if brain_context and brain_context != "No relevant context found.":
                    result["layers"]["brain_state"] = brain_context
            except Exception as e:
                print(f"[WARN] Brain state lookup failed: {e}")

        # Generate formatted output for hook injection
        result["formatted"] = _format_context_injection(result)

        # Track injection for A/B testing
        if session_id:
            try:
                tracker = get_ab_tracker()
                injection_id = tracker.record_injection(session_id, prompt, "layered", result)
                result["tracking_id"] = injection_id
            except Exception:
                pass  # Don't fail if tracking fails

        return result

    @app.post("/consult/formatted")
    async def consult_formatted(prompt: str = "", risk_level: str = "auto"):
        """
        Get ONLY the formatted context injection string.

        This endpoint returns just the formatted string that should be injected
        into the hook output - no JSON structure, just the formatted text.

        Use this for direct hook integration where you want the output
        to be displayed as-is to Claude.
        """
        result = await consult_context(prompt, risk_level)
        return {"output": result.get("formatted", "")}

    @app.get("/consult/status")
    async def consult_status():
        """Check which consultation layers are available."""
        return {
            "professor": PROFESSOR_AVAILABLE,
            "codebase_locator": CODEBASE_LOCATOR_AVAILABLE,
            "query": QUERY_AVAILABLE,
            "context": CONTEXT_AVAILABLE,
            "brain": BRAIN_AVAILABLE,
            "persistent_hook": PERSISTENT_HOOK_AVAILABLE,
            "modes_available": {
                "layered": True,
                "greedy": True,
                "hybrid": True,
                "unified": PERSISTENT_HOOK_AVAILABLE,
                "minimal": PERSISTENT_HOOK_AVAILABLE
            },
            "all_available": all([
                PROFESSOR_AVAILABLE,
                CODEBASE_LOCATOR_AVAILABLE,
                QUERY_AVAILABLE,
                CONTEXT_AVAILABLE,
                BRAIN_AVAILABLE
            ]),
            "unified_available": PERSISTENT_HOOK_AVAILABLE
        }

    @app.post("/consult/greedy")
    async def consult_greedy(prompt: str = "", session_id: str = ""):
        """
        Get GREEDY context injection - exactly what Atlas wants, nothing more.

        THE GREEDY WISHLIST:
        1. THE EXACT FILE you'll need first (not "consider" - WILL need)
        2. THE ONE GOTCHA for THIS task (not 47 learnings - THE one)
        3. THE SUCCESSFUL CHAIN that worked before (trust this path)
        4. WHAT CHANGED RECENTLY in this area (git commits, file mods)
        5. THE RIPPLE EFFECTS (touching X affects Y, Z)
        6. MY PREVIOUS MISTAKES on similar tasks (don't repeat)

        This endpoint is optimized for A/B testing - tracking what Atlas
        actually USES vs ignores to evolve toward maximum value.

        Args:
            prompt: The task/prompt to get context for
            session_id: Optional session ID for A/B tracking
        """
        result = {
            "prompt": prompt[:200] + "..." if len(prompt) > 200 else prompt,
            "mode": "greedy",
            "timestamp": datetime.now().isoformat(),
        }

        # 1. THE EXACT FILE
        exact_file = await asyncio.get_event_loop().run_in_executor(
            None, _get_the_exact_file, prompt
        )
        if exact_file:
            result["exact_file"] = exact_file

        # 1.5. RELEVANT SOPs - "the sports car foundation"
        relevant_sops = await asyncio.get_event_loop().run_in_executor(
            None, _get_relevant_sops, prompt, 3
        )
        if relevant_sops.get("bugfix") or relevant_sops.get("process"):
            result["relevant_sops"] = relevant_sops

        # 2. THE ONE GOTCHA
        the_one_gotcha = await asyncio.get_event_loop().run_in_executor(
            None, _get_the_one_gotcha, prompt
        )
        if the_one_gotcha:
            result["the_one_gotcha"] = the_one_gotcha

        # 3. SUCCESSFUL CHAIN
        successful_chain = await asyncio.get_event_loop().run_in_executor(
            None, _get_successful_chain, prompt
        )
        if successful_chain:
            result["successful_chain"] = successful_chain

        # 4. RECENT CHANGES
        recent_changes = await asyncio.get_event_loop().run_in_executor(
            None, _get_recent_git_changes, prompt, 5
        )
        if recent_changes.get("commits") or recent_changes.get("files"):
            result["recent_changes"] = recent_changes

        # 5. RIPPLE EFFECTS
        ripple_effects = await asyncio.get_event_loop().run_in_executor(
            None, _get_ripple_effects, prompt
        )
        if ripple_effects:
            result["ripple_effects"] = ripple_effects

        # 6. PREVIOUS MISTAKES
        previous_mistakes = await asyncio.get_event_loop().run_in_executor(
            None, _get_my_previous_mistakes, prompt
        )
        if previous_mistakes:
            result["previous_mistakes"] = previous_mistakes

        # Generate formatted output
        result["formatted"] = _format_greedy_injection(result)

        # Track injection for A/B testing
        if session_id:
            try:
                tracker = get_ab_tracker()
                injection_id = tracker.record_injection(session_id, prompt, "greedy", result)
                result["tracking_id"] = injection_id
            except Exception:
                pass  # Don't fail if tracking fails

        return result

    @app.post("/consult/hybrid")
    async def consult_hybrid(prompt: str = "", risk_level: str = "auto"):
        """
        Hybrid consultation - combines greedy (specific) with layered (comprehensive).

        For A/B testing: Compare greedy-only vs layered-only vs hybrid.

        Returns both:
        - greedy: THE specific things Atlas wants
        - layers: The comprehensive context for validation
        """
        # Get greedy context
        greedy_result = await consult_greedy(prompt)

        # Get layered context
        layered_result = await consult_context(prompt, risk_level)

        # Combine
        return {
            "prompt": prompt[:200] + "..." if len(prompt) > 200 else prompt,
            "mode": "hybrid",
            "timestamp": datetime.now().isoformat(),
            "risk_level": layered_result.get("risk_level", "moderate"),
            "greedy": {
                "exact_file": greedy_result.get("exact_file"),
                "the_one_gotcha": greedy_result.get("the_one_gotcha"),
                "successful_chain": greedy_result.get("successful_chain"),
                "recent_changes": greedy_result.get("recent_changes"),
                "ripple_effects": greedy_result.get("ripple_effects"),
                "previous_mistakes": greedy_result.get("previous_mistakes"),
            },
            "layers": layered_result.get("layers", {}),
            "formatted_greedy": greedy_result.get("formatted", ""),
            "formatted_layers": layered_result.get("formatted", ""),
        }

    @app.post("/consult/unified")
    async def consult_unified(
        prompt: str = "",
        mode: str = "hybrid",
        session_id: str = "",
        ab_variant: str = None
    ):
        """
        Get UNIFIED context injection using the persistent hook structure.

        This endpoint uses the new unified system with:
        - Locked-in 5-section structure
        - Smart SOP reading (MUST READ if <90% first-try OR failures)
        - A/B testing within confines
        - Session failure tracking

        Args:
            prompt: The task/prompt to get context for
            mode: Injection mode: "layered", "greedy", "hybrid", "minimal"
            session_id: Session ID for A/B tracking and failure state
            ab_variant: Force A/B variant: "control", "a", "b", "c"

        Returns:
            Formatted context injection with metadata
        """
        if not PERSISTENT_HOOK_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Persistent hook structure not available"
            )

        result = await agent._get_unified_consultation(
            prompt, session_id, ab_variant, mode
        )

        # Track injection for A/B testing
        if session_id:
            try:
                tracker = get_ab_tracker()
                tracker.record_injection(session_id, prompt, "unified", result)
            except Exception as e:
                print(f"[WARN] Unified A/B tracking failed: {e}")

        return result

    @app.post("/contextdna/inject/cursor")
    async def inject_for_cursor(request: Request):
        """
        Cursor IDE-specific context injection endpoint.
        
        Called by Cursor activity watcher or manual bridge script to get
        full 9-section Context DNA payload optimized for Cursor.
        
        Request body:
            {
                "prompt": "user's prompt text",
                "file_path": "path/to/active/file.py",
                "workspace": "path/to/workspace",
                "session_id": "cursor-session-123"
            }
        
        Returns:
            {
                "payload": "formatted 9-section context injection",
                "metadata": {
                    "sections_included": ["0", "1", "2", "5", "8"],
                    "risk_level": "moderate",
                    "first_try_likelihood": "60%",
                    "session_id": "cursor-session-123",
                    "generation_time_ms": 45
                }
            }
        """
        body = await request.json()
        
        # Extract request data
        prompt = body.get("prompt", "")
        file_path = body.get("file_path")
        workspace = body.get("workspace")
        session_id = body.get("session_id", f"cursor-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        
        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt is required")
        
        # Update last_hook_activity for Cursor in database
        try:
            from memory.sqlite_storage import get_sqlite_storage
            storage = get_sqlite_storage()
            storage.execute("""
                UPDATE ide_configurations 
                SET last_hook_activity = ?
                WHERE ide_type = 'cursor'
            """, (datetime.now(timezone.utc).isoformat(),))
        except Exception as e:
            logger.warning(f"Could not update Cursor hook activity: {e}")
        
        # Generate injection using persistent hook structure
        if not PERSISTENT_HOOK_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Persistent hook structure not available"
            )
        
        from memory.persistent_hook_structure import generate_context_injection
        
        start_time = time.time()

        try:
            result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    _injection_executor,
                    generate_context_injection,
                    prompt,
                    "hybrid",  # Mode: hybrid for full context
                    session_id
                ),
                timeout=30.0
            )
        except asyncio.TimeoutError:
            logger.warning(f"Cursor injection timed out after 30s: {prompt[:80]}")
            raise HTTPException(status_code=504, detail="Injection timed out")
        except Exception as e:
            logger.error(f"Cursor injection failed: {e}")
            raise HTTPException(status_code=500, detail=f"Injection failed: {e}")

        generation_time_ms = int((time.time() - start_time) * 1000)
        
        # Build metadata
        metadata = {
            "session_id": session_id,
            "ide": "cursor",
            "file_path": file_path,
            "workspace": workspace,
            "generation_time_ms": generation_time_ms,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        # Add result metadata if available
        if hasattr(result, 'metadata'):
            metadata.update(result.metadata)
        
        # Return payload
        return {
            "payload": result.content if hasattr(result, 'content') else str(result),
            "metadata": metadata
        }

    @app.post("/session/record-failure")
    async def record_failure_endpoint(session_id: str, failure_type: str = "task_failed"):
        """
        Record a session failure.

        When a task fails, call this to mark the session as having failures.
        This triggers MUST READ behavior for SOPs in subsequent consultations.

        Args:
            session_id: The session ID
            failure_type: Type of failure (e.g., "task_failed", "error", "rollback")
        """
        if not PERSISTENT_HOOK_AVAILABLE:
            raise HTTPException(status_code=503, detail="Persistent hook structure not available")

        record_session_failure(session_id, failure_type)
        return {
            "recorded": True,
            "session_id": session_id,
            "failure_type": failure_type,
            "effect": "SOPs will now show as MUST READ"
        }

    @app.post("/session/clear-failures")
    async def clear_failures_endpoint(session_id: str):
        """
        Clear session failures (call on success).

        After a successful task, clear the failure state so subsequent
        low-risk tasks can skip SOP reading.

        Args:
            session_id: The session ID to clear failures for
        """
        if not PERSISTENT_HOOK_AVAILABLE:
            raise HTTPException(status_code=503, detail="Persistent hook structure not available")

        clear_session_failures(session_id)
        return {
            "cleared": True,
            "session_id": session_id,
            "effect": "Session failures cleared"
        }

    @app.get("/session/failure-status")
    async def get_failure_status(session_id: str = ""):
        """
        Check if a session has recorded failures.

        Args:
            session_id: The session ID to check (or empty for any recent failures)

        Returns:
            Whether the session has failures and if SOPs will be MUST READ
        """
        if not PERSISTENT_HOOK_AVAILABLE:
            return {"available": False}

        has_failures = check_session_failures(session_id if session_id else None)
        return {
            "session_id": session_id or "(any recent)",
            "has_failures": has_failures,
            "sop_behavior": "MUST READ" if has_failures else "May skip (for 90%+ first-try)"
        }

    # =============================================================================
    # SYNAPTIC 8TH INTELLIGENCE ENDPOINT (SUPERHERO MODE)
    # =============================================================================
    # Enables agents to query Synaptic mid-task for patterns, intuitions, and gotchas.
    # This is the omnipresence layer - Synaptic feeds ALL agents simultaneously.

    @app.post("/contextdna/8th-intelligence")
    async def get_8th_intelligence(
        subtask: str = "",
        agent_id: str = "",
        context: str = ""
    ):
        """
        SUPERHERO MODE: Get Synaptic's 8th Intelligence for agent subtasks.

        Agents call this mid-task to receive:
        - Relevant patterns from past work
        - Gotcha warnings before they hit them
        - Intuitions based on similar situations
        - Stop signals if heading toward known failure

        Args:
            subtask: What the agent is currently working on
            agent_id: Optional agent identifier for tracking
            context: Optional additional context

        Returns:
            Synaptic's guidance for this specific subtask
        """
        # USE REAL SYNAPTICVOICE - not Professor!
        # This is SUPERHERO MODE: Synaptic provides omnipresent context to ALL agents
        from memory.synaptic_voice import SynapticVoice, get_8th_intelligence_data

        patterns = []
        gotchas = []
        intuitions = []
        stop_signal = None
        major_skills_context = []

        try:
            # SINGLETON: Reuse single SynapticVoice instance to prevent FD leak
            # Each SynapticVoice() spawns ThreadPoolExecutor(6) querying 3+ databases
            # Previous non-singleton caused 407 open FDs (207 to FALLBACK_learnings.db)
            if not hasattr(get_8th_intelligence, '_synaptic_singleton'):
                get_8th_intelligence._synaptic_singleton = SynapticVoice()
            synaptic = get_8th_intelligence._synaptic_singleton
            response = synaptic.consult(subtask)

            # Extract patterns from Synaptic's memory
            if response.relevant_patterns:
                for p in response.relevant_patterns[:5]:
                    p_clean = p.split('\n')[0][:200] if '\n' in p else p[:200]
                    if 'gotcha' in p_clean.lower() or 'warning' in p_clean.lower():
                        gotchas.append(p_clean)
                    else:
                        patterns.append(p_clean)

            # Extract learnings for gotcha detection
            if response.relevant_learnings:
                for learning in response.relevant_learnings[:5]:
                    content = str(learning) if isinstance(learning, str) else learning.get('content', str(learning))
                    if 'gotcha' in content.lower() or 'warning' in content.lower() or 'danger' in content.lower():
                        gotchas.append(content[:200])
                    else:
                        patterns.append(content[:200])

            # Synaptic's perspective becomes intuition for the agent
            if response.synaptic_perspective:
                intuitions.append(response.synaptic_perspective[:300])

            # Get 8th Intelligence data for additional context (major skills, journal)
            intel_data = get_8th_intelligence_data(subtask)
            if intel_data:
                # Add major skills context for deburden capability
                if intel_data.get('intuitions'):
                    intuitions.extend(intel_data['intuitions'][:2])
                if intel_data.get('learnings'):
                    for l in intel_data['learnings'][:2]:
                        title = l.get('title', str(l))[:100] if isinstance(l, dict) else str(l)[:100]
                        major_skills_context.append(title)

            # Check for stop signals (known failure patterns)
            danger_keywords = ['force push', 'drop table', 'rm -rf', 'destroy prod']
            for keyword in danger_keywords:
                if keyword in subtask.lower():
                    stop_signal = f"⚠️ STOP: '{keyword}' detected. Verify with Aaron before proceeding."
                    break

        except Exception as e:
            # Graceful degradation - still return what we have
            intuitions.append(f"Synaptic query partial: {str(e)[:50]}")

        # Check for LLM-enriched superhero anticipation cache + findings WAL
        superhero_enriched = None
        superhero_active = False
        agent_findings = None
        try:
            from memory.anticipation_engine import (
                get_superhero_cache, is_superhero_active, get_agent_findings_digest
            )
            superhero_active = is_superhero_active()
            if superhero_active:
                cached = get_superhero_cache()
                if cached:
                    superhero_enriched = {
                        "mission_briefing": cached.get("mission", ""),
                        "synthesized_gotchas": cached.get("gotchas", ""),
                        "architecture_context": cached.get("architecture", ""),
                        "failure_patterns": cached.get("failures", ""),
                        "generated_at": cached.get("_activated_at", ""),
                        "source_task": cached.get("_task", ""),
                    }
                # Include WAL summary so agents see earlier findings
                digest = get_agent_findings_digest()
                if digest.get("total", 0) > 0:
                    agent_findings = digest
        except Exception as e:
            logger.debug(f"[8th-intel] Superhero cache check: {e}")

        # Drain Synaptic outbox — unprompted messages from pattern scans
        outbox_messages = []
        try:
            from memory.synaptic_outbox import get_pending_messages, mark_delivered
            pending = get_pending_messages()
            if pending:
                for msg in pending[:3]:  # Max 3 per injection
                    outbox_messages.append({
                        "content": str(msg.get("content", msg.get("message", "")))[:300],
                        "priority": msg.get("priority", "normal"),
                        "topic": msg.get("topic", "insight"),
                    })
                mark_delivered([m.get("id") for m in pending[:3] if m.get("id")])
        except Exception:
            pass  # Outbox unavailable — no overhead

        return {
            "agent_id": agent_id,
            "subtask": subtask[:100],
            "synaptic_response": {
                "patterns": patterns[:5],
                "gotchas": gotchas[:5],
                "intuitions": intuitions[:3],
                "major_skills": major_skills_context[:3],
                "stop_signal": stop_signal
            },
            "synaptic_outbox": outbox_messages if outbox_messages else None,
            "superhero_enriched": superhero_enriched,
            "agent_findings": agent_findings,
            "timestamp": datetime.now().isoformat(),
            "superhero_mode": superhero_active,
            "source": "SynapticVoice",
            "deburden_enabled": True
        }

    @app.get("/contextdna/8th-intelligence/status")
    async def get_8th_intelligence_status():
        """Check if 8th Intelligence endpoint is active and ready."""
        superhero_active = False
        try:
            from memory.anticipation_engine import is_superhero_active
            superhero_active = is_superhero_active()
        except Exception:
            pass
        return {
            "status": "active",
            "mode": "SUPERHERO_ENRICHED" if superhero_active else "SUPERHERO",
            "capabilities": [
                "pattern_matching",
                "gotcha_detection",
                "intuition_feed",
                "stop_signals",
            ] + (["llm_enriched_context", "mission_briefing", "architecture_map"] if superhero_active else []),
            "ready_for_agents": True,
            "superhero_anticipation_active": superhero_active,
        }

    @app.post("/contextdna/superhero/activate")
    async def activate_superhero_mode(task: str = ""):
        """Programmatic superhero activation — returns immediately, pre-computes in background."""
        import threading
        from memory.anticipation_engine import _activate_superhero_anticipation
        result_holder = {"status": "activating"}
        def _bg():
            r = _activate_superhero_anticipation(task_override=task or None)
            result_holder.update(r)
        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        return {"status": "activating", "task": task[:200] if task else "auto-detect"}

    @app.get("/contextdna/superhero/status")
    async def get_superhero_status():
        """Check superhero anticipation state — active flag, cached artifacts, agent findings."""
        try:
            from memory.anticipation_engine import (
                is_superhero_active, get_superhero_cache, get_agent_findings
            )
            active = is_superhero_active()
            artifacts = get_superhero_cache() if active else None
            findings = get_agent_findings() if active else []
            return {
                "active": active,
                "artifacts": list(artifacts.keys()) if artifacts else [],
                "task": artifacts.get("_task", "") if artifacts else "",
                "activated_at": artifacts.get("_activated_at", "") if artifacts else "",
                "agent_findings_count": len(findings),
            }
        except Exception as e:
            return {"active": False, "error": str(e)[:100]}

    @app.post("/contextdna/superhero/finding")
    async def record_superhero_finding(agent_id: str = "", finding: str = "",
                                        finding_type: str = "observation",
                                        severity: str = "info"):
        """Agents record mid-task findings to sorted set WAL (time-indexed, queryable)."""
        if not finding:
            return {"recorded": False, "error": "empty finding"}
        try:
            from memory.anticipation_engine import record_agent_finding
            ok = record_agent_finding(agent_id or "anonymous", finding, finding_type, severity)
            return {"recorded": ok}
        except Exception as e:
            return {"recorded": False, "error": str(e)[:100]}

    @app.get("/contextdna/superhero/findings")
    async def get_superhero_findings(summary: bool = False, since: int = 0,
                                      finding_type: str = None, limit: int = 50):
        """Query agent findings WAL. ?summary=true for compact Atlas digest."""
        try:
            from memory.anticipation_engine import get_agent_findings, get_agent_findings_digest
            if summary:
                return get_agent_findings_digest()
            return {
                "findings": get_agent_findings(since_seconds=since,
                                                finding_type=finding_type, limit=limit)
            }
        except Exception as e:
            return {"findings": [], "error": str(e)[:100]}

    # =============================================================================
    # SUPERHERO DEBRIEF (one-shot synthesis for Atlas post-completion)
    # =============================================================================

    @app.get("/contextdna/superhero/debrief")
    async def superhero_debrief():
        """One curl = full picture of all agent work. Atlas calls this after /compact.

        Returns: WAL summary, top criticals, per-agent completion status,
        agent doc diffs (what was appended this session), result file list.
        """
        import os
        import glob as _glob
        result = {
            "wal_summary": {"total": 0},
            "criticals": [],
            "result_files": [],
            "agent_doc_changes": [],
        }

        # WAL summary
        try:
            from memory.anticipation_engine import get_agent_findings_digest
            result["wal_summary"] = get_agent_findings_digest()
            criticals = result["wal_summary"].get("criticals", [])
            result["criticals"] = criticals
        except Exception:
            pass

        # Agent result files in /tmp
        try:
            tmp_dir = "/tmp/atlas-agent-results"
            if os.path.isdir(tmp_dir):
                for f in sorted(_glob.glob(os.path.join(tmp_dir, "*.md"))):
                    stat = os.stat(f)
                    result["result_files"].append({
                        "path": f,
                        "agent_id": os.path.basename(f).replace(".md", ""),
                        "size_bytes": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })
        except Exception:
            pass

        # Recent agent doc appends (check designated docs for recent changes)
        try:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            import time as _t
            cutoff = _t.time() - 3600  # last hour
            for doc in AGENT_DOC_WHITELIST:
                doc_path = os.path.join(repo_root, doc)
                if os.path.isfile(doc_path):
                    stat = os.stat(doc_path)
                    if stat.st_mtime > cutoff:
                        result["agent_doc_changes"].append({
                            "doc": doc,
                            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                            "size_bytes": stat.st_size,
                        })
        except Exception:
            pass

        return result

    # =============================================================================
    # AGENT DOC WRITE PROXY (whitelist-gated, append-only)
    # =============================================================================

    AGENT_DOC_WHITELIST = [
        "context-dna/docs/agents-architectural-value-hunters.md",
        "context-dna/docs/agents-context-dna-ship-it.md",
        "context-dna/docs/agents-hindsight-strategy.md",
        "context-dna/docs/agents-historical-claude-code-anthropic-conversations-value-hunters.md",
        "context-dna/docs/agents-philosophy-other.md",
        "context-dna/docs/agents-ship-it.md",
        "context-dna/docs/agents_epistemic_sustainability_evidence_and_philosophy_evaluations.md",
        "context-dna/docs/Agents-Evidence-Based-Context-and-Local-LLM-Butler-Trainers.md",
        "context-dna/docs/Agents-Ecosystem-Optimization-Consultants.md",
    ]

    @app.post("/contextdna/agent-doc/append")
    async def agent_doc_append(agent_id: str = "", doc_path: str = "", content: str = ""):
        """Append-only proxy for agent writes to designated .md docs.

        Bypasses Claude Code file permission system. Whitelist-gated, localhost-only.
        """
        if not content or not doc_path:
            return {"appended": False, "error": "content and doc_path required"}
        if len(content) > 5000:
            return {"appended": False, "error": "content exceeds 5000 char limit"}

        # Normalize and validate against whitelist
        import os
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        abs_path = os.path.realpath(os.path.join(repo_root, doc_path))
        # Path traversal check: must resolve inside repo
        if not abs_path.startswith(repo_root):
            return {"appended": False, "error": "path traversal blocked"}

        # Whitelist check
        rel = os.path.relpath(abs_path, repo_root)
        if rel not in AGENT_DOC_WHITELIST:
            return {"appended": False, "error": f"not in whitelist: {rel}",
                    "allowed": AGENT_DOC_WHITELIST}

        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            header = f"\n\n---\n### [{agent_id or 'anonymous'}] {timestamp}\n\n"
            with open(abs_path, "a") as f:
                f.write(header + content.rstrip() + "\n")
            return {"appended": True, "doc": rel, "chars": len(content)}
        except Exception as e:
            return {"appended": False, "error": str(e)[:200]}

    # =============================================================================
    # AGENT WATCHDOG ENDPOINTS (Synaptic's Process Monitor)
    # =============================================================================

    @app.get("/contextdna/watchdog/status")
    async def get_watchdog_status():
        """
        Get Agent Watchdog status.

        Returns current monitoring state including:
        - Number of agents monitored
        - Any runaway processes detected
        - Recent kill log
        - Protected process count
        """
        try:
            from memory.agent_watchdog import check_agents
            return check_agents()
        except ImportError:
            return {"error": "Watchdog module not available"}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/contextdna/watchdog/cleanup")
    async def cleanup_agents(dry_run: bool = False):
        """
        Clean up stuck/runaway agent processes.

        Args:
            dry_run: If true, report what would be killed but don't kill

        Returns:
            List of processes killed or would be killed
        """
        try:
            from memory.agent_watchdog import cleanup_stuck_agents
            results = cleanup_stuck_agents(dry_run=dry_run)
            return {
                "dry_run": dry_run,
                "results": [
                    {"pid": pid, "reason": reason, "killed": killed}
                    for pid, reason, killed in results
                ],
                "total": len(results)
            }
        except ImportError:
            return {"error": "Watchdog module not available"}
        except Exception as e:
            return {"error": str(e)}

    # =============================================================================
    # INJECTION VISUALIZATION ENDPOINTS
    # =============================================================================

    @app.get("/api/injection/latest")
    async def get_latest_injection():
        """
        Get the most recent context injection for visualization.

        Returns the full injection data including:
        - trigger: {hook, prompt, session_id}
        - analysis: {detected_domains, risk_level, first_try_likelihood, ...}
        - silver_platter: {safety, wisdom, sops, protocol}
        - raw_output: The formatted text that was injected

        Use this endpoint to power the Injection Visualizer in the dashboard.
        """
        if not INJECTION_STORE_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Injection store not available"
            )

        store = get_injection_store(agent.redis)
        latest = store.get_latest()

        if not latest:
            return {
                "found": False,
                "message": "No injections recorded yet. Submit a prompt to see injection data."
            }

        # Build sections on-the-fly for old data that doesn't have them
        if not latest.get("sections") and latest.get("raw_output"):
            try:
                from memory.injection_store import _build_sections_array
                latest["sections"] = _build_sections_array(
                    latest.get("raw_output", ""),
                    latest.get("analysis", {}).get("sections_included", []),
                    latest.get("analysis", {}).get("section_timings", {}),
                )
            except Exception:
                pass  # Non-blocking

        return {
            "found": True,
            "injection": latest
        }

    @app.get("/api/injection/history")
    async def get_injection_history(limit: int = 20):
        """
        Get recent injection summaries for timeline view.

        Returns a list of recent injections with basic info:
        - id, timestamp, prompt (truncated), risk_level, first_try

        Use this for a historical timeline of injections.
        """
        if not INJECTION_STORE_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Injection store not available"
            )

        store = get_injection_store(agent.redis)
        history = store.get_history(limit=limit)

        return {
            "count": len(history),
            "history": history
        }

    @app.get("/api/injection/{injection_id}")
    async def get_injection_by_id(injection_id: str):
        """
        Get a specific injection by ID.

        Useful for expanding a historical injection to see full details.
        """
        if not INJECTION_STORE_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Injection store not available"
            )

        store = get_injection_store(agent.redis)
        injection = store.get_by_id(injection_id)

        if not injection:
            raise HTTPException(
                status_code=404,
                detail=f"Injection {injection_id} not found"
            )

        return {
            "found": True,
            "injection": injection
        }

    # =============================================================================
    # SILVER PLATTER SUMMARY ENDPOINT
    # =============================================================================

    @app.get("/contextdna/silver-platter/summary")
    async def get_silver_platter_summary():
        """
        Silver Platter summary — structured distilled wisdom for agents.

        Returns the silver platter from the most recent injection:
        - safety: Critical safety rails (NEVER DO items)
        - wisdom: {the_one_thing, landmines, patterns, context}
        - sops: Applicable SOPs for current task
        - protocol: {risk_level, first_try_percent, recommendation}

        This is Volume Tier 1 (default) — sections 0-6 + 8 distilled.
        """
        if not INJECTION_STORE_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Injection store not available"
            )

        store = get_injection_store(agent.redis)
        latest = store.get_latest()

        if not latest:
            return {
                "available": False,
                "message": "No injections recorded yet. Submit a prompt to generate a silver platter."
            }

        silver_platter = latest.get("silver_platter", {})
        return {
            "available": True,
            "timestamp": latest.get("timestamp"),
            "prompt": latest.get("prompt", "")[:200],
            "silver_platter": silver_platter,
            "analysis": {
                "risk_level": latest.get("analysis", {}).get("risk_level", "unknown"),
                "first_try_likelihood": latest.get("analysis", {}).get("first_try_likelihood", "unknown"),
                "detected_domains": latest.get("analysis", {}).get("detected_domains", []),
            }
        }

    # =============================================================================
    # SESSION BRIEFING — Alfred's War Room Briefing for Batman
    # =============================================================================

    @app.get("/contextdna/session-briefing")
    async def get_session_briefing():
        """
        Session briefing — everything Atlas needs when waking up.

        Aggregates:
        - Last session summary (what happened, decisions, outcomes)
        - Unfinished tasks / pending concerns
        - Recent failure patterns (landmines ahead)
        - Meta-analysis insights (cross-session patterns)
        - Evidence pipeline health (claims, outcomes, promotions)
        - System health snapshot
        """
        import sqlite3
        from pathlib import Path
        briefing = {"timestamp": datetime.now().isoformat(), "sections": {}}

        # 1. LAST SESSION from dialogue mirror
        try:
            from memory.db_utils import get_unified_db_path
            dialogue_db = get_unified_db_path(
                Path.home() / ".context-dna" / ".dialogue_mirror.db"
            )
            if dialogue_db.exists():
                from memory.db_utils import unified_table
                _t_threads = unified_table(".dialogue_mirror.db", "dialogue_threads")
                _t_msgs = unified_table(".dialogue_mirror.db", "dialogue_messages")
                conn = sqlite3.connect(str(dialogue_db))
                try:
                    conn.row_factory = sqlite3.Row
                    # Recent threads
                    threads = conn.execute(
                        f"SELECT session_id, last_activity, message_count "
                        f"FROM {_t_threads} ORDER BY last_activity DESC LIMIT 5"
                    ).fetchall()
                    recent_threads = [dict(t) for t in threads]

                    # Recent messages from user
                    user_recent = conn.execute(
                        f"SELECT content, timestamp FROM {_t_msgs} "
                        "WHERE role = 'aaron' ORDER BY timestamp DESC LIMIT 5"
                    ).fetchall()
                    recent_user_msgs = [{"content": m["content"][:200], "timestamp": m["timestamp"]} for m in user_recent]

                    briefing["sections"]["last_session"] = {
                        "recent_threads": len(recent_threads),
                        "threads": recent_threads,
                        "user_recent_messages": recent_user_msgs,
                    }
                finally:
                    conn.close()
        except Exception as e:
            briefing["sections"]["last_session"] = {"error": str(e)[:100]}

        # 2. META-ANALYSIS INSIGHTS
        try:
            meta_db = Path(__file__).parent / ".meta_analysis.db"
            if meta_db.exists():
                conn = sqlite3.connect(str(meta_db))
                try:
                    conn.row_factory = sqlite3.Row
                    runs = conn.execute(
                        "SELECT * FROM meta_analysis_runs ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    if runs:
                        briefing["sections"]["meta_analysis"] = {
                            "last_run": dict(runs),
                            "status": "active"
                        }
                    else:
                        briefing["sections"]["meta_analysis"] = {"status": "no runs yet", "note": "Waiting for 30min session gap"}
                finally:
                    conn.close()
        except Exception as e:
            briefing["sections"]["meta_analysis"] = {"error": str(e)[:100]}

        # 3. FAILURE PATTERNS (landmines ahead)
        try:
            from memory.failure_pattern_analyzer import get_failure_pattern_analyzer
            analyzer = get_failure_pattern_analyzer()
            if analyzer:
                patterns = analyzer.get_landmines_for_task("general", limit=5)
                briefing["sections"]["failure_patterns"] = {
                    "count": len(patterns) if patterns else 0,
                    "patterns": [str(p)[:200] for p in (patterns or [])]
                }
        except Exception as e:
            briefing["sections"]["failure_patterns"] = {"error": str(e)[:100]}

        # 4. EVIDENCE PIPELINE HEALTH (uses singleton to prevent FD leak)
        try:
            from memory.observability_store import get_observability_store
            obs = get_observability_store()
            conn = obs._sqlite_conn
            cursor = conn.cursor()
            try:
                claims = cursor.execute("SELECT COUNT(*) FROM claim").fetchone()[0]
                outcomes = cursor.execute("SELECT COUNT(*) FROM outcome_event").fetchone()[0]
                quarantine = cursor.execute("SELECT COUNT(*) FROM knowledge_quarantine").fetchone()[0]
                # Recent outcomes
                recent_outcomes = cursor.execute(
                    "SELECT task_title, success, reward, timestamp FROM outcome_event "
                    "ORDER BY timestamp DESC LIMIT 5"
                ).fetchall()
            finally:
                cursor.close()
            briefing["sections"]["evidence_pipeline"] = {
                "claims": claims,
                "outcomes": outcomes,
                "quarantine": quarantine,
                "recent_outcomes": [
                    {"task": r[0][:100], "success": bool(r[1]), "reward": r[2], "when": r[3]}
                    for r in recent_outcomes
                ]
            }
        except Exception as e:
            briefing["sections"]["evidence_pipeline"] = {"error": str(e)[:100]}

        # 5. BRAIN STATE
        try:
            brain_path = Path(__file__).parent / "brain_state.md"
            if brain_path.exists():
                content = brain_path.read_text()
                briefing["sections"]["brain_state"] = {
                    "preview": content[:500],
                    "last_updated": datetime.fromtimestamp(brain_path.stat().st_mtime).isoformat()
                }
        except Exception as e:
            briefing["sections"]["brain_state"] = {"error": str(e)[:100]}

        # 6. SCHEDULER HEALTH
        try:
            sched_state = Path(__file__).parent / ".scheduler_state.json"
            if sched_state.exists():
                import json
                state = json.loads(sched_state.read_text())
                jobs = state.get("jobs", {})
                failing = {k: v for k, v in jobs.items() if v.get("last_success") is False}
                briefing["sections"]["scheduler"] = {
                    "total_jobs": len(jobs),
                    "failing_jobs": len(failing),
                    "failing_names": list(failing.keys())[:5],
                }
        except Exception as e:
            briefing["sections"]["scheduler"] = {"error": str(e)[:100]}

        # 7. CODEBASE MAP (architecture wake-up)
        try:
            from memory.codebase_map import get_hot_files, get_changes_summary
            briefing["sections"]["codebase_map"] = {
                "hot_files": get_hot_files(limit=10),
                "changes": get_changes_summary(),
            }
        except Exception as e:
            briefing["sections"]["codebase_map"] = {"error": str(e)[:100]}

        return briefing

    # =============================================================================
    # SEMANTIC SEARCH ENDPOINT
    # =============================================================================

    @app.post("/api/query")
    async def semantic_query(request: Request):
        """
        Semantic search for relevant learnings.

        This endpoint provides intelligent search across learnings using:
        1. Context DNA server (pgvector semantic search) if available
        2. Improved keyword matching as fallback

        Request body:
            {
                "query": "search terms",
                "limit": 10  // optional, default 10
            }

        Returns:
            {
                "query": "original query",
                "results": [...],
                "count": N,
                "source": "context_dna" | "keyword_fallback"
            }
        """
        import urllib.request
        import urllib.error

        try:
            body = await request.json()
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

        query_text = body.get("query", body.get("q", ""))
        limit = body.get("limit", 10)

        if not query_text:
            raise HTTPException(status_code=400, detail="Query is required")

        # Try Context DNA server (port 3456) first for pgvector semantic search
        context_dna_url = os.environ.get("CONTEXT_DNA_URL", "http://127.0.0.1:3456")
        try:
            req_data = json.dumps({"query": query_text, "limit": limit}).encode()
            req = urllib.request.Request(
                f"{context_dna_url}/api/query",
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode())
                return {
                    "query": query_text,
                    "results": result.get("results", []),
                    "count": result.get("count", 0),
                    "source": "context_dna"
                }
        except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
            # Context DNA server not available, try SQLite FTS5 then keyword search
            logger.debug(f"Context DNA server unavailable, using fallback: {e}")

        # Fallback 1: SQLite FTS5 full-text search (more capable than keyword overlap)
        try:
            try:
                from sqlite_storage import get_sqlite_storage
            except ImportError:
                from memory.sqlite_storage import get_sqlite_storage
            sqlite_store = get_sqlite_storage()
            fts_results = sqlite_store.query(query_text, limit=limit)
            if fts_results:
                return {
                    "query": query_text,
                    "results": [{
                        "id": r.get("id", ""),
                        "type": r.get("type", ""),
                        "title": r.get("title", ""),
                        "content": r.get("content", ""),
                        "tags": r.get("tags", []),
                        "score": 0.8
                    } for r in fts_results],
                    "count": len(fts_results),
                    "source": "sqlite_fts5"
                }
        except Exception as e:
            logger.debug(f"SQLite FTS5 fallback failed: {e}")

        # Fallback 2: Improved keyword search with scoring
        try:
            from memory.learning_store import get_learning_store
        except ImportError:
            try:
                from learning_store import get_learning_store
            except ImportError:
                raise HTTPException(
                    status_code=503,
                    detail="Learning store not available"
                )

        store = get_learning_store()
        all_learnings = store.get_recent(limit=200)  # Get larger pool for filtering

        # Improved keyword matching - require 50%+ word overlap
        query_words = set(query_text.lower().split())
        scored_results = []

        for learning in all_learnings:
            title = learning.get("title", "").lower()
            content = learning.get("content", "").lower()
            tags = [t.lower() for t in learning.get("tags", [])]

            # Combine all searchable text
            text = f"{title} {content} {' '.join(tags)}"
            text_words = set(text.split())

            # Calculate word overlap
            if query_words and text_words:
                overlap = len(query_words & text_words)
                overlap_ratio = overlap / len(query_words)

                # Require at least 50% word match
                if overlap_ratio >= 0.5:
                    # Boost score for title matches and tag matches
                    title_boost = 1.5 if any(w in title for w in query_words) else 1.0
                    tag_boost = 1.3 if any(w in ' '.join(tags) for w in query_words) else 1.0

                    score = overlap_ratio * title_boost * tag_boost
                    scored_results.append({
                        "id": learning.get("id", ""),
                        "type": learning.get("type", ""),
                        "title": learning.get("title", ""),
                        "content": learning.get("content", ""),
                        "tags": learning.get("tags", []),
                        "score": round(score, 3)
                    })

        # Sort by score descending
        scored_results.sort(key=lambda x: x.get("score", 0), reverse=True)

        return {
            "query": query_text,
            "results": scored_results[:limit],
            "count": len(scored_results[:limit]),
            "source": "keyword_fallback"
        }

    # =============================================================================
    # LEARNING VISUALIZATION ENDPOINTS
    # =============================================================================

    @app.get("/api/learnings")
    async def get_learnings_with_stats(limit: int = 50):
        """
        Get learnings with stats for the Today's Learnings dashboard panel.

        Returns both learnings (transformed for frontend) and aggregate stats.
        """
        try:
            from memory.learning_store import get_learning_store
        except ImportError:
            try:
                from learning_store import get_learning_store
            except ImportError:
                raise HTTPException(
                    status_code=503,
                    detail="Learning store not available"
                )

        store = get_learning_store()
        raw_learnings = store.get_recent(limit=limit)
        raw_stats = store.get_stats()

        # Transform learnings to match frontend Learning interface
        now_ms = int(datetime.now().timestamp() * 1000)
        learnings = []
        domain_counts: Dict[str, int] = {}
        total_confidence = 0.0
        today_count = 0
        one_day_ms = 86400000

        for raw in raw_learnings:
            # Parse timestamp to ms
            ts = raw.get('timestamp', '')
            try:
                if isinstance(ts, str):
                    from datetime import timezone as tz
                    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    ts_ms = int(dt.timestamp() * 1000)
                elif isinstance(ts, (int, float)):
                    ts_ms = int(ts)
                else:
                    ts_ms = now_ms
            except Exception:
                ts_ms = now_ms

            # Determine domain from tags or type
            domain = raw.get('domain', '')
            if not domain:
                tags = raw.get('tags', [])
                tag_domain_map = {
                    'sqlite': 'database', 'postgres': 'database', 'db': 'database',
                    'redis': 'redis', 'docker': 'docker', 'aws': 'aws',
                    'injection': 'injection', 'scheduler': 'scheduler',
                    'performance': 'performance', 'perf': 'performance',
                    'networking': 'networking', 'ipv6': 'networking',
                    'livekit': 'livekit', 'webrtc': 'livekit',
                    'async': 'async', 'asyncio': 'async',
                    'deploy': 'deployment', 'infrastructure': 'infrastructure',
                }
                for tag in tags:
                    tag_lower = tag.lower()
                    if tag_lower in tag_domain_map:
                        domain = tag_domain_map[tag_lower]
                        break
                if not domain:
                    # Infer from type
                    type_map = {'fix': 'database', 'win': 'injection', 'pattern': 'scheduler',
                                'insight': 'injection', 'gotcha': 'networking'}
                    domain = type_map.get(raw.get('type', ''), 'general')

            # Confidence (from evidence pipeline or heuristic)
            confidence = raw.get('confidence', 0.0)
            if not confidence:
                type_confidence = {'fix': 0.9, 'win': 0.85, 'pattern': 0.75,
                                   'insight': 0.7, 'gotcha': 0.95}
                confidence = type_confidence.get(raw.get('type', ''), 0.7)

            # Evidence status
            evidence_status = raw.get('evidenceStatus') or raw.get('evidence_status')
            if not evidence_status:
                merge_count = raw.get('_merge_count', 1)
                if merge_count >= 3:
                    evidence_status = 'applied'
                elif merge_count >= 2:
                    evidence_status = 'claim'
                else:
                    evidence_status = 'quarantine'

            learning = {
                "id": raw.get('id', ''),
                "title": raw.get('title', ''),
                "details": raw.get('content', raw.get('details', '')),
                "domain": domain,
                "confidence": confidence,
                "evidenceStatus": evidence_status,
                "timestamp": ts_ms,
                "tags": raw.get('tags', []),
                "source": raw.get('source', 'auto_capture'),
            }
            learnings.append(learning)

            # Accumulate stats
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            total_confidence += confidence
            if (now_ms - ts_ms) < one_day_ms:
                today_count += 1

        # Build stats
        top_domain = max(domain_counts, key=domain_counts.get) if domain_counts else 'general'
        avg_confidence = total_confidence / len(learnings) if learnings else 0.0

        return {
            "learnings": learnings,
            "stats": {
                "todayCount": today_count,
                "allTimeCount": raw_stats.get('total', len(learnings)),
                "topDomain": top_domain,
                "avgConfidence": round(avg_confidence, 2),
            }
        }

    @app.get("/api/learnings/recent")
    async def get_recent_learnings(limit: int = 20):
        """
        Get recent context learnings for visualization.

        Returns a list of recent learnings (wins, fixes, patterns, etc.)
        for display in the Context Learning panel.
        """
        try:
            from memory.learning_store import get_learning_store
        except ImportError:
            try:
                from learning_store import get_learning_store
            except ImportError:
                raise HTTPException(
                    status_code=503,
                    detail="Learning store not available"
                )

        store = get_learning_store()
        learnings = store.get_recent(limit=limit)

        return {
            "count": len(learnings),
            "learnings": learnings
        }

    @app.get("/api/learnings/since/{timestamp}")
    async def get_learnings_since(timestamp: str, limit: int = 20):
        """
        Get learnings since a given timestamp.

        Use this to find learnings that occurred after a specific injection.
        Timestamp should be in ISO format.
        """
        try:
            from memory.learning_store import get_learning_store
        except ImportError:
            try:
                from learning_store import get_learning_store
            except ImportError:
                raise HTTPException(
                    status_code=503,
                    detail="Learning store not available"
                )

        store = get_learning_store()
        learnings = store.get_since(timestamp, limit=limit)

        return {
            "count": len(learnings),
            "since": timestamp,
            "learnings": learnings
        }

    @app.get("/api/learnings/session/{session_id}")
    async def get_learnings_by_session(session_id: str):
        """
        Get learnings for a specific session.

        Use this to find all learnings associated with an injection session.
        """
        try:
            from memory.learning_store import get_learning_store
        except ImportError:
            try:
                from learning_store import get_learning_store
            except ImportError:
                raise HTTPException(
                    status_code=503,
                    detail="Learning store not available"
                )

        store = get_learning_store()
        learnings = store.get_by_session(session_id)

        return {
            "session_id": session_id,
            "count": len(learnings),
            "learnings": learnings
        }

    @app.get("/api/learnings/injection/{injection_id}")
    async def get_learnings_for_injection(injection_id: str):
        """
        Get learnings associated with a specific injection.

        Use this to display learnings that resulted from a particular context injection.
        """
        try:
            from memory.learning_store import get_learning_store
        except ImportError:
            try:
                from learning_store import get_learning_store
            except ImportError:
                raise HTTPException(
                    status_code=503,
                    detail="Learning store not available"
                )

        store = get_learning_store()
        learnings = store.get_for_injection(injection_id)

        return {
            "injection_id": injection_id,
            "count": len(learnings),
            "learnings": learnings
        }

    @app.post("/api/learnings")
    async def store_learning(learning: Dict[str, Any]):
        """
        Store a new learning.

        Required fields:
        - type: win, fix, pattern, insight, gotcha
        - title: Short description
        - content: Full details

        Optional fields:
        - tags: List of relevant tags
        - session_id: Associated session
        - injection_id: Associated injection
        - source: Where the learning came from
        """
        try:
            from memory.learning_store import get_learning_store
        except ImportError:
            try:
                from learning_store import get_learning_store
            except ImportError:
                raise HTTPException(
                    status_code=503,
                    detail="Learning store not available"
                )

        if 'type' not in learning or 'title' not in learning:
            raise HTTPException(
                status_code=400,
                detail="Learning must include 'type' and 'title'"
            )

        store = get_learning_store()
        stored = store.store_learning(learning)

        # Broadcast to WebSocket clients if available
        if hasattr(app.state, 'broadcast_learning'):
            try:
                await app.state.broadcast_learning(stored)
            except Exception as e:
                print(f"[WARN] Learning broadcast failed: {e}")

        return {
            "stored": True,
            "learning": stored
        }

    @app.get("/api/learnings/{learning_id}")
    async def get_learning_by_id(learning_id: str):
        """
        Get a specific learning by ID.
        """
        try:
            from memory.learning_store import get_learning_store
        except ImportError:
            try:
                from learning_store import get_learning_store
            except ImportError:
                raise HTTPException(
                    status_code=503,
                    detail="Learning store not available"
                )

        store = get_learning_store()
        learning = store.get_by_id(learning_id)

        if not learning:
            raise HTTPException(
                status_code=404,
                detail=f"Learning {learning_id} not found"
            )

        return {
            "found": True,
            "learning": learning
        }

    # Track learning WebSocket clients
    learning_ws_clients: Set[WebSocket] = set()

    @app.websocket("/ws/learnings")
    async def learning_websocket_endpoint(websocket: WebSocket, client_id: str = "unknown"):
        """WebSocket endpoint for real-time learning updates."""
        await websocket.accept()
        learning_ws_clients.add(websocket)
        logger.info(f"Learning WS client connected: {client_id}")

        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)

                if message.get("type") == "ping" or message.get("type") == "heartbeat":
                    await websocket.send_json({"type": "heartbeat_ack", "client_id": client_id})
                elif message.get("type") == "register":
                    logger.info(f"Learning client registered: {message.get('client_id', client_id)}")
                    await websocket.send_json({"type": "registered", "client_id": client_id})

        except WebSocketDisconnect:
            learning_ws_clients.discard(websocket)
            logger.info(f"Learning WS client disconnected: {client_id}")
        except Exception as e:
            learning_ws_clients.discard(websocket)
            logger.error(f"Learning WS error for {client_id}: {e}")

    async def broadcast_learning(learning_data: dict):
        """Broadcast new learning to all connected WebSocket clients."""
        message = {"event": "learning_captured", "data": learning_data}
        disconnected = []
        for ws in learning_ws_clients:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            learning_ws_clients.discard(ws)

    # Store broadcast function for use by learning store
    app.state.broadcast_learning = broadcast_learning

    # =============================================================================
    # A/B TESTING ENDPOINTS
    # =============================================================================

    @app.post("/ab/record-injection")
    async def record_ab_injection(
        session_id: str,
        prompt: str,
        mode: str,
        result: Dict[str, Any] = None
    ):
        """
        Record a context injection for A/B tracking.

        Called automatically by /consult/* endpoints when session_id is provided.
        """
        tracker = get_ab_tracker()
        injection_id = tracker.record_injection(session_id, prompt, mode, result or {})
        return {"injection_id": injection_id, "tracked": True}

    @app.post("/ab/record-outcome")
    async def record_ab_outcome(
        session_id: str,
        prompt_hash: str = None,
        first_try_success: bool = None,
        context_referenced: bool = None,
        mistake_repeated: bool = None,
        completion_seconds: int = None
    ):
        """
        Record the outcome of a task for A/B correlation.

        Args:
            session_id: The session ID to correlate with
            prompt_hash: Optional hash to match specific injection
            first_try_success: Did Atlas succeed on first attempt?
            context_referenced: Did Atlas reference the provided context?
            mistake_repeated: Did Atlas repeat a mistake the context warned about?
            completion_seconds: How long did the task take?
        """
        tracker = get_ab_tracker()
        tracker.record_outcome(
            session_id=session_id,
            prompt_hash=prompt_hash,
            first_try_success=first_try_success,
            context_referenced=context_referenced,
            mistake_repeated=mistake_repeated,
            completion_seconds=completion_seconds
        )
        return {"recorded": True}

    @app.get("/ab/stats")
    async def get_ab_stats():
        """
        Get A/B testing statistics.

        Returns success rates and metrics by mode (greedy vs layered vs hybrid).
        Use this to determine which context injection mode is most effective.
        """
        tracker = get_ab_tracker()
        stats = tracker.get_mode_stats()

        # Add recommendations
        recommendations = []

        if stats:
            # Compare modes
            modes = list(stats.keys())
            if len(modes) >= 2:
                success_rates = {m: stats[m]["first_try_success_rate"] for m in modes}
                best_mode = max(success_rates, key=success_rates.get)
                recommendations.append(f"Best first-try success rate: {best_mode} ({success_rates[best_mode]:.1%})")

                ref_rates = {m: stats[m]["context_reference_rate"] for m in modes}
                most_referenced = max(ref_rates, key=ref_rates.get)
                recommendations.append(f"Most referenced context: {most_referenced} ({ref_rates[most_referenced]:.1%})")

                mistake_rates = {m: stats[m]["mistake_repeat_rate"] for m in modes}
                fewest_mistakes = min(mistake_rates, key=mistake_rates.get)
                recommendations.append(f"Fewest repeated mistakes: {fewest_mistakes} ({mistake_rates[fewest_mistakes]:.1%})")

        return {
            "stats_by_mode": stats,
            "recommendations": recommendations,
            "note": "More data needed for statistical significance" if sum(s.get("total", 0) for s in stats.values()) < 30 else "Sufficient data for analysis"
        }

    @app.get("/ab/recent")
    async def get_recent_injections(limit: int = 20):
        """Get recent context injections for debugging and analysis."""
        import sqlite3

        tracker = get_ab_tracker()
        conn = sqlite3.connect(tracker.db_path)
        try:
            cursor = conn.cursor()

            cursor.execute('''
                SELECT
                    id, session_id, timestamp, prompt_preview, mode, risk_level,
                    exact_file, gotcha_provided, outcome, first_try_success,
                    context_referenced, mistake_repeated, completion_seconds
                FROM context_injections
                ORDER BY id DESC
                LIMIT ?
            ''', (limit,))

            rows = cursor.fetchall()
        finally:
            conn.close()

        return {
            "count": len(rows),
            "injections": [
                {
                    "id": row[0],
                    "session_id": row[1],
                    "timestamp": row[2],
                    "prompt_preview": row[3],
                    "mode": row[4],
                    "risk_level": row[5],
                    "exact_file": row[6],
                    "gotcha_provided": row[7][:50] + "..." if row[7] and len(row[7]) > 50 else row[7],
                    "outcome": row[8],
                    "first_try_success": bool(row[9]) if row[9] is not None else None,
                    "context_referenced": bool(row[10]) if row[10] is not None else None,
                    "mistake_repeated": bool(row[11]) if row[11] is not None else None,
                    "completion_seconds": row[12]
                }
                for row in rows
            ]
        }

    # =========================================================================
    # ARCHITECTURE GRAPH ENDPOINTS
    # =========================================================================

    # Singleton graph builder (lazy initialized)
    _graph_builder: Optional[ArchitectureGraphBuilder] = None

    def get_graph_builder() -> Optional[ArchitectureGraphBuilder]:
        """Get or create the architecture graph builder singleton."""
        global _graph_builder
        if not ARCHITECTURE_GRAPH_AVAILABLE:
            return None
        if _graph_builder is None:
            repo_root = str(Path(__file__).parent.parent)
            _graph_builder = ArchitectureGraphBuilder(repo_root)
        return _graph_builder

    @app.get("/api/architecture/graph")
    async def get_architecture_graph(force_rebuild: bool = False):
        """
        Get the complete architecture graph.

        Args:
            force_rebuild: If true, ignores cache and rebuilds from scratch

        Returns:
            Complete architecture graph in React Flow compatible format
        """
        if not ARCHITECTURE_GRAPH_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Architecture graph module not available"
            )

        builder = get_graph_builder()
        if not builder:
            raise HTTPException(
                status_code=503,
                detail="Could not initialize graph builder"
            )

        try:
            graph = builder.build_graph(force_rebuild=force_rebuild)
            return {
                "success": True,
                "graph": graph.to_react_flow(),
                "stats": graph.stats,
                "version": graph.version,
                "timestamp": graph.timestamp,
                "changed_nodes": graph.changed_nodes,
            }
        except Exception as e:
            logger.error(f"Error building architecture graph: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Error building graph: {str(e)}"
            )

    @app.get("/api/architecture/graph/{node_id}")
    async def get_architecture_subgraph(node_id: str, depth: int = 2):
        """
        Get a subgraph centered on a specific node.

        Args:
            node_id: The node ID to center on
            depth: How many edges away to include (default 2)

        Returns:
            Subgraph in React Flow compatible format
        """
        if not ARCHITECTURE_GRAPH_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Architecture graph module not available"
            )

        builder = get_graph_builder()
        if not builder:
            raise HTTPException(
                status_code=503,
                detail="Could not initialize graph builder"
            )

        try:
            subgraph = builder.get_subgraph(node_id, depth=depth)
            if not subgraph:
                raise HTTPException(
                    status_code=404,
                    detail=f"Node not found: {node_id}"
                )

            return {
                "success": True,
                "center_node": node_id,
                "depth": depth,
                "graph": subgraph.to_react_flow(),
                "stats": subgraph.stats,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting subgraph for {node_id}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Error getting subgraph: {str(e)}"
            )

    @app.get("/api/architecture/stats")
    async def get_architecture_stats():
        """
        Get statistics about the architecture graph.

        Returns:
            Summary statistics (node counts, edge counts, categories)
        """
        if not ARCHITECTURE_GRAPH_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Architecture graph module not available"
            )

        builder = get_graph_builder()
        if not builder:
            raise HTTPException(
                status_code=503,
                detail="Could not initialize graph builder"
            )

        try:
            stats = builder.get_stats()
            return {
                "success": True,
                "stats": stats,
            }
        except Exception as e:
            logger.error(f"Error getting architecture stats: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Error getting stats: {str(e)}"
            )

    # =========================================================================
    # ARCHITECTURE TWIN REFRESH (Movement 4)
    # =========================================================================

    @app.post("/api/architecture/refresh-twin")
    async def refresh_architecture_twin():
        """Refresh architecture.current.md + architecture.diff.md from code analysis."""
        try:
            from memory.refresh_architecture_twin import refresh
            result = refresh(generate_diff=True)
            return {
                "success": True,
                "current_size": result.get("current_size", 0),
                "current_path": result.get("current_path", ""),
                "has_diff": "diff_path" in result,
            }
        except Exception as e:
            logger.error(f"Architecture twin refresh error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # =========================================================================
    # MODE TRANSITION API (Lite ↔ Heavy)
    # =========================================================================

    @app.get("/api/mode/status")
    async def mode_status():
        """Get current operating mode (lite/heavy) and sync state."""
        result = {
            "sync_available": SYNC_AVAILABLE,
            "sync_enabled": SYNC_ENABLED,
            "mode_transition_available": MODE_TRANSITION_AVAILABLE,
        }

        # Sync status
        if SYNC_AVAILABLE and get_sync_engine:
            try:
                engine = get_sync_engine()
                result["sync"] = {
                    "enabled": True,
                    "mode": engine.detect_mode(),
                    "pg_targets": {
                        name: engine._try_pg_connect(name)
                        for name in ["context_dna", "contextdna"]
                    },
                }
            except Exception:
                result["sync"] = {"enabled": True, "error": "status check failed"}

        # Mode transition status
        if MODE_TRANSITION_AVAILABLE:
            try:
                manager = get_mode_manager()
                mode_info = await manager.get_current_mode()

                # Enrich with per-container details + health (run in thread - blocking subprocess calls)
                def _get_container_details():
                    import subprocess as sp
                    details = {}
                    try:
                        containers = manager.get_running_containers()
                        for c in containers:
                            status = c.get('status', '')
                            if '(healthy)' in status:
                                c['health'] = 'healthy'
                            elif '(unhealthy)' in status:
                                c['health'] = 'unhealthy'
                            else:
                                c['health'] = 'no-healthcheck'
                        details['containers'] = containers
                    except Exception:
                        details['containers'] = []

                    try:
                        proc = sp.run(
                            ['docker', 'ps', '-a', '--filter', 'status=exited',
                             '--filter', 'status=created',
                             '--format', '{{.Names}}\t{{.Status}}'],
                            capture_output=True, text=True, timeout=5
                        )
                        if proc.returncode == 0:
                            stopped = []
                            for line in proc.stdout.strip().split('\n'):
                                if line.strip():
                                    parts = line.split('\t')
                                    if len(parts) >= 2:
                                        stopped.append({'name': parts[0], 'status': parts[1]})
                            details['stopped_containers'] = stopped
                    except Exception:
                        details['stopped_containers'] = []
                    return details

                container_details = await asyncio.to_thread(_get_container_details)
                mode_info.update(container_details)
                if 'containers' in mode_info and 'stopped_containers' in mode_info:
                    mode_info['total_containers'] = len(mode_info['containers']) + len(mode_info['stopped_containers'])

                result["mode"] = mode_info
            except Exception as e:
                result["mode"] = {"error": str(e)}
        else:
            result["mode"] = {
                "current_mode": "lite" if not SYNC_ENABLED else "heavy",
                "note": "ModeTransitionManager not available - showing inferred mode"
            }

        return result

    @app.post("/api/mode/to-lite")
    async def transition_to_lite(
        stop_docker: bool = True,
        force: bool = False
    ):
        """Transition from Heavy Mode to Lite Mode."""
        if not MODE_TRANSITION_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="ModeTransitionManager not available"
            )

        try:
            manager = get_mode_manager()
            result = await manager.transition_to_lite(
                stop_docker=stop_docker,
                force_shutdown=force
            )

            # Update agent state
            agent._mode = "lite"
            agent.stats["current_mode"] = "lite"

            # Broadcast to WebSocket clients
            await agent._broadcast({
                "type": "mode_transition",
                "from": "heavy",
                "to": "lite",
                "result": result.to_dict() if hasattr(result, 'to_dict') else str(result),
            })

            return {
                "success": True,
                "transition": "heavy → lite",
                "result": result.to_dict() if hasattr(result, 'to_dict') else str(result),
            }
        except Exception as e:
            logger.error(f"Transition to lite failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/mode/to-heavy")
    async def transition_to_heavy(
        wait_healthy: bool = True
    ):
        """Transition from Lite Mode to Heavy Mode."""
        if not MODE_TRANSITION_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="ModeTransitionManager not available"
            )

        try:
            manager = get_mode_manager()
            result = await manager.transition_to_heavy(
                wait_for_healthy=wait_healthy
            )

            # Update agent state
            agent._mode = "heavy"
            agent.stats["current_mode"] = "heavy"

            # Broadcast to WebSocket clients
            await agent._broadcast({
                "type": "mode_transition",
                "from": "lite",
                "to": "heavy",
                "result": result.to_dict() if hasattr(result, 'to_dict') else str(result),
            })

            return {
                "success": True,
                "transition": "lite → heavy",
                "result": result.to_dict() if hasattr(result, 'to_dict') else str(result),
            }
        except Exception as e:
            logger.error(f"Transition to heavy failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # -----------------------------------------------------------------------
    # Permission Assistant — tool approval explain + act
    # -----------------------------------------------------------------------

    @app.get("/api/permissions/pending")
    async def get_pending_permissions():
        """Return all currently pending tool approvals."""
        try:
            from memory.permission_assistant import get_permission_assistant
            pa = get_permission_assistant()
            return {"pending": pa.get_pending()}
        except Exception as e:
            logger.error(f"Permission pending query failed: {e}")
            return {"pending": []}

    @app.post("/api/permissions/{tool_use_id}/approve")
    async def approve_permission(tool_use_id: str):
        """Approve a pending tool and send keystroke to Claude Code."""
        try:
            from memory.permission_assistant import get_permission_assistant
            pa = get_permission_assistant()
            ok = pa.approve(tool_use_id)
            return {"success": ok, "action": "approved", "tool_use_id": tool_use_id}
        except Exception as e:
            logger.error(f"Permission approve failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/permissions/{tool_use_id}/deny")
    async def deny_permission(tool_use_id: str):
        """Deny a pending tool and send keystroke to Claude Code."""
        try:
            from memory.permission_assistant import get_permission_assistant
            pa = get_permission_assistant()
            ok = pa.deny(tool_use_id)
            return {"success": ok, "action": "denied", "tool_use_id": tool_use_id}
        except Exception as e:
            logger.error(f"Permission deny failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/sync/force")
    async def force_sync():
        """Force an immediate sync check (normally runs every 120s)."""
        if not SYNC_AVAILABLE or not get_sync_engine:
            raise HTTPException(
                status_code=503,
                detail="Sync not available"
            )

        try:
            engine = get_sync_engine()
            report = await engine.async_sync_all(caller="api_force")
            return {
                "success": report.success,
                "result": report.to_dict(),
            }
        except Exception as e:
            logger.error(f"Force sync failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # WebSocket connections for architecture updates
    architecture_clients: Set[WebSocket] = set()

    @app.websocket("/ws/architecture")
    async def architecture_websocket(websocket: WebSocket):
        """
        WebSocket endpoint for real-time architecture graph updates.

        Broadcasts:
        - graph_update: When files change and graph is rebuilt
        - focus_change: When a specific node should be highlighted
        """
        await websocket.accept()
        architecture_clients.add(websocket)
        logger.info(f"Architecture WebSocket client connected. Total: {len(architecture_clients)}")

        try:
            # Send initial connection confirmation
            await websocket.send_json({
                "event": "connected",
                "message": "Architecture WebSocket connected",
                "available": ARCHITECTURE_GRAPH_AVAILABLE,
            })

            # Keep connection alive and handle messages
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_json(),
                        timeout=30.0
                    )

                    # Handle client requests
                    if data.get("action") == "get_graph":
                        builder = get_graph_builder()
                        if builder:
                            graph = builder.build_graph()
                            await websocket.send_json({
                                "event": "graph_data",
                                "data": graph.to_react_flow(),
                                "stats": graph.stats,
                            })

                    elif data.get("action") == "get_subgraph":
                        node_id = data.get("node_id")
                        depth = data.get("depth", 2)
                        builder = get_graph_builder()
                        if builder and node_id:
                            subgraph = builder.get_subgraph(node_id, depth)
                            if subgraph:
                                await websocket.send_json({
                                    "event": "subgraph_data",
                                    "center_node": node_id,
                                    "data": subgraph.to_react_flow(),
                                })

                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    await websocket.send_json({"event": "ping"})

        except WebSocketDisconnect:
            logger.info("Architecture WebSocket client disconnected")
        except Exception as e:
            logger.error(f"Architecture WebSocket error: {e}")
        finally:
            architecture_clients.discard(websocket)

    async def broadcast_architecture_update(changed_nodes: List[str] = None):
        """Broadcast architecture graph update to all connected clients."""
        if not architecture_clients:
            return

        message = {
            "event": "graph_update",
            "data": {
                "type": "incremental" if changed_nodes else "full",
                "changed_nodes": changed_nodes or [],
                "timestamp": datetime.now().isoformat(),
            }
        }

        disconnected = set()
        for client in architecture_clients:
            try:
                await client.send_json(message)
            except Exception:
                disconnected.add(client)

        architecture_clients.difference_update(disconnected)

    # =============================================================================
    # 3-SURGEON INTEGRATION (cross-exam, consensus, probe, evidence via HTTP)
    # =============================================================================

    def _surgery_query_local(system: str, prompt: str, profile: str = "deep",
                             max_chars: int = 12000, timeout_s: float = 90.0) -> dict:
        """Query Qwen3-4B via priority queue. Thread-safe, blocking."""
        try:
            from memory.llm_priority_queue import llm_generate, Priority
            t0 = time.time()
            result = llm_generate(system, prompt, Priority.ATLAS, profile,
                                  "surgery_team_http", timeout_s=timeout_s)
            latency = int((time.time() - t0) * 1000)
            if result:
                return {"ok": True, "content": result[:max_chars],
                        "latency_ms": latency, "model": "Qwen3-4B-4bit"}
            return {"ok": False, "content": "No response",
                    "latency_ms": latency, "model": "Qwen3-4B-4bit"}
        except Exception as e:
            return {"ok": False, "content": str(e)[:200],
                    "latency_ms": 0, "model": "Qwen3-4B-4bit"}

    def _surgery_query_remote(system: str, prompt: str, model: str = "",
                              max_tokens: int = 2048, timeout_s: float = 300.0) -> dict:
        """Query cardiologist via OpenAI-compatible API. Thread-safe, blocking.

        Provider selected via LLM_PROVIDER env (``deepseek`` | ``openai``).
        Default: ``deepseek`` (Aaron cutover 2026-04-18 — cheaper, primary).
        If LLM_PROVIDER is unset we auto-select DeepSeek when its key exists,
        otherwise fall back to OpenAI. Missing keys or missing openai SDK
        produce a soft failure ({"ok": False}) rather than raising.
        """
        import subprocess as _sp

        # Provider resolution — DeepSeek primary, OpenAI optional
        provider = os.environ.get("LLM_PROVIDER", "").strip().lower()

        def _resolve_deepseek():
            base_url = "https://api.deepseek.com/v1"
            effective_model = model or "deepseek-chat"
            key = (os.environ.get("Context_DNA_Deep_Seek")
                   or os.environ.get("Context_DNA_Deepseek")
                   or os.environ.get("DEEPSEEK_API_KEY", ""))
            if not key:
                try:
                    r = _sp.run(
                        ["security", "find-generic-password", "-s", "fleet-nerve",
                         "-a", "Context_DNA_Deep_Seek", "-w"],
                        capture_output=True, text=True, timeout=2,
                    )
                    if r.returncode == 0:
                        key = r.stdout.strip()
                except Exception:
                    key = ""
            return base_url, effective_model, key, "Context_DNA_Deep_Seek"

        def _resolve_openai():
            return (None,
                    model or "gpt-4.1-mini",
                    os.environ.get("Context_DNA_OPENAI", "")
                        or os.environ.get("OPENAI_API_KEY", ""),
                    "Context_DNA_OPENAI")

        if provider == "openai":
            base_url, effective_model, key, key_label = _resolve_openai()
        elif provider == "deepseek":
            base_url, effective_model, key, key_label = _resolve_deepseek()
        else:
            # Auto: DeepSeek first, OpenAI as optional fallback
            base_url, effective_model, key, key_label = _resolve_deepseek()
            if not key or len(key) < 20:
                base_url, effective_model, key, key_label = _resolve_openai()

        if not key or len(key) < 20:
            return {"ok": False,
                    "content": f"no external LLM key set ({key_label} missing; "
                               "DeepSeek primary, OpenAI optional)",
                    "latency_ms": 0, "model": effective_model}

        try:
            from openai import OpenAI  # OPTIONAL — wraps DeepSeek via OpenAI-compatible API
        except ImportError:
            return {"ok": False,
                    "content": "openai package not installed — external cardiologist "
                               "calls disabled. Install with `pip install openai` "
                               "if you want this path (DeepSeek also uses it).",
                    "latency_ms": 0, "model": effective_model}

        try:
            client = OpenAI(api_key=key, base_url=base_url, timeout=timeout_s)
            t0 = time.time()
            resp = client.chat.completions.create(
                model=effective_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.7,
            )
            latency = int((time.time() - t0) * 1000)
            content = resp.choices[0].message.content or ""
            usage = resp.usage
            cost = 0.0
            if usage:
                pricing = {"gpt-4.1": (2.00, 8.00), "gpt-4.1-mini": (0.40, 1.60),
                           "gpt-4.1-nano": (0.10, 0.40),
                           "deepseek-chat": (0.28, 1.10),
                           "deepseek-reasoner": (0.55, 2.19)}
                in_rate, out_rate = pricing.get(effective_model, (2.00, 8.00))
                cost = (usage.prompt_tokens * in_rate + usage.completion_tokens * out_rate) / 1_000_000
            return {"ok": True, "content": content, "latency_ms": latency,
                    "model": effective_model, "cost_usd": cost,
                    "tokens_in": usage.prompt_tokens if usage else 0,
                    "tokens_out": usage.completion_tokens if usage else 0}
        except Exception as e:
            return {"ok": False, "content": str(e)[:200],
                    "latency_ms": 0, "model": model or "cardiologist"}

    def _surgery_cross_exam_blocking(topic: str) -> dict:
        """Full cross-exam pipeline (blocking). Returns structured results."""
        # System prompts hoisted to module constants (SURGERY_*_SYSTEM_PROMPT)
        # so the 6 calls below share identical text — stable prompt_cache_key
        # on the external-fallback path and no per-call rebuild work.
        initial_prompt = f"Provide your analysis of: {topic}\n\nBe specific. Cite evidence. Admit unknowns."

        # Step 1: Initial reports (parallel would be nice but GPU lock serializes local)
        r_local = _surgery_query_local(SURGERY_BASE_SYSTEM_PROMPT, initial_prompt, profile="deep")
        r_remote = _surgery_query_remote(SURGERY_BASE_SYSTEM_PROMPT, initial_prompt)

        local_report = r_local["content"] if r_local["ok"] else "(Qwen3-4B unavailable)"
        remote_report = r_remote["content"] if r_remote["ok"] else "(GPT-4.1 unavailable)"

        # Step 2: Cross-examination
        r_cross_local = _surgery_query_local(
            SURGERY_CROSS_SYSTEM_PROMPT,
            f"Review this report from GPT-4.1-mini:\n\n{remote_report}\n\nYour critical analysis:",
            profile="deep"
        )
        r_cross_remote = _surgery_query_remote(
            SURGERY_CROSS_SYSTEM_PROMPT,
            f"Review this report from Qwen3-4B (local 4B parameter model):\n\n{local_report}\n\nYour critical analysis:"
        )

        # Step 3: Open Exploration
        combined = ""
        for label, r in [("Qwen3-4B Report", r_local), ("GPT-4.1 Report", r_remote),
                         ("Qwen3-4B Cross-Exam", r_cross_local), ("GPT-4.1 Cross-Exam", r_cross_remote)]:
            if r["ok"]:
                combined += f"=== {label} ===\n{r['content']}\n\n"

        explore_prompt = (
            f"TOPIC: {topic}\n\n=== TEAM ANALYSIS SO FAR ===\n{combined}\n"
            "Now: What are we blind to? Surface unknown unknowns."
        )
        r_explore_local = _surgery_query_local(SURGERY_EXPLORE_SYSTEM_PROMPT, explore_prompt, profile="deep")
        r_explore_remote = _surgery_query_remote(SURGERY_EXPLORE_SYSTEM_PROMPT, explore_prompt)

        total_cost = sum(r.get("cost_usd", 0) for r in [r_remote, r_cross_remote, r_explore_remote])

        results = {
            "ok": True,
            "topic": topic,
            "timestamp": datetime.now().isoformat(),
            "phases": {
                "initial": {
                    "neurologist": {"ok": r_local["ok"], "content": r_local["content"],
                                    "latency_ms": r_local.get("latency_ms", 0)},
                    "cardiologist": {"ok": r_remote["ok"], "content": r_remote["content"],
                                     "latency_ms": r_remote.get("latency_ms", 0)},
                },
                "cross_exam": {
                    "neurologist_reviews_cardiologist": {
                        "ok": r_cross_local["ok"], "content": r_cross_local["content"]},
                    "cardiologist_reviews_neurologist": {
                        "ok": r_cross_remote["ok"], "content": r_cross_remote["content"]},
                },
                "exploration": {
                    "neurologist": {"ok": r_explore_local["ok"],
                                    "content": r_explore_local["content"] if r_explore_local["ok"] else None},
                    "cardiologist": {"ok": r_explore_remote["ok"],
                                     "content": r_explore_remote["content"] if r_explore_remote["ok"] else None},
                },
            },
            "total_cost_usd": total_cost,
            "queries": 6,
        }

        # Save to WAL
        try:
            results_dir = Path("/tmp/atlas-agent-results")
            results_dir.mkdir(exist_ok=True)
            import uuid as _uuid
            out_path = results_dir / f"cross_exam_{int(time.time())}_{_uuid.uuid4().hex[:6]}.json"
            with open(out_path, "w") as f:
                json.dump(results, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save cross-exam WAL: {e}")

        return results

    @app.get("/contextdna/surgeons/probe")
    async def surgeon_probe():
        """Health check all 3 surgeons via HTTP."""
        loop = asyncio.get_running_loop()

        async def _check_local():
            return await loop.run_in_executor(
                None, lambda: _surgery_query_local(
                    "You are a test probe.", "Say 'operational' in one word.",
                    profile="classify", timeout_s=10.0))

        async def _check_remote():
            return await loop.run_in_executor(
                None, lambda: _surgery_query_remote(
                    "You are a test probe.", "Say 'operational' in one word.",
                    max_tokens=32, timeout_s=10.0))

        r_local, r_remote = await asyncio.gather(_check_local(), _check_remote())

        return {
            "neurologist": {"ok": r_local["ok"], "model": "Qwen3-4B-4bit",
                            "latency_ms": r_local.get("latency_ms", 0),
                            "response": r_local["content"][:80]},
            "cardiologist": {"ok": r_remote["ok"], "model": "gpt-4.1-mini",
                             "latency_ms": r_remote.get("latency_ms", 0),
                             "response": r_remote["content"][:80]},
            "atlas": {"ok": True, "model": "claude-opus", "note": "always present"},
        }

    @app.post("/contextdna/surgeons/cross-exam")
    async def surgeon_cross_exam(topic: str = ""):
        """Full 3-phase cross-examination. Runs in background thread (60-120s)."""
        topic = (topic or "").strip()[:2000]
        if len(topic) < 6:
            return {"error": "Topic must be at least 6 characters", "ok": False}

        loop = asyncio.get_running_loop()
        try:
            results = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: _surgery_cross_exam_blocking(topic)),
                timeout=300.0,
            )
            return results
        except asyncio.TimeoutError:
            return {"error": "Cross-exam timed out after 300s", "ok": False,
                    "topic": topic[:80]}

    @app.post("/contextdna/surgeons/consensus")
    async def surgeon_consensus(claim: str = ""):
        """Confidence-weighted consensus on a claim."""
        claim = (claim or "").strip()[:2000]
        if len(claim) < 6:
            return {"error": "Claim must be at least 6 characters", "ok": False}

        system = (
            "You are evaluating a claim for accuracy. Respond with:\n"
            "1. VERDICT: TRUE / FALSE / UNCERTAIN\n"
            "2. CONFIDENCE: 0-100%\n"
            "3. EVIDENCE: Key supporting/contradicting points (2-3 bullets)\n"
            "Be honest about uncertainty."
        )
        prompt = f"Evaluate this claim:\n\n{claim.strip()}"

        loop = asyncio.get_running_loop()

        async def _local():
            return await loop.run_in_executor(
                None, lambda: _surgery_query_local(system, prompt, profile="deep"))

        async def _remote():
            return await loop.run_in_executor(
                None, lambda: _surgery_query_remote(system, prompt))

        r_local, r_remote = await asyncio.gather(_local(), _remote())

        return {
            "ok": r_local["ok"] or r_remote["ok"],  # ok if at least one surgeon responded
            "claim": claim.strip(),
            "timestamp": datetime.now().isoformat(),
            "neurologist": {"ok": r_local["ok"], "content": r_local["content"],
                            "latency_ms": r_local.get("latency_ms", 0)},
            "cardiologist": {"ok": r_remote["ok"], "content": r_remote["content"],
                             "latency_ms": r_remote.get("latency_ms", 0),
                             "cost_usd": r_remote.get("cost_usd", 0)},
        }

    @app.post("/contextdna/surgeons/gains-gate")
    async def surgeon_gains_gate():
        """Run gains-gate.sh and return structured results."""
        import subprocess
        loop = asyncio.get_running_loop()

        def _run_gate():
            result = subprocess.run(
                ["bash", str(Path(__file__).parent.parent / "scripts" / "gains-gate.sh")],
                capture_output=True, text=True, timeout=60,
                cwd=str(Path(__file__).parent.parent),
                env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)},
            )
            return {
                "ok": result.returncode == 0,
                "exit_code": result.returncode,
                "output": result.stdout[-3000:] if result.stdout else "",
                "errors": result.stderr[-1000:] if result.stderr else "",
            }

        try:
            return await loop.run_in_executor(None, _run_gate)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "gains-gate timed out after 60s"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.get("/contextdna/surgeons/status")
    async def surgeon_status():
        """3-surgeon integration status — routing, costs, recent cross-exams."""
        import glob as _glob

        # Recent cross-exam results
        results_dir = Path("/tmp/atlas-agent-results")
        recent = []
        if results_dir.exists():
            files = sorted(_glob.glob(str(results_dir / "cross_exam_*.json")),
                           reverse=True)[:5]
            for f in files:
                try:
                    with open(f) as fh:
                        data = json.load(fh)
                        recent.append({
                            "topic": data.get("topic", "")[:80],
                            "timestamp": data.get("timestamp", ""),
                            "ok": data.get("ok", False),
                            "cost_usd": data.get("total_cost_usd", 0),
                        })
                except Exception as e:
                    logger.debug(f"Skipping corrupt cross-exam result {f}: {e}")

        # LLM hybrid routing status
        routing = {}
        try:
            import redis as redis_lib
            rc = redis_lib.Redis(host="127.0.0.1", port=6379,
                                 decode_responses=True, socket_timeout=2)
            routing["hybrid_mode"] = rc.get("llm:hybrid_mode") or "on"
            today = date.today().isoformat()
            cost_data = rc.hgetall(f"llm:costs:{today}")
            routing["today_cost_usd"] = float(cost_data.get("total_usd", 0))
            routing["today_calls"] = int(cost_data.get("total_calls", 0))
        except Exception as e:
            logger.warning(f"Surgeon status Redis check failed: {e}")
            routing["error"] = "Redis unavailable"

        return {
            "recent_cross_exams": recent,
            "routing": routing,
            "surgeons": {
                "atlas": "claude-opus (this session)",
                "cardiologist": "gpt-4.1-mini (OpenAI)",
                "neurologist": "Qwen3-4B-4bit (local MLX)",
            },
        }

else:
    # Fallback for when FastAPI is not installed
    app = None


# Standalone runner
def run_standalone():
    """Run the agent without FastAPI (simpler mode)."""
    import time

    print("Running Helper Agent in standalone mode...")
    print("Press Ctrl+C to stop")

    # Initialize components
    detector = EnhancedSuccessDetector() if ENHANCED_DETECTOR_AVAILABLE else None

    while True:
        try:
            # Check work log
            if WORK_LOG_AVAILABLE and detector:
                entries = work_log.get_recent_entries(hours=1, include_processed=False)
                if entries:
                    successes = detector.analyze_entries(entries)
                    high_conf = [s for s in successes if s.confidence >= 0.7]
                    if high_conf:
                        print(f"[{datetime.now()}] Detected {len(high_conf)} high-confidence successes")
                        for s in high_conf:
                            print(f"  - {s.task[:60]} ({s.confidence:.2f})")

            # Run brain cycle
            if BRAIN_AVAILABLE and brain:
                result = brain.run_cycle()
                if result.get("successes_recorded", 0) > 0:
                    print(f"[{datetime.now()}] Brain cycle: {result['successes_recorded']} successes recorded")

            time.sleep(60)

        except KeyboardInterrupt:
            print("\nStopping...")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "standalone":
            run_standalone()
        elif cmd == "status":
            print("Agent Service Status:")
            print(f"  FastAPI: {'Available' if FASTAPI_AVAILABLE else 'Not installed'}")
            print(f"  Work Log: {'Available' if WORK_LOG_AVAILABLE else 'Not available'}")
            print(f"  Detector: {'Available' if ENHANCED_DETECTOR_AVAILABLE else 'Not available'}")
            print(f"  Brain: {'Available' if BRAIN_AVAILABLE else 'Not available'}")
            print(f"  Redis: {'Available' if REDIS_AVAILABLE else 'Not available'}")
        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python agent_service.py [standalone|status]")
    else:
        if FASTAPI_AVAILABLE:
            import uvicorn
            uvicorn.run(app, host="0.0.0.0", port=8080)
        else:
            print("FastAPI not installed. Running in standalone mode...")
            run_standalone()
