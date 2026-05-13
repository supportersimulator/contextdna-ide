#!/usr/bin/env bash
# test_cleanup_stale_worktrees.sh — Unit tests for cleanup-stale-worktrees.sh
#
# Runs in an isolated tmp git repo so it never touches the real superrepo.
#
# Tests:
#   1. Stale, clean, merged worktree → REMOVED
#   2. Worktree with uncommitted changes → SKIPPED (dirty)
#   3. Worktree on unmerged branch present on origin → SKIPPED (unmerged)
#   4. Recent worktree (mtime now) → SKIPPED (recent)
#   5. --dry-run prints WOULD but does not delete

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLEANUP_SCRIPT="$SCRIPT_DIR/../cleanup-stale-worktrees.sh"

if [[ ! -x "$CLEANUP_SCRIPT" ]]; then
    echo "FAIL: cleanup script not executable: $CLEANUP_SCRIPT"
    exit 1
fi

PASS=0
FAIL=0
TESTS_RUN=()

note_pass() { PASS=$((PASS + 1)); TESTS_RUN+=("PASS: $1"); echo "  PASS: $1"; }
note_fail() { FAIL=$((FAIL + 1)); TESTS_RUN+=("FAIL: $1"); echo "  FAIL: $1"; }

# ---- isolated test fixture ----
TMP_ROOT="$(mktemp -d -t cleanup-wt-test.XXXXXX)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# Build a fake "origin" bare repo and a "local" clone so we can simulate
# branches present/absent on origin.
ORIGIN="$TMP_ROOT/origin.git"
LOCAL="$TMP_ROOT/local"
git init --quiet --bare "$ORIGIN"

git init --quiet -b main "$LOCAL"
cd "$LOCAL"
git config user.email test@example.com
git config user.name test
git remote add origin "$ORIGIN"
echo seed > README.md
git add README.md
git commit --quiet -m seed
git push --quiet -u origin main

mkdir -p "$LOCAL/.claude/worktrees"

make_wt() {
    # make_wt <name> <branch> <push_to_origin yes|no> <extra_commit yes|no>
    local name="$1" branch="$2" push="$3" extra="$4"
    local wt_path="$LOCAL/.claude/worktrees/$name"
    git -C "$LOCAL" worktree add --quiet -b "$branch" "$wt_path" main
    if [[ "$extra" == "yes" ]]; then
        echo "work" > "$wt_path/work.txt"
        git -C "$wt_path" add work.txt
        git -C "$wt_path" commit --quiet -m "extra commit"
    fi
    if [[ "$push" == "yes" ]]; then
        git -C "$wt_path" push --quiet -u origin "$branch"
    fi
    git -C "$LOCAL" worktree lock "$wt_path" 2>/dev/null || true
    echo "$wt_path"
}

age_dir() {
    # age_dir <path> <days_old>
    local path="$1" days="$2"
    local ts
    ts=$(( $(date +%s) - days * 86400 ))
    # touch -t accepts CCYYMMDDhhmm.SS
    local stamp
    stamp=$(date -r "$ts" +"%Y%m%d%H%M.%S" 2>/dev/null || date -j -f %s "$ts" +"%Y%m%d%H%M.%S")
    find "$path" -not -path '*/.git/*' -exec touch -t "$stamp" {} + 2>/dev/null || true
}

# Test 1: stale, clean, merged
WT1="$(make_wt agent-aaaa1111stale1111 race/test-stale-merged no no)"
age_dir "$WT1" 30

# Test 2: stale + dirty
WT2="$(make_wt agent-aaaa2222dirty2222 race/test-dirty no no)"
echo dirt > "$WT2/dirty.txt"
age_dir "$WT2" 30

# Test 3: stale + unmerged + on origin
WT3="$(make_wt agent-aaaa3333unmerg333 race/test-unmerged yes yes)"
age_dir "$WT3" 30

# Test 4: recent + clean + merged (would be eligible if not for mtime)
WT4="$(make_wt agent-aaaa4444recent444 race/test-recent no no)"
# leave mtimes at "now"

# ---- Run dry-run on the fixture ----
echo "=== Dry-run pass ==="
DRY_OUT="$(REPO_ROOT="$LOCAL" AGE_DAYS=7 "$CLEANUP_SCRIPT" --dry-run 2>&1)"
echo "$DRY_OUT" | sed 's/^/    /'

if echo "$DRY_OUT" | grep -q "WOULD: git worktree remove --force.*agent-aaaa1111stale1111"; then
    note_pass "dry-run lists stale-merged for removal"
else
    note_fail "dry-run did NOT list stale-merged"
fi

if echo "$DRY_OUT" | grep -A2 "agent-aaaa2222dirty2222" | grep -q "SKIP (uncommitted"; then
    note_pass "dry-run skips dirty worktree"
else
    note_fail "dry-run did NOT skip dirty worktree"
fi

if echo "$DRY_OUT" | grep -A2 "agent-aaaa3333unmerg333" | grep -q "SKIP (unmerged"; then
    note_pass "dry-run skips unmerged worktree"
else
    note_fail "dry-run did NOT skip unmerged worktree"
fi

if echo "$DRY_OUT" | grep -A2 "agent-aaaa4444recent444" | grep -q "SKIP (recent"; then
    note_pass "dry-run skips recent worktree"
else
    note_fail "dry-run did NOT skip recent worktree"
fi

# Confirm dry-run preserved everything on disk
ALL_STILL_EXIST=true
for wt in "$WT1" "$WT2" "$WT3" "$WT4"; do
    [[ -d "$wt" ]] || { ALL_STILL_EXIST=false; break; }
done
if $ALL_STILL_EXIST; then
    note_pass "dry-run preserved all worktrees on disk"
else
    note_fail "dry-run deleted something it should not have"
fi

# ---- Run live ----
echo ""
echo "=== Live pass ==="
LIVE_OUT="$(REPO_ROOT="$LOCAL" AGE_DAYS=7 "$CLEANUP_SCRIPT" 2>&1)"
echo "$LIVE_OUT" | sed 's/^/    /'

if [[ ! -d "$WT1" ]]; then
    note_pass "live removed stale-merged worktree"
else
    note_fail "live did NOT remove stale-merged worktree"
fi

if [[ -d "$WT2" ]]; then
    note_pass "live preserved dirty worktree"
else
    note_fail "live wrongly removed dirty worktree"
fi

if [[ -d "$WT3" ]]; then
    note_pass "live preserved unmerged worktree"
else
    note_fail "live wrongly removed unmerged worktree"
fi

if [[ -d "$WT4" ]]; then
    note_pass "live preserved recent worktree"
else
    note_fail "live wrongly removed recent worktree"
fi

echo ""
echo "=== Results ==="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
