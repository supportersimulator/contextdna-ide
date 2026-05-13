#!/usr/bin/env bash
# lift-freeze.sh — restore CI workflows after the 2026-05-13 freeze lift.
#
# Aaron set CI budget=$0 on 2026-05-06 (~1 week freeze; lift ~2026-05-13).
# This script reverses that freeze in three places:
#   1. Repo workflows: rename .github/workflows/*.yml.frozen-2026-05-13 → *.yml
#   2. GATE workflows: clear repo variable FLEET_PUSH_FREEZE (gh variable delete)
#                      so docs.yml + roadmap-orphans-weekly.yml `if:` evaluates true.
#   3. Local push freeze: flip FLEET_PUSH_FREEZE=0 in:
#        - ~/Library/LaunchAgents/io.contextdna.fleet-git-msg.plist
#        - .git/hooks/post-commit
#      and bootstrap-reload the LaunchAgent.
#
# This script does NOT push automatically. Aaron must `git push origin main`
# manually after running this — that staged push is what turns CI back on.
#
# Idempotent: re-running on an already-lifted state is a no-op.
#
# Usage:
#   bash scripts/lift-freeze.sh            # apply
#   bash scripts/lift-freeze.sh --dry-run  # show what would change

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

run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] $*"
    else
        echo "[run]     $*"
        "$@"
    fi
}

inplace_sed() {
    # macOS portable: sed -i '' "$pattern" "$file"
    local pattern="$1" file="$2"
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] sed -i '' '$pattern' '$file'"
    else
        echo "[run]     sed -i '' '$pattern' '$file'"
        sed -i '' "$pattern" "$file"
    fi
}

echo "==> lift-freeze.sh  (dry-run=$DRY_RUN)"
echo "REPO_ROOT=$REPO_ROOT"
echo

# ---------------------------------------------------------------------------
# 1. Rename frozen workflow files back to .yml
# ---------------------------------------------------------------------------
echo "[1/3] Restore PAUSED workflow files"
if [ -d "$WF_DIR" ]; then
    shopt -s nullglob
    frozen_count=0
    for frozen in "$WF_DIR"/*.yml.frozen-2026-05-13; do
        live="${frozen%.frozen-2026-05-13}"
        if [ -e "$live" ]; then
            echo "  skip  $(basename "$frozen") — live file already present"
            continue
        fi
        run mv "$frozen" "$live"
        frozen_count=$((frozen_count + 1))
    done
    if [ "$frozen_count" = "0" ]; then
        echo "  none  no .frozen-2026-05-13 files found (already lifted?)"
    fi
    shopt -u nullglob
else
    echo "  warn  $WF_DIR not found"
fi
echo

# ---------------------------------------------------------------------------
# 2. Clear GitHub repo variable so GATE workflows run
# ---------------------------------------------------------------------------
echo "[2/3] Clear GitHub repo variable FLEET_PUSH_FREEZE (GATE workflows)"
if command -v gh >/dev/null 2>&1; then
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] gh variable set FLEET_PUSH_FREEZE --body 0  (or delete)"
    else
        # Set to 0 (preserves the var so it's discoverable in the UI). Delete
        # would also work but obscures intent.
        if gh variable set FLEET_PUSH_FREEZE --body "0" 2>/tmp/gh-var-set.err; then
            echo "  ok    gh variable set FLEET_PUSH_FREEZE=0"
        else
            echo "  warn  gh variable set failed (likely no PROJECTS_TOKEN scope or var nonexistent)"
            cat /tmp/gh-var-set.err >&2 || true
            echo "  hint  run manually: gh variable set FLEET_PUSH_FREEZE --body 0"
        fi
    fi
else
    echo "  warn  gh CLI not found — cannot toggle repo variable"
    echo "  hint  install gh, or in GitHub UI: Settings → Secrets and variables → Actions → Variables"
fi
echo

# ---------------------------------------------------------------------------
# 3. Flip local push-freeze flags
# ---------------------------------------------------------------------------
echo "[3/3] Flip local push-freeze flags"

# 3a. LaunchAgent plist
if [ -f "$PLIST" ]; then
    if grep -q '<key>FLEET_PUSH_FREEZE</key>' "$PLIST" 2>/dev/null; then
        # Replace whatever value follows that key with "0".
        # plist format: <key>FLEET_PUSH_FREEZE</key>\n<string>1</string>
        # Use a two-line sed pattern via labeled branch (portable on BSD sed).
        if [ "$DRY_RUN" = "1" ]; then
            echo "[dry-run] flip <string> after <key>FLEET_PUSH_FREEZE</key> to 0 in $PLIST"
        else
            python3 - "$PLIST" <<'PY'
import re, sys
p = sys.argv[1]
src = open(p).read()
new = re.sub(
    r'(<key>FLEET_PUSH_FREEZE</key>\s*<string>)[^<]*(</string>)',
    r'\g<1>0\g<2>',
    src,
)
if new != src:
    open(p, 'w').write(new)
    print('  ok    plist FLEET_PUSH_FREEZE=0')
else:
    print('  ok    plist already 0 or pattern not matched')
PY
        fi
    else
        echo "  warn  $PLIST has no FLEET_PUSH_FREEZE key (skipping)"
    fi

    # Bootstrap-reload the LaunchAgent so the new env takes effect.
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] launchctl bootout gui/$UID $PLIST  (ignored if not loaded)"
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
    if grep -q 'FLEET_PUSH_FREEZE:-1' "$HOOK" 2>/dev/null; then
        inplace_sed 's/FLEET_PUSH_FREEZE:-1/FLEET_PUSH_FREEZE:-0/g' "$HOOK"
    else
        echo "  ok    $HOOK already lifted (no FLEET_PUSH_FREEZE:-1 default)"
    fi
else
    echo "  warn  $HOOK not found (skipping)"
fi
echo

echo "==> Done."
echo
echo "NEXT — Aaron's manual steps to actually push:"
echo "  1. Verify state:    git status; git log origin/main..HEAD --oneline | wc -l"
echo "  2. Stage push order (small first, audit-able):"
echo "       git push origin main"
echo "       (or split: git push origin <sha>:refs/heads/main  for a single batch)"
echo "  3. Watch CI:        gh run watch  (or https://github.com/supportersimulator/er-simulator-superrepo/actions)"
echo "  4. Kill switch:     bash scripts/restore-freeze.sh  (re-freezes everything)"
echo
echo "If anything looks wrong, run: bash scripts/restore-freeze.sh"
