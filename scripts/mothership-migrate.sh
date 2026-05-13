#!/usr/bin/env bash
# ============================================================================
#  mothership-migrate.sh
# ----------------------------------------------------------------------------
#  Purpose:
#    Idempotently copy the CORE directories from the er-simulator-superrepo
#    into a fresh contextdna-ide mothership tree (target). Source-of-truth
#    propagation only — additive rsync, no deletions in the target.
#
#    Operational-invariance invariant (Aaron 2026-05-13):
#      "Delete laptop + delete AWS, mothership still works."
#    After this script + the companion sanitize/init/push scripts run, a
#    fresh `git clone` of the mothership repo is sufficient to reconstitute
#    Aaron's full persistent-memory ecosystem (heavy + lite modes).
#
#    Full plan: docs/plans/2026-05-13-contextdna-ide-mothership-plan.md
#
#  Usage:
#    ./mothership-migrate.sh --target /path/to/mothership-checkout
#    ./mothership-migrate.sh --source /custom/superrepo --target /dest
#    ./mothership-migrate.sh --target /dest --dry-run
#    ./mothership-migrate.sh --help
#
#  Defaults:
#    --source  /Users/aarontjomsland/dev/er-simulator-superrepo
#    --target  REQUIRED (no default — fail-loud)
#
#  Exit codes:
#    0  success — all CORE dirs synced, sanitize-rules.sh applied
#    1  fatal   — missing target arg, source dir missing, rsync error,
#                 sanitize-rules.sh failure, or any counted ERROR
#    2  usage   — bad flag combination, --help printed
#
#  ZSF (Zero Silent Failures):
#    Every step bumps one of: COPIED, EXCLUDED, SANITIZED, ERRORS.
#    Final stats block is printed to STDERR on completion. Any rsync
#    non-zero exit increments ERRORS and aborts with code 1.
#
#  What this script does NOT do (other agents own these):
#    - git init / git remote add / git push          (init-mothership.sh)
#    - submodule wiring (landing-page, 3-surgeons)   (wire-submodules.sh)
#    - install dependencies (.venv, npm, brew)       (bootstrap-env.sh)
#    - generate SECRETS scaffolding                  (secrets-template.sh)
#    - delete any existing files in target           (rsync runs additive)
#
#  Companion script (must exist at $SOURCE/scripts/sanitize-rules.sh):
#    Applies sed-based PII/path replacements over the target tree after
#    copy completes. Stats reported via its stdout (lines matching
#    "REPLACEMENTS:" are parsed for SANITIZED counter).
# ============================================================================

set -euo pipefail

# ----------------------------------------------------------------------------
# Counters (ZSF — every step contributes to exactly one)
# ----------------------------------------------------------------------------
COPIED=0
EXCLUDED=0
SANITIZED=0
ERRORS=0

# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------
SOURCE_DEFAULT="/Users/aarontjomsland/dev/er-simulator-superrepo"
SOURCE=""
TARGET=""
DRY_RUN=0

# ----------------------------------------------------------------------------
# CORE directories — relative to $SOURCE
# (verified 2026-05-13 against superrepo; see self-review in caller report)
# ----------------------------------------------------------------------------
CORE_DIRS=(
  "memory"
  "mcp-servers"
  "tools"
  "scripts"
  "contextdna"
  "infra"
  "system"
  "docs/vision"
  "docs/dao"
  ".github/workflows"
)

# docs/plans/contextdna-ide-* is a glob — handled separately below.
PLAN_GLOB="docs/plans/contextdna-ide-*"

# ----------------------------------------------------------------------------
# Exclusion patterns (rsync --exclude form)
# ----------------------------------------------------------------------------
EXCLUDES=(
  # Compiled / cache
  "*.pyc"
  "__pycache__/"
  ".pytest_cache/"
  ".mypy_cache/"

  # Databases / WAL / SHM
  "*.db"
  "*.sqlite"
  "*.db-shm"
  "*.db-wal"

  # Virtual envs / deps
  ".venv*/"
  "venv*/"
  "node_modules/"

  # Runtime state (pid / log / json scratch)
  ".scheduler_coordinator.pid"
  ".synaptic_voice_daemon.pid"
  ".work_dialogue_log.jsonl"
  ".active_session_injections.json"
  ".ab_testing_log*.json"

  # Personal wisdom — must never leave Aaron's local tree
  "family_wisdom/"

  # Memory backups — point-in-time snapshots, not part of mothership
  "memory.bak.*/"
)

# ----------------------------------------------------------------------------
# usage()
# ----------------------------------------------------------------------------
usage() {
  cat <<'USAGE' >&2
mothership-migrate.sh — copy CORE dirs from superrepo into a mothership tree

Usage:
  mothership-migrate.sh --target <path> [--source <path>] [--dry-run]

Options:
  --target  <path>   destination mothership tree (REQUIRED)
  --source  <path>   source superrepo (default: /Users/aarontjomsland/dev/er-simulator-superrepo)
  --dry-run          print rsync actions without executing
  -h, --help         show this help and exit (code 2)

Exit codes: 0 success | 1 fatal error | 2 usage error
USAGE
}

# ----------------------------------------------------------------------------
# parse_args()
# ----------------------------------------------------------------------------
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source)
        SOURCE="${2:?--source requires a path}"
        shift 2
        ;;
      --target)
        TARGET="${2:?--target requires a path}"
        shift 2
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      -h|--help)
        usage
        exit 2
        ;;
      *)
        printf 'mothership-migrate: unknown arg: %s\n' "$1" >&2
        usage
        exit 2
        ;;
    esac
  done

  if [[ -z "$TARGET" ]]; then
    printf 'mothership-migrate: ERROR --target is required\n' >&2
    usage
    exit 2
  fi
  SOURCE="${SOURCE:-$SOURCE_DEFAULT}"
}

# ----------------------------------------------------------------------------
# preflight() — verify source dirs exist; create target if missing
# ----------------------------------------------------------------------------
preflight() {
  if [[ ! -d "$SOURCE" ]]; then
    printf 'mothership-migrate: ERROR source not found: %s\n' "$SOURCE" >&2
    ERRORS=$((ERRORS + 1))
    exit 1
  fi
  if [[ ! -f "$SOURCE/scripts/sanitize-rules.sh" ]]; then
    printf 'mothership-migrate: WARN  expected helper missing: %s/scripts/sanitize-rules.sh\n' "$SOURCE" >&2
    printf 'mothership-migrate: WARN  copy will proceed; sanitization step will skip if still missing post-copy.\n' >&2
  fi
  if (( DRY_RUN == 0 )) && [[ ! -d "$TARGET" ]]; then
    printf 'mothership-migrate: creating target %s\n' "$TARGET" >&2
    mkdir -p -- "$TARGET"
  fi
}

# ----------------------------------------------------------------------------
# build_rsync_excludes() — emit --exclude=PATTERN args
# ----------------------------------------------------------------------------
build_rsync_excludes() {
  local pattern
  for pattern in "${EXCLUDES[@]}"; do
    printf -- '--exclude=%s\n' "$pattern"
  done
}

# ----------------------------------------------------------------------------
# rsync_one_dir <rel-path>
#   Copies $SOURCE/<rel-path> -> $TARGET/<rel-path>, additive.
#   Bumps COPIED, EXCLUDED (parsed from --stats), or ERRORS.
# ----------------------------------------------------------------------------
rsync_one_dir() {
  local rel="$1"
  local src="$SOURCE/$rel"
  local dst="$TARGET/$rel"

  if [[ ! -d "$src" ]]; then
    printf 'mothership-migrate: WARN  source dir missing, skipped: %s\n' "$rel" >&2
    ERRORS=$((ERRORS + 1))
    return 0
  fi

  if (( DRY_RUN == 0 )); then
    mkdir -p -- "$(dirname -- "$dst")"
  fi

  local -a rsync_args=(
    -a
    --stats
  )
  if (( DRY_RUN == 1 )); then
    rsync_args+=( -n -v )
  fi

  # Read excludes into array (one --exclude=PATTERN per line)
  local -a exclude_args=()
  while IFS= read -r line; do
    exclude_args+=("$line")
  done < <(build_rsync_excludes)

  printf 'mothership-migrate: rsync %s/ -> %s/\n' "$rel" "$rel" >&2

  # Capture stats; tee to stderr so caller sees progress.
  local tmp_log
  tmp_log="$(mktemp -t mothership-rsync.XXXXXX)"
  if ! rsync "${rsync_args[@]}" "${exclude_args[@]}" "$src/" "$dst/" >"$tmp_log" 2>&1; then
    printf 'mothership-migrate: ERROR rsync failed for %s\n' "$rel" >&2
    sed -e 's/^/  /' "$tmp_log" >&2
    rm -f -- "$tmp_log"
    ERRORS=$((ERRORS + 1))
    exit 1
  fi

  # Parse openrsync/rsync stats lines. Both emit:
  #   "Number of regular files transferred: N"  (rsync 3)
  #   "Number of files: N"                       (rsync 2.6 / openrsync)
  local transferred
  transferred="$(grep -E 'files transferred|Number of files transferred' "$tmp_log" | head -1 | grep -oE '[0-9,]+' | tail -1 | tr -d ',')"
  if [[ -z "$transferred" ]]; then
    transferred="$(grep -E '^Number of files:' "$tmp_log" | head -1 | grep -oE '[0-9,]+' | head -1 | tr -d ',')"
  fi
  transferred="${transferred:-0}"
  COPIED=$((COPIED + transferred))

  # Approximate EXCLUDED: count files in source matching any exclude pattern.
  # Cheap path-count via find restricted to the source dir to avoid scanning $HOME.
  local excluded_count
  excluded_count="$(find "$src" \( \
        -name '*.pyc' -o \
        -name '__pycache__' -o \
        -name '.pytest_cache' -o \
        -name '.mypy_cache' -o \
        -name '*.db' -o \
        -name '*.sqlite' -o \
        -name '*.db-shm' -o \
        -name '*.db-wal' -o \
        -name '.venv*' -o \
        -name 'venv*' -o \
        -name 'node_modules' -o \
        -name '.scheduler_coordinator.pid' -o \
        -name '.synaptic_voice_daemon.pid' -o \
        -name '.work_dialogue_log.jsonl' -o \
        -name '.active_session_injections.json' -o \
        -name '.ab_testing_log*.json' -o \
        -name 'family_wisdom' -o \
        -name 'memory.bak.*' \
      \) 2>/dev/null | wc -l | tr -d ' ')"
  EXCLUDED=$((EXCLUDED + excluded_count))

  rm -f -- "$tmp_log"
}

# ----------------------------------------------------------------------------
# rsync_plan_glob() — copy each docs/plans/contextdna-ide-* file
# ----------------------------------------------------------------------------
rsync_plan_glob() {
  local src_dir="$SOURCE/docs/plans"
  local dst_dir="$TARGET/docs/plans"

  if [[ ! -d "$src_dir" ]]; then
    printf 'mothership-migrate: WARN  docs/plans/ missing, skipped\n' >&2
    ERRORS=$((ERRORS + 1))
    return 0
  fi

  shopt -s nullglob
  local -a matches=( "$src_dir"/contextdna-ide-* "$src_dir"/*-contextdna-ide-* )
  shopt -u nullglob

  if (( ${#matches[@]} == 0 )); then
    printf 'mothership-migrate: WARN  no docs/plans/contextdna-ide-* files matched\n' >&2
    return 0
  fi

  if (( DRY_RUN == 0 )); then
    mkdir -p -- "$dst_dir"
  fi

  local -a rsync_args=( -a --stats )
  if (( DRY_RUN == 1 )); then
    rsync_args+=( -n -v )
  fi

  printf 'mothership-migrate: rsync docs/plans/contextdna-ide-* (%d files) -> docs/plans/\n' "${#matches[@]}" >&2

  local tmp_log
  tmp_log="$(mktemp -t mothership-rsync.XXXXXX)"
  if ! rsync "${rsync_args[@]}" "${matches[@]}" "$dst_dir/" >"$tmp_log" 2>&1; then
    printf 'mothership-migrate: ERROR rsync failed for docs/plans/contextdna-ide-*\n' >&2
    sed -e 's/^/  /' "$tmp_log" >&2
    rm -f -- "$tmp_log"
    ERRORS=$((ERRORS + 1))
    exit 1
  fi

  COPIED=$((COPIED + ${#matches[@]}))
  rm -f -- "$tmp_log"
}

# ----------------------------------------------------------------------------
# run_sanitize() — invoke $SOURCE/scripts/sanitize-rules.sh $TARGET
# ----------------------------------------------------------------------------
run_sanitize() {
  local helper="$SOURCE/scripts/sanitize-rules.sh"
  if [[ ! -f "$helper" ]]; then
    printf 'mothership-migrate: WARN  sanitize-rules.sh not found at %s — skipping sanitization\n' "$helper" >&2
    ERRORS=$((ERRORS + 1))
    return 0
  fi
  if [[ ! -x "$helper" ]]; then
    printf 'mothership-migrate: WARN  sanitize-rules.sh not executable — chmod +x recommended\n' >&2
  fi

  if (( DRY_RUN == 1 )); then
    printf 'mothership-migrate: DRY-RUN would invoke %s %s\n' "$helper" "$TARGET" >&2
    return 0
  fi

  printf 'mothership-migrate: invoking sanitize-rules.sh %s\n' "$TARGET" >&2

  local tmp_log
  tmp_log="$(mktemp -t mothership-sanitize.XXXXXX)"
  if ! bash "$helper" "$TARGET" >"$tmp_log" 2>&1; then
    printf 'mothership-migrate: ERROR sanitize-rules.sh failed (exit %d)\n' "$?" >&2
    sed -e 's/^/  /' "$tmp_log" >&2
    rm -f -- "$tmp_log"
    ERRORS=$((ERRORS + 1))
    exit 1
  fi
  cat -- "$tmp_log" >&2

  # Parse "REPLACEMENTS: N" line from helper output (contract).
  local n
  n="$(grep -E '^REPLACEMENTS:[[:space:]]*[0-9]+' "$tmp_log" | head -1 | grep -oE '[0-9]+' | head -1)"
  SANITIZED=$((SANITIZED + ${n:-0}))
  rm -f -- "$tmp_log"
}

# ----------------------------------------------------------------------------
# print_stats() — final report, always to STDERR
# ----------------------------------------------------------------------------
print_stats() {
  local target_size="unknown"
  if [[ -d "$TARGET" ]]; then
    target_size="$(du -sh -- "$TARGET" 2>/dev/null | awk '{print $1}')"
  fi

  {
    printf '\n'
    printf '============================================================\n'
    printf '  mothership-migrate report\n'
    printf '============================================================\n'
    printf '  source           : %s\n' "$SOURCE"
    printf '  target           : %s\n' "$TARGET"
    printf '  dry-run          : %s\n' "$([[ $DRY_RUN == 1 ]] && echo yes || echo no)"
    printf '  files copied     : %d\n' "$COPIED"
    printf '  files excluded   : %d\n' "$EXCLUDED"
    printf '  sanitize replac. : %d\n' "$SANITIZED"
    printf '  errors           : %d\n' "$ERRORS"
    printf '  target size      : %s\n' "$target_size"
    printf '============================================================\n'
  } >&2
}

# ----------------------------------------------------------------------------
# main()
# ----------------------------------------------------------------------------
main() {
  parse_args "$@"
  preflight

  local rel
  for rel in "${CORE_DIRS[@]}"; do
    rsync_one_dir "$rel"
  done

  rsync_plan_glob
  run_sanitize
  print_stats

  if (( ERRORS > 0 )); then
    printf 'mothership-migrate: completed with %d error(s) — exiting non-zero\n' "$ERRORS" >&2
    exit 1
  fi

  printf 'mothership-migrate: OK\n' >&2
  exit 0
}

main "$@"
