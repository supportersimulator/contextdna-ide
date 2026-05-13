#!/usr/bin/env bash
# pre-commit-conflict-markers.sh — WaveG (2026-05-12)
#
# Reject ANY commit whose staged content contains git merge conflict
# markers (`<<<<<<< `, bare `=======`, `>>>>>>> `). Repo-wide guard
# against the bandaid pattern that produced commit 68bc82435 (silent
# deletion of _try_superset_fallback after a botched fleet-state.json
# merge).
#
# Why a hook and not just the bot: the bot owns ONE file. A human or
# another bot can still land markers elsewhere. The hook is the
# class-level fix.
#
# Bypass (emergency only): git commit --no-verify

set -uo pipefail

STAGED=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$STAGED" ] && exit 0

# Skip binary files and this hook itself.
SKIP_PATTERN='(pre-commit-conflict-markers\.sh$|\.png$|\.jpg$|\.jpeg$|\.gif$|\.pdf$|\.zip$|\.tar$|\.gz$)'

FAIL=0
HITS=""

while IFS= read -r f; do
    [ -z "$f" ] && continue
    echo "$f" | grep -qE "$SKIP_PATTERN" && continue
    [ -f "$f" ] || continue
    # Inspect only ADDED lines in the staged diff.
    if git diff --cached -- "$f" \
        | grep -E '^\+' | grep -vE '^\+\+\+' \
        | grep -E '^\+(<<<<<<< |>>>>>>> |=======$)' >/dev/null; then
        HITS="${HITS}  ${f}\n"
        FAIL=1
    fi
done <<< "$STAGED"

if [ "$FAIL" -eq 1 ]; then
    printf "BLOCKED: git conflict markers (<<<<<<<, =======, >>>>>>>) detected in staged diff:\n"
    printf "%b" "$HITS"
    printf "\nResolve the conflict before committing. Bypass: git commit --no-verify (NOT recommended).\n"
    exit 1
fi

exit 0
