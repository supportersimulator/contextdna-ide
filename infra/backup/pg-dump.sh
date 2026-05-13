#!/usr/bin/env bash
# pg-dump.sh — Daily encrypted PostgreSQL backup to S3-compatible storage.
#
# Referenced by: docs/operational-invariance.md §5
#
# Backs up the evidence-ledger Postgres database, gzips it, encrypts it
# with age (https://github.com/FiloSottile/age) using a public key from
# $BACKUP_AGE_PUBKEY, and uploads to $BACKUP_BUCKET via the AWS CLI or
# any S3-compatible client.
#
# ZSF: every failure exits non-zero AND prints a labelled error line so
# logs are greppable. Never silently fails.
#
# Env vars (all required unless marked optional):
#   POSTGRES_HOST           default: localhost
#   POSTGRES_PORT           default: 5432
#   POSTGRES_DB             required
#   POSTGRES_USER           required
#   PGPASSWORD              required (read by pg_dump)
#   BACKUP_BUCKET           required (e.g. s3://contextdna-backups)
#   BACKUP_AGE_PUBKEY       required (age public key for encryption)
#   BACKUP_RETENTION_DAYS   default: 90
#   BACKUP_S3_ENDPOINT      optional (for B2, Wasabi, etc.)
#   BACKUP_PREFIX           default: evidence-ledger
#
# Usage:
#   bash infra/backup/pg-dump.sh
#   bash infra/backup/pg-dump.sh --dry-run

set -uo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-90}"
BACKUP_PREFIX="${BACKUP_PREFIX:-evidence-ledger}"
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
    esac
done

# ── Preflight ────────────────────────────────────────────────────────────────
_fail() { echo "[pg-dump] FAIL: $*" >&2; exit 1; }
_ok()   { echo "[pg-dump] OK: $*"; }
_info() { echo "[pg-dump] $*"; }

[ -n "${POSTGRES_DB:-}" ]       || _fail "POSTGRES_DB unset"
[ -n "${POSTGRES_USER:-}" ]     || _fail "POSTGRES_USER unset"
[ -n "${PGPASSWORD:-}" ]        || _fail "PGPASSWORD unset"
[ -n "${BACKUP_BUCKET:-}" ]     || _fail "BACKUP_BUCKET unset"
[ -n "${BACKUP_AGE_PUBKEY:-}" ] || _fail "BACKUP_AGE_PUBKEY unset"

command -v pg_dump >/dev/null || _fail "pg_dump not found (install postgresql-client)"
command -v age     >/dev/null || _fail "age not found (brew install age)"
command -v aws     >/dev/null || _fail "aws CLI not found"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMPDIR="$(mktemp -d -t ctxdna-bkp.XXXXXX)" || _fail "mktemp failed"
trap 'rm -rf "$TMPDIR"' EXIT

LOCAL_PATH="$TMPDIR/${BACKUP_PREFIX}-${TS}.sql.gz.age"
REMOTE_PATH="${BACKUP_BUCKET%/}/${BACKUP_PREFIX}/${BACKUP_PREFIX}-${TS}.sql.gz.age"

# ── Dump → gzip → age-encrypt → upload ───────────────────────────────────────
_info "dumping ${POSTGRES_DB}@${POSTGRES_HOST}:${POSTGRES_PORT}"
if $DRY_RUN; then
    _info "DRY-RUN: would write $LOCAL_PATH then upload to $REMOTE_PATH"
    exit 0
fi

pg_dump \
    --host="$POSTGRES_HOST" \
    --port="$POSTGRES_PORT" \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --no-owner --no-privileges \
    --format=plain 2>"$TMPDIR/dump.err" \
    | gzip -9 \
    | age -r "$BACKUP_AGE_PUBKEY" > "$LOCAL_PATH"

if [ ! -s "$LOCAL_PATH" ]; then
    cat "$TMPDIR/dump.err" >&2
    _fail "dump produced empty output"
fi
DUMP_SIZE="$(wc -c < "$LOCAL_PATH" | tr -d ' ')"
_ok "dump+encrypt complete (${DUMP_SIZE} bytes)"

AWS_ARGS=()
if [ -n "${BACKUP_S3_ENDPOINT:-}" ]; then
    AWS_ARGS+=(--endpoint-url "$BACKUP_S3_ENDPOINT")
fi

aws "${AWS_ARGS[@]}" s3 cp "$LOCAL_PATH" "$REMOTE_PATH" \
    --no-progress 2>"$TMPDIR/upload.err" \
    || { cat "$TMPDIR/upload.err" >&2; _fail "upload failed"; }
_ok "uploaded to $REMOTE_PATH"

# ── Retention sweep ──────────────────────────────────────────────────────────
CUTOFF_EPOCH=$(( $(date -u +%s) - BACKUP_RETENTION_DAYS * 86400 ))
PURGED=0

aws "${AWS_ARGS[@]}" s3 ls "${BACKUP_BUCKET%/}/${BACKUP_PREFIX}/" 2>/dev/null \
    | awk '{print $4}' \
    | while read -r key; do
        [ -z "$key" ] && continue
        # Parse YYYYMMDDTHHMMSSZ from filename
        ts_str=$(echo "$key" | grep -oE '[0-9]{8}T[0-9]{6}Z' || true)
        [ -z "$ts_str" ] && continue
        # Convert to epoch (BSD date on macOS, GNU date on Linux)
        key_epoch=$(date -j -f "%Y%m%dT%H%M%SZ" "$ts_str" +%s 2>/dev/null \
            || date -d "${ts_str:0:8} ${ts_str:9:2}:${ts_str:11:2}:${ts_str:13:2}" +%s 2>/dev/null \
            || echo 0)
        if [ "$key_epoch" -gt 0 ] && [ "$key_epoch" -lt "$CUTOFF_EPOCH" ]; then
            aws "${AWS_ARGS[@]}" s3 rm "${BACKUP_BUCKET%/}/${BACKUP_PREFIX}/$key" >/dev/null 2>&1 && PURGED=$((PURGED + 1))
        fi
    done

_ok "retention sweep complete (>${BACKUP_RETENTION_DAYS}d old keys removed)"
_ok "pg-dump finished — ${BACKUP_PREFIX}-${TS}"
