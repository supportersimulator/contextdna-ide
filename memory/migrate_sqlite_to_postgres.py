#!/usr/bin/env python3
"""
SQLite to PostgreSQL Migration Script for Context DNA

Migrates data from existing SQLite databases to the new PostgreSQL schema.

Sources:
- ~/.context-dna/learnings.db → cd_learnings
- ~/.context-dna/.pattern_evolution.db → cd_hook_variants, cd_session_context, cd_prompt_patterns
- memory/.sop_enhancer.db → cd_sop_hashes
- memory/.context_ab_tracking.db → cd_ab_tests

Usage:
    python memory/migrate_sqlite_to_postgres.py [--dry-run] [--verbose]
"""

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

# Try to import psycopg2, provide helpful error if missing
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, execute_values
except ImportError:
    print("ERROR: psycopg2 not installed. Install with: pip install psycopg2-binary")
    sys.exit(1)


# ============================================================================
# Configuration
# ============================================================================

# SQLite database paths
SQLITE_PATHS = {
    "learnings": Path.home() / ".context-dna" / "learnings.db",
    "pattern_evolution": Path.home() / ".context-dna" / ".pattern_evolution.db",
    "pattern_registry": Path.home() / ".context-dna" / ".pattern_registry.db",
    "sop_enhancer": Path(__file__).parent / ".sop_enhancer.db",
    "ab_tracking": Path(__file__).parent / ".context_ab_tracking.db",
}

# PostgreSQL connection (context-dna stack, container: context-dna-postgres)
# LEARNINGS_DB_* takes priority over DATABASE_* to prevent .env hijacking
PG_CONFIG = {
    "host": os.environ.get("LEARNINGS_DB_HOST", os.environ.get("DATABASE_HOST", "127.0.0.1")),
    "port": int(os.environ.get("LEARNINGS_DB_PORT", os.environ.get("DATABASE_PORT", "5432"))),
    "database": os.environ.get("LEARNINGS_DB_NAME", os.environ.get("DATABASE_NAME", "context_dna")),
    "user": os.environ.get("LEARNINGS_DB_USER", os.environ.get("DATABASE_USER", "context_dna")),
    "password": os.environ.get("LEARNINGS_DB_PASSWORD", os.environ.get("DATABASE_PASSWORD", "context_dna_dev")),
}


# ============================================================================
# Migration Classes
# ============================================================================

class MigrationStats:
    """Track migration statistics."""
    def __init__(self):
        self.migrated = 0
        self.skipped = 0
        self.errors = 0
        self.tables = {}

    def add(self, table: str, count: int, skipped: int = 0, errors: int = 0):
        self.tables[table] = {"migrated": count, "skipped": skipped, "errors": errors}
        self.migrated += count
        self.skipped += skipped
        self.errors += errors

    def summary(self) -> str:
        lines = ["\n" + "=" * 60]
        lines.append("MIGRATION SUMMARY")
        lines.append("=" * 60)
        for table, stats in self.tables.items():
            lines.append(f"  {table}: {stats['migrated']} migrated, {stats['skipped']} skipped, {stats['errors']} errors")
        lines.append("-" * 60)
        lines.append(f"  TOTAL: {self.migrated} migrated, {self.skipped} skipped, {self.errors} errors")
        lines.append("=" * 60)
        return "\n".join(lines)


class SQLitePostgresMigrator:
    """Handles migration from SQLite to PostgreSQL."""

    def __init__(self, dry_run: bool = False, verbose: bool = False):
        self.dry_run = dry_run
        self.verbose = verbose
        self.pg_conn: Optional[psycopg2.extensions.connection] = None
        self.stats = MigrationStats()

    def log(self, msg: str, level: str = "INFO"):
        """Log a message."""
        if level == "DEBUG" and not self.verbose:
            return
        prefix = "DRY RUN: " if self.dry_run else ""
        print(f"[{level}] {prefix}{msg}")

    def connect_postgres(self):
        """Connect to PostgreSQL."""
        self.log(f"Connecting to PostgreSQL at {PG_CONFIG['host']}:{PG_CONFIG['port']}")
        self.pg_conn = psycopg2.connect(**PG_CONFIG)
        self.pg_conn.autocommit = False
        self.log("PostgreSQL connection established", "DEBUG")

    def close(self):
        """Close connections."""
        if self.pg_conn:
            self.pg_conn.close()

    def sqlite_connect(self, path: Path) -> Optional[sqlite3.Connection]:
        """Connect to a SQLite database."""
        if not path.exists():
            self.log(f"SQLite database not found: {path}", "WARN")
            return None
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn

    # ========================================================================
    # Learnings Migration
    # ========================================================================

    def migrate_learnings(self):
        """Migrate learnings from learnings.db to cd_learnings."""
        self.log("Migrating learnings...")
        sqlite_conn = self.sqlite_connect(SQLITE_PATHS["learnings"])
        if not sqlite_conn:
            return

        cursor = sqlite_conn.cursor()
        cursor.execute("SELECT * FROM learnings")
        rows = cursor.fetchall()
        self.log(f"Found {len(rows)} learnings to migrate", "DEBUG")

        migrated = 0
        skipped = 0
        errors = 0

        pg_cursor = self.pg_conn.cursor()

        for row in rows:
            try:
                # Check if already migrated by legacy_id
                pg_cursor.execute(
                    "SELECT id FROM cd_learnings WHERE legacy_id = %s",
                    (row["id"],)
                )
                if pg_cursor.fetchone():
                    skipped += 1
                    self.log(f"Skipping already migrated: {row['id']}", "DEBUG")
                    continue

                # Parse tags JSON
                try:
                    tags = json.loads(row["tags"]) if row["tags"] else []
                except json.JSONDecodeError:
                    tags = []

                # Parse metadata JSON
                try:
                    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                except json.JSONDecodeError:
                    metadata = {}

                if not self.dry_run:
                    pg_cursor.execute("""
                        INSERT INTO cd_learnings (
                            legacy_id, type, title, content, tags, session_id, source, metadata, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        row["id"],
                        row["type"],
                        row["title"],
                        row["content"],
                        json.dumps(tags),
                        row["session_id"] or None,
                        row["source"] or "manual",
                        json.dumps(metadata),
                        row["created_at"] or datetime.now().isoformat()
                    ))

                migrated += 1
                self.log(f"Migrated learning: {row['title'][:50]}...", "DEBUG")

            except Exception as e:
                errors += 1
                self.log(f"Error migrating learning {row['id']}: {e}", "ERROR")

        if not self.dry_run:
            self.pg_conn.commit()

        sqlite_conn.close()
        self.stats.add("cd_learnings", migrated, skipped, errors)
        self.log(f"Learnings migration: {migrated} migrated, {skipped} skipped, {errors} errors")

    # ========================================================================
    # Pattern Evolution Migration
    # ========================================================================

    def migrate_pattern_evolution(self):
        """Migrate data from pattern_evolution.db."""
        self.log("Migrating pattern evolution data...")
        sqlite_conn = self.sqlite_connect(SQLITE_PATHS["pattern_evolution"])
        if not sqlite_conn:
            return

        cursor = sqlite_conn.cursor()
        pg_cursor = self.pg_conn.cursor()

        # Migrate hook_variants
        self._migrate_hook_variants(cursor, pg_cursor)

        # Migrate prompt_patterns
        self._migrate_prompt_patterns(cursor, pg_cursor)

        # Note: session_context table is large (475 rows) but may not need full migration
        # We'll migrate a summary instead
        self._migrate_session_context_summary(cursor, pg_cursor)

        if not self.dry_run:
            self.pg_conn.commit()

        sqlite_conn.close()

    def _migrate_hook_variants(self, sqlite_cursor, pg_cursor):
        """Migrate hook_variants table."""
        try:
            sqlite_cursor.execute("SELECT * FROM hook_variants")
            rows = sqlite_cursor.fetchall()
        except sqlite3.OperationalError:
            self.log("hook_variants table not found", "WARN")
            return

        self.log(f"Found {len(rows)} hook variants to migrate", "DEBUG")
        migrated = 0
        skipped = 0

        for row in rows:
            try:
                pg_cursor.execute(
                    "SELECT id FROM cd_hook_variants WHERE legacy_id = %s",
                    (row["id"],)
                )
                if pg_cursor.fetchone():
                    skipped += 1
                    continue

                if not self.dry_run:
                    pg_cursor.execute("""
                        INSERT INTO cd_hook_variants (
                            legacy_id, hook_id, variant_type, pattern_signature,
                            success_rate, sample_count, metadata, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        row["id"],
                        row.get("hook_id", "unknown"),
                        row.get("variant_type", "unknown"),
                        row.get("pattern_signature"),
                        row.get("success_rate", 0.0),
                        row.get("sample_count", 0),
                        json.dumps(dict(row)),
                        datetime.now().isoformat()
                    ))
                migrated += 1

            except Exception as e:
                self.log(f"Error migrating hook variant: {e}", "ERROR")

        self.stats.add("cd_hook_variants", migrated, skipped)
        self.log(f"Hook variants: {migrated} migrated, {skipped} skipped")

    def _migrate_prompt_patterns(self, sqlite_cursor, pg_cursor):
        """Migrate prompt_patterns table."""
        try:
            sqlite_cursor.execute("SELECT * FROM prompt_patterns")
            rows = sqlite_cursor.fetchall()
        except sqlite3.OperationalError:
            self.log("prompt_patterns table not found", "WARN")
            return

        self.log(f"Found {len(rows)} prompt patterns to migrate", "DEBUG")
        migrated = 0
        skipped = 0

        for row in rows:
            try:
                pg_cursor.execute(
                    "SELECT id FROM cd_prompt_patterns WHERE legacy_id = %s",
                    (row["id"],)
                )
                if pg_cursor.fetchone():
                    skipped += 1
                    continue

                if not self.dry_run:
                    pg_cursor.execute("""
                        INSERT INTO cd_prompt_patterns (
                            legacy_id, pattern_type, pattern_signature,
                            examples, frequency, effectiveness_score, metadata, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        row["id"],
                        row.get("pattern_type", "unknown"),
                        row.get("pattern_signature"),
                        json.dumps([]),  # Examples would need parsing
                        row.get("frequency", 0),
                        row.get("effectiveness_score", 0.0),
                        json.dumps(dict(row)),
                        datetime.now().isoformat()
                    ))
                migrated += 1

            except Exception as e:
                self.log(f"Error migrating prompt pattern: {e}", "ERROR")

        self.stats.add("cd_prompt_patterns", migrated, skipped)
        self.log(f"Prompt patterns: {migrated} migrated, {skipped} skipped")

    def _migrate_session_context_summary(self, sqlite_cursor, pg_cursor):
        """Record session context statistics (large table, summarize only)."""
        try:
            sqlite_cursor.execute("SELECT COUNT(*) as count FROM session_context")
            count = sqlite_cursor.fetchone()["count"]
            self.log(f"Session context has {count} rows (not migrating individual rows)", "INFO")

            # Record as an event for audit trail
            if not self.dry_run:
                pg_cursor.execute("""
                    SELECT record_event(
                        'migration_note',
                        'migrate_script',
                        %s,
                        NULL, NULL, NULL
                    )
                """, (json.dumps({
                    "message": f"SQLite session_context had {count} rows at migration time",
                    "table": "session_context",
                    "row_count": count,
                    "migration_date": datetime.now().isoformat()
                }),))

        except sqlite3.OperationalError:
            self.log("session_context table not found", "WARN")

    # ========================================================================
    # SOP Enhancer Migration
    # ========================================================================

    def migrate_sop_enhancer(self):
        """Migrate SOP hash tracking from .sop_enhancer.db."""
        self.log("Migrating SOP enhancer data...")
        sqlite_conn = self.sqlite_connect(SQLITE_PATHS["sop_enhancer"])
        if not sqlite_conn:
            return

        cursor = sqlite_conn.cursor()
        pg_cursor = self.pg_conn.cursor()

        # Check for hash tables
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row["name"] for row in cursor.fetchall()]
            self.log(f"Found tables in sop_enhancer: {tables}", "DEBUG")

            for table in tables:
                if "hash" in table.lower():
                    cursor.execute(f"SELECT * FROM {table}")
                    rows = cursor.fetchall()
                    self.log(f"Found {len(rows)} rows in {table}", "DEBUG")

                    migrated = 0
                    for row in rows:
                        if not self.dry_run:
                            try:
                                pg_cursor.execute("""
                                    INSERT INTO cd_sop_hashes (hash, first_seen_at, occurrence_count)
                                    VALUES (%s, %s, %s)
                                    ON CONFLICT (hash) DO UPDATE SET
                                        occurrence_count = cd_sop_hashes.occurrence_count + 1,
                                        last_seen_at = NOW()
                                """, (
                                    str(dict(row).get("hash", str(row))),
                                    datetime.now().isoformat(),
                                    1
                                ))
                                migrated += 1
                            except Exception as e:
                                self.log(f"Error inserting hash: {e}", "ERROR")

                    self.stats.add("cd_sop_hashes", migrated)

        except Exception as e:
            self.log(f"Error reading sop_enhancer: {e}", "ERROR")

        if not self.dry_run:
            self.pg_conn.commit()

        sqlite_conn.close()

    # ========================================================================
    # A/B Tracking Migration
    # ========================================================================

    def migrate_ab_tracking(self):
        """Migrate A/B test data from .context_ab_tracking.db."""
        self.log("Migrating A/B tracking data...")
        sqlite_conn = self.sqlite_connect(SQLITE_PATHS["ab_tracking"])
        if not sqlite_conn:
            return

        cursor = sqlite_conn.cursor()
        pg_cursor = self.pg_conn.cursor()

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row["name"] for row in cursor.fetchall()]
            self.log(f"Found tables in ab_tracking: {tables}", "DEBUG")

            # Migrate any test results found
            for table in tables:
                cursor.execute(f"SELECT * FROM {table}")
                rows = cursor.fetchall()
                self.log(f"Found {len(rows)} rows in {table}", "DEBUG")

                if rows:
                    # Record as event for now
                    if not self.dry_run:
                        pg_cursor.execute("""
                            SELECT record_event(
                                'ab_test_migration',
                                'migrate_script',
                                %s,
                                NULL, NULL, NULL
                            )
                        """, (json.dumps({
                            "table": table,
                            "row_count": len(rows),
                            "sample_data": [dict(row) for row in rows[:5]],
                            "migration_date": datetime.now().isoformat()
                        }),))

        except Exception as e:
            self.log(f"Error migrating ab_tracking: {e}", "ERROR")

        if not self.dry_run:
            self.pg_conn.commit()

        sqlite_conn.close()

    # ========================================================================
    # Main Migration Entry Point
    # ========================================================================

    def run_full_migration(self):
        """Run the complete migration."""
        self.log("Starting SQLite to PostgreSQL migration")
        self.log(f"Dry run mode: {self.dry_run}")

        try:
            self.connect_postgres()

            # Run all migrations
            self.migrate_learnings()
            self.migrate_pattern_evolution()
            self.migrate_sop_enhancer()
            self.migrate_ab_tracking()

            # Record migration event
            if not self.dry_run:
                pg_cursor = self.pg_conn.cursor()
                pg_cursor.execute("""
                    SELECT record_event(
                        'migration_completed',
                        'migrate_script',
                        %s,
                        NULL, NULL, NULL
                    )
                """, (json.dumps({
                    "stats": self.stats.tables,
                    "total_migrated": self.stats.migrated,
                    "total_skipped": self.stats.skipped,
                    "total_errors": self.stats.errors,
                    "completed_at": datetime.now().isoformat()
                }),))
                self.pg_conn.commit()

            print(self.stats.summary())

        except Exception as e:
            self.log(f"Migration failed: {e}", "ERROR")
            if self.pg_conn:
                self.pg_conn.rollback()
            raise

        finally:
            self.close()


# ============================================================================
# CLI Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Migrate Context DNA data from SQLite to PostgreSQL"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without making changes"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output"
    )
    args = parser.parse_args()

    migrator = SQLitePostgresMigrator(dry_run=args.dry_run, verbose=args.verbose)
    migrator.run_full_migration()


if __name__ == "__main__":
    main()
