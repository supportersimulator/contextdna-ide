#!/usr/bin/env python3
"""
WAL Mode Enforcer for All SQLite Databases

Discovers all .db files in known locations and enables WAL (Write-Ahead Logging)
journal mode. WAL prevents corruption under concurrent access (proven by
.observability.db corruption in Session 4).

Idempotent: safe to run multiple times. Already-WAL databases are left unchanged.

Usage:
    # CLI
    python memory/enable_wal_all.py              # Enable WAL on all DBs
    python memory/enable_wal_all.py --dry-run     # Report without changing
    python memory/enable_wal_all.py --checkpoint   # WAL checkpoint (TRUNCATE) all
    python memory/enable_wal_all.py --json         # Machine-readable JSON output

    # As a function
    from memory.enable_wal_all import enable_wal_all_dbs
    results = enable_wal_all_dbs()

    # As a scheduler job
    from memory.enable_wal_all import run_enable_wal_all
    success, msg = run_enable_wal_all()

Known DB locations (recursive scan):
    ~/.context-dna/       (primary context DNA store, incl. major_skills/)
    memory/               (repo-local memory DBs)

Startup job: Run at service init to ensure all DBs are WAL-protected
before concurrent access. Add to launchd plist or Context DNA init sequence.

Skip patterns: _corrupt_backup, _backup.db, .db-shm.bak, .db-wal.bak
"""

import json as json_mod
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# Known locations to scan RECURSIVELY for .db files
DB_LOCATIONS = [
    Path.home() / ".context-dna",
    Path(__file__).parent,  # memory/
]

# Skip patterns (known corrupt files, backup artifacts)
SKIP_PATTERNS = [
    "_corrupt_backup",
    "_backup.db",
    ".db-shm.bak",
    ".db-wal.bak",
]


@dataclass
class WALResult:
    """Result of WAL enable operation on a single DB."""
    path: str
    real_path: str
    previous_mode: str
    current_mode: str
    action: str  # "enabled_wal", "already_wal", "initialized_wal", "error", "checkpoint", "skip_corrupt", "skip_locked", "skip_missing"
    integrity: str = "unknown"
    error: str = ""
    size_bytes: int = 0
    is_symlink: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "real_path": self.real_path,
            "previous_mode": self.previous_mode,
            "current_mode": self.current_mode,
            "action": self.action,
            "integrity": self.integrity,
            "error": self.error,
            "size_bytes": self.size_bytes,
            "is_symlink": self.is_symlink,
        }


@dataclass
class WALReport:
    """Summary of WAL enable sweep."""
    results: List[WALResult] = field(default_factory=list)
    total_dbs: int = 0
    already_wal: int = 0
    newly_enabled: int = 0
    initialized: int = 0
    skipped: int = 0
    errors: int = 0
    checkpointed: int = 0

    @property
    def summary(self) -> str:
        parts = [
            f"total={self.total_dbs}",
            f"wal={self.already_wal}",
            f"enabled={self.newly_enabled}",
        ]
        if self.initialized:
            parts.append(f"initialized={self.initialized}")
        if self.skipped:
            parts.append(f"skipped={self.skipped}")
        if self.errors:
            parts.append(f"errors={self.errors}")
        if self.checkpointed:
            parts.append(f"checkpointed={self.checkpointed}")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "total_dbs": self.total_dbs,
            "already_wal": self.already_wal,
            "newly_enabled": self.newly_enabled,
            "initialized": self.initialized,
            "skipped": self.skipped,
            "errors": self.errors,
            "checkpointed": self.checkpointed,
            "results": [r.to_dict() for r in self.results],
        }


def _should_skip(db_path: str) -> bool:
    """Check if path matches a skip pattern (corrupt backups, WAL artifacts)."""
    for pattern in SKIP_PATTERNS:
        if pattern in db_path:
            return True
    return False


def discover_db_files() -> List[str]:
    """
    Discover all .db files in known locations (recursive).

    Follows symlinks for scanning. Deduplicates by resolved real path
    but returns the original path for display. Skips known corrupt/backup files.
    """
    seen_real_paths = set()
    db_files = []

    for location in DB_LOCATIONS:
        location = Path(os.path.abspath(str(location)))
        if not location.is_dir():
            continue

        # Recursive glob for all .db files (including hidden)
        for db_path in sorted(location.rglob("*.db")):
            db_str = str(db_path)

            # Skip known corrupt/backup files
            if _should_skip(db_str):
                continue

            # Resolve symlinks for dedup
            real_path = str(db_path.resolve())
            if real_path in seen_real_paths:
                continue
            seen_real_paths.add(real_path)
            db_files.append(db_str)

    return db_files


def _enable_wal_single(db_path: str, dry_run: bool = False, checkpoint: bool = False) -> WALResult:
    """
    Enable WAL mode on a single SQLite database.

    Process:
    1. Check file existence and size
    2. For empty files: initialize with WAL (future-proofing)
    3. For non-empty: check integrity, then enable WAL if not already set
    4. Optionally run WAL checkpoint (TRUNCATE)
    """
    is_symlink = os.path.islink(db_path)
    real_path = os.path.realpath(db_path)

    # Check existence (follow symlink)
    if not os.path.exists(real_path):
        return WALResult(
            path=db_path, real_path=real_path,
            previous_mode="n/a", current_mode="n/a",
            action="skip_missing", is_symlink=is_symlink,
        )

    size = os.path.getsize(real_path)

    # Empty files: initialize with WAL for future-proofing
    if size == 0:
        if dry_run:
            return WALResult(
                path=db_path, real_path=real_path,
                previous_mode="empty", current_mode="empty",
                action="would_init_wal", is_symlink=is_symlink, size_bytes=0,
            )

        conn = None
        try:
            conn = sqlite3.connect(db_path, timeout=10)
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            new_size = os.path.getsize(real_path)
            return WALResult(
                path=db_path, real_path=real_path,
                previous_mode="empty", current_mode=mode,
                action="initialized_wal", integrity="ok",
                is_symlink=is_symlink, size_bytes=new_size,
            )
        except Exception as e:
            return WALResult(
                path=db_path, real_path=real_path,
                previous_mode="empty", current_mode="unknown",
                action="error", error=str(e), is_symlink=is_symlink,
            )
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    # Non-empty file: check and set journal mode
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10)

        # Check current mode
        current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        # Integrity check (before making changes)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except Exception as e:
            integrity = f"error: {e}"

        if current_mode == "wal":
            action = "already_wal"

            # Optionally checkpoint
            if checkpoint and not dry_run:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    action = "checkpoint"
                except Exception as e:
                    logger.debug(f"Checkpoint failed for {db_path}: {e}")

            return WALResult(
                path=db_path, real_path=real_path,
                previous_mode="wal", current_mode="wal",
                action=action, integrity=integrity,
                size_bytes=size, is_symlink=is_symlink,
            )

        # Integrity failed -- skip WAL conversion (report only)
        if integrity != "ok":
            return WALResult(
                path=db_path, real_path=real_path,
                previous_mode=current_mode, current_mode=current_mode,
                action="skip_corrupt", integrity=integrity,
                size_bytes=size, is_symlink=is_symlink,
            )

        if dry_run:
            return WALResult(
                path=db_path, real_path=real_path,
                previous_mode=current_mode, current_mode=current_mode,
                action="needs_wal", integrity=integrity,
                size_bytes=size, is_symlink=is_symlink,
            )

        # Enable WAL
        new_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]

        if new_mode != "wal":
            return WALResult(
                path=db_path, real_path=real_path,
                previous_mode=current_mode, current_mode=new_mode,
                action="error", integrity=integrity,
                error=f"PRAGMA returned '{new_mode}' instead of 'wal'",
                size_bytes=size, is_symlink=is_symlink,
            )

        # Checkpoint after enabling
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            logger.debug(f"Post-enable checkpoint failed for {db_path}: {e}")

        logger.info(f"WAL enabled: {db_path} ({current_mode} -> wal)")

        return WALResult(
            path=db_path, real_path=real_path,
            previous_mode=current_mode, current_mode="wal",
            action="enabled_wal", integrity=integrity,
            size_bytes=size, is_symlink=is_symlink,
        )

    except sqlite3.OperationalError as e:
        error_msg = str(e)
        action = "skip_locked" if "locked" in error_msg.lower() else "error"
        logger.warning(f"WAL enable failed for {db_path}: {error_msg}")
        return WALResult(
            path=db_path, real_path=real_path,
            previous_mode="unknown", current_mode="unknown",
            action=action, error=error_msg,
            size_bytes=size, is_symlink=is_symlink,
        )
    except Exception as e:
        error_msg = str(e)
        logger.warning(f"WAL enable failed for {db_path}: {error_msg}")
        return WALResult(
            path=db_path, real_path=real_path,
            previous_mode="unknown", current_mode="unknown",
            action="error", error=error_msg,
            size_bytes=size, is_symlink=is_symlink,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def enable_wal_all_dbs(dry_run: bool = False, checkpoint: bool = False) -> WALReport:
    """
    Enable WAL mode on all discovered SQLite databases.

    Args:
        dry_run: If True, only report without changing
        checkpoint: If True, run WAL checkpoint (TRUNCATE) on all DBs

    Returns:
        WALReport with results for each DB
    """
    report = WALReport()
    db_files = discover_db_files()

    for db_path in db_files:
        result = _enable_wal_single(db_path, dry_run=dry_run, checkpoint=checkpoint)
        report.results.append(result)
        report.total_dbs += 1

        if result.action == "already_wal":
            report.already_wal += 1
        elif result.action == "enabled_wal":
            report.newly_enabled += 1
        elif result.action == "initialized_wal":
            report.initialized += 1
        elif result.action == "checkpoint":
            report.already_wal += 1
            report.checkpointed += 1
        elif result.action in ("skip_corrupt", "skip_locked", "skip_missing"):
            report.skipped += 1
        elif result.action == "error":
            report.errors += 1
        elif result.action in ("needs_wal", "would_init_wal"):
            pass  # dry_run counts

    level = "DRY RUN" if dry_run else "WAL SWEEP"
    logger.info(f"{level}: {report.summary}")

    return report


# Scheduler-compatible entry point
def run_enable_wal_all() -> tuple:
    """
    Entry point for lite_scheduler job.

    Returns:
        (success, message) tuple
    """
    try:
        report = enable_wal_all_dbs(dry_run=False, checkpoint=True)
        return True, f"wal: {report.summary}"
    except Exception as e:
        return False, f"wal sweep error: {str(e)[:80]}"


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    dry_run = "--dry-run" in sys.argv
    checkpoint = "--checkpoint" in sys.argv
    json_output = "--json" in sys.argv

    report = enable_wal_all_dbs(dry_run=dry_run, checkpoint=checkpoint)

    if json_output:
        print(json_mod.dumps(report.to_dict(), indent=2))
        sys.exit(1 if report.errors > 0 else 0)

    mode = "DRY RUN" if dry_run else ("CHECKPOINT" if checkpoint else "ENABLE WAL")
    print(f"{'=' * 60}")
    print(f"WAL MODE ENFORCER -- {mode}")
    print(f"{'=' * 60}\n")

    # Display results
    home = os.path.expanduser("~")
    for r in report.results:
        short_path = r.path.replace(home, "~")
        size_str = f"{r.size_bytes:>10,}" if r.size_bytes else "         0"

        if r.action == "enabled_wal":
            icon = "  FIX"
            detail = f"{r.previous_mode} -> wal  [integrity: {r.integrity}]"
        elif r.action == "initialized_wal":
            icon = " INIT"
            detail = "empty -> wal (future-proofed)"
        elif r.action == "already_wal":
            icon = "   OK"
            detail = f"wal  [integrity: {r.integrity}]"
        elif r.action == "checkpoint":
            icon = "  CHK"
            detail = f"wal (checkpointed)  [integrity: {r.integrity}]"
        elif r.action in ("needs_wal", "would_init_wal"):
            icon = " NEED"
            detail = f"{r.previous_mode} (needs WAL)"
        elif r.action == "skip_corrupt":
            icon = " WARN"
            detail = f"integrity FAILED: {r.integrity[:50]}"
        elif r.action == "skip_locked":
            icon = " LOCK"
            detail = f"database locked: {r.error[:50]}"
        elif r.action == "skip_missing":
            icon = " MISS"
            detail = "file not found"
        elif r.action == "error":
            icon = "  ERR"
            detail = r.error[:60]
        else:
            icon = "    ?"
            detail = r.action

        sym = " [symlink]" if r.is_symlink else ""
        print(f"{icon} {size_str}  {short_path}{sym}")
        print(f"      {detail}")

    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {report.summary}")
    print(f"{'=' * 60}")
