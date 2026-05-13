#!/usr/bin/env bash
# install-launchd-plists.sh — Install LaunchAgent plists with path substitution.
#
# Source plists in scripts/launchd/ use __USER_HOME__ as placeholder.
# This script expands it to $HOME and installs to ~/Library/LaunchAgents/.
#
# Usage:
#   bash scripts/install-launchd-plists.sh              # install all
#   bash scripts/install-launchd-plists.sh scheduler    # install matching
#   bash scripts/install-launchd-plists.sh --dry-run    # preview
#   bash scripts/install-launchd-plists.sh --uninstall  # remove all
set -euo pipefail

SRC_DIR="${FLEET_REPO_DIR:-$HOME/dev/er-simulator-superrepo}/scripts/launchd"
DST_DIR="$HOME/Library/LaunchAgents"
FILTER="${1:-}"
DRY_RUN=0
UNINSTALL=0

case "$FILTER" in
    --dry-run) DRY_RUN=1; FILTER="" ;;
    --uninstall) UNINSTALL=1; FILTER="" ;;
esac

[[ -d "$SRC_DIR" ]] || { echo "Source not found: $SRC_DIR" >&2; exit 1; }
mkdir -p "$DST_DIR"

installed=0
for src in "$SRC_DIR"/*.plist; do
    name="$(basename "$src")"
    [[ -n "$FILTER" && "$name" != *"$FILTER"* ]] && continue
    dst="$DST_DIR/$name"

    if [[ $UNINSTALL -eq 1 ]]; then
        if [[ -f "$dst" ]]; then
            launchctl unload "$dst" 2>/dev/null || true
            rm "$dst" && echo "removed: $name"
        fi
        continue
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        echo "would install: $name → $dst (expanding __USER_HOME__ → $HOME)"
        continue
    fi

    sed "s|__USER_HOME__|$HOME|g" "$src" > "$dst"
    launchctl unload "$dst" 2>/dev/null || true
    launchctl load "$dst"
    echo "installed: $name"
    installed=$((installed + 1))
done

[[ $DRY_RUN -eq 0 && $UNINSTALL -eq 0 ]] && echo "Installed $installed plist(s) to $DST_DIR"
