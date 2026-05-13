#!/usr/bin/env bash
# webhook-cache.sh — RACE T4 webhook cache control plane CLI.
#
# Inspect and invalidate the four TTL-based Redis caches that wrap the
# webhook injection LLM calls (S2 wisdom, S6 deep voice, S8 subconscious,
# S4 blueprint). Useful after a major config change or a fresh session
# when stale cached output would mask the new state.
#
# Usage:
#   webhook-cache stats              # hits/misses/errors per section + totals
#   webhook-cache config             # enabled flags + TTL per section
#   webhook-cache clear [s2|s6|s8|s4|all]
#                                    # invalidate one (or all) section caches
#
# Exit codes:
#   0  success
#   1  invalid invocation
#   2  Python error (Redis unreachable, import failure, etc.)
#
# Delegates all work to memory/webhook_cache_control.py so the CLI, the
# NATS event handler, and tests share exactly one implementation.

set -uo pipefail

# Resolve repo root (script lives in scripts/)
SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$( cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd )"

# Prefer the project venv if present, fall back to system python3
if [[ -x "${REPO_ROOT}/.venv/bin/python3" ]]; then
    PY="${REPO_ROOT}/.venv/bin/python3"
else
    PY="$(command -v python3 || true)"
fi

if [[ -z "${PY}" ]]; then
    echo "webhook-cache: python3 not found" >&2
    exit 2
fi

usage() {
    cat <<EOF
webhook-cache — control plane for the RACE M3/R1/S2/T2 webhook caches

Usage:
  webhook-cache stats              show hits/misses/errors per section
  webhook-cache config             show current enable flags + TTLs
  webhook-cache clear SECTION      invalidate cache (SECTION: s2|s6|s8|s4|all)

Examples:
  webhook-cache clear all          # wipe all four section caches
  webhook-cache clear s6           # wipe only the S6 deep-voice cache
EOF
}

cmd="${1:-}"
arg="${2:-}"

case "${cmd}" in
    stats)
        PYTHONPATH="${REPO_ROOT}" "${PY}" - <<'PYEOF'
import json
from memory.webhook_cache_control import cache_stats
out = cache_stats()
print(json.dumps(out, indent=2, sort_keys=True))
PYEOF
        ;;

    config)
        PYTHONPATH="${REPO_ROOT}" "${PY}" - <<'PYEOF'
import json
from memory.webhook_cache_control import cache_config
out = cache_config()
print(json.dumps(out, indent=2, sort_keys=True))
PYEOF
        ;;

    clear)
        if [[ -z "${arg}" ]]; then
            echo "webhook-cache clear: SECTION required (s2|s6|s8|s4|all)" >&2
            usage >&2
            exit 1
        fi
        case "${arg}" in
            s2|s6|s8|s4|all) ;;
            *)
                echo "webhook-cache clear: unknown section '${arg}'" >&2
                echo "  valid sections: s2, s6, s8, s4, all" >&2
                exit 1
                ;;
        esac
        SECTION="${arg}" PYTHONPATH="${REPO_ROOT}" "${PY}" - <<'PYEOF'
import json
import os
import sys
from memory.webhook_cache_control import (
    invalidate_all_webhook_caches,
    invalidate_section,
)

section = os.environ["SECTION"]
try:
    if section == "all":
        result = invalidate_all_webhook_caches()
    else:
        result = invalidate_section(section)
except Exception as e:
    print(f"webhook-cache clear failed: {e}", file=sys.stderr)
    sys.exit(2)
print(json.dumps(result, indent=2, sort_keys=True))
PYEOF
        ;;

    -h|--help|help|"")
        usage
        [[ -z "${cmd}" ]] && exit 1 || exit 0
        ;;

    *)
        echo "webhook-cache: unknown command '${cmd}'" >&2
        usage >&2
        exit 1
        ;;
esac
