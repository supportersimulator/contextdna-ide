#!/bin/bash
# install-python-fleet-wrapper.sh
#
# Installs /usr/local/bin/python-fleet as a symlink to the repo's
# scripts/python-fleet wrapper. Idempotent.
#
# WHY: see scripts/python-fleet header. Single TCC entry for Python across
# every future minor version.
#
# Usage:
#   bash scripts/install-python-fleet-wrapper.sh           # install
#   bash scripts/install-python-fleet-wrapper.sh --uninstall

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER_SRC="$REPO_ROOT/scripts/python-fleet"
INSTALL_PATH="/usr/local/bin/python-fleet"
PYTHON_BIN="/usr/local/bin/python3"

log() { echo "[install-python-fleet-wrapper] $*"; }
die() { echo "[install-python-fleet-wrapper] ERROR: $*" >&2; exit 1; }

uninstall() {
    if [ -L "$INSTALL_PATH" ] || [ -e "$INSTALL_PATH" ]; then
        log "removing $INSTALL_PATH"
        rm -f "$INSTALL_PATH"
        log "uninstalled"
    else
        log "nothing to remove at $INSTALL_PATH"
    fi
}

install_wrapper() {
    # ZSF: bail if upstream python3 missing — wrapper is useless without it.
    if [ ! -x "$PYTHON_BIN" ]; then
        die "$PYTHON_BIN not found or not executable. Install Homebrew python first (e.g. 'brew install python@3.14')."
    fi

    if [ ! -x "$WRAPPER_SRC" ]; then
        die "wrapper source not executable: $WRAPPER_SRC"
    fi

    # Idempotency: if already symlinked correctly, we're done.
    if [ -L "$INSTALL_PATH" ]; then
        existing="$(readlink "$INSTALL_PATH")"
        if [ "$existing" = "$WRAPPER_SRC" ]; then
            log "already installed: $INSTALL_PATH -> $WRAPPER_SRC"
            return 0
        fi
        log "replacing existing symlink ($INSTALL_PATH -> $existing)"
        rm -f "$INSTALL_PATH"
    elif [ -e "$INSTALL_PATH" ]; then
        die "$INSTALL_PATH exists and is not a symlink — refusing to overwrite. Move or remove it manually."
    fi

    # /usr/local/bin is owned by current user on Homebrew installs; if not,
    # ln will fail with a clear EACCES which we surface verbatim.
    log "linking $INSTALL_PATH -> $WRAPPER_SRC"
    ln -s "$WRAPPER_SRC" "$INSTALL_PATH"

    log "verifying:"
    "$INSTALL_PATH" --version
    log "installed. Approve $INSTALL_PATH once in System Settings -> Privacy & Security -> Automation if prompted."
}

case "${1:-}" in
    --uninstall|-u)
        uninstall
        ;;
    ""|--install)
        install_wrapper
        ;;
    -h|--help)
        sed -n '2,12p' "$0"
        ;;
    *)
        die "unknown arg: $1 (use --install, --uninstall, or --help)"
        ;;
esac
