#!/usr/bin/env bash
# cleanup-stale-worktrees.sh — Auto-cleanup stale agent worktrees
#
# Finds agent-created worktrees in .claude/worktrees/agent-* that are SAFE
# to remove and unlocks/removes them. Conservative: skips anything with
# uncommitted changes, recent activity, or unmerged work.
#
# Eligibility (ALL must be true):
#   1. Path matches .claude/worktrees/agent-*
#   2. No uncommitted changes (porcelain status empty)
#   3. Last file mtime under the worktree > AGE_DAYS old (default 7)
#   4. Branch tip is reachable from origin/main (already merged)
#      OR branch does not exist on origin (orphan)
#
# Usage:
#   ./scripts/cleanup-stale-worktrees.sh           # live cleanup
#   ./scripts/cleanup-stale-worktrees.sh --dry-run # show what WOULD be cleaned
#   AGE_DAYS=14 ./scripts/cleanup-stale-worktrees.sh
#
# Exit codes: 0 success, 1 fatal error.

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

REPO_ROOT="${REPO_ROOT:-$HOME/dev/er-simulator-superrepo}"
AGE_DAYS="${AGE_DAYS:-7}"
WORKTREE_GLOB="${REPO_ROOT}/.claude/worktrees/agent-*"

CLEANED=0
SKIPPED_DIRTY=0
SKIPPED_RECENT=0
SKIPPED_UNMERGED=0
SKIPPED_OTHER=0
ERRORS=0

log() { printf '%s\n' "$*"; }

if [[ ! -d "$REPO_ROOT/.git" ]]; then
    log "FATAL: not a git repo: $REPO_ROOT"
    exit 1
fi

cd "$REPO_ROOT"

log "=== Stale Worktree Cleanup ==="
log "Repo:       $REPO_ROOT"
log "Mode:       $( $DRY_RUN && echo 'DRY RUN' || echo 'LIVE' )"
log "Age cutoff: ${AGE_DAYS} days"
log ""

# Refresh remote refs (best-effort; no network = use cached refs)
git fetch --quiet origin main 2>/dev/null || log "  (warn: git fetch failed; using cached refs)"

MAIN_TIP="$(git rev-parse origin/main 2>/dev/null || git rev-parse main 2>/dev/null || echo '')"
if [[ -z "$MAIN_TIP" ]]; then
    log "FATAL: cannot resolve origin/main or main"
    exit 1
fi

# Cutoff in epoch seconds
NOW_EPOCH="$(date +%s)"
CUTOFF_EPOCH=$(( NOW_EPOCH - AGE_DAYS * 86400 ))

# Iterate candidate worktrees
shopt -s nullglob
candidates=( $WORKTREE_GLOB )
shopt -u nullglob

if [[ ${#candidates[@]} -eq 0 ]]; then
    log "No agent worktrees found under $WORKTREE_GLOB"
    log ""
    log "=== Summary ==="
    log "  Cleaned: 0"
    exit 0
fi

for wt_path in "${candidates[@]}"; do
    [[ -d "$wt_path" ]] || continue
    wt_name="$(basename "$wt_path")"

    log "--- $wt_name ---"

    # Resolve branch (best-effort).
    branch=""
    if [[ -f "$wt_path/.git" ]]; then
        # .git is a file in worktrees; look up via git
        branch="$(git -C "$wt_path" branch --show-current 2>/dev/null || echo '')"
    fi

    # Check 1: uncommitted changes
    if ! status_out="$(git -C "$wt_path" status --porcelain 2>/dev/null)"; then
        log "  SKIP (status failed; possibly broken worktree — leaving alone)"
        SKIPPED_OTHER=$((SKIPPED_OTHER + 1))
        continue
    fi
    if [[ -n "$status_out" ]]; then
        dirty_count=$(printf '%s\n' "$status_out" | wc -l | tr -d ' ')
        log "  SKIP (uncommitted changes: $dirty_count files)"
        SKIPPED_DIRTY=$((SKIPPED_DIRTY + 1))
        continue
    fi

    # Check 2: recent activity (max mtime of regular files, top-level depth 3)
    # Subshell isolates pipefail / partial failures from set -e.
    newest_mtime=$(
        set +e +o pipefail
        find "$wt_path" -maxdepth 3 -type f -not -path '*/.git/*' -print0 2>/dev/null \
            | xargs -0 stat -f '%m' 2>/dev/null \
            | sort -nr \
            | head -1
    )
    newest_mtime="${newest_mtime%%[!0-9]*}"
    [[ -z "$newest_mtime" ]] && newest_mtime=0

    if (( newest_mtime > CUTOFF_EPOCH )); then
        age_days=$(( (NOW_EPOCH - newest_mtime) / 86400 ))
        log "  SKIP (recent activity: ${age_days}d old, cutoff ${AGE_DAYS}d)"
        SKIPPED_RECENT=$((SKIPPED_RECENT + 1))
        continue
    fi

    # Check 3: branch reachability — tip must be reachable from main, OR branch absent on origin.
    branch_safe=false
    if [[ -z "$branch" ]] || [[ "$branch" == "HEAD" ]]; then
        # Detached HEAD — treat as orphan
        branch_safe=true
        reason="detached HEAD"
    else
        tip="$(git -C "$wt_path" rev-parse HEAD 2>/dev/null || echo '')"
        if [[ -z "$tip" ]]; then
            log "  SKIP (cannot resolve HEAD)"
            SKIPPED_OTHER=$((SKIPPED_OTHER + 1))
            continue
        fi
        # Already merged into main?
        if git merge-base --is-ancestor "$tip" "$MAIN_TIP" 2>/dev/null; then
            branch_safe=true
            reason="merged into main"
        elif ! git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
            # Branch absent on origin — orphan worktree branch
            branch_safe=true
            reason="branch not on origin (orphan)"
        else
            branch_safe=false
        fi
    fi

    if ! $branch_safe; then
        log "  SKIP (unmerged: branch '$branch' has work not on main)"
        SKIPPED_UNMERGED=$((SKIPPED_UNMERGED + 1))
        continue
    fi

    log "  ELIGIBLE: $reason; branch='$branch'"

    if $DRY_RUN; then
        log "  WOULD: git worktree unlock '$wt_path'"
        log "  WOULD: git worktree remove --force '$wt_path'"
        if [[ -n "$branch" ]] && [[ "$branch" != "HEAD" ]]; then
            log "  WOULD: git branch -D '$branch'"
        fi
        CLEANED=$((CLEANED + 1))
        continue
    fi

    # Live cleanup
    git worktree unlock "$wt_path" 2>/dev/null || true
    if git worktree remove --force "$wt_path" 2>/dev/null; then
        log "  REMOVED worktree"
        if [[ -n "$branch" ]] && [[ "$branch" != "HEAD" ]]; then
            if git branch -D "$branch" 2>/dev/null; then
                log "  DELETED branch $branch"
            else
                log "  (branch $branch not deleted — may still be referenced)"
            fi
        fi
        CLEANED=$((CLEANED + 1))
    else
        log "  ERROR: git worktree remove failed"
        ERRORS=$((ERRORS + 1))
    fi
done

log ""
log "=== Summary ==="
log "  Cleaned:           $CLEANED"
log "  Skipped (dirty):   $SKIPPED_DIRTY"
log "  Skipped (recent):  $SKIPPED_RECENT"
log "  Skipped (unmerged):$SKIPPED_UNMERGED"
log "  Skipped (other):   $SKIPPED_OTHER"
log "  Errors:            $ERRORS"

# Prune any dangling admin entries (only in live mode)
if ! $DRY_RUN && (( CLEANED > 0 )); then
    git worktree prune 2>/dev/null || true
fi

# Surface non-zero exit on errors so launchd/CI can flag them.
[[ $ERRORS -eq 0 ]] || exit 1
exit 0
