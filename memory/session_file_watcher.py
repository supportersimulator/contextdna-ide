#!/usr/bin/env python3
"""
SESSION FILE WATCHER — Real-time dialogue awareness via FSEvents.

Watches Claude Code session JSONL files for new messages using macOS FSEvents
(via watchdog). When a new line is appended, parses it and feeds it to
DialogueMirror + Redis pub/sub for near-real-time butler awareness.

Architecture:
  Claude Code appends to .jsonl (append-only)
        ↓ <100ms (FSEvents kernel callback)
  SessionFileWatcher.on_modified()
        ↓ <50ms
  Parse new JSONL lines (incremental, offset-tracked)
        ↓ <50ms
  DialogueMirror.mirror_message() + Redis publish
        ↓ <50ms
  agent_service Redis subscriber receives event
        ↓
  Fresh context available for NEXT webhook injection

Total latency: <500ms (vs 120s batch polling)

Created: February 7, 2026
Purpose: Real-time dialogue mirror — Alfred handing Batman tools mid-task
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("context_dna.session_watcher")

# Claude Code session files location — reuse session_historian's detection
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
try:
    from memory.session_historian import SUPERREPO_KEY
except ImportError:
    # Fallback if session_historian unavailable (standalone use)
    SUPERREPO_KEY = str(Path(__file__).resolve().parent.parent).replace("/", "-")
SESSION_DIR = CLAUDE_PROJECTS_DIR / SUPERREPO_KEY

# State persistence
STATE_FILE = Path.home() / ".context-dna" / ".session_watcher_offsets.json"

# Debounce rapid writes (Claude appends multiple lines quickly)
DEBOUNCE_MS = 150


class SessionFileWatcher:
    """Watches Claude Code JSONL session files for real-time dialogue awareness."""

    def __init__(self, session_dir: Optional[Path] = None):
        self.session_dir = session_dir or SESSION_DIR
        self._offsets: dict[str, int] = {}  # filename → byte offset
        self._observer = None
        self._running = False
        self._debounce_timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._redis = None
        self._mirror = None
        self._load_offsets()

    def _load_offsets(self):
        """Load persisted file offsets."""
        try:
            if STATE_FILE.exists():
                self._offsets = json.loads(STATE_FILE.read_text())
        except Exception:
            self._offsets = {}

    def _save_offsets(self):
        """Persist file offsets."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self._offsets))
        except Exception:
            pass

    def _get_redis(self):
        """Lazy Redis connection."""
        if self._redis is not None:
            return self._redis
        try:
            import redis
            self._redis = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
            self._redis.ping()
            return self._redis
        except Exception:
            self._redis = None
            return None

    def _get_mirror(self):
        """Lazy DialogueMirror instance."""
        if self._mirror is not None:
            return self._mirror
        try:
            from memory.dialogue_mirror import DialogueMirror
            self._mirror = DialogueMirror()
            return self._mirror
        except Exception:
            return None

    def start(self):
        """Start watching session directory with FSEvents."""
        if self._running:
            return

        if not self.session_dir.exists():
            logger.warning(f"Session dir not found: {self.session_dir}")
            return

        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            watcher = self

            class _Handler(FileSystemEventHandler):
                def on_modified(self, event):
                    if event.is_directory:
                        return
                    if event.src_path.endswith(".jsonl"):
                        watcher._debounced_process(event.src_path)

                def on_created(self, event):
                    if event.is_directory:
                        return
                    if event.src_path.endswith(".jsonl"):
                        watcher._debounced_process(event.src_path)

            self._observer = Observer()
            self._observer.schedule(_Handler(), str(self.session_dir), recursive=False)
            self._observer.daemon = True
            self._observer.start()
            self._running = True
            logger.info(f"Session file watcher started: {self.session_dir}")

        except ImportError:
            logger.error("watchdog not installed — pip install watchdog")
        except Exception as e:
            logger.error(f"Failed to start watcher: {e}")

    def stop(self):
        """Stop the file watcher."""
        self._running = False
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        self._save_offsets()
        logger.info("Session file watcher stopped")

    def _debounced_process(self, filepath: str):
        """Debounce rapid file modifications (150ms)."""
        with self._lock:
            existing = self._debounce_timers.get(filepath)
            if existing:
                existing.cancel()

            timer = threading.Timer(
                DEBOUNCE_MS / 1000.0,
                self._process_new_lines,
                args=[filepath]
            )
            timer.daemon = True
            self._debounce_timers[filepath] = timer
            timer.start()

    def _process_new_lines(self, filepath: str):
        """Read new lines from a JSONL file since last offset."""
        filename = os.path.basename(filepath)
        offset = self._offsets.get(filename, 0)

        try:
            file_size = os.path.getsize(filepath)
            if file_size <= offset:
                return  # No new data

            with open(filepath, "r", encoding="utf-8") as f:
                f.seek(offset)
                new_data = f.read()
                new_offset = f.tell()

            # Update offset
            self._offsets[filename] = new_offset

            # Parse new lines
            new_messages = 0
            for line in new_data.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    self._handle_entry(entry, filename)
                    new_messages += 1
                except json.JSONDecodeError:
                    continue

            if new_messages > 0:
                self._save_offsets()
                logger.debug(f"Processed {new_messages} new messages from {filename}")

        except Exception as e:
            logger.error(f"Error processing {filepath}: {e}")

    def _handle_entry(self, entry: dict, filename: str):
        """Handle a single JSONL entry — feed to mirror + publish to Redis."""
        entry_type = entry.get("type", "")
        message = entry.get("message", {})
        timestamp = entry.get("timestamp", "")
        session_id = entry.get("sessionId", filename.replace(".jsonl", ""))

        # Only process user and assistant messages
        if entry_type not in ("user", "assistant"):
            return

        role = "unknown"
        content = ""

        if isinstance(message, dict):
            role = message.get("role", entry_type)
            msg_content = message.get("content", "")
            if isinstance(msg_content, str):
                content = msg_content
            elif isinstance(msg_content, list):
                # Multi-part content (text + tool results)
                text_parts = []
                for part in msg_content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                        elif "text" in part:
                            text_parts.append(str(part["text"])[:200])
                content = " ".join(text_parts)

                # Permission detection: scan for tool_use / tool_result blocks
                self._detect_permissions(msg_content, session_id)

        if not content or len(content) < 5:
            return

        # Truncate for efficiency (full content stored elsewhere)
        content_truncated = content[:500]

        # 1. Feed to DialogueMirror
        mirror = self._get_mirror()
        if mirror:
            try:
                from memory.dialogue_mirror import MessageRole, DialogueSource
                mirror_role = MessageRole.AARON if role == "user" else MessageRole.ATLAS
                mirror.mirror_message(
                    session_id=session_id,
                    role=mirror_role,
                    content=content_truncated,
                    source=DialogueSource.UNKNOWN,
                )
            except Exception as e:
                logger.debug(f"Mirror feed error: {e}")

        # 2. Publish to Redis for real-time awareness
        r = self._get_redis()
        if r:
            try:
                event = json.dumps({
                    "type": "dialogue",
                    "role": role,
                    "content": content_truncated,
                    "session_id": session_id,
                    "timestamp": timestamp,
                    "source": "session_file_watcher",
                })
                r.publish("session:dialogue:new", event)
            except Exception:
                pass

    def _detect_permissions(self, content_blocks: list, session_id: str):
        """Scan content blocks for tool_use/tool_result — feed permission assistant."""
        try:
            from memory.permission_assistant import get_permission_assistant
            pa = get_permission_assistant()

            for block in content_blocks:
                if not isinstance(block, dict):
                    continue

                block_type = block.get("type", "")

                if block_type == "tool_use":
                    tool_id = block.get("id", "")
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    if tool_id and tool_name:
                        pa.record_tool_use(tool_id, tool_name, tool_input, session_id)

                elif block_type == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    if tool_use_id:
                        pa.record_tool_result(tool_use_id)
        except Exception as e:
            logger.debug(f"Permission detection error: {e}")

    def health(self) -> dict:
        """Health check for monitoring. Actively pings Redis."""
        redis_ok = False
        if self._redis is not None:
            try:
                self._redis.ping()
                redis_ok = True
            except Exception:
                self._redis = None  # Force reconnect on next publish
        return {
            "running": self._running,
            "watched_dir": str(self.session_dir),
            "tracked_files": len(self._offsets),
            "redis_connected": redis_ok,
            "mirror_connected": self._mirror is not None,
        }


# Module-level singleton
_watcher: Optional[SessionFileWatcher] = None
_watcher_lock = threading.Lock()


def get_session_watcher() -> SessionFileWatcher:
    """Get or create the singleton session file watcher."""
    global _watcher
    if _watcher is not None:
        return _watcher
    with _watcher_lock:
        if _watcher is not None:
            return _watcher
        _watcher = SessionFileWatcher()
        return _watcher


def start_session_watcher() -> bool:
    """Start the session file watcher. Returns True if started successfully."""
    watcher = get_session_watcher()
    watcher.start()
    return watcher._running


def stop_session_watcher():
    """Stop the session file watcher."""
    global _watcher
    if _watcher:
        _watcher.stop()
        _watcher = None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)

    if len(sys.argv) > 1 and sys.argv[1] == "health":
        w = get_session_watcher()
        print(json.dumps(w.health(), indent=2))
    else:
        print("Starting session file watcher (Ctrl+C to stop)...")
        w = get_session_watcher()
        w.start()
        try:
            while True:
                time.sleep(10)
                print(f"Health: {json.dumps(w.health())}")
        except KeyboardInterrupt:
            w.stop()
            print("Stopped.")
