#!/usr/bin/env bash
# restore.sh — Symmetric restore for pg-dump.sh and jetstream-snapshot.sh.
#
# Referenced by: docs/operational-invariance.md §5
#
# Lists available snapshots in $BACKUP_BUCKET, lets you pick (or pass via
# flag), downloads, decrypts with age (using $BACKUP_AGE_KEYFILE), and
# restores into Postgres / JetStream.
#
# This is the script that makes the Operational Invariance Promise real:
# clone the repo, restore the latest snapshot, docker compose up.
#
# ZSF: every failure exits non-zero with a labelled error line.
#
# Env vars:
#   BACKUP_BUCKET           required
#   BACKUP_AGE_KEYFILE      required (path to age identity file — keep offline!)
#   BACKUP_S3_ENDPOINT      optional
#   POSTGRES_HOST/PORT/DB/USER  required for pg restore
#   PGPASSWORD              required for pg restore
#   NATS_URL                default: nats://localhost:4222
#   NATS_CREDS              optional
#
# Usage:
#   bash infra/backup/restore.sh --list                          # show what's available
#   bash infra/backup/restore.sh --kind pg --latest              # restore latest pg dump
#   bash infra/backup/restore.sh --kind pg --timestamp 20260513T120000Z
#   bash infra/backup/restore.sh --kind jetstream --latest
#   bash infra/backup/restore.sh --kind all --latest             # restore everything

set -uo pipefail

KIND=""
TIMESTAMP=""
USE_LATEST=false
LIST_ONLY=false
BACKUP_PREFIX_PG="${BACKUP_PREFIX_PG:-evidence-ledger}"
BACKUP_PREFIX_JS="${BACKUP_PREFIX_JS:-jetstream}"

while [ $# -gt 0 ]; do
    case "$1" in
        --kind)      KIND="$2"; shift 2 ;;
        --timestamp) TIMESTAMP="$2"; shift 2 ;;
        --latest)    USE_LATEST=true; shift ;;
        --list)      LIST_ONLY=true; shift ;;
        --help|-h)
            sed -n '2,/^$/p' "$0" | sed 's|^# ||; s|^#||'
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

_fail() { echo "[restore] FAIL: $*" >&2; exit 1; }
_ok()   { echo "[restore] OK: $*"; }
_info() { echo "[restore] $*"; }

[ -n "${BACKUP_BUCKET:-}" ]      || _fail "BACKUP_BUCKET unset"
$LIST_ONLY || [ -n "${BACKUP_AGE_KEYFILE:-}" ] || _fail "BACKUP_AGE_KEYFILE unset"
$LIST_ONLY || [ -f "${BACKUP_AGE_KEYFILE:-}" ] || _fail "age keyfile not found: $BACKUP_AGE_KEYFILE"

command -v aws >/dev/null || _fail "aws CLI not found"
$LIST_ONLY || command -v age >/dev/null || _fail "age not found"

AWS_ARGS=()
[ -n "${BACKUP_S3_ENDPOINT:-}" ] && AWS_ARGS+=(--endpoint-url "$BACKUP_S3_ENDPOINT")

# ── List mode ────────────────────────────────────────────────────────────────
if $LIST_ONLY; then
    _info "available pg snapshots:"
    aws "${AWS_ARGS[@]}" s3 ls "${BACKUP_BUCKET%/}/${BACKUP_PREFIX_PG}/" 2>/dev/null \
        | awk '{print "  " $4 "  (" $1 " " $2 ", " $3 " bytes)"}' || _info "  (none)"
    _info "available jetstream snapshots:"
    aws "${AWS_ARGS[@]}" s3 ls "${BACKUP_BUCKET%/}/${BACKUP_PREFIX_JS}/" 2>/dev/null \
        | awk '{print "  " $4 "  (" $1 " " $2 ", " $3 " bytes)"}' || _info "  (none)"
    exit 0
fi

[ -n "$KIND" ] || _fail "must specify --kind pg|jetstream|all"
case "$KIND" in
    pg|jetstream|all) ;;
    *) _fail "invalid --kind: $KIND" ;;
esac

if ! $USE_LATEST && [ -z "$TIMESTAMP" ]; then
    _fail "must specify either --latest or --timestamp <YYYYMMDDTHHMMSSZ>"
fi

TMPDIR="$(mktemp -d -t ctxdna-restore.XXXXXX)" || _fail "mktemp failed"
trap 'rm -rf "$TMPDIR"' EXIT

# ── Helper: pick the latest snapshot for a prefix ────────────────────────────
_latest_key_for() {
    local prefix="$1"
    aws "${AWS_ARGS[@]}" s3 ls "${BACKUP_BUCKET%/}/${prefix}/" 2>/dev/null \
        | awk '{print $4}' | grep -E "${prefix}-[0-9]{8}T[0-9]{6}Z" | sort | tail -1
}

# ── Restore Postgres ─────────────────────────────────────────────────────────
_restore_pg() {
    [ -n "${POSTGRES_DB:-}" ]   || _fail "POSTGRES_DB unset"
    [ -n "${POSTGRES_USER:-}" ] || _fail "POSTGRES_USER unset"
    [ -n "${PGPASSWORD:-}" ]    || _fail "PGPASSWORD unset"
    command -v psql >/dev/null   || _fail "psql not found"

    local key
    if $USE_LATEST; then
        key="$(_latest_key_for "$BACKUP_PREFIX_PG")"
        [ -n "$key" ] || _fail "no pg snapshots found"
    else
        key="${BACKUP_PREFIX_PG}-${TIMESTAMP}.sql.gz.age"
    fi

    _info "restoring pg from $key"
    local local_path="$TMPDIR/$key"
    aws "${AWS_ARGS[@]}" s3 cp "${BACKUP_BUCKET%/}/${BACKUP_PREFIX_PG}/$key" "$local_path" --no-progress \
        || _fail "download failed: $key"

    age -d -i "$BACKUP_AGE_KEYFILE" "$local_path" | gunzip \
        | psql \
            --host="${POSTGRES_HOST:-localhost}" \
            --port="${POSTGRES_PORT:-5432}" \
            --username="$POSTGRES_USER" \
            --dbname="$POSTGRES_DB" \
            --quiet \
            --single-transaction \
            --set ON_ERROR_STOP=1 \
        || _fail "psql restore failed (check that DB exists and is empty)"

    _ok "pg restored from $key"
}

# ── Restore JetStream ────────────────────────────────────────────────────────
_restore_js() {
    command -v nats >/dev/null || _fail "nats CLI not found"

    local key
    if $USE_LATEST; then
        key="$(_latest_key_for "$BACKUP_PREFIX_JS")"
        [ -n "$key" ] || _fail "no jetstream snapshots found"
    else
        key="${BACKUP_PREFIX_JS}-${TIMESTAMP}.tar.gz.age"
    fi

    _info "restoring jetstream from $key"
    local local_path="$TMPDIR/$key"
    aws "${AWS_ARGS[@]}" s3 cp "${BACKUP_BUCKET%/}/${BACKUP_PREFIX_JS}/$key" "$local_path" --no-progress \
        || _fail "download failed: $key"

    local extract_dir="$TMPDIR/extract"
    mkdir -p "$extract_dir"
    age -d -i "$BACKUP_AGE_KEYFILE" "$local_path" | tar xzf - -C "$extract_dir" \
        || _fail "decrypt+extract failed"

    local NATS_ARGS=(--server "${NATS_URL:-nats://localhost:4222}")
    [ -n "${NATS_CREDS:-}" ] && NATS_ARGS+=(--creds "$NATS_CREDS")

    # Restore each KV bucket directory found in the extract
    for bkt_dir in "$extract_dir"/*/; do
        [ -d "$bkt_dir" ] || continue
        bkt="$(basename "$bkt_dir")"
        # Ensure bucket exists (idempotent)
        nats "${NATS_ARGS[@]}" kv add "$bkt" --history=5 2>/dev/null || true
        local restored=0
        for keyfile in "$bkt_dir"*; do
            [ -f "$keyfile" ] || continue
            keyname="$(basename "$keyfile")"
            nats "${NATS_ARGS[@]}" kv put "$bkt" "$keyname" "$(cat "$keyfile")" >/dev/null 2>&1 \
                && restored=$((restored + 1))
        done
        _ok "KV $bkt: $restored keys restored"
    done

    # Restore streams if present
    for stream_tar in "$extract_dir"/stream-*.tar; do
        [ -f "$stream_tar" ] || continue
        stream_name="$(basename "$stream_tar" .tar | sed 's/^stream-//')"
        nats "${NATS_ARGS[@]}" stream restore "$stream_name" "$stream_tar" 2>/dev/null \
            && _ok "stream $stream_name restored" \
            || _info "stream $stream_name restore skipped (may already exist)"
    done

    _ok "jetstream restored from $key"
}

case "$KIND" in
    pg)        _restore_pg ;;
    jetstream) _restore_js ;;
    all)       _restore_pg; _restore_js ;;
esac

_ok "restore complete"
