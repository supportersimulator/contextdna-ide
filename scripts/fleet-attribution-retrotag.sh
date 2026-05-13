#!/usr/bin/env bash
# fleet-attribution-retrotag.sh — non-destructive retro-tagging via git notes.
#
# Walks the last N commits, parses the subject for a node prefix
# (mac1|mac2|mac3|cloud) and writes a `git notes` entry under the
# `fleet-attribution` ref recording the inferred node. History is NOT
# rewritten.
#
# Usage:
#   scripts/fleet-attribution-retrotag.sh           # last 50 commits
#   scripts/fleet-attribution-retrotag.sh 100       # last 100 commits
#
# Patterns recognised (case-insensitive, first match wins):
#   ^mac[123]:                       e.g. "mac3: feat X"
#   ^cloud:                          e.g. "cloud: chore X"
#   ^fleet\(cloud\):                 e.g. "fleet(cloud): P0 inbox check"
#   ^fleet-msg: mac[123]             e.g. "fleet-msg: mac1 processed N"
#   ^fleet-msg: cloud                e.g. "fleet-msg: cloud inbox check"
#   contains "MULTIFLEET_NODE_ID=<n>"  hint in body
#
# Already-noted commits are skipped (idempotent).

set -uo pipefail

LIMIT="${1:-50}"
NOTES_REF="fleet-attribution"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO_ROOT" ] || { echo "not a git repo" >&2; exit 1; }
cd "$REPO_ROOT"

infer_node() {
    local subject="$1" body="$2" node=""
    # Subject patterns
    case "$subject" in
        mac1:*|MAC1:*) node="mac1" ;;
        mac2:*|MAC2:*) node="mac2" ;;
        mac3:*|MAC3:*) node="mac3" ;;
        cloud:*|CLOUD:*) node="cloud" ;;
    esac
    if [ -z "$node" ]; then
        case "$subject" in
            *"fleet(cloud)"*|*"fleet(CLOUD)"*) node="cloud" ;;
        esac
    fi
    if [ -z "$node" ]; then
        # fleet-msg: <node> ...
        if [[ "$subject" =~ fleet-msg:[[:space:]]+(mac1|mac2|mac3|cloud) ]]; then
            node="${BASH_REMATCH[1]}"
        fi
    fi
    if [ -z "$node" ]; then
        # explicit hint in body
        if [[ "$body" =~ MULTIFLEET_NODE_ID=(mac1|mac2|mac3|cloud) ]]; then
            node="${BASH_REMATCH[1]}"
        fi
    fi
    # Also detect existing trailer (already correctly attributed)
    if [ -z "$node" ]; then
        if [[ "$body" =~ Co-Authored-By:[[:space:]]+(mac1|mac2|mac3|cloud)-atlas ]]; then
            node="${BASH_REMATCH[1]}"
        fi
    fi
    printf '%s' "$node"
}

TAGGED=0
SKIPPED=0
NOMATCH=0

# Iterate; %H sha, %s subject. Body fetched separately to handle newlines.
while read -r SHA SUBJECT; do
    [ -n "$SHA" ] || continue
    BODY="$(git show -s --format=%B "$SHA" 2>/dev/null || true)"

    # Skip if a fleet-attribution note already exists.
    if git notes --ref="$NOTES_REF" show "$SHA" >/dev/null 2>&1; then
        SKIPPED=$((SKIPPED+1))
        continue
    fi

    NODE="$(infer_node "$SUBJECT" "$BODY")"
    if [ -z "$NODE" ]; then
        NOMATCH=$((NOMATCH+1))
        continue
    fi

    NOTE_TEXT="inferred-node: ${NODE}
source: subject-prefix-or-body-hint
retro-tagged-by: fleet-attribution-retrotag.sh
retro-tagged-at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if git notes --ref="$NOTES_REF" add -m "$NOTE_TEXT" "$SHA" >/dev/null 2>&1; then
        TAGGED=$((TAGGED+1))
    else
        # Add can fail in detached states / shallow clones — fall back to append.
        git notes --ref="$NOTES_REF" append -m "$NOTE_TEXT" "$SHA" >/dev/null 2>&1 \
            && TAGGED=$((TAGGED+1)) \
            || NOMATCH=$((NOMATCH+1))
    fi
done < <(git log -n "$LIMIT" --pretty=format:'%H %s')

printf 'retro-tagged: %d\n' "$TAGGED"
printf 'already-noted: %d\n' "$SKIPPED"
printf 'no-pattern-match: %d\n' "$NOMATCH"
printf 'view: git log --show-notes=%s -n %d\n' "$NOTES_REF" "$LIMIT"
