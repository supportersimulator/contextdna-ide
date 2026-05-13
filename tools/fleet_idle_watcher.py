#!/usr/bin/env python3
"""Event-driven idle watcher using `watchdog` FSEvents.

Replaces the 60s poll loop in FleetNerveDaemon.start_idle_watcher with a
filesystem-events-driven watcher on `~/.claude/projects/`. Each modify on a
`.jsonl` file (Claude Code session transcript) updates `_last_activity`. A
single timer fires the idle-triggered action when IDLE_THRESHOLD elapses
with no events.

Import-time fallback: if `watchdog` is not installed, `start_event_idle_watcher`
returns False and the caller should fall back to the polling loop.

Contract for the daemon:
  - `daemon._last_activity` float attribute is updated on every file event.
  - `daemon._check_idle()` is invoked after IDLE_THRESHOLD seconds of silence,
    then the timer is re-armed.

Wins vs poll:
  - 0s median latency (event) vs 30s average (60s poll)
  - Zero CPU when idle (kqueue/FSEvents under watchdog)
  - Immediate "activity" refresh without needing to wait for next tick
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("fleet_nerve.idle_watcher")


def start_event_idle_watcher(
    daemon: Any,
    *,
    watch_dir: Optional[Path] = None,
    idle_threshold_s: float = 300.0,
    check_callback: Optional[Callable[[], None]] = None,
) -> bool:
    """Start an event-driven idle watcher for `daemon`.

    Args:
        daemon: FleetNerveDaemon-shaped object with `_last_activity` attr and
            `_check_idle()` method.
        watch_dir: Directory to observe recursively. Defaults to
            `~/.claude/projects/` which is where Claude Code writes session
            transcripts.
        idle_threshold_s: Seconds of no events before `_check_idle` fires.
        check_callback: Override for the idle callback (test hook). Defaults
            to `daemon._check_idle`.

    Returns:
        True if watchdog started successfully, False if watchdog is unavailable
        or the watch directory is missing (caller should fall back to poll).
    """
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        logger.info("watchdog not installed — falling back to poll-based idle watcher")
        return False

    wd = Path(watch_dir) if watch_dir is not None else (Path.home() / ".claude" / "projects")
    if not wd.exists():
        logger.info(f"watchdog idle: watch dir {wd} missing — falling back to poll")
        return False

    cb = check_callback or getattr(daemon, "_check_idle", None)
    if cb is None:
        logger.warning("watchdog idle: daemon has no _check_idle callback")
        return False

    state_lock = threading.Lock()
    timer_holder: dict[str, Optional[threading.Timer]] = {"t": None}
    stopped = threading.Event()

    def _arm_timer() -> None:
        """(Re)arm the idle-fire timer under state_lock."""
        if stopped.is_set():
            return
        old = timer_holder["t"]
        if old is not None:
            old.cancel()
        t = threading.Timer(idle_threshold_s, _on_idle_elapsed)
        t.daemon = True
        timer_holder["t"] = t
        t.start()

    def _on_idle_elapsed() -> None:
        """Timer fired → threshold elapsed with no events → call idle check."""
        if stopped.is_set():
            return
        try:
            cb()
        except Exception as e:
            logger.debug(f"Idle callback error: {e}")
        # Re-arm so we keep firing on every threshold window of silence.
        with state_lock:
            _arm_timer()

    def _on_activity() -> None:
        """FS event observed → bump last_activity and re-arm the timer."""
        now = time.time()
        try:
            daemon._last_activity = now
        except Exception as _zsf_e:
            # EEE4/ZSF: daemon may have been torn down between event arrival
            # and assignment — log so a flood is observable, but don't crash
            # the filesystem-event hot path.
            logger.warning(
                "idle_watcher activity bump failed: %s: %s",
                type(_zsf_e).__name__, _zsf_e,
            )
        with state_lock:
            _arm_timer()

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event: Any) -> None:
            if getattr(event, "is_directory", False):
                return
            src = getattr(event, "src_path", "") or ""
            if src.endswith(".jsonl"):
                _on_activity()

        def on_created(self, event: Any) -> None:
            if getattr(event, "is_directory", False):
                return
            src = getattr(event, "src_path", "") or ""
            if src.endswith(".jsonl"):
                _on_activity()

    observer = Observer()
    observer.daemon = True
    observer.schedule(_Handler(), str(wd), recursive=True)
    observer.start()

    with state_lock:
        _arm_timer()

    # Stash the observer on the daemon so tests / graceful-shutdown can stop it.
    daemon._idle_observer = observer
    daemon._idle_watcher_stop = stopped

    def _stop() -> None:
        stopped.set()
        with state_lock:
            t = timer_holder["t"]
            if t is not None:
                t.cancel()
                timer_holder["t"] = None
        try:
            observer.stop()
            observer.join(timeout=2.0)
        except Exception as e:
            logger.debug(f"Observer stop error: {e}")

    daemon._idle_watcher_stop_fn = _stop
    logger.info(
        f"Event-driven idle watcher started (watch={wd}, threshold={int(idle_threshold_s)}s)"
    )
    return True


__all__ = ["start_event_idle_watcher"]
