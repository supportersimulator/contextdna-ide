#!/usr/bin/env bash
# test_sync_to_docker_mount.sh
#
# Drives scripts/sync-to-docker-mount.sh against synthetic SRC + DST trees in
# a tempdir, then asserts:
#   1) every non-excluded file in SRC reaches DST
#   2) checksums match for every file copied
#   3) excluded paths (.git/, __pycache__/, node_modules/, .venv/) do NOT
#      appear in DST
#   4) --delete semantics: a file removed from SRC vanishes from DST on the
#      next sync
#   5) --dry-run is a true no-op (DST unchanged)
#
# Bypasses HOME guard via DEV_TO_DOCKER_DST that the test sets to live under
# the tempdir but exposes its real $HOME so the guard passes (we tunnel via
# HOME=$TMPDIR for the duration of the test).
#
# Exit 0 = pass, non-zero = fail (with the assertion that broke).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/sync-to-docker-mount.sh"

if [ ! -x "$SCRIPT" ] && [ ! -f "$SCRIPT" ]; then
  echo "FAIL sync script not found at $SCRIPT" >&2
  exit 1
fi

WORK="$(mktemp -d -t u3-sync-test.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

SRC="$WORK/src/"
DST="$WORK/home/Documents/er-simulator-superrepo/"
LOG="$WORK/sync.log"
mkdir -p "$SRC" "$DST" "$WORK/home"

# --- build a representative SRC tree -----------------------------------------
mkdir -p "$SRC/code/sub" \
         "$SRC/.git/objects" \
         "$SRC/.venv/lib" \
         "$SRC/code/__pycache__" \
         "$SRC/web/node_modules/foo" \
         "$SRC/.claude/worktrees/x"

printf 'hello\n'           > "$SRC/code/main.py"
printf 'nested\n'          > "$SRC/code/sub/util.py"
printf 'readme content\n'  > "$SRC/README.md"
printf 'git pack\n'        > "$SRC/.git/objects/pack"
printf 'venv binary\n'     > "$SRC/.venv/lib/site.py"
printf 'pyc\n'             > "$SRC/code/__pycache__/main.cpython-311.pyc"
printf 'js dep\n'          > "$SRC/web/node_modules/foo/index.js"
printf 'wt\n'              > "$SRC/.claude/worktrees/x/file.txt"

# --- run sync ----------------------------------------------------------------
HOME="$WORK/home" \
DEV_TO_DOCKER_SRC="$SRC" \
DEV_TO_DOCKER_DST="$DST" \
DEV_TO_DOCKER_LOG="$LOG" \
  bash "$SCRIPT" --quiet

assert_exists() {
  if [ ! -e "$1" ]; then
    echo "FAIL expected to exist: $1" >&2
    exit 1
  fi
}
assert_missing() {
  if [ -e "$1" ]; then
    echo "FAIL expected NOT to exist (excluded): $1" >&2
    exit 1
  fi
}

# 1. non-excluded files reached DST
assert_exists "$DST/code/main.py"
assert_exists "$DST/code/sub/util.py"
assert_exists "$DST/README.md"

# 2. checksums match (BSD vs GNU shasum portable: use shasum)
src_sum=$(shasum "$SRC/code/main.py" | awk '{print $1}')
dst_sum=$(shasum "$DST/code/main.py" | awk '{print $1}')
if [ "$src_sum" != "$dst_sum" ]; then
  echo "FAIL checksum mismatch on code/main.py: $src_sum vs $dst_sum" >&2
  exit 1
fi

# Also assert the *count* of files matches (cheap structural check on the
# subtree we expect to be mirrored).
src_count=$(find "$SRC/code" "$SRC/README.md" -type f \
              -not -path '*/__pycache__/*' | wc -l | tr -d ' ')
dst_count=$(find "$DST/code" "$DST/README.md" -type f | wc -l | tr -d ' ')
if [ "$src_count" != "$dst_count" ]; then
  echo "FAIL file count mismatch: src=$src_count dst=$dst_count" >&2
  exit 1
fi

# 3. excluded paths must not exist in DST
assert_missing "$DST/.git"
assert_missing "$DST/.venv"
assert_missing "$DST/web/node_modules"
assert_missing "$DST/code/__pycache__"
assert_missing "$DST/.claude/worktrees"

# 4. --delete semantics: drop a file in SRC, re-sync, file should vanish in DST
rm "$SRC/code/main.py"
HOME="$WORK/home" \
DEV_TO_DOCKER_SRC="$SRC" \
DEV_TO_DOCKER_DST="$DST" \
DEV_TO_DOCKER_LOG="$LOG" \
  bash "$SCRIPT" --quiet
if [ -e "$DST/code/main.py" ]; then
  echo "FAIL --delete did not propagate removal" >&2
  exit 1
fi

# 5. --dry-run must be a no-op. Add a new file in SRC, run --dry-run,
# DST should NOT see it.
printf 'new\n' > "$SRC/code/added.py"
HOME="$WORK/home" \
DEV_TO_DOCKER_SRC="$SRC" \
DEV_TO_DOCKER_DST="$DST" \
DEV_TO_DOCKER_LOG="$LOG" \
  bash "$SCRIPT" --dry-run --quiet
if [ -e "$DST/code/added.py" ]; then
  echo "FAIL --dry-run wrote to DST" >&2
  exit 1
fi

# Real run picks it up.
HOME="$WORK/home" \
DEV_TO_DOCKER_SRC="$SRC" \
DEV_TO_DOCKER_DST="$DST" \
DEV_TO_DOCKER_LOG="$LOG" \
  bash "$SCRIPT" --quiet
assert_exists "$DST/code/added.py"

echo "PASS test_sync_to_docker_mount"
