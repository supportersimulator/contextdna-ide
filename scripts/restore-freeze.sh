#!/usr/bin/env bash
# restore-freeze.sh — re-apply the CI freeze (kill switch / safety net).
#
# Inverse of scripts/lift-freeze.sh. Idempotent.
#
# Use cases:
#   * Aaron lifted the freeze, CI started burning unexpectedly → re-freeze
#   * Aaron wants to extend the freeze beyond 2026-05-13
#   * Atlas/another agent ran lift-freeze in error
#
# What this does:
#   1. Rename .github/workflows/{ci,lint,build-installers,vibe-coder-install-test}.yml
#      back to .yml.frozen-2026-05-13 (matches N3 audit decision matrix).
#   2. Set repo variable FLEET_PUSH_FREEZE=1 (re-engages GATE on docs.yml +
#      roadmap-orphans-weekly.yml).
#   3. Set FLEET_PUSH_FREEZE=1 in:
#        - ~/Library/LaunchAgents/io.contextdna.fleet-git-msg.plist
#        - .git/hooks/post-commit
#      and bootstrap-reload the LaunchAgent.
#
# Does NOT undo any commits — only stops outbound git push + workflow runs.
#
# Usage:
#   bash scripts/restore-freeze.sh             # apply
#   bash scripts/restore-freeze.sh --dry-run   # show what would change

set -uo pipefail

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        --help|-h)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg (use --dry-run or --help)" >&2
            exit 2
            ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WF_DIR="$REPO_ROOT/.github/workflows"
PLIST="$HOME/Library/LaunchAgents/io.contextdna.fleet-git-msg.plist"
HOOK="$REPO_ROOT/.git/hooks/post-commit"

# Workflows that get the PAUSE treatment (rename → .frozen-2026-05-13).
PAUSE_WORKFLOWS=(
    "ci.yml"
    "lint.yml"
    "build-installers.yml"
    "vibe-coder-install-test.yml"
)

run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] $*"
    else
        echo "[run]     $*"
        "$@"
    fi
}

inplace_sed() {
    local pattern="$1" file="$2"
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] sed -i '' '$pattern' '$file'"
    else
        echo "[run]     sed -i '' '$pattern' '$file'"
        sed -i '' "$pattern" "$file"
    fi
}

echo "==> restore-freeze.sh  (dry-run=$DRY_RUN)"
echo "REPO_ROOT=$REPO_ROOT"
echo

# ---------------------------------------------------------------------------
# 1. PAUSE: rename .yml → .yml.frozen-2026-05-13
# ---------------------------------------------------------------------------
echo "[1/3] Re-PAUSE workflow files (rename to .yml.frozen-2026-05-13)"
if [ -d "$WF_DIR" ]; then
    for wf in "${PAUSE_WORKFLOWS[@]}"; do
        live="$WF_DIR/$wf"
        frozen="$WF_DIR/$wf.frozen-2026-05-13"
        if [ -f "$frozen" ]; then
            echo "  skip  $wf — already frozen"
            continue
        fi
        if [ ! -f "$live" ]; then
            echo "  skip  $wf — neither live nor frozen variant present"
            continue
        fi
        run mv "$live" "$frozen"
    done
else
    echo "  warn  $WF_DIR not found"
fi
echo

# ---------------------------------------------------------------------------
# 2. Set repo variable FLEET_PUSH_FREEZE=1 (re-engage GATE)
# ---------------------------------------------------------------------------
echo "[2/3] Set GitHub repo variable FLEET_PUSH_FREEZE=1 (GATE workflows)"
if command -v gh >/dev/null 2>&1; then
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] gh variable set FLEET_PUSH_FREEZE --body 1"
    else
        if gh variable set FLEET_PUSH_FREEZE --body "1" 2>/tmp/gh-var-set.err; then
            echo "  ok    gh variable set FLEET_PUSH_FREEZE=1"
        else
            echo "  warn  gh variable set failed:"
            cat /tmp/gh-var-set.err >&2 || true
            echo "  hint  run manually: gh variable set FLEET_PUSH_FREEZE --body 1"
        fi
    fi
else
    echo "  warn  gh CLI not found — cannot toggle repo variable"
fi
echo

# ---------------------------------------------------------------------------
# 3. Set local push-freeze flags
# ---------------------------------------------------------------------------
echo "[3/3] Set local push-freeze flags to 1"

# 3a. LaunchAgent plist
if [ -f "$PLIST" ]; then
    if grep -q '<key>FLEET_PUSH_FREEZE</key>' "$PLIST" 2>/dev/null; then
        if [ "$DRY_RUN" = "1" ]; then
            echo "[dry-run] flip <string> after <key>FLEET_PUSH_FREEZE</key> to 1 in $PLIST"
        else
            python3 - "$PLIST" <<'PY'
import re, sys
p = sys.argv[1]
src = open(p).read()
new = re.sub(
    r'(<key>FLEET_PUSH_FREEZE</key>\s*<string>)[^<]*(</string>)',
    r'\g<1>1\g<2>',
    src,
)
if new != src:
    open(p, 'w').write(new)
    print('  ok    plist FLEET_PUSH_FREEZE=1')
else:
    print('  ok    plist already 1 or pattern not matched')
PY
        fi
    else
        echo "  warn  $PLIST has no FLEET_PUSH_FREEZE key (skipping)"
    fi

    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] launchctl bootout gui/$UID $PLIST"
        echo "[dry-run] launchctl bootstrap gui/$UID $PLIST"
    else
        launchctl bootout "gui/$UID" "$PLIST" 2>/dev/null || true
        if launchctl bootstrap "gui/$UID" "$PLIST" 2>/tmp/launchctl-bootstrap.err; then
            echo "  ok    launchctl bootstrap gui/$UID $PLIST"
        else
            echo "  warn  launchctl bootstrap failed:"
            cat /tmp/launchctl-bootstrap.err >&2 || true
        fi
    fi
else
    echo "  warn  $PLIST not found (skipping)"
fi

# 3b. post-commit hook
if [ -f "$HOOK" ]; then
    if grep -q 'FLEET_PUSH_FREEZE:-0' "$HOOK" 2>/dev/null; then
        inplace_sed 's/FLEET_PUSH_FREEZE:-0/FLEET_PUSH_FREEZE:-1/g' "$HOOK"
    else
        echo "  ok    $HOOK already frozen (default :-1 or no marker)"
    fi
else
    echo "  warn  $HOOK not found (skipping)"
fi
echo

echo "==> Done. Freeze re-engaged."
echo
echo "Verify:"
echo "  ls -la .github/workflows/"
echo "  grep FLEET_PUSH_FREEZE \"$PLIST\" \"$HOOK\""
echo "  gh variable list | grep FLEET_PUSH_FREEZE"
