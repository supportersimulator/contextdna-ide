#!/usr/bin/env bash
# governance-killswitch.sh — emergency rollback CLI for the governance
# kill-switch (T3 v4). Wraps memory.governance_kill_switch.
#
# Usage:
#   scripts/governance-killswitch.sh status
#   scripts/governance-killswitch.sh activate "<reason>"
#   scripts/governance-killswitch.sh deactivate
#
# Exit codes:
#   0  success
#   1  usage / arg error
#   2  python invocation failed (ZSF: error printed, NOT swallowed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Pick a Python — prefer repo venv, fall back to system python3.
if [[ -x "${REPO_ROOT}/.venv/bin/python3" ]]; then
  PY="${REPO_ROOT}/.venv/bin/python3"
else
  PY="$(command -v python3 || true)"
fi
if [[ -z "${PY}" ]]; then
  echo "ERROR: no python3 found (.venv/bin/python3 missing and python3 not on PATH)" >&2
  exit 2
fi

usage() {
  cat <<'USAGE'
governance-killswitch.sh — emergency rollback for governance enforcement

Commands:
  status                      Show current kill-switch state
  activate "<reason>"         Engage kill-switch (governance becomes pass-through)
  deactivate                  Restore governance enforcement

When killed, memory.invariants.evaluate() returns decision=allow with
reason 'KILL_SWITCH_ACTIVE: <reason>' for every proposal.
USAGE
}

cmd="${1:-}"
case "${cmd}" in
  status)
    PYTHONPATH="${REPO_ROOT}" "${PY}" - <<'PY'
from memory.governance_kill_switch import get_state, get_counters, KILL_SWITCH_PATH
s = get_state()
print(f"path:       {KILL_SWITCH_PATH}")
print(f"enabled:    {s.enabled}")
print(f"reason:     {s.reason}")
print(f"set_at:     {s.set_at}")
print(f"set_by:     {s.set_by}")
c = get_counters()
print("counters:")
for k in sorted(c):
    print(f"  {k}: {c[k]}")
PY
    ;;
  activate)
    reason="${2:-}"
    if [[ -z "${reason}" ]]; then
      echo "ERROR: activate requires a non-empty reason" >&2
      usage >&2
      exit 1
    fi
    actor="${USER:-unknown}"
    PYTHONPATH="${REPO_ROOT}" REASON="${reason}" ACTOR="${actor}" "${PY}" - <<'PY'
import os
from memory.governance_kill_switch import activate, get_state
activate(reason=os.environ["REASON"], by=os.environ["ACTOR"])
s = get_state()
print(f"KILL_SWITCH ACTIVATED enabled={s.enabled} reason={s.reason!r} by={s.set_by} at={s.set_at}")
PY
    ;;
  deactivate)
    actor="${USER:-unknown}"
    PYTHONPATH="${REPO_ROOT}" ACTOR="${actor}" "${PY}" - <<'PY'
import os
from memory.governance_kill_switch import deactivate, get_state
deactivate(by=os.environ["ACTOR"])
s = get_state()
print(f"KILL_SWITCH DEACTIVATED enabled={s.enabled} by={s.set_by} at={s.set_at}")
PY
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    echo "ERROR: unknown command: ${cmd}" >&2
    usage >&2
    exit 1
    ;;
esac
