#!/usr/bin/env bash
# Wire scripts/git-hooks/ as the repo's hooksPath.
#
# Detects existing custom hooks and refuses to overwrite — instead prints
# instructions for manual integration. This means a fresh clone gets the
# fleet hooks installed automatically; a clone with custom user hooks is
# left alone.
#
# Usage: bash scripts/install-fleet-hooks.sh

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_DIR="$REPO_ROOT/scripts/git-hooks"

current_path="$(git -C "$REPO_ROOT" config core.hooksPath 2>/dev/null || true)"

if [ -n "$current_path" ] && [ "$current_path" != "$HOOK_DIR" ]; then
  echo "[install-fleet-hooks] core.hooksPath already set to:"
  echo "    $current_path"
  echo
  echo "Refusing to overwrite. Either:"
  echo "  1. Reset:    git config --unset core.hooksPath && bash $0"
  echo "  2. Manual:   copy $HOOK_DIR/post-commit into $current_path/"
  exit 1
fi

# Make sure the hooks directory exists with executable bits.
if [ ! -d "$HOOK_DIR" ]; then
  echo "[install-fleet-hooks] $HOOK_DIR not found — abort"
  exit 1
fi
chmod +x "$HOOK_DIR"/* 2>/dev/null || true

git -C "$REPO_ROOT" config core.hooksPath "$HOOK_DIR"

echo "[install-fleet-hooks] core.hooksPath -> $HOOK_DIR"
echo "[install-fleet-hooks] hooks installed:"
ls -1 "$HOOK_DIR" | sed 's/^/  /'

# Verify by re-reading config.
verify="$(git -C "$REPO_ROOT" config core.hooksPath)"
if [ "$verify" = "$HOOK_DIR" ]; then
  echo "[install-fleet-hooks] OK"
else
  echo "[install-fleet-hooks] FAILED to verify config — got: $verify"
  exit 1
fi
