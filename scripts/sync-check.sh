#!/bin/bash
# =============================================================================
# Multi-Machine Sync Check
# =============================================================================
# Verifies superrepo + submodules are in sync with their remotes.
# Can run standalone or as part of post-commit hook (background, non-blocking).
#
# Usage:
#   ./scripts/sync-check.sh          # Full check with colored output
#   ./scripts/sync-check.sh --quiet  # Exit code only (0=synced, 1=behind)
#   ./scripts/sync-check.sh --hook   # Post-commit mode (background, warns only)
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

QUIET=false
HOOK_MODE=false
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=true ;;
    --hook)  HOOK_MODE=true; QUIET=true ;;
  esac
done

OUT_OF_SYNC=0
DETAILS=""

# ---------------------------------------------------------------------------
# Check a repo: local HEAD vs remote HEAD
# Args: $1=name, $2=path, $3=remote (default: origin), $4=branch (default: main)
# ---------------------------------------------------------------------------
check_repo() {
  local name="$1"
  local path="$2"
  local remote="${3:-origin}"
  local branch="${4:-main}"

  if [ ! -d "$path/.git" ] && [ ! -f "$path/.git" ]; then
    DETAILS="${DETAILS}\n  ${YELLOW}${name}${NC}: not a git repo (skipped)"
    return
  fi

  # Fetch latest (timeout 5s, fail silently)
  git -C "$path" fetch "$remote" "$branch" --quiet 2>/dev/null || {
    DETAILS="${DETAILS}\n  ${YELLOW}${name}${NC}: fetch failed (offline?)"
    return
  }

  local LOCAL_HEAD
  LOCAL_HEAD=$(git -C "$path" rev-parse HEAD 2>/dev/null)
  local REMOTE_HEAD
  REMOTE_HEAD=$(git -C "$path" rev-parse "${remote}/${branch}" 2>/dev/null)

  if [ -z "$LOCAL_HEAD" ] || [ -z "$REMOTE_HEAD" ]; then
    DETAILS="${DETAILS}\n  ${YELLOW}${name}${NC}: could not resolve heads"
    return
  fi

  if [ "$LOCAL_HEAD" = "$REMOTE_HEAD" ]; then
    DETAILS="${DETAILS}\n  ${GREEN}${name}${NC}: synced (${LOCAL_HEAD:0:7})"
  else
    # Determine ahead/behind
    local AHEAD BEHIND
    AHEAD=$(git -C "$path" rev-list --count "${remote}/${branch}..HEAD" 2>/dev/null || echo "?")
    BEHIND=$(git -C "$path" rev-list --count "HEAD..${remote}/${branch}" 2>/dev/null || echo "?")
    DETAILS="${DETAILS}\n  ${RED}${name}${NC}: OUT OF SYNC — local ${LOCAL_HEAD:0:7} vs remote ${REMOTE_HEAD:0:7} (ahead:${AHEAD} behind:${BEHIND})"
    OUT_OF_SYNC=1
  fi
}

# ---------------------------------------------------------------------------
# Run checks
# ---------------------------------------------------------------------------
check_repo "superrepo" "$REPO_ROOT"
check_repo "shift-trades" "$REPO_ROOT/shift-trades"

# admin.contextdna.io — only check if it exists and has a remote
if [ -d "$REPO_ROOT/admin.contextdna.io" ]; then
  check_repo "admin.contextdna.io" "$REPO_ROOT/admin.contextdna.io"
fi

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
if [ "$HOOK_MODE" = true ]; then
  # Post-commit hook mode: only warn if out of sync
  if [ $OUT_OF_SYNC -eq 1 ]; then
    echo -e "${YELLOW}[sync-check] Repos out of sync with remote:${NC}" >&2
    echo -e "$DETAILS" | grep "OUT OF SYNC" >&2
    echo -e "${YELLOW}Run: ./scripts/sync-check.sh for details${NC}" >&2
  fi
  exit $OUT_OF_SYNC
fi

if [ "$QUIET" = true ]; then
  exit $OUT_OF_SYNC
fi

# Full output mode
echo -e "${CYAN}Multi-Machine Sync Check${NC}"
echo -e "────────────────────────────────────"
echo -e "$DETAILS"
echo ""

if [ $OUT_OF_SYNC -eq 0 ]; then
  echo -e "${GREEN}All repos in sync.${NC}"
else
  echo -e "${RED}Some repos are out of sync. Run 'git pull' where needed.${NC}"
fi

exit $OUT_OF_SYNC
