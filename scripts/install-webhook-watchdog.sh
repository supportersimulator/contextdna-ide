#!/bin/bash
# Install webhook-watchdog xbar plugin (5min schedule).
#
# Symlinks scripts/xbar-webhook-watchdog.5m.sh into xbar's plugin dir.
# Idempotent. Re-run safe. Use --uninstall to remove.
#
# Usage:
#   bash scripts/install-webhook-watchdog.sh
#   bash scripts/install-webhook-watchdog.sh --uninstall

set -e

UNINSTALL=0
[ "$1" = "--uninstall" ] && UNINSTALL=1

REPO=""
for d in "$HOME/dev/er-simulator-superrepo" "$HOME/Documents/er-simulator-superrepo"; do
    [ -d "$d" ] && REPO="$d" && break
done
[ -z "$REPO" ] && { echo "ERR: superrepo not found"; exit 1; }

XBAR_DIR="$HOME/Library/Application Support/xbar/plugins"
XBAR_LINK="$XBAR_DIR/xbar-webhook-watchdog.5m.sh"
XBAR_SRC="$REPO/scripts/xbar-webhook-watchdog.5m.sh"

if [ "$UNINSTALL" = "1" ]; then
    if [ -L "$XBAR_LINK" ] || [ -f "$XBAR_LINK" ]; then
        rm -f "$XBAR_LINK"
        echo "[uninstall] removed $XBAR_LINK"
    else
        echo "[uninstall] no link at $XBAR_LINK (nothing to do)"
    fi
    exit 0
fi

[ ! -f "$XBAR_SRC" ] && { echo "ERR: source missing: $XBAR_SRC"; exit 1; }

mkdir -p "$XBAR_DIR"
chmod +x "$XBAR_SRC"
chmod +x "$REPO/scripts/webhook-watchdog.sh" 2>/dev/null || true
ln -sf "$XBAR_SRC" "$XBAR_LINK"
echo "[install] xbar: $XBAR_LINK -> $XBAR_SRC"
echo "[install] schedule: every 5min (xbar OS scheduler)"
echo "[install] threshold: 600s plateau (10min)"
echo
echo "Verify: bash '$XBAR_LINK' | head -10"
