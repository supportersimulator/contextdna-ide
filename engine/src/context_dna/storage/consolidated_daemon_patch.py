"""
Consolidated Daemon Patch for agent_service.py

This file contains the exact code to add to memory/agent_service.py
to enable unified daemon functionality with lite/heavy mode sync.

INSTALLATION:
1. Copy the imports to the top of agent_service.py
2. Add the __init__ additions to HelperAgent.__init__()
3. Add the start() additions to HelperAgent.start()
4. Add all the new loop methods to the HelperAgent class

After installation, you can remove:
- synaptic_watchdog_daemon.py (health monitoring now in unified daemon)
- heartbeat_watchdog.py (heartbeat now in unified daemon)
"""

# =============================================================================
# STEP 1: Add these imports to the top of agent_service.py
# =============================================================================

IMPORTS_PATCH = '''
# Consolidated daemon imports
try:
    from context_dna.storage.sync_integration import (
        check_and_sync_postgres,
        get_sync_status,
        SYNC_ENABLED,
    )
    SYNC_AVAILABLE = True
except ImportError:
    SYNC_AVAILABLE = False
    check_and_sync_postgres = None
    get_sync_status = None
    SYNC_ENABLED = False

try:
    from memory.agent_watchdog import get_watchdog, AgentWatchdog
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    get_watchdog = None
'''

# =============================================================================
# STEP 2: Add to HelperAgent.__init__()
# =============================================================================

INIT_PATCH = '''
        # Consolidated monitoring intervals
        self.sync_interval = int(os.getenv("POSTGRES_SYNC_INTERVAL", 120))
        self.health_interval = int(os.getenv("HEALTH_CHECK_INTERVAL", 60))
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", 10))

        # Track subsystem state
        self._last_activity = datetime.now()
        self._mode = "lite"  # Will be updated by sync loop
'''

# =============================================================================
# STEP 3: Add to HelperAgent.start()
# =============================================================================

START_PATCH = '''
        # Consolidated background loops
        if SYNC_AVAILABLE and SYNC_ENABLED:
            asyncio.create_task(self._postgres_sync_loop())
            print(f"📡 Postgres sync active (every {self.sync_interval}s)")

        if WATCHDOG_AVAILABLE:
            asyncio.create_task(self._health_monitor_loop())
            print(f"🏥 Health monitor active (every {self.health_interval}s)")

        asyncio.create_task(self._heartbeat_loop())
        print(f"💓 Heartbeat active (every {self.heartbeat_interval}s)")
'''

# =============================================================================
# STEP 4: Add these methods to HelperAgent class
# =============================================================================

METHODS_PATCH = '''
    async def _postgres_sync_loop(self):
        """
        Periodically sync with Postgres - handles lite/heavy mode transitions.

        This loop:
        1. Checks if Postgres is available
        2. If mode changed (lite→heavy or heavy→lite), handles transition
        3. Syncs any queued changes
        4. Broadcasts mode transitions to WebSocket clients
        """
        while self.running:
            try:
                result = await check_and_sync_postgres()

                if result and result.success:
                    # Track sync stats
                    self.stats["postgres_syncs"] = self.stats.get("postgres_syncs", 0) + 1

                    if result.items_synced_to_postgres > 0:
                        self.stats["items_synced_to_postgres"] = (
                            self.stats.get("items_synced_to_postgres", 0) +
                            result.items_synced_to_postgres
                        )

                    # Track current mode
                    self._mode = result.mode_after
                    self.stats["current_mode"] = result.mode_after
                    self.stats["postgres_available"] = result.postgres_available

                    # Broadcast mode transitions
                    if result.mode_before != result.mode_after:
                        await self._broadcast({
                            "type": "mode_transition",
                            "from": result.mode_before,
                            "to": result.mode_after,
                            "postgres_available": result.postgres_available,
                            "items_synced": result.items_synced_to_postgres,
                            "timestamp": datetime.now().isoformat()
                        })

                elif result and not result.success:
                    # Log sync errors but don't crash
                    self.stats["sync_errors"] = self.stats.get("sync_errors", 0) + 1
                    if result.error:
                        print(f"Sync warning: {result.error}")

            except Exception as e:
                print(f"Postgres sync loop error: {e}")
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

            except Exception as e:
                print(f"Health monitor error: {e}")

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
                total_processed = self.stats.get("entries_processed_session", 0)
                total_cycles = self.stats.get("brain_cycles", 0)

                # Any activity counts
                if total_processed > 0 or total_cycles > 0:
                    self._last_activity = current_time

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

            except Exception as e:
                print(f"Heartbeat error: {e}")

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
                print(f"[WARN] Redis health check failed: {e}")

        # Postgres (via sync status)
        if SYNC_AVAILABLE:
            try:
                sync_status = get_sync_status()
                if sync_status and sync_status.get("last_result"):
                    health["postgres"] = sync_status["last_result"].get("postgres_available", False)
                health["sync"] = sync_status.get("enabled", False)
            except Exception as e:
                print(f"[WARN] Postgres/sync health check failed: {e}")

        # Evolution engine
        if self.evolution_engine:
            health["evolution"] = True

        # Professor
        if PROFESSOR_AVAILABLE:
            health["professor"] = True

        return health
'''


# =============================================================================
# CLI - Apply the patch
# =============================================================================

def show_patch():
    """Display all patches for manual application."""
    print("=" * 70)
    print("CONSOLIDATED DAEMON PATCH FOR agent_service.py")
    print("=" * 70)

    print("\n" + "=" * 70)
    print("STEP 1: Add these imports near the top of agent_service.py")
    print("=" * 70)
    print(IMPORTS_PATCH)

    print("\n" + "=" * 70)
    print("STEP 2: Add to HelperAgent.__init__() after existing interval definitions")
    print("=" * 70)
    print(INIT_PATCH)

    print("\n" + "=" * 70)
    print("STEP 3: Add to HelperAgent.start() after existing create_task calls")
    print("=" * 70)
    print(START_PATCH)

    print("\n" + "=" * 70)
    print("STEP 4: Add these methods to HelperAgent class")
    print("=" * 70)
    print(METHODS_PATCH)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "show":
        show_patch()
    else:
        print("Usage: python consolidated_daemon_patch.py show")
        print()
        print("This displays the patches to manually add to agent_service.py")
        print("for unified daemon functionality.")
