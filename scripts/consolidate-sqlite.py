#!/usr/bin/env python3
"""
SQLite Database Consolidation Script — Gap #8 Fix
===================================================

Problem: 16+ separate SQLite databases cause connection chaos, FD leaks,
         WAL corruption, and make it impossible to reason about data flow.

Solution: Consolidate into 3 logical databases:
  1. contextdna_unified.db  — all Context DNA operational data (12 source DBs)
  2. session_archive.db     — stays separate (15MB+, different lifecycle)
  3. ~/.3surgeons/           — stays separate (independent subsystem)

Each source DB's tables get a prefix to avoid collisions:
  learnings.db            → lrn_*
  .dialogue_mirror.db     → dlg_*
  .synaptic_personality.db→ syn_personality_*
  .synaptic_evolution.db  → syn_evo_*
  .synaptic_evolution_tracking.db → syn_track_*
  .synaptic_patterns.db   → syn_pat_*
  .synaptic_chat.db       → syn_chat_*
  contextdna_tasks.db     → task_*
  ghostscan.db            → ghost_*
  memory/.outcome_tracking.db → out_*
  memory/.failure_patterns.db → fail_*
  memory/.strategic_plans.db  → plan_*
  memory/.hindsight_validator.db → hind_*

Usage:
    python scripts/consolidate-sqlite.py --dry-run          # See what would happen
    python scripts/consolidate-sqlite.py --dry-run --verbose # With table schemas
    python scripts/consolidate-sqlite.py --execute           # Actually migrate
    python scripts/consolidate-sqlite.py --execute --backup  # Migrate with backups
    python scripts/consolidate-sqlite.py --verify            # Verify post-migration

Post-migration, update connection paths in:
    memory/sqlite_storage.py (get_db_path)
    memory/dialogue_mirror.py
    memory/synaptic_voice.py
    memory/synaptic_personality.py
    memory/session_intent.py
    memory/atlas_journal.py
    memory/synaptic_anatomy.py
    memory/anticipation_engine.py
    memory/code_chunk_indexer.py
    memory/file_organization_analyzer.py
    memory/organization_patterns_library.py
    memory/butler_repair_miner.py
    memory/recovery_agent.py
    memory/complexity_vector_sentinel.py
    memory/butler_db_repair.py (MANSION_DATABASES registry)
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============================================================================
# Configuration
# ============================================================================

CONTEXT_DNA_DIR = Path(os.environ.get("CONTEXT_DNA_DIR", Path.home() / ".context-dna"))
MEMORY_DIR = Path(__file__).parent.parent / "memory"
UNIFIED_DB_PATH = CONTEXT_DNA_DIR / "contextdna_unified.db"
BACKUP_DIR = CONTEXT_DNA_DIR / "pre-consolidation-backup"

# What stays separate and why
EXCLUDED = {
    "session_archive.db": "15MB+, different lifecycle (archival vs operational), heavy BLOB columns",
    "~/.3surgeons/evidence.db": "Independent subsystem with own schema evolution",
    "~/.3surgeons/state.db": "Independent subsystem KV store",
}


@dataclass
class SourceDB:
    """A source database to consolidate."""
    name: str           # Human-readable name
    path: Path          # Absolute path to .db file
    prefix: str         # Table name prefix in unified DB
    critical: bool      # If True, migration failure is fatal
    description: str


@dataclass
class MigrationPlan:
    """What will happen during migration."""
    source: SourceDB
    tables: List[str] = field(default_factory=list)
    row_counts: Dict[str, int] = field(default_factory=dict)
    schemas: Dict[str, str] = field(default_factory=dict)
    renamed_tables: Dict[str, str] = field(default_factory=dict)  # old -> new
    fts_tables: List[str] = field(default_factory=list)  # FTS tables to skip
    errors: List[str] = field(default_factory=list)


# Source databases to consolidate
SOURCES: List[SourceDB] = [
    SourceDB("learnings", CONTEXT_DNA_DIR / "learnings.db", "lrn",
             critical=True, description="Accumulated knowledge — wins, gotchas, fixes"),
    SourceDB("dialogue_mirror", CONTEXT_DNA_DIR / ".dialogue_mirror.db", "dlg",
             critical=True, description="Dialogue thread history for hindsight analysis"),
    SourceDB("synaptic_personality", CONTEXT_DNA_DIR / ".synaptic_personality.db", "syn_pers",
             critical=False, description="Synaptic voice characteristics and personality"),
    SourceDB("synaptic_evolution", CONTEXT_DNA_DIR / ".synaptic_evolution.db", "syn_evo",
             critical=False, description="Synaptic evolutionary insights and gaps"),
    SourceDB("synaptic_evolution_tracking", CONTEXT_DNA_DIR / ".synaptic_evolution_tracking.db", "syn_trk",
             critical=False, description="Belief evolution tracking"),
    SourceDB("synaptic_patterns", CONTEXT_DNA_DIR / ".synaptic_patterns.db", "syn_pat",
             critical=False, description="Detected behavioral patterns"),
    SourceDB("synaptic_chat", CONTEXT_DNA_DIR / ".synaptic_chat.db", "syn_chat",
             critical=False, description="Synaptic chat history"),
    SourceDB("contextdna_tasks", CONTEXT_DNA_DIR / "contextdna_tasks.db", "task",
             critical=False, description="Task tracking and progress events"),
    SourceDB("ghostscan", CONTEXT_DNA_DIR / "ghostscan.db", "ghost",
             critical=False, description="Ghost scan findings and runs"),
    SourceDB("outcome_tracking", MEMORY_DIR / ".outcome_tracking.db", "out",
             critical=False, description="Outcome tracking for success detection"),
    SourceDB("failure_patterns", MEMORY_DIR / ".failure_patterns.db", "fail",
             critical=False, description="Failure pattern analysis"),
    SourceDB("strategic_plans", MEMORY_DIR / ".strategic_plans.db", "plan",
             critical=False, description="Major plans and big picture injections"),
    SourceDB("hindsight_validator", MEMORY_DIR / ".hindsight_validator.db", "hind",
             critical=False, description="Win verification and miswiring detection"),
]

# FTS-related tables/suffixes to handle specially
FTS_SUFFIXES = ("_fts", "_fts_data", "_fts_idx", "_fts_docsize", "_fts_config")


# ============================================================================
# Analysis
# ============================================================================

def analyze_source(source: SourceDB) -> MigrationPlan:
    """Analyze a source DB and build a migration plan."""
    plan = MigrationPlan(source=source)

    if not source.path.exists():
        plan.errors.append(f"DB file not found: {source.path}")
        return plan

    try:
        conn = sqlite3.connect(str(source.path))
        conn.execute("PRAGMA journal_mode=WAL")

        # Get all tables
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        all_tables = [row[0] for row in cursor.fetchall()]

        for table in all_tables:
            # Identify FTS tables
            if any(table.endswith(s) for s in FTS_SUFFIXES) or _is_fts_table(conn, table):
                plan.fts_tables.append(table)
                continue

            # Skip sqlite internals
            if table.startswith("sqlite_"):
                continue

            plan.tables.append(table)
            new_name = f"{source.prefix}_{table}"
            plan.renamed_tables[table] = new_name

            # Row count
            try:
                count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                plan.row_counts[table] = count
            except Exception as e:
                plan.row_counts[table] = -1
                plan.errors.append(f"Count failed for {table}: {e}")

            # Schema
            try:
                schema = conn.execute(
                    f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)
                ).fetchone()
                if schema and schema[0]:
                    plan.schemas[table] = schema[0]
            except Exception as e:
                plan.errors.append(f"Schema read failed for {table}: {e}")

        conn.close()
    except Exception as e:
        plan.errors.append(f"Failed to open DB: {e}")

    return plan


def _is_fts_table(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table is an FTS virtual table."""
    try:
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        ).fetchone()
        if schema and schema[0] and "USING fts" in schema[0].upper():
            return True
    except Exception:
        pass
    return False


def _rewrite_schema(original_sql: str, old_name: str, new_name: str,
                    source_prefix: str) -> str:
    """Rewrite CREATE TABLE statement with new table name."""
    # Replace table name in CREATE TABLE
    result = original_sql.replace(
        f'CREATE TABLE {old_name}',
        f'CREATE TABLE {new_name}',
        1
    )
    result = result.replace(
        f'CREATE TABLE "{old_name}"',
        f'CREATE TABLE "{new_name}"',
        1
    )
    result = result.replace(
        f"CREATE TABLE '{old_name}'",
        f"CREATE TABLE '{new_name}'",
        1
    )
    # Also handle IF NOT EXISTS
    result = result.replace(
        f'CREATE TABLE IF NOT EXISTS {old_name}',
        f'CREATE TABLE IF NOT EXISTS {new_name}',
        1
    )
    result = result.replace(
        f"CREATE TABLE IF NOT EXISTS '{old_name}'",
        f"CREATE TABLE IF NOT EXISTS '{new_name}'",
        1
    )
    return result


# ============================================================================
# Migration
# ============================================================================

def migrate_source(plan: MigrationPlan, unified_conn: sqlite3.Connection,
                   verbose: bool = False) -> Tuple[int, int, List[str]]:
    """Migrate one source DB into the unified DB. Returns (migrated, skipped, errors)."""
    source = plan.source
    migrated = 0
    skipped = 0
    errors = []

    if not source.path.exists():
        errors.append(f"Source not found: {source.path}")
        return migrated, skipped, errors

    try:
        src_conn = sqlite3.connect(str(source.path))
        src_conn.row_factory = sqlite3.Row

        for table in plan.tables:
            new_name = plan.renamed_tables[table]

            # Create table in unified DB
            if table in plan.schemas:
                new_schema = _rewrite_schema(plan.schemas[table], table, new_name,
                                             source.prefix)
                try:
                    unified_conn.execute(new_schema)
                    if verbose:
                        print(f"    Created table: {new_name}")
                except sqlite3.OperationalError as e:
                    if "already exists" in str(e):
                        if verbose:
                            print(f"    Table exists: {new_name} (skipping create)")
                    else:
                        errors.append(f"Create {new_name} failed: {e}")
                        continue

            # Copy data
            try:
                rows = src_conn.execute(f'SELECT * FROM "{table}"').fetchall()
                if not rows:
                    skipped += 1
                    continue

                # Get column names
                cols = rows[0].keys()
                placeholders = ", ".join(["?"] * len(cols))
                col_names = ", ".join(f'"{c}"' for c in cols)

                insert_sql = f'INSERT OR IGNORE INTO "{new_name}" ({col_names}) VALUES ({placeholders})'

                for row in rows:
                    try:
                        unified_conn.execute(insert_sql, tuple(row))
                        migrated += 1
                    except Exception as e:
                        errors.append(f"Insert into {new_name} failed: {e}")

            except Exception as e:
                errors.append(f"Read from {source.name}.{table} failed: {e}")

        src_conn.close()
    except Exception as e:
        errors.append(f"Open source {source.path} failed: {e}")

    return migrated, skipped, errors


def recreate_fts_indexes(plan: MigrationPlan, unified_conn: sqlite3.Connection,
                         verbose: bool = False) -> List[str]:
    """Recreate FTS indexes for tables that had them in the source."""
    errors = []
    source = plan.source

    if not source.path.exists():
        return errors

    # Check if source had FTS on learnings
    base_tables_with_fts = set()
    for fts_table in plan.fts_tables:
        # Extract base name: "learnings_fts" -> "learnings"
        for suffix in FTS_SUFFIXES:
            if fts_table.endswith(suffix):
                base = fts_table[: -len(suffix)]
                if base in plan.tables:
                    base_tables_with_fts.add(base)
                break

    for base_table in base_tables_with_fts:
        new_name = plan.renamed_tables[base_table]
        fts_name = f"{new_name}_fts"

        # Get FTS definition from source
        try:
            src_conn = sqlite3.connect(str(source.path))
            fts_sql = src_conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (f"{base_table}_fts",)
            ).fetchone()
            src_conn.close()

            if fts_sql and fts_sql[0]:
                # Rewrite FTS definition
                new_fts_sql = fts_sql[0].replace(
                    f"{base_table}_fts", fts_name
                ).replace(
                    f"content={base_table}", f"content={new_name}"
                ).replace(
                    f"content='{base_table}'", f"content='{new_name}'"
                )
                try:
                    unified_conn.execute(new_fts_sql)
                    if verbose:
                        print(f"    Created FTS: {fts_name}")
                except sqlite3.OperationalError as e:
                    if "already exists" not in str(e):
                        errors.append(f"FTS create {fts_name} failed: {e}")
        except Exception as e:
            errors.append(f"FTS read for {base_table} failed: {e}")

    return errors


def copy_indexes(plan: MigrationPlan, unified_conn: sqlite3.Connection,
                 verbose: bool = False) -> List[str]:
    """Copy indexes from source to unified DB with renamed references."""
    errors = []
    source = plan.source

    if not source.path.exists():
        return errors

    try:
        src_conn = sqlite3.connect(str(source.path))
        indexes = src_conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ).fetchall()
        src_conn.close()

        for idx_name, idx_sql in indexes:
            if not idx_sql:
                continue
            # Skip FTS indexes
            if any(idx_name.startswith(ft) for ft in plan.fts_tables):
                continue

            new_sql = idx_sql
            new_idx_name = f"{source.prefix}_{idx_name}"

            # Replace table references
            for old_table, new_table in plan.renamed_tables.items():
                new_sql = new_sql.replace(f"ON {old_table}(", f"ON {new_table}(")
                new_sql = new_sql.replace(f'ON "{old_table}"(', f'ON "{new_table}"(')

            # Replace index name
            new_sql = new_sql.replace(idx_name, new_idx_name, 1)

            try:
                unified_conn.execute(new_sql)
                if verbose:
                    print(f"    Created index: {new_idx_name}")
            except sqlite3.OperationalError as e:
                if "already exists" not in str(e):
                    errors.append(f"Index {new_idx_name} failed: {e}")

    except Exception as e:
        errors.append(f"Index copy for {source.name} failed: {e}")

    return errors


# ============================================================================
# Metadata
# ============================================================================

def write_metadata(unified_conn: sqlite3.Connection, plans: List[MigrationPlan]):
    """Write migration metadata table to the unified DB."""
    unified_conn.execute("""
        CREATE TABLE IF NOT EXISTS _migration_metadata (
            source_name TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            prefix TEXT NOT NULL,
            tables_migrated TEXT NOT NULL,  -- JSON array
            total_rows INTEGER DEFAULT 0,
            migrated_at TEXT NOT NULL,
            errors TEXT DEFAULT '[]'
        )
    """)

    for plan in plans:
        if not plan.tables:
            continue
        total = sum(v for v in plan.row_counts.values() if v > 0)
        unified_conn.execute("""
            INSERT OR REPLACE INTO _migration_metadata
            (source_name, source_path, prefix, tables_migrated, total_rows, migrated_at, errors)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            plan.source.name,
            str(plan.source.path),
            plan.source.prefix,
            json.dumps(plan.tables),
            total,
            datetime.utcnow().isoformat(),
            json.dumps(plan.errors),
        ))


# ============================================================================
# Verification
# ============================================================================

def verify_migration(verbose: bool = False) -> bool:
    """Verify unified DB has all expected tables and data."""
    if not UNIFIED_DB_PATH.exists():
        print("ERROR: Unified DB not found at", UNIFIED_DB_PATH)
        return False

    conn = sqlite3.connect(str(UNIFIED_DB_PATH))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]

    print(f"\nUnified DB: {UNIFIED_DB_PATH}")
    print(f"Size: {UNIFIED_DB_PATH.stat().st_size / 1024:.1f} KB")
    print(f"Tables: {len(tables)}")

    # Check metadata
    if "_migration_metadata" in tables:
        meta = conn.execute("SELECT * FROM _migration_metadata").fetchall()
        print(f"\nMigration sources: {len(meta)}")
        for row in meta:
            src_name, src_path, prefix, tbl_json, total, migrated_at, errs = row
            tbls = json.loads(tbl_json)
            err_list = json.loads(errs)
            status = "OK" if not err_list else f"ERRORS: {len(err_list)}"
            print(f"  {src_name} ({prefix}_*): {len(tbls)} tables, {total} rows [{status}]")

    if verbose:
        print("\nAll tables:")
        for t in tables:
            if t.startswith("sqlite_") or t.startswith("_"):
                continue
            count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            print(f"  {t}: {count} rows")

    # Cross-check against sources
    print("\nCross-check against source DBs:")
    all_ok = True
    for source in SOURCES:
        if not source.path.exists():
            print(f"  {source.name}: SOURCE MISSING (skip)")
            continue
        src_conn = sqlite3.connect(str(source.path))
        for table in [r[0] for r in src_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]:
            if table.startswith("sqlite_") or any(table.endswith(s) for s in FTS_SUFFIXES):
                continue
            if _is_fts_table(src_conn, table):
                continue
            expected = f"{source.prefix}_{table}"
            if expected not in tables:
                print(f"  MISSING: {expected} (from {source.name}.{table})")
                all_ok = False
            else:
                src_count = src_conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                uni_count = conn.execute(f'SELECT COUNT(*) FROM "{expected}"').fetchone()[0]
                if src_count != uni_count:
                    print(f"  MISMATCH: {expected} src={src_count} unified={uni_count}")
                    all_ok = False
                elif verbose:
                    print(f"  OK: {expected} ({uni_count} rows)")
        src_conn.close()

    conn.close()
    if all_ok:
        print("\nAll checks passed.")
    return all_ok


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Consolidate Context DNA SQLite databases into one unified DB"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Analyze and show migration plan without executing")
    mode.add_argument("--execute", action="store_true",
                      help="Execute the migration")
    mode.add_argument("--verify", action="store_true",
                      help="Verify a completed migration")

    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed output")
    parser.add_argument("--backup", action="store_true",
                        help="Create backups before migrating (recommended)")
    args = parser.parse_args()

    if args.verify:
        ok = verify_migration(verbose=args.verbose)
        sys.exit(0 if ok else 1)

    # Analyze all sources
    print("=" * 60)
    print("SQLite Database Consolidation — Gap #8")
    print("=" * 60)
    print(f"\nTarget: {UNIFIED_DB_PATH}")
    print(f"\nExcluded (stay separate):")
    for name, reason in EXCLUDED.items():
        print(f"  {name}: {reason}")

    plans: List[MigrationPlan] = []
    total_rows = 0
    total_tables = 0

    print(f"\n{'Source DB':<35} {'Tables':>7} {'Rows':>8} {'Size':>8}  Status")
    print("-" * 80)

    for source in SOURCES:
        plan = analyze_source(source)
        plans.append(plan)

        rows = sum(v for v in plan.row_counts.values() if v > 0)
        total_rows += rows
        total_tables += len(plan.tables)

        size = f"{source.path.stat().st_size / 1024:.0f}K" if source.path.exists() else "N/A"
        status = "OK" if not plan.errors else f"WARN ({len(plan.errors)} issues)"
        if not source.path.exists():
            status = "MISSING"

        print(f"  {source.name:<33} {len(plan.tables):>7} {rows:>8} {size:>8}  {status}")

    print("-" * 80)
    print(f"  {'TOTAL':<33} {total_tables:>7} {total_rows:>8}")

    if args.verbose:
        print("\n\nTable mapping (old -> new):")
        for plan in plans:
            if plan.tables:
                print(f"\n  {plan.source.name}:")
                for old, new in plan.renamed_tables.items():
                    count = plan.row_counts.get(old, 0)
                    print(f"    {old:<30} -> {new:<35} ({count} rows)")
                if plan.fts_tables:
                    print(f"    FTS tables (rebuilt): {', '.join(plan.fts_tables)}")

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        print("\nTo execute: python scripts/consolidate-sqlite.py --execute --backup")

        # Show what code files need updating
        print("\n" + "=" * 60)
        print("POST-MIGRATION: Code files that need connection path updates")
        print("=" * 60)
        print("""
Files importing specific DB paths (update to use unified DB + prefix):

  memory/sqlite_storage.py        — get_db_path() → unified path
  memory/dialogue_mirror.py       — .dialogue_mirror.db → dlg_* tables
  memory/synaptic_voice.py        — multiple .db fallback queries
  memory/synaptic_personality.py   — .synaptic_personality.db
  memory/session_intent.py        — .session_intents.db (NOT in consolidation yet)
  memory/atlas_journal.py         — .synaptic_family_journal.db (NOT in consolidation yet)
  memory/synaptic_anatomy.py      — .synaptic_anatomy.db (NOT in consolidation yet)
  memory/code_chunk_indexer.py    — .code_chunks.db (NOT in consolidation yet)
  memory/anticipation_engine.py   — .anticipation_archive.db (NOT in consolidation yet)
  memory/recovery_agent.py        — context_dna.db
  memory/file_organization_analyzer.py — file_organization_backup.db, local_files.db
  memory/organization_patterns_library.py — organization_patterns.db
  memory/butler_repair_miner.py   — .repair_sops.db
  memory/complexity_vector_sentinel.py — complexity_vectors.db
  memory/butler_db_repair.py      — MANSION_DATABASES registry (central)
  memory/ab_autonomous.py         — .pattern_evolution.db
  memory/semantic_search.py       — .semantic_embeddings.db

Phase 2 candidates (DBs referenced but not on disk):
  .session_intents.db, .synaptic_family_journal.db, .synaptic_anatomy.db,
  .code_chunks.db, .anticipation_archive.db, context_dna.db,
  .observability.db, .pattern_evolution.db, .semantic_embeddings.db,
  .context_ab_tracking.db, .sop_enhancer.db, .webhook_quality.db,
  complexity_vectors.db, profiles.db, organization_patterns.db,
  file_organization_backup.db, local_files.db, FALLBACK_learnings.db,
  major_skills/skills.db
""")
        sys.exit(0)

    # Execute migration
    if args.execute:
        if args.backup:
            print(f"\nBacking up to {BACKUP_DIR}/")
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            for source in SOURCES:
                if source.path.exists():
                    dest = BACKUP_DIR / source.path.name
                    shutil.copy2(source.path, dest)
                    print(f"  {source.path.name} -> backup/")
            # Also backup unified if it exists
            if UNIFIED_DB_PATH.exists():
                shutil.copy2(UNIFIED_DB_PATH, BACKUP_DIR / "contextdna_unified.db.bak")

        print("\nMigrating...")
        unified_conn = sqlite3.connect(str(UNIFIED_DB_PATH))
        unified_conn.execute("PRAGMA journal_mode=WAL")
        unified_conn.execute("PRAGMA foreign_keys=OFF")  # During migration

        all_errors = []
        for plan in plans:
            if not plan.tables:
                continue
            print(f"\n  {plan.source.name}:")

            migrated, skipped, errors = migrate_source(plan, unified_conn,
                                                       verbose=args.verbose)
            idx_errors = copy_indexes(plan, unified_conn, verbose=args.verbose)
            fts_errors = recreate_fts_indexes(plan, unified_conn, verbose=args.verbose)

            all_errs = errors + idx_errors + fts_errors
            plan.errors.extend(all_errs)
            all_errors.extend(all_errs)

            print(f"    {migrated} rows migrated, {skipped} empty tables skipped", end="")
            if all_errs:
                print(f", {len(all_errs)} errors")
                for e in all_errs:
                    print(f"      ERROR: {e}")
            else:
                print()

        write_metadata(unified_conn, plans)
        unified_conn.commit()
        unified_conn.execute("PRAGMA foreign_keys=ON")
        unified_conn.close()

        print(f"\nUnified DB: {UNIFIED_DB_PATH}")
        print(f"Size: {UNIFIED_DB_PATH.stat().st_size / 1024:.1f} KB")

        if all_errors:
            print(f"\nWARNING: {len(all_errors)} errors occurred. Review above.")
            print("Run --verify to check data integrity.")
        else:
            print("\nMigration complete. Run --verify to confirm.")

        sys.exit(1 if all_errors else 0)


if __name__ == "__main__":
    main()
