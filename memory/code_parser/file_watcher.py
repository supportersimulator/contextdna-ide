"""
File Watcher for Architectural Awareness

Monitors the codebase for structural changes and triggers graph updates.
Uses watchdog for efficient file system monitoring.
"""

import os
import sys
import time
import threading
from pathlib import Path
from typing import Callable, List, Optional, Set
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    Observer = None
    FileSystemEventHandler = object
    FileSystemEvent = None


class ArchitectureChangeHandler(FileSystemEventHandler):
    """
    Handles file system events and triggers architecture graph updates.
    Implements debouncing to avoid excessive updates during rapid changes.
    """

    # File extensions we care about
    WATCHED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx"}

    # Patterns to ignore
    IGNORE_PATTERNS = {
        "__pycache__",
        "node_modules",
        ".git",
        ".venv",
        "venv",
        ".next",
        "build",
        "dist",
        "coverage",
        ".pytest_cache",
    }

    def __init__(
        self,
        on_change: Callable[[List[str]], None],
        debounce_seconds: float = 2.0,
    ):
        """
        Initialize the change handler.

        Args:
            on_change: Callback function when files change (receives list of paths)
            debounce_seconds: Wait time before triggering callback (batches changes)
        """
        super().__init__()
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds

        # Track pending changes
        self._pending_changes: Set[str] = set()
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

    def _should_process(self, path: str) -> bool:
        """Check if we should process this file."""
        path_obj = Path(path)

        # Check extension
        if path_obj.suffix.lower() not in self.WATCHED_EXTENSIONS:
            return False

        # Check for ignored patterns
        path_str = str(path_obj)
        for pattern in self.IGNORE_PATTERNS:
            if pattern in path_str:
                return False

        return True

    def _schedule_callback(self):
        """Schedule the callback after debounce period."""
        with self._lock:
            # Cancel existing timer
            if self._timer is not None:
                self._timer.cancel()

            # Schedule new timer
            self._timer = threading.Timer(
                self.debounce_seconds,
                self._fire_callback,
            )
            self._timer.start()

    def _fire_callback(self):
        """Fire the callback with accumulated changes."""
        with self._lock:
            if self._pending_changes:
                changes = list(self._pending_changes)
                self._pending_changes.clear()
                self._timer = None

        # Call outside lock
        if changes:
            try:
                self.on_change(changes)
            except Exception as e:
                print(f"Error in change callback: {e}")

    def on_modified(self, event: FileSystemEvent):
        """Handle file modification."""
        if event.is_directory:
            return

        if self._should_process(event.src_path):
            with self._lock:
                self._pending_changes.add(event.src_path)
            self._schedule_callback()

    def on_created(self, event: FileSystemEvent):
        """Handle file creation."""
        if event.is_directory:
            return

        if self._should_process(event.src_path):
            with self._lock:
                self._pending_changes.add(event.src_path)
            self._schedule_callback()

    def on_deleted(self, event: FileSystemEvent):
        """Handle file deletion."""
        if event.is_directory:
            return

        if self._should_process(event.src_path):
            with self._lock:
                self._pending_changes.add(event.src_path)
            self._schedule_callback()

    def on_moved(self, event: FileSystemEvent):
        """Handle file move/rename."""
        if event.is_directory:
            return

        # Track both source and destination
        if self._should_process(event.src_path):
            with self._lock:
                self._pending_changes.add(event.src_path)

        if hasattr(event, 'dest_path') and self._should_process(event.dest_path):
            with self._lock:
                self._pending_changes.add(event.dest_path)

        if self._pending_changes:
            self._schedule_callback()


class ArchitectureWatcher:
    """
    Watches the codebase for structural changes and triggers graph rebuilds.
    """

    def __init__(
        self,
        repo_root: str,
        on_change: Callable[[List[str]], None] = None,
        debounce_seconds: float = 2.0,
    ):
        """
        Initialize the watcher.

        Args:
            repo_root: Root directory to watch
            on_change: Callback when files change
            debounce_seconds: Debounce period for batching changes
        """
        if not WATCHDOG_AVAILABLE:
            raise ImportError(
                "watchdog package not installed. "
                "Install with: pip install watchdog"
            )

        self.repo_root = Path(repo_root).resolve()
        self.on_change = on_change or self._default_callback
        self.debounce_seconds = debounce_seconds

        self._observer: Optional[Observer] = None
        self._running = False

    def _default_callback(self, changed_files: List[str]):
        """Default callback - just prints changes."""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Architecture changes detected:")
        for f in changed_files[:10]:  # Limit output
            rel_path = Path(f).relative_to(self.repo_root) if f.startswith(str(self.repo_root)) else f
            print(f"  - {rel_path}")
        if len(changed_files) > 10:
            print(f"  ... and {len(changed_files) - 10} more")

    def start(self):
        """Start watching for changes."""
        if self._running:
            return

        handler = ArchitectureChangeHandler(
            on_change=self.on_change,
            debounce_seconds=self.debounce_seconds,
        )

        self._observer = Observer()
        self._observer.schedule(handler, str(self.repo_root), recursive=True)
        self._observer.start()
        self._running = True

        print(f"Watching for architecture changes in: {self.repo_root}")

    def stop(self):
        """Stop watching."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        self._running = False

    def is_running(self) -> bool:
        """Check if watcher is running."""
        return self._running

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()
        return False


class ArchitectureWatcherWithGraphUpdate:
    """
    Combines file watching with automatic graph updates.
    """

    def __init__(
        self,
        repo_root: str,
        on_graph_update: Callable = None,
        debounce_seconds: float = 2.0,
    ):
        """
        Initialize watcher with graph builder.

        Args:
            repo_root: Root directory to watch
            on_graph_update: Callback with updated graph
            debounce_seconds: Debounce period
        """
        self.repo_root = repo_root
        self.on_graph_update = on_graph_update
        self.debounce_seconds = debounce_seconds

        # Lazy import to avoid circular dependency
        self._graph_builder = None
        self._watcher = None

    def _get_graph_builder(self):
        """Lazy load graph builder."""
        if self._graph_builder is None:
            from memory.code_parser.graph_builder import ArchitectureGraphBuilder
            self._graph_builder = ArchitectureGraphBuilder(self.repo_root)
        return self._graph_builder

    def _handle_changes(self, changed_files: List[str]):
        """Handle file changes by updating graph."""
        print(f"\n[Architecture] Detected changes in {len(changed_files)} file(s)")

        try:
            # Rebuild graph (incremental update)
            builder = self._get_graph_builder()
            graph = builder.build_graph(force_rebuild=False)

            print(f"[Architecture] Graph updated: {len(graph.nodes)} nodes, {len(graph.edges)} edges")

            if graph.changed_nodes:
                print(f"[Architecture] Changed nodes: {len(graph.changed_nodes)}")

            # Notify callback
            if self.on_graph_update:
                self.on_graph_update(graph)

        except Exception as e:
            print(f"[Architecture] Error updating graph: {e}")

    def start(self):
        """Start watching and updating."""
        if not WATCHDOG_AVAILABLE:
            print("[Architecture] watchdog not available - polling mode not implemented")
            return

        self._watcher = ArchitectureWatcher(
            repo_root=self.repo_root,
            on_change=self._handle_changes,
            debounce_seconds=self.debounce_seconds,
        )
        self._watcher.start()

    def stop(self):
        """Stop watching."""
        if self._watcher:
            self._watcher.stop()

    def get_current_graph(self):
        """Get the current graph (builds if needed)."""
        return self._get_graph_builder().build_graph()


def watch_architecture(
    repo_root: str = None,
    on_update: Callable = None,
    debounce: float = 2.0,
) -> ArchitectureWatcherWithGraphUpdate:
    """
    Convenience function to start watching architecture changes.

    Args:
        repo_root: Repository root directory
        on_update: Callback when graph updates
        debounce: Debounce period in seconds

    Returns:
        ArchitectureWatcherWithGraphUpdate instance
    """
    repo_root = repo_root or str(Path.cwd())
    watcher = ArchitectureWatcherWithGraphUpdate(
        repo_root=repo_root,
        on_graph_update=on_update,
        debounce_seconds=debounce,
    )
    watcher.start()
    return watcher


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Watch for architecture changes")
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parent.parent.parent),
        help="Repository root directory",
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=2.0,
        help="Debounce period in seconds",
    )

    args = parser.parse_args()

    def on_update(graph):
        print(f"\nGraph summary:")
        for key, value in graph.stats.items():
            print(f"  {key}: {value}")

    print(f"Starting architecture watcher for: {args.repo}")
    print("Press Ctrl+C to stop\n")

    watcher = watch_architecture(
        repo_root=args.repo,
        on_update=on_update,
        debounce=args.debounce,
    )

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping watcher...")
        watcher.stop()
