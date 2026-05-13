#!/usr/bin/env bash
# surgeon-disagreements.sh — surface the VALUE of 3-Surgeons (RACE V3).
#
# Reads ~/.3surgeons/evidence.db and prints the most-recent disagreements
# (consensus_score < 0.7) ranked by delta.  Use this when Aaron asks
# "what did the surgeons disagree about lately?" and the IDE theater
# panel is not yet open.
#
# Usage:
#   scripts/surgeon-disagreements.sh           # default: top 10, ~/.3surgeons/evidence.db
#   scripts/surgeon-disagreements.sh -n 5      # top 5
#   scripts/surgeon-disagreements.sh -d /tmp/evidence.db
#   scripts/surgeon-disagreements.sh --json    # JSON output for tooling

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MULTIFLEET_DIR="${REPO_ROOT}/multi-fleet"

LIMIT=10
DB_PATH=""
OUTPUT_JSON=0

while [ $# -gt 0 ]; do
    case "$1" in
        -n|--limit)
            LIMIT="$2"; shift 2 ;;
        -d|--db)
            DB_PATH="$2"; shift 2 ;;
        --json)
            OUTPUT_JSON=1; shift ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# //;s/^#//'
            exit 0 ;;
        *)
            echo "unknown option: $1" >&2
            exit 2 ;;
    esac
done

# Pick a python that has multifleet importable.  Prefer the project venv.
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "${PYTHON_BIN}" ]; then
    if [ -x "${REPO_ROOT}/.venv/bin/python3" ]; then
        PYTHON_BIN="${REPO_ROOT}/.venv/bin/python3"
    elif [ -x "${MULTIFLEET_DIR}/.venv/bin/python3" ]; then
        PYTHON_BIN="${MULTIFLEET_DIR}/.venv/bin/python3"
    else
        PYTHON_BIN="python3"
    fi
fi

export PYTHONPATH="${MULTIFLEET_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

SD_LIMIT="${LIMIT}" SD_DB_PATH="${DB_PATH}" SD_JSON="${OUTPUT_JSON}" \
"${PYTHON_BIN}" -c '
import json
import os
import sys
from multifleet import surgeon_disagreement as sd

limit = int(os.environ.get("SD_LIMIT", "10"))
db_path = os.environ.get("SD_DB_PATH") or None
output_json = os.environ.get("SD_JSON", "0") == "1"

items = sd.load_disagreements(db_path=db_path, limit=limit)
errors = sd.get_module_stats()

if output_json:
    payload = {
        "items": [d.to_dict() for d in items],
        "errors": errors,
        "limit": limit,
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
else:
    sys.stdout.write(sd.format_cli(items))
    if any(v > 0 for v in errors.values()):
        sys.stdout.write("\n[zsf] surgeon_disagreement counters: " + json.dumps(errors) + "\n")
'
