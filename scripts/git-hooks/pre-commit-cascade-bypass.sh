#!/usr/bin/env bash
# WaveK 2026-05-12 — Pre-commit gate: forbid direct nc.publish to peer
# subjects outside the dispatcher allowlist. Skips when no .py is staged.
#
# Root cause guarded: dispatcher-as-mandatory-entry invariant. Without
# this gate, a future commit could silently bypass send_with_fallback
# and turn the Ferrari engine off again (HHH1/delta_bundle/INV-008 idle).
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
staged_py="$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.py$' || true)"
[ -z "$staged_py" ] && exit 0

"$REPO_ROOT/scripts/lint-direct-publish-bypass.sh"
rc=$?
# rc=2 (scanner internal) is advisory per WaveK spec — don't block commits
# on a broken scanner; it logged to stderr already.
[ "$rc" -eq 2 ] && exit 0
exit "$rc"
