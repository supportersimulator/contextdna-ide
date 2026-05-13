#!/usr/bin/env bash
# sync-to-docker-mount.sh
#
# RACE U3 — ContextDNA Docker bind-mount divergence fix (option a: rsync).
#
# WHY THIS EXISTS
# ----------------
# Stack-B containers bind-mount  $HOME/Documents/er-simulator-superrepo/
# (inode 4301875). Aaron edits the canonical tree at
# $HOME/dev/er-simulator-superrepo/ (inode 45969428). Without a bridge,
# code edits never reach the running ContextDNA brain — every running
# container reads stale code.
#
# DESIGN
# ------
# - dev/ is canonical; Documents/ is a one-way mirror for the docker daemon.
# - Idempotent: running it twice is a no-op when nothing changed.
# - Excludes huge or unsafe trees (.git, virtualenvs, node_modules, caches).
# - --delete keeps the mirror tight: anything deleted in dev/ disappears from
#   Documents/ on the next sync (so a removed file in dev does not silently
#   keep running inside a container).
# - Logs every run to /tmp/dev-to-docker-sync.log with a timestamp + duration
#   so the watchdog story stays observable (zero silent failures invariant).
# - Safety guards: refuses to run if SRC == DST, refuses if either path is not
#   a real directory, refuses if DST resolves outside $HOME.
#
# USAGE
# -----
#   bash scripts/sync-to-docker-mount.sh           # sync once
#   bash scripts/sync-to-docker-mount.sh --dry-run # preview only, no writes
#   bash scripts/sync-to-docker-mount.sh --quiet   # log only, no stdout
#
# Exit codes:
#   0 success / nothing-to-do
#   1 misconfiguration (paths, missing rsync)
#   2 rsync itself failed (see log)

set -euo pipefail

SRC="${DEV_TO_DOCKER_SRC:-$HOME/dev/er-simulator-superrepo}/"
DST="${DEV_TO_DOCKER_DST:-$HOME/Documents/er-simulator-superrepo}/"
LOG="${DEV_TO_DOCKER_LOG:-/tmp/dev-to-docker-sync.log}"

DRY_RUN=0
QUIET=0
for arg in "$@"; do
  case "$arg" in
    --dry-run|-n) DRY_RUN=1 ;;
    --quiet|-q)   QUIET=1 ;;
    --help|-h)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown arg: $arg (try --help)" >&2
      exit 1
      ;;
  esac
done

log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
  printf '%s\n' "$line" >> "$LOG"
  if [ "$QUIET" -eq 0 ]; then
    printf '%s\n' "$line"
  fi
}

# --- safety guards ------------------------------------------------------------

if [ ! -d "$SRC" ]; then
  log "ERROR src not a directory: $SRC"
  exit 1
fi

# DST may not exist on first run — that's fine, we'll create it. But if it
# *does* exist it must be a directory, not a symlink to somewhere weird.
if [ -e "$DST" ] && [ ! -d "$DST" ]; then
  log "ERROR dst exists but is not a directory: $DST"
  exit 1
fi

# Refuse pathological cases.
if [ "$(cd "$SRC" && pwd -P)" = "$(cd "${DST%/}" 2>/dev/null && pwd -P || true)" ]; then
  log "ERROR src and dst resolve to same path — refusing to sync"
  exit 1
fi

case "$DST" in
  "$HOME"/*) ;;
  *)
    log "ERROR dst must live under \$HOME, got: $DST"
    exit 1
    ;;
esac

if ! command -v rsync >/dev/null 2>&1; then
  log "ERROR rsync not on PATH"
  exit 1
fi

mkdir -p "$DST"

# --- the sync -----------------------------------------------------------------

# Rationale for each exclude:
#   .git/                    — bind-mount only needs working tree, not history
#   .git-rewrite/            — transient git operations
#   .venv/, .venv-*/         — host-built Python virtualenvs (not portable)
#   __pycache__/, *.pyc      — bytecode caches, regenerate fast
#   node_modules/            — host-built JS deps; container uses its own
#   .DS_Store                — macOS Finder garbage
#   .claude/worktrees/       — every worktree is its own clone; recursing them
#                              would explode the sync size and rewrite working
#                              copies for other agents
#   .pytest_cache/, .mypy_cache/, .ruff_cache/ — tool caches
#   *.log (top-level only)   — runtime logs do not belong in container source
RSYNC_FLAGS=(
  -a              # archive: preserve perms, times, symlinks
  --delete        # mirror semantics
  --human-readable
  --stats
  --exclude='.git/'
  --exclude='.git-rewrite/'
  --exclude='.venv/'
  --exclude='.venv-*/'
  --exclude='**/__pycache__/'
  --exclude='*.pyc'
  --exclude='node_modules/'
  --exclude='.DS_Store'
  --exclude='.claude/worktrees/'
  --exclude='.pytest_cache/'
  --exclude='.mypy_cache/'
  --exclude='.ruff_cache/'
  --exclude='/*.log'
  --exclude='.fleet-state/'
)

if [ "$DRY_RUN" -eq 1 ]; then
  RSYNC_FLAGS+=(--dry-run)
fi

start_ts=$(date +%s)
log "BEGIN src=$SRC dst=$DST dry_run=$DRY_RUN"

set +e
rsync "${RSYNC_FLAGS[@]}" "$SRC" "$DST" >> "$LOG" 2>&1
rc=$?
set -e

end_ts=$(date +%s)
elapsed=$((end_ts - start_ts))

if [ "$rc" -eq 0 ]; then
  log "OK rc=0 elapsed_s=$elapsed"
  exit 0
else
  log "FAIL rc=$rc elapsed_s=$elapsed (see $LOG for rsync stderr)"
  exit 2
fi
