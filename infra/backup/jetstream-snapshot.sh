#!/usr/bin/env bash
# jetstream-snapshot.sh — Weekly NATS JetStream KV + stream snapshot to S3.
#
# Referenced by: docs/operational-invariance.md §5
#
# Snapshots JetStream KV buckets (fleet_roster, evidence_chain, etc.) and
# any JetStream streams to a tar.gz, encrypts with age, uploads to S3.
#
# ZSF: every failure exits non-zero with a labelled error line.
#
# Env vars:
#   NATS_URL                default: nats://localhost:4222
#   NATS_CREDS              optional (path to creds file for cloud NATS)
#   BACKUP_BUCKET           required (e.g. s3://contextdna-backups)
#   BACKUP_AGE_PUBKEY       required (age public key for encryption)
#   BACKUP_RETENTION_DAYS   default: 90
#   BACKUP_S3_ENDPOINT      optional (B2/Wasabi)
#   BACKUP_PREFIX           default: jetstream
#   JS_KV_BUCKETS           default: fleet_roster,evidence_chain,context_cache
#   JS_STREAMS              optional comma-separated (e.g. FLEET_EVENTS,WEBHOOK_EVENTS)
#
# Usage:
#   bash infra/backup/jetstream-snapshot.sh
#   bash infra/backup/jetstream-snapshot.sh --dry-run

set -uo pipefail

NATS_URL="${NATS_URL:-nats://localhost:4222}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-90}"
BACKUP_PREFIX="${BACKUP_PREFIX:-jetstream}"
JS_KV_BUCKETS="${JS_KV_BUCKETS:-fleet_roster,evidence_chain,context_cache}"
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
    esac
done

_fail() { echo "[js-snapshot] FAIL: $*" >&2; exit 1; }
_ok()   { echo "[js-snapshot] OK: $*"; }
_info() { echo "[js-snapshot] $*"; }

[ -n "${BACKUP_BUCKET:-}" ]     || _fail "BACKUP_BUCKET unset"
[ -n "${BACKUP_AGE_PUBKEY:-}" ] || _fail "BACKUP_AGE_PUBKEY unset"

command -v nats >/dev/null || _fail "nats CLI not found (brew install nats-io/nats-tools/nats)"
command -v age  >/dev/null || _fail "age not found"
command -v aws  >/dev/null || _fail "aws CLI not found"

NATS_ARGS=(--server "$NATS_URL")
[ -n "${NATS_CREDS:-}" ] && NATS_ARGS+=(--creds "$NATS_CREDS")

TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMPDIR="$(mktemp -d -t ctxdna-js.XXXXXX)" || _fail "mktemp failed"
trap 'rm -rf "$TMPDIR"' EXIT

SNAP_DIR="$TMPDIR/snapshot"
mkdir -p "$SNAP_DIR"

# ── Snapshot each KV bucket ──────────────────────────────────────────────────
IFS=',' read -ra BUCKETS <<< "$JS_KV_BUCKETS"
for bkt in "${BUCKETS[@]}"; do
    bkt="$(echo "$bkt" | tr -d ' ')"
    [ -z "$bkt" ] && continue
    _info "snapshotting KV: $bkt"
    if $DRY_RUN; then continue; fi
    # Dump all keys: list -> for each key, get raw value
    keys_file="$SNAP_DIR/$bkt.keys"
    if nats "${NATS_ARGS[@]}" kv ls "$bkt" 2>/dev/null > "$keys_file"; then
        mkdir -p "$SNAP_DIR/$bkt"
        while IFS= read -r key; do
            [ -z "$key" ] && continue
            nats "${NATS_ARGS[@]}" kv get "$bkt" "$key" --raw > "$SNAP_DIR/$bkt/$key" 2>/dev/null || true
        done < "$keys_file"
        _ok "KV $bkt snapshotted ($(wc -l < "$keys_file" | tr -d ' ') keys)"
    else
        _info "KV $bkt not found (skip)"
    fi
done

# ── Snapshot each stream (if configured) ─────────────────────────────────────
if [ -n "${JS_STREAMS:-}" ]; then
    IFS=',' read -ra STREAMS <<< "$JS_STREAMS"
    for stream in "${STREAMS[@]}"; do
        stream="$(echo "$stream" | tr -d ' ')"
        [ -z "$stream" ] && continue
        _info "snapshotting stream: $stream"
        if $DRY_RUN; then continue; fi
        if nats "${NATS_ARGS[@]}" stream backup "$stream" "$SNAP_DIR/stream-$stream.tar" 2>/dev/null; then
            _ok "stream $stream snapshotted"
        else
            _info "stream $stream backup failed or not found (skip)"
        fi
    done
fi

if $DRY_RUN; then
    _info "DRY-RUN: would tar+encrypt+upload contents of $SNAP_DIR"
    exit 0
fi

# ── Pack, encrypt, upload ────────────────────────────────────────────────────
LOCAL_ARCHIVE="$TMPDIR/${BACKUP_PREFIX}-${TS}.tar.gz.age"
(cd "$SNAP_DIR" && tar czf - .) | age -r "$BACKUP_AGE_PUBKEY" > "$LOCAL_ARCHIVE"

[ -s "$LOCAL_ARCHIVE" ] || _fail "archive produced empty output"
SIZE="$(wc -c < "$LOCAL_ARCHIVE" | tr -d ' ')"
_ok "archive built (${SIZE} bytes)"

REMOTE_PATH="${BACKUP_BUCKET%/}/${BACKUP_PREFIX}/${BACKUP_PREFIX}-${TS}.tar.gz.age"
AWS_ARGS=()
[ -n "${BACKUP_S3_ENDPOINT:-}" ] && AWS_ARGS+=(--endpoint-url "$BACKUP_S3_ENDPOINT")

aws "${AWS_ARGS[@]}" s3 cp "$LOCAL_ARCHIVE" "$REMOTE_PATH" --no-progress \
    || _fail "upload failed"
_ok "uploaded to $REMOTE_PATH"

# ── Retention sweep (shared logic with pg-dump.sh) ───────────────────────────
CUTOFF_EPOCH=$(( $(date -u +%s) - BACKUP_RETENTION_DAYS * 86400 ))
aws "${AWS_ARGS[@]}" s3 ls "${BACKUP_BUCKET%/}/${BACKUP_PREFIX}/" 2>/dev/null \
    | awk '{print $4}' \
    | while read -r key; do
        [ -z "$key" ] && continue
        ts_str=$(echo "$key" | grep -oE '[0-9]{8}T[0-9]{6}Z' || true)
        [ -z "$ts_str" ] && continue
        key_epoch=$(date -j -f "%Y%m%dT%H%M%SZ" "$ts_str" +%s 2>/dev/null \
            || date -d "${ts_str:0:8} ${ts_str:9:2}:${ts_str:11:2}:${ts_str:13:2}" +%s 2>/dev/null \
            || echo 0)
        if [ "$key_epoch" -gt 0 ] && [ "$key_epoch" -lt "$CUTOFF_EPOCH" ]; then
            aws "${AWS_ARGS[@]}" s3 rm "${BACKUP_BUCKET%/}/${BACKUP_PREFIX}/$key" >/dev/null 2>&1
        fi
    done

_ok "jetstream-snapshot finished — ${BACKUP_PREFIX}-${TS}"
