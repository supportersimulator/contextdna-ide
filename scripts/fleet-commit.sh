#!/usr/bin/env bash
# fleet-commit.sh — wrapper around `git commit` that injects a fleet
# Co-Authored-By trailer identifying which node made the commit.
#
# Why: every node in the fleet (mac1, mac2, mac3, cloud) shares Aaron's
# git config, so `git log --author` always returns "Aaron Tjomsland".
# This makes honest cross-node attribution impossible. We add an extra
# Co-Authored-By trailer keyed off MULTIFLEET_NODE_ID (or hostname).
#
# Usage:
#   scripts/fleet-commit.sh -m "fix(daemon): tighten KV warmup"
#   scripts/fleet-commit.sh commit -am "..."     # extra args passed through
#
# Behaviour:
#   - Reads $MULTIFLEET_NODE_ID; falls back to `hostname -s` lowercased.
#   - If neither resolves, opt-in: NO trailer is added (silent passthrough).
#   - Existing Claude / human Co-Authored-By trailers are preserved.
#   - Idempotent: if the fleet trailer is already present, it's not
#     duplicated.
#
# The companion git hook (scripts/git-hooks/prepare-commit-msg-fleet.sh)
# applies the same logic to *any* `git commit` invocation when the env
# var is set, so this wrapper is mostly a convenience for ad-hoc usage.

set -uo pipefail

resolve_node_id() {
    if [ -n "${MULTIFLEET_NODE_ID:-}" ]; then
        printf '%s\n' "$MULTIFLEET_NODE_ID" | tr '[:upper:]' '[:lower:]'
        return 0
    fi
    local h
    h="$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')"
    case "$h" in
        mac1|mac2|mac3|cloud) printf '%s\n' "$h" ;;
        *) return 1 ;;
    esac
}

NODE_ID="$(resolve_node_id || true)"

# Normalise sub-args: support both `fleet-commit.sh -m "..."` and
# `fleet-commit.sh commit -m "..."` styles.
if [ "${1:-}" = "commit" ]; then
    shift
fi

if [ -z "${NODE_ID:-}" ]; then
    # No node identity → opt-in pattern: just forward to git commit.
    exec git commit "$@"
fi

TRAILER_LINE="Co-Authored-By: ${NODE_ID}-atlas <${NODE_ID}@fleet.local>"

# Build a tempfile holding the augmented message. We support -m/--message
# and -F/--file inputs by post-processing; for editor-driven commits we
# fall back to letting prepare-commit-msg-fleet.sh handle it.
TMP_MSG="$(mktemp -t fleet-commit.XXXXXX)"
trap 'rm -f "$TMP_MSG"' EXIT

# Walk args; if we see -m/--message we collect message bodies, otherwise
# pass through. Multiple -m flags concatenate (matching git's behaviour).
PASSTHROUGH=()
HAVE_MESSAGE=0
COLLECTED_MSG=""

while [ $# -gt 0 ]; do
    case "$1" in
        -m|--message)
            HAVE_MESSAGE=1
            if [ -n "$COLLECTED_MSG" ]; then
                COLLECTED_MSG="${COLLECTED_MSG}"$'\n\n'"${2:-}"
            else
                COLLECTED_MSG="${2:-}"
            fi
            shift 2
            ;;
        --message=*)
            HAVE_MESSAGE=1
            msg_part="${1#--message=}"
            if [ -n "$COLLECTED_MSG" ]; then
                COLLECTED_MSG="${COLLECTED_MSG}"$'\n\n'"${msg_part}"
            else
                COLLECTED_MSG="${msg_part}"
            fi
            shift 1
            ;;
        *)
            PASSTHROUGH+=("$1")
            shift
            ;;
    esac
done

if [ "$HAVE_MESSAGE" -eq 1 ]; then
    if printf '%s\n' "$COLLECTED_MSG" | grep -Fq "$TRAILER_LINE"; then
        printf '%s\n' "$COLLECTED_MSG" > "$TMP_MSG"
    else
        # Ensure exactly one blank line before trailer block.
        printf '%s\n' "$COLLECTED_MSG" > "$TMP_MSG"
        # Trim any trailing whitespace, then add a blank line + trailer.
        printf '\n%s\n' "$TRAILER_LINE" >> "$TMP_MSG"
    fi
    exec git commit -F "$TMP_MSG" "${PASSTHROUGH[@]}"
fi

# No -m given (editor mode). The prepare-commit-msg hook should add the
# trailer; if the hook isn't installed, the trailer simply won't appear.
exec git commit "${PASSTHROUGH[@]}"
