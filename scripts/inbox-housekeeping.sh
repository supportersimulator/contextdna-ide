#!/usr/bin/env bash
# inbox-housekeeping.sh — Sweep stale fleet-message clutter into archive/
#
# Producer: surgeon_lan_relay.py P7 fallback writes
#   .fleet-messages/<node>/<ts>-surgeon-task_brief-<id>.json
# These accumulate when receivers consume via NATS/HTTP and never read the
# git-side P7 inbox. NN-batch (2026-05-08) found 119 across mac1/mac2/mac3.
#
# Strategy:
#   - Move surgeon-task_brief-*.json older than AGE_MIN minutes (default 5)
#     from .fleet-messages/<node>/ to .fleet-messages/<node>/archive/surgeon-briefs/
#   - Idempotent: re-running with no eligible files is a no-op (exit 0).
#   - ZSF: every move failure is logged + counter incremented + exit non-zero.
#
# Usage:
#   ./scripts/inbox-housekeeping.sh              # live sweep
#   ./scripts/inbox-housekeeping.sh --dry-run    # report only
#   AGE_MIN=0 ./scripts/inbox-housekeeping.sh    # archive ALL (use with care)
#
# Exit codes: 0 success (incl. no-op), 1 partial failure, 2 fatal error.

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

REPO_ROOT="${REPO_ROOT:-$HOME/dev/er-simulator-superrepo}"
INBOX_ROOT="${INBOX_ROOT:-$REPO_ROOT/.fleet-messages}"
AGE_MIN="${AGE_MIN:-5}"
PATTERN="${PATTERN:-*surgeon-task_brief-*.json}"

MOVED=0
SKIPPED_FRESH=0
ERRORS=0
ERR_LOG="/tmp/inbox-housekeeping.err"
: > "$ERR_LOG"

log() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" | tee -a "$ERR_LOG" >&2; ERRORS=$((ERRORS + 1)); }

if [[ ! -d "$INBOX_ROOT" ]]; then
  err "FATAL: inbox root not found: $INBOX_ROOT"
  exit 2
fi

shopt -s nullglob

for node_dir in "$INBOX_ROOT"/*/; do
  node="$(basename "$node_dir")"
  [[ "$node" == "archive" ]] && continue   # legacy top-level archive
  archive_dir="${node_dir}archive/surgeon-briefs"
  if ! mkdir -p "$archive_dir" 2>>"$ERR_LOG"; then
    err "mkdir failed: $archive_dir"
    continue
  fi

  # find candidates not already under archive/
  while IFS= read -r -d '' file; do
    # skip anything already in archive subtree
    case "$file" in
      */archive/*) continue ;;
    esac

    if $DRY_RUN; then
      log "DRY: would move $file -> $archive_dir/"
      MOVED=$((MOVED + 1))
      continue
    fi

    if mv "$file" "$archive_dir/" 2>>"$ERR_LOG"; then
      MOVED=$((MOVED + 1))
    else
      err "move failed: $file"
    fi
  done < <(find "$node_dir" -maxdepth 1 -type f -name "$PATTERN" -mmin "+${AGE_MIN}" -print0 2>>"$ERR_LOG")

  # count fresh (skipped) for visibility
  fresh=$(find "$node_dir" -maxdepth 1 -type f -name "$PATTERN" -mmin "-${AGE_MIN}" 2>/dev/null | wc -l | tr -d ' ')
  SKIPPED_FRESH=$((SKIPPED_FRESH + fresh))
done

log "inbox-housekeeping summary:"
log "  moved        : $MOVED"
log "  skipped_fresh: $SKIPPED_FRESH (under ${AGE_MIN}min)"
log "  errors       : $ERRORS"
$DRY_RUN && log "  mode         : dry-run"

# ZSF: emit counters for fleet observability (best-effort; never fail the script on this)
if command -v python3 >/dev/null 2>&1 && [[ -f "$REPO_ROOT/memory/brain.py" ]]; then
  python3 - <<PY 2>/dev/null || true
try:
    import json, time, pathlib
    p = pathlib.Path("/tmp/inbox-housekeeping.metrics.json")
    p.write_text(json.dumps({
        "ts": time.time(),
        "moved": $MOVED,
        "skipped_fresh": $SKIPPED_FRESH,
        "errors": $ERRORS,
    }))
except Exception:
    pass
PY
fi

if (( ERRORS > 0 )); then
  log "see $ERR_LOG for details"
  exit 1
fi
exit 0
