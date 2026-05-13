#!/bin/bash
# =============================================================================
# New Integration Notification - Interactive Approval
# =============================================================================
# Shows macOS notification when novel integration is discovered.
# User clicks to approve sharing with community.
#
# Usage: ./notify-new-integration.sh <discovery_id> <source_name>
# =============================================================================

DISCOVERY_ID="$1"
SOURCE_NAME="$2"

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON="$REPO_ROOT/.venv/bin/python3"

# Send notification with action button
osascript <<EOF
display notification "New Context DNA integration saved for use - you rock! Click to share with community." ¬
    with title "🎉 Integration Discovered: $SOURCE_NAME" ¬
    sound name "Glass"

# Wait a moment for user to see
delay 0.5

# Show dialog for approval
set userChoice to button returned of (display dialog "A new Context DNA integration was discovered and is working great!

Source: $SOURCE_NAME
Type: Novel integration pattern
Success rate: ≥75% (verified working)

Share this with the community?
• Your integration will help other users
• Config is anonymized (no secrets/paths)
• You'll be credited (optional)

Share with community?" ¬
    buttons {"Keep Private", "Share with Community"} ¬
    default button "Share with Community" ¬
    with title "Context DNA - New Integration" ¬
    with icon note)

# If user approves, trigger contribution
if userChoice is "Share with Community" then
    do shell script "cd '$REPO_ROOT' && PYTHONPATH=. '$PYTHON' memory/integration_discovery.py --approve '$DISCOVERY_ID' > /tmp/integration-contribution.log 2>&1 &"
    
    display notification "Thank you! Your integration is being shared with the community." ¬
        with title "🌟 Context DNA - Contribution Processing" ¬
        sound name "Glass"
end if
EOF

exit 0
