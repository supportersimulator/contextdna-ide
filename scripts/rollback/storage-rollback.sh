#!/usr/bin/env bash
# ============================================================================
#  storage-rollback.sh
# ----------------------------------------------------------------------------
#  Purpose:
#    Restore the ContextDNA storage layer (SQLite primary + Postgres mirror,
#    when available) from a previously-captured backup snapshot. Designed to
#    be invoked automatically by storage-invariant-check.sh when a backend
#    toggle reveals divergence beyond the configured threshold, OR manually
#    by an operator after a botched migration / corrupt write.
#
#    The rollback is idempotent: running it twice with the same --target
#    produces the same final state. The script never touches state it cannot
#    verify post-restore (row counts + checksums).
#
#  Usage:
#    storage-rollback.sh --target <snapshot-id>  --reason <free-text>
#                      [ --backup-dir <path> ]
#                      [ --learnings-db <path> ]
#                      [ --pg-dsn <postgres://...> ]
#                      [ --dry-run ]
#                      [ --no-postgres ]
#                      [ --help ]
#
#    --target        Snapshot identifier. Maps to files inside --backup-dir:
#                       learnings_<target>.db
#                       learnings_<target>.db.sha256        (optional)
#                       postgres_<target>.sql.gz            (optional)
#                       postgres_<target>.sql.gz.sha256     (optional)
#                    Special value 'latest' picks the newest snapshot.
#
#    --reason        Free-text justification. Logged into rollback.log.jsonl
#                    so we can audit who/what triggered the rollback.
#
#    --backup-dir    Default: $CONTEXTDNA_BACKUP_DIR or
#                            ~/.context-dna/backups
#
#    --learnings-db  Default: $CONTEXT_DNA_LEARNINGS_DB or
#                            ~/.context-dna/learnings.db
#                    (Matches memory/sqlite_storage.get_db_path() order.)
#
#    --pg-dsn        Default: $DATABASE_URL. Skipped if --no-postgres OR
#                    psql / pg_restore are absent on PATH — sqlite is the
#                    invariant primary; postgres is a mirror.
#
#    --dry-run       Verify that all backup artefacts exist and pass
#                    checksum validation, but DO NOT mutate the live DB.
#
#  Exit codes:
#    0  rollback complete, verification passed
#    1  rollback ran but post-verification failed
#    2  missing prereqs / bad flags
#    3  no backup snapshot matches --target
#
#  ZSF:
#    Every step increments a counter in $LOG_FILE. A silent skip is never
#    swallowed; we either bump a *_skipped counter (explicit) or fatal-fail.
#    Pipe failures are fatal (pipefail). `|| true` is FORBIDDEN inside this
#    script — if a step is allowed to fail, it must bump a counter.
# ============================================================================

set -euo pipefail

# ----------------------------------------------------------------------------
# Counters
# ----------------------------------------------------------------------------
STEP_PREFLIGHT_OK=0
STEP_BACKUP_RESOLVED=0
STEP_SQLITE_SNAPSHOT=0          # we snapshot the live DB before clobbering it
STEP_SQLITE_RESTORED=0
STEP_SQLITE_VERIFIED=0
STEP_POSTGRES_RESTORED=0
STEP_POSTGRES_SKIPPED=0         # explicit skip (no DSN / no tooling / flag)
STEP_POSTGRES_VERIFIED=0
STEP_AUDIT_RECORDED=0
ERRORS=0
WARNINGS=0

# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------
TARGET=""
REASON=""
BACKUP_DIR=""
LEARNINGS_DB=""
PG_DSN=""
DO_DRY_RUN=0
NO_POSTGRES=0

ROLLBACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${ROLLBACK_DIR}/rollback.log.jsonl"

# ----------------------------------------------------------------------------
# Logging (ZSF — every observable on STDERR + the audit log)
# ----------------------------------------------------------------------------
log_jsonl() {
    # log_jsonl <step> <status> <detail>
    local step="$1"; local status="$2"; shift 2
    local detail="$*"
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    # We deliberately build JSON with printf — jq optional on rollback hosts.
    local escaped_detail
    escaped_detail=$(printf '%s' "$detail" | python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))')
    {
        printf '{"ts":"%s","script":"storage-rollback","step":"%s","status":"%s","target":"%s","reason":%s,"detail":%s}\n' \
            "$ts" "$step" "$status" "$TARGET" \
            "$(printf '%s' "$REASON" | python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))')" \
            "$escaped_detail"
    } >>"$LOG_FILE"
    printf '[rollback] step=%s status=%s detail="%s"\n' "$step" "$status" "$detail" >&2
}

fatal() {
    ERRORS=$((ERRORS + 1))
    log_jsonl "$1" "fatal" "$2"
    print_stats
    exit "${3:-1}"
}

warn() {
    WARNINGS=$((WARNINGS + 1))
    log_jsonl "$1" "warn" "$2"
}

print_stats() {
    cat >&2 <<EOF
============================================================
  storage-rollback.sh — final stats
------------------------------------------------------------
  target:                  ${TARGET}
  reason:                  ${REASON}
  learnings_db:            ${LEARNINGS_DB}
  backup_dir:              ${BACKUP_DIR}
  STEP_PREFLIGHT_OK        ${STEP_PREFLIGHT_OK}
  STEP_BACKUP_RESOLVED     ${STEP_BACKUP_RESOLVED}
  STEP_SQLITE_SNAPSHOT     ${STEP_SQLITE_SNAPSHOT}
  STEP_SQLITE_RESTORED     ${STEP_SQLITE_RESTORED}
  STEP_SQLITE_VERIFIED     ${STEP_SQLITE_VERIFIED}
  STEP_POSTGRES_RESTORED   ${STEP_POSTGRES_RESTORED}
  STEP_POSTGRES_SKIPPED    ${STEP_POSTGRES_SKIPPED}
  STEP_POSTGRES_VERIFIED   ${STEP_POSTGRES_VERIFIED}
  STEP_AUDIT_RECORDED      ${STEP_AUDIT_RECORDED}
  WARNINGS                 ${WARNINGS}
  ERRORS                   ${ERRORS}
============================================================
EOF
}

usage() {
    sed -n '2,55p' "$0" | sed 's/^# \{0,1\}//'
}

# ----------------------------------------------------------------------------
# Arg parsing
# ----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)        TARGET="$2";        shift 2 ;;
        --reason)        REASON="$2";        shift 2 ;;
        --backup-dir)    BACKUP_DIR="$2";    shift 2 ;;
        --learnings-db)  LEARNINGS_DB="$2";  shift 2 ;;
        --pg-dsn)        PG_DSN="$2";        shift 2 ;;
        --dry-run)       DO_DRY_RUN=1;       shift   ;;
        --no-postgres)   NO_POSTGRES=1;      shift   ;;
        --help|-h)       usage; exit 0 ;;
        *)
            echo "Unknown flag: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "$TARGET" ]]; then
    echo "ERROR: --target is required (snapshot id, or 'latest')" >&2
    exit 2
fi
if [[ -z "$REASON" ]]; then
    echo "ERROR: --reason is required (audit trail)" >&2
    exit 2
fi

# Resolve defaults
if [[ -z "$BACKUP_DIR" ]]; then
    BACKUP_DIR="${CONTEXTDNA_BACKUP_DIR:-$HOME/.context-dna/backups}"
fi
if [[ -z "$LEARNINGS_DB" ]]; then
    LEARNINGS_DB="${CONTEXT_DNA_LEARNINGS_DB:-$HOME/.context-dna/learnings.db}"
fi
if [[ -z "$PG_DSN" && -n "${DATABASE_URL:-}" ]]; then
    PG_DSN="$DATABASE_URL"
fi

mkdir -p "$(dirname "$LOG_FILE")"

# ----------------------------------------------------------------------------
# STEP A — preflight
# ----------------------------------------------------------------------------
require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        fatal "preflight" "required command '$1' not found on PATH" 2
    fi
}
require_cmd sqlite3
require_cmd python3        # used for JSON encoding + checksum compare
require_cmd shasum

if [[ ! -d "$BACKUP_DIR" ]]; then
    fatal "preflight" "backup dir does not exist: $BACKUP_DIR" 3
fi

STEP_PREFLIGHT_OK=1
log_jsonl "preflight" "ok" "backup_dir=$BACKUP_DIR learnings_db=$LEARNINGS_DB dry_run=$DO_DRY_RUN"

# ----------------------------------------------------------------------------
# STEP B — resolve target snapshot
# ----------------------------------------------------------------------------
if [[ "$TARGET" == "latest" ]]; then
    RESOLVED=$(ls -1t "$BACKUP_DIR"/learnings_*.db 2>/dev/null | head -1 || echo "")
    if [[ -z "$RESOLVED" ]]; then
        fatal "resolve" "no learnings_*.db snapshots in $BACKUP_DIR" 3
    fi
    TARGET=$(basename "$RESOLVED" .db | sed 's/^learnings_//')
    log_jsonl "resolve" "info" "latest -> $TARGET"
fi

SQLITE_BACKUP="$BACKUP_DIR/learnings_${TARGET}.db"
SQLITE_BACKUP_SHA="$BACKUP_DIR/learnings_${TARGET}.db.sha256"
PG_BACKUP="$BACKUP_DIR/postgres_${TARGET}.sql.gz"
PG_BACKUP_SHA="$BACKUP_DIR/postgres_${TARGET}.sql.gz.sha256"

if [[ ! -f "$SQLITE_BACKUP" ]]; then
    fatal "resolve" "sqlite backup missing: $SQLITE_BACKUP" 3
fi

# SHA validation (mandatory if .sha256 sidecar exists)
if [[ -f "$SQLITE_BACKUP_SHA" ]]; then
    EXPECTED=$(awk '{print $1}' "$SQLITE_BACKUP_SHA")
    ACTUAL=$(shasum -a 256 "$SQLITE_BACKUP" | awk '{print $1}')
    if [[ "$EXPECTED" != "$ACTUAL" ]]; then
        fatal "resolve" "sqlite backup sha mismatch: expected=$EXPECTED actual=$ACTUAL" 1
    fi
    log_jsonl "resolve" "info" "sqlite sha verified ($ACTUAL)"
else
    warn "resolve" "no sqlite .sha256 sidecar — restoring without integrity check"
fi

STEP_BACKUP_RESOLVED=1
log_jsonl "resolve" "ok" "target=$TARGET sqlite=$SQLITE_BACKUP pg=$PG_BACKUP"

if [[ $DO_DRY_RUN -eq 1 ]]; then
    log_jsonl "dry_run" "info" "exiting before any mutation"
    STEP_AUDIT_RECORDED=1
    print_stats
    exit 0
fi

# ----------------------------------------------------------------------------
# STEP C — snapshot current live DB (so the rollback is itself reversible)
# ----------------------------------------------------------------------------
PRE_ROLLBACK_TS=$(date -u +%Y%m%dT%H%M%SZ)
PRE_ROLLBACK_COPY="$BACKUP_DIR/pre_rollback_${PRE_ROLLBACK_TS}.db"

if [[ -f "$LEARNINGS_DB" ]]; then
    # Use sqlite3 .backup so WAL state is captured cleanly. A plain cp may
    # miss in-flight transactions.
    if sqlite3 "$LEARNINGS_DB" ".backup '$PRE_ROLLBACK_COPY'" >&2; then
        STEP_SQLITE_SNAPSHOT=1
        log_jsonl "snapshot" "ok" "pre_rollback_copy=$PRE_ROLLBACK_COPY"
    else
        fatal "snapshot" "could not snapshot live DB at $LEARNINGS_DB"
    fi
else
    warn "snapshot" "live DB does not exist yet at $LEARNINGS_DB — proceeding with fresh restore"
    STEP_SQLITE_SNAPSHOT=1
fi

# ----------------------------------------------------------------------------
# STEP D — restore SQLite
# ----------------------------------------------------------------------------
mkdir -p "$(dirname "$LEARNINGS_DB")"

# If $LEARNINGS_DB is a symlink (common: learnings.db -> FALLBACK_learnings.db),
# resolve it. Restoring through a symlink is fine, but we want to know.
if [[ -L "$LEARNINGS_DB" ]]; then
    RESOLVED_DB=$(readlink -f "$LEARNINGS_DB" 2>/dev/null || readlink "$LEARNINGS_DB")
    log_jsonl "restore" "info" "live path is a symlink -> $RESOLVED_DB"
fi

# Use sqlite3 .restore semantics: copy the backup into place, then VACUUM to
# normalise. We cannot `mv` directly — readers might still hold the FD.
TMP_RESTORE="${LEARNINGS_DB}.rollback.tmp"
cp "$SQLITE_BACKUP" "$TMP_RESTORE"
# Sanity: the file must open as a valid SQLite DB before we promote it.
if ! sqlite3 "$TMP_RESTORE" "PRAGMA integrity_check;" | grep -q '^ok$'; then
    rm -f "$TMP_RESTORE"
    fatal "restore" "sqlite integrity_check failed for $SQLITE_BACKUP"
fi
mv "$TMP_RESTORE" "$LEARNINGS_DB"

STEP_SQLITE_RESTORED=1
log_jsonl "restore" "ok" "sqlite restored from $SQLITE_BACKUP"

# ----------------------------------------------------------------------------
# STEP E — verify sqlite row counts
# ----------------------------------------------------------------------------
EXPECTED_ROWS=$(sqlite3 "$SQLITE_BACKUP" \
    "SELECT COUNT(*) FROM learnings;" 2>/dev/null || echo "?")
ACTUAL_ROWS=$(sqlite3 "$LEARNINGS_DB" \
    "SELECT COUNT(*) FROM learnings;" 2>/dev/null || echo "?")

if [[ "$EXPECTED_ROWS" == "?" || "$ACTUAL_ROWS" == "?" ]]; then
    fatal "verify" "could not read learnings row count (expected=$EXPECTED_ROWS actual=$ACTUAL_ROWS)"
fi
if [[ "$EXPECTED_ROWS" != "$ACTUAL_ROWS" ]]; then
    fatal "verify" "row count mismatch: expected=$EXPECTED_ROWS actual=$ACTUAL_ROWS"
fi

STEP_SQLITE_VERIFIED=1
log_jsonl "verify" "ok" "learnings rows=$ACTUAL_ROWS"

# ----------------------------------------------------------------------------
# STEP F — restore Postgres mirror (best-effort, gated)
# ----------------------------------------------------------------------------
postgres_available=1
if [[ $NO_POSTGRES -eq 1 ]]; then
    postgres_available=0
    log_jsonl "postgres" "skipped" "--no-postgres flag"
elif [[ -z "$PG_DSN" ]]; then
    postgres_available=0
    log_jsonl "postgres" "skipped" "no DSN (env DATABASE_URL unset)"
elif ! command -v psql >/dev/null 2>&1; then
    postgres_available=0
    log_jsonl "postgres" "skipped" "psql binary not on PATH"
elif [[ ! -f "$PG_BACKUP" ]]; then
    postgres_available=0
    log_jsonl "postgres" "skipped" "no $PG_BACKUP in backup dir"
fi

if [[ $postgres_available -eq 0 ]]; then
    STEP_POSTGRES_SKIPPED=1
    STEP_POSTGRES_VERIFIED=1     # nothing to verify — explicit skip already logged
else
    if [[ -f "$PG_BACKUP_SHA" ]]; then
        PG_EXP=$(awk '{print $1}' "$PG_BACKUP_SHA")
        PG_ACT=$(shasum -a 256 "$PG_BACKUP" | awk '{print $1}')
        if [[ "$PG_EXP" != "$PG_ACT" ]]; then
            fatal "postgres" "postgres backup sha mismatch: expected=$PG_EXP actual=$PG_ACT"
        fi
    fi

    # Stream gzipped dump straight into psql. pipefail makes this fatal if
    # gunzip OR psql barfs.
    if gunzip -c "$PG_BACKUP" | psql "$PG_DSN" --quiet --single-transaction --set ON_ERROR_STOP=on >&2; then
        STEP_POSTGRES_RESTORED=1
        log_jsonl "postgres" "ok" "restored from $PG_BACKUP"

        PG_ROWS=$(psql "$PG_DSN" -At -c "SELECT COUNT(*) FROM learnings;" 2>/dev/null || echo "?")
        if [[ "$PG_ROWS" == "?" ]]; then
            fatal "postgres-verify" "could not read postgres row count"
        fi
        if [[ "$PG_ROWS" != "$ACTUAL_ROWS" ]]; then
            warn "postgres-verify" "postgres rows=$PG_ROWS vs sqlite rows=$ACTUAL_ROWS — mirror divergence"
        fi
        STEP_POSTGRES_VERIFIED=1
    else
        fatal "postgres" "psql restore returned non-zero"
    fi
fi

# ----------------------------------------------------------------------------
# STEP G — audit trail
# ----------------------------------------------------------------------------
STEP_AUDIT_RECORDED=1
log_jsonl "audit" "ok" "rollback complete; pre-rollback copy=$PRE_ROLLBACK_COPY; rows=$ACTUAL_ROWS"

print_stats

cat <<EOF

============================================================
  ROLLBACK COMPLETE
------------------------------------------------------------
  target:                ${TARGET}
  reason:                ${REASON}
  sqlite rows restored:  ${ACTUAL_ROWS}
  pre-rollback snapshot: ${PRE_ROLLBACK_COPY}
  postgres:              $([[ $STEP_POSTGRES_RESTORED -eq 1 ]] && echo "restored" || echo "skipped")
============================================================
EOF

exit 0
