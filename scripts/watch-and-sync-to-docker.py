#!/usr/bin/env python3
"""watch-and-sync-to-docker.py.

RACE U3 — event-driven mirror of $HOME/dev/er-simulator-superrepo/ to
$HOME/Documents/er-simulator-superrepo/. Companion to
``scripts/sync-to-docker-mount.sh`` for users who do not want to wait for
post-commit hooks to mirror changes (the running ContextDNA stack reads from
Documents/, so latency = container staleness).

DESIGN
------
- FSEvents-backed (macOS) via the ``watchdog`` package — no polling. This
  satisfies the project invariant that watchers must be event-driven.
- Debounces a burst of filesystem events into a single rsync call (default
  250 ms) so e.g. ``rg`` writes or compile output do not trigger 800 syncs.
- Delegates the actual sync to ``sync-to-docker-mount.sh`` so the exclude
  list stays in one place (DRY).
- Zero silent failures: every run is appended to /tmp/dev-to-docker-sync.log
  via the shell script; this watcher prints to stdout and to a sibling log
  /tmp/watch-and-sync-to-docker.log so a launchd plist can capture it.
- Graceful degradation: if ``watchdog`` is not installed, exits 2 with a
  clear pip-install hint instead of crashing. We do NOT silently fall back
  to polling — polling would violate the invariant.

USAGE
-----
    pip install watchdog
    python3 scripts/watch-and-sync-to-docker.py

Optional env vars:
    DEV_TO_DOCKER_SRC      override watched dir (default: ~/dev/er-simulator-superrepo)
    DEV_TO_DOCKER_DEBOUNCE seconds to coalesce events (default: 0.25)
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
SRC = Path(os.environ.get("DEV_TO_DOCKER_SRC", HOME / "dev" / "er-simulator-superrepo"))
SYNC_SCRIPT = SRC / "scripts" / "sync-to-docker-mount.sh"
DEBOUNCE = float(os.environ.get("DEV_TO_DOCKER_DEBOUNCE", "0.25"))
WATCHER_LOG = Path("/tmp/watch-and-sync-to-docker.log")

# Same exclude prefixes as the rsync script — we use these to drop noisy
# events before they wake the debouncer.
IGNORE_SUBSTRINGS = (
    "/.git/",
    "/.git-rewrite/",
    "/.venv/",
    "/__pycache__/",
    "/node_modules/",
    "/.claude/worktrees/",
    "/.pytest_cache/",
    "/.mypy_cache/",
    "/.ruff_cache/",
    "/.fleet-state/",
    "/.DS_Store",
)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    try:
        with WATCHER_LOG.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        # /tmp should always be writable; if not, stdout is enough.
        pass


def should_ignore(path: str) -> bool:
    for needle in IGNORE_SUBSTRINGS:
        if needle in path:
            return True
    return False


def run_sync() -> None:
    if not SYNC_SCRIPT.exists():
        log(f"ERROR sync script missing: {SYNC_SCRIPT}")
        return
    cmd = ["bash", str(SYNC_SCRIPT), "--quiet"]
    log("SYNC " + shlex.join(cmd))
    rc = subprocess.call(cmd)
    log(f"SYNC done rc={rc}")


class Debouncer:
    """Coalesces a burst of events into one trailing call."""

    def __init__(self, fn, delay: float) -> None:
        self._fn = fn
        self._delay = delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def trigger(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        try:
            self._fn()
        except Exception as exc:  # noqa: BLE001 — log, never silently swallow
            log(f"ERROR sync raised: {exc!r}")


def main() -> int:
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        sys.stderr.write(
            "watchdog not installed. Install with:\n"
            "    pip install watchdog\n"
            "Refusing to fall back to polling (would violate event-driven invariant).\n"
        )
        return 2

    if not SRC.is_dir():
        log(f"ERROR watch source missing: {SRC}")
        return 1

    debouncer = Debouncer(run_sync, DEBOUNCE)

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):  # type: ignore[override]
            path = getattr(event, "src_path", "") or ""
            if should_ignore(path):
                return
            debouncer.trigger()

    observer = Observer()
    observer.schedule(Handler(), str(SRC), recursive=True)
    observer.start()
    log(f"WATCH started src={SRC} debounce_s={DEBOUNCE}")

    # Initial sync so we converge on startup, not just on first event.
    debouncer.trigger()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log("WATCH interrupted, stopping observer")
    finally:
        observer.stop()
        observer.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
