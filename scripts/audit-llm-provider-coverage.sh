#!/usr/bin/env bash
# audit-llm-provider-coverage.sh — YY5 (Part A)
#
# Greps every external LLM call site under scripts/, memory/, 3-surgeons/,
# and tools/ and reports its provider hierarchy so Aaron can spot any path
# that hard-codes Anthropic/OpenAI without DeepSeek fallback.
#
# Output format (TSV):  call_site<TAB>providers<TAB>primary<TAB>has_deepseek
#   call_site     — file:line of the match
#   providers     — short list of providers referenced in that file
#   primary       — best guess at the primary provider (first in chain)
#   has_deepseek  — yes/no — does this file mention deepseek at all?
#
# ZSF: every grep failure is captured in /tmp/llm-provider-audit.err so
# this script never silently drops a hit. Counter incremented in stderr
# tail.
#
# Usage:
#   bash scripts/audit-llm-provider-coverage.sh           # all roots
#   bash scripts/audit-llm-provider-coverage.sh --json    # JSON per row
#   bash scripts/audit-llm-provider-coverage.sh --summary # totals only

set -u
set -o pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ERR_LOG="/tmp/llm-provider-audit.err"
: > "$ERR_LOG"

MODE="table"
case "${1:-}" in
  --json)    MODE="json" ;;
  --summary) MODE="summary" ;;
  --help|-h) sed -n '2,24p' "$0"; exit 0 ;;
esac

# Scan roots — keep the list short so the audit stays fast.
ROOTS=(
  "$REPO_DIR/scripts"
  "$REPO_DIR/memory"
  "$REPO_DIR/3-surgeons"
  "$REPO_DIR/tools"
)

# Exclusion globs — worktrees, venv, caches must never pollute the audit.
EXCLUDES=(
  --exclude-dir=.venv
  --exclude-dir=venv
  --exclude-dir=venv.nosync
  --exclude-dir=worktrees
  --exclude-dir=.git
  --exclude-dir=__pycache__
  --exclude-dir=node_modules
  --exclude-dir=build
  --exclude-dir=dist
  --exclude-dir=.fleet
)

# Pattern: any reference to a cloud LLM endpoint or SDK class.
PATTERN='(api\.anthropic\.com|api\.openai\.com|api\.deepseek\.com|anthropic\.Anthropic|openai\.OpenAI|claude-[0-9]|gpt-[0-9]|deepseek-(chat|reasoner)|ANTHROPIC_API_KEY|OPENAI_API_KEY|DEEPSEEK_API_KEY)'

# macOS default /bin/bash is 3.2 — no associative arrays, fragile with
# `set -u` on empty index reads. Work around both: use a tmpfile-backed
# dedup set + initialize arrays explicitly.
HITS_FILE=$(mktemp); : > "$HITS_FILE"
for root in "${ROOTS[@]}"; do
  [[ -d "$root" ]] || { echo "skip-root: $root" >> "$ERR_LOG"; continue; }
  grep -rEn "${EXCLUDES[@]}" --include='*.sh' --include='*.py' --include='*.yaml' --include='*.yml' \
       "$PATTERN" "$root" 2>>"$ERR_LOG" >> "$HITS_FILE" || true
done

# Dedup by file (one row per file, not per match line).
SEEN_FILE=$(mktemp); : > "$SEEN_FILE"
ROWS=()
while IFS= read -r h; do
  [[ -z "$h" ]] && continue
  file="${h%%:*}"
  rest="${h#*:}"
  lineno="${rest%%:*}"
  grep -qxF "$file" "$SEEN_FILE" 2>/dev/null && continue
  echo "$file" >> "$SEEN_FILE"

  # grep -c can emit multi-line counts when a file matches both stdout
  # and stderr quirks — coerce to a single integer.
  has_anthropic=$(grep -cE 'anthropic|claude-[0-9]|ANTHROPIC_API_KEY' "$file" 2>/dev/null | head -1 | tr -dc '0-9')
  has_openai=$(grep -cE 'api\.openai|openai\.OpenAI|gpt-[0-9]|OPENAI_API_KEY' "$file" 2>/dev/null | head -1 | tr -dc '0-9')
  has_deepseek=$(grep -cE 'deepseek|DEEPSEEK_API_KEY' "$file" 2>/dev/null | head -1 | tr -dc '0-9')
  has_anthropic="${has_anthropic:-0}"
  has_openai="${has_openai:-0}"
  has_deepseek="${has_deepseek:-0}"

  providers=""
  primary="unknown"
  # Heuristic: scan top 80 lines for the first provider mentioned — that
  # is usually the "primary" in a fallback chain.
  first=$(grep -nE 'anthropic|openai|deepseek' "$file" 2>/dev/null | head -1 | tr '[:upper:]' '[:lower:]')
  case "$first" in
    *deepseek*)  primary="deepseek" ;;
    *anthropic*) primary="anthropic" ;;
    *openai*)    primary="openai" ;;
  esac
  [[ $has_deepseek  -gt 0 ]] && providers="${providers}deepseek,"
  [[ $has_anthropic -gt 0 ]] && providers="${providers}anthropic,"
  [[ $has_openai    -gt 0 ]] && providers="${providers}openai,"
  providers="${providers%,}"
  ds_flag="no"; [[ $has_deepseek -gt 0 ]] && ds_flag="yes"

  ROWS+=("$file:$lineno|$providers|$primary|$ds_flag")
done < "$HITS_FILE"
rm -f "$HITS_FILE" "$SEEN_FILE"

# Counters — guard against empty-array under `set -u`.
total=${#ROWS[@]}
ds_yes=0; ds_no=0; ant_primary=0; oai_primary=0; ds_primary=0
for r in "${ROWS[@]:-}"; do
  [[ -z "$r" ]] && continue
  IFS='|' read -r _ _ primary ds <<<"$r"
  [[ "$ds" == "yes" ]] && ds_yes=$((ds_yes+1)) || ds_no=$((ds_no+1))
  case "$primary" in
    anthropic) ant_primary=$((ant_primary+1)) ;;
    openai)    oai_primary=$((oai_primary+1)) ;;
    deepseek)  ds_primary=$((ds_primary+1)) ;;
  esac
done

emit_row() {
  IFS='|' read -r site providers primary ds <<<"$1"
  case "$MODE" in
    json) printf '{"site":"%s","providers":"%s","primary":"%s","has_deepseek":"%s"}\n' \
              "$site" "$providers" "$primary" "$ds" ;;
    *)    printf '%-70s\t%-26s\t%-9s\t%s\n' "$site" "$providers" "$primary" "$ds" ;;
  esac
}

if [[ "$MODE" != "summary" ]]; then
  [[ "$MODE" == "table" ]] && printf '%-70s\t%-26s\t%-9s\t%s\n' "call_site" "providers" "primary" "has_deepseek"
  for r in "${ROWS[@]:-}"; do
    [[ -z "$r" ]] && continue
    emit_row "$r"
  done
fi

# Summary always goes to stderr so JSON consumers can ignore it.
{
  echo ""
  echo "── LLM PROVIDER COVERAGE SUMMARY ──"
  echo "total_call_sites:        $total"
  echo "with_deepseek:           $ds_yes"
  echo "without_deepseek:        $ds_no"
  echo "primary_anthropic:       $ant_primary"
  echo "primary_openai:          $oai_primary"
  echo "primary_deepseek:        $ds_primary"
  err_count=$(wc -l < "$ERR_LOG" | tr -d ' ')
  echo "grep_errors_recorded:    $err_count   (see $ERR_LOG)"
} >&2

exit 0
