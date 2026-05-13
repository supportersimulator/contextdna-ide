#!/usr/bin/env bash
# superset-memory-bundle.sh — WW11-E: memory snapshot bundle for Superset agent launches.
#
# Builds a compact memory bundle (≤2000 chars) combining:
#   - CLAUDE.md critical sections (PROACTIVE, ATLAS, WEBHOOK, ZERO SILENT, BUTLER, 3-SURGEON)
#   - Last 3 audit filenames from .fleet/audits/
#   - Recent git log --oneline -5
#   - memory/brain.py context "fleet status" output (capped at 800 chars)
#
# Output: /tmp/superset-memory-bundle.md + stdout
#
# ZSF: any section failure is skipped silently; bundle is always produced.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

REPO_ROOT="$(pwd)"
MAX_CHARS=2000
OUT_FILE="/tmp/superset-memory-bundle.md"

# ── helpers ──────────────────────────────────────────────────────────────────

_section() {
  # Extract header + next 5 lines for each matching keyword from CLAUDE.md
  local keyword="$1"
  local claude_md="${REPO_ROOT}/CLAUDE.md"
  [[ -f "$claude_md" ]] || return
  awk -v kw="$keyword" '
    /^#/ && index($0, kw) > 0 { found=1; count=0 }
    found { print; count++; if (count >= 6) { found=0 } }
  ' "$claude_md" 2>/dev/null || true
}

# ── build bundle parts ───────────────────────────────────────────────────────

BUNDLE=""

# 1. Header
BUNDLE+="=== ATLAS MEMORY SNAPSHOT BUNDLE ==="$'\n'
BUNDLE+="Built: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"$'\n\n'

# 2. CLAUDE.md critical sections
MD_PARTS=""
for keyword in "PROACTIVE EXECUTION" "ATLAS IDENTITY" "WEBHOOK" "ZERO SILENT" "BUTLER" "3-SURGEON"; do
  section_text="$(_section "$keyword")"
  if [[ -n "$section_text" ]]; then
    MD_PARTS+="$section_text"$'\n\n'
  fi
done

if [[ -n "$MD_PARTS" ]]; then
  BUNDLE+="--- CLAUDE.md (critical sections) ---"$'\n'
  BUNDLE+="$MD_PARTS"
fi

# 3. Recent audit filenames
AUDIT_DIR="${REPO_ROOT}/.fleet/audits"
if [[ -d "$AUDIT_DIR" ]]; then
  AUDIT_LIST=$(ls -t "$AUDIT_DIR" 2>/dev/null | grep -v '^archive' | head -3 | tr '\n' '  ' || true)
  if [[ -n "$AUDIT_LIST" ]]; then
    BUNDLE+="--- Recent Audits ---"$'\n'
    BUNDLE+="$AUDIT_LIST"$'\n\n'
  fi
fi

# 4. Git log
GIT_LOG=$(git -C "$REPO_ROOT" log --oneline -5 2>/dev/null || true)
if [[ -n "$GIT_LOG" ]]; then
  BUNDLE+="--- Recent Commits ---"$'\n'
  BUNDLE+="$GIT_LOG"$'\n\n'
fi

# 5. brain.py context (capped at 800 chars)
BRAIN_OUTPUT=""
BRAIN_PY="${REPO_ROOT}/memory/brain.py"
if [[ -f "$BRAIN_PY" ]]; then
  VENV_PY="${REPO_ROOT}/.venv/bin/python3"
  PYTHON="python3"
  [[ -x "$VENV_PY" ]] && PYTHON="$VENV_PY"
  BRAIN_OUTPUT=$(PYTHONPATH="$REPO_ROOT" "$PYTHON" "$BRAIN_PY" context "fleet status" 2>/dev/null | head -c 800 || true)
fi
if [[ -n "$BRAIN_OUTPUT" ]]; then
  BUNDLE+="--- Memory Blueprint (fleet status) ---"$'\n'
  BUNDLE+="$BRAIN_OUTPUT"$'\n\n'
fi

# 6. Footer
BUNDLE+="=== END BUNDLE ==="

# ── truncate to MAX_CHARS ─────────────────────────────────────────────────────

END_MARKER=$'\n=== END BUNDLE ==='
if [[ ${#BUNDLE} -gt $MAX_CHARS ]]; then
  BUNDLE="${BUNDLE:0:$((MAX_CHARS - ${#END_MARKER}))}${END_MARKER}"
fi

# ── write + print ─────────────────────────────────────────────────────────────

printf '%s\n' "$BUNDLE" > "$OUT_FILE"
printf '%s\n' "$BUNDLE"
