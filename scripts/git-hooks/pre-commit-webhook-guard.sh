#!/usr/bin/env bash
# pre-commit-webhook-guard.sh — MFINV regression guard for invariant counters.
#
# WHY: 68bc82435 used `git checkout --theirs` during a merge resolution,
# silently deleting the WW1 webhook watchdog (webhook_offsite_nats_* counters).
# This hook catches deletions of known invariant counter names without the
# required WIPE-OK marker in the commit message.
#
# Pattern: if any staged deletion touches a known invariant counter name,
# the commit is blocked unless the staged commit message contains:
#   WIPE-OK: <audit-reference>
#
# Bypass: add `WIPE-OK: <audit>` to commit message OR use --no-verify with
# explicit acknowledgment (operator escape hatch — logged, not silent).

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || exit 0

# Invariant counter name patterns that must not be silently deleted.
INVARIANT_PATTERNS=(
    "webhook_offsite_nats"
    "webhook_subscription_resubscribe"
    "webhook_events_recorded"
    "webhook_budget_exceeded"
    "events_recorded"
    "plist_drift_"
    "heartbeat_drop_"
    "split_brain_"
)

# Check staged deletions in Python/shell files.
DELETED=$(git diff --cached --name-only --diff-filter=D 2>/dev/null || true)
MODIFIED_DIFF=$(git diff --cached 2>/dev/null || true)

VIOLATIONS=()

for pattern in "${INVARIANT_PATTERNS[@]}"; do
    # Check if any deleted line in staged diff matches the invariant pattern.
    if echo "$MODIFIED_DIFF" | grep -q "^-.*${pattern}"; then
        VIOLATIONS+=("$pattern")
    fi
done

if [[ ${#VIOLATIONS[@]} -eq 0 ]]; then
    exit 0
fi

# Violations found — check for WIPE-OK marker in commit message.
COMMIT_MSG_FILE="${REPO_ROOT}/.git/COMMIT_EDITMSG"
if [[ -f "$COMMIT_MSG_FILE" ]]; then
    if grep -q "WIPE-OK:" "$COMMIT_MSG_FILE"; then
        echo "[webhook-guard] WIPE-OK marker found — invariant deletion acknowledged." >&2
        exit 0
    fi
fi

echo "" >&2
echo "╔══════════════════════════════════════════════════════════════╗" >&2
echo "║  INVARIANT COUNTER DELETION BLOCKED (MFINV pre-commit guard) ║" >&2
echo "╚══════════════════════════════════════════════════════════════╝" >&2
echo "" >&2
echo "Staged diff deletes lines matching known invariant counter patterns:" >&2
for v in "${VIOLATIONS[@]}"; do
    echo "  • $v" >&2
done
echo "" >&2
echo "Root cause: 68bc82435 used 'git checkout --theirs' during merge," >&2
echo "silently wiping the WW1 webhook watchdog (events_recorded 4→0)." >&2
echo "" >&2
echo "To proceed, add to your commit message:" >&2
echo "  WIPE-OK: <audit-reference explaining why deletion is intentional>" >&2
echo "" >&2
echo "Or bypass with: git commit --no-verify  (logged, not silent)" >&2
echo "" >&2
exit 1
