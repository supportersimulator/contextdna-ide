#!/usr/bin/env bash
# pre-commit-secrets-check.sh — block home-path + secret leaks before commit
#
# Install:
#   ln -sf ../../scripts/pre-commit-secrets-check.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# Or project-wide:
#   git config core.hooksPath scripts/git-hooks
#   (then symlink into scripts/git-hooks/pre-commit)
#
# Bypass (emergency only): git commit --no-verify

set -uo pipefail
# NOTE: do not use -e; grep returns 1 when no match, which is expected flow here.

STAGED=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$STAGED" ] && exit 0

FAIL=0
SKIP_PATTERN='(\.plist$|/launchd/|\.original\.md$|/file-history/|/\.claude/worktrees/|scripts/pre-commit-secrets-check\.sh$)'

check_pattern() {
    local label="$1"
    local pattern="$2"
    local hits
    hits=$(echo "$STAGED" | grep -vE "$SKIP_PATTERN" | while read -r f; do
        [ -f "$f" ] || continue
        git diff --cached "$f" | grep -E '^\+' | grep -vE '^\+\+\+' | grep -E "$pattern" && echo "  in: $f"
    done)
    if [ -n "$hits" ]; then
        echo "BLOCKED: $label detected in staged diff:"
        echo "$hits"
        echo
        FAIL=1
    fi
}

check_pattern "home-path leak (hardcoded /Users/<name>)" "/Users/aarontjomsland"
check_pattern "private IP leak (192\\.168\\.x.x)"      "192\\.168\\.[0-9]+\\.[0-9]+"
check_pattern "AWS access key"                          "AKIA[0-9A-Z]{16}"
# RACE-P: explicit OpenAI / DeepSeek sk-... shape (>=20 body chars).
check_pattern "OpenAI/DeepSeek key (sk-...)"            "sk-[a-zA-Z0-9]{20,}"
check_pattern "Anthropic key"                           "sk-ant-[A-Za-z0-9_-]{20,}"
# RACE-P: GitHub PAT — exactly ghp_ + 36 alphanum.
check_pattern "GitHub PAT (ghp_)"                       "ghp_[a-zA-Z0-9]{36}"
check_pattern "GitHub other token (gho_/ghu_/ghs_/ghr_)" "gh[osur]_[A-Za-z0-9]{36}"
check_pattern "Slack token"                             "xox[baprs]-[A-Za-z0-9-]{10,}"
check_pattern "Bearer literal"                          "Bearer [A-Za-z0-9_.=-]{20,}"
check_pattern "password= literal"                       "(password|passwd|pwd)[[:space:]]*=[[:space:]]*['\"][^'\"]{4,}"
# RACE-P: Discord-flavored token assignment — discord*token = <50+ token-charset chars>.
check_pattern "Discord token assignment"                "discord[a-zA-Z_]*token[^=]{0,40}=.{0,4}[a-zA-Z0-9_.-]{50,}"

if [ "$FAIL" -eq 1 ]; then
    echo "Commit blocked. Fix or use --no-verify (not recommended)."
    echo "Skipped file types: .plist, launchd/, .original.md, file-history/, worktrees/."
    exit 1
fi

exit 0
