#!/usr/bin/env bash
# ============================================================================
# folder-provenance.sh — R2 retroactive 4-folder pipeline audit CLI
# ============================================================================
# Modes:
#   --scan          (default) rebuild memory/folder_provenance.json from
#                   the filesystem + git rename history
#   --check         exit 1 if index is stale (newer doc mtime than last scan)
#   --show <path>   print provenance for a single repo-relative doc path
#
# ZSF: missing folders / git failures are recorded in counters, never crash.
# ============================================================================

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

MODE="scan"
SHOW_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scan)
            MODE="scan"
            shift
            ;;
        --check)
            MODE="check"
            shift
            ;;
        --show)
            MODE="show"
            shift
            SHOW_PATH="${1:-}"
            [[ -z "$SHOW_PATH" ]] && {
                echo "error: --show requires a path argument" >&2
                exit 2
            }
            shift
            ;;
        -h|--help)
            sed -n '2,15p' "$0"
            exit 0
            ;;
        *)
            echo "error: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

PY="${REPO_DIR}/.venv/bin/python3"
[[ -x "$PY" ]] || PY="$(command -v python3)"

case "$MODE" in
    scan)
        PYTHONPATH="$REPO_DIR" "$PY" - <<'PY'
import json, sys
from memory import folder_provenance as fp

payload = fp.update_provenance_index()
summary = {
    "last_scan_ts": payload.get("last_scan_ts"),
    "total_indexed": payload.get("total_indexed"),
    "level_counts": payload.get("level_counts"),
    "counter_snapshot": payload.get("counter_snapshot"),
    "index_path": str(fp.DEFAULT_INDEX_PATH),
}
json.dump(summary, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
PY
        ;;
    check)
        PYTHONPATH="$REPO_DIR" "$PY" - <<'PY'
import sys
from memory import folder_provenance as fp

if fp.is_index_current():
    print("OK: folder_provenance index is current")
    sys.exit(0)
print("STALE: rebuild needed — run scripts/folder-provenance.sh --scan", file=sys.stderr)
sys.exit(1)
PY
        ;;
    show)
        PYTHONPATH="$REPO_DIR" "$PY" - "$SHOW_PATH" <<'PY'
import json, sys
from memory import folder_provenance as fp

path = sys.argv[1]
index = fp.load_index()
docs = (index or {}).get("docs", {}) if isinstance(index, dict) else {}
entry = docs.get(path)
if entry is None:
    print(f"no provenance entry for {path!r}", file=sys.stderr)
    print("hint: rebuild with --scan; paths are repo-relative (docs/...)", file=sys.stderr)
    sys.exit(1)
json.dump(entry, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
PY
        ;;
esac
