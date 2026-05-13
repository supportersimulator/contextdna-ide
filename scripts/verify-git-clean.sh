#!/usr/bin/env bash
# verify-git-clean.sh — detect and auto-repair stale git state.
#
# Hunts the specific failure modes that have plagued mac2 sessions:
#   1. `.git/rebase-merge/` left behind from an interrupted rebase
#      → surfaces on every `git status` as "interactive rebase in progress"
#   2. `.git/rebase-apply/` left behind from an interrupted `git am`
#   3. Detached HEAD (rebase abandoned mid-flight) when origin/main is
#      a descendant we can safely re-attach to.
#
# Exit codes:
#   0 — clean (or auto-repaired successfully)
#   1 — corruption detected, manual intervention required
#   2 — repository not found / git unavailable
#
# Flags:
#   --dry-run  report without mutating state
#   --quiet    no output unless a repair is performed
#
# This script is idempotent and safe to run from cron / xbar / fleet-check.

set -uo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
STALE_AGE_SECONDS="${STALE_AGE_SECONDS:-3600}"  # 1h default
LOG_PREFIX="[verify-git-clean]"

DRY_RUN=0
QUIET=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --quiet)   QUIET=1 ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
    esac
done

_log() {
    [ "$QUIET" = "1" ] && return 0
    echo "$LOG_PREFIX $*" >&2
}

_log_always() {
    echo "$LOG_PREFIX $*" >&2
}

if [ ! -d "$REPO_ROOT/.git" ]; then
    _log_always "ERROR: $REPO_ROOT is not a git repository"
    exit 2
fi

cd "$REPO_ROOT" || exit 2

# ── Compute mtime age of a directory in seconds ──
_dir_age_seconds() {
    local dir="$1"
    [ -e "$dir" ] || { echo "0"; return; }
    local mtime now
    if mtime=$(stat -f '%m' "$dir" 2>/dev/null); then
        :
    elif mtime=$(stat -c '%Y' "$dir" 2>/dev/null); then
        :
    else
        echo "0"; return
    fi
    now=$(date +%s)
    echo $(( now - mtime ))
}

REPAIRED=0
CORRUPT=0

# ── Check 1: stale .git/rebase-merge/ ──
if [ -d ".git/rebase-merge" ]; then
    age=$(_dir_age_seconds ".git/rebase-merge")
    if [ "$age" -gt "$STALE_AGE_SECONDS" ]; then
        _log_always "stale .git/rebase-merge/ (age=${age}s) — auto-cleanup"
        if [ "$DRY_RUN" = "0" ]; then
            git rebase --abort 2>/dev/null || true
            rm -rf .git/rebase-merge
            REPAIRED=1
        fi
    else
        _log_always "recent .git/rebase-merge/ (age=${age}s) — leaving alone (user may be rebasing)"
        CORRUPT=1
    fi
fi

# ── Check 2: stale .git/rebase-apply/ ──
if [ -d ".git/rebase-apply" ]; then
    age=$(_dir_age_seconds ".git/rebase-apply")
    if [ "$age" -gt "$STALE_AGE_SECONDS" ]; then
        _log_always "stale .git/rebase-apply/ (age=${age}s) — auto-cleanup"
        if [ "$DRY_RUN" = "0" ]; then
            git am --abort 2>/dev/null || true
            rm -rf .git/rebase-apply
            REPAIRED=1
        fi
    else
        _log_always "recent .git/rebase-apply/ (age=${age}s) — leaving alone"
        CORRUPT=1
    fi
fi

# ── Check 3: detached HEAD ──
if ! git symbolic-ref -q HEAD >/dev/null 2>&1; then
    current_sha=$(git rev-parse HEAD 2>/dev/null || echo "")
    _log_always "detached HEAD at ${current_sha:0:8} — attempting re-attach to main"
    if [ "$DRY_RUN" = "0" ]; then
        # Fetch first so origin/main is current.
        git fetch origin main 2>/dev/null || true
        # Only re-attach if current HEAD is an ancestor of origin/main
        # (i.e. nothing unique would be lost).
        if git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
            if git checkout main 2>/dev/null && \
               git merge --ff-only origin/main 2>/dev/null; then
                _log_always "re-attached to main, fast-forwarded to origin/main"
                REPAIRED=1
            else
                _log_always "checkout main failed — manual intervention required"
                CORRUPT=1
            fi
        else
            _log_always "HEAD has commits not on origin/main — NOT auto-repairing"
            CORRUPT=1
        fi
    fi
fi

if [ "$REPAIRED" = "1" ]; then
    _log_always "repair complete"
fi

if [ "$CORRUPT" = "1" ]; then
    exit 1
fi

[ "$QUIET" = "0" ] && [ "$REPAIRED" = "0" ] && _log "clean"
exit 0
