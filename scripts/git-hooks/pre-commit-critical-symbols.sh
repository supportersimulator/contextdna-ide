#!/usr/bin/env bash
# pre-commit-critical-symbols.sh — WaveI invariant gate.
#
# Runs the critical-fallbacks pytest. If ANY symbol invariant fires (e.g. a
# `git checkout --theirs` merge wiped a critical bridge fallback), the
# commit is blocked LOUDLY with the offending symbol + role.
#
# Budget: <2s. Test file is ≤80 LOC, imports only. No I/O, no network.
# ZSF: missing pytest/PYTHONPATH does not silently skip — it fails.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
TEST="$REPO_ROOT/multi-fleet/multifleet/tests/test_critical_fallbacks_present.py"

if [ ! -f "$TEST" ]; then
    echo "[critical-symbols] FAIL: invariant test missing at $TEST" >&2
    echo "  This test itself is invariant — restore from WaveI audit." >&2
    exit 1
fi

# Fast-path: skip full runtime check when staged files don't touch any
# module in the at-risk surface. Keeps the common-case commit <100ms while
# preserving the gate's correctness for any change that COULD have wiped a
# critical symbol (including merge commits, which always touch many files).
AT_RISK_RE='^(tools/fleet_nerve_nats\.py|multi-fleet/multifleet/(channels|channel_priority|channel_scoring|delta_bundle|plist_drift)\.py|multi-fleet/multifleet/tests/test_critical_fallbacks_present\.py|scripts/git-hooks/pre-commit-critical-symbols\.sh)$'
STAGED=$(git diff --cached --name-only)
if ! echo "$STAGED" | grep -Eq "$AT_RISK_RE"; then
    # No at-risk file staged — skip the slow runtime check.
    exit 0
fi

PY="$REPO_ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

PYTHONPATH="$REPO_ROOT/multi-fleet:$REPO_ROOT" "$PY" - <<PYEOF >/tmp/wavei-critsyms.log 2>&1
import importlib, sys
sys.path.insert(0, "$REPO_ROOT/multi-fleet/multifleet/tests")
from test_critical_fallbacks_present import CRITICAL_SYMBOLS, _resolve
fail = []
for mod, sym, role in CRITICAL_SYMBOLS:
    try:
        m = importlib.import_module(mod)
    except ImportError as e:
        fail.append(f"  {mod}.{sym} ({role}): import error {e}")
        continue
    if _resolve(m, sym) is None:
        fail.append(f"  {mod}.{sym} ({role}): MISSING")
if fail:
    print("CRITICAL FLEET SYMBOLS MISSING:")
    print("\n".join(fail))
    sys.exit(1)
sys.exit(0)
PYEOF
RC=$?

if [ "$RC" -ne 0 ]; then
    echo "" >&2
    echo "[critical-symbols] BLOCKED: critical fleet symbol(s) MISSING." >&2
    echo "  A recent change (often a --theirs merge) silently deleted a" >&2
    echo "  fallback/cascade symbol. Inspect, restore, retry." >&2
    echo "  Details:" >&2
    sed 's/^/    /' /tmp/wavei-critsyms.log >&2
    exit 1
fi

exit 0
