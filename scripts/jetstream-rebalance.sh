#!/usr/bin/env bash
# jetstream-rebalance.sh — Aaron-run helper for FLEET_MESSAGES + FLEET_EVENTS
#
# Default mode: DRY-RUN. Prints what it would do and exits 0.
# Pass --apply to actually run destructive commands.
# Pass --target-replicas N to change replica count (default = current).
#
# Created by G3 (round-7 audit, R-0003 diagnostic-only).
# Reference: .fleet/audits/2026-05-04-G3-jetstream-rebalance.md
#
# IMPORTANT: read the audit first. The streams are usually healthy at the
# server layer even when /health reports degraded — the bug is often in the
# daemon's probe path. Do NOT run --apply just because /health is red.

set -euo pipefail

STREAMS=(FLEET_MESSAGES FLEET_EVENTS)
APPLY=0
TARGET_REPLICAS=""
BACKUP_DIR="/tmp/jetstream-rebalance-$(date +%s)"

usage() {
    cat <<EOF
Usage: $0 [--apply] [--target-replicas N] [--backup-dir DIR]

  --apply              Actually run destructive commands (default: dry-run)
  --target-replicas N  Change num_replicas to N (1, 3, or 5)
  --backup-dir DIR     Where to write stream backups (default: $BACKUP_DIR)
  -h, --help           This help

Without --apply, the script only prints + backs up. Safe to run any time.

Pre-flight checks (always run):
  1. nats CLI present
  2. NATS server reachable on 127.0.0.1:4222
  3. Streams exist
  4. Backup of each stream → \$BACKUP_DIR/<stream>.backup/

If --apply is passed and --target-replicas is set, runs:
  nats stream edit <STREAM> --replicas N

EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --target-replicas) TARGET_REPLICAS="$2"; shift 2 ;;
        --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
    esac
done

echo "== JetStream Rebalance Helper =="
echo "Mode: $([ $APPLY -eq 1 ] && echo APPLY || echo DRY-RUN)"
echo "Backup dir: $BACKUP_DIR"
echo

# 1. nats CLI
if ! command -v nats >/dev/null 2>&1; then
    echo "ERROR: nats CLI not on PATH. Install via 'brew install nats-io/nats-tools/nats'." >&2
    exit 2
fi

# 2. server reachable
if ! curl -sf --max-time 3 http://127.0.0.1:8222/varz >/dev/null 2>&1; then
    echo "ERROR: NATS monitoring port 8222 unreachable. Is nats-server running?" >&2
    exit 3
fi
echo "[OK] NATS server reachable (8222 + 4222 assumed paired)"

# 3. streams exist + capture state
mkdir -p "$BACKUP_DIR"
for s in "${STREAMS[@]}"; do
    if ! nats stream info "$s" >/dev/null 2>&1; then
        echo "ERROR: stream $s not found." >&2
        exit 4
    fi
    echo "[OK] stream $s exists"
    echo "----- $s state -----"
    nats stream info "$s" --json | python3 -c "
import json, sys
d = json.load(sys.stdin)
cfg = d.get('config', {})
state = d.get('state', {})
cluster = d.get('cluster', {}) or {}
print(f\"  replicas:        {cfg.get('num_replicas')}\")
print(f\"  storage:         {cfg.get('storage')}\")
print(f\"  retention:       {cfg.get('retention')}\")
print(f\"  max_bytes:       {cfg.get('max_bytes')}\")
print(f\"  messages:        {state.get('messages')}\")
print(f\"  consumers:       {state.get('consumer_count')}\")
print(f\"  leader:          {cluster.get('leader')}\")
followers = cluster.get('replicas', [])
for r in followers:
    print(f\"  follower:        {r.get('name')} current={r.get('current')} lag={r.get('lag', 0)}\")
"
done
echo

# 4. backup
echo "== Backing up streams to $BACKUP_DIR =="
for s in "${STREAMS[@]}"; do
    out="$BACKUP_DIR/$s.backup"
    if [[ $APPLY -eq 1 ]]; then
        nats stream backup "$s" "$out"
        echo "[OK] backed up $s → $out"
    else
        echo "[DRY-RUN] would: nats stream backup $s $out"
    fi
done
echo

# 5. apply rebalance if requested
if [[ -n "$TARGET_REPLICAS" ]]; then
    case "$TARGET_REPLICAS" in
        1|3|5) ;;
        *) echo "ERROR: --target-replicas must be 1, 3, or 5 (got $TARGET_REPLICAS)" >&2; exit 5 ;;
    esac
    echo "== Rebalance to num_replicas=$TARGET_REPLICAS =="
    for s in "${STREAMS[@]}"; do
        cmd=(nats stream edit "$s" --replicas "$TARGET_REPLICAS"
             --description "rebalanced by jetstream-rebalance.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)")
        if [[ $APPLY -eq 1 ]]; then
            echo "[APPLY] ${cmd[*]}"
            "${cmd[@]}" --force
        else
            echo "[DRY-RUN] would: ${cmd[*]}"
        fi
    done
    echo
fi

# 6. final summary
if [[ $APPLY -eq 1 ]]; then
    echo "== Post-state =="
    for s in "${STREAMS[@]}"; do
        echo "----- $s -----"
        nats stream info "$s" | head -20
    done
    echo
    echo "Backups in: $BACKUP_DIR"
    echo "Restore (if needed): nats stream restore $BACKUP_DIR/<stream>.backup"
else
    echo "== Dry-run complete =="
    echo "Re-run with --apply --target-replicas N to actually rebalance."
fi
