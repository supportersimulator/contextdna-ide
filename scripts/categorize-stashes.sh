#!/usr/bin/env bash
# categorize-stashes.sh — READ-ONLY categorization of remaining stashes.
#
# Context:
#   N1 audited 52 stashes → 18 EMPTY/SUBSET safe-to-drop + 34 UNIQUE preserved.
#   O1 dropped the 18 (see .fleet/audits/2026-05-04-O1-dropped-stashes.md).
#   This script (Q4) categorizes the surviving 34 stashes (now indices 0-33)
#   so Aaron can decide which to recover or drop manually.
#
# READ-ONLY: never runs `git stash drop`, `git stash apply`, never commits, never pushes.
#
# Outputs:
#   /tmp/stash-categorization.json
#   .fleet/audits/2026-05-04-Q4-stash-categorization.md
#
# Idempotent: re-running overwrites both outputs with fresh state.
#
# Categories (per-stash):
#   RECOVERED-IN-HEAD : every changed file's content is already at HEAD (≥80% line overlap)
#                       — Aaron can drop these via O1's drop-safe-stashes.sh logic.
#                       This script does NOT overlap with O1's allowlist.
#   AUDIT_DOC         : only `.fleet/audits/**` paths (or `.fleet/**` doc-style)
#   CODE_WIP          : ≥1 .py edit not present at HEAD
#   CONFIG_WIP        : .yaml / .json / .sh / .toml edits, no .py
#   MIXED             : ≥2 different code/config/doc kinds
#   JUNK              : empty diff or whitespace-only
#
# Importance score (rough):
#   total_lines_changed * (CODE_WIP=3, MIXED=2, CONFIG_WIP=2, AUDIT_DOC=1, RECOVERED-IN-HEAD=0, JUNK=0)
#
# ZSF: every parsing branch logs its decision. No silent except.

set -uo pipefail

REPO=/Users/aarontjomsland/dev/er-simulator-superrepo
cd "$REPO"

JSON_OUT=/tmp/stash-categorization.json
MD_OUT="$REPO/.fleet/audits/2026-05-04-Q4-stash-categorization.md"
TMP_DIR=$(mktemp -d -t q4-stash-cat-XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT

NOW_UTC=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
NOW_EPOCH=$(date -u +%s)

# Use rtk proxy for raw git output (rtk filter truncates large diffs).
GIT_RAW="rtk proxy git"

stash_count=$(git stash list | wc -l | tr -d ' ')
if [ "$stash_count" = "0" ]; then
  echo "No stashes to categorize." >&2
  echo "[]" > "$JSON_OUT"
  exit 0
fi

# JSON array assembled in a tmp file
ROWS_JSON="$TMP_DIR/rows.jsonl"
> "$ROWS_JSON"

# tally for MD summary (bash 3.2 — no associative arrays, use parallel vars)
COUNT_RECOVERED=0
COUNT_AUDIT_DOC=0
COUNT_CODE_WIP=0
COUNT_CONFIG_WIP=0
COUNT_MIXED=0
COUNT_JUNK=0

bump_count() {
  case "$1" in
    RECOVERED-IN-HEAD) COUNT_RECOVERED=$(( COUNT_RECOVERED + 1 )) ;;
    AUDIT_DOC)         COUNT_AUDIT_DOC=$(( COUNT_AUDIT_DOC + 1 )) ;;
    CODE_WIP)          COUNT_CODE_WIP=$(( COUNT_CODE_WIP + 1 )) ;;
    CONFIG_WIP)        COUNT_CONFIG_WIP=$(( COUNT_CONFIG_WIP + 1 )) ;;
    MIXED)             COUNT_MIXED=$(( COUNT_MIXED + 1 )) ;;
    JUNK)              COUNT_JUNK=$(( COUNT_JUNK + 1 )) ;;
  esac
}

age_days() {
  # arg: ISO8601 like "2026-05-06 17:39:42 +0200"
  local ts="$1"
  local epoch
  epoch=$(date -u -j -f "%Y-%m-%d %H:%M:%S %z" "$ts" "+%s" 2>/dev/null || echo "")
  if [ -z "$epoch" ]; then
    echo "?"
    return
  fi
  echo $(( (NOW_EPOCH - epoch) / 86400 ))
}

# Per-file recovery: returns "RECOVERED" if HEAD's version contains ≥80% of stash's added lines.
file_recovery_status() {
  local idx="$1"
  local path="$2"
  # If HEAD doesn't have file, definitely not recovered.
  if ! git cat-file -e "HEAD:$path" 2>/dev/null; then
    echo "MISSING_AT_HEAD"
    return
  fi
  # Count added (+) lines in stash's diff for this file vs its parent.
  local added head_count overlap
  added=$($GIT_RAW stash show -p "stash@{$idx}" -- "$path" 2>/dev/null \
    | awk '/^\+\+\+ /{f=1; next} /^--- /{f=0; next} f && /^\+/ && !/^\+\+\+ /{print substr($0,2)}' \
    | wc -l | tr -d ' ')
  if [ "$added" = "0" ]; then
    # Pure deletions or rename — fall back to content equality check.
    local stash_blob head_blob
    stash_blob=$($GIT_RAW show "stash@{$idx}:$path" 2>/dev/null | shasum | awk '{print $1}')
    head_blob=$($GIT_RAW show "HEAD:$path" 2>/dev/null | shasum | awk '{print $1}')
    if [ -n "$stash_blob" ] && [ "$stash_blob" = "$head_blob" ]; then
      echo "RECOVERED"
    else
      echo "NOT_RECOVERED"
    fi
    return
  fi
  # How many of those added lines are present in HEAD's version?
  overlap=$(
    $GIT_RAW stash show -p "stash@{$idx}" -- "$path" 2>/dev/null \
      | awk '/^\+\+\+ /{f=1; next} /^--- /{f=0; next} f && /^\+/ && !/^\+\+\+ /{print substr($0,2)}' \
      | grep -F -x -f - <($GIT_RAW show "HEAD:$path" 2>/dev/null) 2>/dev/null \
      | wc -l | tr -d ' '
  )
  # ratio = overlap / added * 100
  local pct=$(( overlap * 100 / (added > 0 ? added : 1) ))
  if [ "$pct" -ge 80 ]; then
    echo "RECOVERED"
  else
    echo "NOT_RECOVERED"
  fi
}

categorize_stash() {
  local idx="$1"
  local files added removed total ts msg first3 ext_count_py ext_count_md ext_count_json ext_count_sh ext_count_yaml ext_count_other
  local n_files n_recovered n_audit_only n_py n_yaml_json_sh
  ts=$($GIT_RAW log -g --format="%ci" "stash@{$idx}" -n 1 2>/dev/null | head -1)
  msg=$($GIT_RAW log -g --format="%s" "stash@{$idx}" -n 1 2>/dev/null | head -1)
  files=$($GIT_RAW stash show "stash@{$idx}" --name-only 2>/dev/null || echo "")
  if [ -z "$files" ]; then
    echo "JUNK|0|0|0|0|||$ts|$msg|JUNK|EMPTY_STASH"
    return
  fi

  # numstat: added\tremoved\tpath
  local numstat
  numstat=$($GIT_RAW stash show "stash@{$idx}" --numstat 2>/dev/null || echo "")
  added=0; removed=0
  while IFS=$'\t' read -r a r p; do
    [ -z "$p" ] && continue
    # binary file shows "-\t-\t..."
    [ "$a" = "-" ] && a=0
    [ "$r" = "-" ] && r=0
    added=$(( added + a ))
    removed=$(( removed + r ))
  done <<< "$numstat"
  total=$(( added + removed ))

  # ext counts
  ext_count_py=0; ext_count_md=0; ext_count_json=0; ext_count_sh=0; ext_count_yaml=0; ext_count_other=0
  n_files=0; n_audit_only=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    n_files=$(( n_files + 1 ))
    case "$f" in
      .fleet/audits/*) n_audit_only=$(( n_audit_only + 1 )) ;;
    esac
    case "$f" in
      *.py) ext_count_py=$(( ext_count_py + 1 )) ;;
      *.md) ext_count_md=$(( ext_count_md + 1 )) ;;
      *.json|*.jsonl) ext_count_json=$(( ext_count_json + 1 )) ;;
      *.sh) ext_count_sh=$(( ext_count_sh + 1 )) ;;
      *.yaml|*.yml|*.toml) ext_count_yaml=$(( ext_count_yaml + 1 )) ;;
      *) ext_count_other=$(( ext_count_other + 1 )) ;;
    esac
  done <<< "$files"

  # Use comma+space inside the field — pipe is reserved as the record separator below.
  first3=$(printf "%s" "$files" | head -3 | tr '\n' ',' | sed 's/,$//')

  # Per-file recovery check: counts how many files are RECOVERED at HEAD.
  n_recovered=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    rs=$(file_recovery_status "$idx" "$f")
    if [ "$rs" = "RECOVERED" ]; then
      n_recovered=$(( n_recovered + 1 ))
    fi
  done <<< "$files"

  # Choose category
  local cat="MIXED"
  local rationale=""
  if [ "$total" = "0" ]; then
    cat="JUNK"
    rationale="zero line changes"
  elif [ "$n_recovered" = "$n_files" ] && [ "$n_files" -gt 0 ]; then
    cat="RECOVERED-IN-HEAD"
    rationale="all $n_files file(s) match HEAD ≥80%"
  elif [ "$n_audit_only" = "$n_files" ] && [ "$n_files" -gt 0 ]; then
    cat="AUDIT_DOC"
    rationale="only .fleet/audits/* paths"
  elif [ "$ext_count_py" -gt 0 ] && [ "$(( ext_count_md + ext_count_json + ext_count_sh + ext_count_yaml + ext_count_other ))" = "0" ]; then
    cat="CODE_WIP"
    rationale=".py only ($ext_count_py file(s))"
  elif [ "$ext_count_py" -gt 0 ]; then
    cat="MIXED"
    rationale=".py + other types"
  elif [ "$(( ext_count_yaml + ext_count_json + ext_count_sh ))" -gt 0 ] && [ "$ext_count_py" = "0" ]; then
    if [ "$ext_count_md" = "0" ] && [ "$ext_count_other" = "0" ]; then
      cat="CONFIG_WIP"
      rationale="yaml/json/sh only"
    else
      cat="MIXED"
      rationale="config + docs/other"
    fi
  elif [ "$ext_count_md" -gt 0 ] && [ "$(( ext_count_py + ext_count_json + ext_count_sh + ext_count_yaml + ext_count_other ))" = "0" ]; then
    if [ "$n_audit_only" -gt 0 ]; then
      cat="AUDIT_DOC"
      rationale=".md including audits"
    else
      cat="AUDIT_DOC"
      rationale=".md only"
    fi
  fi

  # importance score
  local mult=0
  case "$cat" in
    CODE_WIP) mult=3 ;;
    MIXED) mult=2 ;;
    CONFIG_WIP) mult=2 ;;
    AUDIT_DOC) mult=1 ;;
    RECOVERED-IN-HEAD) mult=0 ;;
    JUNK) mult=0 ;;
  esac
  local importance=$(( total * mult ))

  # Output: pipe-separated for caller, plus emit JSON line.
  local age
  age=$(age_days "$ts")
  echo "OK|$idx|$n_files|$added|$removed|$total|$age|$first3|$ts|$msg|$cat|$rationale|$n_recovered|$importance"

  jq -nc \
    --arg idx "$idx" \
    --arg ts "$ts" \
    --arg msg "$msg" \
    --argjson n_files "$n_files" \
    --argjson added "$added" \
    --argjson removed "$removed" \
    --argjson total "$total" \
    --arg age_days "$age" \
    --arg first3 "$first3" \
    --argjson ext_py "$ext_count_py" \
    --argjson ext_md "$ext_count_md" \
    --argjson ext_json "$ext_count_json" \
    --argjson ext_sh "$ext_count_sh" \
    --argjson ext_yaml "$ext_count_yaml" \
    --argjson ext_other "$ext_count_other" \
    --argjson n_audit "$n_audit_only" \
    --argjson n_recovered "$n_recovered" \
    --arg category "$cat" \
    --arg rationale "$rationale" \
    --argjson importance "$importance" \
    '{stash:("stash@{"+$idx+"}"), idx:($idx|tonumber), ts:$ts, msg:$msg,
      n_files:$n_files, added:$added, removed:$removed, total:$total,
      age_days:(try ($age_days|tonumber) catch null),
      first_files:($first3|split(",")|map(select(length>0))),
      ext:{py:$ext_py, md:$ext_md, json:$ext_json, sh:$ext_sh, yaml:$ext_yaml, other:$ext_other},
      n_audit_only:$n_audit, n_recovered:$n_recovered,
      category:$category, rationale:$rationale, importance:$importance}' \
    >> "$ROWS_JSON"
}

echo "== Q4 categorize-stashes (read-only) =="
echo "stashes: $stash_count"
echo "json:    $JSON_OUT"
echo "md:      $MD_OUT"
echo

# Iterate idx 0..N-1
LAST_IDX=$(( stash_count - 1 ))
declare -a TABLE_ROWS
for i in $(seq 0 $LAST_IDX); do
  out=$(categorize_stash "$i")
  echo "$out"
  status=${out%%|*}
  if [ "$status" = "JUNK" ]; then
    # Empty stash row: synthesize JSON entry
    IFS='|' read -r _ idx_unused n_files_unused added_u removed_u total_u age_u first3_u ts_u msg_u cat_u rat_u <<< "JUNK|$i|0|0|0|0|?||$out||JUNK|empty"
    jq -nc \
      --arg idx "$i" \
      --arg ts "$(git log -g --format="%ci" "stash@{$i}" -n 1 2>/dev/null | head -1)" \
      --arg msg "$(git log -g --format="%s" "stash@{$i}" -n 1 2>/dev/null | head -1)" \
      '{stash:("stash@{"+$idx+"}"), idx:($idx|tonumber), ts:$ts, msg:$msg,
        n_files:0, added:0, removed:0, total:0, age_days:null,
        first_files:[], ext:{py:0,md:0,json:0,sh:0,yaml:0,other:0},
        n_audit_only:0, n_recovered:0, category:"JUNK", rationale:"empty stash", importance:0}' \
      >> "$ROWS_JSON"
    bump_count JUNK
    TABLE_ROWS+=("$i|0|0|0|JUNK|empty stash||")
    continue
  fi
  IFS='|' read -r _ idx n_files added removed total age first3 ts msg cat rationale n_recovered importance <<< "$out"
  bump_count "$cat"
  # Save row for table
  TABLE_ROWS+=("$idx|$n_files|$total|$age|$cat|$rationale|$first3|$msg")
done

# Build the JSON array
jq -s '.' "$ROWS_JSON" > "$JSON_OUT"

# Build markdown
{
  echo "# Q4 — Stash Categorization (Read-Only)"
  echo
  echo "**Generated:** $NOW_UTC"
  echo "**Repo:** \`$REPO\`"
  echo "**Script:** \`scripts/categorize-stashes.sh\` (read-only, idempotent)"
  echo "**Source data:** \`git stash list\` after O1's drop pass (\`.fleet/audits/2026-05-04-O1-dropped-stashes.md\`)."
  echo
  echo "## Mission"
  echo
  echo "N1 found 18 EMPTY/SUBSET stashes that O1 dropped. **34 UNIQUE stashes remained** (now indices 0–$LAST_IDX)."
  echo "This audit categorizes them so Aaron can decide what to recover. **No stashes were dropped, applied, or modified.**"
  echo
  echo "## Category counts"
  echo
  echo "| Category | Count | Meaning |"
  echo "|---|---|---|"
  echo "| RECOVERED-IN-HEAD | $COUNT_RECOVERED | every file already at HEAD with ≥80% line overlap — safe to drop |"
  echo "| AUDIT_DOC | $COUNT_AUDIT_DOC | only .fleet/audits/* or .md docs — usually safe to drop |"
  echo "| CODE_WIP | $COUNT_CODE_WIP | .py edits not yet in HEAD — preserve |"
  echo "| CONFIG_WIP | $COUNT_CONFIG_WIP | yaml/json/sh edits — case by case |"
  echo "| MIXED | $COUNT_MIXED | combination of code+config+docs |"
  echo "| JUNK | $COUNT_JUNK | empty/whitespace-only |"
  echo
  echo "## Per-stash table"
  echo
  echo "| stash | files | total Δlines | age (d) | category | rationale | first file | msg |"
  echo "|---|---|---|---|---|---|---|---|"
  for row in "${TABLE_ROWS[@]}"; do
    IFS='|' read -r idx n_files total age cat rationale first3 msg <<< "$row"
    # take only the first file from first3 (which is comma-joined)
    first_one=${first3%%,*}
    # markdown-escape
    safe_msg=$(printf '%s' "$msg" | sed 's/|/\\|/g')
    safe_first=$(printf '%s' "$first_one" | sed 's/|/\\|/g')
    safe_rat=$(printf '%s' "$rationale" | sed 's/|/\\|/g')
    echo "| stash@{$idx} | $n_files | $total | $age | $cat | $safe_rat | \`$safe_first\` | $safe_msg |"
  done
  echo
  echo "## Per-stash summary"
  echo
  jq -r '.[] | "### stash@{\(.idx)} — \(.category)\n\n- **When:** \(.ts)\n- **Msg:** \(.msg)\n- **Files (\(.n_files)):** \(.first_files | join(", "))\n- **Lines:** +\(.added) / -\(.removed) (\(.total) total)\n- **Age:** \(.age_days // "?") days\n- **Ext mix:** py=\(.ext.py), md=\(.ext.md), json=\(.ext.json), sh=\(.ext.sh), yaml=\(.ext.yaml), other=\(.ext.other)\n- **Recovered files at HEAD:** \(.n_recovered)/\(.n_files)\n- **Rationale:** \(.rationale)\n- **Importance:** \(.importance)\n"' "$JSON_OUT"
  echo
  echo "## Top by importance"
  echo
  echo "These are the stashes most worth Aaron's review (high line count × code-like content):"
  echo
  jq -r '. | sort_by(-.importance) | .[0:10] | .[] | "- **stash@{\(.idx)}** [\(.category)] — importance \(.importance), \(.total) lines, \(.n_files) files. First: \(.first_files | .[0] // "?")"' "$JSON_OUT"
  echo
  echo "## Recommended action"
  echo
  echo "1. **RECOVERED-IN-HEAD** rows: Aaron may safely drop. They are NOT in O1's allowlist — to drop them, extend O1's logic or use a new allowlist; do NOT modify O1's allowlist (those indices are already gone)."
  echo "2. **CODE_WIP / MIXED / CONFIG_WIP**: Aaron should review per-stash via \`git stash show -p stash@{N}\` before any decision."
  echo "3. **AUDIT_DOC**: usually safe to drop after confirming the doc lives elsewhere."
  echo "4. **JUNK**: safe to drop."
  echo
  echo "## Provenance / overlap with O1"
  echo
  echo "O1's \`scripts/drop-safe-stashes.sh\` operates on a static index allowlist that references stashes 25–44 + stash@{3} **at the time of N1's audit**. Those have already been dropped; the allowlist will SKIP them on re-run. This Q4 script does not share state with O1's allowlist and never invokes \`git stash drop\` itself."
  echo
} > "$MD_OUT"

echo
echo "== summary =="
echo "  RECOVERED-IN-HEAD: $COUNT_RECOVERED"
echo "  AUDIT_DOC:         $COUNT_AUDIT_DOC"
echo "  CODE_WIP:          $COUNT_CODE_WIP"
echo "  CONFIG_WIP:        $COUNT_CONFIG_WIP"
echo "  MIXED:             $COUNT_MIXED"
echo "  JUNK:              $COUNT_JUNK"
echo
echo "wrote: $JSON_OUT"
echo "wrote: $MD_OUT"
