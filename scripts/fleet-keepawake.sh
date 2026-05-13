#!/bin/bash
# DEPRECATED: Use scripts/fleet-keepalive.sh or scripts/trip-mode.sh instead
# This script is kept for backward compatibility only.
echo "[DEPRECATED] Use: bash scripts/trip-mode.sh 72"
echo "  Or: bash scripts/fleet-keepalive.sh start"
exec bash "$(dirname "$0")/fleet-keepalive.sh" "$@"
