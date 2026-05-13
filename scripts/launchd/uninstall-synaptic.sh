#!/bin/bash
# Uninstall Synaptic launchd service
# Usage: ./uninstall-synaptic.sh

PLIST_NAME="io.contextdna.synaptic.plist"
DEST_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "Uninstalling Synaptic launchd service..."

# Unload if running
if launchctl list | grep -q "io.contextdna.synaptic"; then
    launchctl unload "$DEST_PLIST" 2>/dev/null
    echo "[OK] Service stopped"
fi

# Remove plist
if [ -f "$DEST_PLIST" ]; then
    rm "$DEST_PLIST"
    echo "[OK] Removed plist"
fi

echo ""
echo "Synaptic service uninstalled."
echo "Note: Log files preserved at ~/.context-dna/logs/"
