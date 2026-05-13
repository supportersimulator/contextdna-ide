#!/usr/bin/env bash
# install-capability-hook.sh — wire canonical post-commit fleet_capability publisher.
#
# Closes fleet_upgrader's capability-query loop — peers self-report
# head_sha/branch/ts to JetStream KV (`fleet_capability`) on every commit.
#
# II2 fix (2026-05-08): previously this script wrote a stripped-down inline
# hook that clobbered the canonical symlink (.git/hooks/post-commit ->
# ../../scripts/git-hooks/post-commit). The canonical hook contains venv
# priority, Stack-B docker-mount sync, log redirection, and `set +e` safety
# that the inline version dropped. Now we install the canonical hook as a
# symlink. Idempotent + backup-safe + ZSF (errors out if canonical missing).
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
HOOK=".git/hooks/post-commit"
CANONICAL_REL="scripts/git-hooks/post-commit"
CANONICAL_ABS="$REPO_ROOT/$CANONICAL_REL"
# Symlink target relative to .git/hooks/ (two levels up to repo root).
SYMLINK_TARGET="../../$CANONICAL_REL"

# ZSF: refuse to install a broken symlink. If canonical is missing, fail loud.
if [ ! -f "$CANONICAL_ABS" ]; then
  echo "[hook-install] ERROR: canonical hook missing: $CANONICAL_ABS" >&2
  echo "[hook-install] cannot install symlink to non-existent target" >&2
  exit 1
fi

chmod +x "$CANONICAL_ABS"

cd "$REPO_ROOT"

if [ -L "$HOOK" ]; then
  # Already a symlink — check target.
  CURRENT_TARGET="$(readlink "$HOOK")"
  if [ "$CURRENT_TARGET" = "$SYMLINK_TARGET" ]; then
    echo "[hook-install] ALREADY-CANONICAL: $HOOK -> $CURRENT_TARGET"
  else
    echo "[hook-install] symlink points elsewhere ($CURRENT_TARGET); repointing"
    rm "$HOOK"
    ln -s "$SYMLINK_TARGET" "$HOOK"
    echo "[hook-install] post-commit symlink installed: $HOOK -> $SYMLINK_TARGET"
  fi
elif [ -e "$HOOK" ]; then
  # Non-symlink content — back up before clobbering.
  TS="$(date +%Y%m%dT%H%M%SZ)"
  BACKUP="${HOOK}.bak.${TS}"
  echo "[hook-install] existing non-symlink hook found; preserving to $BACKUP"
  mv "$HOOK" "$BACKUP"
  ln -s "$SYMLINK_TARGET" "$HOOK"
  echo "[hook-install] post-commit symlink installed: $HOOK -> $SYMLINK_TARGET"
else
  # Fresh install.
  ln -s "$SYMLINK_TARGET" "$HOOK"
  echo "[hook-install] post-commit symlink installed: $HOOK -> $SYMLINK_TARGET"
fi

echo "[hook-install] one-time backfill:"
PYTHONPATH=. python3 tools/fleet_capability_publish.py 2>&1 | tail -3
