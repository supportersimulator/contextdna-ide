#!/bin/bash
# =============================================================================
# Context DNA Backup Script
# =============================================================================
# Creates timestamped backups of all Context DNA data:
# - PostgreSQL database (gzipped)
# - Redis data (RDB snapshot)
# - SeaweedFS artifacts (tarball)
#
# Usage: ./backup-context-dna.sh [backup_dir]
#
# Environment Variables:
#   BACKUP_DIR - Directory to store backups (default: /var/backups/context-dna)
#   RETENTION_DAYS - Days to keep backups (default: 30)
# =============================================================================

set -euo pipefail

# T4 — ZSF observability for the backup pipeline. The previous version
# routed pg_dump's stderr to /dev/null and trusted the exit code via
# `set -o pipefail`; a silent failure mode was discovered where pg_dump
# exits 0 but emits zero rows (e.g. wrong DB name, ACL miss), producing
# a 20-byte gzip header as "today's backup". This block adds:
#   - --strict flag (abort on pg_dump failure or undersized backup)
#   - persistent counters at /tmp/backup-context-dna-counters.txt
#   - a side log capturing pg_dump stderr for forensics
#   - explicit exit-code + size sanity check after the pipe
# Healthy paths are unchanged: same gzip output, same size echo, same
# success log line.

STRICT_MODE=0
# Parse known flags before the optional positional backup_dir arg.
while [ $# -gt 0 ]; do
    case "$1" in
        --strict) STRICT_MODE=1; shift ;;
        --) shift; break ;;
        -*) echo "[ERROR] Unknown flag: $1" >&2; exit 2 ;;
        *) break ;;
    esac
done

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="${BACKUP_DIR:-${1:-/var/backups/context-dna}}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
COUNTER_FILE="${BACKUP_COUNTER_FILE:-/tmp/backup-context-dna-counters.txt}"
PG_DUMP_STDERR_LOG="${BACKUP_PG_DUMP_STDERR_LOG:-/tmp/backup-context-dna-pg_dump.stderr.log}"
# Minimum plausible pg_dump.gz size — a healthy dump is KB+, an empty
# gzip header alone is ~20 bytes. 200B is well under any real backup
# but well over an empty stream.
PG_DUMP_MIN_BYTES="${PG_DUMP_MIN_BYTES:-200}"

# Counter helpers — append-only key=value lines so the file stays
# greppable from any monitor (Atlas, gains-gate, /health probe).
_counter_inc() {
    local key="$1"
    local now
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    {
        # Best-effort atomic-ish: append a single line.
        printf '%s %s=1 ts=%s\n' "$TIMESTAMP" "$key" "$now"
    } >> "$COUNTER_FILE" 2>/dev/null || true
}

# Source environment file if it exists (for Redis password, etc.)
ENV_FILE="$REPO_ROOT/context-dna/infra/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# Create backup directory
mkdir -p "$BACKUP_DIR"
log_info "Backup directory: $BACKUP_DIR"
log_info "Timestamp: $TIMESTAMP"

# =============================================================================
# PostgreSQL Backup
# =============================================================================
log_info "Backing up PostgreSQL..."

if docker ps --format '{{.Names}}' | grep -q 'contextdna-pg'; then
    # T4 — ZSF: capture pg_dump stderr to a side log, snapshot the exit
    # code from the dump (pre-gzip), and verify the resulting file is
    # large enough to be a real backup. `set -o pipefail` is already on
    # at script scope but the failure mode that triggered this fix was
    # pg_dump exiting 0 with empty output (wrong DB ACL etc), so the
    # exit-code check alone is insufficient — we also size-check.
    pg_dump_status=0
    # `set -e` would abort on the first non-zero in the pipe; suspend
    # it just for this command so we can inspect the status ourselves
    # and decide whether to abort (--strict) or warn-and-continue.
    set +e
    {
        docker exec contextdna-pg pg_dump -U acontext acontext \
            2> "$PG_DUMP_STDERR_LOG"
        echo "${PIPESTATUS[0]:-0}" > "$BACKUP_DIR/.pg_dump_status_$TIMESTAMP"
    } | gzip > "$BACKUP_DIR/postgres_$TIMESTAMP.sql.gz"
    gzip_status=$?
    pg_dump_status=$(cat "$BACKUP_DIR/.pg_dump_status_$TIMESTAMP" 2>/dev/null || echo "0")
    rm -f "$BACKUP_DIR/.pg_dump_status_$TIMESTAMP"
    set -e

    pg_dump_bytes=0
    if [ -f "$BACKUP_DIR/postgres_$TIMESTAMP.sql.gz" ]; then
        # POSIX `wc -c` works across macOS + Linux; `stat` differs.
        pg_dump_bytes=$(wc -c < "$BACKUP_DIR/postgres_$TIMESTAMP.sql.gz" | tr -d ' ')
    fi

    pg_dump_failed=0
    if [ "$pg_dump_status" != "0" ] || [ "$gzip_status" != "0" ]; then
        pg_dump_failed=1
        log_error "PostgreSQL backup pipeline failed: pg_dump=$pg_dump_status gzip=$gzip_status"
        if [ -s "$PG_DUMP_STDERR_LOG" ]; then
            log_error "pg_dump stderr (see $PG_DUMP_STDERR_LOG):"
            tail -n 5 "$PG_DUMP_STDERR_LOG" >&2 || true
        fi
        _counter_inc "backup_pg_dump_errors_total"
    elif [ "$pg_dump_bytes" -lt "$PG_DUMP_MIN_BYTES" ]; then
        # Empty-but-successful dump — the silent-failure mode that
        # produced today's 20-byte "backup". Not a hard error from
        # pg_dump's POV but operationally a failure.
        pg_dump_failed=1
        log_error "PostgreSQL backup suspiciously small: ${pg_dump_bytes}B (< ${PG_DUMP_MIN_BYTES}B threshold)"
        if [ -s "$PG_DUMP_STDERR_LOG" ]; then
            log_error "pg_dump stderr (see $PG_DUMP_STDERR_LOG):"
            tail -n 5 "$PG_DUMP_STDERR_LOG" >&2 || true
        fi
        _counter_inc "backup_pg_dump_undersized_total"
    fi

    if [ "$pg_dump_failed" -eq 0 ]; then
        SIZE=$(du -h "$BACKUP_DIR/postgres_$TIMESTAMP.sql.gz" | cut -f1)
        log_info "PostgreSQL backup complete: postgres_$TIMESTAMP.sql.gz ($SIZE)"
    elif [ "$STRICT_MODE" -eq 1 ]; then
        log_error "PostgreSQL backup failed (--strict): aborting"
        exit 3
    else
        # ZSF: surface but do not abort; other backups must still run.
        log_warn "PostgreSQL backup observed-failed; continuing with other backups"
    fi
else
    log_warn "PostgreSQL container (contextdna-pg) not running, skipping"
fi

# =============================================================================
# Redis Backup
# =============================================================================
log_info "Backing up Redis..."

if docker ps --format '{{.Names}}' | grep -q 'contextdna-redis'; then
    # Trigger BGSAVE and wait for completion
    docker exec contextdna-redis redis-cli -a "${REDIS_PASSWORD:-INSECURE_DEFAULT_CHANGE_ME}" BGSAVE 2>/dev/null || true
    sleep 3

    # Copy RDB file
    docker cp contextdna-redis:/data/dump.rdb "$BACKUP_DIR/redis_$TIMESTAMP.rdb" 2>/dev/null || true

    if [ -f "$BACKUP_DIR/redis_$TIMESTAMP.rdb" ]; then
        SIZE=$(du -h "$BACKUP_DIR/redis_$TIMESTAMP.rdb" | cut -f1)
        log_info "Redis backup complete: redis_$TIMESTAMP.rdb ($SIZE)"
    else
        log_warn "Redis backup may have failed (no dump.rdb found)"
    fi
else
    log_warn "Redis container (contextdna-redis) not running, skipping"
fi

# =============================================================================
# SeaweedFS Backup
# =============================================================================
log_info "Backing up SeaweedFS artifacts..."

if docker ps --format '{{.Names}}' | grep -q 'contextdna-seaweedfs'; then
    docker exec contextdna-seaweedfs tar czf - /data 2>/dev/null > "$BACKUP_DIR/seaweedfs_$TIMESTAMP.tar.gz"

    if [ -f "$BACKUP_DIR/seaweedfs_$TIMESTAMP.tar.gz" ] && [ -s "$BACKUP_DIR/seaweedfs_$TIMESTAMP.tar.gz" ]; then
        SIZE=$(du -h "$BACKUP_DIR/seaweedfs_$TIMESTAMP.tar.gz" | cut -f1)
        log_info "SeaweedFS backup complete: seaweedfs_$TIMESTAMP.tar.gz ($SIZE)"
    else
        log_warn "SeaweedFS backup may have failed (empty or missing file)"
    fi
else
    log_warn "SeaweedFS container (contextdna-seaweedfs) not running, skipping"
fi

# =============================================================================
# Local Memory Files Backup
# =============================================================================
log_info "Backing up local memory files..."

MEMORY_DIR="$REPO_ROOT/memory"
if [ -d "$MEMORY_DIR" ]; then
    # Backup important memory files
    tar czf "$BACKUP_DIR/memory_local_$TIMESTAMP.tar.gz" \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        -C "$REPO_ROOT" \
        memory/.work_dialogue_log.jsonl \
        memory/brain_state.md \
        memory/.pattern_database.json \
        memory/.learning_history.json \
        2>/dev/null || true

    if [ -f "$BACKUP_DIR/memory_local_$TIMESTAMP.tar.gz" ]; then
        SIZE=$(du -h "$BACKUP_DIR/memory_local_$TIMESTAMP.tar.gz" | cut -f1)
        log_info "Local memory backup complete: memory_local_$TIMESTAMP.tar.gz ($SIZE)"
    fi
fi

# =============================================================================
# Cleanup Old Backups
# =============================================================================
log_info "Cleaning up backups older than $RETENTION_DAYS days..."

DELETED=0
for pattern in "postgres_*.sql.gz" "redis_*.rdb" "seaweedfs_*.tar.gz" "memory_local_*.tar.gz"; do
    find "$BACKUP_DIR" -name "$pattern" -mtime +$RETENTION_DAYS -delete 2>/dev/null || true
    DELETED=$((DELETED + $(find "$BACKUP_DIR" -name "$pattern" -mtime +$RETENTION_DAYS 2>/dev/null | wc -l)))
done

if [ $DELETED -gt 0 ]; then
    log_info "Deleted $DELETED old backup files"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
log_info "=== Backup Summary ==="
log_info "Timestamp: $TIMESTAMP"
log_info "Location: $BACKUP_DIR"
echo ""
ls -lh "$BACKUP_DIR"/*_$TIMESTAMP.* 2>/dev/null || log_warn "No backup files created for this timestamp"
echo ""
log_info "Backup complete!"

# Create latest symlink for easy access
ln -sf "$BACKUP_DIR/postgres_$TIMESTAMP.sql.gz" "$BACKUP_DIR/postgres_latest.sql.gz" 2>/dev/null || true
ln -sf "$BACKUP_DIR/redis_$TIMESTAMP.rdb" "$BACKUP_DIR/redis_latest.rdb" 2>/dev/null || true
ln -sf "$BACKUP_DIR/seaweedfs_$TIMESTAMP.tar.gz" "$BACKUP_DIR/seaweedfs_latest.tar.gz" 2>/dev/null || true
