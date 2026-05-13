#!/bin/bash
# Install Synaptic launchd service for auto-start
# Usage: ./install-synaptic.sh

set -e

PLIST_NAME="io.contextdna.synaptic.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_PLIST="$SCRIPT_DIR/$PLIST_NAME"
DEST_DIR="$HOME/Library/LaunchAgents"
DEST_PLIST="$DEST_DIR/$PLIST_NAME"
LOG_DIR="$HOME/.context-dna/logs"

echo "Installing Synaptic launchd service..."
echo ""

# Create logs directory
mkdir -p "$LOG_DIR"
echo "[OK] Created logs directory: $LOG_DIR"

# Create LaunchAgents directory if needed
mkdir -p "$DEST_DIR"

# Unload existing service if running
if launchctl list | grep -q "io.contextdna.synaptic"; then
    echo "[INFO] Stopping existing service..."
    launchctl unload "$DEST_PLIST" 2>/dev/null || true
fi

# Copy plist to LaunchAgents
cp "$SOURCE_PLIST" "$DEST_PLIST"
echo "[OK] Copied plist to: $DEST_PLIST"

# Load the service
launchctl load "$DEST_PLIST"
echo "[OK] Service loaded"

# Verify it's running
sleep 2
if launchctl list | grep -q "io.contextdna.synaptic"; then
    echo ""
    echo "SUCCESS: Synaptic service installed and running!"
    echo ""
    echo "Service info:"
    echo "  - Chat UI:  http://localhost:8888"
    echo "  - Chat WS:  ws://localhost:8888/chat"
    echo "  - Voice WS: ws://localhost:8888/voice"
    echo ""
    echo "Logs:"
    echo "  - stdout: $LOG_DIR/synaptic.stdout.log"
    echo "  - stderr: $LOG_DIR/synaptic.stderr.log"
    echo ""
    echo "Commands:"
    echo "  Stop:    launchctl unload ~/Library/LaunchAgents/$PLIST_NAME"
    echo "  Start:   launchctl load ~/Library/LaunchAgents/$PLIST_NAME"
    echo "  Status:  launchctl list | grep synaptic"
    echo "  Logs:    tail -f $LOG_DIR/synaptic.stderr.log"
else
    echo ""
    echo "WARNING: Service may not have started correctly."
    echo "Check logs: tail -f $LOG_DIR/synaptic.stderr.log"
fi
