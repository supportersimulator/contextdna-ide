#!/usr/bin/env python3
"""
BUTLER DB REPAIR — Self-Healing SQLite Integrity for the Mansion

Anatomical Role: SKELETAL SYSTEM (structural integrity of all databases)
Disease Analogy: Bone fracture → SQLite corruption → recover from PG backup

The butler checks all SQLite databases for corruption every 30 minutes.
When corruption is detected:
  1. DIAGNOSE: What's wrong? (integrity_check, file test)
  2. BACKUP: Move corrupted file aside (never delete)
  3. RECOVER: Try sqlite3 .recover first, then PG restore
  4. VERIFY: Confirm restored data counts match expectations
  5. CAPTURE: Record the repair as an SOP with outcome tracking

Repair SOPs feed into the evidence pipeline so unreliable repairs get flagged.

Usage:
    from memory.butler_db_repair import ButlerDBRepair
    repair = ButlerDBRepair()
    report = repair.run_integrity_sweep()
    # Returns: {db_name: RepairResult, ...}

Created: February 6, 2026
Anatomical System: Skeletal (structural integrity)
"""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("context_dna.butler_repair")


class DBHealth(str, Enum):
    HEALTHY = "healthy"
    CORRUPTED = "corrupted"
    MISSING = "missing"
    EMPTY = "empty"
    WAL_ISSUE = "wal_issue"


class RepairStrategy(str, Enum):
    SQLITE_RECOVER = "sqlite_recover"      # sqlite3 .recover → new DB
    PG_RESTORE = "pg_restore"              # Restore from PostgreSQL backup
    RECREATE_EMPTY = "recreate_empty"      # Let code recreate schema
    WAL_CHECKPOINT = "wal_checkpoint"      # Force WAL checkpoint
    NONE = "none"                          # No repair needed


class RepairOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"          # Restored but with data loss
    FAILED = "failed"
    NOT_NEEDED = "not_needed"


@dataclass
class RepairResult:
    db_name: str
    db_path: str
    health_before: DBHealth
    strategy_used: RepairStrategy
    outcome: RepairOutcome
    rows_before: int = 0        # Rows in corrupted DB (if readable)
    rows_after: int = 0         # Rows after repair
    tables_recovered: int = 0
    error: str = ""
    duration_ms: int = 0
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict:
        d = asdict(self)
        d['health_before'] = self.health_before.value
        d['strategy_used'] = self.strategy_used.value
        d['outcome'] = self.outcome.value
        return d


# Resolve DB paths through unified DB if available
def _resolve_mansion_path(path_str: str) -> str:
    """Resolve a mansion DB path, preferring unified DB."""
    try:
        from memory.db_utils import get_unified_db_path
        from pathlib import Path
        resolved = get_unified_db_path(Path(os.path.expanduser(path_str)) if '~' in path_str else Path(path_str))
        return str(resolved)
    except Exception:
        return path_str


# All SQLite databases the mansion relies on
# NOTE: Paths are relative to base_dir (memory/) — do NOT prefix with "memory/"
MANSION_DATABASES = {
    "observability": {
        "path": ".observability.db",
        "alt_path": os.path.expanduser("~/.context-dna/.observability.db"),
        "pg_source": {"db": "context_dna", "key_tables": ["claim", "outcome_event", "knowledge_quarantine"]},
        "critical": True,
        "anatomical_name": "Vertebral Column",
        "description": "Evidence pipeline backbone — claims, outcomes, quarantine",
        "repair_sops_link": ".repair_sops.db",
    },
    "learnings": {
        "path": os.path.expanduser("~/.context-dna/learnings.db"),
        "pg_source": {"db": "context_dna", "key_tables": ["learnings"]},
        "critical": True,
        "anatomical_name": "Skull",
        "description": "Accumulated knowledge — wins, gotchas, fixes, patterns",
        "repair_sops_link": ".repair_sops.db",
    },
    "profiles": {
        "path": os.path.expanduser("~/.context-dna/profiles.db"),
        "pg_source": {"db": "context_dna", "key_tables": ["hierarchy_profiles"]},
        "critical": False,
        "anatomical_name": "Pelvis",
        "description": "Machine profiles and hierarchy data",
        "repair_sops_link": ".repair_sops.db",
    },
    "ab_tracking": {
        "path": ".context_ab_tracking.db",
        "pg_source": None,
        "critical": True,
        "anatomical_name": "Ribcage",
        "description": "A/B testing boundary injections and feedback",
        "repair_sops_link": ".repair_sops.db",
    },
    "hindsight_validator": {
        "path": ".hindsight_validator.db",
        "pg_source": None,
        "critical": False,
        "anatomical_name": "Femur",
        "description": "Win verification and miswiring detection",
        "repair_sops_link": ".repair_sops.db",
    },
    "failure_patterns": {
        "path": ".failure_patterns.db",
        "pg_source": None,
        "critical": False,
        "anatomical_name": "Tibia",
        "description": "Failure pattern analysis database",
        "repair_sops_link": ".repair_sops.db",
    },
    "dialogue_mirror": {
        "path": os.path.expanduser("~/.context-dna/.dialogue_mirror.db"),
        "pg_source": None,
        "critical": True,
        "anatomical_name": "Spine",
        "description": "Dialogue thread history for hindsight analysis",
        "repair_sops_link": ".repair_sops.db",
    },
    "sop_enhancer": {
        "path": ".sop_enhancer.db",
        "pg_source": None,
        "critical": False,
        "anatomical_name": "Humerus",
        "description": "SOP deduplication hashes",
        "repair_sops_link": ".repair_sops.db",
    },
    "webhook_quality": {
        "path": ".webhook_quality.db",
        "pg_source": None,
        "critical": False,
        "anatomical_name": "Radius",
        "description": "Webhook section quality metrics",
        "repair_sops_link": ".repair_sops.db",
    },
    "outcome_tracking": {
        "path": ".outcome_tracking.db",
        "pg_source": None,
        "critical": False,
        "anatomical_name": "Ulna",
        "description": "Outcome tracking for success detection",
        "repair_sops_link": ".repair_sops.db",
    },
    "pattern_evolution": {
        "path": ".pattern_evolution.db",
        "pg_source": None,
        "critical": False,
        "anatomical_name": "Scapula",
        "description": "Hook variant evolution and A/B testing",
        "repair_sops_link": ".repair_sops.db",
    },
    "repair_sops": {
        "path": ".repair_sops.db",
        "pg_source": None,
        "critical": False,
        "anatomical_name": "Bone Marrow",
        "description": "MMOTW-mined repair SOPs — the butler's repair memory",
        "repair_sops_link": ".repair_sops.db",
    },
}


def discover_sqlite_databases(base_dir: str) -> Dict[str, dict]:
    """
    Auto-discover SQLite databases in a workspace directory.

    Returns entries compatible with MANSION_DATABASES format.
    Merges with the static registry — discovered DBs that aren't
    in the registry get added with sensible defaults.
    """
    discovered = {}
    base = Path(base_dir)
    for db_file in base.glob(".*.db"):
        if db_file.is_symlink():
            # Resolve symlinks but keep the original name
            pass
        name = db_file.stem.lstrip(".")
        # Skip backups, WAL, SHM files
        if any(db_file.name.endswith(s) for s in ["-wal", "-shm", "-journal"]):
            continue
        if "backup" in db_file.name or "corrupt" in db_file.name or "malformed" in db_file.name:
            continue
        if name not in MANSION_DATABASES and name not in discovered:
            discovered[name] = {
                "path": db_file.name,
                "pg_source": None,
                "critical": False,
                "anatomical_name": name.replace("_", " ").title(),
                "description": f"Auto-discovered: {db_file.name}",
                "repair_sops_link": ".repair_sops.db",
            }
    return discovered


class ButlerDBRepair:
    """
    Butler's skeletal repair system — keeps all mansion databases structurally sound.

    Runs integrity checks, attempts self-healing, captures repair SOPs.
    Adaptable per workspace: pass base_dir to point at any directory with .db files.
    """

    def __init__(self, base_dir: str = None, auto_discover: bool = True):
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.repair_log: List[RepairResult] = []
        # Merge static registry with auto-discovered databases
        self.databases = dict(MANSION_DATABASES)
        if auto_discover:
            self.databases.update(discover_sqlite_databases(self.base_dir))

    def _resolve_path(self, db_info: dict) -> str:
        """Resolve database path, handling relative paths."""
        path = db_info["path"]
        if os.path.isabs(path):
            return path
        return os.path.join(self.base_dir, path)

    def check_integrity(self, db_path: str) -> Tuple[DBHealth, str]:
        """
        Check a single SQLite database for corruption.

        Returns (health_status, detail_message)
        """
        if not os.path.exists(db_path):
            return DBHealth.MISSING, f"File not found: {db_path}"

        if os.path.getsize(db_path) == 0:
            return DBHealth.EMPTY, f"Empty file: {db_path}"

        try:
            from memory.db_utils import connect_wal
            conn = connect_wal(db_path)
            # Try basic operations
            result = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()

            if result and result[0] == "ok":
                return DBHealth.HEALTHY, "integrity_check: ok"
            else:
                detail = result[0] if result else "unknown"
                return DBHealth.CORRUPTED, f"integrity_check failed: {detail}"

        except sqlite3.DatabaseError as e:
            error_str = str(e)
            if "file is not a database" in error_str:
                return DBHealth.CORRUPTED, f"Not a valid database: {error_str}"
            if "disk I/O error" in error_str:
                return DBHealth.CORRUPTED, f"Disk I/O: {error_str}"
            return DBHealth.CORRUPTED, f"DatabaseError: {error_str}"
        except Exception as e:
            return DBHealth.CORRUPTED, f"Unexpected: {e}"

    def _count_rows(self, db_path: str) -> Dict[str, int]:
        """Count rows in all tables of a database."""
        counts = {}
        try:
            from memory.db_utils import connect_wal
            conn = connect_wal(db_path)
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()]
            for t in tables:
                try:
                    c = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                    counts[t] = c
                except Exception:
                    counts[t] = -1
            conn.close()
        except Exception:
            pass
        return counts

    def _try_sqlite_recover(self, corrupted_path: str, output_path: str) -> bool:
        """
        Attempt sqlite3 .recover to extract data from corrupted DB.

        Returns True if recovery produced usable SQL.
        """
        try:
            result = subprocess.run(
                ["sqlite3", corrupted_path, ".recover"],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0 and len(result.stdout) > 100:
                # Apply recovered SQL to new DB
                subprocess.run(
                    ["sqlite3", output_path],
                    input=result.stdout,
                    capture_output=True, text=True, timeout=60
                )
                return os.path.exists(output_path) and os.path.getsize(output_path) > 0
        except Exception as e:
            logger.warning(f"sqlite3 .recover failed: {e}")
        return False

    def _try_pg_restore(self, db_name: str, db_info: dict, target_path: str) -> Tuple[bool, int]:
        """
        Restore database from PostgreSQL backup.

        Returns (success, total_rows_restored)
        """
        pg_source = db_info.get("pg_source")
        if not pg_source:
            return False, 0

        pg_db = pg_source["db"]
        key_tables = pg_source.get("key_tables", [])

        # The target DB needs its schema created first by the owning module
        # For observability, we can import ObservabilityStore to init schema
        if db_name == "observability":
            try:
                # Set env to force this path
                os.environ["OBSERVABILITY_DB_PATH"] = target_path
                from memory.observability_store import ObservabilityStore
                store = ObservabilityStore(db_path=target_path)
                del os.environ["OBSERVABILITY_DB_PATH"]

                total_restored = 0
                conn = store._sqlite_conn

                # Disable FK for bulk restore
                conn.execute("PRAGMA foreign_keys = OFF")

                for table in key_tables:
                    try:
                        # Get column info from SQLite
                        cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
                        col_names = [c[1] for c in cols_info]

                        # Fetch from PG
                        col_list = ", ".join([f"COALESCE({c}::text, '')" for c in col_names])
                        result = subprocess.run([
                            "docker", "exec", "context-dna-postgres",
                            "psql", "-U", "context_dna", "-d", pg_db,
                            "-t", "-A", "-F", "|",
                            "-c", f"SELECT {col_list} FROM {table}"
                        ], capture_output=True, text=True, timeout=30)

                        if result.returncode != 0:
                            continue

                        rows_inserted = 0
                        for line in result.stdout.strip().split("\n"):
                            if not line:
                                continue
                            parts = line.split("|", len(col_names) - 1)
                            if len(parts) != len(col_names):
                                continue
                            placeholders = ", ".join(["?"] * len(col_names))
                            try:
                                conn.execute(
                                    f"INSERT OR IGNORE INTO {table} ({', '.join(col_names)}) VALUES ({placeholders})",
                                    tuple(p if p else None for p in parts)
                                )
                                rows_inserted += 1
                            except Exception:
                                pass

                        conn.commit()
                        total_restored += rows_inserted
                        logger.info(f"PG restore: {table} → {rows_inserted} rows")

                    except Exception as e:
                        logger.warning(f"PG restore {table} failed: {e}")

                conn.execute("PRAGMA foreign_keys = ON")
                return total_restored > 0, total_restored

            except Exception as e:
                logger.warning(f"PG restore for {db_name} failed: {e}")
                return False, 0

        return False, 0

    def repair_database(self, db_name: str) -> RepairResult:
        """
        Attempt to repair a single database.

        Strategy order:
        1. WAL checkpoint (if WAL issue)
        2. sqlite3 .recover (preserves most data)
        3. PG restore (authoritative backup)
        4. Recreate empty (last resort)
        """
        db_info = self.databases.get(db_name)
        if not db_info:
            return RepairResult(db_name, "", DBHealth.MISSING, RepairStrategy.NONE,
                                RepairOutcome.FAILED, error=f"Unknown database: {db_name}")

        db_path = self._resolve_path(db_info)
        start = time.monotonic()

        # Check health
        health, detail = self.check_integrity(db_path)

        if health == DBHealth.HEALTHY:
            return RepairResult(db_name, db_path, health, RepairStrategy.NONE,
                                RepairOutcome.NOT_NEEDED)

        logger.warning(f"[{db_info['anatomical_name']}] {db_name} is {health.value}: {detail}")

        # Count rows before (if possible)
        rows_before = sum(self._count_rows(db_path).values())

        # Backup corrupted file
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{db_path}.corrupted_{timestamp}"
        if os.path.exists(db_path):
            shutil.copy2(db_path, backup_path)
            os.remove(db_path)
            logger.info(f"Backed up corrupted DB to {backup_path}")

        # Strategy 1: sqlite3 .recover
        if health == DBHealth.CORRUPTED and os.path.exists(backup_path):
            recovered_path = f"{db_path}.recovered"
            if self._try_sqlite_recover(backup_path, recovered_path):
                rows_recovered = sum(self._count_rows(recovered_path).values())
                if rows_recovered > 0:
                    shutil.move(recovered_path, db_path)
                    duration = int((time.monotonic() - start) * 1000)
                    result = RepairResult(
                        db_name, db_path, health, RepairStrategy.SQLITE_RECOVER,
                        RepairOutcome.SUCCESS if rows_recovered >= rows_before * 0.9 else RepairOutcome.PARTIAL,
                        rows_before=rows_before, rows_after=rows_recovered,
                        tables_recovered=len(self._count_rows(db_path)),
                        duration_ms=duration
                    )
                    self._capture_repair_sop(result, db_info)
                    return result
                else:
                    # Recovery produced empty DB, remove
                    os.remove(recovered_path) if os.path.exists(recovered_path) else None

        # Strategy 2: PG restore
        if db_info.get("pg_source"):
            success, total_rows = self._try_pg_restore(db_name, db_info, db_path)
            if success:
                duration = int((time.monotonic() - start) * 1000)
                result = RepairResult(
                    db_name, db_path, health, RepairStrategy.PG_RESTORE,
                    RepairOutcome.SUCCESS, rows_before=rows_before, rows_after=total_rows,
                    tables_recovered=len(self._count_rows(db_path)),
                    duration_ms=duration
                )
                self._capture_repair_sop(result, db_info)
                return result

        # Strategy 3: Recreate empty (schema will be rebuilt by owning module)
        # Just ensure the file doesn't exist so the module creates it fresh
        duration = int((time.monotonic() - start) * 1000)
        result = RepairResult(
            db_name, db_path, health, RepairStrategy.RECREATE_EMPTY,
            RepairOutcome.PARTIAL, rows_before=rows_before, rows_after=0,
            error="Data loss — recreated empty. PG sync will repopulate on next cycle.",
            duration_ms=duration
        )
        self._capture_repair_sop(result, db_info)
        return result

    def _capture_repair_sop(self, result: RepairResult, db_info: dict):
        """
        Record the repair as an SOP and outcome event in the evidence pipeline.

        This is the "repair capture" — every fix the butler performs becomes
        a learnable pattern that can be validated by the evidence system.
        """
        try:
            from memory.observability_store import get_observability_store
            store = get_observability_store()

            # Record as outcome event
            store.record_outcome_event(
                session_id=f"butler_repair_{result.db_name}_{result.timestamp[:10]}",
                outcome_type="butler_db_repair",
                success=result.outcome in (RepairOutcome.SUCCESS, RepairOutcome.PARTIAL),
                reward=1.0 if result.outcome == RepairOutcome.SUCCESS else (
                    0.3 if result.outcome == RepairOutcome.PARTIAL else -0.3
                ),
                notes=json.dumps({
                    "db": result.db_name,
                    "anatomical": db_info.get("anatomical_name", "unknown"),
                    "strategy": result.strategy_used.value,
                    "rows_restored": result.rows_after,
                    "data_loss": max(0, result.rows_before - result.rows_after),
                    "duration_ms": result.duration_ms,
                })
            )

            # Record as butler code note (anatomical system)
            store._sqlite_conn.execute("""
                INSERT OR REPLACE INTO butler_code_note
                (note_id, timestamp_utc, source_job, file_path,
                 anatomic_location, error_type, severity,
                 error_message, suggested_fix, llm_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"repair_{result.db_name}_{result.timestamp[:19]}",
                result.timestamp,
                "butler_db_repair",
                result.db_path,
                "skeleton",  # anatomic_location = skeletal system
                "runtime_error",
                "critical" if db_info.get("critical") else "warning",
                f"{result.db_name} ({db_info.get('anatomical_name', '?')}): {result.health_before.value}",
                f"Strategy: {result.strategy_used.value} → {result.outcome.value}. "
                f"Rows: {result.rows_before}→{result.rows_after}",
                0.9 if result.outcome == RepairOutcome.SUCCESS else 0.5,
            ))
            store._sqlite_conn.commit()

        except Exception as e:
            logger.warning(f"Failed to capture repair SOP: {e}")

    def run_integrity_sweep(self) -> Dict[str, RepairResult]:
        """
        Check all mansion databases and repair any that are corrupted.

        Returns dict of {db_name: RepairResult}
        """
        results = {}
        for db_name, db_info in self.databases.items():
            db_path = self._resolve_path(db_info)
            health, detail = self.check_integrity(db_path)

            if health == DBHealth.HEALTHY:
                results[db_name] = RepairResult(
                    db_name, db_path, health, RepairStrategy.NONE, RepairOutcome.NOT_NEEDED
                )
            elif health == DBHealth.MISSING:
                # Missing is OK — module will recreate on next use
                results[db_name] = RepairResult(
                    db_name, db_path, health, RepairStrategy.NONE, RepairOutcome.NOT_NEEDED,
                    error="File missing — will be recreated by owning module"
                )
            else:
                # Corrupted or empty — repair
                logger.info(f"Repairing {db_name} ({db_info['anatomical_name']}): {health.value}")
                results[db_name] = self.repair_database(db_name)

        self.repair_log.extend(results.values())
        return results

    def get_skeletal_health_report(self) -> dict:
        """Get a summary of all database health for monitoring."""
        report = {
            "timestamp": datetime.utcnow().isoformat(),
            "total_databases": len(self.databases),
            "databases": {},
        }

        healthy = 0
        for db_name, db_info in self.databases.items():
            db_path = self._resolve_path(db_info)
            health, detail = self.check_integrity(db_path)

            row_counts = self._count_rows(db_path) if health == DBHealth.HEALTHY else {}
            total_rows = sum(row_counts.values())

            report["databases"][db_name] = {
                "anatomical_name": db_info["anatomical_name"],
                "health": health.value,
                "critical": db_info["critical"],
                "total_rows": total_rows,
                "path": db_path,
                "detail": detail,
            }

            if health == DBHealth.HEALTHY:
                healthy += 1

        report["healthy_count"] = healthy
        report["health_pct"] = round(healthy / len(self.databases) * 100, 1)
        return report


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    # Accept optional --base-dir for workspace adaptability
    base_dir = None
    args = sys.argv[1:]
    if "--base-dir" in args:
        idx = args.index("--base-dir")
        base_dir = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    repair = ButlerDBRepair(base_dir=base_dir)
    cmd = args[0] if args else None

    if cmd == "sweep":
        print(f"Running integrity sweep on {len(repair.databases)} databases (base: {repair.base_dir})...")
        results = repair.run_integrity_sweep()
        for name, r in results.items():
            icon = "✅" if r.outcome == RepairOutcome.NOT_NEEDED else (
                "🔧" if r.outcome == RepairOutcome.SUCCESS else
                "⚠️" if r.outcome == RepairOutcome.PARTIAL else "❌"
            )
            anat = repair.databases.get(name, {}).get("anatomical_name", name)
            print(f"  {icon} {name} ({anat}): {r.health_before.value} → {r.outcome.value}")
            if r.rows_after > 0:
                print(f"     Rows restored: {r.rows_after}")
    elif cmd == "health":
        report = repair.get_skeletal_health_report()
        print(f"\nSkeletal Health: {report['health_pct']}% ({report['healthy_count']}/{report['total_databases']})")
        for name, info in report["databases"].items():
            icon = "🦴" if info["health"] == "healthy" else "💀"
            crit = " [CRITICAL]" if info["critical"] else ""
            print(f"  {icon} {info['anatomical_name']}: {info['health']}{crit} ({info['total_rows']} rows)")
    else:
        print("Usage: python butler_db_repair.py [sweep|health] [--base-dir /path/to/workspace]")
