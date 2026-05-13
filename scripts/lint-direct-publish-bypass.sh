#!/usr/bin/env bash
# WaveK 2026-05-12 — Cascade-mandatory-entry lint wrapper.
#
# Forbids direct nc.publish to peer-message subjects
# (fleet.message.*, fleet.reply.*, fleet.context.*, fleet.peer.*, rpc.peer.*)
# outside the dispatcher allowlist. Thin shell around the canonical Python
# scanner so pre-commit hooks, gains-gate, and CI all share ONE source of
# truth (multi-fleet/multifleet/extraction_contract.json).
#
# Root cause this guards: WaveK proved no live bypass exists today; this
# lint prevents the regression that would turn the Ferrari off again.
#
# Exit codes: 0 pass | 1 violation | 2 scanner-broken (advisory)
#
# Usage:
#   scripts/lint-direct-publish-bypass.sh           # quiet
#   scripts/lint-direct-publish-bypass.sh -v        # verbose
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERBOSE=""
[ "${1:-}" = "-v" ] || [ "${1:-}" = "--verbose" ] && VERBOSE="--verbose"

PY="python3"
[ -x "$REPO_ROOT/.venv/bin/python3" ] && PY="$REPO_ROOT/.venv/bin/python3"

PYTHONPATH="$REPO_ROOT/multi-fleet:${PYTHONPATH:-}" \
    "$PY" -m multifleet.contract_check $VERBOSE
rc=$?

if [ "$rc" -eq 2 ]; then
    echo "[lint-direct-publish-bypass] scanner internal error — advisory only" >&2
fi
exit "$rc"
