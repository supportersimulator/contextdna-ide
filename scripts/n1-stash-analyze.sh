#!/usr/bin/env bash
# Analyze each stash, determine if content is subset of HEAD/working-tree
set -uo pipefail
cd /Users/aarontjomsland/dev/er-simulator-superrepo

OUT=/tmp/n1-stash-analysis/summary.txt
DETAIL=/tmp/n1-stash-analysis/detail.txt
> "$OUT"
> "$DETAIL"

for i in $(seq 0 51); do
  files=$(git stash show "stash@{$i}" --name-only 2>/dev/null || echo "")
  s_state="UNKNOWN"
  unique_files=""
  if [ -z "$files" ]; then
    s_state="EMPTY"
  else
    all_subset=1
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      stash_blob=$(git show "stash@{$i}:$f" 2>/dev/null | shasum | awk '{print $1}')
      head_blob=$(git show "HEAD:$f" 2>/dev/null | shasum | awk '{print $1}')
      wt_blob=""
      [ -f "$f" ] && wt_blob=$(shasum < "$f" | awk '{print $1}')
      if [ "$stash_blob" = "$head_blob" ] || [ "$stash_blob" = "$wt_blob" ]; then
        continue
      fi
      all_subset=0
      unique_files="$unique_files $f"
    done <<< "$files"
    if [ "$all_subset" = "1" ]; then
      s_state="SUBSET"
    else
      s_state="UNIQUE"
    fi
  fi
  ts=$(git log -g --format="%ci" "stash@{$i}" -n 1 2>/dev/null | head -1)
  msg=$(git log -g --format="%s" "stash@{$i}" -n 1 2>/dev/null | head -1)
  echo "stash@{$i}|$s_state|$ts|$msg" >> "$OUT"
  if [ "$s_state" = "UNIQUE" ]; then
    echo "stash@{$i} UNIQUE files:$unique_files" >> "$DETAIL"
  fi
done

echo "=== summary ==="
cut -d'|' -f2 "$OUT" | sort | uniq -c
echo "=== unique stashes ==="
grep '|UNIQUE|' "$OUT" || echo "(none)"
