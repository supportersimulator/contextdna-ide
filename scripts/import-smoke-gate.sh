#!/usr/bin/env bash
# import-smoke-gate.sh — verify critical hot-path modules import cleanly.
#
# Catches NameError-at-module-load bugs that would otherwise silently break
# runtime (e.g. webhook S4 outage on 2026-04-26: `from typing import ... Any`
# missing `List` → anticipation_engine NameError → S4 silent timeout).
#
# Each module imported in a clean subprocess so one failure can't mask others.
# Exit 0 = all clean. Exit 1 = any module failed to import.
#
# Hooked into multi-fleet/scripts/pre-publish.sh + scripts/gains-gate.sh.
# Run standalone: ./scripts/import-smoke-gate.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Critical hot-path modules (alphabetised). Any addition here means
# "this module breaking takes the system down" — keep tight.
#
# History (2026-05-06, U3): dropped memory.anticipation_engine and
# memory.webhook_batch_helper — both modules deleted from disk earlier,
# manifest still listed them so the gate fired CRITICAL on every run.
# Re-add only when a new file at memory/<name>.py reappears AND it sits
# on a hot-path (webhook/agent/llm/scheduler).
CRITICAL_MODULES=(
    "memory.agent_service"
    "memory.llm_priority_queue"
    "memory.persistent_hook_structure"
)

pass=0
fail=0
fails=()

for mod in "${CRITICAL_MODULES[@]}"; do
    if python3 -c "import $mod" 2>/dev/null; then
        echo "OK   $mod"
        pass=$((pass + 1))
    else
        # Re-run capturing stderr for the failure summary
        err=$(python3 -c "import $mod" 2>&1 | tail -3 | tr '\n' ' ')
        echo "FAIL $mod -- $err"
        fails+=("$mod")
        fail=$((fail + 1))
    fi
done

echo
echo "import-smoke: $pass/$((pass + fail)) clean"
if [[ $fail -gt 0 ]]; then
    echo "BLOCKED: ${fails[*]}"
    exit 1
fi
exit 0
