"""
Unified Sync Engine — ONE engine for ALL SQLite↔PG synchronization.

Replaces 3 separate sync systems:
  1. observability_store.py adaptive bridge (psycopg2, sync)
  2. bidirectional_sync.py BidirectionalSyncStore (asyncpg, async)
  3. sync_integration.py wrapper (async)

Features:
  - Sync + async entry points (lite_scheduler + agent_service)
  - PG advisory lock coordination (2 daemons, 1 sync at a time)
  - Per-table conflict policies from sync_config registry
  - Adaptive schema discovery (new tables + columns auto-mirrored)
  - Mode detection + transition handling (lite↔heavy)
  - 2 SQLite DBs → 2 PG databases
  - Boolean/type casting both directions
  - machine_id injection for hierarchy tables
  - Audit trail in sync_history

Usage:
    # Sync (lite_scheduler):
    engine = get_sync_engine()
    report = engine.sync_all(caller="lite_scheduler")

    # Async (agent_service):
    report = await engine.async_sync_all(caller="agent_service")

    # Write-through (immediate mirror):
    engine.mirror_row("claim", {"claim_id": "...", ...})
"""

import json
import hashlib
import logging
import os
import sqlite3
import socket
import time
import uuid
from asyncio import get_event_loop
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from memory.sync_config import (
    ALL_SYNC_TABLES,
    OBSERVABILITY_DB,
    PG_TARGETS,
    PROFILES_DB,
    SQLITE_DBS,
    SQLITE_TO_PG_TYPE,
    SYNC_EXCLUDE,
    ConflictPolicy,
    SyncDirection,
    SyncTableConfig,
    get_syncable_tables,
    make_discovered_config,
)
from memory.sync_lock import sync_advisory_lock, SYNC_LOCK_ID

logger = logging.getLogger("contextdna.unified_sync")

# Thread pool for async wrapping
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sync")


# =========================================================================
# SYNC REPORT
# =========================================================================

@dataclass
class SyncReport:
    """Structured result from a sync cycle."""
    success: bool = True
    caller: str = ""
    mode_before: str = "unknown"
    mode_after: str = "unknown"
    lock_acquired: bool = False
    lock_contention: bool = False
    pg_targets_available: Dict[str, bool] = field(default_factory=dict)
    schema_results: Dict[str, str] = field(default_factory=dict)
    pushed: Dict[str, int] = field(default_factory=dict)
    pulled: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    duration_ms: int = 0
    tables_synced: int = 0
    total_pushed: int = 0
    total_pulled: int = 0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "caller": self.caller,
            "mode_before": self.mode_before,
            "mode_after": self.mode_after,
            "lock_acquired": self.lock_acquired,
            "lock_contention": self.lock_contention,
            "pg_targets_available": self.pg_targets_available,
            "tables_synced": self.tables_synced,
            "total_pushed": self.total_pushed,
            "total_pulled": self.total_pulled,
            "errors": self.errors[:5],  # Cap error list
            "duration_ms": self.duration_ms,
        }


# =========================================================================
# UNIFIED SYNC ENGINE
# =========================================================================

class UnifiedSyncEngine:
    """
    Core sync engine. Handles ALL SQLite↔PG synchronization.

    - Connects to 2 SQLite DBs and 2 PG databases
    - Uses psycopg2 (sync) — async via run_in_executor
    - PG advisory lock prevents concurrent sync
    - Adaptive schema discovery for new tables/columns
    """

    def __init__(self):
        self._sqlite_conns: Dict[str, sqlite3.Connection] = {}
        self._pg_conns: Dict[str, Any] = {}  # psycopg2 connections
        self._bool_col_cache: Dict[str, Set[str]] = {}
        self._array_col_cache: Dict[str, Set[str]] = {}
        self._machine_id: Optional[str] = None
        self._initialized = False

    # -----------------------------------------------------------------
    # INITIALIZATION
    # -----------------------------------------------------------------

    def _ensure_init(self):
        """Lazy init — connect to SQLite DBs on first use."""
        if self._initialized:
            return
        for name, path in SQLITE_DBS.items():
            if path.exists():
                try:
                    conn = sqlite3.connect(str(path), check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    self._sqlite_conns[name] = conn
                except Exception as e:
                    logger.warning(f"cannot open SQLite {name} at {path}: {e}")
        self._initialized = True

    def _get_machine_id(self) -> str:
        """Stable machine identifier: sha256(hostname:user)[:16]."""
        if self._machine_id:
            return self._machine_id
        hostname = socket.gethostname()
        username = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
        self._machine_id = hashlib.sha256(
            f"{hostname}:{username}".encode()
        ).hexdigest()[:16]
        return self._machine_id

    # -----------------------------------------------------------------
    # PG CONNECTION MANAGEMENT
    # -----------------------------------------------------------------

    def _try_pg_connect(self, target_name: str) -> bool:
        """Connect to a PG target. Returns True if connected."""
        # Check existing connection
        conn = self._pg_conns.get(target_name)
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return True
            except Exception:
                self._pg_conns.pop(target_name, None)

        target = PG_TARGETS.get(target_name)
        if not target:
            return False

        try:
            import psycopg2
            conn = psycopg2.connect(
                dbname=target.database,
                user=target.user,
                password=target.password,
                host=target.host,
                port=target.port,
                connect_timeout=5,
            )
            self._pg_conns[target_name] = conn
            return True
        except Exception as e:
            logger.debug(f"PG {target_name} unavailable: {e}")
            return False

    def _get_pg(self, target_name: str):
        """Get PG connection for a target, or None."""
        if self._try_pg_connect(target_name):
            return self._pg_conns[target_name]
        return None

    def _ensure_contextdna_database(self):
        """
        Create the 'contextdna' database if it doesn't exist.
        Connects to the default 'context_dna' DB and issues CREATE DATABASE.
        """
        main_conn = self._get_pg("context_dna")
        if not main_conn:
            return
        try:
            # autocommit required for CREATE DATABASE
            old_autocommit = main_conn.autocommit
            main_conn.autocommit = True
            with main_conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = 'contextdna'"
                )
                if not cur.fetchone():
                    cur.execute('CREATE DATABASE "contextdna"')
                    logger.info("created 'contextdna' database")
            main_conn.autocommit = old_autocommit
        except Exception as e:
            logger.debug(f"contextdna DB check: {e}")
            try:
                main_conn.autocommit = False
            except Exception:
                pass

    # -----------------------------------------------------------------
    # MODE DETECTION
    # -----------------------------------------------------------------

    def detect_mode(self) -> str:
        """Detect mode via mode_authority (single source of truth)."""
        # Old: self._try_pg_connect('context_dna') — PG-only check, bypassed env/Redis precedence
        from memory.mode_authority import get_mode
        return get_mode()

    def _get_current_mode(self) -> str:
        """Read mode from mode_sync_state singleton."""
        self._ensure_init()
        conn = self._sqlite_conns.get("observability")
        if not conn:
            return "lite"
        try:
            row = conn.execute(
                "SELECT current_mode FROM mode_sync_state WHERE id = 'singleton'"
            ).fetchone()
            return row[0] if row else "lite"
        except Exception:
            return "lite"

    def _update_mode_state(self, mode: str, pg_available: bool):
        """Update mode_sync_state singleton."""
        conn = self._sqlite_conns.get("observability")
        if not conn:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute("""
                UPDATE mode_sync_state
                SET current_mode = ?, postgres_available = ?,
                    last_sync_at = ?, updated_at = ?
                WHERE id = 'singleton'
            """, (mode, 1 if pg_available else 0, now, now))
            conn.commit()
        except Exception as e:
            logger.warning(f"mode state update failed: {e}")

    # -----------------------------------------------------------------
    # MODE TRANSITION
    # -----------------------------------------------------------------

    def handle_mode_transition(self, old_mode: str, new_mode: str) -> Dict[str, Any]:
        """
        Handle mode transitions:
          lite→heavy: flush sync_queue (queued offline writes → PG)
          heavy→lite: update state (PG went down)
        """
        result = {"transition": f"{old_mode}→{new_mode}", "items_flushed": 0}

        # Broadcast via Redis pub/sub so watchdog, UI, and other services know immediately
        try:
            import redis
            r = redis.Redis(host='127.0.0.1', port=6379, socket_timeout=2)
            r.publish('contextdna:mode_transition', json.dumps({
                'old_mode': old_mode,
                'new_mode': new_mode,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'source': 'unified_sync'
            }))
            logger.debug(f"[SYNC] mode transition broadcast: {old_mode}→{new_mode}")
        except Exception as e:
            logger.debug(f"Mode broadcast failed (non-critical): {e}")

        # === CONNECTION DRAIN: close stale connections before transition ===
        # This prevents concurrent SQLite connections (root cause of DB corruption)
        try:
            # 1. WAL checkpoint existing SQLite connections before closing
            for db_name, conn in self._sqlite_conns.items():
                try:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    logger.debug(f"[SYNC] WAL checkpoint {db_name} before transition")
                except Exception:
                    pass
            # 2. Reset ObservabilityStore singleton (forces fresh connection post-transition)
            try:
                import memory.observability_store as obs_mod
                if hasattr(obs_mod, '_store_instance') and obs_mod._store_instance is not None:
                    obs_mod._store_instance = None
                    logger.debug("[SYNC] Reset ObservabilityStore singleton for transition")
            except Exception:
                pass
            # 3. Close and clear all sync engine connections
            self.close()
            logger.info(f"[SYNC] Connection drain complete for {old_mode}→{new_mode}")
        except Exception as e:
            logger.warning(f"[SYNC] Connection drain partial: {e}")

        # Re-initialize connections for new mode
        self.__init__()
        self._ensure_init()  # Re-open SQLite DBs after reset

        if old_mode == "lite" and new_mode == "heavy":
            # Flush sync_queue to PG
            result["items_flushed"] = self._flush_sync_queue()
            logger.info(
                f"[SYNC] lite→heavy: flushed {result['items_flushed']} queued items"
            )

        elif old_mode == "heavy" and new_mode == "lite":
            logger.info("[SYNC] heavy→lite: PG went down, continuing in lite mode")

        # Record transition in sync_history
        self._record_sync_history(
            trigger=f"mode_transition_{old_mode}_to_{new_mode}",
            from_mode=old_mode,
            to_mode=new_mode,
            items_synced=result["items_flushed"],
        )

        return result

    def _flush_sync_queue(self) -> int:
        """Process pending sync_queue items (lite→heavy recovery)."""
        conn = self._sqlite_conns.get("observability")
        if not conn:
            return 0

        try:
            rows = conn.execute("""
                SELECT id, table_name, record_id, operation, data_json, target_mode
                FROM sync_queue
                WHERE status = 'pending'
                ORDER BY priority ASC, created_at ASC
                LIMIT 500
            """).fetchall()
        except Exception:
            return 0

        flushed = 0
        for row in rows:
            row_d = dict(row)
            table = row_d["table_name"]
            try:
                data = json.loads(row_d["data_json"])
                cfg = ALL_SYNC_TABLES.get(table)
                if cfg:
                    pg_conn = self._get_pg(cfg.pg_target)
                    if pg_conn:
                        self._push_row(pg_conn, table, data, cfg)
                        pg_conn.commit()  # Commit PG BEFORE marking SQLite complete
                        conn.execute(
                            "UPDATE sync_queue SET status = 'completed', "
                            "completed_at = ? WHERE id = ?",
                            (datetime.now(timezone.utc).isoformat(), row_d["id"]),
                        )
                        flushed += 1
            except Exception as e:
                conn.execute(
                    "UPDATE sync_queue SET status = 'failed', "
                    "last_error = ?, retry_count = retry_count + 1 WHERE id = ?",
                    (str(e)[:200], row_d["id"]),
                )
        conn.commit()
        return flushed

    # -----------------------------------------------------------------
    # SCHEMA SYNC
    # -----------------------------------------------------------------

    def _to_pg_type(self, sqlite_type: str) -> str:
        """Map SQLite type affinity to PostgreSQL type."""
        upper = (sqlite_type or "").upper().strip()
        for k, v in SQLITE_TO_PG_TYPE.items():
            if k in upper:
                return v
        return "TEXT"

    def _ensure_pg_table(
        self, pg_conn, table_name: str, sqlite_conn: sqlite3.Connection
    ) -> bool:
        """Auto-create or update PG table to mirror SQLite schema."""
        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = %s AND table_schema = 'public'",
                    (table_name,),
                )
                if cur.fetchone():
                    return self._align_pg_columns(pg_conn, table_name, sqlite_conn)

            # Table doesn't exist — create from SQLite schema
            pragma = sqlite_conn.execute(
                f"PRAGMA table_info('{table_name}')"
            ).fetchall()
            if not pragma:
                return False

            parts, pks = [], []
            for _, name, ctype, notnull, dflt, pk in pragma:
                pg_t = self._to_pg_type(ctype)
                frag = f'"{name}" {pg_t}'
                if notnull:
                    frag += " NOT NULL"
                if dflt is not None:
                    d = str(dflt)
                    if d.upper() in ("CURRENT_TIMESTAMP", "'CURRENT_TIMESTAMP'"):
                        frag += " DEFAULT CURRENT_TIMESTAMP"
                    elif d.startswith("'") and d.endswith("'"):
                        frag += f" DEFAULT {d}"
                    else:
                        frag += f" DEFAULT {d}"
                if pk:
                    pks.append(f'"{name}"')
                parts.append(frag)

            if pks:
                parts.append(f"PRIMARY KEY ({', '.join(pks)})")

            ddl = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(parts)})'
            with pg_conn.cursor() as cur:
                cur.execute(ddl)
            pg_conn.commit()
            return True

        except Exception as e:
            logger.debug(f"ensure_pg_table {table_name}: {e}")
            try:
                pg_conn.rollback()
            except Exception:
                pass
            return False

    def _align_pg_columns(
        self, pg_conn, table_name: str, sqlite_conn: sqlite3.Connection
    ) -> bool:
        """Add columns to PG that exist in SQLite but not PG."""
        try:
            sqlite_cols = {
                row[1]: row[2]
                for row in sqlite_conn.execute(
                    f"PRAGMA table_info('{table_name}')"
                ).fetchall()
            }
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s AND table_schema = 'public'",
                    (table_name,),
                )
                pg_cols = {row[0] for row in cur.fetchall()}

            for col, ctype in sqlite_cols.items():
                if col not in pg_cols:
                    pg_type = self._to_pg_type(ctype)
                    with pg_conn.cursor() as cur:
                        cur.execute(
                            f'ALTER TABLE "{table_name}" '
                            f'ADD COLUMN "{col}" {pg_type}'
                        )
                    pg_conn.commit()
            return True

        except Exception as e:
            logger.debug(f"align_pg_columns {table_name}: {e}")
            try:
                pg_conn.rollback()
            except Exception:
                pass
            return False

    def _adaptive_schema_sync(
        self, pg_conn, sqlite_conn: sqlite3.Connection, tables: List[str]
    ) -> Dict[str, str]:
        """Ensure PG mirrors exist for given tables."""
        results = {}
        for tbl in tables:
            if tbl in SYNC_EXCLUDE:
                results[tbl] = "skip"
                continue
            try:
                ok = self._ensure_pg_table(pg_conn, tbl, sqlite_conn)
                results[tbl] = "ok" if ok else "fail"
            except Exception as e:
                results[tbl] = f"err:{str(e)[:60]}"
        return results

    # -----------------------------------------------------------------
    # PG → SQLite: Reverse schema sync (tables that live in PG first)
    # -----------------------------------------------------------------

    def _ensure_sqlite_table(
        self, sqlite_conn: sqlite3.Connection, table_name: str, pg_conn
    ) -> bool:
        """
        Create SQLite table to mirror PG schema.
        For PG-native tables (contextdna_*) that need local backup.
        """
        try:
            # Check if table exists in SQLite
            existing = sqlite_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if existing:
                return self._align_sqlite_columns(sqlite_conn, table_name, pg_conn)

            # Get PG schema
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = %s AND table_schema = 'public' "
                    "ORDER BY ordinal_position",
                    (table_name,),
                )
                pg_cols = cur.fetchall()

            if not pg_cols:
                return False

            # Get PG primary key columns
            with pg_conn.cursor() as cur:
                cur.execute("""
                    SELECT a.attname
                    FROM pg_index i
                    JOIN pg_attribute a ON a.attrelid = i.indrelid
                        AND a.attnum = ANY(i.indkey)
                    WHERE i.indrelid = %s::regclass AND i.indisprimary
                """, (table_name,))
                pk_cols = [row[0] for row in cur.fetchall()]

            # Build SQLite CREATE TABLE
            parts = []
            for col_name, data_type, nullable, default in pg_cols:
                sqlite_type = self._pg_type_to_sqlite(data_type)
                frag = f'"{col_name}" {sqlite_type}'
                if col_name in pk_cols and len(pk_cols) == 1:
                    frag += " PRIMARY KEY"
                parts.append(frag)

            if len(pk_cols) > 1:
                pk_str = ", ".join(f'"{c}"' for c in pk_cols)
                parts.append(f"PRIMARY KEY ({pk_str})")

            ddl = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(parts)})'
            sqlite_conn.execute(ddl)
            sqlite_conn.commit()
            logger.info(f"created SQLite mirror for PG table: {table_name}")
            return True

        except Exception as e:
            logger.debug(f"ensure_sqlite_table {table_name}: {e}")
            return False

    def _align_sqlite_columns(
        self, sqlite_conn: sqlite3.Connection, table_name: str, pg_conn
    ) -> bool:
        """Add columns to SQLite that exist in PG but not SQLite."""
        try:
            # Get SQLite columns
            sqlite_cols = {
                row[1]
                for row in sqlite_conn.execute(
                    f"PRAGMA table_info('{table_name}')"
                ).fetchall()
            }

            # Get PG columns
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_name = %s AND table_schema = 'public'",
                    (table_name,),
                )
                pg_cols = {row[0]: row[1] for row in cur.fetchall()}

            for col, pg_type in pg_cols.items():
                if col not in sqlite_cols:
                    sqlite_type = self._pg_type_to_sqlite(pg_type)
                    sqlite_conn.execute(
                        f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" {sqlite_type}'
                    )
                    sqlite_conn.commit()

            return True

        except Exception as e:
            logger.debug(f"align_sqlite_columns {table_name}: {e}")
            return False

    @staticmethod
    def _pg_type_to_sqlite(pg_type: str) -> str:
        """Map PG type to SQLite type affinity."""
        pg_upper = pg_type.upper()
        if "INT" in pg_upper:
            return "INTEGER"
        if "FLOAT" in pg_upper or "DOUBLE" in pg_upper or "NUMERIC" in pg_upper:
            return "REAL"
        if "BOOL" in pg_upper:
            return "INTEGER"
        if "BYTEA" in pg_upper:
            return "BLOB"
        if "JSON" in pg_upper:
            return "TEXT"
        if "TIMESTAMP" in pg_upper:
            return "TEXT"
        return "TEXT"

    # -----------------------------------------------------------------
    # AUTO-DISCOVERY: find unregistered tables in all 4 DBs
    # -----------------------------------------------------------------

    def _discover_unregistered_tables(self) -> Dict[str, SyncTableConfig]:
        """
        Scan all SQLite DBs and PG databases for tables not in the registry.
        Returns new configs for discovered tables (conservative defaults).
        Auto-expands the registry as the hierarchy system evolves.
        """
        discovered = {}

        # Discover from SQLite DBs
        for db_name, sqlite_conn in self._sqlite_conns.items():
            try:
                tables = sqlite_conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                ).fetchall()
                for (tbl,) in tables:
                    if tbl in ALL_SYNC_TABLES or tbl in SYNC_EXCLUDE or tbl in discovered:
                        continue
                    # Detect PK
                    pragma = sqlite_conn.execute(
                        f"PRAGMA table_info('{tbl}')"
                    ).fetchall()
                    pk_cols = [r[1] for r in pragma if r[5] > 0]
                    has_auto = any(
                        r[2].upper() == "INTEGER" and r[5] > 0 for r in pragma
                    )
                    # Detect machine_id column
                    col_names = {r[1] for r in pragma}
                    has_mid = "machine_id" in col_names
                    # Detect timestamp column
                    ts_col = ""
                    for candidate in ("updated_at", "updated_at_utc", "last_seen_utc",
                                      "created_at", "created_at_utc", "timestamp_utc"):
                        if candidate in col_names:
                            ts_col = candidate
                            break

                    pg_target = "context_dna"  # default PG target
                    cfg = make_discovered_config(
                        table_name=tbl,
                        sqlite_db=db_name,
                        pg_target=pg_target,
                        pk_columns=pk_cols if pk_cols else ["rowid"],
                        has_autoincrement=has_auto,
                    )
                    cfg.has_machine_id = has_mid
                    cfg.timestamp_col = ts_col
                    if ts_col:
                        cfg.conflict_policy = ConflictPolicy.LATEST_WINS
                    discovered[tbl] = cfg
                    logger.info(f"auto-discovered SQLite table: {tbl} ({db_name})")
            except Exception as e:
                logger.debug(f"discovery scan {db_name}: {e}")

        # Discover from PG databases
        for target_name, pg_conn in self._pg_conns.items():
            try:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' "
                        "AND table_type = 'BASE TABLE'"
                    )
                    pg_tables = [row[0] for row in cur.fetchall()]

                for tbl in pg_tables:
                    if tbl in ALL_SYNC_TABLES or tbl in SYNC_EXCLUDE or tbl in discovered:
                        continue
                    # Get PK from PG
                    with pg_conn.cursor() as cur:
                        cur.execute("""
                            SELECT a.attname
                            FROM pg_index i
                            JOIN pg_attribute a ON a.attrelid = i.indrelid
                                AND a.attnum = ANY(i.indkey)
                            WHERE i.indrelid = %s::regclass AND i.indisprimary
                        """, (tbl,))
                        pk_cols = [row[0] for row in cur.fetchall()]

                    # Check if SERIAL
                    has_auto = False
                    if pk_cols:
                        with pg_conn.cursor() as cur:
                            cur.execute(
                                "SELECT column_default FROM information_schema.columns "
                                "WHERE table_name = %s AND column_name = %s",
                                (tbl, pk_cols[0]),
                            )
                            default = cur.fetchone()
                            if default and default[0] and "nextval" in str(default[0]):
                                has_auto = True

                    cfg = make_discovered_config(
                        table_name=tbl,
                        sqlite_db="observability",  # backup to local SQLite
                        pg_target=target_name,
                        pk_columns=pk_cols if pk_cols else ["id"],
                        has_autoincrement=has_auto,
                    )
                    cfg.direction = SyncDirection.PG_TO_SQLITE  # PG-native
                    discovered[tbl] = cfg
                    logger.info(f"auto-discovered PG table: {tbl} ({target_name})")
            except Exception as e:
                logger.debug(f"discovery scan PG {target_name}: {e}")

        return discovered

    # -----------------------------------------------------------------
    # TYPE CASTING
    # -----------------------------------------------------------------

    def _get_pg_bool_cols(self, pg_conn, table_name: str) -> Set[str]:
        """Get boolean columns in PG table (cached)."""
        cached = self._bool_col_cache.get(table_name)
        if cached is not None:
            return cached
        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s AND table_schema = 'public' "
                    "AND data_type = 'boolean'",
                    (table_name,),
                )
                result = {row[0] for row in cur.fetchall()}
            self._bool_col_cache[table_name] = result
            return result
        except Exception:
            return set()

    def _get_pg_array_cols(self, pg_conn, table_name: str) -> Set[str]:
        """Get array columns in PG table (cached)."""
        cached = self._array_col_cache.get(table_name)
        if cached is not None:
            return cached
        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s AND table_schema = 'public' "
                    "AND data_type = 'ARRAY'",
                    (table_name,),
                )
                result = {row[0] for row in cur.fetchall()}
            self._array_col_cache[table_name] = result
            return result
        except Exception:
            return set()

    @staticmethod
    def _sqlite_to_pg_val(val, is_bool_col: bool = False, is_array_col: bool = False):
        """Convert SQLite value for PG insertion."""
        if is_bool_col and isinstance(val, int):
            return bool(val)
        if is_array_col and isinstance(val, str):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return val

    @staticmethod
    def _pg_to_sqlite_val(val):
        """Convert PG value for SQLite insertion."""
        if isinstance(val, bool):
            return 1 if val else 0
        if isinstance(val, (dict, list)):
            return json.dumps(val)
        if val is not None and not isinstance(val, (str, int, float, bytes)):
            return str(val)
        return val

    # -----------------------------------------------------------------
    # PUSH: SQLite → PG
    # -----------------------------------------------------------------

    def _push_row(self, pg_conn, table_name: str, data: dict, cfg: SyncTableConfig):
        """Insert a single row into PG (ON CONFLICT DO NOTHING for INSERT_ONLY)."""
        bool_cols = self._get_pg_bool_cols(pg_conn, table_name)
        array_cols = self._get_pg_array_cols(pg_conn, table_name)

        # Inject machine_id if required and missing
        if cfg.has_machine_id and "machine_id" not in data:
            data["machine_id"] = self._get_machine_id()

        cols = list(data.keys())
        vals = [self._sqlite_to_pg_val(data[k], k in bool_cols, k in array_cols) for k in cols]

        ph = ", ".join(["%s"] * len(cols))
        cn = ", ".join(f'"{c}"' for c in cols)

        # Build conflict clause
        pk_list = ", ".join('"' + p + '"' for p in cfg.pk_columns)
        insert_part = f'INSERT INTO "{table_name}" ({cn}) VALUES ({ph})'

        if cfg.conflict_policy == ConflictPolicy.INSERT_ONLY:
            sql = f"{insert_part} ON CONFLICT DO NOTHING"

        elif cfg.conflict_policy == ConflictPolicy.MERGE_PRESERVE:
            # Same as INSERT_ONLY at row level — skip existing, insert new
            sql = f"{insert_part} ON CONFLICT DO NOTHING"

        elif cfg.conflict_policy == ConflictPolicy.LATEST_WINS and cfg.timestamp_col:
            update_cols = [c for c in cols if c not in cfg.pk_columns]
            if update_cols:
                set_clause = ", ".join('"' + c + '" = EXCLUDED."' + c + '"' for c in update_cols)
                ts = cfg.timestamp_col
                sql = (
                    f'{insert_part} ON CONFLICT ({pk_list}) '
                    f'DO UPDATE SET {set_clause} '
                    f'WHERE EXCLUDED."{ts}" > "{table_name}"."{ts}" '
                    f'OR "{table_name}"."{ts}" IS NULL'
                )
            else:
                sql = f"{insert_part} ON CONFLICT DO NOTHING"

        elif cfg.conflict_policy == ConflictPolicy.SQLITE_WINS:
            update_cols = [c for c in cols if c not in cfg.pk_columns]
            if update_cols:
                set_clause = ", ".join('"' + c + '" = EXCLUDED."' + c + '"' for c in update_cols)
                sql = f'{insert_part} ON CONFLICT ({pk_list}) DO UPDATE SET {set_clause}'
            else:
                sql = f"{insert_part} ON CONFLICT DO NOTHING"

        else:
            sql = f"{insert_part} ON CONFLICT DO NOTHING"

        with pg_conn.cursor() as cur:
            cur.execute(sql, vals)

    def _push_table(
        self, pg_conn, sqlite_conn, table_name: str, cfg: SyncTableConfig
    ) -> int:
        """Push SQLite rows to PG. Returns count pushed."""
        try:
            self._ensure_pg_table(pg_conn, table_name, sqlite_conn)

            pk = cfg.pk_columns[0]

            # Get existing PG keys
            with pg_conn.cursor() as cur:
                cur.execute(f'SELECT "{pk}" FROM "{table_name}"')
                pg_keys = {row[0] for row in cur.fetchall()}

            # SQLite rows not in PG
            rows = sqlite_conn.execute(
                f'SELECT * FROM "{table_name}" ORDER BY rowid DESC LIMIT ?',
                (cfg.batch_size,),
            ).fetchall()

            pushed = 0
            for row in rows:
                d = dict(row)
                if d.get(pk) not in pg_keys:
                    self._push_row(pg_conn, table_name, d, cfg)
                    pushed += 1

            if pushed:
                pg_conn.commit()
            return pushed

        except Exception as e:
            logger.debug(f"push {table_name}: {e}")
            try:
                pg_conn.rollback()
            except Exception:
                pass
            return 0

    # -----------------------------------------------------------------
    # PULL: PG → SQLite
    # -----------------------------------------------------------------

    def _pull_table(
        self, pg_conn, sqlite_conn, table_name: str, cfg: SyncTableConfig
    ) -> int:
        """Pull PG rows missing from SQLite. Returns count pulled."""
        try:
            pragma = sqlite_conn.execute(
                f"PRAGMA table_info('{table_name}')"
            ).fetchall()
            pk_cols = [r[1] for r in pragma if r[5] > 0]
            if not pk_cols:
                return 0
            pk = pk_cols[0]
            all_cols = [r[1] for r in pragma]

            # Existing SQLite keys
            sqlite_keys = {
                row[0]
                for row in sqlite_conn.execute(
                    f'SELECT "{pk}" FROM "{table_name}"'
                ).fetchall()
            }

            # Query PG — only columns that exist in SQLite schema
            col_list = ", ".join(f'"{c}"' for c in all_cols)
            with pg_conn.cursor() as cur:
                try:
                    cur.execute(
                        f'SELECT {col_list} FROM "{table_name}" LIMIT %s',
                        (cfg.batch_size,),
                    )
                except Exception:
                    pg_conn.rollback()
                    return 0

                desc = [d[0] for d in cur.description]
                pulled = 0
                for row in cur.fetchall():
                    rd = dict(zip(desc, row))
                    if rd.get(pk) not in sqlite_keys:
                        present = [c for c in all_cols if c in rd]
                        vals = [self._pg_to_sqlite_val(rd[c]) for c in present]
                        ph = ", ".join(["?"] * len(present))
                        cn = ", ".join(f'"{c}"' for c in present)
                        try:
                            sqlite_conn.execute(
                                f'INSERT OR IGNORE INTO "{table_name}" '
                                f"({cn}) VALUES ({ph})",
                                vals,
                            )
                            pulled += 1
                        except Exception:
                            pass
                if pulled:
                    sqlite_conn.commit()
                return pulled

        except Exception as e:
            logger.debug(f"pull {table_name}: {e}")
            return 0

    # -----------------------------------------------------------------
    # MIRROR ROW (write-through)
    # -----------------------------------------------------------------

    def mirror_row(self, table_name: str, data: dict):
        """
        Immediate best-effort write-through of a single row to PG.
        Called by high-value write paths (claims, outcomes, injections).
        """
        self._ensure_init()
        cfg = ALL_SYNC_TABLES.get(table_name)
        if not cfg or table_name in SYNC_EXCLUDE:
            return

        pg_conn = self._get_pg(cfg.pg_target)
        if not pg_conn:
            return

        sqlite_conn = self._sqlite_conns.get(cfg.sqlite_db)
        if sqlite_conn:
            try:
                self._ensure_pg_table(pg_conn, table_name, sqlite_conn)
            except Exception:
                pass

        try:
            self._push_row(pg_conn, table_name, data, cfg)
            pg_conn.commit()
        except Exception:
            try:
                pg_conn.rollback()
            except Exception:
                pass

    # -----------------------------------------------------------------
    # AUDIT TRAIL
    # -----------------------------------------------------------------

    def _record_sync_history(
        self,
        trigger: str,
        from_mode: str = "lite",
        to_mode: str = "heavy",
        items_synced: int = 0,
        items_failed: int = 0,
        status: str = "completed",
        error: str = "",
        caller: str = "",
        duration_ms: int = 0,
    ):
        """Record sync event in sync_history table."""
        conn = self._sqlite_conns.get("observability")
        if not conn:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute(
                """
                INSERT INTO sync_history
                (id, sync_started_at, sync_completed_at, from_mode, to_mode,
                 trigger_reason, items_queued, items_synced, items_failed,
                 duration_ms, status, machine_id, triggered_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    now,
                    now,
                    from_mode,
                    to_mode,
                    trigger,
                    items_synced + items_failed,
                    items_synced,
                    items_failed,
                    duration_ms,
                    status,
                    self._get_machine_id(),
                    caller,
                ),
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"sync_history write failed: {e}")

    # -----------------------------------------------------------------
    # MAIN SYNC ENTRY POINT
    # -----------------------------------------------------------------

    def sync_all(self, caller: str = "unknown") -> SyncReport:
        """
        Full sync cycle. Called by lite_scheduler (sync) or via
        async_sync_all by agent_service.

        Flow:
          1. Connect to PG targets
          2. Advisory lock (skip if contention)
          3. Detect mode + handle transitions
          4. Adaptive schema sync
          5. Push SQLite→PG per table
          6. Pull PG→SQLite per table
          7. Audit trail
          8. Release lock
        """
        self._ensure_init()
        t0 = time.monotonic()
        report = SyncReport(caller=caller)

        # --- 1. Mode detection ---
        mode_before = self._get_current_mode()
        report.mode_before = mode_before

        # Check each PG target
        for name in PG_TARGETS:
            available = self._try_pg_connect(name)
            report.pg_targets_available[name] = available

        # Ensure contextdna database exists
        if report.pg_targets_available.get("context_dna"):
            self._ensure_contextdna_database()
            # Retry contextdna connection after creating DB
            if not report.pg_targets_available.get("contextdna"):
                report.pg_targets_available["contextdna"] = self._try_pg_connect(
                    "contextdna"
                )

        mode_after = "heavy" if report.pg_targets_available.get("context_dna") else "lite"
        report.mode_after = mode_after

        # No PG available — nothing to sync
        if mode_after == "lite":
            self._update_mode_state("lite", False)
            report.duration_ms = int((time.monotonic() - t0) * 1000)
            return report

        # --- 2. Advisory lock ---
        primary_pg = self._pg_conns.get("context_dna")
        with sync_advisory_lock(primary_pg) as lock:
            if not lock.acquired:
                report.lock_contention = True
                report.duration_ms = int((time.monotonic() - t0) * 1000)
                return report

            report.lock_acquired = True

            # --- 3. Mode transition ---
            if mode_before != mode_after:
                self.handle_mode_transition(mode_before, mode_after)
                # Lock was released when old PG connection closed during drain.
                # Re-acquire on new connection so push/pull runs protected.
                new_pg = self._get_pg("context_dna")
                if new_pg:
                    try:
                        with new_pg.cursor() as cur:
                            cur.execute(
                                "SELECT pg_try_advisory_lock(%s)",
                                (SYNC_LOCK_ID,),
                            )
                            if not cur.fetchone()[0]:
                                report.lock_contention = True
                                report.duration_ms = int(
                                    (time.monotonic() - t0) * 1000
                                )
                                return report
                        lock.conn = new_pg  # __exit__ unlocks correct conn
                    except Exception as e:
                        logger.debug(f"Lock re-acquire after transition: {e}")

            self._update_mode_state(mode_after, True)

            # --- 4. Auto-discover unregistered tables ---
            discovered = self._discover_unregistered_tables()
            if discovered:
                logger.info(f"[SYNC] discovered {len(discovered)} new tables")
            # Merge registered + discovered for this cycle
            all_tables = dict(get_syncable_tables())
            all_tables.update(discovered)

            # --- 5+6. Schema sync + push + pull ---
            # Group tables by sqlite_db
            by_db: Dict[str, List[SyncTableConfig]] = {}
            for cfg in all_tables.values():
                by_db.setdefault(cfg.sqlite_db, []).append(cfg)

            for db_name, configs in by_db.items():
                sqlite_conn = self._sqlite_conns.get(db_name)
                if not sqlite_conn:
                    continue

                # Group by PG target
                by_target: Dict[str, List[SyncTableConfig]] = {}
                for cfg in configs:
                    by_target.setdefault(cfg.pg_target, []).append(cfg)

                for target_name, target_configs in by_target.items():
                    pg_conn = self._get_pg(target_name)
                    if not pg_conn:
                        for cfg in target_configs:
                            report.errors.append(
                                f"{cfg.table_name}: pg_{target_name}_unavailable"
                            )
                        continue

                    # Schema sync: SQLite→PG tables
                    push_tables = [
                        c for c in target_configs
                        if c.direction in (
                            SyncDirection.SQLITE_TO_PG,
                            SyncDirection.BIDIRECTIONAL,
                        )
                    ]
                    if push_tables:
                        schema = self._adaptive_schema_sync(
                            pg_conn, sqlite_conn,
                            [c.table_name for c in push_tables],
                        )
                        report.schema_results.update(schema)

                    # Schema sync: PG→SQLite tables (reverse direction)
                    pull_tables = [
                        c for c in target_configs
                        if c.direction == SyncDirection.PG_TO_SQLITE
                    ]
                    for cfg in pull_tables:
                        ok = self._ensure_sqlite_table(
                            sqlite_conn, cfg.table_name, pg_conn
                        )
                        report.schema_results[cfg.table_name] = "ok" if ok else "fail"

                    # Bidirectional column alignment for BIDIRECTIONAL tables
                    bidi_tables = [
                        c for c in target_configs
                        if c.direction == SyncDirection.BIDIRECTIONAL
                    ]
                    for cfg in bidi_tables:
                        # PG→SQLite column sync (columns added in PG)
                        self._align_sqlite_columns(
                            sqlite_conn, cfg.table_name, pg_conn
                        )

                    # Sort by priority and sync
                    for cfg in sorted(target_configs, key=lambda c: c.priority):
                        tbl = cfg.table_name
                        if report.schema_results.get(tbl) not in ("ok", None):
                            if report.schema_results.get(tbl) != "ok":
                                continue

                        # Push: SQLite → PG
                        if cfg.direction in (
                            SyncDirection.SQLITE_TO_PG,
                            SyncDirection.BIDIRECTIONAL,
                        ):
                            n = self._push_table(pg_conn, sqlite_conn, tbl, cfg)
                            if n:
                                report.pushed[tbl] = n
                                report.total_pushed += n

                        # Pull: PG → SQLite
                        if cfg.direction in (
                            SyncDirection.PG_TO_SQLITE,
                            SyncDirection.BIDIRECTIONAL,
                        ):
                            n = self._pull_table(pg_conn, sqlite_conn, tbl, cfg)
                            if n:
                                report.pulled[tbl] = n
                                report.total_pulled += n

                        report.tables_synced += 1

        # --- 7. Finalize ---
        report.duration_ms = int((time.monotonic() - t0) * 1000)

        self._record_sync_history(
            trigger=f"sync_all_{caller}",
            items_synced=report.total_pushed + report.total_pulled,
            items_failed=len(report.errors),
            status="completed" if report.success else "partial",
            caller=caller,
            duration_ms=report.duration_ms,
        )

        if report.total_pushed or report.total_pulled:
            logger.info(
                f"[SYNC] {caller}: pushed={report.total_pushed} "
                f"pulled={report.total_pulled} tables={report.tables_synced} "
                f"duration={report.duration_ms}ms"
            )

        return report

    # -----------------------------------------------------------------
    # ASYNC WRAPPER
    # -----------------------------------------------------------------

    async def async_sync_all(self, caller: str = "unknown") -> SyncReport:
        """
        Async wrapper for agent_service.py.
        Runs sync_all in a thread pool to avoid blocking the event loop.
        """
        loop = get_event_loop()
        return await loop.run_in_executor(_executor, self.sync_all, caller)

    # -----------------------------------------------------------------
    # CLEANUP
    # -----------------------------------------------------------------

    def close(self):
        """Close all connections."""
        for conn in self._sqlite_conns.values():
            try:
                conn.close()
            except Exception:
                pass
        for conn in self._pg_conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._sqlite_conns.clear()
        self._pg_conns.clear()
        self._initialized = False


# =========================================================================
# SINGLETON
# =========================================================================

_engine_instance: Optional[UnifiedSyncEngine] = None


def get_sync_engine() -> UnifiedSyncEngine:
    """Get or create the singleton sync engine."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = UnifiedSyncEngine()
    return _engine_instance


# =========================================================================
# CLI
# =========================================================================

if __name__ == "__main__":
    import sys

    engine = get_sync_engine()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "sync":
            print("Running full sync...")
            report = engine.sync_all(caller="cli")
            print(json.dumps(report.to_dict(), indent=2))

        elif cmd == "mode":
            mode = engine.detect_mode()
            print(f"Mode: {mode}")

        elif cmd == "tables":
            from memory.sync_config import get_syncable_tables
            tables = get_syncable_tables()
            print(f"\n{len(tables)} syncable tables:")
            for name, cfg in sorted(tables.items(), key=lambda x: x[1].priority):
                print(
                    f"  [{cfg.priority}] {name:40s} "
                    f"{cfg.sqlite_db:15s} → {cfg.pg_target:15s} "
                    f"{cfg.conflict_policy.value}"
                )

        elif cmd == "status":
            engine._ensure_init()
            mode = engine._get_current_mode()
            pg_status = {}
            for name in PG_TARGETS:
                pg_status[name] = engine._try_pg_connect(name)
            print(f"Mode: {mode}")
            print(f"PG targets: {pg_status}")
            print(f"SQLite DBs: {list(engine._sqlite_conns.keys())}")

        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python unified_sync.py [sync|mode|tables|status]")
    else:
        print("Usage: python unified_sync.py [sync|mode|tables|status]")
