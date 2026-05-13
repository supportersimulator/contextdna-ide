#!/usr/bin/env python3
"""
SCHEDULER COORDINATOR - Clean Handoff Between Lite and Heavy Mode

Manages the transition between:
- Lite Mode: lite_scheduler.py (SQLite-backed, no external deps)
- Heavy Mode: Celery Beat (RabbitMQ + Redis)

Ensures only ONE scheduler runs at a time, with clean handoff.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                   SCHEDULER COORDINATOR                          │
    │                                                                  │
    │  ┌──────────────┐         Mode Change         ┌──────────────┐  │
    │  │ Lite Mode    │ ◄─────────────────────────► │ Heavy Mode   │  │
    │  │              │                             │              │  │
    │  │ lite_sched   │    stop lite / start heavy  │ Celery Beat  │  │
    │  │ (SQLite)     │ ◄─────────────────────────► │ (RabbitMQ)   │  │
    │  └──────────────┘                             └──────────────┘  │
    │                                                                  │
    │  Observability Store: Always writes (SQLite primary, PG when    │
    │                       available)                                 │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    from memory.scheduler_coordinator import SchedulerCoordinator

    coordinator = SchedulerCoordinator()
    coordinator.start()  # Auto-detects mode and starts appropriate scheduler

    # Or run as daemon
    python memory/scheduler_coordinator.py
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# State file to track which scheduler is active
STATE_FILE = Path(__file__).parent / ".scheduler_state.json"

# PID file for singleton guard (prevents duplicate coordinators)
PID_FILE = Path(__file__).parent / ".scheduler_coordinator.pid"

# Redis keys for persistent lockout state
CELERY_LOCKOUT_KEY = "contextdna:scheduler:celery_lockout"
CELERY_LOCKOUT_TTL = 600  # 10 minutes


class SchedulerCoordinator:
    """
    Coordinates between lite_scheduler and Celery Beat.

    Ensures clean handoff when mode changes:
    - lite → heavy: Stop lite_scheduler, verify Celery running
    - heavy → lite: Celery stops naturally when Docker down, start lite_scheduler
    """

    # CPU watchdog thresholds
    _CPU_SPIN_THRESHOLD = 60.0  # % CPU to consider spinning
    _CPU_SPIN_STRIKES = 2  # consecutive high-CPU checks before restart

    def __init__(self):
        self._running = False
        self._current_mode: Optional[str] = None
        self._lite_scheduler = None
        self._lite_thread: Optional[threading.Thread] = None
        self._check_interval = 30  # Check mode every 30 seconds
        self._celery_consecutive_failures = 0
        self._celery_lockout_until: float = 0  # monotonic timestamp
        # CPU watchdog state
        self._cpu_prev_times: Optional[tuple] = None  # (user+sys, wall_monotonic)
        self._cpu_spin_strikes = 0
        self._owns_pid_file = False

    def _acquire_singleton(self) -> bool:
        """Acquire singleton PID file. Returns True if acquired, False if another instance alive."""
        if PID_FILE.exists():
            try:
                old_pid = int(PID_FILE.read_text().strip())
                try:
                    os.kill(old_pid, 0)  # Check if alive
                    if old_pid != os.getpid():
                        logger.error(f"Scheduler coordinator already running (PID {old_pid}). Aborting start.")
                        return False
                except OSError:
                    logger.warning(f"Stale PID file for dead process {old_pid}, taking over")
            except (ValueError, IOError):
                pass
        PID_FILE.write_text(str(os.getpid()))
        self._owns_pid_file = True
        return True

    def _release_singleton(self):
        """Release singleton PID file if we own it."""
        if self._owns_pid_file:
            try:
                PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            self._owns_pid_file = False

    def _check_redis_lockout(self) -> bool:
        """Check if Celery is locked out via persistent Redis state."""
        try:
            from memory.redis_cache import get_redis_client
            r = get_redis_client()
            if r and r.get(CELERY_LOCKOUT_KEY):
                return True
        except Exception:
            pass
        return False

    def _set_redis_lockout(self):
        """Persist Celery lockout to Redis with TTL."""
        try:
            from memory.redis_cache import get_redis_client
            r = get_redis_client()
            if r:
                r.setex(CELERY_LOCKOUT_KEY, CELERY_LOCKOUT_TTL, "locked")
        except Exception:
            pass  # In-memory fallback still works

    def _clear_redis_lockout(self):
        """Clear Celery lockout when Celery recovers."""
        try:
            from memory.redis_cache import get_redis_client
            r = get_redis_client()
            if r:
                r.delete(CELERY_LOCKOUT_KEY)
        except Exception:
            pass

    def _detect_mode(self) -> str:
        """
        Detect operational mode via mode_authority, with Celery lockout override.

        Priority:
        0. Celery lockout (after 2+ consecutive failures, stay lite for 10 min)
        1. mode_authority.get_mode() (env > Redis truth_pointer > manifest > 'lite')
        """
        # Check Celery lockout first — prevents crash-loop
        # Check persistent Redis state first, then fall back to in-memory
        if self._check_redis_lockout() or self._celery_consecutive_failures >= 2:
            if time.monotonic() < self._celery_lockout_until:
                logger.debug(
                    f"Celery locked out ({self._celery_consecutive_failures} failures, "
                    f"{self._celery_lockout_until - time.monotonic():.0f}s remaining) → lite"
                )
                return "lite"
            else:
                # Lockout expired — reset and allow re-detection
                logger.info("Celery lockout expired, allowing re-detection")
                self._celery_consecutive_failures = 0
                self._clear_redis_lockout()

        # Old: heartbeat get_intended_mode + check_docker_status — replaced by mode_authority (env > Redis > manifest)
        from memory.mode_authority import get_mode
        return get_mode()

    def _is_celery_running(self) -> bool:
        """Check if Celery Beat is running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "celery.*beat"],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def _check_cpu_watchdog(self):
        """Detect CPU spinning and auto-restart lite scheduler if needed.

        Uses os.times() delta to compute process CPU% over the check interval.
        If CPU > threshold for consecutive checks, force-restart the scheduler.
        """
        try:
            t = os.times()
            cpu_now = t.user + t.system
            wall_now = time.monotonic()

            if self._cpu_prev_times is not None:
                prev_cpu, prev_wall = self._cpu_prev_times
                wall_delta = wall_now - prev_wall
                if wall_delta > 0:
                    cpu_pct = ((cpu_now - prev_cpu) / wall_delta) * 100

                    if cpu_pct > self._CPU_SPIN_THRESHOLD:
                        self._cpu_spin_strikes += 1
                        logger.warning(
                            f"CPU watchdog: {cpu_pct:.1f}% CPU "
                            f"(strike {self._cpu_spin_strikes}/{self._CPU_SPIN_STRIKES})"
                        )
                        if self._cpu_spin_strikes >= self._CPU_SPIN_STRIKES:
                            logger.error(
                                f"CPU watchdog TRIGGERED: {cpu_pct:.1f}% CPU for "
                                f"{self._cpu_spin_strikes} consecutive checks — "
                                f"force-restarting lite scheduler"
                            )
                            # Clear GPU lock (may be stale from spinning thread)
                            try:
                                import redis
                                r = redis.Redis(host="127.0.0.1", port=6379)
                                holder = r.get("llm:gpu_lock")
                                if holder:
                                    r.delete("llm:gpu_lock")
                                    logger.info(f"Cleared stale GPU lock (holder: {holder})")
                            except Exception:
                                pass
                            # Force-restart the lite scheduler
                            self._stop_lite_scheduler()
                            time.sleep(1)
                            self._start_lite_scheduler()
                            self._cpu_spin_strikes = 0
                    else:
                        self._cpu_spin_strikes = 0

            self._cpu_prev_times = (cpu_now, wall_now)
        except Exception as e:
            logger.debug(f"CPU watchdog error: {e}")

    def _is_lite_scheduler_running(self) -> bool:
        """Check if lite scheduler is running (in our thread)."""
        return (
            self._lite_thread is not None and
            self._lite_thread.is_alive()
        )

    def _start_lite_scheduler(self):
        """Start the lite scheduler in a background thread."""
        if self._is_lite_scheduler_running():
            logger.info("Lite scheduler already running")
            return

        logger.info("Starting lite scheduler...")

        from memory.lite_scheduler import LiteScheduler
        self._lite_scheduler = LiteScheduler()

        def run_scheduler():
            try:
                self._lite_scheduler.run(check_interval=1.0)
            except Exception as e:
                logger.error(f"Lite scheduler error: {e}")

        self._lite_thread = threading.Thread(
            target=run_scheduler,
            daemon=True,
            name="lite_scheduler"
        )
        self._lite_thread.start()
        logger.info("✓ Lite scheduler started")

    def _stop_lite_scheduler(self):
        """Stop the lite scheduler."""
        if not self._is_lite_scheduler_running():
            return

        logger.info("Stopping lite scheduler...")
        if self._lite_scheduler:
            self._lite_scheduler.stop()

        # Wait for thread to finish
        if self._lite_thread:
            self._lite_thread.join(timeout=5)

        self._lite_scheduler = None
        self._lite_thread = None
        logger.info("✓ Lite scheduler stopped")

    def _start_celery_beat(self) -> bool:
        """
        Start Celery Beat if not already running.

        Includes robust post-start health check: waits 3s and re-checks
        to avoid pgrep race conditions with crash-looping processes.
        Tracks consecutive failures for lockout.

        Returns:
            True if Celery Beat is running (started or already was)
        """
        if self._is_celery_running():
            logger.info("Celery Beat already running")
            self._celery_consecutive_failures = 0
            self._clear_redis_lockout()
            return True

        logger.info("Starting Celery Beat...")
        repo_root = Path(__file__).parent.parent
        logs_dir = repo_root / "logs"
        logs_dir.mkdir(exist_ok=True)

        try:
            subprocess.Popen(
                [
                    str(repo_root / ".venv" / "bin" / "celery"),
                    "-A", "memory.celery_config",
                    "beat",
                    "--loglevel=info",
                ],
                cwd=str(repo_root),
                stdout=open(logs_dir / "celery_beat.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True
            )

            # Robust health check: wait 3s then verify still alive
            # (catches processes that start but crash immediately)
            time.sleep(3)

            if self._is_celery_running():
                logger.info("✓ Celery Beat started and verified alive after 3s")
                self._celery_consecutive_failures = 0
                self._clear_redis_lockout()
                return True
            else:
                self._celery_consecutive_failures += 1
                lockout_seconds = 600  # 10 minutes
                self._celery_lockout_until = time.monotonic() + lockout_seconds
                self._set_redis_lockout()  # Persist to Redis
                logger.warning(
                    f"Celery Beat failed to start (attempt {self._celery_consecutive_failures}). "
                    f"Locked out for {lockout_seconds}s → forcing lite mode."
                )
                return False

        except Exception as e:
            self._celery_consecutive_failures += 1
            lockout_seconds = 600
            self._celery_lockout_until = time.monotonic() + lockout_seconds
            self._set_redis_lockout()  # Persist to Redis
            logger.error(
                f"Failed to start Celery Beat: {e} "
                f"(attempt {self._celery_consecutive_failures}, locked out {lockout_seconds}s)"
            )
            return False

    def _handle_mode_change(self, old_mode: str, new_mode: str) -> str:
        """
        Handle mode transition with clean handoff.

        Returns:
            The actual mode that was settled on (may differ from new_mode
            if Celery failed and we fell back to lite).
        """
        logger.info(f"Mode change detected: {old_mode} → {new_mode}")

        if new_mode == "heavy":
            # Transitioning TO heavy mode
            # 1. Stop lite scheduler
            self._stop_lite_scheduler()

            # 2. Verify Celery is running, fall back to lite if it fails
            celery_ok = self._is_celery_running()
            if not celery_ok:
                logger.info("Celery not running, starting...")
                celery_ok = self._start_celery_beat()

            if celery_ok:
                # 3. Notify successful handoff
                self._send_handoff_notification("lite", "heavy")
                return "heavy"
            else:
                # Celery failed — fall back to lite (never leave mansion unattended)
                logger.warning("Celery Beat failed during handoff → falling back to lite")
                self._start_lite_scheduler()
                self._record_state("lite", "lite_scheduler")
                self._send_handoff_notification("heavy", "lite")
                return "lite"

        elif new_mode == "lite":
            # Transitioning TO lite mode
            # Celery will stop naturally when RabbitMQ/Redis unavailable

            # 1. Start lite scheduler
            self._start_lite_scheduler()

            # 2. Notify
            self._send_handoff_notification("heavy", "lite")
            return "lite"

        return new_mode

    def _send_handoff_notification(self, from_mode: str, to_mode: str):
        """Send macOS notification about scheduler handoff."""
        try:
            from memory.mutual_heartbeat import send_macos_notification

            if to_mode == "heavy":
                send_macos_notification(
                    title="Context DNA: Scheduler Handoff",
                    message="Switching to Celery Beat (heavy mode)",
                    subtitle="Lite scheduler stopped",
                    sound="Purr"
                )
            else:
                send_macos_notification(
                    title="Context DNA: Scheduler Handoff",
                    message="Switching to Lite Scheduler (lite mode)",
                    subtitle="SQLite-backed scheduling active",
                    sound="Purr"
                )
        except Exception as e:
            logger.debug(f"Notification failed: {e}")

    def _record_state(self, mode: str, scheduler: str):
        """Record current scheduler state to file."""
        import json
        state = {
            "mode": mode,
            "active_scheduler": scheduler,
            "timestamp": datetime.now().isoformat(),
            "pid": os.getpid()
        }
        STATE_FILE.write_text(json.dumps(state, indent=2))

    def start(self):
        """
        Start the coordinator.

        Detects current mode and starts appropriate scheduler.
        Monitors for mode changes and handles handoffs.
        """
        if not self._acquire_singleton():
            return

        logger.info("=" * 60)
        logger.info("SCHEDULER COORDINATOR STARTING")
        logger.info("=" * 60)

        self._running = True

        # Initial mode detection
        self._current_mode = self._detect_mode()
        logger.info(f"Initial mode: {self._current_mode}")

        # Start appropriate scheduler
        if self._current_mode == "heavy":
            celery_ok = self._is_celery_running() or self._start_celery_beat()
            if celery_ok:
                self._record_state("heavy", "celery_beat")
            else:
                # Celery Beat failed — fall back to lite mode
                logger.warning("Heavy mode detected but Celery Beat failed → falling back to lite")
                self._current_mode = "lite"
                self._start_lite_scheduler()
                self._record_state("lite", "lite_scheduler")
        else:
            self._start_lite_scheduler()
            self._record_state("lite", "lite_scheduler")

        # Monitor loop
        while self._running:
            try:
                time.sleep(self._check_interval)

                if not self._running:
                    break

                # Check for mode change
                new_mode = self._detect_mode()

                if new_mode != self._current_mode:
                    actual_mode = self._handle_mode_change(self._current_mode, new_mode)
                    self._current_mode = actual_mode

                    scheduler = "celery_beat" if actual_mode == "heavy" else "lite_scheduler"
                    self._record_state(actual_mode, scheduler)

                # CPU watchdog — detect and auto-restart spinning scheduler
                self._check_cpu_watchdog()

                # Verify correct scheduler is running
                if self._current_mode == "lite" and not self._is_lite_scheduler_running():
                    logger.warning("Lite scheduler died, restarting...")
                    self._start_lite_scheduler()

                # Also verify: if mode is "heavy", check Celery is still alive
                if self._current_mode == "heavy" and not self._is_celery_running():
                    logger.warning("Celery Beat died while in heavy mode → falling back to lite")
                    self._current_mode = "lite"
                    self._celery_consecutive_failures += 1
                    self._celery_lockout_until = time.monotonic() + 600
                    self._set_redis_lockout()  # Persist to Redis
                    self._start_lite_scheduler()
                    self._record_state("lite", "lite_scheduler")

            except Exception as e:
                logger.error(f"Coordinator error: {e}")

        # Cleanup
        logger.info("Coordinator stopping...")
        self._stop_lite_scheduler()
        logger.info("Coordinator stopped")

    def stop(self):
        """Stop the coordinator."""
        self._running = False
        self._release_singleton()

    async def start_async(self):
        """Async version of start()."""
        if not self._acquire_singleton():
            return

        logger.info("=" * 60)
        logger.info("SCHEDULER COORDINATOR STARTING (ASYNC)")
        logger.info("=" * 60)

        self._running = True

        # Initial mode detection and scheduler start
        self._current_mode = self._detect_mode()
        logger.info(f"Initial mode: {self._current_mode}")

        if self._current_mode == "heavy":
            celery_ok = self._is_celery_running() or self._start_celery_beat()
            if celery_ok:
                self._record_state("heavy", "celery_beat")
            else:
                logger.warning("Heavy mode detected but Celery Beat failed → falling back to lite")
                self._current_mode = "lite"
                self._start_lite_scheduler()
                self._record_state("lite", "lite_scheduler")
        else:
            self._start_lite_scheduler()
            self._record_state("lite", "lite_scheduler")

        # Monitor loop
        while self._running:
            try:
                await asyncio.sleep(self._check_interval)

                if not self._running:
                    break

                new_mode = self._detect_mode()

                if new_mode != self._current_mode:
                    actual_mode = self._handle_mode_change(self._current_mode, new_mode)
                    self._current_mode = actual_mode

                    scheduler = "celery_beat" if actual_mode == "heavy" else "lite_scheduler"
                    self._record_state(actual_mode, scheduler)

                if self._current_mode == "lite" and not self._is_lite_scheduler_running():
                    logger.warning("Lite scheduler died, restarting...")
                    self._start_lite_scheduler()

                # Also verify: if mode is "heavy", check Celery is still alive
                if self._current_mode == "heavy" and not self._is_celery_running():
                    logger.warning("Celery Beat died while in heavy mode → falling back to lite")
                    self._current_mode = "lite"
                    self._celery_consecutive_failures += 1
                    self._celery_lockout_until = time.monotonic() + 600
                    self._set_redis_lockout()  # Persist to Redis
                    self._start_lite_scheduler()
                    self._record_state("lite", "lite_scheduler")

            except Exception as e:
                logger.error(f"Coordinator error: {e}")

        logger.info("Coordinator stopping...")
        self._stop_lite_scheduler()


def get_scheduler_state() -> dict:
    """Get current scheduler state from file."""
    import json
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            print(f"[WARN] Scheduler state file read failed: {e}")
    return {"mode": "unknown", "active_scheduler": "unknown"}


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()

        if cmd == "status":
            print("=" * 60)
            print("SCHEDULER COORDINATOR STATUS")
            print("=" * 60)

            state = get_scheduler_state()
            print(f"\nMode: {state.get('mode', 'unknown')}")
            print(f"Active Scheduler: {state.get('active_scheduler', 'unknown')}")
            if state.get('timestamp'):
                print(f"Last Update: {state['timestamp'][:19]}")

            # Check actual running state
            coordinator = SchedulerCoordinator()
            current_mode = coordinator._detect_mode()
            celery_running = coordinator._is_celery_running()

            print(f"\nActual Mode: {current_mode}")
            print(f"Celery Beat Running: {'Yes' if celery_running else 'No'}")

            # Check lite scheduler process
            try:
                result = subprocess.run(
                    ["pgrep", "-f", "lite_scheduler"],
                    capture_output=True,
                    timeout=5
                )
                lite_running = result.returncode == 0
            except Exception:
                lite_running = False

            print(f"Lite Scheduler Running: {'Yes' if lite_running else 'No'}")

            # Recommendation
            print("\nRecommendation:")
            if current_mode == "heavy" and celery_running:
                print("  ✓ Heavy mode with Celery Beat - correct configuration")
            elif current_mode == "lite" and not celery_running:
                print("  ✓ Lite mode - run 'python scheduler_coordinator.py' to start lite_scheduler")
            elif current_mode == "heavy" and not celery_running:
                print("  ⚠ Heavy mode but Celery not running - start with 'python mutual_heartbeat.py restart beat'")
            else:
                print("  ⚠ Run scheduler_coordinator.py to ensure correct scheduler is active")

        elif cmd == "handoff":
            # Force a mode check and handoff
            print("Checking mode and ensuring correct scheduler...")
            coordinator = SchedulerCoordinator()
            mode = coordinator._detect_mode()
            print(f"Detected mode: {mode}")

            if mode == "heavy":
                print("Heavy mode - ensuring Celery Beat is running...")
                if not coordinator._is_celery_running():
                    coordinator._start_celery_beat()
                print("✓ Celery Beat active")
            else:
                print("Lite mode - starting lite scheduler...")
                # Just verify - don't actually start (that's what 'run' is for)
                print("Run 'python scheduler_coordinator.py' to start lite scheduler")

        else:
            print(f"Unknown command: {cmd}")
            print("Commands: status, handoff")
            sys.exit(1)
    else:
        # Singleton guard now lives in start() — all callers protected
        coordinator = SchedulerCoordinator()

        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, stopping...")
            coordinator.stop()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            coordinator.start()
        except KeyboardInterrupt:
            coordinator.stop()
