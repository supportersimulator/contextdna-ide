#!/usr/bin/env bash
# prepare-commit-msg-fleet.sh — augment commit messages with a fleet
# Co-Authored-By trailer when MULTIFLEET_NODE_ID identifies the node.
#
# Install (project-wide):
#   git config core.hooksPath scripts/git-hooks
#   ln -sf ../prepare-commit-msg-fleet.sh scripts/git-hooks/prepare-commit-msg
#
# Behaviour:
#   - Idempotent (skips if a fleet trailer is already present).
#   - Never blocks the commit; on any error we silently exit 0.
#   - Honours MULTIFLEET_NODE_ID first, then hostname { mac1|mac2|mac3|cloud }.
#   - Skips merge / squash / commit-template / message sources to avoid
#     surprising the user during rebases.

set -uo pipefail

MSG_FILE="${1:-}"
SOURCE="${2:-}"

[ -n "$MSG_FILE" ] || exit 0
[ -f "$MSG_FILE" ] || exit 0

case "$SOURCE" in
    merge|squash|commit) exit 0 ;;
esac

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

NODE_ID="$(resolve_node_id 2>/dev/null || true)"
[ -n "${NODE_ID:-}" ] || exit 0

TRAILER_LINE="Co-Authored-By: ${NODE_ID}-atlas <${NODE_ID}@fleet.local>"

# Already present?
if grep -Fq "$TRAILER_LINE" "$MSG_FILE" 2>/dev/null; then
    exit 0
fi

# Append; ensure separating blank line if file doesn't already end with one.
{
    if [ -s "$MSG_FILE" ]; then
        # Ensure file ends with a newline.
        tail -c1 "$MSG_FILE" | od -An -c | grep -q '\\n' || printf '\n' >> "$MSG_FILE"
        # Ensure blank-line separator before trailer block.
        last_line="$(tail -n1 "$MSG_FILE" 2>/dev/null || true)"
        if [ -n "$last_line" ] && ! printf '%s' "$last_line" | grep -Eq '^[A-Za-z-]+: '; then
            printf '\n' >> "$MSG_FILE"
        fi
    fi
    printf '%s\n' "$TRAILER_LINE" >> "$MSG_FILE"
} 2>/dev/null || exit 0

exit 0
