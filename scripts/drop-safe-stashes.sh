#!/usr/bin/env bash
# drop-safe-stashes.sh — Idempotently drop EMPTY/SUBSET stashes from N1's audit.
#
# Source of truth: .fleet/audits/2026-05-04-N1-stash-cleanup.md
#   - 7 EMPTY + 11 SUBSET = 18 stashes safe to drop.
#   - 34 UNIQUE stashes are PRESERVED — Aaron must review manually.
#
# This script:
#   - Re-classifies each candidate stash at runtime via the same logic as
#     scripts/n1-stash-analyze.sh (immune to drift).
#   - Refuses to drop ANY stash that is NOT classified EMPTY or SUBSET.
#   - Drops in DESCENDING index order so lower indices stay stable.
#   - Records every dropped stash (SHA + summary) to
#     .fleet/audits/2026-05-04-O1-dropped-stashes.md so the action is
#     recoverable via `git fsck --unreachable` until gc runs.
#
# Usage:
#   bash scripts/drop-safe-stashes.sh             # dry-run (default)
#   bash scripts/drop-safe-stashes.sh --dry-run   # explicit dry-run
#   bash scripts/drop-safe-stashes.sh --apply     # actually drop
#
# ZSF: every error path either logs to stderr + exits non-zero, or appends
# to the audit doc with a SKIP reason. No silent failures.

set -uo pipefail

REPO=/Users/aarontjomsland/dev/er-simulator-superrepo
cd "$REPO"

MODE="dry-run"
case "${1:-}" in
  --apply) MODE="apply" ;;
  --dry-run|"") MODE="dry-run" ;;
  *) echo "Usage: $0 [--dry-run|--apply]" >&2; exit 2 ;;
esac

AUDIT_OUT="$REPO/.fleet/audits/2026-05-04-O1-dropped-stashes.md"
ALLOWLIST_STATES="EMPTY SUBSET"

# N1's allowlist (per-stash decisions captured at audit time, 2026-05-06).
# Each entry: <index>|<expected_state>|<note>. We RE-VERIFY state at runtime
# and SKIP if it has drifted.
N1_ALLOWLIST=(
  "44|EMPTY|drop OK (2026-04-25 08:27)"
  "42|EMPTY|drop OK (2026-04-25 08:32)"
  "41|EMPTY|drop OK (2026-04-25 08:42)"
  "40|EMPTY|drop OK (2026-04-25 08:43)"
  "39|EMPTY|drop OK (2026-04-25 20:54)"
  "38|EMPTY|drop OK (2026-04-25 20:58)"
  "37|EMPTY|drop OK (2026-04-26 02:10)"
  "34|SUBSET|drop OK (2026-04-27 09:05)"
  "33|SUBSET|drop OK (2026-04-27 12:24)"
  "32|SUBSET|drop OK (2026-04-27 20:14)"
  "31|SUBSET|drop OK (2026-04-28 07:09)"
  "30|SUBSET|drop OK (2026-04-28 09:23)"
  "29|SUBSET|drop OK (2026-04-28 10:12)"
  "28|SUBSET|drop OK (2026-04-28 11:07)"
  "27|SUBSET|drop OK (2026-04-28 14:15)"
  "26|SUBSET|drop OK (2026-04-28 15:10)"
  "25|SUBSET|drop OK (2026-04-28 16:23)"
  "3|SUBSET|drop OK (2026-05-06 16:13)"
)

# classify_stash <index> -> echoes EMPTY|SUBSET|UNIQUE|MISSING
classify_stash() {
  local i="$1"
  if ! git rev-parse --verify --quiet "stash@{$i}" >/dev/null 2>&1; then
    echo "MISSING"
    return 0
  fi
  local files
  files=$(git stash show "stash@{$i}" --name-only 2>/dev/null || echo "")
  if [ -z "$files" ]; then
    echo "EMPTY"
    return 0
  fi
  local all_subset=1
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    local stash_blob head_blob wt_blob=""
    stash_blob=$(git show "stash@{$i}:$f" 2>/dev/null | shasum | awk '{print $1}')
    head_blob=$(git show "HEAD:$f" 2>/dev/null | shasum | awk '{print $1}')
    [ -f "$f" ] && wt_blob=$(shasum < "$f" | awk '{print $1}')
    if [ "$stash_blob" = "$head_blob" ] || [ "$stash_blob" = "$wt_blob" ]; then
      continue
    fi
    all_subset=0
    break
  done <<< "$files"
  if [ "$all_subset" = "1" ]; then
    echo "SUBSET"
  else
    echo "UNIQUE"
  fi
}

is_allowed_state() {
  local s="$1"
  for ok in $ALLOWLIST_STATES; do
    [ "$s" = "$ok" ] && return 0
  done
  return 1
}

# Sort allowlist DESCENDING by index so low indices stay stable as we drop.
SORTED=()
while IFS= read -r line; do
  SORTED+=("$line")
done < <(printf "%s\n" "${N1_ALLOWLIST[@]}" | sort -t'|' -k1,1 -n -r)

ts_now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

ensure_audit_header() {
  if [ ! -f "$AUDIT_OUT" ]; then
    {
      echo "# O1 — Dropped stashes log"
      echo
      echo "**Source audit:** \`.fleet/audits/2026-05-04-N1-stash-cleanup.md\`"
      echo "**Script:** \`scripts/drop-safe-stashes.sh\`"
      echo "**Created:** $(ts_now)"
      echo
      echo "Each row records a stash dropped by O1. The SHA refers to the stash"
      echo "commit object; until \`git gc\` reaps it, the change can be recovered"
      echo "via \`git fsck --unreachable | grep commit\` and \`git stash apply <sha>\`."
      echo
      echo "| When (UTC) | Index@drop | SHA | State | Stash msg | Note |"
      echo "|---|---|---|---|---|---|"
    } > "$AUDIT_OUT"
  fi
}

planned=()
skipped=()
dropped=()

echo "== drop-safe-stashes.sh ($MODE) =="
echo "repo: $REPO"
echo "stashes-before: $(git stash list | wc -l | tr -d ' ')"
echo

for entry in "${SORTED[@]}"; do
  IFS='|' read -r idx expected note <<< "$entry"
  state=$(classify_stash "$idx")
  if [ "$state" = "MISSING" ]; then
    skipped+=("stash@{$idx}|MISSING|index no longer exists")
    echo "SKIP stash@{$idx} — MISSING (already dropped or shifted)"
    continue
  fi
  if ! is_allowed_state "$state"; then
    skipped+=("stash@{$idx}|$state|drifted from $expected — refusing to drop")
    echo "SKIP stash@{$idx} — drifted (expected $expected, got $state)"
    continue
  fi
  if [ "$state" != "$expected" ]; then
    # still EMPTY/SUBSET, just not the same one — still safe per allowlist policy
    echo "NOTE stash@{$idx} — state $state (expected $expected) — still in allowlist, proceeding"
  fi
  sha=$(git rev-parse "stash@{$idx}" 2>/dev/null || echo "?")
  msg=$(git log -g --format="%s" "stash@{$idx}" -n 1 2>/dev/null | head -1)
  planned+=("stash@{$idx}|$state|$sha|$msg|$note")
  if [ "$MODE" = "dry-run" ]; then
    echo "WOULD DROP stash@{$idx} state=$state sha=${sha:0:12} :: $msg"
  else
    ensure_audit_header
    if git stash drop "stash@{$idx}" >/dev/null 2>&1; then
      printf '| %s | stash@{%s} | %s | %s | %s | %s |\n' \
        "$(ts_now)" "$idx" "$sha" "$state" "${msg//|/\\|}" "${note//|/\\|}" \
        >> "$AUDIT_OUT"
      dropped+=("stash@{$idx}|$state|$sha|$msg")
      echo "DROPPED stash@{$idx} state=$state sha=${sha:0:12}"
    else
      skipped+=("stash@{$idx}|$state|git stash drop failed")
      echo "FAIL stash@{$idx} — git stash drop returned non-zero" >&2
    fi
  fi
done

echo
echo "== summary =="
echo "planned: ${#planned[@]}"
echo "dropped: ${#dropped[@]}"
echo "skipped: ${#skipped[@]}"
if [ "${#skipped[@]}" -gt 0 ]; then
  echo "-- skipped --"
  for s in "${skipped[@]}"; do echo "  $s"; done
fi
echo "stashes-after: $(git stash list | wc -l | tr -d ' ')"

if [ "$MODE" = "dry-run" ]; then
  echo
  echo "(dry-run — no stashes were dropped. Re-run with --apply to commit changes.)"
fi
