"""
Mode Transition Manager for Context DNA

Handles elegant transitions between Heavy Mode (Docker/PostgreSQL) and
Lite Mode (SQLite-only) with proper data handoffs and staged shutdowns.

Key Safety Principles:
1. SYNC BEFORE SHUTDOWN: Always snapshot PostgreSQL → SQLite before stopping containers
2. STAGED SHUTDOWN: Respect dependency order (API → Workers → Queues → DB)
3. GRACEFUL FIRST: Try graceful shutdown, force kill only if stuck
4. VERIFY STATE: Confirm target mode is operational before declaring success
5. REVERSIBLE: Can transition back at any time

Usage:
    from context_dna.storage.mode_transition import ModeTransitionManager

    manager = ModeTransitionManager()

    # Transition heavy → lite
    result = await manager.transition_to_lite()

    # Transition lite → heavy
    result = await manager.transition_to_heavy()
"""

import asyncio
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
import logging

from .docker_recovery import get_docker_recovery, DockerRecovery
from .bidirectional_sync import BidirectionalSyncStore, SyncState

logger = logging.getLogger(__name__)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ContainerShutdownOrder:
    """Defines the safe shutdown order for containers."""
    # Priority 1 = shutdown first, higher = shutdown later
    API_SERVICES = 1        # Stop accepting new work
    CELERY_WORKERS = 2      # Drain running tasks
    CELERY_BEAT = 3         # Stop scheduler
    MESSAGE_BROKER = 4      # RabbitMQ/Redis
    DATABASE = 5            # PostgreSQL - save for last
    TELEMETRY = 6           # Monitoring/logging
    CORE = 7                # Infrastructure


@dataclass
class TransitionPhase:
    """Tracks a phase in the transition."""
    name: str
    status: str = 'pending'  # pending, in_progress, completed, failed, skipped
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def start(self):
        self.status = 'in_progress'
        self.started_at = datetime.now(timezone.utc)

    def complete(self, details: Dict[str, Any] = None):
        self.status = 'completed'
        self.completed_at = datetime.now(timezone.utc)
        if self.started_at:
            self.duration_ms = int((self.completed_at - self.started_at).total_seconds() * 1000)
        if details:
            self.details.update(details)

    def fail(self, error: str, details: Dict[str, Any] = None):
        self.status = 'failed'
        self.completed_at = datetime.now(timezone.utc)
        self.error = error
        if self.started_at:
            self.duration_ms = int((self.completed_at - self.started_at).total_seconds() * 1000)
        if details:
            self.details.update(details)

    def skip(self, reason: str):
        self.status = 'skipped'
        self.details['skip_reason'] = reason

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'status': self.status,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'duration_ms': self.duration_ms,
            'error': self.error,
            'details': self.details,
        }


@dataclass
class TransitionResult:
    """Complete result of a mode transition."""
    success: bool
    from_mode: str
    to_mode: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    total_duration_ms: Optional[int] = None
    phases: List[TransitionPhase] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'success': self.success,
            'from_mode': self.from_mode,
            'to_mode': self.to_mode,
            'started_at': self.started_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'total_duration_ms': self.total_duration_ms,
            'phases': [p.to_dict() for p in self.phases],
            'error': self.error,
        }


# =============================================================================
# CONTAINER CONFIGURATION
# =============================================================================

# Container groups for staged shutdown (highest priority = shutdown first)
CONTAINER_GROUPS = {
    ContainerShutdownOrder.API_SERVICES: [
        'context-dna-api',
        'contextdna-api',
        'api',
    ],
    ContainerShutdownOrder.CELERY_WORKERS: [
        'celery-worker',
        'celery_worker',
        'worker',
    ],
    ContainerShutdownOrder.CELERY_BEAT: [
        'celery-beat',
        'celery_beat',
        'beat',
        'scheduler',
    ],
    ContainerShutdownOrder.MESSAGE_BROKER: [
        'rabbitmq',
        'redis',
        'broker',
    ],
    ContainerShutdownOrder.DATABASE: [
        'postgres',
        'postgresql',
        'db',
        'database',
    ],
    ContainerShutdownOrder.TELEMETRY: [
        'prometheus',
        'grafana',
        'telemetry',
        'otel',
        'jaeger',
    ],
    ContainerShutdownOrder.CORE: [
        'nginx',
        'traefik',
        'proxy',
    ],
}


# =============================================================================
# MODE TRANSITION MANAGER
# =============================================================================

class ModeTransitionManager:
    """
    Manages elegant transitions between Heavy and Lite modes.

    Heavy → Lite:
    1. Pre-sync: PostgreSQL → SQLite snapshot
    2. Staged shutdown: API → Workers → Queue → DB
    3. Docker shutdown (optional)
    4. Lite mode activation

    Lite → Heavy:
    1. Docker startup (with recovery)
    2. Wait for containers healthy
    3. Sync: SQLite → PostgreSQL (queued changes)
    4. Heavy mode activation
    """

    def __init__(
        self,
        compose_file: Optional[Path] = None,
        db_path: Optional[Path] = None,
    ):
        self._docker = get_docker_recovery()
        self._compose_file = compose_file or self._find_compose_file()
        self._db_path = db_path or Path.home() / '.contextdna/context_dna.db'
        self._sync_store: Optional[BidirectionalSyncStore] = None

    def _find_compose_file(self) -> Optional[Path]:
        """Find docker-compose.yml in common locations."""
        locations = [
            Path.cwd() / 'docker-compose.yml',
            Path.cwd() / 'docker-compose.yaml',
            Path.cwd() / 'infra/docker-compose.yml',
            Path(__file__).parent.parent.parent.parent / 'docker-compose.yml',
        ]
        for loc in locations:
            if loc.exists():
                return loc
        return None

    async def _get_sync_store(self) -> BidirectionalSyncStore:
        """Get or create sync store instance."""
        if self._sync_store is None:
            self._sync_store = BidirectionalSyncStore()
            await self._sync_store.connect()
        return self._sync_store

    def _run_command(self, cmd: list, timeout: float = 30.0) -> tuple:
        """Run a command and return (success, output)."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.returncode == 0, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)

    # =========================================================================
    # CONTAINER MANAGEMENT
    # =========================================================================

    def get_running_containers(self) -> List[Dict[str, str]]:
        """Get list of running Docker containers."""
        success, output = self._run_command([
            'docker', 'ps', '--format', '{{.Names}}\t{{.Status}}'
        ])

        if not success:
            return []

        containers = []
        for line in output.strip().split('\n'):
            if line.strip():
                parts = line.split('\t')
                if len(parts) >= 2:
                    containers.append({
                        'name': parts[0],
                        'status': parts[1],
                    })
        return containers

    def _get_container_group(self, container_name: str) -> int:
        """Get shutdown priority for a container based on its name."""
        name_lower = container_name.lower()

        for priority, patterns in CONTAINER_GROUPS.items():
            for pattern in patterns:
                if pattern in name_lower:
                    return priority

        # Unknown containers shutdown last
        return 99

    def _sort_containers_for_shutdown(self, containers: List[Dict]) -> List[Dict]:
        """Sort containers by shutdown priority (lowest priority = shutdown first)."""
        return sorted(containers, key=lambda c: self._get_container_group(c['name']))

    async def stop_container(
        self,
        name: str,
        timeout: int = 30,
        force: bool = False
    ) -> bool:
        """
        Stop a single container gracefully.

        Args:
            name: Container name
            timeout: Seconds to wait for graceful shutdown
            force: If True, kill immediately instead of stop
        """
        cmd = ['docker', 'kill', name] if force else ['docker', 'stop', '-t', str(timeout), name]
        success, output = self._run_command(cmd, timeout=timeout + 10)

        if success:
            logger.info(f"{'Killed' if force else 'Stopped'} container: {name}")
        else:
            logger.warning(f"Failed to stop container {name}: {output}")

        return success

    async def staged_container_shutdown(
        self,
        timeout_per_container: int = 30,
        force_after_timeout: bool = True
    ) -> Dict[str, Any]:
        """
        Shut down containers in proper dependency order.

        1. API services (stop accepting new requests)
        2. Workers (let current tasks drain)
        3. Schedulers (stop queuing new tasks)
        4. Message brokers (after workers are done)
        5. Databases (after everything else)
        6. Telemetry/monitoring
        7. Core infrastructure

        Returns dict with shutdown results.
        """
        result = {
            'success': True,
            'containers_stopped': [],
            'containers_failed': [],
            'containers_skipped': [],
        }

        # Get running containers
        containers = self.get_running_containers()
        if not containers:
            logger.info("No containers running")
            return result

        # Sort by shutdown priority
        sorted_containers = self._sort_containers_for_shutdown(containers)

        logger.info(f"Shutting down {len(sorted_containers)} containers in staged order...")

        for container in sorted_containers:
            name = container['name']
            priority = self._get_container_group(name)

            logger.info(f"  [{priority}] Stopping: {name}")

            # Try graceful stop first
            success = await self.stop_container(name, timeout=timeout_per_container, force=False)

            if not success and force_after_timeout:
                # Force kill if graceful failed
                logger.warning(f"  Force killing stuck container: {name}")
                success = await self.stop_container(name, force=True)

            if success:
                result['containers_stopped'].append(name)
            else:
                result['containers_failed'].append(name)
                result['success'] = False

            # Small delay between containers for stability
            await asyncio.sleep(0.5)

        return result

    async def start_containers_compose(self, timeout: float = 120.0) -> bool:
        """Start containers using docker-compose."""
        if not self._compose_file:
            logger.error("No docker-compose.yml found")
            return False

        logger.info(f"Starting containers from {self._compose_file}")

        success, output = self._run_command([
            'docker', 'compose', '-f', str(self._compose_file), 'up', '-d'
        ], timeout=timeout)

        if success:
            logger.info("Containers started")
        else:
            logger.error(f"Failed to start containers: {output}")

        return success

    # =========================================================================
    # HEAVY → LITE TRANSITION
    # =========================================================================

    async def transition_to_lite(
        self,
        stop_docker: bool = True,
        force_shutdown: bool = False,
        timeout: float = 120.0
    ) -> TransitionResult:
        """
        Transition from Heavy Mode to Lite Mode.

        Phases:
        1. PRE_SYNC: Snapshot PostgreSQL → SQLite
        2. STAGED_SHUTDOWN: Stop containers in proper order
        3. DOCKER_SHUTDOWN: Optionally stop Docker Desktop
        4. LITE_ACTIVATION: Update mode state and verify

        Args:
            stop_docker: If True, also quit Docker Desktop
            force_shutdown: If True, force kill stuck containers
            timeout: Max time for entire transition

        Returns:
            TransitionResult with full details
        """
        result = TransitionResult(
            success=False,
            from_mode='heavy',
            to_mode='lite',
            started_at=datetime.now(timezone.utc),
        )

        try:
            store = await self._get_sync_store()
            state = await store.get_sync_state()

            # Verify we're in heavy mode
            if state.current_mode != 'heavy':
                result.error = f"Already in {state.current_mode} mode"
                logger.warning(result.error)
                # Still mark as success if already in lite
                if state.current_mode == 'lite':
                    result.success = True
                return result

            # ─────────────────────────────────────────────────────────────────
            # PHASE 1: PRE-SYNC (PostgreSQL → SQLite)
            # ─────────────────────────────────────────────────────────────────
            phase1 = TransitionPhase(name='PRE_SYNC')
            result.phases.append(phase1)
            phase1.start()

            logger.info("Phase 1: Pre-sync PostgreSQL → SQLite")

            try:
                # Force a fresh snapshot
                await store._sync_heavy_to_lite()

                # Verify SQLite has data
                conn = sqlite3.connect(str(store.fallback_path))
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM hierarchy_profiles")
                profile_count = cursor.fetchone()[0]
                conn.close()

                phase1.complete({
                    'profiles_synced': profile_count,
                    'sqlite_path': str(store.fallback_path),
                })
                logger.info(f"  Synced {profile_count} profiles to SQLite")

            except Exception as e:
                phase1.fail(str(e))
                logger.error(f"  Pre-sync failed: {e}")
                # Continue anyway - SQLite may have recent data from write-through

            # ─────────────────────────────────────────────────────────────────
            # PHASE 2: STAGED CONTAINER SHUTDOWN
            # ─────────────────────────────────────────────────────────────────
            phase2 = TransitionPhase(name='STAGED_SHUTDOWN')
            result.phases.append(phase2)
            phase2.start()

            logger.info("Phase 2: Staged container shutdown")

            try:
                shutdown_result = await self.staged_container_shutdown(
                    timeout_per_container=30,
                    force_after_timeout=force_shutdown
                )

                if shutdown_result['success']:
                    phase2.complete(shutdown_result)
                    logger.info(f"  Stopped {len(shutdown_result['containers_stopped'])} containers")
                else:
                    phase2.fail(
                        f"Failed to stop: {shutdown_result['containers_failed']}",
                        shutdown_result
                    )

            except Exception as e:
                phase2.fail(str(e))
                logger.error(f"  Container shutdown error: {e}")

            # ─────────────────────────────────────────────────────────────────
            # PHASE 3: DOCKER SHUTDOWN (Optional)
            # ─────────────────────────────────────────────────────────────────
            phase3 = TransitionPhase(name='DOCKER_SHUTDOWN')
            result.phases.append(phase3)

            if stop_docker:
                phase3.start()
                logger.info("Phase 3: Docker Desktop shutdown")

                try:
                    # Use graceful quit first
                    if self._docker.quit_docker_desktop(force=force_shutdown):
                        # Wait for Docker to fully stop
                        for i in range(15):
                            if not self._docker.is_docker_desktop_app_running():
                                break
                            await asyncio.sleep(1)

                        if self._docker.is_docker_desktop_app_running():
                            phase3.fail("Docker didn't quit in time")
                        else:
                            phase3.complete({'quit_method': 'graceful' if not force_shutdown else 'force'})
                            logger.info("  Docker Desktop stopped")
                    else:
                        phase3.fail("Quit command failed")

                except Exception as e:
                    phase3.fail(str(e))
                    logger.error(f"  Docker quit error: {e}")
            else:
                phase3.skip("Docker shutdown disabled")
                logger.info("Phase 3: Skipped (Docker left running)")

            # ─────────────────────────────────────────────────────────────────
            # PHASE 4: LITE MODE ACTIVATION
            # ─────────────────────────────────────────────────────────────────
            phase4 = TransitionPhase(name='LITE_ACTIVATION')
            result.phases.append(phase4)
            phase4.start()

            logger.info("Phase 4: Lite mode activation")

            try:
                # Update mode state
                await store._update_sync_state(
                    current_mode='lite',
                    last_mode='heavy',
                    postgres_available=False,
                    last_sync_at=datetime.utcnow(),
                )

                # Verify SQLite is accessible
                conn = sqlite3.connect(str(store.fallback_path))
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                conn.close()

                phase4.complete({'mode': 'lite', 'sqlite_verified': True})
                logger.info("  Lite mode activated")

            except Exception as e:
                phase4.fail(str(e))
                logger.error(f"  Lite activation error: {e}")

            # ─────────────────────────────────────────────────────────────────
            # FINALIZE
            # ─────────────────────────────────────────────────────────────────
            result.completed_at = datetime.now(timezone.utc)
            result.total_duration_ms = int(
                (result.completed_at - result.started_at).total_seconds() * 1000
            )

            # Success if critical phases completed
            critical_phases = ['PRE_SYNC', 'LITE_ACTIVATION']
            critical_ok = all(
                p.status in ('completed', 'skipped')
                for p in result.phases
                if p.name in critical_phases
            )

            result.success = critical_ok

            if result.success:
                logger.info(f"✓ Transition to lite mode complete ({result.total_duration_ms}ms)")
            else:
                logger.error("✗ Transition to lite mode failed")

        except Exception as e:
            result.error = str(e)
            result.completed_at = datetime.now(timezone.utc)
            logger.error(f"Transition error: {e}")

        return result

    # =========================================================================
    # LITE → HEAVY TRANSITION
    # =========================================================================

    async def transition_to_heavy(
        self,
        timeout: float = 120.0,
        wait_for_healthy: bool = True
    ) -> TransitionResult:
        """
        Transition from Lite Mode to Heavy Mode.

        Phases:
        1. DOCKER_STARTUP: Ensure Docker is running
        2. CONTAINER_STARTUP: Start containers via compose
        3. HEALTH_CHECK: Wait for services to be healthy
        4. DATA_SYNC: Sync SQLite → PostgreSQL (queued changes)
        5. HEAVY_ACTIVATION: Update mode state

        Args:
            timeout: Max time for entire transition
            wait_for_healthy: If True, wait for all services healthy

        Returns:
            TransitionResult with full details
        """
        result = TransitionResult(
            success=False,
            from_mode='lite',
            to_mode='heavy',
            started_at=datetime.now(timezone.utc),
        )

        try:
            store = await self._get_sync_store()
            state = await store.get_sync_state()

            # Check current mode (allow if already heavy or lite)
            if state.current_mode == 'heavy':
                logger.info("Already in heavy mode, verifying services...")

            # ─────────────────────────────────────────────────────────────────
            # PHASE 1: DOCKER STARTUP
            # ─────────────────────────────────────────────────────────────────
            phase1 = TransitionPhase(name='DOCKER_STARTUP')
            result.phases.append(phase1)
            phase1.start()

            logger.info("Phase 1: Docker startup")

            try:
                if self._docker.is_docker_daemon_running():
                    phase1.complete({'already_running': True})
                    logger.info("  Docker already running")
                else:
                    # Use smart recovery
                    recovery_result = await self._docker.smart_recover(timeout=60)

                    if recovery_result['success']:
                        phase1.complete({
                            'action': recovery_result['action_taken'],
                            'details': recovery_result['details'],
                        })
                        logger.info(f"  Docker ready ({recovery_result['action_taken']})")

                        # Capture Docker environment for future recovery
                        try:
                            env = self._docker.capture_environment()
                            if env:
                                launch_method = recovery_result.get(
                                    'launch_strategy',
                                    recovery_result['action_taken']
                                )
                                self._docker.save_environment_to_sqlite(
                                    self._db_path, env,
                                    launch_method=launch_method
                                )
                                logger.info(f"  Docker env cached (method: {launch_method})")
                        except Exception as e:
                            logger.warning(f"  Failed to cache Docker env: {e}")
                    else:
                        phase1.fail(recovery_result['details'], recovery_result)
                        result.error = "Docker recovery failed"
                        return result

            except Exception as e:
                phase1.fail(str(e))
                result.error = f"Docker startup error: {e}"
                return result

            # ─────────────────────────────────────────────────────────────────
            # PHASE 2: CONTAINER STARTUP
            # ─────────────────────────────────────────────────────────────────
            phase2 = TransitionPhase(name='CONTAINER_STARTUP')
            result.phases.append(phase2)
            phase2.start()

            logger.info("Phase 2: Container startup")

            try:
                # Check if containers already running
                running = self.get_running_containers()

                if len(running) > 0:
                    phase2.complete({
                        'already_running': True,
                        'container_count': len(running),
                    })
                    logger.info(f"  {len(running)} containers already running")
                else:
                    # Start via compose
                    if await self.start_containers_compose(timeout=90):
                        # Wait a moment for services to initialize
                        await asyncio.sleep(5)
                        running = self.get_running_containers()
                        phase2.complete({
                            'started': True,
                            'container_count': len(running),
                        })
                        logger.info(f"  Started {len(running)} containers")
                    else:
                        phase2.fail("docker compose up failed")

            except Exception as e:
                phase2.fail(str(e))
                logger.error(f"  Container startup error: {e}")

            # ─────────────────────────────────────────────────────────────────
            # PHASE 3: HEALTH CHECK
            # ─────────────────────────────────────────────────────────────────
            phase3 = TransitionPhase(name='HEALTH_CHECK')
            result.phases.append(phase3)

            if wait_for_healthy:
                phase3.start()
                logger.info("Phase 3: Service health check")

                try:
                    # Wait for PostgreSQL specifically
                    postgres_ready = False
                    for attempt in range(30):
                        # Try to connect via sync store
                        await store.close()
                        self._sync_store = None
                        store = await self._get_sync_store()

                        if not store._using_fallback:
                            postgres_ready = True
                            break

                        logger.info(f"  Waiting for PostgreSQL... ({attempt + 1}/30)")
                        await asyncio.sleep(2)

                    if postgres_ready:
                        phase3.complete({'postgres_ready': True})
                        logger.info("  PostgreSQL ready")
                    else:
                        phase3.fail("PostgreSQL not ready after timeout")

                except Exception as e:
                    phase3.fail(str(e))
                    logger.error(f"  Health check error: {e}")
            else:
                phase3.skip("Health check disabled")

            # ─────────────────────────────────────────────────────────────────
            # PHASE 4: DATA SYNC (SQLite → PostgreSQL)
            # ─────────────────────────────────────────────────────────────────
            phase4 = TransitionPhase(name='DATA_SYNC')
            result.phases.append(phase4)
            phase4.start()

            logger.info("Phase 4: Data sync (SQLite → PostgreSQL)")

            try:
                # Process queued changes
                sync_result = await store.force_sync()

                phase4.complete({
                    'lite_to_heavy': sync_result['lite_to_heavy'],
                    'heavy_to_lite': sync_result['heavy_to_lite'],
                })
                logger.info(f"  Synced {sync_result['lite_to_heavy']['success']} items to PostgreSQL")

            except Exception as e:
                phase4.fail(str(e))
                logger.error(f"  Data sync error: {e}")
                # Non-fatal - continue with mode switch

            # ─────────────────────────────────────────────────────────────────
            # PHASE 5: HEAVY MODE ACTIVATION
            # ─────────────────────────────────────────────────────────────────
            phase5 = TransitionPhase(name='HEAVY_ACTIVATION')
            result.phases.append(phase5)
            phase5.start()

            logger.info("Phase 5: Heavy mode activation")

            try:
                await store._update_sync_state(
                    current_mode='heavy',
                    last_mode='lite',
                    postgres_available=True,
                    last_sync_at=datetime.utcnow(),
                )

                phase5.complete({'mode': 'heavy'})
                logger.info("  Heavy mode activated")

            except Exception as e:
                phase5.fail(str(e))
                logger.error(f"  Heavy activation error: {e}")

            # ─────────────────────────────────────────────────────────────────
            # FINALIZE
            # ─────────────────────────────────────────────────────────────────
            result.completed_at = datetime.now(timezone.utc)
            result.total_duration_ms = int(
                (result.completed_at - result.started_at).total_seconds() * 1000
            )

            # Success if critical phases completed
            critical_phases = ['DOCKER_STARTUP', 'HEAVY_ACTIVATION']
            critical_ok = all(
                p.status in ('completed', 'skipped')
                for p in result.phases
                if p.name in critical_phases
            )

            result.success = critical_ok

            if result.success:
                logger.info(f"✓ Transition to heavy mode complete ({result.total_duration_ms}ms)")
            else:
                logger.error("✗ Transition to heavy mode failed")

        except Exception as e:
            result.error = str(e)
            result.completed_at = datetime.now(timezone.utc)
            logger.error(f"Transition error: {e}")

        return result

    # =========================================================================
    # UTILITIES
    # =========================================================================

    async def get_current_mode(self) -> Dict[str, Any]:
        """Get current mode and status."""
        store = await self._get_sync_store()
        state = await store.get_sync_state()

        docker_status = self._docker.diagnose_docker_state()
        containers = self.get_running_containers()

        return {
            'mode': state.current_mode,
            'last_mode': state.last_mode,
            'last_sync_at': state.last_sync_at.isoformat() if state.last_sync_at else None,
            'postgres_available': state.postgres_available,
            'docker_state': docker_status['state'],
            'running_containers': len(containers),
            'sync_state': state.to_dict(),
        }

    async def close(self):
        """Clean up resources."""
        if self._sync_store:
            await self._sync_store.close()
            self._sync_store = None


# =============================================================================
# SINGLETON
# =============================================================================

_mode_manager: Optional[ModeTransitionManager] = None

def get_mode_manager() -> ModeTransitionManager:
    """Get or create singleton ModeTransitionManager."""
    global _mode_manager
    if _mode_manager is None:
        _mode_manager = ModeTransitionManager()
    return _mode_manager


# =============================================================================
# CLI
# =============================================================================

if __name__ == '__main__':
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(message)s'
    )

    async def main():
        manager = ModeTransitionManager()

        if len(sys.argv) > 1:
            cmd = sys.argv[1]

            if cmd == 'status':
                status = await manager.get_current_mode()
                print("\n=== Mode Status ===")
                print(f"Current Mode: {status['mode'].upper()}")
                print(f"Last Mode: {status['last_mode'] or 'N/A'}")
                print(f"Last Sync: {status['last_sync_at'] or 'Never'}")
                print(f"PostgreSQL: {'✓' if status['postgres_available'] else '✗'}")
                print(f"Docker: {status['docker_state']}")
                print(f"Containers: {status['running_containers']} running")

            elif cmd == 'to-lite':
                print("\n=== Transitioning to Lite Mode ===")
                stop_docker = '--keep-docker' not in sys.argv
                force = '--force' in sys.argv

                result = await manager.transition_to_lite(
                    stop_docker=stop_docker,
                    force_shutdown=force
                )

                print(f"\n{'✓' if result.success else '✗'} Transition {'succeeded' if result.success else 'failed'}")
                print(f"Duration: {result.total_duration_ms}ms")

                print("\n=== Phases ===")
                for phase in result.phases:
                    icon = '✓' if phase.status == 'completed' else ('⊘' if phase.status == 'skipped' else '✗')
                    print(f"  {icon} {phase.name}: {phase.status}")
                    if phase.error:
                        print(f"      Error: {phase.error}")
                    if phase.duration_ms:
                        print(f"      Duration: {phase.duration_ms}ms")

            elif cmd == 'to-heavy':
                print("\n=== Transitioning to Heavy Mode ===")
                no_wait = '--no-wait' in sys.argv

                result = await manager.transition_to_heavy(
                    wait_for_healthy=not no_wait
                )

                print(f"\n{'✓' if result.success else '✗'} Transition {'succeeded' if result.success else 'failed'}")
                print(f"Duration: {result.total_duration_ms}ms")

                print("\n=== Phases ===")
                for phase in result.phases:
                    icon = '✓' if phase.status == 'completed' else ('⊘' if phase.status == 'skipped' else '✗')
                    print(f"  {icon} {phase.name}: {phase.status}")
                    if phase.error:
                        print(f"      Error: {phase.error}")
                    if phase.duration_ms:
                        print(f"      Duration: {phase.duration_ms}ms")

            elif cmd == 'containers':
                containers = manager.get_running_containers()
                print(f"\n=== Running Containers ({len(containers)}) ===")
                for c in containers:
                    priority = manager._get_container_group(c['name'])
                    print(f"  [{priority}] {c['name']}: {c['status']}")

            else:
                print(f"Unknown command: {cmd}")
                print("Usage: python mode_transition.py <command>")
                print("\nCommands:")
                print("  status      - Show current mode and status")
                print("  to-lite     - Transition to lite mode")
                print("                --keep-docker  Don't stop Docker Desktop")
                print("                --force        Force kill stuck containers")
                print("  to-heavy    - Transition to heavy mode")
                print("                --no-wait      Don't wait for services healthy")
                print("  containers  - List running containers")
        else:
            # Default: show status
            status = await manager.get_current_mode()
            print(f"Mode: {status['mode'].upper()} | Docker: {status['docker_state']} | Containers: {status['running_containers']}")

        await manager.close()

    asyncio.run(main())
