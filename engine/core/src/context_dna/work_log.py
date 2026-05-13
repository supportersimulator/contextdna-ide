#!/usr/bin/env python3
"""
WORK DIALOGUE LOG - Self-Cleaning JSONL Work Stream

A persistent log of ALL work activity that:
1. Captures commands, outputs, dialogue, successes, errors
2. Self-cleans to stay under 5MB
3. Archives entries older than 7 days
4. Marks processed entries for cleanup
5. Provides query interface for objective success detection

This is the "memory stream" that feeds the Objective Success Detector.
Every command, every output, every user message flows through here.

FILE FORMAT:
    - JSONL (one JSON object per line)
    - Each entry has: entry_type, content, timestamp, source, area, metadata
    - Entries marked "processed: true" are cleaned up after consolidation

SIZE MANAGEMENT:
    - Max file size: 5MB
    - Auto-rotation when approaching limit
    - Processed entries cleaned up after 24h
    - Unprocessed entries kept for 7 days
    - Archives auto-delete after 7 days

Usage:
    from context_dna.work_log import WorkLog

    log = WorkLog()

    # Log various events
    log.command("docker-compose up -d")
    log.output("All containers healthy")
    log.user("that worked!")
    log.agent("Great, I'll move on to the next task")
    log.success("Deployed containers", area="docker")
    log.error("Connection refused", "Fixed by restarting service")

    # Query for objective success detection
    entries = log.get_recent(hours=24)
    successes = log.get_successes(hours=24)

    # Cleanup
    log.cleanup_processed()
    log.rotate_if_needed()
"""

import os
import json
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Generator
from dataclasses import dataclass, asdict, field


# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_LOG_DIR = Path.home() / ".context-dna" / "logs"
MAX_FILE_SIZE_MB = 5
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
PROCESSED_RETENTION_HOURS = 24
UNPROCESSED_RETENTION_DAYS = 7
ARCHIVE_RETENTION_DAYS = 7


# =============================================================================
# WORK LOG ENTRY
# =============================================================================

@dataclass
class WorkLogEntry:
    """A single entry in the work log."""
    entry_type: str  # 'command', 'output', 'dialogue', 'success', 'error', 'observation'
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    source: str = "system"  # 'user', 'agent', 'system'
    area: Optional[str] = None
    processed: bool = False
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "WorkLogEntry":
        """Create from dictionary."""
        return cls(
            entry_type=data.get("entry_type", "observation"),
            content=data.get("content", ""),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
            source=data.get("source", "system"),
            area=data.get("area"),
            processed=data.get("processed", False),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "WorkLogEntry":
        """Create from JSON string."""
        return cls.from_dict(json.loads(json_str))


# =============================================================================
# WORK LOG CLASS
# =============================================================================

class WorkLog:
    """
    Self-cleaning JSONL work log.

    Maintains a rolling log of all work activity, automatically cleaning
    up old and processed entries to stay within size limits.
    """

    def __init__(self, log_dir: Optional[Path] = None, project_name: str = "default"):
        """
        Initialize work log.

        Args:
            log_dir: Directory for log files. Defaults to ~/.context-dna/logs/
            project_name: Name for the log file (allows multiple projects)
        """
        self.log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.project_name = project_name
        self.log_file = self.log_dir / f"{project_name}_work.jsonl"
        self.archive_dir = self.log_dir / "archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # LOGGING METHODS
    # =========================================================================

    def log(self, entry: WorkLogEntry) -> bool:
        """
        Log an entry to the work log.

        Args:
            entry: WorkLogEntry to log

        Returns:
            True if logged successfully
        """
        try:
            # Rotate if needed before writing
            self._rotate_if_needed()

            with open(self.log_file, "a") as f:
                f.write(entry.to_json() + "\n")
            return True
        except Exception as e:
            print(f"Warning: Failed to log entry: {e}")
            return False

    def command(self, cmd: str, output: str = "", exit_code: int = 0, area: str = None) -> bool:
        """Log a command execution."""
        entry = WorkLogEntry(
            entry_type="command",
            content=cmd,
            source="agent",
            area=area,
            metadata={"output": output[:1000], "exit_code": exit_code}
        )
        return self.log(entry)

    def output(self, content: str, area: str = None) -> bool:
        """Log command/system output."""
        entry = WorkLogEntry(
            entry_type="output",
            content=content[:2000],  # Limit output size
            source="system",
            area=area
        )
        return self.log(entry)

    def observation(self, content: str, area: str = None) -> bool:
        """Log an observation (anything noteworthy)."""
        entry = WorkLogEntry(
            entry_type="observation",
            content=content,
            source="system",
            area=area
        )
        return self.log(entry)

    def user(self, message: str) -> bool:
        """Log user dialogue (important for success detection!)."""
        entry = WorkLogEntry(
            entry_type="dialogue",
            content=message,
            source="user"
        )
        return self.log(entry)

    def agent(self, message: str) -> bool:
        """Log agent dialogue."""
        entry = WorkLogEntry(
            entry_type="dialogue",
            content=message,
            source="agent"
        )
        return self.log(entry)

    def success(self, task: str, details: str = "", area: str = None) -> bool:
        """Log a successful task completion."""
        entry = WorkLogEntry(
            entry_type="success",
            content=task,
            source="agent",
            area=area,
            metadata={"details": details}
        )
        return self.log(entry)

    def error(self, error_msg: str, resolution: str = "", area: str = None) -> bool:
        """Log an error and its resolution."""
        entry = WorkLogEntry(
            entry_type="error",
            content=error_msg,
            source="system",
            area=area,
            metadata={"resolution": resolution}
        )
        return self.log(entry)

    # =========================================================================
    # QUERY METHODS
    # =========================================================================

    def get_recent(self, hours: int = 24, include_processed: bool = False) -> List[Dict]:
        """
        Get recent entries from the log.

        Args:
            hours: How far back to look
            include_processed: Include entries marked as processed

        Returns:
            List of entry dictionaries
        """
        cutoff = datetime.now() - timedelta(hours=hours)
        entries = []

        for entry in self._read_entries():
            # Parse timestamp
            try:
                ts = datetime.fromisoformat(entry.get("timestamp", ""))
                if ts < cutoff:
                    continue
            except:
                continue

            # Filter processed
            if not include_processed and entry.get("processed", False):
                continue

            entries.append(entry)

        return entries

    def get_successes(self, hours: int = 24) -> List[Dict]:
        """Get entries marked as success."""
        return [
            e for e in self.get_recent(hours, include_processed=True)
            if e.get("entry_type") == "success"
        ]

    def get_errors(self, hours: int = 24) -> List[Dict]:
        """Get entries marked as error."""
        return [
            e for e in self.get_recent(hours, include_processed=True)
            if e.get("entry_type") == "error"
        ]

    def get_commands(self, hours: int = 24) -> List[Dict]:
        """Get command entries."""
        return [
            e for e in self.get_recent(hours, include_processed=True)
            if e.get("entry_type") == "command"
        ]

    def get_user_dialogue(self, hours: int = 24) -> List[Dict]:
        """Get user dialogue entries (for success detection)."""
        return [
            e for e in self.get_recent(hours, include_processed=True)
            if e.get("entry_type") == "dialogue" and e.get("source") == "user"
        ]

    def search(self, query: str, hours: int = 168) -> List[Dict]:
        """
        Search entries by content.

        Args:
            query: Search string (case-insensitive)
            hours: How far back to search

        Returns:
            Matching entries
        """
        query_lower = query.lower()
        return [
            e for e in self.get_recent(hours, include_processed=True)
            if query_lower in e.get("content", "").lower()
        ]

    # =========================================================================
    # CLEANUP METHODS
    # =========================================================================

    def mark_processed(self, entry_timestamps: List[str]) -> int:
        """
        Mark entries as processed.

        Processed entries are eligible for cleanup.

        Args:
            entry_timestamps: Timestamps of entries to mark

        Returns:
            Number of entries marked
        """
        timestamps_set = set(entry_timestamps)
        marked = 0

        # Rewrite file with processed flags
        entries = list(self._read_entries())
        with open(self.log_file, "w") as f:
            for entry in entries:
                if entry.get("timestamp") in timestamps_set:
                    entry["processed"] = True
                    marked += 1
                f.write(json.dumps(entry) + "\n")

        return marked

    def cleanup_processed(self, retention_hours: int = PROCESSED_RETENTION_HOURS) -> int:
        """
        Remove processed entries older than retention period.

        Args:
            retention_hours: How long to keep processed entries

        Returns:
            Number of entries removed
        """
        cutoff = datetime.now() - timedelta(hours=retention_hours)
        kept = []
        removed = 0

        for entry in self._read_entries():
            # Keep unprocessed entries
            if not entry.get("processed", False):
                kept.append(entry)
                continue

            # Check age of processed entries
            try:
                ts = datetime.fromisoformat(entry.get("timestamp", ""))
                if ts >= cutoff:
                    kept.append(entry)
                else:
                    removed += 1
            except:
                kept.append(entry)  # Keep if timestamp is invalid

        # Rewrite file
        if removed > 0:
            with open(self.log_file, "w") as f:
                for entry in kept:
                    f.write(json.dumps(entry) + "\n")

        return removed

    def cleanup_old_unprocessed(self, retention_days: int = UNPROCESSED_RETENTION_DAYS) -> int:
        """
        Remove unprocessed entries older than retention period.

        Even unprocessed entries get cleaned up eventually.

        Args:
            retention_days: How long to keep unprocessed entries

        Returns:
            Number of entries removed
        """
        cutoff = datetime.now() - timedelta(days=retention_days)
        kept = []
        removed = 0

        for entry in self._read_entries():
            try:
                ts = datetime.fromisoformat(entry.get("timestamp", ""))
                if ts >= cutoff:
                    kept.append(entry)
                else:
                    removed += 1
            except:
                kept.append(entry)

        # Rewrite file
        if removed > 0:
            with open(self.log_file, "w") as f:
                for entry in kept:
                    f.write(json.dumps(entry) + "\n")

        return removed

    def cleanup_archives(self, retention_days: int = ARCHIVE_RETENTION_DAYS) -> int:
        """
        Remove archive files older than retention period.

        Returns:
            Number of archive files removed
        """
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed = 0

        for archive_file in self.archive_dir.glob(f"{self.project_name}_*.jsonl"):
            try:
                # Parse date from filename
                date_str = archive_file.stem.split("_")[-1]
                file_date = datetime.strptime(date_str, "%Y%m%d")
                if file_date < cutoff:
                    archive_file.unlink()
                    removed += 1
            except:
                continue

        return removed

    def full_cleanup(self) -> Dict:
        """
        Run full cleanup cycle.

        Returns:
            Dictionary with cleanup statistics
        """
        return {
            "processed_removed": self.cleanup_processed(),
            "old_removed": self.cleanup_old_unprocessed(),
            "archives_removed": self.cleanup_archives(),
            "rotated": self._rotate_if_needed(),
        }

    # =========================================================================
    # SIZE MANAGEMENT
    # =========================================================================

    def get_size_mb(self) -> float:
        """Get current log file size in MB."""
        if not self.log_file.exists():
            return 0.0
        return self.log_file.stat().st_size / (1024 * 1024)

    def get_entry_count(self) -> int:
        """Get number of entries in log."""
        count = 0
        for _ in self._read_entries():
            count += 1
        return count

    def get_stats(self) -> Dict:
        """Get log statistics."""
        entries = list(self._read_entries())

        # Count by type
        by_type = {}
        by_source = {}
        processed_count = 0

        for entry in entries:
            entry_type = entry.get("entry_type", "unknown")
            source = entry.get("source", "unknown")
            by_type[entry_type] = by_type.get(entry_type, 0) + 1
            by_source[source] = by_source.get(source, 0) + 1
            if entry.get("processed", False):
                processed_count += 1

        return {
            "file_path": str(self.log_file),
            "size_mb": self.get_size_mb(),
            "total_entries": len(entries),
            "processed": processed_count,
            "unprocessed": len(entries) - processed_count,
            "by_type": by_type,
            "by_source": by_source,
        }

    # =========================================================================
    # PRIVATE METHODS
    # =========================================================================

    def _read_entries(self) -> Generator[Dict, None, None]:
        """Read all entries from log file."""
        if not self.log_file.exists():
            return

        with open(self.log_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def _rotate_if_needed(self) -> bool:
        """
        Rotate log file if it exceeds size limit.

        Returns:
            True if rotation occurred
        """
        if not self.log_file.exists():
            return False

        size = self.log_file.stat().st_size
        if size < MAX_FILE_SIZE_BYTES:
            return False

        # Create archive filename
        date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"{self.project_name}_{date_str}.jsonl"
        archive_path = self.archive_dir / archive_name

        # Move current log to archive
        shutil.move(str(self.log_file), str(archive_path))

        # Create fresh log file
        self.log_file.touch()

        return True


# =============================================================================
# GLOBAL LOG INSTANCE
# =============================================================================

_work_log: Optional[WorkLog] = None


def get_work_log(project_name: str = "default") -> WorkLog:
    """Get the global work log instance."""
    global _work_log
    if _work_log is None:
        _work_log = WorkLog(project_name=project_name)
    return _work_log


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def log_command(cmd: str, output: str = "", exit_code: int = 0, area: str = None) -> bool:
    """Log a command execution."""
    return get_work_log().command(cmd, output, exit_code, area)


def log_output(content: str, area: str = None) -> bool:
    """Log system output."""
    return get_work_log().output(content, area)


def log_user(message: str) -> bool:
    """Log user dialogue."""
    return get_work_log().user(message)


def log_agent(message: str) -> bool:
    """Log agent dialogue."""
    return get_work_log().agent(message)


def log_success(task: str, details: str = "", area: str = None) -> bool:
    """Log a success."""
    return get_work_log().success(task, details, area)


def log_error(error_msg: str, resolution: str = "", area: str = None) -> bool:
    """Log an error."""
    return get_work_log().error(error_msg, resolution, area)


def get_recent_entries(hours: int = 24) -> List[Dict]:
    """Get recent log entries."""
    return get_work_log().get_recent(hours)


def run_cleanup() -> Dict:
    """Run full cleanup cycle."""
    return get_work_log().full_cleanup()


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Work Dialogue Log - Context DNA")
        print("")
        print("Self-cleaning JSONL log of all work activity.")
        print("")
        print("Commands:")
        print("  stats             - Show log statistics")
        print("  recent [hours]    - Show recent entries")
        print("  successes [hours] - Show success entries")
        print("  errors [hours]    - Show error entries")
        print("  cleanup           - Run full cleanup cycle")
        print("  search <query>    - Search entries")
        print("  test              - Log test entries")
        print("")
        print("The log captures commands, outputs, dialogue, successes, and errors.")
        print("It self-cleans to stay under 5MB.")
        sys.exit(0)

    cmd = sys.argv[1]
    log = get_work_log()

    if cmd == "stats":
        stats = log.get_stats()
        print("=== Work Log Statistics ===")
        print(f"File: {stats['file_path']}")
        print(f"Size: {stats['size_mb']:.2f} MB")
        print(f"Total entries: {stats['total_entries']}")
        print(f"Processed: {stats['processed']}")
        print(f"Unprocessed: {stats['unprocessed']}")
        print("")
        print("By type:")
        for t, count in stats['by_type'].items():
            print(f"  {t}: {count}")
        print("")
        print("By source:")
        for s, count in stats['by_source'].items():
            print(f"  {s}: {count}")

    elif cmd == "recent":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        entries = log.get_recent(hours)
        print(f"=== Recent Entries (last {hours}h) ===\n")
        for e in entries[-20:]:  # Last 20
            ts = e.get("timestamp", "")[:19]
            etype = e.get("entry_type", "?")[:8]
            source = e.get("source", "?")[:6]
            content = e.get("content", "")[:60]
            print(f"[{ts}] {etype:8} {source:6} {content}")

    elif cmd == "successes":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        successes = log.get_successes(hours)
        print(f"=== Successes (last {hours}h) ===\n")
        for s in successes:
            ts = s.get("timestamp", "")[:19]
            content = s.get("content", "")
            details = s.get("metadata", {}).get("details", "")
            print(f"[{ts}] {content}")
            if details:
                print(f"         {details[:60]}")

    elif cmd == "errors":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        errors = log.get_errors(hours)
        print(f"=== Errors (last {hours}h) ===\n")
        for e in errors:
            ts = e.get("timestamp", "")[:19]
            content = e.get("content", "")
            resolution = e.get("metadata", {}).get("resolution", "")
            print(f"[{ts}] {content}")
            if resolution:
                print(f"         Resolution: {resolution[:60]}")

    elif cmd == "cleanup":
        print("Running cleanup...")
        result = log.full_cleanup()
        print(f"Processed entries removed: {result['processed_removed']}")
        print(f"Old entries removed: {result['old_removed']}")
        print(f"Archives removed: {result['archives_removed']}")
        print(f"File rotated: {result['rotated']}")

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: search <query>")
            sys.exit(1)
        query = " ".join(sys.argv[2:])
        results = log.search(query)
        print(f"=== Search Results for '{query}' ===\n")
        for e in results[:20]:
            ts = e.get("timestamp", "")[:19]
            etype = e.get("entry_type", "?")
            content = e.get("content", "")[:80]
            print(f"[{ts}] {etype}: {content}")

    elif cmd == "test":
        print("Logging test entries...")
        log.command("echo 'test'", "test", 0, "testing")
        log.output("Test output")
        log.user("that worked!")
        log.success("Test task completed", "Details here", "testing")
        log.error("Test error", "Test resolution", "testing")
        print("Done. Check with: python work_log.py recent")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
