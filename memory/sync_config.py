"""
Unified Sync Configuration — Table Registry + Conflict Policies

Maps every syncable table to its source SQLite DB and target PG database.

3 SQLite databases:
  - observability: memory/.observability.db (29 tables)
  - profiles: ~/.context-dna/profiles.db (11 tables, hierarchy/boundary)
  - learnings: ~/.context-dna/learnings.db (FTS5 learning store, 268+ rows)

2 PostgreSQL databases (same server, port 5432):
  - context_dna: observability + profile + learnings tables (Docker pgvector)
  - contextdna: PG-native knowledge tables (hierarchies, skills, facts)
"""

import os
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from pathlib import Path


# =========================================================================
# ENUMS
# =========================================================================

class ConflictPolicy(Enum):
    """Per-table conflict resolution when SQLite and PG disagree."""
    INSERT_ONLY = "insert_only"       # Append-only, never update existing
    LATEST_WINS = "latest_wins"       # Most recent timestamp wins
    SQLITE_WINS = "sqlite_wins"       # Local always wins (offline-first)
    PG_WINS = "pg_wins"               # Postgres always wins (server-authoritative)
    MERGE_PRESERVE = "merge_preserve" # Insert new, preserve existing (upgrade-safe)


class SyncDirection(Enum):
    """Which direction data flows for a table."""
    SQLITE_TO_PG = "sqlite_to_pg"     # Push only
    PG_TO_SQLITE = "pg_to_sqlite"     # Pull only
    BIDIRECTIONAL = "bidirectional"    # Push + pull
    PG_ONLY = "pg_only"               # PG-native, no SQLite source


# =========================================================================
# PG TARGET CONFIG
# =========================================================================

@dataclass
class PGTarget:
    """A PostgreSQL database connection target."""
    name: str
    database: str
    host: str = "127.0.0.1"
    port: int = 5432
    user: str = "context_dna"
    password: str = "context_dna_dev"

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @classmethod
    def from_env(cls, name: str, database: str, env_prefix: str = "") -> "PGTarget":
        p = f"{env_prefix}_" if env_prefix else ""
        # LEARNINGS_DB_* takes priority over POSTGRES_* to prevent .env hijacking
        return cls(
            name=name,
            database=database,
            host=os.getenv(f"{p}POSTGRES_HOST",
                 os.getenv("LEARNINGS_DB_HOST", "127.0.0.1")),
            port=int(os.getenv(f"{p}POSTGRES_PORT",
                     os.getenv("LEARNINGS_DB_PORT", "5432"))),
            user=os.getenv(f"{p}POSTGRES_USER",
                 os.getenv("LEARNINGS_DB_USER", "context_dna")),
            password=os.getenv(f"{p}POSTGRES_PASSWORD",
                     os.getenv("LEARNINGS_DB_PASSWORD", "context_dna_dev")),
        )


# =========================================================================
# TABLE CONFIG
# =========================================================================

@dataclass
class SyncTableConfig:
    """Configuration for a single syncable table."""
    table_name: str
    sqlite_db: str                     # "observability" | "profiles" | "learnings" | "" (PG-only)
    pg_target: str                     # PGTarget name: "context_dna" | "contextdna"
    pk_columns: List[str]
    conflict_policy: ConflictPolicy
    direction: SyncDirection = SyncDirection.BIDIRECTIONAL
    priority: int = 5                  # 1=highest, 10=lowest
    has_machine_id: bool = False       # Inject machine_id on push
    timestamp_col: str = ""            # Column for LATEST_WINS resolution
    has_autoincrement_pk: bool = False  # SERIAL/AUTOINCREMENT PKs need special handling
    batch_size: int = 200


# =========================================================================
# GLOBAL CONFIGURATION
# =========================================================================

# --- PG targets (both on same Docker PG server) ---
PG_TARGETS: Dict[str, PGTarget] = {
    "context_dna": PGTarget.from_env("context_dna", "context_dna"),
    "contextdna": PGTarget.from_env("contextdna", "contextdna", env_prefix="CONTEXTDNA"),
}

# --- SQLite DB paths ---
OBSERVABILITY_DB = Path(os.getenv(
    "OBSERVABILITY_DB_PATH",
    str(Path(__file__).parent / ".observability.db"),
))

PROFILES_DB = Path(os.getenv(
    "PROFILES_DB_PATH",
    str(Path.home() / ".context-dna" / "profiles.db"),
))

def _get_learnings_db() -> Path:
    from memory.db_utils import get_unified_db_path
    return get_unified_db_path(Path(os.getenv(
        "LEARNINGS_DB_PATH",
        str(Path.home() / ".context-dna" / "learnings.db"),
    )))

LEARNINGS_DB = _get_learnings_db()

SQLITE_DBS: Dict[str, Path] = {
    "observability": OBSERVABILITY_DB,
    "profiles": PROFILES_DB,
    "learnings": LEARNINGS_DB,
}

# --- Tables that must NEVER sync (manage sync itself) ---
SYNC_EXCLUDE = frozenset({
    "mode_sync_state",
    "sync_queue",
})

# --- SQLite→PG type mapping ---
SQLITE_TO_PG_TYPE = {
    "TEXT": "TEXT",
    "INTEGER": "INTEGER",
    "REAL": "DOUBLE PRECISION",
    "BLOB": "BYTEA",
    "NUMERIC": "NUMERIC",
}


# =========================================================================
# TABLE REGISTRY — every syncable table declared once
# =========================================================================

def _t(name, sqlite_db, pg, pk, policy, **kw) -> SyncTableConfig:
    """Shorthand table config constructor."""
    return SyncTableConfig(
        table_name=name, sqlite_db=sqlite_db, pg_target=pg,
        pk_columns=pk if isinstance(pk, list) else [pk],
        conflict_policy=policy, **kw,
    )


# Shorthands
IO = ConflictPolicy.INSERT_ONLY
LW = ConflictPolicy.LATEST_WINS
PW = ConflictPolicy.PG_WINS
MP = ConflictPolicy.MERGE_PRESERVE
CD = "context_dna"
CN = "contextdna"
OB = "observability"
PR = "profiles"
LR = "learnings"
BI = SyncDirection.BIDIRECTIONAL
SP = SyncDirection.SQLITE_TO_PG
PO = SyncDirection.PG_ONLY

ALL_SYNC_TABLES: Dict[str, SyncTableConfig] = {
    # ==================================================================
    # OBSERVABILITY DB → context_dna PG
    # ==================================================================

    # --- Core telemetry (INSERT_ONLY — append-only event streams) ---
    "injection_event":       _t("injection_event", OB, CD, "injection_id", IO, priority=2),
    "injection_section":     _t("injection_section", OB, CD, ["injection_id", "section_id"], IO, priority=3),
    "outcome_event":         _t("outcome_event", OB, CD, "outcome_id", IO, priority=2),
    "guardrail_stop_event":  _t("guardrail_stop_event", OB, CD, "stop_id", IO, priority=2),
    "task_run_event":        _t("task_run_event", OB, CD, "task_run_id", IO, priority=3),
    "dependency_status_event": _t("dependency_status_event", OB, CD, "dep_event_id", IO, priority=4),
    "webhook_delivery_event": _t("webhook_delivery_event", OB, CD, "delivery_id", IO, priority=5),
    "direct_claim_outcome":  _t("direct_claim_outcome", OB, CD, "id", IO, priority=3, has_autoincrement_pk=True),
    "injection_claim":       _t("injection_claim", OB, CD, ["injection_id", "claim_id"], IO, priority=4),
    "container_diagnostic":  _t("container_diagnostic", OB, CD, "diagnostic_id", IO, priority=5),

    # --- Mutable state (LATEST_WINS) ---
    "claim":                 _t("claim", OB, CD, "claim_id", LW, priority=2, timestamp_col="created_at_utc"),
    "knowledge_quarantine":  _t("knowledge_quarantine", OB, CD, "item_id", LW, priority=2, timestamp_col="updated_at_utc"),
    "job_schedule":          _t("job_schedule", OB, CD, "job_name", LW, priority=6, timestamp_col="last_run_utc"),
    "experiment":            _t("experiment", OB, CD, "experiment_id", LW, priority=4, timestamp_col="created_at_utc"),
    "experiment_variant":    _t("experiment_variant", OB, CD, ["experiment_id", "variant_id"], LW, priority=4),
    "experiment_baseline":   _t("experiment_baseline", OB, CD, "experiment_id", LW, priority=4),
    "webhook_destination":   _t("webhook_destination", OB, CD, "destination_id", LW, priority=5, timestamp_col="last_delivery_at"),
    "tech_stack_version_registry": _t("tech_stack_version_registry", OB, CD, "component_name", LW, priority=6, timestamp_col="last_updated_at"),
    "butler_code_note":      _t("butler_code_note", OB, CD, "note_id", LW, priority=4, timestamp_col="last_seen_utc"),

    # --- Rollups (LATEST_WINS — re-computed periodically) ---
    "variant_summary_rollup": _t(
        "variant_summary_rollup", OB, CD,
        ["experiment_id", "variant_id", "window_start_utc", "window_end_utc"], LW, priority=7,
    ),
    "variant_vs_baseline_rollup": _t(
        "variant_vs_baseline_rollup", OB, CD,
        ["experiment_id", "variant_id", "window_start_utc", "window_end_utc"], LW, priority=7,
    ),
    "section_metrics_rollup": _t(
        "section_metrics_rollup", OB, CD,
        ["experiment_id", "section_id", "version", "window_start_utc", "window_end_utc"], LW, priority=7,
    ),
    "claim_outcome_rollup": _t(
        "claim_outcome_rollup", OB, CD,
        ["claim_id", "window_start_utc", "window_end_utc"], LW, priority=7,
    ),

    # --- Blob stores (INSERT_ONLY — content-addressed, immutable) ---
    "vault_snapshot_manifest": _t("vault_snapshot_manifest", OB, CD, "vault_snapshot_id", IO, priority=5),
    "payload_blob":          _t("payload_blob", OB, CD, "payload_sha256", IO, priority=6),
    "section_blob":          _t("section_blob", OB, CD, "content_sha256", IO, priority=6),

    # --- Audit trail (push-only) ---
    "sync_history":          _t("sync_history", OB, CD, "id", IO, priority=8, direction=SP),

    # --- Auto-discovered knowledge tables ---
    "cd_migrations":         _t("cd_migrations", OB, CD, "version", IO, priority=8),

    # ==================================================================
    # LEARNINGS DB → context_dna PG (FTS5 knowledge store backup)
    # ==================================================================
    "learnings":             _t("learnings", LR, CD, "id", IO, priority=3),
    "stats":                 _t("stats", LR, CD, "key", LW, priority=8),

    # ==================================================================
    # PROFILES DB → context_dna PG (hierarchy tables with machine_id)
    # ==================================================================
    "project_boundaries":    _t("project_boundaries", PR, CD, "id", LW, priority=3, has_machine_id=True, timestamp_col="updated_at"),
    "keyword_project_associations": _t("keyword_project_associations", PR, CD, "id", LW, priority=4, has_machine_id=True, timestamp_col="updated_at"),
    "boundary_decisions":    _t("boundary_decisions", PR, CD, "id", IO, priority=4, has_machine_id=True),
    "ide_configurations":    _t("ide_configurations", PR, CD, "id", LW, priority=4, has_machine_id=True, timestamp_col="updated_at"),
    "docker_environment":    _t("docker_environment", PR, CD, "id", LW, priority=4, has_machine_id=True, timestamp_col="updated_at"),
    "app_recovery_configs":  _t("app_recovery_configs", PR, CD, "id", LW, priority=4, has_machine_id=True, timestamp_col="updated_at"),

    # --- Auto-discovered profile hierarchy tables ---
    "hierarchy_backups":     _t("hierarchy_backups", PR, CD, "id", IO, priority=6, has_machine_id=True),
    "hierarchy_profiles":    _t("hierarchy_profiles", PR, CD, "id", LW, priority=5, has_machine_id=True),

    # ==================================================================
    # CONTEXTDNA PG → SQLite backup (PG-native, backed up locally)
    # Source of truth: contextdna PG. SQLite gets offline copies.
    # sqlite_db = "observability" for local backup storage.
    # ==================================================================
    "contextdna_hierarchies": _t("contextdna_hierarchies", OB, CN, "id", PW, priority=5, direction=SyncDirection.PG_TO_SQLITE, has_autoincrement_pk=True),
    "contextdna_skills":      _t("contextdna_skills", OB, CN, "id", PW, priority=5, direction=SyncDirection.PG_TO_SQLITE, has_autoincrement_pk=True),
    "contextdna_events":      _t("contextdna_events", OB, CN, "id", IO, priority=6, direction=SyncDirection.PG_TO_SQLITE, has_autoincrement_pk=True),
    "contextdna_snapshots":   _t("contextdna_snapshots", OB, CN, "id", IO, priority=6, direction=SyncDirection.PG_TO_SQLITE, has_autoincrement_pk=True),
    "contextdna_facts":       _t("contextdna_facts", OB, CN, "id", PW, priority=5, direction=SyncDirection.PG_TO_SQLITE, has_autoincrement_pk=True),
    "contextdna_failures":    _t("contextdna_failures", OB, CN, "id", IO, priority=6, direction=SyncDirection.PG_TO_SQLITE, has_autoincrement_pk=True),
}


# =========================================================================
# QUERY HELPERS
# =========================================================================

def get_tables_for_sqlite_db(db_name: str) -> Dict[str, SyncTableConfig]:
    """Tables that sync from a specific SQLite DB."""
    return {k: v for k, v in ALL_SYNC_TABLES.items() if v.sqlite_db == db_name}


def get_tables_for_pg_target(target_name: str) -> Dict[str, SyncTableConfig]:
    """Tables that sync to a specific PG database."""
    return {k: v for k, v in ALL_SYNC_TABLES.items() if v.pg_target == target_name}


def get_high_priority_tables(max_priority: int = 3) -> Dict[str, SyncTableConfig]:
    """Tables at or above priority threshold (lower number = higher priority)."""
    return {k: v for k, v in ALL_SYNC_TABLES.items() if v.priority <= max_priority}


def get_syncable_tables() -> Dict[str, SyncTableConfig]:
    """All tables eligible for sync (excludes meta-tables)."""
    return {k: v for k, v in ALL_SYNC_TABLES.items()
            if v.table_name not in SYNC_EXCLUDE}


def make_discovered_config(
    table_name: str,
    sqlite_db: str = "observability",
    pg_target: str = "context_dna",
    pk_columns: Optional[List[str]] = None,
    has_autoincrement: bool = False,
) -> SyncTableConfig:
    """
    Create a default config for an auto-discovered table not in registry.
    Conservative defaults: INSERT_ONLY, priority 8, bidirectional.
    """
    return SyncTableConfig(
        table_name=table_name,
        sqlite_db=sqlite_db,
        pg_target=pg_target,
        pk_columns=pk_columns or ["id"],
        conflict_policy=ConflictPolicy.INSERT_ONLY,
        direction=SyncDirection.BIDIRECTIONAL,
        priority=8,
        has_autoincrement_pk=has_autoincrement,
    )
