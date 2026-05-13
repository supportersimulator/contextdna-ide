#!/usr/bin/env bash
# install-hooks.sh — RACE Q3 (extended in RACE Y5)
#
# Installs the repo's pre-commit dispatcher into .git/hooks/pre-commit.
# The dispatcher composes three hooks:
#
#   1. scripts/pre-commit-secrets-check.sh         (gate — blocks commit on fail)
#   2. scripts/git-hooks/pre-commit-zsf-smoke.sh   (gate — RACE Q3)
#   3. scripts/git-hooks/pre-commit-north-star.sh  (advisory — RACE Y5)
#
# Idempotent: re-running this script is a no-op once the Y5 dispatcher is in
# place. If a legacy Q3 dispatcher is detected, it is upgraded automatically.
#
# Worktree-safe: dispatcher path resolves through `git rev-parse
# --git-common-dir` so the same script works from main checkout or worktrees.
#
# Usage: bash scripts/install-hooks.sh

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_SRC_DIR="$REPO_ROOT/scripts/git-hooks"
ZSF_HOOK="$HOOK_SRC_DIR/pre-commit-zsf-smoke.sh"
NORTH_STAR_HOOK="$HOOK_SRC_DIR/pre-commit-north-star.sh"
SECRETS_HOOK="$REPO_ROOT/scripts/pre-commit-secrets-check.sh"

GIT_DIR="$(git rev-parse --git-common-dir)"
case "$GIT_DIR" in
  /*) ;;
  *) GIT_DIR="$REPO_ROOT/$GIT_DIR" ;;
esac
DEST="$GIT_DIR/hooks/pre-commit"

if [ ! -x "$ZSF_HOOK" ]; then
  chmod +x "$ZSF_HOOK" 2>/dev/null || true
fi
if [ ! -f "$ZSF_HOOK" ]; then
  echo "[install-hooks] missing $ZSF_HOOK — abort"
  exit 1
fi

# RACE Y5 — north-star advisory hook is optional but desired.
if [ -f "$NORTH_STAR_HOOK" ] && [ ! -x "$NORTH_STAR_HOOK" ]; then
  chmod +x "$NORTH_STAR_HOOK" 2>/dev/null || true
fi

mkdir -p "$(dirname "$DEST")"

# Dispatcher marker bumped to Y5 so older Q3 dispatchers are upgraded in-place.
DISPATCHER_MARK="# RACE-Y5-DISPATCHER"
LEGACY_MARK="# RACE-Q3-DISPATCHER"

if [ -f "$DEST" ] && grep -q "$DISPATCHER_MARK" "$DEST" 2>/dev/null; then
  echo "[install-hooks] dispatcher already installed at $DEST (idempotent no-op)"
  exit 0
fi

# Upgrade path: if the legacy Q3 dispatcher is present, replace it (no backup
# — the content is reproducible and tracked in git).
if [ -f "$DEST" ] && grep -q "$LEGACY_MARK" "$DEST" 2>/dev/null; then
  rm -f "$DEST"
  echo "[install-hooks] upgrading Q3 dispatcher -> Y5 (north-star advisory)"
fi

# If a non-dispatcher hook exists (e.g. the legacy symlink to
# pre-commit-secrets-check.sh), back it up before replacing.
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  BACKUP="$DEST.pre-race-y5.$(date +%s)"
  mv "$DEST" "$BACKUP"
  echo "[install-hooks] existing hook moved to $BACKUP"
fi

cat > "$DEST" <<'EOF'
#!/usr/bin/env bash
# RACE-Y5-DISPATCHER — runs all repo pre-commit hooks in sequence.
# Hooks 1-2 are GATES (commit fails if they reject). Hook 3 is ADVISORY
# (always exits 0; surfaces north-star drift only).
# Legacy: # RACE-Q3-DISPATCHER (kept in this comment for grep history).
set -uo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"

# 1. Secrets / home-path / token leak guard (existing).
if [ -x "$REPO_ROOT/scripts/pre-commit-secrets-check.sh" ]; then
  "$REPO_ROOT/scripts/pre-commit-secrets-check.sh" || exit 1
fi

# 2. ZSF + import-smoke gate (RACE Q3).
if [ -x "$REPO_ROOT/scripts/git-hooks/pre-commit-zsf-smoke.sh" ]; then
  "$REPO_ROOT/scripts/git-hooks/pre-commit-zsf-smoke.sh" || exit 1
fi

# 3. North-star drift detector (RACE Y5) — ADVISORY, never blocks.
if [ -x "$REPO_ROOT/scripts/git-hooks/pre-commit-north-star.sh" ]; then
  "$REPO_ROOT/scripts/git-hooks/pre-commit-north-star.sh" || true
fi

exit 0
EOF
chmod +x "$DEST"

echo "[install-hooks] installed pre-commit dispatcher at:"
echo "    $DEST"
echo "[install-hooks] active hooks:"
echo "    1. scripts/pre-commit-secrets-check.sh         (gate)"
echo "    2. scripts/git-hooks/pre-commit-zsf-smoke.sh   (gate)"
echo "    3. scripts/git-hooks/pre-commit-north-star.sh  (advisory)"
echo "[install-hooks] bypass (emergency only): git commit --no-verify"
