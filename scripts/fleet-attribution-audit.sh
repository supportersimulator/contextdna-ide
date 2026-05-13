#!/usr/bin/env bash
# fleet-attribution-audit.sh — honest cross-node attribution tally.
#
# Reads `Co-Authored-By: <node>-atlas <...>` trailers from commit
# messages and tallies commit count, lines added, lines removed, and
# tests touched per node. This is the "fix at the source" companion to
# scripts/fleet-commit.sh — git author is always Aaron Tjomsland because
# the fleet shares his config, so node attribution lives in trailers.
#
# Usage:
#   scripts/fleet-attribution-audit.sh                # all branches
#   scripts/fleet-attribution-audit.sh --range main~50..HEAD
#   scripts/fleet-attribution-audit.sh --node mac2    # single node detail
#   scripts/fleet-attribution-audit.sh --json         # machine-readable
#
# Honest comparison rules:
#   - Untagged commits show in an "(unattributed)" row — they are NOT
#     re-assigned by heuristic. Use `git notes` (RACE-R retro tag step)
#     to backfill historic commits.
#   - LOC counts use `git log --shortstat` (additions+deletions).
#   - "tests" = files matching `^(test|tests)/` or `_test\.|test_.*\.py`.

set -uo pipefail

RANGE="--all"
NODE_FILTER=""
OUTPUT_JSON=0

while [ $# -gt 0 ]; do
    case "$1" in
        --range) RANGE="$2"; shift 2 ;;
        --node)  NODE_FILTER="$2"; shift 2 ;;
        --json)  OUTPUT_JSON=1; shift ;;
        -h|--help)
            sed -n '2,25p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO_ROOT" ] || { echo "not a git repo" >&2; exit 1; }
cd "$REPO_ROOT"

# Python does the parsing — bash is too painful for multi-stat aggregation.
PY="python3"
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
    PY="$REPO_ROOT/.venv/bin/python3"
fi

# We feed `git log --shortstat --pretty=...` to Python via stdin. The
# pretty marker `__COMMIT__<sha>` lets us split records reliably.
GIT_LOG_FMT='__COMMIT__%H%n%B%n__ENDBODY__'

# Write the Python parser to a tempfile so we can both pipe git log
# into stdin AND pass NODE_FILTER / OUTPUT_JSON as argv. (Heredoc
# stdin would clobber the pipe.)
PY_SCRIPT="$(mktemp -t fleet-attr.XXXXXX.py)"
trap 'rm -f "$PY_SCRIPT"' EXIT
cat > "$PY_SCRIPT" <<'PYEOF'
import json
import os
import re
import sys
from collections import defaultdict

node_filter = sys.argv[1] if len(sys.argv) > 1 else ""
as_json = sys.argv[2] == "1" if len(sys.argv) > 2 else False

TRAILER_RE = re.compile(
    r'^Co-Authored-By:\s*([a-z0-9_-]+)-atlas\s*<[^>]+>\s*$',
    re.MULTILINE,
)
# numstat lines look like: "<added>\t<removed>\t<path>"
# Binary files show "-\t-\t<path>".
NUMSTAT_RE = re.compile(r'^(\d+|-)\t(\d+|-)\t(.+)$')
TEST_FILE_RE = re.compile(r'(^|/)(tests?/|test_[^/]+\.py$|[^/]+_test\.(py|sh|js|ts|go)$)')

stats = defaultdict(lambda: {"commits": 0, "added": 0, "removed": 0, "tests": 0, "shas": []})

raw = sys.stdin.read()
records = raw.split("__COMMIT__")
for rec in records:
    if not rec.strip():
        continue
    # rec begins with: <sha>\n<body...>__ENDBODY__\n<shortstat lines + filenames>
    sha, _, rest = rec.partition("\n")
    sha = sha.strip()
    if not sha:
        continue
    body, _, tail = rest.partition("__ENDBODY__")

    m = TRAILER_RE.search(body)
    node = m.group(1) if m else "(unattributed)"

    if node_filter and node != node_filter:
        continue

    added = removed = 0
    test_touched = False
    for line in tail.splitlines():
        if not line.strip():
            continue
        m2 = NUMSTAT_RE.match(line)
        if m2:
            a, r, path = m2.group(1), m2.group(2), m2.group(3)
            if a.isdigit():
                added += int(a)
            if r.isdigit():
                removed += int(r)
            if TEST_FILE_RE.search(path):
                test_touched = True

    s = stats[node]
    s["commits"] += 1
    s["added"] += added
    s["removed"] += removed
    if test_touched:
        s["tests"] += 1
    if len(s["shas"]) < 5:
        s["shas"].append(sha[:10])

if as_json:
    print(json.dumps(stats, indent=2, sort_keys=True))
    sys.exit(0)

if not stats:
    print("no commits matched")
    sys.exit(0)

print(f"{'node':<18} {'commits':>8} {'added':>10} {'removed':>10} {'tests':>7}")
print("-" * 60)
for node in sorted(stats):
    s = stats[node]
    print(f"{node:<18} {s['commits']:>8} {s['added']:>10} {s['removed']:>10} {s['tests']:>7}")

print()
print("Sample SHAs (first 5 per node):")
for node in sorted(stats):
    print(f"  {node:<18} {' '.join(stats[node]['shas'])}")
PYEOF

# shellcheck disable=SC2086
git log $RANGE --numstat --pretty=format:"$GIT_LOG_FMT" \
    | "$PY" "$PY_SCRIPT" "$NODE_FILTER" "$OUTPUT_JSON"
