#!/usr/bin/env python3
"""SQLite consolidation migration tool.

Consolidates multiple per-domain SQLite databases scattered around the repo
into a single DB at ``memory/contextdna.db`` using prefixed table names.

Source DBs are NEVER deleted or modified. The tool produces a tarball backup
before every migration and supports byte-identical restore.

Usage:
    python3 scripts/migrate-sqlite.py --inventory      # list source DBs (default)
    python3 scripts/migrate-sqlite.py --dry-run        # plan operations + row counts
    python3 scripts/migrate-sqlite.py --backup         # create .tar.gz of all sources
    python3 scripts/migrate-sqlite.py --migrate        # actually run migration
    python3 scripts/migrate-sqlite.py --verify         # row counts dest == sum(src)
    python3 scripts/migrate-sqlite.py --restore <tgz>  # restore from backup tarball

Constraints:
    - Stdlib only (no SQLAlchemy)
    - Read-only on source DBs (uri=True, mode=ro)
    - Transactional per source DB (BEGIN .. COMMIT/ROLLBACK)
    - Idempotent: re-running skips already-migrated tables via
      ``_consolidation_metadata``
    - WAL/SHM sidecar files copied into backup tarball when present
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = REPO_ROOT / "memory"
DEST_DB = MEMORY_DIR / "contextdna.db"
BACKUP_DIR = REPO_ROOT / ".sqlite-backups"


# ── Source catalog (path + table-prefix) ───────────────────────────────────


@dataclass
class SourceDb:
    """One source database to migrate.

    The ``prefix`` is prepended to every table name in the consolidated DB so
    domains never collide. ``domain`` is a logical grouping for reporting.
    """

    name: str
    path: Path
    prefix: str
    domain: str
    notes: str = ""


SOURCES: list[SourceDb] = [
    SourceDb(
        name="hindsight_validator",
        path=MEMORY_DIR / ".hindsight_validator.db",
        prefix="surgeon_hindsight_",
        domain="surgeon",
        notes="3-Surgeons hindsight validator (pending wins, miswiring)",
    ),
    SourceDb(
        name="strategic_plans",
        path=MEMORY_DIR / ".strategic_plans.db",
        prefix="synaptic_plans_",
        domain="synaptic",
        notes="Strategic planner state",
    ),
    SourceDb(
        name="outcome_tracking",
        path=MEMORY_DIR / ".outcome_tracking.db",
        prefix="evidence_outcomes_",
        domain="evidence",
        notes="Tracked outcomes for evidence ledger",
    ),
    SourceDb(
        name="failure_patterns",
        path=MEMORY_DIR / ".failure_patterns.db",
        prefix="surgeon_failures_",
        domain="surgeon",
        notes="Failure pattern registry",
    ),
    SourceDb(
        name="observability",
        path=MEMORY_DIR / ".observability.db",
        prefix="webhook_obs_",
        domain="webhook",
        notes="Observability pipeline (webhook injection events, claims, SOPs)",
    ),
    SourceDb(
        name="supervisor",
        path=REPO_ROOT / "multi-fleet" / ".contextdna" / "local" / "supervisor.db",
        prefix="fleet_supervisor_",
        domain="fleet",
        notes="Supervisor task lifecycle",
    ),
]


# ── Helpers ────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite database read-only, even when locked by a daemon (uri)."""
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _open_rw(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _list_tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return [(name, create_sql)] for user tables (skip sqlite_*)."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [(n, s or "") for n, s in rows]


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    """Count rows. Uses sqlite_master lookup to validate identifier first."""
    safe = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not safe:
        return 0
    # Identifier validated against sqlite_master — safe to interpolate quoted.
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def _ensure_metadata_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _consolidation_metadata (
            source_db        TEXT NOT NULL,
            source_path      TEXT NOT NULL,
            source_table     TEXT NOT NULL,
            dest_table       TEXT NOT NULL,
            row_count_source INTEGER NOT NULL,
            row_count_dest   INTEGER NOT NULL,
            migrated_at_utc  TEXT NOT NULL,
            PRIMARY KEY (source_db, source_table)
        )
        """
    )


def _is_migrated(conn: sqlite3.Connection, source_db: str, source_table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM _consolidation_metadata WHERE source_db=? AND source_table=?",
        (source_db, source_table),
    ).fetchone()
    return row is not None


def _rewrite_create_sql(create_sql: str, old: str, new: str) -> str:
    """Rewrite ``CREATE TABLE old`` -> ``CREATE TABLE new`` once, conservatively."""
    # SQLite always emits 'CREATE TABLE <name>' as the first three tokens.
    # We replace only the first occurrence of the bare/quoted identifier.
    for variant in (f'"{old}"', f"`{old}`", f"[{old}]", old):
        target = f"CREATE TABLE {variant}"
        idx = create_sql.find(target)
        if idx != -1:
            return create_sql.replace(target, f'CREATE TABLE "{new}"', 1)
    # Fallback: prepend a clean DDL we generate ourselves later.
    return ""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not rows:
        return []
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r[1] for r in info]


# ── Inventory & dry-run ────────────────────────────────────────────────────


def inventory() -> list[dict]:
    out: list[dict] = []
    for src in SOURCES:
        entry = {
            "name": src.name,
            "path": str(src.path),
            "prefix": src.prefix,
            "domain": src.domain,
            "exists": src.path.exists(),
            "size_bytes": 0,
            "tables": {},
            "total_rows": 0,
            "notes": src.notes,
        }
        if not src.path.exists():
            out.append(entry)
            continue
        entry["size_bytes"] = src.path.stat().st_size
        try:
            conn = _open_ro(src.path)
            try:
                total = 0
                for tname, _sql in _list_tables(conn):
                    cnt = _row_count(conn, tname)
                    entry["tables"][tname] = cnt
                    total += cnt
                entry["total_rows"] = total
            finally:
                conn.close()
        except sqlite3.Error as e:
            entry["error"] = str(e)
        out.append(entry)
    return out


def cmd_inventory() -> int:
    infos = inventory()
    print(f"SQLite source inventory ({len(infos)} DBs)")
    print("=" * 72)
    for info in infos:
        status = "MISSING" if not info["exists"] else f"{info['size_bytes']:,}B"
        print(f"\n[{info['name']}] domain={info['domain']} prefix={info['prefix']}")
        print(f"  path:   {info['path']}")
        print(f"  status: {status}")
        if info.get("error"):
            print(f"  ERROR:  {info['error']}")
            continue
        if info["tables"]:
            print(f"  tables ({len(info['tables'])}, {info['total_rows']:,} rows):")
            for t, c in sorted(info["tables"].items(), key=lambda x: -x[1]):
                print(f"    {info['prefix']}{t}: {c:,} rows  (was: {t})")
    return 0


def cmd_dry_run() -> int:
    """Print what --migrate would do without touching anything."""
    print("DRY RUN — no files will be modified")
    print("=" * 72)
    print(f"Destination: {DEST_DB}")
    dest_existing: dict[str, bool] = {}
    if DEST_DB.exists():
        try:
            dconn = _open_ro(DEST_DB)
            try:
                _ensure_metadata_table_in_memory_only = True  # marker
                for tname, _ in _list_tables(dconn):
                    dest_existing[tname] = True
            finally:
                dconn.close()
        except sqlite3.Error:
            pass

    total_planned = 0
    total_skipped = 0
    for src in SOURCES:
        print(f"\nSOURCE: {src.name} ({src.path})")
        if not src.path.exists():
            print("  [SKIP] missing on disk")
            continue
        try:
            conn = _open_ro(src.path)
        except sqlite3.Error as e:
            print(f"  [ERROR] cannot open: {e}")
            continue
        try:
            for tname, _sql in _list_tables(conn):
                cnt = _row_count(conn, tname)
                dest = f"{src.prefix}{tname}"
                already = dest in dest_existing
                marker = "SKIP (already migrated)" if already else "COPY"
                if already:
                    total_skipped += cnt
                else:
                    total_planned += cnt
                print(f"  [{marker}] {tname} -> {dest}  rows={cnt:,}")
        finally:
            conn.close()

    print("\n" + "=" * 72)
    print(f"Planned row copies: {total_planned:,}")
    print(f"Skipped (idempotent): {total_skipped:,}")
    print("Run with --migrate to execute. --backup is REQUIRED first.")
    return 0


# ── Backup & restore ───────────────────────────────────────────────────────


def cmd_backup() -> tuple[int, Optional[Path]]:
    """Create a tarball of all source DBs (incl. -wal/-shm sidecars)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    tgz = BACKUP_DIR / f".backup-{ts}.tar.gz"
    manifest = {"created_at_utc": _utc_now(), "files": []}
    with tarfile.open(tgz, "w:gz") as tar:
        for src in SOURCES:
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(src.path) + suffix)
                if not p.exists():
                    continue
                arcname = str(p.relative_to(REPO_ROOT))
                tar.add(str(p), arcname=arcname)
                manifest["files"].append(
                    {
                        "arcname": arcname,
                        "size": p.stat().st_size,
                        "source_name": src.name,
                    }
                )
        # Embed manifest as a tar entry
        import io

        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name="_manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
    print(f"Backup created: {tgz} ({tgz.stat().st_size:,} bytes, {len(manifest['files'])} files)")
    return 0, tgz


def cmd_restore(tgz_path: Path) -> int:
    """Extract a backup tarball back into the repo (overwriting source DBs)."""
    if not tgz_path.exists():
        print(f"ERROR: backup file not found: {tgz_path}", file=sys.stderr)
        return 2
    with tarfile.open(tgz_path, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name != "_manifest.json"]
        for m in members:
            # Hardening: refuse path traversal
            target = (REPO_ROOT / m.name).resolve()
            if REPO_ROOT.resolve() not in target.parents and target != REPO_ROOT.resolve():
                print(f"  REFUSED (outside repo): {m.name}", file=sys.stderr)
                continue
            tar.extract(m, path=REPO_ROOT)
            print(f"  restored: {m.name}")
    print(f"Restore complete from {tgz_path}")
    return 0


# ── Migration ──────────────────────────────────────────────────────────────


def cmd_migrate() -> int:
    """Copy rows from each source DB into the consolidated DB.

    Transactional per source DB. Idempotent via _consolidation_metadata.
    """
    DEST_DB.parent.mkdir(parents=True, exist_ok=True)
    dest = _open_rw(DEST_DB)
    try:
        _ensure_metadata_table(dest)
        for src in SOURCES:
            if not src.path.exists():
                print(f"[SKIP] {src.name}: missing on disk")
                continue
            try:
                ro = _open_ro(src.path)
            except sqlite3.Error as e:
                # Zero silent failures: surface and continue.
                print(f"[ERROR] {src.name}: cannot open ({e})")
                continue
            try:
                _migrate_one(src, ro, dest)
            finally:
                ro.close()
    finally:
        dest.commit()
        dest.close()
    print(f"\nMigration complete. Destination: {DEST_DB}")
    print("Run with --verify to confirm row counts.")
    return 0


def _migrate_one(src: SourceDb, ro: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    """Migrate every table of one source DB inside a single transaction."""
    print(f"\n[MIGRATE] {src.name} -> prefix={src.prefix}")
    dest.execute("BEGIN")
    try:
        for tname, create_sql in _list_tables(ro):
            dest_table = f"{src.prefix}{tname}"
            if _is_migrated(dest, src.name, tname):
                print(f"  [skip] {tname} -> {dest_table} (already migrated)")
                continue
            cols = _columns(ro, tname)
            if not cols:
                print(f"  [warn] {tname}: no columns, skipping")
                continue
            # Create destination table with prefixed name + extra timestamps.
            new_ddl = _rewrite_create_sql(create_sql, tname, dest_table)
            if not new_ddl:
                # Generic fallback: TEXT columns by default — rarely hit.
                col_defs = ", ".join(f'"{c}" TEXT' for c in cols)
                new_ddl = f'CREATE TABLE "{dest_table}" ({col_defs})'
            dest.execute(f'DROP TABLE IF EXISTS "{dest_table}"')
            dest.execute(new_ddl)
            # Add timestamp columns if not present (harmless if already there).
            for ts_col in ("created_at", "updated_at"):
                if ts_col not in cols:
                    try:
                        dest.execute(
                            f'ALTER TABLE "{dest_table}" ADD COLUMN '
                            f"{ts_col} TEXT DEFAULT CURRENT_TIMESTAMP"
                        )
                    except sqlite3.OperationalError:
                        pass
            # Copy rows (parameterized).
            placeholders = ",".join("?" for _ in cols)
            col_list = ",".join(f'"{c}"' for c in cols)
            rows = ro.execute(f'SELECT {col_list} FROM "{tname}"').fetchall()
            if rows:
                dest.executemany(
                    f'INSERT INTO "{dest_table}" ({col_list}) VALUES ({placeholders})',
                    rows,
                )
            src_count = len(rows)
            dest_count = dest.execute(
                f'SELECT COUNT(*) FROM "{dest_table}"'
            ).fetchone()[0]
            dest.execute(
                "INSERT OR REPLACE INTO _consolidation_metadata "
                "(source_db, source_path, source_table, dest_table, "
                " row_count_source, row_count_dest, migrated_at_utc) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    src.name,
                    str(src.path),
                    tname,
                    dest_table,
                    src_count,
                    dest_count,
                    _utc_now(),
                ),
            )
            print(f"  [ok]   {tname} -> {dest_table}  rows={src_count:,}")
        dest.execute("COMMIT")
    except Exception as e:
        dest.execute("ROLLBACK")
        print(f"[ROLLBACK] {src.name}: {e}")
        raise


# ── Verify ─────────────────────────────────────────────────────────────────


def cmd_verify() -> int:
    if not DEST_DB.exists():
        print(f"ERROR: consolidated DB not found at {DEST_DB}", file=sys.stderr)
        return 2
    dest = sqlite3.connect(str(DEST_DB), timeout=5)
    try:
        meta_rows = dest.execute(
            "SELECT source_db, source_table, dest_table, "
            "row_count_source, row_count_dest FROM _consolidation_metadata"
        ).fetchall()
    finally:
        dest.close()
    mismatches = 0
    print(f"Verifying {len(meta_rows)} migrated tables")
    print("=" * 72)
    for sdb, stable, dtable, src_n, dst_n in meta_rows:
        ok = src_n == dst_n
        flag = "OK " if ok else "FAIL"
        if not ok:
            mismatches += 1
        print(f"  [{flag}] {sdb}.{stable} -> {dtable}  src={src_n:,} dst={dst_n:,}")
    if mismatches:
        print(f"\n{mismatches} mismatch(es) — investigate before deleting sources.")
        return 1
    print("\nAll row counts match.")
    return 0


# ── Entry point ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Consolidate scattered SQLite DBs into memory/contextdna.db"
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--inventory", action="store_true", help="List source DBs (default)")
    g.add_argument("--dry-run", action="store_true", help="Plan migration ops")
    g.add_argument("--backup", action="store_true", help="Create .tar.gz of source DBs")
    g.add_argument("--migrate", action="store_true", help="Run migration (after backup)")
    g.add_argument("--verify", action="store_true", help="Compare row counts src vs dst")
    g.add_argument("--restore", metavar="TARBALL", help="Restore source DBs from backup")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        return cmd_dry_run()
    if args.backup:
        rc, _ = cmd_backup()
        return rc
    if args.migrate:
        return cmd_migrate()
    if args.verify:
        return cmd_verify()
    if args.restore:
        return cmd_restore(Path(args.restore))
    return cmd_inventory()


if __name__ == "__main__":
    sys.exit(main())
