#!/usr/bin/env bash
# pre-commit-north-star.sh — RACE Y5
#
# Classifies the in-progress commit subject against the locked North Star
# priority vector (scripts/north_star.py) so Aaron sees, at write time,
# whether the work-being-committed advances one of the five priorities or
# is orphan/drift work.
#
# ADVISORY ONLY — never blocks a commit. Visibility, not enforcement.
# Drift gating lives in gains-gate.sh check #17 (RACE X3).
#
# Behaviour:
#   - Empty / no subject                  -> exit 0 (nothing to classify)
#   - Subject classifies into a priority  -> print one-line "north-star: ..."
#                                            and exit 0
#   - Subject is orphan (no match)        -> print WARN block; if interactive
#                                            tty available, prompt
#                                            "continue? [y/N]" — anything
#                                            other than y/Y still exits 0
#                                            (advisory). If non-tty (CI,
#                                            git GUI), just WARN and exit 0.
#
# Bypass: irrelevant — never blocks anyway. To silence WARN noise, fix the
# subject line to mention a priority slug/keyword.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || exit 0

EDITMSG="$REPO_ROOT/.git/COMMIT_EDITMSG"
NORTH_STAR="$REPO_ROOT/scripts/north_star.py"

# If north_star.py is missing (older checkout, partial install), exit silently.
[ -f "$NORTH_STAR" ] || exit 0

# Allow tests / non-git callers to override the message file.
if [ "${NORTH_STAR_COMMIT_MSG_FILE:-}" != "" ]; then
  EDITMSG="$NORTH_STAR_COMMIT_MSG_FILE"
fi

# No editmsg yet (extremely early hook, or empty repo) — skip silently.
[ -f "$EDITMSG" ] || exit 0

# Extract the subject = first non-comment, non-empty line.
SUBJECT=$(grep -v '^#' "$EDITMSG" 2>/dev/null | awk 'NF{print; exit}')

# Empty subject -> nothing to classify, skip.
if [ -z "${SUBJECT:-}" ]; then
  exit 0
fi

PY_BIN="python3"
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
  PY_BIN="$REPO_ROOT/.venv/bin/python3"
fi

# Run classifier in JSON mode. The classifier never crashes on input — it
# returns priority=null on no-match. Capture both rc and stdout.
JSON_OUT=$("$PY_BIN" "$NORTH_STAR" --json classify "$SUBJECT" 2>/dev/null || true)

if [ -z "$JSON_OUT" ]; then
  # Classifier failed unexpectedly — surface, don't block.
  echo "north-star: classifier unavailable (advisory check skipped)"
  exit 0
fi

# Pull the priority field out via a tiny python one-liner (no jq dep).
PRIORITY=$("$PY_BIN" -c '
import json, sys
try:
    data = json.loads(sys.stdin.read() or "{}")
    p = data.get("priority")
    print(p if p else "")
except Exception:
    print("")
' <<< "$JSON_OUT")

if [ -n "$PRIORITY" ]; then
  echo "north-star: $PRIORITY"
  exit 0
fi

# --- Orphan path -----------------------------------------------------------
echo
echo "WARN: north-star drift — commit subject did not match any priority."
echo "  subject: $SUBJECT"
echo "  vector : Multi-Fleet | 3-Surgeons | ContextDNA-IDE | Full-Local-Ops | ER-Simulator"
echo "  fix    : reword subject to reference a priority, or accept this is"
echo "           exploratory work outside the locked North Star sequence."

# Interactive prompt only if /dev/tty is usable. Pre-commit hooks do not get
# an attached tty by default; we explicitly try to open it. CI / GUI tools /
# non-tty callers will simply fall through to exit 0.
if [ -r /dev/tty ] && [ -w /dev/tty ] && [ "${NORTH_STAR_NON_INTERACTIVE:-}" != "1" ]; then
  printf "  continue? [y/N] " > /dev/tty
  read -r ANSWER < /dev/tty || ANSWER=""
  case "$ANSWER" in
    y|Y|yes|YES) ;;  # accept
    *)
      echo "  (advisory only — commit proceeds; rerun gains-gate to inspect drift)" > /dev/tty
      ;;
  esac
fi

# Always exit 0 — advisory, not gate.
exit 0
